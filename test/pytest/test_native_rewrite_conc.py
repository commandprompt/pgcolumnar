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
