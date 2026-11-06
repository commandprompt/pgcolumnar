#!/usr/bin/env bash
#
# pgColumnar fetch-by-row-number: cost independent of position (issue #143).
#
# #148 made a fetch decode its row group once and cache it, which removed the
# repeated decode but not the walk to the row. Reaching row r's value still meant
# counting the validity bits below r and then decoding and discarding that many
# values, so a cache hit cost O(r) and fetching a whole group stayed quadratic.
# Two indexes built with the decoded group -- a rank prefix over the validity
# bitmap, and an offset table for varying-length columns -- make it O(1).
#
# Four things are asserted.
#
# 1. The values are right, which is where this change can go wrong quietly. The
#    rank has to count present values, not rows, so a column with many nulls is
#    the case that separates a correct index from a plausible one; an off-by-one
#    in the rank returns a neighbouring row's value rather than an error. Checked
#    differentially against a heap mirror over fixed-length, varying-length and
#    heavily-null columns.
#
# 2. The cost no longer depends on where the row is. Timed as a ratio: the same
#    number of index-driven fetches against the first tenth of one big row group
#    and against the last tenth. Before the change the far end walked most of the
#    group per row and the ratio ran with the group size; now both are constant
#    work. A ratio is portable where a millisecond count is not.
#
# 3. Fetching a whole group is no longer quadratic in the group. Doubling the
#    group size while fetching the same number of rows should not double the
#    cost, because neither the rank nor the offset lookup grows with it.
#
# Usage:  test/native_fetch_position.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

ROWS=${PGC_FETCHPOS_ROWS:-120000}

# --- 1. the values are right ---------------------------------------------------

# every fourth row null in the nullable columns, so the rank and the row number
# diverge widely by the end of the group
psql_run "DROP TABLE IF EXISTS fp_c; DROP TABLE IF EXISTS fp_h;
	SET pgcolumnar.stripe_row_limit = $ROWS;
	SET pgcolumnar.chunk_group_row_limit = $ROWS;
	CREATE TABLE fp_c (id int, fixed bigint, txt text, sparse int, stxt text)
		USING pgcolumnar;
	INSERT INTO fp_c
		SELECT g, g * 7,
			   repeat('x', 1 + (g % 40)) || g,
			   CASE WHEN g % 4 = 0 THEN NULL ELSE g END,
			   CASE WHEN g % 4 = 0 THEN NULL ELSE 's' || g END
		FROM generate_series(1, $ROWS) g;
	CREATE INDEX fp_c_id ON fp_c (id);
	CREATE TABLE fp_h (LIKE fp_c);
	INSERT INTO fp_h SELECT * FROM fp_c;" >/dev/null 2>&1

check "the rows are in one row group" \
	"$(q "SELECT count(*) FROM pgcolumnar.row_group r
		JOIN pgcolumnar.storage s ON s.storage_id = r.storage_id
		WHERE s.relation_oid = 'fp_c'::regclass;")" \
	"1"

# Fetch by row number is what an index scan drives, so force one. Each leading
# SET prints its own line ahead of the result, so these queries take the last.
#
# enable_seqscan=off does NOT turn the columnar custom scan off, and it was all
# this suite set: measured, the single-row fetch, the near and far UPDATEs and
# the mirror join all planned as Custom Scan (PgColumnarScan), so none of them
# fetched by row number. A rank off by one left the single-row arm green, and a
# walk over every earlier row left both timing arms green. The custom scan is
# off now, and each measurement asserts first that its plan reads the index.
FORCE="SET enable_seqscan = off; SET enable_bitmapscan = off;
	SET max_parallel_workers_per_gather = 0;
	SET pgcolumnar.enable_custom_scan = off;"

# reads_index QUERY INDEX -> yes, or the plan's first line: the premise that a
# query fetches by row number through INDEX rather than scanning
reads_index() {
	local plan
	plan="$(q "$FORCE EXPLAIN (COSTS OFF) $1")"
	case "$plan" in
		*"Index Scan using $2 "*) echo yes ;;
		*) echo "no: $(grep -m1 -E 'Scan|Update' <<<"$plan" | sed 's/^ *//')" ;;
	esac
}

check "premise: the mirror comparison fetches through the index" \
	"$(reads_index "SELECT count(*) FROM fp_c c JOIN fp_h h USING (id) WHERE c.fixed IS DISTINCT FROM h.fixed" fp_c_id)" "yes"
check "premise: a single-row fetch goes through the index" \
	"$(reads_index "SELECT fixed FROM fp_c WHERE id = 1001" fp_c_id)" "yes"

# rows spread across the whole group, including the far end where the old walk
# was longest
mismatch="$(q "$FORCE
	SELECT count(*) FROM fp_c c JOIN fp_h h USING (id)
	WHERE c.fixed IS DISTINCT FROM h.fixed
	   OR c.txt IS DISTINCT FROM h.txt
	   OR c.sparse IS DISTINCT FROM h.sparse
	   OR c.stxt IS DISTINCT FROM h.stxt;" | tail -1)"
check "every column matches the heap mirror" "$mismatch" "0"

# the same through the fetch path one row at a time, at positions where an
# off-by-one in the rank would land on a neighbour
bad=""
for pos in 1 2 3 4 5 999 1000 1001 $((ROWS / 2)) $((ROWS - 1)) "$ROWS"; do
	got="$(q "$FORCE SELECT fixed || '|' || txt || '|' ||
		coalesce(sparse::text, 'N') || '|' || coalesce(stxt, 'N')
		FROM fp_c WHERE id = $pos;" | tail -1)"
	want="$(q "SELECT fixed || '|' || txt || '|' ||
		coalesce(sparse::text, 'N') || '|' || coalesce(stxt, 'N')
		FROM fp_h WHERE id = $pos;" | tail -1)"
	[ "$got" = "$want" ] || bad="$bad id=$pos(got=$got want=$want)"
done
check "single-row fetches are right at every position" "${bad:-same}" "same"

# --- 2. the cost does not depend on the position -------------------------------

N=$((ROWS / 20))
NEAR_LO=1
NEAR_HI=$N
FAR_LO=$((ROWS - N + 1))
FAR_HI=$ROWS

upd_once() {  # id-low, id-high -> milliseconds for one pass
	local start end
	start=$(date +%s%N)
	psql_run "$FORCE SELECT sum(fixed), sum(length(txt)) FROM fp_c
		WHERE id BETWEEN $1 AND $2;" >/dev/null 2>&1
	end=$(date +%s%N)
	echo $(( (end - start) / 1000000 ))
}

# The median of three passes, not one. A single pass is far too noisy to compare:
# measured on an idle box, the far/near ratio of one pass ranged from 0.52 to
# 1.94 across five repetitions, with the far end frequently the faster of the
# two. That is well inside the tolerance, but a single sample wanders outside it
# under any competing load, and this check failed a gate at 3.05 for that reason
# alone. A flaky check in the matrix is worse than no check: it teaches the
# reader to discount a red result.
#
# The median is taken rather than the tolerance widened, because widening is what
# costs discriminating power. The defect this guards against (issue #143, the
# fetch walking every earlier row of the group) made the far end quadratically
# slower, 1,001,374 ms against 614 ms once fixed. A ratio of 3 catches that with
# orders of magnitude to spare, so the threshold is not what needs relaxing.
upd_ms() {  # id-low, id-high -> median of three passes
	local a b c
	a=$(upd_once "$1" "$2")
	b=$(upd_once "$1" "$2")
	c=$(upd_once "$1" "$2")
	printf '%s\n%s\n%s\n' "$a" "$b" "$c" | sort -n | sed -n 2p
}

# The timed pass is a SELECT, not the UPDATE it was. A columnar UPDATE writes
# each row again into a new row group, so only the first of three passes
# fetched from the far end of the big group; the second and third fetched from
# the start of the new one, and the median was always one of those. Measured
# with the rank walking every earlier row: the UPDATE form read near 122 ms,
# far 132 ms, green. A read leaves every row where it was, so all three passes
# fetch the same positions. sum(length(txt)) makes it decode a varying-length
# column, which the offset table serves.
check "premise: the timed reads fetch through the index" \
	"$(reads_index "SELECT sum(fixed), sum(length(txt)) FROM fp_c WHERE id BETWEEN $FAR_LO AND $FAR_HI" fp_c_id)" "yes"

near="$(upd_ms $NEAR_LO $NEAR_HI)"
far="$(upd_ms $FAR_LO $FAR_HI)"
echo "-- $N fetches, median of three: first tenth ${near} ms, last tenth ${far} ms"

check "fetching from the far end of a group costs about what the near end does" \
	"$(awk -v n="$near" -v f="$far" \
		'BEGIN { print (n > 0 && f / n < 3) ? "yes" : "no (near=" n "ms far=" f "ms)" }')" \
	"yes"

# --- 3. a bigger group does not cost more per fetch ----------------------------

build() {  # table, rows
	psql_run "DROP TABLE IF EXISTS $1;
		SET pgcolumnar.stripe_row_limit = $2;
		SET pgcolumnar.chunk_group_row_limit = $2;
		CREATE TABLE $1 (id int, v bigint, t text) USING pgcolumnar;
		INSERT INTO $1 SELECT g, g * 3, 't' || g FROM generate_series(1, $2) g;
		CREATE INDEX ${1}_id ON $1 (id);" >/dev/null 2>&1
}

# The rows fetched must sit at the END of the group. Fetching the first K rows
# costs the same in a big group as in a small one even with the old walk, because
# the walk is to the row's position and those rows are near the start either way:
# that check would pass on both sides and prove nothing.
fetch_once() {  # table, table-rows, count -> milliseconds for one pass
	local start end
	start=$(date +%s%N)
	psql_run "$FORCE SELECT sum(v), sum(length(t)) FROM $1 WHERE id > $(( $2 - $3 ));" \
		>/dev/null 2>&1
	end=$(date +%s%N)
	echo $(( (end - start) / 1000000 ))
}

# Median of three, for the same reason as the near/far check above: one pass is
# noise. This check failed a run at small=234ms big=483ms, a ratio of 2.06
# against a 1.8 tolerance, on a tree where the positional indexes are present and
# working -- the two structural checks below passed in that same run. Both timing
# checks in this file are now medians.
#
# Know why this one's margin is thin, before widening it or tightening it. The
# fetches share a single decode of the group, and that decode is O(group size),
# so doubling the group legitimately adds close to double the one-time decode
# even when every per-row lookup is O(1). The defect signature (a walk, so about
# 2x) and the honest baseline therefore sit near each other by construction, and
# no tolerance separates them cleanly. Raising the group ratio does not help: it
# scales the decode with it.
#
# An earlier version of this comment said the way to get headroom was to raise
# the fetch count, so that per-fetch work dominates the shared decode. That was
# reasoned from a cost model and it is wrong. Measured, the ratio moves the other
# way: 1.08 at 6,000 fetches, 1.23 at 20,000, 1.29 at 40,000. Per-fetch cost is
# itself a function of group size, so more fetches amplify the difference rather
# than averaging it away.
#
# Those figures are from this file's fixture, which carries a varying-length
# column. A reviewer measured flat ~1.0 across the same fetch counts on a
# two-integer fixture, which is consistent rather than contradictory: a lighter
# per-group decode is exactly the condition under which the effect disappears.
# If this is ever remeasured, remeasure it on a fixture with a text column.
#
# What this check actually needed was not to be run beside five other suites;
# run_all_versions.sh now runs it alone. The two structural checks below are what
# prove the mechanism, and this one is corroboration.
fetch_ms() {  # table, table-rows, count -> median of three passes
	local a b c
	a=$(fetch_once "$1" "$2" "$3")
	b=$(fetch_once "$1" "$2" "$3")
	c=$(fetch_once "$1" "$2" "$3")
	printf '%s\n%s\n%s\n' "$a" "$b" "$c" | sort -n | sed -n 2p
}

build fp_small $((ROWS / 2))
build fp_big "$ROWS"

K=$((ROWS / 20))
check "premise: the doubled group's reads fetch through its index" \
	"$(reads_index "SELECT sum(v), sum(length(t)) FROM fp_big WHERE id > $(( ROWS - K ))" fp_big_id)" "yes"

small="$(fetch_ms fp_small $((ROWS / 2)) $K)"
big="$(fetch_ms fp_big "$ROWS" $K)"
echo "-- $K fetches at the far end, median of three: group of $((ROWS / 2)) took ${small} ms, group of $ROWS took ${big} ms"

check "doubling the row group does not double the cost of the same fetches" \
	"$(awk -v s="$small" -v b="$big" \
		'BEGIN { print (s > 0 && b / s < 1.8) ? "yes" : "no (small=" s "ms big=" b "ms)" }')" \
	"yes"

# --- 4. the indexes that make it O(1) are present ------------------------------

# The timing checks above are the real evidence. These pin the mechanism, so a
# future change that quietly reintroduces a walk fails by name rather than by a
# ratio drifting under whatever machine happens to run it.
#
# They read the source tree, not the built .so, and that difference is worth
# knowing: if these two pass while the timing checks fail, the library under test
# is older than the source beside it. That is not hypothetical -- it happened
# while writing this, when a copied tree carried mtimes older than the objects
# already built from it and make had nothing to do.
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/src"

# PIN THE PROPERTY, NOT THE CALLER COUNT (#1078's class). Both counted a string
# across the whole file against a literal 1. These two are EXISTENCE claims, so the
# direction they failed in is noise: a second honest use of either mechanism
# elsewhere in the reader would have reddened them while the property held. Scoped
# to the function the claim is about, which also says WHERE it must be true.
_nfp_row="$(awk '/^pgcolumnar_fetch_row\(/,/^}/' "$SRC/columnar_reader.c")"

check "premise: pgcolumnar_fetch_row was extracted, not an empty range" \
	"$([ -n "$_nfp_row" ] && echo yes || echo no)" "yes"

# `case`, not a pipe into `grep -q`: that exits on its first match and closes the
# pipe under its writer (#486), which selftest/080 refuses. The pattern is quoted so
# the brackets in valOffset[c][present] are literal rather than glob classes.
check "the rank comes from a prefix rather than a loop over earlier rows" \
	"$(case "$_nfp_row" in *'present = pgcolumnar_rank_before'*) echo yes ;; *) echo no ;; esac)" "yes"

check "a varying-length column reaches its value through an offset table" \
	"$(case "$_nfp_row" in *'entry->valOffset[c][present]'*) echo yes ;; *) echo no ;; esac)" "yes"

pgc_summary
