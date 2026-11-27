"""The maintenance daemon runs each verb in a transaction of its own (#2194). The pytest
twin of `test/autovacuum_verb_txn.sh`, every check by the shell's name (#1341).

A verb keeps its locks to commit: the storage-row lock (class 102) and the locks of the row
groups it rewrote (class 101). When both verbs were due, the daemon ran compact_rewrite and
then recluster in one transaction, so the recluster waited for a busy group while it still
held the storage-row lock the compact_rewrite kept. A writer that held that group's lock
and waited for the storage-row lock closed the #2074 cycle: 40P01.

Deadlock: a writer deletes id 2501 and flushes it; the daemon's compact_rewrite pauses on
its first row (an expression index sleeps 3 s, once per daemon process) holding 102; the
writer inserts, so it waits for 102. Its deadlock_timeout is 6 s and the daemon's 20 s, so a
recluster that waits on the writer's group while it holds 102 is found by the writer's own
check. Limit: the same with pgcolumnar.rewrite_lock_timeout 1.5 s, under the
compact_rewrite's 3 s; a recluster charged for that time, or one that waits on the writer
while it holds the compact_rewrite's lock, stops with 55P03 and the daemon logs a skip.
Yield: a LOCK TABLE with a 1 s deadlock_timeout cancels the paused compact_rewrite, and the
daemon's next verb on the table must be the next pass's compact_rewrite, not a recluster.

ON A PRIVATE DATABASE, with the daemon's settings by ALTER SYSTEM and a reload, reset at
teardown, since this corpus shares one cluster per worker.
"""
import re
import time

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_autovacuum_verb_txn"
TOUCHED = ("pgcolumnar.autovacuum", "pgcolumnar.autovacuum_naptime",
           "pgcolumnar.autovacuum_compact_threshold", "pgcolumnar.autovacuum_recluster_threshold",
           "pgcolumnar.rewrite_lock_timeout", "deadlock_timeout")
AV_PAUSED = "a.backend_type = 'pgcolumnar autovacuum worker' AND a.wait_event = 'PgSleep'"
AV_PAUSED_102 = ("EXISTS (SELECT 1 FROM pg_stat_activity a JOIN pg_locks l ON l.pid = a.pid "
                 f"WHERE {AV_PAUSED} AND l.locktype = 'advisory' AND l.objsubid = 102 AND l.granted)")
MAKE = (
    "DROP TABLE IF EXISTS dl",
    "CREATE TABLE dl (id int, bal int) USING pgcolumnar",
    "SELECT pgcolumnar.set_options('dl', stripe_row_limit => 1000, chunk_group_row_limit => 100)",
    "INSERT INTO dl SELECT g, 1000 FROM generate_series(1, 3000) g",
    "SELECT pgcolumnar.recluster('dl', 'id')",
    "INSERT INTO dl SELECT g, 1000 FROM generate_series(3001, 3300) g",
    "DELETE FROM dl WHERE id <= 1000 AND id % 3 = 0",
    "CREATE OR REPLACE FUNCTION dl_pause(i int) RETURNS int LANGUAGE plpgsql IMMUTABLE AS $$ "
    "BEGIN IF current_setting('pgc.paused', true) IS DISTINCT FROM 'on' AND EXISTS "
    "(SELECT 1 FROM pg_stat_activity WHERE pid = pg_backend_pid() "
    "AND backend_type = 'pgcolumnar autovacuum worker') THEN "
    "PERFORM set_config('pgc.paused', 'on', false); PERFORM pg_sleep(3); END IF; "
    "RETURN i; END $$",
    "CREATE INDEX dl_pause_idx ON dl (dl_pause(id))",
)
EVENT = re.compile(r"pgcolumnar autovacuum: (compact_rewrite|recluster|skipped) public\.dl\b")


def _err(exc):
    return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


class _Av:
    def __init__(self, cluster):
        self.cluster = cluster
        self.dsn = cluster.dsn(dbname=DB)
        self.log = cluster.datadir / "server.log"

    def q(self, sql):
        try:
            with psycopg.connect(self.dsn, autocommit=True) as c:
                row = c.execute(sql).fetchone()
        except psycopg.Error as exc:
            return _err(exc)
        return "" if row is None or row[0] is None else str(row[0])

    def run(self, sql):
        try:
            with psycopg.connect(self.dsn, autocommit=True) as c:
                c.execute(sql.encode())
        except psycopg.Error as exc:
            return _err(exc)
        return ""

    def make(self):
        for sql in MAKE:
            e = self.run(sql)
            if e:
                return e
        return ""

    def logsize(self):
        try:
            return self.log.stat().st_size
        except OSError:
            return 0

    def tail(self, frm):
        try:
            with open(self.log, "rb") as f:
                f.seek(frm)
                return f.read().decode(errors="replace")
        except OSError as exc:
            return f"server log unreadable: {exc}"

    def events(self, frm):
        return " ".join(m.group(1) for m in EVENT.finditer(self.tail(frm)))

    def one_pass(self, frm):
        """"one worker" when the first compact_rewrite and the first recluster logged about dl
        past FROM came from the same worker process, else what was seen."""
        pids = {}
        for line in self.tail(frm).splitlines():
            m = EVENT.search(line)
            p = re.search(r"\[(\d+)\]", line)
            if m and p and m.group(1) in ("compact_rewrite", "recluster"):
                pids.setdefault(m.group(1), p.group(1))
        c, r = pids.get("compact_rewrite"), pids.get("recluster")
        return "one worker" if c and c == r else f"compact_rewrite [{c}], recluster [{r}]"

    def wait_event(self, frm, word):
        """Wait (bounded, 40 s) until the daemon logs WORD about dl past FROM."""
        for _ in range(400):
            if word in self.events(frm).split():
                return
            time.sleep(0.1)

    def switch(self, on):
        self.run(f"ALTER SYSTEM SET pgcolumnar.autovacuum = {'on' if on else 'off'}")
        self.run("SELECT pg_reload_conf()")
        if on:
            return
        for _ in range(300):
            if self.q("SELECT count(*) FROM pg_stat_activity "
                      "WHERE backend_type = 'pgcolumnar autovacuum worker'") == "0":
                return
            time.sleep(0.1)

    def wait_true(self, conn, cond):
        """On CONN, poll COND (bounded, 60 s), clearing the activity snapshot. -> its last value."""
        v = False
        for _ in range(600):
            conn.execute("SELECT pg_stat_clear_snapshot()")
            v = conn.execute(f"SELECT {cond}").fetchone()[0]
            if v:
                break
            time.sleep(0.1)
        return v


def _writer_pass(av, limit):
    """A fresh dl; the daemon runs with rewrite_lock_timeout LIMIT beside a writer that
    deletes id 2501 and flushes it, waits until the daemon sleeps in its compact_rewrite
    holding the storage-row lock, then inserts and commits.
    -> {"paused", "rc": "rc=0" | the SQLSTATE, "after", "events", "deadlocks"}."""
    e = av.make() or av.run(f"ALTER SYSTEM SET pgcolumnar.rewrite_lock_timeout = '{limit}'")
    if e:
        return {"paused": e, "rc": e, "after": e, "events": e, "one_pass": e, "deadlocks": e}
    out = {"paused": "none"}
    frm = None
    try:
        with psycopg.connect(av.dsn) as w:
            w.execute("SET deadlock_timeout = '6s'")
            w.execute("SET statement_timeout = '60s'")
            w.execute("DELETE FROM dl WHERE id = 2501")
            w.execute("SELECT count(*) FROM dl WHERE id = 2501").fetchone()
            frm = av.logsize()
            av.switch(True)
            with psycopg.connect(av.dsn, autocommit=True) as probe:
                out["paused"] = "true" if av.wait_true(probe, AV_PAUSED_102) else "false"
            w.execute("INSERT INTO dl VALUES (0, 0)")
            w.execute("SELECT count(*) FROM dl WHERE id = 0").fetchone()
            w.commit()
        out["rc"] = "rc=0"
    except psycopg.Error as exc:
        out["rc"] = exc.sqlstate or _err(exc)
    if frm is None:
        frm = av.logsize()
        av.switch(True)
    av.wait_event(frm, "recluster")
    av.switch(False)
    av.run("ALTER SYSTEM RESET pgcolumnar.rewrite_lock_timeout")
    av.run("SELECT pg_reload_conf()")
    out["after"] = av.q("SELECT count(*) FILTER (WHERE id = 2501) || ' ' || "
                        "count(*) FILTER (WHERE id = 0) FROM dl")
    out["events"] = av.events(frm)
    out["one_pass"] = av.one_pass(frm)
    out["deadlocks"] = av.tail(frm).count("deadlock detected")
    return out


def _yield(av):
    """A fresh dl; once the daemon pauses in its compact_rewrite, a session with a 1 s
    deadlock_timeout locks dl in ACCESS EXCLUSIVE mode for 3 s.
    -> {"paused", "granted": "granted" | error, "events"}."""
    e = av.make()
    if e:
        return {"paused": e, "granted": e, "events": e}
    out = {"paused": "none"}
    frm = av.logsize()
    av.switch(True)
    try:
        with psycopg.connect(av.dsn, autocommit=True) as c:
            c.execute("SET deadlock_timeout = '1s'")
            c.execute("SET statement_timeout = '60s'")
            out["paused"] = ("true" if av.wait_true(c, f"EXISTS (SELECT 1 FROM pg_stat_activity a "
                                                      f"WHERE {AV_PAUSED})") else "false")
            with c.transaction():
                c.execute("LOCK TABLE dl IN ACCESS EXCLUSIVE MODE")
                out["granted"] = "granted"
                c.execute("SELECT pg_sleep(3)")
    except psycopg.Error as exc:
        out.setdefault("granted", f"not granted: {exc.sqlstate or _err(exc)}")
    av.wait_event(frm, "recluster")
    av.switch(False)
    out["events"] = av.events(frm)
    return out


@pytest.fixture(scope="module")
def avt(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    av = _Av(pgc_cluster)
    try:
        av.run("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        for k, v in (("pgcolumnar.autovacuum", "off"), ("pgcolumnar.autovacuum_naptime", "1"),
                     ("pgcolumnar.autovacuum_compact_threshold", "0.05"),
                     ("pgcolumnar.autovacuum_recluster_threshold", "0.05"),
                     ("deadlock_timeout", "'20s'")):
            av.run(f"ALTER SYSTEM SET {k} = {v}")
        av.run("SELECT pg_reload_conf()")
        r = {"launcher": av.q("SELECT count(*) FROM pg_stat_activity "
                              "WHERE backend_type = 'pgcolumnar autovacuum launcher'")}
        e = av.make()
        r["due"] = e or av.q("SELECT compact_rewrite_due || ' ' || recluster_due "
                             "FROM pgcolumnar.maintenance_due('dl', 0.05, 0.05)")
        r["deadlock"] = _writer_pass(av, "10s")
        r["limit"] = _writer_pass(av, "1500ms")
        r["yield"] = _yield(av)
        yield r
    finally:
        av.switch(False)
        for g in TOUCHED:
            av.run(f"ALTER SYSTEM RESET {g}")
        av.run("SELECT pg_reload_conf()")
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _first(events, n):
    return " ".join(str(events).split()[:n])


def test_the_maintenance_launcher_is_running(avt, expect):
    expect.text(avt["launcher"], "1", "the maintenance launcher is running")


def test_dl_is_due_for_both_verbs(avt, expect):
    expect.text(avt["due"], "true true",
                "dl is due for both compact_rewrite and recluster at the daemon's thresholds")


def test_the_writer_saw_the_paused_compact_rewrite(avt, expect):
    expect.text(f"paused={avt['deadlock']['paused']}", "paused=true",
                "the writer saw the daemon paused in its compact_rewrite, holding the "
                "storage-row lock")


def test_the_writer_beside_both_verbs_commits(avt, expect):
    expect.text(str(avt["deadlock"]["rc"]), "rc=0",
                "the writer that deleted and then inserted beside the daemon's two verbs "
                "commits, with no deadlock")


def test_the_writers_delete_and_insert_took_effect(avt, expect):
    expect.text(avt["deadlock"]["after"], "0 1", "and the writer's DELETE and INSERT took effect")


def test_the_daemon_logged_compact_rewrite_then_recluster(avt, expect):
    expect.text(_first(avt["deadlock"]["events"], 2), "compact_rewrite recluster",
                "the daemon logged compact_rewrite and then recluster on dl in that pass")


def test_both_verbs_ran_in_one_pass(avt, expect):
    expect.text(avt["deadlock"]["one_pass"], "one worker",
                "and the daemon's compact_rewrite and recluster on dl came from one worker, one pass")


def test_no_session_hit_a_deadlock_beside_the_pass(avt, expect):
    expect.text(str(avt["deadlock"]["deadlocks"]), "0",
                "no session hit a deadlock beside the daemon's pass")


def test_the_limit_writer_saw_the_paused_compact_rewrite(avt, expect):
    expect.text(f"paused={avt['limit']['paused']}", "paused=true",
                "beside a 1.5 s rewrite_lock_timeout, the writer saw the paused compact_rewrite")


def test_the_limit_writer_commits(avt, expect):
    expect.text(str(avt["limit"]["rc"]), "rc=0", "and that writer commits")


def test_the_recluster_finished_after_a_long_compact_rewrite(avt, expect):
    expect.text(_first(avt["limit"]["events"], 2), "compact_rewrite recluster",
                "the daemon's recluster, with a 1.5 s rewrite_lock_timeout, finished after a "
                "compact_rewrite of 3 s")


def test_the_locker_saw_the_paused_compact_rewrite(avt, expect):
    expect.text(f"paused={avt['yield']['paused']}", "paused=true",
                "the locker saw the daemon paused in its compact_rewrite")


def test_the_lock_table_was_granted(avt, expect):
    expect.text(avt["yield"]["granted"], "granted",
                "a LOCK TABLE with a 1 s deadlock_timeout was granted beside the paused "
                "compact_rewrite")


def test_a_cancelled_verb_ends_the_pass(avt, expect):
    expect.text(_first(avt["yield"]["events"], 3), "skipped compact_rewrite recluster",
                "after its cancelled compact_rewrite, the daemon's next verb on dl is the next "
                "pass's compact_rewrite, not a recluster")
