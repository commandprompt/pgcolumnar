/*-------------------------------------------------------------------------
 *
 * pgcolumnar_write_state.c
 *		The columnar writer: batch rows into chunk groups and stripes, and
 *		flush a stripe (data pages + catalog rows) when it fills or at
 *		transaction pre-commit (spec 4, 9).
 *
 * Pending writes are held per relation for the life of the transaction. On
 * commit they are flushed at pre-commit; on abort they are discarded with
 * the transaction memory.
 *
 *-------------------------------------------------------------------------
 */
#include "columnar.h"
#include "columnar_encdesc.h"

#include "columnar_metadata.h"
#include "columnar_storage.h"
#include "columnar_write_state.h"
#include "columnar_compat.h"
#include "access/htup_details.h"
#include "access/table.h"
#include "access/xact.h"
#include "storage/procarray.h"
#include "catalog/pg_type.h"
#include "executor/tuptable.h"
#include "miscadmin.h"
#include "libpq/pqsignal.h"
#include "port/atomics.h"
#include "postmaster/bgworker.h"
#include "storage/dsm.h"
#include "storage/ipc.h"
#include "storage/lmgr.h"
#include "storage/shm_toc.h"
#include "tcop/tcopprot.h"
#include "utils/builtins.h"
#include "utils/datum.h"
#include "utils/fmgroids.h"
#include "utils/fmgrprotos.h"
#include "utils/guc.h"
#include "utils/inval.h"
#include "utils/lsyscache.h"
#include "utils/memutils.h"
#include "utils/rel.h"
#include "utils/snapmgr.h"
#include "utils/typcache.h"

/*
 * The encoding-descriptor entry and header lengths must equal the field widths
 * columnar_encdesc.h packs and columnar_reader.c parses. If a field is added or
 * a width changed without updating these, a reader stride lands mid-field; pin it
 * at compile time so the mismatch is a build error, not a DATA_CORRUPTED at read.
 */
StaticAssertDecl(COLUMNAR_NATIVE_ENCDESC_ENTRY_LEN == 1 + 3 * (int) sizeof(uint32),
				 "encdesc entry length drifted from its field widths");
StaticAssertDecl(COLUMNAR_NATIVE_ENCDESC_HEADER_LEN == 2 + (int) sizeof(uint32),
				 "encdesc header length drifted from its field widths");

#ifndef DSM_HANDLE_INVALID
#define DSM_HANDLE_INVALID 0
#endif

/* -------------------------------------------------------------------------
 * #445 slice 3: dispatch the per-column flush across background workers.
 *
 * One input DSM segment carries every column's flush_one_column input (the
 * slice-2 serialize_column_input blobs) plus the shared FSST verdict cache;
 * N workers claim columns off an atomic counter, run flush_one_column, and
 * publish their results in a per-worker OUTPUT segment whose handle they store
 * in their slot. The backend waits, collects every DONE worker's columns, and
 * completes any column a worker did not finish serially in-process, so the set
 * of columns written is always exactly [0, natts) -- slot starvation or a
 * worker error degrades to the serial path, never to a wrong row count.
 * ------------------------------------------------------------------------- */
#define PFLUSH_MAGIC		0x50464c53	/* 'PFLS' */
#define PFLUSH_KEY_HEADER	0
#define PFLUSH_KEY_INPUTS	1
#define PFLUSH_KEY_INOFFS	2
#define PFLUSH_KEY_VERDICTS 3
#define PFLUSH_KEY_SLOTS	4
#define PFLUSH_KEY_CLAIM	5
#define PFLUSH_MAX_WORKERS	8

typedef enum PflushState
{
	PFLUSH_PENDING = 0,			/* not finished (also: worker never started) */
	PFLUSH_DONE,				/* columns written; outHandle is valid */
	PFLUSH_FAILED				/* worker caught an error; see errmsg */
} PflushState;

typedef struct PflushWorkerSlot
{
	pg_atomic_uint32 state;		/* PflushState */
	dsm_handle	outHandle;		/* this worker's OUTPUT segment (valid on DONE) */
	uint32		outLen;			/* its byte length */
	int			sqlerrcode;
	char		errmsg[512];
} PflushWorkerSlot;

typedef struct PflushHeader
{
	Oid			dbid;
	Oid			roleid;
	Oid			relid;
	uint64		storageId;
	uint64		groupNumber;
	uint64		rowCount;
	int			validityBytes;
	int			encodeEffort;
	int			compressionType;
	int			compressionLevel;
	int			natts;
	int			nworkers;
	bool		bloomEnabled;

	/*
	 * Byte-identity: these encoding GUCs are read live inside flush_one_column
	 * (and its encoder), not captured into the write state, so a worker -- a
	 * fresh backend that never saw the launching session's SET -- would encode
	 * different bytes than the serial path under a non-default session value.
	 * Thread the launcher's live values so the worker reproduces them exactly.
	 */
	int			fsstVerdictReuse;
	int			fsstMinGainPercent;
	int			encodingSampleRows;
} PflushHeader;

/* KEY_VERDICTS: the FSST verdict cache, one seed per column (#472). */
typedef struct PflushVerdict
{
	int8		verdict;
	int32		age;
} PflushVerdict;

/*
 * How many bytes of a column chunk to run through both candidates when deciding
 * whether FSST still pays once the block compressor has had its turn.
 *
 * Bounded because the buffer is a copy of bytes already resident. Set from
 * measurement rather than taste: on four text shapes at 300,000 rows, deciding
 * over 256 kB (the table's training sample) gets one of them backwards, 1 MB
 * gets all four right but lands 4% off the best size on one, and 4 MB matches
 * the answer a whole-chunk decision gives on every shape. Four shapes is what
 * this was calibrated on, so treat it as a floor that worked rather than a
 * tuned optimum -- if a shape is found where it decides wrong, raise it.
 *
 * It trades write time for that accuracy, because the bytes judged here are
 * FSST-encoded once to judge them and again per vector when the answer is yes.
 * Medians of three at 300,000 rows, against main: 4 MB costs 12% on 's' || g
 * and 63% on the e-mail shape while matching main's size on both; 1 MB costs
 * nothing measurable on either but writes the e-mail shape 4% larger. Both are
 * roughly 4x faster than main on high-entropy text, which is where the encoding
 * was pure waste. Size is the guarantee worth keeping for a storage format, so
 * the larger value is the default; lowering it here is the throughput trade.
 */
#define COLUMNAR_FSST_DECIDE_CAP (4 * 1024 * 1024)

/*
 * Direct comparison kinds for the zone min/max (issue #155).
 *
 * Tracking a chunk's min and max costs two comparisons per value per column, and
 * routing each through fmgr is most of what they cost: removing min/max tracking
 * entirely saved 15% of a five-int-column load, where the comparison itself is a
 * subtraction. These are the types whose stored Datum can be read as a C value
 * and compared without a collation. Anything else keeps the fmgr path.
 *
 * float4 and float8 are deliberately NOT here, and the reason is worth keeping
 * whatever else changes. btree float ordering puts NaN above every other value, which a C
 * comparison gets wrong: every comparison against NaN is false, so NaN would read
 * as equal and never become a chunk's maximum. A zone map with a maximum that is
 * too low makes the reader skip a row group that does hold matching rows, and the
 * query silently returns fewer rows. A path that compares to answer one
 * predicate could take the shortcut, because it does not build stored bounds
 * that a later scan trusts; this one does, so it cannot.
 */
typedef enum PgColumnarFastCmp
{
	COLUMNAR_FASTCMP_NONE = 0,
	COLUMNAR_FASTCMP_I16,
	COLUMNAR_FASTCMP_I32,
	COLUMNAR_FASTCMP_I64
} PgColumnarFastCmp;

/* per-column, per-write-state facts needed for the min/max skip list */
typedef struct PgColumnarColumnDef
{
	bool		orderable;		/* type has a default btree comparison proc */
	FmgrInfo	cmpFn;			/* the comparison proc, when orderable */
	Oid			collation;		/* collation to compare under */
	PgColumnarFastCmp fastCmp;	/* direct comparison, when the type allows */

	/*
	 * int2/int4 column: its exact sum fits an int64 accumulator, so the zone map
	 * carries a per-vector and per-chunk sum for the zone-map-only aggregate (D5).
	 * Other summable types (int8, numeric, float) are left with a null zone sum.
	 */
	bool		summableInt;

	/*
	 * Bloom-filter support (I7): hashable and non-collatable, so a value's hash
	 * is collation-independent and a probe of the same type is consistent. Only
	 * such columns accumulate hashes for a per-chunk bloom filter.
	 */
	bool		bloomable;
	FmgrInfo	hashFn;
	Oid			hashCollation;	/* collation to hash under (InvalidOid if none) */

	/*
	 * Cached FSST keep/drop verdict and how many row groups have reused it
	 * (#472). COLUMNAR_FSST_UNKNOWN is the palloc0 default, so a fresh write
	 * state always asks once. The cache lives exactly as long as the write
	 * state, which is one statement: nothing is persisted and no on-disk
	 * structure changes.
	 */
	int8		fsstVerdict;
	int			fsstVerdictAge;
} PgColumnarColumnDef;

/* one column's two streams within one chunk group */
typedef struct ColumnChunkBuffer
{
	StringInfoData valueStream;
	StringInfoData existsStream;
	uint64		valueCount;

	/* running min/max of the non-null values seen in this chunk */
	bool		hasMinMax;
	Datum		minValue;		/* held in the stripe context */
	Datum		maxValue;

	/* running exact sum of non-null int2/int4 values (zone map, D5) */
	int64		sum;

	/* accumulated 4-byte value hashes for the per-chunk bloom filter (I7) */
	StringInfoData hashBuf;

	/*
	 * Byte offset into valueStream where each row's value begins, indexed by the
	 * row's position within the chunk group. A null row records the offset it
	 * would have had, so the lookup needs no null accounting.
	 *
	 * NULL until the first buffered fetch of this chunk group builds it, and
	 * maintained on append after that. A load that never fetches never builds it
	 * and pays nothing, which is deliberate: the insert path is what #155 is
	 * about, and this exists only for the fetch path (#212).
	 */
	uint32	   *valOffsets;
	uint64		valOffsetsLen;
	uint64		valOffsetsCap;
} ColumnChunkBuffer;

/* one chunk group: all columns for a horizontal slice of rows */
typedef struct ChunkGroupBuffer
{
	uint64		rowCount;
	ColumnChunkBuffer *columns;		/* array [natts] */
} ChunkGroupBuffer;

struct PgColumnarWriteState
{
	Oid			relid;
	SubTransactionId subid;			/* subtransaction that owns the buffer */
	TupleDesc	tupdesc;			/* copy owned by writeContext */
	int			natts;

	/*
	 * True when `tupdesc` is relid's on-disk descriptor, so a #445-slice-3
	 * parallel-flush worker may rebuild each column's Form_pg_attribute from
	 * table_open(relid). Set only by the base writer; false (fail-closed) for a
	 * projection's inner writer, whose tupdesc is a synthetic {rownumber, proj
	 * cols...} descriptor that does NOT match its base-table relid -- those flush
	 * on the serial path, which is byte-identical.
	 */
	bool		tupdescIsRel;
	int			stripeRowLimit;
	int			chunkGroupRowLimit;
	int			compressionType;	/* columnar.compression at open time */
	int			compressionLevel;	/* columnar.compression_level at open time */
	bool		bloomEnabled;		/* columnar.enable_bloom_filter at open time */
	int			encodeEffort;		/* per-table encode_effort at open time */
	uint64		storageId;
	PgColumnarColumnDef *colDefs;		/* array [natts], in writeContext */

	MemoryContext writeContext;		/* lives for the transaction */
	MemoryContext stripeContext;	/* reset after each stripe flush */

	List	   *chunkGroups;		/* list of ChunkGroupBuffer* */
	ChunkGroupBuffer *currentGroup;
	uint64		stripeRowCount;

	/*
	 * Reservation for the stripe currently being buffered (spec 2.2, 6). The
	 * stripe id and the first row number are reserved eagerly, when the first
	 * row of a stripe is buffered, so every row has a stable row number (and
	 * item pointer) at insert time for indexing. haveReservation is false
	 * between stripes; the file offset is reserved separately at flush.
	 */
	bool		haveReservation;
	uint64		stripeId;
	uint64		stripeFirstRowNumber;

	/*
	 * Every stripe id this write state has reserved, appended as it is reserved
	 * (issue #311). The online reclustering path needs to know which row groups
	 * it wrote, and no property of the group tells it: a concurrent inserter's
	 * group takes a number above the retired ones exactly as the rewrite's own
	 * output does, and its row numbers can fall inside the rewrite's range,
	 * because reservations interleave. Recording the reservations is the only
	 * exact answer. One append per stripe, against the flush that follows it.
	 *
	 * A plain array of uint64 rather than a List: a stripe id is 64 bits and
	 * lappend_int would truncate it on a storage that has outlived INT_MAX
	 * stripes.
	 */
	uint64	   *reservedStripeIds;
	int			nReservedStripeIds;
	int			reservedStripeIdsSize;

	/*
	 * Phase 2 (gap 26): additional projections fanned out from this relation's
	 * inserts. projWriters hangs off the base write state so it shares the
	 * (relid, subid) lifecycle -- flush, discard, subxact abort/promote all
	 * follow the base automatically. projInited guards the one-time catalog
	 * lookup that builds the list.
	 */
	bool		projInited;
	List	   *projWriters;	/* list of PgColumnarProjWriter * */
};

/* per-backend registry of pending write states, in PgColumnarWriteContext */
static MemoryContext PgColumnarWriteContext = NULL;
static List *PgColumnarWriteStates = NIL;

/*
 * #445 slice 3 GUC: dispatch the per-column flush across background workers.
 * Off by default so the merged behaviour is unchanged; the parallel path is
 * opt-in for testing (slice 4 makes it the measured, eventually-default
 * control). Externed in columnar.h; the DefineCustomBoolVariable lives with the
 * other GUCs in columnar_tableam.c.
 */
bool		pgcolumnar_parallel_flush = false;

static void pgcolumnar_flush_row_group(PgColumnarWriteState *writeState);
static void flush_ws_projections(PgColumnarWriteState *writeState);
static ChunkGroupBuffer *pgcolumnar_start_chunk_group(PgColumnarWriteState *writeState);
static uint64 *grow_uint64_array(uint64 *arr, int oldSize, int newSize);
static void pgcolumnar_init_col_defs(PgColumnarWriteState *writeState);
static void build_column_def(Form_pg_attribute att, bool bloomEnabled,
							 MemoryContext cxt, PgColumnarColumnDef *def);

/* bgworker entry: found by name via RegisterDynamicBackgroundWorker (#445 slice 3) */
PGDLLEXPORT void pgcolumnar_parallel_flush_worker(Datum main_arg);

/*
 * pgcolumnar_cmp_value
 *		Compare two values of a column under its ordering, taking the direct
 *		route when the type allows one and fmgr otherwise. The integer kinds
 *		reproduce their btree comparison exactly, so which route is taken can
 *		never change the answer.
 */
static inline int32
pgcolumnar_cmp_value(PgColumnarColumnDef *def, Datum a, Datum b)
{
	switch (def->fastCmp)
	{
		case COLUMNAR_FASTCMP_I16:
			{
				int16		x = DatumGetInt16(a);
				int16		y = DatumGetInt16(b);

				return (x < y) ? -1 : (x > y) ? 1 : 0;
			}
		case COLUMNAR_FASTCMP_I32:
			{
				int32		x = DatumGetInt32(a);
				int32		y = DatumGetInt32(b);

				return (x < y) ? -1 : (x > y) ? 1 : 0;
			}
		case COLUMNAR_FASTCMP_I64:
			{
				int64		x = DatumGetInt64(a);
				int64		y = DatumGetInt64(b);

				return (x < y) ? -1 : (x > y) ? 1 : 0;
			}
		case COLUMNAR_FASTCMP_NONE:
			break;
	}

	return DatumGetInt32(FunctionCall2Coll(&def->cmpFn, def->collation, a, b));
}

/*
 * build_column_def
 *		Resolve one column's skip metadata into *def: the btree comparison proc
 *		(for the per-chunk min/max skip list, spec 7.2) and the hash proc (for
 *		the per-chunk bloom filter, I7), plus the direct-comparison kind and the
 *		int2/int4 summable flag. fmgr procs are copied into `cxt` (the caller's
 *		long-lived context). A dropped column is left fully zero.
 *
 *		Extracted from pgcolumnar_init_col_defs (#445 slice 3) so the flush
 *		workers reconstruct a column's def with byte-for-byte the same logic the
 *		backend's write-state setup uses -- worker and backend must agree exactly
 *		or the stored bytes diverge. *def is zeroed first, so a caller may pass a
 *		stack def; the FSST verdict cache (fsstVerdict/Age) is seeded separately.
 */
static void
build_column_def(Form_pg_attribute att, bool bloomEnabled, MemoryContext cxt,
				 PgColumnarColumnDef *def)
{
	TypeCacheEntry *tce;

	memset(def, 0, sizeof(PgColumnarColumnDef));

	if (att->attisdropped)
		return;

	tce = lookup_type_cache(att->atttypid,
							TYPECACHE_CMP_PROC_FINFO |
							TYPECACHE_HASH_PROC_FINFO);
	if (OidIsValid(tce->cmp_proc_finfo.fn_oid))
	{
		def->orderable = true;
		fmgr_info_copy(&def->cmpFn, &tce->cmp_proc_finfo, cxt);
		def->collation = att->attcollation;

		/*
		 * Resolve a direct comparison where the type permits one. These
		 * compare byte-for-byte under any collation, so the fast path
		 * cannot disagree with the operator it replaces; the zone map it
		 * feeds is read back through the same ordering.
		 */
		switch (att->atttypid)
		{
			case INT2OID:
				def->fastCmp = COLUMNAR_FASTCMP_I16;
				break;
			case INT4OID:
			case DATEOID:
				def->fastCmp = COLUMNAR_FASTCMP_I32;
				break;
			case INT8OID:
			case TIMESTAMPOID:
			case TIMESTAMPTZOID:
				def->fastCmp = COLUMNAR_FASTCMP_I64;
				break;
			default:
				def->fastCmp = COLUMNAR_FASTCMP_NONE;
				break;
		}
	}

	/* int2/int4: exact sum fits int64, carried in the zone map (D5) */
	def->summableInt =
		(att->atttypid == INT2OID || att->atttypid == INT4OID);

	/*
	 * Bloom filter for hashable columns whose collation is safe (I7, gap
	 * 25): non-collatable types and deterministic collations, so a value
	 * hashes consistently between this build and an equality probe. A
	 * nondeterministic collation is left unbloomed.
	 */
	if (bloomEnabled &&
		OidIsValid(tce->hash_proc_finfo.fn_oid) &&
		PgColumnarCollationIsDeterministic(att->attcollation))
	{
		def->bloomable = true;
		fmgr_info_copy(&def->hashFn, &tce->hash_proc_finfo, cxt);
		def->hashCollation = att->attcollation;
	}
}

/*
 * pgcolumnar_init_col_defs
 *		Allocate and fill writeState->colDefs: for each column, resolve the btree
 *		comparison proc (for the per-chunk min/max skip list, spec 7.2) and the
 *		hash proc (for the per-chunk bloom filter, I7). Shared by the base writer
 *		and the projection writer so both carry skip metadata.
 */
static void
pgcolumnar_init_col_defs(PgColumnarWriteState *writeState)
{
	int			c;

	writeState->colDefs = palloc0(sizeof(PgColumnarColumnDef) * writeState->natts);

	for (c = 0; c < writeState->natts; c++)
	{
		Form_pg_attribute att = TupleDescAttr(writeState->tupdesc, c);

		build_column_def(att, writeState->bloomEnabled, PgColumnarWriteContext,
						 &writeState->colDefs[c]);
	}
}

/*
 * PgColumnarWriteStateStripeCount
 *		How many stripe reservations this write state has taken so far (#311).
 *
 *		A caller that wants to know which groups IT wrote records this before
 *		its writes and takes the tail afterwards, because PgColumnarGetWriteState
 *		can hand back a state that already holds another statement's entries.
 */
int
PgColumnarWriteStateStripeCount(PgColumnarWriteState *ws)
{
	return ws->nReservedStripeIds;
}

/*
 * PgColumnarWriteStateStripeIds
 *		The stripe ids this write state has reserved, in reservation order.
 *		Points at the write state's own array, which stays valid until the
 *		transaction ends.
 */
uint64 *
PgColumnarWriteStateStripeIds(PgColumnarWriteState *ws, int *n)
{
	*n = ws->nReservedStripeIds;
	return ws->reservedStripeIds;
}

/*
 * grow_uint64_array
 *		Enlarge (or first allocate) an array of uint64 in the current context.
 */
static uint64 *
grow_uint64_array(uint64 *arr, int oldSize, int newSize)
{
	if (arr == NULL)
		return (uint64 *) palloc(sizeof(uint64) * newSize);
	Assert(newSize > oldSize);
	return (uint64 *) repalloc(arr, sizeof(uint64) * newSize);
}

/*
 * PgColumnarGetWriteState
 *		Find or create the pending write state for a relation.
 */
PgColumnarWriteState *
PgColumnarGetWriteState(Relation rel)
{
	Oid			relid = RelationGetRelid(rel);
	SubTransactionId subid = GetCurrentSubTransactionId();
	ListCell   *lc;
	MemoryContext oldContext;
	PgColumnarWriteState *writeState;

	/*
	 * A write state is keyed by (relation, subtransaction) so that a buffer
	 * never mixes rows written under different subtransactions. That keeps the
	 * rollback of a subtransaction a simple matter of dropping its buffers
	 * (spec 9).
	 */
	foreach(lc, PgColumnarWriteStates)
	{
		writeState = (PgColumnarWriteState *) lfirst(lc);
		if (writeState->relid != relid || writeState->subid != subid)
			continue;

		/*
		 * The descriptor is snapshotted when the buffer is opened, so a buffer
		 * that predates an ALTER TABLE ... ADD COLUMN in this same transaction
		 * still has the old column count and silently drops every value written
		 * into the new column: the flushed chunks carry the old shape, and the
		 * reader then serves the column's missing value for those rows. Nothing
		 * else invalidates the buffer, since the write state registers no
		 * relcache callback.
		 *
		 * The buffered rows are correct under the descriptor they were written
		 * with, so flush them under it and open a new buffer for the new shape.
		 */
		if (writeState->natts == RelationGetDescr(rel)->natts)
			return writeState;

		if (writeState->stripeRowCount > 0)
			pgcolumnar_flush_row_group(writeState);
		flush_ws_projections(writeState);

		oldContext = MemoryContextSwitchTo(PgColumnarWriteContext);
		PgColumnarWriteStates = list_delete_ptr(PgColumnarWriteStates, writeState);
		MemoryContextSwitchTo(oldContext);
		break;
	}

	if (PgColumnarWriteContext == NULL)
		PgColumnarWriteContext = AllocSetContextCreate(TopTransactionContext,
													 "columnar write",
													 ALLOCSET_DEFAULT_SIZES);

	oldContext = MemoryContextSwitchTo(PgColumnarWriteContext);

	writeState = palloc0(sizeof(PgColumnarWriteState));
	writeState->relid = relid;
	writeState->subid = subid;
	/* CopyConstr (not CopyEntry) so attgenerated is preserved: the flush skips
	 * writing a chunk for virtual generated columns (attgenerated 'v'), which
	 * CreateTupleDescCopy would clear. */
	writeState->tupdesc = CreateTupleDescCopyConstr(RelationGetDescr(rel));
	writeState->natts = writeState->tupdesc->natts;
	writeState->tupdescIsRel = true;	/* rebuildable by a parallel-flush worker */
	writeState->stripeRowLimit = pgcolumnar_stripe_row_limit;
	writeState->chunkGroupRowLimit = pgcolumnar_chunk_group_row_limit;
	writeState->compressionType = pgcolumnar_compression;
	writeState->compressionLevel = pgcolumnar_compression_level;

	/*
	 * Whether to build bloom filters at all, captured here with the other
	 * write-time settings rather than consulted per row, so one stripe is
	 * written under one decision.
	 */
	writeState->bloomEnabled = pgcolumnar_enable_bloom_filter;
	writeState->encodeEffort = COLUMNAR_ENCODE_EFFORT_FULL;
	writeState->storageId = PgColumnarStorageId(rel);

	/*
	 * Per-table options (spec 7.4) override the instance-wide GUC defaults for
	 * this relation's writes. They are read at write-state creation, so a value
	 * set with pgcolumnar.set_options takes effect for subsequent
	 * inserts (spec 9).
	 */
	{
		PgColumnarOptions opts;

		if (PgColumnarReadOptions(relid, &opts))
		{
			if (opts.stripeRowLimitSet)
				writeState->stripeRowLimit = opts.stripeRowLimit;
			if (opts.chunkGroupRowLimitSet)
				writeState->chunkGroupRowLimit = opts.chunkGroupRowLimit;
			if (opts.compressionSet)
				writeState->compressionType = opts.compressionType;
			if (opts.compressionLevelSet)
				writeState->compressionLevel = opts.compressionLevel;
			if (opts.encodeEffortSet)
				writeState->encodeEffort = opts.encodeEffort;
		}
	}

	pgcolumnar_init_col_defs(writeState);

	writeState->stripeContext = AllocSetContextCreate(PgColumnarWriteContext,
													  "columnar stripe",
													  ALLOCSET_DEFAULT_SIZES);
	writeState->writeContext = PgColumnarWriteContext;
	writeState->chunkGroups = NIL;
	writeState->currentGroup = NULL;
	writeState->stripeRowCount = 0;
	writeState->haveReservation = false;
	writeState->stripeId = 0;
	writeState->stripeFirstRowNumber = 0;
	writeState->reservedStripeIds = NULL;
	writeState->nReservedStripeIds = 0;
	writeState->reservedStripeIdsSize = 0;

	PgColumnarWriteStates = lappend(PgColumnarWriteStates, writeState);

	MemoryContextSwitchTo(oldContext);

	return writeState;
}

/*
 * PgColumnarEnsureStorageRow
 *		Create the native storage catalog row for `rel` if it does not exist,
 *		with exactly the metadata a normal flush would record. Used by
 *		pgcolumnar.parallel_copy's coordinator to pre-create and commit the storage
 *		row before launching concurrent loaders, so each loader (with
 *		pgcolumnar_bulk_parallel_writer set) sees it committed and skips the
 *		storage-row creation lock. Idempotent -- PgColumnarInsertNativeStorageRow
 *		returns if the row already exists.
 */
void
PgColumnarEnsureStorageRow(Relation rel)
{
	NativeStorageMetadata s;
	int			stripeRowLimit = pgcolumnar_effective_stripe_row_limit(RelationGetRelid(rel));

	s.storageId = PgColumnarStorageId(rel);
	s.relationOid = RelationGetRelid(rel);
	s.formatVersion = COLUMNAR_NATIVE_VERSION_MAJOR;
	s.vectorLength = COLUMNAR_NATIVE_VECTOR_LENGTH;
	s.rowGroupLimit = stripeRowLimit;
	PgColumnarInsertNativeStorageRow(&s);
}

/*
 * buffered_note_offset
 *		Record where the next row's value begins, if this column is tracking
 *		offsets at all.
 *
 * Returns immediately when the array does not exist, which is the case for every
 * write that is never fetched from. The array is created only by
 * buffered_build_offsets, on the first buffered fetch of the chunk group.
 */
static void
buffered_note_offset(ColumnChunkBuffer *col, MemoryContext cxt, uint32 off)
{
	if (col->valOffsets == NULL)
		return;

	if (col->valOffsetsLen >= col->valOffsetsCap)
	{
		MemoryContext old = MemoryContextSwitchTo(cxt);

		col->valOffsetsCap *= 2;
		col->valOffsets = repalloc(col->valOffsets,
								   sizeof(uint32) * col->valOffsetsCap);
		MemoryContextSwitchTo(old);
	}
	col->valOffsets[col->valOffsetsLen++] = off;
}

/*
 * buffered_build_offsets
 *		Walk the column's value stream once and record where every row's value
 *		begins, so later fetches can address a row instead of walking to it.
 *
 * This is the O(n) pass that used to run on *every* fetch. Decoded values are
 * thrown away here, so they are built in a scratch context that is deleted at
 * the end rather than left in the stripe context.
 */
static void
buffered_build_offsets(ColumnChunkBuffer *col, Form_pg_attribute att,
					   MemoryContext cxt, uint64 rowCount)
{
	MemoryContext old;
	MemoryContext scratch;
	char	   *cursor = col->valueStream.data;
	uint64		i;

	old = MemoryContextSwitchTo(cxt);
	col->valOffsetsCap = Max(rowCount, 64);
	col->valOffsets = palloc(sizeof(uint32) * col->valOffsetsCap);
	col->valOffsetsLen = 0;
	MemoryContextSwitchTo(old);

	scratch = AllocSetContextCreate(CurrentMemoryContext,
									"columnar buffered offsets",
									ALLOCSET_SMALL_SIZES);

	for (i = 0; i < rowCount; i++)
	{
		/*
		 * Bounded by chunk_group_row_limit, which is user-settable, so this pass
		 * is a cancellation point for the same reason the decoders are. It is
		 * also on the unique-check fetch path, which had no interrupt check at
		 * all before #220 and still has none below pgcolumnar_fetch_row.
		 */
		CHECK_FOR_INTERRUPTS();

		col->valOffsets[col->valOffsetsLen++] =
			(uint32) (cursor - col->valueStream.data);

		if (col->existsStream.data[i])
			(void) PgColumnarDecodeValue(att, &cursor,
										 col->valueStream.data + col->valueStream.len,
										 scratch);
	}

	MemoryContextDelete(scratch);
}

/*
 * pgcolumnar_start_chunk_group
 *		Begin a new chunk group inside the current stripe, allocated in the
 *		stripe memory context.
 */
static ChunkGroupBuffer *
pgcolumnar_start_chunk_group(PgColumnarWriteState *writeState)
{
	MemoryContext oldContext = MemoryContextSwitchTo(writeState->stripeContext);
	ChunkGroupBuffer *group = palloc0(sizeof(ChunkGroupBuffer));
	int			c;

	group->rowCount = 0;
	group->columns = palloc0(sizeof(ColumnChunkBuffer) * writeState->natts);
	for (c = 0; c < writeState->natts; c++)
	{
		initStringInfo(&group->columns[c].valueStream);
		initStringInfo(&group->columns[c].existsStream);
		initStringInfo(&group->columns[c].hashBuf);
		group->columns[c].valueCount = 0;
	}

	writeState->chunkGroups = lappend(writeState->chunkGroups, group);
	writeState->currentGroup = group;

	MemoryContextSwitchTo(oldContext);
	return group;
}

/*
 * PgColumnarWriteRow
 *		Append one row to the current stripe, opening a new chunk group when
 *		the current one is full and flushing the stripe when it reaches the
 *		stripe row limit. Returns the stable 1-based row number assigned to the
 *		row (spec 6), so the caller can set the row's item pointer for indexing.
 */
uint64
PgColumnarWriteRow(PgColumnarWriteState *writeState, Relation rel,
				 Datum *values, bool *nulls)
{
	ChunkGroupBuffer *group = writeState->currentGroup;
	uint64		rowNumber;
	int			c;

	/*
	 * Reserve this stripe's id and row-number range when its first row is
	 * buffered (spec 2.2, 6). A whole stripe_row_limit worth of row numbers is
	 * reserved up front so the stripe's rows are numbered contiguously from
	 * stripeFirstRowNumber; the writer flushes at stripe_row_limit, so the run
	 * is never overrun. Any unused tail (a stripe flushed early) is a harmless
	 * gap in the row-number space.
	 */
	if (!writeState->haveReservation)
	{
		PgColumnarReserveRowNumbers(rel, (uint64) writeState->stripeRowLimit,
								  &writeState->stripeId,
								  &writeState->stripeFirstRowNumber);
		writeState->haveReservation = true;

		/*
		 * Record the reservation (#311). Allocated in writeContext, not
		 * stripeContext, because it must outlive the stripe flush that resets
		 * the latter.
		 */
		{
			MemoryContext old = MemoryContextSwitchTo(writeState->writeContext);

			if (writeState->nReservedStripeIds >= writeState->reservedStripeIdsSize)
			{
				int			newSize = writeState->reservedStripeIdsSize == 0
					? 16 : writeState->reservedStripeIdsSize * 2;

				writeState->reservedStripeIds =
					grow_uint64_array(writeState->reservedStripeIds,
									  writeState->reservedStripeIdsSize, newSize);
				writeState->reservedStripeIdsSize = newSize;
			}
			writeState->reservedStripeIds[writeState->nReservedStripeIds++] =
				writeState->stripeId;
			MemoryContextSwitchTo(old);
		}
	}

	rowNumber = writeState->stripeFirstRowNumber + writeState->stripeRowCount;

	if (group == NULL ||
		group->rowCount >= (uint64) writeState->chunkGroupRowLimit)
		group = pgcolumnar_start_chunk_group(writeState);

	for (c = 0; c < writeState->natts; c++)
	{
		ColumnChunkBuffer *col = &group->columns[c];
		Form_pg_attribute att = TupleDescAttr(writeState->tupdesc, c);

		/*
		 * Where this row's value starts, recorded before it is written. Only
		 * costs anything once a buffered fetch has built the array; see
		 * buffered_note_offset.
		 */
		buffered_note_offset(col, writeState->stripeContext,
							 (uint32) col->valueStream.len);

		if (nulls[c])
		{
			appendStringInfoChar(&col->existsStream, 0);
		}
		else
		{
			Datum		v = values[c];
			struct varlena *flat = NULL;

			/*
			 * Detoast a varlena ONCE per row and reuse the flattened Datum for
			 * everything below. It is otherwise flattened up to four times -- by
			 * the encoder, the bloom hash, and each of the two min/max
			 * comparisons -- and for a toasted (compressed) value each of those
			 * is a full decompression, which #445's profile saw as detoast_attr
			 * on the write path. pg_detoast_datum returns the same pointer for an
			 * already-flat value, so the uncompressed common case copies and
			 * frees nothing; non-varlena types skip it entirely.
			 */
			if (att->attlen == -1)
			{
				flat = pg_detoast_datum((struct varlena *) DatumGetPointer(v));
				v = PointerGetDatum(flat);
			}

			appendStringInfoChar(&col->existsStream, 1);
			PgColumnarEncodeValue(&col->valueStream, att, v);
			col->valueCount++;

			/* accumulate the value's hash for the per-chunk bloom filter (I7) */
			if (writeState->colDefs[c].bloomable)
			{
				uint32		h = DatumGetUInt32(
					FunctionCall1Coll(&writeState->colDefs[c].hashFn,
									  writeState->colDefs[c].hashCollation,
									  v));

				appendBinaryStringInfo(&col->hashBuf, (char *) &h, sizeof(uint32));
			}

			/* maintain the per-chunk exact int sum for the zone map (D5) */
			if (writeState->colDefs[c].summableInt)
				col->sum += (att->atttypid == INT2OID)
					? (int64) DatumGetInt16(v)
					: (int64) DatumGetInt32(v);

			/* maintain the per-chunk min/max for orderable types */
			if (writeState->colDefs[c].orderable)
			{
				PgColumnarColumnDef *def = &writeState->colDefs[c];
				MemoryContext oldContext =
					MemoryContextSwitchTo(writeState->stripeContext);

				if (!col->hasMinMax)
				{
					col->minValue = datumCopy(v, att->attbyval, att->attlen);
					col->maxValue = datumCopy(v, att->attbyval, att->attlen);
					col->hasMinMax = true;
				}
				else
				{
					/*
					 * A value above the running maximum cannot also be below the
					 * running minimum, so the second comparison is only needed
					 * when the first does not settle it. Testing the maximum
					 * first makes the ascending case -- which is what a bulk load
					 * of a serial or timestamp column produces -- cost one
					 * comparison per value instead of two.
					 */
					int32		cmpMax = pgcolumnar_cmp_value(def, v,
														   col->maxValue);

					if (cmpMax > 0)
					{
						if (!att->attbyval)
							pfree(DatumGetPointer(col->maxValue));
						col->maxValue = datumCopy(v, att->attbyval, att->attlen);
					}
					else if (pgcolumnar_cmp_value(def, v, col->minValue) < 0)
					{
						if (!att->attbyval)
							pfree(DatumGetPointer(col->minValue));
						col->minValue = datumCopy(v, att->attbyval, att->attlen);
					}
				}

				MemoryContextSwitchTo(oldContext);
			}

			/*
			 * Free the flattened copy if detoasting made one. Everything above
			 * has consumed it: the encoder and bloom copied the bytes out, and
			 * min/max datumCopy'd into the stripe context. Nothing was allocated
			 * when the value was already flat (flat == the original pointer).
			 */
			if (flat != NULL && (char *) flat != DatumGetPointer(values[c]))
				pfree(flat);
		}
	}

	group->rowCount++;
	writeState->stripeRowCount++;

	if (writeState->stripeRowCount >= (uint64) writeState->stripeRowLimit)
		pgcolumnar_flush_row_group(writeState);

	return rowNumber;
}

/*
 * PgColumnarBufferedRowByNumber
 *		Reconstruct a single row that is still held in an unflushed write buffer,
 *		addressed by its row number (spec 6). Returns true and fills values/nulls
 *		(by-reference values copied into the current memory context) when the row
 *		is present in a pending stripe buffer for this relation; false otherwise.
 *
 *		This lets an index fetch see rows written earlier in the same statement
 *		but not yet flushed, which is what makes a unique constraint reject two
 *		duplicate rows inserted by a single statement: the btree uniqueness check
 *		fetches the first row's item pointer while both rows are still buffered.
 *		It reads only process-local memory, so it acquires no locks and is safe
 *		to call while the caller holds an index buffer lock.
 */
bool
PgColumnarBufferedRowByNumber(Relation rel, uint64 rowNumber,
							Datum *values, bool *nulls)
{
	Oid			relid = RelationGetRelid(rel);
	MemoryContext target = CurrentMemoryContext;
	ListCell   *lc;

	foreach(lc, PgColumnarWriteStates)
	{
		PgColumnarWriteState *ws = (PgColumnarWriteState *) lfirst(lc);
		uint64		offset;
		uint64		accumulated;
		ListCell   *glc;

		if (ws->relid != relid || !ws->haveReservation)
			continue;
		if (rowNumber < ws->stripeFirstRowNumber ||
			rowNumber >= ws->stripeFirstRowNumber + ws->stripeRowCount)
			continue;

		offset = rowNumber - ws->stripeFirstRowNumber;

		accumulated = 0;
		foreach(glc, ws->chunkGroups)
		{
			ChunkGroupBuffer *group = (ChunkGroupBuffer *) lfirst(glc);
			uint64		posInGroup;
			int			c;

			if (offset >= accumulated + group->rowCount)
			{
				accumulated += group->rowCount;
				continue;
			}

			posInGroup = offset - accumulated;

			for (c = 0; c < ws->natts; c++)
			{
				ColumnChunkBuffer *col = &group->columns[c];
				Form_pg_attribute att = TupleDescAttr(ws->tupdesc, c);
				char	   *existsBytes = col->existsStream.data;
				char	   *cursor;

				/*
				 * Address the row rather than walking to it.
				 *
				 * This loop used to decode and discard every earlier value in
				 * the chunk group, for every column, on every fetch, purely to
				 * advance the cursor. _bt_check_unique() calls this once per
				 * candidate while a statement buffers rows, so the cost was
				 * O(rows x columns) per fetch and quadratic across a statement.
				 * That is what pinned a core for three days in #212.
				 *
				 * The offsets are built once per chunk group on the first fetch
				 * and maintained on append after that, so a load that never
				 * fetches still pays nothing.
				 */
				if (col->valOffsets == NULL ||
					col->valOffsetsLen < group->rowCount)
					buffered_build_offsets(col, att, ws->stripeContext,
										   group->rowCount);

				cursor = col->valueStream.data + col->valOffsets[posInGroup];

				if (existsBytes[posInGroup])
				{
					values[c] = PgColumnarDecodeValue(att, &cursor,
													  col->valueStream.data + col->valueStream.len,
													  target);
					nulls[c] = false;
				}
				else
				{
					values[c] = (Datum) 0;
					nulls[c] = true;
				}
			}

			return true;
		}
	}

	return false;
}

typedef struct FlushColumnResult
{
	StringInfo	chunk;			/* [validity][finalData] column-chunk bytes; NULL if skipped */
	char	   *descriptor;		/* encoding descriptor bytes; NULL if the column was skipped */
	uint32		descriptorLen;
	int			blockCodec;
	List	   *zoneRows;		/* NativeZoneMapMetadata * for this column (per-vector + whole-chunk) */
	NativeBloomMetadata *bloomRow;	/* per-chunk bloom for this column, or NULL */
} FlushColumnResult;

/*
 * flush_one_column
 *		Produce one column chunk's bytes, descriptor, block codec, zone maps and
 *		bloom from that column's buffered input, reading only its arguments (no
 *		writeState reach-through). The caller assembles the column chunks into the
 *		stripe in column order and performs all I/O and catalog writes. Output is
 *		byte-identical to the in-loop code this was extracted from (#445 slice 1).
 */
static FlushColumnResult
flush_one_column(Form_pg_attribute att, List *chunkGroups,
				 PgColumnarColumnDef *def, uint64 rowCount, int validityBytes,
				 int encodeEffort, int compressionType, int compressionLevel,
				 uint64 storageId, uint64 groupNumber, int columnIndex)
{
	FlushColumnResult result;
	List	   *zoneRows = NIL;
	NativeBloomMetadata *bloomRow = NULL;
	StringInfo	chunk = makeStringInfo();
	ListCell   *lc;
	uint8	   *validity = (uint8 *) palloc0(validityBytes);
	uint64		rowIdx = 0;
	StringInfo	encoded = makeStringInfo();
	StringInfo	desc = makeStringInfo();
	uint32		vectorCount = (uint32) list_length(chunkGroups);
	char	   *fsstTable = NULL;	/* chunk-shared FSST table (E3b), or NULL */
	uint32		fsstTableLen = 0;
	char	   *finalData;
	uint32		finalLen;
	int			blockCodec = COLUMNAR_COMPRESSION_NONE;
	int			vec = 0;
	bool		chunkHasMinMax = false;
	Datum		chunkMin = (Datum) 0;
	Datum		chunkMax = (Datum) 0;
	uint64		chunkValueCount = 0;
	int64		chunkSum = 0;

	/*
	 * Virtual generated columns (attgenerated 'v', PostgreSQL 18+) are computed
	 * on read from their base columns and never stored, so writing an all-null
	 * chunk for them wastes space. Skip the chunk entirely: the reader finds no
	 * column_chunk for this column, treats it as absent, and returns its missing
	 * value (NULL via getmissingattr), while the executor expands the generated
	 * expression regardless. A NULL descriptor marks the column skipped for the
	 * column_chunk insertion pass below. ('v' is never set on PG15-17.)
	 */
	if (att->attgenerated == 'v')
	{
		result.chunk = NULL;
		result.descriptor = NULL;
		result.descriptorLen = 0;
		result.blockCodec = COLUMNAR_COMPRESSION_NONE;
		result.zoneRows = NIL;
		result.bloomRow = NULL;
		pfree(validity);
		return result;
	}

	foreach(lc, chunkGroups)
	{
		ChunkGroupBuffer *group = (ChunkGroupBuffer *) lfirst(lc);
		ColumnChunkBuffer *col = &group->columns[columnIndex];
		char	   *existsBytes = col->existsStream.data;
		uint64		i;

		for (i = 0; i < group->rowCount; i++, rowIdx++)
			if (existsBytes[i])
				validity[rowIdx >> 3] |= (uint8) (1 << (rowIdx & 7));
	}
	appendBinaryStringInfo(chunk, (char *) validity, validityBytes);

	/* descriptor header (columnar_encdesc.h owns the wire layout) */
	PgColumnarEncdescPutHeader(desc, vectorCount);

	/*
	 * E3b: build one FSST symbol table for the whole column chunk from a
	 * bounded sample of its value streams, so the costly table build is paid
	 * once here rather than once per vector. It is stored once as a trailing
	 * descriptor region and reused by every FSST vector below. Non-varlena
	 * columns and columns FSST cannot help leave it NULL.
	 */
	if (att->attlen == -1)
	{
		StringInfoData corpus;
		uint32		sampleLen = 0;
		/* `def` is the enclosing block's, at the top of this per-column
		 * loop. Re-declaring it here shadowed that one, which this project
		 * builds with -Wshadow=compatible-local and treats as an error. */
		bool		reuseVerdict;

		initStringInfo(&corpus);
		foreach(lc, chunkGroups)
		{
			ChunkGroupBuffer *group = (ChunkGroupBuffer *) lfirst(lc);
			ColumnChunkBuffer *col = &group->columns[columnIndex];

			if (col->valueStream.len > 0)
				appendBinaryStringInfo(&corpus, col->valueStream.data,
									   col->valueStream.len);
			if (sampleLen == 0 && corpus.len >= 262144)
				sampleLen = (uint32) corpus.len;	/* matches FSST_SAMPLE_CAP:
													 * train the one per-chunk
													 * table on a broad sample */
			if (corpus.len >= COLUMNAR_FSST_DECIDE_CAP)
				break;
		}
		if (sampleLen == 0)
			sampleLen = (uint32) corpus.len;

		/*
		 * encode_effort = fast skips the FSST substring search entirely:
		 * no symbol table, so no whole-corpus decision below and no
		 * per-vector encode either, since all three are reached only
		 * through a non-NULL fsstTable.
		 *
		 * This is where a text column's write cost lives (issue #155).
		 * Measured on 1,000,000 rows, one text column, the load runs 1.2x
		 * to 5.7x faster without it -- and on five of the seven shapes
		 * measured it produced byte-for-byte identical storage, so that
		 * time bought nothing at all. On the two where FSST does win it
		 * costs 2.7% and 12.2% more space, which is why this is a choice
		 * offered rather than a default changed.
		 */
		/*
		 * Skip the FSST symbol-table build when a cheap distinct probe shows
		 * the dictionary wins outright (#155): the build is the single largest
		 * cost of a text load, and for a low-cardinality column the table is
		 * built and then never used per vector. The probe reads the same corpus
		 * the keep/drop decision uses, and only skips when the dictionary is
		 * viable and wins for every vector, so the stored bytes are identical.
		 */
		/*
		 * Reuse this column's previous verdict when it is young enough
		 * (#472). A HURTS verdict skips the build as well as the question,
		 * since the vectors then take their ordinary encoding, which is
		 * what a freshly taken HURTS would have produced.
		 */
		reuseVerdict = (pgcolumnar_fsst_verdict_reuse > 0 &&
						def->fsstVerdict != COLUMNAR_FSST_UNKNOWN &&
						def->fsstVerdictAge < pgcolumnar_fsst_verdict_reuse);

		if (corpus.len > 0 &&
			encodeEffort != COLUMNAR_ENCODE_EFFORT_FAST &&
			!(reuseVerdict && def->fsstVerdict == COLUMNAR_FSST_HURTS) &&
			!PgColumnarFsstDictWins(corpus.data, (uint32) corpus.len))
			PgColumnarFsstBuildChunkTable(corpus.data, sampleLen, att,
										&fsstTable, &fsstTableLen);

		/*
		 * A table that shrinks every vector can still enlarge the chunk,
		 * because what lands on disk is this stream after the codec below
		 * has run, and FSST codes compress far worse than the text they
		 * replace. Ask before committing to it, and drop the table when the
		 * answer is no: the vectors below then take their ordinary encoding
		 * and skip the FSST attempt altogether, so the check pays for itself
		 * in write time exactly when it saves space.
		 *
		 * This is asked over a much longer run of bytes than the table is
		 * trained on, because the answer moves with volume and the sample
		 * size is not neutral: zstd needs a good deal of FSST output before
		 * it finds the structure in it. Measured on 300,000 e-mail-shaped
		 * rows, the 256 kB training sample says FSST is 24% worse while over
		 * the whole column it is 23% better -- a verdict that is not merely
		 * imprecise but inverted, so no margin on the sample would be safe.
		 */
		if (fsstTable != NULL)
		{
			bool		helps;

			/*
			 * A reused HELPS verdict still builds the table above, because
			 * the table is trained on THIS row group's corpus and stored
			 * with the chunk: reusing the table itself would change the
			 * stored bytes. Only the whole-corpus question is skipped, and
			 * that is the expensive half.
			 */
			if (reuseVerdict)
			{
				helps = (def->fsstVerdict == COLUMNAR_FSST_HELPS);
				def->fsstVerdictAge++;
			}
			else
			{
				helps = PgColumnarFsstHelpsCompressed(corpus.data,
													(uint32) corpus.len,
													fsstTable, fsstTableLen,
													compressionType,
													compressionLevel);
				def->fsstVerdict = helps ? COLUMNAR_FSST_HELPS
					: COLUMNAR_FSST_HURTS;
				def->fsstVerdictAge = 0;
			}

			if (!helps)
			{
				pfree(fsstTable);
				fsstTable = NULL;
				fsstTableLen = 0;
			}
		}
		else if (reuseVerdict && def->fsstVerdict == COLUMNAR_FSST_HURTS)
		{
			/*
			 * The build was skipped on the strength of the cached verdict,
			 * so this row group counts as a reuse too. Without this the age
			 * would never advance on the common path and the bound would
			 * never re-take the verdict.
			 */
			def->fsstVerdictAge++;
		}

		pfree(corpus.data);
	}

	/* encode each vector (chunk group) and record its descriptor entry */
	foreach(lc, chunkGroups)
	{
		ChunkGroupBuffer *group = (ChunkGroupBuffer *) lfirst(lc);
		ColumnChunkBuffer *col = &group->columns[columnIndex];
		char	   *encData;
		uint32		encLen;
		int			encType;
		uint8		entryType;
		uint32		entryValueCount;
		uint32		entryRawLen;

		encType = PgColumnarEncodeChunk(col->valueStream.data,
									  col->valueStream.len, att,
									  col->valueCount, fsstTable, fsstTableLen,
									  &encData, &encLen);

		if (encLen > 0)
			appendBinaryStringInfo(encoded, encData, encLen);

		entryType = (uint8) encType;
		entryValueCount = (uint32) col->valueCount;
		entryRawLen = (uint32) col->valueStream.len;
		PgColumnarEncdescPutEntry(desc, entryType, entryValueCount,
								  entryRawLen, encLen);

		/* per-vector zone map (native spec 7.1, D5) */
		{
			NativeZoneMapMetadata *z = palloc0(sizeof(NativeZoneMapMetadata));

			z->storageId = storageId;
			z->groupNumber = groupNumber;
			z->columnIndex = columnIndex;
			z->vectorIndex = vec;
			z->valueCount = col->valueCount;
			z->nullCount = group->rowCount - col->valueCount;

			if (def->summableInt && col->valueCount > 0)
			{
				z->hasSum = true;
				z->sum = DirectFunctionCall1(int8_numeric,
											 Int64GetDatum(col->sum));
			}

			if (col->hasMinMax)
			{
				StringInfoData mn;
				StringInfoData mx;

				initStringInfo(&mn);
				initStringInfo(&mx);
				PgColumnarEncodeValue(&mn, att, col->minValue);
				PgColumnarEncodeValue(&mx, att, col->maxValue);
				z->hasMinMax = true;
				z->minimum = mn.data;
				z->minimumLen = (uint32) mn.len;
				z->maximum = mx.data;
				z->maximumLen = (uint32) mx.len;

				/* fold into the whole-chunk min/max via the btree cmp proc */
				if (!chunkHasMinMax)
				{
					chunkMin = col->minValue;
					chunkMax = col->maxValue;
					chunkHasMinMax = true;
				}
				else
				{
					if (DatumGetInt32(FunctionCall2Coll(&def->cmpFn,
														def->collation,
														col->minValue,
														chunkMin)) < 0)
						chunkMin = col->minValue;
					if (DatumGetInt32(FunctionCall2Coll(&def->cmpFn,
														def->collation,
														col->maxValue,
														chunkMax)) > 0)
						chunkMax = col->maxValue;
				}
			}

			chunkValueCount += col->valueCount;
			chunkSum += col->sum;
			zoneRows = lappend(zoneRows, z);
		}
		vec++;
	}

	/*
	 * E3b: trailing chunk-shared FSST table region (descriptor version 2).
	 * sharedTableLen is 0 when the chunk has no shared table; FSST vectors
	 * above reference this one table instead of embedding their own.
	 */
	appendBinaryStringInfo(desc, (char *) &fsstTableLen, sizeof(uint32));
	if (fsstTableLen > 0)
		appendBinaryStringInfo(desc, fsstTable, fsstTableLen);

	/* whole-chunk zone map (vector_index -1) */
	{
		NativeZoneMapMetadata *z = palloc0(sizeof(NativeZoneMapMetadata));

		z->storageId = storageId;
		z->groupNumber = groupNumber;
		z->columnIndex = columnIndex;
		z->vectorIndex = -1;
		z->valueCount = chunkValueCount;
		z->nullCount = rowCount - chunkValueCount;

		if (def->summableInt && chunkValueCount > 0)
		{
			z->hasSum = true;
			z->sum = DirectFunctionCall1(int8_numeric,
										 Int64GetDatum(chunkSum));
		}

		if (chunkHasMinMax)
		{
			StringInfoData mn;
			StringInfoData mx;

			initStringInfo(&mn);
			initStringInfo(&mx);
			PgColumnarEncodeValue(&mn, att, chunkMin);
			PgColumnarEncodeValue(&mx, att, chunkMax);
			z->hasMinMax = true;
			z->minimum = mn.data;
			z->minimumLen = (uint32) mn.len;
			z->maximum = mx.data;
			z->maximumLen = (uint32) mx.len;
		}

		zoneRows = lappend(zoneRows, z);
	}

	/* per-column-chunk bloom over hashable values (native spec 7.2, D5b) */
	if (def->bloomable)
	{
		StringInfoData hashes;
		char	   *bloom;
		uint32		bloomLen;

		initStringInfo(&hashes);
		foreach(lc, chunkGroups)
		{
			ChunkGroupBuffer *group = (ChunkGroupBuffer *) lfirst(lc);
			ColumnChunkBuffer *col = &group->columns[columnIndex];

			if (col->hashBuf.len > 0)
				appendBinaryStringInfo(&hashes, col->hashBuf.data,
									   col->hashBuf.len);
		}
		if (hashes.len > 0 &&
			PgColumnarBloomBuild((const uint32 *) hashes.data,
							   hashes.len / sizeof(uint32),
							   &bloom, &bloomLen))
		{
			NativeBloomMetadata *b = palloc0(sizeof(NativeBloomMetadata));

			b->storageId = storageId;
			b->groupNumber = groupNumber;
			b->columnIndex = columnIndex;
			b->filter = bloom;
			b->filterLen = bloomLen;
			bloomRow = b;
		}
	}

	/* optional block codec over the whole encoded region (spec 6) */
	finalData = encoded->data;
	finalLen = encoded->len;
	if (compressionType != COLUMNAR_COMPRESSION_NONE &&
		encoded->len > 0)
	{
		char	   *compData;
		uint32		compLen;
		int			usedType;
		int			usedLevel;

		PgColumnarCompressValueStream(encoded->data, encoded->len,
									compressionType,
									compressionLevel,
									&compData, &compLen,
									&usedType, &usedLevel);
		if (usedType != COLUMNAR_COMPRESSION_NONE)
		{
			finalData = compData;
			finalLen = compLen;
			blockCodec = usedType;
		}
	}

	if (finalLen > 0)
		appendBinaryStringInfo(chunk, finalData, finalLen);

	result.chunk = chunk;
	result.descriptor = desc->data;
	result.descriptorLen = (uint32) desc->len;
	result.blockCodec = blockCodec;
	result.zoneRows = zoneRows;
	result.bloomRow = bloomRow;
	return result;
}

/*
 * serialize_column_input
 *		Serialise one column's flush_one_column input (its per-chunk-group
 *		buffers) into `out`, in the INPUT wire format (#445 slice 2). The bytes
 *		are later copied into a dsm segment behind a uint32 length prefix. Only
 *		the fields flush_one_column reads are written; the min/max Datums use
 *		datumSerialize with the column's byval/len.
 */
static void
serialize_column_input(StringInfo out, Form_pg_attribute att, List *chunkGroups,
					   int columnIndex)
{
	uint32		vectorCount = (uint32) list_length(chunkGroups);
	ListCell   *lc;

	appendBinaryStringInfo(out, (char *) &vectorCount, sizeof(uint32));

	foreach(lc, chunkGroups)
	{
		ChunkGroupBuffer *group = (ChunkGroupBuffer *) lfirst(lc);
		ColumnChunkBuffer *col = &group->columns[columnIndex];
		uint64		groupRowCount = group->rowCount;
		uint64		valueCount = col->valueCount;
		int64		sum = col->sum;
		uint8		hasMinMax = (uint8) (col->hasMinMax ? 1 : 0);
		uint32		existsLen = (uint32) col->existsStream.len;
		uint32		valueLen = (uint32) col->valueStream.len;
		uint32		hashLen = (uint32) col->hashBuf.len;

		appendBinaryStringInfo(out, (char *) &groupRowCount, sizeof(uint64));
		appendBinaryStringInfo(out, (char *) &valueCount, sizeof(uint64));
		appendBinaryStringInfo(out, (char *) &sum, sizeof(int64));
		appendBinaryStringInfo(out, (char *) &hasMinMax, sizeof(uint8));

		appendBinaryStringInfo(out, (char *) &existsLen, sizeof(uint32));
		if (existsLen > 0)
			appendBinaryStringInfo(out, col->existsStream.data, existsLen);
		appendBinaryStringInfo(out, (char *) &valueLen, sizeof(uint32));
		if (valueLen > 0)
			appendBinaryStringInfo(out, col->valueStream.data, valueLen);
		appendBinaryStringInfo(out, (char *) &hashLen, sizeof(uint32));
		if (hashLen > 0)
			appendBinaryStringInfo(out, col->hashBuf.data, hashLen);

		if (col->hasMinMax)
		{
			Size		minSpace = datumEstimateSpace(col->minValue, false,
													  att->attbyval, att->attlen);
			Size		maxSpace = datumEstimateSpace(col->maxValue, false,
													  att->attbyval, att->attlen);
			char	   *ptr;

			enlargeStringInfo(out, (int) minSpace);
			ptr = out->data + out->len;
			datumSerialize(col->minValue, false, att->attbyval, att->attlen,
						   &ptr);
			out->len += (int) minSpace;

			enlargeStringInfo(out, (int) maxSpace);
			ptr = out->data + out->len;
			datumSerialize(col->maxValue, false, att->attbyval, att->attlen,
						   &ptr);
			out->len += (int) maxSpace;

			out->data[out->len] = '\0';
		}
	}
}

/*
 * deserialize_column_input
 *		Rebuild the List *chunkGroups flush_one_column expects from a dsm segment
 *		written by serialize_column_input (#445 slice 2). dsmaddr points at a
 *		uint32 payload length followed by the payload. Every buffer is copied out
 *		of the dsm into freshly palloc'd memory so nothing points into the segment
 *		after it is detached. Each ChunkGroupBuffer's columns array is sized
 *		(columnIndex + 1) and only [columnIndex] is populated.
 */
static List *
deserialize_column_input(void *dsmaddr, Form_pg_attribute att, int columnIndex)
{
	char	   *base = (char *) dsmaddr;
	uint32		payloadLen PG_USED_FOR_ASSERTS_ONLY;
	char	   *cursor;
	uint32		vectorCount;
	uint32		v;
	List	   *chunkGroups = NIL;

	memcpy(&payloadLen, base, sizeof(uint32));
	cursor = base + sizeof(uint32);

	memcpy(&vectorCount, cursor, sizeof(uint32));
	cursor += sizeof(uint32);

	for (v = 0; v < vectorCount; v++)
	{
		ChunkGroupBuffer *group = palloc0(sizeof(ChunkGroupBuffer));
		ColumnChunkBuffer *col;
		uint64		groupRowCount;
		uint64		valueCount;
		int64		sum;
		uint8		hasMinMax;
		uint32		existsLen;
		uint32		valueLen;
		uint32		hashLen;

		group->columns = palloc0(sizeof(ColumnChunkBuffer) * (columnIndex + 1));
		col = &group->columns[columnIndex];

		memcpy(&groupRowCount, cursor, sizeof(uint64));
		cursor += sizeof(uint64);
		memcpy(&valueCount, cursor, sizeof(uint64));
		cursor += sizeof(uint64);
		memcpy(&sum, cursor, sizeof(int64));
		cursor += sizeof(int64);
		memcpy(&hasMinMax, cursor, sizeof(uint8));
		cursor += sizeof(uint8);

		group->rowCount = groupRowCount;
		col->valueCount = valueCount;
		col->sum = sum;
		col->hasMinMax = (hasMinMax != 0);

		memcpy(&existsLen, cursor, sizeof(uint32));
		cursor += sizeof(uint32);
		initStringInfo(&col->existsStream);
		if (existsLen > 0)
		{
			appendBinaryStringInfo(&col->existsStream, cursor, existsLen);
			cursor += existsLen;
		}

		memcpy(&valueLen, cursor, sizeof(uint32));
		cursor += sizeof(uint32);
		initStringInfo(&col->valueStream);
		if (valueLen > 0)
		{
			appendBinaryStringInfo(&col->valueStream, cursor, valueLen);
			cursor += valueLen;
		}

		memcpy(&hashLen, cursor, sizeof(uint32));
		cursor += sizeof(uint32);
		initStringInfo(&col->hashBuf);
		if (hashLen > 0)
		{
			appendBinaryStringInfo(&col->hashBuf, cursor, hashLen);
			cursor += hashLen;
		}

		if (col->hasMinMax)
		{
			bool		isnull;

			col->minValue = datumRestore(&cursor, &isnull);
			col->maxValue = datumRestore(&cursor, &isnull);
		}

		chunkGroups = lappend(chunkGroups, group);
	}

	Assert(cursor == base + sizeof(uint32) + payloadLen);
	return chunkGroups;
}

/*
 * serialize_column_result
 *		Serialise one column's flush_one_column result into `out`, in the RESULT
 *		wire format (#445 slice 2). The numeric zone sum is a varlena Datum
 *		(byval=false, len=-1); the encoded min/max and bloom filter are opaque
 *		byte buffers copied verbatim.
 */
static void
serialize_column_result(StringInfo out, FlushColumnResult *res)
{
	uint8		hasChunk = (uint8) (res->chunk != NULL ? 1 : 0);
	uint8		hasDescriptor = (uint8) (res->descriptor != NULL ? 1 : 0);
	int32		blockCodec = (int32) res->blockCodec;
	uint32		zoneCount = (uint32) list_length(res->zoneRows);
	uint8		hasBloom = (uint8) (res->bloomRow != NULL ? 1 : 0);
	ListCell   *lc;

	appendBinaryStringInfo(out, (char *) &hasChunk, sizeof(uint8));
	if (res->chunk != NULL)
	{
		uint32		chunkLen = (uint32) res->chunk->len;

		appendBinaryStringInfo(out, (char *) &chunkLen, sizeof(uint32));
		if (chunkLen > 0)
			appendBinaryStringInfo(out, res->chunk->data, chunkLen);
	}

	appendBinaryStringInfo(out, (char *) &hasDescriptor, sizeof(uint8));
	if (res->descriptor != NULL)
	{
		uint32		descLen = res->descriptorLen;

		appendBinaryStringInfo(out, (char *) &descLen, sizeof(uint32));
		if (descLen > 0)
			appendBinaryStringInfo(out, res->descriptor, descLen);
	}

	appendBinaryStringInfo(out, (char *) &blockCodec, sizeof(int32));
	appendBinaryStringInfo(out, (char *) &zoneCount, sizeof(uint32));

	foreach(lc, res->zoneRows)
	{
		NativeZoneMapMetadata *z = (NativeZoneMapMetadata *) lfirst(lc);
		uint64		storageId = z->storageId;
		uint64		groupNumber = z->groupNumber;
		int32		columnIndex = (int32) z->columnIndex;
		int32		vectorIndex = (int32) z->vectorIndex;
		uint64		valueCount = z->valueCount;
		uint64		nullCount = z->nullCount;
		uint8		hasSum = (uint8) (z->hasSum ? 1 : 0);
		uint8		zHasMinMax = (uint8) (z->hasMinMax ? 1 : 0);

		appendBinaryStringInfo(out, (char *) &storageId, sizeof(uint64));
		appendBinaryStringInfo(out, (char *) &groupNumber, sizeof(uint64));
		appendBinaryStringInfo(out, (char *) &columnIndex, sizeof(int32));
		appendBinaryStringInfo(out, (char *) &vectorIndex, sizeof(int32));
		appendBinaryStringInfo(out, (char *) &valueCount, sizeof(uint64));
		appendBinaryStringInfo(out, (char *) &nullCount, sizeof(uint64));

		appendBinaryStringInfo(out, (char *) &hasSum, sizeof(uint8));
		if (z->hasSum)
		{
			Size		sumSpace = datumEstimateSpace(z->sum, false, false, -1);
			char	   *ptr;

			enlargeStringInfo(out, (int) sumSpace);
			ptr = out->data + out->len;
			datumSerialize(z->sum, false, false, -1, &ptr);
			out->len += (int) sumSpace;
			out->data[out->len] = '\0';
		}

		appendBinaryStringInfo(out, (char *) &zHasMinMax, sizeof(uint8));
		if (z->hasMinMax)
		{
			uint32		minLen = z->minimumLen;
			uint32		maxLen = z->maximumLen;

			appendBinaryStringInfo(out, (char *) &minLen, sizeof(uint32));
			if (minLen > 0)
				appendBinaryStringInfo(out, z->minimum, minLen);
			appendBinaryStringInfo(out, (char *) &maxLen, sizeof(uint32));
			if (maxLen > 0)
				appendBinaryStringInfo(out, z->maximum, maxLen);
		}
	}

	appendBinaryStringInfo(out, (char *) &hasBloom, sizeof(uint8));
	if (res->bloomRow != NULL)
	{
		NativeBloomMetadata *b = res->bloomRow;
		uint64		storageId = b->storageId;
		uint64		groupNumber = b->groupNumber;
		int32		columnIndex = (int32) b->columnIndex;
		uint32		filterLen = b->filterLen;

		appendBinaryStringInfo(out, (char *) &storageId, sizeof(uint64));
		appendBinaryStringInfo(out, (char *) &groupNumber, sizeof(uint64));
		appendBinaryStringInfo(out, (char *) &columnIndex, sizeof(int32));
		appendBinaryStringInfo(out, (char *) &filterLen, sizeof(uint32));
		if (filterLen > 0)
			appendBinaryStringInfo(out, b->filter, filterLen);
	}
}

/*
 * deserialize_column_result
 *		Rebuild a FlushColumnResult from a dsm segment written by
 *		serialize_column_result (#445 slice 2). dsmaddr points at a uint32 payload
 *		length followed by the payload. Every buffer (chunk, descriptor, zone
 *		min/max, bloom filter) and the numeric sum Datum are copied out of the dsm
 *		into palloc'd memory so nothing points into the segment after detach.
 */
static FlushColumnResult
deserialize_column_result(void *dsmaddr)
{
	char	   *base = (char *) dsmaddr;
	uint32		payloadLen PG_USED_FOR_ASSERTS_ONLY;
	char	   *cursor;
	FlushColumnResult result;
	uint8		hasChunk;
	uint8		hasDescriptor;
	int32		blockCodec;
	uint32		zoneCount;
	uint32		z;
	uint8		hasBloom;
	List	   *zoneRows = NIL;

	memcpy(&payloadLen, base, sizeof(uint32));
	cursor = base + sizeof(uint32);

	result.chunk = NULL;
	result.descriptor = NULL;
	result.descriptorLen = 0;
	result.blockCodec = COLUMNAR_COMPRESSION_NONE;
	result.zoneRows = NIL;
	result.bloomRow = NULL;

	memcpy(&hasChunk, cursor, sizeof(uint8));
	cursor += sizeof(uint8);
	if (hasChunk)
	{
		uint32		chunkLen;
		StringInfo	chunk = makeStringInfo();

		memcpy(&chunkLen, cursor, sizeof(uint32));
		cursor += sizeof(uint32);
		if (chunkLen > 0)
		{
			appendBinaryStringInfo(chunk, cursor, chunkLen);
			cursor += chunkLen;
		}
		result.chunk = chunk;
	}

	memcpy(&hasDescriptor, cursor, sizeof(uint8));
	cursor += sizeof(uint8);
	if (hasDescriptor)
	{
		uint32		descLen;

		memcpy(&descLen, cursor, sizeof(uint32));
		cursor += sizeof(uint32);
		if (descLen > 0)
		{
			char	   *desc = palloc(descLen);

			memcpy(desc, cursor, descLen);
			cursor += descLen;
			result.descriptor = desc;
		}
		else
			result.descriptor = palloc(0);
		result.descriptorLen = descLen;
	}

	memcpy(&blockCodec, cursor, sizeof(int32));
	cursor += sizeof(int32);
	result.blockCodec = blockCodec;

	memcpy(&zoneCount, cursor, sizeof(uint32));
	cursor += sizeof(uint32);

	for (z = 0; z < zoneCount; z++)
	{
		NativeZoneMapMetadata *zm = palloc0(sizeof(NativeZoneMapMetadata));
		uint64		storageId;
		uint64		groupNumber;
		int32		columnIndex;
		int32		vectorIndex;
		uint64		valueCount;
		uint64		nullCount;
		uint8		hasSum;
		uint8		zHasMinMax;

		memcpy(&storageId, cursor, sizeof(uint64));
		cursor += sizeof(uint64);
		memcpy(&groupNumber, cursor, sizeof(uint64));
		cursor += sizeof(uint64);
		memcpy(&columnIndex, cursor, sizeof(int32));
		cursor += sizeof(int32);
		memcpy(&vectorIndex, cursor, sizeof(int32));
		cursor += sizeof(int32);
		memcpy(&valueCount, cursor, sizeof(uint64));
		cursor += sizeof(uint64);
		memcpy(&nullCount, cursor, sizeof(uint64));
		cursor += sizeof(uint64);

		zm->storageId = storageId;
		zm->groupNumber = groupNumber;
		zm->columnIndex = columnIndex;
		zm->vectorIndex = vectorIndex;
		zm->valueCount = valueCount;
		zm->nullCount = nullCount;

		memcpy(&hasSum, cursor, sizeof(uint8));
		cursor += sizeof(uint8);
		if (hasSum)
		{
			bool		isnull;

			zm->hasSum = true;
			zm->sum = datumRestore(&cursor, &isnull);
		}

		memcpy(&zHasMinMax, cursor, sizeof(uint8));
		cursor += sizeof(uint8);
		if (zHasMinMax)
		{
			uint32		minLen;
			uint32		maxLen;

			zm->hasMinMax = true;

			memcpy(&minLen, cursor, sizeof(uint32));
			cursor += sizeof(uint32);
			if (minLen > 0)
			{
				char	   *mn = palloc(minLen);

				memcpy(mn, cursor, minLen);
				cursor += minLen;
				zm->minimum = mn;
			}
			else
				zm->minimum = palloc(0);
			zm->minimumLen = minLen;

			memcpy(&maxLen, cursor, sizeof(uint32));
			cursor += sizeof(uint32);
			if (maxLen > 0)
			{
				char	   *mx = palloc(maxLen);

				memcpy(mx, cursor, maxLen);
				cursor += maxLen;
				zm->maximum = mx;
			}
			else
				zm->maximum = palloc(0);
			zm->maximumLen = maxLen;
		}

		zoneRows = lappend(zoneRows, zm);
	}
	result.zoneRows = zoneRows;

	memcpy(&hasBloom, cursor, sizeof(uint8));
	cursor += sizeof(uint8);
	if (hasBloom)
	{
		NativeBloomMetadata *b = palloc0(sizeof(NativeBloomMetadata));
		uint64		storageId;
		uint64		groupNumber;
		int32		columnIndex;
		uint32		filterLen;

		memcpy(&storageId, cursor, sizeof(uint64));
		cursor += sizeof(uint64);
		memcpy(&groupNumber, cursor, sizeof(uint64));
		cursor += sizeof(uint64);
		memcpy(&columnIndex, cursor, sizeof(int32));
		cursor += sizeof(int32);
		memcpy(&filterLen, cursor, sizeof(uint32));
		cursor += sizeof(uint32);

		b->storageId = storageId;
		b->groupNumber = groupNumber;
		b->columnIndex = columnIndex;
		if (filterLen > 0)
		{
			char	   *f = palloc(filterLen);

			memcpy(f, cursor, filterLen);
			cursor += filterLen;
			b->filter = f;
		}
		else
			b->filter = palloc(0);
		b->filterLen = filterLen;

		result.bloomRow = b;
	}

	Assert(cursor == base + sizeof(uint32) + payloadLen);
	return result;
}

/*
 * pflush_auto_workers
 *		Worker count for the parallel flush: half the admin's max_parallel_workers
 *		budget, at least one, capped at PFLUSH_MAX_WORKERS. Mirrors the export
 *		path's pexport_auto_workers (that copy is static in another file).
 */
static int
pflush_auto_workers(void)
{
	const char *s = GetConfigOption("max_parallel_workers", true, false);
	int			budget = (s != NULL) ? atoi(s) : 8;
	int			n = budget / 2;

	if (n < 1)
		n = 1;
	if (n > PFLUSH_MAX_WORKERS)
		n = PFLUSH_MAX_WORKERS;
	return n;
}

/*
 * pgcolumnar_parallel_flush_worker
 *		Background-worker entry (#445 slice 3): attach the input DSM, connect,
 *		and run flush_one_column for every column it can claim off the shared
 *		atomic counter, publishing the results in its own OUTPUT segment.
 *
 *		Modelled on pgcolumnar_parallel_export_worker. Unlike the export path
 *		this does NOT import the launcher's snapshot: the value data lives in the
 *		DSM, not the table, so the worker reads only committed catalog (the
 *		relation's tupdesc). StartTransactionCommand plus a pushed
 *		GetTransactionSnapshot is enough to open the relation; no
 *		ExportSnapshot/ImportSnapshot is needed (owner's default decision).
 */
PGDLLEXPORT void
pgcolumnar_parallel_flush_worker(Datum main_arg)
{
	dsm_segment *seg;
	shm_toc    *toc;
	PflushHeader *hdr;
	PflushWorkerSlot *slots;
	char	   *inputs;
	uint64	   *inoffs;
	PflushVerdict *verds;
	pg_atomic_uint32 *claim;
	PflushWorkerSlot *me;
	int			widx;
	uint32		conn_flags = BGWORKER_BYPASS_ALLOWCONN;

	memcpy(&widx, MyBgworkerEntry->bgw_extra, sizeof(int));

	pqsignal(SIGTERM, die);
	BackgroundWorkerUnblockSignals();

	seg = dsm_attach(DatumGetUInt32(main_arg));
	if (seg == NULL)
		ereport(ERROR,
				(errcode(ERRCODE_INTERNAL_ERROR),
				 errmsg("pgcolumnar parallel_flush worker could not attach to the shared segment")));
	toc = shm_toc_attach(PFLUSH_MAGIC, dsm_segment_address(seg));
	if (toc == NULL)
		ereport(ERROR,
				(errcode(ERRCODE_INTERNAL_ERROR),
				 errmsg("pgcolumnar parallel_flush worker found a bad shared segment")));
	hdr = (PflushHeader *) shm_toc_lookup(toc, PFLUSH_KEY_HEADER, false);
	inputs = (char *) shm_toc_lookup(toc, PFLUSH_KEY_INPUTS, false);
	inoffs = (uint64 *) shm_toc_lookup(toc, PFLUSH_KEY_INOFFS, false);
	verds = (PflushVerdict *) shm_toc_lookup(toc, PFLUSH_KEY_VERDICTS, false);
	slots = (PflushWorkerSlot *) shm_toc_lookup(toc, PFLUSH_KEY_SLOTS, false);
	claim = (pg_atomic_uint32 *) shm_toc_lookup(toc, PFLUSH_KEY_CLAIM, false);
	me = &slots[widx];

#if PG_VERSION_NUM >= 170000
	conn_flags |= BGWORKER_BYPASS_ROLELOGINCHECK;
#endif
	BackgroundWorkerInitializeConnectionByOid(hdr->dbid, hdr->roleid, conn_flags);

	/*
	 * A fresh snapshot for catalog/tupdesc reads only. A plain
	 * GetTransactionSnapshot pushed onto the active stack lets table_open resolve
	 * the relation; no ImportSnapshot, because the worker never reads the
	 * backend's uncommitted rows -- the value bytes are all in the DSM.
	 */
	StartTransactionCommand();
	PushActiveSnapshot(GetTransactionSnapshot());

	PG_TRY();
	{
		Relation	rel = table_open(hdr->relid, AccessShareLock);
		TupleDesc	tupdesc = RelationGetDescr(rel);
		StringInfoData recbuf;
		uint32		count = 0;
		dsm_segment *outseg;
		char	   *obase;

		/*
		 * Adopt the launcher's live encoding GUCs so the bytes this worker
		 * produces match the serial path exactly (see PflushHeader).
		 */
		pgcolumnar_fsst_verdict_reuse = hdr->fsstVerdictReuse;
		pgcolumnar_fsst_min_gain_percent = hdr->fsstMinGainPercent;
		pgcolumnar_encoding_sample_rows = hdr->encodingSampleRows;

		initStringInfo(&recbuf);

		for (;;)
		{
			uint32		c = pg_atomic_fetch_add_u32(claim, 1);
			Form_pg_attribute att;
			PgColumnarColumnDef def;
			List	   *groups;
			FlushColumnResult res;
			StringInfoData outbuf;
			uint32		resultLen;
			int8		verdict;
			int32		age;

			if (c >= (uint32) hdr->natts)
				break;

			att = TupleDescAttr(tupdesc, c);

			/* reconstruct the column def with the SAME logic the backend uses */
			build_column_def(att, hdr->bloomEnabled, CurrentMemoryContext, &def);
			def.fsstVerdict = verds[c].verdict;
			def.fsstVerdictAge = verds[c].age;

			/* rebuild this column's input from the DSM and flush it */
			groups = deserialize_column_input(inputs + inoffs[c], att, (int) c);
			res = flush_one_column(att, groups, &def, hdr->rowCount,
								   hdr->validityBytes, hdr->encodeEffort,
								   hdr->compressionType, hdr->compressionLevel,
								   hdr->storageId, hdr->groupNumber, (int) c);

			/* record (c, updated verdict/age, serialized result) */
			initStringInfo(&outbuf);
			serialize_column_result(&outbuf, &res);
			resultLen = (uint32) outbuf.len;
			verdict = def.fsstVerdict;
			age = (int32) def.fsstVerdictAge;

			appendBinaryStringInfo(&recbuf, (char *) &c, sizeof(uint32));
			appendBinaryStringInfo(&recbuf, (char *) &verdict, sizeof(int8));
			appendBinaryStringInfo(&recbuf, (char *) &age, sizeof(int32));
			appendBinaryStringInfo(&recbuf, (char *) &resultLen, sizeof(uint32));
			if (resultLen > 0)
				appendBinaryStringInfo(&recbuf, outbuf.data, outbuf.len);
			count++;
		}

		/*
		 * Publish one OUTPUT segment: {uint32 count; per column: uint32 c,
		 * int8 verdict, int32 age, uint32 resultLen, result bytes}. The
		 * (resultLen, result bytes) pair is exactly the length-prefixed payload
		 * deserialize_column_result reads, so the backend deserialises straight
		 * from the record. Pin the segment so it survives this worker's exit --
		 * the backend attaches only after WaitForBackgroundWorkerShutdown, by
		 * which point an unpinned segment (zero mappings) would be gone.
		 */
		outseg = dsm_create((Size) sizeof(uint32) + recbuf.len, 0);
		obase = (char *) dsm_segment_address(outseg);
		memcpy(obase, &count, sizeof(uint32));
		if (recbuf.len > 0)
			memcpy(obase + sizeof(uint32), recbuf.data, recbuf.len);
		dsm_pin_segment(outseg);
		me->outHandle = dsm_segment_handle(outseg);
		me->outLen = (uint32) (sizeof(uint32) + recbuf.len);
		dsm_detach(outseg);		/* pinned: persists for the backend to attach */

		table_close(rel, AccessShareLock);
		PopActiveSnapshot();
		CommitTransactionCommand();
		pg_atomic_write_u32(&me->state, PFLUSH_DONE);
	}
	PG_CATCH();
	{
		ErrorData  *edata;

		MemoryContextSwitchTo(TopMemoryContext);
		edata = CopyErrorData();
		me->sqlerrcode = edata->sqlerrcode;
		strlcpy(me->errmsg,
				edata->message ? edata->message : "parallel_flush worker failed",
				sizeof(me->errmsg));
		FlushErrorState();
		FreeErrorData(edata);
		AbortOutOfAnyTransaction();
		pg_atomic_write_u32(&me->state, PFLUSH_FAILED);
	}
	PG_END_TRY();

	dsm_detach(seg);
	proc_exit(0);
}

/*
 * pflush_error_cleanup
 *		Runs on an error during the parallel flush's wait/collect (#445 slice 4).
 *		Terminates any still-running workers, then frees every published-but-
 *		uncollected output segment. The collect loop invalidates each slot's handle
 *		as it frees it, so this only attaches segments still pinned by a worker --
 *		valid handles, safe to map -- and never double-frees one the happy path
 *		already released. Without this, a statement cancel inside WaitForBackground-
 *		WorkerShutdown would leak the pinned segments until postmaster restart.
 */
typedef struct PflushCleanup
{
	BackgroundWorkerHandle **handles;
	int			nstarted;
	PflushWorkerSlot *slots;
	int			nworkers;
} PflushCleanup;

static void
pflush_error_cleanup(int code, Datum arg)
{
	PflushCleanup *cl = (PflushCleanup *) DatumGetPointer(arg);
	int			i;

	for (i = 0; i < cl->nstarted; i++)
		if (cl->handles[i] != NULL)
		{
			TerminateBackgroundWorker(cl->handles[i]);
			WaitForBackgroundWorkerShutdown(cl->handles[i]);
		}

	for (i = 0; i < cl->nworkers; i++)
		if (pg_atomic_read_u32(&cl->slots[i].state) == PFLUSH_DONE &&
			cl->slots[i].outHandle != DSM_HANDLE_INVALID)
		{
			dsm_segment *s = dsm_attach(cl->slots[i].outHandle);

			if (s != NULL)
			{
				dsm_unpin_segment(cl->slots[i].outHandle);
				dsm_detach(s);
			}
		}
}

/*
 * pflush_metrics
 *		Sum a stripe's buffered column sizes for the parallel_flush dispatch log
 *		(#445). One pass over the .len fields already in hand; reads no data and
 *		allocates nothing, so it does not perturb the flush. bufBytes is the total
 *		the parallel path would copy through shared memory; valBytes and valCount
 *		describe the value payload alone.
 */
static void
pflush_metrics(PgColumnarWriteState *writeState, uint64 *bufBytes,
			   uint64 *valBytes, uint64 *valCount)
{
	ListCell   *lc;
	uint64		bb = 0;
	uint64		vb = 0;
	uint64		vc = 0;

	foreach(lc, writeState->chunkGroups)
	{
		ChunkGroupBuffer *group = (ChunkGroupBuffer *) lfirst(lc);
		int			c;

		for (c = 0; c < writeState->natts; c++)
		{
			ColumnChunkBuffer *col = &group->columns[c];

			bb += (uint64) col->existsStream.len + (uint64) col->valueStream.len +
				(uint64) col->hashBuf.len;
			vb += (uint64) col->valueStream.len;
			vc += col->valueCount;
		}
	}
	*bufBytes = bb;
	*valBytes = vb;
	*valCount = vc;
}

/*
 * flush_columns_parallel
 *		The #445 slice-3 parallel flush: dispatch flush_one_column across a pool
 *		of background workers and fill colResults[natts] with every column's
 *		result. Registration failure or a worker error is not fatal -- the
 *		backend completes any column a worker did not produce serially, in this
 *		process, so colResults ends up populated for all of [0, natts). The FSST
 *		verdict cache in writeState->colDefs is updated in column order (each
 *		column is owned by exactly one flush, so there is no cross-worker race).
 *		The caller assembles colResults and does all I/O and catalog writes.
 */
static void
flush_columns_parallel(PgColumnarWriteState *writeState, uint64 groupNumber,
					   uint64 rowCount, int validityBytes,
					   FlushColumnResult *colResults)
{
	int			natts = writeState->natts;
	int			nworkers = Min(natts, pflush_auto_workers());
	bool	   *done = palloc0(sizeof(bool) * natts);
	StringInfoData inputs;
	uint64	   *inoffs = palloc(sizeof(uint64) * (natts + 1));
	shm_toc_estimator est;
	Size		segsize;
	Size		inputsChunk;
	dsm_segment *seg;
	shm_toc    *toc;
	PflushHeader *hdr;
	char	   *inbuf;
	uint64	   *offs;
	PflushVerdict *verds;
	PflushWorkerSlot *slots;
	pg_atomic_uint32 *claim;
	uint32		dsmh;
	BackgroundWorker bw;
	BackgroundWorkerHandle **handles;
	PflushCleanup cleanup;
	int			nstarted = 0;
	int			nWorkerCols = 0;
	int			c;
	int			i;

	/* concatenate every column's length-prefixed serialize_column_input blob */
	initStringInfo(&inputs);
	for (c = 0; c < natts; c++)
	{
		Form_pg_attribute att = TupleDescAttr(writeState->tupdesc, c);
		StringInfoData one;
		uint32		len;

		inoffs[c] = (uint64) inputs.len;
		initStringInfo(&one);
		serialize_column_input(&one, att, writeState->chunkGroups, c);
		len = (uint32) one.len;
		appendBinaryStringInfo(&inputs, (char *) &len, sizeof(uint32));
		if (len > 0)
			appendBinaryStringInfo(&inputs, one.data, one.len);
		pfree(one.data);
	}
	inoffs[natts] = (uint64) inputs.len;

	/* one input DSM segment: header, inputs, offsets, verdict seeds, slots, claim */
	inputsChunk = (inputs.len > 0) ? (Size) inputs.len : 1;
	shm_toc_initialize_estimator(&est);
	shm_toc_estimate_chunk(&est, sizeof(PflushHeader));
	shm_toc_estimate_chunk(&est, inputsChunk);
	shm_toc_estimate_chunk(&est, mul_size(sizeof(uint64), natts + 1));
	shm_toc_estimate_chunk(&est, mul_size(sizeof(PflushVerdict), natts));
	shm_toc_estimate_chunk(&est, mul_size(sizeof(PflushWorkerSlot), nworkers));
	shm_toc_estimate_chunk(&est, sizeof(pg_atomic_uint32));
	shm_toc_estimate_keys(&est, 6);
	segsize = shm_toc_estimate(&est);

	seg = dsm_create(segsize, 0);
	toc = shm_toc_create(PFLUSH_MAGIC, dsm_segment_address(seg), segsize);

	hdr = (PflushHeader *) shm_toc_allocate(toc, sizeof(PflushHeader));
	shm_toc_insert(toc, PFLUSH_KEY_HEADER, hdr);
	inbuf = (char *) shm_toc_allocate(toc, inputsChunk);
	shm_toc_insert(toc, PFLUSH_KEY_INPUTS, inbuf);
	offs = (uint64 *) shm_toc_allocate(toc, mul_size(sizeof(uint64), natts + 1));
	shm_toc_insert(toc, PFLUSH_KEY_INOFFS, offs);
	verds = (PflushVerdict *) shm_toc_allocate(toc, mul_size(sizeof(PflushVerdict), natts));
	shm_toc_insert(toc, PFLUSH_KEY_VERDICTS, verds);
	slots = (PflushWorkerSlot *) shm_toc_allocate(toc,
												  mul_size(sizeof(PflushWorkerSlot), nworkers));
	shm_toc_insert(toc, PFLUSH_KEY_SLOTS, slots);
	claim = (pg_atomic_uint32 *) shm_toc_allocate(toc, sizeof(pg_atomic_uint32));
	shm_toc_insert(toc, PFLUSH_KEY_CLAIM, claim);

	if (inputs.len > 0)
		memcpy(inbuf, inputs.data, inputs.len);
	memcpy(offs, inoffs, sizeof(uint64) * (natts + 1));
	for (c = 0; c < natts; c++)
	{
		verds[c].verdict = writeState->colDefs[c].fsstVerdict;
		verds[c].age = (int32) writeState->colDefs[c].fsstVerdictAge;
	}
	pg_atomic_init_u32(claim, 0);

	hdr->dbid = MyDatabaseId;
	hdr->roleid = GetUserId();
	hdr->relid = writeState->relid;
	hdr->storageId = writeState->storageId;
	hdr->groupNumber = groupNumber;
	hdr->rowCount = rowCount;
	hdr->validityBytes = validityBytes;
	hdr->encodeEffort = writeState->encodeEffort;
	hdr->compressionType = writeState->compressionType;
	hdr->compressionLevel = writeState->compressionLevel;
	hdr->natts = natts;
	hdr->nworkers = nworkers;
	hdr->bloomEnabled = writeState->bloomEnabled;
	hdr->fsstVerdictReuse = pgcolumnar_fsst_verdict_reuse;
	hdr->fsstMinGainPercent = pgcolumnar_fsst_min_gain_percent;
	hdr->encodingSampleRows = pgcolumnar_encoding_sample_rows;

	for (i = 0; i < nworkers; i++)
	{
		pg_atomic_init_u32(&slots[i].state, PFLUSH_PENDING);
		slots[i].outHandle = DSM_HANDLE_INVALID;
		slots[i].outLen = 0;
		slots[i].sqlerrcode = 0;
		slots[i].errmsg[0] = '\0';
	}

	dsmh = dsm_segment_handle(seg);

	/*
	 * Register the workers. A registration failure is NOT an error: the started
	 * workers self-balance via the claim counter (a single worker will claim
	 * every column), and any column left unclaimed is completed serially below.
	 */
	handles = (BackgroundWorkerHandle **)
		palloc0(sizeof(BackgroundWorkerHandle *) * nworkers);

	memset(&bw, 0, sizeof(bw));
	bw.bgw_flags = BGWORKER_SHMEM_ACCESS | BGWORKER_BACKEND_DATABASE_CONNECTION;
	bw.bgw_start_time = BgWorkerStart_RecoveryFinished;
	bw.bgw_restart_time = BGW_NEVER_RESTART;
	strlcpy(bw.bgw_library_name, "pgcolumnar", BGW_MAXLEN);
	strlcpy(bw.bgw_function_name, "pgcolumnar_parallel_flush_worker", BGW_MAXLEN);
	snprintf(bw.bgw_name, BGW_MAXLEN, "pgcolumnar parallel_flush worker");
	snprintf(bw.bgw_type, BGW_MAXLEN, "pgcolumnar parallel_flush worker");
	bw.bgw_main_arg = UInt32GetDatum(dsmh);
	bw.bgw_notify_pid = MyProcPid;

	for (i = 0; i < nworkers; i++)
	{
		memcpy(bw.bgw_extra, &i, sizeof(int));
		if (RegisterDynamicBackgroundWorker(&bw, &handles[i]))
			nstarted++;
		else
		{
			handles[i] = NULL;
			break;				/* slot exhausted: the rest is done serially */
		}
	}

	/*
	 * Arm the cancel-safe cleanup over the wait+collect: an error here (a
	 * statement cancel inside the wait is the realistic one) terminates the
	 * workers and frees any pinned output segment not yet collected, so nothing
	 * leaks to postmaster restart. Disarmed at PG_END below on the normal path.
	 */
	cleanup.handles = handles;
	cleanup.nstarted = nstarted;
	cleanup.slots = slots;
	cleanup.nworkers = nworkers;

	PG_ENSURE_ERROR_CLEANUP(pflush_error_cleanup, PointerGetDatum(&cleanup));
	{
	/* wait for every started worker to finish (SIGTERM handler is die) */
	for (i = 0; i < nstarted; i++)
		if (handles[i] != NULL)
			WaitForBackgroundWorkerShutdown(handles[i]);

	/*
	 * Collect. For each DONE worker, attach its OUTPUT segment and stash every
	 * column it produced, applying the returned FSST verdict. A FAILED worker's
	 * columns are left undone (a WARNING is logged); a worker that never started
	 * leaves state PENDING. Everything not done is completed serially afterwards.
	 */
	for (i = 0; i < nstarted; i++)
	{
		uint32		st = pg_atomic_read_u32(&slots[i].state);

		if (st == PFLUSH_DONE && slots[i].outHandle != DSM_HANDLE_INVALID)
		{
			dsm_segment *outseg = dsm_attach(slots[i].outHandle);
			char	   *obase;
			char	   *cursor;
			uint32		count;
			uint32		k;

			if (outseg == NULL)
				continue;		/* lost the segment: fall to serial completion */

			/*
			 * Release the worker's pin now that we hold a mapping: the segment
			 * lives until our dsm_detach below, and if anything between here and
			 * there throws, the resource owner detaches our (now unpinned) mapping
			 * and the segment is freed rather than leaked to postmaster restart.
			 */
			dsm_unpin_segment(slots[i].outHandle);

			obase = (char *) dsm_segment_address(outseg);
			memcpy(&count, obase, sizeof(uint32));
			cursor = obase + sizeof(uint32);
			for (k = 0; k < count; k++)
			{
				uint32		col;
				int8		verdict;
				int32		age;
				uint32		resultLen;

				memcpy(&col, cursor, sizeof(uint32));
				cursor += sizeof(uint32);
				memcpy(&verdict, cursor, sizeof(int8));
				cursor += sizeof(int8);
				memcpy(&age, cursor, sizeof(int32));
				cursor += sizeof(int32);
				memcpy(&resultLen, cursor, sizeof(uint32));

				/* cursor points at [uint32 resultLen][payload] -> deserialize */
				colResults[col] = deserialize_column_result(cursor);
				cursor += sizeof(uint32) + resultLen;

				writeState->colDefs[col].fsstVerdict = verdict;
				writeState->colDefs[col].fsstVerdictAge = age;
				done[col] = true;
				nWorkerCols++;
			}
			dsm_detach(outseg);
			slots[i].outHandle = DSM_HANDLE_INVALID;	/* freed; skip in cleanup */
		}
		else if (st == PFLUSH_FAILED)
		{
			ereport(WARNING,
					(errmsg("pgcolumnar parallel_flush worker %d failed, completing its columns serially: %s",
							i, slots[i].errmsg[0] ? slots[i].errmsg : "unknown error")));
		}
	}
	}
	PG_END_ENSURE_ERROR_CLEANUP(pflush_error_cleanup, PointerGetDatum(&cleanup));

	dsm_detach(seg);
	pfree(inputs.data);

	/*
	 * Observability + the test premise: how the flush was actually split. A run
	 * with the GUC on but every column done serially (slot starvation) reports 0
	 * worker columns, so a silent fall-back to serial cannot be mistaken for a
	 * working parallel flush.
	 */
	elog(DEBUG1, "pgcolumnar parallel_flush: %d of %d columns by %d worker(s), %d serial",
		 nWorkerCols, natts, nstarted, natts - nWorkerCols);

	/*
	 * Serial completion of the remainder: every column no worker produced
	 * (unstarted slot, failed worker, or lost segment). flush_one_column mutates
	 * writeState->colDefs[c] in place, so its verdict is applied by the call.
	 */
	for (c = 0; c < natts; c++)
	{
		Form_pg_attribute att;
		PgColumnarColumnDef *def;

		if (done[c])
			continue;

		att = TupleDescAttr(writeState->tupdesc, c);
		def = &writeState->colDefs[c];
		colResults[c] = flush_one_column(att, writeState->chunkGroups, def,
										 rowCount, validityBytes,
										 writeState->encodeEffort,
										 writeState->compressionType,
										 writeState->compressionLevel,
										 writeState->storageId, groupNumber, c);
	}
}

/*
 * rel_new_in_current_xact
 *		True if the relation was created in the current transaction, so its
 *		pg_class row is not yet committed. A parallel-flush worker opens the
 *		relation on a fresh snapshot to read its tuple descriptor; for a table
 *		created and loaded in one transaction (CREATE TABLE ...; INSERT ..., or
 *		CREATE TABLE AS) that open fails, and the flush would register workers,
 *		have them all fail, log a WARNING, and redo the whole thing serially.
 *		Detect it up front and keep the flush serial -- silently, and without the
 *		wasted registration and double work. (#445 slice 4)
 */
static bool
rel_new_in_current_xact(Oid relid)
{
	Relation	rel = table_open(relid, NoLock);	/* the INSERT already holds a lock */
	bool		isnew = (rel->rd_createSubid != InvalidSubTransactionId);

	table_close(rel, NoLock);
	return isnew;
}

/*
 * pgcolumnar_flush_row_group
 *		Native-format (PGCN v1) flush. Lay out the accumulated rows as one row
 *		group: each column is a column chunk of [validity bitmap][values], where
 *		the validity bitmap is one bit per row (LSB-first) and the values are the
 *		concatenated present-value streams encoded per-vector via the adaptive
 *		cascade and then optionally block-compressed. Compute per-vector and
 *		whole-chunk zone maps plus a per-chunk bloom filter. Write the bytes to
 *		the relation file and record the native catalog rows (storage, row_group,
 *		column_chunk, zone_map, bloom).
 */
static void
pgcolumnar_flush_row_group(PgColumnarWriteState *writeState)
{
	MemoryContext flushContext;
	MemoryContext oldContext;
	Relation	rel;
	int			natts = writeState->natts;
	StringInfo	data;
	uint64	   *chunkOffset;
	uint64	   *chunkLength;
	char	  **chunkDescriptor;
	uint32	   *chunkDescriptorLen;
	int		   *chunkBlockCodec;
	uint64		rowCount = writeState->stripeRowCount;
	/*
	 * The row group number is the stripe id reserved from the metapage when this
	 * stripe began buffering (persistent and unique per storage), so incremental
	 * inserts across transactions never collide on (storage_id, group_number).
	 * A per-write-state counter would restart at 0 and clash with existing groups.
	 */
	uint64		groupNumber = writeState->stripeId;
	uint64		fileOffset;
	uint64		dataLength;
	bool		reusedOffset;
	int			validityBytes = (int) ((rowCount + 7) / 8);
	ListCell   *lc;
	int			c;
	bool		pushedSnapshot = false;
	bool		goParallel;
	List	   *zoneRows = NIL;		/* NativeZoneMapMetadata * to insert (D5) */
	List	   *bloomRows = NIL;		/* NativeBloomMetadata * to insert (D5b) */

	/*
	 * Nothing buffered: a flush of an empty write state must be a no-op. The
	 * stripe id is only consumed by a group that actually holds rows, so writing
	 * a zero-row group here would reuse a stale stripe id and collide with the
	 * group already written for it (duplicate row_group_pkey). This guards every
	 * caller, including the unconditional pre-commit flush.
	 */
	if (rowCount == 0)
		return;

	if (!ActiveSnapshotSet())
	{
		PushActiveSnapshot(GetTransactionSnapshot());
		pushedSnapshot = true;
	}

	flushContext = AllocSetContextCreate(CurrentMemoryContext,
										 "columnar native flush",
										 ALLOCSET_DEFAULT_SIZES);
	oldContext = MemoryContextSwitchTo(flushContext);

	data = makeStringInfo();
	chunkOffset = palloc0(sizeof(uint64) * natts);
	chunkLength = palloc0(sizeof(uint64) * natts);
	chunkDescriptor = palloc0(sizeof(char *) * natts);
	chunkDescriptorLen = palloc0(sizeof(uint32) * natts);
	chunkBlockCodec = palloc0(sizeof(int) * natts);

	/*
	 * Build the row group column-major: each column chunk is
	 * [validity bitmap][values]. The values region is encoded per 1024-value
	 * vector (one chunk group) with the lightweight adaptive selector (D4), then
	 * an optional block codec runs over the whole encoded region. The chosen
	 * scheme is recorded in the encoding descriptor so the reader reconstructs
	 * the exact raw bytes. A vector whose selector returns NONE is stored raw,
	 * so an incompressible column stays byte-for-byte the D2b baseline plus the
	 * descriptor.
	 */
	/*
	 * #445 slice 3: with pgcolumnar.parallel_flush on and at least two columns
	 * to spread, dispatch flush_one_column across a worker pool, degrading to
	 * serial completion of any column a worker did not produce (byte-identical to
	 * the serial path either way). tupdescIsRel keeps a projection's inner writer
	 * (synthetic tupdesc a worker could not rebuild from relid) on the serial
	 * path. With the GUC off, keep slice 2's in-backend round-trip loop unchanged.
	 *
	 * The GUC stays OFF by default. A gate to turn it on by default was measured
	 * and REFUTED (#445): no metric computable here -- natts, buffered bytes, or
	 * bytes per value -- separates the win from the losses, because the deciding
	 * variable is per-column encode CPU and its balance across columns, which the
	 * buffered .len fields do not carry. Two shapes with byte-identical
	 * (natts, bufbytes) had opposite best: 20 int2 and 5 int8 both buffer the same
	 * bytes yet one wins and one regresses, and a random int column ties a
	 * constant one on the metric while differing three-fold in fact. So do NOT add
	 * a (natts, bufbytes, width) gate here; it would mispredict. The dispatch log
	 * below records the metrics, so the refutation stays checkable and an opt-in
	 * user can see how a flush was split. test/parallel_flush_optin.sh pins this.
	 */
	goParallel = pgcolumnar_parallel_flush && natts >= 2 &&
		writeState->tupdescIsRel && !rel_new_in_current_xact(writeState->relid);

	if (message_level_is_interesting(DEBUG1))
	{
		uint64		bufBytes;
		uint64		valBytes;
		uint64		valCount;

		pflush_metrics(writeState, &bufBytes, &valBytes, &valCount);
		elog(DEBUG1, "pgcolumnar parallel_flush dispatch: rows=%lu natts=%d "
			 "bufbytes=%lu valbytes=%lu valcount=%lu -> %s",
			 (unsigned long) rowCount, natts, (unsigned long) bufBytes,
			 (unsigned long) valBytes, (unsigned long) valCount,
			 goParallel ? "parallel" : "serial");
	}

	if (goParallel)
	{
		FlushColumnResult *colResults = palloc0(sizeof(FlushColumnResult) * natts);

		flush_columns_parallel(writeState, groupNumber, rowCount, validityBytes,
							   colResults);

		/* assemble in column order (identical to the serial assembly) */
		for (c = 0; c < natts; c++)
		{
			FlushColumnResult *r = &colResults[c];

			chunkOffset[c] = data->len;
			if (r->chunk != NULL && r->chunk->len > 0)
				appendBinaryStringInfo(data, r->chunk->data, r->chunk->len);
			chunkLength[c] = data->len - chunkOffset[c];
			chunkDescriptor[c] = r->descriptor;
			chunkDescriptorLen[c] = r->descriptorLen;
			chunkBlockCodec[c] = r->blockCodec;
			if (r->zoneRows != NIL)
				zoneRows = list_concat(zoneRows, r->zoneRows);
			if (r->bloomRow != NULL)
				bloomRows = lappend(bloomRows, r->bloomRow);
		}
	}
	else
	{
		/*
		 * Serial / non-dispatched path: call flush_one_column directly, no dsm.
		 * The serialize/dsm round-trip only earns its cost when a worker will read
		 * the bytes across a process boundary; in-backend-serial it is pure
		 * overhead (a per-column dsm_create pair), so the non-dispatched path is
		 * the same direct call as slice 1 (#589) and OFF stays byte-identical to
		 * and as fast as main. The dsm crossing lives solely in
		 * flush_columns_parallel, on the worker path where it pays for itself.
		 */
		for (c = 0; c < natts; c++)
		{
			Form_pg_attribute att = TupleDescAttr(writeState->tupdesc, c);
			FlushColumnResult res = flush_one_column(att, writeState->chunkGroups,
													 &writeState->colDefs[c], rowCount,
													 validityBytes, writeState->encodeEffort,
													 writeState->compressionType,
													 writeState->compressionLevel,
													 writeState->storageId, groupNumber, c);

			chunkOffset[c] = data->len;
			if (res.chunk != NULL && res.chunk->len > 0)
				appendBinaryStringInfo(data, res.chunk->data, res.chunk->len);
			chunkLength[c] = data->len - chunkOffset[c];
			chunkDescriptor[c] = res.descriptor;
			chunkDescriptorLen[c] = res.descriptorLen;
			chunkBlockCodec[c] = res.blockCodec;
			if (res.zoneRows != NIL)
				zoneRows = list_concat(zoneRows, res.zoneRows);
			if (res.bloomRow != NULL)
				bloomRows = lappend(bloomRows, res.bloomRow);
		}
	}

	dataLength = data->len;

	rel = table_open(writeState->relid, RowExclusiveLock);

	/*
	 * Physical reclaim (Phase F): an online compaction (which holds
	 * ShareUpdateExclusiveLock and is self-serialized, so it is the only writer
	 * that can reuse space at a time) reserves from a previously freed range whose
	 * freeing transaction the oldest-xmin horizon has passed, instead of advancing
	 * the file highwater. Reuse is done here, before the relation extension lock,
	 * so the free_space catalog access is not under that lock. Plain inserts (which
	 * hold only RowExclusiveLock) always append, so they never race a reuse.
	 */
	reusedOffset = false;
	if (dataLength > 0 &&
		CheckRelationLockedByMe(rel, ShareUpdateExclusiveLock, false))
		reusedOffset = PgColumnarAllocateFreeSpace(PgColumnarStorageId(rel), dataLength,
												 PgColumnarOldestXmin(rel), &fileOffset);

	LockRelationForExtension(rel, ExclusiveLock);
	if (!reusedOffset)
		PgColumnarReserveOffset(rel, dataLength, &fileOffset);
	if (dataLength > 0)
		PgColumnarWriteLogicalData(rel, fileOffset, data->data, dataLength);
	UnlockRelationForExtension(rel, ExclusiveLock);

	{
		NativeStorageMetadata s;

		s.storageId = writeState->storageId;
		s.relationOid = writeState->relid;
		s.formatVersion = COLUMNAR_NATIVE_VERSION_MAJOR;
		s.vectorLength = COLUMNAR_NATIVE_VECTOR_LENGTH;
		s.rowGroupLimit = writeState->stripeRowLimit;
		PgColumnarInsertNativeStorageRow(&s);
	}

	/*
	 * A group written OUTSIDE the recorded ordered run retracts the scan's claim
	 * to be sorted (#751), and that claim lives in a PLAN rather than in a
	 * catalog object the plan cache watches. Nothing invalidates a cached plan
	 * on an append, so a plan prepared while the relation was fully ordered would
	 * keep its ordered path and answer ORDER BY ... LIMIT from the run alone.
	 *
	 * That is a WRONG ANSWER, not a stale cost. Measured before this call
	 * existed, on a relation sorted on k and then given 600 rows with k from -1
	 * to -600: a prepared "SELECT k FROM pc ORDER BY k NULLS LAST LIMIT 5"
	 * returned 0,0,0,0,0 instead of -600,-599,-598,-597,-596. Pinned by the
	 * cached-plan arm of test/sorted_pathkeys.sh.
	 *
	 * Gated on the storage actually having a mark, and on this group falling
	 * outside it, so an ordinary bulk load into an unordered relation invalidates
	 * nothing. When it does fire it is once per row group, not once per row. A
	 * rewrite's own groups do not fire it: a rewrite writes into a fresh storage
	 * whose mark is not set until it finishes, and the relfilenode swap
	 * invalidates the relation anyway.
	 */
	{
		int64		sortedFrom,
					sortedThrough;
		List	   *sortedNames;
		char	   *sortedKind;

		PgColumnarGetSortedInfo(writeState->storageId, &sortedFrom, &sortedThrough,
								&sortedNames, &sortedKind);
		if (sortedThrough >= 0 &&
			((int64) groupNumber > sortedThrough || (int64) groupNumber < sortedFrom))
			CacheInvalidateRelcacheByRelid(writeState->relid);
	}
	/*
	 * #445: share one open per metadata table across this flush's inserts. The
	 * PG_TRY drops the session on error so a later open never reuses a relation
	 * the aborting subtransaction has freed; the resource owner closes them.
	 */
	PgColumnarBeginMetadataFlush();
	PG_TRY();
	{
		{
			NativeRowGroupMetadata rg;

			rg.storageId = writeState->storageId;
			rg.groupNumber = groupNumber;
			rg.fileOffset = fileOffset;
			rg.rowCount = rowCount;
			rg.byteLength = dataLength;
			rg.firstRowNumber = writeState->stripeFirstRowNumber;
			PgColumnarInsertRowGroupRow(&rg);
		}
		for (c = 0; c < natts; c++)
		{
			NativeColumnChunkMetadata cc;

			/* skipped virtual generated column (no chunk written) */
			if (chunkDescriptor[c] == NULL)
				continue;

			cc.storageId = writeState->storageId;
			cc.groupNumber = groupNumber;
			cc.columnIndex = c;
			cc.valueCount = rowCount;
			cc.encodingDescriptor = chunkDescriptor[c];
			cc.encodingDescriptorLen = chunkDescriptorLen[c];
			cc.blockCodec = chunkBlockCodec[c];
			cc.pageOffset = fileOffset + chunkOffset[c];
			cc.pageLength = chunkLength[c];
			PgColumnarInsertColumnChunkRow(&cc);
		}
		foreach(lc, zoneRows)
			PgColumnarInsertZoneMapRow((NativeZoneMapMetadata *) lfirst(lc));
		foreach(lc, bloomRows)
			PgColumnarInsertBloomRow((NativeBloomMetadata *) lfirst(lc));

		PgColumnarEndMetadataFlush();
	}
	PG_CATCH();
	{
		PgColumnarResetMetadataFlush();
		PG_RE_THROW();
	}
	PG_END_TRY();

	table_close(rel, RowExclusiveLock);

	MemoryContextSwitchTo(oldContext);
	MemoryContextDelete(flushContext);

	/* reset accumulation; the next row reserves a fresh stripe id (row group) */
	MemoryContextReset(writeState->stripeContext);
	writeState->chunkGroups = NIL;
	writeState->currentGroup = NULL;
	writeState->stripeRowCount = 0;
	writeState->haveReservation = false;

	if (pushedSnapshot)
		PopActiveSnapshot();
}

/* -------------------------------------------------------------------------
 * projection write fan-out (gap 26, phase 2)
 *
 * Each additional projection of a table has its own storage id but shares the
 * table's relation file and row-number space. On insert, the projected columns
 * plus the base row number are buffered; at flush the batch is sorted on the
 * projection's sort key and written as a stripe to the projection's storage,
 * reusing the base stripe encoder (PgColumnarWriteRow + pgcolumnar_flush_row_group).
 * The base row number is stored as a leading int8 column so the projection can
 * be joined back to the base; deletes/visibility come from the base delete_vector, so
 * DELETE does not rewrite the projection. UPDATE is delete-old plus insert-new
 * and must fan the new number out or a covering projection scan will miss it
 * (see design/gaps/26-IMPL-projections-phase2-plan.md).
 * ------------------------------------------------------------------------- */

/* one buffered projection row: [rownumber, projcol1..projcolK] */
typedef struct ProjRow
{
	Datum	   *values;
	bool	   *nulls;
} ProjRow;

typedef struct PgColumnarProjWriter
{
	uint64		projStorageId;
	int			ncols;			/* number of projection columns (K) */
	AttrNumber *colAttnums;		/* table attnums of the K columns (1-based) */
	TupleDesc	projTupdesc;	/* [rownumber int8, projcol1..projcolK] */

	int			nsort;
	int		   *sortBufIdx;		/* index into a row's values[] for each sort col */
	FmgrInfo   *sortCmp;		/* btree cmp proc per sort col */
	Oid		   *sortColl;		/* collation per sort col */

	int			stripeRowLimit;
	int			chunkGroupRowLimit;
	int			compType;
	int			compLevel;

	ProjRow    *rows;			/* buffered rows (capacity stripeRowLimit) */
	int			nrows;
	MemoryContext ctx;			/* persists: struct arrays, projTupdesc */
	MemoryContext rowCtx;		/* reset after each stripe flush: row datums */
	PgColumnarWriteState *innerWs;	/* reused stripe encoder for this projection */
} PgColumnarProjWriter;
/*
 * PgColumnarWriteStateProjStripeIds
 *		The stripe ids this write state's projection fan-out drew (#345).
 *
 *		A projection writes through its own inner write state but reserves from
 *		the BASE relation's stripe counter, because PgColumnarWriteRow is called
 *		with the base relation (see flush_proj_writer). Its groups are recorded
 *		under the projection's own storage id, so they never appear in the base
 *		relation's row group list.
 *
 *		That combination is why the caller needs these separately. To
 *		record_online_sorted_extent, an id drawn by its own projection fan-out is
 *		indistinguishable from one taken by another session: both leave a gap in
 *		the base write state's ids. Treating the former as foreign truncated the
 *		ordered run at the first projection flush, so a fully reclustered table
 *		with a projection reported almost all of itself as decayed.
 *
 *		Returns a palloc'd array in the caller's context, or NULL when this write
 *		state has no projection writers.
 */
uint64 *
PgColumnarWriteStateProjStripeIds(PgColumnarWriteState *ws, int *n)
{
	ListCell   *lc;
	uint64	   *ids = NULL;
	int			total = 0;
	int			k = 0;

	*n = 0;
	if (ws->projWriters == NIL)
		return NULL;

	foreach(lc, ws->projWriters)
	{
		PgColumnarProjWriter *w = (PgColumnarProjWriter *) lfirst(lc);

		if (w->innerWs != NULL)
			total += w->innerWs->nReservedStripeIds;
	}
	if (total == 0)
		return NULL;

	ids = (uint64 *) palloc(sizeof(uint64) * total);
	foreach(lc, ws->projWriters)
	{
		PgColumnarProjWriter *w = (PgColumnarProjWriter *) lfirst(lc);
		int			i;

		if (w->innerWs == NULL)
			continue;
		for (i = 0; i < w->innerWs->nReservedStripeIds; i++)
			ids[k++] = w->innerWs->reservedStripeIds[i];
	}
	*n = k;
	return ids;
}


/*
 * pgcolumnar_build_write_state
 *		Allocate a standalone stripe encoder for the given tuple descriptor and
 *		storage id, not registered in PgColumnarWriteStates. Used for a
 *		projection's inner writer; carries the same per-chunk min/max and bloom
 *		skip metadata as the base writer so a sorted projection gives tight
 *		min/max ranges for the planner (gap 26).
 */
static PgColumnarWriteState *
pgcolumnar_build_write_state(Oid relid, TupleDesc srcTupdesc, uint64 storageId,
						   int stripeRowLimit, int chunkGroupRowLimit,
						   int compType, int compLevel)
{
	MemoryContext oldContext;
	PgColumnarWriteState *ws;

	if (PgColumnarWriteContext == NULL)
		PgColumnarWriteContext = AllocSetContextCreate(TopTransactionContext,
													 "columnar write",
													 ALLOCSET_DEFAULT_SIZES);
	oldContext = MemoryContextSwitchTo(PgColumnarWriteContext);

	ws = palloc0(sizeof(PgColumnarWriteState));
	ws->relid = relid;
	ws->subid = GetCurrentSubTransactionId();
	ws->tupdesc = CreateTupleDescCopy(srcTupdesc);
	ws->natts = ws->tupdesc->natts;
	ws->stripeRowLimit = stripeRowLimit;
	ws->chunkGroupRowLimit = chunkGroupRowLimit;
	ws->compressionType = compType;
	ws->compressionLevel = compLevel;

	/*
	 * A projection is written under the same bloom decision as its base
	 * relation. Leaving this unset would zero it, silently dropping bloom
	 * filters from projections while the setting was on.
	 */
	ws->bloomEnabled = pgcolumnar_enable_bloom_filter;

	/*
	 * And under the same encode_effort as its base, for the same reason: a
	 * projection written at a different effort from the table it projects would
	 * make the setting mean something different depending on which copy you read.
	 */
	ws->encodeEffort = COLUMNAR_ENCODE_EFFORT_FULL;
	{
		PgColumnarOptions opts;

		if (PgColumnarReadOptions(relid, &opts) && opts.encodeEffortSet)
			ws->encodeEffort = opts.encodeEffort;
	}
	ws->storageId = storageId;
	pgcolumnar_init_col_defs(ws);	/* min/max + bloom skip metadata for projections */
	ws->stripeContext = AllocSetContextCreate(PgColumnarWriteContext,
											  "columnar proj stripe",
											  ALLOCSET_DEFAULT_SIZES);
	ws->writeContext = PgColumnarWriteContext;
	ws->chunkGroups = NIL;
	ws->currentGroup = NULL;
	ws->stripeRowCount = 0;
	ws->haveReservation = false;

	MemoryContextSwitchTo(oldContext);
	return ws;
}

/* qsort_arg comparator: ascending, NULLS LAST, over the projection sort key */
static int
proj_row_cmp(const void *a, const void *b, void *arg)
{
	const ProjRow *ra = (const ProjRow *) a;
	const ProjRow *rb = (const ProjRow *) b;
	PgColumnarProjWriter *w = (PgColumnarProjWriter *) arg;
	int			i;

	for (i = 0; i < w->nsort; i++)
	{
		int			idx = w->sortBufIdx[i];
		bool		na = ra->nulls[idx];
		bool		nb = rb->nulls[idx];
		int32		c;

		if (na && nb)
			continue;
		if (na)
			return 1;			/* nulls last */
		if (nb)
			return -1;
		c = DatumGetInt32(FunctionCall2Coll(&w->sortCmp[i], w->sortColl[i],
											ra->values[idx], rb->values[idx]));
		if (c != 0)
			return c;
	}
	return 0;
}

/*
 * flush_proj_writer
 *		Sort the buffered rows on the projection's sort key and write them as one
 *		stripe to the projection's storage, then reset the buffer.
 */
static void
flush_proj_writer(PgColumnarProjWriter *w, Relation tableRel)
{
	int			i;

	if (w->nrows == 0)
		return;

	if (w->nsort > 0)
		qsort_arg(w->rows, w->nrows, sizeof(ProjRow), proj_row_cmp, w);

	if (w->innerWs == NULL)
		w->innerWs = pgcolumnar_build_write_state(RelationGetRelid(tableRel),
												w->projTupdesc, w->projStorageId,
												w->stripeRowLimit,
												w->chunkGroupRowLimit,
												w->compType, w->compLevel);

	for (i = 0; i < w->nrows; i++)
		PgColumnarWriteRow(w->innerWs, tableRel, w->rows[i].values, w->rows[i].nulls);

	if (w->innerWs->stripeRowCount > 0)
		pgcolumnar_flush_row_group(w->innerWs);

	MemoryContextReset(w->rowCtx);
	w->nrows = 0;
}

/*
 * build_proj_writer
 *		Construct a PgColumnarProjWriter for one projection catalog row.
 */
static PgColumnarProjWriter *
build_proj_writer(Relation rel, const PgColumnarProjection *proj,
				  int stripeRowLimit, int chunkGroupRowLimit,
				  int compType, int compLevel)
{
	TupleDesc	tableDesc = RelationGetDescr(rel);
	MemoryContext ctx;
	MemoryContext oldContext;
	PgColumnarProjWriter *w;
	int			i;

	ctx = AllocSetContextCreate(PgColumnarWriteContext, "columnar proj writer",
								ALLOCSET_DEFAULT_SIZES);
	oldContext = MemoryContextSwitchTo(ctx);

	w = palloc0(sizeof(PgColumnarProjWriter));
	w->projStorageId = proj->projStorageId;
	w->ncols = proj->columnsLen;
	w->stripeRowLimit = stripeRowLimit;
	w->chunkGroupRowLimit = chunkGroupRowLimit;
	w->compType = compType;
	w->compLevel = compLevel;
	w->ctx = ctx;
	w->rowCtx = AllocSetContextCreate(ctx, "columnar proj rows",
									  ALLOCSET_DEFAULT_SIZES);
	w->rows = palloc0(sizeof(ProjRow) * stripeRowLimit);
	w->nrows = 0;
	w->innerWs = NULL;

	w->colAttnums = palloc(sizeof(AttrNumber) * w->ncols);
	for (i = 0; i < w->ncols; i++)
		w->colAttnums[i] = (AttrNumber) proj->columns[i];

	/* synthetic tuple descriptor: rownumber int8, then the projection columns */
	w->projTupdesc = CreateTemplateTupleDesc(w->ncols + 1);
	TupleDescInitEntry(w->projTupdesc, 1, "rownumber", INT8OID, -1, 0);
	for (i = 0; i < w->ncols; i++)
		TupleDescCopyEntry(w->projTupdesc, i + 2, tableDesc, w->colAttnums[i]);

	/* sort-key comparators; each sort attnum is one of the projection columns */
	w->nsort = proj->sortKeyLen;
	if (w->nsort > 0)
	{
		w->sortBufIdx = palloc(sizeof(int) * w->nsort);
		w->sortCmp = palloc(sizeof(FmgrInfo) * w->nsort);
		w->sortColl = palloc(sizeof(Oid) * w->nsort);
		for (i = 0; i < w->nsort; i++)
		{
			int16		attno = proj->sortKey[i];
			Form_pg_attribute att = TupleDescAttr(tableDesc, attno - 1);
			TypeCacheEntry *tce;
			int			p;

			/* position of this sort column within the projection's columns */
			w->sortBufIdx[i] = -1;
			for (p = 0; p < w->ncols; p++)
				if (w->colAttnums[p] == attno)
				{
					w->sortBufIdx[i] = p + 1;	/* +1 for the leading rownumber */
					break;
				}
			if (w->sortBufIdx[i] < 0)
				elog(ERROR, "columnar: sort column not in projection columns");

			tce = lookup_type_cache(att->atttypid, TYPECACHE_CMP_PROC_FINFO);
			if (!OidIsValid(tce->cmp_proc_finfo.fn_oid))
				ereport(ERROR,
						(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
						 errmsg("cannot sort projection on column of type %s",
								format_type_be(att->atttypid))));
			fmgr_info_copy(&w->sortCmp[i], &tce->cmp_proc_finfo, ctx);
			w->sortColl[i] = att->attcollation;
		}
	}

	MemoryContextSwitchTo(oldContext);
	return w;
}

/*
 * append_proj_row
 *		Buffer one row (the base row number plus the projection's columns) into a
 *		projection writer, flushing a stripe when the buffer fills. Shared by the
 *		insert fan-out and the add-projection back-fill.
 */
static void
append_proj_row(PgColumnarProjWriter *w, Relation rel, TupleDesc tableDesc,
				uint64 rowNumber, Datum *values, bool *nulls)
{
	MemoryContext oldContext = MemoryContextSwitchTo(w->rowCtx);
	ProjRow    *r = &w->rows[w->nrows];
	int			i;

	r->values = palloc(sizeof(Datum) * (w->ncols + 1));
	r->nulls = palloc(sizeof(bool) * (w->ncols + 1));
	r->values[0] = Int64GetDatum((int64) rowNumber);
	r->nulls[0] = false;
	for (i = 0; i < w->ncols; i++)
	{
		AttrNumber	a = w->colAttnums[i];
		Form_pg_attribute att = TupleDescAttr(tableDesc, a - 1);

		if (nulls[a - 1])
		{
			r->nulls[i + 1] = true;
			r->values[i + 1] = (Datum) 0;
		}
		else
		{
			r->nulls[i + 1] = false;
			r->values[i + 1] = datumCopy(values[a - 1], att->attbyval,
										 att->attlen);
		}
	}
	w->nrows++;
	MemoryContextSwitchTo(oldContext);

	if (w->nrows >= w->stripeRowLimit)
		flush_proj_writer(w, rel);
}

/*
 * PgColumnarProjectionFanoutRow
 *		Buffer a freshly inserted row into each additional projection of the
 *		relation. rowNumber is the base row number returned by PgColumnarWriteRow.
 *		The projection writers hang off the base write state, so they share its
 *		(relid, subid) lifecycle.
 */
void
PgColumnarProjectionFanoutRow(Relation rel, PgColumnarWriteState *baseWs,
							uint64 rowNumber, Datum *values, bool *nulls)
{
	TupleDesc	tableDesc = RelationGetDescr(rel);
	ListCell   *lc;

	if (!baseWs->projInited)
	{
		List	   *projs = PgColumnarListProjections(baseWs->storageId);
		MemoryContext oldContext = MemoryContextSwitchTo(PgColumnarWriteContext);
		ListCell   *pc;

		foreach(pc, projs)
		{
			PgColumnarProjection *p = (PgColumnarProjection *) lfirst(pc);

			if (p->projectionId == 0)
				continue;		/* base projection is the table itself */
			baseWs->projWriters =
				lappend(baseWs->projWriters,
						build_proj_writer(rel, p, baseWs->stripeRowLimit,
										  baseWs->chunkGroupRowLimit,
										  baseWs->compressionType,
										  baseWs->compressionLevel));
		}
		baseWs->projInited = true;
		MemoryContextSwitchTo(oldContext);
	}

	if (baseWs->projWriters == NIL)
		return;

	foreach(lc, baseWs->projWriters)
		append_proj_row((PgColumnarProjWriter *) lfirst(lc), rel, tableDesc,
						rowNumber, values, nulls);
}

/*
 * PgColumnarBackfillProjection
 *		Populate a newly declared projection from the table's existing live rows
 *		(gap 26): scan the base and buffer-sort-flush each row into the
 *		projection's storage. Called by add_projection so a projection added to a
 *		populated table is complete. The caller must hold a lock that blocks
 *		concurrent writers (ShareLock) so no row is missed.
 */
void
PgColumnarBackfillProjection(Relation rel, const PgColumnarProjection *proj)
{
	TupleDesc	tableDesc = RelationGetDescr(rel);
	Oid			relid = RelationGetRelid(rel);
	int			stripeRowLimit = pgcolumnar_stripe_row_limit;
	int			chunkGroupRowLimit = pgcolumnar_chunk_group_row_limit;
	int			compType = pgcolumnar_compression;
	int			compLevel = pgcolumnar_compression_level;
	PgColumnarOptions opts;
	PgColumnarProjWriter *w;
	PgColumnarReadState *readState;
	Snapshot	snapshot;
	Datum	   *values;
	bool	   *nulls;
	uint64		rowNumber;

	if (PgColumnarWriteContext == NULL)
		PgColumnarWriteContext = AllocSetContextCreate(TopTransactionContext,
													 "columnar write",
													 ALLOCSET_DEFAULT_SIZES);

	if (PgColumnarReadOptions(relid, &opts))
	{
		if (opts.stripeRowLimitSet)
			stripeRowLimit = opts.stripeRowLimit;
		if (opts.chunkGroupRowLimitSet)
			chunkGroupRowLimit = opts.chunkGroupRowLimit;
		if (opts.compressionSet)
			compType = opts.compressionType;
		if (opts.compressionLevelSet)
			compLevel = opts.compressionLevel;
	}

	/* flush any pending base writes so the scan sees this transaction's rows */
	PgColumnarFlushWriteStateForRelation(relid);
	PgColumnarFlushDeleteVectorForRelation(rel);

	w = build_proj_writer(rel, proj, stripeRowLimit, chunkGroupRowLimit,
						  compType, compLevel);

	snapshot = ActiveSnapshotSet() ? GetActiveSnapshot() : GetTransactionSnapshot();
	values = palloc(sizeof(Datum) * tableDesc->natts);
	nulls = palloc(sizeof(bool) * tableDesc->natts);

	readState = PgColumnarBeginRead(rel, snapshot, NULL, NULL, 0, NULL);
	while (PgColumnarReadNextRow(readState, values, nulls, &rowNumber))
		append_proj_row(w, rel, tableDesc, rowNumber, values, nulls);
	PgColumnarEndRead(readState);

	flush_proj_writer(w, rel);
}

/* Flush all projection writers hanging off a base write state. */
static void
flush_ws_projections(PgColumnarWriteState *ws)
{
	ListCell   *lc;
	Relation	rel;
	bool		any = false;

	foreach(lc, ws->projWriters)
		if (((PgColumnarProjWriter *) lfirst(lc))->nrows > 0)
			any = true;
	if (!any)
		return;

	rel = table_open(ws->relid, RowExclusiveLock);
	foreach(lc, ws->projWriters)
		flush_proj_writer((PgColumnarProjWriter *) lfirst(lc), rel);
	table_close(rel, RowExclusiveLock);
}

/*
 * PgColumnarFlushWriteStateForRelation
 *		Flush any pending partial stripe for a single relation. Used at scan
 *		start so data written earlier in this transaction is persisted.
 */
void
PgColumnarFlushWriteStateForRelation(Oid relid)
{
	ListCell   *lc;

	foreach(lc, PgColumnarWriteStates)
	{
		PgColumnarWriteState *writeState = (PgColumnarWriteState *) lfirst(lc);

		if (writeState->relid != relid)
			continue;
		if (writeState->stripeRowCount > 0)
			pgcolumnar_flush_row_group(writeState);
		flush_ws_projections(writeState);
	}
}

/*
 * PgColumnarForgetWriteStateForRelation
 *		Drop the cached write state for a relation without flushing it. Used
 *		after the relation's storage is swapped (columnar.vacuum): the cached
 *		state holds the old storage id, so it must be discarded and a fresh one
 *		created for the new storage. The caller must have flushed first if any
 *		buffered rows still needed persisting.
 */
void
PgColumnarForgetWriteStateForRelation(Oid relid)
{
	List	   *kept = NIL;
	ListCell   *lc;
	MemoryContext oldContext;

	if (PgColumnarWriteStates == NIL)
		return;

	oldContext = MemoryContextSwitchTo(PgColumnarWriteContext);
	foreach(lc, PgColumnarWriteStates)
	{
		PgColumnarWriteState *writeState = (PgColumnarWriteState *) lfirst(lc);

		if (writeState->relid != relid)
			kept = lappend(kept, writeState);
	}
	MemoryContextSwitchTo(oldContext);

	PgColumnarWriteStates = kept;
}

/*
 * PgColumnarFlushAllPendingWrites
 *		Flush every pending write state. Called at transaction pre-commit.
 */
void
PgColumnarFlushAllPendingWrites(void)
{
	ListCell   *lc;

	foreach(lc, PgColumnarWriteStates)
	{
		PgColumnarWriteState *writeState = (PgColumnarWriteState *) lfirst(lc);

		pgcolumnar_flush_row_group(writeState);
		flush_ws_projections(writeState);
	}
}

/*
 * PgColumnarDiscardAllPendingWrites
 *		Forget all pending write states. The backing memory is freed with the
 *		transaction context, so we only clear our static references.
 */
void
PgColumnarDiscardAllPendingWrites(void)
{
	PgColumnarWriteStates = NIL;
	PgColumnarWriteContext = NULL;
}

/*
 * PgColumnarWriteStateDiscardSubXact
 *		Drop buffered (unflushed) writes made in an aborting subtransaction.
 *		Stripes already flushed by that subtransaction are made invisible by
 *		the subtransaction abort itself (their catalog rows), so only the
 *		in-memory buffers need discarding here (spec 9).
 */
void
PgColumnarWriteStateDiscardSubXact(SubTransactionId subid)
{
	List	   *kept = NIL;
	ListCell   *lc;
	MemoryContext oldContext;

	if (PgColumnarWriteStates == NIL)
		return;

	oldContext = MemoryContextSwitchTo(PgColumnarWriteContext);
	foreach(lc, PgColumnarWriteStates)
	{
		PgColumnarWriteState *writeState = (PgColumnarWriteState *) lfirst(lc);

		if (writeState->subid != subid)
			kept = lappend(kept, writeState);
	}
	MemoryContextSwitchTo(oldContext);

	PgColumnarWriteStates = kept;
}

/*
 * PgColumnarWriteStatePromoteSubXact
 *		On subtransaction commit, reassign its buffered writes to the parent so
 *		they are flushed when the parent (eventually the top transaction)
 *		commits.
 */
void
PgColumnarWriteStatePromoteSubXact(SubTransactionId subid, SubTransactionId parent)
{
	ListCell   *lc;

	foreach(lc, PgColumnarWriteStates)
	{
		PgColumnarWriteState *writeState = (PgColumnarWriteState *) lfirst(lc);

		if (writeState->subid == subid)
			writeState->subid = parent;
	}
}
