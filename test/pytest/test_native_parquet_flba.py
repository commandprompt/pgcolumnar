"""Parquet FIXED_LEN_BYTE_ARRAY reads: uuid and DECIMAL (Phase G, #1341). The pytest twin
of `test/native_parquet_flba.sh`, every check by the shell's name.

uuid as a 16-byte FLBA, and DECIMAL as big-endian two's complement at the column's
scale, fixed (FLBA) or variable (BYTE_ARRAY) width, or as a little-endian INT32/INT64.
Our own export is read back through read_parquet, import_parquet and the FDW against
the source table; pyarrow-written files are the foreign producer. A footer whose
DECIMAL scale has been patched out of range must be refused at bind, not decoded.
"""
import decimal
import uuid

import psycopg

HASH = ("SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), $e$EMPTY$e$) "
        "FROM (SELECT _row::text AS t FROM ({}) _row) _s")
SHAPE = "t(id int, u uuid, d numeric(12,4), dn numeric(12,4), fb bytea)"


def _hash(cur, sql):
    """-> the shell's pgc_set_hash of `sql`, or the error: two failing queries never agree."""
    try:
        cur.execute(HASH.format(sql))
        return cur.fetchone()[0]
    except psycopg.Error as exc:
        return f"error: {exc.diag.message_primary} [{sql}]"


def _one(cur, sql):
    try:
        cur.execute(sql)
        return str(cur.fetchone()[0])
    except psycopg.Error as exc:
        return f"error: {exc.diag.message_primary}"


def _patch_scale(path):
    """Rewrite the DECIMAL leaf's scale (SchemaElement field 7) from 2 to 50 in place:
    zigzag(2)=0x04 and zigzag(50)=0x64 are one byte each, so the footer stays sound."""
    raw = bytearray(path.read_bytes())
    flen = int.from_bytes(raw[-8:-4], "little")
    pos = [len(raw) - 8 - flen]

    def varint():
        sh = out = 0
        while True:
            b = raw[pos[0]]
            pos[0] += 1
            out |= (b & 0x7F) << sh
            if not b & 0x80:
                return out
            sh += 7

    def zigzag():
        u = varint()
        return (u >> 1) ^ -(u & 1)

    def field(last):
        b = raw[pos[0]]
        pos[0] += 1
        if b == 0:
            return 0, 0, last
        t, d = b & 0x0F, b >> 4
        fid = last + d if d else zigzag()
        return t, fid, fid

    def skip(t):
        if t in (1, 2):
            return
        if t == 3:
            pos[0] += 1
        elif t in (4, 5, 6):
            zigzag()
        elif t == 7:
            pos[0] += 8
        elif t == 8:
            blen = varint()
            pos[0] += blen
        elif t in (9, 10):
            b = raw[pos[0]]
            pos[0] += 1
            n, et = (b >> 4) & 0x0F, b & 0x0F
            if n == 0x0F:
                n = varint()
            for _ in range(n):
                skip(et)
        elif t == 12:
            last = 0
            while True:
                ft, _f, last = field(last)
                if ft == 0:
                    break
                skip(ft)

    last = 0
    while True:
        ft, fid, last = field(last)
        if ft == 0:
            break
        if fid == 2 and ft in (9, 10):
            b = raw[pos[0]]
            pos[0] += 1
            n, et = (b >> 4) & 0x0F, b & 0x0F
            if n == 0x0F:
                n = varint()
            for _ in range(n):
                elast = 0
                while True:
                    eft, efid, elast = field(elast)
                    if eft == 0:
                        break
                    if efid == 7 and eft in (5, 6, 4):
                        assert raw[pos[0]] == 0x04, hex(raw[pos[0]])
                        raw[pos[0]] = 0x64
                        path.write_bytes(bytes(raw))
                        return
                    skip(eft)
            break
        skip(ft)
    raise AssertionError("did not find a scale field to patch")


def test_our_export_reads_back_through_every_surface(pgc_conn, server_dir, expect):
    exp = server_dir / "roundtrip.parquet"
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE SERVER IF NOT EXISTS pq_flba FOREIGN DATA WRAPPER pgcolumnar_parquet")
        cur.execute("CREATE TABLE src (id int, u uuid, d numeric(12,4), dn numeric(12,4), fb bytea) USING pgcolumnar")
        cur.execute("""INSERT INTO src VALUES
            (1, '11111111-2222-3333-4444-555555555555', 123.4500, -0.0001, '\\xdeadbeef'),
            (2, 'ffffffff-ffff-ffff-ffff-ffffffffffff', -9999.9999, 0, '\\x00'),
            (3, '00000000-0000-0000-0000-000000000000', 0.0000, 42.0, NULL)""")
        cur.execute(f"SELECT pgcolumnar.export_parquet('src'::regclass, '{exp}')")
        rp = f"pgcolumnar.read_parquet('{exp}') AS {SHAPE}"
        expect.text(_hash(cur, f"SELECT id, u FROM {rp}"), _hash(cur, "SELECT id, u FROM src"),
                    "uuid round-trips through export/read")
        expect.text(_hash(cur, f"SELECT id, d, dn FROM {rp}"), _hash(cur, "SELECT id, d, dn FROM src"),
                    "numeric round-trips through export/read")
        expect.text(_hash(cur, f"SELECT * FROM {rp}"), _hash(cur, "SELECT * FROM src"), "whole-row round-trip == source")
        # import_parquet shares the decoder
        cur.execute("CREATE TABLE imp (id int, u uuid, d numeric(12,4), dn numeric(12,4), fb bytea)")
        cur.execute(f"SELECT pgcolumnar.import_parquet('imp'::regclass, '{exp}')")
        expect.text(_hash(cur, "SELECT * FROM imp"), _hash(cur, "SELECT * FROM src"), "import_parquet == source")
        cur.execute(f"CREATE FOREIGN TABLE ft (id int, u uuid, d numeric(12,4), dn numeric(12,4), fb bytea) "
                    f"SERVER pq_flba OPTIONS (path '{exp}')")
        expect.text(_hash(cur, "SELECT * FROM ft"), _hash(cur, "SELECT * FROM src"), "FDW scan == source")


def test_pyarrow_written_flba_reads_exactly(pgc_conn, server_dir, expect):
    pa = expect.require_module("pyarrow", name="pyarrow uuid reads as uuid")
    if pa is None:
        return
    pq = expect.require_module("pyarrow.parquet", name="pyarrow uuid reads as uuid")
    us = [uuid.UUID("11111111-2222-3333-4444-555555555555").bytes, uuid.UUID(int=0).bytes]
    pq.write_table(pa.table({"u": pa.array(us, pa.binary(16))}), str(server_dir / "pa_uuid.parquet"))
    ds = [decimal.Decimal("123.45"), decimal.Decimal("-9999.99"), decimal.Decimal("0.01"), decimal.Decimal("0.00")]
    pq.write_table(pa.table({"d": pa.array(ds, pa.decimal128(10, 2))}), str(server_dir / "pa_dec.parquet"))
    bad = server_dir / "dec_badscale.parquet"
    pq.write_table(pa.table({"d": pa.array([decimal.Decimal("1.23")], pa.decimal128(10, 2))}), str(bad))
    _patch_scale(bad)
    for f in ("pa_uuid.parquet", "pa_dec.parquet", "dec_badscale.parquet"):
        (server_dir / f).chmod(0o644)
    with pgc_conn.cursor() as cur:
        # both values in order: the all-zero uuid alone is the same in every byte order
        expect.text(_one(cur, f"SELECT string_agg(u::text, ',' ORDER BY u) "
                              f"FROM pgcolumnar.read_parquet('{server_dir}/pa_uuid.parquet') AS t(u uuid)"),
                    "00000000-0000-0000-0000-000000000000,11111111-2222-3333-4444-555555555555",
                    "pyarrow uuid reads as uuid")
        expect.text(_hash(cur, f"SELECT d FROM pgcolumnar.read_parquet('{server_dir}/pa_dec.parquet') AS t(d numeric(10,2))"),
                    _hash(cur, "SELECT * FROM (VALUES (123.45::numeric(10,2)),(-9999.99),(0.01),(0.00)) v(d)"),
                    "pyarrow decimal128 values are exact")
        # scale 50: the bind must refuse it (42804); the decoder's own check refusing it
        # instead (XX001) means decoding began
        try:
            cur.execute(f"SELECT * FROM pgcolumnar.read_parquet('{bad}') AS t(d numeric)")
            cur.fetchall()
            verdict = "NO ERROR"
        except psycopg.Error as exc:
            verdict = f"REJECTED {exc.sqlstate}"
        expect.text(verdict, "REJECTED 42804", "crafted out-of-range scale is rejected, not decoded")
        expect.text(_one(cur, "SELECT 1"), "1", "backend survived the crafted scale")


def test_a_decimal_held_in_an_integer_reads_as_numeric(pgc_conn, server_dir, expect):
    pa = expect.require_module("pyarrow", name="INT32-backed DECIMAL reads")
    if pa is None:
        return
    pq = expect.require_module("pyarrow.parquet", name="INT32-backed DECIMAL reads")
    vals = [decimal.Decimal("1.25"), decimal.Decimal("-3.50"), decimal.Decimal("0.00"), None]
    for name, p, s in (("i32", 9, 2), ("i64", 18, 6)):
        path = server_dir / f"dec_{name}.parquet"
        pq.write_table(pa.table({"d": pa.array(vals, pa.decimal128(p, s))}), str(path),
                       store_decimal_as_integer=True, compression="none")
        path.chmod(0o644)
    phys = {n: pq.ParquetFile(str(server_dir / f"dec_{n}.parquet")).metadata.row_group(0).column(0).physical_type
            for n in ("i32", "i64")}
    assert phys == {"i32": "INT32", "i64": "INT64"}, phys
    i32, i64 = server_dir / "dec_i32.parquet", server_dir / "dec_i64.parquet"
    with pgc_conn.cursor() as cur:
        agg = "SELECT string_agg(d::text, ',' ORDER BY d) FROM pgcolumnar.read_parquet('{}') AS t(d {})"
        expect.text(_one(cur, agg.format(i32, "numeric")), "-3.50,0.00,1.25", "INT32-backed DECIMAL reads")
        expect.text(_one(cur, agg.format(i64, "numeric")), "-3.500000,0.000000,1.250000", "INT64-backed DECIMAL reads")
        expect.text(_one(cur, f"SELECT count(*) - count(d) FROM pgcolumnar.read_parquet('{i32}') AS t(d numeric)"), "1",
                    "INT32-backed DECIMAL keeps its null")
        expect.text(_one(cur, f"SELECT data_type FROM pgcolumnar.parquet_schema('{i32}')"), "numeric(9,2)",
                    "parquet_schema advises numeric for an INT32 DECIMAL")
        expect.text(_one(cur, f"SELECT data_type FROM pgcolumnar.parquet_schema('{i64}')"), "numeric(18,6)",
                    "parquet_schema advises numeric for an INT64 DECIMAL")
        # the integer reading is not silently reinterpreted: bigint binds to the raw integer
        expect.text(_one(cur, agg.format(i64, "bigint")), "-3500000,0,1250000",
                    "an INT64 DECIMAL still binds to bigint as the unscaled integer")
