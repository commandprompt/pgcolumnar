"""The Parquet reader refuses values that do not fit the column's type, and a row group
whose column-chunk list is shorter than the schema. The pytest twin of
`test/native_parquet_hardening.sh`, every check by the shell's name.

int2 and date ride on INT32, time and timestamp on INT64, because Parquet has no
narrower physical type. An out-of-range value must raise, not wrap. An unsigned column
(UINT_32, UINT_64) reads as the stored value into bigint or numeric, which parquet_schema
now advises, and refuses a narrower type with 22003 rather than coming back negative
(#1814). Four of the five range refusals share 22008, so each check wants its guard's
own message ("value out of range for type <T>") as well as the SQLSTATE. Each "still
reads" check compares the decoded values with the ones the writer stored, not a row
count, and the no-wrap check reads the out-of-range file itself, which must refuse.
Every reading waits for the server first, and survival also reads the server log's
count of killed backends, since SELECT 1 answers again after crash recovery.

STRING CELLS (#1821). A Parquet STRING is UTF-8 by the spec, and a cell holding
invalid UTF-8 or an embedded NUL must be refused with 22021 and a message that names
the column, wherever the decoder is reached: read_parquet (PLAIN and dictionary), the
Parquet foreign table, import_parquet and iceberg_scan. Those readings run in a second
private database created as UTF8, since the check is against the server encoding and a
SQL_ASCII cluster accepts any byte but NUL. The Iceberg FDW must read, not prune, a
file whose manifest partition value is not valid text.

A FOOTER THAT DOES NOT MATCH ITS PAGES (#1855, #1852). A column the footer marks
REQUIRED while its pages carry definition levels, a repetition_type that is no valid
value, a footer num_rows the row groups do not add up to, and a list group marked
OPTIONAL whose pages carry repetition levels are each refused with XX001 and a DETAIL
that names the measurement.

INT96 VALUES THAT ARE NOT TIMESTAMPS (#1870). Nanoseconds outside the day, a Julian day
before timestamp's range, and a page shorter than its num_values are each refused.

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
DB8 = "pgc_native_parquet_hardening_utf8"
CRAFTER = pathlib.Path(__file__).resolve().parent.parent / "craft_parquet_hardening.py"
WAREHOUSE = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "iceberg" / "warehouse"
ENC_MSG = '22021: invalid byte sequence for encoding "UTF8" in Parquet column "name"'
SKIP = "pyarrow not available; Parquet hardening suite needs it"

TABLES = [
    ("f_i2_bad", "int2", "i2_bad"), ("f_i2_ok", "int2", "i2_ok"),
    ("f_time_bad", "time", "time_bad"), ("f_time_ok", "time", "time_ok"),
    ("f_date_bad", "date", "date_bad"), ("f_date_ovf", "date", "date_ovf"),
    ("f_date_ok", "date", "date_ok"),
    ("f_ts_bad", "timestamp", "ts_bad"), ("f_ts_ok", "timestamp", "ts_ok"),
    ("f_u32_i8", "int8", "u32"), ("f_u32_i4", "int4", "u32"),
    ("f_u64_num", "numeric", "u64"), ("f_u64_i8", "int8", "u64"),
    ("f_u32max_i2", "int2", "u32_max"),
]

# the premise reader: pyarrow's own reading of the unsigned file, in a subprocess so
# this module imports no pyarrow
UNSIGNED_READER = ("import sys, pyarrow.parquet as pq; t = pq.read_table(sys.argv[1]); "
                   "print(' '.join(str(v) for c in ('u32', 'u64') for v in t.column(c).to_pylist()))")


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


REP_HIST = ('Row group 0 column 0 ("id") has a definition level histogram of 2 entries, '
            'but the schema gives it a maximum definition level of 0.')
REP_ROWS = "The row groups hold 3000 rows, but the file declares num_rows 2000."
REP_ENUM = ('Schema element "id" has repetition_type 7; a valid one is 0 (REQUIRED), '
            '1 (OPTIONAL) or 2 (REPEATED).')


LST_HIST = ('Row group 0 column 0 ("element") has a repetition level histogram of 2 entries, '
            'but the schema gives it a maximum repetition level of 0.')
I96_DETAIL = ("The Parquet file stores an INT96 timestamp of Julian day {} and {} nanoseconds, "
              "which cannot be represented as timestamp with time zone.")


def _rep_verdict(dsn, sql, want, sqlstate):
    """-> CLEAN REJECT when SQL raised `sqlstate` with the DETAIL `want`, else the
    SQLSTATE, message and DETAIL it raised, or NO ERROR and the rows it returned (#1855)."""
    _wait_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            rows = conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        detail = exc.diag.message_detail or ""
        if exc.sqlstate == sqlstate and detail.startswith(want):
            return "CLEAN REJECT"
        return query_error(f"{exc.sqlstate}: {exc.diag.message_primary} / {detail}")
    return "NO ERROR: " + " ".join(str(r[0]) for r in rows)


def _error_line(dsn, sql):
    """-> "SQLSTATE: message" when SQL raised, else the first column of each row it
    returned, as the shell's psql reading of the same statement prints them."""
    _wait_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            rows = conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        if not exc.sqlstate:
            return query_error("no refusal: the connection was lost")
        return f"{exc.sqlstate}: {exc.diag.message_primary}"
    return " ".join(str(r[0]) for r in rows)


def _detail_verdict(dsn, sql, want):
    """-> "names <value>" when SQL raised with the DETAIL `want` names, NO ERROR when it
    raised nothing, or any other failure as a query_error() that names it."""
    _wait_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        detail = exc.diag.message_detail or ""
        if detail == f"The Parquet file stores {want}, which cannot be represented as bigint.":
            return f"names {want}"
        return query_error(f"{exc.sqlstate}: {exc.diag.message_primary} / {detail}")
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


def _int(v):
    """-> v as an int when it is one, else v unchanged, so num() names a failed reading."""
    return int(v) if isinstance(v, str) and v.lstrip("-").isdigit() else v


@pytest.fixture(scope="module")
def pqh(pgc_cluster, server_dir):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
        a.execute(f"DROP DATABASE IF EXISTS {DB8} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB8} TEMPLATE template0 ENCODING 'UTF8' LOCALE 'C'")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        w = server_dir / "pqh"
        w.mkdir(exist_ok=True)
        w.chmod(0o777)
        subprocess.run([sys.executable, str(CRAFTER), str(w), str(WAREHOUSE)],
                       capture_output=True, text=True)
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
            "CREATE TABLE imp_u (u8 int, u16 int, u32 bigint, u64 numeric)",
            "CREATE TABLE imp_u32_i4 (c int4)",
            f"CREATE FOREIGN TABLE f_short (a int4, b int4) SERVER pq OPTIONS (path '{w}/short_chunks.parquet')",
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
        pre = subprocess.run([sys.executable, "-c", UNSIGNED_READER, f"{w}/unsigned.parquet"],
                             capture_output=True, text=True)
        r["u premise"] = (pre.stdout.strip() if pre.returncode == 0
                          else query_error((pre.stderr.strip().splitlines() or ["no output"])[-1]))
        r["u schema"] = _value(dsn, "SELECT string_agg(column_name || ' ' || data_type, '|' ORDER BY column_name) "
                                    f"FROM pgcolumnar.parquet_schema('{w}/unsigned.parquet')")
        r["u32 i8"] = _value(dsn, "SELECT string_agg(c::text, ' ' ORDER BY c) FROM f_u32_i8")
        r["u64 num"] = _value(dsn, "SELECT string_agg(c::text, ' ' ORDER BY c) FROM f_u64_num")
        r["u32 where"] = _value(dsn, "SELECT count(*) FROM f_u32_i8 WHERE c > 0")
        r["u32 i4"] = _sqlstate(dsn, "SELECT * FROM f_u32_i4")
        r["u64 i8"] = _sqlstate(dsn, "SELECT * FROM f_u64_i8")
        r["u32max i2"] = _sqlstate(dsn, "SELECT * FROM f_u32max_i2")
        r["u64 detail"] = _detail_verdict(dsn, "SELECT * FROM f_u64_i8", "9223372036854775808")
        r["u rp"] = _value(dsn, "SELECT string_agg(u8 || '|' || u16 || '|' || u32 || '|' || u64, ' ' ORDER BY u64) "
                                f"FROM pgcolumnar.read_parquet('{w}/unsigned.parquet') "
                                "AS t(u8 int, u16 int, u32 bigint, u64 numeric)")
        r["u rp i8"] = _sqlstate(dsn, f"SELECT * FROM pgcolumnar.read_parquet('{w}/u64.parquet') AS t(c bigint)")
        for mark in ("logical", "converted"):
            r[f"u32 {mark} only"] = _value(dsn, "SELECT string_agg(c::text, ' ' ORDER BY c) FROM pgcolumnar.read_parquet("
                                                f"'{w}/u32_{mark}_only.parquet') AS t(c bigint)")
        imp = _value(dsn, f"SELECT pgc_value($q$SELECT pgcolumnar.import_parquet('imp_u'::regclass, "
                          f"'{w}/unsigned.parquet')$q$)")
        rows = _value(dsn, "SELECT string_agg(u32 || '|' || u64, ' ' ORDER BY u64) FROM imp_u")
        r["u imp"] = (f"{imp}:{rows}" if isinstance(imp, str) and isinstance(rows, str)
                      else imp if not isinstance(imp, str) else rows)
        r["u imp i4"] = _sqlstate(dsn, f"SELECT pgcolumnar.import_parquet('imp_u32_i4'::regclass, '{w}/u32.parquet')")
        r["short"] = _short_verdict(dsn, "SELECT * FROM f_short")
        r["survived"] = _survived(dsn, pgc_cluster, before)
        r["schema"] = _value(dsn, f"SELECT count(*) FROM pgcolumnar.parquet_schema('{w}/short_chunks.parquet')")
        r["two"] = _value(dsn, f"SELECT string_agg(a || '|' || b, ' ' ORDER BY a) FROM "
                               f"pgcolumnar.read_parquet('{w}/two.parquet') AS t(a int4, b int4)")

        # ---- a repetition_type that does not match the pages (#1855) ----
        rep_sum = "SELECT count(*) || '/' || sum(id) FROM pgcolumnar.read_parquet"
        r["rep base"] = _value(dsn, f"{rep_sum}('{w}/rep_base.parquet') AS t(id bigint, name text)")
        r["rep required"] = _rep_verdict(dsn, f"{rep_sum}('{w}/rep_required.parquet') AS t(id bigint, name text)",
                                         REP_HIST, "XX001")
        r["rep invalid"] = _rep_verdict(dsn, f"{rep_sum}('{w}/rep_invalid.parquet') AS t(id bigint, name text)",
                                        REP_ENUM, "XX001")
        _exec(dsn, "CREATE TABLE imp_rep (id bigint, name text)")
        r["rep imp"] = _rep_verdict(dsn, f"SELECT pgcolumnar.import_parquet('imp_rep'::regclass, "
                                         f"'{w}/rep_required.parquet')", REP_HIST, "XX001")
        r["rows short"] = _rep_verdict(dsn, f"{rep_sum}('{w}/rows_short.parquet') AS t(id bigint, name text)",
                                       REP_ROWS, "XX001")

        # ---- a repetition_type that does not match the repetition levels (#1855) ----
        lst_agg = ("SELECT string_agg(coalesce(l::text, 'null'), ' ' ORDER BY l NULLS LAST) "
                   "FROM pgcolumnar.read_parquet")
        r["lst base"] = _value(dsn, f"{lst_agg}('{w}/lst_base.parquet') AS t(l bigint[])")
        r["lst optional"] = _rep_verdict(dsn, f"{lst_agg}('{w}/lst_optional.parquet') AS t(l bigint[])",
                                         LST_HIST, "XX001")
        r["lst optional scalar"] = _rep_verdict(
            dsn, f"SELECT count(*) || '/' || count(l) FROM pgcolumnar.read_parquet('{w}/lst_optional.parquet') "
                 "AS t(l bigint)", LST_HIST, "XX001")

        # ---- INT96 values that are not timestamps (#1870) ----
        i96_cnt = "SELECT count(*) || '/' || count(t) FROM pgcolumnar.read_parquet"
        r["i96 req"] = _value(dsn, f"{i96_cnt}('{w}/i96_req.parquet') AS t(t timestamptz)")
        for tag, jday, nanos in (("nanos_day", 2461246, 86400000000000), ("nanos_neg", 2461246, -1),
                                 ("jday_neg", -1, 123456789)):
            r[f"i96 {tag}"] = _rep_verdict(dsn, f"{i96_cnt}('{w}/i96_{tag}.parquet') AS t(t timestamptz)",
                                           I96_DETAIL.format(jday, nanos), "22008")
        r["i96 short"] = _error_line(dsn, f"{i96_cnt}('{w}/i96_short.parquet') AS t(t timestamptz)")

        # ---- string cells, in the UTF8 database (#1821) ----
        d8 = pgc_cluster.dsn(dbname=DB8)
        ice_bad = sorted((w / "ice_bad" / "db" / "events" / "metadata").glob("*.metadata.json"))
        ice_part = sorted((w / "ice_part" / "db" / "events" / "metadata").glob("*.metadata.json"))
        ice_bad_md = ice_bad[-1] if ice_bad else w / "ice_bad-missing"
        ice_part_md = ice_part[-1] if ice_part else w / "ice_part-missing"
        for stmt in [
            "CREATE EXTENSION pgcolumnar",
            "CREATE SERVER pq FOREIGN DATA WRAPPER pgcolumnar_parquet",
            "CREATE FUNCTION pgc_try(q text) RETURNS text LANGUAGE plpgsql AS $$ "
            "BEGIN EXECUTE q; RETURN 'NO ERROR'; "
            "EXCEPTION WHEN OTHERS THEN RETURN SQLSTATE || ': ' || SQLERRM; END $$",
            f"CREATE FOREIGN TABLE f_str_dict (id bigint, name text) SERVER pq "
            f"OPTIONS (path '{w}/str_dict_bad.parquet')",
            "CREATE TABLE imp_str (id bigint, name text)",
            "CREATE TABLE heap_str (id bigint, name text) USING heap",
            "CREATE SERVER ice FOREIGN DATA WRAPPER pgcolumnar_iceberg",
            f"CREATE FOREIGN TABLE ev_part (id bigint, region text, amount int) "
            f"SERVER ice OPTIONS (metadata_path '{ice_part_md}')",
        ]:
            _exec(d8, stmt)
        r["s enc"] = _value(d8, "SELECT pg_encoding_to_char(encoding) FROM pg_database "
                                "WHERE datname = current_database()")

        def rp(f, typ="text"):
            return f"SELECT * FROM pgcolumnar.read_parquet('{w}/{f}.parquet') AS t(id bigint, name {typ})"

        r["s plain"] = _sqlstate(d8, rp("str_plain_bad"))
        r["s dict"] = _sqlstate(d8, rp("str_dict_bad"))
        r["s nul"] = _sqlstate(d8, rp("str_nul"))
        r["s varchar"] = _sqlstate(d8, rp("str_plain_bad", "varchar"))
        r["s fdw"] = _sqlstate(d8, "SELECT * FROM f_str_dict")
        r["s imp"] = _sqlstate(d8, f"SELECT pgcolumnar.import_parquet('imp_str'::regclass, "
                                   f"'{w}/str_plain_bad.parquet')")
        r["s imp rows"] = _value(d8, "SELECT count(*) FROM imp_str")
        _sqlstate(d8, "INSERT INTO heap_str " + rp("str_nul"))
        r["s heap rows"] = _value(d8, "SELECT count(*) FROM heap_str")
        r["s ok"] = _value(d8, f"SELECT string_agg(name || ':' || octet_length(name), ' ' ORDER BY id) "
                               f"FROM pgcolumnar.read_parquet('{w}/str_ok.parquet') AS t(id bigint, name text)")
        r["s ok dict"] = _value(d8, f"SELECT string_agg(name, ' ' ORDER BY id) "
                                    f"FROM pgcolumnar.read_parquet('{w}/str_ok_dict.parquet') AS t(id bigint, name text)")
        r["s ice"] = _sqlstate(d8, f"SELECT * FROM pgcolumnar.iceberg_scan('{ice_bad_md}') "
                                   f"AS t(id bigint, region text)")
        r["s part all"] = _value(d8, "SELECT string_agg(id || region, ',' ORDER BY id) FROM ev_part")
        r["s part eu"] = _value(d8, "SELECT string_agg(id::text, ',' ORDER BY id) FROM ev_part "
                                    "WHERE region = 'eu'")
        r["s survived"] = _survived(d8, pgc_cluster, before)
        yield r
    finally:
        _wait_up(pgc_cluster.dsn())
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
            a.execute(f"DROP DATABASE IF EXISTS {DB8} WITH (FORCE)")


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


def test_parquet_hardening_unsigned_premise_pyarrow_reads_unsigned(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["u premise"], "1 2147483648 4294967295 1 9223372036854775808 18446744073709551615", "premise: pyarrow reads the unsigned file's values unsigned")


def test_parquet_hardening_parquet_schema_advises_types_that_hold_unsigned_values(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["u schema"], "u16 integer|u32 bigint|u64 numeric(20,0)|u8 integer", "parquet_schema advises a type that holds every unsigned value")


def test_parquet_hardening_uint32_reads_into_bigint_as_unsigned(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["u32 i8"], "1 2147483648 4294967295", "UINT_32 reads into bigint as unsigned")


def test_parquet_hardening_uint64_reads_into_numeric_as_unsigned(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["u64 num"], "1 9223372036854775808 18446744073709551615", "UINT_64 reads into numeric as unsigned")


def test_parquet_hardening_uint32_into_bigint_where_counts_every_row(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["u32 where"], "3", "UINT_32 into bigint: WHERE c > 0 counts every row")


def test_parquet_hardening_uint32_above_2_31_into_integer_raises_22003(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["u32 i4"], "22003: value out of range for type integer", "UINT_32 at or above 2^31 into integer -> 22003")


def test_parquet_hardening_uint64_above_2_63_into_bigint_raises_22003(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["u64 i8"], "22003: value out of range for type bigint", "UINT_64 at or above 2^63 into bigint -> 22003")


def test_parquet_hardening_uint32_max_into_smallint_raises_22003(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["u32max i2"], "22003: value out of range for type smallint", "UINT_32 2^32-1 into smallint -> 22003, not -1")


def test_parquet_hardening_uint64_refusal_names_the_stored_value(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["u64 detail"], "names 9223372036854775808", "the UINT_64 refusal names the stored value")


def test_parquet_hardening_read_parquet_reads_every_unsigned_width(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["u rp"], "1|1|1|1 200|40000|2147483648|9223372036854775808 255|65535|4294967295|18446744073709551615", "read_parquet reads every unsigned width at its advised type")


def test_parquet_hardening_read_parquet_uint64_into_bigint_raises_22003(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["u rp i8"], "22003: value out of range for type bigint", "read_parquet UINT_64 into bigint -> 22003")


def test_parquet_hardening_uint32_logical_type_alone_marks_it_unsigned(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["u32 logical only"], "1 2147483648 4294967295", "UINT_32 marked only by its LogicalType reads into bigint as unsigned")


def test_parquet_hardening_uint32_converted_type_alone_marks_it_unsigned(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["u32 converted only"], "1 2147483648 4294967295", "UINT_32 marked only by its ConvertedType reads into bigint as unsigned")


def test_parquet_hardening_import_parquet_reads_unsigned_into_wider_types(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["u imp"], "3:1|1 2147483648|9223372036854775808 4294967295|18446744073709551615", "import_parquet reads UINT_32 into bigint and UINT_64 into numeric")


def test_parquet_hardening_import_parquet_uint32_into_integer_raises_22003(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["u imp i4"], "22003: value out of range for type integer", "import_parquet UINT_32 into integer -> 22003")


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


def test_parquet_hardening_unchanged_repetition_file_reads(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["rep base"], "3000/4498500", "the unchanged repetition file reads 3000/4498500")


def test_parquet_hardening_repetition_required_over_optional_pages_is_refused(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["rep required"], "CLEAN REJECT", "repetition_type REQUIRED over optional pages is refused")


def test_parquet_hardening_invalid_repetition_type_is_refused(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["rep invalid"], "CLEAN REJECT", "repetition_type that is not 0, 1 or 2 is refused")


def test_parquet_hardening_import_parquet_refuses_repetition_required_over_optional_pages(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["rep imp"], "CLEAN REJECT", "import_parquet refuses repetition_type REQUIRED over optional pages")


def test_parquet_hardening_footer_num_rows_mismatch_is_refused(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["rows short"], "CLEAN REJECT", "a footer num_rows the row groups do not add up to is refused")


def test_parquet_hardening_unchanged_list_file_reads(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["lst base"], "{} {1,2} {3} {4,5,6} null", "the unchanged list file reads {} {1,2} {3} {4,5,6} null")


def test_parquet_hardening_list_group_marked_optional_over_repeated_pages_is_refused(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["lst optional"], "CLEAN REJECT", "a list group marked OPTIONAL over repeated pages is refused")


def test_parquet_hardening_list_group_marked_optional_is_refused_into_a_scalar_column(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["lst optional scalar"], "CLEAN REJECT",
                "a list group marked OPTIONAL over repeated pages is refused into a scalar column")


def test_parquet_hardening_unpatched_required_int96_file_reads(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["i96 req"], "3/3", "the unpatched REQUIRED INT96 file reads its 3 values")


def test_parquet_hardening_int96_nanoseconds_of_a_whole_day_are_refused(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["i96 nanos_day"], "CLEAN REJECT", "INT96 nanoseconds of a whole day are refused")


def test_parquet_hardening_int96_negative_nanoseconds_are_refused(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["i96 nanos_neg"], "CLEAN REJECT", "INT96 nanoseconds of -1 are refused")


def test_parquet_hardening_int96_negative_julian_day_is_refused(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["i96 jday_neg"], "CLEAN REJECT", "INT96 Julian day -1 is refused")


def test_parquet_hardening_int96_short_page_is_refused(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["i96 short"], "XX001: could not decode Parquet column 0 in row group 0",
                "an INT96 page shorter than its num_values is refused")


def test_parquet_hardening_strings_run_in_a_utf8_database(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["s enc"], "UTF8", "premise: the string arms run in a UTF8 database")


def test_parquet_hardening_read_parquet_plain_string_not_utf8_raises_22021(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["s plain"], ENC_MSG + ": 0xff", "read_parquet PLAIN string not UTF-8 -> 22021 naming the column")


def test_parquet_hardening_read_parquet_dictionary_string_not_utf8_raises_22021(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["s dict"], ENC_MSG + ": 0xc0 0xaf", "read_parquet dictionary string not UTF-8 -> 22021 naming the column")


def test_parquet_hardening_read_parquet_embedded_nul_raises_22021(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["s nul"], ENC_MSG + ": 0x00", "read_parquet string with an embedded NUL -> 22021")


def test_parquet_hardening_varchar_output_column_is_checked_too(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["s varchar"], ENC_MSG + ": 0xff", "varchar output column is checked too -> 22021")


def test_parquet_hardening_parquet_foreign_table_string_not_utf8_raises_22021(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["s fdw"], ENC_MSG + ": 0xc0 0xaf", "parquet foreign table string not UTF-8 -> 22021")


def test_parquet_hardening_import_parquet_string_not_utf8_raises_22021(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["s imp"], ENC_MSG + ": 0xff", "import_parquet string not UTF-8 -> 22021")


def test_parquet_hardening_import_parquet_stored_no_row_of_the_invalid_file(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.num(_int(r["s imp rows"]), 0, "import_parquet stored no row of the invalid file")


def test_parquet_hardening_insert_select_read_parquet_stored_no_row_of_the_invalid_file(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.num(_int(r["s heap rows"]), 0, "INSERT SELECT read_parquet stored no row of the invalid file")


def test_parquet_hardening_valid_multibyte_utf8_still_reads_plain(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["s ok"], "ok:2 caf\u00e9:5 \u65e5\u672c:6", "valid multibyte UTF-8 still reads (PLAIN)")


def test_parquet_hardening_valid_multibyte_utf8_still_reads_dictionary(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["s ok dict"], "caf\u00e9 caf\u00e9 caf\u00e9 ok", "valid multibyte UTF-8 still reads (dictionary)")


def test_parquet_hardening_iceberg_scan_data_file_string_not_utf8_raises_22021(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["s ice"], '22021: invalid byte sequence for encoding "UTF8" in Parquet column "region": 0xff', "iceberg_scan data file string not UTF-8 -> 22021 naming the column")


def test_parquet_hardening_patched_manifest_table_reads_all_five_rows(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["s part all"], "1eu,2eu,3us,4us,5us", "the patched-manifest table reads all five rows")


def test_parquet_hardening_iceberg_fdw_reads_a_file_whose_partition_value_is_not_valid_text(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["s part eu"], "1,2", "iceberg FDW reads, not prunes, a file whose partition value is not valid text")


def test_parquet_hardening_backend_survives_the_string_arms(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; Parquet hardening suite needs it") is None:
        return
    r = request.getfixturevalue("pqh")
    expect.text(r["s survived"], "1", "backend survived the string arms")
