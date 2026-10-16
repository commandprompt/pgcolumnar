"""Concurrent inserters of one unique key serialize on a columnar table (issue #5,
#1341). The pytest twin of `test/unique_conc.sh`, every check by the shell's name.

A columnar row's data is invisible to other backends until its stripe is flushed at
statement end, but its btree entry is written at once. So while T1's INSERT is still
running, T2 inserting the same key finds T1's index entry and cannot resolve the row,
and the btree check would let a duplicate through. The table AM takes a
transaction-scoped advisory lock per unique key (columnar_unique.c) before the btree
check runs: T2 waits for T1 to commit, then gets 23505.

HOLDING T1 MID-STATEMENT. T1 runs a two-row INSERT whose second row calls `cq_block`,
which waits on an advisory lock a third session (the holder) has taken. The first
row, the real key, is inserted and indexed, and the statement has not ended, so it is
not flushed. T2 then inserts while T1 waits there. Every barrier is a poll of
pg_stat_activity for a real lock wait, never a sleep.

A PRIVATE CLUSTER, because `pgcolumnar.unique_lock_buckets` is fixed at server start.
It runs at 100003 buckets, as the shell's does, so two unrelated keys do not share a
bucket and a serialization observed here is the guarantee, not a collision.
"""
import pathlib
import shutil
import tempfile
import threading
import time

import psycopg
import pytest

BUCKETS = "100003"
WAIT = 60  # seconds a bounded wait allows, as the shell's 1200 polls of 0.05s


@pytest.fixture(scope="module")
def uc(pgc_cluster):
    """-> the dsn of a private cluster at BUCKETS buckets, with cq_block defined."""
    from pgc_cluster import Cluster, pick_port

    work = pathlib.Path(tempfile.mkdtemp(prefix="pgc-uconc-"))
    work.chmod(0o777)
    datadir = work / "data"
    datadir.mkdir()
    cluster = Cluster(pgc_cluster.pg_config, "uconc", datadir, pick_port(15))
    try:
        cluster.initdb()
        with open(datadir / "postgresql.conf", "a") as fh:
            # lock_timeout caps a real hang, so a bug cannot pass by blocking forever.
            fh.write(f"\nlock_timeout=60000\npgcolumnar.unique_lock_buckets={BUCKETS}\n")
        cluster.start()
        dsn = cluster.dsn()
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            # plpgsql so it is never inlined; anyelement so one function serves every key type.
            c.execute("CREATE FUNCTION cq_block(k bigint, v anyelement) RETURNS anyelement LANGUAGE plpgsql "
                      "VOLATILE AS $$ BEGIN PERFORM pg_advisory_xact_lock(k); RETURN v; END $$")
        yield dsn
    finally:
        cluster.stop()
        shutil.rmtree(work, ignore_errors=True)


class _Session:
    """One connection, whose statements run in a thread so the test can watch it wait."""

    def __init__(self, dsn, app):
        self.conn = psycopg.connect(dsn, autocommit=True, application_name=app)
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


class _Race:
    """The three sessions of one scenario, and the waits the shell bounds. Each wait
    that runs out records its failure under the shell's fixed name for that kind."""

    def __init__(self, dsn, expect):
        self.expect = expect
        self.watch = psycopg.connect(dsn, autocommit=True)
        self.s1 = _Session(dsn, "cc_s1")
        self.s2 = _Session(dsn, "cc_s2")
        self.sh = _Session(dsn, "cc_sh")

    def close(self):
        for c in (self.sh.conn, self.s2.conn, self.s1.conn, self.watch):
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

    def waiting(self, app):
        return self.one(f"SELECT count(*) FROM pg_stat_activity WHERE application_name = '{app}' "
                        "AND wait_event_type = 'Lock'")

    def wait_blocked(self, app, asserted=False):
        """Wait for app to wait on a lock. `asserted`: the caller's next check counts
        the waiter itself, so running out returns and is that check's failure, after
        10s, rather than a second failure under the wait's own name, as the shell does."""
        expect = self.expect
        deadline = time.monotonic() + (10 if asserted else WAIT)
        while self.waiting(app) != 1:
            if time.monotonic() > deadline:
                if asserted:
                    return
                expect.text(f"{app} never waited", "completed", "a bounded wait for a session to block completed")
            time.sleep(0.05)

    def wait_idle_intx(self, app):
        expect = self.expect
        deadline = time.monotonic() + WAIT
        while self.one(f"SELECT state FROM pg_stat_activity WHERE application_name = '{app}'") \
                != "idle in transaction":
            if time.monotonic() > deadline:
                expect.text(f"{app} never went idle in transaction", "completed",
                            "a bounded wait for a session to reach idle-in-transaction completed")
            time.sleep(0.05)

    def pause_s1(self, hkey, insert):
        """T1 left waiting mid-INSERT: its real-key row buffered and indexed, unflushed."""
        self.run(self.sh, f"SELECT pg_advisory_lock({hkey})")
        self.run(self.s1, "BEGIN")
        self.s1.start(insert)
        self.wait_blocked("cc_s1")

    def release_s1(self, hkey):
        """The pause released: T1's INSERT finishes, which flushes it."""
        self.run(self.sh, f"SELECT pg_advisory_unlock({hkey})")
        return self.finish(self.s1)

    def conflict(self, hkey, table, first, second):
        """T1 holds `first` unflushed; T2 inserts `second` and should wait for it.
        -> how many sessions wait, read while T1 still holds the key. The test
        asserts it before `settle` releases T1, so a T2 that did not wait is that
        check's red, not a later wait's (#1341: with no key lock, T1 then stalled
        behind T2 and the first red was a bounded wait)."""
        self.pause_s1(hkey, f"INSERT INTO {table} {first}")
        self.run(self.s2, "BEGIN")
        self.s2.start(f"INSERT INTO {table} {second}")
        self.wait_blocked("cc_s2", asserted=True)
        return self.waiting("cc_s2")

    def settle(self, hkey, index):
        """After `conflict`: T1 finishes and commits, and T2 should get 23505.
        -> _violated's answer."""
        self.release_s1(hkey)
        self.run(self.s1, "COMMIT")
        violated = _violated(self.finish(self.s2), index)
        self.run(self.s2, "ROLLBACK")
        return violated

    def no_conflict(self, hkey, table, first, second):
        """T1 holds `first` unflushed; T2's `second` must not wait for it.
        -> '<sessions waiting once T2's INSERT returned>|returned', or '|refused
        <sqlstate>' when the INSERT was refused. T2 runs with a 5s lock_timeout, so
        an INSERT that waits is refused well inside the 60s bound. Under the server's
        60s lock_timeout the two ran out together, T2 had stopped waiting by the time
        the count was read, and these arms could not fail (#1341)."""
        self.pause_s1(hkey, f"INSERT INTO {table} {first}")
        self.run(self.s2, "BEGIN")
        self.run(self.s2, "SET LOCAL lock_timeout = '5s'")
        err = self.run(self.s2, f"INSERT INTO {table} {second}")
        if err is None:
            self.wait_idle_intx("cc_s2")
        waiting = f"{self.waiting('cc_s2')}|{'returned' if err is None else f'refused {err.sqlstate}'}"
        self.run(self.s2, "COMMIT")
        self.release_s1(hkey)
        self.run(self.s1, "COMMIT")
        return waiting

    def duplicate_with_the_lock_off(self, hkey, table, first, second):
        """T1 holds `first` unflushed; T2, with the lock off for itself, inserts `second`
        and commits without waiting. The bug the lock fixes."""
        self.pause_s1(hkey, f"INSERT INTO {table} {first}")
        self.run(self.s2, "BEGIN")
        self.run(self.s2, "SET LOCAL pgcolumnar.enable_unique_insert_lock = off")
        self.run(self.s2, "SET LOCAL lock_timeout = '5s'")
        self.run(self.s2, f"INSERT INTO {table} {second}")
        self.run(self.s2, "COMMIT")
        self.release_s1(hkey)
        self.run(self.s1, "COMMIT")


def _violated(err, index):
    """'yes' when err is a 23505 naming index, else what arrived."""
    if err is not None and err.sqlstate == "23505" and f'"{index}"' in str(err):
        return "yes"
    return f"no ({err.sqlstate}: {str(err).splitlines()[0]})" if err is not None else "no (no error)"


def _paused(key_sql, block_sql, rest=""):
    """The two-row SELECT whose second row waits in cq_block: `key_sql` is the real key."""
    return f"SELECT CASE WHEN g = 2 THEN {block_sql} ELSE {key_sql} END{rest} FROM generate_series(1, 2) g"


@pytest.fixture
def race(uc, expect):
    r = _Race(uc, expect)
    try:
        yield r
    finally:
        r.close()


def test_the_bucket_count_is_fixed_at_server_start(race, expect):
    # 55P02 cant_change_runtime_param: a PGC_POSTMASTER setting, refused in a session
    with pytest.raises(psycopg.errors.CantChangeRuntimeParam) as exc:
        race.s2.conn.execute("SET pgcolumnar.unique_lock_buckets = 1")
    expect.sqlstate(exc.value, "55P02", "the bucket count cannot be changed per session")
    expect.text(race.one("SHOW pgcolumnar.unique_lock_buckets"), BUCKETS,
                "the cluster runs the bucket count the suite needs")


def test_one_key_serializes_and_the_lock_is_what_does_it(race, expect):
    race.run(race.s1, "CREATE TABLE s_int (k int) USING pgcolumnar")
    race.run(race.s1, "CREATE UNIQUE INDEX s_int_uidx ON s_int (k)")
    race.duplicate_with_the_lock_off(1001, "s_int", _paused("1", "cq_block(1001, -1)"), "VALUES (1)")
    expect.num(race.one("SELECT count(*) FROM s_int WHERE k = 1"), 2,
               "1a pre-fix: lock disabled lets both same-key inserts commit (bug)")

    race.run(race.s1, "TRUNCATE s_int")
    expect.num(race.conflict(1002, "s_int", _paused("1", "cq_block(1002, -1)"), "VALUES (1)"), 1,
               "1b post-fix: same-key second inserter blocks")
    violated = race.settle(1002, "s_int_uidx")
    expect.text(violated, "yes", "1b post-fix: blocked inserter got a unique_violation")
    expect.num(race.one("SELECT count(*) FROM s_int WHERE k = 1"), 1, "1b post-fix: exactly one row with key 1")


def test_keys_equal_under_the_opclass_serialize(race, expect):
    """numeric 1.0 and 1.00 are equal and differ in bytes; a raw-byte hash would split them."""
    race.run(race.s1, "CREATE TABLE s_num (v numeric) USING pgcolumnar")
    race.run(race.s1, "CREATE UNIQUE INDEX s_num_uidx ON s_num (v)")
    race.duplicate_with_the_lock_off(1003, "s_num", _paused("1.0", "cq_block(1003, (-1)::numeric)"),
                                     "VALUES (1.00)")
    expect.num(race.one("SELECT count(*) FROM s_num WHERE v = 1.0"), 2,
               "2a pre-fix: numeric 1.0 and 1.00 both commit with lock off (bug)")

    race.run(race.s1, "TRUNCATE s_num")
    expect.num(race.conflict(1004, "s_num", _paused("1.0", "cq_block(1004, (-1)::numeric)"), "VALUES (1.00)"), 1,
               "2b post-fix: numeric 1.00 serializes behind 1.0 (opclass-safe hash)")
    violated = race.settle(1004, "s_num_uidx")
    expect.text(violated, "yes", "2b post-fix: numeric equal-but-different-bytes got unique_violation")
    expect.num(race.one("SELECT count(*) FROM s_num WHERE v = 1.0"), 1, "2b post-fix: exactly one row equal to 1.0")


def test_citext_keys_equal_by_case_serialize(race, expect):
    try:
        race.watch.execute("CREATE EXTENSION IF NOT EXISTS citext")
    except psycopg.Error as exc:
        expect.cannot_run("MISSING_DEPENDENCY", f"citext extension not available: {exc}",
                          name="the 2c citext case")
        return
    race.run(race.s1, "CREATE TABLE s_ci (v citext) USING pgcolumnar")
    race.run(race.s1, "CREATE UNIQUE INDEX s_ci_uidx ON s_ci (v)")
    expect.num(race.conflict(1005, "s_ci", _paused("'ABC'::citext", "cq_block(1005, 'zzz'::citext)"), "VALUES ('abc')"), 1,
               "2c post-fix: citext 'abc' serializes behind 'ABC' (case-folded hash)")
    violated = race.settle(1005, "s_ci_uidx")
    expect.text(violated, "yes", "2c post-fix: citext case-insensitive duplicate got unique_violation")
    expect.num(race.one("SELECT count(*) FROM s_ci WHERE v = 'abc'"), 1, "2c post-fix: exactly one row equal to 'abc'")


def test_different_keys_do_not_block_each_other(race, expect):
    race.run(race.s1, "CREATE TABLE s_diff (k int) USING pgcolumnar")
    race.run(race.s1, "CREATE UNIQUE INDEX s_diff_uidx ON s_diff (k)")
    expect.text(race.no_conflict(1006, "s_diff", _paused("10", "cq_block(1006, -1)"), "VALUES (20)"), "0|returned",
               "3 different keys do not block each other")
    expect.text(race.one("SELECT string_agg(k::text, ',' ORDER BY k) FROM s_diff WHERE k IN (10, 20)"), "10,20",
                "3 both distinct keys present")


def test_a_multi_column_key(race, expect):
    race.run(race.s1, "CREATE TABLE s_mc (a int, b int) USING pgcolumnar")
    race.run(race.s1, "CREATE UNIQUE INDEX s_mc_uidx ON s_mc (a, b)")
    expect.num(race.conflict(1007, "s_mc", _paused("1", "cq_block(1007, -1)", ", 2"), "VALUES (1, 2)"), 1,
               "4 multi-column equal key serializes")
    violated = race.settle(1007, "s_mc_uidx")
    expect.text(violated, "yes", "4 multi-column duplicate got unique_violation")
    expect.num(race.one("SELECT count(*) FROM s_mc WHERE a = 1 AND b = 2"), 1, "4 exactly one (1,2) row")
    # (5,7) differs from (5,6) only in its second column
    expect.text(race.no_conflict(1008, "s_mc", _paused("5", "cq_block(1008, -1)", ", 6"), "VALUES (5, 7)"), "0|returned",
               "4 multi-column differing key does not block")


def test_a_partial_unique_index(race, expect):
    race.run(race.s1, "CREATE TABLE s_part (k int, active boolean) USING pgcolumnar")
    race.run(race.s1, "CREATE UNIQUE INDEX s_part_uidx ON s_part (k) WHERE active")
    expect.num(race.conflict(1009, "s_part", _paused("1", "cq_block(1009, -1)", ", true"), "VALUES (1, true)"), 1,
               "5 partial index, row inside predicate serializes")
    violated = race.settle(1009, "s_part_uidx")
    expect.text(violated, "yes", "5 partial index in-predicate duplicate got unique_violation")
    expect.num(race.one("SELECT count(*) FROM s_part WHERE k = 1 AND active"), 1, "5 exactly one active row with k=1")
    # Outside the predicate the index covers neither row, so nothing serializes.
    expect.text(race.no_conflict(1010, "s_part", _paused("2", "cq_block(1010, -1)", ", false"), "VALUES (2, false)"),
               "0|returned", "5 partial index, rows outside predicate do not serialize")
    expect.num(race.one("SELECT count(*) FROM s_part WHERE k = 2 AND NOT active"), 2,
               "5 both inactive k=2 rows kept (predicate not enforced)")


def test_null_keys(race, expect):
    race.run(race.s1, "CREATE TABLE s_nd (k int) USING pgcolumnar")
    race.run(race.s1, "CREATE UNIQUE INDEX s_nd_uidx ON s_nd (k)")
    expect.text(race.no_conflict(1011, "s_nd", _paused("NULL", "cq_block(1011, -1)"), "VALUES (NULL)"), "0|returned",
               "6 NULLS DISTINCT: two NULL keys do not block")
    expect.num(race.one("SELECT count(*) FROM s_nd WHERE k IS NULL"), 2, "6 NULLS DISTINCT: both NULL rows kept")

    # NULLS NOT DISTINCT is PG15's, the oldest major this tree supports.
    race.run(race.s1, "CREATE TABLE s_nn (k int) USING pgcolumnar")
    race.run(race.s1, "CREATE UNIQUE INDEX s_nn_uidx ON s_nn (k) NULLS NOT DISTINCT")
    expect.num(race.conflict(1012, "s_nn", _paused("NULL", "cq_block(1012, -1)"), "VALUES (NULL)"), 1,
               "6 NULLS NOT DISTINCT: second NULL key serializes")
    violated = race.settle(1012, "s_nn_uidx")
    expect.text(violated, "yes", "6 NULLS NOT DISTINCT: second NULL got unique_violation")
    expect.num(race.one("SELECT count(*) FROM s_nn WHERE k IS NULL"), 1, "6 NULLS NOT DISTINCT: exactly one NULL row")


def test_a_duplicate_within_one_statement(race, expect):
    race.run(race.s1, "CREATE TABLE s_ss (k int) USING pgcolumnar")
    race.run(race.s1, "CREATE UNIQUE INDEX s_ss_uidx ON s_ss (k)")
    expect.text(_violated(race.run(race.s2, "INSERT INTO s_ss SELECT 7 FROM generate_series(1, 2)"), "s_ss_uidx"),
                "yes", "7 same-statement duplicate raises unique_violation")
    expect.num(race.one("SELECT count(*) FROM s_ss"), 0, "7 same-statement duplicate inserted no rows")


def test_an_aborted_first_inserter(race, expect):
    """T1's INSERT has completed and flushed, uncommitted, and holds the key lock."""
    race.run(race.s1, "CREATE TABLE s_ab (k int) USING pgcolumnar")
    race.run(race.s1, "CREATE UNIQUE INDEX s_ab_uidx ON s_ab (k)")
    race.run(race.s1, "BEGIN")
    race.run(race.s1, "INSERT INTO s_ab VALUES (1)")
    race.run(race.s2, "BEGIN")
    race.s2.start("INSERT INTO s_ab VALUES (1)")
    race.wait_blocked("cc_s2", asserted=True)
    expect.num(race.waiting("cc_s2"), 1, "8 second inserter blocks behind uncommitted first")
    race.run(race.s1, "ROLLBACK")
    err = race.finish(race.s2)
    race.run(race.s2, "COMMIT")
    expect.text(f"{race.one('SELECT count(*) FROM s_ab WHERE k = 1')}|{err.sqlstate if err else 'no error'}",
                "1|no error", "8 after first aborts, second insert succeeds; one row")
