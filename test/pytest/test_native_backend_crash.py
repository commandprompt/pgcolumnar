"""One backend dies, the postmaster lives, and committed columnar data is intact
(#217, #1341). The pytest twin of `test/native_backend_crash.sh`, every check by the
shell's name.

A SIGSEGV in one backend makes the postmaster drop every session and reinitialize
shared memory, with crash recovery, while the postmaster itself stays up. Columnar
data goes through the buffer manager and WAL, so committed rows must match a heap
mirror afterwards, and an INSERT killed before its commit must leave no row and no
row group. The kill lands at two points: mid-write, once the INSERT has written
pages to the relation and before it reaches its trailing pg_sleep, and after every
stripe has flushed, while the backend sleeps. Both are asserted, not assumed.

The write is uncommitted either way, so "no rows survived" cannot tell a crash from
a clean abort. A sentinel session, opened before each kill, must be dropped by the
reinit: that is what proves the crash happened.

A PRIVATE CLUSTER, because the reinit drops every session on the server.
"""
import os
import pathlib
import shutil
import signal
import tempfile
import threading
import time

import psycopg
import pytest

from pgc_vacuity import query_error

BIG_STRIPE = None   # a stripe_row_limit for the inserting session; None keeps the default


@pytest.fixture(scope="module")
def nbc(pgc_cluster):
    """-> the private cluster, started, with the extension and the h/c pair at 5000 rows."""
    from pgc_cluster import Cluster, pick_port

    work = pathlib.Path(tempfile.mkdtemp(prefix="pgc-nbcrash-"))
    work.chmod(0o777)
    datadir = work / "data"
    datadir.mkdir()
    cluster = Cluster(pgc_cluster.pg_config, "nbcrash", datadir, pick_port(14))
    try:
        cluster.initdb()
        cluster.start()
        with psycopg.connect(cluster.dsn(), autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            # the postmaster must reinitialize after a backend crash, not shut down
            c.execute("ALTER SYSTEM SET restart_after_crash = on")
            c.execute("SELECT pg_reload_conf()")
            c.execute("CREATE TABLE h (id int, v text, x float8)")
            c.execute("CREATE TABLE c (id int, v text, x float8) USING pgcolumnar")
            c.execute("INSERT INTO h SELECT g, 'b' || g, g * 2.5 FROM generate_series(1, 5000) g")
            c.execute("INSERT INTO c SELECT g, 'b' || g, g * 2.5 FROM generate_series(1, 5000) g")
        yield cluster
    finally:
        cluster.stop()
        shutil.rmtree(work, ignore_errors=True)


def _one(cluster, sql):
    """-> the first column of the first row, or a distinct failure sentinel."""
    try:
        with psycopg.connect(cluster.dsn(), autocommit=True, connect_timeout=5) as c:
            return c.execute(sql).fetchone()[0]
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0])


def _rows(cluster, sql):
    try:
        with psycopg.connect(cluster.dsn(), autocommit=True, connect_timeout=5) as c:
            return c.execute(sql).fetchall()
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0])


def _row_groups(cluster):
    return _one(cluster, "SELECT count(*) FROM pgcolumnar.row_group r JOIN pgcolumnar.storage s "
                         "ON s.storage_id = r.storage_id WHERE s.relation_oid = 'c'::regclass")


def _postmaster_pid(cluster):
    return (cluster.datadir / "postmaster.pid").read_text().splitlines()[0]


class _Bystander:
    """A session parked in pg_sleep on its own thread. -> .dropped once the server
    ended it, which a plain exit of another backend never does."""

    def __init__(self, cluster, seconds):
        self.conn = psycopg.connect(cluster.dsn(), autocommit=True)
        self.pid = self.conn.info.backend_pid
        self.error = None
        self.thread = threading.Thread(target=self._run, args=(seconds,), daemon=True)
        self.thread.start()

    def _run(self, seconds):
        try:
            self.conn.execute(f"SELECT pg_sleep({seconds})")
        except psycopg.Error as exc:
            self.error = exc

    def dropped(self, within):
        self.thread.join(within)
        return not self.thread.is_alive() and self.error is not None

    def close(self, cluster):
        _end(cluster, self.conn, self.thread, self.pid)


def _end(cluster, conn, thread, pid):
    """Close a session whose statement may still run on its thread. A live one is
    terminated first: closing a connection under a running statement crashes the
    driver, and a session a failed check left behind holds its locks into the next
    test."""
    if thread.is_alive():
        _one(cluster, f"SELECT pg_terminate_backend({pid})")
        thread.join(10)
    try:
        conn.close()
    except psycopg.Error:
        pass


def _wait_up(cluster):
    """Twelve successful connections in a row, 0.25s apart: a 3s window a reinit's
    brief accepting blip cannot fake. -> True once it holds."""
    ok = 0
    for _ in range(1200):
        if _one(cluster, "SELECT 1") == 1:
            ok += 1
            if ok >= 12:
                return True
        else:
            ok = 0
        time.sleep(0.25)
    return False


def _crash_one_backend(cluster, expect, rows, marker, phase):
    """An INSERT into c, held open by a trailing pg_sleep so it never commits, SIGSEGVed
    in `phase` ('insert': mid-write, 'sleep': after every stripe flushed).
    -> whether the sentinel was dropped by the reinit."""
    sentinel = _Bystander(cluster, 120)
    size0 = _one(cluster, "SELECT pg_relation_size('c')")
    writer = psycopg.connect(cluster.dsn(), autocommit=True)
    pid = writer.info.backend_pid

    def write():
        try:
            if BIG_STRIPE:
                writer.execute(f"SET pgcolumnar.stripe_row_limit = {BIG_STRIPE}")
            writer.execute("BEGIN")
            writer.execute(f"INSERT INTO c /* {marker} */ SELECT g, 'k' || g, g FROM generate_series(1, {rows}) g")
            writer.execute("SELECT pg_sleep(60)")
        except psycopg.Error:
            pass
    thread = threading.Thread(target=write, daemon=True)
    thread.start()
    try:
        return _kill_when_ready(cluster, expect, marker, phase, pid, size0, thread, sentinel)
    finally:
        _end(cluster, writer, thread, pid)
        sentinel.close(cluster)


def _kill_when_ready(cluster, expect, marker, phase, pid, size0, thread, sentinel):
    def wait_event():
        return _one(cluster, f"SELECT coalesce(wait_event, '') FROM pg_stat_activity WHERE pid = {pid}")

    for _ in range(600):
        if phase == "sleep":
            if wait_event() == "PgSleep":
                break
        elif _one(cluster, f"SELECT pg_relation_size('c') > {size0}") is True:
            break
        time.sleep(0.1)
    expect.text(str(_one(cluster, f"SELECT pg_relation_size('c') > {size0}")), "True",
                f"{marker}: the INSERT had written pages before the kill (the relation grew)")
    we = wait_event()
    if phase == "sleep":
        expect.text(we, "PgSleep", f"{marker}: kill lands after the stripes flushed (backend in pg_sleep)")
    else:
        expect.text("yes" if we != "PgSleep" else "no", "yes",
                    f"{marker}: kill lands mid-write (backend not yet in pg_sleep)")

    os.kill(pid, signal.SIGSEGV)
    thread.join(30)
    _wait_up(cluster)
    return sentinel.dropped(5)


def _assert_intact(cluster, expect, label, dropped, base):
    expect.text("yes" if dropped else "no", "yes",
                f"{label}: the crash reinitialized the cluster (a live session was dropped)")
    expect.text(_postmaster_pid(cluster), base["pm"], f"{label}: postmaster survived, same pid")
    expect.num(_one(cluster, "SELECT 1"), 1, f"{label}: cluster accepts new connections")
    expect.row_set(_rows(cluster, "SELECT id, v, x FROM c"), base["content"],
                   f"{label}: committed content in c matches the heap mirror")
    expect.row_set(_rows(cluster, "SELECT id, v, x FROM h"), base["mirror"],
                   f"{label}: the mirror itself is unchanged")
    expect.num(_one(cluster, "SELECT count(*) FROM c"), 5000, f"{label}: the killed insert left no visible rows")
    expect.num(_row_groups(cluster), base["groups"], f"{label}: the killed insert left no partial row group")


@pytest.fixture(scope="module")
def base(nbc):
    """The committed baseline, read once before any crash."""
    return {"groups": _row_groups(nbc), "content": _rows(nbc, "SELECT id, v, x FROM c"),
            "mirror": _rows(nbc, "SELECT id, v, x FROM h"), "pm": _postmaster_pid(nbc)}


def test_a_kill_mid_write(nbc, base, expect):
    dropped = _crash_one_backend(nbc, expect, 3000000, "crashA", "insert")
    _assert_intact(nbc, expect, "mid-write", dropped, base)


def test_a_kill_after_the_stripes_flushed(nbc, base, expect):
    dropped = _crash_one_backend(nbc, expect, 100000, "crashB", "sleep")
    _assert_intact(nbc, expect, "post-flush", dropped, base)


def test_a_kill_with_a_second_reader(nbc, base, expect):
    """The reinit drops every session, so a second parked reader goes too."""
    reader = _Bystander(nbc, 90)
    try:
        dropped = _crash_one_backend(nbc, expect, 3000000, "crashC", "insert")
        expect.text("yes" if reader.dropped(5) else "no", "yes",
                    "a second concurrent reader was also dropped by the crash")
    finally:
        reader.close(nbc)
    _assert_intact(nbc, expect, "with-reader", dropped, base)
