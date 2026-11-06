"""The skip predicates are probed most-selective-first, so a pruned group is not probed
through every predicate's column (#403 item 4). The pytest twin of
`test/qual_order_selectivity.sh`, every check by the shell's name (#1341).

`a >= 0` excludes nothing and `b = 42` excludes all but one of 100 groups. Probed in the
written order every group costs two zone-map probes; once the reader moves the excluding
predicate to the front, a pruned group costs one. Asserted: the columnar scan ran with
both predicates usable over the fixture's 100 groups; the probe count stays near one per
group, whichever order the predicates are written in; and the answers match a heap
mirror, including when nothing is selective.

A PROBE COUNT IS A NUMBER OR NO READING. The reversed-order check read a missing count as
0, inside its bound: with the counter reporting nothing it passed. The heap mirror comes
from the generator, not from the table under test.

ON A PRIVATE DATABASE, with stripe_row_limit 1000 for the table (the shell sets it for its
cluster) and serial planning, as the shell's plan_of runs.
"""
import re

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_qual_order_selectivity"
NROWS = 100000
NGROUPS = 100
BOUND = NGROUPS + 4
FLAT = NGROUPS * 2


def _plan(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("SET max_parallel_workers_per_gather = 0")
            return "\n".join(r[0] for r in conn.execute(
                f"EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) {sql}"))
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0])


def _field(plan, name):
    m = re.search(re.escape(name) + r": (\d+)", plan)
    return int(m.group(1)) if m else f"no '{name}' line"


def _row(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            return "|".join(str(v) for v in conn.execute(sql).fetchone())
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0])


@pytest.fixture(scope="module")
def qo(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            conn.execute("CREATE TABLE qos (a int, b int, pad int) USING pgcolumnar")
            conn.execute("SELECT pgcolumnar.set_options('qos', stripe_row_limit => 1000)")
            conn.execute(f"INSERT INTO qos SELECT i % 10, i, i FROM generate_series(1,{NROWS}) i")
            conn.execute("ANALYZE qos")
            # the mirror comes from the generator, not from qos
            conn.execute("CREATE TABLE qos_heap (a int, b int, pad int)")
            conn.execute(f"INSERT INTO qos_heap SELECT i % 10, i, i FROM generate_series(1,{NROWS}) i")
        plan = _plan(dsn, "SELECT count(*) FROM qos WHERE a >= 0 AND b = 42")
        rev = _plan(dsn, "SELECT count(*) FROM qos WHERE b = 42 AND a >= 0")
        unsel = _plan(dsn, "SELECT count(*) FROM qos WHERE a >= 0 AND b >= 0")
        r = {
            "plan": plan,
            "probes": _field(plan, "Columnar Zone Map Probes"),
            "total": _field(plan, "Columnar Chunk Groups Total"),
            "removed": _field(plan, "Columnar Chunk Groups Removed by Filter"),
            "usable": _field(plan, "Columnar Usable Skip Predicates"),
            "rev probes": _field(rev, "Columnar Zone Map Probes"),
            "unsel removed": _field(unsel, "Columnar Chunk Groups Removed by Filter"),
        }
        for key, cond in (("eq", "a >= 0 AND b = 42"), ("range", "a >= 0 AND b BETWEEN 5000 AND 5100"),
                          ("none", "a >= 0 AND b >= 0")):
            cols = "count(*), sum(b)" if key == "range" else "count(*)"
            r[key] = (_row(dsn, f"SELECT {cols} FROM qos WHERE {cond}"),
                      _row(dsn, f"SELECT {cols} FROM qos_heap WHERE {cond}"))
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_the_columnar_scan_ran(qo, expect):
    expect.text("yes" if "Custom Scan (PgColumnarScan)" in qo["plan"] else "no", "yes",
                "premise: the columnar scan ran, so these are its counters")


def test_both_predicates_are_usable(qo, expect):
    expect.num(qo["usable"], 2, "both predicates were pushed down and are usable")


def test_the_fixture_has_its_groups(qo, expect):
    expect.num(qo["total"], NGROUPS, "premise: the fixture has the groups this suite is sized for")


def test_the_selective_predicate_still_prunes(qo, expect):
    expect.num(qo["removed"], NGROUPS - 1, "the selective predicate still prunes what it always did")


def test_the_probe_counter_is_reported(qo, expect):
    expect.text("yes" if re.search(r"Columnar Zone Map Probes: \d+", qo["plan"]) else "no", "yes",
                "the probe counter is reported at all")


def _ordered(n):
    if not isinstance(n, int) or n == 0:
        return f"no reading ({n} probes)"
    return "ordered" if n <= BOUND else f"UNORDERED ({n} probes for {NGROUPS} groups, flat cost {FLAT})"


def test_a_pruned_group_is_not_probed_through_every_column(qo, expect):
    expect.text(_ordered(qo["probes"]), "ordered",
                "a pruned group is not probed through every predicate's column (#403 item 4)")


def test_the_order_the_predicates_are_written_in_does_not_matter(qo, expect):
    expect.text(_ordered(qo["rev probes"]), "ordered",
                "and it does not depend on the order the predicates are written in")


def test_the_answer_matches_the_heap_oracle(qo, expect):
    got, want = qo["eq"]
    expect.text(got, want, "the answer matches the heap oracle")


def test_a_range_selective_predicate_matches(qo, expect):
    got, want = qo["range"]
    expect.text(got, want, "and so does a query where the selective predicate is a range")


def test_nothing_selective_still_answers_correctly(qo, expect):
    got, want = qo["none"]
    expect.text(got, want, "and one where NOTHING is selective, which must still answer correctly")


def test_no_selective_predicate_prunes_nothing(qo, expect):
    expect.num(qo["unsel removed"], 0, "a query with no selective predicate prunes nothing, as before")
