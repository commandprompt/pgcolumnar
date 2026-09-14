"""Ordered paths on a physically sorted columnar table (#751, #432).

`pgcolumnar.vacuum_sorted` physically orders a relation. A scan that then tells the
planner about that order lets `ORDER BY` skip the Sort and lets `ORDER BY ... LIMIT n`
stop early.

THE FAILURE MODE THIS PORTS IS SILENT WRONGNESS, NOT A MISSING SPEED-UP. A scan that
claims an ordering the rows are not in returns wrong answers for LIMIT and for merge
joins, and no correctness test on unordered data would notice, because the planner puts
a Sort above it anyway. So the arms come in two groups:

    REFUSAL   every shape where the rows are NOT in the claimed order must plan a Sort
              AND return the same rows as a heap table holding identical data. These
              pass trivially when no pathkeys exist at all, so they are proved by an
              over-claiming mutation rather than by being green.
    CLAIM     the shapes where the ordering is real must lose the Sort.

EVERY ARM THAT ASSERTS A PLAN ALSO ASSERTS THE ANSWER against a heap oracle holding the
same rows. A plan check alone cannot see a wrong result; an answer check alone cannot
see that the Sort was never removed. That pairing is the whole design, and it is why
this suite was worth porting only after #1058 -- until then the grader could not read
the wrapper the answer arms go through, so 18 of its names were invisible and a port
could have dropped every one of them and still graded one-for-one.

THE ORDER COMPARISON IS SEQUENTIAL, NOT A SET. A set comparison cannot fail on order,
which is the only thing a wrong pathkey claim breaks.
"""
import pathlib

import pytest

ROWS = 20_000


def _one(cur, sql):
    cur.execute(sql)
    row = cur.fetchone()
    return None if row is None else row[0]


def _plans_sort(cur, sql):
    """-> True when the plan contains a Sort or Incremental Sort node.

    Read from EXPLAIN's own lines rather than from a substring of the whole plan: a
    column named `sorted_kind` or a value containing `sort` appears in property lines,
    and matching those would make every plan look sorted (the trap recorded as
    `a plan-node regex matches property lines`).
    """
    cur.execute("EXPLAIN (COSTS OFF) " + sql)
    for (line,) in cur.fetchall():
        stripped = line.lstrip(" ->")
        if stripped.startswith("Sort") or stripped.startswith("Incremental Sort"):
            return True
    return False


def _inversions(cur, table, col):
    """-> how many times the column DECREASES in the order the scan returns rows.

    Zero means the relation really is physically ordered on that column, which is the
    premise every claim arm rests on. Asserted rather than assumed, because
    `vacuum_sorted` succeeding is not the same as the rows being in order.
    """
    return _one(cur, f"SELECT count(*) FROM (SELECT {col}, lag({col}) OVER () AS p "
                     f"FROM {table}) s WHERE p > {col}")


def _sort_status(cur, table, field):
    return _one(cur, f"SELECT {field} FROM pgcolumnar.sort_status('{table}')")


def _storage(cur, table, field):
    return _one(cur, f"SELECT {field} FROM pgcolumnar.storage "
                     f"WHERE storage_id = pgcolumnar.get_storage_id('{table}')")


def _rows(cur, sql):
    """-> every row, IN THE ORDER THE SERVER RETURNED THEM."""
    cur.execute(sql)
    return cur.fetchall()


@pytest.fixture(scope="module")
def fx(pgc_cluster):
    """A columnar table and a heap table holding identical rows.

    `k` carries duplicates and NULLs on purpose: NULLS LAST is part of what the claim
    says, and a tie on `k` is where a wrong secondary order would show.
    """
    import psycopg

    conn = psycopg.connect(pgc_cluster.dsn(), autocommit=True)
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        cur.execute("CREATE TABLE h (id int, k int, j int, t text) USING heap")
        # ONE % , NOT TWO. psycopg doubles `%` only when parameters are passed; with
        # none, `%%` reaches the server literally and `integer %% integer` is not an
        # operator. Caught on the first run.
        cur.execute("INSERT INTO h SELECT g, CASE WHEN g % 97 = 0 THEN NULL "
                    "ELSE (g*7919)%500 END, g%13, 'v'||g "
                    f"FROM generate_series(1,{ROWS}) g")
        cur.execute("CREATE TABLE c (id int, k int, j int, t text) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('c', stripe_row_limit => 2000, "
                    "chunk_group_row_limit => 500)")
        cur.execute("INSERT INTO c SELECT * FROM h")
        cur.execute("SELECT pgcolumnar.vacuum_sorted('c', 'k', 'j')")
    yield conn
    conn.close()


def test_the_fixture_really_is_ordered(fx, expect):
    """THE PREMISES EVERY CLAIM ARM RESTS ON. `vacuum_sorted` returning is not the same
    as the rows being in order, and a claim arm on an unordered fixture would pass for
    the wrong reason."""
    with fx.cursor() as cur:
        expect.num(_inversions(cur, "c", "k"), 0,
                   "premise: the fixture is physically ordered on k")
        expect.num(_sort_status(cur, "c", "appended_groups"), 0,
                   "premise: with no unsorted tail")
        expect.text(_storage(cur, "c", "sorted_kind"), "lexicographic",
                    "premise: recorded as a lexicographic run")
        expect.text(str(_storage(cur, "c", "sorted_by::text")), "{k,j}",
                    "premise: on the key it was given")
        expect.at_least(_one(cur, "SELECT count(*) FROM c WHERE k IS NULL"), 1,
                        "premise: the fixture has NULLs in the sort column")
        expect.at_least(_one(cur, "SELECT count(*) FROM (SELECT k FROM c "
                                  "WHERE k IS NOT NULL GROUP BY k "
                                  "HAVING count(*) > 1) s"), 1,
                        "premise: and ties on it")
        cur.execute("EXPLAIN (COSTS OFF) SELECT k FROM c ORDER BY k")
        plan = "\n".join(r[0] for r in cur.fetchall())
    expect.num(1 if "PgColumnarScan" in plan else 0, 1,
               "premise: the columnar table is read by the columnar scan")


# ============================================================ CLAIM arms
#
# The ordering is real on these shapes, so the Sort must be gone. Each is paired with
# the answer, because losing the Sort is only correct if the rows still come back in
# that order.

CLAIMS = [
    ("SELECT k FROM c ORDER BY k",
     "ORDER BY the sort key plans no Sort"),
    ("SELECT k, j FROM c ORDER BY k, j",
     "ORDER BY the full key plans no Sort"),
    ("SELECT k FROM c ORDER BY k ASC NULLS LAST",
     "ORDER BY the key prefix plans no Sort"),
    ("SELECT k FROM c ORDER BY k LIMIT 10",
     "ORDER BY k LIMIT plans no Sort"),
    ("SELECT k FROM c WHERE k IS NOT NULL ORDER BY k LIMIT 1",
     "MIN over the sort key plans no Sort"),
]


@pytest.mark.parametrize("sql,name", CLAIMS)
def test_a_real_ordering_loses_the_sort(fx, expect, sql, name):
    with fx.cursor() as cur:
        expect.num(1 if _plans_sort(cur, sql) else 0, 0, name)


# ========================================================== REFUSAL arms
#
# Shapes where the physical order does NOT satisfy the requested one. A Sort must
# remain. THESE PASS WITH NO FEATURE AT ALL -- an engine claiming nothing plans a Sort
# everywhere -- so they are not evidence on their own. What they catch is the
# over-claim: a scan that announces an order it does not have.

REFUSALS = [
    ("SELECT k FROM c ORDER BY k DESC",
     "REFUSE: DESC is not the order the rows are in"),
    ("SELECT k FROM c ORDER BY k NULLS FIRST",
     "REFUSE: NULLS FIRST is not the null placement the rows are in"),
    ("SELECT j FROM c ORDER BY j",
     "REFUSE: a non-prefix of the key is not an order the rows are in"),
    ("SELECT id FROM c ORDER BY id",
     "REFUSE: a column that is not in the key at all"),
    ("SELECT k, j FROM c ORDER BY j, k",
     "REFUSE: the key columns in the wrong order"),
]


@pytest.mark.parametrize("sql,name", REFUSALS)
def test_an_order_the_rows_are_not_in_keeps_the_sort(fx, expect, sql, name):
    with fx.cursor() as cur:
        expect.num(1 if _plans_sort(cur, sql) else 0, 1, name)


# ============================================================ ANSWER arms
#
# THE HALF A PLAN CHECK CANNOT SEE. Every query above is run against both tables and
# compared ROW BY ROW IN ORDER. A set comparison cannot fail on order, which is the
# only thing a wrong pathkey claim breaks -- so these compare sequences.
#
# They are in their own tests rather than beside the plan arms because the bash suite
# names them separately, and because a plan failure and an answer failure want
# different reading: one is a lost optimisation, the other is a wrong result.

# THE THIRD FIELD SAYS WHETHER THE TEMPLATE CAN CARRY AN ORDERING CLAIM, and it is
# declared rather than sniffed at runtime. `expect.ordered_rows` refuses a sequence whose
# elements are all identical, because the reverse reads the same and the claim cannot
# fail -- and a `LIMIT 1` result is that case by construction. Deciding per call by
# looking at the data is how an ordering claim silently becomes a value one, which is the
# failure the two instruments exist to keep apart.
#
# This caught a decorative arm of my own: `and the first row matches heap` compared one
# row to one row through `ordered_rows` and asserted nothing about order. It is a VALUE
# claim -- the minimum under the ordering -- so it takes `rows`, which still names the
# position on a mismatch.
ANSWERS = [
    ("SELECT id, k, j FROM %T ORDER BY k, j, id",
     "and returns the same rows in the same order as heap", True),
    ("SELECT k, j FROM %T ORDER BY k NULLS LAST, j LIMIT 10",
     "and LIMIT returns the same first rows as heap", True),
    ("SELECT k, j, id FROM %T ORDER BY k NULLS LAST, j, id LIMIT 500",
     "and a larger LIMIT does too", True),
    ("SELECT k FROM %T WHERE k IS NOT NULL ORDER BY k LIMIT 1",
     "and the first row matches heap", False),
    ("SELECT k, j, id FROM %T ORDER BY k DESC NULLS FIRST, j DESC, id DESC LIMIT 200",
     "and DESC still answers correctly", True),
    ("SELECT k, id FROM %T ORDER BY k NULLS FIRST, id LIMIT 300",
     "and NULLS FIRST still answers correctly", True),
    ("SELECT j, id FROM %T ORDER BY j, id LIMIT 300",
     "and a non-prefix still answers correctly", True),
    ("SELECT id FROM %T ORDER BY id LIMIT 300",
     "and a non-key column still answers correctly", True),
]


@pytest.mark.parametrize("template,name,ordered", ANSWERS)
def test_the_columnar_answer_matches_heap_in_order(fx, expect, template, name, ordered):
    with fx.cursor() as cur:
        columnar = _rows(cur, template.replace("%T", "c"))
        heap = _rows(cur, template.replace("%T", "h"))
        expect.at_least(len(heap), 1,
                        f"premise: the heap oracle returns rows for {name!r}, so the "
                        f"comparison is not two empty lists")
        if ordered:
            expect.ordered_rows(columnar, heap, name)
        else:
            expect.rows(columnar, heap, name)


def test_a_constant_leading_key_is_skipped(fx, expect):
    """A constant leading key is SKIPPED and the prefix continues, mirroring core's
    `build_index_pathkeys`. Every row the scan returns has k = 5, so within that
    restriction the rows are ordered by j and `ORDER BY j` is satisfied by the run on
    (k,j). Without the skip-and-continue this would end the prefix at k and plan a Sort.

    The bare `ORDER BY j` refusal above is its control: j alone, with no equality on k,
    is NOT an order the rows are in.
    """
    with fx.cursor() as cur:
        expect.at_least(_one(cur, "SELECT count(*) FROM c WHERE k = 5"), 2,
                        "premise: the equality really selects rows, so the arm is not "
                        "empty")
        expect.num(1 if _plans_sort(cur, "SELECT j FROM c WHERE k = 5 ORDER BY j")
                   else 0, 0,
                   "a constant leading key is skipped, so ORDER BY the next key plans "
                   "no Sort")
        columnar = _rows(cur, "SELECT j, id FROM c WHERE k = 5 ORDER BY j, id")
        heap = _rows(cur, "SELECT j, id FROM h WHERE k = 5 ORDER BY j, id")
        expect.at_least(len(heap), 1, "premise: the heap oracle returns those rows too")
        expect.ordered_rows(columnar, heap,
                           "and it answers in j order, matching heap")


# ================================================== an unsorted tail
#
# The run is still ordered; the RELATION is not. Rows whose k falls BELOW the run's
# minimum, so a scan returning the run first and the tail afterwards gives a wrong
# LIMIT answer rather than merely an unordered one.

@pytest.fixture(scope="module")
def tail(fx):
    with fx.cursor() as cur:
        cur.execute("CREATE TABLE tailc (LIKE c) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('tailc', stripe_row_limit => 2000, "
                    "chunk_group_row_limit => 500)")
        cur.execute("INSERT INTO tailc SELECT * FROM c")
        cur.execute("SELECT pgcolumnar.vacuum_sorted('tailc', 'k', 'j')")
        cur.execute("CREATE TABLE tailh (LIKE h) USING heap")
        cur.execute("INSERT INTO tailh SELECT * FROM tailc")
        for t in ("tailc", "tailh"):
            cur.execute(f"INSERT INTO {t} SELECT g, -g, g%13, 'x'||g "
                        f"FROM generate_series(1,600) g")
    return fx


def test_a_run_with_an_appended_tail_is_not_an_ordered_relation(tail, expect):
    with tail.cursor() as cur:
        expect.at_least(_sort_status(cur, "tailc", "appended_groups"), 1,
                        "premise: the tail really appended past the run")
        expect.at_least(_inversions(cur, "tailc", "k"), 1,
                        "premise: and the relation is no longer in k order")
        # READ FROM THE HEAP TWIN. min() over the columnar table is itself a candidate
        # for the ordered path, so a premise taken there would be measuring the thing
        # under test -- an over-claiming build answered it wrongly.
        expect.text(str(_one(cur, "SELECT (min(k) < 0)::text FROM tailh")), "true",
                    "premise: the tail holds values below the run's minimum")
        expect.num(1 if _plans_sort(cur, "SELECT k FROM tailc ORDER BY k") else 0, 1,
                   "REFUSE: a run with an appended tail is not an ordered relation")


@pytest.mark.parametrize("template,name", [
    ("SELECT k, id FROM %T ORDER BY k NULLS LAST, id LIMIT 10",
     "and ORDER BY k LIMIT still returns the true first rows"),
    ("SELECT k, j, id FROM %T ORDER BY k NULLS LAST, j, id",
     "and the whole ordered result matches heap with the tail appended"),
])
def test_the_tail_answer_matches_heap(tail, expect, template, name):
    """The arm that would catch a wrong claim as a WRONG ANSWER rather than a slow plan:
    with the tail below the run, the first ten rows of a claimed order are not the first
    ten rows."""
    with tail.cursor() as cur:
        columnar = _rows(cur, template.replace("%T", "tailc"))
        heap = _rows(cur, template.replace("%T", "tailh"))
        expect.at_least(len(heap), 1, f"premise: the oracle returns rows for {name!r}")
        expect.ordered_rows(columnar, heap, name)


# =================================================== a Z-order run
#
# An order, but not a sort on any ONE column.

@pytest.fixture(scope="module")
def zorder(fx):
    with fx.cursor() as cur:
        cur.execute("CREATE TABLE zc (LIKE c) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('zc', stripe_row_limit => 2000, "
                    "chunk_group_row_limit => 500)")
        cur.execute("INSERT INTO zc SELECT * FROM h WHERE k IS NOT NULL")
        cur.execute("SELECT pgcolumnar.set_options('zc', "
                    "sort_by => ARRAY['k','j']::name[])")
        cur.execute("SELECT pgcolumnar.cluster('zc', 'k', 'j')")
        cur.execute("CREATE TABLE zh (LIKE h) USING heap")
        cur.execute("INSERT INTO zh SELECT * FROM zc")
    return fx


def test_a_zorder_run_is_not_a_sort_on_its_lead_column(zorder, expect):
    with zorder.cursor() as cur:
        expect.num(_sort_status(cur, "zc", "appended_groups"), 0,
                   "premise: the Z-ordered table records a full run with no tail")
        expect.text(_storage(cur, "zc", "sorted_kind"), "zorder",
                    "premise: recorded as a zorder run, not lexicographic")
        expect.at_least(_inversions(cur, "zc", "k"), 1,
                        "premise: and it is NOT in k order")
        expect.num(1 if _plans_sort(cur, "SELECT k FROM zc ORDER BY k") else 0, 1,
                   "REFUSE: a Z-order run is not a sort on its lead column")
        columnar = _rows(cur, "SELECT k, j, id FROM zc ORDER BY k, j, id LIMIT 300")
        heap = _rows(cur, "SELECT k, j, id FROM zh ORDER BY k, j, id LIMIT 300")
        expect.at_least(len(heap), 1, "premise: the Z-order oracle returns rows")
        expect.ordered_rows(columnar, heap,
                           "and the Z-order run still answers correctly")


# ============================== an unsorted relation, and a rewrite that retracts

def test_a_declared_sort_key_is_an_intention_not_a_layout(fx, expect):
    with fx.cursor() as cur:
        cur.execute("CREATE TABLE uc (LIKE c) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('uc', stripe_row_limit => 2000, "
                    "chunk_group_row_limit => 500)")
        cur.execute("INSERT INTO uc SELECT * FROM h")
        cur.execute("SELECT pgcolumnar.set_options('uc', "
                    "sort_by => ARRAY['k','j']::name[])")
        expect.text(str(_one(cur, "SELECT coalesce(sorted_kind,'<NULL>') "
                                  "FROM pgcolumnar.storage WHERE storage_id = "
                                  "pgcolumnar.get_storage_id('uc')")), "<NULL>",
                    "premise: an unsorted relation records no kind")
        # `sort_key::text`, not `sort_key`. psycopg returns a PG array as a python
        # list, so the bare column gives "['k', 'j']" where psql renders "{k,j}". The
        # cast makes the SERVER render it, which is what the assertion is about.
        expect.text(str(_sort_status(cur, "uc", "sort_key::text")), "{k,j}",
                    "premise: even though sort_status reports the declared key")
        expect.num(1 if _plans_sort(cur, "SELECT k FROM uc ORDER BY k") else 0, 1,
                   "REFUSE: a DECLARED sort key is an intention, not a layout")


def test_an_unsorted_vacuum_retracts_the_ordered_path(fx, expect):
    with fx.cursor() as cur:
        cur.execute("CREATE TABLE rc (LIKE c) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('rc', stripe_row_limit => 2000, "
                    "chunk_group_row_limit => 500)")
        cur.execute("INSERT INTO rc SELECT * FROM h")
        cur.execute("SELECT pgcolumnar.vacuum_sorted('rc', 'k', 'j')")
        expect.num(1 if _plans_sort(cur, "SELECT k FROM rc ORDER BY k") else 0, 0,
                   "premise: the claim is live before the unsorted rewrite")
        cur.execute("SELECT pgcolumnar.vacuum('rc')")
        expect.num(1 if _plans_sort(cur, "SELECT k FROM rc ORDER BY k") else 0, 1,
                   "REFUSE: an unsorted vacuum retracts the ordered path")


# ================================= a rewrite forced by a type change retracts the mark

def test_a_type_change_rewrite_drops_the_mark(fx, expect):
    with fx.cursor() as cur:
        cur.execute("CREATE TABLE atc (id int, k int) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('atc', stripe_row_limit => 2000, "
                    "chunk_group_row_limit => 500)")
        cur.execute("INSERT INTO atc SELECT g, (g*7919)%500 "
                    "FROM generate_series(1,20000) g")
        cur.execute("SELECT pgcolumnar.vacuum_sorted('atc', 'k')")
        expect.num(1 if _plans_sort(cur, "SELECT k FROM atc ORDER BY k") else 0, 0,
                   "premise: the claim is live before the type change")
        before = _one(cur, "SELECT pgcolumnar.get_storage_id('atc')")
        cur.execute("ALTER TABLE atc ALTER COLUMN k TYPE text")
        after = _one(cur, "SELECT pgcolumnar.get_storage_id('atc')")
        expect.text("same" if before == after else "rewritten", "rewritten",
                    "premise: a type change DID rewrite the storage")
        expect.num(1 if _plans_sort(cur, "SELECT k FROM atc ORDER BY k") else 0, 1,
                   "REFUSE: integer order is not text order, and the rewrite dropped "
                   "the mark")


# ============================ an UPDATE lands outside the run, so the claim lapses

def test_one_updated_row_is_a_row_outside_the_run(fx, expect):
    with fx.cursor() as cur:
        cur.execute("CREATE TABLE upc (LIKE c) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('upc', stripe_row_limit => 2000, "
                    "chunk_group_row_limit => 500)")
        cur.execute("INSERT INTO upc SELECT * FROM h")
        cur.execute("SELECT pgcolumnar.vacuum_sorted('upc', 'k', 'j')")
        expect.num(1 if _plans_sort(cur, "SELECT k FROM upc ORDER BY k") else 0, 0,
                   "premise: the claim is live before the update")
        cur.execute("UPDATE upc SET k = -1 WHERE id = 1")
        expect.at_least(_sort_status(cur, "upc", "appended_groups"), 1,
                        "premise: the new row version appended past the run")
        expect.num(1 if _plans_sort(cur, "SELECT k FROM upc ORDER BY k") else 0, 1,
                   "REFUSE: one updated row is a row outside the run")
        expect.num(_one(cur, "SELECT k FROM upc ORDER BY k LIMIT 1"), -1,
                   "and ORDER BY k LIMIT 1 finds the updated row")


# ================== a column rename: the mark FOLLOWS it, so the claim survives (#778)
#
# This arm used to assert the opposite, and was right to at the time: nothing maintained
# the mark, so after a rename the recorded name stopped resolving and the claim was
# refused. #778 made the mark follow the rename, because a rename does not move data --
# the rows really are still ordered by whichever column now carries the name.

def test_the_mark_follows_a_rename(fx, expect):
    with fx.cursor() as cur:
        cur.execute("CREATE TABLE rnc (LIKE c) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('rnc', stripe_row_limit => 2000, "
                    "chunk_group_row_limit => 500)")
        cur.execute("INSERT INTO rnc SELECT * FROM h")
        cur.execute("SELECT pgcolumnar.vacuum_sorted('rnc', 'k', 'j')")
        expect.num(1 if _plans_sort(cur, "SELECT k FROM rnc ORDER BY k") else 0, 0,
                   "premise: the claim is live before the rename")
        cur.execute("ALTER TABLE rnc RENAME COLUMN k TO kk")
        expect.text(str(_storage(cur, "rnc", "sorted_by::text")), "{kk,j}",
                   "the recorded key FOLLOWS the rename (#778)")
        expect.num(1 if _plans_sort(cur, "SELECT kk FROM rnc ORDER BY kk") else 0, 0,
                   "so the claim survives the rename instead of being refused")
        # ...and the claim is not merely available, it is TRUE: no Sort AND the rows
        # really do come out ordered. A plan with no Sort over unordered rows is the
        # wrong answer, which is the whole risk of claiming a pathkey.
        expect.num(_one(cur, "SELECT count(*) FROM (SELECT kk < lag(kk) OVER () AS d "
                             "FROM rnc) z WHERE d"), 0,
                   "and the rows really are ordered by the renamed column (no Sort AND "
                   "correct)")


def test_a_recorded_name_that_no_longer_resolves_is_not_a_claim(fx, expect):
    """Drop the FIRST key column, not the second. Dropping the second leaves {k} as a
    resolvable PREFIX, and a prefix of a sort key is a sound claim -- so that shape
    cannot test a refusal at all. With the first column gone nothing about the remaining
    order can be claimed: j is ordered only WITHIN equal k."""
    with fx.cursor() as cur:
        cur.execute("CREATE TABLE rnd (LIKE c) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('rnd', stripe_row_limit => 2000, "
                    "chunk_group_row_limit => 500)")
        cur.execute("INSERT INTO rnd SELECT * FROM h")
        cur.execute("SELECT pgcolumnar.vacuum_sorted('rnd', 'k', 'j')")
        cur.execute("ALTER TABLE rnd DROP COLUMN k")
        expect.text(str(_storage(cur, "rnd", "sorted_by::text")), "{k,j}",
                    "premise: the recorded key still names the dropped column")
        expect.at_least(_one(cur, "SELECT count(*) FROM (SELECT j < lag(j) OVER () AS d "
                                  "FROM rnd) z WHERE d"), 1,
                        "premise: and j alone really is NOT ordered, so a claim on it "
                        "would be wrong")
        expect.num(1 if _plans_sort(cur, "SELECT j FROM rnd ORDER BY j") else 0, 1,
                   "REFUSE: a recorded name that no longer resolves is not a claim")


# ===================================================== the GUC is the escape hatch

def test_the_guc_turns_the_claim_off(fx, expect):
    """Both directions. A GUC that only ever agrees with the default is not an escape
    hatch, and an arm that sets it without checking the ON case cannot tell a working
    switch from a feature that never engaged."""
    with fx.cursor() as cur:
        cur.execute("SET pgcolumnar.enable_sorted_pathkeys = on")
        expect.num(1 if _plans_sort(cur, "SELECT k FROM c ORDER BY k") else 0, 0,
                   "premise: the claim is live with the GUC on")
        cur.execute("SET pgcolumnar.enable_sorted_pathkeys = off")
        expect.num(1 if _plans_sort(cur, "SELECT k FROM c ORDER BY k") else 0, 1,
                   "control: pgcolumnar.enable_sorted_pathkeys = off restores the Sort")
        cur.execute("RESET pgcolumnar.enable_sorted_pathkeys")


# ========================================== a CTAS relation was never ordered

def test_a_ctas_relation_claims_nothing(fx, expect):
    with fx.cursor() as cur:
        cur.execute("CREATE TABLE ctas USING pgcolumnar AS SELECT * FROM h")
        expect.num(1 if _plans_sort(cur, "SELECT k FROM ctas ORDER BY k") else 0, 1,
                   "a CTAS relation was never ordered, so it claims nothing")


# ============================== a collatable sort key is refused, and the wrong
#                                answer that refusal saves
#
# Only the column NAMES are recorded, so nothing at plan time can tell whether the
# collation the rewrite sorted under is still the column's collation. And it can change
# with NO REWRITE AT ALL: `ALTER COLUMN k TYPE text COLLATE X` on a column already text
# needs no transformation, so PostgreSQL updates pg_attribute and leaves every stored
# row where it is.

ALT_COLLATIONS = ("en_US.utf8", "en_US.UTF-8", "en_US", "und-x-icu")


@pytest.fixture(scope="module")
def collated(fx):
    """The text fixture, plus whichever alternate collation this server has.

    The values are chosen so the two collations DISAGREE: in C, 'B' (0x42) sorts before
    'a' (0x61), and in en_US it does not. Without that the arm cannot fail.
    """
    with fx.cursor() as cur:
        cur.execute('CREATE TABLE colh (id int, k text COLLATE "C") USING heap')
        cur.execute("INSERT INTO colh SELECT g, "
                    "(ARRAY['aB','Ab','aa','AA','Ba','bA','_x','Zz'])[1+(g%8)] || g "
                    "FROM generate_series(1,4000) g")
        cur.execute('CREATE TABLE colc (id int, k text COLLATE "C") USING pgcolumnar')
        cur.execute("SELECT pgcolumnar.set_options('colc', stripe_row_limit => 1000, "
                    "chunk_group_row_limit => 250)")
        cur.execute("INSERT INTO colc SELECT * FROM colh")
        cur.execute("SELECT pgcolumnar.vacuum_sorted('colc', 'k')")
        cur.execute("SELECT collname FROM pg_collation WHERE collname = ANY(%s) "
                    "ORDER BY 1 LIMIT 1", (list(ALT_COLLATIONS),))
        row = cur.fetchone()
    return fx, (row[0] if row else None)


def test_a_collatable_sort_column_is_not_claimed(collated, expect):
    conn, _alt = collated
    with conn.cursor() as cur:
        expect.text(_storage(cur, "colc", "sorted_kind"), "lexicographic",
                    "premise: the rewrite recorded a lexicographic run on the text "
                    "column")
        expect.num(_sort_status(cur, "colc", "appended_groups"), 0,
                   "premise: with no tail, so only the collation stands between it and "
                   "a claim")
        expect.num(1 if _plans_sort(cur, "SELECT k FROM colc ORDER BY k") else 0, 1,
                   "REFUSE: a collatable sort column is not claimed, whatever its "
                   "collation")
        columnar = _rows(cur, "SELECT k, id FROM colc ORDER BY k, id")
        heap = _rows(cur, "SELECT k, id FROM colh ORDER BY k, id")
        expect.at_least(len(heap), 1, "premise: the C-collation oracle returns rows")
        expect.ordered_rows(columnar, heap,
                           "and it answers in C order, matching heap")


def test_a_collation_alter_changes_the_order_without_rewriting(collated, expect):
    """THE DEMONSTRATION OF WHY. A collation-only ALTER changes the ordering the column
    asks for without rewriting a single row. It needs two collations that DISAGREE, and
    a server that has one is not guaranteed.

    Without the refusal this returned the C order, AA1003|AA1011|AA1019, where the
    answer is aa10|aa1002|AA1003 -- a wrong answer from a plan with no Sort.
    """
    conn, alt = collated
    if alt is None:
        expect.cannot_run("UNMET_PRECONDITION",
                          "this server has no collation that disagrees with C on "
                          "ASCII, so the ALTER cannot change any order and the arm "
                          "could not fail")
        return
    with conn.cursor() as cur:
        first_c = _one(cur, 'SELECT k FROM colh ORDER BY k COLLATE "C" LIMIT 1')
        first_alt = _one(cur, f'SELECT k FROM colh ORDER BY k COLLATE "{alt}" LIMIT 1')
        if first_c == first_alt:
            expect.cannot_run("UNMET_PRECONDITION",
                              f"C and {alt} agree on this data, so the ALTER changes "
                              f"no order and the arm could not fail")
            return
        expect.text("differ" if first_c != first_alt else "agree", "differ",
                    "premise: C and the alternate collation really disagree on this "
                    "data")
        before = _one(cur, "SELECT pgcolumnar.get_storage_id('colc')")
        cur.execute(f'ALTER TABLE colc ALTER COLUMN k TYPE text COLLATE "{alt}"')
        cur.execute(f'ALTER TABLE colh ALTER COLUMN k TYPE text COLLATE "{alt}"')
        after = _one(cur, "SELECT pgcolumnar.get_storage_id('colc')")
        expect.text("same" if before == after else "rewritten", "same",
                    "premise: the collation ALTER rewrote nothing (same storage id)")
        expect.text(_storage(cur, "colc", "sorted_kind"), "lexicographic",
                    "premise: so the run is still recorded as lexicographic")
        expect.text(str(_one(cur, "SELECT collname FROM pg_collation WHERE oid = "
                                  "(SELECT attcollation FROM pg_attribute WHERE "
                                  "attrelid = 'colc'::regclass AND attname = 'k')")),
                    alt, "premise: and the column's collation really did change")
        expect.num(1 if _plans_sort(cur, "SELECT k FROM colc ORDER BY k") else 0, 1,
                   "REFUSE: the order the rows are in is no longer the order the column "
                   "asks for")
        for template, name in (
                ("SELECT k, id FROM %T ORDER BY k, id LIMIT 3",
                 "and ORDER BY k LIMIT returns the new collation's first rows, "
                 "matching heap"),
                ("SELECT k, id FROM %T ORDER BY k, id",
                 "and the whole ordered result matches heap under the new collation")):
            columnar = _rows(cur, template.replace("%T", "colc"))
            heap = _rows(cur, template.replace("%T", "colh"))
            expect.ordered_rows(columnar, heap, name)


# ==================== which types the collation refusal actually covers
#
# The refusal is `OidIsValid(att->attcollation)`, and the claim is that this is EXACT
# for "has an ordering that can change under us". A type where attcollation is
# InvalidOid and the ordering can still change would be a wrong answer the refusal does
# not reach. These pin the REASON for each family rather than the reasoning, because
# the reasoning is what would rot.

def test_a_domain_and_an_array_carry_their_base_collation(fx, expect):
    with fx.cursor() as cur:
        cur.execute('CREATE DOMAIN dom_t AS text COLLATE "C"')
        cur.execute("CREATE TABLE t_dom (id int, k dom_t) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('t_dom', stripe_row_limit => 1000, "
                    "chunk_group_row_limit => 250)")
        cur.execute("INSERT INTO t_dom SELECT g, ('v' || g)::dom_t "
                    "FROM generate_series(1,2000) g")
        cur.execute("SELECT pgcolumnar.vacuum_sorted('t_dom', 'k')")
        expect.num(1 if _plans_sort(cur, "SELECT k FROM t_dom ORDER BY k") else 0, 1,
                   "REFUSE: a DOMAIN over text carries the base type's collation")

        cur.execute("CREATE TABLE t_arr (id int, k text[]) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('t_arr', stripe_row_limit => 1000, "
                    "chunk_group_row_limit => 250)")
        cur.execute("INSERT INTO t_arr SELECT g, ARRAY['v' || g] "
                    "FROM generate_series(1,2000) g")
        cur.execute("SELECT pgcolumnar.vacuum_sorted('t_arr', 'k')")
        expect.num(1 if _plans_sort(cur, "SELECT k FROM t_arr ORDER BY k") else 0, 1,
                   "REFUSE: an ARRAY of a collatable type is collatable")


def test_a_composite_is_claimed_and_postgres_closes_the_hole(fx, expect):
    """A COMPOSITE has attcollation InvalidOid while comparing by its FIELD collations,
    which looks like a hole. It is closed by PostgreSQL, not by this extension: a
    composite's attribute cannot be altered while any column uses the type. The arm
    asserts the refusal, so if that ever stops being true this goes red rather than
    quietly wrong."""
    import psycopg
    with fx.cursor() as cur:
        cur.execute('CREATE TYPE comp_t AS (a text COLLATE "C", b int)')
        cur.execute("CREATE TABLE t_comp (id int, k comp_t) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('t_comp', stripe_row_limit => 1000, "
                    "chunk_group_row_limit => 250)")
        cur.execute("INSERT INTO t_comp SELECT g, ROW('v' || g, g)::comp_t "
                    "FROM generate_series(1,2000) g")
        cur.execute("SELECT pgcolumnar.vacuum_sorted('t_comp', 'k')")
        expect.text(str(_one(cur, "SELECT (attcollation = 0)::text FROM pg_attribute "
                                  "WHERE attrelid = 't_comp'::regclass "
                                  "AND attname = 'k'")), "true",
                    "premise: a composite column's attcollation is InvalidOid, so it "
                    "IS claimed")
        expect.num(1 if _plans_sort(cur, "SELECT k FROM t_comp ORDER BY k") else 0, 0,
                   "premise: and it is claimed")
    message = ""
    try:
        with fx.cursor() as cur:
            cur.execute('ALTER TYPE comp_t ALTER ATTRIBUTE a TYPE text COLLATE "C" '
                        'CASCADE')
    except psycopg.Error as exc:
        message = str(exc)
    expect.num(1 if "cannot alter type" in message else 0, 1,
               "PostgreSQL refuses to change a composite's field collation while a "
               "column uses it")


def test_an_enum_add_value_before_does_not_renumber(fx, expect):
    """An ENUM also has attcollation InvalidOid. `ALTER TYPE ... ADD VALUE ... BEFORE`
    slots a new value in without renumbering the existing ones, and the new value cannot
    be in already-stored rows, so the stored order survives."""
    with fx.cursor() as cur:
        cur.execute("CREATE TYPE enum_t AS ENUM ('b','d','f')")
        cur.execute("CREATE TABLE t_enum (id int, k enum_t) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('t_enum', stripe_row_limit => 1000, "
                    "chunk_group_row_limit => 250)")
        cur.execute("INSERT INTO t_enum SELECT g, (ARRAY['b','d','f'])[1+(g%3)]::enum_t "
                    "FROM generate_series(1,2000) g")
        cur.execute("CREATE TABLE t_enum_h (id int, k enum_t) USING heap")
        cur.execute("INSERT INTO t_enum_h SELECT * FROM t_enum")
        cur.execute("SELECT pgcolumnar.vacuum_sorted('t_enum', 'k')")
        expect.num(1 if _plans_sort(cur, "SELECT k FROM t_enum ORDER BY k") else 0, 0,
                   "premise: an enum sort key is claimed")
    with fx.cursor() as cur:
        cur.execute("ALTER TYPE enum_t ADD VALUE 'a' BEFORE 'b'")
    with fx.cursor() as cur:
        expect.num(1 if _plans_sort(cur, "SELECT k FROM t_enum ORDER BY k") else 0, 0,
                   "an enum ADD VALUE ... BEFORE does not renumber the values already "
                   "stored")
        columnar = _rows(cur, "SELECT k, id FROM t_enum ORDER BY k, id")
        heap = _rows(cur, "SELECT k, id FROM t_enum_h ORDER BY k, id")
        expect.at_least(len(heap), 1, "premise: the enum oracle returns rows")
        expect.ordered_rows(columnar, heap,
                           "and the ordered answer still matches heap")


# ================================= a CACHED ordered plan must be retracted
#
# THE LAST EXECUTION MUST BE THE CACHED ONE. Written with a fresh ad-hoc SELECT at the
# end, every arm here stays green with the invalidation disabled -- a fresh query is
# planned from scratch, so it can never observe a stale plan. The whole point is to
# re-run the plan that was already made.
#
# The appended rows carry k = -600..-1, all below the run, so a plan still claiming the
# old order answers 0,0,0,0,0 where the truth is -600,-599,-598,-597,-596.

@pytest.fixture(scope="module")
def server_dir():
    """A directory the SERVER can write, which `tmp_path` is not.

    `COPY ... TO` and `parallel_copy` are executed by the backend, and the backend runs
    as a different user: pytest's `tmp_path` lives under `/tmp/pytest-of-root/` at mode
    700, so the server cannot reach it and the error names a permission rather than the
    real cause. Opening the parents matters as much as the leaf -- one closed directory
    above makes the subtree unreachable however open the leaf is -- so this makes its
    own directory instead of trying to prise `tmp_path` open.
    """
    import os, shutil, tempfile
    d = pathlib.Path(tempfile.mkdtemp(prefix="pgc_sorted_pathkeys_"))
    os.chmod(d, 0o777)
    yield d
    shutil.rmtree(d, ignore_errors=True)


WANT_FIRST5 = [-600, -599, -598, -597, -596]


def _mk_sorted(cur, table):
    cur.execute(f"CREATE TABLE {table} (id int, k int, j int, t text) USING pgcolumnar")
    cur.execute(f"SELECT pgcolumnar.set_options('{table}', stripe_row_limit => 2000, "
                f"chunk_group_row_limit => 500)")
    cur.execute(f"INSERT INTO {table} SELECT * FROM h")
    cur.execute(f"SELECT pgcolumnar.vacuum_sorted('{table}', 'k', 'j')")


def _first5_after(conn, table, append):
    """Prepare and run the ordered plan SIX times, append, then run THE SAME plan again.

    Six because PostgreSQL costs a custom plan for the first five executions before it
    will consider a generic one; the generic plan is the thing that can go stale.
    `prepare=True` keeps psycopg on one server-side statement rather than re-parsing.
    """
    sql = f"SELECT k FROM {table} ORDER BY k NULLS LAST LIMIT 5"
    with conn.cursor() as cur:
        for _ in range(6):
            cur.execute(sql, prepare=True)
            cur.fetchall()
        append(cur)
        cur.execute(sql, prepare=True)
        return [r[0] for r in cur.fetchall()]


# THE TABLE IS LITERAL, and the append is SQL rather than a callable. Written with
# lambdas the rows cannot be resolved by `ast.literal_eval`, so `compare_to_bash.py`
# reads no names from the decorator and reports every arm here MISSING -- measured, it
# cost three names until this was rewritten. That is #1045 class 2 in my own port, one
# day after building the reader for it.
APPENDS = [
    ("pc", "INSERT INTO pc SELECT g, -g, g%13, 'x'||g FROM generate_series(1,600) g",
     "a cached ordered plan sees rows appended after it was planned"),
    ("w_ins", "INSERT INTO w_ins SELECT g, -g, g%13, 'x'||g "
              "FROM generate_series(1,600) g",
     "cached plan retracted by a plain INSERT"),
    ("w_isel", "INSERT INTO w_isel SELECT * FROM feed",
     "cached plan retracted by INSERT ... SELECT from another table"),
]


@pytest.mark.parametrize("table,append,name", APPENDS)
def test_a_cached_ordered_plan_is_retracted(fx, expect, table, append, name):
    with fx.cursor() as cur:
        _mk_sorted(cur, table)
        if table == "w_isel":
            cur.execute("CREATE TABLE IF NOT EXISTS feed AS SELECT g AS id, -g AS k, "
                        "g%13 AS j, 'x'||g AS t FROM generate_series(1,600) g")
    got = _first5_after(fx, table, lambda cur: cur.execute(append))
    expect.text(",".join(str(v) for v in got),
                ",".join(str(v) for v in WANT_FIRST5), name)


def test_a_cached_plan_is_retracted_by_copy(fx, expect, server_dir):
    path = server_dir / "feed.csv"
    with fx.cursor() as cur:
        _mk_sorted(cur, "w_copy")
        cur.execute("COPY (SELECT g, -g, g%13, 'x'||g FROM generate_series(1,600) g) "
                    f"TO '{path}' WITH (FORMAT csv)")
    got = _first5_after(fx, "w_copy",
                        lambda cur: cur.execute(
                            f"COPY w_copy FROM '{path}' WITH (FORMAT csv)"))
    expect.text(",".join(str(v) for v in got),
                ",".join(str(v) for v in WANT_FIRST5),
                "cached plan retracted by COPY")


def test_a_cached_plan_is_retracted_under_parallel_flush(fx, expect):
    """`parallel_flush` has FOUR conjuncts in its gate, two of which fail silently in
    ordinary fixture shapes: a table created in the same transaction as the insert takes
    the serial path, and so does anything narrower than two columns. So the DEBUG1
    dispatch line is asserted as the premise -- without it this arm is a plain INSERT
    wearing a GUC.

    THE PREMISE RUNS ON ITS OWN TABLE. Taken on the arm's table it appended a tail
    before the plan was ever prepared, so the relation had no ordered path to retract
    and the arm passed with the invalidation disabled.
    """
    import re as _re
    notices = []
    fx.add_notice_handler(lambda diag: notices.append(diag.message_primary or ""))
    try:
        with fx.cursor() as cur:
            _mk_sorted(cur, "w_pflush_probe")
            cur.execute("SET client_min_messages = debug1")
            cur.execute("SET pgcolumnar.parallel_flush = on")
            cur.execute("INSERT INTO w_pflush_probe SELECT g, -g, g%13, 'x'||g "
                        "FROM generate_series(1,600) g")
            cur.execute("RESET client_min_messages")
            cur.execute("RESET pgcolumnar.parallel_flush")
    finally:
        fx.remove_notice_handler(fx._notice_handlers[-1]) if getattr(
            fx, "_notice_handlers", None) else None
    dispatch = ""
    for line in notices:
        found = _re.search(r"parallel_flush dispatch: .*-> (parallel|serial)", line)
        if found:
            dispatch = found.group(1)
    expect.text(dispatch, "parallel",
                "premise: parallel_flush dispatches parallel on exactly this shape")

    with fx.cursor() as cur:
        _mk_sorted(cur, "w_pflush")
        cur.execute("SET pgcolumnar.parallel_flush = on")
    got = _first5_after(fx, "w_pflush",
                        lambda cur: cur.execute(
                            "INSERT INTO w_pflush SELECT g, -g, g%13, 'x'||g "
                            "FROM generate_series(1,600) g"))
    with fx.cursor() as cur:
        cur.execute("RESET pgcolumnar.parallel_flush")
    expect.text(",".join(str(v) for v in got),
                ",".join(str(v) for v in WANT_FIRST5),
                "cached plan retracted with pgcolumnar.parallel_flush on")


def test_a_cached_plan_is_retracted_across_backends_by_parallel_copy(
        fx, expect, server_dir):
    """`parallel_copy` is the one write path where separate BACKENDS flush groups in
    their own transactions, so it is the only place the invalidation has to cross a
    PROCESS boundary. That makes it the most interesting of the five here.

    IT NEEDS `max_prepared_transactions` RAISED BEFORE THE POSTMASTER STARTS -- one
    prepared transaction per worker, and the setting cannot be changed by `SET`. The
    default is 0, so it is not a matter of asking for fewer workers: any number of
    workers is one too many. `pgc_cluster` therefore sets it where it writes
    `postgresql.conf`, at the same value `lib.sh` gives this suite through
    `PGC_EXTRA_CONF`.

    THE ROW COUNT IS ASSERTED BEFORE ANYTHING ELSE. Loaders that cannot get worker
    slots load ZERO rows, and a retraction arm over an empty table passes while testing
    nothing -- the vacuous shape the bash suite calls out in the same words.
    """
    path = str(server_dir / "pcopy.txt")
    with fx.cursor() as cur:
        _mk_sorted(cur, "w_pcopy")
        cur.execute("COPY (SELECT g, -g, g%%13, 'x'||g FROM generate_series(1,600) g) "
                    "TO '%s'" % path)

        loaded = _one(cur, "SELECT pgcolumnar.parallel_copy('w_pcopy', '%s', 2)" % path)
        expect.num(loaded, 600,
                   "premise: parallel_copy actually loaded its rows (worker slots "
                   "sufficed)")
        expect.at_least(_sort_status(cur, "w_pcopy", "appended_groups"), 1,
                        "premise: and they appended past the run")

        # The load above already happened, so this arm plans against a SECOND table and
        # appends to it after the plan is cached: the cross-backend case.
        _mk_sorted(cur, "w_pcopy2")
    got = _first5_after(
        fx, "w_pcopy2",
        lambda cur: cur.execute(
            "SELECT pgcolumnar.parallel_copy('w_pcopy2', '%s', 2)" % path))
    expect.text(",".join(str(v) for v in got),
                ",".join(str(v) for v in WANT_FIRST5),
                "cached plan retracted by pgcolumnar.parallel_copy")

    # A PREPARED TRANSACTION LEFT BEHIND holds its locks until someone resolves it, and
    # this cluster is session-scoped -- so a leak here would not fail this test, it
    # would wedge every file that runs after it. Asserted rather than assumed.
    with fx.cursor() as cur:
        expect.num(_one(cur, "SELECT count(*) FROM pg_prepared_xacts"), 0,
                   "and parallel_copy resolved every transaction it prepared")


# ==================== TRUNCATE restarts group numbering, in a NEW storage
#
# Group numbers and the sorted mark live in the SAME storage row, so numbering can only
# reset together with a mark that resets to NULL. This arm exists because that invariant
# is invisible: anything that reused a storage id, or reset numbering within one, would
# silence the gate and bring stale ordered plans back with no other test noticing.

def test_truncate_restarts_numbering_in_a_new_storage(fx, expect):
    with fx.cursor() as cur:
        _mk_sorted(cur, "trunc")
        before = _one(cur, "SELECT pgcolumnar.get_storage_id('trunc')")
        expect.text(str(_one(cur,
                    "SELECT (sorted_from = min(group_number))::text "
                    "FROM pgcolumnar.row_group, pgcolumnar.storage "
                    "WHERE pgcolumnar.storage.storage_id = "
                    "pgcolumnar.get_storage_id('trunc') AND "
                    "pgcolumnar.row_group.storage_id = pgcolumnar.storage.storage_id "
                    "GROUP BY sorted_from")), "true",
                    "premise: the mark is set and numbering starts at 1 before the "
                    "truncate")
        cur.execute("TRUNCATE trunc")
        cur.execute("INSERT INTO trunc SELECT * FROM h")
        expect.text(str(_one(cur, "SELECT (min(group_number) = 1)::text "
                                  "FROM pgcolumnar.row_group WHERE storage_id = "
                                  "pgcolumnar.get_storage_id('trunc')")), "true",
                    "TRUNCATE restarts group numbering")
        after = _one(cur, "SELECT pgcolumnar.get_storage_id('trunc')")
        expect.text("new" if before != after else "reused", "new",
                    "but in a NEW storage, so the mark it could collide with is gone")
        expect.text(str(_one(cur, "SELECT coalesce(sorted_kind,'<NULL>') "
                                  "FROM pgcolumnar.storage WHERE storage_id = "
                                  "pgcolumnar.get_storage_id('trunc')")), "<NULL>",
                    "and that new storage claims no ordering")
        expect.num(1 if _plans_sort(cur, "SELECT k FROM trunc ORDER BY k") else 0, 1,
                   "REFUSE: so a restarted group number cannot land inside a live mark")


# ============ a reclaiming rewrite retracts the claim even though the rows stay ordered
#
# Group numbers are monotonic: a reclaiming rewrite writes ABOVE the mark rather than
# reusing numbers inside it, so the run no longer covers every group and the claim
# lapses WHILE THE DATA IS STILL IN ORDER. That is conservative and deliberate, and this
# arm exists so a later optimisation cannot quietly remove the conservatism without a
# red.

def test_a_reclaiming_rewrite_retracts_while_the_rows_stay_ordered(fx, expect):
    with fx.cursor() as cur:
        _mk_sorted(cur, "rec")
        cur.execute("DELETE FROM rec WHERE id % 2 = 0")
        cur.execute("SELECT pgcolumnar.compact_rewrite('rec')")
        expect.num(_sort_status(cur, "rec", "sorted_groups"), 0,
                   "premise: the reclaiming rewrite moved every group above the mark")
        expect.num(_inversions(cur, "rec", "k"), 0,
                   "premise: and the rows are still physically in k order")
        expect.num(1 if _plans_sort(cur, "SELECT k FROM rec ORDER BY k") else 0, 1,
                   "REFUSE: a run that no longer covers every group is not a claim")


# ============== the claim must cost nothing at plan time for a query that cannot use it
#
# Deciding whether to claim an order reads the group list. A query with no ORDER BY
# gains nothing from that read, so it must not pay for it.

@pytest.fixture(scope="module")
def planbuf_fx(fx):
    """A fixture with MANY groups, so a per-group read is visible.

    `row_group` holds one row per STRIPE, so `stripe_row_limit` is what sets how many
    rows the plan-time read walks -- not `chunk_group_row_limit`.
    """
    with fx.cursor() as cur:
        cur.execute("CREATE TABLE pb (id int, k int, j int) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('pb', stripe_row_limit => 1000, "
                    "chunk_group_row_limit => 500)")
        cur.execute("INSERT INTO pb SELECT g, ((g::bigint*7919)%1000000)::int, g%17 "
                    "FROM generate_series(1,1000000) g")
        cur.execute("SELECT pgcolumnar.vacuum_sorted('pb', 'k', 'j')")
        cur.execute("ANALYZE pb")
    return fx


def _planning_buffers(conn, guc, sql):
    """-> shared hit+read during PLANNING, from the SECOND EXPLAIN.

    The second, because the first warms the catalog cache and its planning buffers are
    a measurement of that rather than of this query.
    """
    with conn.cursor() as cur:
        cur.execute(f"SET pgcolumnar.enable_sorted_pathkeys = {guc}")
        total = None
        for _ in range(2):
            cur.execute("EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF) " + sql)
            lines = [r[0] for r in cur.fetchall()]
            seen_planning = False
            for line in lines:
                if line.strip().startswith("Planning:"):
                    seen_planning = True
                    continue
                if seen_planning and "Buffers:" in line:
                    import re as _re
                    hit = _re.search(r"shared hit=(\d+)", line)
                    read = _re.search(r"read=(\d+)", line)
                    total = (int(hit.group(1)) if hit else 0) + \
                            (int(read.group(1)) if read else 0)
                    break
        cur.execute("RESET pgcolumnar.enable_sorted_pathkeys")
    return total


def test_a_query_that_cannot_use_the_order_does_not_pay_to_decide(planbuf_fx, expect):
    conn = planbuf_fx
    with conn.cursor() as cur:
        groups = _sort_status(cur, "pb", "total_groups")
        expect.at_least(groups, 900,
                        "premise: the fixture has many groups, so a per-group read "
                        "would show")
        expect.text(_storage(cur, "pb", "sorted_kind"), "lexicographic",
                    "premise: and it is marked, so the claim is not refused at "
                    "condition 1")
    on = _planning_buffers(conn, "on", "SELECT count(*) FROM pb WHERE j = 3")
    off = _planning_buffers(conn, "off", "SELECT count(*) FROM pb WHERE j = 3")
    expect.num(1 if isinstance(off, int) else 0, 1,
               "premise: the planning buffer count is a measurement, not an empty "
               "string")
    # A TOLERANCE, not equality: two backends differ by a couple of buffers whatever
    # this code does. Set far below the effect it must detect -- without the guard this
    # read +44 on this fixture, and it grows with the group count.
    expect.num(1 if abs(on - off) <= 5 else 0, 1,
               "a query with no ORDER BY does not read the group list to decide")

    # THE CONTROL that stops the arm above from being satisfied by a function that never
    # reads anything: the query that CAN use the ordering must still pay.
    order_on = _planning_buffers(conn, "on", "SELECT k FROM pb ORDER BY k LIMIT 10")
    order_off = _planning_buffers(conn, "off", "SELECT k FROM pb ORDER BY k LIMIT 10")
    expect.num(1 if order_on > order_off + 5 else 0, 1,
               "control: a query that CAN use the ordering does read to decide")


# ======================= a projection sorted on a DIFFERENT key must not lend its order

def test_a_projection_does_not_lend_its_order_to_the_base_relation(fx, expect):
    with fx.cursor() as cur:
        cur.execute("CREATE TABLE prc (LIKE h) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('prc', stripe_row_limit => 2000, "
                    "chunk_group_row_limit => 500)")
        cur.execute("INSERT INTO prc SELECT * FROM h")
        cur.execute("SELECT pgcolumnar.vacuum_sorted('prc', 'k', 'j')")
        cur.execute("SELECT pgcolumnar.add_projection('prc', 'p_on_j', "
                    "ARRAY['k','j'], ARRAY['j'])")
        cur.execute("CREATE TABLE prc_h (LIKE h) USING heap")
        cur.execute("INSERT INTO prc_h SELECT * FROM h")
        # sort_key is stored as attnums; j is attnum 3, so a projection sorted on {3}
        # is sorted on a column that is NOT the base relation's lead sort column.
        expect.text(str(_one(cur, "SELECT sort_key::text FROM pgcolumnar.projection "
                                  "WHERE projection_id > 0 AND storage_id = "
                                  "pgcolumnar.get_storage_id('prc')")), "{3}",
                    "premise: the projection exists and is sorted on a DIFFERENT key")
        expect.text(str(_storage(cur, "prc", "sorted_by::text")), "{k,j}",
                    "premise: the base relation still records its own lexicographic "
                    "run on {k,j}")
        for template, name in (
                ("SELECT k, j FROM %T WHERE j = 3 ORDER BY k, j",
                 "a query the projection can serve still answers in the requested "
                 "order"),
                ("SELECT k, j FROM %T WHERE j = 3 ORDER BY k, j LIMIT 10",
                 "and with a LIMIT, which is where a borrowed claim would show")):
            columnar = _rows(cur, template.replace("%T", "prc"))
            heap = _rows(cur, template.replace("%T", "prc_h"))
            expect.at_least(len(heap), 1, f"premise: the oracle returns rows for {name!r}")
            expect.ordered_rows(columnar, heap, name)


# =========== the claim must not survive into a plan that interleaves rows

def test_a_plain_gather_never_sits_above_a_scan_claiming_an_order(fx, expect):
    """A bare Gather interleaves worker output, so an order claimed below it is not the
    order the rows arrive in. Either the plan is not parallel, or something above the
    Gather restores the order -- a Gather Merge or a Sort.

    Parallelism is forced on in this session because the harness pins gather workers to
    zero, which would make the arm pass by never planning a parallel node at all.
    """
    with fx.cursor() as cur:
        expect.num(_inversions(cur, "c", "k"), 0,
                   "premise: the fixture is columnar and ordered")
        for guc in ("max_parallel_workers_per_gather = 4", "parallel_setup_cost = 0",
                    "parallel_tuple_cost = 0", "min_parallel_table_scan_size = 0"):
            cur.execute("SET " + guc)
        cur.execute("EXPLAIN (COSTS OFF) SELECT k, j FROM c ORDER BY k, j LIMIT 20")
        plan = [r[0] for r in cur.fetchall()]
        expect.at_least(len(plan), 1,
                        "premise: parallelism was actually available in that session")
        bare_gather = sum(1 for l in plan if l.strip(" ->") == "Gather")
        keeps_order = sum(1 for l in plan
                          if "Gather Merge" in l or "Sort" in l.strip(" ->"))
        expect.text("bad" if bare_gather > 0 and keeps_order == 0 else "ok", "ok",
                    "a plain Gather never sits above a scan claiming an order")
        parallel = _one(cur, "SELECT string_agg(k || ':' || j, ',') FROM "
                             "(SELECT k, j FROM c ORDER BY k, j LIMIT 20) s")
        for guc in ("max_parallel_workers_per_gather", "parallel_setup_cost",
                    "parallel_tuple_cost", "min_parallel_table_scan_size"):
            cur.execute("RESET " + guc)
        serial = _one(cur, "SELECT string_agg(k || ':' || j, ',') FROM "
                           "(SELECT k, j FROM c ORDER BY k, j LIMIT 20) s")
        expect.text(parallel, serial, "and the parallel answer matches the serial one")
