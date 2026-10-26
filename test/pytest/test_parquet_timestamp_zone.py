"""A Parquet timestamp read into the other PostgreSQL timestamp type follows PostgreSQL's
cast semantics (#1706 item 2). The pytest twin of `test/parquet_timestamp_zone.sh`, every
check by the shell's name.

Parquet marks a timestamp column isAdjustedToUTC: true for an instant, false for a wall
time. In a New York session, a naive file read into timestamptz must be its wall time in
the session zone, and an adjusted file read into timestamp the session-zone wall time of
its instant, each equal to PostgreSQL's own cast; the same-kind reads are the controls. An
export and import round trip keeps both types (it needs #1707's exporter). A row group that
spans the 2024-03-10 spring-forward gap must not be skipped on its converted min and max,
which need not bound its converted rows. The naive file read again in Tokyo must be the cast
in that zone, a different instant from New York's; the repeated hour of New York's 2024-11-03
fall-back must resolve as the cast resolves it; and a file carrying only the legacy
TIMESTAMP_MICROS converted type (pgcolumnar's own export before #1707, checked in as
`test/fixtures/parquet/legacy_timestamp_micros.parquet`) is read as the instant the
Parquet spec defines it to be.

The files are written by `python3` with pyarrow, as the shell writes them, so no test here
imports pyarrow: each declines by the shell's skip name first, and only then asks for the
module fixture. ON A PRIVATE DATABASE, every session at TimeZone America/New_York.
"""
import pathlib
import shutil
import subprocess
import sys

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_parquet_timestamp_zone"
ZONE = "-c TimeZone=America/New_York"
TOKYO = "-c TimeZone=Asia/Tokyo"
LEGACY = (pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "parquet"
          / "legacy_timestamp_micros.parquet")

WRITER = r'''
import sys, datetime as dt, pyarrow as pa, pyarrow.parquet as pq
w = sys.argv[1]
noon = dt.datetime(2024, 6, 1, 12, 0)
pq.write_table(pa.table({"t": pa.array([noon], pa.timestamp("us"))}), f"{w}/naive.parquet")
pq.write_table(pa.table({"t": pa.array([noon], pa.timestamp("us", tz="UTC"))}), f"{w}/adjusted.parquet")
gap = [dt.datetime(2024, 3, 10, 2, 0), dt.datetime(2024, 3, 10, 2, 30), dt.datetime(2024, 3, 10, 3, 0)]
pq.write_table(pa.table({"t": pa.array(gap, pa.timestamp("us"))}), f"{w}/gap.parquet", row_group_size=3)
fb = [dt.datetime(2024, 11, 3, 1, 30)]
pq.write_table(pa.table({"t": pa.array(fb, pa.timestamp("us"))}), f"{w}/fallback.parquet")
a = lambda f: pq.ParquetFile(f).schema_arrow.field("t").type.tz
print(a(f"{w}/naive.parquet"), a(f"{w}/adjusted.parquet"))
'''

# pyarrow derives a logical type from a converted type and says so in this flag
LEGACY_PREMISE = r'''
import sys, pyarrow.parquet as pq
c = pq.ParquetFile(sys.argv[1]).metadata.schema.column(0)
print(c.converted_type, "is_from_converted_type=true" in str(c.logical_type))
'''


def _failed(exc):
    return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _value(dsn, sql, zone=ZONE):
    """-> the first column of the first row as text, in a New York session unless
    `zone` names another, or query_error() carrying the failure."""
    try:
        with psycopg.connect(dsn, autocommit=True, options=zone) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return _failed(exc)
    return "" if row is None or row[0] is None else str(row[0])


def _run(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True, options=ZONE) as conn:
            conn.execute(sql)
    except psycopg.Error as exc:
        return _failed(exc)
    return None


@pytest.fixture(scope="module")
def ptz(pgc_cluster, server_dir):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        w = server_dir
        wrote = subprocess.run([sys.executable, "-c", WRITER, str(w)], capture_output=True, text=True)
        shutil.copyfile(LEGACY, w / "legacy.parquet")
        for f in ("naive", "adjusted", "gap", "fallback", "legacy"):
            if (w / f"{f}.parquet").exists():
                (w / f"{f}.parquet").chmod(0o644)
        r = {"tz of the files": wrote.stdout.strip() if wrote.returncode == 0
             else query_error(f"the writer failed: {wrote.stderr.strip().splitlines()[-1:]}")}
        legacy = subprocess.run([sys.executable, "-c", LEGACY_PREMISE, str(w / "legacy.parquet")],
                                capture_output=True, text=True)
        r["legacy premise"] = (legacy.stdout.strip() if legacy.returncode == 0 else
                               query_error(f"the reader failed: {legacy.stderr.strip().splitlines()[-1:]}"))
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        _run(dsn, "CREATE SERVER pq FOREIGN DATA WRAPPER pgcolumnar_parquet")
        for t, typ in (("n_ts", "timestamp"), ("n_tz", "timestamptz"),
                       ("a_ts", "timestamp"), ("a_tz", "timestamptz")):
            _run(dsn, f"CREATE TABLE {t} (t {typ}) USING pgcolumnar")
            src = "naive" if t.startswith("n_") else "adjusted"
            _run(dsn, f"SELECT pgcolumnar.import_parquet('{t}', '{w}/{src}.parquet')")
        for t in ("n_ts", "n_tz", "a_ts", "a_tz"):
            r[t] = _value(dsn, f"SELECT t::text FROM {t}")
        r["n_tz cast"] = _value(dsn, "SELECT ((SELECT t FROM n_tz) = "
                                     "'2024-06-01 12:00'::timestamp::timestamptz)::text")
        r["a_ts cast"] = _value(dsn, "SELECT ((SELECT t FROM a_ts) = "
                                     "'2024-06-01 12:00+00'::timestamptz::timestamp)::text")
        _run(dsn, "CREATE TABLE rt_src (ts timestamp, tz timestamptz) USING pgcolumnar")
        _run(dsn, "INSERT INTO rt_src VALUES ('2024-06-01 12:00', '2024-06-01 12:00')")
        _run(dsn, "CREATE TABLE rt_dst (ts timestamp, tz timestamptz) USING pgcolumnar")
        _run(dsn, f"SELECT pgcolumnar.export_parquet('rt_src', '{w}/rt.parquet')")
        _run(dsn, f"SELECT pgcolumnar.import_parquet('rt_dst', '{w}/rt.parquet')")
        r["round trip"] = _value(dsn, "SELECT ts::text || ' | ' || tz::text FROM rt_dst")
        _run(dsn, f"CREATE FOREIGN TABLE f_gap (t timestamptz) SERVER pq OPTIONS (path '{w}/gap.parquet')")
        r["gap instants"] = _value(dsn, "SELECT string_agg(to_char(t AT TIME ZONE 'UTC', 'HH24:MI'), ',' "
                                        "ORDER BY t) FROM f_gap")
        r["gap count"] = _value(dsn, "SELECT count(*)::text FROM f_gap WHERE t > '2024-03-10 07:15+00'")
        _run(dsn, f"CREATE FOREIGN TABLE f_naive (t timestamptz) SERVER pq OPTIONS (path '{w}/naive.parquet')")
        r["tokyo"] = _value(dsn, "SELECT t::text || ' ' || (t = '2024-06-01 12:00'::timestamp::timestamptz)::text "
                                 "FROM f_naive", zone=TOKYO)
        r["tokyo differs"] = _value(dsn, "SELECT ((SELECT t FROM f_naive) <> (SELECT t FROM n_tz))::text",
                                    zone=TOKYO)
        _run(dsn, f"CREATE FOREIGN TABLE f_fb (t timestamptz) SERVER pq OPTIONS (path '{w}/fallback.parquet')")
        r["fall-back"] = _value(dsn, "SELECT t::text || ' ' || (t = '2024-11-03 01:30'::timestamp::timestamptz)::text "
                                     "FROM f_fb")
        _run(dsn, f"CREATE FOREIGN TABLE f_leg_ts (t timestamp) SERVER pq OPTIONS (path '{w}/legacy.parquet')")
        _run(dsn, f"CREATE FOREIGN TABLE f_leg_tz (t timestamptz) SERVER pq OPTIONS (path '{w}/legacy.parquet')")
        r["legacy ts"] = _value(dsn, "SELECT t::text || ' ' || (t = '2024-06-01 12:00+00'::timestamptz::timestamp)::text "
                                     "FROM f_leg_ts")
        r["legacy tz"] = _value(dsn, "SELECT t::text FROM f_leg_tz")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _readings(request, expect):
    """Decline by the shell's skip name first; only then build the fixture."""
    if expect.require_module("pyarrow.parquet",
                             name="pyarrow not available; Parquet timestamp zone suite needs it") is None:
        return None
    return request.getfixturevalue("ptz")


def test_ptz_pyarrow_wrote_one_naive_and_one_adjusted_file(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["tz of the files"], "None UTC",
                    "premise: pyarrow wrote the naive file without isAdjustedToUTC and the other with it")


def test_ptz_a_naive_file_into_timestamptz_is_its_session_wall_time(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["n_tz"], "2024-06-01 12:00:00-04",
                    "a naive file read into timestamptz is its wall time in the session zone")


def test_ptz_the_naive_read_equals_the_cast_from_timestamp(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["n_tz cast"], "true", "and that is what PostgreSQL's cast from timestamp gives")


def test_ptz_an_adjusted_file_into_timestamp_is_the_session_wall_time(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["a_ts"], "2024-06-01 08:00:00",
                    "an adjusted file read into timestamp is the session-zone wall time of its instant")


def test_ptz_the_adjusted_read_equals_the_cast_from_timestamptz(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["a_ts cast"], "true", "and that is what PostgreSQL's cast from timestamptz gives")


def test_ptz_a_naive_file_into_timestamp_keeps_its_wall_time(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["n_ts"], "2024-06-01 12:00:00",
                    "control: a naive file read into timestamp keeps its wall time")


def test_ptz_an_adjusted_file_into_timestamptz_keeps_its_instant(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["a_tz"], "2024-06-01 08:00:00-04",
                    "control: an adjusted file read into timestamptz keeps its instant")


def test_ptz_an_export_import_round_trip_keeps_both_types(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["round trip"], "2024-06-01 12:00:00 | 2024-06-01 12:00:00-04",
                    "an export and import round trip keeps timestamp and timestamptz in a non-UTC session")


def test_ptz_the_gap_wall_times_become_their_instants(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["gap instants"], "07:00,07:00,07:30",
                    "the gap file's wall times 02:00, 02:30 and 03:00 become, sorted, the instants "
                    "07:00Z, 07:00Z and 07:30Z")


def test_ptz_a_gap_spanning_group_is_not_skipped(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["gap count"], "1",
                    "a row group spanning a spring-forward gap is not skipped on its converted min and max")


def test_ptz_the_legacy_fixture_carries_only_the_converted_type(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["legacy premise"], "TIMESTAMP_MICROS True",
                    "premise: the legacy fixture carries the TIMESTAMP_MICROS converted type and no logical type")


def test_ptz_a_naive_file_in_tokyo_is_its_wall_time_there(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["tokyo"], "2024-06-01 12:00:00+09 true",
                    "a naive file read into timestamptz in Tokyo is its wall time there, as the cast gives")


def test_ptz_the_tokyo_reading_differs_from_new_york(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["tokyo differs"], "true",
                    "and the Tokyo reading is a different instant from the New York one")


def test_ptz_a_fall_back_wall_time_is_what_the_cast_gives(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["fall-back"], "2024-11-03 01:30:00-05 true",
                    "a naive wall time in the fall-back hour read into timestamptz is what the cast gives")


def test_ptz_a_legacy_file_into_timestamp_is_the_session_wall_time(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["legacy ts"], "2024-06-01 08:00:00 true",
                    "a legacy TIMESTAMP_MICROS file read into timestamp is the session-zone wall time of its instant")


def test_ptz_a_legacy_file_into_timestamptz_keeps_its_instant(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["legacy tz"], "2024-06-01 08:00:00-04",
                    "a legacy TIMESTAMP_MICROS file read into timestamptz keeps its instant")
