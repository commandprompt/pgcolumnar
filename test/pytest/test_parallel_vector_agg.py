"""The parallel-aware vectorized aggregates fold correctly (#289 phase 5/6, #349). The
pytest twin of `test/parallel_vector_agg.sh`, every check by the shell's name (#1341).

With pgcolumnar.enable_parallel_vector_agg on, the ungrouped batch fold plans as a
parallel partial PgColumnarAgg under a core Gather and Finalize Aggregate, and the
grouped fold as a parallel partial grouped node under a Finalize HashAggregate. Each
worker claims distinct row groups through the shared counter. Asserted: both plans are
chosen and workers launch; count and integer sums equal a serial oracle exactly;
float sums and averages equal core's own PARALLEL aggregate (within 1e-6 relative for
the ungrouped node, to six decimals for the grouped one); the all-null, empty,
more-workers-than-groups and not-batch-foldable cases; committed deletes; deletes made
earlier in the same transaction (H2), for both nodes; and no backend died.

FIXES THE SHELL SHARES. Every comparison of two readings refuses a failed reading: a
failed query is a query_error(), so two failed arms never agree. The H2 sessions need
both SELECTs to answer. The grouped oracles are core's grouped aggregate
(pgcolumnar.enable_group_vectorization off), not pgcolumnar's own grouped fold run
serially. And "server still up" also needs the server log's count of killed backends
not to have moved since setup, since a fresh connection answers after crash recovery.

ON A PRIVATE DATABASE, built in the shell's order by a module fixture that takes every
reading.
"""
import re

import psycopg
import pytest
from psycopg.types.string import TextLoader

from pgc_vacuity import query_error

DB = "pgc_parallel_vector_agg"
# Every query_error() a reading returned, so a check derived from a failed reading
# (a plan grep) hands the failure on rather than reading it as "no match".
_FAILED = set()

# Force parallel plans; both arms then differ only in the aggregate node.
PAR = ("SET max_parallel_workers_per_gather=4",
       "SET parallel_setup_cost=0", "SET parallel_tuple_cost=0",
       "SET min_parallel_table_scan_size=0", "SET min_parallel_index_scan_size=0")
UG = ("SET pgcolumnar.enable_ungrouped_vector_agg=on",)
PP = ("SET pgcolumnar.enable_parallel_vector_agg=on",)
VEC = PAR + UG + PP
SER = ("SET max_parallel_workers_per_gather=0",)
GVP = ("SET pgcolumnar.enable_group_vectorization=on",
       "SET pgcolumnar.enable_parallel_vector_agg=on")
GPAR = PAR + GVP
# The grouped oracles are core's grouped aggregate, not pgcolumnar's fold run serially.
GSER = SER + ("SET pgcolumnar.enable_group_vectorization=off",)
GCORE = PAR + ("SET pgcolumnar.enable_group_vectorization=off",)

T_SQL = """
DROP TABLE IF EXISTS t;
CREATE TABLE t (id int, k int, v float8, w float4) USING pgcolumnar;
SELECT pgcolumnar.set_options('t'::regclass, stripe_row_limit => 40000);
INSERT INTO t
SELECT g, g % 1000,
       CASE WHEN g % 50 = 0 THEN NULL
            ELSE ((g % 13) - 6)::float8 * (10.0 ^ (g % 4)) END,
       ((g % 7) - 3)::float4
FROM generate_series(1, 2000000) g;
ANALYZE t;
"""
TINY_SQL = """
DROP TABLE IF EXISTS tiny;
CREATE TABLE tiny (v float8) USING pgcolumnar;
SELECT pgcolumnar.set_options('tiny'::regclass, stripe_row_limit => 1000);
INSERT INTO tiny SELECT g::float8 FROM generate_series(1,2500) g; ANALYZE tiny;
"""
GT_SQL = """
DROP TABLE IF EXISTS gt;
CREATE TABLE gt (id int, h text, k int, v float8, w int) USING pgcolumnar;
SELECT pgcolumnar.set_options('gt'::regclass, stripe_row_limit => 20000);
INSERT INTO gt
SELECT g, 'h' || (g % 50), g % 200,
       CASE WHEN g % 50 = 0 THEN NULL
            ELSE ((g % 13) - 6)::float8 * (10.0 ^ (g % 4)) END,
       g % 13
FROM generate_series(1, 800000) g;
ANALYZE gt;
"""


def _first(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _fail(exc):
    e = query_error(_first(exc))
    _FAILED.add(e)
    return e


def _crashes(dbdir):
    try:
        text = (dbdir / "server.log").read_text(errors="replace")
    except OSError as exc:
        return f"server log unreadable: {exc}"
    return sum(1 for line in text.splitlines() if "terminated by signal" in line)


class _Db:
    def __init__(self, dsn):
        self.dsn = dsn

    def _connect(self, autocommit=True):
        conn = psycopg.connect(self.dsn, autocommit=autocommit, connect_timeout=10)
        # Read every value as the server's text, as psql -At prints it.
        for t in ("int2", "int4", "int8", "float4", "float8", "numeric", "bool"):
            conn.adapters.register_loader(t, TextLoader)
        return conn

    def run(self, sql):
        try:
            with self._connect() as conn:
                conn.execute(sql)
        except psycopg.Error as exc:
            print(f"-- step failed: {_first(exc)}")

    def read(self, sets, sql):
        """-> (True, rows as psql -At prints them) or (False, query_error())."""
        try:
            with self._connect() as conn:
                for s in sets:
                    conn.execute(s)
                rows = conn.execute(sql).fetchall()
        except psycopg.Error as exc:
            return False, _fail(exc)
        return True, "\n".join("|".join("" if c is None else c for c in r) for r in rows)

    def q(self, sets, sql):
        return self.read(sets, sql)[1]

    def reldiff(self, expr):
        """The shell's reldiff: |vec-core|/(|core|+1) <= 1e-6, decided in SQL."""
        ok, vec = self.read(VEC, f"SELECT {expr} FROM t WHERE k < 700")
        if not ok:
            return vec
        ok, core = self.read(PAR + ("SET pgcolumnar.enable_ungrouped_vector_agg=off",),
                             f"SELECT {expr} FROM t WHERE k < 700")
        if not ok:
            return core
        return self.q((), "SELECT CASE WHEN abs((%s::float8)-(%s::float8)) <= 1e-6*(abs(%s::float8)+1) "
                          f"THEN 'ok' ELSE 'DIFF vec={vec} core={core}' END" % (
                              f"'{vec}'", f"'{core}'", f"'{core}'"))

    def h2(self, delete, par_sets, ser_sets, sql):
        """Delete, then the parallel and the serial reading, in ONE transaction that
        is rolled back. -> (par, ser); both are query_error()s when either failed."""
        try:
            with self._connect(autocommit=False) as conn:
                conn.execute(delete)
                for s in par_sets:
                    conn.execute(s)
                par = conn.execute(sql).fetchall()
                for s in ser_sets:
                    conn.execute(s)
                ser = conn.execute(sql).fetchall()
                conn.rollback()
        except psycopg.Error as exc:
            return _fail(exc), _fail(exc)

        def fmt(rows):
            return "\n".join("|".join("" if c is None else c for c in r) for r in rows)
        return fmt(par), fmt(ser)


@pytest.fixture(scope="module")
def pva(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        d = _Db(pgc_cluster.dsn(dbname=DB))
        d.run("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r = {"crashes before": _crashes(pgc_cluster.datadir)}
        d.run(T_SQL)
        r["plan"] = d.q(VEC, "EXPLAIN (COSTS OFF) SELECT count(*), avg(v), sum(v) FROM t WHERE k < 700")
        r["cnt vec"] = d.q(VEC, "SELECT count(*) FROM t WHERE k < 700")
        r["cnt ser"] = d.q(SER, "SELECT count(*) FROM t WHERE k < 700")
        r["avg v"] = d.reldiff("avg(v)")
        r["sum v"] = d.reldiff("sum(v)")
        r["avg w"] = d.reldiff("avg(w)::float8")
        r["plan i"] = d.q(VEC, "EXPLAIN (COSTS OFF) SELECT sum(k), avg(k) FROM t WHERE k < 700")
        r["ik vec"] = d.q(VEC, "SELECT sum(k), avg(k) FROM t WHERE k < 700")
        r["ik ser"] = d.q(SER, "SELECT sum(k), avg(k) FROM t WHERE k < 700")
        r["an avg"] = d.q(VEC, "SELECT coalesce(avg(v)::text,'NULLZ') FROM t WHERE k < 700 AND v IS NULL")
        r["an cnt"] = d.q(VEC, "SELECT count(*) FROM t WHERE k < 700 AND v IS NULL")
        r["an cnt2"] = d.q(SER, "SELECT count(*) FROM t WHERE k<700 AND v IS NULL")
        r["empty cnt"] = d.q(VEC, "SELECT count(*) FROM t WHERE k < 0")
        r["empty avg"] = d.q(VEC, "SELECT coalesce(avg(v)::text,'NULLZ') FROM t WHERE k < 0")
        d.run(TINY_SQL)
        r["tw vec"] = d.q(("SET max_parallel_workers_per_gather=8", "SET parallel_setup_cost=0",
                           "SET parallel_tuple_cost=0", "SET min_parallel_table_scan_size=0") + UG + PP,
                          "SELECT count(*), sum(v) FROM tiny")
        r["tw ser"] = d.q(SER, "SELECT count(*), sum(v) FROM tiny")
        d.run("DELETE FROM t WHERE k IN (1,2,3)")
        r["d vec"] = d.q(VEC, "SELECT count(*), sum(v) FROM t WHERE k < 700")
        r["d ser"] = d.q(SER, "SELECT count(*), sum(v) FROM t WHERE k < 700")
        r["h2 par"], r["h2 ser"] = d.h2("DELETE FROM t WHERE k IN (10,11,12,13,14)", VEC, SER,
                                        "SELECT count(*), sum(v) FROM t WHERE k < 700")
        d.run(GT_SQL)
        r["gt rows"] = d.q((), "SELECT count(*) FROM gt")
        r["gea"] = d.q(GPAR, "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) "
                             "SELECT h, count(*), sum(w) FROM gt GROUP BY h")
        r["groups"] = d.q(GPAR, "SELECT count(*) FROM (SELECT h, count(*), sum(w) FROM gt GROUP BY h) s")
        g = "SELECT h, count(*), sum(w) FROM gt WHERE k < 150 GROUP BY h ORDER BY h"
        r["g vec"], r["g ser"] = d.q(GPAR, g), d.q(GSER, g)
        gm = "SELECT h, k, count(*), sum(w) FROM gt WHERE v IS NOT NULL GROUP BY h, k ORDER BY h, k"
        r["gm vec"], r["gm ser"] = d.q(GPAR, gm), d.q(GSER, gm)
        gf = "SELECT h, round(avg(v)::numeric,6), round(sum(v)::numeric,6) FROM gt GROUP BY h ORDER BY h"
        r["gf vec"], r["gf par"] = d.q(GPAR, gf), d.q(GCORE, gf)
        gw = "SELECT k % 3 AS g, count(*), sum(w) FROM gt GROUP BY 1 ORDER BY 1"
        r["gw vec"] = d.q(GPAR + ("SET max_parallel_workers_per_gather=8",), gw)
        r["gw ser"] = d.q(GSER, gw)
        r["gh2 par"], r["gh2 ser"] = d.h2("DELETE FROM gt WHERE k IN (20,21,22,23,24)", GPAR, GSER,
                                          "SELECT h, count(*), sum(w) FROM gt GROUP BY h ORDER BY h")
        ok, up = d.read((), "SELECT 1")
        now = _crashes(pgc_cluster.datadir)
        before = r["crashes before"]
        if not (isinstance(before, int) and isinstance(now, int)):
            r["up"] = "no (crash count unreadable)"
        elif ok and up == "1" and now == before:
            r["up"] = "1"
        else:
            r["up"] = f"no ({'answers' if ok else 'no answer'}, {now - before} backend crashes)"
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _has(plan, pattern):
    """-> "y" when the plan text matches, "n" when it does not; a failed EXPLAIN
    stays a query_error() so the test refuses it by name."""
    if plan in _FAILED:
        return plan
    return "y" if re.search(pattern, plan, re.I) else "n"


# ---- the ungrouped plan ------------------------------------------------------
# Not premises (#1339): never adding the parallel partial path reddens the first three.

def test_pva_finalize_aggregate_present(pva, expect):
    expect.text(_has(pva["plan"], "Finalize Aggregate"), "y", "Finalize Aggregate present")


def test_pva_gather_present(pva, expect):
    expect.text(_has(pva["plan"], "Gather"), "y", "Gather present")


def test_pva_the_partial_columnar_agg_node_present(pva, expect):
    p = pva["plan"]
    got = p if p in _FAILED else (
        "y" if _has(p, re.escape("Parallel Custom Scan (PgColumnarScan)")) == "y"
        and _has(p, "Columnar Vectorized Aggregates") == "y" else "n")
    expect.text(got, "y", "the partial columnar agg node present")


# Not a premise (#1339): a batch-fold shape gate that always refuses reddens it.
def test_pva_the_partial_runs_the_batch_fold(pva, expect):
    expect.text(_has(pva["plan"], "Batch Fold: yes"), "y", "the partial runs the batch fold")


# ---- ungrouped values ----------------------------------------------------------

def test_pva_count_equals_the_serial_oracle(pva, expect):
    expect.text(pva["cnt vec"], pva["cnt ser"], "count(*) parallel-vec == serial oracle")


def test_pva_avg_float8_matches_core_parallel(pva, expect):
    expect.text(pva["avg v"], "ok", "avg(v) parallel-vec ~= core parallel agg")


def test_pva_sum_float8_matches_core_parallel(pva, expect):
    expect.text(pva["sum v"], "ok", "sum(v) parallel-vec ~= core parallel agg")


def test_pva_avg_float4_matches_core_parallel(pva, expect):
    expect.text(pva["avg w"], "ok", "avg(w::float8) f4 ~= core parallel agg")


def test_pva_int_sum_avg_takes_the_parallel_fold(pva, expect):
    p = pva["plan i"]
    got = p if p in _FAILED else (
        "y" if _has(p, "Gather") == "y" and _has(p, "Batch Fold: yes") == "y" else "n")
    expect.text(got, "y", "int sum/avg takes the parallel fold")


def test_pva_int_sum_avg_equal_serial_exactly(pva, expect):
    expect.text(pva["ik vec"], pva["ik ser"], "sum(k)+avg(k) int: parallel fold == serial (exact)")


# ---- edge cases ------------------------------------------------------------------

def test_pva_not_batch_foldable_all_null_avg(pva, expect):
    expect.text(pva["an avg"], "NULLZ", "not-batch-foldable filter: all-null avg is NULL")


def test_pva_not_batch_foldable_count_exact(pva, expect):
    expect.text(pva["an cnt"], pva["an cnt2"], "not-batch-foldable filter: count(*) exact")


def test_pva_empty_count_is_zero(pva, expect):
    expect.text(pva["empty cnt"], "0", "empty count(*) == 0")


def test_pva_empty_avg_is_null(pva, expect):
    expect.text(pva["empty avg"], "NULLZ", "empty avg is NULL")


def test_pva_more_workers_than_groups_exact(pva, expect):
    expect.text(pva["tw vec"], pva["tw ser"], "more-workers-than-groups count+sum exact")


# ---- deletes ------------------------------------------------------------------------

def test_pva_committed_deletes_equal_serial(pva, expect):
    expect.text(pva["d vec"], pva["d ser"], "committed deletes: parallel fold == serial")


def test_pva_in_xact_deletes_equal_serial(pva, expect):
    expect.text(pva["h2 par"], pva["h2 ser"], "in-xact deletes (H2): parallel fold == serial")


# ---- the grouped node (#349) ------------------------------------------------------
# Not premises (#1339): an insert that drops a row reddens the fixture's count, and
# never adding the grouped parallel partial path reddens the three plan checks.

def test_pva_the_grouped_fixture_has_its_rows(pva, expect):
    expect.text(pva["gt rows"], "800000", "the grouped fixture has its rows")


def test_pva_grouped_finalize_hashaggregate_present(pva, expect):
    expect.text(_has(pva["gea"], "Finalize HashAggregate"), "y",
                "grouped Finalize HashAggregate present (#349)")


def test_pva_partial_grouped_node_under_the_gather(pva, expect):
    g = pva["gea"]
    got = g if g in _FAILED else (
        "y" if _has(g, re.escape("Parallel Custom Scan (PgColumnarScan)")) == "y"
        and _has(g, "Columnar Vectorized Group Keys") == "y" else "n")
    expect.text(got, "y",
                "the partial grouped node is under the Gather (#349)")


def test_pva_workers_launched_for_our_grouped_node(pva, expect):
    g = pva["gea"]
    got = g if g in _FAILED else (
        "y" if _has(g, "Workers Launched: [1-9]") == "y"
        and _has(g, "Columnar Vectorized Group Keys") == "y" else "n")
    expect.text(got, "y",
                "workers actually launched for OUR grouped node (#349)")


def test_pva_gather_carries_per_worker_partials(pva, expect):
    g = pva["gea"]
    rows = [m.group(1) for line in g.splitlines() if re.match(r"^ *-> *Gather", line, re.I)
            for m in [re.search(r"rows=([0-9]+)", line)] if m]
    got = g if g in _FAILED else (
        "y" if rows and int(rows[0]) > 50 else f"n (gather rows={rows[0] if rows else 'unset'})")
    expect.text(got, "y",
                "the Gather carries per-worker partials, more than one row per group (#349)")


def test_pva_finalize_collapses_to_the_group_count(pva, expect):
    expect.text(pva["groups"], "50", "the Finalize collapses them back to exactly the group count (#349)")


def test_pva_grouped_int_equals_core_serial(pva, expect):
    expect.text(pva["g vec"], pva["g ser"], "grouped count+sum(int): parallel fold == serial oracle (#349)")


def test_pva_grouped_multikey_where_equals_core_serial(pva, expect):
    expect.text(pva["gm vec"], pva["gm ser"],
                "grouped multi-key + WHERE: parallel fold == serial oracle (#349)")


def test_pva_grouped_float_equals_core_parallel(pva, expect):
    expect.text(pva["gf vec"], pva["gf par"],
                "grouped avg/sum(float8): parallel fold == core parallel agg (#349)")


def test_pva_grouped_few_groups_many_workers(pva, expect):
    expect.text(pva["gw vec"], pva["gw ser"], "grouped few-groups-many-workers: parallel == serial (#349)")


def test_pva_grouped_in_xact_deletes_equal_core_serial(pva, expect):
    expect.text(pva["gh2 par"], pva["gh2 ser"],
                "grouped in-xact deletes (H2): parallel fold == serial (#349)")


def test_pva_server_still_up(pva, expect):
    expect.text(pva["up"], "1", "server still up")
