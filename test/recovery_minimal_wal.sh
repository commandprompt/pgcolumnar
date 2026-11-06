#!/usr/bin/env bash
#
# At wal_level=minimal, a table created or truncated in the transaction that loads
# it survives a crash (#1341). PostgreSQL skips WAL for such a table and makes it
# durable at COMMIT instead: below wal_skip_threshold it writes the new pages to
# WAL, at or above it it fsyncs the files. The columnar storage code branches on
# RelationNeedsWAL, and no suite ran at wal_level=minimal.
#
# The cluster runs at wal_level=minimal. Each arm loads a columnar table and a heap
# mirror in one transaction, commits, crashes the server (SIGKILL of the process
# group), restarts it into crash recovery, and compares the two by content. Both
# branches of the commit-time sync run: wal_skip_threshold 0 (always fsync) and
# 64MB (these tables are smaller, so their pages are written to WAL).
#
# Usage:  test/recovery_minimal_wal.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
export PGC_EXTRA_CONF="wal_level=minimal
max_wal_senders=0"
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

crash_cluster() {	# SIGKILL the postmaster's process group, as recovery.sh does
	local pm i
	pm="$(head -1 "$PGC_PGDATA/postmaster.pid" 2>/dev/null || true)"
	if [ -n "$pm" ]; then
		kill -9 -"$pm" 2>/dev/null || true
		kill -9 "$pm" 2>/dev/null || true
	fi
	for i in $(seq 1 100); do
		{ [ -n "$pm" ] && kill -0 "$pm" 2>/dev/null; } || break
		sleep 0.1
	done
	sleep 1
}
restart_cluster() {	# -> ok once the cluster is back
	local attempt
	for attempt in 1 2 3 4 5 6; do
		pgc_pg "pg_ctl -D '$PGC_PGDATA' -l '$PGC_LOGFILE' start -w" >/dev/null 2>&1 && { echo ok; return 0; }
		sleep 2
	done
	echo "restart failed"
}
redo_count() {
	local n
	n="$(pgc_pg "grep -c 'database system was not properly shut down' '$PGC_LOGFILE'" 2>/dev/null | tail -1)"
	echo "${n:-0}"
}
digest() {	# digest TABLE -> count and md5 over every row
	q "SELECT count(*) || '/' || md5(string_agg(id || ':' || v || ':' || w, ',' ORDER BY id)) FROM $1" | grep -E '^[0-9]+/' | tail -1
}
LOAD="SELECT g, (g * 7919) % 10007, md5(g::text) FROM generate_series(1, 50000) g"

check "premise: the cluster runs at wal_level=minimal" "$(q "SHOW wal_level")" "minimal"

# crash_after SQL TABLE -> "<committed rows>|<came back>|<recovered>": run SQL, read
# how many rows TABLE holds before the crash (so a load that failed cannot pass as
# two empty tables agreeing), then crash
crash_after() {
	local r0 r1 back n
	r0="$(redo_count)"
	psql_run "$1" >/dev/null 2>&1
	n="$(q "SELECT count(*) FROM $2" 2>/dev/null | tail -1)"
	crash_cluster
	back="$(restart_cluster)"
	r1="$(redo_count)"
	echo "${n:-no table}|$back|$([ "$r1" -gt "$r0" ] && echo recovered || echo "no recovery logged ($r0 before, $r1 after)")"
}

for thr in 0 64MB; do
	cr="$(crash_after "BEGIN; SET LOCAL wal_skip_threshold = '$thr';
	                   CREATE TABLE mw_c_$thr (id int, v int, w text) USING pgcolumnar;
	                   CREATE TABLE mw_h_$thr (id int, v int, w text);
	                   INSERT INTO mw_c_$thr $LOAD; INSERT INTO mw_h_$thr $LOAD; COMMIT;" mw_c_$thr)"
	check "a table created and loaded in one transaction at wal_skip_threshold $thr committed its rows, and the server came back through recovery" "$cr" "50000|ok|recovered"
	check "and the columnar table holds what its heap mirror holds, at wal_skip_threshold $thr" \
		"$(digest mw_c_$thr)|$(digest mw_h_$thr | cut -d/ -f1)" "$(digest mw_h_$thr)|50000"
done
for thr in 0 64MB; do
	cr="$(crash_after "BEGIN; SET LOCAL wal_skip_threshold = '$thr';
	                   TRUNCATE mw_c_$thr, mw_h_$thr;
	                   INSERT INTO mw_c_$thr SELECT id + 1, v, w FROM ($LOAD) s(id, v, w);
	                   INSERT INTO mw_h_$thr SELECT id + 1, v, w FROM ($LOAD) s(id, v, w); COMMIT;" mw_c_$thr)"
	check "a table truncated and reloaded in one transaction at wal_skip_threshold $thr committed its rows, and the server came back through recovery" "$cr" "50000|ok|recovered"
	check "and after the truncate the columnar table holds what its heap mirror holds, at wal_skip_threshold $thr" \
		"$(digest mw_c_$thr)|$(digest mw_h_$thr | cut -d/ -f1)" "$(digest mw_h_$thr)|50000"
done

pgc_summary
