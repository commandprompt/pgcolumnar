"""Chunk-group skipping for parameterized predicates. The pytest twin of
`test/native_param_pushdown.sh`, every check by the shell's name (#1341).

The scan-key builder accepts only a Const operand, so Begin freezes execution-stable
operands (a PARAM_EXTERN, or a subexpression with no Var, no PARAM_EXEC and no volatile
function) into Consts before building the keys. A correlated PARAM_EXEC (a nestloop
inner scan's reference to the outer row) changes per rescan and the keys are built
once, so it must NOT be frozen.

Three things the shell's first version could not see, now read in both twins:
- with the PARAM_EXEC guard removed the lower-bound correlated arm still answered
  3002, because Begin runs before any outer row sets the param and "ts >= <its
  initial value>" prunes nothing. The arm now reads the scan's own "Usable Skip
  Predicates" and asks the upper bound too, where a frozen initial value prunes all;
- every arm ran one execution per session, so a value frozen once and reused by a
  later execution passed. Two executions in one session now each count and prune
  with their own value;
- survival was a bare SELECT 1; it now also reads the server log's crash count.

An error raised while Begin freezes a stable operand must reach the client (#2065).
Begin caught it with PG_TRY and threw it away, with no subtransaction to release what
the failed call held: a statement_timeout during that evaluation was lost, the query
returned rows, and the failed call's plancache, snapshot and SPI state leaked as
WARNINGs. The functions misbehave on their FIRST call only, on a generic plan, where
the planner cannot fold "fn($1)", so that first call is the one the scan makes.

A scan that is set up but never run must not call the operand at all: an Append arm
that LIMIT cuts off, or a branch under a false one-time filter. A btree index scan
evaluates its runtime keys when it first runs, and heap per row, so both answer.
That includes a PL/pgSQL variable, whose parameter list has a fetch hook that
evaluates it. The scan freezes the operand once, at its first run, and hands a
projection scan the keys mapped onto the projection's own columns.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading. A failed query is a `query_error()`, so two failures never agree.
"""
import re
import time

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_native_param_pushdown"
CORR_GE = "SELECT sum(c) FROM thr, LATERAL (SELECT count(*) c FROM t WHERE t.ts >= thr.lo) s"
CORR_LE = "SELECT sum(c) FROM thr, LATERAL (SELECT count(*) c FROM t WHERE t.ts <= thr.lo) s"
GENERIC = "SET plan_cache_mode = force_generic_plan"


def _failed(exc):
    return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _await_up(dsn):
    for _ in range(60):
        try:
            with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as conn:
                conn.execute("SELECT 1")
            return
        except psycopg.Error:
            time.sleep(0.5)


def _session(dsn, statements):
    """Run `statements` in ONE session and return the rows of each, as text lists, or
    the failure as a query_error()."""
    _await_up(dsn)
    out = []
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for sql in statements:
                cur = conn.execute(sql)
                rows = cur.fetchall() if cur.description else []
                out.append(["" if r[0] is None else str(r[0]) for r in rows])
    except psycopg.Error as exc:
        return _failed(exc)
    return out


def _value(dsn, sql):
    res = _session(dsn, [sql])
    if isinstance(res, str):
        return res
    return res[0][0] if res[0] else query_error(f"no row from {sql}")


def _field(lines, label):
    """-> the number after `label:` in an EXPLAIN, or why there is none."""
    if isinstance(lines, str):
        return lines
    for line in lines:
        m = re.search(re.escape(label) + r": (\d+)", line)
        if m:
            return m.group(1)
    return query_error(f"the plan has no {label} line")


FIRST_CALL = """CREATE FUNCTION {name}(int) RETURNS int LANGUAGE plpgsql STABLE AS $$
    DECLARE n int := coalesce(nullif(current_setting('t.n', true), ''), '0')::int + 1;
    BEGIN PERFORM set_config('t.n', n::text, false);
    IF n = 1 THEN {act}; END IF; RETURN $1; END $$"""

# boom() raises on every call. same() is PL/pgSQL so the planner cannot inline it into
# a bare parameter.
BOOM = """CREATE FUNCTION boom(int) RETURNS int LANGUAGE plpgsql STABLE AS $$
    BEGIN RAISE EXCEPTION 'boom called'; END $$"""
SAME = """CREATE FUNCTION same(int) RETURNS int LANGUAGE plpgsql STABLE AS $$
    BEGIN RETURN $1; END $$"""
# cntc() counts its calls in t.n and never raises. A function that raised would roll
# back its own count, so t.n would read 0 whether it was called or not.
CNTC = """CREATE FUNCTION cntc(int) RETURNS int LANGUAGE plpgsql STABLE AS $$
    DECLARE n int := coalesce(nullif(current_setting('t.n', true), ''), '0')::int + 1;
    BEGIN PERFORM set_config('t.n', n::text, false); RETURN $1; END $$"""
# A PL/pgSQL optional filter: with use_filter false the record r is never assigned,
# the plan's one-time filter is false, and the scan never runs.
OPT_EQ = """CREATE FUNCTION opt_eq(use_filter bool) RETURNS bigint LANGUAGE plpgsql AS $$
    DECLARE r record; c bigint;
    BEGIN
      IF use_filter THEN SELECT 39500 AS id INTO r; END IF;
      SELECT count(*) INTO c FROM t WHERE use_filter AND ts = r.id;
      RETURN c;
    END $$"""
# A PL/pgSQL variable is frozen only when the scan first runs. A plain EXPLAIN inside
# PL/pgSQL inlines the value, so auto_explain logs the generic plan the function ran.
LO_CNT = """CREATE FUNCTION lo_cnt(lo int) RETURNS bigint LANGUAGE plpgsql AS $$
    DECLARE c bigint; BEGIN SELECT count(*) INTO c FROM t WHERE ts >= lo; RETURN c; END $$"""
AUTO_EXPLAIN = ["LOAD 'auto_explain'", "SET auto_explain.log_min_duration = 0",
                "SET auto_explain.log_analyze = on", "SET auto_explain.log_timing = off",
                "SET auto_explain.log_nested_statements = on", "SET client_min_messages = log"]
PROJ_SET = ["SET pgcolumnar.enable_projection_scan = on", "SET max_parallel_workers_per_gather = 0",
            "SET pgcolumnar.enable_ungrouped_vector_agg = off",
            "SET pgcolumnar.enable_group_vectorization = off"]
PROJ_LOW = "SELECT count(*), sum(k) FROM prsc WHERE k >= 100 AND k <= same(900)"
PROJ_HIGH = "SELECT count(*), sum(k) FROM prsc WHERE k >= 19990 AND k <= same(20000)"
# A correlated subquery rescans the columnar scan once per outer row, and each rescan
# reuses the key frozen at the first run. A 300 kB text value gets its own allocation, so
# a key that still points at the freed result crashes the backend.
SAME_T = """CREATE FUNCTION same_t(text) RETURNS text LANGUAGE plpgsql STABLE AS $$
    BEGIN RETURN $1; END $$"""
TEXT_RESCAN = ("PREPARE r(text) AS SELECT g || '|' || (SELECT count(*) FROM tx WHERE s >= same_t($1) "
               "AND s <= g::text || 'z') FROM generate_series(1, 3) g")


def _transcript(dsn, statements):
    """Run `statements` in ONE session and return everything the client saw, one line
    per row, notice or raised error: "WARNING: ...", "raised: ...". The first raised
    error ends the session, as psql's -c sequence does not; nothing after it matters
    here. A raised error is the reading under test, compared with a fixed message; a
    session that cannot connect at all is a query_error()."""
    _await_up(dsn)
    lines = []
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.add_notice_handler(lambda d: lines.append(f"{d.severity}: {d.message_primary}"))
            for sql in statements:
                try:
                    cur = conn.execute(sql)
                except psycopg.Error as exc:
                    lines.append(f"raised: {exc.diag.message_primary}")
                    break
                for row in (cur.fetchall() if cur.description else []):
                    lines.append("" if row[0] is None else str(row[0]))
    except psycopg.Error as exc:
        return _failed(exc)
    return lines


def _proj_answers(dsn):
    """-> "count|sum count|sum" for the two projection queries, as psql -At prints them."""
    _await_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for sql in PROJ_SET:
                conn.execute(sql)
            return " ".join("|".join("" if v is None else str(v) for v in conn.execute(q).fetchone())
                            for q in (PROJ_LOW, PROJ_HIGH))
    except psycopg.Error as exc:
        return _failed(exc)


def _count(lines, needle):
    if isinstance(lines, str):
        return lines
    return str(sum(1 for line in lines if needle in line))


def _crashes(cluster):
    try:
        return (cluster.datadir / "server.log").read_text(errors="replace").count("terminated by signal")
    except OSError as exc:
        return query_error(f"crash count unreadable: {exc}")


def _survived(dsn, cluster, before):
    up, now = _value(dsn, "SELECT 1"), _crashes(cluster)
    if not isinstance(before, int) or not isinstance(now, int):
        return query_error(f"crash count unreadable: {before} / {now}")
    if up == "1" and now == before:
        return "1"
    _await_up(dsn)
    return f"no ([{up}], {now - before} backend crashes)"


@pytest.fixture(scope="module")
def pp(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        # 40000 rows / 2000 = 20 monotonic groups; ts >= 39000 lives in the last group.
        _session(dsn, ["CREATE EXTENSION IF NOT EXISTS pgcolumnar",
                       "SET pgcolumnar.stripe_row_limit = 2000",
                       "CREATE TABLE t (id int, ts int) USING pgcolumnar",
                       "INSERT INTO t SELECT g, g FROM generate_series(1, 40000) g",
                       "CREATE TABLE thr (lo int)",
                       "INSERT INTO thr VALUES (39000), (38000)"])
        before = _crashes(pgc_cluster)
        r = {}
        plan = _session(dsn, [GENERIC, "DEALLOCATE ALL",
                              "PREPARE p(int) AS SELECT count(*) FROM t WHERE ts >= $1",
                              "EXPLAIN (ANALYZE, TIMING off, SUMMARY off) EXECUTE p(39000)"])
        r["removed"] = _field(plan if isinstance(plan, str) else plan[-1], "Chunk Groups Removed by Filter")
        cnt = _session(dsn, [GENERIC, "DEALLOCATE ALL",
                             "PREPARE c(int) AS SELECT count(*) FROM t WHERE ts >= $1",
                             "EXECUTE c(39000)"])
        r["prep count"] = cnt if isinstance(cnt, str) else (cnt[-1][0] if cnt[-1] else query_error("no count"))
        r["literal count"] = _value(dsn, "SELECT count(*) FROM t WHERE ts >= 39000")
        corr = _session(dsn, [f"EXPLAIN (ANALYZE, TIMING off, SUMMARY off, COSTS off) {CORR_GE}"])
        corr = corr if isinstance(corr, str) else corr[0]
        r["corr rescans"] = (corr if isinstance(corr, str) else
                             str(sum(1 for l in corr if re.search(
                                 r"Custom Scan \(PgColumnarScan\) on t \(actual rows=[\d.]+ loops=2\)", l))))
        r["corr skip preds"] = _field(corr, "Columnar Usable Skip Predicates")
        r["corr ge"] = _value(dsn, CORR_GE)
        r["corr le"] = _value(dsn, CORR_LE)
        twice = _session(dsn, [GENERIC, "DEALLOCATE ALL",
                               "PREPARE w(int) AS SELECT count(*) FROM t WHERE ts >= $1",
                               "EXECUTE w(39000)", "EXECUTE w(38000)",
                               "EXPLAIN (ANALYZE, TIMING off, SUMMARY off) EXECUTE w(38000)"])
        if isinstance(twice, str):
            r["twice count"] = r["twice removed"] = twice
        else:
            r["twice count"] = twice[4][0] if twice[4] else query_error("no second count")
            r["twice removed"] = _field(twice[5], "Chunk Groups Removed by Filter")
        _session(dsn, [FIRST_CALL.format(name="slow_first", act="PERFORM pg_sleep(2)"),
                       FIRST_CALL.format(name="fail_first", act="RAISE EXCEPTION 'call 1 fails'")])
        timeout = _transcript(dsn, [GENERIC, "PREPARE s(int) AS SELECT count(*) FROM t WHERE ts > slow_first($1)",
                                    "SET t.n = '0'", "SET statement_timeout = '300ms'", "EXECUTE s(0)"])
        r["timeout"] = _count(timeout, "raised: canceling statement due to statement timeout")
        fail = _transcript(dsn, [GENERIC, "PREPARE f(int) AS SELECT count(*) FROM t WHERE ts > fail_first($1)",
                                 "SET t.n = '0'", "EXECUTE f(0)"])
        r["fail once"] = _count(fail, "raised: call 1 fails")
        r["warnings"] = (timeout if isinstance(timeout, str) else fail if isinstance(fail, str)
                         else _count(timeout + fail, "WARNING"))
        _session(dsn, [CNTC])
        expl = _transcript(dsn, [GENERIC, "PREPARE e(int) AS SELECT count(*) FROM t WHERE ts > cntc($1)",
                                 "SET t.n = '0'", "EXPLAIN (COSTS off) EXECUTE e(0)", "SHOW t.n"])
        r["explain calls"] = expl if isinstance(expl, str) else (expl[-1] if expl else query_error("no SHOW"))
        bound = _transcript(dsn, [GENERIC, "PREPARE b(int) AS SELECT count(*) FROM t WHERE ts >= $1",
                                  "EXPLAIN (COSTS off) EXECUTE b(39000)"])
        r["explain bound"] = _field(bound, "Columnar Pushed-Down Filters")
        # A scan that is set up but never run calls no operand; boom() always raises.
        _session(dsn, [BOOM, SAME])
        cut = _transcript(dsn, [GENERIC, "PREPARE l(int) AS SELECT 1 UNION ALL "
                                "(SELECT ts FROM t WHERE ts > boom($1)) LIMIT 1", "EXECUTE l(0)"])
        r["limit cut"] = cut if isinstance(cut, str) else (cut[-1] if cut else query_error("no row"))
        off = _transcript(dsn, [GENERIC, "PREPARE o(int, int) AS SELECT count(*) FROM t "
                                "WHERE ts > boom($1) AND $2 = 0", "EXECUTE o(0, 1)"])
        r["one-time off"] = off if isinstance(off, str) else (off[-1] if off else query_error("no row"))
        fn = _transcript(dsn, [GENERIC, "PREPARE g(int) AS SELECT count(*) FROM t WHERE ts >= same($1)",
                               "EXPLAIN (ANALYZE, TIMING off, SUMMARY off) EXECUTE g(39000)"])
        r["fn removed"] = _field(fn, "Chunk Groups Removed by Filter")
        # One call to freeze the operand, then one per row the Filter tests.
        once = _transcript(dsn, [GENERIC, "PREPARE n(int) AS SELECT count(*) FROM t WHERE ts >= cntc($1)",
                                 "SET t.n = '0'",
                                 "EXPLAIN (ANALYZE, COSTS off, TIMING off, SUMMARY off) EXECUTE n(39001)",
                                 "SHOW t.n"])
        if isinstance(once, str):
            r["once calls"] = r["once want"] = once
        else:
            rows = next((m.group(1) for l in once for m in [re.search(
                r"Custom Scan \(PgColumnarScan\) on t \(actual rows=(\d+)", l)] if m), None)
            removed = next((m.group(1) for l in once for m in [re.search(
                r"Rows Removed by Filter: (\d+)", l)] if m), None)
            r["once calls"] = once[-1] if once else query_error("no SHOW")
            r["once want"] = (str(1 + int(rows) + int(removed)) if rows and int(rows) > 0 and removed
                              else query_error(f"no rows or Rows Removed by Filter: [{rows}] [{removed}]"))
        _session(dsn, [OPT_EQ])
        opt = _transcript(dsn, [GENERIC, "SELECT opt_eq(true)", "SELECT opt_eq(false)"])
        r["plpgsql one-time off"] = opt if isinstance(opt, str) else " ".join(opt)
        _session(dsn, [LO_CNT])
        lo = _transcript(dsn, [GENERIC] + AUTO_EXPLAIN + ["SELECT lo_cnt(39000)"])
        r["plpgsql premise"] = _count(lo, "Filter: (ts >= $1)")
        r["plpgsql removed"] = _field(lo, "Chunk Groups Removed by Filter")
        _session(dsn, ["CREATE TABLE prsc (k int, payload text) USING pgcolumnar",
                       "SELECT pgcolumnar.set_options('prsc', chunk_group_row_limit => 1000, "
                       "stripe_row_limit => 1000)",
                       "INSERT INTO prsc SELECT k, 'p' || k FROM generate_series(1, 20000) k "
                       "ORDER BY md5(k::text)",
                       "SELECT pgcolumnar.add_projection('prsc', 'byk', ARRAY['k'], ARRAY['k'])",
                       "ANALYZE prsc"])
        pplan = _session(dsn, PROJ_SET + [f"EXPLAIN (COSTS off) {PROJ_LOW}"])
        r["proj premise"] = (pplan if isinstance(pplan, str)
                             else str(sum(1 for l in pplan[-1] if "Columnar Projection: byk" in l)))
        # Heap answers 801|400500 (k 100..900) and 11|219945 (k 19990..20000).
        r["proj answers"] = _proj_answers(dsn)
        # The keys frozen at the first run obey pgcolumnar.enable_qual_pushdown = off, as
        # Begin's keys do; with pushdown on the same query removes 19 ("fn removed").
        qoff = _transcript(dsn, [GENERIC, "SET pgcolumnar.enable_qual_pushdown = off",
                                 "PREPARE q(int) AS SELECT count(*) FROM t WHERE ts >= same($1)",
                                 "EXPLAIN (ANALYZE, TIMING off, SUMMARY off) EXECUTE q(39000)"])
        r["qual pushdown off removed"] = _field(qoff, "Chunk Groups Removed by Filter")
        # ts >= 1 excludes no group, so only the second key removes the other 19.
        two = _transcript(dsn, [GENERIC, "PREPARE k(int, int) AS SELECT count(*) FROM t "
                                "WHERE ts >= same($1) AND ts <= same($2)",
                                "EXPLAIN (ANALYZE, TIMING off, SUMMARY off) EXECUTE k(1, 2000)"])
        r["two keys removed"] = _field(two, "Chunk Groups Removed by Filter")
        _session(dsn, ["SET pgcolumnar.stripe_row_limit = 2000",
                       "CREATE TABLE tx (s text) USING pgcolumnar",
                       "INSERT INTO tx SELECT lpad(g::text, 6, '0') FROM generate_series(1, 40000) g",
                       SAME_T])
        # 1655 rows of tx are >= '038345' followed by 'x's.
        rescan = _transcript(dsn, [GENERIC, TEXT_RESCAN, "EXECUTE r('038345' || repeat('x', 300000))"])
        r["text rescan"] = rescan if isinstance(rescan, str) else " ".join(rescan)
        r["survived"] = _survived(dsn, pgc_cluster, before)
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_param_pushdown_a_generic_plan_parameterized_qual_prunes(pp, expect):
    expect.text(pp["removed"], "19",
                "a generic-plan parameterized qual prunes chunk groups (19 of 20 removed)")


def test_param_pushdown_the_parameterized_count_matches_the_literal(pp, expect):
    expect.text(pp["prep count"], "1001", "parameterized count matches the literal count")


def test_param_pushdown_the_literal_count_is_the_expected_value(pp, expect):
    expect.text(pp["literal count"], "1001", "literal count is the expected value")


def test_param_pushdown_the_correlated_arm_rescans_the_columnar_scan(pp, expect):
    expect.text(pp["corr rescans"], "1", "the correlated arm rescans the columnar scan once per outer row")


def test_param_pushdown_a_correlated_param_exec_builds_no_skip_predicate(pp, expect):
    expect.text(pp["corr skip preds"], "0", "a correlated PARAM_EXEC builds no skip predicate")


def test_param_pushdown_a_correlated_param_exec_is_not_mis_pruned(pp, expect):
    expect.text(pp["corr ge"], "3002", "a correlated PARAM_EXEC is not mis-pruned (results stay correct)")


def test_param_pushdown_a_correlated_upper_bound_is_not_mis_pruned(pp, expect):
    expect.text(pp["corr le"], "77000", "a correlated PARAM_EXEC upper bound is not mis-pruned either")


def test_param_pushdown_a_second_execution_counts_with_its_own_value(pp, expect):
    expect.text(pp["twice count"], "2001", "a second execution in the same session counts with its own value")


def test_param_pushdown_a_second_execution_prunes_with_its_own_value(pp, expect):
    expect.text(pp["twice removed"], "18", "and prunes with its own value too (18 of 20 removed)")


def test_param_pushdown_a_statement_timeout_at_scan_start_cancels(pp, expect):
    expect.text(pp["timeout"], "1",
                "a statement_timeout during scan start-up cancels the columnar query (#2065)")


def test_param_pushdown_a_stable_operand_that_raises_once_reaches_the_client(pp, expect):
    expect.text(pp["fail once"], "1",
                "a stable operand that raises once reaches the client, as on heap (#2065)")


def test_param_pushdown_no_failed_query_leaves_a_warning(pp, expect):
    expect.text(pp["warnings"], "0",
                "neither failed query leaves a resource or SPI-stack WARNING (#2065)")


def test_param_pushdown_a_plain_explain_does_not_call_a_stable_operand(pp, expect):
    expect.text(pp["explain calls"], "0", "a plain EXPLAIN does not call a stable operand (#2065)")


def test_param_pushdown_a_plain_explain_execute_still_pushes_down_a_bound_param(pp, expect):
    expect.text(pp["explain bound"], "1",
                "a plain EXPLAIN EXECUTE still pushes down a bound parameter (#2065)")


def test_param_pushdown_a_scan_that_limit_cuts_off_calls_no_stable_operand(pp, expect):
    expect.text(pp["limit cut"], "1",
                "a columnar scan that LIMIT cuts off never calls its stable operand (#2065)")


def test_param_pushdown_a_scan_under_a_false_one_time_filter_calls_no_stable_operand(pp, expect):
    expect.text(pp["one-time off"], "0",
                "a columnar scan under a false one-time filter never calls its stable operand (#2065)")


def test_param_pushdown_a_stable_function_operand_still_prunes_when_the_scan_runs(pp, expect):
    expect.text(pp["fn removed"], "19",
                "a stable function operand still prunes when the scan runs (19 of 20 removed) (#2065)")


def test_param_pushdown_a_stable_operand_is_called_once_to_freeze_then_once_per_row(pp, expect):
    expect.text(pp["once calls"], pp["once want"],
                "the scan calls a stable operand once to freeze it, then once per row the Filter tests (#2065)")


def test_param_pushdown_a_plpgsql_record_field_under_a_false_one_time_filter_is_not_fetched(pp, expect):
    expect.text(pp["plpgsql one-time off"], "1 0",
                "a PL/pgSQL record field under a false one-time filter is not fetched at scan set-up (#2065)")


def test_param_pushdown_auto_explain_logs_the_plan_a_plpgsql_function_ran(pp, expect):
    expect.text(pp["plpgsql premise"], "1",
                "auto_explain logs the plan a PL/pgSQL function ran, with its parameter (premise) (#2065)")


def test_param_pushdown_a_plpgsql_variable_on_a_generic_plan_prunes_when_the_scan_runs(pp, expect):
    expect.text(pp["plpgsql removed"], "19",
                "a PL/pgSQL variable on a generic plan prunes when the scan runs (19 of 20 removed) (#2065)")


def test_param_pushdown_the_projection_arm_uses_the_projection(pp, expect):
    expect.text(pp["proj premise"], "1", "the projection arm's scan uses the projection (premise) (#2065)")


def test_param_pushdown_a_projection_scan_with_a_first_run_key_counts_what_heap_counts(pp, expect):
    expect.text(pp["proj answers"], "801|400500 11|219945",
                "a projection scan with a key frozen at the first run counts what heap counts (#2065)")


def test_param_pushdown_a_first_run_key_obeys_qual_pushdown_off(pp, expect):
    expect.text(pp["qual pushdown off removed"], "0",
                "with pgcolumnar.enable_qual_pushdown off, a key frozen at the first run prunes nothing (#2065)")


def test_param_pushdown_two_first_run_keys_both_prune(pp, expect):
    expect.text(pp["two keys removed"], "19",
                "two keys frozen at the first run both prune (19 of 20 removed) (#2065)")


def test_param_pushdown_a_long_text_first_run_key_survives_each_rescan(pp, expect):
    expect.text(pp["text rescan"], "1|1655 2|1655 3|1655",
                "a long text key frozen at the first run survives each rescan (#2065)")


def test_param_pushdown_the_backend_is_alive(pp, expect):
    expect.text(pp["survived"], "1", "backend alive")
