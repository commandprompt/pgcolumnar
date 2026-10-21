"""A columnar table with a generated column returns what heap does. The pytest twin of
`test/generated_columns.sh`, every check by the shell's name (#1341).

STORED generated columns, on every major, must read, filter and aggregate as the heap
copy does, and get a column chunk. VIRTUAL ones, on PostgreSQL 18+, must read and
filter as heap does, equal their expression on every row, and store no chunk while the
base column still gets one. Before 18 the virtual arm declines, by the shell's skip
name, in the one test that carries it; the other virtual tests decline unnamed, because
the shell records one skip and naming six would publish checks it does not have.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading (#1339). Every columnar reading carries its row count against the
constant the fixture loads, and the heap oracle must hold the fixture's aggregate, so
two empty tables cannot agree.
"""
import psycopg
import pytest

from pgc_vacuity import QUERY_ERROR, query_error

DB = "pgc_generated_columns"
HASH = ("SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), 'EMPTY') "
        "FROM (SELECT _row::text AS t FROM ({}) _row) _s")


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


def _hash(dsn, sql):
    return _value(dsn, HASH.format(sql))


def _counted_hash(dsn, sql):
    n, h = _value(dsn, f"SELECT count(*) FROM ({sql}) _c"), _hash(dsn, sql)
    return query_error(f"no counted hash: {n} / {h}") if _failed(n) or _failed(h) else f"{n}:{h}"


def _expected(n, h):
    return query_error(f"no oracle hash: {h}") if _failed(h) else f"{n}:{h}"


def _chunks(dsn, table, where):
    return _value(dsn, "SELECT count(*) FROM pgcolumnar.column_chunk "
                       f"WHERE storage_id = pgcolumnar.get_storage_id('{table}') AND {where}")


@pytest.fixture(scope="module")
def gc(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r = {}
        for kind, am, tab in (("STORED", "heap", "gs_heap"), ("STORED", "pgcolumnar", "gs_col")):
            _run(dsn, f"CREATE TABLE {tab} (a int, b bigint GENERATED ALWAYS AS (a * 2) {kind}, "
                      f"c text GENERATED ALWAYS AS ('r' || a) {kind}) USING {am}; "
                      f"INSERT INTO {tab} (a) SELECT g FROM generate_series(1, 5000) g;")
        r["s rows"] = _value(dsn, "SELECT count(*) FROM gs_col")
        r["s values"] = _counted_hash(dsn, "SELECT a, b, c FROM gs_col")
        r["s values want"] = _expected(5000, _hash(dsn, "SELECT a, b, c FROM gs_heap"))
        r["s filter"] = _counted_hash(dsn, "SELECT a, b FROM gs_col  WHERE b > 8000 AND b < 9000")
        r["s filter want"] = _expected(499, _hash(dsn, "SELECT a, b FROM gs_heap WHERE b > 8000 AND b < 9000"))
        agg = "SELECT sum(b)::bigint || '|' || count(c) FROM {}"
        r["s agg"] = _value(dsn, agg.format("gs_col"))
        heap = _value(dsn, agg.format("gs_heap"))
        r["s agg want"] = heap if heap == "25005000|5000" else query_error(
            f"the heap oracle does not hold the fixture: [{heap}]")
        r["s chunk"] = _chunks(dsn, "gs_col", "column_index = 1")

        raw = _value(dsn, "SHOW server_version_num")
        r["major"] = int(raw) // 10000 if str(raw).isdigit() else query_error(f"got [{raw}]")
        if isinstance(r["major"], int) and r["major"] >= 18:
            for am, tab in (("heap", "gv_heap"), ("pgcolumnar", "gv_col")):
                _run(dsn, f"CREATE TABLE {tab} (a int, b bigint GENERATED ALWAYS AS (a * 2) VIRTUAL, "
                          f"c text GENERATED ALWAYS AS ('r' || a) VIRTUAL) USING {am}; "
                          f"INSERT INTO {tab} (a) SELECT g FROM generate_series(1, 5000) g;")
            r["v rows"] = _value(dsn, "SELECT count(*) FROM gv_col")
            r["v values"] = _counted_hash(dsn, "SELECT a, b, c FROM gv_col")
            r["v values want"] = _expected(5000, _hash(dsn, "SELECT a, b, c FROM gv_heap"))
            r["v filter"] = _counted_hash(dsn, "SELECT a, b FROM gv_col  WHERE b > 8000 AND b < 9000")
            r["v filter want"] = _expected(499, _hash(dsn, "SELECT a, b FROM gv_heap WHERE b > 8000 AND b < 9000"))
            r["v mism"] = _value(dsn, "SELECT count(*) FILTER (WHERE b IS DISTINCT FROM a * 2 "
                                      "OR c IS DISTINCT FROM 'r' || a) || '/' || count(*) FROM gv_col")
            r["v chunks"] = _value(dsn, "SELECT count(*) FILTER (WHERE column_index IN (1, 2)) || '/' || "
                                        "(count(*) FILTER (WHERE column_index = 0) > 0) "
                                        "FROM pgcolumnar.column_chunk "
                                        "WHERE storage_id = pgcolumnar.get_storage_id('gv_col')")
            r["v base"] = _chunks(dsn, "gv_col", "column_index = 0")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _declined(gc, expect):
    """True when the virtual arm cannot run here; the decline is unnamed."""
    m = gc["major"]
    if isinstance(m, int) and m >= 18:
        return False
    expect.cannot_run("UNSUPPORTED_MAJOR",
                      f"VIRTUAL generated columns arrived in PostgreSQL 18; this server is {m}")
    return True


def test_stored_generated_row_count(gc, expect):
    expect.text(str(gc["s rows"]), "5000", "stored generated: row count")


def test_stored_generated_values_match(gc, expect):
    expect.text(gc["s values"], gc["s values want"], "stored generated: values match")


def test_stored_generated_filtered_scan_matches(gc, expect):
    expect.text(gc["s filter"], gc["s filter want"], "stored generated: filtered scan matches")


def test_stored_generated_aggregate_matches(gc, expect):
    expect.text(str(gc["s agg"]), gc["s agg want"], "stored generated: aggregate matches")


def test_stored_generated_chunk_present_for_attr_2(gc, expect):
    n = gc["s chunk"]
    expect.text(("yes" if n > 0 else "no") if isinstance(n, int) else n, "yes",
                "stored generated: chunk present for attr 2")


def test_virtual_generated_row_count(gc, expect):
    m = gc["major"]
    if not isinstance(m, int):
        expect.text(m, "readable", "could not read the server major, so the virtual arm cannot be gated")
        return
    if m < 18:
        expect.cannot_run("UNSUPPORTED_MAJOR",
                          f"VIRTUAL generated columns arrived in PostgreSQL 18; this server is {m}",
                          name="virtual generated columns")
        return
    expect.text(str(gc["v rows"]), "5000", "virtual generated: row count")


def test_virtual_generated_values_match(gc, expect):
    if _declined(gc, expect):
        return
    expect.text(gc["v values"], gc["v values want"], "virtual generated: values match")


def test_virtual_generated_filtered_scan_matches(gc, expect):
    if _declined(gc, expect):
        return
    expect.text(gc["v filter"], gc["v filter want"], "virtual generated: filtered scan matches")


def test_virtual_generated_recomputed_correctly(gc, expect):
    if _declined(gc, expect):
        return
    expect.text(str(gc["v mism"]), "0/5000", "virtual generated: recomputed correctly")


def test_virtual_generated_no_chunk_stored_for_the_virtual_columns(gc, expect):
    if _declined(gc, expect):
        return
    expect.text(str(gc["v chunks"]), "0/true", "virtual generated: no chunk stored for the virtual columns")


def test_virtual_generated_base_column_still_stored(gc, expect):
    if _declined(gc, expect):
        return
    n = gc["v base"]
    expect.text(("yes" if n > 0 else "no") if isinstance(n, int) else n, "yes",
                "virtual generated: base column still stored")
