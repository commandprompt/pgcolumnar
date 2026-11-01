"""pgcolumnar.read_parquet reads a server-side Parquet file back as the rows that were
generated, refuses what it must with its own guard's message, and keeps its own
pg_read_server_files gate. The pytest twin of `test/native_read_parquet.sh`, every check
by the shell's name (#1341).

THE ORACLE IS THE GENERATOR. The source table is read through the columnar scan both
to write the file and, before #1341, as "the source", so a scan that decoded one value
wrong agreed with itself. Every round trip here hashes the generator's own rows.

A REFUSAL IS ITS OWN GUARD'S. Two refusals share SQLSTATE 42804, so each requires its
message as well; each waits for the server first, and a survival check reads the server
log's count of killed backends. A failed reading is a query_error(), never a default.
ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading.
"""
import time

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_native_read_parquet"
SCALAR_COLS = ("c_i2 int2, c_i4 int4, c_i8 int8, c_f4 float4, c_f8 float8, c_bool bool, "
               "c_text text, c_bytea bytea, c_date date, c_time time, c_ts timestamp")
GEN_SCALAR = ("SELECT (g%100)::int2, g, g::int8*1000, (g/2.0)::float4, (g/4.0)::float8, (g%2=0), "
              "'row'||g, decode(lpad(to_hex(g),8,'0'),'hex'), date '2020-01-01' + g, "
              "time '00:00:00' + (g||' seconds')::interval, "
              "timestamp '2020-01-01' + (g||' minutes')::interval "
              "FROM generate_series(1, 5000) g "
              "UNION ALL SELECT NULL::int2, NULL::int4, NULL::int8, NULL::float4, NULL::float8, "
              "NULL::bool, NULL::text, NULL::bytea, NULL::date, NULL::time, NULL::timestamp")
NESTED_COLS = "id int, ia int[], ta text[], c rp_npc"
GEN_NESTED = ("SELECT g, "
              "CASE WHEN g%11=0 THEN NULL WHEN g%7=0 THEN '{}'::int[] ELSE ARRAY[g, g+1, g+2] END, "
              "CASE WHEN g%13=0 THEN NULL ELSE ARRAY['a'||g, 'b'||g] END, "
              "CASE WHEN g%5=0 THEN NULL ELSE ROW(g, 'c'||g)::rp_npc END "
              "FROM generate_series(1, 3000) g")
GATE = "must be superuser or a member of the pg_read_server_files role to read a server file"


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _up(dsn):
    """Wait (bounded) until the server answers, so a crash fails only its own reading."""
    for _ in range(40):
        try:
            with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as conn:
                if conn.execute("SELECT 1").fetchone()[0] == 1:
                    return
        except psycopg.Error:
            pass
        time.sleep(0.5)


def _run(dsn, sql):
    _up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.encode())  # simple protocol: several statements, as psql -c
    except psycopg.Error as exc:
        return _message(exc)
    return ""


def _value(dsn, sql, settings=()):
    """-> the first column of the first row as text, or the failure as a query_error()."""
    _up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for s in settings:
                conn.execute(s)
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    if row is None or row[0] is None:
        return query_error(f"no value from {sql}")
    return str(row[0])


def _set_hash(dsn, sql):
    return _value(dsn, "SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), 'EMPTY') "
                       f"FROM (SELECT _row::text AS t FROM ({sql}) _row) _s")


def _refusal(dsn, sql, phrase, label, settings=()):
    """-> "<SQLSTATE><label>" when `sql` raised with `phrase` in its message, the bare
    SQLSTATE when the message lacks it, or what happened instead."""
    _up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for s in settings:
                conn.execute(s)
            conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        if not exc.sqlstate:
            return query_error(_message(exc))
        return exc.sqlstate + (label if phrase in str(exc) else "")
    return "no error"


def _error(dsn, sql):
    """-> the primary message of the error `sql` raised, or "no error (<first row>)"."""
    _up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        if not exc.sqlstate:
            return query_error(_message(exc))
        return exc.diag.message_primary or ""
    return f"no error ({row})"


def _crashes(cluster):
    try:
        text = (cluster.datadir / "server.log").read_text(errors="replace")
    except OSError as exc:
        return query_error(f"crash count unreadable: {exc}")
    return text.count("terminated by signal")


@pytest.fixture(scope="module")
def rp(pgc_cluster, server_dir):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        before = _crashes(pgc_cluster)
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        scalar = server_dir / "rp_scalar.parquet"
        nested = server_dir / "rp_nested.parquet"
        bad = server_dir / "rp_bad.dat"
        r = {}

        _run(dsn, f"CREATE TABLE s ({SCALAR_COLS}) USING pgcolumnar")
        _run(dsn, f"INSERT INTO s {GEN_SCALAR}")
        r["scalar export"] = _value(dsn, f"SELECT pgcolumnar.export_parquet('s', '{scalar}')")
        rp_scalar = f"SELECT * FROM pgcolumnar.read_parquet('{scalar}') AS t({SCALAR_COLS})"
        r["scalar rp"] = _set_hash(dsn, rp_scalar)
        r["scalar gen"] = _set_hash(dsn, GEN_SCALAR)
        r["scalar count"] = _value(dsn, f"SELECT count(*) FROM pgcolumnar.read_parquet('{scalar}') "
                                        f"AS t({SCALAR_COLS})")
        r["proj rp"] = _set_hash(dsn, f"SELECT c_text FROM pgcolumnar.read_parquet('{scalar}') "
                                      f"AS t({SCALAR_COLS})")
        r["proj gen"] = _set_hash(dsn, "SELECT 'row'||g FROM generate_series(1, 5000) g "
                                       "UNION ALL SELECT NULL::text")

        _run(dsn, "CREATE TYPE rp_npc AS (x int, y text)")
        _run(dsn, "CREATE TABLE np (id int, ia int[], ta text[], c rp_npc) USING pgcolumnar")
        _run(dsn, f"INSERT INTO np {GEN_NESTED}")
        r["nested export"] = _value(dsn, f"SELECT pgcolumnar.export_parquet('np', '{nested}')")
        r["nested rp"] = _set_hash(dsn, f"SELECT * FROM pgcolumnar.read_parquet('{nested}') "
                                        f"AS t({NESTED_COLS})")
        r["nested gen"] = _set_hash(dsn, GEN_NESTED)

        r["no list"] = _refusal(dsn, f"SELECT * FROM pgcolumnar.read_parquet('{scalar}')",
                                "a column definition list is required", " (no column list)")
        r["fewer"] = _refusal(dsn, f"SELECT * FROM pgcolumnar.read_parquet('{scalar}') AS t(c_i2 int2)",
                              "Parquet file has 11 leaf columns, target table expands to 1",
                              " by the leaf-count guard")
        r["type"] = _refusal(dsn, f"SELECT * FROM pgcolumnar.read_parquet('{scalar}') "
                                  "AS t(c_i2 int2, c_i4 text, c_i8 int8, c_f4 float4, c_f8 float8, "
                                  "c_bool bool, c_text text, c_bytea bytea, c_date date, c_time time, "
                                  "c_ts timestamp)",
                             'physical type is not compatible with target column "c_i4"',
                             " by the physical-type guard")
        # ---- a type refusal names the function that was called (#1871) ----
        money_cols = ("c_i2 money, c_i4 int4, c_i8 int8, c_f4 float4, c_f8 float8, c_bool bool, "
                      "c_text text, c_bytea bytea, c_date date, c_time time, c_ts timestamp")
        r["money rp"] = _error(dsn, f"SELECT * FROM pgcolumnar.read_parquet('{scalar}') AS t({money_cols})")
        _run(dsn, f"CREATE TABLE imp_money ({money_cols}) USING pgcolumnar")
        r["money imp"] = _error(dsn, f"SELECT pgcolumnar.import_parquet('imp_money', '{scalar}')")
        _run(dsn, "CREATE SERVER rp_pq FOREIGN DATA WRAPPER pgcolumnar_parquet")
        _run(dsn, f"CREATE FOREIGN TABLE f_money ({money_cols}) SERVER rp_pq OPTIONS (path '{scalar}')")
        r["money fdw"] = _error(dsn, "SELECT * FROM f_money")
        bad.write_text("not parquet")
        bad.chmod(0o644)
        r["bad"] = _refusal(dsn, f"SELECT * FROM pgcolumnar.read_parquet('{bad}') AS t(a int)",
                            "is not a Parquet file", " by the magic check")

        _run(dsn, "DROP ROLE IF EXISTS t_rp_none")
        _run(dsn, "DROP ROLE IF EXISTS t_rp_files")
        _run(dsn, "CREATE ROLE t_rp_none NOSUPERUSER LOGIN")
        _run(dsn, "CREATE ROLE t_rp_files NOSUPERUSER LOGIN")
        _run(dsn, "GRANT USAGE ON SCHEMA pgcolumnar TO t_rp_none, t_rp_files")
        _run(dsn, "GRANT pg_read_server_files TO t_rp_files")
        r["gate"] = _refusal(dsn, f"SELECT count(*) FROM pgcolumnar.read_parquet('{scalar}') "
                                  f"AS t({SCALAR_COLS})", GATE, " by the file gate",
                             settings=("SET ROLE t_rp_none",))
        r["member"] = _value(dsn, f"SELECT count(*) FROM pgcolumnar.read_parquet('{scalar}') "
                                  f"AS t({SCALAR_COLS})", settings=("SET ROLE t_rp_files",))

        _up(dsn)
        up, now = _value(dsn, "SELECT 'alive'"), _crashes(pgc_cluster)
        if not isinstance(before, int) or not isinstance(now, int):
            r["alive"] = query_error(f"crash count unreadable: {before} / {now}")
        elif up == "alive" and now == before:
            r["alive"] = "alive"
        else:
            r["alive"] = f"no ([{up}], {now - before} backend crashes)"

        _run(dsn, f"CREATE TABLE imp ({SCALAR_COLS}) USING pgcolumnar")
        r["import"] = _value(dsn, f"SELECT pgcolumnar.import_parquet('imp', '{scalar}')")
        r["import rp"] = _set_hash(dsn, rp_scalar)
        r["import imp"] = _set_hash(dsn, "SELECT * FROM imp")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
            a.execute("DROP ROLE IF EXISTS t_rp_none")
            a.execute("DROP ROLE IF EXISTS t_rp_files")


def test_read_parquet_fixture_exports_every_scalar_row(rp, expect):
    expect.text(rp["scalar export"], "5001", "scalar export rows")


def test_read_parquet_returns_the_generated_scalar_rows(rp, expect):
    expect.text(rp["scalar rp"], rp["scalar gen"], "scalar round-trip: read_parquet == the generated rows")


def test_read_parquet_counts_every_scalar_row(rp, expect):
    expect.text(rp["scalar count"], "5001", "scalar read_parquet row count")


def test_a_projected_column_round_trips(rp, expect):
    expect.text(rp["proj rp"], rp["proj gen"], "projected column round-trips")


def test_read_parquet_fixture_exports_every_nested_row(rp, expect):
    expect.text(rp["nested export"], "3000", "nested export rows")


def test_read_parquet_returns_the_generated_nested_rows(rp, expect):
    expect.text(rp["nested rp"], rp["nested gen"], "nested round-trip: read_parquet == the generated rows")


def test_a_missing_column_definition_list_is_refused(rp, expect):
    expect.text(rp["no list"], "42601 (no column list)", "premise: reject missing column definition list")


def test_fewer_declared_columns_than_the_file_is_refused(rp, expect):
    expect.text(rp["fewer"], "42804 by the leaf-count guard", "reject fewer columns than file")


def test_an_incompatible_declared_type_is_refused(rp, expect):
    expect.text(rp["type"], "42804 by the physical-type guard", "reject incompatible declared type")


def test_read_parquet_type_refusal_names_read_parquet(rp, expect):
    expect.text(rp["money rp"], 'column "c_i2" has type money, which pgcolumnar.read_parquet does not support',
                "read_parquet's type refusal names read_parquet")


def test_import_parquet_type_refusal_names_import_parquet(rp, expect):
    expect.text(rp["money imp"], 'column "c_i2" has type money, which pgcolumnar.import_parquet does not support',
                "import_parquet's type refusal names import_parquet")


def test_parquet_foreign_table_type_refusal_names_the_wrapper(rp, expect):
    expect.text(rp["money fdw"], 'column "c_i2" has type money, which the pgcolumnar_parquet foreign-data wrapper '
                                 'does not support',
                "a Parquet foreign table's type refusal names the foreign-data wrapper")


def test_a_file_that_is_not_parquet_is_refused(rp, expect):
    expect.text(rp["bad"], "XX001 by the magic check", "reject non-parquet file")


def test_read_parquet_keeps_its_own_file_gate(rp, expect):
    expect.text(rp["gate"], "42501 by the file gate",
                "a role without pg_read_server_files is refused by read_parquet's own gate")


def test_a_pg_read_server_files_member_reads_every_row(rp, expect):
    expect.text(rp["member"], "5001", "and a member of pg_read_server_files reads every row")


def test_the_backend_is_still_up_after_the_read_parquet_refusals(rp, expect):
    expect.text(rp["alive"], "alive", "backend still up after the refusals")


def test_import_parquet_loads_every_row(rp, expect):
    expect.text(rp["import"], "5001", "import parity rows")


def test_read_parquet_agrees_with_import_parquet(rp, expect):
    expect.text(rp["import rp"], rp["import imp"], "read_parquet == import_parquet")
