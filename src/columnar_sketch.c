/*-------------------------------------------------------------------------
 * columnar_sketch.c
 *		pgcolumnar.approx_count_distinct (#1309 phase 2): a HyperLogLog count of
 *		distinct values over the type's extended hash (seed 0), with the same
 *		2048 registers and register rule as the per-row-group sketches the writer
 *		stores (pgc_sketch_add), so the two can be merged.
 *
 * The state is the registers. It travels between parallel workers as a bytea of
 * exactly PGC_SKETCH_REGISTERS bytes, and the combine is a register-wise max,
 * which does not depend on which worker saw which value: a parallel plan returns
 * the serial answer exactly.
 *
 * NULLs are not hashed, matching count(DISTINCT). A type with no extended hash,
 * or a value under a nondeterministic collation, is refused with 0A000, for the
 * reason set_options refuses such a sketch column: two values equal under the
 * collation would hash apart and count twice.
 *-------------------------------------------------------------------------
 */
#include "postgres.h"

#include <math.h>

#include "columnar.h"
#include "fmgr.h"
#include "utils/builtins.h"
#include "utils/typcache.h"

PG_FUNCTION_INFO_V1(pgcolumnar_acd_transfn);
PG_FUNCTION_INFO_V1(pgcolumnar_acd_combinefn);
PG_FUNCTION_INFO_V1(pgcolumnar_acd_serialfn);
PG_FUNCTION_INFO_V1(pgcolumnar_acd_deserialfn);
PG_FUNCTION_INFO_V1(pgcolumnar_acd_finalfn);

typedef struct PgcSketchState
{
	bool		haveHash;		/* hashFn resolved, on the first non-NULL value */
	FmgrInfo	hashFn;
	Oid			collation;
	uint8		regs[PGC_SKETCH_REGISTERS];
} PgcSketchState;

/*
 * pgc_sketch_estimate
 *		The count a sketch's registers estimate. The raw HyperLogLog estimate,
 *		alpha_m * m^2 / sum(2^-M[j]) with alpha_m = 0.7213 / (1 + 1.079 / m), and
 *		linear counting, m * ln(m / zeros), when the raw estimate is at most 5m/2
 *		and some register is zero (Flajolet et al. 2007). The hash is 64 bits, so
 *		no large-range correction is needed. Fixed, so a pinned value means
 *		something.
 */
double
pgc_sketch_estimate(const uint8 *regs)
{
	const double m = (double) PGC_SKETCH_REGISTERS;
	double		alpha = 0.7213 / (1.0 + 1.079 / m);
	double		sum = 0.0;
	int			zeros = 0;
	int			j;
	double		raw;

	for (j = 0; j < PGC_SKETCH_REGISTERS; j++)
	{
		sum += ldexp(1.0, -(int) regs[j]);
		if (regs[j] == 0)
			zeros++;
	}
	raw = alpha * m * m / sum;
	if (raw <= 2.5 * m && zeros > 0)
		return m * log(m / (double) zeros);
	return raw;
}

static PgcSketchState *
pgc_sketch_state_new(MemoryContext aggcontext)
{
	return (PgcSketchState *) MemoryContextAllocZero(aggcontext, sizeof(PgcSketchState));
}

static MemoryContext
pgc_sketch_aggcontext(FunctionCallInfo fcinfo, const char *fn)
{
	MemoryContext aggcontext;

	if (!AggCheckCallContext(fcinfo, &aggcontext))
		elog(ERROR, "%s called in non-aggregate context", fn);
	return aggcontext;
}

Datum
pgcolumnar_acd_transfn(PG_FUNCTION_ARGS)
{
	MemoryContext aggcontext = pgc_sketch_aggcontext(fcinfo, "pgcolumnar_acd_transfn");
	PgcSketchState *state = PG_ARGISNULL(0) ? pgc_sketch_state_new(aggcontext)
		: (PgcSketchState *) PG_GETARG_POINTER(0);

	if (PG_ARGISNULL(1))
		PG_RETURN_POINTER(state);

	if (!state->haveHash)
	{
		Oid			argtype = get_fn_expr_argtype(fcinfo->flinfo, 1);
		TypeCacheEntry *tce = lookup_type_cache(argtype, TYPECACHE_HASH_EXTENDED_PROC_FINFO);
		Oid			collation = PG_GET_COLLATION();

		if (!OidIsValid(tce->hash_extended_proc_finfo.fn_oid))
			ereport(ERROR,
					(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
					 errmsg("pgcolumnar.approx_count_distinct cannot count type %s: "
							"it has no extended hash function", format_type_be(argtype))));
		if (!PgColumnarCollationIsDeterministic(collation))
			ereport(ERROR,
					(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
					 errmsg("pgcolumnar.approx_count_distinct cannot count under a "
							"nondeterministic collation"),
					 errhint("Two values equal under the collation would hash apart "
							 "and count twice.")));
		fmgr_info_cxt(tce->hash_extended_proc_finfo.fn_oid, &state->hashFn, aggcontext);
		state->collation = collation;
		state->haveHash = true;
	}

	pgc_sketch_add(state->regs,
				   DatumGetUInt64(FunctionCall2Coll(&state->hashFn, state->collation,
													PG_GETARG_DATUM(1), Int64GetDatum(0))));
	PG_RETURN_POINTER(state);
}

Datum
pgcolumnar_acd_combinefn(PG_FUNCTION_ARGS)
{
	MemoryContext aggcontext = pgc_sketch_aggcontext(fcinfo, "pgcolumnar_acd_combinefn");
	PgcSketchState *a = PG_ARGISNULL(0) ? NULL : (PgcSketchState *) PG_GETARG_POINTER(0);
	PgcSketchState *b = PG_ARGISNULL(1) ? NULL : (PgcSketchState *) PG_GETARG_POINTER(1);
	int			j;

	if (b == NULL)
	{
		if (a == NULL)
			PG_RETURN_NULL();
		PG_RETURN_POINTER(a);
	}
	if (a == NULL)
	{
		a = pgc_sketch_state_new(aggcontext);
		memcpy(a->regs, b->regs, PGC_SKETCH_REGISTERS);
		PG_RETURN_POINTER(a);
	}
	for (j = 0; j < PGC_SKETCH_REGISTERS; j++)
		if (b->regs[j] > a->regs[j])
			a->regs[j] = b->regs[j];
	PG_RETURN_POINTER(a);
}

Datum
pgcolumnar_acd_serialfn(PG_FUNCTION_ARGS)
{
	PgcSketchState *state = (PgcSketchState *) PG_GETARG_POINTER(0);
	bytea	   *out = (bytea *) palloc(VARHDRSZ + PGC_SKETCH_REGISTERS);

	SET_VARSIZE(out, VARHDRSZ + PGC_SKETCH_REGISTERS);
	memcpy(VARDATA(out), state->regs, PGC_SKETCH_REGISTERS);
	PG_RETURN_BYTEA_P(out);
}

Datum
pgcolumnar_acd_deserialfn(PG_FUNCTION_ARGS)
{
	MemoryContext aggcontext = pgc_sketch_aggcontext(fcinfo, "pgcolumnar_acd_deserialfn");
	bytea	   *in = PG_GETARG_BYTEA_PP(0);
	PgcSketchState *state;

	if (VARSIZE_ANY_EXHDR(in) != PGC_SKETCH_REGISTERS)
		elog(ERROR, "approx_count_distinct state is %zu bytes, want %d",
			 (size_t) VARSIZE_ANY_EXHDR(in), PGC_SKETCH_REGISTERS);
	state = pgc_sketch_state_new(aggcontext);
	memcpy(state->regs, VARDATA_ANY(in), PGC_SKETCH_REGISTERS);
	PG_RETURN_POINTER(state);
}

Datum
pgcolumnar_acd_finalfn(PG_FUNCTION_ARGS)
{
	PgcSketchState *state;

	if (PG_ARGISNULL(0))
		PG_RETURN_INT64(0);
	state = (PgcSketchState *) PG_GETARG_POINTER(0);
	PG_RETURN_INT64((int64) llround(pgc_sketch_estimate(state->regs)));
}
