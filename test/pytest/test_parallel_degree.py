"""Identical data in a heap and a columnar table is granted the same parallel degree
(#451). The pytest twin of `test/parallel_degree.sh`, every check by the shell's name
(#1341).

compute_parallel_worker() sizes a scan from relpages on a log3 ladder. Columnar
relpages count compressed pages, so the same rows would get fewer workers the better
they compress; the custom scan sizes itself from the uncompressed estimate instead.
Asserted: both arms hold the same rows, columnar really is smaller on disk, both plans
are parallel, the worker cap decides neither arm, and the two degrees are equal.

EVERY READING MUST BE A MEASUREMENT. With the server gone after ANALYZE, the shell's
old form passed three checks: the counts ("" = ""), the cap ("" read as 0) and the
degree ("none" = "none"). Here a failed reading is a query_error(), and a plan with no
Workers Planned line is "", which neither expect.num nor the cap check accepts.

ON A PRIVATE DATABASE, the planner settings set on it as the shell does, built by a
module fixture.
"""
import re

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_parallel_degree"
MAXW = 8


class _Db:
    def __init__(self, dsn):
        self.dsn = dsn

    def run(self, sql):
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                conn.execute(sql)
        except psycopg.Error as exc:
            print(f"-- step failed: {str(exc).splitlines()[0]}")

    def num(self, sql):
        """-> the first column as an int, or a query_error()."""
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                row = conn.execute(sql).fetchone()
        except psycopg.Error as exc:
            return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
        if row is None or row[0] is None:
            return query_error("no row")
        return int(row[0])

    def workers(self, table):
        """-> the first Workers Planned count in the plan, "" when there is none, or a
        query_error(). The count EXPLAIN reports is what the planner decided."""
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                plan = "\n".join(r[0] for r in conn.execute(
                    f"EXPLAIN (COSTS OFF) SELECT count(*) FROM {table} WHERE k = 5"))
        except psycopg.Error as exc:
            return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
        m = re.search(r"Workers Planned: ([0-9]+)", plan)
        return int(m.group(1)) if m else ""


def _smaller(ph, pc):
    ok = isinstance(ph, int) and isinstance(pc, int) and pc > 0 and ph > pc
    return "yes" if ok else f"no (heap={ph} col={pc})"


def _parallel(w):
    return "yes" if isinstance(w, int) else "no (no Workers Planned line)"


def _cap(wh, wc):
    if not isinstance(wh, int) or not isinstance(wc, int):
        return f"no reading (heap=[{wh}] col=[{wc}])"
    return "yes" if wh < MAXW and wc < MAXW else f"no (cap={MAXW} heap={wh} col={wc})"


@pytest.fixture(scope="module")
def pd(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        d = _Db(pgc_cluster.dsn(dbname=DB))
        d.run("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        # A few-fold compression ratio: one low-cardinality column that compresses,
        # unique text and bigint that do not (the shell explains the first fixture).
        d.run("CREATE TABLE t_heap (id int, k int, v bigint, t text) USING heap")
        d.run("CREATE TABLE t_col (id int, k int, v bigint, t text) USING pgcolumnar")
        d.run("INSERT INTO t_heap SELECT g, g%50, g::bigint * 7919, 'row_' || g || '_' || md5(g::text) "
              "FROM generate_series(1,400000) g")
        d.run("INSERT INTO t_col SELECT * FROM t_heap")
        # Set small, not zeroed: the ladder reading relpages is the subject.
        for guc, val in (("max_parallel_workers_per_gather", MAXW), ("parallel_setup_cost", 0),
                         ("parallel_tuple_cost", 0), ("min_parallel_table_scan_size", "'1MB'")):
            d.run(f"ALTER DATABASE {DB} SET {guc} = {val}")
        d.run("ANALYZE t_heap")
        d.run("ANALYZE t_col")
        r = {
            "heap rows": d.num("SELECT count(*) FROM t_heap"),
            "col rows": d.num("SELECT count(*) FROM t_col"),
            "ph": d.num("SELECT relpages FROM pg_class WHERE relname = 't_heap'"),
            "pc": d.num("SELECT relpages FROM pg_class WHERE relname = 't_col'"),
            "wh": d.workers("t_heap"),
            "wc": d.workers("t_col"),
        }
        print(f"heap: relpages={r['ph']} workers={r['wh']}   columnar: relpages={r['pc']} workers={r['wc']}")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


# Not premises (#1339): the rows, the columnar partial path and the columnar degree
# are pgcolumnar's own work, and a one-line change reddens each of the three.
def test_degree_heap_and_columnar_hold_the_same_rows(pd, expect):
    expect.num(pd["heap rows"], pd["col rows"], "both tables hold the same rows")


def test_degree_columnar_is_smaller_on_disk(pd, expect):
    expect.text(_smaller(pd["ph"], pd["pc"]), "yes", "premise: the columnar table really is smaller on disk")


def test_degree_the_heap_plan_is_parallel(pd, expect):
    expect.text(_parallel(pd["wh"]), "yes", "premise: the heap plan is parallel")


def test_degree_the_columnar_plan_is_parallel(pd, expect):
    expect.text(_parallel(pd["wc"]), "yes", "the columnar plan is parallel")


def test_degree_the_worker_cap_decides_neither_arm(pd, expect):
    expect.text(_cap(pd["wh"], pd["wc"]), "yes", "the worker cap is not what decides either arm")


def test_degree_identical_data_gets_the_same_workers(pd, expect):
    expect.num(pd["wc"], pd["wh"], "identical data is granted the same parallel degree")
