"""The Parquet reader refuses values that do not fit the column's type, and a row group
whose column-chunk list is shorter than the schema. The pytest twin of
`test/native_parquet_hardening.sh`, every check by the shell's name.

int2 and date ride on INT32, time and timestamp on INT64, because Parquet has no
narrower physical type. An out-of-range value must raise, not wrap. Four of the five
range refusals share 22008, so each check wants its guard's own message ("value out of
range for type <T>") as well as the SQLSTATE. Each "still reads" check compares the
decoded values with the ones the writer stored, not a row count, and the no-wrap check
reads the out-of-range file itself, which must refuse. Every reading waits for the
server first, and survival also reads the server log's count of killed backends, since
SELECT 1 answers again after crash recovery.

ON A PRIVATE DATABASE. The shell skips whole without pyarrow, so every test declines
under the shell's skip name before it asks for the fixture. The files are written by
`test/craft_parquet_hardening.py`, the shell's own crafter, run as a subprocess so this
module imports no pyarrow, into `server_dir`.
"""
import pathlib
import subprocess
import sys
import time

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_native_parquet_hardening"
CRAFTER = pathlib.Path(__file__).resolve().parent.parent / "craft_parquet_hardening.py"
SKIP = "pyarrow not available; Parquet hardening suite needs it"

TABLES = [
    ("f_i2_bad", "int2", "i2_bad"), ("f_i2_ok", "int2", "i2_ok"),
    ("f_time_bad", "time", "time_bad"), ("f_time_ok", "time", "time_ok"),
    ("f_date_bad", "date", "date_bad"), ("f_date_ovf", "date", "date_ovf"),
    ("f_date_ok", "date", "date_ok"),
    ("f_ts_bad", "timestamp", "ts_bad"), ("f_ts_ok", "timestamp", "ts_ok"),
]


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _wait_up(dsn):
    for _ in range(60):
        try:
            with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as conn:
                conn.execute("SELECT 1")
            return
        except psycopg.Error:
            time.sleep(0.5)


def _value(dsn, sql):
    """-> the first column of the first row as text, as the shell's q prints it ("" for
    no row or NULL), or the failure as a query_error()."""
    _wait_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return "" if row is None or row[0] is None else str(row[0])


def _exec(dsn, sql):
    """Run one setup statement; -> None, or the failure as a query_error()."""
    _wait_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return None


def _sqlstate(dsn, sql):
    """-> pgc_try's reading of SQL: "SQLSTATE: message", or NO ERROR."""
    return _value(dsn, f"SELECT pgc_try($q${sql}$q$)")


def _short_verdict(dsn, sql):
    """-> CLEAN REJECT when SQL raised the chunk-count guard's refusal, NO ERROR when it
    raised nothing, or any other failure as a query_error() that names it."""
    _wait_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        if not exc.sqlstate:
            return query_error("no refusal: the connection was lost")
        msg = exc.diag.message_primary or ""
        detail = exc.diag.message_detail or ""
        # five guards share the message; only this guard's detail is the chunk-count one
        if ("has a malformed row group" in msg
                and "carries 1 column chunks, but the schema has 2 leaf columns" in detail):
            return "CLEAN REJECT"
        return query_error(f"{exc.sqlstate}: {msg}")
    return "NO ERROR"


def _crashes(cluster):
    try:
        return (cluster.datadir / "server.log").read_text(errors="replace").count("terminated by signal")
    except OSError as exc:
        return query_error(f"crash count unreadable: {exc}")


def _survived(dsn, cluster, before):
    """-> "1" when the server answers and no backend died since `before`."""
    _wait_up(dsn)
    up, now = _value(dsn, "SELECT 1"), _crashes(cluster)
    if not isinstance(before, int) or not isinstance(now, int):
        return query_error(f"crash count unreadable: {before} / {now}")
    if up == "1" and now == before:
        return "1"
    return f"no ([{up if up == '1' else ''}], {now - before} backend crashes)"


FOOTER_FILES = ("footer_good", "footer_reencoded", "nibble_row_groups", "nibble_num_rows", "no_row_groups",
                "no_rg_num_rows", "no_file_num_rows", "file_num_rows_2999", "unknown_map_field",
                "unknown_bool_list_field")
UNPARSED = '0A000: could not parse "<file>" (unsupported or corrupt Parquet metadata)'
GOOD_READ = "NO ERROR: 3000/4498500"


def _int(v):
    """-> v as an int when it is one, else v unchanged, so num() names a failed reading."""
    return int(v) if isinstance(v, str) and v.lstrip("-").isdigit() else v


@pytest.fixture(scope="module")
def pqh(pgc_cluster, server_dir):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        w = server_dir / "pqh"
        w.mkdir(exist_ok=True)
        w.chmod(0o777)
        subprocess.run([sys.executable, str(CRAFTER), str(w)], capture_output=True, text=True)
        for f in w.glob("*.parquet"):
            f.chmod(0o644)
        r = {}
        before = _crashes(pgc_cluster)
        setup = ["CREATE EXTENSION IF NOT EXISTS pgcolumnar",
                 "CREATE SERVER pq FOREIGN DATA WRAPPER pgcolumnar_parquet"]
        setup += [f"CREATE FOREIGN TABLE {t} (c {typ}) SERVER pq OPTIONS (path '{w}/{f}.parquet')"
                  for t, typ, f in TABLES]
        setup += [
            "CREATE FUNCTION pgc_try(q text) RETURNS text LANGUAGE plpgsql AS $$ "
            "BEGIN EXECUTE q; RETURN 'NO ERROR'; "
            "EXCEPTION WHEN OTHERS THEN RETURN SQLSTATE || ': ' || SQLERRM; END $$",
            "CREATE FUNCTION pgc_value(q text) RETURNS text LANGUAGE plpgsql AS $$ "
            "DECLARE r text; BEGIN EXECUTE q INTO r; RETURN coalesce(r, '<null>'); "
            "EXCEPTION WHEN OTHERS THEN RETURN SQLSTATE; END $$",
            "CREATE TABLE imp_bad (c int2)",
            f"CREATE FOREIGN TABLE f_short (a int4, b int4) SERVER pq OPTIONS (path '{w}/short_chunks.parquet')",
            # #1822: a refusal as "SQLSTATE: message / detail" with the path masked
            "CREATE FUNCTION pgc_err(q text) RETURNS text LANGUAGE plpgsql AS $$ "
            "DECLARE r text; d text; "
            "BEGIN EXECUTE q INTO r; RETURN 'NO ERROR: ' || coalesce(r, '<null>'); "
            "EXCEPTION WHEN OTHERS THEN GET STACKED DIAGNOSTICS d = PG_EXCEPTION_DETAIL; "
            "RETURN SQLSTATE || ': ' || regexp_replace(SQLERRM, '\"[^\"]*\"', '\"<file>\"') "
            "|| coalesce(' / ' || nullif(d, ''), ''); END $$",
        ]
        for stmt in setup:
            # a failed setup step surfaces in the readings that depend on it, by name
            _exec(dsn, stmt)

        r["i2 bad"] = _sqlstate(dsn, "SELECT * FROM f_i2_bad")
        r["time bad"] = _sqlstate(dsn, "SELECT * FROM f_time_bad")
        r["date bad"] = _sqlstate(dsn, "SELECT * FROM f_date_bad")
        r["date ovf"] = _sqlstate(dsn, "SELECT * FROM f_date_ovf")
        r["ts bad"] = _sqlstate(dsn, "SELECT * FROM f_ts_bad")
        r["i2 ok"] = _value(dsn, "SELECT string_agg(c::text, ' ' ORDER BY c) FROM f_i2_ok")
        r["time ok"] = _value(dsn, "SELECT string_agg(c::text, ' ' ORDER BY c) FROM f_time_ok")
        r["date ok"] = _value(dsn, "SELECT string_agg(c::text, ' ' ORDER BY c) FROM f_date_ok")
        r["ts ok"] = _value(dsn, "SELECT string_agg(c::text, ',' ORDER BY c) FROM f_ts_ok")
        r["no wrap"] = _value(dsn, "SELECT pgc_value($q$SELECT count(*) FROM f_i2_bad WHERE c = (-25536)::int2$q$)")
        r["rp bad"] = _sqlstate(dsn, f"SELECT * FROM pgcolumnar.read_parquet('{w}/i2_bad.parquet') AS t(c int2)")
        r["rp ok"] = _value(dsn, f"SELECT string_agg(c::text, ' ' ORDER BY c) FROM "
                                 f"pgcolumnar.read_parquet('{w}/i2_ok.parquet') AS t(c int2)")
        r["imp bad"] = _sqlstate(dsn, f"SELECT pgcolumnar.import_parquet('imp_bad'::regclass, '{w}/i2_bad.parquet')")
        r["imp rows"] = _value(dsn, "SELECT count(*) FROM imp_bad")
        r["short"] = _short_verdict(dsn, "SELECT * FROM f_short")
        r["survived"] = _survived(dsn, pgc_cluster, before)
        r["schema"] = _value(dsn, f"SELECT count(*) FROM pgcolumnar.parquet_schema('{w}/short_chunks.parquet')")
        r["two"] = _value(dsn, f"SELECT string_agg(a || '|' || b, ' ' ORDER BY a) FROM "
                               f"pgcolumnar.read_parquet('{w}/two.parquet') AS t(a int4, b int4)")
        # ---- malformed footer Thrift (#1822) ----
        footer_before = _crashes(pgc_cluster)
        for name in FOOTER_FILES:
            r[name] = _value(dsn, f"SELECT pgc_err($q$SELECT count(*) || '/' || coalesce(sum(id), 0) FROM "
                                  f"pgcolumnar.read_parquet('{w}/{name}.parquet') AS t(id bigint, name text)$q$)")
        try:
            nsweep = (w / "nibble_sweep.count").read_text().strip()
        except OSError:
            nsweep = ""
        r["nsweep"] = nsweep
        r["sweep"] = _value(dsn, (
            f"SELECT count(*) || ' of {nsweep} read without an error' "
            "|| coalesce(': ' || string_agg(i || '=' || v, ' ' ORDER BY i), '') "
            "FROM (SELECT i, pgc_err(format($f$SELECT count(*) || '/' || coalesce(sum(id), 0) FROM "
            "pgcolumnar.read_parquet(%L) AS t(id bigint, name text)$f$, "
            f"'{w}/nibble_sweep_' || lpad(i::text, 4, '0') || '.parquet')) AS v "
            f"FROM generate_series(0, {nsweep or 0} - 1) AS i) s WHERE v LIKE 'NO ERROR%'"))
        r["footer survived"] = _survived(dsn, pgc_cluster, footer_before)
        yield r
    finally:
        _wait_up(pgc_cluster.dsn())
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_parquet_hardening_int2_out_of_range_raises_22003(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["i2 bad"], "22003: value out of range for type smallint", "int2 out of range -> 22003")


def test_parquet_hardening_time_out_of_day_raises_22008(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["time bad"], "22008: value out of range for type time", "time out of day -> 22008")


def test_parquet_hardening_invalid_date_raises_22008(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["date bad"], "22008: value out of range for type date", "date invalid -> 22008")


def test_parquet_hardening_date_offset_overflow_raises_22008(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["date ovf"], "22008: value out of range for type date", "date offset overflow -> 22008")


def test_parquet_hardening_timestamp_overflow_raises_22008(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["ts bad"], "22008: value out of range for type timestamp", "timestamp overflow -> 22008")


def test_parquet_hardening_int2_in_range_still_reads(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["i2 ok"], "-30000 0 30000", "int2 in range still reads")


def test_parquet_hardening_time_in_range_still_reads(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["time ok"], "00:00:00 01:00:00", "time in range still reads")


def test_parquet_hardening_date_in_range_still_reads(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["date ok"], "1970-01-01 2022-01-08", "date in range still reads")


def test_parquet_hardening_timestamp_in_range_still_reads(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["ts ok"], "1970-01-01 00:00:00,2023-11-14 22:13:20", "timestamp in range still reads")


def test_parquet_hardening_no_wrapped_value_appears(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["no wrap"], "22003", "no wrapped -25536 appears")


def test_parquet_hardening_read_parquet_also_raises_22003(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["rp bad"], "22003: value out of range for type smallint", "read_parquet also raises 22003")


def test_parquet_hardening_read_parquet_in_range_still_reads(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["rp ok"], "-30000 0 30000", "read_parquet in range still reads")


def test_parquet_hardening_import_parquet_also_raises_22003(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["imp bad"], "22003: value out of range for type smallint", "import_parquet also raises 22003")


def test_parquet_hardening_import_parquet_left_no_wrapped_rows(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.num(_int(r["imp rows"]), 0, "import_parquet left no wrapped rows")


def test_parquet_hardening_short_chunk_list_is_a_clean_reject(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["short"], "CLEAN REJECT", "short chunk list rejected with a specific error")


def test_parquet_hardening_backend_survives_the_short_chunk_list(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["survived"], "1", "backend survived short chunk list")


def test_parquet_hardening_parquet_schema_still_describes_the_bad_file(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["schema"], "2", "parquet_schema still describes a bad chunk list")


def test_parquet_hardening_a_valid_two_column_file_still_reads(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["two"], "1|4 2|5 3|6", "valid two-column file still reads")


# ---- malformed footer Thrift (#1822) -----------------------------------------
# The crafter decoded a good 3000-row file's footer and wrote variants of it. A field
# header with an invalid type nibble was skipped as zero bytes, so the parser lost its
# place and could lose row_groups or a num_rows. Each reading is "count/sum(id)" or the
# refusal as "SQLSTATE: message / detail", so a silent wrong answer shows its numbers.

def _footer(request, expect):
    """-> the fixture's readings, or None when pyarrow is missing (declined by name)."""
    if expect.require_module("pyarrow.parquet", name=SKIP) is None:
        return None
    return request.getfixturevalue("pqh")


def test_parquet_hardening_footer_good_file_still_reads(request, expect):
    r = _footer(request, expect)
    if r is None:
        return
    expect.text(r["footer_good"], GOOD_READ, "footer: the good file still reads")


def test_parquet_hardening_footer_reencoded_good_file_still_reads(request, expect):
    r = _footer(request, expect)
    if r is None:
        return
    expect.text(r["footer_reencoded"], GOOD_READ, "footer: the re-encoded good file still reads")


def test_parquet_hardening_footer_invalid_type_nibble_on_row_groups_is_refused(request, expect):
    r = _footer(request, expect)
    if r is None:
        return
    expect.text(r["nibble_row_groups"], UNPARSED,
                "footer: an invalid type nibble on row_groups is refused")


def test_parquet_hardening_footer_invalid_type_nibble_on_num_rows_is_refused(request, expect):
    r = _footer(request, expect)
    if r is None:
        return
    expect.text(r["nibble_num_rows"], UNPARSED,
                "footer: an invalid type nibble on a num_rows is refused")


def test_parquet_hardening_footer_every_invalid_type_nibble_is_refused(request, expect):
    if expect.require_module("pyarrow.parquet", name=SKIP) is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["sweep"], f"0 of {r['nsweep']} read without an error",
                "footer: every invalid type nibble on a field header is refused")


def test_parquet_hardening_footer_without_row_groups_is_refused(request, expect):
    r = _footer(request, expect)
    if r is None:
        return
    expect.text(r["no_row_groups"],
                'XX001: Parquet file "<file>" has a malformed footer / The footer has no row_groups field.',
                "footer: no row_groups field is refused")


def test_parquet_hardening_footer_row_group_without_num_rows_is_refused(request, expect):
    r = _footer(request, expect)
    if r is None:
        return
    expect.text(r["no_rg_num_rows"],
                'XX001: Parquet file "<file>" has a malformed row group / Row group 2 has no num_rows field.',
                "footer: a row group with no num_rows is refused")


def test_parquet_hardening_footer_without_file_num_rows_is_refused(request, expect):
    r = _footer(request, expect)
    if r is None:
        return
    expect.text(r["no_file_num_rows"],
                'XX001: Parquet file "<file>" has a malformed footer / The footer has no num_rows field.',
                "footer: no file num_rows is refused")


def test_parquet_hardening_footer_num_rows_differing_from_row_groups_is_refused(request, expect):
    r = _footer(request, expect)
    if r is None:
        return
    expect.text(r["file_num_rows_2999"],
                'XX001: Parquet file "<file>" has a malformed footer / '
                'The footer declares num_rows 2999, but its row groups hold 3000 rows.',
                "footer: a file num_rows that differs from its row groups is refused")


def test_parquet_hardening_footer_unknown_map_field_is_skipped_whole(request, expect):
    r = _footer(request, expect)
    if r is None:
        return
    expect.text(r["unknown_map_field"], GOOD_READ, "footer: an unknown map field is skipped whole")


def test_parquet_hardening_footer_unknown_bool_list_is_skipped_whole(request, expect):
    r = _footer(request, expect)
    if r is None:
        return
    expect.text(r["unknown_bool_list_field"], GOOD_READ,
                "footer: an unknown list of bools is skipped whole")


def test_parquet_hardening_backend_survived_the_malformed_footers(request, expect):
    r = _footer(request, expect)
    if r is None:
        return
    expect.text(r["footer survived"], "1", "backend survived the malformed footers")
