"""Rescanning a columnar aggregate does not grow query memory (#717, #727, #734). The
pytest twin of `test/vector_agg_rescan_memory.sh`, every check by the shell's name
(#1341).

A LATERAL or parameterized sub-scan re-executes its node once per outer row, and
anything the node allocates per rescan into query memory lives until ExecutorEnd.
Three such growths were fixed: pushdown scan keys rebuilt per rescan, with an IN-list's
array detoasted and deconstructed each time (#717); the vectorized aggregate's own
per-rescan arrays (#727); and the plain columnar scan's row-group list (#734). Each claim
is a DIFFERENTIAL, not a bound: the growth per rescan is a slope across two rescan
counts, and it is compared with a control that has everything but the thing under test
-- the same query without the IN-list, or the same query over a heap table.

THE INSTRUMENTS. For #717, the peak `ExecutorState` total while the query runs, read
from the server log: one connection runs the query in a thread, and a second calls
`pg_log_backend_memory_contexts()` on it every 0.1 s until it finishes. For #727 and
#734, the backend's peak RSS (`VmHWM` in /proc), which only rises, so it is read once
before and once after. The row path's pruning is read from EXPLAIN ANALYZE as work done.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order. A missing
reading is never 0: a claim reads "no reading" unless both of its slopes were measured,
so readers that measured nothing cannot pass as "no growth".
"""
import re
import threading
import time

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_vector_agg_rescan_memory"
NELEM, SMALL, LARGE = 20000, 200, 3200
RS_LO, RS_HI = 5000, 40000
GUC = ["SET pgcolumnar.enable_ungrouped_vector_agg = on", "SET enable_material = off"]
GUC_OFF = ["SET pgcolumnar.enable_ungrouped_vector_agg = off",
           "SET pgcolumnar.enable_group_vectorization = off", "SET enable_material = off"]
LATERAL = ("SELECT sum(s.c) FROM (SELECT i FROM vam_drv LIMIT {n}) d, "
           "LATERAL (SELECT count(*) c FROM {t} WHERE {where}v > d.i) s")


def _failed(exc):
    return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _value(dsn, sql, settings=()):
    """-> the first column of the first row, or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for s in settings:
                conn.execute(s)
            row = conn.execute(sql).fetchone()
            return None if row is None else row[0]
    except psycopg.Error as exc:
        return _failed(exc)


def _plan(dsn, sql, settings, analyze=False):
    """-> the plan text, or the failure as a query_error()."""
    opts = "ANALYZE, TIMING OFF, COSTS OFF, SUMMARY OFF" if analyze else "COSTS OFF"
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for s in settings:
                conn.execute(s)
            return "\n".join(r[0] for r in conn.execute(f"EXPLAIN ({opts}) {sql}"))
    except psycopg.Error as exc:
        return _failed(exc)


def _run(dsn, *sqls):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for sql in sqls:
                conn.execute(sql)
    except psycopg.Error as exc:
        print(f"-- step failed: {str(exc).splitlines()[0]}")


def _query(n, inlist=None):
    """The #717 query, driven by n rescans, with or without the IN-list."""
    return LATERAL.format(n=n, t="vam", where=f"k IN ({inlist}) AND " if inlist else "")


def _peak_executor_state(dsn, log, sql):
    """-> the largest "ExecutorState: N total" logged while `sql` ran, or why there is none.

    The path is asserted first, because every number is void without it: the vectorized
    aggregate, under a Nested Loop so it rescans, with no Materialize to replay a store."""
    plan = _plan(dsn, sql, GUC)
    if ("Columnar Vectorized Aggregates" not in plan or "Nested Loop" not in plan
            or "Materialize" in plan):
        return f"no reading: the plan is not a rescanned vectorized aggregate ({plan[:80]!r})"
    with psycopg.connect(dsn, autocommit=True) as runner, psycopg.connect(dsn, autocommit=True) as sampler:
        pid = runner.execute("SELECT pg_backend_pid()").fetchone()[0]
        for s in GUC:
            runner.execute(s)
        mark = log.stat().st_size
        failure = []

        def go():
            try:
                runner.execute(sql)
            except psycopg.Error as exc:
                failure.append(exc)
        t = threading.Thread(target=go)
        t.start()
        while t.is_alive():
            sampler.execute("SELECT pg_log_backend_memory_contexts(%s)", (pid,))
            time.sleep(0.1)
        t.join()
    if failure:
        return _failed(failure[0])
    with open(log, "rb") as f:
        f.seek(mark)
        totals = [int(m) for m in re.findall(rb"ExecutorState: (\d+) total", f.read())]
    return max(totals) if totals else "no reading: no ExecutorState line was logged"


def _rss_growth(dsn, table, n, settings):
    """-> the backend's peak RSS growth, in kB, across one run of the rescan query."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            pid = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
            for s in settings:
                conn.execute(s)
            hwm = lambda: int(re.search(r"VmHWM:\s+(\d+)", open(f"/proc/{pid}/status").read()).group(1))
            base = hwm()
            conn.execute(LATERAL.format(n=n, t=table, where=""))
            return hwm() - base
    except (psycopg.Error, OSError, AttributeError) as exc:
        return f"no reading: {type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"


def _slope(hi, lo, rescans, scale=1):
    """-> bytes per rescan between two peaks, or None when either is not a number."""
    if not all(isinstance(v, int) for v in (hi, lo)):
        return None
    return int((hi - lo) * scale / rescans)


def _under(limit, a, b):
    """-> "yes" when both slopes were measured and a exceeds b by less than `limit`
    bytes per rescan, else what was wrong. A missing slope is "no reading", never 0."""
    if a is None or b is None:
        return f"no reading: [{a}] against [{b}]"
    return "yes" if a - b < limit else f"no: excess {a - b} B per rescan"


@pytest.fixture(scope="module")
def vam(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn, log = pgc_cluster.dsn(dbname=DB), pgc_cluster.datadir / "server.log"
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar",
             "CREATE TABLE vam (k int, v int) USING pgcolumnar",
             "INSERT INTO vam SELECT g % 5000, g FROM generate_series(1, 50000) g",
             "CREATE TABLE vam_drv (i int)",
             f"INSERT INTO vam_drv SELECT g FROM generate_series(1, {LARGE}) g",
             "ANALYZE vam", "ANALYZE vam_drv")
        r = {"loaded": _value(dsn, "SELECT count(*) FROM vam")}
        inlist = ",".join(str(g) for g in range(1, NELEM + 1))

        # ---- #717: the IN-list's scan keys ----
        keyed = _plan(dsn, _query(2, inlist), GUC, analyze=True)
        m = re.search(r"Columnar Pushed-Down Filters\D*(\d+)", keyed)
        r["keyed"] = int(m.group(1)) if m else f"no reading: {keyed[:120]!r}"
        for arm, n, il in (("with_small", SMALL, inlist), ("with_large", LARGE, inlist),
                           ("without_small", SMALL, None), ("without_large", LARGE, None)):
            r[arm] = _peak_executor_state(dsn, log, _query(n, il))
        r["with slope"] = _slope(r["with_large"], r["with_small"], LARGE - SMALL)
        r["without slope"] = _slope(r["without_large"], r["without_small"], LARGE - SMALL)
        print(f"-- #717 per-rescan growth: with the IN-list {r['with slope']} B, without {r['without slope']} B")

        # ---- #727 and #734: the node's own footprint, against a heap floor ----
        _run(dsn, f"INSERT INTO vam_drv SELECT g FROM generate_series({LARGE + 1}, {RS_HI}) g",
             "CREATE TABLE vam_rs (k int, v int) USING pgcolumnar",
             "INSERT INTO vam_rs SELECT g % 500, g FROM generate_series(1, 5000) g",
             "CREATE TABLE vam_heap (k int, v int)",
             "INSERT INTO vam_heap SELECT k, v FROM vam_rs",
             "ANALYZE vam_rs", "ANALYZE vam_heap")
        r["driver"] = _value(dsn, "SELECT count(*) FROM vam_drv")
        r["counts"] = (_value(dsn, f"SELECT count(*) FROM (SELECT i FROM vam_drv LIMIT {RS_HI}) a"),
                       _value(dsn, f"SELECT count(*) FROM (SELECT i FROM vam_drv LIMIT {RS_LO}) b"))
        r["mirror"] = (_value(dsn, "SELECT count(*) FROM vam_heap"), _value(dsn, "SELECT count(*) FROM vam_rs"))
        probe = LATERAL.format(n=100, t="vam_rs", where="")
        r["plan on"] = _plan(dsn, probe, GUC)
        r["plan off"] = _plan(dsn, probe, GUC_OFF)
        for key, table, settings in (("vec", "vam_rs", GUC), ("heap", "vam_heap", GUC),
                                     ("plain", "vam_rs", GUC_OFF), ("plain heap", "vam_heap", GUC_OFF)):
            r[key] = _slope(_rss_growth(dsn, table, RS_HI, settings), _rss_growth(dsn, table, RS_LO, settings),
                            RS_HI - RS_LO, 1024)
        print(f"-- per-rescan growth: vectorized {r['vec']} B, heap {r['heap']} B; "
              f"plain {r['plain']} B, heap {r['plain heap']} B")
        answer = LATERAL.format(n=200, t="{t}", where="")
        r["vec answer"] = (_value(dsn, answer.format(t="vam_rs"), GUC), _value(dsn, answer.format(t="vam_heap")))
        r["plain answer"] = (_value(dsn, answer.format(t="vam_rs"), GUC_OFF), _value(dsn, answer.format(t="vam_heap")))

        # ---- the row path's keys, pinned as work ----
        _run(dsn, "CREATE TABLE vam_prune (k int, s text) USING pgcolumnar",
             "SELECT pgcolumnar.set_options('vam_prune', stripe_row_limit => 10000)",
             "INSERT INTO vam_prune SELECT g, 'x' || g FROM generate_series(1, 200000) g")
        r["prune plan"] = _plan(dsn, "SELECT count(s) FROM vam_prune WHERE k > 190000", GUC, analyze=True)
        r["pruned answer"] = _value(dsn, "SELECT count(s) FROM vam_prune WHERE k > 190000")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _has(plan, marker):
    return plan if plan.startswith("QUERY_ERROR") else ("yes" if marker in plan else "no")


# ---- #717 ---------------------------------------------------------------------

def test_the_fixture_loaded(vam, expect):
    expect.num(vam["loaded"], 50000, "premise: the fixture loaded")


def test_the_in_list_becomes_pushdown_scan_keys(vam, expect):
    expect.at_least(vam["keyed"], 1, "premise: the IN-list really becomes pushdown scan keys")


@pytest.mark.parametrize("arm,name", [
    ("with_small", "premise: the with_small arm produced a reading"),
    ("with_large", "premise: the with_large arm produced a reading"),
    ("without_small", "premise: the without_small arm produced a reading"),
    ("without_large", "premise: the without_large arm produced a reading"),
])
def test_each_arm_produced_a_reading(vam, arm, name, expect):
    expect.at_least(vam[arm], 1, name)


def test_an_in_list_does_not_add_per_rescan_query_memory(vam, expect):
    expect.text(_under(20000, vam["with slope"], vam["without slope"]), "yes",
                "an IN-list does not add per-rescan query memory")


# ---- #727 ---------------------------------------------------------------------

def test_the_driver_has_enough_rows(vam, expect):
    expect.at_least(vam["driver"], RS_HI, "premise: the driver has enough rows for the larger rescan count")


def test_the_two_rescan_counts_differ(vam, expect):
    hi, lo = vam["counts"]
    expect.text(f"{hi} against {lo}", f"{RS_HI} against {RS_LO}", "premise: and the two rescan counts really differ")


def test_the_heap_mirror_has_the_same_rows(vam, expect):
    expect.num(*vam["mirror"], "premise: the heap mirror has the same rows")


def test_the_columnar_rescan_arm_is_the_vectorized_aggregate(vam, expect):
    expect.text(_has(vam["plan on"], "Columnar Vectorized Aggregates"), "yes",
                "premise: the columnar rescan arm is the vectorized aggregate")


def test_both_rescan_slopes_were_measured(vam, expect):
    expect.text(f"{vam['vec']} and {vam['heap']}" if None in (vam["vec"], vam["heap"]) else "measured",
                "measured", "premise: both rescan slopes were measured")


def test_the_vectorized_aggregate_does_not_grow_query_memory_per_rescan(vam, expect):
    expect.text(_under(100, vam["vec"], vam["heap"]), "yes",
                "the vectorized aggregate does not grow query memory per rescan (#727)")


def test_the_rescanned_aggregate_still_returns_the_heaps_answer(vam, expect):
    expect.num(*vam["vec answer"], "the rescanned aggregate still returns the heap's answer")


# ---- #734 ---------------------------------------------------------------------

def test_with_the_aggregate_off_the_arm_is_the_plain_columnar_scan(vam, expect):
    expect.text(_has(vam["plan off"], "Custom Scan (PgColumnarScan)"), "yes",
                "premise: with the aggregate off the arm is the plain columnar scan")


def test_and_it_is_not_the_vectorized_aggregate_node(vam, expect):
    expect.text(_has(vam["plan off"], "Columnar Vectorized Aggregates"), "no",
                "premise: and it is NOT the vectorized aggregate node")


def test_both_plain_scan_slopes_were_measured(vam, expect):
    expect.text(f"{vam['plain']} and {vam['plain heap']}" if None in (vam["plain"], vam["plain heap"])
                else "measured", "measured", "premise: both plain-scan slopes were measured")


def test_the_plain_columnar_scan_does_not_grow_query_memory_per_rescan(vam, expect):
    expect.text(_under(100, vam["plain"], vam["plain heap"]), "yes",
                "the plain columnar scan does not grow query memory per rescan (#734)")


def test_the_rescanned_plain_scan_still_returns_the_heaps_answer(vam, expect):
    expect.num(*vam["plain answer"], "the rescanned plain scan still returns the heap's answer")


# ---- the row path's keys, pinned as work ------------------------------------

def test_the_pruning_arm_is_on_the_row_path(vam, expect):
    plan = vam["prune plan"]
    m = re.search(r"Columnar Batch Fold\D*?(yes|no)", plan)
    expect.text(plan if plan.startswith("QUERY_ERROR") else (m.group(1) if m else f"no fold line in {plan[:80]!r}"),
                "no", "premise: the pruning arm is on the ROW path, not the fold")


def test_and_it_is_the_vectorized_aggregate_node(vam, expect):
    expect.text(_has(vam["prune plan"], "Columnar Vectorized Aggregates"), "yes",
                "premise: and it is the vectorized aggregate node")


def test_the_row_paths_scan_keys_still_prune(vam, expect):
    m = re.search(r"Columnar Chunk Groups Removed by Filter\D*(\d+)", vam["prune plan"])
    expect.at_least(int(m.group(1)) if m else f"no reading: {vam['prune plan'][:120]!r}", 1,
                    "the row path's scan keys still prune (work done, #717)")


def test_the_pruned_query_still_answers_correctly(vam, expect):
    expect.num(vam["pruned answer"], 10000, "row path: the pruned query still answers correctly")
