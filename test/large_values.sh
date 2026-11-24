#!/usr/bin/env bash
#
# Megabyte-scale values are stored exactly (#1341). A columnar table has no TOAST
# table: the writer detoasts every value and stores it inline in the value stream.
# The largest value any test stored was 70 KB.
#
# Each value goes into a heap table first and is copied to a columnar one, and the
# two must agree on each value's length and md5:
#   - 1 MB and two 16 MB incompressible values (md5 text), and a 200 MB value that
#     compresses to almost nothing;
#   - an UPDATE that takes a value heap has TOASTed and stores it in a columnar row.
#
# Rows whose column passes 1 GB in one row group, or in one chunk group, load and
# are stored exactly (#2071), also when they are copied from heap, which stores them
# compressed. The writer closes a row group early instead of building a buffer past
# what one allocation may hold, so the 8 KB rows make row groups just under 256 MiB. An opt-in arm (PGC_RUN_OVER_1GB=1)
# does the same with about 1.1 GB of incompressible text and compression off, so no
# codec makes the data smaller. Before #2071 that INSERT failed with 54000. It is
# opt-in because it needs several GB of memory, more than a CI runner should spend
# on one arm.
#
# Usage:  test/large_values.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

mb() {	# mb N SALT -> an SQL expression for N MB of incompressible text
	echo "(SELECT string_agg(md5(g::text || '$2'), '') FROM generate_series(1, $1 * 32768) g)"
}
DIGEST="SELECT string_agg(id || ':' || length(big) || ':' || md5(big), ' ' ORDER BY id) FROM"

psql_run "CREATE TABLE lv_heap (id int, big text);
          CREATE TABLE lv (id int, big text) USING pgcolumnar;
          INSERT INTO lv_heap VALUES (1, $(mb 1 a)), (2, $(mb 16 b)), (3, $(mb 16 c)),
                                     (4, repeat('z', 200 * 1024 * 1024));"
check "premise: heap holds a 1 MB, two 16 MB and a 200 MB value" \
	"$(q "SELECT string_agg(length(big)::text, ',' ORDER BY id) FROM lv_heap")" "1048576,16777216,16777216,209715200"
check "the values copied into a columnar table are stored exactly, by length and md5" \
	"$(psql_run "INSERT INTO lv SELECT * FROM lv_heap;" >/dev/null 2>&1; q "$DIGEST lv")" "$(q "$DIGEST lv_heap")"

# ---- an UPDATE from a TOASTed heap value ----------------------------------------------
psql_run "UPDATE lv_heap SET big = big || 'x' WHERE id = 2;"
check "premise: heap stores the new 16 MB value TOASTed, not inline" \
	"$(q "SELECT pg_column_size(big) < length(big) OR pg_column_size(big) > 8192 FROM lv_heap WHERE id = 2")" "t"
check "an UPDATE that takes a TOASTed heap value stores it exactly in the columnar row" \
	"$(psql_run "UPDATE lv SET big = h.big FROM lv_heap h WHERE lv.id = h.id AND h.id = 2;" >/dev/null 2>&1
	   q "$DIGEST lv WHERE id = 2")" "$(q "$DIGEST lv_heap WHERE id = 2")"
check "and the other rows are unchanged" "$(q "$DIGEST lv WHERE id <> 2")" "$(q "$DIGEST lv_heap WHERE id <> 2")"

# ---- many rows whose column passes 1 GB per row group or chunk group (#2071) ---------
# The writer used to cut row groups and chunk groups by row count alone. With the
# default limits, 150000 rows of 8 KB, or 10000 rows of 120 KB, put 1.2 GB of one
# column into one buffer, and the INSERT failed with "string buffer exceeds maximum
# allowed length". The values compress to almost nothing, so this costs little disk.
lv_load() {	# lv_load SQL -> psql's command tag, or the first line of its error
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X \
		-c "$1" 2>&1 | head -1
}
LVSUM="SELECT count(*) || ',' || sum(id) || ',' || sum(length(t)) || ',' || count(*) FILTER (WHERE t <> repeat"
check "premise: the row limits are the defaults, 150000 per row group and 10000 per chunk group" \
	"$(q "SELECT current_setting('pgcolumnar.stripe_row_limit') || ',' || current_setting('pgcolumnar.chunk_group_row_limit')")" \
	"150000,10000"
psql_run "CREATE TABLE lv_rg (id int, t text) USING pgcolumnar;
          CREATE TABLE lv_cg (id int, t text) USING pgcolumnar;"
check "150000 rows of 8 KB text, 1.2 GB for one row group, load and are stored exactly" \
	"$(lv_load "INSERT INTO lv_rg SELECT g, repeat('y', 8000) FROM generate_series(1, 150000) g")|$(q "$LVSUM('y', 8000)) FROM lv_rg")" \
	"INSERT 0 150000|150000,11250075000,1200000000,0"
# A row of 8 KB measures 8012 bytes (8000, a 4-byte header, a 4-byte int), and
# 33504 x 8012 = 268434048 is the most rows under the 256 MiB cap (268435456). So
# this shape pins the cap's value, and that the byte count goes back to 0 after
# each flush: without that, every row group after the first holds one row.
check "the 8 KB rows make 5 row groups of 15984 to 33504 rows: the cap closes each one just under 256 MiB" \
	"$(q "SELECT count(*) || ',' || min(rowcount) || ',' || max(rowcount) FROM pgcolumnar.stats('lv_rg')")" \
	"5,15984,33504"
check "10000 rows of 120 KB text, 1.2 GB for one chunk group, load and are stored exactly" \
	"$(lv_load "INSERT INTO lv_cg SELECT g, repeat('x', 120000) FROM generate_series(1, 10000) g")|$(q "$LVSUM('x', 120000)) FROM lv_cg")" \
	"INSERT 0 10000|10000,50005000,1200000000,0"
# The same 1.2 GB copied from heap, which stores each 8 KB value compressed to about
# 100 bytes. The writer detoasts every value before it encodes it, so the cap must
# count a value at its raw size, not at the size it is stored at in the source.
psql_run "CREATE TABLE lv_hz (id int, t text);
          INSERT INTO lv_hz SELECT g, repeat('y', 8000) FROM generate_series(1, 150000) g;
          CREATE TABLE lv_hc (id int, t text) USING pgcolumnar;"
check "premise: heap stores the 150000 values of 8 KB compressed, under 20 MB in all" \
	"$(q "SELECT sum(pg_column_size(t)) < 20000000 FROM lv_hz")" "t"
check "150000 rows of 8 KB text that heap stores compressed, copied to columnar, load and are stored exactly" \
	"$(lv_load "INSERT INTO lv_hc SELECT * FROM lv_hz")|$(q "$LVSUM('y', 8000)) FROM lv_hc")" \
	"INSERT 0 150000|150000,11250075000,1200000000,0"
psql_run "DROP TABLE lv_rg, lv_cg, lv_hz, lv_hc;"

# ---- 1.1 GB of incompressible text in one chunk group's rows (opt-in) -----------------
OVER="1.1 GB of incompressible text in one chunk group's rows loads, and the server stays up"
if [ "${PGC_RUN_OVER_1GB:-0}" = 1 ]; then
	psql_run "CREATE TABLE lv_huge (id int, big text) USING pgcolumnar;"
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At \
		-v VERBOSITY=sqlstate -c "SET statement_timeout = '900s'; SET pgcolumnar.compression = none;" \
		-c "INSERT INTO lv_huge SELECT k, $(mb 1 "k' || k || '") FROM generate_series(1, 1100) k;" 2>&1)"
	st="$(sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\).*/\1/p' <<<"$out" | tail -1)"
	check "$OVER" "${st:-committed}|$(q "SELECT count(*) || ',' || sum(length(big)) FROM lv_huge" | tail -1)|$(q "SELECT 1" | tail -1)" \
		"committed|1100,1153433600|1"
else
	check_skip "$OVER" "SKIP  $OVER (set PGC_RUN_OVER_1GB=1; it needs several GB of memory)" "opt-in heavy arm"
fi

pgc_summary
