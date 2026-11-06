"""The vectorized grouped aggregate sizes its hash table from the planner's group
estimate instead of growing it from nothing (#403 item 6), bounded so a wrong estimate
cannot inflate it (#810 review). The pytest twin of `test/groupagg_table_sizing.sh`,
every check by the shell's name (#1341).

On 2,000,000 rows in 200,000 groups with n_distinct declared, the node must rehash at
most a quarter of what the doubling ladder would, in at most four allocations, and
never allocate more than the real group count needs. An expression key the planner
over-estimates must not inflate a table that never grows, and a key that does force a
grow must not jump past eight times its need. Every answer is read through the
vectorized node, and must be right.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading (#1339). A counter that was not printed is a failed reading, and a
real group count must be a positive number, so two empty readings cannot agree.
"""
import re

import psycopg
import pytest

from pgc_vacuity import QUERY_ERROR, query_error

DB = "pgc_groupagg_table_sizing"
NROWS, NGROUPS = 2000000, 200000
ON = ("SET pgcolumnar.enable_group_vectorization = on", "SET pgcolumnar.groupagg_max_groups = 10000000")
BIG = ("SET work_mem = '64MB'",)


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _failed(v):
    return str(v).startswith(QUERY_ERROR)


def _lines(dsn, sets, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for s in sets:
                conn.execute(s)
            return [r[0] for r in conn.execute(sql)]
    except psycopg.Error as exc:
        return query_error(_message(exc))


def _value(dsn, sql, sets=()):
    v = _lines(dsn, sets, sql)
    if _failed(v):
        return v
    return query_error(f"no value from {sql}") if not v or v[0] is None else v[0]


def _field(plan, label):
    if _failed(plan):
        return ""
    for line in plan:
        m = re.search(rf"{label}: (\d+)", line)
        if m:
            return int(m.group(1))
    return ""


def _estimate(plan):
    if _failed(plan):
        return ""
    for line in plan:
        m = re.search(r"rows=(\d+).*width", line)
        if m:
            return int(m.group(1))
    return ""


def _need(groups):
    c = 1024
    while c * 0.7 < groups:
        c *= 2
    return c


def _num(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _groups_or_why(v):
    return str(v) if _num(v) and v > 0 else f"no groups read [{v}]"


@pytest.fixture(scope="module")
def gt(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        try:
            with psycopg.connect(dsn, autocommit=True) as c:
                c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
                c.execute("CREATE TABLE gts (k int, v int) USING pgcolumnar")
                c.execute(f"INSERT INTO gts SELECT i % {NGROUPS}, i FROM generate_series(1, {NROWS}) i")
                c.execute(f"ALTER TABLE gts ALTER COLUMN k SET (n_distinct = {NGROUPS})")
                c.execute("ANALYZE gts")
                c.execute("CREATE TABLE gts_expr (ts timestamptz, v int) USING pgcolumnar")
                c.execute("INSERT INTO gts_expr SELECT '2026-01-01'::timestamptz + (i || ' seconds')::interval, i "
                          "FROM generate_series(1, 500000) i")
                c.execute("ANALYZE gts_expr")
        except psycopg.Error:
            pass
        analyze = "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) "
        r = {"plan": _lines(dsn, ON + BIG, analyze + "SELECT k, sum(v) FROM gts GROUP BY k")}
        r["distinct"] = _value(dsn, "SELECT count(DISTINCT k) FROM gts")
        r["est"] = _estimate(_lines(dsn, ON + BIG, "EXPLAIN (COSTS ON) SELECT k, sum(v) FROM gts GROUP BY k"))
        for key, label in (("rehashed", "Columnar Group Table Entries Rehashed"),
                           ("resizes", "Columnar Group Table Resizes"), ("cap", "Columnar Group Table Capacity")):
            r[key] = _field(r["plan"], label)

        expr_q = "SELECT v / 100000 d, count(*) FROM gts_expr GROUP BY 1"
        r["expr cap"] = _field(_lines(dsn, ON + BIG, analyze + expr_q), "Columnar Group Table Capacity")
        r["expr real"] = _value(dsn, "SELECT count(*) FROM (SELECT v / 100000 FROM gts_expr GROUP BY 1) s")
        r["expr est"] = _estimate(_lines(dsn, ON + BIG, "EXPLAIN (COSTS ON) " + expr_q))
        # through the node, counted: the "real" counts above run core's HashAggregate
        r["expr node"] = _value(dsn, "SELECT count(c) FROM (SELECT v / 100000 d, count(*) c FROM gts_expr GROUP BY 1) s",
                                ON + BIG)
        r["expr distinct"] = _value(dsn, "SELECT count(DISTINCT v / 100000) FROM gts_expr")

        min_q = "SELECT v / 60 d, count(*) FROM gts_expr GROUP BY 1"
        r["min cap"] = _field(_lines(dsn, ON + BIG, analyze + min_q), "Columnar Group Table Capacity")
        r["min real"] = _value(dsn, "SELECT count(*) FROM (SELECT v / 60 FROM gts_expr GROUP BY 1) s")
        r["min node"] = _value(dsn, "SELECT count(c) FROM (SELECT v / 60 d, count(*) c FROM gts_expr GROUP BY 1) s",
                               ON + BIG)
        r["min distinct"] = _value(dsn, "SELECT count(DISTINCT v / 60) FROM gts_expr")

        r["small"] = _value(dsn, "SELECT count(s) FROM (SELECT k, sum(v) AS s FROM gts GROUP BY k) t",
                            ON + ("SET work_mem = '1MB'",))
        r["agg"] = _value(dsn, "SELECT sum(s) FROM (SELECT k, sum(v) AS s FROM gts GROUP BY k) t", ON + BIG)
        r["agg want"] = _value(dsn, f"SELECT CASE WHEN count(*) = {NROWS} THEN sum(v)::numeric::text "
                                    "ELSE 'gts holds ' || count(*) || ' rows' END FROM gts")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_the_vectorized_grouped_aggregate_is_the_node_that_ran(gt, expect):
    plan = gt["plan"]
    if _failed(plan):
        got = plan
    elif any("Columnar Vectorized Group Keys" in l for l in plan):
        got = "yes"
    else:
        other = next((m.group(0) for l in plan for m in [re.search(r"HashAggregate|GroupAggregate|Custom Scan", l)] if m), "")
        got = f"no -- {other}"
    expect.text(got, "yes", "the vectorized grouped aggregate is the node that ran")


def test_it_built_the_group_count_this_suite_is_sized_for(gt, expect):
    expect.text(str(gt["distinct"]), str(NGROUPS), "premise: it built the group count this suite is sized for")


def test_the_work_done_counter_is_reported(gt, expect):
    expect.text("yes" if _num(gt["rehashed"]) else "no", "yes", "the work-done counter is reported at all")


def test_the_declared_n_distinct_reached_the_planner(gt, expect):
    e = gt["est"] if _num(gt["est"]) else 0
    expect.text("accurate" if 0.9 * NGROUPS <= e <= 1.1 * NGROUPS else f"SAMPLED ({e})", "accurate",
                "premise: the declared n_distinct reached the planner, so the estimate is not a sample")


def test_a_sized_table_removes_most_of_the_rehashing(gt, expect):
    ladder = int(0.7 * (_need(NGROUPS) - 1024))
    r = gt["rehashed"] if _num(gt["rehashed"]) else 999999999
    expect.text("removed" if r <= ladder / 4 else f"KEPT {r} of {ladder}", "removed",
                "a sized table removes most of the rehashing (#403 item 6)")


def test_it_gets_there_in_a_few_allocations(gt, expect):
    n = gt["resizes"] if _num(gt["resizes"]) else 99
    expect.text("few" if n <= 4 else f"{n} allocations", "few",
                "and it gets there in a few allocations, not a ladder (#403 item 6)")


def test_the_table_is_never_larger_than_the_real_group_count_needs(gt, expect):
    cap, need = gt["cap"] if _num(gt["cap"]) else 0, _need(NGROUPS)
    expect.text("bounded" if 0 < cap <= need else f"OVERSIZED ({cap} entries for {NGROUPS} groups, which need {need})",
                "bounded", "the table is never larger than the real group count needs (#810 review)")


def test_the_planner_really_over_estimates_an_expression_key(gt, expect):
    e = gt["expr est"] if _num(gt["expr est"]) else 0
    t = gt["expr real"] if _num(gt["expr real"]) else 1
    expect.text("over-estimated" if e >= 100 * t else f"NOT OVER-ESTIMATED (est {e} real {t})", "over-estimated",
                "premise: the planner really over-estimates an expression key, or there is nothing to guard")


def test_a_wrong_estimate_cannot_inflate_a_table_that_never_needed_to_grow(gt, expect):
    cap = gt["expr cap"] if _num(gt["expr cap"]) else 99999
    expect.text("not inflated" if cap <= 1024 else f"INFLATED ({cap} entries for {gt['expr real']} real groups)",
                "not inflated", "a wrong estimate cannot inflate a table that never needed to grow (#810 review)")


def test_the_expression_key_still_answers_correctly(gt, expect):
    expect.text(_groups_or_why(gt["expr node"]), str(gt["expr distinct"]),
                "and the expression key still answers correctly")


def test_this_key_really_does_force_the_table_to_grow(gt, expect):
    n = gt["min real"] if _num(gt["min real"]) else 0
    expect.text("grows" if n > 716 else f"FITS IN 1024 ({n} groups)", "grows",
                "premise: this key really does force the table to grow, or the bound is untested")


def test_a_grow_cannot_jump_to_a_wrong_estimate(gt, expect):
    c = gt["min cap"] if _num(gt["min cap"]) else 0
    n = _need(gt["min real"] if _num(gt["min real"]) else 1)
    expect.text("bounded" if 0 < c <= 8 * n else f"UNBOUNDED ({c} for a need of {n})", "bounded",
                "a grow cannot jump to a wrong estimate, only toward it (#810 review)")


def test_the_minute_key_answers_correctly_too(gt, expect):
    expect.text(_groups_or_why(gt["min node"]), str(gt["min distinct"]), "and the minute key answers correctly too")


def test_the_answer_is_the_same_under_a_small_work_mem(gt, expect):
    expect.text(str(gt["small"]), str(NGROUPS), "and the answer is the same under a small work_mem")


def test_the_aggregate_itself_is_still_right(gt, expect):
    expect.text(str(gt["agg"]), str(gt["agg want"]), "the aggregate itself is still right")
