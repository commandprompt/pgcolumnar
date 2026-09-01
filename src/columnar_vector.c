/*-------------------------------------------------------------------------
 *
 * pgcolumnar_vector.c
 *		Vectorized execution for pgColumnar (spec 9): a column-at-a-time filter
 *		and vectorized aggregates over decoded chunk-group arrays.
 *
 * Two things live here. First, a shared filter: a plan's simple "column op
 * const" restriction clauses are turned into predicates that are evaluated
 * column-at-a-time over a decoded chunk group to build a selection vector.
 * Second, a vectorized aggregate custom scan: for the common shape
 * SELECT agg(col) FROM t [WHERE ...] with no GROUP BY or HAVING, a custom path
 * on the grouping upper relation computes count, sum, avg, min and max from the
 * zone-map metadata, or by scanning when the group has deletes, skipping the
 * per-tuple executor path.
 *
 * Correctness is the invariant. The vectorized aggregate is only chosen when
 * every aggregate, every column type, and every filter clause is one this module
 * fully supports; anything else falls back to the ordinary scalar Agg plan. The
 * accumulation reproduces PostgreSQL's own aggregate semantics exactly (integer
 * sum overflow behaviour, average as numeric via numeric_div, min/max by the
 * type's default ordering), so a vectorized result equals the scalar result for
 * every query. The toggle columnar.enable_vectorization disables this path so
 * tests can assert that equality.
 *
 * The aggregate custom scan reuses the same registered CustomScanMethods as the
 * base custom scan (so both show as "Custom Scan (PgColumnarScan)"); the shared
 * create-state callback in pgcolumnar_customscan.c dispatches to the aggregate
 * variant when the plan is a scanrelid==0 upper node.
 *
 * Independent MIT implementation built from
 * design/NATIVE_FORMAT_AND_INTERFACE_SPEC.md and the public PostgreSQL API (the
 * custom-scan provider contract, create_upper_paths_hook, and the documented
 * aggregate result types) only.
 *
 *-------------------------------------------------------------------------
 */
#include "columnar.h"

#include "columnar_customscan.h"
#include "columnar_metadata.h"
#include "columnar_reader.h"
#include <math.h>

#include "miscadmin.h"
#include "port/atomics.h"
#include "access/parallel.h"
#include "access/relation.h"
#include "access/table.h"
#include "storage/shm_toc.h"
#include "catalog/pg_aggregate.h"
#include "catalog/pg_namespace.h"
#include "catalog/pg_type.h"
#include "commands/explain.h"
#if PG_VERSION_NUM >= 180000
/* PG18 split the ExplainProperty* helpers out into explain_format.h. */
#include "commands/explain_format.h"
#endif
#include "executor/executor.h"
#include "executor/tuptable.h"
#include "nodes/execnodes.h"
#include "nodes/extensible.h"
#include "nodes/makefuncs.h"
#include "nodes/nodeFuncs.h"
#include "nodes/pathnodes.h"
#include "nodes/plannodes.h"
#include "optimizer/optimizer.h"
#include "optimizer/pathnode.h"
#include "optimizer/paths.h"
#include "optimizer/planner.h"
#include "optimizer/cost.h"
#include "optimizer/restrictinfo.h"
#include "optimizer/tlist.h"
#include "utils/array.h"
#include "access/sysattr.h"
#include "utils/fmgroids.h"
#include "utils/timestamp.h"
#include "utils/selfuncs.h"
#include "utils/builtins.h"
#include "utils/snapmgr.h"
#include "utils/datum.h"
#include "utils/fmgrprotos.h"
#include "utils/lsyscache.h"
#include "access/stratnum.h"
#include "access/tupmacs.h"
#include "common/hashfn.h"
#include "utils/rel.h"
#include "utils/typcache.h"

/* GUC: use the vectorized aggregate path (spec 8.3 scan control) */
bool		pgcolumnar_enable_vectorization = true;

/*
 * GUC: extend the vectorized aggregate to GROUP BY (#289). Default off while the
 * grouped path is built out incrementally; the ungrouped path is unaffected.
 * groupagg_max_groups caps the ACTUAL group count at execution time; exceeding
 * it raises an error (see pgcolumnar_groupagg_lookup) rather than routing to core
 * HashAgg. It is not a plan-time gate.
 */
bool		pgcolumnar_enable_group_vectorization = false;
int			pgcolumnar_groupagg_max_groups = 1000000;

/*
 * GUC: extend the ungrouped vectorized aggregate to the shapes a zone map cannot
 * answer -- an aggregate with a WHERE filter, or sum/avg over int8/float/numeric
 * (#289). Those fall to the row-wise core Agg today; this folds them in one pass
 * over the columnar scan instead. Default off while the path is proven and
 * benchmarked; the metadata-answerable no-filter path is unaffected.
 */
bool		pgcolumnar_enable_ungrouped_vector_agg = false;

/*
 * GUC: make the ungrouped vectorized batch fold parallel-aware (#289 phase 5/6).
 * Each parallel worker claims distinct row groups through the same shared atomic
 * the base parallel scan uses (gap 23) and folds them column-at-a-time, emitting
 * one partial transition-state tuple; a core Finalize Aggregate combines the
 * per-worker partials. Default off while the path is proven and benchmarked. Only
 * the batch-eligible count/sum/avg-float shapes take the parallel arm; everything
 * else keeps the serial node or the ordinary parallel core Agg.
 */
bool		pgcolumnar_enable_parallel_vector_agg = false;

/* -------------------------------------------------------------------------
 * shared column-at-a-time filter
 * ------------------------------------------------------------------------- */

/*
 * One convertible "column op const" restriction clause. Scratch only: it is
 * filled to decide whether a clause converts and discarded, and nothing stores
 * an array of these. The machinery that evaluated them over a decoded chunk
 * group had no call site and is deleted (issue #200).
 */
typedef struct PgColumnarVecPredicate
{
	int			attidx;			/* 0-based column index */
	bool		varOnLeft;		/* column op const, else const op column */
	FmgrInfo	opFn;			/* the operator function (returns bool) */
	Datum		constValue;
	Oid			collation;
} PgColumnarVecPredicate;


/*
 * pgcolumnar_clause_to_predicate
 *		Turn one "column op const" (or "const op column") clause into a predicate
 *		we can evaluate row by row. Requires a strict boolean operator and a
 *		non-null constant, so that a null column value or a failed comparison
 *		excludes the row, matching SQL WHERE semantics. Returns false for any
 *		other clause.
 */
static bool
pgcolumnar_clause_to_predicate(Node *clause, Index scanrelid, TupleDesc tupdesc,
							 PgColumnarVecPredicate *pred)
{
	OpExpr	   *op;
	Node	   *leftop;
	Node	   *rightop;
	Var		   *var;
	Const	   *con;
	bool		varOnLeft;
	Oid			opfuncid;

	if (!IsA(clause, OpExpr))
		return false;
	op = (OpExpr *) clause;
	if (list_length(op->args) != 2)
		return false;
	if (op->opresulttype != BOOLOID)
		return false;

	leftop = (Node *) linitial(op->args);
	rightop = (Node *) lsecond(op->args);
	if (IsA(leftop, RelabelType))
		leftop = (Node *) ((RelabelType *) leftop)->arg;
	if (IsA(rightop, RelabelType))
		rightop = (Node *) ((RelabelType *) rightop)->arg;

	if (IsA(leftop, Var) && IsA(rightop, Const))
	{
		var = (Var *) leftop;
		con = (Const *) rightop;
		varOnLeft = true;
	}
	else if (IsA(rightop, Var) && IsA(leftop, Const))
	{
		var = (Var *) rightop;
		con = (Const *) leftop;
		varOnLeft = false;
	}
	else
		return false;

	if (var->varno != scanrelid)
		return false;
	if (var->varattno < 1 || var->varattno > tupdesc->natts)
		return false;
	if (con->constisnull)
		return false;

	opfuncid = get_opcode(op->opno);
	if (!OidIsValid(opfuncid) || !func_strict(opfuncid))
		return false;

	pred->attidx = var->varattno - 1;
	pred->varOnLeft = varOnLeft;
	fmgr_info(opfuncid, &pred->opFn);
	pred->constValue = con->constvalue;
	pred->collation = op->inputcollid;
	return true;
}

static void
PgColumnarCountConvertibleQuals(List *qual, Index scanrelid, TupleDesc tupdesc,
							  int *nconvertible, bool *allConvertible)
{
	ListCell   *lc;
	int			n = 0;

	*nconvertible = 0;
	*allConvertible = true;
	if (qual == NIL)
		return;

	foreach(lc, qual)
	{
		PgColumnarVecPredicate scratch;

		if (pgcolumnar_clause_to_predicate((Node *) lfirst(lc), scanrelid, tupdesc,
										 &scratch))
			n++;
		else
			*allConvertible = false;
	}

	*nconvertible = n;
}


/*
 * The branch-free typed comparison loops that lived here built a selection
 * vector, and were reached only from a function that had no call site anywhere
 * in the tree; both are deleted (issue #200). Recoverable from history if a
 * filtered aggregate path is ever built. What should not be recovered with them is their gating, which
 * was none: the scalar path's predicates are gated on
 * pgcolumnar.enable_qual_pushdown inside PgColumnarBeginRead and these never were,
 * so wiring them up as they stood would have filtered rows while EXPLAIN
 * reported no pushdown at all.
 */


/* -------------------------------------------------------------------------
 * vectorized aggregate: classification
 * ------------------------------------------------------------------------- */

typedef enum PgColumnarAggKind
{
	COLUMNAR_AGG_COUNT_STAR,
	COLUMNAR_AGG_COUNT_COL,
	COLUMNAR_AGG_SUM_INT,
	COLUMNAR_AGG_AVG_INT,
	COLUMNAR_AGG_MIN,
	COLUMNAR_AGG_MAX,
	/*
	 * Extended kinds used by the grouped path and the ungrouped scan-fold path
	 * (#289). The metadata-fold path (pgcolumnar_fill_native_metadata_agg) still
	 * never produces these: a query using one is routed to scan-fold instead.
	 */
	COLUMNAR_AGG_SUM_INT8,		/* sum(int8) -> numeric */
	COLUMNAR_AGG_SUM_FLOAT,		/* sum(float4/float8) -> float8 */
	COLUMNAR_AGG_SUM_NUMERIC,	/* sum(numeric) -> numeric */
	COLUMNAR_AGG_AVG_INT8,		/* avg(int8) -> numeric */
	COLUMNAR_AGG_AVG_FLOAT,		/* avg(float4/float8) -> float8 */
	COLUMNAR_AGG_AVG_NUMERIC	/* avg(numeric) -> numeric */
} PgColumnarAggKind;

typedef struct PgColumnarAggSpec
{
	PgColumnarAggKind kind;
	int			attidx;			/* 0-based column, or -1 for count(*) */
	Oid			inputType;		/* column type (min/max/sum/avg) */

	/* min/max helpers */
	FmgrInfo	cmpFn;			/* type default btree comparison */
	Oid			collation;
	bool		byval;
	int16		typlen;

	/* accumulators */
	int64		count;			/* count(*), count(col), avg count */
	int64		sum;			/* integer sum / avg sum */
	bool		sawValue;		/* any non-null value contributed */
	Datum		extreme;		/* min/max running value (in resultContext) */
	float8		fsum;			/* float running sum (scan order, like float8_accum) */
	float8		fsxx;			/* avg(float): Youngs-Cramer Sxx, for overflow parity */
	Datum		nsum;			/* numeric running total (in resultContext) */
	bool		nsumSet;		/* nsum initialized */
#ifdef HAVE_INT128
	int128		i128sum;		/* sum/avg over int8: 128-bit running total (#785) */
#endif
} PgColumnarAggSpec;

/*
 * pgcolumnar_classify_aggref
 *		Decide whether an Aggref is one we can compute vectorized, and if so fill
 *		its spec. expectedVarno is the scan relation's range-table index at plan
 *		time (to check the argument Var), or a negative value at execution time
 *		where only the attribute number matters. Returns false to force the
 *		scalar fallback.
 */
static bool
pgcolumnar_classify_aggref(Aggref *agg, int expectedVarno, bool allowExtended,
						 bool allowPartial, PgColumnarAggSpec *spec)
{
	char	   *name;
	Oid			nsp;
	Var		   *argVar = NULL;

	if (agg->aggorder != NIL || agg->aggdistinct != NIL ||
		agg->aggfilter != NULL || agg->aggvariadic ||
		agg->aggkind != AGGKIND_NORMAL)
		return false;

	/*
	 * A plain node finalizes AGGSPLIT_SIMPLE aggrefs. The parallel arm (#289
	 * phase 5/6) rebuilds specs from AGGSPLIT_INITIAL_SERIAL partial aggrefs and
	 * emits transition state instead; the kind is still recovered from the
	 * function name below, so only the split check relaxes.
	 */
	if (agg->aggsplit != AGGSPLIT_SIMPLE &&
		!(allowPartial && agg->aggsplit == AGGSPLIT_INITIAL_SERIAL))
		return false;

	nsp = get_func_namespace(agg->aggfnoid);
	if (nsp != PG_CATALOG_NAMESPACE)
		return false;
	name = get_func_name(agg->aggfnoid);
	if (name == NULL)
		return false;

	/* recover the single column argument, when there is one */
	if (list_length(agg->args) == 1)
	{
		TargetEntry *tle = (TargetEntry *) linitial(agg->args);
		Node	   *arg = (Node *) tle->expr;

		if (IsA(arg, RelabelType))
			arg = (Node *) ((RelabelType *) arg)->arg;
		if (IsA(arg, Var))
			argVar = (Var *) arg;
	}

	memset(spec, 0, sizeof(*spec));
	spec->attidx = -1;

	if (strcmp(name, "count") == 0)
	{
		if (agg->aggstar || list_length(agg->args) == 0)
		{
			spec->kind = COLUMNAR_AGG_COUNT_STAR;
			return true;
		}
		if (argVar == NULL)
			return false;
		if (expectedVarno >= 0 && argVar->varno != (Index) expectedVarno)
			return false;
		spec->kind = COLUMNAR_AGG_COUNT_COL;
		spec->attidx = argVar->varattno - 1;
		return spec->attidx >= 0;
	}

	if (argVar == NULL)
		return false;
	if (expectedVarno >= 0 && argVar->varno != (Index) expectedVarno)
		return false;
	if (argVar->varattno < 1)
		return false;
	spec->attidx = argVar->varattno - 1;
	spec->inputType = argVar->vartype;

	if (strcmp(name, "sum") == 0)
	{
		if (spec->inputType == INT2OID || spec->inputType == INT4OID)
		{
			spec->kind = COLUMNAR_AGG_SUM_INT;
			return true;
		}
		if (allowExtended)
		{
			if (spec->inputType == INT8OID)
				spec->kind = COLUMNAR_AGG_SUM_INT8;
			else if (spec->inputType == FLOAT4OID || spec->inputType == FLOAT8OID)
				spec->kind = COLUMNAR_AGG_SUM_FLOAT;
			else if (spec->inputType == NUMERICOID)
				spec->kind = COLUMNAR_AGG_SUM_NUMERIC;
			else
				return false;
			return true;
		}
		return false;			/* ungrouped: int8/float/numeric fall back */
	}

	if (strcmp(name, "avg") == 0)
	{
		if (spec->inputType == INT2OID || spec->inputType == INT4OID)
		{
			spec->kind = COLUMNAR_AGG_AVG_INT;
			return true;
		}
		if (allowExtended)
		{
			if (spec->inputType == INT8OID)
				spec->kind = COLUMNAR_AGG_AVG_INT8;
			else if (spec->inputType == FLOAT4OID || spec->inputType == FLOAT8OID)
				spec->kind = COLUMNAR_AGG_AVG_FLOAT;
			else if (spec->inputType == NUMERICOID)
				spec->kind = COLUMNAR_AGG_AVG_NUMERIC;
			else
				return false;
			return true;
		}
		return false;
	}

	if (strcmp(name, "min") == 0 || strcmp(name, "max") == 0)
	{
		TypeCacheEntry *tce = lookup_type_cache(spec->inputType,
												TYPECACHE_CMP_PROC_FINFO);

		if (!OidIsValid(tce->cmp_proc_finfo.fn_oid))
			return false;
		spec->kind = (name[1] == 'i') ? COLUMNAR_AGG_MIN : COLUMNAR_AGG_MAX;
		return true;
	}

	return false;
}

/*
 * pgcolumnar_group_key_unsupported_walker
 *		Reject any node in a candidate GROUP BY key the grouped path cannot
 *		evaluate against a bare base-relation slot: aggregates, grouping-set
 *		constructs, window functions, sublinks/subplans, external parameters,
 *		and whole-row or system-column Vars. Everything the executor's ordinary
 *		expression machinery can evaluate from scan columns alone (Const, real
 *		Vars, OpExpr, FuncExpr, CoerceViaIO, CaseExpr, ...) is accepted, subject
 *		to the volatility and varno checks the caller also applies.
 */
static bool
pgcolumnar_group_key_unsupported_walker(Node *node, void *context)
{
	if (node == NULL)
		return false;

	switch (nodeTag(node))
	{
		case T_Var:
			{
				Var		   *var = (Var *) node;

				/* whole-row and system columns are not projectable here */
				if (var->varattno <= 0)
					return true;
				return false;
			}
		case T_Aggref:
		case T_GroupingFunc:
		case T_WindowFunc:
		case T_SubLink:
		case T_SubPlan:
		case T_AlternativeSubPlan:
		case T_Param:
			return true;
		default:
			break;
	}

	return expression_tree_walker(node, pgcolumnar_group_key_unsupported_walker,
								  context);
}

/*
 * pgcolumnar_classify_group_keys
 *		Decide whether every GROUP BY key can be computed and grouped by the
 *		grouped vectorized path, and if so return copies of the key expressions
 *		(original varnos, one per grouping column). A key must be computable from
 *		this relation alone, non-volatile, not set-returning, free of the node
 *		kinds above, and have both a hash function and an equality operator; a
 *		collatable key must use a deterministic collation, because grouping then
 *		matches the byte-exact semantics the scalar path would produce. Returns
 *		false (add no path, run the ordinary Agg) on anything unsupported.
 */
static bool
pgcolumnar_classify_group_keys(PlannerInfo *root, RelOptInfo *input_rel,
							 List **keysOut)
{
	Query	   *parse = root->parse;
	List	   *keys = NIL;
	ListCell   *lc;

	foreach(lc, parse->groupClause)
	{
		SortGroupClause *sgc = lfirst_node(SortGroupClause, lc);
		Node	   *expr = get_sortgroupclause_expr(sgc, parse->targetList);
		Oid			type;
		Oid			coll;
		TypeCacheEntry *tce;

		if (expr == NULL)
			return false;

		/*
		 * Do NOT strip a wrapping RelabelType: it carries the cast's result type
		 * and collation. Stripping it would read the type/collation from the
		 * underlying value instead, which for an explicit COLLATE defeats the
		 * deterministic-collation check below and could group with the wrong
		 * equality. Group by the expression exactly as written.
		 */
		if (!bms_is_subset(pull_varnos(root, expr), input_rel->relids))
			return false;
		if (contain_volatile_functions(expr))
			return false;
		if (expression_returns_set(expr))
			return false;
		if (pgcolumnar_group_key_unsupported_walker(expr, NULL))
			return false;

		type = exprType(expr);
		coll = exprCollation(expr);
		tce = lookup_type_cache(type,
								TYPECACHE_HASH_PROC_FINFO |
								TYPECACHE_EQ_OPR_FINFO);
		if (!OidIsValid(tce->hash_proc_finfo.fn_oid))
			return false;
		if (!OidIsValid(tce->eq_opr_finfo.fn_oid))
			return false;
		if (OidIsValid(coll) && !PgColumnarCollationIsDeterministic(coll))
			return false;

		keys = lappend(keys, copyObject(expr));
	}

	if (keys == NIL)
		return false;
	*keysOut = keys;
	return true;
}

/* -------------------------------------------------------------------------
 * vectorized aggregate: executor state
 * ------------------------------------------------------------------------- */

typedef struct PgColumnarAggScanState
{
	CustomScanState css;

	Oid			relid;			/* base relation to scan */
	List	   *quals;			/* restriction clauses (original varnos) */
	Index		scanrelid;		/* their range-table index */

	PgColumnarAggSpec *specs;
	int			naggs;

	int			npreds;			/* vector predicates, for EXPLAIN (#493) */
	int			nscankeys;		/* scan keys, the shared label's quantity (#493) */

	MemoryContext resultContext;	/* holds min/max running values */

	bool		done;			/* the single result row was emitted */

	/*
	 * Scan-fold mode (#289): the aggregate has a WHERE filter, or a sum/avg a
	 * zone map cannot answer, so the node scans and folds every surviving row
	 * instead of answering from metadata. NULL whereState means no residual
	 * recheck (a pure extended aggregate with no filter).
	 */
	bool		scanFold;
	Bitmapset  *projected;		/* base columns the scan must return */
	TupleTableSlot *baseSlot;	/* holds each read row for the qual recheck */
	ExprState  *whereState;		/* residual WHERE recheck, or NULL */

	/*
	 * Batch fold (#289): when every aggregate and the whole WHERE are
	 * batch-eligible, fold the decoded column buffer column-at-a-time instead of
	 * one Datum tuple per row. batchEligible is decided at Begin (a shape
	 * property, for EXPLAIN); batchFolded records that the fold actually ran.
	 */
	bool		batchEligible;
	bool		batchFolded;

	/*
	 * Fold-path payload deferral (#405 step 2). foldPayloadLoads counts
	 * fetch_att materializations of non-key columns, incremented at the
	 * stable post-key site when deferring and at the eager gather otherwise,
	 * so the counter measures the work either way and the deferral's saving
	 * is their difference. foldGroupsDeferred/foldGroupsTotal expose the
	 * runtime-adaptive gate (defer unless observed survival exceeds half).
	 */
	uint64		foldPayloadLoads;
	int			foldGroupsDeferred;
	int			foldGroupsTotal;

	/*
	 * Parallel batch fold (#289 phase 5/6): a partial node emits its per-worker
	 * transition state instead of the finalized value, and its reader claims
	 * distinct row groups through parallelCounter -- the same shared atomic the
	 * base parallel scan uses (gap 23). isPartial is set from the output tuple's
	 * aggregate split at CreateState; parallelCounter is wired by the DSM/worker
	 * init callbacks and stays NULL on the serial path (each group then read once
	 * via the reader's own rowGroupIndex++).
	 */
	bool		isPartial;
	pg_atomic_uint32 *parallelCounter;

	/* chunk-group skip counters captured for EXPLAIN */
	bool		haveStats;
	uint64		groupsRead;
	uint64		groupsSkipped;
	uint64		vectorsSkipped;
	uint64		vectorsDecoded;
	uint64		vectorDecodes;
	uint64		vectorsRuledOutByValue;
	uint64		zoneMapProbes;
	uint64		groupsTotal;

	/*
	 * How many of npreds the reader could actually exclude a group with (#479).
	 * Captured from the read state beside the counters above, because the read
	 * state is ended before EXPLAIN runs. Meaningful only when haveStats.
	 */
	int			usablePreds;
} PgColumnarAggScanState;

static const CustomExecMethods pgcolumnar_agg_exec_methods;
static const CustomExecMethods pgcolumnar_agg_parallel_exec_methods;

/* -------------------------------------------------------------------------
 * grouped vectorized aggregate (#289): executor state
 *
 * Fires for SELECT <keys>, agg(col) ... [WHERE ...] GROUP BY <keys> over a
 * single columnar relation. The reader (PgColumnarReadNextRow) applies WHERE
 * pushdown for group/vector skipping; each surviving row is rechecked against
 * the full WHERE, its group keys are evaluated, and it is scattered into an
 * open-addressing hash table whose per-group accumulators fold in scan order --
 * byte-identical to the scalar Agg the planner would otherwise run. Grouping
 * uses each key type's own hash and equality functions, so -0.0/NaN, numeric
 * scale, and deterministic-collation text all group exactly as core does.
 * ------------------------------------------------------------------------- */

typedef struct PgColumnarGroupKey
{
	Expr	   *expr;			/* key expression (original varnos) */
	ExprState  *exprState;		/* evaluates it against the base slot */
	Oid			type;
	Oid			collation;
	int16		typlen;
	bool		byval;
	FmgrInfo	hashFn;			/* type hash function */
	FmgrInfo	eqFn;			/* type equality operator function */

	/*
	 * Batch fold (#708). attidx is the base column this key reads when the key
	 * is a plain Var of the scanned relation, and -1 otherwise; the fold takes
	 * the value straight out of the gathered column instead of running
	 * ExecEvalExpr per row, and refuses the shape when any key is -1.
	 *
	 * simple says the type's equality IS bitwise equality of the Datum, so the
	 * fold can hash the bits and compare with ==. See
	 * pgcolumnar_groupagg_simple_key for what that excludes and why.
	 */
	int			attidx;
	bool		simple;
} PgColumnarGroupKey;

typedef struct PgColumnarGroupEntry
{
	uint32		hash;
	bool		used;
	Datum	   *keys;			/* nkeys key values, in keyContext */
	bool	   *keyNulls;		/* nkeys null flags */
	PgColumnarAggSpec *specs;		/* naggs accumulators, in specContext */
} PgColumnarGroupEntry;

typedef struct PgColumnarGroupAggScanState
{
	CustomScanState css;

	Oid			relid;			/* base relation to scan */
	List	   *quals;			/* WHERE clauses (original varnos) */
	Index		scanrelid;		/* their range-table index */

	int			nkeys;
	PgColumnarGroupKey *keys;

	int			naggs;
	PgColumnarAggSpec *aggTemplate;	/* classified once; copied per new group */

	int			nout;			/* output tuple width */
	int		   *outMap;			/* per output pos: >=0 key index, else agg -(v)-1 */

	Bitmapset  *projected;		/* base columns the reader must return */
	TupleTableSlot *baseSlot;	/* holds each read row for key/qual eval */
	ExprState  *whereState;		/* residual WHERE recheck, or NULL */

	PgColumnarGroupEntry *entries;	/* open-addressing table (power-of-two) */
	int			capacity;
	int			nGroups;
	int			maxGroups;		/* GUC cap enforced at execution (pgcolumnar_groupagg_lookup) */

	MemoryContext keyContext;	/* copied key Datums */
	MemoryContext specContext;	/* per-group specs + running min/max/numeric */
	MemoryContext hashContext;	/* the entries array itself */

	bool		started;		/* scan + build completed */
	int			emitPos;		/* next entry index to emit */

	/*
	 * Batch fold (#708). batchEligible is decided at Begin from the query shape
	 * alone, so a plain EXPLAIN can report what will be attempted; batchFolded
	 * records that the fold actually ran. They disagree exactly where the
	 * ungrouped node's pair does (#602): a column added after some row groups is
	 * predicted eligible and then refused at execution.
	 */
	bool		batchEligible;
	bool		batchFolded;

	/*
	 * Parallel grouped fold (#349). A partial node emits each group's per-worker
	 * transition state instead of the finalized value, and its reader claims
	 * distinct row groups through parallelCounter -- the same shared atomic the
	 * base parallel scan and the ungrouped partial node (#343) use (gap 23). Each
	 * worker builds its own hash table over the row groups it claimed; the core
	 * Finalize re-aggregates across workers by grouping key.
	 *
	 * isPartial is read from the output tuple's aggregate split at CreateState;
	 * parallelCounter is wired by the DSM/worker callbacks, and is NULL in a
	 * leader-only run, which then folds every row group itself.
	 */
	bool		isPartial;
	pg_atomic_uint32 *parallelCounter;

	/* EXPLAIN */
	int			npreds;
	int			nscankeys;
	bool		haveStats;
	uint64		groupsRead;
	uint64		groupsSkipped;
	uint64		vectorsSkipped;
	uint64		vectorsDecoded;
	uint64		vectorDecodes;
	uint64		vectorsRuledOutByValue;
	uint64		zoneMapProbes;
	uint64		groupsTotal;
	int			usablePreds;	/* of npreds, how many can exclude (#479) */

	/*
	 * Resizes of the group table (#403 item 6). The work-done line for sizing it
	 * from statistics: sizing it right and sizing it wrong give identical
	 * answers, so no correctness check can see the difference. Counted where the
	 * rehash actually happens, not where a capacity is predicted, because a
	 * number derived from the estimate that drives the change reports a saving
	 * whether or not one occurred.
	 */
	uint64		hashResizes;
	uint64		hashRehashed;	/* live entries MOVED, which is the actual work */
	int			peakCapacity;	/* largest table allocated: the memory cost */
	int			sizeHint;		/* capacity the estimate justifies, 0 for none */
} PgColumnarGroupAggScanState;

static const CustomExecMethods pgcolumnar_groupagg_exec_methods;
static const CustomExecMethods pgcolumnar_groupagg_parallel_exec_methods;
static bool pgcolumnar_groupagg_simple_key(Oid typ);
/*
 * How far a single grow may jump beyond what the table has proven it needs. 64
 * reaches any size in two steps from 1024 while bounding what a wrong estimate
 * can allocate to 64x the live count.
 */
#define COLUMNAR_GROUPAGG_JUMP	64

static int pgcolumnar_groupagg_size_hint(CustomScanState *node, int maxGroups);
static bool pgcolumnar_groupagg_batch_eligible(PgColumnarGroupAggScanState *state,
											 TupleDesc tupdesc, ScanKey *keysOut,
											 int *nkeysOut);
static void PgColumnarTryGroupAggPath(PlannerInfo *root, RelOptInfo *input_rel,
									RelOptInfo *output_rel, void *extra);
static bool pgcolumnar_batch_shape_eligible(PgColumnarAggScanState *state,
										  TupleDesc tupdesc, ScanKey *keysOut,
										  int *nkeysOut);

/* -------------------------------------------------------------------------
 * vectorized aggregate: planning
 * ------------------------------------------------------------------------- */

static Plan *
PgColumnarPlanAggPath(PlannerInfo *root, RelOptInfo *rel, CustomPath *best_path,
					List *tlist, List *clauses, List *custom_plans)
{
	CustomScan *cscan = makeNode(CustomScan);

	cscan->scan.plan.targetlist = tlist;
	cscan->scan.plan.qual = NIL;	/* WHERE is applied inside the scan */
	cscan->scan.scanrelid = 0;		/* not a base-relation scan */
	cscan->flags = best_path->flags;
	cscan->custom_plans = NIL;
	cscan->custom_exprs = NIL;
	cscan->custom_private = best_path->custom_private;
	cscan->custom_scan_tlist = tlist;	/* defines the output tuple shape */
	cscan->methods = &pgcolumnar_scan_methods;	/* shared registered methods */

	return &cscan->scan.plan;
}

static const CustomPathMethods pgcolumnar_agg_path_methods = {
	.CustomName = "PgColumnarAgg",
	.PlanCustomPath = PgColumnarPlanAggPath,
	.ReparameterizeCustomPathByChild = NULL,
};

static create_upper_paths_hook_type prev_create_upper_paths_hook = NULL;

/*
 * pgcolumnar_agg_metadata_answerable
 *		True when this aggregate kind is answerable from whole-chunk zone maps
 *		with no data scan: count, count(col), sum/avg over int2/int4 (the zone
 *		stores an int sum), min and max. The extended kinds (sum/avg over
 *		int8/float/numeric) are not, so a query using one must scan and fold. See
 *		pgcolumnar_fill_native_metadata_agg.
 */
static bool
pgcolumnar_agg_metadata_answerable(PgColumnarAggKind kind)
{
	switch (kind)
	{
		case COLUMNAR_AGG_COUNT_STAR:
		case COLUMNAR_AGG_COUNT_COL:
		case COLUMNAR_AGG_SUM_INT:
		case COLUMNAR_AGG_AVG_INT:
		case COLUMNAR_AGG_MIN:
		case COLUMNAR_AGG_MAX:
			return true;
		case COLUMNAR_AGG_SUM_INT8:
		case COLUMNAR_AGG_SUM_FLOAT:
		case COLUMNAR_AGG_SUM_NUMERIC:
		case COLUMNAR_AGG_AVG_INT8:
		case COLUMNAR_AGG_AVG_FLOAT:
		case COLUMNAR_AGG_AVG_NUMERIC:
			return false;
	}
	return false;
}

/*
 * pgcolumnar_parallel_agg_ok
 *		Kinds whose transition state is a plain, non-internal value the batch fold
 *		already holds and a core Finalize can combine (#289 phase 5/6): count(*),
 *		count(col), and sum/avg over int2/int4/float4/float8. The transition types:
 *		count/sum(int) -> int8, sum(float) -> the identity float, avg(int) -> the
 *		int8[2] {N,sum} array, avg(float) -> the _float8 {N,Sx,Sxx} array.
 *
 *		The internal-transtype kinds (sum/avg over int8/numeric) stay OUT OF THIS
 *		PREDICATE, and still should: an int128 running total is not a partial
 *		state a core Finalize can combine, whatever else changes.
 *
 *		This header used to add "and are not batch-eligible", which was a claim
 *		about a DIFFERENT predicate asserted inside this one. Batch eligibility
 *		is pgcolumnar_batch_agg_ok, which has its own callers, and since #755 q3
 *		it admits the int8 kinds while this one does not. Two predicates, two
 *		answers; one sentence used to give both (OffgridwithJD, #755 review).
 */
static bool
pgcolumnar_parallel_agg_ok(PgColumnarAggKind kind)
{
	switch (kind)
	{
		case COLUMNAR_AGG_COUNT_STAR:
		case COLUMNAR_AGG_COUNT_COL:
		case COLUMNAR_AGG_SUM_INT:
		case COLUMNAR_AGG_SUM_FLOAT:
		case COLUMNAR_AGG_AVG_INT:
		case COLUMNAR_AGG_AVG_FLOAT:
			return true;
		default:
			return false;
	}
}

/*
 * PgColumnarCreateUpperPaths
 *		create_upper_paths_hook: for a plain SELECT agg(col) FROM pgcolumnar_table
 *		[WHERE simple quals] with no grouping or HAVING, add a custom path that
 *		computes the aggregates vectorized. Every aggregate, column type and
 *		filter clause must be fully supported, or we add nothing and the ordinary
 *		Agg plan runs, so results are never at risk.
 */
static void
PgColumnarCreateUpperPaths(PlannerInfo *root, UpperRelationKind stage,
						 RelOptInfo *input_rel, RelOptInfo *output_rel,
						 void *extra)
{
	Query	   *parse = root->parse;
	RangeTblEntry *rte;
	Oid			relid;
	List	   *tlist = output_rel->reltarget->exprs;
	List	   *aggList;			/* the aggregates the tlist CONTAINS (#755) */
	bool		tlistIsBare;		/* ... and whether the tlist already IS them */
	ListCell   *lc;
	int			naggs;
	int			i;
	PgColumnarAggSpec *specs;
	List	   *quals;
	int			npreds;
	bool		allConvertible;
	bool		needsScan;
	Path	   *cheapest;
	CustomPath *cpath;
	bool		parallelAdded = false;

	if (prev_create_upper_paths_hook)
		prev_create_upper_paths_hook(root, stage, input_rel, output_rel, extra);

	if (stage != UPPERREL_GROUP_AGG)
		return;
	if (!pgcolumnar_enable_vectorization || !pgcolumnar_enable_custom_scan)
		return;

	if (!parse->hasAggs)
		return;

	/*
	 * GROUP BY: try the grouped vectorized path (#289). It handles a plain
	 * grouped aggregate over one columnar relation, with an optional WHERE, and
	 * no grouping sets / HAVING / DISTINCT / window / SRF. Anything else, or an
	 * unsupported key or aggregate, adds no path and the ordinary Agg runs.
	 */
	if (parse->groupClause != NIL || parse->groupingSets != NIL ||
		parse->havingQual != NULL || parse->distinctClause != NIL ||
		parse->hasWindowFuncs || parse->hasTargetSRFs)
	{
		if (pgcolumnar_enable_group_vectorization &&
			parse->groupClause != NIL &&
			parse->groupingSets == NIL &&
			parse->havingQual == NULL &&
			parse->distinctClause == NIL &&
			!parse->hasWindowFuncs &&
			!parse->hasTargetSRFs)
			PgColumnarTryGroupAggPath(root, input_rel, output_rel, extra);
		return;
	}

	/* plain, ungrouped aggregation only (spec 9) */

	/* a single columnar base relation with no joins */
	if (input_rel->reloptkind != RELOPT_BASEREL)
		return;
	if (bms_membership(input_rel->relids) != BMS_SINGLETON)
		return;
	if (input_rel->relid == 0 ||
		input_rel->relid >= (Index) root->simple_rel_array_size)
		return;
	rte = root->simple_rte_array[input_rel->relid];
	if (rte == NULL || rte->rtekind != RTE_RELATION ||
		rte->relkind != RELKIND_RELATION)
		return;
	if (rte->tablesample != NULL)
		return;
	if (!OidIsValid(rte->relid) || !PgColumnarIsColumnarRelation(rte->relid))
		return;
	relid = rte->relid;

	/*
	 * The aggregates the target list CONTAINS, which need not be the target list
	 * (#755). This used to require every entry to BE an Aggref, so an entry that
	 * merely contained one fell off the vectorized path entirely -- and on an
	 * unfiltered columnar table that is the difference between answering from
	 * the zone maps and scanning the whole relation. Measured on 8,000,000 rows:
	 *
	 *   count(*)                    0.030 ms      count(*)::text        634.1 ms
	 *   avg(a)                      0.508 ms      avg(a)+avg(b)         329.5 ms
	 *   min(a), max(a)              0.480 ms      max(a)-min(a)         245.4 ms
	 *                                             round(avg(a)::numeric,2)  230.9 ms
	 *
	 * Two to four orders of magnitude, and every losing shape is ordinary SQL:
	 * a cast on a count, a difference of two aggregates, a rounded average.
	 *
	 * The node itself is unchanged and still emits one bare aggregate per output
	 * column -- PgColumnarCreateAggScanState rebuilds its specs from
	 * custom_scan_tlist assuming exactly that. What changes is that when the
	 * target list is not already bare, this path produces the AGGREGATES and a
	 * projection above it computes the expressions, which is how core's own Agg
	 * relates to an upper target.
	 */
	{
		List	   *found = pull_var_clause((Node *) tlist,
											PVC_INCLUDE_AGGREGATES |
											PVC_INCLUDE_WINDOWFUNCS |
											PVC_INCLUDE_PLACEHOLDERS);
		ListCell   *fc;

		/*
		 * INCLUDE rather than RECURSE on all three, so anything that is not a
		 * plain aggregate arrives here and is refused below rather than making
		 * pull_var_clause elog. A bare Var cannot appear in a valid ungrouped
		 * aggregate query, and a window function or placeholder is not a shape
		 * this node computes.
		 */
		aggList = NIL;
		foreach(fc, found)
		{
			Node	   *n = (Node *) lfirst(fc);

			if (!IsA(n, Aggref))
				return;
			/* equal(), so avg(a)+avg(a) folds to one aggregate */
			aggList = list_append_unique(aggList, n);
		}
		list_free(found);
	}
	naggs = list_length(aggList);
	if (naggs == 0)
		return;

	/*
	 * Whether the target list is ALREADY exactly the aggregates. When it is, the
	 * path keeps output_rel->reltarget and no projection is added, so every plan
	 * that worked before this change is planned identically to before.
	 */
	tlistIsBare = (list_length(tlist) == naggs);
	if (tlistIsBare)
	{
		foreach(lc, tlist)
			if (!IsA((Node *) lfirst(lc), Aggref))
			{
				tlistIsBare = false;
				break;
			}
	}

	specs = (PgColumnarAggSpec *) palloc0(sizeof(PgColumnarAggSpec) * naggs);
	i = 0;
	foreach(lc, aggList)
	{
		if (!pgcolumnar_classify_aggref((Aggref *) lfirst(lc), (int) input_rel->relid,
									  true, false, &specs[i]))
			return;
		i++;
	}

	/*
	 * The whole WHERE, rechecked per row in scan-fold mode; NIL with no filter.
	 * A native table answers an ungrouped aggregate from its zone maps with no
	 * data scan (native spec 7.1), but only with no filter and every aggregate
	 * zone-map answerable. A filter, or a sum/avg over int8/float/numeric, needs
	 * the scan-fold path instead (#289).
	 */
	quals = extract_actual_clauses(input_rel->baserestrictinfo, false);

	needsScan = (quals != NIL);
	for (i = 0; i < naggs; i++)
		if (!pgcolumnar_agg_metadata_answerable(specs[i].kind))
			needsScan = true;

	if (needsScan)
	{
		/*
		 * Scan-fold path (#289): the ungrouped sibling of the grouped path. It
		 * scans every surviving row once and folds it, so it handles a filter and
		 * the extended sum/avg kinds a zone map cannot. Opt-in while it is proven.
		 */
		ListCell   *rc;
		Bitmapset  *whereAtts = NULL;
		int			m = -1;

		if (!pgcolumnar_enable_ungrouped_vector_agg)
			return;

		/*
		 * sum/avg over numeric are refused HERE rather than in
		 * pgcolumnar_classify_aggref (#785). This path is slower than the
		 * ordinary Agg for them and there is no equivalent of #786's int128
		 * accumulator to fix it: the input is already numeric and carries a
		 * scale.
		 *
		 * Measured on 8,000,000 rows, interleaved, min of 7, plan and answers
		 * asserted:
		 *
		 *   sum(numeric)               1.10x SLOWER on this path
		 *   sum(float8)                2.73x faster
		 *   sum(numeric), sum(float8)  1.00x -- no gain at all
		 *
		 * That last row is why this is unconditional. Classification is all or
		 * nothing (the loop above returns on the first refusal), so refusing
		 * numeric refuses the whole node, and the obvious worry is that a mixed
		 * query loses the float win with it.
		 *
		 * It does not, and the truth is stronger than "nothing is given up".
		 * A mixed query was being actively HARMED. One cluster, one fixture,
		 * only the .so changing:
		 *
		 *   sum(numeric)               391.0 -> 351.0 ms   1.11x faster
		 *   sum(numeric), sum(float8)  412.4 -> 367.6 ms   1.12x faster
		 *   sum(float8) (control)       78.8 ->  77.2 ms   flat
		 *
		 * So the mixed case is an argument FOR the refusal rather than a cost of
		 * it. Stated this way deliberately: "gains nothing" invites someone to
		 * restore a conditional later on the grounds that a mixed query might one
		 * day benefit, when the measurement says it was paying for the numeric
		 * aggregate twice over (OffgridwithJD, #796 review).
		 *
		 * Refused here and not at classification because classify_aggref is
		 * shared with the GROUPED path, which is a different implementation and
		 * has not been measured for these kinds. This narrows only what was
		 * measured.
		 *
		 * The reviewer established the other half: numeric is slower whether or
		 * not chunk-group pruning happens (1.14x with 25 of 27 groups removed,
		 * 1.56x with none), so this does not need to be conditional on the shape
		 * of the data either.
		 */
		for (i = 0; i < naggs; i++)
			if (specs[i].kind == COLUMNAR_AGG_SUM_NUMERIC ||
				specs[i].kind == COLUMNAR_AGG_AVG_NUMERIC)
				return;

		/*
		 * A pseudoconstant (gating) qual is a one-time filter, not a per-row
		 * predicate; the recheck this path relies on runs per row and would never
		 * apply it, so a false gate would wrongly return a row. Rare; fall back.
		 * (Mirrors the grouped path.)
		 */
		foreach(rc, input_rel->baserestrictinfo)
			if (lfirst_node(RestrictInfo, rc)->pseudoconstant)
				return;

		/*
		 * A WHERE on a system column or a whole-row Var cannot be evaluated from
		 * the projected data columns the recheck reads; fall back rather than
		 * evaluate it against unset slot values.
		 */
		pull_varattnos((Node *) quals, input_rel->relid, &whereAtts);
		while ((m = bms_next_member(whereAtts, m)) >= 0)
			if (m + FirstLowInvalidHeapAttributeNumber <= 0)
				return;

		npreds = 0;				/* the EXPLAIN count is filled at execution */
	}
	else
	{
		Relation	rel = table_open(relid, AccessShareLock);
		TupleDesc	tupdesc = RelationGetDescr(rel);

		PgColumnarCountConvertibleQuals(quals, input_rel->relid, tupdesc,
									  &npreds, &allConvertible);
		table_close(rel, AccessShareLock);
		if (!allConvertible)
			return;
	}

	cheapest = input_rel->cheapest_total_path;
	if (cheapest == NULL)
		return;

	cpath = makeNode(CustomPath);
	cpath->path.pathtype = T_CustomScan;
	cpath->path.parent = output_rel;
	/*
	 * The aggregates, not the expressions over them, unless the target list is
	 * already exactly the aggregates (#755). PlanCustomPath copies whatever core
	 * derives from this into custom_scan_tlist, and the executor rebuilds its
	 * per-aggregate specs from that list assuming each entry is an Aggref.
	 */
	if (tlistIsBare)
		cpath->path.pathtarget = output_rel->reltarget;
	else
	{
		PathTarget *aggTarget = create_empty_pathtarget();
		ListCell   *ac;

		foreach(ac, aggList)
			add_column_to_pathtarget(aggTarget, (Expr *) lfirst(ac), 0);
		set_pathtarget_cost_width(root, aggTarget);
		cpath->path.pathtarget = aggTarget;
	}
	cpath->path.param_info = NULL;
	cpath->path.parallel_aware = false;
	cpath->path.parallel_safe = false;
	cpath->path.parallel_workers = 0;
	cpath->path.rows = 1;

	/*
	 * Cost what this path actually reads.
	 *
	 * Pricing it at the cheapest scan's cost, as this did, is not merely
	 * pessimistic: it loses. A parallel Agg divides that same scan cost across
	 * workers and comes out cheaper, so the planner reads the whole table to
	 * compute what this path takes from row-group metadata (issue #133).
	 *
	 * The executor answers a clean row group from its metadata and reads no data
	 * pages for it; it reads only the groups that have deletes (issue #149). So
	 * the price is one metadata entry per row group, plus a scan of the fraction
	 * of the table that is deleted-in.
	 *
	 * Pricing the whole path at the scan cost the moment any row anywhere is
	 * deleted, as this did, gives the planner back the choice this path exists to
	 * take away: one deleted row out of six million made a parallel Agg look
	 * cheaper again and #133 came back. The probe below asks the same question
	 * execution asks; if the answer changes between planning and execution the
	 * plan is mispriced, never wrong.
	 */
	if (needsScan)
	{
		/*
		 * A full columnar scan, priced from the cheapest non-index path (an index
		 * scan cannot serve this node), plus a token per-row bump so this opt-in
		 * accelerator is chosen over the ordinary Agg-over-scan when it is enabled.
		 */
		Path	   *scanp = NULL;
		ListCell   *pc;
		Cost		cost;

		foreach(pc, input_rel->pathlist)
		{
			Path	   *p = (Path *) lfirst(pc);

			if (p->pathtype == T_IndexScan || p->pathtype == T_IndexOnlyScan ||
				p->pathtype == T_BitmapHeapScan)
				continue;
			if (scanp == NULL || p->total_cost < scanp->total_cost)
				scanp = p;
		}
		if (scanp == NULL)
			scanp = cheapest;
		cost = scanp->total_cost + cpu_tuple_cost;
		cpath->path.startup_cost = cost;
		cpath->path.total_cost = cost;
	}
	else
	{
		PgColumnarOptions opts;
		int			limit = pgcolumnar_stripe_row_limit;
		double		rows = input_rel->tuples;
		double		ngroups;
		double		dirtyFraction = 0.0;
		Snapshot	snap = GetActiveSnapshot();
		Cost		cost;

		/*
		 * Row groups are governed by stripe_row_limit, not chunk_group_row_limit:
		 * the latter sets the vector size within a group. Dividing by the vector
		 * size overstated the group count by the ratio between them -- at the
		 * default limits, 60 computed groups against 4 actual. The estimate was
		 * clamped below the scan cost so it still chose right, but it was a wrong
		 * model getting a right answer, and it stops being harmless as soon as the
		 * clamp is not what decides.
		 */
		if (PgColumnarReadOptions(relid, &opts) &&
			opts.stripeRowLimitSet && opts.stripeRowLimit > 0)
			limit = opts.stripeRowLimit;
		if (limit <= 0)
			limit = 1;

		/*
		 * A never-analyzed relation has no row estimate. Deriving one from the
		 * page count keeps the cost tied to something real, so a missing estimate
		 * cannot make this path look free.
		 */
		if (rows < 0)
			rows = (input_rel->pages > 0)
				? (double) input_rel->pages * 100.0
				: 1000.0;
		ngroups = ceil(rows / (double) limit);
		if (ngroups < 1)
			ngroups = 1;

		/*
		 * What fraction of the groups must actually be read. Counting them exactly
		 * would mean a delete-vector lookup per group at planning time, which is
		 * the per-group catalog traffic this path exists to avoid paying. The
		 * storage-wide probe is one lookup and distinguishes the case that matters
		 * -- nothing deleted at all -- from the case where something is. When
		 * something is, assume one group in four is affected rather than all of
		 * them: wrong in both directions by a bounded factor, where the previous
		 * all-or-nothing was wrong by the whole table.
		 */
		if (snap != NULL)
		{
			Relation	frel = table_open(relid, AccessShareLock);

			if (PgColumnarStorageHasDeleteVector(PgColumnarStorageId(frel),
											   PgColumnarCatalogSnapshot(snap)))
				dirtyFraction = 0.25;
			table_close(frel, AccessShareLock);
		}
		else
			dirtyFraction = 0.25;

		cost = cpu_tuple_cost * 10;		/* getting to the catalog */
		cost += ngroups * (cpu_tuple_cost +
						   cpu_operator_cost * (double) naggs);
		cost += dirtyFraction * cheapest->total_cost;

		/* reading metadata is never dearer than reading the table */
		if (cost > cheapest->total_cost)
			cost = cheapest->total_cost;

		/*
		 * One row comes out, and only after every row group is folded in, so
		 * there is no partial result to start up cheaply with.
		 */
		cpath->path.startup_cost = cost;
		cpath->path.total_cost = cost;
	}
	cpath->path.pathkeys = NIL;
	cpath->flags = 0;
	cpath->custom_paths = NIL;
#if PG_VERSION_NUM >= 170000
	cpath->custom_restrictinfo = NIL;
#endif
	cpath->custom_private =
		list_make3(makeInteger((int) input_rel->relid),
				   copyObject(quals),
				   makeConst(OIDOID, -1, InvalidOid, sizeof(Oid),
							 ObjectIdGetDatum(relid), false, true));
	cpath->methods = &pgcolumnar_agg_path_methods;

	/*
	 * Parallel arm (#289 phase 5/6). When the shape is one whose transition state
	 * the fold already holds and can be combined by a core Finalize
	 * (pgcolumnar_parallel_agg_ok), and the base relation has a partial (parallel)
	 * path, add a parallel-aware partial version of this node under Gather ->
	 * Finalize Aggregate. Each worker claims distinct row groups through the shared
	 * gap-23 counter and emits one per-worker transition-state tuple; the core
	 * Finalize combines them exactly as it does for an ordinary parallel aggregate
	 * (int8pl for count, float8_combine + float8_avg for avg(float8)).
	 *
	 * When this arm is added it supersedes the serial node at every worker count
	 * (its Gather runs leader-only when no workers start), so the serial node is
	 * added below only when the parallel arm was not -- keeping it, priced at the
	 * cheap Gather cost by #133, would wrongly out-cost the genuinely parallel
	 * plan. Opt-in while it is proven and benchmarked.
	 */
	if (needsScan && pgcolumnar_enable_parallel_vector_agg)
	{
		GroupPathExtraData *gpe = (GroupPathExtraData *) extra;
		bool		parallelOk = (gpe != NULL &&
								  (gpe->flags & GROUPING_CAN_PARTIAL_AGG) != 0 &&
								  gpe->partial_costs_set &&
								  output_rel->consider_parallel &&
								  input_rel->partial_pathlist != NIL);

		for (i = 0; parallelOk && i < naggs; i++)
			if (!pgcolumnar_parallel_agg_ok(specs[i].kind))
				parallelOk = false;

		if (parallelOk)
		{
			RelOptInfo *pgr = fetch_upper_rel(root, UPPERREL_PARTIAL_GROUP_AGG,
											  output_rel->relids);
			Path	   *partialScan = NULL;
			ListCell   *pc;

			/* the cheapest per-worker base scan drives this partial node's cost */
			foreach(pc, input_rel->partial_pathlist)
			{
				Path	   *p = (Path *) lfirst(pc);

				if (partialScan == NULL || p->total_cost < partialScan->total_cost)
					partialScan = p;
			}

			/*
			 * pgr->reltarget is core's partial grouping target: the same Aggrefs,
			 * marked AGGSPLIT_INITIAL_SERIAL, that the Finalize below combines. Reuse
			 * it so the partial and final aggregates are structurally related and
			 * setrefs matches them (build our own and they would not). Guard against
			 * a partial rel core did not fully populate.
			 */
			if (partialScan != NULL && partialScan->parallel_workers >= 1 &&
				pgr->reltarget != NULL)
			{
				CustomPath *ppath = makeNode(CustomPath);
				GatherPath *gather;
				double		grows = 1;
				Cost		pcost = partialScan->total_cost + cpu_tuple_cost;

				ppath->path.pathtype = T_CustomScan;
				ppath->path.parent = pgr;
				ppath->path.pathtarget = pgr->reltarget;
				ppath->path.param_info = NULL;
				ppath->path.parallel_aware = true;
				ppath->path.parallel_safe = true;
				ppath->path.parallel_workers = partialScan->parallel_workers;
				ppath->path.rows = 1;	/* one partial tuple per worker */
				ppath->path.startup_cost = pcost;
				ppath->path.total_cost = pcost;
				ppath->path.pathkeys = NIL;
				ppath->flags = 0;
				ppath->custom_paths = NIL;
#if PG_VERSION_NUM >= 170000
				ppath->custom_restrictinfo = NIL;
#endif
				ppath->custom_private =
					list_make3(makeInteger((int) input_rel->relid),
							   copyObject(quals),
							   makeConst(OIDOID, -1, InvalidOid, sizeof(Oid),
										 ObjectIdGetDatum(relid), false, true));
				ppath->methods = &pgcolumnar_agg_path_methods;

				/*
				 * Gather this partial node directly rather than add_partial_path'ing
				 * it: add_partial_path may pfree a dominated path, and we hold the
				 * only pointer create_gather_path needs.
				 */
				gather = create_gather_path(root, pgr, &ppath->path,
											pgr->reltarget, NULL, &grows);

				add_path(output_rel,
						 (Path *) create_agg_path(root, output_rel, &gather->path,
												  output_rel->reltarget,
												  AGG_PLAIN, AGGSPLIT_FINAL_DESERIAL,
												  NIL, NIL, &gpe->agg_final_costs,
												  1));
				parallelAdded = true;
			}
		}
	}

	/* the serial node is the fallback; skip it when the parallel arm was added */
	if (!parallelAdded)
	{
		if (tlistIsBare)
			add_path(output_rel, &cpath->path);
		else
			/*
			 * The node emits the aggregates; this evaluates the expressions over
			 * them (#755). setrefs matches the projection's Aggrefs to the
			 * subplan's by equal(), and they are the same nodes, pulled from this
			 * very target list.
			 *
			 * The PARALLEL arm above needs nothing equivalent and never did: it
			 * uses core's partial grouping target, which is bare Aggrefs however
			 * the final target is shaped, and its Finalize Agg is created with
			 * output_rel->reltarget, so core evaluates the expressions there. Only
			 * this serial gate was refusing the shape, and refusing it early
			 * killed the parallel arm with it.
			 */
			add_path(output_rel,
					 (Path *) create_projection_path(root, output_rel,
													 &cpath->path,
													 output_rel->reltarget));
	}
}

/*
 * pgcolumnar_groupagg_outmap
 *		Map one target list onto this node's output: per position, the group-key
 *		index (>= 0) or the aggregate index encoded as -(index + 1). False when an
 *		entry is neither a supported aggregate nor a bare reference to a group key,
 *		which forces the fallback.
 *
 *		Built per target rather than once, because the serial path outputs
 *		output_rel->reltarget (finished values) while the parallel partial path
 *		outputs core's UPPERREL_PARTIAL_GROUP_AGG target (grouping columns plus
 *		AGGSPLIT_INITIAL_SERIAL aggrefs). The two differ in both the aggregate
 *		split and, potentially, the column order, so each needs its own map.
 *		allowPartial admits the INITIAL_SERIAL aggrefs of the second.
 */
static bool
pgcolumnar_groupagg_outmap(List *exprs, List *groupKeys, Index scanrelid,
						 bool allowPartial, List **outMapOut, int *naggsOut)
{
	List	   *outMap = NIL;
	ListCell   *lc;
	int			aggIdx = 0;

	foreach(lc, exprs)
	{
		Node	   *oexpr = (Node *) lfirst(lc);

		if (IsA(oexpr, Aggref))
		{
			PgColumnarAggSpec spec;

			if (!pgcolumnar_classify_aggref((Aggref *) oexpr, (int) scanrelid,
										  true, allowPartial, &spec))
				return false;
			outMap = lappend(outMap, makeInteger(-(aggIdx + 1)));
			aggIdx++;
		}
		else
		{
			ListCell   *kc;
			int			k = 0;
			int			found = -1;

			/*
			 * Match the output expression against a group key exactly as written
			 * (no RelabelType stripping): the classifier stores keys un-stripped
			 * too, and both come from the same target list, so equal() lines them
			 * up. An output built on top of a key (not a bare reference) matches
			 * nothing and forces the fallback.
			 */
			foreach(kc, groupKeys)
			{
				if (equal(oexpr, (Node *) lfirst(kc)))
				{
					found = k;
					break;
				}
				k++;
			}
			if (found < 0)
				return false;
			outMap = lappend(outMap, makeInteger(found));
		}
	}

	if (aggIdx == 0)
		return false;			/* no aggregate: leave it to the ordinary plan */

	*outMapOut = outMap;
	*naggsOut = aggIdx;
	return true;
}

/*
 * PgColumnarTryGroupAggPath
 *		Add a grouped vectorized aggregate path (#289) when the query is one we
 *		can answer exactly: a single columnar base relation, an optional WHERE,
 *		every output entry either a supported aggregate or a bare reference to a
 *		supported GROUP BY key. On anything unsupported it adds nothing and the
 *		ordinary Agg plan runs. The group-count cap is enforced only at
 *		execution (pgcolumnar_groupagg_lookup).
 *
 *		When the shape also qualifies for the parallel arm (#349) this adds
 *		Finalize HashAggregate -> Gather -> parallel-aware partial node instead of
 *		the serial node, so the vectorized fold runs across workers rather than
 *		displacing a parallel plan with a single-threaded one.
 */
/*
 * pgcolumnar_truncating_time_bound
 *		Upper bound on the distinct values of date_trunc(unit, ts) over the rows
 *		this scan will read, or 0 when we cannot bound it (#369).
 *
 *		Truncation is a step function, so the number of distinct outputs cannot
 *		exceed the number of buckets the input range spans. Two partial buckets
 *		are possible, one at each end, hence the +2.
 *
 *		The range comes only from Const btree comparisons on that same Var in the
 *		relation's baserestrictinfo, because those describe the rows actually
 *		scanned. Anything less certain gives no bound rather than a guess: this
 *		value is used to make a plan cheaper, so an under-estimate would
 *		under-price the arm and under-size the Finalize's hash table.
 */
static double
pgcolumnar_truncating_time_bound(PlannerInfo *root, RelOptInfo *input_rel,
							   Node *expr)
{
	FuncExpr   *f = (FuncExpr *) expr;
	Const	   *unit;
	Var		   *tsvar;
	char	   *unitstr;
	double		width;
	bool		havelo = false,
				havehi = false;
	double		lo = 0,
				hi = 0;
	ListCell   *lc;

	if (!IsA(expr, FuncExpr))
		return 0.0;
	f = (FuncExpr *) expr;

	/*
	 * Match the FUNCTION, not its shape.
	 *
	 * Matching on shape alone -- a FuncExpr of two or three args with a text
	 * Const and a timestamp Var -- accepts any user function declared
	 * (text, timestamp), and then applies a truncation bound to something that
	 * does not truncate. Measured: a deliberately high-cardinality
	 * hi_card(text, timestamp) over 500k rows was estimated at 722 against
	 * 499,999 actual, a 692x UNDER-estimate, where core had it very nearly right
	 * at 499,900 before the substitution replaced it.
	 *
	 * Under-estimating is the direction that does damage. It under-prices the arm
	 * and under-sizes the Finalize's hash table into avoidable spill, which is
	 * the opposite of what this change exists to do.
	 *
	 * These oids are identical on 15 through 19. The interval form is excluded
	 * deliberately: its argument is a duration rather than a point in time, so a
	 * range on it does not describe a scanned window.
	 */
	if (f->funcid != F_DATE_TRUNC_TEXT_TIMESTAMP &&
		f->funcid != F_DATE_TRUNC_TEXT_TIMESTAMPTZ &&
		f->funcid != F_DATE_TRUNC_TEXT_TIMESTAMPTZ_TEXT)
		return 0.0;

	if (list_length(f->args) < 2 || list_length(f->args) > 3)
		return 0.0;
	if (!IsA(linitial(f->args), Const))
		return 0.0;
	unit = (Const *) linitial(f->args);
	if (unit->consttype != TEXTOID || unit->constisnull)
		return 0.0;
	if (!IsA(lsecond(f->args), Var))
		return 0.0;
	tsvar = (Var *) lsecond(f->args);
	if (tsvar->varno != input_rel->relid || tsvar->varlevelsup != 0)
		return 0.0;
	if (tsvar->vartype != TIMESTAMPOID && tsvar->vartype != TIMESTAMPTZOID)
		return 0.0;

	/* bucket width in microseconds, which is the timestamp unit */
	unitstr = TextDatumGetCString(unit->constvalue);
	if (pg_strcasecmp(unitstr, "microseconds") == 0)		width = 1;
	else if (pg_strcasecmp(unitstr, "milliseconds") == 0)	width = 1000;
	else if (pg_strcasecmp(unitstr, "second") == 0)			width = USECS_PER_SEC;
	else if (pg_strcasecmp(unitstr, "minute") == 0)			width = USECS_PER_MINUTE;
	else if (pg_strcasecmp(unitstr, "hour") == 0)			width = USECS_PER_HOUR;
	else if (pg_strcasecmp(unitstr, "day") == 0)			width = (double) USECS_PER_DAY;
	else if (pg_strcasecmp(unitstr, "week") == 0)			width = 7.0 * USECS_PER_DAY;
	else if (pg_strcasecmp(unitstr, "month") == 0)			width = 28.0 * USECS_PER_DAY;
	else if (pg_strcasecmp(unitstr, "quarter") == 0)		width = 89.0 * USECS_PER_DAY;
	else if (pg_strcasecmp(unitstr, "year") == 0)			width = 365.0 * USECS_PER_DAY;
	else if (pg_strcasecmp(unitstr, "decade") == 0)			width = 3650.0 * USECS_PER_DAY;
	else if (pg_strcasecmp(unitstr, "century") == 0)		width = 36500.0 * USECS_PER_DAY;
	else if (pg_strcasecmp(unitstr, "millennium") == 0)		width = 365000.0 * USECS_PER_DAY;
	else return 0.0;
	/*
	 * month, quarter and year use their SHORTEST possible length above, so the
	 * bucket count is over-estimated rather than under-estimated. This must stay
	 * an upper bound.
	 */

	foreach(lc, input_rel->baserestrictinfo)
	{
		RestrictInfo *ri = lfirst_node(RestrictInfo, lc);
		OpExpr	   *op;
		Node	   *l,
				   *r;
		Const	   *c;
		double		v;
		bool		varonleft;

		if (!IsA(ri->clause, OpExpr))
			continue;
		op = (OpExpr *) ri->clause;
		if (list_length(op->args) != 2)
			continue;
		l = (Node *) linitial(op->args);
		r = (Node *) lsecond(op->args);
		if (IsA(l, Var) && IsA(r, Const) && ((Var *) l)->varattno == tsvar->varattno)
		{ varonleft = true; c = (Const *) r; }
		else if (IsA(r, Var) && IsA(l, Const) && ((Var *) r)->varattno == tsvar->varattno)
		{ varonleft = false; c = (Const *) l; }
		else
			continue;
		/*
		 * The Const's type must MATCH the Var's, not merely be one of the two
		 * timestamp types.
		 *
		 * PostgreSQL has cross-type operators for timestamp and timestamptz, so a
		 * mixed predicate keeps a bare Const and reaches this loop rather than
		 * being wrapped in a cast. Both are int64 microseconds and
		 * DatumGetTimestamp reads either, but they are on different scales: a
		 * naive timestamp is wall clock and a timestamptz is UTC. Under a
		 * non-UTC TimeZone the range would be computed across that offset and the
		 * bound would be wrong, in whichever direction the offset points.
		 *
		 * Converting is possible and is not worth it here: no bound is always
		 * safe, and this is an optimisation.
		 */
		if (c->constisnull || c->consttype != tsvar->vartype)
			continue;

		v = (double) DatumGetTimestamp(c->constvalue);
		switch (get_oprrest(op->opno))
		{
			case F_SCALARLTSEL:
			case F_SCALARLESEL:
				if (varonleft) { hi = havehi ? Min(hi, v) : v; havehi = true; }
				else		   { lo = havelo ? Max(lo, v) : v; havelo = true; }
				break;
			case F_SCALARGTSEL:
			case F_SCALARGESEL:
				if (varonleft) { lo = havelo ? Max(lo, v) : v; havelo = true; }
				else		   { hi = havehi ? Min(hi, v) : v; havehi = true; }
				break;
			default:
				break;
		}
	}

	if (!havelo || !havehi || hi <= lo)
		return 0.0;
	return floor((hi - lo) / width) + 2.0;
}

/*
 * pgcolumnar_group_estimate_bound
 *		An upper bound on the group count, for the case where the planner had
 *		nothing to estimate from (#369). Returns 0 when no bound applies.
 *
 *		The gate first. If EVERY grouping expression is informed, we return 0 and
 *		change nothing. "Informed" is core's own test, from examine_variable: a
 *		statsTuple, or a unique index. A plain Var is informed. So is an
 *		expression carrying CREATE STATISTICS ON (expr), or an expression index.
 *		Those outrank anything we could infer and G3, whose key is a plain column,
 *		never reaches the substitution at all.
 *
 *		Then the bound. Only one shape is handled, and deliberately: a truncating
 *		time function over a single Var, which is the shape that produced the
 *		27,772x inflation. The bucket count over the scanned range is a true upper
 *		bound on the distinct values the expression can take, plus one partial
 *		bucket at each end.
 *
 *		The range comes from Const btree bounds on that Var in the relation's own
 *		baserestrictinfo, which describe the rows this scan will actually read.
 *		A query with no such bound gets no bound from us.
 */
static double
pgcolumnar_group_estimate_bound(PlannerInfo *root, RelOptInfo *input_rel,
							  List *groupExprs)
{
	ListCell   *lc;
	double		bound = 1.0;

	if (groupExprs == NIL)
		return 0.0;

	foreach(lc, groupExprs)
	{
		Node	   *expr = (Node *) lfirst(lc);
		VariableStatData vardata;
		bool		informed;
		double		one;

		/*
		 * Core's own question: does anything actually know about this expression?
		 * If so, leave the estimate entirely alone.
		 */
		examine_variable(root, expr, 0, &vardata);
		informed = (HeapTupleIsValid(vardata.statsTuple) || vardata.isunique);
		ReleaseVariableStats(vardata);
		if (informed)
			return 0.0;

		one = pgcolumnar_truncating_time_bound(root, input_rel, expr);
		if (one <= 0.0)
			return 0.0;		/* one unbounded key and the product is unbounded */

		bound *= one;
		if (bound >= (double) INT_MAX)
			return 0.0;
	}
	return bound;
}

static void
PgColumnarTryGroupAggPath(PlannerInfo *root, RelOptInfo *input_rel,
						RelOptInfo *output_rel, void *extra)
{
	RangeTblEntry *rte;
	Oid			relid;
	List	   *groupKeys = NIL;
	List	   *outMap = NIL;
	List	   *quals;
	List	   *groupExprs;
	ListCell   *lc;
	int			naggs = 0;
	double		dNumGroups;
	Path	   *cheapest;
	Cost		serialScanCost = 0.0;
	double		serialScanRows = 0.0;
	CustomPath *cpath;

	/* a single columnar base relation with no joins */
	if (input_rel->reloptkind != RELOPT_BASEREL)
		return;
	if (bms_membership(input_rel->relids) != BMS_SINGLETON)
		return;
	if (input_rel->relid == 0 ||
		input_rel->relid >= (Index) root->simple_rel_array_size)
		return;
	rte = root->simple_rte_array[input_rel->relid];
	if (rte == NULL || rte->rtekind != RTE_RELATION ||
		rte->relkind != RELKIND_RELATION)
		return;
	if (rte->tablesample != NULL)
		return;
	/*
	 * A legacy inheritance parent is a plain RELKIND_RELATION with rte->inh set;
	 * its children hold rows this single-relation scan would never see. Leave the
	 * whole tree to the ordinary Append + Agg plan.
	 */
	if (rte->inh)
		return;
	if (!OidIsValid(rte->relid) || !PgColumnarIsColumnarRelation(rte->relid))
		return;
	relid = rte->relid;

	/* every GROUP BY key must be one we can evaluate and group exactly */
	if (!pgcolumnar_classify_group_keys(root, input_rel, &groupKeys))
		return;

	/*
	 * Every output entry is either a supported aggregate or a bare reference to
	 * one of the group keys. An output expression built on top of a key (a
	 * function of a grouping column) is not handled here and forces the fallback.
	 */
	if (!pgcolumnar_groupagg_outmap(output_rel->reltarget->exprs, groupKeys,
								  input_rel->relid, false, &outMap, &naggs))
		return;

	/*
	 * estimate_num_groups only sizes the path's row estimate; it is deliberately
	 * NOT a gate. For an expression key such as date_trunc(...) the planner
	 * cannot estimate distinctness and returns a count near the input row count
	 * (here ~8.4M estimated against 48k actual), which would wrongly disable this
	 * path on exactly the large tables it helps. The unbounded-hash-table guard
	 * is the execution-time cap on the actual group count
	 * (pgcolumnar.groupagg_max_groups), which errors with guidance -- see
	 * pgcolumnar_groupagg_lookup -- rather than silently declining the feature.
	 */
	groupExprs = groupKeys;
	dNumGroups = estimate_num_groups(root, groupExprs,
									 input_rel->rows > 0 ? input_rel->rows : 1.0,
									 NULL, NULL);

	/*
	 * Bound the estimate when the planner had nothing to estimate FROM (#369).
	 *
	 * estimate_num_groups cannot see through a function. For date_trunc('minute',
	 * ts) it falls back to the underlying column's ndistinct, which for a
	 * high-cardinality timestamp is close to the row count: measured 19,996,000
	 * against 720 actual, 27,772x.
	 *
	 * That number is charged TWICE on the parallel arm, once by the Gather as
	 * parallel_tuple_cost * rows and again by the Finalize's per-group terms, and
	 * not at all on the serial node, which is priced per input row. So the serial
	 * node wins by construction on exactly the shapes where the parallel arm is
	 * measured 4.3x faster (G1) and 3.5x faster (G2).
	 *
	 * The gate is core's own test for "is this estimate informed", from
	 * clausesel.c's use of examine_variable: a statsTuple or a unique index means
	 * somebody measured this expression, and we leave it alone. That covers a
	 * plain Var, an expression index, and a user's CREATE STATISTICS ON (expr),
	 * all of which outrank anything we could infer.
	 *
	 * Only when nothing is informed do we substitute an upper bound, and only ever
	 * downward: Min() cannot raise an estimate, so a shape we get wrong is priced
	 * no worse than it is today.
	 */
	{
		double		bound = pgcolumnar_group_estimate_bound(root, input_rel,
														  groupExprs);

		if (bound > 0.0 && bound < dNumGroups)
			dNumGroups = Max(1.0, bound);
	}

	/*
	 * Pseudoconstant (gating) quals -- e.g. WHERE (SELECT false) -- are one-time
	 * filters, not per-row predicates. The residual ExecQual recheck this path
	 * relies on runs per row and would never apply them, so a false gate would
	 * wrongly return rows. They are rare; leave such a query to the ordinary plan.
	 */
	foreach(lc, input_rel->baserestrictinfo)
	{
		if (lfirst_node(RestrictInfo, lc)->pseudoconstant)
			return;
	}

	/*
	 * WHERE is carried whole and rechecked per row, so correctness never depends
	 * on which clauses become scan keys; the scan keys only prune groups.
	 */
	quals = extract_actual_clauses(input_rel->baserestrictinfo, false);

	/*
	 * A WHERE clause referencing a system column or a whole-row Var cannot be
	 * answered from the projected data columns this path reads, so fall back
	 * rather than evaluate it against unset slot values.
	 */
	{
		Bitmapset  *whereAtts = NULL;
		int			m = -1;

		pull_varattnos((Node *) quals, input_rel->relid, &whereAtts);
		while ((m = bms_next_member(whereAtts, m)) >= 0)
			if (m + FirstLowInvalidHeapAttributeNumber <= 0)
				return;
	}

	/*
	 * Cost from a full columnar scan, not input_rel->cheapest_total_path: the
	 * cheapest overall path may be an index scan, but this node always performs a
	 * full columnar scan, so pricing it from an index scan would understate it.
	 * Take the cheapest non-index path (the columnar custom or sequential scan).
	 *
	 * A *parallel* path is excluded for the same reason and it is not hypothetical
	 * (issue #369). By the time this hook runs, `generate_useful_gather_paths` has
	 * put a Gather over the columnar partial path into `input_rel->pathlist`, and
	 * `add_path` has dropped the serial columnar path it dominates -- so the
	 * cheapest surviving non-index path is routinely the *Gather*, whose cost is
	 * the scan divided across workers. Pricing a node that runs single-threaded
	 * from it understates the scan by the worker count.
	 *
	 * Measured on a 2M-row fixture: the pathlist held only a GatherPath at 15,026
	 * and a GatherMergePath at 103,771, no serial scan at all. The serial grouped
	 * node was priced from the 15,026, against a true serial scan of ~60,104. That
	 * made it unbeatable by any honestly-costed parallel plan, which is why the
	 * parallel arm added in #366 was declined on shapes where it runs 3.9x faster.
	 *
	 * Skipping parallel paths usually leaves nothing, so the fallback below costs
	 * the serial scan directly rather than borrowing a surviving path. That also
	 * removes the dependence on which paths happened to survive `add_path`, which
	 * is the same fragility #362 was.
	 */
	cheapest = NULL;
	foreach(lc, input_rel->pathlist)
	{
		Path	   *p = (Path *) lfirst(lc);

		if (p->pathtype == T_IndexScan || p->pathtype == T_IndexOnlyScan ||
			p->pathtype == T_BitmapHeapScan)
			continue;
		if (p->parallel_aware || p->parallel_workers > 0 ||
			IsA(p, GatherPath) || IsA(p, GatherMergePath))
			continue;
		if (cheapest == NULL || p->total_cost < cheapest->total_cost)
			cheapest = p;
	}
	if (cheapest != NULL)
	{
		serialScanCost = cheapest->total_cost;
		serialScanRows = cheapest->rows;
	}
	else
	{
		/*
		 * Nothing serial survived, which is the common case rather than the rare
		 * one: add_path drops the serial columnar scan once a Gather over the
		 * partial path dominates it. Cost the scan this node performs directly,
		 * the same way PgColumnarSetRelPathlist costs its own fallback, instead of
		 * borrowing whatever path happened to survive.
		 */
		double		ntuples = (input_rel->tuples >= 0) ? input_rel->tuples
			: input_rel->rows;
		Cost		startup;

		/*
		 * Price the input scan the same way PgColumnarSetRelPathlist prices the
		 * columnar scan node -- projected-width I/O, per-column decode CPU, and
		 * zone-map survival -- not with the bare seqscan formula, which under-
		 * priced this node's input on a wide, low-pruning scan.
		 */
		pgcolumnar_refined_scan_cost(input_rel, relid, NULL,
									 &startup, &serialScanCost);
		serialScanRows = (ntuples > 0) ? ntuples : input_rel->rows;
	}
	if (serialScanCost <= 0.0)
		return;

	cpath = makeNode(CustomPath);
	cpath->path.pathtype = T_CustomScan;
	cpath->path.parent = output_rel;
	cpath->path.pathtarget = output_rel->reltarget;
	cpath->path.param_info = NULL;
	cpath->path.parallel_aware = false;
	cpath->path.parallel_safe = false;
	cpath->path.parallel_workers = 0;
	cpath->path.rows = (dNumGroups < 1.0) ? 1.0 : dNumGroups;

	/*
	 * Price the scan this node performs, plus the folding it does over that
	 * scan.
	 *
	 * Charging only the scan, as this did, makes the node unpriceable-against:
	 * every competing plan pays a per-row aggregation cost and this one paid
	 * none, so it won by construction against anything, including a parallel
	 * plan several times faster than itself. That is not a conservative bias, it
	 * is a blind spot -- and it cost ~1.9x on a full-scan GROUP BY with few
	 * groups, where the four-worker plan this displaced was the better one
	 * (issue #349).
	 *
	 * The node is serial, so it cannot simply be priced low and left to win. It
	 * has to compete honestly: it folds vectors instead of advancing a row-wise
	 * Agg, which is cheaper per row, but it does that work on every row without
	 * dividing it across workers. Charging cpu_operator_cost per row per
	 * aggregate -- the same rate core charges a transition -- expresses exactly
	 * that. It is conservative, since the fold is cheaper per row than the
	 * row-wise advance it replaces, and it leaves the node ahead wherever it
	 * actually is ahead.
	 *
	 * Measured against the planner's own numbers on a 100M-row TSBS fixture, per
	 * row per aggregate: any charge above ~0.0013 correctly loses the full-scan
	 * case, and any charge below ~0.0107 correctly wins the windowed ones. The
	 * default cpu_operator_cost of 0.0025 sits inside that range with margin at
	 * both ends, so this is not tuned to the fixture.
	 *
	 * Charging per input row also makes the estimate respond to the number of
	 * aggregates, which the previous cost did not: ten aggregates were priced
	 * identically to one, while costing 2.35x as much to run.
	 *
	 * An earlier version charged per output GROUP, which let autoanalyze's group
	 * estimate flip the choice on large inputs so the node sometimes did not run
	 * at all. Per input row avoids that: it does not depend on n_distinct.
	 *
	 * One row per group comes out only after the whole scan is folded, so there
	 * is no cheap partial start-up.
	 */
	{
		Cost		cost = serialScanCost + cpu_tuple_cost +
			cpu_operator_cost * serialScanRows * (naggs > 0 ? naggs : 1);

		cpath->path.startup_cost = cost;
		cpath->path.total_cost = cost;
	}
	cpath->path.pathkeys = NIL;
	cpath->flags = 0;
	cpath->custom_paths = NIL;
#if PG_VERSION_NUM >= 170000
	cpath->custom_restrictinfo = NIL;
#endif

	/*
	 * custom_private (length 5 marks the grouped path for the shared create-state
	 * dispatch): rti, WHERE quals, relid, group-key expressions, output map. The
	 * planner leaves custom_private untouched by setrefs, so the key and qual
	 * expressions keep their original varnos and evaluate against a base slot.
	 */
	cpath->custom_private =
		list_make5(makeInteger((int) input_rel->relid),
				   copyObject(quals),
				   makeConst(OIDOID, -1, InvalidOid, sizeof(Oid),
							 ObjectIdGetDatum(relid), false, true),
				   groupKeys,
				   outMap);
	cpath->methods = &pgcolumnar_agg_path_methods;

	/*
	 * Parallel arm (#349). The serial node above folds vectors instead of
	 * advancing a row-wise Agg, which is cheaper per row, but it does that work on
	 * every row without dividing it across workers -- so whenever it won it
	 * replaced a four-worker plan with a single-threaded one. #350 made the
	 * *choice* honest; this makes the node itself parallel, so it wins on merit
	 * rather than needing to be priced to win.
	 *
	 * Each worker claims distinct row groups through the shared counter, builds
	 * its own hash table over them, and emits one (group keys, transition states)
	 * tuple per group. A core Finalize re-aggregates across workers by key,
	 * exactly as it does for an ordinary parallel grouped aggregate.
	 */
	if (pgcolumnar_enable_parallel_vector_agg)
	{
		GroupPathExtraData *gpe = (GroupPathExtraData *) extra;
		bool		parallelOk = (gpe != NULL &&
								  (gpe->flags & GROUPING_CAN_PARTIAL_AGG) != 0 &&
								  gpe->partial_costs_set &&
								  output_rel->consider_parallel &&
								  input_rel->partial_pathlist != NIL);
		List	   *partialMap = NIL;
		int			partialAggs = 0;

		/*
		 * Only the kinds whose transition state is a plain, non-internal value a
		 * core Finalize can combine (#343). The rest keep the serial node.
		 */
		foreach(lc, output_rel->reltarget->exprs)
		{
			PgColumnarAggSpec spec;

			if (!parallelOk)
				break;
			if (!IsA(lfirst(lc), Aggref))
				continue;
			if (!pgcolumnar_classify_aggref((Aggref *) lfirst(lc),
										  (int) input_rel->relid, true, false,
										  &spec) ||
				!pgcolumnar_parallel_agg_ok(spec.kind))
				parallelOk = false;
		}

		if (parallelOk)
		{
			RelOptInfo *pgr = fetch_upper_rel(root, UPPERREL_PARTIAL_GROUP_AGG,
											  output_rel->relids);
			Path	   *partialScan = NULL;
			ListCell   *pc;

			/* the cheapest per-worker base scan drives this partial node's cost */
			foreach(pc, input_rel->partial_pathlist)
			{
				Path	   *p = (Path *) lfirst(pc);

				if (p->pathtype == T_IndexScan || p->pathtype == T_IndexOnlyScan ||
					p->pathtype == T_BitmapHeapScan)
					continue;
				if (partialScan == NULL || p->total_cost < partialScan->total_cost)
					partialScan = p;
			}

			/*
			 * pgr->reltarget is core's partial grouping target: the grouping
			 * columns plus the same Aggrefs marked AGGSPLIT_INITIAL_SERIAL that
			 * the Finalize below combines. Reuse it rather than building one, so
			 * the partial and final aggregates are structurally related and
			 * setrefs matches them up -- build our own and they would not. The
			 * output map has to be rebuilt against it, since its column order and
			 * aggregate split both differ from output_rel->reltarget.
			 */
			if (partialScan != NULL && partialScan->parallel_workers >= 1 &&
				pgr->reltarget != NULL &&
				pgcolumnar_groupagg_outmap(pgr->reltarget->exprs, groupKeys,
										 input_rel->relid, true,
										 &partialMap, &partialAggs))
			{
				CustomPath *ppath = makeNode(CustomPath);
				GatherPath *gather;
				double		grows = dNumGroups;
				Cost		pcost = partialScan->total_cost + cpu_tuple_cost +
					cpu_operator_cost * partialScan->rows *
					(partialAggs > 0 ? partialAggs : 1);
#if PG_VERSION_NUM >= 160000
				List	   *groupClause = root->processed_groupClause;
#else
				List	   *groupClause = root->parse->groupClause;
#endif

				ppath->path.pathtype = T_CustomScan;
				ppath->path.parent = pgr;
				ppath->path.pathtarget = pgr->reltarget;
				ppath->path.param_info = NULL;
				ppath->path.parallel_aware = true;
				ppath->path.parallel_safe = true;
				ppath->path.parallel_workers = partialScan->parallel_workers;

				/*
				 * Every worker may see every group, so each emits up to the whole
				 * group count -- not the count divided by the worker count, which
				 * is what an ordinary partial aggregate would estimate.
				 */
				ppath->path.rows = (dNumGroups < 1.0) ? 1.0 : dNumGroups;
				ppath->path.startup_cost = pcost;
				ppath->path.total_cost = pcost;
				ppath->path.pathkeys = NIL;
				ppath->flags = 0;
				ppath->custom_paths = NIL;
#if PG_VERSION_NUM >= 170000
				ppath->custom_restrictinfo = NIL;
#endif
				ppath->custom_private =
					list_make5(makeInteger((int) input_rel->relid),
							   copyObject(quals),
							   makeConst(OIDOID, -1, InvalidOid, sizeof(Oid),
										 ObjectIdGetDatum(relid), false, true),
							   groupKeys,
							   partialMap);
				ppath->methods = &pgcolumnar_agg_path_methods;

				/*
				 * Gather this partial node directly rather than add_partial_path'ing
				 * it: add_partial_path may pfree a dominated path, and we hold the
				 * only pointer create_gather_path needs.
				 */
				gather = create_gather_path(root, pgr, &ppath->path,
											pgr->reltarget, NULL, &grows);

				add_path(output_rel,
						 (Path *) create_agg_path(root, output_rel, &gather->path,
												  output_rel->reltarget,
												  AGG_HASHED,
												  AGGSPLIT_FINAL_DESERIAL,
												  groupClause, NIL,
												  &gpe->agg_final_costs,
												  dNumGroups));
			}
		}
	}

	/*
	 * Offer the serial node too, even when the parallel arm was added, and let
	 * the planner choose between them.
	 *
	 * The ungrouped arm (#343) deliberately does the opposite -- it drops its
	 * serial node once the parallel one exists -- because #133 priced that node at
	 * the cheap Gather cost, so keeping it would wrongly out-cost a genuinely
	 * parallel plan. That reasoning does not carry over: #350 gave this node an
	 * honest per-row charge, so it competes on merit and cannot win by being
	 * underpriced.
	 *
	 * Suppressing it here actively costs. On a ten-aggregate windowed shape the
	 * parallel arm is charged for ten aggregates per row and loses to core's own
	 * parallel plan by a hair, so dropping the serial node left neither: 7,583 ms
	 * with this feature off against 8,944 ms with it on, purely from the missing
	 * path. Offering both makes turning the GUC on a strict addition to the
	 * planner's choices, which is the only form in which an opt-in accelerator can
	 * be safe to enable.
	 */
	add_path(output_rel, &cpath->path);
}

/* -------------------------------------------------------------------------
 * vectorized aggregate: execution
 * ------------------------------------------------------------------------- */

Node *
PgColumnarCreateAggScanState(CustomScan *cscan)
{
	PgColumnarAggScanState *state =
		(PgColumnarAggScanState *) palloc0(sizeof(PgColumnarAggScanState));
	int			naggs = list_length(cscan->custom_scan_tlist);
	ListCell   *lc;
	int			i = 0;

	state->css.ss.ps.type = T_CustomScanState;

	/* custom_private: rti (Integer), quals (List), relid (Const OIDOID) */
	state->scanrelid = (Index) intVal(linitial(cscan->custom_private));
	state->quals = (List *) lsecond(cscan->custom_private);
	state->relid = DatumGetObjectId(((Const *) lthird(cscan->custom_private))->constvalue);

	/*
	 * A parallel partial node (#289 phase 5/6) is planned with
	 * AGGSPLIT_INITIAL_SERIAL aggrefs in its output tuple: it emits per-worker
	 * transition state a core Finalize combines. Detect it here and switch to the
	 * exec methods table that carries the DSM/worker callbacks so each worker
	 * shares the group-claim counter (gap 23).
	 */
	state->isPartial = false;
	foreach(lc, cscan->custom_scan_tlist)
	{
		TargetEntry *tle = (TargetEntry *) lfirst(lc);

		if (IsA(tle->expr, Aggref) &&
			((Aggref *) tle->expr)->aggsplit != AGGSPLIT_SIMPLE)
			state->isPartial = true;
	}
	state->css.methods = state->isPartial
		? &pgcolumnar_agg_parallel_exec_methods
		: &pgcolumnar_agg_exec_methods;

	/* rebuild the aggregate specs from the output tuple's aggregates */
	state->naggs = naggs;
	state->specs = (PgColumnarAggSpec *) palloc0(sizeof(PgColumnarAggSpec) * naggs);
	foreach(lc, cscan->custom_scan_tlist)
	{
		TargetEntry *tle = (TargetEntry *) lfirst(lc);

		/* classified successfully at plan time; -1 skips the varno check.
		 * allowPartial accepts the partial arm's INITIAL_SERIAL aggrefs. */
		(void) pgcolumnar_classify_aggref((Aggref *) tle->expr, -1, true, true,
										&state->specs[i]);
		i++;
	}

	/*
	 * Scan-fold mode (#289) matches the planner's routing: a residual filter, or
	 * any aggregate a zone map cannot answer. The metadata-answerable no-filter
	 * case keeps the zone-map path in PgColumnarExecAggScan.
	 */
	state->scanFold = (state->quals != NIL);
	for (i = 0; i < naggs; i++)
		if (!pgcolumnar_agg_metadata_answerable(state->specs[i].kind))
			state->scanFold = true;

	return (Node *) state;
}

static void
PgColumnarBeginAggScan(CustomScanState *node, EState *estate, int eflags)
{
	PgColumnarAggScanState *state = (PgColumnarAggScanState *) node;
	Relation	rel;
	TupleDesc	tupdesc;
	bool		allConvertible;
	int			a;

	state->resultContext = AllocSetContextCreate(estate->es_query_cxt,
												 "columnar vec agg result",
												 ALLOCSET_SMALL_SIZES);
	state->done = false;
	state->haveStats = false;

	rel = table_open(state->relid, AccessShareLock);
	tupdesc = RelationGetDescr(rel);

	/*
	 * Batch eligibility is a shape property; decide it before the EXPLAIN-only
	 * return so a plain EXPLAIN reports whether the batch fold will run.
	 */
	if (state->scanFold)
	{
		Bitmapset  *proj = NULL;
		int			x;

		/*
		 * Build the projected set BEFORE deciding eligibility, because eligibility
		 * now depends on it: the fold gathers every projected column and needs each
		 * one fixed width (#423). It used to be built after the EXPLAIN-only return,
		 * so a plain EXPLAIN decided eligibility against an empty set and reported
		 * "Columnar Batch Fold: yes" for a shape that falls back at execution.
		 *
		 * Only the set moves. baseSlot and whereState stay below the EXPLAIN return,
		 * since an EXPLAIN-only node must not initialise executor state.
		 */
		pull_varattnos((Node *) state->quals, state->scanrelid, &proj);
		x = -1;
		while ((x = bms_next_member(proj, x)) >= 0)
		{
			AttrNumber	attno = x + FirstLowInvalidHeapAttributeNumber;

			if (attno > 0)
				state->projected = bms_add_member(state->projected, attno - 1);
		}
		for (a = 0; a < state->naggs; a++)
			if (state->specs[a].attidx >= 0)
				state->projected = bms_add_member(state->projected,
												  state->specs[a].attidx);
		if (state->projected == NULL)
			state->projected = bms_make_singleton(0);

		/*
		 * Eligibility BUILDS keys to inspect and throws them away, so bound them
		 * here rather than leaving them in the executor's context for the life
		 * of the query. At execution the enclosing scratch does this job; Begin
		 * has no scratch, so it makes one.
		 */
		{
			MemoryContext tmp = AllocSetContextCreate(CurrentMemoryContext,
													  "columnar vec agg eligibility",
													  ALLOCSET_SMALL_SIZES);
			MemoryContext oldcxt = MemoryContextSwitchTo(tmp);

			state->batchEligible =
				pgcolumnar_batch_shape_eligible(state, tupdesc, NULL, NULL);
			MemoryContextSwitchTo(oldcxt);
			MemoryContextDelete(tmp);
		}
	}

	/*
	 * The EXPLAIN counts, settled BEFORE the EXPLAIN-only return (#726).
	 *
	 * They were assigned below it, so a plain EXPLAIN of this node printed the
	 * palloc0 zero for both, whatever the truth was: a query whose filter really
	 * did become scan keys still reported "Columnar Pushed-Down Filters: 0".
	 * Under ANALYZE the same plan reported the real number, which is why every
	 * existing arm in pushdown_report.sh missed it.
	 *
	 * That is the #602 shape on another line, a plain-EXPLAIN value that was
	 * never computed, and it left the two vectorized nodes disagreeing about one
	 * label: the grouped node has always counted before its own return. Both
	 * calls are catalog and plan only and do no I/O, which is the same argument
	 * the scalar node already makes for computing its keys in EXPLAIN-only mode.
	 *
	 * Only the count survives, and only EXPLAIN reads it. The predicate array was
	 * stored here and never read: what would have applied it had no call site
	 * anywhere in the tree and is deleted (issue #200).
	 *
	 * The call stays rather than the count being hardcoded to zero. This path is
	 * only chosen when the relation has no quals, so the count is always zero
	 * today and the call looks redundant, but that is an inference from a
	 * planner early return several hundred lines away, and it is the kind that
	 * stops being true without anyone noticing. Computing it costs one walk of
	 * an empty list.
	 */
	PgColumnarCountConvertibleQuals(state->quals, state->scanrelid, tupdesc,
								  &state->npreds, &allConvertible);
	state->nscankeys = PgColumnarCountScanKeys(state->quals, state->scanrelid,
											 tupdesc);

	if (eflags & EXEC_FLAG_EXPLAIN_ONLY)
	{
		table_close(rel, AccessShareLock);
		return;
	}

	/*
	 * Guard the native format version before folding any aggregate (#240). The
	 * plain read path checks it in PgColumnarBeginReadWithStorage, but the
	 * zone-map-only aggregate path answers count/min/max from metadata without
	 * ever opening a read state, so the check must also sit here -- otherwise an
	 * unsupported-format table answers from bytes this build may not decode
	 * correctly. PgColumnarStorageId reads the metapage, so its version is checked
	 * here too.
	 */
	PgColumnarCheckNativeFormatVersion(PgColumnarStorageId(rel),
									 RelationGetRelationName(rel));

	/* finish setting up min/max comparison info now that we have the tupdesc */
	for (a = 0; a < state->naggs; a++)
	{
		PgColumnarAggSpec *spec = &state->specs[a];

		if (spec->kind == COLUMNAR_AGG_MIN || spec->kind == COLUMNAR_AGG_MAX)
		{
			Form_pg_attribute att = TupleDescAttr(tupdesc, spec->attidx);
			TypeCacheEntry *tce = lookup_type_cache(att->atttypid,
													TYPECACHE_CMP_PROC_FINFO);

			fmgr_info_copy(&spec->cmpFn, &tce->cmp_proc_finfo, estate->es_query_cxt);
			spec->collation = att->attcollation;
			spec->byval = att->attbyval;
			spec->typlen = att->attlen;
		}
	}

	/*
	 * Scan-fold mode reads and rechecks every surviving row (#289): a virtual
	 * base slot to hold each row, the residual WHERE recheck (scan keys only
	 * prune groups and vectors), and the set of columns the aggregates and the
	 * WHERE reference so the reader returns them.
	 */
	if (state->scanFold)
	{
		state->baseSlot = MakeSingleTupleTableSlot(CreateTupleDescCopy(tupdesc),
												   &TTSOpsVirtual);
		state->whereState = (state->quals != NIL)
			? ExecInitQual(state->quals, &node->ss.ps)
			: NULL;

	}

	table_close(rel, AccessShareLock);
}

/*
 * Running float additions that reproduce core's float4pl/float8pl exactly,
 * including the overflow error: core raises "value out of range: overflow" when a
 * finite + finite addition produces an infinity, and the grouped accumulators
 * must do the same rather than silently carrying an Infinity the scalar Agg would
 * never have produced. Same compiler and flags as the server (PGXS), so the
 * arithmetic is bit-for-bit what float4pl/float8pl compute.
 */
static inline float8
pgcolumnar_float8_pl(float8 a, float8 b)
{
	float8		r = a + b;

	if (unlikely(isinf(r)) && !isinf(a) && !isinf(b))
		ereport(ERROR,
				(errcode(ERRCODE_NUMERIC_VALUE_OUT_OF_RANGE),
				 errmsg("value out of range: overflow")));
	return r;
}

static inline float4
pgcolumnar_float4_pl(float4 a, float4 b)
{
	float4		r = a + b;

	if (unlikely(isinf(r)) && !isinf(a) && !isinf(b))
		ereport(ERROR,
				(errcode(ERRCODE_NUMERIC_VALUE_OUT_OF_RANGE),
				 errmsg("value out of range: overflow")));
	return r;
}

#ifdef HAVE_INT128
/*
 * pgcolumnar_int128_to_numeric
 *		Convert a 128-bit accumulator to numeric, once, at finalize (#785).
 *
 *		sum(int8) and avg(int8) return numeric, and this path used to reach that
 *		by converting EVERY value to numeric and calling numeric_add per row: two
 *		pallocs and a full numeric addition for each of 8,000,000 rows. A profile
 *		of the enabled path was 13.0% make_result_opt_error, 9.3% add_abs, 8.1%
 *		init_var_from_num and 8.5% AllocSet alloc/free -- all of it the numeric
 *		machinery, none of it the aggregate. Core does not do that either: its
 *		int8 accumulators are int128 and convert once.
 *
 *		The fast case is the only one that will ever run in practice: a sum that
 *		fits in int64 converts directly. The split below covers the rest. It is
 *		exact because C division truncates toward zero, so hi and lo carry the
 *		same sign and hi * 10^18 + lo reconstructs v.
 *
 *		hi must fit in int64, which bounds |v| at 9.2e36. A sum of N int64 values
 *		is bounded by N * 9.2e18, so that is N <= 1e18 rows. The int128 itself
 *		overflows first, at about 1.8e19 rows. Neither is reachable, and core
 *		makes the same assumption for the same reason.
 */
static Datum
pgcolumnar_int128_to_numeric(int128 v)
{
	const int128 base = (int128) INT64CONST(1000000000000000000);
	int64		hi;
	int64		lo;
	Datum		nhi;
	Datum		nlo;

	if (v >= (int128) PG_INT64_MIN && v <= (int128) PG_INT64_MAX)
		return DirectFunctionCall1(int8_numeric, Int64GetDatum((int64) v));

	hi = (int64) (v / base);
	lo = (int64) (v % base);
	nhi = DirectFunctionCall2(numeric_mul,
							  DirectFunctionCall1(int8_numeric, Int64GetDatum(hi)),
							  DirectFunctionCall1(int8_numeric,
												  Int64GetDatum(INT64CONST(1000000000000000000))));
	nlo = DirectFunctionCall1(int8_numeric, Int64GetDatum(lo));
	return DirectFunctionCall2(numeric_add, nhi, nlo);
}
#endif

/*
 * pgcolumnar_apply_one
 *		Fold one value (or a null) into an aggregate accumulator. This is the
 *		reference per-row semantics, shared by the ungrouped scan path
 *		(pgcolumnar_native_scan_agg) and the grouped path (pgcolumnar_groupagg_build).
 */
static void
pgcolumnar_apply_one(MemoryContext resultContext, PgColumnarAggSpec *spec,
				   Datum val, bool isnull)
{
	switch (spec->kind)
	{
		case COLUMNAR_AGG_COUNT_STAR:
			spec->count++;
			break;

		case COLUMNAR_AGG_COUNT_COL:
			if (!isnull)
				spec->count++;
			break;

		case COLUMNAR_AGG_SUM_INT:
			if (!isnull)
			{
				int64		v = (spec->inputType == INT2OID)
					? (int64) DatumGetInt16(val)
					: (int64) DatumGetInt32(val);

				spec->sum += v;
				spec->sawValue = true;
			}
			break;

		case COLUMNAR_AGG_AVG_INT:
			if (!isnull)
			{
				int64		v = (spec->inputType == INT2OID)
					? (int64) DatumGetInt16(val)
					: (int64) DatumGetInt32(val);

				spec->sum += v;
				spec->count++;
			}
			break;

		case COLUMNAR_AGG_SUM_FLOAT:
			if (!isnull)
			{
				/*
				 * sum(real) accumulates in real (core float4pl) and returns real;
				 * sum(double precision) accumulates in float8 (core float8pl). Core's
				 * sum has a strict transfn with a null initial state, so it assigns
				 * the FIRST non-null value directly -- preserving a signed zero --
				 * rather than adding it to +0.0. Match that, then fold the rest with
				 * float4pl/float8pl semantics. For the float4 case fsum always holds
				 * a value exactly representable as float4, so the round-trip through
				 * (float4) reproduces float4pl step for step.
				 */
				if (!spec->sawValue)
					spec->fsum = (spec->inputType == FLOAT4OID)
						? (float8) DatumGetFloat4(val)
						: DatumGetFloat8(val);
				else if (spec->inputType == FLOAT4OID)
					spec->fsum = (float8) pgcolumnar_float4_pl((float4) spec->fsum,
															 DatumGetFloat4(val));
				else
					spec->fsum = pgcolumnar_float8_pl(spec->fsum,
													DatumGetFloat8(val));
				spec->sawValue = true;
			}
			break;

		case COLUMNAR_AGG_AVG_FLOAT:
			if (!isnull)
			{
				/*
				 * avg is Sx/N, but core's float4_accum/float8_accum keep the
				 * Youngs-Cramer Sxx as well and raise overflow when EITHER Sx or Sxx
				 * goes finite+finite -> inf. Track Sxx only to reproduce that error
				 * exactly (Sxx can overflow on finite inputs while Sx stays finite);
				 * the returned average is Sx/N and is unaffected by it.
				 */
				float8		v = (spec->inputType == FLOAT4OID)
					? (float8) DatumGetFloat4(val)
					: DatumGetFloat8(val);
				float8		n = (float8) spec->count;	/* N before this value */
				float8		newN = n + 1.0;
				float8		newSx = spec->fsum + v;

				if (spec->count > 0)
				{
					float8		tmp = v * newN - newSx;
					float8		newSxx = spec->fsxx + tmp * tmp / (n * newN);

					if ((isinf(newSx) || isinf(newSxx)) &&
						!isinf(spec->fsum) && !isinf(v))
						ereport(ERROR,
								(errcode(ERRCODE_NUMERIC_VALUE_OUT_OF_RANGE),
								 errmsg("value out of range: overflow")));
					spec->fsxx = newSxx;
				}
				spec->fsum = newSx;
				spec->count++;
				spec->sawValue = true;
			}
			break;

		case COLUMNAR_AGG_SUM_INT8:
		case COLUMNAR_AGG_AVG_INT8:
		case COLUMNAR_AGG_SUM_NUMERIC:
		case COLUMNAR_AGG_AVG_NUMERIC:
#ifdef HAVE_INT128
			/*
			 * int8 accumulates in 128 bits and converts once at finalize
			 * (#785). The numeric kinds cannot: their input is already numeric
			 * and has a scale, so they keep the running-numeric path below.
			 */
			if (spec->kind == COLUMNAR_AGG_SUM_INT8 ||
				spec->kind == COLUMNAR_AGG_AVG_INT8)
			{
				if (!isnull)
				{
					spec->i128sum += (int128) DatumGetInt64(val);
					spec->nsumSet = true;
					spec->count++;
					/*
					 * Set for parity with the numeric path below rather than
					 * because this kind reads it. Two paths that are meant to be
					 * equivalent should not leave a state field diverging: the
					 * next reader of sawValue would then behave differently
					 * depending on which one ran.
					 */
					spec->sawValue = true;
				}
				break;
			}
#endif
			if (!isnull)
			{
				bool		is_int8 = (spec->kind == COLUMNAR_AGG_SUM_INT8 ||
									   spec->kind == COLUMNAR_AGG_AVG_INT8);
				MemoryContext old = MemoryContextSwitchTo(resultContext);
				Datum		nv = is_int8
					? DirectFunctionCall1(int8_numeric, val)
					: val;

				/*
				 * Keep live memory O(groups), not O(rows): free the previous
				 * running sum and each per-row int8->numeric intermediate. Without
				 * this every scanned row leaked one or two numerics into the
				 * per-group context, which is not reset until end of scan -- O(rows)
				 * on exactly the 100M-row shape this path targets.
				 */
				if (!spec->nsumSet)
				{
					/* int8: nv is freshly allocated and becomes the running sum;
					 * numeric: nv is borrowed from the reader, so copy it. */
					spec->nsum = is_int8 ? nv : datumCopy(nv, false, -1);
					spec->nsumSet = true;
				}
				else
				{
					Datum		newsum = DirectFunctionCall2(numeric_add,
															 spec->nsum, nv);

					pfree(DatumGetPointer(spec->nsum));
					spec->nsum = newsum;
					if (is_int8)
						pfree(DatumGetPointer(nv));
				}
				spec->count++;
				spec->sawValue = true;
				MemoryContextSwitchTo(old);
			}
			break;

		case COLUMNAR_AGG_MIN:
		case COLUMNAR_AGG_MAX:
			if (!isnull)
			{
				bool		take;

				if (!spec->sawValue)
					take = true;
				else
				{
					int32		cmp = DatumGetInt32(
						FunctionCall2Coll(&spec->cmpFn, spec->collation,
										  val, spec->extreme));

					/*
					 * On a tie, take the later value, matching core's larger and
					 * smaller comparators (transfn(state, newval) returns newval
					 * when equal). Observable for numeric 1.0 vs 1.00, which tie
					 * by value but differ in display scale.
					 */
					take = (spec->kind == COLUMNAR_AGG_MIN)
						? (cmp <= 0) : (cmp >= 0);
				}

				if (take)
				{
					MemoryContext old =
						MemoryContextSwitchTo(resultContext);

					if (spec->sawValue && !spec->byval)
						pfree(DatumGetPointer(spec->extreme));
					spec->extreme = datumCopy(val, spec->byval, spec->typlen);
					spec->sawValue = true;
					MemoryContextSwitchTo(old);
				}
			}
			break;
	}
}


/*
 * pgcolumnar_agg_emit_partial
 *		A parallel partial node (#289 phase 5/6) emits each aggregate's transition
 *		state, not its finalized value, for a core Finalize Aggregate to combine.
 *		The transition types match core's own partial aggregate exactly, so
 *		int8pl (count) and float8_combine + float8_avg (avg) reproduce an ordinary
 *		parallel aggregate -- overflow parity included, because float8_combine
 *		re-derives and re-checks the Youngs-Cramer Sxx we pass through.
 */
static Datum
pgcolumnar_agg_emit_partial(PgColumnarAggSpec *spec, bool *isnull)
{
	*isnull = false;

	switch (spec->kind)
	{
		case COLUMNAR_AGG_COUNT_STAR:
		case COLUMNAR_AGG_COUNT_COL:
			/* transtype int8: the running count is the partial state */
			return Int64GetDatum(spec->count);

		case COLUMNAR_AGG_SUM_INT:

			/*
			 * sum(int2/int4) -> int8 transtype. NULL until a value is seen: core's
			 * sum(int) carries a NULL state for an empty input and its strict int8pl
			 * combine then treats a worker that matched no rows as absent, so the
			 * cross-worker sum and its overflow check match an ordinary parallel sum.
			 */
			if (!spec->sawValue)
			{
				*isnull = true;
				return (Datum) 0;
			}
			return Int64GetDatum(spec->sum);

		case COLUMNAR_AGG_SUM_FLOAT:

			/*
			 * transtype is the identity float (float4 for sum(real), float8 for
			 * sum(double)); NULL until a value is seen so a strict float4pl/float8pl
			 * combine treats a worker that matched no rows as absent, exactly as the
			 * serial finalize does.
			 */
			if (!spec->sawValue)
			{
				*isnull = true;
				return (Datum) 0;
			}
			return (spec->inputType == FLOAT4OID)
				? Float4GetDatum((float4) spec->fsum)
				: Float8GetDatum(spec->fsum);

		case COLUMNAR_AGG_AVG_FLOAT:
			{
				/*
				 * transtype _float8 {N, Sx, Sxx}: the same Youngs-Cramer state
				 * float4_accum/float8_accum build and float8_combine merges. Always
				 * non-null -- an empty worker emits {0,0,0}, which float8_avg maps to
				 * NULL after the combine.
				 */
				Datum		elems[3];

				elems[0] = Float8GetDatum((float8) spec->count);
				elems[1] = Float8GetDatum(spec->fsum);
				elems[2] = Float8GetDatum(spec->fsxx);
				/*
				 * construct_array with explicit float8 type params, exactly as
				 * core's float8_accum builds this transition array. NOT
				 * construct_array_builtin: its supported-type list did not include
				 * float8 (type 701) until PG18, so it errors on PG15-17.
				 */
				return PointerGetDatum(construct_array(elems, 3, FLOAT8OID,
													   sizeof(float8),
													   FLOAT8PASSBYVAL,
													   TYPALIGN_DOUBLE));
			}

		case COLUMNAR_AGG_AVG_INT:
			{
				/*
				 * avg(int2/int4) -> _int8 {N, sum}: the same array int4_avg_accum
				 * builds and int4_avg_combine merges, finalized by int8_avg to
				 * numeric. Always non-null -- an empty worker emits {0,0}, which
				 * int8_avg maps to NULL after the combine. int8's by-value/alignment
				 * follow FLOAT8PASSBYVAL / TYPALIGN_DOUBLE, exactly as core builds it.
				 */
				Datum		elems[2];

				elems[0] = Int64GetDatum(spec->count);
				elems[1] = Int64GetDatum(spec->sum);
				return PointerGetDatum(construct_array(elems, 2, INT8OID,
													   sizeof(int64),
													   FLOAT8PASSBYVAL,
													   TYPALIGN_DOUBLE));
			}

		default:

			/*
			 * The parallel arm is gated to the kinds above
			 * (pgcolumnar_parallel_agg_ok), so this is unreachable; fail loudly rather
			 * than emit a value of the wrong transition type.
			 */
			elog(ERROR, "columnar parallel partial: unsupported aggregate kind %d",
				 (int) spec->kind);
			return (Datum) 0;	/* keep the compiler happy */
	}
}

/*
 * pgcolumnar_agg_finalize
 *		Turn one accumulator into its output Datum, reproducing PostgreSQL's
 *		aggregate result types and empty-input behaviour exactly.
 */
static Datum
pgcolumnar_agg_finalize(PgColumnarAggSpec *spec, bool *isnull)
{
	*isnull = false;

	switch (spec->kind)
	{
		case COLUMNAR_AGG_COUNT_STAR:
		case COLUMNAR_AGG_COUNT_COL:
			return Int64GetDatum(spec->count);

		case COLUMNAR_AGG_SUM_INT:
			if (!spec->sawValue)
			{
				*isnull = true;
				return (Datum) 0;
			}
			return Int64GetDatum(spec->sum);	/* sum(int2/int4) -> int8 */

		case COLUMNAR_AGG_AVG_INT:
			if (spec->count == 0)
			{
				*isnull = true;
				return (Datum) 0;
			}
			else
			{
				/* avg(int) -> numeric, exactly as int8_avg: sum/count in numeric */
				Datum		sumd = DirectFunctionCall1(int8_numeric,
													   Int64GetDatum(spec->sum));
				Datum		cntd = DirectFunctionCall1(int8_numeric,
													   Int64GetDatum(spec->count));

				return DirectFunctionCall2(numeric_div, sumd, cntd);
			}

		case COLUMNAR_AGG_SUM_FLOAT:
			if (!spec->sawValue)
			{
				*isnull = true;
				return (Datum) 0;
			}
			/* sum(real) -> real; sum(double precision) -> double precision */
			return (spec->inputType == FLOAT4OID)
				? Float4GetDatum((float4) spec->fsum)
				: Float8GetDatum(spec->fsum);

		case COLUMNAR_AGG_AVG_FLOAT:
			if (spec->count == 0)
			{
				*isnull = true;
				return (Datum) 0;
			}
			return Float8GetDatum(spec->fsum / (float8) spec->count);

		case COLUMNAR_AGG_SUM_INT8:
		case COLUMNAR_AGG_SUM_NUMERIC:
			if (!spec->nsumSet)
			{
				*isnull = true;
				return (Datum) 0;
			}
#ifdef HAVE_INT128
			if (spec->kind == COLUMNAR_AGG_SUM_INT8)
				return pgcolumnar_int128_to_numeric(spec->i128sum);
#endif
			return spec->nsum;

		case COLUMNAR_AGG_AVG_INT8:
		case COLUMNAR_AGG_AVG_NUMERIC:
			if (spec->count == 0 || !spec->nsumSet)
			{
				*isnull = true;
				return (Datum) 0;
			}
			return DirectFunctionCall2(numeric_div,
#ifdef HAVE_INT128
									   spec->kind == COLUMNAR_AGG_AVG_INT8
									   ? pgcolumnar_int128_to_numeric(spec->i128sum)
									   : spec->nsum,
#else
									   spec->nsum,
#endif
									   DirectFunctionCall1(int8_numeric,
														   Int64GetDatum(spec->count)));

		case COLUMNAR_AGG_MIN:
		case COLUMNAR_AGG_MAX:
			if (!spec->sawValue)
			{
				*isnull = true;
				return (Datum) 0;
			}
			return spec->extreme;
	}

	*isnull = true;
	return (Datum) 0;
}

/*
 * pgcolumnar_fill_native_metadata_agg
 *		Answer an ungrouped, unfiltered aggregate over a native (PGCN v1) table
 *		from its whole-chunk zone maps (native spec 7.1, D5b): count(*) from
 *		row-group row counts, count(col) and the avg count from value_count, sum
 *		and the avg sum from the zone int sum (int2/int4), and min/max from the
 *		zone min/max. The upper-path hook adds this path only when every aggregate
 *		is so answerable and there is no filter.
 *
 *		Deletes are handled per row group rather than per storage (issue #149). A
 *		zone map describes every row written into its group, deleted ones
 *		included, so a group with deletes cannot be folded from its zone map. It
 *		used to be that one deleted row anywhere disabled this path for the whole
 *		table, and a six-million-row table lost a 0.02 ms count(*) to a 222 ms scan
 *		because a single row was gone. Deletion is a property of a group, so the
 *		decision belongs there: clean groups fold from their zone maps, and only
 *		the groups that actually have deletes are read.
 *
 *		Returns the groups that must be scanned, by group number, with *ndirty set
 *		to their count. A count(*)-only query never returns any: count(*) over a
 *		group is rowCount minus the deleted count, which is exact and needs no
 *		data pages even when the group has deletes.
 */
static uint64 *
pgcolumnar_fill_native_metadata_agg(PgColumnarAggScanState *state, int *ndirty)
{
	EState	   *estate = state->css.ss.ps.state;
	Relation	rel;
	TupleDesc	tupdesc;
	Snapshot	snap;
	uint64		storageId;
	List	   *groups;
	ListCell   *lc;
	bool		needZones = false;
	bool		anyDeletes;
	uint64	   *dirty;
	int			na;

	/*
	 * count(*) comes from the row group's own stored row count. Every other
	 * aggregate here reads a zone map, and reading them means one catalog lookup
	 * per row group returning an entry for every column, whether the query names
	 * that column or not. For a count(*) on its own that is all waste, and it is
	 * the whole cost of the query: at 6,000,000 rows and the default limits it
	 * measured 5.2 ms over 40 row groups, and 1.4 ms over 10 (issue #133).
	 */
	for (na = 0; na < state->naggs; na++)
	{
		if (state->specs[na].kind != COLUMNAR_AGG_COUNT_STAR)
		{
			needZones = true;
			break;
		}
	}

	PgColumnarFlushWriteStateForRelation(state->relid);
	rel = table_open(state->relid, AccessShareLock);
	tupdesc = RelationGetDescr(rel);
	snap = PgColumnarCatalogSnapshot(estate->es_snapshot);
	storageId = PgColumnarStorageId(rel);

	/*
	 * One storage-wide probe first. When nothing is deleted no group can have
	 * deletes, so the per-group delete lookup below is pure cost; skipping it
	 * keeps a clean table at exactly the catalog traffic it had before this
	 * change, which for a count(*) is the row group list and nothing else.
	 */
	anyDeletes = PgColumnarStorageHasDeleteVector(storageId, snap);

	groups = PgColumnarReadRowGroupList(storageId, snap);
	dirty = palloc(sizeof(uint64) * (list_length(groups) > 0
									 ? list_length(groups) : 1));
	*ndirty = 0;

	foreach(lc, groups)
	{
		NativeRowGroupMetadata *rg = (NativeRowGroupMetadata *) lfirst(lc);
		NativeZoneMapMetadata **byCol = NULL;
		uint64		deleted = 0;
		int			a;

		if (anyDeletes)
			deleted = PgColumnarGroupDeletedCount(storageId, rg, snap);

		if (deleted > 0)
		{
			/*
			 * This group's zone maps describe deleted rows too, so they cannot
			 * answer anything that reads a value. count(*) is the exception: the
			 * live row count is exactly rowCount - deleted, so a count(*)-only
			 * query stays on metadata even here. Anything else defers the whole
			 * group to the scan, which folds every aggregate for it -- including
			 * count(*), so nothing is counted twice.
			 */
			if (!needZones)
			{
				for (a = 0; a < state->naggs; a++)
				{
					Assert(state->specs[a].kind == COLUMNAR_AGG_COUNT_STAR);
					state->specs[a].count += (int64) (rg->rowCount - deleted);
				}
			}
			else
				dirty[(*ndirty)++] = rg->groupNumber;
			continue;
		}

		if (needZones)
		{
			List	   *zones = PgColumnarReadZoneMapList(storageId,
														rg->groupNumber, snap);
			ListCell   *zc;

			byCol = palloc0(sizeof(NativeZoneMapMetadata *) * tupdesc->natts);
			foreach(zc, zones)
			{
				NativeZoneMapMetadata *z = (NativeZoneMapMetadata *) lfirst(zc);

				if (z->columnIndex >= 0 && z->columnIndex < tupdesc->natts)
					byCol[z->columnIndex] = z;
			}
		}

		/*
		 * A column added by ALTER TABLE ADD COLUMN has no chunk, and so no zone
		 * map, in any row group written before it existed. Its value for those
		 * rows is the attribute's missing value, which the reader supplies
		 * through getmissingattr -- but a zone map cannot, because there is
		 * none. Folding such a group from its zone maps silently dropped every
		 * one of those rows: count(col) came back 0 where a scan returned the
		 * full row count, and sum, min and max came back null.
		 *
		 * Rather than reconstruct the missing value here, the group joins the
		 * ones that have to be read. The scan path already produces missing
		 * values correctly, and a group predating the column is exactly a group
		 * whose contents the metadata cannot describe.
		 */
		if (needZones)
		{
			bool		missingColumn = false;

			for (a = 0; a < state->naggs; a++)
			{
				int			ai = state->specs[a].attidx;

				if (state->specs[a].kind == COLUMNAR_AGG_COUNT_STAR)
					continue;
				if (ai < 0 || ai >= tupdesc->natts)
					continue;
				if (byCol[ai] == NULL)
				{
					missingColumn = true;
					break;
				}
			}

			if (missingColumn)
			{
				dirty[(*ndirty)++] = rg->groupNumber;
				continue;
			}
		}

		for (a = 0; a < state->naggs; a++)
		{
			PgColumnarAggSpec *spec = &state->specs[a];
			NativeZoneMapMetadata *z =
				(byCol != NULL && spec->attidx >= 0 &&
				 spec->attidx < tupdesc->natts)
				? byCol[spec->attidx] : NULL;

			switch (spec->kind)
			{
				case COLUMNAR_AGG_COUNT_STAR:
					spec->count += (int64) rg->rowCount;
					break;
				case COLUMNAR_AGG_COUNT_COL:
					if (z != NULL)
						spec->count += (int64) z->valueCount;
					break;
				case COLUMNAR_AGG_SUM_INT:
					if (z != NULL && z->hasSum)
					{
						spec->sum += DatumGetInt64(
							DirectFunctionCall1(numeric_int8, z->sum));
						if (z->valueCount > 0)
							spec->sawValue = true;
					}
					break;
				case COLUMNAR_AGG_AVG_INT:
					if (z != NULL)
					{
						if (z->hasSum)
							spec->sum += DatumGetInt64(
								DirectFunctionCall1(numeric_int8, z->sum));
						spec->count += (int64) z->valueCount;
					}
					break;
				case COLUMNAR_AGG_MIN:
				case COLUMNAR_AGG_MAX:
					if (z != NULL && z->hasMinMax)
					{
						Form_pg_attribute att = TupleDescAttr(tupdesc, spec->attidx);
						MemoryContext oldcx =
							MemoryContextSwitchTo(state->resultContext);
						char	   *cur = (spec->kind == COLUMNAR_AGG_MIN)
							? (char *) z->minimum : (char *) z->maximum;
						const char *curEnd = (spec->kind == COLUMNAR_AGG_MIN)
							? z->minimum + z->minimumLen : z->maximum + z->maximumLen;
						Datum		v = PgColumnarDecodeValue(att, &cur, curEnd,
														state->resultContext);

						if (!spec->sawValue)
						{
							spec->extreme = v;
							spec->sawValue = true;
						}
						else
						{
							int32		c = DatumGetInt32(
								FunctionCall2Coll(&spec->cmpFn, spec->collation,
												  v, spec->extreme));

							if ((spec->kind == COLUMNAR_AGG_MIN && c < 0) ||
								(spec->kind == COLUMNAR_AGG_MAX && c > 0))
								spec->extreme = v;
						}
						MemoryContextSwitchTo(oldcx);
					}
					break;
				default:

					/*
					 * The extended int8/float/numeric sum/avg kinds are produced
					 * only for the grouped path; the ungrouped classifier rejects
					 * them, so they never reach this metadata fold.
					 */
					Assert(false);
					break;
			}
		}
	}

	table_close(rel, AccessShareLock);
	return dirty;
}

/* -------------------------------------------------------------------------
 * ungrouped batch fold (#289)
 *
 * The row-at-a-time path materializes every projected column into a Datum tuple,
 * resets a per-row memory context, and hands the tuple to ExecQual and the fold,
 * once per row. For a full-scan ungrouped aggregate that per-row tax is the whole
 * cost. The batch fold instead walks each loaded group's packed value streams,
 * evaluates a pushable WHERE inline, and folds each surviving value through the
 * same pgcolumnar_apply_one -- so accumulators are byte-identical to the row path,
 * floats included -- with none of the per-row Datum, context, or executor cost.
 * It runs only when every aggregate and the whole WHERE are batch-eligible; a
 * false return means it folded nothing and the caller runs the always-correct
 * row path.
 * ------------------------------------------------------------------------- */

/* PostgreSQL's float total order: NaN sorts above every non-NaN, NaN == NaN, and
 * -0.0 == 0.0, matching float8_cmp_internal so an inline compare equals the
 * operator ExecQual would call. */
static inline int
pgcolumnar_batch_float_cmp(double a, double b)
{
	if (isnan(a))
		return isnan(b) ? 0 : 1;
	if (isnan(b))
		return -1;
	return (a < b) ? -1 : (a > b) ? 1 : 0;
}

/* Whether one fixed-width numeric column value (known non-null) satisfies one
 * btree scan key. */
static bool
pgcolumnar_batch_key_pass(Oid coltype, Datum val, StrategyNumber strat, Datum arg)
{
	int			c;

	switch (coltype)
	{
		case INT2OID:
			{ int16 a = DatumGetInt16(val), b = DatumGetInt16(arg); c = (a < b) ? -1 : (a > b) ? 1 : 0; break; }
		case INT4OID:
			{ int32 a = DatumGetInt32(val), b = DatumGetInt32(arg); c = (a < b) ? -1 : (a > b) ? 1 : 0; break; }
		case INT8OID:
			{ int64 a = DatumGetInt64(val), b = DatumGetInt64(arg); c = (a < b) ? -1 : (a > b) ? 1 : 0; break; }
		case FLOAT4OID:
			c = pgcolumnar_batch_float_cmp((double) DatumGetFloat4(val), (double) DatumGetFloat4(arg)); break;
		case FLOAT8OID:
			c = pgcolumnar_batch_float_cmp(DatumGetFloat8(val), DatumGetFloat8(arg)); break;
		default:
			return false;
	}
	switch (strat)
	{
		case BTLessStrategyNumber: return c < 0;
		case BTLessEqualStrategyNumber: return c <= 0;
		case BTEqualStrategyNumber: return c == 0;
		case BTGreaterEqualStrategyNumber: return c >= 0;
		case BTGreaterStrategyNumber: return c > 0;
		default: return false;
	}
}

/* A fixed-width by-value numeric column type the batch fold can read directly. */
static bool
pgcolumnar_batch_type_ok(Oid typ)
{
	return typ == INT2OID || typ == INT4OID || typ == INT8OID ||
		typ == FLOAT4OID || typ == FLOAT8OID;
}

/* An aggregate kind the batch fold accumulates (folded via pgcolumnar_apply_one
 * over a fixed-width by-value numeric column, or count). int8/numeric sum/avg and
 * min/max stay on the row path. */
static bool
pgcolumnar_batch_agg_ok(PgColumnarAggKind kind)
{
	/*
	 * Admitting a kind here makes pgcolumnar_agg_specs_reset reachable for it,
	 * on the batch fold's fall-back path. Check that every accumulator the kind
	 * uses is cleared there before adding one. The int8 kinds accumulate in
	 * spec->i128sum (#786), which that function did not clear until the omission
	 * was found while scoping #755 question 3.
	 *
	 * Admitting a kind here does NOT by itself make a stale accumulator
	 * observable -- see the note on that function for why the one known
	 * fall-back trigger cannot reach it -- so do not rely on a test to catch a
	 * missing reset. Read it.
	 */
	switch (kind)
	{
		case COLUMNAR_AGG_COUNT_STAR:
		case COLUMNAR_AGG_COUNT_COL:
		case COLUMNAR_AGG_SUM_INT:
		case COLUMNAR_AGG_AVG_INT:
		case COLUMNAR_AGG_SUM_FLOAT:
		case COLUMNAR_AGG_AVG_FLOAT:
			return true;
#ifdef HAVE_INT128
		case COLUMNAR_AGG_SUM_INT8:
		case COLUMNAR_AGG_AVG_INT8:
			/*
			 * #755 q3. The fold reads a column at a time and then calls
			 * pgcolumnar_apply_one per value, so a kind is foldable when its
			 * accumulate is cheap, not when it has a combinable transtype. Since
			 * #786 the int8 kinds accumulate into an int128 and allocate
			 * nothing, which is what makes them eligible here.
			 *
			 * This is NOT parallel eligibility. pgcolumnar_parallel_agg_ok is a
			 * separate predicate with its own callers, and int8 stays out of it
			 * on its own merits: an int128 running total is not a partial state
			 * a core Finalize can combine.
			 *
			 * Guarded on HAVE_INT128 because without it apply_one takes the
			 * per-row numeric path, which allocates twice per value, and the
			 * fold would be folding the expensive accumulate faster rather than
			 * making it cheap.
			 */
			return true;
#endif
		default:
			return false;
	}
}

/*
 * pgcolumnar_batch_gates_ok
 *		The batch fold's shape gates, shared by the ungrouped node
 *		(pgcolumnar_native_batch_fold) and the grouped one
 *		(pgcolumnar_groupagg_batch_fold, #708).
 *
 *		Every aggregate is batch-accumulable, the whole WHERE converts to scan
 *		keys EXACTLY (no residual, no merely-conservative key), every column the
 *		fold will gather is passed by value, and every scan key is a supported
 *		btree comparison on a batch-readable column. When keysOut is non-NULL the
 *		built scan keys are returned for the fold to reuse; otherwise they are
 *		only inspected. Deterministic from the query shape, so Begin can report
 *		it in EXPLAIN before execution.
 *
 *		Single-sourced rather than copied per node ON PURPOSE. Both folds gather
 *		packed column values themselves and filter rows with a scan-key loop and
 *		nothing else, so both need exactly these guarantees, and every one of
 *		them below was added to fix a wrong answer or a crash. A second fold
 *		arriving with a stale copy of the list is precisely how #715 comes back.
 *
 *		The GROUPED caller has one further requirement of its own -- each group
 *		key must be a plain Var it can read out of a gathered column -- which is
 *		specific to that node and stays there.
 */
static bool
pgcolumnar_batch_gates_ok(const PgColumnarAggSpec *specs, int naggs,
						List *quals, Index scanrelid, Bitmapset *projected,
						TupleDesc tupdesc, ScanKey *keysOut, int *nkeysOut)
{
	ScanKey		keys;
	int			nkeys = 0;
	int			npred;
	bool		allConvertible;
	int			a;
	int			k;
	bool		ok = true;

	for (a = 0; a < naggs; a++)
		if (!pgcolumnar_batch_agg_ok(specs[a].kind))
			return false;

	PgColumnarCountConvertibleQuals(quals, scanrelid, tupdesc,
								  &npred, &allConvertible);
	if (!allConvertible)
		return false;

	/*
	 * LOAD-BEARING. allConvertible only means every clause is a strict OpExpr
	 * the fold could evaluate; it does NOT mean the clause becomes a scan key.
	 * The fold's only per-row filter is the scan-key loop, so a clause that
	 * produces no key (`<>`, strict but with no btree strategy) or only a
	 * conservative pruning key would let non-matching rows be counted (#715).
	 *
	 * The gate below is exactly the exactness marker the conservative keys need
	 * kept out of the fold: PgColumnarBuildScanKeys emits keys WEAKER than their
	 * clause for a ScalarArrayOpExpr ([min, max] range, #704) and for an anchored
	 * LIKE (#426), and pgcolumnar_clause_to_scankey now reports that inexactness.
	 * PgColumnarQualsExactlyKeyed demands one EXACT key per clause, so those keys
	 * never serve as the fold's WHERE even if pgcolumnar_clause_to_predicate were
	 * later taught to accept a SAOP. The byval gather guard below is a second,
	 * independent reason LIKE and other varlena filters fall back. Both
	 * native_saop_pushdown.sh's vector-agg arms and ungrouped_vector_agg.sh's
	 * #715 arms go red if this regresses.
	 */
	if (!PgColumnarQualsExactlyKeyed(quals, scanrelid, tupdesc))
		return false;

	/*
	 * Every column the fold will GATHER must be passed BY VALUE (#423).
	 *
	 * The gather does pointer arithmetic on attlen and hardcodes attbyval:
	 *
	 *     cval[col] = fetch_att(cpacked[col] + cpresent[col] * cattlen[col],
	 *                           true, cattlen[col]);
	 *                           ^^^^ not the column's real attbyval
	 *
	 * So the requirement is not "fixed width", which was this guard's first and
	 * wrong form. A varlena has attlen -1 and fails on the arithmetic. But a
	 * FIXED-WIDTH BY-REFERENCE type passes an attlen > 0 test and still fails,
	 * because fetch_att will not pass 16 or 64 bytes by value: uuid is attlen 16
	 * and name is attlen 64, and both raised "unsupported byval length: 16" and
	 * "... 64" while EXPLAIN still reported the fold as engaged.
	 *
	 * attbyval is the property the gather actually depends on, and it implies
	 * attlen is 1, 2, 4 or 8, so it subsumes the width test rather than adding
	 * to it. uuid in particular is ordinary in the event-log shapes this fold
	 * targets.
	 *
	 * The type check below is not this check and cannot stand in for it. It walks
	 * the SCAN KEYS and asks whether each type is comparable, while the gather
	 * walks state->projected and needs to know whether each type is fixed width.
	 * A text column filtered with an unanchored LIKE is in projected and not in
	 * the keys (an anchored LIKE builds only conservative range keys, #426; an
	 * unanchored one builds none), so it reached the gather unchecked. That is
	 * exactly ClickBench q21, SELECT COUNT(*) FROM hits WHERE URL LIKE '%google%'.
	 *
	 * Falling back to the row path is what the ADD COLUMN case already does.
	 */
	{
		int			c = -1;

		while ((c = bms_next_member(projected, c)) >= 0)
		{
			if (c < 0 || c >= tupdesc->natts)
				continue;
			if (!TupleDescAttr(tupdesc, c)->attbyval)
				return false;
		}
	}

	keys = PgColumnarBuildScanKeys(quals, scanrelid, tupdesc, &nkeys);
	for (k = 0; k < nkeys; k++)
	{
		ScanKey		key = &keys[k];
		int			attidx = key->sk_attno - 1;
		Oid			coltype;

		if (key->sk_flags != 0 || attidx < 0 || attidx >= tupdesc->natts)
			{ ok = false; break; }
		coltype = TupleDescAttr(tupdesc, attidx)->atttypid;
		if (!pgcolumnar_batch_type_ok(coltype))
			{ ok = false; break; }
		if (key->sk_subtype != InvalidOid && key->sk_subtype != coltype)
			{ ok = false; break; }
		if (key->sk_strategy < BTLessStrategyNumber ||
			key->sk_strategy > BTGreaterStrategyNumber)
			{ ok = false; break; }
	}
	if (!ok)
		return false;

	if (keysOut != NULL)
	{
		*keysOut = keys;
		*nkeysOut = nkeys;
	}
	return true;
}

/*
 * pgcolumnar_batch_shape_eligible
 *		The ungrouped node's view of pgcolumnar_batch_gates_ok.
 */
static bool
pgcolumnar_batch_shape_eligible(PgColumnarAggScanState *state, TupleDesc tupdesc,
							  ScanKey *keysOut, int *nkeysOut)
{
	/*
	 * The gates BUILD scan keys to inspect them, and for a SAOP that detoasts
	 * and deconstructs the array constant (#717). Those allocations are the
	 * CALLER's to bound, and every caller now does: at execution the enclosing
	 * scratch context reclaims them when the scan returns (#727), and at Begin
	 * the caller wraps this in a temp context it deletes, because it discards
	 * the keys anyway.
	 */
	return pgcolumnar_batch_gates_ok(state->specs, state->naggs, state->quals,
								   state->scanrelid, state->projected, tupdesc,
								   keysOut, nkeysOut);
}

/*
 * Reset the accumulators to their initial state (for a clean fall-back).
 *
 * Every accumulating field of PgColumnarAggSpec belongs here, and i128sum is
 * one -- it was added by #786 and this function was not updated with it.
 *
 * NO TEST CAN FAIL ON THIS, and the reason is stronger than it first looked.
 *
 * The only caller is the batch fold's fall-back, taken when a row group predates
 * an ADD COLUMN. I first wrote that the stale accumulator becomes a wrong answer
 * once the int8 kinds are admitted to pgcolumnar_batch_agg_ok. That was
 * reasoning, and it is wrong. Instrumented by OffgridwithJD with the int8 kinds
 * admitted: the fall-back is reached, once, with count = 0 and nothing
 * accumulated, so the reset is a no-op and reverting this line changes no
 * answer.
 *
 * The order looks structural rather than lucky. The groups lacking the column
 * are exactly those written BEFORE the ADD COLUMN, so they carry the lowest
 * group numbers and are scanned first; the fold trips the missing column on the
 * first group it reads. Observing a stale accumulator needs a group folded
 * successfully and THEN a group that trips the fall-back, which ADD COLUMN
 * orders the wrong way round by construction.
 *
 * That is one route shown not to reach it, not a proof that none does, and it
 * is not claimed as one.
 *
 * Fixed regardless. A function whose job is to reset every accumulator, that
 * silently keeps one, is wrong whether or not today's callers can observe it,
 * and it is one line. pgcolumnar_batch_agg_ok carries a note pointing back here
 * so the next kind admitted gets checked against it.
 */
static void
pgcolumnar_agg_specs_reset(PgColumnarAggScanState *state)
{
	int			a;

	MemoryContextReset(state->resultContext);
	for (a = 0; a < state->naggs; a++)
	{
		PgColumnarAggSpec *spec = &state->specs[a];

		spec->count = 0;
		spec->sum = 0;
		spec->sawValue = false;
		spec->extreme = (Datum) 0;
		spec->fsum = 0;
		spec->fsxx = 0;
		spec->nsum = (Datum) 0;
		spec->nsumSet = false;
#ifdef HAVE_INT128
		spec->i128sum = 0;
#endif
	}
}

/*
 * pgcolumnar_native_batch_fold
 *		Fold the whole scan column-at-a-time. Returns false (having folded and
 *		reset nothing that the caller cannot redo) when the shape is not eligible
 *		or a group is missing a needed column, so the caller runs the row path.
 */
static bool
pgcolumnar_native_batch_fold(PgColumnarAggScanState *state, Relation rel,
						   TupleDesc tupdesc)
{
	EState	   *estate = state->css.ss.ps.state;
	ScanKey		keys = NULL;
	int			nkeys = 0;
	int			a;
	int			k;
	int			col;
	int			natts = tupdesc->natts;
	PgColumnarReadState *rs;
	const char **cvalidity = (const char **) palloc0(sizeof(char *) * natts);
	const char **cpacked = (const char **) palloc0(sizeof(char *) * natts);
	int16	   *cattlen = (int16 *) palloc0(sizeof(int16) * natts);
	uint64	   *cpresent = (uint64 *) palloc0(sizeof(uint64) * natts);
	bool	   *cneeded = (bool *) palloc0(sizeof(bool) * natts);
	bool	   *ciskey = (bool *) palloc0(sizeof(bool) * natts);
	Datum	   *cval = (Datum *) palloc0(sizeof(Datum) * natts);
	bool	   *cisnull = (bool *) palloc0(sizeof(bool) * natts);
	/*
	 * Compact needed-column lists, so the per-row loops step over the columns a
	 * query actually reads rather than all natts (a count(x) on a 100-column
	 * table needs one). keyCols and payloadCols split the needed set the way
	 * ciskey does; phase1Cols is what phase 1 gathers (keys always, payload too
	 * unless the group defers). Order is column order, and each column's state is
	 * independent, so iterating the list is equivalent to the masked full range.
	 */
	int		   *keyCols = (int *) palloc(sizeof(int) * Max(natts, 1));
	int		   *payloadCols = (int *) palloc(sizeof(int) * Max(natts, 1));
	int		   *phase1Cols = (int *) palloc(sizeof(int) * Max(natts, 1));
	int			nKeyCols = 0;
	int			nPayCols = 0;
	int			nPhase1 = 0;
	int			ci;
	int			npayload = 0;
	int64		candRows = 0;	/* rows that reached the key check */
	int64		survRows = 0;	/* rows that passed it */
	bool		deferOn = false;

	if (!pgcolumnar_batch_shape_eligible(state, tupdesc, &keys, &nkeys))
		return false;

	col = -1;
	while ((col = bms_next_member(state->projected, col)) >= 0)
		if (col >= 0 && col < natts)
			cneeded[col] = true;

	/*
	 * Payload deferral (#405 step 2): key columns are the ones the scan keys
	 * read; every other needed column is payload, whose fetch_att can wait
	 * until the key check passes. ciskey is the split; npayload > 0 is the
	 * only case with anything to defer.
	 */
	{
		int			kk;

		for (kk = 0; kk < nkeys; kk++)
		{
			int			ka = keys[kk].sk_attno - 1;

			if (ka >= 0 && ka < natts)
				ciskey[ka] = true;
		}
		for (col = 0; col < natts; col++)
			if (cneeded[col])
			{
				if (ciskey[col])
					keyCols[nKeyCols++] = col;
				else
					payloadCols[nPayCols++] = col;
			}
		npayload = nPayCols;
	}

	/*
	 * Push the scan keys so the reader prunes whole row groups its zone maps rule
	 * out (#349). The fold walks every surviving group in full and still rechecks
	 * the WHERE inline per value below, so pruning is a pure win: on clustered or
	 * range-partitioned data -- the workload this fold targets -- it skips groups
	 * no row can match instead of decoding and folding them. (Before this the fold
	 * read every group, 100x more than the row path on a selective clustered scan.)
	 *
	 * Per-vector skipping WITHIN a surviving group is a separate refinement the
	 * fold does not take: it would need to step the present index past a skipped
	 * vector. That is safe to leave out because pgcolumnar_native_load_group builds
	 * the packed present-value stream whole regardless of the skip vector, so
	 * walking all rows and advancing the present index over each is correct.
	 */
	rs = PgColumnarBeginRead(rel, estate->es_snapshot, NULL, state->projected,
						   nkeys, keys);

	/*
	 * Parallel arm (#289 phase 5/6): route group claiming through the shared
	 * atomic (gap 23) so each worker folds distinct row groups. A partial node
	 * with no counter would read every group in every worker and the Finalize
	 * would sum the duplicates -- a wrong answer, not a crash. The counter is
	 * always wired for our node (it is planned only under a Gather, whose
	 * InitializeDSM callback runs even leader-only), so a NULL here is a bug.
	 */
	if (state->parallelCounter != NULL)
		PgColumnarReadSetParallelCounter(rs, state->parallelCounter);
	else if (state->isPartial)
	{
		PgColumnarEndRead(rs);
		elog(ERROR, "parallel columnar aggregate ran without a shared group counter");
	}

	while (PgColumnarReadFoldNextGroup(rs))
	{
		uint64		nrows;
		const char *dmask;
		uint32		dlen;
		const bool *skipVec;
		bool		decodeSkipped;
		const uint32 *vecStart;
		int			vcount;
		int			curVec;
		uint64		r;

		PgColumnarReadFoldGroupInfo(rs, &nrows, &dmask, &dlen,
								  &skipVec, &decodeSkipped, &vecStart, &vcount);

		/*
		 * The adaptive gate (#405, amended step 4): defer this group's payload
		 * unless the survival observed so far exceeds half, in which case the
		 * two-phase gather would refetch nearly everything and eager is
		 * cheaper. The first group is optimistic. One comparison per group:
		 * the #289 no-per-group-precompute guard holds.
		 */
		deferOn = (npayload > 0 &&
				   (candRows == 0 || survRows * 2 <= candRows));
		state->foldGroupsTotal++;
		if (deferOn)
			state->foldGroupsDeferred++;

		/*
		 * Phase 1 gathers the key columns always, and the payload too unless this
		 * group defers it. deferOn is per group, so this list is rebuilt here.
		 */
		nPhase1 = 0;
		for (ci = 0; ci < nKeyCols; ci++)
			phase1Cols[nPhase1++] = keyCols[ci];
		if (!deferOn)
			for (ci = 0; ci < nPayCols; ci++)
				phase1Cols[nPhase1++] = payloadCols[ci];

		/*
		 * This loop now honours skipVec, and it has to (#512, #452 phase 1b-i).
		 * Decode no longer produces the vectors the zone maps ruled out, so the
		 * packed stream has HOLES in it. Reading one would re-check uninitialised
		 * memory: a wrong aggregate, silently, and only on data whose zone maps
		 * rule something out.
		 *
		 * Skipping them is not merely safe, it is exact. The reader built skipVec
		 * from the same scan keys this loop re-checks -- pgcolumnar_batch_shape_
		 * eligible requires every qual to be convertible to a scan key, and those
		 * keys are what PgColumnarBeginRead was given -- so a vector those zone
		 * maps rule out contains no row this fold would have counted.
		 *
		 * The present index must still advance across a skipped vector, exactly
		 * as it does across a deleted row: the stream is packed by presence, so
		 * a skipped row's slot is consumed whether or not its value is read.
		 * Getting that wrong misaligns every later vector rather than losing one.
		 *
		 * The tripwire that used to stand here (#523) was an ordering guard: it
		 * refused a group whose decode had skipped, so that decode could not be
		 * taught to skip without this loop being taught first. It has done its
		 * job -- it fired on exactly the change it was written for -- and the
		 * handling below replaces it. What remains of it is the fallback: if the
		 * reader reports a skip without the per-vector map this loop needs to
		 * honour it, refuse rather than guess.
		 */
		if (decodeSkipped && (skipVec == NULL || vecStart == NULL || vcount <= 0))
			elog(ERROR,
				 "pgcolumnar: the vectorized aggregate cannot fold a row group "
				 "whose decode skipped vectors without the per-vector map (#512)");

		for (col = 0; col < natts; col++)
		{
			const char *vbits;
			const char *pk;
			int16		al;
			const uint32 *vrl;

			cpresent[col] = 0;
			if (!cneeded[col])
				continue;
			if (!PgColumnarReadFoldColumn(rs, col, &vbits, &pk, &al, &vrl))
			{
				/*
				 * The column is absent from this group (a later ADD COLUMN). The
				 * batch path cannot supply its missing value, so reset and fall
				 * back to the row path for the whole scan, which handles it.
				 *
				 * A parallel partial node cannot take that fallback: this group and
				 * the ones before it were already claimed through the shared counter
				 * (gap 23), so the row path would re-open with the counter advanced
				 * past them and undercount. This is the one unsafe fallback; fail
				 * cleanly rather than return a wrong answer. Rare (an old row group
				 * predating an ADD COLUMN); turn the parallel GUC off for the table.
				 */
				PgColumnarEndRead(rs);
				pgcolumnar_agg_specs_reset(state);
				if (state->isPartial)
					elog(ERROR, "parallel columnar aggregate cannot fold a relation "
						 "with a column added after some row groups; "
						 "set pgcolumnar.enable_parallel_vector_agg = off");
				return false;
			}
			cvalidity[col] = vbits;
			cpacked[col] = pk;
			cattlen[col] = al;
		}

		curVec = 0;
		for (r = 0; r < nrows; r++)
		{
			bool		del;
			bool		pass = true;
			bool		vecSkipped = false;

			CHECK_FOR_INTERRUPTS();

			/*
			 * Which vector holds this row, and was it ruled out? vecStart is
			 * cumulative row spans with a [vcount] terminator, and r only ever
			 * increases, so this walk costs one step per vector boundary across
			 * the whole group rather than a search per row.
			 */
			if (skipVec != NULL && vecStart != NULL && vcount > 0)
			{
				while (curVec < vcount && r >= vecStart[curVec + 1])
					curVec++;
				vecSkipped = (curVec < vcount && skipVec[curVec]);
			}

			/*
			 * Phase 1: gather the scan-key columns (and, when this group is
			 * eager, everything). Deferred payload columns are not touched
			 * here at all -- not even their cursors -- so the failing-row
			 * path below can consume their slots without a fetch (#405).
			 */
			for (ci = 0; ci < nPhase1; ci++)
			{
				col = phase1Cols[ci];
				if ((cvalidity[col][r >> 3] >> (r & 7)) & 1)
				{
					/*
					 * A skipped vector's bytes were never decoded. Consume the
					 * slot, do not read it.
					 */
					if (!vecSkipped)
					{
						cval[col] = fetch_att(cpacked[col] + cpresent[col] * cattlen[col],
											  true, cattlen[col]);
						cisnull[col] = false;
						if (!ciskey[col])
							state->foldPayloadLoads++;
					}
					cpresent[col]++;
				}
				else
					cisnull[col] = true;
			}

			if (vecSkipped)
			{
				/* consume deferred payload slots; never read them (#405) */
				if (deferOn)
					for (ci = 0; ci < nPayCols; ci++)
					{
						col = payloadCols[ci];
						if ((cvalidity[col][r >> 3] >> (r & 7)) & 1)
							cpresent[col]++;
					}
				continue;
			}

			del = (dmask != NULL && (r >> 3) < dlen &&
				   (dmask[r >> 3] & (1 << (r & 7))) != 0);
			if (del)
			{
				if (deferOn)
					for (ci = 0; ci < nPayCols; ci++)
					{
						col = payloadCols[ci];
						if ((cvalidity[col][r >> 3] >> (r & 7)) & 1)
							cpresent[col]++;
					}
				continue;
			}
			candRows++;

			for (k = 0; k < nkeys; k++)
			{
				ScanKey		key = &keys[k];
				int			attidx = key->sk_attno - 1;

				if (cisnull[attidx] ||
					!pgcolumnar_batch_key_pass(TupleDescAttr(tupdesc, attidx)->atttypid,
											 cval[attidx], key->sk_strategy,
											 key->sk_argument))
				{
					pass = false;
					break;
				}
			}
			if (!pass)
			{
				if (deferOn)
					for (ci = 0; ci < nPayCols; ci++)
					{
						col = payloadCols[ci];
						if ((cvalidity[col][r >> 3] >> (r & 7)) & 1)
							cpresent[col]++;
					}
				continue;
			}
			survRows++;

			/*
			 * Phase 2 (#405): the row survived, so NOW materialize its
			 * deferred payload. This is the stable post-key counter site the
			 * plan required: on a deferred group the counter equals surviving
			 * rows times fetched payload values, never candidates.
			 */
			if (deferOn)
				for (ci = 0; ci < nPayCols; ci++)
				{
					col = payloadCols[ci];
					if ((cvalidity[col][r >> 3] >> (r & 7)) & 1)
					{
						cval[col] = fetch_att(cpacked[col] + cpresent[col] * cattlen[col],
											  true, cattlen[col]);
						cisnull[col] = false;
						state->foldPayloadLoads++;
						cpresent[col]++;
					}
					else
						cisnull[col] = true;
				}

			for (a = 0; a < state->naggs; a++)
			{
				PgColumnarAggSpec *spec = &state->specs[a];

				if (spec->attidx >= 0)
					pgcolumnar_apply_one(state->resultContext, spec,
									   cval[spec->attidx], cisnull[spec->attidx]);
				else
					pgcolumnar_apply_one(state->resultContext, spec, (Datum) 0, true);
			}
		}
	}

	PgColumnarReadStats(rs, &state->groupsRead, &state->groupsSkipped,
					  &state->groupsTotal);
	state->usablePreds = PgColumnarReadUsablePredicates(rs);
	state->vectorsSkipped = PgColumnarVectorsSkipped(rs);
	state->vectorsDecoded = PgColumnarVectorsDecoded(rs);
	state->vectorDecodes = PgColumnarVectorDecodes(rs);
	state->vectorsRuledOutByValue = PgColumnarVectorsRuledOutByValue(rs);
	state->zoneMapProbes = PgColumnarZoneMapProbes(rs);
	state->haveStats = true;
	state->batchFolded = true;
	PgColumnarEndRead(rs);
	return true;
}

/*
 * pgcolumnar_native_scan_agg
 *		Fold an ungrouped, unfiltered aggregate over a native table by scanning it
 *		one row at a time (PgColumnarReadNextRow applies the delete mask), for the
 *		case where the zone-map-only path cannot be used because the storage has
 *		deletes (D6b). No quals: the upper-path hook only adds the native agg path
 *		when there is no filter.
 *
 *		When restrictGroups is non-NULL the scan is confined to those row groups
 *		(issue #149), so only the groups that have deletes are read; the rest were
 *		already folded from their zone maps by the caller.
 */
static void
pgcolumnar_native_scan_agg(PgColumnarAggScanState *state,
						 const uint64 *restrictGroups, int nRestrictGroups)
{
	EState	   *estate = state->css.ss.ps.state;
	ExprContext *econtext = state->css.ss.ps.ps_ExprContext;
	Relation	rel = table_open(state->relid, AccessShareLock);
	TupleDesc	tupdesc = RelationGetDescr(rel);
	Bitmapset  *projected;
	PgColumnarReadState *rs;
	ScanKey		keys = NULL;
	int			nScanKeys = 0;
	Datum	   *values = (Datum *) palloc(sizeof(Datum) * tupdesc->natts);
	bool	   *nulls = (bool *) palloc(sizeof(bool) * tupdesc->natts);
	uint64		rowNumber;
	int			a;

	/*
	 * Scan-fold mode carries the projected set (aggregate plus WHERE columns) and
	 * a base slot for the per-row recheck; the dirty-groups metadata path passes
	 * neither and projects only the aggregate columns.
	 */
	if (state->scanFold)
		projected = state->projected;
	else
	{
		projected = NULL;
		for (a = 0; a < state->naggs; a++)
			if (state->specs[a].attidx >= 0)
				projected = bms_add_member(projected, state->specs[a].attidx);
		if (projected == NULL)
			projected = bms_make_singleton(0);	/* count(*) only: one column */
	}

	PgColumnarFlushWriteStateForRelation(state->relid);
	PgColumnarFlushDeleteVectorForRelation(rel);

	/*
	 * Batch fold when the shape allows it (#289): the whole scan folds
	 * column-at-a-time, which is the point of this path. Only the full scan uses
	 * it; the dirty-groups metadata tail (restrictGroups != NULL) stays on the
	 * row path.
	 */
	if (state->scanFold && restrictGroups == NULL &&
		pgcolumnar_native_batch_fold(state, rel, tupdesc))
	{
		table_close(rel, AccessShareLock);
		return;
	}

	/*
	 * The row path reached here because the shape is not batch-foldable (e.g. a
	 * NULL test or a non-btree filter): pgcolumnar_native_batch_fold returned false
	 * before claiming any group, so the shared counter is untouched and a parallel
	 * partial node can fold correctly here too -- each worker just claims distinct
	 * groups through the same atomic and applies the WHERE recheck per row. (The
	 * one unsafe fallback -- an absent column discovered mid-scan, after the
	 * counter has advanced -- errors inside the batch fold instead.)
	 */

	/* push the WHERE down for group and vector pruning; the recheck is exact */
	if (state->scanFold && state->quals != NIL)
	{
		/*
		 * Reached only when the fold declined, so any keys it built are already
		 * dead (it ended its read before returning false). These live until the
		 * enclosing scratch context is deleted at the end of the scan (#727),
		 * which is after the reader that uses them has finished.
		 */
		keys = PgColumnarBuildScanKeys(state->quals, state->scanrelid, tupdesc,
									 &nScanKeys);
	}

	rs = PgColumnarBeginRead(rel, estate->es_snapshot, NULL, projected,
						   nScanKeys, keys);
	if (state->parallelCounter != NULL)
		PgColumnarReadSetParallelCounter(rs, state->parallelCounter);
	if (restrictGroups != NULL)
		PgColumnarReadRestrictToGroups(rs, restrictGroups, nRestrictGroups);

	/* columns outside the projection stay null in the recheck slot */
	if (state->whereState != NULL)
		memset(state->baseSlot->tts_isnull, true, sizeof(bool) * tupdesc->natts);

	while (PgColumnarReadNextRow(rs, values, nulls, &rowNumber))
	{
		/*
		 * Recheck the whole WHERE per row: the scan keys only prune groups and
		 * vectors, so a surviving row may still fail the predicate.
		 */
		if (state->whereState != NULL)
		{
			int			x = -1;

			ResetExprContext(econtext);
			ExecClearTuple(state->baseSlot);
			while ((x = bms_next_member(state->projected, x)) >= 0)
			{
				state->baseSlot->tts_values[x] = values[x];
				state->baseSlot->tts_isnull[x] = nulls[x];
			}
			ExecStoreVirtualTuple(state->baseSlot);
			econtext->ecxt_scantuple = state->baseSlot;
			if (!ExecQual(state->whereState, econtext))
				continue;
		}

		for (a = 0; a < state->naggs; a++)
		{
			PgColumnarAggSpec *spec = &state->specs[a];

			if (spec->attidx >= 0)
				pgcolumnar_apply_one(state->resultContext, spec,
								   values[spec->attidx], nulls[spec->attidx]);
			else
				pgcolumnar_apply_one(state->resultContext, spec, (Datum) 0, true);
		}
	}

	if (state->scanFold)
	{
		PgColumnarReadStats(rs, &state->groupsRead, &state->groupsSkipped,
						  &state->groupsTotal);
		state->usablePreds = PgColumnarReadUsablePredicates(rs);
	state->vectorsSkipped = PgColumnarVectorsSkipped(rs);
	state->vectorsDecoded = PgColumnarVectorsDecoded(rs);
	state->vectorDecodes = PgColumnarVectorDecodes(rs);
	state->vectorsRuledOutByValue = PgColumnarVectorsRuledOutByValue(rs);
	state->zoneMapProbes = PgColumnarZoneMapProbes(rs);
		state->haveStats = true;
	}

	PgColumnarEndRead(rs);
	table_close(rel, AccessShareLock);
}

static TupleTableSlot *
PgColumnarExecAggScan(CustomScanState *node)
{
	PgColumnarAggScanState *state = (PgColumnarAggScanState *) node;
	TupleTableSlot *scanSlot = node->ss.ss_ScanTupleSlot;
	ExprContext *econtext = node->ss.ps.ps_ExprContext;
	TupleTableSlot *result;
	int			a;
	Relation	frel;
	uint64	   *dirtyGroups;
	int			nDirtyGroups;

	if (state->done)
		return NULL;
	state->done = true;

	/*
	 * A native table answers from zone maps (native spec 7.1): the upper-path
	 * hook added this path only when every aggregate is zone-map answerable and
	 * there is no filter. A zone map covers deleted rows too, so a row group with
	 * deletes cannot be folded from it; those groups are scanned instead, and only
	 * those (issue #149).
	 *
	 * The delete vector must be flushed before any of this. A delete made earlier
	 * in this transaction can still be sitting in the per-relation buffer, and a
	 * group whose deletes are unflushed reads as clean -- which would fold it from
	 * a zone map that counts the rows this transaction has already removed.
	 */
	frel = table_open(state->relid, AccessShareLock);
	PgColumnarFlushWriteStateForRelation(state->relid);
	PgColumnarFlushDeleteVectorForRelation(frel);
	table_close(frel, AccessShareLock);

	/*
	 * Run the scan in a scratch context deleted when it returns (#727).
	 *
	 * This node re-executes on every rescan of a LATERAL or parameterized
	 * aggregate sub-scan, and everything it allocates in the caller's context --
	 * the per-row value and null arrays, the projected set on the metadata path,
	 * whatever the flushes and the reader leave behind -- lived until
	 * ExecutorEnd. Measured at ~178 bytes a rescan attributable to this node,
	 * above what the columnar scan under it already costs.
	 *
	 * Wrapped at the CALL SITE rather than inside the function, so that every
	 * return path is covered by construction: the batch fold's early return, the
	 * ADD COLUMN fall-back and the normal end would otherwise each need their
	 * own restore, and missing one leaks the context instead of the memory.
	 *
	 * Nothing allocated inside needs to outlive the call. The accumulators are
	 * passed state->resultContext explicitly and land there; the scan keys have
	 * their own context (#717); the statistics are scalars in the node. The
	 * projected set the metadata path builds IS local and dies here, which is
	 * what is wanted; the scan-fold path's set was built at Begin in another
	 * context and is not touched by this delete.
	 */
	{
		MemoryContext scanCxt = AllocSetContextCreate(CurrentMemoryContext,
													  "columnar vec agg scan",
													  ALLOCSET_DEFAULT_SIZES);
		MemoryContext oldCxt = MemoryContextSwitchTo(scanCxt);

		if (state->scanFold)
		{
			/*
			 * A filter, or a sum/avg no zone map answers (#289): scan every row
			 * once and fold it. pgcolumnar_native_scan_agg builds the scan keys,
			 * rechecks the WHERE, and captures the EXPLAIN stats.
			 */
			pgcolumnar_native_scan_agg(state, NULL, 0);
		}
		else
		{
			/*
			 * dirtyGroups is deliberately allocated OUTSIDE this context, below
			 * in the caller's, because it is read after the scratch is gone.
			 */
			MemoryContextSwitchTo(oldCxt);
			dirtyGroups = pgcolumnar_fill_native_metadata_agg(state, &nDirtyGroups);
			MemoryContextSwitchTo(scanCxt);
			if (nDirtyGroups > 0)
				pgcolumnar_native_scan_agg(state, dirtyGroups, nDirtyGroups);
			state->haveStats = false;
		}

		MemoryContextSwitchTo(oldCxt);
		MemoryContextDelete(scanCxt);
	}

	/*
	 * Build the single result row. A parallel partial node (#289 phase 5/6) emits
	 * each aggregate's transition state for the core Finalize above it to combine;
	 * every other node emits the finalized value.
	 */
	ExecClearTuple(scanSlot);
	for (a = 0; a < state->naggs; a++)
		scanSlot->tts_values[a] = state->isPartial
			? pgcolumnar_agg_emit_partial(&state->specs[a], &scanSlot->tts_isnull[a])
			: pgcolumnar_agg_finalize(&state->specs[a], &scanSlot->tts_isnull[a]);
	ExecStoreVirtualTuple(scanSlot);

	/*
	 * Project to the result tuple when the executor built a projection; when the
	 * output matches the scan tuple exactly it left ps_ProjInfo NULL and the scan
	 * slot is the result.
	 */
	if (node->ss.ps.ps_ProjInfo != NULL)
	{
		econtext->ecxt_scantuple = scanSlot;
		result = ExecProject(node->ss.ps.ps_ProjInfo);
	}
	else
		result = scanSlot;

	return result;
}

static void
PgColumnarEndAggScan(CustomScanState *node)
{
	PgColumnarAggScanState *state = (PgColumnarAggScanState *) node;

	if (state->baseSlot != NULL)
		ExecDropSingleTupleTableSlot(state->baseSlot);
	state->baseSlot = NULL;
	/* the reader is ended inside PgColumnarExecAggScan; the memory contexts are
	 * children of es_query_cxt and freed with it */
}

static void
PgColumnarReScanAggScan(CustomScanState *node)
{
	PgColumnarAggScanState *state = (PgColumnarAggScanState *) node;

	state->done = false;
	state->haveStats = false;
	state->batchFolded = false;
	state->foldPayloadLoads = 0;
	state->foldGroupsDeferred = 0;
	state->foldGroupsTotal = 0;

	/*
	 * One reset, not two. This used to open-code the loop that
	 * pgcolumnar_agg_specs_reset already contains, and the two copies were
	 * identical except that this one never cleared spec->i128sum. The int8 kinds
	 * accumulate there, so a rescan carried the previous execution's running
	 * total into the next one and sum(bigint) came back cumulative: measured
	 * 800022400060000 then 1600044799119997 then 2400067196179988 over four
	 * LATERAL iterations whose true answers were all near the first.
	 *
	 * The comment on pgcolumnar_batch_agg_ok already said to check that every
	 * accumulator a kind uses is cleared, and named i128sum. It points at
	 * pgcolumnar_agg_specs_reset, which was right. Calling that function from
	 * here is what makes the instruction reach both paths, rather than leaving a
	 * second copy for the next accumulator to be forgotten in.
	 *
	 * The float and numeric clears are load-bearing rather than defensive:
	 * resultContext has just been reset, so nsum's storage is gone and leaving
	 * nsumSet true would make the next scan add to a dangling pointer.
	 */
	pgcolumnar_agg_specs_reset(state);
}

static void
PgColumnarExplainAggScan(CustomScanState *node, List *ancestors, ExplainState *es)
{
	PgColumnarAggScanState *state = (PgColumnarAggScanState *) node;

	ExplainPropertyInteger("Columnar Vectorized Aggregates", NULL,
						   state->naggs, es);
	PgColumnarExplainPushedDown(state->nscankeys, es);
	PgColumnarExplainVectorPredicates(state->npreds, es);
	if (state->scanFold)
	{
		/*
		 * Plain EXPLAIN has no execution to report, so it shows the shape
		 * prediction. Once the node has run, report what actually happened:
		 * a predicted-eligible fold can still fall back to the row path (a
		 * column added after some row groups), and printing the prediction
		 * under ANALYZE reported "yes" for a row-path run (#602).
		 *
		 * A node ANALYZE marks "(never executed)" has no stats either, so
		 * the prediction prints there too; beside that marker it means
		 * "would have been attempted", not "ran".
		 */
		ExplainPropertyText("Columnar Batch Fold",
							(state->haveStats ? state->batchFolded
											  : state->batchEligible)
							? "yes" : "no", es);
	}

	if (state->haveStats)
	{
		/*
		 * npreds is this node's built-key count from
		 * PgColumnarCountConvertibleQuals, the same quantity the scalar node
		 * reports, so it has the same gap and needs the same second number.
		 * PgColumnarExplainGroupStats is why that is now automatic.
		 */
		PgColumnarGroupStats gs;

		if (state->batchFolded)
		{
			ExplainPropertyInteger("Columnar Fold Payload Loads", NULL,
								   (int64) state->foldPayloadLoads, es);
			ExplainPropertyText("Columnar Fold Deferred Groups",
								psprintf("%d of %d", state->foldGroupsDeferred,
										 state->foldGroupsTotal), es);
		}

		gs.usableSkipPredicates = state->usablePreds;
		gs.groupsTotal = state->groupsTotal;
		gs.groupsRead = state->groupsRead;
		gs.groupsRemoved = state->groupsSkipped;
		gs.vectorsSkipped = state->vectorsSkipped;
		gs.vectorsDecoded = state->vectorsDecoded;
		gs.vectorDecodes = state->vectorDecodes;
		gs.vectorsRuledOutByValue = state->vectorsRuledOutByValue;
		gs.zoneMapProbes = state->zoneMapProbes;
		PgColumnarExplainGroupStats(&gs, es);
	}
}

static const CustomExecMethods pgcolumnar_agg_exec_methods = {
	.CustomName = "PgColumnarScan",
	.BeginCustomScan = PgColumnarBeginAggScan,
	.ExecCustomScan = PgColumnarExecAggScan,
	.EndCustomScan = PgColumnarEndAggScan,
	.ReScanCustomScan = PgColumnarReScanAggScan,
	.ExplainCustomScan = PgColumnarExplainAggScan,
};

/* -------------------------------------------------------------------------
 * parallel partial aggregate (#289 phase 5/6): DSM callbacks
 *
 * A shared pg_atomic_uint32 hands out row-group indices so each worker folds
 * distinct groups -- the same mechanism the base parallel scan uses (gap 23).
 * The custom scan framework sizes and allocates the DSM chunk from EstimateDSM
 * and passes its address as `coordinate` to the DSM/worker init callbacks. Our
 * agg node opens its reader lazily during Exec, strictly after both DSM-init and
 * Worker-init, so the callbacks only need to record the counter on the state;
 * PgColumnarBeginRead wiring happens at fold time.
 * ------------------------------------------------------------------------- */

static Size
PgColumnarEstimateDSMAggScan(CustomScanState *node, ParallelContext *pcxt)
{
	return sizeof(pg_atomic_uint32);
}

static void
PgColumnarInitializeDSMAggScan(CustomScanState *node, ParallelContext *pcxt,
							 void *coordinate)
{
	PgColumnarAggScanState *state = (PgColumnarAggScanState *) node;
	pg_atomic_uint32 *counter = (pg_atomic_uint32 *) coordinate;

	pg_atomic_init_u32(counter, 0);
	state->parallelCounter = counter;

	/*
	 * Flush this backend's pending writes and deletes here, in the leader, before
	 * any worker launches (H2). A worker is a separate backend and cannot see the
	 * leader's unflushed in-transaction buffers; flushing per-worker would miss
	 * rows the leader deleted earlier in this transaction. The Exec-time flush in
	 * every backend then only ever flushes its own (empty) buffers.
	 */
	PgColumnarFlushWriteStateForRelation(state->relid);
	{
		Relation	frel = table_open(state->relid, AccessShareLock);

		PgColumnarFlushDeleteVectorForRelation(frel);
		table_close(frel, AccessShareLock);
	}
}

static void
PgColumnarReInitializeDSMAggScan(CustomScanState *node, ParallelContext *pcxt,
							   void *coordinate)
{
	pg_atomic_uint32 *counter = (pg_atomic_uint32 *) coordinate;

	/* a rescan restarts group claiming from zero for every participant */
	pg_atomic_write_u32(counter, 0);
}

static void
PgColumnarInitializeWorkerAggScan(CustomScanState *node, shm_toc *toc,
								void *coordinate)
{
	PgColumnarAggScanState *state = (PgColumnarAggScanState *) node;
	pg_atomic_uint32 *counter = (pg_atomic_uint32 *) coordinate;

	state->parallelCounter = counter;
}

static const CustomExecMethods pgcolumnar_agg_parallel_exec_methods = {
	.CustomName = "PgColumnarScan",
	.BeginCustomScan = PgColumnarBeginAggScan,
	.ExecCustomScan = PgColumnarExecAggScan,
	.EndCustomScan = PgColumnarEndAggScan,
	.ReScanCustomScan = PgColumnarReScanAggScan,
	.ExplainCustomScan = PgColumnarExplainAggScan,
	.EstimateDSMCustomScan = PgColumnarEstimateDSMAggScan,
	.InitializeDSMCustomScan = PgColumnarInitializeDSMAggScan,
	.ReInitializeDSMCustomScan = PgColumnarReInitializeDSMAggScan,
	.InitializeWorkerCustomScan = PgColumnarInitializeWorkerAggScan,
};

/* -------------------------------------------------------------------------
 * grouped vectorized aggregate (#289): execution
 * ------------------------------------------------------------------------- */

Node *
PgColumnarCreateGroupAggScanState(CustomScan *cscan)
{
	PgColumnarGroupAggScanState *state =
		(PgColumnarGroupAggScanState *) palloc0(sizeof(PgColumnarGroupAggScanState));
	List	   *groupKeys;
	List	   *outMapList;
	ListCell   *lc;
	int			i;
	int			naggs = 0;

	state->css.ss.ps.type = T_CustomScanState;

	/*
	 * A parallel partial grouped node (#349) is planned with
	 * AGGSPLIT_INITIAL_SERIAL aggrefs in its output tuple: it emits one
	 * (group keys, transition states) tuple per group per worker for a core
	 * Finalize to combine by key. Detect it from the output tuple exactly as the
	 * ungrouped node does, and switch to the exec methods table carrying the
	 * DSM/worker callbacks so every worker shares the group-claim counter.
	 */
	state->isPartial = false;
	foreach(lc, cscan->custom_scan_tlist)
	{
		TargetEntry *tle = (TargetEntry *) lfirst(lc);

		if (IsA(tle->expr, Aggref) &&
			((Aggref *) tle->expr)->aggsplit != AGGSPLIT_SIMPLE)
			state->isPartial = true;
	}
	state->css.methods = state->isPartial
		? &pgcolumnar_groupagg_parallel_exec_methods
		: &pgcolumnar_groupagg_exec_methods;

	/* custom_private: rti, quals, relid, group-key exprs, output map (length 5) */
	state->scanrelid = (Index) intVal(linitial(cscan->custom_private));
	state->quals = (List *) lsecond(cscan->custom_private);
	state->relid =
		DatumGetObjectId(((Const *) lthird(cscan->custom_private))->constvalue);
	groupKeys = (List *) lfourth(cscan->custom_private);
	outMapList = (List *) list_nth(cscan->custom_private, 4);

	state->nkeys = list_length(groupKeys);
	state->keys = (PgColumnarGroupKey *)
		palloc0(sizeof(PgColumnarGroupKey) * Max(state->nkeys, 1));
	i = 0;
	foreach(lc, groupKeys)
		state->keys[i++].expr = (Expr *) lfirst(lc);

	/* rebuild the aggregate template from the output tuple's aggregates */
	foreach(lc, cscan->custom_scan_tlist)
		if (IsA(((TargetEntry *) lfirst(lc))->expr, Aggref))
			naggs++;
	state->naggs = naggs;
	state->aggTemplate = (PgColumnarAggSpec *)
		palloc0(sizeof(PgColumnarAggSpec) * Max(naggs, 1));
	i = 0;
	foreach(lc, cscan->custom_scan_tlist)
	{
		TargetEntry *tle = (TargetEntry *) lfirst(lc);

		if (IsA(tle->expr, Aggref))
		{
			/* allowPartial accepts the parallel arm's INITIAL_SERIAL aggrefs */
			(void) pgcolumnar_classify_aggref((Aggref *) tle->expr, -1, true, true,
											&state->aggTemplate[i]);
			i++;
		}
	}

	state->nout = list_length(outMapList);
	state->outMap = (int *) palloc(sizeof(int) * Max(state->nout, 1));
	i = 0;
	foreach(lc, outMapList)
		state->outMap[i++] = intVal(lfirst(lc));

	state->maxGroups = pgcolumnar_groupagg_max_groups;
	state->capacity = 0;
	state->hashResizes = 0;
	state->hashRehashed = 0;
	state->peakCapacity = 0;
	state->nGroups = 0;
	state->entries = NULL;

	return (Node *) state;
}

static void
PgColumnarBeginGroupAggScan(CustomScanState *node, EState *estate, int eflags)
{
	PgColumnarGroupAggScanState *state = (PgColumnarGroupAggScanState *) node;
	Relation	rel;
	TupleDesc	basedesc;
	Bitmapset  *proj = NULL;
	Bitmapset  *projected = NULL;
	List	   *keyExprList = NIL;
	bool		allConvertible;
	int			k;
	int			a;
	int			x;

	state->specContext = AllocSetContextCreate(estate->es_query_cxt,
											   "columnar groupagg specs",
											   ALLOCSET_SMALL_SIZES);
	state->keyContext = AllocSetContextCreate(estate->es_query_cxt,
											  "columnar groupagg keys",
											  ALLOCSET_SMALL_SIZES);
	state->hashContext = AllocSetContextCreate(estate->es_query_cxt,
											   "columnar groupagg table",
											   ALLOCSET_DEFAULT_SIZES);
	state->started = false;
	state->emitPos = 0;
	state->nGroups = 0;
	state->capacity = 0;
	state->hashResizes = 0;
	state->hashRehashed = 0;
	state->peakCapacity = 0;
	state->sizeHint = pgcolumnar_groupagg_size_hint(node, state->maxGroups);
	state->entries = NULL;
	state->haveStats = false;

	rel = table_open(state->relid, AccessShareLock);
	basedesc = RelationGetDescr(rel);

	/*
	 * Count pushable filters for EXPLAIN before the EXPLAIN-only early return, so
	 * a plain EXPLAIN reports the real pushed-down filter count instead of 0.
	 */
	PgColumnarCountConvertibleQuals(state->quals, state->scanrelid, basedesc,
								  &state->npreds, &allConvertible);
	state->nscankeys = PgColumnarCountScanKeys(state->quals, state->scanrelid,
											 basedesc);

	/*
	 * Everything the batch fold's eligibility depends on is a property of the
	 * PLAN, not of executor state, so it is settled here -- ahead of the
	 * EXPLAIN-only return, and ahead of any ExecInitExpr. A plain EXPLAIN must
	 * be able to report what the fold will attempt, and the ungrouped node
	 * learned the hard way (#423) that deciding eligibility against a projected
	 * set that had not been built yet reports "yes" for a shape that falls back
	 * at execution.
	 *
	 * The per-key executor machinery (ExecInitExpr, the hash and equality
	 * FmgrInfos) stays below the return, where it belongs: an EXPLAIN-only node
	 * must not initialise executor state.
	 */
	for (k = 0; k < state->nkeys; k++)
	{
		PgColumnarGroupKey *key = &state->keys[k];
		Oid			type = exprType((Node *) key->expr);

		key->type = type;
		key->collation = exprCollation((Node *) key->expr);
		get_typlenbyval(type, &key->typlen, &key->byval);

		/*
		 * A key the fold can read straight out of a gathered column: a plain Var
		 * of the relation this node scans. Anything else -- an expression, a
		 * cast, a Var from another level -- keeps attidx at -1 and the fold
		 * declines the shape rather than growing an expression evaluator.
		 */
		key->attidx = -1;
		if (IsA(key->expr, Var))
		{
			Var		   *v = (Var *) key->expr;

			if (v->varno == state->scanrelid && v->varlevelsup == 0 &&
				v->varattno >= 1 && v->varattno <= basedesc->natts)
				key->attidx = v->varattno - 1;
		}
		key->simple = key->byval && pgcolumnar_groupagg_simple_key(type);

		keyExprList = lappend(keyExprList, key->expr);
	}

	/* project the columns the keys, WHERE and aggregates reference */
	pull_varattnos((Node *) keyExprList, state->scanrelid, &proj);
	pull_varattnos((Node *) state->quals, state->scanrelid, &proj);
	x = -1;
	while ((x = bms_next_member(proj, x)) >= 0)
	{
		AttrNumber	attno = x + FirstLowInvalidHeapAttributeNumber;

		if (attno > 0)
			projected = bms_add_member(projected, attno - 1);
	}
	for (a = 0; a < state->naggs; a++)
		if (state->aggTemplate[a].attidx >= 0)
			projected = bms_add_member(projected, state->aggTemplate[a].attidx);
	if (projected == NULL)
		projected = bms_make_singleton(0);	/* count(*) with no keys touched */
	state->projected = projected;

	{
		/* the keys are discarded; bound them, as the ungrouped Begin does */
		MemoryContext tmp = AllocSetContextCreate(CurrentMemoryContext,
												  "columnar groupagg eligibility",
												  ALLOCSET_SMALL_SIZES);
		MemoryContext oldcxt = MemoryContextSwitchTo(tmp);

		state->batchEligible =
			pgcolumnar_groupagg_batch_eligible(state, basedesc, NULL, NULL);
		MemoryContextSwitchTo(oldcxt);
		MemoryContextDelete(tmp);
	}
	state->batchFolded = false;

	if (eflags & EXEC_FLAG_EXPLAIN_ONLY)
	{
		table_close(rel, AccessShareLock);
		return;
	}

	/* guard the native format before decoding any value (#240) */
	PgColumnarCheckNativeFormatVersion(PgColumnarStorageId(rel),
									 RelationGetRelationName(rel));

	/* a virtual slot holding each read row for key and qual evaluation */
	state->baseSlot = MakeSingleTupleTableSlot(CreateTupleDescCopy(basedesc),
											   &TTSOpsVirtual);

	/*
	 * The group keys' executor machinery. Their type, collation, width and
	 * fold-readability were settled above, before the EXPLAIN-only return; what
	 * is left is the state an executing node needs.
	 */
	for (k = 0; k < state->nkeys; k++)
	{
		PgColumnarGroupKey *key = &state->keys[k];
		TypeCacheEntry *tce = lookup_type_cache(key->type,
												TYPECACHE_HASH_PROC_FINFO |
												TYPECACHE_EQ_OPR_FINFO);

		key->exprState = ExecInitExpr(key->expr, &node->ss.ps);
		fmgr_info_copy(&key->hashFn, &tce->hash_proc_finfo, estate->es_query_cxt);
		fmgr_info_copy(&key->eqFn, &tce->eq_opr_finfo, estate->es_query_cxt);
	}

	/* residual WHERE recheck (the scan keys only prune groups) */
	state->whereState = (state->quals != NIL)
		? ExecInitQual(state->quals, &node->ss.ps)
		: NULL;

	/* finish min/max comparison setup on the per-agg template */
	for (a = 0; a < state->naggs; a++)
	{
		PgColumnarAggSpec *spec = &state->aggTemplate[a];

		if (spec->kind == COLUMNAR_AGG_MIN || spec->kind == COLUMNAR_AGG_MAX)
		{
			Form_pg_attribute att = TupleDescAttr(basedesc, spec->attidx);
			TypeCacheEntry *tce = lookup_type_cache(att->atttypid,
													TYPECACHE_CMP_PROC_FINFO);

			fmgr_info_copy(&spec->cmpFn, &tce->cmp_proc_finfo,
						   estate->es_query_cxt);
			spec->collation = att->attcollation;
			spec->byval = att->attbyval;
			spec->typlen = att->attlen;
		}
	}

	table_close(rel, AccessShareLock);
}

/*
 * pgcolumnar_groupagg_simple_key
 *		Whether a group-key type's equality operator is exactly bitwise equality
 *		of the Datum, and every value of it has one representation. For such a
 *		type the hash probe can hash the Datum's bits and compare Datums with ==
 *		instead of paying an fmgr call per key per row (#708).
 *
 *		The HASH is private to this hash table and never leaves it, so any hash
 *		consistent with the equality relation is correct. The EQUALITY is the
 *		part that has to match how core groups, and that is what this list is
 *		asserting -- one entry at a time.
 *
 *		STATE THE INVARIANT PRECISELY, because the next person to extend this
 *		list will read this sentence instead of re-deriving it. The requirement
 *		is NOT "the type's equality is an integer compare of the stored width".
 *		It is that two values this type calls EQUAL must produce identical Datum
 *		BITS *after fetch_att* -- which is a property of the GATHER as much as of
 *		the equality operator.
 *
 *		oid is the entry that shows the difference. fetch_att SIGN-EXTENDS a
 *		len-4 column, so an oid above 2^31 arrives with the high half set, which
 *		the narrower rule does not describe at all. It is still correct here,
 *		and only because the sign extension is CONSISTENT: the same oid always
 *		yields the same bits, so equal values stay equal and unequal values stay
 *		unequal. A type for which that did not hold would satisfy the narrower
 *		rule and still group wrongly. (Found in review, by probing oid values
 *		above 2^31 against a heap oracle rather than by reading the list.)
 *
 *		float4 and float8 are deliberately ABSENT even though they are passed by
 *		value and the fold reads them happily: -0.0 and 0.0 are ONE group and
 *		have different bit patterns, so a bitwise probe would split them into
 *		two. They keep the type's own hash and equality functions and still
 *		fold, just without this shortcut. test/native_groupagg_batch.sh's
 *		"float8 zero" arms go red if float8 is ever added here, which is the
 *		removal proof for the exclusion.
 *
 *		By-reference types cannot reach this at all: the fold's gather passes
 *		attbyval = true to fetch_att (#423) and pgcolumnar_batch_gates_ok
 *		already requires it of every projected column.
 */
static bool
pgcolumnar_groupagg_simple_key(Oid typ)
{
	switch (typ)
	{
		case INT2OID:
		case INT4OID:
		case INT8OID:
		case OIDOID:
		case DATEOID:
		case TIMEOID:
		case TIMESTAMPOID:
		case TIMESTAMPTZOID:
			return true;
		default:
			return false;
	}
}

/*
 * A cheap, well-mixed hash of a Datum's bits, for the key types
 * pgcolumnar_groupagg_simple_key admits. It does not have to agree with the
 * type's own hash function -- only with itself -- because the table it indexes
 * is private to this node.
 */
static inline uint32
pgcolumnar_groupagg_hash_datum(Datum d)
{
	uint64		x = (uint64) d;

	x ^= x >> 33;
	x *= UINT64CONST(0xff51afd7ed558ccd);
	x ^= x >> 33;
	x *= UINT64CONST(0xc4ceb9fe1a85ec53);
	x ^= x >> 33;
	return (uint32) x;
}

/*
 * pgcolumnar_groupagg_keys_equal
 *		Whether a probing row's keys match a stored group's, by SQL grouping
 *		semantics: two nulls are equal, and non-nulls compare with the key type's
 *		equality operator (with collation) -- exactly how core groups.
 */
static bool
pgcolumnar_groupagg_keys_equal(PgColumnarGroupAggScanState *state,
							 PgColumnarGroupEntry *e,
							 Datum *keyvals, bool *keynulls)
{
	int			k;

	for (k = 0; k < state->nkeys; k++)
	{
		if (e->keyNulls[k] != keynulls[k])
			return false;
		if (keynulls[k])
			continue;
		if (state->keys[k].simple)
		{
			/* bitwise equality IS this type's equality (#708) */
			if (e->keys[k] != keyvals[k])
				return false;
			continue;
		}
		if (!DatumGetBool(FunctionCall2Coll(&state->keys[k].eqFn,
											state->keys[k].collation,
											e->keys[k], keyvals[k])))
			return false;
	}
	return true;
}

/*
 * pgcolumnar_groupagg_size_hint
 *		The capacity to allocate up front, from the group estimate the planner
 *		already made (#403 item 6). Zero means "no useful estimate", and the
 *		table then starts where it always did.
 *
 *		The table doubles at 70% load from a standing start, so a query with
 *		200,000 groups walks 1024 to 524288 and rehashes every live entry ten
 *		times. The estimate that would have avoided that is already computed:
 *		estimate_num_groups at plan time, bounded since #369 by what the zone
 *		maps say the column can hold, and carried on the plan as plan_rows.
 *
 *		Sizing from an estimate trades bounded rehashing for unbounded memory
 *		unless the estimate is bounded, and an over-estimate is the common case
 *		rather than the exotic one. Two bounds apply here:
 *
 *		- pgcolumnar.groupagg_max_groups, because a run that exceeds it errors,
 *		  so allocating for more than it can hold is allocating for a failure;
 *		- the existing 1<<30 entry ceiling.
 *
 *		The bound that does the real work is not here. It is
 *		COLUMNAR_GROUPAGG_JUMP, applied at each grow in the caller, because it is
 *		expressed in what the data has PROVEN it needs rather than in a budget
 *		that has nothing to do with this query. That distinction is the whole
 *		correction from the review of #810: a hint bounded only by work_mem still
 *		allocated 131072 entries for 47 real groups.
 *
 *		Under-estimating costs nothing new: the table grows exactly as it does
 *		today from wherever it started.
 */
static int
pgcolumnar_groupagg_size_hint(CustomScanState *node, int maxGroups)
{
	double		est;
	double		want;
	uint64		cap;
	uint64		limit;

	if (node->ss.ps.plan == NULL)
		return 0;

	est = node->ss.ps.plan->plan_rows;
	if (est <= 0.0 || isnan(est))
		return 0;

	/* the load factor the table maintains, so the estimate fits without a grow */
	want = est * 10.0 / 7.0;
	if (want < 1024.0)
		return 0;				/* the default start already covers it */

	/* round up to a power of two, which the index masking requires */
	cap = 1024;
	while ((double) cap < want && cap < (1u << 30))
		cap *= 2;

	limit = (uint64) (1u << 30);

	/*
	 * No work_mem bound here. It was in the first version of this change, to
	 * bound what a wrong estimate could allocate. COLUMNAR_GROUPAGG_JUMP now
	 * bounds that structurally, in terms of the live count this table has proven
	 * it needs, which is the quantity that actually matters; and the doubling
	 * ladder has never respected work_mem either, before or after this change,
	 * so a work_mem bound on the hint alone could not make the claim it looked
	 * like it was making. A second bound that no check can distinguish from the
	 * first is decoration.
	 */

	/* what the execution-time group cap can ever hold */
	if (maxGroups > 0)
	{
		uint64		byCap = (uint64) ((double) maxGroups * 10.0 / 7.0);

		if (byCap < limit)
			limit = byCap;
	}

	while (cap > limit && cap > 1024)
		cap /= 2;

	return (cap <= 1024) ? 0 : (int) cap;
}

/*
 * pgcolumnar_groupagg_grow
 *		Double the open-addressing table and reinsert live entries. Entry structs
 *		(and the key/spec pointers they carry) move by value; the pointed-at key
 *		Datums and accumulators stay put in their own contexts.
 */
static void
pgcolumnar_groupagg_grow(PgColumnarGroupAggScanState *state)
{
	int			oldCap = state->capacity;
	int			newCap;
	PgColumnarGroupEntry *newEntries;
	MemoryContext old;
	int			i;

	/*
	 * The table still STARTS at 1024 and is only sized up once the data has
	 * proven it needs to grow (#403 item 6, corrected on review of #810).
	 *
	 * Allocating the estimate at Begin was wrong in the ordinary case rather
	 * than an exotic one. estimate_num_groups cannot see through a function, so
	 * GROUP BY date_trunc('day', ts) over a high-cardinality timestamp reaches
	 * this node with rows=4000000 against 47 real groups, and the up-front
	 * allocation was 131072 entries where growing from nothing would have used
	 * 1024 and rehashed nothing. That is 128x the memory to save zero work, and
	 * columnar_vector.c:1817 already documented the estimator's blindness here.
	 *
	 * A query whose real group count fits in 1024 now never reaches this
	 * function at all, so it cannot be charged for a wrong estimate.
	 *
	 * When the table does grow, it jumps toward the estimate rather than
	 * doubling, but never further than COLUMNAR_GROUPAGG_JUMP times what the
	 * data has already proven it needs. A bounded jump keeps the saving, since
	 * the rehashing cost is dominated by the last doublings, while bounding what
	 * a wrong estimate can allocate to a multiple of the live count rather than
	 * to work_mem.
	 */
	if (oldCap <= 0)
		newCap = 1024;
	else
	{
		newCap = oldCap * 2;

		if (state->sizeHint > newCap)
		{
			int			ceiling = (oldCap > (1 << 30) / COLUMNAR_GROUPAGG_JUMP)
				? (1 << 30) : oldCap * COLUMNAR_GROUPAGG_JUMP;

			newCap = (state->sizeHint < ceiling) ? state->sizeHint : ceiling;
		}
	}

	if (newCap > (1 << 30))
		newCap = 1 << 30;
	if (newCap <= oldCap)
		return;					/* already at the ceiling; let probing lengthen */

	/* counted here, after the ceiling refusal, so it counts rehashes done */
	state->hashResizes++;
	if (newCap > state->peakCapacity)
		state->peakCapacity = newCap;

	/*
	 * The table can grow past what a plain palloc allows (MaxAllocSize is 1 GB;
	 * this array reaches it well before the 1<<30 entry ceiling), so allocate it
	 * as a huge, zeroed chunk. Memory is still bounded by the actual group count
	 * via pgcolumnar.groupagg_max_groups.
	 */
	old = MemoryContextSwitchTo(state->hashContext);
	newEntries = (PgColumnarGroupEntry *)
		MemoryContextAllocExtended(state->hashContext,
								   sizeof(PgColumnarGroupEntry) * (Size) newCap,
								   MCXT_ALLOC_HUGE | MCXT_ALLOC_ZERO);
	MemoryContextSwitchTo(old);

	for (i = 0; i < oldCap; i++)
	{
		PgColumnarGroupEntry *e = &state->entries[i];
		uint32		idx;

		if (!e->used)
			continue;
		state->hashRehashed++;
		idx = e->hash & (uint32) (newCap - 1);
		while (newEntries[idx].used)
			idx = (idx + 1) & (uint32) (newCap - 1);
		newEntries[idx] = *e;
	}

	if (state->entries != NULL)
		pfree(state->entries);
	state->entries = newEntries;
	state->capacity = newCap;
}

/*
 * pgcolumnar_groupagg_lookup
 *		Find the group for this row's keys, inserting a fresh one (with the key
 *		Datums copied into keyContext and accumulators seeded from the template)
 *		when it is new.
 */
static PgColumnarGroupEntry *
pgcolumnar_groupagg_lookup(PgColumnarGroupAggScanState *state,
						 Datum *keyvals, bool *keynulls)
{
	uint32		hash = 0;
	uint32		idx;
	int			k;
	PgColumnarGroupEntry *e;

	/* grow before probing so the index is computed against the final table */
	if ((int64) (state->nGroups + 1) * 10 >= (int64) state->capacity * 7)
		pgcolumnar_groupagg_grow(state);

	for (k = 0; k < state->nkeys; k++)
	{
		uint32		h;

		if (keynulls[k])
			h = 0x9e3779b9u;	/* fixed contribution for a null key */
		else if (state->keys[k].simple)
			h = pgcolumnar_groupagg_hash_datum(keyvals[k]);	/* #708 */
		else
			h = DatumGetUInt32(FunctionCall1Coll(&state->keys[k].hashFn,
												 state->keys[k].collation,
												 keyvals[k]));
		hash = hash_combine(hash, h);
	}

	idx = hash & (uint32) (state->capacity - 1);
	for (;;)
	{
		e = &state->entries[idx];
		if (!e->used)
			break;
		if (e->hash == hash &&
			pgcolumnar_groupagg_keys_equal(state, e, keyvals, keynulls))
			return e;
		idx = (idx + 1) & (uint32) (state->capacity - 1);
	}

	/*
	 * A new group. Bounding the actual group count keeps this no-spill hash table
	 * from growing without limit; over the cap we stop with guidance rather than
	 * exhaust memory, since the plan cannot fall back mid-scan.
	 */
	if (state->nGroups >= state->maxGroups)
		ereport(ERROR,
				(errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
				 errmsg("grouped vectorized aggregate exceeded pgcolumnar.groupagg_max_groups (%d)",
						state->maxGroups),
				 errhint("Raise pgcolumnar.groupagg_max_groups, or set "
						 "pgcolumnar.enable_group_vectorization = off.")));

	/* insert a new group here */
	e->used = true;
	e->hash = hash;
	{
		MemoryContext oldc = MemoryContextSwitchTo(state->keyContext);

		e->keys = (Datum *) palloc(sizeof(Datum) * Max(state->nkeys, 1));
		e->keyNulls = (bool *) palloc(sizeof(bool) * Max(state->nkeys, 1));
		for (k = 0; k < state->nkeys; k++)
		{
			e->keyNulls[k] = keynulls[k];
			if (keynulls[k])
				e->keys[k] = (Datum) 0;
			else
				e->keys[k] = datumCopy(keyvals[k], state->keys[k].byval,
									   state->keys[k].typlen);
		}
		MemoryContextSwitchTo(oldc);
	}
	{
		MemoryContext oldc = MemoryContextSwitchTo(state->specContext);

		e->specs = (PgColumnarAggSpec *)
			palloc(sizeof(PgColumnarAggSpec) * Max(state->naggs, 1));
		memcpy(e->specs, state->aggTemplate,
			   sizeof(PgColumnarAggSpec) * state->naggs);
		MemoryContextSwitchTo(oldc);
	}
	state->nGroups++;
	return e;
}

/*
 * pgcolumnar_groupagg_batch_eligible
 *		Whether this grouped aggregate can use the batch fold: the shape gates
 *		every fold needs (pgcolumnar_batch_gates_ok), plus the one this node adds
 *		of its own -- every group key must be a plain Var of the scanned
 *		relation, so the fold can take the key value out of a gathered column
 *		instead of evaluating an expression per row.
 *
 *		Decided from the query shape alone, so Begin can report it before there
 *		is any execution to report.
 */
static bool
pgcolumnar_groupagg_batch_eligible(PgColumnarGroupAggScanState *state,
								 TupleDesc tupdesc, ScanKey *keysOut,
								 int *nkeysOut)
{
	int			k;

	if (state->nkeys <= 0)
		return false;			/* the grouped node is never planned without one */

	for (k = 0; k < state->nkeys; k++)
	{
		if (state->keys[k].attidx < 0)
			return false;

		/*
		 * The fold reads the key out of cval[attidx], which is only gathered for
		 * columns in the projected set. Begin builds that set from these very
		 * key expressions, so this cannot fail today; it is here so that the
		 * fold's dependency is stated where the fold is decided rather than left
		 * as an invariant two functions apart, and so that a future change to
		 * the projected set is refused instead of reading an ungathered slot.
		 */
		if (!bms_is_member(state->keys[k].attidx, state->projected))
			return false;
	}

	/* the caller bounds these allocations; see pgcolumnar_batch_shape_eligible */
	return pgcolumnar_batch_gates_ok(state->aggTemplate, state->naggs,
								   state->quals, state->scanrelid,
								   state->projected, tupdesc,
								   keysOut, nkeysOut);
}

/*
 * pgcolumnar_groupagg_reset_table
 *		Drop everything the fold accumulated, so the row path can redo the whole
 *		scan from an empty hash table. Used only on the mid-scan fall-back below.
 */
static void
pgcolumnar_groupagg_reset_table(PgColumnarGroupAggScanState *state)
{
	MemoryContextReset(state->keyContext);
	MemoryContextReset(state->specContext);
	MemoryContextReset(state->hashContext);
	state->entries = NULL;
	state->capacity = 0;
	state->nGroups = 0;
}

/*
 * pgcolumnar_groupagg_batch_fold
 *		Fold the whole grouped scan column-at-a-time (#708). Returns false --
 *		having folded nothing the caller cannot redo -- when the shape is not
 *		eligible or a group is missing a needed column, in which case the caller
 *		runs the always-correct row path.
 *
 *		This is the grouped twin of pgcolumnar_native_batch_fold, and it is
 *		deliberately the same loop: walk row groups, gather each needed column's
 *		packed values, honour the delete mask and the skipped-vector map, apply
 *		the scan keys inline, then probe and fold. What the row path pays per row
 *		and this does not: PgColumnarReadNextRow, ResetExprContext, staging the
 *		row into a virtual slot, ExecStoreVirtualTuple, ExecQual, and one
 *		ExecEvalExpr per group key. What is left of the fmgr traffic -- the hash
 *		and the probe equality -- goes inline for the key types
 *		pgcolumnar_groupagg_simple_key admits.
 *
 *		It does NOT take the ungrouped fold's payload deferral (#405). Group key
 *		columns are needed for every surviving row by definition, so only the
 *		aggregate payload could ever be deferred, and the two-phase gather is
 *		surface this path has no measured need of yet.
 *
 *		The per-value delete/skip/scan-key handling below is load-bearing in
 *		exactly the ways the ungrouped fold documents at length, and the reasons
 *		are not repeated here: read pgcolumnar_native_batch_fold for why the
 *		present index must advance across a skipped or deleted row, why skipVec
 *		is exact rather than merely safe, and why an absent column cannot be
 *		fallen back from under a parallel partial node.
 */
static bool
pgcolumnar_groupagg_batch_fold(PgColumnarGroupAggScanState *state, Relation rel,
							 TupleDesc tupdesc)
{
	EState	   *estate = state->css.ss.ps.state;
	ScanKey		keys = NULL;
	int			nScanKeys = 0;
	int			natts = tupdesc->natts;
	PgColumnarReadState *rs;
	const char **cvalidity = (const char **) palloc0(sizeof(char *) * natts);
	const char **cpacked = (const char **) palloc0(sizeof(char *) * natts);
	int16	   *cattlen = (int16 *) palloc0(sizeof(int16) * natts);
	uint64	   *cpresent = (uint64 *) palloc0(sizeof(uint64) * natts);
	bool	   *cneeded = (bool *) palloc0(sizeof(bool) * natts);
	Datum	   *cval = (Datum *) palloc0(sizeof(Datum) * natts);
	bool	   *cisnull = (bool *) palloc0(sizeof(bool) * natts);
	/*
	 * The compact list of columns to gather, so the per-row loop steps over the
	 * columns this query reads rather than all natts. Order is column order and
	 * each column's cursor is independent, so walking the list is equivalent to
	 * walking the full range under the cneeded mask.
	 */
	int		   *needCols = (int *) palloc(sizeof(int) * Max(natts, 1));
	int			nNeed = 0;
	Datum	   *keyvals = (Datum *) palloc(sizeof(Datum) * Max(state->nkeys, 1));
	bool	   *keynulls = (bool *) palloc(sizeof(bool) * Max(state->nkeys, 1));
	int			a;
	int			k;
	int			col;
	int			ci;

	if (!pgcolumnar_groupagg_batch_eligible(state, tupdesc, &keys, &nScanKeys))
		return false;

	col = -1;
	while ((col = bms_next_member(state->projected, col)) >= 0)
		if (col >= 0 && col < natts)
			cneeded[col] = true;
	for (col = 0; col < natts; col++)
		if (cneeded[col])
			needCols[nNeed++] = col;

	rs = PgColumnarBeginRead(rel, estate->es_snapshot, NULL, state->projected,
						   nScanKeys, keys);

	/*
	 * Parallel partial run (#349): claim row groups through the shared atomic so
	 * each worker folds a distinct set and the core Finalize combines them by
	 * key. A partial node with no counter would fold every group in every worker
	 * and the Finalize would sum the duplicates -- a wrong answer, not a crash.
	 */
	if (state->parallelCounter != NULL)
		PgColumnarReadSetParallelCounter(rs, state->parallelCounter);
	else if (state->isPartial)
	{
		PgColumnarEndRead(rs);
		elog(ERROR, "parallel columnar grouped aggregate ran without a shared group counter");
	}

	while (PgColumnarReadFoldNextGroup(rs))
	{
		uint64		nrows;
		const char *dmask;
		uint32		dlen;
		const bool *skipVec;
		bool		decodeSkipped;
		const uint32 *vecStart;
		int			vcount;
		int			curVec;
		uint64		r;

		PgColumnarReadFoldGroupInfo(rs, &nrows, &dmask, &dlen,
								  &skipVec, &decodeSkipped, &vecStart, &vcount);

		if (decodeSkipped && (skipVec == NULL || vecStart == NULL || vcount <= 0))
			elog(ERROR,
				 "pgcolumnar: the grouped vectorized aggregate cannot fold a row "
				 "group whose decode skipped vectors without the per-vector map (#512)");

		for (ci = 0; ci < nNeed; ci++)
		{
			const char *vbits;
			const char *pk;
			int16		al;
			const uint32 *vrl;

			col = needCols[ci];
			cpresent[col] = 0;
			if (!PgColumnarReadFoldColumn(rs, col, &vbits, &pk, &al, &vrl))
			{
				/*
				 * The column is absent from this group (a later ADD COLUMN). The
				 * batch path cannot supply its missing value, so throw away what
				 * has been folded and let the caller redo the whole scan on the
				 * row path, which handles it.
				 *
				 * A parallel partial node cannot take that fall-back: this group
				 * and the ones before it were already claimed through the shared
				 * counter, so the row path would re-open with the counter
				 * advanced past them and undercount. Fail cleanly rather than
				 * return a wrong answer.
				 */
				PgColumnarEndRead(rs);
				pgcolumnar_groupagg_reset_table(state);
				if (state->isPartial)
					elog(ERROR, "parallel columnar grouped aggregate cannot fold a "
						 "relation with a column added after some row groups; "
						 "set pgcolumnar.enable_parallel_vector_agg = off");
				return false;
			}
			cvalidity[col] = vbits;
			cpacked[col] = pk;
			cattlen[col] = al;
		}

		curVec = 0;
		for (r = 0; r < nrows; r++)
		{
			bool		pass = true;
			bool		vecSkipped = false;
			PgColumnarGroupEntry *e;

			CHECK_FOR_INTERRUPTS();

			/*
			 * Which vector holds this row, and was it ruled out? vecStart is
			 * cumulative row spans with a [vcount] terminator and r only ever
			 * increases, so this walk costs one step per vector boundary across
			 * the group rather than a search per row.
			 */
			if (skipVec != NULL && vecStart != NULL && vcount > 0)
			{
				while (curVec < vcount && r >= vecStart[curVec + 1])
					curVec++;
				vecSkipped = (curVec < vcount && skipVec[curVec]);
			}

			/*
			 * Gather. The present index advances for every present value whether
			 * or not the value is read: the stream is packed by presence, so a
			 * skipped row's slot is consumed either way, and getting that wrong
			 * misaligns every later vector rather than losing one row. A skipped
			 * vector's bytes were never decoded, so consume the slot and do not
			 * read it.
			 */
			for (ci = 0; ci < nNeed; ci++)
			{
				col = needCols[ci];
				if ((cvalidity[col][r >> 3] >> (r & 7)) & 1)
				{
					if (!vecSkipped)
					{
						cval[col] = fetch_att(cpacked[col] + cpresent[col] * cattlen[col],
											  true, cattlen[col]);
						cisnull[col] = false;
					}
					cpresent[col]++;
				}
				else
					cisnull[col] = true;
			}

			if (vecSkipped)
				continue;
			if (dmask != NULL && (r >> 3) < dlen &&
				(dmask[r >> 3] & (1 << (r & 7))) != 0)
				continue;		/* deleted */

			for (k = 0; k < nScanKeys; k++)
			{
				ScanKey		key = &keys[k];
				int			attidx = key->sk_attno - 1;

				if (cisnull[attidx] ||
					!pgcolumnar_batch_key_pass(TupleDescAttr(tupdesc, attidx)->atttypid,
											 cval[attidx], key->sk_strategy,
											 key->sk_argument))
				{
					pass = false;
					break;
				}
			}
			if (!pass)
				continue;

			/*
			 * The group keys are plain Vars (eligibility requires it), so the
			 * value is already gathered and there is no expression to evaluate.
			 */
			for (k = 0; k < state->nkeys; k++)
			{
				int			ai = state->keys[k].attidx;

				keyvals[k] = cval[ai];
				keynulls[k] = cisnull[ai];
			}

			e = pgcolumnar_groupagg_lookup(state, keyvals, keynulls);

			for (a = 0; a < state->naggs; a++)
			{
				PgColumnarAggSpec *spec = &e->specs[a];

				if (spec->attidx >= 0)
					pgcolumnar_apply_one(state->specContext, spec,
									   cval[spec->attidx], cisnull[spec->attidx]);
				else
					pgcolumnar_apply_one(state->specContext, spec, (Datum) 0, true);
			}
		}
	}

	PgColumnarReadStats(rs, &state->groupsRead, &state->groupsSkipped,
					  &state->groupsTotal);
	state->usablePreds = PgColumnarReadUsablePredicates(rs);
	state->vectorsSkipped = PgColumnarVectorsSkipped(rs);
	state->vectorsDecoded = PgColumnarVectorsDecoded(rs);
	state->vectorDecodes = PgColumnarVectorDecodes(rs);
	state->vectorsRuledOutByValue = PgColumnarVectorsRuledOutByValue(rs);
	state->zoneMapProbes = PgColumnarZoneMapProbes(rs);
	state->haveStats = true;
	state->batchFolded = true;

	PgColumnarEndRead(rs);
	return true;
}

/*
 * pgcolumnar_groupagg_build
 *		Scan the relation once and fold every surviving row into its group. The
 *		reader prunes groups and vectors with the pushed-down WHERE; each row is
 *		rechecked against the whole WHERE, its keys evaluated, and its values
 *		folded in scan order so accumulators match the scalar Agg byte for byte.
 */
static void
pgcolumnar_groupagg_build(PgColumnarGroupAggScanState *state)
{
	EState	   *estate = state->css.ss.ps.state;
	ExprContext *econtext = state->css.ss.ps.ps_ExprContext;
	Relation	rel = table_open(state->relid, AccessShareLock);
	TupleDesc	basedesc = RelationGetDescr(rel);
	int			natts = basedesc->natts;
	Datum	   *values = (Datum *) palloc(sizeof(Datum) * natts);
	bool	   *nulls = (bool *) palloc(sizeof(bool) * natts);
	Datum	   *keyvals = (Datum *) palloc(sizeof(Datum) * Max(state->nkeys, 1));
	bool	   *keynulls = (bool *) palloc(sizeof(bool) * Max(state->nkeys, 1));
	PgColumnarReadState *rs;
	ScanKey		keys;
	int			nScanKeys = 0;
	uint64		rowNumber;

	PgColumnarFlushWriteStateForRelation(state->relid);
	PgColumnarFlushDeleteVectorForRelation(rel);

	/*
	 * Fold column-at-a-time when the shape allows it (#708). A false return
	 * means nothing was folded and nothing was claimed, so the row path below
	 * runs the whole scan from an empty table exactly as it did before.
	 */
	if (pgcolumnar_groupagg_batch_fold(state, rel, basedesc))
	{
		table_close(rel, AccessShareLock);
		return;
	}

	/* same lifetime as the ungrouped row path: the enclosing scratch owns it */
	keys = PgColumnarBuildScanKeys(state->quals, state->scanrelid, basedesc,
								 &nScanKeys);
	rs = PgColumnarBeginRead(rel, estate->es_snapshot, NULL, state->projected,
						   nScanKeys, keys);

	/*
	 * In a parallel partial run each participant folds only the row groups it
	 * claims from the shared counter, so the union across workers is every group
	 * exactly once (#349). NULL when this node is running leader-only, in which
	 * case the reader walks every group as before.
	 */
	if (state->parallelCounter != NULL)
		PgColumnarReadSetParallelCounter(rs, state->parallelCounter);

	/* columns outside the projection stay null in the base slot */
	memset(state->baseSlot->tts_isnull, true, sizeof(bool) * natts);

	while (PgColumnarReadNextRow(rs, values, nulls, &rowNumber))
	{
		int			x;
		int			k;
		int			a;
		PgColumnarGroupEntry *e;

		ResetExprContext(econtext);

		/* stage the projected columns into the base slot */
		ExecClearTuple(state->baseSlot);
		x = -1;
		while ((x = bms_next_member(state->projected, x)) >= 0)
		{
			state->baseSlot->tts_values[x] = values[x];
			state->baseSlot->tts_isnull[x] = nulls[x];
		}
		ExecStoreVirtualTuple(state->baseSlot);
		econtext->ecxt_scantuple = state->baseSlot;

		/* recheck the full WHERE against this row */
		if (state->whereState != NULL && !ExecQual(state->whereState, econtext))
			continue;

		/*
		 * Evaluate the group keys in the per-tuple context (reset each row at the
		 * top of this loop), not the query context ExecEvalExpr would use, so a
		 * byref key does not leak one allocation per scanned row. Datums that
		 * start a new group are datumCopy'd into keyContext by the lookup.
		 */
		for (k = 0; k < state->nkeys; k++)
			keyvals[k] = ExecEvalExprSwitchContext(state->keys[k].exprState,
												   econtext, &keynulls[k]);

		e = pgcolumnar_groupagg_lookup(state, keyvals, keynulls);

		/* fold this row's values into the group's accumulators */
		for (a = 0; a < state->naggs; a++)
		{
			PgColumnarAggSpec *spec = &e->specs[a];

			if (spec->attidx >= 0)
				pgcolumnar_apply_one(state->specContext, spec,
								   values[spec->attidx], nulls[spec->attidx]);
			else
				pgcolumnar_apply_one(state->specContext, spec, (Datum) 0, true);
		}
	}

	PgColumnarReadStats(rs, &state->groupsRead, &state->groupsSkipped,
					  &state->groupsTotal);
	state->usablePreds = PgColumnarReadUsablePredicates(rs);
	state->vectorsSkipped = PgColumnarVectorsSkipped(rs);
	state->vectorsDecoded = PgColumnarVectorsDecoded(rs);
	state->vectorDecodes = PgColumnarVectorDecodes(rs);
	state->vectorsRuledOutByValue = PgColumnarVectorsRuledOutByValue(rs);
	state->zoneMapProbes = PgColumnarZoneMapProbes(rs);
	state->haveStats = true;

	PgColumnarEndRead(rs);
	table_close(rel, AccessShareLock);
}

static TupleTableSlot *
PgColumnarExecGroupAggScan(CustomScanState *node)
{
	PgColumnarGroupAggScanState *state = (PgColumnarGroupAggScanState *) node;
	TupleTableSlot *scanSlot = node->ss.ss_ScanTupleSlot;
	ExprContext *econtext = node->ss.ps.ps_ExprContext;

	if (!state->started)
	{
		/*
		 * The grouped twin of the ungrouped node's scratch context (#727), so
		 * that ONE mechanism owns the lifetime of everything a scan allocates
		 * on both nodes rather than two overlapping ones.
		 *
		 * Everything this build RETAINS already lives in a context of its own:
		 * the group keys in keyContext, the accumulators in specContext and the
		 * table in hashContext, each switched into explicitly by
		 * pgcolumnar_groupagg_lookup and pgcolumnar_groupagg_grow. What is left
		 * -- the scan keys, the per-row value and null arrays, the reader's own
		 * state -- is scratch for the duration of the build, and is what this
		 * reclaims.
		 *
		 * Wrapped at the call site for the same reason as the ungrouped one:
		 * the fold's early return, the ADD COLUMN fall-back and the normal end
		 * would each need their own restore, and missing one leaks the context
		 * instead of the memory.
		 */
		MemoryContext buildCxt = AllocSetContextCreate(CurrentMemoryContext,
													   "columnar groupagg build",
													   ALLOCSET_DEFAULT_SIZES);
		MemoryContext oldCxt = MemoryContextSwitchTo(buildCxt);

		pgcolumnar_groupagg_build(state);

		MemoryContextSwitchTo(oldCxt);
		MemoryContextDelete(buildCxt);

		state->started = true;
		state->emitPos = 0;
	}

	while (state->emitPos < state->capacity)
	{
		PgColumnarGroupEntry *e = &state->entries[state->emitPos++];
		int			p;

		if (!e->used)
			continue;

		ResetExprContext(econtext);
		ExecClearTuple(scanSlot);
		for (p = 0; p < state->nout; p++)
		{
			int			m = state->outMap[p];

			if (m >= 0)
			{
				scanSlot->tts_values[p] = e->keys[m];
				scanSlot->tts_isnull[p] = e->keyNulls[m];
			}
			else
			{
				int			a = -(m) - 1;

				/*
				 * A partial node hands the core Finalize this worker's transition
				 * state for the group rather than the finished value; the Finalize
				 * combines the states of every worker that saw the same key
				 * (#349). A worker that claimed no row groups has an empty hash
				 * table and so emits nothing at all, which is what the Finalize
				 * expects -- not one empty group.
				 */
				scanSlot->tts_values[p] = state->isPartial
					? pgcolumnar_agg_emit_partial(&e->specs[a],
											   &scanSlot->tts_isnull[p])
					: pgcolumnar_agg_finalize(&e->specs[a],
											&scanSlot->tts_isnull[p]);
			}
		}
		ExecStoreVirtualTuple(scanSlot);

		if (node->ss.ps.ps_ProjInfo != NULL)
		{
			econtext->ecxt_scantuple = scanSlot;
			return ExecProject(node->ss.ps.ps_ProjInfo);
		}
		return scanSlot;
	}

	return NULL;
}

static void
PgColumnarEndGroupAggScan(CustomScanState *node)
{
	PgColumnarGroupAggScanState *state = (PgColumnarGroupAggScanState *) node;

	if (state->baseSlot != NULL)
		ExecDropSingleTupleTableSlot(state->baseSlot);
	state->baseSlot = NULL;
	/* the memory contexts are children of es_query_cxt and freed with it */
}

static void
PgColumnarReScanGroupAggScan(CustomScanState *node)
{
	PgColumnarGroupAggScanState *state = (PgColumnarGroupAggScanState *) node;

	state->started = false;
	state->emitPos = 0;
	state->nGroups = 0;
	state->capacity = 0;
	state->entries = NULL;
	state->haveStats = false;
	state->batchFolded = false;
	MemoryContextReset(state->keyContext);
	MemoryContextReset(state->specContext);
	MemoryContextReset(state->hashContext);
}

static void
PgColumnarExplainGroupAggScan(CustomScanState *node, List *ancestors,
							ExplainState *es)
{
	PgColumnarGroupAggScanState *state = (PgColumnarGroupAggScanState *) node;

	ExplainPropertyInteger("Columnar Vectorized Group Keys", NULL,
						   state->nkeys, es);
	ExplainPropertyInteger("Columnar Vectorized Aggregates", NULL,
						   state->naggs, es);

	/*
	 * The work-done line for #708, and the only assertion that goes red when
	 * this fold stops folding: the answers are identical either way, so no
	 * correctness check can see the difference (the #545 rule).
	 *
	 * Plain EXPLAIN has no execution to report and so shows the shape
	 * prediction; once the node has run, report what actually happened, because
	 * a predicted-eligible fold can still fall back to the row path (a column
	 * added after some row groups). Printing the prediction under ANALYZE is the
	 * #602 defect, and this node inherits the pattern that caused it.
	 */
	ExplainPropertyText("Columnar Batch Fold",
						(state->haveStats ? state->batchFolded
										  : state->batchEligible)
						? "yes" : "no", es);
	PgColumnarExplainPushedDown(state->nscankeys, es);
	PgColumnarExplainVectorPredicates(state->npreds, es);

	if (state->haveStats)
	{
		PgColumnarGroupStats gs;

		/*
		 * Work done, not work predicted. Under ANALYZE only, for the reason the
		 * batch-fold line above records (#602): a plain EXPLAIN has nothing to
		 * report because the table was never built.
		 */
		ExplainPropertyInteger("Columnar Group Table Resizes", NULL,
							   state->hashResizes, es);
		ExplainPropertyInteger("Columnar Group Table Entries Rehashed", NULL,
							   state->hashRehashed, es);
		ExplainPropertyInteger("Columnar Group Table Capacity", NULL,
							   state->peakCapacity, es);

		gs.usableSkipPredicates = state->usablePreds;
		gs.groupsTotal = state->groupsTotal;
		gs.groupsRead = state->groupsRead;
		gs.groupsRemoved = state->groupsSkipped;
		gs.vectorsSkipped = state->vectorsSkipped;
		gs.vectorsDecoded = state->vectorsDecoded;
		gs.vectorDecodes = state->vectorDecodes;
		gs.vectorsRuledOutByValue = state->vectorsRuledOutByValue;
		gs.zoneMapProbes = state->zoneMapProbes;
		PgColumnarExplainGroupStats(&gs, es);
	}
}

static const CustomExecMethods pgcolumnar_groupagg_exec_methods = {
	.CustomName = "PgColumnarScan",
	.BeginCustomScan = PgColumnarBeginGroupAggScan,
	.ExecCustomScan = PgColumnarExecGroupAggScan,
	.EndCustomScan = PgColumnarEndGroupAggScan,
	.ReScanCustomScan = PgColumnarReScanGroupAggScan,
	.ExplainCustomScan = PgColumnarExplainGroupAggScan,
};

/* -------------------------------------------------------------------------
 * parallel partial grouped aggregate (#349): DSM callbacks
 *
 * The same shared pg_atomic_uint32 the ungrouped partial node uses (gap 23),
 * handing out row-group indices so each worker folds distinct groups. These
 * mirror the ungrouped four and cannot share their bodies: those cast the node
 * to PgColumnarAggScanState, and the grouped node is a different struct.
 *
 * The grouped node opens its reader lazily in Exec, strictly after both DSM-init
 * and Worker-init, so the callbacks need only record the counter on the state.
 * ------------------------------------------------------------------------- */

static Size
PgColumnarEstimateDSMGroupAggScan(CustomScanState *node, ParallelContext *pcxt)
{
	return sizeof(pg_atomic_uint32);
}

static void
PgColumnarInitializeDSMGroupAggScan(CustomScanState *node, ParallelContext *pcxt,
								  void *coordinate)
{
	PgColumnarGroupAggScanState *state = (PgColumnarGroupAggScanState *) node;
	pg_atomic_uint32 *counter = (pg_atomic_uint32 *) coordinate;

	pg_atomic_init_u32(counter, 0);
	state->parallelCounter = counter;

	/*
	 * Flush this backend's pending writes and deletes in the leader, before any
	 * worker launches: a worker is a separate backend and cannot see the leader's
	 * unflushed in-transaction buffers. Mirrors what #343 does for the ungrouped
	 * node.
	 *
	 * Defensive rather than load-bearing today, which is worth stating precisely
	 * because the comment on the ungrouped copy reads as though it were required.
	 * Removing both flushes and running in-transaction INSERT and DELETE followed
	 * by a confirmed-parallel grouped aggregate produces answers identical to
	 * serial, because the buffers are already flushed at the command boundary
	 * before the aggregate is planned. It is kept because it is cheap and because
	 * a future path that reaches here with unflushed state would silently give
	 * workers a stale view; no test covers its removal, and none claims to.
	 */
	PgColumnarFlushWriteStateForRelation(state->relid);
	{
		Relation	frel = table_open(state->relid, AccessShareLock);

		PgColumnarFlushDeleteVectorForRelation(frel);
		table_close(frel, AccessShareLock);
	}
}

static void
PgColumnarReInitializeDSMGroupAggScan(CustomScanState *node, ParallelContext *pcxt,
									void *coordinate)
{
	pg_atomic_uint32 *counter = (pg_atomic_uint32 *) coordinate;

	/* a rescan restarts group claiming from zero for every participant */
	pg_atomic_write_u32(counter, 0);
}

static void
PgColumnarInitializeWorkerGroupAggScan(CustomScanState *node, shm_toc *toc,
									 void *coordinate)
{
	PgColumnarGroupAggScanState *state = (PgColumnarGroupAggScanState *) node;
	pg_atomic_uint32 *counter = (pg_atomic_uint32 *) coordinate;

	state->parallelCounter = counter;
}

static const CustomExecMethods pgcolumnar_groupagg_parallel_exec_methods = {
	.CustomName = "PgColumnarScan",
	.BeginCustomScan = PgColumnarBeginGroupAggScan,
	.ExecCustomScan = PgColumnarExecGroupAggScan,
	.EndCustomScan = PgColumnarEndGroupAggScan,
	.ReScanCustomScan = PgColumnarReScanGroupAggScan,
	.ExplainCustomScan = PgColumnarExplainGroupAggScan,
	.EstimateDSMCustomScan = PgColumnarEstimateDSMGroupAggScan,
	.InitializeDSMCustomScan = PgColumnarInitializeDSMGroupAggScan,
	.ReInitializeDSMCustomScan = PgColumnarReInitializeDSMGroupAggScan,
	.InitializeWorkerCustomScan = PgColumnarInitializeWorkerGroupAggScan,
};

/* -------------------------------------------------------------------------
 * registration
 * ------------------------------------------------------------------------- */

void
PgColumnarVectorInit(void)
{
	prev_create_upper_paths_hook = create_upper_paths_hook;
	create_upper_paths_hook = PgColumnarCreateUpperPaths;
}
