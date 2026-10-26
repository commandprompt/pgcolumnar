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
import math

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
