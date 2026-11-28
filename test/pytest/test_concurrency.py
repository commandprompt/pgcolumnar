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
ShareLock once the row exists, so a third writer does not wait for it (F). Writers of
unrelated rows that share a row-identity bucket (class 104) do not wait for each other:
a flush takes a bucket only if it is free (G, concurrent_diff's deadlock). What
serializes two writers of one row is the chunk-group lock of every group in the flush,
taken before the same-row check (H). A second first writer at REPEATABLE READ re-checks
the storage row against the latest committed state (I). Six writers of their own rows,
five rounds each, never fail (J, #2251). A rewrite verb after a write refuses with 55P03
while another writer is open, and runs once it is alone (K). The group locks are held
from before the same-row check to commit, so a writer of the same row cannot slip in
between (L), and a flush takes them in group order, not visit order (M). Each
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
                    self.errors.append(f"{exc.sqlstate}: {_message(exc)}")
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
                     "',' ORDER BY l.mode || CASE WHEN l.granted THEN '' ELSE '(waiting)' END), '<none>') FROM pg_locks l JOIN pg_stat_activity a USING (pid) "
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


SERIAL = "40001: columnar: could not serialize access due to concurrent update or delete of the same row"
M_SET = "SET LOCAL enable_hashjoin = off; SET LOCAL enable_mergejoin = off; SET LOCAL enable_seqscan = off;"
M_DEL = "DELETE FROM tm USING (VALUES (1, 2510), (2, 20)) v(o, id) WHERE tm.id = v.id"
J_W, J_R, J_N = 6, 5, 3000
J_ALL = J_W * J_R * J_N
K_VERBS = (("recluster", "pgcolumnar.recluster('%T', 'id')"),
           ("compact_rewrite", "pgcolumnar.compact_rewrite('%T', 0.0)"))


def _disjoint(r, dsn):
    """Scenario J: six writers, five rounds, each round one transaction on its own ids."""
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("CREATE TABLE tj_ins (id int, v bigint) USING pgcolumnar")
        c.execute("CREATE TABLE tj_pre (id int, v bigint) USING pgcolumnar")
        # one row group per round's ids: writers share buckets (class 104), no group lock
        c.execute(f"SELECT pgcolumnar.set_options('tj_pre', chunk_group_row_limit => 1000, stripe_row_limit => {J_N})")
        c.execute(f"INSERT INTO tj_pre SELECT g, g FROM generate_series(1, {J_ALL}) g")
    r["J want"] = _ctl(dsn, "SELECT count(*) || ' ' || sum(g + CASE WHEN g % 5 = 0 THEN 1 ELSE 0 END) "
                            f"FROM generate_series(1, {J_ALL}) g WHERE g % 7 <> 0")

    def run(table, insert):
        failed = []

        def writer(w):
            with psycopg.connect(dsn, autocommit=True) as c:
                for rd in range(J_R):
                    lo = (w * J_R + rd) * J_N + 1
                    hi = lo + J_N - 1
                    try:
                        with c.transaction():
                            c.execute("SET LOCAL statement_timeout = '60s'")
                            c.execute("SET LOCAL lock_timeout = '60s'")
                            if insert:
                                c.execute(f"INSERT INTO {table} SELECT g, g FROM generate_series({lo}, {hi}) g")
                            c.execute(f"DELETE FROM {table} WHERE id BETWEEN {lo} AND {hi} AND id % 7 = 0")
                            c.execute(f"UPDATE {table} SET v = v + 1 WHERE id BETWEEN {lo} AND {hi} AND id % 5 = 0")
                    except psycopg.Error as exc:
                        failed.append(f"{exc.sqlstate}: {_message(exc)}")

        threads = [threading.Thread(target=writer, args=(w,)) for w in range(J_W)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(600)
        return len(failed) + sum(1 for t in threads if t.is_alive())

    r["J ins failed"] = run("tj_ins", True)
    r["J ins rows"] = _ctl(dsn, "SELECT count(*) || ' ' || sum(v) FROM tj_ins")
    r["J pre failed"] = run("tj_pre", False)
    r["J pre rows"] = _ctl(dsn, "SELECT count(*) || ' ' || sum(v) FROM tj_pre")


def _buckets(dsn, app):
    return _ctl(dsn, "SELECT count(*) || '/' || current_setting('pgcolumnar.row_lock_buckets') FROM pg_locks l "
                     f"JOIN pg_stat_activity a USING (pid) WHERE a.application_name = '{app}' "
                     "AND l.locktype = 'advisory' AND l.objsubid = 104 AND l.granted")


def _writers(r, dsn):
    """Scenarios D, E, F and G, in the shell's order, into r."""
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("CREATE TABLE t4 (id int) USING pgcolumnar")
        c.execute("INSERT INTO t4 SELECT g FROM generate_series(1,8) g")
        c.execute("CREATE TABLE t5 (id int) USING pgcolumnar")
        c.execute("INSERT INTO t5 SELECT g FROM generate_series(1,8) g")
        c.execute("UPDATE pgcolumnar.storage SET format_version = 1 WHERE relation_oid = 't5'::regclass")
        c.execute("CREATE TABLE t6 (id int) USING pgcolumnar")
    s1, s2 = _Queue(dsn, "cc_s1"), _Queue(dsn, "cc_s2")
    s3 = None
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

        # G
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE TABLE t7 (id int) USING pgcolumnar")
            c.execute("INSERT INTO t7 SELECT g FROM generate_series(1,40000) g")
            c.execute("INSERT INTO t7 SELECT g FROM generate_series(40001,40004) g")
        mark = len(s1.errors) + len(s2.errors)
        _send_wait(r, s1, "BEGIN", "g_begin")
        _send_wait(r, s1, "DELETE FROM t7 WHERE id BETWEEN 2 AND 40000; SELECT count(*) FROM t7", "g_del")
        r["G s1 buckets"] = _buckets(dsn, "cc_s1")
        _send_wait(r, s2, "BEGIN; SET LOCAL lock_timeout = '2s'; DELETE FROM t7 WHERE id = 40001; "
                          "SELECT count(*) FROM t7", "g_del")
        r["G s2 returns"] = (f"{len(s1.errors) + len(s2.errors) - mark}/"
                             f"{_lks(dsn, 'cc_s2', 101)}/{_lks(dsn, 'cc_s2', 104)}")
        _send_wait(r, s1, "UPDATE t7 SET id = 0 WHERE id = 1; SELECT count(*) FROM t7", "g_upd")
        _send_wait(r, s2, "UPDATE t7 SET id = 40000 WHERE id = 40002; SELECT count(*) FROM t7", "g_upd")
        _send_wait(r, s1, "COMMIT", "g_commit")
        _send_wait(r, s2, "COMMIT", "g_commit")
        r["G errors"] = len(s1.errors) + len(s2.errors) - mark
        r["G ids"] = _ids(dsn, "t7")

        # H
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE TABLE t8 (id int) USING pgcolumnar")
            c.execute("INSERT INTO t8 SELECT g FROM generate_series(1,4) g")
            c.execute("INSERT INTO t8 SELECT g FROM generate_series(5,8) g")
        mark = len(s1.errors) + len(s2.errors)
        _send_wait(r, s1, "BEGIN; UPDATE t8 SET id = -6 WHERE id = 6; SELECT count(*) FROM t8", "h_upd")
        s2.send("UPDATE t8 SET id = id + 100 WHERE id IN (2, 6)", "h_upd2")
        _wait_blocked(r, dsn, "cc_s2")
        r["H s2 waits"] = _lks(dsn, "cc_s2", 101)
        _send_wait(r, s1, "COMMIT", "h_commit")
        _wait_label(r, s2, "h_upd2")
        serial = sum(1 for e in s2.errors if e == "40001: columnar: could not serialize access due to "
                                             "concurrent update or delete of the same row")
        r["H s2 fails"] = f"{len(s1.errors) + len(s2.errors) - mark}/{serial}"
        r["H ids"] = _ids(dsn, "t8")

        # I
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE TABLE t9 (id int) USING pgcolumnar")
        storage9 = "SELECT count(*) FROM pgcolumnar.storage WHERE relation_oid = 't9'::regclass"
        r["I no row"] = _ctl(dsn, storage9)
        mark = len(s1.errors) + len(s2.errors)
        _send_wait(r, s1, "BEGIN ISOLATION LEVEL REPEATABLE READ; SELECT count(*) FROM t9", "i_begin")
        _send_wait(r, s2, "BEGIN ISOLATION LEVEL REPEATABLE READ; SELECT count(*) FROM t9", "i_begin")
        _send_wait(r, s1, "INSERT INTO t9 VALUES (1); SELECT count(*) FROM t9", "i_ins")
        s2.send("INSERT INTO t9 VALUES (2); SELECT count(*) FROM t9", "i_ins2")
        _wait_blocked(r, dsn, "cc_s2")
        r["I s2 waits"] = _lks(dsn, "cc_s2", 102)
        _send_wait(r, s1, "COMMIT", "i_commit")
        _wait_label(r, s2, "i_ins2")
        _send_wait(r, s2, "COMMIT", "i_commit")
        r["I second commits"] = (f"{len(s1.errors) + len(s2.errors) - mark}/{_ids(dsn, 't9')}/"
                                 f"{_ctl(dsn, storage9)}")

        # J
        _disjoint(r, dsn)

        # K
        for vname, call in K_VERBS:
            t = f"tk_{vname}"
            call = call.replace("%T", t)
            with psycopg.connect(dsn, autocommit=True) as c:
                c.execute(f"CREATE TABLE {t} (id int) USING pgcolumnar")
                c.execute(f"SELECT pgcolumnar.set_options('{t}', stripe_row_limit => 1000)")
                c.execute(f"INSERT INTO {t} SELECT g FROM generate_series(1,3000) g")
                c.execute(f"DELETE FROM {t} WHERE id % 2 = 0")
            mark = len(s1.errors) + len(s2.errors)
            _send_wait(r, s1, f"BEGIN; INSERT INTO {t} VALUES (0); SELECT count(*) FROM {t}", f"k_ins_{vname}")
            _send_wait(r, s2, f"BEGIN; INSERT INTO {t} VALUES (-1); SELECT count(*) FROM {t}", f"k_ins_{vname}")
            s1.send(f"SELECT {call}", f"k_rw_{vname}")
            _wait_settled(r, dsn, s1, "cc_s1", f"k_rw_{vname}")
            refused = sum(1 for e in s1.errors
                          if e == f'55P03: cannot rewrite table "{t}" while another open transaction has written to it')
            r[f"K {vname} refused"] = f"{1 if s1.done(f'k_rw_{vname}') else 0}/{refused}"
            _send_wait(r, s1, "ROLLBACK", f"k_rb_{vname}")
            _send_wait(r, s2, f"SELECT {call}; COMMIT", f"k_rw_{vname}")
            r[f"K {vname} runs"] = (f"{len(s1.errors) + len(s2.errors) - mark}/"
                                    f"{_ctl(dsn, f'SELECT count(*) || chr(32) || count(*) FILTER (WHERE id IN (0,-1)) FROM {t}')}")

        # L
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE TABLE tl (id int) USING pgcolumnar")
            c.execute("INSERT INTO tl SELECT g FROM generate_series(1,4) g")
            c.execute("INSERT INTO tl SELECT g FROM generate_series(5,8) g")
        s3 = _Queue(dsn, "cc_s3")
        mark = len(s1.errors) + len(s2.errors)
        serial_before = sum(1 for e in s2.errors if e == SERIAL)
        _send_wait(r, s3, "BEGIN; DELETE FROM tl WHERE id = 6; SELECT count(*) FROM tl", "l_del")
        s1.send("BEGIN; UPDATE tl SET id = id + 100 WHERE id IN (2, 7)", "l_upd1")
        _wait_blocked(r, dsn, "cc_s1")
        r["L s1 waits"] = _lks(dsn, "cc_s1", 101)
        s2.send("BEGIN; UPDATE tl SET id = -2 WHERE id = 2", "l_upd2")
        _wait_blocked(r, dsn, "cc_s2")
        r["L s2 waits"] = _lks(dsn, "cc_s2", 101)
        _send_wait(r, s3, "COMMIT", "l_c3")
        _wait_label(r, s1, "l_upd1")
        _send_wait(r, s1, "COMMIT", "l_c1")
        _wait_label(r, s2, "l_upd2")
        _send_wait(r, s2, "COMMIT", "l_c2")
        r["L s2 fails"] = (f"{len(s1.errors) + len(s2.errors) - mark}/"
                           f"{sum(1 for e in s2.errors if e == SERIAL) - serial_before}")
        r["L ids"] = _ids(dsn, "tl")

        # M
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE TABLE tm (id int) USING pgcolumnar")
            c.execute("SELECT pgcolumnar.set_options('tm', chunk_group_row_limit => 1000, stripe_row_limit => 1000)")
            c.execute("INSERT INTO tm SELECT g FROM generate_series(1,3000) g")
            c.execute("CREATE INDEX tm_id ON tm (id)")
            c.execute("ANALYZE tm")
            c.execute(M_SET.replace("LOCAL ", ""))
            plan = [row[0] for row in c.execute(f"EXPLAIN (COSTS OFF) {M_DEL}").fetchall()]
        r["M plan"] = "|".join(" ".join(l.strip().removeprefix("->").strip().split(" ")[:2]) for l in plan)
        mark = len(s1.errors) + len(s2.errors)
        _send_wait(r, s2, "BEGIN; DELETE FROM tm WHERE id = 10; SELECT count(*) FROM tm", "m_del")
        s1.send(f"BEGIN; {M_SET} {M_DEL}", "m_del1")
        _wait_blocked(r, dsn, "cc_s1")
        r["M s1 waits"] = _lks(dsn, "cc_s1", 101)
        _send_wait(r, s2, "DELETE FROM tm WHERE id = 2520; SELECT count(*) FROM tm", "m_del2")
        _send_wait(r, s2, "COMMIT", "m_c2")
        _wait_label(r, s1, "m_del1")
        _send_wait(r, s1, "COMMIT", "m_c1")
        r["M both commit"] = (f"{len(s1.errors) + len(s2.errors) - mark}/"
                              f"{_ctl(dsn, 'SELECT count(*) FROM tm WHERE id IN (10, 20, 2510, 2520)')}")
    finally:
        s1.close()
        s2.close()
        if s3 is not None:
            s3.close()


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


def test_g_s1_holds_every_bucket(cc, expect):
    expect.text(str(cc["G s1 buckets"]), "1024/1024",
                "G s1's DELETE of 39999 rows takes every row-identity bucket, all free, and holds them")


def test_g_delete_in_other_group_returns(cc, expect):
    expect.text(str(cc["G s2 returns"]), "0/ExclusiveLock/<none>",
                "G s2's DELETE in another row group, with a 2 s lock_timeout, returns holding its group lock and no bucket")


def test_g_both_commit(cc, expect):
    expect.text(str(cc["G errors"]), "0", "G both deleting-then-updating transactions commit")


def test_g_both_took_effect(cc, expect):
    expect.text(str(cc["G ids"]), "0,40000,40003,40004", "G both DELETEs and both UPDATEs took effect")


def test_h_premise_s2_waits_on_group_lock(cc, expect):
    expect.text(str(cc["H s2 waits"]), "ExclusiveLock,ExclusiveLock(waiting)",
                "premise: H s2's UPDATE waits on a group lock while s1 is open")


def test_h_same_row_update_fails_40001(cc, expect):
    expect.text(str(cc["H s2 fails"]), "1/1",
                "H once s1 commits, s2's UPDATE of the same row fails with serialization_failure")


def test_h_one_version_of_the_row(cc, expect):
    expect.text(str(cc["H ids"]), "-6,1,2,3,4,5,7,8",
                "H the row s1 updated has one version, and s2 changed nothing")


def test_i_premise_no_storage_row(cc, expect):
    expect.text(str(cc["I no row"]), "0", "premise: I t9 has no storage row before its first write")


def test_i_premise_second_waits(cc, expect):
    expect.text(str(cc["I s2 waits"]), "ExclusiveLock(waiting)",
                "premise: I the second first writer waits on the storage-row lock")


def test_i_second_first_writer_commits_at_rr(cc, expect):
    expect.text(str(cc["I second commits"]), "0/1,2/1",
                "I a second first writer at REPEATABLE READ commits once the first has, and the table has one storage row")


def test_j_inserting_writers_do_not_fail(cc, expect):
    expect.text(str(cc["J ins failed"]), "0",
                "J six writers that each insert, delete and update their own rows, five rounds: no transaction fails")


def test_j_inserting_writers_rows(cc, expect):
    expect.text(str(cc["J ins rows"]), str(cc["J want"]), "J and their rows are what the rounds leave")


def test_j_existing_rows_writers_do_not_fail(cc, expect):
    expect.text(str(cc["J pre failed"]), "0",
                "J six writers that each delete and update their own existing rows, five rounds: no transaction fails (#2251)")


def test_j_existing_rows_writers_rows(cc, expect):
    expect.text(str(cc["J pre rows"]), str(cc["J want"]), "J and those rows are what the rounds leave")


@pytest.mark.parametrize("vname", ("recluster", "compact_rewrite"))
def test_k_rewrite_after_write_refuses_while_another_writer_is_open(cc, expect, vname):
    expect.text(str(cc[f"K {vname} refused"]), "1/1",
                f"K {vname} after s1's own INSERT, while s2's INSERT is open, refuses at once with 55P03")


@pytest.mark.parametrize("vname", ("recluster", "compact_rewrite"))
def test_k_rewrite_after_write_runs_alone(cc, expect, vname):
    expect.text(str(cc[f"K {vname} runs"]), "1/1501 1",
                f"K once s1 has rolled back, s2's {vname} after its own INSERT commits, and the rows are kept")


def test_l_premise_s1_waits_holding_first_group(cc, expect):
    expect.text(str(cc["L s1 waits"]), "ExclusiveLock,ExclusiveLock(waiting)",
                "premise: L s1's UPDATE holds its first group's lock and waits on the second's, which s3 holds")


def test_l_premise_s2_waits_on_group_lock(cc, expect):
    expect.text(str(cc["L s2 waits"]), "ExclusiveLock(waiting)",
                "premise: L s2's UPDATE of a row s1 is updating waits on that row's group lock")


def test_l_same_row_update_fails_40001(cc, expect):
    expect.text(str(cc["L s2 fails"]), "1/1",
                "L once s1 commits, s2's UPDATE of the same row fails with serialization_failure")


def test_l_one_version_of_the_row(cc, expect):
    expect.text(str(cc["L ids"]), "1,3,4,5,8,102,107", "L the row both updated has one version, s1's")


def test_m_premise_index_nested_loop(cc, expect):
    expect.text(str(cc["M plan"]), "Delete on|Nested Loop|Values Scan|Index Scan|Index Cond:",
                "premise: M s1's DELETE is an index nested loop driven by the VALUES list")


def test_m_waits_holding_no_group_lock(cc, expect):
    expect.text(str(cc["M s1 waits"]), "ExclusiveLock(waiting)",
                "M s1's DELETE, which visits the third group first, waits for the first group holding no group lock")


def test_m_both_commit(cc, expect):
    expect.text(str(cc["M both commit"]), "0/0", "M both DELETE transactions commit, and the four rows are gone")


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
