"""Arrow nested-type export: 1-D arrays become Arrow List and named composites Arrow
Struct (gap 27). The pytest twin of `test/arrow_nested.sh`, every check by the shell's
name (#1341).

A columnar table with int[], text[] and composite columns, NULL arrays, empty arrays,
NULL elements and NULL composites among them, is exported; pyarrow reads the file back,
the field types must be List/List/Struct, and every one of the 5000 rows must equal
the value recomputed from the generator. A table with nested columns and no rows still
writes a readable schema-only stream, and a multi-dimensional array is refused rather
than flattened.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading, so one red cannot stop the next (#1339). The shell skips whole
without pyarrow, so every test declines by the shell's skip name first. The files live
in `server_dir`, because the backend writes them. A failed query is a `query_error()`.
"""
import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_arrow_nested"
MULTIDIM = "does not support multi-dimensional arrays"


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


def _run(dsn, sql):
    """Run one statement and swallow its failure, as the shell's psql_run does: a
    failed step shows up in the readings after it."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)
    except psycopg.Error:
        pass


def _refusal(dsn, sql):
    """-> "refused: multi-dimensional", "succeeded", or the other error."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)
    except psycopg.Error as exc:
        msg = str(exc)
        return "refused: multi-dimensional" if MULTIDIM in msg else f"other error: {msg.splitlines()[0] if msg else type(exc).__name__}"
    return "succeeded"


def _read(pa, path):
    return pa.ipc.open_stream(pa.OSFile(str(path), "rb")).read_all()


@pytest.fixture(scope="module")
def nested(pgc_cluster, server_dir):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        nf, ef = server_dir / "nt.arrows", server_dir / "nte.arrows"
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        _run(dsn, "CREATE TYPE ntc AS (x int, y text)")
        _run(dsn, "CREATE TABLE nt (id int, ia int[], ta text[], c ntc) USING pgcolumnar")
        _run(dsn, "INSERT INTO nt SELECT g, "
                  "CASE WHEN g % 10 = 0 THEN NULL WHEN g % 7 = 0 THEN '{}'::int[] "
                  "ELSE ARRAY[g, NULL, g + 3] END, "
                  "ARRAY['a' || g, 'b' || g], "
                  "CASE WHEN g % 5 = 0 THEN NULL ELSE ROW(g * 2, 'c' || g)::ntc END "
                  "FROM generate_series(1, 5000) g")
        r = {"file": nf, "empty file": ef,
             "rows": _value(dsn, f"SELECT pgcolumnar.export_arrow('nt', '{nf}')")}
        _run(dsn, "CREATE TABLE nte (id int, ia int[], c ntc) USING pgcolumnar")
        r["empty rows"] = _value(dsn, f"SELECT pgcolumnar.export_arrow('nte', '{ef}')")
        _run(dsn, "CREATE TABLE ntm (id int, m int[]) USING pgcolumnar")
        _run(dsn, "INSERT INTO ntm VALUES (1, ARRAY[[1,2],[3,4]])")
        r["multidim"] = _refusal(dsn, f"SELECT pgcolumnar.export_arrow('ntm', '{server_dir / 'ntm.arrows'}')")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _types(pa, path):
    s = _read(pa, path).schema
    ia, ta, c = s.field("ia").type, s.field("ta").type, s.field("c").type
    return " ".join(str(v) for v in (
        pa.types.is_list(ia), ia.value_type, pa.types.is_list(ta), ta.value_type,
        pa.types.is_struct(c), c.num_fields,
        c.field(0).name, c.field(0).type, c.field(1).name, c.field(1).type))


def _values(pa, path):
    """-> "MATCH" when the file holds every id 1..5000 once and each row equals the
    generator's, else what differs."""
    d = _read(pa, path).to_pydict()
    if sorted(d["id"]) != list(range(1, 5001)):
        return f"ROWS {len(d['id'])}, not ids 1..5000"
    for i, g in enumerate(d["id"]):
        ia = None if g % 10 == 0 else ([] if g % 7 == 0 else [g, None, g + 3])
        ta = [f"a{g}", f"b{g}"]
        c = None if g % 5 == 0 else {"x": g * 2, "y": f"c{g}"}
        if d["ia"][i] != ia or d["ta"][i] != ta or d["c"][i] != c:
            return f"MISMATCH@{g}"
    return "MATCH"


def _empty(pa, path):
    t = _read(pa, path)
    return f"{t.num_rows} {pa.types.is_list(t.schema.field('ia').type)} {pa.types.is_struct(t.schema.field('c').type)}"


def _reading(fn, pa, path):
    """-> fn(pa, path), or the failure to read the file as a query_error()."""
    try:
        return fn(pa, path)
    except (OSError, KeyError, IndexError, ValueError, pa.ArrowException) as exc:
        # a missing or unreadable file, or a field it lacks, is a failed reading
        return query_error(f"{type(exc).__name__}: {exc}")


def test_the_nested_export_writes_every_row(nested, expect):
    if expect.require_module("pyarrow", name="pyarrow not available; skipping Arrow nested export verification") is None:
        return
    expect.text(nested["rows"], "5000", "nested export rows written")


def test_the_fields_are_list_list_struct(nested, expect):
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Arrow nested export verification")
    if pa is None or expect.require_module("pyarrow.ipc", name="pyarrow not available; skipping Arrow nested export verification") is None:
        return
    expect.text(_reading(_types, pa, nested["file"]), "True int32 True string True 2 x int32 y string",
                "nested field types")


def test_every_value_matches_the_generator(nested, expect):
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Arrow nested export verification")
    if pa is None or expect.require_module("pyarrow.ipc", name="pyarrow not available; skipping Arrow nested export verification") is None:
        return
    expect.text(_reading(_values, pa, nested["file"]), "MATCH", "nested values match expectation")


def test_the_empty_table_writes_no_rows(nested, expect):
    if expect.require_module("pyarrow", name="pyarrow not available; skipping Arrow nested export verification") is None:
        return
    expect.text(nested["empty rows"], "0", "empty nested rows")


def test_the_empty_table_writes_a_readable_schema(nested, expect):
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Arrow nested export verification")
    if pa is None or expect.require_module("pyarrow.ipc", name="pyarrow not available; skipping Arrow nested export verification") is None:
        return
    expect.text(_reading(_empty, pa, nested["empty file"]), "0 True True", "empty nested readable")


def test_a_multidimensional_array_is_refused(nested, expect):
    if expect.require_module("pyarrow", name="pyarrow not available; skipping Arrow nested export verification") is None:
        return
    expect.text(nested["multidim"], "refused: multi-dimensional", "reject multi-dim array")
