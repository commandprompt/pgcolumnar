#!/usr/bin/env bash
#
# pgColumnar: a parallel custom scan must not divide I/O by the worker count.
#
# The partial path prices itself as serial_startup + (serial_run / workers).
# Core seqscan divides CPU only and leaves disk I/O whole (costsize.c: "the disk
# run cost cannot be amortized at all"). Dividing the whole run quotes an
# I/O-dominated scan at 1/N of its serial cost, so Gather wins against honestly
# costed alternatives.
#
# This suite pins the PLANNER number, not a runtime. Costs move with the
# constants; the property is that an I/O-only run is not halved when two
# workers are granted. Independent of test/pytest/test_parallel_scan_cost.py:
# same public seam (EXPLAIN of a columnar scan), own fixture, own observations.
#
# Usage:  test/parallel_scan_cost.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

N=40000
psql_run "CREATE TABLE psc (id int, k int, payload text) USING pgcolumnar;"
psql_run "INSERT INTO psc SELECT g, g%17, md5(g::text) FROM generate_series(1,$N) g;"
psql_run "ALTER TABLE psc SET (parallel_workers = 2);"
psql_run "ANALYZE psc;"

# Session GUCs that isolate the I/O term: seq_page_cost is raised so the
# serial run is mostly pages. Dividing the whole run by 2 then halves I/O;
# leaving I/O whole leaves the run almost unchanged. CPU terms stay at their
# defaults so the parallel path is still a little cheaper than serial and
# Gather still wins -- zeroing CPU made the two paths equal and the planner
# declined Gather, which hid the number this suite exists to read.
# parallel_leader_participation is off so the divisor is the worker count.
setg() { q "ALTER DATABASE $PGC_DB SET $1 = $2;" >/dev/null; }
setg seq_page_cost 100
setg parallel_setup_cost 0
setg parallel_tuple_cost 0
setg parallel_leader_participation off
setg min_parallel_table_scan_size 0
setg jit off

explain_scan() {
	# $1 = max_parallel_workers_per_gather
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -Atq \
		-c "SET max_parallel_workers_per_gather = $1;" \
		-c "EXPLAIN (COSTS ON) SELECT id, k, payload FROM psc;"
}

serial_plan="$(explain_scan 0)"
par_plan="$(explain_scan 2)"

scan_cost_pair() {
	# startup and total of the first Custom Scan (PgColumnarScan) line.
	echo "$1" | grep -F "Custom Scan (PgColumnarScan)" | head -1 \
		| grep -oE "cost=[0-9.]+\\.\\.[0-9.]+" | head -1 \
		| sed -E "s/cost=([0-9.]+)\\.\\.([0-9.]+)/\\1 \\2/"
}

s_pair="$(scan_cost_pair "$serial_plan")"
p_pair="$(scan_cost_pair "$par_plan")"
s_start="${s_pair%% *}"
s_total="${s_pair##* }"
p_start="${p_pair%% *}"
p_total="${p_pair##* }"

s_run="$(awk -v t="$s_total" -v s="$s_start" "BEGIN{ print t-s }")"
p_run="$(awk -v t="$p_total" -v s="$p_start" "BEGIN{ print t-s }")"
ratio="$(awk -v s="$s_run" -v p="$p_run" "BEGIN{ if (p<=0) print 0; else printf \"%.3f\", s/p }")"

echo "-- serial Custom Scan cost=$s_start..$s_total run=$s_run"
echo "-- parallel Custom Scan cost=$p_start..$p_total run=$p_run ratio=$ratio"

check "premise: the table holds every inserted row" \
	"$(q "SELECT count(*) FROM psc")" "$N"

check "premise: the serial plan is a columnar scan" \
	"$(echo "$serial_plan" | grep -c "Custom Scan (PgColumnarScan)")" "1"

check "premise: the serial plan has no Gather" \
	"$(echo "$serial_plan" | grep -c "Gather")" "0"

check "premise: the parallel plan has Gather" \
	"$(echo "$par_plan" | grep -c "Gather")" "1"

check "premise: the parallel plan uses two workers" \
	"$(echo "$par_plan" | grep -oE "Workers Planned: [0-9]+" | head -1 | grep -oE "[0-9]+")" "2"

check "premise: the parallel plan is a columnar scan" \
	"$(echo "$par_plan" | grep -c "Custom Scan (PgColumnarScan)")" "1"

check "premise: both scans have a positive run cost" \
	"$(awk -v s="$s_run" -v p="$p_run" "BEGIN{ print (s>0 && p>0) ? \"yes\" : \"no\" }")" "yes"

# On the unfixed path the ratio is 2.000 (whole run / 2 workers). Core leaves
# I/O whole, so with CPU terms zeroed the ratio stays near 1. 1.35 is
# unreachable by dividing the whole run, and reachable only if I/O remains.
check "an I/O-dominated parallel scan is not priced at serial/workers" \
	"$(awk -v r="$ratio" "BEGIN{ print (r < 1.35) ? \"io-kept\" : \"halved\" }")" "io-kept"

pgc_summary
