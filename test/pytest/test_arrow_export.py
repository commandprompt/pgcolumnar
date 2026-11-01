"""pgcolumnar.export_arrow writes an Arrow IPC stream that pyarrow reads back equal to a
heap oracle (gap 27, piece 1). The pytest twin of `test/arrow_export.sh`, every check by
the shell's name (#1341).

A mixed-type columnar table (NULLs, integer boundaries, NaN and infinities, empty and
unicode text, bytea) copied from a heap table is exported across more than one record
batch, and every value must equal the heap's; an empty table writes a readable
schema-only stream; a heap table and an unsupported column type are refused, each by
its own message; and date, time, timestamps, uuid, numeric and json export to the
representation each maps to.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading, so one red cannot stop the next (#1339). The heap oracle is read
over the driver with the shell's own SQL expressions, not through the file. Each file
reading requires the oracle to hold the fixture's rows, so two empty sides cannot agree,
and the file's row count to equal the oracle's: rows are compared by id, so without it
a row written twice would collapse into one. The shell skips whole
without pyarrow, so every test declines by the shell's skip name first. The files live
in `server_dir`, because the backend writes them.
"""
import hashlib
import math
import os

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_arrow_export"
COLS = ["id", "a", "b", "c", "d", "e", "f", "g", "h"]
DEFS = "id int, a int2, b int4, c int8, d float4, e float8, f bool, g text, h bytea"
BODY = r"""
  SELECT s AS id, (s%30000-15000)::int2, s*2, s::int8*1000,
         (s::float4/3), (s::float8/7), (s%2=0), 'r'||s,
         decode(lpad(to_hex(s),6,'0'),'hex')
  FROM generate_series(1,20000) s
  UNION ALL VALUES
   (100001, NULL::int2, NULL::int4, NULL::int8, NULL::float4, NULL::float8, NULL::bool, NULL::text, NULL::bytea),
   (100002, (-32768)::int2, (-2147483648)::int4, (-9223372036854775808)::int8, '-Infinity'::float4, 'NaN'::float8, false, ''::text, '\x'::bytea),
   (100003, (32767)::int2, (2147483647)::int4, (9223372036854775807)::int8, 'Infinity'::float4, (3.14)::float8, true, 'unicode q'::text, '\x00ff10'::bytea)
"""
# float4 widened to float8 so its value matches Arrow's, as the shell's COPY does
ORACLE = "SELECT id, a, b, c, (d::float8), e, f, g, encode(h,'hex') FROM t_heap ORDER BY id"
EXT_INSERT = """INSERT INTO t_ext_heap VALUES
  (1,'2021-03-04','12:34:56.789012','2021-03-04 12:34:56.789012',
     '2021-03-04 12:34:56.789012+00','11111111-2222-3333-4444-555555555555',
     12345.6789, 9999999999999999.1234, '{"a": 1}', '{"b": 2}'),
  (2,'1970-01-01','00:00:00','2000-01-01 00:00:00','2000-01-01 00:00:00+00',
     '00000000-0000-0000-0000-000000000000', -0.0001, -1234567890.5,
     '[1, 2, 3]', '[3, 2, 1]'),
  (3, NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL),
  (4,'infinity','23:59:59.999999','infinity','-infinity',
     'ffffffff-ffff-ffff-ffff-ffffffffffff', 'NaN', 'NaN', 'null', 'true')"""
EXT_ORACLE = """SELECT id,
    CASE WHEN dt IS NULL OR NOT isfinite(dt) THEN NULL ELSE (dt - DATE '1970-01-01') END,
    CASE WHEN tm IS NULL THEN NULL ELSE (extract(epoch from tm)*1000000)::bigint END,
    CASE WHEN ts IS NULL OR NOT isfinite(ts) THEN NULL ELSE (extract(epoch from ts)*1000000)::bigint END,
    CASE WHEN tz IS NULL OR NOT isfinite(tz) THEN NULL ELSE (extract(epoch from tz)*1000000)::bigint END,
    CASE WHEN u IS NULL THEN NULL ELSE replace(u::text,'-','') END,
    CASE WHEN num IS NULL OR num = 'NaN'::numeric THEN NULL ELSE num::text END,
    CASE WHEN numun IS NULL THEN NULL ELSE numun::text END,
    j::text, jb::text
   FROM t_ext_heap ORDER BY id"""
BYTES_DIGEST = "SELECT string_agg(id || ':' || length(s) || ':' || md5(s), ' ' ORDER BY id) FROM ar_bytes"
TXT_DIGEST = ("SELECT string_agg(id || '|' || coalesce(n::text, 'NULL') || '|' || coalesce(array_to_string(a, ',', 'N'), 'NULL')"
              " || '|' || j::text || '|' || (c).x::text || '|' || (c).y, ' ' ORDER BY id) FROM ar_txt")
OVER_NAME = ("over 1 GiB of one column (2200 values of 500000 bytes) exports whole, "
             "in 5 record batches of at most 536 rows")
SCHEMA = "id:int32,a:int16,b:int32,c:int64,d:float,e:double,f:bool,g:string,h:binary"
EXT_SCHEMA = ("id:int32,dt:date32[day],tm:time64[us],ts:timestamp[us],tz:timestamp[us, tz=UTC],"
              "u:fixed_size_binary[16],num:decimal128(20, 4),numun:string,j:string,jb:string")


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


def _rows(dsn, sql):
    """-> every row as a tuple, or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            return [tuple(r) for r in conn.execute(sql)]
    except psycopg.Error as exc:
        return _failed(exc)


def _run(dsn, sql):
    """Run one statement and swallow its failure, as the shell's psql_run does: a
    failed step shows up in the readings after it."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)
    except psycopg.Error:
        pass


def _refusal(dsn, sql, msg):
    """-> "refused" when `sql` fails with `msg`, "succeeded", or the other error."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)
    except psycopg.Error as exc:
        text = str(exc)
        return "refused" if msg in text else f"other error: {text.splitlines()[0] if text else type(exc).__name__}"
    return "succeeded"


def _value_after(dsn, setup, sql):
    """-> `sql`'s first value after the `setup` statements on the same session, or the
    failure as a query_error(). A failed setup statement is passed over, as psql passes
    over a failed -c, so the reading is what `sql` did without it."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for st in setup:
                try:
                    conn.execute(st)
                except psycopg.Error:
                    pass
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return _failed(exc)
    return "" if row is None or row[0] is None else str(row[0])


@pytest.fixture(scope="module")
def ae(pgc_cluster, server_dir):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        f = {k: server_dir / n for k, n in
             (("t", "t.arrows"), ("e", "e.arrows"), ("x", "x.arrows"), ("ext", "ext.arrows"))}
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        # make_pair and load_pair: generated once into heap, then copied to columnar
        _run(dsn, f"CREATE TABLE t_heap ({DEFS}) USING heap")
        _run(dsn, f"CREATE TABLE t_col ({DEFS}) USING pgcolumnar")
        _run(dsn, f"INSERT INTO t_heap {BODY}")
        _run(dsn, "INSERT INTO t_col SELECT * FROM t_heap")
        r = {"files": f, "rows": _value(dsn, f"SELECT pgcolumnar.export_arrow('t_col', '{f['t']}')"),
             "oracle": _rows(dsn, ORACLE)}
        _run(dsn, "CREATE TABLE t_empty (a int, b text) USING pgcolumnar")
        r["empty rows"] = _value(dsn, f"SELECT pgcolumnar.export_arrow('t_empty', '{f['e']}')")
        r["non-columnar"] = _refusal(dsn, f"SELECT pgcolumnar.export_arrow('t_heap', '{f['x']}')",
                                     "is not a columnar table")
        _run(dsn, "CREATE TABLE t_un (a int, p point) USING pgcolumnar")
        _run(dsn, "INSERT INTO t_un VALUES (1, '(1,2)')")
        r["unsupported"] = _refusal(dsn, f"SELECT pgcolumnar.export_arrow('t_un', '{f['x']}')",
                                    "which columnar.export_arrow does not support")
        _run(dsn, "CREATE TABLE t_ext_heap (id int, dt date, tm time, ts timestamp, "
                  "tz timestamptz, u uuid, num numeric(20,4), numun numeric, j json, jb jsonb)")
        _run(dsn, "CREATE TABLE t_ext_col (LIKE t_ext_heap) USING pgcolumnar")
        _run(dsn, EXT_INSERT)
        _run(dsn, "INSERT INTO t_ext_col SELECT * FROM t_ext_heap")
        r["ext rows"] = _value(dsn, f"SELECT pgcolumnar.export_arrow('t_ext_col', '{f['ext']}')")
        r["ext oracle"] = _rows(dsn, EXT_ORACLE)
        # #1811: record batches cut by bytes, shown at small scale under a lowered cap
        f["bytes"] = server_dir / "bytes.arrows"
        _run(dsn, "CREATE TABLE ar_bytes (id int, s text) USING pgcolumnar")
        _run(dsn, "INSERT INTO ar_bytes SELECT g, repeat(chr(64 + g % 26), 10000) FROM generate_series(1, 40) g")
        r["bytes rows"] = _value_after(dsn, ["SET pgcolumnar.export_group_bytes = 65536"],
                                       f"SELECT pgcolumnar.export_arrow('ar_bytes', '{f['bytes']}')")
        r["bytes digest"] = _value(dsn, BYTES_DIGEST)
        # #1843 review: the cut counts each row as written, not as stored
        for t, ddl, gen in (("ar_w4", "id int", "SELECT g FROM generate_series(1, 1000) g"),
                            ("ar_wnum", "n numeric", "SELECT 1e1000 FROM generate_series(1, 10)"),
                            ("ar_wlist", "a numeric[]", "SELECT ARRAY[1e300, 1e300, 1e300] FROM generate_series(1, 10)")):
            f[t] = server_dir / f"{t}.arrows"
            _run(dsn, f"CREATE TABLE {t} ({ddl}) USING pgcolumnar")
            _run(dsn, f"INSERT INTO {t} {gen}")
            r[f"{t} rows"] = _value_after(dsn, ["SET pgcolumnar.export_group_bytes = 1024"],
                                          f"SELECT pgcolumnar.export_arrow('{t}', '{f[t]}')")
        # #1843 review, second round: the incoming row's estimate counts every column shape
        _run(dsn, "CREATE TYPE ar_rec AS (t text)")
        for t, ddl, gen in (("ar_wst", "id int, r ar_rec", "SELECT g, ROW(repeat('x', 10000))::ar_rec FROM generate_series(1, 40) g"),
                            ("ar_wtl", "id int, a text[]", "SELECT g, ARRAY[repeat('x', 5000), repeat('y', 5000)] FROM generate_series(1, 40) g"),
                            ("ar_wil", "id int, a int[]", "SELECT g, array_fill(7, ARRAY[2000]) FROM generate_series(1, 40) g"),
                            ("ar_wbig", "s text", "SELECT repeat('z', 2000) FROM generate_series(1, 3)")):
            f[t] = server_dir / f"{t}.arrows"
            _run(dsn, f"CREATE TABLE {t} ({ddl}) USING pgcolumnar")
            _run(dsn, f"INSERT INTO {t} {gen}")
            cap = 1024 if t.endswith("_wbig") else 65536
            r[f"{t} rows"] = _value_after(dsn, [f"SET pgcolumnar.export_group_bytes = {cap}"],
                                          f"SELECT pgcolumnar.export_arrow('{t}', '{f[t]}')")
        # #1843 review: each kept numeric or jsonb text reaches its own value
        _run(dsn, "CREATE TYPE ar_tpair AS (x numeric, y text)")
        _run(dsn, "CREATE TABLE ar_txt (id int, n numeric, a numeric[], j jsonb, c ar_tpair) USING pgcolumnar")
        _run(dsn, "INSERT INTO ar_txt SELECT g, CASE WHEN g % 10 = 0 THEN NULL ELSE g::numeric / 7 END, "
                  "CASE WHEN g % 7 = 0 THEN NULL ELSE ARRAY[g::numeric / 3, NULL, g * 1e20] END, "
                  "jsonb_build_object('k', g, 'v', g::numeric / 9), ROW(g::numeric / 11, 't' || g)::ar_tpair "
                  "FROM generate_series(1, 300) g")
        f["txt"] = server_dir / "txt.arrows"
        r["txt rows"] = _value(dsn, f"SELECT pgcolumnar.export_arrow('ar_txt', '{f['txt']}')")
        r["txt digest"] = _value(dsn, TXT_DIGEST)
        r["dsn"] = dsn
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _read(pa, path):
    return pa.ipc.open_stream(pa.OSFile(str(path), "rb")).read_all()


def _reading(fn, pa, *args):
    """-> fn(pa, *args), or the failure to read the file as a query_error()."""
    try:
        return fn(pa, *args)
    except (OSError, KeyError, IndexError, ValueError, TypeError, pa.ArrowException) as exc:
        # a missing or unreadable file, or a field it lacks, is a failed reading
        return query_error(f"{type(exc).__name__}: {exc}")


def _feq(x, y):
    if isinstance(x, float) or isinstance(y, float):
        if x is None or y is None:
            return x is y
        if math.isnan(x) and math.isnan(y):
            return True
        return x == y
    return x == y


def _values(pa, path, oracle):
    if isinstance(oracle, str):
        return oracle
    tbl = _read(pa, path)
    ad = tbl.to_pydict()
    ar = {ad["id"][i]: tuple(ad[c][i] for c in COLS) for i in range(tbl.num_rows)}
    pg = {r[0]: (r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7],
                 None if r[8] is None else bytes.fromhex(r[8])) for r in oracle}
    if len(pg) != 20003:  # with the load failed, both sides were empty and agreed
        return f"ORACLE {len(pg)} rows, not 20003"
    if tbl.num_rows != len(pg):
        return f"ROWS {tbl.num_rows} in the file, {len(pg)} in the oracle"
    ok = set(ar) == set(pg) and all(all(_feq(a, b) for a, b in zip(ar[k], pg[k])) for k in pg)
    return "MATCH" if ok else "MISMATCH"


def _schema(pa, path):
    return ",".join(f"{f.name}:{f.type}" for f in _read(pa, path).schema)


def _empty(pa, path):
    t = _read(pa, path)
    return f"{t.num_rows} {','.join(t.schema.names)}"


def _ext_values(pa, path, oracle):
    if isinstance(oracle, str):
        return oracle
    t = _read(pa, path)

    def col(n):
        return t.column(n)

    def i(n):
        return col(n).cast(pa.int64()).to_pylist()

    ids = col("id").to_pylist()
    dt = col("dt").cast(pa.int32()).to_pylist()  # date32 casts to int32, not int64
    tm, ts, tz = i("tm"), i("ts"), i("tz")
    u = [None if v is None else v.hex() for v in col("u").to_pylist()]
    num = [None if v is None else str(v) for v in col("num").to_pylist()]
    nun = [None if v is None else str(v) for v in col("numun").to_pylist()]
    j, jb = col("j").to_pylist(), col("jb").to_pylist()
    ar = {ids[k]: (ids[k], dt[k], tm[k], ts[k], tz[k], u[k], num[k], nun[k], j[k], jb[k])
          for k in range(t.num_rows)}
    pg = {r[0]: tuple(r) for r in oracle}
    if len(pg) != 4:
        return f"ORACLE {len(pg)} rows, not 4"
    if t.num_rows != len(pg):
        return f"ROWS {t.num_rows} in the file, {len(pg)} in the oracle"
    return "MATCH" if ar == pg else f"MISMATCH {ar!r} {pg!r}"


def test_export_arrow_reports_every_row(ae, expect):
    if expect.require_module("pyarrow", name="pyarrow not available; skipping Arrow export verification") is None:
        return
    expect.text(ae["rows"], "20003", "export_arrow rows written")


def test_arrow_values_match_the_heap_oracle(ae, expect):
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Arrow export verification")
    if pa is None or expect.require_module("pyarrow.ipc", name="pyarrow not available; skipping Arrow export verification") is None:
        return
    expect.text(_reading(_values, pa, ae["files"]["t"], ae["oracle"]), "MATCH", "arrow values match heap oracle")


def test_the_arrow_schema_has_the_expected_types(ae, expect):
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Arrow export verification")
    if pa is None or expect.require_module("pyarrow.ipc", name="pyarrow not available; skipping Arrow export verification") is None:
        return
    expect.text(_reading(_schema, pa, ae["files"]["t"]), SCHEMA, "arrow schema")


def test_an_empty_table_reports_no_rows(ae, expect):
    if expect.require_module("pyarrow", name="pyarrow not available; skipping Arrow export verification") is None:
        return
    expect.text(ae["empty rows"], "0", "empty export rows written")


def test_an_empty_table_writes_a_readable_stream(ae, expect):
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Arrow export verification")
    if pa is None or expect.require_module("pyarrow.ipc", name="pyarrow not available; skipping Arrow export verification") is None:
        return
    expect.text(_reading(_empty, pa, ae["files"]["e"]), "0 a,b", "empty arrow readable")


def test_a_heap_table_is_refused(ae, expect):
    if expect.require_module("pyarrow", name="pyarrow not available; skipping Arrow export verification") is None:
        return
    expect.text(ae["non-columnar"], "refused", "reject non-columnar table")


def test_an_unsupported_column_type_is_refused(ae, expect):
    if expect.require_module("pyarrow", name="pyarrow not available; skipping Arrow export verification") is None:
        return
    expect.text(ae["unsupported"], "refused", "reject unsupported type")


def test_the_extended_export_reports_every_row(ae, expect):
    if expect.require_module("pyarrow", name="pyarrow not available; skipping Arrow export verification") is None:
        return
    expect.text(ae["ext rows"], "4", "extended export rows written")


def test_extended_values_match_the_heap_oracle(ae, expect):
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Arrow export verification")
    if pa is None or expect.require_module("pyarrow.ipc", name="pyarrow not available; skipping Arrow export verification") is None:
        return
    expect.text(_reading(_ext_values, pa, ae["files"]["ext"], ae["ext oracle"]), "MATCH",
                "extended values match heap oracle")


def test_the_extended_schema_has_the_expected_types(ae, expect):
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Arrow export verification")
    if pa is None or expect.require_module("pyarrow.ipc", name="pyarrow not available; skipping Arrow export verification") is None:
        return
    expect.text(_reading(_schema, pa, ae["files"]["ext"]), EXT_SCHEMA, "extended arrow schema")


def _batches(pa, path):
    """-> "rows|n1,n2,..." from the stream: the rows in each record batch."""
    with pa.OSFile(str(path), "rb") as fh:
        sizes = [b.num_rows for b in pa.ipc.open_stream(fh)]
    return f"{sum(sizes)}|" + ",".join(str(n) for n in sizes)


def _digest(pa, path):
    """-> "id:length:md5 ..." by id, as BYTES_DIGEST reads the table."""
    t = _read(pa, path).to_pydict()
    return " ".join(f"{i}:{len(v)}:{hashlib.md5(v.encode()).hexdigest()}" for i, v in sorted(zip(t["id"], t["s"])))


def test_a_record_batch_is_cut_at_the_byte_cap(ae, expect):
    """#1811: 40 rows that each buffer 10010 bytes, under a 65536-byte cap. The writer
    starts a new batch before a row that would pass the cap, so batches hold 6 rows."""
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Arrow export verification")
    if pa is None or expect.require_module("pyarrow.ipc", name="pyarrow not available; skipping Arrow export verification") is None:
        return
    expect.text(f"{ae['bytes rows']}|{_reading(_batches, pa, ae['files']['bytes'])}", "40|40|6,6,6,6,6,6,4",
                "a record batch is cut at the byte cap: 40 rows of 10000 bytes under a 65536-byte cap make batches of 6,6,6,6,6,6,4")


def test_the_byte_cut_arrow_stream_reads_back_exactly(ae, expect):
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Arrow export verification")
    if pa is None or expect.require_module("pyarrow.ipc", name="pyarrow not available; skipping Arrow export verification") is None:
        return
    expect.text(_reading(_digest, pa, ae["files"]["bytes"]), ae["bytes digest"],
                "and the byte-cut stream reads back every row exactly, by id, length and md5")


def test_over_1gib_of_one_column_exports_in_several_record_batches(ae, expect):
    """#1811's repro, opt-in as the shell's arm is: about 1.1 GB in the table and in the
    stream. Each row buffers 500010 bytes, so a 256 MiB batch holds 536 rows."""
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Arrow export verification")
    if pa is None or expect.require_module("pyarrow.ipc", name="pyarrow not available; skipping Arrow export verification") is None:
        return
    if os.environ.get("PGC_RUN_OVER_1GB", "0") != "1":
        expect.cannot_run("UNMET_PRECONDITION",
                          "PGC_RUN_OVER_1GB is not 1; the arm needs several GB of memory and disk",
                          name=OVER_NAME)
        return
    dsn = ae["dsn"]
    path = ae["files"]["t"].parent / "huge.arrows"
    _run(dsn, "CREATE TABLE ar_huge (id int, s text) USING pgcolumnar")
    _run(dsn, "SELECT pgcolumnar.set_options('ar_huge', stripe_row_limit => 1000)")
    _run(dsn, "INSERT INTO ar_huge SELECT g, repeat('x', 500000) FROM generate_series(1, 2200) g")
    try:
        got = _value_after(dsn, ["SET statement_timeout = '900s'"],
                           f"SELECT pgcolumnar.export_arrow('ar_huge', '{path}')")
        expect.text(f"{got}|{_reading(_batches, pa, path)}", "2200|2200|536,536,536,536,56", OVER_NAME)
    finally:
        path.unlink(missing_ok=True)
        _run(dsn, "DROP TABLE ar_huge")


WIDE_NAME = "a row of 8200 numerics of 1e100000 (98 KB stored, 820 MB as text) after a 260 MB row exports whole, in 2 record batches of 1"


def _cut(fix, pa, table):
    return f"{fix[table + ' rows']}|{_reading(_batches, pa, fix['files'][table])}"


def test_a_record_batch_cut_counts_an_int4s_validity_byte(ae, expect):
    """#1843 review: an int4 buffers 4 bytes and a validity byte, so a 1024-byte batch
    holds 204 rows (1020 bytes), not 205."""
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Arrow export verification")
    if pa is None or expect.require_module("pyarrow.ipc", name="pyarrow not available; skipping Arrow export verification") is None:
        return
    expect.text(_cut(ae, pa, "ar_w4"), "1000|1000|204,204,204,204,184",
                "the byte cut counts an int4's validity byte: 1000 int4 rows under a 1024-byte cap make batches of 204,204,204,204,184")


def test_a_record_batch_cut_counts_a_numerics_text_form(ae, expect):
    """#1843 review: 1e1000 stores in a few bytes but is written as 1001 digits."""
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Arrow export verification")
    if pa is None or expect.require_module("pyarrow.ipc", name="pyarrow not available; skipping Arrow export verification") is None:
        return
    expect.text(_cut(ae, pa, "ar_wnum"), "10|10|1,1,1,1,1,1,1,1,1,1",
                "the byte cut counts a numeric's text form: 10 rows of 1e1000 under a 1024-byte cap make 10 batches of 1")


def test_a_record_batch_cut_counts_a_lists_elements_as_written(ae, expect):
    """#1843 review: three 1e300 elements are written as 301 digits each."""
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Arrow export verification")
    if pa is None or expect.require_module("pyarrow.ipc", name="pyarrow not available; skipping Arrow export verification") is None:
        return
    expect.text(_cut(ae, pa, "ar_wlist"), "10|10|1,1,1,1,1,1,1,1,1,1",
                "the byte cut counts a list's elements as written: 10 rows of three 1e300 under a 1024-byte cap make 10 batches of 1")


def test_a_record_batch_cut_counts_a_composites_fields_as_written(ae, expect):
    """#1843 review, second round: a composite row counts its field as written, so a
    10 KB field cannot join a nearly full batch."""
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Arrow export verification")
    if pa is None or expect.require_module("pyarrow.ipc", name="pyarrow not available; skipping Arrow export verification") is None:
        return
    expect.text(_cut(ae, pa, "ar_wst"), "40|40|6,6,6,6,6,6,4",
                "the byte cut counts a composite's fields as written: 40 rows of a 10000-byte text field under a 65536-byte cap make batches of 6,6,6,6,6,6,4")


def test_a_record_batch_cut_counts_a_text_lists_elements_as_written(ae, expect):
    """#1843 review, second round: a text[] row counts each element as written."""
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Arrow export verification")
    if pa is None or expect.require_module("pyarrow.ipc", name="pyarrow not available; skipping Arrow export verification") is None:
        return
    expect.text(_cut(ae, pa, "ar_wtl"), "40|40|6,6,6,6,6,6,4",
                "the byte cut counts a text list's elements as written: 40 rows of two 5000-byte texts under a 65536-byte cap make batches of 6,6,6,6,6,6,4")


def test_a_record_batch_cut_counts_an_int_lists_elements_as_written(ae, expect):
    """#1843 review, second round: an int[] row counts each fixed-width element as written."""
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Arrow export verification")
    if pa is None or expect.require_module("pyarrow.ipc", name="pyarrow not available; skipping Arrow export verification") is None:
        return
    expect.text(_cut(ae, pa, "ar_wil"), "40|40|6,6,6,6,6,6,4",
                "the byte cut counts an int list's elements as written: 40 rows of 2000 int4 under a 65536-byte cap make batches of 6,6,6,6,6,6,4")


def test_a_row_larger_than_the_byte_cap_goes_in_a_batch_of_its_own(ae, expect):
    """#1843 review: a row larger than the cap is written alone, with no empty batch
    before it."""
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Arrow export verification")
    if pa is None or expect.require_module("pyarrow.ipc", name="pyarrow not available; skipping Arrow export verification") is None:
        return
    expect.text(_cut(ae, pa, "ar_wbig"), "3|3|1,1,1",
                "a row larger than the byte cap goes in a batch of its own: 3 rows of 2000 bytes under a 1024-byte cap make batches of 1,1,1")


def test_a_row_far_wider_as_written_than_stored_goes_in_a_batch_of_its_own(ae, expect):
    """#1843 review, opt-in as the shell's arm is. Row 1 holds 260 MB of text; row 2
    holds 8200 numerics of 1e100000, about 98 KB stored but 820 MB as text."""
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Arrow export verification")
    if pa is None or expect.require_module("pyarrow.ipc", name="pyarrow not available; skipping Arrow export verification") is None:
        return
    if os.environ.get("PGC_RUN_OVER_1GB", "0") != "1":
        expect.cannot_run("UNMET_PRECONDITION",
                          "PGC_RUN_OVER_1GB is not 1; the arm needs several GB of memory and disk",
                          name=WIDE_NAME)
        return
    dsn = ae["dsn"]
    path = ae["files"]["t"].parent / "wide.arrows"
    _run(dsn, "CREATE TABLE ar_wide (id int, t text, n numeric[]) USING pgcolumnar")
    _run(dsn, "INSERT INTO ar_wide VALUES (1, repeat('a', 260000000), NULL)")
    _run(dsn, "INSERT INTO ar_wide SELECT 2, NULL, array_fill(1e100000::numeric, ARRAY[8200])")
    try:
        got = _value_after(dsn, ["SET statement_timeout = '900s'"],
                           f"SELECT pgcolumnar.export_arrow('ar_wide', '{path}')")
        expect.text(f"{got}|{_reading(_batches, pa, path)}", "2|2|1,1", WIDE_NAME)
    finally:
        path.unlink(missing_ok=True)
        _run(dsn, "DROP TABLE ar_wide")


def _texts(pa, path):
    """-> "id|n|a|j|x|y ..." by id, as TXT_DIGEST prints the table."""
    t = _read(pa, path).to_pydict()

    def arr(v):
        return "NULL" if v is None else ",".join("N" if e is None else e for e in v)
    return " ".join(f"{i}|{'NULL' if n is None else n}|{arr(a)}|{j}|{c['x']}|{c['y']}"
                    for i, n, a, j, c in sorted(zip(t["id"], t["n"], t["a"], t["j"], t["c"])))


def test_every_kept_text_reaches_its_own_value_in_the_stream(ae, expect):
    """#1843 review: the writer converts a numeric or jsonb once, while it counts the
    row, and keeps the text for the append. Scalars, list elements and a composite's
    field each get their own text."""
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Arrow export verification")
    if pa is None or expect.require_module("pyarrow.ipc", name="pyarrow not available; skipping Arrow export verification") is None:
        return
    expect.text(f"{ae['txt rows']}|{_reading(_texts, pa, ae['files']['txt'])}", f"300|{ae['txt digest']}",
                "every numeric and jsonb text reaches its own value: 300 rows of numeric, numeric[], jsonb and a composite read back from the stream as PostgreSQL prints them")
