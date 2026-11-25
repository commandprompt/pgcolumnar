#!/usr/bin/env bash
#
# pgColumnar chunk-group skipping works for parameterized predicates.
#
# The scan-key builder (pgcolumnar_clause_to_scankey) only accepted a Const
# operand, so a predicate like "col >= $1" -- a PARAM_EXTERN from a prepared
# statement, PL/pgSQL, or the extended protocol -- built no scan key and disabled
# chunk-group skipping entirely. On a generic plan the scan then read every group.
#
# The scan now freezes execution-stable operands (a PARAM_EXTERN, or any subexpression
# with no Var, no PARAM_EXEC, and no volatile function) into Consts before building
# the keys. Begin freezes a bound parameter; an operand that needs a call, such as
# "ts > f($1)", waits until the scan first runs (#2065). This is exact: such an
# operand cannot change within the execution, and the executor still re-applies the
# original qual to every surviving row.
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

# An error raised while the scan freezes a stable operand must reach the client (#2065).
# Begin used to catch it with PG_TRY and throw it away, with no subtransaction to
# release what the failed call held. So a statement_timeout that fired during that one
# evaluation was lost and the query returned rows, and the plancache, snapshot and SPI
# state of the failed call leaked as WARNINGs.
#
# Each function below misbehaves on its FIRST call only. On a generic plan the planner
# cannot fold "fn($1)" (the value is unknown), so that first call is the one the scan
# makes; a heap scan makes it on the first row and fails. A literal argument would not
# do: the planner's selectivity estimate calls a stable function itself. Each statement
# goes in its own -c, so each gets its own statement_timeout timer.
psql_m() { local a=(); for s in "$@"; do a+=(-c "$s"); done
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At "${a[@]}" 2>&1; }
q "CREATE FUNCTION slow_first(int) RETURNS int LANGUAGE plpgsql STABLE AS \$\$
	DECLARE n int := coalesce(nullif(current_setting('t.n', true), ''), '0')::int + 1;
	BEGIN PERFORM set_config('t.n', n::text, false);
	IF n = 1 THEN PERFORM pg_sleep(2); END IF; RETURN \$1; END \$\$;
   CREATE FUNCTION fail_first(int) RETURNS int LANGUAGE plpgsql STABLE AS \$\$
	DECLARE n int := coalesce(nullif(current_setting('t.n', true), ''), '0')::int + 1;
	BEGIN PERFORM set_config('t.n', n::text, false);
	IF n = 1 THEN RAISE EXCEPTION 'call 1 fails'; END IF; RETURN \$1; END \$\$;" >/dev/null
TIMEOUT_OUT="$(psql_m "SET plan_cache_mode = force_generic_plan" "PREPARE s(int) AS SELECT count(*) FROM t WHERE ts > slow_first(\$1)" \
	"SET t.n = '0'" "SET statement_timeout = '300ms'" "EXECUTE s(0)")"
check "a statement_timeout during scan start-up cancels the columnar query (#2065)" \
	"$(grep -c 'canceling statement due to statement timeout' <<<"$TIMEOUT_OUT")" "1"
FAIL_OUT="$(psql_m "SET plan_cache_mode = force_generic_plan" "PREPARE f(int) AS SELECT count(*) FROM t WHERE ts > fail_first(\$1)" \
	"SET t.n = '0'" "EXECUTE f(0)")"
check "a stable operand that raises once reaches the client, as on heap (#2065)" \
	"$(grep -c 'ERROR:  call 1 fails' <<<"$FAIL_OUT")" "1"
check_num "neither failed query leaves a resource or SPI-stack WARNING (#2065)" \
	"$(grep -c 'WARNING' <<<"$TIMEOUT_OUT$FAIL_OUT")" "0"
# A plain EXPLAIN executes nothing, so it must not call the operand either. Begin
# runs under EXPLAIN too, and froze the operand there as well. cntc() counts its calls
# in t.n and never raises: a function that raised would roll back its own count, so
# t.n would read 0 whether EXPLAIN called it or not.
q "CREATE FUNCTION cntc(int) RETURNS int LANGUAGE plpgsql STABLE AS \$\$
	DECLARE n int := coalesce(nullif(current_setting('t.n', true), ''), '0')::int + 1;
	BEGIN PERFORM set_config('t.n', n::text, false); RETURN \$1; END \$\$;" >/dev/null
check "a plain EXPLAIN does not call a stable operand (#2065)" \
	"$(psql_m "SET plan_cache_mode = force_generic_plan" "PREPARE e(int) AS SELECT count(*) FROM t WHERE ts > cntc(\$1)" \
		"SET t.n = '0'" "EXPLAIN (COSTS off) EXECUTE e(0)" "SHOW t.n" | tail -1)" "0"
# But a plain EXPLAIN EXECUTE still reports a bare bound parameter as pushed down:
# reading a bound value calls nothing and cannot fail.
check "a plain EXPLAIN EXECUTE still pushes down a bound parameter (#2065)" \
	"$(psql_m "SET plan_cache_mode = force_generic_plan" "PREPARE b(int) AS SELECT count(*) FROM t WHERE ts >= \$1" \
		"EXPLAIN (COSTS off) EXECUTE b(39000)" \
		| sed -n 's/.*Columnar Pushed-Down Filters: \([0-9]*\).*/\1/p' | head -1)" "1"

# A scan that is set up but never run must not call its stable operand either (#2065).
# The executor sets up every plan node, including one that never runs: an Append arm
# that LIMIT cuts off, or a branch under a one-time filter that is false. A btree index
# scan evaluates its runtime keys when it first runs, not when it is set up, and heap
# calls the operand per row, so both answer these queries. boom() raises on every call.
q "CREATE FUNCTION boom(int) RETURNS int LANGUAGE plpgsql STABLE AS \$\$
	BEGIN RAISE EXCEPTION 'boom called'; END \$\$;" >/dev/null
check "a columnar scan that LIMIT cuts off never calls its stable operand (#2065)" \
	"$(psql_m "SET plan_cache_mode = force_generic_plan" \
		"PREPARE l(int) AS SELECT 1 UNION ALL (SELECT ts FROM t WHERE ts > boom(\$1)) LIMIT 1" \
		"EXECUTE l(0)" | tail -1)" "1"
check "a columnar scan under a false one-time filter never calls its stable operand (#2065)" \
	"$(psql_m "SET plan_cache_mode = force_generic_plan" \
		"PREPARE o(int, int) AS SELECT count(*) FROM t WHERE ts > boom(\$1) AND \$2 = 0" \
		"EXECUTE o(0, 1)" | tail -1)" "0"
# Freezing at the first run must still prune: a stable function operand, not only a
# bare parameter, drives chunk-group skipping once the scan runs. PL/pgSQL, so the
# planner cannot inline it into a bare parameter.
q "CREATE FUNCTION same(int) RETURNS int LANGUAGE plpgsql STABLE AS \$\$
	BEGIN RETURN \$1; END \$\$;" >/dev/null
check "a stable function operand still prunes when the scan runs (19 of 20 removed) (#2065)" \
	"$(psql_m "SET plan_cache_mode = force_generic_plan" \
		"PREPARE g(int) AS SELECT count(*) FROM t WHERE ts >= same(\$1)" \
		"EXPLAIN (ANALYZE, TIMING off, SUMMARY off) EXECUTE g(39000)" \
		| sed -n 's/.*Chunk Groups Removed by Filter: \([0-9]*\).*/\1/p' | head -1)" "19"

# The scan freezes such an operand once, on its first run, and not again on each
# later call for a row. cntc() counts its calls: one for the freeze, then one per row
# the Filter tests, which is the rows it returned plus the rows it removed.
ONCE="$(psql_m "SET plan_cache_mode = force_generic_plan" \
	"PREPARE n(int) AS SELECT count(*) FROM t WHERE ts >= cntc(\$1)" \
	"SET t.n = '0'" "EXPLAIN (ANALYZE, COSTS off, TIMING off, SUMMARY off) EXECUTE n(39001)" "SHOW t.n")"
ONCE_ROWS="$(sed -n 's/.*Custom Scan (PgColumnarScan) on t (actual rows=\([0-9]*\).*/\1/p' <<<"$ONCE" | head -1)"
ONCE_REMOVED="$(sed -n 's/.*Rows Removed by Filter: \([0-9]*\).*/\1/p' <<<"$ONCE" | head -1)"
if [[ "$ONCE_ROWS" =~ ^[1-9][0-9]*$ && "$ONCE_REMOVED" =~ ^[0-9]+$ ]]; then
	ONCE_WANT="$(( 1 + ONCE_ROWS + ONCE_REMOVED ))"
else
	ONCE_WANT="a plan with rows and Rows Removed by Filter (rows [$ONCE_ROWS], removed [$ONCE_REMOVED])"
fi
check "the scan calls a stable operand once to freeze it, then once per row the Filter tests (#2065)" \
	"$(tail -1 <<<"$ONCE")" "$ONCE_WANT"

# Begin must call nothing for a PL/pgSQL parameter either. PL/pgSQL hands the executor
# a parameter list with a fetch hook, and that hook evaluates the variable: for a
# record field that is not assigned it raises. Here the optional filter is off, so
# the plan's one-time filter is false, the scan never runs, and heap answers 0.
q "CREATE FUNCTION opt_eq(use_filter bool) RETURNS bigint LANGUAGE plpgsql AS \$\$
	DECLARE r record; c bigint;
	BEGIN
	  IF use_filter THEN SELECT 39500 AS id INTO r; END IF;
	  SELECT count(*) INTO c FROM t WHERE use_filter AND ts = r.id;
	  RETURN c;
	END \$\$;" >/dev/null
check "a PL/pgSQL record field under a false one-time filter is not fetched at scan set-up (#2065)" \
	"$(psql_m "SET plan_cache_mode = force_generic_plan" "SELECT opt_eq(true)" "SELECT opt_eq(false)" \
		| grep -vx 'SET' | tr '\n' ' ' | sed 's/ $//')" "1 0"

# So a PL/pgSQL variable is frozen only when the scan first runs, and it must still
# prune there. A plain EXPLAIN inside PL/pgSQL would not show it: it plans with the
# value inlined ("ts >= 39000"). auto_explain logs the plan the function really ran,
# a generic plan with "ts >= $1", and client_min_messages = log sends it to psql.
q "CREATE FUNCTION lo_cnt(lo int) RETURNS bigint LANGUAGE plpgsql AS \$\$
	DECLARE c bigint; BEGIN SELECT count(*) INTO c FROM t WHERE ts >= lo; RETURN c; END \$\$;" >/dev/null
LO_OUT="$(psql_m "SET plan_cache_mode = force_generic_plan" "LOAD 'auto_explain'" \
	"SET auto_explain.log_min_duration = 0" "SET auto_explain.log_analyze = on" \
	"SET auto_explain.log_timing = off" "SET auto_explain.log_nested_statements = on" \
	"SET client_min_messages = log" "SELECT lo_cnt(39000)")"
check "auto_explain logs the plan a PL/pgSQL function ran, with its parameter (premise) (#2065)" \
	"$(grep -c 'Filter: (ts >= \$1)' <<<"$LO_OUT")" "1"
check "a PL/pgSQL variable on a generic plan prunes when the scan runs (19 of 20 removed) (#2065)" \
	"$(sed -n 's/.*Chunk Groups Removed by Filter: \([0-9]*\).*/\1/p' <<<"$LO_OUT" | head -1)" "19"

# A projection scan gets the frozen keys mapped onto its own columns. The projection
# reads [row number, its columns...], so handing it the base table's keys would test
# the wrong column and skip groups that match. Small stripes, so the planner picks
# the projection; one literal key and one key frozen at the first run.
q "CREATE TABLE prsc (k int, payload text) USING pgcolumnar;
   SELECT pgcolumnar.set_options('prsc', chunk_group_row_limit => 1000, stripe_row_limit => 1000);
   INSERT INTO prsc SELECT k, 'p' || k FROM generate_series(1, 20000) k ORDER BY md5(k::text);
   SELECT pgcolumnar.add_projection('prsc', 'byk', ARRAY['k'], ARRAY['k']);
   ANALYZE prsc;" >/dev/null
PROJ_SET=("SET pgcolumnar.enable_projection_scan = on" "SET max_parallel_workers_per_gather = 0"
	"SET pgcolumnar.enable_ungrouped_vector_agg = off" "SET pgcolumnar.enable_group_vectorization = off")
check "the projection arm's scan uses the projection (premise) (#2065)" \
	"$(psql_m "${PROJ_SET[@]}" "EXPLAIN (COSTS off) SELECT count(*), sum(k) FROM prsc WHERE k >= 100 AND k <= same(900)" \
		| grep -c 'Columnar Projection: byk')" "1"
# Heap answers 801|400500 (k 100..900) and 11|219945 (k 19990..20000).
check "a projection scan with a key frozen at the first run counts what heap counts (#2065)" \
	"$(psql_m "${PROJ_SET[@]}" "SELECT count(*), sum(k) FROM prsc WHERE k >= 100 AND k <= same(900)" \
		"SELECT count(*), sum(k) FROM prsc WHERE k >= 19990 AND k <= same(20000)" | grep -vx 'SET' | tr '\n' ' ' | sed 's/ $//')" \
	"801|400500 11|219945"

# The keys frozen at the first run replace the reader's predicates, and that step must
# obey pgcolumnar.enable_qual_pushdown = off as Begin does. Otherwise EXPLAIN reports
# no pushed-down filter while the scan prunes 19 groups (#191). The same query with
# pushdown on removes 19 (the stable-function check above).
check "with pgcolumnar.enable_qual_pushdown off, a key frozen at the first run prunes nothing (#2065)" \
	"$(psql_m "SET plan_cache_mode = force_generic_plan" "SET pgcolumnar.enable_qual_pushdown = off" \
		"PREPARE q(int) AS SELECT count(*) FROM t WHERE ts >= same(\$1)" \
		"EXPLAIN (ANALYZE, TIMING off, SUMMARY off) EXECUTE q(39000)" \
		| sed -n 's/.*Chunk Groups Removed by Filter: \([0-9]*\).*/\1/p' | head -1)" "0"
# Two keys frozen at the first run must both be tested. ts >= 1 excludes no group, so
# only ts <= 2000 removes the other 19: a scan that tests the first key alone removes 0.
check "two keys frozen at the first run both prune (19 of 20 removed) (#2065)" \
	"$(psql_m "SET plan_cache_mode = force_generic_plan" \
		"PREPARE k(int, int) AS SELECT count(*) FROM t WHERE ts >= same(\$1) AND ts <= same(\$2)" \
		"EXPLAIN (ANALYZE, TIMING off, SUMMARY off) EXECUTE k(1, 2000)" \
		| sed -n 's/.*Chunk Groups Removed by Filter: \([0-9]*\).*/\1/p' | head -1)" "19"
# The frozen operand must be copied out of the memory it was evaluated in. A correlated
# subquery rescans the scan once per outer row, and each rescan reuses the frozen key.
# A 300 kB text value gets its own allocation, so a key that still points at the freed
# result crashes the backend. 1655 rows of tx are >= '038345' followed by 'x's.
q "SET pgcolumnar.stripe_row_limit=2000;
   CREATE TABLE tx (s text) USING pgcolumnar;
   INSERT INTO tx SELECT lpad(g::text, 6, '0') FROM generate_series(1, 40000) g;
   CREATE FUNCTION same_t(text) RETURNS text LANGUAGE plpgsql STABLE AS \$\$
	BEGIN RETURN \$1; END \$\$;" >/dev/null
check "a long text key frozen at the first run survives each rescan (#2065)" \
	"$(psql_m "SET plan_cache_mode = force_generic_plan" \
		"PREPARE r(text) AS SELECT g || '|' || (SELECT count(*) FROM tx WHERE s >= same_t(\$1) AND s <= g::text || 'z') FROM generate_series(1, 3) g" \
		"EXECUTE r('038345' || repeat('x', 300000))" | grep -vxE 'SET|PREPARE' | tr '\n' ' ' | sed 's/ $//')" \
	"1|1655 2|1655 3|1655"

check "backend alive" "$(survived "$CRASHES_BEFORE")" "1"

pgc_summary
