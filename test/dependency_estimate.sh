#!/usr/bin/env bash
#
# pgColumnar: a functional-dependency row estimate is not collapsed by a
# capacity-truncated extended MCV (#438).
#
# A columnar scan inherits the row estimate core computes, and core applies a
# partial extended MCV BEFORE the functional dependency, which -- when the MCV
# cannot hold every group -- displaces the (perfect) dependency and clamps the
# joint estimate to 1 (measured: 1 against an actual 300, a 300x under-estimate).
# PgColumnarSetRelPathlist recomputes a dependency-consistent estimate and raises
# rel->rows to it when core's is lower. This suite pins that, proves it does not
# distort a case core already gets right, and that a table without a dependency
# statistic is untouched.
#
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

est() {  # est <sql> -> the top node's estimated row count
	q "EXPLAIN (COSTS ON) $1" | grep -m1 -oE 'rows=[0-9]+' | head -1 | cut -d= -f2
}
# A reading is a number or it is not a reading. `${EST:-0}` read a failed EXPLAIN as
# 0, and 0 passes both "bounded" and "untouched": with every EXPLAIN failing, two of
# the four arms below stayed green.
num() { [[ "$1" =~ ^[0-9]+$ ]]; }

# ---- the #438 case: 1000 near-uniform values, b = a*2, full extended stats ----
psql_run "CREATE TABLE dep_full (a int, b int) USING pgcolumnar;"
psql_run "INSERT INTO dep_full SELECT g%1000, (g%1000)*2 FROM generate_series(1,300000) g;"
psql_run "CREATE STATISTICS dep_full_s (dependencies, ndistinct, mcv) ON a, b FROM dep_full;"
psql_run "ANALYZE dep_full;"
# THE PROBE IS A PAIR THE EXTENDED MCV DOES NOT HOLD. The clamp happens only then: the
# MCV holds 100 of the 1000 pairs, chosen by ANALYZE's random sample, and when the
# fixed probe a=5 landed in it core estimated 400 on its own and the arm below passed
# with the correction turned off (1 run in 6, measured). So the probe is the first
# pair outside the MCV, read from the statistic itself.
PROBE="$(q "SELECT v FROM generate_series(0, 999) v WHERE ARRAY[v::text, (v*2)::text] NOT IN (
	SELECT m.values FROM pg_statistic_ext s JOIN pg_statistic_ext_data d ON d.stxoid = s.oid,
	pg_mcv_list_items(d.stxdmcv) m WHERE s.stxname = 'dep_full_s') ORDER BY v LIMIT 1;")"
FULL_ACTUAL="$(q "SELECT count(*) FROM dep_full WHERE a=${PROBE:-0} AND b=$(( ${PROBE:-0} * 2 ));")"

# a table core already estimates correctly (10 values, each an MCV) -- must NOT move
psql_run "CREATE TABLE dep_ok (a int, b int) USING pgcolumnar;"
psql_run "INSERT INTO dep_ok SELECT g%10, (g%10)*2 FROM generate_series(1,300000) g;"
psql_run "CREATE STATISTICS dep_ok_s (dependencies, ndistinct, mcv) ON a, b FROM dep_ok;"
psql_run "ANALYZE dep_ok;"
OK_ACTUAL="$(q "SELECT count(*) FROM dep_ok WHERE a=5 AND b=10;")"

# a table with NO extended statistic -- the correction must not fire
psql_run "CREATE TABLE dep_none (a int, b int) USING pgcolumnar;"
psql_run "INSERT INTO dep_none SELECT g%1000, (g%1000)*2 FROM generate_series(1,300000) g;"
psql_run "ANALYZE dep_none;"

# ---- premises: the fixtures express the question ---------------------------
check "premise: the #438 probe is a pair the truncated extended MCV does not hold" \
	"$(num "$PROBE" && echo yes || echo "no probe [$PROBE]")" "yes"
check_num "premise: the #438 case really matches ~300 rows" "$FULL_ACTUAL" "300"

# ---- the correction: the clamped estimate is raised to the dependency floor -
# On unfixed main this is 1 (the MCV displaces the dependency). The fix makes it
# track the dependency-informed estimate, which is within a small factor of actual.
FULL_EST="$(est "SELECT * FROM dep_full WHERE a=${PROBE:-0} AND b=$(( ${PROBE:-0} * 2 ))")"
check "the functional-dependency estimate is not collapsed to the clamp floor" \
	"$(if ! num "$FULL_EST"; then echo "no estimate [$FULL_EST]"
	   elif [ "$FULL_EST" -ge 100 ]; then echo raised; else echo "clamped($FULL_EST)"; fi)" "raised"
check "and it is not an over-correction (bounded by the most selective marginal)" \
	"$(if ! num "$FULL_EST" || ! num "$FULL_ACTUAL"; then echo "no estimate [$FULL_EST|$FULL_ACTUAL]"
	   elif [ "$FULL_EST" -le "$((FULL_ACTUAL * 2))" ]; then echo bounded; else echo "over($FULL_EST)"; fi)" "bounded"

# ---- no distortion where core is already right -----------------------------
OK_EST="$(est "SELECT * FROM dep_ok WHERE a=5 AND b=10")"
check "a case core already estimates well is unchanged (within 20%)" \
	"$(if ! num "$OK_EST" || ! num "$OK_ACTUAL"; then echo "no estimate [$OK_EST|$OK_ACTUAL]"
	   else awk -v e="$OK_EST" -v a="$OK_ACTUAL" 'BEGIN{print (e>=a*0.8 && e<=a*1.2)?"same":"moved("e")"}'; fi)" "same"

# ---- no dependency statistic -> no correction ------------------------------
NONE_EST="$(est "SELECT * FROM dep_none WHERE a=5 AND b=10")"
check "a table with no dependency statistic is left to core's estimate (not raised)" \
	"$(if ! num "$NONE_EST"; then echo "no estimate [$NONE_EST]"
	   elif [ "$NONE_EST" -le 10 ]; then echo untouched; else echo "raised($NONE_EST)"; fi)" "untouched"

pgc_summary
