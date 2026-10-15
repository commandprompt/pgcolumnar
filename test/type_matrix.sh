#!/usr/bin/env bash
#
# Types the differential matrix never stored, against heap (#1341). The encoder has
# fast paths for fixed widths 1, 2, 4 and 8; every other width and every varlena
# takes a generic path, and a zone map is built for each column whose type has a
# btree ordering. None of these types had been written to a columnar table:
#
#   - fixed width, by reference, at unusual widths: timetz (12), macaddr (6),
#     macaddr8 (8), name (64), point (16, no btree ordering);
#   - the 1-byte "char";
#   - time, money, inet, cidr, bit, varbit, tsvector, xml, json (no equality);
#   - an enum, a composite, a domain over int;
#   - numeric NaN and +-Infinity, timestamp and date +-infinity.
#
# 12000 rows in row groups of 1000, each column NULL on its own pattern. The same
# rows go into heap and are copied to columnar. Then: every row, as text; each
# column's count and NULL count; a range on every ordered column, with constants
# read from heap so the zone maps can prune; equality on json (as text) and point
# (~=); and the aggregates over the special values.
#
# Usage:  test/type_matrix.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

psql_run "CREATE TYPE tm_mood AS ENUM ('sad', 'ok', 'happy', 'elated');
          CREATE TYPE tm_pair AS (a int, b text);
          CREATE DOMAIN tm_pos AS int CHECK (VALUE > 0);"
COLS="id int, tz timetz, mac macaddr, mac8 macaddr8, nm name, pt point, ch \"char\",
      tm time, mo money, ip inet, cd cidr, bt bit(16), vb varbit, tsv tsvector, x xml,
      j json, e tm_mood, cmp tm_pair, dom tm_pos, n numeric, tsi timestamp, d date"
# xml needs a server built with libxml. Without it the load below fails and every
# arm after it would fail for that reason, so the suite says so and stops
# (OffgridwithJD, review of #1426).
xml_ok="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -XAtq \
	-c "SELECT '<a/>'::xml IS NOT NULL" 2>&1 | tail -1)"	# 2>&1, so a missing libxml is named
check "premise: the server supports xml (built with libxml)" "$([ "$xml_ok" = t ] && echo yes || echo "no ($xml_ok)")" "yes"
[ "$xml_ok" = t ] || pgc_summary
make_pair "$COLS"
q "SELECT pgcolumnar.set_options('t_col', stripe_row_limit => 1000)" >/dev/null
# g % k = 0 makes the column NULL; each column has its own k.
load_pair "SELECT g,
  CASE WHEN g % 7 = 0 THEN NULL ELSE ((('00:00'::time + (g * 7) * interval '1 s')::text || '+' || lpad((g % 13)::text, 2, '0'))::timetz) END,
  CASE WHEN g % 11 = 0 THEN NULL ELSE ('08:00:2b:' || lpad(to_hex(g / 256 % 256), 2, '0') || ':' || lpad(to_hex(g % 256), 2, '0') || ':01')::macaddr END,
  CASE WHEN g % 13 = 0 THEN NULL ELSE ('08:00:2b:01:02:03:' || lpad(to_hex(g / 256 % 256), 2, '0') || ':' || lpad(to_hex(g % 256), 2, '0'))::macaddr8 END,
  CASE WHEN g % 17 = 0 THEN NULL ELSE ('name_' || lpad(g::text, 6, '0'))::name END,
  CASE WHEN g % 19 = 0 THEN NULL ELSE point(g, -g) END,
  CASE WHEN g % 23 = 0 THEN NULL ELSE chr(65 + g % 26)::\"char\" END,
  CASE WHEN g % 29 = 0 THEN NULL ELSE ('00:00'::time + g * interval '7 s') END,
  CASE WHEN g % 31 = 0 THEN NULL ELSE (g * 1.25)::numeric::money END,
  CASE WHEN g % 37 = 0 THEN NULL ELSE ('10.' || g / 256 % 256 || '.' || g % 256 || '.' || g % 7 || '/32')::inet END,
  CASE WHEN g % 41 = 0 THEN NULL ELSE ('10.' || g / 256 % 256 || '.' || g % 256 || '.0/24')::cidr END,
  CASE WHEN g % 43 = 0 THEN NULL ELSE g::bit(16) END,
  CASE WHEN g % 47 = 0 THEN NULL ELSE (g::bit(16) || (g % 7)::bit(3))::varbit END,
  CASE WHEN g % 53 = 0 THEN NULL ELSE to_tsvector('simple', 'word' || g || ' common term' || g % 9) END,
  CASE WHEN g % 59 = 0 THEN NULL ELSE ('<r id=\"' || g || '\">' || md5(g::text) || '</r>')::xml END,
  CASE WHEN g % 61 = 0 THEN NULL ELSE json_build_object('g', g, 's', md5(g::text)) END,
  CASE WHEN g % 67 = 0 THEN NULL ELSE (enum_range(NULL::tm_mood))[1 + g % 4] END,
  CASE WHEN g % 71 = 0 THEN NULL ELSE ROW(g, 'p' || g)::tm_pair END,
  CASE WHEN g % 73 = 0 THEN NULL ELSE (g)::tm_pos END,
  CASE WHEN g % 79 = 0 THEN NULL WHEN g % 1000 = 1 THEN 'NaN'::numeric WHEN g % 1000 = 2 THEN 'Infinity'::numeric
       WHEN g % 1000 = 3 THEN '-Infinity'::numeric ELSE g / 7.0 END,
  CASE WHEN g % 83 = 0 THEN NULL WHEN g = 500 THEN 'infinity'::timestamp WHEN g = 501 THEN '-infinity'::timestamp
       ELSE '2026-01-01'::timestamp + g * interval '1 min' END,
  CASE WHEN g % 89 = 0 THEN NULL WHEN g = 600 THEN 'infinity'::date WHEN g = 601 THEN '-infinity'::date
       ELSE '2026-01-01'::date + g END
  FROM generate_series(1, 12000) g"

check "premise: both tables hold 12000 rows, the columnar one in at least twelve row groups" \
	"$(q "SELECT count(*) FROM t_heap")|$(q "SELECT count(*) FROM t_col")|$(q "SELECT CASE WHEN count(*) >= 12 THEN 'many' ELSE count(*)::text END
	        FROM pgcolumnar.row_group WHERE storage_id = pgcolumnar.get_storage_id('t_col')" 2>/dev/null | tail -1)" "12000|12000|many"

ALL="id, tz::text, mac::text, mac8::text, nm::text, pt::text, ch::text, tm::text, mo::text, ip::text, cd::text,
     bt::text, vb::text, tsv::text, x::text, j::text, e::text, cmp::text, dom::text, n::text, tsi::text, d::text"
diff_query "every row, as text, is heap's" "SELECT $ALL FROM %T"
parts=()
for c in tz mac mac8 nm pt ch tm mo ip cd bt vb tsv x j e cmp dom n tsi d; do
	parts+=("count($c), count(*) FILTER (WHERE $c IS NULL)")
done
COUNTS="$(IFS=,; echo "${parts[*]}")"
diff_query "each column's count and NULL count is heap's" "SELECT $COUNTS FROM %T"

# A range on every ordered column. The bounds are the values heap holds at ids 3005
# and 9005, as literals, so the columnar scan can prune row groups by zone map. Not
# 3001 and 9001: id 3001 holds n's NaN, which made n's "range" NaN..NaN (OffgridwithJD,
# review of #1426). Neither id is NULL in any column.
#
# Each arm also says whether the zone maps skipped chunk groups, from EXPLAIN ANALYZE,
# so an arm that should prune cannot pass on a scan that read everything. Four measure
# 0 of 12 on PG17 and PG18, and are pinned at 0 with the reason:
declare -A NO_PRUNE=(
	[ch]="every row group holds every value, since the column cycles through 26 letters"
	[e]="every row group holds every value, since the column cycles through the 4 labels"
	[n]="every row group holds NaN and both infinities, so each zone map spans every value"
	[cd]="a cidr key is treated as cross-type against inet and dropped (#1433)")
removed() {	# removed SQL -> the columnar scan's "Chunk Groups Removed by Filter"
	q "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) ${1//%T/t_col}" |
		sed -n 's/.*Columnar Chunk Groups Removed by Filter: \([0-9]*\).*/\1/p' | head -1
}
for c in tz mac mac8 nm ch tm mo ip cd bt vb e dom n tsi d; do
	# format_type keeps the typmod: pg_typeof would make bt's bounds bit(1)
	# (OffgridwithJD, review of #1426).
	ty="$(q "SELECT format_type(atttypid, atttypmod) FROM pg_attribute WHERE attrelid = 't_heap'::regclass AND attname = '$c'" | tail -1)"
	lo="$(q "SELECT quote_literal($c::text) || '::' || '$ty' FROM t_heap WHERE id = 3005" | tail -1)"
	hi="$(q "SELECT quote_literal($c::text) || '::' || '$ty' FROM t_heap WHERE id = 9005" | tail -1)"
	sql="SELECT id FROM %T WHERE $c BETWEEN least($lo, $hi) AND greatest($lo, $hi)"
	diff_query "a range on $c finds heap's rows" "$sql"
	r="$(removed "$sql")"
	if [ -n "${NO_PRUNE[$c]:-}" ]; then
		check "a range on $c skips no chunk groups: ${NO_PRUNE[$c]}" "${r:-none}" "0"
	else
		check "a range on $c lets the zone maps skip chunk groups" \
			"$([ -n "$r" ] && [ "$r" -ge 1 ] && echo skips || echo "removed ${r:-none}")" "skips"
	fi
done
diff_query "equality on json, as text, finds heap's rows" \
	"SELECT id FROM %T WHERE j::text = (SELECT j::text FROM t_heap WHERE id = 4242)"
diff_query "point equality (~=) finds heap's rows" "SELECT id FROM %T WHERE pt ~= point(4242, -4242)"
diff_query "a tsvector match finds heap's rows" "SELECT id FROM %T WHERE tsv @@ to_tsquery('simple', 'term3')"
diff_query "the aggregates over numeric NaN and Infinity and timestamp and date infinity are heap's" \
	"SELECT min(n)::text, max(n)::text, count(*) FILTER (WHERE n = 'NaN'), count(*) FILTER (WHERE n = 'Infinity'),
	        count(*) FILTER (WHERE n = '-Infinity'), min(tsi)::text, max(tsi)::text, min(d)::text, max(d)::text FROM %T"
diff_query "the composite's fields and the enum's order read back as heap's" \
	"SELECT (cmp).a, (cmp).b, e, e > 'ok' FROM %T WHERE id % 97 = 0"

pgc_summary
