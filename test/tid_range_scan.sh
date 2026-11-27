#!/usr/bin/env bash
#
# A ctid range reads only the row groups that hold it (TID range scans).
#
# WRITTEN BEFORE THE CODE. Measured on #1294: `WHERE ctid >= '(600000,0)'` on a
# 10M-row table matched 0 rows and still read all 67 row groups, because the
# access method has no scan_set_tidrange and the range is a per-row filter. A
# rollup that processes "rows added since the last refresh" (#1294) needs to read
# the newest row groups only, and so does anything else that bounds ctid.
#
# THE ORACLE is the same query with TID scans disabled (enable_tidscan = off),
# which is the per-row filter every build applies today: a TID range scan must
# return exactly what that returns, for every shape of range. THE COST is the
# table's own blocks from pg_statio, flushed by the querying session -- an
# instrument that does not depend on which plan node answers.
#
# Usage:  test/tid_range_scan.sh [PG_CONFIG]

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

at_least() { [ -z "$1" ] && return; [ "$1" -lt "$2" ] && echo "$1" || echo "$2"; }

q "CREATE EXTENSION IF NOT EXISTS pgcolumnar;" >/dev/null
q "CREATE TABLE tr (id bigint, v text) USING pgcolumnar;
   SELECT pgcolumnar.set_options('tr', stripe_row_limit => 10000);
   INSERT INTO tr SELECT g, md5(g::text) FROM generate_series(1, 400000) g;" >/dev/null
check_num "premise: the table holds every row" "$(q "SELECT count(*) FROM tr;")" "400000"
groups="$(q "SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = pgcolumnar.get_storage_id('tr');")"
check_num "premise: the table has many row groups, so skipping can show" "$(at_least "$groups" 30)" "30"

# ctid of the row with a given id, as the AM numbers it
tid_of() { q "SELECT ctid FROM tr WHERE id = $1;"; }
LAST="$(tid_of 390001)"; MID_A="$(tid_of 150001)"; MID_B="$(tid_of 170000)"; END="$(tid_of 400000)"
# Offset 0 is no row's offset, and neither is any offset past the 291 a block
# holds (MaxHeapTuplesPerPage), though a bound may name either: the executor
# turns `ctid < '(B,0)'` into an upper bound of (B-1,65535). Those are the edges
# the AM must translate; read unclamped, offset 400 named a row in block B+1.
BLK="${MID_A#(}"; BLK="${BLK%%,*}"
echo "-- ctids: 390001=$LAST 150001=$MID_A 170000=$MID_B 400000=$END"

check "premise: a ctid range plans a TID range scan, so the arms below exercise it" \
	"$(q "EXPLAIN SELECT id FROM tr WHERE ctid >= '$LAST';" | grep -c 'Tid Range Scan')" "1"

hash() { q "SET enable_tidscan = ${2:-on}; SELECT md5(string_agg(id::text || v, ',' ORDER BY id)) || '|' || count(*) FROM tr WHERE $1;" | tail -1; }
for pred in "ctid >= '$LAST'" "ctid >= '$MID_A' AND ctid <= '$MID_B'" "ctid < '$MID_A'" \
            "ctid > '$END'" "ctid >= '(0,0)'" \
            "ctid <= '($BLK,0)'" "ctid > '($BLK,0)'" "ctid < '($BLK,1)'" "ctid <= '(0,0)'" \
            "ctid >= '($BLK,7)' AND ctid < '($BLK,7)'" \
            "ctid < '($BLK,0)'" "ctid >= '($BLK,400)'" "ctid <= '($BLK,400)'" \
            "ctid > '($BLK,65535)'" "ctid >= '($BLK,291)' AND ctid <= '($BLK,292)'"; do
	check "a ctid range returns what the per-row filter returns: ${pred//$LAST/LAST}" \
		"$(hash "$pred" on)" "$(hash "$pred" off)"
done

# Rescan: an outer row per range, so the scan is rescanned with new bounds and
# a reader built for the previous range must not survive into the next one.
LATQ="SELECT string_agg(r.n || ':' || x.c || ':' || x.h, ' ' ORDER BY r.n)
   FROM (VALUES (1, '$MID_A'::tid, '$MID_B'::tid), (2, '$LAST'::tid, '$END'::tid),
                (3, '(0,1)'::tid, '$MID_A'::tid), (4, '$END'::tid, '$LAST'::tid)) r(n, lo, hi),
   LATERAL (SELECT count(*) c, md5(string_agg(id::text, ',' ORDER BY id)) h FROM tr
            WHERE ctid >= r.lo AND ctid <= r.hi) x"
lat() { q "SET enable_tidscan = $1; $LATQ;" | tail -1; }
# The query the arm runs, not a stand-in: a draft that left the lateral's
# output unreferenced planned no scan of tr at all.
check "premise: the per-range join rescans a TID range scan" \
	"$(q "EXPLAIN $LATQ;" | grep -c 'Tid Range Scan')" "1"
check "a TID range scan rescanned with new bounds returns what the filter returns" "$(lat on)" "$(lat off)"

# Cursors. PostgreSQL counts a TID range scan as able to scan backward
# (ExecSupportsBackwardScan), so no Material goes above it: a SCROLL cursor, and a
# plain DECLARE that the portal makes scrollable for the same reason, send a
# backward fetch straight to the access method. Found by @jdatcmd reviewing
# #1306: FETCH LAST errored where main's plan answered. Compared, outcome for
# outcome, with the same session under enable_tidscan = off.
CQ="SELECT id FROM tr WHERE ctid >= '$MID_A' AND ctid <= '$MID_B'"
cursor_run() {	# cursor_run on|off SCROLL|plain -> every fetch's rows or SQLSTATE, on one line
	local kind=""; [ "$2" = SCROLL ] && kind="SCROLL"
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At \
		-v VERBOSITY=sqlstate -c "SET enable_tidscan = $1" -c "BEGIN" \
		-c "DECLARE c $kind CURSOR FOR $CQ" -c "FETCH 2 FROM c" -c "FETCH LAST FROM c" \
		-c "FETCH BACKWARD 2 FROM c" -c "FETCH ABSOLUTE 3 FROM c" -c "COMMIT" 2>&1 | tr '\n' ' '
}
check "premise: the cursor's query plans a TID range scan" \
	"$(q "EXPLAIN $CQ;" | grep -c 'Tid Range Scan')" "1"
check "a SCROLL cursor over a ctid range fetches backward as the filter's plan does" \
	"$(cursor_run on SCROLL)" "$(cursor_run off SCROLL)"
# A plain DECLARE over the filter's plan is made forward-only (55000 on a backward
# fetch). Over a TID range scan it is made scrollable, so it must answer as a
# SCROLL cursor does, correctly; what it may never do is reach the access method.
check "a plain cursor over a ctid range answers a backward fetch correctly" \
	"$(cursor_run on plain)" "$(cursor_run off SCROLL)"

# A cursor whose query has a subplan: the Material's node id must not repeat one
# of the subplan's (#1306 review). set_plan_references numbers the main tree, then
# the subplans, from one counter; an assert build checks the ids are unique.
SQ="SELECT id, (SELECT max(id) FROM tr) AS top FROM tr WHERE ctid >= '$MID_A' AND ctid <= '$MID_B'"
sub_cursor() {	# sub_cursor on|off -> the fetches of a SCROLL cursor over SQ
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At \
		-v VERBOSITY=sqlstate -c "SET enable_tidscan = $1" -c "BEGIN" -c "DECLARE c SCROLL CURSOR FOR $SQ" \
		-c "FETCH LAST FROM c" -c "FETCH BACKWARD 2 FROM c" -c "COMMIT" 2>&1 | tr '\n' ' '
}
check "premise: the subquery cursor plans a TID range scan and a subplan" \
	"$(q "EXPLAIN $SQ;" | grep -c 'Tid Range Scan\|InitPlan')" "2"
check "a SCROLL cursor whose query has a subplan fetches backward as the filter's plan does" \
	"$(sub_cursor on)" "$(sub_cursor off)"

# Rows written earlier in the same transaction: set_tidrange reads the row-group
# catalog, and the pending writes must be in it by then (#1306 review).
same_xact() {	# same_xact on|off -> count|digest of the range after an insert in the same transaction
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At \
		-c "SET enable_tidscan = $1" -c "BEGIN" \
		-c "INSERT INTO tr SELECT g, md5(g::text) FROM generate_series(400001, 400700) g" \
		-c "SELECT count(*) || '|' || md5(string_agg(id::text, ',' ORDER BY id)) FROM tr WHERE ctid >= '$LAST'" \
		-c "ROLLBACK" 2>&1 | grep '|'
}
check "premise: inside a transaction the range still plans a TID range scan" \
	"$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At \
	   -c "BEGIN" -c "INSERT INTO tr VALUES (0, 'x')" -c "EXPLAIN SELECT id FROM tr WHERE ctid >= '$LAST'" -c "ROLLBACK" |
	   grep -c 'Tid Range Scan')" "1"
check "rows inserted earlier in the same transaction are in the range, as the filter sees them" \
	"$(same_xact on)" "$(same_xact off)"

# PostgreSQL 19 runs a TID range scan in parallel: each worker sets the range
# and the reader's shared group claim hands out only the restricted groups. The
# custom scan is turned off because, with parallelism cheap, the planner prefers
# a parallel custom scan that filters ctid per row, and this arm is about the
# AM's path.
if [ "$PGC_MAJOR" -ge 19 ]; then
	PAR="SET parallel_setup_cost = 0; SET parallel_tuple_cost = 0; SET min_parallel_table_scan_size = 0;
	     SET max_parallel_workers_per_gather = 2; SET parallel_leader_participation = off;
	     SET pgcolumnar.enable_custom_scan = off;"
	PQ="SELECT md5(string_agg(id::text || v, ',' ORDER BY id)) || '|' || count(*) FROM tr
	    WHERE ctid >= '$MID_A' AND ctid <= '$LAST'"
	plan="$(q "$PAR EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) $PQ;")"
	check "premise: the range runs as a parallel TID range scan with two workers" \
		"$(grep -c 'Parallel Tid Range Scan' <<<"$plan")|$(grep -o 'Workers Launched: [0-9]*' <<<"$plan")" \
		"1|Workers Launched: 2"
	check "a parallel TID range scan returns what the filter returns" \
		"$(q "$PAR $PQ;" | tail -1)" "$(hash "ctid >= '$MID_A' AND ctid <= '$LAST'" off)"
else
	check_skip "a parallel TID range scan returns what the filter returns" \
		"SKIP  a parallel TID range scan returns what the filter returns (PostgreSQL $PGC_MAJOR has none; 19 added it)" \
		"PostgreSQL $PGC_MAJOR has no parallel TID range scan"
fi

# max(v || 'x'), not max(v): an unfiltered count/max is answered from zone maps
# (#1293) and read 5 blocks of this table in the first draft, so the "whole
# table" baseline measured metadata, not data.
blocks() {	# blocks PRED -> tr blocks read, in the querying session
	q "SELECT pg_stat_reset();" >/dev/null
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At \
		-c "SELECT count(*), max(v || 'x') FROM tr WHERE $1" -c "SELECT pg_stat_force_next_flush()" >/dev/null 2>&1
	q "SELECT coalesce(heap_blks_read, 0) + coalesce(heap_blks_hit, 0) FROM pg_statio_user_tables WHERE relname = 'tr';"
}
full="$(blocks "true")"
tail_b="$(blocks "ctid >= '$LAST'")"
echo "-- blocks: whole table $full, last 2.5% by ctid $tail_b"
check_num "premise: reading the whole table reads its blocks" "$(at_least "$full" 100)" "100"
check_num "a ctid range over the last row groups reads under a fifth of the table" \
	"$( [ -n "$tail_b" ] && [ "$tail_b" -ge 1 ] && at_least $((full - 5 * tail_b)) 1 || echo "read ${tail_b:-nothing}")" "1"

# Deleted rows inside the range stay invisible.
q "DELETE FROM tr WHERE id BETWEEN 395000 AND 395999;" >/dev/null
check "and rows deleted inside the range stay deleted" \
	"$(hash "ctid >= '$LAST'" on)" "$(hash "ctid >= '$LAST'" off)"
check_num "premise: the delete removed rows from the range" \
	"$(q "SET enable_tidscan = off; SELECT count(*) FROM tr WHERE ctid >= '$LAST';" | tail -1)" "9000"

# Compaction moves a group's surviving rows into a new group with new, higher
# row numbers. The range over the old addresses now names rows nowhere, and the
# range over the new ones must find them all.
check_num "premise: compaction rewrote the group the delete thinned" \
	"$(q "SELECT pgcolumnar.compact_rewrite('tr', 0.05, 0);")" "1"
MOVED="$(tid_of 390001)"
check "premise: compaction gave the surviving rows new addresses" \
	"$([ "$MOVED" != "$LAST" ] && echo moved || echo "kept $MOVED")" "moved"
for pred in "ctid >= '$LAST'" "ctid >= '$MOVED'" "ctid >= '$MID_A' AND ctid <= '$MOVED'"; do
	check "after compaction a ctid range returns what the filter returns: ${pred//$LAST/LAST}" \
		"$(hash "$pred" on)" "$(hash "$pred" off)"
done

# A single ctid that no row can have (#2058). A TID scan asks the access method
# whether each listed ctid is valid before it fetches. Offset 0 is never a valid
# ctid, and no columnar row has an offset past the 291 a block holds. Accepting
# them all, the AM failed an assertion on '(0,0)', which restarts every backend
# of an assert build, and read '(0,292)' as row 291, whose ctid is (1,1): a
# query, a DELETE or an UPDATE reached a row it did not name. Last in the suite,
# so a crash on unfixed code cannot take down the arms above. Its own table and
# its own rows. THE ORACLE is a heap table: zero rows for every one of them.
# A DELETE or UPDATE that reaches a row moves or removes it, which would hide
# the next statement's fault, so each one gets the table fresh.
badtid() {
	q "DROP TABLE IF EXISTS badtid;
	   CREATE TABLE badtid (id int, v int) USING pgcolumnar;
	   INSERT INTO badtid SELECT g, g FROM generate_series(1, 700) g;" >/dev/null
}
rows_left() { q "SELECT count(*) || '|' || sum(v) FROM badtid WHERE v > 0;"; }
badtid
check "premise: the row after offset 291 of block 0 sits at (1,1)" \
	"$(q "SELECT ctid FROM badtid WHERE id = 291;")" "(1,1)"
check "premise: a single ctid plans a TID scan" \
	"$(q "EXPLAIN SELECT id FROM badtid WHERE ctid = '(0,292)';" | grep -c 'Tid Scan on badtid')" "1"
# The other side of the boundary, so a fix that refused every ctid fails here.
check "a TID scan of the last offset a block holds, '(0,291)', still returns its row" \
	"$(q "SELECT string_agg(id::text, ',') FROM badtid WHERE ctid = '(0,291)';")" "290"
check "a TID scan of two ctids rows have and one past a block's last offset returns those two rows" \
	"$(q "SELECT string_agg(id::text, ',' ORDER BY id) FROM badtid WHERE ctid = ANY (ARRAY['(1,1)', '(0,292)', '(0,2)']::tid[]);")" "1,291"
for t in "(0,292)" "(1,400)" "(0,0)" "(1,0)"; do
	check "a TID scan of ctid '$t', which no row has, returns no row, as on heap" \
		"$(q "SELECT 'rows=' || count(*) FROM badtid WHERE ctid = '$t';")" "rows=0"
	check "a TID scan of ctid '$t' in an array returns no row, as on heap" \
		"$(q "SELECT 'rows=' || count(*) FROM badtid WHERE ctid = ANY (ARRAY['$t']::tid[]);")" "rows=0"
	badtid
	check "UPDATE ... WHERE ctid = '$t' changes no row, and every row is unchanged" \
		"$(q "WITH u AS (UPDATE badtid SET v = -v WHERE ctid = '$t' RETURNING 1) SELECT 'updated=' || count(*) FROM u;")|$(rows_left)" \
		"updated=0|700|245350"
	badtid
	check "DELETE ... WHERE ctid = '$t' deletes no row, and every row is still there" \
		"$(q "WITH d AS (DELETE FROM badtid WHERE ctid = '$t' RETURNING 1) SELECT 'deleted=' || count(*) FROM d;")|$(rows_left)" \
		"deleted=0|700|245350"
done

pgc_summary
