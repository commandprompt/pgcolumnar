#!/usr/bin/env bash
#
# The columnar scan must be priced for what the zone maps let it skip (#434).
#
# The scan skips a row group whose stored minimum and maximum prove the
# restriction cannot match, but the cost model quoted a full scan regardless. An
# index scan on the same relation was quoted at its best case, so the planner
# preferred it: priced 12,407 and ran 500 ms, against a columnar scan priced
# 17,326 that ran 43 ms. Declined a plan 11.6x faster, on a correlated bigint key
# with 80 percent of the rows excluded.
#
# What is asserted here is the PLAN and the COST RELATION, not wall clock. Which
# plan the planner picks is deterministic given statistics, so nothing in this
# suite is timing-sensitive.
#
# The control is the point. A blanket discount would also flip the correlated
# case, and would be wrong: pruning depends on how the matching rows are LAID
# OUT, not on how many there are. So the same query shape runs against a
# scattered key, where every group holds the full value range and nothing can be
# skipped, and the discount must NOT appear there.
#
# Usage:  test/zonemap_cost.sh [PG_CONFIG]
# Written fresh for pgColumnar.
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

ROWS=200000
CUT=$((ROWS * 8 / 10))

# Two tables, one query shape. `seq` ascends with physical order; `scat` is the
# same values in an order uncorrelated with it, so its zone maps overlap and
# prune nothing.
# `saw` is the third shape, and it is the one correlation cannot see (#461).
#
# Correlation measures agreement between value order and physical order across
# the WHOLE relation. Pruning is local: a group is skipped when its own min and
# max rule the predicate out. A table can be globally unsorted and locally tight
# at once, and that is not a corner case -- it is what batch-loaded time-series
# data looks like, which is the shape this engine exists for.
#
# So each group of 10,000 rows covers one narrow band of the value range, and the
# bands are handed out by (i * 9) mod 20, which is a permutation because 9 and 20
# are coprime. Group spans come out 0-9999, 90000-99999, 180000-189999 and so on:
# 5 percent of the range each, in an order that does not follow physical order.
#
# The multiplier is chosen, not decorative. Correlation of (i * m) mod 20 with i:
#
#     m = 7  ->  +0.3665      m = 9  ->  +0.0376      m = 19 ->  -0.7143
#
# m = 7 was the first version and it is the trap this issue is about: 0.3665
# earns a partial rho^2 discount, so the check below would have PASSED without
# any fix, for the wrong reason, on the exact defect under test. m = 9 lands at
# 0.0376, next to the 0.0133 and 0.0463 measured on the TSBS `time` column in
# #391 -- the case on the record.
#
# The within-group order is scrambled too ((g * 7919) mod 10000). Letting values
# ascend inside a group adds correlation on top of the band ordering, and local
# tightness only needs min and max to be narrow, never sorted.

psql_run "DROP TABLE IF EXISTS zc;
	CREATE TABLE zc (seq bigint, scat bigint, saw bigint, tag int, pad text) USING pgcolumnar;
	SELECT pgcolumnar.set_options('zc', stripe_row_limit => 10000);
	INSERT INTO zc
	SELECT g, ((g * 7919) % $ROWS),
	       ((((g - 1) / 10000) * 9) % 20) * 10000 + ((g * 7919) % 10000),
	       g % 5, md5(g::text)
	  FROM generate_series(1, $ROWS) g;
	CREATE INDEX zc_seq ON zc (seq);
	CREATE INDEX zc_scat ON zc (scat);
	CREATE INDEX zc_saw ON zc (saw);
	ANALYZE zc;" >/dev/null 2>&1

check_num "premise: every row loaded" "$(q 'SELECT count(*) FROM zc')" "$ROWS"

CORR_SEQ=$(q "SELECT round(abs(correlation)::numeric, 2) FROM pg_stats
              WHERE tablename = 'zc' AND attname = 'seq'")
CORR_SCAT=$(q "SELECT round(abs(correlation)::numeric, 2) FROM pg_stats
               WHERE tablename = 'zc' AND attname = 'scat'")
echo "-- |correlation|: seq = ${CORR_SEQ:-?}, scat = ${CORR_SCAT:-?}"

# The premise the whole suite rests on. Without this the two arms could differ
# for some reason other than the one being tested, and the control would prove
# nothing. A fixture whose "scattered" column is accidentally ordered makes both
# arms the correlated arm.
check_num "premise: the ordered key really is correlated" \
	"$([ -n "$CORR_SEQ" ] && awk "BEGIN{exit !($CORR_SEQ > 0.9)}" && echo 1 || echo 0)" "1"
check_num "premise: the scattered key really is not" \
	"$([ -n "$CORR_SCAT" ] && awk "BEGIN{exit !($CORR_SCAT < 0.2)}" && echo 1 || echo 0)" "1"

# EXPLAIN's chosen node, and the total cost of a named path.
node_for() {	# $1 = column
	psql_run "SET max_parallel_workers_per_gather=0;
	          EXPLAIN (COSTS OFF)
	          SELECT tag, count(*) FROM zc WHERE $1 > $CUT GROUP BY tag;" 2>/dev/null |
		grep -oE 'Custom Scan \(PgColumnarScan\)|Index Scan|Seq Scan' | head -1
}
cost_for() {	# $1 = column, $2 = extra SETs
	psql_run "SET max_parallel_workers_per_gather=0; $2
	          EXPLAIN SELECT tag, count(*) FROM zc WHERE $1 > $CUT GROUP BY tag;" 2>/dev/null |
		grep -oE 'cost=[0-9.]+\.\.[0-9.]+' | head -1 | sed 's/.*\.\.//'
}

# ---- the physical premise, which this suite asserted only in its title -------
#
# Everything below prices pruning. Nothing below checked that any pruning
# happens, and for the whole life of this file none did (#477).
#
# `seq` is bigint and `$CUT` is a bare integer, so the scan key was cross-type
# and the reader dropped it: zero chunk groups removed, on the arm whose entire
# purpose is to be the one that prunes. The suite still passed, because a cost
# relation between two priced plans is true or false regardless of whether the
# physical effect being priced occurs. #460's discount was validated here.
#
# So the premise is now measured from the executor's own counter, before any
# cost is compared. A discount for pruning that does not happen is not a
# conservative error, it is a wrong price.
removed_for() {	# $1 = column
	psql_run "SET max_parallel_workers_per_gather=0;
	          SET enable_indexscan=off; SET enable_bitmapscan=off;
	          EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF)
	          SELECT tag, count(*) FROM zc WHERE $1 > $CUT GROUP BY tag;" 2>/dev/null |
		grep -oE 'Columnar Chunk Groups Removed by Filter: [0-9]+' |
		grep -oE '[0-9]+$' | head -1
}

SEQ_REMOVED=$(removed_for seq)
SCAT_REMOVED=$(removed_for scat)
echo "-- groups removed: seq = ${SEQ_REMOVED:-?}, scat = ${SCAT_REMOVED:-?}"

# Named as an arm, not a premise: a reader that drops the cross-type key (#477)
# turns it red, and the ledger gate refuses a premise a mutation has reddened.
check_num "the correlated arm actually removes chunk groups (#477)" \
	"$([ -n "$SEQ_REMOVED" ] && awk "BEGIN{exit !($SEQ_REMOVED > 0)}" && echo 1 || echo 0)" "1"

# And the control removes none, which is what makes the discount's absence there
# meaningful rather than incidental.
check_num "premise: and the scattered arm removes none, so the two differ physically" \
	"$([ -n "$SCAT_REMOVED" ] && awk "BEGIN{exit !($SCAT_REMOVED == 0)}" && echo 1 || echo 0)" "1"

# ---- the correlated arm: pruning is real, so it must be priced -------------
SEQ_NODE=$(node_for seq)
SEQ_COL=$(cost_for seq "SET enable_indexscan=off; SET enable_bitmapscan=off;")
SEQ_IDX=$(cost_for seq "SET pgcolumnar.enable_custom_scan=off; SET enable_seqscan=off; SET enable_bitmapscan=off;")
echo "-- correlated: chosen=$SEQ_NODE  columnar=$SEQ_COL  index=$SEQ_IDX"

check_num "premise: the columnar cost is a measurement" \
	"$([ -n "$SEQ_COL" ] && awk "BEGIN{exit !($SEQ_COL > 0)}" && echo 1 || echo 0)" "1"
check_num "premise: the index cost is a measurement" \
	"$([ -n "$SEQ_IDX" ] && awk "BEGIN{exit !($SEQ_IDX > 0)}" && echo 1 || echo 0)" "1"
check_text "a correlated restriction picks the columnar scan (#434)" \
	"$SEQ_NODE" "Custom Scan (PgColumnarScan)"
check_num "and it is priced below the index scan it used to lose to (#434)" \
	"$(awk "BEGIN{print ($SEQ_COL < $SEQ_IDX) ? 1 : 0}")" "1"

# ---- the control: nothing to prune, so nothing to discount ------------------
#
# This is what separates "price the pruning" from "make the columnar scan
# cheaper". The discount must come from the layout, and here there is none.
SCAT_COL=$(cost_for scat "SET enable_indexscan=off; SET enable_bitmapscan=off;")
FULL_COL=$(cost_for scat "SET enable_indexscan=off; SET enable_bitmapscan=off; SET pgcolumnar.enable_custom_scan=on;")
echo "-- scattered: columnar=$SCAT_COL"
check_num "premise: the scattered columnar cost is a measurement" \
	"$([ -n "$SCAT_COL" ] && awk "BEGIN{exit !($SCAT_COL > 0)}" && echo 1 || echo 0)" "1"
# A MARGIN, as in the #461 check below, and for the same reason. Written as a bare
# `>` this passed under a blanket discount (every restriction priced at a fifth):
# 921.01 against 919.29, two equal prices apart by the sampling noise in ANALYZE.
# The control exists to catch exactly that, so it must demand the gap pruning makes:
# the scattered arm removes none of 20 groups and the correlated arm 16, about 4.6x
# in cost on this fixture, and twice is far above the noise.
check_num "an uncorrelated restriction gets no pruning discount (#434)" \
	"$(awk "BEGIN{print ($SCAT_COL > $SEQ_COL * 2) ? 1 : 0}")" "1"

# ---- #461: locally tight, globally uncorrelated -----------------------------
#
# The gap in the correlation model. Everything physical is measured before
# anything about price is asserted, because "this column prunes" is the premise
# the whole arm rests on, and re-deriving it from the model under test would make
# the check tautological.

CORR_SAW=$(q "SELECT round(abs(correlation)::numeric, 4) FROM pg_stats
              WHERE tablename = 'zc' AND attname = 'saw'")
SAW_REMOVED=$(removed_for saw)
echo "-- sawtooth: |correlation| = ${CORR_SAW:-?}, groups removed = ${SAW_REMOVED:-?}"

check_num "premise: the sawtooth column is uncorrelated, like TSBS time (#391)" \
	"$([ -n "$CORR_SAW" ] && awk "BEGIN{exit !($CORR_SAW < 0.2)}" && echo 1 || echo 0)" "1"

# The two premises together are the issue in one line: no correlation, and it
# prunes anyway. Either alone proves nothing.
check_num "premise: and it prunes as hard as the correlated column, measured" \
	"$([ -n "$SAW_REMOVED" ] && [ -n "$SEQ_REMOVED" ] &&
	   awk "BEGIN{exit !($SAW_REMOVED >= $SEQ_REMOVED)}" && echo 1 || echo 0)" "1"

SAW_NODE=$(node_for saw)
SAW_COL=$(cost_for saw "SET enable_indexscan=off; SET enable_bitmapscan=off;")
SAW_IDX=$(cost_for saw "SET pgcolumnar.enable_custom_scan=off; SET enable_seqscan=off; SET enable_bitmapscan=off;")
echo "-- sawtooth: chosen=$SAW_NODE  columnar=$SAW_COL  index=$SAW_IDX"

check_num "premise: the sawtooth columnar cost is a measurement" \
	"$([ -n "$SAW_COL" ] && awk "BEGIN{exit !($SAW_COL > 0)}" && echo 1 || echo 0)" "1"

# The assertion. A column that demonstrably removes as many groups as the
# correlated one must be priced like it, not like the one that removes none,
# because correlation is not what the reader consults when it skips a group.
#
# A MARGIN, not merely "less than". Written as a bare `<` this check passed
# before the fix existed, on 3215.33 against 3222.29 -- a 0.2 percent difference
# that is rounding in the seq-page term, reported as a discount. The arm removes
# 16 of 20 groups, so anything worth calling a discount is far larger than the
# gap between two undiscounted prices.
check_num "a demonstrably pruning restriction is discounted whatever its correlation (#461)" \
	"$(awk "BEGIN{print ($SAW_COL < $SCAT_COL * 0.8) ? 1 : 0}")" "1"

# Priced like what it physically resembles. Bounded rather than exact: the model
# estimates from a sample of groups, so demanding equality with the correlated
# arm would assert the sampler's precision rather than the behaviour.
check_num "and priced close to the correlated arm it matches physically (#461)" \
	"$(awk "BEGIN{print ($SAW_COL < $SEQ_COL * 1.5) ? 1 : 0}")" "1"

check_text "and the planner picks the columnar scan for it" \
	"$SAW_NODE" "Custom Scan (PgColumnarScan)"

# ---- correctness, which the cost model may not change ----------------------
check_text "the correlated query returns the same rows either way" \
	"$(q "SELECT md5(string_agg(tag || ':' || n, ',' ORDER BY tag)) FROM (
	        SELECT tag, count(*) n FROM zc WHERE seq > $CUT GROUP BY tag) s")" \
	"$(q "SET pgcolumnar.enable_custom_scan=off;
	      SELECT md5(string_agg(tag || ':' || n, ',' ORDER BY tag)) FROM (
	        SELECT tag, count(*) n FROM zc WHERE seq > $CUT GROUP BY tag) s" | tail -1)"
check_num "the correlated query returns the right count" \
	"$(q "SELECT count(*) FROM zc WHERE seq > $CUT")" "$((ROWS - CUT))"

pgc_summary
