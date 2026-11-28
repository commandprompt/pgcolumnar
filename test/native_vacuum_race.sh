#!/usr/bin/env bash
#
# Regression for #295: compaction must not drop rows committed by another
# transaction while it waited for its lock. pgcolumnar_compact_relation used the
# caller's pre-lock statement snapshot, so a row group committed after that
# snapshot was invisible to the rewrite and was destroyed by the relfilenode swap.
# The fix takes a fresh snapshot after the lock.
#
# Here session A inserts 100 rows and holds its transaction open. Session B runs
# one READ COMMITTED statement that counts the table and then calls the
# maintenance op: the count runs at once, under the statement snapshot, and the op
# waits for its AccessExclusiveLock until A commits. So the op starts under a
# statement snapshot that does not hold A's rows, and all 150 must survive.
#
# This used to run B at REPEATABLE READ, with its snapshot pinned before A's
# commit. That is #2167's case: the op then made A's rows visible to B's old
# snapshot. Since #2167 the op refuses there with 40001 (test/rewrite_snapshot.sh),
# so #295's window is the lock wait at READ COMMITTED, as the issue describes.
# Covers vacuum() and vacuum_sorted() (pgcolumnar_compact_relation) and cluster()
# (its Z-order twin). Written fresh for pgColumnar.
#
# Usage:  test/native_vacuum_race.sh [PG_CONFIG]

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

raw() { env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -tAX "$@"; }
OPTS="-c statement_timeout=60s -c lock_timeout=30s"

# Run the race for one maintenance call. What B's statement counted before its
# lock wait goes to $PGC_WORKDIR/<table>.b, and whether B was seen waiting for its
# lock to $PGC_WORKDIR/<table>.waiting; the surviving row count is echoed.
race() {  # maintenance_call  table
	local call="$1" tbl="$2" gate a b i waiting=no
	psql_run "DROP TABLE IF EXISTS $tbl;" >/dev/null 2>&1
	psql_run "CREATE TABLE $tbl (id int) USING pgcolumnar;" >/dev/null
	psql_run "INSERT INTO $tbl SELECT g FROM generate_series(1,50) g;" >/dev/null
	# A gate session holds advisory lock 4295 until B is waiting.
	PGOPTIONS="$OPTS" raw -c "SET application_name = 'nvr_gate'" -c "SELECT pg_advisory_lock(4295)" \
		-c "SELECT pg_sleep(50)" >/dev/null 2>&1 &
	gate=$!
	for i in $(seq 1 300); do
		[ "$(q "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity s ON s.pid = l.pid
		        WHERE s.application_name = 'nvr_gate' AND l.locktype = 'advisory' AND l.granted")" = 1 ] && break
		sleep 0.1
	done
	# A: insert 100 rows, then wait on the gate with its transaction open.
	PGOPTIONS="$OPTS" raw -c "SET application_name = 'nvr_a'" -c "BEGIN" \
		-c "INSERT INTO $tbl SELECT g FROM generate_series(51,150) g" \
		-c "SELECT pg_advisory_lock(4295)" -c "COMMIT" >/dev/null 2>&1 &
	a=$!
	for i in $(seq 1 300); do
		[ "$(q "SELECT count(*) FROM pg_stat_activity WHERE application_name = 'nvr_a' AND wait_event_type = 'Lock'")" = 1 ] && break
		sleep 0.1
	done
	# B: count, then the op, in one READ COMMITTED statement; the op waits for A.
	PGOPTIONS="$OPTS" raw -c "SET application_name = 'nvr_b'" \
		-c "SELECT (SELECT count(*) FROM $tbl), $call" > "$PGC_WORKDIR/$tbl.b" 2>&1 &
	b=$!
	for i in $(seq 1 300); do
		[ "$(q "SELECT count(*) FROM pg_stat_activity WHERE application_name = 'nvr_b' AND wait_event_type = 'Lock'")" = 1 ] && { waiting=yes; break; }
		sleep 0.1
	done
	echo "$waiting" > "$PGC_WORKDIR/$tbl.waiting"
	q "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = 'nvr_gate'" >/dev/null
	wait "$gate" 2>/dev/null; wait "$a"; wait "$b"
	q "SELECT count(*) FROM $tbl"
}
# The precondition for the defect, checked rather than assumed (#1339): B's statement
# counted 50, so its snapshot did not hold A's rows, and B was seen waiting for its
# lock while A's transaction was open. A B that started after A's commit counts 150.
in_snapshot() {  # table -> "<what B's statement counted>|<B was seen waiting>"
	echo "$(grep -v "^SET$" "$PGC_WORKDIR/$1.b" | cut -d'|' -f1 | head -1)|$(cat "$PGC_WORKDIR/$1.waiting")"
}

check "vacuum() keeps rows committed during its snapshot window (#295)" \
	"$(race "pgcolumnar.vacuum('r_vac')" r_vac)" "150"
check "premise: vacuum() -- A's rows are outside B's snapshot" "$(in_snapshot r_vac)" "50|yes"
check "vacuum_sorted() keeps rows committed during its snapshot window (#295)" \
	"$(race "pgcolumnar.vacuum_sorted('r_vsort','id')" r_vsort)" "150"
check "premise: vacuum_sorted() -- A's rows are outside B's snapshot" "$(in_snapshot r_vsort)" "50|yes"
check "cluster() keeps rows committed during its snapshot window (#295)" \
	"$(race "pgcolumnar.cluster('r_clu','id')" r_clu)" "150"
check "premise: cluster() -- A's rows are outside B's snapshot" "$(in_snapshot r_clu)" "50|yes"

# And the data is actually correct, not just the count: exactly the 150 distinct
# ids 1..150 are present (no loss, no duplication). For each call, not only
# vacuum() (#1339): a vacuum_sorted() or cluster() rewrite that lost 20 rows and
# duplicated 20 others still counted 150.
check "vacuum() preserves the exact row set" \
	"$(q 'SELECT count(DISTINCT id) FROM r_vac WHERE id BETWEEN 1 AND 150')" "150"
check "vacuum_sorted() preserves the exact row set" \
	"$(q 'SELECT count(DISTINCT id) FROM r_vsort WHERE id BETWEEN 1 AND 150')" "150"
check "cluster() preserves the exact row set" \
	"$(q 'SELECT count(DISTINCT id) FROM r_clu WHERE id BETWEEN 1 AND 150')" "150"

pgc_summary
