#!/usr/bin/env bash
#
# A row group that lazy VACUUM retires stays retired when another session reads
# its catalog rows while that VACUUM is still running (#2264).
#
# Core sets PROC_IN_VACUUM for a lazy VACUUM, and GetSnapshotData leaves such a
# backend's XID out of every other snapshot. pgColumnar's lazy VACUUM writes the
# catalog: it deletes a retired group's row_group and delete_vector rows and
# inserts its free_space row. A session whose snapshot was taken after that XID
# was assigned, and after a later XID committed, found the deleting XID neither
# in its snapshot nor committed, took it for aborted, and set HEAP_XMAX_INVALID
# on the row_group row. That hint is permanent. After the VACUUM committed, the
# group was live again while its range was also in free_space. The next VACUUM
# that retired it inserted the same (storage_id, file_offset) and failed with
# 23505 on free_space_pkey. When the reader read only row_group, the group's
# deleted rows came back.
#
# The race is made deterministic, not timed. Three single-row inserts make three
# row groups and the first two are deleted. Session L locks the second group's
# row_group row FOR UPDATE and then waits on advisory lock 2264, which session H
# holds. VACUUM retires the first group, then waits on L's row lock when it
# deletes the second group's row_group row. A transaction then commits
# (txid_current), so the VACUUM's XID is below the next snapshot's xmax, and the
# reader reads the first group's rows. H releases, L commits, and the VACUUM
# finishes.
#
# Two variants. "dv": the reader reads the first group's row_group and
# delete_vector rows. "rg": it reads only row_group.
#
# Every wait is bounded: L and both VACUUMs run with statement_timeout, and the
# polls give up after 20 s.
#
# The pytest twin is test/pytest/test_vacuum_retire_snapshot.py, same check
# names.
#
# Usage:  test/vacuum_retire_snapshot.sh [PG_CONFIG]

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

raw() { env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At "$@"; }

crashes() {
	# grep -c prints 0 itself when nothing matches, and then exits 1
	pgc_pg "grep -c 'terminated by signal' '$PGC_LOGFILE'" 2>/dev/null || true
}
crashes_before="$(crashes)"

H_IN="$PGC_WORKDIR/sessH.in"; H_OUT="$PGC_WORKDIR/sessH.out"
mkfifo "$H_IN"; : > "$H_OUT"
raw -f "$H_IN" >> "$H_OUT" 2>&1 &
H_PID=$!
exec 9> "$H_IN"
h_send() { printf '%s\n' "$*" >&9; }
h_wait() {
	local token="$1" i
	for i in $(seq 1 200); do
		grep -q "$token" "$H_OUT" && return 0
		sleep 0.1
	done
	return 1
}
poll() {  # SQL -> "yes" once SQL returns 1, else "no" after 20 s
	local i
	for i in $(seq 1 200); do
		[ "$(q "$1")" = "1" ] && { echo yes; return; }
		sleep 0.1
	done
	echo no
}

# retire_race TAG TABLE READ: set rr_* for the checks below. READ is the reader's
# query, with %S for the storage id and %G for the first group's number.
retire_race() {
	local tag="$1" tbl="$2" read="$3" s g1 g2 lpid vpid vacq vxid cx
	psql_run "CREATE TABLE $tbl (id int, pad text) USING pgcolumnar;"
	psql_run "INSERT INTO $tbl VALUES (1, 'p1');"
	psql_run "INSERT INTO $tbl VALUES (2, 'p2');"
	psql_run "INSERT INTO $tbl VALUES (3, 'p3');"
	psql_run "DELETE FROM $tbl WHERE id IN (1, 2);"
	s="$(q "SELECT pgcolumnar.get_storage_id('$tbl');")"
	rr_groups="$(q "SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = $s;")"
	g1="$(q "SELECT min(group_number) FROM pgcolumnar.row_group WHERE storage_id = $s;")"
	g2="$(q "SELECT min(group_number) FROM pgcolumnar.row_group WHERE storage_id = $s AND group_number > $g1;")"
	# the page footprints of the two groups VACUUM retires: each group's bytes
	# rounded up to whole logical pages (block size less the page header)
	rr_free_want="$(q "SELECT sum((byte_length + p - 1) / p * p) FROM pgcolumnar.row_group,
		(SELECT current_setting('block_size')::int - 24 AS p) z
		WHERE storage_id = $s AND group_number IN ($g1, $g2);")"

	h_send "SELECT 'H_LOCKED_$tag' FROM pg_advisory_lock(2264);"
	h_wait "H_LOCKED_$tag" || true
	raw -c "SET statement_timeout = '60s'" -c "BEGIN" \
		-c "SELECT 'L' FROM pgcolumnar.row_group WHERE storage_id = $s AND group_number = $g2 FOR UPDATE" \
		-c "SELECT 'L_WAITING' FROM pg_advisory_lock_shared(2264)" -c "COMMIT" \
		> "$PGC_WORKDIR/$tbl.l.out" 2>&1 &
	lpid=$!
	rr_locked="$(poll "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = 2264 AND NOT granted;")"

	vacq="VACUUM $tbl"
	raw -c "SET statement_timeout = '60s'" -c "$vacq" > "$PGC_WORKDIR/$tbl.v.out" 2>&1 &
	vpid=$!
	rr_paused="$(poll "SELECT count(*) FROM pg_stat_activity WHERE query = '$vacq' AND wait_event_type = 'Lock';")"
	vxid="$(q "SELECT backend_xid FROM pg_stat_activity WHERE query = '$vacq';")"
	cx="$(q "SELECT txid_current();")"
	rr_xid="no"
	if [ -n "$vxid" ] && [ "$vxid" -lt "$cx" ] 2>/dev/null; then rr_xid="yes"; fi

	read="${read//%S/$s}"; read="${read//%G/$g1}"
	rr_seen="$(q "$read")"
	rr_still="$(q "SELECT count(*) FROM pg_stat_activity WHERE query = '$vacq' AND wait_event_type = 'Lock';")"

	h_send "SELECT 'H_UNLOCKED_$tag' FROM pg_advisory_unlock(2264);"
	h_wait "H_UNLOCKED_$tag" || true
	wait "$vpid" 2>/dev/null || true
	wait "$lpid" 2>/dev/null || true
	rr_vacuum="$(cat "$PGC_WORKDIR/$tbl.v.out")"
	rr_vacuum="${rr_vacuum#SET$'\n'}"

	rr_retired="$(q "SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = $s AND group_number = $g1;")"
	rr_overlap="$(q "SELECT count(*) FROM pgcolumnar.free_space f JOIN pgcolumnar.row_group r
		ON r.storage_id = f.storage_id AND r.file_offset < f.file_offset + f.byte_length
		AND f.file_offset < r.file_offset + greatest(r.byte_length, 1)
		WHERE f.storage_id = $s;")"
	rr_free="$(q "SELECT coalesce(sum(byte_length), 0) FROM pgcolumnar.free_space WHERE storage_id = $s;")"
	# with the error's SQLSTATE when the scan refuses (a group with no column
	# chunks is refused with XX001 since #2263), not an empty string
	rr_rows="$(raw -c "\\set VERBOSITY sqlstate" \
		-c "SELECT string_agg(coalesce(id::text, 'NULL') || ':' || coalesce(pad, 'NULL'), ',' ORDER BY id) FROM $tbl;" 2>&1)"
	rr_again="$(raw -c "SET statement_timeout = '60s'" -c "\\set VERBOSITY sqlstate" -c "$vacq" 2>&1)"
	rr_again="${rr_again#SET$'\n'}"
}

echo "-- dv: the reader reads the retired group's row_group and delete_vector rows"
retire_race dv rv_dv "SELECT (SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = %S AND group_number = %G)
	|| '|' || (SELECT count(*) FROM pgcolumnar.delete_vector WHERE storage_id = %S AND group_number = %G);"
check "premise: dv -- three single-row inserts made three row groups" "$rr_groups" "3"
check "premise: dv -- the row lock holder is waiting on the advisory lock" "$rr_locked" "yes"
check "premise: dv -- VACUUM is paused on the row lock inside its retirement" "$rr_paused" "yes"
check "premise: dv -- the paused VACUUM's XID is below a committed XID" "$rr_xid" "yes"
check "premise: dv -- the reader saw the group VACUUM was retiring" "$rr_seen" "1|1"
check "premise: dv -- VACUUM was still paused after the read" "$rr_still" "1"
check "premise: dv -- the paused VACUUM finished" "$rr_vacuum" "VACUUM"
check "dv: the retired group stays retired after a concurrent read (#2264)" "$rr_retired" "0"
check "dv: no free_space range overlaps a live row group (#2264)" "$rr_overlap" "0"
check "dv: free_space holds exactly the two retired groups' pages (#2264)" "$rr_free" "$rr_free_want"
check "dv: the next VACUUM does not fail on free_space_pkey (#2264)" "$rr_again" "VACUUM"
check "dv: the table returns only the live row" "$rr_rows" "3:p3"

echo "-- rg: the reader reads only the retired group's row_group row"
retire_race rg rv_rg "SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = %S AND group_number = %G;"
check "premise: rg -- three single-row inserts made three row groups" "$rr_groups" "3"
check "premise: rg -- the row lock holder is waiting on the advisory lock" "$rr_locked" "yes"
check "premise: rg -- VACUUM is paused on the row lock inside its retirement" "$rr_paused" "yes"
check "premise: rg -- the paused VACUUM's XID is below a committed XID" "$rr_xid" "yes"
check "premise: rg -- the reader saw the group VACUUM was retiring" "$rr_seen" "1"
check "premise: rg -- VACUUM was still paused after the read" "$rr_still" "1"
check "premise: rg -- the paused VACUUM finished" "$rr_vacuum" "VACUUM"
check "rg: the retired group stays retired after a concurrent read (#2264)" "$rr_retired" "0"
check "rg: no free_space range overlaps a live row group (#2264)" "$rr_overlap" "0"
check "rg: free_space holds exactly the two retired groups' pages (#2264)" "$rr_free" "$rr_free_want"
check "rg: the table returns only the live row (#2264)" "$rr_rows" "3:p3"
check "rg: the next VACUUM succeeds" "$rr_again" "VACUUM"

exec 9>&-
wait "$H_PID" 2>/dev/null || true
check "no backend terminated by signal during the races" "$(( $(crashes) - crashes_before ))" "0"

pgc_summary
