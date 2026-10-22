"""Nested Parquet import round-trips arrays and composites (gap 27). The pytest twin of
`test/parquet_nested_import.sh`, every check by the shell's name (#1341).

A table with int[], text[] and composite columns, NULL arrays, empty arrays, NULL
elements and NULL composites among them, is exported to Parquet and imported into a
fresh table, which must equal the original as a set; after a DELETE, a fresh export and
import must equal the live rows. A nested file written by pyarrow, the reference writer,
must import as the generator's rows, proving the Dremel levels are read rather than only
our own exporter's layout.

A SET IS HASHED IN SQL, as the shell's pgc_set_hash does: md5 over the sorted row texts,
EMPTY for no rows, and a failed query is a query_error(), unique, so two failures never
agree. The row counts compare as numbers, so two failed counts no longer agree either.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order. Only the two
pyarrow arms need pyarrow, and each declines by its own name without it, as the shell's
two skips do. The files live in `server_dir`, because the backend reads and writes them.
"""
import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_parquet_nested_import"
SEL = "SELECT id, ia::text, ta::text, c::text FROM"
SEL3 = "SELECT id, ia::text, c::text FROM"


def _failed(exc):
    return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _value(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return _failed(exc)
    return "" if row is None or row[0] is None else str(row[0])


def _run(dsn, sql):
    """Run one statement and swallow its failure, as the shell's psql_run does."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)
    except psycopg.Error:
        pass


def _set_hash(dsn, query):
    return _value(dsn, "SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), 'EMPTY') "
                       f"FROM (SELECT _row::text AS t FROM ( {query} ) _row) _s")


def _int(v):
    return int(v) if isinstance(v, str) and v.isdigit() else v


def _write_reference(path, pa, pq):
    """Write the pyarrow nested file the shell writes, with the modules the test's
    require_module returned: a fixture cannot decline, so it never imports pyarrow."""
    ids = list(range(1, 201))
    ia = [None if g % 10 == 0 else ([] if g % 7 == 0 else [g, None, g + 3]) for g in ids]
    c = [None if g % 5 == 0 else {"x": g * 2, "y": "c" + str(g)} for g in ids]
    t = pa.table({
        "id": pa.array(ids, pa.int32()),
        "ia": pa.array(ia, pa.list_(pa.int32())),
        "c": pa.array(c, pa.struct([("x", pa.int32()), ("y", pa.string())])),
    })
    pq.write_table(t, str(path))
    path.chmod(0o644)


def _pyarrow_arm(ni, pa, pq):
    """Run the shell's pyarrow arm once, on the first pyarrow test that gets here."""
    if "pyarrow import" not in ni:
        dsn = ni["dsn"]
        pyf = ni["dir"] / "pyarrow_nested.parquet"
        _write_reference(pyf, pa, pq)
        _run(dsn, "CREATE TABLE pna3 (id int, ia int[], c pnic) USING pgcolumnar")
        ni["pyarrow import"] = _value(dsn, f"SELECT pgcolumnar.import_parquet('pna3', '{pyf}')")
        _run(dsn, "CREATE TABLE pna3_o (id int, ia int[], c pnic)")
        _run(dsn, "INSERT INTO pna3_o SELECT g, "
                  "CASE WHEN g % 10 = 0 THEN NULL WHEN g % 7 = 0 THEN '{}'::int[] "
                  "ELSE ARRAY[g, NULL, g + 3] END, "
                  "CASE WHEN g % 5 = 0 THEN NULL ELSE ROW(g * 2, 'c' || g)::pnic END "
                  "FROM generate_series(1, 200) g")
        ni["pyarrow oracle"] = (_set_hash(dsn, f"{SEL3} pna3"), _set_hash(dsn, f"{SEL3} pna3_o"))
    return ni


@pytest.fixture(scope="module")
def ni(pgc_cluster, server_dir):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        pf = server_dir / "ni.parquet"
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        _run(dsn, "CREATE TYPE pnic AS (x int, y text)")
        _run(dsn, "CREATE TABLE pna (id int, ia int[], ta text[], c pnic) USING pgcolumnar")
        _run(dsn, "INSERT INTO pna SELECT g, "
                  "CASE WHEN g % 10 = 0 THEN NULL WHEN g % 7 = 0 THEN '{}'::int[] "
                  "ELSE ARRAY[g, NULL, g + 3] END, "
                  "ARRAY['a' || g, 'b' || g], "
                  "CASE WHEN g % 5 = 0 THEN NULL ELSE ROW(g * 2, 'c' || g)::pnic END "
                  "FROM generate_series(1, 5000) g")
        r = {"export": _value(dsn, f"SELECT pgcolumnar.export_parquet('pna', '{pf}')")}
        _run(dsn, "CREATE TABLE pna2 (id int, ia int[], ta text[], c pnic) USING pgcolumnar")
        r["import"] = _value(dsn, f"SELECT pgcolumnar.import_parquet('pna2', '{pf}')")
        r["round trip"] = (_set_hash(dsn, f"{SEL} pna2"), _set_hash(dsn, f"{SEL} pna"))
        r["counts"] = (_value(dsn, "SELECT count(*) FROM pna2"), _value(dsn, "SELECT count(*) FROM pna"))
        _run(dsn, "DELETE FROM pna WHERE id BETWEEN 1000 AND 2000")
        _run(dsn, f"SELECT pgcolumnar.export_parquet('pna', '{pf}')")
        _run(dsn, "TRUNCATE pna2")
        _run(dsn, f"SELECT pgcolumnar.import_parquet('pna2', '{pf}')")
        r["after delete"] = (_set_hash(dsn, f"{SEL} pna2"), _set_hash(dsn, f"{SEL} pna"))

        r["dsn"], r["dir"] = dsn, server_dir
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_the_nested_table_exports_every_row(ni, expect):
    expect.text(ni["export"], "5000", "export rows")


def test_the_nested_file_imports_every_row(ni, expect):
    expect.text(ni["import"], "5000", "import rows")


def test_the_nested_parquet_import_round_trips(ni, expect):
    got, want = ni["round trip"]
    expect.text(got, want, "nested import round-trips (arrays + composite)")


def test_the_parquet_import_count_matches(ni, expect):
    got, want = ni["counts"]
    expect.num(_int(got), _int(want), "import count matches")


def test_a_fresh_round_trip_after_a_delete_matches(ni, expect):
    got, want = ni["after delete"]
    expect.text(got, want, "round-trips after delete")


def test_a_pyarrow_nested_file_imports_every_row(ni, expect):
    pa = expect.require_module("pyarrow", name="pyarrow import rows")
    pq = expect.require_module("pyarrow.parquet", name="pyarrow import rows") if pa else None
    if pq is None:
        return
    expect.text(_pyarrow_arm(ni, pa, pq)["pyarrow import"], "200", "pyarrow import rows")


def test_a_pyarrow_nested_file_matches_the_oracle(ni, expect):
    pa = expect.require_module("pyarrow", name="pyarrow nested file matches oracle")
    pq = expect.require_module("pyarrow.parquet", name="pyarrow nested file matches oracle") if pa else None
    if pq is None:
        return
    got, want = _pyarrow_arm(ni, pa, pq)["pyarrow oracle"]
    expect.text(got, want, "pyarrow nested file matches oracle")
