"""A parallel columnar scan returns what the heap oracle returns. The pytest twin of
`test/parallel.sh`, every check by the shell's name (#1341).

The planner settings force a parallel plan, so several backends claim distinct row
groups from one shared counter. Asserted: the table really has row groups to share,
the planner puts a Gather over a parallel PgColumnarScan, every "par" query really
plans one, and its rows match the heap. Then the same queries, forced serial, plan no
Gather and still match.

EVERY READING IS A MEASUREMENT. A failed query is a query_error(), so two failures
never compare equal, and a failed EXPLAIN counts for neither plan reading. The heap
is loaded from the generator and the columnar table from the heap, as the shell does.

ON A PRIVATE DATABASE, the planner settings set on it as the shell does, built by a
module fixture; each test asserts one reading, so one red cannot stop the next.
"""
import re

import psycopg
import pytest

from pgc_vacuity import QUERY_ERROR, query_error

DB = "pgc_parallel"
SET_HASH = ("SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), $e$EMPTY$e$) "
            "FROM (SELECT _row::text AS t FROM ( {q} ) _row) _s")
# The shell's queries, named once, so the plan readings read the queries the
# comparisons ran.
Q_FILTERED = "SELECT id, v, t FROM %T WHERE k = 7"
Q_RANGE = "SELECT id FROM %T WHERE k < 30"
Q_SAMPLE = "SELECT * FROM %T WHERE id % 997 = 0"
Q_COUNT = "SELECT count(*), sum(v) FROM %T WHERE k < 50"
# Row-returning: a bare count(*) is answered by the vectorized aggregate, which plans
# no Gather, so it never reached the parallel scan.
Q_NOFILTER = "SELECT id FROM %T"
PAR = {
    "par filtered scan": Q_FILTERED,
    "par range scan": Q_RANGE,
    "par whole sample": Q_SAMPLE,
    "par count": Q_COUNT,
    "par no-filter": Q_NOFILTER,
}
SERIAL = {
    "serial filtered scan": Q_FILTERED,
    "serial range scan": Q_RANGE,
    "serial count": Q_COUNT,
}


def _failed(exc):
    return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


class _Db:
    def __init__(self, dsn):
        self.dsn = dsn

    def run(self, sql):
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                conn.execute(sql)
        except psycopg.Error as exc:
            print(f"-- step failed: {str(exc).splitlines()[0]}")

    def value(self, sql):
        """-> the first column of the first row as text, or a query_error()."""
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                row = conn.execute(sql).fetchone()
        except psycopg.Error as exc:
            return _failed(exc)
        if row is None or row[0] is None:
            return query_error("no row")
        return row[0]

    def plan(self, sql):
        """-> the EXPLAIN (COSTS off) text, or a query_error()."""
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                return "\n".join(r[0] for r in conn.execute(f"EXPLAIN (COSTS off) {sql}"))
        except psycopg.Error as exc:
            return _failed(exc)

    def diff(self, tmpl):
        """-> (columnar hash, heap hash), each a query_error() when its query failed."""
        return (self.value(SET_HASH.format(q=tmpl.replace("%T", "t_col"))),
                self.value(SET_HASH.format(q=tmpl.replace("%T", "t_heap"))))

    def plans_with(self, gather, tmpls):
        """-> how many of the queries, on t_col, plan a PgColumnarScan with (or without)
        a Gather. A failed EXPLAIN counts for neither."""
        n = 0
        for tmpl in tmpls:
            p = self.plan(tmpl.replace("%T", "t_col"))
            if p.startswith(QUERY_ERROR) or "Custom Scan (PgColumnarScan)" not in p:
                continue
            if ("Gather" in p) == gather:
                n += 1
        return n


def _lines(plan, pattern, flags=0):
    """-> how many plan lines match, as `grep -c` counts them, or the query_error()."""
    if plan.startswith(QUERY_ERROR):
        return plan
    return sum(1 for line in plan.splitlines() if re.search(pattern, line, flags))


@pytest.fixture(scope="module")
def par(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        d = _Db(pgc_cluster.dsn(dbname=DB))
        d.run("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        d.run("CREATE TABLE t_heap (id int, k int, v bigint, t text) USING heap")
        d.run("CREATE TABLE t_col (id int, k int, v bigint, t text) USING pgcolumnar")
        d.run("SELECT pgcolumnar.set_options('t_col', chunk_group_row_limit => 1000, "
              "stripe_row_limit => 2000)")
        d.run("INSERT INTO t_heap SELECT g, g%100, g::bigint*2, 'r'||g FROM generate_series(1,50000) g")
        d.run("INSERT INTO t_col SELECT * FROM t_heap")
        r = {"row groups": d.value("SELECT count(*) FROM pgcolumnar.row_group "
                                   "WHERE storage_id = pgcolumnar.get_storage_id('t_col')")}
        for guc in ("max_parallel_workers_per_gather = 4", "parallel_setup_cost = 0",
                    "parallel_tuple_cost = 0", "min_parallel_table_scan_size = 0"):
            d.run(f"ALTER DATABASE {DB} SET {guc}")
        plan = d.plan("SELECT id, v FROM t_col WHERE k = 5")
        r["gather lines"] = _lines(plan, r"gather", re.IGNORECASE)
        r["scan lines"] = _lines(plan, r"Custom Scan \(PgColumnarScan\)")
        r["diffs"] = {name: d.diff(t) for name, t in PAR.items()}
        r["par plans"] = d.plans_with(True, PAR.values())
        d.run(f"ALTER DATABASE {DB} SET max_parallel_workers_per_gather = 0")
        r["diffs"].update({name: d.diff(t) for name, t in SERIAL.items()})
        r["serial plans"] = d.plans_with(False, SERIAL.values())
        print(f"row groups={r['row groups']} par plans={r['par plans']} serial plans={r['serial plans']}")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


# Not a premise (#1339): the writer honoring stripe_row_limit is pgcolumnar's own
# work, and a writer that ignores it writes 1 group and reddens this.
def test_parallel_table_has_row_groups_to_claim(par, expect):
    expect.num(par["row groups"], 25, "par table has 25 row groups to claim")


def test_parallel_plan_has_a_gather(par, expect):
    expect.num(par["gather lines"], 1, "parallel plan has Gather")


def test_parallel_plan_has_the_columnar_scan(par, expect):
    expect.num(par["scan lines"], 1, "parallel plan has PgColumnarScan")


def test_parallel_filtered_scan_matches_the_heap(par, expect):
    expect.text(*par["diffs"]["par filtered scan"], "par filtered scan")


def test_parallel_range_scan_matches_the_heap(par, expect):
    expect.text(*par["diffs"]["par range scan"], "par range scan")


def test_parallel_whole_row_sample_matches_the_heap(par, expect):
    expect.text(*par["diffs"]["par whole sample"], "par whole sample")


def test_parallel_count_and_sum_match_the_heap(par, expect):
    expect.text(*par["diffs"]["par count"], "par count")


def test_parallel_unfiltered_scan_matches_the_heap(par, expect):
    expect.text(*par["diffs"]["par no-filter"], "par no-filter")


def test_parallel_every_par_query_plans_a_gather(par, expect):
    expect.num(par["par plans"], 5, "par queries all plan a Gather")


def test_serial_filtered_scan_matches_the_heap(par, expect):
    expect.text(*par["diffs"]["serial filtered scan"], "serial filtered scan")


def test_serial_range_scan_matches_the_heap(par, expect):
    expect.text(*par["diffs"]["serial range scan"], "serial range scan")


def test_serial_count_and_sum_match_the_heap(par, expect):
    expect.text(*par["diffs"]["serial count"], "serial count")


def test_serial_queries_plan_no_gather(par, expect):
    expect.num(par["serial plans"], 3, "serial queries plan no Gather")
