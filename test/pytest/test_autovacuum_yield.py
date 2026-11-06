"""The maintenance daemon yields its lock to a waiting session (#415). The pytest twin of
`test/autovacuum_yield.sh`, every check by the shell's name (#1341).

The worker marks itself autovacuum, so a backend that queues for a lock conflicting
with the worker's ShareUpdateExclusiveLock has core cancel the worker after
deadlock_timeout. With the daemon holding that lock for a 30s maintenance hold, an
ACCESS EXCLUSIVE request must be granted within seconds, and the daemon must log that
it cancelled its operation.

THE SETTINGS ARRIVE BY ALTER SYSTEM AND A RELOAD, read back from a fresh session and
reset at teardown, since this corpus shares one cluster per worker. THE GRANT IS READ,
NOT ASSUMED: the shell once read a psql that never connected as "granted", and its
instant failure as "fast"; here the grant is the statement returning, and anything
else is its failure.
"""
import re
import time

import psycopg
import pytest

from pgc_vacuity import query_error

SCHEMA = "pgc_autovacuum_yield"
CG = 1000
TOUCHED = ("pgcolumnar.autovacuum", "pgcolumnar.autovacuum_naptime",
           "pgcolumnar.autovacuum_compact_threshold", "pgcolumnar.maintenance_hold_ms", "deadlock_timeout")


def _one(conn, sql):
    try:
        row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0])
    return query_error(f"no row from {sql}") if row is None else row[0]


def _fresh(cluster, sql):
    with psycopg.connect(cluster.dsn(), autocommit=True) as c:
        return _one(c, sql)


def _log_count(cluster, pattern):
    try:
        text = (cluster.datadir / "server.log").read_text(errors="replace")
    except OSError as exc:
        return query_error(f"server log unreadable: {exc}")
    return sum(1 for line in text.splitlines() if re.search(pattern, line))


@pytest.fixture(scope="module")
def ay(pgc_cluster):
    conn = psycopg.connect(pgc_cluster.dsn(), autocommit=True)
    conn.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
    conn.execute(f'CREATE SCHEMA "{SCHEMA}"')
    conn.execute(f'SET search_path TO "{SCHEMA}", public')
    r = {}
    try:
        for k, v in (("pgcolumnar.autovacuum", "on"), ("pgcolumnar.autovacuum_naptime", 2),
                     ("pgcolumnar.autovacuum_compact_threshold", 0.1),
                     ("pgcolumnar.maintenance_hold_ms", 30000), ("deadlock_timeout", "'1s'")):
            conn.execute(f"ALTER SYSTEM SET {k} = {v}")
        conn.execute("SELECT pg_reload_conf()")
        r["launcher"] = _fresh(pgc_cluster, "SELECT count(*) FROM pg_stat_activity "
                                            "WHERE backend_type = 'pgcolumnar autovacuum launcher'")
        r["settings"] = _fresh(pgc_cluster, "SELECT current_setting('pgcolumnar.autovacuum') || '/' || "
                                            "current_setting('pgcolumnar.maintenance_hold_ms')")
        logged_before = _log_count(pgc_cluster, r"pgcolumnar autovacuum: skipped .* canceling statement")

        conn.execute("CREATE TABLE yt (id int, v int) USING pgcolumnar")
        conn.execute(f"SELECT pgcolumnar.set_options('yt', chunk_group_row_limit => {CG})")
        conn.execute("INSERT INTO yt SELECT g, g % 100 FROM generate_series(1, 20000) g")
        conn.execute("DELETE FROM yt WHERE (id * 2654435761)::bigint % 100 < 40")
        conn.execute("SELECT pgcolumnar.compact('yt')")

        held = "no"
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            n = _one(conn, "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                           "WHERE l.relation = 'yt'::regclass AND l.mode = 'ShareUpdateExclusiveLock' "
                           "AND l.granted AND a.backend_type = 'pgcolumnar autovacuum worker'")
            if isinstance(n, int) and n >= 1:
                held = "yes"
                break
            time.sleep(0.5)
        r["held"] = held

        t0 = time.monotonic()
        try:
            with psycopg.connect(pgc_cluster.dsn(), autocommit=False) as w:
                w.execute(f'SET search_path TO "{SCHEMA}", public')
                w.execute("SET statement_timeout = '15s'")
                w.execute("LOCK TABLE yt IN ACCESS EXCLUSIVE MODE")
                w.commit()
            r["state"] = "granted"
        except psycopg.Error as exc:
            r["state"] = exc.sqlstate or query_error(str(exc).splitlines()[0])
        r["elapsed"] = time.monotonic() - t0
        time.sleep(1)
        after = _log_count(pgc_cluster, r"pgcolumnar autovacuum: skipped .* canceling statement")
        r["logged"] = (after - logged_before if isinstance(after, int) and isinstance(logged_before, int)
                       else query_error(f"log count: {logged_before} -> {after}"))
        yield r
    finally:
        for g in TOUCHED:
            try:
                conn.execute(f"ALTER SYSTEM RESET {g}")
            except psycopg.Error:
                pass
        conn.execute("SELECT pg_reload_conf()")
        conn.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
        conn.close()


def test_the_launcher_is_running_for_the_yield(ay, expect):
    expect.text(str(ay["launcher"]), "1", "the launcher is running")


def test_the_daemon_is_on_with_a_30s_maintenance_hold(ay, expect):
    expect.text(str(ay["settings"]), "on/30s", "premise: the daemon is on with a 30s maintenance hold")


def test_the_daemon_is_holding_its_lock_on_the_table(ay, expect):
    expect.text(ay["held"], "yes", "the daemon is holding SUEL on the table (the hold window is open)")


def test_yield_the_access_exclusive_lock_was_granted(ay, expect):
    expect.text(str(ay["state"]), "granted",
                "yield: the AccessExclusive lock was granted (no timeout -> daemon yielded)")


def test_yield_and_it_was_granted_fast(ay, expect):
    if ay["state"] != "granted":
        got = f"not granted ({ay['state']})"
    else:
        got = "fast" if ay["elapsed"] <= 8 else f"slow({ay['elapsed']:.0f}s)"
    expect.text(got, "fast", "yield: and it was granted FAST (<= 8s, far under the 30s hold)")


def test_the_daemon_logged_cancelling_its_maintenance_op(ay, expect):
    n = ay["logged"]
    expect.text(("yes" if n >= 1 else "no") if isinstance(n, int) else n, "yes",
                "the daemon logged cancelling its maintenance op")
