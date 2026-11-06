"""Port of test/hilbert_locality.sh: does the Hilbert curve buy range locality?

BLOCKED ON #897. THIS FILE CANNOT RUN ON main.

The harness it is written against -- `conftest.py`, `pgc_vacuity.py`,
`pgc_cluster.py`, `pytest.ini` -- lives only on the branch of pull request #897,
which is NOT MERGED and currently has changes requested. `test/pytest/` does not
exist on main, so on main this file has no `expect` fixture, no `pgc_cluster`
fixture and no vacuity plugin, and pytest will fail at collection. It is
committed here so that the port exists and can be reviewed beside the bash
suite; it becomes runnable when #897 lands, and not before.

It was RUN against **#897 at b785795d7ccd** -- the head as of 2026-09-09 -- and
passed 18 of 18 there. It was originally written against 5f3dedb; those two trees
are NOT identical (e09554995ebe vs c8120cdfca1e, +925 lines including the
`cannot_run` fix this file depends on), so it was re-run rather than relabelled.
The SHA is pinned rather than the branch name because a branch name cannot be
checked six weeks from now, and this one moved three times while the file was
being written.

It moved a fourth time after this header was written: #897 is now 9064a46eb57d.
That pin is deliberately NOT updated, because the point of a SHA is to say what
was tested. `test/pytest/` is byte-identical between the two -- both trees hash
to b20ad7e388e0, and the whole delta is thirty lines in one
`test/selftest/` file the pytest harness never reads -- so the 18-of-18 result
above still describes the current head. Verified here rather than taken from the
push notice.

#897 HAS SINCE MERGED, as `6364e22`, so this file is no longer blocked and the
corpus gate it depends on is now satisfiable from one tree. It is documented in
`test/pytest/TESTS.md` section 9, and `test/selftest/350` checks that document
against disk -- with this file undocumented the gate was RED, measured, `got
[74 6] want [86 7]`. Those four integers are what the gate said THEN, on a tree
holding #897 and this file and nothing else; #903 has since added four harness
tests, so the same gate now reads 90 in 7. The old pair is left as the record of
the run rather than regenerated, for the reason a corrected number dies when the
summary is rebuilt from the pre-fix text: a count belongs to the revision it
counted. The SHA pins above are kept on the same footing -- the record of what was
tested when, not a live dependency.
identical to the bash suite's, from a different harness and a different way of
reading the counters -- 241/118, 351/209, 588/402, 1624/1313, and z/h of
2.0424, 1.6794, 1.4627, 1.2369.

    pytest --pg-config .../bin/pg_config --pgc-expect-tests 18 \
           test_hilbert_locality.py

AND THE BLOCKING DEPENDENCY IS WIDER THAN test/pytest/. #897 also adds
`pgc_build_and_install` to test/lib.sh, which `pgc_cluster.build_and_install`
drives. With this file dropped beside #897's test/pytest/ but main's lib.sh, the
session ends before any test runs:

    RuntimeError: pgcolumnar failed to build or install from ...
    bash: line 1: pgc_build_and_install: command not found

So "blocked on #897" means the whole of #897, not only the directory.

THE REFUSAL THIS FILE ONCE COULD NOT CARRY, AND NOW DOES

The bash suite REFUSES to report a ratio when the two partitions are not
different: the sixteen measurement arms print UNRUN and pgc_summary counts
them as a third state, "16 unrunnable"; with nothing else red the suite exits
67, INCOMPLETE. Here the same refusal goes
through `expect.cannot_run("UNMET_PRECONDITION", ...)`, and the layer records
the third state and reports it.

THE PARAGRAPH BELOW WAS TRUE WHEN WRITTEN AND IS NOT ANY MORE. It is kept as history
because the measurement is the valuable part: it is the record of what the gap
actually was, and it should outlive the gap.

What was true at 5f3dedb: `cannot_run` set `self.unrunnable` and nothing read
it, so declaring a test unrunnable made it PASS. Measured against that head,
with the fixture mutated to lay BOTH arms out with cluster(): "1 failed, 17
passed", and all four test_groups_read_over_sixty_placements cases were among
the greens while asserting nothing about groups read. Only
test_the_two_partitions_differ reddened, and it reddened for its own reason.

#897 CLOSED IT, and this file therefore DOES carry the bash suite's refusal.
Re-measured here on the merged tree rather than taken from the merge:

    UNRUN  test_p.py::test_cannot: ABSENT_FIXTURE: no corpus
    checks unrunnable: 1
    exit code = 67

Read that exit code and that count, NOT pytest's own tally, which still prints
"1 passed" for the item. The per-item outcome is the thing that has not
changed; the session-level refusal is what PGC_EXIT_INCOMPLETE=67 means in the
bash suite and it is what this run produces. A reader who greps for "passed"
will reach the wrong conclusion, which is how this paragraph came to be stale
and then to be reported fixed while it was not.

WHAT THIS IS A PORT OF, AND WHAT IT IS FOR

test/hilbert_locality.sh builds one fixture, lays it out twice -- once with
pgcolumnar.cluster() (Z-order) and once with pgcolumnar.cluster_hilbert() --
and counts the engine's own "Columnar Chunk Groups Read" over 60 deterministic
window placements at each of four window sizes. Read that file first: its
header carries the reasoning, the three defects the shape exists to avoid, the
account of which arm catches which regression, and the list of things the
measurement does NOT claim.

HOW FAITHFUL THE PORT IS, MEASURED RATHER THAN CLAIMED

Every assertion here carries the bash check's name string verbatim, prefixes
included, so `test/pytest/compare_to_bash.py` can diff the two by property. RUN
ON THIS PAIR IT STILL EXITS 1: 30 bash checks, 30 distinct names on this side,
13 reported missing. None of the 13 is a property this file fails to assert,
and both reasons are worth knowing before trusting the comparator:

  1. ELEVEN OF THE THIRTEEN INTERPOLATE A SHELL VARIABLE. The comparator reads
     SOURCE literals, so bash's "box $box: ..." can never equal this file's
     f-string "box {box}: ...", and crun's one literal, "premise: $1 ran
     without raising", stands for six runtime names. The names the two
     harnesses PRINT are identical; the source literals cannot be.
  2. THE OTHER TWO ARE THE COMPARATOR'S OWN EXTRACTOR. It is a regular
     expression that takes the FIRST string literal in an expect() call and
     stops at the first closing parenthesis. For expect.text(got, WANT, NAME)
     the first literal is the expected TEXT, so the comparator reports
     "different" and "IDENTICAL" as assertion names and calls the two arms
     behind them missing. The same shape makes it report "Columnar Usable Skip
     Predicates" and "Columnar Chunk Groups Total" as names.

Taking the bash prefixes verbatim took the missing count from 27 to 23, and
hoisting nested calls out of the expect() arguments -- so the extractor has a
clear shot at the name -- took it from 23 to 13. Closing the rest is a change
to compare_to_bash.py -- parse with `ast`, take the LAST string-literal
argument of each expect() call, and compare interpolated names after expansion
-- and that file belongs to #897. Until it lands, "the two can be compared
mechanically" is an aspiration, not a fact about this pair.

WHAT THIS PORT DOES NOT CLAIM

It does not claim the two harnesses agree by construction. They agree because
both were run and both produced 241/118, 351/209, 588/402 and 1624/1313 -- and
if one day they disagree, that disagreement is the finding, not a merge
conflict to resolve by editing one of them.

THREE MECHANISMS DIFFER FROM THE BASH ARM, ON PURPOSE

  1. THE COUNTERS ARE READ AS TYPED JSON, NOT GREPPED. The bash suite runs
     EXPLAIN in text and pulls integers out with sed. Here it is
     `EXPLAIN (..., FORMAT JSON)`, which psycopg hands back as parsed Python, so
     "Columnar Chunk Groups Read" is a key holding an int. A regex that matched
     the wrong line, or that matched nothing and yielded the empty string, is
     not a failure mode this arm has.

  2. THE COLUMNAR-SCAN PREMISE GOES THROUGH expect.plan_marker, not a grep for
     a node name. `Custom Plan Provider == "PgColumnarScan"` is NOT the same
     question: the vectorized aggregate node reuses the scan's registered
     methods and reports the same provider, so that predicate says yes for a
     plan with no columnar scan in it. `plan_marker` asks for the
     "Columnar Projected Columns" key, which only the scan's explain callback
     emits -- the faithful port of `pgc_is_columnar_scan`.

  3. THE FIXTURE IS BUILT ONCE PER MODULE, not once per test. It is 200,000
     rows and six clustering operations. See the note on `locality` below for
     what that costs in isolation and why it is still the right trade.
"""

import pytest

# THE PRIVATE IMPORT IS DELIBERATE AND IT IS A GAP, NOT A PREFERENCE.
#
# The vacuity layer walks an EXPLAIN JSON tree in `_plan_nodes`, and exposes no
# public way to READ a counter out of one -- `plan_marker` asserts a key is
# present and `plan_node` returns a node matched by type or provider, but this
# suite's whole subject is the VALUE under "Columnar Chunk Groups Read". The
# alternatives were to reimplement the traversal (a second copy of the tree
# shape, free to drift from the layer's) or to import the layer's own. Importing
# it keeps one traversal in the tree. See the report accompanying this file:
# `expect.counter(plan, key)` belongs in the layer.
from pgc_vacuity import _plan_nodes

# ---- the fixture's constants, identical to the bash suite --------------------

ROWS = 200_000
SPAN = 100_000

# NOT DYADIC, ON PURPOSE. 200000/1500 is 133.33, so no group boundary lines up
# with a power of two. The dyadic case is where the two curves cut the same
# blocks, and it is kept as a control (test_dense_dyadic_grid_is_one_partition)
# rather than allowed to become the fixture by accident.
STRIPE_ROWS = 1500
GROUPS = 134

PLACEMENTS = 60
STRIDE_X = 997
STRIDE_Y = 7919

# The pins: box -> (Z-order total, Hilbert total, margin floor) over the 60
# placements. Measured on main f2af080, PG 18.4, and reproduced bit-for-bit by
# the bash suite and by this one.
#
# EXACT INTEGERS AND NOT A THRESHOLD. A threshold is the thing someone lowers
# when it reddens; it survives a regression that costs half the benefit, and it
# survives it quietly. An exact pin cannot be lowered without saying so.
#
# WHAT THE EIGHT INTEGERS ACTUALLY CATCH, MEASURED. The bash suite's header
# carries the full account and it is the one to read; in short, a CHANGED CURVE
# is caught by the digest pins above, which move on every run the integers move
# on and sit upstream of them, and the integers' own domain is a CHANGED READER
# at an unchanged layout -- refusing to skip odd-numbered row groups left both
# digests exactly at their pins and moved all eight integers. So do not read
# these eight as the arm that guards the curve.
#
# THE MARGIN FLOOR BESIDE EACH PIN IS NOT THE THRESHOLD REJECTED ABOVE. It is
# about 88% of the measured ratio, so it names a COLLAPSE of the benefit rather
# than a layout that moved. It is here because "Hilbert reads fewer groups" is
# satisfied by a win of one group in four thousand: the reader regression above
# keeps that arm GREEN at every box while z/h falls from 2.0424 to 1.0149.
PINS = {
    2000: (241, 118, "1.80"),
    5000: (351, 209, "1.48"),
    12000: (588, 402, "1.28"),
    30000: (1624, 1313, "1.10"),
}

# The partition digests the pins were measured over.
DIGEST_ZORDER = "2169ae4551d8"
DIGEST_HILBERT = "1706e49ef5a2"

DENSE_SIDE = 256
DENSE_STRIPE_ROWS = 1024

# The deterministic generator. hashint8 rather than random(): the pilot ran a
# random() INSERT once per table, so the two arms held DIFFERENT DATA and the
# ratio was a fact about the data rather than about the curve.
SRC_INSERT = f"""
INSERT INTO src (a, b, pad)
SELECT (hashint8(g) % {SPAN} + {SPAN}) % {SPAN},
       (hashint8(g * 2654435761) % {SPAN} + {SPAN}) % {SPAN},
       repeat('x', 40)
FROM generate_series(1, {ROWS}) g
"""

# THE PARTITION DIGEST, ORDER-INDEPENDENT BY CONSTRUCTION.
#
# One string per row group from BOTH columns' min and max, and then those
# strings SORTED before hashing. What is hashed is the SET of group boxes, so
# two layouts that cut the same boxes hash equal however the curve numbered
# them. Ordering the groups by group_number instead -- the obvious first
# version -- makes the digest report the NUMBERING too, so it says "different"
# for two identical partitions and the dense control below passes its premise
# and goes on to report a ratio. That is the defect this form exists to avoid.
#
# vector_index = -1 is the row-group-level zone map: the box of the whole group,
# which is what decides whether the group can be skipped.
DIGEST_SQL = """
SELECT substr(md5(string_agg(g, ',' ORDER BY g)), 1, 12) FROM (
    SELECT string_agg(column_index::text || ':' ||
                      encode(minimum, 'hex') || ':' ||
                      encode(maximum, 'hex'), '/' ORDER BY column_index) AS g
    FROM pgcolumnar.zone_map
    WHERE storage_id = pgcolumnar.get_storage_id(%s)
      AND vector_index = -1 AND column_index IN (0, 1)
    GROUP BY group_number) t
"""

# Order-blind, exactly like pgc_set_hash in test/lib.sh, and that is right here:
# the arms are supposed to differ in ORDER and in nothing else, so an
# order-sensitive oracle would report a difference for the property under test.
#
# AN EMPTY RELATION YIELDS A QUERY_ERROR SENTINEL, NOT THE STRING 'EMPTY'.
# pgc_set_hash returns EMPTY and the bash suite lists EMPTY as unmeasured in
# pgc_measured(). The layer does not: `expect.hash` refuses a value that starts
# with QUERY_ERROR, and its `_empty()` is `v is None or len(v) == 0`, which the
# five-character string EMPTY is not. Measured on two genuinely empty columnar
# relations, hashed by the oracle in its old form: the arm PASSED, comparing
# EMPTY with EMPTY. In its form below the same pair is REFUSED -- "the left side
# is a failed query". So two empty relations would have satisfied the first
# premise of this file and now cannot. Shaping
# the sentinel like the one the layer already refuses puts one guard over both
# cases instead of asking the layer for a second one.
SET_HASH_SQL = """
SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)),
                'QUERY_ERROR.empty-relation')
FROM (SELECT r::text AS t FROM {table} r) s
"""


# ---- helpers ----------------------------------------------------------------


def _scalar(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return row[0] if row else None


def _digest(conn, table):
    """The partition digest, or a sentinel that no comparison can accept.

    A helper that returned None here would let two FAILED reads compare equal
    and pass an equality arm, and would satisfy an INEQUALITY arm outright --
    which matters, because the arm that licenses the whole measurement is an
    inequality. `expect.hash` refuses a QUERY_ERROR sentinel outright, so the
    sentinel is the layer's own.
    """
    value = _scalar(conn, DIGEST_SQL, (table,))
    return value if value else f"QUERY_ERROR.no-partition-for-{table}"


def _verdict(a, b):
    """different | IDENTICAL | UNMEASURED[...] -- the bash suite's differs().

    THE LAYER HAS NO INEQUALITY RECORDER. `expect` compares for equality
    (num, text, hash, rows), asserts a floor (at_least), and asserts a plan key.
    "these two differ" has to become "the verdict about these two equals the
    string 'different'", which is what this function is for. Computing the
    verdict here rather than writing `assert a != b` is not decoration: a bare
    Python assert is not COUNTED by the vacuity layer, so a test that concluded
    only that way would be failed for asserting nothing.
    """
    for label, value in (("a", a), ("b", b)):
        if not value or str(value).startswith("QUERY_ERROR") or value == "EMPTY":
            return f"UNMEASURED[{label}={value}]"
    return "different" if a != b else "IDENTICAL"


def _plan(conn, sql):
    """The ANALYZEd plan as parsed JSON."""
    return _scalar(
        conn, f"EXPLAIN (ANALYZE, TIMING OFF, COSTS OFF, FORMAT JSON) {sql}")


def _counter(plan, key):
    """One Columnar counter out of a plan, or None when no node carries it.

    None rather than 0: `expect.num` refuses a non-number, so a counter that was
    never reported fails the arm instead of being summed as nothing.
    """
    for node in _plan_nodes(plan):
        if key in node:
            return node[key]
    return None


# The six layout verbs, and how each one ended. WRITTEN BY THE `locality`
# FIXTURE, READ BY test_every_layout_verb_ran_without_raising.
#
# THE PORT OF crun(). A cluster verb that RAISED would leave its table simply
# unclustered -- and an unclustered table has a partition, a group count and a
# plan, so it reddens the pins below as though the CURVE had changed. The bash
# suite runs every verb through crun(), which names the verb and its SQLSTATE.
# Letting the exception out of the fixture instead would abort the module with
# an error that names no property, so the outcome is recorded here and asserted
# by name, exactly as bash does it.
_VERB_STATES = {}


def _verb(conn, what, sql):
    """Run one layout verb and record how it ended, under the bash suite's name."""
    import psycopg

    try:
        conn.execute(sql)
        _VERB_STATES[what] = "noerror"
    except psycopg.Error as exc:
        _VERB_STATES[what] = exc.sqlstate or "UNKNOWN"


def _window(table, ox, oy, box):
    """The query under measurement. One shape, one place, so the arms and the
    premises cannot drift into asking about two different queries."""
    return (f"SELECT count(*) FROM {table} "
            f"WHERE a BETWEEN {ox} AND {ox + box} "
            f"AND b BETWEEN {oy} AND {oy + box}")


def _origins(box):
    """The 60 placements. Two coprime strides over the span, so the origins
    neither repeat nor march in step with the group boundaries, and the same 60
    are used for both arms and every box."""
    span = SPAN - box
    return [((i * STRIDE_X) % span, (i * STRIDE_Y) % span)
            for i in range(1, PLACEMENTS + 1)]


def _groups_read(conn, table, box):
    """(placements measured, groups read) over the 60 windows.

    BOTH numbers are returned. The sum alone cannot distinguish a genuinely
    small number of groups from a run that measured fewer windows than it was
    asked to -- and a truncated run reads as a BETTER result, which is the
    direction in which a defect here would go unnoticed.
    """
    measured, total = 0, 0
    for ox, oy in _origins(box):
        value = _counter(_plan(conn, _window(table, ox, oy, box)),
                         "Columnar Chunk Groups Read")
        if isinstance(value, int):
            measured += 1
            total += value
    return measured, total


# ---- the fixture ------------------------------------------------------------


@pytest.fixture(scope="module")
def locality(pgc_cluster):
    """The whole fixture, built once for the module.

    MODULE SCOPE, NOT THE `pgc_conn` FIXTURE, AND THAT IS A COMPROMISE. This
    fixture is 200,000 rows and six clustering operations; per test it would be
    paid fifteen times over. `pgc_conn` is function-scoped -- it gives every
    test a private schema, which is the isolation this harness is built on --
    and a module-scoped fixture cannot depend on a function-scoped one, so this
    opens its own connection and makes its own schema by hand, duplicating what
    `pgc_conn` does. See the report accompanying this file: a module- or
    session-scoped sibling of `pgc_conn` is the missing piece.

    SET max_parallel_workers_per_gather = 0, ON THIS CONNECTION, IS NOT TUNING.
    Every digest below is a claim about physical group boundaries and every
    count is read out of a plan; a parallel plan divides that work across
    workers, so parallelism off is what makes the counters a fact about the
    layout rather than about scheduling. The bash suite says this once, in the
    server's config file, through PGC_EXTRA_CONF. `pgc_cluster.py` hard-codes
    its postgresql.conf and has no equivalent hook, so it is a SET on the one
    connection every query in this module runs through -- which is why the
    module hands out a single connection rather than one per test.
    """
    import psycopg

    conn = psycopg.connect(pgc_cluster.dsn(), autocommit=True)
    schema = "pgc_hilbert_locality"
    try:
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        conn.execute(f'CREATE SCHEMA "{schema}"')
        conn.execute(f'SET search_path TO "{schema}", public')
        conn.execute("SET max_parallel_workers_per_gather = 0")

        # The data is materialised ONCE, into a heap table, and both arms load
        # from it. Two independent evaluations of the generator is how the arms
        # come to hold different rows.
        conn.execute("CREATE TABLE src (a int, b int, pad text)")
        conn.execute(SRC_INSERT)

        for table in ("hz", "hh", "cz1", "cz2"):
            conn.execute(
                f"CREATE TABLE {table} (a int, b int, pad text) USING pgcolumnar")
            conn.execute("SELECT pgcolumnar.set_options(%s, stripe_row_limit => %s)",
                         (table, STRIPE_ROWS))
            conn.execute(f"INSERT INTO {table} SELECT * FROM src")

        # hz is Z-order, hh is Hilbert. cz1 and cz2 are the control: the same
        # verb twice, so an "the partitions differ" arm cannot be satisfied by
        # an instrument that reports any two tables as different.
        _verb(conn, "cluster() on the Z-order arm",
              "SELECT pgcolumnar.cluster('hz', 'a', 'b')")
        _verb(conn, "cluster_hilbert() on the Hilbert arm",
              "SELECT pgcolumnar.cluster_hilbert('hh', 'a', 'b')")
        _verb(conn, "cluster() on control table cz1",
              "SELECT pgcolumnar.cluster('cz1', 'a', 'b')")
        _verb(conn, "cluster() on control table cz2",
              "SELECT pgcolumnar.cluster('cz2', 'a', 'b')")

        # The dense dyadic grid the design predicted the two curves would agree
        # on: 256 x 256 cells, 1024 rows to a group, 64 full groups.
        conn.execute("CREATE TABLE dsrc (a int, b int)")
        conn.execute(
            f"INSERT INTO dsrc SELECT (g - 1) / {DENSE_SIDE}, (g - 1) % {DENSE_SIDE} "
            f"FROM generate_series(1, {DENSE_SIDE * DENSE_SIDE}) g")
        for table in ("dz", "dh"):
            conn.execute(f"CREATE TABLE {table} (a int, b int) USING pgcolumnar")
            conn.execute("SELECT pgcolumnar.set_options(%s, stripe_row_limit => %s)",
                         (table, DENSE_STRIPE_ROWS))
            conn.execute(f"INSERT INTO {table} SELECT * FROM dsrc")
        _verb(conn, "cluster() on the dense control",
              "SELECT pgcolumnar.cluster('dz', 'a', 'b')")
        _verb(conn, "cluster_hilbert() on the dense control",
              "SELECT pgcolumnar.cluster_hilbert('dh', 'a', 'b')")

        yield conn
    finally:
        try:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            conn.close()


@pytest.fixture(scope="module")
def digests(locality):
    """The two partitions under measurement, read once."""
    return _digest(locality, "hz"), _digest(locality, "hh")


# =============================================================================
# ARM 1  PREMISE: THE TWO ARMS HOLD THE IDENTICAL ROW MULTISET
# =============================================================================


def test_every_layout_verb_ran_without_raising(locality, expect):
    """bash: the six `crun` premises, 'premise: WHAT ran without raising'.

    A CALL THAT RAISED LEAVES A TABLE MERELY UNCLUSTERED, and an unclustered
    table has a partition, a group count and a plan -- so it reddens the pins
    below as though the CURVE had changed. Six named arms, one per verb, so the
    output says which verb failed and with what SQLSTATE rather than leaving a
    reader to infer it from a moved integer.
    """
    for what in ("cluster() on the Z-order arm",
                 "cluster_hilbert() on the Hilbert arm",
                 "cluster() on control table cz1",
                 "cluster() on control table cz2",
                 "cluster() on the dense control",
                 "cluster_hilbert() on the dense control"):
        expect.text(_VERB_STATES.get(what, "NOT RUN"), "noerror",
                    f"{what} ran without raising")


def test_the_source_holds_the_rows_both_arms_will_load(locality, expect):
    """bash: 'premise: the source holds the rows both arms will load'.

    EXACT, NOT A FLOOR. Both arms load from this one heap table, so a source
    that is short is a fixture that is short on both arms at once -- which is
    the shape the arms' own comparison cannot see.
    """
    src_rows = _scalar(locality, "SELECT count(*) FROM src")
    expect.num(src_rows, ROWS,
               "premise: the source holds the rows both arms will load")


def test_both_arms_hold_the_identical_row_multiset(locality, expect):
    """bash: 'premise: the two arms hold the identical row multiset' and the
    three arms that follow it.

    Without this every number below is a fact about two different tables.
    """
    hz = _scalar(locality, SET_HASH_SQL.format(table="hz"))
    hh = _scalar(locality, SET_HASH_SQL.format(table="hh"))
    src = _scalar(locality, SET_HASH_SQL.format(table="src"))
    expect.hash(hh, hz, "premise: the two arms hold the identical row multiset")
    # And that multiset is the source's. Comparing the arms only to each other
    # passes if both loads went equally wrong.
    expect.hash(hz, src,
                "premise: and it is the source's multiset, so neither load dropped rows")
    # A hash of nothing equals a hash of nothing. The oracle returns a
    # QUERY_ERROR sentinel for an empty relation and `expect.hash` refuses it
    # outright -- but a refusal is not a NAMED arm, and the bash suite has one,
    # so the same property is asserted here by name as well.
    hz_is_a_measurement = 0 if str(hz).startswith("QUERY_ERROR") else 1
    expect.num(hz_is_a_measurement, 1,
               "premise: and it is a hash of rows, not of an empty or failed read")
    # EXACT, NOT TWO FLOORS. `at_least(count, ROWS)` on each arm is satisfied by
    # an arm that was loaded TWICE, which is a fixture defect that would move
    # every number below it. The bash suite pins the sum; so does this.
    total = _scalar(locality,
                    "SELECT (SELECT count(*) FROM hz) + (SELECT count(*) FROM hh)")
    expect.num(total, ROWS * 2, "premise: both arms hold every source row")


def test_the_fixture_is_two_dimensional(locality, expect):
    """bash: 'premise: column a spans the square' and its sibling.

    A generator that collapsed one column to a handful of values would make
    both curves the identity in that dimension and the comparison meaningless,
    and it would do so silently.
    """
    spans = {}
    for column in ("a", "b"):
        spans[column] = _scalar(
            locality,
            f"SELECT (min({column}) < {SPAN // 100} "
            f"AND max({column}) > {SPAN - SPAN // 100})::int FROM src")
    # UNROLLED, NOT LOOPED, because the two bash checks have two different
    # names and a loop can only produce one. The names are the bash suite's,
    # so compare_to_bash.py can match them.
    expect.num(
        spans["a"], 1,
        "premise: column a spans the square, so this is a two-dimensional fixture")
    expect.num(spans["b"], 1, "premise: column b spans the square too")


def test_both_arms_have_the_group_count_measured(locality, expect):
    """bash: 'premise: hz has the group count this measurement was taken at'.

    The fixture is only the fixture that was measured if the groups are the
    size they were measured at: 200,000 rows at 1,500 to a group is 134, the
    last one short.
    """
    counts = {}
    for table in ("hz", "hh"):
        counts[table] = _scalar(
            locality,
            "SELECT count(*) FROM pgcolumnar.row_group "
            "WHERE storage_id = pgcolumnar.get_storage_id(%s)",
            (table,))
    expect.num(counts["hz"], GROUPS,
               "premise: hz has the group count this measurement was taken at")
    expect.num(
        counts["hh"], counts["hz"],
        "premise: hh has the same group count, so a group is the same unit on both arms")


# =============================================================================
# ARM 2  PREMISE: THE TWO PARTITIONS ARE TWO DIFFERENT PARTITIONS
# =============================================================================


def test_the_two_partitions_differ(digests, expect):
    """bash: 'premise: the Z-order and Hilbert partitions differ', and the two
    pins under it.

    This is what licenses the measurement to attribute its difference to the
    curve. Pinned as well as merely different: "different" is satisfied by any
    change to either layout, while these two values say the layouts are the
    ones the pinned integers were measured over. A moved digest beside moved
    integers is a layout change; moved integers beside these digests would be a
    change in the reader.
    """
    dz, dh = digests
    verdict = _verdict(dz, dh)
    expect.text(verdict, "different",
                "the Z-order and Hilbert partitions differ")
    expect.text(dz, DIGEST_ZORDER,
                "the Z-order partition is the one these numbers were measured over")
    expect.text(dh, DIGEST_HILBERT,
                "the Hilbert partition is the one these numbers were measured over")


# =============================================================================
# ARM 3  CONTROL: SAME CURVE TWICE -> THE DIGESTS ARE IDENTICAL
# =============================================================================


def test_two_tables_on_the_same_curve_are_one_partition(locality, digests, expect):
    """bash: 'control: two tables on the same curve have the identical partition'.

    The removal proof for the arm above. An inequality passes for any
    instrument that reports "different" too readily -- including one that is
    really reporting "these are two different tables". Two tables built the
    same way from the same source and laid out by the SAME verb must hash
    EQUAL, and that hash must be the measured Z-order arm's, so the control is
    a control on THIS fixture rather than on an unrelated one.
    """
    dz, _ = digests
    cz1 = _digest(locality, "cz1")
    cz2 = _digest(locality, "cz2")
    expect.hash(cz1, cz2,
                "control: two tables on the same curve have the identical partition")
    # THROUGH THE VERDICT, like its sibling and like every digest comparison in
    # the bash suite. `expect.hash` refuses a QUERY_ERROR sentinel, so the hole
    # the bash arm had -- two failed reads comparing equal -- is closed there by
    # the layer; the verdict is used anyway so that the two arms of this control
    # are read the same way.
    control_verdict = _verdict(cz1, dz)
    expect.text(control_verdict, "IDENTICAL",
                "control: and that partition is the measured Z-order arm's")


# =============================================================================
# ARM 4  CONTROL: A DENSE DYADIC GRID -> THE TWO CURVES AGREE
# =============================================================================


def test_dense_dyadic_grid_is_one_partition(locality, expect):
    """bash: 'control: on a dense dyadic grid the two curves cut the identical
    partition'.

    The degenerate case the design predicted: over a dense grid whose side is a
    power of two, cut into groups whose size is a power of two, the two curves
    visit the same blocks in a different ORDER and produce the same set of
    group boxes. It is here so the suite carries its own counterexample to
    "Hilbert always changes the layout", and so a reader can see that arm 2's
    "different" is a measurement rather than a foregone conclusion.
    """
    duplicated_cells = _scalar(
        locality,
        "SELECT count(*) FROM (SELECT a, b FROM dsrc "
        "GROUP BY a, b HAVING count(*) <> 1) x")
    dense_groups = _scalar(
        locality,
        "SELECT count(*) FROM pgcolumnar.row_group "
        "WHERE storage_id = pgcolumnar.get_storage_id('dz')")
    ddz = _digest(locality, "dz")
    ddh = _digest(locality, "dh")
    expect.num(
        duplicated_cells, 0,
        "control premise: the dense grid is dense -- every cell present exactly once")
    expect.num(
        dense_groups, DENSE_SIDE * DENSE_SIDE // DENSE_STRIPE_ROWS,
        "control premise: the dense grid has the dyadic group count")
    expect.hash(
        ddz, ddh,
        "control: on a dense dyadic grid the two curves cut the identical partition")


# =============================================================================
# ARM 5  PREMISE: BOTH ARMS PLAN AS A COLUMNAR SCAN
# =============================================================================


def test_both_arms_plan_as_a_columnar_scan(locality, expect):
    """bash: 'premise: the Z-order arm plans as a columnar scan'.

    plan_marker, not a provider name. The vectorized aggregate node reuses the
    scan's registered methods and reports the same Custom Plan Provider, so
    that predicate says yes for a plan with no columnar scan in it.
    "Columnar Projected Columns" is emitted only by the scan's explain
    callback.

    AND THE HELPER UNDER THIS ARM IS NOT ITSELF PINNED. #897's
    test_guards_pinned.py pins num, text, at_least, plan_node, outcomes,
    cannot_run and hash, and has no plan_marker case. Measured on #897 head
    5f3dedb merged into this branch: replacing plan_marker's present-arm raise
    with `pass` leaves test_hilbert_locality.py, test_connection.py,
    test_guards_pinned.py and test_layer.py all green, 50 passed. So the arms
    below rest on a helper that has no removal proof of its own; pin it in
    #897 before reading them as strong.
    """
    box = 2000
    ox, oy = _origins(box)[0]
    pz = _plan(locality, _window("hz", ox, oy, box))
    ph = _plan(locality, _window("hh", ox, oy, box))
    # UNROLLED, AND THE NAMES ARE PLAIN LITERALS. A loop can carry only one
    # name, and an f-string is invisible to compare_to_bash.py's extractor, so
    # both arms would drop out of the port's name list.
    expect.plan_marker(pz, "Columnar Projected Columns",
                       name="premise: the Z-order arm plans as a columnar scan")
    expect.plan_marker(ph, "Columnar Projected Columns",
                       name="premise: the Hilbert arm plans as a columnar scan")


def test_parallelism_is_off_so_a_counter_is_a_fact_about_the_layout(locality, expect):
    """bash: 'parallelism is off on the connection every arm runs through'. The
    bash suite sets it in the server's config through PGC_EXTRA_CONF and reads it
    back; here it is a SET on one connection.

    Here it is a SET on one connection, so it is asserted rather than assumed.
    A parallel plan divides the scan across workers and the counters below
    would be a fact about scheduling.
    """
    expect.text(_scalar(locality, "SHOW max_parallel_workers_per_gather"), "0",
                "parallelism is off on the connection every arm runs through")


# =============================================================================
# ARMS 6-7  PREMISES ABOUT THE COUNTERS' OWN PLAN
# =============================================================================


@pytest.mark.parametrize("box", sorted(PINS))
def test_the_predicates_are_usable_and_the_denominators_match(locality, expect, box):
    """bash: 'premise: box N, the Z-order arm can skip on all four predicates'
    and the two arms after it.

    FOUR, NOT TWO. A BETWEEN is two scan keys, >= and <=, and the reader builds
    a skip predicate from each; two BETWEENs over two columns are four usable
    predicates. A "> 0" bound would accept an arm that had lost one whole
    dimension -- which is the dimension the curve is about.

    "Pushed-Down Filters" is deliberately not the number checked: it counts what
    the reader was HANDED, and a key it could not build a skip predicate from
    excludes no group at all while still being reported as pushed down (#477).

    And the denominators are compared to EACH OTHER, not to a literal: a
    literal would still be satisfied if both arms drifted together, and the
    property is that a ratio is taken between two equal denominators.
    """
    ox, oy = _origins(box)[0]
    pz = _plan(locality, _window("hz", ox, oy, box))
    ph = _plan(locality, _window("hh", ox, oy, box))

    expect.num(_counter(pz, "Columnar Usable Skip Predicates"), 4,
               f"premise: box {box}, the Z-order arm can skip on all four predicates")
    expect.num(_counter(ph, "Columnar Usable Skip Predicates"), 4,
               f"premise: box {box}, the Hilbert arm can skip on all four predicates")
    expect.num(_counter(ph, "Columnar Chunk Groups Total"),
               _counter(pz, "Columnar Chunk Groups Total"),
               f"premise: box {box}, both arms have the same number of groups to read")
    expect.num(_counter(pz, "Columnar Chunk Groups Total"), GROUPS,
               f"premise: box {box}, that denominator is the whole relation")


# =============================================================================
# ARM 8  THE MEASUREMENT
# =============================================================================


@pytest.mark.parametrize("box", sorted(PINS))
def test_groups_read_over_sixty_placements(locality, digests, expect, box):
    """bash: 'box N: groups read over 60 placements, Z-order' and the three
    arms beside it.

    Reported only if the partitions differ. With no difference in layout there
    is nothing for a ratio to be about, and printing one anyway is how the
    pilot produced numbers for the dense grid.
    """
    dz, dh = digests
    verdict = _verdict(dz, dh)
    want_z, want_h, floor = PINS[box]
    if verdict != "different":
        # BY THE SHELL'S FOUR NAMES, NOT ONE ANONYMOUS DECLINE (#1339 :792): the bash
        # suite records each of the four arms as unrunnable by name, and a refusal
        # that names none of them leaves the two harnesses with different records.
        why = (f"the two partitions are not different ({verdict}), so a ratio "
               f"between them is not about the curve")
        expect.cannot_run("UNMET_PRECONDITION", why,
                          name=f"box {box}: groups read over {PLACEMENTS} placements, Z-order")
        expect.cannot_run("UNMET_PRECONDITION", why,
                          name=f"box {box}: groups read over {PLACEMENTS} placements, Hilbert")
        expect.cannot_run("UNMET_PRECONDITION",
                          f"the two partitions are not different ({verdict})",
                          name=f"box {box}: Hilbert reads fewer groups than Z-order")
        expect.cannot_run("UNMET_PRECONDITION",
                          f"the two partitions are not different ({verdict})",
                          name=f"box {box}: and it wins by the margin measured, z/h at least {floor}")
        return

    measured_z, total_z = _groups_read(locality, "hz", box)
    measured_h, total_h = _groups_read(locality, "hh", box)

    # EVERY PLACEMENT WAS MEASURED. A run that lost windows would sum fewer
    # groups and read as a better result on whichever arm lost them.
    expect.num(measured_z, PLACEMENTS,
               f"box {box}: all {PLACEMENTS} placements reported a Z-order group count")
    expect.num(measured_h, PLACEMENTS,
               f"box {box}: all {PLACEMENTS} placements reported a Hilbert group count")

    expect.num(total_z, want_z,
               f"box {box}: groups read over {PLACEMENTS} placements, Z-order")
    expect.num(total_h, want_h,
               f"box {box}: groups read over {PLACEMENTS} placements, Hilbert")

    # And, separately from the pins: the curve still wins here.
    #
    # A DIFFERENCE, BECAUSE THE LAYER HAS NO STRICT INEQUALITY. `at_least` is
    # the only bound it offers and it refuses a floor of zero, so "h < z" is
    # written as "z - h is at least 1", which asserts exactly that and nothing
    # weaker.
    expect.at_least(total_z - total_h, 1,
                    f"box {box}: Hilbert reads fewer groups than Z-order")

    # AND IT STILL WINS BY THE MARGIN IT WAS MEASURED AT.
    #
    # "z - h is at least 1" is satisfied by a win of ONE group in four
    # thousand, so on its own it cannot tell "the layout changed" from "the
    # benefit is gone". Refusing to skip odd-numbered row groups in the reader
    # moves all eight pins, keeps the arm above GREEN at every box, and takes
    # z/h from 2.0424 to 1.0149. This arm is what reddens there. The ratio is
    # computed once and both this arm and the line printed below use that one
    # value, so the number asserted and the number reported cannot drift.
    ratio = total_z / total_h if total_h else 0.0
    expect.text(
        "at or above the floor" if ratio >= float(floor)
        else f"BELOW THE FLOOR (z/h={ratio:.4f})",
        "at or above the floor",
        f"box {box}: and it wins by the margin measured, z/h at least {floor}")

    # The ratio is PRINTED as well, and asserted nowhere beyond the floor above:
    # a ratio can be held constant by both arms getting worse together.
    print(f"-- box {box}: z={total_z} h={total_h} z/h={ratio:.4f}")
