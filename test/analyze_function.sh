#!/usr/bin/env bash
#
# pgColumnar pgcolumnar.analyze(): per-column statistics without reading the
# whole table (issue #414).
#
# Core ANALYZE decodes essentially the entire table. It samples 30,000 rows, and
# on a table of any size those rows are spread across every row group, so every
# group is decoded for every column. Measured on 3M rows x 20 columns (1237 MB,
# incompressible fixture, serial):
#
#   decode all 19 text columns (a full-table read)   7,680 ms
#   ANALYZE w (20 columns)                           6,302 ms
#   decode only k out of the 20-column table           268 ms
#   heap ANALYZE wh (k)                                186 ms
#
# So ANALYZE costs about what reading the whole table costs, and reading one
# column costs 23.5x less. That gap is what this function exists to collect.
#
# It cannot be fixed in the table-AM callbacks, and that is settled rather than
# assumed. acquire_sample_rows copies whole tuples (ExecCopySlotHeapTuple), so
# the AM cannot decline to produce columns core is about to copy -- ANALYZE w
# costs 6,302 ms against ANALYZE w (k) at 6,073 ms, a 6% saving for asking for
# one column out of twenty. Nor is there slack in which row groups the sample
# touches: rebuilding at a tenth the chunk_group_row_limit left the cost
# unchanged (6,466 ms against 6,771 ms), because a fixed-size sample simply
# touches proportionally more groups when they are smaller.
#
# This suite is about the function, not the AM sampler. test/analyze_stats.sh
# covers the sampler (#154) and stays the correctness path for plain ANALYZE.
#
# Slice 1 asserts null_frac is EXACT where core's is sampled. It came from the
# zone maps until #485: those counts describe what was WRITTEN, so a DELETE left
# the fraction normalised against rows the table no longer held. It now comes
# from the same read as n_distinct. Slice 2 asserts n_distinct is exact from
# reading ONE column, which is the claim the whole issue rests on.
#
# Usage:  test/analyze_function.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg18/bin/pg_config}"

ROWS=${PGC_ANALYZE_ROWS:-500000}

# Writing statistics uses pg_restore_attribute_stats, which core added in 18.
# On 15 to 17 this would mean writing pg_statistic directly, which is a real
# version-support decision and not a detail (stavalues anyarray typing, staop,
# stacoll, stadistinct's sign convention). Refuse rather than silently narrow:
# pgc_skip fails by default and must be waived deliberately.
#
# A version gate, NOT pgc_skip. pgc_skip is for a missing DEPENDENCY, which is an
# environment defect and fails by default so somebody installs the thing. A major
# that does not ship pg_restore_attribute_stats is not a defect to fix: 15 to 17
# genuinely lack it, the same way 15 lacks WITHOUT OVERLAPS. Using pgc_skip here
# turned every PG17 CI run red the moment this suite was registered, which is a
# red nobody can act on.
#
# So it reports SKIP and runs no checks, which pgc_summary turns into exit 2 and
# the matrix records as SKIP rather than as a pass (#447).
#
# The major is asserted first. An unreadable version must not be mistaken for an
# old one, or a broken environment would report SKIP and look supported.
if ! pgc_is_number "${PGC_MAJOR:-}"; then
	pgc_fail "could not read the server major, so the gate below cannot be trusted" \
		"got [${PGC_MAJOR:-<none>}]"
	pgc_summary
fi
if [ "$PGC_MAJOR" -lt 18 ]; then
	check_skip "pgcolumnar.analyze()" "SKIP  pgcolumnar.analyze() needs pg_restore_attribute_stats (PG18+); this server is $PGC_MAJOR" "needs pg_restore_attribute_stats, PG18+"
	pgc_summary
fi

# --- fixture ------------------------------------------------------------------
#
# k is NULL for exactly one row in ten. 500,000 rows against core's 30,000-row
# sample is what makes the sampled estimate inexact, which slice 1 depends on.

psql_run "DROP TABLE IF EXISTS af_c;
	CREATE TABLE af_c (k int, k7 int, skew int, cat int, pad1 text, pad2 text, pad3 text) USING pgcolumnar;
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
	FROM generate_series(1, $ROWS) g;" >/dev/null

# --- premise 1: the fixture really is one-in-ten NULL --------------------------
#
# Everything below compares against 0.1. If the fixture is not 10% NULL then a
# "PASS" means the function agreed with a number that was never true.

true_nullfrac="$(q "SELECT round(count(*) FILTER (WHERE k IS NULL)::numeric / count(*), 6)
	FROM af_c")"
check_num "premise: the fixture is exactly one-in-ten NULL" "$true_nullfrac" "0.100000"

# --- premise 2: core CANNOT report the truth, by arithmetic rather than by luck -
#
# The premise this replaces required core's SAMPLED null_frac to differ from the
# truth, and failed the whole suite as "vacuous" when it did not. That is a real
# concern implemented with the wrong instrument, and it cost a matrix run (#487).
#
# The concern is right: if core's number and the exact number are the same here,
# an implementation that merely called core ANALYZE would pass, and the check
# would be vacuous in precisely the way a green suite hides.
#
# The instrument was wrong because it gated on a random draw. k is NULL for one
# row in ten, so core's sampled null_frac is (nulls drawn)/30000 and the truth is
# 0.1 = 3000/30000 -- a value core hits whenever its sample lands on its own mode.
# That is the single most likely outcome, about 0.8% of runs, one in 130. It came
# up on PG19 and reported a suite that measures correctly as broken.
#
# This is the same mistake #475 removed from the histogram slice below, where
# "core misses the outlier" is now printed rather than asserted, for the same
# reason: it is probabilistic and it is not a property of our code.
#
# So the premise is restated as something true by CONSTRUCTION. k7 is NULL for
# one row in seven, so the truth is 71428/500000 = 0.142856, and core's estimate
# is always (a whole number of sampled rows)/30000. There is no whole number k
# with k/30000 = 0.142856: it would need k = 4285.68. Core therefore cannot
# report this fraction whatever it draws, and the discrimination no longer
# depends on luck at all.
#
# Measured, three consecutive core runs on this fixture: 0.1442, 0.1425 and
# 0.13893333, which are 4326/30000, 4275/30000 and 4168/30000. Ours: 0.142856.
#
# Both facts the argument rests on are asserted below rather than assumed,
# because if either changes the premise silently becomes decorative.

psql_run "ANALYZE af_c;" >/dev/null
core_nullfrac="$(q "SELECT null_frac FROM pg_stats WHERE tablename = 'af_c' AND attname = 'k'")"
# Captured BEFORE our call, because our call overwrites them. Reading these
# afterwards would compare our own output against itself.
core_ndistinct_before="$(q "SELECT n_distinct FROM pg_stats WHERE tablename = 'af_c' AND attname = 'k'")"
core_correlation_before="$(q "SELECT correlation FROM pg_stats WHERE tablename = 'af_c' AND attname = 'k'")"

check "premise: core ANALYZE produced a null_frac at all, so the numbers below exist" \
	"$(pgc_is_number "$core_nullfrac" && echo yes || echo "no (got [${core_nullfrac:-<none>}])")" \
	"yes"

true_nullfrac7="$(q "SELECT round(count(*) FILTER (WHERE k7 IS NULL)::numeric / count(*), 6)
	FROM af_c")"
check_num "premise: the k7 fixture is exactly one row in seven NULL" \
	"$true_nullfrac7" "0.142856"

# Core's sample is 300 * the column's EFFECTIVE statistics target. Read it rather
# than writing 30000, so a server configured differently fails the arithmetic
# below honestly instead of having it quietly stop applying.
#
# Per column, not the global default. attstattarget overrides
# default_statistics_target, and reading the global one is a mistake this suite
# has already made once: slice 3b found the histogram honouring the global
# default so that ALTER TABLE ... SET STATISTICS did nothing. Written against the
# global default here, a per-column target large enough to make core read the
# whole table would leave this premise reporting a 30,000-row sample while core
# censused, and the contradiction would surface two checks later as a confusing
# failure instead of here as a clear one. Verified by forcing exactly that.
core_sample_rows=$(( $(q "SELECT 300 * coalesce(nullif(attstattarget, -1),
		current_setting('default_statistics_target')::int)
	FROM pg_attribute
	WHERE attrelid = 'af_c'::regclass AND attname = 'k7'") ))
check "premise: core samples fewer rows than the table holds, so its number is an estimate" \
	"$([ "$core_sample_rows" -lt "$ROWS" ] && echo yes \
		|| echo "no (sample $core_sample_rows covers all $ROWS rows)")" "yes"

check "premise: and no whole number of sampled rows gives that fraction, so core cannot report it" \
	"$(awk -v t="$true_nullfrac7" -v n="$core_sample_rows" \
		'BEGIN { p = t * n; print (p == int(p)) ? "no (" p " is a whole number of rows)" : "yes" }')" \
	"yes"

core_nullfrac7="$(q "SELECT null_frac FROM pg_stats WHERE tablename = 'af_c' AND attname = 'k7'")"

# Printed, not asserted. Whether core's draw for k happened to land on 0.1 is a
# fact about a random sample, not about this extension, and gating on it is what
# #487 was.
echo "-- core sampled null_frac: k = $core_nullfrac (truth $true_nullfrac), k7 = $core_nullfrac7 (truth $true_nullfrac7)"

# --- check 1: pgcolumnar.analyze() gives the EXACT null_frac -------------------
#
# Exact because the column is read rather than sampled, which is the thing a
# sampled implementation cannot fake.
#
# This used to come from the zone maps, which was cheaper -- a metadata read
# rather than a scan -- and wrong after a DELETE (#485): those counts describe
# what was WRITTEN, and deleting a row does not rewrite them. It now comes from
# the same read as n_distinct, which costs nothing extra because that read
# happens either way, and which cannot disagree with the denominator the
# most-common frequencies are divided by. The delete case is checked below.

psql_run "SELECT pgcolumnar.analyze('af_c'::regclass, ARRAY['k']);" >/dev/null
ours_nullfrac="$(q "SELECT null_frac FROM pg_stats WHERE tablename = 'af_c' AND attname = 'k'")"

check_num "pgcolumnar.analyze() reports null_frac exactly, from reading the column" \
	"$ours_nullfrac" "0.1"

# The same claim on the column core cannot express. This is the one that carries
# the weight: 0.1 is a number a sampler reaches whenever it is lucky, and
# 0.142856 is not a number a 30,000-row sample can produce at all. A pass here
# cannot be a coincidence and cannot be core's own answer wearing our name.
psql_run "SELECT pgcolumnar.analyze('af_c'::regclass, ARRAY['k7']);" >/dev/null
ours_nullfrac7="$(q "SELECT null_frac FROM pg_stats WHERE tablename = 'af_c' AND attname = 'k7'")"

check_num "and exactly for a fraction core's sample cannot express" \
	"$ours_nullfrac7" "0.142856"

# The negative half, stated separately: core's own number for that column must
# NOT be the truth. If this ever passes by matching, the arithmetic premise above
# is wrong and everything resting on it needs rereading.
check "and core's own number for it is not the truth, as the arithmetic requires" \
	"$(awk -v c="$core_nullfrac7" -v t="$true_nullfrac7" \
		'BEGIN { print (c == t) ? "no (core reported the exact truth)" : "yes" }')" \
	"yes"

# --- check 2: n_distinct is exact, from reading one column --------------------
#
# This is the slice the whole issue rests on. null_frac above is metadata only;
# n_distinct requires actually reading the column, and the case for a function is
# that reading ONE column of a wide table is cheap where core's whole-table
# sample is not: 268 ms against 6,302 ms on the 3M x 20 fixture.
#
# Exactness is the observable that a sampled implementation cannot fake, which is
# why it is what gets asserted. Core's own convention is mirrored: an absolute
# count normally, a negated fraction once the distinct count passes 10% of the
# rows (analyze.c does exactly this, on the grounds that such a column's
# cardinality scales with the table rather than sitting at a fixed value).

true_ndistinct="$(q "SELECT count(DISTINCT k) FROM af_c")"
check_num "premise: the fixture has the cardinality this check compares against" \
	"$true_ndistinct" "45001"

# Under 10% of 500,000 rows, so core's rule keeps this a positive absolute count
# rather than a negated fraction. If ROWS is ever lowered past 450,010 this flips
# sign and the check below needs to expect the fraction instead.
check "premise: the fixture stays on the absolute-count side of core's 10% rule" \
	"$(awk -v d="$true_ndistinct" -v n="$ROWS" 'BEGIN { print (d > 0.1 * n) ? "no (fraction side)" : "yes" }')" \
	"yes"

# Printed rather than asserted, for the reason given at premise 2 (#487): whether
# core's sampled n_distinct happens to land on 45001 is a property of a random
# draw. It is far less likely here than the null_frac case was, because a sampled
# distinct estimate comes out of a formula rather than off a binomial's mode, but
# "much less likely" is still the wrong thing to gate a suite on.
#
# What makes the check below discriminating is not core being wrong. It is that
# the expected value comes from an independent SELECT count(DISTINCT k) over the
# table, which is not how the function computes it.
echo "-- core sampled n_distinct = ${core_ndistinct_before:-<none>}, truth = $true_ndistinct"

ours_ndistinct="$(q "SELECT n_distinct FROM pg_stats WHERE tablename = 'af_c' AND attname = 'k'")"
check_num "pgcolumnar.analyze() reports n_distinct exactly, from one column" \
	"$ours_ndistinct" "$true_ndistinct"

# --- check 3: it must not destroy the statistics it does not compute -----------
#
# pg_restore_attribute_stats is a RESTORE api: it reinstates a whole attribute's
# statistics from a dump, so kinds not named in the call could be cleared rather
# than left alone. An accelerator that produces an exact null_frac and n_distinct
# while discarding everything else makes plans worse, not better, and does it
# silently because nothing errors.
#
# This watched n_distinct in slice 1. Slice 2 computes n_distinct, so it now
# watches correlation -- a statistic core collects and we still do not. Keeping it
# pointed at something we write would make it assert nothing.

ours_correlation="$(q "SELECT correlation FROM pg_stats WHERE tablename = 'af_c' AND attname = 'k'")"
echo "-- correlation before=${core_correlation_before:-<none>} after=${ours_correlation:-<none>}"

check "pgcolumnar.analyze() leaves the statistics it does not compute in place" \
	"$(if pgc_is_number "$ours_correlation" && pgc_is_number "$core_correlation_before"; then
			echo yes
		else
			echo "no (correlation was [${core_correlation_before:-<null>}], is now [${ours_correlation:-<null>}])"
		fi)" \
	"yes"

# ---- slice 3: histogram_bounds, whose top end is exact ------------------------
#
# `k` cannot test this. It is uniform over 0..45000, so core's 30,000-row sample
# almost certainly hits both extremes and its bounds are already near-exact. A
# check asserting "ours is exact where core's is not" would pass or fail on the
# luck of the sample, which is three samples rather than three behaviours.
#
# `skew` is built so the extreme is genuinely rare: ONE row in 500,000 holds
# 1,000,000 and every other row is under 100. Core's sample misses it with
# near-certainty; a full read cannot. Four orders of magnitude is not a coin flip.
#
# It is also the case that matters. A range predicate above the sampled maximum
# is exactly where the planner's estimate collapses.

# Two fixture decisions, both forced by what core actually does.
#
# The ordinary values span 100,000 distinct rather than 100. With only 100
# distinct values core stores every one as a most-common-value and emits NO
# histogram at all, so the first version of this check compared against an empty
# array and failed on its own premise rather than on the behaviour.
#
# And core's sample for this column is cut to 10 buckets (about 3,000 rows).
# At the default target it samples 30,000 of 500,000 rows, which finds a
# one-in-500,000 outlier about 6% of the time -- a check that fails one run in
# sixteen for no defect. At 10 it is about 0.6%. That is small and it is not
# zero: any discrimination against a SAMPLE is probabilistic, and pretending
# otherwise would be the flaky-by-construction shape this fixture exists to
# avoid. The premise below states the condition rather than assuming it.
psql_run "ALTER TABLE af_c ALTER COLUMN skew SET STATISTICS 10;" >/dev/null

true_skew_max="$(q "SELECT max(skew) FROM af_c")"
check_num "premise: the outlier really is in the table" "$true_skew_max" "1000000"
check_num "premise: and it really is one row in $ROWS" \
	"$(q "SELECT count(*) FROM af_c WHERE skew = 1000000")" "1"

psql_run "ANALYZE af_c;" >/dev/null
core_hist_max="$(q "SELECT (histogram_bounds::text::int[])[array_length(histogram_bounds::text::int[], 1)]
	FROM pg_stats WHERE tablename = 'af_c' AND attname = 'skew'")"
echo "-- core sampled histogram max = ${core_hist_max:-<none>}, truth = $true_skew_max"

# Reported, deliberately NOT asserted.
#
# "Core misses the outlier" cannot be a gate. It is probabilistic by definition,
# and it also depends on how our own access method hands rows to the sampler, so
# a red here would mean "the sample was lucky" and never "the code is wrong".
# Measured both ways while writing this: core's histogram max came back 99,999 on
# one run and 1,000,000 on the next, on identical data. Gating on that would have
# been the flaky-by-construction shape this fixture was built to avoid.
#
# What IS asserted below is exactness, and it does not need core to be wrong: the
# expected value comes from an independent SELECT max(), not from the code path
# under test, so the check fails whenever our bounds are not the true ones.
if pgc_is_number "$core_hist_max" && [ "$core_hist_max" -lt 200000 ]; then
	echo "-- core missed the outlier this run, which is the case that motivates #414"
else
	echo "-- core happened to sample the outlier this run; exactness is asserted regardless"
fi

# Captured, not discarded. While writing this slice the function raised
#
#     ERROR:  record "att" has no field "atttypid"
#
# and the redirect swallowed it, so the call did nothing, pg_stats still held
# core's numbers, and the failure presented as "our maximum is wrong" rather than
# "our function did not run". The assertion caught it, but the diagnosis needed
# the error, so the error is now a check of its own.
skew_out="$(psql_run "SELECT pgcolumnar.analyze('af_c'::regclass, ARRAY['skew']);" 2>&1)"
check_num "pgcolumnar.analyze() ran without raising, so the statistics below are its own" \
	"$(grep -c 'ERROR' <<<"$skew_out")" "0"
ours_hist="$(q "SELECT histogram_bounds::text::int[]
	FROM pg_stats WHERE tablename = 'af_c' AND attname = 'skew'")"
ours_hist_max="$(q "SELECT (histogram_bounds::text::int[])[array_length(histogram_bounds::text::int[], 1)]
	FROM pg_stats WHERE tablename = 'af_c' AND attname = 'skew'")"
echo "-- ours histogram max = ${ours_hist_max:-<none>}"

check_num "pgcolumnar.analyze() puts the true maximum at the top of histogram_bounds" \
	"${ours_hist_max:-<none>}" "$true_skew_max"

# Both ends, not just the interesting one. A histogram whose top is right and
# whose bottom is invented is still wrong, and percentile_disc returning real
# column values is what makes both exact.
#
# The bottom is the smallest value that is NOT most-common, which is not the same
# as the column minimum and stopped being the same in slice 3b. `skew` is
# g % 100000, so 0 occurs five times, which makes it a most-common value and
# excludes it from the histogram (analyze.c:2744). This check read
# `SELECT min(skew)` while nothing was ever excluded and began failing with
# got [1] want [0] the moment the exclusion landed -- correctly, because 1 occurs
# four times rather than five and so misses the list that 0 makes.
#
# The expected value is derived from the written MCV list rather than hardcoded,
# so it stays right if the fixture or the bucket count moves.
check_num "and the smallest non-most-common value at the bottom" \
	"$(q "SELECT (histogram_bounds::text::int[])[1]
		FROM pg_stats WHERE tablename = 'af_c' AND attname = 'skew'")" \
	"$(q "WITH m AS (SELECT most_common_vals::text::int[] AS v FROM pg_stats
			WHERE tablename = 'af_c' AND attname = 'skew')
		SELECT min(a.skew) FROM af_c a, m WHERE a.skew <> ALL (m.v)")"

# The exclusion holds for skew too, not only for the column built to show it.
check_num "and no most-common value is inside skew's histogram either" \
	"$(q "SELECT count(*) FROM pg_stats s, unnest(s.histogram_bounds::text::int[]) b
		WHERE s.tablename = 'af_c' AND s.attname = 'skew'
		  AND b = ANY (s.most_common_vals::text::int[])")" "0"

# percentile_disc returns values the column HOLDS. percentile_cont would
# interpolate and invent ones it does not, which is wrong for a histogram of
# stored data and impossible for a non-numeric type.
check_num "every bound is a value the column actually holds" \
	"$(q "SELECT count(*) FROM unnest((SELECT histogram_bounds::text::int[]
			FROM pg_stats WHERE tablename = 'af_c' AND attname = 'skew')) b
		WHERE NOT EXISTS (SELECT 1 FROM af_c WHERE skew = b)")" "0"

# A histogram is an ordered ladder, not a pair of extremes. Asserting only the
# last element would pass for an array of two values, which is not a histogram
# and would ruin every estimate between the ends.
check "and it is an ordered ladder rather than two extremes" \
	"$(if pgc_is_number "$(q "SELECT array_length(histogram_bounds::text::int[], 1)
			FROM pg_stats WHERE tablename = 'af_c' AND attname = 'skew'")" &&
		 [ "$(q "SELECT array_length(histogram_bounds::text::int[], 1)
			FROM pg_stats WHERE tablename = 'af_c' AND attname = 'skew'")" -ge 5 ]; then
			echo yes
		else
			echo "no (length [$(q "SELECT array_length(histogram_bounds::text::int[], 1) FROM pg_stats WHERE tablename = 'af_c' AND attname = 'skew'")])"
		fi)" "yes"

check "and it is sorted ascending, which a histogram must be to be usable" \
	"$(q "SELECT CASE WHEN histogram_bounds::text::int[] =
			(SELECT array_agg(x ORDER BY x) FROM unnest(histogram_bounds::text::int[]) x)
		THEN 'yes' ELSE 'no' END
		FROM pg_stats WHERE tablename = 'af_c' AND attname = 'skew'")" "yes"

# ---- slice 3b: most_common_vals and most_common_freqs ------------------------
#
# The selection rule is core's, and it is not the sampled one. analyze_mcv_list()
# opens by refusing to filter at all when the whole table was read:
#
#     /*
#      * If the entire table was sampled, keep the whole list.  This also
#      * protects us against division by zero in the code below.
#      */
#     if (samplerows == totalrows || totalrows <= 1.0)
#         return num_mcv;                             -- analyze.c:2995
#
# The machinery that guard skips -- a continuity-corrected Wald interval over a
# hypergeometric variance -- exists to decide whether a SAMPLE frequency is
# trustworthy enough to store. We read the entire column, so that question does
# not arise and core's own answer is "keep them". What is left is mechanical:
#
#   only values with count > 1 are eligible          analyze.c:2549
#   top default_statistics_target by count            analyze.c:2552-2564
#   frequency is count / total rows INCLUDING nulls   analyze.c:2720
#
# `cat` is built so that the third of those is observable, because it is the one
# that fails silently. Three values repeat and nothing else does:
#
#   NULL   50,000   one row in ten
#   7      90,000   freq 0.18   -- 0.2   if divided by the non-null count
#   42     54,000   freq 0.108  -- 0.12  if divided by the non-null count
#   99     27,000   freq 0.054  -- 0.06  if divided by the non-null count
#   tail  279,000 distinct values, each appearing exactly once
#
# Dividing by the 450,000 non-null rows rather than the 500,000 total inflates
# every frequency by 1/(1-null_frac) and produces 0.2/0.12/0.06: three numbers
# that are individually plausible, sum to less than one, and are wrong. No error
# is raised on that path, so the fixture has to be the thing that catches it.
# That is why the null fraction is not zero here.
#
# Exactly three values repeat, so the list is fully determined rather than a
# top-N cut of a longer one, and the check can name it outright.

cat_total="$(q "SELECT count(*) FROM af_c")"
check_num "premise: the MCV fixture has the row count the frequencies divide by" \
	"$cat_total" "$ROWS"

check_num "premise: value 7 appears exactly 90,000 times" \
	"$(q "SELECT count(*) FROM af_c WHERE cat = 7")" "90000"
check_num "premise: value 42 appears exactly 54,000 times" \
	"$(q "SELECT count(*) FROM af_c WHERE cat = 42")" "54000"
check_num "premise: value 99 appears exactly 27,000 times" \
	"$(q "SELECT count(*) FROM af_c WHERE cat = 99")" "27000"

# The rule at analyze.c:2549 is "count > 1", so this premise is what makes the
# expected list exactly three long. If the tail ever stopped being unique, the
# list would fill to default_statistics_target with tied values and the check
# below would fail for a reason that is not a defect.
check_num "premise: and nothing else in the column repeats, so the list is exactly three" \
	"$(q "SELECT count(*) FROM (SELECT cat FROM af_c WHERE cat IS NOT NULL
		GROUP BY cat HAVING count(*) > 1) t")" "3"

# Core's sampled frequencies, captured before our call overwrites them.
psql_run "ANALYZE af_c;" >/dev/null
core_mcv_before="$(q "SELECT most_common_vals::text FROM pg_stats
	WHERE tablename = 'af_c' AND attname = 'cat'")"
core_freq_7="$(q "SELECT (most_common_freqs)[array_position(most_common_vals::text::int[], 7)]
	FROM pg_stats WHERE tablename = 'af_c' AND attname = 'cat'")"
echo "-- core sampled MCVs = ${core_mcv_before:-<none>}, its freq for 7 = ${core_freq_7:-<none>}"

# Reported, not gated. Core's sample finding 7 at exactly 0.18 is unlikely but it
# is a sample, and a check that depends on core being unlucky is the shape slice 3
# already had to retract. Exactness below is asserted against independent counts,
# so it does not need core to be wrong.
if pgc_is_number "$core_freq_7" && [ "$core_freq_7" != "0.18" ]; then
	echo "-- core's sampled frequency is inexact, which is the gap this slice closes"
fi

# The statistics are CLEARED before our call, and this is not tidiness.
#
# Written without it, every check below read core's leftover MCV list and none of
# them could tell "our function wrote this" from "core wrote it and our function
# left it alone". Two passed that way on the first run: most_common_vals matched
# because core had already put {7,42,99} there, and 99's frequency matched because
# core's sample happened to round to 0.054 at six places. A function that writes
# no MCVs at all scored three of four.
#
# Clearing first makes attribution structural rather than lucky: after this call
# the column has no MCV list, so anything the checks below find is ours.
psql_run "SELECT pg_catalog.pg_clear_attribute_stats('public', 'af_c', 'cat', false);" >/dev/null
# A scalar subquery, because clearing removes the whole pg_statistic row rather
# than nulling a column: read as `SELECT ... FROM pg_stats WHERE ...` this returns
# NO ROWS, q() yields the empty string, and coalesce never runs. The premise then
# fails for its own reason rather than reporting the state it was asked about.
check "premise: the MCV list really is gone before we write, so what follows is ours" \
	"$(q "SELECT coalesce((SELECT most_common_vals::text FROM pg_stats
		WHERE tablename = 'af_c' AND attname = 'cat'), '<cleared>')")" \
	"<cleared>"

mcv_out="$(psql_run "SELECT pgcolumnar.analyze('af_c'::regclass, ARRAY['cat']);" 2>&1)"
check_num "pgcolumnar.analyze() ran without raising for the MCV column" \
	"$(grep -c 'ERROR' <<<"$mcv_out")" "0"

# A WARNING here is the silent-wrong-write this slice was told to guard against.
# pg_restore_attribute_stats takes VARIADIC "any": most_common_vals must be text
# and most_common_freqs must be real[] (attribute_stats.c:70-71). A float8[] is
# not an error, it is a WARNING and a dropped argument, and the call then reports
# success having stored nothing.
check_num "and without a WARNING, which is how a mistyped argument is dropped" \
	"$(grep -c 'WARNING' <<<"$mcv_out")" "0"

check "pgcolumnar.analyze() writes the three repeated values as most_common_vals" \
	"$(q "SELECT most_common_vals::text FROM pg_stats
		WHERE tablename = 'af_c' AND attname = 'cat'")" \
	"{7,42,99}"

# Each frequency against its own independently counted truth, not against a
# recomputation of what the function did.
check_num "and 7's frequency exactly, over total rows rather than non-null rows" \
	"$(q "SELECT round((most_common_freqs)[1]::numeric, 6) FROM pg_stats
		WHERE tablename = 'af_c' AND attname = 'cat'")" \
	"$(q "SELECT round(90000::numeric / $ROWS, 6)")"

check_num "and 42's" \
	"$(q "SELECT round((most_common_freqs)[2]::numeric, 6) FROM pg_stats
		WHERE tablename = 'af_c' AND attname = 'cat'")" \
	"$(q "SELECT round(54000::numeric / $ROWS, 6)")"

check_num "and 99's" \
	"$(q "SELECT round((most_common_freqs)[3]::numeric, 6) FROM pg_stats
		WHERE tablename = 'af_c' AND attname = 'cat'")" \
	"$(q "SELECT round(27000::numeric / $ROWS, 6)")"

# ---- the exclusion, which is why 3b could not be a line added to slice 3 ------
#
# Core builds the histogram from the values left AFTER the most-common ones are
# removed (analyze.c:2744 num_hist = ndistinct - num_mcv, and the collapse loop at
# :2768-2799). Writing both lists without that exclusion counts those values
# twice in selectivity: eqsel finds the value in the MCV list and takes its
# frequency, and the range estimators count it again inside whichever bucket
# holds it. Nothing errors; the estimates are just wrong, and wrong in the
# direction that says a heavily-repeated value is more common than it is.
#
# This is the check that has to fail before the exclusion exists. `cat`'s three
# most-common values are 7, 42 and 99 -- the three SMALLEST values in the column,
# so an unexcluded histogram puts 7 at the bottom bound and the check below finds
# it immediately.

# Asserted first, because the exclusion check is vacuously true when there is no
# histogram at all. "No MCV appears in histogram_bounds" passes trivially against
# NULL, so a change that silently stopped emitting histograms would read as a fix.
check "premise: a histogram exists for cat, so the exclusion below is not vacuous" \
	"$(q "SELECT CASE WHEN coalesce(array_length(histogram_bounds::text::int[], 1), 0) >= 2
			THEN 'yes' ELSE 'no' END
		FROM pg_stats WHERE tablename = 'af_c' AND attname = 'cat'")" "yes"

check_num "no most-common value appears in histogram_bounds, which would double-count it" \
	"$(q "SELECT count(*) FROM pg_stats s, unnest(s.histogram_bounds::text::int[]) b
		WHERE s.tablename = 'af_c' AND s.attname = 'cat'
		  AND b = ANY (s.most_common_vals::text::int[])")" "0"

# The same fact from the other side, and the one that shows the histogram is over
# the remaining population rather than merely filtered at the ends: the lowest
# bound must be the smallest value that is NOT most-common, not the column's
# minimum. Truth comes from an independent query, not from the function.
check_num "so the bottom bound is the smallest non-most-common value, not the column minimum" \
	"$(q "SELECT (histogram_bounds::text::int[])[1] FROM pg_stats
		WHERE tablename = 'af_c' AND attname = 'cat'")" \
	"$(q "SELECT min(cat) FROM af_c WHERE cat NOT IN (7, 42, 99)")"

# ---- the per-column statistics target, which core reads and we did not --------
#
# Core sizes both lists from the COLUMN's attstattarget, not from the global
# default_statistics_target:
#
#     attstattarget = isnull ? -1 : DatumGetInt16(dat);   -- analyze.c:1065
#     if (attstattarget == 0) return NULL;                -- :1070, skip entirely
#     if (stats->attstattarget < 0)                       -- :1897
#         stats->attstattarget = default_statistics_target;
#
# So NULL means "use the default", a positive value overrides it, and zero means
# do not collect statistics for this column at all. This function read the global
# setting for every column, which silently ignored ALTER TABLE ... SET STATISTICS.
# This suite has been setting it on `skew` since slice 3 while asserting nothing
# about it, so the divergence was already present here and invisible.
#
# skew is at SET STATISTICS 10, set above for core's benefit. Ten buckets means
# eleven bounds: percentile_disc is asked for target+1 fractions, which is the
# shape core caps at num_bins+1 (analyze.c:2746).

check_num "premise: skew really is at a non-default statistics target" \
	"$(q "SELECT attstattarget FROM pg_attribute
		WHERE attrelid = 'af_c'::regclass AND attname = 'skew'")" "10"

check_num "premise: and the global default differs from it, so the two are distinguishable" \
	"$(q "SHOW default_statistics_target")" "100"

# Cleared and re-run, which this check needs and did not originally have. The MCV
# section above calls plain ANALYZE to capture core's sampled list for `cat`, and
# that analyses EVERY column of af_c, skew included. Reading skew's histogram
# after it therefore reads CORE's -- and core honours attstattarget, so the check
# passed at eleven bounds while the function under test was still producing a
# hundred and one. Measured directly on an isolated table to find it.
psql_run "SELECT pg_catalog.pg_clear_attribute_stats('public', 'af_c', 'skew', false);" >/dev/null
psql_run "SELECT pgcolumnar.analyze('af_c'::regclass, ARRAY['skew']);" >/dev/null

check_num "the histogram honours the column's statistics target, not the global default" \
	"$(q "SELECT array_length(histogram_bounds::text::int[], 1) FROM pg_stats
		WHERE tablename = 'af_c' AND attname = 'skew'")" "11"

# The most-common list is sized by the same target, and by the same rule
# (analyze.c:2552 tracks at most attstattarget entries).
check "and so does the most-common list, which is capped by the same target" \
	"$(if [ "$(q "SELECT array_length(most_common_vals::text::int[], 1) FROM pg_stats
			WHERE tablename = 'af_c' AND attname = 'skew'")" -le 10 ] 2>/dev/null; then echo yes
		else echo "no (length [$(q "SELECT array_length(most_common_vals::text::int[], 1) FROM pg_stats WHERE tablename = 'af_c' AND attname = 'skew'")])"; fi)" \
	"yes"

# Zero is not "a small target", it is "do not collect". A column set to zero that
# comes back with statistics has had the DBA's instruction overridden, and the
# planner is then using numbers somebody deliberately turned off.
psql_run "ALTER TABLE af_c ALTER COLUMN pad1 SET STATISTICS 0;" >/dev/null
psql_run "SELECT pg_catalog.pg_clear_attribute_stats('public', 'af_c', 'pad1', false);" >/dev/null

pad_out="$(psql_run "SELECT pgcolumnar.analyze('af_c'::regclass, ARRAY['pad1']);" 2>&1)"
check_num "pgcolumnar.analyze() ran for the zero-target column without raising" \
	"$(grep -c 'ERROR' <<<"$pad_out")" "0"

check "a column at SET STATISTICS 0 is left alone, because that is what zero means" \
	"$(q "SELECT coalesce((SELECT 'wrote-' || attname FROM pg_stats
		WHERE tablename = 'af_c' AND attname = 'pad1'), '<nothing>')")" \
	"<nothing>"

# --- histogram bounds are POSITIONS, not quantiles (#414 follow-on) -----------
#
# core's compute_scalar_stats places bound i at
#
#     values[floor(i * (nvals - 1) / (num_hist - 1))]
#
# among the rows left after the most-common values are removed. percentile_disc
# resolves a fraction p to index ceil(p * nv) - 1, which is a different index and
# therefore a different VALUE whenever the shift crosses a value boundary.
#
# The 500,000-row fixtures above cannot show the difference: with many rows per
# distinct value a one-row shift lands on the same value, so both algorithms
# agree and the check would pass either way. A fixture that cannot distinguish
# the two implementations cannot test them. Eleven distinct rows at a statistics
# target of 3 can:
#
#     nv = 11, nhist = 4, so the divisor is 3
#     stride          i=2 -> floor(2*10/3) = 6 -> the 7th value = 7
#     percentile_disc i=2 -> ceil(2*11/3)-1 = 7 -> the 8th value = 8
#
# The expectation is NOT taken from the implementation. It is computed by the
# oracle below, straight from core's formula over row_number(), and it is also
# hand-workable: the values are 1..11 each appearing once, so position p holds
# value p+1 and the bounds are {1,4,7,11}. Two independent derivations that agree
# with each other before either judges the code.
psql_run "DROP TABLE IF EXISTS af_h11;
	CREATE TABLE af_h11 (v int) USING pgcolumnar;
	INSERT INTO af_h11 SELECT generate_series(1, 11);
	ALTER TABLE af_h11 ALTER COLUMN v SET STATISTICS 3;" >/dev/null

check_num "premise: the stride fixture has no repeated value, so no MCV is excluded" \
	"$(q "SELECT count(*) FROM (SELECT v FROM af_h11 GROUP BY v HAVING count(*) > 1) t")" "0"

h11_oracle="$(q "WITH nonmcv AS (
			SELECT v, row_number() OVER (ORDER BY v) - 1 AS pos
			  FROM af_h11 WHERE v IS NOT NULL),
		     n AS (SELECT count(*)::bigint AS nv FROM nonmcv),
		     p AS (SELECT floor(i::numeric * (n.nv - 1) / (4 - 1))::bigint AS pos
			     FROM generate_series(0, 3) i, n)
		SELECT (SELECT array_agg(v ORDER BY pos) FROM nonmcv
			 WHERE pos IN (SELECT pos FROM p))::text")"

# The oracle and the hand-worked figure are derived separately. If they ever
# disagree the oracle is wrong, and nothing below it means anything.
check "premise: the independent oracle agrees with the hand-worked bounds" \
	"$h11_oracle" "{1,4,7,11}"

psql_run "SELECT pgcolumnar.analyze('af_h11'::regclass, ARRAY['v']);" >/dev/null

check "premise: a histogram was written, so the comparison below is not vacuous" \
	"$(q "SELECT CASE WHEN histogram_bounds IS NULL THEN 'none' ELSE 'present' END
		FROM pg_stats WHERE tablename = 'af_h11' AND attname = 'v'")" "present"

check "histogram_bounds are core's positional stride, not evenly spaced quantiles" \
	"$(q "SELECT histogram_bounds::text FROM pg_stats
		WHERE tablename = 'af_h11' AND attname = 'v'")" "$h11_oracle"

# --- null_frac describes the rows the table HOLDS (#485) ----------------------
#
# It used to come from the zone maps, which count what was written. A DELETE
# marks rows dead without rewriting those counts, so the fraction stayed
# normalised against a population the table no longer had -- and VACUUM did not
# clear it.
#
# The consequence is worse than the size of the error. null_frac came from the
# zone maps while the most-common frequencies came from count(*), so one
# pg_stats row carried two statistics normalised against different populations
# and null_frac + sum(mcv_freqs) + rest = 1 stopped holding. eqsel subtracts both
# to price everything else, so the residual it computes went wrong by the
# difference.
#
# 1,200 rows: 120 null, 300 holding 7, 300 holding 9, the rest unique. Deleting
# the rows holding 9 leaves 900 rows and keeps 7 in the most-common list, so both
# denominators are observable in the same written row.
psql_run "DROP TABLE IF EXISTS af_del;
	CREATE TABLE af_del (v int) USING pgcolumnar;
	INSERT INTO af_del
	SELECT CASE WHEN i % 10 = 0 THEN NULL
		    WHEN i %  4 = 0 THEN 7
		    WHEN i %  4 = 1 THEN 9
		    ELSE 100000 + i END
	  FROM generate_series(1, 1200) i;
	DELETE FROM af_del WHERE v = 9;" >/dev/null

check_num "premise: the delete left fewer live rows than the zone maps describe" \
	"$(q "SELECT CASE WHEN (SELECT count(*) FROM af_del)
			 < (SELECT sum(z.value_count + z.null_count)
			      FROM pgcolumnar.zone_map z
			      JOIN pgcolumnar.storage s ON s.storage_id = z.storage_id
			     WHERE s.relation_oid = 'af_del'::regclass
			       AND z.column_index = 0 AND z.vector_index = -1)
		     THEN 1 ELSE 0 END")" "1"

psql_run "SELECT pgcolumnar.analyze('af_del'::regclass, ARRAY['v']);" >/dev/null

# 120 nulls in 900 live rows. Against the zone maps this read 0.1.
check "null_frac counts live rows, not rows a DELETE left behind" \
	"$(q "SELECT round(null_frac::numeric, 6)::text FROM pg_stats
		WHERE tablename = 'af_del' AND attname = 'v'")" "0.133333"

check_num "premise: 7 survived the delete and is still a most-common value" \
	"$(q "SELECT CASE WHEN most_common_vals::text::int[] @> ARRAY[7] THEN 1 ELSE 0 END
		FROM pg_stats WHERE tablename = 'af_del' AND attname = 'v'")" "1"

# The point of the pair: both statistics must imply the same table. Divide each
# by the count it describes and the row count that falls out must be the real
# one, from both directions.
check "null_frac and the most-common frequencies agree on how many rows there are" \
	"$(q "SELECT CASE WHEN
		 round((SELECT count(*) FILTER (WHERE v IS NULL) FROM af_del)::numeric
		       / nullif(null_frac::numeric, 0)) = (SELECT count(*) FROM af_del)
	     AND round((SELECT count(*) FILTER (WHERE v = 7) FROM af_del)::numeric
		       / nullif((most_common_freqs)[1]::numeric, 0)) = (SELECT count(*) FROM af_del)
		THEN 'yes' ELSE 'no ('
		     || round((SELECT count(*) FILTER (WHERE v IS NULL) FROM af_del)::numeric
			      / nullif(null_frac::numeric, 0))::text || ' vs '
		     || round((SELECT count(*) FILTER (WHERE v = 7) FROM af_del)::numeric
			      / nullif((most_common_freqs)[1]::numeric, 0))::text || ')' END
		FROM pg_stats WHERE tablename = 'af_del' AND attname = 'v'")" "yes"


# ---- the documented statistics must be the statistics written (#414) --------
#
# pgcolumnar.analyze() went five slices without an entry in
# docs/sql-reference.md. Now that it has one, the list in it is a claim about
# this function, and a claim in prose is the kind that rots quietly: nothing
# builds it, no suite reads it, and it is wrong only for the reader.
#
# So the doc's table is parsed and compared against what the function actually
# populates. Source text on one side, live catalog on the other.
_doc="$PGC_SRCDIR/docs/sql-reference.md"
# awk, not sed: a sed range ending at /^## / runs past the next ### heading and
# swallows the tables of pgcolumnar.stats and sort_status, which made the first
# version of this check compare against seventeen names from three sections.
_docstats="$(awk '/^### pgcolumnar\.analyze\(/{f=1;next} /^### /{f=0} f' "$_doc" \
             | grep -oE '^\| `[a-z_]+`' | tr -d '|` ' | sort -u | tr '\n' ' ' | sed 's/ $//')"
check "premise: the doc lists the statistics it claims to write" \
	"$([ -n "$_docstats" ] && echo yes || echo no)" "yes"

psql_run "CREATE TABLE af_doc (k int, t text) USING pgcolumnar;"
psql_run "INSERT INTO af_doc SELECT g % 500, 'v' || (g % 90) FROM generate_series(1, 20000) g;"
psql_run "SELECT pgcolumnar.analyze('af_doc', ARRAY['k']);"
_written="$(q "SELECT string_agg(x, ' ' ORDER BY x) FROM (
                 SELECT 'histogram_bounds' AS x WHERE (SELECT histogram_bounds IS NOT NULL FROM pg_stats WHERE tablename='af_doc' AND attname='k')
                 UNION ALL SELECT 'most_common_freqs' WHERE (SELECT most_common_freqs IS NOT NULL FROM pg_stats WHERE tablename='af_doc' AND attname='k')
                 UNION ALL SELECT 'most_common_vals'  WHERE (SELECT most_common_vals  IS NOT NULL FROM pg_stats WHERE tablename='af_doc' AND attname='k')
                 UNION ALL SELECT 'n_distinct'        WHERE (SELECT n_distinct        IS NOT NULL FROM pg_stats WHERE tablename='af_doc' AND attname='k')
                 UNION ALL SELECT 'null_frac'         WHERE (SELECT null_frac         IS NOT NULL FROM pg_stats WHERE tablename='af_doc' AND attname='k')) s")"
check "every statistic the doc lists is one the function writes" \
	"$_written" "$_docstats"

# And the negative the doc states in prose: correlation is NOT written. Without
# this the check above passes if the doc silently drops a column it should list.
check "and correlation is absent, as the doc says" \
	"$(q "SELECT correlation IS NULL FROM pg_stats WHERE tablename='af_doc' AND attname='k'")" "t"

# The doc also claims reltuples stays -1 and that plans are unaffected. Both are
# surprising enough that a reader will check them, so the suite checks them too.
check "reltuples is untouched, as the doc says" \
	"$(q "SELECT reltuples::bigint FROM pg_class WHERE relname='af_doc'")" "-1"


# --- a covering projection must not decide which columns get statistics (#1276)
#
# analyze() resolved its storage id with
#
#     SELECT s.storage_id INTO sid FROM pgcolumnar.storage s WHERE s.relation_oid = rel;
#
# relation_oid is NOT unique in pgcolumnar.storage -- storage_pkey, on
# storage_id, is the only unique index -- because a covering projection gets its
# OWN row carrying the BASE table's relation_oid. SELECT ... INTO takes the
# first row in heap order without complaining about the second. (#1210 is the
# same root cause in C; this is the plpgsql reader, and #1210's fix does not
# touch it.)
#
# THE HARM IS MISSING STATISTICS, NOT WRONG ONES. The values come from reading
# the column; sid is only a GATE, at alpha5:1532:
#
#     PERFORM 1 FROM pgcolumnar.zone_map z
#       WHERE z.storage_id = sid AND z.column_index = att.attnum - 1 ...
#     CONTINUE WHEN NOT FOUND;
#
# so a projection NARROWER than its base fails that gate for every column index
# it does not carry, and CONTINUE skips those columns in silence with a
# successful return.
#
# A TWO-COLUMN BASE CANNOT SEE THIS. The projection carries zone maps for both
# indexes, the gate passes either way, and the statistics come out identical to
# the control -- a fixture that returns a stable, plausible, meaningless number.
# Five columns covered by one is the shape that separates them.
q "DROP TABLE IF EXISTS af_proj;
   SET pgcolumnar.stripe_row_limit = 150000;
   CREATE TABLE af_proj (a int, b text, c int, d int, e int) USING pgcolumnar;
   INSERT INTO af_proj SELECT g, 'x'||(g%50), g%7, g%11, g%13
     FROM generate_series(1,20000) g;" >/dev/null
q "SET pgcolumnar.stripe_row_limit = 3000;
   SELECT pgcolumnar.add_projection('af_proj', 'cov_b', '{b}', '{b}');" >/dev/null
q "ANALYZE af_proj;" >/dev/null

proj_base_sid="$(q "SELECT pgcolumnar.get_storage_id('af_proj'::regclass);")"
proj_other_sid="$(q "SELECT storage_id FROM pgcolumnar.storage
	WHERE relation_oid = 'af_proj'::regclass::oid AND storage_id <> $proj_base_sid;")"
proj_rows="$(q "SELECT count(*) FROM pgcolumnar.storage
	WHERE relation_oid = 'af_proj'::regclass::oid;")"
base_list="$(q "SELECT string_agg(DISTINCT column_index::text, ',' ORDER BY column_index::text)
	FROM pgcolumnar.zone_map WHERE storage_id = $proj_base_sid AND vector_index = -1;")"
cov_list="$(q "SELECT string_agg(DISTINCT column_index::text, ',' ORDER BY column_index::text)
	FROM pgcolumnar.zone_map WHERE storage_id = $proj_other_sid AND vector_index = -1;")"
echo "-- af_proj owns $proj_rows storage rows; zone_map column indexes: base=[$base_list] projection=[$cov_list]"

# WITHOUT A SECOND ROW THERE IS NO AMBIGUITY, and without the projection being
# NARROWER the gate passes either way. Both are read from the catalog.
check_num "premise: a covering projection gave the table a second storage row" \
	"$proj_rows" "2"
# THE LIST, NOT A COUNT, AND NO NUMBER IN THE NAME. A number in a check NAME is
# a claim nothing re-derives: change the fixture to four columns or six and the
# arm keeps passing under a name that now says something false -- in the ledger,
# in TESTS.md and in every RESULT record it has ever carried. Renaming a check
# later costs a ledger row, so the count goes in the VALUE, where a fixture
# change reddens it. Raised by @jdatcmd.
#
# The list is also a better diagnostic than its length: `got [0,1] want
# [0,1,2,3,4]` says WHICH indexes went missing.
check_text "premise: the base storage covers every column index" \
	"$base_list" "0,1,2,3,4"
check_text "premise: the projection's storage covers fewer of them" \
	"$cov_list" "0,1"

# AND THE FIXTURE'S SHAPE IS PINNED WHERE IT CAN BE RE-DERIVED. If the table
# gains or loses a column the arms above are about a different question, and
# this reddens with the new count rather than letting them pass quietly.
check_num "premise: the fixture's column count has not changed" \
	"$(q "SELECT count(*) FROM pg_attribute
		WHERE attrelid = 'af_proj'::regclass AND attnum > 0 AND NOT attisdropped;")" "5"

proj_stats() {	# -> how many of af_proj's columns hold statistics
	q "DELETE FROM pg_statistic WHERE starelid = 'af_proj'::regclass;" >/dev/null
	q "SELECT pgcolumnar.analyze('af_proj'::regclass);" >/dev/null
	q "SELECT count(*) FROM pg_stats WHERE tablename = 'af_proj';"
}
proj_first() {
	q "SELECT CASE WHEN storage_id = $proj_base_sid THEN 'base' ELSE 'proj' END
		FROM pgcolumnar.storage WHERE relation_oid = 'af_proj'::regclass::oid
		ORDER BY ctid LIMIT 1;"
}

# PUT THE BASE ROW FIRST by writing the OTHER one, rather than assuming where
# the fixture left it. Assuming a heap position is what this defect is about.
q "UPDATE pgcolumnar.storage SET row_group_limit = row_group_limit
	WHERE storage_id = $proj_other_sid;" >/dev/null
first_base="$(proj_first)"
cols_base="$(proj_stats)"

# THE MUTATION ALTERS NO VALUE: it writes the base row back unchanged, which
# moves it later in the heap exactly as any real write to it would.
q "UPDATE pgcolumnar.storage SET row_group_limit = row_group_limit
	WHERE storage_id = $proj_base_sid;" >/dev/null
first_flip="$(proj_first)"
cols_flip="$(proj_stats)"
echo "-- first row in the heap: $first_base -> $first_flip;  columns with statistics: $cols_base -> $cols_flip"

# THE FLIP MUST HAVE HAPPENED, or both readings come from one heap order and the
# arm below passes without having asked anything.
check_text "premise: the base storage row was first" "$first_base" "base"
check_text "premise: the update put the projection's row first" "$first_flip" "proj"
check_num "premise: every column has statistics when the base row is resolved" \
	"$cols_base" "5"

check_num "pgcolumnar.analyze() covers every column whichever storage row the heap returns first" \
	"$cols_flip" "5"

# AND THE BRANCH THAT REFUSES A RELATION WITH NO STORAGE MUST STAY ALIVE. A
# never-written columnar table has a READABLE metapage and NO storage row, so
# resolving through the metapage still leaves sid NULL. Without this the fix
# could make that branch dead code and nothing would say so.
q "DROP TABLE IF EXISTS af_empty;
   CREATE TABLE af_empty (a int, b text) USING pgcolumnar;" >/dev/null
empty_rows="$(q "SELECT count(*) FROM pgcolumnar.storage
	WHERE storage_id = pgcolumnar.get_storage_id('af_empty'::regclass);")"
# CAPTURED AS A VALUE, NOT AS A NOTICE. `q` sends stderr to /dev/null, so a
# RAISE NOTICE from a DO block returns the empty string -- and an empty side is
# indistinguishable from a refusal that never happened. A helper that RETURNS
# the sqlstate puts the answer in the result set where `q` can see it.
q "CREATE OR REPLACE FUNCTION af_try(r regclass) RETURNS text LANGUAGE plpgsql AS \$fn\$
	BEGIN
		PERFORM pgcolumnar.analyze(r);
		RETURN 'succeeded';
	EXCEPTION WHEN OTHERS THEN
		RETURN 'refused ' || SQLSTATE;
	END
	\$fn\$;" >/dev/null
empty_state="$(q "SELECT af_try('af_empty'::regclass);")"
echo "-- a never-written columnar table: storage rows by metapage id=$empty_rows, analyze() $empty_state"

check_num "premise: a never-written columnar table has no storage row to find" \
	"$empty_rows" "0"

# THE HELPER MUST BE ABLE TO REPORT A SUCCESS TOO, or "refused" is the only
# thing it can ever say and the arm below cannot fail.
check_text "premise: the capture helper reports a success when there is one" \
	"$(q "SELECT af_try('af_proj'::regclass);")" "succeeded"
check_text "pgcolumnar.analyze() still refuses a relation that has never been written" \
	"$empty_state" "refused P0001"

# A MATVIEW CREATED WITH DATA, AND THIS ARM HAS BEEN SUPERSEDED ONCE ALREADY.
#
# #1276 added it to record a behaviour change: a WITH DATA matview had an
# ORPHANED relation_oid until its first REFRESH, so keyed on that column
# analyze() refused a matview holding rows, and through the metapage it
# resolved. Its premise asserted the orphan was still there -- and #1275 then
# removed the orphan, so the premise went red. That is the premise doing its
# job: it recorded a fact about the world and reddened when the world changed,
# rather than passing quietly under an arm that no longer meant anything.
#
# WHAT IS LEFT TO GUARD, AND WHAT IS NOT. With the orphan gone, both routes
# resolve, so this shape no longer distinguishes them and is NOT evidence for
# #1276's fix -- the projection arms above are. What it still holds is that
# analyze() reaches a freshly created matview at all, which was an outright
# error before #1276, and that the two routes agree about WHICH storage, which
# is #1275's guarantee cross-checked from a different file.
q "DROP MATERIALIZED VIEW IF EXISTS af_mv;
   CREATE MATERIALIZED VIEW af_mv USING pgcolumnar AS SELECT a, b, c FROM af_proj;" >/dev/null
mv_by_reloid="$(q "SELECT coalesce(min(storage_id)::text, 'none') FROM pgcolumnar.storage
	WHERE relation_oid = 'af_mv'::regclass::oid;")"
mv_by_meta="$(q "SELECT coalesce(min(storage_id)::text, 'none') FROM pgcolumnar.storage
	WHERE storage_id = pgcolumnar.get_storage_id('af_mv'::regclass);")"
mv_state="$(q "SELECT af_try('af_mv'::regclass);")"
echo "-- a WITH DATA matview: by relation_oid=$mv_by_reloid, by metapage=$mv_by_meta, analyze() $mv_state"

# THE IDS, NOT THEIR COUNTS. Two rows counting 1 each can still be two
# DIFFERENT storages, and that is the failure this arm would most want to see.
check_text "premise: the matview's relation_oid finds a storage row" \
	"$(if [ "$mv_by_reloid" = "none" ]; then echo none; else echo found; fi)" "found"
check_text "the two routes agree which storage a WITH DATA matview owns" \
	"$mv_by_meta" "$mv_by_reloid"

check_text "pgcolumnar.analyze() reaches a matview created WITH DATA" \
	"$mv_state" "succeeded"


pgc_summary
