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


def test_param_pushdown_the_backend_is_alive(pp, expect):
    expect.text(pp["survived"], "1", "backend alive")
