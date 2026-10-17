"""The differential oracle: a columnar table and a heap table must answer alike.

The governing property of `test/differential.sh`, and the reason that suite is the
largest in the tree: load the same data into both access methods and every query must
return the same thing. Heap is the oracle, so this catches encode/decode, null handling
and chunk-skipping bugs generically rather than one at a time.

**This is part 1 of that port: the type matrix.** Twenty columns covering every type the
suite exercises, at a null density that differs per column so no two columns share a null
pattern, loaded across many chunk groups and several stripes. The boundary, encoding,
bloom and aggregate parts are separate slices.

**Names are the bash suite's, character for character**, because that is what lets
`compare_to_bash.py` diff the two harnesses by property rather than by count. A port that
renames a check asserts the same thing and reports a different one.

**Why row lists and not hashes.** `lib.sh` compares `md5(string_agg(...))` because bash has
no structured result; `EMPTY` and a unique `QUERY_ERROR.N` keep an empty set apart from a
failed query there. Here `expect.row_set` compares the rows themselves, so a failure prints
the rows that differ instead of two hexadecimal strings, and the harness already refuses the
both-sides-empty comparison that a hash cannot see.

**The fixture is module-scoped and that is load-bearing.** Twelve thousand rows across twenty
columns is too slow to rebuild for each of ninety-odd assertions, so it is built once in its
own schema. That costs the `pgc_conn` write watch, so the load asserts its own row count --
the three premise arms below are not decoration, they are what `watch_writes` would otherwise
have done.
"""

import pytest

# Exactly the suite's MATRIX_DEFS, in order. The types are the point, so they are written
# out rather than generated: a generator would be a second place for the list to be wrong.
MATRIX_DEFS = """
    id        int,
    c_int     int,
    c_big     bigint,
    c_small   smallint,
    c_num     numeric(24,6),
    c_f4      real,
    c_f8      double precision,
    c_bool    boolean,
    c_date    date,
    c_ts      timestamp,
    c_tstz    timestamptz,
    c_uuid    uuid,
    c_bytea   bytea,
    c_vc      varchar(50),
    c_char    char(10),
    c_iv      interval,
    c_jsonb   jsonb,
    c_arr     int[],
    c_text    text,
    c_ztext   text
"""

# A DIFFERENT NULL MODULUS PER COLUMN, which is the fixture expressing the question: two
# columns sharing a pattern cannot distinguish a bug that keys on the pattern from one that
# keys on the column. %5, %17, %11, %3, %13, %7 and "every even row is the empty string"
# are the suite's own choices.
MATRIX_LOAD = """SELECT
    g AS id,
    CASE WHEN g%5=0  THEN NULL ELSE (g*7-100) END,
    CASE WHEN g%17=0 THEN NULL ELSE (g::bigint*1000000000) END,
    CASE WHEN g%17=0 THEN NULL ELSE ((g%100)-50)::smallint END,
    CASE WHEN g%11=0 THEN NULL ELSE (g::numeric*1.250000-1000) END,
    CASE WHEN g%17=0 THEN NULL ELSE (g*0.5-10)::real END,
    CASE WHEN g%3=0  THEN NULL ELSE (sqrt(g::float8)*(CASE WHEN g%2=0 THEN -1 ELSE 1 END)) END,
    CASE WHEN g%17=0 THEN NULL ELSE (g%3=0) END,
    CASE WHEN g%17=0 THEN NULL ELSE (DATE '2000-01-01' + g) END,
    CASE WHEN g%17=0 THEN NULL ELSE (TIMESTAMP '2000-01-01' + make_interval(hours => g)) END,
    CASE WHEN g%17=0 THEN NULL ELSE (TIMESTAMPTZ '2000-01-01 00:00:00+00' + make_interval(hours => g)) END,
    CASE WHEN g%13=0 THEN NULL ELSE (md5(g::text)::uuid) END,
    CASE WHEN g%13=0 THEN NULL ELSE decode(md5(g::text),'hex') END,
    CASE WHEN g%7=0  THEN NULL ELSE ('v'||g) END,
    CASE WHEN g%17=0 THEN NULL ELSE lpad(g::text,10,'0') END,
    CASE WHEN g%17=0 THEN NULL ELSE make_interval(days => g%1000, hours => g%24) END,
    CASE WHEN g%13=0 THEN NULL ELSE jsonb_build_object('n',g,'neg',-g,'s','x'||g) END,
    CASE WHEN g%17=0 THEN NULL ELSE ARRAY[g, g+1, -g] END,
    CASE WHEN g%7=0  THEN NULL WHEN g%500=0 THEN repeat('abc',4000) ELSE ('t'||g) END,
    CASE WHEN g%2=0  THEN '' ELSE ('z'||g) END
    FROM generate_series(1,12000) g"""

ALL_COLS = ["c_int", "c_big", "c_small", "c_num", "c_f4", "c_f8", "c_bool", "c_date",
            "c_ts", "c_tstz", "c_uuid", "c_bytea", "c_vc", "c_char", "c_iv", "c_jsonb",
            "c_arr", "c_text", "c_ztext"]

# uuid and bytea order under btree but have no min/max aggregate; the suite covers their
# ordering through the range predicates instead, and this list is why.
ORDERED = ["c_int", "c_big", "c_small", "c_num", "c_f4", "c_f8", "c_date", "c_ts",
           "c_tstz", "c_vc", "c_char", "c_iv", "c_text", "c_ztext"]

NUMERIC = ["c_int", "c_big", "c_small", "c_num", "c_f4", "c_f8"]

RANGES = {
    "c_int":   "c_int BETWEEN -50 AND 5000",
    "c_big":   "c_big > 6000000000000",
    "c_num":   "c_num BETWEEN 0 AND 5000",
    "c_f8":    "c_f8 < 0",
    "c_date":  "c_date BETWEEN DATE '2005-01-01' AND DATE '2010-01-01'",
    "c_ts":    "c_ts >= TIMESTAMP '2000-06-01'",
    "c_tstz":  "c_tstz < TIMESTAMPTZ '2000-03-01 00:00:00+00'",
    "c_vc":    "c_vc BETWEEN 'v100' AND 'v200'",
    "c_uuid":  "c_uuid > '80000000-0000-0000-0000-000000000000'::uuid",
    "c_bytea": r"c_bytea > '\x80'::bytea",
    "c_text":  "c_text > 't5000'",
}

EQUALITIES = {
    "c_int":   "c_int = 600",
    "c_vc":    "c_vc = 'v500'",
    "c_bool":  "c_bool = true",
    "c_uuid":  "c_uuid = md5('999')::uuid",
    "c_jsonb": "c_jsonb = jsonb_build_object('n',600,'neg',-600,'s','x600')",
    "c_arr":   "c_arr = ARRAY[600,601,-600]",
}


class _Pair:
    """The heap/columnar pair, and the one way to query both."""

    def __init__(self, conn):
        self.conn = conn

    def both(self, template):
        """(columnar_rows, heap_rows) for a query with %T for the table name."""
        col = self.conn.execute(template.replace("%T", "t_col")).fetchall()
        heap = self.conn.execute(template.replace("%T", "t_heap")).fetchall()
        return col, heap

    def one(self, sql):
        return self.conn.execute(sql).fetchone()[0]


@pytest.fixture(scope="module")
def matrix(pgc_cluster):
    """The type matrix, built once: 12,000 rows over twenty columns.

    Its own schema rather than `pgc_conn`'s per-test one, because the cost is the point --
    rebuilding this for each assertion would take the suite from seconds to minutes. The
    premise arms in test_the_matrix_fixture_is_what_it_claims stand in for the write watch
    that a module fixture cannot use.
    """
    import psycopg

    conn = psycopg.connect(pgc_cluster.dsn(), autocommit=True)
    schema = "pgc_differential_matrix"
    try:
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        conn.execute(f'CREATE SCHEMA "{schema}"')
        conn.execute(f'SET search_path TO "{schema}", public')
        conn.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        conn.execute(f"CREATE TABLE t_heap ({MATRIX_DEFS})")
        conn.execute(f"CREATE TABLE t_col ({MATRIX_DEFS}) USING pgcolumnar")
        # Small limits so 12,000 rows span many chunk groups and several stripes: the
        # skipping paths are only exercised when there is something to skip.
        conn.execute("SELECT pgcolumnar.set_options('t_col', "
                     "chunk_group_row_limit => 1000, stripe_row_limit => 5000)")
        # GENERATED ONCE INTO HEAP, THEN COPIED, exactly as lib.sh's load_pair does and
        # for the reason its comment gives: "both hold byte-identical logical contents
        # regardless of any volatile generators". Running the generator twice produces two
        # DIFFERENT tables the moment anything in it is volatile, and then the oracle is
        # comparing two fixtures rather than two access methods.
        #
        # Every load in parts 1 and 2 happens to be deterministic, so this was right by
        # luck rather than by construction until part 3 needed random().
        conn.execute(f"INSERT INTO t_heap {MATRIX_LOAD}")
        conn.execute("INSERT INTO t_col SELECT * FROM t_heap")
        yield _Pair(conn)
    finally:
        try:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            conn.close()


def test_the_matrix_fixture_is_what_it_claims(matrix, expect):
    """The premises the comparisons rest on, asserted rather than assumed.

    A differential suite over an empty table passes every comparison. So does one whose
    data fits in a single chunk group, while proving nothing about skipping. These three
    are the bash suite's own sanity arms, and here they also replace the `pgc_conn` write
    watch the module fixture cannot use.
    """
    expect.num(matrix.one("SELECT count(*) FROM t_col"), 12000, "matrix row count")
    # The catalogs lib.sh's chunk_group_count and stripe_count read. I guessed
    # `pgcolumnar.chunk_group` first and the server said it does not exist -- a guessed
    # catalog name is a premise arm that errors instead of asserting.
    expect.at_least(matrix.one(
        "SELECT count(*) FROM pgcolumnar.zone_map "
        "WHERE storage_id = pgcolumnar.get_storage_id('t_col') "
        "AND vector_index >= 0 AND column_index = 0"), 12, "matrix chunk groups>=12")
    expect.at_least(matrix.one(
        "SELECT count(*) FROM pgcolumnar.row_group "
        "WHERE storage_id = pgcolumnar.get_storage_id('t_col')"), 2, "matrix stripes>=2")


def test_the_whole_row_agrees_across_every_column(matrix, expect):
    """Every column of every row, in one comparison.

    The per-column arms below localise a failure; this one is what catches a bug that only
    shows when columns are read together -- a projection offset, a shared null bitmap.
    """
    col, heap = matrix.both("SELECT * FROM %T")
    expect.row_set(col, heap, "matrix whole-row")


@pytest.mark.parametrize("col", ALL_COLS)
def test_every_column_projects_counts_and_places_its_nulls(matrix, col, expect):
    """Projection, non-null count, and the positions of the nulls, per column.

    The null POSITIONS and not just the count: a decoder that loses the null bitmap's
    alignment returns the right number of nulls in the wrong rows, and a count alone
    cannot see it. Each column has its own null modulus, so the two directions
    (`IS NULL` and `IS NOT NULL`) partition that column's rows differently from every
    other column's.
    """
    c, h = matrix.both(f"SELECT id, {col} FROM %T")
    expect.row_set(c, h, f"{col} project")
    c, h = matrix.both(f"SELECT count({col}) FROM %T")
    expect.row_set(c, h, f"{col} count")
    # THE BASH ARM IS VACUOUS FOR c_ztext AND THIS PORT SAYS SO. c_ztext is
    # `CASE WHEN g%2=0 THEN '' ELSE 'z'||g END` -- never NULL, 6000 empty strings and
    # 6000 values. `diff_query "c_ztext is null"` therefore compares EMPTY against
    # EMPTY, which pgc_set_hash renders equal, and the check passes having compared
    # nothing to nothing. Measured: 0 rows IS NULL, 6000 rows = ''.
    #
    # So the assertion here is the one that can fail: the column's nulls agree AND the
    # column has nulls to agree about. Where it has none, that is stated as the
    # property instead, with the empty-string count as the positive control -- a
    # decoder that confused '' with NULL would move both numbers.
    c, h = matrix.both(f"SELECT id FROM %T WHERE {col} IS NULL")
    if col == "c_ztext":
        expect.row_set(c, h, f"{col} is null",
                       allow_empty="c_ztext is never NULL by construction; the arm below "
                                   "is what can fail")
        expect.num(matrix.one("SELECT count(*) FROM t_col WHERE c_ztext = ''"), 6000,
                   "c_ztext empty strings are not nulls")
    else:
        expect.row_set(c, h, f"{col} is null")
    c, h = matrix.both(f"SELECT id FROM %T WHERE {col} IS NOT NULL")
    expect.row_set(c, h, f"{col} not null")


@pytest.mark.parametrize("col", ORDERED)
def test_min_and_max_agree_for_every_ordered_type(matrix, col, expect):
    """min/max exercises the comparison operator the zone map also uses.

    A type whose stored min/max disagrees with its runtime comparison prunes a group that
    held a matching row, so this arm and the range arms below are the same property read
    from two directions.
    """
    c, h = matrix.both(f"SELECT min({col}), max({col}) FROM %T")
    expect.row_set(c, h, f"{col} min/max")


# Float addition is not associative, so a float sum has no single right answer: it has one
# per summation ORDER. MEASURED on heap alone, same table, three orders, full precision:
#
#     ORDER BY id        -0.27597385772197924
#     ORDER BY id DESC   -0.2759738577219848
#     ORDER BY c_f8      -0.2759738578545523
#
# Three answers from one table and one access method. So "columnar equals heap exactly" is
# false by construction for c_f4 and c_f8, and the bash suite asserts it anyway -- it passes
# because pgc_set_hash hashes the TEXT rendering, and psql's default precision rounds the
# difference away at some magnitudes and not others. Its tolerance is real, implicit, and
# magnitude-dependent.
#
# This port states the tolerance instead. Relative, because the error scales with the
# magnitude of the partial sums rather than the result: c_f8 sums to ~-0.28 out of terms of
# order 100, so an absolute bound would be either vacuous or false.
FLOAT_COLS = {"c_f4", "c_f8"}
FLOAT_RTOL = 1e-6


@pytest.mark.parametrize("col", NUMERIC)
def test_sum_and_avg_agree_for_every_numeric_type(matrix, col, expect):
    """sum and avg read every non-null value, so they catch a decode error the
    aggregates above can miss: min/max touch two rows, these touch all of them.

    Exact for the exact types -- int, bigint, smallint and numeric have no rounding, so a
    tolerance there would hide the defect this arm exists to find. Float is compared within
    a stated relative tolerance for the reason measured above the parametrize list.
    """
    c, h = matrix.both(f"SELECT sum({col}), avg({col}) FROM %T")
    if col not in FLOAT_COLS:
        expect.row_set(c, h, f"{col} sum/avg")
        return
    cs, hs = c[0], h[0]
    # TWO NUMBERS ON BOTH SIDES, not the same length (#1339). len() of a two-column row
    # is 2 whatever the data, so a columnar sum of NULL passed that arm and then raised
    # an unnamed TypeError in the tolerance below.
    numbers = all(isinstance(v, (int, float)) for v in (*cs, *hs))
    expect.text("two numbers each" if numbers and len(cs) == len(hs) == 2
                else f"columnar {cs!r} heap {hs!r}",
                "two numbers each", f"{col} sum/avg returns two numbers on both sides")
    if not numbers:
        return
    for got, want, what in zip(cs, hs, ("sum", "avg")):
        # THE MEASUREMENT BELONGS IN THE COMPARED VALUE, NOT IN THE NAME. This put
        # `got`, `want` and `rel` into the check NAME, which does carry them to the
        # reader but makes the name a different string on every run: the name is what
        # the parity grader matches, what the accounting counts, and what a ledger row
        # is keyed on, and none of those want a float in them. The name now states the
        # property and the value carries the numbers.
        rel = abs(got - want) / max(abs(want), 1e-300)
        expect.text("within tolerance" if rel <= FLOAT_RTOL
                    else f"relative {rel:.3e} (got {got!r} want {want!r})",
                    "within tolerance",
                    f"{col} sum/avg agrees on {what} within {FLOAT_RTOL} relative")
    # AND THE CONTROL: the tolerance must not be so loose that it accepts anything. A
    # decode error large enough to matter is orders of magnitude outside it, so the arm
    # asserts the bound is tight enough to have a direction.
    #
    # This compares two module constants, so it cannot move at run time -- it is a
    # drift guard against somebody loosening FLOAT_RTOL, and it fails only when the
    # source changes. Which is exactly why its failure has to say what the constant
    # became.
    expect.text("tight" if FLOAT_RTOL < 1e-3 else f"FLOAT_RTOL is {FLOAT_RTOL}",
                "tight",
                f"{col} sum/avg tolerance is tight enough to fail on a real decode error")


@pytest.mark.parametrize("col", sorted(RANGES))
def test_a_range_predicate_agrees_for_every_type_that_has_one(matrix, col, expect):
    """Range predicates are what drive chunk-group skipping.

    A wrong zone map shows here and nowhere else: the rows are present and correct, and the
    scan never looks at the group that holds them. Per type, because the min/max comparison
    is the type's own.
    """
    c, h = matrix.both(f"SELECT id FROM %T WHERE {RANGES[col]}")
    expect.row_set(c, h, f"{col} range")


@pytest.mark.parametrize("col", sorted(EQUALITIES))
def test_an_equality_predicate_agrees_for_every_type_that_has_one(matrix, col, expect):
    """Equality is the predicate a bloom filter prunes on, and the one a hash can get
    wrong for a type whose equality is not byte equality -- jsonb and int[] are here for
    that reason."""
    # `c_int = 600` MATCHES NOTHING in the bash suite and the arm still passes. c_int is
    # `g*7-100`, so 600 needs g=100, and 100%5=0 puts a NULL there. Measured: 0 rows.
    # Both sides hash to EMPTY, compare equal, and the arm cannot fail.
    #
    # The fix is a probe value that is PRESENT, so the comparison has something to be
    # wrong about. 607 needs g=101, which no null modulus touches.
    c, h = matrix.both(f"SELECT id FROM %T WHERE {EQUALITIES[col]}")
    expect.row_set(c, h, f"{col} eq",
                   allow_empty=("c_int = 600 lands on a NULL row; see the present-value "
                                "arm below" if col == "c_int" else None))
    if col == "c_int":
        c, h = matrix.both("SELECT id FROM %T WHERE c_int = 607")
        expect.row_set(c, h, "c_int eq present")


def test_an_ordered_projection_agrees_in_order(matrix, expect):
    """ORDER BY ... LIMIT, where the ORDER is part of the assertion.

    `row_set` is order-blind deliberately, which is right for every arm above and wrong
    for these two: a query that asks for an order is the only kind that can be wrong about
    one. The premise comes first -- an oracle that cannot tell forward from reverse would
    pass both arms while asserting nothing, which is `pgc_check_ordered_oracle`'s third
    assertion and the reason it exists.
    """
    fwd, _ = matrix.both("SELECT id FROM %T ORDER BY id LIMIT 25")
    rev, _ = matrix.both("SELECT id FROM %T ORDER BY id DESC LIMIT 25")
    expect.ordering_observable(fwd, rev,
                              "premise: the ordered oracle is order-sensitive")

    c, h = matrix.both("SELECT id, c_int, c_text FROM %T ORDER BY id LIMIT 25")
    expect.ordered_rows(c, h, "order limit head")
    c, h = matrix.both("SELECT id, c_num, c_vc FROM %T ORDER BY id DESC LIMIT 25")
    expect.ordered_rows(c, h, "order limit tail")


def test_a_compound_predicate_over_several_columns_agrees(matrix, expect):
    """Four columns of four types in one WHERE, which is where a per-column arm cannot
    reach: the scan combines their skip decisions, and a predicate that is right alone can
    be wrong in conjunction."""
    c, h = matrix.both("SELECT id FROM %T WHERE c_int > 0 AND c_bool "
                       "AND c_vc IS NOT NULL AND c_num < 8000")
    expect.row_set(c, h, "compound")


# ---------------------------------------------------------------------------
# Part 2: boundary conditions
#
# Part 1 asks whether the two access methods agree about DATA. This part asks whether
# they agree at the SIZES where the format's structure changes: the row counts that
# land exactly on a chunk-group or stripe limit, a table with no rows, a table with
# one, a column that is entirely NULL, a whole chunk group that is, and the empty
# string against NULL.
#
# Each fixture is built per test rather than shared, because the whole point is a
# DIFFERENT geometry each time -- the options are the subject, so a module fixture
# would have to pick one and the rest would go untested.
# ---------------------------------------------------------------------------


def _pair(conn, defs, load=None, options=None):
    """Build a heap/columnar pair with the given shape, and return a _Pair over it.

    `load` is a SELECT without INSERT, exactly as `load_pair` takes it in the bash
    suite. It is generated ONCE into heap and then copied, which is not a detail: a
    volatile generator run twice fills the two tables differently and the oracle then
    compares two fixtures instead of two access methods.
    """
    conn.execute("DROP TABLE IF EXISTS t_heap")
    conn.execute("DROP TABLE IF EXISTS t_col")
    conn.execute(f"CREATE TABLE t_heap ({defs})")
    conn.execute(f"CREATE TABLE t_col ({defs}) USING pgcolumnar")
    if options:
        conn.execute(f"SELECT pgcolumnar.set_options('t_col', {options})")
    if load:
        # ONE generation, then a copy -- see the module fixture above.
        conn.execute(f"INSERT INTO t_heap {load}")
        conn.execute("INSERT INTO t_col SELECT * FROM t_heap")
    return _Pair(conn)


def _groups(pair):
    """Chunk groups, by lib.sh's own definition in chunk_group_count."""
    return pair.one("SELECT count(*) FROM pgcolumnar.zone_map "
                    "WHERE storage_id = pgcolumnar.get_storage_id('t_col') "
                    "AND vector_index >= 0 AND column_index = 0")


def _stripes(pair):
    """Row groups, by lib.sh's own definition in stripe_count."""
    return pair.one("SELECT count(*) FROM pgcolumnar.row_group "
                    "WHERE storage_id = pgcolumnar.get_storage_id('t_col')")


def test_an_empty_table_agrees_and_the_agreement_is_not_vacuous(pgc_conn, expect):
    """Three arms on a table with no rows, and one of them cannot fail on its own.

    `empty scan` compares two empty results. `pgc_set_hash` renders both `EMPTY`, they
    compare equal, and the bash arm passes -- which is not nothing, because a columnar
    scan that invented a row would break it, but it is an assertion whose only failing
    input is a bug nobody has. The vacuity layer refuses it by default, so the reason
    is stated and a POSITIVE CONTROL is added: the same query returns a row once one
    exists, which is what proves the comparison can move at all.

    `empty count` and `empty agg` are not vacuous -- 0 and a row of NULLs are values.
    """
    p = _pair(pgc_conn, "id int, v text")
    c, h = p.both("SELECT * FROM %T")
    expect.row_set(c, h, "empty scan",
                   allow_empty="the table has no rows; the control below is what can fail")
    c, h = p.both("SELECT count(*) FROM %T")
    expect.row_set(c, h, "empty count")
    c, h = p.both("SELECT min(id), max(id), sum(id) FROM %T")
    expect.row_set(c, h, "empty agg")

    # THE CONTROL. Without it "both sides empty" is the only thing the first arm has
    # ever observed, and an oracle that always returns nothing would satisfy it.
    pgc_conn.execute("INSERT INTO t_heap VALUES (1, 'x')")
    pgc_conn.execute("INSERT INTO t_col VALUES (1, 'x')")
    c, h = p.both("SELECT * FROM %T")
    expect.row_set(c, h, "empty scan control: one row is visible to both")
    expect.num(len(c), 1, "and the control really did put a row there")


def test_a_single_row_agrees(pgc_conn, expect):
    """One row is the smallest geometry that stores anything: a stripe, a chunk group,
    and a value stream all of length one. A format that assumes a full vector anywhere
    breaks here and nowhere else in this file."""
    p = _pair(pgc_conn, "id int, v text", load="SELECT 1, 'only'")
    c, h = p.both("SELECT * FROM %T")
    expect.row_set(c, h, "single scan")
    c, h = p.both("SELECT count(*) FROM %T")
    expect.row_set(c, h, "single count")


@pytest.mark.parametrize("n", [99, 100, 101, 200, 201, 250])
def test_the_chunk_group_boundary_is_exact_and_the_data_survives_it(pgc_conn, n, expect):
    """N-1, N and N+1 around a 100-row chunk-group limit, for two limits.

    The GROUP COUNT is asserted as well as the data, because the data can agree while
    the geometry is wrong: a writer that never closes a group produces one group and
    the right rows, and only the count says so. `ceil(N/100)` is the claim, and 99,
    100 and 101 are what distinguish an off-by-one in the close from a correct one --
    exactly the boundary `test-the-exact-boundary-value` is about.
    """
    p = _pair(pgc_conn, "id int, v text",
              load=f"SELECT g, 'r'||g FROM generate_series(1,{n}) g",
              options="chunk_group_row_limit => 100, stripe_row_limit => 100000")
    expect.num(_groups(p), (n + 99) // 100, f"cg boundary N={n} groups")
    c, h = p.both("SELECT * FROM %T")
    expect.row_set(c, h, f"cg boundary N={n} scan")
    c, h = p.both(f"SELECT id FROM %T WHERE id BETWEEN 50 AND {n - 10}")
    expect.row_set(c, h, f"cg boundary N={n} range")


@pytest.mark.parametrize("n", [1000, 1001, 2000, 2001])
def test_the_stripe_boundary_is_exact_and_the_data_survives_it(pgc_conn, n, expect):
    """The same question one level up, at the 1000-row stripe limit the product allows.

    1000 is the floor `set_options` enforces, so this is the smallest legal stripe and
    the most boundaries per row. Note that it is also below one 1024-value vector,
    which #1017 measures as a compression cliff -- that is a SIZE question and this is
    a CORRECTNESS one, and the oracle here says the rows survive it either way.
    """
    p = _pair(pgc_conn, "id int, v text",
              load=f"SELECT g, 'r'||g FROM generate_series(1,{n}) g",
              options="chunk_group_row_limit => 100, stripe_row_limit => 1000")
    expect.num(_stripes(p), (n + 999) // 1000, f"stripe boundary N={n} stripes")
    c, h = p.both("SELECT * FROM %T")
    expect.row_set(c, h, f"stripe boundary N={n} scan")
    c, h = p.both("SELECT count(*), min(id), max(id), sum(id) FROM %T")
    expect.row_set(c, h, f"stripe boundary N={n} agg")


def test_a_column_that_is_entirely_null_agrees(pgc_conn, expect):
    """A column with no values at all, across several chunk groups.

    The zone map for such a column has no minimum and no maximum, and a scan that
    treats a missing range as "matches nothing" loses every row of the table rather
    than of the column. `minmax` is the arm that sees it: both sides must answer NULL,
    NULL, and a columnar side that answered anything else would be reading a range it
    does not have.
    """
    p = _pair(pgc_conn, "id int, allnull int, v text",
              load="SELECT g, NULL::int, 'r'||g FROM generate_series(1,350) g",
              options="chunk_group_row_limit => 100")
    expect.at_least(_groups(p), 4, "premise: the all-null column spans several chunk groups")
    for label, sql in (("allnull column scan",   "SELECT * FROM %T"),
                       ("allnull column count",  "SELECT count(allnull) FROM %T"),
                       ("allnull column isnull", "SELECT count(*) FROM %T WHERE allnull IS NULL"),
                       ("allnull column minmax", "SELECT min(allnull), max(allnull) FROM %T")):
        c, h = p.both(sql)
        expect.row_set(c, h, label)


def test_a_whole_chunk_group_that_is_null_agrees(pgc_conn, expect):
    """Rows 101..200 are NULL and the rest are not, with 100-row groups -- so exactly
    one group is entirely NULL and its neighbours are not.

    That is the case a column-wide NULL cannot reach: the skip decision is per group,
    so a group with no range sitting between two groups that have one is where a wrong
    "cannot match" prunes live rows. The range arm straddles it deliberately: 150..250
    starts inside the null group and ends inside the one after.
    """
    p = _pair(pgc_conn, "id int, sometimes int",
              load="SELECT g, CASE WHEN g BETWEEN 101 AND 200 THEN NULL ELSE g END "
                   "FROM generate_series(1,400) g",
              options="chunk_group_row_limit => 100")
    expect.num(p.one("SELECT count(*) FROM t_col WHERE sometimes IS NULL"), 100,
               "premise: exactly one hundred rows are null, so one whole group is")
    for label, sql in (("allnull chunk scan",   "SELECT * FROM %T"),
                       ("allnull chunk range",  "SELECT id FROM %T WHERE sometimes BETWEEN 150 AND 250"),
                       ("allnull chunk isnull", "SELECT id FROM %T WHERE sometimes IS NULL")):
        c, h = p.both(sql)
        expect.row_set(c, h, label)


def test_the_empty_string_stays_distinct_from_null(pgc_conn, expect):
    """Two different things that a null bitmap can confuse, and a count each.

    A varlena column stores an empty string as a zero-length value and a NULL as a bit,
    so a decoder that loses the bitmap returns '' where NULL was -- and both counts move
    in opposite directions, which is why both are asserted rather than just the total.
    """
    p = _pair(pgc_conn, "id int, s text",
              load="SELECT g, CASE WHEN g%2=0 THEN '' WHEN g%3=0 THEN NULL ELSE 'x'||g END "
                   "FROM generate_series(1,300) g")
    expect.at_least(p.one("SELECT count(*) FROM t_col WHERE s = ''"), 1,
                    "premise: there are empty strings to confuse")
    expect.at_least(p.one("SELECT count(*) FROM t_col WHERE s IS NULL"), 1,
                    "premise: and nulls to confuse them with")
    for label, sql in (("empty-vs-null scan",    "SELECT * FROM %T"),
                       ("empty-vs-null empties", "SELECT count(*) FROM %T WHERE s = ''"),
                       ("empty-vs-null nulls",   "SELECT count(*) FROM %T WHERE s IS NULL")):
        c, h = p.both(sql)
        expect.row_set(c, h, label)


def test_a_wide_row_of_sixty_one_columns_agrees(pgc_conn, expect):
    """Sixty-one columns, where a per-column offset error shows and a narrow table
    hides it: every column after a wrong one reads the previous column's bytes, and a
    projection of three scattered columns is what catches it without reading them all."""
    defs = "id int, " + ", ".join(f"c{i} int" for i in range(1, 61))
    sel = "g, " + ", ".join(f"(g*{i} - {i})" for i in range(1, 61))
    p = _pair(pgc_conn, defs, load=f"SELECT {sel} FROM generate_series(1,500) g")
    # VIA regclass, not information_schema.columns by name. The unqualified form counts
    # every table called t_col in every schema, and the module-scoped matrix fixture has
    # one of its own with twenty columns -- so this premise arm reported 81 and caught my
    # own query rather than the tree. `'t_col'::regclass` resolves through search_path to
    # THIS test's schema and nothing else.
    expect.num(p.one("SELECT count(*) FROM pg_attribute "
                     "WHERE attrelid = 't_col'::regclass "
                     "AND attnum > 0 AND NOT attisdropped"), 61,
               "premise: the table really is sixty-one columns wide")
    c, h = p.both("SELECT * FROM %T")
    expect.row_set(c, h, "wide row scan")
    c, h = p.both("SELECT id, c1, c30, c60 FROM %T WHERE c30 > 5000")
    expect.row_set(c, h, "wide row proj")


# ---------------------------------------------------------------------------
# Part 3: lightweight encodings
#
# Data shaped so each encoding is the one chosen, and the oracle proves the round trip.
# Which encoding was APPLIED is the native_encoding suite's question; this asks only
# that whatever was applied is reversible.
#
# THE i4 FIXTURE USES random(), which is why `_pair` generates once into heap and
# copies. Run the generator twice and the two tables hold different numbers, and the
# oracle then reports a columnar defect that is really two fixtures -- on the suite
# whose whole purpose is to be believed when it says the two disagree.
# ---------------------------------------------------------------------------

_ENC_OPTS = ("chunk_group_row_limit => 2000, stripe_row_limit => 20000, "
             "compression => 'none'")


def test_the_integer_encodings_round_trip(pgc_conn, expect):
    """Four shapes in one table, each the input a different encoding is chosen for.

    `seqv` is g*3, so consecutive deltas are constant and delta-of-delta wins.
    `lowcard` is g%4, four distinct values, so a dictionary wins. `constv` is one
    value, so the column is a constant. `rnd` is a hash-spread bigint, so nothing
    lightweight applies and it takes the ordinary path. Putting them in ONE table is
    the point: the encodings are chosen per column, and a writer that applied one
    column's verdict to another would pass a table with only one shape in it.

    `compression => 'none'` so the codec cannot mask a wrong encoding by compressing
    the damage away.
    """
    p = _pair(pgc_conn, "id int, seqv bigint, lowcard int, constv int, rnd bigint",
              load="SELECT g, g::bigint*3, g%4, 42, ((g*2654435761)%1000000000)::bigint "
                   "FROM generate_series(1,10000) g",
              options=_ENC_OPTS)
    expect.num(p.one("SELECT count(DISTINCT lowcard) FROM t_col"), 4,
               "premise: the low-cardinality column really has four values")
    expect.num(p.one("SELECT count(DISTINCT constv) FROM t_col"), 1,
               "premise: and the constant column really has one")
    for label, sql in (
            ("enc whole-row",  "SELECT * FROM %T"),
            ("enc seq range",  "SELECT id FROM %T WHERE seqv BETWEEN 100 AND 5000"),
            ("enc lowcard eq", "SELECT id FROM %T WHERE lowcard = 2"),
            ("enc const scan", "SELECT id FROM %T WHERE constv = 42"),
            ("enc aggregate",  "SELECT sum(seqv), min(lowcard), max(lowcard), "
                               "count(constv), sum(rnd) FROM %T")):
        c, h = p.both(sql)
        expect.row_set(c, h, label)


def test_the_float_and_timestamp_encodings_round_trip(pgc_conn, expect):
    """Gorilla and delta-of-delta, on the inputs each is chosen for (I4).

    `alt` is a random walk: many distinct values so a dictionary bails, irregular bit
    deltas so frame-of-reference and delta lose, small consecutive XOR so Gorilla wins.
    `tsreg` is a fixed one-minute interval, so the delta of the delta is zero and DOD
    beats plain delta. `fr` cycles through seven values, so frame-of-reference applies.

    THE RANDOM WALK IS WHY THE PAIR IS COPIED RATHER THAN REGENERATED. Measured on this
    fixture's generator, 2000 rows: regenerated, all 2000 rows differ; generated once
    and copied, 0 differ. An oracle over two different fixtures is not an oracle.

    min/max rather than sum for the floats, deliberately: a float sum has no single
    right answer (part 1 measures three from heap alone by row order), and min/max does.
    """
    p = _pair(pgc_conn, "id int, alt float8, tsreg timestamp, fr float8",
              load="SELECT g, (1000 + sum(random() - 0.5) OVER (ORDER BY g))::float8, "
                   "TIMESTAMP '2020-01-01' + make_interval(mins => g), "
                   "(100 + (g%7) * 0.25)::float8 FROM generate_series(1,10000) g",
              options=_ENC_OPTS)
    expect.at_least(p.one("SELECT count(DISTINCT alt) FROM t_col"), 9000,
                    "premise: the random walk is high-cardinality, so a dictionary bails")
    expect.num(p.one("SELECT count(DISTINCT fr) FROM t_col"), 7,
               "premise: and the frame-of-reference column cycles through seven")
    for label, sql in (
            ("i4 whole-row", "SELECT * FROM %T"),
            ("i4 float agg", "SELECT count(alt), min(alt), max(alt), "
                             "count(fr), min(fr), max(fr) FROM %T"),
            ("i4 ts range",  "SELECT id FROM %T WHERE tsreg >= TIMESTAMP '2020-01-05'"),
            ("i4 ts minmax", "SELECT min(tsreg), max(tsreg) FROM %T")):
        c, h = p.both(sql)
        expect.row_set(c, h, label)


def test_the_dictionary_encoding_round_trips_including_varlena(pgc_conn, expect):
    """Dictionary (I5), including the text and varchar columns that had no lightweight
    encoding before it.

    `cat` has four values and `tag` six, so both are dictionary candidates; `hicard` is
    an md5 per row, so it is not, and it is in the table to prove the verdict is per
    column. `GROUP BY cat` is the arm a whole-row comparison cannot replace: it reads
    the column through the grouping path rather than the projection path.
    """
    p = _pair(pgc_conn, "id int, cat text, tag varchar(16), code int, hicard text",
              load="SELECT g, (ARRAY['north','south','east','west'])[1 + g%4], "
                   "('t' || (g%6))::varchar(16), g%5, md5(g::text) "
                   "FROM generate_series(1,10000) g",
              options=_ENC_OPTS)
    expect.num(p.one("SELECT count(DISTINCT cat) FROM t_col"), 4,
               "premise: the dictionary column has four values")
    expect.at_least(p.one("SELECT count(DISTINCT hicard) FROM t_col"), 9000,
                    "premise: and the high-cardinality one is not a candidate")
    for label, sql in (
            ("dict whole-row",  "SELECT * FROM %T"),
            ("dict text eq",    "SELECT id FROM %T WHERE cat = 'east'"),
            ("dict text agg",   "SELECT count(cat), min(cat), max(cat), "
                                "count(distinct tag) FROM %T"),
            ("dict text group", "SELECT cat, count(*) FROM %T GROUP BY cat")):
        c, h = p.both(sql)
        expect.row_set(c, h, label)


def test_an_uncompressed_table_still_round_trips(pgc_conn, expect):
    """Encoding is independent of the codec, so a table with compression off must still
    decode. Without this arm every encoding above is only ever read back through a
    codec, and a bug that the codec happens to mask would never show."""
    p = _pair(pgc_conn, "id int, v bigint",
              load="SELECT g, (g%7)::bigint FROM generate_series(1,5000) g",
              options="compression => 'none'")
    for label, sql in (("enc+nocompress scan", "SELECT * FROM %T"),
                       ("enc+nocompress agg",  "SELECT count(*), sum(v), min(v), max(v) FROM %T")):
        c, h = p.both(sql)
        expect.row_set(c, h, label)


# ---------------------------------------------------------------------------
# Part 4: aggregates over data with nulls and deletes
#
# The vectorized aggregate answers ungrouped count/sum/avg/min/max from the value
# stream. Nulls are skipped by that stream and deletes force a per-group fallback, so a
# fixture with both exercises the fast path AND its fallback in one table.
# ---------------------------------------------------------------------------


def test_aggregates_agree_with_nulls_and_deletes_present(pgc_conn, expect):
    """Five columns, one with nulls, and one row in fifty deleted.

    The DELETE is what makes this more than part 1's aggregate arms: a row group with a
    delete cannot be answered from the value stream alone, so the scan falls back per
    group -- and a fallback that double-counts or skips shows in `count(*)` while every
    other arm stays green. Both tables get the same DELETE, so the oracle still holds.
    """
    p = _pair(pgc_conn, "id int, k int, big int, s smallint, nv int",
              load="SELECT g, g%6, g*2, ((g%100)-50)::smallint, "
                   "CASE WHEN g%9=0 THEN NULL ELSE g%13 END FROM generate_series(1,20000) g",
              options="chunk_group_row_limit => 1000, stripe_row_limit => 5000")
    before = p.one("SELECT count(*) FROM t_col")
    pgc_conn.execute("DELETE FROM t_heap WHERE id % 50 = 0")
    pgc_conn.execute("DELETE FROM t_col  WHERE id % 50 = 0")
    after = p.one("SELECT count(*) FROM t_col")
    expect.num(before - after, 400, "premise: the delete removed four hundred rows")
    expect.at_least(p.one("SELECT count(*) FROM t_col WHERE nv IS NULL"), 1,
                    "premise: and there are nulls for the value stream to skip")
    for label, sql in (
            ("agg count",  "SELECT count(*), count(k), count(nv) FROM %T"),
            ("agg sum",    "SELECT sum(big), sum(k), sum(nv) FROM %T"),
            ("agg avg",    "SELECT avg(k), avg(nv) FROM %T"),
            ("agg minmax", "SELECT min(k), max(k), min(big), max(big), "
                           "min(s), max(s), min(nv), max(nv) FROM %T")):
        c, h = p.both(sql)
        expect.row_set(c, h, label)


# ---------------------------------------------------------------------------
# Part 5: bloom-filter equality skipping
#
# Values are hash-spread so every chunk's min/max spans the domain and cannot skip an
# in-range equality probe. The bloom filter is what prunes it, and whether a bloom was
# BUILT is the native_bloom suite's question -- these arms ask whether the answer is
# right, which is the one thing a wrong bloom breaks and a missing one does not.
# ---------------------------------------------------------------------------

_BLOOM_OPTS = "chunk_group_row_limit => 1000, stripe_row_limit => 20000"


def test_bloom_equality_agrees_on_numeric_and_uuid_keys(pgc_conn, expect):
    """Hash-spread keys, so min/max cannot prune and only a bloom can.

    The premise is the whole fixture: if the keys were ordered, every arm here would pass
    on zone maps alone and say nothing about blooms. `(g*2654435761)%100000` spreads them,
    so each chunk's range covers almost the whole domain -- asserted below rather than
    assumed, because a fixture that quietly became ordered would make this suite vacuous
    in a way no arm would report.
    """
    p = _pair(pgc_conn, "id int, k bigint, u uuid",
              load="SELECT g, ((g*2654435761)%100000)::bigint, md5((g%99999)::text)::uuid "
                   "FROM generate_series(1,20000) g",
              options=_BLOOM_OPTS)
    # Each chunk group's range must span most of the domain, or min/max alone prunes and
    # the bloom is never the thing under test.
    spread = p.one(
        "SELECT min(width) FROM (SELECT max(k) - min(k) AS width FROM ("
        "  SELECT k, ntile(20) OVER (ORDER BY id) AS grp FROM t_col) s GROUP BY grp) w")
    expect.at_least(spread, 90000,
                    "premise: every chunk's key range spans the domain, so min/max cannot skip")
    for label, sql in (
            ("bloom k present", "SELECT id FROM %T WHERE k = ((7*2654435761)%100000)::bigint"),
            ("bloom u eq",      "SELECT count(*) FROM %T WHERE u = md5('123')::uuid"),
            ("bloom k range",   "SELECT count(*) FROM %T WHERE k < 50000")):
        c, h = p.both(sql)
        expect.row_set(c, h, label)

    # A value strictly inside the global min/max that is present in NO row. Every
    # hash-spread chunk's range contains it, so min/max cannot exclude it and the answer
    # must still be nothing. Derived from the data rather than guessed, because a guessed
    # "absent" value that turns out to be present asserts the opposite of the intent.
    absent = p.one("SELECT v FROM generate_series((SELECT min(k)+1 FROM t_heap)::int, "
                   "(SELECT max(k)-1 FROM t_heap)::int) v "
                   "WHERE v NOT IN (SELECT k FROM t_heap) LIMIT 1")
    expect.num(p.one(f"SELECT count(*) FROM t_heap WHERE k = {absent}::bigint"), 0,
               "premise: the probe value really is absent from the oracle")
    expect.num(p.one(f"SELECT count(*) FROM t_col WHERE k = {absent}::bigint"), 0,
               "bloom absent correct")


def test_text_bloom_equality_agrees_including_a_mismatched_collation(pgc_conn, expect):
    """Deterministic-collation text is bloomed; an explicit mismatched COLLATE is not.

    The last arm is the one that matters: a bloom built under one collation cannot be
    used to skip a probe under another, so the filter must NOT be pushed -- and if it
    were, the query would return too few rows rather than erroring. That is a wrong
    answer, not a failure, which is why it is compared against the oracle rather than
    checked for a plan shape.
    """
    p = _pair(pgc_conn, 'id int, tk text, tc text COLLATE "C"',
              load="SELECT g, 'k' || ((g*2654435761)%50000), 'c' || ((g*40503)%50000) "
                   "FROM generate_series(1,16000) g",
              options=_BLOOM_OPTS)
    # THE BASH ARM PROBES A VALUE THAT IS NOT THERE, and that is the fourth unfalsifiable
    # arm this port has found -- the subtlest, because the arm is specifically built to
    # catch a wrongly-pushed filter and a wrongly-pushed filter produces the same answer
    # as the absent value.
    #
    # `tk` is 'k' || ((g*2654435761)%50000) over 16,000 rows of a 50,000-wide domain, so
    # only 32% of values appear. Measured: `tk = 'k100'` matches 0 rows. A bloom wrongly
    # pushed under a mismatched collation would skip the chunks holding the match and
    # return 0 -- identical to the correct answer for an absent value. Both sides give 0,
    # the arm passes, and the one defect it exists to detect is invisible to it.
    #
    # So the port probes a value that IS present, derived from the data rather than
    # guessed. Then a wrongly-pushed filter returns 0 where the oracle returns 1, and the
    # arm can fail for its own reason. The name is kept so compare_to_bash.py still pairs
    # the two.
    present = p.one("SELECT tk FROM t_heap ORDER BY id LIMIT 1")
    expect.num(p.one(f"SELECT count(*) FROM t_heap WHERE tk = '{present}'"), 1,
               "premise: the mismatched-collation probe value is present exactly once")
    for label, sql in (
            ("textbloom present",           "SELECT id FROM %T WHERE tk = 'k' || ((7*2654435761)%50000)"),
            ("textbloom absent",            "SELECT count(*) FROM %T WHERE tk = 'zzzzzzzz'"),
            ("textbloom C absent",          "SELECT count(*) FROM %T WHERE tc = 'zzzzzzzz'"),
            # A PRESENT value (#1339): 'c123' is in no row, so both sides counted 0
            # and a wrongly skipped chunk could not show. Row 7's value is present once.
            ("textbloom C eq",              "SELECT count(*) FROM %T WHERE tc = 'c' || ((7*40503)%50000)"),
            ("textbloom collate-mismatch",
             f"SELECT count(*) FROM %T WHERE tk = '{present}' COLLATE \"C\"")):
        c, h = p.both(sql)
        expect.row_set(c, h, label)
    # AND THE DIRECTION IT MUST FAIL IN, stated as its own arm: the mismatched collation
    # must return the row, not zero. Without this the comparison above is satisfied by
    # both sides returning 0, which is what the bash arm does today.
    expect.num(p.one(f"SELECT count(*) FROM t_col WHERE tk = '{present}' COLLATE \"C\""), 1,
               "textbloom collate-mismatch returns the row rather than skipping it")


# ---------------------------------------------------------------------------
# Part 6: a selective filter with several wide output columns
# ---------------------------------------------------------------------------


def test_a_selective_filter_with_a_wide_projection_agrees(pgc_conn, expect):
    """One filtered column, four projected, at four selectivities from one row to most.

    `wide nomatch` matches nothing on both sides, which is the second arm in this file
    that cannot fail on its own -- kept with a stated reason and a positive control, the
    same treatment `empty scan` gets. The other three are values or non-empty sets.
    """
    p = _pair(pgc_conn, "id int, sel int, a text, b bigint, c numeric",
              load="SELECT g, g, 'a'||g, g::bigint*2, g::numeric*1.5 "
                   "FROM generate_series(1,20000) g",
              options=_BLOOM_OPTS)
    c, h = p.both("SELECT id, a, b, c FROM %T WHERE sel = 12345")
    expect.row_set(c, h, "wide point")
    c, h = p.both("SELECT id, a, b FROM %T WHERE sel BETWEEN 5000 AND 5100")
    expect.row_set(c, h, "wide range")
    c, h = p.both("SELECT id, a, b, c FROM %T WHERE sel = 999999")
    expect.row_set(c, h, "wide nomatch",
                   allow_empty="no row has sel = 999999; the control below is what can fail")
    c, h = p.both("SELECT id, a FROM %T WHERE sel > 100")
    expect.row_set(c, h, "wide most")
    # THE CONTROL for `wide nomatch`: the same projection at a value that IS present must
    # return a row, or "returns nothing" is the only behaviour that arm has ever observed.
    c, h = p.both("SELECT id, a, b, c FROM %T WHERE sel = 19999")
    expect.row_set(c, h, "wide nomatch control: a present value returns its row")
    expect.num(len(c), 1, "and the control really did match one row")


# ---------------------------------------------------------------------------
# Part 7: covering count(*) from metadata
#
# count(*) with no filter is answered from each row group's stored row count minus the
# visible-row-mask deletes, skipping the data scan. It must equal the oracle after
# inserts, deletes AND updates, whether that path is taken or not.
# ---------------------------------------------------------------------------


COUNT_MODES = ["on", "off"]


def _count_path(plan):
    """The path a covering count took: "metadata" when the plan shows the metadata
    node, "scan" when it shows the columnar scan without it. Anything else is named,
    so a plan that lost the columnar scan, or a failed EXPLAIN, cannot pass as either."""
    if not plan:
        return "no plan"
    if "Columnar Vectorized Aggregates" in plan:
        return "metadata"
    if "Custom Scan (PgColumnarScan)" in plan:
        return "scan"
    return "other plan: " + plan.replace("\n", " | ")


@pytest.fixture(scope="module")
def covering(pgc_cluster):
    """The covering-count pair after a delete and an update, and each mode's readings.

    ONE CHECK PER TEST (#1339). Each mode's path and count pair are taken here as
    values, so a red in one cannot hide another: the #1528 review measured an
    inverted switch reddening both path checks in the shell and only the first in a
    loop that asserted as it went. A failed query is a query_error(), which every
    comparison refuses.

    `SET` on this connection rather than the bash suite's `ALTER DATABASE`: that form
    exists because each psql invocation there is a new session, and this one is not.
    """
    import psycopg
    from pgc_vacuity import query_error

    conn = psycopg.connect(pgc_cluster.dsn(), autocommit=True)
    schema = "pgc_differential_count"

    def rows(sql):
        try:
            return [tuple(r) for r in conn.execute(sql).fetchall()]
        except psycopg.Error as exc:
            return query_error(exc.diag.message_primary)

    got = {}
    try:
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        conn.execute(f'CREATE SCHEMA "{schema}"')
        conn.execute(f'SET search_path TO "{schema}", public')
        try:
            _pair(conn, "id int, v int",
                  load="SELECT g, g%10 FROM generate_series(1,20000) g",
                  options="chunk_group_row_limit => 1000, stripe_row_limit => 3000")
            conn.execute("DELETE FROM t_heap WHERE id % 13 = 0")
            conn.execute("DELETE FROM t_col  WHERE id % 13 = 0")
            conn.execute("UPDATE t_heap SET v = v + 1 WHERE id % 17 = 0")
            conn.execute("UPDATE t_col  SET v = v + 1 WHERE id % 17 = 0")
        except psycopg.Error as exc:
            print(f"-- building the covering-count pair failed: {exc.diag.message_primary}")
        heap = rows("SELECT count(*) FROM t_heap")
        got["count"] = heap[0][0] if isinstance(heap, list) and heap else heap
        for mode in COUNT_MODES:
            conn.execute(f"SET pgcolumnar.enable_vectorization = {mode}")
            plan = rows("EXPLAIN (COSTS OFF) SELECT count(*) FROM t_col")
            got[mode] = {
                "path": _count_path("\n".join(r[0] for r in plan))
                        if isinstance(plan, list) else f"no plan: {plan}",
                "col": rows("SELECT count(*) FROM t_col"),
                "heap": rows("SELECT count(*) FROM t_heap"),
            }
        conn.execute("RESET pgcolumnar.enable_vectorization")
        yield got
    finally:
        try:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            conn.close()


def test_the_delete_and_update_leave_a_count_neither_gives_alone(covering, expect):
    """An UPDATE on a columnar table appends the new row and masks the old, so the
    stored per-group count and the visible count diverge: the arithmetic the
    metadata path has to get right, and a plain scan gets right for free."""
    expect.num(covering["count"], 18462,
               "premise: the delete and update leave a count neither one would give alone")


@pytest.mark.parametrize("mode", COUNT_MODES)
def test_each_count_mode_takes_the_path_it_names(covering, expect, mode):
    """A fallback returns the same count, so the count arm alone cannot say which path
    answered it. native_agg proves the metadata node on a table with no deletes, not
    on this one."""
    expect.text(covering[mode]["path"], "metadata" if mode == "on" else "scan",
                f"count meta={mode} takes the path it names")


@pytest.mark.parametrize("mode", COUNT_MODES)
def test_the_covering_count_agrees_with_the_heap(covering, expect, mode):
    expect.row_set(covering[mode]["col"], covering[mode]["heap"], f"count meta={mode}")
