"""The cost model describes the geometry a table WAS WRITTEN WITH, not the one a write in
the planning session would produce (#806). The pytest twin of
`test/cost_written_geometry.sh`, every check by the shell's name (#1341).

Two tables of 400,000 rows: gb written at the default stripe_row_limit, ga at 20,000 in
the writing session. Varying only the planning session's stripe_row_limit must not
reprice gb's index fetch; ga, really laid out in smaller groups, must still cost less;
and a never-written table must still plan.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading (#1339). Every plan is read with max_parallel_workers_per_gather = 0
in the session that sets the plan-time GUC. A cost that cannot be read is a failed
reading, so two missing costs cannot agree, and the row counts are compared against the
constant rather than with each other.
"""
import re

import psycopg
import pytest

from pgc_vacuity import QUERY_ERROR, query_error

DB = "pgc_cost_written_geometry"
ROWS = 400000


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _failed(v):
    return str(v).startswith(QUERY_ERROR)


def _run(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.encode())
    except psycopg.Error as exc:
        return _message(exc)
    return ""


def _value(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return query_error(f"no value from {sql}") if row is None or row[0] is None else row[0]


def _plan_line(dsn, table, limit):
    """-> the first costed plan line for an index-driven fetch at a plan-time limit."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f"SET pgcolumnar.stripe_row_limit = {limit}")
            conn.execute("SET enable_seqscan = off")
            conn.execute("SET max_parallel_workers_per_gather = 0")
            lines = [r[0] for r in conn.execute(f"EXPLAIN SELECT id, v FROM {table} WHERE id <= 100")]
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return next((l for l in lines if "cost=" in l), query_error(f"no costed line for {table}"))


def _cost(dsn, table, limit):
    line = _plan_line(dsn, table, limit)
    if _failed(line):
        return line
    m = re.search(r"cost=[0-9.]+\.\.([0-9.]+)", line)
    return float(m.group(1)) if m else query_error(f"no total cost in {line}")


def _groups(dsn, table):
    return _value(dsn, "SELECT count(*) FROM pgcolumnar.row_group r JOIN pgcolumnar.storage s "
                       f"USING (storage_id) WHERE s.relation_oid = '{table}'::regclass")


def _written_limit(dsn, table):
    return _value(dsn, f"SELECT row_group_limit FROM pgcolumnar.storage WHERE relation_oid = '{table}'::regclass")


@pytest.fixture(scope="module")
def cg(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        _run(dsn, "CREATE TABLE gb (id int, v int, t text) USING pgcolumnar; "
                  f"INSERT INTO gb SELECT g, g, 'r' || g FROM generate_series(1, {ROWS}) g; "
                  "CREATE INDEX gb_id ON gb (id);")
        _run(dsn, "SET pgcolumnar.stripe_row_limit = 20000; SET pgcolumnar.chunk_group_row_limit = 20000; "
                  "CREATE TABLE ga (id int, v int, t text) USING pgcolumnar; "
                  f"INSERT INTO ga SELECT g, g, 'r' || g FROM generate_series(1, {ROWS}) g; "
                  "CREATE INDEX ga_id ON ga (id);")
        _run(dsn, "ANALYZE ga; ANALYZE gb;")
        r = {"rows": f"{_value(dsn, 'SELECT count(*) FROM ga')}|{_value(dsn, 'SELECT count(*) FROM gb')}",
             "gb groups": _groups(dsn, "gb"), "ga groups": _groups(dsn, "ga")}
        default = _value(dsn, "SHOW pgcolumnar.stripe_row_limit")
        default = default if str(default).isdigit() else f"unread [{default}]"
        r["limits"] = f"{_written_limit(dsn, 'ga')}|{_written_limit(dsn, 'gb')}"
        r["limits want"] = f"20000|{default}"
        for g in (150000, 20000):
            line = _plan_line(dsn, "gb", g)
            r[f"index {g}"] = query_error(f"no plan: {line}") if _failed(line) else line.count("Index Scan")
        r["c1"], r["c2"] = _cost(dsn, "gb", 150000), _cost(dsn, "gb", 20000)
        r["ca"], r["cb"] = _cost(dsn, "ga", 150000), _cost(dsn, "gb", 150000)
        _run(dsn, "CREATE TABLE gempty (id int, v int) USING pgcolumnar; CREATE INDEX gempty_id ON gempty (id);")
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute("SET enable_seqscan = off")
                plan = [x[0] for x in conn.execute("EXPLAIN SELECT id, v FROM gempty WHERE id <= 100")]
            r["empty plan"] = "yes" if any("cost=" in l for l in plan) else "no"
        except psycopg.Error as exc:
            r["empty plan"] = query_error(_message(exc))
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _both_costs(a, b):
    return isinstance(a, float) and isinstance(b, float)


def test_both_tables_hold_the_same_number_of_rows(cg, expect):
    expect.text(cg["rows"], f"{ROWS}|{ROWS}", "premise: both tables hold the same number of rows")


def test_gb_was_written_at_the_default_limit_in_few_groups(cg, expect):
    n = cg["gb groups"]
    expect.text(("few" if n <= 5 else str(n)) if isinstance(n, int) else n, "few",
                "gb was written at the default limit, in few groups")


def test_ga_was_written_at_20000_in_many_more_groups(cg, expect):
    n = cg["ga groups"]
    expect.text(("many" if n >= 15 else str(n)) if isinstance(n, int) else n, "many",
                "ga was written at 20000, in many more groups")


def test_the_catalog_recorded_both_limits(cg, expect):
    expect.text(cg["limits"], cg["limits want"], "and the catalog recorded both limits")


def test_gb_is_costed_as_an_index_scan_at_plan_time_limit_150000(cg, expect):
    expect.text(str(cg["index 150000"]), "1", "premise: gb is costed as an index scan at plan-time limit 150000")


def test_gb_is_costed_as_an_index_scan_at_plan_time_limit_20000(cg, expect):
    expect.text(str(cg["index 20000"]), "1", "premise: gb is costed as an index scan at plan-time limit 20000")


def test_the_planning_sessions_limit_does_not_reprice_a_written_table(cg, expect):
    c1, c2 = cg["c1"], cg["c2"]
    got = f"{c1}|{c2}" if _both_costs(c1, c2) else query_error(f"unread [{c1}|{c2}]")
    expect.text(got, f"{c1}|{c1}", "the planning session's stripe_row_limit does not reprice a written table")


def test_a_table_written_in_smaller_groups_is_costed_cheaper(cg, expect):
    ca, cb = cg["ca"], cg["cb"]
    got = ("cheaper" if ca < 0.99 * cb else f"not cheaper: ga {ca} gb {cb}") if _both_costs(ca, cb) else query_error(f"unread [{ca}|{cb}]")
    expect.text(got, "cheaper", "a table written in smaller groups is costed cheaper than one in larger")


def test_the_index_fetch_penalty_sees_the_written_geometry(cg, expect):
    ca, cb = cg["ca"], cg["cb"]
    if not _both_costs(ca, cb) or ca == 0 or cb == 0:
        got = query_error(f"no ratio from [{ca}|{cb}]")
    else:
        got = "under" if round(ca / cb, 2) <= 0.33 else f"{ca / cb:.2f}x over the 0.33x bound"
    expect.text(got, "under", "the index-fetch penalty sees the written geometry: ga costs under a third of gb")


def test_a_never_written_table_still_produces_a_plan(cg, expect):
    expect.text(cg["empty plan"], "yes", "a never-written table still produces a plan")
