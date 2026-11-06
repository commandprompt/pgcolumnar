"""The Iceberg FDW pushes projection down, and doing so never changes a result. The
pytest twin of `test/iceberg_fdw_projection.sh`, every check by the shell's name
(#1341).

Over a 13-column fixture, each projection shape the FDW decodes -- one column, three,
the whole row, none (count(*)), a filter on an unselected column, one selected and
another filtered -- must answer exactly what bare iceberg_scan, which reads every
column, answers.

ON A PRIVATE DATABASE, with the fixture warehouse copied into `server_dir`, because
the backend reads it. Both sides of each comparison are read and compared only when
both were taken. Where the fixture is missing the first test fails under the shell's
pgc_skip message, as pgc_skip does, and the rest decline unnamed.
"""
import pathlib
import shutil

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_iceberg_fdw_projection"
WH = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "iceberg" / "warehouse_wide"
COLS = "k bigint" + "".join(f", c{i} text" for i in range(12))
SHAPES = {
    "one column": ("string_agg(c0, ',' ORDER BY k)", ""),
    "three columns": ("string_agg(c0||c5||c11, ',' ORDER BY k)", ""),
    "whole row": ("string_agg(k||c0||c6||c11, ',' ORDER BY k)", ""),
    "count only": ("count(*)", ""),
    "filter unselected": ("string_agg(c1, ',' ORDER BY k)", "WHERE c7 = 'c7r10'"),
    "select one filter another": ("string_agg(c2, ',' ORDER BY k)", "WHERE c9 = 'c9r5' OR c9 = 'c9r6'"),
}


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _value(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return query_error(f"no value from {sql}") if row is None or row[0] is None else str(row[0])


@pytest.fixture(scope="module")
def fp(pgc_cluster, server_dir):
    if not list(WH.glob("db/wide/data/*.parquet")):
        yield None
        return
    dest = server_dir / "whw"
    shutil.copytree(WH / "db", dest / "db")
    for p in [dest, *dest.rglob("*")]:
        p.chmod(0o777 if p.is_dir() else 0o666)
    md = sorted((dest / "db" / "wide" / "metadata").glob("*.metadata.json"))[-1]
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            c.execute("CREATE SERVER ice FOREIGN DATA WRAPPER pgcolumnar_iceberg")
            c.execute(f"CREATE FOREIGN TABLE w ({COLS}) SERVER ice OPTIONS (metadata_path '{md}')")
        r = {}
        for key, (sel, where) in SHAPES.items():
            r[key] = (_value(dsn, f"SELECT {sel} FROM w {where}"),
                      _value(dsn, f"SELECT {sel} FROM pgcolumnar.iceberg_scan('{md}') AS t({COLS}) {where}"))
        r["alive"] = _value(dsn, "SELECT 1")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _declined(fp, expect):
    if fp is not None:
        return False
    expect.cannot_run("ABSENT_FIXTURE", "the wide iceberg fixture is missing")
    return True


def test_one_column(fp, expect):
    expect.require_module("json", name="python3 is needed")
    if fp is None:
        expect.text("missing", "present", "wide iceberg fixture is missing")
        return
    f, s = fp["one column"]
    expect.text(f, s, "one column : FDW projection matches iceberg_scan")


def test_three_columns(fp, expect):
    if _declined(fp, expect):
        return
    f, s = fp["three columns"]
    expect.text(f, s, "three columns : FDW projection matches iceberg_scan")


def test_whole_row(fp, expect):
    if _declined(fp, expect):
        return
    f, s = fp["whole row"]
    expect.text(f, s, "whole row : FDW projection matches iceberg_scan")


def test_count_only(fp, expect):
    if _declined(fp, expect):
        return
    f, s = fp["count only"]
    expect.text(f, s, "count only (no column referenced) : FDW projection matches iceberg_scan")


def test_filter_references_an_unselected_column(fp, expect):
    if _declined(fp, expect):
        return
    f, s = fp["filter unselected"]
    expect.text(f, s, "filter references an unselected column : FDW projection matches iceberg_scan")


def test_select_one_filter_another(fp, expect):
    if _declined(fp, expect):
        return
    f, s = fp["select one filter another"]
    expect.text(f, s, "select one, filter another : FDW projection matches iceberg_scan")


def test_backend_alive_after_the_projections(fp, expect):
    if _declined(fp, expect):
        return
    expect.text(str(fp["alive"]), "1", "backend alive")
