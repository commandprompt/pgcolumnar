"""nbtree's deletion passes must never destroy an index entry for a live row. The pytest
twin of `test/index_delete_liveness.sh`, every check by the shell's name (#1341).

Version churn on a non-indexed column sets nbtree's indexUnchanged hint and triggers
the bottom-up deletion pass, which asks the table "is this entry dead to everyone".
After twelve rounds of such churn over 400 live rows, rolled back (arm A) or committed
(arm D), an index scan must still reach all 400. Churn on the indexed column (control
B) runs no bottom-up pass, and a heap table under the same hint (control C) is the
oracle; at shipped defaults count(*) and count(b) must agree.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading (#1339). Every churn reports the rows it updated, and each arm's
reading carries that count, so a churn that never ran cannot pass as a churn that lost
nothing.
"""
import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_index_delete_liveness"
N, ROUNDS = 400, 12
WANT = N * ROUNDS
TRUTH = ("SET enable_indexscan = off", "SET enable_bitmapscan = off", "SET enable_indexonlyscan = off")
VIA = ("SET enable_seqscan = off", "SET enable_indexonlyscan = off", "SET pgcolumnar.enable_custom_scan = off")


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _value(dsn, sql, sets=()):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for s in sets:
                conn.execute(s)
            rows = conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return query_error(f"no row from {sql}") if not rows else rows


def _scalar(dsn, sql, sets=()):
    rows = _value(dsn, sql, sets)
    return rows[0][0] if isinstance(rows, list) else rows


def _mk(dsn, t, using, second=False):
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(f"CREATE TABLE {t}(a int, b text) {using}")
        c.execute(f"CREATE INDEX {t}i ON {t}(a)")
        if second:
            c.execute(f"CREATE INDEX {t}i2 ON {t}(b)")
        c.execute(f"INSERT INTO {t} SELECT 1, repeat('x',20) FROM generate_series(1,{N})")


def _churn(dsn, t, set_clause, end):
    """-> the rows twelve rounds of UPDATE touched, inside a transaction ended by END."""
    try:
        with psycopg.connect(dsn) as conn:
            n = 0
            for _ in range(ROUNDS):
                n += conn.execute(f"UPDATE {t} SET {set_clause}").rowcount
            conn.commit() if end == "COMMIT" else conn.rollback()
            return n
    except psycopg.Error as exc:
        return query_error(_message(exc))


def _arm(dsn, t):
    return (_scalar(dsn, f"SELECT count(*) FROM {t} WHERE a = 1", TRUTH),
            _scalar(dsn, f"SELECT count(b) FROM {t} WHERE a = 1", VIA))


@pytest.fixture(scope="module")
def dl(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r = {}
        _mk(dsn, "ca", "USING pgcolumnar")
        r["a before"] = _scalar(dsn, "SELECT count(*) FROM ca WHERE a = 1", TRUTH)
        r["a churn"] = _churn(dsn, "ca", "b = b || 'y'", "ROLLBACK")
        plan = _value(dsn, "EXPLAIN (COSTS OFF) SELECT count(b) FROM ca WHERE a = 1", VIA)
        r["a plan"] = " ".join(x[0] for x in plan) if isinstance(plan, list) else str(plan)
        r["a"] = _arm(dsn, "ca")

        _mk(dsn, "cb", "USING pgcolumnar")
        r["b churn"] = _churn(dsn, "cb", "a = a", "ROLLBACK")
        r["b"] = _arm(dsn, "cb")

        _mk(dsn, "hb", "", second=True)
        r["c churn"] = _churn(dsn, "hb", "b = b || 'y'", "ROLLBACK")
        r["c"] = _arm(dsn, "hb")

        _mk(dsn, "cc", "USING pgcolumnar")
        r["d churn"] = _churn(dsn, "cc", "b = b || 'y'", "COMMIT")
        r["d"] = _arm(dsn, "cc")
        r["def star"] = _scalar(dsn, "SELECT count(*) FROM cc WHERE a = 1")
        r["def b"] = _scalar(dsn, "SELECT count(b) FROM cc WHERE a = 1")

        _mk(dsn, "hc", "", second=True)
        r["h churn"] = _churn(dsn, "hc", "b = b || 'y'", "COMMIT")
        r["h"] = _scalar(dsn, "SELECT count(*) FROM hc WHERE a = 1")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _carried(churned, value):
    return f"churned {churned}: {value}"


def test_arm_a_holds_n_rows_before_the_churn(dl, expect):
    expect.text(str(dl["a before"]), str(N), "premise: arm A holds 400 rows before the churn")


def test_arm_a_really_plans_an_index_scan(dl, expect):
    expect.text("yes" if "Index Scan" in dl["a plan"] else f"no: {dl['a plan']}", "yes",
                "premise: arm A really plans an Index Scan, so the arm measures the index")


def test_arm_a_a_rollback_leaves_every_live_row_reachable(dl, expect):
    t, i = dl["a"]
    expect.text(_carried(dl["a churn"], i), _carried(WANT, t),
                "arm A: a ROLLBACK leaves every live row reachable through the index")


def test_control_b_indexed_column_churn_loses_nothing(dl, expect):
    t, i = dl["b"]
    expect.text(_carried(dl["b churn"], i), _carried(WANT, t),
                "control B: indexed-column churn triggers no bottom-up pass and loses nothing")


def test_control_c_the_heap_loses_nothing(dl, expect):
    t, i = dl["c"]
    expect.text(_carried(dl["c churn"], i), _carried(WANT, t),
                "control C: the heap loses nothing under the same churn")


def test_arm_d_a_commit_leaves_every_live_row_reachable(dl, expect):
    t, i = dl["d"]
    expect.text(_carried(dl["d churn"], i), _carried(WANT, t),
                "arm D: a COMMIT leaves every live row reachable through the index")


def test_at_shipped_defaults_count_star_is_right(dl, expect):
    expect.text(str(dl["def star"]), str(dl["d"][0]), "at shipped defaults count(*) is right")


def test_at_shipped_defaults_count_star_and_count_b_agree(dl, expect):
    expect.text(str(dl["def star"]), str(dl["def b"]),
                "at shipped defaults the table answers the same to count(*) and count(b)")


def test_oracle_the_heap_answers_count_star_correctly(dl, expect):
    expect.text(_carried(dl["h churn"], dl["h"]), _carried(WANT, N),
                "oracle: the heap answers count(*) correctly after the same churn")
