#!/usr/bin/env bash
#
# pgColumnar: a fetching index scan on a correlated key is priced below the
# custom scan through ~50,000 rows, while it does about 27x the work (#913).
#
# The penalty term exists for this. The measurement says it is too small. This
# suite asserts the PLAN, not a cost number: costs drift with the constants, the
# chosen node is the property. Split from #766, which closed on the opposite
# question (custom scan vs heap). Raising the custom-scan cost would enlarge the
# wrong-plan region.
#
# Independent of test/pytest/test_index_fetch_penalty_crossover.py: same public
# seam, own fixture, own observations.
#
# Usage:  test/index_fetch_penalty_crossover.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

N=1000000
K_RANGE=50000

psql_run "CREATE TABLE ifc (id int, a int, b int) USING pgcolumnar;"
psql_run "INSERT INTO ifc SELECT g, g % 10, g % 100 FROM generate_series(1,$N) g;"
psql_run "CREATE INDEX ifc_id ON ifc(id);"

# THE ESTIMATE MUST NOT BE A SAMPLE DRAW (#1168). `ANALYZE` samples 300 x the
# statistics target rows; at the default 100 that is 30,000 of these 1,000,000,
# so the estimate for `id <= 50000` moves every run. This suite asserts a CHOSEN
# NODE where the two prices are close, and the node flips when the estimate does.
# It flaked twice in two days in the gate, once on `main` itself.
#
# 300 x 3500 = 1,050,000 >= the table, so ANALYZE reads ALL of it and the
# statistics stop being a draw. That is a STRUCTURAL fix, not a wider margin:
# there is no sample left to come out differently. Measured on PG17, bisecting
# K* = the smallest range at which the custom scan wins. EVERY ROW CARRIES ITS
# OWN n, because the first version of this table put one sample size in a header
# over rows collected under different ones:
#
#      target    n    K* mean   K* sd   K* min..max   estimate at 50,000
#       100     30     46847     1067   44706..49276  47859..52486  <- flaked
#      1000     30     47163      336   46406..47812  49303..50707
#      3500     30     47109        0   47109..47109  50000 in 30 of 30
#      3000     30     47072       64   46933..47167  49906..50189  <- control
#
# 3000 IS THE CONTROL AND IT IS THE INTERESTING ROW: 300 x 3000 = 900,000 is
# just under the table, and the wobble is back at sd 64 with the estimate exact
# in 0 of 30 draws. So the determinism comes from COVERING THE TABLE and not
# from a large target, which is the thing to keep if this fixture ever grows --
# RAISE THE TARGET WITH N, or the flake returns silently.
#
# Cost of the whole fix, PG17: ANALYZE 0.07s -> 0.41s, histogram 101 -> 3501
# bounds. K* is 47,109, so the 50,000 this suite asserts at sits 2,891 rows
# above the crossover with no variance under it.
psql_run "ALTER TABLE ifc ALTER COLUMN id SET STATISTICS 3500;"
psql_run "ANALYZE ifc;"

q1() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq \
		-c "$1" 2>&1 | tail -1
}

SETS="SET enable_seqscan=off;
SET enable_bitmapscan=off;
SET enable_indexonlyscan=off;
SET max_parallel_workers_per_gather=0;
SET jit=off;
SET pgcolumnar.enable_vectorization=off;
SET pgcolumnar.enable_ungrouped_vector_agg=off;
SET pgcolumnar.enable_group_vectorization=off;"

plan_of() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq \
		-c "$SETS $1" 2>&1
}

top_node() {
	# First scan/join node in COSTS OFF text, which is the chosen path.
	grep -m1 -oE 'Index Scan|Index Only Scan|Bitmap Heap Scan|Custom Scan|Seq Scan' <<<"$1"
}

check "premise: the table holds all $N rows" "$(q1 "SELECT count(*) FROM ifc")" "$N"
check "premise: the btree on id exists" \
	"$(q1 "SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE i.indrelid = 'ifc'::regclass AND c.relname = 'ifc_id'")" \
	"1"

PLAN_PT="$(plan_of "EXPLAIN (COSTS OFF) SELECT sum(a) FROM ifc WHERE id = 1")"
echo "-- point lookup: $(printf '%s\n' "$PLAN_PT" | grep -m1 -E 'Scan')"
check "a selective point lookup still uses the index" \
	"$(top_node "$PLAN_PT")" "Index Scan"

# PREMISE, AND IT IS THE WHOLE OF #1168: the arm below asserts a CHOSEN NODE at
# a range where the two prices are close, so it is only evidence if the row
# estimate feeding those prices is the same number every run. Asserted rather
# than assumed -- a sampled estimate and an exact one look identical here until
# the day the node flips.
EST_AT_K="$(plan_of "EXPLAIN SELECT sum(a) FROM ifc WHERE id <= $K_RANGE" \
	| grep -m1 -E 'Index Scan|Custom Scan|Seq Scan' | grep -oE 'rows=[0-9]+' | cut -d= -f2)"
# NOT A PREMISE (#1339): the estimate is the scan path pgcolumnar prices, so a custom
# scan that misreports its rows reddens it.
check "the ${K_RANGE}-row estimate is exact, so no arm here rests on a sample draw" \
	"${EST_AT_K:-none}" "$K_RANGE"

PLAN_50="$(plan_of "EXPLAIN (COSTS OFF) SELECT sum(a) FROM ifc WHERE id <= $K_RANGE")"
echo "-- ${K_RANGE}-row range: $(printf '%s\n' "$PLAN_50" | grep -m1 -E 'Scan')"
check "a ${K_RANGE}-row correlated range uses the custom scan, not a fetching index" \
	"$(top_node "$PLAN_50")" "Custom Scan"

# BOTH PATHS AGAINST THE GENERATOR, AND THE INDEX PATH SEEN TO BE ONE (#1339). This
# compared the two sums with each other, so two failed reads or two equally wrong
# answers agreed, and nothing said the "index" path fetched by index at all. a is
# g % 10, so the sum over ids 1..K is computed from generate_series, not read back.
IDX_FORCE="SET enable_seqscan=off; SET enable_bitmapscan=off; SET pgcolumnar.enable_custom_scan=off;"
IDX_SUM="$(q1 "$IDX_FORCE SELECT sum(a) FROM ifc WHERE id <= $K_RANGE")"
CS_SUM="$(q1 "SET enable_indexscan=off; SET enable_bitmapscan=off; SELECT sum(a) FROM ifc WHERE id <= $K_RANGE")"
GEN_SUM="$(q1 "SELECT sum(g % 10) FROM generate_series(1, $K_RANGE) g")"
check_text "both paths return the same aggregate at $K_RANGE" \
	"index ${IDX_SUM:-none}, scan ${CS_SUM:-none}" "index $GEN_SUM, scan $GEN_SUM"
check "and the index-forced path really fetches by index" \
	"$(top_node "$(plan_of "$IDX_FORCE EXPLAIN (COSTS OFF) SELECT sum(a) FROM ifc WHERE id <= $K_RANGE")")" "Index Scan"

# #355: a clustered ORDER BY of the whole table must stay on the index. A
# per-row term that grows with rows costs this path off onto a Sort. The
# 50,000-row range above projects two ints; this table carries a payload so
# SELECT * is the same shape that #355 must not over-fire on.
N_ORD=300000
psql_run "CREATE TABLE ifc_cl (id int, payload text) USING pgcolumnar;"
psql_run "INSERT INTO ifc_cl SELECT g, repeat('x', 48) FROM generate_series(1,$N_ORD) g;"
psql_run "CREATE INDEX ifc_cl_id ON ifc_cl(id);"
psql_run "ANALYZE ifc_cl;"

SETS_ORD="SET max_parallel_workers_per_gather=0; SET random_page_cost=1.0;"
PLAN_ORD="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq \
	-c "$SETS_ORD EXPLAIN (COSTS OFF) SELECT * FROM ifc_cl ORDER BY id" 2>&1)"
echo "-- clustered ORDER BY: $(printf '%s\n' "$PLAN_ORD" | grep -m1 -E 'Scan|Sort')"
# THIS ARM IS THE CAP'S REMOVAL PROOF, and nothing else in the suite is.
#
# The per-row term is capped at half a group. Nothing here NAMES the cap, so a
# reader asking "is that cap load-bearing, or can it be simplified away?" finds
# no arm mentioning it and concludes nothing protects it. That conclusion is
# wrong, and it was reached in writing during review of this PR before anyone
# mutated the code.
#
# Measured, deleting the cap and leaving everything else:
#
#     as written   6 passed + 0 failed
#     uncapped     FAIL  the fetch penalty leaves a clustered ORDER BY on its
#                        index: got [no (Sort)] want [yes]
#
# per_row = cpu_tuple_cost * rows * decodeUnits grows with the whole table on an
# ordered scan, so this IS the saturation case: uncapped it costs the ordered
# scan off its index, which is the #355 regression the cap exists to prevent.
#
# The arm above it is the other side. Together they bound the cap in both
# directions -- too small and the 50,000-row range stays on the index, too large
# and the ordered scan leaves it. Removing either leaves the cap pinned on one
# side only, which is the easy miss.
check "the fetch penalty leaves a clustered ORDER BY on its index" \
	"$(grep -q 'Index Scan using ifc_cl_id' <<<"$PLAN_ORD" && echo yes \
		|| echo "no ($(printf '%s' "$PLAN_ORD" | grep -m1 -E 'Scan|Sort'))")" \
	"yes"

pgc_summary
