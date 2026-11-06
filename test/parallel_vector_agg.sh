#!/usr/bin/env bash
#
# pgColumnar parallel-aware ungrouped vectorized aggregate (#289 phase 5/6).
#
# pgcolumnar.enable_parallel_vector_agg makes the ungrouped batch fold
# parallel-aware: a parallel partial PgColumnarAgg under a core Gather + Finalize.
# Each worker claims distinct row groups through the shared gap-23 counter and
# emits per-worker transition state the Finalize combines. This suite proves:
#   (1) the plan Finalize Aggregate -> Gather -> parallel partial PgColumnarAgg is
#       ACTUALLY chosen with the GUC on and parallelism enabled;
#   (2) count(*) is exact vs a serial oracle;
#   (3) avg/sum over float4/float8 match core's OWN parallel aggregate within
#       float-reassociation tolerance -- a parallel fold sums in worker/arrival
#       order, so the oracle is core PARALLEL Agg, never the serial fold;
#   (4) all-null, empty, more-workers-than-groups, and a not-batch-foldable
#       filter (a NULL test, which runs on the row path with the shared counter).
#
# Usage:  test/parallel_vector_agg.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# Force parallel plans; both arms then differ only in the aggregate node.
PAR="SET max_parallel_workers_per_gather=4;
     SET parallel_setup_cost=0; SET parallel_tuple_cost=0;
     SET min_parallel_table_scan_size=0; SET min_parallel_index_scan_size=0;"
UG="SET pgcolumnar.enable_ungrouped_vector_agg=on;"
PP="SET pgcolumnar.enable_parallel_vector_agg=on;"

# One psql run. ON_ERROR_STOP, so a SET that fails stops the run rather than
# letting the query run under the wrong settings. psql's exit status decides: a
# failed run prints NOTHING on stdout and its first line on stderr, into the log.
#
# It printed psql's error text, and with the server stopped after the first table
# was built, main's shell passed eleven of its 29 checks: each compared one arm's
# "psql: error: ... Connection refused" with the same text from the other arm.
# Every comparison of two readings is now check_text, which refuses an empty side.
q() {
	local out
	if out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
			-d "$PGC_DB" -v ON_ERROR_STOP=1 -Atq "$@" 2>&1)"; then
		printf '%s\n' "$out"
	else
		printf 'q failed: %s\n' "$(head -1 <<<"$out")" >&2
	fi
}

# A backend that crashed is not a server that is up: after crash recovery
# "SELECT 1" answers again. With a parallel worker dying on SIGSEGV, main's
# "server still up" passed. Copied from avro_manifest.sh: the server must answer
# AND the log's "terminated by signal" count must not have moved.
crashes() {
	local _n
	_n="$(pgc_pg "grep -c 'terminated by signal' '$PGC_LOGFILE'" 2>/dev/null)"
	case "$_n" in '' | *[!0-9]*) ;; *) echo "$_n" ;; esac
}
survived() {	# survived BEFORE -> "1" when the server answers and no backend died since
	local _up _now
	_up="$(q -c 'SELECT 1')"; _now="$(crashes)"
	if ! [[ "$1" =~ ^[0-9]+$ && "$_now" =~ ^[0-9]+$ ]]; then echo "no (crash count unreadable)"
	elif [ "$_up" = 1 ] && [ "$_now" = "$1" ]; then echo 1
	else echo "no ([$_up], $(( _now - $1 )) backend crashes)"; fi
}
CRASHES_BEFORE="$(crashes)"

# ~2M rows, small stripe -> many row groups so several workers each claim some.
q -c "DROP TABLE IF EXISTS t;
      CREATE TABLE t (id int, k int, v float8, w float4) USING pgcolumnar;
      SELECT pgcolumnar.set_options('t'::regclass, stripe_row_limit => 40000);
      INSERT INTO t
      SELECT g, g % 1000,
             CASE WHEN g % 50 = 0 THEN NULL
                  ELSE ((g % 13) - 6)::float8 * (10.0 ^ (g % 4)) END,
             ((g % 7) - 3)::float4
      FROM generate_series(1, 2000000) g;
      ANALYZE t;" >/dev/null

# ---- the parallel plan is actually chosen ----------------------------------
PLAN="$(q -c "$PAR $UG $PP" -c "EXPLAIN (COSTS OFF) SELECT count(*), avg(v), sum(v) FROM t WHERE k < 700")"
sed 's/^/      /' <<<"$PLAN"
# Not premises (#1339, dated 2026-10-22): the parallel partial path is pgcolumnar's
# own, and never adding it reddens both.
check "Finalize Aggregate present" \
   "$(grep -qi 'Finalize Aggregate' <<<"$PLAN" && echo y || echo n)" y
check "Gather present" \
   "$(grep -qiE 'Gather' <<<"$PLAN" && echo y || echo n)" y
# Our node, and the PARALLEL one: the serial node also prints "Columnar Vectorized
# Aggregates", so with the parallel partial path never added main's version of this
# check passed. Not a premise (#1339, dated 2026-10-22): that mutant reddens it.
check "the partial columnar agg node present" \
   "$(grep -qi 'Parallel Custom Scan (PgColumnarScan)' <<<"$PLAN" &&
      grep -qi 'Columnar Vectorized Aggregates' <<<"$PLAN" && echo y || echo n)" y
# Not a premise (#1339, dated 2026-10-22): a batch-fold shape gate that always
# refuses reddens it.
check "the partial runs the batch fold" \
   "$(grep -qi 'Batch Fold: yes' <<<"$PLAN" && echo y || echo n)" y

# ---- values: count exact, avg/sum vs core PARALLEL agg within tolerance -----
CNT_VEC="$(q -c "$PAR $UG $PP" -c "SELECT count(*) FROM t WHERE k < 700")"
CNT_SER="$(q -c "SET max_parallel_workers_per_gather=0;" -c "SELECT count(*) FROM t WHERE k < 700")"
check_text "count(*) parallel-vec == serial oracle" "$CNT_VEC" "$CNT_SER"

# relative-difference gate in SQL: |vec-core|/(|core|+1) < 1e-6
reldiff() { # reldiff <expr>
	local vec core
	vec="$(q -c "$PAR $UG $PP" -c "SELECT $1 FROM t WHERE k < 700")"
	core="$(q -c "$PAR" -c "SET pgcolumnar.enable_ungrouped_vector_agg=off;" \
	           -c "SELECT $1 FROM t WHERE k < 700")"
	if [ -z "$vec" ] || [ -z "$core" ]; then echo "no reading (vec=[$vec] core=[$core])"; return; fi
	q -c "SELECT CASE WHEN abs(($vec::float8)-($core::float8)) <= 1e-6*(abs($core::float8)+1)
	                  THEN 'ok' ELSE 'DIFF vec=$vec core=$core' END"
}
check "avg(v) parallel-vec ~= core parallel agg"  "$(reldiff 'avg(v)')" ok
check "sum(v) parallel-vec ~= core parallel agg"  "$(reldiff 'sum(v)')" ok
check "avg(w::float8) f4 ~= core parallel agg"    "$(reldiff 'avg(w)::float8')" ok

# int sum/avg (#289 phase 5/6): sum(int)->int8, avg(int)->numeric. Integer sums
# and numeric division have no float reassociation, so the parallel fold must
# equal the serial oracle EXACTLY, and the plan must be the parallel fold.
PLAN_I="$(q -c "$PAR $UG $PP" -c "EXPLAIN (COSTS OFF) SELECT sum(k), avg(k) FROM t WHERE k < 700")"
# Not a premise (#1339, dated 2026-10-22): never adding the parallel partial path
# reddens it.
check "int sum/avg takes the parallel fold" \
   "$(grep -qiE 'Gather' <<<"$PLAN_I" && grep -qi 'Batch Fold: yes' <<<"$PLAN_I" && echo y || echo n)" y
IK_VEC="$(q -c "$PAR $UG $PP" -c "SELECT sum(k), avg(k) FROM t WHERE k < 700")"
IK_SER="$(q -c "SET max_parallel_workers_per_gather=0;" -c "SELECT sum(k), avg(k) FROM t WHERE k < 700")"
check_text "sum(k)+avg(k) int: parallel fold == serial (exact)" "$IK_VEC" "$IK_SER"

# ---- edge cases ------------------------------------------------------------
# a NULL test is not batch-foldable: the partial must still be correct on the
# row path (shape ineligible from the start -> shared counter untouched).
AN_AVG="$(q -c "$PAR $UG $PP" -c "SELECT coalesce(avg(v)::text,'NULLZ') FROM t WHERE k < 700 AND v IS NULL")"
check "not-batch-foldable filter: all-null avg is NULL" "$AN_AVG" "NULLZ"
AN_CNT="$(q -c "$PAR $UG $PP" -c "SELECT count(*) FROM t WHERE k < 700 AND v IS NULL")"
AN_CNT2="$(q -c "SET max_parallel_workers_per_gather=0;" -c "SELECT count(*) FROM t WHERE k<700 AND v IS NULL")"
check_text "not-batch-foldable filter: count(*) exact" "$AN_CNT" "$AN_CNT2"
# empty
check "empty count(*) == 0"  "$(q -c "$PAR $UG $PP" -c "SELECT count(*) FROM t WHERE k < 0")" "0"
check "empty avg is NULL"    "$(q -c "$PAR $UG $PP" -c "SELECT coalesce(avg(v)::text,'NULLZ') FROM t WHERE k < 0")" "NULLZ"
# more workers than groups: 8 workers over a tiny 3-group table, small ints so exact
q -c "DROP TABLE IF EXISTS tiny;
      CREATE TABLE tiny (v float8) USING pgcolumnar;
      SELECT pgcolumnar.set_options('tiny'::regclass, stripe_row_limit => 1000);
      INSERT INTO tiny SELECT g::float8 FROM generate_series(1,2500) g; ANALYZE tiny;" >/dev/null
TW_VEC="$(q -c "SET max_parallel_workers_per_gather=8; SET parallel_setup_cost=0; SET parallel_tuple_cost=0; SET min_parallel_table_scan_size=0; $UG $PP" -c "SELECT count(*), sum(v) FROM tiny")"
TW_SER="$(q -c "SET max_parallel_workers_per_gather=0;" -c "SELECT count(*), sum(v) FROM tiny")"
check_text "more-workers-than-groups count+sum exact" "$TW_VEC" "$TW_SER"

# ---- deletes: a worker is a separate backend (design hazard H2) -------------
# The batch fold applies each group's delete mask, and the partial node flushes
# write + delete state in the LEADER's InitializeDSM before workers launch -- a
# worker cannot see the leader's unflushed in-transaction delete buffer. If that
# flush were missing the workers would count deleted rows and diverge from serial.
q -c "DELETE FROM t WHERE k IN (1,2,3);" >/dev/null
D_VEC="$(q -c "$PAR $UG $PP" -c "SELECT count(*), sum(v) FROM t WHERE k < 700")"
D_SER="$(q -c "SET max_parallel_workers_per_gather=0;" -c "SELECT count(*), sum(v) FROM t WHERE k < 700")"
check_text "committed deletes: parallel fold == serial" "$D_VEC" "$D_SER"

# THE H2 CASE: delete more rows and run the parallel fold in the SAME transaction
# so the deletes are still unflushed in the leader's buffer when workers launch.
#
# psql's exit status is read, and both arms must print. A parallel fold that
# raised an ERROR here aborted the transaction, the serial SELECT then failed
# too, both arms read empty, and main's shell passed: measured with both parallel
# InitializeDSM callbacks raising an ERROR in a transaction that holds an XID.
H2="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
	-v ON_ERROR_STOP=1 -Atq 2>&1 <<SQL
BEGIN;
DELETE FROM t WHERE k IN (10,11,12,13,14);
$PAR $UG $PP
SELECT count(*), sum(v) FROM t WHERE k < 700;
SET max_parallel_workers_per_gather=0;
SELECT count(*), sum(v) FROM t WHERE k < 700;
ROLLBACK;
SQL
)"
H2_RC=$?
H2_ROWS="$(printf '%s\n' "$H2" | grep -E '^[0-9]+\|')"
if [ "$H2_RC" -eq 0 ] && [ "$(grep -c . <<<"$H2_ROWS")" -eq 2 ]; then
	H2_PAR="$(sed -n 1p <<<"$H2_ROWS")"; H2_SER="$(sed -n 2p <<<"$H2_ROWS")"
else
	H2_PAR=""; H2_SER=""
	printf 'H2 run failed (psql exit %s): %s\n' "$H2_RC" "$(grep -m1 -E 'ERROR|error' <<<"$H2")"
fi
check_text "in-xact deletes (H2): parallel fold == serial" "$H2_PAR" "$H2_SER"

# ---- #349: the GROUPED vectorized fold is parallel-aware too ----------------
# The grouped node used to be serial by construction (parallel_aware = false, no
# DSM callbacks), so whenever it won it replaced a four-worker plan with a
# single-threaded one -- a 1.92x regression on a full-scan GROUP BY with few
# groups. It now plans Finalize HashAggregate -> Gather -> parallel-aware partial
# grouped node: each worker claims distinct row groups through the same gap-23
# counter, builds its OWN hash table over them, and emits one (group keys,
# transition states) tuple per group for the core Finalize to combine by key.
GVP="SET pgcolumnar.enable_group_vectorization=on;
     SET pgcolumnar.enable_parallel_vector_agg=on;"
# The oracles are core's own grouped aggregate. With max_parallel_workers_per_gather
# = 0 alone, the "serial" arm ran pgcolumnar's grouped fold serially (it is on by
# default), so a fold bug both arms share went unseen: with the grouped batch fold
# ignoring the delete mask, main's grouped H2 check passed.
GSER="SET max_parallel_workers_per_gather=0; SET pgcolumnar.enable_group_vectorization=off;"
GCORE="$PAR SET pgcolumnar.enable_group_vectorization=off;"

q -c "DROP TABLE IF EXISTS gt;
      CREATE TABLE gt (id int, h text, k int, v float8, w int) USING pgcolumnar;
      SELECT pgcolumnar.set_options('gt'::regclass, stripe_row_limit => 20000);
      INSERT INTO gt
      SELECT g, 'h' || (g % 50), g % 200,
             CASE WHEN g % 50 = 0 THEN NULL
                  ELSE ((g % 13) - 6)::float8 * (10.0 ^ (g % 4)) END,
             g % 13
      FROM generate_series(1, 800000) g;
      ANALYZE gt;" >/dev/null

# Assert the fixture before comparing anything: two arms that both error return
# empty and compare equal, which is a green check that tested nothing.
# Not a premise (#1339, dated 2026-10-22): the rows are pgcolumnar's own writes, and
# an insert that drops one row reddens it.
check "the grouped fixture has its rows" \
   "$(q -c 'SELECT count(*) FROM gt')" 800000

# Every assertion here is made against ONE EXPLAIN ANALYZE, and each names a
# property core's own parallel grouped plan does NOT have. That distinction is
# the whole point: without the change core still plans
# "Finalize GroupAggregate -> Gather Merge -> Sort -> Partial HashAggregate" over
# the same table, which has a Gather and launches workers too. Checking only for
# "a Gather" or "workers launched" therefore passes on unmodified main and proves
# nothing -- both did, until this was tightened.
GEA="$(q -c "$PAR $GVP" -c "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF)
        SELECT h, count(*), sum(w) FROM gt GROUP BY h")"
sed 's/^/      /' <<<"$GEA"

# Not premises (#1339, dated 2026-10-22): the grouped parallel partial path is
# pgcolumnar's own, and never adding it reddens all three.

# A HashAggregate finalize, not core's GroupAggregate-over-Sort.
check "grouped Finalize HashAggregate present (#349)" \
   "$(grep -qi 'Finalize HashAggregate' <<<"$GEA" && echo y || echo n)" y
# OUR grouped node, and it is the parallel-aware one.
check "the partial grouped node is under the Gather (#349)" \
   "$(grep -qi 'Parallel Custom Scan (PgColumnarScan)' <<<"$GEA" &&
      grep -qi 'Columnar Vectorized Group Keys' <<<"$GEA" && echo y || echo n)" y
# Planned is not launched: a leader-only run would satisfy every value check
# below while never exercising a worker. Assert launch AND our node together, so
# core's parallel plan cannot satisfy this on its own.
check "workers actually launched for OUR grouped node (#349)" \
   "$(grep -qiE 'Workers Launched: [1-9]' <<<"$GEA" &&
      grep -qi 'Columnar Vectorized Group Keys' <<<"$GEA" && echo y || echo n)" y

# Each participant emits its own partial per group, so the Gather carries a
# MULTIPLE of the group count and the Finalize collapses it back. 50 groups with
# workers launched means the Gather must show strictly more than 50 rows; core's
# partial-HashAggregate plan shows the same shape, so this is paired with the
# node checks above rather than standing alone.
GATHER_ROWS="$(printf '%s\n' "$GEA" | grep -iE '^ *-> *Gather' | grep -oE 'rows=[0-9.]+' | head -1 | cut -d= -f2 | cut -d. -f1)"
check "the Gather carries per-worker partials, more than one row per group (#349)" \
   "$( [ -n "$GATHER_ROWS" ] && [ "$GATHER_ROWS" -gt 50 ] && echo y ||
       echo "n (gather rows=${GATHER_ROWS:-unset})")" y
check "the Finalize collapses them back to exactly the group count (#349)" \
   "$(q -c "$PAR $GVP" -c "SELECT count(*) FROM (SELECT h, count(*), sum(w) FROM gt GROUP BY h) s")" 50

# ---- values: integer aggregates are exact against a serial oracle -----------
G_VEC="$(q -c "$PAR $GVP" -c "SELECT h, count(*), sum(w) FROM gt WHERE k < 150 GROUP BY h ORDER BY h")"
G_SER="$(q -c "$GSER" -c "SELECT h, count(*), sum(w) FROM gt WHERE k < 150 GROUP BY h ORDER BY h")"
check_text "grouped count+sum(int): parallel fold == serial oracle (#349)" "$G_VEC" "$G_SER"

GM_VEC="$(q -c "$PAR $GVP" -c "SELECT h, k, count(*), sum(w) FROM gt WHERE v IS NOT NULL GROUP BY h, k ORDER BY h, k")"
GM_SER="$(q -c "$GSER" -c "SELECT h, k, count(*), sum(w) FROM gt WHERE v IS NOT NULL GROUP BY h, k ORDER BY h, k")"
check_text "grouped multi-key + WHERE: parallel fold == serial oracle (#349)" "$GM_VEC" "$GM_SER"

# ---- float: oracle is core's own PARALLEL agg, for the reason above ---------
GF_VEC="$(q -c "$PAR $GVP" -c "SELECT h, round(avg(v)::numeric,6), round(sum(v)::numeric,6) FROM gt GROUP BY h ORDER BY h")"
GF_PAR="$(q -c "$GCORE" -c "SELECT h, round(avg(v)::numeric,6), round(sum(v)::numeric,6) FROM gt GROUP BY h ORDER BY h")"
check_text "grouped avg/sum(float8): parallel fold == core parallel agg (#349)" "$GF_VEC" "$GF_PAR"

# ---- a group count below the worker count: some workers see every group -----
GW_VEC="$(q -c "$PAR $GVP SET max_parallel_workers_per_gather=8;" -c "SELECT k % 3 AS g, count(*), sum(w) FROM gt GROUP BY 1 ORDER BY 1")"
GW_SER="$(q -c "$GSER" -c "SELECT k % 3 AS g, count(*), sum(w) FROM gt GROUP BY 1 ORDER BY 1")"
check_text "grouped few-groups-many-workers: parallel == serial (#349)" "$GW_VEC" "$GW_SER"

# ---- H2 for the grouped node: in-transaction deletes -------------------------
# The grouped node runs the same shape as the ungrouped H2 case above: delete
# inside a transaction, then aggregate in parallel in that same transaction, and
# require the parallel answer to match the serial one.
#
# What this does NOT establish, measured rather than assumed: it does not prove
# the leader-side flush in PgColumnarInitializeDSMGroupAggScan. Removing that flush
# -- and the ungrouped one this is modelled on -- leaves both H2 checks green,
# with in-transaction INSERT and DELETE and a confirmed parallel plan, because
# the write and delete buffers are already flushed at the command boundary before
# the aggregate is planned. The flush is kept as defence and for symmetry with
# the ungrouped node, not because anything here fails without it. Stated so the
# next person does not read a passing H2 check as cover for that flush.
#
# The exit status and the row count are read, as for the ungrouped H2 case, and
# the serial arm is core's grouped aggregate (GSER), not pgcolumnar's fold.
GH2="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
	-v ON_ERROR_STOP=1 -Atq 2>&1 <<SQL
BEGIN;
DELETE FROM gt WHERE k IN (20,21,22,23,24);
$PAR $GVP
SELECT h, count(*), sum(w) FROM gt GROUP BY h ORDER BY h;
$GSER
SELECT h, count(*), sum(w) FROM gt GROUP BY h ORDER BY h;
ROLLBACK;
SQL
)"
GH2_RC=$?
GH2_ROWS="$(printf '%s\n' "$GH2" | grep -E '^h[0-9]+\|')"
if [ "$GH2_RC" -eq 0 ] && [ "$(grep -c . <<<"$GH2_ROWS")" -eq 100 ]; then
	GH2_PAR="$(sed -n '1,50p' <<<"$GH2_ROWS")"; GH2_SER="$(sed -n '51,100p' <<<"$GH2_ROWS")"
else
	GH2_PAR=""; GH2_SER=""
	printf 'grouped H2 run failed (psql exit %s): %s\n' "$GH2_RC" "$(grep -m1 -E 'ERROR|error' <<<"$GH2")"
fi
check_text "grouped in-xact deletes (H2): parallel fold == serial (#349)" "$GH2_PAR" "$GH2_SER"

check "server still up" "$(survived "$CRASHES_BEFORE")" 1
pgc_summary
