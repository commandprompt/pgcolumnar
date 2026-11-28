#!/usr/bin/env bash
#
# Lazy VACUUM retires a fully deleted row group, and a reader that runs while
# that VACUUM is still open must not undo the retirement (#2263).
#
# Core marks a lazy VACUUM with PROC_IN_VACUUM, and other backends then leave
# its xid out of their snapshots. The retirement deletes catalog rows. Before
# the fix, a reader that met one of those deleted rows while the VACUUM was
# open found the deleting xid neither in its snapshot nor committed, took it
# for aborted and set HEAP_XMAX_INVALID on the row. The VACUUM then committed
# and the delete was lost for that row only: the row_group row survived, its
# column_chunk and delete_vector rows did not, and the group's deleted row came
# back with every column NULL, from a sequential scan and from an index scan.
#
# Part 1 makes the window by hand. A blocker session holds an uncommitted
# free_space row with the key the retirement inserts last, so the VACUUM waits
# on it after its catalog deletes. A reader then reads the row_group catalog,
# through a statement prepared before the VACUUM, so its planning (which reads
# every delete_vector row) is not in the window. The blocker rolls back.
#
# Part 2 is the reader's guard: a row group whose bytes are on disk but whose
# column_chunk rows are gone, the state Part 1 leaves on a build without the
# fix, is refused with XX001 by both read paths rather than read as NULLs. A
# table with no stored column has groups with no chunks and no bytes, and
# still reads. A row number no row group covers (an aborted insert's index
# entry) is not visible, and an index scan skips it.
#
# Part 3 pins the fix's extent: the VACUUM keeps PROC_IN_VACUUM while it sets
# the visibility map, so a VACUUM held there does not hold back the horizon of
# a VACUUM on another table.
#
# The pytest twin is test/pytest/test_vacuum_retire_visibility.py, with the
# same check names.
#
# Usage:  test/vacuum_retire_visibility.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

q "CREATE EXTENSION IF NOT EXISTS pgcolumnar;" >/dev/null

# Backends killed by a signal, from the server log: a number, or nothing when the
# log cannot be read, so a blind count never reads as "no crashes".
vr_crashes() {
	local _n
	_n="$(pgc_pg "grep -c 'terminated by signal' '$PGC_LOGFILE'" 2>/dev/null)"
	case "$_n" in '' | *[!0-9]*) ;; *) echo "$_n" ;; esac
}
CRASHES_BEFORE="$(vr_crashes)"

# STATEMENT -> its output, or "ERROR <sqlstate> <message>" when it fails. One
# psql, so SET lines in STATEMENT apply to the query after them.
sql_out() {
	local out rc
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -Atq -v ON_ERROR_STOP=1 -v VERBOSITY=verbose -c "$1" 2>&1)"
	rc=$?
	if [ "$rc" -ne 0 ]; then
		sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\): \(.*\)$/ERROR \1 \2/p' <<<"$out" | head -1
		[ -n "$(sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\): .*$/x/p' <<<"$out")" ] || echo "psql rc=$rc: $out"
		return
	fi
	printf '%s\n' "$out"
}

# the fetch path: an index scan, never the columnar scan
FETCH="SET enable_seqscan = off; SET enable_bitmapscan = off; SET enable_indexonlyscan = off;
       SET pgcolumnar.enable_custom_scan = off;"
# the sequential path: the columnar scan, never an index
SCAN="SET enable_indexscan = off; SET enable_bitmapscan = off; SET enable_indexonlyscan = off;"
# every row as id:pad, NULL spelled out, or "none"
rows_of() { echo "SELECT coalesce(string_agg(coalesce(id::text, 'NULL') || ':' || coalesce(pad, 'NULL'), ',' ORDER BY id NULLS FIRST), 'none') FROM $1"; }

# ---------------------------------------------------------------------------
# Part 1: a reader during the VACUUM's retirement (#2263)
# ---------------------------------------------------------------------------
echo "-- part 1: a reader runs while lazy VACUUM retires a row group"
psql_run "CREATE TABLE r (id int, pad text) USING pgcolumnar WITH (autovacuum_enabled = false);"
psql_run "CREATE TABLE rh (id int, pad text) WITH (autovacuum_enabled = false);"
# one row per INSERT, so one row group per row
psql_run "INSERT INTO r VALUES (1, 'one');"
psql_run "INSERT INTO r VALUES (2, 'two');"
psql_run "INSERT INTO rh VALUES (1, 'one'), (2, 'two');"
psql_run "CREATE INDEX r_id ON r (id);"
psql_run "DELETE FROM r WHERE id = 1;"
psql_run "DELETE FROM rh WHERE id = 1;"
SID="$(q "SELECT pgcolumnar.get_storage_id('r');")"
G1="$(q "SELECT group_number FROM pgcolumnar.row_group WHERE storage_id = $SID ORDER BY first_row_number LIMIT 1;")"
G1OFF="$(q "SELECT file_offset FROM pgcolumnar.row_group WHERE storage_id = $SID AND group_number = $G1;")"
check "premise: two row groups before the vacuum" \
	"$(q "SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = $SID;")" "2"
check "premise: the first row group is fully deleted" \
	"$(q "SELECT deleted_count FROM pgcolumnar.delete_vector WHERE storage_id = $SID AND group_number = $G1;")" "1"
blk="$(pgc_wait_delete_past_horizon r)"
check "premise: no transaction is older than the delete" "${blk%/*}" "0"

B_IN="$PGC_WORKDIR/blocker.in"; B_OUT="$PGC_WORKDIR/blocker.out"
R_IN="$PGC_WORKDIR/reader.in"; R_OUT="$PGC_WORKDIR/reader.out"
mkfifo "$B_IN" "$R_IN"; : > "$B_OUT"; : > "$R_OUT"
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
	-d "$PGC_DB" -At -f "$B_IN" >> "$B_OUT" 2>&1 &
B_PID=$!
exec 8> "$B_IN"
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
	-d "$PGC_DB" -At -f "$R_IN" >> "$R_OUT" 2>&1 &
R_PID=$!
exec 9> "$R_IN"
await_out() {	# FILE TOKEN: wait up to 20 s for TOKEN in FILE
	local i
	for i in $(seq 1 200); do
		grep -q "$2" "$1" && return 0
		sleep 0.1
	done
	return 1
}

# The blocker's uncommitted row has the key the retirement inserts into
# free_space after its catalog deletes, so the VACUUM waits there.
printf '%s\n' "BEGIN;" "INSERT INTO pgcolumnar.free_space VALUES ($SID, $G1OFF, 8192, 1);" \
	"SELECT 'B_IN';" >&8
await_out "$B_OUT" B_IN || true
# The reader plans and runs once now, before the VACUUM.
printf '%s\n' "SET plan_cache_mode = force_generic_plan;" "SET statement_timeout = '60s';" \
	"PREPARE rd(int) AS SELECT 'rd:' || id FROM r WHERE id = \$1;" "EXECUTE rd(2);" >&9
await_out "$R_OUT" "rd:2" || true

env PATH="$PGC_BINDIR:$PATH" PGOPTIONS="-c lock_timeout=60s -c statement_timeout=90s" \
	psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At -c "VACUUM r;" \
	> "$PGC_WORKDIR/vacuum_r.out" 2>&1 &
V_PID=$!
waiting="no"
for i in $(seq 1 200); do
	if [ "$(q "SELECT count(*) FROM pg_stat_activity WHERE query = 'VACUUM r;' AND wait_event_type = 'Lock';")" = "1" ]; then
		waiting="yes"; break
	fi
	sleep 0.1
done
check "premise: the vacuum waits on the blocker after deleting the group's catalog rows" "$waiting" "yes"
# A transaction that commits now has an xid newer than the VACUUM's, so the
# reader's snapshot that follows covers the VACUUM's xid.
q "SELECT txid_current();" >/dev/null
: > "$R_OUT"
printf '%s\n' "EXECUTE rd(2);" >&9
got="no reply"; await_out "$R_OUT" "rd:2" && got="$(tail -1 "$R_OUT")"
check "premise: the reader ran while the vacuum was open" "$got" "rd:2"
printf '%s\n' "ROLLBACK;" >&8
exec 8>&- 9>&-
done="no"
for i in $(seq 1 200); do
	kill -0 "$V_PID" 2>/dev/null || { done="yes"; break; }
	sleep 0.1
done
wait "$V_PID" 2>/dev/null || true
wait "$B_PID" "$R_PID" 2>/dev/null || true
check "premise: the vacuum finished" "$done $(cat "$PGC_WORKDIR/vacuum_r.out")" "yes VACUUM"
check "premise: the vacuum retired the group (its range is in the free list)" \
	"$(q "SELECT count(*) FROM pgcolumnar.free_space WHERE storage_id = $SID AND file_offset = $G1OFF;")" "1"

check "the retired group's row_group row is gone" \
	"$(q "SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = $SID AND group_number = $G1;")" "0"
check "a sequential scan returns the heap's rows" \
	"$(sql_out "$SCAN $(rows_of r);")" "$(q "$(rows_of rh);")"
check "no row reads with every column NULL" \
	"$(sql_out "$SCAN SELECT count(*) FROM r WHERE id IS NULL AND pad IS NULL;")" "0"
check "an index scan for the deleted id returns no row" \
	"$(sql_out "$FETCH $(rows_of r) WHERE id = 1;")" "none"
check "premise: that query is an index scan" \
	"$(sql_out "$FETCH EXPLAIN (COSTS OFF) $(rows_of r) WHERE id = 1;" | grep -m1 -oE 'Index Scan|Seq Scan|Custom Scan|Bitmap Heap Scan')" "Index Scan"

# ---------------------------------------------------------------------------
# Part 2: the reader's guard
# ---------------------------------------------------------------------------
echo "-- part 2: a row group with data and no column chunks is refused"
psql_run "CREATE TABLE gd (id int, pad text) USING pgcolumnar WITH (autovacuum_enabled = false);"
psql_run "INSERT INTO gd VALUES (1, 'one');"
psql_run "INSERT INTO gd VALUES (2, 'two');"
psql_run "CREATE INDEX gd_id ON gd (id);"
psql_run "DELETE FROM gd WHERE id = 1;"
GSID="$(q "SELECT pgcolumnar.get_storage_id('gd');")"
GG1="$(q "SELECT group_number FROM pgcolumnar.row_group WHERE storage_id = $GSID ORDER BY first_row_number LIMIT 1;")"
# What Part 1 leaves without the fix: the group's row_group row and nothing else.
psql_run "DELETE FROM pgcolumnar.column_chunk WHERE storage_id = $GSID AND group_number = $GG1;"
psql_run "DELETE FROM pgcolumnar.delete_vector WHERE storage_id = $GSID AND group_number = $GG1;"
check "premise: the damaged group has data and no column chunks" \
	"$(q "SELECT g.byte_length > 0, (SELECT count(*) FROM pgcolumnar.column_chunk c
	       WHERE c.storage_id = g.storage_id AND c.group_number = g.group_number)
	      FROM pgcolumnar.row_group g WHERE g.storage_id = $GSID AND g.group_number = $GG1;")" "t|0"
GMSG="columnar row group $GG1 of relation \"gd\" has no column chunks"
check "a sequential scan of a row group with no column chunks is refused (XX001)" \
	"$(sql_out "$SCAN $(rows_of gd);")" "ERROR XX001 $GMSG"
check "an index fetch of a row in that group is refused (XX001)" \
	"$(sql_out "$FETCH $(rows_of gd) WHERE id = 1;")" "ERROR XX001 $GMSG"
check "an index fetch of a row in another group still returns it" \
	"$(sql_out "$FETCH $(rows_of gd) WHERE id = 2;")" "2:two"

# A table with no stored column: its row groups have no chunks and no bytes.
psql_run "CREATE TABLE z () USING pgcolumnar WITH (autovacuum_enabled = false);"
psql_run "INSERT INTO z DEFAULT VALUES;"
psql_run "INSERT INTO z DEFAULT VALUES;"
psql_run "ALTER TABLE z ADD COLUMN a int;"
psql_run "CREATE INDEX z_a ON z (a);"
ZSID="$(q "SELECT pgcolumnar.get_storage_id('z');")"
check "premise: the no-column table's row groups have no chunks and no bytes" \
	"$(q "SELECT count(*), sum(byte_length), (SELECT count(*) FROM pgcolumnar.column_chunk WHERE storage_id = $ZSID)
	      FROM pgcolumnar.row_group WHERE storage_id = $ZSID;")" "2|0|0"
check "a sequential scan of a table with no stored column reads its rows" \
	"$(sql_out "$SCAN SELECT string_agg(coalesce(a::text, 'NULL'), ',') FROM z;")" "NULL,NULL"
check "an index fetch from a table with no stored column reads its rows" \
	"$(sql_out "$FETCH SELECT string_agg(coalesce(a::text, 'NULL'), ',') FROM z WHERE a IS NULL;")" "NULL,NULL"

# An index entry whose row no row group covers: an aborted insert's.
psql_run "BEGIN; INSERT INTO gd VALUES (3, 'three'); ROLLBACK;"
check "an index entry no row group covers returns no row" \
	"$(sql_out "$FETCH $(rows_of gd) WHERE id = 3;")" "none"

# ---------------------------------------------------------------------------
# Part 3: the VACUUM keeps PROC_IN_VACUUM while it sets the visibility map
# ---------------------------------------------------------------------------
echo "-- part 3: a VACUUM held in its visibility-map step does not hold back another table's horizon"
psql_run "CREATE TABLE va (id int) USING pgcolumnar WITH (autovacuum_enabled = false);"
psql_run "INSERT INTO va SELECT g FROM generate_series(1, 1000) g;"
psql_run "CREATE TABLE vb (id int, pad text) USING pgcolumnar WITH (autovacuum_enabled = false);"
psql_run "INSERT INTO vb VALUES (1, 'one');"
psql_run "INSERT INTO vb VALUES (2, 'two');"
VBSID="$(q "SELECT pgcolumnar.get_storage_id('vb');")"
env PATH="$PGC_BINDIR:$PATH" PGOPTIONS="-c pgcolumnar.vm_set_hold_ms=30000 -c statement_timeout=90s" \
	psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At -c "VACUUM va;" \
	> "$PGC_WORKDIR/vacuum_va.out" 2>&1 &
VA_PID=$!
held="no"
for i in $(seq 1 200); do
	if [ "$(q "SELECT count(*) FROM pg_stat_activity WHERE query = 'VACUUM va;' AND wait_event_type = 'Extension';")" = "1" ]; then
		held="yes"; break
	fi
	sleep 0.1
done
check "premise: the vacuum of va is held in its visibility-map step" "$held" "yes"
# The delete commits after the held VACUUM took its snapshot.
psql_run "DELETE FROM vb WHERE id = 1;"
retired="0"
for i in $(seq 1 20); do
	psql_run "VACUUM vb;"
	if [ "$(q "SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = $VBSID;")" = "1" ]; then
		retired="1"; break
	fi
	sleep 0.5
done
still="$( [ "$(q "SELECT count(*) FROM pg_stat_activity WHERE query = 'VACUUM va;' AND wait_event_type = 'Extension';")" = "1" ] && echo yes || echo no )"
check "a vacuum of vb retires its dead group while va's vacuum is held" "$retired" "1"
check "premise: va's vacuum was still held when vb's group was retired" "$still" "yes"
q "SELECT pg_cancel_backend(pid) FROM pg_stat_activity WHERE query = 'VACUUM va;';" >/dev/null
wait "$VA_PID" 2>/dev/null || true

# Equal counts, both read from the log: an unreadable log reads empty on both
# sides, which check refuses.
check "no backend terminated by signal" "$(vr_crashes)" "$CRASHES_BEFORE"

pgc_summary
