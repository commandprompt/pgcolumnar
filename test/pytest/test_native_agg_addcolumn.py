"""Metadata aggregates over a column added by ALTER TABLE. The pytest twin of
`test/native_agg_addcolumn.sh`, every check by the shell's name (#1341).

An ungrouped, unfiltered aggregate is answered from row-group metadata. A column
added by ALTER TABLE ADD COLUMN has no chunk, and so no zone map, in any row group
written before it: its value there is the attribute's missing value, which only the
reader supplies. Folding such a group from its zone maps dropped those rows in
silence (count(d) 0 against 3000, sum and min null), so the fold sends the group to
be read instead.

The oracle is the same bare query with `pgcolumnar.enable_vectorization` off, on a
connection that carries the setting from its start, as the shell's PGOPTIONS does.
Each reading has a connection of its own, opened once any crash recovery is over, as
the shell's psql calls are: a reading that crashes the backend fails its own arm. Two premises come first: the metadata side takes the metadata path, and the
oracle side does not. Without the second, a setting that never took hold would leave
both sides on the path under test, agreeing with each other. And since both paths read
an old group's added column from the reader's missing value, the oracle's own answer
is pinned once, to the rows and default that went in.

ON A PRIVATE DATABASE, built once by a module fixture that runs the shell's steps in
its order and records each reading; each test asserts one, so one red cannot stop the
next (#1339). A failed query is a `query_error()`, refused on either side by name.
"""
import time

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_agg_addcolumn"
ROWS = 3000
OFF = "-c pgcolumnar.enable_vectorization=off"
LABEL = "Columnar Vectorized Aggregates"
SCAN = "Custom Scan (PgColumnarScan)"

# (name, aggregate, table, the step it follows), in the shell's order. count(*) never
# reads a column, so it was right before the fix too; it catches a fix that breaks the
# case that already worked
ARMS = (
    ("count over an added int column", "count(d)", "ac_t", "default"),
    ("sum over an added int column", "sum(d)", "ac_t", "default"),
    ("min over an added int column", "min(d)", "ac_t", "default"),
    ("max over an added int column", "max(d)", "ac_t", "default"),
    ("avg over an added int column", "avg(d)", "ac_t", "default"),
    ("count over an added text column", "count(e)", "ac_t", "default"),
    ("min over an added text column", "min(e)", "ac_t", "default"),
    ("count(*) is unaffected", "count(*)", "ac_t", "default"),
    ("count over an added column with no default", "count(d)", "ac_n", "no default"),
    ("sum over an added column with no default", "sum(d)", "ac_n", "no default"),
    ("count across groups written either side of the ALTER", "count(d)", "ac_t", "both sides"),
    ("sum across groups written either side of the ALTER", "sum(d)", "ac_t", "both sides"),
    ("max across groups written either side of the ALTER", "max(d)", "ac_t", "both sides"),
    ("min over text across both sides", "min(e)", "ac_t", "both sides"),
    ("count with both a delete and an added column", "count(d)", "ac_t", "delete"),
    ("sum with both a delete and an added column", "sum(d)", "ac_t", "delete"),
)


def _connect(dsn, options=""):
    """A connection, after waiting out any crash recovery, as the shell's qn does: a
    reading that crashes the backend fails its own arm, not every reading after it."""
    deadline = time.monotonic() + 60
    while True:
        try:
            return psycopg.connect(dsn, autocommit=True, connect_timeout=5, options=options)
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)


def _value(dsn, sql, options=""):
    """-> the one value sql returns on a connection of its own, as text, with NULL as
    "(null)" (psql's own rendering in the shell), or the failure as a query_error()."""
    try:
        with _connect(dsn, options) as conn:
            v = conn.execute(sql).fetchone()[0]
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
    return "(null)" if v is None else str(v)


def _path(dsn, options=""):
    """-> metadata | scan | other plan | no plan, for count(d) on ac_t; the scan is
    named by its node, so a failed EXPLAIN or some other plan cannot pass as it."""
    try:
        with _connect(dsn, options) as conn:
            plan = "\n".join(r[0] for r in conn.execute("EXPLAIN (COSTS OFF) SELECT count(d) FROM ac_t"))
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
    if not plan:
        return "no plan"
    if LABEL in plan:
        return "metadata"
    return "scan" if SCAN in plan else "other plan"


def _step(dsn, sql):
    try:
        with _connect(dsn) as conn:
            conn.execute(sql)
    except psycopg.Error as exc:
        print(f"-- step failed: {str(exc).splitlines()[0] if str(exc) else exc!r}")


@pytest.fixture(scope="module")
def addcol(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _step(dsn, "CREATE EXTENSION pgcolumnar")
        r = {}

        def both(step):
            for name, agg, table, at in ARMS:
                if at != step:
                    continue
                sql = f"SELECT {agg} FROM {table}"
                r[name] = (_value(dsn, sql), _value(dsn, sql, OFF))

        for sql in ("CREATE TABLE ac_t (id int) USING pgcolumnar",
                    f"INSERT INTO ac_t SELECT g FROM generate_series(1, {ROWS}) g",
                    "ALTER TABLE ac_t ADD COLUMN d int DEFAULT 7",
                    "ALTER TABLE ac_t ADD COLUMN e text DEFAULT 'zz'"):
            _step(dsn, sql)
        r["path"], r["oracle path"] = _path(dsn), _path(dsn, OFF)
        r["pinned"] = _value(dsn, "SELECT count(d) || '|' || sum(d) FROM ac_t", OFF)
        both("default")

        # the old rows are genuinely null here: the case the broken fold got
        # right by accident, and it must stay right
        for sql in ("CREATE TABLE ac_n (id int) USING pgcolumnar",
                    f"INSERT INTO ac_n SELECT g FROM generate_series(1, {ROWS}) g",
                    "ALTER TABLE ac_n ADD COLUMN d int"):
            _step(dsn, sql)
        both("no default")

        # groups on both sides of the ALTER: older ones with no chunk for d,
        # newer ones with a real chunk and a zone map
        _step(dsn, f"INSERT INTO ac_t SELECT g, 100, 'new' "
                   f"FROM generate_series({ROWS + 1}, {ROWS + 500}) g")
        both("both sides")

        # a delete, so the per-group delete handling and the missing column
        # have to work together; in the newer group, since a group with deletes
        # goes whole to the scan, and the old one would never reach the fold
        _step(dsn, f"DELETE FROM ac_t WHERE id = {ROWS + 5}")
        both("delete")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_the_metadata_aggregate_path_is_the_one_under_test(addcol, expect):
    expect.text(addcol["path"], "metadata", "the metadata aggregate path is the one under test")


def test_the_oracle_reads_the_rows(addcol, expect):
    expect.text(addcol["oracle path"], "scan", "premise: the oracle reads the rows")


def test_the_scan_reads_an_added_columns_default_for_the_rows_written_before_it(addcol, expect):
    """Both paths read an old group's added column from the reader's missing value, so
    a reader that lost it would leave them agreeing on the wrong answer: the oracle's
    own answer is pinned once, to the rows and the default (7) that went in."""
    expect.text(addcol["pinned"], f"{ROWS}|{ROWS * 7}",
                "the scan reads an added column's default for the rows written before it")


@pytest.mark.parametrize("name, agg, table, step", ARMS)
def test_the_metadata_path_agrees_with_the_scan(addcol, expect, name, agg, table, step):
    expect.text(*addcol[name], name)
