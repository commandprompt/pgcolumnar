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

# ARMS, NOT PREMISES (#1339, #1236). Which scan the planner picks, whether it
# gathers and with how many workers are decided by this extension's own paths, so a
# change here can redden each one: the scan renamed in EXPLAIN reddens the three
# "columnar scan" arms, a partial path planned with one worker reddens "has Gather",
# and one planned with workers + 1 reddens "uses two workers".
check "the serial plan is a columnar scan" \
	"$(echo "$serial_plan" | grep -c "Custom Scan (PgColumnarScan)")" "1"

check "premise: the serial plan has no Gather" \
	"$(echo "$serial_plan" | grep -c "Gather")" "0"

check "the parallel plan has Gather" \
	"$(echo "$par_plan" | grep -c "Gather")" "1"

check "the parallel plan uses two workers" \
	"$(echo "$par_plan" | grep -oE "Workers Planned: [0-9]+" | head -1 | grep -oE "[0-9]+")" "2"

check "the parallel plan is a columnar scan" \
	"$(echo "$par_plan" | grep -c "Custom Scan (PgColumnarScan)")" "1"

# CARRIES BOTH. Collapsed to yes/no this said one of two was not positive and
# nothing about which, or by how much (#1164). Single quotes round the awk program
# so the strings need no escaping.
check "premise: both scans have a positive run cost" \
	"$(awk -v s="$s_run" -v p="$p_run" \
		'BEGIN{ print (s>0 && p>0) ? "yes" : "serial " s ", parallel " p }')" "yes"

# ONE DEFINITION OF THE BOUND. Writing 1.35 in the condition and again in the
# message lets a future edit move the test and leave the message describing the
# old threshold, which is the same class of defect as an arm that does not carry
# its measurement: the reader is told a number that nothing checks.
IO_BOUND=1.35

# On the unfixed path the ratio is 2.000 (whole run / 2 workers). Core leaves
# I/O whole, so with CPU terms zeroed the ratio stays near 1. 1.35 is
# unreachable by dividing the whole run, and reachable only if I/O remains.
# CARRIES THE RATIO. "halved" named the verdict and threw away every operand
# that produced it, so a red said the bound was crossed and not by how much or
# from what (#1164). Single quotes round the awk program so the strings need no
# escaping, as in the premise above.
check "an I/O-dominated parallel scan is not priced at serial/workers" \
	"$(awk -v r="$ratio" -v s="$s_run" -v p="$p_run" -v b="$IO_BOUND" \
		'BEGIN{ print (r < b) ? "io-kept" : \
		       sprintf("ratio %.3f at or above %s (serial %s, parallel %s)", r, b, s, p) }')" \
	"io-kept"

# ---- the same property with the leader participating, which is the default ----
#
# EVERYTHING ABOVE RUNS WITH parallel_leader_participation OFF. That makes the
# divisor exactly the worker count and leaves the
# `if (parallel_leader_participation)` arm of pgcolumnar_parallel_divisor
# unexecuted -- so the copied heuristic was covered only in the configuration
# nobody runs. The GUC defaults ON.
#
# Reported in review of #1065.
setg parallel_leader_participation on
par_plan_on="$(explain_scan 2)"

# The row estimate is the observable that proves the branch ran: the partial path
# divides rel->rows by the SAME divisor, so leader-on and leader-off cannot agree.
# With two workers the divisor is 2 against 2 + (1 - 0.3*2) = 2.4, which is a
# visible difference in the plan rather than an inference about the code.
scan_rows() {
	awk '/Custom Scan \(PgColumnarScan\)/ {
		if (match($0, /rows=[0-9]+/)) {
			print substr($0, RSTART + 5, RLENGTH - 5)
			exit
		}
	}' <<-EOF
	$1
	EOF
}

rows_off="$(scan_rows "$par_plan")"
rows_on="$(scan_rows "$par_plan_on")"

check "the leader-on plan is still a parallel columnar scan" \
	"$(printf '%s' "$par_plan_on" | grep -c 'Custom Scan (PgColumnarScan)')" "1"

check "premise: both leader settings produced a row estimate to compare" \
	"$([ -n "$rows_off" ] && [ -n "$rows_on" ] && echo yes || echo no)" "yes"

# If this ever reports "same", the leader-participation branch did not run and
# every assertion below it is about the wrong divisor.
check "the leader-participation branch changes the divisor" \
	"$([ "$rows_off" != "$rows_on" ] && echo differs || echo same)" "differs"

p_pair_on="$(scan_cost_pair "$par_plan_on")"
p_start_on="${p_pair_on%% *}"
p_total_on="${p_pair_on##* }"
p_run_on="$(awk -v t="$p_total_on" -v s="$p_start_on" "BEGIN{ print t-s }")"
ratio_on="$(awk -v s="$s_run" -v p="$p_run_on" \
	"BEGIN{ if (p<=0) print 0; else printf \"%.3f\", s/p }")"

echo "-- leader on: parallel Custom Scan cost=$p_start_on..$p_total_on run=$p_run_on ratio=$ratio_on"
echo "-- rows: leader off=$rows_off leader on=$rows_on"

# A DIFFERENCE OF TWO COSTS IS SIGNED, so "no" covered both a degenerate plan
# (total == startup) and a cost-model inversion (total < startup) -- different
# defects (#1164).
check "premise: the leader-on parallel scan has a positive run cost" \
	"$(awk -v p="$p_run_on" -v t="$p_total_on" -v s="$p_start_on" \
		'BEGIN{ print (p>0) ? "yes" : "run cost " p " (total " t ", startup " s ")" }')" "yes"

# The bound is the same as the leader-off arm: dividing the WHOLE run would put
# the ratio at the divisor, and the divisor is larger here, so a regression is if
# anything easier to see in this configuration.
check "an I/O-dominated parallel scan is not priced at serial/workers with the leader participating" \
	"$(awk -v r="$ratio_on" -v s="$s_run" -v p="$p_run_on" -v b="$IO_BOUND" \
		'BEGIN{ print (r < b) ? "io-kept" : \
		       sprintf("ratio %.3f at or above %s (serial %s, parallel %s)", r, b, s, p) }')" \
	"io-kept"

setg parallel_leader_participation off

pgc_summary
