"""The planner's chosen plan must not be far slower than one it declined (#433, #434). The
pytest twin of `test/planner_choice_quality.sh`, every check by the shell's name (#1341).

Every other suite asks whether a plan is CORRECT. This one asks whether it is the plan a
sane cost model picks. On wide incompressible rows with an index on a correlated key, it
runs the planner's own choice, then forces each alternative (indexes off; the custom scan
and the sequential scan off), and fails when the choice is more than PLAN_BOUND times
slower than the fastest alternative that really is a different plan. Both arms run in
the same session as the EXPLAIN that names their plan.

A FAILED RUN IS NOT A FAST ONE. The chosen plan must run to completion before it can be
compared: an error, a cancel by the statement timeout or a lost backend is a failure by
name, never a short time. A forced alternative that failed fails the comparison instead
of standing in as the fastest plan. An arm whose EXPLAIN named no scan node is not a
different plan.

PGC_SKIP_TIMING, AS THE SHELL READS IT. With PGC_SKIP_TIMING=1 nothing is executed and
the fixture is built at a tenth of the size; the plan-shape premises still run, and the
two measured checks per query are declared unrunnable under the shell's names, so the
session cannot end green. The shell's driver does not run the suite at all under that
flag, which is why CI's suites job reports it skipped.

ON A PRIVATE DATABASE, with serial planning, as the shell's cluster runs.
"""
import os
import re
import time

import psycopg
import pytest

from pgc_vacuity import QUERY_ERROR, query_error

DB = "pgc_planner_choice_quality"
MEASURING = os.environ.get("PGC_SKIP_TIMING", "0") != "1"
ROWS = int(os.environ.get("PGC_PLAN_ROWS", "200000" if MEASURING else "20000"))
PLAN_BOUND = float(os.environ.get("PGC_PLAN_BOUND", "3"))
PLAN_TIMEOUT = int(os.environ.get("PGC_PLAN_TIMEOUT", "20"))
NODE = re.compile(r"Index Only Scan|Index Scan|Bitmap Heap Scan|Custom Scan \([A-Za-z]+\)|Seq Scan")
SKIP_TIMING = "PGC_SKIP_TIMING: wall-clock measurement"
SKIP_RATIO = "PGC_SKIP_TIMING: wall-clock ratio"

QUERIES = {
    "range": f"SELECT tag, count(*) FROM pq WHERE k > {ROWS * 4 // 5} GROUP BY tag",
    "wide": f"SELECT count(*) FROM pq WHERE k > {ROWS // 2}",
}
LABELS = {"range": "range scan with aggregate", "wide": "wide range, narrow projection"}
ARMS = ("", "SET enable_indexscan=off; SET enable_bitmapscan=off;",
        "SET pgcolumnar.enable_custom_scan=off; SET enable_seqscan=off;")


def _first_line(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _one(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_first_line(exc))
    return row[0] if row else query_error("no row")


def _run_plan(dsn, settings, sql):
    """-> (node, reading): a time in ms, "TIMEOUT", a query_error(), or None when unmeasured.

    The EXPLAIN, the warming run and the timed run share one session, so the plan named
    is the plan timed.
    """
    try:
        conn = psycopg.connect(dsn, autocommit=True, connect_timeout=10)
    except psycopg.Error as exc:
        return "unknown", query_error(_first_line(exc))
    with conn:
        try:
            if settings:
                conn.execute(settings)
            plan = "\n".join(r[0] for r in conn.execute(f"EXPLAIN (COSTS OFF) {sql}").fetchall())
            m = NODE.search(plan)
            node = m.group(0) if m else "unknown"
        except psycopg.Error:
            node = "unknown"
        if not MEASURING:
            return node, None
        try:
            conn.execute(f"SET statement_timeout='{PLAN_TIMEOUT}s'")
            # warm first: the plans run in a fixed order, and a cold first arm against a 3x bound
            # could fail on cache order alone. Its outcome is read from the timed run below.
            try:
                conn.execute(sql).fetchall()
            except psycopg.Error:
                pass
            t0 = time.perf_counter()
            conn.execute(sql).fetchall()
            return node, (time.perf_counter() - t0) * 1000.0
        except psycopg.errors.QueryCanceled as exc:
            if "statement timeout" in str(exc):
                return node, "TIMEOUT"
            return node, query_error(_first_line(exc))
        except psycopg.Error as exc:
            return node, query_error(_first_line(exc))


def _measured(v):
    return isinstance(v, float)


def _failed(v):
    return isinstance(v, str) and v.startswith(QUERY_ERROR)


def _shown(v):
    """A failed run's own message, for the failure text: never compared with anything."""
    return f"failed: {v.split('.', 2)[2]}" if _failed(v) and v.count(".") >= 2 else v


def _verdict(r, label):
    """The one outcome the shell's choice_vs_best records for a measured query, as
    (kind, got): kind is "ratio", "slow" or "none"."""
    cnode, cms = r["arms"][label][0]
    best = bnode = slowalt = failalt = None
    for node, ms in r["arms"][label][1:]:
        if node == cnode:
            continue
        if ms == "TIMEOUT":
            slowalt = node
            continue
        if _failed(ms):
            failalt = f"{node} ({_shown(ms)})"
            continue
        if not _measured(ms):
            continue
        if best is None or ms < best:
            best, bnode = ms, node
    if failalt is not None:
        return "ratio", f"no (the forced alternative {failalt} failed, so there is nothing to compare)"
    if best is None:
        ours = "yes" if cnode.startswith("Custom Scan") else f"no ({cnode})"
        return ("slow" if slowalt is not None else "none"), ours
    if cms <= 0 or best <= 0:
        return "ratio", f"no (a side of the ratio is zero: a={cms} b={best})"
    ratio = cms / best
    print(f"-- {label}: chose {cnode} {cms:.3f} ms, best alternative {bnode} {best:.3f} ms, "
          f"{ratio:.2f}x against a {PLAN_BOUND:g}x bound")
    return "ratio", "yes" if round(ratio, 2) <= PLAN_BOUND else f"no ({ratio:.2f}x exceeds the {PLAN_BOUND:g}x bound)"


@pytest.fixture(scope="module")
def pcq(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
        # serial planning, as the shell's cluster runs: a parallel plan aggregates through
        # a different node, so the two harnesses would time different plans
        a.execute(f"ALTER DATABASE {DB} SET max_parallel_workers_per_gather = 0")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        r = {}
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
                conn.execute("CREATE TABLE pq (k bigint, tag text, payload bytea) USING pgcolumnar")
                conn.execute(
                    "INSERT INTO pq SELECT g, 'tag' || (g % 5),"
                    " decode((SELECT string_agg(md5(g::text || s::text), '')"
                    "           FROM generate_series(1,64) s), 'hex')"
                    f" FROM generate_series(1,{ROWS}) g")
                conn.execute("CREATE INDEX pq_k ON pq (k)")
                conn.execute("ANALYZE pq")
        except psycopg.Error as exc:
            print(f"-- fixture build failed: {_first_line(exc)}")
        r["rows"] = _one(dsn, "SELECT count(*) FROM pq")
        r["size"] = _one(dsn, "SELECT pg_total_relation_size('pq')")
        r["correlated"] = _one(dsn, "SELECT CASE WHEN correlation > 0.9 THEN 'yes'"
                                    " ELSE 'no (' || correlation || ')' END"
                                    " FROM pg_stats WHERE tablename='pq' AND attname='k'")
        r["arms"] = {}
        for label, sql in QUERIES.items():
            r["arms"][label] = [_run_plan(dsn, s, sql) for s in ARMS]
            print(f"-- {label}: " + "; ".join(f"{n}={m}" for n, m in r["arms"][label]))
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _identified(r, label):
    cnode = r["arms"][label][0][0]
    return "yes" if cnode != "unknown" else "no (grep matched no scan node)"


def _forced_differs(r, label):
    (cnode, _), (n1, _), (n2, _) = r["arms"][label]
    differs = any(n != cnode and n != "unknown" for n in (n1, n2))
    return "yes" if differs else f"no (chosen {cnode}, forced {n1} and {n2})"


def _completed(r, label):
    cms = r["arms"][label][0][1]
    return "yes" if _measured(cms) else f"no ({_shown(cms) if cms is not None else 'no timing reported'})"


def test_planner_choice_fixture_rows(pcq, expect):
    expect.num(pcq["rows"], ROWS, "fixture rows")


def test_planner_choice_payload_did_not_compress(pcq, expect):
    sz = pcq["size"]
    got = sz if not isinstance(sz, int) else (
        "yes" if sz > ROWS * 700 else f"no ({sz // ROWS} bytes per row)")
    expect.text(got, "yes", "premise: the payload did not compress, so the fetch path is exercised")


def test_planner_choice_index_key_is_correlated(pcq, expect):
    expect.text(pcq["correlated"], "yes",
                "premise: the index key is correlated, which is the case that misprices")


def _check_identified(r, key, expect):
    label = LABELS[key]
    expect.text(_identified(r, key), "yes", f"premise: [{label}] the chosen plan was identified")


def _check_forced_differs(r, key, expect):
    label = LABELS[key]
    expect.text(_forced_differs(r, key), "yes", f"[{label}] forcing produced a different plan")


def _check_completed(r, key, expect):
    label = LABELS[key]
    if not MEASURING:
        expect.cannot_run("UNMET_PRECONDITION", SKIP_TIMING,
                          name=f"[{label}] the chosen plan ran to completion")
        return
    expect.text(_completed(r, key), "yes", f"[{label}] the chosen plan ran to completion")


def _check_choice(r, key, expect):
    """Exactly one of the shell's three outcome names, by the branch the shell takes."""
    label = LABELS[key]
    if not MEASURING:
        expect.cannot_run("UNMET_PRECONDITION", SKIP_RATIO,
                          name=f"[{label}] the chosen plan is within the bound of the best alternative")
        return
    if not _measured(r["arms"][key][0][1]):
        expect.text(f"no (not a measurement: {_completed(r, key)})", "yes",
                    f"[{label}] the chosen plan is within the bound of the best alternative")
        return
    kind, got = _verdict(r, key)
    if kind == "slow":
        expect.text(got, "yes", f"[{label}] a forced alternative did not finish, "
                    "so the chosen plan must be the custom scan")
    elif kind == "none":
        expect.text(got, "yes", f"[{label}] nothing could be forced, "
                    "so the chosen plan must be the custom scan")
    else:
        expect.text(got, "yes", f"[{label}] the chosen plan is within the bound of the best alternative")


# -- range scan with aggregate ----------------------------------------------------------

def test_range_aggregate_chosen_plan_was_identified(pcq, expect):
    _check_identified(pcq, "range", expect)


def test_range_aggregate_forcing_produced_a_different_plan(pcq, expect):
    _check_forced_differs(pcq, "range", expect)


def test_range_aggregate_chosen_plan_ran_to_completion(pcq, expect):
    _check_completed(pcq, "range", expect)


def test_range_aggregate_chosen_plan_is_not_far_slower(pcq, expect):
    _check_choice(pcq, "range", expect)


# -- wide range, narrow projection ------------------------------------------------------

def test_wide_range_count_chosen_plan_was_identified(pcq, expect):
    _check_identified(pcq, "wide", expect)


def test_wide_range_count_forcing_produced_a_different_plan(pcq, expect):
    _check_forced_differs(pcq, "wide", expect)


def test_wide_range_count_chosen_plan_ran_to_completion(pcq, expect):
    _check_completed(pcq, "wide", expect)


def test_wide_range_count_chosen_plan_is_not_far_slower(pcq, expect):
    _check_choice(pcq, "wide", expect)
