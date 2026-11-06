"""Two writers of the same columnar row serialize (issue #5, the UPDATE half, #1341).
The pytest twin of `test/update_conc.sh`, every check by the shell's name.

A columnar UPDATE marks the old row deleted and appends a new one. With nothing
serializing the row identity, two transactions updating one row each kept their own
new version: the row was duplicated and one update lost. columnar_row_lock.c takes a
transaction-scoped advisory lock per row identity at the delete-vector flush, then
re-reads the latest committed state and raises a retryable serialization_failure
(40001) when a concurrent transaction committed the row as deleted. A table with a
unique index takes the lock during the UPDATE instead, so the unique check cannot
answer 23505 first (#1421).

Each concurrent scenario runs the same interleaving on a heap table, the oracle, and
on a columnar one. s1 updates and holds its transaction open; s2's statement runs in
a thread and must wait on a lock, which a poll of pg_stat_activity confirms, never a
sleep; then s1 commits. At READ COMMITTED a heap re-applies s2's update to the new
version, where columnar gives s2 a 40001, so at RC the columnar VALUE is not compared
with the heap's.

A PRIVATE DATABASE on the shared cluster: the waits are read from pg_stat_activity
for this database only. Each session runs at a 30s lock_timeout, as the shell's
cluster does, so a real hang ends in an error rather than a pass.
"""
import threading
import time

import psycopg
import pytest

WAIT = 60  # seconds a bounded wait allows, as the shell's 1200 polls of 0.05s
LOCK_TIMEOUT = "30s"


@pytest.fixture(scope="module")
def upconc(pgc_cluster):
    """-> the dsn of a private database holding the four tables the scenarios use."""
    name = "pgc_upconc"

    def admin(sql):
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as c:
            c.execute(sql)

    admin(f"DROP DATABASE IF EXISTS {name}")
    admin(f"CREATE DATABASE {name}")
    dsn = pgc_cluster.dsn(dbname=name)
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION pgcolumnar")
            c.execute("CREATE TABLE t_heap (id int, v int)")
            c.execute("CREATE TABLE t_col (id int, v int) USING pgcolumnar")
            c.execute("CREATE TABLE t_heapu (id int, v int)")
            c.execute("CREATE UNIQUE INDEX t_heapu_uidx ON t_heapu (id)")
            c.execute("CREATE TABLE t_colu (id int, v int) USING pgcolumnar")
            c.execute("CREATE UNIQUE INDEX t_colu_uidx ON t_colu (id)")
        yield dsn
    finally:
        admin(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


class _Session:
    """One connection, whose statements run in a thread so the test can watch it wait."""

    def __init__(self, dsn, app):
        self.conn = psycopg.connect(dsn, autocommit=True, application_name=app,
                                    options=f"-c lock_timeout={LOCK_TIMEOUT}")
        self.thread = None
        self.error = None

    def start(self, sql):
        self.error = None

        def go():
            try:
                self.conn.execute(sql)
            except psycopg.Error as exc:
                self.error = exc
        self.thread = threading.Thread(target=go, daemon=True)
        self.thread.start()

    def finished(self):
        self.thread.join(WAIT)
        return not self.thread.is_alive()


def _code(err):
    """The SQLSTATE a statement ended with: '00000' when it succeeded, as psql's
    :SQLSTATE reads."""
    return "00000" if err is None else err.sqlstate


class _Race:
    """s1, s2 and a watcher, and the waits the shell bounds. Each wait that runs out
    records its failure under the shell's fixed name for that kind."""

    def __init__(self, dsn, expect):
        self.expect = expect
        self.watch = psycopg.connect(dsn, autocommit=True)
        self.s1 = _Session(dsn, "cc_s1")
        self.s2 = _Session(dsn, "cc_s2")

    def close(self):
        for c in (self.s1.conn, self.s2.conn, self.watch):
            c.close()

    def one(self, sql):
        return self.watch.execute(sql).fetchone()[0]

    def run(self, sess, sql):
        """sql on sess, which must complete within the bound. -> its error, or None."""
        expect = self.expect
        sess.start(sql)
        if not sess.finished():
            expect.text(f"timed out on {sql}", "completed", "a bounded wait for a command's sentinel completed")
        return sess.error

    def finish(self, sess):
        """Wait for sess's statement, already running. -> its error, or None."""
        expect = self.expect
        if not sess.finished():
            expect.text("timed out", "completed", "a bounded wait for a standalone sentinel completed")
        return sess.error

    def wait_blocked(self, app):
        expect = self.expect
        deadline = time.monotonic() + WAIT
        while self.one(f"SELECT count(*) FROM pg_stat_activity WHERE application_name = '{app}' "
                       "AND datname = current_database() AND wait_event_type = 'Lock'") != 1:
            if time.monotonic() > deadline:
                expect.text(f"{app} never waited", "completed", "a bounded wait for a session to block completed")
                return
            time.sleep(0.05)

    def seed(self, table, rows=1):
        self.watch.execute(f"TRUNCATE {table}")
        self.watch.execute(f"INSERT INTO {table} SELECT g, 0 FROM generate_series(1, {rows}) g")

    def count(self, table):
        return self.one(f"SELECT count(*) FROM {table} WHERE id = 1")

    def values(self, table):
        return self.one(f"SELECT coalesce(string_agg(v::text, ',' ORDER BY v), '') FROM {table} WHERE id = 1")

    def overlap(self, iso, first, second):
        """s1 runs `first` and holds its transaction; s2's `second` must wait on it;
        s1 commits, then s2 finishes and commits. -> s2's SQLSTATE."""
        self.run(self.s1, f"BEGIN ISOLATION LEVEL {iso}")
        self.run(self.s2, f"BEGIN ISOLATION LEVEL {iso}")
        self.run(self.s1, first)
        self.s2.start(second)
        self.wait_blocked("cc_s2")
        self.run(self.s1, "COMMIT")
        code = _code(self.finish(self.s2))
        self.run(self.s2, "COMMIT")
        return code

    def uu(self, table, iso):
        """update-vs-update: s1 sets v=10, s2 sets v=20, on id=1. Distinct constants,
        never v+1, so the duplicated and the composed states cannot coincide.
        -> (rows with id=1, their values, s2's SQLSTATE)."""
        self.seed(table)
        code = self.overlap(iso, f"UPDATE {table} SET v = 10 WHERE id = 1",
                            f"UPDATE {table} SET v = 20 WHERE id = 1")
        return self.count(table), self.values(table), code


@pytest.fixture
def race(upconc, expect):
    r = _Race(upconc, expect)
    try:
        yield r
    finally:
        r.close()


def test_update_vs_update_read_committed(race, expect):
    h_cnt, _, _ = race.uu("t_heap", "READ COMMITTED")
    c_cnt, _, c_ss = race.uu("t_col", "READ COMMITTED")
    expect.num(h_cnt, 1, "uu/RC heap oracle composes both updates (one row)")
    expect.num(c_cnt, 1, "uu/RC columnar keeps a single row (no duplicate)")
    expect.text(c_ss, "40001", "uu/RC columnar loser gets serialization_failure")
    # The value is not compared with the heap's: columnar's loser retries (it ends
    # {10}) where a heap re-applies transparently (it ends {20}).


def test_update_vs_update_repeatable_read(race, expect):
    h_cnt, h_vals, h_ss = race.uu("t_heap", "REPEATABLE READ")
    c_cnt, c_vals, c_ss = race.uu("t_col", "REPEATABLE READ")
    expect.text(h_ss, "40001", "uu/RR heap oracle: loser serialization_failure")
    expect.num(c_cnt, h_cnt, "uu/RR columnar count matches heap oracle")
    expect.text(c_vals, h_vals, "uu/RR columnar value-set matches heap oracle")
    expect.text(c_ss, "40001", "uu/RR columnar loser gets serialization_failure")


def test_update_vs_update_serializable(race, expect):
    """Safe before the row lock existed too: the delete-vector conflict, or SSI,
    already failed the loser at this level. A regression guard."""
    h_cnt, _, _ = race.uu("t_heap", "SERIALIZABLE")
    c_cnt, _, c_ss = race.uu("t_col", "SERIALIZABLE")
    expect.num(c_cnt, h_cnt, "uu/SER columnar count matches heap oracle")
    expect.text(c_ss, "40001", "uu/SER columnar loser gets serialization_failure")


def test_update_vs_delete_read_committed(race, expect):
    """At RC the count is 1 whether the DELETE was lost or the loser aborted, so the
    SQLSTATE is what tells them apart."""
    def ud(table):
        race.seed(table)
        code = race.overlap("READ COMMITTED", f"UPDATE {table} SET v = 10 WHERE id = 1",
                            f"DELETE FROM {table} WHERE id = 1")
        return race.count(table), code
    h_cnt, _ = ud("t_heap")
    _, c_ss = ud("t_col")
    expect.num(h_cnt, 0, "ud/RC heap oracle: delete applies to the live row (count 0)")
    expect.text(c_ss, "40001", "ud/RC columnar loser gets serialization_failure")


def test_delete_vs_update_read_committed(race, expect):
    """Without the lock, s2 wrote a successor to a row s1 had deleted and committed."""
    def du(table):
        race.seed(table)
        code = race.overlap("READ COMMITTED", f"DELETE FROM {table} WHERE id = 1",
                            f"UPDATE {table} SET v = 20 WHERE id = 1")
        return race.count(table), code
    h_cnt, _ = du("t_heap")
    c_cnt, c_ss = du("t_col")
    expect.num(h_cnt, 0, "du/RC heap oracle: committed delete stands (count 0)")
    expect.num(c_cnt, h_cnt, "du/RC columnar count matches heap oracle")
    expect.text(c_ss, "40001", "du/RC columnar loser gets serialization_failure")


def test_a_row_updated_twice_by_one_statement(race, expect):
    """UPDATE ... FROM (VALUES (1), (1)) matches id=1 twice. A heap raises "tuple to be
    updated was already modified by an operation triggered by the current command"
    and keeps one row; columnar reports TM_SelfModified, so core does the same."""
    def self_update(table):
        race.seed(table)
        try:
            race.watch.execute(f"UPDATE {table} t SET v = v + 1 FROM (VALUES (1), (1)) s(id) WHERE t.id = s.id")
        except psycopg.Error:
            pass
        return race.count(table)
    expect.num(self_update("t_heap"), 1, "self-update heap oracle keeps one row")
    expect.num(self_update("t_col"), 1, "self-update columnar keeps one row (no self-duplicate)")


def test_overlapping_multi_row_updates(race, expect):
    """s1 updates ids 1..120, s2 ids 80..200: 41 rows overlap."""
    def multirow(table):
        race.seed(table, 200)
        code = race.overlap("READ COMMITTED", f"UPDATE {table} SET v = v + 1 WHERE id BETWEEN 1 AND 120",
                            f"UPDATE {table} SET v = v + 1 WHERE id BETWEEN 80 AND 200")
        return race.one(f"SELECT count(*) FROM {table}"), code
    h_tot, _ = multirow("t_heap")
    c_tot, c_ss = multirow("t_col")
    expect.num(h_tot, 200, "multirow heap oracle total unchanged (200)")
    expect.num(c_tot, h_tot, "multirow columnar total matches heap (no per-row duplicates)")
    expect.text(c_ss, "40001", "multirow columnar loser gets serialization_failure")


def test_serial_updates_control(race, expect):
    """The same two updates, s1 committed before s2 begins: no overlap, so a red above
    is the overlap's and not a broken update."""
    race.seed("t_col")
    for sess, v in ((race.s1, 10), (race.s2, 20)):
        race.run(sess, "BEGIN")
        race.run(sess, f"UPDATE t_col SET v = {v} WHERE id = 1")
        race.run(sess, "COMMIT")
    expect.num(race.count("t_col"), 1,
               "CONTROL serial columnar keeps one row (GREEN today; proves the RED is the overlap)")
    expect.text(race.values("t_col"), "20", "CONTROL serial columnar keeps the last writer's value")


def test_a_covering_unique_index(race, expect):
    """The loser gets 40001 here too, as docs/limitations.md promises. It used to get
    23505: its new version's unique-key lock waited on the winner's identical key, and
    the unique check fired before the flush that raises 40001 (#1421)."""
    _, _, h_ss = race.uu("t_heapu", "READ COMMITTED")
    c_cnt, _, c_ss = race.uu("t_colu", "READ COMMITTED")
    expect.text(h_ss, "00000", "unique/RC heap composes with no error (sqlstate 00000)")
    expect.text(f"{c_ss}|{c_cnt}", "40001|1",
                "unique/RC columnar loser gets serialization_failure (40001), as without a unique index")


def _id1(race, table):
    return race.one(f"SELECT count(*) || '/' || coalesce(string_agg(v::text, ',' ORDER BY v), '') "
                    f"FROM {table} WHERE id = 1")


def test_a_savepoint_rolled_back(race, expect):
    """The inner UPDATE completes, so its marks and its new row are flushed under the
    inner subtransaction, and its abort hides them."""
    def sp(table):
        race.seed(table)
        c = race.watch
        c.execute("BEGIN")
        c.execute(f"UPDATE {table} SET v = 10 WHERE id = 1")
        c.execute("SAVEPOINT sp")
        c.execute(f"UPDATE {table} SET v = 99 WHERE id = 1")
        c.execute("ROLLBACK TO sp")
        c.execute("COMMIT")
        return _id1(race, table)
    h_sp = sp("t_heap")
    c_sp = sp("t_col")
    expect.text(c_sp, h_sp, "savepoint columnar final state matches heap oracle")
    expect.text(c_sp, "1/10", "savepoint columnar kept the pre-savepoint value, no duplicate (1/10)")


def test_a_statement_failing_inside_a_savepoint(race, expect):
    """The inner UPDATE changes id=1 and then fails on id=3, so no executor end runs
    and its delete mark and new row are still buffered at ROLLBACK TO. Only the
    subtransaction discard keeps them from being written at COMMIT; the arm above
    cannot reach it (#1341). The premise is the scan order: id=1 is read, and
    updated, before id=3 fails the statement."""
    def sp_fail(table):
        race.seed(table)
        c = race.watch
        c.execute("BEGIN")
        c.execute(f"UPDATE {table} SET v = 10 WHERE id = 1")
        c.execute(f"INSERT INTO {table} VALUES (3, 0)")
        order = c.execute(f"SELECT string_agg(id::text, ',') FROM {table}").fetchone()[0]
        c.execute("SAVEPOINT sp")
        try:
            c.execute(f"UPDATE {table} SET v = CASE WHEN id = 3 THEN 1 / (id - 3) ELSE 99 END")
        except psycopg.errors.DivisionByZero:
            pass
        c.execute("ROLLBACK TO sp")
        c.execute("COMMIT")
        return order, f"{_id1(race, table)}|{race.one(f'SELECT count(*) FROM {table}')}"
    _, h_after = sp_fail("t_heap")
    c_order, c_after = sp_fail("t_col")
    expect.text(c_order, "1,3", "premise: the failing statement reads id=1 before id=3 on the columnar table")
    expect.text(c_after, h_after, "savepoint, failed statement: columnar final state matches heap oracle")
    expect.text(c_after, "1/10|2",
                "savepoint, failed statement: columnar kept the pre-savepoint value, no duplicate (1/10, 2 rows)")
