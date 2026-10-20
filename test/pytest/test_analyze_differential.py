"""What `pgcolumnar.analyze()` writes must have the SHAPE core ANALYZE writes.

`pg_restore_attribute_stats` takes `VARIADIC "any"` and validates each argument's
type at run time. A mistyped argument is NOT an error: the function emits a
WARNING, sets that argument to NULL, and returns cleanly having stored nothing.
A value-level suite cannot see that, because `pg_restore_attribute_stats` leaves
kinds it was not given in place -- so a statistic we failed to write is still
there, wearing core's shape, and every value assertion reads core's work and
reports on ours.

So core ANALYZE is the oracle, and the comparison is over SHAPE: for every
statistic kind written for a column, the operator and the collation stored
alongside it must match what core stored for the same kind of the same column.
A dropped argument leaves the slot absent, which a shape comparison sees at once.

INDEPENDENT OF `test/analyze_differential.sh`. Same public seam -- the catalog
after each of the two writers has run -- and nothing else shared:

  * The shell materialises an `ad_shape` table because every `q()` opens a new
    connection, and it pairs kind/op/collation with `unnest ... WITH ORDINALITY`
    joined three ways. This file holds ONE connection, reads the five slots as
    three arrays, and zips them in Python. Same pairing, different machinery.
  * The shell greps psql's output for `WARNING`. This file registers a psycopg
    notice handler. The WARNING is the thing the suite is named for, so the two
    harnesses observe it through two unrelated channels on purpose.
  * Frequencies are checked one most-common value at a time, through a
    parameterised count, rather than in one set-returning query. A failure then
    names the value that disagreed instead of returning a count of disagreements.

THE REFUSAL IS NAMED, AND THAT IS #1131. `analyze_differential.sh` declines on
PG15-17 through `check_skip`, which RECORDS the refusal under the name "the
differential analyze path". Until `expect.cannot_run` could carry a name, no port
could emit that string and `compare_to_bash` reported it MISSING however faithful
the rest was. The precondition -- the server major -- exists on both sides, which
is the case resolution 1 unblocks.

THE MAJOR IS ASSERTED, NOT ASSUMED. The shell reaches its `pgc_fail` only when
the version is unreadable; here the same property is asserted on every run, under
the same name, so the gate below is known to have been decided on a real number
rather than on an empty string that compared less-than 18.
"""
import decimal

import psycopg

# The fixture size. Fixed here rather than read from the shell suite's
# PGC_ANALYZE_DIFF_ROWS: the two harnesses share no configuration, and a
# frequency oracle that moves with an environment variable is not an oracle.
ROWS = 50000

# Five types, because the failure this file hunts is type-dependent. The array
# literal for most_common_vals is built through the type's own output function;
# an int column can be written correctly by code that mangles every text column.

# The two text values that break an array literal assembled by hand: one carries
# the separator, the other the quote.
COMMA_VALUE = "alpha,beta"
QUOTE_VALUE = "it's here"

# pg_statistic.h. Asserted rather than assumed below, because a comparison of two
# empty sets passes.
KIND_MCV = 1
KIND_HISTOGRAM = 2

# Every column is one-in-seven NULL, and that is load-bearing rather than
# realistic. Frequencies are count / TOTAL rows including nulls (analyze.c:2720);
# dividing by the non-null count instead is the quiet defect this file should
# catch, and without nulls the two denominators are the same number.
FIXTURE = f"""
    CREATE TABLE ad_c (i int, t text, n numeric, d date, b boolean)
        USING pgcolumnar;
    INSERT INTO ad_c SELECT
        CASE WHEN g % 7 = 0 THEN NULL
             WHEN g % 5 = 0 THEN 7 WHEN g % 5 = 1 THEN 42 ELSE 1000 + g END,
        CASE WHEN g % 7 = 0 THEN NULL
             WHEN g % 5 = 0 THEN 'alpha,beta' WHEN g % 5 = 1 THEN 'it''s here'
             ELSE 'v' || g END,
        CASE WHEN g % 7 = 0 THEN NULL
             WHEN g % 5 = 0 THEN 1.5 WHEN g % 5 = 1 THEN 2.25
             ELSE (1000 + g)::numeric END,
        CASE WHEN g % 7 = 0 THEN NULL
             WHEN g % 5 = 0 THEN DATE '2020-01-01'
             WHEN g % 5 = 1 THEN DATE '2021-06-15'
             ELSE DATE '2000-01-01' + g END,
        CASE WHEN g % 7 = 0 THEN NULL ELSE (g % 3 = 0) END
    FROM generate_series(1, {ROWS}) g
"""
# The `%` above are the server's modulo and reach it literally: psycopg
# substitutes only when parameters are passed, and none are. Doubling them would
# send `%%` to the parser. Measured twice, hours apart, the second time after it
# had been written down.

# The five slots, read as three arrays and paired in Python. Comparing kind SETS
# alone would miss an operator or a collation written into the right kind's slot
# but wrong, which is the silent half of this failure mode.
SHAPE_SQL = """
    SELECT a.attname,
           ARRAY[st.stakind1, st.stakind2, st.stakind3, st.stakind4, st.stakind5],
           ARRAY[st.staop1,   st.staop2,   st.staop3,   st.staop4,   st.staop5],
           ARRAY[st.stacoll1, st.stacoll2, st.stacoll3, st.stacoll4, st.stacoll5]
      FROM pg_attribute a
      JOIN pg_statistic st ON st.starelid = a.attrelid
                          AND st.staattnum = a.attnum
     WHERE a.attrelid = 'ad_c'::regclass AND a.attnum > 0
       AND NOT a.attisdropped AND NOT st.stainherit
"""


def _shape(conn):
    """-> {(attname, kind, op, collation)} for every occupied slot.

    Kind 0 is an empty slot and is dropped here, the way the shell suite drops it
    in the WHERE clause.
    """
    out = set()
    with conn.cursor() as cur:
        cur.execute(SHAPE_SQL)
        for attname, kinds, ops, colls in cur.fetchall():
            for kind, op, coll in zip(kinds, ops, colls):
                if kind != 0:
                    out.add((attname, kind, op, coll))
    return out


def _columns_with(shape, kind):
    return {attname for attname, k, _op, _coll in shape if k == kind}


def _one(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params) if params is not None else cur.execute(sql)
        row = cur.fetchone()
    return row[0] if row else None


def _mcv(conn, col, typ):
    """-> (values, frequencies) as Python objects of the column's own type.

    Cast through text into the column's array type so psycopg decodes real values:
    `most_common_vals` is declared anyarray and comes back as an opaque string
    otherwise. The cast is also what the round-trip assertions need -- comparing
    the printed array against a printed expectation would pass on a literal that
    lost its quoting and reparsed into different values.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT most_common_vals::text::{typ}[], most_common_freqs "
            "FROM pg_stats WHERE schemaname = current_schema() "
            "AND tablename = 'ad_c' AND attname = %s", (col,))
        row = cur.fetchone()
    if row is None or row[0] is None:
        return [], []
    return list(row[0]), list(row[1] or [])



def _mcv_listed(conn, col, typ):
    """-> "yes" when the column has a most-common list, else its length. An empty list
    makes "no value disagrees" true of nothing, so this is read first."""
    values, _ = _mcv(conn, col, typ)
    return "yes" if len(values) >= 1 else f"no (length [{len(values)}])"


def _mcv_wrong(conn, col, typ):
    """-> how many most-common values have a stored frequency the table disagrees
    with. The oracle is an independent count over the table, never a re-reading of
    what the function wrote; a tolerance is required because most_common_freqs is
    real while the true frequency is exact."""
    values, freqs = _mcv(conn, col, typ)
    wrong = []
    for value, freq in zip(values, freqs):
        seen = _one(conn, f"SELECT count(*) FROM ad_c WHERE {col} IS NOT DISTINCT FROM %s", (value,))
        if abs(decimal.Decimal(seen) / ROWS - decimal.Decimal(repr(freq))) > decimal.Decimal("0.000001"):
            wrong.append((value, freq, seen))
    if wrong:
        print(f"-- {col}: stored frequency disagrees with the table for {wrong}")
    return len(wrong)


def test_analyze_differential(pgc_conn, expect):
    version_num = _one(pgc_conn, "SHOW server_version_num")
    readable = str(version_num).isdigit()
    # Asserted on EVERY run, not only when it fails. The shell suite reaches its
    # `pgc_fail` of this name only on an unreadable version; asserting it always
    # is strictly stronger and matches the name either way, so an empty string
    # cannot be mistaken for an old major and reported as "supported, skipped".
    expect.text("readable" if readable else f"got [{version_num}]", "readable",
                "could not read the server major, so the gate below cannot be trusted")
    major = int(version_num) // 10000 if readable else 0

    if major < 18:
        expect.cannot_run(
            "UNSUPPORTED_MAJOR",
            f"pgcolumnar.analyze() writes through pg_restore_attribute_stats, "
            f"which arrived in PostgreSQL 18; this server is {major}, so there "
            f"is no differential to take",
            # WRITTEN AT THE CALL, not behind a constant: `compare_to_bash`
            # resolves string literals at the call site, so a name in a module
            # constant is a name the grader cannot see.
            name="the differential analyze path")
        return

    warnings = []
    pgc_conn.add_notice_handler(lambda diag: warnings.append(diag.message_primary))

    with pgc_conn.cursor() as cur:
        cur.execute(FIXTURE)

    expect.num(_one(pgc_conn, "SELECT count(*) FROM ad_c"), ROWS,
               "premise: the fixture loaded, so the shapes below describe real data")

    # The premise that makes every frequency assertion below discriminating: if
    # the column were never null, count/total and count/non-null would agree and
    # a wrong denominator would pass unseen.
    nulls = _one(pgc_conn, "SELECT count(*) FROM ad_c WHERE i IS NULL")
    expect.text("yes" if nulls > 0 else "no", "yes",
                "premise: the columns are nullable in fact, so the two denominators differ")

    # The values that break a hand-built literal are actually in the table.
    # Without this, a green run could mean the quoting was never exercised.
    comma_rows = _one(pgc_conn, "SELECT count(*) FROM ad_c WHERE t = %s", (COMMA_VALUE,))
    expect.text("yes" if comma_rows > 0 else "no", "yes",
                "premise: a most-common text value contains a comma")
    quote_rows = _one(pgc_conn, "SELECT count(*) FROM ad_c WHERE t = %s", (QUOTE_VALUE,))
    expect.text("yes" if quote_rows > 0 else "no", "yes",
                "premise: and another contains a quote")

    # --- core's shape, which is the oracle ------------------------------------
    with pgc_conn.cursor() as cur:
        cur.execute("ANALYZE ad_c")
    core = _shape(pgc_conn)

    # Not premises (#1339): core reads a columnar table through pgcolumnar's own
    # ANALYZE sampler, so a repository change can redden these -- with the sampler
    # mapping no block to a slice, core collects nothing and both go red.
    expect.num(len(_columns_with(core, KIND_MCV)), 5,
               "core ANALYZE, through the columnar sampler, produced a most-common list for every column")
    # `b` has two distinct values, both repeated, so its most-common list
    # describes it completely and num_hist = ndistinct - num_mcv = 0
    # (analyze.c:2744). Four columns, not five, and that is the point.
    expect.num(len(_columns_with(core, KIND_HISTOGRAM)), 4,
               "and a histogram for the four with a tail, but not for boolean")

    # --- clear, so what follows is unambiguously ours -------------------------
    #
    # pg_restore_attribute_stats leaves kinds it was not given in place. That is
    # correct and it is fatal to attribution here: a statistic we failed to write
    # would still be present, wearing core's shape.
    with pgc_conn.cursor() as cur:
        cur.execute(
            "SELECT pg_catalog.pg_clear_attribute_stats("
            "  current_schema()::text, 'ad_c', a.attname::text, false) "
            "FROM pg_attribute a "
            "WHERE a.attrelid = 'ad_c'::regclass AND a.attnum > 0 "
            "AND NOT a.attisdropped")
    expect.num(_one(pgc_conn,
                    "SELECT count(*) FROM pg_statistic "
                    "WHERE starelid = 'ad_c'::regclass"), 0,
               "premise: every statistic is gone before we write, so nothing below is core's")

    # --- our call -------------------------------------------------------------
    del warnings[:]
    failure = ""
    try:
        with pgc_conn.cursor() as cur:
            cur.execute("SELECT pgcolumnar.analyze('ad_c'::regclass)")
    except psycopg.Error as exc:
        failure = f"{exc.sqlstate}: {str(exc).splitlines()[0]}"
    expect.text(failure or "no error", "no error",
                "pgcolumnar.analyze() ran over every column without raising")

    # The assertion this file is named for. A WARNING here IS the silent wrong
    # write: the argument was dropped, the call succeeded, and the statistic is
    # missing. Collected from the notice stream, so a WARNING raised on a
    # connection that printed nothing to a terminal is still seen.
    print(f"-- notices during pgcolumnar.analyze(): {warnings or 'none'}")
    expect.num(len(warnings), 0,
               "and without a WARNING, which is how pg_restore_attribute_stats drops an argument")

    ours = _shape(pgc_conn)

    # --- the differential -----------------------------------------------------
    #
    # Asserted first, because every comparison that follows is over the rows this
    # counts. If the function wrote nothing, "no kind we wrote disagrees with
    # core" is true of the empty set.
    ours_mcv = _columns_with(ours, KIND_MCV)
    expect.num(len(ours_mcv), 5,
               "we wrote a most-common list for every column, as core did")

    ours_hist = _columns_with(ours, KIND_HISTOGRAM)
    expect.num(len(ours_hist), 4,
               "and a histogram for exactly the four core gave one")

    # The SAME four, not merely four of them.
    expect.num(len(ours_hist - _columns_with(core, KIND_HISTOGRAM)), 0,
               "and they are the same four columns, not just the same count")

    # Every slot we wrote must exist in core's shape for that column carrying the
    # same operator and the same collation. A missing counterpart counts as a
    # mismatch, which is what the set difference does.
    disagree = sorted(ours - core)
    if disagree:
        print(f"-- slots we wrote that core does not have: {disagree}")
    expect.num(len(disagree), 0,
               "every statistic we wrote agrees with core on operator and collation")

    # The element type of the stored array is deliberately NOT compared.
    # pg_statistic.stavalues1 is declared `anyarray` (pg_statistic.h:119), so
    # pg_typeof returns the constant string "anyarray" for every row ever stored.
    # A probe built on it reports every slot as mismatched -- including core's
    # own -- so it tests the expectation rather than the code. What IS observable
    # is stronger and is asserted below.

    # --- the values and frequencies mean what they say, per type ---------------
    #
    # A tolerance is required and is not slack: most_common_freqs is real
    # (float4, ~7 significant digits) while the true frequency is exact, so
    # demanding equality would fail on representation rather than on correctness.
    # Not premises (#1339): these read the most-common list pgcolumnar.analyze()
    # wrote, so a repository change reddens them. One literal name per column.
    expect.text(_mcv_listed(pgc_conn, "i", "int"), "yes", "i has a most-common list to check")
    expect.num(_mcv_wrong(pgc_conn, "i", "int"), 0,
               "every most-common value of i exists with exactly its stored frequency")
    expect.text(_mcv_listed(pgc_conn, "t", "text"), "yes", "t has a most-common list to check")
    expect.num(_mcv_wrong(pgc_conn, "t", "text"), 0,
               "every most-common value of t exists with exactly its stored frequency")
    expect.text(_mcv_listed(pgc_conn, "n", "numeric"), "yes", "n has a most-common list to check")
    expect.num(_mcv_wrong(pgc_conn, "n", "numeric"), 0,
               "every most-common value of n exists with exactly its stored frequency")
    expect.text(_mcv_listed(pgc_conn, "d", "date"), "yes", "d has a most-common list to check")
    expect.num(_mcv_wrong(pgc_conn, "d", "date"), 0,
               "every most-common value of d exists with exactly its stored frequency")
    expect.text(_mcv_listed(pgc_conn, "b", "boolean"), "yes", "b has a most-common list to check")
    expect.num(_mcv_wrong(pgc_conn, "b", "boolean"), 0,
               "every most-common value of b exists with exactly its stored frequency")

    # --- the values themselves survived the round trip -------------------------
    #
    # Shape agreement does not prove the text column's array literal was
    # assembled correctly: a literal that lost a comma still parses, into the
    # wrong values. These two were built to break it.
    text_values, _ = _mcv(pgc_conn, "t", "text")
    expect.num(text_values.count(COMMA_VALUE), 1,
               "the most-common text value containing a comma round-tripped intact")
    expect.num(text_values.count(QUOTE_VALUE), 1,
               "and the one containing a quote")

    bounds = _one(pgc_conn,
                  "SELECT histogram_bounds::text FROM pg_stats "
                  "WHERE schemaname = current_schema() AND tablename = 'ad_c' "
                  "AND attname = 'b'")
    expect.text(bounds if bounds is not None else "<none>", "<none>",
                "boolean gets no histogram, because the most-common list already describes it")
