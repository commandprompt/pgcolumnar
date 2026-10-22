#!/usr/bin/env bash
#
# pgColumnar: exact selection feeds back into the skip vector (#452 phase 1b-ii).
#
# Phase 1b-i taught decode to honour the skip vector, but that vector is built
# from zone maps alone: it says "min/max could not rule this vector out", never
# "no row in it actually matches". So the case #452 opens with is untouched by
# it. A predicate that matches ZERO rows while lying inside every vector's
# min/max rules out nothing, and all 105 columns of ClickBench are decoded to
# produce no rows at all.
#
# The predicate has to be a RANGE, and finding that out is half of what this
# suite records. The issue's own example is an EQUALITY on an absent value
# (clienttimezone = 3600), and that case is ALREADY SOLVED: the per-chunk bloom
# filter prunes the whole row group, measured here as
# "Chunk Groups Removed by Filter: 1" with nothing decoded at all. A suite built
# on it would prove nothing about decode, because decode never runs.
#
# Bloom filters answer equality and nothing else, so a range that falls in a gap
# between stored values reaches the case #452 is actually about: z holds only
# multiples of ten, and the predicate asks for 51..59. Every vector's min/max
# brackets that range, so zone maps are useless by construction, and the
# premises below assert that rather than hope for it -- if anything ever did
# prune this, the suite would be measuring 1b-i again and would pass without
# 1b-ii existing.
#
# The observable is "Columnar Vector Decodes", which counts (vector, column)
# decodes. That unit is the point. "Columnar Vectors Decoded" counts vector
# POSITIONS, so it cannot see this fix at all: the qual column is still decoded
# for every position, and the saving is entirely in the payload columns.
#
# Usage:  test/native_exact_selection.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

ROWS=32768
VECTORS=32				# ROWS / 1024
NCOLS=6					# z plus five payload columns

psql_run "CREATE TABLE h (z int, p1 int, p2 int, p3 int, p4 int, p5 int);"
psql_run "CREATE TABLE n (z int, p1 int, p2 int, p3 int, p4 int, p5 int) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('n', stripe_row_limit => 65536, chunk_group_row_limit => 1024);"

# z holds only multiples of ten and spans 0..990 inside every single vector,
# because it cycles every 100 rows and a vector is 1024 rows. So no vector's zone
# map can exclude 51..59: min <= 51 and max >= 59 hold everywhere.
GEN="SELECT (g % 100) * 10, g, g + 1, g + 2, g + 3, g + 4 FROM generate_series(1, $ROWS) g"
psql_run "INSERT INTO h $GEN;"
psql_run "INSERT INTO n $GEN;"

explain_of() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) $1" 2>/dev/null
}
counter_in() { grep -F "$2" <<<"$1" | grep -oE '[0-9]+' | head -1; }
has_line() { grep -qF "$2" <<<"$1" && echo yes || echo no; }

# The zero-matching query, and an all-matching one as the control. Both project
# every column and both are pushed down, so they differ only in how many rows
# actually satisfy the predicate.
ZEROQ="SELECT * FROM n WHERE z BETWEEN 51 AND 59"
ALLQ="SELECT * FROM n WHERE z >= 0"

PLAN_ZERO="$(explain_of "$ZEROQ")"
PLAN_ALL="$(explain_of "$ALLQ")"

# ---- the ground --------------------------------------------------------------

check "the zero-match plan is a columnar custom scan" \
	"$(has_line "$PLAN_ZERO" 'Columnar Projected Columns')" "yes"
check "one row group, so every count below is one group's" \
	"$(q "SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = pgcolumnar.get_storage_id('n');")" \
	"1"
# The fixture premise reads the heap mirror h, which pgcolumnar does not write; the
# row-group count reads the writer's catalog, so a writer that reads stripe_row_limit
# wrong (one line) reddens it, and it is not a premise.
check "premise: the predicate really matches no row" \
	"$(q 'SELECT count(*) FROM h WHERE z BETWEEN 51 AND 59;')" "0"
check "and the control matches every row" \
	"$(q 'SELECT count(*) FROM n WHERE z >= 0;')" "$ROWS"

# The whole point of the fixture. If zone maps prune here, this is the 1b-i case
# and the checks below would pass without exact selection existing at all.
# "Columnar Vectors Skipped" is the TOTAL, and since exact selection writes into
# the same mask it no longer says which mechanism did the ruling out. So the
# premise is stated as the subtraction: every vector skipped here was ruled out
# by value, therefore the zone maps ruled out none. Asserting the total is 0
# would have been the obvious phrasing and it fails the moment the feature works.
# Both counters must be read as numbers before they are subtracted. An unread one
# made the arithmetic fail, and the check then recorded nothing at all: with the
# custom scan off by default, the accounting read 15 checks, not 16.
zm_skipped="$(counter_in "$PLAN_ZERO" 'Columnar Vectors Skipped')"
zm_byvalue="$(counter_in "$PLAN_ZERO" 'Columnar Vectors Ruled Out by Value')"
check "ZONE MAPS RULE OUT NOTHING, which is what makes this 1b-ii" \
	"$(if [[ "$zm_skipped" =~ ^[0-9]+$ && "$zm_byvalue" =~ ^[0-9]+$ ]]; then echo $(( zm_skipped - zm_byvalue ));
	   else echo "no reading (skipped [$zm_skipped], by value [$zm_byvalue])"; fi)" \
	"0"
check "no whole row group is pruned either" \
	"$(counter_in "$PLAN_ZERO" 'Columnar Chunk Groups Removed by Filter')" "0"

# ---- the observable --------------------------------------------------------

check "EXPLAIN reports Columnar Vector Decodes" \
	"$(has_line "$PLAN_ALL" 'Columnar Vector Decodes')" "yes"

# The control decodes every column of every vector: nothing can be ruled out
# when every row matches. This pins the unit as (vector, column) pairs rather
# than positions, so a counter that quietly counted positions would fail here
# rather than silently make the next check unfalsifiable.
check "the control decodes every column of every vector" \
	"$(counter_in "$PLAN_ALL" 'Columnar Vector Decodes')" "$(( VECTORS * NCOLS ))"

# ---- the fix ---------------------------------------------------------------
#
# Exactly the qual column, and nothing else. Not "fewer": a payload column that
# was decoded for even one vector would be a mask that leaks, and the arithmetic
# is what catches that. The qual column must still be decoded in full, because
# the values are what the selection is evaluated against.
check "exact selection rules out every vector, none of them by zone map" \
	"$(counter_in "$PLAN_ZERO" 'Columnar Vectors Ruled Out by Value')" "$VECTORS"

check "a zero-matching predicate decodes the qual column and no payload column" \
	"$(counter_in "$PLAN_ZERO" 'Columnar Vector Decodes')" "$VECTORS"

# The control must rule out nothing by value: every row matches it. Without this
# the check above passes against an implementation that marks vectors
# unconditionally, which would also return no rows for every query.
check "and the control rules out no vector by value" \
	"$(counter_in "$PLAN_ALL" 'Columnar Vectors Ruled Out by Value')" "0"

# ---- correctness -----------------------------------------------------------

check "the zero-matching query returns nothing" \
	"$(q 'SELECT count(*) FROM n WHERE z BETWEEN 51 AND 59;')" "0"

# Recorded as a check rather than a comment, because it is the reason this suite
# uses a range at all: the equality form of the same question never reaches
# decode, so a future edit that "simplifies" the predicate back to z = 51 would
# silently stop testing anything.
check "the EQUALITY form is pruned by bloom and reaches no decode" \
	"$(counter_in "$(explain_of 'SELECT * FROM n WHERE z = 51')" 'Columnar Chunk Groups Removed by Filter')" \
	"1"
check "a selective matching predicate still returns the right rows" \
	"$(pgc_set_hash 'SELECT * FROM n WHERE z = 50')" \
	"$(pgc_set_hash 'SELECT * FROM h WHERE z = 50')"
check "and a partially matching range does too" \
	"$(pgc_set_hash 'SELECT * FROM n WHERE z BETWEEN 100 AND 220')" \
	"$(pgc_set_hash 'SELECT * FROM h WHERE z BETWEEN 100 AND 220')"
# Every vector holds every z value, so the ranges above match many rows per vector,
# and an exact selection whose comparison was off by one at the bound (>= taken as
# >, or <= as <) still found a match in each vector and ruled out nothing it
# shouldn't: main passed 16/16 under both. Here the only matching rows sit ON both
# bounds, so such a selection rules out every vector and the rows go missing.
check "a range whose only matches lie on its bounds still returns them" \
	"$(pgc_set_hash 'SELECT * FROM n WHERE z BETWEEN 990 AND 990')" \
	"$(pgc_set_hash 'SELECT * FROM h WHERE z BETWEEN 990 AND 990')"
check_text "the control returns every row" \
	"$(q 'SELECT count(*), sum(p1) FROM n WHERE z >= 0;')" \
	"$(q 'SELECT count(*), sum(p1) FROM h WHERE z >= 0;')"

pgc_summary
