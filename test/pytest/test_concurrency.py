"""Concurrent deletes into one chunk group serialize on the chunk-group lock and both
survive (tracking issue #4). The pytest twin of `test/concurrency.sh`, every check by
the shell's name (#1341).

Two sessions delete different rows of one chunk group: the second must wait on the
chunk-group (advisory) lock, and after both commit both rows are gone -- with the
group's delete_vector row already present (A) and absent (B). Deletes into different
chunk groups must not wait at all, and must finish in a live transaction (C).

D, E and F (#2184): a writer's chunk-group lock (class 101) and storage-row lock
(class 102) form no cycle. A transaction that inserts and then deletes in a group, and
one that deletes in that group and then inserts, both commit (D), also on a table an
earlier build stamped, where the DELETE waits for the raising INSERT before it takes
its group lock (E). A second first writer that waited for the first keeps only
ShareLock once the row exists, so a third writer does not wait for it (F). Each
session there is a queue of statements run by its own thread, as the shell's sessions
are psql reading a FIFO, so a statement that blocks never blocks the controller.

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


class _Queue:
    """A session fed statements in order by its own thread, like the shell's psql on
    a FIFO: send() never waits, and done(label) says whether the statements up to that
    label have returned. A failed statement is recorded, and the session goes on, as
    psql without ON_ERROR_STOP does."""

    def __init__(self, dsn, name):
        import queue
        self.conn = psycopg.connect(dsn, application_name=name, autocommit=True)
        self.conn.execute("SET lock_timeout = '60s'")
        self.conn.execute("SET statement_timeout = '60s'")
        self.q = queue.Queue()
        self.labels = set()
        self.errors = []
        self.thread = threading.Thread(target=self._go, daemon=True)
        self.thread.start()

    def _go(self):
        while True:
            sql, label = self.q.get()
            if sql is None:
                return
            if sql:
                try:
                    self.conn.execute(sql)
                except psycopg.Error as exc:
                    self.errors.append(_message(exc))
            if label:
                self.labels.add(label)

    def send(self, sql, label=None):
        self.q.put((sql, label))

    def done(self, label):
        return label in self.labels

    def close(self):
        self.q.put((None, None))
        self.thread.join(5)
        try:
            self.conn.close()
        except psycopg.Error:
            pass


def _until(pred):
    deadline = time.monotonic() + WAIT
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


def _lks(dsn, app, cls):
    return _ctl(dsn, "SELECT coalesce(string_agg(l.mode || CASE WHEN l.granted THEN '' ELSE '(waiting)' END, "
                     "',' ORDER BY l.mode), '<none>') FROM pg_locks l JOIN pg_stat_activity a USING (pid) "
                     f"WHERE a.application_name = '{app}' AND l.locktype = 'advisory' AND l.objsubid = {cls}")


def _lock_waiting(dsn, app):
    return _ctl(dsn, "SELECT count(*) FROM pg_stat_activity "
                     f"WHERE application_name='{app}' AND wait_event_type='Lock'") == 1


def _send_wait(r, sess, sql, label):
    sess.send(sql, label)
    if not _until(lambda: sess.done(label)):
        r["timeouts"].setdefault("command", label)


def _wait_label(r, sess, label):
    if not _until(lambda: sess.done(label)):
        r["timeouts"].setdefault("sentinel", label)


def _wait_blocked(r, dsn, app):
    if not _until(lambda: _lock_waiting(dsn, app)):
        r["timeouts"].setdefault("block", app)


def _wait_settled(r, dsn, sess, app, label):
    if not _until(lambda: sess.done(label) or _lock_waiting(dsn, app)):
        r["timeouts"].setdefault("settle", app)


def _writers(r, dsn):
    """Scenarios D, E and F, in the shell's order, into r."""
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("CREATE TABLE t4 (id int) USING pgcolumnar")
        c.execute("INSERT INTO t4 SELECT g FROM generate_series(1,8) g")
        c.execute("CREATE TABLE t5 (id int) USING pgcolumnar")
        c.execute("INSERT INTO t5 SELECT g FROM generate_series(1,8) g")
        c.execute("UPDATE pgcolumnar.storage SET format_version = 1 WHERE relation_oid = 't5'::regclass")
        c.execute("CREATE TABLE t6 (id int) USING pgcolumnar")
    s1, s2 = _Queue(dsn, "cc_s1"), _Queue(dsn, "cc_s2")
    try:
        both = ("SELECT count(*) FILTER (WHERE id IN (5,6)) || ' ' || "
                          "count(*) FILTER (WHERE id IN (0,-1)) FROM {}")

        # D
        mark = len(s1.errors) + len(s2.errors)
        _send_wait(r, s1, "BEGIN", "d_begin")
        _send_wait(r, s1, "INSERT INTO t4 VALUES (0); SELECT count(*) FROM t4", "d_ins")
        r["D s1 102"] = _lks(dsn, "cc_s1", 102)
        _send_wait(r, s2, "BEGIN", "d_begin")
        _send_wait(r, s2, "DELETE FROM t4 WHERE id = 6; SELECT count(*) FROM t4", "d_del")
        r["D s2 101"] = _lks(dsn, "cc_s2", 101)
        r["D s2 102"] = _lks(dsn, "cc_s2", 102)
        s1.send("DELETE FROM t4 WHERE id = 5; SELECT count(*) FROM t4", "d_del1")
        _wait_blocked(r, dsn, "cc_s1")
        r["D s1 waits"] = _lks(dsn, "cc_s1", 101)
        _send_wait(r, s2, "INSERT INTO t4 VALUES (-1); SELECT count(*) FROM t4", "d_ins")
        r["D s1 still waits"] = _lks(dsn, "cc_s1", 101)
        _send_wait(r, s2, "COMMIT", "d_commit")
        _wait_label(r, s1, "d_del1")
        _send_wait(r, s1, "COMMIT", "d_commit")
        r["D errors"] = len(s1.errors) + len(s2.errors) - mark
        r["D rows"] = _ctl(dsn, both.format("t4"))

        # E
        r["E stamp before"] = _ctl(dsn, "SELECT format_version FROM pgcolumnar.storage "
                                        "WHERE relation_oid = 't5'::regclass")
        mark = len(s1.errors) + len(s2.errors)
        _send_wait(r, s1, "BEGIN", "e_begin")
        _send_wait(r, s1, "INSERT INTO t5 VALUES (0); SELECT count(*) FROM t5", "e_ins")
        r["E s1 102"] = _lks(dsn, "cc_s1", 102)
        _send_wait(r, s2, "BEGIN", "e_begin")
        s2.send("DELETE FROM t5 WHERE id = 6; SELECT count(*) FROM t5", "e_del2")
        _wait_settled(r, dsn, s2, "cc_s2", "e_del2")
        r["E s2 waits first"] = f"{_lks(dsn, 'cc_s2', 102)}/{_lks(dsn, 'cc_s2', 101)}"
        s1.send("DELETE FROM t5 WHERE id = 5; SELECT count(*) FROM t5", "e_del1")
        _wait_settled(r, dsn, s1, "cc_s1", "e_del1")
        r["E s1 returns"] = f"{1 if s1.done('e_del1') else 0}/{_lks(dsn, 'cc_s1', 101)}"
        s1.send("COMMIT", "e_c1")
        _wait_label(r, s2, "e_del2")
        r["E s2 after"] = f"{_lks(dsn, 'cc_s2', 102)}/{_lks(dsn, 'cc_s2', 101)}"
        s2.send("INSERT INTO t5 VALUES (-1); SELECT count(*) FROM t5")
        s2.send("COMMIT", "e_c2")
        _wait_label(r, s1, "e_c1")
        _wait_label(r, s2, "e_c2")
        r["E errors"] = len(s1.errors) + len(s2.errors) - mark
        stamp = "SELECT format_version FROM pgcolumnar.storage WHERE relation_oid = 't5'::regclass"
        r["E rows"] = f"{_ctl(dsn, both.format('t5'))} {_ctl(dsn, stamp)}"

        # F
        r["F no row"] = _ctl(dsn, "SELECT count(*) FROM pgcolumnar.storage WHERE relation_oid = 't6'::regclass")
        mark = len(s1.errors) + len(s2.errors)
        _send_wait(r, s1, "BEGIN", "f_begin")
        _send_wait(r, s1, "INSERT INTO t6 VALUES (1); SELECT count(*) FROM t6", "f_ins")
        r["F s1 102"] = _lks(dsn, "cc_s1", 102)
        _send_wait(r, s2, "BEGIN", "f_begin")
        s2.send("INSERT INTO t6 VALUES (2); SELECT count(*) FROM t6", "f_ins2")
        _wait_blocked(r, dsn, "cc_s2")
        r["F s2 waits"] = _lks(dsn, "cc_s2", 102)
        _send_wait(r, s1, "COMMIT", "f_commit")
        _wait_label(r, s2, "f_ins2")
        r["F s2 102"] = _lks(dsn, "cc_s2", 102)
        _send_wait(r, s1, "BEGIN; SET LOCAL lock_timeout = '2s'; INSERT INTO t6 VALUES (3); "
                          "SELECT count(*) FROM t6; COMMIT", "f_ins3")
        r["F third"] = (f"{_ctl(dsn, 'SELECT count(*) FROM t6 WHERE id = 3')}/"
                        f"{len(s1.errors) + len(s2.errors) - mark}")
        _send_wait(r, s2, "COMMIT", "f_commit")
        r["F ids"] = _ids(dsn, "t6")
    finally:
        s1.close()
        s2.close()


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
        for s in (s1, s2):
            s.close()
        s1 = s2 = None
        _writers(r, dsn)
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


def test_d_insert_and_scan_hold_share_lock_only(cc, expect):
    expect.text(str(cc["D s1 102"]), "ShareLock",
                "D an INSERT and a scan leave s1 holding the storage-row lock in ShareLock only")


def test_d_premise_delete_holds_group_lock(cc, expect):
    expect.text(str(cc["D s2 101"]), "ExclusiveLock",
                "premise: D s2's DELETE holds the group lock of the row it deleted")


def test_d_delete_takes_no_storage_row_lock(cc, expect):
    expect.text(str(cc["D s2 102"]), "<none>",
                "D that DELETE, on a table whose storage row is current, takes no storage-row lock")


def test_d_premise_delete_waits_on_group_lock(cc, expect):
    expect.text(str(cc["D s1 waits"]), "ExclusiveLock(waiting)",
                "premise: D s1's DELETE in that group waits on s2's group lock")


def test_d_insert_returns_while_s1_waits(cc, expect):
    expect.text(str(cc["D s1 still waits"]), "ExclusiveLock(waiting)",
                "D s2's INSERT after its DELETE returns while s1 still waits on s2's group lock")


def test_d_both_commit(cc, expect):
    expect.text(str(cc["D errors"]), "0",
                "D the inserting-then-deleting and the deleting-then-inserting transactions both commit")


def test_d_both_took_effect(cc, expect):
    expect.text(str(cc["D rows"]), "0 2", "D both DELETEs and both INSERTs took effect")


def test_e_premise_older_stamp(cc, expect):
    expect.text(str(cc["E stamp before"]), "1", "premise: E t5's storage row carries an earlier build's stamp")


def test_e_raiser_holds_exclusive(cc, expect):
    expect.text(str(cc["E s1 102"]), "ExclusiveLock",
                "E s1's INSERT raised the stamp and holds the storage-row lock in ExclusiveLock")


def test_e_delete_waits_before_group_lock(cc, expect):
    expect.text(str(cc["E s2 waits first"]), "ExclusiveLock(waiting)/<none>",
                "E a DELETE first in its transaction waits on the raiser's storage-row lock before it takes a group lock")


def test_e_raiser_delete_returns(cc, expect):
    expect.text(str(cc["E s1 returns"]), "1/ExclusiveLock",
                "E the raiser's DELETE in the waiting deleter's group returns at once")


def test_e_deleter_keeps_no_storage_row_lock(cc, expect):
    expect.text(str(cc["E s2 after"]), "<none>/ExclusiveLock",
                "E once the raiser commits, the deleter holds its group lock and no storage-row lock")


def test_e_both_commit(cc, expect):
    expect.text(str(cc["E errors"]), "0", "E the raising and the deleting transactions both commit")


def test_e_both_took_effect(cc, expect):
    expect.text(str(cc["E rows"]), "0 2 2",
                "E both DELETEs and both INSERTs took effect, and the stamp is raised")


def test_f_premise_no_storage_row(cc, expect):
    expect.text(str(cc["F no row"]), "0", "premise: F t6 has no storage row before its first write")


def test_f_first_writer_exclusive(cc, expect):
    expect.text(str(cc["F s1 102"]), "ExclusiveLock",
                "F the first writer holds the storage-row lock in ExclusiveLock")


def test_f_second_writer_waits(cc, expect):
    expect.text(str(cc["F s2 waits"]), "ExclusiveLock(waiting)",
                "F a second first writer waits on the storage-row lock")


def test_f_second_writer_share_only(cc, expect):
    expect.text(str(cc["F s2 102"]), "ShareLock",
                "F once the first writer commits, the second holds the storage-row lock in ShareLock only")


def test_f_third_writer_commits(cc, expect):
    expect.text(str(cc["F third"]), "1/0",
                "F a third writer's INSERT, with a 2 s lock_timeout, commits while the second is open")


def test_f_all_three_took_effect(cc, expect):
    expect.text(str(cc["F ids"]), "1,2,3", "F all three INSERTs took effect")


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
    if "settle" in t:
        expect.text(f"timed out waiting for {t['settle']}", "completed",
                    "a bounded wait for a session to finish or block completed")
    if "idle" in t:
        expect.text(f"timed out waiting for {t['idle']}", "completed",
                    "a bounded wait for a session to reach idle-in-transaction completed")
