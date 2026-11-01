"""pgcolumnar.parquet_schema(path) reports a Parquet file's leaf columns with the
PostgreSQL type each maps to (Phase G, #388, #1341). The pytest twin of
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


def test_a_map_column_reports_by_name_as_not_supported(pgc_conn, server_dir, expect):
    name = "parquet_schema reports a map column by name as not supported (#1893)"
    pa = expect.require_module("pyarrow", name=name)
    if pa is None:
        return
    pq = expect.require_module("pyarrow.parquet", name=name)
    mapf = server_dir / "map.parquet"
    pq.write_table(pa.table({"m": pa.array([[("k", 1)]], pa.map_(pa.string(), pa.int64())),
                             "n": pa.array([1], pa.int32())}), str(mapf))
    mapf.chmod(0o644)
    with pgc_conn.cursor() as cur:
        expect.text(_one(cur, "SELECT string_agg(column_name || ':' || coalesce(data_type, 'NULL'), ',') "
                              "FROM pgcolumnar.parquet_schema(%s)", (str(mapf),)),
                    "m:map (not supported),n:integer",
                    "parquet_schema reports a map column by name as not supported (#1893)")
