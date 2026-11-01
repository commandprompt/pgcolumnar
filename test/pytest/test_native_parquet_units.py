"""Parquet TIME/TIMESTAMP unit handling (Phase G). The pytest twin of
`test/native_parquet_units.sh`, every check by the shell's name.

Parquet stores a time unit beside TIME and TIMESTAMP columns, in the deprecated
ConvertedType or in the LogicalType union, which can also say NANOS. PostgreSQL stores
microseconds, so a reader that ignores the unit returns values off by a factor of 1000,
in range, so nothing errors. pyarrow writes ConvertedType for millis and micros but only
LogicalType for nanos, so both metadata paths run.

Two oracles are literals or the generator, never the file read back through the reader
under test: the nanos truncation checks compare with the toward-zero microseconds as
text (a second file read the same way agreed on two failed reads), and the two-group
millis file is compared with the hourly series the fixture wrote (a mirror filled
through read_parquet agreed with a decode that shifted rows to the next dictionary
entry). Every reading returns a failure as a value, so a failed query fails its own
check by name.

INT96 (#1870), the legacy 12-byte timestamp, is advised as timestamptz and reads as an
instant, into timestamptz and into timestamp, through read_parquet and import_parquet.

ON A PRIVATE DATABASE. The shell skips whole without pyarrow, so every test declines
under the shell's skip name before it asks for the fixture: the writer needs pyarrow,
and it runs as a subprocess so this module imports none. The files live in
`server_dir`.
"""
import re
import subprocess
import sys

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_native_parquet_units"

# the same writer the shell runs, less the reference files the literals replaced
WRITER = r'''
import sys, pyarrow as pa, pyarrow.parquet as pq
W = sys.argv[1]
secs = 1784851200
for unit, mult, ver in [("ms", 10**3, "1.0"), ("us", 10**6, "1.0"), ("ns", 10**9, "2.6")]:
    pq.write_table(pa.table({"t": pa.array([secs * mult], pa.int64()).cast(pa.timestamp(unit))}),
                   f"{W}/ts_{unit}.parquet", version=ver)
pq.write_table(pa.table({"t": pa.array([3723000004], pa.int64()).cast(pa.time64('us'))}),
               f"{W}/time_us.parquet")
pq.write_table(pa.table({"t": pa.array([3723000], pa.int32()).cast(pa.time32('ms'))}),
               f"{W}/time_ms.parquet")
pq.write_table(pa.table({"t": pa.array([9223372036854776], pa.int64()).cast(pa.timestamp('ms'))}),
               f"{W}/ms_overflow.parquet")
for tag, ns in [("frac", 1784851200_123_456_789), ("neg", -1784851200_123_456_789)]:
    pq.write_table(pa.table({"t": pa.array([ns], pa.int64()).cast(pa.timestamp('ns'))}),
                   f"{W}/tsns_{tag}.parquet", version="2.6")
HOUR = 3600000
g0 = [1704067200000 + i * HOUR for i in range(1000)]
g1 = [1751328000000 + i * HOUR for i in range(1000)]
pq.write_table(pa.table({"t": pa.array(g0 + g1, pa.int64()).cast(pa.timestamp('ms'))}),
               f"{W}/ms_groups.parquet", row_group_size=1000)
assert pq.ParquetFile(f"{W}/ms_groups.parquet").num_row_groups == 2
pq.write_table(pa.table({"t": pa.array([1784851200_000_000_000, 1784851200_123_456_789,
                                        -1784851200_123_456_789, None],
                                       pa.timestamp("ns", tz="UTC"))}),
               f"{W}/int96.parquet", use_deprecated_int96_timestamps=True)
'''

# the premise reader: pyarrow's physical type for the INT96 file (#1870)
INT96_READER = ("import sys, pyarrow.parquet as pq; "
                "print(pq.ParquetFile(sys.argv[1]).schema.column(0).physical_type)")
I96_UTC = "1913-06-10 23:59:59.876543,2026-07-24 00:00:00,2026-07-24 00:00:00.123456,null"
I96_TOKYO = "1913-06-11 08:59:59.876543,2026-07-24 09:00:00,2026-07-24 09:00:00.123456,null"

# the hourly series the fixture wrote, built in SQL: the oracle for the two-group file
GENERATOR = ("SELECT timestamp '2024-01-01' + g * interval '1 hour' AS t FROM generate_series(0, 999) g "
             "UNION ALL SELECT timestamp '2025-07-01' + g * interval '1 hour' FROM generate_series(0, 999) g")


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _run(dsn, sql):
    """-> None when the statement ran, or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return None


def _value(dsn, sql, settings=()):
    """-> the first column of the first row as text, or the failure as a query_error().
    `settings` run first on the same connection, as a SET before the query."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for st in settings:
                conn.execute(st)
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    if row is None or row[0] is None:
        return query_error(f"no value from {sql}")
    return str(row[0])


def _set_hash(dsn, sql):
    """-> lib.sh's pgc_set_hash, on the server; a failed query is a query_error()."""
    return _value(dsn, "SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), 'EMPTY') "
                       f"FROM (SELECT _row::text AS t FROM ({sql}) _row) _s")


def _sqlstate(dsn, sql):
    """-> the SQLSTATE the statement raised, or "NO ERROR", as the shell's pgc_try."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        return exc.sqlstate or query_error(_message(exc))
    return "NO ERROR"


def _skipped(dsn, pred):
    """-> the scan's Row Groups Skipped for `pred`, or a query_error() when unread."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            plan = "\n".join(r[0] for r in conn.execute(
                "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) "
                f"SELECT count(*) FROM fmsg WHERE {pred}").fetchall())
    except psycopg.Error as exc:
        return query_error(_message(exc))
    m = re.search(r"Row Groups Skipped: (\d+)", plan)
    return m.group(1) if m else query_error("no 'Row Groups Skipped' line in the plan")


@pytest.fixture(scope="module")
def pqu(pgc_cluster, server_dir):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        w = server_dir / "pqu"
        w.mkdir(exist_ok=True)
        w.chmod(0o777)
        subprocess.run([sys.executable, "-c", WRITER, str(w)], capture_output=True, text=True)
        for f in w.glob("*.parquet"):
            f.chmod(0o644)
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        _run(dsn, "CREATE SERVER pq FOREIGN DATA WRAPPER pgcolumnar_parquet")
        for u in ("ms", "us", "ns"):
            _run(dsn, f"CREATE FOREIGN TABLE fts_{u} (t timestamp) SERVER pq OPTIONS (path '{w}/ts_{u}.parquet')")
        _run(dsn, f"CREATE FOREIGN TABLE ftime (t time) SERVER pq OPTIONS (path '{w}/time_us.parquet')")
        _run(dsn, f"CREATE FOREIGN TABLE ftime_ms (t time) SERVER pq OPTIONS (path '{w}/time_ms.parquet')")
        r = {}
        for u in ("ms", "us", "ns"):
            r[f"ts {u}"] = _value(dsn, f"SELECT t::text FROM fts_{u}")
        r["time us"] = _value(dsn, "SELECT t::text FROM ftime")
        r["time ms"] = _value(dsn, "SELECT t::text FROM ftime_ms")
        for key, f in (("advice time ms", "time_ms"), ("advice ms", "ts_ms"),
                       ("advice us", "ts_us"), ("advice ns", "ts_ns")):
            r[key] = _value(dsn, f"SELECT data_type FROM pgcolumnar.parquet_schema('{w}/{f}.parquet')")
        r["ns bigint"] = _value(dsn, f"SELECT t::text FROM pgcolumnar.read_parquet('{w}/ts_ns.parquet') AS t(t int8)")
        r["overflow"] = _sqlstate(dsn, f"SELECT * FROM pgcolumnar.read_parquet('{w}/ms_overflow.parquet') "
                                       "AS t(t timestamp)")
        r["read_parquet"] = _value(dsn, f"SELECT t::text FROM pgcolumnar.read_parquet('{w}/ts_ms.parquet') "
                                        "AS t(t timestamp)")
        _run(dsn, "CREATE TABLE imp_ms (t timestamp)")
        _run(dsn, f"SELECT pgcolumnar.import_parquet('imp_ms'::regclass, '{w}/ts_ms.parquet')")
        r["import"] = _value(dsn, "SELECT t::text FROM imp_ms")
        r["hash ms"] = _set_hash(dsn, "SELECT t FROM fts_ms")
        r["hash us"] = _set_hash(dsn, "SELECT t FROM fts_us")
        r["hash ns"] = _set_hash(dsn, "SELECT t FROM fts_ns")
        for tag in ("frac", "neg"):
            r[f"trunc {tag}"] = _value(dsn, f"SELECT t::text FROM pgcolumnar.read_parquet('{w}/tsns_{tag}.parquet') "
                                            "AS t(t timestamp)")
        # ---- INT96 timestamps (#1870) ----
        i96 = f"{w}/int96.parquet"
        pre = subprocess.run([sys.executable, "-c", INT96_READER, i96], capture_output=True, text=True)
        r["i96 premise"] = (pre.stdout.strip() if pre.returncode == 0
                            else query_error((pre.stderr.strip().splitlines() or ["no output"])[-1]))
        r["i96 advice"] = _value(dsn, f"SELECT coalesce(data_type, '<null>') FROM pgcolumnar.parquet_schema('{i96}')")
        utc = "string_agg(coalesce((t AT TIME ZONE 'UTC')::text, 'null'), ',' ORDER BY t NULLS LAST)"
        r["i96 tz"] = _value(dsn, f"SELECT {utc} FROM pgcolumnar.read_parquet('{i96}') AS t(t timestamptz)")
        r["i96 ts"] = _value(dsn, "SELECT string_agg(coalesce(t::text, 'null'), ',' ORDER BY t NULLS LAST) "
                                  f"FROM pgcolumnar.read_parquet('{i96}') AS t(t timestamp)",
                             settings=("SET timezone = 'Asia/Tokyo'",))
        _run(dsn, "CREATE TABLE imp_i96 (t timestamptz)")
        imp = _value(dsn, f"SELECT pgcolumnar.import_parquet('imp_i96'::regclass, '{i96}')")
        r["i96 import"] = (imp if not isinstance(imp, str) or imp.startswith("QUERY_ERROR")
                           else imp + ":" + str(_value(dsn, f"SELECT {utc} FROM imp_i96")))
        _run(dsn, f"CREATE FOREIGN TABLE fmsg (t timestamp) SERVER pq OPTIONS (path '{w}/ms_groups.parquet')")
        r["skipped"] = _skipped(dsn, "t >= timestamp '2025-06-01'")
        r["file"] = _set_hash(dsn, "SELECT t FROM fmsg")
        r["file oracle"] = _set_hash(dsn, GENERATOR)
        r["pushdown"] = _set_hash(dsn, "SELECT t FROM fmsg WHERE t >= timestamp '2025-06-01'")
        r["pushdown oracle"] = _set_hash(dsn, f"SELECT t FROM ({GENERATOR}) g WHERE t >= timestamp '2025-06-01'")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_ts_ms_decodes_to_the_right_instant(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet unit suite needs it") is None:
        return
    r = request.getfixturevalue("pqu")
    expect.text(r["ts ms"], "2026-07-24 00:00:00", "ts_ms decodes to the right instant")


def test_ts_us_decodes_to_the_right_instant(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet unit suite needs it") is None:
        return
    r = request.getfixturevalue("pqu")
    expect.text(r["ts us"], "2026-07-24 00:00:00", "ts_us decodes to the right instant")


def test_ts_ns_decodes_to_the_right_instant(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet unit suite needs it") is None:
        return
    r = request.getfixturevalue("pqu")
    expect.text(r["ts ns"], "2026-07-24 00:00:00", "ts_ns decodes to the right instant")


def test_time_micros_decodes_to_the_right_time(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet unit suite needs it") is None:
        return
    r = request.getfixturevalue("pqu")
    expect.text(r["time us"], "01:02:03.000004", "time micros decodes to the right time")


def test_time_millis_decodes_to_the_right_time(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet unit suite needs it") is None:
        return
    r = request.getfixturevalue("pqu")
    expect.text(r["time ms"], "01:02:03", "time millis decodes to the right time")


def test_parquet_schema_advises_time_for_time_millis(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet unit suite needs it") is None:
        return
    r = request.getfixturevalue("pqu")
    expect.text(r["advice time ms"], "time without time zone", "parquet_schema advises time for TIME_MILLIS")


def test_parquet_schema_advises_timestamp_for_millis(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet unit suite needs it") is None:
        return
    r = request.getfixturevalue("pqu")
    expect.text(r["advice ms"], "timestamp without time zone", "parquet_schema advises timestamp for millis")


def test_parquet_schema_advises_timestamp_for_micros(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet unit suite needs it") is None:
        return
    r = request.getfixturevalue("pqu")
    expect.text(r["advice us"], "timestamp without time zone", "parquet_schema advises timestamp for micros")


def test_parquet_schema_advises_bigint_for_nanos(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet unit suite needs it") is None:
        return
    r = request.getfixturevalue("pqu")
    expect.text(r["advice ns"], "bigint", "parquet_schema advises bigint for nanos (lossless)")


def test_nanos_read_as_bigint_is_exact(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet unit suite needs it") is None:
        return
    r = request.getfixturevalue("pqu")
    expect.text(r["ns bigint"], "1784851200000000000", "nanos read as bigint is exact")


def test_millis_to_micros_overflow_raises_22008(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet unit suite needs it") is None:
        return
    r = request.getfixturevalue("pqu")
    expect.text(r["overflow"], "22008", "millis-to-micros overflow raises 22008")


def test_read_parquet_honours_the_unit(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet unit suite needs it") is None:
        return
    r = request.getfixturevalue("pqu")
    expect.text(r["read_parquet"], "2026-07-24 00:00:00", "read_parquet honours the unit")


def test_import_parquet_honours_the_unit(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet unit suite needs it") is None:
        return
    r = request.getfixturevalue("pqu")
    expect.text(r["import"], "2026-07-24 00:00:00", "import_parquet honours the unit")


def test_millis_equals_micros(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet unit suite needs it") is None:
        return
    r = request.getfixturevalue("pqu")
    expect.text(r["hash ms"], r["hash us"], "millis == micros")


def test_nanos_equals_micros(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet unit suite needs it") is None:
        return
    r = request.getfixturevalue("pqu")
    expect.text(r["hash ns"], r["hash us"], "nanos == micros")


def test_nanos_sub_us_truncates_toward_zero_frac(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet unit suite needs it") is None:
        return
    r = request.getfixturevalue("pqu")
    expect.text(r["trunc frac"], "2026-07-24 00:00:00.123456", "nanos sub-us truncates toward zero [frac]")


def test_nanos_sub_us_truncates_toward_zero_neg(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet unit suite needs it") is None:
        return
    r = request.getfixturevalue("pqu")
    expect.text(r["trunc neg"], "1913-06-10 23:59:59.876544", "nanos sub-us truncates toward zero [neg]")


def test_millis_scaled_stats_skip_the_non_matching_group(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet unit suite needs it") is None:
        return
    r = request.getfixturevalue("pqu")
    expect.text(r["skipped"], "1", "millis-scaled stats skip the non-matching group")


def test_the_millis_file_reads_back_exactly_as_written(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet unit suite needs it") is None:
        return
    r = request.getfixturevalue("pqu")
    expect.text(r["file"], r["file oracle"], "the millis file reads back exactly as written")


def test_millis_pushdown_result_equals_the_oracle(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet unit suite needs it") is None:
        return
    r = request.getfixturevalue("pqu")
    expect.text(r["pushdown"], r["pushdown oracle"], "millis pushdown result == oracle")


def test_int96_premise_pyarrow_wrote_int96(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet unit suite needs it") is None:
        return
    r = request.getfixturevalue("pqu")
    expect.text(r["i96 premise"], "INT96", "premise: pyarrow wrote the INT96 file as INT96")


def test_parquet_schema_advises_timestamptz_for_int96(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet unit suite needs it") is None:
        return
    r = request.getfixturevalue("pqu")
    expect.text(r["i96 advice"], "timestamp with time zone", "parquet_schema advises timestamptz for INT96")


def test_int96_reads_into_timestamptz_as_the_stored_instant(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet unit suite needs it") is None:
        return
    r = request.getfixturevalue("pqu")
    expect.text(r["i96 tz"], I96_UTC, "INT96 reads into timestamptz as the stored instant")


def test_int96_reads_into_timestamp_as_the_session_wall_time(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet unit suite needs it") is None:
        return
    r = request.getfixturevalue("pqu")
    expect.text(r["i96 ts"], I96_TOKYO, "INT96 reads into timestamp as the wall time in the session zone")


def test_import_parquet_reads_int96_into_timestamptz(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet unit suite needs it") is None:
        return
    r = request.getfixturevalue("pqu")
    expect.text(r["i96 import"], "4:" + I96_UTC, "import_parquet reads INT96 into timestamptz")
