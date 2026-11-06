#!/usr/bin/env bash
#
# pgcolumnar.stats() and pgcolumnar.sort_status() must read a table's storage id
# once, not once per row of the catalog they filter (#1637).
#
# THE BUG. Both filtered a shared catalog with
#
#     WHERE x.storage_id = pgcolumnar.get_storage_id(rel)
#
# get_storage_id is STABLE, and each call reads the table's first page. As an
# index condition it is evaluated once. In the filter of a sequential scan it is
# evaluated once per catalog row, so the call is charged for every other
# columnar table in the database. stats() filters pgcolumnar.row_group, one row
# per row group of every table; sort_status() filters pgcolumnar.storage, one
# row per table, three times. Which plan the planner picks depends on the
# catalog's statistics, so the cost came and went with autovacuum (#1614 is the
# same defect in the rollup functions).
#
# Measured on main, PG17, after ANALYZE, with 20 other tables of 5 groups each
# (102 row groups, 22 storage rows): stats('t') read 103 blocks of t and
# sort_status('t') 172. One get_storage_id call reads 1.
#
# WHAT IS ASSERTED. The blocks of t this backend fetched inside the call's own
# transaction (pg_stat_get_xact_blocks_fetched), so no other backend's reads
# are counted. The bound, 5, is far below one read per catalog row (22 or more
# here) and above the one or two reads a single call costs. Premises show that
# the catalog is large enough and has statistics, and that the counter counts.
# Controls show each function still reports only t's groups.
#
# Usage:  test/catalog_filter_reads.sh [PG_CONFIG]

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

OTHERS=20
CEILING=5

# at_least GOT FLOOR -> GOT when it falls short of FLOOR, else FLOOR; at_most
# GOT CEILING -> GOT when it is over CEILING, else CEILING. A check
# comparing this with CEILING passes exactly when GOT <= CEILING, and a failing
# one reports the number it measured. A GOT that is not a number is passed
# through unchanged, so check_num refuses it rather than reading it as in bound.
at_least() { pgc_is_number "$1" || { echo "$1"; return; }; [ "$1" -lt "$2" ] && echo "$1" || echo "$2"; }
at_most() { pgc_is_number "$1" || { echo "$1"; return; }; [ "$1" -gt "$2" ] && echo "$1" || echo "$2"; }

# reads SQL -> the blocks of t this backend fetched while SQL ran, in SQL's own
# transaction and a fresh session. A failed SQL leaves an error as the last
# line, which is not a number.
reads() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-X -qAt -v ON_ERROR_STOP=1 -c BEGIN -c "$1" \
		-c "SELECT pg_stat_get_xact_blocks_fetched('t'::regclass)" -c COMMIT 2>&1 | tail -1
}

q "CREATE EXTENSION IF NOT EXISTS pgcolumnar;" >/dev/null
q "CREATE TABLE t (a int, b int) USING pgcolumnar;
   SELECT pgcolumnar.set_options('t', stripe_row_limit => 1000);
   INSERT INTO t SELECT g, g FROM generate_series(1, 3000) g;" >/dev/null
for n in $(seq 1 "$OTHERS"); do
	q "CREATE TABLE o$n (a int) USING pgcolumnar;" >/dev/null
	# five statements, five row groups
	for i in 1 2 3 4 5; do q "INSERT INTO o$n SELECT g FROM generate_series(1, 10) g;" >/dev/null; done
done
q "ANALYZE;" >/dev/null

check_num "premise: other tables put at least 100 row groups in the catalog" \
	"$(at_least "$(q "SELECT count(*) FROM pgcolumnar.row_group;")" 100)" "100"
check_text "premise: pgcolumnar.row_group and pgcolumnar.storage have statistics" \
	"$(q "SELECT count(DISTINCT tablename) FROM pg_stats
	      WHERE schemaname = 'pgcolumnar' AND tablename IN ('row_group', 'storage');")" "2"
check_num "premise: one get_storage_id call reads t" \
	"$(at_least "$(reads "SELECT pgcolumnar.get_storage_id('t')")" 1)" "1"

got="$(reads "SELECT count(*) FROM pgcolumnar.stats('t')")"
echo "-- stats('t') read $got blocks of t"
check_num "stats() reads the storage id once, not once per catalog row group" \
	"$(at_most "$got" "$CEILING")" "$CEILING"
check_num "control: stats() reports t's 3 row groups and no other table's" \
	"$(q "SELECT count(*) FROM pgcolumnar.stats('t');")" "3"

got="$(reads "SELECT * FROM pgcolumnar.sort_status('t')")"
echo "-- sort_status('t') read $got blocks of t"
check_num "sort_status() reads the storage id once, not once per catalog row" \
	"$(at_most "$got" "$CEILING")" "$CEILING"
check_num "control: sort_status() counts t's 3 row groups and no other table's" \
	"$(q "SELECT total_groups FROM pgcolumnar.sort_status('t');")" "3"

pgc_summary
