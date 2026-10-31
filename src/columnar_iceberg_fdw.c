/*-------------------------------------------------------------------------
 * columnar_iceberg_fdw.c
 *		A read-only foreign-data wrapper over an Apache Iceberg table (#388).
 *
 * iceberg_scan() is a bare set-returning function: no query predicate reaches
 * it, so it cannot prune. This FDW gives Iceberg a predicate-bearing scan node.
 * Its first job is IDENTITY-PARTITION PRUNING: a qual on an identity-partitioned
 * column removes whole data files -- whose partition value is read from the
 * manifest, already decoded and typed -- before they are opened. Everything
 * downstream (field-id projection, all delete kinds, name mapping, vended creds)
 * is the shared read path (PgColumnarIcebergScanCore); this file only captures
 * quals and filters the file list. Pruning is only ever an optimization: a file
 * that is not pruned is read normally, so a qual this FDW cannot decide never
 * changes the rows returned.
 *
 * See design/ISSUE_388_PRUNING.md. Non-identity transforms and data-file
 * min/max metrics pruning are later increments; until then such files are read.
 *-------------------------------------------------------------------------
 */
#include "postgres.h"

#include <sys/stat.h>

#include "access/reloptions.h"
#include "access/stratnum.h"
#include "catalog/pg_authid_d.h"
#include "catalog/pg_foreign_table.h"
#include "catalog/pg_type_d.h"
#include "commands/defrem.h"
#if PG_VERSION_NUM >= 180000
#include "commands/explain_format.h"
#include "commands/explain_state.h"
#else
#include "commands/explain.h"
#endif
#include "fmgr.h"
#include "foreign/fdwapi.h"
#include "foreign/foreign.h"
#include "miscadmin.h"
#include "nodes/makefuncs.h"
#include "optimizer/cost.h"
#include "optimizer/optimizer.h"
#include "optimizer/pathnode.h"
#include "optimizer/planmain.h"
#include "optimizer/restrictinfo.h"
#include "datatype/timestamp.h"	/* POSTGRES_EPOCH_JDATE, UNIX_EPOCH_JDATE */
#include "utils/acl.h"
#include "utils/builtins.h"
#include "utils/date.h"
#include "utils/rel.h"
#include "utils/timestamp.h"	/* timestamp2tm */
#include "utils/tuplestore.h"

/* Iceberg dates count days from the 1970 Unix epoch; PostgreSQL dates count from
 * 2000. Add this to a PG date to get the Iceberg day value a day() cell stores. */
#define ICE_DATE_EPOCH_OFFSET	(POSTGRES_EPOCH_JDATE - UNIX_EPOCH_JDATE)

/* Same offset in microseconds, to move a PG timestamp (micros from 2000) to the
 * 1970 epoch that Iceberg's day()/hour() count from. */
#define ICE_TS_EPOCH_OFFSET_US	\
	((int64) (POSTGRES_EPOCH_JDATE - UNIX_EPOCH_JDATE) * USECS_PER_DAY)

/* The coarse temporal transforms this FDW prunes on a TIMESTAMP column. */
typedef enum IceTemporalKind
{
	ICE_TEMPORAL_YEAR,
	ICE_TEMPORAL_MONTH,
	ICE_TEMPORAL_DAY,
	ICE_TEMPORAL_HOUR
}			IceTemporalKind;

#include "columnar_avro.h"
#include "columnar_compat.h"
#include "columnar_iceberg.h"

typedef struct IceFdwState
{
	char	   *metadata_path;
	TupleDesc	tupdesc;
	Tuplestorestate *tupstore;
	TupleTableSlot *readslot;	/* minimal-tuple slot to drain the tuplestore */
	bool		started;		/* the read+prune pass has run */

	/* pruning, compiled once in Begin */
	ForeignScanState *node;
	int		   *partAttno;		/* per partition-tuple position -> attno, or 0 */
	int			npart;
	int32		specid;			/* current spec; a file with another spec is read */
	List	   *partQuals;		/* compiled quals over identity-partition columns */
	TupleTableSlot *partSlot;
	List	   *metricQuals;	/* IceMetricQual*, min/max prune on int/bool cols */
	List	   *bucketQuals;	/* IceBucketQual*, equality prune on bucket parts */
	List	   *truncQuals;		/* IceTruncQual*, range prune on truncate[W] parts */
	List	   *dayQuals;		/* IceDayQual*, range prune on day() date parts */
	List	   *temporalQuals;	/* IceTemporalQual*, coarse year/month/day/hour on ts */
	int64		filesPruned;
	const bool *needTop;		/* projection mask over tupdesc, or NULL for all */
}			IceFdwState;

/*
 * A predicate a data file's column bounds can decide: "column <op> constant" on
 * an integer/boolean column, reduced to a btree strategy (normalized so the
 * column is on the left) and the constant as int64.
 */
typedef struct IceMetricQual
{
	int32		fieldid;		/* the column's Iceberg field id */
	int			strategy;		/* BTLess..BTGreater, column-on-left normalized */
	int64		constval;
}			IceMetricQual;

/*
 * A "bucket-column = constant" predicate a bucket[N] partition can decide. The
 * bucket of the constant does not depend on the file, so it is computed once:
 * `target` is bucket(const, N), and a file whose stored bucket cell at `pos`
 * differs from it cannot contain a matching row.
 */
typedef struct IceBucketQual
{
	int			pos;			/* partition-tuple position of the bucket cell */
	int32		target;			/* bucket(const, N) */
}			IceBucketQual;

/*
 * A "truncate-column <op> const" predicate a truncate[W] partition can decide.
 * A file's stored truncate cell V means its values lie in [V, V+W); the
 * predicate is decided against that range with the same min/max logic as metrics
 * (truncate is order-preserving).
 */
typedef struct IceTruncQual
{
	int			pos;			/* partition-tuple position of the truncate cell */
	int			w;				/* truncation width */
	int			strategy;		/* BTLess..BTGreater, column-on-left normalized */
	int64		constval;
}			IceTruncQual;

/*
 * A "date-column <op> const" predicate a day()-partitioned DATE column can
 * decide. day() stores one date per file as an Iceberg day int; `dayval` is the
 * constant converted to that same day scale, decided against the file's cell.
 */
typedef struct IceDayQual
{
	int			pos;			/* partition-tuple position of the day cell */
	int			strategy;		/* BTLess..BTGreater, column-on-left normalized */
	int64		dayval;			/* const as Iceberg days (PG date + epoch offset) */
}			IceDayQual;

/*
 * A "timestamp-column <op> const" predicate a COARSE temporal partition can
 * decide: year(), month(), day(), or hour() on a TIMESTAMP column. Each maps a
 * source value to an integer bucket that spans a range (a year, a month, a day,
 * an hour), so `bucket` is the constant mapped through the same transform and the
 * decision at the file's cell V is by the coarse rule (see ice_fdw_temporal_excludes):
 * at V == bucket the file straddles the constant and is never pruned.
 */
typedef struct IceTemporalQual
{
	int			pos;			/* partition-tuple position of the temporal cell */
	int			strategy;		/* BTLess..BTGreater, column-on-left normalized */
	int64		bucket;			/* const mapped through the transform */
}			IceTemporalQual;

/* murmur3 x86 32-bit (seed 0), the hash Iceberg's bucket transform uses. */
static uint32
ice_murmur3_32(const unsigned char *data, int len)
{
	uint32		h = 0;
	int			nblocks = len / 4;
	const uint32 c1 = 0xcc9e2d51;
	const uint32 c2 = 0x1b873593;
	int			i;
	const unsigned char *tail;
	uint32		k1 = 0;

	for (i = 0; i < nblocks; i++)
	{
		uint32		k = (uint32) data[i * 4] |
			((uint32) data[i * 4 + 1] << 8) |
			((uint32) data[i * 4 + 2] << 16) |
			((uint32) data[i * 4 + 3] << 24);

		k *= c1;
		k = (k << 15) | (k >> 17);
		k *= c2;
		h ^= k;
		h = (h << 13) | (h >> 19);
		h = h * 5 + 0xe6546b64;
	}
	tail = data + nblocks * 4;
	/* the tail (1..3 bytes); written without switch fall-through */
	if ((len & 3) >= 3)
		k1 ^= (uint32) tail[2] << 16;
	if ((len & 3) >= 2)
		k1 ^= (uint32) tail[1] << 8;
	if ((len & 3) >= 1)
	{
		k1 ^= (uint32) tail[0];
		k1 *= c1;
		k1 = (k1 << 15) | (k1 >> 17);
		k1 *= c2;
		h ^= k1;
	}
	h ^= (uint32) len;
	h ^= h >> 16;
	h *= 0x85ebca6b;
	h ^= h >> 13;
	h *= 0xc2b2ae35;
	h ^= h >> 16;
	return h;
}

/* Iceberg bucket[N] of the already-serialized value bytes. */
static int32
ice_bucket_of(const unsigned char *data, int len, int n)
{
	uint32		h = ice_murmur3_32(data, len);

	return (int32) ((h & 0x7fffffff) % (uint32) n);
}

/*
 * Compute bucket(const, N) for a bucket-partition source column, serializing the
 * constant the way Iceberg hashes it (int/long as 8-byte little-endian long,
 * string as UTF-8 bytes). Returns false for a type this increment does not hash,
 * so the file is not pruned.
 */
static bool
ice_fdw_bucket_of_const(Const *c, int n, int32 *out)
{
	if (c->constisnull)
		return false;
	switch (c->consttype)
	{
		case INT2OID:
		case INT4OID:
		case INT8OID:
			{
				int64		v = (c->consttype == INT8OID) ? DatumGetInt64(c->constvalue)
					: (c->consttype == INT4OID) ? (int64) DatumGetInt32(c->constvalue)
					: (int64) DatumGetInt16(c->constvalue);
				unsigned char b[8];
				int			i;

				for (i = 0; i < 8; i++)
					b[i] = (unsigned char) ((uint64) v >> (i * 8));
				*out = ice_bucket_of(b, 8, n);
				return true;
			}
		case TEXTOID:
		case VARCHAROID:
			{
				text	   *t = DatumGetTextPP(c->constvalue);
				int			len = VARSIZE_ANY_EXHDR(t);

				*out = ice_bucket_of((const unsigned char *) VARDATA_ANY(t), len, n);
				return true;
			}
		default:
			return false;
	}
}

/* a named option of a foreign table, or NULL */
static char *
ice_fdw_get_option(Oid foreigntableid, const char *name)
{
	ForeignTable *ft = GetForeignTable(foreigntableid);
	ListCell   *lc;

	foreach(lc, ft->options)
	{
		DefElem    *def = (DefElem *) lfirst(lc);

		if (strcmp(def->defname, name) == 0)
			return defGetString(def);
	}
	return NULL;
}

/* ------------------------------------------------------------------ planner */

static void
icefdwGetForeignRelSize(PlannerInfo *root, RelOptInfo *baserel,
						Oid foreigntableid)
{
	char	   *mdpath = ice_fdw_get_option(foreigntableid, "metadata_path");
	int64		est = 0;

	/*
	 * Estimate from the manifests the scan reads anyway: the sum of the live
	 * data files' record counts. A constant guess (the old 1000) mis-sizes a
	 * large table and corrupts every join decision above the scan. The metadata
	 * is small, and the scan opens it regardless, so reading it here is cheap.
	 *
	 * The sum is the base-table cardinality (baserel->tuples), not the output
	 * row count. Set it and let set_baserel_size_estimates apply the scan's
	 * restriction clauses (baserestrictinfo is populated by now), so a filtered
	 * scan estimates its filtered rows rather than the whole table.
	 */
	/*
	 * ONLY FOR A ROLE THE SCAN WOULD ADMIT. The executor refuses a caller without
	 * pg_read_server_files at icefdwBeginForeignScan, but planning ran first and
	 * read the metadata for anyone: EXPLAIN then told such a role how many rows a
	 * server-side table holds, and a metadata_path that did not exist failed
	 * planning with 58P01, so planning probed whether a server path exists. The
	 * Parquet wrapper has gated its planner the same way since #330. Others get
	 * the flat estimate and learn nothing from the plan.
	 */
	if (!has_privs_of_role(GetUserId(), ROLE_PG_READ_SERVER_FILES))
	{
		baserel->tuples = 1000.0;
		set_baserel_size_estimates(root, baserel);
		return;
	}
	if (mdpath != NULL)
		est = PgColumnarIcebergEstimateRows(mdpath);
	baserel->tuples = (est > 0) ? (double) est : 1.0;
	set_baserel_size_estimates(root, baserel);
}

static void
icefdwGetForeignPaths(PlannerInfo *root, RelOptInfo *baserel,
					  Oid foreigntableid)
{
	add_path(baserel, (Path *)
			 COLUMNAR_CREATE_FOREIGNSCAN_PATH(root, baserel,
											  NULL, baserel->rows,
											  0, baserel->rows,
											  NIL, NULL, NULL, NIL));
}

static ForeignScan *
icefdwGetForeignPlan(PlannerInfo *root, RelOptInfo *baserel,
					 Oid foreigntableid, ForeignPath *best_path,
					 List *tlist, List *scan_clauses, Plan *outer_plan)
{
	Bitmapset  *attrs = NULL;
	List	   *needed = NIL;
	int			x = -1;

	/*
	 * Clause evaluation stays with the executor: pruning here only drops whole
	 * files a partition value cannot match, so every clause is still recheckable
	 * and must remain in the plan qual.
	 */
	scan_clauses = extract_actual_clauses(scan_clauses, false);

	/*
	 * Projection pushdown: record the attributes the output tlist and the
	 * recheck quals reference, so the reader decodes only those columns of each
	 * surviving file. Passed as fdw_private and expanded to a per-column mask in
	 * BeginForeignScan. A whole-row reference (attno 0) forces every column.
	 */
	pull_varattnos((Node *) baserel->reltarget->exprs, baserel->relid, &attrs);
	pull_varattnos((Node *) scan_clauses, baserel->relid, &attrs);
	while ((x = bms_next_member(attrs, x)) >= 0)
		needed = lappend_int(needed, x + FirstLowInvalidHeapAttributeNumber);

	return make_foreignscan(tlist, scan_clauses, baserel->relid,
							NIL, needed, NIL, NIL, outer_plan);
}

/* ------------------------------------------------------------------- pruning */

/*
 * Compile the scan quals that reference ONLY identity-partition columns and hold
 * no volatile function -- the ones a file's partition value alone can decide.
 */
static List *
ice_fdw_partition_quals(ForeignScanState *node, TupleDesc tupdesc,
						const bool *partMask)
{
	ForeignScan *fs = (ForeignScan *) node->ss.ps.plan;
	List	   *compiled = NIL;
	ListCell   *lc;

	foreach(lc, fs->scan.plan.qual)
	{
		Node	   *clause = (Node *) lfirst(lc);
		Bitmapset  *attrs = NULL;
		bool		partitionOnly = true;
		int			x = -1;

		pull_varattnos(clause, fs->scan.scanrelid, &attrs);
		if (bms_is_empty(attrs))
			continue;
		while ((x = bms_next_member(attrs, x)) >= 0)
		{
			int			attno = x + FirstLowInvalidHeapAttributeNumber;

			if (attno < 1 || attno > tupdesc->natts || !partMask[attno - 1])
			{
				partitionOnly = false;
				break;
			}
		}
		if (!partitionOnly)
			continue;
		if (contain_volatile_functions(clause))
			continue;
		compiled = lappend(compiled, ExecInitQual(list_make1(clause),
												  (PlanState *) node));
	}
	return compiled;
}

/*
 * Can the column's declared typmod change a value the scan returns (#1790)? The
 * reader applies the typmod to every value: varchar(2) returns a stored 'eu  ' as
 * 'eu', and timestamp(0) returns 00:59:59.7 as 01:00:00. A partition value in the
 * manifest is the stored one, so it need not describe the returned rows, and the
 * FDW must not prune on such a column. A time or timestamp at full microsecond
 * precision changes nothing.
 */
static bool
ice_fdw_typmod_changes_values(TupleDesc tupdesc, int attno)
{
	Form_pg_attribute att = TupleDescAttr(tupdesc, attno - 1);

	if (att->atttypmod < 0)
		return false;
	if ((att->atttypid == TIMESTAMPOID || att->atttypid == TIMESTAMPTZOID ||
		 att->atttypid == TIMEOID) && att->atttypmod >= MAX_TIMESTAMP_PRECISION)
		return false;
	return true;
}

/* Is `typid` an identity-partition column type this increment can prune on? Only
 * these are put in the partition mask, so a qual over, say, a date-partitioned
 * column is never compiled and its files are read in full (sound). */
static bool
ice_fdw_type_supported(Oid typid)
{
	switch (typid)
	{
		case TEXTOID:
		case VARCHAROID:
		case INT2OID:
		case INT4OID:
		case INT8OID:
		case BOOLOID:
			return true;
		default:
			return false;
	}
}

/* Convert one already-transformed partition cell to a Datum of `typid`, for the
 * identity types this increment prunes. Sets *ok=false for a cell it cannot
 * convert (a type mismatch or an incomparable value); the caller must then treat
 * the file as UNDECIDABLE and not prune it -- a present-but-unconverted value is
 * unknown, NOT null, so a qual on it must never drop the file. */
static Datum
ice_fdw_cell_datum(const PgColumnarAvroPartCell *c, Oid typid, bool *ok)
{
	*ok = true;
	if (!c->comparable)
	{
		*ok = false;
		return (Datum) 0;
	}
	switch (typid)
	{
		case TEXTOID:
		case VARCHAROID:
			if (!c->is_bytes)
				break;
			return PointerGetDatum(cstring_to_text_with_len(c->bytes, c->blen));
		case INT2OID:
			if (c->is_bytes)
				break;
			return Int16GetDatum((int16) c->ival);
		case INT4OID:
			if (c->is_bytes)
				break;
			return Int32GetDatum((int32) c->ival);
		case INT8OID:
			if (c->is_bytes)
				break;
			return Int64GetDatum(c->ival);
		case BOOLOID:
			if (c->is_bytes)
				break;
			return BoolGetDatum(c->ival != 0);
		default:
			break;
	}
	*ok = false;
	return (Datum) 0;
}

/* the lower/upper bound bytes for `fieldid` in a bound map, or NULL */
static const PgColumnarAvroBound *
ice_fdw_find_bound(const PgColumnarAvroBound *b, int n, int32 fieldid)
{
	int			i;

	for (i = 0; i < n; i++)
		if (b[i].field_id == fieldid)
			return &b[i];
	return NULL;
}

/* Decode an Iceberg single-value binary integer/boolean bound to int64 by its
 * byte width (bool 1, int 4, long 8, all little-endian). Returns false for any
 * other width, so an unexpected encoding never prunes. */
static bool
ice_fdw_bound_int(const PgColumnarAvroBound *b, int64 *out)
{
	const unsigned char *p = (const unsigned char *) b->bytes;

	if (b->bytes == NULL)
		return false;
	if (b->blen == 1)
		*out = (int64) p[0];
	else if (b->blen == 4)
		*out = (int64) (int32) ((uint32) p[0] | ((uint32) p[1] << 8) |
								((uint32) p[2] << 16) | ((uint32) p[3] << 24));
	else if (b->blen == 8)
		*out = (int64) ((uint64) p[0] | ((uint64) p[1] << 8) |
						((uint64) p[2] << 16) | ((uint64) p[3] << 24) |
						((uint64) p[4] << 32) | ((uint64) p[5] << 40) |
						((uint64) p[6] << 48) | ((uint64) p[7] << 56));
	else
		return false;
	return true;
}

/* Does "column <strategy> const" have no satisfying value in [lo, hi]? Sound and
 * conservative: only returns true when the file provably yields no matching row. */
static bool
ice_fdw_metric_excludes(int strategy, int64 c, int64 lo, int64 hi)
{
	switch (strategy)
	{
		case BTEqualStrategyNumber:
			return (c < lo || c > hi);
		case BTLessStrategyNumber:	/* col < c : possible iff lo < c */
			return (lo >= c);
		case BTLessEqualStrategyNumber:	/* col <= c : possible iff lo <= c */
			return (lo > c);
		case BTGreaterStrategyNumber:	/* col > c : possible iff hi > c */
			return (hi <= c);
		case BTGreaterEqualStrategyNumber:	/* col >= c : possible iff hi >= c */
			return (hi < c);
		default:
			return false;
	}
}

/*
 * The coarse-transform analogue of ice_fdw_metric_excludes, for a file whose
 * temporal cell is the whole bucket V and whose predicate constant maps to bucket
 * b. A file in bucket V holds every source value that transforms to V (a range),
 * so at V == b the file straddles the constant and CANNOT be excluded; only a
 * bucket strictly past the constant is. This is strictly weaker than the exact
 * [V, V] test, which is why day() on a date (an exact one-to-one bucket) keeps
 * its own tighter path.
 */
static bool
ice_fdw_temporal_excludes(int strategy, int64 b, int64 v)
{
	switch (strategy)
	{
		case BTEqualStrategyNumber:
			return (v != b);
		case BTLessStrategyNumber:	/* col < c : some value < c iff V <= b */
		case BTLessEqualStrategyNumber: /* col <= c : some value <= c iff V <= b */
			return (v > b);
		case BTGreaterStrategyNumber:	/* col > c : some value > c iff V >= b */
		case BTGreaterEqualStrategyNumber:	/* col >= c : some value >= c iff V >= b */
			return (v < b);
		default:
			return false;
	}
}

/* Extract an int2/int4/int8/bool Const as int64; false for any other type. */
static bool
ice_fdw_const_int(Const *c, int64 *out)
{
	if (c->constisnull)
		return false;
	switch (c->consttype)
	{
		case BOOLOID:
			*out = DatumGetBool(c->constvalue) ? 1 : 0;
			return true;
		case INT2OID:
			*out = (int64) DatumGetInt16(c->constvalue);
			return true;
		case INT4OID:
			*out = (int64) DatumGetInt32(c->constvalue);
			return true;
		case INT8OID:
			*out = DatumGetInt64(c->constvalue);
			return true;
		default:
			return false;
	}
}

/*
 * Compile the scan's "column <op> const" predicates over integer/boolean columns
 * into IceMetricQuals a data file's min/max bounds can decide. Any column with a
 * field id and a supported type qualifies (not only partition columns), so a
 * predicate on an unpartitioned column can still prune whole files by metrics.
 */
static List *
ice_fdw_metric_quals(ForeignScanState *node, TupleDesc tupdesc,
					 const int *attFieldId)
{
	ForeignScan *fs = (ForeignScan *) node->ss.ps.plan;
	List	   *out = NIL;
	ListCell   *lc;

	foreach(lc, fs->scan.plan.qual)
	{
		OpExpr	   *op = (OpExpr *) lfirst(lc);
		Node	   *larg;
		Node	   *rarg;
		Var		   *var;
		Const	   *con;
		bool		varLeft;
		int			strategy = 0;
		int64		cval;
		Oid			ctype;
		ListCell   *ic;
		List	   *interp;
		IceMetricQual *mq;

		if (!IsA(op, OpExpr) || list_length(op->args) != 2)
			continue;
		larg = (Node *) linitial(op->args);
		rarg = (Node *) lsecond(op->args);
		if (IsA(larg, Var) && IsA(rarg, Const))
		{
			var = (Var *) larg;
			con = (Const *) rarg;
			varLeft = true;
		}
		else if (IsA(larg, Const) && IsA(rarg, Var))
		{
			var = (Var *) rarg;
			con = (Const *) larg;
			varLeft = false;
		}
		else
			continue;
		if (var->varattno < 1 || var->varattno > tupdesc->natts ||
			attFieldId[var->varattno - 1] == 0)
			continue;
		ctype = TupleDescAttr(tupdesc, var->varattno - 1)->atttypid;
		if (ctype != INT2OID && ctype != INT4OID && ctype != INT8OID &&
			ctype != BOOLOID)
			continue;
		if (contain_volatile_functions((Node *) op))
			continue;
		if (!ice_fdw_const_int(con, &cval))
			continue;

		interp = PgColumnarGetOpInterpretation(op->opno);
		foreach(ic, interp)
		{
			PgColumnarOpInterpretation *o = (PgColumnarOpInterpretation *) lfirst(ic);
			int			s = PgColumnarOpInterpStrategy(o);

			if (s >= BTLessStrategyNumber && s <= BTGreaterStrategyNumber)
			{
				strategy = s;
				break;
			}
		}
		if (strategy == 0)
			continue;
		/* normalize so the column is on the left: "c op var" -> "var flip(op) c" */
		if (!varLeft)
		{
			switch (strategy)
			{
				case BTLessStrategyNumber:
					strategy = BTGreaterStrategyNumber;
					break;
				case BTLessEqualStrategyNumber:
					strategy = BTGreaterEqualStrategyNumber;
					break;
				case BTGreaterStrategyNumber:
					strategy = BTLessStrategyNumber;
					break;
				case BTGreaterEqualStrategyNumber:
					strategy = BTLessEqualStrategyNumber;
					break;
				default:
					break;		/* BTEqual is symmetric */
			}
		}

		mq = (IceMetricQual *) palloc(sizeof(IceMetricQual));
		mq->fieldid = attFieldId[var->varattno - 1];
		mq->strategy = strategy;
		mq->constval = cval;
		out = lappend(out, mq);
	}
	return out;
}

/*
 * Compile "bucket-source-column = const" predicates into IceBucketQuals. A
 * bucket[N] partition stores the bucket of the source value; an equality
 * predicate on the source column can prune a file whose stored bucket differs
 * from bucket(const, N). Only "=" prunes (the hash destroys order).
 */
static List *
ice_fdw_bucket_quals(ForeignScanState *node, TupleDesc tupdesc,
					 const int *bpos, const int *battno, const int *bn, int bcount)
{
	ForeignScan *fs = (ForeignScan *) node->ss.ps.plan;
	List	   *out = NIL;
	ListCell   *lc;

	if (bcount == 0)
		return NIL;

	foreach(lc, fs->scan.plan.qual)
	{
		OpExpr	   *op = (OpExpr *) lfirst(lc);
		Node	   *larg;
		Node	   *rarg;
		Var		   *var;
		Const	   *con;
		int			strategy = 0;
		ListCell   *ic;
		List	   *interp;
		int			bi;

		if (!IsA(op, OpExpr) || list_length(op->args) != 2)
			continue;
		larg = (Node *) linitial(op->args);
		rarg = (Node *) lsecond(op->args);
		if (IsA(larg, Var) && IsA(rarg, Const))
		{
			var = (Var *) larg;
			con = (Const *) rarg;
		}
		else if (IsA(larg, Const) && IsA(rarg, Var))
		{
			var = (Var *) rarg;
			con = (Const *) larg;
		}
		else
			continue;
		if (var->varattno < 1 || contain_volatile_functions((Node *) op))
			continue;
		/*
		 * The stored bucket need not match a value the typmod changed (#1790).
		 * Not reachable today: the only bucket source types with a typmod are
		 * varchar(n), and "=" on varchar wraps the Var in a cast to text, which
		 * the test above rejects. The guard keeps this sound if that changes.
		 */
		if (var->varattno <= tupdesc->natts &&
			ice_fdw_typmod_changes_values(tupdesc, var->varattno))
			continue;

		interp = PgColumnarGetOpInterpretation(op->opno);
		foreach(ic, interp)
		{
			PgColumnarOpInterpretation *o = (PgColumnarOpInterpretation *) lfirst(ic);

			strategy = PgColumnarOpInterpStrategy(o);
			if (strategy == BTEqualStrategyNumber)
				break;
			strategy = 0;
		}
		if (strategy != BTEqualStrategyNumber)
			continue;			/* bucket prunes on equality only */

		/* is this Var the source column of a bucket partition field? */
		for (bi = 0; bi < bcount; bi++)
		{
			int32		target;
			IceBucketQual *bq;

			if (battno[bi] != var->varattno)
				continue;
			if (!ice_fdw_bucket_of_const(con, bn[bi], &target))
				continue;		/* unhashable const type: cannot prune */
			bq = (IceBucketQual *) palloc(sizeof(IceBucketQual));
			bq->pos = bpos[bi];
			bq->target = target;
			out = lappend(out, bq);
		}
	}
	return out;
}

/*
 * Compile "truncate-source-column <op> const" predicates over integer columns
 * into IceTruncQuals. A truncate[W] partition is order-preserving, so any btree
 * comparison prunes: a file's cell V bounds its values to [V, V+W).
 */
static List *
ice_fdw_trunc_quals(ForeignScanState *node, TupleDesc tupdesc,
					const int *tpos, const int *tattno, const int *tw, int tcount)
{
	ForeignScan *fs = (ForeignScan *) node->ss.ps.plan;
	List	   *out = NIL;
	ListCell   *lc;

	if (tcount == 0)
		return NIL;

	foreach(lc, fs->scan.plan.qual)
	{
		OpExpr	   *op = (OpExpr *) lfirst(lc);
		Node	   *larg;
		Node	   *rarg;
		Var		   *var;
		Const	   *con;
		bool		varLeft;
		int			strategy = 0;
		int64		cval;
		Oid			ctype;
		ListCell   *ic;
		List	   *interp;
		int			ti;

		if (!IsA(op, OpExpr) || list_length(op->args) != 2)
			continue;
		larg = (Node *) linitial(op->args);
		rarg = (Node *) lsecond(op->args);
		if (IsA(larg, Var) && IsA(rarg, Const))
		{
			var = (Var *) larg;
			con = (Const *) rarg;
			varLeft = true;
		}
		else if (IsA(larg, Const) && IsA(rarg, Var))
		{
			var = (Var *) rarg;
			con = (Const *) larg;
			varLeft = false;
		}
		else
			continue;
		if (var->varattno < 1 || var->varattno > tupdesc->natts ||
			contain_volatile_functions((Node *) op))
			continue;
		ctype = TupleDescAttr(tupdesc, var->varattno - 1)->atttypid;
		if (ctype != INT2OID && ctype != INT4OID && ctype != INT8OID)
			continue;			/* truncate on non-int types deferred */
		if (!ice_fdw_const_int(con, &cval))
			continue;

		interp = PgColumnarGetOpInterpretation(op->opno);
		foreach(ic, interp)
		{
			PgColumnarOpInterpretation *o = (PgColumnarOpInterpretation *) lfirst(ic);
			int			s = PgColumnarOpInterpStrategy(o);

			if (s >= BTLessStrategyNumber && s <= BTGreaterStrategyNumber)
			{
				strategy = s;
				break;
			}
		}
		if (strategy == 0)
			continue;
		if (!varLeft)
		{
			switch (strategy)
			{
				case BTLessStrategyNumber:
					strategy = BTGreaterStrategyNumber;
					break;
				case BTLessEqualStrategyNumber:
					strategy = BTGreaterEqualStrategyNumber;
					break;
				case BTGreaterStrategyNumber:
					strategy = BTLessStrategyNumber;
					break;
				case BTGreaterEqualStrategyNumber:
					strategy = BTLessEqualStrategyNumber;
					break;
				default:
					break;
			}
		}

		for (ti = 0; ti < tcount; ti++)
		{
			IceTruncQual *tq;

			if (tattno[ti] != var->varattno)
				continue;
			tq = (IceTruncQual *) palloc(sizeof(IceTruncQual));
			tq->pos = tpos[ti];
			tq->w = tw[ti];
			tq->strategy = strategy;
			tq->constval = cval;
			out = lappend(out, tq);
		}
	}
	return out;
}

/* Floor division (toward negative infinity), matching Iceberg's day()/hour(). C
 * integer division truncates toward zero, so a pre-1970 value needs the fix-up. */
static int64
ice_floordiv(int64 a, int64 b)
{
	int64		q = a / b;

	if ((a % b != 0) && ((a < 0) != (b < 0)))
		q--;
	return q;
}

/*
 * Map a PostgreSQL timestamp (micros from 2000) to the integer partition bucket
 * an Iceberg `kind` transform stores, all from the 1970 epoch in UTC. Returns
 * false for a non-finite timestamp (infinity), which has no bucket and must not
 * prune. year()/month() decompose the calendar; day()/hour() are floors of the
 * elapsed micros.
 */
static bool
ice_temporal_bucket(IceTemporalKind kind, Timestamp ts, int64 *out)
{
	if (TIMESTAMP_NOT_FINITE(ts))
		return false;

	if (kind == ICE_TEMPORAL_YEAR || kind == ICE_TEMPORAL_MONTH)
	{
		struct pg_tm tm;
		fsec_t		fsec;

		if (timestamp2tm(ts, NULL, &tm, &fsec, NULL, NULL) != 0)
			return false;
		if (kind == ICE_TEMPORAL_YEAR)
			*out = (int64) tm.tm_year - 1970;
		else
			*out = ((int64) tm.tm_year - 1970) * 12 + (tm.tm_mon - 1);
	}
	else
	{
		int64		us = ts + ICE_TS_EPOCH_OFFSET_US;

		if (kind == ICE_TEMPORAL_DAY)
			*out = ice_floordiv(us, USECS_PER_DAY);
		else					/* ICE_TEMPORAL_HOUR */
			*out = ice_floordiv(us, USECS_PER_HOUR);
	}
	return true;
}

/*
 * The source column types this coarse path prunes for a given transform. A
 * timestamp or timestamptz column takes all four transforms; a DATE column takes
 * year() and month() (day() on a DATE keeps the exact ice_fdw_day_quals path, and
 * hour() on a DATE is not a valid Iceberg transform).
 */
static bool
ice_temporal_source_ok(IceTemporalKind kind, Oid typid)
{
	if (typid == TIMESTAMPOID || typid == TIMESTAMPTZOID)
		return true;
	if (typid == DATEOID)
		return (kind == ICE_TEMPORAL_YEAR || kind == ICE_TEMPORAL_MONTH);
	return false;
}

/*
 * Convert a DATE/TIMESTAMP/TIMESTAMPTZ constant to a Timestamp (micros from
 * 2000) for ice_temporal_bucket. A timestamptz's stored value is already UTC
 * micros from 2000, the epoch Iceberg transforms count from, so it needs no
 * shift. Returns false for a non-finite or out-of-range value, which has no
 * bucket and must not prune.
 */
static bool
ice_const_to_timestamp(Oid typid, Datum value, Timestamp *out)
{
	if (typid == TIMESTAMPOID)
	{
		Timestamp	t = DatumGetTimestamp(value);

		if (TIMESTAMP_NOT_FINITE(t))
			return false;
		*out = t;
	}
	else if (typid == TIMESTAMPTZOID)
	{
		TimestampTz t = DatumGetTimestampTz(value);

		if (TIMESTAMP_NOT_FINITE(t))
			return false;
		*out = (Timestamp) t;	/* same int64: UTC micros from 2000 */
	}
	else						/* DATEOID: midnight of the day, micros from 2000 */
	{
		DateADT		d = DatumGetDateADT(value);
		int64		lim = PG_INT64_MAX / USECS_PER_DAY;

		if (DATE_NOT_FINITE(d) || (int64) d > lim || (int64) d < -lim)
			return false;		/* infinite or out of timestamp range */
		*out = (Timestamp) ((int64) d * USECS_PER_DAY);
	}
	return true;
}

/*
 * Compile "column <op> const" predicates over a COARSE temporal partition
 * (year/month/day/hour) into IceTemporalQuals. Each transform is
 * order-preserving, so the constant is mapped through the same transform to a
 * bucket and the file's cell is decided by the coarse rule. Sources: a timestamp
 * or timestamptz column (all four), or a DATE column (year/month). day() on a
 * DATE keeps its exact path. The constant must have the column's own type.
 */
static List *
ice_fdw_temporal_quals(ForeignScanState *node, TupleDesc tupdesc,
					   IceTemporalKind kind, const int *tpos, const int *tattno,
					   int tcount)
{
	ForeignScan *fs = (ForeignScan *) node->ss.ps.plan;
	List	   *out = NIL;
	ListCell   *lc;

	if (tcount == 0)
		return NIL;

	foreach(lc, fs->scan.plan.qual)
	{
		OpExpr	   *op = (OpExpr *) lfirst(lc);
		Node	   *larg;
		Node	   *rarg;
		Var		   *var;
		Const	   *con;
		bool		varLeft;
		int			strategy = 0;
		int64		bucket;
		ListCell   *ic;
		List	   *interp;
		int			ti;

		if (!IsA(op, OpExpr) || list_length(op->args) != 2)
			continue;
		larg = (Node *) linitial(op->args);
		rarg = (Node *) lsecond(op->args);
		if (IsA(larg, Var) && IsA(rarg, Const))
		{
			var = (Var *) larg;
			con = (Const *) rarg;
			varLeft = true;
		}
		else if (IsA(larg, Const) && IsA(rarg, Var))
		{
			var = (Var *) rarg;
			con = (Const *) larg;
			varLeft = false;
		}
		else
			continue;
		if (var->varattno < 1 || var->varattno > tupdesc->natts ||
			contain_volatile_functions((Node *) op))
			continue;
		{
			Oid			atttypid = TupleDescAttr(tupdesc, var->varattno - 1)->atttypid;
			Timestamp	cts;

			if (!ice_temporal_source_ok(kind, atttypid))
				continue;
			if (ice_fdw_typmod_changes_values(tupdesc, var->varattno))
				continue;		/* the stored bucket need not match (#1790) */
			if (con->constisnull || con->consttype != atttypid)
				continue;		/* only a constant of the column's own type */
			if (!ice_const_to_timestamp(atttypid, con->constvalue, &cts))
				continue;		/* non-finite/out-of-range const: cannot prune */
			if (!ice_temporal_bucket(kind, cts, &bucket))
				continue;
		}

		interp = PgColumnarGetOpInterpretation(op->opno);
		foreach(ic, interp)
		{
			PgColumnarOpInterpretation *o = (PgColumnarOpInterpretation *) lfirst(ic);
			int			s = PgColumnarOpInterpStrategy(o);

			if (s >= BTLessStrategyNumber && s <= BTGreaterStrategyNumber)
			{
				strategy = s;
				break;
			}
		}
		if (strategy == 0)
			continue;
		if (!varLeft)
		{
			switch (strategy)
			{
				case BTLessStrategyNumber:
					strategy = BTGreaterStrategyNumber;
					break;
				case BTLessEqualStrategyNumber:
					strategy = BTGreaterEqualStrategyNumber;
					break;
				case BTGreaterStrategyNumber:
					strategy = BTLessStrategyNumber;
					break;
				case BTGreaterEqualStrategyNumber:
					strategy = BTLessEqualStrategyNumber;
					break;
				default:
					break;
			}
		}

		for (ti = 0; ti < tcount; ti++)
		{
			IceTemporalQual *tq;

			if (tattno[ti] != var->varattno)
				continue;
			tq = (IceTemporalQual *) palloc(sizeof(IceTemporalQual));
			tq->pos = tpos[ti];
			tq->strategy = strategy;
			tq->bucket = bucket;
			out = lappend(out, tq);
		}
	}
	return out;
}

/*
 * Compile "date-column <op> const" predicates over day()-partitioned DATE
 * columns into IceDayQuals. day() is order-preserving; the constant is converted
 * from a PostgreSQL date (days from 2000) to the Iceberg day scale (from 1970).
 */
static List *
ice_fdw_day_quals(ForeignScanState *node, TupleDesc tupdesc,
				  const int *dpos, const int *dattno, int dcount)
{
	ForeignScan *fs = (ForeignScan *) node->ss.ps.plan;
	List	   *out = NIL;
	ListCell   *lc;

	if (dcount == 0)
		return NIL;

	foreach(lc, fs->scan.plan.qual)
	{
		OpExpr	   *op = (OpExpr *) lfirst(lc);
		Node	   *larg;
		Node	   *rarg;
		Var		   *var;
		Const	   *con;
		bool		varLeft;
		int			strategy = 0;
		ListCell   *ic;
		List	   *interp;
		int			di;

		if (!IsA(op, OpExpr) || list_length(op->args) != 2)
			continue;
		larg = (Node *) linitial(op->args);
		rarg = (Node *) lsecond(op->args);
		if (IsA(larg, Var) && IsA(rarg, Const))
		{
			var = (Var *) larg;
			con = (Const *) rarg;
			varLeft = true;
		}
		else if (IsA(larg, Const) && IsA(rarg, Var))
		{
			var = (Var *) rarg;
			con = (Const *) larg;
			varLeft = false;
		}
		else
			continue;
		if (var->varattno < 1 || var->varattno > tupdesc->natts ||
			contain_volatile_functions((Node *) op))
			continue;
		if (TupleDescAttr(tupdesc, var->varattno - 1)->atttypid != DATEOID)
			continue;			/* day() on timestamp/other deferred */
		if (con->constisnull || con->consttype != DATEOID)
			continue;

		interp = PgColumnarGetOpInterpretation(op->opno);
		foreach(ic, interp)
		{
			PgColumnarOpInterpretation *o = (PgColumnarOpInterpretation *) lfirst(ic);
			int			s = PgColumnarOpInterpStrategy(o);

			if (s >= BTLessStrategyNumber && s <= BTGreaterStrategyNumber)
			{
				strategy = s;
				break;
			}
		}
		if (strategy == 0)
			continue;
		if (!varLeft)
		{
			switch (strategy)
			{
				case BTLessStrategyNumber:
					strategy = BTGreaterStrategyNumber;
					break;
				case BTLessEqualStrategyNumber:
					strategy = BTGreaterEqualStrategyNumber;
					break;
				case BTGreaterStrategyNumber:
					strategy = BTLessStrategyNumber;
					break;
				case BTGreaterEqualStrategyNumber:
					strategy = BTLessEqualStrategyNumber;
					break;
				default:
					break;
			}
		}

		for (di = 0; di < dcount; di++)
		{
			IceDayQual *dq;

			if (dattno[di] != var->varattno)
				continue;
			dq = (IceDayQual *) palloc(sizeof(IceDayQual));
			dq->pos = dpos[di];
			dq->strategy = strategy;
			dq->dayval = (int64) DatumGetDateADT(con->constvalue) +
				ICE_DATE_EPOCH_OFFSET;
			out = lappend(out, dq);
		}
	}
	return out;
}

/*
 * The per-file filter passed to PgColumnarIcebergScanCore: return true to prune
 * (skip) a data file that provably yields no matching row -- because its column
 * min/max bounds exclude a predicate, or its identity-partition value does. A
 * file written under a different partition spec, or with a missing bound or an
 * incomparable/unsupported partition cell, is never pruned (it is read and its
 * rows are re-filtered by the recheckable qual).
 */
static bool
ice_fdw_file_excludes(void *arg, const PgColumnarIceFileMeta *meta)
{
	IceFdwState *st = (IceFdwState *) arg;
	ExprContext *econtext;
	TupleDesc	tupdesc = st->tupdesc;
	ListCell   *lc;
	int			i;
	int			k;

	/* metrics pruning: a data file whose [lower, upper] for a column excludes
	 * the predicate over it yields no matching row (bounds present for both). */
	foreach(lc, st->metricQuals)
	{
		IceMetricQual *mq = (IceMetricQual *) lfirst(lc);
		const PgColumnarAvroBound *lb = ice_fdw_find_bound(meta->lower, meta->nlower,
														   mq->fieldid);
		const PgColumnarAvroBound *ub = ice_fdw_find_bound(meta->upper, meta->nupper,
														   mq->fieldid);
		int64		lo;
		int64		hi;

		if (lb == NULL || ub == NULL)
			continue;			/* no bounds for this column: cannot decide */
		if (!ice_fdw_bound_int(lb, &lo) || !ice_fdw_bound_int(ub, &hi))
			continue;			/* unexpected width: do not prune */
		if (lo > hi)
			continue;			/* a nonsense bound: never prune on it */
		if (ice_fdw_metric_excludes(mq->strategy, mq->constval, lo, hi))
			return true;
	}

	/* bucket pruning: an equality predicate on a bucket-partitioned source
	 * column excludes a file whose stored bucket differs from bucket(const). */
	if (st->bucketQuals != NIL && meta->spec_id == st->specid)
	{
		foreach(lc, st->bucketQuals)
		{
			IceBucketQual *bq = (IceBucketQual *) lfirst(lc);

			if (bq->pos < 0 || bq->pos >= meta->ncells)
				continue;
			/* the stored cell is the bucket number: an integer, non-null */
			if (meta->cells[bq->pos].isnull ||
				meta->cells[bq->pos].is_bytes ||
				!meta->cells[bq->pos].comparable)
				continue;
			if (meta->cells[bq->pos].ival != bq->target)
				return true;
		}
	}

	/* truncate pruning: a truncate[W] cell V bounds the file's values to
	 * [V, V+W); decide the predicate over that range (order-preserving). */
	if (st->truncQuals != NIL && meta->spec_id == st->specid)
	{
		foreach(lc, st->truncQuals)
		{
			IceTruncQual *tq = (IceTruncQual *) lfirst(lc);
			int64		v;
			int64		hi;

			if (tq->pos < 0 || tq->pos >= meta->ncells)
				continue;
			if (meta->cells[tq->pos].isnull || meta->cells[tq->pos].is_bytes ||
				!meta->cells[tq->pos].comparable)
				continue;
			v = meta->cells[tq->pos].ival;
			if (v > PG_INT64_MAX - tq->w)	/* overflow guard: do not prune */
				continue;
			hi = v + tq->w - 1;
			if (ice_fdw_metric_excludes(tq->strategy, tq->constval, v, hi))
				return true;
		}
	}

	/* temporal day() pruning: a day() cell V is the file's single date (Iceberg
	 * days); decide the predicate against [V, V]. */
	if (st->dayQuals != NIL && meta->spec_id == st->specid)
	{
		foreach(lc, st->dayQuals)
		{
			IceDayQual *dq = (IceDayQual *) lfirst(lc);
			int64		v;

			if (dq->pos < 0 || dq->pos >= meta->ncells)
				continue;
			if (meta->cells[dq->pos].isnull || meta->cells[dq->pos].is_bytes ||
				!meta->cells[dq->pos].comparable)
				continue;
			v = meta->cells[dq->pos].ival;
			if (ice_fdw_metric_excludes(dq->strategy, dq->dayval, v, v))
				return true;
		}
	}

	/* coarse temporal (year/month/day/hour on a timestamp): the cell V is the
	 * file's bucket, spanning a range of source values. Prune only when the whole
	 * bucket lies on the excluded side of the constant's bucket b; at V == b the
	 * file straddles the constant and is read (see ice_fdw_temporal_excludes). */
	if (st->temporalQuals != NIL && meta->spec_id == st->specid)
	{
		foreach(lc, st->temporalQuals)
		{
			IceTemporalQual *tq = (IceTemporalQual *) lfirst(lc);
			int64		v;

			if (tq->pos < 0 || tq->pos >= meta->ncells)
				continue;
			if (meta->cells[tq->pos].isnull || meta->cells[tq->pos].is_bytes ||
				!meta->cells[tq->pos].comparable)
				continue;
			v = meta->cells[tq->pos].ival;
			if (ice_fdw_temporal_excludes(tq->strategy, tq->bucket, v))
				return true;
		}
	}

	if (st->partQuals == NIL || meta->spec_id != st->specid ||
		meta->ncells != st->npart)
		return false;

	econtext = st->node->ss.ps.ps_ExprContext;
	ExecClearTuple(st->partSlot);
	for (i = 0; i < tupdesc->natts; i++)
	{
		st->partSlot->tts_values[i] = (Datum) 0;
		st->partSlot->tts_isnull[i] = true;
	}
	for (k = 0; k < st->npart; k++)
	{
		int			a = st->partAttno[k];
		Oid			typid;
		bool		ok;
		Datum		d;

		if (a <= 0 || a > tupdesc->natts)
			continue;
		if (meta->cells[k].isnull)
		{
			st->partSlot->tts_isnull[a - 1] = true;	/* a real NULL: prunable */
			continue;
		}
		typid = TupleDescAttr(tupdesc, a - 1)->atttypid;
		d = ice_fdw_cell_datum(&meta->cells[k], typid, &ok);
		if (!ok)
		{
			/*
			 * A present value we cannot convert (a type mismatch or an
			 * incomparable cell) is UNKNOWN, not null. Leaving it null would let
			 * a qual like "col = X" evaluate NULL -> false and prune a file that
			 * may well match: dropped rows. So do not prune this file at all --
			 * it is read and its rows are re-filtered by the recheckable qual.
			 * (The compile-time type filter already keeps unsupported-type
			 * columns out of the mask; this covers a per-file mismatch.)
			 */
			return false;
		}
		st->partSlot->tts_values[a - 1] = d;
		st->partSlot->tts_isnull[a - 1] = false;
	}
	ExecStoreVirtualTuple(st->partSlot);

	econtext->ecxt_scantuple = st->partSlot;
	foreach(lc, st->partQuals)
	{
		if (!ExecQual((ExprState *) lfirst(lc), econtext))
			return true;		/* this file's partition value excludes a qual */
	}
	return false;
}

/* --------------------------------------------------------------- executor */

static void
icefdwBeginForeignScan(ForeignScanState *node, int eflags)
{
	Relation	rel = node->ss.ss_currentRelation;
	TupleDesc	tupdesc = RelationGetDescr(rel);
	IceFdwState *st;
	bool	   *partMask;
	int			k;
	bool		any;

	if (eflags & EXEC_FLAG_EXPLAIN_ONLY)
		return;

	if (!has_privs_of_role(GetUserId(), ROLE_PG_READ_SERVER_FILES))
		ereport(ERROR,
				(errcode(ERRCODE_INSUFFICIENT_PRIVILEGE),
				 errmsg("must be superuser or a member of the pg_read_server_files role to read an Iceberg table")));

	st = (IceFdwState *) palloc0(sizeof(IceFdwState));
	st->node = node;
	st->tupdesc = tupdesc;

	/*
	 * Expand the plan-time needed-attribute list (fdw_private) into a per-column
	 * mask for the reader. A whole-row reference (attno 0) needs every column, so
	 * the mask is NULL then; otherwise a column is decoded only if referenced, and
	 * a scan that references nothing (count(*)) decodes nothing.
	 */
	{
		ForeignScan *fs = (ForeignScan *) node->ss.ps.plan;
		List	   *needed = (List *) fs->fdw_private;

		if (needed == NIL || list_member_int(needed, 0))
			st->needTop = NULL;		/* whole-row, or nothing recorded: all columns */
		else
		{
			bool	   *nt = (bool *) palloc0(sizeof(bool) * Max(tupdesc->natts, 1));
			int			ci;

			for (ci = 0; ci < tupdesc->natts; ci++)
				if (list_member_int(needed, ci + 1))
					nt[ci] = true;
			st->needTop = nt;
		}
	}
	st->metadata_path = ice_fdw_get_option(RelationGetRelid(rel), "metadata_path");
	if (st->metadata_path == NULL)
		ereport(ERROR,
				(errcode(ERRCODE_FDW_OPTION_NAME_NOT_FOUND),
				 errmsg("foreign table \"%s\" has no \"metadata_path\" option",
						RelationGetRelationName(rel))));

	/* the identity-partition -> column map, and the current spec id */
	PgColumnarIcebergIdentityPartMap(st->metadata_path, NULL, tupdesc,
									 &st->partAttno, &st->npart, &st->specid);

	partMask = (bool *) palloc0(sizeof(bool) * tupdesc->natts);
	any = false;
	for (k = 0; k < st->npart; k++)
	{
		int			a = st->partAttno[k];

		/*
		 * Only mask a column whose type this increment can convert a partition
		 * cell to. A column of an unsupported type (date, timestamp, numeric,
		 * uuid, ...) is left unmasked, so no qual over it is compiled and its
		 * files are read in full -- correct, if not yet optimized. This is the
		 * compile-time half of the "never over-prune" guarantee; the filter
		 * covers a per-file mismatch of a supported-type column at read time.
		 */
		if (a >= 1 && a <= tupdesc->natts &&
			ice_fdw_type_supported(TupleDescAttr(tupdesc, a - 1)->atttypid) &&
			!ice_fdw_typmod_changes_values(tupdesc, a))
		{
			partMask[a - 1] = true;
			any = true;
		}
	}
	if (any)
	{
		st->partQuals = ice_fdw_partition_quals(node, tupdesc, partMask);
		st->partSlot = MakeSingleTupleTableSlot(tupdesc, &TTSOpsVirtual);
	}

	/* metrics pruning: min/max over int/bool columns, keyed by field id */
	{
		int		   *attFieldId = PgColumnarIcebergColumnFieldIds(st->metadata_path,
																 NULL, tupdesc);

		st->metricQuals = ice_fdw_metric_quals(node, tupdesc, attFieldId);
	}

	/* bucket pruning: equality over a bucket[N]-partitioned source column */
	{
		int		   *bpos;
		int		   *battno;
		int		   *bn;
		int			bcount;
		int32		bspec;

		PgColumnarIcebergBucketMap(st->metadata_path, NULL, tupdesc,
								   &bpos, &battno, &bn, &bcount, &bspec);
		st->bucketQuals = ice_fdw_bucket_quals(node, tupdesc, bpos, battno, bn, bcount);
	}

	/* truncate pruning: range over a truncate[W]-partitioned integer column */
	{
		int		   *tpos;
		int		   *tattno;
		int		   *tw;
		int			tcount;
		int32		tspec;

		PgColumnarIcebergTruncateMap(st->metadata_path, NULL, tupdesc,
									 &tpos, &tattno, &tw, &tcount, &tspec);
		st->truncQuals = ice_fdw_trunc_quals(node, tupdesc, tpos, tattno, tw, tcount);
	}

	/* temporal pruning: range over a day()-partitioned date column */
	{
		int		   *dpos;
		int		   *dattno;
		int			dcount;
		int32		dspec;

		PgColumnarIcebergDayMap(st->metadata_path, NULL, tupdesc,
								&dpos, &dattno, &dcount, &dspec);
		st->dayQuals = ice_fdw_day_quals(node, tupdesc, dpos, dattno, dcount);
	}

	/* coarse temporal pruning: year/month/day/hour on a timestamp column */
	{
		static const struct
		{
			const char *name;
			IceTemporalKind kind;
		}			kinds[] = {
			{"year", ICE_TEMPORAL_YEAR},
			{"month", ICE_TEMPORAL_MONTH},
			{"day", ICE_TEMPORAL_DAY},
			{"hour", ICE_TEMPORAL_HOUR},
		};
		int			ki;

		for (ki = 0; ki < (int) lengthof(kinds); ki++)
		{
			int		   *tpos;
			int		   *tattno;
			int			tcount;
			int32		tspec;

			PgColumnarIcebergTemporalMap(st->metadata_path, NULL, tupdesc,
										 kinds[ki].name, &tpos, &tattno,
										 &tcount, &tspec);
			st->temporalQuals = list_concat(st->temporalQuals,
											ice_fdw_temporal_quals(node, tupdesc,
																   kinds[ki].kind,
																   tpos, tattno, tcount));
		}
	}

	st->tupstore = tuplestore_begin_heap(false, false, work_mem);
	st->readslot = MakeSingleTupleTableSlot(tupdesc, &TTSOpsMinimalTuple);
	st->started = false;
	node->fdw_state = st;
}

static TupleTableSlot *
icefdwIterateForeignScan(ForeignScanState *node)
{
	IceFdwState *st = (IceFdwState *) node->fdw_state;
	TupleTableSlot *slot = node->ss.ss_ScanTupleSlot;
	MemoryContext oldcxt;
	bool		got;

	if (st == NULL)
		return ExecClearTuple(slot);

	if (!st->started)
	{
		/* read the surviving files (pruned by the filter) into the tuplestore */
		bool		prune = (st->partQuals != NIL || st->metricQuals != NIL ||
							 st->bucketQuals != NIL || st->truncQuals != NIL ||
							 st->dayQuals != NIL || st->temporalQuals != NIL);

		st->filesPruned = PgColumnarIcebergScanCore(st->metadata_path, st->tupdesc,
													NULL, st->tupstore,
													prune ? ice_fdw_file_excludes : NULL,
													st, st->needTop);
		st->started = true;
	}

	/*
	 * Drain into the minimal-tuple readslot in a context that outlives the row
	 * (ExecScan resets the per-tuple context before every fetch), then copy into
	 * the scan slot, which for a foreign table is a heap-tuple slot.
	 */
	oldcxt = MemoryContextSwitchTo(node->ss.ps.state->es_query_cxt);
	got = tuplestore_gettupleslot(st->tupstore, true, false, st->readslot);
	MemoryContextSwitchTo(oldcxt);
	if (!got)
		return ExecClearTuple(slot);
	return ExecCopySlot(slot, st->readslot);
}

static void
icefdwReScanForeignScan(ForeignScanState *node)
{
	IceFdwState *st = (IceFdwState *) node->fdw_state;

	if (st == NULL)
		return;
	tuplestore_clear(st->tupstore);
	st->started = false;
	st->filesPruned = 0;
}

static void
icefdwEndForeignScan(ForeignScanState *node)
{
	IceFdwState *st = (IceFdwState *) node->fdw_state;

	if (st == NULL)
		return;
	if (st->tupstore != NULL)
		tuplestore_end(st->tupstore);
	if (st->readslot != NULL)
		ExecDropSingleTupleTableSlot(st->readslot);
	if (st->partSlot != NULL)
		ExecDropSingleTupleTableSlot(st->partSlot);
}

static void
icefdwExplainForeignScan(ForeignScanState *node, ExplainState *es)
{
	IceFdwState *st = (IceFdwState *) node->fdw_state;

	if (st == NULL)
		return;
	ExplainPropertyInteger("Files Pruned", NULL, st->filesPruned, es);
}

/* -------------------------------------------------------------------- SQL */

PG_FUNCTION_INFO_V1(pgcolumnar_iceberg_fdw_handler);

Datum
pgcolumnar_iceberg_fdw_handler(PG_FUNCTION_ARGS)
{
	FdwRoutine *r = makeNode(FdwRoutine);

	r->GetForeignRelSize = icefdwGetForeignRelSize;
	r->GetForeignPaths = icefdwGetForeignPaths;
	r->GetForeignPlan = icefdwGetForeignPlan;
	r->BeginForeignScan = icefdwBeginForeignScan;
	r->IterateForeignScan = icefdwIterateForeignScan;
	r->ReScanForeignScan = icefdwReScanForeignScan;
	r->ExplainForeignScan = icefdwExplainForeignScan;
	r->EndForeignScan = icefdwEndForeignScan;
	PG_RETURN_POINTER(r);
}

PG_FUNCTION_INFO_V1(pgcolumnar_iceberg_fdw_validator);

Datum
pgcolumnar_iceberg_fdw_validator(PG_FUNCTION_ARGS)
{
	List	   *options = untransformRelOptions(PG_GETARG_DATUM(0));
	Oid			catalog = PG_GETARG_OID(1);
	ListCell   *lc;

	foreach(lc, options)
	{
		DefElem    *def = (DefElem *) lfirst(lc);

		if (catalog == ForeignTableRelationId)
		{
			if (strcmp(def->defname, "metadata_path") != 0)
				ereport(ERROR,
						(errcode(ERRCODE_FDW_INVALID_OPTION_NAME),
						 errmsg("invalid option \"%s\" for a pgcolumnar_iceberg foreign table",
								def->defname),
						 errhint("Valid table option: metadata_path.")));
		}
		else if (def->defname != NULL)
			ereport(ERROR,
					(errcode(ERRCODE_FDW_INVALID_OPTION_NAME),
					 errmsg("invalid option \"%s\" for pgcolumnar_iceberg",
							def->defname)));
	}
	PG_RETURN_VOID();
}
