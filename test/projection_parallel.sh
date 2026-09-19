#!/usr/bin/env bash
#
# pgColumnar: a covering projection scan must be able to run in parallel.
#
# PgColumnarSetRelPathlist offers a covering projection as a serial CustomPath
# (parallel_aware = false, parallel_safe = false) and a parallel base scan as a
# partial path with no projection name. Those cannot both be true of one plan:
# either Gather wins and the projection is dropped, or the serial projection
# wins and the workers are dropped. A covering query under parallel settings
# should be both.
#
# Gather in the plan is not enough. A partial path that no worker claims a
# stripe from still looks parallel, and the leader (or a single claimer)
# still returns the covering rows once. The load-bearing arms are EXPLAIN
# ANALYZE: two workers launched, and both produced rows. Same public seam
# parallel_am_scan already uses for the table-AM claimer.
#
# Independent of test/pytest/test_projection_parallel.py. Same public seam
# (EXPLAIN / EXPLAIN ANALYZE of a covering projection query, plus the
# query's count). Own table, own row count, own bounds. Neither file is
# read by the other.
#
# Usage:  test/projection_parallel.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/lib/postgresql/18/bin/pg_config}"

N=32000
LO=40
HI=8039
WANT=$((HI - LO + 1))
psql_run "CREATE TABLE cvppar (ik int, val int, blob text) USING pgcolumnar;"
# The covering projection is stored sorted on ik, so BETWEEN LO AND HI
# occupies consecutive groups. 181 matching rows at the 100-row floor
# is two groups; 2000 matching rows is twenty. Both geometries still
# let one worker finish the range on PG15 before the other claimed.
# 8000 matching rows (eighty groups) plus 12x md5 decode work is
# what kept both workers busy on every major this run measured.
psql_run "SELECT pgcolumnar.set_options('cvppar', stripe_row_limit => 1000, chunk_group_row_limit => 100);"
# Include blob so each claimed group has real decode work. A two-int
# projection finished so fast that one worker could claim every group
# before the other started; Gather and the count still passed.
psql_run "SELECT pgcolumnar.add_projection('cvppar', 'byik', ARRAY['ik','val','blob'], ARRAY['ik']);"
# Scrambled so the base layout cannot prune on ik; the covering projection is
# stored sorted on ik.
psql_run "INSERT INTO cvppar SELECT g, g % 17, repeat(md5(g::text), 12) FROM generate_series(1, $N) g ORDER BY md5(g::text);"
# Two workers, matching parallel_am_scan: both launched workers must produce
# rows. Four workers on this fixture can leave one idle, which would make
# "every launched worker produced rows" a test of scheduling rather than of
# the shared claim.
psql_run "ALTER TABLE cvppar SET (parallel_workers = 2);"
psql_run "ANALYZE cvppar;"

PAR="SET parallel_setup_cost = 0;
     SET parallel_tuple_cost = 0;
     SET parallel_leader_participation = off;
     SET min_parallel_table_scan_size = 0;
     SET jit = off;
     SET pgcolumnar.enable_ungrouped_vector_agg = off;
     SET pgcolumnar.enable_group_vectorization = off;"

Q="SELECT ik, val, blob FROM cvppar WHERE ik BETWEEN $LO AND $HI"

explain_cov() {
	# $1 = max_parallel_workers_per_gather
	# $2 = on|off for pgcolumnar.enable_projection_scan
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -Atq \
		-c "$PAR" \
		-c "SET max_parallel_workers_per_gather = $1;" \
		-c "SET pgcolumnar.enable_projection_scan = $2;" \
		-c "EXPLAIN (COSTS OFF) $Q;" \
		| grep -v '^SET$'
}

analyze_cov() {
	# $1 = max_parallel_workers_per_gather
	# $2 = on|off
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -Atq \
		-c "$PAR" \
		-c "SET max_parallel_workers_per_gather = $1;" \
		-c "SET pgcolumnar.enable_projection_scan = $2;" \
		-c "EXPLAIN (COSTS OFF, VERBOSE, ANALYZE, TIMING OFF, SUMMARY OFF) $Q;" \
		| grep -v '^SET$'
}

count_cov() {
	# $1 = max_parallel_workers_per_gather
	# $2 = on|off
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -Atq \
		-c "$PAR" \
		-c "SET max_parallel_workers_per_gather = $1;" \
		-c "SET pgcolumnar.enable_projection_scan = $2;" \
		-c "SELECT count(*) FROM cvppar WHERE ik BETWEEN $LO AND $HI;" \
		| grep -v '^SET$'
}

shape() {
	local plan="$1"
	local g p
	g=$(printf '%s\n' "$plan" | grep -c -i 'Gather' || true)
	p=$(printf '%s\n' "$plan" | grep -c 'Columnar Projection: byik' || true)
	if [ "$g" -ge 1 ] && [ "$p" -ge 1 ]; then
		echo gather+projection
	elif [ "$g" -ge 1 ]; then
		echo gather-only
	elif [ "$p" -ge 1 ]; then
		echo projection-only
	else
		echo neither
	fi
}

# Per-worker actual rows from ANALYZE text. A worker that produced nothing
# still prints rows=0, so a missing line is not a zero -- it is no measurement.
worker_rows() {
	echo "$1" | grep -oE 'Worker [0-9]+:.*rows=[0-9]+' \
		| grep -oE 'rows=[0-9]+' | grep -oE '[0-9]+'
}

serial_plan="$(explain_cov 0 on)"
par_off_plan="$(explain_cov 2 off)"
par_on_plan="$(explain_cov 2 on)"
par_on_ana="$(analyze_cov 2 on)"
par_on_count="$(count_cov 2 on)"

rows_list="$(worker_rows "$par_on_ana")"
n_lines="$(echo "$rows_list" | grep -c . || true)"
n_busy="$(echo "$rows_list" | awk '$1>0{n++} END{print n+0}')"

echo "-- serial:"
printf '%s\n' "$serial_plan"
echo "-- parallel, projection off:"
printf '%s\n' "$par_off_plan"
echo "-- parallel, projection on:"
printf '%s\n' "$par_on_plan"
echo "-- parallel covering analyze:"
printf '%s\n' "$par_on_ana"
echo "-- worker rows: $(echo "$rows_list" | tr "\n" " ") busy=$n_busy lines=$n_lines"
echo "-- parallel covering count=$par_on_count want=$WANT"

check "premise: the table holds every inserted row" \
	"$(q "SELECT count(*) FROM cvppar")" "$N"

check "premise: a covering projection exists" \
	"$(q "SELECT count(*) FROM pgcolumnar.projection_declaration WHERE rel = 'cvppar'::regclass AND name = 'byik'")" "1"

check "premise: a serial covering query uses the projection" \
	"$(shape "$serial_plan")" "projection-only"

check "premise: a parallel base scan is available when the projection is off" \
	"$(shape "$par_off_plan")" "gather-only"

# The defect: the covering projection path cannot be parallel, so the planner
# cannot keep both. got is gather-only or projection-only on the unfixed tree.
check "a covering projection can be a parallel scan" \
	"$(shape "$par_on_plan")" "gather+projection"

check "a parallel covering projection returns the covering rows once" \
	"$par_on_count" "$WANT"

check "premise: EXPLAIN ANALYZE launched two workers" \
	"$(echo "$par_on_ana" | grep -oE 'Workers Launched: [0-9]+' | head -1 | grep -oE '[0-9]+')" "2"

check "premise: ANALYZE printed a rows= line per launched worker" \
	"$n_lines" "2"

# THE DEFECT the plan-shape arms cannot see: Gather is present and the count
# is right when one backend claims every stripe. Sharing means both launched
# workers produced rows.
check "workers share the covering projection scan, it is not a single claimer" \
	"$n_busy" "2"

pgc_summary
