"""Concurrent deletes into one chunk group serialize on the chunk-group lock and both
survive (tracking issue #4). The pytest twin of `test/concurrency.sh`, every check by
the shell's name (#1341).

Two sessions delete different rows of one chunk group: the second must wait on the
chunk-group (advisory) lock, and after both commit both rows are gone -- with the
group's delete_vector row already present (A) and absent (B). Deletes into different
chunk groups must not wait at all, and must finish in a live transaction (C).

ON A PRIVATE DATABASE, built once by a module fixture. Session 2's blocking DELETE runs
in a thread while the controller polls pg_stat_activity, as the shell polls it, and
every wait is bounded at 60s. A wait that times out is recorded under the shell's
fixed name for that kind of wait, in its own test, which declines on a green run --
the shell's wait_timeout records only on its timeout path, so there is nothing to
assert there.
"""
import threading
import time

import psycopg
import pytest

from pgc_vacuity import QUERY_ERROR, query_error

DB = "pgc_concurrency"
WAIT = 60.0
# The wait is the shell's: any Lock wait. The CHECK reads the chunk-group
# (advisory) lock, which is what the arm claims.
WAITING = "SELECT count(*) FROM pg_stat_activity WHERE application_name='cc_s2' AND wait_event_type='Lock'"
BLOCKED = WAITING + " AND wait_event='advisory'"


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _ctl(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return query_error(f"no row from {sql}") if row is None else row[0]


def _session(dsn, name):
    conn = psycopg.connect(dsn, application_name=name)
    conn.execute("SET lock_timeout = '60s'")
    conn.execute("SET statement_timeout = '60s'")
    conn.commit()
    return conn


class _Run:
    """Session 2's statement in a thread, so the controller can watch it wait."""

    def __init__(self, conn, sql):
        self.error = None
        self.thread = threading.Thread(target=self._go, args=(conn, sql), daemon=True)
        self.thread.start()

    def _go(self, conn, sql):
        try:
            conn.execute(sql)
        except psycopg.Error as exc:
            self.error = _message(exc)

    def join(self):
        self.thread.join(WAIT)
        return not self.thread.is_alive()


def _poll(dsn, sql, want):
    deadline = time.monotonic() + WAIT
    while time.monotonic() < deadline:
        if _ctl(dsn, sql) == want:
            return True
        time.sleep(0.05)
    return False


def _step(r, conn, sql, label):
    """A session command; one that fails or runs past the bound is the shell's
    command-sentinel timeout."""
    try:
        conn.execute(sql)
    except psycopg.Error as exc:
        r["timeouts"].setdefault("command", f"{label}: {_message(exc)}")


def _commit(r, conn, label):
    try:
        conn.commit()
    except psycopg.Error as exc:
        r["timeouts"].setdefault("command", f"{label}: {_message(exc)}")


def _ids(dsn, table):
    return _ctl(dsn, f"SELECT string_agg(id::text, ',' ORDER BY id) FROM {table}")


@pytest.fixture(scope="module")
def cc(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    s1 = s2 = None
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            c.execute("CREATE TABLE t (id int) USING pgcolumnar")
            c.execute("INSERT INTO t SELECT g FROM generate_series(1,6) g")
            c.execute("DELETE FROM t WHERE id = 6")
            c.execute("CREATE TABLE t2 (id int) USING pgcolumnar")
            c.execute("INSERT INTO t2 SELECT g FROM generate_series(1,6) g")
            c.execute("CREATE TABLE t3 (id int) USING pgcolumnar")
            c.execute("INSERT INTO t3 SELECT g FROM generate_series(1,4) g")
            c.execute("INSERT INTO t3 SELECT g FROM generate_series(5,8) g")
        s1, s2 = _session(dsn, "cc_s1"), _session(dsn, "cc_s2")
        r = {"timeouts": {}}

        for key, table in (("A", "t"), ("B", "t2")):
            _step(r, s1, f"DELETE FROM {table} WHERE id = 1", f"s1/{key.lower()}_del")
            run = _Run(s2, f"DELETE FROM {table} WHERE id = 2")
            if not _poll(dsn, WAITING, 1):
                r["timeouts"].setdefault("block", "cc_s2")
            r[f"{key} blocks"] = _ctl(dsn, BLOCKED)
            _commit(r, s1, f"s1/{key.lower()}_commit")
            if not run.join():
                r["timeouts"].setdefault("sentinel", f"s2/{key.lower()}_del2")
            _commit(r, s2, f"s2/{key.lower()}_commit")
            r[f"{key} ids"] = _ids(dsn, table)
        r["A count"] = _ctl(dsn, "SELECT count(*) FROM t")

        _step(r, s1, "DELETE FROM t3 WHERE id = 1", "s1/c_del")
        run = _Run(s2, "DELETE FROM t3 WHERE id = 5")
        if not _poll(dsn, "SELECT state FROM pg_stat_activity WHERE application_name='cc_s2'", "idle in transaction"):
            r["timeouts"].setdefault("idle", "cc_s2")
        r["C state"] = _ctl(dsn, "SELECT coalesce(state, '<none>') || '/' || "
                                 "(wait_event_type IS NOT DISTINCT FROM 'Lock') FROM pg_stat_activity "
                                 "WHERE application_name='cc_s2'")
        _commit(r, s1, "s1/c_commit")
        run.join()
        _commit(r, s2, "s2/c_commit")
        r["C ids"] = _ids(dsn, "t3")
        yield r
    finally:
        for s in (s1, s2):
            if s is not None:
                try:
                    s.close()
                except psycopg.Error:
                    pass
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_a_same_group_second_deleter_blocks(cc, expect):
    expect.text(str(cc["A blocks"]), "1", "A same-group second deleter blocks")


def test_a_both_concurrent_deletes_survived(cc, expect):
    expect.text(str(cc["A ids"]), "3,4,5", "A both concurrent deletes survived (ids 1,2,6 gone)")


def test_a_row_count_after_concurrent_deletes(cc, expect):
    expect.text(str(cc["A count"]), "3", "A row count after concurrent deletes")


def test_b_first_delete_second_deleter_blocks(cc, expect):
    expect.text(str(cc["B blocks"]), "1", "B first-delete second deleter blocks")


def test_b_both_first_deletes_survived(cc, expect):
    expect.text(str(cc["B ids"]), "3,4,5,6", "B both first deletes survived (ids 1,2 gone)")


def test_c_different_group_deleter_does_not_block(cc, expect):
    expect.text(str(cc["C state"]), "idle in transaction/false", "C different-group deleter does not block")


def test_c_both_different_group_deletes_survived(cc, expect):
    expect.text(str(cc["C ids"]), "2,3,4,6,7,8", "C both different-group deletes survived (ids 1,5 gone)")


def test_every_bounded_wait_completed(cc, expect):
    """The shell's wait_timeout names, recorded only where a wait timed out."""
    t = cc["timeouts"]
    if not t:
        expect.cannot_run("UNMET_PRECONDITION", "no bounded wait timed out, so there is no timeout to record")
        return
    if "command" in t:
        expect.text(f"timed out waiting for {t['command']}", "completed",
                    "a bounded wait for a command's sentinel completed")
    if "sentinel" in t:
        expect.text(f"timed out waiting for {t['sentinel']}", "completed",
                    "a bounded wait for a standalone sentinel completed")
    if "block" in t:
        expect.text(f"timed out waiting for {t['block']}", "completed",
                    "a bounded wait for a session to block completed")
    if "idle" in t:
        expect.text(f"timed out waiting for {t['idle']}", "completed",
                    "a bounded wait for a session to reach idle-in-transaction completed")
