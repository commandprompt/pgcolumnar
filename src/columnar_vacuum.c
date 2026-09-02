/*-------------------------------------------------------------------------
 *
 * pgcolumnar_vacuum.c
 *		Compaction, statistics, and storage-id lookup functions for pgColumnar
 *		(spec 8.2, 9). columnar.vacuum rewrites a columnar table's live rows
 *		into fresh, full stripes: this combines many small stripes into few and
 *		physically reclaims the space of rows marked deleted in the delete vector,
 *		since deleted rows are simply not read back. columnar.stats reports the
 *		per-stripe layout (implemented in SQL over the catalog, using the
 *		get_storage_id function here to resolve a relation to its storage id).
 *
 * The rewrite swaps the relation to a new relfilenode (RelationSetNewRelfile-
 * number), which is transactional: on rollback the original storage remains.
 * Because compaction assigns fresh row numbers, indexes are rebuilt afterward
 * so their synthetic item pointers (spec 6) address the new rows.
 *
 * Independent MIT implementation built from
 * design/NATIVE_FORMAT_AND_INTERFACE_SPEC.md and the public PostgreSQL API only.
 *
 *-------------------------------------------------------------------------
 */
#include "columnar.h"
#include "columnar_metadata.h"
#include "columnar_storage.h"
#include "columnar_write_state.h"
#include "columnar_compat.h"

#include <math.h>

#include "fmgr.h"
#include "access/genam.h"
#include "access/xact.h"
#include "access/relation.h"
#include "access/table.h"
#include "catalog/index.h"
#include "catalog/pg_class.h"
#include "catalog/pg_type.h"
#include "executor/executor.h"
#include "executor/tuptable.h"
#include "miscadmin.h"
#include "pgstat.h"
#include "storage/latch.h"
#include "storage/lmgr.h"
#include "storage/lockdefs.h"
#include "storage/procarray.h"
#include "storage/smgr.h"
#include "utils/acl.h"
#include "utils/array.h"
#include "utils/inval.h"
#include "utils/builtins.h"
#include "utils/lsyscache.h"
#include "utils/relcache.h"
#include "utils/timestamp.h"
#include "utils/rls.h"
#include "utils/rel.h"
#include "utils/snapmgr.h"
#include "utils/tuplesort.h"
#include "utils/tuplestore.h"
#include "utils/typcache.h"

PG_FUNCTION_INFO_V1(pgcolumnar_relation_storageid);
PG_FUNCTION_INFO_V1(pgcolumnar_vacuum);
PG_FUNCTION_INFO_V1(pgcolumnar_vacuum_sorted);
PG_FUNCTION_INFO_V1(pgcolumnar_cluster);
PG_FUNCTION_INFO_V1(pgcolumnar_compact);
PG_FUNCTION_INFO_V1(pgcolumnar_expire);
PG_FUNCTION_INFO_V1(pgcolumnar_compact_rewrite);
PG_FUNCTION_INFO_V1(pgcolumnar_recluster);
PG_FUNCTION_INFO_V1(pgcolumnar_truncate);
PG_FUNCTION_INFO_V1(pgcolumnar_debug_advance_reserved_offset);
PG_FUNCTION_INFO_V1(pgcolumnar_debug_set_metapage_version);

/* physical end-truncation opt-in (GUC), registered in _PG_init. Default off
 * until the abort/crash path is fully hardened and matrix-validated. */
bool		pgcolumnar_enable_end_truncation = false;

PG_FUNCTION_INFO_V1(pgcolumnar_require_caller_select);

/*
 * pgcolumnar_require_caller_select(rel regclass) -> void
 *		Raise unless the CALLER may SELECT the relation.
 *
 * Exists for pgcolumnar.stats, which is SECURITY DEFINER (#560). stats() reads
 * pgcolumnar's catalog tables, which carry no GRANT, so a columnar table's own
 * owner could not read the statistics of the table they own. Granting SELECT on
 * those catalogs instead would publish pgcolumnar.zone_map, which stores per
 * column minimum, maximum and sum for every columnar table: actual column
 * values, and a much larger disclosure than the bug being fixed.
 *
 * GetOuterUserId, NOT GetUserId. Inside a SECURITY DEFINER function the
 * effective user is the function's OWNER, so GetUserId returns the superuser who
 * installed the extension and pg_class_aclcheck against it returns ACLCHECK_OK
 * for every relation in the database. Such a check refuses nobody while looking
 * exactly like a correct one. Measured on a build of this tree:
 *
 *		plain call   as t_id: cur=t_id      outer=t_id  sess=t_id
 *		definer call as t_id: cur=postgres  outer=t_id  sess=t_id
 *
 * GetOuterUserId is the calling role, and it follows SET ROLE, which is the
 * behaviour wanted: the privilege tested is the one the caller is acting with.
 */
Datum
pgcolumnar_require_caller_select(PG_FUNCTION_ARGS)
{
	Oid			relid = PG_GETARG_OID(0);
	AclResult	ac = pg_class_aclcheck(relid, GetOuterUserId(), ACL_SELECT);

	if (ac != ACLCHECK_OK)
		aclcheck_error(ac, OBJECT_TABLE, get_rel_name(relid));

	PG_RETURN_VOID();
}

/*
 * PgColumnarRequireNoRowSecurity
 *		Refuse a relation whose row-level security policies apply to this caller.
 *
 * Row-level security is applied by the REWRITER to a query's range table entry.
 * Every entry point that calls this opens storage and reads or writes it
 * directly, so there is no query to rewrite and no policy is ever consulted. A
 * caller holding SELECT but restricted to one row by a policy received every
 * row (#563).
 *
 * Refusing is the fix rather than applying the policies, deliberately. Routing
 * these reads through SPI or the executor would discard the reason
 * direct-storage access exists, and evaluating policies per row would
 * reimplement the rewriter inside an extension. A refusal closes the hole
 * completely, is cheap, and is defensible the day it ships.
 *
 * The bar is RLS_ENABLED and nothing wider. check_enable_rls returns
 * RLS_NONE_ENV for a superuser, a BYPASSRLS role, and an owner without FORCE
 * ROW LEVEL SECURITY. All of those already read every row by other means, so
 * refusing them would break admin tooling and export fixtures while buying no
 * confidentiality. noError is true so the verdict does not change with the
 * row_security GUC.
 *
 * FORCE ROW LEVEL SECURITY makes check_enable_rls return RLS_ENABLED for the
 * owner too, which is why declaring this surface owner-only would narrow the
 * hole rather than close it.
 *
 * Call this AFTER the relation ACL check, not before it.
 *
 * An earlier version of this argued the opposite, that an RLS check below the
 * ACL check "is never reached by the only caller that needs it". That reasoning
 * is wrong and was corrected in review: the caller this exists for HOLDS
 * SELECT, so it passes pg_class_aclcheck without raising and reaches whatever
 * follows. The ACL check only raises for a caller who lacks the privilege.
 *
 * What the order actually decides is the error a caller WITHOUT privilege sees
 * on an RLS table. With this check first, they were told "row-level security is
 * in force", which discloses RLS state that ordinary SQL does not, and disagrees
 * with core, which applies the ACL first. Measured:
 *
 *		RLS on, no-select, ordinary SQL     -> permission denied for table
 *		RLS on, no-select, read_projection  -> row-level security is in force
 *
 * Below the ACL check both say "permission denied", and the intended caller's
 * behaviour is identical either way.
 */
void
PgColumnarRequireNoRowSecurity(Oid relid)
{
	if (check_enable_rls(relid, InvalidOid, true) == RLS_ENABLED)
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("row-level security is in force on \"%s\"",
						get_rel_name(relid)),
				 errdetail("pgcolumnar reads and writes this relation's storage "
						   "directly, so row-level security policies are not "
						   "applied."),
				 errhint("Use ordinary SQL against the table, which applies the "
						 "policies.")));
}

/*
 * PgColumnarRequireTableOwner
 *		Error unless the current user owns the relation (superusers pass). Every
 *		maintenance and projection-DDL function gates on this: they rewrite data,
 *		reclaim space, or take strong locks (truncate takes AccessExclusiveLock),
 *		so they must be owner-only, like VACUUM and CLUSTER.
 */
void
PgColumnarRequireTableOwner(Relation rel)
{
	if (!COLUMNAR_TABLE_OWNERCHECK(RelationGetRelid(rel)))
		aclcheck_error(ACLCHECK_NOT_OWNER, OBJECT_TABLE,
					   RelationGetRelationName(rel));
}

/*
 * Same check on an Oid, for callers that must refuse BEFORE table_open (#568).
 * vacuum, vacuum_sorted and cluster open with AccessExclusiveLock, which queues
 * in the lock FIFO ahead of readers. An unprivileged caller that reached
 * table_open would sit in that queue and block every reader of a table it does
 * not own until its own lock request was resolved, so it has to be turned away
 * before the lock is requested rather than after it is held.
 */
void
PgColumnarRequireTableOwnerByOid(Oid relid)
{
	if (!COLUMNAR_TABLE_OWNERCHECK(relid))
		aclcheck_error(ACLCHECK_NOT_OWNER, OBJECT_TABLE, get_rel_name(relid));
}

/* Z-order helpers (defined later, used by the online recluster below) */
static bool cluster_type_supported(Oid typid);
static bytea *cluster_zorder_key(Datum *values, bool *isnull, AttrNumber *atts,
								 int ncols, TupleDesc tupdesc);
/* Names an ordering rewrite records as its key (defined later, #415) */
static List *sort_key_names(TupleDesc tupdesc, AttrNumber *atts, int ncols);
static bool vacuum_sorted_gate_is_noop(Relation rel, int ncols,
									   AttrNumber *atts);

/* v1 refuses tables with more groups than this (one advisory lock per group) */
#define RECLUSTER_MAX_GROUPS 8192

static int
uint64_cmp(const void *a, const void *b)
{
	uint64		x = *(const uint64 *) a;
	uint64		y = *(const uint64 *) b;

	return (x < y) ? -1 : (x > y) ? 1 : 0;
}

/*
 * A retired group and the row numbers it held. Recluster needs both: the number
 * to lock and retire in ascending order, and the range to clear from the
 * visibility map, because those row numbers are being reassigned. Carried in
 * one struct rather than parallel arrays so the sort below cannot separate a
 * group from its range.
 */
typedef struct RetiredGroup
{
	uint64		groupNumber;
	uint64		firstRowNumber;
	uint64		rowCount;
}			RetiredGroup;

static int
retired_group_cmp(const void *a, const void *b)
{
	uint64		x = ((const RetiredGroup *) a)->groupNumber;
	uint64		y = ((const RetiredGroup *) b)->groupNumber;

	return (x < y) ? -1 : (x > y) ? 1 : 0;
}

/* -------------------------------------------------------------------------
 * Online rewrite of partially-deleted row groups (Phase F3b)
 *
 * Reclaims the space of deleted rows in a group that is only partially deleted
 * (F3a retires only fully-dead groups) by rewriting the group's live rows into a
 * new group with fresh row numbers, then retiring the old group -- all under
 * ShareUpdateExclusiveLock, concurrent with readers and writers. Correctness
 * rests on the protocol in design/PHASE_F3B_PLAN.md: the rewrite serializes with
 * deleters on the per-chunk-group advisory lock, and a delete that races a
 * rewrite of its group is aborted with a serialization failure by the
 * conflict check in PgColumnarUpsertDeleteVector.
 * ------------------------------------------------------------------------- */

/* the relation's ready indexes, opened once for a rewrite pass */
/*
 * Rewrite one group's live rows into a fresh group and retire the old group.
 * Returns the number of live rows moved. Caller holds ShareUpdateExclusiveLock
 * on rel and has opened the indexes in ris.
 */
static int64
rewrite_one_group(Relation rel, PgColumnarIndexInsertState *ris, uint64 storageId,
				  uint64 groupNumber, uint64 firstRow, uint64 rowCount)
{
	Oid			relid = RelationGetRelid(rel);
	int			natts = RelationGetDescr(rel)->natts;
	Datum	   *values = palloc(natts * sizeof(Datum));
	bool	   *isnull = palloc(natts * sizeof(bool));
	PgColumnarWriteState *ws;
	Snapshot	snap;
	PgColumnarReadState *rs;
	uint64		oldRn;
	int64		moved = 0;

	/* serialize with concurrent deleters to this group (see PgColumnarUpsertDeleteVector) */
	PgColumnarLockChunkGroup(storageId, groupNumber);

	/* Read the group's live set under a snapshot taken after the lock, so every
	 * delete committed before the lock is reflected. Register it (not just push
	 * active): the reader derives a catalog snapshot copy from it, in
	 * PgColumnarCatalogSnapshot, that must inherit a nonzero regd_count for PG18's
	 * heap-visibility assertion, which a merely-active GetLatestSnapshot does not
	 * provide. */
	snap = RegisterSnapshot(GetLatestSnapshot());
	PushActiveSnapshot(snap);

	ws = PgColumnarGetWriteState(rel);

	/*
	 * Stream the group rather than fetching its rows one at a time.
	 *
	 * PgColumnarReadRowByNumber decodes the whole group to return one value and
	 * relies on the fetch cache to make the next call cheap. That cache holds
	 * only what fits under COLUMNAR_FETCH_CACHE_MAX_BYTES, so a group whose
	 * decoded form exceeds the cap re-decodes the columns that did not fit, once
	 * per row rather than once per group.
	 *
	 * That used to be a cliff rather than a slope: the whole entry was dropped,
	 * so a group over the cap by any margin decoded entirely, per row. Three
	 * columns of 150,000 rows with one varlena among them decodes to 34,713,408
	 * bytes against a 33,554,432 cap: 3.5% over. On an idle box, rewriting
	 * 200,000 rows of that shape did not finish inside 120 seconds, while the
	 * same table without the middle column -- under the cap, so cached -- took
	 * 1.9. #359 made the overflow proportional, which shrinks that gap but does
	 * not close it; streaming remains strictly cheaper than fetching per row.
	 *
	 * A reader restricted to this group decodes it once and walks it, which is
	 * what the loop wanted all along, and it does not care how large the group
	 * is: 60 decode calls for the shape above, against one per row per column.
	 * ANALYZE's sampler was moved off the same per-row fetch for the same
	 * reason.
	 */
	rs = PgColumnarBeginRead(rel, snap, NULL, NULL, 0, NULL);
	PgColumnarReadRestrictToGroups(rs, &groupNumber, 1);

	while (PgColumnarReadNextRow(rs, values, isnull, &oldRn))
	{
		uint64		newRn;

		CHECK_FOR_INTERRUPTS();

		newRn = PgColumnarWriteRow(ws, rel, values, isnull);
		PgColumnarProjectionFanoutRow(rel, ws, newRn, values, isnull);
		PgColumnarIndexInsertRow(ris, rel, values, isnull, newRn);
		moved++;
	}

	PgColumnarEndRead(rs);
	PgColumnarFlushWriteStateForRelation(relid);

	/* atomically (same transaction) the new group is now in the catalog; drop the
	 * old one. Heap MVCC keeps the old group readable to older snapshots.
	 *
	 * The rows just moved carry NEW row numbers, so the old numbers' visibility
	 * map bits must go with the group. Without this an index-only scan answers
	 * from the index for a TID whose group no longer exists. */
	PgColumnarVMClearForRowRange(rel, firstRow, rowCount);
	PgColumnarRetireGroup(storageId, groupNumber);

	PopActiveSnapshot();
	UnregisterSnapshot(snap);
	pfree(values);
	pfree(isnull);
	return moved;
}

/* one candidate group to rewrite */
typedef struct RewriteCandidate
{
	uint64		groupNumber;
	uint64		firstRow;
	uint64		rowCount;
} RewriteCandidate;

/*
 * pgcolumnar_rewrite_partial_groups
 *		Rewrite up to maxGroups groups whose deleted fraction is at least
 *		minDeletedFraction (and which are not fully dead -- F3a handles those).
 *		maxGroups <= 0 means all. Returns the number of groups rewritten.
 */
static int64
pgcolumnar_rewrite_partial_groups(Relation rel, double minDeletedFraction,
								int maxGroups)
{
	uint64		storageId = PgColumnarStorageId(rel);
	Oid			relid = RelationGetRelid(rel);
	Snapshot	snap;
	List	   *rgList;
	ListCell   *lc;
	List	   *cands = NIL;
	PgColumnarIndexInsertState *ris;
	int64		rewritten = 0;

	/* persist own pending work so the group list and deletes are current */
	PgColumnarFlushWriteStateForRelation(relid);
	PgColumnarFlushDeleteVectorForRelation(rel);

	/* drop any free_space row overlapping a live group (the residual of a crash
	 * in end-truncation's narrow window) before reusing anything */
	PgColumnarReconcileFreeList(rel);

	/* collect candidate groups first (do not mutate the catalog mid-scan) */
	snap = RegisterSnapshot(GetLatestSnapshot());
	rgList = PgColumnarReadRowGroupList(storageId, snap);
	foreach(lc, rgList)
	{
		NativeRowGroupMetadata *rg = (NativeRowGroupMetadata *) lfirst(lc);
		List	   *rmList;
		ListCell   *lc2;
		int64		deleted = 0;

		if (rg->rowCount == 0)
			continue;
		rmList = PgColumnarReadDeleteVectorList(storageId, rg->groupNumber, snap);
		foreach(lc2, rmList)
			deleted += ((DeleteVectorMetadata *) lfirst(lc2))->deletedCount;

		/* partially deleted and past the threshold; fully-dead is F3a's job */
		if (deleted > 0 && deleted < (int64) rg->rowCount &&
			(double) deleted / (double) rg->rowCount >= minDeletedFraction)
		{
			RewriteCandidate *c = palloc(sizeof(RewriteCandidate));

			c->groupNumber = rg->groupNumber;
			c->firstRow = rg->firstRowNumber;
			c->rowCount = rg->rowCount;
			cands = lappend(cands, c);
		}
	}
	UnregisterSnapshot(snap);

	if (cands == NIL)
		return 0;

	ris = PgColumnarIndexInsertBegin(rel, false);
	foreach(lc, cands)
	{
		RewriteCandidate *c = (RewriteCandidate *) lfirst(lc);

		if (maxGroups > 0 && rewritten >= maxGroups)
			break;
		rewrite_one_group(rel, ris, storageId, c->groupNumber,
						  c->firstRow, c->rowCount);
		rewritten++;
	}
	PgColumnarIndexInsertEnd(ris);

	COLUMNAR_ASSERT_NO_OVERLAP(storageId);
	return rewritten;
}

/*
 * pgcolumnar_compact_rewrite
 *		SQL: pgcolumnar.compact_rewrite(tablename regclass,
 *			 min_deleted_fraction float8 default 0.2, max_groups int default 0).
 *		The lazy online space-reclaiming path (Phase F3b): rewrite partially
 *		deleted groups to drop their dead rows, under ShareUpdateExclusiveLock
 *		(concurrent reads and writes). Returns the number of groups rewritten.
 */
/*
 * record_online_sorted_extent
 *		Mark where this online rewrite's ordered run ends, when it can prove
 *		where that is (#311).
 *
 *		The rewrite holds ShareUpdateExclusiveLock, so another session can be
 *		inserting while it works. sort_status reads the mark as a boundary,
 *		"every group numbered at or below this one is ordered", and a single
 *		boundary can only say that if no foreign group is numbered below it.
 *
 *		A concurrent inserter reserves its stripe id when it buffers its first
 *		row and commits some time later, so its group can be absent from any list
 *		this reads while still owning an id among the rewrite's own.
 *
 *		The run is therefore recorded as a range, [ours[0], runEnd], not as a
 *		single upper bound. Ids come from one serialized counter, so a foreign id
 *		strictly inside that range must have been drawn between two of the
 *		rewrite's own draws, which breaks the consecutive run below and truncates
 *		it before reaching that id. An id drawn below the rewrite's first falls
 *		outside the range by construction. Both cases hold whenever the foreign
 *		transaction commits, because neither depends on seeing it.
 *
 *		An earlier version marked only an upper bound and tried to exclude the
 *		below case by requiring the lowest live group to equal ours[0]. That
 *		could not work (#342): the group list was read under the rewrite's own
 *		snapshot, taken before it read a row, and PgColumnarCatalogSnapshot only
 *		advances curcid rather than refreshing xmin/xmax, so a concurrent
 *		inserter's group was invisible to the check whenever it committed. A
 *		foreign group written just before the rewrite's first reservation was
 *		then swept under the mark and counted as ordered.
 *
 *		Truncating at the first gap still marks only the part it can prove. That
 *		reports more decay than there is, which is the direction to fail in: it
 *		can prompt a re-sort that was not needed, where the opposite leaves a
 *		decayed table looking ordered and costs every query against it.
 */
static void
record_online_sorted_extent(Relation rel, uint64 storageId,
							PgColumnarWriteState *writeState, int stripeMark,
							List *sortByNames, const char *sortedKind)
{
	int			nAll;
	uint64	   *all = PgColumnarWriteStateStripeIds(writeState, &nAll);
	int			nOurs = nAll - stripeMark;
	uint64	   *ours;
	uint64		runEnd;
	int			i;

	if (nOurs <= 0)
		return;					/* this rewrite reserved nothing */

	/*
	 * Our own reservations, ascending -- including the ones our projection
	 * fan-out drew (#345). A projection writes through its own write state but
	 * reserves from this relation's stripe counter, so its ids interleave with
	 * ours and would otherwise read as foreign reservations, truncating the run
	 * at the first projection flush. They are ours: the same transaction drew
	 * them. Only ids at or above our own first one are taken, so anything drawn
	 * before this rewrite began is still excluded.
	 */
	ours = (uint64 *) palloc(sizeof(uint64) * nOurs);
	memcpy(ours, all + stripeMark, sizeof(uint64) * nOurs);
	qsort(ours, nOurs, sizeof(uint64), uint64_cmp);

	{
		int			nProj = 0;
		uint64	   *projIds = PgColumnarWriteStateProjStripeIds(writeState, &nProj);

		if (nProj > 0)
		{
			uint64		lo = ours[0];
			uint64	   *merged = (uint64 *) palloc(sizeof(uint64) * (nOurs + nProj));
			int			m = nOurs;
			int			j;

			memcpy(merged, ours, sizeof(uint64) * nOurs);
			for (j = 0; j < nProj; j++)
			{
				if (projIds[j] >= lo)
					merged[m++] = projIds[j];
			}
			pfree(ours);
			ours = merged;
			nOurs = m;
			qsort(ours, nOurs, sizeof(uint64), uint64_cmp);
		}
		if (projIds != NULL)
			pfree(projIds);
	}

	/* the consecutive run of our own ids, starting at the lowest */
	runEnd = ours[0];
	for (i = 1; i < nOurs; i++)
	{
		if (ours[i] != runEnd + 1)
			break;				/* a foreign reservation took this id */
		runEnd = ours[i];
	}

	PgColumnarSetSortedExtent(storageId, (int64) ours[0], (int64) runEnd,
							  sortByNames, sortedKind);
	pfree(ours);
}

/*
 * pgcolumnar_recluster_online
 *		Re-establish global Z-order clustering over the relation's live rows
 *		online (Phase F3c): read all live rows under a snapshot taken after
 *		advisory-locking every group, Morton-sort them, write them back as fresh
 *		groups with online index maintenance, and retire the old groups in the same
 *		transaction. Holds ShareUpdateExclusiveLock (the caller's), so reads never
 *		block; deletes to the reclustered groups serialize and retry via the F3b
 *		conflict protocol. Returns the number of groups retired.
 */
static int64
pgcolumnar_recluster_online(Relation rel, int ncols, AttrNumber *atts)
{
	uint64		storageId = PgColumnarStorageId(rel);
	Oid			relid = RelationGetRelid(rel);
	TupleDesc	tupdesc = RelationGetDescr(rel);
	int			natts = tupdesc->natts;
	AttrNumber	zAtt = (AttrNumber) (natts + 1);
	Snapshot	listSnap;
	List	   *rgList;
	ListCell   *lc;
	RetiredGroup *oldGroups;
	int			nGroups = 0;
	int			i;
	Snapshot	snap;
	TupleDesc	augdesc;
	Tuplesortstate *tsort;
	TupleTableSlot *readSlot;
	TupleTableSlot *putSlot;
	TupleTableSlot *augSlot;
	TypeCacheEntry *tce;
	Oid			byteaLt;
	Oid			sortColl = InvalidOid;
	bool		nullsFirst = false;
	PgColumnarReadState *readState;
	PgColumnarWriteState *writeState;
	PgColumnarIndexInsertState *ris;
	uint64		rowNumber;
	int			stripeMark;

	/* persist own pending work so the group list and deletes are current */
	PgColumnarFlushWriteStateForRelation(relid);
	PgColumnarFlushDeleteVectorForRelation(rel);

	/* drop any free_space row overlapping a live group (the residual of a crash
	 * in end-truncation's narrow window) before reusing anything */
	PgColumnarReconcileFreeList(rel);

	/* capture the current groups (retired at the end, after the new ones exist) */
	listSnap = RegisterSnapshot(GetLatestSnapshot());
	rgList = PgColumnarReadRowGroupList(storageId, listSnap);
	oldGroups = palloc(sizeof(RetiredGroup) *
					   (list_length(rgList) > 0 ? list_length(rgList) : 1));
	foreach(lc, rgList)
	{
		NativeRowGroupMetadata *rg = (NativeRowGroupMetadata *) lfirst(lc);

		oldGroups[nGroups].groupNumber = rg->groupNumber;
		oldGroups[nGroups].firstRowNumber = rg->firstRowNumber;
		oldGroups[nGroups].rowCount = rg->rowCount;
		nGroups++;
	}
	UnregisterSnapshot(listSnap);

	if (nGroups == 0)
		return 0;
	if (nGroups > RECLUSTER_MAX_GROUPS)
		ereport(ERROR,
				(errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
				 errmsg("table has %d row groups, above the online recluster limit of %d",
						nGroups, RECLUSTER_MAX_GROUPS),
				 errhint("Use pgcolumnar.cluster() for a one-shot reorg of a very large table.")));

	/* lock every group in ascending order (deadlock-safe), held to commit */
	qsort(oldGroups, nGroups, sizeof(RetiredGroup), retired_group_cmp);

	/*
	 * Self-gate (#415): if the whole live relation is already the Z-order run
	 * over exactly these columns -- nothing appended past the recorded run, same
	 * kind, same key -- there is nothing to recluster. Return before locking and
	 * rewriting every group, so a scheduler (or a user) can call recluster
	 * speculatively without paying a full rewrite each time. Any mismatch
	 * (appended groups, a different key, a lexicographic or unknown run) falls
	 * through to the full reorg below, so re-clustering by a new key still works.
	 */
	{
		int64		sfrom,
					sthrough;
		List	   *skey;
		char	   *skind;
		bool		sameKey = false;

		PgColumnarGetSortedInfo(storageId, &sfrom, &sthrough, &skey, &skind);
		if (skind != NULL && strcmp(skind, "zorder") == 0 &&
			list_length(skey) == ncols)
		{
			ListCell   *klc;

			sameKey = true;
			i = 0;
			foreach(klc, skey)
			{
				const char *want = NameStr(TupleDescAttr(tupdesc, atts[i] - 1)->attname);

				if (strcmp((char *) lfirst(klc), want) != 0)
				{
					sameKey = false;
					break;
				}
				i++;
			}
		}
		if (sameKey && sfrom >= 0 && sthrough >= 0 &&
			(int64) oldGroups[0].groupNumber >= sfrom &&
			(int64) oldGroups[nGroups - 1].groupNumber <= sthrough)
		{
			pfree(oldGroups);
			/* no active snapshot pushed yet at this point -- see below */
			return 0;
		}
	}

	for (i = 0; i < nGroups; i++)
		PgColumnarLockChunkGroup(storageId, oldGroups[i].groupNumber);

	/* read all live rows into a Morton-keyed tuplesort (as in eager cluster).
	 * Register the snapshot (not just push active) so the catalog snapshot copies
	 * the reader derives satisfy PG18's heap-visibility assertion. */
	snap = RegisterSnapshot(GetLatestSnapshot());
	PushActiveSnapshot(snap);

	augdesc = CreateTemplateTupleDesc(natts + 1);
	for (i = 1; i <= natts; i++)
		TupleDescCopyEntry(augdesc, (AttrNumber) i, tupdesc, (AttrNumber) i);
	TupleDescInitEntry(augdesc, zAtt, "__zorder", BYTEAOID, -1, 0);
#if PG_VERSION_NUM >= 190000
	/* PG19 requires a manually-built TupleDesc to be finalized before use, which
	 * computes firstNonCachedOffsetAttr (asserted by the tuple routines) after the
	 * TupleDescCopyEntry calls filled the FormData array directly. */
	TupleDescFinalize(augdesc);
#endif

	tce = lookup_type_cache(BYTEAOID, TYPECACHE_LT_OPR);
	byteaLt = tce->lt_opr;
	tsort = tuplesort_begin_heap(augdesc, 1, &zAtt, &byteaLt, &sortColl,
								 &nullsFirst, maintenance_work_mem, NULL,
								 COLUMNAR_TUPLESORT_NONACCESS);

	readSlot = MakeSingleTupleTableSlot(tupdesc, &TTSOpsVirtual);
	putSlot = MakeSingleTupleTableSlot(augdesc, &TTSOpsVirtual);
	augSlot = MakeSingleTupleTableSlot(augdesc, &TTSOpsMinimalTuple);

	readState = PgColumnarBeginRead(rel, snap, NULL, NULL, 0, NULL);
	while (PgColumnarReadNextRow(readState, readSlot->tts_values,
							   readSlot->tts_isnull, &rowNumber))
	{
		bytea	   *zkey;

		CHECK_FOR_INTERRUPTS();
		memcpy(putSlot->tts_values, readSlot->tts_values, natts * sizeof(Datum));
		memcpy(putSlot->tts_isnull, readSlot->tts_isnull, natts * sizeof(bool));
		zkey = cluster_zorder_key(readSlot->tts_values, readSlot->tts_isnull,
								  atts, ncols, tupdesc);
		putSlot->tts_values[natts] = PointerGetDatum(zkey);
		putSlot->tts_isnull[natts] = false;
		ExecStoreVirtualTuple(putSlot);
		tuplesort_puttupleslot(tsort, putSlot);
		ExecClearTuple(putSlot);
	}
	PgColumnarEndRead(readState);
	ExecDropSingleTupleTableSlot(readSlot);
	ExecDropSingleTupleTableSlot(putSlot);
	tuplesort_performsort(tsort);

	/* write the sorted rows back as fresh groups, with online index maintenance */
	ris = PgColumnarIndexInsertBegin(rel, false);
	writeState = PgColumnarGetWriteState(rel);
	/*
	 * Note where this rewrite's own stripe reservations begin (#311). The write
	 * state can already hold entries from earlier work in this transaction, so
	 * only the tail from here on belongs to us.
	 */
	stripeMark = PgColumnarWriteStateStripeCount(writeState);
	while (tuplesort_gettupleslot(tsort, true, false, augSlot, NULL))
	{
		uint64		newRn;

		CHECK_FOR_INTERRUPTS();
		slot_getallattrs(augSlot);
		newRn = PgColumnarWriteRow(writeState, rel, augSlot->tts_values,
								 augSlot->tts_isnull);
		PgColumnarProjectionFanoutRow(rel, writeState, newRn, augSlot->tts_values,
									augSlot->tts_isnull);
		PgColumnarIndexInsertRow(ris, rel, augSlot->tts_values,
							   augSlot->tts_isnull, newRn);
	}
	PgColumnarFlushWriteStateForRelation(relid);
	PgColumnarIndexInsertEnd(ris);
	tuplesort_end(tsort);
	ExecDropSingleTupleTableSlot(augSlot);

	/*
	 * Retire the old groups; heap MVCC keeps them readable to older snapshots.
	 *
	 * Clear the visibility map over the row numbers each retired group held.
	 * Recluster reassigns those rows fresh numbers, so an index-only scan that
	 * answers from the index for an old TID would answer for a group that is
	 * gone. That is the same rule expire follows, and it holds wherever LIVE
	 * rows are renumbered rather than only where they expire.
	 */
	for (i = 0; i < nGroups; i++)
	{
		PgColumnarVMClearForRowRange(rel, oldGroups[i].firstRowNumber,
									 oldGroups[i].rowCount);
		PgColumnarRetireGroup(storageId, oldGroups[i].groupNumber);
	}

	/* record how far the reordered run reaches (#311) and BY WHAT (#415) */
	record_online_sorted_extent(rel, storageId, writeState, stripeMark,
								sort_key_names(tupdesc, atts, ncols), "zorder");

	PopActiveSnapshot();
	UnregisterSnapshot(snap);
	pfree(oldGroups);

	COLUMNAR_ASSERT_NO_OVERLAP(storageId);
	return nGroups;
}

/*
 * pgcolumnar_recluster
 *		SQL: pgcolumnar.recluster(tablename regclass, VARIADIC columns name[]).
 *		The lazy online counterpart to cluster(): re-establish global Z-order
 *		clustering under ShareUpdateExclusiveLock (concurrent reads and writes),
 *		not the AccessExclusiveLock the eager cluster() reorg takes. Returns the
 *		number of groups reclustered.
 */
Datum
pgcolumnar_recluster(PG_FUNCTION_ARGS)
{
	Oid			relid = PG_GETARG_OID(0);
	ArrayType  *colArray;
	Datum	   *colDatums;
	bool	   *colNulls;
	int			ncols;
	Relation	rel;
	TupleDesc	tupdesc;
	AttrNumber *atts;
	int64		reclustered;
	int			i;

	if (PG_ARGISNULL(0))
		ereport(ERROR,
				(errcode(ERRCODE_NULL_VALUE_NOT_ALLOWED),
				 errmsg("table name cannot be null")));
	if (PG_ARGISNULL(1))
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("at least one clustering column is required")));

	colArray = PG_GETARG_ARRAYTYPE_P(1);
	deconstruct_array(colArray, NAMEOID, NAMEDATALEN, false, 'c',
					  &colDatums, &colNulls, &ncols);
	if (ncols < 1)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("at least one clustering column is required")));
	if (ncols > 8)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("Z-order clustering supports at most 8 columns")));

	PgColumnarRequireTableOwnerByOid(relid);

	/* the lazy lock: concurrent reads and writes during the recluster */
	rel = table_open(relid, ShareUpdateExclusiveLock);

	if (!PgColumnarIsColumnarRelation(relid))
	{
		table_close(rel, ShareUpdateExclusiveLock);
		ereport(ERROR,
				(errcode(ERRCODE_WRONG_OBJECT_TYPE),
				 errmsg("relation \"%s\" is not a columnar table",
						RelationGetRelationName(rel))));
	}

	tupdesc = RelationGetDescr(rel);
	atts = palloc(ncols * sizeof(AttrNumber));
	for (i = 0; i < ncols; i++)
	{
		char	   *colname;
		AttrNumber	attno;
		Form_pg_attribute att;

		if (colNulls[i])
		{
			table_close(rel, ShareUpdateExclusiveLock);
			ereport(ERROR,
					(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
					 errmsg("clustering column name cannot be null")));
		}
		colname = NameStr(*DatumGetName(colDatums[i]));
		attno = get_attnum(relid, colname);
		if (attno == InvalidAttrNumber || attno <= 0 ||
			TupleDescAttr(tupdesc, attno - 1)->attisdropped)
		{
			table_close(rel, ShareUpdateExclusiveLock);
			ereport(ERROR,
					(errcode(ERRCODE_UNDEFINED_COLUMN),
					 errmsg("column \"%s\" does not exist in table \"%s\"",
							colname, RelationGetRelationName(rel))));
		}
		att = TupleDescAttr(tupdesc, attno - 1);
		if (!cluster_type_supported(att->atttypid))
		{
			table_close(rel, ShareUpdateExclusiveLock);
			ereport(ERROR,
					(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
					 errmsg("column \"%s\" of type %s cannot be used as a clustering key",
							colname, format_type_be(att->atttypid)),
					 errhint("Z-order clustering supports integer, date/time, boolean, and floating-point columns. "
							 "For a text or other btree-orderable key, use pgcolumnar.vacuum_sorted() "
							 "(lexicographic sort on the given columns), optionally declared via set_options(..., sort_by => ...).")));
		}
		atts[i] = attno;
	}

	reclustered = pgcolumnar_recluster_online(rel, ncols, atts);

	table_close(rel, NoLock);
	PG_RETURN_INT64(reclustered);
}

/*
 * Dev/test fault injection (#415): hold the caller's lock for
 * pgcolumnar.maintenance_hold_ms, interruptibly. A query-cancel -- including the
 * one the lock manager sends when this process holds a lock blocking a stronger
 * request while carrying PROC_IS_AUTOVACUUM (the maintenance daemon's yield) --
 * fires at CHECK_FOR_INTERRUPTS and aborts the verb, which is what the yield test
 * asserts. Zero (default) is a no-op.
 */
static void
pgcolumnar_maintenance_hold(void)
{
	long		remaining = (long) pgcolumnar_maintenance_hold_ms;

	while (remaining > 0)
	{
		long		slice = Min(remaining, 1000L);
		int			rc;

		rc = WaitLatch(MyLatch, WL_LATCH_SET | WL_TIMEOUT | WL_EXIT_ON_PM_DEATH,
					   slice, PG_WAIT_EXTENSION);
		if (rc & WL_LATCH_SET)
			ResetLatch(MyLatch);
		CHECK_FOR_INTERRUPTS();
		remaining -= slice;
	}
}

Datum
pgcolumnar_compact_rewrite(PG_FUNCTION_ARGS)
{
	Oid			relid = PG_GETARG_OID(0);
	double		minFrac = PG_ARGISNULL(1) ? 0.2 : PG_GETARG_FLOAT8(1);
	int			maxGroups = PG_ARGISNULL(2) ? 0 : PG_GETARG_INT32(2);
	Relation	rel;
	int64		rewritten;

	if (PG_ARGISNULL(0))
		ereport(ERROR,
				(errcode(ERRCODE_NULL_VALUE_NOT_ALLOWED),
				 errmsg("table name cannot be null")));
	if (isnan(minFrac) || minFrac < 0.0 || minFrac > 1.0)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("min_deleted_fraction must be a number between 0 and 1")));

	PgColumnarRequireTableOwnerByOid(relid);

	rel = table_open(relid, ShareUpdateExclusiveLock);

	if (!PgColumnarIsColumnarRelation(relid))
	{
		table_close(rel, ShareUpdateExclusiveLock);
		ereport(ERROR,
				(errcode(ERRCODE_WRONG_OBJECT_TYPE),
				 errmsg("relation \"%s\" is not a columnar table",
						RelationGetRelationName(rel))));
	}

	/* dev/test: hold SUEL so the daemon's yield is observable (#415) */
	pgcolumnar_maintenance_hold();

	rewritten = pgcolumnar_rewrite_partial_groups(rel, minFrac, maxGroups);

	table_close(rel, NoLock);
	PG_RETURN_INT64(rewritten);
}

/* -------------------------------------------------------------------------
 * Z-order (Morton) clustering (Phase F2)
 *
 * Space-filling-curve clustering orders rows so that rows close in a
 * multi-column key space are close on disk, which tightens every clustered
 * column's per-vector min/max zone maps at once (unlike a single-column sort,
 * which only tightens the lead column). We map each clustered column value to
 * an order-preserving unsigned 64-bit ordinal, then bit-interleave the ordinals
 * most-significant-round-first into a byte string whose memcmp order is the
 * Z-order. Rows are rewritten sorted by that key (spec 9). Clean-room from the
 * public description of Morton/Z-order codes.
 * ------------------------------------------------------------------------- */

/*
 * Map a value of a supported clustering type to an order-preserving uint64:
 * a < b (in the type's default order) iff ordinal(a) < ordinal(b). Signed
 * integers flip the sign bit; floats flip the sign bit when positive and all
 * bits when negative (the standard radix-sort float transform). NULL is mapped
 * by the caller to 0 (sorts first).
 */
static uint64
cluster_type_ordinal(Datum value, Oid typid)
{
	switch (typid)
	{
		case BOOLOID:
			return DatumGetBool(value) ? 1 : 0;
		case INT2OID:
			return (uint64) ((int64) DatumGetInt16(value)) ^ UINT64CONST(0x8000000000000000);
		case INT4OID:
		case DATEOID:
			return (uint64) ((int64) DatumGetInt32(value)) ^ UINT64CONST(0x8000000000000000);
		case INT8OID:
		case TIMESTAMPOID:
		case TIMESTAMPTZOID:
			return (uint64) DatumGetInt64(value) ^ UINT64CONST(0x8000000000000000);
		case FLOAT8OID:
			{
				uint64		b;
				float8		f = DatumGetFloat8(value);

				memcpy(&b, &f, sizeof(b));
				return b ^ ((b >> 63) ? UINT64CONST(0xFFFFFFFFFFFFFFFF)
							: UINT64CONST(0x8000000000000000));
			}
		case FLOAT4OID:
			{
				uint32		b;
				float4		f = DatumGetFloat4(value);
				uint32		o;

				memcpy(&b, &f, sizeof(b));
				o = b ^ ((b >> 31) ? 0xFFFFFFFFu : 0x80000000u);
				return ((uint64) o) << 32;	/* keep the ordering in the high bits */
			}
		default:
			ereport(ERROR,
					(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
					 errmsg("column type %s cannot be used as a clustering key",
							format_type_be(typid)),
					 errhint("Z-order clustering supports integer, date/time, boolean, and floating-point columns.")));
			return 0;				/* keep the compiler happy */
	}
}

/* true when a type is usable as a Z-order clustering key */
static bool
cluster_type_supported(Oid typid)
{
	switch (typid)
	{
		case BOOLOID:
		case INT2OID:
		case INT4OID:
		case DATEOID:
		case INT8OID:
		case TIMESTAMPOID:
		case TIMESTAMPTZOID:
		case FLOAT8OID:
		case FLOAT4OID:
			return true;
		default:
			return false;
	}
}

/*
 * Build the Z-order key for one row: interleave the ncols column ordinals
 * MSB-first into an 8*ncols-byte string. Output bit stream is
 * ord[0].bit63, ord[1].bit63, ..., ord[n-1].bit63, ord[0].bit62, ... packed
 * MSB-first, so lexicographic (memcmp) order over the bytea equals Z-order.
 */
static bytea *
cluster_zorder_key(Datum *values, bool *isnull, AttrNumber *atts, int ncols,
				   TupleDesc tupdesc)
{
	int			keybytes = ncols * 8;
	bytea	   *result = (bytea *) palloc(VARHDRSZ + keybytes);
	unsigned char *out = (unsigned char *) VARDATA(result);
	uint64	   *ord = (uint64 *) palloc(ncols * sizeof(uint64));
	int			c;
	int			r;
	int			outbit = 0;

	SET_VARSIZE(result, VARHDRSZ + keybytes);
	memset(out, 0, keybytes);

	for (c = 0; c < ncols; c++)
	{
		AttrNumber	a = atts[c];
		Form_pg_attribute att = TupleDescAttr(tupdesc, a - 1);

		ord[c] = isnull[a - 1] ? 0
			: cluster_type_ordinal(values[a - 1], att->atttypid);
	}

	for (r = 63; r >= 0; r--)
	{
		for (c = 0; c < ncols; c++)
		{
			if ((ord[c] >> r) & 1)
				out[outbit >> 3] |= (unsigned char) (0x80 >> (outbit & 7));
			outbit++;
		}
	}

	pfree(ord);
	return result;
}

/*
 * pgcolumnar_relation_storageid
 *		SQL: columnar.get_storage_id(regclass) -> bigint. Reads the relation's
 *		metapage and returns its storage id (spec 3), so SQL-level functions
 *		such as columnar.stats can join the metadata catalog by storage id.
 */
Datum
pgcolumnar_relation_storageid(PG_FUNCTION_ARGS)
{
	Oid			relid = PG_GETARG_OID(0);
	Relation	rel;
	uint64		storageId;

	/*
	 * SELECT on the relation (#566). This returns an internal storage identifier
	 * of a caller-supplied relation and checked nothing, so USAGE on the schema
	 * was the whole boundary. The value is not otherwise reachable: a role with
	 * schema USAGE is refused pgcolumnar.storage directly. A reader who may
	 * SELECT the table may know its storage id; anyone else may not.
	 */
	{
		AclResult	ac = pg_class_aclcheck(relid, GetUserId(), ACL_SELECT);

		if (ac != ACLCHECK_OK)
			aclcheck_error(ac, OBJECT_TABLE, get_rel_name(relid));
	}

	rel = try_relation_open(relid, AccessShareLock);
	if (rel == NULL)
		PG_RETURN_NULL();

	if (!PgColumnarIsColumnarRelation(relid))
	{
		relation_close(rel, AccessShareLock);
		ereport(ERROR,
				(errcode(ERRCODE_WRONG_OBJECT_TYPE),
				 errmsg("relation \"%s\" is not a columnar table",
						RelationGetRelationName(rel))));
	}

	storageId = PgColumnarStorageId(rel);
	relation_close(rel, AccessShareLock);

	PG_RETURN_INT64((int64) storageId);
}

/*
 * sort_key_names
 *		The attribute names behind an ordering rewrite's key, as a list of
 *		palloc'd strings for pgcolumnar.storage.sorted_by (#415).
 *
 *		Names, not attnums, because the column is read back long after the rewrite
 *		and has to survive an ALTER that renumbers attributes, the same reason
 *		pgcolumnar.options.sort_by stores names (#288).
 */
static List *
sort_key_names(TupleDesc tupdesc, AttrNumber *atts, int ncols)
{
	List	   *names = NIL;
	int			i;

	for (i = 0; i < ncols; i++)
		names = lappend(names,
						pstrdup(NameStr(TupleDescAttr(tupdesc, atts[i] - 1)->attname)));
	return names;
}

/*
 * record_sorted_extent
 *		Mark where the ordered run this rewrite just wrote ends (issue #301), and
 *		what ordering it is (#415, #758).
 *
 *		Called only by the rewrites that order every live row, after the write
 *		state is flushed, so every group they wrote is in pgcolumnar.row_group and
 *		nothing has been appended yet. The highest group number present is
 *		therefore the end of the run, and later inserts get higher numbers.
 *
 *		sortByNames and sortedKind say WHICH ordering, and are not optional for a
 *		caller that applied one. Both eager rewrites used to pass NIL and NULL, so
 *		an eagerly sorted relation and an eagerly Z-ordered one were identical in
 *		the catalog: same run extent, same NULL kind, and sort_status fell back to
 *		reporting the DECLARED options.sort_by for both. Z-order over two or more
 *		columns is not a sort on any one of them, so that fallback reported an
 *		order the rows were not in. The base schema has always specified this
 *		column as 'zorder' (recluster/cluster) or 'lexicographic' (vacuum_sorted);
 *		only the online recluster wrote it.
 *
 *		A rewrite that produced no groups at all, meaning the relation held no
 *		live rows, leaves the mark alone. There is no run to record, and the
 *		reader reports no decay because there are no groups.
 */
static void
record_sorted_extent(Relation rel, List *sortByNames, const char *sortedKind)
{
	uint64		storageId = PgColumnarStorageId(rel);
	List	   *groups;
	uint64		lastGroup = 0;
	uint64		firstGroup = 0;
	bool		haveGroup = false;
	ListCell   *lc;

	groups = PgColumnarReadRowGroupList(storageId,
									  PgColumnarCatalogSnapshot(GetActiveSnapshot()));
	foreach(lc, groups)
	{
		NativeRowGroupMetadata *rg = (NativeRowGroupMetadata *) lfirst(lc);

		if (!haveGroup || rg->groupNumber > lastGroup)
			lastGroup = rg->groupNumber;
		if (!haveGroup || rg->groupNumber < firstGroup)
			firstGroup = rg->groupNumber;
		haveGroup = true;
	}
	list_free_deep(groups);

	if (haveGroup)
		PgColumnarSetSortedExtent(storageId, (int64) firstGroup, (int64) lastGroup,
								  sortByNames, sortedKind);
}

/*
 * pgcolumnar_compact_relation
 *		Rewrite every live row of a columnar relation into fresh stripes. The
 *		relation is already open with AccessExclusiveLock.
 *
 *		When nsortkeys == 0 the rows are rewritten in their existing order
 *		(plain compaction). When nsortkeys > 0 the live rows are first sorted
 *		ascending / NULLS LAST on the attributes in sortAtts[] (in order), so
 *		the table is stored physically sorted on that key (gap 26, piece 1).
 *		Sorting is a one-shot physical reorder; it is not persisted or
 *		auto-maintained, so rows inserted afterward append in insert order.
 */
static void
pgcolumnar_compact_relation(Relation rel, int nsortkeys, AttrNumber *sortAtts)
{
	Oid			relid = RelationGetRelid(rel);
	TupleDesc	tupdesc = RelationGetDescr(rel);
	uint64		oldStorageId;
	Snapshot	snapshot;
	PgColumnarReadState *readState;
	PgColumnarWriteState *writeState;
	Tuplestorestate *tstore = NULL;
	Tuplesortstate *tsort = NULL;
	TupleTableSlot *readSlot;
	TupleTableSlot *writeSlot;
	uint64		rowNumber;

	List	   *oldProjs;

	/* persist any pending work so the read below sees it (spec 9) */
	PgColumnarFlushWriteStateForRelation(relid);
	PgColumnarFlushDeleteVectorForRelation(rel);

	oldStorageId = PgColumnarStorageId(rel);

	/*
	 * Capture the table's projections (gap 26) before the storage swap. Compaction
	 * reassigns base row numbers, so each projection must be rebuilt from the
	 * compacted base; we re-record the definitions under the new storage id below
	 * and the rewrite loop re-fans-out every live row into them.
	 */
	oldProjs = PgColumnarListProjections(oldStorageId);

	/*
	 * Take the read snapshot AFTER the AccessExclusiveLock the caller already
	 * holds, not the caller's pre-lock statement snapshot (#295). A row group
	 * committed by another transaction while we waited for the lock is in that
	 * pre-lock snapshot's in-progress set, so it would be invisible to the row
	 * enumeration below and silently discarded by the relfilenode swap -- data
	 * loss. A fresh GetLatestSnapshot sees every commit as of now. Register it
	 * (not merely push active): PgColumnarBeginRead derives a PgColumnarCatalogSnapshot
	 * copy that must inherit a nonzero regd_count for PG18's heap-visibility
	 * assertion, exactly as the sibling rewrite/retire paths document.
	 */
	snapshot = RegisterSnapshot(GetLatestSnapshot());
	PushActiveSnapshot(snapshot);

	/*
	 * Read every live row (the reader skips row-mask-deleted rows) and
	 * materialize it, copying values out of the scan so they survive the
	 * storage swap below. A virtual slot receives the reader's values. Without a
	 * sort key the rows go into a tuplestore in read order; with a sort key they
	 * go into a tuplesort so they come back ordered. Either way a minimal-tuple
	 * slot reads them back.
	 */
	if (nsortkeys > 0)
	{
		Oid		   *sortOps = palloc(nsortkeys * sizeof(Oid));
		Oid		   *sortColls = palloc(nsortkeys * sizeof(Oid));
		bool	   *nullsFirst = palloc0(nsortkeys * sizeof(bool));
		int			i;

		for (i = 0; i < nsortkeys; i++)
		{
			Form_pg_attribute att = TupleDescAttr(tupdesc, sortAtts[i] - 1);
			TypeCacheEntry *tce = lookup_type_cache(att->atttypid,
													TYPECACHE_LT_OPR);

			if (!OidIsValid(tce->lt_opr))
				ereport(ERROR,
						(errcode(ERRCODE_UNDEFINED_FUNCTION),
						 errmsg("column \"%s\" of type %s has no default ordering operator",
								NameStr(att->attname),
								format_type_be(att->atttypid)),
						 errhint("Sorting requires a type with a default btree operator class.")));

			sortOps[i] = tce->lt_opr;
			sortColls[i] = att->attcollation;
			/* ascending, NULLS LAST (PostgreSQL's default for ASC) */
			nullsFirst[i] = false;
		}

		tsort = tuplesort_begin_heap(tupdesc, nsortkeys, sortAtts, sortOps,
									 sortColls, nullsFirst, maintenance_work_mem,
									 NULL, COLUMNAR_TUPLESORT_NONACCESS);
	}
	else
		tstore = tuplestore_begin_heap(false, false, work_mem);

	readSlot = MakeSingleTupleTableSlot(tupdesc, &TTSOpsVirtual);
	writeSlot = MakeSingleTupleTableSlot(tupdesc, &TTSOpsMinimalTuple);

	readState = PgColumnarBeginRead(rel, snapshot, NULL, NULL, 0, NULL);
	while (PgColumnarReadNextRow(readState, readSlot->tts_values,
							   readSlot->tts_isnull, &rowNumber))
	{
		CHECK_FOR_INTERRUPTS();
		ExecStoreVirtualTuple(readSlot);
		if (tsort != NULL)
			tuplesort_puttupleslot(tsort, readSlot);
		else
			tuplestore_puttupleslot(tstore, readSlot);
		ExecClearTuple(readSlot);
	}
	PgColumnarEndRead(readState);
	ExecDropSingleTupleTableSlot(readSlot);

	if (tsort != NULL)
		tuplesort_performsort(tsort);

	/*
	 * Swap to a brand-new relfilenode. This creates fresh, empty columnar
	 * storage (a new metapage with a new storage id) transactionally; the old
	 * storage is discarded at commit. Then forget the cached write state (it
	 * still points at the old storage id) and remove the old metadata rows.
	 */
	RelationSetNewRelfilenumber(rel, rel->rd_rel->relpersistence);
	PgColumnarForgetWriteStateForRelation(relid);
	PgColumnarDeleteMetadata(oldStorageId);

	/*
	 * Realign projections to the compacted base (gap 26): drop each old
	 * projection's storage and its now-stale catalog row (keyed by the old base
	 * storage id), then re-record the definitions under the new base storage id
	 * with fresh projection storage ids. The rewrite loop below re-fans-out every
	 * live row into them, so they end up sorted and aligned to the new row
	 * numbers. No-op for a table without projections.
	 */
	if (oldProjs != NIL)
	{
		uint64		newStorageId = PgColumnarStorageId(rel);
		ListCell   *lc;

		foreach(lc, oldProjs)
		{
			PgColumnarProjection *p = (PgColumnarProjection *) lfirst(lc);

			if (p->projStorageId != oldStorageId)
				PgColumnarDeleteMetadata(p->projStorageId);
			PgColumnarDeleteProjectionRow(oldStorageId, p->projectionId);
		}
		foreach(lc, oldProjs)
		{
			PgColumnarProjection *p = (PgColumnarProjection *) lfirst(lc);
			PgColumnarProjection np = *p;

			np.storageId = newStorageId;
			np.projStorageId = (p->projectionId == 0) ? newStorageId
				: PgColumnarNextStorageId();
			PgColumnarInsertProjectionRow(&np);
		}
	}

	/* write the live rows back into the fresh storage, in sorted order if any */
	writeState = PgColumnarGetWriteState(rel);
	if (tsort != NULL)
	{
		while (tuplesort_gettupleslot(tsort, true, false, writeSlot, NULL))
		{
			uint64		newRowNumber;

			CHECK_FOR_INTERRUPTS();
			slot_getallattrs(writeSlot);
			newRowNumber = PgColumnarWriteRow(writeState, rel, writeSlot->tts_values,
											writeSlot->tts_isnull);
			PgColumnarProjectionFanoutRow(rel, writeState, newRowNumber,
										writeSlot->tts_values, writeSlot->tts_isnull);
			ExecClearTuple(writeSlot);
		}
	}
	else
	{
		while (tuplestore_gettupleslot(tstore, true, false, writeSlot))
		{
			uint64		newRowNumber;

			CHECK_FOR_INTERRUPTS();
			slot_getallattrs(writeSlot);
			newRowNumber = PgColumnarWriteRow(writeState, rel, writeSlot->tts_values,
											writeSlot->tts_isnull);
			PgColumnarProjectionFanoutRow(rel, writeState, newRowNumber,
										writeSlot->tts_values, writeSlot->tts_isnull);
			ExecClearTuple(writeSlot);
		}
	}
	PgColumnarFlushWriteStateForRelation(relid);

	/*
	 * A sorted rewrite leaves the whole relation ordered, so record its extent.
	 * An unsorted one does not: the new storage row's zero already says the
	 * layout is unsorted, and overwriting it would claim an order that is not
	 * there.
	 */
	if (tsort != NULL)
		record_sorted_extent(rel, sort_key_names(tupdesc, sortAtts, nsortkeys),
							 "lexicographic");

	if (tsort != NULL)
		tuplesort_end(tsort);
	else
		tuplestore_end(tstore);
	ExecDropSingleTupleTableSlot(writeSlot);

	/*
	 * Rewrite assigned fresh row numbers, so rebuild the indexes to repoint
	 * their synthetic item pointers (spec 6). A relation with no indexes is a
	 * no-op here.
	 */
	PgColumnarReindexRelation(relid, REINDEX_REL_PROCESS_TOAST);

	PopActiveSnapshot();
	UnregisterSnapshot(snapshot);
}

/*
 * pgcolumnar_compact_relation_zorder
 *		Rewrite every live row of a columnar relation ordered by the Z-order
 *		(Morton) code over atts[0..ncols-1] (Phase F2). Mirrors
 *		pgcolumnar_compact_relation, but sorts by a computed key carried as a
 *		trailing bytea column of an augmented tuple, so the sort still spills to
 *		disk through tuplesort. The relation is already open AccessExclusiveLock.
 */
static void
pgcolumnar_compact_relation_zorder(Relation rel, int ncols, AttrNumber *atts)
{
	Oid			relid = RelationGetRelid(rel);
	TupleDesc	tupdesc = RelationGetDescr(rel);
	int			natts = tupdesc->natts;
	AttrNumber	zAtt = (AttrNumber) (natts + 1);
	uint64		oldStorageId;
	Snapshot	snapshot;
	PgColumnarReadState *readState;
	PgColumnarWriteState *writeState;
	Tuplesortstate *tsort;
	TupleDesc	augdesc;
	TupleTableSlot *readSlot;
	TupleTableSlot *putSlot;
	TupleTableSlot *augSlot;
	TypeCacheEntry *tce;
	Oid			byteaLt;
	Oid			sortColl = InvalidOid;
	bool		nullsFirst = false;
	uint64		rowNumber;
	List	   *oldProjs;
	int			i;

	/* persist pending work so the read below sees it (spec 9) */
	PgColumnarFlushWriteStateForRelation(relid);
	PgColumnarFlushDeleteVectorForRelation(rel);

	oldStorageId = PgColumnarStorageId(rel);
	oldProjs = PgColumnarListProjections(oldStorageId);
	/* fresh snapshot AFTER the AccessExclusiveLock, not the caller's pre-lock one,
	 * so a row group committed during the lock wait is copied rather than dropped
	 * by the swap (#295); registered for PG18's catalog-snapshot regd_count. Same
	 * reasoning as pgcolumnar_compact_relation. */
	snapshot = RegisterSnapshot(GetLatestSnapshot());
	PushActiveSnapshot(snapshot);

	/* augmented descriptor: the table's columns plus a trailing bytea Z-order key */
	augdesc = CreateTemplateTupleDesc(natts + 1);
	for (i = 1; i <= natts; i++)
		TupleDescCopyEntry(augdesc, (AttrNumber) i, tupdesc, (AttrNumber) i);
	TupleDescInitEntry(augdesc, zAtt, "__zorder", BYTEAOID, -1, 0);
#if PG_VERSION_NUM >= 190000
	/* PG19 requires a manually-built TupleDesc to be finalized before use, which
	 * computes firstNonCachedOffsetAttr (asserted by the tuple routines) after the
	 * TupleDescCopyEntry calls filled the FormData array directly. */
	TupleDescFinalize(augdesc);
#endif

	tce = lookup_type_cache(BYTEAOID, TYPECACHE_LT_OPR);
	byteaLt = tce->lt_opr;
	tsort = tuplesort_begin_heap(augdesc, 1, &zAtt, &byteaLt, &sortColl,
								 &nullsFirst, maintenance_work_mem, NULL,
								 COLUMNAR_TUPLESORT_NONACCESS);

	readSlot = MakeSingleTupleTableSlot(tupdesc, &TTSOpsVirtual);
	putSlot = MakeSingleTupleTableSlot(augdesc, &TTSOpsVirtual);
	augSlot = MakeSingleTupleTableSlot(augdesc, &TTSOpsMinimalTuple);

	readState = PgColumnarBeginRead(rel, snapshot, NULL, NULL, 0, NULL);
	while (PgColumnarReadNextRow(readState, readSlot->tts_values,
							   readSlot->tts_isnull, &rowNumber))
	{
		bytea	   *zkey;

		CHECK_FOR_INTERRUPTS();
		memcpy(putSlot->tts_values, readSlot->tts_values, natts * sizeof(Datum));
		memcpy(putSlot->tts_isnull, readSlot->tts_isnull, natts * sizeof(bool));
		zkey = cluster_zorder_key(readSlot->tts_values, readSlot->tts_isnull,
								  atts, ncols, tupdesc);
		putSlot->tts_values[natts] = PointerGetDatum(zkey);
		putSlot->tts_isnull[natts] = false;
		ExecStoreVirtualTuple(putSlot);
		tuplesort_puttupleslot(tsort, putSlot);
		ExecClearTuple(putSlot);
	}
	PgColumnarEndRead(readState);
	ExecDropSingleTupleTableSlot(readSlot);
	ExecDropSingleTupleTableSlot(putSlot);
	tuplesort_performsort(tsort);

	/* swap to fresh storage and drop old metadata (as in compact_relation) */
	RelationSetNewRelfilenumber(rel, rel->rd_rel->relpersistence);
	PgColumnarForgetWriteStateForRelation(relid);
	PgColumnarDeleteMetadata(oldStorageId);

	/* realign projections to the compacted base (as in compact_relation) */
	if (oldProjs != NIL)
	{
		uint64		newStorageId = PgColumnarStorageId(rel);
		ListCell   *lc;

		foreach(lc, oldProjs)
		{
			PgColumnarProjection *p = (PgColumnarProjection *) lfirst(lc);

			if (p->projStorageId != oldStorageId)
				PgColumnarDeleteMetadata(p->projStorageId);
			PgColumnarDeleteProjectionRow(oldStorageId, p->projectionId);
		}
		foreach(lc, oldProjs)
		{
			PgColumnarProjection *p = (PgColumnarProjection *) lfirst(lc);
			PgColumnarProjection np = *p;

			np.storageId = newStorageId;
			np.projStorageId = (p->projectionId == 0) ? newStorageId
				: PgColumnarNextStorageId();
			PgColumnarInsertProjectionRow(&np);
		}
	}

	/* write the live rows back in Z-order; the trailing key column is ignored */
	writeState = PgColumnarGetWriteState(rel);
	while (tuplesort_gettupleslot(tsort, true, false, augSlot, NULL))
	{
		uint64		newRowNumber;

		CHECK_FOR_INTERRUPTS();
		slot_getallattrs(augSlot);
		newRowNumber = PgColumnarWriteRow(writeState, rel, augSlot->tts_values,
										augSlot->tts_isnull);
		PgColumnarProjectionFanoutRow(rel, writeState, newRowNumber,
									augSlot->tts_values, augSlot->tts_isnull);
		ExecClearTuple(augSlot);
	}
	PgColumnarFlushWriteStateForRelation(relid);

	/* Z-order is an order, so the same extent applies (see record_sorted_extent). */
	record_sorted_extent(rel, sort_key_names(tupdesc, atts, ncols), "zorder");

	tuplesort_end(tsort);
	ExecDropSingleTupleTableSlot(augSlot);

	PgColumnarReindexRelation(relid, REINDEX_REL_PROCESS_TOAST);

	PopActiveSnapshot();
	UnregisterSnapshot(snapshot);
}

/*
 * pgcolumnar_vacuum
 *		SQL: columnar.vacuum(tablename regclass, stripe_count int default 0).
 *		Compacts a columnar table by combining its stripes and reclaiming the
 *		space of deleted rows (spec 8.2, 9). stripe_count is accepted for
 *		interface compatibility; this implementation always rewrites the whole
 *		relation into full stripes, which is the strongest form of the "combine
 *		recent stripes" contract.
 */
Datum
pgcolumnar_vacuum(PG_FUNCTION_ARGS)
{
	Oid			relid = PG_GETARG_OID(0);
	Relation	rel;

	/*
	 * stripe_count is refused rather than honoured (#560).
	 *
	 * It was documented as bounding how many row groups are combined in one
	 * call, and it has never been read: this function fetched argument 0 and
	 * passed a literal 0 as the bound. A caller asking for a bound of 4 got an
	 * unbounded whole-relation rewrite and no indication of it.
	 *
	 * Honouring it is not a small change and must not be attempted here.
	 * pgcolumnar_compact_relation's second parameter is nsortkeys, not a bound,
	 * and its body dereferences sortAtts[i]. More seriously it swaps the whole
	 * relation to a NEW relfilenode, so rewriting only some row groups would
	 * leave the rest behind in storage that is then discarded. Bounded
	 * compaction is a different algorithm, and one already exists:
	 * pgcolumnar.compact_rewrite passes its max_groups through to
	 * pgcolumnar_rewrite_partial_groups.
	 *
	 * So refuse, and name the thing that works. An accepted-and-ignored
	 * argument is a documented promise the code does not keep; an error is the
	 * only answer that is both truthful and non-breaking for the default form.
	 */
	if (!PG_ARGISNULL(1) && PG_GETARG_INT32(1) != 0)
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("pgcolumnar.vacuum does not support a stripe_count bound"),
				 errdetail("The argument was accepted and ignored in earlier "
						   "releases: every call rewrote the whole relation."),
				 errhint("Use pgcolumnar.compact_rewrite(rel, min_deleted_fraction, "
						 "max_groups), which bounds the number of row groups it "
						 "rewrites, or omit stripe_count to rewrite the whole "
						 "relation.")));

	/* Ownership before the AccessExclusiveLock, so a non-owner cannot queue in
	 * the lock FIFO and block readers of a table it does not own (#568). */
	PgColumnarRequireTableOwnerByOid(relid);

	rel = table_open(relid, AccessExclusiveLock);

	if (!PgColumnarIsColumnarRelation(relid))
	{
		table_close(rel, AccessExclusiveLock);
		ereport(ERROR,
				(errcode(ERRCODE_WRONG_OBJECT_TYPE),
				 errmsg("relation \"%s\" is not a columnar table",
						RelationGetRelationName(rel))));
	}

	pgcolumnar_compact_relation(rel, 0, NULL);

	/* keep the lock until end of transaction */
	table_close(rel, NoLock);

	PG_RETURN_VOID();
}

/*
 * vacuum_sorted_gate_is_noop
 *		Would pgcolumnar.vacuum_sorted(rel, atts...) change anything?
 *
 *		#415 gave the online recluster a self-gate so a scheduler can call it
 *		speculatively without paying a full rewrite each time;
 *		design/ISSUE_415_AUTOVACUUM.md promises the mirror here, and #760 says
 *		why the mirror cannot be a copy.
 *
 *		vacuum_sorted has TWO jobs, not one: it orders the live rows AND it
 *		physically reclaims deleted-row space and combines small stripes
 *		(docs/ARCHITECTURE.md). recluster only orders, so "is it already in
 *		this order" is a complete skip condition there and is NOT one here --
 *		it would answer yes on a relation that is in order and full of dead
 *		rows, and silently stop reclaiming. That is a data-size regression
 *		with no error and no report, which is worse than the rewrite it saves.
 *
 *		So the gate is "already in this order AND there is nothing to reclaim":
 *
 *		  1. the recorded run is lexicographic (a zorder run over the same
 *		     columns is NOT this order -- Z-order over two or more columns is
 *		     not a sort on any one of them);
 *		  2. the recorded key is exactly these columns, in this order;
 *		  3. every row group lies inside [sorted_from, sorted_through], so no
 *		     group was appended past the run or drawn by a concurrent writer
 *		     below it. This also covers stripe-combining: a group inside the
 *		     run was produced BY the rewrite that set the mark, so it is
 *		     already combined;
 *		  4. no group carries a deleted row, and none is empty. This is the
 *		     half recluster does not need and the reason #760 exists.
 *
 *		Any mismatch falls through to the full rewrite, so re-sorting by a new
 *		key, re-sorting a Z-ordered table, and reclaiming all still work. A
 *		mark written before sorted_from/sorted_kind existed leaves them unset
 *		and never gates, which is the safe pre-#415 default.
 */
static bool
vacuum_sorted_gate_is_noop(Relation rel, int ncols, AttrNumber *atts)
{
	uint64		storageId = PgColumnarStorageId(rel);
	TupleDesc	tupdesc = RelationGetDescr(rel);
	int64		sfrom,
				sthrough;
	List	   *skey;
	char	   *skind;
	Snapshot	snap;
	List	   *rgList;
	ListCell   *lc;
	int			i;
	bool		noop = true;

	PgColumnarGetSortedInfo(storageId, &sfrom, &sthrough, &skey, &skind);

	/* 1 + 2: a lexicographic run over exactly this key */
	if (skind == NULL || strcmp(skind, "lexicographic") != 0)
		return false;
	if (list_length(skey) != ncols)
		return false;
	i = 0;
	foreach(lc, skey)
	{
		const char *want = NameStr(TupleDescAttr(tupdesc, atts[i] - 1)->attname);

		if (strcmp((char *) lfirst(lc), want) != 0)
			return false;
		i++;
	}
	if (sfrom < 0 || sthrough < 0)
		return false;

	/*
	 * Persist own pending work before reading the group list and the delete
	 * vectors, as pgcolumnar_compact_relation does on the fall-through path.
	 * Both flushes are idempotent.
	 *
	 * Stated honestly: this is defensive, not a fix for an observed hole, and
	 * no arm in test/vacuum_sorted_gate.sh goes red without it. The write
	 * state and the delete vector are both flushed at end of statement, so by
	 * the time a later vacuum_sorted() runs, everything an earlier statement
	 * in the same transaction wrote is already in row_group (measured: a
	 * 500-row insert below chunk_group_row_limit shows storedrows=20500
	 * immediately, inside the transaction).
	 *
	 * It stays because the gate SKIPS A REWRITE on what it reads, so reading
	 * stale state is a silent correctness bug, and the invariant that makes
	 * the flush unnecessary belongs to another module. The transaction arms in
	 * that suite pin the invariant rather than this flush: if end-of-statement
	 * flushing ever stops holding, they go red and this becomes load-bearing.
	 */
	PgColumnarFlushWriteStateForRelation(RelationGetRelid(rel));
	PgColumnarFlushDeleteVectorForRelation(rel);

	snap = RegisterSnapshot(GetLatestSnapshot());
	rgList = PgColumnarReadRowGroupList(storageId, snap);
	if (rgList == NIL)
		noop = false;			/* nothing written: let the ordinary path run */
	foreach(lc, rgList)
	{
		NativeRowGroupMetadata *rg = (NativeRowGroupMetadata *) lfirst(lc);
		List	   *rmList;
		ListCell   *lc2;

		/* 3: inside the recorded run */
		if ((int64) rg->groupNumber < sfrom || (int64) rg->groupNumber > sthrough)
		{
			noop = false;
			break;
		}

		/* 4: an empty group is space a rewrite would drop, so it is work */
		if (rg->rowCount == 0)
		{
			noop = false;
			break;
		}
		rmList = PgColumnarReadDeleteVectorList(storageId, rg->groupNumber, snap);
		foreach(lc2, rmList)
		{
			if (((DeleteVectorMetadata *) lfirst(lc2))->deletedCount > 0)
			{
				noop = false;
				break;
			}
		}
		if (!noop)
			break;
	}
	UnregisterSnapshot(snap);

	return noop;
}

/*
 * pgcolumnar_vacuum_sorted
 *		SQL: columnar.vacuum_sorted(tablename regclass, VARIADIC sort_columns name[]).
 *		Like columnar.vacuum, but rewrites the live rows physically sorted
 *		ascending / NULLS LAST on the named columns, in order (gap 26, piece 1).
 *		Storing the table sorted on a key tightens per-chunk-group min/max ranges
 *		and helps the RLE/DELTA encodings on that key, so range predicates and
 *		ordered scans skip far more chunk groups. Results are unchanged; this only
 *		reorders physical storage. It is a one-shot reorder (not auto-maintained).
 *
 *		With no explicit columns (vacuum_sorted('t')) it applies the table's
 *		declared sort_by key from pgcolumnar.options (#288), like a bare
 *		"CLUSTER t" re-applying a remembered index; it errors if none is set.
 *		Sorting supports any btree-orderable column, text included; the Z-order
 *		cluster() path takes integer, date/time, boolean and floating-point
 *		columns.
 */
Datum
pgcolumnar_vacuum_sorted(PG_FUNCTION_ARGS)
{
	Oid			relid;
	Relation	rel;
	TupleDesc	tupdesc;
	List	   *colNames = NIL;
	bool		fromPersisted = false;
	AttrNumber *sortAtts;
	int			ncols;
	int			i;
	ListCell   *lc;

	if (PG_ARGISNULL(0))
		ereport(ERROR,
				(errcode(ERRCODE_NULL_VALUE_NOT_ALLOWED),
				 errmsg("table name cannot be null")));
	relid = PG_GETARG_OID(0);

	/* Ownership before the AccessExclusiveLock (#568), as in pgcolumnar_vacuum. */
	PgColumnarRequireTableOwnerByOid(relid);

	rel = table_open(relid, AccessExclusiveLock);

	if (!PgColumnarIsColumnarRelation(relid))
	{
		table_close(rel, AccessExclusiveLock);
		ereport(ERROR,
				(errcode(ERRCODE_WRONG_OBJECT_TYPE),
				 errmsg("relation \"%s\" is not a columnar table",
						RelationGetRelationName(rel))));
	}

	/*
	 * Collect the sort-column names. Explicit columns win; when none are given
	 * (vacuum_sorted('t'), or an empty/NULL VARIADIC array) fall back to the
	 * sort_by key declared with set_options (#288). Both sources feed one
	 * resolution + validation loop below so the explicit and declared paths
	 * cannot diverge. Mirrors bare "CLUSTER t" re-applying a remembered index.
	 */
	if (PG_NARGS() >= 2 && !PG_ARGISNULL(1))
	{
		ArrayType  *colArray = PG_GETARG_ARRAYTYPE_P(1);
		Datum	   *colDatums;
		bool	   *colNulls;
		int			n;

		deconstruct_array(colArray, NAMEOID, NAMEDATALEN, false, 'c',
						  &colDatums, &colNulls, &n);
		for (i = 0; i < n; i++)
		{
			if (colNulls[i])
			{
				table_close(rel, AccessExclusiveLock);
				ereport(ERROR,
						(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
						 errmsg("sort column name cannot be null")));
			}
			colNames = lappend(colNames,
							   pstrdup(NameStr(*DatumGetName(colDatums[i]))));
		}
	}

	if (colNames == NIL)
	{
		colNames = PgColumnarReadSortBy(relid);
		fromPersisted = true;
	}

	if (colNames == NIL)
	{
		table_close(rel, AccessExclusiveLock);
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("no sort columns given and table \"%s\" has no declared sort_by key",
						RelationGetRelationName(rel))),
				errhint("Pass the sort columns explicitly, or declare them with "
						"pgcolumnar.set_options(..., sort_by => ARRAY[...])."));
	}

	tupdesc = RelationGetDescr(rel);
	ncols = list_length(colNames);
	sortAtts = palloc(ncols * sizeof(AttrNumber));

	i = 0;
	foreach(lc, colNames)
	{
		char	   *colname = (char *) lfirst(lc);
		AttrNumber	attno = get_attnum(relid, colname);
		Form_pg_attribute att;

		if (attno == InvalidAttrNumber || attno <= 0 ||
			TupleDescAttr(tupdesc, attno - 1)->attisdropped)
		{
			table_close(rel, AccessExclusiveLock);
			if (fromPersisted)
				ereport(ERROR,
						(errcode(ERRCODE_UNDEFINED_COLUMN),
						 errmsg("declared sort_by column \"%s\" does not exist in table \"%s\"",
								colname, RelationGetRelationName(rel)),
						 errhint("A column named in sort_by was dropped or renamed. Update it with "
								 "pgcolumnar.set_options(..., sort_by => ARRAY[...]) or clear it with "
								 "pgcolumnar.reset_options(..., sort_by => true).")));
			else
				ereport(ERROR,
						(errcode(ERRCODE_UNDEFINED_COLUMN),
						 errmsg("column \"%s\" does not exist in table \"%s\"",
								colname, RelationGetRelationName(rel))));
		}

		att = TupleDescAttr(tupdesc, attno - 1);

		/*
		 * A virtual generated column ('v', PG18+) has no stored value, so rows
		 * cannot be ordered by it. The literal is inert on PG < 18, where
		 * attgenerated is only '\0' or 's'. Stored generated columns are fine.
		 */
		if (att->attgenerated == 'v')
		{
			table_close(rel, AccessExclusiveLock);
			ereport(ERROR,
					(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
					 errmsg("column \"%s\" is a virtual generated column and cannot be a sort key",
							colname),
					 errhint("Virtual generated columns are not stored, so rows cannot be ordered by them.")));
		}

		sortAtts[i++] = attno;
	}

	/*
	 * Self-gate (#760): skip the rewrite when the relation is already exactly
	 * this lexicographic run with nothing appended and nothing to reclaim. See
	 * vacuum_sorted_gate_is_noop for why the reclaim half is not optional.
	 */
	if (vacuum_sorted_gate_is_noop(rel, ncols, sortAtts))
		ereport(DEBUG1,
				(errmsg("pgcolumnar: \"%s\" is already sorted on this key with nothing to reclaim, skipping rewrite",
						RelationGetRelationName(rel))));
	else
		pgcolumnar_compact_relation(rel, ncols, sortAtts);

	/* keep the lock until end of transaction */
	table_close(rel, NoLock);

	PG_RETURN_VOID();
}

/*
 * pgcolumnar_cluster
 *		SQL: pgcolumnar.cluster(tablename regclass, VARIADIC columns name[]).
 *		Physically reorders a columnar table by the Z-order (Morton) space-filling
 *		curve over the named columns (Phase F2, spec 9). Unlike vacuum_sorted's
 *		single lead-column sort, Z-order clustering tightens the min/max zone maps
 *		of ALL clustered columns at once, so multi-column range and point
 *		predicates skip far more vectors and chunks. Results are unchanged; this
 *		only reorders physical storage.
 *
 *		This is the EAGER / offline reorg: it rewrites the whole relation and
 *		swaps the relfilenode, so it holds AccessExclusiveLock for the duration
 *		(like PostgreSQL's own CLUSTER / VACUUM FULL) and blocks concurrent reads
 *		and writes. Use it for an initial bulk reorg. The routine, online path that
 *		reclusters incrementally under ShareUpdateExclusiveLock (concurrent reads
 *		and writes allowed) is Phase F3; every maintenance op must offer that lazy
 *		path.
 */
Datum
pgcolumnar_cluster(PG_FUNCTION_ARGS)
{
	Oid			relid = PG_GETARG_OID(0);
	ArrayType  *colArray;
	Datum	   *colDatums;
	bool	   *colNulls;
	int			ncols;
	Relation	rel;
	TupleDesc	tupdesc;
	AttrNumber *atts;
	int			i;

	if (PG_ARGISNULL(0))
		ereport(ERROR,
				(errcode(ERRCODE_NULL_VALUE_NOT_ALLOWED),
				 errmsg("table name cannot be null")));
	if (PG_ARGISNULL(1))
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("at least one clustering column is required")));

	colArray = PG_GETARG_ARRAYTYPE_P(1);
	deconstruct_array(colArray, NAMEOID, NAMEDATALEN, false, 'c',
					  &colDatums, &colNulls, &ncols);
	if (ncols < 1)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("at least one clustering column is required")));
	if (ncols > 8)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("Z-order clustering supports at most 8 columns")));

	/* Ownership before the AccessExclusiveLock (#568), as in pgcolumnar_vacuum. */
	PgColumnarRequireTableOwnerByOid(relid);

	rel = table_open(relid, AccessExclusiveLock);

	if (!PgColumnarIsColumnarRelation(relid))
	{
		table_close(rel, AccessExclusiveLock);
		ereport(ERROR,
				(errcode(ERRCODE_WRONG_OBJECT_TYPE),
				 errmsg("relation \"%s\" is not a columnar table",
						RelationGetRelationName(rel))));
	}

	tupdesc = RelationGetDescr(rel);
	atts = palloc(ncols * sizeof(AttrNumber));

	for (i = 0; i < ncols; i++)
	{
		char	   *colname;
		AttrNumber	attno;
		Form_pg_attribute att;

		if (colNulls[i])
		{
			table_close(rel, AccessExclusiveLock);
			ereport(ERROR,
					(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
					 errmsg("clustering column name cannot be null")));
		}

		colname = NameStr(*DatumGetName(colDatums[i]));
		attno = get_attnum(relid, colname);
		if (attno == InvalidAttrNumber || attno <= 0 ||
			TupleDescAttr(tupdesc, attno - 1)->attisdropped)
		{
			table_close(rel, AccessExclusiveLock);
			ereport(ERROR,
					(errcode(ERRCODE_UNDEFINED_COLUMN),
					 errmsg("column \"%s\" does not exist in table \"%s\"",
							colname, RelationGetRelationName(rel))));
		}

		att = TupleDescAttr(tupdesc, attno - 1);
		if (!cluster_type_supported(att->atttypid))
		{
			table_close(rel, AccessExclusiveLock);
			ereport(ERROR,
					(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
					 errmsg("column \"%s\" of type %s cannot be used as a clustering key",
							colname, format_type_be(att->atttypid)),
					 errhint("Z-order clustering supports integer, date/time, boolean, and floating-point columns. "
							 "For a text or other btree-orderable key, use pgcolumnar.vacuum_sorted() "
							 "(lexicographic sort on the given columns), optionally declared via set_options(..., sort_by => ...).")));
		}
		atts[i] = attno;
	}

	pgcolumnar_compact_relation_zorder(rel, ncols, atts);

	/* keep the lock until end of transaction */
	table_close(rel, NoLock);

	PG_RETURN_VOID();
}

/*
 * pgcolumnar_compact
 *		SQL: pgcolumnar.compact(tablename regclass) -> bigint. The LAZY / online
 *		maintenance path (Phase F3a): retire every row group that is fully deleted
 *		as-of the oldest-xmin horizon, dropping its catalog rows so scans no longer
 *		read it. Runs under ShareUpdateExclusiveLock, so it is concurrent with
 *		readers and writers -- unlike vacuum / cluster, it never takes
 *		AccessExclusiveLock. Returns the number of groups retired. Physical page
 *		reclaim of retired groups is deferred; run the eager vacuum to reclaim the
 *		file, or a later F3 pass. Rewriting partially-deleted groups online is
 *		Phase F3b.
 */
/*
 * pgcolumnar_expire
 *		Drop every row group whose rows are ALL older than the retention declared
 *		by set_options(ttl_column, ttl_interval) (#403 item 5a). Returns the
 *		number of groups dropped.
 *
 *		The decision is a catalog read. A group's zone map records the maximum
 *		value of each column in it, so a group whose maximum is below the cutoff
 *		holds no row that is still within the retention. Nothing is decoded and
 *		nothing is rewritten; the group's catalog rows are retired exactly as
 *		pgcolumnar.compact retires a fully deleted one, under the same
 *		ShareUpdateExclusiveLock, so readers and writers continue throughout.
 *
 *		A group that STRADDLES the cutoff is kept whole. Its expired rows survive
 *		until every row in the group has expired. That is the price of deciding
 *		by group rather than by row, and it is the safe direction: the
 *		alternative drops rows that are still inside the retention.
 *
 *		A NULL in the retention column is the same kind of straddle. The zone
 *		map's maximum covers only the non-NULL timestamps, so a group whose
 *		timestamps are all past the cutoff can still hold rows whose retention
 *		is unknown. Dropping it would delete those rows.
 *
 *		Retiring a live group also clears the visibility-map bits covering its
 *		row numbers. VACUUM may already have marked the group all-visible, and
 *		an index-only scan would then return the expired keys from the index
 *		without fetching. A fetch would correctly fail once the catalog rows
 *		are gone; skipping the fetch is what made the ghosts visible.
 *
 *		This is called by name and never runs on its own. It deletes rows, and an
 *		operation a user runs for maintenance must not do that silently, so it is
 *		not wired into vacuum, compact or autovacuum.
 */
Datum
pgcolumnar_expire(PG_FUNCTION_ARGS)
{
	Oid			relid = PG_GETARG_OID(0);
	Relation	rel;
	TupleDesc	tupdesc;
	char	   *ttlColumn;
	Interval   *ttlInterval;
	AttrNumber	attno = InvalidAttrNumber;
	Form_pg_attribute att;
	Datum		cutoff;
	TypeCacheEntry *tce;
	uint64		storageId;
	List	   *groups;
	ListCell   *lc;
	int64		retired = 0;
	int			i;

	PgColumnarRequireTableOwnerByOid(relid);

	if (!PgColumnarReadTtl(relid, &ttlColumn, &ttlInterval))
		ereport(ERROR,
				(errcode(ERRCODE_OBJECT_NOT_IN_PREREQUISITE_STATE),
				 errmsg("table \"%s\" has no declared retention",
						get_rel_name(relid)),
				 errhint("Declare one with pgcolumnar.set_options(..., ttl_column => ..., ttl_interval => ...).")));

	/* the lazy lock, as compact takes: readers and writers continue */
	rel = table_open(relid, ShareUpdateExclusiveLock);

	if (!PgColumnarIsColumnarRelation(relid))
	{
		table_close(rel, ShareUpdateExclusiveLock);
		ereport(ERROR,
				(errcode(ERRCODE_WRONG_OBJECT_TYPE),
				 errmsg("relation \"%s\" is not a columnar table",
						RelationGetRelationName(rel))));
	}

	tupdesc = RelationGetDescr(rel);
	for (i = 0; i < tupdesc->natts; i++)
	{
		Form_pg_attribute a = TupleDescAttr(tupdesc, i);

		if (!a->attisdropped && strcmp(NameStr(a->attname), ttlColumn) == 0)
		{
			attno = a->attnum;
			break;
		}
	}
	if (attno == InvalidAttrNumber)
	{
		table_close(rel, ShareUpdateExclusiveLock);
		ereport(ERROR,
				(errcode(ERRCODE_UNDEFINED_COLUMN),
				 errmsg("retention column \"%s\" does not exist in table \"%s\"",
						ttlColumn, get_rel_name(relid))));
	}

	att = TupleDescAttr(tupdesc, attno - 1);

	/*
	 * The cutoff is computed in the column's own type, so the comparison below
	 * is the type's own ordering rather than a coercion invented here.
	 */
	switch (att->atttypid)
	{
		case TIMESTAMPTZOID:
			cutoff = DirectFunctionCall2(timestamptz_mi_interval,
										 TimestampTzGetDatum(GetCurrentTimestamp()),
										 IntervalPGetDatum(ttlInterval));
			break;
		case TIMESTAMPOID:
			cutoff = DirectFunctionCall2(timestamp_mi_interval,
										 DirectFunctionCall1(timestamptz_timestamp,
															 TimestampTzGetDatum(GetCurrentTimestamp())),
										 IntervalPGetDatum(ttlInterval));
			break;
		default:
			table_close(rel, ShareUpdateExclusiveLock);
			ereport(ERROR,
					(errcode(ERRCODE_DATATYPE_MISMATCH),
					 errmsg("retention column \"%s\" must be timestamp or timestamptz",
							ttlColumn)));
			cutoff = (Datum) 0;	/* keep the compiler quiet */
			break;
	}

	tce = lookup_type_cache(att->atttypid, TYPECACHE_CMP_PROC_FINFO);
	if (!OidIsValid(tce->cmp_proc_finfo.fn_oid))
	{
		table_close(rel, ShareUpdateExclusiveLock);
		ereport(ERROR,
				(errcode(ERRCODE_UNDEFINED_FUNCTION),
				 errmsg("retention column \"%s\" has no ordering to compare against",
						ttlColumn)));
	}

	storageId = PgColumnarStorageId(rel);
	groups = PgColumnarReadRowGroupList(storageId, GetActiveSnapshot());

	foreach(lc, groups)
	{
		NativeRowGroupMetadata *rg = (NativeRowGroupMetadata *) lfirst(lc);
		NativeZoneMapMetadata *z;
		char	   *cur;
		Datum		maxv;
		int32		c;

		z = PgColumnarReadZoneMapForColumn(storageId, rg->groupNumber,
										   attno - 1, GetActiveSnapshot(), NULL);

		/*
		 * No zone map, or one without min/max, means nothing is known about what
		 * this group holds. Keeping it is the only safe reading: dropping on an
		 * absent bound would drop groups whose contents were never examined.
		 */
		if (z == NULL || !z->hasMinMax)
			continue;

		/*
		 * Keep any group that stored a NULL in the retention column. The
		 * maximum cannot speak for those rows, and treating "every timestamp
		 * we can see is expired" as "the group is expired" drops them.
		 */
		if (z->nullCount > 0)
			continue;

		cur = (char *) z->maximum;
		maxv = PgColumnarDecodeValue(att, &cur, z->maximum + z->maximumLen,
									 CurrentMemoryContext);

		c = DatumGetInt32(FunctionCall2Coll(&tce->cmp_proc_finfo,
										   att->attcollation, maxv, cutoff));
		if (c < 0)
		{
			PgColumnarVMClearForRowRange(rel, rg->firstRowNumber, rg->rowCount);
			PgColumnarRetireGroup(storageId, rg->groupNumber);
			retired++;
		}
	}

	/* keep the lock until end of transaction, as compact does */
	table_close(rel, NoLock);

	PG_RETURN_INT64(retired);
}

Datum
pgcolumnar_compact(PG_FUNCTION_ARGS)
{
	Oid			relid = PG_GETARG_OID(0);
	Relation	rel;
	int64		retired;

	if (PG_ARGISNULL(0))
		ereport(ERROR,
				(errcode(ERRCODE_NULL_VALUE_NOT_ALLOWED),
				 errmsg("table name cannot be null")));

	PgColumnarRequireTableOwnerByOid(relid);

	/* the lazy lock: concurrent reads and writes are allowed during compaction */
	rel = table_open(relid, ShareUpdateExclusiveLock);

	if (!PgColumnarIsColumnarRelation(relid))
	{
		table_close(rel, ShareUpdateExclusiveLock);
		ereport(ERROR,
				(errcode(ERRCODE_WRONG_OBJECT_TYPE),
				 errmsg("relation \"%s\" is not a columnar table",
						RelationGetRelationName(rel))));
	}

	/* self-heal a truncate crash-residual so the no-overlap assert holds eagerly,
	 * even though compact does not reuse (see PgColumnarReconcileFreeList) */
	PgColumnarReconcileFreeList(rel);

	retired = PgColumnarRetireFullyDeletedGroups(rel);

	COLUMNAR_ASSERT_NO_OVERLAP(PgColumnarStorageId(rel));

	/* keep the lock until end of transaction */
	table_close(rel, NoLock);

	PG_RETURN_INT64(retired);
}

/*
 * pgcolumnar_end_truncation_storages
 *		Collect the distinct storage ids that share this relation's file: the base
 *		storage plus every projection's own storage. Returns a list of palloc'd
 *		uint64. All must be considered when computing the safe truncation point,
 *		because they all place data in the one shared file.
 */
static List *
pgcolumnar_end_truncation_storages(uint64 base)
{
	List	   *projs = PgColumnarListProjections(base);
	List	   *result = NIL;
	ListCell   *lc;
	uint64	   *b = palloc(sizeof(uint64));

	*b = base;
	result = lappend(result, b);

	foreach(lc, projs)
	{
		PgColumnarProjection *pr = (PgColumnarProjection *) lfirst(lc);
		ListCell   *rc;
		bool		dup = false;

		foreach(rc, result)
			if (*(uint64 *) lfirst(rc) == pr->projStorageId)
			{
				dup = true;
				break;
			}
		if (!dup)
		{
			uint64	   *s = palloc(sizeof(uint64));

			*s = pr->projStorageId;
			result = lappend(result, s);
		}
	}
	return result;
}

/*
 * pgcolumnar_do_end_truncation
 *		Compute the safe truncation point and, if the trailing region is entirely
 *		reclaimable, physically shrink the main fork. The caller holds
 *		AccessExclusiveLock, so no reader or writer is concurrent. Returns the
 *		number of blocks returned to the OS (0 if nothing was truncated).
 *
 *		Safe point = the end of the highest-offset LIVE row group across every
 *		storage id in the file (footprints are page-aligned). The trailing region
 *		[liveEnd, EOF) is truncated only when every free_space range there was
 *		freed before the oldest-xmin horizon: a more recent retirement could still
 *		be read by a snapshot older than it, and that read would fault past the
 *		shrunken EOF.
 *
 *		Ordering for crash safety: the physical smgrtruncate is done BEFORE the
 *		metapage highwater is lowered. The highwater lowering is a full-page-image
 *		WAL that persists even on abort, whereas the free_space deletes are
 *		transactional; performing the shrink first means a crash in the large
 *		window (after the truncate, before the highwater is lowered) leaves
 *		"highwater still high + free_space restored + file short", which the
 *		gap-tolerant write path self-heals. The function also runs outside a
 *		transaction block (see pgcolumnar_truncate), so a user ROLLBACK cannot land
 *		in the residual window between lowering the highwater and commit; and it
 *		first purges any free_space row at or above the current highwater, which
 *		under the exclusive lock is stale by definition and would otherwise be the
 *		artifact of a prior crash in that residual window. Readers only ever touch
 *		live groups, none of which are in the truncated region.
 */
static int64
pgcolumnar_do_end_truncation(Relation rel)
{
	uint64		base = PgColumnarStorageId(rel);
	TransactionId oldestXmin = PgColumnarOldestXmin(rel);
	List	   *storages = pgcolumnar_end_truncation_storages(base);
	ListCell   *lc;
	uint64		liveEnd = COLUMNAR_FIRST_LOGICAL_OFFSET;
	PgColumnarMetapage meta;
	uint64		highwater;
	Snapshot	snap;
	BlockNumber oldnblocks;
	BlockNumber truncBlock;

	PgColumnarReadMetapage(rel, &meta);
	highwater = meta.reservedOffset;

	/*
	 * Self-heal a prior crash. Two residual shapes are possible and both are
	 * cleaned before we proceed: a free_space row at or above the highwater (freed
	 * ranges are always below it, so any such row is the artifact of a crash
	 * between lowering the highwater and commit), and a row below the highwater
	 * that overlaps a live group (a range this truncation would have removed, left
	 * by such a crash and then covered by a later insert). The latter also keeps
	 * the end-of-run no-overlap assert valid.
	 */
	foreach(lc, storages)
		PgColumnarDeleteFreeSpaceAtOrAbove(*(uint64 *) lfirst(lc), highwater);
	PgColumnarReconcileFreeList(rel);

	/* highest live-data end across all storages, in the latest committed state */
	snap = RegisterSnapshot(GetLatestSnapshot());
	foreach(lc, storages)
	{
		uint64		sid = *(uint64 *) lfirst(lc);
		List	   *rgs = PgColumnarReadRowGroupList(sid, snap);
		ListCell   *g;

		foreach(g, rgs)
		{
			NativeRowGroupMetadata *rg = (NativeRowGroupMetadata *) lfirst(g);
			uint64		end = rg->fileOffset + COLUMNAR_PAGE_ROUND_UP(rg->byteLength);

			if (end > liveEnd)
				liveEnd = end;
		}
	}
	UnregisterSnapshot(snap);

	Assert(liveEnd % COLUMNAR_BYTES_PER_PAGE == 0);
	truncBlock = (BlockNumber) (liveEnd / COLUMNAR_BYTES_PER_PAGE);

	oldnblocks = smgrnblocks(RelationGetSmgr(rel), MAIN_FORKNUM);
	if (truncBlock >= oldnblocks)
		return 0;				/* no trailing blocks to reclaim */

	/* the trailing region must be entirely behind the oldest-xmin horizon */
	foreach(lc, storages)
		if (!PgColumnarTrailingFreeSpaceSafe(*(uint64 *) lfirst(lc), liveEnd,
										   oldestXmin))
			return 0;			/* a recent retirement is in the tail; retry later */

	/* drop the trailing free ranges, shrink the file, THEN lower the highwater */
	foreach(lc, storages)
		PgColumnarDeleteFreeSpaceAtOrAbove(*(uint64 *) lfirst(lc), liveEnd);
	CommandCounterIncrement();
	PgColumnarTruncateMainFork(rel, truncBlock);
	PgColumnarSetReservedOffset(rel, liveEnd);

	/* stale offset-keyed cache entries for this relation must go */
	CacheInvalidateRelcacheByRelid(RelationGetRelid(rel));
	COLUMNAR_ASSERT_NO_OVERLAP(base);

	return (int64) (oldnblocks - truncBlock);
}

/*
 * pgcolumnar_truncate
 *		SQL: pgcolumnar.truncate(regclass) -> bigint (blocks returned to the OS).
 *		Physically shrinks a columnar table's file by dropping trailing blocks that
 *		reclaim has freed. Opt-in (gated by pgcolumnar.enable_end_truncation) and
 *		best-effort: it takes AccessExclusiveLock CONDITIONALLY for the brief
 *		physical step (as PostgreSQL's own lazy-VACUUM truncation does) and returns
 *		0 without waiting if the table is busy, so it never blocks concurrent load.
 */
Datum
pgcolumnar_truncate(PG_FUNCTION_ARGS)
{
	Oid			relid;
	Relation	rel;
	int64		result = 0;

	if (PG_ARGISNULL(0))
		ereport(ERROR,
				(errcode(ERRCODE_NULL_VALUE_NOT_ALLOWED),
				 errmsg("table name cannot be null")));
	relid = PG_GETARG_OID(0);

	/*
	 * Must not run inside a transaction block. The physical smgrtruncate is not
	 * rolled back on abort, but the metapage highwater lowering (a full-page-image
	 * WAL, which persists across abort) and the free_space deletes (transactional,
	 * which do roll back) are not the same persistence class. A user ROLLBACK
	 * around truncate would therefore leave them inconsistent with the shortened
	 * file -- a lowered highwater with the trailing free ranges restored -- which a
	 * later insert plus compaction could overwrite. Forbidding a transaction block
	 * (as VACUUM does) removes that hazard and keeps the AccessExclusiveLock brief.
	 */
	if (IsTransactionBlock())
		ereport(ERROR,
				(errcode(ERRCODE_ACTIVE_SQL_TRANSACTION),
				 errmsg("pgcolumnar.truncate() cannot run inside a transaction block")));

	/* serialize with other lazy maintenance (compact/recluster also take SUEL) */
	rel = table_open(relid, ShareUpdateExclusiveLock);
	if (!PgColumnarIsColumnarRelation(relid))
	{
		table_close(rel, ShareUpdateExclusiveLock);
		ereport(ERROR,
				(errcode(ERRCODE_WRONG_OBJECT_TYPE),
				 errmsg("relation \"%s\" is not a columnar table",
						RelationGetRelationName(rel))));
	}

	PgColumnarRequireTableOwner(rel);

	if (pgcolumnar_enable_end_truncation &&
		ConditionalLockRelation(rel, AccessExclusiveLock))
		result = pgcolumnar_do_end_truncation(rel);

	/* keep the locks until end of transaction */
	table_close(rel, NoLock);
	PG_RETURN_INT64(result);
}

/*
 * pgcolumnar_debug_advance_reserved_offset
 *		SQL test hook: advance a columnar table's write highwater by N pages
 *		without writing data, leaving a gap between the physical EOF and the
 *		highwater so the next write exercises the gap-tolerant path. Not bound in
 *		the shipped catalog; the gap test creates the SQL binding itself.
 */
Datum
pgcolumnar_debug_advance_reserved_offset(PG_FUNCTION_ARGS)
{
	Oid			relid = PG_GETARG_OID(0);
	int32		npages = PG_GETARG_INT32(1);
	Relation	rel;

	/*
	 * Owner-only, checked before table_open like the other maintenance verbs
	 * (#568, #707). This function ships unbound, but a binding is one CREATE
	 * FUNCTION away (the test suites do exactly that), and a storage-metadata
	 * mutator with no gate is a footgun waiting for that binding.
	 */
	PgColumnarRequireTableOwnerByOid(relid);

	if (npages < 0)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("npages must be non-negative")));

	rel = table_open(relid, RowExclusiveLock);
	if (!PgColumnarIsColumnarRelation(relid))
	{
		table_close(rel, RowExclusiveLock);
		ereport(ERROR,
				(errcode(ERRCODE_WRONG_OBJECT_TYPE),
				 errmsg("relation \"%s\" is not a columnar table",
						RelationGetRelationName(rel))));
	}

	PgColumnarAdvanceReservedOffset(rel, (uint64) npages * COLUMNAR_BYTES_PER_PAGE);

	table_close(rel, NoLock);
	PG_RETURN_VOID();
}

/*
 * pgcolumnar_debug_set_metapage_version
 *		Test hook: overwrite a columnar table's stored metapage format version so
 *		a subsequent read exercises the unsupported-version rejection in
 *		PgColumnarReadMetapage. Not bound in the shipped catalog; the format suite
 *		creates the binding when it needs it (like the advance helper above).
 */
Datum
pgcolumnar_debug_set_metapage_version(PG_FUNCTION_ARGS)
{
	Oid			relid = PG_GETARG_OID(0);
	int32		major = PG_GETARG_INT32(1);
	int32		minor = PG_GETARG_INT32(2);
	Relation	rel;

	/* owner-only before the open, same as advance_reserved_offset (#707) */
	PgColumnarRequireTableOwnerByOid(relid);

	rel = table_open(relid, RowExclusiveLock);
	if (!PgColumnarIsColumnarRelation(relid))
	{
		table_close(rel, RowExclusiveLock);
		ereport(ERROR,
				(errcode(ERRCODE_WRONG_OBJECT_TYPE),
				 errmsg("relation \"%s\" is not a columnar table",
						RelationGetRelationName(rel))));
	}

	PgColumnarDebugSetMetapageVersion(rel, (uint32) major, (uint32) minor);

	table_close(rel, NoLock);
	PG_RETURN_VOID();
}
