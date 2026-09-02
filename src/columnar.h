/*-------------------------------------------------------------------------
 *
 * columnar.h
 *		Shared declarations for the pgColumnar table access method.
 *
 * pgColumnar is an independent MIT implementation built solely from
 * design/NATIVE_FORMAT_AND_INTERFACE_SPEC.md and the public PostgreSQL API. It
 * reads and writes the native format (PGCN v1) described in that specification.
 *
 *-------------------------------------------------------------------------
 */
#ifndef PGCOLUMNAR_H
#define PGCOLUMNAR_H

#include "postgres.h"

#include "columnar_compat.h"

#include "access/skey.h"
#include "access/tableam.h"
#include "port/atomics.h"
#include "lib/stringinfo.h"
#include "nodes/bitmapset.h"
#include "utils/array.h"
#include "nodes/pg_list.h"
#include "nodes/extensible.h"
#include "storage/bufpage.h"
#include "utils/rel.h"
#include "utils/snapshot.h"

/* Physical storage-layer version stamped on the metapage (block 0). This is the
 * shared logical-storage version, independent of the data format; the data
 * format is the native format identified below and in pgcolumnar.storage. */
#define COLUMNAR_VERSION_MAJOR 2
#define COLUMNAR_VERSION_MINOR 2

/*
 * Native format (PGCN v1). The on-disk data format, designed from public
 * research and the open Arrow/Parquet/ORC specs; see
 * design/NATIVE_FORMAT_AND_INTERFACE_SPEC.md. It is identified by its own magic
 * and major version, recorded in the pgcolumnar.storage catalog row. The row
 * group holds per-column chunks encoded in fixed-length vectors, with zone maps
 * and per-chunk bloom filters for skipping.
 */
#define COLUMNAR_NATIVE_MAGIC "PGCN"		/* native format magic tag */
#define COLUMNAR_NATIVE_VERSION_MAJOR 1
#define COLUMNAR_NATIVE_VECTOR_LENGTH 1024	/* values per vector (fixed) */

/*
 * Native column-chunk encoding descriptor (D4). A column chunk's
 * pgcolumnar.column_chunk.encoding_descriptor is either the D2b baseline (a
 * single 0 byte: raw present values, no per-vector encoding, block_codec 0) or a
 * D4 descriptor with this leading version byte, recording the lightweight
 * encoding chosen per 1024-value vector so the reader reconstructs the exact raw
 * value stream. The layout is: uint8 version, uint8 reserved, uint32 vectorCount,
 * then per vector { uint8 encodingType, uint32 valueCount, uint32 rawLen,
 * uint32 encLen }. Integers are host-endian (little-endian hosts assumed, spec 3).
 *
 * Version 2 (E3b) appends one trailing region after the per-vector entries:
 * { uint32 sharedTableLen, sharedTableLen bytes }. It holds a chunk-shared FSST
 * symbol table (0 when the chunk has none); FSST vectors in a version-2 chunk
 * store a bare code stream decoded against this one table instead of embedding
 * their own, so the costly table build is paid once per chunk, not per vector.
 * It is appended (not inserted after the header) so every per-vector entry offset
 * is unchanged from version 1.
 */
#define COLUMNAR_NATIVE_ENCDESC_BASELINE 0
#define COLUMNAR_NATIVE_ENCDESC_VERSION 2
#define COLUMNAR_NATIVE_ENCDESC_HEADER_LEN 6	/* version + reserved + vectorCount */
#define COLUMNAR_NATIVE_ENCDESC_ENTRY_LEN 13	/* encodingType + 3 * uint32 */
#define COLUMNAR_NATIVE_ENCDESC_SHARED_LEN_BYTES 4	/* trailing uint32 sharedTableLen */

/* first row number is 1 (spec 3) */
#define COLUMNAR_FIRST_ROW_NUMBER 1

/*
 * Logical/physical mapping constants (spec 2, 2.1).
 *
 * BYTES_PER_PAGE is the number of logical data bytes carried by one physical
 * page. The first logical byte lives at the start of block 2.
 */
#define COLUMNAR_BYTES_PER_PAGE ((uint64) (BLCKSZ - SizeOfPageHeaderData))
#define COLUMNAR_FIRST_LOGICAL_OFFSET (2 * COLUMNAR_BYTES_PER_PAGE)

/*
 * Round a logical byte length up to a whole number of pages. Every reservation
 * (PgColumnarReserveOffset) starts on a page boundary and the next one starts on
 * the next page boundary, so a group's on-disk footprint is its data length
 * rounded up to a page. Physical reclaim keeps free ranges page-aligned in both
 * offset and length by working in these footprints.
 */
#define COLUMNAR_PAGE_ROUND_UP(n) \
	(((((uint64) (n)) + COLUMNAR_BYTES_PER_PAGE - 1) / COLUMNAR_BYTES_PER_PAGE) \
	 * COLUMNAR_BYTES_PER_PAGE)

/*
 * Row-number <-> item-pointer mapping (spec 6).
 *
 * VALID_ITEMPOINTER_OFFSETS is the count of item-pointer offsets we use per
 * synthetic block. The spec only requires it to be bounded by MaxOffsetNumber
 * and to yield a reversible mapping; we use MaxHeapTuplesPerPage, which is
 * comfortably below MaxOffsetNumber. These TIDs are synthetic row addresses;
 * they are never used to locate bytes in the data file. (Implementation
 * choice, noted per PROVENANCE clean-room rules.)
 */
#define COLUMNAR_VALID_ITEMPOINTER_OFFSETS ((uint64) MaxHeapTuplesPerPage)

/* compression codes (spec 5) */
#define COLUMNAR_COMPRESSION_NONE 0
#define COLUMNAR_COMPRESSION_PGLZ 1
#define COLUMNAR_COMPRESSION_LZ4 2
#define COLUMNAR_COMPRESSION_ZSTD 3

/*
 * Value-stream encoding codes (I1, format 2.1). An encoding is a reversible
 * transform of the raw value-stream bytes, applied before block compression on
 * write and reversed after decompression on read (pgcolumnar_encoding.c).
 */
#define COLUMNAR_ENCODING_NONE 0
#define COLUMNAR_ENCODING_RLE 1		/* run-length of a fixed-width value */
#define COLUMNAR_ENCODING_FOR 2		/* frame-of-reference + bit-packing */
#define COLUMNAR_ENCODING_DELTA 3	/* delta + zigzag + bit-packing */
#define COLUMNAR_ENCODING_GORILLA 4 /* Gorilla XOR for float4/float8 */
#define COLUMNAR_ENCODING_DOD 5		/* delta-of-delta + zigzag + bit-packing */
#define COLUMNAR_ENCODING_DICT 6	/* dictionary of distinct values + codes */
#define COLUMNAR_ENCODING_ALP 7		/* ALP decimal scheme for float4/float8 (E1) */
#define COLUMNAR_ENCODING_FSST 8	/* FSST symbol-table string compression (E2) */

/* schema that holds the metadata catalog */
#define COLUMNAR_SCHEMA_NAME "pgcolumnar"

/* -------------------------------------------------------------------------
 * Per-table options (spec 7.4). A "set" flag distinguishes an explicitly
 * stored per-table value from the instance-wide GUC default.
 * ------------------------------------------------------------------------- */
/*
 * How much work the writer spends choosing an encoding (issue #155).
 *
 * FULL is what this format has always done. FAST skips the FSST substring
 * search -- the symbol-table build, the whole-corpus "does it help" decision,
 * and the per-vector encode -- which is where the write cost of a text column
 * overwhelmingly is. Nothing else changes: dictionary, RLE, the numeric schemes
 * and the block codec all still run, and a chunk written either way is read
 * back by the same code.
 */
#define COLUMNAR_ENCODE_EFFORT_FULL 0
#define COLUMNAR_ENCODE_EFFORT_FAST 1

typedef struct PgColumnarOptions
{
	bool		chunkGroupRowLimitSet;
	int			chunkGroupRowLimit;
	bool		stripeRowLimitSet;
	int			stripeRowLimit;
	bool		compressionSet;
	int			compressionType;	/* one of COLUMNAR_COMPRESSION_* */
	bool		compressionLevelSet;
	int			compressionLevel;
	bool		encodeEffortSet;
	int			encodeEffort;		/* one of COLUMNAR_ENCODE_EFFORT_* */
} PgColumnarOptions;

/* GUC-backed instance defaults (spec 8.3) */
extern int pgcolumnar_stripe_row_limit;
extern int pgcolumnar_chunk_group_row_limit;
extern int pgcolumnar_encoding_sample_rows;
extern int pgcolumnar_compression;		/* one of COLUMNAR_COMPRESSION_* */
extern int pgcolumnar_compression_level;	/* zstd level */
extern int pgcolumnar_fsst_min_gain_percent;	/* min compressed FSST win to keep it (#155) */
/*
 * How many row groups may reuse a column's FSST keep/drop verdict before it is
 * taken again (#472). 0 never reuses, which is the behaviour before that issue
 * and what the byte-identical test arm compares against.
 *
 * Asking the question costs a whole-corpus FSST encode plus a compression pass,
 * once per column per row group, because the answer cannot be sampled: on a
 * training prefix FSST can look 24% worse while over the whole column it is 23%
 * better. Measured at 41 to 47 percent of a text load, re-deriving a verdict
 * that did not change across 20 row groups.
 */
extern int	pgcolumnar_fsst_verdict_reuse;

/* a column's cached FSST verdict (#472) */
#define COLUMNAR_FSST_UNKNOWN	0
#define COLUMNAR_FSST_HELPS		1
#define COLUMNAR_FSST_HURTS		2
extern bool pgcolumnar_enable_qual_pushdown;
extern int	pgcolumnar_qual_skipvec_min_payload_cols;	/* #595 width gate */
extern bool pgcolumnar_enable_late_materialization;
extern bool pgcolumnar_enable_column_projection;
extern bool pgcolumnar_enable_custom_scan;
extern bool pgcolumnar_enable_bloom_filter;	/* bloom equality skipping (I7) */
extern bool pgcolumnar_objstore_buffered;	/* chunk-granular remote reads (#393) */
extern char *pgcolumnar_objstore_allowed_endpoints; /* #393 allow-list, SUSET */
extern int	pgcolumnar_objstore_part_size;	/* #394 remote multipart part size */
extern char *pgcolumnar_objstore_s3_addressing; /* #621 path|virtual addressing */
/* #415 autovacuum daemon */
extern bool pgcolumnar_autovacuum;
extern int	pgcolumnar_autovacuum_naptime;
extern double pgcolumnar_autovacuum_compact_threshold;
extern double pgcolumnar_autovacuum_recluster_threshold;
extern void PgColumnarAutovacuumRegister(void);
extern int	pgcolumnar_maintenance_hold_ms;	/* dev/test: hold SUEL this long in a maintenance verb */

/* Phase 6 GUCs (spec 8.3) */
extern bool pgcolumnar_enable_vectorization;	/* vectorized aggregate path */
extern bool pgcolumnar_enable_group_vectorization;	/* GROUP BY vectorized agg (#289) */
extern bool pgcolumnar_enable_ungrouped_vector_agg;	/* filtered/extended ungrouped agg (#289) */
extern bool pgcolumnar_enable_parallel_vector_agg;	/* parallel-aware ungrouped batch fold (#289 phase 5/6) */
extern int pgcolumnar_groupagg_max_groups;	/* execution-time group-count cap; over it the query ERRORS rather than falling back (#289) */
extern bool pgcolumnar_enable_read_stream;	/* stream/prefetch block reads (PG17+) */
extern bool pgcolumnar_enable_index_only_scan;	/* allow index-only scans (gap 28) */
extern bool pgcolumnar_bulk_parallel_writer;	/* internal: parallel_copy loader skips the storage-row creation lock (#300) */
extern bool pgcolumnar_parallel_flush;	/* dispatch the per-column stripe flush across bgworkers (#445 slice 3) */
extern bool pgcolumnar_enable_projection_scan;	/* scan a covering projection (gap 26) */
extern bool pgcolumnar_enable_sorted_pathkeys;	/* advertise a sorted run's order (#751) */
extern bool pgcolumnar_enable_index_fetch_penalty;	/* price a columnar index scan's per-row fetch (#355) */

/*
 * Statement-scoped by-row-number fetch cache cap (pgcolumnar_reader.c). Named here
 * so the index-fetch cost model (#355) can tell when a stripe is too wide to be
 * retained across fetches and must be treated as re-decoded per row.
 */
#define COLUMNAR_FETCH_CACHE_MAX_BYTES	(32 * 1024 * 1024)

/* issue #5: concurrent unique-key insert serialization */
extern bool pgcolumnar_enable_unique_lock;	/* serialize same-key inserters */
extern int pgcolumnar_unique_lock_buckets;	/* advisory-lock buckets per index */

/* -------------------------------------------------------------------------
 * Metapage (spec 3)
 * ------------------------------------------------------------------------- */
typedef struct PgColumnarMetapage
{
	uint32		versionMajor;
	uint32		versionMinor;
	uint64		storageId;
	uint64		reservedStripeId;
	uint64		reservedRowNumber;
	uint64		reservedOffset;
	bool		unloggedReset;
} PgColumnarMetapage;


/* -------------------------------------------------------------------------
 * Native format catalog row shapes (re-origination line, PGCN v1). These map
 * to pgcolumnar.storage / row_group / column_chunk (native spec section 11).
 * ------------------------------------------------------------------------- */
typedef struct NativeStorageMetadata
{
	uint64		storageId;
	Oid			relationOid;
	int			formatVersion;
	int			vectorLength;
	int			rowGroupLimit;
} NativeStorageMetadata;

typedef struct NativeRowGroupMetadata
{
	uint64		storageId;
	uint64		groupNumber;
	uint64		fileOffset;
	uint64		rowCount;
	uint64		byteLength;
	uint64		firstRowNumber;
} NativeRowGroupMetadata;

typedef struct NativeColumnChunkMetadata
{
	uint64		storageId;
	uint64		groupNumber;
	int			columnIndex;
	uint64		valueCount;
	const char *encodingDescriptor;	/* bytea payload */
	uint32		encodingDescriptorLen;
	int			blockCodec;			/* 0 = none */
	uint64		pageOffset;
	uint64		pageLength;
} NativeColumnChunkMetadata;

/*
 * One pgcolumnar.zone_map row (native spec 7.1, Phase D5): a Small Materialized
 * Aggregate for one vector of a column chunk (vectorIndex 0-based) or for the
 * whole column chunk (vectorIndex -1). minimum and maximum are the column's
 * value serialized with PgColumnarEncodeValue (NULL when the type has no btree
 * ordering); sum is a numeric Datum (D5a leaves it unset, hasSum false; the
 * zone-map-only aggregate that consumes it lands in D5b). value_count and
 * null_count are always present.
 */
typedef struct NativeZoneMapMetadata
{
	uint64		storageId;
	uint64		groupNumber;
	int			columnIndex;
	int			vectorIndex;		/* 0-based vector; -1 for the whole chunk */
	bool		hasMinMax;
	const char *minimum;			/* PgColumnarEncodeValue bytes, when hasMinMax */
	uint32		minimumLen;
	const char *maximum;
	uint32		maximumLen;
	bool		hasSum;				/* D5a: false; sum computed in D5b */
	Datum		sum;				/* numeric Datum when hasSum */
	uint64		valueCount;
	uint64		nullCount;
} NativeZoneMapMetadata;

/*
 * One pgcolumnar.bloom row (native spec 7.2, Phase D5b): a per-column-chunk bloom
 * filter over the chunk's hashable values, for equality skipping on unsorted
 * columns. filter is the PgColumnarBloomBuild byte image.
 */
typedef struct NativeBloomMetadata
{
	uint64		storageId;
	uint64		groupNumber;
	int			columnIndex;
	const char *filter;
	uint32		filterLen;
} NativeBloomMetadata;

/*
 * One columnar.delete_vector row (spec 7.5): one row per chunk group, keyed by
 * (storage_id, group_number). A set bit in the bitmap marks a deleted row; bit i
 * (0-based) corresponds to the group's i-th row (row number
 * row_group.first_row_number + i) and is stored LSB-first in byte i/8. The group
 * origin comes from the row_group, so it is not stored here.
 */
typedef struct DeleteVectorMetadata
{
	uint64		groupNumber;
	int			deletedCount;
	char	   *bitmap;			/* bitmapLen bytes, in the caller's context */
	uint32		bitmapLen;
} DeleteVectorMetadata;

/*
 * Is `row` (a 0-based index within a chunk group) marked deleted in a delete
 * vector bitmap of maskLen bytes? The single source of the delete-vector bit
 * test: the seqscan, index fetch, index-only scan, per-row fetch, and buffered
 * delete-vector paths all consult it, so the bounds check and the bit math stay
 * in one place. A NULL or too-short mask means "not deleted" (the row is beyond
 * what the mask covers), which is the conservative, correct answer.
 */
static inline bool
dv_row_deleted(const char *mask, uint32 maskLen, uint64 row)
{
	return mask != NULL &&
		(row >> 3) < maskLen &&
		(mask[row >> 3] & (1 << (row & 7))) != 0;
}

/*
 * One columnar.projection row (gap 26, format 2.2). A projection is a named,
 * ordered column subset stored as its own columnar storage (projStorageId),
 * sorted on sortKey, sharing the table's row-number identity space.
 * projectionId 0 is the implicit base projection. attnums are 1-based; sortKey
 * attnums are a subset of columns.
 */
typedef struct PgColumnarProjection
{
	uint64		storageId;			/* the table's base storage id */
	int			projectionId;		/* 0 = base, 1..N additional */
	char	   *name;				/* projection name (caller's context) */
	uint64		projStorageId;		/* this projection's own storage id */
	int16	   *sortKey;			/* attnums in sort order, sortKeyLen entries */
	int			sortKeyLen;
	int16	   *columns;			/* stored attnums, columnsLen entries */
	int			columnsLen;
} PgColumnarProjection;


/* -------------------------------------------------------------------------
 * storage layer (pgcolumnar_storage.c)
 * ------------------------------------------------------------------------- */
struct SMgrRelationData;

extern void PgColumnarReadMetapage(Relation rel, PgColumnarMetapage *meta);
extern uint64 PgColumnarStorageId(Relation rel);

/* row number <-> item pointer (spec 6) */
extern void PgColumnarRowNumberToItemPointer(uint64 rowNumber, ItemPointer tid);
extern uint64 PgColumnarItemPointerToRowNumber(ItemPointer tid);

/* -------------------------------------------------------------------------
 * visibility map for index-only scans (pgcolumnar_visibilitymap.c, gap 28)
 * ------------------------------------------------------------------------- */
extern void PgColumnarVMClearForRow(Relation rel, uint64 rowNumber);
extern void PgColumnarVMClearForRowRange(Relation rel, uint64 firstRowNumber,
										 uint64 rowCount);
extern uint64 PgColumnarVMSetVisibleForRelation(Relation rel);

/* index maintenance for callers that insert rows without an executor (#153) */
typedef struct PgColumnarIndexInsertState
{
	EState	   *estate;
	TupleTableSlot *slot;

	/*
	 * Enforcing (import) mode: the executor's own index maintenance, bracketed
	 * by an after-trigger query level so deferred constraints reach commit.
	 */
	bool		enforcing;
	ResultRelInfo *rri;
	bool		queryLevel;		/* the after-trigger level is open */

	/*
	 * Non-enforcing (rewrite) mode only: the executor has no "insert without
	 * checking" mode, so this path opens the indexes itself.
	 */
	int			n;
	Relation   *rels;
	IndexInfo **infos;
	ExprState **predicates;		/* partial-index predicate, or NULL */
} PgColumnarIndexInsertState;

/*
 * enforceConstraints is fixed for the life of the state rather than passed per
 * row, because it selects which of the two routes above is built: an importer
 * enforces for every row it inserts and a rewrite enforces for none.
 */
extern PgColumnarIndexInsertState *PgColumnarIndexInsertBegin(Relation rel,
														  bool enforceConstraints);
extern void PgColumnarIndexInsertRow(PgColumnarIndexInsertState *st, Relation rel,
								   Datum *values, bool *isnull,
								   uint64 rowNumber);
extern void PgColumnarIndexInsertEnd(PgColumnarIndexInsertState *st);
extern bool PgColumnarRelationHasIndexes(Relation rel);


/* a contiguous run of all-visible row numbers (gap 28 phase 3) */
typedef struct PgColumnarRowRange
{
	uint64		firstRowNumber;
	uint64		rowCount;
}			PgColumnarRowRange;

/* row groups every one of whose rows is deleted as-of oldestXmin. Returns a
 * List of palloc'd uint64 group numbers. */
extern int64 PgColumnarRetireFullyDeletedGroups(Relation rel);
/* all-visible chunk-group row ranges: stripe committed past the horizon and no
 * deletes (committed or in-progress). Returns a List of PgColumnarRowRange *. */

/* physical reclaim: split freed ranges on allocate and coalesce on free (GUC) */
extern bool pgcolumnar_reclaim_coalesce;

/* physical end-truncation opt-in (GUC) */
extern bool pgcolumnar_enable_end_truncation;

/* error unless the current user owns the relation (maintenance/DDL gate) */
extern void PgColumnarRequireTableOwner(Relation rel);
extern void PgColumnarRequireTableOwnerByOid(Oid relid);
extern void PgColumnarRequireNoRowSecurity(Oid relid);

/*
 * Assert-only invariant: a storage's live row-group footprints and its
 * free_space ranges tile the file page-aligned with no overlap. Compiled and
 * called only in assert builds (the version matrix builds with asserts).
 */
#ifdef USE_ASSERT_CHECKING
extern void PgColumnarCheckFreeSpaceNoOverlap(uint64 storageId);
#define COLUMNAR_ASSERT_NO_OVERLAP(sid) PgColumnarCheckFreeSpaceNoOverlap(sid)
#else
#define COLUMNAR_ASSERT_NO_OVERLAP(sid) ((void) 0)
#endif

/* -------------------------------------------------------------------------
 * metadata layer (pgcolumnar_metadata.c)
 * ------------------------------------------------------------------------- */
extern uint64 PgColumnarNextStorageId(void);

/* projection: needed attnos (pull_varattnos form) -> the reader's 0-based set */

extern void PgColumnarCheckNativeFormatVersion(uint64 storageId, const char *relName);
extern List *PgColumnarReadRowGroupList(uint64 storageId, Snapshot snapshot);
extern List *PgColumnarReadZoneMapList(uint64 storageId, uint64 groupNumber,
									 Snapshot snapshot);
/*
 * #744: one scan's handle on pgcolumnar.zone_map.
 *
 * The skip loop probes zone_map once per chunk group per predicate column, and
 * each probe used to open the relation and resolve two names. This holds them
 * for the scan instead. It lives in the scan's own read state rather than in a
 * static, deliberately: a scan that ereports never runs its end hook, and a
 * shared static would then carry a Relation the resource owner has already
 * released into the NEXT scan. Per-scan, the pointer dies with the scan, and on
 * abort the resource owner releases the relation silently (resowner.c prints
 * leak warnings only when isCommit).
 *
 * probes and opens are the pair that makes the saving measurable rather than
 * asserted: without the cache they are equal, with it opens is 1.
 */
typedef struct PgColumnarZoneMapSession
{
	Relation	rel;
	Oid			idxOid;
	uint64		probes;
	uint64		opens;
	/*
	 * What this session was opened for, used only in the DEBUG1 report. The
	 * planner's survival estimate holds a session of its own, and its report has
	 * to be told apart from a scan's: they interleave in one backend's log, and
	 * native_zonemap_session counts scan reports to prove a scan around an
	 * aborted one opens for itself. NULL reads as "read", so an executor scan's
	 * line is byte-identical to what #744 shipped.
	 */
	const char *what;
} PgColumnarZoneMapSession;

/* sess may be NULL, which is the old open-per-probe behaviour. */
extern NativeZoneMapMetadata *PgColumnarReadZoneMapForColumn(uint64 storageId,
									 uint64 groupNumber, int columnIndex,
									 Snapshot snapshot,
									 PgColumnarZoneMapSession *sess);
extern void PgColumnarCloseZoneMapSession(PgColumnarZoneMapSession *sess);
extern void PgColumnarDeleteMetadata(uint64 storageId);

/* declared retention, read by pgcolumnar.expire (#403 item 5a) */
extern bool PgColumnarReadTtl(Oid relid, char **column, Interval **interval);

/* per-table options catalog (spec 7.4) */
extern bool PgColumnarReadOptions(Oid relid, PgColumnarOptions *opts);
extern int pgcolumnar_effective_stripe_row_limit(Oid relid);
extern int pgcolumnar_written_stripe_row_limit(Oid relid);

/* declared physical sort key (#288); List of pstrdup'd column names, NIL if
 * none is declared. Names (not attnums) so the value survives dump/restore. */

/* projection catalog (gap 26, format 2.2). List entries are PgColumnarProjection*
 * palloc'd in the current context, ordered by projection_id. */
extern List *PgColumnarListProjections(uint64 storageId);
extern void PgColumnarInsertProjectionRow(const PgColumnarProjection *proj);
/* The dumpable declaration behind a projection, keyed by regclass and stored as
 * column names so a dump and restore can carry it (#266). */
/* Every declaration for a relation, for the drop hook: a dropped table must not
 * leave rows behind whose regclass no longer resolves (#304). */
extern void PgColumnarDeleteProjectionRow(uint64 storageId, int projectionId);

/* whether a relation uses the columnar table access method */
extern bool PgColumnarIsColumnarRelation(Oid relid);

/*
 * A snapshot suitable for reading the columnar metadata catalog during a scan
 * or a DML operation. It is the given base snapshot with its command id
 * advanced so that catalog rows written earlier in this same transaction (even
 * in the current command, e.g. flushed at scan start) are visible, giving
 * same-transaction read-your-writes while preserving isolation from other
 * transactions (spec 9). Returns the base snapshot unchanged when it is not an
 * MVCC snapshot. The result is palloc'd in the current context and shares the
 * base snapshot's arrays, so the base must outlive it.
 */
extern Snapshot PgColumnarCatalogSnapshot(Snapshot base);

/* delete_vector catalog access (spec 7.5) */
extern List *PgColumnarReadDeleteVectorList(uint64 storageId, uint64 stripeId,
									 Snapshot snapshot);
extern void PgColumnarUpsertDeleteVector(uint64 storageId, DeleteVectorMetadata *rm);
extern uint64 PgColumnarGroupDeletedCount(uint64 storageId, NativeRowGroupMetadata *rg,
									  Snapshot snapshot);

/* -------------------------------------------------------------------------
 * writer (pgcolumnar_write_state.c)
 * ------------------------------------------------------------------------- */
typedef struct PgColumnarWriteState PgColumnarWriteState;

extern PgColumnarWriteState *PgColumnarGetWriteState(Relation rel);
extern uint64 PgColumnarWriteRow(PgColumnarWriteState *writeState, Relation rel,
							   Datum *values, bool *nulls);
extern void PgColumnarProjectionFanoutRow(Relation rel, PgColumnarWriteState *baseWs,
										uint64 rowNumber, Datum *values,
										bool *nulls);
extern void PgColumnarFlushWriteStateForRelation(Oid relid);

/* -------------------------------------------------------------------------
 * delete vector / delete tracking (pgcolumnar_delete_vector.c, spec 7.5, 9)
 * ------------------------------------------------------------------------- */
extern void PgColumnarFlushDeleteVectorForRelation(Relation rel);

/* -------------------------------------------------------------------------
 * reader (pgcolumnar_reader.c)
 * ------------------------------------------------------------------------- */
typedef struct PgColumnarReadState PgColumnarReadState;

extern PgColumnarReadState *PgColumnarBeginRead(Relation rel, Snapshot snapshot,
											ParallelTableScanDesc parallelScan,
											Bitmapset *projectedColumns,
											int nkeys, ScanKey keys);
/* like PgColumnarBeginRead but reads an explicit storage id with an explicit
 * tuple descriptor -- used to read a projection's storage (gap 26) */
extern PgColumnarReadState *PgColumnarBeginReadWithStorage(Relation rel,
													   Snapshot snapshot,
													   uint64 storageId,
													   TupleDesc tupdesc,
													   ParallelTableScanDesc parallelScan,
													   Bitmapset *projectedColumns,
													   int nkeys, ScanKey keys);
extern bool PgColumnarReadNextRow(PgColumnarReadState *readState,
								Datum *values, bool *nulls,
								uint64 *rowNumber);

/*
 * Late materialization (#452 Phase 1a). The filter is asked whether a row can
 * survive once the columns it reads are decoded, and returns true to keep it.
 * It must read only the columns the caller marked in qualCols.
 */
typedef bool (*PgColumnarRowFilter) (void *arg);

extern void PgColumnarRescanRead(PgColumnarReadState *readState);
extern void PgColumnarEndRead(PgColumnarReadState *readState);

/*
 * Batch-fold accessors (#289): expose the current loaded group's decoded buffer
 * so an ungrouped aggregate can fold it column-at-a-time instead of one Datum
 * tuple per row. See the block comment in pgcolumnar_reader.c for the contract.
 */

/*
 * Restrict a scan to a set of row groups (issue #149). Groups outside the set
 * are skipped without their bytes being read. Must be called before the first
 * PgColumnarReadNextRow; ngroups == 0 makes the scan return no rows.
 */
extern void PgColumnarReadRestrictToGroups(PgColumnarReadState *readState,
										 const uint64 *groupNumbers,
										 int ngroups);

/* Parquet export helpers, shared by the serial and parallel exporters
 * (src/pgcolumnar_parquet.c). */
extern int64 PgColumnarWriteParquetFile(Relation rel, Snapshot snapshot,
									  const char *filepath,
									  const uint64 *restrictGroups,
									  int nRestrictGroups);
extern void PgColumnarParquetCheckExportable(Relation rel);

/*
 * Column projection on an already-opened reader (#413). The table-AM scan
 * interface has nowhere to carry a projection, so a reader obtained through it
 * reads every column; a caller that knows better narrows it here, before the
 * first read. PgColumnarReadProjectedCount reports what the reader WILL decode,
 * read off colWanted, so a caller reporting a projection cannot report one it
 * failed to apply.
 */

/*
 * Parallel scan (gap 23): point the read state at a shared atomic that hands out
 * stripe indices, so several workers scanning the same relation each claim
 * distinct stripes. Set by the custom scan's DSM init callbacks.
 */
extern void PgColumnarReadSetParallelCounter(PgColumnarReadState *readState,
										   pg_atomic_uint32 *counter);

/*
 * Chunk-group skip counters for the current scan (spec 9), used by the custom
 * scan's EXPLAIN output to show how many chunk groups the min/max skip lists
 * removed. total = read + skipped over the groups the scan has reached.
 */
extern void PgColumnarReadStats(PgColumnarReadState *readState,
							  uint64 *groupsRead, uint64 *groupsSkipped,
							  uint64 *groupsTotal);
extern uint64 PgColumnarVectorsSkipped(PgColumnarReadState *readState);
extern uint64 PgColumnarVectorsDecoded(PgColumnarReadState *readState);
extern uint64 PgColumnarVectorDecodes(PgColumnarReadState *readState);
extern uint64 PgColumnarVectorsRuledOutByValue(PgColumnarReadState *readState);
extern uint64 PgColumnarZoneMapProbes(PgColumnarReadState *readState);

/* pgcolumnar.parallel_copy load dedup (#403 item 7) */
extern bool PgColumnarLoadFingerprintSeen(Oid relationOid,
										  const uint8 *fingerprint,
										  int fingerprintLen,
										  Snapshot snapshot);
extern void PgColumnarRecordLoadFingerprint(Oid relationOid,
											const uint8 *fingerprint,
											int fingerprintLen, int64 rows);

/*
 * How many of the scan keys the reader was handed became skip predicates it can
 * actually exclude a chunk group with (#479). Never larger than the scan-key
 * count EXPLAIN reports as "Columnar Pushed-Down Filters", and smaller whenever
 * pgcolumnar_make_predicates dropped a key -- a cross-type pair the opfamily has
 * no ordering proc for, a strategy outside BTLess..BTGreater, a null-test or
 * row-comparison key. Those keys are still counted as pushed down, and skip
 * nothing.
 */
extern int	PgColumnarReadUsablePredicates(PgColumnarReadState *readState);

/* cached base-liveness for a projection scan (gap 26): build once per scan,
 * probe per row with a binary search instead of a per-row catalog scan */
typedef struct PgColumnarLivenessCache PgColumnarLivenessCache;
/*
 * Fetch a single row by its 1-based row number (spec 6), for the table AM's
 * fetch-by-tid callback used by UPDATE. Fills values/nulls (by-reference values
 * are allocated in the current memory context) and returns true when the row
 * exists and is not marked deleted in the delete vector.
 */
extern bool PgColumnarReadRowByNumber(Relation rel, Snapshot snapshot,
									uint64 rowNumber, Datum *values, bool *nulls);

/*
 * Decode exactly the columns in `needed` (0-based attnums); every other column
 * reads as null. An empty or NULL set decodes nothing, which is what it says:
 * this deliberately has no "NULL means all" convention, because a Bitmapset
 * cannot distinguish empty from NULL, so a caller whose computed set came out
 * empty would silently get the opposite of what it asked for. For every column
 * call PgColumnarReadRowByNumber, which takes no set.
 *
 * Decoding every column regardless makes a wide table exceed the fetch cache's
 * size cap, so the entry is dropped after every fetch and the group is decoded
 * again for the next row (issue #157).
 */
extern bool PgColumnarReadRowByNumberCols(Relation rel, Snapshot snapshot,
										uint64 rowNumber, Datum *values,
										bool *nulls, Bitmapset *needed);

/* Is the row visible? Decodes nothing. */
extern bool PgColumnarRowIsLive(Relation rel, Snapshot snapshot,
							  uint64 rowNumber);

/* -------------------------------------------------------------------------
 * Decoded chunk group (pgcolumnar_vector.c aggregate path)
 *
 * A PgColumnarVector is one decoded chunk group: for each projected column, the
 * whole group's values and null flags as flat arrays, plus the per-row deleted
 * flag resolved from the delete vector. The vectorized aggregate builds a selection
 * vector over it; the scan itself is the scalar per-row
 * reader (PgColumnarReadNextRow).
 * ------------------------------------------------------------------------- */
typedef struct PgColumnarVector
{
	uint64		nrows;			/* rows in this chunk group */
	uint64		firstRowNumber; /* row number of local row 0 */
	Datum	  **values;			/* [natts]; values[c] is Datum[nrows] or NULL */
	bool	  **isnull;			/* [natts]; isnull[c] is bool[nrows] or NULL */
	bool	   *deleted;		/* [nrows]; true when row-mask-deleted */
} PgColumnarVector;

/* value stream encode/decode shared by writer and reader */
extern void PgColumnarEncodeValue(StringInfo buf, Form_pg_attribute att,
								Datum value);
extern Datum PgColumnarDecodeValue(Form_pg_attribute att, char **cursor,
								 const char *end, MemoryContext targetContext);

/* -------------------------------------------------------------------------
 * lightweight value-stream encodings (pgcolumnar_encoding.c, I1)
 * ------------------------------------------------------------------------- */
extern int PgColumnarEncodeChunk(const char *raw, uint32 rawLen,
							   Form_pg_attribute att, uint64 valueCount,
							   const char *fsstTable, uint32 fsstTableLen,
							   char **out, uint32 *outLen);
extern char *PgColumnarDecodeChunk(const char *enc, uint32 encLen,
								 int encodingType, Form_pg_attribute att,
								 uint64 valueCount, uint32 rawLen,
								 const char *fsstTable, uint32 fsstTableLen,
								 MemoryContext cx);
extern const char *PgColumnarEncodingName(int encodingType);

/*
 * Build one FSST symbol table for a whole column chunk from a sample of its
 * concatenated varlena value streams (E3b). Returns true and sets *tableOut /
 * *tableLenOut (palloc'd, serialized as [uint8 nSym][ nSym x (uint8 len, bytes)])
 * when a table was built; false for non-varlena columns or when no useful table
 * exists. The table is passed back into PgColumnarEncodeChunk / PgColumnarDecodeChunk
 * as fsstTable so the per-vector build cost is paid once per chunk.
 */
extern bool PgColumnarFsstBuildChunkTable(const char *corpus, uint32 corpusLen,
										Form_pg_attribute att,
										char **tableOut, uint32 *tableLenOut);
/* Cheap distinct-count pre-check: true when dictionary encoding wins outright, so
 * the costly FSST table build can be skipped with byte-identical output (#155). */
extern bool PgColumnarFsstDictWins(const char *corpus, uint32 corpusLen);

/*
 * True when encoding the chunk with the table just built is still a win after
 * the block compressor runs over the result, judged on the same sample the
 * table was trained on. The per-vector test inside PgColumnarEncodeChunk compares
 * uncompressed lengths, which is the wrong objective when a codec is configured:
 * FSST codes are smaller than repetitive text but much less compressible, so
 * FSST can win every vector and still enlarge the chunk. Callers pass NULL for
 * fsstTable when this returns false.
 */
extern bool PgColumnarFsstHelpsCompressed(const char *corpus, uint32 corpusLen,
										const char *table, uint32 tableLen,
										int compressionType,
										int compressionLevel);

/* -------------------------------------------------------------------------
 * per-chunk bloom filters (pgcolumnar_bloom.c, I7)
 * ------------------------------------------------------------------------- */
extern bool PgColumnarBloomBuild(const uint32 *hashes, uint32 n,
							   char **out, uint32 *outLen);
extern bool PgColumnarBloomProbe(const char *bloom, uint32 bloomLen, uint32 hash);

/*
 * True when a column of the given collation can carry a bloom filter (I7/gap 25):
 * a non-collatable type (InvalidOid), or a deterministic collation, so equal
 * values are byte-identical and hash consistently between build and probe.
 * Nondeterministic collations return false and are left unbloomed.
 */
extern bool PgColumnarCollationIsDeterministic(Oid collid);

/* -------------------------------------------------------------------------
 * compression-block run iterator (pgcolumnar_encoding.c, I2)
 *
 * Exposes a column chunk's (non-null) values as a sequence of (value, run
 * length) pairs so operators run once per run instead of once per row (I3
 * compressed execution). It iterates the decoded raw value stream and coalesces
 * adjacent equal fixed-width values, so a repetitive or run-length-encoded
 * column yields long runs. Fixed-width columns only.
 * ------------------------------------------------------------------------- */
typedef struct PgColumnarBlockReader
{
	const char *raw;			/* raw value stream (packed fixed-width values) */
	uint64		valueCount;		/* number of values in the stream */
	int			width;			/* bytes per value (attlen) */
	uint64		pos;			/* next value index */
} PgColumnarBlockReader;

extern void PgColumnarBlockReaderInit(PgColumnarBlockReader *br, const char *raw,
									uint64 valueCount, int width);

/*
 * Yield the next run: *valBytes points at the run's value (width bytes, valid
 * while the underlying stream is), *runLen is how many consecutive values equal
 * it. Returns false at end of stream.
 */
extern bool PgColumnarBlockNextRun(PgColumnarBlockReader *br,
								 const char **valBytes, uint64 *runLen);

/* -------------------------------------------------------------------------
 * compression (pgcolumnar_compression.c, spec 5)
 * ------------------------------------------------------------------------- */
extern void PgColumnarCompressValueStream(const char *raw, uint32 rawLen,
										int requestedType, int level,
										char **outData, uint32 *outLen,
										int *usedType, int *usedLevel);
extern char *PgColumnarDecompressValueStream(const char *comp, uint32 compLen,
										   int compressionType, uint32 rawLen,
										   MemoryContext targetContext);


/* -------------------------------------------------------------------------
 * advisory-lock classes (issue #430)
 *
 * locktag_field4 discriminates advisory lock spaces, and PostgreSQL's own
 * SQL-callable functions already own two values. From lockfuncs.c:
 *
 *     field4: 1 if using an int8 key, 2 if using 2 int4 keys
 *
 * so pg_advisory_lock(bigint) is class 1 and pg_advisory_lock(int4, int4) is
 * class 2, and NOTHING ELSE reachable from SQL sets this field. We used to use
 * 1 and 2, which made our internal locks bit-identical to a user's: the
 * unique-key lock was exactly pg_advisory_lock(indexOid, bucket). An
 * application holding that tag blocked our inserts, and we blocked it, silently
 * and with no bad query to point at.
 *
 * Any value above 2 is unreachable from SQL, so these three cannot be taken by
 * an application at all. They are distinct from each other as well, which is
 * what the previous comment in pgcolumnar_unique.c wanted and did not achieve:
 * there were three uses across two classes, and the storage-row lock shared
 * class 2 with the unique-key lock.
 *
 * These values are part of the on-the-wire lock protocol between backends, so
 * two backends running different builds would not exclude each other. That is a
 * restart rather than a rolling upgrade, which this extension already requires
 * because it loads through shared_preload_libraries.
 * ------------------------------------------------------------------------- */
#define PGCOLUMNAR_LOCKCLASS_DELETE_VECTOR	101
#define PGCOLUMNAR_LOCKCLASS_STORAGE_ROW	102
#define PGCOLUMNAR_LOCKCLASS_UNIQUE_KEY		103
#define PGCOLUMNAR_LOCKCLASS_ROW_IDENTITY	104

/* -------------------------------------------------------------------------
 * concurrent unique-key insert serialization (pgcolumnar_unique.c, issue #5)
 *
 * Before an inserted row is handed to the executor's index maintenance, the
 * table AM insert paths call PgColumnarLockUniqueKeys to take a transaction-
 * scoped advisory lock per applicable unique index key, so a concurrent
 * inserter of an equal key serializes behind this transaction until it commits
 * (and has therefore flushed its row), at which point the ordinary btree
 * uniqueness check catches the duplicate. See pgcolumnar_unique.c.
 * ------------------------------------------------------------------------- */
extern void PgColumnarLockUniqueKeys(Relation rel, TupleTableSlot *slot);
extern void PgColumnarUniqueInit(void);

/* -------------------------------------------------------------------------
 * concurrent same-row UPDATE/DELETE serialization (pgcolumnar_row_lock.c,
 * issue #5, the UPDATE facet). The write paths call PgColumnarRowWriteConflict
 * before marking the old row deleted, to serialize on the row identity and turn
 * a lost update into a retryable serialization_failure. See columnar_row_lock.c.
 * ------------------------------------------------------------------------- */
extern bool pgcolumnar_enable_row_update_lock;
extern int	pgcolumnar_row_lock_buckets;
extern bool PgColumnarRowWriteConflict(Relation rel, ItemPointer otid,
									   CommandId cid, bool wait,
									   TM_FailureData *tmfd, TM_Result *result);
extern void PgColumnarSerializeFlushRows(uint64 storageId, const uint64 *rows,
										 int nrows);

/* -------------------------------------------------------------------------
 * planner integration (pgcolumnar_customscan.c, spec 8.3, 9)
 * ------------------------------------------------------------------------- */

/*
 * The single registered CustomScanMethods, shared by the base custom scan and
 * the vectorized aggregate. The create-state callback dispatches on scanrelid:
 * a scanrelid==0 upper node is the vectorized aggregate.
 */
extern const CustomScanMethods pgcolumnar_scan_methods;
extern Node *PgColumnarCreateAggScanState(CustomScan *cscan);
extern Node *PgColumnarCreateGroupAggScanState(CustomScan *cscan);

/*
 * Build the chunk-group skip scan keys from a plan's restriction clauses.
 * Shared by the base custom scan and the vectorized aggregate (spec 9). Clauses
 * that are not simple "column op const" comparisons are ignored.
 */
/*
 * The chunk-group statistics every scan node reports, and the one place they are
 * printed (#495).
 *
 * Three nodes emit these lines -- the scalar custom scan and both vectorized
 * aggregates -- and each used to print its own copy. #484 had to add
 * "Usable Skip Predicates" in three places for exactly that reason, and said why
 * it could not do fewer: fixing one would leave a line of plan text meaning two
 * different things depending on which node ran. That invariant was held by
 * discipline; this holds it by structure, and pushdown_report.sh fails if a
 * fourth emitter appears.
 *
 * Two functions rather than one because the nodes interleave: the aggregate node
 * prints "Columnar Batch Fold" between the filter count and the group counters,
 * so combining them would reorder its plan output.
 */
typedef struct PgColumnarGroupStats
{
	int64		usableSkipPredicates;
	uint64		groupsTotal;
	uint64		groupsRead;
	uint64		groupsRemoved;
	uint64		vectorsSkipped;

	/*
	 * Vectors actually decoded (#452 phase 1b-i). Separate from vectorsSkipped
	 * because the two answered the same question only by accident: decode ran
	 * before the skip vector existed, so a skipped vector was decoded in full
	 * and merely not turned into Datums. "Skipped" therefore never implied "not
	 * decoded", and no counter could tell those apart until this one.
	 */
	uint64		vectorsDecoded;

	/*
	 * Vector decodes as (vector, column) PAIRS, where vectorsDecoded above counts
	 * vector POSITIONS. Two quantities, two labels (#493). Positions cannot see
	 * #452 phase 1b-ii at all: exact selection leaves the qual column decoded at
	 * every position and saves the payload columns, so the max across columns
	 * does not move while the work done falls by a factor of the column count.
	 */
	uint64		vectorDecodes;

	/*
	 * Zone-map catalog probes (#403 item 4). One index scan per (group, column)
	 * a predicate touches, so a conjunction whose EXCLUDING predicate is tried
	 * last probes every other predicate's column first, on every group it then
	 * throws away. This is the number predicate ordering moves; groups removed
	 * cannot see it, because the same groups are removed either way.
	 */
	uint64		zoneMapProbes;

	/*
	 * The subset of vectorsSkipped that exact selection ruled out (#452 phase
	 * 1b-ii), as opposed to the zone maps. Separate because "Vectors Skipped"
	 * alone stopped being one quantity the moment a second mechanism could set
	 * a bit in the same mask, and a reader cannot tell a zone map win from an
	 * exact-selection win without it (#493).
	 */
	uint64		vectorsRuledOutByValue;
} PgColumnarGroupStats;


extern ScanKey PgColumnarBuildScanKeys(List *qual, Index scanrelid,
									 TupleDesc tupdesc, int *nkeys);
extern bool PgColumnarQualsExactlyKeyed(List *qual, Index scanrelid,
									  TupleDesc tupdesc);

/*
 * How many chunk groups the planner samples when estimating how much a
 * restriction prunes (#461). Constant work per plan whatever the table's size:
 * evaluating every group is O(groups) catalog reads on every plan, including
 * plans that are discarded. Costing compares two plans, so it does not need
 * three digits of precision.
 */
#define PGCOLUMNAR_PRUNE_SAMPLE_GROUPS 32


/* -------------------------------------------------------------------------
 * vectorized aggregation and filtering (pgcolumnar_vector.c, spec 9)
 * ------------------------------------------------------------------------- */
extern void PgColumnarVectorInit(void);

/*
 * The vectorized predicate machinery that used to be declared here is private to
 * pgcolumnar_vector.c. PgColumnarVecRowPasses and PgColumnarVecSelect were exported
 * with no call site anywhere in the tree and are gone; see issue #200. What
 * remains of it is a convertibility probe the planner uses, and it has no
 * business outside that file.
 */

/* -------------------------------------------------------------------------
 * Reading a varlena header out of a packed value buffer
 * ------------------------------------------------------------------------- */

/*
 * VARSIZE_ANY for a pointer that may not be aligned.
 *
 * Values in a chunk's raw buffer are packed end to end with no alignment
 * padding, so a four-byte varlena header starts wherever the previous value
 * ended. VARSIZE_ANY reads that header by casting to varattrib_4b and loading a
 * uint32, which is undefined behaviour on an unaligned address: it happens to
 * work on x86_64 and is a SIGBUS on a strict-alignment target; unaligned reads
 * are covered by the sanitizer gate, though non-x86_64 architectures are
 * untested (docs/limitations.md).
 *
 * This is not a crafted-input problem. An ordinary INSERT of low-cardinality
 * text reports it six times under UBSAN, because encode_dict walks exactly such
 * a buffer. It was found by the #214 fuzzer only in the sense that the fuzzer is
 * why there was a sanitizer build to see it with.
 *
 * A one-byte header carries its length in the first byte, so it needs no
 * alignment and goes through the ordinary macro.
 *
 * The four-byte case decodes the copied header itself rather than handing an
 * aligned local back to VARSIZE_4B. That was the first attempt and it is also
 * undefined: VARSIZE_4B casts its argument to varattrib_4b, and a uint32 local
 * is not large enough for that type, which UBSAN reports as "insufficient space
 * for an object of type 'varattrib_4b'". Trading an alignment fault for a
 * type-size fault is not a fix. The shift and mask below mirror varatt.h
 * exactly, including its endianness split, which is the only part worth
 * duplicating.
 */
static inline uint32
PgColumnarVarSizeAnyUnaligned(const char *p)
{
	uint32		hdr;

	if (VARATT_IS_1B(p) || VARATT_IS_1B_E(p))
		return (uint32) VARSIZE_ANY(p);

	memcpy(&hdr, p, sizeof(hdr));
#ifdef WORDS_BIGENDIAN
	return hdr & 0x3FFFFFFF;
#else
	return (hdr >> 2) & 0x3FFFFFFF;
#endif
}

/*
 * Bounded variant, for a varlena decoded from native storage. The value's length
 * prefix is stored, not computed, so a corrupt native chunk (bit rot, or a
 * hand-written stripe) can declare a value that runs past its decoded buffer --
 * an out-of-bounds read -- and if the prefix carries the external-TOAST tag, the
 * value is later detoasted through a pointer that points at nothing. `end` is one
 * past the last valid byte of the value stream. This refuses a value that starts
 * at or past `end`, a 4-byte header whose bytes are not all in range, an external
 * TOAST tag (native storage stores only inline, detoasted values), or a length
 * that would read past `end`. A valid inline value is unaffected: its stored
 * length always fits. Returns the value's total size in bytes.
 */
static inline uint32
PgColumnarVarSizeAnyUnalignedBounded(const char *p, const char *end)
{
	uint32		len;

	if (p >= end)
		ereport(ERROR,
				(errcode(ERRCODE_DATA_CORRUPTED),
				 errmsg("pgcolumnar: varlena value starts at or past the value stream end")));
	if (VARATT_IS_EXTERNAL(p))
		ereport(ERROR,
				(errcode(ERRCODE_DATA_CORRUPTED),
				 errmsg("pgcolumnar: external TOAST pointer in a native value stream")));
	if (!VARATT_IS_1B(p) && (Size) (end - p) < sizeof(uint32))
		ereport(ERROR,
				(errcode(ERRCODE_DATA_CORRUPTED),
				 errmsg("pgcolumnar: varlena header runs past the value stream end")));
	len = PgColumnarVarSizeAnyUnaligned(p);
	if (len < 1 || (Size) (end - p) < (Size) len)
		ereport(ERROR,
				(errcode(ERRCODE_DATA_CORRUPTED),
				 errmsg("pgcolumnar: varlena value length runs past the value stream end")));
	return len;
}

#endif							/* PGCOLUMNAR_H */
