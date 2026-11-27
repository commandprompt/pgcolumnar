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
 * is escape-free); CSV and other key types are later phases (see the plan). The
 * file is read in the caller's client_encoding, as COPY FROM reads it, and the byte
 * scans step over whole characters where that encoding lets a character's second
 * byte be a backslash (#2188, PcopyScan).
 *
 * Independent MIT implementation. References only the public PostgreSQL API
 * (server-file access, the COPY BeginCopyFrom interface, the background-worker and
 * two-phase-commit APIs, and the role check used by COPY FROM file), plus one field
 * of the installed commands/copyfrom_internal.h: a loader sets the COPY state's
 * eol_type, so its range keeps the file's newline style (#2189). No
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
#include "columnar_objstore.h"

#include "columnar_write_state.h"
#include "access/relation.h"
#include "access/table.h"
#include "access/twophase.h"
#include "access/xact.h"
#include "catalog/pg_authid_d.h"
#include "catalog/pg_class.h"
#include "catalog/pg_index.h"
#include "catalog/pg_inherits.h"
#include "catalog/pg_trigger.h"
#include "catalog/pg_type.h"
#include "commands/copy.h"
#include "commands/trigger.h"
#include "commands/copyfrom_internal.h"	/* EolType, CopyFromStateData.eol_type */
#include "fmgr.h"
#include "funcapi.h"
#include "libpq/pqsignal.h"
#include "mb/pg_wchar.h"
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
#include "storage/lock.h"
#include "storage/lmgr.h"
#include "storage/shm_toc.h"
#include "storage/shmem.h"
#include "utils/syscache.h"
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
 * PcopyScan
 *		Where a forward byte scan of a COPY text file stands: the state that every
 *		scan here shares (pcopy_line_offsets, pcopy_file_eol_type, and the
 *		partition splitter's record reader), so they all read backslashes and
 *		line ends alike (#2188).
 *
 *		A backslash escapes the character after it, whatever it is, so a newline
 *		after an odd run of backslashes is data (#2084). Which bytes are characters
 *		depends on the file's encoding, the caller's client_encoding, which the
 *		loaders read the file in too. COPY FROM converts the file to the database
 *		encoding before it looks for a backslash or a line end, unless the file is
 *		already in that encoding or one of the two is SQL_ASCII (BeginCopyFrom's
 *		need_transcoding, the same on PG15 to 19). Every database encoding is
 *		ASCII-safe: a byte below 0x80 is always a whole character. The client-only
 *		encodings (SJIS, SHIFT_JIS_2004, BIG5, GBK, GB18030, UHC, JOHAB) are not:
 *		the second byte of a character can be below 0x80, so in SJIS 83 5c is one
 *		character (U+30BD) and not a byte and a backslash. For those, when the
 *		database is not SQL_ASCII, the scan steps over each character's trailing
 *		bytes with pg_encoding_mblen, as core's COPY TO does (encoding_embeds_ascii,
 *		PG_ENCODING_IS_CLIENT_ONLY). A client-only encoding is never the database
 *		encoding, so the file is always converted then. In a SQL_ASCII database
 *		core reads the bytes unconverted, every 0x5c a backslash, and so does this.
 *
 *		The length comes from the lead byte alone. GB18030's four-byte characters
 *		are the one case where pg_encoding_mblen looks at the second byte (0x30 to
 *		0x39 there), and a four-byte character read as two two-byte ones covers the
 *		same bytes, with both its ASCII-range bytes inside a character, so no byte
 *		below 0x80 is classed differently. Invalid input (a lead byte with no valid
 *		trailing byte) is refused by core COPY in the loader whose range holds it;
 *		the scan only has to keep every cut on a byte that ends a record.
 */
typedef struct PcopyScan
{
	int			encoding;		/* the file's encoding: the caller's client_encoding */
	bool		embeds_ascii;	/* a trailing byte can be below 0x80, and core converts */
	int			mb_left;		/* trailing bytes of the current character still to pass */
	bool		escaped;		/* the previous character is a backslash that no
								 * backslash escapes */
} PcopyScan;

static void
pcopy_scan_init(PcopyScan *scan, int encoding)
{
	scan->encoding = encoding;
	scan->embeds_ascii = PG_ENCODING_IS_CLIENT_ONLY(encoding) &&
		GetDatabaseEncoding() != PG_SQL_ASCII;
	scan->mb_left = 0;
	scan->escaped = false;
}

/*
 * pcopy_char_trail
 *		How many bytes follow c in its character: 0 for a byte below 0x80, and for
 *		every byte when the scan need not step over characters.
 */
static int
pcopy_char_trail(const PcopyScan *scan, char c)
{
	char		lead[2];

	if (!scan->embeds_ascii || !IS_HIGHBIT_SET(c))
		return 0;
	lead[0] = c;
	lead[1] = '\0';
	return pg_encoding_mblen(scan->encoding, lead) - 1;
}

/*
 * pcopy_scan_byte
 *		Feed the next byte of the file to the scan. Returns the byte when it is a
 *		whole character that no backslash escapes and is not itself a backslash,
 *		and '\0' otherwise: a trailing byte of a character, an escaped character
 *		(a multibyte one escaped as a whole), or a backslash that escapes the next.
 */
static char
pcopy_scan_byte(PcopyScan *scan, char c)
{
	if (scan->mb_left > 0)
	{
		scan->mb_left--;
		return '\0';
	}
	if (scan->embeds_ascii && IS_HIGHBIT_SET(c))
	{
		/* the lead byte of a character: data, escaped or not */
		scan->mb_left = pcopy_char_trail(scan, c);
		scan->escaped = false;
		return '\0';
	}
	if (scan->escaped)
	{
		scan->escaped = false;
		return '\0';
	}
	if (c == '\\')
	{
		scan->escaped = true;
		return '\0';
	}
	return c;
}

/*
 * pcopy_line_offsets
 *		Compute nranges+1 record-aligned byte offsets partitioning an open file of
 *		`size` bytes into that many ranges, in ONE forward pass (O(filesize),
 *		interruptible), rather than a separate seek+scan per boundary. Each
 *		interior boundary is the first byte after the first record-ending newline
 *		at or beyond the even split point size*i/nranges; ranges may be empty when
 *		the file has fewer records than ranges. Returns a palloc'd int64[nranges+1].
 *
 *		A newline ends a record only when an even number of backslashes comes
 *		right before it. In COPY text format a backslash followed by a raw newline
 *		is data inside the field (#2084), so a cut there would split one record in
 *		two. The scan starts at byte 0, so the backslash-run parity is exact, and
 *		it carries across chunk reads, as does a character that a chunk read ends
 *		inside. `encoding` is the file's (PcopyScan, #2188).
 */
static int64 *
pcopy_line_offsets(int fd, off_t size, int nranges, const char *path, int encoding)
{
	int64	   *offs = (int64 *) palloc(sizeof(int64) * (nranges + 1));
	char	   *buf = (char *) palloc(COLUMNAR_SPLIT_SCAN_CHUNK);
	int64		bufbase = 0;		/* absolute file offset of buf[0] */
	int			next = 1;
	PcopyScan	scan;

	pcopy_scan_init(&scan, encoding);
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
			/* an escaped newline, or a byte inside a character, is data */
			if (pcopy_scan_byte(&scan, buf[j]) == '\n')
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
 * pcopy_file_eol_type
 *		The newline style of the file's first line, decided as core COPY FROM
 *		decides it (#2189): by the first newline or carriage return that no
 *		backslash escapes. "\r\n" is EOL_CRNL, a "\r" followed by anything else
 *		EOL_CR, and "\n" EOL_NL. EOL_UNKNOWN when the file has no such byte.
 *
 *		COPY FROM takes the style from the first line and refuses every later
 *		line ending, and an end-of-data marker line, whose style differs (22P04).
 *		Each loader runs core COPY over its own range, so a loader whose range
 *		does not start the file took the style from its own first line, and a
 *		change of style at its range start loaded. The loaders now start core
 *		COPY with this style instead, and core refuses such a file with its own
 *		SQLSTATE and message. A backslash escapes the character after it whatever
 *		it is, as in core's CopyReadLineText and in pcopy_line_offsets, and the
 *		file is read as characters of `encoding` (PcopyScan, #2188).
 */
static int
pcopy_file_eol_type(const char *path, int encoding)
{
	char	   *buf = (char *) palloc(COLUMNAR_SPLIT_SCAN_CHUNK);
	int			fd = pcopy_open_regular_file(path, NULL);
	int			eol = EOL_UNKNOWN;
	PcopyScan	scan;
	bool		after_cr = false;	/* the previous byte was an unescaped \r */

	pcopy_scan_init(&scan, encoding);

	while (eol == EOL_UNKNOWN)
	{
		int			got = (int) read(fd, buf, COLUMNAR_SPLIT_SCAN_CHUNK);
		int			j;

		if (got < 0)
			ereport(ERROR,
					(errcode_for_file_access(),
					 errmsg("could not read file \"%s\": %m", path)));
		if (got == 0)
		{
			/* a \r that is the file's last byte ends its line, as EOL_CR */
			if (after_cr)
				eol = EOL_CR;
			break;
		}
		for (j = 0; j < got && eol == EOL_UNKNOWN; j++)
		{
			if (after_cr)
				eol = (buf[j] == '\n') ? EOL_CRNL : EOL_CR;
			else
			{
				char		c = pcopy_scan_byte(&scan, buf[j]);

				if (c == '\n')
					eol = EOL_NL;
				else if (c == '\r')
					after_cr = true;
			}
		}
		CHECK_FOR_INTERRUPTS();
	}
	CloseTransientFile(fd);
	pfree(buf);
	return eol;
}

/*
 * pcopy_has_unique_or_exclusion_index
 *		Does rel have a unique or exclusion index?
 *
 *		parallel_copy cannot load such a single table with more than one worker
 *		(#2080). Each loader keeps the locks its inserts took -- the unique-key
 *		advisory locks, and the xid a sibling's btree or exclusion check waits
 *		on -- in its prepared transaction. The coordinator commits only after
 *		every loader has finished, so a loader that waits on a sibling's
 *		prepared transaction either waits until cancelled or fails with a
 *		deadlock error. Distinct keys do not help: the advisory locks are hash
 *		buckets.
 *
 *		The calling backend checks before it starts any worker, and each loader
 *		checks again once it holds ROW EXCLUSIVE on the table: the first check
 *		runs under ACCESS SHARE, which CREATE INDEX does not wait for.
 */
static bool
pcopy_has_unique_or_exclusion_index(Relation rel)
{
	List	   *indexes = RelationGetIndexList(rel);
	ListCell   *lc;
	bool		found = false;

	foreach(lc, indexes)
	{
		HeapTuple	tup = SearchSysCache1(INDEXRELID,
										  ObjectIdGetDatum(lfirst_oid(lc)));
		Form_pg_index idx;

		if (!HeapTupleIsValid(tup))
			continue;
		idx = (Form_pg_index) GETSTRUCT(tup);
		found = idx->indisunique || idx->indisexclusion;
		ReleaseSysCache(tup);
		if (found)
			break;
	}
	list_free(indexes);
	return found;
}

/*
 * pcopy_insert_trigger
 *		The name of a user-defined trigger that fires on INSERT into rel in
 *		this session, or NULL when it has none.
 *
 *		parallel_copy cannot load such a single table with more than one worker
 *		either (#2187). Each loader runs the trigger, and a trigger can write to
 *		another table. Whatever locks those writes take, each loader keeps in
 *		its prepared transaction until every loader has finished, so the
 *		loaders can wait on each other just as they do over a unique index. Measured on PG 18 before this check: a BEFORE or
 *		AFTER row trigger that inserts into a columnar table with a primary key
 *		fails the load with 40P01; one that inserts into a heap table with a
 *		primary key does the same when the loaders' keys overlap; an AFTER
 *		STATEMENT trigger that inserts into a columnar table hangs until
 *		cancelled. Nothing tells a trigger that writes from one that does not,
 *		so every timing and level counts. Internal triggers do not: a foreign
 *		key's take shared row locks, and 2 workers still load such a table.
 */
static const char *
pcopy_insert_trigger(Relation rel)
{
	TriggerDesc *td = rel->trigdesc;
	int			i;

	if (td == NULL)
		return NULL;
	for (i = 0; i < td->numtriggers; i++)
	{
		Trigger    *t = &td->triggers[i];

		bool		fires;

		/*
		 * Whether it fires in this session, by core's own rule: a REPLICA
		 * trigger only under session_replication_role = replica, an ORIGIN one
		 * only outside it, ALWAYS in both, DISABLED never (#2219 review). The
		 * calling backend and each loader judge by their own setting.
		 */
		if (SessionReplicationRole == SESSION_REPLICATION_ROLE_REPLICA)
			fires = t->tgenabled == TRIGGER_FIRES_ON_REPLICA ||
				t->tgenabled == TRIGGER_FIRES_ALWAYS;
		else
			fires = t->tgenabled == TRIGGER_FIRES_ON_ORIGIN ||
				t->tgenabled == TRIGGER_FIRES_ALWAYS;
		if (!t->tgisinternal && fires && TRIGGER_FOR_INSERT(t->tgtype))
			return t->tgname;
	}
	return NULL;
}

/*
 * pcopy_refuse_more_workers
 *		Raise the 2+-worker refusal for a single table with a unique or exclusion
 *		index (#2080) or an INSERT trigger (#2187), if it has either. The calling
 *		backend runs it before it starts any worker, and each loader runs it again
 *		once it holds ROW EXCLUSIVE on the table. `close_lockmode` is the lock to
 *		close rel with before raising, or NoLock to leave it open.
 */
static void
pcopy_refuse_more_workers(Relation rel, LOCKMODE close_lockmode)
{
	char		nm[NAMEDATALEN];
	const char *tg;
	char		tgname[NAMEDATALEN];

	strlcpy(nm, RelationGetRelationName(rel), NAMEDATALEN);
	if (pcopy_has_unique_or_exclusion_index(rel))
	{
		if (close_lockmode != NoLock)
			table_close(rel, close_lockmode);
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("pgcolumnar.parallel_copy cannot load \"%s\" with more than one worker, because it has a unique or exclusion index",
						nm),
				 errhint("Load it with workers => 1, or with COPY.")));
	}
	tg = pcopy_insert_trigger(rel);
	if (tg != NULL)
	{
		strlcpy(tgname, tg, NAMEDATALEN);
		if (close_lockmode != NoLock)
			table_close(rel, close_lockmode);
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("pgcolumnar.parallel_copy cannot load \"%s\" with more than one worker, because it has trigger \"%s\" on INSERT",
						nm, tgname),
				 errdetail("Each worker runs the trigger in its own prepared transaction, so writes the trigger makes can wait on another worker until the load is cancelled."),
				 errhint("Load it with workers => 1, or with COPY.")));
	}
}

/*
 * pcopy_naive_offsets
 *		Line-aligned byte offsets partitioning `path` into `workers` even ranges,
 *		for the single-table load where every worker writes the one storage and any
 *		record-aligned split is correct (no partition key, so no ordering
 *		requirement). `encoding` is the file's (#2188). Returns a palloc'd
 *		int64[workers+1].
 */
static int64 *
pcopy_naive_offsets(const char *path, int workers, int encoding)
{
	int			fd;
	off_t		size;
	int64	   *offs;

	fd = pcopy_open_regular_file(path, &size);
	offs = pcopy_line_offsets(fd, size, workers, path, encoding);
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
 * the first record-ending newline that follows the even split point
 * filesize*i/workers. This is a record boundary only for COPY *text* format,
 * where a raw newline ends a record unless an odd run of backslashes comes right
 * before it: a backslash followed by a raw newline is data (#2084). It is NOT
 * safe for CSV, whose quoted fields may contain literal newlines -- quote-aware
 * splitting is a later phase; callers holding a CSV must not use these
 * offsets. The file is read in the session's client_encoding, as COPY FROM
 * reads it (#2188). Ranges may be empty (equal consecutive offsets) when the file has
 * fewer records than workers -- that worker then loads nothing, which is
 * harmless. `workers` is capped at PCOPY_MAX_WORKERS.
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

	/* a local file only (#1886): refuse a URL rather than fail it as ENOENT */
	PgColumnarRefuseRemotePath("file_split_offsets", path);

	/* read the file as COPY FROM in this session would: in its client_encoding */
	fd = pcopy_open_regular_file(path, &size);
	offs = pcopy_line_offsets(fd, size, workers, path, pg_get_client_encoding());
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
	bool		ended_at_marker;	/* COPY stopped at a \. line before the range
									 * end, so later ranges are not data (#2084) */
	int			sqlerrcode;
	char		errmsg[512];
	char		errdetail[512];	/* the loader's DETAIL and HINT, empty if none, */
	char		errhint[256];	/* passed on to the caller (#2187) */
	char		gid[GIDSIZE];	/* 2PC gid the coordinator assigned this loader */
	pg_atomic_uint32 armed;		/* 1 once the loader holds its open lock (#2187) */
	pg_atomic_uint32 running;	/* 1 once the loader holds its run lock (#2229) */
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
	int			eol_type;		/* EolType of the file's first line (#2189) */
	int			file_encoding;	/* the caller's client_encoding, which the loaders
								 * read the file in, as COPY FROM does (#2188) */
	/* coordinator -> function result channel */
	pg_atomic_uint32 coord_state;	/* PcopyCoordState */
	int64		total_rows;		/* valid when coord_state == PCOPY_COORD_DONE */
	int			failed_worker;	/* index of the first failed loader, or -1 */
	int			coord_sqlerrcode;
	char		coord_errmsg[512];
	char		coord_errdetail[512];
	char		coord_errhint[256];

	/*
	 * names this load's open locks (#2187), see "Opening the target together",
	 * and its run locks (#2229), see "The caller waits in the lock manager"
	 */
	uint32		open_lock_key;
	pg_atomic_uint32 coord_running;	/* 1 once the coordinator holds its run
									 * lock (#2229) */

	/*
	 * Set once every COMMIT PREPARED is done (#2219, third review). No prepared
	 * transaction is left from then on, so a cancelled caller stops waiting
	 * for the coordinator at that point; the coordinator goes on to record the
	 * dedup fingerprint with no deadline, as before #2187.
	 */
	pg_atomic_uint32 committed;

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
 *
 * eof_seen records that COPY asked for more and got none. COPY asks again only
 * when it has parsed everything it was given, so a COPY that finishes without
 * eof_seen stopped at an end-of-data marker (a \. line) inside the range (#2084).
 */
typedef struct PcopyRangeSource
{
	int			fd;
	int64		remaining;
	const char *path;
	bool		eof_seen;
} PcopyRangeSource;

static PcopyRangeSource pcopy_src = {-1, 0, NULL, false};

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
	if (total == 0)
		pcopy_src.eof_seen = true;
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
 * The file is read as characters of `encoding`, the caller's client_encoding
 * (PcopyScan, #2188).
 *
 * v1 restrictions (all checked): single-column RANGE partitioning, no DEFAULT
 * partition, non-expression key, COPY text format with default (whole-table,
 * table-order) column list, and a key field free of COPY escapes (true for the
 * numeric/temporal keys people range-partition on).
 */
static int64 *
pcopy_partition_aligned_offsets(Relation parent, const char *path, int *workers_io,
								int encoding)
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
	StringInfoData rec;			/* a record that spans physical lines (#2084) */
	PcopyScan	scan;			/* where the record reader stands (#2188) */
	int64		rec_start = 0;
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
	initStringInfo(&rec);
	pcopy_scan_init(&scan, encoding);
	PG_TRY();
	{
		for (;;)
		{
			int64		line_start = off;
			char	   *p;
			int			f = 0;
			int			flen;
			char	   *fld;
			Datum		kd;
			int			bucket;
			bool		at_eof = false;

			len = getline(&line, &cap, fp);
			if (len == -1)
			{
				/* a file that ends inside a record: that record is the last row */
				if (rec.len == 0)
					break;
				at_eof = true;
			}
			else
			{
				char		last = '\0';
				ssize_t		e;

				off += len;

				/*
				 * COPY text syntax (#2084): a newline after an odd run of
				 * backslashes is data, so the record goes on in the next line.
				 * Read forward with the shared scan, which carries from line to
				 * line, so a backslash that is the second byte of a character
				 * escapes nothing (#2188).
				 */
				for (e = 0; e < len; e++)
					last = pcopy_scan_byte(&scan, line[e]);
				if (len > 0 && line[len - 1] == '\n' && last != '\n')
				{
					if (rec.len == 0)
						rec_start = line_start;
					appendBinaryStringInfo(&rec, line, (int) len);
					continue;
				}
				if (rec.len > 0)
					appendBinaryStringInfo(&rec, line, (int) len);
				else if (len >= 2 && line[0] == '\\' && line[1] == '.' &&
						 (len == 2 || line[2] == '\n' || line[2] == '\r'))
				{
					/*
					 * The end-of-data marker. COPY reads nothing after it, so
					 * the load ends here: the last range keeps the marker line,
					 * and core COPY in that loader stops at it.
					 */
					size = off;
					break;
				}
			}
			if (rec.len > 0)
			{
				p = rec.data;
				line_start = rec_start;
			}
			else
				p = line;

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
			resetStringInfo(&rec);

			if ((++nrows & 0xFFFF) == 0)
				CHECK_FOR_INTERRUPTS();
			if (at_eof)
				break;
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
	pfree(rec.data);

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

int			pgcolumnar_parallel_copy_open_hold_ms = 0;	/* dev/test (#2187) */

/*
 * Opening the target together (#2187).
 *
 * A loader keeps its ROW EXCLUSIVE lock on the target in its prepared
 * transaction, so a loader that prepares holds it until the coordinator
 * finishes the load, which waits for every loader. A lock request that
 * conflicts with ROW EXCLUSIVE and arrives between the loaders' requests --
 * CREATE INDEX's SHARE, say -- queues behind the loaders that have the lock,
 * and the loaders that do not queue behind it. The lock manager sees nothing
 * wrong: CREATE INDEX waits on a prepared transaction, which waits on nothing
 * it can see. Before this, the load hung until cancelled (issue #2187: 8
 * workers, CREATE UNIQUE INDEX 61 ms in).
 *
 * So no loader goes past opening the target until every sibling has the
 * target open, and it waits for each one in the lock manager. Loader i takes
 * an exclusive lock of class PGCOLUMNAR_LOCKCLASS_PCOPY_OPEN on (load, i)
 * before it requests ROW EXCLUSIVE, and releases it once it has the lock. A
 * sibling that has the target open takes the same lock in SHARE mode, so it
 * waits until loader i has the target too. The wait-for graph is now whole:
 * sibling -> loader i -> CREATE INDEX -> sibling. The deadlock detector finds
 * that loader i's wait behind CREATE INDEX is only queue order, since
 * ROW EXCLUSIVE does not conflict with the sibling's ROW EXCLUSIVE, and moves
 * loader i ahead of CREATE INDEX in the queue. The load then finishes and
 * CREATE INDEX runs after it, as it would after any other writer.
 *
 * The armed flag says loader i holds its open lock. A sibling that took the
 * lock before loader i did would not wait at all, so it first waits, outside
 * the lock manager, for the flag. Loader i sets it before it requests anything
 * on the target, so that wait cannot be part of a lock cycle. A loader that
 * fails sets its state to FAILED, and for one that exits without reporting the
 * coordinator moves that loader's slot from PENDING to FAILED, so nobody waits
 * for the flag forever -- and the other siblings are still waited for (#2219
 * review: one flag for the whole load stopped the wait for every sibling).
 */
static void
pcopy_open_locktag(LOCKTAG *tag, const PcopyHeader *hdr, int widx)
{
	SET_LOCKTAG_ADVISORY(*tag, MyDatabaseId, hdr->open_lock_key, (uint32) widx,
						 PGCOLUMNAR_LOCKCLASS_PCOPY_OPEN);
}

/*
 * pcopy_hold_before_open
 *		pgcolumnar.parallel_copy_open_hold_ms, for loaders after the first: a
 *		test sets it to queue a lock request between the first loader's lock on
 *		the target and the others' (#2187). Interruptible.
 */
static void
pcopy_hold_before_open(int widx)
{
	long		remaining = (long) pgcolumnar_parallel_copy_open_hold_ms;

	if (widx == 0)
		return;
	while (remaining > 0)
	{
		long		step = Min(remaining, 100L);

		CHECK_FOR_INTERRUPTS();
		(void) WaitLatch(MyLatch, WL_LATCH_SET | WL_TIMEOUT | WL_EXIT_ON_PM_DEATH,
						 step, PG_WAIT_EXTENSION);
		ResetLatch(MyLatch);
		remaining -= step;
	}
}

/*
 * pcopy_wait_for_siblings_to_open
 *		Return once every sibling loader has the target open, has failed, or is
 *		gone. See "Opening the target together" above.
 */
static void
pcopy_wait_for_siblings_to_open(PcopyHeader *hdr, PcopyWorkerSlot *slots, int widx)
{
	int			j;

	for (j = 0; j < hdr->nworkers; j++)
	{
		LOCKTAG		tag;

		if (j == widx)
			continue;
		while (pg_atomic_read_u32(&slots[j].armed) == 0 &&
			   pg_atomic_read_u32(&slots[j].state) == PCOPY_PENDING)
		{
			CHECK_FOR_INTERRUPTS();
			(void) WaitLatch(MyLatch, WL_LATCH_SET | WL_TIMEOUT | WL_EXIT_ON_PM_DEATH,
							 10L, PG_WAIT_EXTENSION);
			ResetLatch(MyLatch);
		}
		pcopy_open_locktag(&tag, hdr, j);
		(void) LockAcquire(&tag, ShareLock, false, false);
		LockRelease(&tag, ShareLock, false);
	}
}

/*
 * The caller waits in the lock manager (#2229).
 *
 * The calling backend takes the workers' ROW EXCLUSIVE on the target and on
 * each partition before it starts the coordinator, so a conflicting lock of
 * another session -- LOCK TABLE ... IN SHARE MODE, CREATE INDEX -- is waited
 * for there, in the lock manager, where the deadlock detector and the caller's
 * lock_timeout see it. Before, the coordinator or a loader waited for it in its
 * own transaction while the caller waited for them on a latch. If the other
 * session then waited for a lock the caller held, nothing saw the cycle, and
 * both waited until statement_timeout.
 *
 * Holding that lock has a cost of its own: a SHARE request that arrives after
 * it waits for the caller, and a worker that asks for its ROW EXCLUSIVE later
 * queues behind that request (the lock manager queues a request behind a
 * waiting one it conflicts with). Measured on a partitioned target, a plain
 * CREATE INDEX on a partition the loaders had not reached yet hung the load,
 * where main's code just ran the index first.
 *
 * So the caller does not wait for its workers on a latch either. The
 * coordinator, and each loader, takes an exclusive lock of class
 * PGCOLUMNAR_LOCKCLASS_PCOPY_RUN on (load, its index) as soon as it has
 * connected, before it asks for any lock on a relation, and says so in shared
 * memory. The calling backend waits for each of those in SHARE mode in turn:
 * the coordinator until it has created a single table's storage row, each
 * loader until it exits. The wait-for graph then has an edge from the caller
 * to the worker it is waiting for. A worker queued behind a SHARE request that
 * waits for the caller closes a cycle with one soft edge, and the deadlock
 * detector moves the worker ahead of the request, since its ROW EXCLUSIVE does
 * not conflict with the caller's. A worker that waits for a lock the caller
 * holds closes a cycle of hard edges, and one side gets 40P01 -- which is also
 * what a lock the caller holds on a table the loaders reach through a foreign
 * key, a trigger or a sequence now gives, instead of a hang (#2216).
 *
 * The run lock is a session lock, so a loader's PREPARE TRANSACTION does not
 * carry it into the prepared transaction, and it goes with the process if the
 * process dies. The caller's waits on it are waits for the load, not lock
 * contention, so lock_timeout does not apply to them and log_lock_waits does
 * not report them. A wait for a worker that has not taken its lock yet is a
 * latch wait, but a worker takes it before it asks for anything a lock cycle
 * could hold up.
 */
#define PCOPY_RUN_COORDINATOR	PCOPY_MAX_WORKERS	/* the coordinator's run lock index */

static void
pcopy_run_locktag(LOCKTAG *tag, const PcopyHeader *hdr, int idx)
{
	SET_LOCKTAG_ADVISORY(*tag, hdr->dbid, hdr->open_lock_key, (uint32) idx,
						 PGCOLUMNAR_LOCKCLASS_PCOPY_RUN);
}

/* a worker takes its run lock, then says so; see above */
static void
pcopy_take_run_lock(const PcopyHeader *hdr, int idx, pg_atomic_uint32 *flag)
{
	LOCKTAG		tag;

	pcopy_run_locktag(&tag, hdr, idx);
	(void) LockAcquire(&tag, ExclusiveLock, true, false);
	pg_atomic_write_u32(flag, 1);
}

static void
pcopy_release_run_lock(const PcopyHeader *hdr, int idx)
{
	LOCKTAG		tag;

	pcopy_run_locktag(&tag, hdr, idx);
	LockRelease(&tag, ExclusiveLock, true);
}

/*
 * pcopy_caller_wait_run_lock
 *		The calling backend: wait until the worker behind *flag has taken its run
 *		lock, or will not, then wait in the lock manager until it releases it.
 *		*state is the worker's PENDING state, NULL for the coordinator.
 */
static void
pcopy_caller_wait_run_lock(PcopyHeader *hdr, int idx, pg_atomic_uint32 *flag,
						   pg_atomic_uint32 *state,
						   BackgroundWorkerHandle *coord_handle)
{
	LOCKTAG		tag;

	while (pg_atomic_read_u32(flag) == 0)
	{
		pid_t		cpid;

		if (state != NULL && pg_atomic_read_u32(state) != PCOPY_PENDING)
			return;
		if (pg_atomic_read_u32(&hdr->coord_state) != PCOPY_COORD_PENDING ||
			GetBackgroundWorkerPid(coord_handle, &cpid) == BGWH_STOPPED)
			return;
		(void) WaitLatch(MyLatch, WL_LATCH_SET | WL_TIMEOUT | WL_EXIT_ON_PM_DEATH,
						 10L, PG_WAIT_EXTENSION);
		ResetLatch(MyLatch);
		CHECK_FOR_INTERRUPTS();
	}
	pcopy_run_locktag(&tag, hdr, idx);
	(void) LockAcquire(&tag, ShareLock, false, false);
	LockRelease(&tag, ShareLock, false);
}

/*
 * pcopy_caller_wait_for_workers
 *		The calling backend: wait for the coordinator's and then each loader's run
 *		lock (see above). Returns once every loader has exited or never started;
 *		the caller then waits for the coordinator to finish.
 */
static void
pcopy_caller_wait_for_workers(PcopyHeader *hdr, PcopyWorkerSlot *slots,
							  BackgroundWorkerHandle *coord_handle)
{
	int			nest = NewGUCNestLevel();
	int			i;

	(void) set_config_option("lock_timeout", "0", PGC_SUSET, PGC_S_SESSION,
							 GUC_ACTION_SAVE, true, 0, false);
	(void) set_config_option("log_lock_waits", "off", PGC_SUSET, PGC_S_SESSION,
							 GUC_ACTION_SAVE, true, 0, false);
	pcopy_caller_wait_run_lock(hdr, PCOPY_RUN_COORDINATOR, &hdr->coord_running,
							   NULL, coord_handle);
	for (i = 0; i < hdr->nworkers; i++)
		pcopy_caller_wait_run_lock(hdr, i, &slots[i].running, &slots[i].state,
								   coord_handle);
	AtEOXact_GUC(true, nest);
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
	/* before any lock on a relation (#2229), see "The caller waits..." */
	pcopy_take_run_lock(hdr, widx, &me->running);

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
		bool		ended_at_marker;
		int			fd;
		LOCKTAG		open_tag;

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
		pcopy_src.eof_seen = false;

		/* open the target together with the siblings (#2187), see above */
		pcopy_hold_before_open(widx);
		if (hdr->nworkers > 1)
		{
			pcopy_open_locktag(&open_tag, hdr, widx);
			(void) LockAcquire(&open_tag, ExclusiveLock, false, false);
			pg_atomic_write_u32(&me->armed, 1);
		}
		rel = table_open(hdr->relid, RowExclusiveLock);
		if (hdr->nworkers > 1)
			LockRelease(&open_tag, ExclusiveLock, false);

		/*
		 * The calling backend refused a unique or exclusion index, or an INSERT
		 * trigger, under ACCESS SHARE, which neither CREATE INDEX nor CREATE
		 * TRIGGER waits for, so one may have appeared since (#2080, #2187).
		 * ROW EXCLUSIVE makes both wait until this load ends, so what is read
		 * now holds for the whole load. One that queues after some loaders have
		 * their lock waits for the load to finish: no loader reads it, and the
		 * open lock above lets the loaders behind it go first.
		 */
		if (hdr->single_table && hdr->nworkers > 1)
			pcopy_refuse_more_workers(rel, NoLock);
		if (hdr->nworkers > 1)
			pcopy_wait_for_siblings_to_open(hdr, slots, widx);
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

		/*
		 * Read the file in the caller's client_encoding, as the caller's COPY
		 * FROM would (#2188). A loader's own client_encoding is the database's
		 * or the role's default, which is not the caller's, so the loaders
		 * converted the same bytes to other text, or refused them.
		 */
		options = list_make2(makeDefElem("format",
										 (Node *) makeString("text"), -1),
							 makeDefElem("encoding",
										 (Node *) makeString(pstrdup(pg_encoding_to_char(hdr->file_encoding))),
										 -1));
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

		/*
		 * A range that does not start the file continues it, so core COPY must
		 * hold its lines to the newline style of the file's first line, as one
		 * COPY FROM of the whole file does (#2189). Left to itself it would take
		 * the style from the range's first line, and load a file that changes
		 * style, or has a \.\r\n line in an LF file, at the range start.
		 */
		if (me->start_off > 0 && hdr->eol_type != EOL_UNKNOWN)
			cstate->eol_type = (EolType) hdr->eol_type;
		processed = CopyFrom(cstate);
		ended_at_marker = !pcopy_src.eof_seen;
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
		me->ended_at_marker = ended_at_marker;
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
		strlcpy(me->errdetail, edata->detail ? edata->detail : "",
				sizeof(me->errdetail));
		strlcpy(me->errhint, edata->hint ? edata->hint : "",
				sizeof(me->errhint));
		MemoryContextSwitchTo(ecxt);
		FlushErrorState();

		/*
		 * Close the file before the abort, not after it (#2083). The abort's
		 * AtEOXact_Files closes every transient file this transaction opened, so
		 * a close after it named a descriptor fd.c no longer tracks: it logged
		 * "fd passed to CloseTransientFile was not obtained from
		 * OpenTransientFile" and called close() on a number that could by then
		 * belong to another file.
		 */
		if (pcopy_src.fd >= 0)
		{
			CloseTransientFile(pcopy_src.fd);
			pcopy_src.fd = -1;
		}
		AbortOutOfAnyTransaction();
		pg_atomic_write_u32(&me->state, PCOPY_FAILED);
	}
	PG_END_TRY();

	pcopy_release_run_lock(hdr, widx);
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
 *
 * It also raises a query cancel (#2219 review). The caller now waits for this
 * process to exit after it terminates it, so every wait here must end on SIGTERM,
 * a lock wait included: with dedup => true the coordinator reads and writes
 * pgcolumnar.load_fingerprint, and a lock the caller holds there kept both
 * waiting until a second cancel. The cancel turns such a wait into an ERROR,
 * whose PG_CATCH below rolls the prepared loaders back. The cleanup paths, and
 * everything from the first COMMIT PREPARED to the fingerprint record, hold
 * interrupts, so the cancel cannot cut them short. The caller stops waiting
 * once the commit is done (committed in the header), so a lock wait in the
 * record does not hold it up.
 */
static volatile sig_atomic_t pcopy_coord_got_sigterm = false;

static void
pcopy_coord_sigterm(SIGNAL_ARGS)
{
	int			save_errno = errno;

	pcopy_coord_got_sigterm = true;
	InterruptPending = true;
	QueryCancelPending = true;
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
	/* held while it locks the target, below (#2229) */
	pcopy_take_run_lock(hdr, PCOPY_RUN_COORDINATOR, &hdr->coord_running);

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
		int			ncommit = nworkers;	/* ranges that hold data */

		/*
		 * Inside the PG_TRY (#2229, review of #2244): this step waits for a
		 * lock on the target's storage, and the deadlock detector can pick the
		 * coordinator as the victim of a cycle through it. Before, its error
		 * ended the process before anything was recorded, and the caller
		 * reported XX000 "coordinator exited without reporting a result"
		 * instead of the 40P01. The PG_CATCH below records it for the caller.
		 *
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
		/* the caller waits for the loaders' run locks from here (#2229) */
		pcopy_release_run_lock(hdr, PCOPY_RUN_COORDINATOR);

		for (i = 0; i < nworkers && !pcopy_coord_got_sigterm; i++)
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

				if (handles[i] == NULL)
					continue;
				if (GetBackgroundWorkerPid(handles[i], &pid) != BGWH_STOPPED)
					running++;
				else
				{
					/*
					 * Stopped without reporting: mark that slot FAILED, so its
					 * siblings stop waiting for it to open the table (see
					 * pcopy_wait_for_siblings_to_open). It has exited, so it
					 * can no longer store a state of its own.
					 */
					uint32		expected = PCOPY_PENDING;

					(void) pg_atomic_compare_exchange_u32(&slots[i].state, &expected,
														  PCOPY_FAILED);
				}
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
			 * prepared transaction is orphaned in-doubt. Interrupts stay held
			 * from here to exit: the pending cancel must not cut this short.
			 */
			HOLD_INTERRUPTS();
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
		/*
		 * The ranges are in file order. A range whose COPY stopped at an
		 * end-of-data marker is the last one with data: COPY FROM reads nothing
		 * after the marker, so the later ranges are rolled back and their rows,
		 * or their errors, do not count (#2084).
		 */
		for (i = 0; i < nworkers; i++)
		{
			if (pg_atomic_read_u32(&slots[i].state) != PCOPY_PREPARED)
			{
				all_prepared = false;
				failed = i;
				break;
			}
			if (slots[i].ended_at_marker)
			{
				ncommit = i + 1;
				break;
			}
		}

		if (all_prepared)
		{
			bool		duplicate = false;

			for (i = ncommit; i < nworkers; i++)
				if (pg_atomic_read_u32(&slots[i].state) == PCOPY_PREPARED)
					pcopy_rollback_prepared_quietly(slots[i].gid);

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
				for (i = 0; i < ncommit; i++)
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
			/*
			 * From the first COMMIT PREPARED to the fingerprint record, a cancel
			 * is too late to take back (#2219): interrupts stay held, so a
			 * cancel stops neither the loop halfway nor the record. A record
			 * cut short by a cancel would let a dedup retry load the file a
			 * second time, the one thing dedup promises not to do.
			 *
			 * The record may then wait on a lock without limit, as it did
			 * before #2187, and it cannot be cancelled. That cannot hold up a
			 * cancelled caller: once the loop is done this sets committed, and
			 * the caller stops waiting there, with no prepared transaction
			 * left. A lock the caller itself holds is released when its
			 * transaction ends, and the record follows.
			 */
			HOLD_INTERRUPTS();
			for (i = 0; i < ncommit; i++)
			{
				pcopy_finish_prepared(slots[i].gid, true);
				total += slots[i].rows;
			}
			hdr->total_rows = total;
			pg_memory_barrier();
			pg_atomic_write_u32(&hdr->committed, 1);

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
			RESUME_INTERRUPTS();

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
			strlcpy(hdr->coord_errdetail, slots[failed].errdetail,
					sizeof(hdr->coord_errdetail));
			strlcpy(hdr->coord_errhint, slots[failed].errhint,
					sizeof(hdr->coord_errhint));
			pg_atomic_write_u32(&hdr->coord_state, PCOPY_COORD_FAILED);
		}
		}						/* end: not cancelled */
	}
	PG_CATCH();
	{
		ErrorData  *edata;
		MemoryContext ecxt;

		/* held to exit, so a pending cancel cannot cut the cleanup short */
		HOLD_INTERRUPTS();
		ecxt = MemoryContextSwitchTo(TopMemoryContext);
		edata = CopyErrorData();
		hdr->coord_sqlerrcode = edata->sqlerrcode;
		strlcpy(hdr->coord_errmsg,
				edata->message ? edata->message : "coordinator error",
				sizeof(hdr->coord_errmsg));
		strlcpy(hdr->coord_errdetail, edata->detail ? edata->detail : "",
				sizeof(hdr->coord_errdetail));
		strlcpy(hdr->coord_errhint, edata->hint ? edata->hint : "",
				sizeof(hdr->coord_errhint));
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
	List	   *lockrels = NIL;	/* the target and its partitions (#2229) */
	int64	   *offs;
	int			eol_type;
	int			file_encoding = pg_get_client_encoding();
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

		/*
		 * A local file only (#1886): refuse a URL after the INSERT and RLS
		 * checks, as import_arrow does, so a caller who may not write the target
		 * gets 42501 for a URL as for a local path. Before both the partitioned
		 * and the single-table branch, so both are covered, and before a heap
		 * target is refused.
		 */
		PgColumnarRefuseRemotePath("parallel_copy", path);

		/*
		 * The coordinator and the loaders open the target with ROW EXCLUSIVE, and
		 * the loaders route rows into its partitions under the same mode, each in
		 * its own transaction (#2183). Refuse a lock of ours that would make them
		 * wait, before anything is read or spawned.
		 *
		 * After the INSERT, RLS and URL checks, so a caller who may not write the
		 * target gets 42501 whatever it holds. Before the unique-index refusal
		 * below (#2080): that one's hint is "workers => 1", and a caller holding
		 * such a lock would be refused again with one worker, so the refusal
		 * whose cause holds for every worker count comes first.
		 */
		{
			List	   *rels = find_all_inheritors(relid, NoLock, NULL);
			ListCell   *lc;

			foreach(lc, rels)
				PgColumnarRefuseOwnLockConflict("pgcolumnar.parallel_copy",
												lfirst_oid(lc), RowExclusiveLock);

			/*
			 * Nor may this transaction have written to the target or to one of
			 * its partitions (#2211). The write took the storage's storage-row
			 * lock (PgColumnarInsertNativeStorageRow), held to transaction end.
			 * The coordinator takes it to pre-create a single table's storage
			 * row, and a loader takes it on its first flush into a partition,
			 * each in its own transaction, so they waited on it while this
			 * backend waited on them on a latch, until statement_timeout. A
			 * refusal, like #2183's: the workers could not see the caller's
			 * uncommitted rows or storage row anyway. Checked for every columnar
			 * partition, because which ones the file reaches is not known yet.
			 */
			foreach(lc, rels)
			{
				Oid			partid = lfirst_oid(lc);
				Relation	part;
				uint64		storageId;

				if (!PgColumnarIsColumnarRelation(partid))
					continue;
				part = table_open(partid, AccessShareLock);
				storageId = PgColumnarStorageId(part);
				table_close(part, AccessShareLock);
				if (PgColumnarStorageRowLockHeldByMe(storageId))
					ereport(ERROR,
							(errcode(ERRCODE_OBJECT_NOT_IN_PREREQUISITE_STATE),
							 errmsg("pgcolumnar.parallel_copy cannot run after this transaction has written to \"%s\"",
									get_rel_name(partid)),
							 errdetail("The write holds the lock that serializes the first writers of \"%s\" until this transaction ends, and the background workers take that lock in their own transactions, so they would wait for this transaction to end.",
									   get_rel_name(partid)),
							 errhint("Commit the write first, or load the file with COPY in this transaction.")));
			}

			lockrels = rels;
		}

		if (target->rd_rel->relkind == RELKIND_PARTITIONED_TABLE)
		{
			/* each worker loads a DISTINCT partition (distinct storage) */
			offs = pcopy_partition_aligned_offsets(target, path, &workers,
													   file_encoding);
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
			 *
			 * Not with a unique or exclusion index (#2080), or a trigger on INSERT
			 * (#2187), and more than one worker: the loaders would wait on each
			 * other's prepared transactions until cancelled, or fail with a
			 * deadlock error. Each loader checks again under its ROW EXCLUSIVE
			 * lock. A partitioned target is not affected by the index, because
			 * each worker loads distinct partitions, and so distinct indexes.
			 */
			if (workers > 1)
				pcopy_refuse_more_workers(target, AccessShareLock);
			offs = pcopy_naive_offsets(path, workers, file_encoding);
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

	/* the newline style every range is held to, read before any worker starts */
	eol_type = pcopy_file_eol_type(path, file_encoding);

	/*
	 * Take the workers' lock first (#2229): ROW EXCLUSIVE on the target and on
	 * each partition, the list the refusals above walked, held to the end of
	 * the transaction as a writer's is. A conflicting lock of another session
	 * is then waited for here, in the lock manager, where the deadlock detector
	 * and this backend's lock_timeout see it, and not by the coordinator or a
	 * loader in a transaction of its own; see "The caller waits in the lock
	 * manager". After every refusal, so that none of them waits for another
	 * session first, and before anything is created or started.
	 */
	{
		ListCell   *lc;

		foreach(lc, lockrels)
			LockRelationOid(lfirst_oid(lc), RowExclusiveLock);
	}

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
	hdr->open_lock_key = (uint32) dsmh;

	hdr->dbid = MyDatabaseId;
	GetUserIdAndSecContext(&hdr->roleid, &hdr->secContext);
	hdr->authid = GetAuthenticatedUserId();
	hdr->roleIsSuperuser = superuser_arg(hdr->roleid);
	hdr->relid = relid;
	hdr->nworkers = workers;
	hdr->single_table = single_table;
	strlcpy(hdr->filename, path, sizeof(hdr->filename));
	hdr->eol_type = eol_type;
	hdr->file_encoding = file_encoding;
	hdr->total_rows = 0;
	hdr->failed_worker = -1;
	hdr->coord_sqlerrcode = 0;
	hdr->coord_errmsg[0] = '\0';
	hdr->coord_errdetail[0] = '\0';
	hdr->coord_errhint[0] = '\0';
	hdr->dedup = dedup;
	hdr->skipped_duplicate = false;
	memset(hdr->fingerprint, 0, sizeof(hdr->fingerprint));
	pg_atomic_init_u32(&hdr->coord_state, PCOPY_COORD_PENDING);
	pg_atomic_init_u32(&hdr->committed, 0);
	pg_atomic_init_u32(&hdr->coord_running, 0);

	for (i = 0; i < workers; i++)
	{
		slots[i].start_off = offs[i];
		slots[i].end_off = offs[i + 1];
		slots[i].rows = 0;
		slots[i].ended_at_marker = false;
		slots[i].sqlerrcode = 0;
		slots[i].errmsg[0] = '\0';
		slots[i].errdetail[0] = '\0';
		slots[i].errhint[0] = '\0';
		pg_atomic_init_u32(&slots[i].state, PCOPY_PENDING);
		pg_atomic_init_u32(&slots[i].armed, 0);
		pg_atomic_init_u32(&slots[i].running, 0);
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
	 *
	 * And wait for it to exit before raising the error (#2187). Returning at
	 * once left the loaders' prepared transactions in pg_prepared_xacts while
	 * the coordinator rolled them back: measured on PG 18, the caller's own
	 * next statement saw 1 after a statement_timeout. The wait is bounded by
	 * the coordinator's cleanup, not by any lock: its SIGTERM raises a query
	 * cancel, so a lock wait there -- on the target before it starts the
	 * loaders, on pgcolumnar.load_fingerprint in the dedup check -- ends in an
	 * ERROR and the same rollback (#2219 review). Once it has committed, the
	 * caller stops waiting (see PG_CATCH). A second cancel also ends the wait.
	 */
	PG_TRY();
	{
		if (!RegisterDynamicBackgroundWorker(&bw, &coord_handle))
			ereport(ERROR,
					(errcode(ERRCODE_INSUFFICIENT_RESOURCES),
					 errmsg("could not register pgcolumnar parallel_copy coordinator"),
					 errhint("Increase max_worker_processes.")));
		pcopy_caller_wait_for_workers(hdr, slots, coord_handle);
		WaitForBackgroundWorkerShutdown(coord_handle);
	}
	PG_CATCH();
	{
		/*
		 * Wait until the coordinator has exited, or until it has committed:
		 * from then on no prepared transaction is left, and what remains is
		 * the dedup record, which may wait on a lock this backend holds until
		 * its transaction ends (#2219, third review). A second cancel also
		 * ends the wait.
		 */
		if (coord_handle != NULL)
		{
			TerminateBackgroundWorker(coord_handle);
			for (;;)
			{
				pid_t		cpid;

				if (GetBackgroundWorkerPid(coord_handle, &cpid) == BGWH_STOPPED ||
					pg_atomic_read_u32(&hdr->committed) != 0)
					break;
				(void) WaitLatch(MyLatch, WL_LATCH_SET | WL_TIMEOUT | WL_EXIT_ON_PM_DEATH,
								 100L, PG_WAIT_EXTENSION);
				ResetLatch(MyLatch);
				CHECK_FOR_INTERRUPTS();
			}
		}
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
		char		detail[512];
		char		hint[256];
		int			code = hdr->coord_sqlerrcode;
		int			fw = hdr->failed_worker;

		strlcpy(msg, hdr->coord_errmsg[0] ? hdr->coord_errmsg
				: "coordinator exited without reporting a result", sizeof(msg));
		strlcpy(detail, hdr->coord_errdetail, sizeof(detail));
		strlcpy(hint, hdr->coord_errhint, sizeof(hint));
		dsm_detach(seg);
		/* the loader's DETAIL and HINT go with its message (#2187) */
		if (fw >= 0)
			ereport(ERROR,
					(errcode(code ? code : ERRCODE_INTERNAL_ERROR),
					 errmsg("pgcolumnar.parallel_copy worker %d failed: %s", fw, msg),
					 detail[0] ? errdetail_internal("%s", detail) : 0,
					 hint[0] ? errhint("%s", hint) : 0));
		else
			ereport(ERROR,
					(errcode(code ? code : ERRCODE_INTERNAL_ERROR),
					 errmsg("pgcolumnar.parallel_copy failed: %s", msg),
					 detail[0] ? errdetail_internal("%s", detail) : 0,
					 hint[0] ? errhint("%s", hint) : 0));
	}

	PG_RETURN_INT64(0);			/* unreachable */
}
