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
FN_PLAN="$(psql_m "SET plan_cache_mode = force_generic_plan" \
	"PREPARE g(int) AS SELECT count(*) FROM t WHERE ts >= same(\$1)" \
	"EXPLAIN (ANALYZE, TIMING off, SUMMARY off) EXECUTE g(39000)")"
check "a stable function operand still prunes when the scan runs (19 of 20 removed) (#2065)" \
	"$(sed -n 's/.*Chunk Groups Removed by Filter: \([0-9]*\).*/\1/p' <<<"$FN_PLAN" | head -1)" "19"
# The keys frozen at the first run are also the scan's own keys, which EXPLAIN counts.
# A freeze that handed the keys to the reader alone pruned the same 19 groups and
# reported 0 pushed-down filters.
check "and the same EXPLAIN ANALYZE counts the frozen key as pushed down (#2065)" \
	"$(sed -n 's/.*Columnar Pushed-Down Filters: \([0-9]*\).*/\1/p' <<<"$FN_PLAN" | head -1)" "1"

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
# Two keys frozen at the first run must both be tested. Each key removes groups the
# other keeps: ts >= 2001 removes group 1, and ts <= 4000 removes groups 3 to 20. A scan
# that tests only the first key removes 1, and one that tests only the last removes 18.
check "two keys frozen at the first run both prune (19 of 20 removed) (#2065)" \
	"$(psql_m "SET plan_cache_mode = force_generic_plan" \
		"PREPARE k(int, int) AS SELECT count(*) FROM t WHERE ts >= same(\$1) AND ts <= same(\$2)" \
		"EXPLAIN (ANALYZE, TIMING off, SUMMARY off) EXECUTE k(2001, 4000)" \
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

# A varchar parameter compared with a text column reaches the scan wrapped in a
# RelabelType to text. Begin must see through it and freeze the bound value, so a plain
# EXPLAIN EXECUTE reports it as pushed down, as it does for the int parameter above.
check "a plain EXPLAIN EXECUTE still pushes down a bound varchar parameter (#2065)" \
	"$(psql_m "SET plan_cache_mode = force_generic_plan" "PREPARE bv(varchar) AS SELECT count(*) FROM tx WHERE s >= \$1" \
		"EXPLAIN (COSTS off) EXECUTE bv('039000')" \
		| sed -n 's/.*Columnar Pushed-Down Filters: \([0-9]*\).*/\1/p' | head -1)" "1"
# The same holds for a domain parameter on an int column, which also arrives wrapped in a
# RelabelType.
q "CREATE DOMAIN dint AS int;" >/dev/null
check "a plain EXPLAIN EXECUTE still pushes down a bound domain parameter (#2065)" \
	"$(psql_m "SET plan_cache_mode = force_generic_plan" "PREPARE d(dint) AS SELECT count(*) FROM t WHERE ts >= \$1" \
		"EXPLAIN (COSTS off) EXECUTE d(39000)" \
		| sed -n 's/.*Columnar Pushed-Down Filters: \([0-9]*\).*/\1/p' | head -1)" "1"
# A bound parameter that is NULL must be frozen as NULL. "col >= NULL" matches nothing
# and builds no scan key. Frozen as a non-null Const, an int NULL became the key
# "ts >= 0", and a text NULL a key that pointed at address 0, which crashed the backend.
check "a NULL bound int parameter on a generic plan builds no pushed-down filter (#2065)" \
	"$(psql_m "SET plan_cache_mode = force_generic_plan" "PREPARE ni(int) AS SELECT count(*) FROM t WHERE ts >= \$1" \
		"EXPLAIN (ANALYZE, TIMING off, SUMMARY off) EXECUTE ni(NULL)" \
		| sed -n 's/.*Columnar Pushed-Down Filters: \([0-9]*\).*/\1/p' | head -1)" "0"
check "a NULL bound text parameter on a generic plan counts 0 (#2065)" \
	"$(psql_m "SET plan_cache_mode = force_generic_plan" "PREPARE nt(text) AS SELECT count(*) FROM tx WHERE s >= \$1" \
		"EXECUTE nt(NULL)" | tail -1)" "0"
check "the backend is alive after the NULL text parameter (#2065)" "$(survived "$CRASHES_BEFORE")" "1"

# A hash join attaches its runtime range to the columnar scan after Begin. When the
# first run replaces the scan's predicates with the frozen keys, the range must stay,
# after them. A slip there loses the range's pruning, and two slips crashed the
# backend: one that dropped the range's copy, and one that, with pushdown off, kept
# the old predicate count and wrote the range past the new array. The hash builds on
# d (200 rows, k 8001..8200) and probes f (20000 rows in 20 groups of 1000). Heap
# answers 51|416925 (k 8150..8200).
q "SET pgcolumnar.stripe_row_limit = 1000;
   CREATE TABLE d (k int); INSERT INTO d SELECT generate_series(8001, 8200);
   CREATE TABLE f (k int, p text) USING pgcolumnar;
   INSERT INTO f SELECT g, 'p' || g FROM generate_series(1, 20000) g;
   ANALYZE d; ANALYZE f;" >/dev/null
JOIN_SET=("SET plan_cache_mode = force_generic_plan" "SET enable_nestloop = off" "SET enable_mergejoin = off"
	"SET max_parallel_workers_per_gather = 0")
JOIN_Q="PREPARE j(int) AS SELECT count(*), sum(f.k) FROM f JOIN d ON f.k = d.k WHERE f.k >= same(\$1)"
RR_ON="$(psql_m "${JOIN_SET[@]}" "$JOIN_Q" "EXECUTE j(8150)" \
	"EXPLAIN (ANALYZE, TIMING off, SUMMARY off, COSTS off) EXECUTE j(8150)")"
check "a key frozen at the first run under a hash join's runtime range counts what heap counts (#2065)" \
	"$(grep -xE '[0-9]+\|[0-9]+' <<<"$RR_ON" | head -1)" "51|416925"
check "the scan keeps the runtime range after the frozen key: three skip predicates (#2065)" \
	"$(sed -n 's/.*Columnar Usable Skip Predicates: \([0-9]*\).*/\1/p' <<<"$RR_ON" | head -1)" "3"
check "and the kept runtime range still removes groups (11 removed) (#2065)" \
	"$(sed -n 's/.*Runtime Filter Groups Removed: \([0-9]*\).*/\1/p' <<<"$RR_ON" | head -1)" "11"
check "the backend is alive after the runtime-range arm (#2065)" "$(survived "$CRASHES_BEFORE")" "1"
RR_OFF="$(psql_m "${JOIN_SET[@]}" "SET pgcolumnar.enable_qual_pushdown = off" "$JOIN_Q" \
	"EXPLAIN (ANALYZE, TIMING off, SUMMARY off, COSTS off) EXECUTE j(8150)")"
check "with pushdown off, the scan keeps the runtime range alone: two skip predicates (#2065)" \
	"$(sed -n 's/.*Columnar Usable Skip Predicates: \([0-9]*\).*/\1/p' <<<"$RR_OFF" | head -1)" "2"
check "with pushdown off, the runtime range still reads one chunk group (#2065)" \
	"$(sed -n 's/.*Columnar Chunk Groups Read: \([0-9]*\).*/\1/p' <<<"$RR_OFF" | head -1)" "1"
check "the backend is alive after the runtime-range arm with pushdown off (#2065)" "$(survived "$CRASHES_BEFORE")" "1"

# A rescan of that hash join builds a new runtime range for each outer row and must
# drop the old one. Here a subquery runs the join once per group of d2: group 1 is
# k 8001..8200, group 2 is k 15001..15200. A scan that kept group 1's range while
# testing group 2 pruned every group and counted 0. fh is a heap copy of f, so it
# gives the answer heap gives.
q "CREATE TABLE d2 (grp int, k int);
   INSERT INTO d2 SELECT 1, g FROM generate_series(8001, 8200) g;
   INSERT INTO d2 SELECT 2, g FROM generate_series(15001, 15200) g;
   CREATE TABLE fh AS SELECT * FROM f;
   ANALYZE d2; ANALYZE fh;" >/dev/null
rescan_q() {	# rescan_q TABLE -> the two loops' "grp|count|sum", run twice in one session
	psql_m "${JOIN_SET[@]}" \
		"PREPARE r(int) AS SELECT g.g || '|' || (SELECT count(*)::text || '|' || coalesce(sum($1.k), 0) FROM $1 JOIN d2 ON $1.k = d2.k AND d2.grp = g.g WHERE $1.k >= same(\$1)) FROM generate_series(1, 2) g(g)" \
		"EXECUTE r(8150)" "EXECUTE r(8150)" | grep -xE '[0-9]+\|[0-9]+\|[0-9]+' | tr '\n' ' ' | sed 's/ $//'
}
check "the heap copy answers the hash-join rescan (premise) (#2065)" \
	"$(rescan_q fh)" "1|51|416925 2|200|3020100 1|51|416925 2|200|3020100"
check "a hash-join rescan with a key frozen at the first run counts what heap counts in each loop (#2065)" \
	"$(rescan_q f)" "1|51|416925 2|200|3020100 1|51|416925 2|200|3020100"
check "the backend is alive after the hash-join rescan (#2065)" "$(survived "$CRASHES_BEFORE")" "1"

# A NULL that a stable function returns at the first run must be frozen as NULL too.
# Frozen as a non-null Const, an int NULL became the key "ts >= 0", and a text NULL a
# key that pointed at address 0, which crashed the backend.
check "a NULL from a stable function on an int column builds no pushed-down filter (#2065)" \
	"$(psql_m "SET plan_cache_mode = force_generic_plan" "PREPARE nfi(int) AS SELECT count(*) FROM t WHERE ts >= same(\$1)" \
		"EXPLAIN (ANALYZE, TIMING off, SUMMARY off) EXECUTE nfi(NULL)" \
		| sed -n 's/.*Columnar Pushed-Down Filters: \([0-9]*\).*/\1/p' | head -1)" "0"
check "a NULL from a stable function on a text column counts 0 (#2065)" \
	"$(psql_m "SET plan_cache_mode = force_generic_plan" "PREPARE nf(text) AS SELECT count(*) FROM tx WHERE s >= same_t(\$1)" \
		"EXECUTE nf(NULL)" | tail -1)" "0"
check "the backend is alive after the NULL from a stable function (#2065)" "$(survived "$CRASHES_BEFORE")" "1"

# The scan evaluates an operand only when its clause could become a scan key. "<>"
# has no btree strategy, and "ts + 0" is not a bare column, so neither clause can be
# a key; btree leaves them in its Filter and heap tests them per row. ts < 0 holds
# for no row, so heap and btree never call the operand and count 0.
#
# Each boomc function counts its calls in the sequence ncalls and then raises. A
# sequence keeps its value when the query fails, so "COUNT|CALLS" shows a call even
# where main's code swallowed the error and still counted 0 (review of 5f90e108).
# Every function takes an int, so every arm runs EXECUTE nc(0).
q "CREATE SEQUENCE ncalls;
   CREATE FUNCTION boomc(int) RETURNS int LANGUAGE plpgsql STABLE AS \$\$
	BEGIN PERFORM nextval('ncalls'); RAISE EXCEPTION 'boomc called'; END \$\$;
   CREATE FUNCTION boomc_t(int) RETURNS text LANGUAGE plpgsql STABLE AS \$\$
	BEGIN PERFORM nextval('ncalls'); RAISE EXCEPTION 'boomc_t called'; END \$\$;
   CREATE FUNCTION boomc_ts(int) RETURNS timestamp LANGUAGE plpgsql STABLE AS \$\$
	BEGIN PERFORM nextval('ncalls'); RAISE EXCEPTION 'boomc_ts called'; END \$\$;
   CREATE FUNCTION boomc_arr(int) RETURNS int[] LANGUAGE plpgsql STABLE AS \$\$
	BEGIN PERFORM nextval('ncalls'); RAISE EXCEPTION 'boomc_arr called'; END \$\$;
   CREATE FUNCTION boomc_r(int) RETURNS tsrange LANGUAGE plpgsql STABLE AS \$\$
	BEGIN PERFORM nextval('ncalls'); RAISE EXCEPTION 'boomc_r called'; END \$\$;" >/dev/null
nc_arm() {	# nc_arm "TABLE WHERE ..." -> "COUNT|CALLS"; COUNT is the error line if the query raised
	local out
	out="$(psql_m "SET plan_cache_mode = force_generic_plan" "SET max_parallel_workers_per_gather = 0" \
		"SELECT setval('ncalls', 1, false)" "PREPARE nc(int) AS SELECT count(*) FROM $1" "EXECUTE nc(0)" \
		"SELECT CASE WHEN is_called THEN last_value ELSE 0 END FROM ncalls")"
	echo "$(sed -n '5p' <<<"$out")|$(tail -1 <<<"$out")"
}
check "a stable operand of a clause with no btree strategy (<>) is not evaluated (#2065)" \
	"$(nc_arm "t WHERE ts < 0 AND ts <> boomc(\$1)")" "0|0"
check "a stable operand compared with an expression of the column is not evaluated (#2065)" \
	"$(nc_arm "t WHERE ts < 0 AND ts + 0 > boomc(\$1)")" "0|0"

# tk holds each clause shape the scan can key, 20 groups of 2000 rows: k int, sc a
# C-collated text, sn the same text under a collation that is not C, v varchar, tsp a
# timestamp one hour per row from 2020-01-01, and r the hour that starts at tsp. tkh
# is a heap copy.
NONC="$(q "SELECT collname FROM pg_collation
           WHERE collname IN ('en_US.utf8', 'en_US.UTF-8', 'en_US', 'und-x-icu')
           AND collencoding IN (-1, pg_char_to_encoding(current_setting('server_encoding')))
           ORDER BY collname = 'und-x-icu', collname LIMIT 1;")"
check "premise: sn's collation orders a lower-case letter before an upper-case one, which C does not (#2065)" \
	"$(q "SELECT ('a' < 'B' COLLATE \"$NONC\")::text || '|' || ('a' < 'B' COLLATE \"C\")::text;")" "true|false"
q "SET pgcolumnar.stripe_row_limit=2000;
   CREATE TABLE tk (k int, sc text COLLATE \"C\", sn text COLLATE \"$NONC\", v varchar, tsp timestamp, r tsrange,
	tsz timestamptz) USING pgcolumnar;
   INSERT INTO tk SELECT g, lpad(g::text, 6, '0'), lpad(g::text, 6, '0'), lpad(g::text, 6, '0'),
	timestamp '2020-01-01' + g * interval '1 hour',
	tsrange(timestamp '2020-01-01' + g * interval '1 hour', timestamp '2020-01-01' + (g + 1) * interval '1 hour'),
	timestamptz '2020-01-01 00:00+00' + g * interval '1 hour'
	FROM generate_series(1, 40000) g;
   CREATE TABLE tkh AS SELECT * FROM tk;" >/dev/null

# Three shapes that can never be keys (review of 5f90e108, blocker 1). LIKE builds a
# key only under the C collation; date_trunc builds one only for a btree operator and
# a bare timestamp column.
check "a stable operand of LIKE on a column whose collation is not C is not evaluated (#2065)" \
	"$(nc_arm "tk WHERE sn < '' AND sn LIKE boomc_t(\$1)")" "0|0"
check "a stable operand of date_trunc with no btree strategy (<>) is not evaluated (#2065)" \
	"$(nc_arm "tk WHERE tsp < '2000-01-01' AND date_trunc('day', tsp) <> boomc_ts(\$1)")" "0|0"
check "a stable operand of date_trunc over an expression of the column is not evaluated (#2065)" \
	"$(nc_arm "tk WHERE tsp < '2000-01-01' AND date_trunc('day', tsp + interval '1 hour') = boomc_ts(\$1)")" "0|0"
check "a stable operand of date_trunc with a unit read from a column is not evaluated (#2065)" \
	"$(nc_arm "tk WHERE tsp < '2000-01-01' AND date_trunc(sc, tsp) = boomc_ts(\$1)")" "0|0"
# More shapes that can never be keys (blocker 2): each one pins a branch of the test.
check "a stable operand on the column side of LIKE is not evaluated (#2065)" \
	"$(nc_arm "tk WHERE sc < '' AND boomc_t(\$1) LIKE sc")" "0|0"
check "a stable array operand of <> ANY is not evaluated (#2065)" \
	"$(nc_arm "t WHERE ts < 0 AND ts <> ANY(boomc_arr(\$1))")" "0|0"
check "a stable array operand of = ALL is not evaluated (#2065)" \
	"$(nc_arm "t WHERE ts < 0 AND ts = ALL(boomc_arr(\$1))")" "0|0"
check "a stable operand of range @> range is not evaluated (#2065)" \
	"$(nc_arm "tk WHERE tsp < '2000-01-01' AND r @> boomc_r(\$1)")" "0|0"
check "a stable operand compared under a collation that is not the column's is not evaluated (#2065)" \
	"$(nc_arm "tk WHERE sn < '' AND sn > boomc_t(\$1) COLLATE \"C\"")" "0|0"
check "a stable operand compared with a function of the column other than date_trunc is not evaluated (#2065)" \
	"$(nc_arm "t WHERE ts < 0 AND abs(ts) > boomc(\$1)")" "0|0"

# Five shapes that are keys, each with a bound parameter: they must still prune, and
# count what heap counts.
prune_arm() {	# prune_arm TYPE "TABLE WHERE ..." VALUE -> "REMOVED|COUNT"
	local out
	out="$(psql_m "SET plan_cache_mode = force_generic_plan" "SET max_parallel_workers_per_gather = 0" \
		"PREPARE pa($1) AS SELECT count(*) FROM $2" \
		"EXPLAIN (ANALYZE, TIMING off, SUMMARY off) EXECUTE pa($3)" "EXECUTE pa($3)")"
	echo "$(sed -n 's/.*Chunk Groups Removed by Filter: \([0-9]*\).*/\1/p' <<<"$out" | head -1)|$(tail -1 <<<"$out")"
}
heap_count() {	# heap_count TYPE "TABLE WHERE ..." VALUE -> the count on a generic plan
	psql_m "SET plan_cache_mode = force_generic_plan" "PREPARE ha($1) AS SELECT count(*) FROM $2" \
		"EXECUTE ha($3)" | tail -1
}
check "the heap copy answers the five pruning queries (premise) (#2065)" \
	"$(heap_count timestamp "tkh WHERE date_trunc('day', tsp) = \$1" "'2024-07-01'") $(heap_count text "tkh WHERE sc LIKE \$1" "'0390%'") $(heap_count tsrange "tkh WHERE r && \$1" "'[2024-07-01,2024-07-02)'") $(heap_count text "tkh WHERE v >= \$1" "'039001'") $(heap_count 'int[]' "tkh WHERE k = ANY(\$1)" "'{39500,39600}'")" \
	"24 100 24 1000 2"
check "a bound parameter compared with date_trunc of a timestamp column prunes (#2065)" \
	"$(prune_arm timestamp "tk WHERE date_trunc('day', tsp) = \$1" "'2024-07-01'")" "19|24"
check "a bound parameter in LIKE on a C-collated column prunes (#2065)" \
	"$(prune_arm text "tk WHERE sc LIKE \$1" "'0390%'")" "19|100"
check "a bound parameter that a range column overlaps prunes (#2065)" \
	"$(prune_arm tsrange "tk WHERE r && \$1" "'[2024-07-01,2024-07-02)'")" "19|24"
check "a bound text parameter compared with a varchar column prunes (#2065)" \
	"$(prune_arm text "tk WHERE v >= \$1" "'039001'")" "19|1000"
check "a bound array parameter of = ANY prunes (#2065)" \
	"$(prune_arm 'int[]' "tk WHERE k = ANY(\$1)" "'{39500,39600}'")" "19|2"

# Shapes that can never be keys because a key builder declines them (review of
# 27aa4c46, blocker 1). pgcolumnar_preimage_range_scankey keys date_trunc only for a
# timestamp column, a timestamp operand and a unit in its table; the range branch
# keys && only against the column's own range type. tz holds a timestamp domain
# column and a timestamptz column; no row matches its "< 2000-01-01" filters.
q "CREATE DOMAIN tsdom AS timestamp;
   CREATE TABLE tz (tsd tsdom, tsz timestamptz) USING pgcolumnar;
   INSERT INTO tz SELECT timestamp '2020-01-01' + g * interval '1 hour', timestamptz '2020-01-01' + g * interval '1 hour'
	FROM generate_series(1, 100) g;
   CREATE FUNCTION boomc_tstz(int) RETURNS timestamptz LANGUAGE plpgsql STABLE AS \$\$
	BEGIN PERFORM nextval('ncalls'); RAISE EXCEPTION 'boomc_tstz called'; END \$\$;
   CREATE FUNCTION boomc_date(int) RETURNS date LANGUAGE plpgsql STABLE AS \$\$
	BEGIN PERFORM nextval('ncalls'); RAISE EXCEPTION 'boomc_date called'; END \$\$;
   CREATE FUNCTION boomc_mr(int) RETURNS tsmultirange LANGUAGE plpgsql STABLE AS \$\$
	BEGIN PERFORM nextval('ncalls'); RAISE EXCEPTION 'boomc_mr called'; END \$\$;" >/dev/null
check "a stable timestamptz operand compared with date_trunc of a timestamp column is not evaluated (#2065)" \
	"$(nc_arm "tk WHERE tsp < '2000-01-01' AND date_trunc('day', tsp) = boomc_tstz(\$1)")" "0|0"
check "a stable date operand compared with date_trunc of a timestamp column is not evaluated (#2065)" \
	"$(nc_arm "tk WHERE tsp < '2000-01-01' AND date_trunc('day', tsp) = boomc_date(\$1)")" "0|0"
check "a stable operand of date_trunc to a decade is not evaluated (#2065)" \
	"$(nc_arm "tk WHERE tsp < '2000-01-01' AND date_trunc('decade', tsp) = boomc_ts(\$1)")" "0|0"
check "a stable operand of date_trunc to a plural unit (days) is not evaluated (#2065)" \
	"$(nc_arm "tk WHERE tsp < '2000-01-01' AND date_trunc('days', tsp) = boomc_ts(\$1)")" "0|0"
check "a stable operand of date_trunc of a timestamp domain column is not evaluated (#2065)" \
	"$(nc_arm "tz WHERE tsd < '2000-01-01' AND date_trunc('day', tsd) = boomc_ts(\$1)")" "0|0"
check "a stable multirange operand that a range column overlaps is not evaluated (#2065)" \
	"$(nc_arm "tk WHERE tsp < '2000-01-01' AND r && boomc_mr(\$1)")" "0|0"
# blocker 2 and the attacker's two items
check "a stable operand of date_trunc of a timestamptz column is not evaluated (#2065)" \
	"$(nc_arm "tz WHERE tsz < '2000-01-01' AND date_trunc('day', tsz) = boomc_tstz(\$1)")" "0|0"
# trunc_k(text, int) has date_trunc's shape, a unit and a column, but is not date_trunc.
q "CREATE FUNCTION trunc_k(text, int) RETURNS timestamp LANGUAGE plpgsql IMMUTABLE AS \$\$
	BEGIN RETURN timestamp '2020-01-01' + \$2 * interval '1 hour'; END \$\$;" >/dev/null
check "a stable operand compared with a two-argument function of the column other than date_trunc is not evaluated (#2065)" \
	"$(nc_arm "tk WHERE k < 0 AND trunc_k('day', k) = boomc_ts(\$1)")" "0|0"
check "a stable array operand of < ANY is not evaluated (#2065)" \
	"$(nc_arm "tk WHERE k < 0 AND k < ANY(boomc_arr(\$1))")" "0|0"
check "the heap copy answers the date_trunc >= and range @> pruning queries (premise) (#2065)" \
	"$(heap_count timestamp "tkh WHERE date_trunc('day', tsp) >= \$1" "'2024-07-01'") $(heap_count timestamp "tkh WHERE r @> \$1" "'2024-07-01 00:30'")" \
	"569 1"
check "a bound parameter that date_trunc of a timestamp column is >= prunes (#2065)" \
	"$(prune_arm timestamp "tk WHERE date_trunc('day', tsp) >= \$1" "'2024-07-01'")" "19|569"
check "a bound parameter that a range column contains prunes (#2065)" \
	"$(prune_arm timestamp "tk WHERE r @> \$1" "'2024-07-01 00:30'")" "19|1"

# A timestamp column compared with a timestamptz value never prunes: the reader drops
# that key whatever the value (#1738). So its operand must not be called either
# (review of ff1fc62f, blocker 1).
q "CREATE FUNCTION boomc_tstzarr(int) RETURNS timestamptz[] LANGUAGE plpgsql STABLE AS \$\$
	BEGIN PERFORM nextval('ncalls'); RAISE EXCEPTION 'boomc_tstzarr called'; END \$\$;" >/dev/null
check "a stable timestamptz operand compared with a timestamp column is not evaluated (#2065)" \
	"$(nc_arm "tk WHERE tsp < '2000-01-01' AND tsp > boomc_tstz(\$1)")" "0|0"
check "a stable timestamptz array operand of = ANY on a timestamp column is not evaluated (#2065)" \
	"$(nc_arm "tk WHERE tsp < '2000-01-01' AND tsp = ANY(boomc_tstzarr(\$1))")" "0|0"
check "a stable timestamptz operand compared with a timestamp domain column is not evaluated (#2065)" \
	"$(nc_arm "tz WHERE tsd < '2000-01-01' AND tsd > boomc_tstz(\$1)")" "0|0"

# A bound parameter of a domain type, or varchar, reaches the scan inside a
# RelabelType. Its frozen Const must take the relabelled type, as main's did, or the
# LIKE, date_trunc and range key builders, which need an exact type, decline it
# (review of ff1fc62f, the attacker's blocker A). A timestamptz column with a
# timestamptz parameter still prunes, and date_trunc reads its unit in any case.
q "CREATE DOMAIN tdom AS text; CREATE DOMAIN rdom AS tsrange;" >/dev/null
check "the heap copy answers the bound domain and varchar pruning queries (premise) (#2065)" \
	"$(heap_count varchar "tkh WHERE sc LIKE \$1" "'0390%'") $(heap_count tdom "tkh WHERE sc LIKE \$1" "'0390%'") $(heap_count tsdom "tkh WHERE date_trunc('day', tsp) = \$1" "'2024-07-01'") $(heap_count tsdom "tkh WHERE date_trunc('day', tsp) >= \$1" "'2024-07-01'") $(heap_count rdom "tkh WHERE r && \$1" "'[2024-07-01,2024-07-02)'") $(heap_count timestamp "tkh WHERE date_trunc('Day', tsp) = \$1" "'2024-07-01'") $(heap_count timestamptz "tkh WHERE tsz >= \$1" "'2024-07-01 00:00+00'")" \
	"100 100 24 569 24 24 569"
check "a bound varchar parameter in LIKE on a C-collated column prunes (#2065)" \
	"$(prune_arm varchar "tk WHERE sc LIKE \$1" "'0390%'")" "19|100"
check "a bound text domain parameter in LIKE on a C-collated column prunes (#2065)" \
	"$(prune_arm tdom "tk WHERE sc LIKE \$1" "'0390%'")" "19|100"
check "a bound timestamp domain parameter compared with date_trunc prunes (#2065)" \
	"$(prune_arm tsdom "tk WHERE date_trunc('day', tsp) = \$1" "'2024-07-01'")" "19|24"
check "a bound timestamp domain parameter that date_trunc is >= prunes (#2065)" \
	"$(prune_arm tsdom "tk WHERE date_trunc('day', tsp) >= \$1" "'2024-07-01'")" "19|569"
check "a bound range domain parameter that a range column overlaps prunes (#2065)" \
	"$(prune_arm rdom "tk WHERE r && \$1" "'[2024-07-01,2024-07-02)'")" "19|24"
check "a bound parameter compared with date_trunc to an upper-case unit (Day) prunes (#2065)" \
	"$(prune_arm timestamp "tk WHERE date_trunc('Day', tsp) = \$1" "'2024-07-01'")" "19|24"
check "a bound timestamptz parameter compared with a timestamptz column prunes (#2065)" \
	"$(prune_arm timestamptz "tk WHERE tsz >= \$1" "'2024-07-01 00:00+00'")" "19|569"

# The projection and rescan arms above check answers; these check that the keys
# frozen at the first run still prune there (the attacker's surviving mutants). The
# rescan arm reads "Chunk Groups Read|Removed by Filter|Runtime Filter Groups Removed"
# for f.k >= same(19500), a key that removes more than either loop's runtime range.
# prsc's rows are in random order, so its groups cannot prune; prsk holds the same
# rows in k order, so its projection's groups can.
q "CREATE TABLE prsk (k int, payload text) USING pgcolumnar;
   SELECT pgcolumnar.set_options('prsk', chunk_group_row_limit => 1000, stripe_row_limit => 1000);
   INSERT INTO prsk SELECT k, 'p' || k FROM generate_series(1, 20000) k;
   SELECT pgcolumnar.add_projection('prsk', 'bykk', ARRAY['k'], ARRAY['k']);
   ANALYZE prsk;" >/dev/null
check "a projection scan with a key frozen at the first run removes groups (#2065)" \
	"$(psql_m "${PROJ_SET[@]}" "EXPLAIN (ANALYZE, TIMING off, SUMMARY off, COSTS off) SELECT count(*), sum(k) FROM prsk WHERE k >= 100 AND k <= same(900)" \
		| grep -E 'Columnar Projection: bykk|Chunk Groups Removed by Filter' | sed 's/.*: //' | tr '\n' '|' | sed 's/|$//')" "bykk|19"
check "a hash-join rescan with a key frozen at the first run removes groups in each loop (#2065)" \
	"$(psql_m "${JOIN_SET[@]}" \
		"PREPARE r(int) AS SELECT g.g || '|' || (SELECT count(*)::text || '|' || coalesce(sum(f.k), 0) FROM f JOIN d2 ON f.k = d2.k AND d2.grp = g.g WHERE f.k >= same(\$1)) FROM generate_series(1, 2) g(g)" \
		"EXPLAIN (ANALYZE, TIMING off, SUMMARY off, COSTS off) EXECUTE r(19500)" \
		| grep -E 'Chunk Groups Removed by Filter|Runtime Filter Groups Removed|Chunk Groups Read' | sed 's/.*: //' | tr '\n' '|' | sed 's/|$//')" "0|40|1"

check "backend alive" "$(survived "$CRASHES_BEFORE")" "1"

pgc_summary
