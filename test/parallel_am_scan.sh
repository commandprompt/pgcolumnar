#!/usr/bin/env bash
#
# pgColumnar: a table-AM parallel scan must share work across workers.
#
# With the custom scan off, Parallel Seq Scan goes through the AM. The AM
# used to treat phs_nallocated as a first-wins flag: one backend claimed
# the whole scan and the others marked themselves exhausted. Workers
# launched, one backend read.
#
# The custom-scan path already claims distinct row groups from a shared
# counter. This suite pins the AM path to the same property, via EXPLAIN
# ANALYZE worker rows -- not internals. Leader participation is off so
# the two launched workers are the claimers under test, not the leader.
# Many small row groups keep both workers busy before either finishes
# the table.
#
# Independent of test/pytest/test_parallel_am_scan.py: same public seam, own
# fixture, own observations.
#
# Usage:  test/parallel_am_scan.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

N=50000
psql_run "CREATE TABLE pam (id int, k int, payload text) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('pam', chunk_group_row_limit => 100, stripe_row_limit => 1000);"
psql_run "INSERT INTO pam SELECT g, g%23, md5(g::text) FROM generate_series(1,$N) g;"
psql_run "ALTER TABLE pam SET (parallel_workers = 2);"
psql_run "ANALYZE pam;"

setg() { q "ALTER DATABASE $PGC_DB SET $1 = $2;" >/dev/null; }
setg pgcolumnar.enable_custom_scan off
setg parallel_setup_cost 0
setg parallel_tuple_cost 0
setg min_parallel_table_scan_size 0
setg jit off
setg parallel_leader_participation off

explain_text() {
	# $1 = max_parallel_workers_per_gather
	# $2 = ANALYZE or empty
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -Atq \
		-c "SET max_parallel_workers_per_gather = $1;" \
		-c "EXPLAIN (COSTS OFF, VERBOSE $2) SELECT id FROM pam;"
}

serial_plan="$(explain_text 0 "")"
par_plan="$(explain_text 2 "")"
par_ana="$(explain_text 2 ", ANALYZE, TIMING OFF, SUMMARY OFF")"

echo "-- serial plan --"
echo "$serial_plan"
echo "-- parallel plan --"
echo "$par_plan"
echo "-- parallel analyze --"
echo "$par_ana"

# Per-worker actual rows from ANALYZE text. A worker that produced nothing
# still prints rows=0, so a missing line is not a zero -- it is no measurement.
worker_rows() {
	echo "$1" | grep -oE 'Worker [0-9]+:.*rows=[0-9]+' \
		| grep -oE 'rows=[0-9]+' | grep -oE '[0-9]+'
}

rows_list="$(worker_rows "$par_ana")"
n_lines="$(echo "$rows_list" | grep -c . || true)"
n_busy="$(echo "$rows_list" | awk '$1>0{n++} END{print n+0}')"
echo "-- worker rows: $(echo "$rows_list" | tr "\n" " ") busy=$n_busy lines=$n_lines"

check "premise: the table holds every inserted row" \
	"$(q "SELECT count(*) FROM pam")" "$N"

check "premise: with the custom scan off the serial plan is a Seq Scan" \
	"$(echo "$serial_plan" | grep -c 'Seq Scan')" "1"

check "premise: the serial plan is not a columnar custom scan" \
	"$(echo "$serial_plan" | grep -c 'Custom Scan')" "0"

check "premise: the parallel plan has Gather" \
	"$(echo "$par_plan" | grep -c 'Gather')" "1"

check "premise: the parallel plan uses two workers" \
	"$(echo "$par_plan" | grep -oE 'Workers Planned: [0-9]+' | head -1 | grep -oE '[0-9]+')" "2"

check "premise: the parallel plan is still a Seq Scan, not a custom scan" \
	"$(echo "$par_ana" | grep -c 'Seq Scan')" "1"

check "premise: EXPLAIN ANALYZE launched two workers" \
	"$(echo "$par_ana" | grep -oE 'Workers Launched: [0-9]+' | head -1 | grep -oE '[0-9]+')" "2"

check "premise: ANALYZE printed a rows= line per launched worker" \
	"$n_lines" "2"

serial_cnt="$(q "SET max_parallel_workers_per_gather = 0; SELECT count(*) FROM pam;" | grep -v '^SET$' | tail -1)"
par_cnt="$(q "SET max_parallel_workers_per_gather = 2; SELECT count(*) FROM pam;" | grep -v '^SET$' | tail -1)"
check "a parallel table-AM scan returns the same row count as serial" \
	"$par_cnt" "$serial_cnt"

# THE DEFECT: one backend's rows=N and every other worker's rows=0.
# Sharing means both launched workers produced rows.
check "workers share the table-AM scan, it is not a single claimer" \
	"$n_busy" "2"

pgc_summary
