#!/usr/bin/env bash
#
# pgColumnar #289: grouped vectorized aggregate.
#
# The grouped path fires for SELECT <keys>, agg(col) ... [WHERE ...] GROUP BY
# <keys> over one columnar relation. It reads each surviving row with
# PgColumnarReadNextRow (WHERE pushed down for group/vector skipping), rechecks the
# full WHERE, evaluates the group keys, and scatters the row into an
# open-addressing hash table whose per-group accumulators fold in scan order.
#
# Two oracles prove it:
#
#   * Heap mirror. Every query runs against a heap table with identical data.
#     Exact aggregates (count, integer/numeric sums, min/max) compare byte-exact;
#     float sums/averages compare rounded, because float summation order (not the
#     grouping) is all that can differ and that is the executor, not a defect.
#
#   * Toggle-differential. The same query runs against the columnar table with
#     the path off (scalar Agg over the columnar scan) and on. Both read the same
#     rows in the same order, so exact aggregates must be byte-identical -- this
#     is what validates the order-preserving accumulators.
#
# Plan assertions confirm the node is actually chosen when supported and that
# every unsupported shape (group count over the cap, non-deterministic collation,
# an output built on a key, no aggregate) falls back to the ordinary Agg while
# still producing the oracle's answer.
#
# Runs on an assert server too, so a bad read or a mis-sized accumulator also
# trips a backend assertion under the oracle data.
#
# Usage:  test/native_groupagg.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# The grouped path is opt-in; set it at the database level so every new psql
# connection the oracle helpers open picks it up.
groupvec_on()  { psql_run "ALTER DATABASE $PGC_DB SET pgcolumnar.enable_group_vectorization = on;"; }
groupvec_off() { psql_run "ALTER DATABASE $PGC_DB SET pgcolumnar.enable_group_vectorization = off;"; }

# Is this query planned as the grouped vectorized node? Its own marker line is
# "Columnar Vectorized Group Keys"; no other node emits it, so a positive grep is
# proof of the node rather than an absence test that a fallback would also pass.
pgc_is_groupvec() {	# query -> yes|no
	# grep -c on a captured plan, not a pipe into grep -q; see lib.sh's
	# pgc_is_columnar_scan for the mechanism and the measurement.
	local _plan
	_plan="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "EXPLAIN (COSTS OFF) $1" 2>/dev/null)"
	[ "$(grep -c 'Columnar Vectorized Group Keys' <<<"$_plan" || true)" -ne 0 ] \
		&& echo yes || echo no
}

# toggle_diff LABEL "QUERY on t_col": same query, path off vs on, byte-exact.
# Asserts the node actually fires with the GUC on -- without that premise a query
# the node quietly rejects runs the scalar Agg in both arms and the comparison is
# vacuously green (exactly how the float-sum defect slipped through before).
toggle_diff() {
	local label="$1" query="$2" h_off h_on
	groupvec_on
	check "$label [node fires]" "$(pgc_is_groupvec "$query")" yes
	groupvec_off
	h_off="$(pgc_set_hash "$query")"
	groupvec_on
	h_on="$(pgc_set_hash "$query")"
	check "$label" "$h_on" "$h_off"
}

# oracle LABEL "TEMPLATE with %T": assert the grouped node is chosen for the
# columnar form, then compare it to the heap oracle. The node-fires assertion is
# the premise every comparison here needs and none used to state.
oracle() {
	local label="$1" tmpl="$2"
	check "$label [node fires]" "$(pgc_is_groupvec "${tmpl//%T/t_col}")" yes
	diff_query "$label" "$tmpl"
}

# ---- 1. the data: a q4/q5-shaped table with many group shapes ---------------

make_pair "g       int,
	ts      timestamp,
	host    text,
	region  text,
	i2      smallint,
	i4      int,
	i8      bigint,
	f4      real,
	f8      double precision,
	num     numeric"
# small row groups so grouping spans many groups and vectors
psql_run "SELECT pgcolumnar.set_options('t_col', stripe_row_limit => 1000);"

# 20000 rows: 50 hosts, 4 regions, timestamps spanning ~14 hours, values with
# interleaved NULLs in both keys and measures (a NULL host/ts must form its own
# group, exactly as GROUP BY does).
load_pair "SELECT g,
	CASE WHEN g % 331 = 0 THEN NULL
	     ELSE timestamp '2024-01-01 00:00:00' + (g * interval '25 seconds') END,
	CASE WHEN g % 197 = 0 THEN NULL ELSE 'host_' || (g % 50) END,
	'region_' || (g % 4),
	CASE WHEN g % 11 = 0 THEN NULL ELSE (g % 97 - 48)::smallint END,
	CASE WHEN g %  7 = 0 THEN NULL ELSE (g * 7 - 3) END,
	CASE WHEN g %  5 = 0 THEN NULL ELSE (g::bigint * 1000003 - 5) END,
	CASE WHEN g %  6 = 0 THEN NULL ELSE (g * 1.5)::real END,
	CASE WHEN g %  9 = 0 THEN NULL ELSE (g::float8 * 0.125 - 3.5) END,
	CASE WHEN g %  8 = 0 THEN NULL ELSE (g * 0.01)::numeric(12,4) END
	FROM generate_series(1, 20000) g"

# ---- 2. plan assertions: the node is chosen for the supported shapes --------

groupvec_on
Q_KEYHOST="SELECT host, count(*), sum(i4) FROM t_col GROUP BY host"
Q_KEYHOUR="SELECT date_trunc('hour', ts) h, count(*) FROM t_col GROUP BY date_trunc('hour', ts)"
Q_Q4="SELECT date_trunc('hour', ts) h, host, avg(f8), count(*)
      FROM t_col WHERE ts >= timestamp '2024-01-01 02:00:00'
                   AND ts <  timestamp '2024-01-01 06:00:00'
      GROUP BY date_trunc('hour', ts), host"
check "plan: GROUP BY text key uses grouped node"      "$(pgc_is_groupvec "$Q_KEYHOST")" yes
check "plan: GROUP BY hour expr uses grouped node"     "$(pgc_is_groupvec "$Q_KEYHOUR")" yes
check "plan: q4-shape (WHERE+2 keys) uses grouped node" "$(pgc_is_groupvec "$Q_Q4")"    yes
groupvec_off
check "plan: off -> ordinary Agg, not grouped node"    "$(pgc_is_groupvec "$Q_KEYHOST")" no
groupvec_on

# ---- 3. heap oracle: exact aggregates, every group shape -------------------
# GUC on, so t_col runs the grouped path; t_heap is the reference.

EXACT="count(*), count(i4), count(host),
	sum(i2), sum(i4), sum(i8), sum(num),
	min(f8), max(f8), min(host), max(host), min(ts), max(ts), min(i8), max(i8)"

oracle "oracle exact: GROUP BY host" \
	"SELECT host, $EXACT FROM %T GROUP BY host"
oracle "oracle exact: GROUP BY hour" \
	"SELECT date_trunc('hour', ts), $EXACT FROM %T GROUP BY date_trunc('hour', ts)"
oracle "oracle exact: GROUP BY hour, host (q4/q5 shape)" \
	"SELECT date_trunc('hour', ts), host, $EXACT FROM %T GROUP BY date_trunc('hour', ts), host"
oracle "oracle exact: GROUP BY region, host (two text keys)" \
	"SELECT region, host, $EXACT FROM %T GROUP BY region, host"
oracle "oracle exact: GROUP BY integer expr key" \
	"SELECT (g % 7), $EXACT FROM %T GROUP BY (g % 7)"
oracle "oracle exact: GROUP BY smallint column key" \
	"SELECT i2, count(*), sum(i4), min(f8), max(f8) FROM %T GROUP BY i2"

# with a WHERE that both prunes groups and needs a residual recheck
oracle "oracle exact: WHERE range + GROUP BY hour, host" \
	"SELECT date_trunc('hour', ts), host, $EXACT FROM %T
	 WHERE ts >= timestamp '2024-01-01 02:00:00'
	   AND ts <  timestamp '2024-01-01 06:00:00'
	 GROUP BY date_trunc('hour', ts), host"
oracle "oracle exact: WHERE on measure + GROUP BY host" \
	"SELECT host, $EXACT FROM %T WHERE i4 > 40000 AND host IS NOT NULL GROUP BY host"

# ---- 4. float and average accumulators, through the node --------------------
# These are the accumulators the earlier suite left uncovered: rounding an
# aggregate IN the SELECT list makes the output an expression over an aggregate,
# which the node rejects, so both arms silently ran the scalar Agg and the check
# was vacuous (that is how sum(real) returning 0 got through). Cover them by a
# toggle-differential of the BARE aggregates: the GUC-off arm is core's Agg over
# the same columnar rows in the same fold order, so the node's result must be
# byte-identical -- and toggle_diff now asserts the node actually fires.

FLOATAGG="sum(f4), sum(f8), sum(num),
	avg(i2), avg(i4), avg(i8), avg(f4), avg(f8), avg(num)"

toggle_diff "toggle float/avg: GROUP BY host" \
	"SELECT host, $FLOATAGG FROM t_col GROUP BY host"
toggle_diff "toggle float/avg: GROUP BY hour, host" \
	"SELECT date_trunc('hour', ts), host, $FLOATAGG FROM t_col GROUP BY date_trunc('hour', ts), host"
toggle_diff "toggle float/avg: WHERE + GROUP BY region, host" \
	"SELECT region, host, $FLOATAGG FROM t_col WHERE f8 IS NOT NULL GROUP BY region, host"

# min/max tie-break: numeric values equal by value but differing in display scale
# (1.0 vs 1.00). Core's larger/smaller keep the later value on a tie; the node
# must match. Order-sensitive, so tested as a toggle-differential (same fold
# order in both arms), not against heap (which scans in a different order).
psql_run "DROP TABLE IF EXISTS tie_col;
          CREATE TABLE tie_col (k int, n numeric) USING pgcolumnar;
          INSERT INTO tie_col VALUES
            (1, 1.0), (1, 1.00), (1, 1.000), (1, 0.5), (1, 0.50),
            (2, 5.5), (2, 5.50), (2, 5.500);" >/dev/null
toggle_diff "toggle min/max numeric display-scale tie" \
	"SELECT k, min(n), max(n) FROM tie_col GROUP BY k"

# ---- 5. toggle-differential: exact accumulators, off vs on -----------------

toggle_diff "toggle exact: GROUP BY host" \
	"SELECT host, $EXACT FROM t_col GROUP BY host"
toggle_diff "toggle exact: GROUP BY hour, host" \
	"SELECT date_trunc('hour', ts), host, $EXACT FROM t_col GROUP BY date_trunc('hour', ts), host"
toggle_diff "toggle exact: GROUP BY region, host, WHERE" \
	"SELECT region, host, $EXACT FROM t_col WHERE i8 IS NOT NULL GROUP BY region, host"

# ---- 6. adversarial: NULL keys, deletes, ADD COLUMN ------------------------

groupvec_on
# NULL keys already present (g%197, g%331); confirm the node is still used and
# the NULL group matches the oracle.
oracle "oracle exact: NULL ts group folds like heap" \
	"SELECT date_trunc('hour', ts), count(*), sum(i4) FROM %T GROUP BY date_trunc('hour', ts)"

psql_run "DELETE FROM t_heap WHERE g % 17 = 0;"
psql_run "DELETE FROM t_col  WHERE g % 17 = 0;"
oracle "oracle exact: GROUP BY host after deletes" \
	"SELECT host, $EXACT FROM %T GROUP BY host"
toggle_diff "toggle exact: GROUP BY host after deletes" \
	"SELECT host, $EXACT FROM t_col GROUP BY host"

psql_run "ALTER TABLE t_heap ADD COLUMN extra int DEFAULT 7;"
psql_run "ALTER TABLE t_col  ADD COLUMN extra int DEFAULT 7;"
oracle "oracle exact: GROUP BY host, added column" \
	"SELECT host, count(*), sum(extra), sum(i4) FROM %T GROUP BY host"
oracle "oracle exact: GROUP BY added column" \
	"SELECT extra, count(*), sum(i4) FROM %T GROUP BY extra"

# ---- 7. fallback shapes: not the node, still the right answer ---------------

# Since #1292 phase 2 the planner takes the node only for an estimate of at most a
# tenth of pgcolumnar.groupagg_max_groups, so the runtime cap is reached only when
# the estimate is wrong; section 10 proves that path with a stale-statistics
# fixture. (Before the gate this section forced the node with hashagg and sort off
# under a cap of 100; the gate now declines that plan, which is the point of it.)
check "plan: the node is chosen for a key well under the gate" \
	"$(pgc_is_groupvec "SELECT g, count(*) FROM t_col GROUP BY g")" yes
diff_query "oracle exact: default cap runs the high-cardinality key" \
	"SELECT g, count(*), sum(i4) FROM %T GROUP BY g"

# non-deterministic collation key -> falls back
psql_run "DROP TABLE IF EXISTS t_ci;"
psql_run "CREATE COLLATION IF NOT EXISTS ci (provider = icu, locale = 'und-u-ks-level2', deterministic = false);" \
	2>/dev/null || true
if [ "$(q "SELECT 1 FROM pg_collation WHERE collname = 'ci'")" = "1" ]; then
	psql_run "CREATE TABLE t_ci (k text COLLATE ci, v int) USING pgcolumnar;"
	# Keys where case is DECOUPLED from the suffix: case comes from g%2, the
	# suffix from (g/2)%10, so for every suffix 0..9 both 'host'N and 'HOST'N
	# exist -- 20 byte-distinct keys that case-fold to 10. A case-insensitive
	# (level-2) collation yields 10 groups; a byte-equality path (which the node
	# uses, and why it must fall back here) would yield 20. So this fixture fails
	# if the fallback ever stops happening. (An earlier version used g%20, whose
	# parity tracked g's, so nothing case-folded and the check was vacuous.)
	psql_run "INSERT INTO t_ci
	          SELECT (CASE WHEN g % 2 = 0 THEN 'host' ELSE 'HOST' END) || ((g / 2) % 10), g
	          FROM generate_series(1, 5000) g;"
	check "plan: non-deterministic collation key falls back" \
		"$(pgc_is_groupvec "SELECT k, count(*) FROM t_ci GROUP BY k")" no
	# THE QUERY THE PLAN ARM ASKS ABOUT (#1339). A GROUP BY with no aggregate always
	# falls back, so the old answer compared core with core. The rows of the grouped
	# query are counted instead, against the 10 the name states and the folded count.
	check "answer: non-deterministic collation grouping is case-insensitive (10 groups)" \
		"$(q "SELECT k, count(*) FROM t_ci GROUP BY k" | grep -c .)|$(q "SELECT count(DISTINCT lower(k)) FROM t_ci")" \
		"10|10"
else
	check_skip "the non-deterministic collation case" "SKIP  non-deterministic collation (ICU unavailable)" "ICU unavailable"
fi

# an output expression built on a group key (not a bare key) -> falls back
check "plan: f(key) output falls back" \
	"$(pgc_is_groupvec "SELECT upper(host), count(*) FROM t_col GROUP BY host")" no
diff_query "oracle exact: f(key) output still correct" \
	"SELECT upper(host), count(*), sum(i4) FROM %T GROUP BY host"

# GROUP BY with no aggregate -> falls back (nothing to vectorize)
check "plan: GROUP BY with no aggregate falls back" \
	"$(pgc_is_groupvec "SELECT host FROM t_col GROUP BY host")" no

# ---- 7b. named regressions for the two reproduced wrong-answer blockers -----

# Blocker 1: sum(real) once returned 0 -- a float8 Datum handed to a float4 slot.
# With integer-valued reals the sum is exact regardless of fold order, so compare
# to heap directly; require the node and require the result be nonzero.
psql_run "DROP TABLE IF EXISTS r_col; DROP TABLE IF EXISTS r_heap;"
psql_run "CREATE TABLE r_col (h int, r real, d float8) USING pgcolumnar;
          CREATE TABLE r_heap (h int, r real, d float8);
          INSERT INTO r_heap SELECT g % 4, (g % 7)::real, (g % 7)::float8
                             FROM generate_series(1, 4000) g;
          INSERT INTO r_col SELECT * FROM r_heap;" >/dev/null
check "regress B1: sum(real) fires the node" \
	"$(pgc_is_groupvec "SELECT h, sum(r) FROM r_col GROUP BY h")" yes
check "regress B1: sum(real) matches heap, not 0" \
	"$(pgc_set_hash "SELECT h, sum(r), sum(d) FROM r_col GROUP BY h")" \
	"$(pgc_set_hash "SELECT h, sum(r), sum(d) FROM r_heap GROUP BY h")"
check "regress B1: sum(real) is nonzero" \
	"$(q "SELECT bool_and(s <> 0) FROM (SELECT sum(r) s FROM r_col GROUP BY h) x")" t

# Blocker 2: a gating (pseudoconstant, one-time) WHERE was dropped, so a false
# gate wrongly returned rows. The node cannot honor a one-time filter, so it must
# fall back; either way the answer must match heap.
check "regress B2: gating WHERE falls back (not the node)" \
	"$(pgc_is_groupvec "SELECT host, count(*) FROM t_col WHERE (SELECT false) GROUP BY host")" no
diff_query "regress B2: gating WHERE (SELECT false) returns no rows" \
	"SELECT host, count(*) FROM %T WHERE (SELECT false) GROUP BY host"
diff_query "regress B2: gating WHERE (SELECT true) is a no-op" \
	"SELECT host, count(*), sum(i4) FROM %T WHERE (SELECT true) GROUP BY host"

# Signed zero: core's sum assigns the first value directly, so sum of a lone -0.0
# prints '-0'. Folding it into +0.0 would drop the sign. Toggle-diff over columnar
# rows containing -0.0 (node vs core's scalar Agg, same fold order) is byte-exact.
psql_run "DROP TABLE IF EXISTS z_col;
          CREATE TABLE z_col (k int, r real, d float8) USING pgcolumnar;
          INSERT INTO z_col VALUES (1,'-0','-0'), (2,'-0','-0'), (2,'-0','-0');" >/dev/null
toggle_diff "toggle sum signed-zero (-0 preserved)" \
	"SELECT k, sum(r), sum(d) FROM z_col GROUP BY k"

# avg overflow parity: core's float accum keeps Youngs-Cramer Sxx and raises on
# finite inputs whose sum-of-squares overflows even if the running sum stays
# finite. The node must raise too. (diff_query can't compare two errors, so check
# the node fires and errors with the same message.)
psql_run "DROP TABLE IF EXISTS ov_col;
          CREATE TABLE ov_col (k int, d float8) USING pgcolumnar;
          INSERT INTO ov_col VALUES (1, -1e308), (1, 1e308);" >/dev/null
check "avg(float8) overflow: node fires" \
	"$(pgc_is_groupvec "SELECT k, avg(d) FROM ov_col GROUP BY k")" yes
ov_out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
	-d "$PGC_DB" -Atc "SET pgcolumnar.enable_group_vectorization=on;
		SELECT k, avg(d) FROM ov_col GROUP BY k" 2>&1 || true)"
check "avg(float8) overflow errors like core" \
	"$(grep -qi 'out of range' <<<"$ov_out" && echo yes || echo no)" yes

# sum overflow parity (#1341 item 50): two finite float8s, or two finite reals, whose
# sum is infinite raise "value out of range: overflow" (22003) in core's float8pl and
# float4pl. The node's accumulators must raise it too rather than return Infinity.
# Judged by SQLSTATE, against core with the node turned off.
psql_run "DROP TABLE IF EXISTS ovs_col;
          CREATE TABLE ovs_col (k int, d float8, r real) USING pgcolumnar;
          INSERT INTO ovs_col VALUES (1, 1e308, 3e38), (1, 1e308, 3e38);" >/dev/null
gv_state() {	# gv_state on|off QUERY -> ok, or the SQLSTATE the query raised
	local out
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-X -At -v ON_ERROR_STOP=1 -v VERBOSITY=sqlstate \
		-c "SET pgcolumnar.enable_group_vectorization = $1;" -c "$2" 2>&1 >/dev/null)"
	[ -z "$out" ] && { echo ok; return; }
	sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\).*/\1/p' <<<"$out" | head -1
}
check "sum(float8) and sum(real) overflow: node fires" \
	"$(pgc_is_groupvec "SELECT k, sum(d) FROM ovs_col GROUP BY k")|$(pgc_is_groupvec "SELECT k, sum(r) FROM ovs_col GROUP BY k")" "yes|yes"
check "sum(float8) and sum(real) overflow raise 22003, as core does" \
	"$(gv_state on "SELECT k, sum(d) FROM ovs_col GROUP BY k")|$(gv_state on "SELECT k, sum(r) FROM ovs_col GROUP BY k")|$(gv_state off "SELECT k, sum(d) FROM ovs_col GROUP BY k")|$(gv_state off "SELECT k, sum(r) FROM ovs_col GROUP BY k")" \
	"22003|22003|22003|22003"

# ---- 8. degenerate inputs --------------------------------------------------

diff_query "oracle exact: all rows filtered out -> 0 groups" \
	"SELECT host, count(*) FROM %T WHERE g < 0 GROUP BY host"

# empty columnar table: GROUP BY yields no rows
psql_run "DROP TABLE IF EXISTS t_empty;"
psql_run "CREATE TABLE t_empty (k text, v int) USING pgcolumnar;"
check "empty table: grouped scan yields 0 rows" \
	"$(q "SELECT count(*) FROM (SELECT k, count(*) FROM t_empty GROUP BY k) s")" 0

# ---- 9. the path pays for the folding it does (#349) -----------------------

# The grouped path used to price itself at the scan cost plus a fixed bump, and
# charged nothing for folding. Every competing plan pays a per-row aggregation
# cost and this one paid none, so it won by construction -- including against a
# parallel plan measured 1.9x faster than itself on a full-scan GROUP BY.
#
# The property asserted here is the one that was observably wrong and that does
# not depend on the planner picking any particular plan at fixture scale: the
# estimate must respond to how much folding the node will do. Ten aggregates
# over the same rows must not be priced the same as one.
psql_run "DROP TABLE IF EXISTS t_cost;
          CREATE TABLE t_cost (host text, a int, b int, c int, d int, e int,
                               f int, g2 int, h int, i int, j int)
              USING pgcolumnar;
          INSERT INTO t_cost
          SELECT 'h' || (n % 50), n, n, n, n, n, n, n, n, n, n
          FROM generate_series(1, 20000) n;
          ANALYZE t_cost;" >/dev/null

# THE TWO COSTS MUST BE COSTS OF THE SAME PLAN SHAPE, and of the node under test.
# Ten aggregates over ten DIFFERENT columns are not that: a scan projecting ten
# columns costs more than one projecting one whatever the folding charge is, and
# that difference alone kept this arm green against a build charging nothing per
# aggregate -- the exact defect #349 fixed. Measured on PG 18, 20,000 rows:
#
#     distinct columns, 1 agg     350.50   350.50    (control, mutant)
#     distinct columns, 10 aggs  1253.01   803.01    <- 803.01 > 350.50, arm green
#     one column,       1 agg     350.50   350.50
#     one column,       10 aggs   800.50   350.50    <- the whole difference is folding
#
# 450.00 is cpu_operator_cost (0.0025) x 20000 rows x 9 extra aggregates, and under
# the mutation it is 0.00. Ten aggregates over the SAME column hold the projection
# fixed; the path is forced so both arms really are the grouped node rather than
# whatever the planner prefers once the charge is applied -- with the charge on, ten
# avg(a) is priced OUT of the grouped path and core's Agg wins it, which would
# silently change which node is being read (#1162).
gplan() {  # gplan <select-list> -> the plan text, grouped path forced
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -Atq \
		-c "SET pgcolumnar.enable_group_vectorization=on" \
		-c "SET enable_hashagg=off" \
		-c "SET enable_sort=off" \
		-c "EXPLAIN (COSTS ON) SELECT host, $1 FROM t_cost GROUP BY host" 2>&1
}
# ONE implementation of the plan, so the premise and the cost cannot drift onto
# different queries.
gtopcost() { printf '%s\n' "$1" | head -1 | grep -oE '\.\.[0-9]+\.[0-9]+' | tr -d '.' | head -1; }
gmarker() {  # gmarker <plan> -> yes, or the top line, so a red says what it got
	[ "$(grep -c 'Columnar Vectorized Group Keys' <<<"$1" || true)" -ne 0 ] &&
		echo yes || echo "no ($(head -1 <<<"$1" | tr -s ' ' | cut -c1-60))"
}

TEN_SAME="avg(a),avg(a),avg(a),avg(a),avg(a),avg(a),avg(a),avg(a),avg(a),avg(a)"
P1="$(gplan 'avg(a)')"
P10="$(gplan "$TEN_SAME")"
C1="$(gtopcost "$P1")"
C10="$(gtopcost "$P10")"
echo "      grouped path cost (digits, decimal point removed): 1 aggregate $C1, 10 aggregates $C10"

# Asserted, because a short load would leave the whole explanation above describing
# another table.
check_num "premise: the cost fixture holds the rows the 450.00 is computed from" \
	"$(q 'SELECT count(*) FROM t_cost')" "20000"
# The premise the two costs are costs OF. Without it a planner that stopped choosing
# the node would leave the arms comparing core's Agg with itself, which is monotone
# in the aggregate count for reasons of its own.
check "premise: one aggregate is costed as the grouped node" "$(gmarker "$P1")" "yes"
check "premise: and so is ten, so the two costs are comparable" "$(gmarker "$P10")" "yes"
check "premise: the grouped path is costed at all (non-empty estimate)" \
	"$([ -n "$C1" ] && [ -n "$C10" ] && echo yes || echo "no [1=$C1 10=$C10]")" "yes"
check "ten aggregates cost more than one (#349)" \
	"$([ "${C10:-0}" -gt "${C1:-0}" ] 2>/dev/null && echo yes || echo "no [1=$C1 10=$C10]")" "yes"

# ---- the group-estimate bound (#369) ---------------------------------------
#
# estimate_num_groups cannot see through a function. For date_trunc('minute', ts) it falls
# back to the timestamp column's ndistinct, which is near the row count: 19,996,000 against
# 720 actual on the 20M fixture. That number is charged twice on the parallel arm, by the
# Gather and by the Finalize, and not at all on the serial node, so the serial node won by
# construction on shapes where the arm is 4x faster.
#
# Three things are asserted, and the middle one is the one that matters:
#   1 the bound is ACCURATE, not merely smaller: 722 is floor(12h/1min) + 2
#   2 an INFORMED estimate is untouched, asserted as bit-identical rather than "still fast"
#   3 a query with no Const bound on the key gets no bound from us
psql_run "CREATE TABLE gb (ts timestamptz, host text, v float8) USING pgcolumnar;
	INSERT INTO gb SELECT '2026-01-01'::timestamptz + (g * interval '2160 microseconds'),
	  'h'||(g % 4000), random()*100 FROM generate_series(1,300000) g;
	ANALYZE gb;" >/dev/null 2>&1
GB="SET pgcolumnar.enable_group_vectorization=on;
    SET pgcolumnar.enable_parallel_vector_agg=on; SET max_parallel_workers_per_gather=4;"
gbest() { env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
	-d "$PGC_DB" -At -q -c "$GB" -c "EXPLAIN (COSTS ON) $1" 2>&1 |
	grep -oE "rows=[0-9]+" | head -1 | cut -d= -f2; }

# The window is 300000 * 2160us = 648s = 10.8 minutes, so 11 buckets plus the two
# partials the bound allows.
W="WHERE ts >= '2026-01-01' AND ts < '2026-01-01 00:10:48'"
gb_bound="$(gbest "SELECT date_trunc('minute', ts) b, avg(v) FROM gb $W GROUP BY b")"
check "the bound is accurate for a truncating time key, not just smaller" "$gb_bound" "12"
# Against the estimate measured, not the literal the arm above expects (#1339): a
# bound that fell below the true count failed only the arm above, and this one,
# comparing with 12, still said yes.
gb_true="$(q "SELECT count(*) FROM (SELECT date_trunc('minute', ts) FROM gb $W GROUP BY 1) z")"
check "and it is an UPPER bound: the true group count is at or below it" \
	"$([ -n "$gb_bound" ] && [ "$gb_true" -le "$gb_bound" ] && echo yes || echo "no ($gb_true groups over a bound of ${gb_bound:-none})")" "yes"

# The informed case. A plain Var has statistics, so examine_variable reports informed and
# the substitution must not run at all. Bit-identical, not merely still fast.
plain_before=$(gbest "SELECT host, avg(v) FROM gb GROUP BY host")
check "an informed estimate (plain column) is not replaced" \
	"$([ "$plain_before" -ge 3900 ] && [ "$plain_before" -le 4100 ] && echo yes || echo no)" "yes"

# A MISSING END OF THE RANGE COMES FROM THE ZONE MAPS (#1292). This arm used to assert
# the opposite -- "a truncating key with no range bound gets no bound from us" -- and that
# was the design until the owner chose, on #1292, to gate the grouped path on this
# estimate. A gate cannot use a number 60x to 3600x too high (measured on #1292), and a
# dashboard query bounds only one side ("the last day"). The zone maps hold every row
# group's minimum and maximum of the key's column, which is the range the scan reads.
#
# Each arm states BOTH directions: at least the true group count, because an
# under-estimate is the direction that under-sizes the hash table and would let the gate
# pass a query the runtime cap then refuses; and at most 4x, which is the accuracy a gate
# needs. Each reports the estimate and the truth when it fails.
gbwithin() {	# gbwithin QUERY -> "ok" when actual <= estimate <= 4*actual
	local est act
	est="$(gbest "$1")"
	act="$(q "SELECT count(*) FROM ($1) z;")"
	if [ -n "$est" ] && [ -n "$act" ] && [ "$act" -ge 1 ] && [ "$est" -ge "$act" ] && [ "$est" -le $((act * 4)) ]; then
		echo ok
	else
		echo "estimate ${est:-none} for ${act:-none} groups"
	fi
}
check "with no time bound at all, a truncating key is bounded from the zone maps" \
	"$(gbwithin "SELECT date_trunc('minute', ts) b, avg(v) FROM gb GROUP BY b")" "ok"
check "with only a lower time bound, a truncating key is still bounded" \
	"$(gbwithin "SELECT date_trunc('minute', ts) b, avg(v) FROM gb WHERE ts >= '2026-01-01' GROUP BY b")" "ok"
check "a date_bin key is bounded as a truncating key is" \
	"$(gbwithin "SELECT date_bin('1 minute', ts, timestamptz '2026-01-01') b, avg(v) FROM gb $W GROUP BY b")" "ok"
check "and a date_bin key with no time bound is bounded from the zone maps" \
	"$(gbwithin "SELECT date_bin('5 minutes', ts, timestamptz '2026-01-01') b, avg(v) FROM gb GROUP BY b")" "ok"

# THE RANGE COSTS ONE CATALOG SCAN, NOT ONE PER ROW GROUP (#1304 review). The first
# version probed zone_map_pkey once per group, on every plan: 109 -> 313 planning
# buffers at 67 groups, about 3 per group, so ~20,000 per plan at a billion rows. Counted
# as the planning transaction's own zone_map scans, which no other session can move (the
# #1287 instrument), on the table AND its index: a catalog index scan counts on the
# INDEX, and reading the table's counter alone gave 0 against 50 real probes. No WHERE,
# so the filter's survival estimate -- which samples zone
# maps on purpose -- is not in the count; 50 groups, so one scan and one per group are
# far apart.
psql_run "CREATE TABLE gbz (ts timestamptz, v float8) USING pgcolumnar;
	SELECT pgcolumnar.set_options('gbz', stripe_row_limit => 1000, chunk_group_row_limit => 1000);
	INSERT INTO gbz SELECT '2026-01-01'::timestamptz + g * interval '1 second', g FROM generate_series(1,50000) g;
	ANALYZE gbz;" >/dev/null 2>&1
gbz_groups="$(q "SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = pgcolumnar.get_storage_id('gbz');")"
# THE FALLBACK IS WHAT THIS MEASURES. Since #1304 option 5 a complete column extent
# bounds the range with no zone_map scan at all (column_extent.sh asserts 0); without
# one the planner reads the zone maps, and that read must stay one scan.
q "DELETE FROM pgcolumnar.column_extent WHERE storage_id = pgcolumnar.get_storage_id('gbz');" >/dev/null
gbz_scans="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At -q \
	-c "$GB" -c "BEGIN" -c "EXPLAIN SELECT date_trunc('minute', ts) b, avg(v) FROM gbz GROUP BY b" \
	-c "SELECT pg_stat_get_xact_numscans('pgcolumnar.zone_map'::regclass)
	          + pg_stat_get_xact_numscans('pgcolumnar.zone_map_pkey'::regclass)" -c "ROLLBACK" 2>&1 | grep -E '^[0-9]+$' | tail -1)"
check "premise: the fixture has many row groups, so one scan and one per group differ" \
	"$([ "${gbz_groups:-0}" -ge 40 ] && echo yes || echo "no (${gbz_groups:-none} groups)")" "yes"
check_num "planning a bucketed GROUP BY scans zone_map once, not once per row group" \
	"${gbz_scans:-}" "1"
# A user function with date_trunc's SHAPE must not get date_trunc's BOUND. PL/pgSQL
# because an SQL function would be inlined and lose the shape. Matching on shape alone
# estimated this at 722 against 499,999 actual, a 692x UNDER-estimate, which is the
# direction that under-prices the arm and under-sizes the Finalize's hash table.
psql_run "CREATE FUNCTION gb_hi_card(t text, s timestamptz) RETURNS text AS
	\$\$ BEGIN RETURN t || md5(s::text); END \$\$ LANGUAGE plpgsql IMMUTABLE;" >/dev/null 2>&1
check "a lookalike function with the same signature gets NO bound" \
	"$([ "$(gbest "SELECT gb_hi_card('minute', ts) b, avg(v) FROM gb $W GROUP BY b")" -gt 1000 ] && echo yes || echo no)" "yes"

# A MIXED timestamp/timestamptz predicate must get no bound. PostgreSQL has cross-type
# operators, so the Const stays bare and reaches the extraction instead of being wrapped in
# a cast. Both are int64 microseconds on different scales, wall clock against UTC, so under
# a non-UTC TimeZone the range would be computed across the offset. Measured with a
# discriminating fixture: core's own estimate is six figures here, so "bound applied" and
# "no bound" cannot be confused.
psql_run "CREATE TABLE gbm (ts timestamptz, v float8) USING pgcolumnar;
	INSERT INTO gbm SELECT '2026-01-01 00:00:00+00'::timestamptz + (g * interval '86400 microseconds'),
	  random() FROM generate_series(1,200000) g; ANALYZE gbm;" >/dev/null 2>&1
gbmest() { env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
	-d "$PGC_DB" -At -q -c "SET TimeZone='Asia/Kolkata';" -c "$GB" \
	-c "EXPLAIN (COSTS ON) $1" 2>&1 | grep -oE "rows=[0-9]+" | head -1 | cut -d= -f2; }
# WHAT THE MIXED-TYPE RULE PROTECTS, stated directly (#1292). This asserted "gets NO
# bound", which was the observable sign that the mixed predicate was not used. With the
# zone maps supplying a range, there is a bound either way, so the arm now asserts the
# property itself: a wall-clock window read as UTC must never narrow the estimate below
# the true group count. Under Asia/Kolkata, 04:00-08:00 wall clock is 22:30-02:30 UTC,
# 150 minutes of data; read as UTC it would clip the data to 04:00-04:48, about 50.
gbmcount() { env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
	-d "$PGC_DB" -At -q -c "SET TimeZone='Asia/Kolkata';" -c "SELECT count(*) FROM ($1) z;" 2>&1; }
GBM_MIXED="SELECT date_trunc('minute',ts) b, avg(v) FROM gbm
	   WHERE ts >= timestamp '2026-01-01 04:00' AND ts < timestamp '2026-01-01 08:00' GROUP BY b"
gbm_est="$(gbmest "$GBM_MIXED")"; gbm_act="$(gbmcount "$GBM_MIXED")"
check "premise: the mixed-type window holds data" \
	"$([ "${gbm_act:-0}" -ge 100 ] && echo yes || echo "no (${gbm_act:-none} groups)")" "yes"
check "a mixed timestamp/timestamptz predicate never narrows the estimate below the true count" \
	"$([ "${gbm_est:-0}" -ge "${gbm_act:-1}" ] && echo yes || echo "no (estimate ${gbm_est:-none} for ${gbm_act:-none} groups)")" "yes"
check "while a matching-type predicate still gets one" \
	"$([ "$(gbmest "SELECT date_trunc('minute',ts) b, avg(v) FROM gbm
	   WHERE ts >= timestamptz '2026-01-01 00:00+00' AND ts < timestamptz '2026-01-01 12:00+00' GROUP BY b")" -lt 5000 ] && echo yes || echo no)" "yes"

check "a non-time expression key gets no bound either" \
	"$([ "$(gbest "SELECT upper(host) b, avg(v) FROM gb GROUP BY b")" -gt 100 ] && echo yes || echo no)" "yes"

# ---- 10. on by default, gated on an upper bound (#1292 phase 2) -------------
# The grouped path is on by default, and the planner takes it only when a TRUE
# UPPER BOUND on the group count is within pgcolumnar.groupagg_max_groups: the
# relation's live row count, or, where every key is a time bucket, the buckets the
# zone maps allow. Over the cap the node errors, since its plan is fixed by then,
# so a gate on an ESTIMATE let a skewed key through: @OffgridwithJD measured on
# #1330 a fresh ANALYZE estimating 29,356 groups for 2,000,001, and the query
# that main answers errored by default. With a bound, the default never errors.
# The database-level setting the sections above use is cleared, so these arms see
# the compiled default.
psql_run "ALTER DATABASE $PGC_DB RESET pgcolumnar.enable_group_vectorization;"
check "the grouped path is on by default" \
	"$(q "SELECT boot_val || '|' || setting FROM pg_settings WHERE name = 'pgcolumnar.enable_group_vectorization';")" "on|on"
# 400,000 rows over two days: 576 five-minute buckets, and k with 200,000 values.
GATE_GEN="SELECT '2026-02-01 00:00:00+00'::timestamptz + g * interval '432 ms', (g % 200000)::int, (g % 1000)::int
	FROM generate_series(1, 400000) g"
psql_run "CREATE TABLE gate_t (ts timestamptz, k int, i int) USING pgcolumnar;
	INSERT INTO gate_t $GATE_GEN;
	CREATE TABLE gate_h (ts timestamptz, k int, i int);
	INSERT INTO gate_h $GATE_GEN;
	ANALYZE gate_t; ANALYZE gate_h;"
BUCKETQ="SELECT date_bin('5 minutes', ts, '2026-01-01 00:00:00+00') b, count(*), sum(i) FROM gate_t GROUP BY b"
HIGHK="SELECT k, count(*), sum(i) FROM %T GROUP BY k"
# groups_or_error CAP QUERY -> the number of groups the query returns under that cap,
# or its error. The query runs itself: wrapped in SELECT count(*) FROM (...), the
# planner drops the unused aggregates and the node refuses that shape, so the
# wrapper would measure a different plan.
# Both helpers turn hash aggregation and sorting off, so the grouped path wins
# whenever the gate offers it: these arms test the gate, not the cost model. A
# declined query still runs, on the disabled plan. Without this, a gate that let a
# skewed key through could still lose on cost, and the arm would stay green.
groups_or_error() {
	local out
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At -q \
		-c "SET enable_hashagg = off" -c "SET enable_sort = off" \
		-c "SET pgcolumnar.groupagg_max_groups = $1" -c "$2" 2>&1)"
	if grep -q '^ERROR' <<<"$out"; then grep -m1 '^ERROR' <<<"$out"; else grep -c '' <<<"$out"; fi
}
under_cap() {	# under_cap CAP QUERY -> 1 when the grouped node is planned under that cap
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At -q \
		-c "SET enable_hashagg = off" -c "SET enable_sort = off" \
		-c "SET pgcolumnar.groupagg_max_groups = $1" -c "EXPLAIN (COSTS OFF) $2" 2>&1 \
		| grep -c 'Columnar Vectorized Group Keys'
}
check "by default a five-minute bucket query takes the grouped path" "$(pgc_is_groupvec "$BUCKETQ")" yes
check "a five-minute bucket query over more rows than the cap still takes the grouped path" \
	"$(under_cap 100000 "$BUCKETQ")" "1"
check "a plain key over more rows than the cap is declined" "$(under_cap 100000 "${HIGHK//%T/gate_t}")" "0"
check "and the declined query answers what heap answers" \
	"$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At -q \
		-c "SET pgcolumnar.groupagg_max_groups = 100000" \
		-c "SELECT md5(string_agg(x::text, ',' ORDER BY x)) FROM (${HIGHK//%T/gate_t}) x" 2>&1)" \
	"$(q "SELECT md5(string_agg(x::text, ',' ORDER BY x)) FROM (${HIGHK//%T/gate_h}) x;")"
check "raising groupagg_max_groups to 10,000,000 lets the same query take the grouped path" \
	"$(under_cap 10000000 "${HIGHK//%T/gate_t}")" "1"

# @OffgridwithJD's shape on #1330: one key holds most rows, the rest are unique.
# A fresh ANALYZE samples 30,000 rows and estimates the groups from them, far too
# few (29,356 for 2,000,001 there). Here 95% of 800,000 rows share key 0, so
# 40,001 groups, and under a cap of 30,000 a tenth-of-the-cap gate on the estimate
# (3,000) would take the path and error. The row count, 800,000, declines it.
psql_run "CREATE TABLE gate_sk (k bigint, v int) USING pgcolumnar;
	INSERT INTO gate_sk SELECT CASE WHEN g % 20 = 0 THEN g ELSE 0 END, g FROM generate_series(1, 800000) g;
	ANALYZE gate_sk;"
SKQ="SELECT k, count(*), sum(v) FROM gate_sk GROUP BY k"
check "premise: ANALYZE estimates the skewed key's 40,001 groups below 3,000" \
	"$(q "SELECT CASE WHEN n_distinct BETWEEN 0 AND 3000 THEN 'below' ELSE n_distinct::text END FROM pg_stats WHERE tablename = 'gate_sk' AND attname = 'k';")" "below"
check "a skewed key whose estimate is far too low is declined under a cap of 30,000, and answers every group" \
	"$(under_cap 30000 "$SKQ")|$(groups_or_error 30000 "$SKQ")" "0|40001"

# ---- 11. past the cap, the node hands off to a core Finalize (#1300) ---------
# With a bound the cap is reached only by a plan that outlives it: the cap lowered
# after planning, or the table grown past it. A grouped plan whose aggregates all
# have a partial state is a Partial node under a core Finalize, so at the cap the
# node emits every group's partial state, empties its table and carries on, and the
# Finalize combines the duplicates. The answer is the heap's either way.
# srt_md5 SQL... -> md5 of the psql output, sorted: EXECUTE cannot be wrapped in a
# subquery, so the comparison is of its rows as text. A parameterless prepared
# statement is planned at its first EXECUTE, so each arm runs one under the default
# cap first, as PLAN_FIRST, with its rows discarded.
srt_md5() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At -q \
		"$@" 2>&1 | LC_ALL=C sort | md5sum | cut -c1-32
}
HEAP_HIGHK="$(srt_md5 -c "${HIGHK//%T/gate_h}")"
PLAN_FIRST=(-c '\o /dev/null' -c "EXECUTE gk" -c '\o')
check "a cached plan made under the default cap is a Partial grouped node under a core Finalize" \
	"$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At -q \
		-c "PREPARE gk AS ${HIGHK//%T/gate_t}" -c "EXPLAIN (COSTS OFF) EXECUTE gk" 2>&1 \
		| grep -cE '^Finalize HashAggregate|Columnar Vectorized Group Keys')" "2"
check "a cached plan run under a lower cap hands off at the cap and answers what heap answers" \
	"$(srt_md5 -c "PREPARE gk AS ${HIGHK//%T/gate_t}" "${PLAN_FIRST[@]}" \
		-c "SET pgcolumnar.groupagg_max_groups = 1000" -c "EXECUTE gk")" "$HEAP_HIGHK"
ho_flush="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At -q \
	-c "PREPARE gk AS ${HIGHK//%T/gate_t}" "${PLAN_FIRST[@]}" \
	-c "SET pgcolumnar.groupagg_max_groups = 1000" \
	-c "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) EXECUTE gk" 2>&1 \
	| sed -n 's/^ *Columnar Group Table Flushes: //p')"
check "and its EXPLAIN ANALYZE reports the group table flushed at the cap" \
	"$([ "${ho_flush:-0}" -gt 0 ] 2>/dev/null && echo yes || echo "no (${ho_flush:-<none>})")" "yes"
MINMAXK="SELECT k, min(i), max(i), count(i) FROM %T GROUP BY k"
check "min and max hand off too: a cached min/max plan under a lower cap answers what heap answers" \
	"$(srt_md5 -c "PREPARE gm AS ${MINMAXK//%T/gate_t}" -c '\o /dev/null' -c "EXECUTE gm" -c '\o' \
		-c "SET pgcolumnar.groupagg_max_groups = 1000" -c "EXECUTE gm")" \
	"$(srt_md5 -c "${MINMAXK//%T/gate_h}")"

# planned_md5 NAME SQL -> "<1 when the cached plan is the grouped node>|<md5 of its rows
# under a cap of 1,000>", planned at a first EXECUTE under the default cap. The plan is
# read too, so a query the node declined cannot pass by matching heap.
planned_md5() {
	local n
	n="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At -q \
		-c "PREPARE $1 AS $2" -c "EXPLAIN (COSTS OFF) EXECUTE $1" 2>&1 | grep -c 'Columnar Vectorized Group Keys')"
	echo "$n|$(srt_md5 -c "PREPARE $1 AS $2" -c '\o /dev/null' -c "EXECUTE $1" -c '\o' \
		-c "SET pgcolumnar.groupagg_max_groups = 1000" -c "EXECUTE $1")"
}
# min and max across a flush, where order is not byte order (#1764 review): float8
# with NaN in every group (NaN sorts above every number), and, where the server has
# ICU, text under en-x-icu, where 'a' < 'B'. 2,999 groups of ten rows, each in two runs
# of five on either side of a flush: the node compares the values within a run, and the
# Finalize combines the two runs' states. With one row per flush, the node never compared
# two values and a comparator ignoring the collation passed. Runs mix NaN with numbers,
# and 'a' values with 'B' values.
psql_run "CREATE TABLE mm_f (k int, f float8) USING pgcolumnar;
	CREATE TABLE mm_fh (k int, f float8);
	INSERT INTO mm_f SELECT (g / 5) % 2999, CASE WHEN g % 7 = 0 THEN 'NaN'::float8 ELSE g::float8 END FROM generate_series(1, 29990) g;
	INSERT INTO mm_fh SELECT * FROM mm_f;
	ANALYZE mm_f;"
MMF="SELECT k, min(f), max(f), count(f) FROM %T GROUP BY k"
check "min and max hand off over float8 NaN: a cached plan under a lower cap answers what heap answers" \
	"$(planned_md5 gmf "${MMF//%T/mm_f}")" "1|$(srt_md5 -c "${MMF//%T/mm_fh}")"
MMT_NAME="min and max hand off over text under an ICU collation: a cached plan under a lower cap answers what heap answers"
if [ "$(q "SELECT count(*) FROM pg_collation WHERE collname = 'en-x-icu'")" = 1 ]; then
	psql_run "CREATE TABLE mm_t (k int, t text COLLATE \"en-x-icu\") USING pgcolumnar;
		CREATE TABLE mm_th (k int, t text COLLATE \"en-x-icu\");
		INSERT INTO mm_t SELECT (g / 5) % 2999, CASE WHEN g % 2 = 0 THEN 'a' || g ELSE 'B' || g END FROM generate_series(1, 29990) g;
		INSERT INTO mm_th SELECT * FROM mm_t;
		ANALYZE mm_t;"
	MMT="SELECT k, min(t), max(t) FROM %T GROUP BY k"
	check "$MMT_NAME" "$(planned_md5 gmt "${MMT//%T/mm_t}")" "1|$(srt_md5 -c "${MMT//%T/mm_th}")"
else
	check_skip "$MMT_NAME" "SKIP  $MMT_NAME (this server has no ICU collation en-x-icu)" "no ICU"
fi

# THE CAP BOUNDS THE NODE'S MEMORY (#1764 review). The answers above are the same
# whether a flush frees what the table held or keeps it, so the peak the node reports
# is read: under a cap of 1,000, ten times the distinct keys must not take much more.
# A flush that emptied the table but kept its keys and states held 1.4 GB more at 2M
# keys, with every answer right.
psql_run "CREATE TABLE mem_a (k text, i int) USING pgcolumnar;
	CREATE TABLE mem_b (k text, i int) USING pgcolumnar;
	INSERT INTO mem_a SELECT md5(g::text) || md5((g * 7)::text), g FROM generate_series(1, 20000) g;
	INSERT INTO mem_b SELECT md5(g::text) || md5((g * 7)::text), g FROM generate_series(1, 200000) g;
	ANALYZE mem_a; ANALYZE mem_b;"
peak_kb() {	# TABLE -> the node's reported peak memory, in kB, under a cap of 1,000
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At -q \
		-c "PREPARE pm AS SELECT k, count(*), sum(i) FROM $1 GROUP BY k" \
		-c '\o /dev/null' -c "EXECUTE pm" -c '\o' -c "SET pgcolumnar.groupagg_max_groups = 1000" \
		-c "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) EXECUTE pm" 2>&1 \
		| sed -n 's/^ *Columnar Group Table Peak Memory: \([0-9]*\) kB.*/\1/p'
}
pk_a="$(peak_kb mem_a)"; pk_b="$(peak_kb mem_b)"
check "under a cap of 1,000, ten times the distinct keys keep the node's peak memory within twice" \
	"$([ -n "$pk_a" ] && [ -n "$pk_b" ] && [ "$pk_a" -gt 0 ] && [ "$pk_b" -le $((pk_a * 2)) ] && echo yes || echo "no (${pk_a:-none} kB at 20,000 keys, ${pk_b:-none} kB at 200,000)")" "yes"

# The table grown past the cap under a cached plan. 500 keys plan under a cap of
# 1,000, then 5,000 more commit before the plan runs again.
psql_run "CREATE TABLE gate_gr (k int, i int) USING pgcolumnar;
	CREATE TABLE gate_grh (k int, i int);
	INSERT INTO gate_gr SELECT g, g FROM generate_series(1, 500) g;
	INSERT INTO gate_grh SELECT g, g FROM generate_series(1, 500) g;"
gr_out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At -q \
	-c "SET pgcolumnar.groupagg_max_groups = 1000" \
	-c "PREPARE gg AS SELECT k, count(*), sum(i) FROM gate_gr GROUP BY k" -c "EXECUTE gg" \
	-c "INSERT INTO gate_gr SELECT g, g FROM generate_series(501, 5500) g" \
	-c "EXPLAIN (COSTS OFF) EXECUTE gg" -c "EXECUTE gg" 2>&1)"
psql_run "INSERT INTO gate_grh SELECT g, g FROM generate_series(501, 5500) g;"
check "premise: the cached plan is still the grouped node after the table grew" \
	"$(grep -c 'Columnar Vectorized Group Keys' <<<"$gr_out")" "1"
check "a table grown past the cap under a cached plan hands off and answers what heap answers" \
	"$(grep -E '^[0-9]+\|' <<<"$gr_out" | tail -n 5500 | LC_ALL=C sort | md5sum | cut -c1-32)" \
	"$(srt_md5 -c "SELECT k, count(*), sum(i) FROM gate_grh GROUP BY k")"

# An aggregate with no partial state the node can emit keeps the serial node, and
# the runtime cap stays its backstop: sum(int8) is a numeric sum.
psql_run "CREATE TABLE gate_b (k int, b bigint) USING pgcolumnar;
	INSERT INTO gate_b SELECT g % 200000, g FROM generate_series(1, 400000) g;
	ANALYZE gate_b;"
SUM8K="SELECT k, sum(b) FROM gate_b GROUP BY k"
rc_out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At -q \
	-c "PREPARE gk8 AS $SUM8K" -c "EXPLAIN (COSTS OFF) EXECUTE gk8" -c "EXECUTE gk8" \
	-c "SET pgcolumnar.groupagg_max_groups = 1000" -c "EXECUTE gk8" 2>&1 || true)"
check "a cached plan whose aggregates cannot hand off still stops at the runtime cap, with its hint" \
	"$(grep -c 'Columnar Vectorized Group Keys' <<<"$rc_out")|$(grep -c '^Finalize' <<<"$rc_out")|$(grep -c 'exceeded pgcolumnar.groupagg_max_groups (1000)' <<<"$rc_out")|$(grep -c 'HINT:  Raise pgcolumnar.groupagg_max_groups' <<<"$rc_out")" "1|0|1|1"

# A NULL key is one more group, and the bucket bound must count it. @OffgridwithJD's
# shape on #1330: buckets at 00:00:59, 00:01:30 and 00:02:00 fill the bound of 3
# (floor(61 s / 60 s) + 2), so the NULL rows make a fourth group. Under a cap of 3 a
# bound without the NULL group takes the path and errors.
psql_run "CREATE TABLE gate_nb (ts timestamptz, v int) USING pgcolumnar;
	INSERT INTO gate_nb SELECT '2026-01-01 00:00:59+00', g FROM generate_series(1, 50) g;
	INSERT INTO gate_nb SELECT '2026-01-01 00:01:30+00', g FROM generate_series(1, 50) g;
	INSERT INTO gate_nb SELECT '2026-01-01 00:02:00+00', g FROM generate_series(1, 50) g;
	INSERT INTO gate_nb SELECT NULL, g FROM generate_series(1, 50) g;"
# Declined, not only answered (#1339): the answer alone held for a node that took the
# query and did not stop at its cap.
check "a NULL key counts as a group in the bucket bound: four groups under a cap of 3 are declined and all answered" \
	"$(under_cap 3 "SELECT date_trunc('minute', ts) b, count(*) FROM gate_nb GROUP BY b")|$(groups_or_error 3 "SELECT date_trunc('minute', ts) b, count(*) FROM gate_nb GROUP BY b")" "0|4"

pgc_summary
