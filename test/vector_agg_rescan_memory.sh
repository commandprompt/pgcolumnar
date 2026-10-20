#!/usr/bin/env bash
#
# pgColumnar #717: the vectorized aggregates must not stack a fresh set of
# pushdown scan keys in query memory on every rescan.
#
# The scalar custom scan builds its scan keys once at Begin and reuses them. The
# vectorized aggregate paths rebuild them at Exec, so a rescan (a LATERAL or
# parameterized aggregate sub-scan) built them again, into es_query_cxt, which
# lives until ExecutorEnd. Since #704 the build of an `IN (...)` key also
# detoasts the array constant and deconstructs it into elems and nulls, so the
# growth is O(rescans x array size) rather than O(rescans).
#
# Measured before the fix on 2000 rescans with a 20000-element IN-list:
# ExecutorState peaked at 362,877,504 bytes. An independent prediction from the
# mechanism alone -- 2000 x (20000 Datums + 20000 bools) -- is about 343 MB, and
# the two agreeing is what identifies the growth as THIS defect rather than
# something else that also consumes memory. After the fix, 1,572,928 bytes.
#
# THE ASSERTION HERE IS A DIFFERENTIAL, NOT A BOUND, and deliberately so.
#
# An absolute byte limit has to be picked against one machine's block sizes and
# one fixture's shape, and it silently stops meaning anything when either moves.
# Worse, it would charge this defect for memory it does not cause: the aggregate
# rescan path grows about 655 bytes per rescan WITH OR WITHOUT an IN-list, at
# slopes that measure identical, so that part is a different leak and is filed
# separately. Subtracting a no-IN-list control removes it exactly, and leaves
# only what adding the IN-list costs.
#
# Measured with the fix reverted: 179,877 bytes per rescan with a
# 20000-element IN-list against 655 without, an excess of 179,222. A prediction
# from the mechanism alone -- one Datum and one bool per element, 20000 x 9 --
# is 180,000, and the two agreeing to 0.07% is what identifies the growth as
# THIS defect rather than something else that also consumes memory. With the fix
# the two slopes are 655 and 655, an excess of zero. The control arm reads
# byte-identical before and after (131,072 -> 2,097,152 either way), which is
# what says the fix changed only the thing it claims to.
# The instrument is pg_log_backend_memory_contexts() called from a second
# session against the running backend, with the MAX taken across samples, so
# the reading does not depend on catching one particular instant.
#
# Usage:  test/vector_agg_rescan_memory.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

q() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "$1" 2>&1
}
q1() { q "$1" | tail -1; }

NELEM=20000
SMALL=200
LARGE=3200

psql_run "CREATE TABLE vam(k int, v int) USING pgcolumnar;"
psql_run "INSERT INTO vam SELECT g % 5000, g FROM generate_series(1,50000) g;"
psql_run "CREATE TABLE vam_drv(i int);"
psql_run "INSERT INTO vam_drv SELECT g FROM generate_series(1,$LARGE) g;"
psql_run "ANALYZE vam; ANALYZE vam_drv;"

INLIST="$(q1 "SELECT string_agg(g::text, ',') FROM generate_series(1,$NELEM) g;")"
GUC="SET pgcolumnar.enable_ungrouped_vector_agg=on; SET enable_material=off;"

# The correlated `v > d.i` is a PARAM_EXEC, deliberately NOT frozen into a scan
# key, so it stays a residual qual and the node keeps rescanning. The IN-list is
# the Const that gets rebuilt, detoasted and deconstructed on each of those
# rescans, and it is the only difference between the two arms below.
mkq() {	# mkq NROWS [nokeys] -> the query, driven by NROWS rescans
	if [ "${2:-}" = nokeys ]; then
		printf 'SELECT sum(s.c) FROM (SELECT i FROM vam_drv LIMIT %s) d, LATERAL (SELECT count(*) c FROM vam WHERE v > d.i) s' "$1"
	else
		printf 'SELECT sum(s.c) FROM (SELECT i FROM vam_drv LIMIT %s) d, LATERAL (SELECT count(*) c FROM vam WHERE k IN (%s) AND v > d.i) s' "$1" "$INLIST"
	fi
}

check_num "premise: the fixture loaded" "$(q1 'SELECT count(*) FROM vam;')" 50000

# The premise the whole measurement rests on: this query's IN-list really does
# become pushdown scan keys, so there is something per-rescan to rebuild. Read
# under ANALYZE and not plain EXPLAIN, because the ungrouped node does not fill
# its scan-key count in before the EXPLAIN-only return and a plain EXPLAIN
# reports 0 whatever the truth is (#726) -- so a plain-EXPLAIN premise here
# would refuse every arm for a reason that has nothing to do with this defect.
keyed_plan="$(q "$GUC EXPLAIN (ANALYZE, TIMING OFF, COSTS OFF, SUMMARY OFF) $(mkq 2)")"
keyed="$(grep 'Columnar Pushed-Down Filters' <<<"$keyed_plan" | grep -oE '[0-9]+' | head -1)"
check_num "premise: the IN-list really becomes pushdown scan keys" \
	"$([ -n "$keyed" ] && [ "$keyed" -gt 0 ] && echo 1 || echo 0)" 1
echo "      (Columnar Pushed-Down Filters = ${keyed:-unset})"

# peak_executor_state NROWS [nokeys] -> max "ExecutorState: N total" while it ran
#
# Asserts the execution path on the way past, because every number below is void
# without it: it must be OUR aggregate node, under a Nested Loop (so it rescans
# at all), with no Materialize (which would rescan a stored tuplestore instead
# of re-executing the node, and re-executing the node is the whole subject).
#
# There is deliberately no "= ANY" check here. This node absorbs its quals, so
# EXPLAIN prints no Filter line for it at all, and an assertion on the rendered
# qual text silently refuses every arm. The scan-key premise above covers it.
peak_executor_state() {	# -> bytes, or empty when the path assertion refuses
	local nrows="$1" variant="${2:-}" query fifo out marker pid="" n=0 mark peak plan
	query="$(mkq "$nrows" "$variant")"

	plan="$(q "$GUC EXPLAIN (COSTS OFF) $query")"
	if ! grep -q 'Columnar Vectorized Aggregates' <<<"$plan" ||
	   ! grep -q 'Nested Loop' <<<"$plan" ||
	     grep -q 'Materialize' <<<"$plan"; then
		echo ""
		return 1
	fi

	fifo="$PGC_WORKDIR/vam.$nrows$variant.fifo"; out="$fifo.out"
	rm -f "$fifo" "$out"; mkfifo "$fifo"
	marker="vam_done_$nrows$variant"
	q "DROP TABLE IF EXISTS $marker;" >/dev/null

	env PATH="$PGC_BINDIR:$PATH" PGAPPNAME="pgc717_$nrows$variant" \
		psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At \
		< "$fifo" > "$out" 2>&1 &
	exec 9> "$fifo"
	echo "SELECT 1;" >&9
	while [ -z "$pid" ] && [ $n -lt 200 ]; do
		pid="$(q1 "SELECT pid FROM pg_stat_activity WHERE application_name = 'pgc717_$nrows$variant' LIMIT 1;")"
		n=$((n+1)); [ -z "$pid" ] && sleep 0.1
	done
	if [ -z "$pid" ]; then exec 9>&-; echo ""; return 1; fi

	mark=$(wc -l < "$PGC_LOGFILE")
	echo "$GUC $query;" >&9
	echo "CREATE TABLE $marker(x int);" >&9
	n=0
	while [ "$(q1 "SELECT to_regclass('$marker') IS NOT NULL;")" != t ] && [ $n -lt 3000 ]; do
		q "SELECT pg_log_backend_memory_contexts($pid);" >/dev/null
		n=$((n+1)); sleep 0.1
	done
	exec 9>&-; wait 2>/dev/null
	rm -f "$fifo" "$out"
	q "DROP TABLE IF EXISTS $marker;" >/dev/null

	peak="$(tail -n +"$mark" "$PGC_LOGFILE" |
		grep -oE 'ExecutorState: [0-9]+ total' | grep -oE '[0-9]+' |
		sort -n | tail -1)"
	printf '%s' "$peak"
}

with_small="$(peak_executor_state "$SMALL")"
with_large="$(peak_executor_state "$LARGE")"
without_small="$(peak_executor_state "$SMALL" nokeys)"
without_large="$(peak_executor_state "$LARGE" nokeys)"

# One check per arm, each named literally: a name built from the loop variable is a
# template to compare_to_bash, so a port could omit three of the four and still grade.
reading() { [ -n "$1" ] && [ "$1" -gt 0 ] 2>/dev/null && echo 1 || echo 0; }
check_num "premise: the with_small arm produced a reading" "$(reading "$with_small")" 1
check_num "premise: the with_large arm produced a reading" "$(reading "$with_large")" 1
check_num "premise: the without_small arm produced a reading" "$(reading "$without_small")" 1
check_num "premise: the without_large arm produced a reading" "$(reading "$without_large")" 1

# Bytes of query memory per rescan, as a SLOPE across two rescan counts, so the
# fixed setup cost cancels instead of dominating the small arm.
# Empty when either peak is: awk reads an empty peak as 0, so two missing peaks made a
# slope of 0, and every claim below passed with no reading taken.
slope() {
	[ -n "$1" ] && [ -n "$2" ] || { echo ""; return; }
	awk -v a="$1" -v b="$2" -v n="$(( LARGE - SMALL ))" 'BEGIN { printf "%d", (a - b) / n }'
}

# under LIMIT A B -> "yes" when A and B are both measured and A exceeds B by less than
# LIMIT bytes per rescan, else what was wrong. A missing slope is "no reading", never 0:
# with both memory readers made to return nothing, all three claims below passed
# (measured), because the excess of two missing slopes was computed as 0.
under() {
	if [[ "$2" =~ ^-?[0-9]+$ ]] && [[ "$3" =~ ^-?[0-9]+$ ]]; then
		[ $(( $2 - $3 )) -lt "$1" ] && echo yes || echo "no: excess $(( $2 - $3 )) B per rescan"
	else
		echo "no reading: [$2] against [$3]"
	fi
}
with_slope="$(slope "$with_large" "$with_small")"
without_slope="$(slope "$without_large" "$without_small")"
echo "      per-rescan growth: with a ${NELEM}-element IN-list = ${with_slope} B, without one = ${without_slope} B"
echo "      (peaks: with ${with_small} -> ${with_large}; without ${without_small} -> ${without_large})"

# THE CLAIM, as a DIFFERENTIAL against the no-IN-list control rather than an
# absolute bound.
#
# A bound would have to be picked against one machine's block sizes, and it
# would also be measuring something this defect is not responsible for: the
# aggregate rescan path grows about 0.65 KB per rescan WITH OR WITHOUT the
# IN-list, at identical slopes, so that part is a separate leak (filed
# separately) and not #717. Subtracting the control removes it exactly.
#
# What #717 costs is the difference: before the fix, adding the IN-list added
# about 180,000 bytes per rescan (90,752,384 bytes over 500 rescans at 20000
# elements, against 524,288 flat without it). After it, the two slopes are the
# same to within block quantisation. The 20,000-byte gate sits nine times below
# the defect and far above the noise.
excess="$(awk -v a="$with_slope" -v b="$without_slope" 'BEGIN { d = a - b; print (d < 0 ? 0 : d) }')"
echo "      excess attributable to the IN-list: ${excess} bytes per rescan"
# ONE MECHANISM, ONE ARM, and the history is worth keeping because it is how a
# green check stopped meaning anything for a while.
#
# Written for #717 and labelled with it. #727 then wrapped the ungrouped scan in
# a per-call scratch context, which reclaimed the same allocations, and this arm
# stayed GREEN with #717's contexts fully reverted -- falsely attributing, since
# the fix it named was gone and it could not tell. It was relabelled to the
# invariant and given the joint removal proof.
#
# The scan-key contexts have since been removed outright: the scratch wrap now
# covers both nodes, both row paths and both Begin-time eligibility builds, so
# there is exactly one mechanism behind this property again. Removing it reddens
# this arm on its own, at 180,843 bytes per rescan. The label can mean what it
# says.
check "an IN-list does not add per-rescan query memory" \
	"$(under 20000 "$with_slope" "$without_slope")" "yes"

# ---- the node's own per-rescan footprint, against core's floor (#727) -------
# #717 above is about the scan KEYS. This is the rest of what the node allocated
# per rescan and never freed: the per-row value and null arrays, the projected
# set on the metadata path, and whatever the flushes and the reader left in the
# caller's context. It re-executes on every rescan of a LATERAL sub-scan, and all
# of it lived until ExecutorEnd.
#
# THE FLOOR IS A HEAP TABLE, not zero. Re-executing any node costs something per
# rescan, and asserting an absolute figure would charge this node for PostgreSQL's
# own cost and would move with every release. The same query shape over a heap
# table with identical data measures that floor directly, and the excess over it
# is what pgcolumnar adds.
#
# Measured before the fix: 376 B per rescan against a 33 B heap floor. After it,
# 31 B against 27 B -- the node is no longer distinguishable from core.
# The driver needs enough rows for the two rescan counts below to DIFFER. It was
# sized for the #717 arms (LARGE=3200), and the first version of the arms below
# asked for LIMIT 5000 and LIMIT 40000 against it: both clamped to 3200, both
# arms drove the same number of rescans, and the slope divided a real difference
# of zero by an assumed 35000. It reported ~0 bytes per rescan whether the defect
# was present or not. The premise that catches that is asserted explicitly below.
RS_LO=5000
RS_HI=40000
psql_run "INSERT INTO vam_drv SELECT g FROM generate_series($(( LARGE + 1 )), $RS_HI) g;"
check_num "premise: the driver has enough rows for the larger rescan count" \
	"$(q1 "SELECT (count(*) >= $RS_HI)::int FROM vam_drv;")" 1
check_num "premise: and the two rescan counts really differ" \
	"$(q1 "SELECT ((SELECT count(*) FROM (SELECT i FROM vam_drv LIMIT $RS_HI) a)
	             > (SELECT count(*) FROM (SELECT i FROM vam_drv LIMIT $RS_LO) b))::int;")" 1

# A SMALL table for these arms, and deliberately so. What is being measured is
# allocation per RESCAN, which does not depend on how many rows each rescan
# reads, so scanning the 50000-row fixture 40000 times would spend two billion
# row reads to measure the same slope. 5000 rows keeps the arms to a couple of
# minutes and changes nothing about the quantity.
psql_run "CREATE TABLE vam_rs(k int, v int) USING pgcolumnar;"
psql_run "INSERT INTO vam_rs SELECT g % 500, g FROM generate_series(1,5000) g;"
psql_run "CREATE TABLE vam_heap(k int, v int);"
psql_run "INSERT INTO vam_heap SELECT k, v FROM vam_rs;"
psql_run "ANALYZE vam_rs; ANALYZE vam_heap;"
check_num "premise: the heap mirror has the same rows" \
	"$(q1 'SELECT count(*) FROM vam_heap;')" "$(q1 'SELECT count(*) FROM vam_rs;')"

# Peak RSS, which is monotone and so cannot be missed by a sampler.
rescan_peak() {	# rescan_peak TABLE NROWS [GUC] -> kB above idle, or empty
	local tbl="$1" nrows="$2" guc="${3:-$GUC}" fifo pid="" n=0 base pk query
	query="SELECT sum(s.c) FROM (SELECT i FROM vam_drv LIMIT $nrows) d, LATERAL (SELECT count(*) c FROM $tbl WHERE v > d.i) s"
	fifo="$PGC_WORKDIR/rs.$tbl.$nrows"; rm -f "$fifo"; mkfifo "$fifo"
	env PATH="$PGC_BINDIR:$PATH" PGAPPNAME="pgc727_${tbl}_$nrows" \
		psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At \
		< "$fifo" > "$fifo.out" 2>&1 &
	exec 9> "$fifo"
	echo "SELECT 1;" >&9
	while [ -z "$pid" ] && [ $n -lt 200 ]; do
		pid="$(q1 "SELECT pid FROM pg_stat_activity WHERE application_name = 'pgc727_${tbl}_$nrows' LIMIT 1;")"
		n=$((n+1)); [ -z "$pid" ] && sleep 0.1
	done
	if [ -z "$pid" ]; then exec 9>&-; echo ""; return 1; fi
	base=$(awk '/VmHWM/{print $2}' "/proc/$pid/status" 2>/dev/null)
	echo "$guc $query;" >&9
	n=0
	while [ "$(q1 "SELECT count(*) FROM pg_stat_activity WHERE pid = $pid AND state = 'idle';")" != 1 ] && [ $n -lt 4000 ]; do
		n=$((n+1)); sleep 0.1
	done
	pk=$(awk '/VmHWM/{print $2}' "/proc/$pid/status" 2>/dev/null)
	exec 9>&-; wait 2>/dev/null; rm -f "$fifo" "$fifo.out"
	[ -n "$base" ] && [ -n "$pk" ] && echo $(( pk - base )) || echo ""
}
rescan_slope() {	# rescan_slope TABLE [GUC] -> bytes per rescan
	local tbl="$1" guc="${2:-$GUC}" lo hi
	lo="$(rescan_peak "$tbl" "$RS_LO" "$guc")"; hi="$(rescan_peak "$tbl" "$RS_HI" "$guc")"
	[ -z "$lo" ] || [ -z "$hi" ] && { echo ""; return 1; }
	awk -v a="$hi" -v b="$lo" -v d="$(( RS_HI - RS_LO ))" 'BEGIN { printf "%d", ((a - b) * 1024) / d }'
}
# The columnar arm must really be OUR node, or it is measuring the plain scan.
# Captured first, then read from a here-string. Piping the plan straight into an
# early-exit reader answers "absent" for a plan that CONTAINS the node whenever
# the writer takes EPIPE first, and the failure direction is the expensive one:
# this premise would report the vectorized aggregate missing and send the reader
# after a planner regression that is not there (#486).
vam_plan_on="$(q "$GUC EXPLAIN (COSTS OFF) SELECT sum(s.c) FROM (SELECT i FROM vam_drv LIMIT 100) d, LATERAL (SELECT count(*) c FROM vam_rs WHERE v > d.i) s")"
check_text "premise: the columnar rescan arm is the vectorized aggregate" \
	"$(grep -q 'Columnar Vectorized Aggregates' <<<"$vam_plan_on" && echo yes || echo no)" yes
vec_slope="$(rescan_slope vam_rs)"
heap_slope="$(rescan_slope vam_heap)"
echo "      per-rescan growth: columnar vectorized aggregate = ${vec_slope:-unset} B, heap floor = ${heap_slope:-unset} B"
check_num "premise: both rescan slopes were measured" \
	"$([ -n "$vec_slope" ] && [ -n "$heap_slope" ] && echo 1 || echo 0)" 1
# The excess over core. 100 B gates three times under the 343 B defect and well
# above the few bytes that remain.
rescan_excess="$(awk -v a="$vec_slope" -v b="$heap_slope" 'BEGIN { d = a - b; print (d < 0 ? 0 : d) }')"
echo "      excess over the heap floor: ${rescan_excess} B per rescan"
check "the vectorized aggregate does not grow query memory per rescan (#727)" \
	"$(under 100 "$vec_slope" "$heap_slope")" "yes"
# And the answers are unchanged, so the arm above measures the saving and is not
# standing in for correctness.
check_num "the rescanned aggregate still returns the heap's answer" \
	"$(q1 "$GUC SELECT sum(s.c) FROM (SELECT i FROM vam_drv LIMIT 200) d, LATERAL (SELECT count(*) c FROM vam_rs WHERE v > d.i) s;")" \
	"$(q1 "SELECT sum(s.c) FROM (SELECT i FROM vam_drv LIMIT 200) d, LATERAL (SELECT count(*) c FROM vam_heap WHERE v > d.i) s;")"

# ---- the PLAIN columnar scan's own per-rescan footprint (#734) --------------
# The arms above cover the vectorized aggregate. This covers the scan UNDER it,
# which is a different code path with a different lifetime and was measured
# separately: with the vectorized aggregate turned off, the columnar scan alone
# still grew query memory per rescan while the heap did not.
#
# The mechanism, pinned by a memory-context dump rather than guessed: the growth
# is in the read state's own "columnar read" context, and it is the ROW-GROUP
# LIST. A rescan reuses the read state (PgColumnarRescanRead, not an end and
# re-open), that function sets rowGroupList to NIL without freeing it, and the
# next start rebuilds a fresh list and a fresh metadata struct per group into the
# same context. Nothing reclaims the previous one.
GUC_OFF="SET pgcolumnar.enable_ungrouped_vector_agg=off; SET pgcolumnar.enable_group_vectorization=off; SET enable_material=off;"
# ONE capture, read twice. Both premises are statements about the SAME plan, so
# explaining it twice was two chances for the answers to disagree as well as two
# pipelines that could take EPIPE. The second one is the dangerous half: it WANTS
# "no", so a spurious absence makes it pass for the wrong reason and the vacuity
# is silent rather than red.
vam_plan_off="$(q "$GUC_OFF EXPLAIN (COSTS OFF) SELECT sum(s.c) FROM (SELECT i FROM vam_drv LIMIT 100) d, LATERAL (SELECT count(*) c FROM vam_rs WHERE v > d.i) s")"
check_text "premise: with the aggregate off the arm is the plain columnar scan" \
	"$(grep -q 'Custom Scan (PgColumnarScan)' <<<"$vam_plan_off" && echo yes || echo no)" yes
check_text "premise: and it is NOT the vectorized aggregate node" \
	"$(grep -q 'Columnar Vectorized Aggregates' <<<"$vam_plan_off" && echo yes || echo no)" no
plain_slope="$(rescan_slope vam_rs "$GUC_OFF")"
plain_heap="$(rescan_slope vam_heap "$GUC_OFF")"
echo "      per-rescan growth: plain columnar scan = ${plain_slope:-unset} B, heap floor = ${plain_heap:-unset} B"
check_num "premise: both plain-scan slopes were measured" \
	"$([ -n "$plain_slope" ] && [ -n "$plain_heap" ] && echo 1 || echo 0)" 1
plain_excess="$(awk -v a="$plain_slope" -v b="$plain_heap" 'BEGIN { d = a - b; print (d < 0 ? 0 : d) }')"
echo "      excess over the heap floor: ${plain_excess} B per rescan"
check "the plain columnar scan does not grow query memory per rescan (#734)" \
	"$(under 100 "$plain_slope" "$plain_heap")" "yes"
check_num "the rescanned plain scan still returns the heap's answer" \
	"$(q1 "$GUC_OFF SELECT sum(s.c) FROM (SELECT i FROM vam_drv LIMIT 200) d, LATERAL (SELECT count(*) c FROM vam_rs WHERE v > d.i) s;")" \
	"$(q1 "SELECT sum(s.c) FROM (SELECT i FROM vam_drv LIMIT 200) d, LATERAL (SELECT count(*) c FROM vam_heap WHERE v > d.i) s;")"

# ---- the row path's keys, pinned as WORK ------------------------------------
# The two halves of this change fail very differently, and only one of them is
# loud.
#
# On the FOLD path the scan keys ARE the per-row filter, so a key set freed
# while in use is immediately fatal: injecting exactly that (reset the context
# after the gates build the keys) takes SIGSEGV on a plain
# `count(*) ... WHERE k > 500`, and three suites go from a verdict to no verdict
# at all.
#
# On the ROW path the same injection is SILENT. There the keys only prune row
# groups, and every surviving row is rechecked against the full WHERE anyway, so
# freed keys change which groups are read and NOT the answer: the four suites
# run against that injection all returned correct results and passed.
#
# So correctness checks cannot cover the row path's key lifetime, and this arm
# is the one that can. It pins the PRUNING as work: with live keys the reader
# skips row groups the zone maps rule out; with a freed or corrupted key set it
# skips none and reads everything, while still answering correctly.
psql_run "CREATE TABLE vam_prune(k int, s text) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('vam_prune', stripe_row_limit => 10000);"
psql_run "INSERT INTO vam_prune SELECT g, 'x' || g FROM generate_series(1,200000) g;"

# A by-reference column in the projection is what keeps this on the row path
# (the fold's gather is by-value only, #423), which is the path being pinned.
prune_plan="$(q "$GUC EXPLAIN (ANALYZE, TIMING OFF, COSTS OFF, SUMMARY OFF) SELECT count(s) FROM vam_prune WHERE k > 190000")"
check_text "premise: the pruning arm is on the ROW path, not the fold" \
	"$(grep 'Columnar Batch Fold' <<<"$prune_plan" | grep -oE 'yes|no' | head -1)" no
check_text "premise: and it is the vectorized aggregate node" \
	"$(grep -q 'Columnar Vectorized Aggregates' <<<"$prune_plan" && echo yes || echo no)" yes
removed="$(grep 'Columnar Chunk Groups Removed by Filter' <<<"$prune_plan" | grep -oE '[0-9]+' | head -1)"
total="$(grep 'Columnar Chunk Groups Total' <<<"$prune_plan" | grep -oE '[0-9]+' | head -1)"
echo "      row-path pruning: removed ${removed:-unset} of ${total:-unset} chunk groups"
check_num "the row path's scan keys still prune (work done, #717)" \
	"$([ -n "$removed" ] && [ "$removed" -gt 0 ] && echo 1 || echo 0)" 1
# And the answer stays right, so the arm above is measuring the saving and not
# standing in for correctness.
check_num "row path: the pruned query still answers correctly" \
	"$(q1 'SELECT count(s) FROM vam_prune WHERE k > 190000;')" 10000

pgc_summary
