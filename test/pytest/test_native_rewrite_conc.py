"""The online rewrite runs alongside writers without losing or resurrecting a row, and
without blocking them (Phase F3b). The pytest twin of `test/native_rewrite_conc.sh`,
every check by the shell's name (#1341).

Six workers insert, delete and update disjoint id ranges of a columnar table and of its
heap oracle, while a compactor loop runs compact_rewrite (online rewrite of partially
deleted groups) and compact (retire fully-dead groups). A delete or update that races a
rewrite of its group is refused with a serialization failure and retried, so the final
state is deterministic and the columnar table must equal the heap oracle. Asserted: every
worker finished, the compactor moved rows while they wrote (so the race was run, not
assumed), the two tables agree by whole row, by count and sum, and over a range, and a
compact_rewrite held mid-verb with its lock taken lets a reader and a writer, each with a
3 s lock_timeout, finish while it still holds the lock -- a rewrite that took a stronger
lock would otherwise pass everything above, since the workers would simply wait for it.
Last, on a second private database, recluster and compact_rewrite each meet a single-row
UPDATE from an open writer, and a writer that deleted, scanned and then inserts, without
a deadlock (#2074). Then each verb gives back the group locks it took before it waits
on a busy group, and the busy group's lock after that wait, counts a group lock its own
transaction holds as taken, and stops with SQLSTATE 55P03 within
pgcolumnar.rewrite_lock_timeout under steady deletes and behind a writer that stays open
(review of #2139 at 630f63e3). That open writer is a deleter, holding a group lock, and an
inserter, holding the storage-row lock. After the verb, on success and after its 55P03, the
session's lock_timeout is back to what the user set, and a shorter user lock_timeout ends
the verb with PostgreSQL's own lock-timeout error (review of #2139 at 2fe1d31f).

ON A PRIVATE DATABASE, built in the shell's order by a module fixture.
"""
import threading
import time

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_native_rewrite_conc"
WORKERS = 6
PER = 3000
COLS = "id int, cat int, v bigint, txt text"


def _err(exc):
    return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


class _Db:
    def __init__(self, dsn):
        self.dsn = dsn

    def run(self, sql, settings=()):
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                for s in settings:
                    conn.execute(s)
                conn.execute(sql.encode())  # simple protocol, as psql -c
        except psycopg.Error as exc:
            return _err(exc)
        return ""

    def q(self, sql):
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                row = conn.execute(sql).fetchone()
        except psycopg.Error as exc:
            return _err(exc)
        if row is None or row[0] is None:
            return ""
        return str(row[0])

    def n(self, sql):
        v = self.q(sql)
        return int(v) if v.lstrip("-").isdigit() else v

    def set_hash(self, sql):
        return self.q("SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), 'EMPTY') "
                      f"FROM (SELECT _row::text AS t FROM ({sql}) _row) _s")


def _retry(d, sql, errors):
    """Run one autocommit statement, retrying on failure as the shell's runsql does (each
    failed attempt rolls back fully, so a retry applies exactly once). -> True when it
    succeeded within 200 attempts."""
    for _ in range(200):
        e = d.run(sql)
        if e == "":
            return True
        errors.append(e)
    return False


@pytest.fixture(scope="module")
def rc(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        d = _Db(pgc_cluster.dsn(dbname=DB))
        d.run("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        d.run(f"CREATE TABLE t_heap ({COLS}) USING heap")
        d.run(f"CREATE TABLE t_col ({COLS}) USING pgcolumnar")
        d.run("SELECT pgcolumnar.set_options('t_col', chunk_group_row_limit => 500, stripe_row_limit => 1000)")

        stop = threading.Event()
        moved = []

        def compactor():
            while not stop.is_set():
                v = d.n("SELECT pgcolumnar.compact_rewrite('t_col', 0.0)")
                if isinstance(v, int):
                    moved.append(v)
                d.run("SELECT pgcolumnar.compact('t_col')")

        results = [None] * WORKERS
        errs = [[] for _ in range(WORKERS)]

        def worker(w):
            lo, hi = w * PER + 1, (w + 1) * PER
            steps = [
                f"INSERT INTO t_heap SELECT g, g%5, g*3, md5(g::text) FROM generate_series({lo},{hi}) g",
                f"INSERT INTO t_col  SELECT g, g%5, g*3, md5(g::text) FROM generate_series({lo},{hi}) g",
                f"DELETE FROM t_heap WHERE id BETWEEN {lo} AND {hi} AND id % 7 = 0",
                f"DELETE FROM t_col  WHERE id BETWEEN {lo} AND {hi} AND id % 7 = 0",
                f"UPDATE t_heap SET v = v + 1 WHERE id BETWEEN {lo} AND {hi} AND id % 5 = 0",
                f"UPDATE t_col  SET v = v + 1 WHERE id BETWEEN {lo} AND {hi} AND id % 5 = 0",
            ]
            results[w] = all(_retry(d, s, errs[w]) for s in steps)

        comp = threading.Thread(target=compactor)
        comp.start()
        ws = [threading.Thread(target=worker, args=(w,)) for w in range(WORKERS)]
        for t in ws:
            t.start()
        for t in ws:
            t.join()
        stop.set()
        comp.join()

        r = {
            "fail": "0" if all(results) else "1",
            "moved": sum(moved),
            "retries": sum(1 for e in errs for x in e if "compacted concurrently" in x),
        }
        # a final settling pass, then the columnar table must equal the heap oracle
        d.run("SELECT pgcolumnar.compact_rewrite('t_col', 0.0)")
        d.run("SELECT pgcolumnar.compact('t_col')")
        for label, tmpl in (("whole-row", "SELECT * FROM %T"),
                            ("count/sum", "SELECT count(*), sum(v), count(distinct cat) FROM %T"),
                            ("range", "SELECT count(*) FROM %T WHERE id BETWEEN 4000 AND 12000")):
            hq = d.set_hash(tmpl.replace("%T", "t_heap"))
            hc = d.set_hash(tmpl.replace("%T", "t_col"))
            r[label] = (hc or "COL_ERROR", hq or "HEAP_ERROR")
        r["count"] = d.n("SELECT count(*) FROM t_col")

        # the online rewrite does not block readers or writers
        before = d.n("SELECT count(*) FROM t_col")
        holder = {}

        def hold():
            holder["result"] = d.run("SELECT pgcolumnar.compact_rewrite('t_col', 0.0)",
                                     settings=("SET pgcolumnar.maintenance_hold_ms = 12000",))

        def held():
            return d.n("SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a USING (pid) "
                       "WHERE l.relation = 't_col'::regclass AND l.granted "
                       "AND a.query LIKE '%compact_rewrite%' AND a.pid <> pg_backend_pid()")

        ht = threading.Thread(target=hold)
        ht.start()
        for _ in range(50):
            if held() == 1:
                break
            time.sleep(0.2)
        r["held"] = held()
        try:
            with psycopg.connect(d.dsn, autocommit=True) as conn:
                with conn.transaction():
                    conn.execute("SET LOCAL lock_timeout = '3s'")
                    r["reader"] = conn.execute("SELECT count(*) FROM t_col").fetchone()[0]
        except psycopg.Error as exc:
            r["reader"] = _err(exc)
        try:
            with psycopg.connect(d.dsn, autocommit=True) as conn:
                with conn.transaction():
                    conn.execute("SET LOCAL lock_timeout = '3s'")
                    conn.execute("INSERT INTO t_col VALUES (-1, 0, 0, 'probe')")
            r["writer"] = "inserted"
        except psycopg.Error:
            r["writer"] = "refused"
        r["still held"] = held()
        ht.join()
        r["holder"] = "rc=0" if holder.get("result") == "" else f"rc=1 ({holder.get('result')})"
        r["before"] = before
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_all_rewrite_workers_succeeded(rc, expect):
    expect.text(rc["fail"], "0", "all workers succeeded (retried past serialization failures)")


def test_the_compactor_rewrote_groups_while_the_workers_were_writing(rc, expect):
    m = rc["moved"]
    expect.text("yes" if isinstance(m, int) and m > 0 else f"no (moved [{m}])", "yes",
                "the compactor rewrote groups while the workers were writing")


def test_conc_rewrite_whole_row(rc, expect):
    expect.text(rc["whole-row"][0], rc["whole-row"][1], "conc-rewrite whole-row")


def test_conc_rewrite_count_sum(rc, expect):
    expect.text(rc["count/sum"][0], rc["count/sum"][1], "conc-rewrite count/sum")


def test_conc_rewrite_range(rc, expect):
    expect.text(rc["range"][0], rc["range"][1], "conc-rewrite range")


def test_row_count_sane(rc, expect):
    c = rc["count"]
    expect.text("ok" if isinstance(c, int) and c > 0 else f"no ({c})", "ok", "row count sane")


def test_a_compact_rewrite_holds_its_lock_mid_verb(rc, expect):
    expect.text(str(rc["held"]), "1", "a compact_rewrite holds its lock on t_col mid-verb")


def test_a_reader_finishes_while_the_rewrite_holds_its_lock(rc, expect):
    expect.num(rc["reader"], rc["before"],
               "a reader with a 3 s lock_timeout finishes while the rewrite holds its lock")


def test_a_writer_inserts_while_the_rewrite_holds_its_lock(rc, expect):
    expect.text(rc["writer"], "inserted",
                "a writer with a 3 s lock_timeout inserts while the rewrite holds its lock")


def test_the_rewrite_still_held_its_lock_when_both_had_finished(rc, expect):
    expect.text(str(rc["still held"]), "1", "and the rewrite still held its lock when both had finished")


def test_the_held_rewrite_itself_completed(rc, expect):
    expect.text(rc["holder"], "rc=0", "the held rewrite itself completed")


# ---- an online rewrite and an open writer cannot deadlock ----------------------
#
# Issue #2074. The storage-row lock (class 102) is taken by every row-group flush and
# held to commit; the chunk-group lock (class 101) guards a group's delete vector. A
# writer takes the two in either order and holds both to commit. In the update shape,
# an UPDATE flushes its new row (102), then its delete vector (101). In the delete
# shape, a later scan flushes a DELETE's marks (101), then COMMIT flushes an INSERT
# (102). So a rewrite that waits for one class while it holds the other closes a cycle
# with one shape: main's rewrites (101, then wait on 102) deadlocked with the update
# shape, and taking 102 first (then wait on 101) deadlocked with the delete shape.
# To place each cycle deterministically, an open writer takes its first lock only, the
# rewrite runs out of its hold until pg_locks shows it waiting on that lock, and only
# then does the writer take its second lock on a covered group and commit.

DL_DB = "pgc_native_rewrite_conc_dl"
DL_VERBS = {
    "recluster": "pgcolumnar.recluster('dl', 'id')",
    "compact_rewrite": "pgcolumnar.compact_rewrite('dl', 0.0)",
}
# shape -> (first statement, the lock class it leaves the writer holding, second statement)
DL_SHAPES = {
    "update": ("INSERT INTO dl VALUES (0, 0)", 102, "UPDATE dl SET bal = bal + 1 WHERE id = 5"),
    "delete": ("DELETE FROM dl WHERE id = 5", 101, "INSERT INTO dl VALUES (0, 0)"),
}


def _waiting(cls):
    return ("SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
            f"AND objsubid = {cls} AND NOT granted AND pid <> pg_backend_pid())")


def _dl_race(d, verb, shape):
    """-> {"waited": bool | error, "writer": "ok" | error, "rewrite": "ok" | error,
    "after": the shape's effect afterwards}."""
    first, cls, second = DL_SHAPES[shape]
    for sql in ("DROP TABLE IF EXISTS dl",
                "CREATE TABLE dl (id int, bal int) USING pgcolumnar",
                "SELECT pgcolumnar.set_options('dl', stripe_row_limit => 1000, "
                "chunk_group_row_limit => 100)",
                "INSERT INTO dl SELECT g, 1000 FROM generate_series(1, 3000) g",
                "DELETE FROM dl WHERE id <= 1000 AND id % 3 = 0"):
        e = d.run(sql)
        if e:
            return {"waited": e, "writer": e, "rewrite": e, "after": e}
    out = {}

    def rewrite():
        e = d.run(f"SELECT {verb}", settings=("SET pgcolumnar.maintenance_hold_ms = 3000",))
        out["rewrite"] = e or "ok"

    rt = threading.Thread(target=rewrite)
    rt.start()
    try:
        # bounded wait until the rewrite holds its table lock, so it is inside its hold
        for _ in range(50):
            if d.n("SELECT count(*) FROM pg_locks WHERE relation = 'dl'::regclass AND granted "
                   "AND mode = 'ShareUpdateExclusiveLock' AND pid <> pg_backend_pid()") == 1:
                break
            time.sleep(0.1)
        with psycopg.connect(d.dsn) as w:
            try:
                w.execute(first)
                w.execute("SELECT count(*) FROM dl").fetchone()  # flushes: takes `cls`
                waited = False
                with psycopg.connect(d.dsn, autocommit=True) as probe:
                    for _ in range(300):
                        waited = probe.execute(_waiting(cls)).fetchone()[0]
                        if waited:
                            break
                        time.sleep(0.1)
                out["waited"] = waited
                w.execute(second)
                w.commit()
                out["writer"] = "ok"
            except psycopg.Error as exc:
                out.setdefault("waited", _err(exc))
                out["writer"] = _err(exc)
    finally:
        rt.join()
    if shape == "update":
        out["after"] = d.q("SELECT bal FROM dl WHERE id = 5")
    else:
        out["after"] = d.q("SELECT count(*) FILTER (WHERE id = 5) || ' ' || "
                           "count(*) FILTER (WHERE id = 0) FROM dl")
    return out


# ---- the rewrite gives its locks back, and its waiting has a bound ------------
#
# Review of #2139 at 630f63e3. lock_rewrite_groups waits for one lock at a time while
# it holds no other, and gives up after pgcolumnar.rewrite_lock_timeout. Each arm
# reddens one way of getting that wrong. Gives back: the writer deletes in group 1, the
# rewrite waits on it, then the writer updates group 0; a rewrite that kept group 0
# deadlocks. After the wait: W1 holds group 0's lock and W2 the storage-row lock; when
# W1 commits the rewrite waits on W2, then W2 updates group 0; a rewrite that kept group
# 0 deadlocks. Same transaction: a verb after a DELETE in its own transaction already
# holds that group's lock and must count it as taken. Bound: under steady deletes, and
# behind a writer that does not commit, the verb stops with 55P03 within the limit.

DL_SAMETXN_WANT = {"recluster": "3", "compact_rewrite": "1"}


def _dl_make(d, extra=False, rows=3000, deletes="id <= 1000 AND id % 3 = 0"):
    for sql in ("DROP TABLE IF EXISTS dl",
                "CREATE TABLE dl (id int, bal int) USING pgcolumnar",
                "SELECT pgcolumnar.set_options('dl', stripe_row_limit => 1000, "
                "chunk_group_row_limit => 100)",
                f"INSERT INTO dl SELECT g, 1000 FROM generate_series(1, {rows}) g",
                f"DELETE FROM dl WHERE {deletes}"):
        e = d.run(sql)
        if e:
            return e
    if extra:
        return d.run("DELETE FROM dl WHERE id BETWEEN 1001 AND 2000 AND id % 3 = 0")
    return ""


def _dl_wait(d, sql):
    """Poll `sql` (a boolean query) every 0.1 s for up to 30 s. -> its last value."""
    v = False
    with psycopg.connect(d.dsn, autocommit=True) as probe:
        for _ in range(300):
            v = probe.execute(sql).fetchone()[0]
            if v:
                break
            time.sleep(0.1)
    return v


def _dl_rewrite_bg(d, verb, out):
    """Start the verb with a 3 s hold in a thread; return once it holds its table lock."""
    def rewrite():
        e = d.run(f"SELECT {verb}", settings=("SET pgcolumnar.maintenance_hold_ms = 3000",))
        out["rewrite"] = e or "ok"

    rt = threading.Thread(target=rewrite)
    rt.start()
    for _ in range(50):
        if d.n("SELECT count(*) FROM pg_locks WHERE relation = 'dl'::regclass AND granted "
               "AND mode = 'ShareUpdateExclusiveLock' AND pid <> pg_backend_pid()") == 1:
            break
        time.sleep(0.1)
    return rt


def _dl_giveback(d, verb):
    """-> {"waited", "writer", "rewrite", "after": "<id 1501 count> <bal of id 5>"}."""
    e = _dl_make(d, extra=True)
    if e:
        return {"waited": e, "writer": e, "rewrite": e, "after": e}
    out = {}
    rt = _dl_rewrite_bg(d, verb, out)
    try:
        with psycopg.connect(d.dsn) as w:
            try:
                w.execute("DELETE FROM dl WHERE id = 1501")
                w.execute("SELECT count(*) FROM dl").fetchone()  # flushes: group 1's lock
                out["waited"] = _dl_wait(d, _waiting(101))
                w.execute("UPDATE dl SET bal = bal + 1 WHERE id = 5")
                w.execute("SELECT count(*) FROM dl").fetchone()  # flushes: group 0's lock
                w.commit()
                out["writer"] = "ok"
            except psycopg.Error as exc:
                out.setdefault("waited", _err(exc))
                out["writer"] = _err(exc)
    finally:
        rt.join()
    out["after"] = d.q("SELECT count(*) FILTER (WHERE id = 1501) || ' ' || "
                       "max(bal) FILTER (WHERE id = 5) FROM dl")
    return out


def _dl_afterwait(d, verb):
    """-> {"waited102", "w1", "w2", "rewrite", "after": "<id 5> <id 0> <bal of id 7>"}."""
    e = _dl_make(d)
    if e:
        return {"waited102": e, "w1": e, "w2": e, "rewrite": e, "after": e}
    out = {}
    rt = _dl_rewrite_bg(d, verb, out)

    def w1():
        try:
            with psycopg.connect(d.dsn) as c:
                c.execute("DELETE FROM dl WHERE id = 5")
                c.execute("SELECT count(*) FROM dl").fetchone()  # group 0's lock
                _dl_wait(d, _waiting(101))
                _dl_wait(d, "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
                            "AND objsubid = 102 AND granted)")
                c.commit()
            out["w1"] = "ok"
        except psycopg.Error as exc:
            out["w1"] = _err(exc)

    t1 = threading.Thread(target=w1)
    t1.start()
    try:
        _dl_wait(d, _waiting(101))
        with psycopg.connect(d.dsn) as w:
            try:
                w.execute("INSERT INTO dl VALUES (0, 0)")
                w.execute("SELECT count(*) FROM dl").fetchone()  # the storage-row lock
                out["waited102"] = _dl_wait(d, _waiting(102))
                w.execute("UPDATE dl SET bal = bal + 1 WHERE id = 7")
                w.execute("SELECT count(*) FROM dl").fetchone()  # group 0's lock
                w.commit()
                out["w2"] = "ok"
            except psycopg.Error as exc:
                out.setdefault("waited102", _err(exc))
                out["w2"] = _err(exc)
    finally:
        t1.join()
        rt.join()
    out["after"] = d.q("SELECT count(*) FILTER (WHERE id = 5) || ' ' || "
                       "count(*) FILTER (WHERE id = 0) || ' ' || "
                       "max(bal) FILTER (WHERE id = 7) FROM dl")
    return out


def _dl_sametxn(d, verb):
    """-> {"result": "rc=<0|error> verb=<value>", "after": id 7's count}."""
    e = _dl_make(d)
    if e:
        return {"result": e, "after": e}
    try:
        with psycopg.connect(d.dsn) as c:
            c.execute("SET statement_timeout = '20s'")
            c.execute("DELETE FROM dl WHERE id = 7")
            v = c.execute(f"SELECT {verb}").fetchone()[0]
            c.commit()
        result = f"rc=0 verb={v}"
    except psycopg.Error as exc:
        result = f"rc=error verb={_err(exc)}"
    return {"result": result, "after": d.q("SELECT count(*) FROM dl WHERE id = 7")}


def _dl_timed(d, verb, settings):
    """Run SETTINGS, then the verb, then read the session's own lock_timeout, all in one
    session. -> (elapsed ms, "ok" or the SQLSTATE, the error message or "",
    "lock_timeout=<value>")."""
    with psycopg.connect(d.dsn, autocommit=True) as c:
        c.execute("SET statement_timeout = '30s'")
        for s in settings:
            c.execute(s)
        t0 = time.monotonic()
        try:
            c.execute(f"SELECT {verb}")
            state, msg = "ok", ""
        except psycopg.Error as exc:
            state, msg = exc.sqlstate or _err(exc), str(exc)
        ms = int((time.monotonic() - t0) * 1000)
        lt = c.execute("SELECT current_setting('lock_timeout')").fetchone()[0]
    return ms, state, msg, f"lock_timeout={lt}"


def _dl_starve(d, verb):
    """Eight deleters each delete one random row, flush it with a scan and hold the group
    lock for 200 ms, in a loop; the verb runs with a 2 s limit.
    -> {"ms", "state", "deadlocks": deleter errors that name a deadlock}."""
    e = _dl_make(d, rows=20000, deletes="id % 3 = 0")
    if e:
        return {"ms": e, "state": e, "deadlocks": e}
    stop = threading.Event()
    errors = []

    def deleter(seed):
        import random
        r = random.Random(seed)
        while not stop.is_set():
            a = r.randint(1, 20000)
            try:
                with psycopg.connect(d.dsn) as c:
                    c.execute(f"DELETE FROM dl WHERE id = {a}")
                    c.execute(f"SELECT count(*) FROM dl WHERE id = {a}").fetchone()
                    c.execute("SELECT pg_sleep(0.2)")
                    c.commit()
            except psycopg.Error as exc:
                errors.append(str(exc))

    ts = [threading.Thread(target=deleter, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    try:
        time.sleep(2)
        ms, state, _, _ = _dl_timed(d, verb, ("SET pgcolumnar.rewrite_lock_timeout = '2s'",))
    finally:
        stop.set()
        for t in ts:
            t.join()
    return {"ms": ms, "state": state,
            "deadlocks": sum("deadlock detected" in x for x in errors)}


# the writers _dl_held runs: a deleter flushes its delete with a scan and so holds its
# group's lock (101); an inserter flushes its row with a scan and so holds the storage-row
# lock (102)
DL_DELETER = ("DELETE FROM dl WHERE id = 5", "SELECT count(*) FROM dl")
DL_INSERTER = ("INSERT INTO dl VALUES (0, 0)", "SELECT count(*) FROM dl")


def _dl_held(d, verb, writer_sqls, cls, hold, settings):
    """A writer runs writer_sqls, which take the lock of class cls (101 or 102), and keeps
    its transaction open for hold seconds. Once it holds that lock, one session runs
    settings, the verb, and reads its own lock_timeout.
    -> {"ms", "state", "kind": names-limit | stock | other, "lock_timeout"}."""
    e = _dl_make(d)
    if e:
        return {"ms": e, "state": e, "kind": e, "lock_timeout": e}

    def writer():
        try:
            with psycopg.connect(d.dsn) as c:
                for sql in writer_sqls:
                    c.execute(sql)
                c.execute(f"SELECT pg_sleep({hold})")
                c.commit()
        except psycopg.Error:
            pass

    wt = threading.Thread(target=writer)
    wt.start()
    try:
        _dl_wait(d, "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
                    f"AND objsubid = {cls} AND granted)")
        ms, state, msg, lt = _dl_timed(d, verb, settings)
    finally:
        wt.join()
    kind = ("names-limit" if "pgcolumnar.rewrite_lock_timeout" in msg
            else "stock" if "canceling statement due to lock timeout" in msg else "other")
    return {"ms": ms, "state": state, "kind": kind, "lock_timeout": lt}


def _dl_finished(d, verb):
    """With no writer, one session runs the verb with a 10 s rewrite_lock_timeout and then
    reads its own lock_timeout. -> "<ok|SQLSTATE> lock_timeout=<value>"."""
    e = _dl_make(d)
    if e:
        return e
    _, state, _, lt = _dl_timed(d, verb, ("SET pgcolumnar.rewrite_lock_timeout = '10s'",))
    return f"{state} {lt}"


@pytest.fixture(scope="module")
def dl(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DL_DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DL_DB}")
    try:
        d = _Db(pgc_cluster.dsn(dbname=DL_DB))
        d.run("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        # the shell's order: for each verb, the update shape, then the delete shape;
        # then for each verb the arms of the 630f63e3 and 2fe1d31f reviews
        out = {(name, shape): _dl_race(d, verb, shape)
               for name, verb in DL_VERBS.items() for shape in DL_SHAPES}
        for name, verb in DL_VERBS.items():
            out[name, "giveback"] = _dl_giveback(d, verb)
            out[name, "afterwait"] = _dl_afterwait(d, verb)
            out[name, "sametxn"] = _dl_sametxn(d, verb)
            out[name, "starve"] = _dl_starve(d, verb)
            out[name, "stuck"] = _dl_held(d, verb, DL_DELETER, 101, 8,
                                          ("SET pgcolumnar.rewrite_lock_timeout = '1s'",))
            out[name, "stuck102"] = _dl_held(d, verb, DL_INSERTER, 102, 8,
                                             ("SET pgcolumnar.rewrite_lock_timeout = '1s'",))
            out[name, "finished"] = _dl_finished(d, verb)
            out[name, "shorter"] = _dl_held(d, verb, DL_DELETER, 101, 6,
                                            ("SET lock_timeout = '500ms'",
                                             "SET pgcolumnar.rewrite_lock_timeout = '5s'"))
        yield out
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DL_DB} WITH (FORCE)")


@pytest.mark.parametrize("name", list(DL_VERBS))
def test_the_rewrite_waited_on_the_storage_row_lock_of_an_open_writer(dl, name, expect):
    expect.text(str(dl[name, "update"]["waited"]), "True",
                f"a {name} waited on the storage-row lock of an open writer")


@pytest.mark.parametrize("name", list(DL_VERBS))
def test_a_single_row_update_and_the_rewrite_both_commit(dl, name, expect):
    r = dl[name, "update"]
    expect.text(f"update={r['writer']} rewrite={r['rewrite']}", "update=ok rewrite=ok",
                f"a single-row UPDATE from that writer and the {name} both commit, "
                "with no deadlock")


@pytest.mark.parametrize("name", list(DL_VERBS))
def test_the_update_beside_the_rewrite_took_effect(dl, name, expect):
    expect.text(dl[name, "update"]["after"], "1001",
                f"and the UPDATE beside the {name} took effect")


@pytest.mark.parametrize("name", list(DL_VERBS))
def test_the_rewrite_waited_on_the_chunk_group_lock_of_a_writer_that_deleted(dl, name, expect):
    expect.text(str(dl[name, "delete"]["waited"]), "True",
                f"a {name} waited on the chunk-group lock of an open writer that deleted "
                "and scanned")


@pytest.mark.parametrize("name", list(DL_VERBS))
def test_a_delete_scan_insert_writer_and_the_rewrite_both_commit(dl, name, expect):
    r = dl[name, "delete"]
    expect.text(f"writer={r['writer']} rewrite={r['rewrite']}", "writer=ok rewrite=ok",
                f"a DELETE, a scan and an INSERT from that writer and the {name} both "
                "commit, with no deadlock")


@pytest.mark.parametrize("name", list(DL_VERBS))
def test_the_delete_and_insert_beside_the_rewrite_took_effect(dl, name, expect):
    expect.text(dl[name, "delete"]["after"], "0 1",
                f"and the DELETE and INSERT beside the {name} took effect")


@pytest.mark.parametrize("name", list(DL_VERBS))
def test_the_rewrite_gave_back_its_group_locks_before_it_waited(dl, name, expect):
    expect.text(str(dl[name, "giveback"]["waited"]), "True",
                f"a {name} gave back its group locks before it waited on a later busy group")


@pytest.mark.parametrize("name", list(DL_VERBS))
def test_a_delete_in_group_1_then_an_update_in_group_0_and_the_rewrite_both_commit(dl, name, expect):
    r = dl[name, "giveback"]
    expect.text(f"writer={r['writer']} rewrite={r['rewrite']}", "writer=ok rewrite=ok",
                f"a DELETE in group 1 then an UPDATE in group 0 from one writer and the {name} "
                "both commit")


@pytest.mark.parametrize("name", list(DL_VERBS))
def test_the_delete_in_group_1_and_the_update_in_group_0_took_effect(dl, name, expect):
    expect.text(dl[name, "giveback"]["after"], "0 1001",
                f"and the DELETE in group 1 and the UPDATE in group 0 beside the {name} took effect")


@pytest.mark.parametrize("name", list(DL_VERBS))
def test_the_rewrite_waited_on_the_storage_row_lock_after_its_group_wait(dl, name, expect):
    expect.text(str(dl[name, "afterwait"]["waited102"]), "True",
                f"a {name} waited on the storage-row lock after the group lock it waited for "
                "came free")


@pytest.mark.parametrize("name", list(DL_VERBS))
def test_two_writers_and_the_rewrite_all_commit(dl, name, expect):
    r = dl[name, "afterwait"]
    expect.text(f"w1={r.get('w1')} w2={r.get('w2')} rewrite={r.get('rewrite')}",
                "w1=ok w2=ok rewrite=ok",
                f"two writers and the {name} all commit when the second writer updates that group")


@pytest.mark.parametrize("name", list(DL_VERBS))
def test_both_writers_changes_beside_the_rewrite_took_effect(dl, name, expect):
    expect.text(dl[name, "afterwait"]["after"], "0 1 1001",
                f"and both writers' changes beside the {name} took effect")


@pytest.mark.parametrize("name", list(DL_VERBS))
def test_the_rewrite_after_a_delete_in_its_own_transaction_commits(dl, name, expect):
    expect.text(dl[name, "sametxn"]["result"], f"rc=0 verb={DL_SAMETXN_WANT[name]}",
                f"a {name} after a DELETE in its own transaction commits and returns its count")


@pytest.mark.parametrize("name", list(DL_VERBS))
def test_the_row_deleted_before_the_rewrite_stays_deleted(dl, name, expect):
    expect.text(dl[name, "sametxn"]["after"], "0",
                f"and the row deleted before the {name} in its transaction stays deleted")


@pytest.mark.parametrize("name", list(DL_VERBS))
def test_the_rewrite_under_steady_deletes_ends_within_its_limit(dl, name, expect):
    ms = dl[name, "starve"]["ms"]
    expect.text("yes" if isinstance(ms, int) and ms < 6000 else f"no ({ms} ms)", "yes",
                f"a {name} under steady deletes ends within its 2 s rewrite_lock_timeout plus 4 s")


@pytest.mark.parametrize("name", list(DL_VERBS))
def test_the_rewrite_under_steady_deletes_finished_or_stopped_with_55p03(dl, name, expect):
    st = dl[name, "starve"]["state"]
    expect.text("ok-or-55P03" if st in ("ok", "55P03") else st, "ok-or-55P03",
                f"and the {name} either finished or stopped with the lock error 55P03")


@pytest.mark.parametrize("name", list(DL_VERBS))
def test_no_deleter_beside_the_rewrite_hit_a_deadlock(dl, name, expect):
    expect.text(str(dl[name, "starve"]["deadlocks"]), "0",
                f"and no deleter beside the {name} hit a deadlock")


@pytest.mark.parametrize("name", list(DL_VERBS))
def test_the_rewrite_behind_an_open_writer_stops_with_55p03(dl, name, expect):
    r = dl[name, "stuck"]
    expect.text(f"{r['state']} {r['kind']}", "55P03 names-limit",
                f"a {name} behind a writer that stays open stops with 55P03 naming "
                "rewrite_lock_timeout")


@pytest.mark.parametrize("name", list(DL_VERBS))
def test_the_rewrite_behind_an_open_writer_stops_within_its_limit(dl, name, expect):
    ms = dl[name, "stuck"]["ms"]
    expect.text("yes" if isinstance(ms, int) and ms < 4000 else f"no ({ms} ms)", "yes",
                f"and the {name} stops within its 1 s rewrite_lock_timeout plus 3 s")


@pytest.mark.parametrize("name", list(DL_VERBS))
def test_the_session_lock_timeout_after_the_rewrite_55p03_is_back_to_0(dl, name, expect):
    expect.text(dl[name, "stuck"]["lock_timeout"], "lock_timeout=0",
                f"and the session's lock_timeout after the {name}'s 55P03 is back to 0")


@pytest.mark.parametrize("name", list(DL_VERBS))
def test_the_rewrite_behind_an_open_inserter_stops_with_55p03(dl, name, expect):
    r = dl[name, "stuck102"]
    expect.text(f"{r['state']} {r['kind']}", "55P03 names-limit",
                f"a {name} behind an inserter that stays open stops with 55P03 naming "
                "rewrite_lock_timeout")


@pytest.mark.parametrize("name", list(DL_VERBS))
def test_the_rewrite_behind_an_open_inserter_stops_within_its_limit(dl, name, expect):
    ms = dl[name, "stuck102"]["ms"]
    expect.text("yes" if isinstance(ms, int) and ms < 4000 else f"no ({ms} ms)", "yes",
                f"and the {name} behind that inserter stops within its 1 s "
                "rewrite_lock_timeout plus 3 s")


@pytest.mark.parametrize("name", list(DL_VERBS))
def test_the_session_lock_timeout_after_the_rewrite_finished_is_back_to_0(dl, name, expect):
    expect.text(dl[name, "finished"], "ok lock_timeout=0",
                f"after a {name} that finished, the session's lock_timeout is back to 0")


@pytest.mark.parametrize("name", list(DL_VERBS))
def test_the_rewrite_under_a_shorter_lock_timeout_stops_with_the_stock_error(dl, name, expect):
    r = dl[name, "shorter"]
    expect.text(f"{r['state']} {r['kind']}", "55P03 stock",
                f"a {name} under a shorter lock_timeout of 500 ms stops with the stock "
                "lock-timeout error")


@pytest.mark.parametrize("name", list(DL_VERBS))
def test_the_rewrite_under_a_shorter_lock_timeout_stops_within_it(dl, name, expect):
    ms = dl[name, "shorter"]["ms"]
    expect.text("yes" if isinstance(ms, int) and ms < 2000 else f"no ({ms} ms)", "yes",
                f"and the {name} stops within that 500 ms lock_timeout plus 1.5 s")
