"""pgcolumnar.parquet_schema(path) reports a Parquet file's columns, one row per
column read_parquet declares (#1893), with the PostgreSQL type each maps to (Phase G, #388, #1341). The pytest twin of
`test/native_parquet_schema.sh`, every check by the shell's name.

The round trip: a columnar table covering every type the exporter emits is exported,
and parquet_schema must report each source type back, every column nullable (the
exporter writes OPTIONAL only). A file that is not Parquet is refused by the magic
check itself: "any error" was the old arm, and with the magic check gone the same file
is still refused, as a corrupt footer. pyarrow, the independent writer, supplies what
the exporter cannot: a REQUIRED column (nullable=f), and field ids out of order and
disjoint from positions, with absence reported as NULL, since 0 is a legal id.
"""
import pathlib
import subprocess
import sys

import psycopg

from pgc_vacuity import QUERY_ERROR, query_error


def _one(cur, sql, params=None):
    cur.execute(sql, params)
    return cur.fetchone()[0]


def _schema(cur, path, col, name):
    return str(_one(cur, f"SELECT {col} FROM pgcolumnar.parquet_schema(%s) WHERE column_name = %s", (path, name)))


def test_the_exported_schema_round_trips(pgc_conn, server_dir, expect):
    # every type the exporter emits, with the type parquet_schema must report back
    types = (("c_i2", "smallint"), ("c_i4", "integer"), ("c_i8", "bigint"), ("c_f4", "real"),
             ("c_f8", "double precision"), ("c_bool", "boolean"), ("c_text", "text"), ("c_bytea", "bytea"),
             ("c_date", "date"), ("c_time", "time without time zone"), ("c_ts", "timestamp without time zone"),
             ("c_uuid", "uuid"), ("c_num", "numeric(10,2)"))
    path = str(server_dir / "schema.parquet")
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE n (c_i2 int2, c_i4 int4, c_i8 int8, c_f4 float4, c_f8 float8, c_bool bool, "
                    "c_text text, c_bytea bytea, c_date date, c_time time, c_ts timestamp, c_uuid uuid, "
                    "c_num numeric(10,2)) USING pgcolumnar")
        cur.execute("INSERT INTO n VALUES (2::int2, 3, 4::int8, 1.5::float4, 2.5::float8, true, 'hi', "
                    "'\\xdeadbeef'::bytea, '2020-01-01'::date, '12:34:56'::time, '2020-01-01 12:34:56'::timestamp, "
                    "'11111111-1111-1111-1111-111111111111'::uuid, 123.45::numeric(10,2)), "
                    "(NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)")
        expect.num(_one(cur, "SELECT pgcolumnar.export_parquet('n', %s)", (path,)), 2, "export_parquet rows written")
        expect.num(_one(cur, "SELECT count(*) FROM pgcolumnar.parquet_schema(%s)", (path,)), 13,
                   "parquet_schema column count")
        for name, want in types:
            expect.text(_schema(cur, path, "data_type", name), want, f"type of {name}")
            expect.text(_schema(cur, path, "nullable", name), "True", f"nullable of {name}")

        notpq = server_dir / "not.parquet"
        notpq.write_text("this is not a parquet file at all")
        notpq.chmod(0o644)
        try:
            cur.execute("SELECT count(*) FROM pgcolumnar.parquet_schema(%s)", (str(notpq),))
            err = f"no error ({cur.fetchone()[0]})"
        except psycopg.Error as exc:
            err = exc.diag.message_primary or str(exc)
        # by the magic check, not by whatever fails next
        expect.text("yes" if "is not a Parquet file (bad magic)" in err else f"no ({err})", "yes",
                    "bad magic is rejected by the magic check")


def test_required_columns_and_field_ids(pgc_conn, server_dir, expect):
    pa = expect.require_module("pyarrow", name="REQUIRED column reports nullable=f")
    if pa is None:
        return
    pq = expect.require_module("pyarrow.parquet", name="REQUIRED column reports nullable=f")

    req = server_dir / "required.parquet"
    schema = pa.schema([pa.field("req_i", pa.int32(), nullable=False), pa.field("opt_s", pa.string(), nullable=True)])
    pq.write_table(pa.table({"req_i": pa.array([1, 2, 3], pa.int32()),
                             "opt_s": pa.array(["a", None, "c"], pa.string())}, schema=schema), str(req))
    fid = server_dir / "fieldids.parquet"
    schema = pa.schema([pa.field("alpha", pa.int32(), metadata={b"PARQUET:field_id": b"7"}),
                        pa.field("beta", pa.string(), metadata={b"PARQUET:field_id": b"3"}),
                        pa.field("gamma", pa.float64(), metadata={b"PARQUET:field_id": b"12"})])
    pq.write_table(pa.table({"alpha": pa.array([1], pa.int32()), "beta": pa.array(["x"], pa.string()),
                             "gamma": pa.array([1.5], pa.float64())}, schema=schema), str(fid))
    for p in (req, fid):
        p.chmod(0o644)
    with pgc_conn.cursor() as cur:
        expect.text(_schema(cur, str(req), "nullable", "req_i"), "False", "REQUIRED column reports nullable=f")
        expect.text(_schema(cur, str(req), "nullable", "opt_s"), "True", "OPTIONAL column reports nullable=t")
        expect.text(_schema(cur, str(req), "data_type", "req_i"), "integer", "REQUIRED int32 maps to integer")
        expect.text("yes" if pathlib.Path(fid).stat().st_size > 0 else "no", "yes",
                    "premise: pyarrow wrote the field-id file")
        # ids out of order and disjoint from positions, so a reader inventing them from position cannot pass
        expect.text(_one(cur, "SELECT string_agg(field_id::text, ',' ORDER BY ord) FROM (SELECT field_id, "
                              "row_number() OVER () AS ord FROM pgcolumnar.parquet_schema(%s)) s", (str(fid),)),
                    "7,3,12", "field ids report as written, in schema order")
        expect.text(_one(cur, "SELECT count(*) FILTER (WHERE field_id IS NULL) || '/' || count(*) "
                              "FROM pgcolumnar.parquet_schema(%s)", (str(req),)),
                    "2/2", "a file without field ids reports NULL, not zero")


# ---- timestamp vs timestamptz: the UTC flag the exporter writes (#1706) -------------
# A timestamp is a wall time, a timestamptz an instant; Parquet says which through
# TIMESTAMP's isAdjustedToUTC. Both used to leave as TIMESTAMP_MICROS (adjusted), so
# other readers took a wall time for a UTC instant, while pgcolumnar's own import,
# which ignores the flag, round-tripped either way. pyarrow is the independent reader.

def _tsflag_export(pgc_conn, server_dir):
    """-> the exported file's path, or a query_error() when the export failed."""
    path = str(server_dir / "tsflag.parquet")
    try:
        with pgc_conn.cursor() as cur:
            cur.execute("CREATE TABLE tsf (ts timestamp, tz timestamptz) USING pgcolumnar")
            cur.execute("INSERT INTO tsf VALUES ('2024-06-01 12:00:00', '2024-06-01 12:00:00-04')")
            cur.execute("SELECT pgcolumnar.export_parquet('tsf', %s)", (path,))
    except psycopg.Error as exc:
        return query_error(exc.diag.message_primary or type(exc).__name__)
    return path


def _tsflag_read(pq, path, field, what):
    """-> the field's tz ("none" when it has none) or its first value, as text."""
    if not isinstance(path, str) or path.startswith(QUERY_ERROR):
        return path
    try:
        col = pq.read_table(path).column(field)
    except (OSError, ValueError, KeyError) as exc:  # a missing file, ArrowInvalid, a missing column
        return query_error(f"pyarrow could not read {field}: {type(exc).__name__}")
    if what == "tz":
        return col.type.tz if getattr(col.type, "tz", None) else "none"
    return str(col[0].as_py())


def test_read_parquet_returns_exported_timestamps_unchanged(pgc_conn, server_dir, expect):
    path = _tsflag_export(pgc_conn, server_dir)
    got = path
    if not path.startswith(QUERY_ERROR):
        try:
            with pgc_conn.cursor() as cur:
                cur.execute("SET timezone = 'America/New_York'")
                got = _one(cur, "SELECT ts || '|' || tz FROM pgcolumnar.read_parquet(%s) "
                                "AS x(ts timestamp, tz timestamptz)", (path,))
        except psycopg.Error as exc:
            got = query_error(exc.diag.message_primary or type(exc).__name__)
    expect.text(got, "2024-06-01 12:00:00|2024-06-01 12:00:00-04",
                "read_parquet returns the exported timestamp and timestamptz unchanged")


def test_an_exported_timestamp_is_not_utc_adjusted(pgc_conn, server_dir, expect):
    pq = expect.require_module("pyarrow.parquet",
                               name="an exported timestamp column is not UTC-adjusted (pyarrow reads no time zone)")
    if pq is None:
        return
    expect.text(_tsflag_read(pq, _tsflag_export(pgc_conn, server_dir), "ts", "tz"), "none",
                "an exported timestamp column is not UTC-adjusted (pyarrow reads no time zone)")


def test_an_exported_timestamptz_is_utc_adjusted(pgc_conn, server_dir, expect):
    pq = expect.require_module("pyarrow.parquet",
                               name="an exported timestamptz column is UTC-adjusted (pyarrow reads UTC)")
    if pq is None:
        return
    expect.text(_tsflag_read(pq, _tsflag_export(pgc_conn, server_dir), "tz", "tz"), "UTC",
                "an exported timestamptz column is UTC-adjusted (pyarrow reads UTC)")


def test_pyarrow_reads_an_exported_timestamp_as_its_wall_time(pgc_conn, server_dir, expect):
    pq = expect.require_module("pyarrow.parquet", name="pyarrow reads an exported timestamp back as its wall time")
    if pq is None:
        return
    expect.text(_tsflag_read(pq, _tsflag_export(pgc_conn, server_dir), "ts", "value"), "2024-06-01 12:00:00",
                "pyarrow reads an exported timestamp back as its wall time")


def test_pyarrow_reads_an_exported_timestamptz_as_its_instant(pgc_conn, server_dir, expect):
    pq = expect.require_module("pyarrow.parquet", name="pyarrow reads an exported timestamptz back as its instant")
    if pq is None:
        return
    expect.text(_tsflag_read(pq, _tsflag_export(pgc_conn, server_dir), "tz", "value"), "2024-06-01 16:00:00+00:00",
                "pyarrow reads an exported timestamptz back as its instant")


# ---- nested columns report by their top-level names (#1893) -------------------------
# parquet_schema used to list a nested column by its leaf names ("element", "a", "b").
# Each top-level column now gets one row, with the type read_parquet reads it as. The
# exporter writes a list and a struct, so only the map arm needs pyarrow.

def _nested_export(cur, server_dir):
    path = str(server_dir / "nested.parquet")
    cur.execute("CREATE TYPE nest_s AS (a int8, b text)")
    cur.execute("CREATE TABLE nest (l int8[], s nest_s, n int4) USING pgcolumnar")
    cur.execute("INSERT INTO nest VALUES ('{1,2}', ROW(1, 'x'), 1), ('{3}', ROW(2, 'y'), 2)")
    cur.execute("SELECT pgcolumnar.export_parquet('nest', %s)", (path,))
    return path


def test_nested_columns_report_by_top_level_name(pgc_conn, server_dir, expect):
    with pgc_conn.cursor() as cur:
        path = _nested_export(cur, server_dir)
        expect.text(_one(cur, "SELECT string_agg(column_name, ',' ORDER BY ord) FROM (SELECT column_name, "
                              "row_number() OVER () AS ord FROM pgcolumnar.parquet_schema(%s)) s", (path,)),
                    "l,s,n", "parquet_schema lists a nested file by its top-level column names (#1893)")
        expect.text(_schema(cur, path, "data_type", "l"), "bigint[]",
                    "parquet_schema reports a list column as an array type (#1893)")
        expect.text(_schema(cur, path, "data_type", "s"), "composite (a bigint, b text)",
                    "parquet_schema reports a struct column as a composite with its field types (#1893)")
        expect.text(_one(cur, "SELECT string_agg(l::text || (s).a || (s).b || n, ';' ORDER BY n) "
                              "FROM pgcolumnar.read_parquet(%s) AS t(l bigint[], s nest_s, n integer)", (path,)),
                    "{1,2}1x1;{3}2y2", "read_parquet reads a nested file with the reported names and types (#1893)")


# ---- nested shapes read_parquet takes leaf by leaf (#1893 review) ---------------------
# read_parquet binds every leaf by position, so a map, a list of structs, a struct holding
# a list or a struct of a struct reads as one declared column per leaf, an array where
# the leaf repeats. parquet_schema lists such a column one row per leaf, named by its path
# in the file. A leaf under two repeated levels has no faithful type and reports NULL.
# test/craft_parquet_nested.py, the shell's own writer, writes the files.
CRAFTER = pathlib.Path(__file__).resolve().parent.parent / "craft_parquet_nested.py"


def _crafted(server_dir, expect, name):
    if expect.require_module("pyarrow.parquet", name=name) is None:
        return None
    done = subprocess.run([sys.executable, "-I", str(CRAFTER), str(server_dir)], capture_output=True, text=True)
    if done.returncode != 0:
        expect.text(done.stderr.strip()[-300:] or f"exit {done.returncode}", "crafted", name)
        return None
    return server_dir


def _schema_rows(cur, path):
    return _one(cur, "SELECT string_agg(column_name || ':' || coalesce(data_type, 'NULL'), ',' ORDER BY ord) "
                     "FROM (SELECT column_name, data_type, row_number() OVER () AS ord "
                     "FROM pgcolumnar.parquet_schema(%s)) s", (path,))


def _shape_rows(pgc_conn, w, f):
    with pgc_conn.cursor() as cur:
        return _schema_rows(cur, str(w / f"{f}.parquet"))


def test_a_map_column_lists_one_row_per_leaf(pgc_conn, server_dir, expect):
    w = _crafted(server_dir, expect, "parquet_schema lists a map column one row per leaf, each an array (#1893)")
    if w is not None:
        expect.text(_shape_rows(pgc_conn, w, "map"), "m.key_value.key:text[],m.key_value.value:bigint[],n:integer",
                    "parquet_schema lists a map column one row per leaf, each an array (#1893)")


def test_a_list_of_a_two_field_struct_lists_one_row_per_leaf(pgc_conn, server_dir, expect):
    w = _crafted(server_dir, expect, "parquet_schema lists a list of a two-field struct one row per leaf (#1893)")
    if w is not None:
        expect.text(_shape_rows(pgc_conn, w, "los"), "l.list.element.x:bigint[],l.list.element.y:text[],n:integer",
                    "parquet_schema lists a list of a two-field struct one row per leaf (#1893)")


def test_a_list_of_a_one_field_struct_lists_by_its_leaf_path(pgc_conn, server_dir, expect):
    w = _crafted(server_dir, expect, "parquet_schema lists a list of a one-field struct by its leaf path (#1893)")
    if w is not None:
        expect.text(_shape_rows(pgc_conn, w, "lofs"), "l.list.element.x:bigint[],n:integer",
                    "parquet_schema lists a list of a one-field struct by its leaf path (#1893)")


def test_a_struct_holding_a_list_lists_by_its_leaf_path(pgc_conn, server_dir, expect):
    w = _crafted(server_dir, expect, "parquet_schema lists a struct holding a list by its leaf path (#1893)")
    if w is not None:
        expect.text(_shape_rows(pgc_conn, w, "shl"), "s.x.list.element:bigint[],n:integer",
                    "parquet_schema lists a struct holding a list by its leaf path (#1893)")


def test_a_struct_of_a_struct_lists_one_scalar_row_per_leaf(pgc_conn, server_dir, expect):
    w = _crafted(server_dir, expect, "parquet_schema lists a struct of a struct one scalar row per leaf (#1893)")
    if w is not None:
        expect.text(_shape_rows(pgc_conn, w, "sos"), "s.t.a:bigint,s.t.b:text,n:integer",
                    "parquet_schema lists a struct of a struct one scalar row per leaf (#1893)")


def test_a_struct_holding_a_map_lists_one_row_per_leaf(pgc_conn, server_dir, expect):
    w = _crafted(server_dir, expect, "parquet_schema lists a struct holding a map one row per leaf (#1893)")
    if w is not None:
        expect.text(_shape_rows(pgc_conn, w, "smap"), "s.m.key_value.key:text[],s.m.key_value.value:bigint[],n:integer",
                    "parquet_schema lists a struct holding a map one row per leaf (#1893)")


def test_a_list_of_maps_reports_null_for_its_leaves(pgc_conn, server_dir, expect):
    w = _crafted(server_dir, expect, "parquet_schema reports NULL for the leaves of a list of maps, repeated twice (#1893)")
    if w is not None:
        expect.text(_shape_rows(pgc_conn, w, "lmap"), "l.list.element.key_value.key:NULL,l.list.element.key_value.value:NULL,n:integer",
                    "parquet_schema reports NULL for the leaves of a list of maps, repeated twice (#1893)")


def test_a_list_of_lists_reports_null_for_its_leaf(pgc_conn, server_dir, expect):
    w = _crafted(server_dir, expect, "parquet_schema reports NULL for the leaf of a list of lists, repeated twice (#1893)")
    if w is not None:
        expect.text(_shape_rows(pgc_conn, w, "lol"), "l.list.element.list.element:NULL,n:integer",
                    "parquet_schema reports NULL for the leaf of a list of lists, repeated twice (#1893)")


def test_a_legacy_two_level_list_reports_by_its_name_as_an_array(pgc_conn, server_dir, expect):
    w = _crafted(server_dir, expect, "parquet_schema reports a legacy two-level list by its name as an array (#1893)")
    if w is not None:
        expect.text(_shape_rows(pgc_conn, w, "twolevel"), "l:bigint[],n:integer",
                    "parquet_schema reports a legacy two-level list by its name as an array (#1893)")


def test_read_parquet_reads_each_nested_file_with_the_reported_column_list(pgc_conn, server_dir, expect):
    w = _crafted(server_dir, expect,
                 "read_parquet reads each nested file with the column list parquet_schema reports (#1893)")
    if w is None:
        return
    got = []
    with pgc_conn.cursor() as cur:
        # not twolevel: read_parquet reads its REQUIRED repeated leaf as empty arrays, an
        # older defect of the reader, not of parquet_schema (#1916)
        for f in ("map", "los", "lofs", "shl", "sos", "smap"):
            path = str(w / f"{f}.parquet")
            cols = _one(cur, "SELECT string_agg(quote_ident(column_name) || ' ' || data_type, ', ' ORDER BY ord) "
                             "FROM (SELECT column_name, data_type, row_number() OVER () AS ord "
                             "FROM pgcolumnar.parquet_schema(%s)) s", (path,))
            try:
                got.append(f"{f}=" + str(_one(cur, f"SELECT string_agg(t::text, ';') FROM "
                                                   f"pgcolumnar.read_parquet(%s) AS t({cols})", (path,))))
            except psycopg.Error as exc:
                got.append(f"{f}=" + (exc.diag.message_primary or str(exc)))
    expect.text(" ".join(got),
                'map=("{k,j}","{1,2}",1);({z},{3},2) los=({1},{a},1);("{2,3}","{b,c}",2) lofs=("{1,2}",1);({3},2) '
                'shl=("{1,2}",1);({3},2) sos=(1,x,1);(2,y,2) smap=({k},{1},1);({z},{3},2)',
                "read_parquet reads each nested file with the column list parquet_schema reports (#1893)")


# A leaf under two repeated levels read into a one-dimensional array used to flatten:
# [[1], [2, 3]] read as {1,2,3}, with no error (#1917). The bind now refuses it with
# 42804. The reading is the SQLSTATE and message, or the rows that came back instead.
def _nested_refusal(pgc_conn, sql, params):
    try:
        with pgc_conn.cursor() as cur:
            return "read: " + str(_one(cur, sql, params))
    except psycopg.Error as exc:
        return f"{exc.diag.sqlstate} {exc.diag.message_primary}"


def test_read_parquet_refuses_a_list_of_lists_as_a_one_dimensional_array(pgc_conn, server_dir, expect):
    w = _crafted(server_dir, expect, "read_parquet refuses a list of lists declared as a one-dimensional array (#1917)")
    if w is None:
        return
    expect.text(_nested_refusal(pgc_conn, "SELECT string_agg(t::text, ';') FROM "
                                          "pgcolumnar.read_parquet(%s) AS t(l bigint[], n int)",
                                (str(w / "lol.parquet"),)),
                '42804 Parquet column for array "l" is nested 2 lists deep, '
                'but pgcolumnar.read_parquet reads one list level into an array',
                "read_parquet refuses a list of lists declared as a one-dimensional array (#1917)")


def test_read_parquet_refuses_a_list_of_maps_as_one_dimensional_arrays(pgc_conn, server_dir, expect):
    w = _crafted(server_dir, expect, "read_parquet refuses a list of maps declared as one-dimensional arrays (#1917)")
    if w is None:
        return
    expect.text(_nested_refusal(pgc_conn, "SELECT string_agg(t::text, ';') FROM "
                                          "pgcolumnar.read_parquet(%s) AS t(k text[], v bigint[], n int)",
                                (str(w / "lmap.parquet"),)),
                '42804 Parquet column for array "k" is nested 2 lists deep, '
                'but pgcolumnar.read_parquet reads one list level into an array',
                "read_parquet refuses a list of maps declared as one-dimensional arrays (#1917)")


def test_import_parquet_refuses_a_list_of_lists_for_a_one_dimensional_array(pgc_conn, server_dir, expect):
    w = _crafted(server_dir, expect, "import_parquet refuses a list of lists for a one-dimensional array column (#1917)")
    if w is None:
        return
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE lol_imp (l bigint[], n int) USING pgcolumnar")
    got = _nested_refusal(pgc_conn, "SELECT pgcolumnar.import_parquet('lol_imp', %s)",
                          (str(w / "lol.parquet"),))
    with pgc_conn.cursor() as cur:
        rows = _one(cur, "SELECT count(*) FROM lol_imp")
    expect.text(f"{got}|{rows}",
                '42804 Parquet column for array "l" is nested 2 lists deep, '
                'but pgcolumnar.import_parquet reads one list level into an array|0',
                "import_parquet refuses a list of lists for a one-dimensional array column (#1917)")


def test_a_required_list_or_struct_is_not_nullable(pgc_conn, server_dir, expect):
    w = _crafted(server_dir, expect, "parquet_schema reports a REQUIRED list or struct as not nullable, "
                                     "an OPTIONAL list as nullable (#1893)")
    if w is None:
        return
    with pgc_conn.cursor() as cur:
        expect.text(_one(cur, "SELECT string_agg(column_name || ':' || nullable::text, ',' ORDER BY ord) "
                              "FROM (SELECT column_name, nullable, row_number() OVER () AS ord "
                              "FROM pgcolumnar.parquet_schema(%s)) s", (str(w / "req.parquet"),)),
                    "l:false,s:false,o:true",
                    "parquet_schema reports a REQUIRED list or struct as not nullable, an OPTIONAL list as nullable (#1893)")


def test_a_leaf_row_is_nullable_only_above_its_first_repeated_level(pgc_conn, server_dir, expect):
    # A null above the first repeated level leaves the row without a value; below it,
    # a null is one missing array element. In leafnull only o's leaves pass that test.
    w = _crafted(server_dir, expect, "parquet_schema reports a leaf row nullable only when an OPTIONAL element sits above its first repeated level (#1893)")
    if w is None:
        return
    with pgc_conn.cursor() as cur:
        expect.text(_one(cur, "SELECT string_agg(column_name || ':' || nullable::text, ',' ORDER BY ord) "
                              "FROM (SELECT column_name, nullable, row_number() OVER () AS ord "
                              "FROM pgcolumnar.parquet_schema(%s)) s", (str(w / "leafnull.parquet"),)),
                    "m.key_value.key:false,m.key_value.value:false,l.list.element.x:false,"
                    "o.key_value.key:true,o.key_value.value:true",
                    "parquet_schema reports a leaf row nullable only when an OPTIONAL element sits above its first repeated level (#1893)")


def test_a_struct_field_name_that_needs_quotes_comes_quoted(pgc_conn, server_dir, expect):
    # The composite text is the field list for CREATE TYPE: measure the text, the
    # CREATE TYPE built from it, and the read through that type.
    w = _crafted(server_dir, expect, "parquet_schema quotes a struct field name that needs quotes, so CREATE TYPE takes the composite text (#1893)")
    if w is None:
        return
    path = str(w / "qname.parquet")
    with pgc_conn.cursor() as cur:
        txt = _one(cur, "SELECT data_type FROM pgcolumnar.parquet_schema(%s) WHERE column_name = 's'", (path,))
    parts = [str(txt)]
    for sql in ("CREATE TYPE nest_q AS " + str(txt).removeprefix("composite "),
                "SELECT string_agg((s).\"Weird Name\" || '/' || n, ';' ORDER BY n) "
                "FROM pgcolumnar.read_parquet(%s) AS t(s nest_q, n integer)"):
        try:
            with pgc_conn.cursor() as cur:
                if sql.startswith("CREATE"):
                    cur.execute(sql)
                    parts.append("created")
                else:
                    parts.append(str(_one(cur, sql, (path,))))
        except psycopg.Error as exc:
            parts.append(exc.diag.message_primary or str(exc))
    expect.text("|".join(parts), 'composite ("Weird Name" smallint)|created|1/1;2/2',
                "parquet_schema quotes a struct field name that needs quotes, so CREATE TYPE takes the composite text (#1893)")
