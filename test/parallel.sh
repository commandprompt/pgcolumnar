#!/usr/bin/env bash
#
# pgColumnar parallel scan suite (gap 23).
#
# Forces a parallel plan and checks that (1) the planner puts a Gather over a
# parallel columnar scan, and (2) results are identical to the heap oracle
# whether the scan runs parallel or serial. Several backends claim distinct
# stripes from a shared counter, so correctness under concurrent claiming is the
# property.
#
# Usage:  test/parallel.sh [PG_CONFIG]
#
# Written fresh for pgColumnar; it does not reuse any upstream test file.

set -uo pipefail

. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# Many stripes so parallel workers have distinct work to claim.
make_pair "id int, k int, v bigint, t text"
q "SELECT pgcolumnar.set_options('t_col', chunk_group_row_limit => 1000, stripe_row_limit => 2000);" >/dev/null
load_pair "SELECT g, g%100, g::bigint*2, 'r'||g FROM generate_series(1,50000) g"
# 50000 rows at 2000 a row group is 25 groups. With one group there is nothing to
# share: a claim that skips every other group returns the same rows as a correct one,
# and with the writer ignoring stripe_row_limit main's shell passed 10 of 10 under
# exactly that claim. So the count is asserted, not assumed.
check_num "par table has 25 row groups to claim" "$(stripe_count)" "25"

setg() { q "ALTER DATABASE $PGC_DB SET $1 = $2;" >/dev/null; }

# Force parallelism.
setg max_parallel_workers_per_gather 4
setg parallel_setup_cost 0
setg parallel_tuple_cost 0
setg min_parallel_table_scan_size 0

# A row-returning scan should plan a Gather over a parallel PgColumnarScan.
plan="$(q "EXPLAIN (COSTS off) SELECT id, v FROM t_col WHERE k = 5;")"
check "parallel plan has Gather"        "$(echo "$plan" | grep -c -i 'Gather')" "1"
# The full node name as EXPLAIN prints it, not a substring of it. A bare
# 'ColumnarScan' also matches 'PgColumnarScan', so this check passed both before
# and after the #428 rename without ever asserting which name was in the plan.
check "parallel plan has PgColumnarScan" \
	"$(echo "$plan" | grep -c 'Custom Scan (PgColumnarScan)')" "1"

# The queries, named once, so the plan checks below read the queries the
# comparisons ran and not a copy that can drift from them.
Q_FILTERED="SELECT id, v, t FROM %T WHERE k = 7"
Q_RANGE="SELECT id FROM %T WHERE k < 30"
Q_SAMPLE="SELECT * FROM %T WHERE id % 997 = 0"
Q_COUNT="SELECT count(*), sum(v) FROM %T WHERE k < 50"
# Row-returning, not count(*). A bare count(*) is answered by the vectorized
# aggregate, which plans no Gather, so this check ran serially and stayed green
# with every participant reading every row group.
Q_NOFILTER="SELECT id FROM %T"

# plans_with GATHER|NOGATHER QUERY... -> how many of the queries, on t_col, plan a
# PgColumnarScan with (or without) a Gather. A query whose EXPLAIN fails counts
# for neither, so a dead server cannot satisfy either reading.
plans_with() {
	local want="$1" n=0 tmpl plan
	shift
	for tmpl in "$@"; do
		plan="$(q "EXPLAIN (COSTS off) ${tmpl//%T/t_col}")"
		[[ "$plan" == *'Custom Scan (PgColumnarScan)'* ]] || continue
		if [[ "$plan" == *Gather* ]]; then
			[ "$want" = GATHER ] && n=$((n + 1))
		else
			[ "$want" = NOGATHER ] && n=$((n + 1))
		fi
	done
	echo "$n"
}

# Results must match the heap oracle under the parallel plan.
diff_query "par filtered scan"  "$Q_FILTERED"
diff_query "par range scan"     "$Q_RANGE"
diff_query "par whole sample"   "$Q_SAMPLE"
diff_query "par count"          "$Q_COUNT"
diff_query "par no-filter"      "$Q_NOFILTER"
# A "par" check proves nothing about the parallel scan unless its query ran under
# one. Measured on main: "par no-filter" planned no Gather.
check_num "par queries all plan a Gather" \
	"$(plans_with GATHER "$Q_FILTERED" "$Q_RANGE" "$Q_SAMPLE" "$Q_COUNT" "$Q_NOFILTER")" "5"

# The same queries, forced serial, must give the same answers.
setg max_parallel_workers_per_gather 0
diff_query "serial filtered scan" "$Q_FILTERED"
diff_query "serial range scan"    "$Q_RANGE"
diff_query "serial count"         "$Q_COUNT"
# setg swallows its error, so a setting that did not take leaves these three
# running in parallel, and they pass as "serial" without a serial scan.
check_num "serial queries plan no Gather" \
	"$(plans_with NOGATHER "$Q_FILTERED" "$Q_RANGE" "$Q_COUNT")" "3"

q "ALTER DATABASE $PGC_DB RESET ALL;" >/dev/null

pgc_summary
