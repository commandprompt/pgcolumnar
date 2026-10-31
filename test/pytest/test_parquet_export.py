"""pgcolumnar.export_parquet writes a Parquet file that pyarrow, and DuckDB where it is
installed, read back equal to a heap oracle (gap 27, piece 2). The pytest twin of
`test/parquet_export.sh`, every check by the shell's name (#1341).

A mixed-type columnar table (NULLs, integer boundaries, NaN and infinities, empty and
unicode text, bytea) copied from a heap table is exported across more than one row
group (80003 rows, over PARQUET_ROWGROUP_ROWS of 65536), and every value must equal the
heap's; DuckDB, a second and independent reader, must count the same rows and the same
non-null b. An empty table writes a readable schema-only file; a heap table and an
unsupported column type are refused, each by its own message; and date, time,
timestamps, uuid, numeric and json export to the representation each maps to.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading, so one red cannot stop the next (#1339). The heap oracle is read
over the driver with the shell's own SQL expressions, not through the file. Each file
reading requires the oracle and the file to hold the generator's ids, so two empty
sides cannot agree, and the file's row count to equal them: rows are compared by id,
so without it a row written twice would collapse into one. The shell skips whole
without pyarrow, so every test declines by the shell's skip name first. The shell
reads the file with the `duckdb` CLI where it is installed and skips those four checks
by name where it is not; here DuckDB is the `duckdb` Python package, pinned in
requirements-test.txt, and without it the same four tests decline by the same names.
CI sets PGC_REQUIRE=pyarrow,duckdb, which turns either decline into a failure. The
files live in `server_dir`, because the backend writes them. A failed query is a
`query_error()`.
"""
import hashlib
import math
import os

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_parquet_export"
COLS = ["id", "a", "b", "c", "d", "e", "f", "g", "h"]
DEFS = "id int, a int2, b int4, c int8, d float4, e float8, f bool, g text, h bytea"
# > PARQUET_ROWGROUP_ROWS (65536) rows to force multiple row groups, plus explicit
# edge-case rows.
BODY = r"""
  SELECT s AS id, (s%30000-15000)::int2, s*2, s::int8*1000,
         (s::float4/3), (s::float8/7), (s%2=0), 'r'||s,
         decode(lpad(to_hex(s),6,'0'),'hex')
  FROM generate_series(1,80000) s
  UNION ALL VALUES
   (100001, NULL::int2, NULL::int4, NULL::int8, NULL::float4, NULL::float8, NULL::bool, NULL::text, NULL::bytea),
   (100002, (-32768)::int2, (-2147483648)::int4, (-9223372036854775808)::int8, '-Infinity'::float4, 'NaN'::float8, false, ''::text, '\x'::bytea),
   (100003, (32767)::int2, (2147483647)::int4, (9223372036854775807)::int8, 'Infinity'::float4, (3.14)::float8, true, 'unicode q'::text, '\x00ff10'::bytea)
"""
# The ids the generator wrote.
IDS = set(range(1, 80001)) | {100001, 100002, 100003}
# float4 widened to float8 so its value matches Parquet's, as the shell's COPY does
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
EXT_TYPES = "date32[day]|time64[us]|fixed_size_binary[16]|decimal128(20, 4)"
BYTES_DIGEST = "SELECT string_agg(id || ':' || length(s) || ':' || md5(s), ' ' ORDER BY id) FROM pq_bytes"
TXT_DIGEST = ("SELECT string_agg(id || '|' || coalesce(n::text, 'NULL') || '|' || coalesce(array_to_string(a, ',', 'N'), 'NULL')"
              " || '|' || j::text || '|' || (c).x::text || '|' || (c).y, ' ' ORDER BY id) FROM pq_txt")
CAP_REFUSAL = '268435457 is outside the valid range for parameter "pgcolumnar.export_group_bytes" (1024 .. 268435456)'
OVER_NAME = ("over 1 GiB of one column (2200 values of 500000 bytes) exports whole, "
             "in 5 row groups of at most 536 rows")
NOT_COLUMNAR = "is not a columnar table"
UNSUPPORTED = 'column "p" has type point, which columnar parquet export does not support'


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
    """-> "refused: MSG" when `sql` fails with `msg`, "succeeded", or the other error.
    Any error used to count, so a backend crash passed as the refusal."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)
    except psycopg.Error as exc:
        text = str(exc)
        return f"refused: {msg}" if msg in text else f"other error: {text.splitlines()[0] if text else type(exc).__name__}"
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


def _set_refusal(dsn, sql):
    """-> "accepted", or the message of the refusal, as the shell reads its ERROR line."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)
    except psycopg.Error as exc:
        return _failed(exc) if not exc.diag.message_primary else exc.diag.message_primary
    return "accepted"


@pytest.fixture(scope="module")
def pqx(pgc_cluster, server_dir):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        f = {k: server_dir / n for k, n in
             (("t", "t.parquet"), ("e", "e.parquet"), ("x", "x.parquet"), ("ext", "ext.parquet"))}
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        # make_pair and load_pair: generated once into heap, then copied to columnar
        _run(dsn, f"CREATE TABLE t_heap ({DEFS}) USING heap")
        _run(dsn, f"CREATE TABLE t_col ({DEFS}) USING pgcolumnar")
        _run(dsn, f"INSERT INTO t_heap {BODY}")
        _run(dsn, "INSERT INTO t_col SELECT * FROM t_heap")
        r = {"files": f, "rows": _value(dsn, f"SELECT pgcolumnar.export_parquet('t_col', '{f['t']}')"),
             "oracle": _rows(dsn, ORACLE)}
        _run(dsn, "CREATE TABLE t_empty (a int, b text) USING pgcolumnar")
        r["empty rows"] = _value(dsn, f"SELECT pgcolumnar.export_parquet('t_empty', '{f['e']}')")
        r["non-columnar"] = _refusal(dsn, f"SELECT pgcolumnar.export_parquet('t_heap', '{f['x']}')",
                                     NOT_COLUMNAR)
        _run(dsn, "CREATE TABLE t_un (a int, p point) USING pgcolumnar")
        _run(dsn, "INSERT INTO t_un VALUES (1, '(1,2)')")
        r["unsupported"] = _refusal(dsn, f"SELECT pgcolumnar.export_parquet('t_un', '{f['x']}')",
                                    UNSUPPORTED)
        _run(dsn, "CREATE TABLE t_ext_heap (id int, dt date, tm time, ts timestamp, "
                  "tz timestamptz, u uuid, num numeric(20,4), numun numeric, j json, jb jsonb)")
        _run(dsn, "CREATE TABLE t_ext_col (LIKE t_ext_heap) USING pgcolumnar")
        _run(dsn, EXT_INSERT)
        _run(dsn, "INSERT INTO t_ext_col SELECT * FROM t_ext_heap")
        r["ext rows"] = _value(dsn, f"SELECT pgcolumnar.export_parquet('t_ext_col', '{f['ext']}')")
        r["ext oracle"] = _rows(dsn, EXT_ORACLE)
        # #1811: row groups cut by bytes, shown at small scale under a lowered cap
        f["bytes"] = server_dir / "bytes.parquet"
        _run(dsn, "CREATE TABLE pq_bytes (id int, s text) USING pgcolumnar")
        _run(dsn, "INSERT INTO pq_bytes SELECT g, repeat(chr(64 + g % 26), 10000) FROM generate_series(1, 40) g")
        r["bytes rows"] = _value_after(dsn, ["SET pgcolumnar.export_group_bytes = 65536"],
                                       f"SELECT pgcolumnar.export_parquet('pq_bytes', '{f['bytes']}')")
        r["bytes digest"] = _value(dsn, BYTES_DIGEST)
        # #1843 review: the cut counts each row as written, not as stored
        for t, ddl, gen in (("pq_w4", "id int", "SELECT g FROM generate_series(1, 1000) g"),
                            ("pq_wnum", "n numeric", "SELECT 1e1000 FROM generate_series(1, 10)"),
                            ("pq_wlist", "a numeric[]", "SELECT ARRAY[1e300, 1e300, 1e300] FROM generate_series(1, 10)")):
            f[t] = server_dir / f"{t}.parquet"
            _run(dsn, f"CREATE TABLE {t} ({ddl}) USING pgcolumnar")
            _run(dsn, f"INSERT INTO {t} {gen}")
            r[f"{t} rows"] = _value_after(dsn, ["SET pgcolumnar.export_group_bytes = 1024"],
                                          f"SELECT pgcolumnar.export_parquet('{t}', '{f[t]}')")
        # #1843 review, second round: an int2 is written as a 4-byte INT32
        f["pq_w2"] = server_dir / "pq_w2.parquet"
        _run(dsn, "CREATE TABLE pq_w2 (id int2) USING pgcolumnar")
        _run(dsn, "INSERT INTO pq_w2 SELECT g FROM generate_series(1, 1000) g")
        r["pq_w2 rows"] = _value_after(dsn, ["SET pgcolumnar.export_group_bytes = 1024"],
                                       f"SELECT pgcolumnar.export_parquet('pq_w2', '{f['pq_w2']}')")
        # #1843 review, second round: the incoming row's estimate counts every column shape
        _run(dsn, "CREATE TYPE pq_rec AS (t text)")
        for t, ddl, gen in (("pq_wst", "id int, r pq_rec", "SELECT g, ROW(repeat('x', 10000))::pq_rec FROM generate_series(1, 40) g"),
                            ("pq_wtl", "id int, a text[]", "SELECT g, ARRAY[repeat('x', 5000), repeat('y', 5000)] FROM generate_series(1, 40) g"),
                            ("pq_wil", "id int, a int[]", "SELECT g, array_fill(7, ARRAY[2000]) FROM generate_series(1, 40) g"),
                            ("pq_wbig", "s text", "SELECT repeat('z', 2000) FROM generate_series(1, 3)")):
            f[t] = server_dir / f"{t}.parquet"
            _run(dsn, f"CREATE TABLE {t} ({ddl}) USING pgcolumnar")
            _run(dsn, f"INSERT INTO {t} {gen}")
            cap = 1024 if t.endswith("_wbig") else 65536
            r[f"{t} rows"] = _value_after(dsn, [f"SET pgcolumnar.export_group_bytes = {cap}"],
                                          f"SELECT pgcolumnar.export_parquet('{t}', '{f[t]}')")
        # #1843 review: each kept numeric or jsonb text reaches its own value
        _run(dsn, "CREATE TYPE pq_tpair AS (x numeric, y text)")
        _run(dsn, "CREATE TABLE pq_txt (id int, n numeric, a numeric[], j jsonb, c pq_tpair) USING pgcolumnar")
        _run(dsn, "INSERT INTO pq_txt SELECT g, CASE WHEN g % 10 = 0 THEN NULL ELSE g::numeric / 7 END, "
                  "CASE WHEN g % 7 = 0 THEN NULL ELSE ARRAY[g::numeric / 3, NULL, g * 1e20] END, "
                  "jsonb_build_object('k', g, 'v', g::numeric / 9), ROW(g::numeric / 11, 't' || g)::pq_tpair "
                  "FROM generate_series(1, 300) g")
        f["txt"] = server_dir / "txt.parquet"
        r["txt rows"] = _value(dsn, f"SELECT pgcolumnar.export_parquet('pq_txt', '{f['txt']}')")
        r["txt digest"] = _value(dsn, TXT_DIGEST)
        r["cap refusal"] = _set_refusal(dsn, "SET pgcolumnar.export_group_bytes = 268435457")
        r["dsn"] = dsn
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _read(pa, path):
    # pyarrow.parquet is a submodule: each caller has imported it through
    # require_module, which binds it on the package
    return pa.parquet.read_table(str(path))


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
    if tbl.schema.names != COLS:
        return "SCHEMA_NAMES_MISMATCH"
    ad = tbl.to_pydict()
    ar = {ad["id"][i]: tuple(ad[c][i] for c in COLS) for i in range(tbl.num_rows)}
    pg = {r[0]: (r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7],
                 None if r[8] is None else bytes.fromhex(r[8])) for r in oracle}
    # the generator's ids on both sides, so that two empty readings cannot agree, and
    # the file's own count, so that a row written twice cannot collapse into one
    if set(pg) != IDS or set(ar) != IDS or tbl.num_rows != len(IDS):
        return f"ROWS heap {len(pg)} parquet {tbl.num_rows}, not the generator's 80003"
    ok = all(all(_feq(a, b) for a, b in zip(ar[k], pg[k])) for k in pg)
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
    if set(pg) != {1, 2, 3, 4} or set(ar) != {1, 2, 3, 4} or t.num_rows != 4:
        return f"ROWS heap {sorted(pg)!r} parquet {ids!r}, not ids 1..4"
    return "MATCH" if ar == pg else f"MISMATCH {ar!r} {pg!r}"


def _ext_types(pa, path):
    d = {f.name: str(f.type) for f in _read(pa, path).schema}
    return f"{d['dt']}|{d['tm']}|{d['u']}|{d['num']}"


def _duckdb_value(db, sql):
    """-> the one value DuckDB answers `sql` with, or its failure as a query_error().

    `db` is the duckdb module that the test's require_module returned: a fixture or a
    module-level import could not decline by name. A connection of its own, in memory,
    so nothing outlives the reading."""
    try:
        con = db.connect()
        try:
            row = con.execute(sql).fetchone()
        finally:
            con.close()
    except db.Error as exc:
        return query_error(f"duckdb: {exc}")
    if row is None or row[0] is None:
        return query_error(f"duckdb answered no value to {sql!r}")
    return row[0]


def test_export_parquet_reports_every_row(pqx, expect):
    if expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet export verification") is None:
        return
    expect.text(pqx["rows"], "80003", "export_parquet rows written")


def test_parquet_values_match_the_heap_oracle(pqx, expect):
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet export verification")
    if pa is None or expect.require_module("pyarrow.parquet", name="pyarrow not available; skipping Parquet export verification") is None:
        return
    expect.text(_reading(_values, pa, pqx["files"]["t"], pqx["oracle"]), "MATCH",
                "parquet values match heap oracle")


def test_the_parquet_schema_has_the_expected_types(pqx, expect):
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet export verification")
    if pa is None or expect.require_module("pyarrow.parquet", name="pyarrow not available; skipping Parquet export verification") is None:
        return
    expect.text(_reading(_schema, pa, pqx["files"]["t"]), SCHEMA, "parquet schema")


def test_duckdb_counts_every_parquet_row(pqx, expect):
    if expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet export verification") is None:
        return
    db = expect.require_module("duckdb", name="duckdb row count")
    if db is None:
        return
    expect.num(_duckdb_value(db, f"SELECT count(*) FROM read_parquet('{pqx['files']['t']}')"), 80003,
               "duckdb row count")


def test_duckdb_counts_the_generators_non_null_b(pqx, expect):
    if expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet export verification") is None:
        return
    db = expect.require_module("duckdb", name="duckdb non-null count(b)")
    if db is None:
        return
    # 80000 generated rows plus the two edge rows with a b, from the generator: t_col is
    # the table under test, so its own count agreed with whatever it lost (#1341)
    expect.num(_duckdb_value(db, f"SELECT count(b) FROM read_parquet('{pqx['files']['t']}')"), 80002,
               "duckdb non-null count(b)")


def test_an_empty_table_exports_no_parquet_rows(pqx, expect):
    if expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet export verification") is None:
        return
    expect.text(pqx["empty rows"], "0", "empty export rows written")


def test_an_empty_table_writes_a_readable_parquet_file(pqx, expect):
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet export verification")
    if pa is None or expect.require_module("pyarrow.parquet", name="pyarrow not available; skipping Parquet export verification") is None:
        return
    expect.text(_reading(_empty, pa, pqx["files"]["e"]), "0 a,b", "empty parquet readable")


def test_parquet_export_refuses_a_heap_table(pqx, expect):
    if expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet export verification") is None:
        return
    expect.text(pqx["non-columnar"], f"refused: {NOT_COLUMNAR}", "reject non-columnar table")


def test_parquet_export_refuses_an_unsupported_type(pqx, expect):
    if expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet export verification") is None:
        return
    expect.text(pqx["unsupported"], f"refused: {UNSUPPORTED}", "reject unsupported type")


def test_the_extended_parquet_export_reports_every_row(pqx, expect):
    if expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet export verification") is None:
        return
    expect.text(pqx["ext rows"], "4", "extended export rows written")


def test_extended_parquet_values_match_the_heap_oracle(pqx, expect):
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet export verification")
    if pa is None or expect.require_module("pyarrow.parquet", name="pyarrow not available; skipping Parquet export verification") is None:
        return
    expect.text(_reading(_ext_values, pa, pqx["files"]["ext"], pqx["ext oracle"]), "MATCH",
                "extended values match heap oracle")


def test_the_extended_parquet_types_are_the_mapped_ones(pqx, expect):
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet export verification")
    if pa is None or expect.require_module("pyarrow.parquet", name="pyarrow not available; skipping Parquet export verification") is None:
        return
    expect.text(_reading(_ext_types, pa, pqx["files"]["ext"]), EXT_TYPES, "extended parquet types")


def test_duckdb_reads_the_parquet_decimal(pqx, expect):
    if expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet export verification") is None:
        return
    db = expect.require_module("duckdb", name="duckdb reads decimal")
    if db is None:
        return
    # cast in DuckDB, so the reading is text as the CLI printed it
    expect.text(_duckdb_value(db, f"SELECT num::VARCHAR FROM read_parquet('{pqx['files']['ext']}') WHERE id=1"),
                "12345.6789", "duckdb reads decimal")


def test_duckdb_counts_every_extended_parquet_row(pqx, expect):
    if expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet export verification") is None:
        return
    db = expect.require_module("duckdb", name="duckdb extended row count")
    if db is None:
        return
    expect.num(_duckdb_value(db, f"SELECT count(*) FROM read_parquet('{pqx['files']['ext']}')"), 4,
               "duckdb extended row count")


def _groups(pa, path):
    """-> "rows|n1,n2,..." from the file's footer: the rows in each row group."""
    md = pa.parquet.ParquetFile(str(path)).metadata
    return f"{md.num_rows}|" + ",".join(str(md.row_group(i).num_rows) for i in range(md.num_row_groups))


def _digest(pa, path):
    """-> "id:length:md5 ..." by id, as BYTES_DIGEST reads the table."""
    t = _read(pa, path).to_pydict()
    return " ".join(f"{i}:{len(v)}:{hashlib.md5(v.encode()).hexdigest()}" for i, v in sorted(zip(t["id"], t["s"])))


def test_a_row_group_is_cut_at_the_byte_cap(pqx, expect):
    """#1811: 40 rows that each buffer 10010 bytes, under a 65536-byte cap. The writer
    starts a new group before a row that would pass the cap, so groups hold 6 rows."""
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet export verification")
    if pa is None or expect.require_module("pyarrow.parquet", name="pyarrow not available; skipping Parquet export verification") is None:
        return
    expect.text(f"{pqx['bytes rows']}|{_reading(_groups, pa, pqx['files']['bytes'])}", "40|40|6,6,6,6,6,6,4",
                "a row group is cut at the byte cap: 40 rows of 10000 bytes under a 65536-byte cap make groups of 6,6,6,6,6,6,4")


def test_the_byte_cut_parquet_file_reads_back_exactly(pqx, expect):
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet export verification")
    if pa is None or expect.require_module("pyarrow.parquet", name="pyarrow not available; skipping Parquet export verification") is None:
        return
    expect.text(_reading(_digest, pa, pqx["files"]["bytes"]), pqx["bytes digest"],
                "and the byte-cut file reads back every row exactly, by id, length and md5")


def test_the_group_byte_cap_refuses_more_than_256_mib(pqx, expect):
    if expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet export verification") is None:
        return
    expect.text(pqx["cap refusal"], CAP_REFUSAL, "export_group_bytes refuses a value above its 256 MiB maximum")


def test_over_1gib_of_one_column_exports_in_several_row_groups(pqx, expect):
    """#1811's repro, opt-in as the shell's arm is: about 1.1 GB in the table and in the
    file. Each row buffers 500010 bytes, so a 256 MiB group holds 536 rows."""
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet export verification")
    if pa is None or expect.require_module("pyarrow.parquet", name="pyarrow not available; skipping Parquet export verification") is None:
        return
    if os.environ.get("PGC_RUN_OVER_1GB", "0") != "1":
        expect.cannot_run("UNMET_PRECONDITION",
                          "PGC_RUN_OVER_1GB is not 1; the arm needs several GB of memory and disk",
                          name=OVER_NAME)
        return
    dsn = pqx["dsn"]
    path = pqx["files"]["t"].parent / "huge.parquet"
    _run(dsn, "CREATE TABLE pq_huge (id int, s text) USING pgcolumnar")
    _run(dsn, "SELECT pgcolumnar.set_options('pq_huge', stripe_row_limit => 1000)")
    _run(dsn, "INSERT INTO pq_huge SELECT g, repeat('x', 500000) FROM generate_series(1, 2200) g")
    try:
        got = _value_after(dsn, ["SET statement_timeout = '900s'"],
                           f"SELECT pgcolumnar.export_parquet('pq_huge', '{path}')")
        expect.text(f"{got}|{_reading(_groups, pa, path)}", "2200|2200|536,536,536,536,56", OVER_NAME)
    finally:
        path.unlink(missing_ok=True)
        _run(dsn, "DROP TABLE pq_huge")


WIDE_NAME = "a row of 8200 numerics of 1e100000 (98 KB stored, 820 MB as text) after a 260 MB row exports whole, in 2 row groups of 1"


def _cut(fix, pa, table):
    return f"{fix[table + ' rows']}|{_reading(_groups, pa, fix['files'][table])}"


def test_a_row_group_cut_counts_an_int4s_level_byte(pqx, expect):
    """#1843 review: an int4 buffers 4 bytes and a level byte, so a 1024-byte group
    holds 204 rows (1020 bytes), not 205."""
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet export verification")
    if pa is None or expect.require_module("pyarrow.parquet", name="pyarrow not available; skipping Parquet export verification") is None:
        return
    expect.text(_cut(pqx, pa, "pq_w4"), "1000|1000|204,204,204,204,184",
                "the byte cut counts an int4's level byte: 1000 int4 rows under a 1024-byte cap make groups of 204,204,204,204,184")


def test_a_row_group_cut_counts_a_numerics_text_form(pqx, expect):
    """#1843 review: 1e1000 stores in a few bytes but is written as 1001 digits."""
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet export verification")
    if pa is None or expect.require_module("pyarrow.parquet", name="pyarrow not available; skipping Parquet export verification") is None:
        return
    expect.text(_cut(pqx, pa, "pq_wnum"), "10|10|1,1,1,1,1,1,1,1,1,1",
                "the byte cut counts a numeric's text form: 10 rows of 1e1000 under a 1024-byte cap make 10 groups of 1")


def test_a_row_group_cut_counts_a_lists_elements_as_written(pqx, expect):
    """#1843 review: three 1e300 elements are written as 301 digits each."""
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet export verification")
    if pa is None or expect.require_module("pyarrow.parquet", name="pyarrow not available; skipping Parquet export verification") is None:
        return
    expect.text(_cut(pqx, pa, "pq_wlist"), "10|10|1,1,1,1,1,1,1,1,1,1",
                "the byte cut counts a list's elements as written: 10 rows of three 1e300 under a 1024-byte cap make 10 groups of 1")


def test_a_row_group_cut_counts_an_int2_as_the_int32_it_is_written_as(pqx, expect):
    """#1843 review, second round: an int2 is written as a 4-byte INT32, so it
    buffers 4 bytes and a level byte, as an int4 does."""
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet export verification")
    if pa is None or expect.require_module("pyarrow.parquet", name="pyarrow not available; skipping Parquet export verification") is None:
        return
    expect.text(_cut(pqx, pa, "pq_w2"), "1000|1000|204,204,204,204,184",
                "the byte cut counts an int2 as the INT32 it is written as: 1000 int2 rows under a 1024-byte cap make groups of 204,204,204,204,184")


def test_a_row_group_cut_counts_a_composites_fields_as_written(pqx, expect):
    """#1843 review, second round: a composite row counts its field as written, so a
    10 KB field cannot join a nearly full group."""
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet export verification")
    if pa is None or expect.require_module("pyarrow.parquet", name="pyarrow not available; skipping Parquet export verification") is None:
        return
    expect.text(_cut(pqx, pa, "pq_wst"), "40|40|6,6,6,6,6,6,4",
                "the byte cut counts a composite's fields as written: 40 rows of a 10000-byte text field under a 65536-byte cap make groups of 6,6,6,6,6,6,4")


def test_a_row_group_cut_counts_a_text_lists_elements_as_written(pqx, expect):
    """#1843 review, second round: a text[] row counts each element as written."""
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet export verification")
    if pa is None or expect.require_module("pyarrow.parquet", name="pyarrow not available; skipping Parquet export verification") is None:
        return
    expect.text(_cut(pqx, pa, "pq_wtl"), "40|40|6,6,6,6,6,6,4",
                "the byte cut counts a text list's elements as written: 40 rows of two 5000-byte texts under a 65536-byte cap make groups of 6,6,6,6,6,6,4")


def test_a_row_group_cut_counts_an_int_lists_elements_as_written(pqx, expect):
    """#1843 review, second round: an int[] row counts each fixed-width element as written."""
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet export verification")
    if pa is None or expect.require_module("pyarrow.parquet", name="pyarrow not available; skipping Parquet export verification") is None:
        return
    expect.text(_cut(pqx, pa, "pq_wil"), "40|40|5,5,5,5,5,5,5,5",
                "the byte cut counts an int list's elements as written: 40 rows of 2000 int4 under a 65536-byte cap make groups of 5,5,5,5,5,5,5,5")


def test_a_row_larger_than_the_byte_cap_goes_in_a_group_of_its_own(pqx, expect):
    """#1843 review: a row larger than the cap is written alone, with no empty group
    before it."""
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet export verification")
    if pa is None or expect.require_module("pyarrow.parquet", name="pyarrow not available; skipping Parquet export verification") is None:
        return
    expect.text(_cut(pqx, pa, "pq_wbig"), "3|3|1,1,1",
                "a row larger than the byte cap goes in a group of its own: 3 rows of 2000 bytes under a 1024-byte cap make groups of 1,1,1")


def test_a_row_far_wider_as_written_than_stored_goes_in_a_group_of_its_own(pqx, expect):
    """#1843 review, opt-in as the shell's arm is. Row 1 holds 260 MB of text; row 2
    holds 8200 numerics of 1e100000, about 98 KB stored but 820 MB as text."""
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet export verification")
    if pa is None or expect.require_module("pyarrow.parquet", name="pyarrow not available; skipping Parquet export verification") is None:
        return
    if os.environ.get("PGC_RUN_OVER_1GB", "0") != "1":
        expect.cannot_run("UNMET_PRECONDITION",
                          "PGC_RUN_OVER_1GB is not 1; the arm needs several GB of memory and disk",
                          name=WIDE_NAME)
        return
    dsn = pqx["dsn"]
    path = pqx["files"]["t"].parent / "wide.parquet"
    _run(dsn, "CREATE TABLE pq_wide (id int, t text, n numeric[]) USING pgcolumnar")
    _run(dsn, "INSERT INTO pq_wide VALUES (1, repeat('a', 260000000), NULL)")
    _run(dsn, "INSERT INTO pq_wide SELECT 2, NULL, array_fill(1e100000::numeric, ARRAY[8200])")
    try:
        got = _value_after(dsn, ["SET statement_timeout = '900s'"],
                           f"SELECT pgcolumnar.export_parquet('pq_wide', '{path}')")
        expect.text(f"{got}|{_reading(_groups, pa, path)}", "2|2|1,1", WIDE_NAME)
    finally:
        path.unlink(missing_ok=True)
        _run(dsn, "DROP TABLE pq_wide")


def _texts(pa, path):
    """-> "id|n|a|j|x|y ..." by id, as TXT_DIGEST prints the table."""
    t = _read(pa, path).to_pydict()

    def arr(v):
        return "NULL" if v is None else ",".join("N" if e is None else e for e in v)
    return " ".join(f"{i}|{'NULL' if n is None else n}|{arr(a)}|{j}|{c['x']}|{c['y']}"
                    for i, n, a, j, c in sorted(zip(t["id"], t["n"], t["a"], t["j"], t["c"])))


def test_every_kept_text_reaches_its_own_value_in_the_file(pqx, expect):
    """#1843 review: the writer converts a numeric or jsonb once, while it counts the
    row, and keeps the text for the append. Scalars, list elements and a composite's
    field each get their own text."""
    pa = expect.require_module("pyarrow", name="pyarrow not available; skipping Parquet export verification")
    if pa is None or expect.require_module("pyarrow.parquet", name="pyarrow not available; skipping Parquet export verification") is None:
        return
    expect.text(f"{pqx['txt rows']}|{_reading(_texts, pa, pqx['files']['txt'])}", f"300|{pqx['txt digest']}",
                "every numeric and jsonb text reaches its own value: 300 rows of numeric, numeric[], jsonb and a composite read back from the file as PostgreSQL prints them")
