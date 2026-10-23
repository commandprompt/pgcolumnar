#!/usr/bin/env bash
#
# pgColumnar group-skipping reads only the predicate columns' zone maps.
#
# pgcolumnar_native_group_can_match read the WHOLE group's zone maps up front
# (PgColumnarReadZoneMapList, all attributes) and used only the columns carrying
# predicates. On a wide table that is min/max buffer traffic proportional to the
# table width, for a scan that consults one or two columns. The per-column bloom
# fetch already avoids exactly this; the zone map now does too, via a per-column
# probe (PgColumnarReadZoneMapForColumn) keyed on zone_map_pkey's column_index.
#
# The measurement is width-independent: a one-predicate scan reads the same number
# of zone_map tuples whether the table has 2 columns or 30, because it fetches only
# the predicate column's zone map. Before the change the wide table read about
# width-times more. The instrument is pg_stat_all_tables.idx_tup_fetch for
# pgcolumnar.zone_map, made readable with pg_stat_force_next_flush.
#
# Removal proof: reverting to the whole-group read makes the wide table's zone_map
# fetches scale with its column count, so the wide-vs-narrow check fails.
#
# Usage:  test/native_zonemap_narrow.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

q "CREATE EXTENSION IF NOT EXISTS pgcolumnar;" >/dev/null

wcols=""; wvals=""
for i in $(seq 1 30); do
	wcols="$wcols${wcols:+, }c$i int"
	wvals="$wvals${wvals:+, }g"
done
q "SET pgcolumnar.stripe_row_limit=2000;
   CREATE TABLE w ($wcols) USING pgcolumnar;
   INSERT INTO w SELECT $wvals FROM generate_series(1,20000) g;
   CREATE TABLE n (c1 int, c2 int) USING pgcolumnar;
   INSERT INTO n SELECT g, g FROM generate_series(1,20000) g;" >/dev/null

# zone_map tuples fetched by a one-predicate scan of TABLE, examining every group.
zm_fetch() {
	q "SELECT pg_stat_reset();" >/dev/null
	q "SELECT count(*) FROM $1 WHERE c1 >= 0;" >/dev/null
	q "SELECT pg_stat_force_next_flush();" >/dev/null
	q "SELECT coalesce(idx_tup_fetch,0) FROM pg_stat_all_tables WHERE relname='zone_map' AND schemaname='pgcolumnar';"
}

WF="$(zm_fetch w)"
NF="$(zm_fetch n)"
echo "-- zone_map idx_tup_fetch: wide(30 col)=$WF narrow(2 col)=$NF"

# Per-column: the 30-column table reads essentially the same zone_map tuples as
# the 2-column table (only c1's). Allow a small margin. Before the fix WF was
# about 15x NF (30 columns vs 2), so this fails on the whole-group read.
# Both readings must be numbers: an unread one is "no reading", never compared (#1341).
check "zone_map reads scale with predicates, not table width (wide ~= narrow)" \
	"$(if ! [[ "$WF" =~ ^[0-9]+$ && "$NF" =~ ^[0-9]+$ ]]; then echo "no reading (wide [$WF], narrow [$NF])"
	   elif [ "$WF" -le $((NF * 2)) ]; then echo yes; else echo no; fi)" "yes"

# Correctness: skipping unread columns' zone maps must not change results.
check "wide scan result is correct" "$(q 'SELECT count(*) FROM w WHERE c1 >= 0;')" "20000"
check "narrow scan result is correct" "$(q 'SELECT count(*) FROM n WHERE c1 >= 0;')" "20000"
# A pruning scan still prunes: c1 has values 1..20000 across 10 groups; c1 > 19000
# lives in the last group only.
check "a pruning predicate still prunes on the wide table" \
	"$(q 'SELECT count(*) FROM w WHERE c1 > 19000;')" "1000"

# The answer alone does not show a group was skipped: a scan that read the zone maps
# but never excluded a group, or never consulted them, returned the same 1000 rows,
# and main passed all four checks under each (#1341). The scan's own counter says how
# many of the ten groups the zone map removed: c1 > 19000 leaves only the last.
# removed TABLE PRED -> "Columnar Chunk Groups Removed by Filter", or "no reading".
removed() {
	local plan n
	plan="$(q "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) SELECT * FROM $1 WHERE $2;")"
	n="$(sed -n 's/.*Columnar Chunk Groups Removed by Filter: \([0-9]*\).*/\1/p' <<<"$plan" | head -1)"
	[ -n "$n" ] && echo "$n" || echo "no reading ($(head -1 <<<"$plan"))"
}
check "the zone map removes the nine groups c1 > 19000 cannot match, on the wide table" \
	"$(removed w 'c1 > 19000')" "9"
check "and on the narrow table" "$(removed n 'c1 > 19000')" "9"

pgc_summary
