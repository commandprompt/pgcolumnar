"""A functional-dependency row estimate is not collapsed by a capacity-truncated
extended MCV (#438). The pytest twin of `test/dependency_estimate.sh`, every check by
the shell's name (#1341).

Core applies a partial extended MCV before the functional dependency, which -- when the
MCV cannot hold every group -- displaces the perfect dependency and clamps the joint
estimate to 1 against an actual 300. PgColumnarSetRelPathlist recomputes a
dependency-consistent estimate and raises rel->rows to it when core's is lower. This
pins that, that it does not distort a case core already gets right, and that a table
without a dependency statistic is untouched.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading, so one red cannot stop the next (#1339). An estimate is the top
plan node's `rows=`, or a `query_error()` when EXPLAIN fails or prints none: a missing
estimate is never read as 0, which would pass "bounded" and "untouched".
"""
import re

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_dependency_estimate"
ROWS = re.compile(r"rows=([0-9]+)")
PROBE = "SELECT * FROM {t} WHERE a=5 AND b=10"
COUNT = "SELECT count(*) FROM {t} WHERE a=5 AND b=10"
# The first pair the extended MCV does not hold: the clamp happens only for such a pair,
# and the MCV's 100 of 1000 pairs are ANALYZE's random sample, so a fixed a=5 was in it
# in 1 run of 6 and the arm then passed with the correction off.
OUTSIDE_MCV = ("SELECT v FROM generate_series(0, 999) v WHERE ARRAY[v::text, (v*2)::text] NOT IN ("
               "SELECT m.values FROM pg_statistic_ext s JOIN pg_statistic_ext_data d ON d.stxoid = s.oid, "
               "pg_mcv_list_items(d.stxdmcv) m WHERE s.stxname = 'dep_full_s') ORDER BY v LIMIT 1")


def _failed(exc):
    return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _count(dsn, sql):
    """-> the first column of the first row as an int, or the failure (or no row) as
    a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return _failed(exc)
    return int(row[0]) if row and row[0] is not None else query_error(f"no row from {sql}")


def _est(dsn, sql):
    """-> the top node's estimated row count as an int, or a query_error() when EXPLAIN
    fails or its first plan line carries no estimate."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            plan = [r[0] for r in conn.execute(f"EXPLAIN (COSTS ON) {sql}")]
    except psycopg.Error as exc:
        return _failed(exc)
    m = next((m for m in map(ROWS.search, plan) if m), None)
    return int(m.group(1)) if m else query_error(f"no rows= in the plan of {sql}")


@pytest.fixture(scope="module")
def dep(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            # the #438 case: 1000 near-uniform values, b = a*2, full extended stats
            c.execute("CREATE TABLE dep_full (a int, b int) USING pgcolumnar")
            c.execute("INSERT INTO dep_full SELECT g%1000, (g%1000)*2 FROM generate_series(1,300000) g")
            c.execute("CREATE STATISTICS dep_full_s (dependencies, ndistinct, mcv) ON a, b FROM dep_full")
            c.execute("ANALYZE dep_full")
            # a table core already estimates correctly (10 values, each an MCV)
            c.execute("CREATE TABLE dep_ok (a int, b int) USING pgcolumnar")
            c.execute("INSERT INTO dep_ok SELECT g%10, (g%10)*2 FROM generate_series(1,300000) g")
            c.execute("CREATE STATISTICS dep_ok_s (dependencies, ndistinct, mcv) ON a, b FROM dep_ok")
            c.execute("ANALYZE dep_ok")
            # a table with NO extended statistic: the correction must not fire
            c.execute("CREATE TABLE dep_none (a int, b int) USING pgcolumnar")
            c.execute("INSERT INTO dep_none SELECT g%1000, (g%1000)*2 FROM generate_series(1,300000) g")
            c.execute("ANALYZE dep_none")
        probe = _count(dsn, OUTSIDE_MCV)
        p = probe if isinstance(probe, int) else 0
        full = f"SELECT {{}} FROM dep_full WHERE a={p} AND b={p * 2}"
        yield {
            "probe": probe,
            "full actual": _count(dsn, full.format("count(*)")),
            "ok actual": _count(dsn, COUNT.format(t="dep_ok")),
            "full est": _est(dsn, full.format("*")),
            "ok est": _est(dsn, PROBE.format(t="dep_ok")),
            "none est": _est(dsn, PROBE.format(t="dep_none")),
        }
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _reading(*values):
    """-> the first value that is not an int (a failed reading), or None."""
    return next((v for v in values if not isinstance(v, int)), None)


def test_the_438_probe_is_outside_the_truncated_mcv(dep, expect):
    expect.text(_reading(dep["probe"]) or "yes", "yes",
                "premise: the #438 probe is a pair the truncated extended MCV does not hold")


def test_the_438_case_really_matches_about_300_rows(dep, expect):
    expect.num(dep["full actual"], 300, "premise: the #438 case really matches ~300 rows")


def test_the_dependency_estimate_is_not_collapsed(dep, expect):
    e = dep["full est"]
    expect.text(_reading(e) or ("raised" if e >= 100 else f"clamped({e})"), "raised",
                "the functional-dependency estimate is not collapsed to the clamp floor")


def test_and_it_is_not_an_over_correction(dep, expect):
    e, a = dep["full est"], dep["full actual"]
    expect.text(_reading(e, a) or ("bounded" if e <= a * 2 else f"over({e})"), "bounded",
                "and it is not an over-correction (bounded by the most selective marginal)")


def test_a_case_core_already_estimates_well_is_unchanged(dep, expect):
    e, a = dep["ok est"], dep["ok actual"]
    expect.text(_reading(e, a) or ("same" if a * 0.8 <= e <= a * 1.2 else f"moved({e})"), "same",
                "a case core already estimates well is unchanged (within 20%)")


def test_a_table_with_no_dependency_statistic_is_untouched(dep, expect):
    e = dep["none est"]
    expect.text(_reading(e) or ("untouched" if e <= 10 else f"raised({e})"), "untouched",
                "a table with no dependency statistic is left to core's estimate (not raised)")
