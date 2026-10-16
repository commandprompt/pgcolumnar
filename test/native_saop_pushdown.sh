#!/usr/bin/env bash
#
# pgColumnar chunk-group skipping works for IN-list / = ANY(array) predicates.
#
# The scan-key builder (pgcolumnar_clause_to_scankey) accepted only OpExpr, so a
# ScalarArrayOpExpr -- `col IN (...)` or `col = ANY(array)` -- built no scan key
# and the scan read every row group, while every comparable predicate shape
# (equality, range, anchored LIKE, parameterized scalar) already pruned (#704).
#
# The fix keeps up to 128 non-NULL elements as one set predicate. A group
# survives when any element can lie in its zone map and bloom filter; the
# executor still rechecks exact membership. Larger sets retain the bounded
# [min,max] hull.
#
# The correctness hazards, each with an arm below: a negated SAOP must build no
# keys (NOT IN admits everything outside the list); an empty or all-NULL array
# must not crash or mis-count; a parameterized array ($1 on a generic plan) is
# frozen at Begin like #697's scalars, but a correlated PARAM_EXEC array changes
# per rescan and must NOT be frozen.
#
# Usage:  test/native_saop_pushdown.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

q "CREATE EXTENSION IF NOT EXISTS pgcolumnar;" >/dev/null
# 40000 rows / 2000 = 20 monotonic groups; values >= 39000 live in the last one.
# ov = (g%40)*2+10 gives every group the same even-valued range [10,88], so an
# odd probe value is inside every group's [min,max] but present in none: range
# keys prune nothing there, only the bloom probe can, which isolates the
# single-value-list equality-key refinement.
q "SET pgcolumnar.stripe_row_limit=2000;
   CREATE DOMAIN intdom AS int;
   CREATE TABLE t (id int, ts int, txt text COLLATE \"C\", ov int) USING pgcolumnar;
   INSERT INTO t SELECT g, g, lpad(g::text, 8, '0'), (g % 40)*2 + 10
     FROM generate_series(1,40000) g;
   CREATE TABLE thr (lo int[]); INSERT INTO thr VALUES ('{39100}'),('{100,200}');" >/dev/null

psql_c() { env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At -v ON_ERROR_STOP=1 -c "$1" 2>&1; }

# Chunk groups removed by a query, from EXPLAIN ANALYZE. Prints the count, or
# NOSCAN when the plan did not use the columnar scan at all -- so a plan-shape
# change reads as its own failure, not as "0 groups removed".
groups_removed() {
	local plan
	plan="$(psql_c "EXPLAIN (ANALYZE, TIMING off, SUMMARY off) $1")"
	case "$plan" in
		*"Chunk Groups Removed by Filter"*)
			sed -n 's/.*Chunk Groups Removed by Filter: \([0-9]*\).*/\1/p' <<<"$plan" | head -1 ;;
		*) echo "NOSCAN($plan)" ;;
	esac
}

# --- pruning arms (RED before the fix: each reads 0) ---

# The full instrument on the primary arm: the plan pushed both range keys
# (intent), the reader accepted both as skip predicates (usable), and 19
# groups were in fact removed (work done). The three lines are distinct
# counters precisely so intent cannot impersonate work (#477/#479).
saop_plan="$(psql_c 'EXPLAIN (ANALYZE, TIMING off, SUMMARY off) SELECT count(*) FROM t WHERE ts IN (39100, 39200);')"
check "an IN-list pushes one set filter (intent)" \
	"$(sed -n 's/.*Columnar Pushed-Down Filters: \([0-9]*\).*/\1/p' <<<"$saop_plan" | head -1)" "1"
check "the IN-list set filter is a usable skip predicate" \
	"$(sed -n 's/.*Columnar Usable Skip Predicates: \([0-9]*\).*/\1/p' <<<"$saop_plan" | head -1)" "1"
check "an IN-list confined to the last group prunes (19 of 20 removed)" \
	"$(sed -n 's/.*Chunk Groups Removed by Filter: \([0-9]*\).*/\1/p' <<<"$saop_plan" | head -1)" "19"

check "IN-list count matches the OR-literal equivalent" \
	"$(q 'SELECT count(*) FROM t WHERE ts IN (39100, 39200);')" \
	"$(q 'SELECT count(*) FROM t WHERE ts = 39100 OR ts = 39200;')"

# A scattered set is the removal proof for per-element pruning. Its hull spans
# nearly the whole table, so the old [min,max] reduction removes no group; testing
# each value against the group range removes every group except the three that
# can hold one listed value. A contiguous set is the negative control: its hull
# and its elements imply the same one surviving group.
check "a scattered IN-list prunes by element, not only by its hull (17 removed)" \
	"$(groups_removed 'SELECT count(*) FROM t WHERE ts IN (100, 20100, 38100);')" "17"
check "scattered per-element pruning keeps the exact answer" \
	"$(q 'SELECT count(*) FROM t WHERE ts IN (100, 20100, 38100);')" "3"
check "a contiguous IN-list agrees with its hull (19 removed)" \
	"$(groups_removed 'SELECT count(*) FROM t WHERE ts IN (100, 101, 102);')" "19"
check "contiguous per-element pruning keeps the exact answer" \
	"$(q 'SELECT count(*) FROM t WHERE ts IN (100, 101, 102);')" "3"

# Pin both sides of the O(elements * rows) bound.
limit_list="$(seq -s, 1 128)"
limit_plan="$(psql_c "EXPLAIN (ANALYZE, TIMING off, SUMMARY off)
	SELECT count(*) FROM t WHERE ts IN ($limit_list);")"
check "exactly 128 elements still use one set filter" \
	"$(sed -n 's/.*Columnar Pushed-Down Filters: \([0-9]*\).*/\1/p' <<<"$limit_plan" | head -1)" "1"

# Above 128 non-NULL elements the builder deliberately keeps the old two-key hull.
large_list="$(seq -s, 1 129)"
large_plan="$(psql_c "EXPLAIN (ANALYZE, TIMING off, SUMMARY off)
	SELECT count(*) FROM t WHERE ts IN ($large_list);")"
check "a large IN-list falls back to two bounded hull filters" \
	"$(sed -n 's/.*Columnar Pushed-Down Filters: \([0-9]*\).*/\1/p' <<<"$large_plan" | head -1)" "2"
check "the large-list fallback keeps the exact answer" \
	"$(q "SELECT count(*) FROM t WHERE ts IN ($large_list);")" "129"

check "a NULL element is ignored for the range, not a bailout (19 removed)" \
	"$(groups_removed 'SELECT count(*) FROM t WHERE ts = ANY (ARRAY[39100, NULL]::int[]);')" "19"

check "NULL-element count is exact" \
	"$(q 'SELECT count(*) FROM t WHERE ts = ANY (ARRAY[39100, NULL]::int[]);')" "1"

check "a multi-value integer set strips NULL and still prunes 17 groups" \
	"$(groups_removed 'SELECT count(*) FROM t WHERE ts = ANY (ARRAY[100,20100,38100,NULL]::int[]);')" "17"
check "the multi-value integer NULL set stays exact" \
	"$(q 'SELECT count(*) FROM t WHERE ts = ANY (ARRAY[100,20100,38100,NULL]::int[]);')" "3"

check "a by-reference text set strips NULL without crashing (17 removed)" \
	"$(groups_removed "SELECT count(*) FROM t
		WHERE txt = ANY (ARRAY['00000100','00020100','00038100',NULL]::text[]);")" "17"
check "the by-reference text NULL set stays exact" \
	"$(q "SELECT count(*) FROM t
		WHERE txt = ANY (ARRAY['00000100','00020100','00038100',NULL]::text[]);")" "3"

check "a cross-type element array (int col, bigint[] list) prunes (19 removed)" \
	"$(groups_removed "SELECT count(*) FROM t WHERE ts = ANY ('{39100,39200}'::bigint[]);")" "19"

check "cross-type count is exact" \
	"$(q "SELECT count(*) FROM t WHERE ts = ANY ('{39100,39200}'::bigint[]);")" "2"

check "a C-collated text IN-list prunes (19 removed)" \
	"$(groups_removed "SELECT count(*) FROM t WHERE txt IN ('00039100', '00039200');")" "19"

check "text IN-list count is exact" \
	"$(q "SELECT count(*) FROM t WHERE txt IN ('00039100', '00039200');")" "2"

# Characterization: a domain-element cast arrives with the BASE type's OID in
# the array header (probed: literal cast, ArrayExpr cast, and a domain-array
# param all prune with no domain resolution in the builder), so no getBaseType
# is needed. These arms lock that in; if a PG version changes the header OID,
# they go red and the builder needs #483's resolution.
check "a domain-element array prunes (19 removed)" \
	"$(groups_removed "SELECT count(*) FROM t WHERE ts = ANY ('{39100,39200}'::intdom[]);")" "19"

check "domain-element count is exact" \
	"$(q "SELECT count(*) FROM t WHERE ts = ANY ('{39100,39200}'::intdom[]);")" "2"

# Fixture premises first (assert-the-fixture-premise). The range probe pins
# that 25 lies INSIDE every group's stored [min, max] -- range keys alone
# prune nothing -- so the 20-removed arms below can only be the bloom probe.
# If the ov formula or the stripe size ever drifts so some group's range
# excludes 25, this goes red instead of the removal proof quietly dissolving.
check "fixture premise: 25 is inside every group's range (range keys prune 0)" \
	"$(groups_removed 'SELECT count(*) FROM t WHERE ov >= 25 AND ov <= 25;')" "0"

check "fixture premise: equality on the overlap column bloom-prunes all 20" \
	"$(groups_removed 'SELECT count(*) FROM t WHERE ov = 25;')" "20"

# Every group's zone map is [10,88], so only the set's bloom loop can exclude
# these three absent odd values.
check "a multi-value set bloom-prunes all overlapping groups" \
	"$(groups_removed "SELECT count(*) FROM t WHERE ov = ANY ('{25,27,29}'::int[]);")" "20"
check "the bloom-only multi-value set returns the exact empty answer" \
	"$(q "SELECT count(*) FROM t WHERE ov = ANY ('{25,27,29}'::int[]);")" "0"

# `IN (25)` parses to plain `= 25` (the parser collapses a one-element list),
# so it exercises the OpExpr path; the `= ANY('{25,25}')` arm below is the
# SAOP shape. As two range keys [25,25] it removes 0, so it reddens when the
# single-distinct-value case is emitted as a range. It does NOT prove the
# equality-key refinement is needed: without it the list takes the set path,
# whose bloom loop probes 25 too and removes all 20 (measured).
check "a one-element IN-list (parser-collapsed to =) bloom-prunes (20 removed)" \
	"$(groups_removed 'SELECT count(*) FROM t WHERE ov IN (25);')" "20"

check "a single-DISTINCT-value SAOP list ('{25,25}') bloom-prunes (20 removed)" \
	"$(groups_removed "SELECT count(*) FROM t WHERE ov = ANY ('{25,25}'::int[]);")" "20"

check "single-value IN-list count is exact (0 rows)" \
	"$(q 'SELECT count(*) FROM t WHERE ov IN (25);')" "0"

# --- conservativeness arms (must hold before AND after the fix) ---

# A list spanning the whole range still prunes the groups BETWEEN its elements:
# this is the distinction the old hull could not express.
check "a range-spanning IN-list prunes between its elements (18 removed, exact count)" \
	"$(groups_removed 'SELECT count(*) FROM t WHERE ts IN (100, 39900);')/$(q 'SELECT count(*) FROM t WHERE ts IN (100, 39900);')" \
	"18/2"

# The NOT IN list is confined to ONE group on purpose: a builder that wrongly
# derived the positive [19000, 19500] range from the negated clause would skip
# the 19 other groups -- all full of matching rows -- and the count would
# collapse to 1998. A list spanning the table could not catch that, because a
# wrong range intersecting every group prunes nothing. The removed=0 companion
# pins that no keys are built at all.
check "NOT IN builds no range (0 removed) and stays correct" \
	"$(groups_removed 'SELECT count(*) FROM t WHERE ts NOT IN (19000, 19500);')/$(q 'SELECT count(*) FROM t WHERE ts NOT IN (19000, 19500);')" \
	"0/39998"

check "an empty array counts zero and does not crash" \
	"$(q "SELECT count(*) FROM t WHERE ts = ANY ('{}'::int[]);")" "0"

check "an all-NULL array counts zero and does not crash" \
	"$(q "SELECT count(*) FROM t WHERE ts = ANY (ARRAY[NULL,NULL]::int[]);")" "0"

# --- parameterized arms (#697 machinery extended to array args) ---

gp_removed() {
	psql_c "SET plan_cache_mode=force_generic_plan;
		DEALLOCATE ALL;
		PREPARE p(int[]) AS SELECT count(*) FROM t WHERE ts = ANY (\$1);
		EXPLAIN (ANALYZE, TIMING off, SUMMARY off) EXECUTE p('{39100,39200}');" \
		| sed -n 's/.*Chunk Groups Removed by Filter: \([0-9]*\).*/\1/p' | head -1
}

check "a generic-plan parameterized array prunes (19 removed)" \
	"$(gp_removed)" "19"

check "parameterized array count matches the literal count" \
	"$(psql_c "SET plan_cache_mode=force_generic_plan;
		DEALLOCATE ALL;
		PREPARE c(int[]) AS SELECT count(*) FROM t WHERE ts = ANY (\$1);
		EXECUTE c('{39100,39200}');" | tail -1)" "2"

# A MIXED list -- `IN (literal, $1)` -- stays an unfolded ArrayExpr in a
# generic plan (eval_const_expressions folds an ArrayExpr only when every
# element is Const), so it prunes only because the freeze evaluates the WHOLE
# array argument, not just a bare Param. This is the shape #697 existed for.
mixed_removed() {
	psql_c "SET plan_cache_mode=force_generic_plan;
		DEALLOCATE ALL;
		PREPARE m(int) AS SELECT count(*) FROM t WHERE ts IN (39100, \$1);
		EXPLAIN (ANALYZE, TIMING off, SUMMARY off) EXECUTE m(39200);" \
		| sed -n 's/.*Chunk Groups Removed by Filter: \([0-9]*\).*/\1/p' | head -1
}

check "a mixed literal+param IN-list prunes on a generic plan (19 removed)" \
	"$(mixed_removed)" "19"

# The vectorized-aggregate guard: SAOP-derived keys are CONSERVATIVE (a
# [min,max] range, weaker than the clause), so they must never serve as the
# batch fold's exact row filter. Today the fold's eligibility gate rejects
# the clause; if anyone teaches it about SAOP without an exactness marker,
# this count goes wrong (see #715 for the same hole reached via <>).
vec_plan="$(psql_c "SET pgcolumnar.enable_ungrouped_vector_agg=on;
	EXPLAIN (ANALYZE, TIMING off, SUMMARY off) SELECT count(*) FROM t WHERE ts IN (39100, 39150);")"
check "vector-agg path engaged for the guard arm (plan shape)" \
	"$(grep -c 'Columnar Vectorized Aggregates' <<<"$vec_plan")" "1"
# The no/yes pair proves the CLAUSE gate is what excludes the SAOP, not some
# other eligibility rule: the same aggregate over the equivalent exact range
# quals folds, so the fold is reachable for this query shape and only the
# IN-list is refused. If a later change makes count(*)-with-qual take the row
# path for another reason, the yes-arm goes red and the guard is known dead.
check "the batch fold refuses the IN-list (Batch Fold: no)" \
	"$(grep -c 'Columnar Batch Fold: no' <<<"$vec_plan")" "1"
check "the batch fold accepts the equivalent exact range quals (Batch Fold: yes)" \
	"$(psql_c "SET pgcolumnar.enable_ungrouped_vector_agg=on;
		EXPLAIN (ANALYZE, TIMING off, SUMMARY off)
		SELECT count(*) FROM t WHERE ts >= 39100 AND ts <= 39150;" \
		| grep -c 'Columnar Batch Fold: yes')" "1"
check "vector-agg count over an IN-list is exact (conservative keys not folded)" \
	"$(psql_c "SET pgcolumnar.enable_ungrouped_vector_agg=on;
		SELECT count(*) FROM t WHERE ts IN (39100, 39150);" | tail -1)" "2"

# A correlated PARAM_EXEC array (LATERAL, array from the outer row) changes per
# rescan and must not be frozen. The two thr rows land in DIFFERENT chunk
# groups with different cardinalities (39100 in the last group; 100 and 200 in
# the first), so a builder that froze the first rescan's array would prune the
# other rescan's group away and the sum would drop below 1 + 2 = 3.
check "a correlated PARAM_EXEC array is not mis-pruned (results stay correct)" \
	"$(q 'SELECT sum(c) FROM thr, LATERAL (SELECT count(*) c FROM t WHERE t.ts = ANY (thr.lo)) s;')" \
	"3"

# --- array keys have one dedicated arm; all other special flags are refused --
#
# A SEARCHARRAY key holds an array in sk_argument, so it must reach the dedicated
# deconstruction arm rather than the scalar compareValue path.
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/src"

check "premise: plain-key special flags have one reject expression" \
	"$(grep -c 'key->sk_flags & (SK_ISNULL' "$SRC/columnar_reader.c")" "1"

# From the `if (key->sk_flags & (` line through the closing `))`, comments
# excluded by construction: the range starts at the `if`, so prose above it
# cannot be captured.
guard="$(awk '/if \(key->sk_flags & \(/,/\)\)/' "$SRC/columnar_reader.c" | tr -d ' \t\n')"

check "premise: the reject expression was extracted, not blank" \
	"$([ -n "$guard" ] && [ "${guard#*sk_flags}" != "$guard" ] && echo yes \
		|| echo "extracted=<${guard:-empty}>")" "yes"

# Membership, not adjacency. The flag list is a SET; asserting the order of it
# would redden on a harmless reflow and would assert more than the code means.
for _f in SK_ISNULL SK_ROW_HEADER SK_ROW_MEMBER SK_ROW_END SK_SEARCHNULL \
		  SK_SEARCHNOTNULL SK_ORDER_BY; do
	check "the predicate builder's reject expression contains $_f" \
		"$(case "$guard" in *"$_f"*) echo yes ;; *) echo "absent from <$guard>" ;; esac)" "yes"
done

check "SK_SEARCHARRAY reaches the dedicated array predicate arm" \
	"$(grep -c 'out\[n\]\.searchArray = (key->sk_flags & SK_SEARCHARRAY)' \
		"$SRC/columnar_reader.c")" "1"

check "backend alive" "$(q 'SELECT 1;')" "1"

pgc_summary
