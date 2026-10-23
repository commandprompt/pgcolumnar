"""A scan is cancellable while it loads a row group: the pytest twin of
`test/native_cancel.sh`, every check by the shell's name (#1341).

The executor checks for interrupts once per tuple, which is no help while a scan
decodes a vector of pgcolumnar.chunk_group_row_limit values (user-settable, unbounded)
before its first row. COLUMNAR_DECODE_INTERRUPT, every 65536 values in the decode
loops, is what lets statement_timeout land inside that load.

ONE ROW GROUP, ONE VECTOR, ONE COLUMN, as the shell builds it: the whole table decodes
in a single load, and the only interrupt point inside it is the decode loop's.
pgcolumnar_native_load_group() also checks before each column chunk; on the old
two-column table the second chunk's check fell inside the window, and with the decode
loop's check disabled the cancel landed there and the timing check passed (#1341).

SELF-CALIBRATING, as the shell is: the timeout is a quarter of the server's own
Execution Time for the load, at least 5 ms (#1481), and the cancel must land in the
lower half of the interval between the timeout and a full load, both timed from the
client with a connection each, as psql is. The quickest of three cancels is taken: a
cancel cannot land early without an interrupt point inside the load, so a retry cannot
pass a missing guard. Every attempt must raise the timeout's own 57014, and only
those that did are timed: a connection refused during crash recovery fails in a few ms.

A PRIVATE DATABASE, because "one row group" counts pgcolumnar.row_group, which every
test's tables share.
"""
import os
import time

import psycopg
import pytest


DB = "pgc_native_cancel"
ROWS = int(os.environ.get("PGC_CANCEL_ROWS", "16000000"))
TIMEOUT_ERR = "57014: canceling statement due to statement timeout"
SERIAL = "SET max_parallel_workers_per_gather = 0"
LOAD = "SELECT * FROM cancel_t LIMIT 1"


def _timed(dsn, *sqls):
    """-> (ms from connecting to the last statement's end, "SQLSTATE: message" of its
    error or "no error"), as the shell's ms_for times a psql."""
    t0 = time.monotonic()
    said = "no error"
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as c:
            for sql in sqls[:-1]:
                c.execute(sql)
            c.execute(sqls[-1]).fetchall()
    except psycopg.Error as exc:
        first = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        said = f"{exc.sqlstate}: {exc.diag.message_primary}" if exc.sqlstate else first
    return round((time.monotonic() - t0) * 1000), said


@pytest.fixture(scope="module")
def nc(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    dsn = pgc_cluster.dsn(dbname=DB)
    try:
        r = {}
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            c.execute(f"SET pgcolumnar.stripe_row_limit = {ROWS}")
            c.execute(f"SET pgcolumnar.chunk_group_row_limit = {ROWS}")
            c.execute("CREATE TABLE cancel_t (v bigint) USING pgcolumnar")
            c.execute(f"INSERT INTO cancel_t SELECT g * 7 FROM generate_series(1, {ROWS}) g")
            r["groups"] = c.execute("SELECT count(*) FROM pgcolumnar.row_group").fetchone()[0]
        # a full uninterrupted load, twice, so the second is warm like the timed ones
        _timed(dsn, SERIAL, LOAD)
        r["full"], _ = _timed(dsn, SERIAL, LOAD)
        # the server's own Execution Time, which leaves out the client's connect
        exec_ms = None
        try:
            with psycopg.connect(dsn, autocommit=True) as c:
                c.execute(SERIAL)
                plan = c.execute(f"EXPLAIN (ANALYZE, TIMING OFF, SUMMARY ON, FORMAT JSON) {LOAD}").fetchone()[0]
            exec_ms = int(plan[0]["Execution Time"])
        except (psycopg.Error, KeyError, IndexError, TypeError) as exc:
            print(f"-- Execution Time unreadable: {exc}")
        r["exec"] = exec_ms
        r["timeout"] = max((exec_ms or 0) // 4, 5)
        cancels, r["err"] = [], TIMEOUT_ERR
        for _ in range(3):
            ms, said = _timed(dsn, SERIAL, f"SET statement_timeout = '{r['timeout']}ms'", LOAD)
            # only a cancel times a cancel: a connection refused during crash recovery
            # fails in a few ms, and counted it would read as an early cancel
            if said != TIMEOUT_ERR:
                r["err"] = said
                continue
            cancels.append(ms)
        r["cancel"] = min(cancels) if cancels else None
        print(f"-- server-side load {exec_ms} ms; statement timeout {r['timeout']} ms; "
              f"full load {r['full']} ms; cancels {cancels} ms")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_one_row_group(nc, expect):
    expect.text(str(nc["groups"]), "1", "one row group")


def test_the_load_outlasts_the_timeout(nc, expect):
    load, t = nc["exec"], nc["timeout"]
    expect.text("1" if load is not None and load > t else f"0 (load {load} ms, timeout {t} ms)", "1",
                "premise: the load outlasts the timeout, so the timeout can fire during it")


def test_the_short_timeout_is_what_fired(nc, expect):
    expect.text("OK" if nc["err"] == TIMEOUT_ERR else nc["err"], "OK", "the short timeout is what fired")


def test_the_cancel_arrives_well_before_the_load_completes(nc, expect):
    t, full, cancel = nc["timeout"], nc["full"], nc["cancel"]
    limit = t + (full - t) // 2
    expect.text("yes" if full > 2 * t and cancel is not None and cancel < limit else f"no (cancel={cancel}ms full={full}ms limit={limit}ms)",
                "yes", "cancel arrives well before the load completes")
