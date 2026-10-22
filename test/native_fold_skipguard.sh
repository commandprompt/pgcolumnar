#!/usr/bin/env bash
#
# pgColumnar: the batch fold must refuse a group whose decode skipped vectors
# (#512).
#
# That day has arrived. #452 phase 1b-i taught decode to skip the vectors the
# zone maps rule out, and taught the fold to honour the same mask, so the text
# that used to stand here -- "the fold does NOT honour the skip vector ... decode
# produces every vector" -- is no longer true of any line in the tree.
#
# What the #512 guard was for: decode gaining holes before the fold learned about
# them would have made the fold re-check UNINITIALISED memory -- a wrong
# aggregate, silently, and only on data whose zone maps rule something out. It
# was an ORDERING guard, and it fired on exactly the change it was written for.
# What survives it is a narrow fallback: a skip reported without the per-vector
# map to honour it must ERROR rather than guess.
#
# So be exact about what this suite does and does not prove, because the two are
# easy to confuse now that the guard is no longer load-bearing:
#
#   this suite          the fold is REACHABLE for a shape that skips vectors, and
#                       that it accounts for the skipping it performs
#   native_vecdecode    the fold is CORRECT there -- its poison check picks a
#                       predicate that ACCEPTS 0xA5, so a fold reading an
#                       undecoded hole counts rows that are not there
#
# Neutering the surviving fallback does NOT redden this suite, and that is now
# the expected result rather than a hole: the path it guards is unreachable while
# decode and the fold honour the same mask. Measured, not assumed.
#
# Usage:  test/native_fold_skipguard.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

ROWS=60000
psql_run "CREATE TABLE t (k int, v int) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('t'::regclass, stripe_row_limit => 60000, chunk_group_row_limit => 1024);"
psql_run "INSERT INTO t SELECT g, g FROM generate_series(1, $ROWS) g;"
psql_run "ANALYZE t;"

# Monotonic k, so each 1024-row vector has a tight non-overlapping zone map and a
# narrow range rules almost all of them out.
Q="SELECT count(*), sum(v) FROM t WHERE k BETWEEN 30000 AND 30100"

# The fold needs its GUC and no parallelism; the scalar arm is the same query
# with the GUC off, which is what reports the per-vector counter.
SET_ON="SET max_parallel_workers_per_gather=0; SET pgcolumnar.enable_ungrouped_vector_agg=on;"
SET_OFF="SET max_parallel_workers_per_gather=0; SET pgcolumnar.enable_ungrouped_vector_agg=off;"

run() {  # run <setup> <sql>
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "$1" -c "$2" 2>&1
}

# ---- the FOLD ARM does the skipping, and says so ------------------------------
#
# Read off the fold arm itself, which is the plan under test. It used to be read
# off the scalar arm with the note "same table, same predicate, same reader, so
# what it skips there it skips here" -- a proxy, and one that hid #542: the
# counter was incremented only where rows are produced, so the fold reported
# "Vectors Skipped: 0" while decoding 1 of 59 vectors. Measuring the premise on a
# plan that is not the one under test is what let that sit unnoticed.
#
# Exact values, not "greater than zero". 60,000 rows at chunk_group_row_limit
# 1024 is 59 vector positions in one group, and k BETWEEN 30000 AND 30100 lies
# inside one of them, so decode must touch exactly 1 and skip exactly 58.
#
# This is a WORK-DONE assertion, and it is the only kind that can catch a fold
# that silently stops skipping. Every other check here and in native_vecdecode is
# a correctness check, and a fold that decoded all 59 vectors would pass all of
# them: decoding everything reads real values, never a hole, so the answers stay
# right and the saving disappears in silence.
GROUP_VECS=59
fplan=$(run "$SET_ON" "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) $Q")
vskip=$(grep "Columnar Vectors Skipped" <<<"$fplan" | grep -oE '[0-9]+' | head -1)
vdec=$(grep "Columnar Vectors Decoded" <<<"$fplan" | grep -oE '[0-9]+' | head -1)
check "the fold arm decodes only the vector it needs" "${vdec:-none}" "1"
check "and reports the 58 it skipped, on its own plan (#542)" "${vskip:-none}" "58"
check "so decoded plus skipped is the group's vectors, on the FOLD arm" \
	"$(( ${vdec:-0} + ${vskip:-0} ))" "$GROUP_VECS"

# ---- the query actually reaches the fold -------------------------------------
#
# Without this the guard below is unreachable and the suite would pass forever
# while protecting nothing. This is the premise a first version of this test
# lacked, and it is why that version could not be trusted.
fold=$(run "$SET_ON" "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) $Q" \
	| grep -oE "Columnar Batch Fold: [a-z]+" | head -1)
check "the query reaches the batch fold" "$fold" "Columnar Batch Fold: yes"

# ---- and it answers correctly while decode DOES skip -----------------------
#
# The name matters. This check was called "the fold answers correctly while
# decode skips nothing", which contradicted the premise directly above it even
# when it was written, and became plainly false when #452 phase 1b-i landed.
# Decode skips 58 of 59 vectors on this query; the fold answers over the one that
# survives. The heap arithmetic is the oracle: 101 rows, sum of 30000..30100.
want_n=101
want_s=$(( (30000 + 30100) * 101 / 2 ))
got=$(run "$SET_ON" "$Q" | tail -1)
check "the fold answers correctly while decode skips 58 of 59 vectors" \
	"$got" "$want_n|$want_s"

# And the same answer without the fold, against the same arithmetic oracle. It
# used to be compared with the fold's own reading, so two failed readings agreed:
# with the server stopped before these reads, both returned the same connection
# error and this check passed (#1341). A wrong fold also reddened it, which named
# the scalar arm for the fold's fault.
got_off=$(run "$SET_OFF" "$Q" | tail -1)
check "and the scalar arm agrees" "$got_off" "$want_n|$want_s"

pgc_summary
