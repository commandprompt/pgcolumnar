"""Concurrent writers to one columnar relation end equal to a heap oracle. The pytest
twin of `test/concurrent_diff.sh`, every check by the shell's name (#1341).

Six backends run the same INSERT/DELETE/UPDATE workload over disjoint id ranges
against a heap table and a columnar one, so the final logical state is deterministic
whatever the interleaving, and the columnar table must end equal to the heap. This
exercises concurrent writers to one columnar relation: stripe and row-number
reservation, the delete vector, and the encoding and bloom write paths.

Each worker is a thread with a connection of its own, and sends its workload as one
simple-protocol query, as `psql -c` does, so it runs in one implicit transaction. The
workers must overlap: every one starts before any finishes. Each comparison hashes
both sides on the server exactly as lib.sh's `pgc_set_hash` does, and a failed query
is a `query_error()`, so two failures never compare equal.

ON A PRIVATE DATABASE, built once by a module fixture; each test asserts one reading,
so one red cannot stop the next (#1339).
"""
import threading
import time

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_concurrent_diff"
WORKERS = 6
PER = 3000
SET_HASH = ("SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), $e$EMPTY$e$) "
            "FROM (SELECT _row::text AS t FROM ( {q} ) _row) _s")
DIFFS = {
    "concurrent whole-row": "SELECT * FROM %T",
    "concurrent count/sum": "SELECT count(*), sum(v), count(distinct cat) FROM %T",
    # min/max/count are order-independent; a float sum is not
    "concurrent price agg": "SELECT min(price), max(price), count(price) FROM %T",
    "concurrent bloom eq": "SELECT count(*) FROM %T WHERE v = 900",
    "concurrent range": "SELECT count(*) FROM %T WHERE id BETWEEN 4000 AND 12000",
}


def _failed(exc):
    return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _value(dsn, sql):
    """-> the first column of the first row as text, or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return _failed(exc)
    return "" if row is None or row[0] is None else str(row[0])


def _workload(lo, hi):
    body = (f"SELECT g, g%5, g*3, md5(g::text), ((g*37)%100000)*0.01 "
            f"FROM generate_series({lo},{hi}) g")
    return (f"INSERT INTO t_heap {body}; INSERT INTO t_col {body}; "
            f"DELETE FROM t_heap WHERE id BETWEEN {lo} AND {hi} AND id % 7 = 0; "
            f"DELETE FROM t_col WHERE id BETWEEN {lo} AND {hi} AND id % 7 = 0; "
            f"UPDATE t_heap SET v = v + 1 WHERE id BETWEEN {lo} AND {hi} AND id % 5 = 0; "
            f"UPDATE t_col SET v = v + 1 WHERE id BETWEEN {lo} AND {hi} AND id % 5 = 0")


@pytest.fixture(scope="module")
def cdf(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        defs = "id int, cat int, v bigint, txt text, price float8"
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            c.execute(f"CREATE TABLE t_heap ({defs}) USING heap")
            c.execute(f"CREATE TABLE t_col ({defs}) USING pgcolumnar")
            c.execute("SELECT pgcolumnar.set_options('t_col', chunk_group_row_limit => 500, "
                      "stripe_row_limit => 2000)")
        starts, ends, errors = [None] * WORKERS, [None] * WORKERS, [None] * WORKERS

        def worker(w):
            lo, hi = w * PER + 1, (w + 1) * PER
            starts[w] = time.monotonic_ns()
            try:
                with psycopg.connect(dsn, autocommit=True) as conn:
                    # no parameters: one simple-protocol query, one implicit transaction
                    conn.execute(_workload(lo, hi).encode())
            except psycopg.Error as exc:
                errors[w] = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            ends[w] = time.monotonic_ns()

        threads = [threading.Thread(target=worker, args=(w,)) for w in range(WORKERS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        failed = [f"worker {w}: {e}" for w, e in enumerate(errors) if e]
        r = {
            "workers": "0" if not failed else "; ".join(failed),
            "overlap": ("no timing for every worker" if None in starts or None in ends
                        else "yes" if max(starts) < min(ends)
                        else "no (the last start came after the first end)"),
            "diffs": {name: (_value(dsn, SET_HASH.format(q=t.replace("%T", "t_col"))),
                             _value(dsn, SET_HASH.format(q=t.replace("%T", "t_heap"))))
                      for name, t in DIFFS.items()},
            "count": _value(dsn, "SELECT count(*) FROM t_col"),
        }
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_all_workers_succeeded(cdf, expect):
    expect.text(cdf["workers"], "0", "all workers succeeded")


def test_every_worker_started_before_any_finished(cdf, expect):
    expect.text(cdf["overlap"], "yes", "premise: every worker started before any worker finished")


def test_the_whole_row_agrees(cdf, expect):
    expect.text(*cdf["diffs"]["concurrent whole-row"], "concurrent whole-row")


def test_count_and_sum_agree(cdf, expect):
    expect.text(*cdf["diffs"]["concurrent count/sum"], "concurrent count/sum")


def test_the_price_aggregates_agree(cdf, expect):
    expect.text(*cdf["diffs"]["concurrent price agg"], "concurrent price agg")


def test_the_bloom_equality_agrees(cdf, expect):
    expect.text(*cdf["diffs"]["concurrent bloom eq"], "concurrent bloom eq")


def test_the_range_agrees(cdf, expect):
    expect.text(*cdf["diffs"]["concurrent range"], "concurrent range")


def test_the_row_count_is_sane(cdf, expect):
    c = cdf["count"]
    expect.text(c if not c.isdigit() else "ok" if int(c) > 0 else f"empty ({c})", "ok", "row count sane")
