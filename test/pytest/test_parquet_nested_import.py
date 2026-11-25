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


# Definition levels that depend on the schema shape (#2079, #1916). The reader used
# fixed levels: def 0 was a NULL list or struct, def 1 an empty list. That holds only
# for an OPTIONAL list or struct with no OPTIONAL parent. A REQUIRED list, a REQUIRED
# struct, or a list inside a nullable struct moves the levels, and the reader returned
# wrong values with no error. The wanted values are the ones pyarrow reads back.
LVL_STL_WANT = "0:1:{1,2} 1:NULL:NULL 2:2:{} 3:3:{NULL} 4:NULL:NULL"
LVL_REQ_WANT = "0:{}:{}:(,x) 1:{NULL}:{5}:(5,y) 2:{1,NULL}:{1,2}:(,)"
LVL_STL_ROWS = ("string_agg(id || ':' || coalesce(sa::text, 'NULL') || ':' || "
                "coalesce(sl::text, 'NULL'), ' ' ORDER BY id)")
LVL_REQ_ROWS = ("string_agg(id || ':' || coalesce(rl::text, 'NULL') || ':' || "
                "coalesce(rlr::text, 'NULL') || ':' || coalesce(s::text, 'NULL'), ' ' ORDER BY id)")


def _write_level_files(stl_path, req_path, pa, pq):
    # Row 4 is a NULL struct: its list is NULL because a parent is (#2146 review).
    stl = pa.array([{"a": 1, "l": [1, 2]}, {"a": None, "l": None},
                    {"a": 2, "l": []}, {"a": 3, "l": [None]}, None],
                   pa.struct([("a", pa.int32()), ("l", pa.list_(pa.int64()))]))
    pq.write_table(pa.table({"id": pa.array([0, 1, 2, 3, 4], pa.int32()), "stl": stl}),
                   str(stl_path))
    schema = pa.schema([
        pa.field("id", pa.int32(), nullable=False),
        pa.field("rl", pa.list_(pa.field("item", pa.int32(), nullable=True)), nullable=False),
        pa.field("rlr", pa.list_(pa.field("item", pa.int32(), nullable=False)), nullable=False),
        pa.field("s", pa.struct([pa.field("a", pa.int32()), pa.field("b", pa.string())]),
                 nullable=False),
    ])
    pq.write_table(pa.Table.from_pylist([
        {"id": 0, "rl": [], "rlr": [], "s": {"a": None, "b": "x"}},
        {"id": 1, "rl": [None], "rlr": [5], "s": {"a": 5, "b": "y"}},
        {"id": 2, "rl": [1, None], "rlr": [1, 2], "s": {"a": None, "b": None}},
    ], schema=schema), str(req_path))
    stl_path.chmod(0o644)
    req_path.chmod(0o644)


def _write_shape_files(sub_path, flat_path, pa, pq):
    # A struct holding a field and a nullable sub-struct. A composite (a, b) binds
    # s.a and s.inner.b, two leaves at different depths. A NULL inner must leave a.
    sub = pa.array([{"a": 1, "inner": {"b": 2}}, {"a": 3, "inner": None}, None,
                    {"a": None, "inner": {"b": 4}}],
                   pa.struct([("a", pa.int32()), ("inner", pa.struct([("b", pa.int32())]))]))
    pq.write_table(pa.table({"id": pa.array([0, 1, 2, 3], pa.int32()), "s": sub}),
                   str(sub_path))
    # Flat top-level columns only: the file holds no struct for a composite.
    pq.write_table(pa.table({"id": pa.array([0, 1], pa.int32()),
                             "a": pa.array([1, None], pa.int32()),
                             "b": pa.array([None, 2], pa.int32())}), str(flat_path))
    sub_path.chmod(0o644)
    flat_path.chmod(0o644)


def _write_more_shape_files(paths, pa, pq):
    """The shapes of the second review of #2146, as the shell writes them."""
    i32 = pa.int32()
    # The deep leaf first: s struct<inner struct<b>, a>. A composite (b, a) stands
    # for s, not for inner, the first leaf's parent.
    mir = pa.array([{"inner": {"b": 2}, "a": 1}, {"inner": None, "a": 3}, None,
                    {"inner": {"b": 4}, "a": None}],
                   pa.struct([("inner", pa.struct([("b", i32)])), ("a", i32)]))
    pq.write_table(pa.table({"id": pa.array([0, 1, 2, 3], i32), "s": mir}), str(paths["mirror"]))
    # One field: s struct<a>, and a top-level int a1 for the one-column refusal.
    one = pa.array([{"a": None}, None, {"a": 7}], pa.struct([("a", i32)]))
    pq.write_table(pa.table({"id": pa.array([0, 1, 2], i32), "s": one,
                             "a1": pa.array([None, 3, 4], i32)}), str(paths["one"]))
    # A list two nullable structs deep: o struct<p struct<l list<int>>>.
    deep = pa.array([None, {"p": None}, {"p": {"l": None}}, {"p": {"l": []}},
                     {"p": {"l": [1, None]}}],
                    pa.struct([("p", pa.struct([("l", pa.list_(i32))]))]))
    pq.write_table(pa.table({"id": pa.array([0, 1, 2, 3, 4], i32), "o": deep}), str(paths["deep"]))
    # A composite over a nested struct: o struct<x, inner struct<a, b>>.
    nest = pa.array([None, {"x": 1, "inner": None}, {"x": 2, "inner": {"a": None, "b": None}},
                     {"x": 3, "inner": {"a": 5, "b": "q"}}],
                    pa.struct([("x", i32), ("inner", pa.struct([("a", i32), ("b", pa.string())]))]))
    pq.write_table(pa.table({"id": pa.array([0, 1, 2, 3], i32), "o": nest}), str(paths["nest"]))
    # The struct-in-a-struct shape of craft_parquet_nested.py's sos.parquet.
    sos = pa.array([{"t": {"a": 1, "b": "x"}}, {"t": {"a": 2, "b": "y"}}],
                   pa.struct([("t", pa.struct([("a", pa.int64()), ("b", pa.string())]))]))
    pq.write_table(pa.table({"s": sos, "n": pa.array([1, 2], i32)}), str(paths["sos"]))
    # A composite three nullable structs deep: o struct<p struct<q struct<a, b>>>.
    # Rows 0 to 2 make o, p, then q NULL; q's own def is 3 (#2146 re-check).
    deep3 = pa.array([None, {"p": None}, {"p": {"q": None}},
                      {"p": {"q": {"a": None, "b": None}}}, {"p": {"q": {"a": 1, "b": 2}}}],
                     pa.struct([("p", pa.struct([("q", pa.struct([("a", i32), ("b", i32)]))]))]))
    pq.write_table(pa.table({"id": pa.array([0, 1, 2, 3, 4], i32), "o": deep3}), str(paths["deep3"]))
    # Two top-level structs and a top-level column: s1 struct<a>, s2 struct<b>, b.
    # Leaves from two of them sit in no one struct (#2146 third review).
    pq.write_table(pa.table({"id": pa.array([0, 1, 2], i32),
                             "s1": pa.array([{"a": 1}, None, {"a": None}], pa.struct([("a", i32)])),
                             "s2": pa.array([{"b": 2}, {"b": 5}, None], pa.struct([("b", i32)])),
                             "b": pa.array([3, None, 6], i32)}), str(paths["two"]))
    # A REQUIRED struct under a nullable parent: o struct<x, inner struct<a, b> not null>.
    # A NULL o makes inner NULL too, though inner itself is REQUIRED.
    reqin = pa.struct([pa.field("x", i32),
                       pa.field("inner", pa.struct([("a", i32), ("b", pa.string())]), nullable=False)])
    pq.write_table(pa.table({"id": pa.array([0, 1, 2], i32),
                             "o": pa.array([None, {"x": 1, "inner": {"a": None, "b": None}},
                                            {"x": 2, "inner": {"a": 5, "b": "q"}}], reqin)}),
                   str(paths["reqin"]))
    # s struct<a, b>, for a composite over b alone: part of the struct.
    part = pa.array([{"a": 1, "b": 2}, None, {"a": 3, "b": None}], pa.struct([("a", i32), ("b", i32)]))
    pq.write_table(pa.table({"id": pa.array([0, 1, 2], i32), "s": part}), str(paths["part"]))
    for path in paths.values():
        path.chmod(0o644)


NOT_ONE_STRUCT = "binds Parquet columns that are not inside one struct"


def _refusal(dsn, sql):
    """-> "refused: not inside one struct", "succeeded", or the other error. The
    refusal counts only with SQLSTATE 42804 (datatype_mismatch)."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        msg = str(exc)
        if NOT_ONE_STRUCT in msg and exc.sqlstate == "42804":
            return "refused: not inside one struct"
        return f"other error: {msg.splitlines()[0] if msg else type(exc).__name__}"
    return "succeeded"


def _level_arm(ni, pa, pq):
    """Write the two files and read them every way, once, on the first test here."""
    if "levels" not in ni:
        dsn = ni["dsn"]
        stl, req = ni["dir"] / "lvl_stl.parquet", ni["dir"] / "lvl_req.parquet"
        _write_level_files(stl, req, pa, pq)
        _run(dsn, "CREATE TYPE lvl_ab AS (a int, b text)")
        got = {"stl read": _value(
            dsn, "SELECT string_agg(id || ':' || coalesce(sa::text, 'NULL') || ':' || "
                 "coalesce(sl::text, 'NULL'), ' ' ORDER BY id) "
                 f"FROM pgcolumnar.read_parquet('{stl}') AS t(id int, sa int, sl bigint[])")}
        got["req read"] = _value(
            dsn, f"SELECT {LVL_REQ_ROWS} FROM pgcolumnar.read_parquet('{req}') "
                 "AS t(id int, rl int[], rlr int[], s lvl_ab)")
        _run(dsn, "CREATE TABLE lvl_heap (id int, rl int[], rlr int[], s lvl_ab)")
        _run(dsn, f"SELECT pgcolumnar.import_parquet('lvl_heap', '{req}')")
        got["req import"] = _value(dsn, f"SELECT {LVL_REQ_ROWS} FROM lvl_heap")
        _run(dsn, "CREATE SERVER lvl_srv FOREIGN DATA WRAPPER pgcolumnar_parquet")
        _run(dsn, "CREATE FOREIGN TABLE lvl_f (id int, rl int[], rlr int[], s lvl_ab) "
                  f"SERVER lvl_srv OPTIONS (path '{req}')")
        got["req fdw"] = _value(dsn, f"SELECT {LVL_REQ_ROWS} FROM lvl_f")
        # The list inside a nullable struct, through the other two read paths.
        _run(dsn, "CREATE TABLE lvl_stl_heap (id int, sa int, sl bigint[])")
        _run(dsn, f"SELECT pgcolumnar.import_parquet('lvl_stl_heap', '{stl}')")
        got["stl import"] = _value(dsn, f"SELECT {LVL_STL_ROWS} FROM lvl_stl_heap")
        _run(dsn, "CREATE FOREIGN TABLE lvl_stl_f (id int, sa int, sl bigint[]) "
                  f"SERVER lvl_srv OPTIONS (path '{stl}')")
        got["stl fdw"] = _value(dsn, f"SELECT {LVL_STL_ROWS} FROM lvl_stl_f")
        sub, flat = ni["dir"] / "lvl_sub.parquet", ni["dir"] / "lvl_flat.parquet"
        _write_shape_files(sub, flat, pa, pq)
        _run(dsn, "CREATE TYPE lvl_ii AS (a int, b int)")
        got["sub read"] = _value(
            dsn, "SELECT string_agg(id || ':' || coalesce(s::text, 'NULL'), ' ' ORDER BY id) "
                 f"FROM pgcolumnar.read_parquet('{sub}') AS t(id int, s lvl_ii)")
        got["flat refusal"] = _refusal(
            dsn, f"SELECT * FROM pgcolumnar.read_parquet('{flat}') AS t(id int, c lvl_ii)")
        more = {k: ni["dir"] / f"lvl_{k}.parquet" for k in ("mirror", "one", "deep", "nest", "sos", "deep3",
                                                                 "two", "reqin", "part")}
        _write_more_shape_files(more, pa, pq)
        rows = "string_agg(id || ':' || coalesce(s::text, 'NULL'), ' ' ORDER BY id)"
        _run(dsn, "CREATE TYPE lvl_ba AS (b int, a int)")
        got["mirror read"] = _value(
            dsn, f"SELECT {rows} FROM pgcolumnar.read_parquet('{more['mirror']}') AS t(id int, s lvl_ba)")
        _run(dsn, "CREATE TYPE lvl_one AS (a int)")
        got["one read"] = _value(
            dsn, f"SELECT {rows} FROM pgcolumnar.read_parquet('{more['one']}') "
                 "AS t(id int, s lvl_one, a1 int)")
        got["one refusal"] = _refusal(
            dsn, f"SELECT * FROM pgcolumnar.read_parquet('{more['one']}') "
                 "AS t(id int, s lvl_one, c lvl_one)")
        got["deep read"] = _value(
            dsn, "SELECT string_agg(id || ':' || coalesce(l::text, 'NULL'), ' ' ORDER BY id) "
                 f"FROM pgcolumnar.read_parquet('{more['deep']}') AS t(id int, l int[])")
        got["nest read"] = _value(
            dsn, "SELECT string_agg(id || ':' || coalesce(x::text, 'NULL') || ':' || "
                 "coalesce(c::text, 'NULL'), ' ' ORDER BY id) "
                 f"FROM pgcolumnar.read_parquet('{more['nest']}') AS t(id int, x int, c lvl_ab)")
        _run(dsn, "CREATE TYPE lvl_lt AS (a bigint, b text)")
        got["sos read"] = _value(
            dsn, "SELECT string_agg(coalesce(c::text, 'NULL'), ' ' ORDER BY n) "
                 f"FROM pgcolumnar.read_parquet('{more['sos']}') AS t(c lvl_lt, n int)")
        got["deep3 read"] = _value(
            dsn, "SELECT string_agg(id || ':' || coalesce(c::text, 'NULL'), ' ' ORDER BY id) "
                 f"FROM pgcolumnar.read_parquet('{more['deep3']}') AS t(id int, c lvl_ii)")
        got["two refusal"] = _refusal(
            dsn, f"SELECT * FROM pgcolumnar.read_parquet('{more['two']}') AS t(id int, c lvl_ii, b int)")
        got["struct column refusal"] = _refusal(
            dsn, f"SELECT * FROM pgcolumnar.read_parquet('{more['two']}') AS t(id int, a int, c lvl_ii)")
        x_c_rows = ("string_agg(id || ':' || coalesce(x::text, 'NULL') || ':' || "
                    "coalesce(c::text, 'NULL'), ' ' ORDER BY id)")
        got["reqin read"] = _value(
            dsn, f"SELECT {x_c_rows} "
                 f"FROM pgcolumnar.read_parquet('{more['reqin']}') AS t(id int, x int, c lvl_ab)")
        _run(dsn, "CREATE TYPE lvl_b AS (b int)")
        got["part read"] = _value(
            dsn, f"SELECT {x_c_rows} "
                 f"FROM pgcolumnar.read_parquet('{more['part']}') AS t(id int, x int, c lvl_b)")
        ni["levels"] = got
    return ni["levels"]


def _levels_or_decline(ni, pa, pq):
    return None if pq is None else _level_arm(ni, pa, pq)


def test_a_list_in_a_nullable_struct_reads_like_pyarrow(ni, expect):
    pa = expect.require_module("pyarrow", name="list in a nullable struct reads like pyarrow")
    pq = expect.require_module("pyarrow.parquet",
                               name="list in a nullable struct reads like pyarrow") if pa else None
    got = _levels_or_decline(ni, pa, pq)
    if got is not None:
        expect.text(got["stl read"], LVL_STL_WANT, "list in a nullable struct reads like pyarrow")


def test_required_lists_and_struct_read_like_pyarrow(ni, expect):
    pa = expect.require_module("pyarrow", name="required lists and struct read like pyarrow")
    pq = expect.require_module("pyarrow.parquet",
                               name="required lists and struct read like pyarrow") if pa else None
    got = _levels_or_decline(ni, pa, pq)
    if got is not None:
        expect.text(got["req read"], LVL_REQ_WANT, "required lists and struct read like pyarrow")


def test_required_lists_and_struct_import_like_pyarrow(ni, expect):
    pa = expect.require_module("pyarrow", name="required lists and struct import like pyarrow")
    pq = expect.require_module("pyarrow.parquet",
                               name="required lists and struct import like pyarrow") if pa else None
    got = _levels_or_decline(ni, pa, pq)
    if got is not None:
        expect.text(got["req import"], LVL_REQ_WANT, "required lists and struct import like pyarrow")


def test_required_lists_and_struct_read_like_pyarrow_through_the_fdw(ni, expect):
    pa = expect.require_module("pyarrow",
                               name="required lists and struct read like pyarrow through the FDW")
    pq = expect.require_module("pyarrow.parquet",
                               name="required lists and struct read like pyarrow through the FDW") if pa else None
    got = _levels_or_decline(ni, pa, pq)
    if got is not None:
        expect.text(got["req fdw"], LVL_REQ_WANT,
                    "required lists and struct read like pyarrow through the FDW")



def test_a_list_in_a_nullable_struct_imports_like_pyarrow(ni, expect):
    pa = expect.require_module("pyarrow", name="list in a nullable struct imports like pyarrow")
    pq = expect.require_module("pyarrow.parquet",
                               name="list in a nullable struct imports like pyarrow") if pa else None
    got = _levels_or_decline(ni, pa, pq)
    if got is not None:
        expect.text(got["stl import"], LVL_STL_WANT, "list in a nullable struct imports like pyarrow")


def test_a_list_in_a_nullable_struct_reads_like_pyarrow_through_the_fdw(ni, expect):
    pa = expect.require_module("pyarrow",
                               name="list in a nullable struct reads like pyarrow through the FDW")
    pq = expect.require_module("pyarrow.parquet",
                               name="list in a nullable struct reads like pyarrow through the FDW") if pa else None
    got = _levels_or_decline(ni, pa, pq)
    if got is not None:
        expect.text(got["stl fdw"], LVL_STL_WANT,
                    "list in a nullable struct reads like pyarrow through the FDW")


def test_a_null_sub_struct_keeps_its_sibling_field(ni, expect):
    # The struct is NULL only when s is; a NULL inner is a NULL field b, not a NULL row.
    pa = expect.require_module("pyarrow", name="a NULL sub-struct keeps its sibling field")
    pq = expect.require_module("pyarrow.parquet",
                               name="a NULL sub-struct keeps its sibling field") if pa else None
    got = _levels_or_decline(ni, pa, pq)
    if got is not None:
        expect.text(got["sub read"], "0:(1,2) 1:(3,) 2:NULL 3:(,4)",
                    "a NULL sub-struct keeps its sibling field")


def test_a_composite_over_top_level_columns_is_refused(ni, expect):
    # No struct in the file stands for the composite, so its NULL cannot be placed.
    pa = expect.require_module("pyarrow", name="a composite over top-level columns is refused")
    pq = expect.require_module("pyarrow.parquet",
                               name="a composite over top-level columns is refused") if pa else None
    got = _levels_or_decline(ni, pa, pq)
    if got is not None:
        expect.text(got["flat refusal"], "refused: not inside one struct",
                    "a composite over top-level columns is refused")


def test_a_composite_finds_its_struct_when_the_deep_leaf_comes_first(ni, expect):
    # The composite stands for s, not inner, the first leaf's parent.
    pa = expect.require_module("pyarrow", name="a composite finds its struct when the deep leaf comes first")
    pq = expect.require_module("pyarrow.parquet",
                               name="a composite finds its struct when the deep leaf comes first") if pa else None
    got = _levels_or_decline(ni, pa, pq)
    if got is not None:
        expect.text(got["mirror read"], "0:(2,1) 1:(,3) 2:NULL 3:(4,)",
                    "a composite finds its struct when the deep leaf comes first")


def test_a_one_field_composite_is_null_only_when_its_struct_is(ni, expect):
    # It stands for s, the field's parent, so a NULL a reads (), not NULL.
    pa = expect.require_module("pyarrow", name="a one-field composite is NULL only when its struct is")
    pq = expect.require_module("pyarrow.parquet",
                               name="a one-field composite is NULL only when its struct is") if pa else None
    got = _levels_or_decline(ni, pa, pq)
    if got is not None:
        expect.text(got["one read"], "0:() 1:NULL 2:(7)",
                    "a one-field composite is NULL only when its struct is")


def test_a_one_field_composite_over_a_top_level_column_is_refused(ni, expect):
    # One top-level column sits in no struct, so its NULL cannot be placed.
    pa = expect.require_module("pyarrow", name="a one-field composite over a top-level column is refused")
    pq = expect.require_module("pyarrow.parquet",
                               name="a one-field composite over a top-level column is refused") if pa else None
    got = _levels_or_decline(ni, pa, pq)
    if got is not None:
        expect.text(got["one refusal"], "refused: not inside one struct",
                    "a one-field composite over a top-level column is refused")


def test_a_list_two_structs_deep_is_null_under_a_null_outer_struct(ni, expect):
    # A NULL list or any NULL parent struct makes the list NULL.
    pa = expect.require_module("pyarrow", name="a list two structs deep is NULL under a NULL outer struct")
    pq = expect.require_module("pyarrow.parquet",
                               name="a list two structs deep is NULL under a NULL outer struct") if pa else None
    got = _levels_or_decline(ni, pa, pq)
    if got is not None:
        expect.text(got["deep read"], "0:NULL 1:NULL 2:NULL 3:{} 4:{1,NULL}",
                    "a list two structs deep is NULL under a NULL outer struct")


def test_a_composite_over_a_nested_struct_is_null_only_when_that_struct_is(ni, expect):
    # The composite stands for inner; it is NULL when inner or o is.
    pa = expect.require_module("pyarrow", name="a composite over a nested struct is NULL only when that struct is")
    pq = expect.require_module("pyarrow.parquet",
                               name="a composite over a nested struct is NULL only when that struct is") if pa else None
    got = _levels_or_decline(ni, pa, pq)
    if got is not None:
        expect.text(got["nest read"], "0:NULL:NULL 1:1:NULL 2:2:(,) 3:3:(5,q)",
                    "a composite over a nested struct is NULL only when that struct is")


def test_a_composite_over_a_struct_inside_a_struct_reads_like_pyarrow(ni, expect):
    # The sos.parquet shape: the composite stands for s.t.
    pa = expect.require_module("pyarrow", name="a composite over a struct inside a struct reads like pyarrow")
    pq = expect.require_module("pyarrow.parquet",
                               name="a composite over a struct inside a struct reads like pyarrow") if pa else None
    got = _levels_or_decline(ni, pa, pq)
    if got is not None:
        expect.text(got["sos read"], "(1,x) (2,y)",
                    "a composite over a struct inside a struct reads like pyarrow")


def test_a_composite_three_structs_deep_is_null_under_every_null_parent(ni, expect):
    # The composite stands for q; it is NULL when q, p or o is, so every def
    # below q's own def 3 is a NULL row, not only 0 and 2.
    pa = expect.require_module("pyarrow", name="a composite three structs deep is NULL under every NULL parent")
    pq = expect.require_module("pyarrow.parquet",
                               name="a composite three structs deep is NULL under every NULL parent") if pa else None
    got = _levels_or_decline(ni, pa, pq)
    if got is not None:
        expect.text(got["deep3 read"], "0:NULL 1:NULL 2:NULL 3:(,) 4:(1,2)",
                    "a composite three structs deep is NULL under every NULL parent")


def test_a_composite_over_two_top_level_structs_is_refused(ni, expect):
    # Leaves from s1 and s2 sit in no one struct, though neither is top-level.
    pa = expect.require_module("pyarrow", name="a composite over two top-level structs is refused")
    pq = expect.require_module("pyarrow.parquet",
                               name="a composite over two top-level structs is refused") if pa else None
    got = _levels_or_decline(ni, pa, pq)
    if got is not None:
        expect.text(got["two refusal"], "refused: not inside one struct",
                    "a composite over two top-level structs is refused")


def test_a_composite_over_a_struct_and_a_top_level_column_is_refused(ni, expect):
    # Leaves from s2 and the top-level b sit in no one struct.
    pa = expect.require_module("pyarrow", name="a composite over a struct and a top-level column is refused")
    pq = expect.require_module("pyarrow.parquet",
                               name="a composite over a struct and a top-level column is refused") if pa else None
    got = _levels_or_decline(ni, pa, pq)
    if got is not None:
        expect.text(got["struct column refusal"], "refused: not inside one struct",
                    "a composite over a struct and a top-level column is refused")


def test_a_required_struct_is_null_under_a_null_parent(ni, expect):
    # inner is REQUIRED, but o is not: a NULL o makes inner NULL, as it makes x.
    pa = expect.require_module("pyarrow", name="a REQUIRED struct is NULL under a NULL parent")
    pq = expect.require_module("pyarrow.parquet",
                               name="a REQUIRED struct is NULL under a NULL parent") if pa else None
    got = _levels_or_decline(ni, pa, pq)
    if got is not None:
        expect.text(got["reqin read"], "0:NULL:NULL 1:1:(,) 2:2:(5,q)",
                    "a REQUIRED struct is NULL under a NULL parent")


def test_a_composite_over_part_of_a_struct_is_null_only_when_that_struct_is(ni, expect):
    # x binds s.a and the composite binds s.b alone. It still stands for s.
    pa = expect.require_module("pyarrow", name="a composite over part of a struct is NULL only when that struct is")
    pq = expect.require_module("pyarrow.parquet",
                               name="a composite over part of a struct is NULL only when that struct is") if pa else None
    got = _levels_or_decline(ni, pa, pq)
    if got is not None:
        expect.text(got["part read"], "0:1:(2) 1:NULL:NULL 2:3:()",
                    "a composite over part of a struct is NULL only when that struct is")
