#!/usr/bin/env bash
#
# sum() and avg() over bigint accumulate in 128 bits (#785).
#
# The vectorized path used to convert EVERY value to numeric and call
# numeric_add per row: two pallocs and a full numeric addition for each row. A
# profile of that path was 13.0% make_result_opt_error, 9.3% add_abs, 8.1%
# init_var_from_num and 8.5% AllocSet alloc/free -- the numeric machinery, not
# the aggregate. Turning the documented fast path ON made sum(bigint) 2.03x
# SLOWER than leaving it off.
#
# It now accumulates in int128 and converts once at finalize, as core does.
#
# WHY THIS SUITE EXISTS SEPARATELY. pgcolumnar.enable_ungrouped_vector_agg is
# OFF by default, so the ordinary aggregate suites never reach this code at all.
# Every arm here sets it explicitly and asserts the plan really took the
# vectorized node, because an arm that silently fell back to the core Agg would
# pass while testing nothing.
#
# THE ARM THAT MATTERS MOST is the one whose sum exceeds int64. Below that, the
# conversion is a single int8_numeric and any implementation looks right. Above
# it the value must be reconstructed from two 64-bit halves, which is where an
# error would live, and it is the case a fixture of small numbers never reaches.
#
# HOW TO REVERT THIS FIX WHEN CHECKING THESE ARMS. `#undef HAVE_INT128` after
# the includes is a better mutation than deleting the code, and it is the
# reviewer's, not mine. It takes the platform fallback rather than removing the
# branches, so the 22 value arms below then exercise the NON-int128 path and
# check that it is still correct -- coverage neither of us set out to get. It
# also reproduces the timing result: 3.63x against the 3.68x that deleting the
# accumulator gave.
#
# Usage:  test/int8_agg_int128.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

ON="SET pgcolumnar.enable_ungrouped_vector_agg=on"

# Every value arm runs the query three ways and requires all three to agree:
# columnar with the vectorized path ON, columnar with it OFF, and a heap twin.
# The heap twin is the oracle; the OFF arm catches a fault that is ours but not
# the new path's.
trio() {
	local tbl="$1" q="$2"
	local on off heap
	# tail -1: the SET prints "SET" of its own, so the value is the last line.
	# A NULL answer reads <NULL>, so it is not the empty string a failed query
	# reads: three failures used to "agree:" exactly as three NULLs do, and the
	# empty-table and all-NULL arms passed on them.
	on="$(q "$ON; SELECT coalesce(($q)::text, '<NULL>') FROM $tbl;" | tail -1)"
	off="$(q "SET pgcolumnar.enable_ungrouped_vector_agg=off; SELECT coalesce(($q)::text, '<NULL>') FROM $tbl;" | tail -1)"
	heap="$(q "SELECT coalesce(($q)::text, '<NULL>') FROM ${tbl}_h;" | tail -1)"
	if [ -z "$on" ] || [ -z "$off" ] || [ -z "$heap" ]; then echo "UNREAD on=[$on] off=[$off] heap=[$heap]"
	elif [ "$on" = "$off" ] && [ "$off" = "$heap" ]; then echo "agree:$on"
	else echo "DIFFER on=[$on] off=[$off] heap=[$heap]"; fi
}
vecplan() { q "$ON; EXPLAIN (COSTS OFF) SELECT $1 FROM $2;" | grep -c 'Columnar Vectorized Aggregates'; }

# The heap twin is loaded from the SAME GENERATOR, never copied out of the
# columnar table: a copy reads through the columnar decoder, so a decode fault
# lands in the oracle too and the trio agrees on the wrong answer.
mk() {  # $1 name, $2 value expression over g, $3 series end, $4 type (bigint)
	psql_run "CREATE TABLE $1 (v ${4:-bigint}) USING pgcolumnar;"
	psql_run "INSERT INTO $1 SELECT $2 FROM generate_series(1,$3) g;"
	psql_run "CREATE TABLE $1_h (v ${4:-bigint});"
	psql_run "INSERT INTO $1_h SELECT $2 FROM generate_series(1,$3) g;"
}

# ---- ordinary values --------------------------------------------------------
mk a8 "(g % 1000)::bigint" 200000
check_num "premise: the vectorized node really runs for sum(v)" "$(vecplan 'sum(v)' a8)" "1"
check_num "premise: and for avg(v)" "$(vecplan 'avg(v)' a8)" "1"
check "sum(bigint) agrees with the scalar path and with heap" "$(trio a8 'sum(v)')" "agree:99900000"
check "avg(bigint) agrees too" "$(trio a8 'avg(v)')" "agree:499.5000000000000000"

# ---- negatives and nulls ----------------------------------------------------
psql_run "DROP TABLE IF EXISTS n8; DROP TABLE IF EXISTS n8_h;"
mk n8 "CASE WHEN g % 7 = 0 THEN NULL WHEN g % 3 = 0 THEN -(g::bigint) ELSE g::bigint END" 100000
check "premise: the fixture really holds NULLs and negatives" \
	"$(q "SELECT (count(*) FILTER (WHERE v IS NULL) > 0)::text||'/'||(count(*) FILTER (WHERE v < 0) > 0)::text FROM n8;")" \
	"true/true"
check "sum over a column with NULLs and negatives agrees" "$(trio n8 'sum(v)')" \
	"agree:$(q 'SELECT sum(v)::text FROM n8_h;')"
check "avg over the same agrees" "$(trio n8 'avg(v)')" \
	"agree:$(q 'SELECT avg(v)::text FROM n8_h;')"

# ---- THE ARM THIS SUITE EXISTS FOR: a sum that does not fit in int64 --------
# 20,000 rows of 9e18 sums to 1.8e23, far past int64's 9.22e18. Below that
# boundary the finalize is one int8_numeric call and any implementation looks
# correct; above it the value is rebuilt from two 64-bit halves.
#
# The value is 9e18 + 7, NOT 9e18. The rebuild splits on 10^18, and 20,000 rows
# of exactly 9e18 sum to 180000 * 10^18 with a low half of 0, so a rebuild that
# dropped the low half entirely passed every arm here (measured). + 7 gives a
# low half of 140000.
psql_run "DROP TABLE IF EXISTS big8; DROP TABLE IF EXISTS big8_h;"
mk big8 "9000000000000000007::bigint" 20000
check "premise: the sum really exceeds what int64 can hold" \
	"$(q "SELECT (sum(v) > 9223372036854775807::numeric)::text FROM big8_h;")" "true"
check_num "premise: the vectorized node runs on this table too" "$(vecplan 'sum(v)' big8)" "1"
check "a sum past int64 agrees with the scalar path and with heap" \
	"$(trio big8 'sum(v)')" "agree:180000000000000000140000"
check "and its avg agrees" "$(trio big8 'avg(v)')" \
	"agree:$(q 'SELECT avg(v)::text FROM big8_h;')"

# the negative side of the same boundary, since the split carries a sign
psql_run "DROP TABLE IF EXISTS neg8; DROP TABLE IF EXISTS neg8_h;"
mk neg8 "-9000000000000000007::bigint" 20000
check "premise: this sum is past int64 in the negative direction" \
	"$(q "SELECT (sum(v) < -9223372036854775807::numeric)::text FROM neg8_h;")" "true"
check "a large NEGATIVE sum agrees" "$(trio neg8 'sum(v)')" "agree:-180000000000000000140000"

# a mixed sign fixture whose total lands back inside int64, so the split is
# exercised on the way and the answer is small
psql_run "DROP TABLE IF EXISTS mix8; DROP TABLE IF EXISTS mix8_h;"
mk mix8 "CASE WHEN g % 2 = 0 THEN 9000000000000000000::bigint ELSE -8999999999999999999::bigint END" 20000
check "mixed signs summing back into int64 range agree" "$(trio mix8 'sum(v)')" \
	"agree:$(q 'SELECT sum(v)::text FROM mix8_h;')"

# ---- degenerate shapes ------------------------------------------------------
mk e8 "g::bigint" 0
check "sum over an empty table is NULL, as on heap" "$(trio e8 'sum(v)')" "agree:<NULL>"
check "avg over an empty table is NULL too" "$(trio e8 'avg(v)')" "agree:<NULL>"
mk z8 "NULL::bigint" 5000
check "sum over an all-NULL column is NULL" "$(trio z8 'sum(v)')" "agree:<NULL>"
check "avg over an all-NULL column is NULL" "$(trio z8 'avg(v)')" "agree:<NULL>"

# ---- numeric is NOT changed by this, and must still agree -------------------
mk d8 "(g::numeric / 7)" 50000 numeric
check "sum(numeric) keeps its scale and still agrees" "$(trio d8 'sum(v)')" \
	"agree:$(q 'SELECT sum(v)::text FROM d8_h;')"
check "avg(numeric) too" "$(trio d8 'avg(v)')" \
	"agree:$(q 'SELECT avg(v)::text FROM d8_h;')"

# ---- WORK DONE: the vectorized path must not be SLOWER than the scalar one ---
#
# The 22 value arms above cannot see this fix removed. Reverting it restores the
# per-row numeric accumulation, which is CORRECT and merely slow, so all 22 stay
# green. The defect #785 records was speed, so the arm that guards it has to
# measure speed.
#
# It uses check_ratio and NOT check_ratio_needs_quiet_machine, and that distinction is the
# whole reason this arm runs at all: PGC_SKIP_TIMING=1 is set in ci.yml and in
# nightly.yml, so the _timing form would run in no automated gate while these 22
# arms still reported PASSED. That is a suite passing with its subject dropped.
#
# The exemption is the one cancel_decode.sh already states for itself: this is a
# ratio between two readings taken back to back in the same run on the same
# machine, not a ratio against a data-volume baseline. Both readings move
# together under load, which is what makes it safe on shared hardware where the
# wall-clock ratio suites are not.
#
# THE ASSERTION IS A BOUND, NOT A TARGET. "Not slower than the scalar path" is
# exactly the defect, and it does not encode how fast this machine is. The gap
# is wide: 2.03x slower before the fix, 1.65x faster after, and reverting it
# measures 3.63x to 3.68x against a 1.10x bound. A false red needs the machine
# to make the fixed arm more than 2x slower than it measures here.
psql_run "DROP TABLE IF EXISTS perf8;"
psql_run "CREATE TABLE perf8 (v bigint) USING pgcolumnar;"
psql_run "INSERT INTO perf8 SELECT (g % 1000)::bigint FROM generate_series(1,4000000) g;"
check_num "premise: the timing fixture loaded every row" \
	"$(q 'SELECT count(*) FROM perf8;')" "4000000"
check_num "premise: and the vectorized node really runs, so the ratio compares two paths" \
	"$(vecplan 'sum(v)' perf8)" "1"

# min of 5 each, INTERLEAVED, which is what makes the ratio self-normalising.
ms() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -q \
		-c "SET pgcolumnar.enable_ungrouped_vector_agg=$1" -c '\timing on' \
		-c "SELECT sum(v) FROM perf8" 2>/dev/null \
	| grep -o 'Time: [0-9.]*' | tail -1 | cut -d' ' -f2
}
BON=""; BOFF=""
for _ in 1 2 3 4 5; do
	x="$(ms on)"; y="$(ms off)"
	[ -z "$BON" ] && BON="$x"; [ -z "$BOFF" ] && BOFF="$y"
	BON="$(awk -v a="$BON" -v b="$x" 'BEGIN{print (b<a)?b:a}')"
	BOFF="$(awk -v a="$BOFF" -v b="$y" 'BEGIN{print (b<a)?b:a}')"
done
echo "-- sum(bigint): vectorized $BON ms, scalar $BOFF ms"
check_ratio "the vectorized path is not slower than the scalar one" "$BON" "$BOFF" "1.10"

# ---- the FILTERED case, which is what #755 question 3 fixed -----------------
#
# Before the int8 kinds were admitted to the batch fold this was the arm that
# lost: 1.13x to 1.21x SLOWER with the path on, while sum(float8) on the same
# fixture was 2.4x faster. The asymmetry was the whole evidence for q3, so it
# gets its own arm rather than riding on the unfiltered one.
#
# The fixture deliberately defeats run-length and dictionary encoding. A column
# of `g % 1000` is answered from encoded runs in about a millisecond, and a fold
# measured on that measures nothing -- the same trap as an FSST fixture that the
# dictionary collapses before the encoder ever runs.
psql_run "DROP TABLE IF EXISTS perf8f;"
psql_run "CREATE TABLE perf8f (k int, v bigint) USING pgcolumnar;"
psql_run "INSERT INTO perf8f SELECT g % 1000, ((g*2654435761)::bigint % 1000000007)
          FROM generate_series(1,4000000) g;"
check_num "premise: the filtered fixture loaded every row" \
	"$(q 'SELECT count(*) FROM perf8f;')" "4000000"
check "premise: its values are NOT low-cardinality, so the fold is not answered from runs" \
	"$([ "$(q 'SELECT count(DISTINCT v) FROM perf8f;')" -gt 1000000 ] && echo varied || echo "collapsed")" \
	"varied"
check_num "premise: the vectorized node runs on the FILTERED query too" \
	"$(q "$ON; EXPLAIN (COSTS OFF) SELECT sum(v) FROM perf8f WHERE k < 50;" | grep -c 'Columnar Vectorized Aggregates')" "1"

msf() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -q \
		-c "SET pgcolumnar.enable_ungrouped_vector_agg=$1" -c '\timing on' \
		-c "SELECT sum(v) FROM perf8f WHERE k < 50" 2>/dev/null \
	| grep -o 'Time: [0-9.]*' | tail -1 | cut -d' ' -f2
}
FON=""; FOFF=""
for _ in 1 2 3 4 5; do
	x="$(msf on)"; y="$(msf off)"
	[ -z "$FON" ] && FON="$x"; [ -z "$FOFF" ] && FOFF="$y"
	FON="$(awk -v a="$FON" -v b="$x" 'BEGIN{print (b<a)?b:a}')"
	FOFF="$(awk -v a="$FOFF" -v b="$y" 'BEGIN{print (b<a)?b:a}')"
done
echo "-- sum(bigint) WHERE k < 50: vectorized $FON ms, scalar $FOFF ms"
check_ratio "the FILTERED vectorized path is not slower than the scalar one" "$FON" "$FOFF" "1.10"

# ---- the accumulator must be cleared on a RESCAN ---------------------------
# The int8 kinds accumulate into spec->i128sum. There are two reset sites:
# pgcolumnar_agg_specs_reset clears nine fields including that one, and
# PgColumnarReScanAggScan open-coded the same loop with eight and no i128sum. On
# a rescan the running int128 total was therefore never zeroed and each
# re-execution added to the previous one.
#
# Nothing here reached it. This suite drives the int128 accumulator and never
# rescans; vector_agg_rescan_memory.sh rescans and aggregates count(*), which
# does not touch i128sum. Each condition was covered and their intersection was
# not.
#
# A LATERAL over a small driver re-executes the aggregate node once per driver
# row. enable_material=off keeps core from materialising the inner side, which
# would hide the rescan entirely.
psql_run "CREATE TABLE rs_drv(i bigint);"
psql_run "INSERT INTO rs_drv SELECT g FROM generate_series(1,4) g;"
RSGUC="$ON; SET enable_material=off; SET max_parallel_workers_per_gather=0;"
RSLAT="SELECT d.i, s.c FROM rs_drv d, LATERAL (SELECT %A c FROM %T s2 WHERE s2.v > d.i) s ORDER BY d.i"

rs_run() {  # rs_run <table> <agg>
	# Separate declarations on purpose: bash expands the whole command line
	# before assigning, so `local t="$1" sql="${RSLAT//%T/$t}"` reads t unset.
	# Under `set -u` that aborted the function and both sides of a comparison
	# came back as the SAME error text, which `check` then passed. An arm that
	# cannot tell an error from an answer is not an arm.
	local t="$1"
	local a="$2"
	local sql="${RSLAT//%T/$t}"
	sql="${sql//%A/$a}"
	# ON_ERROR_STOP: psql runs the next -c after a failed one, so a setting that
	# did not take let the rescan run on core's path, where both sides agreed
	# (measured, with enable_material set to an invalid value).
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -Atq -v ON_ERROR_STOP=1 -c "$RSGUC" -c "$sql" 2>&1
}
rs_plan() {
	local sql="${RSLAT//%T/a8}"; sql="${sql//%A/sum(s2.v)}"
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -Atq -c "$RSGUC" -c "EXPLAIN (ANALYZE, TIMING OFF, SUMMARY OFF, COSTS OFF) $sql" 2>&1
}

# Each side must return its four rows; otherwise the reading names its table, so
# two sides that both errored cannot agree on the same error text.
rs_rows() {  # rs_rows <table> <agg>
	local out
	out="$(rs_run "$1" "$2")"
	if [ "$(printf '%s\n' "$out" | grep -cE '^[0-9]+\|-?[0-9.]+$')" = 4 ]; then printf '%s\n' "$out"
	else echo "not 4 rows from $1: $out"; fi
}

RSPLAN="$(rs_plan)"
check_num "premise: the rescan shape still takes the vectorized node" \
	"$(grep -c 'Columnar Vectorized Aggregates' <<<"$RSPLAN")" "1"
RSLOOPS="$(grep -oE 'loops=[0-9]+' <<<"$RSPLAN" | grep -oE '[0-9]+' | sort -rn | head -1)"
echo "-- rescan loops observed: ${RSLOOPS:-none}"
check "premise: the node really is re-executed, so a stale accumulator can show" \
	"$([ "${RSLOOPS:-0}" -gt 1 ] && echo rescanned || echo "not rescanned")" "rescanned"

# Premise before the comparisons: the rescan query returns one row per driver
# row. Without it an error on BOTH sides compares equal and passes.
RSOUT="$(rs_run a8 'sum(s2.v)')"
check_num "premise: the rescan query returns a row per driver row, not an error" \
	"$(printf '%s\n' "$RSOUT" | grep -c '^[0-9]')" "4"
check "sum(bigint) across a rescan matches the heap" \
	"$(rs_rows a8 'sum(s2.v)')" "$(rs_rows a8_h 'sum(s2.v)')"
check "avg(bigint) across a rescan matches the heap" \
	"$(rs_rows a8 'avg(s2.v)')" "$(rs_rows a8_h 'avg(s2.v)')"
check "sum(bigint) across a rescan on the >int64 fixture matches the heap" \
	"$(rs_rows big8 'sum(s2.v)')" "$(rs_rows big8_h 'sum(s2.v)')"
# The control: a kind that does NOT use i128sum must be right either way, which
# places any failure above on the int128 accumulator rather than on rescan.
check "control: count(*) across the same rescan matches the heap" \
	"$(rs_rows a8 'count(*)')" "$(rs_rows a8_h 'count(*)')"

pgc_summary
