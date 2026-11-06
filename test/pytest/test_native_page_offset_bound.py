"""A corrupt column_chunk.page_offset is refused by the containment guard on both
reads, and the backend survives (#1341). The pytest twin of
`test/native_page_offset_bound.sh`, every check by the shell's name.

The whole-group read (pgcolumnar.enable_column_projection = off) reads a group as one
span and derives each chunk's base from page_offset; the projected read computes its
ranges from it. Each guard refuses a chunk outside its group with XX001 and "... lies
outside row group", overflow-safe for page_offset = -1.

A REFUSAL IS THE CONTAINMENT GUARD'S OWN: a reading is "XX001 (containment guard)"
only when the primary message is the guard's, so a crash or a later decode error is
not a refusal. A BACKEND THAT DIED DOES NOT PASS AS ONE THAT SURVIVED: survival also
reads the server log's count of backends killed by a signal since the arm began, and
an unreadable log fails it. AN INTACT TABLE STILL READS: the controls read rows
behind an OFFSET 0 fence, which an aggregate over an intact table would not, so a
guard that refuses a legal chunk is caught.

ON A PRIVATE DATABASE, built in the shell's order; each test asserts one reading.
"""
import re
import time

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_native_page_offset_bound"
GUARD_RE = r"lies outside row group"
WANT = "XX001 (containment guard)"
EVERY_1000 = ",".join(str(i) for i in range(1000, 20001, 1000))
OFF = "SET pgcolumnar.enable_column_projection = off"


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _run(dsn, sql, off=False):
    """-> None when every statement ran, or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            if off:
                conn.execute(OFF)
            conn.execute(sql)
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return None


def _value(dsn, sql, off=False):
    """-> the first column of the first row as text, or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            if off:
                conn.execute(OFF)
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    if row is None or row[0] is None:
        return query_error(f"no value from {sql}")
    return str(row[0])


def _refusal(dsn, sql, off=False):
    """-> "XX001 (containment guard)" when the primary message is the guard's,
    "<SQLSTATE>: <message>" for any other error, or what happened instead."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            if off:
                conn.execute(OFF)
            conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        if not exc.sqlstate:
            return "no refusal: the connection was lost"
        msg = exc.diag.message_primary or ""
        return f"{exc.sqlstate} (containment guard)" if re.search(GUARD_RE, msg) else f"{exc.sqlstate}: {msg}"
    return "no error"


def _crashes(cluster):
    try:
        return (cluster.datadir / "server.log").read_text(errors="replace").count("terminated by signal")
    except OSError as exc:
        return query_error(f"crash count unreadable: {exc}")


def _survived(dsn, cluster, before):
    """-> "1" when the server answers and no backend died since `before`; after a
    crash, waits out the recovery so the arms after it are not collateral."""
    up, now = _value(dsn, "SELECT 1"), _crashes(cluster)
    if not isinstance(before, int) or not isinstance(now, int):
        return query_error(f"crash count unreadable: {before} / {now}")
    if up == "1" and now == before:
        return "1"
    for _ in range(60):
        if _value(dsn, "SELECT 1") == "1":
            break
        time.sleep(0.5)
    return f"no ([{up if up == '1' else ''}], {now - before} backend crashes)"


def _mkc(dsn):
    _run(dsn, "DROP TABLE IF EXISTS c")
    _run(dsn, "CREATE TABLE c (id int, r bigint, t text) USING pgcolumnar")
    _run(dsn, "INSERT INTO c SELECT g, (random()*9e18)::bigint, 'v'||(g%5) FROM generate_series(1,20000) g")


def _corrupt(dsn, setexpr, column_index):
    _run(dsn, f"UPDATE pgcolumnar.column_chunk SET page_offset = {setexpr} "
              f"WHERE storage_id = pgcolumnar.get_storage_id('c') AND column_index = {column_index}")


@pytest.fixture(scope="module")
def pob(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        crashes = lambda: _crashes(pgc_cluster)  # noqa: E731
        alive = lambda b: _survived(dsn, pgc_cluster, b)  # noqa: E731
        r = {}

        _mkc(dsn)
        r["intact off"] = _value(dsn, "SELECT string_agg(id::text, ',' ORDER BY id) FROM "
                                      "(SELECT * FROM c WHERE id % 1000 = 0 AND r IS NOT NULL "
                                      "AND t LIKE 'v%' OFFSET 0) s", off=True)
        r["intact proj"] = _value(dsn, "SELECT string_agg(id::text, ',' ORDER BY id) FROM "
                                       "(SELECT id FROM c WHERE id % 1000 = 0 OFFSET 0) s")

        _mkc(dsn)
        _corrupt(dsn, "page_offset + 999999999", 1)
        b = crashes()
        r["proj overrun"] = _refusal(dsn, "SELECT sum(r) FROM c")
        r["proj overrun up"] = alive(b)

        _mkc(dsn)
        _corrupt(dsn, "page_offset + 999999999", 1)
        b = crashes()
        r["whole overrun"] = _refusal(dsn, "SELECT * FROM c", off=True)
        r["whole overrun up"] = alive(b)

        _mkc(dsn)
        _corrupt(dsn, "0", 2)
        b = crashes()
        r["whole wrap"] = _refusal(dsn, "SELECT * FROM c", off=True)
        r["whole wrap up"] = alive(b)

        _mkc(dsn)
        _corrupt(dsn, "-1", 1)
        b = crashes()
        r["whole minus1"] = _refusal(dsn, "SELECT * FROM c", off=True)
        r["whole minus1 up"] = alive(b)
        b = crashes()
        r["proj minus1"] = _refusal(dsn, "SELECT sum(r) FROM c")
        r["proj minus1 up"] = alive(b)
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_an_intact_table_reads_every_row_with_projection_off(pob, expect):
    expect.text(pob["intact off"], EVERY_1000, "an intact table reads every row and value with projection off")


def test_an_intact_tables_projected_read_returns_every_id(pob, expect):
    expect.text(pob["intact proj"], EVERY_1000, "an intact table's projected read returns every id")


def test_a_projected_page_offset_overrun_is_refused_by_the_guard(pob, expect):
    expect.text(pob["proj overrun"], WANT, "projected page_offset overrun is refused by the containment guard")


def test_the_backend_survives_the_projected_overrun(pob, expect):
    expect.text(pob["proj overrun up"], "1", "alive after projected overrun")


def test_a_whole_group_page_offset_overrun_is_refused_by_the_guard(pob, expect):
    expect.text(pob["whole overrun"], WANT, "whole-group page_offset overrun is refused by the containment guard")


def test_the_backend_survives_the_whole_group_overrun(pob, expect):
    expect.text(pob["whole overrun up"], "1", "alive after whole-group overrun")


def test_a_whole_group_page_offset_of_zero_is_refused_by_the_guard(pob, expect):
    expect.text(pob["whole wrap"], WANT, "whole-group page_offset=0 is refused by the containment guard")


def test_the_backend_survives_the_whole_group_wrap(pob, expect):
    expect.text(pob["whole wrap up"], "1", "alive after whole-group wrap")


def test_a_whole_group_page_offset_of_minus_one_hits_the_guard(pob, expect):
    expect.text(pob["whole minus1"], WANT, "whole-group page_offset=-1 hits the containment guard")


def test_the_backend_survives_the_whole_group_minus_one(pob, expect):
    expect.text(pob["whole minus1 up"], "1", "alive after whole-group page_offset=-1")


def test_a_projected_page_offset_of_minus_one_hits_the_guard(pob, expect):
    expect.text(pob["proj minus1"], WANT, "projected page_offset=-1 hits the containment guard")


def test_the_backend_survives_the_projected_minus_one(pob, expect):
    expect.text(pob["proj minus1 up"], "1", "alive after projected page_offset=-1")
