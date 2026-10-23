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

explain_count() {
	# The count arm's OWN plan (#1339): its query is not the one explain_cov shows.
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -Atq \
		-c "$PAR" \
		-c "SET max_parallel_workers_per_gather = 2;" \
		-c "SET pgcolumnar.enable_projection_scan = on;" \
		-c "EXPLAIN (COSTS OFF) SELECT count(*) FROM cvppar WHERE ik BETWEEN $LO AND $HI;" \
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
par_on_count="$(count_cov 2 on)"

# UP TO FIVE ANALYZE ATTEMPTS, STOPPING AT THE FIRST THAT SHOWS BOTH WORKERS BUSY
# (#1336). On a correct build a worker that starts late can find every row group
# already claimed: legitimate parallel behaviour, seen once in a 19beta2 cassert
# matrix (1 of 12 runs on that host) and in 0 of 44 here, idle and 8-way loaded.
# The defect the arm is named for is single BY CONSTRUCTION -- one claimer on every
# run -- so it still fails all five, while a correct build fails only if the race
# goes the wrong way five times running. The premises read the attempt that was
# used, and every attempt is printed.
attempts=""
for _try in 1 2 3 4 5; do
	par_on_ana="$(analyze_cov 2 on)"
	rows_list="$(worker_rows "$par_on_ana")"
	n_lines="$(echo "$rows_list" | grep -c . || true)"
	n_busy="$(echo "$rows_list" | awk '$1>0{n++} END{print n+0}')"
	attempts="$attempts${attempts:+; }try $_try: $(echo "$rows_list" | tr '\n' ' ')"
	[ "$n_busy" = 2 ] && break
done
echo "-- worker rows per ANALYZE attempt: $attempts"

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

# THE COUNT'S OWN PLAN (#1339). The count comes from a different query from the one
# whose shape is asserted above; if it planned serially or as a base scan, this arm
# would check nothing about the parallel covering path.
check "the parallel count is a parallel covering projection scan" \
	"$(shape "$(explain_count)")" "gather+projection"
check "a parallel covering projection returns the covering rows once" \
	"$par_on_count" "$WANT"

# ---- an I/O-dominated covering scan is not quoted at 1/N of its cost (#1209) -
#
# Raising seq_page_cost until the base relation's I/O dominates its decode CPU
# must cost the PARALLEL covering path, because core leaves disk I/O whole and
# amortises only CPU. If the partial covering total divided both, an I/O-bound
# scan would be quoted at 1/N and Gather would keep winning however expensive
# the pages became.
#
# THIS IS NOT A GUARD ON THE CLAMP, and the difference is measured rather than
# assumed. Removing `if (ioRunProj > projRun) ioRunProj = projRun;` entirely
# changes NOTHING here: every rung of the ladder below reads the same, and the
# suite passes. The arithmetic says why. At the binding point, with pre =
# ioRun*projScale > projRun:
#
#     clamped     total = startup + projRun
#     unclamped   total = startup + pre + (projRun - pre)/divisor
#
# and the difference has a sign: unclamped - clamped = (pre - projRun) *
# (1 - 1/divisor), which is strictly positive because pre > projRun IS the
# binding condition and divisor > 1 always. Removing the clamp makes the partial
# path DEARER, so the serial covering path wins either way and no plan moves. The clamp earns its
# place by keeping cpuRunProj from going negative, which is an internal quantity
# no plan exposes -- not by changing a decision.
#
# What DOES redden the second arm is amortising the I/O: rewriting the total as
# `(ioRunProj + cpuRunProj) / divisor` gives `got [gather+projection] want
# [projection-only]` at the top of the ladder.
#
# #1209 for the measurement behind this: the clamp IS reachable, at
# seq_page_cost > C/(A-B) = 2625 on a 20,000-row fixture, predicted from a fit
# of six points and then confirmed at 2048 (no, by 0.9%) and 4096 (yes).
SPC_HI=1000000
explain_cov_spc() {	# $1 = seq_page_cost
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At \
		-c "SET min_parallel_table_scan_size = 0;" \
		-c "SET parallel_setup_cost = 0;" \
		-c "SET parallel_tuple_cost = 0;" \
		-c "SET max_parallel_workers_per_gather = 2;" \
		-c "SET pgcolumnar.enable_projection_scan = on;" \
		-c "SET seq_page_cost = $1;" \
		-c "EXPLAIN (COSTS OFF) $Q;" 2>/dev/null || true
}

# THE THRESHOLD IS NOT SCALE-INVARIANT, and an earlier draft of this
# comment said it was (@jdatcmd caught it). The claim rested on two fixtures
# that agreed -- and both were 20,000 rows, so their agreement says the
# difference is insensitive to CONTENT and says nothing about N. Measured on
# three fixtures, the rung at seq_page_cost = 4096:
#
#     20,000 rows, range 300     projection-only     threshold 2625
#     32,000 rows, range 8,000   gather+projection   threshold above 4096
#     50,000 rows, range 12,100  gather+projection   threshold above 4096
#
# It GROWS with the row count. C = cpuRun * projScale scales with N while
# basePagesRead - projPages came out at 2 pages regardless, so the quotient
# rises: 2625 * 32/20 = 4200 and 2625 * 50/20 = 6560, both above 4096, which
# is what the two rungs show.
#
# So THE ARM IS SAFE BY MARGIN, NOT BY INVARIANCE. 1000000 is roughly 150x
# the largest threshold observed. Pinning a rung near a crossover would rest
# on whatever ANALYZE sampled that day; this does not.
#
# THE TWO ARMS ARE A PAIR AND NEITHER HALF IS SOUND ALONE (@jdatcmd, review).
# The second cannot separate "I/O is left whole" from "there is no parallel
# covering path at all": both read projection-only. The premise at
# seq_page_cost=1 is what excludes the second reading, so deleting it as
# redundant leaves a passing arm that proves nothing.
#
# The ladder is printed rather than summarised: a failure of the second arm has
# two possible causes -- the partial total amortising I/O, or a host where the
# covering projection is not smaller than the base's read columns so no page
# cost ever tips it -- and the rungs tell them apart.
echo "-- page-cost ladder, seq_page_cost against plan shape:"
for _spc in 1 1024 4096 $SPC_HI; do
	printf '     seq_page_cost=%-8s %s\n' "$_spc" "$(shape "$(explain_cov_spc "$_spc")")"
done

check "premise: the covering plan is parallel at the default page cost" \
	"$(shape "$(explain_cov_spc 1)")" "gather+projection"

check "a page cost that makes I/O dominate costs the parallel covering path" \
	"$(shape "$(explain_cov_spc $SPC_HI)")" "projection-only"

# NOT PREMISES (#1339, #1236). The worker count is the projection path's own
# parallel_workers: with it set to 1, Workers Launched reads 1 and both of these
# go red. A check a pgcolumnar change can redden is an arm.
check "EXPLAIN ANALYZE launched two workers" \
	"$(echo "$par_on_ana" | grep -oE 'Workers Launched: [0-9]+' | head -1 | grep -oE '[0-9]+')" "2"

check "ANALYZE printed a rows= line per launched worker" \
	"$n_lines" "2"

# THE DEFECT the plan-shape arms cannot see: Gather is present and the count
# is right when one backend claims every stripe. Sharing means both launched
# workers produced rows, in at least one of the attempts above.
check "workers share the covering projection scan, it is not a single claimer" \
	"$n_busy" "2"
# AND SHARE IT, NOT DUPLICATE IT (#1339). Two busy workers is also what two workers
# each scanning every group look like. With the leader not participating, the
# workers' rows are everything the Gather returns, so they must add up to the
# covering rows exactly once.
check "the workers' rows add up to the covering rows, each returned once" \
	"$(echo "$rows_list" | awk '{s+=$1} END{print s+0}')" "$WANT"

pgc_summary
