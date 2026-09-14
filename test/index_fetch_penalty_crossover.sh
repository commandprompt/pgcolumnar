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

PLAN_50="$(plan_of "EXPLAIN (COSTS OFF) SELECT sum(a) FROM ifc WHERE id <= $K_RANGE")"
echo "-- ${K_RANGE}-row range: $(printf '%s\n' "$PLAN_50" | grep -m1 -E 'Scan')"
check "a ${K_RANGE}-row correlated range uses the custom scan, not a fetching index" \
	"$(top_node "$PLAN_50")" "Custom Scan"

IDX_SUM="$(q1 "SET enable_seqscan=off; SET enable_bitmapscan=off; SET pgcolumnar.enable_custom_scan=off; SELECT sum(a) FROM ifc WHERE id <= $K_RANGE")"
CS_SUM="$(q1 "SET enable_indexscan=off; SET enable_bitmapscan=off; SELECT sum(a) FROM ifc WHERE id <= $K_RANGE")"
check "both paths return the same aggregate at $K_RANGE" "$IDX_SUM" "$CS_SUM"

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
check "the fetch penalty leaves a clustered ORDER BY on its index" \
	"$(grep -q 'Index Scan using ifc_cl_id' <<<"$PLAN_ORD" && echo yes \
		|| echo "no ($(printf '%s' "$PLAN_ORD" | grep -m1 -E 'Scan|Sort'))")" \
	"yes"

pgc_summary
