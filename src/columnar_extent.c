/*-------------------------------------------------------------------------
 *
 * columnar_extent.c
 *		The per-column extent (#1304 option 5): a bound on every value of a
 *		column, read in one index lookup instead of every zone map.
 *
 * THE CATALOG, pgcolumnar.column_extent, holds rows of (storage, column,
 * complete, minimum, maximum). Rows are inserted and deleted, never updated:
 *
 *   - Each transaction folds the whole-chunk zone maps it flushes, in memory,
 *     with the column type's comparison and collation, and at pre-commit
 *     inserts ONE row per (storage, column), complete = false. Its groups and
 *     its row commit or abort together, so any snapshot sees both or neither,
 *     and two writers never touch the same tuple.
 *   - A COMPLETE row comes only from a recompute: the fold of every whole-chunk
 *     zone map visible to one snapshot, which then replaces the rows visible to
 *     that same snapshot. It refuses (and changes nothing) when a group lacks a
 *     zone map for the column or has values without a minimum and maximum.
 *   - Every path that retires a group marks its storage (PgColumnarRetireGroup)
 *     and the storage is recomputed before commit, so a shrink is computed,
 *     never guessed.
 *
 * THE READER trusts the rows only when a complete one is visible, and then the
 * fold of every visible row is a true bound. A reader's snapshot S that sees a
 * complete row R (computed at snapshot B, committed before S): the groups S sees
 * were either visible at B, and so are in R, or committed between B and S, in
 * which case their writer's own row committed with them and S sees it. A retire
 * and its recompute are one transaction, so S sees both or neither. The
 * reader's own transaction adds its pending fold, since its groups are visible
 * to it before its row is written. Otherwise the caller falls back to the zone
 * maps.
 *
 * A ROLLED-BACK SUBTRANSACTION that flushed a group leaves its values in the
 * fold: wider than the data, which only over-estimates.
 *
 *-------------------------------------------------------------------------
 */
#include "postgres.h"

#include "columnar.h"
#include "access/relation.h"
#include "access/table.h"
#include "access/xact.h"
#include "funcapi.h"
#include "lib/stringinfo.h"
#include "miscadmin.h"
#include "storage/lmgr.h"
#include "storage/lock.h"
#include "utils/acl.h"
#include "utils/builtins.h"
#include "utils/datum.h"
#include "utils/hsearch.h"
#include "utils/lsyscache.h"
#include "utils/memutils.h"
#include "utils/rel.h"
#include "utils/snapmgr.h"
#include "utils/tuplestore.h"
#include "utils/typcache.h"

PG_FUNCTION_INFO_V1(pgcolumnar_column_extent);

/*
 * More rows than this for one column, with a complete one among them, and the
 * committing writer folds them into one. The fold reads only these rows, never
 * the zone maps, so it costs a few tuples however large the table.
 */
#define EXTENT_MERGE_ABOVE 8

typedef struct ExtentKey
{
	uint64		storageId;
	int32		columnIndex;
} ExtentKey;

typedef struct ExtentEntry
{
	ExtentKey	key;
	Oid			relid;
	FormData_pg_attribute att;
	Datum		minimum;
	Datum		maximum;
} ExtentEntry;

/* both live in TopTransactionContext, and are forgotten at transaction end */
static HTAB *extent_pending = NULL;
static HTAB *extent_recompute = NULL;	/* uint64 storage ids */

static int
extent_cmp(Form_pg_attribute att, Datum a, Datum b)
{
	TypeCacheEntry *tce = lookup_type_cache(att->atttypid, TYPECACHE_CMP_PROC_FINFO);

	if (!OidIsValid(tce->cmp_proc))
		elog(ERROR, "column extent: type %u has no btree comparison", att->atttypid);
	return DatumGetInt32(FunctionCall2Coll(&tce->cmp_proc_finfo, att->attcollation, a, b));
}

static Datum
extent_decode(Form_pg_attribute att, const char *bytes, int len)
{
	char	   *cur = (char *) bytes;

	return PgColumnarDecodeValue(att, &cur, (char *) bytes + len, CurrentMemoryContext);
}

static HTAB *
extent_hash(const char *name, Size keysize, Size entrysize)
{
	HASHCTL		ctl;

	memset(&ctl, 0, sizeof(ctl));
	ctl.keysize = keysize;
	ctl.entrysize = entrysize;
	ctl.hcxt = TopTransactionContext;
	return hash_create(name, 16, &ctl, HASH_ELEM | HASH_BLOBS | HASH_CONTEXT);
}

/*
 * PgColumnarExtentNote
 *		Fold one whole-chunk zone map, just flushed for the relation's base
 *		storage, into this transaction's extent for the column.
 */
void
PgColumnarExtentNote(Oid relid, uint64 storageId, int columnIndex,
					 const NativeZoneMapMetadata *z)
{
	ExtentKey	key;
	ExtentEntry *e;
	bool		found;
	Relation	rel;
	Form_pg_attribute att;
	MemoryContext old;
	Datum		mn,
				mx;

	if (!z->hasMinMax)
		return;					/* all NULL, or a type with no ordering */
	if (extent_pending == NULL)
		extent_pending = extent_hash("pgcolumnar column extents", sizeof(ExtentKey),
									 sizeof(ExtentEntry));
	memset(&key, 0, sizeof(key));
	key.storageId = storageId;
	key.columnIndex = columnIndex;
	e = (ExtentEntry *) hash_search(extent_pending, &key, HASH_ENTER, &found);
	if (!found)
	{
		rel = relation_open(relid, NoLock);
		att = TupleDescAttr(RelationGetDescr(rel), columnIndex);
		memcpy(&e->att, att, sizeof(FormData_pg_attribute));
		relation_close(rel, NoLock);
		e->relid = relid;
	}
	att = &e->att;
	mn = extent_decode(att, z->minimum, z->minimumLen);
	mx = extent_decode(att, z->maximum, z->maximumLen);
	old = MemoryContextSwitchTo(TopTransactionContext);
	if (!found || extent_cmp(att, mn, e->minimum) < 0)
		e->minimum = datumCopy(mn, att->attbyval, att->attlen);
	if (!found || extent_cmp(att, mx, e->maximum) > 0)
		e->maximum = datumCopy(mx, att->attbyval, att->attlen);
	MemoryContextSwitchTo(old);
}

void
PgColumnarExtentMarkRecompute(uint64 storageId)
{
	if (extent_recompute == NULL)
		extent_recompute = extent_hash("pgcolumnar extent recomputes", sizeof(uint64),
									   sizeof(uint64));
	(void) hash_search(extent_recompute, &storageId, HASH_ENTER, NULL);
}

void
PgColumnarExtentAtXactEnd(void)
{
	/* the tables fall with TopTransactionContext */
	extent_pending = NULL;
	extent_recompute = NULL;
}

static void
extent_encode(Form_pg_attribute att, Datum v, StringInfo buf)
{
	resetStringInfo(buf);
	PgColumnarEncodeValue(buf, att, v);
}

/*
 * One recompute or merge per storage at a time, held to transaction end: the
 * rows a recompute deletes are the rows its snapshot sees, and a second one
 * deleting the same rows would fail. Returns false only when !wait and taken.
 */
static bool
extent_lock(uint64 storageId, bool wait)
{
	LOCKTAG		tag;

	SET_LOCKTAG_ADVISORY(tag, MyDatabaseId,
						 (uint32) (storageId >> 32), (uint32) (storageId & 0xFFFFFFFF),
						 PGCOLUMNAR_LOCKCLASS_EXTENT);
	return LockAcquire(&tag, ExclusiveLock, false, !wait) != LOCKACQUIRE_NOT_AVAIL;
}

/* replace every row the snapshot sees with one complete row */
static void
extent_replace(uint64 storageId, int col, Form_pg_attribute att, Snapshot snap,
			   bool any, Datum mn, Datum mx)
{
	List	   *rows = PgColumnarReadExtentRows(storageId, col, snap);
	ListCell   *lc;
	StringInfoData a,
				b;

	foreach(lc, rows)
		PgColumnarDeleteExtentRow(&((NativeExtentRow *) lfirst(lc))->tid);
	if (!any)
	{
		PgColumnarInsertExtentRow(storageId, col, true, NULL, 0, NULL, 0);
		return;
	}
	initStringInfo(&a);
	initStringInfo(&b);
	extent_encode(att, mn, &a);
	extent_encode(att, mx, &b);
	PgColumnarInsertExtentRow(storageId, col, true, a.data, a.len, b.data, b.len);
}

/*
 * The full recompute, from every whole-chunk zone map the snapshot sees. Gives
 * up, writing nothing, when the zone maps cannot bound the column.
 */
static void
extent_recompute_column(uint64 storageId, int col, Form_pg_attribute att, Snapshot snap)
{
	List	   *groups = PgColumnarReadRowGroupList(storageId, snap);
	List	   *zones = PgColumnarReadWholeChunkZoneMapsForColumn(storageId, col, snap);
	ListCell   *lc;
	bool		any = false;
	Datum		mn = (Datum) 0,
				mx = (Datum) 0;

	if (list_length(zones) != list_length(groups))
		return;					/* a group with no zone map: a column added later */
	foreach(lc, zones)
	{
		NativeZoneMapMetadata *z = (NativeZoneMapMetadata *) lfirst(lc);
		Datum		zmn,
					zmx;

		if (!z->hasMinMax)
		{
			if (z->valueCount == 0)
				continue;		/* all NULL */
			return;				/* values without an ordering */
		}
		zmn = extent_decode(att, z->minimum, z->minimumLen);
		zmx = extent_decode(att, z->maximum, z->maximumLen);
		if (!any || extent_cmp(att, zmn, mn) < 0)
			mn = zmn;
		if (!any || extent_cmp(att, zmx, mx) > 0)
			mx = zmx;
		any = true;
	}
	extent_replace(storageId, col, att, snap, any, mn, mx);
}

/*
 * The merge: fold the rows the snapshot sees into one complete row. Valid only
 * when a complete row is among them -- then their fold is a bound on every
 * group the snapshot sees, by the reader's argument above.
 */
static void
extent_merge_column(uint64 storageId, int col, Form_pg_attribute att, Snapshot snap)
{
	List	   *rows = PgColumnarReadExtentRows(storageId, col, snap);
	ListCell   *lc;
	bool		complete = false;
	bool		any = false;
	Datum		mn = (Datum) 0,
				mx = (Datum) 0;

	foreach(lc, rows)
	{
		NativeExtentRow *r = (NativeExtentRow *) lfirst(lc);
		Datum		rmn,
					rmx;

		complete |= r->complete;
		if (!r->hasValues)
			continue;
		rmn = extent_decode(att, r->minimum, r->minimumLen);
		rmx = extent_decode(att, r->maximum, r->maximumLen);
		if (!any || extent_cmp(att, rmn, mn) < 0)
			mn = rmn;
		if (!any || extent_cmp(att, rmx, mx) > 0)
			mx = rmx;
		any = true;
	}
	if (complete)
		extent_replace(storageId, col, att, snap, any, mn, mx);
	else
		extent_recompute_column(storageId, col, att, snap);
}

/* a fresh snapshot, taken after the lock, that sees this transaction's own rows */
static Snapshot
extent_snapshot(void)
{
	CommandCounterIncrement();
	return RegisterSnapshot(GetLatestSnapshot());
}

static void
extent_recompute_storage(uint64 storageId)
{
	Snapshot	snap = extent_snapshot();
	Oid			relid = PgColumnarStorageRelationOid(storageId, snap);
	Relation	rel;
	TupleDesc	td;
	int			i;

	UnregisterSnapshot(snap);
	if (!OidIsValid(relid))
		return;
	rel = try_relation_open(relid, AccessShareLock);
	if (rel == NULL)
		return;
	/* a projection's storage names its base relation: no extents for those */
	if (PgColumnarStorageId(rel) != storageId)
	{
		relation_close(rel, AccessShareLock);
		return;
	}
	(void) extent_lock(storageId, true);
	snap = extent_snapshot();
	td = RelationGetDescr(rel);
	for (i = 0; i < td->natts; i++)
		if (!TupleDescAttr(td, i)->attisdropped)
			extent_recompute_column(storageId, i, TupleDescAttr(td, i), snap);
	UnregisterSnapshot(snap);
	relation_close(rel, AccessShareLock);
}

/*
 * PgColumnarExtentPreCommit
 *		Called at pre-commit and pre-prepare, after the pending writes are
 *		flushed: write this transaction's rows, then recompute what it marked.
 */
void
PgColumnarExtentPreCommit(void)
{
	HASH_SEQ_STATUS st;
	ExtentEntry *e;
	uint64	   *sid;
	List	   *written = NIL;
	ListCell   *lc;
	StringInfoData a,
				b;
	bool		pushed = false;

	if (extent_pending == NULL && extent_recompute == NULL)
		return;
	if (!PgColumnarExtentCatalogPresent())
	{
		PgColumnarExtentAtXactEnd();
		return;
	}
	/* pre-commit runs with no active snapshot, and catalog writes need one */
	if (!ActiveSnapshotSet())
	{
		PushActiveSnapshot(GetTransactionSnapshot());
		pushed = true;
	}
	initStringInfo(&a);
	initStringInfo(&b);
	if (extent_pending != NULL)
	{
		Snapshot	seen = extent_snapshot();

		hash_seq_init(&st, extent_pending);
		while ((e = (ExtentEntry *) hash_seq_search(&st)) != NULL)
		{
			/* a recompute of the storage takes this transaction's groups anyway */
			if (extent_recompute != NULL &&
				hash_search(extent_recompute, &e->key.storageId, HASH_FIND, NULL) != NULL)
				continue;
			/* created in a savepoint that rolled back: nothing to bound */
			if (!OidIsValid(PgColumnarStorageRelationOid(e->key.storageId, seen)))
				continue;
			extent_encode(&e->att, e->minimum, &a);
			extent_encode(&e->att, e->maximum, &b);
			PgColumnarInsertExtentRow(e->key.storageId, e->key.columnIndex, false,
									  a.data, a.len, b.data, b.len);
			written = lappend(written, e);
		}
		UnregisterSnapshot(seen);
	}
	if (extent_recompute != NULL)
	{
		hash_seq_init(&st, extent_recompute);
		while ((sid = (uint64 *) hash_seq_search(&st)) != NULL)
			extent_recompute_storage(*sid);
	}

	/*
	 * Keep the rows few. A writer that finds no complete row recomputes one; one
	 * that finds too many merges them. Conditional: a writer never waits for
	 * another's recompute, it leaves the work to the next.
	 */
	foreach(lc, written)
	{
		List	   *rows;
		ListCell   *rc;
		bool		complete = false;
		Snapshot	snap;

		e = (ExtentEntry *) lfirst(lc);
		snap = extent_snapshot();
		rows = PgColumnarReadExtentRows(e->key.storageId, e->key.columnIndex, snap);
		UnregisterSnapshot(snap);
		foreach(rc, rows)
			complete |= ((NativeExtentRow *) lfirst(rc))->complete;
		if (complete && list_length(rows) <= EXTENT_MERGE_ABOVE)
			continue;
		if (!extent_lock(e->key.storageId, false))
			continue;
		snap = extent_snapshot();
		if (complete)
			extent_merge_column(e->key.storageId, e->key.columnIndex, &e->att, snap);
		else
			extent_recompute_column(e->key.storageId, e->key.columnIndex, &e->att, snap);
		UnregisterSnapshot(snap);
	}
	if (pushed)
		PopActiveSnapshot();
	PgColumnarExtentAtXactEnd();
}

/*
 * PgColumnarExtentBound
 *		The column's bound as the snapshot sees it, when a complete row is
 *		visible. false: no trustworthy extent, fall back to the zone maps.
 *		*hasValues false: the column holds no non-NULL value.
 *
 *		THE CALLER'S OWN TRANSACTION sees the groups it has flushed, but its
 *		extent row is written only at pre-commit, so the visible rows alone would
 *		leave those groups out and bound the column too narrowly. Its pending fold
 *		is added in. Groups it has retired are still in the visible rows until
 *		its recompute commits: wider than the data, which only over-estimates.
 */
bool
PgColumnarExtentBound(Relation rel, int columnIndex, Snapshot snapshot,
					  Datum *minimum, Datum *maximum, bool *hasValues)
{
	Form_pg_attribute att;
	List	   *rows;
	ListCell   *lc;
	bool		complete = false;
	uint64		storageId;

	if (columnIndex < 0 || columnIndex >= RelationGetDescr(rel)->natts ||
		!PgColumnarExtentCatalogPresent())
		return false;
	att = TupleDescAttr(RelationGetDescr(rel), columnIndex);
	storageId = PgColumnarStorageId(rel);
	rows = PgColumnarReadExtentRows(storageId, columnIndex, snapshot);
	*hasValues = false;
	foreach(lc, rows)
	{
		NativeExtentRow *r = (NativeExtentRow *) lfirst(lc);
		Datum		rmn,
					rmx;

		complete |= r->complete;
		if (!r->hasValues)
			continue;
		rmn = extent_decode(att, r->minimum, r->minimumLen);
		rmx = extent_decode(att, r->maximum, r->maximumLen);
		if (!*hasValues || extent_cmp(att, rmn, *minimum) < 0)
			*minimum = rmn;
		if (!*hasValues || extent_cmp(att, rmx, *maximum) > 0)
			*maximum = rmx;
		*hasValues = true;
	}
	if (complete && extent_pending != NULL)
	{
		ExtentKey	key;
		ExtentEntry *e;

		memset(&key, 0, sizeof(key));
		key.storageId = storageId;
		key.columnIndex = columnIndex;
		e = (ExtentEntry *) hash_search(extent_pending, &key, HASH_FIND, NULL);
		if (e != NULL)
		{
			if (!*hasValues || extent_cmp(att, e->minimum, *minimum) < 0)
				*minimum = e->minimum;
			if (!*hasValues || extent_cmp(att, e->maximum, *maximum) > 0)
				*maximum = e->maximum;
			*hasValues = true;
		}
	}
	return complete;
}

/*
 * pgcolumnar.column_extent(rel regclass, col name)
 *		-> (complete bool, minimum text, maximum text)
 *		The bound the planner would use, printed by the column type's output
 *		function; no row when it would fall back to the zone maps. The values are
 *		column data, so the caller must be able to SELECT the table.
 */
Datum
pgcolumnar_column_extent(PG_FUNCTION_ARGS)
{
	ReturnSetInfo *rsinfo = (ReturnSetInfo *) fcinfo->resultinfo;
	Oid			relid = PG_GETARG_OID(0);
	char	   *col = NameStr(*PG_GETARG_NAME(1));
	AclResult	ac;
	Relation	rel;
	AttrNumber	attnum;
	Datum		mn = (Datum) 0,
				mx = (Datum) 0;
	bool		hasValues;

	InitMaterializedSRF(fcinfo, 0);
	ac = pg_class_aclcheck(relid, GetUserId(), ACL_SELECT);
	if (ac != ACLCHECK_OK)
		aclcheck_error(ac, OBJECT_TABLE, get_rel_name(relid));
	/*
	 * The stored extent covers every row, so under a policy it would show values
	 * from rows the caller cannot see. Refused as the other direct-storage entry
	 * points are (#563), after the ACL check (OffgridwithJD, review of #1429).
	 */
	PgColumnarRequireNoRowSecurity(relid);
	rel = relation_open(relid, AccessShareLock);
	if (!PgColumnarIsColumnarRelation(relid))
		ereport(ERROR,
				(errcode(ERRCODE_WRONG_OBJECT_TYPE),
				 errmsg("\"%s\" is not a pgcolumnar table", RelationGetRelationName(rel))));
	attnum = get_attnum(relid, col);
	if (attnum <= 0)
		ereport(ERROR,
				(errcode(ERRCODE_UNDEFINED_COLUMN),
				 errmsg("column \"%s\" of relation \"%s\" does not exist",
						col, RelationGetRelationName(rel))));
	if (PgColumnarExtentBound(rel, attnum - 1,
							  PgColumnarCatalogSnapshot(GetActiveSnapshot()),
							  &mn, &mx, &hasValues))
	{
		Form_pg_attribute att = TupleDescAttr(RelationGetDescr(rel), attnum - 1);
		Datum		values[3];
		bool		nulls[3] = {false, !hasValues, !hasValues};
		Oid			out;
		bool		isvarlena;

		values[0] = BoolGetDatum(true);
		if (hasValues)
		{
			getTypeOutputInfo(att->atttypid, &out, &isvarlena);
			values[1] = CStringGetTextDatum(OidOutputFunctionCall(out, mn));
			values[2] = CStringGetTextDatum(OidOutputFunctionCall(out, mx));
		}
		tuplestore_putvalues(rsinfo->setResult, rsinfo->setDesc, values, nulls);
	}
	relation_close(rel, AccessShareLock);
	return (Datum) 0;
}
