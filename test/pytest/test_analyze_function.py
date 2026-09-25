"""`pgcolumnar.analyze()` collects per-column statistics without reading the table.

Core ANALYZE decodes essentially the whole table: it samples 30,000 rows, and on a
table of any size those rows are spread across every row group, so every group is
decoded for every column. Reading ONE column of a wide table is cheap where that
sample is not, and the statistics that come out of a full read are EXACT where
core's are estimates. Exactness is the observable a sampled implementation cannot
fake, which is why it is what gets asserted.

THE REFUSAL IS NAMED, AND THAT IS #1131. On 15, 16 and 17 `analyze_function.sh`
declines through `check_skip`, which RECORDS the refusal under the name
"pgcolumnar.analyze()" -- the statistics are written through
`pg_restore_attribute_stats`, which core added in 18. The precondition is the server
major, which exists on both sides.

ONE NAMED REFUSAL, THREE UNNAMED ONES. The shell suite is a single script: it prints
one `check_skip` and exits. This file is four tests, because three of them build
small fixtures of their own and paying the 500,000-row fixture four times to keep one
function would be the wrong trade. Only the test carrying the main fixture names the
refusal; the other three decline UNNAMED, because an unnamed `cannot_run` states no
property and the shell suite has exactly one name to match. Naming all four would
publish three checks the original does not have.

WHAT THE FIXTURES ARE FOR, since every one of them is built to defeat a specific
false pass:

  * `k` is one row in ten NULL. 0.1 is a number a sampler reaches whenever it is
    lucky, so `k` alone cannot discriminate.
  * `k7` is one row in SEVEN NULL, so the truth is 71428/500000 = 0.142856. Core's
    estimate is always a whole number of sampled rows over 30,000, and there is no
    whole k with k/30000 = 0.142856 -- it would need 4285.68. Core cannot report this
    fraction whatever it draws, so the discrimination does not depend on luck.
  * `skew` holds 1,000,000 in exactly one row of 500,000 and under 100 elsewhere, so
    a sample misses the extreme and a full read cannot.
  * `cat` has three repeated values and a unique tail, with one row in ten NULL, so
    dividing frequencies by the non-null count instead of the total row count
    produces three individually plausible numbers that are all wrong.

WHAT IS DELIBERATELY PRINTED RATHER THAN ASSERTED. Whether core's sampled number
happens to land on the truth is a fact about a random draw, not about this
extension. The shell suite retracted two such gates (#475, #487) after one of them
failed a correct suite about one run in 130. This file prints the same figures and
asserts only against independently counted truth.

INDEPENDENT OF `test/analyze_function.sh`: same public seam -- the catalog after the
function has run -- and nothing else shared. The shell greps psql's output for
ERROR and WARNING; this registers a psycopg notice handler and catches
`psycopg.Error`. The shell parses the documentation table with awk; this parses it
with a regex over the same section in Python. The shell computes the stride oracle in
SQL and compares against a hand-worked figure; this does both the same way, because
two independent derivations agreeing before either judges the code is the point of
that check rather than an implementation detail.
"""
import pathlib
import re
from decimal import Decimal

import psycopg

# The main fixture size. Fixed here rather than read from the shell suite's
# PGC_ANALYZE_ROWS: the two harnesses share no configuration, and an oracle that
# moves with an environment variable is not an oracle.
ROWS = 500000

SRCDIR = pathlib.Path(__file__).resolve().parents[2]

# The `%` below are the server's modulo and reach it literally: psycopg substitutes
# only when parameters are passed, and none are.
AF_C = f"""
    CREATE TABLE af_c (k int, k7 int, skew int, cat int,
                       pad1 text, pad2 text, pad3 text) USING pgcolumnar;
    INSERT INTO af_c SELECT
        CASE WHEN g % 10 = 0 THEN NULL ELSE g % 45001 END,
        CASE WHEN g % 7 = 0 THEN NULL ELSE g END,
        CASE WHEN g = 1 THEN 1000000 ELSE g % 100000 END,
        CASE WHEN g % 10 = 0  THEN NULL
             WHEN g <= 100000 THEN 7
             WHEN g <= 160000 THEN 42
             WHEN g <= 190000 THEN 99
             ELSE 1000 + g END,
        md5(g::text), md5((g * 7)::text), md5((g * 13)::text)
    FROM generate_series(1, {ROWS}) g
"""


def _exec(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)


def _one(conn, sql, params=None):
    with conn.cursor() as cur:
        if params is None:
            cur.execute(sql)
        else:
            cur.execute(sql, params)
        row = cur.fetchone()
    return row[0] if row else None


def _stat(conn, table, column, expr="*"):
    """-> one pg_stats expression for a column, scoped to THIS test's schema.

    `pg_stats` carries a schemaname and the fixture's connection holds a private
    one, so an unscoped `tablename = ...` could read another schema's table of the
    same name. Returns None when the row is absent, which is what a cleared
    attribute looks like -- the whole pg_statistic row goes, not just a field.
    """
    return _one(conn,
                f"SELECT {expr} FROM pg_stats WHERE schemaname = current_schema() "
                "AND tablename = %s AND attname = %s", (table, column))


def _analyze(conn, table, columns, warnings):
    """Run pgcolumnar.analyze() and return ("" | the error), collecting notices.

    The shell suite captures psql's output and greps it for ERROR and WARNING; the
    two facts arrive here through two different channels, an exception and a notice
    handler, which is the same pair of observations by unrelated means.
    """
    del warnings[:]
    cols = "ARRAY[" + ", ".join(f"'{c}'" for c in columns) + "]"
    try:
        _exec(conn, f"SELECT pgcolumnar.analyze('{table}'::regclass, {cols})")
    except psycopg.Error as exc:
        return f"{exc.sqlstate}: {str(exc).splitlines()[0]}"
    return ""


def _major(conn):
    raw = _one(conn, "SHOW server_version_num")
    return (str(raw).isdigit(), raw, int(raw) // 10000 if str(raw).isdigit() else 0)


def _decline_unnamed(expect, major):
    """The version refusal for the three tests that must NOT name it.

    UNNAMED deliberately: the shell suite prints ONE `check_skip` and exits, so there
    is one name to match. An unnamed `cannot_run` states no property, which is
    exactly right here -- naming it in all four would publish three checks the
    original does not have.
    """
    expect.cannot_run(
        "UNSUPPORTED_MAJOR",
        f"pgcolumnar.analyze() writes through pg_restore_attribute_stats, which "
        f"core added in PostgreSQL 18; this server is {major}")


def test_analyze_function(pgc_conn, expect):
    """The main fixture and everything measured over it."""
    readable, raw, major = _major(pgc_conn)
    # Asserted on EVERY run, not only when it fails. The shell reaches this name
    # only through a `pgc_fail` on an unreadable version; asserting it always is
    # strictly stronger and stops an empty string being mistaken for an old major
    # and reported as "supported, skipped".
    expect.text("readable" if readable else f"got [{raw}]", "readable",
                "could not read the server major, so the gate below cannot be trusted")
    if major < 18:
        expect.cannot_run(
            "UNSUPPORTED_MAJOR",
            f"pgcolumnar.analyze() writes through pg_restore_attribute_stats, which "
            f"core added in PostgreSQL 18; this server is {major}. Writing "
            f"pg_statistic directly on 15-17 is a version-support decision, not a "
            f"detail",
            # At the call, not behind a constant: the grader resolves string
            # literals at the call site.
            name="pgcolumnar.analyze()")
        return

    warnings = []
    pgc_conn.add_notice_handler(lambda diag: warnings.append(diag.message_primary))
    _exec(pgc_conn, AF_C)

    # --- premise: the fixture really is one-in-ten NULL ----------------------
    #
    # Everything below compares against 0.1. If the fixture is not 10% NULL then a
    # pass means the function agreed with a number that was never true.
    expect.text(
        str(_one(pgc_conn, "SELECT round(count(*) FILTER (WHERE k IS NULL)::numeric "
                           "/ count(*), 6) FROM af_c")),
        "0.100000", "premise: the fixture is exactly one-in-ten NULL")

    _exec(pgc_conn, "ANALYZE af_c")
    # Captured BEFORE our call, because our call overwrites them. Reading these
    # afterwards would compare our own output against itself.
    core_nullfrac = _stat(pgc_conn, "af_c", "k", "null_frac")
    core_ndistinct = _stat(pgc_conn, "af_c", "k", "n_distinct")
    core_correlation = _stat(pgc_conn, "af_c", "k", "correlation")

    expect.text("yes" if isinstance(core_nullfrac, float) else f"no (got [{core_nullfrac}])",
                "yes",
                "premise: core ANALYZE produced a null_frac at all, so the numbers below exist")

    true_nullfrac7 = Decimal(str(_one(
        pgc_conn, "SELECT round(count(*) FILTER (WHERE k7 IS NULL)::numeric "
                  "/ count(*), 6) FROM af_c")))
    expect.text(str(true_nullfrac7), "0.142856",
                "premise: the k7 fixture is exactly one row in seven NULL")

    # PER COLUMN, not the global default. attstattarget overrides
    # default_statistics_target, and a per-column target large enough to make core
    # census the table would leave this premise describing a 30,000-row sample that
    # did not happen.
    sample_rows = 300 * int(_one(
        pgc_conn,
        "SELECT coalesce(nullif(attstattarget, -1), "
        "current_setting('default_statistics_target')::int) FROM pg_attribute "
        "WHERE attrelid = 'af_c'::regclass AND attname = 'k7'"))
    expect.text(
        "yes" if sample_rows < ROWS else f"no (sample {sample_rows} covers all {ROWS} rows)",
        "yes",
        "premise: core samples fewer rows than the table holds, so its number is an estimate")

    # THE ARITHMETIC THAT REPLACES A COIN FLIP. An earlier version of this suite
    # gated on core's sampled number DIFFERING from the truth, and failed a correct
    # suite about one run in 130 when the sample landed on its own mode (#487).
    # There is no whole number of sampled rows that yields 0.142856.
    product = true_nullfrac7 * sample_rows
    expect.text(
        "yes" if product != product.to_integral_value()
        else f"no ({product} is a whole number of rows)", "yes",
        "premise: and no whole number of sampled rows gives that fraction, so core cannot report it")

    core_nullfrac7 = _stat(pgc_conn, "af_c", "k7", "null_frac")
    # PRINTED, NOT ASSERTED -- see the module docstring. Whether core's draw landed
    # on the truth is a property of a random sample.
    print(f"-- core sampled null_frac: k = {core_nullfrac} (truth 0.1), "
          f"k7 = {core_nullfrac7} (truth {true_nullfrac7})")

    # --- null_frac is exact, from reading the column -------------------------
    fail = _analyze(pgc_conn, "af_c", ["k"], warnings)
    expect.text(fail or "no error", "no error",
                "pgcolumnar.analyze() ran without raising, so the statistics below are its own")
    expect.num(_stat(pgc_conn, "af_c", "k", "null_frac"), 0.1,
               "pgcolumnar.analyze() reports null_frac exactly, from reading the column")

    # The claim that carries the weight: 0.1 is reachable by luck, 0.142856 is not
    # a number a 30,000-row sample can produce at all.
    _analyze(pgc_conn, "af_c", ["k7"], warnings)
    expect.text(str(Decimal(str(_stat(pgc_conn, "af_c", "k7", "null_frac"))).quantize(Decimal("0.000001"))),
                "0.142856", "and exactly for a fraction core's sample cannot express")

    # The negative half, stated separately. If this ever passes by MATCHING, the
    # arithmetic premise above is wrong and everything resting on it needs rereading.
    expect.text(
        "no (core reported the exact truth)"
        if Decimal(str(core_nullfrac7)) == true_nullfrac7 else "yes", "yes",
        "and core's own number for it is not the truth, as the arithmetic requires")

    # --- n_distinct is exact, from reading one column ------------------------
    true_ndistinct = _one(pgc_conn, "SELECT count(DISTINCT k) FROM af_c")
    expect.num(true_ndistinct, 45001,
               "premise: the fixture has the cardinality this check compares against")
    # Under 10% of the rows, so core's rule keeps this a positive absolute count
    # rather than a negated fraction.
    expect.text("yes" if true_ndistinct <= 0.1 * ROWS
                else f"fraction side: {true_ndistinct} distinct of {ROWS} rows "
                     f"is over the {0.1 * ROWS:.0f} threshold", "yes",
                "premise: the fixture stays on the absolute-count side of core's 10% rule")
    print(f"-- core sampled n_distinct = {core_ndistinct}, truth = {true_ndistinct}")
    expect.num(_stat(pgc_conn, "af_c", "k", "n_distinct"), float(true_ndistinct),
               "pgcolumnar.analyze() reports n_distinct exactly, from one column")

    # --- it must not destroy the statistics it does not compute --------------
    #
    # pg_restore_attribute_stats is a RESTORE api: kinds not named in the call could
    # be cleared rather than left alone. Watching correlation, which core collects
    # and this function still does not -- watching something we write would make the
    # check assert nothing.
    ours_correlation = _stat(pgc_conn, "af_c", "k", "correlation")
    print(f"-- correlation before={core_correlation} after={ours_correlation}")
    expect.text(
        "yes" if isinstance(ours_correlation, float) and isinstance(core_correlation, float)
        else f"no (correlation was [{core_correlation}], is now [{ours_correlation}])",
        "yes", "pgcolumnar.analyze() leaves the statistics it does not compute in place")

    # --- histogram_bounds, whose top end is exact ----------------------------
    #
    # `k` cannot test this: uniform over 0..45000, so core's sample almost certainly
    # hits both extremes and its bounds are already near-exact. `skew` holds
    # 1,000,000 in ONE row of 500,000, which a sample misses and a full read cannot.
    # Core's target is cut to 10 here so the sample is ~3,000 rows and the odds of
    # it finding a one-in-500,000 outlier drop from ~6% to ~0.6%.
    _exec(pgc_conn, "ALTER TABLE af_c ALTER COLUMN skew SET STATISTICS 10")
    expect.num(_one(pgc_conn, "SELECT max(skew) FROM af_c"), 1000000,
               "premise: the outlier really is in the table")
    expect.num(_one(pgc_conn, "SELECT count(*) FROM af_c WHERE skew = 1000000"), 1,
               f"premise: and it really is one row in {ROWS}")

    _exec(pgc_conn, "ANALYZE af_c")
    core_hist_max = _one(
        pgc_conn,
        "SELECT (histogram_bounds::text::int[])[array_length(histogram_bounds::text::int[], 1)] "
        "FROM pg_stats WHERE schemaname = current_schema() "
        "AND tablename = 'af_c' AND attname = 'skew'")
    # REPORTED, DELIBERATELY NOT ASSERTED. "Core misses the outlier" is
    # probabilistic by definition and also depends on how the access method hands
    # rows to the sampler, so a red here would mean "the sample was lucky".
    print(f"-- core sampled histogram max = {core_hist_max}, truth = 1000000")
    print("-- core missed the outlier this run, which is the case that motivates #414"
          if isinstance(core_hist_max, int) and core_hist_max < 200000
          else "-- core happened to sample the outlier this run; exactness is asserted regardless")

    _analyze(pgc_conn, "af_c", ["skew"], warnings)
    ours_hist = _one(
        pgc_conn, "SELECT histogram_bounds::text::int[] FROM pg_stats "
                  "WHERE schemaname = current_schema() AND tablename = 'af_c' "
                  "AND attname = 'skew'")
    print(f"-- ours histogram max = {ours_hist[-1] if ours_hist else None}")
    expect.num(ours_hist[-1] if ours_hist else None, 1000000,
               "pgcolumnar.analyze() puts the true maximum at the top of histogram_bounds")

    # Both ends, not just the interesting one. The bottom is the smallest value that
    # is NOT most-common, which is not the column minimum: `skew` is g % 100000, so
    # 0 occurs five times and is excluded as an MCV while 1 occurs four and is not.
    # The expectation is DERIVED from the written MCV list, so it stays right if the
    # fixture or the bucket count moves.
    expect.num(
        ours_hist[0] if ours_hist else None,
        _one(pgc_conn,
             "WITH m AS (SELECT most_common_vals::text::int[] AS v FROM pg_stats "
             "WHERE schemaname = current_schema() AND tablename = 'af_c' AND attname = 'skew') "
             "SELECT min(a.skew) FROM af_c a, m WHERE a.skew <> ALL (m.v)"),
        "and the smallest non-most-common value at the bottom")

    expect.num(
        _one(pgc_conn,
             "SELECT count(*) FROM pg_stats s, unnest(s.histogram_bounds::text::int[]) b "
             "WHERE s.schemaname = current_schema() AND s.tablename = 'af_c' "
             "AND s.attname = 'skew' AND b = ANY (s.most_common_vals::text::int[])"), 0,
        "and no most-common value is inside skew's histogram either")

    # percentile_disc returns values the column HOLDS. percentile_cont would
    # interpolate and invent ones it does not.
    missing = [b for b in (ours_hist or [])
               if _one(pgc_conn, "SELECT count(*) FROM af_c WHERE skew = %s", (b,)) == 0]
    expect.num(len(missing), 0, "every bound is a value the column actually holds")

    # A histogram is an ordered ladder, not a pair of extremes. Asserting only the
    # last element would pass for an array of two, which would ruin every estimate
    # between the ends.
    length = len(ours_hist) if ours_hist else 0
    expect.text("yes" if length >= 5 else f"no (length [{length}])", "yes",
                "and it is an ordered ladder rather than two extremes")
    expect.text("yes" if ours_hist == sorted(ours_hist or []) else "no", "yes",
                "and it is sorted ascending, which a histogram must be to be usable")

    # --- most_common_vals and most_common_freqs ------------------------------
    #
    # `cat` is built so that "frequency is count / TOTAL rows INCLUDING nulls" is
    # observable, because it is the term that fails silently. Dividing by the
    # 450,000 non-null rows instead of 500,000 gives 0.2/0.12/0.06: three numbers
    # that are individually plausible, sum to less than one, and are wrong.
    expect.num(_one(pgc_conn, "SELECT count(*) FROM af_c"), ROWS,
               "premise: the MCV fixture has the row count the frequencies divide by")
    expect.num(_one(pgc_conn, "SELECT count(*) FROM af_c WHERE cat = 7"), 90000,
               "premise: value 7 appears exactly 90,000 times")
    expect.num(_one(pgc_conn, "SELECT count(*) FROM af_c WHERE cat = 42"), 54000,
               "premise: value 42 appears exactly 54,000 times")
    expect.num(_one(pgc_conn, "SELECT count(*) FROM af_c WHERE cat = 99"), 27000,
               "premise: value 99 appears exactly 27,000 times")
    # The rule is "count > 1", so this is what makes the expected list exactly three
    # long. If the tail stopped being unique the list would fill with tied values.
    expect.num(_one(pgc_conn, "SELECT count(*) FROM (SELECT cat FROM af_c "
                              "WHERE cat IS NOT NULL GROUP BY cat HAVING count(*) > 1) t"), 3,
               "premise: and nothing else in the column repeats, so the list is exactly three")

    _exec(pgc_conn, "ANALYZE af_c")
    core_mcv = _stat(pgc_conn, "af_c", "cat", "most_common_vals::text")
    print(f"-- core sampled MCVs = {core_mcv}")

    # CLEARED BEFORE OUR CALL, and this is not tidiness. Written without it, every
    # check below reads core's leftover list and none can tell "our function wrote
    # this" from "core wrote it and ours left it alone". A function writing no MCVs
    # at all once scored three of four that way.
    _exec(pgc_conn,
          "SELECT pg_catalog.pg_clear_attribute_stats(current_schema()::text, "
          "'af_c', 'cat', false)")
    # Clearing removes the whole pg_statistic ROW, so the pg_stats row is absent
    # rather than null-valued -- `_stat` returns None for exactly that.
    cleared = _stat(pgc_conn, "af_c", "cat", "most_common_vals::text")
    expect.text("<cleared>" if cleared is None else str(cleared), "<cleared>",
                "premise: the MCV list really is gone before we write, so what follows is ours")

    fail = _analyze(pgc_conn, "af_c", ["cat"], warnings)
    expect.text(fail or "no error", "no error",
                "pgcolumnar.analyze() ran without raising for the MCV column")
    # A WARNING here IS the silent wrong write: pg_restore_attribute_stats takes
    # VARIADIC "any", so a float8[] where real[] is wanted is a dropped argument and
    # a successful call that stored nothing.
    print(f"-- notices during the MCV call: {warnings or 'none'}")
    expect.num(len(warnings), 0,
               "and without a WARNING, which is how a mistyped argument is dropped")

    expect.text(_stat(pgc_conn, "af_c", "cat", "most_common_vals::text"), "{7,42,99}",
                "pgcolumnar.analyze() writes the three repeated values as most_common_vals")

    # Each frequency against its OWN independently counted truth, not against a
    # recomputation of what the function did.
    freqs = _stat(pgc_conn, "af_c", "cat", "most_common_freqs")

    def _freq(idx):
        return str(Decimal(str(freqs[idx])).quantize(Decimal("0.000001")))

    def _truth(count):
        return str((Decimal(count) / ROWS).quantize(Decimal("0.000001")))

    # UNROLLED, not a loop over (value, count, name) triples. `compare_to_bash`
    # resolves a name bound by a loop only for the simple shapes it was taught
    # (#1045 class 2), and a nested-tuple unpack is not one of them: written as a
    # loop these three reported MISSING while the strings sat in the file. The
    # shell suite spells them out too.
    expect.text(_freq(0), _truth(90000),
                "and 7's frequency exactly, over total rows rather than non-null rows")
    expect.text(_freq(1), _truth(54000), "and 42's")
    expect.text(_freq(2), _truth(27000), "and 99's")

    # --- the exclusion, which is why this could not be a line added above ----
    #
    # Core builds the histogram from the values left AFTER the most-common ones are
    # removed. Writing both lists without that exclusion counts those values twice
    # in selectivity: eqsel takes the MCV frequency and the range estimators count
    # it again inside whichever bucket holds it. Nothing errors.
    #
    # Asserted FIRST, because the exclusion check is vacuously true when there is no
    # histogram at all -- "no MCV appears in histogram_bounds" passes trivially
    # against NULL, so a change that stopped emitting histograms would read as a fix.
    cat_hist = _one(
        pgc_conn, "SELECT histogram_bounds::text::int[] FROM pg_stats "
                  "WHERE schemaname = current_schema() AND tablename = 'af_c' "
                  "AND attname = 'cat'")
    expect.text("yes" if cat_hist and len(cat_hist) >= 2
                else f"no ({len(cat_hist) if cat_hist else 0} bounds)", "yes",
                "premise: a histogram exists for cat, so the exclusion below is not vacuous")

    expect.num(
        _one(pgc_conn,
             "SELECT count(*) FROM pg_stats s, unnest(s.histogram_bounds::text::int[]) b "
             "WHERE s.schemaname = current_schema() AND s.tablename = 'af_c' "
             "AND s.attname = 'cat' AND b = ANY (s.most_common_vals::text::int[])"), 0,
        "no most-common value appears in histogram_bounds, which would double-count it")

    # The same fact from the other side, and the one that shows the histogram is over
    # the remaining POPULATION rather than merely filtered at the ends. Truth comes
    # from an independent query, not from the function.
    expect.num(cat_hist[0],
               _one(pgc_conn, "SELECT min(cat) FROM af_c WHERE cat NOT IN (7, 42, 99)"),
               "so the bottom bound is the smallest non-most-common value, not the column minimum")

    # --- the per-column statistics target, which core reads ------------------
    #
    # Core sizes both lists from the COLUMN's attstattarget, not the global default.
    # This function read the global setting for every column, which silently ignored
    # ALTER TABLE ... SET STATISTICS.
    expect.num(_one(pgc_conn, "SELECT attstattarget FROM pg_attribute "
                              "WHERE attrelid = 'af_c'::regclass AND attname = 'skew'"), 10,
               "premise: skew really is at a non-default statistics target")
    expect.num(int(_one(pgc_conn, "SHOW default_statistics_target")), 100,
               "premise: and the global default differs from it, so the two are distinguishable")

    # CLEARED AND RE-RUN, which this check needs and did not originally have: the
    # plain ANALYZE above analyses EVERY column of af_c, skew included, and core
    # honours attstattarget -- so reading skew's histogram after it reads CORE's, and
    # the check passed at eleven bounds while the function was still producing 101.
    _exec(pgc_conn, "SELECT pg_catalog.pg_clear_attribute_stats(current_schema()::text, "
                    "'af_c', 'skew', false)")
    _analyze(pgc_conn, "af_c", ["skew"], warnings)

    # Ten buckets means eleven bounds: percentile_disc is asked for target+1
    # fractions, which is the shape core caps at num_bins+1.
    skew_hist = _one(
        pgc_conn, "SELECT histogram_bounds::text::int[] FROM pg_stats "
                  "WHERE schemaname = current_schema() AND tablename = 'af_c' "
                  "AND attname = 'skew'")
    expect.num(len(skew_hist) if skew_hist else 0, 11,
               "the histogram honours the column's statistics target, not the global default")

    skew_mcv = _one(
        pgc_conn, "SELECT most_common_vals::text::int[] FROM pg_stats "
                  "WHERE schemaname = current_schema() AND tablename = 'af_c' "
                  "AND attname = 'skew'")
    mcv_len = len(skew_mcv) if skew_mcv else 0
    expect.text("yes" if mcv_len <= 10 else f"no (length [{mcv_len}])", "yes",
                "and so does the most-common list, which is capped by the same target")

    # Zero is not "a small target", it is "do not collect". A column set to zero that
    # comes back with statistics has had the DBA's instruction overridden, and the
    # planner is then using numbers somebody deliberately turned off.
    _exec(pgc_conn, "ALTER TABLE af_c ALTER COLUMN pad1 SET STATISTICS 0")
    _exec(pgc_conn, "SELECT pg_catalog.pg_clear_attribute_stats(current_schema()::text, "
                    "'af_c', 'pad1', false)")
    fail = _analyze(pgc_conn, "af_c", ["pad1"], warnings)
    expect.text(fail or "no error", "no error",
                "pgcolumnar.analyze() ran for the zero-target column without raising")
    wrote = _stat(pgc_conn, "af_c", "pad1", "'wrote-' || attname")
    expect.text(wrote if wrote is not None else "<nothing>", "<nothing>",
                "a column at SET STATISTICS 0 is left alone, because that is what zero means")


def test_histogram_bounds_are_a_positional_stride(pgc_conn, expect):
    """Core's bounds are POSITIONS, not quantiles, and the difference is observable.

    `compute_scalar_stats` places bound i at `values[floor(i * (nvals - 1) /
    (num_hist - 1))]`, while `percentile_disc` resolves a fraction p to index
    `ceil(p * nv) - 1`. Those are different indexes and therefore different VALUES
    whenever the shift crosses a value boundary.

    THE BIG FIXTURE CANNOT SHOW THIS. With many rows per distinct value a one-row
    shift lands on the same value and both algorithms agree, so the check would pass
    either way -- a fixture that cannot distinguish two implementations cannot test
    them. Eleven distinct rows at a statistics target of 3 can:

        nv = 11, nhist = 4, divisor 3
        stride          i=2 -> floor(2*10/3) = 6 -> the 7th value = 7
        percentile_disc i=2 -> ceil(2*11/3)-1 = 7 -> the 8th value = 8

    The expectation is NOT taken from the implementation. It is computed by the
    oracle below straight from core's formula over row_number(), and it is also
    hand-workable -- values 1..11 each appearing once, so position p holds value p+1
    and the bounds are {1,4,7,11}. Two independent derivations that must agree with
    each other before either judges the code.
    """
    readable, _raw, major = _major(pgc_conn)
    if not readable or major < 18:
        _decline_unnamed(expect, major)
        return

    _exec(pgc_conn, "CREATE TABLE af_h11 (v int) USING pgcolumnar")
    _exec(pgc_conn, "INSERT INTO af_h11 SELECT generate_series(1, 11)")
    _exec(pgc_conn, "ALTER TABLE af_h11 ALTER COLUMN v SET STATISTICS 3")

    expect.num(_one(pgc_conn, "SELECT count(*) FROM (SELECT v FROM af_h11 "
                              "GROUP BY v HAVING count(*) > 1) t"), 0,
               "premise: the stride fixture has no repeated value, so no MCV is excluded")

    oracle = _one(pgc_conn, """
        WITH nonmcv AS (
                SELECT v, row_number() OVER (ORDER BY v) - 1 AS pos
                  FROM af_h11 WHERE v IS NOT NULL),
             n AS (SELECT count(*)::bigint AS nv FROM nonmcv),
             p AS (SELECT floor(i::numeric * (n.nv - 1) / (4 - 1))::bigint AS pos
                     FROM generate_series(0, 3) i, n)
        SELECT (SELECT array_agg(v ORDER BY pos) FROM nonmcv
                 WHERE pos IN (SELECT pos FROM p))::text""")
    # If the oracle and the hand-worked figure ever disagree, the oracle is wrong and
    # nothing below it means anything.
    expect.text(oracle, "{1,4,7,11}",
                "premise: the independent oracle agrees with the hand-worked bounds")

    _analyze(pgc_conn, "af_h11", ["v"], [])
    written = _stat(pgc_conn, "af_h11", "v", "histogram_bounds::text")
    expect.text("present" if written is not None else "none", "present",
                "premise: a histogram was written, so the comparison below is not vacuous")
    expect.text(written, oracle,
                "histogram_bounds are core's positional stride, not evenly spaced quantiles")


def test_null_frac_counts_live_rows_not_ones_a_delete_left(pgc_conn, expect):
    """#485. null_frac used to come from the zone maps, which count what was WRITTEN.

    A DELETE marks rows dead without rewriting those counts, so the fraction stayed
    normalised against a population the table no longer held, and VACUUM did not
    clear it.

    The consequence is worse than the size of the error. null_frac came from the zone
    maps while the most-common frequencies came from `count(*)`, so ONE pg_stats row
    carried two statistics normalised against different populations and
    `null_frac + sum(mcv_freqs) + rest = 1` stopped holding. eqsel subtracts both to
    price everything else, so the residual went wrong by the difference -- which is
    why the last assertion here is that the two agree on the row count, not merely
    that each is individually plausible.
    """
    readable, _raw, major = _major(pgc_conn)
    if not readable or major < 18:
        _decline_unnamed(expect, major)
        return

    # 1,200 rows: 120 null, 300 holding 7, 300 holding 9, the rest unique. Deleting
    # the 9s leaves 900 rows and keeps 7 in the most-common list, so both
    # denominators are observable in the same written row.
    _exec(pgc_conn, "CREATE TABLE af_del (v int) USING pgcolumnar")
    _exec(pgc_conn, """
        INSERT INTO af_del
        SELECT CASE WHEN i % 10 = 0 THEN NULL
                    WHEN i %  4 = 0 THEN 7
                    WHEN i %  4 = 1 THEN 9
                    ELSE 100000 + i END
          FROM generate_series(1, 1200) i""")
    _exec(pgc_conn, "DELETE FROM af_del WHERE v = 9")

    live = _one(pgc_conn, "SELECT count(*) FROM af_del")
    written = _one(pgc_conn, """
        SELECT sum(z.value_count + z.null_count)
          FROM pgcolumnar.zone_map z
          JOIN pgcolumnar.storage s ON s.storage_id = z.storage_id
         WHERE s.relation_oid = 'af_del'::regclass
           AND z.column_index = 0 AND z.vector_index = -1""")
    print(f"-- live rows {live}, rows the zone maps describe {written}")
    expect.text("fewer" if live < written
                else f"{live} live against {written} the zone maps describe", "fewer",
                "premise: the delete left fewer live rows than the zone maps describe")

    _analyze(pgc_conn, "af_del", ["v"], [])

    # 120 nulls in 900 live rows. Against the zone maps this read 0.1.
    expect.text(
        str(Decimal(str(_stat(pgc_conn, "af_del", "v", "null_frac"))).quantize(Decimal("0.000001"))),
        "0.133333", "null_frac counts live rows, not rows a DELETE left behind")

    expect.num(
        _one(pgc_conn,
             "SELECT CASE WHEN most_common_vals::text::int[] @> ARRAY[7] THEN 1 ELSE 0 END "
             "FROM pg_stats WHERE schemaname = current_schema() "
             "AND tablename = 'af_del' AND attname = 'v'"), 1,
        "premise: 7 survived the delete and is still a most-common value")

    # THE POINT OF THE PAIR: both statistics must imply the same table. Divide each by
    # the count it describes and the row count that falls out must be the real one,
    # from both directions. Computed in Python from three independent counts rather
    # than in one SQL CASE, so a disagreement prints both figures.
    nulls = _one(pgc_conn, "SELECT count(*) FROM af_del WHERE v IS NULL")
    sevens = _one(pgc_conn, "SELECT count(*) FROM af_del WHERE v = 7")
    null_frac = Decimal(str(_stat(pgc_conn, "af_del", "v", "null_frac")))
    freq7 = Decimal(str(_stat(pgc_conn, "af_del", "v", "most_common_freqs")[0]))
    from_nulls = int((Decimal(nulls) / null_frac).to_integral_value())
    from_sevens = int((Decimal(sevens) / freq7).to_integral_value())
    expect.text(
        "yes" if from_nulls == live and from_sevens == live
        else f"no ({from_nulls} vs {from_sevens}, live {live})", "yes",
        "null_frac and the most-common frequencies agree on how many rows there are")


def test_the_documented_statistics_are_the_ones_written(pgc_conn, expect):
    """The list in `docs/sql-reference.md` is a claim about this function.

    A claim in prose is the kind that rots quietly: nothing builds it, no suite reads
    it, and it is wrong only for the reader. So the table is parsed and compared
    against what the function actually populates -- source text on one side, live
    catalog on the other.

    The section is bounded at the next `### ` heading. The shell suite's first
    version used a sed range ending at `/^## /`, which ran past the next `###` and
    swallowed two neighbouring tables, comparing against seventeen names from three
    sections. The regex here stops at the same boundary for the same reason.
    """
    readable, _raw, major = _major(pgc_conn)
    if not readable or major < 18:
        _decline_unnamed(expect, major)
        return

    doc = (SRCDIR / "docs" / "sql-reference.md").read_text()
    section = re.split(r"^### ", doc, flags=re.M)
    body = next((s for s in section if s.startswith("pgcolumnar.analyze(")), "")
    listed = sorted(set(re.findall(r"^\| `([a-z_]+)`", body, flags=re.M)))
    print(f"-- the doc lists: {listed}")
    expect.text("yes" if listed else "no", "yes",
                "premise: the doc lists the statistics it claims to write")

    _exec(pgc_conn, "CREATE TABLE af_doc (k int, t text) USING pgcolumnar")
    _exec(pgc_conn, "INSERT INTO af_doc SELECT g % 500, 'v' || (g % 90) "
                    "FROM generate_series(1, 20000) g")
    _analyze(pgc_conn, "af_doc", ["k"], [])

    written = sorted(
        name for name in ("histogram_bounds", "most_common_freqs", "most_common_vals",
                          "n_distinct", "null_frac")
        if _stat(pgc_conn, "af_doc", "k", f"{name} IS NOT NULL"))
    expect.text(" ".join(written), " ".join(listed),
                "every statistic the doc lists is one the function writes")

    # The negative the doc states in prose. Without it the check above passes if the
    # doc silently drops a column it should list.
    expect.text("t" if _stat(pgc_conn, "af_doc", "k", "correlation IS NULL") else "f",
                "t", "and correlation is absent, as the doc says")

    expect.num(_one(pgc_conn, "SELECT reltuples::bigint FROM pg_class "
                              "WHERE relname = 'af_doc' "
                              "AND relnamespace = current_schema()::regnamespace"), -1,
               "reltuples is untouched, as the doc says")


def _stats_cols(conn, table):
    """How many of `table`'s columns hold statistics after a fresh analyze()."""
    _exec(conn, f"DELETE FROM pg_statistic WHERE starelid = '{table}'::regclass")
    _exec(conn, f"SELECT pgcolumnar.analyze('{table}'::regclass)")
    return int(_one(conn, f"SELECT count(*) FROM pg_stats WHERE tablename = '{table}'"))


def _first_in_heap(conn, table, base_sid):
    got = _one(
        conn,
        f"SELECT CASE WHEN storage_id = {base_sid} THEN 'base' ELSE 'proj' END"
        f"  FROM pgcolumnar.storage WHERE relation_oid = '{table}'::regclass::oid"
        f"  ORDER BY ctid LIMIT 1",
    )
    return str(got)


def _try_analyze(conn, table):
    """'succeeded' or 'refused <sqlstate>', as a VALUE rather than a NOTICE.

    A DO block's RAISE NOTICE lands on the client's message stream, not in a
    result set, so an arm reading it gets nothing back and cannot tell a
    refusal that did not happen from one it could not see.
    """
    _exec(
        conn,
        "CREATE OR REPLACE FUNCTION af_try_py(r regclass) RETURNS text"
        " LANGUAGE plpgsql AS $fn$"
        " BEGIN PERFORM pgcolumnar.analyze(r); RETURN 'succeeded';"
        " EXCEPTION WHEN OTHERS THEN RETURN 'refused ' || SQLSTATE; END $fn$",
    )
    return str(_one(conn, f"SELECT af_try_py('{table}'::regclass)"))


def test_a_covering_projection_does_not_decide_which_columns_get_statistics(pgc_own_db, expect):
    """analyze() resolved its storage id by relation_oid, which is not unique (#1276).

    A covering projection gets its OWN row in `pgcolumnar.storage` carrying the
    BASE table's `relation_oid`, so `SELECT ... INTO` took whichever row heap
    order handed back first without complaining about the second. Same root
    cause as #1210; that fixed the C lookup and does not touch this one.

    THE HARM IS MISSING STATISTICS, NOT WRONG ONES. The values come from reading
    the column. `sid` is only a gate: a projection NARROWER than its base fails
    it for every column index the projection does not carry, and `CONTINUE`
    skips those columns in silence with a successful return.

    A TWO-COLUMN BASE CANNOT SEE THIS -- the projection carries zone maps for
    both indexes, the gate passes either way, and the statistics come out
    identical to the control. Five columns covered by one is the shape that
    separates them.

    ON A PRIVATE DATABASE, because the fixture writes `pgcolumnar.storage` by
    hand to move a row and reads `pg_statistic` for one relation.
    """
    conn = pgc_own_db
    ok, raw, major = _major(conn)
    expect.num(1 if ok else 0, 1, "premise: the server major is readable")
    if major < 18:
        _decline_unnamed(expect, raw)
        return

    _exec(conn, "SET pgcolumnar.stripe_row_limit = 150000")
    _exec(conn, "CREATE TABLE ap (a int, b text, c int, d int, e int) USING pgcolumnar")
    _exec(
        conn,
        "INSERT INTO ap SELECT g, 'x'||(g%50), g%7, g%11, g%13"
        " FROM generate_series(1,20000) g",
    )
    _exec(conn, "SET pgcolumnar.stripe_row_limit = 3000")
    _exec(conn, "SELECT pgcolumnar.add_projection('ap', 'cov_b', '{b}', '{b}')")
    _exec(conn, "RESET pgcolumnar.stripe_row_limit")
    _exec(conn, "ANALYZE ap")

    base_sid = int(_one(conn, "SELECT pgcolumnar.get_storage_id('ap'::regclass)"))
    other_sid = int(
        _one(
            conn,
            "SELECT storage_id FROM pgcolumnar.storage"
            f" WHERE relation_oid = 'ap'::regclass::oid AND storage_id <> {base_sid}",
        )
    )
    n_rows = int(
        _one(conn, "SELECT count(*) FROM pgcolumnar.storage WHERE relation_oid = 'ap'::regclass::oid")
    )
    idx_of = lambda sid: _one(
        conn,
        "SELECT string_agg(DISTINCT column_index::text, ',' ORDER BY column_index::text)"
        f" FROM pgcolumnar.zone_map WHERE storage_id = {sid} AND vector_index = -1",
    )
    base_idx, cov_idx = str(idx_of(base_sid)), str(idx_of(other_sid))
    print(f"-- ap owns {n_rows} storage rows; base covers [{base_idx}], projection covers [{cov_idx}]")

    # A SKIP AND AN ABSENCE LOOK IDENTICAL DOWNSTREAM, so the indexes each
    # storage actually carries are read from the catalog and asserted, not
    # assumed from the DDL.
    expect.num(n_rows, 2, "premise: a covering projection gave the table a second storage row")
    expect.text(base_idx, "0,1,2,3,4", "premise: the base storage covers every column index")
    expect.text(cov_idx, "0,1", "premise: the projection's storage covers fewer of them")

    # PUT THE BASE ROW FIRST by writing the OTHER one. Assuming a heap position
    # is the defect this test is about.
    _exec(
        conn,
        f"UPDATE pgcolumnar.storage SET row_group_limit = row_group_limit WHERE storage_id = {other_sid}",
    )
    first_base = _first_in_heap(conn, "ap", base_sid)
    cols_base = _stats_cols(conn, "ap")

    # THE MUTATION ALTERS NO VALUE: it writes the base row back unchanged, which
    # moves it later in the heap exactly as any real write to it would.
    _exec(
        conn,
        f"UPDATE pgcolumnar.storage SET row_group_limit = row_group_limit WHERE storage_id = {base_sid}",
    )
    first_flip = _first_in_heap(conn, "ap", base_sid)
    cols_flip = _stats_cols(conn, "ap")
    print(f"-- first in the heap: {first_base} -> {first_flip};  columns with statistics: {cols_base} -> {cols_flip}")

    expect.text(first_base, "base", "premise: the base storage row was first")
    expect.text(first_flip, "proj", "premise: the update put the projection's row first")
    expect.num(cols_base, 5, "premise: every column has statistics when the base row is resolved")

    expect.num(
        cols_flip,
        5,
        "pgcolumnar.analyze() covers every column whichever storage row the heap returns first",
    )

    # THE REFUSAL BRANCH MUST STAY ALIVE. A never-written columnar table has a
    # READABLE metapage and NO storage row, so resolving through the metapage
    # still leaves sid NULL. Without this the fix could make that branch dead
    # code and nothing would say so.
    _exec(conn, "CREATE TABLE ap_empty (a int, b text) USING pgcolumnar")
    empty_rows = int(
        _one(
            conn,
            "SELECT count(*) FROM pgcolumnar.storage"
            " WHERE storage_id = pgcolumnar.get_storage_id('ap_empty'::regclass)",
        )
    )
    empty_state = _try_analyze(conn, "ap_empty")
    print(f"-- a never-written columnar table: rows by metapage id={empty_rows}, analyze() {empty_state}")

    expect.num(empty_rows, 0, "premise: a never-written columnar table has no storage row to find")
    # THE HELPER MUST BE ABLE TO REPORT A SUCCESS TOO, or 'refused' is the only
    # thing it can ever say and the arm below cannot fail.
    expect.text(
        _try_analyze(conn, "ap"), "succeeded", "premise: the capture helper reports a success when there is one"
    )
    expect.text(
        empty_state,
        "refused P0001",
        "pgcolumnar.analyze() still refuses a relation that has never been written",
    )

    # AND THE BEHAVIOUR CHANGE IS ASSERTED, NOT LEFT TO BE DISCOVERED. A matview
    # created WITH DATA has an orphaned relation_oid until its first REFRESH
    # (#1275). Keyed on relation_oid this refused a matview holding rows;
    # through the metapage it resolves. THAT IS NOT #1275 BEING FIXED -- the
    # orphan is still in the catalog for every other reader.
    _exec(conn, "CREATE MATERIALIZED VIEW ap_mv USING pgcolumnar AS SELECT a, b, c FROM ap")
    mv_by_reloid = int(
        _one(conn, "SELECT count(*) FROM pgcolumnar.storage WHERE relation_oid = 'ap_mv'::regclass::oid")
    )
    mv_by_meta = int(
        _one(
            conn,
            "SELECT count(*) FROM pgcolumnar.storage"
            " WHERE storage_id = pgcolumnar.get_storage_id('ap_mv'::regclass)",
        )
    )
    mv_state = _try_analyze(conn, "ap_mv")
    print(f"-- a WITH DATA matview: by relation_oid={mv_by_reloid}, by metapage={mv_by_meta}, analyze() {mv_state}")

    expect.num(mv_by_reloid, 0, "premise: the matview's relation_oid still finds no storage row")
    expect.num(mv_by_meta, 1, "premise: and its metapage still finds exactly one")
    expect.text(
        mv_state,
        "succeeded",
        "pgcolumnar.analyze() now reaches a matview whose relation_oid is orphaned",
    )
