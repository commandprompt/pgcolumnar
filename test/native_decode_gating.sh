#!/usr/bin/env bash
#
# pgColumnar: gate payload-column decode for UNPRUNABLE quals (#452 phase 2).
#
# Phase 1b-ii taught the skip vector to say "no row in this vector matches",
# but only for predicates the reader admits as SkipPredicates -- btree
# comparisons. A leading-wildcard LIKE ('%needle%') is never a SkipPredicate, so
# numPredicates is 0, refine_skipvec returns early, no mask is built, and the
# payload columns are decoded for EVERY vector no matter how few rows survive.
# That is the case ClickBench q21-q24 live in and the one #452 is named after.
#
# Phase 1a already evaluates this exact qual, but per ROW in the producer, AFTER
# both decode passes, so it saves materialization and never decode. Phase 2
# hoists that same executor-qual callback to group-load time and runs it per
# VECTOR between the two passes: a vector no row can pass is marked in
# nativeSkipVec, so pass 1 skips the payload columns' decode for it -- through
# the machinery 1b-i already built.
#
# The observable is "Columnar Vector Decodes", which counts (vector, column)
# decodes. "Columnar Vectors Decoded" counts vector POSITIONS and is a per-group
# MAX across columns, so it cannot see this: the qual column is decoded for every
# position either way, and the saving is entirely in the payload columns.
#
# A LIKE qual is never convertible to a scan key, so it is never fold-eligible;
# phase 2's mask writer therefore never runs on the vectorized-aggregate path,
# and there is no fold arm here. The path premise below asserts the LIKE query
# takes the late-materialization row path, or every count is measuring the wrong
# node.
#
# Usage:  test/native_decode_gating.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# #595 added a payload-WIDTH gate (pgcolumnar.qual_skipvec_min_payload_cols,
# default 20): phase-2 decode gating runs only once the projection has at least
# that many non-qual payload columns to spare from decode, because the per-vector
# evaluation does not pay on a narrow projection. THIS suite tests the gating
# MACHINERY, not the width policy, and its fixture projects five payload columns
# -- below the default -- so under the shipped default it would not gate and every
# count below would change. Pin the width gate OFF (0 = always gate, the pre-#595
# behaviour) so the machinery is exercised whatever the threshold. The width
# POLICY has its own suite, native_decode_gate_width.sh.
PGC_EXTRA_CONF="pgcolumnar.qual_skipvec_min_payload_cols=0"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

ROWS=32768
VECTORS=32				# ROWS / 1024
NCOLS=6					# tag plus five payload columns
SURVIVING_VECTORS=1		# the needle lives only in the first vector

psql_run "CREATE TABLE h (tag text, p1 int, p2 int, p3 int, p4 int, p5 int);"
psql_run "CREATE TABLE n (tag text, p1 int, p2 int, p3 int, p4 int, p5 int) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('n', stripe_row_limit => 65536, chunk_group_row_limit => 1024);"

# Every tag begins 'row' so LIKE '%row%' matches all rows (the control that must
# decode everything). Only the FIRST vector's 1024 rows also contain 'needle',
# so LIKE '%needle%' survives in exactly one vector and none of the other 31.
# The payload columns are the monotone integers 1b-ii uses, which produce the
# per-vector (D4) structure a per-vector skip needs.
GEN="SELECT 'row' || CASE WHEN g <= 1024 THEN 'needle' ELSE 'miss' END,
	 g, g + 1, g + 2, g + 3, g + 4 FROM generate_series(1, $ROWS) g"
psql_run "INSERT INTO h $GEN;"
psql_run "INSERT INTO n $GEN;"

explain_of() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) $1" 2>/dev/null
}
counter_in() { grep -F "$2" <<<"$1" | grep -oE '[0-9]+' | head -1; }
has_line() { grep -qF "$2" <<<"$1" && echo yes || echo no; }

# A leading-wildcard LIKE: unprunable by design. NEEDLE survives one vector,
# NOMATCH survives none, CONTROL survives all -- and all three are the same
# infix-LIKE shape, so they differ only in how many vectors hold a survivor.
NEEDLEQ="SELECT * FROM n WHERE tag LIKE '%needle%'"
NOMATCHQ="SELECT * FROM n WHERE tag LIKE '%zzzzz%'"
CONTROLQ="SELECT * FROM n WHERE tag LIKE '%row%'"

PLAN_NEEDLE="$(explain_of "$NEEDLEQ")"
PLAN_NOMATCH="$(explain_of "$NOMATCHQ")"
PLAN_CONTROL="$(explain_of "$CONTROLQ")"

# ---- premises --------------------------------------------------------------

check "the needle plan is a columnar custom scan" \
	"$(has_line "$PLAN_NEEDLE" 'Columnar Projected Columns')" "yes"
check "one row group, so every count below is one group's" \
	"$(q "SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = pgcolumnar.get_storage_id('n');")" \
	"1"
check "the width gate is pinned off, so this narrow projection still gates" \
	"$(q "SHOW pgcolumnar.qual_skipvec_min_payload_cols;")" "0"

# The whole point of the fixture is that this qual is NOT admitted to the reader.
# If a future #426 taught the reader to prune an infix LIKE, numPredicates would
# be non-zero, 1b-ii would do the ruling out, and this suite would pass without
# phase 2 existing. So assert the unprunable shape rather than assume it.
check "the reader has no usable skip predicate for this LIKE" \
	"$(counter_in "$PLAN_NEEDLE" 'Columnar Usable Skip Predicates')" "0"
check "no whole row group is pruned" \
	"$(counter_in "$PLAN_NEEDLE" 'Columnar Chunk Groups Removed by Filter')" "0"
check "no vector is ruled out by VALUE either (that is 1b-ii's path)" \
	"$(counter_in "$PLAN_NEEDLE" 'Columnar Vectors Ruled Out by Value')" "0"

# Phase 2 rides on 1a's callback, so 1a's path must be the one in use. Its
# counter is present only when late materialization is active, which is also what
# puts the qual on the scan with tag as a qual column. If this line is absent the
# qual is being applied somewhere else and no count below measures phase 2.
check "the needle query takes the late-materialization path" \
	"$(has_line "$PLAN_NEEDLE" 'Columnar Rows Filtered Before Materialization')" "yes"

# The fixture's shape, read from the heap mirror the same generator filled (#1339):
# read from the columnar table, a one-line reader change reddened these premises.
check "premise: the needle really matches one vector's worth of rows" \
	"$(q "SELECT count(*) FROM h WHERE tag LIKE '%needle%';")" "1024"
check "premise: the nomatch predicate matches no row" \
	"$(q "SELECT count(*) FROM h WHERE tag LIKE '%zzzzz%';")" "0"
check "premise: the control matches every row" \
	"$(q "SELECT count(*) FROM h WHERE tag LIKE '%row%';")" "$ROWS"

# ---- the observable, and its unit ------------------------------------------

check "EXPLAIN reports Columnar Vector Decodes" \
	"$(has_line "$PLAN_CONTROL" 'Columnar Vector Decodes')" "yes"

# Nothing can be ruled out when every row matches, so the control decodes every
# column of every vector. This pins the unit as (vector, column) pairs: a counter
# that quietly counted positions would fail here rather than make the fix checks
# below unfalsifiable. It passes on unfixed main too, which is correct -- the
# control is the arm the feature must NOT change.
check "the control decodes every column of every vector" \
	"$(counter_in "$PLAN_CONTROL" 'Columnar Vector Decodes')" "$(( VECTORS * NCOLS ))"

# ---- the fix (RED on main until phase 2 lands) -----------------------------
#
# Removal proof: delete the between-pass mask writer and these two go RED for the
# stated reason -- the payload columns are decoded for all 32 vectors again, so
# both counts return to VECTORS * NCOLS (192). "Fewer" is not asserted; the exact
# arithmetic is, so a mask that leaks one payload vector is caught.

# Zero survivors: the qual column is decoded in full (the selection is evaluated
# against it), and NOT ONE payload vector is decoded. This is parent-doc
# acceptance check 2 for the unprunable qual: SELECT * approaches count(*).
check "a zero-matching LIKE decodes the qual column and no payload column" \
	"$(counter_in "$PLAN_NOMATCH" 'Columnar Vector Decodes')" "$VECTORS"

# One surviving vector: qual column in full (32) plus the five payload columns
# for that single vector (5). A payload column decoded for any other vector would
# break this sum.
check "a one-vector LIKE decodes the qual column plus payload for that vector only" \
	"$(counter_in "$PLAN_NEEDLE" 'Columnar Vector Decodes')" \
	"$(( VECTORS + (NCOLS - 1) * SURVIVING_VECTORS ))"

# ---- the filter counters must stay EXACT -----------------------------------
#
# The evaluator that builds the mask also walks every row, so it counts each
# rejection. If it let the producer re-count the survivors' vector, "Rows Removed
# by Filter" would read high -- a real bug this suite must catch, not just the
# decode saving. Both counters equal the number of non-matching rows, whether a
# row was ruled out one at a time (the one surviving vector) or a whole vector at
# once. NEEDLE matches all of vector 0 (1024 rows), so it rejects the other 31.
MATCHES=1024
check "the needle removes exactly the non-matching rows, counted once" \
	"$(counter_in "$PLAN_NEEDLE" 'Rows Removed by Filter')" "$(( ROWS - MATCHES ))"
check "and reports them all filtered before materialization" \
	"$(counter_in "$PLAN_NEEDLE" 'Columnar Rows Filtered Before Materialization')" \
	"$(( ROWS - MATCHES ))"
check "the needle skips exactly the vectors holding no match" \
	"$(counter_in "$PLAN_NEEDLE" 'Columnar Vectors Skipped')" \
	"$(( VECTORS - SURVIVING_VECTORS ))"
check "the zero-match query removes every row by filter, counted once" \
	"$(counter_in "$PLAN_NOMATCH" 'Rows Removed by Filter')" "$ROWS"
check "and skips every vector" \
	"$(counter_in "$PLAN_NOMATCH" 'Columnar Vectors Skipped')" "$VECTORS"

# A SURVIVING VECTOR WITH REJECTS IN IT (#1341). Every row of the needle's
# surviving vector matches, so the producer, re-testing that vector to find its
# rows, finds nothing to reject: a producer that counted those rejections a second
# time read exactly right above. Measured, with the producer's non-counting filter
# disabled, this file stayed 26/26. Half of vector 0 passes this qual (p1 % 2 = 0 is
# an expression, so it is no skip predicate either), so its 512 rejections are
# counted twice by that mistake.
HALFQ="SELECT * FROM n WHERE tag LIKE '%needle%' AND p1 % 2 = 0"
PLAN_HALF="$(explain_of "$HALFQ")"
check "premise: the half-needle survives in one vector, half of its rows" \
	"$(q "SELECT count(*) || '|' || count(DISTINCT (p1 - 1) / 1024) FROM h WHERE tag LIKE '%needle%' AND p1 % 2 = 0;")" "512|1"
check "rejections inside a surviving vector are counted once" \
	"$(counter_in "$PLAN_HALF" 'Rows Removed by Filter')" "$(( ROWS - 512 ))"

# ---- correctness -----------------------------------------------------------
#
# The mask must never drop a row. Without these, an implementation that marks
# every vector skipped would pass the decode-count checks and return nothing.

check "the needle query returns exactly the matching rows" \
	"$(pgc_set_hash "SELECT * FROM n WHERE tag LIKE '%needle%'")" \
	"$(pgc_set_hash "SELECT * FROM h WHERE tag LIKE '%needle%'")"
check "the nomatch query returns nothing" \
	"$(q "SELECT count(*) FROM n WHERE tag LIKE '%zzzzz%';")" "0"
check "the control returns every row, values intact" \
	"$(q "SELECT count(*), sum(p1), sum(p5) FROM n WHERE tag LIKE '%row%';")" \
	"$(q "SELECT count(*), sum(p1), sum(p5) FROM h WHERE tag LIKE '%row%';")"

# ---- deleted rows across MULTIPLE groups (lifecycle regression) -------------
#
# The evaluator reads the delete mask at group-load time to keep deleted rows out
# of "does any row pass" and the reject tally. That mask must be the CURRENT
# group's. An earlier version built it AFTER the evaluator, so on the second group
# of a table with deletes the evaluator read the PREVIOUS group's mask -- freed by
# the group-context reset one load earlier. A use-after-free ASAN aborts on and
# this differential pins. It needs all three at once: more than one row group,
# deleted rows, and the unprunable qual; none of the single-group arms above
# reaches it. The heap mirror carries the identical deletes and is the oracle.
MROWS=49152
psql_run "CREATE TABLE hm (tag text, p1 int, p2 int);"
psql_run "CREATE TABLE m  (tag text, p1 int, p2 int) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('m', stripe_row_limit => 8192, chunk_group_row_limit => 1024);"
MGEN="SELECT 'x' || CASE WHEN g % 4096 = 7 THEN 'needle' ELSE 'miss' END || g::text, g, g * 2
      FROM generate_series(1, $MROWS) g"
psql_run "INSERT INTO hm $MGEN;"
psql_run "INSERT INTO m  $MGEN;"
psql_run "DELETE FROM hm WHERE p1 % 17 = 0;"
# The fixture's DELETE must not depend on the gating under test: its WHERE is an
# unprunable qual too, so it is run with the width gate out of reach (#1341).
psql_run "SET pgcolumnar.qual_skipvec_min_payload_cols = 100000; DELETE FROM m  WHERE p1 % 17 = 0;"

check "the columnar table spans more than one row group" \
	"$(q "SELECT count(*) > 1 FROM pgcolumnar.row_group WHERE storage_id = pgcolumnar.get_storage_id('m');")" \
	"t"
# count(*) rather than a count WHERE p1 % 17 = 0: that WHERE is an unprunable qual
# too, so it ran through the very gating under test, and a gating mutant broke this
# premise rather than the arms below (#1341).
check "rows really were deleted (so the mask is non-NULL)" \
	"$(q "SELECT count(*) FROM m;")" "$(( MROWS - MROWS / 17 ))"

check "deletes + multiple groups + unprunable qual: columnar matches the heap" \
	"$(pgc_set_hash "SELECT * FROM m  WHERE tag LIKE '%needle%'")" \
	"$(pgc_set_hash "SELECT * FROM hm WHERE tag LIKE '%needle%'")"
check "and the full scan across every group's mask matches too" \
	"$(pgc_set_hash "SELECT * FROM m")" \
	"$(pgc_set_hash "SELECT * FROM hm")"

# AND THE COUNTER SKIPS DELETED ROWS (#1341). The evaluator must not count a deleted
# row as filtered: it is invisible. Only the single-group table above had its
# counters checked, and it has no deletes, so with the evaluator's deleted-row test
# removed this file stayed 26/26.
PLAN_M="$(explain_of "SELECT * FROM m WHERE tag LIKE '%needle%'")"
check "rows removed by filter across deleted rows count only live rows" \
	"$(counter_in "$PLAN_M" 'Rows Removed by Filter')" \
	"$(q "SELECT count(*) FROM hm WHERE tag NOT LIKE '%needle%';")"

pgc_summary
