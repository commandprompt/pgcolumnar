#!/usr/bin/env bash
#
# A bloom filter must be sized by its DISTINCT count, not its value count (#467).
#
# PgColumnarBloomBuild takes n = the number of hashes, which is the stripe's row
# count, and both of its guards test that same n. At the default stripe limit
# neither fires: `n < 64` is false because n is 150,000, and the BLOOM_MAX_BITS
# refusal only fires above n = 209,715. So every bloomable column of every stripe
# gets next_pow2(n * 10) bits regardless of cardinality -- 256 KB for a column
# with five distinct values and 256 KB for a unique one.
#
# Measured on 2M ClickBench rows: 19.3x over-provisioned, filters 361 MB raw
# against a 262 MB table, and 29% of load time.
#
# WHAT IS ASSERTED, AND WHY THIS SHAPE
#
# The defect is a SIZE, and `pgcolumnar.bloom.filter` is a bytea in a catalog
# table, so it is measured directly with octet_length. No timing, so nothing here
# is subject to PGC_SKIP_TIMING.
#
# The assertion is the RELATION between two cardinalities in the same table, not
# a literal byte count. A literal would snapshot today's next_pow2 constants and
# fail the next time BLOOM_BITS_PER_VALUE is tuned, which is not this suite's
# business.
#
# The selectivity arm is the one that matters adversarially: shrinking a filter by
# making it useless would satisfy every size check here. A filter that no longer
# skips is worthless at any size.
#
# Usage:  test/bloom_sizing.sh [PG_CONFIG]
# Written fresh for pgColumnar.
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

ROWS=20000

# One table, one stripe, two columns that differ ONLY in cardinality. Same rows,
# same type, same filter code path -- so a difference in filter size can only be
# cardinality, which is what makes this able to discriminate at all.
psql_run "DROP TABLE IF EXISTS bs;
	CREATE TABLE bs (lo int, hi int, pad text) USING pgcolumnar;
	SELECT pgcolumnar.set_options('bs', stripe_row_limit => $ROWS);
	INSERT INTO bs SELECT g % 5, g, md5(g::text) FROM generate_series(1, $ROWS) g;
	ANALYZE bs;" >/dev/null 2>&1

SID=$(q "SELECT pgcolumnar.get_storage_id('bs')")
flt() {	# $1 = 1-based column index -> stored filter bytes, or empty when absent
	q "SELECT octet_length(filter) FROM pgcolumnar.bloom
	   WHERE storage_id = $SID AND column_index = $(( $1 - 1 ))
	   ORDER BY group_number LIMIT 1"
}

# ---- premises, or a smaller filter proves nothing ---------------------------
#
# octet_length of a MISSING row is empty, not 0, and an empty side compares equal
# to another empty side under plain check (#418). Every size assertion below
# needs the row to exist first.
check_num "premise: every row loaded" "$(q 'SELECT count(*) FROM bs')" "$ROWS"
check_num "premise: the whole table is one row group" \
	"$(q "SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = $SID")" "1"
check_num "premise: the low-cardinality column really is low" \
	"$(q 'SELECT count(DISTINCT lo) FROM bs')" "5"
check_num "premise: the high-cardinality column really is high" \
	"$(q 'SELECT count(DISTINCT hi) FROM bs')" "$ROWS"

LO=$(flt 1)
HI=$(flt 2)
echo "-- filter bytes: lo (5 distinct) = ${LO:-none}, hi ($ROWS distinct) = ${HI:-none}"
# check_num refuses a non-number on either side, so an absent filter, an errored
# query or a psql notice fails here rather than being coerced to 0 downstream.
# The comparison below then uses the RAW values with no default: `${LO:-0}` made
# the headline assertion evaluate `0 * 8 <= 0` and PASS when both measurements
# were missing, which is the #418 shape this suite is supposed to be immune to.
check_num "premise: the low-cardinality filter size is a measurement" "${LO:-x}" "${LO:-y}"
check_num "premise: the high-cardinality filter size is a measurement" "${HI:-x}" "${HI:-y}"

# ---- the assertion ----------------------------------------------------------
#
# Sized by distinct count, `lo` needs next_pow2(5 * 10) = 64 bits and `hi` needs
# next_pow2(20000 * 10) = 262,144. Sized by value count they are identical, which
# is the defect. A bound of 8x is far inside the real ratio and far outside any
# rounding effect.
check_num "a five-value column does not get a unique column's filter (#467)" \
	"$(awk "BEGIN{print ($LO * 8 <= $HI) ? 1 : 0}")" "1"

# ---- the arm that stops this being satisfied by a useless filter ------------
#
# Shrinking a filter to nothing would pass every size check above. What a filter
# is FOR is skipping groups on an equality probe, so that has to survive. A value
# outside the column's five must still be excluded.
check_num "a value that is absent is still reported absent" \
	"$(q 'SELECT count(*) FROM bs WHERE lo = 999')" "0"
check_num "and a value that is present is still found" \
	"$(q 'SELECT count(*) FROM bs WHERE lo = 3')" "$(( ROWS / 5 ))"

# ---- equivalence: a smaller filter may not change an answer -----------------
#
# The filter is an optimisation, so the same query must return the same rows with
# it and without it. Both sides go through check_text, which refuses an empty
# side: a down cluster returns nothing on both arms and would otherwise compare
# equal.
ON=$(q "SELECT md5(string_agg(hi::text, ',' ORDER BY hi)) FROM bs WHERE lo = 2")
OFF=$(q "SET pgcolumnar.enable_bloom_filter = off;
         SELECT md5(string_agg(hi::text, ',' ORDER BY hi)) FROM bs WHERE lo = 2" | tail -1)
check_text "results are identical with the filter and without it" "$ON" "$OFF"


# ---- the filter must still SKIP, which no size check can show ----------------
#
# Every check above passes with pgcolumnar.enable_bloom_filter = off: that GUC
# gates only the read-side probe, so filters are still written, the sizes still
# differ, and the on/off md5 pair is trivially equal because NEITHER side used a
# filter. This suite claimed to guard against "smaller by being useless" and did
# not. A skip oracle is the only thing that shows a probe was consulted, and it
# needs several row groups -- the single-group fixture above cannot produce one,
# because there is nothing to skip.
#
# The values are SCATTERED, and that is the whole design. With sequential k every
# group's min/max covers a narrow band, so the zone maps alone remove nine of ten
# groups and the counter reads 9 whether or not a bloom filter is consulted --
# measured, by disabling the probe and watching the number not move. Scattering
# makes every group's min/max span the full range, so zone maps can exclude
# nothing and only the bloom filter can.
psql_run "DROP TABLE IF EXISTS bsk;
	CREATE TABLE bsk (k int, pad text) USING pgcolumnar;
	SELECT pgcolumnar.set_options('bsk', stripe_row_limit => 2000);
	INSERT INTO bsk SELECT (g * 7919) % 20000, md5(g::text)
	  FROM generate_series(1, 20000) g;
	ANALYZE bsk;" >/dev/null 2>&1

check_num "premise: the skip fixture has many row groups to skip" \
	"$(q "SELECT count(*) FROM pgcolumnar.row_group
	      WHERE storage_id = pgcolumnar.get_storage_id('bsk')")" "10"

skipped() {	# $1 = probed value, $2 = extra SETs -> groups removed
	psql_run "SET max_parallel_workers_per_gather=0; $2
	          EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF)
	          SELECT count(*) FROM bsk WHERE k = $1;" 2>/dev/null |
		grep 'Columnar Chunk Groups Removed by Filter' | grep -oE '[0-9]+' | head -1
}

SK=$(skipped 4242 "")
SK_OFF=$(skipped 4242 "SET pgcolumnar.enable_bloom_filter=off;")
echo "-- chunk groups removed: bloom on = ${SK:-none}, bloom off = ${SK_OFF:-none}"
check_num "premise: the skip counter is reported with the probe on" "${SK:-x}" "${SK:-y}"
check_num "premise: the skip counter is reported with the probe off" "${SK_OFF:-x}" "${SK_OFF:-y}"
# The zone maps must NOT be able to do this on their own, or the next check
# passes without a bloom probe ever running.
check_num "premise: zone maps alone skip nothing on this fixture" "$SK_OFF" "0"
check_num "the filter still skips groups it can prove empty (#467)" \
	"$(awk "BEGIN{print ($SK > 0) ? 1 : 0}")" "1"

# ---- the guard that actually changed, which nothing in the tree crossed ------
#
# BLOOM_MAX_BITS refuses a filter above 209,715 distinct. Before this change the
# refusal tested the VALUE count; it now tests the DISTINCT count, and no suite
# anywhere reached either threshold -- the largest stripe_row_limit in any bloom
# suite was 20,000. The branch whose semantics changed had zero coverage, which
# is how an allocation regression above the same threshold shipped green.
psql_run "DROP TABLE IF EXISTS bsx;
	CREATE TABLE bsx (u bigint, pad text, lo int) USING pgcolumnar;
	SELECT pgcolumnar.set_options('bsx', stripe_row_limit => 300000);
	INSERT INTO bsx SELECT g, md5(g::text), g % 5 FROM generate_series(1, 300000) g;
	ANALYZE bsx;" >/dev/null 2>&1

check_num "premise: one row group holds more than the refusal threshold" \
	"$(q "SELECT max(row_count) FROM pgcolumnar.row_group
	      WHERE storage_id = pgcolumnar.get_storage_id('bsx')")" "300000"
check_num "premise: those values really are distinct" \
	"$(q 'SELECT count(DISTINCT u) FROM bsx')" "300000"
# Read as "refused/kept": u's filters against those of lo, a five-value column in the
# same row group, which is far under the cap and must keep its filter. A count of
# zero alone also reads 0 when no filter is written at all, and with the build
# refusing every filter this check passed on main.
check_text "a column above the distinct cap is refused a filter, not given a bad one" \
	"$(q "SELECT count(*) FILTER (WHERE column_index = 0) || '/' ||
	             count(*) FILTER (WHERE column_index = 2)
	      FROM pgcolumnar.bloom WHERE storage_id = pgcolumnar.get_storage_id('bsx')")" "0/1"
check_num "and the load itself succeeded, rather than erroring on the way" \
	"$(q 'SELECT count(*) FROM bsx')" "300000"

pgc_summary
