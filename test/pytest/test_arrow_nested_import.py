"""Arrow nested-type import round-trips arrays and composites (gap 27). The pytest twin
of `test/arrow_nested_import.sh`, every check by the shell's name (#1341).

A table with int[], text[] and composite columns is exported to Arrow and imported
into a fresh table, which must equal the original: the arrays and composites are
rebuilt from the Arrow List and Struct buffers, NULL arrays, empty arrays, NULL
elements and NULL composites included. After a DELETE on the source, a fresh export
and import must round-trip the live set alone.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading, so one red cannot stop the next (#1339). Each comparison hashes
both sides on the server exactly as lib.sh's `pgc_set_hash` does, and a failed query
is a `query_error()`, so two failures never compare equal. The source's side of each
agreement is read only when the source holds the rows it should, so two empty tables
cannot agree. The file lives in
`server_dir`, because the backend writes and reads it.
"""
import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_arrow_nested_import"
SET_HASH = ("SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), $e$EMPTY$e$) "
            "FROM (SELECT _row::text AS t FROM ( {q} ) _row) _s")
SEL = "SELECT id, ia::text, ta::text, c::text FROM {t}"
DEFS = "id int, ia int[], ta text[], c nic"


def _failed(exc):
    return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _value(dsn, sql):
    """-> the first column of the first row as text, or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return _failed(exc)
    return "" if row is None or row[0] is None else str(row[0])


def _count(dsn, sql):
    """-> the count as an int, or the failure (or a non-number) as a query_error()."""
    v = _value(dsn, sql)
    return int(v) if v.isdigit() else v if v else query_error(f"no count from {sql}")


def _of_source(dsn, rows, value):
    """-> `value`, the source's reading, when the source holds `rows` rows, else why it
    cannot be compared. Two empty tables hash alike and count alike, so an empty
    source passed every agreement; a sentence equals no reading of na2."""
    n = _count(dsn, "SELECT count(*) FROM na")
    return value if n == rows else f"source holds [{n}], not {rows} rows"


def _hash(dsn, table):
    return _value(dsn, SET_HASH.format(q=SEL.format(t=table)))


def _run(dsn, sql):
    """Run one statement and swallow its failure, as the shell's psql_run does: a
    failed step shows up in the readings after it."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)
    except psycopg.Error:
        pass


@pytest.fixture(scope="module")
def ani(pgc_cluster, server_dir):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        af = str(server_dir / "ni.arrows")
        export = f"SELECT pgcolumnar.export_arrow('na', '{af}')"
        imp = f"SELECT pgcolumnar.import_arrow('na2', '{af}')"
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        _run(dsn, "CREATE TYPE nic AS (x int, y text)")
        _run(dsn, f"CREATE TABLE na ({DEFS}) USING pgcolumnar")
        _run(dsn, "INSERT INTO na SELECT g, "
                  "CASE WHEN g % 10 = 0 THEN NULL WHEN g % 7 = 0 THEN '{}'::int[] "
                  "ELSE ARRAY[g, NULL, g + 3] END, "
                  "ARRAY['a' || g, 'b' || g], "
                  "CASE WHEN g % 5 = 0 THEN NULL ELSE ROW(g * 2, 'c' || g)::nic END "
                  "FROM generate_series(1, 5000) g")
        r = {"export": _value(dsn, export)}
        _run(dsn, f"CREATE TABLE na2 ({DEFS}) USING pgcolumnar")
        r["import"] = _value(dsn, imp)
        r["hash"] = (_hash(dsn, "na2"), _of_source(dsn, 5000, _hash(dsn, "na")))
        r["count"] = (_count(dsn, "SELECT count(*) FROM na2"),
                      _of_source(dsn, 5000, _count(dsn, "SELECT count(*) FROM na")))
        _run(dsn, "DELETE FROM na WHERE id BETWEEN 1000 AND 2000")
        r["live"] = _count(dsn, "SELECT count(*) FROM na")
        _run(dsn, export)
        _run(dsn, "TRUNCATE na2")
        _run(dsn, imp)
        r["hash after delete"] = (_hash(dsn, "na2"), _of_source(dsn, 3999, _hash(dsn, "na")))
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_the_export_writes_every_row(ani, expect):
    expect.text(ani["export"], "5000", "export rows")


def test_the_import_reads_every_row(ani, expect):
    expect.text(ani["import"], "5000", "import rows")


def test_the_nested_import_round_trips(ani, expect):
    expect.text(*ani["hash"], "nested import round-trips (arrays + composite)")


def test_the_import_count_matches(ani, expect):
    expect.num(*ani["count"], "import count matches")


def test_the_delete_left_the_live_rows(ani, expect):
    expect.num(ani["live"], 3999, "premise: the delete left 3999 live rows in the source")


def test_a_fresh_round_trip_after_a_delete(ani, expect):
    expect.text(*ani["hash after delete"], "round-trips after delete")
