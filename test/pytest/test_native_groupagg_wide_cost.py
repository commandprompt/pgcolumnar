"""The grouped vector aggregate's input scan, priced on a wide table. The pytest twin
of `test/native_groupagg_wide_cost.sh`, every check by the shell's name (#1341).

The grouped fold node prices the scan it performs from the cheapest serial scan path
that survived add_path. When none did -- a Gather over the partial path dominated it
-- it prices the scan itself, and must do so the way the columnar scan node does
(projected-width I/O, per-column decode CPU, zone-map survival), not with the bare
seqscan formula that under-priced it on a wide table (#12).

With 40,000 rows a serial scan survives, so the fold and result readings never reach
that fallback. Free parallelism lets a Gather displace the serial scan; with hash and
sort off the parallel finalize cannot win, so the fold node is the plan and its cost
can be read. It must equal the cost a surviving serial scan gives.

Each reading's settings ride in the connection's options, as the shell's PGOPTIONS
do, and each reading has a connection of its own, as the shell's psql calls do. ON A
PRIVATE DATABASE, built once by a module fixture that takes every reading in the
shell's order; each test asserts one, so one red cannot stop the next (#1339). A
failed query is a `query_error()`, refused on either side by name.
"""
import re

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_groupagg_wide_cost"
COLS = 25
Q = ("SELECT string_agg(g||':'||s0||':'||s24||':'||c, '|' ORDER BY g) "
     "FROM (SELECT g, sum(c0) s0, sum(c24) s24, count(*) c FROM wa GROUP BY g) r")
EXPLAINQ = "SELECT g, sum(c0) FROM wa GROUP BY g"
REF = "-c pgcolumnar.enable_group_vectorization=off -c pgcolumnar.enable_ungrouped_vector_agg=off"
FOLD = ("-c pgcolumnar.enable_ungrouped_vector_agg=on -c pgcolumnar.enable_parallel_vector_agg=on "
        "-c max_parallel_workers_per_gather=4")
FREE = ("-c parallel_setup_cost=0 -c parallel_tuple_cost=0 -c min_parallel_table_scan_size=0 "
        "-c max_parallel_workers_per_gather=4")
FOLD_LINE = "Columnar Batch Fold: yes"
COST = re.compile(r"^Custom Scan \(PgColumnarScan\)\s+\(cost=[0-9.]+\.\.([0-9.]+) ")


def _failed(exc):
    return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _rows(dsn, sql, options=""):
    """-> every row's first column as text, on a connection of its own, or the
    failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True, options=options) as conn:
            return ["" if r[0] is None else str(r[0]) for r in conn.execute(sql)]
    except psycopg.Error as exc:
        return _failed(exc)


def _value(dsn, sql, options=""):
    rows = _rows(dsn, sql, options)
    return rows if isinstance(rows, str) else (rows[0] if rows else "")


def _fold_cost(dsn, options):
    """-> the fold node's total cost, "" when the plan is not the fold node, or the
    failure as a query_error()."""
    plan = _rows(dsn, f"EXPLAIN {EXPLAINQ}", options)
    if isinstance(plan, str):
        return plan
    m = COST.match(plan[0]) if plan else None
    return m.group(1) if m and any(FOLD_LINE in line for line in plan) else ""


@pytest.fixture(scope="module")
def wide(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        cols = ", ".join(f"c{i} bigint" for i in range(COLS))
        vals = ", ".join(f"g*{i + 1}" for i in range(COLS))
        with psycopg.connect(dsn, autocommit=True) as c:
            for sql in ("CREATE EXTENSION pgcolumnar",
                        f"CREATE TABLE wa (g int, {cols}) USING pgcolumnar",
                        f"INSERT INTO wa SELECT g%16, {vals} FROM generate_series(1, 40000) g",
                        "ANALYZE wa"):
                try:
                    c.execute(sql)
                except psycopg.Error as exc:
                    print(f"-- step failed: {str(exc).splitlines()[0] if str(exc) else exc!r}")
        r = {"ref": _value(dsn, Q, REF), "got": _value(dsn, Q, FOLD)}
        plan = _rows(dsn, f"EXPLAIN (COSTS OFF, VERBOSE) {EXPLAINQ}", FOLD)
        r["fold markers"] = plan if isinstance(plan, str) else sum(FOLD_LINE in line for line in plan)
        gather = _rows(dsn, f"EXPLAIN (COSTS OFF) {EXPLAINQ}", FREE)
        r["gather"] = gather if isinstance(gather, str) else (
            "yes" if any("Gather" in line for line in gather) else "")
        r["fallback cost"] = _fold_cost(dsn, f"{FREE} -c enable_hashagg=off -c enable_sort=off")
        r["serial cost"] = _fold_cost(dsn, "")
        r["alive"] = _value(dsn, "SELECT 1")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_the_fold_path_is_used_on_the_wide_grouped_aggregate(wide, expect):
    m = wide["fold markers"]
    expect.text(m if isinstance(m, str) else ("yes" if m > 0 else ""), "yes",
                "the fold path is used on the wide grouped aggregate")


def test_the_fold_result_matches_a_plain_aggregate_on_a_wide_table(wide, expect):
    expect.text(wide["got"], wide["ref"], "the fold result matches a plain aggregate on a wide table")


def test_with_parallelism_free_a_gather_displaces_the_serial_scan(wide, expect):
    expect.text(wide["gather"], "yes", "premise: with parallelism free, a Gather displaces the serial scan")


def test_the_fallback_prices_the_fold_as_a_surviving_serial_scan_does(wide, expect):
    expect.text(wide["fallback cost"], wide["serial cost"],
                "the fallback prices the fold as a surviving serial scan does")


def test_backend_alive(wide, expect):
    expect.text(wide["alive"], "1", "backend alive")
