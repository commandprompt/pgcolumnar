/*-------------------------------------------------------------------------
 *
 * pgcolumnar_tableam.c
 *		Table access method handler for pgColumnar and extension glue:
 *		GUCs, the pre-commit flush hook, and drop-time metadata cleanup.
 *
 * Implements the TableAmRoutine callbacks: create, bulk insert, sequential
 * scan, delete and update via the delete vector, fetch by tid, size
 * estimation, non-transactional truncate, index fetch/build, and lazy VACUUM.
 * Only the TABLESAMPLE (sample) callbacks are stubbed for later phases.
 *
 *-------------------------------------------------------------------------
 */
#include "columnar.h"

#include "columnar_customscan.h"
#include "columnar_sink.h"
#include "columnar_delete_vector.h"
#include "columnar_metadata.h"
#include "columnar_reader.h"
#include "columnar_storage.h"
#include "columnar_write_state.h"
#include "access/multixact.h"
#include "access/genam.h"
#include "access/table.h"
#include "access/relation.h"
#include "access/relscan.h"
#include "access/xact.h"
#include "catalog/index.h"
#include "catalog/objectaccess.h"
#include "catalog/pg_class.h"
#include "catalog/storage.h"
#include "commands/defrem.h"
#include "commands/vacuum.h"
#include "executor/executor.h"
#include "executor/tuptable.h"
#include "miscadmin.h"
#include "replication/logicalworker.h"
#include "nodes/pathnodes.h"
#include "optimizer/optimizer.h"
#include "optimizer/pathnode.h"
#include "optimizer/plancat.h"
#include "port/atomics.h"
#include "storage/bufmgr.h"
#if PG_VERSION_NUM >= 170000
/* the read-stream ANALYZE rework landed in PG17, and so did this header */
#include "storage/read_stream.h"
#endif
#include "storage/lmgr.h"
#include "storage/smgr.h"
#include "storage/spin.h"
#include "utils/builtins.h"
#include "utils/guc.h"
#include "catalog/indexing.h"
#include "catalog/namespace.h"
#include "catalog/pg_am.h"
#include "catalog/pg_inherits.h"
#include "catalog/pg_constraint.h"
#include "tcop/utility.h"
#include "utils/fmgroids.h"
#include "utils/lsyscache.h"
#include "utils/syscache.h"
#include "utils/rel.h"
#include "utils/snapmgr.h"

PG_MODULE_MAGIC;

/* GUC-backed instance defaults (spec 8.3) */
int			pgcolumnar_stripe_row_limit = 150000;
int			pgcolumnar_chunk_group_row_limit = 10000;
int			pgcolumnar_fsst_verdict_reuse = 16;
int			pgcolumnar_encoding_sample_rows = 2048;

int			pgcolumnar_compression = COLUMNAR_COMPRESSION_ZSTD;
int			pgcolumnar_compression_level = 3;
int			pgcolumnar_fsst_min_gain_percent = 5;
int			pgcolumnar_qual_skipvec_min_payload_cols = 20;	/* #595 width gate */
bool		pgcolumnar_enable_qual_pushdown = true;
bool		pgcolumnar_enable_late_materialization = true;
bool		pgcolumnar_enable_column_projection = true;
bool		pgcolumnar_enable_bloom_filter = true;
bool		pgcolumnar_objstore_buffered = true;	/* #393 */
int			pgcolumnar_objstore_part_size = 0;	/* #394 remote part size */
char	   *pgcolumnar_objstore_allowed_endpoints = NULL;	/* #393 allow-list */
char	   *pgcolumnar_objstore_s3_addressing = NULL;	/* #621 path|virtual */
bool		pgcolumnar_autovacuum = false;		/* #415 daemon master switch */
int			pgcolumnar_autovacuum_naptime = 60;
double		pgcolumnar_autovacuum_compact_threshold = 0.2;
double		pgcolumnar_autovacuum_recluster_threshold = 0.05;
int			pgcolumnar_maintenance_hold_ms = 0;	/* dev/test fault injection */

/* value set for columnar.compression (spec 5, 8.3) */
static const struct config_enum_entry pgcolumnar_compression_options[] = {
	{"none", COLUMNAR_COMPRESSION_NONE, false},
	{"pglz", COLUMNAR_COMPRESSION_PGLZ, false},
	{"lz4", COLUMNAR_COMPRESSION_LZ4, false},
	{"zstd", COLUMNAR_COMPRESSION_ZSTD, false},
	{NULL, 0, false}
};

/* forward declaration of the AM routine so hooks can compare against it */
static const TableAmRoutine pgcolumnar_am_methods;

static object_access_hook_type prev_object_access_hook = NULL;
static ProcessUtility_hook_type prev_process_utility_hook = NULL;
static ExecutorEnd_hook_type prev_executor_end_hook = NULL;
#if PG_VERSION_NUM >= 190000
static build_simple_rel_hook_type prev_build_simple_rel_hook = NULL;
#else
static get_relation_info_hook_type prev_get_relation_info_hook = NULL;
#endif

/* cached OID of the "columnar" table access method (index-only-scan hook) */
static Oid	pgcolumnar_am_oid_cache = InvalidOid;

/* our scan descriptor wraps the base scan and the reader state */
/*
 * ANALYZE sampling state (issue #154).
 *
 * The scan_analyze_next_block contract is block-oriented: core's block sampler
 * picks physical blocks, hands one to the AM at a time, and the AM offers the
 * rows on it. A columnar block holds encoded column bytes rather than rows, so
 * the rows "on" it have to be defined rather than read off it.
 *
 * Defining a block as a whole row group would be cluster sampling. A table sorted
 * or Z-ordered on a key holds a narrow slice of that key per group, so whole-group
 * sampling underestimates n_distinct and skews the MCV list -- and does so worst
 * on the tables pgcolumnar.vacuum_sorted and Z-ordering work hardest to produce.
 * Confidently wrong statistics are worse than none.
 *
 * Instead a block maps to the *slice* of its row group that the block's position
 * within that group represents: a group of R rows spanning K blocks is cut into K
 * equal row slices, and block j of the group offers slice j. That keeps the heap
 * analogue exact -- a sampled block offers its own rows and no others, so core's
 * liverows-per-block scaling stays honest -- while spreading the sample across the
 * whole of every group core touches, which is what defeats the clustering trap.
 * A row is offered by exactly one block, so no row can be sampled twice.
 */
typedef struct PgColumnarAnalyzeState
{
	List	   *rowGroups;		/* NativeRowGroupMetadata *, in row order */
	Snapshot	metaSnapshot;
	MemoryContext cx;

	/* the slice the current block maps to; sliceRows == 0 means "no rows here" */
	uint64		sliceFirstRow;
	uint64		sliceRows;
	uint64		sliceNext;		/* next offset within the slice */
	uint64		sliceGroup;		/* the row group the slice belongs to */

	Datum	   *values;
	bool	   *nulls;

	/*
	 * A forward reader over the group the current slice belongs to.
	 *
	 * The rows a slice offers are a contiguous run of row numbers, slices within
	 * a group are visited in ascending order, and groups likewise, so one
	 * forward scan per group serves every slice in it. Fetching each row by
	 * number instead -- which this did first -- re-reads the row group list from
	 * the catalog and re-locates the row on every call, and ANALYZE offers every
	 * row of every block core samples: 250,000 fetches on a 250,000-row table,
	 * which ran for over 200 seconds and did not improve when the statistics
	 * target was lowered, because the work is per row offered rather than per row
	 * kept.
	 */
	PgColumnarReadState *rs;		/* NULL until the first slice with rows */
	uint64		rsGroup;		/* group number rs is restricted to */
	bool		rsHavePending;	/* pendingRow/values hold an unconsumed row */
	uint64		pendingRow;
	Datum	   *pendingValues;
	bool	   *pendingNulls;
} PgColumnarAnalyzeState;

typedef struct PgColumnarScanDescData
{
	TableScanDescData rs_base;
	PgColumnarReadState *readState;

	/*
	 * The context the scan descriptor itself was allocated in. The read state
	 * is built on the first getnextslot, where the current context is usually a
	 * per-tuple one that is reset before the scan ends, so it is allocated here
	 * instead and outlives the row that triggered it.
	 */
	MemoryContext scanContext;
	PgColumnarAnalyzeState *analyzeState;
} PgColumnarScanDescData;
typedef struct PgColumnarScanDescData *PgColumnarScanDesc;

PG_FUNCTION_INFO_V1(pgcolumnar_handler);

/* -------------------------------------------------------------------------
 * slot / scan callbacks
 * ------------------------------------------------------------------------- */

/*
 * Slot operations for a columnar relation (issue #154).
 *
 * These are TTSOpsVirtual with one callback replaced. A virtual slot's
 * copy_heap_tuple is heap_form_tuple over the slot's values, which leaves the
 * new tuple's t_self invalid and never looks at tts_tid -- the slot's item
 * pointer is simply dropped on the way out.
 *
 * That is fatal for ANALYZE rather than merely lossy. acquire_sample_rows
 * collects the sample with ExecCopySlotHeapTuple and then sorts it by item
 * pointer, and ItemPointerGetBlockNumber asserts the pointer is valid, so an
 * assert-enabled backend dies on the sort:
 *
 *     TRAP: failed Assert("ItemPointerIsValid(pointer)"), itemptr.h:105
 *
 * On a non-assert build the same invalid pointers are read as garbage and the
 * sample is sorted into an arbitrary order, which is the silent form of it: the
 * correlation statistic is then computed over rows in no particular order and
 * comes out as noise.
 *
 * Carrying tts_tid into t_self fixes both, and costs nothing on the scan path
 * because copy_heap_tuple is not on it -- a scan stores values into the slot and
 * the executor reads them from there.
 *
 * The slot is no longer "virtual" by TTS_IS_VIRTUAL, which is a pointer identity
 * test against TTSOpsVirtual. That is safe here: nothing in core requires it,
 * the paths that check it fall back to the general case, and the one that would
 * error -- tts_virtual_getsomeattrs -- is unreachable because
 * ExecStoreVirtualTuple sets tts_nvalid to the full attribute count, so
 * slot_getsomeattrs never calls it. The full suite is run on an assert-enabled
 * build to keep that reasoning honest.
 */
static TupleTableSlotOps PgColumnarSlotOps;

/*
 * A slot that can defer its decode (issue #157).
 *
 * An index fetch used to reconstruct every column of the row before returning,
 * because a virtual slot holds values and nothing else. On a wide table that is
 * ruinous: the decoded row group exceeds the fetch cache's size cap, the entry is
 * dropped after every fetch, and each row re-reads and re-decodes the whole
 * group. Measured at 41 columns, 2,000 index fetches reading one column took
 * about seventeen minutes.
 *
 * The executor never tells the access method which columns it will read, so there
 * is no projection to pass down. But it does ask, through slot_getsomeattrs, and
 * it asks for the smallest prefix it needs. So the slot carries the row's address
 * instead of its values, and decodes when asked.
 *
 * Only the index fetch defers. Everything else stores values eagerly with
 * ExecStoreVirtualTuple, which sets tts_nvalid to the full count, so getsomeattrs
 * is never reached for those and their behaviour is unchanged.
 */
typedef struct PgColumnarSlot
{
	/*
	 * VirtualTupleTableSlot, not TupleTableSlot, and it must come first. Every
	 * callback inherited from TTSOpsVirtual casts the slot to its own type and
	 * uses the `data` pointer that follows the base: tts_virtual_materialize
	 * writes it and tts_virtual_clear pfrees it. Deriving from TupleTableSlot
	 * puts our own first field exactly where `data` belongs, so materialising a
	 * slot would scribble on it and clearing one would free it.
	 */
	VirtualTupleTableSlot vslot;

	/* set when the slot holds a row's address rather than its values */
	bool		deferred;
	Relation	rel;
	Snapshot	snapshot;
	uint64		rowNumber;
} PgColumnarSlot;

/*
 * The fields above must sit past everything the inherited callbacks touch. If
 * VirtualTupleTableSlot ever grows, this fails to compile rather than silently
 * aliasing.
 */
StaticAssertDecl(offsetof(PgColumnarSlot, deferred) >= sizeof(VirtualTupleTableSlot),
				 "PgColumnarSlot fields must not overlap VirtualTupleTableSlot");

/*
 * pgcolumnar_slot_decode_upto
 *		Materialise attributes 0 .. natts-1 of a deferred slot.
 *
 *		Decodes a prefix because that is what slot_getsomeattrs asks for, and the
 *		executor asks for the largest attribute number it needs. A query reading
 *		column 2 of 41 decodes two columns, not forty-one.
 */
static void
pgcolumnar_slot_decode_upto(TupleTableSlot *slot, int natts)
{
	PgColumnarSlot *cslot = (PgColumnarSlot *) slot;
	Bitmapset  *needed = NULL;
	MemoryContext scratch;
	MemoryContext oldContext;
	int			i;

	Assert(cslot->deferred);

	/*
	 * Build the needed-column set in a private context, not the caller's. This
	 * runs from a slot materialization during tuplesort_puttupleslot, whose
	 * current context on PG17 is a bump context that forbids pfree (#720); a
	 * Bitmapset built and freed there would trip "pfree is not supported by the
	 * bump memory allocator". The reader below still writes its decoded values
	 * into the caller's context (palloc is fine there); only the transient set
	 * lives here, and deleting the context frees it without a pfree upstream.
	 */
	scratch = AllocSetContextCreate(CurrentMemoryContext, "columnar decode-upto",
									ALLOCSET_SMALL_SIZES);
	oldContext = MemoryContextSwitchTo(scratch);
	for (i = 0; i < natts; i++)
		needed = bms_add_member(needed, i);
	MemoryContextSwitchTo(oldContext);

	/*
	 * Visibility was settled when the slot was filled, so this cannot fail for a
	 * row that was live then. It can still miss a row held only in an unflushed
	 * write buffer, which is where the buffered reader comes in; that path
	 * reconstructs the whole row, which is correct if not lazy, and it is bounded
	 * by what one transaction has buffered.
	 */
	if (!PgColumnarReadRowByNumberCols(cslot->rel, cslot->snapshot,
									 cslot->rowNumber, slot->tts_values,
									 slot->tts_isnull, needed))
		(void) PgColumnarBufferedRowByNumber(cslot->rel, cslot->rowNumber,
										   slot->tts_values, slot->tts_isnull);

	MemoryContextDelete(scratch);

	/*
	 * Attributes past the prefix hold nothing meaningful yet. tts_nvalid is what
	 * tells the executor how far it may read, so leaving them is correct, but a
	 * later call asking for more has to decode again from the start rather than
	 * assume the earlier ones are still there -- which they are, so it does not.
	 */
	slot->tts_nvalid = natts;
}

static void
pgcolumnar_slot_getsomeattrs(TupleTableSlot *slot, int natts)
{
	PgColumnarSlot *cslot = (PgColumnarSlot *) slot;

	if (!cslot->deferred)
	{
		/*
		 * A slot filled eagerly has tts_nvalid at the full count already, so
		 * nothing should reach here. Erroring matches the virtual slot this
		 * otherwise behaves as, rather than silently returning junk.
		 */
		elog(ERROR, "getsomeattrs on a columnar slot that was filled eagerly");
	}

	pgcolumnar_slot_decode_upto(slot, natts);
}

/*
 * Anything that wants the row whole -- materialising, copying, forming a tuple
 * -- has to finish the decode first.
 */
static void
pgcolumnar_slot_force_full(TupleTableSlot *slot)
{
	PgColumnarSlot *cslot;

	/*
	 * Callers hand us slots that are not ours. copyslot in particular takes a
	 * source of any type -- the executor copies an ordinary virtual slot into a
	 * columnar one on every INSERT -- and casting that to PgColumnarSlot reads
	 * past the end of it, so the deferred flag is whatever happened to be in
	 * the next word and the relation pointer behind it is garbage. That is a
	 * segfault on the plainest INSERT there is, which is how it was found.
	 */
	if (slot->tts_ops != &PgColumnarSlotOps)
		return;

	cslot = (PgColumnarSlot *) slot;
	if (cslot->deferred && slot->tts_nvalid < slot->tts_tupleDescriptor->natts)
		pgcolumnar_slot_decode_upto(slot, slot->tts_tupleDescriptor->natts);
}

/*
 * The added fields have to start out cleared: a slot that is never used for a
 * deferred fetch still has them read, and MakeTupleTableSlot does not know they
 * are there.
 */
static void
pgcolumnar_slot_init(TupleTableSlot *slot)
{
	PgColumnarSlot *cslot = (PgColumnarSlot *) slot;

	TTSOpsVirtual.init(slot);
	cslot->deferred = false;
	cslot->rel = NULL;
	cslot->snapshot = NULL;
	cslot->rowNumber = 0;
}

static void
pgcolumnar_slot_clear(TupleTableSlot *slot)
{
	PgColumnarSlot *cslot = (PgColumnarSlot *) slot;

	Assert(slot->tts_ops == &PgColumnarSlotOps);
	cslot->deferred = false;
	cslot->rel = NULL;
	cslot->snapshot = NULL;
	cslot->rowNumber = 0;
	TTSOpsVirtual.clear(slot);
}

static void
pgcolumnar_slot_materialize(TupleTableSlot *slot)
{
	pgcolumnar_slot_force_full(slot);
	TTSOpsVirtual.materialize(slot);
}

static void
pgcolumnar_slot_copyslot(TupleTableSlot *dstslot, TupleTableSlot *srcslot)
{
	pgcolumnar_slot_force_full(srcslot);
	TTSOpsVirtual.copyslot(dstslot, srcslot);
}

static MinimalTuple
pgcolumnar_slot_copy_minimal_tuple(COLUMNAR_COPY_MINIMAL_TUPLE_ARGS)
{
	pgcolumnar_slot_force_full(slot);
	return TTSOpsVirtual.copy_minimal_tuple
		COLUMNAR_COPY_MINIMAL_TUPLE_FWD(slot);
}

/*
 * PgColumnarSlotStoreDeferred
 *		Point the slot at a row without decoding it. The caller has already
 *		established that the row is visible.
 */
static void
PgColumnarSlotStoreDeferred(TupleTableSlot *slot, Relation rel,
						  Snapshot snapshot, uint64 rowNumber)
{
	PgColumnarSlot *cslot = (PgColumnarSlot *) slot;

	ExecClearTuple(slot);
	cslot->deferred = true;
	cslot->rel = rel;
	cslot->snapshot = snapshot;
	cslot->rowNumber = rowNumber;

	slot->tts_flags &= ~TTS_FLAG_EMPTY;
	slot->tts_nvalid = 0;
}

static HeapTuple
pgcolumnar_slot_copy_heap_tuple(TupleTableSlot *slot)
{
	HeapTuple	tuple;

	Assert(!TTS_EMPTY(slot));

	pgcolumnar_slot_force_full(slot);

	tuple = heap_form_tuple(slot->tts_tupleDescriptor,
							slot->tts_values, slot->tts_isnull);
	tuple->t_self = slot->tts_tid;
	tuple->t_tableOid = slot->tts_tableOid;

	return tuple;
}

static const TupleTableSlotOps *
pgcolumnar_slot_callbacks(Relation relation)
{
	return &PgColumnarSlotOps;
}

static TableScanDesc
pgcolumnar_scan_begin(Relation rel, Snapshot snapshot, int nkeys,
					ScanKey key, ParallelTableScanDesc pscan, uint32 flags)
{
	PgColumnarScanDesc scan;

	RelationIncrementReferenceCount(rel);

	/*
	 * Persist any data and delete marks written earlier in this transaction so
	 * they reach the catalog before this scan reads it. The reader consults the
	 * catalog with a command-id-advanced snapshot (PgColumnarCatalogSnapshot), so
	 * these become visible to this same scan: same-transaction read-your-writes
	 * (spec 9).
	 */
	PgColumnarFlushWriteStateForRelation(RelationGetRelid(rel));
	PgColumnarFlushDeleteVectorForRelation(rel);

	scan = (PgColumnarScanDesc) palloc0(sizeof(PgColumnarScanDescData));
	scan->rs_base.rs_rd = rel;
	scan->rs_base.rs_snapshot = snapshot;
	scan->rs_base.rs_nkeys = nkeys;
	scan->rs_base.rs_flags = flags;
	scan->rs_base.rs_parallel = pscan;

	/*
	 * The read state is built on the first getnextslot, not here, because the
	 * shape to decode against arrives with the slot and is not always the
	 * relation's current one.
	 *
	 * ALTER TABLE ... ALTER COLUMN TYPE is where they differ (#178). Phase 2 of
	 * ATRewriteTable has already updated pg_attribute when phase 3 scans the old
	 * relation, so RelationGetDescr(rel) describes the new types while the bytes
	 * on disk are still the old ones. Core hands the scan a slot built from
	 * tab->oldDesc for exactly that reason; decoding against the relation
	 * instead read 4-byte values as 8-byte ones and worse.
	 *
	 * For every other scan the slot's descriptor is the relation's, so this
	 * costs a branch and changes nothing.
	 *
	 * Phase 2 projects all columns for a plain sequential scan (there is no
	 * per-scan projection channel in the table AM without the custom scan of
	 * a later phase), so we pass a NULL projection set. Any ScanKeys the
	 * executor supplies are forwarded for chunk-group skipping.
	 */
	scan->rs_base.rs_key = key;
	scan->readState = NULL;
	scan->scanContext = CurrentMemoryContext;

	return (TableScanDesc) scan;
}

/*
 * pgcolumnar_scan_read_state
 *		The scan's reader, built on first use against the descriptor the caller
 *		is asking for.
 *
 * Deferring it is what lets a rewrite decode against tab->oldDesc (#178). It
 * also means no caller may assume scan->readState is already set: the parallel
 * index build reads it straight off the scan without ever going through
 * getnextslot, and dereferenced NULL the first time this was written that way.
 * Everything that wants the reader comes through here.
 *
 * The read state is allocated in the context the scan descriptor itself lives
 * in. The current context on first use is usually a per-tuple one that is reset
 * before the scan ends, and PgColumnarEndRead then frees an already-freed pointer.
 */
static PgColumnarReadState *
pgcolumnar_scan_read_state(PgColumnarScanDesc scan, TupleDesc tupdesc)
{
	if (scan->readState == NULL)
	{
		MemoryContext oldContext = MemoryContextSwitchTo(scan->scanContext);

		scan->readState =
			PgColumnarBeginReadWithStorage(scan->rs_base.rs_rd,
										 scan->rs_base.rs_snapshot,
										 PgColumnarStorageId(scan->rs_base.rs_rd),
										 tupdesc,
										 scan->rs_base.rs_parallel, NULL,
										 scan->rs_base.rs_nkeys,
										 scan->rs_base.rs_key);
		MemoryContextSwitchTo(oldContext);
	}

	return scan->readState;
}

static void
pgcolumnar_scan_end(TableScanDesc sscan)
{
	PgColumnarScanDesc scan = (PgColumnarScanDesc) sscan;

	if (scan->readState != NULL)
		PgColumnarEndRead(scan->readState);

	if (scan->analyzeState != NULL)
	{
		if (scan->analyzeState->rs != NULL)
			PgColumnarEndRead(scan->analyzeState->rs);
		MemoryContextDelete(scan->analyzeState->cx);
	}

	/* release a snapshot restored+registered for a parallel worker */
	if (scan->rs_base.rs_flags & SO_TEMP_SNAPSHOT)
		UnregisterSnapshot(scan->rs_base.rs_snapshot);

	RelationDecrementReferenceCount(scan->rs_base.rs_rd);
	pfree(scan);
}

static void
pgcolumnar_scan_rescan(TableScanDesc sscan, ScanKey key, bool set_params,
					 bool allow_strat, bool allow_sync, bool allow_pagemode)
{
	PgColumnarScanDesc scan = (PgColumnarScanDesc) sscan;

	if (scan->readState != NULL)
		PgColumnarRescanRead(scan->readState);
}

static bool
pgcolumnar_scan_getnextslot(TableScanDesc sscan, ScanDirection direction,
						  TupleTableSlot *slot)
{
	PgColumnarScanDesc scan = (PgColumnarScanDesc) sscan;
	uint64		rowNumber;

	ExecClearTuple(slot);

	/*
	 * Honour the direction, or refuse it. This used to ignore the argument
	 * entirely and advance forward whatever was asked, so a scrollable cursor
	 * that reached the AM rather than the custom scan answered FETCH BACKWARD
	 * with more forward rows -- 6 7 8 where the heap gave 4 3 2 -- and FETCH
	 * LAST with nothing at all.
	 *
	 * NoMovement is a real request for no row and is answered as such.
	 *
	 * Backward is refused rather than emulated. The reader walks row groups and
	 * decodes vectors forwards, so scanning the other way is a feature rather
	 * than a correction, and inventing an answer is the one thing that must not
	 * happen here. Refusing is visible, and at shipped defaults the custom scan
	 * serves these cursors, so the refusal is reached only when that path has
	 * been turned off.
	 */
	if (unlikely(direction != ForwardScanDirection))
	{
		if (direction == NoMovementScanDirection)
			return false;

		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("columnar tables do not support backward scans"),
				 errdetail("A scrollable cursor over relation \"%s\" asked for a "
						   "backward fetch, which native storage cannot serve.",
						   RelationGetRelationName(scan->rs_base.rs_rd)),
				 errhint("Leave pgcolumnar.enable_custom_scan on, or add an "
						 "ORDER BY so the cursor is materialized.")));
	}

	if (!PgColumnarReadNextRow(pgcolumnar_scan_read_state(scan,
													 slot->tts_tupleDescriptor),
							 slot->tts_values, slot->tts_isnull, &rowNumber))
		return false;

	ExecStoreVirtualTuple(slot);
	PgColumnarRowNumberToItemPointer(rowNumber, &slot->tts_tid);
	slot->tts_tableOid = RelationGetRelid(scan->rs_base.rs_rd);

	return true;
}

/* -------------------------------------------------------------------------
 * parallel scan: single-worker claim (see pgcolumnar_reader.c)
 * ------------------------------------------------------------------------- */

static Size
pgcolumnar_parallelscan_estimate(Relation rel)
{
	return sizeof(ParallelBlockTableScanDescData);
}

static Size
pgcolumnar_parallelscan_initialize(Relation rel, ParallelTableScanDesc pscan)
{
	ParallelBlockTableScanDesc bpscan = (ParallelBlockTableScanDesc) pscan;

	memset(bpscan, 0, sizeof(ParallelBlockTableScanDescData));
	COLUMNAR_PARALLELSCAN_SET_REL(bpscan, rel);
	bpscan->phs_nblocks = 0;
	SpinLockInit(&bpscan->phs_mutex);
	bpscan->phs_startblock = InvalidBlockNumber;
	pg_atomic_init_u64(&bpscan->phs_nallocated, 0);

	return sizeof(ParallelBlockTableScanDescData);
}

static void
pgcolumnar_parallelscan_reinitialize(Relation rel, ParallelTableScanDesc pscan)
{
	ParallelBlockTableScanDesc bpscan = (ParallelBlockTableScanDesc) pscan;

	pg_atomic_write_u64(&bpscan->phs_nallocated, 0);
}

/* -------------------------------------------------------------------------
 * insert callbacks
 * ------------------------------------------------------------------------- */

static void
pgcolumnar_tuple_insert(Relation rel, TupleTableSlot *slot, CommandId cid,
					  COLUMNAR_TABLE_OPTIONS options,
					  struct BulkInsertStateData *bistate)
{
	PgColumnarWriteState *writeState = PgColumnarGetWriteState(rel);
	uint64		rowNumber;

	slot_getallattrs(slot);

	/*
	 * Serialize concurrent inserters of the same unique key (issue #5) before
	 * the executor runs its btree uniqueness check on this row, so the check
	 * runs only after any conflicting transaction has committed and flushed.
	 */
	PgColumnarLockUniqueKeys(rel, slot);

	rowNumber = PgColumnarWriteRow(writeState, rel, slot->tts_values,
								 slot->tts_isnull);

	/* fan the row out to every additional projection of this table (gap 26) */
	PgColumnarProjectionFanoutRow(rel, writeState, rowNumber, slot->tts_values,
								slot->tts_isnull);

	/*
	 * A new row makes its block not all-visible; clear any VM bit so an
	 * index-only scan never skips the fetch for a block that just changed
	 * (gap 28). A no-op unless a prior vacuum had marked the block visible.
	 */
	PgColumnarVMClearForRow(rel, rowNumber);

	/*
	 * Publish the row's synthetic item pointer (spec 6) so the executor can
	 * insert correct (index value, TID) entries into any indexes on this
	 * relation and enforce unique constraints (spec 9).
	 */
	PgColumnarRowNumberToItemPointer(rowNumber, &slot->tts_tid);
	slot->tts_tableOid = RelationGetRelid(rel);
}

static void
pgcolumnar_multi_insert(Relation rel, TupleTableSlot **slots, int nslots,
					  CommandId cid, COLUMNAR_TABLE_OPTIONS options,
					  struct BulkInsertStateData *bistate)
{
	PgColumnarWriteState *writeState = PgColumnarGetWriteState(rel);
	int			i;

	for (i = 0; i < nslots; i++)
	{
		uint64		rowNumber;

		slot_getallattrs(slots[i]);
		PgColumnarLockUniqueKeys(rel, slots[i]);	/* issue #5 */
		rowNumber = PgColumnarWriteRow(writeState, rel, slots[i]->tts_values,
									 slots[i]->tts_isnull);
		PgColumnarProjectionFanoutRow(rel, writeState, rowNumber,
									slots[i]->tts_values, slots[i]->tts_isnull);
		PgColumnarVMClearForRow(rel, rowNumber);	/* gap 28: block changed */
		PgColumnarRowNumberToItemPointer(rowNumber, &slots[i]->tts_tid);
		slots[i]->tts_tableOid = RelationGetRelid(rel);
	}
}

static void
pgcolumnar_finish_bulk_insert(Relation rel, COLUMNAR_TABLE_OPTIONS options)
{
	/*
	 * End of a bulk-load path (COPY, CREATE TABLE AS, ALTER TABLE rewrite).
	 * Flush now, under this operation's subtransaction, so the buffer never
	 * spans a later statement or savepoint boundary (spec 9).
	 */
	PgColumnarFlushWriteStateForRelation(RelationGetRelid(rel));
	PgColumnarFlushDeleteVectorForRelation(rel);
}

/* -------------------------------------------------------------------------
 * DDL callbacks
 * ------------------------------------------------------------------------- */

/*
 * pgcolumnar_delete_storage_tree
 *		Drop catalog rows for one storage id and any projections hanging off it.
 *		Options and projection declarations are keyed by relation OID and are
 *		left to the caller: they still apply after a rewrite, and they must go
 *		on DROP.
 */
static void
pgcolumnar_delete_storage_tree(uint64 storageId)
{
	List	   *projs = PgColumnarListProjections(storageId);
	ListCell   *lc;

	foreach(lc, projs)
	{
		PgColumnarProjection *p = (PgColumnarProjection *) lfirst(lc);

		if (p->projStorageId != storageId)
			PgColumnarDeleteMetadata(p->projStorageId);
		PgColumnarDeleteProjectionRow(storageId, p->projectionId);
	}

	PgColumnarDeleteMetadata(storageId);
}

static void
pgcolumnar_relation_set_new_filelocator(Relation rel,
									  const RelFileLocator *newrlocator,
									  char persistence,
									  TransactionId *freezeXid,
									  MultiXactId *minmulti)
{
	SMgrRelation srel;
	SMgrRelation oldsrel;
	uint64		storageId;

	*freezeXid = InvalidTransactionId;
	*minmulti = InvalidMultiXactId;

	if (persistence == RELPERSISTENCE_UNLOGGED)
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("unlogged columnar tables are not supported")));

	/*
	 * CREATE TABLE calls this with no existing main fork. TRUNCATE and other
	 * rewrites call it while the old fork is still attached, so the metapage
	 * still names the storage id whose catalog rows would otherwise remain
	 * after the new file is installed. DROP only deletes the current id, so
	 * a TRUNCATE-then-DROP left every previous storage behind.
	 */
	oldsrel = RelationGetSmgr(rel);
	if (smgrexists(oldsrel, MAIN_FORKNUM) &&
		smgrnblocks(oldsrel, MAIN_FORKNUM) >= 2) /* metapage + reserved */
	{
		pgcolumnar_delete_storage_tree(PgColumnarStorageId(rel));

		/*
		 * And drop the cached write state, which still names the storage id
		 * whose rows were just deleted. Without this, a transaction that
		 * writes, truncates and writes again COMMITS into storage nothing
		 * reads: the second insert reuses the stale state, flushes into the
		 * retired storage id, and the relation then reads the new one and
		 * finds it empty.
		 *
		 * Before the delete above existed, the retired storage's catalog rows
		 * survived and the stale flush collided with them on the primary key,
		 * so the transaction ERRORed and rolled back. That collision was the
		 * only thing making this safe, and deleting the rows removed it. A
		 * loud failure became silent loss of committed data, which is why this
		 * call belongs in the same branch rather than anywhere else.
		 *
		 * Forget rather than flush: the rows this state buffers are exactly the
		 * rows the rewrite is discarding.
		 */
		PgColumnarForgetWriteStateForRelation(RelationGetRelid(rel));
	}

	srel = PgColumnarRelationCreateStorage(*newrlocator, persistence);
	storageId = PgColumnarNextStorageId();
	PgColumnarWriteNewMetapage(newrlocator, srel, persistence, storageId);
}

static void
pgcolumnar_relation_nontransactional_truncate(Relation rel)
{
	uint64		storageId = PgColumnarStorageId(rel);

	PgColumnarDeleteMetadata(storageId);

	/*
	 * The same stale-write-state hazard as the rewrite path above, reached the
	 * other way: ExecuteTruncateGuts calls heap_truncate_one_rel, and so this
	 * callback, when the relation got its filelocator in the current
	 * subtransaction. The metapage keeps its storage id here, but the buffered
	 * rows are still the ones being truncated away.
	 */
	PgColumnarForgetWriteStateForRelation(RelationGetRelid(rel));

	RelationTruncate(rel, 2);
	PgColumnarResetMetapage(rel);
}

/* -------------------------------------------------------------------------
 * miscellaneous callbacks
 * ------------------------------------------------------------------------- */

static uint64
pgcolumnar_relation_size(Relation rel, ForkNumber forkNumber)
{
	SMgrRelation srel = RelationGetSmgr(rel);

	/*
	 * Compute size from smgr directly. We must not call
	 * RelationGetNumberOfBlocksInFork here: for a table AM it dispatches back
	 * into this very callback.
	 */
	if (forkNumber != MAIN_FORKNUM && !smgrexists(srel, forkNumber))
		return 0;

	return (uint64) smgrnblocks(srel, forkNumber) * BLCKSZ;
}

static bool
pgcolumnar_relation_needs_toast_table(Relation rel)
{
	/* the writer detoasts and stores values inline in the value stream */
	return false;
}

static void
pgcolumnar_relation_estimate_size(Relation rel, int32 *attr_widths,
								BlockNumber *pages, double *tuples,
								double *allvisfrac)
{
	BlockNumber nblocks = RelationGetNumberOfBlocks(rel);
	uint64		storageId = PgColumnarStorageId(rel);
	Snapshot	snapshot;
	List	   *rowGroupList;
	ListCell   *lc;
	double		liveRows = 0;

	/*
	 * Estimate the row count from row-group metadata, not from the metapage
	 * reservation high-water mark: row numbers are reserved a whole row group at
	 * a time, so the reservation overcounts. An accurate estimate keeps the
	 * planner from mis-costing scans (spec 6, 9).
	 */
	snapshot = ActiveSnapshotSet() ? GetActiveSnapshot() : GetTransactionSnapshot();
	snapshot = PgColumnarCatalogSnapshot(snapshot);
	rowGroupList = PgColumnarReadRowGroupList(storageId, snapshot);

	/*
	 * row_group.row_count is the physical occupancy, including rows later
	 * marked in delete_vector. The planner uses this callback instead of
	 * pg_class.reltuples, so leaving those rows in *tuples prices every scan
	 * as if DELETE had not happened.
	 */
	{
		uint64		physicalRows = 0;
		uint64		deleted;

		foreach(lc, rowGroupList)
		{
			NativeRowGroupMetadata *rg = (NativeRowGroupMetadata *) lfirst(lc);

			physicalRows += rg->rowCount;
		}

		/*
		 * One catalog scan summing delete_vector.deleted_count, not one scan
		 * and a bitmap walk per row group. This runs on every plan of a
		 * columnar relation, so a per-group fold is paid per plan.
		 *
		 * Summing is exact, not an approximation: the unique index on
		 * (storage_id, group_number) means one delete_vector row per group, so
		 * no two summands can count the same row. An earlier comment here
		 * justified the per-group fold by saying a group can have several
		 * delete_vector rows whose bitmaps overlap. The catalog forbids that,
		 * and the expensive path was defended by a premise the schema rules
		 * out.
		 *
		 * The clamp is against the storage total rather than per group, which
		 * is what the per-group clamp was doing in aggregate. It matters only
		 * if the count is ever inconsistent with the row groups, and an
		 * estimate must not go negative.
		 *
		 * Both reads take the same catalog snapshot as the row-group list
		 * above, deliberately, so the count cannot straddle two snapshots and
		 * report more deletes than there are rows.
		 */
		deleted = PgColumnarStorageDeletedCount(storageId, snapshot);
		if (deleted > physicalRows)
			deleted = physicalRows;
		liveRows = (double) (physicalRows - deleted);
	}

	*pages = Max(nblocks, 1);
	*tuples = Max(liveRows, 0);

	/*
	 * The all-visible fraction, from the catalog rather than hardcoded (#507).
	 *
	 * This callback is the ONLY place the planner learns it: core's
	 * estimate_rel_size delegates to the AM, so a hardcoded zero here overrides
	 * pg_class entirely and no amount of VACUUM recording the truth can reach
	 * cost_index. That is worth stating plainly because it is not visible from
	 * either end -- the catalog looks right and the plan stays wrong, and a test
	 * that asserts relallvisible is green while the defect is untouched.
	 *
	 * Zero is not a safe default here either. cost_index scales an index-only
	 * scan's heap-fetch estimate by (1 - allvisfrac), so zero prices every
	 * index-only scan as though it fetched every row -- on a 500,000-row fixture
	 * that is the difference between an index-only scan priced 5,505 and the same
	 * scan priced as high as 21,284, and it decides the plan.
	 *
	 * Same derivation core uses for a heap: the recorded all-visible pages over
	 * the pages we are reporting, clamped, since the two are read at different
	 * moments and a relation that shrank between them could otherwise exceed one.
	 *
	 * The two ends deliberately use different page counts, and it is not an
	 * oversight: vacuum scales the numerator by the CATALOG relpages when it
	 * records it, and this divides by the LIVE block count at plan time. Heap has
	 * the same property -- vac_update_relstats writes relallvisible against the
	 * pages it counted, and estimate_rel_size divides by curpages -- and both
	 * directions are safe. A relation that grew since its vacuum divides an old
	 * numerator by a larger denominator, so the fraction decays exactly where the
	 * new pages are the ones not yet all-visible; one that shrank is caught by
	 * the clamp. test/analyze_stats.sh pins the growth direction.
	 */
	if (nblocks > 0 && rel->rd_rel->relallvisible > 0)
	{
		double		frac = (double) rel->rd_rel->relallvisible / (double) nblocks;

		*allvisfrac = (frac > 1.0) ? 1.0 : frac;
	}
	else
		*allvisfrac = 0.0;
}

/*
 * pgcolumnar_analyze_state
 *		The sampling state for this scan, built on first use. The row group list
 *		is read once: ANALYZE holds ShareUpdateExclusiveLock, so no group is added
 *		or retired under us, and re-reading it per block would put a catalog scan
 *		in the middle of the sample loop.
 */
static PgColumnarAnalyzeState *
pgcolumnar_analyze_state(PgColumnarScanDesc scan)
{
	PgColumnarAnalyzeState *st = scan->analyzeState;
	Relation	rel = scan->rs_base.rs_rd;
	MemoryContext oldContext;

	if (st != NULL)
		return st;

	st = palloc0(sizeof(PgColumnarAnalyzeState));
	st->cx = AllocSetContextCreate(CurrentMemoryContext, "columnar analyze",
								   ALLOCSET_DEFAULT_SIZES);
	oldContext = MemoryContextSwitchTo(st->cx);
	st->metaSnapshot = PgColumnarCatalogSnapshot(scan->rs_base.rs_snapshot);
	st->rowGroups = PgColumnarReadRowGroupList(PgColumnarStorageId(rel),
											 st->metaSnapshot);
	st->values = palloc(sizeof(Datum) * RelationGetDescr(rel)->natts);
	st->nulls = palloc(sizeof(bool) * RelationGetDescr(rel)->natts);
	st->pendingValues = palloc(sizeof(Datum) * RelationGetDescr(rel)->natts);
	st->pendingNulls = palloc(sizeof(bool) * RelationGetDescr(rel)->natts);
	MemoryContextSwitchTo(oldContext);

	scan->analyzeState = st;
	return st;
}

/*
 * pgcolumnar_analyze_set_slice
 *		Point the sampler at the rows a physical block stands for.
 *
 *		The block's logical byte offset locates the row group it falls in; its
 *		position within that group's byte range gives the slice ordinal, and the
 *		group's rows are cut into as many equal slices as it spans blocks. A block
 *		that lands outside every group -- the metapage, or space reserved but not
 *		yet written -- yields an empty slice, which is the columnar equivalent of
 *		sampling an empty heap page and is reported the same way: the block counts
 *		as visited and offers nothing.
 */
static void
pgcolumnar_analyze_set_slice(PgColumnarAnalyzeState *st, BlockNumber blockno)
{
	uint64		logicalOffset;
	ListCell   *lc;

	st->sliceFirstRow = 0;
	st->sliceRows = 0;
	st->sliceNext = 0;

	/* blocks 0 and 1 are the metapage and its reserve; no logical data there */
	if ((uint64) blockno * COLUMNAR_BYTES_PER_PAGE < COLUMNAR_FIRST_LOGICAL_OFFSET)
		return;

	/*
	 * A row group's file_offset is already an absolute logical offset -- the
	 * first one a table can have is COLUMNAR_FIRST_LOGICAL_OFFSET itself -- so
	 * the block's offset is compared to it directly. Subtracting the first
	 * offset here as well shifted every block two blocks low, and a table whose
	 * only group started at the very beginning matched no block at all:
	 * ANALYZE then scanned every page, was offered nothing, and recorded
	 * reltuples = 0 for a table with rows in it.
	 */
	logicalOffset = (uint64) blockno * COLUMNAR_BYTES_PER_PAGE;

	foreach(lc, st->rowGroups)
	{
		NativeRowGroupMetadata *rg = (NativeRowGroupMetadata *) lfirst(lc);
		uint64		span;
		uint64		nblocks;
		uint64		slice;
		uint64		firstOff;
		uint64		endOff;

		if (rg->rowCount == 0)
			continue;
		if (logicalOffset < rg->fileOffset)
			continue;

		/*
		 * A group's footprint is its data length rounded up to a page, so the
		 * blocks it owns are exactly those its rounded span covers. Using the
		 * unrounded length would leave the last block of every group unmatched
		 * and its slice of rows unreachable.
		 */
		span = COLUMNAR_PAGE_ROUND_UP(rg->byteLength);
		if (logicalOffset >= rg->fileOffset + span)
			continue;

		nblocks = span / COLUMNAR_BYTES_PER_PAGE;
		if (nblocks == 0)
			nblocks = 1;
		slice = (logicalOffset - rg->fileOffset) / COLUMNAR_BYTES_PER_PAGE;
		if (slice >= nblocks)
			slice = nblocks - 1;

		/*
		 * Cut the group's rows into nblocks slices. Computing both edges from the
		 * same expression makes the slices exactly partition the group however the
		 * division rounds, so every row belongs to one block and none to two.
		 */
		firstOff = (rg->rowCount * slice) / nblocks;
		endOff = (rg->rowCount * (slice + 1)) / nblocks;

		st->sliceFirstRow = rg->firstRowNumber + firstOff;
		st->sliceRows = endOff - firstOff;
		st->sliceNext = 0;
		st->sliceGroup = rg->groupNumber;
		return;
	}
}

/*
 * The block comes from a read stream from PG17 and as a plain BlockNumber
 * before that. pgcolumnar_compat.h supplies the parameter list and splits at the
 * same major; these two must agree, and when they did not, PG17 took the
 * pre-17 branch and failed to compile on a `blockno` its signature does not
 * have.
 */
#if PG_VERSION_NUM >= 170000
static bool
pgcolumnar_scan_analyze_next_block(COLUMNAR_ANALYZE_NEXT_BLOCK_ARGS)
{
	PgColumnarScanDesc cscan = (PgColumnarScanDesc) scan;
	PgColumnarAnalyzeState *st = pgcolumnar_analyze_state(cscan);
	Buffer		buf = read_stream_next_buffer(stream, NULL);

	if (!BufferIsValid(buf))
		return false;

	/*
	 * The stream chose the block; the buffer's contents are of no use here,
	 * because a columnar block holds encoded column bytes and the rows it stands
	 * for are read through the fetch path instead. Release the pin at once rather
	 * than holding it across the tuple loop as heap does.
	 */
	pgcolumnar_analyze_set_slice(st, BufferGetBlockNumber(buf));
	ReleaseBuffer(buf);
	return true;
}
#else
static bool
pgcolumnar_scan_analyze_next_block(COLUMNAR_ANALYZE_NEXT_BLOCK_ARGS)
{
	PgColumnarScanDesc cscan = (PgColumnarScanDesc) scan;

	pgcolumnar_analyze_set_slice(pgcolumnar_analyze_state(cscan), blockno);
	return true;
}
#endif

static bool
pgcolumnar_scan_analyze_next_tuple(COLUMNAR_ANALYZE_NEXT_TUPLE_ARGS)
{
	PgColumnarScanDesc cscan = (PgColumnarScanDesc) scan;
	PgColumnarAnalyzeState *st = pgcolumnar_analyze_state(cscan);
	Relation	rel = scan->rs_rd;
	TupleDesc	tupdesc = RelationGetDescr(rel);
	uint64		sliceEnd = st->sliceFirstRow + st->sliceRows;
	int			i;

	if (st->sliceRows == 0)
		return false;

	/* a slice in a new group needs a reader positioned on that group */
	if (st->rs == NULL || st->rsGroup != st->sliceGroup)
	{
		MemoryContext oldContext = MemoryContextSwitchTo(st->cx);

		if (st->rs != NULL)
			PgColumnarEndRead(st->rs);
		st->rs = PgColumnarBeginRead(rel, scan->rs_snapshot, NULL, NULL, 0, NULL);
		PgColumnarReadRestrictToGroups(st->rs, &st->sliceGroup, 1);
		st->rsGroup = st->sliceGroup;
		st->rsHavePending = false;
		MemoryContextSwitchTo(oldContext);
	}

	for (;;)
	{
		uint64		rowNumber;

		CHECK_FOR_INTERRUPTS();

		if (st->rsHavePending)
		{
			rowNumber = st->pendingRow;
			st->rsHavePending = false;
			memcpy(st->values, st->pendingValues,
				   sizeof(Datum) * tupdesc->natts);
			memcpy(st->nulls, st->pendingNulls, sizeof(bool) * tupdesc->natts);
		}
		else if (!PgColumnarReadNextRow(st->rs, st->values, st->nulls, &rowNumber))
		{
			/*
			 * The group is exhausted. Any rows of this slice not returned were
			 * removed by the delete vector, which the reader applies for us.
			 */
			*deadrows += (double) (sliceEnd - st->sliceFirstRow - st->sliceNext);
			st->sliceNext = st->sliceRows;
			return false;
		}

		/* rows before the slice belong to a block core has already visited */
		if (rowNumber < st->sliceFirstRow)
			continue;

		if (rowNumber >= sliceEnd)
		{
			/*
			 * Past the slice. Hold the row for the next one rather than losing
			 * it: the reader only goes forward, and re-reading the group per
			 * slice is what this design exists to avoid.
			 */
			memcpy(st->pendingValues, st->values, sizeof(Datum) * tupdesc->natts);
			memcpy(st->pendingNulls, st->nulls, sizeof(bool) * tupdesc->natts);
			st->pendingRow = rowNumber;
			st->rsHavePending = true;
			*deadrows += (double) (sliceEnd - st->sliceFirstRow - st->sliceNext);
			st->sliceNext = st->sliceRows;
			return false;
		}

		/*
		 * Rows the reader skipped between the last one and this are deleted; the
		 * live-row estimate core computes needs them counted, not merely left
		 * out of the sample.
		 */
		*deadrows += (double) (rowNumber - st->sliceFirstRow - st->sliceNext);
		st->sliceNext = rowNumber - st->sliceFirstRow + 1;

		ExecClearTuple(slot);
		for (i = 0; i < tupdesc->natts; i++)
		{
			slot->tts_values[i] = st->values[i];
			slot->tts_isnull[i] = st->nulls[i];
		}
		ExecStoreVirtualTuple(slot);

		/*
		 * Give the row its synthetic address. ANALYZE sorts the collected sample
		 * by item pointer before computing statistics, and the row-number mapping
		 * is monotonic, so this is what makes the sorted order the physical order
		 * -- which in turn is what makes the correlation statistic mean anything.
		 */
		PgColumnarRowNumberToItemPointer(rowNumber, &slot->tts_tid);

		*liverows += 1;
		return true;
	}
}

/* VACUUM: mark all-visible groups in the VM fork and retire fully-deleted
 * groups online, both under ShareUpdateExclusiveLock */
static void
pgcolumnar_relation_vacuum(Relation rel, COLUMNAR_VACUUM_PARAMS params,
						 BufferAccessStrategy bstrategy)
{
	/*
	 * Lazy vacuum (gap 28 phase 3): mark all-visible chunk groups in the VM
	 * fork so index-only scans can skip the columnar fetch. This only reads
	 * committed state and writes the VM fork -- no data rewrite -- so it runs
	 * fine under the ShareUpdateExclusiveLock a plain VACUUM/autovacuum holds,
	 * concurrent with readers and writers. The space-reclaiming rewrite stays in
	 * columnar.vacuum (AccessExclusiveLock, the VACUUM-FULL analog).
	 */
	uint64		visibleRows = PgColumnarVMSetVisibleForRelation(rel);

	/*
	 * Record the result where the planner can see it (#507).
	 *
	 * Setting the VM bits is only half the job. rel->allvisfrac is derived from
	 * pg_class.relallvisible, and core's cost_index prices an index-only scan by
	 * it, so a relation whose VM is populated and whose relallvisible is 0 is
	 * priced as though every row still needs a fetch. Measured on 20,000,000
	 * rows: relallvisible stayed 0 where a heap on identical rows reached 186,914
	 * of 186,916 pages, and the planner refused an index-only scan running
	 * 187.8 ms in favour of one running 361.4 ms.
	 *
	 * Converted through the row count rather than counted in blocks, because the
	 * VM is keyed by SYNTHETIC blocks (rowNumber / K) while relpages counts
	 * stored pages -- 68,730 against 45,994 on that table. Core computes the
	 * fraction as relallvisible / relpages, so the numerator has to be in
	 * relpages units or the fraction exceeds 1 by arithmetic alone.
	 *
	 * relpages and reltuples are passed back unchanged: ANALYZE owns them, and a
	 * VACUUM that also rewrote them would make the two commands fight over the
	 * same fields. InvalidTransactionId and InvalidMultiXactId are core's own
	 * idiom for "leave the horizons alone".
	 */
	if (visibleRows > 0 && rel->rd_rel->reltuples > 0 && rel->rd_rel->relpages > 0)
	{
		double		frac = (double) visibleRows / (double) rel->rd_rel->reltuples;
		BlockNumber allvis;

		/*
		 * When the clamp can bind, and why it is not hiding an overshoot.
		 *
		 * visibleRows counts rows in blocks lying ENTIRELY within an all-visible
		 * run, so it is bounded by the true all-visible row count, which is
		 * bounded by the true live row count. The denominator is not a count at
		 * all -- reltuples is ANALYZE's estimate -- so the only way frac exceeds
		 * one is that the estimate sits below the rows actually there. The clamp
		 * therefore fires when the two inputs disagree, never when both are
		 * accurate, and the value it produces in that case (every page
		 * all-visible) is the right answer for a relation whose rows are all
		 * visible. It cannot mask a miscounted numerator, because a numerator
		 * that could overshoot on its own would have to count rows that do not
		 * exist.
		 *
		 * test/native_ios.sh asserts the outcome either way: relallvisible must
		 * not exceed relpages, and must still be most of the relation.
		 */
		if (frac > 1.0)
			frac = 1.0;
		/* truncating cast, not floor(): both are non-negative here, and this
		 * needs no <math.h> in a file that otherwise wants none */
		allvis = (BlockNumber) ((double) rel->rd_rel->relpages * frac);

		COLUMNAR_VAC_UPDATE_RELSTATS(rel,
									 rel->rd_rel->relpages,
									 rel->rd_rel->reltuples,
									 allvis,
									 rel->rd_rel->relhasindex,
									 InvalidTransactionId,
									 InvalidMultiXactId,
									 NULL, NULL,
									 false);
	}

	/*
	 * Online compaction (Phase F3a): retire row groups that are fully deleted
	 * as-of the oldest-xmin horizon, dropping their metadata so scans skip them.
	 * This is also read-mostly on data (it only deletes catalog rows for groups
	 * every snapshot agrees are dead) and is safe under ShareUpdateExclusiveLock,
	 * so a plain VACUUM / autovacuum reclaims fully-deleted groups online without
	 * the AccessExclusiveLock rewrite.
	 */
	PgColumnarRetireFullyDeletedGroups(rel);
}

/* -------------------------------------------------------------------------
 * not-yet-supported callbacks (later phases)
 * ------------------------------------------------------------------------- */

#define COLUMNAR_UNSUPPORTED(feature) \
	ereport(ERROR, \
			(errcode(ERRCODE_FEATURE_NOT_SUPPORTED), \
			 errmsg("columnar: %s is not supported yet", \
					feature)))

/* our index-fetch descriptor is just the base plus nothing extra */
typedef struct PgColumnarIndexFetchData
{
	IndexFetchTableData xs_base;
} PgColumnarIndexFetchData;

static struct IndexFetchTableData *
pgcolumnar_index_fetch_begin(COLUMNAR_INDEX_FETCH_BEGIN_ARGS)
{
	PgColumnarIndexFetchData *scan = palloc0(sizeof(PgColumnarIndexFetchData));

	scan->xs_base.rel = rel;
	return &scan->xs_base;
}

static void
pgcolumnar_index_fetch_reset(struct IndexFetchTableData *scan)
{
}

static void
pgcolumnar_index_fetch_end(struct IndexFetchTableData *scan)
{
	pfree(scan);
}

/*
 * pgcolumnar_index_fetch_tuple
 *		Fetch the columnar row addressed by an index item pointer (spec 6) into
 *		the slot. Returns false when the row is marked deleted in the delete vector
 *		or does not exist, so an index scan never returns a deleted row and a
 *		unique check does not treat a deleted row as a live duplicate (spec 9).
 *
 *		The row is looked up first in the flushed stripes, then in any unflushed
 *		write buffer for the relation. The latter lets a unique constraint catch
 *		two duplicate rows inserted within a single statement, where the first
 *		row's item pointer is fetched while both rows are still buffered. This
 *		function acquires no relation extension or metapage locks, so it is safe
 *		to call while the caller holds an index buffer lock (the uniqueness
 *		check path).
 */
static bool
pgcolumnar_index_fetch_tuple(struct IndexFetchTableData *scan, ItemPointer tid,
						   Snapshot snapshot, TupleTableSlot *slot,
						   bool *call_again, bool *all_dead)
{
	Relation	rel = scan->rel;
	uint64		rowNumber = PgColumnarItemPointerToRowNumber(tid);

	/* columnar rows are 1:1 with item pointers: no chain, never dead here */
	*call_again = false;
	if (all_dead != NULL)
		*all_dead = false;

	ExecClearTuple(slot);

	/*
	 * Honour the dirty-snapshot contract. _bt_check_unique() hands us its own
	 * on-stack SnapshotDirty and reads xmin/xmax back out afterwards to pick the
	 * xact to wait on; InitDirtySnapshot() leaves those fields uninitialised, and
	 * HeapTupleSatisfiesDirty() -- which never runs on a columnar row -- is what
	 * would reset and then set them for a heap row. Reset them here exactly as it
	 * does at entry. The catalog read of the row's containing group below runs
	 * under this same snapshot, so a group still being inserted by another
	 * transaction sets xmin to that xact and the unique check waits on it; when
	 * nothing covers the row the fields stay Invalid and the check does not wait
	 * on stack garbage.
	 */
	if (snapshot != NULL && snapshot->snapshot_type == SNAPSHOT_DIRTY)
	{
		snapshot->xmin = snapshot->xmax = InvalidTransactionId;
		snapshot->speculativeToken = 0;
	}

	/*
	 * Settle visibility without decoding anything, then hand the slot the row's
	 * address rather than its values (issue #157). The columns are decoded when
	 * the executor asks for them, and it asks for the smallest prefix it needs.
	 *
	 * A row that is not in the flushed stripes may still be in this
	 * transaction's write buffer, and that reader reconstructs whole rows, so it
	 * is stored eagerly.
	 */
	if (PgColumnarRowIsLive(rel, snapshot, rowNumber))
		PgColumnarSlotStoreDeferred(slot, rel, snapshot, rowNumber);
	else if (PgColumnarBufferedRowByNumber(rel, rowNumber,
										 slot->tts_values, slot->tts_isnull))
		ExecStoreVirtualTuple(slot);
	else
		return false;

	PgColumnarRowNumberToItemPointer(rowNumber, &slot->tts_tid);
	slot->tts_tableOid = RelationGetRelid(rel);

	return true;
}

/*
 * pgcolumnar_tuple_fetch_row_version
 *		Fetch the row addressed by tid into slot (spec 6). Used by UPDATE, which
 *		re-fetches the old row by its item pointer. Returns false when the row
 *		does not exist or is marked deleted.
 */
static bool
pgcolumnar_tuple_fetch_row_version(Relation rel, ItemPointer tid,
								 Snapshot snapshot, TupleTableSlot *slot)
{
	uint64		rowNumber = PgColumnarItemPointerToRowNumber(tid);

	ExecClearTuple(slot);

	/*
	 * Fall back to this transaction's write buffer, as the index fetch does.
	 *
	 * A row is given its stripe reservation -- and so its row number and item
	 * pointer -- when it is buffered, not when the stripe is flushed. Between
	 * those two moments the row has an address that nothing on disk answers to,
	 * and a fetch by that address is not a fetch of a missing row.
	 *
	 * An AFTER INSERT ... FOR EACH ROW trigger lands exactly there (#179). The
	 * trigger machinery records the TID when it queues the event and re-fetches
	 * by it when the event fires, and after-row events fire in
	 * AfterTriggerEndQuery -- before ExecutorEnd, and so before
	 * finish_bulk_insert flushes the stripe. Every such trigger failed with
	 * "failed to fetch tuple1 for AFTER trigger", taking its own INSERT down
	 * with it.
	 *
	 * The buffered reader takes no locks and reads only process-local memory,
	 * so this costs a search of the pending stripe and never a flush: writing a
	 * partial stripe to satisfy a read would fragment storage for the sake of
	 * data already in hand.
	 */
	if (!PgColumnarReadRowByNumber(rel, snapshot, rowNumber,
								 slot->tts_values, slot->tts_isnull) &&
		!PgColumnarBufferedRowByNumber(rel, rowNumber,
									 slot->tts_values, slot->tts_isnull))
		return false;

	ExecStoreVirtualTuple(slot);
	PgColumnarRowNumberToItemPointer(rowNumber, &slot->tts_tid);
	slot->tts_tableOid = RelationGetRelid(rel);

	return true;
}

static bool
pgcolumnar_tuple_tid_valid(TableScanDesc scan, ItemPointer tid)
{
	return true;
}

static void
pgcolumnar_tuple_get_latest_tid(TableScanDesc scan, ItemPointer tid)
{
	COLUMNAR_UNSUPPORTED("get latest tid");
}

static bool
pgcolumnar_tuple_satisfies_snapshot(Relation rel, TupleTableSlot *slot,
								  Snapshot snapshot)
{
	/* stripes are visible per their metadata snapshot; slots are visible */
	return true;
}

/*
 * pgcolumnar_index_delete_tuples
 *		Opportunistic index tuple deletion. An index entry is deletable exactly
 *		when its row is no longer visible, i.e. PgColumnarReadRowByNumber cannot
 *		return it (deleted via the delete vector). Reporting deletability by actual
 *		liveness is required for correctness: nbtree's deletion pass (including
 *		bottom-up deletion of duplicate keys, which a same-key UPDATE produces)
 *		marks candidate items and calls this callback as the authority; leaving a
 *		genuinely dead item marked non-deletable would make nbtree assert
 *		(ndeletable > 0 || nupdatable > 0). Entries left in place are still
 *		filtered on fetch, so either way is correct. The snapshot conflict horizon
 *		is reported as invalid (no conflict), matching the delete vector's own MVCC on
 *		the catalog.
 */
#if PG_VERSION_NUM < 140000
/*
 * PG13 spelling of the same policy: the callback is
 * compute_xid_horizon_for_tuples, which reports the snapshot conflict horizon
 * for a batch of index tuples the caller would like to remove. We never remove
 * index entries opportunistically, so an invalid (no-conflict) horizon is the
 * correct and always-safe answer.
 */
static TransactionId
pgcolumnar_compute_xid_horizon_for_tuples(Relation rel, ItemPointerData *tids,
										int nitems)
{
	return InvalidTransactionId;
}
#else
static TransactionId
pgcolumnar_index_delete_tuples(Relation rel, TM_IndexDeleteOp *delstate)
{
	/*
	 * The question here is "is this entry dead to EVERYONE", not "is this row
	 * invisible to me". nbtree acts on the answer in _bt_delitems_delete, which
	 * removes the items from the leaf page physically, inside a critical
	 * section, WAL-logged. Nothing restores them, and an abort does not.
	 *
	 * heapam answers from a global visibility test (InitNonVacuumableSnapshot
	 * over GlobalVisTestFor). This used to answer from the calling backend's
	 * MVCC snapshot, which is a different question with a different answer: a
	 * row the current, still-uncommitted transaction has deleted reads as "not
	 * live" while remaining live to everyone else, and PgColumnarRowIsLive
	 * consults this backend's unflushed delete buffer as well. Entries for live
	 * rows were therefore erased. Measured on 400 live rows churned on a
	 * non-indexed column: an index scan reached 0 of them after a ROLLBACK, and
	 * 228 of 400 after a COMMIT, while `count(*)` at shipped defaults answered
	 * 228 through an index-only scan and `count(b)` answered 400 through the
	 * columnar scan, on the same table in the same session.
	 *
	 * Native storage carries no per-row xid to compare against a global horizon:
	 * the delete vector is a bitmap, and its visibility comes from the MVCC
	 * catalog rows that hold it. So this cannot prove global deadness, and it
	 * vouches for nothing instead.
	 *
	 * For a bottom-up caller that is free: the work is speculative by contract,
	 * and _bt_delitems_delete_check returns cleanly on an empty deltids array
	 * (its Assert there is Assert(delstate->bottomup)). The cost is that version
	 * churn no longer reclaims leaf space on a columnar table, so such an index
	 * grows where it used to be compacted. That is a space cost, not an answer.
	 *
	 * A simple-deletion caller has already marked its entries knowndeletable
	 * from LP_DEAD hints and is not asking us to discover anything, so its marks
	 * are left exactly as they arrived. Zeroing ndeltids there would be wrong
	 * twice over: it would discard the caller's own findings and trip that
	 * Assert.
	 */
	if (delstate->bottomup)
		delstate->ndeltids = 0;

	return InvalidTransactionId;
}
#endif

static void
pgcolumnar_tuple_insert_speculative(Relation rel, TupleTableSlot *slot,
								  CommandId cid, COLUMNAR_TABLE_OPTIONS options,
								  struct BulkInsertStateData *bistate,
								  uint32 specToken)
{
	COLUMNAR_UNSUPPORTED("speculative insert");
}

static void
pgcolumnar_tuple_complete_speculative(Relation rel, TupleTableSlot *slot,
									uint32 specToken, bool succeeded)
{
	COLUMNAR_UNSUPPORTED("speculative insert");
}

/*
 * pgcolumnar_tuple_delete
 *		Mark the row addressed by tid as deleted in the delete vector (spec 9). The
 *		stripe is not rewritten. The tid is the synthetic item pointer the scan
 *		produced, which maps back to the row number.
 */
static TM_Result
pgcolumnar_tuple_delete(COLUMNAR_TUPLE_DELETE_ARGS)
{
	uint64		rowNumber = PgColumnarItemPointerToRowNumber(tid);
	TM_Result	conflict;

	/* issue #5: serialize on the row identity before marking it deleted */
	if (PgColumnarRowWriteConflict(rel, tid, cid, wait, tmfd, &conflict))
		return conflict;

	PgColumnarMarkRowDeleted(rel, rowNumber);
	return TM_Ok;
}

/*
 * pgcolumnar_tuple_update
 *		Update is delete-plus-insert (spec 9): mark the old row deleted in the
 *		delete vector and append the new tuple as a fresh row with a new row number.
 *		The new row's item pointer is published on the slot and index
 *		maintenance is requested, so the new row gets fresh index entries. The
 *		old row's index entries remain but are filtered on fetch because the old
 *		row is now marked deleted (spec 6, 9).
 */
static TM_Result
pgcolumnar_tuple_update(COLUMNAR_TUPLE_UPDATE_ARGS)
{
	uint64		oldRowNumber = PgColumnarItemPointerToRowNumber(otid);
	PgColumnarWriteState *writeState;
	uint64		rowNumber;
	TM_Result	conflict;

	/*
	 * issue #5: serialize on the row identity before marking the old row
	 * deleted, so two transactions updating the same row do not each keep their
	 * own new version. The loser gets a retryable serialization_failure (or
	 * TM_SelfModified for a same-command self-join). lockmode is set for the
	 * non-Ok returns as core expects.
	 */
	if (PgColumnarRowWriteConflict(rel, otid, cid, wait, tmfd, &conflict))
	{
		*lockmode = LockTupleExclusive;
		return conflict;
	}

	PgColumnarMarkRowDeleted(rel, oldRowNumber);

	writeState = PgColumnarGetWriteState(rel);
	slot_getallattrs(slot);

	/* the new row version is a fresh insert: serialize its unique keys too */
	PgColumnarLockUniqueKeys(rel, slot);		/* issue #5 */

	rowNumber = PgColumnarWriteRow(writeState, rel, slot->tts_values,
								 slot->tts_isnull);

	/*
	 * The new version is a new row number. Projections store that number as
	 * their join key and skip deleted base numbers via the delete vector, so
	 * DELETE needs no fan-out. UPDATE does: without it the projection still
	 * holds the old number, the delete vector hides that number, and a covering
	 * projection scan (or read_projection) answers as if the row is gone.
	 */
	PgColumnarProjectionFanoutRow(rel, writeState, rowNumber, slot->tts_values,
								slot->tts_isnull);

	/*
	 * The new row version makes its block not all-visible, exactly as a plain
	 * insert does (gap 28). This half of update-as-delete-plus-insert was the
	 * only write path not clearing the bit, while PgColumnarVMClearForRow's own
	 * contract says it is "called by every write path (insert/delete/update)".
	 * PgColumnarMarkRowDeleted above covers the OLD row; this covers the new one.
	 *
	 * It is a no-op today, and that is the reason to write it rather than a
	 * reason to leave it out. PgColumnarVMSetVisibleForRelation marks only blocks
	 * lying ENTIRELY inside an all-visible run -- `bend = hi / K` with a
	 * `b < bend` loop -- so the partly-filled block at the append frontier is
	 * never marked, and an appended row can only land there or beyond. That
	 * safety is a `<` in another file with nothing pointing at it: marking the
	 * boundary block looks like a tightening, and whoever makes it would turn
	 * this no-op into a stale all-visible bit on a block that just changed, which
	 * an index-only scan reads as permission to skip the fetch.
	 *
	 * Cheap when no bit is set (a short-circuit read), and the insert path
	 * already pays it per row.
	 */
	PgColumnarVMClearForRow(rel, rowNumber);

	PgColumnarRowNumberToItemPointer(rowNumber, &slot->tts_tid);
	slot->tts_tableOid = RelationGetRelid(rel);

	*lockmode = LockTupleExclusive;
	*update_indexes = COLUMNAR_TU_ALL;
	return TM_Ok;
}

static TM_Result
pgcolumnar_tuple_lock(Relation rel, ItemPointer tid, Snapshot snapshot,
					TupleTableSlot *slot, CommandId cid, LockTupleMode mode,
					LockWaitPolicy wait_policy, uint8 flags,
					TM_FailureData *tmfd)
{
	/*
	 * A logical replication apply worker reaches this on the first streamed
	 * UPDATE or DELETE, and the generic message leaves it looking like a
	 * transient fault (#435).
	 *
	 * It is not transient and it is not bounded. Core takes a tuple lock for
	 * every apply of an UPDATE or a DELETE (FindReplTupleInLocalRel passes
	 * LockTupleExclusive on both the index and the sequential path), so there
	 * is no lock-free apply to fall back to. And the apply worker deliberately
	 * does NOT advance the replication origin when a transaction fails, to
	 * avoid losing it, so the same transaction is re-sent forever: measured at
	 * one error every 5 seconds, indefinitely, with every INSERT queued behind
	 * it never applying.
	 *
	 * So the subscription is wedged permanently, and the only diagnostic is a
	 * message about row locking, which is not what the user was doing. Name the
	 * situation and the way out instead. INSERT-only publications work: initial
	 * sync and streamed INSERTs use COPY and table_tuple_insert, neither of
	 * which takes a tuple lock.
	 */
	if (IsLogicalWorker())
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("columnar: logical replication cannot apply UPDATE or DELETE "
						"to a columnar table"),
				 errdetail("Applying an UPDATE or a DELETE requires a row lock, which "
						   "columnar storage does not support. The subscription will "
						   "retry this transaction indefinitely and no later change "
						   "will be applied."),
				 errhint("Publish inserts only, with "
						 "CREATE PUBLICATION ... WITH (publish = 'insert'), or "
						 "replicate into a heap table.")));

	COLUMNAR_UNSUPPORTED("row locking");
	return TM_Ok;
}

static void
pgcolumnar_relation_copy_data(Relation rel, const RelFileLocator *newrlocator)
{
	COLUMNAR_UNSUPPORTED("relation copy (ALTER TABLE SET TABLESPACE)");
}

static void
pgcolumnar_relation_copy_for_cluster(COLUMNAR_COPY_FOR_CLUSTER_ARGS)
{
	/*
	 * Name every command that reaches here, and name them per major (#399).
	 *
	 * PostgreSQL 19 adds REPACK, which replaces CLUSTER and VACUUM FULL and
	 * dispatches through this same callback. A 19 user who types REPACK was
	 * previously told "CLUSTER / VACUUM FULL is not supported yet", naming two
	 * commands they did not type and, on 19, the superseded ones. The error
	 * should describe what the user asked for.
	 *
	 * The hint matters more than the message: the operation is available, under
	 * a different name. pgcolumnar.vacuum() reclaims space and the ordering
	 * functions cluster, so this is a spelling difference rather than a missing
	 * capability, and the error is where someone will look for that.
	 */
	ereport(ERROR,
			(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
#if PG_VERSION_NUM >= 190000
			 errmsg("columnar: REPACK, CLUSTER and VACUUM FULL are not supported yet"),
#else
			 errmsg("columnar: CLUSTER and VACUUM FULL are not supported yet"),
#endif
			 errhint("Use pgcolumnar.vacuum() to reclaim space, or "
					 "pgcolumnar.vacuum_sorted() to rewrite in sorted order.")));
}

/*
 * pgcolumnar_index_build_range_scan
 *		Scan every live row of the columnar table and hand it to the index
 *		build callback, so CREATE INDEX (btree or hash) works over a columnar
 *		table (spec 9). Deleted rows (delete vector) are skipped by the reader, so
 *		they are not indexed. Each row's synthetic item pointer (spec 6) is the
 *		TID recorded in the index.
 *
 *		Only a full-table build is supported: a partial block range would have
 *		no meaning for synthetic item pointers, and concurrent validation uses a
 *		separate callback. Pending writes are flushed first so buffered rows are
 *		included in the build.
 */
static double
pgcolumnar_index_build_range_scan(Relation table_rel, Relation index_rel,
								struct IndexInfo *index_info, bool allow_sync,
								bool anyvisible, bool progress,
								BlockNumber start_blockno, BlockNumber numblocks,
								IndexBuildCallback callback, void *callback_state,
								TableScanDesc scan)
{
	PgColumnarReadState *readState;
	bool		ownReadState;
	Bitmapset  *projected;
	int			nProjected = 0;
	EState	   *estate;
	ExprContext *econtext;
	ExprState  *predicate;
	TupleTableSlot *slot;
	Datum		indexValues[INDEX_MAX_KEYS];
	bool		indexNulls[INDEX_MAX_KEYS];
	double		reltuples = 0;
	uint64		rowNumber;

	if (start_blockno != 0 || numblocks != InvalidBlockNumber)
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("columnar: partial-range index build is not supported")));

	/* persist buffered rows and delete marks so the build sees them (spec 9) */
	PgColumnarFlushWriteStateForRelation(RelationGetRelid(table_rel));
	PgColumnarFlushDeleteVectorForRelation(table_rel);

	estate = CreateExecutorState();
	econtext = GetPerTupleExprContext(estate);
	slot = table_slot_create(table_rel, NULL);
	econtext->ecxt_scantuple = slot;

	/* a partial index only indexes rows satisfying its predicate */
	predicate = ExecPrepareQual(index_info->ii_Predicate, estate);

	/*
	 * Obtain the reader. A parallel index build passes the TableScanDesc it
	 * opened with table_beginscan_parallel; that scan already holds a reader
	 * bound to the shared parallel scan, whose single-participant claim (see
	 * pgcolumnar_read_start) makes exactly one participant read the whole table.
	 * We must read through that reader, not a private one: a private full-table
	 * reader in every participant would index every row once per participant,
	 * producing duplicate (key, TID) entries. When no scan is supplied (a serial
	 * build), open a private reader under an MVCC snapshot: the active snapshot
	 * when one is set (planning/DDL always has one), otherwise the transaction
	 * snapshot. The reader advances the command id internally for
	 * read-your-writes.
	 */
	/*
	 * Project. The columns this build needs are all in our own arguments and we
	 * were throwing them away, so a one-column index on a wide table decoded
	 * every column (#413).
	 *
	 * Three sources, and missing any of them reads unset slot values:
	 *   ii_IndexAttrNumbers  the key columns
	 *   ii_Expressions       an expression index references more
	 *   ii_Predicate         a partial index evaluates against more
	 *
	 * PgColumnarProjectionFromAttnos returns NULL for "every column", which
	 * covers a whole-row or system-column reference, and is what the custom scan
	 * already does with the same escapes.
	 *
	 * Computed before the branch because BOTH readers need it. Every
	 * participant in a parallel build computes the same set from the same
	 * IndexInfo, so they agree without having to communicate.
	 */
	{
		Bitmapset  *needed = NULL;
		int			i;

		for (i = 0; i < index_info->ii_NumIndexAttrs; i++)
		{
			AttrNumber	attno = index_info->ii_IndexAttrNumbers[i];

			/* 0 marks an expression column; Vars come from ii_Expressions */
			if (attno != 0)
				needed = bms_add_member(needed,
										attno - FirstLowInvalidHeapAttributeNumber);
		}
		pull_varattnos((Node *) index_info->ii_Expressions, 1, &needed);
		pull_varattnos((Node *) index_info->ii_Predicate, 1, &needed);

		/*
		 * nProjected is a required out-parameter, not the number we report.
		 * The DEBUG1 line below reads the count off the reader instead; see
		 * the comment there for why.
		 */
		projected = PgColumnarProjectionFromAttnos(needed,
												 RelationGetDescr(table_rel)->natts,
												 &nProjected);
	}

	if (scan != NULL)
	{
		/*
		 * An index build always wants the relation's current shape: it is
		 * indexing what the table is now, not what an in-flight rewrite is
		 * converting away from.
		 */
		readState = pgcolumnar_scan_read_state((PgColumnarScanDesc) scan,
											 RelationGetDescr(table_rel));

		/*
		 * This reader was opened through the table-AM scan interface, which has
		 * nowhere to carry a projection, so it would decode every column. We
		 * know better here, so narrow it before the first read.
		 *
		 * This branch is not hypothetical and it is not the rare case: with
		 * parallel maintenance workers available, EVERY participant including
		 * the leader arrives here, and the serial branch below never runs. Fix
		 * only the serial branch and a parallel build stays unprojected.
		 */
		PgColumnarReadSetProjection(readState, projected);
		ownReadState = false;
	}
	else
	{
		Snapshot	snapshot;

		if (ActiveSnapshotSet())
			snapshot = GetActiveSnapshot();
		else
			snapshot = GetTransactionSnapshot();

		readState = PgColumnarBeginRead(table_rel, snapshot, NULL,
									  projected, 0, NULL);
		ownReadState = true;
	}

	/*
	 * Say which branch ran and how wide the reader it produced will actually
	 * read, so a test can assert the projection NARROWED rather than infer it
	 * from a stopwatch. A fix that silently did nothing would pass every
	 * correctness check, and a wall-clock check on a quiet machine, which is
	 * the failure mode this projection is being added to avoid.
	 *
	 * The count comes from the READER, not from nProjected. Reporting what we
	 * computed would keep printing "1 of 20" if the parallel branch stopped
	 * applying it, and the assertion guarding that branch would pass while the
	 * build read every column. Reporting what the reader will decode cannot.
	 *
	 * DEBUG1, so it costs nothing at the default log level.
	 */
	elog(DEBUG1,
		 "columnar: %s index build on \"%s\" projecting %d of %d columns",
		 scan != NULL ? "parallel" : "serial",
		 RelationGetRelationName(index_rel),
		 PgColumnarReadProjectedCount(readState),
		 RelationGetDescr(table_rel)->natts);

	while (true)
	{
		CHECK_FOR_INTERRUPTS();

		ExecClearTuple(slot);
		if (!PgColumnarReadNextRow(readState, slot->tts_values, slot->tts_isnull,
								 &rowNumber))
			break;
		ExecStoreVirtualTuple(slot);

		reltuples += 1;

		MemoryContextReset(econtext->ecxt_per_tuple_memory);

		if (predicate != NULL && !ExecQual(predicate, econtext))
			continue;

		FormIndexDatum(index_info, slot, estate, indexValues, indexNulls);

		PgColumnarRowNumberToItemPointer(rowNumber, &slot->tts_tid);

		callback(index_rel, &slot->tts_tid, indexValues, indexNulls, true,
				 callback_state);
	}

	if (ownReadState)
		PgColumnarEndRead(readState);
	ExecDropSingleTupleTableSlot(slot);
	FreeExecutorState(estate);

	/*
	 * The table AM contract makes index_build_range_scan the owner of a scan the
	 * caller supplied: it must end it, exactly as heapam_index_build_range_scan
	 * calls table_endscan on the passed scan whether or not it created the scan
	 * itself. pgcolumnar_scan_begin took a relation reference (and, for a worker,
	 * a registered snapshot) and created the reader used above; table_endscan
	 * runs pgcolumnar_scan_end, which ends that reader and releases the reference.
	 * Omitting this leaked one relation reference per build participant, which
	 * surfaced at commit as "resource was not closed: relation".
	 */
	if (scan != NULL)
		table_endscan(scan);

	return reltuples;
}

static void
pgcolumnar_index_validate_scan(Relation table_rel, Relation index_rel,
							 struct IndexInfo *index_info, Snapshot snapshot,
							 struct ValidateIndexState *state)
{
	COLUMNAR_UNSUPPORTED("concurrent index validate");
}

static bool
pgcolumnar_scan_sample_next_block(TableScanDesc scan,
								struct SampleScanState *scanstate)
{
	COLUMNAR_UNSUPPORTED("TABLESAMPLE");
	return false;
}

static bool
pgcolumnar_scan_sample_next_tuple(TableScanDesc scan,
								struct SampleScanState *scanstate,
								TupleTableSlot *slot)
{
	COLUMNAR_UNSUPPORTED("TABLESAMPLE");
	return false;
}

/* -------------------------------------------------------------------------
 * the routine
 * ------------------------------------------------------------------------- */

static const TableAmRoutine pgcolumnar_am_methods = {
	.type = T_TableAmRoutine,

	.slot_callbacks = pgcolumnar_slot_callbacks,

	.scan_begin = pgcolumnar_scan_begin,
	.scan_end = pgcolumnar_scan_end,
	.scan_rescan = pgcolumnar_scan_rescan,
	.scan_getnextslot = pgcolumnar_scan_getnextslot,

	.parallelscan_estimate = pgcolumnar_parallelscan_estimate,
	.parallelscan_initialize = pgcolumnar_parallelscan_initialize,
	.parallelscan_reinitialize = pgcolumnar_parallelscan_reinitialize,

	.index_fetch_begin = pgcolumnar_index_fetch_begin,
	.index_fetch_reset = pgcolumnar_index_fetch_reset,
	.index_fetch_end = pgcolumnar_index_fetch_end,
	.index_fetch_tuple = pgcolumnar_index_fetch_tuple,

	.tuple_fetch_row_version = pgcolumnar_tuple_fetch_row_version,
	.tuple_tid_valid = pgcolumnar_tuple_tid_valid,
	.tuple_get_latest_tid = pgcolumnar_tuple_get_latest_tid,
	.tuple_satisfies_snapshot = pgcolumnar_tuple_satisfies_snapshot,
#if PG_VERSION_NUM < 140000
	.COLUMNAR_AM_INDEX_DELETE_FIELD = pgcolumnar_compute_xid_horizon_for_tuples,
#else
	.COLUMNAR_AM_INDEX_DELETE_FIELD = pgcolumnar_index_delete_tuples,
#endif

	.tuple_insert = pgcolumnar_tuple_insert,
	.tuple_insert_speculative = pgcolumnar_tuple_insert_speculative,
	.tuple_complete_speculative = pgcolumnar_tuple_complete_speculative,
	.multi_insert = pgcolumnar_multi_insert,
	.tuple_delete = pgcolumnar_tuple_delete,
	.tuple_update = pgcolumnar_tuple_update,
	.tuple_lock = pgcolumnar_tuple_lock,
	.finish_bulk_insert = pgcolumnar_finish_bulk_insert,

	.COLUMNAR_AM_SET_NEW_FILE_FIELD = pgcolumnar_relation_set_new_filelocator,
	.relation_nontransactional_truncate = pgcolumnar_relation_nontransactional_truncate,
	.relation_copy_data = pgcolumnar_relation_copy_data,
	.relation_copy_for_cluster = pgcolumnar_relation_copy_for_cluster,
	.relation_vacuum = pgcolumnar_relation_vacuum,
	.scan_analyze_next_block = pgcolumnar_scan_analyze_next_block,
	.scan_analyze_next_tuple = pgcolumnar_scan_analyze_next_tuple,
	.index_build_range_scan = pgcolumnar_index_build_range_scan,
	.index_validate_scan = pgcolumnar_index_validate_scan,

	.relation_size = pgcolumnar_relation_size,
	.relation_needs_toast_table = pgcolumnar_relation_needs_toast_table,

	.relation_estimate_size = pgcolumnar_relation_estimate_size,

	.scan_sample_next_block = pgcolumnar_scan_sample_next_block,
	.scan_sample_next_tuple = pgcolumnar_scan_sample_next_tuple,
};

Datum
pgcolumnar_handler(PG_FUNCTION_ARGS)
{
	PG_RETURN_POINTER(&pgcolumnar_am_methods);
}

/* -------------------------------------------------------------------------
 * transaction callback: flush pending writes at pre-commit
 * ------------------------------------------------------------------------- */

static void
pgcolumnar_xact_callback(XactEvent event, void *arg)
{
	switch (event)
	{
		case XACT_EVENT_PRE_COMMIT:
		case XACT_EVENT_PARALLEL_PRE_COMMIT:
		case XACT_EVENT_PREPARE:
			PgColumnarFlushAllPendingWrites();
			PgColumnarFlushAllDeleteVectors();

			/*
			 * PREPARE deletes TopTransactionContext without ever reaching the
			 * COMMIT/ABORT arms below, so a memo slot populated outside any
			 * executor lifecycle (a fetch during a utility command has no
			 * executor-end reset) would keep a dangling context pointer into
			 * the next transaction. Descriptors only; the contexts are still
			 * live here and fall with the transaction's memory.
			 */
			PgColumnarGroupMemoReset(false);
			break;
		case XACT_EVENT_COMMIT:
		case XACT_EVENT_ABORT:
		case XACT_EVENT_PARALLEL_COMMIT:
		case XACT_EVENT_PARALLEL_ABORT:
			PgColumnarDiscardAllPendingWrites();
			PgColumnarDiscardAllDeleteVectors();
			PgColumnarDiscardFetchCache();
			/*
			 * Descriptors only: the memo contexts are TopTransactionContext
			 * children, still live at callback time but taken by the
			 * transaction's memory teardown right after.
			 */
			PgColumnarGroupMemoReset(false);
			break;
		default:
			break;
	}
}

/* -------------------------------------------------------------------------
 * subtransaction callback: discard or promote pending work of a savepoint
 * ------------------------------------------------------------------------- */

static void
pgcolumnar_subxact_callback(SubXactEvent event, SubTransactionId mySubid,
						  SubTransactionId parentSubid, void *arg)
{
	switch (event)
	{
		case SUBXACT_EVENT_ABORT_SUB:
			PgColumnarWriteStateDiscardSubXact(mySubid);
			PgColumnarDeleteVectorDiscardSubXact(mySubid);

			/*
			 * The abort flips the xmin verdict on any group the savepoint
			 * flushed, with no change in snapshot content or command id --
			 * the two things the group-list memo keys on -- so a memo built
			 * while the subxact was live would keep serving the aborted
			 * group's rows. The per-fetch catalog read this memo replaced
			 * re-judged xmin every time; the reset restores that. (#709)
			 */
			PgColumnarGroupMemoReset(true);
			break;
		case SUBXACT_EVENT_COMMIT_SUB:
			PgColumnarWriteStatePromoteSubXact(mySubid, parentSubid);
			PgColumnarDeleteVectorPromoteSubXact(mySubid, parentSubid);
			break;
		default:
			break;
	}
}

/* -------------------------------------------------------------------------
 * executor end hook: flush pending writes at statement end
 *
 * INSERT/UPDATE/DELETE do not call finish_bulk_insert, so flush here at the
 * end of each executed statement. Flushing while the writing statement's
 * subtransaction is still current is what makes savepoint rollback correct:
 * a buffer written before a savepoint is persisted (and attributed) under the
 * outer subtransaction, so it survives a later ROLLBACK TO, while a buffer
 * written after the savepoint is attributed to the inner subtransaction and
 * is correctly discarded on its rollback (spec 9).
 * ------------------------------------------------------------------------- */

static void
pgcolumnar_executor_end(QueryDesc *queryDesc)
{
	if (prev_executor_end_hook)
		prev_executor_end_hook(queryDesc);
	else
		standard_ExecutorEnd(queryDesc);

	PgColumnarFlushAllPendingWrites();
	PgColumnarFlushAllDeleteVectors();

	/*
	 * The fetch cache is scoped to a statement, so release it here rather than
	 * waiting for transaction end. Without this a statement that filled every
	 * slot pins them for the rest of the transaction, and a session sitting idle
	 * in transaction after one UPDATE holds them indefinitely.
	 */
	PgColumnarDiscardFetchCache();
	/* same statement scope for the group-list memo; its contexts are live */
	PgColumnarGroupMemoReset(true);
}

/* -------------------------------------------------------------------------
 * object access hook: clean up metadata when a columnar table is dropped, and
 * refuse a foreign key that references a columnar table
 * ------------------------------------------------------------------------- */

/*
 * pgcolumnar_reject_fk_to_columnar
 *		Raise on a foreign key whose referenced side is a columnar table.
 *
 * The referential-integrity check reads the referenced row with FOR KEY SHARE,
 * to hold the parent key still until the referencing transaction ends. Row
 * locking is not implemented for columnar tables (tuple_lock raises), so that
 * read cannot succeed.
 *
 * Without this the constraint is accepted and then every insert into the
 * referencing table fails, including inserts that satisfy it:
 *
 *     CREATE TABLE parent (id int PRIMARY KEY) USING pgcolumnar;
 *     INSERT INTO parent VALUES (1);
 *     CREATE TABLE child (id int REFERENCES parent(id));   -- accepted
 *     INSERT INTO child VALUES (1);                        -- parent row exists
 *     ERROR:  columnar: row locking is not supported yet
 *
 * A table that can never be written to is worse than a constraint that is
 * refused, and the refusal belongs where the configuration is chosen rather
 * than at every use of it. This is the shape core uses for an unlogged table
 * under a foreign key, which fails at CREATE TABLE and not at each INSERT.
 *
 * Only the referenced side is refused. Columnar on the referencing side is
 * fine: that direction reads the parent, which is heap, and writes the child
 * through the ordinary insert path.
 */
static void
pgcolumnar_reject_fk_to_columnar(Oid constraintId)
{
	Relation	conRel;
	SysScanDesc scan;
	ScanKeyData key;
	HeapTuple	tup;
	Oid			referenced = InvalidOid;

	/*
	 * Read pg_constraint with SnapshotSelf rather than through the syscache.
	 * The post-create hook fires inside the command that inserted the row and
	 * before any CommandCounterIncrement, so a syscache lookup does not find it
	 * and the check silently passes -- which is exactly how the first version of
	 * this failed, quietly and identically to having no check at all.
	 */
	conRel = table_open(ConstraintRelationId, AccessShareLock);
	ScanKeyInit(&key, Anum_pg_constraint_oid, BTEqualStrategyNumber,
				F_OIDEQ, ObjectIdGetDatum(constraintId));
	scan = systable_beginscan(conRel, ConstraintOidIndexId, true,
							  SnapshotSelf, 1, &key);
	tup = systable_getnext(scan);
	if (HeapTupleIsValid(tup))
	{
		Form_pg_constraint con = (Form_pg_constraint) GETSTRUCT(tup);

		if (con->contype == CONSTRAINT_FOREIGN)
			referenced = con->confrelid;
	}
	systable_endscan(scan);
	table_close(conRel, AccessShareLock);

	if (!OidIsValid(referenced))
		return;

	if (get_rel_relkind(referenced) != RELKIND_RELATION)
		return;

	if (PgColumnarIsColumnarRelation(referenced))
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("cannot create a foreign key referencing columnar table \"%s\"",
						get_rel_name(referenced)),
				 errdetail("Enforcing the constraint requires locking the referenced row, "
						   "and row locking is not supported on columnar tables."),
				 errhint("Reference a heap table, or keep the referencing table columnar "
						 "and the referenced table heap.")));
}

/*
 * pgcolumnar_am_is_columnar
 *		Does this table access method oid resolve to this extension's routine?
 *
 * By handler identity rather than by name, so a second name for the same access
 * method is still recognised.
 */
static bool
pgcolumnar_am_is_columnar(Oid amoid)
{
	HeapTuple	tup;
	Form_pg_am	amform;
	Oid			handler;

	if (!OidIsValid(amoid))
		return false;

	tup = SearchSysCache1(AMOID, ObjectIdGetDatum(amoid));
	if (!HeapTupleIsValid(tup))
		return false;

	amform = (Form_pg_am) GETSTRUCT(tup);
	handler = amform->amhandler;

	/*
	 * Only ask a table access method for its routine. An index access method
	 * name here is a user error that core reports precisely; GetTableAmRoutine
	 * would assert on it first.
	 */
	if (amform->amtype != AMTYPE_TABLE || !OidIsValid(handler))
	{
		ReleaseSysCache(tup);
		return false;
	}
	ReleaseSysCache(tup);

	return GetTableAmRoutine(handler) == &pgcolumnar_am_methods;
}

/*
 * pgcolumnar_fk_referencing
 *		Name of some foreign key whose referenced side is relid, or NULL.
 */
static char *
pgcolumnar_fk_referencing(Oid relid)
{
	Relation	conRel;
	SysScanDesc scan;
	ScanKeyData key[2];
	HeapTuple	tup;
	char	   *conname = NULL;

	ScanKeyInit(&key[0], Anum_pg_constraint_contype, BTEqualStrategyNumber,
				F_CHAREQ, CharGetDatum(CONSTRAINT_FOREIGN));
	ScanKeyInit(&key[1], Anum_pg_constraint_confrelid, BTEqualStrategyNumber,
				F_OIDEQ, ObjectIdGetDatum(relid));

	/*
	 * pg_constraint has no index on confrelid, so this is a heap scan with the
	 * keys applied -- the same way core finds the foreign keys pointing at a
	 * relation. It runs once per ALTER TABLE ... SET ACCESS METHOD.
	 */
	conRel = table_open(ConstraintRelationId, AccessShareLock);
	scan = systable_beginscan(conRel, InvalidOid, false, NULL, 2, key);
	tup = systable_getnext(scan);
	if (HeapTupleIsValid(tup))
	{
		Form_pg_constraint con = (Form_pg_constraint) GETSTRUCT(tup);

		conname = pstrdup(NameStr(con->conname));
	}
	systable_endscan(scan);
	table_close(conRel, AccessShareLock);

	return conname;
}

/*
 * pgcolumnar_reject_set_am_to_columnar
 *		Refuse ALTER TABLE ... SET ACCESS METHOD to columnar when the relation is
 *		the referenced side of a foreign key.
 *
 * pgcolumnar_reject_fk_to_columnar closes this door where the constraint is
 * created. The same unusable configuration is reachable from the other end, by
 * making the referenced table columnar after the constraint already exists:
 *
 *     CREATE TABLE p (id int PRIMARY KEY);          -- heap
 *     CREATE TABLE c (id int REFERENCES p(id));     -- fine
 *     ALTER TABLE p SET ACCESS METHOD pgcolumnar;   -- was accepted
 *     INSERT INTO c VALUES (1);                     -- ERROR: row locking ...
 *
 * After that the constraint is still in pg_constraint, the parent is columnar,
 * and every subsequent insert into the child fails -- the exact trap the
 * constraint-side refusal exists to remove, arrived at from the other side. It
 * is not unsound (deletes of referenced parent rows are still refused, and no
 * child row is orphaned), so this is the same usability trap rather than a new
 * correctness one.
 *
 * Checked here, in ProcessUtility, rather than in the object access hook. The
 * post-alter hook fires for every ALTER TABLE subcommand and does not say which
 * one ran, so refusing there would also reject an unrelated ALTER -- a rename,
 * say -- on a table that had already reached this state under an older version.
 * Refusing the command that chooses the configuration is both narrower and the
 * rule the constraint-side check already follows.
 *
 * pgcolumnar.alter_table_set_access_method() needs no separate treatment: on
 * every supported major it executes this same statement.
 */
static void
pgcolumnar_reject_set_am_to_columnar(AlterTableStmt *stmt)
{
	Oid			relid;
	ListCell   *lc;

	relid = RangeVarGetRelid(stmt->relation, NoLock, true);
	if (!OidIsValid(relid))
		return;

	/*
	 * Ordinary tables and partitioned ones.
	 *
	 * A partitioned table has no storage of its own, so setting its access
	 * method breaks nothing at the time. What it does is choose the access
	 * method every partition created afterwards inherits -- and if the table is
	 * referenced by a foreign key, every one of those is then refused by the
	 * constraint-side check, because core clones the foreign key to each new
	 * partition:
	 *
	 *     ALTER TABLE pp SET ACCESS METHOD pgcolumnar;   -- accepted
	 *     CREATE TABLE pp9 PARTITION OF pp ...;
	 *     ERROR: cannot create a foreign key referencing columnar table "pp9"
	 *
	 * That error names pp9, a table the user has just written, and says nothing
	 * about the earlier ALTER that caused it. Refusing here is the same rule
	 * this check already applies to an ordinary table: a configuration that
	 * cannot be honoured is refused where it is chosen, not at every later use.
	 *
	 * Nothing is lost by refusing. The intent of setting a columnar access
	 * method on a partitioned table is that its partitions be columnar, and
	 * while the foreign key exists that is unreachable by any route (#201).
	 */
	{
		char		relkind = get_rel_relkind(relid);

		if (relkind != RELKIND_RELATION && relkind != RELKIND_PARTITIONED_TABLE)
			return;
	}

	foreach(lc, stmt->cmds)
	{
		AlterTableCmd *cmd = (AlterTableCmd *) lfirst(lc);
		const char *amname;
		char	   *conname;

		if (cmd->subtype != AT_SetAccessMethod)
			continue;

		/* SET ACCESS METHOD DEFAULT leaves the name unset (PG17+) */
		amname = cmd->name ? cmd->name : default_table_access_method;

		if (!pgcolumnar_am_is_columnar(get_table_am_oid(amname, true)))
			continue;

		conname = pgcolumnar_fk_referencing(relid);
		if (conname == NULL)
			continue;

		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("cannot convert table \"%s\" to columnar storage while it is referenced by a foreign key",
						get_rel_name(relid)),
				 errdetail("Constraint \"%s\" requires locking the referenced row, "
						   "and row locking is not supported on columnar tables.",
						   conname),
				 errhint("Drop the foreign key constraint first, or leave this "
						 "table on a row store.")));
	}
}

static void
pgcolumnar_process_utility(PlannedStmt *pstmt, const char *queryString,
						 bool readOnlyTree, ProcessUtilityContext context,
						 ParamListInfo params, QueryEnvironment *queryEnv,
						 DestReceiver *dest, QueryCompletion *qc)
{
	Node	   *parsetree = pstmt->utilityStmt;

	/* read-only inspection, so readOnlyTree needs no copy of the tree */
	if (parsetree != NULL && IsA(parsetree, AlterTableStmt))
		pgcolumnar_reject_set_am_to_columnar((AlterTableStmt *) parsetree);

	if (prev_process_utility_hook)
		prev_process_utility_hook(pstmt, queryString, readOnlyTree, context,
								  params, queryEnv, dest, qc);
	else
		standard_ProcessUtility(pstmt, queryString, readOnlyTree, context,
								params, queryEnv, dest, qc);

	/*
	 * A column rename must be carried through the ordering mark (#778). The
	 * mark records its sort key as column NAMES and both ordering self-gates
	 * compare those against the current attname, so a rename that moves a name
	 * onto another column makes a gate skip work it must do. See
	 * PgColumnarRenameSortKeyColumn.
	 *
	 * AFTER the statement, deliberately: a rename that errors, or that names a
	 * column or relation which does not exist, must leave the mark alone. By
	 * this point the rename has committed to the catalog and holds
	 * AccessExclusiveLock on the relation, so the lookup below cannot race and
	 * needs no lock of its own.
	 */
	if (parsetree != NULL && IsA(parsetree, RenameStmt))
	{
		RenameStmt *rs = (RenameStmt *) parsetree;

		if (rs->renameType == OBJECT_COLUMN && rs->relation != NULL &&
			rs->subname != NULL && rs->newname != NULL)
		{
			Oid			relid = RangeVarGetRelid(rs->relation, NoLock, true);

			if (OidIsValid(relid))
			{
				List	   *kin;
				ListCell   *lc;

				/*
				 * Every INHERITANCE CHILD and PARTITION too, not just the
				 * relation named in the statement. A column rename cascades to
				 * descendants, which are separate relations with their own
				 * storage and their own marks, and PostgreSQL REFUSES
				 * "ALTER TABLE child RENAME COLUMN" with "cannot rename
				 * inherited column" -- so for a columnar partition the parent is
				 * the only route, and a hook that looked only at the named
				 * relation would never fire for it at all.
				 *
				 * Not gated on the named relation being columnar, deliberately:
				 * a partitioned parent is not itself a columnar relation, and
				 * testing it here would close the door before the columnar
				 * children are reached. The per-descendant test below is the
				 * one that decides.
				 *
				 * And the walk cannot be too WIDE, which is the natural worry
				 * about it -- moving a mark for a descendant whose column never
				 * changed. PostgreSQL refuses both halves of that. A child
				 * alone is refused with "cannot rename inherited column", and
				 * the parent alone is refused too: ALTER TABLE ONLY p RENAME
				 * COLUMN gives "inherited column must be renamed in child
				 * tables too". So every rename that reaches this hook has
				 * already cascaded to the whole hierarchy, and walking it
				 * unconditionally is CORRECT rather than merely safe.
				 *
				 * That is the argument to keep. Without it the walk reads as
				 * over-broad and invites a later condition that reintroduces
				 * the bug (OffgridwithJD, #779 review).
				 *
				 * The rename already holds the locks it needs on the whole
				 * hierarchy, so NoLock here takes no new lock and cannot race.
				 */
				kin = find_all_inheritors(relid, NoLock, NULL);
				foreach(lc, kin)
				{
					Oid			kid = lfirst_oid(lc);

					if (!PgColumnarIsColumnarRelation(kid))
						continue;

					{
						Relation	rel = relation_open(kid, NoLock);

						PgColumnarRenameSortKeyColumn(PgColumnarStorageId(rel),
													  rs->subname, rs->newname);
						relation_close(rel, NoLock);
					}

					/*
					 * And the DECLARED key, which vacuum_sorted(t) resolves
					 * when called with no columns. Maintaining the mark without
					 * this would make the two catalogs name different columns,
					 * so the same call with the same declared intent would
					 * silently rewrite on a different physical key.
					 */
					PgColumnarRenameDeclaredSortByColumn(kid, rs->subname,
														 rs->newname);
				}
				list_free(kin);
			}
		}
	}
}

static void
pgcolumnar_object_access(ObjectAccessType access, Oid classId, Oid objectId,
					   int subId, void *arg)
{
	if (prev_object_access_hook)
		prev_object_access_hook(access, classId, objectId, subId, arg);

	if (access == OAT_POST_CREATE && classId == ConstraintRelationId)
		pgcolumnar_reject_fk_to_columnar(objectId);

	if (access == OAT_DROP && classId == RelationRelationId && subId == 0)
	{
		Relation	rel;

		if (get_rel_relkind(objectId) != RELKIND_RELATION)
			return;

		/* DROP already holds AccessExclusiveLock on the relation */
		rel = relation_open(objectId, NoLock);

		if (rel->rd_tableam == &pgcolumnar_am_methods)
		{
			uint64		storageId = PgColumnarStorageId(rel);

			/*
			 * A projection keeps its own storage, so dropping the table has to
			 * drop that too. Deleting only the base storage left the
			 * projection's row groups, chunks, zone maps and bloom filters
			 * behind with no relation to reach them from: metadata for a table
			 * that no longer exists, accumulating one projection's worth per
			 * drop. This is the same loop pgcolumnar_vacuum.c runs when it
			 * rewrites into fresh storage.
			 */
			pgcolumnar_delete_storage_tree(storageId);
			PgColumnarDeleteOptions(objectId);
			/*
			 * And the projection declarations, for the same reason and in the
			 * same place (#304). A declaration left behind holds a regclass that
			 * no longer resolves, which config_dump then dumps as a bare OID and
			 * rebuild_projections() aborts on, taking every other table in the
			 * database with it.
			 */
			PgColumnarDeleteProjectionDeclarationsForRel(objectId);
		}

		relation_close(rel, NoLock);
	}
}

/* -------------------------------------------------------------------------
 * planner hook: forbid index-only scans on columnar tables
 *
 * A columnar table has no visibility map, so an index-only scan cannot decide
 * visibility from the map and is not supported (spec 9). An ordinary index scan
 * is fine because it fetches each row through index_fetch_tuple, which applies
 * the delete vector. We forbid index-only scans by clearing each candidate index's
 * per-column "can return" flags for a columnar table, before the planner builds
 * any path; the planner then builds a plain index scan instead of an index-only
 * scan for the same index.
 *
 * The clearing must happen after get_relation_info() has populated the base
 * relation's indexlist. Through PG18 that is get_relation_info_hook; PG19
 * removed it and added build_simple_rel_hook, which fires at the same point
 * (right after get_relation_info in build_simple_rel) for exactly this kind of
 * editorializing on the index list. Both routes yield an identical plan.
 * ------------------------------------------------------------------------- */

static bool
pgcolumnar_relation_is_columnar(Oid relid)
{
	if (pgcolumnar_am_oid_cache == InvalidOid)
		pgcolumnar_am_oid_cache = get_am_oid("pgcolumnar", true);

	return OidIsValid(pgcolumnar_am_oid_cache) &&
		get_rel_relam(relid) == pgcolumnar_am_oid_cache;
}

/* GUC: when on, allow the planner to build index-only-scan paths for columnar
 * tables, served by the VM fork (gap 28). Default on: the phase-5 MVCC,
 * concurrency, and crash-recovery suites prove the all-visible protocol (the
 * horizon accounts for open snapshots and every write clears the bit, both
 * WAL-logged), and a not-all-visible block always falls back to the
 * snapshot-checked fetch, so results are correct regardless. */
bool		pgcolumnar_enable_index_only_scan = true;

/* clear the "can return" flags of every index on a columnar relation */
static void
pgcolumnar_forbid_index_only_scan(Oid relid, RelOptInfo *rel)
{
	ListCell   *lc;

	/* when index-only scans are enabled, leave the index canreturn flags intact
	 * so the planner may choose an IOS; the VM fork (set by lazy vacuum) drives
	 * whether the executor skips the fetch, and a not-all-visible block still
	 * falls back to pgcolumnar_index_fetch_tuple, so results are always correct. */
	if (pgcolumnar_enable_index_only_scan)
		return;

	if (!OidIsValid(relid) || !pgcolumnar_relation_is_columnar(relid))
		return;

	foreach(lc, rel->indexlist)
	{
		IndexOptInfo *index = (IndexOptInfo *) lfirst(lc);
		int			i;

		if (index->canreturn == NULL)
			continue;

		for (i = 0; i < index->ncolumns; i++)
			index->canreturn[i] = false;
	}
}

#if PG_VERSION_NUM >= 190000
static void
pgcolumnar_build_simple_rel(PlannerInfo *root, RelOptInfo *rel,
						  RangeTblEntry *rte)
{
	if (prev_build_simple_rel_hook)
		prev_build_simple_rel_hook(root, rel, rte);

	if (rte->rtekind == RTE_RELATION)
		pgcolumnar_forbid_index_only_scan(rte->relid, rel);
}
#else
static void
pgcolumnar_get_relation_info(PlannerInfo *root, Oid relationObjectId,
						   bool inhparent, RelOptInfo *rel)
{
	if (prev_get_relation_info_hook)
		prev_get_relation_info_hook(root, relationObjectId, inhparent, rel);

	pgcolumnar_forbid_index_only_scan(relationObjectId, rel);
}
#endif

/* -------------------------------------------------------------------------
 * module init
 * ------------------------------------------------------------------------- */

void
_PG_init(void)
{
	/*
	 * Virtual slot behaviour, except that a copied heap tuple keeps the slot's
	 * item pointer. See the comment on pgcolumnar_slot_copy_heap_tuple.
	 */
	PgColumnarSlotOps = TTSOpsVirtual;
	PgColumnarSlotOps.base_slot_size = sizeof(PgColumnarSlot);
	PgColumnarSlotOps.copy_heap_tuple = pgcolumnar_slot_copy_heap_tuple;
	PgColumnarSlotOps.init = pgcolumnar_slot_init;
	PgColumnarSlotOps.getsomeattrs = pgcolumnar_slot_getsomeattrs;
	PgColumnarSlotOps.clear = pgcolumnar_slot_clear;
	PgColumnarSlotOps.materialize = pgcolumnar_slot_materialize;
	PgColumnarSlotOps.copyslot = pgcolumnar_slot_copyslot;
	PgColumnarSlotOps.copy_minimal_tuple = pgcolumnar_slot_copy_minimal_tuple;

	DefineCustomIntVariable("pgcolumnar.stripe_row_limit",
							"Maximum number of rows per stripe.",
							NULL,
							&pgcolumnar_stripe_row_limit,
							150000,
							1000, INT_MAX,
							PGC_USERSET,
							0,
							NULL, NULL, NULL);

	DefineCustomIntVariable("pgcolumnar.chunk_group_row_limit",
							"Maximum number of rows per chunk group.",
							NULL,
							&pgcolumnar_chunk_group_row_limit,
							10000,
							100, INT_MAX,
							PGC_USERSET,
							0,
							NULL, NULL, NULL);

	DefineCustomIntVariable("pgcolumnar.fsst_verdict_reuse",
							"Row groups that may reuse a column's FSST keep/drop verdict.",
							"Deciding whether an FSST symbol table pays for itself costs a "
							"whole-corpus encode plus a compression pass, and the answer "
							"cannot be sampled, so it is asked once per column per row "
							"group. For a column whose data does not change character that "
							"re-derives a constant. 0 asks every time, which is the "
							"behaviour before this setting existed.",
							&pgcolumnar_fsst_verdict_reuse,
							16,
							0, INT_MAX,
							PGC_USERSET,
							0,
							NULL, NULL, NULL);

	DefineCustomIntVariable("pgcolumnar.encoding_sample_rows",
							"Rows sampled to choose a chunk's value encoding.",
							"Candidate encodings are estimated on a windowed sample "
							"of this many values, and only the best two are applied "
							"to the whole vector. 0 applies every candidate to the "
							"whole vector, which is what earlier versions did. A "
							"value below 128 is treated as 0, because a sample that "
							"small cannot rank candidates: every candidate's fixed "
							"header would exceed the sample itself.",
							&pgcolumnar_encoding_sample_rows,
							2048,
							0, INT_MAX,
							PGC_USERSET,
							0,
							NULL, NULL, NULL);

	DefineCustomEnumVariable("pgcolumnar.compression",
							 "Default compression codec for new chunks.",
							 NULL,
							 &pgcolumnar_compression,
							 COLUMNAR_COMPRESSION_ZSTD,
							 pgcolumnar_compression_options,
							 PGC_USERSET,
							 0,
							 NULL, NULL, NULL);

	DefineCustomIntVariable("pgcolumnar.compression_level",
							"Compression level for the zstd codec.",
							NULL,
							&pgcolumnar_compression_level,
							3,
							1, 22,
							PGC_USERSET,
							0,
							NULL, NULL, NULL);

	DefineCustomIntVariable("pgcolumnar.fsst_min_gain_percent",
							"Minimum compressed size reduction, in percent, for FSST "
							"string encoding to be kept for a column chunk.",
							"Building FSST codes for every vector is a dominant cost of "
							"a text or varlena load (issue #155). At 0 FSST is kept on "
							"any compressed win, however small; a higher value keeps it "
							"only when it saves at least that percentage after the block "
							"codec has run, trading a bounded size regression on "
							"marginal chunks for skipping their per-vector FSST encode. "
							"The default of 5 costs about 2 percent stored size on the "
							"shapes where FSST barely wins, such as high-entropy text, "
							"and saves roughly a third of their load time; where FSST "
							"wins clearly it changes nothing. Set to 0 to keep FSST on "
							"any win at all.",
							&pgcolumnar_fsst_min_gain_percent,
							5,
							0, 99,
							PGC_USERSET,
							0,
							NULL, NULL, NULL);

	DefineCustomIntVariable("pgcolumnar.qual_skipvec_min_payload_cols",
							"Minimum projected non-qual columns for per-vector decode "
							"gating of an unprunable qual to run (#595).",
							"Gating (#452 phase 2) evaluates an unprunable qual per "
							"1024-row vector so a no-match vector skips its payload "
							"decode. That only spares the projected columns that are "
							"not the qual's own -- a wide SELECT * has many, a count or "
							"single-column aggregate almost none. Below this many, the "
							"per-vector evaluation cannot pay for itself, so gating is "
							"skipped; the benchmark shows it wins the wide projection "
							"(ClickBench q24) and regresses the narrow aggregates. Set "
							"to 0 to disable the width gate and always gate.",
							&pgcolumnar_qual_skipvec_min_payload_cols,
							20,
							0, 100000,
							PGC_USERSET,
							0,
							NULL, NULL, NULL);

	DefineCustomBoolVariable("pgcolumnar.enable_qual_pushdown",
							 "Push scan qualifiers down for chunk-group skipping.",
							 NULL,
							 &pgcolumnar_enable_qual_pushdown,
							 true,
							 PGC_USERSET,
							 0,
							 NULL, NULL, NULL);

	/*
	 * Dev control for #393: off maps every page read 1:1 onto a ranged request
	 * so the request-count suite can measure both arms in one run. Nothing
	 * else should turn it off; the buffered path is the supported one.
	 */
	DefineCustomBoolVariable("pgcolumnar.objstore_buffered",
							 "Coalesce remote Parquet reads to one request per column chunk.",
							 NULL,
							 &pgcolumnar_objstore_buffered,
							 true,
							 PGC_USERSET,
							 0,
							 NULL, NULL, NULL);

	DefineCustomBoolVariable("pgcolumnar.enable_late_materialization",
							 "Evaluate the scan qualifier before building the columns it does not read.",
							 "A row the qualifier rejects has its remaining projected columns "
							 "stepped over rather than built, so decode cost scales with rows "
							 "emitted rather than rows scanned (#452). Off restores the single "
							 "pass, and is the A/B oracle the late-materialization test uses.",
							 &pgcolumnar_enable_late_materialization,
							 true,
							 PGC_USERSET,
							 0,
							 NULL, NULL, NULL);

	DefineCustomBoolVariable("pgcolumnar.enable_column_projection",
							 "Read only the columns a query references.",
							 "When off, every column of each visited row group is "
							 "read and decoded, as before the projection was honored. "
							 "Provided as an escape hatch and as the A/B oracle the "
							 "projection tests compare against.",
							 &pgcolumnar_enable_column_projection,
							 true,
							 PGC_USERSET,
							 0,
							 NULL, NULL, NULL);

	DefineCustomBoolVariable("pgcolumnar.enable_custom_scan",
							 "Use the columnar custom scan path for columnar tables.",
							 NULL,
							 &pgcolumnar_enable_custom_scan,
							 true,
							 PGC_USERSET,
							 0,
							 NULL, NULL, NULL);

	DefineCustomBoolVariable("pgcolumnar.enable_vectorization",
							 "Use the vectorized aggregate fast path.",
							 NULL,
							 &pgcolumnar_enable_vectorization,
							 true,
							 PGC_USERSET,
							 0,
							 NULL, NULL, NULL);

	DefineCustomBoolVariable("pgcolumnar.enable_group_vectorization",
							 "Use the vectorized aggregate fast path for GROUP BY queries.",
							 NULL,
							 &pgcolumnar_enable_group_vectorization,
							 false,
							 PGC_USERSET,
							 0,
							 NULL, NULL, NULL);

	DefineCustomBoolVariable("pgcolumnar.enable_ungrouped_vector_agg",
							 "Use the vectorized aggregate fast path for ungrouped "
							 "aggregates with a filter or sum/avg over "
							 "int8/float/numeric.",
							 NULL,
							 &pgcolumnar_enable_ungrouped_vector_agg,
							 false,
							 PGC_USERSET,
							 0,
							 NULL, NULL, NULL);

	DefineCustomBoolVariable("pgcolumnar.enable_parallel_vector_agg",
							 "Make the ungrouped vectorized batch fold parallel-aware: "
							 "each worker folds distinct row groups and emits a partial "
							 "aggregate a core Finalize combines.",
							 NULL,
							 &pgcolumnar_enable_parallel_vector_agg,
							 false,
							 PGC_USERSET,
							 0,
							 NULL, NULL, NULL);

	DefineCustomIntVariable("pgcolumnar.groupagg_max_groups",
							"Cap on the actual group count the grouped vectorized "
							"aggregate builds before it stops with an error.",
							"Enforced at execution against the real number of groups, "
							"not the planner's estimate: over the cap the query errors "
							"rather than falling back, since the plan is fixed by then. "
							"Raise it, or turn off pgcolumnar.enable_group_vectorization.",
							&pgcolumnar_groupagg_max_groups,
							1000000, 1, INT_MAX,
							PGC_USERSET,
							0,
							NULL, NULL, NULL);

	DefineCustomBoolVariable("pgcolumnar.enable_bloom_filter",
							 "Skip chunk groups on equality using per-chunk bloom filters.",
							 NULL,
							 &pgcolumnar_enable_bloom_filter,
							 true,
							 PGC_USERSET,
							 0,
							 NULL, NULL, NULL);

	DefineCustomBoolVariable("pgcolumnar.reclaim_coalesce",
							 "Split oversized freed ranges on reuse and coalesce "
							 "adjacent freed ranges, so compaction reclaims space "
							 "under fragmentation. Off reverts to whole-range reuse.",
							 NULL,
							 &pgcolumnar_reclaim_coalesce,
							 true,
							 PGC_USERSET,
							 0,
							 NULL, NULL, NULL);

	DefineCustomBoolVariable("pgcolumnar.enable_end_truncation",
							 "Allow pgcolumnar.truncate() to physically return "
							 "trailing reclaimed blocks to the OS. Off (the default) "
							 "makes truncate() a no-op.",
							 NULL,
							 &pgcolumnar_enable_end_truncation,
							 false,
							 PGC_SUSET,
							 0,
							 NULL, NULL, NULL);

	DefineCustomBoolVariable("pgcolumnar.enable_read_stream",
							 "Prefetch block reads with the read stream API (PostgreSQL 17+).",
							 NULL,
							 &pgcolumnar_enable_read_stream,
							 true,
							 PGC_USERSET,
							 0,
							 NULL, NULL, NULL);

	DefineCustomBoolVariable("pgcolumnar.enable_index_only_scan",
							 "Allow index-only scans on columnar tables, served by the "
							 "visibility-map fork (gap 28). On by default; set off to force "
							 "a plain index scan.",
							 NULL,
							 &pgcolumnar_enable_index_only_scan,
							 true,
							 PGC_USERSET,
							 0,
							 NULL, NULL, NULL);

	DefineCustomBoolVariable("pgcolumnar.enable_projection_scan",
							 "Let the planner scan a covering projection instead of the "
							 "base table when one serves the query better (gap 26).",
							 NULL,
							 &pgcolumnar_enable_projection_scan,
							 true,
							 PGC_USERSET,
							 0,
							 NULL, NULL, NULL);

	DefineCustomBoolVariable("pgcolumnar.enable_sorted_pathkeys",
							 "Let the columnar scan advertise the order a sorted rewrite "
							 "left the rows in, so ORDER BY on that key needs no Sort (#751).",
							 "Only a lexicographic run recorded by pgcolumnar.vacuum_sorted "
							 "and covering every row group is advertised. Off restores the "
							 "behaviour of always planning a Sort.",
							 &pgcolumnar_enable_sorted_pathkeys,
							 true,
							 PGC_USERSET,
							 0,
							 NULL, NULL, NULL);

	DefineCustomBoolVariable("pgcolumnar.enable_index_fetch_penalty",
							 "Add the cost of the row-group decodes a columnar index "
							 "scan's per-row heap fetches force (#355).",
							 "A columnar heap fetch decodes the whole row group the row "
							 "lives in, so an unclustered ordered index scan can cost far "
							 "more than core's per-page estimate. Off restores the "
							 "unpenalized planner behaviour.",
							 &pgcolumnar_enable_index_fetch_penalty,
							 true,
							 PGC_USERSET,
							 0,
							 NULL, NULL, NULL);

	DefineCustomBoolVariable("pgcolumnar.bulk_parallel_writer",
							 "Internal. Set by pgcolumnar.parallel_copy loader workers "
							 "so they skip the storage-row creation lock when the row "
							 "already exists committed, allowing concurrent atomic writers "
							 "to one table (#300). Safe even if set by hand: the skip fires "
							 "only when the storage row is already committed, which is exactly "
							 "when the creation lock guards nothing, so an ordinary write still "
							 "behaves correctly. Off by default leaves the write path unchanged.",
							 NULL,
							 &pgcolumnar_bulk_parallel_writer,
							 false,
							 PGC_USERSET,
							 GUC_NOT_IN_SAMPLE,
							 NULL, NULL, NULL);

	DefineCustomBoolVariable("pgcolumnar.parallel_flush",
							 "Dispatch the per-column stripe flush across background "
							 "workers (#445).",
							 "Off by default, and stays that way (#445 slice 4 measured "
							 "it). When on, a stripe flush of two or more columns fans "
							 "the per-column encode/compress work out to a pool of "
							 "background workers, degrading to serial in-backend "
							 "completion for any column a worker does not finish, so the "
							 "stored bytes are byte-identical to the serial path either "
							 "way. It is a targeted opt-in, not a general win: it helps a "
							 "single large flush of many cheap (numeric) columns -- a "
							 "wide bulk load -- by up to ~14%, and it REGRESSES the common "
							 "cases, because it copies the buffered data through shared "
							 "memory: small or frequent flushes (a low stripe_row_limit, "
							 "or per-row commits) pay the fixed worker-spawn cost (3.6x "
							 "slower measured), and text-heavy or very large flushes pay "
							 "an O(bytes) serialization cost that outweighs the parallel "
							 "saving. Turn it on only for a wide-numeric bulk load, off "
							 "otherwise.",
							 &pgcolumnar_parallel_flush,
							 false,
							 PGC_USERSET,
							 0,
							 NULL, NULL, NULL);

	DefineCustomBoolVariable("pgcolumnar.enable_unique_insert_lock",
							 "Serialize concurrent inserts of the same unique key.",
							 "Takes a transaction-scoped advisory lock per unique "
							 "index key so overlapping same-key inserts conflict "
							 "correctly (issue #5). Turning it off restores the "
							 "prior racy behavior.",
							 &pgcolumnar_enable_unique_lock,
							 true,
							 PGC_USERSET,
							 0,
							 NULL, NULL, NULL);

	/*
	 * The endpoint allow-list (#393, owner decision 2026-08-13): empty (the
	 * default) refuses every remote endpoint, so pg_read_server_files is not
	 * an SSRF primitive out of the box. SUSET is load-bearing: a USERSET list
	 * would let any role widen its own allow-list, which is the privilege the
	 * list exists to withhold. The objstore module enforces it at connect
	 * time via GetConfigOption; link-local ranges are refused there
	 * unconditionally, list or no list.
	 */
	DefineCustomStringVariable("pgcolumnar.objstore_allowed_endpoints",
							   "Remote endpoints the object-store module may connect to.",
							   "Comma-separated host or host:port entries; empty refuses all remote access.",
							   &pgcolumnar_objstore_allowed_endpoints,
							   "",
							   PGC_SUSET,
							   GUC_LIST_INPUT,
							   NULL, NULL, NULL);

	/*
	 * S3 addressing style (#621), read by the object-store module via
	 * GetConfigOption. "path" (the default) sends s3://bucket/key to
	 * endpoint/bucket/key; "virtual" sends it to bucket.endpoint/key, which is
	 * what AWS now prefers. Any value other than "virtual" is treated as path.
	 */
	DefineCustomStringVariable("pgcolumnar.objstore_s3_addressing",
							   "S3 request addressing style: 'path' or 'virtual'.",
							   "Virtual-host puts the bucket in the hostname (bucket.endpoint); path keeps it as the first path segment.",
							   &pgcolumnar_objstore_s3_addressing,
							   "path",
							   PGC_USERSET,
							   0,
							   NULL, NULL, NULL);

	/*
	 * Dev fault injection for the export sink (#394): fail the sink, as
	 * ENOSPC would, once the export has written this many bytes. Exists so
	 * the export failure paths (error, no partial file at the final name, no
	 * temp debris) are testable deterministically; the suite chooses values
	 * that land inside write_record_batch, the helper the 13-site census
	 * missed.
	 */
	/*
	 * Multipart part size for remote export (#394). Default 0 means the
	 * module's built-in 8 MiB; a small value forces the multipart path on a
	 * small fixture. Read by the objstore module via GetConfigOption; defined
	 * here because the module loads after configuration parsing.
	 */
	DefineCustomIntVariable("pgcolumnar.objstore_part_size",
							"Multipart part size in bytes for remote export (0 = module default 8 MiB).",
							NULL,
							&pgcolumnar_objstore_part_size,
							0,
							0, INT_MAX,
							PGC_USERSET,
							0,
							NULL, NULL, NULL);

	DefineCustomIntVariable("pgcolumnar.sink_fail_after",
							"Fail export writes after this many bytes (dev fault injection).",
							"-1 disables. The failure takes the same path a full disk takes.",
							&pgcolumnar_sink_fail_after,
							-1,
							-1, INT_MAX,
							PGC_USERSET,
							0,
							NULL, NULL, NULL);

	DefineCustomIntVariable("pgcolumnar.unique_lock_buckets",
							"Advisory-lock buckets per unique index for same-key "
							"insert serialization.",
							"Bounds the transaction's held advisory locks to at "
							"most this many per unique index. Equal keys always "
							"share a bucket; unrelated keys may share one, which "
							"only over-serializes. Fixed at server start because "
							"the bucket is part of the advisory lock tag: two "
							"backends inserting the same key must compute the "
							"same bucket, which they only do when they agree on "
							"this value.",
							&pgcolumnar_unique_lock_buckets,
							128,
							1, 1048576,
							PGC_POSTMASTER,
							0,
							NULL, NULL, NULL);

	DefineCustomBoolVariable("pgcolumnar.enable_row_update_lock",
							 "Serialize concurrent UPDATE/DELETE of the same "
							 "columnar row so the losing writer gets a retryable "
							 "serialization_failure instead of duplicating the "
							 "row and losing an update (issue #5).",
							 "On by default. Off restores the prior behavior, "
							 "where two transactions updating the same row can "
							 "each keep their own version.",
							 &pgcolumnar_enable_row_update_lock,
							 true,
							 PGC_USERSET,
							 0,
							 NULL, NULL, NULL);

	DefineCustomIntVariable("pgcolumnar.row_lock_buckets",
							"Advisory-lock buckets per storage for same-row "
							"UPDATE/DELETE serialization.",
							"Bounds the transaction's held row locks to at most "
							"this many per storage, so a bulk UPDATE cannot "
							"exhaust the lock table. The same row always hashes to "
							"the same bucket; unrelated rows may share one, which "
							"only over-serializes. Fixed at server start because "
							"the bucket is part of the advisory lock tag: two "
							"backends racing the same row must compute the same "
							"bucket, which they only do when they agree on this "
							"value.",
							&pgcolumnar_row_lock_buckets,
							1024,
							1, 1048576,
							PGC_POSTMASTER,
							0,
							NULL, NULL, NULL);

	DefineCustomBoolVariable("pgcolumnar.autovacuum",
							 "Run the pgColumnar maintenance daemon (compact_rewrite / recluster).",
							 "Off by default. When on it runs ONLY the online ShareUpdateExclusiveLock "
							 "verbs, and yields (like autovacuum) to any statement needing a stronger lock.",
							 &pgcolumnar_autovacuum,
							 false,
							 PGC_SIGHUP,
							 0,
							 NULL, NULL, NULL);

	DefineCustomIntVariable("pgcolumnar.autovacuum_naptime",
							"Seconds the maintenance launcher sleeps between sweeps.",
							NULL,
							&pgcolumnar_autovacuum_naptime,
							60, 1, 86400,
							PGC_SIGHUP,
							GUC_UNIT_S,
							NULL, NULL, NULL);

	DefineCustomRealVariable("pgcolumnar.autovacuum_compact_threshold",
							 "Deleted fraction at or above which the daemon compact_rewrites a table.",
							 NULL,
							 &pgcolumnar_autovacuum_compact_threshold,
							 0.2, 0.0, 1.0,
							 PGC_SIGHUP,
							 0,
							 NULL, NULL, NULL);

	DefineCustomRealVariable("pgcolumnar.autovacuum_recluster_threshold",
							 "Appended fraction at or above which the daemon reclusters a table.",
							 NULL,
							 &pgcolumnar_autovacuum_recluster_threshold,
							 0.05, 0.0, 1.0,
							 PGC_SIGHUP,
							 0,
							 NULL, NULL, NULL);

	DefineCustomIntVariable("pgcolumnar.maintenance_hold_ms",
							"Dev/test: hold ShareUpdateExclusiveLock this many ms inside a "
							"maintenance verb, interruptibly.",
							"0 disables. Exists so the autovacuum-yield behaviour is testable "
							"deterministically: the verb waits with the lock held, so a session "
							"needing a stronger lock can observe the daemon cancel.",
							&pgcolumnar_maintenance_hold_ms,
							0, 0, 600000,
							PGC_USERSET,
							GUC_UNIT_MS,
							NULL, NULL, NULL);

	MarkGUCPrefixReserved("pgcolumnar");

	/*
	 * The maintenance daemon launcher (#415). Registered only under
	 * shared_preload_libraries, which pgColumnar already requires; a server
	 * without preload simply loses the daemon.
	 */
	if (process_shared_preload_libraries_in_progress)
		PgColumnarAutovacuumRegister();

	RegisterXactCallback(pgcolumnar_xact_callback, NULL);
	RegisterSubXactCallback(pgcolumnar_subxact_callback, NULL);

	prev_object_access_hook = object_access_hook;
	object_access_hook = pgcolumnar_object_access;

	prev_process_utility_hook = ProcessUtility_hook;
	ProcessUtility_hook = pgcolumnar_process_utility;

	prev_executor_end_hook = ExecutorEnd_hook;
	ExecutorEnd_hook = pgcolumnar_executor_end;

	/*
	 * Forbid index-only scans on columnar tables. PG19 replaced
	 * get_relation_info_hook with build_simple_rel_hook; both fire right after
	 * get_relation_info has built the base relation's index list.
	 */
#if PG_VERSION_NUM >= 190000
	prev_build_simple_rel_hook = build_simple_rel_hook;
	build_simple_rel_hook = pgcolumnar_build_simple_rel;
#else
	prev_get_relation_info_hook = get_relation_info_hook;
	get_relation_info_hook = pgcolumnar_get_relation_info;
#endif

	/* register the custom scan provider and install the pathlist hook */
	PgColumnarCustomScanInit();

	/* install the vectorized-aggregate upper-path hook (spec 9) */
	PgColumnarVectorInit();

	/* register the unique-index cache invalidation callback (issue #5) */
	PgColumnarUniqueInit();
}
