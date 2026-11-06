"""EXPLAIN ANALYZE must not report "Columnar Batch Fold: yes" for an execution that
fell back to the row path (#602). The pytest twin of `test/batch_fold_explain.sh`,
every check by the shell's name (#1341).

The ungrouped vectorized aggregate decides fold eligibility from the query shape at
Begin, so plain EXPLAIN can report what will be attempted, and records during
execution whether the fold actually ran. A column added after some row groups exist
is predicted-eligible, but the first old group lacks it, the fold refuses before
claiming any group, and the whole scan runs the row path. ANALYZE must report that,
plain EXPLAIN keeps the prediction, and the parallel partial fold, which cannot take
that fallback, refuses the relation with an error.

ON A PRIVATE DATABASE, built once by a module fixture that takes every reading in the
shell's order; each test asserts one, so one red cannot stop the next (#1339). Each
reading has a connection of its own with the suite's settings in its options, as the
shell's psql calls set them. A failed query is a `query_error()`; a plan with no
"Columnar Batch Fold" line reads "", which `expect.text` refuses.
"""
import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_batch_fold_explain"
GUC = "-c pgcolumnar.enable_ungrouped_vector_agg=on"
PGUC = (GUC + " -c pgcolumnar.enable_parallel_vector_agg=on -c max_parallel_workers_per_gather=2"
        " -c parallel_setup_cost=0 -c parallel_tuple_cost=0 -c min_parallel_table_scan_size=0")
Q_OLD = "SELECT sum(v) FROM bfe_old WHERE q <= 50"
Q_CTRL = "SELECT sum(v) FROM bfe_ctrl WHERE q <= 50"
ANALYZE_PFX = "EXPLAIN (ANALYZE, TIMING OFF, COSTS OFF, SUMMARY OFF)"
PLAIN_PFX = "EXPLAIN (COSTS OFF)"
REFUSAL = "cannot fold a relation with a column added"


def _failed(exc):
    return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _rows(dsn, sql, options=GUC):
    """-> every row's first column as text, on a connection of its own, or the
    failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True, options=options) as conn:
            return ["" if r[0] is None else str(r[0]) for r in conn.execute(sql)]
    except psycopg.Error as exc:
        return _failed(exc)


def _num(dsn, sql):
    rows = _rows(dsn, sql)
    if isinstance(rows, str):
        return rows
    try:
        return int(rows[0]) if rows else query_error(f"no row from {sql}")
    except ValueError:
        return query_error(f"not a number from {sql}: {rows[0]!r}")


def _fold_line(dsn, prefix, query):
    """-> "yes" or "no" from the plan's "Columnar Batch Fold" line, "" when the plan has
    no such line (the custom aggregate node was not planned), or the failure."""
    plan = _rows(dsn, f"{prefix} {query}")
    if isinstance(plan, str):
        return plan
    for line in plan:
        if "Columnar Batch Fold" in line:
            return "yes" if "yes" in line else "no" if "no" in line else ""
    return ""


@pytest.fixture(scope="module")
def bfe(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            # bfe_old: 50k rows written BEFORE v exists, 10k after, so its old groups
            # have no chunk for v. bfe_ctrl: the same 10k rows, v present from the start.
            c.execute("CREATE TABLE bfe_old(q int) USING pgcolumnar")
            c.execute("INSERT INTO bfe_old SELECT 1 + g % 100 FROM generate_series(1,50000) g")
            c.execute("ALTER TABLE bfe_old ADD COLUMN v int")
            c.execute("INSERT INTO bfe_old SELECT 1 + g % 100, g FROM generate_series(1,10000) g")
            c.execute("CREATE TABLE bfe_ctrl(q int, v int) USING pgcolumnar")
            c.execute("INSERT INTO bfe_ctrl SELECT 1 + g % 100, g FROM generate_series(1,10000) g")
        r = {
            "count": _num(dsn, "SELECT count(*) FROM bfe_old"),
            # the answers, against an oracle that touches neither table
            "want": _num(dsn, "SELECT sum(g) FROM generate_series(1,10000) g WHERE 1 + g % 100 <= 50"),
            "old sum": _num(dsn, Q_OLD),
            "ctrl sum": _num(dsn, Q_CTRL),
            "ctrl analyze": _fold_line(dsn, ANALYZE_PFX, Q_CTRL),
            "old plain": _fold_line(dsn, PLAIN_PFX, Q_OLD),
            "old analyze": _fold_line(dsn, ANALYZE_PFX, Q_OLD),
        }
        plan = _rows(dsn, f"{PLAIN_PFX} {Q_OLD}", PGUC)
        r["par planned"] = (plan if isinstance(plan, str) else
                            "yes" if any("Gather" in l for l in plan)
                            and any("Columnar Batch Fold" in l for l in plan) else "no")
        if r["par planned"] == "yes":
            try:
                with psycopg.connect(dsn, autocommit=True, options=PGUC) as conn:
                    conn.execute(Q_OLD).fetchall()
                r["par hits"] = 0
            except psycopg.Error as exc:
                r["par hits"] = str(exc).count(REFUSAL)
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_the_fixture_loaded_every_row(bfe, expect):
    expect.num(bfe["count"], 60000, "premise: fixture loaded every row")


def test_the_fallback_table_returns_the_right_sum(bfe, expect):
    expect.num(bfe["old sum"], bfe["want"], "premise: fallback table returns the right sum")


def test_the_control_table_returns_the_right_sum(bfe, expect):
    expect.num(bfe["ctrl sum"], bfe["want"], "premise: control table returns the right sum")


def test_analyze_reports_the_fold_that_ran(bfe, expect):
    expect.text(bfe["ctrl analyze"], "yes", "control: ANALYZE reports the fold that ran")


def test_plain_explain_still_reports_the_prediction(bfe, expect):
    expect.text(bfe["old plain"], "yes", "fallback table: plain EXPLAIN still reports the prediction")


def test_analyze_reports_the_row_path_that_ran(bfe, expect):
    expect.text(bfe["old analyze"], "no", "fallback table: ANALYZE reports the row path that actually ran")


def test_the_planner_gives_the_parallel_partial_fold(bfe, expect):
    expect.text(bfe["par planned"], "yes",
                "premise: with parallelism free, the planner gives the parallel partial fold")


def test_the_parallel_partial_fold_refuses_the_fallback(bfe, expect):
    if bfe["par planned"] != "yes":
        expect.text(bfe["par planned"], "yes", "parallel partial fold refuses the fallback relation")
    expect.num(bfe["par hits"], 1, "parallel partial fold refuses the fallback relation")
