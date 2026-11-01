/*-------------------------------------------------------------------------
 *
 * pgcolumnar_parallel_copy.c
 *		Parallel bulk ingest for pgColumnar (#300).
 *
 * Splits a load across background workers, each running core COPY over a
 * line-aligned byte range of the input file, so parse semantics stay exactly
 * core COPY's. See design/PARALLEL_COPY_PLAN.md.
 *
 * Contents:
 *   - a partition-aligned splitter (pcopy_partition_aligned_offsets): one forward
 *     pass over a key-sorted COPY text file, bucketing each row against the target's
 *     RANGE bounds, that returns byte ranges each covering a DISTINCT set of the
 *     target's partitions -- so no two workers ever write the same partition;
 *   - the atomic (2PC) load: the SQL function launches a coordinator background
 *     worker, which spawns N loader workers (each COPYs its byte range into the
 *     partitioned parent -- tuple routing sends its rows to its partitions only --
 *     then PREPARE TRANSACTIONs it), and then COMMIT PREPAREDs them all or ROLLBACK
 *     PREPAREDs them all, so a failure in any range leaves no partial load. Distinct
 *     partitions means distinct storage, which is what makes this parallel (no
 *     shared per-storage write lock) and 2PC-safe (no deadlock).
 *   - the single (non-partitioned) columnar table shape: N loaders write ONE shared
 *     storage concurrently. Here parallelism does not come from distinct storage;
 *     it comes from the coordinator pre-creating and committing the storage row so
 *     the loaders skip its creation lock, each writing via pgcolumnar_bulk_parallel_writer.
 *   - a standalone byte splitter (pgcolumnar_file_split_offsets) exposed to SQL: N+1
 *     line-aligned offsets, a diagnostic the parallel load itself no longer calls.
 * Text format only for now, numeric/date-time partition keys only (their text form
 * is escape-free); CSV and other key types are later phases (see the plan).
 *
 * Independent MIT implementation. References only the public PostgreSQL API
 * (server-file access, the COPY BeginCopyFrom interface, the background-worker and
 * two-phase-commit APIs, and the role check used by COPY FROM file). No
 * core/TimescaleDB/Citus/DuckDB/pg_background source consulted.
 *
 *-------------------------------------------------------------------------
 */
#include "postgres.h"

#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include "common/cryptohash.h"
#include "common/sha2.h"

/* read size for the dedup fingerprint pass */
#define PCOPY_FINGERPRINT_BUFSZ	(256 * 1024)

#include "columnar.h"

#include "columnar_write_state.h"
#include "access/relation.h"
#include "access/table.h"
#include "access/twophase.h"
#include "access/xact.h"
#include "catalog/pg_authid_d.h"
#include "catalog/pg_class.h"
#include "catalog/pg_type.h"
#include "commands/copy.h"
#include "fmgr.h"
#include "funcapi.h"
#include "libpq/pqsignal.h"
#include "access/sysattr.h"
#include "miscadmin.h"
#include "nodes/makefuncs.h"
#include "nodes/parsenodes.h"
#include "nodes/value.h"
#include "parser/parse_node.h"
#include "parser/parse_relation.h"
#include "partitioning/partbounds.h"
#include "partitioning/partdesc.h"
#include "postmaster/bgworker.h"
#include "storage/dsm.h"
#include "storage/fd.h"
#include "storage/ipc.h"
#include "storage/latch.h"
#include "storage/shm_toc.h"
#include "storage/shmem.h"
#include "tcop/tcopprot.h"
#if PG_VERSION_NUM >= 160000
#include "utils/wait_event.h"	/* PG_WAIT_EXTENSION */
#else
#include "pgstat.h"
#endif
#include "utils/acl.h"
#include "utils/array.h"
#include "utils/builtins.h"
#include "utils/guc.h"
#include "utils/lsyscache.h"
#include "utils/memutils.h"
#include "utils/partcache.h"
#include "utils/rel.h"
#include "utils/snapmgr.h"

/* 2PC global-transaction-id buffer size; core defines this, guard in case a
 * given major puts it in a header we don't reach here. */
#ifndef GIDSIZE
#define GIDSIZE 200
#endif

/* how much we read at a time while scanning the file */
#define COLUMNAR_SPLIT_SCAN_CHUNK 65536
/* upper bound on worker/range count, enforced everywhere the count is taken from
 * SQL, so a huge value cannot allocate gigabytes before doing any work */
#define PCOPY_MAX_WORKERS 1024

/*
 * pcopy_open_regular_file
 *		Open a server-side file for reading, requiring it to be a regular file.
 *		Rejecting directories, devices and FIFOs matches core COPY (copyfrom.c
 *		fstat + S_ISDIR) and avoids nonsense like a directory reported as an
 *		8-exabyte splittable file or an uninterruptible open() on a FIFO. Returns
 *		the fd, and the size via *size_out when size_out is not NULL.
 */
static int
pcopy_open_regular_file(const char *path, off_t *size_out)
{
	int			fd;
	struct stat st;

	/*
	 * Open with O_NONBLOCK so a FIFO open(2) returns immediately instead of
	 * blocking until a writer appears. A blocking open on a FIFO is uninterruptible
	 * across a cancel or statement_timeout (SA_RESTART), a denial of service. We
	 * then fstat the fd we hold and reject anything that is not a regular file, so
	 * there is no stat-before-open race: the file we checked is the file we opened.
	 * A regular file has O_NONBLOCK cleared below; it is a no-op on regular reads,
	 * but leaving it set on some platforms is untidy.
	 */
	fd = OpenTransientFile(path, O_RDONLY | O_NONBLOCK | PG_BINARY);
	if (fd < 0)
		ereport(ERROR,
				(errcode_for_file_access(),
				 errmsg("could not open file \"%s\" for reading: %m", path)));
	if (fstat(fd, &st) != 0)
	{
		int			save_errno = errno;

		CloseTransientFile(fd);
		errno = save_errno;
		ereport(ERROR,
				(errcode_for_file_access(),
				 errmsg("could not stat file \"%s\": %m", path)));
	}
	if (!S_ISREG(st.st_mode))
	{
		CloseTransientFile(fd);
		ereport(ERROR,
				(errcode(ERRCODE_WRONG_OBJECT_TYPE),
				 errmsg("\"%s\" is not a regular file", path)));
	}
	{
		int			flags = fcntl(fd, F_GETFL);

		if (flags == -1 || fcntl(fd, F_SETFL, flags & ~O_NONBLOCK) == -1)
		{
			int			save_errno = errno;

			CloseTransientFile(fd);
			errno = save_errno;
			ereport(ERROR,
					(errcode_for_file_access(),
					 errmsg("could not clear nonblocking mode on file \"%s\": %m", path)));
		}
	}
	if (size_out != NULL)
		*size_out = st.st_size;
	return fd;
}

/*
 * pcopy_line_offsets
 *		Compute nranges+1 line-aligned byte offsets partitioning an open file of
 *		`size` bytes into that many ranges, in ONE forward pass (O(filesize),
 *		interruptible), rather than a separate seek+scan per boundary. Each
 *		interior boundary is the first byte after the first newline at or beyond
 *		the even split point size*i/nranges; ranges may be empty when the file has
 *		fewer records than ranges. Returns a palloc'd int64[nranges+1].
 */
static int64 *
pcopy_line_offsets(int fd, off_t size, int nranges, const char *path)
{
	int64	   *offs = (int64 *) palloc(sizeof(int64) * (nranges + 1));
	char	   *buf = (char *) palloc(COLUMNAR_SPLIT_SCAN_CHUNK);
	int64		bufbase = 0;		/* absolute file offset of buf[0] */
	int			next = 1;

	offs[0] = 0;
	offs[nranges] = (int64) size;

	if (lseek(fd, 0, SEEK_SET) < 0)
		ereport(ERROR,
				(errcode_for_file_access(),
				 errmsg("could not seek in file \"%s\": %m", path)));

	while (next < nranges && bufbase < size)
	{
		int			got = (int) read(fd, buf, COLUMNAR_SPLIT_SCAN_CHUNK);
		int			j;

		if (got < 0)
			ereport(ERROR,
					(errcode_for_file_access(),
					 errmsg("could not read file \"%s\": %m", path)));
		if (got == 0)
			break;
		for (j = 0; j < got && next < nranges; j++)
		{
			if (buf[j] == '\n')
			{
				int64		boundary = bufbase + j + 1;

				/*
				 * This newline is the first at or beyond every still-unfilled
				 * split point it reaches; assign it to all of them (equal
				 * consecutive offsets = empty ranges).
				 */
				while (next < nranges &&
					   (int64) ((double) size * next / nranges) <= bufbase + j)
					offs[next++] = boundary;
			}
		}
		bufbase += got;
		CHECK_FOR_INTERRUPTS();
	}

	/* any split points past the last newline get the end of file */
	while (next < nranges)
		offs[next++] = (int64) size;

	pfree(buf);
	return offs;
}

/*
 * pcopy_naive_offsets
 *		Line-aligned byte offsets partitioning `path` into `workers` even ranges,
 *		for the single-table load where every worker writes the one storage and any
 *		record-aligned split is correct (no partition key, so no ordering
 *		requirement). Returns a palloc'd int64[workers+1].
 */
static int64 *
pcopy_naive_offsets(const char *path, int workers)
{
	int			fd;
	off_t		size;
	int64	   *offs;

	fd = pcopy_open_regular_file(path, &size);
	offs = pcopy_line_offsets(fd, size, workers, path);
	CloseTransientFile(fd);
	return offs;
}

PG_FUNCTION_INFO_V1(pgcolumnar_file_split_offsets);

/*
 * pgcolumnar_file_split_offsets(path text, workers int) -> bigint[]
 *
 * Returns workers+1 ascending byte offsets [0 .. filesize] that split the file
 * into `workers` line-aligned ranges. off[0] is always 0 and off[workers] is
 * always the file size; each interior boundary is placed at the first byte after
 * the newline that follows the even split point filesize*i/workers. This is a
 * record boundary only for COPY *text* format, where a raw newline always ends a
 * record (text format escapes any embedded newline). It is NOT safe for CSV,
 * whose quoted fields may contain literal newlines -- quote-aware splitting is a
 * later phase; callers holding a CSV must not use these offsets. Ranges may be
 * empty (equal consecutive offsets) when the file has fewer records than workers
 * -- that worker then loads nothing, which is harmless. `workers` is capped at
 * PCOPY_MAX_WORKERS.
 */
Datum
pgcolumnar_file_split_offsets(PG_FUNCTION_ARGS)
{
	char	   *path;
	int32		workers;
	int			fd;
	off_t		size;
	int64	   *offs;
	Datum	   *elems;
	ArrayType  *result;
	int			i;

	/* the SQL wrapper is STRICT, so NULL path/workers never reach here */
	path = text_to_cstring(PG_GETARG_TEXT_PP(0));
	workers = PG_GETARG_INT32(1);

	if (workers < 1)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("workers must be at least 1")));
	/*
	 * Bound the range count before allocating anything: this helper is callable
	 * directly from SQL, and workers => 100000000 would otherwise palloc gigabytes
	 * for the offset array before reading a byte.
	 */
	if (workers > PCOPY_MAX_WORKERS)
		workers = PCOPY_MAX_WORKERS;

	/*
	 * Reading a server-side file is the same privilege COPY FROM file requires:
	 * membership in pg_read_server_files (which superusers hold). Enforce it here
	 * so the splitter cannot be used to probe arbitrary files without it.
	 */
	if (!has_privs_of_role(GetUserId(), ROLE_PG_READ_SERVER_FILES))
		ereport(ERROR,
				(errcode(ERRCODE_INSUFFICIENT_PRIVILEGE),
				 errmsg("must be superuser or a member of the pg_read_server_files role to split a server file"),
				 errhint("Anyone can COPY from a file if they are a member of the "
						 "pg_read_server_files role.")));

	fd = pcopy_open_regular_file(path, &size);
	offs = pcopy_line_offsets(fd, size, workers, path);
	CloseTransientFile(fd);

	elems = (Datum *) palloc(sizeof(Datum) * (workers + 1));
	for (i = 0; i <= workers; i++)
		elems[i] = Int64GetDatum(offs[i]);

	result = construct_array(elems, workers + 1, INT8OID,
							 sizeof(int64), FLOAT8PASSBYVAL, TYPALIGN_DOUBLE);

	PG_RETURN_ARRAYTYPE_P(result);
}

/* -------------------------------------------------------------------------
 * Parallel bulk ingest: atomic (2PC) coordinator + loader workers (#300, phase 3).
 *
 * The SQL function pgcolumnar.parallel_copy computes the line-aligned byte ranges,
 * lays out one DSM segment (a control header + a per-worker slot array), and
 * launches ONE coordinator background worker; then it just waits and returns the
 * coordinator's total. The coordinator does the real orchestration:
 *
 *   1. launches N loader background workers over the same DSM segment;
 *   2. each loader runs core COPY over its byte range via BeginCopyFrom with a
 *      bounded data source (so parse/write semantics are exactly core COPY's),
 *      then PREPARE TRANSACTION 'gid' instead of committing, reports rows +
 *      PREPARED (or FAILED) through its shared slot, and exits;
 *   3. once every loader has exited, the coordinator COMMIT PREPAREDs all of them
 *      if all prepared, or ROLLBACK PREPAREDs the prepared ones if any failed.
 *
 * Atomicity: because no loader commits on its own, a failure in any range
 * (a bad row, disk full, a constraint) leaves the whole load rolled back -- the
 * target is byte-identical to its pre-load state. The one residual window is the
 * standard 2PC in-doubt case: a coordinator crash *during* the final COMMIT
 * PREPARED loop can leave some ranges committed and some still prepared, which a
 * DBA resolves (prepared transactions are WAL-durable and listed in
 * pg_prepared_xacts). See design/PARALLEL_COPY_PLAN.md.
 *
 * Why a coordinator bgworker at all: COMMIT PREPARED / ROLLBACK PREPARED cannot
 * run inside a transaction block, and a SQL function always is one, so the
 * function itself cannot finish the 2PC. A background worker is its own top-level
 * session and can.
 * ------------------------------------------------------------------------- */

#define PCOPY_MAGIC			0x50434f50	/* 'PCOP' */
#define PCOPY_KEY_HEADER	0
#define PCOPY_KEY_WORKERS	1

/* per-loader outcome, reported through the slot's atomic state word */
typedef enum PcopyState
{
	PCOPY_PENDING = 0,			/* not yet finished (also: loader crashed) */
	PCOPY_PREPARED,				/* range loaded and PREPARE TRANSACTION'd */
	PCOPY_FAILED				/* loader caught an error; see errmsg */
} PcopyState;

/* coordinator outcome, reported to the waiting function through the header */
typedef enum PcopyCoordState
{
	PCOPY_COORD_PENDING = 0,	/* coordinator has not finished (also: crashed) */
	PCOPY_COORD_DONE,			/* all ranges committed; total_rows is valid */
	PCOPY_COORD_FAILED			/* rolled back / errored; see coord_errmsg */
} PcopyCoordState;

typedef struct PcopyWorkerSlot
{
	int64		start_off;		/* byte range [start_off, end_off) for this worker */
	int64		end_off;
	pg_atomic_uint32 state;		/* PcopyState */
	int64		rows;			/* rows loaded (valid when state == PCOPY_PREPARED) */
	int			sqlerrcode;
	char		errmsg[512];
	char		gid[GIDSIZE];	/* 2PC gid the coordinator assigned this loader */
} PcopyWorkerSlot;

typedef struct PcopyHeader
{
	Oid			dbid;
	Oid			roleid;			/* the caller's user id, with secContext */
	int			secContext;
	Oid			authid;			/* the authenticated user, which workers connect as below 17 (#1585) */
	bool		roleIsSuperuser;	/* roleid's superuser flag, for the worker's session user */
	Oid			relid;			/* target relation (partitioned parent, or a single columnar table) */
	int			nworkers;
	bool		single_table;	/* target is one columnar table (not partitioned):
								 * coordinator pre-creates the storage row and loaders
								 * set pgcolumnar_bulk_parallel_writer */
	char		filename[MAXPGPATH];
	/* coordinator -> function result channel */
	pg_atomic_uint32 coord_state;	/* PcopyCoordState */
	int64		total_rows;		/* valid when coord_state == PCOPY_COORD_DONE */
	int			failed_worker;	/* index of the first failed loader, or -1 */
	int			coord_sqlerrcode;
	char		coord_errmsg[512];

	/*
	 * Opt-in load dedup (#403 item 7). dedup is what the caller asked for;
	 * fingerprint is the SHA-256 of the file, computed by the coordinator while
	 * the loaders run; skipped_duplicate reports back that the load was refused
	 * because this table has already taken this file.
	 */
	bool		dedup;
	uint8		fingerprint[PG_SHA256_DIGEST_LENGTH];
	bool		skipped_duplicate;
} PcopyHeader;

/*
 * The COPY data-source callback has no context argument, and a worker runs one
 * COPY at a time, so the bounded range lives in this per-process static. It hands
 * COPY only the bytes in the worker's range and reports EOF at the range end.
 */
typedef struct PcopyRangeSource
{
	int			fd;
	int64		remaining;
	const char *path;
} PcopyRangeSource;

static PcopyRangeSource pcopy_src = {-1, 0, NULL};

/* background-worker entry points, resolved via bgw_function_name */
PGDLLEXPORT void pgcolumnar_parallel_copy_worker(Datum main_arg);
PGDLLEXPORT void pgcolumnar_parallel_copy_coordinator(Datum main_arg);

static int
pcopy_range_read(void *outbuf, int minread, int maxread)
{
	int			total = 0;

	(void) minread;				/* we return up to maxread; COPY re-calls as needed */
	while (total < maxread && pcopy_src.remaining > 0)
	{
		int			want = (int) Min((int64) (maxread - total), pcopy_src.remaining);
		int			got = (int) read(pcopy_src.fd, (char *) outbuf + total, want);

		if (got < 0)
			ereport(ERROR,
					(errcode_for_file_access(),
					 errmsg("could not read file \"%s\": %m",
							pcopy_src.path ? pcopy_src.path : "")));
		if (got == 0)
			break;				/* unexpected EOF inside the range */
		total += got;
		pcopy_src.remaining -= got;
	}
	return total;
}

/*
 * pcopy_partition_bucket
 *		For a value `kd`, return the number of range bound datums it is >= (its
 *		"bucket"): 0 for the first partition, up to ndatums for the last. Binary
 *		search over the ascending bound datums using the partition key's compare
 *		support function. For a covering RANGE layout this bucket is the partition
 *		ordinal, so distinct buckets => distinct partitions.
 */
static int
pcopy_partition_bucket(Datum kd, PartitionBoundInfo bi, FmgrInfo *cmpfn, Oid coll)
{
	int			lo = 0;
	int			hi = bi->ndatums;

	while (lo < hi)
	{
		int			mid = (lo + hi) / 2;
		int32		c;

		/*
		 * A MINVALUE/MAXVALUE range bound stores an UNDEFINED datum (the bound
		 * info is palloc0'd, so datums[mid][0] reads as 0); its real meaning is in
		 * kind[mid][0]. Core's partition_rbound_datum_cmp checks kind before
		 * touching the datum, and so must we -- otherwise a signed key (int, or
		 * timestamp[tz], which is negative before 2000-01-01) straddling 0 is
		 * mis-bucketed around an unbounded first/last partition, planting a worker
		 * boundary inside one partition and reintroducing the write-lock deadlock.
		 */
		if (bi->kind[mid][0] == PARTITION_RANGE_DATUM_MINVALUE)
			c = 1;				/* bound is -inf: key is always greater */
		else if (bi->kind[mid][0] == PARTITION_RANGE_DATUM_MAXVALUE)
			c = -1;				/* bound is +inf: key is always smaller */
		else
			c = DatumGetInt32(FunctionCall2Coll(cmpfn, coll, kd,
												bi->datums[mid][0]));

		if (c >= 0)
			lo = mid + 1;		/* kd >= this bound; it's in a higher bucket */
		else
			hi = mid;
	}
	return lo;
}

/*
 * pcopy_partition_aligned_offsets
 *		Split a COPY text-format file into *workers_io byte ranges ALIGNED to the
 *		range-partition boundaries of `parent`, so each worker's range routes to a
 *		distinct set of partitions and no two workers ever write the same partition
 *		(the storage-lock contention that makes same-table parallel load serialize
 *		and 2PC deadlock). Requires the file sorted ascending by the partition key
 *		(verified as we scan). Returns a palloc'd int64[*workers_io + 1]; may lower
 *		*workers_io to the number of partition segments.
 *
 * v1 restrictions (all checked): single-column RANGE partitioning, no DEFAULT
 * partition, non-expression key, COPY text format with default (whole-table,
 * table-order) column list, and a key field free of COPY escapes (true for the
 * numeric/temporal keys people range-partition on).
 */
static int64 *
pcopy_partition_aligned_offsets(Relation parent, const char *path, int *workers_io)
{
	PartitionKey pkey = RelationGetPartitionKey(parent);
	PartitionDesc pdesc = RelationGetPartitionDesc(parent, false);
	PartitionBoundInfo bi;
	TupleDesc	td = RelationGetDescr(parent);
	AttrNumber	keyattno;
	int			keyfield = 0;
	Oid			keytype,
				ioparam,
				infnoid,
				keycoll;
	int32		keytypmod;
	FmgrInfo	infn;
	FmgrInfo   *cmpfn;
	int			nseg;
	int64	   *seg_start;
	int64	   *woff;
	int			W;
	off_t		size;
	FILE	   *fp;
	char	   *line = NULL;
	size_t		cap = 0;
	ssize_t		len;
	int64		off = 0;
	int			cur = 0;
	int			prev_bucket = 0;
	uint64		nrows = 0;
	int			a;
	int			b;

	if (pkey->strategy != PARTITION_STRATEGY_RANGE)
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("pgcolumnar.parallel_copy supports only RANGE-partitioned targets"),
				 errhint("Partition the target by the load's sort key (e.g. time).")));
	if (pkey->partnatts != 1 || pkey->partattrs[0] == 0)
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("pgcolumnar.parallel_copy supports only single-column RANGE partition keys")));
	bi = pdesc->boundinfo;
	if (bi == NULL || bi->ndatums == 0)
		ereport(ERROR,
				(errcode(ERRCODE_OBJECT_NOT_IN_PREREQUISITE_STATE),
				 errmsg("target \"%s\" has no partitions to load into",
						RelationGetRelationName(parent))));
	if (bi->default_index >= 0)
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("pgcolumnar.parallel_copy does not support a DEFAULT partition"),
				 errhint("A default partition can receive rows from any range, so workers could not stay on distinct partitions.")));

	/*
	 * Which tab-delimited field is the key. A default COPY column list is the
	 * non-dropped, non-generated attributes in order, so the field index must skip
	 * BOTH -- a generated column before the key would otherwise shift every field.
	 */
	keyattno = pkey->partattrs[0];
	for (a = 1; a < keyattno; a++)
	{
		Form_pg_attribute att = TupleDescAttr(td, a - 1);

		if (!att->attisdropped && att->attgenerated == '\0')
			keyfield++;
	}

	keytype = pkey->parttypid[0];
	keytypmod = pkey->parttypmod[0];
	keycoll = pkey->partcollation[0];

	/*
	 * Restrict the key to numeric/date-time types. Their COPY text representation
	 * never contains a delimiter, newline or backslash, so reading the raw field
	 * without COPY de-escaping is exact. Other types (text, etc.) can carry escapes
	 * that would mis-parse and mis-bucket a row -- de-escaping them is a planned
	 * enhancement, and until then we reject rather than silently misroute.
	 */
	{
		char		cat;
		bool		preferred;

		get_type_category_preferred(keytype, &cat, &preferred);
		if (cat != TYPCATEGORY_NUMERIC && cat != TYPCATEGORY_DATETIME)
			ereport(ERROR,
					(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
					 errmsg("pgcolumnar.parallel_copy supports only numeric or date/time partition keys"),
					 errhint("The partition key is read from the text file without COPY de-escaping; support for other key types is a planned enhancement.")));
	}

	getTypeInputInfo(keytype, &infnoid, &ioparam);
	fmgr_info(infnoid, &infn);
	cmpfn = &pkey->partsupfunc[0];

	nseg = bi->ndatums + 1;		/* buckets 0..ndatums */
	seg_start = (int64 *) palloc(sizeof(int64) * (nseg + 1));
	seg_start[0] = 0;

	/*
	 * Open O_NONBLOCK so a FIFO cannot block the open(2) -- a cancel-resistant
	 * availability DoS (#644/#686) -- then fstat the fd we hold and reject a
	 * non-regular file, with no stat-before-open race: the file checked is the
	 * file opened. Clear O_NONBLOCK and fdopen for getline(), which needs stdio.
	 * A raw open + fdopen (not OpenTransientFile) is used so fclose fully closes it
	 * without leaving fd.c's transient-file bookkeeping dangling; every error path
	 * here closes the fd, and the getline loop's fclose runs under the PG_TRY
	 * below. The caller has already checked pg_read_server_files.
	 */
	{
		int			fd = open(path, O_RDONLY | O_NONBLOCK | PG_BINARY);
		struct stat st;
		int			flags;

		if (fd < 0)
			ereport(ERROR,
					(errcode_for_file_access(),
					 errmsg("could not open file \"%s\" for reading: %m", path)));
		if (fstat(fd, &st) != 0)
		{
			int			save_errno = errno;

			close(fd);
			errno = save_errno;
			ereport(ERROR,
					(errcode_for_file_access(),
					 errmsg("could not stat file \"%s\": %m", path)));
		}
		if (!S_ISREG(st.st_mode))
		{
			close(fd);
			ereport(ERROR,
					(errcode(ERRCODE_WRONG_OBJECT_TYPE),
					 errmsg("\"%s\" is not a regular file", path)));
		}
		flags = fcntl(fd, F_GETFL);
		if (flags == -1 || fcntl(fd, F_SETFL, flags & ~O_NONBLOCK) == -1)
		{
			int			save_errno = errno;

			close(fd);
			errno = save_errno;
			ereport(ERROR,
					(errcode_for_file_access(),
					 errmsg("could not clear nonblocking mode on file \"%s\": %m", path)));
		}
		size = st.st_size;
		fp = fdopen(fd, PG_BINARY_R);
		if (fp == NULL)
		{
			int			save_errno = errno;

			close(fd);
			errno = save_errno;
			ereport(ERROR,
					(errcode_for_file_access(),
					 errmsg("could not open file \"%s\" for reading: %m", path)));
		}
	}

	/*
	 * Wrap the scan so every exit path -- including an implicit throw from
	 * InputFunctionCall on an unparseable key field (a \N NULL marker, an empty
	 * field, non-numeric text) -- frees the getline() buffer (malloc'd, NOT
	 * reclaimed by memory-context reset) and the AllocateFile handle.
	 */
	PG_TRY();
	{
		while ((len = getline(&line, &cap, fp)) != -1)
		{
			int64		line_start = off;
			char	   *p = line;
			int			f = 0;
			int			flen;
			char	   *fld;
			Datum		kd;
			int			bucket;

			off += len;

			/* advance to the key field */
			for (f = 0; f < keyfield; f++)
			{
				p = strchr(p, '\t');
				if (p == NULL)
					break;
				p++;
			}
			if (p == NULL)
				ereport(ERROR,
						(errcode(ERRCODE_BAD_COPY_FILE_FORMAT),
						 errmsg("row at byte %ld has too few columns for the partition key",
								(long) line_start)));
			/* field ends at a tab, or a bare/CRLF line ending -- stopping at \r
			 * too keeps a trailing \r out of the key value */
			flen = (int) strcspn(p, "\t\r\n");
			fld = pnstrdup(p, flen);
			kd = InputFunctionCall(&infn, fld, ioparam, keytypmod);
			pfree(fld);

			bucket = pcopy_partition_bucket(kd, bi, cmpfn, keycoll);
			/* free the parsed key: numeric and other by-reference key types would
			 * otherwise leak one Datum per row across the whole pre-scan */
			if (!pkey->parttypbyval[0])
				pfree(DatumGetPointer(kd));
			if (bucket < prev_bucket)
				ereport(ERROR,
						(errcode(ERRCODE_DATA_EXCEPTION),
						 errmsg("input file is not sorted ascending by the partition key"),
						 errdetail("Row at byte %ld belongs to an earlier partition than a preceding row.",
								   (long) line_start),
						 errhint("Partition-parallel load requires the file sorted by the partition key.")));
			while (cur < bucket)
				seg_start[++cur] = line_start;
			prev_bucket = bucket;

			if ((++nrows & 0xFFFF) == 0)
				CHECK_FOR_INTERRUPTS();
		}
	}
	PG_CATCH();
	{
		if (line)
			free(line);
		fclose(fp);
		PG_RE_THROW();
	}
	PG_END_TRY();
	if (line)
		free(line);				/* getline() uses malloc, not palloc */
	fclose(fp);

	while (cur < nseg - 1)
		seg_start[++cur] = size;
	seg_start[nseg] = size;

	/* group contiguous segments into workers, balanced by bytes, snapped to
	 * partition edges so each worker owns whole (distinct) partitions */
	W = *workers_io;
	if (W > nseg)
		W = nseg;
	if (W < 1)
		W = 1;
	woff = (int64 *) palloc(sizeof(int64) * (W + 1));
	woff[0] = 0;
	{
		int64		target = size / W;
		int			wi = 1;
		int64		running = 0;

		for (b = 0; b < nseg && wi < W; b++)
		{
			running += seg_start[b + 1] - seg_start[b];
			if (running >= target * wi)
				woff[wi++] = seg_start[b + 1];
		}
		while (wi <= W)
			woff[wi++] = size;
	}

	pfree(seg_start);
	*workers_io = W;
	return woff;
}

/*
 * pcopy_auto_workers
 *		The worker count to use when the caller does not pass one. Derived from the
 *		admin's existing parallelism budget (max_parallel_workers) rather than a
 *		fixed 8: defaulting to the whole background-worker pool would starve
 *		autovacuum and everything else that needs a slot. Half the budget, at least
 *		one, capped at PCOPY_MAX_WORKERS.
 */
static int
pcopy_auto_workers(void)
{
	const char *s = GetConfigOption("max_parallel_workers", true, false);
	int			budget = (s != NULL) ? atoi(s) : 8;
	int			n = budget / 2;

	if (n < 1)
		n = 1;
	if (n > PCOPY_MAX_WORKERS)
		n = PCOPY_MAX_WORKERS;
	return n;
}

/*
 * pgcolumnar_parallel_copy_worker (loader)
 *		Background-worker entry: attach the DSM, connect, run core COPY over this
 *		worker's byte range into the target, then PREPARE TRANSACTION 'gid' rather
 *		than committing. Reports rows + PREPARED (or FAILED) through the shared slot
 *		before exit; the coordinator finishes the 2PC.
 */
PGDLLEXPORT void
pgcolumnar_parallel_copy_worker(Datum main_arg)
{
	dsm_segment *seg;
	shm_toc    *toc;
	PcopyHeader *hdr;
	PcopyWorkerSlot *slots;
	PcopyWorkerSlot *me;
	int			widx;

	memcpy(&widx, MyBgworkerEntry->bgw_extra, sizeof(int));

	pqsignal(SIGTERM, die);
	BackgroundWorkerUnblockSignals();

	seg = dsm_attach(DatumGetUInt32(main_arg));
	if (seg == NULL)
		ereport(ERROR,
				(errcode(ERRCODE_INTERNAL_ERROR),
				 errmsg("pgcolumnar parallel_copy worker could not attach to the shared segment")));
	toc = shm_toc_attach(PCOPY_MAGIC, dsm_segment_address(seg));
	if (toc == NULL)
		ereport(ERROR,
				(errcode(ERRCODE_INTERNAL_ERROR),
				 errmsg("pgcolumnar parallel_copy worker found a bad shared segment")));
	hdr = (PcopyHeader *) shm_toc_lookup(toc, PCOPY_KEY_HEADER, false);
	slots = (PcopyWorkerSlot *) shm_toc_lookup(toc, PCOPY_KEY_WORKERS, false);
	me = &slots[widx];

	columnar_worker_connect_as(hdr->dbid, hdr->authid, hdr->roleid,
							   hdr->roleIsSuperuser);

	/*
	 * Single-table load: this loader writes the one shared storage concurrently
	 * with its siblings. The coordinator has pre-created and committed the storage
	 * row, so opt this session into skipping the storage-row creation lock -- the
	 * only transaction-length serializer on the same-storage write path. For a
	 * partitioned target each loader owns distinct partitions (distinct storage) and
	 * never contends, so the flag stays off there.
	 */
	if (hdr->single_table)
		pgcolumnar_bulk_parallel_writer = true;

	PG_TRY();
	{
		Relation	rel;
		ParseState *pstate;
		List	   *options;
		CopyFromState cstate;
		uint64		processed;
		int			fd;

		/*
		 * An explicit transaction block (BEGIN) so we can PREPARE it: a bare
		 * implicit transaction cannot be prepared (PrepareTransactionBlock would
		 * turn into a no-op rollback).
		 */
		StartTransactionCommand();
		BeginTransactionBlock();
		CommitTransactionCommand();		/* TBLOCK_BEGIN -> in progress */
		/* the caller's security context, now that a transaction is open (#1612) */
		columnar_worker_take_context(hdr->roleid, hdr->secContext);

		fd = pcopy_open_regular_file(hdr->filename, NULL);
		if (lseek(fd, (off_t) me->start_off, SEEK_SET) < 0)
			ereport(ERROR,
					(errcode_for_file_access(),
					 errmsg("could not seek in file \"%s\": %m", hdr->filename)));
		pcopy_src.fd = fd;
		pcopy_src.remaining = me->end_off - me->start_off;
		pcopy_src.path = hdr->filename;

		rel = table_open(hdr->relid, RowExclusiveLock);
		pstate = make_parsestate(NULL);
		pstate->p_sourcetext = "(pgcolumnar parallel_copy)";

		/*
		 * BeginCopyFrom/CopyFrom expect the target in the ParseState's range
		 * table (with INSERT permission recorded), exactly as core's DoCopy sets
		 * up before a COPY FROM. PG16 moved permission info out of RangeTblEntry
		 * into a separate RTEPermissionInfo, so guard that.
		 */
		{
			TupleDesc	td = RelationGetDescr(rel);
			int			an;
#if PG_VERSION_NUM >= 160000
			ParseNamespaceItem *nsitem =
				addRangeTableEntryForRelation(pstate, rel, RowExclusiveLock,
											  NULL, false, false);
			RTEPermissionInfo *perminfo = nsitem->p_perminfo;

			perminfo->requiredPerms = ACL_INSERT;
			for (an = 1; an <= td->natts; an++)
				if (!TupleDescAttr(td, an - 1)->attisdropped)
					perminfo->insertedCols =
						bms_add_member(perminfo->insertedCols,
									   an - FirstLowInvalidHeapAttributeNumber);
#else
			/* PG15: addRangeTableEntryForRelation returns a ParseNamespaceItem;
			 * permissions live on the RangeTblEntry itself (no RTEPermissionInfo). */
			ParseNamespaceItem *nsitem =
				addRangeTableEntryForRelation(pstate, rel, RowExclusiveLock,
											  NULL, false, false);
			RangeTblEntry *rte = nsitem->p_rte;

			rte->requiredPerms = ACL_INSERT;
			for (an = 1; an <= td->natts; an++)
				if (!TupleDescAttr(td, an - 1)->attisdropped)
					rte->insertedCols =
						bms_add_member(rte->insertedCols,
									   an - FirstLowInvalidHeapAttributeNumber);
#endif
		}

		options = list_make1(makeDefElem("format",
										 (Node *) makeString("text"), -1));
		/*
		 * Core runs COPY FROM inside a portal that pushes an active snapshot
		 * (PortalRunUtility) before CopyFrom. We call CopyFrom directly, so we
		 * push one ourselves: the columnar insert path reads pgcolumnar.options
		 * via a visibility-checked systable scan, which requires a registered or
		 * active snapshot. Pop it before PREPARE (a prepared transaction must
		 * carry no active snapshot).
		 */
		PushActiveSnapshot(GetTransactionSnapshot());
		cstate = BeginCopyFrom(pstate, rel, NULL, NULL, false,
							   pcopy_range_read, NIL, options);
		processed = CopyFrom(cstate);
		EndCopyFrom(cstate);
		PopActiveSnapshot();
		free_parsestate(pstate);
		table_close(rel, NoLock);
		CloseTransientFile(fd);
		pcopy_src.fd = -1;

		/*
		 * PREPARE instead of commit. The transaction becomes durable and
		 * dissociated from this process; the coordinator (running as the same
		 * role, so LockGXact accepts it) will COMMIT PREPARED or ROLLBACK
		 * PREPARED it by gid once every loader has reported. After this the
		 * loader holds no transaction, so writing the result slot is a plain
		 * shared-memory store.
		 */
		/*
		 * PREPARE only as the caller (#1612 review). A trigger can change role
		 * inside this transaction (SET ROLE to a role the caller belongs to, or a
		 * transaction-local set_config('role')), and a transaction prepared as
		 * another user is one the coordinator, running as the caller, cannot
		 * finish: it would stay prepared, holding its locks, until someone ran
		 * ROLLBACK PREPARED. Refused here it aborts like any other error, and the
		 * coordinator rolls the load back.
		 */
		{
			Oid			nowUser;
			int			nowContext;

			GetUserIdAndSecContext(&nowUser, &nowContext);
			if (nowUser != hdr->roleid || nowContext != hdr->secContext)
				ereport(ERROR,
						(errcode(ERRCODE_INSUFFICIENT_PRIVILEGE),
						 errmsg("pgcolumnar parallel_copy worker is no longer running as the role that called parallel_copy"),
						 errdetail("A trigger changed the role during the load, so its transaction cannot be prepared for the caller to finish.")));
		}

		if (!PrepareTransactionBlock(me->gid))
			ereport(ERROR,
					(errcode(ERRCODE_INTERNAL_ERROR),
					 errmsg("pgcolumnar parallel_copy worker could not prepare transaction \"%s\"",
							me->gid)));
		CommitTransactionCommand();		/* TBLOCK_PREPARE -> PrepareTransaction() */

		me->rows = (int64) processed;
		pg_atomic_write_u32(&me->state, PCOPY_PREPARED);
	}
	PG_CATCH();
	{
		ErrorData  *edata;
		MemoryContext ecxt;

		ecxt = MemoryContextSwitchTo(TopMemoryContext);
		edata = CopyErrorData();
		me->sqlerrcode = edata->sqlerrcode;
		strlcpy(me->errmsg,
				edata->message ? edata->message : "unknown error",
				sizeof(me->errmsg));
		MemoryContextSwitchTo(ecxt);
		FlushErrorState();
		AbortOutOfAnyTransaction();
		if (pcopy_src.fd >= 0)
		{
			CloseTransientFile(pcopy_src.fd);
			pcopy_src.fd = -1;
		}
		pg_atomic_write_u32(&me->state, PCOPY_FAILED);
	}
	PG_END_TRY();

	dsm_detach(seg);
	proc_exit(0);
}

/*
 * pcopy_fingerprint_file
 *		SHA-256 of a file's bytes, for the opt-in load dedup (#403 item 7).
 *
 *		The whole file rather than its path, size or mtime, because all three of
 *		those call a changed file the same file, and being wrong in that
 *		direction discards rows the caller meant to load.
 *
 *		The coordinator runs this while the loaders are already reading the same
 *		file, so it overlaps their work rather than adding a serial pass in front
 *		of it. Measured on 264 MiB: the load takes 1.13 s across four workers and
 *		a single-threaded SHA-256 of the same bytes takes 0.48 s, so doing it
 *		before dispatch would have cost 42%.
 *
 *		A file that cannot be read is an error, not a reason to load without
 *		checking: the caller asked for the load to be refused if it was a repeat,
 *		and silently loading it again is the answer they excluded.
 */
static void
pcopy_fingerprint_file(const char *path, uint8 *out)
{
	pg_cryptohash_ctx *ctx;
	char	   *buf;
	int			fd;
	int			nread;

	fd = OpenTransientFile(path, O_RDONLY | PG_BINARY);
	if (fd < 0)
		ereport(ERROR,
				(errcode_for_file_access(),
				 errmsg("could not open \"%s\" to fingerprint it for dedup: %m",
						path)));

	ctx = pg_cryptohash_create(PG_SHA256);
	if (ctx == NULL || pg_cryptohash_init(ctx) < 0)
	{
		CloseTransientFile(fd);
		ereport(ERROR,
				(errcode(ERRCODE_INTERNAL_ERROR),
				 errmsg("could not initialise SHA-256 for the dedup fingerprint")));
	}

	buf = palloc(PCOPY_FINGERPRINT_BUFSZ);
	while ((nread = read(fd, buf, PCOPY_FINGERPRINT_BUFSZ)) > 0)
	{
		if (pg_cryptohash_update(ctx, (const uint8 *) buf, (size_t) nread) < 0)
		{
			CloseTransientFile(fd);
			pg_cryptohash_free(ctx);
			ereport(ERROR,
					(errcode(ERRCODE_INTERNAL_ERROR),
					 errmsg("could not compute the dedup fingerprint of \"%s\"",
							path)));
		}
	}

	if (nread < 0)
	{
		int			saved = errno;

		CloseTransientFile(fd);
		pg_cryptohash_free(ctx);
		errno = saved;
		ereport(ERROR,
				(errcode_for_file_access(),
				 errmsg("could not read \"%s\" to fingerprint it for dedup: %m",
						path)));
	}
	CloseTransientFile(fd);

	if (pg_cryptohash_final(ctx, out, PG_SHA256_DIGEST_LENGTH) < 0)
	{
		pg_cryptohash_free(ctx);
		ereport(ERROR,
				(errcode(ERRCODE_INTERNAL_ERROR),
				 errmsg("could not finalise the dedup fingerprint of \"%s\"",
						path)));
	}
	pg_cryptohash_free(ctx);
	pfree(buf);
}

/*
 * pcopy_finish_prepared
 *		COMMIT PREPARED (isCommit) or ROLLBACK PREPARED a gid from this (coordinator)
 *		session. FinishPreparedTransaction is what the COMMIT/ROLLBACK PREPARED
 *		utility statements call, and the utility path runs it inside the implicit
 *		transaction command, so we wrap it the same way. May ereport (e.g. the gid
 *		no longer exists); callers that must not fail use the _quietly variant.
 */
static void
pcopy_finish_prepared(const char *gid, bool isCommit)
{
	StartTransactionCommand();
	FinishPreparedTransaction(gid, isCommit);
	CommitTransactionCommand();
}

/*
 * pcopy_rollback_prepared_quietly
 *		Best-effort ROLLBACK PREPARED that never throws, for cleanup paths already
 *		handling an error. A gid that is already gone (committed, or never prepared)
 *		just leaves the error flushed.
 */
static void
pcopy_rollback_prepared_quietly(const char *gid)
{
	PG_TRY();
	{
		pcopy_finish_prepared(gid, false);
	}
	PG_CATCH();
	{
		FlushErrorState();
		AbortOutOfAnyTransaction();
	}
	PG_END_TRY();
}

/*
 * pgcolumnar_parallel_copy_coordinator
 *		Background-worker entry for atomic mode. Attaches the DSM, connects as the
 *		calling role, launches the N loader workers, waits for them all, then either
 *		COMMIT PREPAREDs every range (all prepared) or ROLLBACK PREPAREDs the ones
 *		that prepared (any failed). The outcome + total rows are written to the
 *		header for the waiting SQL function. Runs the 2PC finish that the function
 *		cannot (transaction-control commands are illegal inside a function).
 */
/*
 * The coordinator must NOT just die() on SIGTERM: proc_exit would run past its 2PC
 * cleanup and orphan every loader's prepared transaction (pinning the cluster xmin
 * horizon). Instead the handler records the request and wakes the wait loop, which
 * rolls the prepared loaders back in normal backend context.
 */
static volatile sig_atomic_t pcopy_coord_got_sigterm = false;

static void
pcopy_coord_sigterm(SIGNAL_ARGS)
{
	int			save_errno = errno;

	pcopy_coord_got_sigterm = true;
	SetLatch(MyLatch);
	errno = save_errno;
}

PGDLLEXPORT void
pgcolumnar_parallel_copy_coordinator(Datum main_arg)
{
	dsm_segment *seg;
	shm_toc    *toc;
	PcopyHeader *hdr;
	PcopyWorkerSlot *slots;
	int			nworkers;
	BackgroundWorker bw;
	BackgroundWorkerHandle **handles;
	int			i;
	bool		fingerprinted = false;

	pqsignal(SIGTERM, pcopy_coord_sigterm);
	BackgroundWorkerUnblockSignals();

	seg = dsm_attach(DatumGetUInt32(main_arg));
	if (seg == NULL)
		ereport(ERROR,
				(errcode(ERRCODE_INTERNAL_ERROR),
				 errmsg("pgcolumnar parallel_copy coordinator could not attach to the shared segment")));
	toc = shm_toc_attach(PCOPY_MAGIC, dsm_segment_address(seg));
	if (toc == NULL)
		ereport(ERROR,
				(errcode(ERRCODE_INTERNAL_ERROR),
				 errmsg("pgcolumnar parallel_copy coordinator found a bad shared segment")));
	hdr = (PcopyHeader *) shm_toc_lookup(toc, PCOPY_KEY_HEADER, false);
	slots = (PcopyWorkerSlot *) shm_toc_lookup(toc, PCOPY_KEY_WORKERS, false);
	nworkers = hdr->nworkers;

	columnar_worker_connect_as(hdr->dbid, hdr->authid, hdr->roleid,
							   hdr->roleIsSuperuser);

	/*
	 * Single-table load: pre-create and COMMIT the storage catalog row before any
	 * loader starts, in the coordinator's own top-level session (the SQL function
	 * cannot commit). With the row committed, each loader -- which sets
	 * pgcolumnar_bulk_parallel_writer -- sees it and skips the storage-row creation
	 * lock, so N loaders write the one storage concurrently and 2PC-safely. The
	 * coordinator itself leaves the flag off, so this uses the normal create path.
	 */
	if (hdr->single_table)
	{
		Relation	rel;

		StartTransactionCommand();
		/*
		 * StartTransactionCommand does not push an active snapshot, but
		 * PgColumnarEnsureStorageRow reads pgcolumnar.options/storage via
		 * systable scans, and those visibility checks require a registered or
		 * active snapshot. A normal backend has one from the executor; this
		 * bgworker does not, so push one explicitly. Without it the scan runs
		 * on an unregistered GetTransactionSnapshot() and aborts an assert
		 * build the moment the options relation has a matching row (i.e. when
		 * the target has custom options set).
		 */
		PushActiveSnapshot(GetTransactionSnapshot());
		rel = table_open(hdr->relid, RowExclusiveLock);
		PgColumnarEnsureStorageRow(rel);
		table_close(rel, NoLock);
		PopActiveSnapshot();
		CommitTransactionCommand();
	}

	handles = (BackgroundWorkerHandle **)
		palloc0(sizeof(BackgroundWorkerHandle *) * nworkers);

	/* loader worker template (all share the one DSM segment) */
	memset(&bw, 0, sizeof(bw));
	bw.bgw_flags = BGWORKER_SHMEM_ACCESS | BGWORKER_BACKEND_DATABASE_CONNECTION;
	bw.bgw_start_time = BgWorkerStart_RecoveryFinished;
	bw.bgw_restart_time = BGW_NEVER_RESTART;
	strlcpy(bw.bgw_library_name, "pgcolumnar", BGW_MAXLEN);
	strlcpy(bw.bgw_function_name, "pgcolumnar_parallel_copy_worker", BGW_MAXLEN);
	snprintf(bw.bgw_name, BGW_MAXLEN, "pgcolumnar parallel_copy loader");
	snprintf(bw.bgw_type, BGW_MAXLEN, "pgcolumnar parallel_copy loader");
	bw.bgw_main_arg = main_arg;
	bw.bgw_notify_pid = MyProcPid;

	PG_TRY();
	{
		bool		all_prepared = true;
		int			failed = -1;
		int64		total = 0;

		for (i = 0; i < nworkers; i++)
		{
			memcpy(bw.bgw_extra, &i, sizeof(int));
			if (!RegisterDynamicBackgroundWorker(&bw, &handles[i]))
				ereport(ERROR,
						(errcode(ERRCODE_INSUFFICIENT_RESOURCES),
						 errmsg("could not register pgcolumnar parallel_copy loader %d of %d",
								i + 1, nworkers),
						 errhint("Increase max_worker_processes; atomic parallel_copy needs one coordinator plus %d loader slots.",
								 nworkers)));
		}

		/*
		 * Wait for every loader to finish (PREPARE or FAIL), staying responsive to
		 * cancellation. WaitForBackgroundWorkerShutdown would not return on cancel
		 * with our non-die SIGTERM handler (the loader is still running), so poll the
		 * handles on the latch and break out when asked to stop.
		 */
		for (;;)
		{
			int			running = 0;

			for (i = 0; i < nworkers; i++)
			{
				pid_t		pid;

				if (handles[i] != NULL &&
					GetBackgroundWorkerPid(handles[i], &pid) != BGWH_STOPPED)
					running++;
			}
			if (running == 0 || pcopy_coord_got_sigterm)
				break;

			/*
			 * Fingerprint the file once, here rather than before dispatch, so
			 * the pass overlaps the loaders instead of delaying them (#403
			 * item 7). The decision it feeds is taken below, after every loader
			 * has PREPAREd and before anything commits.
			 *
			 * INSIDE A TRANSACTION, which is not optional. On a build with
			 * --with-openssl, pg_cryptohash_create registers the context with
			 * CurrentResourceOwner, and outside a transaction that pointer is
			 * NULL: the coordinator segfaults. A build without OpenSSL uses the
			 * in-core SHA-2, which touches no resource owner and does not care,
			 * which is why this passed on one build and crashed on another.
			 */
			if (hdr->dedup && !fingerprinted)
			{
				StartTransactionCommand();
				pcopy_fingerprint_file(hdr->filename, hdr->fingerprint);
				CommitTransactionCommand();
				fingerprinted = true;
			}
			(void) WaitLatch(MyLatch, WL_LATCH_SET | WL_TIMEOUT | WL_EXIT_ON_PM_DEATH,
							 1000L, PG_WAIT_EXTENSION);
			ResetLatch(MyLatch);
		}

		if (pcopy_coord_got_sigterm)
		{
			/*
			 * Cancelled: stop every loader, wait for it to exit, then roll back
			 * every range that prepared -- all in normal backend context, so no
			 * prepared transaction is orphaned in-doubt.
			 */
			for (i = 0; i < nworkers; i++)
				if (handles[i] != NULL)
					TerminateBackgroundWorker(handles[i]);
			for (i = 0; i < nworkers; i++)
				if (handles[i] != NULL)
					WaitForBackgroundWorkerShutdown(handles[i]);
			for (i = 0; i < nworkers; i++)
				if (pg_atomic_read_u32(&slots[i].state) == PCOPY_PREPARED)
					pcopy_rollback_prepared_quietly(slots[i].gid);
			strlcpy(hdr->coord_errmsg, "pgcolumnar.parallel_copy was cancelled",
					sizeof(hdr->coord_errmsg));
			pg_atomic_write_u32(&hdr->coord_state, PCOPY_COORD_FAILED);
		}
		else
		{
		for (i = 0; i < nworkers; i++)
		{
			if (pg_atomic_read_u32(&slots[i].state) != PCOPY_PREPARED)
			{
				all_prepared = false;
				if (failed < 0)
					failed = i;
			}
		}

		if (all_prepared)
		{
			bool		duplicate = false;

			/*
			 * The load is done and prepared; nothing is visible yet. This is the
			 * only point at which a repeat can be refused for free, because 2PC
			 * lets the whole thing be thrown away after the work rather than
			 * before it (#403 item 7). A duplicate therefore costs a full load
			 * that is discarded, which is the right trade: the duplicate is the
			 * exceptional case, and checking before the load would mean either a
			 * serial hash pass in front of every load or a fingerprint that
			 * depends on the worker count.
			 *
			 * NOT serialized against a CONCURRENT identical load. Two loads of
			 * the same file running at once both check before either records,
			 * so both commit -- which is the behaviour without dedup, and is the
			 * safe direction. What this refuses is the case it was built for: a
			 * committed load whose acknowledgement the client never saw, retried
			 * afterwards. Serializing the concurrent case needs a lock held
			 * across the check, the COMMIT PREPAREDs and the record, and
			 * COMMIT PREPARED cannot run inside a transaction block.
			 */
			if (hdr->dedup)
			{
				StartTransactionCommand();
				PushActiveSnapshot(GetTransactionSnapshot());
				/* inside the transaction, for the resource-owner reason above */
				if (!fingerprinted)
				{
					pcopy_fingerprint_file(hdr->filename, hdr->fingerprint);
					fingerprinted = true;
				}
				duplicate = PgColumnarLoadFingerprintSeen(hdr->relid,
														  hdr->fingerprint,
														  PG_SHA256_DIGEST_LENGTH,
														  GetActiveSnapshot());
				PopActiveSnapshot();
				CommitTransactionCommand();
			}

			if (duplicate)
			{
				/* refuse it: roll every range back, so nothing becomes visible */
				for (i = 0; i < nworkers; i++)
					pcopy_rollback_prepared_quietly(slots[i].gid);
				hdr->total_rows = 0;
				hdr->skipped_duplicate = true;
				pg_atomic_write_u32(&hdr->coord_state, PCOPY_COORD_DONE);
			}
			else
			{
			/*
			 * Decision: commit. Committing N prepared transactions is not itself
			 * one atomic step -- a coordinator crash mid-loop leaves the standard
			 * 2PC in-doubt state (some committed, the rest prepared and listed in
			 * pg_prepared_xacts for resolution). COMMIT PREPARED does not fail
			 * transiently, so short of a crash this loop completes.
			 */
			for (i = 0; i < nworkers; i++)
			{
				pcopy_finish_prepared(slots[i].gid, true);
				total += slots[i].rows;
			}
			hdr->total_rows = total;

			/*
			 * Recorded AFTER the data commits, never before. A crash between the
			 * two leaves data with no fingerprint, so a retry loads again, which
			 * is the behaviour without this feature and is the safe direction.
			 * The reverse order would leave a fingerprint with no data and refuse
			 * rows that were never stored.
			 */
			if (hdr->dedup)
			{
				StartTransactionCommand();
				PushActiveSnapshot(GetTransactionSnapshot());
				PgColumnarRecordLoadFingerprint(hdr->relid, hdr->fingerprint,
												PG_SHA256_DIGEST_LENGTH, total);
				PopActiveSnapshot();
				CommitTransactionCommand();
			}

			pg_atomic_write_u32(&hdr->coord_state, PCOPY_COORD_DONE);
			}
		}
		else
		{
			/* any failure: roll back every range that prepared -> no partial load */
			for (i = 0; i < nworkers; i++)
				if (pg_atomic_read_u32(&slots[i].state) == PCOPY_PREPARED)
					pcopy_rollback_prepared_quietly(slots[i].gid);

			hdr->failed_worker = failed;
			hdr->coord_sqlerrcode = slots[failed].sqlerrcode;
			strlcpy(hdr->coord_errmsg,
					slots[failed].errmsg[0] ? slots[failed].errmsg
					: "loader exited without reporting a result",
					sizeof(hdr->coord_errmsg));
			pg_atomic_write_u32(&hdr->coord_state, PCOPY_COORD_FAILED);
		}
		}						/* end: not cancelled */
	}
	PG_CATCH();
	{
		ErrorData  *edata;
		MemoryContext ecxt;

		ecxt = MemoryContextSwitchTo(TopMemoryContext);
		edata = CopyErrorData();
		hdr->coord_sqlerrcode = edata->sqlerrcode;
		strlcpy(hdr->coord_errmsg,
				edata->message ? edata->message : "coordinator error",
				sizeof(hdr->coord_errmsg));
		MemoryContextSwitchTo(ecxt);
		FlushErrorState();
		AbortOutOfAnyTransaction();

		/*
		 * Stop every launched loader, then WAIT for each to actually exit before
		 * reading its slot: a loader makes its transaction durable
		 * (CommitTransactionCommand) just before storing PCOPY_PREPARED, so a slot
		 * scanned mid-flight can read PENDING while a prepared transaction already
		 * exists. Without the wait, an error after some loaders prepared (e.g.
		 * RegisterDynamicBackgroundWorker exhausts max_worker_processes) would
		 * orphan their prepared transactions in-doubt. SIGTERM'd loaders finish the
		 * slot store (no interrupt point between the durable prepare and the store);
		 * only an uncatchable SIGKILL in that window leaves an in-doubt xact, which
		 * is then the documented pg_prepared_xacts / gid-prefix recovery case.
		 */
		for (i = 0; i < nworkers; i++)
			if (handles[i] != NULL)
				TerminateBackgroundWorker(handles[i]);
		for (i = 0; i < nworkers; i++)
			if (handles[i] != NULL)
				WaitForBackgroundWorkerShutdown(handles[i]);
		for (i = 0; i < nworkers; i++)
			if (pg_atomic_read_u32(&slots[i].state) == PCOPY_PREPARED)
				pcopy_rollback_prepared_quietly(slots[i].gid);

		pg_atomic_write_u32(&hdr->coord_state, PCOPY_COORD_FAILED);
	}
	PG_END_TRY();

	dsm_detach(seg);
	proc_exit(0);
}

PG_FUNCTION_INFO_V1(pgcolumnar_parallel_copy);

/*
 * pgcolumnar_parallel_copy(target regclass, filename text, workers int)
 *		-> rows loaded.
 *
 * Atomic bulk load: launches the coordinator bgworker (which spawns the loaders,
 * 2-phase-commits them all or rolls them all back) and returns its total.
 */
Datum
pgcolumnar_parallel_copy(PG_FUNCTION_ARGS)
{
	Oid			relid;
	char	   *path;
	int			workers;
	int			max_prepared;
	bool		dedup;
	bool		single_table = false;
	int64	   *offs;
	shm_toc_estimator est;
	Size		segsize;
	dsm_segment *seg;
	shm_toc    *toc;
	PcopyHeader *hdr;
	PcopyWorkerSlot *slots;
	dsm_handle	dsmh;
	BackgroundWorker bw;
	BackgroundWorkerHandle *coord_handle = NULL;
	uint32		cstate;
	int			i;

	if (PG_ARGISNULL(0) || PG_ARGISNULL(1))
		ereport(ERROR,
				(errcode(ERRCODE_NULL_VALUE_NOT_ALLOWED),
				 errmsg("target and filename must not be null")));

	relid = PG_GETARG_OID(0);
	path = text_to_cstring(PG_GETARG_TEXT_PP(1));
	workers = PG_ARGISNULL(2) ? pcopy_auto_workers() : PG_GETARG_INT32(2);
	dedup = PG_ARGISNULL(3) ? false : PG_GETARG_BOOL(3);

	if (!has_privs_of_role(GetUserId(), ROLE_PG_READ_SERVER_FILES))
		ereport(ERROR,
				(errcode(ERRCODE_INSUFFICIENT_PRIVILEGE),
				 errmsg("must be superuser or a member of the pg_read_server_files role to run pgcolumnar.parallel_copy")));

	if (workers < 1)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("workers must be at least 1")));
	if (workers > PCOPY_MAX_WORKERS)
		workers = PCOPY_MAX_WORKERS;

	/*
	 * The target is one of two shapes that pgcolumnar can load in parallel AND
	 * atomically:
	 *   - a RANGE-partitioned table: each worker loads a distinct partition
	 *     (distinct storage id), so there is nothing to serialize on. Compute
	 *     partition-aligned byte ranges (this may lower `workers` to the partition
	 *     count), which requires the file sorted by the partition key.
	 *   - a single columnar table: the loaders write the one storage concurrently
	 *     via pgcolumnar_bulk_parallel_writer (see below), so a naive record-aligned
	 *     byte split is enough and the file needs no ordering.
	 * Any other target (e.g. a heap, or a partitioned table with non-columnar
	 * partitions) is rejected. A naive split of one non-partitioned columnar table
	 * WITHOUT that opt-in would serialize on the per-storage write lock and, under
	 * 2PC, deadlock -- which is why the single-table path pre-creates the storage
	 * row and the loaders skip that lock.
	 */
	{
		Relation	target = table_open(relid, AccessShareLock);

		/*
		 * The loaders run as this role and INSERT into the target, so the caller
		 * must hold INSERT on it -- exactly what core COPY FROM checks. The loaders
		 * do not run the executor permission check themselves, so enforce it here,
		 * up front, before anything is spawned.
		 */
		{
			AclResult	aclresult = pg_class_aclcheck(relid, GetUserId(), ACL_INSERT);

			if (aclresult != ACLCHECK_OK)
			{
				char		nm[NAMEDATALEN];

				strlcpy(nm, RelationGetRelationName(target), NAMEDATALEN);
				table_close(target, AccessShareLock);
				aclcheck_error(aclresult, OBJECT_TABLE, nm);
			}

		/* RLS after the ACL check, matching core's ordering (#563). */
		PgColumnarRequireNoRowSecurity(relid);

		}

		if (target->rd_rel->relkind == RELKIND_PARTITIONED_TABLE)
		{
			/* each worker loads a DISTINCT partition (distinct storage) */
			offs = pcopy_partition_aligned_offsets(target, path, &workers);
			single_table = false;
		}
		else if (PgColumnarIsColumnarRelation(relid))
		{
			/*
			 * A single columnar table: workers write the ONE storage concurrently
			 * (distinct stripe/row-number reservations; the coordinator pre-creates
			 * the storage row and the loaders skip its creation lock via
			 * pgcolumnar_bulk_parallel_writer). Any record-aligned byte split is
			 * correct -- no partition key, so no sorted-input requirement.
			 */
			offs = pcopy_naive_offsets(path, workers);
			single_table = true;
		}
		else
		{
			char		nm[NAMEDATALEN];

			strlcpy(nm, RelationGetRelationName(target), NAMEDATALEN);
			table_close(target, AccessShareLock);
			ereport(ERROR,
					(errcode(ERRCODE_WRONG_OBJECT_TYPE),
					 errmsg("\"%s\" is not a pgcolumnar table or a partitioned table",
							nm)));
		}
		table_close(target, AccessShareLock);
	}

	/*
	 * Atomic mode prepares one transaction per (effective) worker, so it needs at
	 * least that many prepared-transaction slots. Check after the split (which may
	 * have lowered the worker count) for a clear error rather than failing partway.
	 * Necessary, not sufficient: slots shared with other sessions can still run out
	 * at PREPARE time, which surfaces as a clean rollback with the loader's error.
	 */
	{
		const char *s = GetConfigOption("max_prepared_transactions", true, false);

		max_prepared = (s != NULL) ? atoi(s) : 0;
	}
	if (max_prepared < workers)
		ereport(ERROR,
				(errcode(ERRCODE_OBJECT_NOT_IN_PREREQUISITE_STATE),
				 errmsg("pgcolumnar.parallel_copy requires max_prepared_transactions >= %d, but it is %d",
						workers, max_prepared),
				 errhint("Atomic parallel_copy prepares one transaction per worker; raise max_prepared_transactions (requires a restart) or reduce workers.")));

	/* lay out one DSM segment: control header + per-worker slot array */
	shm_toc_initialize_estimator(&est);
	shm_toc_estimate_chunk(&est, sizeof(PcopyHeader));
	shm_toc_estimate_chunk(&est, mul_size(sizeof(PcopyWorkerSlot), workers));
	shm_toc_estimate_keys(&est, 2);
	segsize = shm_toc_estimate(&est);

	seg = dsm_create(segsize, 0);
	toc = shm_toc_create(PCOPY_MAGIC, dsm_segment_address(seg), segsize);
	hdr = (PcopyHeader *) shm_toc_allocate(toc, sizeof(PcopyHeader));
	shm_toc_insert(toc, PCOPY_KEY_HEADER, hdr);
	slots = (PcopyWorkerSlot *) shm_toc_allocate(toc,
												 mul_size(sizeof(PcopyWorkerSlot), workers));
	shm_toc_insert(toc, PCOPY_KEY_WORKERS, slots);
	dsmh = dsm_segment_handle(seg);

	hdr->dbid = MyDatabaseId;
	GetUserIdAndSecContext(&hdr->roleid, &hdr->secContext);
	hdr->authid = GetAuthenticatedUserId();
	hdr->roleIsSuperuser = superuser_arg(hdr->roleid);
	hdr->relid = relid;
	hdr->nworkers = workers;
	hdr->single_table = single_table;
	strlcpy(hdr->filename, path, sizeof(hdr->filename));
	hdr->total_rows = 0;
	hdr->failed_worker = -1;
	hdr->coord_sqlerrcode = 0;
	hdr->coord_errmsg[0] = '\0';
	hdr->dedup = dedup;
	hdr->skipped_duplicate = false;
	memset(hdr->fingerprint, 0, sizeof(hdr->fingerprint));
	pg_atomic_init_u32(&hdr->coord_state, PCOPY_COORD_PENDING);

	for (i = 0; i < workers; i++)
	{
		slots[i].start_off = offs[i];
		slots[i].end_off = offs[i + 1];
		slots[i].rows = 0;
		slots[i].sqlerrcode = 0;
		slots[i].errmsg[0] = '\0';
		pg_atomic_init_u32(&slots[i].state, PCOPY_PENDING);
		/* gid unique among concurrently-prepared xacts: our pid + segment + index */
		snprintf(slots[i].gid, GIDSIZE, "pgcolumnar/%d/%u/%d",
				 (int) MyProcPid, (unsigned) dsmh, i);
	}

	/* one coordinator bgworker owns the loaders and the 2PC finish */
	memset(&bw, 0, sizeof(bw));
	bw.bgw_flags = BGWORKER_SHMEM_ACCESS | BGWORKER_BACKEND_DATABASE_CONNECTION;
	bw.bgw_start_time = BgWorkerStart_RecoveryFinished;
	bw.bgw_restart_time = BGW_NEVER_RESTART;
	strlcpy(bw.bgw_library_name, "pgcolumnar", BGW_MAXLEN);
	strlcpy(bw.bgw_function_name, "pgcolumnar_parallel_copy_coordinator", BGW_MAXLEN);
	snprintf(bw.bgw_name, BGW_MAXLEN, "pgcolumnar parallel_copy coordinator");
	snprintf(bw.bgw_type, BGW_MAXLEN, "pgcolumnar parallel_copy coordinator");
	bw.bgw_main_arg = UInt32GetDatum(dsmh);
	bw.bgw_notify_pid = MyProcPid;

	/*
	 * Launch the coordinator and wait. On cancellation, terminate it; the
	 * coordinator rolls back or (if it had already decided to commit) leaves the
	 * standard in-doubt prepared transactions for resolution.
	 */
	PG_TRY();
	{
		if (!RegisterDynamicBackgroundWorker(&bw, &coord_handle))
			ereport(ERROR,
					(errcode(ERRCODE_INSUFFICIENT_RESOURCES),
					 errmsg("could not register pgcolumnar parallel_copy coordinator"),
					 errhint("Increase max_worker_processes.")));
		WaitForBackgroundWorkerShutdown(coord_handle);
	}
	PG_CATCH();
	{
		if (coord_handle != NULL)
			TerminateBackgroundWorker(coord_handle);
		dsm_detach(seg);
		PG_RE_THROW();
	}
	PG_END_TRY();

	cstate = pg_atomic_read_u32(&hdr->coord_state);
	if (cstate == PCOPY_COORD_DONE)
	{
		int64		total = hdr->total_rows;
		bool		skipped = hdr->skipped_duplicate;

		dsm_detach(seg);

		/*
		 * Say what happened rather than returning a silent 0. Discarding rows a
		 * caller asked to insert is not ordinary INSERT behaviour, so a caller
		 * that gets 0 back is owed the reason (#403 item 7).
		 */
		if (skipped)
			ereport(NOTICE,
					(errmsg("pgcolumnar.parallel_copy skipped a load already applied to \"%s\"",
							get_rel_name(relid)),
					 errdetail("The file has the same contents as a load this table already took."),
					 errhint("Pass dedup => false to load it again.")));

		PG_RETURN_INT64(total);
	}
	else
	{
		char		msg[512];
		int			code = hdr->coord_sqlerrcode;
		int			fw = hdr->failed_worker;

		strlcpy(msg, hdr->coord_errmsg[0] ? hdr->coord_errmsg
				: "coordinator exited without reporting a result", sizeof(msg));
		dsm_detach(seg);
		if (fw >= 0)
			ereport(ERROR,
					(errcode(code ? code : ERRCODE_INTERNAL_ERROR),
					 errmsg("pgcolumnar.parallel_copy worker %d failed: %s", fw, msg)));
		else
			ereport(ERROR,
					(errcode(code ? code : ERRCODE_INTERNAL_ERROR),
					 errmsg("pgcolumnar.parallel_copy failed: %s", msg)));
	}

	PG_RETURN_INT64(0);			/* unreachable */
}
