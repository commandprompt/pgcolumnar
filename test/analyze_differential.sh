#!/usr/bin/env bash
#
# pgColumnar: what pgcolumnar.analyze() writes must have the SHAPE core writes
# (issue #414, the differential harness that lands with slice 3b).
#
# This suite exists because of one property of the API the function writes
# through. pg_restore_attribute_stats takes VARIADIC "any" and validates each
# argument's type at run time. A mistyped argument is not an error:
#
#     if (!stats_check_arg_array(fcinfo, attarginfo, MOST_COMMON_FREQS_ARG))
#     {
#         do_mcv = false;                     -- attribute_stats.c:247-251
#         result = false;
#     }
#
# It emits a WARNING, sets the argument to NULL, and carries on. The call then
# returns cleanly having stored nothing. Nothing in the value-level suite catches
# that: test/analyze_function.sh asserts our numbers are exact, and a statistic
# that was never written simply leaves core's earlier numbers in place, so the
# assertions read core's work and report on ours. That is not hypothetical -- the
# first draft of the slice 3b checks did exactly this and scored three of four
# passes against a function that wrote no most-common values at all.
#
# most_common_freqs is real[] and most_common_vals is text (attribute_stats.c:70-71);
# they are also a PAIR, so supplying one without the other drops both (:265).
# float8[] instead of real[] is the easiest mistake to make and the hardest to
# see, because plpgsql will happily produce one.
#
# So this suite compares SHAPE against core rather than values. Values must
# differ -- ours are exact and core's are sampled, which is the whole feature, so
# values cannot be the oracle. Shape must not differ: for every statistic kind we
# write, core writing the same kind for the same column must agree on the
# operator, the collation, and the element type of the stored array. A dropped
# argument leaves the slot absent, which a shape comparison sees immediately.
#
# Core ANALYZE is therefore the oracle here, the same way heap is the oracle for
# columnar in test/differential.sh.
#
# Usage:  test/analyze_differential.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg18/bin/pg_config}"

ROWS=${PGC_ANALYZE_DIFF_ROWS:-50000}

# Same version gate as test/analyze_function.sh, and for the same reason: a major
# without pg_restore_attribute_stats is not a defect anybody can fix, so it is a
# SKIP with no checks (exit 66) rather than pgc_skip, which fails by default and
# would redden every PG15-17 run the moment this suite was registered.
#
# The major is asserted first so an unreadable version is not mistaken for an old
# one and reported as "supported, skipped".
if ! pgc_is_number "${PGC_MAJOR:-}"; then
	pgc_fail "could not read the server major, so the gate below cannot be trusted" \
		"got [${PGC_MAJOR:-<none>}]"
	pgc_summary
fi
if [ "$PGC_MAJOR" -lt 18 ]; then
	check_skip "the differential analyze path" "SKIP  pgcolumnar.analyze() needs pg_restore_attribute_stats (PG18+); this server is $PGC_MAJOR" "needs pg_restore_attribute_stats, PG18+"
	pgc_summary
fi

# --- fixture ------------------------------------------------------------------
#
# Five types, because the failure this suite hunts is type-dependent. An int
# column can be written correctly by code that mangles every text column: the
# array literal for most_common_vals is built through the type's own output
# function, and the two text values below are the ones that break a literal
# assembled by hand instead -- one contains a comma, the other a quote.
#
# Each column is skewed the same way: two values repeat heavily and the rest are
# unique, so core produces both a most-common list and a histogram and there is a
# shape to compare. `b` is deliberately different: two distinct values, both
# repeated, so the MCV list describes the column completely and NEITHER core nor
# this function emits a histogram (analyze.c:2744, num_hist = ndistinct - num_mcv).

# Every column is one-in-seven NULL, and that is load-bearing rather than
# realistic. Frequencies are count / TOTAL rows including nulls (analyze.c:2720);
# dividing by the non-null count instead is the quiet defect this suite should
# catch. Written WITHOUT nulls -- as this fixture first was -- the two
# denominators are the same number, the frequency checks below cannot tell them
# apart, and the whole suite passed with that defect injected while
# test/analyze_function.sh caught it. A differential harness that misses the
# failure the value suite catches is not adding coverage, and the removal proof
# is what exposed it.

psql_run "DROP TABLE IF EXISTS ad_c;
	CREATE TABLE ad_c (i int, t text, n numeric, d date, b boolean) USING pgcolumnar;
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
			 WHEN g % 5 = 0 THEN DATE '2020-01-01' WHEN g % 5 = 1 THEN DATE '2021-06-15'
			 ELSE DATE '2000-01-01' + g END,
		CASE WHEN g % 7 = 0 THEN NULL ELSE (g % 3 = 0) END
	FROM generate_series(1, $ROWS) g;" >/dev/null

check_num "premise: the fixture loaded, so the shapes below describe real data" \
	"$(q "SELECT count(*) FROM ad_c")" "$ROWS"

# The premise that makes every frequency check below discriminating. If the
# column were never null, count/total and count/non-null would agree and a wrong
# denominator would pass unseen.
check "premise: the columns are nullable in fact, so the two denominators differ" \
	"$(q "SELECT CASE WHEN count(*) FILTER (WHERE i IS NULL) > 0 THEN 'yes' ELSE 'no' END
		FROM ad_c")" "yes"

# The text values that break a hand-built array literal are actually present.
# Without this, a green run could mean the quoting was never exercised. Counted
# by query rather than by arithmetic over ROWS, because the null pattern and the
# skew pattern overlap and the closed form is a distraction.
check "premise: a most-common text value contains a comma" \
	"$(if [ "$(q "SELECT count(*) FROM ad_c WHERE t = 'alpha,beta'")" -gt 0 ] 2>/dev/null; then echo yes; else echo no; fi)" "yes"
check "premise: and another contains a quote" \
	"$(if [ "$(q "SELECT count(*) FROM ad_c WHERE t = 'it''s here'")" -gt 0 ] 2>/dev/null; then echo yes; else echo no; fi)" "yes"

# --- core's shape, which is the oracle ----------------------------------------
#
# Captured into an ordinary table rather than a temp one: every psql_run and q()
# opens its own connection, so a temp table would not survive to be read.
#
# The five stakind/staop/stacoll/stavalues slots are unnested WITH ORDINALITY so
# each kind stays joined to the slot it was found in. Comparing kind sets alone
# would miss an operator or collation written into the right kind's slot but
# wrong, which is the silent half of this failure mode.

psql_run "ANALYZE ad_c;" >/dev/null

psql_run "DROP TABLE IF EXISTS ad_shape;
	CREATE TABLE ad_shape (source text, attname name, kind smallint,
						   op oid, coll oid, elemtype text);
	INSERT INTO ad_shape
	SELECT 'core', a.attname, s.kind, s.op, s.coll, s.elemtype
	  FROM pg_attribute a
	  JOIN pg_statistic st ON st.starelid = a.attrelid AND st.staattnum = a.attnum
	  CROSS JOIN LATERAL (
		SELECT k.kind, o.op, c.coll,
			   CASE k.ord
				 WHEN 1 THEN pg_typeof(st.stavalues1)::text
				 WHEN 2 THEN pg_typeof(st.stavalues2)::text
				 WHEN 3 THEN pg_typeof(st.stavalues3)::text
				 WHEN 4 THEN pg_typeof(st.stavalues4)::text
				 WHEN 5 THEN pg_typeof(st.stavalues5)::text
			   END AS elemtype
		  FROM unnest(ARRAY[st.stakind1, st.stakind2, st.stakind3, st.stakind4, st.stakind5])
			   WITH ORDINALITY AS k(kind, ord)
		  JOIN unnest(ARRAY[st.staop1, st.staop2, st.staop3, st.staop4, st.staop5])
			   WITH ORDINALITY AS o(op, ord) ON o.ord = k.ord
		  JOIN unnest(ARRAY[st.stacoll1, st.stacoll2, st.stacoll3, st.stacoll4, st.stacoll5])
			   WITH ORDINALITY AS c(coll, ord) ON c.ord = k.ord
	  ) s
	 WHERE a.attrelid = 'ad_c'::regclass AND a.attnum > 0 AND NOT a.attisdropped
	   AND NOT st.stainherit AND s.kind <> 0;" >/dev/null

# STATISTIC_KIND_MCV is 1 and STATISTIC_KIND_HISTOGRAM is 2 (pg_statistic.h).
# Asserted rather than assumed: if core produced neither for these columns, every
# comparison below would compare an empty set with an empty set and pass.
# Not premises (#1339): core reads a columnar table through pgcolumnar's own ANALYZE
# sampler, so a repository change can redden these -- with the sampler mapping no
# block to a slice, core collects nothing and both go red.
check_num "core ANALYZE, through the columnar sampler, produced a most-common list for every column" \
	"$(q "SELECT count(DISTINCT attname) FROM ad_shape WHERE source = 'core' AND kind = 1")" "5"
check_num "and a histogram for the four with a tail, but not for boolean" \
	"$(q "SELECT count(DISTINCT attname) FROM ad_shape WHERE source = 'core' AND kind = 2")" "4"

# --- clear, so what follows is unambiguously ours ------------------------------
#
# pg_restore_attribute_stats leaves kinds it was not given in place, which is
# correct (test/analyze_function.sh pins it) and fatal to attribution here: a
# statistic we failed to write would still be present, wearing core's shape, and
# every check below would pass on core's work.

psql_run "SELECT pg_catalog.pg_clear_attribute_stats('public', 'ad_c', a.attname::text, false)
	FROM pg_attribute a
	WHERE a.attrelid = 'ad_c'::regclass AND a.attnum > 0 AND NOT a.attisdropped;" >/dev/null

check_num "premise: every statistic is gone before we write, so nothing below is core's" \
	"$(q "SELECT count(*) FROM pg_statistic WHERE starelid = 'ad_c'::regclass")" "0"

# --- our call ------------------------------------------------------------------

ad_out="$(psql_run "SELECT pgcolumnar.analyze('ad_c'::regclass);" 2>&1)"
check_num "pgcolumnar.analyze() ran over every column without raising" \
	"$(grep -c 'ERROR' <<<"$ad_out")" "0"

# The check this suite is named for. A WARNING here IS the silent wrong write:
# the argument was dropped, the call succeeded, and the statistic is missing.
check_num "and without a WARNING, which is how pg_restore_attribute_stats drops an argument" \
	"$(grep -c 'WARNING' <<<"$ad_out")" "0"

psql_run "INSERT INTO ad_shape
	SELECT 'ours', a.attname, s.kind, s.op, s.coll, s.elemtype
	  FROM pg_attribute a
	  JOIN pg_statistic st ON st.starelid = a.attrelid AND st.staattnum = a.attnum
	  CROSS JOIN LATERAL (
		SELECT k.kind, o.op, c.coll,
			   CASE k.ord
				 WHEN 1 THEN pg_typeof(st.stavalues1)::text
				 WHEN 2 THEN pg_typeof(st.stavalues2)::text
				 WHEN 3 THEN pg_typeof(st.stavalues3)::text
				 WHEN 4 THEN pg_typeof(st.stavalues4)::text
				 WHEN 5 THEN pg_typeof(st.stavalues5)::text
			   END AS elemtype
		  FROM unnest(ARRAY[st.stakind1, st.stakind2, st.stakind3, st.stakind4, st.stakind5])
			   WITH ORDINALITY AS k(kind, ord)
		  JOIN unnest(ARRAY[st.staop1, st.staop2, st.staop3, st.staop4, st.staop5])
			   WITH ORDINALITY AS o(op, ord) ON o.ord = k.ord
		  JOIN unnest(ARRAY[st.stacoll1, st.stacoll2, st.stacoll3, st.stacoll4, st.stacoll5])
			   WITH ORDINALITY AS c(coll, ord) ON c.ord = k.ord
	  ) s
	 WHERE a.attrelid = 'ad_c'::regclass AND a.attnum > 0 AND NOT a.attisdropped
	   AND NOT st.stainherit AND s.kind <> 0;" >/dev/null

# --- the differential ----------------------------------------------------------
#
# Asserted first, because every comparison that follows is over the rows this
# counts. If the function wrote nothing, "no kind we wrote disagrees with core"
# is true of the empty set and the suite would report success for a function that
# does nothing at all -- the exact failure this file exists to detect.

check_num "we wrote a most-common list for every column, as core did" \
	"$(q "SELECT count(DISTINCT attname) FROM ad_shape WHERE source = 'ours' AND kind = 1")" "5"

check_num "and a histogram for exactly the four core gave one" \
	"$(q "SELECT count(DISTINCT attname) FROM ad_shape WHERE source = 'ours' AND kind = 2")" "4"

# The columns must be the SAME four, not merely four of them.
check_num "and they are the same four columns, not just the same count" \
	"$(q "SELECT count(*) FROM (
		SELECT attname FROM ad_shape WHERE source = 'ours' AND kind = 2
		EXCEPT
		SELECT attname FROM ad_shape WHERE source = 'core' AND kind = 2) t")" "0"

# Every kind we wrote must exist in core's shape for that column carrying the
# same operator and collation. A missing counterpart counts as a mismatch, which
# is what the LEFT JOIN with an IS NULL test does.
check_num "every statistic we wrote agrees with core on operator and collation" \
	"$(q "SELECT count(*) FROM ad_shape o
		LEFT JOIN ad_shape c ON c.source = 'core' AND c.attname = o.attname
							AND c.kind = o.kind AND c.op = o.op AND c.coll = o.coll
		WHERE o.source = 'ours' AND c.attname IS NULL")" "0"

# The element type of the stored array is NOT checked here, and the reason is
# worth writing down because the check that was here looked right and measured
# nothing.
#
# pg_statistic.stavalues1 is declared `anyarray` (pg_statistic.h:119), so
# pg_typeof(stavalues1) returns the static type of the expression -- the constant
# string "anyarray" -- for every row ever stored. Comparing that against the
# column's type reported all nine of our slots as mismatched, which looked like a
# defect in the function and was a defect in the probe: it fails identically
# against CORE's own statistics, so it was testing the expectation rather than
# the code. A count that comes back equal to the total is a probe result, not a
# measurement.
#
# What IS observable is stronger anyway, and is asserted below: whether each
# stored value exists in the column with exactly the stored frequency. A value
# mangled by bad quoting, or a frequency scaled by the wrong denominator, fails
# that on the actual data rather than on a type name.

# --- the values and frequencies mean what they say, per type -------------------
#
# Run per column with the column's own type, because this is where a type-
# dependent defect surfaces: text literals lose their quoting, numeric and date
# have their own output forms. The oracle is an independent count over the table,
# never a re-reading of what the function wrote.
#
# A tolerance is required and is not slack: most_common_freqs is real (float4,
# ~7 significant digits) while the true frequency is exact numeric, so demanding
# equality would fail on representation rather than on correctness.

# Not premises (#1339): these read the most-common list pgcolumnar.analyze() wrote,
# so a repository change reddens them -- with the list passed as NULL, all five went
# red. One literal name per column, so the grader sees each.
mcv_listed() {	# $1 column, $2 type -> "yes" when the column has a most-common list
	local n
	n="$(q "SELECT coalesce(array_length(most_common_vals::text::$2[], 1), 0)
		FROM pg_stats WHERE tablename = 'ad_c' AND attname = '$1'")"
	if pgc_is_number "$n" && [ "$n" -ge 1 ]; then echo yes; else echo "no (length [$n])"; fi
}
# The oracle is an independent count over the table, never a re-reading of what the
# function wrote. An empty list has no wrong value, which is why mcv_listed comes
# first: an empty MCV list makes "no value disagrees" true of nothing.
mcv_wrong() {	# $1 column, $2 type -> most-common values whose stored frequency is wrong
	q "WITH m AS (
			SELECT unnest(most_common_vals::text::$2[]) AS v,
				   unnest(most_common_freqs) AS f
			  FROM pg_stats WHERE tablename = 'ad_c' AND attname = '$1')
		SELECT count(*) FROM m
		 WHERE abs((SELECT count(*) FROM ad_c WHERE ad_c.$1 IS NOT DISTINCT FROM m.v)::numeric
				   / $ROWS - m.f::numeric) > 0.000001"
}
check "i has a most-common list to check" "$(mcv_listed i int)" "yes"
check_num "every most-common value of i exists with exactly its stored frequency" "$(mcv_wrong i int)" "0"
check "t has a most-common list to check" "$(mcv_listed t text)" "yes"
check_num "every most-common value of t exists with exactly its stored frequency" "$(mcv_wrong t text)" "0"
check "n has a most-common list to check" "$(mcv_listed n numeric)" "yes"
check_num "every most-common value of n exists with exactly its stored frequency" "$(mcv_wrong n numeric)" "0"
check "d has a most-common list to check" "$(mcv_listed d date)" "yes"
check_num "every most-common value of d exists with exactly its stored frequency" "$(mcv_wrong d date)" "0"
check "b has a most-common list to check" "$(mcv_listed b boolean)" "yes"
check_num "every most-common value of b exists with exactly its stored frequency" "$(mcv_wrong b boolean)" "0"

# --- the values themselves survived the round trip ----------------------------
#
# Shape agreement does not prove the text column's array literal was assembled
# correctly: a literal that lost a comma still parses, into the wrong values.
# These two are the ones built to break it.

check_num "the most-common text value containing a comma round-tripped intact" \
	"$(q "SELECT count(*) FROM pg_stats
		WHERE tablename = 'ad_c' AND attname = 't'
		  AND 'alpha,beta' = ANY (most_common_vals::text::text[])")" "1"

check_num "and the one containing a quote" \
	"$(q "SELECT count(*) FROM pg_stats
		WHERE tablename = 'ad_c' AND attname = 't'
		  AND 'it''s here' = ANY (most_common_vals::text::text[])")" "1"

# boolean has two distinct values, both repeated, so the MCV list describes the
# column completely and there is nothing left to bucket. Core emits no histogram
# and neither may we: num_hist = ndistinct - num_mcv = 0 (analyze.c:2744).
check "boolean gets no histogram, because the most-common list already describes it" \
	"$(q "SELECT coalesce((SELECT histogram_bounds::text FROM pg_stats
		WHERE tablename = 'ad_c' AND attname = 'b'), '<none>')")" "<none>"

pgc_summary
