"""pgcolumnar.import_arrow: the pytest twin of `test/arrow_import.sh` (#1341). Every
check carries the shell's name.

The reverse of export_arrow: a round trip through our own writer against a
differential oracle, files a foreign producer (pyarrow) wrote, the documented lossy
mappings, and the refusals. The four tests that need a pyarrow-written file decline
by name when pyarrow is absent, as their first outcome, and fail where PGC_REQUIRE
names it; the four that need no pyarrow always run, which is the shell's #1159
lesson (a suite that runs fewer checks reads exactly like one that has fewer).

Refusals are asserted by SQLSTATE, and where the shell does, by message too. Every
file lives in `server_dir`, because the backend opens it.
"""
import decimal
import os
import re
import struct
import threading

import pytest


def _one(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    return row[0] if row else None


def _run(conn, *sqls):
    with conn.cursor() as cur:
        for s in sqls:
            cur.execute(s)


def _state(conn, sql):
    """-> the SQLSTATE `sql` raised, or '00000'."""
    import psycopg
    try:
        _run(conn, sql)
        return "00000"
    except psycopg.Error as e:
        return e.sqlstate or "?"


def _refusal(conn, sql):
    """-> 'SQLSTATE message' of the error `sql` raised, or 'ok'."""
    import psycopg
    try:
        _run(conn, sql)
        return "ok"
    except psycopg.Error as e:
        return f"{e.sqlstate} {e.diag.message_primary}"


def _outcome(conn, sql):
    """-> the value `sql` returned, as text, or 'SQLSTATE message' of its error.

    The shell's ai_outcome: the check shows what the server did either way."""
    import psycopg
    try:
        return str(_one(conn, sql))
    except psycopg.Error as e:
        return f"{e.sqlstate} {e.diag.message_primary}"


def _notices(conn, sql):
    """-> the messages the server sent while running `sql`, errors included."""
    import psycopg
    got = []

    def keep(diag):
        got.append(diag.message_primary or "")

    conn.add_notice_handler(keep)
    try:
        _run(conn, sql)
    except psycopg.Error as e:
        got.append(e.diag.message_primary or "")
    finally:
        conn.remove_notice_handler(keep)
    return got


def _set_hash(conn, query):
    return _one(conn, "SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), 'EMPTY')"
                      f" FROM (SELECT _row::text AS t FROM ({query}) _row) _s")


def _write_stream(pa, path, table, options=None):
    with pa.ipc.new_stream(pa.OSFile(str(path), "wb"), table.schema, options=options) as w:
        w.write_table(table)


def test_a_round_trip_and_the_documented_lossy_mapping(pgc_conn, server_dir, expect):
    conn = pgc_conn
    arrow = server_dir / "rt.arrows"
    # 16 columns: the maximum export_arrow supports.
    _run(conn,
         "CREATE TABLE ri_src (id int, c int8, d float4, e float8, f bool, g text, h bytea, dt date,"
         " tm time, ts timestamp, tz timestamptz, u uuid, num numeric(20,4), numun numeric, j json,"
         " jb jsonb) USING pgcolumnar",
         "INSERT INTO ri_src SELECT s, s::int8*7, (s::float4/3), (s::float8/9), (s%2=0), 'row'||s,"
         " decode(lpad(to_hex(s),4,'0'),'hex'), DATE '2000-01-01' + s,"
         " TIME '00:00:00' + (s || ' seconds')::interval,"
         " TIMESTAMP '2001-01-01' + (s||' minutes')::interval,"
         " TIMESTAMPTZ '2001-01-01 00:00:00+00' + (s||' minutes')::interval,"
         " ('00000000-0000-0000-0000-' || lpad(to_hex(s),12,'0'))::uuid,"
         " (s::numeric/100)::numeric(20,4), (s::numeric/7), json_build_object('n', s),"
         " jsonb_build_object('n', s) FROM generate_series(1, 3000) s",
         # explicit NULLs and float NaN/Inf, which round trip exactly
         "INSERT INTO ri_src (id, d, e) VALUES (900001, NULL, NULL),"
         " (900002, 'NaN'::float4, 'Infinity'::float8), (900003, '-Infinity'::float4, 'NaN'::float8)")
    src_rows = _one(conn, "SELECT count(*) FROM ri_src")
    expect.num(_one(conn, f"SELECT pgcolumnar.export_arrow('ri_src', '{arrow}')"), src_rows, "export rows")
    _run(conn, "CREATE TABLE ri_dst (LIKE ri_src) USING pgcolumnar")
    expect.num(_one(conn, f"SELECT pgcolumnar.import_arrow('ri_dst', '{arrow}')"), src_rows, "import rows")
    expect.text(_set_hash(conn, "SELECT * FROM ri_dst"), _set_hash(conn, "SELECT * FROM ri_src"),
                "round trip identical")

    # non-finite date/timestamp and NaN numeric export as null
    lossy = server_dir / "lossy.arrows"
    _run(conn, "CREATE TABLE ri_nf (id int, dt date, ts timestamp, num numeric(10,2)) USING pgcolumnar",
         "INSERT INTO ri_nf VALUES (1, 'infinity', '-infinity', 'NaN'), (2, '2020-01-01', '2020-01-01 00:00', 1.25)",
         f"SELECT pgcolumnar.export_arrow('ri_nf', '{lossy}')",
         "CREATE TABLE ri_nf2 (LIKE ri_nf) USING pgcolumnar",
         f"SELECT pgcolumnar.import_arrow('ri_nf2', '{lossy}')")
    expect.num(_one(conn, "SELECT count(*) FROM ri_nf2 WHERE id=1 AND dt IS NULL AND ts IS NULL AND num IS NULL"),
               1, "non-finite imported as null")
    expect.num(_one(conn, "SELECT count(*) FROM ri_nf2 WHERE id=2 AND dt='2020-01-01' AND num=1.25"), 1,
               "finite row preserved")

    # argument validation, by SQLSTATE: 42809 wrong_object_type, 42804 datatype_mismatch
    _run(conn, "CREATE TABLE ri_heap (a bigint, b float8, c text) USING heap",
         "CREATE TABLE ri_wrong (a int) USING pgcolumnar")
    expect.text(_state(conn, f"SELECT pgcolumnar.import_arrow('ri_heap', '{arrow}')"), "42809",
                "reject non-columnar target")
    expect.text(_state(conn, f"SELECT pgcolumnar.import_arrow('ri_wrong', '{arrow}')"), "42804",
                "reject column-count mismatch")

    # What export_arrow writes (#1806): the IPC stream format, whose first word is
    # the 0xFFFFFFFF continuation marker, not the "ARROW1" of the IPC file format.
    expect.text(arrow.read_bytes()[:4].hex(), "ffffffff",
                "export_arrow writes the IPC stream format, not the ARROW1 file format (#1806)")

    # Lengths larger than the file are refused before they are allocated (#1806).
    bigmeta = server_dir / "bigmeta.arrows"
    bigmeta.write_bytes(struct.pack("<Ii", 0xFFFFFFFF, 1048576) + bytes(8))
    bigmeta.chmod(0o644)
    _run(conn, "CREATE TABLE ri_len (a int) USING pgcolumnar")
    expect.text(_refusal(conn, f"SELECT pgcolumnar.import_arrow('ri_len', '{bigmeta}')"),
                "XX001 columnar: malformed Arrow IPC file: metadata length 1048576 is larger than"
                " the 8 bytes left in the file",
                "a metadata length larger than the file is refused before it is allocated (#1806)")
    # a one-row export cut halfway through its only record-batch body
    one = server_dir / "one.arrows"
    _run(conn, "CREATE TABLE ri_one (a int) USING pgcolumnar", "INSERT INTO ri_one VALUES (7)",
         f"SELECT pgcolumnar.export_arrow('ri_one', '{one}')")
    d = one.read_bytes()
    p = 0
    for _ in range(2):  # the Schema message, then the RecordBatch metadata
        cont, mlen = struct.unpack_from("<Ii", d, p)
        assert cont == 0xFFFFFFFF, cont
        p += 8 + mlen
    body = len(d) - 8 - p
    bigbody = server_dir / "bigbody.arrows"
    bigbody.write_bytes(d[:p + body // 2])
    bigbody.chmod(0o644)
    _run(conn, "CREATE TABLE ri_body (a int) USING pgcolumnar")
    expect.text(_refusal(conn, f"SELECT pgcolumnar.import_arrow('ri_body', '{bigbody}')")
                + f"|{_one(conn, 'SELECT count(*) FROM ri_body')}",
                f"XX001 columnar: malformed Arrow IPC file: body length {body} is larger than"
                f" the {body // 2} bytes left in the file|0",
                "a body length larger than the rest of the file is refused before it is allocated (#1806)")
    # an IPC file whose footer length is larger than the file, or negative: the
    # one-row export wrapped in "ARROW1", 2 padding bytes, a 4-byte footer, the
    # footer length and "ARROW1"
    wrapped = b"ARROW1\0\0" + d + bytes(4)
    size = len(wrapped) + 10
    feet = []
    for name, flen in (("footlong.arrow", size), ("footneg.arrow", -1)):
        f = server_dir / name
        f.write_bytes(wrapped + struct.pack("<i", flen) + b"ARROW1")
        f.chmod(0o644)
        feet.append(f)
    _run(conn, "CREATE TABLE ri_foot (a int) USING pgcolumnar")
    expect.text(" / ".join(_outcome(conn, f"SELECT pgcolumnar.import_arrow('ri_foot', '{f}')") for f in feet)
                + f"|{_one(conn, 'SELECT count(*) FROM ri_foot')}",
                f"XX001 columnar: malformed Arrow IPC file: footer length {size} is out of range"
                " / XX001 columnar: malformed Arrow IPC file: footer length -1 is out of range|0",
                "an Arrow IPC file whose footer length is negative or larger than the file is refused as malformed"
                " (#1806)")


def test_a_file_pyarrow_wrote(pgc_conn, server_dir, expect):
    pa = expect.require_module("pyarrow", name="pyarrow import rows")
    if pa is None:
        return
    conn = pgc_conn
    pyf = server_dir / "foreign.arrows"
    _write_stream(pa, pyf, pa.table({
        "a": pa.array([1, 2, None, 4], pa.int64()),
        "b": pa.array([1.5, None, 3.5, 4.5], pa.float64()),
        "c": pa.array(["x", "y", "z", None], pa.string())}))
    _run(conn, "CREATE TABLE ri_for (a bigint, b float8, c text) USING pgcolumnar")
    expect.num(_one(conn, f"SELECT pgcolumnar.import_arrow('ri_for', '{pyf}')"), 4, "pyarrow import rows")
    expect.text(_one(conn, "SELECT string_agg(coalesce(a::text,'-')||'/'||coalesce(b::text,'-')||'/'||"
                           "coalesce(c,'-'), ',' ORDER BY a NULLS LAST) FROM ri_for"),
                "1/1.5/x,2/-/y,4/4.5/-,-/3.5/z", "pyarrow values")

    # The Arrow IPC FILE format (#1806), and Feather v2, which is the same format.
    tab = pa.table({
        "a": pa.array([1, 2, None, 4], pa.int64()),
        "b": pa.array([1.5, None, 3.5, 4.5], pa.float64()),
        "c": pa.array(["x", "y", "z", None], pa.string())})
    ipcfile = server_dir / "foreign.arrow"
    with pa.ipc.new_file(pa.OSFile(str(ipcfile), "wb"), tab.schema) as w:
        w.write_table(tab)

    def file_rows(table, path):
        """-> "<what import_arrow returned, or its error>|<values>", as the shell's ai_file_rows."""
        _run(conn, f"CREATE TABLE {table} (a bigint, b float8, c text) USING pgcolumnar")
        got = _outcome(conn, f"SELECT pgcolumnar.import_arrow('{table}', '{path}')")
        vals = _one(conn, "SELECT string_agg(coalesce(a::text,'-')||'/'||coalesce(b::text,'-')||'/'||"
                          f"coalesce(c,'-'), ',' ORDER BY a NULLS LAST) FROM {table}")
        return f"{got}|{vals or ''}"

    expect.text(file_rows("ri_ipcfile", ipcfile), "4|1/1.5/x,2/-/y,4/4.5/-,-/3.5/z",
                "an Arrow IPC file (ARROW1 magic) imports its rows (#1806)")
    nofooter = server_dir / "nofooter.arrow"
    nofooter.write_bytes(ipcfile.read_bytes()[:-10])
    nofooter.chmod(0o644)
    _run(conn, "CREATE TABLE ri_nofooter (a bigint, b float8, c text) USING pgcolumnar")
    expect.text(_refusal(conn, f"SELECT pgcolumnar.import_arrow('ri_nofooter', '{nofooter}')")
                + f"|{_one(conn, 'SELECT count(*) FROM ri_nofooter')}",
                "XX001 columnar: malformed Arrow IPC file: the file starts with ARROW1 but does not end with it|0",
                "an Arrow IPC file without its trailing magic is refused as malformed (#1806)")
    # the same file without the 8-byte end-of-stream marker before its footer; the
    # removed bytes lead the reading, so a builder that cut the wrong ones fails it
    raw = ipcfile.read_bytes()
    fstart = len(raw) - 10 - struct.unpack_from("<i", raw, len(raw) - 10)[0]
    noeos = server_dir / "noeos.arrow"
    noeos.write_bytes(raw[:fstart - 8] + raw[fstart:])
    noeos.chmod(0o644)
    expect.text(f"{raw[fstart - 8:fstart].hex()}|{file_rows('ri_noeos', noeos)}",
                "ffffffff00000000|4|1/1.5/x,2/-/y,4/4.5/-,-/3.5/z",
                "an Arrow IPC file without an end-of-stream marker before its footer imports its rows (#1806)")

    # Feather v2 last, so no check above depends on pyarrow.feather. It ships inside
    # pyarrow, which this test required first. A decline here would follow recorded
    # checks, which the harness refuses, so a missing feather fails the test.
    pafeather = expect.require_module("pyarrow.feather", name="a Feather v2 file imports its rows too (#1806)")
    if pafeather is None:
        return
    feather = server_dir / "foreign.feather"
    pafeather.write_feather(tab, str(feather), compression="uncompressed")
    expect.text(file_rows("ri_feather", feather), "4|1/1.5/x,2/-/y,4/4.5/-,-/3.5/z",
                "a Feather v2 file imports its rows too (#1806)")


def test_nanosecond_narrowing_is_reported_never_refused(pgc_conn, server_dir, expect):
    pa = expect.require_module("pyarrow", name="a lossy nanosecond file imports every row")
    if pa is None:
        return
    conn = pgc_conn
    s2000, noon = 946684800, 12 * 3600

    def ns(off):
        return pa.array([(s2000 + i) * 10**9 + off for i in range(4)], pa.timestamp("ns"))

    def us():
        return pa.array([(s2000 + i) * 10**6 for i in range(4)], pa.timestamp("us"))

    # TWO temporal columns over four rows, so VALUES that lose digits (8) differ from ROWS (4).
    _write_stream(pa, server_dir / "ns_lossy.arrows", pa.table({"ts": ns(123), "ts2": ns(123)}))
    _write_stream(pa, server_dir / "ns_exact.arrows", pa.table({"ts": ns(0), "ts2": ns(0)}))
    _write_stream(pa, server_dir / "us_plain.arrows", pa.table({"ts": us(), "ts2": us()}))
    # the time call site and the list recursion: 4 + 8 = 12 values, all 123ns past a boundary
    tm = pa.array([(noon + i) * 10**9 + 123 for i in range(4)], pa.time64("ns"))
    lst = pa.array([[(s2000 + i) * 10**9 + 123, (s2000 + i + 1) * 10**9 + 123] for i in range(4)],
                   pa.list_(pa.timestamp("ns")))
    _write_stream(pa, server_dir / "ns_deep.arrows", pa.table({"tm": tm, "lst": lst}))
    for t in ("ri_nsl", "ri_nse", "ri_usp"):
        _run(conn, f"CREATE TABLE {t} (ts timestamp, ts2 timestamp) USING pgcolumnar")
    nsl = _notices(conn, f"SELECT pgcolumnar.import_arrow('ri_nsl', '{server_dir / 'ns_lossy.arrows'}')")
    nse = _notices(conn, f"SELECT pgcolumnar.import_arrow('ri_nse', '{server_dir / 'ns_exact.arrows'}')")
    usp = _notices(conn, f"SELECT pgcolumnar.import_arrow('ri_usp', '{server_dir / 'us_plain.arrows'}')")

    expect.num(_one(conn, "SELECT count(*) FROM ri_nsl"), 4, "a lossy nanosecond file imports every row")
    expect.num(_one(conn, "SELECT count(*) FROM ri_nse"), 4, "so does a microsecond-exact nanosecond file")
    expect.num(_one(conn, "SELECT count(*) FROM ri_usp"), 4, "and a microsecond file")
    expect.num(sum("columnar.import_arrow: 8 values lost sub-microsecond precision" in m for m in nsl), 1,
               "the narrowing reports the number of VALUES that lost digits, not rows")
    expect.num(sum("import_arrow: 4 values lost" in m for m in nsl), 0, "and does not report the row count instead")

    _run(conn, "CREATE TABLE ri_nsd (tm time, lst timestamp[]) USING pgcolumnar")
    nsd = _notices(conn, f"SELECT pgcolumnar.import_arrow('ri_nsd', '{server_dir / 'ns_deep.arrows'}')")
    expect.num(sum("columnar.import_arrow: 12 values lost sub-microsecond precision" in m for m in nsd), 1,
               "a time64 column and a nested list are counted too, not just a top-level timestamp")
    expect.num(_one(conn, "SELECT count(*) FROM ri_nsd"), 4, "and that file imports every row as well")

    expect.num(sum("sub-microsecond" in m.lower() for m in nse), 0,
               "control: an ns file on microsecond boundaries reports nothing")
    expect.num(sum("sub-microsecond" in m.lower() for m in usp), 0, "control: a microsecond file reports nothing")
    expect.text(_one(conn, "SELECT string_agg(ts::text, ',' ORDER BY ts) FROM ri_nsl"),
                "2000-01-01 00:00:00,2000-01-01 00:00:01,2000-01-01 00:00:02,2000-01-01 00:00:03",
                "the narrowed values are floored to the microsecond")


def _temporal_fixtures(pa, out):
    """The shell's fixture files, written the same ways: raw-byte packing for the
    temporal carriers and pa.array for the #881 parameter cases."""
    s2000, noon = 946684800, 12 * 3600
    cases = {
        "tu_date32": (pa.date32(), s2000 // 86400), "tu_date64": (pa.date64(), s2000 * 1000),
        "tu_ts_s": (pa.timestamp("s"), s2000), "tu_ts_ms": (pa.timestamp("ms"), s2000 * 1000),
        "tu_ts_us": (pa.timestamp("us"), s2000 * 1000000), "tu_ts_ns": (pa.timestamp("ns"), s2000 * 1000000000),
        "tu_t64_us": (pa.time64("us"), noon * 1000000), "tu_t64_ns": (pa.time64("ns"), noon * 1000000000),
        "tu_t32_s": (pa.time32("s"), noon), "tu_t32_ms": (pa.time32("ms"), noon * 1000),
        "tu_ts_s_ovf": (pa.timestamp("s"), 2**63 - 1), "tu_ts_ms_far": (pa.timestamp("ms"), -300000000000000),
        "tu_d64_frac": (pa.date64(), 86400001), "tu_d64_neg": (pa.date64(), -1),
        "tu_t64_ns_ovf": (pa.time64("ns"), 25 * 3600 * 10**9),
        "tu_ts_ns_trunc": (pa.timestamp("ns"), s2000 * 10**9 + 1500),
        "tu_t64_ns_neg": (pa.time64("ns"), -500), "tu_ts_ns_neg": (pa.timestamp("ns"), -1500),
        "tu_d32_far": (pa.date32(), 2**31 - 1), "tu_d32_past": (pa.date32(), -2**31),
        "tu_d64_far": (pa.date64(), 2**62), "tu_t64_plain": (pa.time64("us"), noon * 10**6),
    }
    for name, (typ, v) in cases.items():
        raw = struct.pack("<i" if typ.bit_width == 32 else "<q", v)
        arr = pa.Array.from_buffers(typ, 1, [None, pa.py_buffer(raw)])
        _write_stream(pa, out / f"{name}.arrows", pa.table({"v": arr}))
    extra = {
        "tu_u64": pa.array([2**63 + 5], pa.uint64()), "tu_i32": pa.array([7], pa.int32()),
        "tu_i64": pa.array([7], pa.int64()), "tu_f32": pa.array([1.5], pa.float32()),
        "tu_f64": pa.array([1.5], pa.float64()),
        "tu_d102": pa.array([decimal.Decimal("1.25")], pa.decimal128(10, 2)),
        "tu_d204": pa.array([decimal.Decimal("1.25")], pa.decimal128(20, 4)),
        "tu_fsb32": pa.array([b"0123456789abcdef0123456789abcdef"], pa.binary(32)),
        "tu_fsb16": pa.array([b"0123456789abcdef"], pa.binary(16)),
        "tu_int64_ts": pa.array([946684800 * 10**6], pa.int64()),
        # three date32 rows: a 12-byte buffer a stride-8 decoder runs off the end of
        "tu_d32_3": pa.array([10957, 10958, 10959], pa.date32()),
    }
    for name, arr in extra.items():
        _write_stream(pa, out / f"{name}.arrows", pa.table({"v": arr}))
    ns = s2000 * 10**9
    _write_stream(pa, out / "tu_list_ns.arrows", pa.table({"v": pa.array([[ns]], pa.list_(pa.timestamp("ns")))}))
    st = pa.array([{"a": 7, "b": ns}], pa.struct([("a", pa.int32()), ("b", pa.timestamp("ns"))]))
    _write_stream(pa, out / "tu_struct_ns.arrows", pa.table({"v": st}))


def test_the_refusals_and_every_temporal_carrier(pgc_conn, server_dir, expect):
    pa = expect.require_module("pyarrow", name="reject dictionary-encoded file")
    if pa is None:
        return
    conn = pgc_conn

    _write_stream(pa, server_dir / "dict.arrows", pa.table({"c": pa.array(["a", "b", "a", "b"]).dictionary_encode()}))
    _run(conn, "CREATE TABLE ri_d (c text) USING pgcolumnar")
    expect.text(_refusal(conn, f"SELECT pgcolumnar.import_arrow('ri_d', '{server_dir / 'dict.arrows'}')"),
                "0A000 columnar.import_arrow does not support dictionary-encoded Arrow files",
                "reject dictionary-encoded file")

    codec = next((c for c in ("zstd", "lz4") if pa.Codec.is_available(c)), None)
    zipf = server_dir / "compressed.arrows"
    if codec:
        _write_stream(pa, zipf, pa.table({"a": pa.array(range(1000), pa.int64())}),
                      options=pa.ipc.IpcWriteOptions(compression=codec))
    _run(conn, "CREATE TABLE ri_z (a bigint) USING pgcolumnar")
    expect.text("codec" if codec in ("zstd", "lz4") else f"none ({codec})", "codec",
                "premise: this pyarrow writes a compressed body (zstd or lz4)")
    expect.text(_refusal(conn, f"SELECT pgcolumnar.import_arrow('ri_z', '{zipf}')")
                + f"|{_one(conn, 'SELECT count(*) FROM ri_z')}",
                "0A000 columnar.import_arrow does not support compressed Arrow bodies|0",
                "reject a compressed record-batch body")

    _temporal_fixtures(pa, server_dir)

    def tu_value(fixture, pgtype):
        import psycopg
        _run(conn, "DROP TABLE IF EXISTS tu_t", f"CREATE TABLE tu_t (v {pgtype}) USING pgcolumnar")
        try:
            _run(conn, f"SELECT pgcolumnar.import_arrow('tu_t', '{server_dir / (fixture + '.arrows')}')")
        except psycopg.Error:
            return "IMPORT-FAILED"
        return _one(conn, "SELECT v::text FROM tu_t LIMIT 1") or ""

    def tu_state(fixture, pgtype):
        _run(conn, "DROP TABLE IF EXISTS tu_t", f"CREATE TABLE tu_t (v {pgtype}) USING pgcolumnar")
        return _state(conn, f"SELECT pgcolumnar.import_arrow('tu_t', '{server_dir / (fixture + '.arrows')}')")

    # PREMISES: our own exporter's units round-trip, or every value arm could pass
    # on a reader that rejects everything.
    expect.text(tu_value("tu_ts_us", "timestamp"), "2000-01-01 00:00:00",
                "premise: the exporter's own timestamp unit still imports correctly")
    expect.text(tu_value("tu_t64_us", "time"), "12:00:00", "premise: and its own time unit does too")
    expect.text(tu_value("tu_date32", "date"), "2000-01-01",
                "premise: and a date32 carrier, which shares Arrow's tag 8 with date64")
    expect.text(tu_value("tu_date64", "date"), "2000-01-01", "a date64 carrier decodes to the date it holds (#864)")
    expect.text(tu_value("tu_ts_s", "timestamp"), "2000-01-01 00:00:00",
                "a timestamp in seconds decodes to the instant it holds (#865)")
    expect.text(tu_value("tu_ts_ms", "timestamp"), "2000-01-01 00:00:00",
                "a timestamp in milliseconds decodes to the instant it holds (#865)")
    expect.text(tu_value("tu_ts_ns", "timestamp"), "2000-01-01 00:00:00",
                "a timestamp in nanoseconds decodes to the instant it holds (#865)")
    expect.text(tu_value("tu_t64_ns", "time"), "12:00:00", "a time64 in nanoseconds decodes to the time it holds (#865)")
    expect.text(tu_value("tu_t32_s", "time"), "12:00:00", "a time32 in seconds decodes to the time it holds (#865)")
    expect.text(tu_value("tu_t32_ms", "time"), "12:00:00",
                "a time32 in milliseconds decodes to the time it holds (#865)")

    # the guards the unit fix introduced; 22008 is datetime_field_overflow
    expect.text(tu_state("tu_ts_us", "timestamp"), "00000",
                "premise: a well-formed import reports 00000, so the probe reads success")
    expect.text(tu_state("tu_ts_s_ovf", "timestamp"), "22008", "a second count that overflows on scaling is refused, not wrapped")
    expect.text(tu_state("tu_ts_ms_far", "timestamp"), "22008", "a timestamp before PostgreSQL's range is refused")
    expect.text(tu_state("tu_t64_ns_ovf", "time"), "22008", "a time past midnight is refused")
    expect.text(tu_value("tu_d64_frac", "date"), "1970-01-02", "a date64 instant mid-day reports the day it falls in")
    expect.text(tu_value("tu_d64_neg", "date"), "1969-12-31", "a date64 instant before the epoch floors rather than truncating")
    expect.text(tu_value("tu_ts_ns_trunc", "timestamp"), "2000-01-01 00:00:00.000001",
                "a nanosecond timestamp truncates to microseconds")
    expect.text(tu_value("tu_ts_ns_neg", "timestamp"), "1969-12-31 23:59:59.999998",
                "a nanosecond timestamp before the epoch floors rather than truncating")
    expect.text(tu_state("tu_t64_ns_neg", "time"), "22008", "a negative nanosecond time is refused, not narrowed into midnight")
    expect.text(tu_state("tu_d32_far", "date"), "22008", "a date32 past PostgreSQL's last date is refused")
    expect.text(tu_state("tu_d32_past", "date"), "22008", "a date32 before PostgreSQL's first date is refused")
    expect.text(tu_state("tu_d64_far", "date"), "22008", "a date64 past PostgreSQL's last date is refused")

    # the file's declared type must match the target's
    targets = ("bigint", "uuid", "time", "numeric(20,4)", "timestamp")
    expect.num(len(targets), 5, "premise: the date32 sweep covers five target types")
    for target in targets:
        expect.text(tu_state("tu_d32_3", target), "42804",
                    f"a date32 file is refused for a {target} column, not read past its buffer")
    expect.text(tu_state("tu_t64_plain", "date"), "42804", "a time64 file is refused for a date column, not stored as year 687342")
    expect.text(tu_state("tu_ts_us", "date"), "42804", "a timestamp file is refused for a date column")
    expect.text(tu_state("tu_date64", "timestamp"), "42804", "a date64 file is refused for a timestamp column")

    # the file's own Int/Float/Decimal parameters (#881)
    expect.text(tu_state("tu_u64", "bigint"), "42804", "a uint64 file is refused for bigint (#881)")
    expect.text(tu_state("tu_i64", "int"), "42804", "an int64 file is refused for a 4-byte int (#881)")
    expect.text(tu_state("tu_i32", "bigint"), "42804", "an int32 file is refused for bigint (#881)")
    expect.text(tu_state("tu_f32", "float8"), "42804", "a float32 file is refused for float8 (#881)")
    expect.text(tu_state("tu_i64", "bigint"), "00000", "control: a matching int64 file imports into bigint")
    expect.text(tu_state("tu_i32", "int"), "00000", "control: a matching int32 file imports into int")
    expect.text(tu_state("tu_f64", "float8"), "00000", "control: a matching float64 file imports into float8")
    expect.text(tu_state("tu_f32", "float4"), "00000", "control: a matching float32 file imports into float4")
    expect.text(tu_state("tu_fsb32", "uuid"), "42804", "a wider fixed-size binary is refused for uuid (#881)")
    expect.text(tu_state("tu_fsb16", "uuid"), "00000", "control: a 16-byte fixed-size binary imports into uuid")
    expect.text(tu_state("tu_d102", "numeric(20,4)"), "42804", "a decimal at another scale is refused (#881)")
    expect.text(tu_state("tu_d204", "numeric(20,4)"), "00000", "control: a matching decimal imports")
    tu_state("tu_u64", "bigint")
    expect.num(_one(conn, "SELECT count(*) FROM tu_t"), 0, "and a refused import leaves the target empty")
    expect.text(tu_state("tu_int64_ts", "timestamp"), "00000",
                "control: a non-temporal tag is not caught by the temporal refusal")

    # the unit on a nested field
    expect.text(tu_value("tu_list_ns", "timestamp[]"), '{"2000-01-01 00:00:00"}',
                "a list element's nanosecond unit is honoured, not just the top level")
    _run(conn, "DROP TYPE IF EXISTS tu_st CASCADE", "CREATE TYPE tu_st AS (a int, b timestamp)")
    expect.text(tu_value("tu_struct_ns", "tu_st"), '(7,"2000-01-01 00:00:00")',
                "a struct field's nanosecond unit is honoured at child index 1")


def test_an_import_maintains_the_tables_indexes(pgc_conn, server_dir, expect):
    """#153: table_tuple_insert writes the row and nothing else, so an importer must
    maintain indexes itself. The two access paths are compared on one predicate."""
    conn = pgc_conn
    ixfile = server_dir / "ix_roundtrip.arrows"
    _run(conn, "CREATE TABLE ix_src (id int, v text) USING pgcolumnar",
         "INSERT INTO ix_src SELECT g, 'v' || g FROM generate_series(1, 3000) g",
         f"SELECT pgcolumnar.export_arrow('ix_src', '{ixfile}')",
         "CREATE TABLE ix_tgt (id int, v text) USING pgcolumnar",
         "CREATE INDEX ix_tgt_id ON ix_tgt (id)",
         f"SELECT pgcolumnar.import_arrow('ix_tgt', '{ixfile}')")
    # enable_seqscan alone leaves the columnar custom scan cheapest, so the index
    # was never consulted; enable_custom_scan takes that path out, and the plan is asserted.
    idx_on = ("SET enable_seqscan = off", "SET pgcolumnar.enable_custom_scan = off")
    idx_off = ("RESET enable_seqscan", "RESET pgcolumnar.enable_custom_scan")
    seq_on = ("SET enable_indexscan = off", "SET enable_bitmapscan = off", "SET enable_indexonlyscan = off")
    seq_off = ("RESET enable_indexscan", "RESET enable_bitmapscan", "RESET enable_indexonlyscan")

    def under(on, off, sql):
        _run(conn, *on)
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                return cur.fetchall()
        finally:
            _run(conn, *off)

    q100 = "SELECT count(*) FROM ix_tgt WHERE id BETWEEN 100 AND 199"
    plan = "\n".join(r[0] for r in under(idx_on, idx_off, f"EXPLAIN (COSTS OFF) {q100}"))
    expect.text("yes" if re.search(r"index.*scan", plan, re.I) else "no", "yes",
                "the index-scan check really uses the index")
    idx_n = under(idx_on, idx_off, q100)[0][0]
    seq_n = under(seq_on, seq_off, q100)[0][0]
    expect.num(idx_n, seq_n, "import into an indexed table: both scans agree")
    expect.num(seq_n, 100, "import into an indexed table: the rows are there")
    expect.num(under(idx_on, idx_off, "SELECT count(*) FROM ix_tgt WHERE id = 2222")[0][0], 1,
               "import into an indexed table: a point lookup finds its row")

    _run(conn, "CREATE TABLE ix_uq (id int, v text) USING pgcolumnar",
         "CREATE UNIQUE INDEX ix_uq_id ON ix_uq (id)",
         f"SELECT pgcolumnar.import_arrow('ix_uq', '{ixfile}')")
    expect.text(_state(conn, f"SELECT pgcolumnar.import_arrow('ix_uq', '{ixfile}')"), "23505",
                "importing the same keys twice raises unique_violation")
    expect.num(_one(conn, "SELECT count(*) FROM ix_uq"), 3000, "the failed second import left the row count alone")

    _run(conn, "CREATE TABLE ix_part (id int, v text) USING pgcolumnar",
         "CREATE INDEX ix_part_id ON ix_part (id) WHERE id <= 500",
         f"SELECT pgcolumnar.import_arrow('ix_part', '{ixfile}')")
    expect.num(under(idx_on, idx_off, "SELECT count(*) FROM ix_part WHERE id BETWEEN 100 AND 199")[0][0], 100,
               "partial index covers its rows after import")


def test_a_fifo_is_refused_not_a_hang(pgc_conn, pgc_cluster, server_dir, expect):
    """#686/#644: fopen("rb") on a FIFO blocks in open(2), and SA_RESTART carries the
    block through a cancel, so only a wall clock can tell a hang. The call runs on a
    connection of its own in a thread; past 5 s it is a HANG, and the FIFO is then
    opened here so the backend can return."""
    import psycopg
    conn = pgc_conn
    schema = _one(conn, "SELECT current_schema()")
    _run(conn, "CREATE TABLE ia_fifo (a int) USING pgcolumnar")
    fifo = server_dir / "ia.fifo"
    os.mkfifo(fifo)
    os.chmod(fifo, 0o666)
    got = {}

    def call():
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as c:
            c.execute(f'SET search_path TO "{schema}", pgcolumnar, public')
            got["state"] = _state(c, f"SELECT pgcolumnar.import_arrow('ia_fifo', '{fifo}')")

    t = threading.Thread(target=call)
    t.start()
    t.join(5)
    if t.is_alive():
        fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
        os.close(fd)
        t.join(30)
        got["state"] = "HANG"
    expect.text(got.get("state", "no result"), "XX001", "import_arrow on a FIFO is refused, not a hang (XX001)")
    expect.num(_one(conn, "SELECT 1"), 1, "backend still up after the import_arrow FIFO refusal")


REMOTE_S3 = "s3://pgc-test/f1886/m.arrow"
REMOTE_HTTP = "http://127.0.0.1:9/f1886/m.arrow"
REMOTE_OTHER = ("https://127.0.0.1:9/f1886/m.arrow", "gs://pgc-test/f1886/m.arrow",
                "az://pgc-test/f1886/m.arrow", "S3://pgc-test/f1886/m.arrow")


def _remote_refusal(conn, sql):
    """-> 'SQLSTATE message | hint' of the error `sql` raised, or 'no error (...)'."""
    import psycopg
    try:
        return f"no error ({_one(conn, sql)})"
    except psycopg.Error as e:
        return f"{e.sqlstate} {e.diag.message_primary} | {e.diag.message_hint or 'no hint'}"


def _want_refusal(url):
    return (f'0A000 columnar: import_arrow does not read "{url}" | '
            "import_arrow reads local files only. Use a local filesystem path.")


def test_a_remote_url_is_refused_up_front(pgc_conn, server_dir, expect):
    """#1886: import_arrow reads local files only. An s3:// or http:// URL went to
    open(2) and failed with ENOENT, which says the object does not exist. Every scheme
    PgColumnarPathIsRemote recognises is now refused 0A000, naming the function and the
    URL, after the INSERT check and before the target's kind is checked (so a heap
    target gets 0A000, not 42809); a LOCAL path holding "s3://" past its start
    is still read."""
    conn = pgc_conn
    _run(conn, "CREATE TABLE ia_rem (a int) USING pgcolumnar",
         "CREATE TABLE ia_rem_heap (a int) USING heap")
    expect.text(_remote_refusal(conn, f"SELECT pgcolumnar.import_arrow('ia_rem', '{REMOTE_S3}')"),
                _want_refusal(REMOTE_S3),
                "import_arrow refuses an s3:// URL with 0A000, naming itself and the URL (#1886)")
    expect.text(_remote_refusal(conn, f"SELECT pgcolumnar.import_arrow('ia_rem', '{REMOTE_HTTP}')"),
                _want_refusal(REMOTE_HTTP),
                "import_arrow refuses an http:// URL with 0A000, naming itself and the URL (#1886)")
    expect.text(" ; ".join(_remote_refusal(conn, f"SELECT pgcolumnar.import_arrow('ia_rem', '{u}')")
                           for u in REMOTE_OTHER),
                " ; ".join(_want_refusal(u) for u in REMOTE_OTHER),
                "import_arrow refuses https://, gs://, az:// and S3:// URLs with 0A000 too (#1886)")
    expect.text(_remote_refusal(conn, f"SELECT pgcolumnar.import_arrow('ia_rem_heap', '{REMOTE_S3}')"),
                _want_refusal(REMOTE_S3),
                "import_arrow refuses a remote URL before it looks at the target: a heap target gets 0A000 (#1886)")

    # server_dir/"s3:" is a directory, so server_dir/"s3://ctl.arrows" is a local file
    # whose path holds "s3://" past its start.
    sub = server_dir / "s3:"
    sub.mkdir()
    os.chmod(sub, 0o777)
    local = f"{server_dir}/s3://ctl.arrows"
    _run(conn, "CREATE TABLE ia_ctl_src (a int) USING pgcolumnar",
         "INSERT INTO ia_ctl_src SELECT g FROM generate_series(1, 300) g",
         f"SELECT pgcolumnar.export_arrow('ia_ctl_src', '{local}')")
    expect.num(_one(conn, f"SELECT pgcolumnar.import_arrow('ia_rem', '{local}')"), 300,
               "control: import_arrow still reads a local path that holds s3:// past its start (#1886)")
