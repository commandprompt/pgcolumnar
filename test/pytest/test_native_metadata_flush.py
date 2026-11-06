"""A stripe flush opens each metadata table ONCE, not once per row (#445). The pytest twin
of `test/native_metadata_flush.sh`, every check by the shell's name (#1341).

The witness is the DEBUG1 line each flush emits, "pgcolumnar metadata flush: opens=N
tables=M", where N counts the real metadata relation opens of that flush. Batched, a
3-column and a 20-column flush open the same small set of tables; un-batched, the wide
one opens one relation per metadata row (141 against 22, measured). The count is held
from both sides: no more than six, and at least the three tables every flush writes, so
a witness that counted nothing (opens=0) cannot pass as "small and equal".

The written data is compared with a heap mirror built FROM THE GENERATOR, not copied
from the columnar table, so a writer that stored a wrong value cannot agree with itself.
And the range read must rule out a vector by its zone row alone (Skipped minus Ruled Out
by Value), because the reader also rules vectors out by value after decoding, with or
without a usable zone map.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading, and a failed query is a `query_error()`.
"""
import re

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_native_metadata_flush"
ROWS = 2000
LIMITS = ("SET pgcolumnar.stripe_row_limit = 2000", "SET pgcolumnar.chunk_group_row_limit = 500")
WIDE_COLS = [f"c{i:02d}" for i in range(20)]
WIDE_SUM = "+".join(WIDE_COLS)
GEN20 = ",".join(["g"] * 20)
RANGE = "c00 BETWEEN 500 AND 1500"


def _failed(exc):
    return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _run(dsn, sql):
    """Run one statement and swallow its failure, as the shell's q does for setup: a
    failed step shows up in the readings after it."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)
    except psycopg.Error:
        pass


def _one(dsn, sql):
    """-> the first column of the first row as text, or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return _failed(exc)
    if row is None or row[0] is None:
        return query_error(f"no value from {sql}")
    return str(row[0])


def _opens(dsn, sql):
    """-> N from the last 'metadata flush: opens=N' DEBUG1 line the statement raised in a
    session holding the stripe limits, as the shell's opens() reads it; or a failure."""
    messages = []
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.add_notice_handler(lambda d: messages.append(d.message_primary or ""))
            conn.execute("SET client_min_messages = debug1")
            for s in LIMITS:
                conn.execute(s)
            conn.execute(sql)
    except psycopg.Error as exc:
        return _failed(exc)
    found = [m.group(1) for line in messages for m in [re.search(r"metadata flush: opens=(\d+)", line)] if m]
    return found[-1] if found else query_error("no 'metadata flush: opens=' line from the flush")


def _plan_field(plan, label):
    if not isinstance(plan, list):
        return plan
    for line in plan:
        m = re.search(re.escape(label) + r": (\d+)", line)
        if m:
            return m.group(1)
    return query_error(f"no '{label}' line in the range read's plan")


def _plan(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("SET max_parallel_workers_per_gather = 0")
            return [r[0] for r in conn.execute(sql).fetchall()]
    except psycopg.Error as exc:
        return _failed(exc)


@pytest.fixture(scope="module")
def mf(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r = {}
        _run(dsn, "CREATE TABLE tprobe (a int, b int, c int) USING pgcolumnar")
        r["probe"] = _opens(dsn, f"INSERT INTO tprobe SELECT g, g*2, g%7 FROM generate_series(1,{ROWS}) g")
        _run(dsn, "CREATE TABLE tnarrow (a int, b int, c int) USING pgcolumnar")
        _run(dsn, "CREATE TABLE twide (" + ", ".join(f"{c} int" for c in WIDE_COLS) + ") USING pgcolumnar")
        r["narrow"] = _opens(dsn, f"INSERT INTO tnarrow SELECT g, g*2, g%7 FROM generate_series(1,{ROWS}) g")
        r["wide"] = _opens(dsn, f"INSERT INTO twide SELECT {GEN20} FROM generate_series(1,{ROWS}) g")
        # The mirror is written from the generator, not copied from twide (#1339).
        _run(dsn, "CREATE TABLE twide_h (LIKE twide)")
        _run(dsn, f"INSERT INTO twide_h SELECT {GEN20} FROM generate_series(1,{ROWS}) g")
        r["count"] = _one(dsn, "SELECT count(*) FROM twide")
        r["sum col"] = _one(dsn, f"SELECT sum({WIDE_SUM}) FROM twide")
        r["sum heap"] = _one(dsn, f"SELECT sum({WIDE_SUM}) FROM twide_h")
        r["range col"] = _one(dsn, f"SELECT count(*) FROM twide WHERE {RANGE}")
        r["range heap"] = _one(dsn, f"SELECT count(*) FROM twide_h WHERE {RANGE}")
        plan = _plan(dsn, f"EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) SELECT count(*) FROM twide WHERE {RANGE}")
        r["skipped"] = _plan_field(plan, "Columnar Vectors Skipped")
        r["by value"] = _plan_field(plan, "Columnar Vectors Ruled Out by Value")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _num(v):
    return int(v) if isinstance(v, str) and v.isdigit() else v


def test_native_metadata_flush_emits_the_witness(mf, expect):
    expect.text("yes" if isinstance(mf["probe"], str) and mf["probe"].isdigit() else f"no ({mf['probe']})", "yes",
                "the flush emits the metadata-open witness")


def test_native_metadata_flush_narrow_and_wide_open_the_same(mf, expect):
    expect.num(_num(mf["narrow"]), _num(mf["wide"]),
               "narrow (3 col) and wide (20 col) flush open the same number of metadata relations")


def test_native_metadata_flush_wide_opens_a_small_constant(mf, expect):
    w = mf["wide"]
    got = "small" if isinstance(w, str) and w.isdigit() and int(w) <= 6 else f"big:[{w}]"
    expect.text(got, "small", "a 20-column flush opens a small constant number of metadata relations, not one per row")


def test_native_metadata_flush_wide_opens_at_least_three(mf, expect):
    w = mf["wide"]
    got = "yes" if isinstance(w, str) and w.isdigit() and int(w) >= 3 else f"no:[{w}]"
    expect.text(got, "yes", "and the 20-column flush opens at least the three tables every flush writes")


def test_native_metadata_flush_row_count_survives(mf, expect):
    expect.num(_num(mf["count"]), ROWS, "the wide table's row count survives the batched flush")


def test_native_metadata_flush_every_column_reads_back(mf, expect):
    expect.num(_num(mf["sum col"]), _num(mf["sum heap"]),
               "every column reads back correct (sum over all 20 columns matches the heap mirror)")


def test_native_metadata_flush_range_read_matches_heap(mf, expect):
    expect.num(_num(mf["range col"]), _num(mf["range heap"]),
               "a zone-map-pruned range read matches heap (the batched zone rows are correct)")


def test_native_metadata_flush_zone_row_rules_out_a_vector(mf, expect):
    s, b = mf["skipped"], mf["by value"]
    ok = isinstance(s, str) and s.isdigit() and isinstance(b, str) and b.isdigit() and int(s) - int(b) >= 1
    expect.text("yes" if ok else f"no:[skipped {s}, by value {b}]", "yes",
                "and that range read rules out a vector by its zone row alone")
