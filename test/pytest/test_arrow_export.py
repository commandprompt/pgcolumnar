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
import math

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
