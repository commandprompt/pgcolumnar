"""The pgcolumnar_parquet foreign-data wrapper reads an exported Parquet file as a
foreign table (Phase G). The pytest twin of `test/native_parquet_fdw.sh`, every check
by the shell's name.

The oracle is a heap table written from the generator, not the columnar table the
file was exported from. Projection and row-group skipping are read from the scan's
own EXPLAIN ANALYZE counters: every arm used to compare answers only, which the
executor's recheck and a discarding projection keep right, so a scan that decoded
every column or skipped no row group passed. Each refusal wants its own guard's
SQLSTATE and message, not any error: with the scan's missing-path guard removed the
backend crashed, and with the short-file guard removed a later guard refused, and
both passed. Every reading waits for the server first, and survival reads the server
log's count of killed backends, since SELECT 1 answers again after crash recovery.

ON A PRIVATE DATABASE, with its own server; the files live in `server_dir`.
"""
import re
import time

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_native_parquet_fdw"

SCALAR_COLS = ("c_i2 int2, c_i4 int4, c_i8 int8, c_f4 float4, c_f8 float8, c_bool bool, "
               "c_text text, c_bytea bytea, c_date date, c_time time, c_ts timestamp")
GEN = ("SELECT (g%100)::int2, g, g::int8*1000, (g/2.0)::float4, (g/4.0)::float8, (g%2=0), "
       "'row'||g, decode(lpad(to_hex(g),8,'0'),'hex'), date '2020-01-01' + g, "
       "time '00:00:00' + (g||' seconds')::interval, "
       "timestamp '2020-01-01' + (g||' minutes')::interval "
       "FROM generate_series(1, 5000) g")
NULLROW = "VALUES (NULL::int2,NULL::int4,NULL::int8,NULL::float4,NULL::float8,NULL::bool,NULL::text,NULL::bytea,NULL::date,NULL::time,NULL::timestamp)"
EXPLAIN = "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) "


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


def _run(dsn, sql):
    """Run a setup statement after waiting for the server; a failure is left to the
    readings that depend on it, which then fail by name."""
    _wait_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)
    except psycopg.Error:
        pass


def _rows(dsn, sql, settings=()):
    """-> the rows joined as the shell's psql -qtA prints them, or the failure as a
    query_error(). `settings` run first on the same connection."""
    _wait_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for st in settings:
                conn.execute(st)
            rows = conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return "\n".join("|".join("" if v is None else str(v) for v in row) for row in rows)


def _set_hash(dsn, query):
    """The port of pgc_set_hash: a failed query is a query_error(), unique per call."""
    return _rows(dsn, "SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), 'EMPTY') "
                      f"FROM (SELECT _row::text AS t FROM ({query}) _row) _s")


def _field(plan, name):
    """-> the integer after "<name>: " in an EXPLAIN text, or "no reading"."""
    if not isinstance(plan, str) or plan.startswith("QUERY_ERROR"):
        return "no reading"
    m = re.search(rf"{re.escape(name)}: (\d+)", plan)
    return m.group(1) if m else "no reading"


def _refusal(dsn, sql, regex, label):
    """-> "<SQLSTATE> (<label>)" when the statement raised the wanted refusal,
    "<SQLSTATE>: <message>" for any other error, or what happened instead."""
    _wait_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)
    except psycopg.Error as exc:
        if not exc.sqlstate:
            return "no refusal: the connection was lost"
        msg = exc.diag.message_primary or ""
        return f"{exc.sqlstate} ({label})" if re.search(regex, msg) else f"{exc.sqlstate}: {msg}"
    return "no error"


def _crashes(cluster):
    try:
        return (cluster.datadir / "server.log").read_text(errors="replace").count("terminated by signal")
    except OSError as exc:
        return query_error(f"crash count unreadable: {exc}")


def _survived(dsn, cluster, before):
    """-> "1" when the server answers and no backend died since `before`."""
    _wait_up(dsn)
    up, now = _rows(dsn, "SELECT 1"), _crashes(cluster)
    if not isinstance(before, int) or not isinstance(now, int):
        return query_error(f"crash count unreadable: {before} / {now}")
    if up == "1" and now == before:
        return "1"
    return f"no ([{up if up == '1' else ''}], {now - before} backend crashes)"


def _int(v):
    return int(v) if isinstance(v, str) and v.isdigit() else v


@pytest.fixture(scope="module")
def pfdw(pgc_cluster, server_dir):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        scalar = server_dir / "fdw_scalar.parquet"
        nested = server_dir / "fdw_nested.parquet"
        skf = server_dir / "fdw_skip.parquet"
        bad = server_dir / "fdw_bad.dat"
        r = {}
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        _run(dsn, "CREATE SERVER pqsrv FOREIGN DATA WRAPPER pgcolumnar_parquet")
        _run(dsn, f"CREATE TABLE s ({SCALAR_COLS}) USING pgcolumnar")
        _run(dsn, f"INSERT INTO s {GEN}")
        _run(dsn, f"INSERT INTO s {NULLROW}")
        _run(dsn, f"CREATE TABLE h ({SCALAR_COLS}) USING heap")
        _run(dsn, f"INSERT INTO h {GEN}")
        _run(dsn, f"INSERT INTO h {NULLROW}")
        r["scalar export"] = _rows(dsn, f"SELECT pgcolumnar.export_parquet('s', '{scalar}')")
        _run(dsn, f"CREATE FOREIGN TABLE fs ({SCALAR_COLS}) SERVER pqsrv OPTIONS (path '{scalar}')")
        r["full fdw"] = _set_hash(dsn, "SELECT * FROM fs")
        r["full src"] = _set_hash(dsn, "SELECT * FROM h")
        r["count"] = _rows(dsn, "SELECT count(*) FROM fs")
        r["proj fdw"] = _set_hash(dsn, "SELECT c_text FROM fs")
        r["proj src"] = _set_hash(dsn, "SELECT c_text FROM h")
        r["pred fdw"] = _set_hash(dsn, "SELECT c_i4, c_text FROM fs WHERE c_i4 > 2500 AND c_bool")
        r["pred src"] = _set_hash(dsn, "SELECT c_i4, c_text FROM h  WHERE c_i4 > 2500 AND c_bool")
        nl = ("SET enable_hashjoin=off", "SET enable_mergejoin=off")
        r["join fdw"] = _rows(dsn, "SELECT count(*) FROM fs a JOIN fs b ON a.c_i4 = b.c_i4 WHERE a.c_i4 < 200", nl)
        r["join src"] = _rows(dsn, "SELECT count(*) FROM h  a JOIN h  b ON a.c_i4 = b.c_i4 WHERE a.c_i4 < 200", nl)
        plan = _rows(dsn, "EXPLAIN (COSTS OFF) SELECT * FROM fs")
        r["foreign scan"] = (str(sum(1 for line in plan.splitlines() if "Foreign Scan" in line))
                             if not plan.startswith("QUERY_ERROR") else plan)
        r["proj plan"] = _rows(dsn, EXPLAIN + "SELECT c_text FROM fs")
        _run(dsn, "CREATE TABLE sk (id int8) USING pgcolumnar")
        _run(dsn, "INSERT INTO sk SELECT g FROM generate_series(1, 200000) g")
        r["skip export"] = _rows(dsn, f"SELECT pgcolumnar.export_parquet('sk', '{skf}')")
        _run(dsn, f"CREATE FOREIGN TABLE fsk (id int8) SERVER pqsrv OPTIONS (path '{skf}')")
        r["skip plan"] = _rows(dsn, EXPLAIN + "SELECT count(*) FROM fsk WHERE id > 196608::int8")
        # the plan's own query: a plain 196608 is cross-type and skips nothing (#1711 review)
        r["skip count"] = _rows(dsn, "SELECT count(*) FROM fsk WHERE id > 196608::int8")
        _run(dsn, "CREATE TYPE fdw_npc AS (x int, y text)")
        _run(dsn, "CREATE TABLE np (id int, ia int[], ta text[], c fdw_npc) USING pgcolumnar")
        _run(dsn, "INSERT INTO np SELECT g, "
                  "CASE WHEN g%11=0 THEN NULL WHEN g%7=0 THEN '{}'::int[] ELSE ARRAY[g,g+1] END, "
                  "CASE WHEN g%13=0 THEN NULL ELSE ARRAY['a'||g,'b'||g] END, "
                  "CASE WHEN g%5=0 THEN NULL ELSE ROW(g,'c'||g)::fdw_npc END "
                  "FROM generate_series(1, 3000) g")
        r["nested export"] = _rows(dsn, f"SELECT pgcolumnar.export_parquet('np', '{nested}')")
        _run(dsn, f"CREATE FOREIGN TABLE fnp (id int, ia int[], ta text[], c fdw_npc) SERVER pqsrv OPTIONS (path '{nested}')")
        r["nested fdw"] = _set_hash(dsn, "SELECT * FROM fnp")
        r["nested src"] = _set_hash(dsn, "SELECT * FROM np")

        before = _crashes(pgc_cluster)
        r["bad option"] = _refusal(dsn, f"CREATE FOREIGN TABLE fbad ({SCALAR_COLS}) SERVER pqsrv "
                                        f"OPTIONS (path '{scalar}', bogus 'x')",
                                   r'invalid option "bogus"', "invalid option")
        r["empty path"] = _refusal(dsn, f"CREATE FOREIGN TABLE fempty ({SCALAR_COLS}) SERVER pqsrv OPTIONS (path '')",
                                   r'"path" option cannot be empty', "empty path")
        _run(dsn, f"CREATE FOREIGN TABLE fnopath ({SCALAR_COLS}) SERVER pqsrv")
        r["no path"] = _refusal(dsn, "SELECT * FROM fnopath",
                                r'foreign table "fnopath" has no "path" option', "no path")
        bad.write_text("not parquet")
        bad.chmod(0o644)
        _run(dsn, f"CREATE FOREIGN TABLE fbadfile ({SCALAR_COLS}) SERVER pqsrv OPTIONS (path '{bad}')")
        r["not parquet"] = _refusal(dsn, "SELECT * FROM fbadfile",
                                    r'bad\.dat" is not a Parquet file$', "too short")
        _run(dsn, "CREATE FOREIGN TABLE fwrongtype (c_i2 int2, c_i4 text, c_i8 int8, c_f4 float4, "
                  "c_f8 float8, c_bool bool, c_text text, c_bytea bytea, c_date date, c_time time, "
                  f"c_ts timestamp) SERVER pqsrv OPTIONS (path '{scalar}')")
        r["wrong type"] = _refusal(dsn, "SELECT * FROM fwrongtype",
                                   r'physical type is not compatible with target column "c_i4"', "type mismatch")
        r["survived"] = _survived(dsn, pgc_cluster, before)
        yield r
    finally:
        _wait_up(pgc_cluster.dsn())
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_the_scalar_table_exports_every_row(pfdw, expect):
    expect.text(pfdw["scalar export"], "5001", "scalar export rows")


def test_a_full_foreign_scan_equals_the_source(pfdw, expect):
    expect.text(pfdw["full fdw"], pfdw["full src"], "full scan: fdw == source")


def test_the_foreign_table_counts_every_row(pfdw, expect):
    expect.num(_int(pfdw["count"]), 5001, "fdw row count")


def test_a_projection_equals_the_source(pfdw, expect):
    expect.text(pfdw["proj fdw"], pfdw["proj src"], "projection: fdw == source")


def test_a_predicate_equals_the_source(pfdw, expect):
    expect.text(pfdw["pred fdw"], pfdw["pred src"], "predicate: fdw == source")


def test_a_nested_loop_self_join_rescans_correctly(pfdw, expect):
    expect.num(_int(pfdw["join fdw"]), _int(pfdw["join src"]), "nested-loop self-join (rescan): fdw == source")


def test_explain_shows_a_foreign_scan(pfdw, expect):
    expect.text(pfdw["foreign scan"], "1", "EXPLAIN shows Foreign Scan")


def test_a_one_column_query_reads_one_column(pfdw, expect):
    p = pfdw["proj plan"]
    expect.text(f"{_field(p, 'Columns Read')}/{_field(p, 'Columns Total')}", "1/11",
                "a one-column query reads one of the eleven columns")


def test_the_skip_table_exports_every_row(pfdw, expect):
    expect.num(_int(pfdw["skip export"]), 200000, "skip export rows")


def test_the_skip_file_has_four_row_groups(pfdw, expect):
    expect.text(_field(pfdw["skip plan"], "Row Groups"), "4", "the skip file has four row groups")


def test_a_selective_predicate_skips_three_row_groups(pfdw, expect):
    expect.text(_field(pfdw["skip plan"], "Row Groups Skipped"), "3",
                "a selective predicate skips the three row groups it cannot match")


def test_the_skipped_scan_returns_every_matching_row(pfdw, expect):
    expect.num(_int(pfdw["skip count"]), 3392, "and the skipped scan still returns every matching row")


def test_the_fdw_nested_table_exports_every_row(pfdw, expect):
    expect.text(pfdw["nested export"], "3000", "nested export rows")


def test_a_nested_full_scan_equals_the_source(pfdw, expect):
    expect.text(pfdw["nested fdw"], pfdw["nested src"], "nested full scan: fdw == source")


def test_an_invalid_table_option_is_rejected(pfdw, expect):
    expect.text(pfdw["bad option"], "HV00D (invalid option)", "reject invalid table option (HV00D)")


def test_an_empty_path_option_is_rejected(pfdw, expect):
    expect.text(pfdw["empty path"], "HV00D (empty path)", "reject empty path option (HV00D)")


def test_a_scan_without_a_path_option_errors(pfdw, expect):
    expect.text(pfdw["no path"], "HV00J (no path)", "scan without path option errors (HV00J)")


def test_a_scan_of_a_non_parquet_file_errors(pfdw, expect):
    expect.text(pfdw["not parquet"], "XX001 (too short)", "scan of non-parquet file errors (XX001)")


def test_a_scan_with_an_incompatible_declared_type_errors(pfdw, expect):
    expect.text(pfdw["wrong type"], "42804 (type mismatch)", "scan with incompatible declared type errors (42804)")


def test_the_backend_is_still_up_after_the_fdw_refusals(pfdw, expect):
    expect.text(pfdw["survived"], "1", "backend still up after the refusals")
