#!/usr/bin/env bash
#
# pgColumnar online reclustering (Phase F3c). pgcolumnar.recluster() re-establishes
# global Z-order clustering over several columns ONLINE, under
# ShareUpdateExclusiveLock (concurrent reads and writes), unlike the eager
# cluster() which holds AccessExclusiveLock. This suite proves it tightens the
# multi-column zone maps just as eager clustering does (a 2D box skips far more
# groups after), results match a heap mirror, indexes are maintained online (an
# index scan returns each live row exactly once), and the lock is
# ShareUpdateExclusiveLock, never AccessExclusiveLock.
#
# Usage:  test/native_recluster.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# 40960 rows in 20 row groups; x and y scattered in insert order, so before
# reclustering a small 2D box skips nothing.
GEN="SELECT g,
       ((g::bigint * 7919) % 200)::int   AS x,
       ((g::bigint * 104729) % 200)::int AS y,
       'p' || (g % 97)                    AS payload
  FROM generate_series(1, 40960) g"

psql_run "CREATE TABLE h (id int, x int, y int, payload text);"
psql_run "CREATE TABLE n (id int, x int, y int, payload text) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('n', stripe_row_limit => 2048, chunk_group_row_limit => 1024);"
psql_run "INSERT INTO h $GEN;"
psql_run "INSERT INTO n $GEN;"
psql_run "CREATE UNIQUE INDEX n_id_uq ON n (id);"

skipped() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) $1" 2>/dev/null \
		| grep 'Columnar Chunk Groups Removed by Filter' | grep -oE '[0-9]+' | head -1
}
# The count runs as an INDEX-ONLY scan of n_id_uq, with bitmap scans off too, and
# idxplan prints that plan (#1341). With only seqscan and the custom scan off, the
# count came back right with the index dropped, through the disabled seqscan.
# An index-only scan is also what can see the visibility-map rule recluster
# follows: the old index entries stay until a vacuum, and an index-only scan
# answers for one without a heap fetch wherever its VM bit is still set.
IDX_GUCS="SET enable_seqscan=off; SET enable_bitmapscan=off; SET pgcolumnar.enable_custom_scan=off;"
IDX_Q="SELECT count(*) FROM n WHERE id BETWEEN 1 AND 40960"
idxcount() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At 2>/dev/null -c "$IDX_GUCS $IDX_Q;" | tail -1
}
idxplan() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At 2>/dev/null -c \
		"$IDX_GUCS EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF, BUFFERS OFF) $IDX_Q;"
}

BOX="x BETWEEN 20 AND 40 AND y BETWEEN 20 AND 40"

check "row count" "$(q 'SELECT count(*) FROM n;')" "40960"
check "box parity (pre-recluster)" \
	"$(pgc_set_hash "SELECT id, x, y, payload FROM n WHERE $BOX")" \
	"$(pgc_set_hash "SELECT id, x, y, payload FROM h WHERE $BOX")"

# The node, before any counter read out of it: the skip counts below are
# reported by the columnar custom scan and by nothing else.
check "the plan under test is a columnar custom scan" \
	"$(pgc_is_columnar_scan "SELECT id FROM n WHERE $BOX")" "yes"

before="$(skipped "SELECT id FROM n WHERE $BOX")"; before="${before:-0}"

# VACUUM first, so the visibility map is set over the groups recluster replaces:
# without it every VM bit is clear, an index-only scan fetches every row from the
# heap, and recluster's VM clear is never needed.
psql_run "VACUUM n;"
# Most bits, not all: measured, 511 of the 40960 rows still take a heap fetch.
pre_fetches="$(idxplan | grep -oE 'Heap Fetches: [0-9]+' | grep -oE '[0-9]+')"
check "VACUUM set the visibility map, so the index-only count answers from the index before the recluster" \
	"$([ -n "$pre_fetches" ] && [ "$pre_fetches" -lt 20480 ] && echo yes || echo "no (${pre_fetches:-no} heap fetches for 40960 rows)")" "yes"

# Online recluster by (x, y).
rec="$(q "SELECT pgcolumnar.recluster('n', 'x', 'y');")"
echo "  (recluster processed $rec groups; box skip before=$before)"
check "reclustered the groups" "$([ "$rec" -gt 0 ] && echo yes || echo no)" "yes"

after="$(skipped "SELECT id FROM n WHERE $BOX")"; after="${after:-0}"
echo "  (box groups skipped: before=$before after=$after)"
check "reclustering increases 2D skipping" "$([ "$after" -gt "$before" ] && echo yes || echo no)" "yes"
check "reclustering skips most groups" "$([ "$after" -ge 10 ] && echo yes || echo no)" "yes"

# Results unchanged.
check "full-table parity after recluster" \
	"$(pgc_set_hash 'SELECT id, x, y, payload FROM n ORDER BY id')" \
	"$(pgc_set_hash 'SELECT id, x, y, payload FROM h ORDER BY id')"
check "row count after recluster" "$(q 'SELECT count(*) FROM n;')" "40960"

# Online index maintenance: index scan returns each live row exactly once. The old
# entries are still in the index, so a recluster that left their VM bits set would
# count them too.
post_plan="$(idxplan)"
echo "-- index count plan after the recluster:"; sed 's/^/     /' <<<"$post_plan"
check "premise: the count takes an index-only scan of n_id_uq" \
	"$(grep -c 'Index Only Scan using n_id_uq on n' <<<"$post_plan")" "1"
check "index scan returns each row once" "$(idxcount)" "40960"
# THE SQLSTATE, NOT THE EXIT CODE. This arm was
# `psql ... && echo no || echo yes`, which reports "yes" for every failure psql can
# have. Measured on the identical expression: a missing table, a wrong port, psql
# absent from PATH and a syntax error all produced "yes", so the arm passed with the
# unique index doing nothing. 23505 is ERRCODE_UNIQUE_VIOLATION and comes from the
# index; 42P01, 42601 and a connection failure do not. The project already reads
# SQLSTATE this way in `arrow_import.sh` and `audit.sh`.
dup_sqlstate() {	# -> the SQLSTATE of a duplicate insert, or empty if it succeeded
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -qtA -v ON_ERROR_STOP=0 -v VERBOSITY=sqlstate \
		-c "INSERT INTO n VALUES (1, 0, 0, 'x');" 2>&1 \
		| sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\).*/\1/p' | head -1
}
check "unique still enforced, by SQLSTATE 23505 and not by any failure" \
	"$(dup_sqlstate)" "23505"

# Lock level: ShareUpdateExclusiveLock, never AccessExclusiveLock.
locks="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
	-d "$PGC_DB" -At 2>/dev/null <<'SQL' | grep -E '^[0-9]+\|[0-9]+$'
BEGIN;
SELECT pgcolumnar.recluster('n', 'x', 'y');
SELECT count(*) FILTER (WHERE mode='ShareUpdateExclusiveLock')::text || '|' ||
       count(*) FILTER (WHERE mode='AccessExclusiveLock')::text
  FROM pg_locks WHERE relation='n'::regclass AND locktype='relation';
ROLLBACK;
SQL
)"
check "recluster holds ShareUpdateExclusiveLock" "${locks%%|*}" "1"
check "recluster holds no AccessExclusiveLock" "${locks##*|}" "0"

pgc_summary
