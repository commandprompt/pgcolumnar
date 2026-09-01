/*-------------------------------------------------------------------------
 *
 * pgcolumnar_parallel_export.c
 *		pgcolumnar.parallel_export_parquet: parallel Parquet export.
 *
 * N read-only background workers each write a disjoint slice of the source to
 * its own part-NNNN.parquet file under one output directory, which
 * pgcolumnar.read_parquet and the parquet FDW read back as a single relation.
 * Export is read-only, so unlike parallel_copy there is no coordinator and no
 * two-phase commit -- the SQL function is itself the dispatcher.
 *
 * Consistency: the dispatcher ExportSnapshot()s its MVCC snapshot and each worker
 * ImportSnapshot()s it (the pg_dump parallel-dump mechanism), so every worker
 * sees the identical committed image. This is the cross-transaction mechanism;
 * the parallel-query SerializeSnapshot/RestoreSnapshot pair is NOT sound here,
 * because these workers are independent backends rather than members of the
 * dispatcher's parallel group. Rows the caller has written but not committed are
 * not part of that committed image and are not exported.
 *
 * Two target kinds mirror parallel_copy:
 *	 - a partitioned table with columnar partitions: one file per leaf partition;
 *	   the dispatcher ships the oid-sorted leaf list so workers do not re-derive
 *	   it from the live catalog (which a concurrent ATTACH could desynchronise);
 *	 - a single columnar table: split by row-group index ranges; each worker
 *	   restricts its read to its slice via PgColumnarReadRestrictToGroups.
 *
 * Cleanroom: public PostgreSQL APIs and this project's own code only.
 *
 *-------------------------------------------------------------------------
 */
#include "postgres.h"

#include <dirent.h>
#include <sys/stat.h>
#include <unistd.h>

#include "columnar.h"
#include "columnar_objstore.h"
#include "columnar_sink.h"

#include "access/relation.h"
#include "access/table.h"
#include "access/xact.h"
#include "catalog/pg_authid_d.h"
#include "catalog/pg_class.h"
#include "catalog/pg_inherits.h"
#include "fmgr.h"
#include "libpq/pqsignal.h"
#include "miscadmin.h"
#include "postmaster/bgworker.h"
#include "storage/dsm.h"
#include "storage/fd.h"
#include "storage/ipc.h"
#include "storage/latch.h"
#include "storage/shm_toc.h"
#include "tcop/tcopprot.h"
#if PG_VERSION_NUM >= 160000
#include "utils/wait_event.h"	/* PG_WAIT_EXTENSION */
#else
#include "pgstat.h"
#endif
#include "utils/acl.h"
#include "utils/builtins.h"
#include "utils/guc.h"
#include "utils/lsyscache.h"
#include "utils/memutils.h"
#include "utils/rel.h"
#include "utils/snapmgr.h"

#define PEXPORT_MAGIC		0x50455850	/* 'PEXP' */
#define PEXPORT_KEY_HEADER	0
#define PEXPORT_KEY_SLOTS	1
#define PEXPORT_KEY_LEAVES	2
#define PEXPORT_MAX_WORKERS 32

typedef enum PexportState
{
	PEXPORT_PENDING = 0,		/* not finished (also: worker crashed) */
	PEXPORT_DONE,				/* slice written; rows is valid */
	PEXPORT_FAILED				/* worker caught an error; see errmsg */
} PexportState;

typedef struct PexportWorkerSlot
{
	int32		startIdx;		/* [startIdx, endIdx): row-group indices for a */
	int32		endIdx;			/*   single table, or leaf-partition indices    */
	pg_atomic_uint32 state;		/* PexportState */
	int64		rows;			/* rows written (valid when state == PEXPORT_DONE) */
	int			sqlerrcode;
	char		errmsg[512];
	char		filepath[MAXPGPATH];	/* single-table: the one output file */
} PexportWorkerSlot;

typedef struct PexportHeader
{
	Oid			dbid;
	Oid			roleid;
	Oid			relid;			/* single columnar table, or partitioned parent */
	uint64		storageId;		/* single-table storage id (0 when partitioned) */
	int			nworkers;
	int			npart;			/* leaf-partition count (0 when single-table) */
	bool		single_table;
	char		dirpath[MAXPGPATH];		/* output directory */
	char		snapname[64];	/* ExportSnapshot() name workers ImportSnapshot() */
} PexportHeader;

/* handles + output dir for the error-cleanup callback */
typedef struct PexportSpawn
{
	BackgroundWorkerHandle **handles;
	int			n;
	const char *dirpath;
	int			nkeys;			/* part-NNNN.parquet count, for remote cleanup */
} PexportSpawn;

PGDLLEXPORT void pgcolumnar_parallel_export_worker(Datum main_arg);

/* ------------------------------------------------------------------------- */

static int
pexport_oid_cmp(const void *a, const void *b)
{
	Oid			oa = *(const Oid *) a;
	Oid			ob = *(const Oid *) b;

	if (oa < ob)
		return -1;
	if (oa > ob)
		return 1;
	return 0;
}

/*
 * pexport_leaf_partitions
 *		The oid-sorted leaf partitions of `parent`, palloc'd into *out; returns
 *		the count. The dispatcher computes this once and ships the array, so the
 *		exact set is fixed for the whole export even if the catalog changes.
 */
static int
pexport_leaf_partitions(Oid parent, Oid **out)
{
	List	   *inh = find_all_inheritors(parent, AccessShareLock, NULL);
	ListCell   *lc;
	Oid		   *arr = palloc(sizeof(Oid) * Max(list_length(inh), 1));
	int			n = 0;

	foreach(lc, inh)
	{
		Oid			oid = lfirst_oid(lc);

		if (oid == parent)
			continue;
		if (get_rel_relkind(oid) == RELKIND_PARTITIONED_TABLE)
			continue;			/* intermediate partitioned table, not a leaf */
		arr[n++] = oid;
	}
	list_free(inh);
	if (n > 1)
		qsort(arr, n, sizeof(Oid), pexport_oid_cmp);
	*out = arr;
	return n;
}

/*
 * pexport_auto_workers
 *		Worker count when the caller passes none: half the admin's
 *		max_parallel_workers budget, at least one, capped at PEXPORT_MAX_WORKERS.
 */
static int
pexport_auto_workers(void)
{
	const char *s = GetConfigOption("max_parallel_workers", true, false);
	int			budget = (s != NULL) ? atoi(s) : 8;
	int			n = budget / 2;

	if (n < 1)
		n = 1;
	if (n > PEXPORT_MAX_WORKERS)
		n = PEXPORT_MAX_WORKERS;
	return n;
}

/*
 * pexport_prepare_dir
 *		Create the output directory if absent; require it empty if it exists.
 *		read_parquet and the FDW union every *.parquet in the directory, so a
 *		stale file from an earlier, larger export would be silently folded into a
 *		read-back. Only absent-then-created, or already-empty, is allowed.
 */
static void
pexport_prepare_dir(const char *dir)
{
	struct stat st;
	DIR		   *d;
	struct dirent *de;

	/*
	 * A remote prefix (#623) has no directory to create, and require-empty
	 * cannot be checked without a bucket listing, which is #619 and
	 * deliberately deferred. So the remote branch does not verify the prefix
	 * is empty: the documented rule is that a remote prefix must be new or
	 * empty, and a stale higher-numbered part from a larger prior export into
	 * the same prefix would be unioned by a later read. The one thing done
	 * here is to load the module and reject an unsupported scheme up front, so
	 * a missing module fails before any worker is spawned rather than N times.
	 */
	if (PgColumnarPathIsRemote(dir))
	{
		const PgColumnarObjStoreApi *api = PgColumnarObjStoreGet();

		if (api == NULL)
			ereport(ERROR,
					(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
					 errmsg("columnar: writing to \"%s\" requires the object-store module",
							dir),
					 errdetail("Object storage support is a separate library, "
							   "pgcolumnar_objstore, which is not installed.")));
		if (!api->handles_url(dir))
			ereport(ERROR,
					(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
					 errmsg("columnar: writing to \"%s\" is not supported", dir),
					 errhint("A remote prefix must be new or empty; "
							 "pgcolumnar cannot verify this without a bucket "
							 "listing.")));
		return;
	}

	if (stat(dir, &st) != 0)
	{
		if (errno != ENOENT)
			ereport(ERROR,
					(errcode_for_file_access(),
					 errmsg("could not stat \"%s\": %m", dir)));
		if (MakePGDirectory(dir) != 0)
			ereport(ERROR,
					(errcode_for_file_access(),
					 errmsg("could not create directory \"%s\": %m", dir)));
		return;
	}
	if (!S_ISDIR(st.st_mode))
		ereport(ERROR,
				(errcode(ERRCODE_WRONG_OBJECT_TYPE),
				 errmsg("\"%s\" exists and is not a directory", dir)));

	d = AllocateDir(dir);
	if (d == NULL)
		ereport(ERROR,
				(errcode_for_file_access(),
				 errmsg("could not open directory \"%s\": %m", dir)));
	while ((de = ReadDir(d, dir)) != NULL)
	{
		if (strcmp(de->d_name, ".") == 0 || strcmp(de->d_name, "..") == 0)
			continue;
		FreeDir(d);
		ereport(ERROR,
				(errcode(ERRCODE_OBJECT_NOT_IN_PREREQUISITE_STATE),
				 errmsg("output directory \"%s\" is not empty", dir),
				 errhint("pgcolumnar.parallel_export_parquet writes into a new or empty directory.")));
	}
	FreeDir(d);
}

/*
 * pexport_remove_outputs
 *		Best-effort removal of everything this export wrote. A failed or
 *		cancelled run must not leave part files behind: read_parquet unions
 *		whatever is present (so a partial set reads as a complete export), and the
 *		require-empty-directory guard would block a retry. The output directory was
 *		created or required empty at entry, so every *.parquet in it is ours.
 *
 *		Since #394 the workers write through the sink, so an in-flight part is
 *		part-NNNN.parquet.tmp.<pid>. A worker that ERRORs unlinks its own temp
 *		through the sink's abort, but a worker the dispatcher TERMINATES dies
 *		FATAL, which does not unwind through PG_CATCH, so its temp survives the
 *		worker. It is still ours by construction; remove it here.
 */
static void
pexport_remove_outputs(const char *dir, int nkeys)
{
	DIR		   *d;
	struct dirent *de;
	char		fp[MAXPGPATH];

	if (dir == NULL || dir[0] == '\0')
		return;

	/*
	 * Remote (#623): the dispatcher knows its own key set exactly, so no
	 * listing is needed. The keys are dir/part-0000.parquet through
	 * dir/part-(nkeys-1).parquet; delete each through the module, best-effort
	 * like the local unlink. A worker the dispatcher TERMINATES dies FATAL
	 * with its multipart upload neither completed nor aborted, and the ABI's
	 * delete_object removes a completed key, not an in-progress upload, so an
	 * orphaned upload is left to the bucket's incomplete-multipart lifecycle
	 * rule (the same residue #622 documented for a single object).
	 */
	if (PgColumnarPathIsRemote(dir))
	{
		const PgColumnarObjStoreApi *api = PgColumnarObjStoreGet();
		int			i;

		if (api == NULL)
			return;
		for (i = 0; i < nkeys; i++)
		{
			snprintf(fp, sizeof(fp), "%s/part-%04d.parquet", dir, i);
			api->delete_object(fp, NULL);
		}
		/* also drop any _SUCCESS marker (#394): a failed or cancelled run must
		 * not leave a stale completion marker from an earlier run at the prefix */
		snprintf(fp, sizeof(fp), "%s/_SUCCESS", dir);
		api->delete_object(fp, NULL);
		return;
	}

	d = AllocateDir(dir);
	if (d == NULL)
		return;
	while ((de = ReadDir(d, dir)) != NULL)
	{
		size_t		len = strlen(de->d_name);
		bool		ours = false;

		if (len >= 8 && strcmp(de->d_name + len - 8, ".parquet") == 0)
			ours = true;
		else if (strstr(de->d_name, ".parquet.tmp.") != NULL)
			ours = true;
		if (!ours)
			continue;
		snprintf(fp, sizeof(fp), "%s/%s", dir, de->d_name);
		(void) unlink(fp);
	}
	FreeDir(d);
	/* the completion marker (#394) is not a *.parquet name; drop it explicitly */
	snprintf(fp, sizeof(fp), "%s/_SUCCESS", dir);
	(void) unlink(fp);
}

/* match exactly "part-<digits>.parquet"; on success set *idx to the index */
static bool
pexport_part_index(const char *base, int *idx)
{
	const char *p = base;
	const char *digits;

	if (strncmp(p, "part-", 5) != 0)
		return false;
	p += 5;
	digits = p;
	while (*p >= '0' && *p <= '9')
		p++;
	if (p == digits || strcmp(p, ".parquet") != 0)
		return false;
	*idx = atoi(digits);
	return true;
}

/*
 * Reconcile a remote prefix before stamping the completion marker (#394, #632
 * review). The remote path allows an overwrite re-run into a used prefix
 * (#619's require-empty is deferred), so a smaller run leaves the stale
 * higher-numbered parts of a larger prior run behind: run A writes
 * part-0000..0007, run B with fewer workers overwrites part-0000..0003 and
 * leaves 0004..0007. Writing _SUCCESS on top would certify a mixed set as one
 * complete run. Now that listing exists (#619), the dispatcher deletes any
 * part-NNNN.parquet at the prefix whose index is >= this run's key count -- the
 * stale tail -- so the marker means what it says: the prefix holds exactly this
 * run's parts. The local path needs none of this: prepare_dir require-empty
 * already refuses a non-empty directory before any worker spawns.
 */
static void
pexport_remove_stale_remote_parts(const char *dir, int nkeys)
{
	const PgColumnarObjStoreApi *api = PgColumnarObjStoreGet();
	char	  **keys;
	int			n = 0;
	int			i;

	if (api == NULL || api->list_objects == NULL)
		return;
	keys = api->list_objects(dir, NULL, &n);
	for (i = 0; i < n; i++)
	{
		const char *slash = strrchr(keys[i], '/');
		const char *base = slash ? slash + 1 : keys[i];
		int			idx;

		if (pexport_part_index(base, &idx) && idx >= nkeys)
			api->delete_object(keys[i], NULL);
	}
}

/*
 * Write the _SUCCESS completion marker (#394). parallel_export_parquet writes a
 * directory or prefix of part-NNNN.parquet objects, and a bucket (or a directory)
 * carries no record of which run produced which objects, nor whether the run
 * finished. This is the Hadoop/Spark convention a consumer already recognizes: an
 * empty _SUCCESS object at the destination, written ONLY after every worker
 * completed. Its presence means "a complete run's output is here"; its absence
 * (a failed or cancelled run, whose parts the cleanup removes) means it is not.
 * The read path already skips '_'-prefixed names, so the marker is never folded
 * into a read. Written through the same sink seam as the data, so it is
 * temp-and-rename locally and a single object remotely.
 */
static void
pexport_write_success_marker(const char *dir)
{
	char		fp[MAXPGPATH];
	PqSink	   *snk;

	snprintf(fp, sizeof(fp), "%s/_SUCCESS", dir);
	snk = PgColumnarSinkOpen(fp);
	PG_TRY();
	{
		PgColumnarSinkFinish(snk);	/* empty marker */
	}
	PG_CATCH();
	{
		PgColumnarSinkAbort(snk);
		PG_RE_THROW();
	}
	PG_END_TRY();
}

/* error-cleanup: stop every worker so a dispatcher FATAL cannot leave them
 * reading after the exported snapshot is gone, and remove any partial output */
static void
pexport_cleanup(int code, Datum arg)
{
	PexportSpawn *s = (PexportSpawn *) DatumGetPointer(arg);
	int			i;

	for (i = 0; i < s->n; i++)
		if (s->handles[i] != NULL)
			TerminateBackgroundWorker(s->handles[i]);
	pexport_remove_outputs(s->dirpath, s->nkeys);
}

/*
 * pgcolumnar_parallel_export_worker
 *		Background-worker entry: attach the DSM, connect, import the dispatcher's
 *		snapshot, and write this worker's slice to its Parquet file(s). Read-only,
 *		so there is nothing to commit and no 2PC.
 */
PGDLLEXPORT void
pgcolumnar_parallel_export_worker(Datum main_arg)
{
	dsm_segment *seg;
	shm_toc    *toc;
	PexportHeader *hdr;
	PexportWorkerSlot *slots;
	Oid		   *leaves;
	PexportWorkerSlot *me;
	int			widx;
	uint32		conn_flags = BGWORKER_BYPASS_ALLOWCONN;
	Snapshot	snap;

	memcpy(&widx, MyBgworkerEntry->bgw_extra, sizeof(int));

	pqsignal(SIGTERM, die);
	BackgroundWorkerUnblockSignals();

	seg = dsm_attach(DatumGetUInt32(main_arg));
	if (seg == NULL)
		ereport(ERROR,
				(errcode(ERRCODE_INTERNAL_ERROR),
				 errmsg("pgcolumnar parallel_export worker could not attach to the shared segment")));
	toc = shm_toc_attach(PEXPORT_MAGIC, dsm_segment_address(seg));
	if (toc == NULL)
		ereport(ERROR,
				(errcode(ERRCODE_INTERNAL_ERROR),
				 errmsg("pgcolumnar parallel_export worker found a bad shared segment")));
	hdr = (PexportHeader *) shm_toc_lookup(toc, PEXPORT_KEY_HEADER, false);
	slots = (PexportWorkerSlot *) shm_toc_lookup(toc, PEXPORT_KEY_SLOTS, false);
	leaves = (Oid *) shm_toc_lookup(toc, PEXPORT_KEY_LEAVES, false);
	me = &slots[widx];

#if PG_VERSION_NUM >= 170000
	conn_flags |= BGWORKER_BYPASS_ROLELOGINCHECK;
#endif
	BackgroundWorkerInitializeConnectionByOid(hdr->dbid, hdr->roleid, conn_flags);

	/*
	 * Adopt the dispatcher's exact snapshot. ImportSnapshot requires a
	 * repeatable-read (or serializable) transaction with no snapshot taken yet,
	 * exactly as SET TRANSACTION SNAPSHOT does.
	 */
	StartTransactionCommand();
	XactIsoLevel = XACT_REPEATABLE_READ;
	ImportSnapshot(hdr->snapname);
	snap = GetTransactionSnapshot();
	PushActiveSnapshot(snap);

	PG_TRY();
	{
		int64		rows = 0;

		if (hdr->single_table)
		{
			Relation	rel = table_open(hdr->relid, AccessShareLock);
			List	   *groups = PgColumnarReadRowGroupList(hdr->storageId,
														  PgColumnarCatalogSnapshot(snap));
			int			ntake = me->endIdx - me->startIdx;
			/* always non-NULL, so an empty slice restricts to nothing (not all) */
			uint64	   *gnos = palloc(sizeof(uint64) * Max(ntake, 1));
			int			k = 0;
			int			idx = 0;
			ListCell   *lc;

			foreach(lc, groups)
			{
				if (idx >= me->startIdx && idx < me->endIdx)
				{
					NativeRowGroupMetadata *rg = (NativeRowGroupMetadata *) lfirst(lc);

					gnos[k++] = rg->groupNumber;
				}
				idx++;
			}
			rows = PgColumnarWriteParquetFile(rel, snap, me->filepath, gnos, k);
			table_close(rel, AccessShareLock);
		}
		else
		{
			int			i;

			for (i = me->startIdx; i < me->endIdx && i < hdr->npart; i++)
			{
				/* a fresh context per partition, so buffers do not accumulate */
				MemoryContext pctx = AllocSetContextCreate(CurrentMemoryContext,
														   "pgcolumnar parallel_export partition",
														   ALLOCSET_DEFAULT_SIZES);
				MemoryContext old = MemoryContextSwitchTo(pctx);
				Relation	part = table_open(leaves[i], AccessShareLock);
				char		fp[MAXPGPATH];

				snprintf(fp, sizeof(fp), "%s/part-%04d.parquet", hdr->dirpath, i);
				rows += PgColumnarWriteParquetFile(part, snap, fp, NULL, 0);
				table_close(part, AccessShareLock);
				MemoryContextSwitchTo(old);
				MemoryContextDelete(pctx);
			}
		}

		me->rows = rows;
		PopActiveSnapshot();
		CommitTransactionCommand();
		pg_atomic_write_u32(&me->state, PEXPORT_DONE);
	}
	PG_CATCH();
	{
		ErrorData  *edata;

		MemoryContextSwitchTo(TopMemoryContext);
		edata = CopyErrorData();
		me->sqlerrcode = edata->sqlerrcode;
		strlcpy(me->errmsg,
				edata->message ? edata->message : "parallel_export worker failed",
				sizeof(me->errmsg));
		FlushErrorState();
		FreeErrorData(edata);
		AbortOutOfAnyTransaction();
		pg_atomic_write_u32(&me->state, PEXPORT_FAILED);
	}
	PG_END_TRY();

	dsm_detach(seg);
	proc_exit(0);
}

/*
 * pgcolumnar_parallel_export_parquet
 *		SQL: pgcolumnar.parallel_export_parquet(target regclass, path text,
 *											    workers int DEFAULT NULL) -> bigint.
 */
PG_FUNCTION_INFO_V1(pgcolumnar_parallel_export_parquet);
Datum
pgcolumnar_parallel_export_parquet(PG_FUNCTION_ARGS)
{
	Oid			relid;
	char	   *dir;
	int			workers;
	Relation	rel;
	bool		single_table = false;
	int			ntasks = 0;		/* row-group count, or leaf-partition count */
	int			npart = 0;
	Oid		   *leafoids = NULL;
	uint64		storageId = 0;
	Snapshot	snap;
	bool		pushed = false;
	char	   *snapname;
	shm_toc_estimator est;
	Size		segsize;
	dsm_segment *seg;
	shm_toc    *toc;
	PexportHeader *hdr;
	PexportWorkerSlot *slots;
	Oid		   *leafchunk;
	uint32		dsmh;
	BackgroundWorker bw;
	BackgroundWorkerHandle **handles;
	PexportSpawn spawn;
	int			i;
	int			per,
				rem,
				cur;
	int64		total = 0;
	int			failed = -1;
	char		pathProbe[MAXPGPATH];

	if (PG_ARGISNULL(0) || PG_ARGISNULL(1))
		ereport(ERROR,
				(errcode(ERRCODE_NULL_VALUE_NOT_ALLOWED),
				 errmsg("target and path must not be null")));
	relid = PG_GETARG_OID(0);
	dir = text_to_cstring(PG_GETARG_TEXT_PP(1));
	workers = PG_ARGISNULL(2) ? pexport_auto_workers() : PG_GETARG_INT32(2);
	if (workers < 1)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("workers must be at least 1")));
	if (workers > PEXPORT_MAX_WORKERS)
		workers = PEXPORT_MAX_WORKERS;

	/*
	 * The workers write a server-side path and read the table. Require the
	 * write-server-files role (the write-side analog of parallel_copy's
	 * read-server-files check) plus SELECT on the target -- the workers do not
	 * run the executor permission check, so enforce it here before spawning.
	 */
	if (!has_privs_of_role(GetUserId(), ROLE_PG_WRITE_SERVER_FILES))
		ereport(ERROR,
				(errcode(ERRCODE_INSUFFICIENT_PRIVILEGE),
				 errmsg("must be superuser or a member of the pg_write_server_files role to export to server files")));
	{
		AclResult	ac = pg_class_aclcheck(relid, GetUserId(), ACL_SELECT);

		if (ac != ACLCHECK_OK)
			aclcheck_error(ac, OBJECT_TABLE, get_rel_name(relid));
	}

	/* RLS after the ACL check, matching core's ordering (#563). */
	PgColumnarRequireNoRowSecurity(relid);

	rel = table_open(relid, AccessShareLock);

	/* an MVCC snapshot to export; a SQL function normally has one active */
	if (!ActiveSnapshotSet())
	{
		PushActiveSnapshot(GetTransactionSnapshot());
		pushed = true;
	}
	snap = GetActiveSnapshot();

	if (rel->rd_rel->relkind == RELKIND_PARTITIONED_TABLE)
	{
		npart = pexport_leaf_partitions(relid, &leafoids);
		if (npart == 0)
			ereport(ERROR,
					(errcode(ERRCODE_OBJECT_NOT_IN_PREREQUISITE_STATE),
					 errmsg("partitioned table \"%s\" has no partitions to export",
							RelationGetRelationName(rel))));
		/* validate every leaf we will actually write, not just the parent */
		for (i = 0; i < npart; i++)
		{
			Relation	part = table_open(leafoids[i], AccessShareLock);

			if (!PgColumnarIsColumnarRelation(leafoids[i]))
			{
				char		nm[NAMEDATALEN];

				strlcpy(nm, RelationGetRelationName(part), NAMEDATALEN);
				table_close(part, AccessShareLock);
				ereport(ERROR,
						(errcode(ERRCODE_WRONG_OBJECT_TYPE),
						 errmsg("partition \"%s\" is not a columnar table", nm)));
			}
			PgColumnarParquetCheckExportable(part);
			table_close(part, AccessShareLock);
		}
		single_table = false;
		ntasks = npart;
		if (workers > npart)
			workers = npart;
	}
	else if (PgColumnarIsColumnarRelation(relid))
	{
		List	   *groups;

		PgColumnarParquetCheckExportable(rel);
		storageId = PgColumnarStorageId(rel);
		groups = PgColumnarReadRowGroupList(storageId, PgColumnarCatalogSnapshot(snap));
		ntasks = list_length(groups);
		single_table = true;
		if (workers > Max(ntasks, 1))
			workers = Max(ntasks, 1);
	}
	else
		ereport(ERROR,
				(errcode(ERRCODE_WRONG_OBJECT_TYPE),
				 errmsg("relation \"%s\" is not a columnar table or a partitioned table with columnar partitions",
						RelationGetRelationName(rel))));

	/*
	 * Worker paths cross DSM in MAXPGPATH buffers and add a generated part
	 * name. Refuse a destination that cannot hold the longest possible name;
	 * truncating it would publish a differently named object and still stamp
	 * _SUCCESS over an unreadable export.
	 */
	if (snprintf(pathProbe, sizeof(pathProbe), "%s/part-%04d.parquet",
				 dir, INT_MAX) >= (int) sizeof(pathProbe))
		ereport(ERROR,
				(errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
				 errmsg("parallel export destination is too long"),
				 errdetail("The destination and generated part name must fit in %d bytes.",
						   MAXPGPATH)));

	pexport_prepare_dir(dir);

	/*
	 * Export the snapshot for the workers to import. It stays valid until this
	 * transaction ends, and this function holds the transaction open across the
	 * whole wait, so every worker's import resolves against a live source.
	 */
	snapname = ExportSnapshot(snap);

	/* lay out one DSM segment: header + slots + the leaf-oid array */
	shm_toc_initialize_estimator(&est);
	shm_toc_estimate_chunk(&est, sizeof(PexportHeader));
	shm_toc_estimate_chunk(&est, mul_size(sizeof(PexportWorkerSlot), workers));
	shm_toc_estimate_chunk(&est, mul_size(sizeof(Oid), Max(npart, 1)));
	shm_toc_estimate_keys(&est, 3);
	segsize = shm_toc_estimate(&est);

	seg = dsm_create(segsize, 0);
	toc = shm_toc_create(PEXPORT_MAGIC, dsm_segment_address(seg), segsize);
	hdr = (PexportHeader *) shm_toc_allocate(toc, sizeof(PexportHeader));
	shm_toc_insert(toc, PEXPORT_KEY_HEADER, hdr);
	slots = (PexportWorkerSlot *) shm_toc_allocate(toc,
												   mul_size(sizeof(PexportWorkerSlot), workers));
	shm_toc_insert(toc, PEXPORT_KEY_SLOTS, slots);
	leafchunk = (Oid *) shm_toc_allocate(toc, mul_size(sizeof(Oid), Max(npart, 1)));
	shm_toc_insert(toc, PEXPORT_KEY_LEAVES, leafchunk);
	for (i = 0; i < npart; i++)
		leafchunk[i] = leafoids[i];
	dsmh = dsm_segment_handle(seg);

	hdr->dbid = MyDatabaseId;
	hdr->roleid = GetUserId();
	hdr->relid = relid;
	hdr->storageId = storageId;
	hdr->nworkers = workers;
	hdr->npart = npart;
	hdr->single_table = single_table;
	strlcpy(hdr->dirpath, dir, sizeof(hdr->dirpath));
	strlcpy(hdr->snapname, snapname, sizeof(hdr->snapname));

	/* even-ish contiguous [startIdx, endIdx) task ranges */
	per = ntasks / workers;
	rem = ntasks % workers;
	cur = 0;
	for (i = 0; i < workers; i++)
	{
		int			take = per + (i < rem ? 1 : 0);

		slots[i].startIdx = cur;
		slots[i].endIdx = cur + take;
		cur += take;
		slots[i].rows = 0;
		slots[i].sqlerrcode = 0;
		slots[i].errmsg[0] = '\0';
		pg_atomic_init_u32(&slots[i].state, PEXPORT_PENDING);
		if (single_table)
			snprintf(slots[i].filepath, sizeof(slots[i].filepath),
					 "%s/part-%04d.parquet", dir, i);
		else
			slots[i].filepath[0] = '\0';	/* worker names files per partition */
	}

	handles = (BackgroundWorkerHandle **)
		palloc0(sizeof(BackgroundWorkerHandle *) * workers);
	spawn.handles = handles;
	spawn.n = workers;
	spawn.dirpath = dir;
	/* one part-NNNN.parquet per worker (single table) or per partition */
	spawn.nkeys = single_table ? workers : npart;

	memset(&bw, 0, sizeof(bw));
	bw.bgw_flags = BGWORKER_SHMEM_ACCESS | BGWORKER_BACKEND_DATABASE_CONNECTION;
	/* read-only, so it may run once a standby has reached a consistent state */
	bw.bgw_start_time = BgWorkerStart_ConsistentState;
	bw.bgw_restart_time = BGW_NEVER_RESTART;
	strlcpy(bw.bgw_library_name, "pgcolumnar", BGW_MAXLEN);
	strlcpy(bw.bgw_function_name, "pgcolumnar_parallel_export_worker", BGW_MAXLEN);
	snprintf(bw.bgw_name, BGW_MAXLEN, "pgcolumnar parallel_export worker");
	snprintf(bw.bgw_type, BGW_MAXLEN, "pgcolumnar parallel_export worker");
	bw.bgw_main_arg = UInt32GetDatum(dsmh);
	bw.bgw_notify_pid = MyProcPid;

	/*
	 * PG_ENSURE_ERROR_CLEANUP so a FATAL in this backend still terminates the
	 * workers before it exits, rather than orphaning them to read on after the
	 * exported snapshot is gone.
	 */
	PG_ENSURE_ERROR_CLEANUP(pexport_cleanup, PointerGetDatum(&spawn));
	{
		for (i = 0; i < workers; i++)
		{
			memcpy(bw.bgw_extra, &i, sizeof(int));
			if (!RegisterDynamicBackgroundWorker(&bw, &handles[i]))
				ereport(ERROR,
						(errcode(ERRCODE_INSUFFICIENT_RESOURCES),
						 errmsg("could not register pgcolumnar parallel_export worker %d of %d",
								i + 1, workers),
						 errhint("Increase max_worker_processes; parallel_export_parquet needs %d worker slots.",
								 workers)));
		}

		/* wait for all workers, staying responsive to cancellation */
		for (;;)
		{
			int			running = 0;

			CHECK_FOR_INTERRUPTS();
			for (i = 0; i < workers; i++)
			{
				pid_t		pid;

				if (handles[i] != NULL &&
					GetBackgroundWorkerPid(handles[i], &pid) != BGWH_STOPPED)
					running++;
			}
			if (running == 0)
				break;
			(void) WaitLatch(MyLatch, WL_LATCH_SET | WL_TIMEOUT | WL_EXIT_ON_PM_DEATH,
							 1000L, PG_WAIT_EXTENSION);
			ResetLatch(MyLatch);
		}
	}
	PG_END_ENSURE_ERROR_CLEANUP(pexport_cleanup, PointerGetDatum(&spawn));

	for (i = 0; i < workers; i++)
	{
		if (pg_atomic_read_u32(&slots[i].state) == PEXPORT_DONE)
			total += slots[i].rows;
		else if (failed < 0)
			failed = i;
	}

	if (failed >= 0)
	{
		int			code = slots[failed].sqlerrcode;
		char		msg[512];

		strlcpy(msg, slots[failed].errmsg[0] ? slots[failed].errmsg
				: "worker exited without reporting a result", sizeof(msg));
		/* all workers have stopped; drop any part files they wrote so the failed
		 * run leaves a clean directory and a retry is not half-read or blocked */
		pexport_remove_outputs(dir, single_table ? workers : npart);
		dsm_detach(seg);
		if (pushed)
			PopActiveSnapshot();
		table_close(rel, AccessShareLock);
		ereport(ERROR,
				(errcode(code ? code : ERRCODE_INTERNAL_ERROR),
				 errmsg("pgcolumnar.parallel_export_parquet worker %d failed: %s",
						failed, msg)));
	}

	dsm_detach(seg);
	if (pushed)
		PopActiveSnapshot();
	table_close(rel, AccessShareLock);

	/*
	 * Every worker completed. On a remote prefix, first drop any stale
	 * higher-numbered parts a larger prior run left (see
	 * pexport_remove_stale_remote_parts), so the marker certifies exactly this
	 * run's parts. Then write the _SUCCESS marker (#394). Both are past the
	 * error-cleanup region (PG_END_ENSURE_ERROR_CLEANUP above), so a failure here
	 * raises without deleting the parts the workers already committed -- the data
	 * is preserved and the caller learns the completion signal could not be
	 * finalized, rather than losing a good export to a marker hiccup.
	 */
	if (PgColumnarPathIsRemote(dir))
		pexport_remove_stale_remote_parts(dir, single_table ? workers : npart);
	pexport_write_success_marker(dir);
	PG_RETURN_INT64(total);
}
