"""EXPLAIN reports what qual pushdown did, the same way on every columnar node (#191, #479,
#493, #495, #726). The pytest twin of `test/pushdown_report.sh`, every check by the
shell's name (#1341).

With pushdown on, the scan reports its pushed-down filters and the chunk groups they
skipped, and the executor filters almost nothing; with it off, none of that, and the
executor examines every row. A qual whose operator has no ordering proc in its btree
family (built here: `bigint >>> oid`) is still pushed down but builds no usable skip
predicate, and removes no groups. The scalar scan, the ungrouped vectorized aggregate
and the grouped one agree on these counters, plain EXPLAIN reports them as EXPLAIN
ANALYZE does, and each counter has exactly one emitter in the source.

A COMPARISON OF TWO EXPLAIN FIELDS IS NUMERIC: five of them read a field from two plans
and agreed when the field was missing from both. With the vector-predicate line never
printed, "plain EXPLAIN reports the ungrouped node's vector predicates (#726)" passed.
A failed read of the pushdown-on plan is "no reading", not 0 rows removed: the
executor-filter check passed with that plan failing.

ON A PRIVATE DATABASE, with serial planning as the shell's cluster runs, built in the
shell's order by a module fixture.
"""
import pathlib
import re

import psycopg
import pytest

from pgc_vacuity import QUERY_ERROR, query_error

DB = "pgc_pushdown_report"
ROWS = 200000
SRC = pathlib.Path(__file__).resolve().parent.parent.parent / "src"
UAGG = "pgcolumnar.enable_ungrouped_vector_agg"
GAGG = "pgcolumnar.enable_group_vectorization"
UNUSABLE_OP = """
CREATE FUNCTION pdr_gt_oid(bigint, oid) RETURNS boolean
  LANGUAGE plpgsql IMMUTABLE AS $b$ BEGIN RETURN $1 > ($2::bigint); END $b$;
CREATE OPERATOR >>> (LEFTARG = bigint, RIGHTARG = oid, FUNCTION = pdr_gt_oid);
ALTER OPERATOR FAMILY integer_ops USING btree ADD OPERATOR 5 >>> (bigint, oid);
"""
EMITTERS = [
    ("exactly one emitter for Columnar Pushed-Down Filters (#495)", "Columnar Pushed-Down Filters"),
    ("exactly one emitter for Columnar Usable Skip Predicates (#495)", "Columnar Usable Skip Predicates"),
    ("exactly one emitter for Columnar Chunk Groups Total (#495)", "Columnar Chunk Groups Total"),
    ("exactly one emitter for Columnar Chunk Groups Read (#495)", "Columnar Chunk Groups Read"),
    ("exactly one emitter for Columnar Chunk Groups Removed by Filter (#495)", "Columnar Chunk Groups Removed by Filter"),
    ("exactly one emitter for Columnar Vector Predicates (#495)", "Columnar Vector Predicates"),
]


def _plan(dsn, sql, settings=(), analyze=True):
    opts = "ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF" if analyze else "COSTS OFF"
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for s in settings:
                conn.execute(s)
            return "\n".join(r[0] for r in conn.execute(f"EXPLAIN ({opts}) {sql}"))
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0])


def _field(plan, name):
    m = re.search(re.escape(name) + r": (\d+)", plan)
    return int(m.group(1)) if m else f"no '{name}' line"


def _one(dsn, sql, settings=()):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for s in settings:
                conn.execute(s)
            return str(conn.execute(sql).fetchone()[0])
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0])


def _has(plan, marker):
    return "yes" if marker in plan else "no"


def _scalar(plan):
    return "yes" if "Columnar Projected Columns" in plan else "no"


def _positive(n):
    return ("yes" if n > 0 else f"no ({n})") if isinstance(n, int) else f"no ({n})"


@pytest.fixture(scope="module")
def pd(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
        a.execute(f"ALTER DATABASE {DB} SET max_parallel_workers_per_gather = 0")  # as the shell's cluster
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            conn.execute("CREATE TABLE pdr (id int, k int) USING pgcolumnar")
            conn.execute("SELECT pgcolumnar.set_options('pdr', chunk_group_row_limit => 1000)")
            conn.execute(f"INSERT INTO pdr SELECT g, g FROM generate_series(1, {ROWS}) g")
            conn.execute("CREATE TABLE pdr_u (plain int, b bigint) USING pgcolumnar")
            conn.execute("SELECT pgcolumnar.set_options('pdr_u', stripe_row_limit => 10000)")
            conn.execute(f"INSERT INTO pdr_u SELECT g, g FROM generate_series(1, {ROWS}) g")
            conn.execute(UNUSABLE_OP.encode())
        on = ["SET pgcolumnar.enable_qual_pushdown = on"]
        off = ["SET pgcolumnar.enable_qual_pushdown = off"]
        p_on = _plan(dsn, "SELECT * FROM pdr WHERE id = 1", on)
        p_off = _plan(dsn, "SELECT * FROM pdr WHERE id = 1", off)
        between = "SELECT count(*) || '/' || coalesce(sum(id), 0) FROM pdr WHERE id BETWEEN 5000 AND 5100"
        two = _plan(dsn, "SELECT * FROM pdr WHERE id > 100 AND id < 200", on)
        cut = ROWS - 10000
        usable = _plan(dsn, f"SELECT count(*) FROM pdr_u WHERE plain > {cut}", on)
        unusable = _plan(dsn, f"SELECT count(*) FROM pdr_u WHERE b >>> {cut}::oid", on)
        u_q = f"SELECT count(*), sum(b) FROM pdr_u WHERE plain > {cut}"
        u_usable = _plan(dsn, u_q, on + [f"SET {UAGG} = on"])
        u_unusable = _plan(dsn, f"SELECT count(*), sum(b) FROM pdr_u WHERE b >>> {cut}::oid", on + [f"SET {UAGG} = on"])
        u_plain = _plan(dsn, u_q, on + [f"SET {UAGG} = on"], analyze=False)
        g_plain = _plan(dsn, f"SELECT b, count(*) FROM pdr_u WHERE plain > {cut} GROUP BY 1",
                        on + [f"SET {GAGG} = on"], analyze=False)
        g_usable = _plan(dsn, f"SELECT b, count(*) FROM pdr_u WHERE plain > {cut} GROUP BY 1", on + [f"SET {GAGG} = on"])
        g_unusable = _plan(dsn, f"SELECT plain, count(*) FROM pdr_u WHERE b >>> {cut}::oid GROUP BY 1",
                           on + [f"SET {GAGG} = on"])
        scalar = _plan(dsn, f"SELECT count(*), sum(b) FROM pdr_u WHERE b >>> {cut}::oid",
                       on + [f"SET {UAGG} = off", f"SET {GAGG} = off"])
        yield {
            "on": p_on, "off": p_off, "two": two, "usable": usable, "unusable": unusable,
            "u usable": u_usable, "u unusable": u_unusable, "u plain": u_plain, "g plain": g_plain,
            "g usable": g_usable, "g unusable": g_unusable, "scalar": scalar,
            "answer on": _one(dsn, between, on), "answer off": _one(dsn, between, off),
            "unusable answer": _one(dsn, f"SELECT count(*) FROM pdr_u WHERE b >>> {cut}::oid"),
            "no proc": _one(dsn, "SELECT count(*) FROM pg_amproc ap JOIN pg_opfamily f ON f.oid = ap.amprocfamily "
                                 "WHERE f.opfname = 'integer_ops' AND ap.amproclefttype = 'int8'::regtype "
                                 "AND ap.amprocrighttype = 'oid'::regtype AND ap.amprocnum = 1"),
            "no core pair": _one(dsn, """SELECT count(*) FROM pg_amop ao
                JOIN pg_am am ON am.oid = ao.amopmethod AND am.amname = 'btree'
                WHERE ao.amoplefttype <> ao.amoprighttype AND ao.amopstrategy BETWEEN 1 AND 5
                  AND ao.amopfamily <> (SELECT oid FROM pg_opfamily WHERE opfname = 'integer_ops' AND opfmethod = am.oid)
                  AND NOT EXISTS (SELECT 1 FROM pg_amproc ap WHERE ap.amprocfamily = ao.amopfamily
                                  AND ap.amproclefttype = ao.amoplefttype AND ap.amprocrighttype = ao.amoprighttype
                                  AND ap.amprocnum = 1)"""),
        }
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


F, U, T, RD, RM, V = ("Columnar Pushed-Down Filters", "Columnar Usable Skip Predicates", "Columnar Chunk Groups Total",
                      "Columnar Chunk Groups Read", "Columnar Chunk Groups Removed by Filter", "Columnar Vector Predicates")


# ---- the scalar scan, pushdown on and off ------------------------------------------

def test_the_plan_is_a_columnar_custom_scan(pd, expect):
    expect.text(_scalar(pd["on"]), "yes", "the plan under test is a columnar custom scan")


def test_it_is_still_one_with_pushdown_off(pd, expect):
    expect.text(_scalar(pd["off"]), "yes", "and it is still one with pushdown off")


def test_pushdown_on_reports_a_filter(pd, expect):
    expect.num(_field(pd["on"], F), 1, "pushdown on reports a pushed-down filter")


def test_pushdown_off_reports_none(pd, expect):
    expect.num(_field(pd["off"], F), 0, "pushdown off reports none")


def test_pushdown_on_skips_chunk_groups(pd, expect):
    expect.text(_positive(_field(pd["on"], RM)), "yes", "pushdown on skips chunk groups")


def test_pushdown_off_skips_none(pd, expect):
    expect.num(_field(pd["off"], RM), 0, "pushdown off skips none")


def test_the_executor_filters_more_with_pushdown_off(pd, expect):
    on, off = _field(pd["on"], "Rows Removed by Filter"), _field(pd["off"], "Rows Removed by Filter")
    # A plan that ran may filter nothing at all, so no line reads as 0. A failed read
    # of the on plan is no reading, not 0: it used to pass this check.
    if pd["on"].startswith(QUERY_ERROR):
        got = f"no reading (the pushdown-on plan failed: {pd['on']})"
    elif not isinstance(off, int):
        got = f"no reading ({off})"
    else:
        on = on if isinstance(on, int) else 0
        got = "yes" if off > on else f"no (off removed {off}, on removed {on})"
    expect.text(got, "yes", "the executor filters far more rows with pushdown off")


def test_pushdown_off_examines_every_row(pd, expect):
    expect.num(_field(pd["off"], "Rows Removed by Filter"), ROWS - 1, "pushdown off examines every row")


def test_the_answer_is_unchanged_with_pushdown_on(pd, expect):
    expect.text(pd["answer on"], "101/510050", "the answer is unchanged with pushdown on")


def test_the_answer_is_unchanged_with_pushdown_off(pd, expect):
    expect.text(pd["answer off"], "101/510050", "the answer is unchanged with pushdown off")


def test_two_quals_report_two_filters(pd, expect):
    expect.num(_field(pd["two"], F), 2, "two quals report two pushed-down filters")


# ---- a pushed-down qual that builds no usable skip predicate -------------------------

def test_integer_ops_has_no_ordering_proc_for_the_pair(pd, expect):
    expect.text(pd["no proc"], "0", "premise: integer_ops has no ordering proc for (bigint, oid)")


def test_core_has_no_such_pair(pd, expect):
    expect.text(pd["no core pair"], "0", "premise: and core has no such pair of its own, which is why this is built")


def test_the_usable_arm_is_a_custom_scan(pd, expect):
    expect.text(_scalar(pd["usable"]), "yes", "the usable arm is a columnar custom scan")


def test_the_unusable_arm_is_a_custom_scan(pd, expect):
    expect.text(_scalar(pd["unusable"]), "yes", "the unusable arm is a columnar custom scan")


def test_the_usable_arm_removes_chunk_groups(pd, expect):
    expect.text(_positive(_field(pd["usable"], RM)), "yes", "the usable arm removes chunk groups")


def test_the_unusable_arm_removes_none(pd, expect):
    expect.num(_field(pd["unusable"], RM), 0, "the unusable arm removes none")


def test_the_unusable_arm_returns_the_right_answer(pd, expect):
    expect.text(pd["unusable answer"], "10000", "and the unusable arm still returns the right answer")


def test_both_arms_report_the_filter_pushed_down(pd, expect):
    expect.text(f"{_field(pd['usable'], F)}/{_field(pd['unusable'], F)}", "1/1",
                "both arms report the filter as pushed down")


def test_the_usable_arm_reports_one_usable_predicate(pd, expect):
    expect.num(_field(pd["usable"], U), 1, "the usable arm reports one usable skip predicate")


def test_the_unusable_arm_reports_no_usable_predicate(pd, expect):
    expect.num(_field(pd["unusable"], U), 0, "the unusable arm reports none")


def test_pushdown_off_reports_no_usable_predicates(pd, expect):
    expect.num(_field(pd["off"], U), 0, "pushdown off reports no usable skip predicates")


# ---- the ungrouped vectorized aggregate -------------------------------------------

def test_the_ungrouped_node_ran_usable(pd, expect):
    expect.text(_has(pd["u usable"], "Columnar Vectorized Aggregates"), "yes",
                "the ungrouped vectorized aggregate node ran (usable arm)")


def test_the_ungrouped_node_ran_unusable(pd, expect):
    expect.text(_has(pd["u unusable"], "Columnar Vectorized Aggregates"), "yes",
                "the ungrouped vectorized aggregate node ran (unusable arm)")


def test_ungrouped_the_usable_arm_removes_groups(pd, expect):
    expect.text(_positive(_field(pd["u usable"], RM)), "yes", "ungrouped: the usable arm removes chunk groups")


def test_ungrouped_the_unusable_arm_removes_none(pd, expect):
    expect.num(_field(pd["u unusable"], RM), 0, "ungrouped: the unusable arm removes none")


def test_the_plain_explain_arm_is_the_ungrouped_node(pd, expect):
    expect.text(_has(pd["u plain"], "Columnar Vectorized Aggregates"), "yes",
                "the plain EXPLAIN arm is the ungrouped vectorized aggregate")


def test_the_analyze_arm_reports_a_nonzero_count(pd, expect):
    expect.text(_positive(_field(pd["u usable"], F)), "yes",
                "the same query under ANALYZE reports a non-zero count")


def test_plain_explain_reports_the_ungrouped_filters(pd, expect):
    expect.num(_field(pd["u plain"], F), _field(pd["u usable"], F),
               "plain EXPLAIN reports the ungrouped node's pushed-down filters (#726)")


def test_plain_explain_reports_the_ungrouped_vector_predicates(pd, expect):
    expect.num(_field(pd["u plain"], V), _field(pd["u usable"], V),
               "plain EXPLAIN reports the ungrouped node's vector predicates (#726)")


def test_the_grouped_node_is_planned_for_the_cross_node_arm(pd, expect):
    expect.text(_has(pd["g plain"], "Columnar Vectorized Group Keys"), "yes",
                "the grouped node IS planned for the cross-node arm")


def test_plain_explain_the_grouped_node_agrees(pd, expect):
    expect.num(_field(pd["g plain"], F), _field(pd["u plain"], F),
               "plain EXPLAIN: the grouped node agrees with the ungrouped one (#493, #726)")


def test_ungrouped_the_usable_arm_reports_one_of_each(pd, expect):
    expect.text(f"{_field(pd['u usable'], F)}/{_field(pd['u usable'], U)}", "1/1",
                "ungrouped: the usable arm reports one of each, agreeing")


def test_ungrouped_the_unusable_arm_builds_no_predicate(pd, expect):
    expect.num(_field(pd["u unusable"], U), 0, "ungrouped: and the unusable arm builds no skip predicate")


# ---- the grouped vectorized aggregate ----------------------------------------------

def test_the_grouped_node_ran_usable(pd, expect):
    expect.text(_has(pd["g usable"], "Columnar Vectorized Group Keys"), "yes",
                "the grouped vectorized aggregate node ran (usable arm)")


def test_the_grouped_node_ran_unusable(pd, expect):
    expect.text(_has(pd["g unusable"], "Columnar Vectorized Group Keys"), "yes",
                "the grouped vectorized aggregate node ran (unusable arm)")


def test_grouped_the_usable_arm_removes_groups(pd, expect):
    expect.text(_positive(_field(pd["g usable"], RM)), "yes", "grouped: the usable arm removes chunk groups")


def test_grouped_the_unusable_arm_removes_none(pd, expect):
    expect.num(_field(pd["g unusable"], RM), 0, "grouped: the unusable arm removes none")


def test_grouped_the_usable_arm_reports_one_of_each(pd, expect):
    expect.text(f"{_field(pd['g usable'], F)}/{_field(pd['g usable'], U)}", "1/1",
                "grouped: the usable arm reports one of each, agreeing")


def test_grouped_the_unusable_arm_builds_no_predicate(pd, expect):
    expect.num(_field(pd["g unusable"], U), 0, "grouped: and the unusable arm builds no skip predicate")


# ---- one emitter per counter, and the three nodes agree ------------------------------

@pytest.mark.parametrize("name,line", EMITTERS)
def test_each_counter_has_one_emitter(name, line, expect):
    # Every file under src/, headers included, as the shell's grep -r reads it.
    n = sum(p.read_text(errors="replace").count(f'ExplainPropertyInteger("{line}"')
            for p in SRC.rglob("*") if p.is_file())
    expect.num(n, 1, name)


def test_the_scalar_arm_is_the_scalar_scan(pd, expect):
    expect.text(_has(pd["scalar"], "Columnar Projected Columns"), "yes",
                "the scalar arm really is the scalar scan (#493)")


def test_scalar_and_ungrouped_agree(pd, expect):
    expect.num(_field(pd["u unusable"], F), _field(pd["scalar"], F),
               "scalar and ungrouped agree on Pushed-Down Filters (#493)")


def test_scalar_and_grouped_agree(pd, expect):
    expect.num(_field(pd["g unusable"], F), _field(pd["scalar"], F),
               "scalar and grouped agree on Pushed-Down Filters (#493)")


def test_the_ungrouped_node_reports_vector_predicates_separately(pd, expect):
    expect.text(_has(pd["u unusable"], "Columnar Vector Predicates"), "yes",
                "the ungrouped node reports its vector-predicate count separately (#493)")


def test_it_is_the_count_that_used_to_be_mislabelled(pd, expect):
    expect.num(_field(pd["u unusable"], V), 0, "and it is the count that used to be mislabelled (#493)")
