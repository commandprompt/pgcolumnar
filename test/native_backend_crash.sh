#!/usr/bin/env bash
#
# pgColumnar single-backend crash blast radius (#217).
#
# recovery.sh crashes the whole cluster and restarts it. This suite covers the
# other shape: one backend dies while the postmaster lives, which is the #210
# case (a SIGSEGV takes down a single backend, the postmaster reinitializes
# shared memory and drops every other session). The guarantee under test is that
# committed columnar data is intact on the far side of that, and that an
# in-flight write leaves nothing, because columnar data goes through the buffer
# manager and WAL like any other relation. If that holds, a single backend death
# is no different from any other backend death in PostgreSQL.
#
# A heap mirror written the same way is the oracle: after the crash the columnar
# table must match it exactly. The kill is repeated at more than one point in the
# write path -- asserted from wait_event, not assumed from a timing delay. Because
# the write is uncommitted either way, "no rows survived" alone cannot tell a
# crash apart from a clean abort, so every scenario also opens a sentinel session
# before the kill and asserts the reinit dropped it: that is what proves a crash
# actually happened and the durability checks are not passing vacuously.
#
# Usage:  test/native_backend_crash.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# The postmaster must reinitialize a crashed backend rather than shut the cluster
# down; that is the default, set here so the suite does not depend on it.
psql_run "ALTER SYSTEM SET restart_after_crash = on;"
# THE WAL WRITER IS HELD OFF (its 10s maximum), as a busy runner can leave it, so
# a kill always loses the killed write's unflushed WAL. See the post-flush
# scenario below for why that decides the order of the scenarios.
psql_run "ALTER SYSTEM SET wal_writer_delay = '10s';"
psql_run "SELECT pg_reload_conf();" >/dev/null

# Heap mirror and columnar table, same committed baseline in both.
psql_run "DROP TABLE IF EXISTS h; DROP TABLE IF EXISTS c;
	CREATE TABLE h (id int, v text, x float8);
	CREATE TABLE c (id int, v text, x float8) USING pgcolumnar;
	INSERT INTO h SELECT g, 'b'||g, g*2.5 FROM generate_series(1, 5000) g;
	INSERT INTO c SELECT g, 'b'||g, g*2.5 FROM generate_series(1, 5000) g;"

rowgroups() {
	q "SELECT count(*) FROM pgcolumnar.row_group r
		JOIN pgcolumnar.storage s ON s.storage_id = r.storage_id
		WHERE s.relation_oid = 'c'::regclass;"
}
content() { pgc_set_hash "SELECT id, v, x FROM c"; }

base_groups="$(rowgroups)"
base_hash="$(content)"
base_mirror="$(pgc_set_hash "SELECT id, v, x FROM h")"
pm_before="$(head -1 "$PGC_PGDATA/postmaster.pid" 2>/dev/null)"

# Wait for the postmaster to finish reinitializing and accept connections again.
# Reinitialization briefly accepts a connection and then refuses the next while it
# finishes clearing the old backends, so a handful of quick successes can all land
# inside one transient "accepting" blip and race the recovery window. Require a
# continuous run of successes that spans a real stretch of wall-clock time instead:
# 12 in a row at 0.25s is a 3s uninterrupted window a reinit blip cannot fake, and
# it holds up under matrix contention where the blip is wider.
wait_up() {
	local i ok=0
	for i in $(seq 1 1200); do
		if [ "$(q 'SELECT 1' 2>/dev/null)" = "1" ]; then
			ok=$((ok + 1))
			[ "$ok" -ge 12 ] && return 0
		else
			ok=0
		fi
		sleep 0.25
	done
	return 1
}

# A backend that must NOT die of its own accord, opened before each crash so we
# can prove the postmaster actually reinitialized: a plain backend exit drops only
# itself, a crash-triggered reinit drops every other session too. If this session
# survives the kill, no reinit happened and the durability checks below would be
# passing vacuously (the insert is uncommitted either way).  Sleeps 120s, longer
# than any single scenario, and is identified by that literal so its own pid is
# easy to find.
sentinel_dropped=""
open_sentinel() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -c "SELECT pg_sleep(120);" >/dev/null 2>&1 &
	sentinel_bg=$!
	# Generous budget: this connect races the rest of the matrix for the box, and
	# a false "did not connect" here would fail the reinit assertion spuriously.
	local i
	for i in $(seq 1 300); do
		[ -n "$(q "SELECT pid FROM pg_stat_activity
			WHERE query LIKE '%pg_sleep(120)%' AND pid <> pg_backend_pid() LIMIT 1;")" ] && return 0
		sleep 0.1
	done
	return 1
}

# Run an INSERT into c inside an explicit transaction held open by a trailing
# pg_sleep, so the write is never committed no matter how fast it runs; then
# SIGSEGV that one backend and wait for the postmaster to reinitialize. $1 rows
# sizes the write, $2 a marker that locates the backend in its query text, $3 the
# phase to kill in: "insert" lands the kill mid-write (the backend is still
# executing the INSERT), "sleep" lands it once every stripe has flushed and the
# backend is parked in the trailing pg_sleep, between the last flush and the
# commit that never comes. The phase is asserted, not assumed, from wait_event.
crash_one_backend() {
	local rows="$1" marker="$2" phase="$3" bg pid i we src size0
	open_sentinel || echo "-- sentinel did not connect"
	size0="$(q "SELECT pg_relation_size('c');")"
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -c "BEGIN; INSERT INTO c /* $marker */ SELECT g, 'k'||g, g
			FROM generate_series(1, $rows) g; SELECT pg_sleep(60);" >/dev/null 2>&1 &
	bg=$!
	pid=""
	for i in $(seq 1 300); do
		pid="$(q "SELECT pid FROM pg_stat_activity
			WHERE query LIKE '%$marker%' AND state = 'active'
			  AND pid <> pg_backend_pid() LIMIT 1;")"
		[ -n "$pid" ] && break
		sleep 0.1
	done
	if [ -z "$pid" ]; then
		echo "-- could not locate the inserting backend"
		kill "$bg" "$sentinel_bg" 2>/dev/null || true
		return 1
	fi
	# Drive to the requested phase. For "sleep" poll until the backend actually
	# reaches pg_sleep (the insert has flushed and finished); for "insert" give it
	# a moment to be well inside the write, then confirm it is not yet sleeping.
	if [ "$phase" = sleep ]; then
		for i in $(seq 1 600); do
			[ "$(q "SELECT coalesce(wait_event,'') FROM pg_stat_activity WHERE pid = $pid;")" = PgSleep ] && break
			sleep 0.1
		done
	else
		# Until the write has reached the relation. A fixed 0.5s stood here, and
		# nothing showed the INSERT had written a page by then: with its stripes
		# buffered whole (stripe_row_limit above the row count) the kill landed on
		# a write that had produced nothing, and every mid-write arm still passed
		# (#1341). So the kill now waits for the relation to grow, and asserts it.
		for i in $(seq 1 600); do
			[ "$(q "SELECT pg_relation_size('c') > $size0;")" = t ] && break
			sleep 0.1
		done
	fi
	check "$marker: the INSERT had written pages before the kill (the relation grew)" \
		"$(q "SELECT pg_relation_size('c') > $size0;")" "t"
	we="$(q "SELECT coalesce(wait_event,'') FROM pg_stat_activity WHERE pid = $pid;")"
	if [ "$phase" = sleep ]; then
		check "$marker: kill lands after the stripes flushed (backend in pg_sleep)" "$we" "PgSleep"
	else
		check "$marker: kill lands mid-write (backend not yet in pg_sleep)" \
			"$( [ "$we" != PgSleep ] && echo yes || echo no )" "yes"
	fi
	echo "-- SIGSEGV backend $pid ($marker, phase $phase, wait_event=${we:-<null>})"
	kill -SEGV "$pid" 2>/dev/null || true
	kill "$bg" 2>/dev/null || true
	wait "$bg" 2>/dev/null || true
	wait_up || echo "-- cluster did not come back"
	# The sentinel opened before the crash must have been dropped by the reinit;
	# if it is still alive, no reinit happened and nothing below is a real test.
	# Poll for the client to exit rather than wait on it: a reinit drops its
	# connection and it exits within the recovery window (already elapsed in
	# wait_up), while a session that was NOT dropped would otherwise block this
	# wait for the full pg_sleep. No exit inside the window means not dropped.
	sentinel_dropped=no
	for i in $(seq 1 50); do
		if ! kill -0 "$sentinel_bg" 2>/dev/null; then
			wait "$sentinel_bg" 2>/dev/null || true
			sentinel_dropped=yes
			break
		fi
		sleep 0.1
	done
	[ "$sentinel_dropped" = yes ] || { kill "$sentinel_bg" 2>/dev/null || true; wait "$sentinel_bg" 2>/dev/null || true; }
}

assert_intact() {  # label
	check "$1: the crash reinitialized the cluster (a live session was dropped)" \
		"$sentinel_dropped" "yes"
	check "$1: postmaster survived, same pid" \
		"$(head -1 "$PGC_PGDATA/postmaster.pid" 2>/dev/null)" "$pm_before"
	check "$1: cluster accepts new connections" "$(q 'SELECT 1')" "1"
	check "$1: committed content in c matches the heap mirror" "$(content)" "$base_hash"
	check "$1: the mirror itself is unchanged" \
		"$(pgc_set_hash "SELECT id, v, x FROM h")" "$base_mirror"
	check "$1: the killed insert left no visible rows" "$(q 'SELECT count(*) FROM c')" "5000"
	check "$1: the killed insert left no partial row group" "$(rowgroups)" "$base_groups"
}

# --- kill after the stripes have flushed, before the commit that never comes -
# FIRST, because its write is the small one. A killed write's stripe reservation
# (the metapage highwater) is WAL-logged but unflushed, while its pages already
# extend the file. Redo then restores the old highwater over blocks that exist,
# and the next write fills them before it extends anything: after crashA, all
# 100000 rows of this one fit, the relation never grew, and its premise failed
# (nightly 40376459928, PG 17). Here it follows only the committed baseline.
# crashA and crashC write 3000000 rows, which outgrow any such leftover.
crash_one_backend 100000 crashB sleep
assert_intact "post-flush"

# --- kill while a large insert is mid-write ----------------------------------
crash_one_backend 3000000 crashA insert
assert_intact "mid-write"

# --- kill with a concurrent reader holding a connection ----------------------
# The reinit drops every session, not only the one that died. This scenario adds
# a second parked reader on top of the sentinel and asserts it too was dropped,
# so the "every session" claim is tested with more than one bystander present.
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
	-d "$PGC_DB" -c "SELECT pg_sleep(90);" >/dev/null 2>&1 &
reader=$!
sleep 0.5
crash_one_backend 3000000 crashC insert
wait "$reader" 2>/dev/null; reader_rc=$?
check "a second concurrent reader was also dropped by the crash" \
	"$( [ "$reader_rc" -ne 0 ] && echo yes || echo no )" "yes"
assert_intact "with-reader"

pgc_summary
