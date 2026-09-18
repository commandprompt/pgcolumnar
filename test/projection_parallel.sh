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
# Independent of test/pytest/test_projection_parallel.py. Same public seam
# (EXPLAIN of a covering projection query, plus the query's count). Own table,
# own row count, own bounds. Neither file is read by the other.
#
# Usage:  test/projection_parallel.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/lib/postgresql/18/bin/pg_config}"

N=32000
LO=40
HI=220
WANT=$((HI - LO + 1))
psql_run "CREATE TABLE cvppar (ik int, val int, blob text) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('cvppar', stripe_row_limit => 1000, chunk_group_row_limit => 500);"
psql_run "SELECT pgcolumnar.add_projection('cvppar', 'byik', ARRAY['ik','val'], ARRAY['ik']);"
# Scrambled so the base layout cannot prune on ik; the covering projection is
# stored sorted on ik.
psql_run "INSERT INTO cvppar SELECT g, g % 17, md5(g::text) FROM generate_series(1, $N) g ORDER BY md5(g::text);"
psql_run "ALTER TABLE cvppar SET (parallel_workers = 4);"
psql_run "ANALYZE cvppar;"

PAR="SET parallel_setup_cost = 0;
     SET parallel_tuple_cost = 0;
     SET parallel_leader_participation = off;
     SET min_parallel_table_scan_size = 0;
     SET jit = off;
     SET pgcolumnar.enable_ungrouped_vector_agg = off;
     SET pgcolumnar.enable_group_vectorization = off;"

Q="SELECT ik, val FROM cvppar WHERE ik BETWEEN $LO AND $HI"

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

serial_plan="$(explain_cov 0 on)"
par_off_plan="$(explain_cov 4 off)"
par_on_plan="$(explain_cov 4 on)"
par_on_count="$(count_cov 4 on)"

echo "-- serial:"
printf '%s\n' "$serial_plan"
echo "-- parallel, projection off:"
printf '%s\n' "$par_off_plan"
echo "-- parallel, projection on:"
printf '%s\n' "$par_on_plan"
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

pgc_summary
