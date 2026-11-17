"""pgcolumnar.import_arrow refuses text that is not valid in the database encoding.

import_arrow handed an Arrow UTF8 value to the column type's input function as it
came. textin, varcharin, json_in and jsonb_in do not check the encoding, because
core converts and verifies client text before any input function sees it, and a
file read by the backend skips that step. So a stream holding the bytes ff fe and a lone c3 imported 3 rows into a text column,
and upper() and ORDER BY on that column then failed with "invalid byte sequence
for encoding". read_parquet refuses the same bytes (pq_text_cell_ok). import_arrow
now refuses them too, with 22021, naming the column, and stores no row.

Every check needs a pyarrow-written file, so each test declines by name when
pyarrow is absent. Every file lives in `server_dir`, because the backend opens it.
"""
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


def _outcome(conn, sql):
    """-> the value `sql` returned, as text, or 'SQLSTATE message' of its error."""
    import psycopg
    try:
        return str(_one(conn, sql))
    except psycopg.Error as e:
        return f"{e.sqlstate} {e.diag.message_primary}"


def _write_strings(pa, path, data, offsets, name="c", wrap=None):
    """Write a one-column stream of `len(offsets) - 1` UTF8 values cut from the raw
    bytes `data`. pyarrow's own constructors refuse invalid UTF-8, so the array is
    built from its buffers. `wrap` turns the string array into another column, such
    as a list of it."""
    import struct
    n = len(offsets) - 1
    offs = pa.py_buffer(struct.pack(f"<{len(offsets)}i", *offsets))
    arr = pa.Array.from_buffers(pa.string(), n, [None, offs, pa.py_buffer(data)])
    if wrap is not None:
        arr = wrap(arr)
    table = pa.table({name: arr})
    with pa.ipc.new_stream(pa.OSFile(str(path), "wb"), table.schema) as w:
        w.write_table(table)
    path.chmod(0o644)


# The audit's stream: 'ok', then ff fe (no UTF-8 sequence starts with ff), then a
# lone c3 (a lead byte with no continuation byte).
BAD_DATA = b"ok\xff\xfe\xc3"
BAD_OFFSETS = [0, 2, 4, 5]


def _import(conn, server_dir, fixture, table, coltype, col="c"):
    """-> "<what import_arrow returned, or 'SQLSTATE message'>|<rows stored>"."""
    _run(conn, f"CREATE TABLE {table} ({col} {coltype}) USING pgcolumnar")
    got = _outcome(conn, f"SELECT pgcolumnar.import_arrow('{table}', '{server_dir / fixture}')")
    return f"{got}|{_one(conn, f'SELECT count(*) FROM {table}')}"


def _premise_utf8(conn, expect, name):
    """Every check here is about UTF-8 bytes, so the database must be UTF8."""
    expect.text(_one(conn, "SHOW server_encoding"), "UTF8",
                f"premise: the test database is UTF8, for: {name}")


def test_invalid_utf8_into_text_is_refused_and_nothing_is_stored(pgc_conn, server_dir, expect):
    name = ("import_arrow refuses invalid UTF-8 for a text column with 22021, naming the column,"
            " and stores no row")
    pa = expect.require_module("pyarrow", name=name)
    if pa is None:
        return
    _premise_utf8(pgc_conn, expect, name)
    _write_strings(pa, server_dir / "enc_bad.arrows", BAD_DATA, BAD_OFFSETS, name="t")
    expect.text(_import(pgc_conn, server_dir, "enc_bad.arrows", "enc_text", "text", "t"),
                '22021 invalid byte sequence for encoding "UTF8" in Arrow column "t": 0xff|0',
                name)


def test_invalid_utf8_into_varchar_is_refused_and_nothing_is_stored(pgc_conn, server_dir, expect):
    name = ("import_arrow refuses invalid UTF-8 for a varchar(10) column with 22021, naming the column,"
            " and stores no row")
    pa = expect.require_module("pyarrow", name=name)
    if pa is None:
        return
    _premise_utf8(pgc_conn, expect, name)
    _write_strings(pa, server_dir / "enc_bad_v.arrows", BAD_DATA, BAD_OFFSETS, name="v")
    expect.text(_import(pgc_conn, server_dir, "enc_bad_v.arrows", "enc_varchar", "varchar(10)", "v"),
                '22021 invalid byte sequence for encoding "UTF8" in Arrow column "v": 0xff|0',
                name)


def test_a_truncated_sequence_alone_is_refused(pgc_conn, server_dir, expect):
    """The first bad value above is ff, which no encoding check could miss. A lone
    lead byte at the very end of a value is the subtler case: the check must read
    the value's length, not stop at the first valid prefix."""
    name = "import_arrow refuses a value ending in a lone UTF-8 lead byte (c3) with 22021, and stores no row"
    pa = expect.require_module("pyarrow", name=name)
    if pa is None:
        return
    _premise_utf8(pgc_conn, expect, name)
    _write_strings(pa, server_dir / "enc_lead.arrows", b"okab\xc3", [0, 2, 5], name="t")
    expect.text(_import(pgc_conn, server_dir, "enc_lead.arrows", "enc_lead", "text", "t"),
                '22021 invalid byte sequence for encoding "UTF8" in Arrow column "t": 0xc3|0',
                name)


def test_invalid_utf8_inside_a_text_array_is_refused(pgc_conn, server_dir, expect):
    """A list<string> into text[] decodes each element the same way as a scalar."""
    name = "import_arrow refuses invalid UTF-8 inside a list<string> for a text[] column with 22021"
    pa = expect.require_module("pyarrow", name=name)
    if pa is None:
        return
    _premise_utf8(pgc_conn, expect, name)

    def as_list(arr):
        return pa.ListArray.from_arrays(pa.array([0, 3], pa.int32()), arr)

    _write_strings(pa, server_dir / "enc_list.arrows", BAD_DATA, BAD_OFFSETS, name="a", wrap=as_list)
    expect.text(_import(pgc_conn, server_dir, "enc_list.arrows", "enc_list", "text[]", "a"),
                '22021 invalid byte sequence for encoding "UTF8" in Arrow column "a": 0xff|0',
                name)


def test_valid_multibyte_utf8_imports_unchanged(pgc_conn, server_dir, expect):
    """The control: the check refuses bad bytes, not every byte with the high bit set."""
    name = "control: valid multibyte UTF-8 imports into text and varchar and reads back unchanged"
    pa = expect.require_module("pyarrow", name=name)
    if pa is None:
        return
    conn = pgc_conn
    _premise_utf8(conn, expect, name)
    values = ["ok", "é", "日本", "\U0001F600", ""]
    data = b""
    offsets = [0]
    for v in values:
        data += v.encode("utf-8")
        offsets.append(len(data))
    _write_strings(pa, server_dir / "enc_good.arrows", data, offsets, name="t")
    want = "|".join(values)
    for table, coltype in (("enc_good_t", "text"), ("enc_good_v", "varchar(10)")):
        expect.text(_import(conn, server_dir, "enc_good.arrows", table, coltype, "t"), "5|5",
                    f"{name}: {coltype} imports every row")
        _run(conn, f"ALTER TABLE {table} ADD COLUMN n serial")
        expect.text(_one(conn, f"SELECT string_agg(t, '|' ORDER BY n) FROM {table}"), want,
                    f"{name}: {coltype} reads back unchanged")
        expect.text(_outcome(conn, f"SELECT count(u) FROM (SELECT upper(t) AS u FROM {table} ORDER BY t) s"),
                    "5", f"{name}: {coltype} works with upper() and ORDER BY")


def test_bytea_still_takes_the_raw_bytes(pgc_conn, server_dir, expect):
    """bytea is a byte string, not text: a UTF8 column into it keeps every byte, as
    before the fix."""
    name = "import_arrow still stores invalid UTF-8 bytes into a bytea column unchanged"
    pa = expect.require_module("pyarrow", name=name)
    if pa is None:
        return
    conn = pgc_conn
    _write_strings(pa, server_dir / "enc_bytea.arrows", BAD_DATA, BAD_OFFSETS, name="b")
    expect.text(_import(conn, server_dir, "enc_bytea.arrows", "enc_bytea", "bytea", "b"), "3|3",
                f"{name}: every row imports")
    expect.text(_one(conn, "SELECT string_agg(encode(b, 'hex'), ',' ORDER BY b) FROM enc_bytea"),
                "6f6b,c3,fffe", f"{name}: every byte is kept")


@pytest.mark.parametrize("coltype", ["json", "jsonb"])
def test_json_targets_refuse_invalid_utf8_too(pgc_conn, server_dir, expect, coltype):
    """json_in and jsonb_in check JSON syntax, not the bytes inside a JSON string.
    Measured on main before the fix, this file imported 3 rows into json and into
    jsonb. The check runs before every input function, so they refuse it now."""
    name = f"import_arrow refuses invalid UTF-8 inside a JSON string for a {coltype} column, and stores no row"
    pa = expect.require_module("pyarrow", name=name)
    if pa is None:
        return
    _premise_utf8(pgc_conn, expect, name)
    data = b'"ok""\xff\xfe""\xc3"'
    _write_strings(pa, server_dir / f"enc_{coltype}.arrows", data, [0, 4, 8, 11], name="j")
    expect.text(_import(pgc_conn, server_dir, f"enc_{coltype}.arrows", f"enc_{coltype}", coltype, "j"),
                '22021 invalid byte sequence for encoding "UTF8" in Arrow column "j": 0xff|0', name)


@pytest.mark.parametrize("coltype", ["json", "jsonb"])
def test_valid_json_still_imports(pgc_conn, server_dir, expect, coltype):
    """The json control: valid JSON with multibyte UTF-8 imports as before."""
    name = f"control: valid JSON holding multibyte UTF-8 still imports into a {coltype} column"
    pa = expect.require_module("pyarrow", name=name)
    if pa is None:
        return
    conn = pgc_conn
    _premise_utf8(conn, expect, name)
    data = '{"k": "\u00e9"}["\u65e5\u672c"]'.encode("utf-8")
    first = len('{"k": "\u00e9"}'.encode("utf-8"))
    _write_strings(pa, server_dir / f"enc_ok_{coltype}.arrows", data, [0, first, len(data)], name="j")
    expect.text(_import(conn, server_dir, f"enc_ok_{coltype}.arrows", f"enc_ok_{coltype}", coltype, "j"),
                "2|2", f"{name}: every row imports")
    expect.text(_one(conn, f"SELECT coalesce(j->>'k', j->>0) FROM enc_ok_{coltype} ORDER BY 1 LIMIT 1"),
                "\u00e9", f"{name}: the value reads back")


def test_a_nul_byte_is_refused_not_cut(pgc_conn, server_dir, expect):
    """An input function reads a C string, which ends at a NUL byte, and PostgreSQL
    text cannot hold one. Before the fix this file imported both rows. The value is
    refused now, as read_parquet refuses it."""
    name = "import_arrow refuses a value holding a NUL byte with 22021, and stores no row"
    pa = expect.require_module("pyarrow", name=name)
    if pa is None:
        return
    _premise_utf8(pgc_conn, expect, name)
    _write_strings(pa, server_dir / "enc_nul.arrows", b"oka\x00b", [0, 2, 5], name="t")
    expect.text(_import(pgc_conn, server_dir, "enc_nul.arrows", "enc_nul", "text", "t"),
                '22021 invalid byte sequence for encoding "UTF8" in Arrow column "t": 0x00|0', name)
