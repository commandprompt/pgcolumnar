/*-------------------------------------------------------------------------
 *
 * pgcolumnar_metadata.c
 *		Access to the "columnar" metadata catalog tables and the storage-id
 *		sequence (spec 7). Most metadata are ordinary heap tables keyed by
 *		storage id (the options and projection_declaration tables are keyed by
 *		relation OID instead); we read and write them with direct catalog access
 *		so we do not depend on SPI reentrancy.
 *
 *-------------------------------------------------------------------------
 */
#include "columnar.h"

#include "columnar_metadata.h"
#include "fmgr.h"
#include "columnar_compat.h"
#include "columnar_reader.h"
#include "access/genam.h"
#include "access/htup_details.h"
#include "access/table.h"
#include "access/transam.h"
#include "access/xact.h"
#include "catalog/indexing.h"
#include "catalog/namespace.h"
#include "catalog/pg_class.h"
#include "catalog/pg_type.h"
#include "commands/defrem.h"
#include "commands/sequence.h"
#include "miscadmin.h"
#include "storage/bufmgr.h"
#include "storage/lock.h"
#include "storage/procarray.h"
#include "utils/array.h"
#include "utils/builtins.h"
#include "utils/datum.h"
#include "utils/fmgroids.h"
#include "utils/lsyscache.h"
#include "utils/rel.h"
#include "utils/snapmgr.h"

/* attribute numbers for columnar.options (spec 7.4) */
#define Anum_options_regclass 1
#define Anum_options_chunk_group_row_limit 2
#define Anum_options_stripe_row_limit 3
#define Anum_options_compression_level 4
#define Anum_options_compression 5
#define Anum_options_encode_effort 6
#define Anum_options_sort_by 7
#define Anum_options_ttl_column 8
#define Anum_options_ttl_interval 9
/* Nine, not seven: ttl_column and ttl_interval (#403 item 5a) widened the table
 * and this constant was left behind. It sizes the values/nulls/replace arrays
 * handed to heap_modify_tuple, which iterates tupdesc->natts, so a short value
 * here reads past three stack arrays at once. test/catalog_natts.sh pins every
 * Natts_* against the width the server reports. */
#define Natts_options 9

/* attribute numbers for columnar.projection (gap 26, format 2.2) */
#define Anum_projection_declaration_rel 1
#define Anum_projection_declaration_name 2
#define Anum_projection_declaration_columns 3
#define Anum_projection_declaration_sort_key 4
#define Natts_projection_declaration 4

#define Anum_projection_storage_id 1
#define Anum_projection_projection_id 2
#define Anum_projection_name 3
#define Anum_projection_proj_storage_id 4
#define Anum_projection_sort_key 5
#define Anum_projection_columns 6
#define Natts_projection 6

/* attribute numbers for the native format catalog (PGCN v1, native spec 11) */
#define Anum_native_storage_storage_id 1
#define Anum_native_storage_relation_oid 2
#define Anum_native_storage_format_version 3
#define Anum_native_storage_vector_length 4
#define Anum_native_storage_row_group_limit 5
#define Anum_native_storage_sorted_through 6
#define Anum_native_storage_sorted_from 7
#define Anum_native_storage_sorted_by 8
#define Anum_native_storage_sorted_kind 9
#define Natts_native_storage 9

#define Anum_row_group_storage_id 1
#define Anum_row_group_group_number 2
#define Anum_row_group_file_offset 3
#define Anum_row_group_row_count 4
#define Anum_row_group_byte_length 5
#define Anum_row_group_first_row_number 6
#define Anum_row_group_sort_key 7
#define Natts_row_group 7

#define Anum_column_chunk_storage_id 1
#define Anum_column_chunk_group_number 2
#define Anum_column_chunk_column_index 3
#define Anum_column_chunk_value_count 4
#define Anum_column_chunk_encoding_descriptor 5
#define Anum_column_chunk_block_codec 6
#define Anum_column_chunk_page_offset 7
#define Anum_column_chunk_page_length 8
#define Natts_column_chunk 8

#define Anum_zone_map_storage_id 1
#define Anum_zone_map_group_number 2
#define Anum_zone_map_column_index 3
#define Anum_zone_map_vector_index 4
#define Anum_zone_map_minimum 5
#define Anum_zone_map_maximum 6
#define Anum_zone_map_sum 7
#define Anum_zone_map_value_count 8
#define Anum_zone_map_null_count 9
/*
 * #1144. A NULLABLE column added by the alpha5 upgrade, so a catalog that has
 * not been upgraded has NINE attributes and this attnum does not exist in it.
 * Every read of it is guarded by tupdesc->natts, because heap_getattr past natts
 * CRASHES rather than returning NULL -- this repository has paid for that once.
 */
#define Anum_zone_map_max_upper 10
#define Natts_zone_map 10

#define Anum_bloom_storage_id 1
#define Anum_bloom_group_number 2
#define Anum_bloom_column_index 3
#define Anum_bloom_filter 4
#define Natts_bloom 4

/* pgcolumnar.load_fingerprint (#403 item 7) */
#define Anum_load_fingerprint_relation_oid 1
#define Anum_load_fingerprint_fingerprint 2
#define Anum_load_fingerprint_rows 3
#define Anum_load_fingerprint_loaded_at 4
#define Natts_load_fingerprint 4

/* attribute numbers for columnar.free_space (Phase F physical reclaim) */
#define Anum_free_space_storage_id 1
#define Anum_free_space_file_offset 2
#define Anum_free_space_byte_length 3
#define Anum_free_space_freed_xid 4
#define Natts_free_space 4

/* attribute numbers for columnar.delete_vector (spec 7.5) */
#define Anum_delete_vector_storage_id 1
#define Anum_delete_vector_group_number 2
#define Anum_delete_vector_bitmap 3
#define Anum_delete_vector_deleted_count 4
#define Natts_delete_vector 4

static Oid	pgcolumnar_schema_oid(void);
static Relation open_columnar_table(const char *name, LOCKMODE lockmode);
static Oid	pgcolumnar_index_oid(const char *name);
static Oid	pgcolumnar_scan_index_oid(Relation rel, const char *name);

/*
 * pgcolumnar_schema_oid
 *		OID of the "columnar" schema. It always exists once the extension is
 *		installed.
 */
static Oid
pgcolumnar_schema_oid(void)
{
	return get_namespace_oid(COLUMNAR_SCHEMA_NAME, false);
}

/*
 * #445: a metadata flush session.
 *
 * Every PgColumnarInsert*Row opened its catalog with open_columnar_table,
 * inserted one row, and closed. A stripe flush inserts a chunk row per column
 * and about 16 zone rows per column, so it opened a metadata relation on the
 * order of natts * 17 times per flush. Each open ran get_namespace_oid plus
 * get_relname_relid (catcache probes) and table_open (a relcache lookup, a lock,
 * a resource-owner remember), and the matching close undid it. A profile of the
 * numeric write path (#445) put that per-row open and close cycle, not the
 * encoding, at the top of the profile.
 *
 * A session caches each metadata relation and its index state the first time the
 * flush opens it, and reuses that open for every later row of the same flush. The
 * relation is closed once, when the flush ends. The rows written, their values,
 * their order, and the indexes updated are all unchanged, so the catalog and the
 * on-disk bytes are byte-for-byte identical. This is a scan-local optimisation,
 * not a format change.
 */
#define MD_FLUSH_MAX 8
typedef struct MetadataFlushSession
{
	bool		active;
	int			count;
	const char *names[MD_FLUSH_MAX];
	LOCKMODE	locks[MD_FLUSH_MAX];
	Relation	rels[MD_FLUSH_MAX];
	CatalogIndexState indstates[MD_FLUSH_MAX];	/* lazily opened on first insert */
} MetadataFlushSession;

static MetadataFlushSession md_flush = {0};

/*
 * Count of real metadata relation opens during the active session. It is the
 * work-done witness for the #445 batching: with the session off it counts one
 * open per inserted metadata row, with it on it counts one per distinct table.
 * native_metadata_flush.sh asserts on it via the DEBUG1 line End emits.
 */
static uint64 md_flush_opens = 0;

static Relation
open_columnar_table(const char *name, LOCKMODE lockmode)
{
	Oid			nspOid;
	Oid			relOid;
	Relation	rel;

	/* Reuse a relation already open for this flush session. */
	if (md_flush.active)
	{
		int			i;

		for (i = 0; i < md_flush.count; i++)
			if (md_flush.locks[i] == lockmode &&
				strcmp(md_flush.names[i], name) == 0)
				return md_flush.rels[i];
	}

	nspOid = pgcolumnar_schema_oid();
	relOid = get_relname_relid(name, nspOid);

	if (!OidIsValid(relOid))
		ereport(ERROR,
				(errcode(ERRCODE_UNDEFINED_TABLE),
				 errmsg("columnar metadata table \"%s.%s\" does not exist",
						COLUMNAR_SCHEMA_NAME, name)));

	rel = table_open(relOid, lockmode);

	/*
	 * Count every real open reached during a session, whether or not it is
	 * cached. With reuse on this is one per distinct table; with reuse removed it
	 * is one per inserted row, which is what the removal proof in
	 * native_metadata_flush.sh relies on.
	 */
	if (md_flush.active)
		md_flush_opens++;

	/*
	 * Cache it for the rest of the flush. The names are string literals with
	 * static lifetime, so storing the pointer is safe. If more than MD_FLUSH_MAX
	 * distinct tables are ever opened in one flush (there are five), the extra
	 * relation is left uncached and metadata_flush_close closes it as before, so
	 * nothing leaks.
	 */
	if (md_flush.active && md_flush.count < MD_FLUSH_MAX)
	{
		int			i = md_flush.count++;

		md_flush.names[i] = name;
		md_flush.locks[i] = lockmode;
		md_flush.rels[i] = rel;
		md_flush.indstates[i] = NULL;
	}
	return rel;
}

/*
 * metadata_flush_insert
 *		Insert one metadata tuple. Under a flush session the relation's index
 *		state is opened once and reused, so CatalogOpenIndexes does not run per
 *		row. Off-session it is the plain CatalogTupleInsert, unchanged.
 */
static void
metadata_flush_insert(Relation rel, HeapTuple tuple)
{
	if (md_flush.active)
	{
		int			i;

		for (i = 0; i < md_flush.count; i++)
		{
			if (md_flush.rels[i] == rel)
			{
				if (md_flush.indstates[i] == NULL)
					md_flush.indstates[i] = CatalogOpenIndexes(rel);
				CatalogTupleInsertWithInfo(rel, tuple, md_flush.indstates[i]);
				return;
			}
		}
	}
	CatalogTupleInsert(rel, tuple);
}

/*
 * metadata_flush_close
 *		Close a metadata relation, unless the session owns it (then the session
 *		closes it once at the end). Off-session it is the plain table_close.
 */
static void
metadata_flush_close(Relation rel, LOCKMODE lockmode)
{
	if (md_flush.active)
	{
		int			i;

		for (i = 0; i < md_flush.count; i++)
			if (md_flush.rels[i] == rel)
				return;			/* PgColumnarEndMetadataFlush closes it */
	}
	table_close(rel, lockmode);
}

/*
 * PgColumnarBeginMetadataFlush / EndMetadataFlush / ResetMetadataFlush
 *		Bracket the metadata inserts of one stripe flush so the inserters share
 *		one open per table. Begin and End are the success path. Reset is the
 *		error path: the aborting subtransaction closes the relations through the
 *		resource owner, so Reset only drops the session pointer, so that a later
 *		open never reads a relation the abort has freed.
 */
void
PgColumnarBeginMetadataFlush(void)
{
	Assert(!md_flush.active);	/* nesting would orphan the outer opens */
	md_flush.active = true;
	md_flush.count = 0;
	md_flush_opens = 0;
}

void
PgColumnarEndMetadataFlush(void)
{
	int			i;

	if (!md_flush.active)
		return;
	for (i = 0; i < md_flush.count; i++)
	{
		if (md_flush.indstates[i] != NULL)
			CatalogCloseIndexes(md_flush.indstates[i]);
		table_close(md_flush.rels[i], md_flush.locks[i]);
	}
	md_flush.active = false;
	if (message_level_is_interesting(DEBUG1))
		elog(DEBUG1, "pgcolumnar metadata flush: opens=%lu tables=%d",
			 (unsigned long) md_flush_opens, md_flush.count);
	md_flush.count = 0;
}

void
PgColumnarResetMetadataFlush(void)
{
	md_flush.active = false;
	md_flush.count = 0;
}

/*
 * #744: close one scan's zone_map handle.
 *
 * Called from PgColumnarEndRead on the normal path. On an error path it is not
 * called at all, and must not be: the resource owner has already released the
 * relation by then, and it does so silently because leak warnings are printed
 * only when isCommit (resowner.c). The session lives in the scan's read state,
 * so the pointer goes away with the scan and can never reach another one.
 */
void
PgColumnarCloseZoneMapSession(PgColumnarZoneMapSession *sess)
{
	if (sess == NULL)
		return;
	if (sess->rel != NULL)
	{
		table_close(sess->rel, AccessShareLock);
		sess->rel = NULL;
	}
	sess->idxOid = InvalidOid;
	if (message_level_is_interesting(DEBUG1))
		elog(DEBUG1, "pgcolumnar zone map %s: probes=%lu opens=%lu",
			 sess->what != NULL ? sess->what : "read",
			 (unsigned long) sess->probes, (unsigned long) sess->opens);
}

/*
 * pgcolumnar_index_oid
 *		The OID of one of the metadata indexes, by name. Returns InvalidOid when
 *		it cannot be resolved, which callers pass through to systable_beginscan
 *		as indexOK = false so the lookup degrades to a heap scan rather than
 *		failing.
 */
static Oid
pgcolumnar_index_oid(const char *name)
{
	return get_relname_relid(name, pgcolumnar_schema_oid());
}

/*
 * pgcolumnar_index_min_blocks
 *		Heap pages at or above which pgcolumnar_scan_index_oid() prefers an
 *		index probe to a sequential scan. Zero probes always; a very large
 *		value never probes.
 *
 *		DERIVED BY MEASUREMENT (#1213). Total buffers for `compact()` over 40
 *		row groups with 20 retired, against every threshold from "always probe"
 *		to "never probe", on PG 15, 17 and 19, over two families of fixture:
 *		many small columnar tables (0 to 1000, catalogs 8 to 65 pages) and one
 *		deep one (catalogs 8 to 390 pages). 24 size points in all.
 *
 *		Scored against the cheapest threshold at each point:
 *
 *		    threshold   total cost above best   worst point   worse than main
 *		            0             1561 buffers          155   10 of 24 points
 *		            2              448                   69    7
 *		            3               19                    4    3
 *		            5               95                   42    none
 *		          8..16          791..3862          162..716   none
 *		        never           15477                 7913     --
 *
 *		3 costs least overall by a factor of five and never more than 4 buffers
 *		at any point. Its whole cost is one shape: a `zone_map` of exactly four
 *		pages, which is worth scanning and gets probed. THE OPTIMUM IS NOT THE
 *		SAME FOR EVERY CATALOG, because the hot ones are scanned more often per
 *		retired group: `row_group` pays for a probe at three pages (probing it
 *		there saves 42), `zone_map` not until five (probing it at four costs 4).
 *		One value for all six is a deliberate compromise, and the measurement
 *		above prices it at 19 buffers across 24 points.
 *
 *		Re-derive rather than adjust. #1217 would cache the index Oid, which
 *		removes the fixed cost of a probe and so moves this number down.
 */
int			pgcolumnar_index_min_blocks = 3;

/*
 * pgcolumnar_scan_index_oid
 *		The index to hand systable_beginscan for a keyed scan of `rel`, or
 *		InvalidOid to read the heap instead.
 *
 *		An index probe is not unconditionally cheaper than a sequential scan.
 *		Reading a heap of n pages costs n buffer touches; probing costs the
 *		btree descent plus the heap fetch, plus the two catcache lookups
 *		pgcolumnar_index_oid() makes on every call. Below a few pages the
 *		sequential scan is the cheaper read.
 *
 *		Both sides of that comparison are reachable in one installation,
 *		because these catalogs are shared by every columnar table in the
 *		database: `row_group` is one page where there is one table and 23 in a
 *		database holding two million rows of columnar data. A path that commits
 *		to one access method is therefore wrong at one end of that range
 *		whichever end it picks, and #1213 measured it wrong at both.
 *
 *		So ask the relation. RelationGetNumberOfBlocks() answers from smgr's
 *		cached block count after the first call in a backend. The decision is
 *		per relation and per call, so a path touching six catalogs of different
 *		sizes picks for each rather than committing all six one way.
 *
 *		pgcolumnar_index_min_blocks is derived by measurement; see its
 *		declaration above.
 */
static Oid
pgcolumnar_scan_index_oid(Relation rel, const char *name)
{
	if (RelationGetNumberOfBlocks(rel) < (BlockNumber) pgcolumnar_index_min_blocks)
		return InvalidOid;
	return pgcolumnar_index_oid(name);
}

/*
 * PgColumnarNextStorageId
 *		Draw the next value from pgcolumnar.storageid_seq (spec 3, 7.6).
 */
uint64
PgColumnarNextStorageId(void)
{
	Oid			nspOid = pgcolumnar_schema_oid();
	Oid			seqOid = get_relname_relid("storageid_seq", nspOid);
	int64		value;

	if (!OidIsValid(seqOid))
		ereport(ERROR,
				(errcode(ERRCODE_UNDEFINED_OBJECT),
				 errmsg("pgcolumnar.storageid_seq does not exist")));

	value = nextval_internal(seqOid, false);
	return (uint64) value;
}

/*
 * PgColumnarCatalogSnapshot
 *		Return a snapshot for reading the columnar metadata catalog that also
 *		sees this transaction's own writes made in the current command (spec 9).
 *		curcid only affects visibility of the current transaction's own tuples,
 *		so advancing it yields read-your-writes without weakening isolation from
 *		other transactions.
 */
Snapshot
PgColumnarCatalogSnapshot(Snapshot base)
{
	Snapshot	copy;
	CommandId	now;

	if (base == NULL || !IsMVCCSnapshot(base))
		return base;

	copy = (Snapshot) palloc(sizeof(SnapshotData));
	*copy = *base;

	now = GetCurrentCommandId(false);
	if (copy->curcid <= now)
		copy->curcid = now + 1;

	return copy;
}

/* -------------------------------------------------------------------------
 * inserts
 * ------------------------------------------------------------------------- */




/* -------------------------------------------------------------------------
 * reads
 * ------------------------------------------------------------------------- */




/*
 * PgColumnarComputeAllVisibleGroups
 *		Return the chunk groups that are all-visible to every snapshot (gap 28
 *		phase 3): the covering stripe's insert xid is frozen or precedes
 *		`oldestXmin` (so every current/future snapshot sees the insert), and the
 *		group has no deletes -- committed *or* in progress. Deletes are checked
 *		under a dirty snapshot so a group being modified concurrently is excluded;
 *		combined with clear-on-write (which removes a bit for any later
 *		delete/insert), this keeps a set bit from ever covering a modified row.
 *		Returns a List of PgColumnarRowRange * (one per all-visible group).
 */
List *
PgColumnarComputeAllVisibleGroups(uint64 storageId, TransactionId oldestXmin)
{
	Relation	grel = open_columnar_table("row_group", AccessShareLock);
	Oid			rgIdx = pgcolumnar_scan_index_oid(grel, "row_group_pkey");
	TupleDesc	gtd = RelationGetDescr(grel);
	ScanKeyData gkey[1];
	SysScanDesc gscan;
	HeapTuple	gtuple;
	Snapshot	snap;
	SnapshotData dirty;
	List	   *ranges = NIL;

	/*
	 * Register the MVCC snapshot: PG18 asserts that a snapshot used for heap
	 * visibility in a scan is registered or active (heapam_visibility.c). Called
	 * from the vacuum path, there is no active snapshot to rely on.
	 *
	 * A row group is all-visible when its catalog row (inserted in the flushing
	 * transaction) precedes oldestXmin and the group has no delete (committed or
	 * in progress, checked under a dirty snapshot). The whole row group is one
	 * all-visible range. Clear-on-write removes the bit for any later delete or
	 * insert, so a set bit never covers a modified row.
	 */
	snap = RegisterSnapshot(GetLatestSnapshot());
	InitDirtySnapshot(dirty);

	ScanKeyInit(&gkey[0], Anum_row_group_storage_id, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) storageId));
	gscan = systable_beginscan(grel, rgIdx, OidIsValid(rgIdx), snap, 1, gkey);
	while (HeapTupleIsValid(gtuple = systable_getnext(gscan)))
	{
		TransactionId xmin = HeapTupleHeaderGetXmin(gtuple->t_data);
		bool		isnull;
		uint64		groupNumber,
					firstRow,
					rowCount;
		List	   *rmList;
		ListCell   *lc;
		bool		hasDelete = false;
		PgColumnarRowRange *r;

		if (TransactionIdIsNormal(xmin) &&
			!TransactionIdPrecedes(xmin, oldestXmin))
			continue;

		groupNumber = (uint64) DatumGetInt64(
			heap_getattr(gtuple, Anum_row_group_group_number, gtd, &isnull));
		rowCount = (uint64) DatumGetInt64(
			heap_getattr(gtuple, Anum_row_group_row_count, gtd, &isnull));
		firstRow = (uint64) DatumGetInt64(
			heap_getattr(gtuple, Anum_row_group_first_row_number, gtd, &isnull));
		if (rowCount == 0)
			continue;

		rmList = PgColumnarReadDeleteVectorList(storageId, groupNumber, &dirty);
		foreach(lc, rmList)
		{
			if (((DeleteVectorMetadata *) lfirst(lc))->deletedCount > 0)
			{
				hasDelete = true;
				break;
			}
		}
		if (hasDelete)
			continue;

		r = palloc(sizeof(PgColumnarRowRange));
		r->firstRowNumber = firstRow;
		r->rowCount = rowCount;
		ranges = lappend(ranges, r);
	}
	systable_endscan(gscan);
	table_close(grel, AccessShareLock);
	UnregisterSnapshot(snap);

	return ranges;
}

/*
 * PgColumnarComputeFullyDeletedGroups
 *		Return the group numbers of row groups every one of whose rows is deleted
 *		as-of oldestXmin -- i.e. the group's catalog row and every delete on it
 *		committed before oldestXmin, so every live snapshot agrees the group is
 *		dead (Phase F3a). Such a group can be retired online (its catalog rows
 *		dropped) with no rewrite and no row-number remap, and the retirement is
 *		race-free: a fully-dead-to-all group has no visible rows for any writer to
 *		delete or any inserter to target, and old-snapshot readers keep the old
 *		catalog version via heap MVCC. Returns a List of palloc'd uint64.
 */
static List *
PgColumnarComputeFullyDeletedGroups(uint64 storageId, TransactionId oldestXmin)
{
	Relation	grel = open_columnar_table("row_group", AccessShareLock);
	Oid			rgIdx = pgcolumnar_scan_index_oid(grel, "row_group_pkey");
	TupleDesc	gtd = RelationGetDescr(grel);
	Relation	mrel = open_columnar_table("delete_vector", AccessShareLock);
	TupleDesc	mtd = RelationGetDescr(mrel);
	ScanKeyData gkey[1];
	SysScanDesc gscan;
	HeapTuple	gtuple;
	Snapshot	snap;
	List	   *groups = NIL;
	Oid			dvIdx = pgcolumnar_index_oid("delete_vector_pkey");

	snap = RegisterSnapshot(GetLatestSnapshot());

	ScanKeyInit(&gkey[0], Anum_row_group_storage_id, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) storageId));
	gscan = systable_beginscan(grel, rgIdx, OidIsValid(rgIdx), snap, 1, gkey);
	while (HeapTupleIsValid(gtuple = systable_getnext(gscan)))
	{
		TransactionId xmin = HeapTupleHeaderGetXmin(gtuple->t_data);
		bool		isnull;
		uint64		groupNumber;
		uint64		rowCount;
		int64		deletedSeen = 0;
		ScanKeyData mkey[2];
		SysScanDesc mscan;
		HeapTuple	mtuple;

		/* only settled groups: created and committed before oldestXmin */
		if (TransactionIdIsNormal(xmin) &&
			!TransactionIdPrecedes(xmin, oldestXmin))
			continue;

		groupNumber = (uint64) DatumGetInt64(
			heap_getattr(gtuple, Anum_row_group_group_number, gtd, &isnull));
		rowCount = (uint64) DatumGetInt64(
			heap_getattr(gtuple, Anum_row_group_row_count, gtd, &isnull));
		if (rowCount == 0)
			continue;

		/*
		 * Sum the deleted-row counts of this group's delete_vector rows, counting only
		 * masks whose own xmin precedes oldestXmin -- those are visible to every
		 * live snapshot. A more recent delete (xmin >= oldestXmin) is not yet
		 * visible to all, so the group is not retired this pass.
		 */
		ScanKeyInit(&mkey[0], Anum_delete_vector_storage_id, BTEqualStrategyNumber,
					F_INT8EQ, Int64GetDatum((int64) storageId));
		ScanKeyInit(&mkey[1], Anum_delete_vector_group_number, BTEqualStrategyNumber,
					F_INT8EQ, Int64GetDatum((int64) groupNumber));
		mscan = systable_beginscan(mrel, dvIdx, OidIsValid(dvIdx), snap, 2, mkey);
		while (HeapTupleIsValid(mtuple = systable_getnext(mscan)))
		{
			TransactionId mxmin = HeapTupleHeaderGetXmin(mtuple->t_data);

			if (TransactionIdIsNormal(mxmin) &&
				!TransactionIdPrecedes(mxmin, oldestXmin))
				continue;
			deletedSeen += DatumGetInt32(
				heap_getattr(mtuple, Anum_delete_vector_deleted_count, mtd, &isnull));
		}
		systable_endscan(mscan);

		if (deletedSeen >= (int64) rowCount)
		{
			uint64	   *g = palloc(sizeof(uint64));

			*g = groupNumber;
			groups = lappend(groups, g);
		}
	}
	systable_endscan(gscan);
	table_close(mrel, AccessShareLock);
	table_close(grel, AccessShareLock);
	UnregisterSnapshot(snap);

	return groups;
}

/*
 * delete_group_rows
 *		Delete every row of a metadata table matching (storageAttno, groupAttno),
 *		probing through indexName.
 *
 *		ONE SOURCE LINE HERE IS FIVE SCAN SITES AT RUN TIME. The relation comes
 *		from a PARAMETER, and PgColumnarDeleteGroupMetadata calls this five
 *		times -- delete_vector, column_chunk, zone_map, bloom, row_group -- so
 *		the single systable_beginscan below ran once per catalog per retired
 *		group. With InvalidOid that was five sequential scans of five catalogs
 *		for every group a compaction retires, walking every OTHER columnar
 *		table's rows each time.
 *
 *		IT WAS INVISIBLE TO BOTH AUDITS OF THESE SCANS (#1207). Each of them
 *		attributed a systable_beginscan to a catalog by reading the
 *		open_columnar_table("<name>") that produced its relation handle, or by
 *		finding the nearest scan key. Neither can resolve a relation that
 *		arrives as an argument: at this call site the catalog has no name. What
 *		found it was a reconciliation rather than a better reading -- probing
 *		all 44 systable_beginscan sites in this file and requiring
 *		sum(probes that ran with InvalidOid) == sum(seq_scan over pgcolumnar),
 *		which failed at 41 counted against 22 probed.
 *
 *		The caller passes the index name because this function cannot derive it:
 *		the pkey of the table named by tableName is what it needs, and only the
 *		caller knows which table that is. Every one of the five is a prefix
 *		match on the two keys below, so none of this needs a new index:
 *
 *		    delete_vector_pkey  (storage_id, group_number)              exact
 *		    row_group_pkey      (storage_id, group_number)              exact
 *		    column_chunk_pkey   (storage_id, group_number, column_index)
 *		    bloom_pkey          (storage_id, group_number, column_index)
 *		    zone_map_pkey       (storage_id, group_number, column_index, ...)
 *
 *		Deleting while scanning through the index is the pattern core uses for
 *		its own catalogs (see deleteDependencyRecordsFor and its siblings): the
 *		scan holds its position by tid, and CatalogTupleDelete updates the index
 *		entries behind it.
 */
static void
delete_group_rows(const char *tableName, const char *indexName,
				  AttrNumber storageAttno,
				  AttrNumber groupAttno, uint64 storageId, uint64 groupNumber)
{
	Relation	rel = open_columnar_table(tableName, RowExclusiveLock);
	Oid			idx = pgcolumnar_scan_index_oid(rel, indexName);
	ScanKeyData key[2];
	SysScanDesc scan;
	HeapTuple	tuple;

	ScanKeyInit(&key[0], storageAttno, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) storageId));
	ScanKeyInit(&key[1], groupAttno, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) groupNumber));

	scan = systable_beginscan(rel, idx, OidIsValid(idx), NULL, 2, key);
	while (HeapTupleIsValid(tuple = systable_getnext(scan)))
		CatalogTupleDelete(rel, &tuple->t_self);
	systable_endscan(scan);

	table_close(rel, RowExclusiveLock);
}

/* read a row group's data byte range; false if the group is not found */
static bool
read_row_group_range(uint64 storageId, uint64 groupNumber,
					 uint64 *fileOffset, uint64 *byteLength)
{
	Relation	rel = open_columnar_table("row_group", AccessShareLock);
	TupleDesc	td = RelationGetDescr(rel);
	Oid			rgIdx = pgcolumnar_scan_index_oid(rel, "row_group_pkey");
	ScanKeyData key[2];
	SysScanDesc scan;
	HeapTuple	tuple;
	Snapshot	snap;
	bool		found = false;

	snap = RegisterSnapshot(GetLatestSnapshot());
	ScanKeyInit(&key[0], Anum_row_group_storage_id, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) storageId));
	ScanKeyInit(&key[1], Anum_row_group_group_number, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) groupNumber));
	/*
	 * row_group_pkey IS (storage_id, group_number), so these two keys are the
	 * whole index. One more sequential scan per retired group, beside the five
	 * delete_group_rows makes (#1207).
	 */
	scan = systable_beginscan(rel, rgIdx, OidIsValid(rgIdx), snap, 2, key);
	if (HeapTupleIsValid(tuple = systable_getnext(scan)))
	{
		bool		isnull;

		*fileOffset = (uint64) DatumGetInt64(
			heap_getattr(tuple, Anum_row_group_file_offset, td, &isnull));
		*byteLength = (uint64) DatumGetInt64(
			heap_getattr(tuple, Anum_row_group_byte_length, td, &isnull));
		found = true;
	}
	systable_endscan(scan);
	UnregisterSnapshot(snap);
	table_close(rel, AccessShareLock);
	return found;
}

/* physical reclaim: split freed ranges on allocate and coalesce on free */
bool		pgcolumnar_reclaim_coalesce = true;

/* insert one free_space row (offset, length page-aligned, freed at freedXid) */
static void
insert_free_space_row(Relation rel, TupleDesc td, uint64 storageId,
					  uint64 fileOffset, uint64 byteLength, TransactionId freedXid)
{
	Datum		values[Natts_free_space];
	bool		nulls[Natts_free_space];
	HeapTuple	tuple;

	memset(nulls, false, sizeof(nulls));
	values[Anum_free_space_storage_id - 1] = Int64GetDatum((int64) storageId);
	values[Anum_free_space_file_offset - 1] = Int64GetDatum((int64) fileOffset);
	values[Anum_free_space_byte_length - 1] = Int64GetDatum((int64) byteLength);
	values[Anum_free_space_freed_xid - 1] = Int64GetDatum((int64) freedXid);

	tuple = heap_form_tuple(td, values, nulls);
	CatalogTupleInsert(rel, tuple);
	heap_freetuple(tuple);
}

/*
 * record_free_space
 *		Record a freed data range for later reuse (Phase F physical reclaim). The
 *		range is stored as its page-aligned footprint (the group's data rounded up
 *		to a whole page), so free ranges tile the file page-aligned and a split
 *		remnant or a coalesced union stays page-aligned.
 *
 *		When pgcolumnar.reclaim_coalesce is on, the new range is merged with any
 *		immediately adjacent existing free range (left neighbor ending at this
 *		offset, or right neighbor starting at this range's end) before insertion,
 *		so a later request larger than either neighbor can still be satisfied from
 *		contiguous free space. The merged freeing xid is the newest of the merged
 *		ranges, so reuse stays gated until every component is behind the horizon.
 */
static void
record_free_space(uint64 storageId, uint64 fileOffset, uint64 byteLength)
{
	Relation	rel = open_columnar_table("free_space", RowExclusiveLock);
	TupleDesc	td = RelationGetDescr(rel);
	uint64		off = fileOffset;
	uint64		len = COLUMNAR_PAGE_ROUND_UP(byteLength);
	uint64		origOff = off;
	uint64		origEnd = off + len;
	TransactionId freedXid = GetCurrentTransactionId();

	if (pgcolumnar_reclaim_coalesce)
	{
		Snapshot	snap = RegisterSnapshot(GetLatestSnapshot());
		ScanKeyData key[1];
		SysScanDesc scan;
		HeapTuple	tuple;
		ItemPointerData mergeTids[2];
		int			nMerge = 0;

		Oid			fsIdx = pgcolumnar_scan_index_oid(rel, "free_space_pkey");

		ScanKeyInit(&key[0], Anum_free_space_storage_id, BTEqualStrategyNumber,
					F_INT8EQ, Int64GetDatum((int64) storageId));
		/*
		 * storage_id leads free_space_pkey (storage_id, file_offset), so the
		 * one key here is a prefix probe. The loop is order-independent: it
		 * tests adjacency against the ORIGINAL range bounds, not against the
		 * accumulating ones, and takes Min() of the offsets, so reading the
		 * rows in index order rather than heap order cannot change which
		 * neighbours merge (#1207).
		 *
		 * WHAT THAT ARGUMENT RESTS ON, said out loud because it is not
		 * self-evident (@OffgridwithJD, #1213 review). Order-independence needs
		 * `nMerge < 2` below to be a cap that never binds, and it never binds
		 * only because there is AT MOST one left neighbour and one right
		 * neighbour. With three matches `len` would absorb all three while only
		 * two rows were deleted, leaving a double-counted extent. The invariant
		 * that makes three impossible -- file_offset unique, no overlap -- is
		 * enforced by PgColumnarCheckFreeSpaceNoOverlap, which is ASSERT-ONLY.
		 * So the cap's safety and that checker are one argument, not two
		 * independent ones, and a release build carries the invariant without
		 * checking it. The invariant does hold; it is the independence that
		 * does not.
		 */
		scan = systable_beginscan(rel, fsIdx, OidIsValid(fsIdx), snap, 1, key);
		while (HeapTupleIsValid(tuple = systable_getnext(scan)))
		{
			bool		isnull;
			uint64		noff = (uint64) DatumGetInt64(
				heap_getattr(tuple, Anum_free_space_file_offset, td, &isnull));
			uint64		nlen = (uint64) DatumGetInt64(
				heap_getattr(tuple, Anum_free_space_byte_length, td, &isnull));
			TransactionId nxid = (TransactionId) DatumGetInt64(
				heap_getattr(tuple, Anum_free_space_freed_xid, td, &isnull));

			/*
			 * Only coalesce ranges freed by THIS transaction (same freed_xid).
			 * Merging a fresh free with an older, already-reusable neighbor would
			 * force the union to the newer xid (required for safety: reuse must
			 * wait until every merged component is behind the horizon), which would
			 * delay reuse of space that was reusable a moment ago -- a net loss.
			 * Same-xid coalescing still merges the many groups one maintenance
			 * operation retires together, which is the fragmentation case that
			 * matters, without poisoning older reusable ranges.
			 *
			 * Adjacency is tested against the ORIGINAL range bounds. The file is
			 * already maximally coalesced before this insert, so there is at most
			 * one left neighbor (ends at origOff) and one right neighbor (starts
			 * at origEnd); testing against the accumulating bounds would be wrong.
			 */
			if (nxid == freedXid && (noff + nlen == origOff || noff == origEnd))
			{
				off = Min(off, noff);
				len += nlen;
				if (nMerge < 2)
					mergeTids[nMerge++] = tuple->t_self;
			}
		}
		systable_endscan(scan);

		if (nMerge > 0)
		{
			int			i;

			for (i = 0; i < nMerge; i++)
				CatalogTupleDelete(rel, &mergeTids[i]);
			/* make the deletes visible before inserting the merged row so the
			 * unique (storage_id, file_offset) key cannot collide with a just-
			 * deleted neighbor, and so a later record in this command sees it */
			CommandCounterIncrement();
		}
		UnregisterSnapshot(snap);
	}

	insert_free_space_row(rel, td, storageId, off, len, freedXid);

	/*
	 * LOAD-BEARING FOR #84 IN THE DEFAULT CONFIGURATION, not an optimisation
	 * detail (#1194). It makes the new free_space row visible to the next scan
	 * within the same command, which is the same visibility the #84 fix provides
	 * in PgColumnarAllocateFreeSpace -- so with coalescing on, this increment
	 * masks #84 entirely.
	 *
	 * Measured on PG17. Remove BOTH increments and the SHIPPED configuration
	 * breaks; the regression guard reaches the defect only because it sets
	 * reclaim_coalesce off:
	 *
	 *     #84 fix removed, this one kept      12 passed + 0 failed  (masked)
	 *     both removed, coalescing default     8 passed + 4 failed
	 *       ERROR: tuple already updated by self
	 *
	 * So deleting this line is safe only while the #84 fix is present, and
	 * nothing in the tree would catch it being deleted together with that fix.
	 * test/native_reclaim_cycles.sh guards #84 with coalescing OFF, which is a
	 * configuration no production cluster uses.
	 */
	if (pgcolumnar_reclaim_coalesce)
		CommandCounterIncrement();
	table_close(rel, RowExclusiveLock);
}

/*
 * PgColumnarRetireGroup
 *		Drop all catalog rows for one row group (row_group, column_chunk,
 *		zone_map, bloom, delete_vector) in the current transaction (Phase F3a). The
 *		storage row is left intact. Heap MVCC on these deletes keeps the group
 *		readable to older snapshots. The group's data byte range is recorded in
 *		free_space so a later reservation can reuse it once the oldest-xmin horizon
 *		passes this transaction (physical reclaim); the blocks are not freed here.
 *		The caller must have verified the group is fully deleted as-of oldestXmin.
 */
void
PgColumnarRetireGroup(uint64 storageId, uint64 groupNumber)
{
	uint64		fileOffset = 0;
	uint64		byteLength = 0;
	bool		haveRange = read_row_group_range(storageId, groupNumber,
												 &fileOffset, &byteLength);

	delete_group_rows("delete_vector", "delete_vector_pkey", Anum_delete_vector_storage_id,
					  Anum_delete_vector_group_number, storageId, groupNumber);
	delete_group_rows("column_chunk", "column_chunk_pkey", Anum_column_chunk_storage_id,
					  Anum_column_chunk_group_number, storageId, groupNumber);
	delete_group_rows("zone_map", "zone_map_pkey", Anum_zone_map_storage_id,
					  Anum_zone_map_group_number, storageId, groupNumber);
	delete_group_rows("bloom", "bloom_pkey", Anum_bloom_storage_id,
					  Anum_bloom_group_number, storageId, groupNumber);
	delete_group_rows("row_group", "row_group_pkey", Anum_row_group_storage_id,
					  Anum_row_group_group_number, storageId, groupNumber);

	/*
	 * A group-list memo built earlier in the SAME command would serve this
	 * group after these deletes -- with its delete vectors just gone,
	 * resurrecting rows -- so drop the memo with the group (#709). This
	 * reset was once removed on the theory that every reachable
	 * same-command retirement crosses a CommandCounterIncrement first
	 * (changing the memo's key), and on PG17 the rewrite path does. On PG18
	 * it does not: the same-statement rewrite arm of
	 * native_fetch_group_memo.sh read one doubled row there (the first
	 * post-rewrite probe hit the stale memo through the old index entry
	 * before refresh-on-miss healed it). The CCI is an accident of the
	 * flush's storage-row update, major-dependent, and nothing to lean on;
	 * that arm is this reset's removal proof.
	 */
	PgColumnarGroupMemoReset(true);

	if (haveRange && byteLength > 0)
		record_free_space(storageId, fileOffset, byteLength);
}

/*
 * PgColumnarAllocateFreeSpace
 *		Try to satisfy a data reservation of dataLength bytes from a previously
 *		freed range (Phase F physical reclaim). Returns true and sets *fileOffset
 *		to a page-aligned freed range that is large enough AND whose freeing
 *		transaction precedes oldestXmin (so no snapshot can still read its old
 *		contents), consuming that free_space row. Best-fit (smallest fitting range)
 *		to limit waste. Reservations are serialized by the relation extension lock,
 *		so no two callers race for the same row.
 */
bool
PgColumnarAllocateFreeSpace(uint64 storageId, uint64 dataLength,
						  TransactionId oldestXmin, uint64 *fileOffset)
{
	Relation	rel = open_columnar_table("free_space", RowExclusiveLock);
	TupleDesc	td = RelationGetDescr(rel);
	ScanKeyData key[1];
	SysScanDesc scan;
	HeapTuple	tuple;
	Snapshot	snap;
	bool		found = false;
	uint64		bestOff = 0;
	uint64		bestLen = 0;
	TransactionId bestXid = InvalidTransactionId;
	ItemPointerData bestTid;

	ItemPointerSetInvalid(&bestTid);
	snap = RegisterSnapshot(GetLatestSnapshot());
	ScanKeyInit(&key[0], Anum_free_space_storage_id, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) storageId));
	scan = systable_beginscan(rel, InvalidOid, false, snap, 1, key);
	while (HeapTupleIsValid(tuple = systable_getnext(scan)))
	{
		bool		isnull;
		uint64		len = (uint64) DatumGetInt64(
			heap_getattr(tuple, Anum_free_space_byte_length, td, &isnull));
		TransactionId fxid = (TransactionId) DatumGetInt64(
			heap_getattr(tuple, Anum_free_space_freed_xid, td, &isnull));

		if (len < dataLength)
			continue;
		/* not reusable until every live snapshot has passed the freeing xid */
		if (TransactionIdIsNormal(fxid) &&
			!TransactionIdPrecedes(fxid, oldestXmin))
			continue;
		if (!found || len < bestLen)
		{
			found = true;
			bestLen = len;
			bestXid = fxid;
			bestOff = (uint64) DatumGetInt64(
				heap_getattr(tuple, Anum_free_space_file_offset, td, &isnull));
			bestTid = tuple->t_self;
		}
	}
	systable_endscan(scan);

	if (found)
	{
		CatalogTupleDelete(rel, &bestTid);
		*fileOffset = bestOff;

		/*
		 * Split: the reservation consumes a whole number of pages (allocLen), so
		 * the remnant beyond it stays page-aligned and reusable. Record it back
		 * with the same freeing xid as the consumed range (already frozen, so the
		 * same oldest-xmin gate still applies). Without this the tail of an
		 * oversized freed range would leak until its group is re-freed.
		 */
		if (pgcolumnar_reclaim_coalesce)
		{
			uint64		allocLen = COLUMNAR_PAGE_ROUND_UP(dataLength);

			if (bestLen > allocLen)
				insert_free_space_row(rel, td, storageId, bestOff + allocLen,
									  bestLen - allocLen, bestXid);
		}

		/*
		 * A single compaction command can allocate many blocks in a row. Make
		 * this consumption (and any remnant) visible to the next allocation's scan
		 * within the same command; otherwise it would still see this row as free,
		 * re-select it, and fail with "tuple already updated by self" on the
		 * second delete.
		 */
		CommandCounterIncrement();
	}
	UnregisterSnapshot(snap);
	table_close(rel, RowExclusiveLock);
	return found;
}

#ifdef USE_ASSERT_CHECKING
typedef struct ReclaimRange
{
	uint64		off;
	uint64		len;
} ReclaimRange;

static int
reclaim_range_cmp(const void *a, const void *b)
{
	uint64		oa = ((const ReclaimRange *) a)->off;
	uint64		ob = ((const ReclaimRange *) b)->off;

	if (oa < ob)
		return -1;
	if (oa > ob)
		return 1;
	return 0;
}

/*
 * PgColumnarCheckFreeSpaceNoOverlap
 *		Assert-only invariant check for physical reclaim: a storage's live
 *		row-group footprints (data rounded up to whole pages) and its free_space
 *		ranges must not overlap. An overlap would mean a reused block was handed
 *		out on top of live data or another free range, i.e. corruption. Called at
 *		the end of the online maintenance operations in assert builds.
 */
void
PgColumnarCheckFreeSpaceNoOverlap(uint64 storageId)
{
	Relation	rg;
	Relation	fs;
	Oid			rgIdx;
	Oid			fsIdx;
	TupleDesc	rgd;
	TupleDesc	fsd;
	Snapshot	snap;
	ScanKeyData key[1];
	SysScanDesc scan;
	HeapTuple	t;
	ReclaimRange *ranges;
	int			n = 0;
	int			cap = 64;
	int			i;

	/* see this transaction's own writes made so far in the command */
	CommandCounterIncrement();
	snap = RegisterSnapshot(GetLatestSnapshot());
	rg = open_columnar_table("row_group", AccessShareLock);
	fs = open_columnar_table("free_space", AccessShareLock);
	rgIdx = pgcolumnar_scan_index_oid(rg, "row_group_pkey");
	fsIdx = pgcolumnar_scan_index_oid(fs, "free_space_pkey");
	rgd = RelationGetDescr(rg);
	fsd = RelationGetDescr(fs);
	ranges = palloc(sizeof(ReclaimRange) * cap);

	ScanKeyInit(&key[0], Anum_row_group_storage_id, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) storageId));
	/*
	 * ASSERT-ONLY, AND THAT IS EXACTLY WHY IT WAS THE LAST ONE FOUND. This
	 * check does not run in a release build, so a measurement taken on one
	 * reports zero sequential scans here while every assert-enabled CI leg pays
	 * two per maintenance operation. Both keys below lead their table's primary
	 * key, and the loops qsort what they collect, so index order changes
	 * nothing (#1207).
	 */
	scan = systable_beginscan(rg, rgIdx, OidIsValid(rgIdx), snap, 1, key);
	while (HeapTupleIsValid(t = systable_getnext(scan)))
	{
		bool		isnull;
		uint64		off = (uint64) DatumGetInt64(
			heap_getattr(t, Anum_row_group_file_offset, rgd, &isnull));
		uint64		blen = (uint64) DatumGetInt64(
			heap_getattr(t, Anum_row_group_byte_length, rgd, &isnull));

		if (blen == 0)
			continue;
		if (n == cap)
			ranges = repalloc(ranges, sizeof(ReclaimRange) * (cap *= 2));
		ranges[n].off = off;
		ranges[n].len = COLUMNAR_PAGE_ROUND_UP(blen);
		n++;
	}
	systable_endscan(scan);

	ScanKeyInit(&key[0], Anum_free_space_storage_id, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) storageId));
	scan = systable_beginscan(fs, fsIdx, OidIsValid(fsIdx), snap, 1, key);
	while (HeapTupleIsValid(t = systable_getnext(scan)))
	{
		bool		isnull;
		uint64		off = (uint64) DatumGetInt64(
			heap_getattr(t, Anum_free_space_file_offset, fsd, &isnull));
		uint64		len = (uint64) DatumGetInt64(
			heap_getattr(t, Anum_free_space_byte_length, fsd, &isnull));

		if (len == 0)
			continue;
		if (n == cap)
			ranges = repalloc(ranges, sizeof(ReclaimRange) * (cap *= 2));
		ranges[n].off = off;
		ranges[n].len = len;
		n++;
	}
	systable_endscan(scan);

	qsort(ranges, n, sizeof(ReclaimRange), reclaim_range_cmp);
	for (i = 1; i < n; i++)
	{
		if (ranges[i].off < ranges[i - 1].off + ranges[i - 1].len)
			elog(ERROR,
				 "columnar reclaim: overlapping ranges [" UINT64_FORMAT ",+"
				 UINT64_FORMAT ") and [" UINT64_FORMAT ",+" UINT64_FORMAT ")",
				 ranges[i - 1].off, ranges[i - 1].len,
				 ranges[i].off, ranges[i].len);
	}

	pfree(ranges);
	table_close(fs, AccessShareLock);
	table_close(rg, AccessShareLock);
	UnregisterSnapshot(snap);
}
#endif							/* USE_ASSERT_CHECKING */

/*
 * PgColumnarTrailingFreeSpaceSafe
 *		Physical end-truncation guard: return false if any free_space row for this
 *		storage at or above liveEnd was freed at a transaction the oldest-xmin
 *		horizon has NOT passed. Such a row is a recently retired group whose bytes
 *		lie in the truncation region; a snapshot older than that retirement can
 *		still see the group and read those bytes, so truncating them would fault
 *		that reader. Only when every trailing free range is behind the horizon is
 *		the tail safe to drop.
 */
bool
PgColumnarTrailingFreeSpaceSafe(uint64 storageId, uint64 liveEnd,
							  TransactionId oldestXmin)
{
	Relation	rel = open_columnar_table("free_space", AccessShareLock);
	TupleDesc	td = RelationGetDescr(rel);
	Snapshot	snap = RegisterSnapshot(GetLatestSnapshot());
	ScanKeyData key[1];
	SysScanDesc scan;
	HeapTuple	tuple;
	bool		safe = true;

	ScanKeyInit(&key[0], Anum_free_space_storage_id, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) storageId));
	scan = systable_beginscan(rel, InvalidOid, false, snap, 1, key);
	while (HeapTupleIsValid(tuple = systable_getnext(scan)))
	{
		bool		isnull;
		uint64		off = (uint64) DatumGetInt64(
			heap_getattr(tuple, Anum_free_space_file_offset, td, &isnull));
		TransactionId fxid = (TransactionId) DatumGetInt64(
			heap_getattr(tuple, Anum_free_space_freed_xid, td, &isnull));

		if (off < liveEnd)
			continue;
		if (TransactionIdIsNormal(fxid) &&
			!TransactionIdPrecedes(fxid, oldestXmin))
		{
			safe = false;
			break;
		}
	}
	systable_endscan(scan);
	UnregisterSnapshot(snap);
	table_close(rel, AccessShareLock);
	return safe;
}

/*
 * PgColumnarDeleteFreeSpaceAtOrAbove
 *		Physical end-truncation: drop every free_space row for this storage whose
 *		offset is at or above liveEnd. Those ranges are being physically removed
 *		from the file, so they must no longer appear as reusable space. The caller
 *		holds AccessExclusiveLock and has verified the tail is safe.
 */
void
PgColumnarDeleteFreeSpaceAtOrAbove(uint64 storageId, uint64 liveEnd)
{
	Relation	rel = open_columnar_table("free_space", RowExclusiveLock);
	TupleDesc	td = RelationGetDescr(rel);
	Snapshot	snap = RegisterSnapshot(GetLatestSnapshot());
	ScanKeyData key[1];
	SysScanDesc scan;
	HeapTuple	tuple;
	List	   *tids = NIL;
	ListCell   *lc;

	ScanKeyInit(&key[0], Anum_free_space_storage_id, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) storageId));
	scan = systable_beginscan(rel, InvalidOid, false, snap, 1, key);
	while (HeapTupleIsValid(tuple = systable_getnext(scan)))
	{
		bool		isnull;
		uint64		off = (uint64) DatumGetInt64(
			heap_getattr(tuple, Anum_free_space_file_offset, td, &isnull));

		if (off >= liveEnd)
		{
			ItemPointer tid = palloc(sizeof(ItemPointerData));

			*tid = tuple->t_self;
			tids = lappend(tids, tid);
		}
	}
	systable_endscan(scan);

	foreach(lc, tids)
		CatalogTupleDelete(rel, (ItemPointer) lfirst(lc));

	UnregisterSnapshot(snap);
	table_close(rel, RowExclusiveLock);
}

typedef struct FootRange
{
	uint64		off;
	uint64		end;
} FootRange;

static int
footrange_cmp(const void *a, const void *b)
{
	uint64		oa = ((const FootRange *) a)->off;
	uint64		ob = ((const FootRange *) b)->off;

	if (oa < ob)
		return -1;
	if (oa > ob)
		return 1;
	return 0;
}

/* append live row-group footprints for one storage to foots[] */
static void
collect_footprints(uint64 storageId, Snapshot snap, FootRange **foots,
				   int *nf, int *capf)
{
	List	   *rgs = PgColumnarReadRowGroupList(storageId, snap);
	ListCell   *lc;

	foreach(lc, rgs)
	{
		NativeRowGroupMetadata *rg = (NativeRowGroupMetadata *) lfirst(lc);

		if (rg->byteLength == 0)
			continue;
		if (*nf == *capf)
			*foots = repalloc(*foots, sizeof(FootRange) * (*capf *= 2));
		(*foots)[*nf].off = rg->fileOffset;
		(*foots)[*nf].end = rg->fileOffset + COLUMNAR_PAGE_ROUND_UP(rg->byteLength);
		(*nf)++;
	}
}

/*
 * Does [start, end) overlap any footprint? foots is sorted ascending by offset
 * and the footprints are non-overlapping (they tile live groups), so ends are
 * also ascending; the footprint with the largest offset below `end` has the
 * largest end among candidates, so one binary search plus one check settles it.
 */
static bool
range_overlaps_footprint(uint64 start, uint64 end, FootRange *foots, int nf)
{
	int			lo = 0;
	int			hi = nf;			/* find last index with off < end */

	while (lo < hi)
	{
		int			mid = (lo + hi) / 2;

		if (foots[mid].off < end)
			lo = mid + 1;
		else
			hi = mid;
	}
	/* lo is the count of footprints with off < end; check the last one's end */
	return lo > 0 && foots[lo - 1].end > start;
}

/*
 * PgColumnarReconcileFreeList
 *		Delete any free_space row (for the base or any projection storage of this
 *		relation) that overlaps a LIVE row-group footprint as-of the latest
 *		committed state.
 *
 *		Retirement is atomic on this catalog design (the row_group delete and the
 *		free_space insert are both transactional), so a normal aborted compaction
 *		leaves no stale entries. The one seam that remains is physical
 *		end-truncation: it lowers the metapage highwater (a non-transactional
 *		full-page-image write that persists on abort) while deleting free_space
 *		rows (transactional, which roll back). A crash in the narrow window between
 *		the highwater lowering and commit can therefore leave a lowered highwater
 *		with the trailing free rows restored; a later insert then places a live
 *		group over one of those ranges. Running this at the start of every reuse
 *		(compact_rewrite, recluster), under ShareUpdateExclusiveLock and before any
 *		PgColumnarAllocateFreeSpace, drops such an entry before it can be handed out
 *		on top of a live group. Inserts never reuse, so no stale entry is consumed
 *		between the crash and the next reuse.
 */
void
PgColumnarReconcileFreeList(Relation dataRel)
{
	uint64		base = PgColumnarStorageId(dataRel);
	List	   *projs = PgColumnarListProjections(base);
	ListCell   *lc;
	Snapshot	snap;
	FootRange  *foots;
	int			nf = 0;
	int			capf = 64;
	Relation	fsrel;
	Oid			fsIdx;
	TupleDesc	td;
	List	   *storages = NIL;
	List	   *tids = NIL;

	foots = palloc(sizeof(FootRange) * capf);
	snap = RegisterSnapshot(GetLatestSnapshot());

	/* live footprints and the storage id set (base + distinct projections). Store
	 * storage ids as heap uint64s, not stuffed into pointers, so an id > 2^32 is
	 * safe on 32-bit builds (matches pgcolumnar_end_truncation_storages). */
	collect_footprints(base, snap, &foots, &nf, &capf);
	{
		uint64	   *s = palloc(sizeof(uint64));

		*s = base;
		storages = lappend(storages, s);
	}
	foreach(lc, projs)
	{
		PgColumnarProjection *p = (PgColumnarProjection *) lfirst(lc);

		if (p->projStorageId != base)
		{
			uint64	   *s = palloc(sizeof(uint64));

			collect_footprints(p->projStorageId, snap, &foots, &nf, &capf);
			*s = p->projStorageId;
			storages = lappend(storages, s);
		}
	}

	/* sort once so each free row is checked with a binary search, not a linear
	 * scan of every footprint (this runs on every reuse op, usually purging
	 * nothing) */
	qsort(foots, nf, sizeof(FootRange), footrange_cmp);

	fsrel = open_columnar_table("free_space", RowExclusiveLock);
	fsIdx = pgcolumnar_scan_index_oid(fsrel, "free_space_pkey");
	td = RelationGetDescr(fsrel);
	foreach(lc, storages)
	{
		uint64		sid = *(uint64 *) lfirst(lc);
		ScanKeyData key[1];
		SysScanDesc scan;
		HeapTuple	tuple;

		ScanKeyInit(&key[0], Anum_free_space_storage_id, BTEqualStrategyNumber,
					F_INT8EQ, Int64GetDatum((int64) sid));
		/*
		 * storage_id leads free_space_pkey. The loop tests each row against the
		 * footprints independently and collects tids, deleting only after every
		 * scan has finished, so index order rather than heap order changes
		 * nothing about which rows are collected (#1207).
		 */
		scan = systable_beginscan(fsrel, fsIdx, OidIsValid(fsIdx), snap, 1, key);
		while (HeapTupleIsValid(tuple = systable_getnext(scan)))
		{
			bool		isnull;
			uint64		off = (uint64) DatumGetInt64(
				heap_getattr(tuple, Anum_free_space_file_offset, td, &isnull));
			uint64		len = (uint64) DatumGetInt64(
				heap_getattr(tuple, Anum_free_space_byte_length, td, &isnull));

			if (range_overlaps_footprint(off, off + len, foots, nf))
			{
				ItemPointer tid = palloc(sizeof(ItemPointerData));

				*tid = tuple->t_self;
				tids = lappend(tids, tid);
			}
		}
		systable_endscan(scan);
	}

	foreach(lc, tids)
		CatalogTupleDelete(fsrel, (ItemPointer) lfirst(lc));
	if (tids != NIL)
		CommandCounterIncrement();

	table_close(fsrel, RowExclusiveLock);
	UnregisterSnapshot(snap);
	list_free_deep(storages);
	list_free_deep(tids);
	pfree(foots);
}

/*
 * PgColumnarRetireFullyDeletedGroups
 *		Online compaction (Phase F3a, lazy path): retire every row group that is
 *		fully deleted as-of oldestXmin. Safe under ShareUpdateExclusiveLock,
 *		concurrent with readers and writers. Returns the number of groups retired.
 */
int64
PgColumnarRetireFullyDeletedGroups(Relation rel)
{
	uint64		storageId = PgColumnarStorageId(rel);
	TransactionId oldestXmin = PgColumnarOldestXmin(rel);
	List	   *groups = PgColumnarComputeFullyDeletedGroups(storageId, oldestXmin);
	ListCell   *lc;
	int64		retired = 0;

	foreach(lc, groups)
	{
		PgColumnarRetireGroup(storageId, *(uint64 *) lfirst(lc));
		retired++;
	}
	return retired;
}



/*
 * PgColumnarReadDeleteVectorList
 *		Read all delete_vector rows for a stripe (spec 7.5). Returns a list of
 *		DeleteVectorMetadata* allocated in the current memory context.
 */
List *
PgColumnarReadDeleteVectorList(uint64 storageId, uint64 stripeId, Snapshot snapshot)
{
	Relation	rel = open_columnar_table("delete_vector", AccessShareLock);
	TupleDesc	tupdesc = RelationGetDescr(rel);
	ScanKeyData key[2];
	SysScanDesc scan;
	HeapTuple	tuple;
	List	   *result = NIL;
	Oid			dvIdx = pgcolumnar_index_oid("delete_vector_pkey");

	ScanKeyInit(&key[0], Anum_delete_vector_storage_id, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) storageId));
	ScanKeyInit(&key[1], Anum_delete_vector_group_number, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) stripeId));

	scan = systable_beginscan(rel, dvIdx, OidIsValid(dvIdx), snapshot, 2, key);
	while (HeapTupleIsValid(tuple = systable_getnext(scan)))
	{
		DeleteVectorMetadata *rm = palloc0(sizeof(DeleteVectorMetadata));
		bool		isnull;
		Datum		bitmapDatum;

		rm->groupNumber = stripeId;
		rm->deletedCount = DatumGetInt32(
			heap_getattr(tuple, Anum_delete_vector_deleted_count, tupdesc, &isnull));

		bitmapDatum = heap_getattr(tuple, Anum_delete_vector_bitmap, tupdesc, &isnull);
		if (!isnull)
		{
			bytea	   *bitmapb = DatumGetByteaP(bitmapDatum);

			rm->bitmapLen = VARSIZE(bitmapb) >= VARHDRSZ ?
				(uint32) (VARSIZE(bitmapb) - VARHDRSZ) : 0;
			rm->bitmap = palloc(rm->bitmapLen + 1);
			memcpy(rm->bitmap, VARDATA(bitmapb), rm->bitmapLen);
		}
		else
		{
			rm->bitmap = NULL;
			rm->bitmapLen = 0;
		}

		result = lappend(result, rm);
	}
	systable_endscan(scan);
	table_close(rel, AccessShareLock);

	return result;
}

/*
 * PgColumnarStorageHasDeleteVector
 *		True when the storage has any delete_vector row, i.e. at least one delete has
 *		been recorded. Used to decide whether the native zone-map-only aggregate
 *		is valid (it is only correct when no rows are deleted) or must fall back to
 *		a delete-applying scan (D6b).
 */
bool
PgColumnarStorageHasDeleteVector(uint64 storageId, Snapshot snapshot)
{
	Relation	rel = open_columnar_table("delete_vector", AccessShareLock);
	ScanKeyData key[1];
	SysScanDesc scan;
	bool		found;
	Oid			dvIdx = pgcolumnar_index_oid("delete_vector_pkey");

	ScanKeyInit(&key[0], Anum_delete_vector_storage_id, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) storageId));
	scan = systable_beginscan(rel, dvIdx, OidIsValid(dvIdx), snapshot, 1, key);
	found = HeapTupleIsValid(systable_getnext(scan));
	systable_endscan(scan);
	table_close(rel, AccessShareLock);

	return found;
}

/*
 * PgColumnarStorageDeletedCount
 *		Total rows marked deleted across the whole storage, read from
 *		delete_vector.deleted_count.
 *
 *		One index scan over the storage's delete_vector rows, summing a stored
 *		integer, rather than one scan and a bitmap walk per row group. The
 *		planner calls this on every plan of a columnar relation, so the cost is
 *		paid per plan and not per query.
 *
 *		deleted_count is the number of set bits in that row's bitmap, maintained
 *		where the bitmap is written, so summing it is exact rather than an
 *		approximation -- the unique index on (storage_id, group_number) means
 *		one row per group, so no two summands can count the same row.
 */
uint64
PgColumnarStorageDeletedCount(uint64 storageId, Snapshot snapshot)
{
	Relation	rel = open_columnar_table("delete_vector", AccessShareLock);
	ScanKeyData key[1];
	SysScanDesc scan;
	HeapTuple	tup;
	TupleDesc	tupdesc = RelationGetDescr(rel);
	uint64		total = 0;
	Oid			dvIdx = pgcolumnar_index_oid("delete_vector_pkey");

	ScanKeyInit(&key[0], Anum_delete_vector_storage_id, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) storageId));
	scan = systable_beginscan(rel, dvIdx, OidIsValid(dvIdx), snapshot, 1, key);
	while (HeapTupleIsValid(tup = systable_getnext(scan)))
	{
		bool		isnull;
		Datum		d = heap_getattr(tup, Anum_delete_vector_deleted_count,
									 tupdesc, &isnull);

		if (!isnull)
		{
			int32		n = DatumGetInt32(d);

			if (n > 0)
				total += (uint64) n;
		}
	}
	systable_endscan(scan);
	table_close(rel, AccessShareLock);

	return total;
}

/*
 * delete_vector_chunk_lock_key
 *		Mix the identity of a chunk group into a 64-bit advisory-lock key. The
 *		triple (storage_id, stripe_id, chunk_id) uniquely names a chunk group
 *		(start_row_number is a function of the stripe and chunk), so it is the
 *		full key. A finalizing avalanche spreads the bits so distinct chunk
 *		groups almost never share a key; a collision only makes two unrelated
 *		chunk groups serialize needlessly, it never affects correctness.
 */
static uint64
delete_vector_chunk_lock_key(uint64 storageId, uint64 stripeId, int chunkId)
{
	uint64		h = 1469598103934665603UL;	/* FNV-1a 64-bit offset basis */

	h = (h ^ storageId) * 1099511628211UL;
	h = (h ^ stripeId) * 1099511628211UL;
	h = (h ^ (uint64) (uint32) chunkId) * 1099511628211UL;

	/* splitmix64/murmur3 finalizer for a good avalanche */
	h ^= h >> 33;
	h *= 0xff51afd7ed558ccdUL;
	h ^= h >> 33;
	h *= 0xc4ceb9fe1a85ec53UL;
	h ^= h >> 33;

	return h;
}

/*
 * delete_vector_lock_chunk_group
 *		Take a transaction-scoped exclusive lock covering one chunk group's
 *		delete_vector tuple, so that the read-modify-write in PgColumnarUpsertDeleteVector
 *		serializes against any concurrent deleter or updater touching the SAME
 *		chunk group, while deletes to different chunk groups still proceed
 *		concurrently. The lock is held until this transaction ends (commit or
 *		abort), which is required: a concurrent deleter that acquires the lock
 *		after us must not run its SnapshotSelf read until our merged tuple is
 *		committed and therefore visible to it.
 *
 *		An advisory lock tag is used because the delete_vector heap tuple to protect
 *		may not exist yet on the first delete of a chunk group; the key names
 *		the chunk group itself, so the first-insert race is serialized too. The
 *		lock is taken with a plain wait (deadlock-detector armed); callers must
 *		acquire chunk-group locks in a consistent global order (see
 *		delete_vector_flush_buffer) so two transactions cannot form an AB-BA cycle.
 */
static void
delete_vector_lock_chunk_group(uint64 storageId, uint64 stripeId, int chunkId)
{
	LOCKTAG		tag;
	uint64		key = delete_vector_chunk_lock_key(storageId, stripeId, chunkId);

	SET_LOCKTAG_ADVISORY(tag, MyDatabaseId,
						 (uint32) (key >> 32), (uint32) (key & 0xFFFFFFFF),
						 PGCOLUMNAR_LOCKCLASS_DELETE_VECTOR);

	(void) LockAcquire(&tag, ExclusiveLock, false /* transaction lock */ ,
					   false /* wait */ );
}

/*
 * PgColumnarUpsertDeleteVector
 *		Insert or replace the delete_vector row for one chunk group, identified by
 *		(storage_id, group_number). If a row already exists it is replaced with the
 *		merged bitmap carried in rm; otherwise a fresh row is inserted. Used at
 *		flush of the in-memory delete buffer (pgcolumnar_delete_vector.c), at most
 *		once per chunk group per flush, so a single heap tuple is never updated
 *		twice in the same command.
 *
 *		Concurrency (issue #4): the whole read-modify-write is guarded by a
 *		transaction-scoped chunk-group lock (delete_vector_lock_chunk_group), so two
 *		transactions deleting different rows in the same chunk group serialize
 *		instead of overwriting each other's delete bits. Once the lock is held,
 *		any earlier writer to this chunk group has committed, so the existing
 *		row is located with SnapshotSelf (which also sees this transaction's own
 *		prior modifications) and its bits are merged (bitwise OR) into rm->bitmap.
 *		Because no concurrent writer can touch the tuple while we hold the lock,
 *		the CatalogTupleUpdate cannot lose an update and the CatalogTupleInsert
 *		on the first-delete path cannot hit a duplicate-key race.
 */
/* does a row_group row for (storageId, groupNumber) exist under `snapshot`? */
static bool
row_group_exists(uint64 storageId, uint64 groupNumber, Snapshot snapshot)
{
	Relation	rel = open_columnar_table("row_group", AccessShareLock);
	ScanKeyData key[2];
	SysScanDesc scan;
	bool		found;

	ScanKeyInit(&key[0], Anum_row_group_storage_id, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) storageId));
	ScanKeyInit(&key[1], Anum_row_group_group_number, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) groupNumber));
	scan = systable_beginscan(rel, InvalidOid, false, snapshot, 2, key);
	found = HeapTupleIsValid(systable_getnext(scan));
	systable_endscan(scan);
	table_close(rel, AccessShareLock);
	return found;
}

/*
 * PgColumnarLockChunkGroup
 *		Take the transaction-scoped advisory lock for one chunk group (the whole
 *		row group, chunk id 0), the same lock PgColumnarUpsertDeleteVector takes. Used by
 *		online compaction (Phase F3b) so a rewrite of a group serializes with
 *		concurrent deletes to that group.
 */
void
PgColumnarLockChunkGroup(uint64 storageId, uint64 groupNumber)
{
	delete_vector_lock_chunk_group(storageId, groupNumber, 0);
}

void
PgColumnarUpsertDeleteVector(uint64 storageId, DeleteVectorMetadata *rm)
{
	Relation	rel;
	TupleDesc	tupdesc;
	ScanKeyData key[2];
	SysScanDesc scan;
	HeapTuple	existing;
	HeapTuple	tuple;
	Datum		values[Natts_delete_vector];
	bool		nulls[Natts_delete_vector];
	bytea	   *bitmapb;
	int			deletedCount = rm->deletedCount;
	ItemPointerData replaceTid;
	bool		haveReplace = false;

	delete_vector_lock_chunk_group(storageId, rm->groupNumber, 0);

	/*
	 * Phase F3b conflict detection. Having taken the chunk-group advisory lock,
	 * we have serialized with any concurrent online rewrite of this group. If that
	 * rewrite retired the group, its row_group row is gone in the latest committed
	 * state (our own snapshot may still show it, but the row now lives at a new
	 * number in the new group). Applying this delete to the retired group would
	 * lose it, so abort with a serialization failure; the transaction retries and
	 * re-resolves the row at its new location. A group that was never rewritten
	 * still exists, so normal deletes are unaffected.
	 */
	{
		Snapshot	latest;
		bool		exists;

		/*
		 * Latest committed state, with curcid advanced (PgColumnarCatalogSnapshot)
		 * so a group this same transaction just flushed (pending writes flush
		 * before delete vectors at pre-commit) is visible and does not look retired.
		 * A group retired by a concurrent COMMITTED rewrite is gone here.
		 */
		PushActiveSnapshot(GetLatestSnapshot());
		latest = PgColumnarCatalogSnapshot(GetActiveSnapshot());
		exists = row_group_exists(storageId, rm->groupNumber, latest);
		PopActiveSnapshot();
		if (!exists)
			ereport(ERROR,
					(errcode(ERRCODE_T_R_SERIALIZATION_FAILURE),
					 errmsg("row group was compacted concurrently"),
					 errhint("Retry the transaction.")));
	}

	rel = open_columnar_table("delete_vector", RowExclusiveLock);
	tupdesc = RelationGetDescr(rel);

	ScanKeyInit(&key[0], Anum_delete_vector_storage_id, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) storageId));
	ScanKeyInit(&key[1], Anum_delete_vector_group_number, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) rm->groupNumber));

	/*
	 * THE INDEX, like every sibling catalog read. This ran once per row
	 * group with InvalidOid, so a DELETE touching twenty groups took twenty
	 * sequential scans of delete_vector -- the cost the index switch existed
	 * to remove, still being paid by the session that did the writing.
	 *
	 * Attributed with an elog probe at every delete_vector scan site (#1146):
	 * 20 here, 40 at ReadDeleteVectorList, 1 at ReadDeleteVectorsForStorage,
	 * against seq_scan=20 idx_scan=41. Afterwards seq_scan=0 idx_scan=61.
	 *
	 * SnapshotSelf is unchanged: the upsert has to see its own uncommitted
	 * row, and systable_beginscan applies the same snapshot to an index scan.
	 */
	{
		Oid			dvIdx = pgcolumnar_index_oid("delete_vector_pkey");

		scan = systable_beginscan(rel, dvIdx, OidIsValid(dvIdx),
								  SnapshotSelf, 2, key);
	}
	if (HeapTupleIsValid(existing = systable_getnext(scan)))
	{
		bool		isnull;
		Datum		existingBitmap;

		/* merge the existing bitmap bits into rm->bitmap and recount */
		existingBitmap = heap_getattr(existing, Anum_delete_vector_bitmap, tupdesc, &isnull);
		if (!isnull)
		{
			bytea	   *eb = DatumGetByteaP(existingBitmap);
			uint32		elen = VARSIZE(eb) >= VARHDRSZ ?
				(uint32) (VARSIZE(eb) - VARHDRSZ) : 0;
			char	   *ebytes = VARDATA(eb);
			uint32		i;
			int			bit;

			deletedCount = 0;
			for (i = 0; i < rm->bitmapLen; i++)
			{
				if (i < elen)
					rm->bitmap[i] |= ebytes[i];
			}
			for (i = 0; i < rm->bitmapLen; i++)
				for (bit = 0; bit < 8; bit++)
					if (rm->bitmap[i] & (1 << bit))
						deletedCount++;
		}

		replaceTid = existing->t_self;
		haveReplace = true;
	}
	systable_endscan(scan);

	memset(nulls, false, sizeof(nulls));
	values[Anum_delete_vector_storage_id - 1] = Int64GetDatum((int64) storageId);
	values[Anum_delete_vector_group_number - 1] = Int64GetDatum((int64) rm->groupNumber);
	values[Anum_delete_vector_deleted_count - 1] = Int32GetDatum(deletedCount);

	bitmapb = (bytea *) palloc(VARHDRSZ + rm->bitmapLen);
	SET_VARSIZE(bitmapb, VARHDRSZ + rm->bitmapLen);
	memcpy(VARDATA(bitmapb), rm->bitmap, rm->bitmapLen);
	values[Anum_delete_vector_bitmap - 1] = PointerGetDatum(bitmapb);

	tuple = heap_form_tuple(tupdesc, values, nulls);

	if (haveReplace)
	{
		tuple->t_self = replaceTid;
		CatalogTupleUpdate(rel, &replaceTid, tuple);
	}
	else
		CatalogTupleInsert(rel, tuple);

	heap_freetuple(tuple);
	table_close(rel, RowExclusiveLock);
}

/*
 * PgColumnarDeleteMetadata
 *		Remove every metadata row for a storage id. Used when a columnar
 *		table is dropped or truncated.
 */
static void
delete_rows_by_storage_id(const char *tableName, AttrNumber storageAttno,
						  uint64 storageId)
{
	Relation	rel = open_columnar_table(tableName, RowExclusiveLock);
	ScanKeyData key[1];
	SysScanDesc scan;
	HeapTuple	tuple;

	ScanKeyInit(&key[0], storageAttno, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) storageId));

	scan = systable_beginscan(rel, InvalidOid, false, NULL, 1, key);
	while (HeapTupleIsValid(tuple = systable_getnext(scan)))
		CatalogTupleDelete(rel, &tuple->t_self);
	systable_endscan(scan);

	table_close(rel, RowExclusiveLock);
}

void
PgColumnarDeleteMetadata(uint64 storageId)
{
	delete_rows_by_storage_id("delete_vector", Anum_delete_vector_storage_id, storageId);
	/* native format catalog (PGCN v1); no-op rows for 2.2-line tables */
	delete_rows_by_storage_id("column_chunk", Anum_column_chunk_storage_id, storageId);
	delete_rows_by_storage_id("zone_map", Anum_zone_map_storage_id, storageId);
	delete_rows_by_storage_id("bloom", Anum_bloom_storage_id, storageId);
	delete_rows_by_storage_id("free_space", Anum_free_space_storage_id, storageId);
	delete_rows_by_storage_id("row_group", Anum_row_group_storage_id, storageId);
	delete_rows_by_storage_id("storage", Anum_native_storage_storage_id, storageId);

	/*
	 * The third row-group loss path (#709, #721 review). The vacuum-family
	 * callers pass an OLD storage id a memo cannot be keyed on, but the
	 * TRUNCATE path (pgcolumnar_relation_nontransactional_truncate) wipes
	 * the metadata of the SAME storage id a memo may hold. Today a stale HIT
	 * is additionally masked by TRUNCATE marking the command id used, so the
	 * next fetch's cid differs -- but that is a cid accident of the current
	 * truncate path, exactly the class of unstated invariant the retirement
	 * reset above was once wrongly rested on (it held on PG17 and broke on
	 * PG18). Reset here so the memo's correctness does not depend on it.
	 */
	PgColumnarGroupMemoReset(true);
}

/*
 * Opt-in, default off. Set for its own session by a pgcolumnar.parallel_copy
 * loader (backing the pgcolumnar.bulk_parallel_writer GUC, registered in
 * _PG_init) so PgColumnarInsertNativeStorageRow can skip the storage-row creation
 * advisory lock when the row already exists committed. Ordinary writes never set
 * it, so the default write path is unchanged.
 */
bool		pgcolumnar_bulk_parallel_writer = false;

/*
 * PgColumnarInsertNativeStorageRow, PgColumnarInsertRowGroupRow,
 * PgColumnarInsertColumnChunkRow
 *		Record the native-format catalog rows (PGCN v1, native spec 11). Called
 *		by the native writer's flush. The 2.2-line writer does not use these.
 */
/*
 * columnar_relation_is_new_in_xact
 *		Was this relation created by the transaction that is now writing to it?
 *
 *		rd_createSubid is set while a relation's creating (sub)transaction is
 *		still uncommitted and cleared at commit, so a true answer means no other
 *		session can see the relation yet. That is the exact condition under which
 *		the first-writer race below cannot happen: there is no second writer.
 *
 *		Deliberately NOT rd_newRelfilelocatorSubid. A relation rewritten by this
 *		transaction (TRUNCATE, CLUSTER, ALTER TABLE) is still visible to other
 *		sessions in its previous form, so another backend can be writing to it and
 *		the race is live. Only creation makes a relation invisible.
 *
 *		Opened with NoLock: the caller is mid-write and already holds a lock on
 *		this relation, and taking another here would be lock traffic on the hot
 *		flush path for a relcache field.
 */
static bool
columnar_relation_is_new_in_xact(Oid relid)
{
	Relation	rel;
	bool		isNew;

	if (!OidIsValid(relid))
		return false;

	rel = RelationIdGetRelation(relid);
	if (!RelationIsValid(rel))
		return false;
	isNew = (rel->rd_createSubid != InvalidSubTransactionId);
	RelationClose(rel);
	return isNew;
}

void
PgColumnarInsertNativeStorageRow(const NativeStorageMetadata *s)
{
	Relation	rel = open_columnar_table("storage", RowExclusiveLock);
	TupleDesc	tupdesc = RelationGetDescr(rel);
	Datum		values[Natts_native_storage];
	bool		nulls[Natts_native_storage];
	HeapTuple	tuple;
	Snapshot	snapshot;
	ScanKeyData key[1];
	SysScanDesc scan;
	bool		exists;
	LOCKTAG		tag;

	/*
	 * The storage row is written once per storage id, but the native writer
	 * calls this on every row-group flush, so it must be idempotent. Under
	 * concurrent first-writes to the same table both backends' inserts are
	 * invisible to each other's snapshot, so a plain check-then-insert would let
	 * both insert and the second would fail on storage_pkey (issue seen by the
	 * concurrent differential suite in native mode). Serialize creators with a
	 * transaction-scoped advisory lock keyed by the storage id (discriminator 2,
	 * distinct from the delete_vector lock's 1), then re-check against the latest
	 * committed state: the loser of the race sees the winner's committed row and
	 * skips the insert. The lock is held to transaction end so the winner's row
	 * is committed and visible before the loser proceeds.
	 */
	/*
	 * Bulk-parallel fast path (opt-in, default off). A pgcolumnar.parallel_copy
	 * loader sets pgcolumnar_bulk_parallel_writer for its own session; when the
	 * storage row already exists in the latest committed state we skip the advisory
	 * lock entirely and return. That lock exists ONLY to serialize the first-writer
	 * creation race -- once the row is committed there is nothing to wait for -- and
	 * holding it to transaction end is exactly what makes concurrent writers to one
	 * storage serialize and, under two-phase commit, deadlock. Skipping it (only
	 * when the row provably exists, and only for an opting-in loader whose
	 * coordinator pre-created and committed that row) lets N loaders write one
	 * table's storage concurrently and 2PC-safely. Every ordinary write leaves the
	 * flag false and takes the unchanged path below.
	 */
	if (pgcolumnar_bulk_parallel_writer)
	{
		PushActiveSnapshot(GetLatestSnapshot());
		snapshot = PgColumnarCatalogSnapshot(GetActiveSnapshot());
		ScanKeyInit(&key[0], Anum_native_storage_storage_id, BTEqualStrategyNumber,
					F_INT8EQ, Int64GetDatum((int64) s->storageId));
		scan = systable_beginscan(rel, InvalidOid, false, snapshot, 1, key);
		exists = HeapTupleIsValid(systable_getnext(scan));
		systable_endscan(scan);
		PopActiveSnapshot();
		if (exists)
		{
			table_close(rel, RowExclusiveLock);
			return;
		}
	}

	/*
	 * A relation this transaction created cannot be seen by anyone else, so the
	 * first-writer race the lock below defends against cannot happen, and the
	 * fresh snapshot that detects it is not needed either (issue #387).
	 *
	 * That matters beyond saving work: GetLatestSnapshot() raises "cannot update
	 * SecondarySnapshot during a parallel operation" under IsInParallelMode(),
	 * and CREATE TABLE ... USING pgcolumnar AS SELECT runs the whole executor in
	 * parallel mode whenever the source plan is parallel, which is the default at
	 * any size worth loading. So this path failed outright on every major.
	 *
	 * The condition is creation, NOT parallel mode. Being in parallel mode says
	 * nothing about whether a second writer exists; a committed, empty columnar
	 * table can be first-written by two sessions at once, and skipping the lock
	 * there would restore the storage_pkey failure the concurrent differential
	 * suite found. Invisibility is what makes it safe.
	 *
	 * The existence check is still needed, because the writer calls this on every
	 * row-group flush and it must stay idempotent. It reads the active snapshot
	 * with curcid advanced, which sees this transaction's own earlier insert and
	 * touches no global snapshot state.
	 */
	if (columnar_relation_is_new_in_xact(s->relationOid) && ActiveSnapshotSet())
	{
		snapshot = PgColumnarCatalogSnapshot(GetActiveSnapshot());
		ScanKeyInit(&key[0], Anum_native_storage_storage_id, BTEqualStrategyNumber,
					F_INT8EQ, Int64GetDatum((int64) s->storageId));
		scan = systable_beginscan(rel, InvalidOid, false, snapshot, 1, key);
		exists = HeapTupleIsValid(systable_getnext(scan));
		systable_endscan(scan);
		if (exists)
		{
			table_close(rel, RowExclusiveLock);
			return;
		}
		goto insert_row;
	}

	SET_LOCKTAG_ADVISORY(tag, MyDatabaseId,
						 (uint32) (s->storageId >> 32),
						 (uint32) (s->storageId & 0xFFFFFFFF),
						 PGCOLUMNAR_LOCKCLASS_STORAGE_ROW);
	(void) LockAcquire(&tag, ExclusiveLock, false /* transaction lock */ ,
					   false /* wait */ );

	/*
	 * Re-check under the lock against a fresh snapshot so the loser of a
	 * cross-transaction race sees the winner's just-committed row (GetLatestSnapshot),
	 * with curcid advanced (PgColumnarCatalogSnapshot) so a second flush in this same
	 * transaction still sees the row this transaction already inserted. The latest
	 * snapshot must be pushed active before it drives a heap visibility check:
	 * PostgreSQL 18 asserts a scan snapshot is registered or active
	 * (heapam_visibility.c), and the catalog-snapshot copy inherits the active
	 * count. Pop it once the existence scan is done.
	 */
	PushActiveSnapshot(GetLatestSnapshot());
	snapshot = PgColumnarCatalogSnapshot(GetActiveSnapshot());
	ScanKeyInit(&key[0], Anum_native_storage_storage_id, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) s->storageId));
	scan = systable_beginscan(rel, InvalidOid, false, snapshot, 1, key);
	exists = HeapTupleIsValid(systable_getnext(scan));
	systable_endscan(scan);
	PopActiveSnapshot();
	if (exists)
	{
		table_close(rel, RowExclusiveLock);
		return;
	}

insert_row:
	memset(nulls, false, sizeof(nulls));
	values[Anum_native_storage_storage_id - 1] = Int64GetDatum((int64) s->storageId);
	values[Anum_native_storage_relation_oid - 1] = ObjectIdGetDatum(s->relationOid);
	values[Anum_native_storage_format_version - 1] = Int32GetDatum(s->formatVersion);
	values[Anum_native_storage_vector_length - 1] = Int32GetDatum(s->vectorLength);
	values[Anum_native_storage_row_group_limit - 1] = Int32GetDatum(s->rowGroupLimit);
	/*
	 * A new storage starts unordered. An ordering rewrite sets this afterwards
	 * (PgColumnarSetSortedThrough); an unsorted one leaves it NULL, which is what
	 * makes a rewrite reset the sort state with no invalidation step.
	 */
	nulls[Anum_native_storage_sorted_through - 1] = true;
	nulls[Anum_native_storage_sorted_from - 1] = true;
	nulls[Anum_native_storage_sorted_by - 1] = true;
	nulls[Anum_native_storage_sorted_kind - 1] = true;

	tuple = heap_form_tuple(tupdesc, values, nulls);
	metadata_flush_insert(rel, tuple);
	heap_freetuple(tuple);
	metadata_flush_close(rel, RowExclusiveLock);
}

/*
 * PgColumnarRetargetStorageRelation
 *		Point this relation's storage row at the OID that survived the rewrite.
 *
 *		ALTER COLUMN TYPE rewrites through the transient relation make_new_heap
 *		builds. The flush records that transient's OID (columnar_write_state.c,
 *		RelationGetRelid of the relation it was handed). The swap keeps the
 *		user's OID and drops the transient, so pgcolumnar.storage.relation_oid
 *		names a relation that no longer exists. The written-geometry lookup is
 *		keyed on that column and finds nothing. The rows are still readable:
 *		the metapage on the surviving relation already names the storage id
 *		the rewrite wrote.
 *
 *		A no-op when the row already names this relation, which is TRUNCATE
 *		and any statement that did not rewrite, and when the relation has no
 *		storage row yet.
 */
void
PgColumnarRetargetStorageRelation(Oid relid)
{
	Relation	userRel;
	Relation	rel;
	TupleDesc	tupdesc;
	ScanKeyData key[1];
	SysScanDesc scan;
	HeapTuple	tuple;
	uint64		storageId;
	Oid			storIdx;

	if (!OidIsValid(relid) || !PgColumnarIsColumnarRelation(relid))
		return;

	userRel = table_open(relid, NoLock);
	storageId = PgColumnarStorageId(userRel);
	table_close(userRel, NoLock);
	if (storageId == 0)
		return;

	/*
	 * The storage row was inserted earlier in this statement. Advance the
	 * command counter so this scan sees it.
	 */
	CommandCounterIncrement();

	rel = open_columnar_table("storage", RowExclusiveLock);
	tupdesc = RelationGetDescr(rel);
	ScanKeyInit(&key[0], Anum_native_storage_storage_id, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) storageId));
	storIdx = pgcolumnar_index_oid("storage_pkey");
	scan = systable_beginscan(rel, storIdx, OidIsValid(storIdx), NULL, 1, key);
	tuple = systable_getnext(scan);
	if (HeapTupleIsValid(tuple))
	{
		bool		isnull;
		Datum		d = heap_getattr(tuple, Anum_native_storage_relation_oid,
									 tupdesc, &isnull);
		Oid			stored = isnull ? InvalidOid : DatumGetObjectId(d);

		if (stored != relid)
		{
			Datum		values[Natts_native_storage];
			bool		nulls[Natts_native_storage];
			bool		replace[Natts_native_storage];
			HeapTuple	newTuple;

			memset(values, 0, sizeof(values));
			memset(nulls, false, sizeof(nulls));
			memset(replace, false, sizeof(replace));
			values[Anum_native_storage_relation_oid - 1] = ObjectIdGetDatum(relid);
			replace[Anum_native_storage_relation_oid - 1] = true;
			newTuple = heap_modify_tuple(tuple, tupdesc, values, nulls, replace);
			CatalogTupleUpdate(rel, &newTuple->t_self, newTuple);
			heap_freetuple(newTuple);
		}
	}
	systable_endscan(scan);
	table_close(rel, RowExclusiveLock);
}

/*
 * PgColumnarRenameDeclaredSortByColumn
 *		Follow a column rename through the DECLARED sort key in
 *		pgcolumnar.options (#778).
 *
 *		options.sort_by is what pgcolumnar.vacuum_sorted(t) resolves when it is
 *		called with no explicit columns, and it holds NAMES for a deliberate
 *		reason the catalog comment gives: options is the one pgcolumnar catalog
 *		carried through pg_dump and restore, where attnums renumber and names do
 *		not. So names are correct there, and maintaining them across a rename is
 *		the only correction available.
 *
 *		Maintaining it here is not optional once the ordering mark is
 *		maintained. Before, both were consistently stale and a bare
 *		vacuum_sorted(t) at least agreed with the mark. Renaming only the mark
 *		makes the two catalogs point at DIFFERENT columns, so the same call with
 *		the same declared intent silently rewrites on a different physical key
 *		than it did yesterday.
 */
void
PgColumnarRenameDeclaredSortByColumn(Oid relid, const char *oldName,
									 const char *newName)
{
	Relation	rel;
	TupleDesc	tupdesc;
	ScanKeyData key[1];
	SysScanDesc scan;
	HeapTuple	tuple;

	CommandCounterIncrement();

	rel = open_columnar_table("options", RowExclusiveLock);
	tupdesc = RelationGetDescr(rel);
	ScanKeyInit(&key[0], Anum_options_regclass, BTEqualStrategyNumber,
				F_OIDEQ, ObjectIdGetDatum(relid));

	/*
	 * options_pkey, like every other scan whose key is that column. The sixth
	 * site: five moved when the others did and this one did not, and the
	 * population was a list of function names rather than the property
	 * "the key column IS the index's column" -- which is what a list silently
	 * regrows past.
	 *
	 * AND THE PROPERTY IS WIDER THAN THIS CHANGE. Counted on both trees rather
	 * than on one:
	 *
	 *     main        44 systable_beginscan calls, 31 InvalidOid, 13 indexed
	 *     this branch 44                         , 24            , 20
	 *
	 * so this change resolves SEVEN sites, and 21 of the remaining 24 have a scan
	 * key that is a prefix of an existing index. None of the 21 is called a defect
	 * here, because none has been measured the way these seven were. #1207 carries
	 * them, with line numbers.
	 *
	 * NO PER-CATALOG TABLE HERE, DELIBERATELY. Three independent sweeps agreed on
	 * 21 and disagreed on how it splits, because a sweep that searches BACKWARDS
	 * for the nearest ScanKeyInit mis-assigns any function that scans two
	 * catalogs -- PgColumnarCheckFreeSpaceNoOverlap scans row_group at 1020 and
	 * free_space at 1041, and a backward search gives both to whichever key it
	 * meets first. The relation HANDLE passed to systable_beginscan is the ground
	 * truth; tracing it to its open_columnar_table("<name>") settles it. A table
	 * of counts in a comment is the thing this comment is warning about.
	 *
	 * THE COUNTS ABOVE ARE OF CALLS, NOT OF THE STRING. A first draft said 46 and
	 * "six", from a `grep -c systable_beginscan` that counted this very
	 * paragraph's own sentence about systable_beginscan. A comment describing a
	 * sweep is input to that sweep, which is the same trap one level down from the
	 * one it is describing.
	 *
	 * NO MEASUREMENT WILL SHOW THIS ONE. It runs on ALTER TABLE ... RENAME
	 * COLUMN, not on a plan, so the planner-path probe that found the others
	 * cannot reach it. That is the reason to fix it rather than a reason not to.
	 */
	{
		Oid			optIdx = pgcolumnar_scan_index_oid(rel, "options_pkey");

		scan = systable_beginscan(rel, optIdx, OidIsValid(optIdx), NULL, 1, key);
	}
	if (HeapTupleIsValid(tuple = systable_getnext(scan)))
	{
		bool		isnull;
		Datum		d = heap_getattr(tuple, Anum_options_sort_by, tupdesc, &isnull);

		if (!isnull)
		{
			ArrayType  *arr = DatumGetArrayTypeP(d);
			Datum	   *elems;
			bool	   *elnulls;
			int			n;
			int			i;
			bool		hit = false;

			deconstruct_array(arr, NAMEOID, NAMEDATALEN, false, 'c',
							  &elems, &elnulls, &n);
			for (i = 0; i < n; i++)
			{
				if (elnulls[i])
					continue;
				if (strcmp(NameStr(*DatumGetName(elems[i])), oldName) == 0)
				{
					elems[i] = DirectFunctionCall1(namein,
												   CStringGetDatum((char *) newName));
					hit = true;
				}
			}

			/* a rename touching no declared column must not rewrite the row */
			if (hit)
			{
				Datum		values[Natts_options];
				bool		nulls[Natts_options];
				bool		replace[Natts_options];
				HeapTuple	newTuple;

				memset(values, 0, sizeof(values));
				memset(nulls, false, sizeof(nulls));
				memset(replace, false, sizeof(replace));
				values[Anum_options_sort_by - 1] =
					PointerGetDatum(construct_array(elems, n, NAMEOID,
													NAMEDATALEN, false,
													TYPALIGN_CHAR));
				replace[Anum_options_sort_by - 1] = true;
				newTuple = heap_modify_tuple(tuple, tupdesc, values, nulls, replace);
				CatalogTupleUpdate(rel, &newTuple->t_self, newTuple);
				heap_freetuple(newTuple);
			}
		}
	}
	systable_endscan(scan);
	metadata_flush_close(rel, RowExclusiveLock);
}

/*
 * PgColumnarRenameSortKeyColumn
 *		Follow an ALTER TABLE ... RENAME COLUMN through the ordering mark (#778).
 *
 *		pgcolumnar.storage records the applied sort key as a list of column
 *		NAMES, and both ordering self-gates compare that list against the
 *		CURRENT attname of the columns they were asked about -- vacuum_sorted's
 *		gate (#760) and the online recluster's (#415). Column names are not
 *		stable, and nothing else maintains the mark.
 *
 *		A three-statement swap (a->tmp, b->a, tmp->b) therefore leaves the
 *		stored name resolving to a DIFFERENT column: the gate reports "already
 *		in this order" about a column that was never sorted and skips the
 *		rewrite. For vacuum_sorted that is the failure #760 exists to prevent,
 *		reached through another door, and the table ends up neither ordered nor
 *		reclaimed with no error raised.
 *
 *		Renaming the entry rather than clearing the mark is deliberate. The
 *		DATA has not moved, so the ordering is still true of whichever column
 *		now carries the name, and following the rename keeps that fact instead
 *		of throwing away a real optimisation on every rename. It composes
 *		correctly through the swap above: {a} -> {tmp} -> {tmp} -> {b}, which is
 *		the column the rows are actually ordered by.
 *
 *		Called after the rename has executed, so a rename that ERRORS leaves the
 *		mark untouched.
 */
void
PgColumnarRenameSortKeyColumn(uint64 storageId, const char *oldName,
							  const char *newName)
{
	int64		sfrom,
				sthrough;
	List	   *skey;
	char	   *skind;
	List	   *renamed = NIL;
	ListCell   *lc;
	bool		hit = false;

	PgColumnarGetSortedInfo(storageId, &sfrom, &sthrough, &skey, &skind);
	if (skey == NIL || skind == NULL)
		return;					/* no mark to maintain */

	foreach(lc, skey)
	{
		char	   *nm = (char *) lfirst(lc);

		if (strcmp(nm, oldName) == 0)
		{
			renamed = lappend(renamed, pstrdup(newName));
			hit = true;
		}
		else
			renamed = lappend(renamed, pstrdup(nm));
	}

	/* a rename that touches no sort-key column must not rewrite the row */
	if (!hit)
		return;

	PgColumnarSetSortedExtent(storageId, sfrom, sthrough, renamed, skind);
}

/*
 * PgColumnarSetSortedThrough
 *		Record the row group number the last ordering rewrite ended at, so a
 *		reader can tell how much of the layout is still ordered (issue #301).
 *
 *		Called at the end of an ordering rewrite, after the write state is
 *		flushed, so every group it covers exists in pgcolumnar.row_group. Groups
 *		written after that point get higher numbers and are the unsorted tail.
 *
 *		A boundary, not a count, because the online maintenance paths retire a
 *		group and write its survivors back with a fresh, higher number. Those
 *		replacements sit above the mark, which is where they belong: their rows
 *		are no longer in the run's order. A count would slide down onto them as
 *		the run shrank and report an order that is not there.
 *
 *		A storage with no row, which happens only when the rewrite wrote nothing
 *		at all, has nothing to update and is left alone. The reader then sees no
 *		groups and reports no decay.
 */
void
PgColumnarSetSortedExtent(uint64 storageId, int64 firstGroup, int64 lastGroup,
						  List *sortByNames, const char *sortedKind)
{
	Relation	rel;
	TupleDesc	tupdesc;
	ScanKeyData key[1];
	SysScanDesc scan;
	HeapTuple	tuple;
	Datum		byDatum = (Datum) 0;
	bool		byNull = (sortByNames == NIL);

	/* build the name[] once, before opening the catalog */
	if (!byNull)
	{
		Datum	   *elems = (Datum *) palloc(sizeof(Datum) * list_length(sortByNames));
		int			ne = 0;
		ListCell   *lc;

		foreach(lc, sortByNames)
			elems[ne++] = DirectFunctionCall1(namein,
											  CStringGetDatum((char *) lfirst(lc)));
		byDatum = PointerGetDatum(construct_array(elems, ne, NAMEOID,
												  NAMEDATALEN, false, TYPALIGN_CHAR));
	}

	/*
	 * The storage row was inserted by this same transaction, in the same command
	 * that flushed the rewrite's first group. heap_update refuses to update a
	 * tuple whose cmin is the current command ("attempted to update invisible
	 * tuple"), and no choice of scan snapshot avoids that: advancing curcid only
	 * makes the row visible to the scan, not to the update. Closing the command
	 * makes the insert a completed one, which is what the update requires.
	 */
	CommandCounterIncrement();

	rel = open_columnar_table("storage", RowExclusiveLock);
	tupdesc = RelationGetDescr(rel);

	ScanKeyInit(&key[0], Anum_native_storage_storage_id, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) storageId));

	/* NULL snapshot -> catalog snapshot, as the other read-side scans use. */
	scan = systable_beginscan(rel, InvalidOid, false, NULL, 1, key);
	tuple = systable_getnext(scan);
	if (HeapTupleIsValid(tuple))
	{
		Datum		values[Natts_native_storage];
		bool		nulls[Natts_native_storage];
		bool		replace[Natts_native_storage];
		HeapTuple	newTuple;

		memset(values, 0, sizeof(values));
		memset(nulls, false, sizeof(nulls));
		memset(replace, false, sizeof(replace));
		values[Anum_native_storage_sorted_through - 1] = Int64GetDatum(lastGroup);
		replace[Anum_native_storage_sorted_through - 1] = true;
		values[Anum_native_storage_sorted_from - 1] = Int64GetDatum(firstGroup);
		replace[Anum_native_storage_sorted_from - 1] = true;
		/*
		 * The new columns exist only after the base script (fresh install) or
		 * the dev->alpha upgrade ALTERs. A new .so on an un-upgraded 7-column
		 * storage table must not touch attnums 8/9 -- heap_modify_tuple would
		 * index past the tuple descriptor. Guard on the live natts: with the
		 * columns absent this records nothing (the run's key stays unknown, so
		 * recluster never self-gates), exactly the pre-#415 behavior.
		 */
		if (tupdesc->natts >= Anum_native_storage_sorted_kind)
		{
			replace[Anum_native_storage_sorted_by - 1] = true;
			if (byNull)
				nulls[Anum_native_storage_sorted_by - 1] = true;
			else
				values[Anum_native_storage_sorted_by - 1] = byDatum;
			replace[Anum_native_storage_sorted_kind - 1] = true;
			if (sortedKind == NULL)
				nulls[Anum_native_storage_sorted_kind - 1] = true;
			else
				values[Anum_native_storage_sorted_kind - 1] =
					DirectFunctionCall1(textin, CStringGetDatum((char *) sortedKind));
		}

		newTuple = heap_modify_tuple(tuple, tupdesc, values, nulls, replace);
		CatalogTupleUpdate(rel, &newTuple->t_self, newTuple);
		heap_freetuple(newTuple);
	}
	systable_endscan(scan);
	table_close(rel, RowExclusiveLock);
}

/*
 * PgColumnarGetSortedInfo
 *		Read the run bounds, clustering key and kind for a storage (#415).
 *		*firstGroup and *lastGroup are -1 when unordered; *sortByNames is NIL and
 *		*sortedKind is NULL when unknown. Used by recluster's self-gate.
 */
void
PgColumnarGetSortedInfo(uint64 storageId, int64 *firstGroup, int64 *lastGroup,
						List **sortByNames, char **sortedKind)
{
	Relation	rel;
	TupleDesc	tupdesc;
	ScanKeyData key[1];
	SysScanDesc scan;
	HeapTuple	tuple;
	Oid			storIdx;

	*firstGroup = -1;
	*lastGroup = -1;
	*sortByNames = NIL;
	*sortedKind = NULL;

	rel = open_columnar_table("storage", AccessShareLock);
	tupdesc = RelationGetDescr(rel);
	ScanKeyInit(&key[0], Anum_native_storage_storage_id, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) storageId));

	/*
	 * NAME THE INDEX (#1237). The key is storage_id, which storage_pkey is a
	 * UNIQUE btree on, so this was a sequential scan of the catalog on the one
	 * column it is indexed by -- 2 * pages per call, growing with the number of
	 * columnar relations in the database.
	 *
	 * It matters because this is on the PLANNING path, not a maintenance one:
	 * pgcolumnar_sorted_pathkeys reaches it at columnar_customscan.c:1542. The
	 * comment above says "used by recluster's self-gate", which was true when
	 * it was written and is no longer the whole truth.
	 *
	 * Unlike #1210's lookup on relation_oid, nothing has to be built and no
	 * decision about #1211 is involved: the index exists and the key is exact.
	 */
	storIdx = pgcolumnar_index_oid("storage_pkey");
	scan = systable_beginscan(rel, storIdx, OidIsValid(storIdx), NULL, 1, key);
	tuple = systable_getnext(scan);
	if (HeapTupleIsValid(tuple))
	{
		Datum		d;
		bool		isnull;

		d = heap_getattr(tuple, Anum_native_storage_sorted_through, tupdesc, &isnull);
		if (!isnull)
			*lastGroup = DatumGetInt64(d);
		d = heap_getattr(tuple, Anum_native_storage_sorted_from, tupdesc, &isnull);
		if (!isnull)
			*firstGroup = DatumGetInt64(d);
		/*
		 * attnums 8/9 exist only after the base script or the upgrade ALTERs
		 * (#614): on an un-upgraded 7-column storage table reading them would
		 * index past the descriptor. Absent -> leave key/kind unknown, so the
		 * caller (recluster) never self-gates -- the safe pre-#415 default.
		 */
		if (tupdesc->natts >= Anum_native_storage_sorted_kind)
		{
			d = heap_getattr(tuple, Anum_native_storage_sorted_by, tupdesc, &isnull);
			if (!isnull)
			{
				Datum	   *elems;
				int			n;
				int			i;

				deconstruct_array(DatumGetArrayTypeP(d), NAMEOID, NAMEDATALEN,
								  false, TYPALIGN_CHAR, &elems, NULL, &n);
				for (i = 0; i < n; i++)
					*sortByNames = lappend(*sortByNames,
										   pstrdup(NameStr(*DatumGetName(elems[i]))));
			}
			d = heap_getattr(tuple, Anum_native_storage_sorted_kind, tupdesc, &isnull);
			if (!isnull)
				*sortedKind = text_to_cstring(DatumGetTextPP(d));
		}
	}
	systable_endscan(scan);
	table_close(rel, AccessShareLock);
}

/*
 * PgColumnarCheckNativeFormatVersion
 *		Reject a native data format version this build does not understand, before
 *		any bytes are decoded (issue #240). The physical metapage version is checked
 *		separately when the metapage is read (PgColumnarReadMetapage); this is the
 *		independent data-format stamp (pgcolumnar.storage.format_version), so a
 *		future PGCN version that changes the encoding while keeping the metapage
 *		layout is caught here rather than silently misread.
 *
 *		A table with no storage row -- the 2.2-line writer never wrote one, or a
 *		native table before its first flush -- has nothing to check and is
 *		accepted. The value is read out and the scan closed before any ereport, so
 *		nothing is left open across the error.
 */
void
PgColumnarCheckNativeFormatVersion(uint64 storageId, const char *relName)
{
	Relation	rel = open_columnar_table("storage", AccessShareLock);
	TupleDesc	tupdesc = RelationGetDescr(rel);
	ScanKeyData key[1];
	SysScanDesc scan;
	HeapTuple	tuple;
	bool		found = false;
	int32		formatVersion = 0;
	Oid			storIdx;

	ScanKeyInit(&key[0], Anum_native_storage_storage_id, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) storageId));
	/* NULL snapshot -> catalog snapshot, same as the other read-side scans. */
	/*
	 * NAME THE INDEX (#1237), for the same reason as PgColumnarGetSortedInfo
	 * above: storage_pkey is a UNIQUE btree on storage_id. This one is NOT on
	 * the planning path -- measured by @OffgridwithJD, fmtver=0 in all five
	 * planned shapes -- but it is reached ONCE PER COLUMNAR RELATION SCANNED at
	 * execution, from columnar_reader.c:644, and a `count(*)` that pays nothing
	 * at planning still pays this.
	 *
	 * Whether that cost MATTERS against actually reading the relation's data is
	 * not claimed here and has not been measured. The scan is wrong either way:
	 * it is sequential on an indexed primary key.
	 */
	storIdx = pgcolumnar_index_oid("storage_pkey");
	scan = systable_beginscan(rel, storIdx, OidIsValid(storIdx), NULL, 1, key);

	tuple = systable_getnext(scan);
	if (HeapTupleIsValid(tuple))
	{
		bool		isnull;
		Datum		d = heap_getattr(tuple, Anum_native_storage_format_version,
									 tupdesc, &isnull);

		if (!isnull)
		{
			found = true;
			formatVersion = DatumGetInt32(d);
		}
	}

	systable_endscan(scan);
	table_close(rel, AccessShareLock);

	if (found && formatVersion != COLUMNAR_NATIVE_VERSION_MAJOR)
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("unsupported columnar native format version %d",
						formatVersion),
				 errdetail("Relation \"%s\" was written with native format version %d; this build supports version %d.",
						   relName, formatVersion, COLUMNAR_NATIVE_VERSION_MAJOR)));
}

void
PgColumnarInsertRowGroupRow(const NativeRowGroupMetadata *rg)
{
	Relation	rel = open_columnar_table("row_group", RowExclusiveLock);
	TupleDesc	tupdesc = RelationGetDescr(rel);
	Datum		values[Natts_row_group];
	bool		nulls[Natts_row_group];
	HeapTuple	tuple;

	memset(nulls, false, sizeof(nulls));
	values[Anum_row_group_storage_id - 1] = Int64GetDatum((int64) rg->storageId);
	values[Anum_row_group_group_number - 1] = Int64GetDatum((int64) rg->groupNumber);
	values[Anum_row_group_file_offset - 1] = Int64GetDatum((int64) rg->fileOffset);
	values[Anum_row_group_row_count - 1] = Int64GetDatum((int64) rg->rowCount);
	values[Anum_row_group_byte_length - 1] = Int64GetDatum((int64) rg->byteLength);
	values[Anum_row_group_first_row_number - 1] = Int64GetDatum((int64) rg->firstRowNumber);
	values[Anum_row_group_sort_key - 1] =
		PointerGetDatum(construct_empty_array(INT2OID));

	tuple = heap_form_tuple(tupdesc, values, nulls);
	metadata_flush_insert(rel, tuple);
	heap_freetuple(tuple);
	metadata_flush_close(rel, RowExclusiveLock);
}

void
PgColumnarInsertColumnChunkRow(const NativeColumnChunkMetadata *cc)
{
	Relation	rel = open_columnar_table("column_chunk", RowExclusiveLock);
	TupleDesc	tupdesc = RelationGetDescr(rel);
	Datum		values[Natts_column_chunk];
	bool		nulls[Natts_column_chunk];
	HeapTuple	tuple;
	bytea	   *desc;

	memset(nulls, false, sizeof(nulls));
	desc = (bytea *) palloc(VARHDRSZ + cc->encodingDescriptorLen);
	SET_VARSIZE(desc, VARHDRSZ + cc->encodingDescriptorLen);
	if (cc->encodingDescriptorLen > 0)
		memcpy(VARDATA(desc), cc->encodingDescriptor, cc->encodingDescriptorLen);

	values[Anum_column_chunk_storage_id - 1] = Int64GetDatum((int64) cc->storageId);
	values[Anum_column_chunk_group_number - 1] = Int64GetDatum((int64) cc->groupNumber);
	values[Anum_column_chunk_column_index - 1] = Int16GetDatum((int16) cc->columnIndex);
	values[Anum_column_chunk_value_count - 1] = Int64GetDatum((int64) cc->valueCount);
	values[Anum_column_chunk_encoding_descriptor - 1] = PointerGetDatum(desc);
	values[Anum_column_chunk_block_codec - 1] = Int16GetDatum((int16) cc->blockCodec);
	values[Anum_column_chunk_page_offset - 1] = Int64GetDatum((int64) cc->pageOffset);
	values[Anum_column_chunk_page_length - 1] = Int64GetDatum((int64) cc->pageLength);

	tuple = heap_form_tuple(tupdesc, values, nulls);
	metadata_flush_insert(rel, tuple);
	heap_freetuple(tuple);
	metadata_flush_close(rel, RowExclusiveLock);
}

/*
 * PgColumnarInsertZoneMapRow
 *		Record one native zone-map row (Small Materialized Aggregate) for a vector
 *		or for a whole column chunk (native spec 7.1, Phase D5). Called by the
 *		native writer's flush.
 */
void
PgColumnarInsertZoneMapRow(const NativeZoneMapMetadata *z)
{
	Relation	rel = open_columnar_table("zone_map", RowExclusiveLock);
	TupleDesc	tupdesc = RelationGetDescr(rel);
	Datum		values[Natts_zone_map];
	bool		nulls[Natts_zone_map];
	HeapTuple	tuple;

	memset(nulls, false, sizeof(nulls));

	values[Anum_zone_map_storage_id - 1] = Int64GetDatum((int64) z->storageId);
	values[Anum_zone_map_group_number - 1] = Int64GetDatum((int64) z->groupNumber);
	values[Anum_zone_map_column_index - 1] = Int16GetDatum((int16) z->columnIndex);
	values[Anum_zone_map_vector_index - 1] = Int32GetDatum((int32) z->vectorIndex);

	if (z->hasMinMax)
	{
		bytea	   *mn = (bytea *) palloc(VARHDRSZ + z->minimumLen);
		bytea	   *mx = (bytea *) palloc(VARHDRSZ + z->maximumLen);

		SET_VARSIZE(mn, VARHDRSZ + z->minimumLen);
		SET_VARSIZE(mx, VARHDRSZ + z->maximumLen);
		if (z->minimumLen > 0)
			memcpy(VARDATA(mn), z->minimum, z->minimumLen);
		if (z->maximumLen > 0)
			memcpy(VARDATA(mx), z->maximum, z->maximumLen);
		values[Anum_zone_map_minimum - 1] = PointerGetDatum(mn);
		values[Anum_zone_map_maximum - 1] = PointerGetDatum(mx);
	}
	else
	{
		nulls[Anum_zone_map_minimum - 1] = true;
		nulls[Anum_zone_map_maximum - 1] = true;
	}

	if (z->hasSum)
		values[Anum_zone_map_sum - 1] = z->sum;
	else
		nulls[Anum_zone_map_sum - 1] = true;

	values[Anum_zone_map_value_count - 1] = Int64GetDatum((int64) z->valueCount);
	values[Anum_zone_map_null_count - 1] = Int64GetDatum((int64) z->nullCount);

	/*
	 * The range upper-bound summary (#1144), in its three states. NULL means no
	 * summary and the reader must never prune above; a zero-length value means
	 * the unit holds a range that is unbounded above, which also means never
	 * prune -- but it is a DIFFERENT fact, and the reader is entitled to say
	 * which. Absent-versus-unbounded matters because the field is PRESENT and
	 * understated in the unbounded case, which is exactly what a two-state
	 * design cannot express.
	 *
	 * Guarded by natts: on a catalog that predates the alpha5 upgrade this
	 * attribute does not exist, and writing past the descriptor is a corruption
	 * rather than an error.
	 */
	if (tupdesc->natts >= Anum_zone_map_max_upper)
	{
		if (!z->hasMaxUpper)
			nulls[Anum_zone_map_max_upper - 1] = true;
		else
		{
			uint32		len = z->upperUnbounded ? 0 : z->maxUpperLen;
			bytea	   *up = (bytea *) palloc(VARHDRSZ + len);

			SET_VARSIZE(up, VARHDRSZ + len);
			if (len > 0)
				memcpy(VARDATA(up), z->maxUpper, len);
			values[Anum_zone_map_max_upper - 1] = PointerGetDatum(up);
		}
	}

	tuple = heap_form_tuple(tupdesc, values, nulls);
	metadata_flush_insert(rel, tuple);
	heap_freetuple(tuple);
	metadata_flush_close(rel, RowExclusiveLock);
}

/*
 * PgColumnarInsertBloomRow
 *		Record one per-column-chunk bloom filter (native spec 7.2, Phase D5b).
 */
void
PgColumnarInsertBloomRow(const NativeBloomMetadata *b)
{
	Relation	rel = open_columnar_table("bloom", RowExclusiveLock);
	TupleDesc	tupdesc = RelationGetDescr(rel);
	Datum		values[Natts_bloom];
	bool		nulls[Natts_bloom];
	HeapTuple	tuple;
	bytea	   *filt;

	memset(nulls, false, sizeof(nulls));
	filt = (bytea *) palloc(VARHDRSZ + b->filterLen);
	SET_VARSIZE(filt, VARHDRSZ + b->filterLen);
	if (b->filterLen > 0)
		memcpy(VARDATA(filt), b->filter, b->filterLen);

	values[Anum_bloom_storage_id - 1] = Int64GetDatum((int64) b->storageId);
	values[Anum_bloom_group_number - 1] = Int64GetDatum((int64) b->groupNumber);
	values[Anum_bloom_column_index - 1] = Int16GetDatum((int16) b->columnIndex);
	values[Anum_bloom_filter - 1] = PointerGetDatum(filt);

	tuple = heap_form_tuple(tupdesc, values, nulls);
	metadata_flush_insert(rel, tuple);
	heap_freetuple(tuple);
	metadata_flush_close(rel, RowExclusiveLock);
}

/*
 * PgColumnarLoadFingerprintSeen
 *		Has this exact file already been loaded into this table by a committed
 *		pgcolumnar.parallel_copy (#403 item 7)?
 *
 *		Keyed by (relation_oid, fingerprint), which is load_fingerprint_idx, so
 *		this is an exact index lookup. The fingerprint is the SHA-256 of the
 *		file's bytes: a file that changed at the same path is a different load.
 */
bool
PgColumnarLoadFingerprintSeen(Oid relationOid, const uint8 *fingerprint,
							  int fingerprintLen, Snapshot snapshot)
{
	Relation	rel = open_columnar_table("load_fingerprint", AccessShareLock);
	ScanKeyData key[2];
	SysScanDesc scan;
	Oid			idxOid;
	HeapTuple	tuple;
	bytea	   *fp;
	bool		found;

	fp = (bytea *) palloc(VARHDRSZ + fingerprintLen);
	SET_VARSIZE(fp, VARHDRSZ + fingerprintLen);
	memcpy(VARDATA(fp), fingerprint, fingerprintLen);

	ScanKeyInit(&key[0], Anum_load_fingerprint_relation_oid, BTEqualStrategyNumber,
				F_OIDEQ, ObjectIdGetDatum(relationOid));
	ScanKeyInit(&key[1], Anum_load_fingerprint_fingerprint, BTEqualStrategyNumber,
				F_BYTEAEQ, PointerGetDatum(fp));

	idxOid = pgcolumnar_index_oid("load_fingerprint_idx");
	scan = systable_beginscan(rel, idxOid, OidIsValid(idxOid), snapshot, 2, key);
	tuple = systable_getnext(scan);
	found = HeapTupleIsValid(tuple);
	systable_endscan(scan);
	table_close(rel, AccessShareLock);
	pfree(fp);

	return found;
}

/*
 * PgColumnarRecordLoadFingerprint
 *		Record that this file has been loaded into this table (#403 item 7).
 *
 *		Called AFTER the data commits, never before. A crash between the two
 *		leaves data with no fingerprint, so a retry loads again -- the behaviour
 *		without this feature, and the safe direction. The reverse order would
 *		leave a fingerprint with no data and refuse rows that were never stored.
 */
void
PgColumnarRecordLoadFingerprint(Oid relationOid, const uint8 *fingerprint,
								int fingerprintLen, int64 rows)
{
	Relation	rel = open_columnar_table("load_fingerprint", RowExclusiveLock);
	TupleDesc	tupdesc = RelationGetDescr(rel);
	Datum		values[Natts_load_fingerprint];
	bool		nulls[Natts_load_fingerprint];
	HeapTuple	tuple;
	bytea	   *fp;

	memset(nulls, false, sizeof(nulls));
	fp = (bytea *) palloc(VARHDRSZ + fingerprintLen);
	SET_VARSIZE(fp, VARHDRSZ + fingerprintLen);
	memcpy(VARDATA(fp), fingerprint, fingerprintLen);

	values[Anum_load_fingerprint_relation_oid - 1] = ObjectIdGetDatum(relationOid);
	values[Anum_load_fingerprint_fingerprint - 1] = PointerGetDatum(fp);
	values[Anum_load_fingerprint_rows - 1] = Int64GetDatum(rows);
	values[Anum_load_fingerprint_loaded_at - 1] =
		TimestampTzGetDatum(GetCurrentTimestamp());

	tuple = heap_form_tuple(tupdesc, values, nulls);
	metadata_flush_insert(rel, tuple);
	heap_freetuple(tuple);
	metadata_flush_close(rel, RowExclusiveLock);
}

/*
 * PgColumnarReadBloomForColumn
 *		One column's bloom filter for one row group, or NULL when it has none
 *		(issue #314).
 *
 *		bloom_pkey is (storage_id, group_number, column_index), so naming all
 *		three makes this an exact index lookup rather than a range scan whose
 *		unwanted rows are discarded by the caller. That matters because a bloom
 *		filter is one of the larger things in this catalog: it is sized by the
 *		group's distinct values, so reading a whole group's worth to probe one
 *		column reads most of what it fetches for nothing.
 */
NativeBloomMetadata *
PgColumnarReadBloomForColumn(uint64 storageId, uint64 groupNumber,
						   int columnIndex, Snapshot snapshot)
{
	Relation	rel = open_columnar_table("bloom", AccessShareLock);
	TupleDesc	tupdesc = RelationGetDescr(rel);
	ScanKeyData key[3];
	SysScanDesc scan;
	Oid			idxOid;
	HeapTuple	tuple;
	NativeBloomMetadata *b = NULL;

	ScanKeyInit(&key[0], Anum_bloom_storage_id, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) storageId));
	ScanKeyInit(&key[1], Anum_bloom_group_number, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) groupNumber));
	ScanKeyInit(&key[2], Anum_bloom_column_index, BTEqualStrategyNumber,
				F_INT2EQ, Int16GetDatum((int16) columnIndex));
	idxOid = pgcolumnar_index_oid("bloom_pkey");
	scan = systable_beginscan(rel, idxOid, OidIsValid(idxOid), snapshot,
							  3, key);
	tuple = systable_getnext(scan);
	if (HeapTupleIsValid(tuple))
	{
		bool		isnull;
		Datum		d;

		b = palloc0(sizeof(NativeBloomMetadata));
		b->storageId = storageId;
		b->groupNumber = groupNumber;
		b->columnIndex = (int16) columnIndex;
		d = heap_getattr(tuple, Anum_bloom_filter, tupdesc, &isnull);
		if (!isnull)
		{
			bytea	   *bf = DatumGetByteaPP(d);

			b->filterLen = VARSIZE_ANY_EXHDR(bf);
			b->filter = (const char *) memcpy(palloc(b->filterLen + 1),
											  VARDATA_ANY(bf), b->filterLen);
		}
	}
	systable_endscan(scan);
	table_close(rel, AccessShareLock);

	return b;
}

static NativeZoneMapMetadata *zonemap_from_tuple(HeapTuple tuple,
					TupleDesc tupdesc, uint64 storageId,
					uint64 groupNumber, int32 vecIndex);

/*
 * PgColumnarReadZoneMapVectors
 *		The per-vector zone maps (vector_index >= 0) of one row group, for
 *		per-vector skipping (native spec 7.1, Phase D5b). Only min/max and the
 *		vector/column indices are needed by the caller. The min/max bytes are
 *		copied into the current memory context.
 */
List *
PgColumnarReadZoneMapVectors(uint64 storageId, uint64 groupNumber, Snapshot snapshot)
{
	Relation	rel = open_columnar_table("zone_map", AccessShareLock);
	TupleDesc	tupdesc = RelationGetDescr(rel);
	ScanKeyData key[2];
	SysScanDesc scan;
	Oid			idxOid;
	HeapTuple	tuple;
	List	   *result = NIL;

	ScanKeyInit(&key[0], Anum_zone_map_storage_id, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) storageId));
	ScanKeyInit(&key[1], Anum_zone_map_group_number, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) groupNumber));
	idxOid = pgcolumnar_index_oid("zone_map_pkey");
	scan = systable_beginscan(rel, idxOid, OidIsValid(idxOid), snapshot,
							  2, key);
	while (HeapTupleIsValid(tuple = systable_getnext(scan)))
	{
		bool		isnull;
		int32		vecIndex = DatumGetInt32(
			heap_getattr(tuple, Anum_zone_map_vector_index, tupdesc, &isnull));

		if (vecIndex < 0)
			continue;			/* per-vector rows only */

		result = lappend(result,
						 zonemap_from_tuple(tuple, tupdesc, storageId,
											groupNumber, vecIndex));
	}
	systable_endscan(scan);
	table_close(rel, AccessShareLock);

	return result;
}

/*
 * PgColumnarReadZoneMapVectorsForColumn
 *		The per-vector zone maps of ONE column of a row group. Mirrors
 *		PgColumnarReadZoneMapForColumn: per-vector skipping consults only the
 *		predicate columns, and every column's per-vector rows carry the same
 *		value/null counts, so a scan reads the predicate columns' rows plus one
 *		column's for the vector spans, not every attribute's. Probes zone_map_pkey
 *		on (storage_id, group_number, column_index).
 */
List *
PgColumnarReadZoneMapVectorsForColumn(uint64 storageId, uint64 groupNumber,
									  int columnIndex, Snapshot snapshot)
{
	Relation	rel = open_columnar_table("zone_map", AccessShareLock);
	TupleDesc	tupdesc = RelationGetDescr(rel);
	ScanKeyData key[3];
	SysScanDesc scan;
	Oid			idxOid;
	HeapTuple	tuple;
	List	   *result = NIL;

	ScanKeyInit(&key[0], Anum_zone_map_storage_id, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) storageId));
	ScanKeyInit(&key[1], Anum_zone_map_group_number, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) groupNumber));
	ScanKeyInit(&key[2], Anum_zone_map_column_index, BTEqualStrategyNumber,
				F_INT2EQ, Int16GetDatum((int16) columnIndex));
	idxOid = pgcolumnar_index_oid("zone_map_pkey");
	scan = systable_beginscan(rel, idxOid, OidIsValid(idxOid), snapshot, 3, key);
	while (HeapTupleIsValid(tuple = systable_getnext(scan)))
	{
		bool		isnull;
		int32		vecIndex = DatumGetInt32(
			heap_getattr(tuple, Anum_zone_map_vector_index, tupdesc, &isnull));

		if (vecIndex < 0)
			continue;			/* per-vector rows only */
		result = lappend(result,
						 zonemap_from_tuple(tuple, tupdesc, storageId,
											groupNumber, vecIndex));
	}
	systable_endscan(scan);
	table_close(rel, AccessShareLock);

	return result;
}

/* order native row groups by group_number */
static int
row_group_cmp(const ListCell *a, const ListCell *b)
{
	const NativeRowGroupMetadata *ra = lfirst(a);
	const NativeRowGroupMetadata *rb = lfirst(b);

	if (ra->groupNumber < rb->groupNumber)
		return -1;
	if (ra->groupNumber > rb->groupNumber)
		return 1;
	return 0;
}

/*
 * PgColumnarReadRowGroupList
 *		The native row groups of a storage, ordered by group number.
 */
List *
PgColumnarReadRowGroupList(uint64 storageId, Snapshot snapshot)
{
	Relation	rel = open_columnar_table("row_group", AccessShareLock);
	TupleDesc	tupdesc = RelationGetDescr(rel);
	ScanKeyData key[1];
	SysScanDesc scan;
	HeapTuple	tuple;
	List	   *result = NIL;
	Oid			rgIdx = pgcolumnar_index_oid("row_group_pkey");

	ScanKeyInit(&key[0], Anum_row_group_storage_id, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) storageId));
	/*
	 * Index scan on row_group_pkey, so a fetch reads this storage's groups
	 * rather than a heap scan of every storage's row groups in the database.
	 *
	 * This read is on the unique-check path: _bt_check_unique() reaches it
	 * through pgcolumnar_index_fetch_tuple() under its own on-stack SnapshotDirty
	 * and reads xmin/xmax back out afterwards. A bare index scan here once
	 * spun the check forever (test/unique_conc.sh scenario 7): when this storage
	 * has nothing flushed the scan matches no rows, HeapTupleSatisfiesDirty()
	 * never runs, and the uninitialised out-fields are read as a phantom xact to
	 * wait on. pgcolumnar_index_fetch_tuple() now resets those fields before the
	 * fetch, the way HeapTupleSatisfiesDirty() does for a heap row, so the index
	 * scan is safe: an in-progress group still sets xmin here and the check waits
	 * on the real inserter.
	 */
	scan = systable_beginscan(rel, rgIdx, OidIsValid(rgIdx), snapshot, 1, key);
	while (HeapTupleIsValid(tuple = systable_getnext(scan)))
	{
		NativeRowGroupMetadata *rg = palloc0(sizeof(NativeRowGroupMetadata));
		bool		isnull;

		rg->storageId = storageId;
		rg->groupNumber = (uint64) DatumGetInt64(
			heap_getattr(tuple, Anum_row_group_group_number, tupdesc, &isnull));
		rg->fileOffset = (uint64) DatumGetInt64(
			heap_getattr(tuple, Anum_row_group_file_offset, tupdesc, &isnull));
		rg->rowCount = (uint64) DatumGetInt64(
			heap_getattr(tuple, Anum_row_group_row_count, tupdesc, &isnull));
		rg->byteLength = (uint64) DatumGetInt64(
			heap_getattr(tuple, Anum_row_group_byte_length, tupdesc, &isnull));
		rg->firstRowNumber = (uint64) DatumGetInt64(
			heap_getattr(tuple, Anum_row_group_first_row_number, tupdesc, &isnull));
		result = lappend(result, rg);
	}
	systable_endscan(scan);
	table_close(rel, AccessShareLock);

	list_sort(result, row_group_cmp);
	return result;
}

/*
 * PgColumnarReadColumnChunkList
 *		The native column chunks of one row group. The caller indexes the result
 *		by column_index; the encoding descriptor bytes are copied into the
 *		current memory context.
 */
List *
PgColumnarReadColumnChunkList(uint64 storageId, uint64 groupNumber, Snapshot snapshot)
{
	Relation	rel = open_columnar_table("column_chunk", AccessShareLock);
	TupleDesc	tupdesc = RelationGetDescr(rel);
	ScanKeyData key[2];
	SysScanDesc scan;
	Oid			idxOid;
	HeapTuple	tuple;
	List	   *result = NIL;

	ScanKeyInit(&key[0], Anum_column_chunk_storage_id, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) storageId));
	ScanKeyInit(&key[1], Anum_column_chunk_group_number, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) groupNumber));
	idxOid = pgcolumnar_index_oid("column_chunk_pkey");
	scan = systable_beginscan(rel, idxOid, OidIsValid(idxOid), snapshot,
							  2, key);
	while (HeapTupleIsValid(tuple = systable_getnext(scan)))
	{
		NativeColumnChunkMetadata *cc = palloc0(sizeof(NativeColumnChunkMetadata));
		bool		isnull;
		Datum		d;

		cc->storageId = storageId;
		cc->groupNumber = groupNumber;
		cc->columnIndex = DatumGetInt16(
			heap_getattr(tuple, Anum_column_chunk_column_index, tupdesc, &isnull));
		cc->valueCount = (uint64) DatumGetInt64(
			heap_getattr(tuple, Anum_column_chunk_value_count, tupdesc, &isnull));
		d = heap_getattr(tuple, Anum_column_chunk_encoding_descriptor, tupdesc, &isnull);
		if (!isnull)
		{
			bytea	   *b = DatumGetByteaPP(d);

			cc->encodingDescriptorLen = VARSIZE_ANY_EXHDR(b);
			cc->encodingDescriptor = (const char *)
				memcpy(palloc(cc->encodingDescriptorLen + 1),
					   VARDATA_ANY(b), cc->encodingDescriptorLen);
		}
		cc->blockCodec = DatumGetInt16(
			heap_getattr(tuple, Anum_column_chunk_block_codec, tupdesc, &isnull));
		cc->pageOffset = (uint64) DatumGetInt64(
			heap_getattr(tuple, Anum_column_chunk_page_offset, tupdesc, &isnull));
		cc->pageLength = (uint64) DatumGetInt64(
			heap_getattr(tuple, Anum_column_chunk_page_length, tupdesc, &isnull));
		result = lappend(result, cc);
	}
	systable_endscan(scan);
	table_close(rel, AccessShareLock);

	return result;
}

/*
 * zonemap_from_tuple
 *		Build a NativeZoneMapMetadata from one whole-chunk zone_map tuple. Shared
 *		by the whole-group reader and the per-column probe; the caller has
 *		confirmed vector_index == -1 and holds the relation open. min/max/sum are
 *		copied into the current memory context.
 */
static NativeZoneMapMetadata *
zonemap_from_tuple(HeapTuple tuple, TupleDesc tupdesc,
				   uint64 storageId, uint64 groupNumber, int32 vecIndex)
{
	NativeZoneMapMetadata *z = palloc0(sizeof(NativeZoneMapMetadata));
	bool		isnull;
	Datum		d;

	z->storageId = storageId;
	z->groupNumber = groupNumber;
	z->columnIndex = DatumGetInt16(
		heap_getattr(tuple, Anum_zone_map_column_index, tupdesc, &isnull));
	z->vectorIndex = vecIndex;

	d = heap_getattr(tuple, Anum_zone_map_minimum, tupdesc, &isnull);
	if (!isnull)
	{
		bytea	   *bmin = DatumGetByteaPP(d);
		Datum		dmax = heap_getattr(tuple, Anum_zone_map_maximum, tupdesc, &isnull);

		if (!isnull)
		{
			bytea	   *bmax = DatumGetByteaPP(dmax);

			z->minimumLen = VARSIZE_ANY_EXHDR(bmin);
			z->minimum = (const char *) memcpy(palloc(z->minimumLen + 1),
											   VARDATA_ANY(bmin), z->minimumLen);
			z->maximumLen = VARSIZE_ANY_EXHDR(bmax);
			z->maximum = (const char *) memcpy(palloc(z->maximumLen + 1),
											   VARDATA_ANY(bmax), z->maximumLen);
			z->hasMinMax = true;
		}
	}

	d = heap_getattr(tuple, Anum_zone_map_sum, tupdesc, &isnull);
	if (!isnull)
	{
		z->sum = datumCopy(d, false, -1);	/* numeric is varlena */
		z->hasSum = true;
	}

	/*
	 * The range upper-bound summary (#1144), read ONLY when the catalog has the
	 * column. A pgcolumnar installed before the alpha5 upgrade has nine
	 * attributes here, and heap_getattr past natts CRASHES rather than returning
	 * NULL, so the guard is the difference between "this table predates the
	 * feature" and a backend that dies reading an old table.
	 *
	 * NULL and zero-length are different answers and both mean "do not prune
	 * above": NULL is no summary at all, zero length is a unit that holds a
	 * range with no upper bound. Keeping them apart is what lets a reader say
	 * WHY it could not prune.
	 */
	if (tupdesc->natts >= Anum_zone_map_max_upper)
	{
		d = heap_getattr(tuple, Anum_zone_map_max_upper, tupdesc, &isnull);
		if (!isnull)
		{
			bytea	   *bup = DatumGetByteaPP(d);

			z->hasMaxUpper = true;
			z->maxUpperLen = VARSIZE_ANY_EXHDR(bup);
			if (z->maxUpperLen > 0)
				z->maxUpper = (const char *) memcpy(palloc(z->maxUpperLen + 1),
													VARDATA_ANY(bup),
													z->maxUpperLen);
			else
			{
				z->maxUpper = NULL;
				z->upperUnbounded = true;
			}
		}
	}

	z->valueCount = (uint64) DatumGetInt64(
		heap_getattr(tuple, Anum_zone_map_value_count, tupdesc, &isnull));
	z->nullCount = (uint64) DatumGetInt64(
		heap_getattr(tuple, Anum_zone_map_null_count, tupdesc, &isnull));
	return z;
}

/*
 * PgColumnarReadZoneMapList
 *		The whole-chunk zone maps (vector_index -1) of one row group, for group
 *		skipping (native spec 7.1, Phase D5b). The caller indexes the result by
 *		column_index; the minimum/maximum bytes are copied into the current memory
 *		context. Per-vector rows (vector_index >= 0) are skipped by this reader.
 */
List *
PgColumnarReadZoneMapList(uint64 storageId, uint64 groupNumber, Snapshot snapshot)
{
	Relation	rel = open_columnar_table("zone_map", AccessShareLock);
	TupleDesc	tupdesc = RelationGetDescr(rel);
	ScanKeyData key[2];
	SysScanDesc scan;
	Oid			idxOid;
	HeapTuple	tuple;
	List	   *result = NIL;

	ScanKeyInit(&key[0], Anum_zone_map_storage_id, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) storageId));
	ScanKeyInit(&key[1], Anum_zone_map_group_number, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) groupNumber));
	idxOid = pgcolumnar_index_oid("zone_map_pkey");
	scan = systable_beginscan(rel, idxOid, OidIsValid(idxOid), snapshot,
							  2, key);
	while (HeapTupleIsValid(tuple = systable_getnext(scan)))
	{
		bool		isnull;
		int32		vecIndex = DatumGetInt32(
			heap_getattr(tuple, Anum_zone_map_vector_index, tupdesc, &isnull));

		if (vecIndex != -1)
			continue;			/* whole-chunk rows only, for group skipping */

		result = lappend(result,
						 zonemap_from_tuple(tuple, tupdesc, storageId, groupNumber, -1));
	}
	systable_endscan(scan);
	table_close(rel, AccessShareLock);

	return result;
}

/*
 * PgColumnarReadZoneMapForColumn
 *		The whole-chunk zone map of ONE column of a row group, for group skipping.
 *		Mirrors PgColumnarReadBloomForColumn: a scan wants the zone maps of only
 *		the columns carrying predicates, so reading a whole group's worth (all
 *		attributes) is wasted work and buffer traffic on a wide table. zone_map_pkey
 *		is (storage_id, group_number, column_index, vector_index), so this probes
 *		the exact column. Returns NULL when the column has no whole-chunk zone map.
 */
NativeZoneMapMetadata *
PgColumnarReadZoneMapForColumn(uint64 storageId, uint64 groupNumber,
							   int columnIndex, Snapshot snapshot,
							   PgColumnarZoneMapSession *sess)
{
	Relation	rel;
	TupleDesc	tupdesc;
	ScanKeyData key[3];
	SysScanDesc scan;
	Oid			idxOid;
	HeapTuple	tuple;
	NativeZoneMapMetadata *result = NULL;

	/*
	 * #744. Inside a read session the relation and the index oid are resolved
	 * once and held for the scan; outside one this behaves exactly as before.
	 */
	if (sess != NULL)
	{
		sess->probes++;
		if (sess->rel == NULL)
		{
			sess->rel = open_columnar_table("zone_map", AccessShareLock);
			sess->idxOid = pgcolumnar_index_oid("zone_map_pkey");
			sess->opens++;
		}
		rel = sess->rel;
		idxOid = sess->idxOid;
	}
	else
	{
		rel = open_columnar_table("zone_map", AccessShareLock);
		idxOid = pgcolumnar_index_oid("zone_map_pkey");
	}
	tupdesc = RelationGetDescr(rel);

	ScanKeyInit(&key[0], Anum_zone_map_storage_id, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) storageId));
	ScanKeyInit(&key[1], Anum_zone_map_group_number, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) groupNumber));
	ScanKeyInit(&key[2], Anum_zone_map_column_index, BTEqualStrategyNumber,
				F_INT2EQ, Int16GetDatum((int16) columnIndex));
	/*
	 * idxOid is already resolved above -- from the session when there is one,
	 * by lookup when there is not. A second unconditional
	 * pgcolumnar_index_oid("zone_map_pkey") stood here and overwrote both, so
	 * #744's cache stored an index oid that every probe discarded and
	 * re-derived. The `opens` counter never noticed, because it counts relation
	 * opens, which the session really did save; the cache read as wholly
	 * effective while half of what it cached was thrown away. A dead store
	 * draws no compiler warning, which is how it survived.
	 *
	 * The sibling PgColumnarReadZoneMapVectorsForColumn was checked and has one
	 * lookup, not two.
	 */
	scan = systable_beginscan(rel, idxOid, OidIsValid(idxOid), snapshot, 3, key);
	while (HeapTupleIsValid(tuple = systable_getnext(scan)))
	{
		bool		isnull;
		int32		vecIndex = DatumGetInt32(
			heap_getattr(tuple, Anum_zone_map_vector_index, tupdesc, &isnull));

		if (vecIndex != -1)
			continue;			/* whole-chunk row only */
		result = zonemap_from_tuple(tuple, tupdesc, storageId, groupNumber, -1);
		break;
	}
	systable_endscan(scan);
	if (sess == NULL)
		table_close(rel, AccessShareLock);

	return result;
}

/* -------------------------------------------------------------------------
 * per-table options (spec 7.4)
 * ------------------------------------------------------------------------- */

/*
 * pgcolumnar_compression_from_name
 *		Map a compression codec name to its code (spec 5). Returns -1 for an
 *		unrecognized name so the caller can fall back to the instance default.
 */
static int
pgcolumnar_compression_from_name(const char *name)
{
	if (strcmp(name, "none") == 0)
		return COLUMNAR_COMPRESSION_NONE;
	if (strcmp(name, "pglz") == 0)
		return COLUMNAR_COMPRESSION_PGLZ;
	if (strcmp(name, "lz4") == 0)
		return COLUMNAR_COMPRESSION_LZ4;
	if (strcmp(name, "zstd") == 0)
		return COLUMNAR_COMPRESSION_ZSTD;
	return -1;
}

/*
 * PgColumnarReadOptions
 *		Load the per-table options row for a relation (spec 7.4) into *opts,
 *		setting a per-field "set" flag for each column that is present (not
 *		SQL NULL). Returns true when a row exists. The catalog is read with a
 *		command-id-advanced snapshot so options set earlier in this transaction
 *		take effect for subsequent writes (spec 9).
 */
bool
PgColumnarReadOptions(Oid relid, PgColumnarOptions *opts)
{
	Relation	rel = open_columnar_table("options", AccessShareLock);
	TupleDesc	tupdesc = RelationGetDescr(rel);
	ScanKeyData key[1];
	SysScanDesc scan;
	HeapTuple	tuple;
	Snapshot	base;
	Snapshot	snapshot;
	bool		found = false;

	memset(opts, 0, sizeof(PgColumnarOptions));

	base = ActiveSnapshotSet() ? GetActiveSnapshot() : GetTransactionSnapshot();
	snapshot = PgColumnarCatalogSnapshot(base);

	ScanKeyInit(&key[0], Anum_options_regclass, BTEqualStrategyNumber,
				F_OIDEQ, ObjectIdGetDatum(relid));

	/*
	 * options_pkey is (regclass), the same column this key names. Passing
	 * InvalidOid walked every columnar table's options row on a plan that
	 * asked about one of them.
	 */
	{
		Oid			optIdx = pgcolumnar_scan_index_oid(rel, "options_pkey");

		scan = systable_beginscan(rel, optIdx, OidIsValid(optIdx), snapshot, 1, key);
	}
	if (HeapTupleIsValid(tuple = systable_getnext(scan)))
	{
		bool		isnull;
		Datum		d;

		found = true;

		d = heap_getattr(tuple, Anum_options_chunk_group_row_limit, tupdesc, &isnull);
		if (!isnull)
		{
			opts->chunkGroupRowLimitSet = true;
			opts->chunkGroupRowLimit = DatumGetInt32(d);
		}

		d = heap_getattr(tuple, Anum_options_stripe_row_limit, tupdesc, &isnull);
		if (!isnull)
		{
			opts->stripeRowLimitSet = true;
			opts->stripeRowLimit = DatumGetInt32(d);
		}

		d = heap_getattr(tuple, Anum_options_compression_level, tupdesc, &isnull);
		if (!isnull)
		{
			opts->compressionLevelSet = true;
			opts->compressionLevel = DatumGetInt32(d);
		}

		d = heap_getattr(tuple, Anum_options_compression, tupdesc, &isnull);
		if (!isnull)
		{
			int			code = pgcolumnar_compression_from_name(NameStr(*DatumGetName(d)));

			if (code >= 0)
			{
				opts->compressionSet = true;
				opts->compressionType = code;
			}
		}

		d = heap_getattr(tuple, Anum_options_encode_effort, tupdesc, &isnull);
		if (!isnull)
		{
			const char *name = NameStr(*DatumGetName(d));

			/*
			 * An unrecognised value is ignored rather than raised on. set_options
			 * rejects anything but "full" and "fast", so reaching this with
			 * something else means the catalog was edited directly, and a write
			 * path is the wrong place to fail for that.
			 */
			if (strcmp(name, "fast") == 0)
			{
				opts->encodeEffortSet = true;
				opts->encodeEffort = COLUMNAR_ENCODE_EFFORT_FAST;
			}
			else if (strcmp(name, "full") == 0)
			{
				opts->encodeEffortSet = true;
				opts->encodeEffort = COLUMNAR_ENCODE_EFFORT_FULL;
			}
		}

	}
	systable_endscan(scan);
	table_close(rel, AccessShareLock);

	return found;
}

/*
 * The stripe (row group) row limit in force for a relation: the per-table
 * `stripe_row_limit` option when set, else the GUC default. The writer honors
 * the per-table override, so every consumer that reasons about how many row
 * groups a table has (the writer, the zone-map survival estimate, the index-
 * fetch cost penalty) must read the same effective value. Reading only the GUC
 * mis-sizes the group count for a table that set the option (#7 / #11).
 */
int
pgcolumnar_effective_stripe_row_limit(Oid relid)
{
	PgColumnarOptions opts;

	if (PgColumnarReadOptions(relid, &opts) &&
		opts.stripeRowLimitSet && opts.stripeRowLimit > 0)
		return opts.stripeRowLimit;
	return pgcolumnar_stripe_row_limit;
}

/*
 * pgcolumnar_written_stripe_row_limit
 *		The row-group limit the table was actually WRITTEN with, read back from
 *		pgcolumnar.storage.row_group_limit (#806).
 *
 *		The planner must ask this and not pgcolumnar_effective_stripe_row_limit.
 *		That function answers "what limit would a write in THIS session use",
 *		which is the right question for the writer -- columnar_write_state.c is
 *		deciding the geometry it is about to lay down -- and the wrong one for a
 *		cost model, which is describing geometry that already exists. A table
 *		written while pgcolumnar.stripe_row_limit differed from the planning
 *		session's value is otherwise costed against a shape it does not have:
 *		measured at 20 groups of 20,000 rows being costed as 3 of 150,000.
 *
 *		Falls back to the session answer only when there is neither an explicit
 *		per-table option nor a storage row -- a table that has never been
 *		written. There is no written geometry to prefer then, and the next write
 *		will use the session value anyway.
 *
 *		This is ONE number, the last writer's. A table whose groups were laid
 *		down under changing limits is still approximated. The exact quantity is
 *		the real group count, and reading it is a scan proportional to the number
 *		of groups on every plan, which is too much to spend refining a term that
 *		is approximate by construction.
 *
 *		Scanned without an index: storage_pkey is on storage_id and this
 *		looks up by relation_oid. options_pkey does match its lookup, and
 *		PgColumnarReadOptions uses it; this one cannot.
 */
int
pgcolumnar_written_stripe_row_limit(Oid relid)
{
	PgColumnarOptions opts;
	Relation	rel;
	TupleDesc	tupdesc;
	ScanKeyData key[1];
	SysScanDesc scan;
	HeapTuple	tuple;
	int32		limit = 0;

	/*
	 * An explicit per-table option still wins, and deliberately so. #806 is
	 * about the PLANNING SESSION's GUC leaking into the cost of a table it has
	 * nothing to do with. A per-table stripe_row_limit is the opposite: a
	 * durable statement by the table's owner about that table, which
	 * native_index_fetch_stripe_cost asserts the cost model can see.
	 *
	 * It is worth naming what this leaves unresolved. Setting the option does
	 * not rewrite the table, so between the option and the next rewrite the
	 * model describes the geometry the owner asked for rather than the one on
	 * disk. That is a real gap, it predates this change, and narrowing #806 to
	 * the session GUC is not the place to decide it.
	 */
	if (PgColumnarReadOptions(relid, &opts) &&
		opts.stripeRowLimitSet && opts.stripeRowLimit > 0)
		return opts.stripeRowLimit;

	rel = open_columnar_table("storage", AccessShareLock);
	tupdesc = RelationGetDescr(rel);

	ScanKeyInit(&key[0], Anum_native_storage_relation_oid, BTEqualStrategyNumber,
				F_OIDEQ, ObjectIdGetDatum(relid));
	scan = systable_beginscan(rel, InvalidOid, false, NULL, 1, key);

	if (HeapTupleIsValid(tuple = systable_getnext(scan)))
	{
		bool		isnull;
		Datum		d = heap_getattr(tuple, Anum_native_storage_row_group_limit,
									 tupdesc, &isnull);

		if (!isnull)
			limit = DatumGetInt32(d);
	}

	systable_endscan(scan);
	table_close(rel, AccessShareLock);

	if (limit > 0)
		return (int) limit;

	/* never written and no option: the session's value is all there is */
	return pgcolumnar_stripe_row_limit;
}


/*
 * PgColumnarReadSortBy
 *		Load the declared sort_by column names for a relation (#288) from
 *		pgcolumnar.options. Returns a List of pstrdup'd column-name strings in
 *		the caller's memory context, or NIL when no sort key is declared (no
 *		options row, or sort_by is SQL NULL, or the array is empty). Stored as
 *		column NAMES, not attnums, so it survives dump/restore; the caller
 *		resolves the names to attnums and validates them each apply. Read with
 *		the same command-id-advanced snapshot as PgColumnarReadOptions so a
 *		sort_by set earlier in this transaction is visible.
 */
/*
 * PgColumnarReadTtl
 *		The retention declared for this table by set_options (#403 item 5a), or
 *		false when it has none. Read with the same command-id-advanced snapshot
 *		as PgColumnarReadOptions, so a retention set earlier in this transaction
 *		is visible.
 *
 *		Both halves must be present to mean anything: a column with no interval
 *		names nothing to compare against, and an interval with no column has
 *		nothing to compare. Either alone reads as "no retention", so a half
 *		declaration cannot drop rows.
 */
bool
PgColumnarReadTtl(Oid relid, char **column, Interval **interval)
{
	Relation	rel = open_columnar_table("options", AccessShareLock);
	TupleDesc	tupdesc = RelationGetDescr(rel);
	ScanKeyData key[1];
	SysScanDesc scan;
	HeapTuple	tuple;
	Snapshot	base;
	Snapshot	snapshot;
	bool		found = false;

	*column = NULL;
	*interval = NULL;

	base = ActiveSnapshotSet() ? GetActiveSnapshot() : GetTransactionSnapshot();
	snapshot = PgColumnarCatalogSnapshot(base);

	ScanKeyInit(&key[0], Anum_options_regclass, BTEqualStrategyNumber,
				F_OIDEQ, ObjectIdGetDatum(relid));
	{
		Oid			optIdx = pgcolumnar_scan_index_oid(rel, "options_pkey");

		scan = systable_beginscan(rel, optIdx, OidIsValid(optIdx), snapshot, 1, key);
	}
	tuple = systable_getnext(scan);
	if (HeapTupleIsValid(tuple))
	{
		bool		colnull;
		bool		ivnull;
		Datum		cold = heap_getattr(tuple, Anum_options_ttl_column,
										tupdesc, &colnull);
		Datum		ivd = heap_getattr(tuple, Anum_options_ttl_interval,
									   tupdesc, &ivnull);

		if (!colnull && !ivnull)
		{
			*column = pstrdup(NameStr(*DatumGetName(cold)));
			*interval = (Interval *) palloc(sizeof(Interval));
			memcpy(*interval, DatumGetIntervalP(ivd), sizeof(Interval));
			found = true;
		}
	}
	systable_endscan(scan);
	table_close(rel, AccessShareLock);

	return found;
}

List *
PgColumnarReadSortBy(Oid relid)
{
	Relation	rel = open_columnar_table("options", AccessShareLock);
	TupleDesc	tupdesc = RelationGetDescr(rel);
	ScanKeyData key[1];
	SysScanDesc scan;
	HeapTuple	tuple;
	Snapshot	base;
	Snapshot	snapshot;
	List	   *names = NIL;

	base = ActiveSnapshotSet() ? GetActiveSnapshot() : GetTransactionSnapshot();
	snapshot = PgColumnarCatalogSnapshot(base);

	ScanKeyInit(&key[0], Anum_options_regclass, BTEqualStrategyNumber,
				F_OIDEQ, ObjectIdGetDatum(relid));

	{
		Oid			optIdx = pgcolumnar_scan_index_oid(rel, "options_pkey");

		scan = systable_beginscan(rel, optIdx, OidIsValid(optIdx), snapshot, 1, key);
	}
	if (HeapTupleIsValid(tuple = systable_getnext(scan)))
	{
		bool		isnull;
		Datum		d = heap_getattr(tuple, Anum_options_sort_by, tupdesc, &isnull);

		if (!isnull)
		{
			ArrayType  *arr = DatumGetArrayTypeP(d);
			Datum	   *elems;
			bool	   *elnulls;
			int			n;
			int			i;

			deconstruct_array(arr, NAMEOID, NAMEDATALEN, false, 'c',
							  &elems, &elnulls, &n);
			for (i = 0; i < n; i++)
			{
				/* set_options rejects NULL names; guard defensively anyway */
				if (elnulls[i])
					continue;
				names = lappend(names,
								pstrdup(NameStr(*DatumGetName(elems[i]))));
			}
		}
	}
	systable_endscan(scan);
	table_close(rel, AccessShareLock);

	return names;
}


/*
 * PgColumnarDeleteOptions
 *		Remove a relation's per-table options row, called when the table is
 *		dropped. The options table is keyed by regclass (relation oid), not by
 *		storage id, so it is cleaned up separately from PgColumnarDeleteMetadata.
 */
void
PgColumnarDeleteOptions(Oid relid)
{
	Relation	rel = open_columnar_table("options", RowExclusiveLock);
	ScanKeyData key[1];
	SysScanDesc scan;
	HeapTuple	tuple;

	ScanKeyInit(&key[0], Anum_options_regclass, BTEqualStrategyNumber,
				F_OIDEQ, ObjectIdGetDatum(relid));

	{
		Oid			optIdx = pgcolumnar_scan_index_oid(rel, "options_pkey");

		scan = systable_beginscan(rel, optIdx, OidIsValid(optIdx), NULL, 1, key);
	}
	while (HeapTupleIsValid(tuple = systable_getnext(scan)))
		CatalogTupleDelete(rel, &tuple->t_self);
	systable_endscan(scan);

	table_close(rel, RowExclusiveLock);
}

/*
 * PgColumnarIsColumnarRelation
 *		Whether a relation uses the columnar table access method AND has
 *		storage of its own. The access method oid is resolved once and cached.
 *
 *		THE RELKIND TEST IS NOT DECORATION (#1259). From PostgreSQL 17 a
 *		PARTITIONED table may carry relam = pgcolumnar, as the default access
 *		method for its future partitions, and it has no storage: relfilenode is
 *		0. Every caller that took the true branch and then read storage handed
 *		smgropen a zero relfilenumber, which on an assert build aborts the
 *		backend and restarts the cluster:
 *
 *		    TRAP: failed Assert("RelFileNumberIsValid(rlocator.relNumber)"),
 *		          File: "smgr.c", Line: 204
 *
 *		Ten entry points were measured reaching it. On a production build it
 *		does not crash, it answers wrong -- get_storage_id returns NULL and
 *		stats and sort_status build on that -- which is the harder half to
 *		notice.
 *
 *		RELKIND_HAS_STORAGE RATHER THAN == RELKIND_RELATION, and the difference
 *		is load-bearing: a MATERIALIZED VIEW can be columnar. Measured --
 *		`CREATE MATERIALIZED VIEW mv USING pgcolumnar AS ...` succeeds, and the
 *		result has relkind 'm', relam 'pgcolumnar' and readable rows. Narrowing
 *		to RELKIND_RELATION would have stopped recognising it at all 31 call
 *		sites, silently. The macro admits it and excludes the partitioned
 *		parent, which is exactly the property the callers need.
 */
bool
PgColumnarIsColumnarRelation(Oid relid)
{
	static Oid	columnarAmOid = InvalidOid;

	if (columnarAmOid == InvalidOid)
		columnarAmOid = get_am_oid("pgcolumnar", true);

	if (!OidIsValid(columnarAmOid) || get_rel_relam(relid) != columnarAmOid)
		return false;

	return RELKIND_HAS_STORAGE(get_rel_relkind(relid));
}

/* -------------------------------------------------------------------------
 * projection catalog (gap 26, format 2.2)
 * ------------------------------------------------------------------------- */

/* build a smallint[] Datum from a C int16 array (empty array when n <= 0) */
static Datum
int16_array_datum(const int16 *vals, int n)
{
	ArrayType  *arr;

	if (n <= 0)
		arr = construct_empty_array(INT2OID);
	else
	{
		Datum	   *elems = palloc(sizeof(Datum) * n);
		int			i;

		for (i = 0; i < n; i++)
			elems[i] = Int16GetDatum(vals[i]);
		arr = construct_array(elems, n, INT2OID, 2, true, TYPALIGN_SHORT);
		pfree(elems);
	}
	return PointerGetDatum(arr);
}

/* read a smallint[] Datum into a palloc'd C int16 array; *len set to count */
static int16 *
int16_array_from_datum(Datum d, int *len)
{
	ArrayType  *arr = DatumGetArrayTypeP(d);
	Datum	   *elems;
	bool	   *nulls;
	int			n;
	int16	   *out;
	int			i;

	deconstruct_array(arr, INT2OID, 2, true, TYPALIGN_SHORT, &elems, &nulls, &n);
	out = (n > 0) ? palloc(sizeof(int16) * n) : NULL;
	for (i = 0; i < n; i++)
		out[i] = DatumGetInt16(elems[i]);
	*len = n;
	return out;
}

/* order a projection list by projection_id ascending (base first) */
static int
projection_cmp(const ListCell *a, const ListCell *b)
{
	const PgColumnarProjection *pa = (const PgColumnarProjection *) lfirst(a);
	const PgColumnarProjection *pb = (const PgColumnarProjection *) lfirst(b);

	if (pa->projectionId < pb->projectionId)
		return -1;
	if (pa->projectionId > pb->projectionId)
		return 1;
	return 0;
}

void
PgColumnarInsertProjectionRow(const PgColumnarProjection *proj)
{
	Relation	rel = open_columnar_table("projection", RowExclusiveLock);
	TupleDesc	tupdesc = RelationGetDescr(rel);
	Datum		values[Natts_projection];
	bool		nulls[Natts_projection];
	HeapTuple	tuple;

	memset(nulls, false, sizeof(nulls));
	values[Anum_projection_storage_id - 1] = Int64GetDatum((int64) proj->storageId);
	values[Anum_projection_projection_id - 1] = Int32GetDatum(proj->projectionId);
	values[Anum_projection_name - 1] =
		DirectFunctionCall1(namein, CStringGetDatum(proj->name));
	values[Anum_projection_proj_storage_id - 1] =
		Int64GetDatum((int64) proj->projStorageId);
	values[Anum_projection_sort_key - 1] =
		int16_array_datum(proj->sortKey, proj->sortKeyLen);
	values[Anum_projection_columns - 1] =
		int16_array_datum(proj->columns, proj->columnsLen);

	tuple = heap_form_tuple(tupdesc, values, nulls);
	CatalogTupleInsert(rel, tuple);
	heap_freetuple(tuple);

	table_close(rel, RowExclusiveLock);
	CommandCounterIncrement();		/* make the row visible to later reads */
}

/*
 * PgColumnarListProjectionDeclarations
 *		Every declared projection for one relation, by column NAME.
 *
 * The declaration is keyed by relation, not by storage id, so it is the only
 * record of a projection that survives a rewrite -- which is what makes it the
 * source a post-rewrite re-record must read from (#876, #887). The storage-id
 * keyed pgcolumnar.projection rows are gone by then.
 *
 * Names rather than attnums, deliberately: that is what the declaration holds,
 * because a dump and restore cannot carry attnums (#266). The caller resolves
 * them against the relation as it is NOW, which is the behaviour a type change
 * or an added column needs.
 */
List *
PgColumnarListProjectionDeclarations(Oid relid)
{
	Relation	rel = open_columnar_table("projection_declaration",
										  AccessShareLock);
	TupleDesc	tupdesc = RelationGetDescr(rel);
	ScanKeyData key[1];
	SysScanDesc scan;
	HeapTuple	tuple;
	List	   *result = NIL;

	ScanKeyInit(&key[0], Anum_projection_declaration_rel, BTEqualStrategyNumber,
				F_OIDEQ, ObjectIdGetDatum(relid));

	scan = systable_beginscan(rel, InvalidOid, false, NULL, 1, key);
	while (HeapTupleIsValid(tuple = systable_getnext(scan)))
	{
		PgColumnarProjectionDeclaration *d =
			palloc0(sizeof(PgColumnarProjectionDeclaration));
		bool		isnull;
		Datum		v;

		d->relid = relid;
		v = heap_getattr(tuple, Anum_projection_declaration_name, tupdesc,
						 &isnull);
		d->name = pstrdup(NameStr(*DatumGetName(v)));
		v = heap_getattr(tuple, Anum_projection_declaration_columns, tupdesc,
						 &isnull);
		/* Copied out of the scan: the tuple is not ours past systable_getnext. */
		d->columns = isnull ? NULL : DatumGetArrayTypePCopy(v);
		v = heap_getattr(tuple, Anum_projection_declaration_sort_key, tupdesc,
						 &isnull);
		d->sortKey = isnull ? NULL : DatumGetArrayTypePCopy(v);

		result = lappend(result, d);
	}
	systable_endscan(scan);
	table_close(rel, AccessShareLock);

	return result;
}

/*
 * PgColumnarRecordProjectionDeclaration
 *		Record the intent behind a projection: which relation, which name, and
 *		which columns by NAME rather than by attnum (#266).
 *
 *		This is written on the same path that creates the projection, so a
 *		projection cannot exist without its declaration. pgcolumnar.projection
 *		records the result and cannot survive a dump, because its key is a
 *		storage id that a restore reassigns; this table can, for the same reason
 *		pgcolumnar.options can.
 *
 *		Replaces any existing row for the pair, so re-declaring a projection does
 *		not leave two declarations behind.
 */
void
PgColumnarRecordProjectionDeclaration(Oid relid, const char *name,
									ArrayType *columns, ArrayType *sortKey)
{
	Relation	rel;
	TupleDesc	tupdesc;
	Datum		values[Natts_projection_declaration];
	bool		nulls[Natts_projection_declaration];
	HeapTuple	tuple;

	PgColumnarDeleteProjectionDeclaration(relid, name);

	rel = open_columnar_table("projection_declaration", RowExclusiveLock);
	tupdesc = RelationGetDescr(rel);

	memset(nulls, false, sizeof(nulls));
	values[Anum_projection_declaration_rel - 1] = ObjectIdGetDatum(relid);
	values[Anum_projection_declaration_name - 1] =
		DirectFunctionCall1(namein, CStringGetDatum(name));
	values[Anum_projection_declaration_columns - 1] = PointerGetDatum(columns);
	values[Anum_projection_declaration_sort_key - 1] = PointerGetDatum(sortKey);

	tuple = heap_form_tuple(tupdesc, values, nulls);
	CatalogTupleInsert(rel, tuple);
	heap_freetuple(tuple);

	table_close(rel, RowExclusiveLock);
	CommandCounterIncrement();
}

/*
 * rename_projection_declaration_array
 *		Replace oldName in one text[] declaration field.
 */
static Datum
rename_projection_declaration_array(Datum value, const char *oldName,
									const char *newName, bool *changed)
{
	ArrayType  *arr = DatumGetArrayTypeP(value);
	Datum	   *elems;
	bool	   *nulls;
	int			nelems;
	int			i;

	deconstruct_array(arr, TEXTOID, -1, false, TYPALIGN_INT,
					  &elems, &nulls, &nelems);

	for (i = 0; i < nelems; i++)
	{
		char	   *name;

		if (nulls[i])
			continue;

		name = TextDatumGetCString(elems[i]);
		if (strcmp(name, oldName) == 0)
		{
			elems[i] = CStringGetTextDatum(newName);
			*changed = true;
		}
	}

	/*
	 * resolve_columns() rejects NULL elements before add_projection() records a
	 * declaration. Preserve the bitmap anyway, so this helper remains safe if a
	 * catalog row created outside that path contains one.
	 */
	return PointerGetDatum(construct_md_array(elems, nulls,
											 ARR_NDIM(arr),
											 ARR_DIMS(arr),
											 ARR_LBOUND(arr),
											 TEXTOID, -1, false,
											 TYPALIGN_INT));
}

/*
 * PgColumnarRenameProjectionDeclarationColumn
 *		Carry ALTER TABLE ... RENAME COLUMN through dumpable declarations.
 *
 * The materialized projection stores attnums, so it follows a rename without
 * any catalog change. The declaration deliberately stores names because
 * pg_dump restores a table with newly assigned attnums. Leaving its old name
 * behind therefore breaks rebuild_projections() after restore even though the
 * live projection continued to work before the backup.
 */
void
PgColumnarRenameProjectionDeclarationColumn(Oid relid, const char *oldName,
											 const char *newName)
{
	Relation	rel;
	TupleDesc	tupdesc;
	ScanKeyData key[1];
	SysScanDesc scan;
	HeapTuple	tuple;

	rel = open_columnar_table("projection_declaration", RowExclusiveLock);
	tupdesc = RelationGetDescr(rel);
	ScanKeyInit(&key[0], Anum_projection_declaration_rel, BTEqualStrategyNumber,
				F_OIDEQ, ObjectIdGetDatum(relid));
	scan = systable_beginscan(rel, InvalidOid, false, NULL, 1, key);

	while (HeapTupleIsValid(tuple = systable_getnext(scan)))
	{
		Datum		values[Natts_projection_declaration];
		bool		nulls[Natts_projection_declaration];
		bool		replace[Natts_projection_declaration];
		bool		changed = false;
		bool		isnull;
		Datum		d;
		HeapTuple	newTuple;

		memset(values, 0, sizeof(values));
		memset(nulls, false, sizeof(nulls));
		memset(replace, false, sizeof(replace));

		d = heap_getattr(tuple, Anum_projection_declaration_columns,
						 tupdesc, &isnull);
		if (!isnull)
		{
			values[Anum_projection_declaration_columns - 1] =
				rename_projection_declaration_array(d, oldName, newName,
													&changed);
			replace[Anum_projection_declaration_columns - 1] = changed;
		}

		d = heap_getattr(tuple, Anum_projection_declaration_sort_key,
						 tupdesc, &isnull);
		if (!isnull)
		{
			bool		sortChanged = false;

			values[Anum_projection_declaration_sort_key - 1] =
				rename_projection_declaration_array(d, oldName, newName,
													&sortChanged);
			replace[Anum_projection_declaration_sort_key - 1] = sortChanged;
			changed = changed || sortChanged;
		}

		/*
		 * This is an unindexed catalog seqscan updated in place. If it encounters
		 * its updated tuple again, the old name is absent and this idempotent arm
		 * prevents a second CatalogTupleUpdate.
		 */
		if (!changed)
			continue;

		newTuple = heap_modify_tuple(tuple, tupdesc, values, nulls, replace);
		CatalogTupleUpdate(rel, &newTuple->t_self, newTuple);
		heap_freetuple(newTuple);
	}

	systable_endscan(scan);
	table_close(rel, RowExclusiveLock);
	CommandCounterIncrement();
}

/*
 * PgColumnarDeleteProjectionDeclaration
 *		Forget the declaration for one projection. Called when it is dropped, and
 *		before recording a replacement.
 */
void
PgColumnarDeleteProjectionDeclaration(Oid relid, const char *name)
{
	Relation	rel = open_columnar_table("projection_declaration",
										  RowExclusiveLock);
	ScanKeyData key[1];
	SysScanDesc scan;
	HeapTuple	tuple;
	TupleDesc	tupdesc = RelationGetDescr(rel);

	ScanKeyInit(&key[0], Anum_projection_declaration_rel, BTEqualStrategyNumber,
				F_OIDEQ, ObjectIdGetDatum(relid));

	scan = systable_beginscan(rel, InvalidOid, false, NULL, 1, key);
	while (HeapTupleIsValid(tuple = systable_getnext(scan)))
	{
		bool		isnull;
		Datum		d = heap_getattr(tuple, Anum_projection_declaration_name,
									 tupdesc, &isnull);

		if (!isnull &&
			strcmp(NameStr(*DatumGetName(d)), name) == 0)
			CatalogTupleDelete(rel, &tuple->t_self);
	}
	systable_endscan(scan);
	table_close(rel, RowExclusiveLock);
	CommandCounterIncrement();
}

/*
 * PgColumnarDeleteProjectionDeclarationsForRel
 *		Forget every declaration for a relation. Called when the relation is
 *		dropped (#304).
 *
 *		Without this a dropped table leaves declaration rows whose regclass no
 *		longer resolves, and the damage is not confined to that table: the
 *		orphan is dumped by config_dump as a bare OID pointing at nothing, and
 *		rebuild_projections() aborts on it, which stops every other table in the
 *		database being rebuilt. pgcolumnar.options avoids this by being cleaned in
 *		the same hook; this catalog needs the same treatment.
 */
void
PgColumnarDeleteProjectionDeclarationsForRel(Oid relid)
{
	Relation	rel = open_columnar_table("projection_declaration",
										  RowExclusiveLock);
	ScanKeyData key[1];
	SysScanDesc scan;
	HeapTuple	tuple;

	ScanKeyInit(&key[0], Anum_projection_declaration_rel, BTEqualStrategyNumber,
				F_OIDEQ, ObjectIdGetDatum(relid));

	scan = systable_beginscan(rel, InvalidOid, false, NULL, 1, key);
	while (HeapTupleIsValid(tuple = systable_getnext(scan)))
		CatalogTupleDelete(rel, &tuple->t_self);
	systable_endscan(scan);

	table_close(rel, RowExclusiveLock);
	CommandCounterIncrement();
}

List *
PgColumnarListProjections(uint64 storageId)
{
	Relation	rel = open_columnar_table("projection", AccessShareLock);
	TupleDesc	tupdesc = RelationGetDescr(rel);
	ScanKeyData key[1];
	SysScanDesc scan;
	HeapTuple	tuple;
	List	   *result = NIL;

	ScanKeyInit(&key[0], Anum_projection_storage_id, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) storageId));

	/*
	 * NULL snapshot -> catalog snapshot: sees committed rows plus this
	 * transaction's own writes after a CommandCounterIncrement (DDL
	 * semantics). projection_pkey leads with storage_id, so the index
	 * answers this key. The snapshot is what makes the index scan see
	 * those writes; dropping it would not.
	 */
	{
		Oid			projIdx = pgcolumnar_scan_index_oid(rel, "projection_pkey");

		scan = systable_beginscan(rel, projIdx, OidIsValid(projIdx), NULL, 1, key);
	}
	while (HeapTupleIsValid(tuple = systable_getnext(scan)))
	{
		PgColumnarProjection *p = palloc0(sizeof(PgColumnarProjection));
		bool		isnull;
		Datum		d;

		p->storageId = storageId;
		p->projectionId = DatumGetInt32(
			heap_getattr(tuple, Anum_projection_projection_id, tupdesc, &isnull));
		d = heap_getattr(tuple, Anum_projection_name, tupdesc, &isnull);
		p->name = pstrdup(NameStr(*DatumGetName(d)));
		p->projStorageId = (uint64) DatumGetInt64(
			heap_getattr(tuple, Anum_projection_proj_storage_id, tupdesc, &isnull));
		d = heap_getattr(tuple, Anum_projection_sort_key, tupdesc, &isnull);
		p->sortKey = int16_array_from_datum(d, &p->sortKeyLen);
		d = heap_getattr(tuple, Anum_projection_columns, tupdesc, &isnull);
		p->columns = int16_array_from_datum(d, &p->columnsLen);

		result = lappend(result, p);
	}
	systable_endscan(scan);
	table_close(rel, AccessShareLock);

	list_sort(result, projection_cmp);
	return result;
}

void
PgColumnarDeleteProjectionRow(uint64 storageId, int projectionId)
{
	Relation	rel = open_columnar_table("projection", RowExclusiveLock);
	ScanKeyData key[2];
	SysScanDesc scan;
	HeapTuple	tuple;

	ScanKeyInit(&key[0], Anum_projection_storage_id, BTEqualStrategyNumber,
				F_INT8EQ, Int64GetDatum((int64) storageId));
	ScanKeyInit(&key[1], Anum_projection_projection_id, BTEqualStrategyNumber,
				F_INT4EQ, Int32GetDatum(projectionId));

	{
		Oid			projIdx = pgcolumnar_scan_index_oid(rel, "projection_pkey");

		scan = systable_beginscan(rel, projIdx, OidIsValid(projIdx), NULL, 2, key);
	}
	while (HeapTupleIsValid(tuple = systable_getnext(scan)))
		CatalogTupleDelete(rel, &tuple->t_self);
	systable_endscan(scan);

	table_close(rel, RowExclusiveLock);
	CommandCounterIncrement();
}
