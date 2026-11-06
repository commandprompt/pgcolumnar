"""The Iceberg FDW estimates rows from the manifests, not a constant. The pytest twin of
`test/iceberg_fdw_estimate.sh`, every check by the shell's name (#1341).

The old code set rows = 1000 for every table. The estimate must be positive, must
equal the live record count summed from the manifests, and must still have the
scan's selectivity applied, so a filtered scan estimates fewer rows than the table.

ON A PRIVATE DATABASE, with the fixture warehouse copied into `server_dir`, because
the backend reads it. Where the fixture is missing the first test fails under the
shell's pgc_skip message, as pgc_skip does, and the rest decline unnamed.
"""
import pathlib
import re
import shutil

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_iceberg_fdw_estimate"
WH = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "iceberg" / "warehouse"


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _value(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return query_error(f"no value from {sql}") if row is None or row[0] is None else row[0]


def _estimate(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            plan = "\n".join(r[0] for r in conn.execute(f"EXPLAIN (FORMAT text) {sql}"))
    except psycopg.Error as exc:
        return query_error(_message(exc))
    m = re.search(r"rows=(\d+)", plan, re.I)
    return int(m.group(1)) if m else query_error("no rows= in the plan")


@pytest.fixture(scope="module")
def fe(pgc_cluster, server_dir):
    if not list(WH.glob("db/events/data/*/*.parquet")):
        yield None
        return
    dest = server_dir / "wh"
    shutil.copytree(WH / "db", dest / "db")
    for p in [dest, *dest.rglob("*")]:
        p.chmod(0o777 if p.is_dir() else 0o666)
    md = sorted((dest / "db" / "events" / "metadata").glob("*.metadata.json"))[-1]
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            c.execute("CREATE SERVER ice FOREIGN DATA WRAPPER pgcolumnar_iceberg")
            c.execute("CREATE FOREIGN TABLE ev (id bigint, region text, amount int) "
                      f"SERVER ice OPTIONS (metadata_path '{md}')")
        yield {"actual": _value(dsn, "SELECT count(*) FROM ev"), "est": _estimate(dsn, "SELECT * FROM ev"),
               "filt": _estimate(dsn, "SELECT * FROM ev WHERE region = 'eu'"), "alive": _value(dsn, "SELECT 1")}
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _declined(fe, expect):
    if fe is not None:
        return False
    expect.cannot_run("ABSENT_FIXTURE", "the iceberg warehouse fixture is missing")
    return True


def test_the_fdw_estimates_a_positive_row_count(fe, expect):
    expect.require_module("json", name="python3 is needed")
    if fe is None:
        expect.text("missing", "present", "iceberg warehouse data files are missing")
        return
    e = fe["est"]
    expect.text("yes" if isinstance(e, int) and e > 0 else f"no ({e})", "yes",
                "the FDW estimates a positive row count from the manifests")


def test_the_estimate_is_the_real_record_count(fe, expect):
    if _declined(fe, expect):
        return
    expect.num(fe["est"], fe["actual"], "the estimate is the real record count, not the old constant 1000")


def test_a_filtered_scan_estimates_fewer_rows(fe, expect):
    if _declined(fe, expect):
        return
    f, a = fe["filt"], fe["actual"]
    ok = isinstance(f, int) and isinstance(a, int) and f < a
    expect.text("yes" if ok else f"no ({f} of {a})", "yes",
                "a filtered scan estimates fewer rows than the whole table (selectivity applied)")


def test_backend_alive_after_the_estimates(fe, expect):
    if _declined(fe, expect):
        return
    expect.text(str(fe["alive"]), "1", "backend alive")
