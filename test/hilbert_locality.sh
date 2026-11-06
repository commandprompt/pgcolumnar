#!/usr/bin/env bash
#
# pgColumnar Hilbert clustering: does the curve actually buy range locality?
# (issue #889, the measurement half.)
#
# WHAT THIS SUITE IS FOR
#
# test/hilbert_curve.sh pins the CURVE in C, by its mathematical properties and
# by frozen bytes. test/hilbert_cluster.sh pins the SQL SURFACE: the two verbs,
# the recorded kind, the gates and the daemon. Neither of them can tell whether
# laying a table on the Hilbert curve does the one thing the feature was added
# for -- put two-dimensionally near rows in the same row group, so that a
# two-dimensional range predicate has fewer groups to open.
#
# This file is that question and only that question. It builds one fixture,
# lays it out twice -- once with pgcolumnar.cluster() (Z-order) and once with
# pgcolumnar.cluster_hilbert() -- and counts the engine's own
# "Columnar Chunk Groups Read" over 60 window placements at each of four window
# sizes. The counts are PINNED AS EXACT INTEGERS.
#
# WHY EXACT INTEGERS AND NOT A THRESHOLD -- AND WHAT EACH INSTRUMENT CATCHES
#
# A threshold is the thing someone lowers when it reddens. "Hilbert reads at
# most 0.6x the groups Z-order does" survives a regression that costs half the
# benefit, and it survives it silently. An exact pin cannot be lowered without
# saying so, and it names which number moved.
#
# What an exact pin is NOT is automatically the sensitive instrument in this
# file. The eight integers were mutation-tested to find out what they, and only
# they, catch. Measured, not assumed:
#
#	- A CHANGED CURVE is caught by the DIGEST PINS in arm 2, about two hundred
#	  lines above the integers, and the integers add nothing to it. Two
#	  independently built, valid Hilbert re-orientations -- a point reflection
#	  inside cluster_hilbert_transpose, and a swap of the clustering axes
#	  before the transpose -- each moved the Hilbert digest AND all four
#	  Hilbert integers on the same run (h=117/202/403/1301 and
#	  h=130/208/409/1316 against pins of 118/209/402/1313). That is structural
#	  rather than a lucky choice of mutation: the counts are a deterministic
#	  function of the group boxes, which the digest hashes, of the 60 fixed
#	  origins, and of the skip logic. So for a changed curve the integers are a
#	  strictly weaker copy of a check the suite already makes upstream.
#	  Re-pinning arm 2 and arm 8 together after a deliberate curve change is
#	  right, but the digest is the arm that did the work.
#	- A CHANGED READER, AT AN UNCHANGED LAYOUT, is what only the integers
#	  catch. It is their own domain and it is why they are here. Refusing to
#	  skip odd-numbered row groups in src/columnar_reader.c left BOTH digests
#	  exactly at their pins and arms 1 and 3-7 green, and moved all eight
#	  integers (z=4144/4196/4317/4848, h=4083/4133/4228/4678).
#	- A CHANGED ROW MULTISET is NAMED by arm 1 and by nothing else. Loading the
#	  Hilbert arm from a differently seeded 200,000-row source over the same
#	  square moved the Hilbert digest, all four Hilbert integers (220/330/567/
#	  1603) and all four margins -- but not one of those arms says WHY, and a
#	  reader looking at moved integers would go hunting in the curve. Arm 1 is
#	  what says the two arms no longer hold the same rows. Its SECOND check is
#	  the one that survives the case where both loads went equally wrong:
#	  loading both arms with OFFSET 1 keeps "the identical row multiset" green
#	  and reddens "it is the source's multiset" and "both arms hold every
#	  source row" (399998 against 400000), while the measurement below refuses
#	  and the integers say nothing at all. So none of the three instruments is
#	  redundant to the others, and a reader who drops arm 1 or the digest pins
#	  as "already covered by the pins" is not covered.
#	- THE GROSS CASE -- the transpose gutted, so cluster_hilbert() silently
#	  lays Z-order -- is caught by arm 2 alone. The integers never execute:
#	  arm 8 refuses the measurement and prints sixteen UNRUN lines. A wall of
#	  UNMET_PRECONDITION here means the curve collapsed, not that the suite
#	  broke.
#
# The pins are the measurement, not a target -- if one moves, the correct
# response is to find out what moved it and then, if the new layout is better,
# re-pin with the new numbers beside the reason.
#
# WHY h < z IS NOT ENOUGH, AND WHAT THE MARGIN FLOOR IS FOR
#
# Alongside the pins, and separately, h < z is asserted AT EVERY BOX SIZE, and
# so is the MARGIN. An earlier version of this header told the reader that "the
# pins moved but h < z still holds" means the layout merely changed. THAT RULE
# WAS FALSE and it was reddened. The reader mutation above moves all eight
# integers, keeps h < z PASSING at every box, and takes z/h from 2.0424 to
# 1.0149 at box 2000: Hilbert won by 61 groups out of 4,144 and the suite
# printed PASS. A maintainer following the old rule would have re-pinned and
# accepted a change that took the feature's benefit from 2.04x to 1.01x.
#
# So h < z says only that Hilbert was not BEATEN. The per-box floor beside it
# says Hilbert still wins by the margin these numbers were measured at. Read
# the three arms together:
#
#	pins move, margin holds   the layout changed; find what moved it, re-pin.
#	margin fails              most of the benefit is gone, whatever h < z says.
#	h < z fails               HILBERT STOPPED WINNING, the worst of the three.
#
# WHAT THIS SUITE DOES NOT CLAIM
#
#	- NOT that Hilbert is faster. Nothing here is timed. The unit is groups
#	  read, which is a count the engine reports about its own work; wall clock
#	  on this hardware is not a fair instrument and is not used.
#	- NOT that Hilbert wins on every workload. It measures square windows over
#	  two uniformly distributed int columns. A one-dimensional predicate, a
#	  skewed distribution, a non-square window or a different column count is a
#	  different measurement and this suite says nothing about any of them.
#	- NOT that Hilbert wins on every fixture. Arm 4 below is a fixture where it
#	  provably does not, and it is in here as a control precisely so that the
#	  win reported by arms 8a-8d cannot be read as a universal one.
#	- NOT anything about the curve's mathematics or the SQL surface. Those are
#	  the two suites named above and this one does not repeat them.
#
# THREE DEFECTS THIS SHAPE EXISTS TO AVOID. Each was found in the pilot that
# produced these numbers, and each produced a plausible ratio while measuring
# something else:
#
#	1. THE TWO ARMS MUST HOLD THE IDENTICAL ROWS. The pilot ran a random()
#	   INSERT once per table, so the arms held DIFFERENT DATA and the ratio was
#	   a fact about the data rather than about the curve. Hence the heap table
#	   `src`, materialised once, from which both arms load -- and hence arm 1,
#	   which asserts the row multisets are equal rather than assuming that two
#	   INSERTs from one source produced one.
#
#	2. THE PARTITION DIGEST MUST BE ORDER-INDEPENDENT. The pilot's first digest
#	   ordered each column's min/max BY group_number, so it reported "the
#	   partitions differ" when the two curves had merely NUMBERED the same
#	   groups differently. Under that digest the dense dyadic control (arm 4)
#	   PASSED its premise and went on to report a ratio, which is exactly the
#	   case the control exists to catch. The digest below groups by
#	   group_number, builds one string per group from BOTH columns' min and max,
#	   and then sorts THOSE STRINGS before hashing. Two identical partitions
#	   hash equal however they are numbered.
#
#	3. A SINGLE WINDOW ORIGIN IS NOT A MEASUREMENT. At one origin the pilot's
#	   four differences were 1, 1, 0 and 1 groups, and the reported 2.000 ratio
#	   came off a single group. Hence 60 deterministic placements per box, and
#	   hence the assertion that all 60 were actually measured -- a grep that
#	   matched 3 lines and summed them would otherwise report a smaller total
#	   and read as a better result.
#
# WHY THE CONTROLS ARE NOT OPTIONAL
#
# Arms 8a-8d divide one number by another and call the quotient a benefit. That
# is only meaningful if the two arms differ IN THE LAYOUT and in nothing else,
# so the suite refuses to report the measurement at all unless arm 2 has
# established that the two partitions really are two different partitions. Arm 3
# proves that refusal can fire: two tables laid out by the SAME verb produce
# IDENTICAL digests, so the digest is reporting the curve rather than reporting
# that any two tables differ. Arm 4 is the degenerate fixture the design
# predicted -- a dense dyadic grid, where the two curves cut the same blocks --
# and there the digests are identical too, on real data, from the same
# instrument.
#
# Usage:  test/hilbert_locality.sh [PG_CONFIG]
# Written fresh for pgColumnar.
#
# THE PARAGRAPH THAT STOOD HERE WAS CARRIED OUT AND LEFT BEHIND (#1088). It said
# this suite is deliberately unregistered and makes harness_selftest's
# registration arm red until someone lands it in the matrix. Both halves were true
# when written and neither is now:
#
#     registered in test/run_all_versions.sh   yes
#     harness_selftest                         967 passed + 0 failed
#     070's registration arm                   PASS, every suite is registered
#
# The same shape as #1043 in hilbert_cluster.sh, and it matters more here: this is
# the suite the published clustering advice cites. `docs/features.md`,
# `docs/how-to.md` and `docs/best-practices.md` quote the 1.24x to 2.04x range
# against Z-order, and the pins below are where that range comes from. A reader
# arriving from the docs to check the number was told the file is not in the
# matrix and is expected to redden the selftest.
#
# A stale instruction is worse than a stale fact: it tells the next person to undo
# what was done.

set -uo pipefail

# Every digest below is a claim about the PHYSICAL group boundaries, and every
# group count is read out of a plan. A parallel plan divides that work across
# workers, so parallelism off is not a tuning choice: it is what makes the
# counters a fact about the layout rather than about scheduling.
export PGC_EXTRA_CONF="max_parallel_workers_per_gather=0"

. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# ---- the fixture's constants ------------------------------------------------

# 200,000 rows over a [0, 100000) square in two int columns.
ROWS=200000
SPAN=100000

# NOT DYADIC, ON PURPOSE. 200000/1500 is 133.33, so the last group is a short
# one and no group boundary lines up with a power of two. A dyadic stripe over a
# dyadic domain is the case where Z-order and Hilbert cut the same blocks and
# the measured difference collapses -- which is arm 4, kept as a control rather
# than allowed to become the main fixture by accident.
SR=1500

# The window placements. Two coprime strides over the span, so the 60 origins
# neither repeat nor march in step with the group boundaries, and the same 60
# are used for both arms and every box.
PLACEMENTS=60
STRIDE_X=997
STRIDE_Y=7919

# ---- the instruments --------------------------------------------------------

# WHAT COUNTS AS A MEASUREMENT. A digest helper that returns the empty string on
# a query that could not run lets two FAILED reads compare equal and pass an
# equality arm (#418); the same empty string satisfies an INEQUALITY arm
# outright, which is the shape that matters here because arm 2 is an inequality.
# So the helpers return sentinels and every comparison goes through differs().
pgc_measured() {	# pgc_measured VALUE -> 0 when VALUE is a real measurement
	case "$1" in
		'' | QUERY_ERROR.* | EMPTY | NO_PARTITION) return 1 ;;
	esac
	return 0
}

differs() {	# differs A B -> different | IDENTICAL | UNMEASURED[...]
	pgc_measured "$1" || { printf 'UNMEASURED[a=%s]\n' "$1"; return; }
	pgc_measured "$2" || { printf 'UNMEASURED[b=%s]\n' "$2"; return; }
	if [ "$1" != "$2" ]; then echo different; else echo IDENTICAL; fi
}

# THE PARTITION DIGEST, ORDER-INDEPENDENT BY CONSTRUCTION.
#
# One string per row group, built from BOTH clustering columns' min and max
# (ordered by column_index, so the two columns always appear in the same order
# within a group), and then those strings SORTED before hashing. What is hashed
# is therefore the SET of group boxes, and two layouts that cut the same boxes
# hash equal however the curve numbered them.
#
# Ordering the groups by group_number instead -- the obvious first version --
# makes the digest report the NUMBERING as well as the partition, so it says
# "different" for two identical partitions and the dense control below passes
# its premise and produces numbers. That is defect 2 in the header.
#
# vector_index = -1 is the row-group-level zone map: the box of the whole group,
# which is what decides whether the group can be skipped. The per-vector rows
# (vector_index >= 0) describe a finer unit and are not what a group count is
# about.
partition_digest() {	# partition_digest TABLE -> 12 hex chars, or NO_PARTITION
	local d
	d="$(q "SELECT substr(md5(string_agg(g, ',' ORDER BY g)), 1, 12) FROM (
		SELECT string_agg(column_index::text || ':' ||
		                  encode(minimum, 'hex') || ':' ||
		                  encode(maximum, 'hex'), '/' ORDER BY column_index) AS g
		FROM pgcolumnar.zone_map
		WHERE storage_id = pgcolumnar.get_storage_id('$1')
		  AND vector_index = -1 AND column_index IN (0, 1)
		GROUP BY group_number) t;")"
	printf '%s\n' "${d:-NO_PARTITION}"
}

# The number of row groups the partition digest was built over, from the
# catalog. Printed and asserted rather than assumed from ROWS/SR: set_options
# has a floor and a call below it RAISES, and a suite that discards the error
# then measures a fixture built on the defaults.
group_count() {	# group_count TABLE
	q "SELECT count(*) FROM pgcolumnar.row_group
	   WHERE storage_id = pgcolumnar.get_storage_id('$1');"
}

# A plan, with the run's own counters in it.
plan_of() {	# plan_of SQL
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "EXPLAIN (ANALYZE, TIMING OFF, COSTS OFF) $1" 2>&1
}

# One counter out of a plan. Empty when the line is absent, which check_num
# rejects rather than compares.
field() {	# field PLAN LABEL
	sed -n "s/.*$2: \([0-9]*\).*/\1/p" <<<"$1" | head -1
}

# The query under measurement. One shape, one place, so the arms and the
# premises cannot drift into asking about two different queries.
window_sql() {	# window_sql TABLE OX OY BOX
	printf 'SELECT count(*) FROM %s WHERE a BETWEEN %d AND %d AND b BETWEEN %d AND %d' \
		"$1" "$2" "$(($2 + $4))" "$3" "$(($3 + $4))"
}

# THE MEASUREMENT ITSELF: 60 placements, one psql session, two numbers out.
#
# Returns "LINES TOTAL": how many "Columnar Chunk Groups Read" lines the run
# actually produced, and their sum. Both are returned because the sum alone
# cannot distinguish a genuinely small number of groups from a grep that matched
# fewer statements than were sent -- a truncated run reads as a BETTER result,
# which is the direction a defect here would go undetected.
read_groups() {	# read_groups TABLE BOX -> "LINES TOTAL"
	local t="$1" box="$2" f i ox oy span out
	span=$((SPAN - box))
	f="$PGC_SQLDIR/loc.$t.$box.sql"
	: > "$f"
	for i in $(seq 1 "$PLACEMENTS"); do
		ox=$(( (i * STRIDE_X) % span ))
		oy=$(( (i * STRIDE_Y) % span ))
		printf 'EXPLAIN (ANALYZE, TIMING OFF, COSTS OFF) %s;\n' \
			"$(window_sql "$t" "$ox" "$oy" "$box")" >> "$f"
	done
	out="$(psql_file "$f")"
	printf '%s\n' "$out" | awk '
		/Columnar Chunk Groups Read:/ { n++; v = $NF; gsub(/[^0-9]/, "", v); s += v }
		END { print n+0, s+0 }'
}

# The SQLSTATE a statement raises, as a value.
#
# A CALL MADE WITH psql_run IS NOT AN ASSERTION. This suite runs under
# `set -uo pipefail` with no -e and psql_run's exit status is checked nowhere in
# this tree, so a cluster verb that RAISED would leave the table simply
# unclustered -- and an unclustered table has a partition digest, a group count
# and a plan, so it reddens the pins below as though the CURVE had changed.
# Every layout verb goes through crun(), which says which one raised and with
# what state.
#
# The sentinel statement behind the one under test is not decoration: without
# it, "no ERROR line was found" is also what a psql that never reached the
# server produces, so an unreachable probe reads as success.
sqlstate() {	# sqlstate SQL -> a five-character SQLSTATE, noerror, or PROBE_UNREACHABLE
	local out st
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -v VERBOSITY=sqlstate -v ON_ERROR_STOP=0 \
		-c "$1" -c "SELECT 'PGC_PROBE_OK';" 2>&1)"
	case "$out" in
		*PGC_PROBE_OK*) ;;
		*) echo PROBE_UNREACHABLE; return ;;
	esac
	st="$(printf '%s\n' "$out" | sed -n 's/^.*ERROR:[[:space:]]*\([0-9A-Z]\{5\}\).*$/\1/p' | head -1)"
	if [ -n "$st" ]; then printf '%s\n' "$st"; else echo noerror; fi
}

crun() {	# crun WHAT SQL
	check_text "$1 ran without raising" "$(sqlstate "$2")" "noerror"
}

# ---- the fixture ------------------------------------------------------------
#
# THE DATA IS MATERIALISED ONCE, INTO A HEAP TABLE, AND BOTH ARMS LOAD FROM IT.
# Running the generator once per arm would be two independent evaluations, and
# hashint8 is deterministic but a generator that were not -- random(), which is
# what the pilot used -- gives the two arms different rows and turns the ratio
# into a fact about the data. Loading from one materialised source removes the
# question rather than arguing about it, and arm 1 then checks it anyway.
psql_run "CREATE TABLE src (a int, b int, pad text);" > /dev/null
psql_run "INSERT INTO src (a, b, pad)
          SELECT (hashint8(g) % $SPAN + $SPAN) % $SPAN,
                 (hashint8(g * 2654435761) % $SPAN + $SPAN) % $SPAN,
                 repeat('x', 40)
          FROM generate_series(1, $ROWS) g;" > /dev/null
check_num "premise: the source holds the rows both arms will load" \
	"$(q 'SELECT count(*) FROM src;')" "$ROWS"

# The generator is only a fair two-dimensional fixture if it fills the square.
# A generator that collapsed one column to a handful of values would make both
# curves the identity in that dimension and the comparison meaningless, and it
# would do so silently.
check_num "premise: column a spans the square, so this is a two-dimensional fixture" \
	"$(q "SELECT (min(a) < $((SPAN / 100)) AND max(a) > $((SPAN - SPAN / 100)))::int FROM src;")" "1"
check_num "premise: column b spans the square too" \
	"$(q "SELECT (min(b) < $((SPAN / 100)) AND max(b) > $((SPAN - SPAN / 100)))::int FROM src;")" "1"

# hz: Z-order. hh: Hilbert. Identical in every other respect, and built by the
# same three statements in the same order.
for t in hz hh; do
	psql_run "CREATE TABLE $t (a int, b int, pad text) USING pgcolumnar;" > /dev/null
	psql_run "SELECT pgcolumnar.set_options('$t', stripe_row_limit => $SR);" > /dev/null
	psql_run "INSERT INTO $t SELECT * FROM src;" > /dev/null
done
crun "cluster() on the Z-order arm"          "SELECT pgcolumnar.cluster('hz', 'a', 'b');"
crun "cluster_hilbert() on the Hilbert arm"  "SELECT pgcolumnar.cluster_hilbert('hh', 'a', 'b');"

HZ_GROUPS="$(group_count hz)"
HH_GROUPS="$(group_count hh)"
echo "-- groups: hz=$HZ_GROUPS hh=$HH_GROUPS (stripe_row_limit=$SR over $ROWS rows)"

# =============================================================================
# ARM 1  PREMISE: THE TWO ARMS HOLD THE IDENTICAL ROW MULTISET
# =============================================================================
#
# Without this every number below is a fact about two different tables. The
# hash is order-blind by construction (pgc_set_hash sorts the rendered rows
# before hashing), which is exactly right here: the arms are supposed to differ
# in ORDER and in nothing else, so an order-SENSITIVE oracle would report a
# difference for the very property under test.
HZ_SET="$(pgc_set_hash 'SELECT * FROM hz')"
HH_SET="$(pgc_set_hash 'SELECT * FROM hh')"
SRC_SET="$(pgc_set_hash 'SELECT * FROM src')"
check_text "premise: the two arms hold the identical row multiset" "$HH_SET" "$HZ_SET"
# And that multiset is the source's. Comparing the arms only to each other
# passes if both loads went equally wrong.
check_text "premise: and it is the source's multiset, so neither load dropped rows" \
	"$HZ_SET" "$SRC_SET"
# A hash of nothing equals a hash of nothing. pgc_set_hash returns EMPTY for a
# genuinely empty relation and QUERY_ERROR.N for a read that failed, so this
# rejects both without having to trust that the two above compared anything.
check_num "premise: and it is a hash of rows, not of an empty or failed read" \
	"$(pgc_measured "$HZ_SET" && echo 1 || echo 0)" "1"
check_num "premise: both arms hold every source row" \
	"$(q 'SELECT (SELECT count(*) FROM hz) + (SELECT count(*) FROM hh);')" "$((ROWS * 2))"

# The fixture is only the fixture that was measured if the groups are the size
# they were measured at. 200,000 rows at 1,500 to a group is 134 groups, the
# last one short.
check_num "premise: hz has the group count this measurement was taken at" "$HZ_GROUPS" "134"
check_num "premise: hh has the same group count, so a group is the same unit on both arms" \
	"$HH_GROUPS" "$HZ_GROUPS"

# =============================================================================
# ARM 2  PREMISE: THE TWO PARTITIONS ARE TWO DIFFERENT PARTITIONS
# =============================================================================
#
# This is what licenses arms 8a-8d to attribute their difference to the curve.
# If it fails, the measurement is not reported: there is no curve difference to
# measure, and a ratio printed anyway would be a number about noise.
DZ="$(partition_digest hz)"
DH="$(partition_digest hh)"
echo "-- partition digests: zorder=$DZ hilbert=$DH"
check_text "the Z-order and Hilbert partitions differ" "$(differs "$DZ" "$DH")" "different"

# Pinned, not merely different. "Different" is satisfied by any change to
# either layout; these two values say the layouts are the ones the pinned
# integers below were measured over. A moved digest beside moved integers is a
# layout change; moved integers beside these digests would be a change in the
# reader.
check_text "the Z-order partition is the one these numbers were measured over" "$DZ" "2169ae4551d8"
check_text "the Hilbert partition is the one these numbers were measured over" "$DH" "1706e49ef5a2"

# =============================================================================
# ARM 3  CONTROL: SAME CURVE TWICE -> THE DIGESTS ARE IDENTICAL
# =============================================================================
#
# Arm 2 is an inequality, and an inequality passes for any instrument that
# reports "different" too readily -- including one that is really reporting
# "these are two different tables". This is the removal proof for that: two
# tables built the same way from the same source and laid out by the SAME verb
# must hash EQUAL. If this fails, arm 2 has proved nothing and neither has
# anything below it.
for t in cz1 cz2; do
	psql_run "CREATE TABLE $t (a int, b int, pad text) USING pgcolumnar;" > /dev/null
	psql_run "SELECT pgcolumnar.set_options('$t', stripe_row_limit => $SR);" > /dev/null
	psql_run "INSERT INTO $t SELECT * FROM src;" > /dev/null
	crun "cluster() on control table $t" "SELECT pgcolumnar.cluster('$t', 'a', 'b');"
done
CZ1="$(partition_digest cz1)"
CZ2="$(partition_digest cz2)"
echo "-- control digests, both Z-order: $CZ1 $CZ2"
check_text "control: two tables on the same curve have the identical partition" \
	"$(differs "$CZ1" "$CZ2")" "IDENTICAL"
# And it is the same partition the measured Z-order arm has, so the control is
# a control ON THIS FIXTURE rather than on an unrelated one.
#
# THROUGH differs(), LIKE EVERY OTHER DIGEST COMPARISON IN THIS FILE. Comparing
# $CZ1 with $DZ directly was the one exception, and the exception was a hole:
# check_text refuses an EMPTY expectation, but NO_PARTITION is not empty, so two
# FAILED digest reads compared equal and this arm printed PASS. Measured: with
# partition_digest() pointed at a storage_id that does not exist, every other
# digest arm reddened with UNMEASURED[a=NO_PARTITION] and this one was green.
check_text "control: and that partition is the measured Z-order arm's" \
	"$(differs "$CZ1" "$DZ")" "IDENTICAL"

# =============================================================================
# ARM 4  CONTROL: A DENSE DYADIC GRID -> THE TWO CURVES AGREE
# =============================================================================
#
# The design predicted the degenerate case: over a dense grid whose side is a
# power of two, cut into groups whose size is a power of two, Z-order and
# Hilbert visit the same blocks in a different ORDER and therefore produce the
# same set of group boxes. This is that case, on real data, through the same
# digest -- so the suite carries its own counterexample to "Hilbert always
# changes the layout", and a reader can see that arm 2's "different" is a
# measurement rather than a foregone conclusion.
#
# 256 x 256 = 65,536 rows at 1,024 to a group is 64 groups, all full.
DENSE_SIDE=256
DENSE_SR=1024
psql_run "CREATE TABLE dsrc (a int, b int);" > /dev/null
psql_run "INSERT INTO dsrc SELECT (g - 1) / $DENSE_SIDE, (g - 1) % $DENSE_SIDE
          FROM generate_series(1, $((DENSE_SIDE * DENSE_SIDE))) g;" > /dev/null
for t in dz dh; do
	psql_run "CREATE TABLE $t (a int, b int) USING pgcolumnar;" > /dev/null
	psql_run "SELECT pgcolumnar.set_options('$t', stripe_row_limit => $DENSE_SR);" > /dev/null
	psql_run "INSERT INTO $t SELECT * FROM dsrc;" > /dev/null
done
crun "cluster() on the dense control"         "SELECT pgcolumnar.cluster('dz', 'a', 'b');"
crun "cluster_hilbert() on the dense control" "SELECT pgcolumnar.cluster_hilbert('dh', 'a', 'b');"
check_num "control premise: the dense grid is dense -- every cell present exactly once" \
	"$(q "SELECT count(*) FROM (SELECT a, b FROM dsrc GROUP BY a, b HAVING count(*) <> 1) x;")" "0"
check_num "control premise: the dense grid has the dyadic group count" \
	"$(group_count dz)" "$(( DENSE_SIDE * DENSE_SIDE / DENSE_SR ))"
DDZ="$(partition_digest dz)"
DDH="$(partition_digest dh)"
echo "-- dense grid digests: zorder=$DDZ hilbert=$DDH"
check_text "control: on a dense dyadic grid the two curves cut the identical partition" \
	"$(differs "$DDZ" "$DDH")" "IDENTICAL"

# =============================================================================
# ARMS 5-7  PREMISES ABOUT THE PLAN THE COUNTERS COME OUT OF
# =============================================================================
#
# The counters below are the columnar scan's own instrumentation. Three things
# have to hold before a difference in them is a difference in LAYOUT:
#
#	5. the query planned as a columnar scan on BOTH arms -- a fallback would
#	   read no groups at all and report nothing to sum;
#	6. the predicates were pushed down AND USABLE on both arms. "Pushed-Down
#	   Filters" counts what the reader was handed; a key it could not build a
#	   skip predicate from excludes no group at all while still being reported
#	   as pushed down (#477). Usable is the number that says skipping is
#	   possible, so a zero here would mean both arms read everything and the
#	   ratio would be 1.0 for a reason that is not about the curve;
#	7. the two arms have the same number of groups TO read. A ratio between two
#	   different denominators is not a ratio.
BOXES="2000 5000 12000 30000"

PROBE_SQL_HZ="$(window_sql hz $((STRIDE_X % (SPAN - 2000))) $((STRIDE_Y % (SPAN - 2000))) 2000)"
PROBE_SQL_HH="$(window_sql hh $((STRIDE_X % (SPAN - 2000))) $((STRIDE_Y % (SPAN - 2000))) 2000)"
# PARALLELISM OFF IS ASSERTED, NOT ONLY CONFIGURED. PGC_EXTRA_CONF puts it in the
# server's config above; this reads it back on the same kind of connection every
# arm runs through. A parallel plan divides the scan across workers, and the group
# counters below would be a fact about scheduling. Same name as the twin's check,
# which has to assert it because a pytest session SETs it (#1339 :733).
check_text "parallelism is off on the connection every arm runs through" \
	"$(q "SHOW max_parallel_workers_per_gather;")" "0"

check_text "premise: the Z-order arm plans as a columnar scan" \
	"$(pgc_is_columnar_scan "$PROBE_SQL_HZ")" "yes"
check_text "premise: the Hilbert arm plans as a columnar scan" \
	"$(pgc_is_columnar_scan "$PROBE_SQL_HH")" "yes"

for box in $BOXES; do
	ox=$(( STRIDE_X % (SPAN - box) ))
	oy=$(( STRIDE_Y % (SPAN - box) ))
	pz="$(plan_of "$(window_sql hz "$ox" "$oy" "$box")")"
	ph="$(plan_of "$(window_sql hh "$ox" "$oy" "$box")")"
	uz="$(field "$pz" 'Columnar Usable Skip Predicates')"
	uh="$(field "$ph" 'Columnar Usable Skip Predicates')"
	tz="$(field "$pz" 'Columnar Chunk Groups Total')"
	th="$(field "$ph" 'Columnar Chunk Groups Total')"
	echo "-- box $box: usable z=$uz h=$uh; total z=$tz h=$th"
	# FOUR, NOT TWO, AND THE FOUR IS THE POINT. A BETWEEN is two scan keys,
	# >= and <=, and the reader builds a skip predicate from each; two BETWEENs
	# over two columns are therefore four usable predicates. This arm was
	# written expecting 2, and the plan said 4 (measured on PG 18.4). A "> 0"
	# bound would have accepted either and would also accept an arm that had
	# lost one whole dimension -- which is the dimension the curve is about --
	# so the exact count is what is pinned.
	check_num "premise: box $box, the Z-order arm can skip on all four predicates" "$uz" "4"
	check_num "premise: box $box, the Hilbert arm can skip on all four predicates" "$uh" "4"
	# Read from the plan on both arms and compared to each other. Not compared
	# to a number typed in here: a literal would still be satisfied if BOTH
	# arms drifted, and the property is that the denominators match.
	check_num "premise: box $box, both arms have the same number of groups to read" "$th" "$tz"
	# And the denominator is the fixture's, not a plan that saw a fraction of it.
	check_num "premise: box $box, that denominator is the whole relation" "$tz" "$HZ_GROUPS"
done

# =============================================================================
# ARM 8  THE MEASUREMENT
# =============================================================================
#
# Reported only if arm 2 established a curve difference. This is the refusal the
# header describes: with no difference in layout there is nothing for a ratio to
# be about, and printing one anyway is how the pilot's first version produced
# numbers for the dense grid.
#
# AND THE REFUSAL IS WHAT ANSWERS THE GROSS CASE. With the Hilbert transpose
# gutted, so that cluster_hilbert() lays Z-order, arm 2 reddens twice and every
# check below refuses: sixteen UNRUN lines and "39 passed + 2 failed + 16
# unrunnable = 57", the run exiting 1 because a failure outranks an incomplete.
# A reader who sees that wall of UNMET_PRECONDITION is looking at a collapsed
# curve, not at a broken suite, and the two failures naming it are arm 2's.
CURVE_DIFFERS="$(differs "$DZ" "$DH")"

# The pins, and the margin floor beside each one. Measured on main f2af080,
# PG 18.4, identical on two consecutive runs of the fixture above, and
# reproduced from a clean tree on 2026-09-09. box:zorder:hilbert:floor.
#
# THE FLOOR IS NOT THE THRESHOLD THIS FILE'S HEADER REJECTS. The exact integers
# are still the primary detector; the floor is deliberately slack -- about 88%
# of the measured ratio -- so that it names a COLLAPSE of the benefit rather
# than tracking a layout that moved. It is here because h < z on its own is
# satisfied by a win of one group in four thousand, which is what a reader-only
# regression produces. Measured z/h at these pins: 2.0424, 1.6794, 1.4627,
# 1.2369.
#
# The slack was chosen against both mutation families and both are recorded so
# the next person can re-derive it. The reader regression takes z/h to 1.0149,
# 1.0152, 1.0211 and 1.0363, which is below every floor. The two valid curve
# re-orientations keep it at 1.8538 to 2.0598, 1.6875 to 1.7376, 1.4376 to
# 1.4590 and 1.2340 to 1.2483, which is above every floor -- so a layout that
# genuinely moved reddens the PINS and leaves this arm green, and the two arms
# say different things about the same run.
PINS="2000:241:118:1.80 5000:351:209:1.48 12000:588:402:1.28 30000:1624:1313:1.10"

for pin in $PINS; do
	IFS=: read -r box want_z want_h floor <<<"$pin"

	if [ "$CURVE_DIFFERS" != different ]; then
		check_unrunnable "box $box: groups read over $PLACEMENTS placements, Z-order" UNMET_PRECONDITION \
			"the two partitions are not different ($CURVE_DIFFERS), so a ratio between them is not about the curve"
		check_unrunnable "box $box: groups read over $PLACEMENTS placements, Hilbert" UNMET_PRECONDITION \
			"the two partitions are not different ($CURVE_DIFFERS), so a ratio between them is not about the curve"
		check_unrunnable "box $box: Hilbert reads fewer groups than Z-order" UNMET_PRECONDITION \
			"the two partitions are not different ($CURVE_DIFFERS)"
		check_unrunnable "box $box: and it wins by the margin measured, z/h at least $floor" \
			UNMET_PRECONDITION \
			"the two partitions are not different ($CURVE_DIFFERS)"
		continue
	fi

	rz="$(read_groups hz "$box")"
	rh="$(read_groups hh "$box")"
	nz="${rz%% *}"; sz="${rz##* }"
	nh="${rh%% *}"; sh="${rh##* }"

	# EVERY PLACEMENT WAS MEASURED. A run that lost statements would sum fewer
	# groups and read as a better result on whichever arm lost them.
	check_num "box $box: all $PLACEMENTS placements reported a Z-order group count" "$nz" "$PLACEMENTS"
	check_num "box $box: all $PLACEMENTS placements reported a Hilbert group count" "$nh" "$PLACEMENTS"

	# The pins.
	check_num "box $box: groups read over $PLACEMENTS placements, Z-order" "$sz" "$want_z"
	check_num "box $box: groups read over $PLACEMENTS placements, Hilbert" "$sh" "$want_h"

	# And, separately from the pins: the curve still wins here. This is the arm
	# that stays meaningful when the pins are re-taken.
	check_text "box $box: Hilbert reads fewer groups than Z-order" \
		"$(awk -v a="$sh" -v b="$sz" 'BEGIN { print (a + 0 < b + 0) ? "fewer" : "NOT FEWER" }')" \
		"fewer"

	# AND IT STILL WINS BY THE MARGIN IT WAS MEASURED AT.
	#
	# h < z above is satisfied by a win of ONE group, so on its own it cannot
	# tell "the layout changed" from "the benefit is gone" -- which is what this
	# file's header used to tell a reader it could. Refusing to skip
	# odd-numbered row groups in the reader keeps h < z green at every box, moves
	# all eight pins, and takes z/h from 2.0424 to 1.0149. This arm is what
	# reddens there, and it is the removal proof for the h < z arm's meaning.
	#
	# The ratio is computed ONCE, here, and both this arm and the line printed
	# below use that one value, so the number asserted and the number reported
	# cannot drift. A zero Hilbert total yields 0 and fails the floor rather than
	# dividing by zero.
	ratio="$(awk -v a="$sz" -v b="$sh" 'BEGIN { printf "%.4f", (b + 0 == 0) ? 0 : a / b }')"
	check_text "box $box: and it wins by the margin measured, z/h at least $floor" \
		"$(awk -v r="$ratio" -v f="$floor" \
			'BEGIN { print (r + 0 >= f + 0) ? "at or above the floor" : "BELOW THE FLOOR (z/h=" r ")" }')" \
		"at or above the floor"

	# The ratio is PRINTED as well, and asserted nowhere beyond the floor above.
	# A ratio is the readable form of the result; it is not the pin, because a
	# ratio can be held constant by both arms getting worse together.
	echo "-- box $box: z=$sz h=$sh  z/h=$ratio"
done

pgc_summary
