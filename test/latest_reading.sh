#!/usr/bin/env bash
#
# The "latest reading per device" recipe in docs/how-to.md does what it says (#1293).
#
# The recipe's SQL is READ FROM THE DOCUMENT, not copied here, so an edit to the
# document is what this suite runs: a recipe that stops returning the right rows,
# or stops being cheap, turns this red. The approach is jdatcmd's, from #1288.
#
# Measured on #1293 with 10,000,000 rows and 1,000 devices:
#
#     DISTINCT ON (device) ... ORDER BY device, ts DESC     17,204 blocks   5.6 s
#     GROUP BY device, max(ts)                               8,071 blocks   1.1 s  (with a projection)
#     a btree on (device, ts) + LATERAL over a device list   1,013 blocks   45 ms
#     the same LATERAL WITHOUT that btree                8,733,021 blocks  757 s
#
# What is asserted, at a size a suite can afford. The LATERAL form costs about a
# block per device, whatever the table's size, so the fixture needs many rows per
# device for "a tenth of the table" to be the claim it is at scale: at 5,000 rows
# per device it read 45 blocks of a 177-page table and failed; at 25,000 it is a
# tenth with room.
#
#   1. each documented form returns exactly the rows DISTINCT ON returns;
#   2. the btree + LATERAL form reads under a tenth of the table's blocks;
#   3. the trap the document warns about holds: without the btree the LATERAL form
#      reads the table once per device -- at least the table's blocks times half
#      the device count.
#
# Usage:  test/latest_reading.sh [PG_CONFIG]

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# at_least GOT FLOOR -- GOT when it falls short, else FLOOR; an empty GOT stays
# empty, which check_num refuses (selftest part 540).
at_least() { [ -z "$1" ] && return; [ "$1" -lt "$2" ] && echo "$1" || echo "$2"; }

DOC="$PGC_SRCDIR/docs/how-to.md"
# doc_sql N -> the Nth fenced sql block under "## Find the latest reading per device"
doc_sql() {
	awk -v want="$1" '
		/^## /            { inside = ($0 == "## Find the latest reading per device") }
		inside && /^```sql/ { n++; grab = (n == want); next }
		/^```/            { grab = 0; next }
		grab              { print }' "$DOC"
}
INDEX_SQL="$(doc_sql 1)"
GROUP_SQL="$(doc_sql 2)"
LATERAL_SQL="$(doc_sql 3)"
found=0
for b in "$INDEX_SQL" "$GROUP_SQL" "$LATERAL_SQL"; do [ -n "$b" ] && found=$((found + 1)); done
check_num "premise: the document's three recipe blocks were found" "$found" "3"

q "CREATE EXTENSION IF NOT EXISTS pgcolumnar;" >/dev/null
DEVICES=40
q "CREATE TABLE readings (device int, ts timestamptz, v float8) USING pgcolumnar;
   CREATE TABLE devices (device int PRIMARY KEY);
   INSERT INTO devices SELECT g FROM generate_series(1, $DEVICES) g;
   INSERT INTO readings
     SELECT 1 + (h % $DEVICES), '2026-05-01'::timestamptz + (h % 1000000) * interval '1 min', h % 977
     FROM (SELECT hashint4(g)::bigint & 2147483647 AS h FROM generate_series(1, 1000000) g) x;" >/dev/null
check_num "premise: the table holds every row" "$(q "SELECT count(*) FROM readings;")" "1000000"

ORACLE="$(q "SELECT DISTINCT ON (device) device, ts FROM readings ORDER BY device, ts DESC;" | md5sum | cut -c1-12)"

blocks() {	# blocks SQL -> readings blocks read by SQL, in its own session
	q "SELECT pg_stat_reset();" >/dev/null
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "$1" -c "SELECT pg_stat_force_next_flush()" >/dev/null 2>&1
	q "SELECT coalesce(heap_blks_read, 0) + coalesce(heap_blks_hit, 0)
	   FROM pg_statio_user_tables WHERE relname = 'readings';"
}
pages="$(q "SELECT pg_relation_size('readings') / 8192;")"

# The trap first, before the document's index exists.
trap_blocks="$(blocks "$LATERAL_SQL")"
echo "-- table $pages pages; LATERAL without the btree read $trap_blocks blocks"
check_num "without the btree the LATERAL form reads the table once per device" \
	"$( [ -n "$trap_blocks" ] && at_least "$trap_blocks" $((pages * DEVICES / 2)) )" "$((pages * DEVICES / 2))"

check "the GROUP BY form returns what DISTINCT ON returns" \
	"$(q "$GROUP_SQL" | md5sum | cut -c1-12)" "$ORACLE"
# The document says this form reads the table once (#1339): rows alone cannot tell it
# from a per-device subquery, which returns the same rows and reads the table once
# per device. Reading nothing is refused, as for the LATERAL form below.
grp_blocks="$(blocks "$GROUP_SQL")"
echo "-- table $pages pages; the GROUP BY form read $grp_blocks blocks"
check_num "the GROUP BY form reads the table once" \
	"$( [ -n "$grp_blocks" ] && [ "$grp_blocks" -ge 1 ] && at_least $((pages + 1 - grp_blocks)) 1 || echo "read ${grp_blocks:-nothing}" )" "1"

q "$INDEX_SQL" >/dev/null
check_num "premise: the document's index exists" \
	"$(q "SELECT count(*) FROM pg_indexes WHERE tablename = 'readings';")" "1"
check "the LATERAL form returns what DISTINCT ON returns" \
	"$(q "$LATERAL_SQL" | md5sum | cut -c1-12)" "$ORACLE"
lat_blocks="$(blocks "$LATERAL_SQL")"
echo "-- with the btree the LATERAL form read $lat_blocks blocks"
check_num "premise: the indexed LATERAL form read the table at all" \
	"$(at_least "$lat_blocks" 1)" "1"
# A query that reads NOTHING also reads under a tenth of the table: the red run
# before the document existed passed this arm on empty SQL. So reading at least
# one block is part of the claim, and a reading of zero is not a measurement.
check_num "with the btree the LATERAL form reads under a tenth of the table" \
	"$( [ -n "$lat_blocks" ] && [ "$lat_blocks" -ge 1 ] && at_least $((pages - 10 * lat_blocks)) 1 || echo "read ${lat_blocks:-nothing}" )" "1"

pgc_summary
