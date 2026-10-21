"""Parquet nested-type export: 1-D arrays become Parquet LIST and named composites Parquet
groups, with Dremel repetition and definition levels (gap 27). The pytest twin of
`test/parquet_nested.sh`, every check by the shell's name (#1341).

A columnar table with int[], text[] and composite columns, NULL arrays, empty arrays,
NULL elements and NULL composites among them, is exported; pyarrow reads the file back,
the field types must be list/list/struct, and every one of the 5000 rows must equal the
value recomputed from the generator. A 70,000-row table of arrays spans several row
groups and must round-trip whole, and a multi-dimensional array is refused rather than
flattened.

EVERY ID, ONCE. The value comparison only visits rows the file holds, so an empty file
matched; it now requires ids 1..5000. And the refusal must be the multi-dimensional one:
any error used to count, a missing table's included.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading, so one red cannot stop the next. The shell skips whole without
pyarrow, so every test declines by the shell's skip name first. The files live in
`server_dir`, because the backend writes them. A failed query is a `query_error()`.
"""
import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_parquet_nested"
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
    """Run one statement and swallow its failure, as the shell's psql_run does: a failed
    step shows up in the readings after it."""
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


@pytest.fixture(scope="module")
def nested(pgc_cluster, server_dir):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        nf, nf2 = server_dir / "nt.parquet", server_dir / "np2.parquet"
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        _run(dsn, "CREATE TYPE npc AS (x int, y text)")
        _run(dsn, "CREATE TABLE np (id int, ia int[], ta text[], c npc) USING pgcolumnar")
        _run(dsn, "INSERT INTO np SELECT g, "
                  "CASE WHEN g % 10 = 0 THEN NULL WHEN g % 7 = 0 THEN '{}'::int[] "
                  "ELSE ARRAY[g, NULL, g + 3] END, "
                  "ARRAY['a' || g, 'b' || g], "
                  "CASE WHEN g % 5 = 0 THEN NULL ELSE ROW(g * 2, 'c' || g)::npc END "
                  "FROM generate_series(1, 5000) g")
        r = {"file": nf, "file2": nf2,
             "rows": _value(dsn, f"SELECT pgcolumnar.export_parquet('np', '{nf}')")}
        _run(dsn, "CREATE TABLE np2 (id int, ia int[]) USING pgcolumnar")
        _run(dsn, "INSERT INTO np2 SELECT g, ARRAY[g, g*2] FROM generate_series(1, 70000) g")
        r["rows2"] = _value(dsn, f"SELECT pgcolumnar.export_parquet('np2', '{nf2}')")
        _run(dsn, "CREATE TABLE npm (id int, m int[]) USING pgcolumnar")
        _run(dsn, "INSERT INTO npm VALUES (1, ARRAY[[1,2],[3,4]])")
        r["multidim"] = _refusal(dsn, f"SELECT pgcolumnar.export_parquet('npm', '{server_dir / 'npm.parquet'}')")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _types(pa, pq, path):
    s = pq.read_schema(str(path))
    ia, ta, c = s.field("ia").type, s.field("ta").type, s.field("c").type
    return " ".join(str(v) for v in (
        pa.types.is_list(ia), ia.value_type, pa.types.is_list(ta), ta.value_type,
        pa.types.is_struct(c), c.num_fields,
        c.field(0).name, c.field(0).type, c.field(1).name, c.field(1).type))


def _values(pq, path):
    """-> "MATCH" when the file holds every id 1..5000 once and each row equals the
    generator's, else what differs."""
    d = pq.read_table(str(path)).to_pydict()
    if sorted(d["id"]) != list(range(1, 5001)):
        return f"ROWS {len(d['id'])}, not ids 1..5000"
    for i, g in enumerate(d["id"]):
        ia = None if g % 10 == 0 else ([] if g % 7 == 0 else [g, None, g + 3])
        ta = [f"a{g}", f"b{g}"]
        c = None if g % 5 == 0 else {"x": g * 2, "y": f"c{g}"}
        if d["ia"][i] != ia or d["ta"][i] != ta or d["c"][i] != c:
            return f"MISMATCH@{g}"
    return "MATCH"


def _values2(pq, path):
    d = pq.read_table(str(path)).to_pydict()
    ok = all(d["ia"][i] == [d["id"][i], d["id"][i] * 2] for i in range(len(d["id"])))
    return "MATCH" if ok and len(d["id"]) == 70000 else "MISMATCH"


def _reading(read, pa):
    """-> read(), or the failure to read the file as a query_error()."""
    try:
        return read()
    except (OSError, KeyError, IndexError, ValueError, pa.ArrowException) as exc:
        # a missing or unreadable file, or a field it lacks, is a failed reading
        return query_error(f"{type(exc).__name__}: {exc}")


def _modules(expect):
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet nested export verification")
    pq = expect.require_module("pyarrow.parquet", name="pyarrow not available; skipping Parquet nested export verification") if pa is not None else None
    return (pa, pq) if pq is not None else (None, None)


def test_the_nested_parquet_export_writes_every_row(nested, expect):
    if expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet nested export verification") is None:
        return
    expect.text(nested["rows"], "5000", "nested Parquet export rows written")


def test_the_parquet_fields_are_list_list_struct(nested, expect):
    pa, pq = _modules(expect)
    if pa is None:
        return
    expect.text(_reading(lambda: _types(pa, pq, nested["file"]), pa), "True int32 True string True 2 x int32 y string",
                "nested Parquet field types")


def test_every_parquet_value_matches_the_generator(nested, expect):
    pa, pq = _modules(expect)
    if pa is None:
        return
    expect.text(_reading(lambda: _values(pq, nested["file"]), pa), "MATCH", "nested Parquet values match expectation")


def test_the_multi_row_group_export_writes_every_row(nested, expect):
    if expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet nested export verification") is None:
        return
    expect.text(nested["rows2"], "70000", "np2 export rows")


def test_the_multi_row_group_values_match(nested, expect):
    pa, pq = _modules(expect)
    if pa is None:
        return
    expect.text(_reading(lambda: _values2(pq, nested["file2"]), pa), "MATCH", "np2 values match")


def test_a_multidimensional_array_is_refused_for_parquet(nested, expect):
    if expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet nested export verification") is None:
        return
    expect.text(nested["multidim"], "refused: multi-dimensional", "reject multi-dim array")
