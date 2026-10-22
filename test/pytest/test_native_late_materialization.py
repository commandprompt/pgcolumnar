"""Late materialization (#452 phase 1a): a row the qual rejects does not have its payload
columns built. The pytest twin of `test/native_late_materialization.sh`, every check by
the shell's name (#1341).

A leading-wildcard LIKE builds no scan key and no min/max can prune it, so nothing is
pruned and what is measured is the per-row path alone. One row in 1024 carries the
needle, so every vector keeps a survivor and phase 2's vector skipping rules none out.
"Columnar Rows Filtered Before Materialization" counts the rows the qual rejected on
this path; "Rows Removed by Filter" must keep counting them too. The heap mirror is
built from the generator and is the oracle for every answer, with the feature on, off,
and refused for a volatile qual.

EVERY READING FAILS CLOSED. A counter that could not be read is a query_error() or a
"no reading" string, never 0: four checks want exactly 0, and the shell's version of
them passed with the server stopped. NOT MEASURABLE HERE, as the shell says: whether a
rejected row's payload is actually left unbuilt (no counter reports per-row
materialization, and the answers are the same by design).

ON A PRIVATE DATABASE.
"""
import re

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_native_late_materialization"
ROWS = 20480
NEEDLE = "zqx"
GEN = (f"SELECT g AS id, "
       f"CASE WHEN g % 1024 = 7 THEN 'aaa{NEEDLE}bbb' ELSE 'aaaaaabbb' END AS k, "
       f"repeat(md5(g::text), 4) AS p1, repeat(md5((g+1)::text), 4) AS p2, "
       f"repeat(md5((g+2)::text), 4) AS p3 FROM generate_series(1, {ROWS}) g")
Q = f"SELECT * FROM n WHERE k LIKE '%{NEEDLE}%'"
QH = f"SELECT * FROM h WHERE k LIKE '%{NEEDLE}%'"
QV = f"SELECT * FROM n WHERE k LIKE '%{NEEDLE}%' AND random() < 2"
QVH = f"SELECT * FROM h WHERE k LIKE '%{NEEDLE}%' AND random() < 2"
OFF = "SET pgcolumnar.enable_late_materialization = off"
EARLY = "Columnar Rows Filtered Before Materialization"
# The shell's cluster runs serial; a parallel plan prints per-worker averages.
SERIAL = "SET max_parallel_workers_per_gather = 0"


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _one(dsn, sql, setting=None):
    """-> the first column of the first row as text ("" for none or NULL), or the
    failed query as query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(SERIAL)
            if setting:
                conn.execute(setting)
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return "" if row is None or row[0] is None else str(row[0])


def _counter(dsn, label, sql, setting=None):
    """-> the integer on the EXPLAIN ANALYZE line holding label, a query_error() when
    the EXPLAIN failed, or "no reading" when no such line was printed. These counters
    are always printed, so a missing line is a failed reading, not 0."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(SERIAL)
            if setting:
                conn.execute(setting)
            plan = [r[0] for r in conn.execute(
                f"EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) {sql}").fetchall()]
    except psycopg.Error as exc:
        return query_error(_message(exc))
    for line in plan:
        if label in line:
            m = re.search(r"\d+", line[line.index(label) + len(label):])
            if m:
                return int(m.group(0))
    return f'no reading (no "{label}" line)'


def _int(v):
    return int(v) if isinstance(v, str) and v.isdigit() else v


@pytest.fixture(scope="module")
def nlm(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            c.execute("CREATE TABLE h (id int, k text, p1 text, p2 text, p3 text)")
            c.execute("CREATE TABLE n (id int, k text, p1 text, p2 text, p3 text) USING pgcolumnar")
            c.execute("SELECT pgcolumnar.set_options('n', stripe_row_limit => 65536, "
                      "chunk_group_row_limit => 1024)")
            c.execute(f"INSERT INTO h {GEN}")
            c.execute(f"INSERT INTO n {GEN}")
            c.execute("ANALYZE h")
            c.execute("ANALYZE n")
        r = {
            "rows": _int(_one(dsn, "SELECT count(*) FROM n")),
            "n match": _int(_one(dsn, f"SELECT count(*) FROM n WHERE k LIKE '%{NEEDLE}%'")),
            "h match": _int(_one(dsn, f"SELECT count(*) FROM h WHERE k LIKE '%{NEEDLE}%'")),
            "proj": _counter(dsn, "Columnar Projected Columns", Q),
            "groups removed": _counter(dsn, "Columnar Chunk Groups Removed by Filter", Q),
            "vectors skipped": _counter(dsn, "Columnar Vectors Skipped", Q),
            "early": _counter(dsn, EARLY, Q),
            "removed": _counter(dsn, "Rows Removed by Filter", Q),
            "heap md5": _one(dsn, f"SELECT md5(string_agg(t::text, '|' ORDER BY id)) FROM ({QH}) t"),
            "col md5": _one(dsn, f"SELECT md5(string_agg(t::text, '|' ORDER BY id)) FROM ({Q}) t"),
            "off early": _counter(dsn, EARLY, Q, OFF),
            "off md5": _one(dsn, f"SELECT md5(string_agg(t::text, '|' ORDER BY id)) FROM ({Q}) t", OFF),
            "vol early": _counter(dsn, EARLY, QV),
            "vol heap md5": _one(dsn, f"SELECT md5(string_agg(t::text, '|' ORDER BY id)) FROM ({QVH}) t"),
            "vol col md5": _one(dsn, f"SELECT md5(string_agg(t::text, '|' ORDER BY id)) FROM ({QV}) t"),
            "same shape early": _counter(dsn, EARLY, Q),
        }
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _rejected(r):
    """-> ROWS minus the matches, or the failed match reading itself."""
    return ROWS - r["n match"] if isinstance(r["n match"], int) else r["n match"]


def test_late_materialization_fixture_loaded_every_row(nlm, expect):
    expect.num(nlm["rows"], ROWS, "premise: the fixture loaded every row")


def test_late_materialization_needle_is_selective(nlm, expect):
    expect.num(nlm["n match"], 20, "premise: the needle is selective, not most of the table")


def test_late_materialization_heap_agrees_on_matches(nlm, expect):
    expect.num(nlm["h match"], nlm["n match"], "premise: heap agrees on how many rows match")


def test_late_materialization_query_runs_on_the_custom_scan(nlm, expect):
    p = nlm["proj"]
    expect.text("yes" if isinstance(p, int) else f"no ({p})", "yes",
                "the query runs on the columnar custom scan")


def test_late_materialization_nothing_pruned_at_group_level(nlm, expect):
    expect.num(nlm["groups removed"], 0, "nothing was pruned at the group level")


def test_late_materialization_nothing_pruned_at_vector_level(nlm, expect):
    expect.num(nlm["vectors skipped"], 0, "nothing was pruned at the vector level")


def test_late_materialization_rejected_row_not_materialized(nlm, expect):
    expect.num(nlm["early"], _rejected(nlm), "a rejected row does not materialize its payload columns")


def test_late_materialization_executor_still_reports_filtered_rows(nlm, expect):
    expect.num(nlm["removed"], _rejected(nlm), "the executor still reports the rows the scan filtered")


def test_late_materialization_rows_identical_to_heap(nlm, expect):
    expect.text(nlm["col md5"], nlm["heap md5"], "the rows are identical to the heap mirror")


def test_late_materialization_off_filters_nothing_early(nlm, expect):
    expect.num(nlm["off early"], 0, "with the feature off nothing is filtered early")


def test_late_materialization_off_answers_unchanged(nlm, expect):
    expect.text(nlm["off md5"], nlm["heap md5"], "and the answers are the same with it off")


def test_late_materialization_volatile_qual_refused(nlm, expect):
    expect.num(nlm["vol early"], 0, "a volatile qual is refused the two-pass path")


def test_late_materialization_volatile_matches_heap(nlm, expect):
    expect.text(nlm["vol col md5"], nlm["vol heap md5"], "and the volatile query still matches the heap mirror")


def test_late_materialization_same_shape_uses_the_path(nlm, expect):
    expect.num(nlm["same shape early"], _rejected(nlm), "the same shape without the volatile term does use it")
