#!/usr/bin/env bash
#
# pgColumnar: the vectorized grouped aggregate sizes its hash table from the
# group estimate the planner already made, instead of growing it from nothing
# (#403 item 6).
#
# The table is open-addressing, starts at capacity 0, and doubles whenever the
# live count reaches 70% of capacity. A query with 200,000 groups therefore walks
# 1024, 2048, ... , 524288, and every step rehashes every live entry into the new
# array. Measured on 2,000,000 rows before the change: 10 allocations and 366,285
# entries rehashed.
#
# WHAT IS COUNTED, and why it is not the resize count. The first instrument here
# counted resizes, and it flattered the change: sizing removed 7 of 10 resizes
# while removing only about a quarter of the rehashing, because the work is
# dominated by the last two steps and those are the ones a low estimate leaves
# behind. Entries rehashed is the work; the resize count is kept only as a
# diagnostic. A saving must be measured as work removed, not as steps removed.
#
# WHY n_distinct IS DECLARED rather than sampled. ANALYZE's n_distinct estimator
# is a sample, and on this shape two runs of the same fixture gave 91,750 and
# 209,338 for the same 200,000 groups. Asserting against a sampled estimate makes
# the suite fail on the sample rather than on the code. The suite declares
# n_distinct so the estimate is fixed, which tests OUR sizing rather than
# PostgreSQL's estimator.
#
# WHY work_mem IS SET. The allocation is clamped by work_mem, so at the 4MB
# default a 200,000-group table cannot be sized at all and the check would pin
# the clamp rather than the sizing. The clamp gets its own arm below, where it is
# the thing under test.
#
# THE OVER-ESTIMATE ARM came out of review and is why the sizing happens on the
# first grow rather than at Begin. estimate_num_groups cannot see through a
# expression, so `GROUP BY v / 100000` over a high-cardinality column reaches
# this node with an estimate of every row against a handful of real groups.
# (The arm used date_trunc('day', ts) until #1304 bounded truncating time keys
# from the zone maps, which made that estimate right and this arm vacuous.) Sizing at Begin allocated 131,072 entries for 47 groups: 128x the
# memory of growing from nothing, to save zero rehashing, on the most ordinary
# grouping in analytics. A table that never outgrows its first 1024 entries must
# never be charged for a wrong estimate.
#
# Usage:  test/groupagg_table_sizing.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

NROWS=2000000
NGROUPS=200000
ENTRY_BYTES=32			# sizeof(PgColumnarGroupEntry) on a 64-bit build

psql_run "CREATE TABLE gts (k int, v int) USING pgcolumnar;"
psql_run "INSERT INTO gts SELECT i % $NGROUPS, i FROM generate_series(1,$NROWS) i;"
psql_run "ALTER TABLE gts ALTER COLUMN k SET (n_distinct = $NGROUPS);"
psql_run "ANALYZE gts;"

# The over-estimate fixture: a high-cardinality key that an expression collapses.
psql_run "CREATE TABLE gts_expr (ts timestamptz, v int) USING pgcolumnar;"
psql_run "INSERT INTO gts_expr SELECT '2026-01-01'::timestamptz + (i || ' seconds')::interval, i
          FROM generate_series(1,500000) i;"
psql_run "ANALYZE gts_expr;"

# The cap is raised so the gate (a tenth of it, #1292 phase 2) admits the 200,000-group
# fixture: this suite measures the hash table's sizing, not the plan choice.
ON="SET pgcolumnar.enable_group_vectorization=on; SET pgcolumnar.groupagg_max_groups=10000000;"
BIG="SET work_mem='64MB';"

explain_with() {	# $1 = extra SET, $2 = query
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq \
		-c "$ON $1 EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) $2" 2>&1
}
q1() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq \
		-c "$1" 2>&1 | tail -1
}
field() {	# $1 = plan text, $2 = EXPLAIN label
	sed -n "s/.*$2: \([0-9]*\).*/\1/p" <<<"$1" | head -1
}

PLAN="$(explain_with "$BIG" "SELECT k, sum(v) FROM gts GROUP BY k")"

# ---- premises -------------------------------------------------------------
# The grouped vectorized path is OFF by default, so "the GUC took effect" can
# silently fail and leave these numbers describing some other node. It and the
# counter check below read pgcolumnar's own plan choice and EXPLAIN output, so
# neither is a premise (#1339).
check "the vectorized grouped aggregate is the node that ran" \
	"$(grep -q 'Columnar Vectorized Group Keys' <<<"$PLAN" && echo yes \
	   || echo "no -- $(grep -m1 -oE 'HashAggregate|GroupAggregate|Custom Scan' <<<"$PLAN")")" "yes"

check "premise: it built the group count this suite is sized for" \
	"$(q1 "SELECT count(DISTINCT k) FROM gts")" "$NGROUPS"

check "the work-done counter is reported at all" \
	"$(grep -qE 'Columnar Group Table Entries Rehashed: [0-9]+' <<<"$PLAN" && echo yes || echo no)" "yes"

EST="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq \
	-c "$ON $BIG EXPLAIN (COSTS ON) SELECT k, sum(v) FROM gts GROUP BY k" 2>&1 \
	| sed -n 's/.*rows=\([0-9]*\).*width.*/\1/p' | head -1)"
check "premise: the declared n_distinct reached the planner, so the estimate is not a sample" \
	"$(awk -v e="${EST:-0}" -v t="$NGROUPS" \
	   'BEGIN { print (e >= 0.9 * t && e <= 1.1 * t) ? "accurate" : "SAMPLED (" e ")" }')" "accurate"

# ---- the check ------------------------------------------------------------
REHASHED="$(field "$PLAN" 'Columnar Group Table Entries Rehashed')"
RESIZES="$(field "$PLAN" 'Columnar Group Table Resizes')"
CAP="$(field "$PLAN" 'Columnar Group Table Capacity')"

# What growing from nothing would have cost, computed from the observed group
# count rather than pinned from a previous build: the ladder rehashes 70% of each
# capacity it leaves behind, which sums to 0.7 * (final - 1024).
LADDER="$(awk -v g="$NGROUPS" 'BEGIN {
	c = 1024; while (c * 0.7 < g) c *= 2;
	printf "%d", 0.7 * (c - 1024);
}')"
echo "-- $NGROUPS groups: $REHASHED entries rehashed over $RESIZES allocations, initial capacity $CAP"
echo "-- growing from nothing would rehash about $LADDER"

# Not "rehashes nothing": the table starts at 1024 and jumps toward the estimate,
# so a well-estimated 200,000-group query pays the first 716-entry rehash and one
# bounded jump. The bar is the work REMOVED against what the ladder would cost,
# computed from the observed group count rather than pinned from a build.
check "a sized table removes most of the rehashing (#403 item 6)" \
	"$(awk -v r="${REHASHED:-999999999}" -v l="$LADDER" \
	   'BEGIN { print (r <= l / 4) ? "removed" : "KEPT " r " of " l }')" "removed"

check "and it gets there in a few allocations, not a ladder (#403 item 6)" \
	"$([ "${RESIZES:-99}" -le 4 ] && echo "few" || echo "$RESIZES allocations")" "few"

# ---- the memory bound -----------------------------------------------------
# The table must never be larger than the real group count justifies, whatever
# the estimate said. This is the check the first version of this change failed:
# it bounded the allocation by work_mem, which is a budget unrelated to the
# query, and so allowed 131072 entries for 47 groups.
NEEDED="$(awk -v g="$NGROUPS" 'BEGIN { c = 1024; while (c * 0.7 < g) c *= 2; printf "%d", c }')"
echo "-- $NGROUPS groups need $NEEDED entries; the table allocated $CAP"

check "the table is never larger than the real group count needs (#810 review)" \
	"$([ "${CAP:-0}" -gt 0 ] && [ "${CAP:-0}" -le "$NEEDED" ] && echo "bounded" \
	   || echo "OVERSIZED ($CAP entries for $NGROUPS groups, which need $NEEDED)")" "bounded"

# ---- the over-estimate arm (review of #810) -------------------------------
EXPR_Q="SELECT v / 100000 d, count(*) FROM gts_expr GROUP BY 1"
EXPR_PLAN="$(explain_with "$BIG" "$EXPR_Q")"
EXPR_CAP="$(field "$EXPR_PLAN" 'Columnar Group Table Capacity')"
EXPR_REAL="$(q1 "SELECT count(*) FROM (SELECT v / 100000 FROM gts_expr GROUP BY 1) s")"
EXPR_EST="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq \
	-c "$ON $BIG EXPLAIN (COSTS ON) $EXPR_Q" 2>&1 | sed -n 's/.*rows=\([0-9]*\).*width.*/\1/p' | head -1)"
echo "-- expression key: planner estimate $EXPR_EST, real groups $EXPR_REAL, capacity ${EXPR_CAP:-0}"

check "premise: the planner really over-estimates an expression key, or there is nothing to guard" \
	"$(awk -v e="${EXPR_EST:-0}" -v t="${EXPR_REAL:-1}" \
	   'BEGIN { print (e >= 100 * t) ? "over-estimated" : "NOT OVER-ESTIMATED (est " e " real " t ")" }')" \
	"over-estimated"

# The table starts at 1024 and these groups fit in it, so it must never grow: a
# capacity above 1024 is the estimate inflating a table that needed nothing.
check "a wrong estimate cannot inflate a table that never needed to grow (#810 review)" \
	"$([ "${EXPR_CAP:-99999}" -le 1024 ] && echo "not inflated" \
	   || echo "INFLATED ($EXPR_CAP entries for $EXPR_REAL real groups)")" "not inflated"

# A real group count must be a positive number: an empty table reads 0 on both
# sides and agreed (measured, with gts_expr's load removed). The answer is read
# THROUGH the node, with $ON and a counted aggregate: EXPR_REAL above ran core's
# HashAggregate, so comparing it with core's count(DISTINCT) tested PostgreSQL
# only (#1596 review).
groups_or_why() { pgc_is_number "$1" && [ "$1" -gt 0 ] && echo "$1" || echo "no groups read [$1]"; }
check_text "and the expression key still answers correctly" \
	"$(groups_or_why "$(q1 "$ON $BIG SELECT count(c) FROM (SELECT v / 100000 d, count(*) c FROM gts_expr GROUP BY 1) s")")" \
	"$(q1 "SELECT count(DISTINCT v / 100000) FROM gts_expr")"

# ---- the jump bound -------------------------------------------------------
# The arm above cannot see the jump bound: 6 groups fit in the starting 1024, so
# the table never grows and no jump is taken. This one has enough real groups to
# force a grow while keeping the estimate wildly wrong, which is the only shape
# where the bound is observable.
MIN_Q="SELECT v / 60 d, count(*) FROM gts_expr GROUP BY 1"
MIN_PLAN="$(explain_with "$BIG" "$MIN_Q")"
MIN_CAP="$(field "$MIN_PLAN" 'Columnar Group Table Capacity')"
MIN_REAL="$(q1 "SELECT count(*) FROM (SELECT v / 60 FROM gts_expr GROUP BY 1) s")"
MIN_NEED="$(awk -v g="${MIN_REAL:-1}" 'BEGIN { c = 1024; while (c * 0.7 < g) c *= 2; printf "%d", c }')"
echo "-- minute key: real groups $MIN_REAL need $MIN_NEED entries; allocated ${MIN_CAP:-0}"

check "premise: this key really does force the table to grow, or the bound is untested" \
	"$([ "${MIN_REAL:-0}" -gt 716 ] && echo "grows" || echo "FITS IN 1024 ($MIN_REAL groups)")" "grows"

# With the bound a grow reaches at most 64x the proven live count; without it the
# first grow jumps straight to the estimate, which is 64x the need on this shape.
check "a grow cannot jump to a wrong estimate, only toward it (#810 review)" \
	"$(awk -v c="${MIN_CAP:-0}" -v n="${MIN_NEED:-1}" \
	   'BEGIN { print (c > 0 && c <= 8 * n) ? "bounded" : "UNBOUNDED (" c " for a need of " n ")" }')" "bounded"

# Through the node too: a grow that dropped one group in 16 lost 46 of 8334
# minute groups, and core's MIN_REAL (kept, since MIN_NEED is sized from it)
# still matched core's count(DISTINCT) (#1596 review).
check_text "and the minute key answers correctly too" \
	"$(groups_or_why "$(q1 "$ON $BIG SELECT count(c) FROM (SELECT v / 60 d, count(*) c FROM gts_expr GROUP BY 1) s")")" \
	"$(q1 "SELECT count(DISTINCT v / 60) FROM gts_expr")"

# Both answers are read THROUGH the vectorized node, with $ON. Without it they ran
# core's HashAggregate, which this suite does not size: a grow that dropped one
# group in 1024 passed all 15 checks (measured). And count(s), not count(*): with
# the sum unused, the planner pruned it and chose HashAggregate even with $ON.
check "and the answer is the same under a small work_mem" \
	"$(q1 "$ON SET work_mem='1MB'; SELECT count(s) FROM (SELECT k, sum(v) AS s FROM gts GROUP BY k) t")" "$NGROUPS"

# The oracle must hold the fixture: on an empty gts both sums were NULL and agreed
# as "" == "" (measured, with gts's load removed).
check_text "the aggregate itself is still right" \
	"$(q1 "$ON $BIG SELECT sum(s) FROM (SELECT k, sum(v) AS s FROM gts GROUP BY k) t")" \
	"$(q1 "SELECT CASE WHEN count(*) = $NROWS THEN sum(v)::numeric::text
	              ELSE 'gts holds ' || count(*) || ' rows' END FROM gts")"

pgc_summary
