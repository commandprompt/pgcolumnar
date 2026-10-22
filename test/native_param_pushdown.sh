#!/usr/bin/env bash
#
# pgColumnar chunk-group skipping works for parameterized predicates.
#
# The scan-key builder (pgcolumnar_clause_to_scankey) only accepted a Const
# operand, so a predicate like "col >= $1" -- a PARAM_EXTERN from a prepared
# statement, PL/pgSQL, or the extended protocol -- built no scan key and disabled
# chunk-group skipping entirely. On a generic plan the scan then read every group.
#
# Begin now freezes execution-stable operands (a PARAM_EXTERN, or any subexpression
# with no Var, no PARAM_EXEC, and no volatile function) into Consts before building
# the keys. This is exact: such an operand cannot change within the execution, and
# the executor still re-applies the original qual to every surviving row.
#
# The correctness hazard is a correlated PARAM_EXEC (a nestloop inner scan whose
# qual references the outer row): it changes per rescan, and the keys are built
# once, so freezing it would silently skip groups that in fact match. That param
# is deliberately NOT frozen; the correlated arm below proves results stay correct.
#
# Usage:  test/native_param_pushdown.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

q "CREATE EXTENSION IF NOT EXISTS pgcolumnar;" >/dev/null
# 40000 rows / 2000 = 20 monotonic groups; ts >= 39000 lives in the last group.
q "SET pgcolumnar.stripe_row_limit=2000;
   CREATE TABLE t (id int, ts int) USING pgcolumnar;
   INSERT INTO t SELECT g, g FROM generate_series(1,40000) g;
   CREATE TABLE thr (lo int); INSERT INTO thr VALUES (39000),(38000);" >/dev/null

psql_c() { env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At -c "$1" 2>&1; }

# A backend that crashed answers "SELECT 1" again once recovery is over, so survival
# also requires that the server log's count of backends killed by a signal has not
# moved since the arms began (#1341).
crashes() {
	local _n
	_n="$(pgc_pg "grep -c 'terminated by signal' '$PGC_LOGFILE'" 2>/dev/null)"
	case "$_n" in '' | *[!0-9]*) ;; *) echo "$_n" ;; esac
}
survived() {	# survived BEFORE -> "1" when the server answers and no backend died since BEFORE
	local _up _now _i
	_up="$(q 'SELECT 1')"; _now="$(crashes)"
	if ! [[ "$1" =~ ^[0-9]+$ && "$_now" =~ ^[0-9]+$ ]]; then echo "no (crash count unreadable)"; return; fi
	if [ "$_up" = 1 ] && [ "$_now" = "$1" ]; then echo 1; return; fi
	for _i in $(seq 1 60); do [ "$(q 'SELECT 1' 2>/dev/null)" = 1 ] && break; sleep 0.5; done
	echo "no ([$_up], $(( _now - $1 )) backend crashes)"
}
CRASHES_BEFORE="$(crashes)"

# Groups read by a generic-plan parameterized scan. Under the fix the last group
# is the only one read; without it, all 20 are.
groups_removed() {
	psql_c "SET plan_cache_mode=force_generic_plan;
		DEALLOCATE ALL;
		PREPARE p(int) AS SELECT count(*) FROM t WHERE ts >= \$1;
		EXPLAIN (ANALYZE, TIMING off, SUMMARY off) EXECUTE p(39000);" \
		| sed -n 's/.*Chunk Groups Removed by Filter: \([0-9]*\).*/\1/p' | head -1
}

check "a generic-plan parameterized qual prunes chunk groups (19 of 20 removed)" \
	"$(groups_removed)" "19"

# Correctness: the pruned count equals the literal count (ids 39000..40000).
prep_count() {
	psql_c "SET plan_cache_mode=force_generic_plan;
		DEALLOCATE ALL;
		PREPARE c(int) AS SELECT count(*) FROM t WHERE ts >= \$1;
		EXECUTE c(39000);" | tail -1
}
check "parameterized count matches the literal count" "$(prep_count)" "1001"
check "literal count is the expected value" \
	"$(q 'SELECT count(*) FROM t WHERE ts >= 39000;')" "1001"

# Safety: a correlated PARAM_EXEC (nestloop inner scan) must NOT be frozen, or it
# would mis-prune per outer row. sum over thr(39000, 38000) = 1001 + 2001 = 3002.
#
# That answer alone could not tell. The keys are built once, at Begin, before any
# outer row has set the param, so a scan that DID freeze it froze the param's initial
# value, and "ts >= <that>" prunes nothing: with the PARAM_EXEC guard removed the
# lower-bound arm still returned 3002, and every check passed (#1341). So the arm now
# reads the scan itself -- a correlated param must build no skip predicate -- and asks
# the opposite bound too, "ts <= thr.lo", where a frozen initial value would rule out
# every group.
CORR_PLAN="$(q 'EXPLAIN (ANALYZE, TIMING off, SUMMARY off, COSTS off)
	SELECT sum(c) FROM thr, LATERAL (SELECT count(*) c FROM t WHERE t.ts >= thr.lo) s;')"
check "the correlated arm rescans the columnar scan once per outer row" \
	"$(grep -c 'Custom Scan (PgColumnarScan) on t (actual rows=[0-9.]* loops=2)' <<<"$CORR_PLAN")" "1"
check_num "a correlated PARAM_EXEC builds no skip predicate" \
	"$(sed -n 's/.*Columnar Usable Skip Predicates: \([0-9]*\).*/\1/p' <<<"$CORR_PLAN" | head -1)" "0"
check "a correlated PARAM_EXEC is not mis-pruned (results stay correct)" \
	"$(q 'SELECT sum(c) FROM thr, LATERAL (SELECT count(*) c FROM t WHERE t.ts >= thr.lo) s;')" \
	"3002"
# ts <= 39000 is 39000 rows and ts <= 38000 is 38000: 77000.
check "a correlated PARAM_EXEC upper bound is not mis-pruned either" \
	"$(q 'SELECT sum(c) FROM thr, LATERAL (SELECT count(*) c FROM t WHERE t.ts <= thr.lo) s;')" \
	"77000"

# A generic plan executed twice in ONE session with two values must freeze each
# value afresh. Every arm above ran one execution per session, so a frozen value
# reused by a later execution passed them all (#1341). ts >= 38000 is 2001 rows, and
# prunes 18 of the 20 groups.
TWICE="$(psql_c "SET plan_cache_mode=force_generic_plan;
	DEALLOCATE ALL;
	PREPARE w(int) AS SELECT count(*) FROM t WHERE ts >= \$1;
	EXECUTE w(39000);
	EXECUTE w(38000);
	EXPLAIN (ANALYZE, TIMING off, SUMMARY off) EXECUTE w(38000);")"
# The two counts are the output's only all-digit lines (psql also prints the tags).
check "a second execution in the same session counts with its own value" \
	"$(grep -xE '[0-9]+' <<<"$TWICE" | sed -n 2p)" "2001"
check "and prunes with its own value too (18 of 20 removed)" \
	"$(sed -n 's/.*Chunk Groups Removed by Filter: \([0-9]*\).*/\1/p' <<<"$TWICE" | head -1)" "18"

check "backend alive" "$(survived "$CRASHES_BEFORE")" "1"

pgc_summary
