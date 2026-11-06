"""A range column prunes on overlap and containment, and under its own collation (#1144).

A zone map recorded `minimum` and `maximum` only. Those are the range type's btree
ordering, which sorts by lower bound and then upper bound, so the largest range is
not the one reaching furthest right: a unit holding `[1,2)` and `[3,100)` has the
same `maximum` as one holding `[1,2)` and `[3,4)`. Neither `&&` nor `@>` can be
answered from that. `zone_map.max_upper` records the greatest upper bound, in three
states (a bound; NULL for "no bound"; present but empty for "unbounded"), and the
scan skips a unit only when the summary proves it cannot match.

Both halves are asserted, because a pruning change can be wrong in two directions:
a heap mirror answers every query (a prune that removes a unit it should have kept
LOSES ROWS), and the scan's counters must move (a prune that removes nothing is
correct and worthless).

THE PORT OF test/range_pruning.sh, BY NAME (#1341). Every check carries the shell
check's name and asks the same question of the same fixture: 50,000 rows in one
stripe, which at the default chunk_group_row_limit of 10,000 is one row group of
five vectors, so pruning shows at both levels. Independent of the shell suite:
neither file reads or runs the other.

ONE CHECK PER TEST (#1339). Module fixtures build each data set once, and every
reading after that is read-only, so a red cannot stop the next check from running.
A failed query becomes `query_error()`, which every comparison refuses, so an error
fails under the check's own name and two failures never agree.
"""
import re

import psycopg
import pytest

from pgc_vacuity import query_error

N = 50000

WINDOW = "tstzrange('2020-01-02 00:00+00','2020-01-02 01:00+00')"
WEEK = "tstzrange('2020-01-05 00:00+00','2020-01-12 00:00+00')"
POINT = "'2020-01-02 00:30+00'::timestamptz"
FUTURE = "tstzrange('2099-01-01 00:00+00','2099-01-02 00:00+00')"
# NEAR THE END (#1339). A window at minute 45,360 falls in the last of the group's five
# vectors. The four before it start before the window ends, so their LOWER bounds
# cannot rule them out; only the upper bound each one records can.
LATE = "tstzrange('2020-02-01 12:00+00','2020-02-01 13:00+00')"
FAR_POINT = "'2099-06-06 00:00+00'::timestamptz"


def _q(conn, sql):
    """The first column of the first row, or a query_error naming the failure."""
    try:
        return conn.execute(sql).fetchone()[0]
    except psycopg.Error as exc:
        return query_error(exc.diag.message_primary)


def _fingerprint(conn, table, pred):
    """A count AND two sums: a count alone agrees whenever two wrong answers are the
    same size."""
    return str(_q(conn, f"SELECT count(*) || '/' || coalesce(sum(payload),0) || '/' "
                        f"|| coalesce(sum(id),0) FROM {table} WHERE {pred}"))


def _counter(conn, table, pred, label):
    """The scan's own count for `label`, or a query_error when the plan failed or has
    no such line: a plan that lost the columnar scan must not read as "skipped
    nothing"."""
    try:
        plan = "\n".join(r[0] for r in conn.execute(
            f"EXPLAIN (ANALYZE, TIMING OFF, COSTS OFF) SELECT count(*) FROM {table} "
            f"WHERE {pred}").fetchall())
    except psycopg.Error as exc:
        return query_error(exc.diag.message_primary)
    m = re.search(re.escape(label) + r": (\d+)", plan)
    return int(m.group(1)) if m else query_error(f"the plan has no '{label}' line")


def _zone(conn, table, where):
    """Zone map rows of `table` matching `where`, counted."""
    return _q(conn, f"SELECT count(*) FROM pgcolumnar.zone_map z "
                    f"JOIN pgcolumnar.storage s USING (storage_id) "
                    f"WHERE s.relation_oid = '{table}'::regclass AND {where}")


def _schema_conn(pgc_cluster, schema):
    conn = psycopg.connect(pgc_cluster.dsn(), autocommit=True)
    conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    conn.execute(f"CREATE SCHEMA {schema}")
    conn.execute(f"SET search_path = {schema}, public")
    return conn


def _drop(conn, schema):
    try:
        conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    except psycopg.Error:
        pass
    finally:
        conn.close()


def _build(conn, steps, what):
    """Run each step; a failure is printed and leaves the table short or missing,
    which the premises and the oracle then name."""
    try:
        for sql in steps:
            conn.execute(sql)
    except psycopg.Error as exc:
        print(f"-- building {what} failed: {exc.diag.message_primary}")


def _mk(name, columnar, clustered):
    off = "g" if clustered else f"(g * 7919 % {N})"
    return [f"CREATE TABLE {name} (id bigint, span tstzrange, payload int)"
            + (" USING pgcolumnar" if columnar else "")] + (
        [f"SELECT pgcolumnar.set_options('{name}', stripe_row_limit => {N})"]
        if columnar else []) + [
        f"INSERT INTO {name} SELECT g, "
        f"tstzrange('2020-01-01'::timestamptz + ({off}||' minutes')::interval, "
        f"'2020-01-01'::timestamptz + ({off}||' minutes')::interval + interval '1 hour'), "
        f"g % 97 FROM generate_series(1,{N}) g",
        f"ANALYZE {name}"]


UNBOUNDED_ROWS = (
    "SELECT g, tstzrange('2020-01-01'::timestamptz + (g||' minutes')::interval, "
    "'2020-01-01'::timestamptz + (g||' minutes')::interval + interval '1 hour'), g % 97 "
    "FROM generate_series(1,20000) g "
    "UNION ALL SELECT 20001, tstzrange('2020-01-03'::timestamptz, NULL), 5")
# NO EMPTY RANGE IN THIS UNIT: an empty range sorts first, becomes the unit's minimum,
# and a unit whose minimum is empty is never pruned, so the unbounded arms could not
# fail. e_col holds the empty case on its own.


@pytest.fixture(scope="module")
def rp(pgc_cluster):
    """The clustered and scattered pairs, the unbounded pair and the all-empty table."""
    c = _schema_conn(pgc_cluster, "pgc_rp")
    try:
        for name, columnar, clustered in (("r_col", True, True), ("r_heap", False, True),
                                          ("s_col", True, False), ("s_heap", False, False)):
            _build(c, _mk(name, columnar, clustered), name)
        _build(c, [
            "CREATE TABLE u_col (id bigint, span tstzrange, payload int) USING pgcolumnar",
            f"SELECT pgcolumnar.set_options('u_col', stripe_row_limit => {N})",
            "CREATE TABLE u_heap (id bigint, span tstzrange, payload int)",
            f"INSERT INTO u_col {UNBOUNDED_ROWS}",
            f"INSERT INTO u_heap {UNBOUNDED_ROWS}",
            "ANALYZE u_col"], "the unbounded pair")
        # AN EMPTY RANGE NEEDS ITS OWN TABLE. In u_col it shares a unit with the
        # unbounded row, and unbounded is absorbing, so that unit says nothing about
        # empty.
        _build(c, [
            "CREATE TABLE e_col (id bigint, span tstzrange) USING pgcolumnar",
            f"SELECT pgcolumnar.set_options('e_col', stripe_row_limit => {N})",
            "INSERT INTO e_col SELECT g, 'empty'::tstzrange FROM generate_series(1,2000) g",
            "ANALYZE e_col"], "the all-empty table")
        yield c
    finally:
        _drop(c, "pgc_rp")


# ---- premises ----------------------------------------------------------------------

def test_the_clustered_fixture_holds_the_rows(rp, expect):
    expect.num(_q(rp, "SELECT count(*) FROM r_col"), N,
               "premise: the clustered fixture holds the rows the numbers below count")


def test_the_heap_mirror_holds_the_same_rows(rp, expect):
    expect.num(_q(rp, "SELECT count(*) FROM r_heap"), N,
               "premise: the heap mirror holds the same rows")


def test_the_range_column_is_summarised_in_several_units(rp, expect):
    expect.at_least(_zone(rp, "r_col", "z.column_index = 1 AND z.vector_index >= 0"), 4,
                    "premise: the range column is summarised in several units, "
                    "or nothing can be skipped")


def test_every_unit_carries_an_upper_bound(rp, expect):
    expect.num(_zone(rp, "r_col", "z.column_index = 1 AND z.max_upper IS NOT NULL"),
               _zone(rp, "r_col", "z.column_index = 1"),
               "premise: every one of those units carries an upper bound")


def test_a_non_range_column_carries_none(rp, expect):
    expect.num(_zone(rp, "r_col", "z.column_index <> 1 AND z.max_upper IS NOT NULL"), 0,
               "premise: a non-range column carries none")


# ---- correctness: the heap answers the same questions ------------------------------

PROBES = {"WINDOW": WINDOW, "WEEK": WEEK, "POINT": POINT, "FUTURE": FUTURE,
          "LATE": LATE, "FAR_POINT": FAR_POINT}
# (name, pair, operator, probe): a literal table, so the twin grader reads its names.
ORACLE = [
    ("overlap with a one-hour window returns the heap's rows", "r", "&&", "WINDOW"),
    ("overlap with a week-long window returns the heap's rows", "r", "&&", "WEEK"),
    ("containment of a point returns the heap's rows", "r", "@>", "POINT"),
    ("a window no row can meet returns nothing, from both", "r", "&&", "FUTURE"),
    ("and that late overlap returns the heap's rows", "r", "&&", "LATE"),
    ("and still returns the right rows", "s", "&&", "WINDOW"),
    ("a unit holding an unbounded range is never pruned from above", "u", "&&", "FUTURE"),
    ("the same for a containment probe far to its right", "u", "@>", "FAR_POINT"),
]


@pytest.mark.parametrize("name, pair, op, probe", ORACLE)
def test_the_columnar_side_returns_the_heaps_rows(rp, expect, name, pair, op, probe):
    pred = f"span {op} {PROBES[probe]}"
    expect.text(_fingerprint(rp, f"{pair}_col", pred), _fingerprint(rp, f"{pair}_heap", pred),
                name)


def test_the_first_three_oracle_probes_are_not_vacuously_empty(rp, expect):
    counts = [_q(rp, f"SELECT count(*) FROM r_heap WHERE {p}")
              for p in (f"span && {WINDOW}", f"span && {WEEK}", f"span @> {POINT}")]
    expect.text("all three match rows"
                if all(isinstance(n, int) and n > 0 for n in counts)
                else f"AT LEAST ONE IS EMPTY {counts}",
                "all three match rows",
                "premise: the first three of those are not vacuously empty")


# ---- effect: the counters move -----------------------------------------------------

def test_the_overlap_qual_is_pushed_down(rp, expect):
    expect.num(_counter(rp, "r_col", f"span && {WINDOW}", "Pushed-Down Filters"), 1,
               "the overlap qual is pushed down, where it used to be dropped")


def test_the_zone_map_is_probed_for_it(rp, expect):
    expect.at_least(_counter(rp, "r_col", f"span && {WINDOW}", "Zone Map Probes"), 1,
                    "and the zone map is probed for it")


def test_a_clustered_overlap_skips_vectors(rp, expect):
    expect.at_least(_counter(rp, "r_col", f"span && {WINDOW}", "Vectors Skipped"), 1,
                    "a clustered overlap skips vectors instead of decoding them")


def test_a_late_overlap_skips_the_four_vectors_only_upper_bounds_rule_out(rp, expect):
    expect.num(_counter(rp, "r_col", f"span && {LATE}", "Vectors Skipped"), 4,
               "a clustered overlap near the end skips the four vectors only their "
               "upper bounds rule out")


def test_a_containment_probe_skips_vectors(rp, expect):
    expect.at_least(_counter(rp, "r_col", f"span @> {POINT}", "Vectors Skipped"), 1,
                    "so does a containment probe")


def test_a_window_no_row_can_meet_reads_no_chunk_group(rp, expect):
    expect.num(_counter(rp, "r_col", f"span && {FUTURE}", "Chunk Groups Read"), 0,
               "a window no row can meet reads no chunk group at all")


# ---- the documented zero -----------------------------------------------------------

def test_a_scattered_column_skips_nothing(rp, expect):
    expect.num(_counter(rp, "s_col", f"span && {WINDOW}", "Vectors Skipped"), 0,
               "a scattered column skips nothing, which is what the documentation says")


# ---- the two values upper() cannot tell apart --------------------------------------

def test_the_unbounded_row_really_is_matched(rp, expect):
    expect.num(_q(rp, f"SELECT count(*) FROM u_heap WHERE span && {FUTURE}"), 1,
               "premise: that unbounded row really is matched, so the arm is not vacuous")


def test_the_all_empty_table_is_summarised(rp, expect):
    expect.at_least(_zone(rp, "e_col", "z.column_index = 1"), 1,
                    "premise: the all-empty table is summarised at all")


def test_an_empty_range_contributes_no_bound(rp, expect):
    expect.num(_zone(rp, "e_col", "z.column_index = 1 AND z.max_upper IS NULL"),
               _zone(rp, "e_col", "z.column_index = 1"),
               "an empty range contributes no bound, so its units record NULL")


def test_an_unbounded_range_records_a_present_but_empty_bound(rp, expect):
    n = _zone(rp, "u_col", "z.column_index = 1 AND z.max_upper IS NOT NULL "
                           "AND octet_length(z.max_upper) = 0")
    expect.text("present and empty" if isinstance(n, int) and n >= 1
                else f"NOT RECORDED ({n})", "present and empty",
                "and an unbounded range records a bound that is present but empty")


# ---- a range compares under ITS OWN collation, not its element type's --------------
#
# NOT REACHABLE WITH A BUILT-IN RANGE TYPE: every built-in range is over a
# non-collatable subtype. It needs a user-defined range over text with a COLLATION
# whose ordering differs from the database default, and the collation is discovered,
# not assumed: the shell's candidates are tried in the shell's order, and the first
# whose ordering differs from the database default is used.

COLLATION_CANDIDATES = ["en_US.utf8", "en_GB.utf8", "unicode", "ucs_basic", "C"]
COLLATED_PROBES = ['["A","C"]', '["a","b"]', '["B","M"]', '["m","z"]']
NO_COLLATION = ("no available collation orders differently from this database's "
                "default, so the arm cannot discriminate")
RC_GEN = ("SELECT g, tr_coll(v, v || 'zz') FROM (SELECT g, "
          "(ARRAY['A','a','B','b','C','c','M','m','Y','y'])[1 + (g / 1000) % 10] "
          "AS v FROM generate_series(1, 10000) g) s")


@pytest.fixture(scope="module")
def rc(pgc_cluster):
    """(connection, collation), the collation None when no candidate discriminates."""
    c = _schema_conn(pgc_cluster, "pgc_rp_coll")
    try:
        coll = None
        # The shell's candidates, in the shell's order, each asked directly: a
        # collation that does not exist, or an ICU one in a build without ICU, raises,
        # _q turns that into a query_error (never True), and the next is tried. On an
        # autocommit connection the failure poisons nothing. Asking the catalog first,
        # in its own order and without ICU, chose differently from the shell on some
        # hosts (#1529 review).
        for candidate in COLLATION_CANDIDATES:
            if _q(c, f"SELECT ('B' < 'a' COLLATE \"{candidate}\") <> ('B' < 'a')") is True:
                coll = f'"{candidate}"'
                break
        if coll is not None:
            # 1000 is the floor set_options enforces.
            _build(c, [
                f"CREATE TYPE tr_coll AS RANGE (SUBTYPE = text, COLLATION = {coll})",
                "CREATE TABLE rc_heap (id int, span tr_coll)",
                "CREATE TABLE rc_col (id int, span tr_coll) USING pgcolumnar",
                "SELECT pgcolumnar.set_options('rc_col', stripe_row_limit => 1000, "
                "chunk_group_row_limit => 1000)",
                f"INSERT INTO rc_heap {RC_GEN}",
                f"INSERT INTO rc_col {RC_GEN}",
                "ANALYZE rc_heap",
                "ANALYZE rc_col"], "the collated pair")
        yield c, coll
    finally:
        _drop(c, "pgc_rp_coll")


def _collated(rc, expect, name):
    """The connection, or None after declaring this check unrunnable."""
    conn, coll = rc
    if coll is None:
        expect.cannot_run("MISSING_DEPENDENCY", NO_COLLATION, name=name)
        return None
    return conn


def _differs(conn):
    return str(_q(conn, "SELECT (SELECT rngcollation FROM pg_range "
                        "WHERE rngtypid = 'tr_coll'::regtype) "
                        "<> (SELECT typcollation FROM pg_type WHERE oid = 'text'::regtype)"))


def _same_rows(conn):
    return (f"{_q(conn, 'SELECT count(*) FROM rc_heap')} "
            f"{_q(conn, 'SELECT count(*) FROM rc_col')}")


def _summarised(conn):
    n = _zone(conn, "rc_col", "z.max_upper IS NOT NULL")
    return "summarised" if isinstance(n, int) and n >= 1 else f"NOT SUMMARISED ({n})"


def _ids_match(conn):
    ids = ("SELECT string_agg(id::text, ',' ORDER BY id) FROM {} "
           "WHERE span && '[\"A\",\"C\"]'::tr_coll")
    col, heap = str(_q(conn, ids.format("rc_col"))), str(_q(conn, ids.format("rc_heap")))
    return "same ids" if col == heap and "QUERY_ERROR" not in col else f"{col} vs {heap}"


READ = {"differs": (_differs, "True"), "same_rows": (_same_rows, "10000 10000"),
        "summarised": (_summarised, "summarised"), "ids": (_ids_match, "same ids")}
# (name, reading): a literal table, so the twin grader reads its names.
COLLATED = [
    ("premise: the range type collates differently from its element type", "differs"),
    ("premise: both tables hold the same rows before any predicate", "same_rows"),
    ("premise: the collated range column is summarised, so pruning can engage", "summarised"),
    ("and the rows themselves match, not only the count", "ids"),
]


def test_a_discriminating_collation_was_found(rc, expect):
    """The shell records this name only as its SKIP. Here it also asserts what the
    skip stands for: the collation chosen really orders differently."""
    conn = _collated(rc, expect, "a range type's declared collation is the one it prunes under")
    if conn is None:
        return
    expect.text(str(_q(conn, f"SELECT ('B' < 'a' COLLATE {rc[1]}) <> ('B' < 'a')")), "True",
                "a range type's declared collation is the one it prunes under")


@pytest.mark.parametrize("name, reading", COLLATED)
def test_the_collated_pair(rc, expect, name, reading):
    conn = _collated(rc, expect, name)
    if conn is None:
        return
    fn, want = READ[reading]
    expect.text(fn(conn), want, name)


@pytest.mark.parametrize("probe", COLLATED_PROBES)
def test_a_collated_overlap_returns_the_heaps_rows(rc, expect, probe):
    conn, coll = rc
    if coll is None:
        expect.cannot_run("MISSING_DEPENDENCY", NO_COLLATION,
                          name=f"overlap {probe} returns the heap's rows under a declared collation")
        return
    expect.text(str(_q(conn, f"SELECT count(*) FROM rc_col WHERE span && '{probe}'::tr_coll")),
                str(_q(conn, f"SELECT count(*) FROM rc_heap WHERE span && '{probe}'::tr_coll")),
                f"overlap {probe} returns the heap's rows under a declared collation")
