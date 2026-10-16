"""Chunk-group skipping works for IN-list / = ANY(array) predicates (#704, #1341). The
pytest twin of `test/native_saop_pushdown.sh`, every check by the shell's name.

The scan-key builder keeps up to 128 non-NULL elements as one set predicate: a group
survives when any element can lie in its zone map and bloom filter, and larger sets keep
the bounded [min,max] hull. 40000 rows in 20 monotonic groups, plus an overlap column
whose every group has the range [10,88] and only even values, so an odd probe inside it
can be pruned by the bloom filter alone. The hazards each have an arm: NOT IN builds no
keys, an empty or all-NULL array neither crashes nor mis-counts, a generic plan's array
parameter is frozen at Begin, a correlated PARAM_EXEC array is not, and the batch fold
never takes the conservative set keys as its exact filter. Last, the reader's flag guard:
every special scan-key flag but SK_SEARCHARRAY is refused, and that one has its own arm.
"""
import pathlib
import re

import psycopg
import pytest

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "columnar_reader.c"
REMOVED = "Chunk Groups Removed by Filter"


def _one(c, sql):
    return c.execute(sql).fetchone()[0]


def _plan(c, sql, sets=()):
    for s in sets:
        c.execute(s)
    try:
        return "\n".join(r[0] for r in c.execute(f"EXPLAIN (ANALYZE, TIMING off, SUMMARY off) {sql}"))
    finally:
        for s in sets:
            c.execute(f"RESET {s.split()[1].split('=')[0]}")


def _counter(plan, label):
    """-> the first number after `label: `, or -1 when the plan has no such line: a plan
    that is not the columnar scan reads as its own red, not as 0 groups removed."""
    m = re.search(rf"{re.escape(label)}: (\d+)", plan)
    return int(m.group(1)) if m else -1


def _answer(c, sql):
    """-> the first value as text, or the error: a crash is a named red, not a lost test."""
    try:
        return str(_one(c, sql))
    except psycopg.Error as exc:
        return f"error: {exc}"


def _removed(c, sql):
    return _counter(_plan(c, sql), REMOVED)


def _generic(c, name, params, body, run):
    """-> the rows of `run` on a forced generic plan of `body`, prepared as `name`."""
    c.execute("SET plan_cache_mode = force_generic_plan")
    try:
        c.execute(f"PREPARE {name}({params}) AS {body}")
        try:
            return [r[0] for r in c.execute(run)]
        finally:
            c.execute(f"DEALLOCATE {name}")
    finally:
        c.execute("RESET plan_cache_mode")


@pytest.fixture(scope="module")
def saop(pgc_cluster):
    """-> a connection to a database holding the shell's t and thr."""
    db = "pgc_saop"
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {db}")
    try:
        # prepare_threshold=None: the arms PREPARE and DEALLOCATE by name themselves
        with psycopg.connect(pgc_cluster.dsn(dbname=db), autocommit=True, prepare_threshold=None) as c:
            c.execute("CREATE EXTENSION pgcolumnar")
            # 40000 rows / 2000 = 20 monotonic groups; values >= 39000 live in the last one.
            # ov = (g%40)*2+10 gives every group the same even-valued range [10,88].
            c.execute("SET pgcolumnar.stripe_row_limit = 2000")
            c.execute("CREATE DOMAIN intdom AS int")
            c.execute('CREATE TABLE t (id int, ts int, txt text COLLATE "C", ov int) USING pgcolumnar')
            c.execute("INSERT INTO t SELECT g, g, lpad(g::text, 8, '0'), (g % 40)*2 + 10 "
                      "FROM generate_series(1,40000) g")
            c.execute("RESET pgcolumnar.stripe_row_limit")
            c.execute("CREATE TABLE thr (lo int[])")
            c.execute("INSERT INTO thr VALUES ('{39100}'),('{100,200}')")
            yield c
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


def test_a_set_prunes_by_element_and_keeps_the_exact_answer(saop, expect):
    c = saop
    # intent, usability and work done are three counters, so intent cannot impersonate work
    plan = _plan(c, "SELECT count(*) FROM t WHERE ts IN (39100, 39200)")
    expect.num(_counter(plan, "Columnar Pushed-Down Filters"), 1, "an IN-list pushes one set filter (intent)")
    expect.num(_counter(plan, "Columnar Usable Skip Predicates"), 1,
               "the IN-list set filter is a usable skip predicate")
    expect.num(_counter(plan, REMOVED), 19, "an IN-list confined to the last group prunes (19 of 20 removed)")
    expect.num(_one(c, "SELECT count(*) FROM t WHERE ts IN (39100, 39200)"),
               _one(c, "SELECT count(*) FROM t WHERE ts = 39100 OR ts = 39200"),
               "IN-list count matches the OR-literal equivalent")

    # the scattered set's hull spans nearly the whole table, so only per-element pruning removes 17
    expect.num(_removed(c, "SELECT count(*) FROM t WHERE ts IN (100, 20100, 38100)"), 17,
               "a scattered IN-list prunes by element, not only by its hull (17 removed)")
    expect.num(_one(c, "SELECT count(*) FROM t WHERE ts IN (100, 20100, 38100)"), 3,
               "scattered per-element pruning keeps the exact answer")
    expect.num(_removed(c, "SELECT count(*) FROM t WHERE ts IN (100, 101, 102)"), 19,
               "a contiguous IN-list agrees with its hull (19 removed)")
    expect.num(_one(c, "SELECT count(*) FROM t WHERE ts IN (100, 101, 102)"), 3,
               "contiguous per-element pruning keeps the exact answer")

    # both sides of the 128-element bound
    limit = ",".join(str(i) for i in range(1, 129))
    expect.num(_counter(_plan(c, f"SELECT count(*) FROM t WHERE ts IN ({limit})"), "Columnar Pushed-Down Filters"), 1,
               "exactly 128 elements still use one set filter")
    large = ",".join(str(i) for i in range(1, 130))
    expect.num(_counter(_plan(c, f"SELECT count(*) FROM t WHERE ts IN ({large})"), "Columnar Pushed-Down Filters"), 2,
               "a large IN-list falls back to two bounded hull filters")
    expect.num(_one(c, f"SELECT count(*) FROM t WHERE ts IN ({large})"), 129,
               "the large-list fallback keeps the exact answer")


def test_nulls_types_and_collations_prune_exactly(saop, expect):
    c = saop
    for name, sql, want in (
        ("a NULL element is ignored for the range, not a bailout (19 removed)",
         "SELECT count(*) FROM t WHERE ts = ANY (ARRAY[39100, NULL]::int[])", 19),
        ("a multi-value integer set strips NULL and still prunes 17 groups",
         "SELECT count(*) FROM t WHERE ts = ANY (ARRAY[100,20100,38100,NULL]::int[])", 17),
        ("a by-reference text set strips NULL without crashing (17 removed)",
         "SELECT count(*) FROM t WHERE txt = ANY (ARRAY['00000100','00020100','00038100',NULL]::text[])", 17),
        ("a cross-type element array (int col, bigint[] list) prunes (19 removed)",
         "SELECT count(*) FROM t WHERE ts = ANY ('{39100,39200}'::bigint[])", 19),
        ("a C-collated text IN-list prunes (19 removed)",
         "SELECT count(*) FROM t WHERE txt IN ('00039100', '00039200')", 19),
        # a domain-element cast arrives with the BASE type's OID in the array header
        ("a domain-element array prunes (19 removed)",
         "SELECT count(*) FROM t WHERE ts = ANY ('{39100,39200}'::intdom[])", 19),
    ):
        expect.num(_removed(c, sql), want, name)
    for name, sql, want in (
        ("NULL-element count is exact", "SELECT count(*) FROM t WHERE ts = ANY (ARRAY[39100, NULL]::int[])", 1),
        ("the multi-value integer NULL set stays exact",
         "SELECT count(*) FROM t WHERE ts = ANY (ARRAY[100,20100,38100,NULL]::int[])", 3),
        ("the by-reference text NULL set stays exact",
         "SELECT count(*) FROM t WHERE txt = ANY (ARRAY['00000100','00020100','00038100',NULL]::text[])", 3),
        ("cross-type count is exact", "SELECT count(*) FROM t WHERE ts = ANY ('{39100,39200}'::bigint[])", 2),
        ("text IN-list count is exact", "SELECT count(*) FROM t WHERE txt IN ('00039100', '00039200')", 2),
        ("domain-element count is exact", "SELECT count(*) FROM t WHERE ts = ANY ('{39100,39200}'::intdom[])", 2),
    ):
        expect.num(_one(c, sql), want, name)


def test_only_the_bloom_filter_can_prune_the_overlap_column(saop, expect):
    c = saop
    # premises first: 25 lies inside every group's range, so the 20-removed arms are the bloom probe
    expect.num(_removed(c, "SELECT count(*) FROM t WHERE ov >= 25 AND ov <= 25"), 0,
               "fixture premise: 25 is inside every group's range (range keys prune 0)")
    expect.num(_removed(c, "SELECT count(*) FROM t WHERE ov = 25"), 20,
               "fixture premise: equality on the overlap column bloom-prunes all 20")
    expect.num(_removed(c, "SELECT count(*) FROM t WHERE ov = ANY ('{25,27,29}'::int[])"), 20,
               "a multi-value set bloom-prunes all overlapping groups")
    expect.num(_one(c, "SELECT count(*) FROM t WHERE ov = ANY ('{25,27,29}'::int[])"), 0,
               "the bloom-only multi-value set returns the exact empty answer")
    # IN (25) is parsed to `= 25`; '{25,25}' is the SAOP shape of the single-distinct-value refinement
    expect.num(_removed(c, "SELECT count(*) FROM t WHERE ov IN (25)"), 20,
               "a one-element IN-list (parser-collapsed to =) bloom-prunes (20 removed)")
    expect.num(_removed(c, "SELECT count(*) FROM t WHERE ov = ANY ('{25,25}'::int[])"), 20,
               "a single-DISTINCT-value SAOP list ('{25,25}') bloom-prunes (20 removed)")
    expect.num(_one(c, "SELECT count(*) FROM t WHERE ov IN (25)"), 0, "single-value IN-list count is exact (0 rows)")


def test_the_conservative_arms_build_nothing_wrong(saop, expect):
    c = saop
    q = "SELECT count(*) FROM t WHERE ts IN (100, 39900)"
    expect.text(f"{_removed(c, q)}/{_one(c, q)}", "18/2",
                "a range-spanning IN-list prunes between its elements (18 removed, exact count)")
    # confined to one group: a positive range wrongly derived from NOT IN would collapse the count
    q = "SELECT count(*) FROM t WHERE ts NOT IN (19000, 19500)"
    expect.text(f"{_removed(c, q)}/{_one(c, q)}", "0/39998", "NOT IN builds no range (0 removed) and stays correct")
    expect.num(_one(c, "SELECT count(*) FROM t WHERE ts = ANY ('{}'::int[])"), 0,
               "an empty array counts zero and does not crash")
    expect.num(_one(c, "SELECT count(*) FROM t WHERE ts = ANY (ARRAY[NULL,NULL]::int[])"), 0,
               "an all-NULL array counts zero and does not crash")


def test_a_parameter_array_is_frozen_at_begin(saop, expect):
    c = saop
    body = "SELECT count(*) FROM t WHERE ts = ANY ($1)"
    ex = "EXPLAIN (ANALYZE, TIMING off, SUMMARY off) "
    rows = _generic(c, "p", "int[]", body, ex + "EXECUTE p('{39100,39200}')")
    expect.num(_counter("\n".join(rows), REMOVED), 19, "a generic-plan parameterized array prunes (19 removed)")
    expect.num(_generic(c, "pc", "int[]", body, "EXECUTE pc('{39100,39200}')")[0], 2,
               "parameterized array count matches the literal count")
    # IN (literal, $1) stays an unfolded ArrayExpr on a generic plan: the freeze must evaluate it whole
    rows = _generic(c, "m", "int", "SELECT count(*) FROM t WHERE ts IN (39100, $1)", ex + "EXECUTE m(39200)")
    expect.num(_counter("\n".join(rows), REMOVED), 19,
               "a mixed literal+param IN-list prunes on a generic plan (19 removed)")


def test_the_batch_fold_never_takes_the_set_keys_as_exact(saop, expect):
    c = saop
    vec = ("SET pgcolumnar.enable_ungrouped_vector_agg=on",)
    plan = _plan(c, "SELECT count(*) FROM t WHERE ts IN (39100, 39150)", vec)
    expect.num(plan.count("Columnar Vectorized Aggregates"), 1,
               "vector-agg path engaged for the guard arm (plan shape)")
    # the no/yes pair: the clause gate is what excludes the SAOP, not another eligibility rule
    expect.num(plan.count("Columnar Batch Fold: no"), 1, "the batch fold refuses the IN-list (Batch Fold: no)")
    expect.num(_plan(c, "SELECT count(*) FROM t WHERE ts >= 39100 AND ts <= 39150", vec)
               .count("Columnar Batch Fold: yes"), 1,
               "the batch fold accepts the equivalent exact range quals (Batch Fold: yes)")
    c.execute(vec[0])
    try:
        expect.num(_one(c, "SELECT count(*) FROM t WHERE ts IN (39100, 39150)"), 2,
                   "vector-agg count over an IN-list is exact (conservative keys not folded)")
    finally:
        c.execute("RESET pgcolumnar.enable_ungrouped_vector_agg")


def test_a_correlated_array_is_not_frozen_and_the_backend_lives(saop, expect):
    c = saop
    # the two thr rows land in different groups, so a frozen first array would lose rows;
    # freezing the PARAM_EXEC at Begin crashes the backend instead, and that is a red here too
    expect.text(_answer(c, "SELECT sum(c) FROM thr, LATERAL "
                           "(SELECT count(*) c FROM t WHERE t.ts = ANY (thr.lo)) s"), "3",
                "a correlated PARAM_EXEC array is not mis-pruned (results stay correct)")
    expect.text(_answer(c, "SELECT 1"), "1", "backend alive")


def test_the_reader_refuses_every_special_flag_but_the_array_one(expect):
    src = SRC.read_text()
    expect.num(sum("key->sk_flags & (SK_ISNULL" in l for l in src.splitlines()), 1,
               "premise: plain-key special flags have one reject expression")
    # from the `if (key->sk_flags & (` line through the first line holding `))`, as the shell's awk range
    guard, inside = "", False
    for line in src.splitlines():
        if not inside and re.search(r"if \(key->sk_flags & \(", line):
            inside = True
        if inside:
            guard += re.sub(r"\s", "", line)
            if "))" in line:
                break
    expect.text("yes" if guard and "sk_flags" in guard else f"extracted=<{guard or 'empty'}>", "yes",
                "premise: the reject expression was extracted, not blank")
    # membership, not adjacency: the flag list is a set
    for flag in ("SK_ISNULL", "SK_ROW_HEADER", "SK_ROW_MEMBER", "SK_ROW_END", "SK_SEARCHNULL",
                 "SK_SEARCHNOTNULL", "SK_ORDER_BY"):
        expect.text("yes" if flag in guard else f"absent from <{guard}>", "yes",
                    f"the predicate builder's reject expression contains {flag}")
    expect.num(len(re.findall(r"out\[n\]\.searchArray = \(key->sk_flags & SK_SEARCHARRAY\)", src)), 1,
               "SK_SEARCHARRAY reaches the dedicated array predicate arm")
