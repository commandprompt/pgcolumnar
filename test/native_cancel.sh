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

ROWS=${PGC_CANCEL_ROWS:-8000000}

# One row group, one vector: the whole table decodes in a single load, which is
# the shape that makes the uninterruptible window long enough to measure.
psql_run "SET pgcolumnar.stripe_row_limit = $ROWS;
          SET pgcolumnar.chunk_group_row_limit = $ROWS;
          CREATE TABLE cancel_t (id bigint, v bigint) USING pgcolumnar;
          INSERT INTO cancel_t SELECT g, g * 7 FROM generate_series(1, $ROWS) g;"

check "one row group" "$(q 'SELECT count(*) FROM pgcolumnar.row_group;')" "1"

# Parallel workers would give the executor its own interrupt points and hide the
# thing under test, so the scan is kept serial.
ms_for() {
	local start end
	start=$(date +%s%N)
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "SET max_parallel_workers_per_gather = 0; $1" \
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

cancel="$(ms_for "SET statement_timeout = '${TIMEOUT_MS}ms'; SELECT * FROM cancel_t LIMIT 1;")"
err="$(grep -oE 'ERROR:.*' "$PGC_WORKDIR/cancel.out" | head -1)"

echo "-- full load ${full} ms; cancel fired after ${cancel} ms"

check "the short timeout is what fired" \
	"$(case "$err" in *"canceling statement due to statement timeout"*) echo OK ;; *) echo "$err" ;; esac)" \
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
# per-column-chunk CHECK_FOR_INTERRUPTS in pgcolumnar_native_load_group(). Deleting
# the per-chunk check leaves this suite green, because a two-column group reaches
# it only twice; disabling the decode-loop macro makes cancel converge on full
# (151 ms against 150) and fails this check, which is the behaviour the paragraph
# above predicts. Stated so the next person does not remove the cheap guard on the
# strength of a green run here.
limit=$(( TIMEOUT_MS + (full - TIMEOUT_MS) / 2 ))
check_timing "cancel arrives well before the load completes" \
	"$( [ "$full" -gt $(( TIMEOUT_MS * 2 )) ] && [ "$cancel" -lt "$limit" ] && echo yes ||
	    echo "no (cancel=${cancel}ms full=${full}ms limit=${limit}ms)")" \
	"yes"

pgc_summary
