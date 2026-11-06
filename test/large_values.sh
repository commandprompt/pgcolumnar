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
# A chunk over 1 GB is past what one allocation may hold. An opt-in arm
# (PGC_RUN_OVER_1GB=1, about 1.1 GB of incompressible text in one chunk group) checks
# that the INSERT is refused with 54000, commits nothing, and leaves the server up.
# It is opt-in because it needs several GB of memory, more than a CI runner should
# spend on one arm.
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

# ---- over 1 GB in one chunk: refused cleanly (opt-in) ---------------------------------
OVER="a chunk over 1 GB is refused with 54000, commits nothing, and the server stays up"
if [ "${PGC_RUN_OVER_1GB:-0}" = 1 ]; then
	psql_run "CREATE TABLE lv_huge (id int, big text) USING pgcolumnar;"
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At \
		-v VERBOSITY=sqlstate -c "SET statement_timeout = '900s'; SET pgcolumnar.compression = none;" \
		-c "INSERT INTO lv_huge SELECT k, $(mb 1 "k' || k || '") FROM generate_series(1, 1100) k;" 2>&1)"
	st="$(sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\).*/\1/p' <<<"$out" | tail -1)"
	check "$OVER" "${st:-committed}|$(q "SELECT count(*) FROM lv_huge" | tail -1)|$(q "SELECT 1" | tail -1)" "54000|0|1"
else
	check_skip "$OVER" "SKIP  $OVER (set PGC_RUN_OVER_1GB=1; it needs several GB of memory)" "opt-in heavy arm"
fi

pgc_summary
