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
