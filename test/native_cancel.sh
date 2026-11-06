#!/usr/bin/env bash
#
# pgColumnar scan cancellation (interrupt handling in the read path).
#
# The executor checks for interrupts once per tuple it receives, which is no help
# while a scan is doing work without producing a tuple. The expensive case is
# loading a row group: pgcolumnar_native_load_group() reads and decodes every
# column chunk of the group before the row loop can iterate once, and a vector
# holds up to pgcolumnar.chunk_group_row_limit values, which is user-settable and
# unbounded. Without interrupt checks inside that load, statement_timeout,
# pg_cancel_backend() and a standby's recovery conflict all wait for the whole
# decode.
#
# The check here is self-calibrating rather than a fixed millisecond threshold,
# so it means the same thing on fast and slow hardware: time an uninterrupted
# load, then time how long a deliberately short statement_timeout takes to fire
# on the same query. With interrupt checks in the decode path the timeout fires
# early in the load; without them it fires only after the load completes, so the
# two timings converge. Asserting the cancel is comfortably under half the full
# load distinguishes the two without depending on absolute speed.
#
# Usage:  test/native_cancel.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# 16M values in one column, as many as the old table's two columns of 8M held, so
# the load (and the window the timing check reads) stays as long
ROWS=${PGC_CANCEL_ROWS:-16000000}

# One row group, one vector: the whole table decodes in a single load, which is
# the shape that makes the uninterruptible window long enough to measure.
#
# ONE COLUMN, so the only interrupt point inside the load is the decode loop's.
# pgcolumnar_native_load_group() also checks once before each column chunk, and with
# two columns that check fell at half the load: with the decode-loop check disabled
# the cancel landed there (90 ms against a 172 ms load and a 106 ms limit) and the
# timing check below passed (#1341). With one column every per-chunk check falls
# before the decode starts, so only the decode loop can cut the load short.
psql_run "SET pgcolumnar.stripe_row_limit = $ROWS;
          SET pgcolumnar.chunk_group_row_limit = $ROWS;
          CREATE TABLE cancel_t (v bigint) USING pgcolumnar;
          INSERT INTO cancel_t SELECT g * 7 FROM generate_series(1, $ROWS) g;"

check "one row group" "$(q 'SELECT count(*) FROM pgcolumnar.row_group;')" "1"

# Parallel workers would give the executor its own interrupt points and hide the
# thing under test, so the scan is kept serial.
ms_for() {
	local start end
	start=$(date +%s%N)
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -v VERBOSITY=verbose -c "SET max_parallel_workers_per_gather = 0; $1" \
		>"$PGC_WORKDIR/cancel.out" 2>&1
	end=$(date +%s%N)
	echo $(( (end - start) / 1000000 ))
}

# a full uninterrupted load, twice, so the second is warm like the timed one
ms_for "SELECT * FROM cancel_t LIMIT 1;" >/dev/null
full="$(ms_for "SELECT * FROM cancel_t LIMIT 1;")"

# THE TIMEOUT IS A QUARTER OF THIS BOX'S LOAD, NOT A FIXED 50 ms (#1481). On a fast
# runner the whole load finished in about 50 ms, so a 50 ms timeout had nothing left
# to interrupt and the query returned with no error: the nightly's coverage job
# measured "full load 57 ms" against it and went red with no product change. The
# load is read from the server's own Execution Time, because `full` above also counts
# psql's start and connect. A quarter of it, at least 5 ms, lands inside the load on
# any machine, which is what both arms below need.
exec_ms="$(q "SET max_parallel_workers_per_gather = 0;
              EXPLAIN (ANALYZE, TIMING OFF, SUMMARY ON) SELECT * FROM cancel_t LIMIT 1;" |
	sed -n 's/^Execution Time: \([0-9]*\)\..*/\1/p')"
TIMEOUT_MS=$(( ${exec_ms:-0} / 4 ))
[ "$TIMEOUT_MS" -lt 5 ] && TIMEOUT_MS=5
check_num "premise: the load outlasts the timeout, so the timeout can fire during it" \
	"$([ "${exec_ms:-0}" -gt "$TIMEOUT_MS" ] && echo 1 || echo "0 (load ${exec_ms:-?} ms, timeout ${TIMEOUT_MS} ms)")" "1"
echo "-- server-side load ${exec_ms} ms; statement timeout ${TIMEOUT_MS} ms"

# The quickest of three cancels. A cancel cannot land early without an interrupt
# point inside the load, so a retry cannot pass a missing guard: it only stops one
# slow connect on a busy box from reading as one. Every attempt must still be the
# timeout's own error.
TIMEOUT_ERR="ERROR:  57014: canceling statement due to statement timeout"
cancel="" err="$TIMEOUT_ERR"
for _try in 1 2 3; do
	_ms="$(ms_for "SET statement_timeout = '${TIMEOUT_MS}ms'; SELECT * FROM cancel_t LIMIT 1;")"
	_err="$(grep -m1 -oE 'ERROR:.*' "$PGC_WORKDIR/cancel.out")"
	# only a cancel times a cancel: a connection refused during crash recovery fails
	# in a few ms, and counted it would read as an early cancel
	if [ "$_err" != "$TIMEOUT_ERR" ]; then err="${_err:-no error}"; continue; fi
	{ [ -z "$cancel" ] || [ "$_ms" -lt "$cancel" ]; } && cancel="$_ms"
done

echo "-- full load ${full} ms; quickest of three cancels fired after ${cancel} ms"

# on every attempt, by its SQLSTATE and the timeout's own message (VERBOSITY=verbose
# prints both)
check "the short timeout is what fired" \
	"$([ "$err" = "$TIMEOUT_ERR" ] && echo OK || echo "$err")" \
	"OK"

# Without interrupt checks in the load path the timeout cannot fire until the
# load finishes, so cancel and full converge. With them it fires during the load.
#
# The yardstick is the window between the two outcomes, not a bare fraction of
# `full`. `cancel` cannot go below the timeout itself, so `full / 2` is only a
# real threshold while `full` is comfortably more than twice the timeout: at
# full=155 it leaves 27 ms of headroom above a 50 ms floor, and PG17 measured
# 64-82 ms against PG18's stable 63-64, failing two runs in three on hardware
# where the cancel worked correctly every time. That is a threshold reporting the
# box, not the guard.
#
# What the guard actually distinguishes: cancel fires *during* the load (just
# after the timeout) or only *after* it (converging on `full`). So require cancel
# to land in the lower half of the interval between those two, which is
# self-calibrating in both directions and cannot be squeezed by a fast box.
#
# Which guard this actually proves, established by removing each one rather than
# from the description above: it is COLUMNAR_DECODE_INTERRUPT in
# columnar_encoding.c, the per-value decode-loop check on a 65536 stride, not the
# per-column-chunk CHECK_FOR_INTERRUPTS in pgcolumnar_native_load_group(). On the
# one-column table every per-chunk check falls before the decode, so deleting it
# leaves this suite green, and disabling the decode-loop macro makes cancel converge
# on full and fails this check. (On the old two-column table the macro's removal
# passed: the second chunk's check fell inside the window, see the table above.)
# Stated so the next person does not remove the cheap guard on the strength of a
# green run here.
limit=$(( TIMEOUT_MS + (full - TIMEOUT_MS) / 2 ))
check_timing "cancel arrives well before the load completes" \
	"$( [ "$full" -gt $(( TIMEOUT_MS * 2 )) ] && [ -n "$cancel" ] && [ "$cancel" -lt "$limit" ] && echo yes ||
	    echo "no (cancel=${cancel}ms full=${full}ms limit=${limit}ms)")" \
	"yes"

pgc_summary
