#!/usr/bin/env bash
#
# A prepared transaction, seen from other sessions and across a crash (#1341).
# prepared_writes.sh covers PREPARE and COMMIT PREPARED in one session. Untested
# until now:
#
#   - what another session sees while a transaction is prepared;
#   - the unique-key and row locks a prepared transaction still holds: a second
#     writer of the same key or row waits, then fails once COMMIT PREPARED runs;
#   - ROLLBACK PREPARED of a DELETE and of an UPDATE;
#   - a prepared insert surviving a crash and committing after recovery.
#
# The columnar table has a primary key; a heap table loaded with the same rows is the
# answer where one is needed. Waits are observed, not slept on.
#
# Usage:  test/prepared_across_sessions.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
export PGC_EXTRA_CONF='max_prepared_transactions=4'
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

check "premise: prepared transactions are enabled" "$(q "SHOW max_prepared_transactions")" "4"
psql_run "CREATE TABLE px (k int PRIMARY KEY, v text) USING pgcolumnar;
          INSERT INTO px SELECT g, 'r' || g FROM generate_series(1, 1000) g;"
rows() { q "SELECT count(*) || '/' || md5(string_agg(k || ':' || v, ',' ORDER BY k)) FROM px" | grep -E '^[0-9]+/' | tail -1; }
P() { env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq -v VERBOSITY=sqlstate "$@"; }
sqlstate_of() { sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\).*/\1/p' "$1" | tail -1; }
waits_on_lock() {	# waits_on_lock APPLICATION_NAME -> yes once that session waits on a lock
	local i
	for i in $(seq 1 300); do
		[ "$(q "SELECT count(*) FROM pg_stat_activity WHERE application_name = '$1' AND wait_event_type = 'Lock'")" = 1 ] && { echo yes; return; }
		sleep 0.1
	done
	echo no
}

# ---- a prepared insert, from another session ------------------------------------------
psql_run "BEGIN; INSERT INTO px SELECT g, 'p' || g FROM generate_series(1001, 1100) g; PREPARE TRANSACTION 'px_ins';" >/dev/null
check "a prepared insert is invisible to another session" "$(q "SELECT count(*) FROM px WHERE k > 1000")" "0"
P -c "SET application_name = 'px_dup'; SET statement_timeout = '60s';" -c "INSERT INTO px VALUES (1050, 'dup');" \
	>"$PGC_WORKDIR/px_dup.out" 2>&1 &
dup=$!
waited="$(waits_on_lock px_dup)"
psql_run "COMMIT PREPARED 'px_ins';" >/dev/null
wait "$dup"
check "a second insert of a key the prepared transaction holds waits, then fails with 23505 after COMMIT PREPARED" \
	"$waited|$(sqlstate_of "$PGC_WORKDIR/px_dup.out")|$(q "SELECT count(*) || '/' || min(v) FROM px WHERE k = 1050")" "yes|23505|1/p1050"
check "and after COMMIT PREPARED the other session sees all the prepared rows" "$(q "SELECT count(*) FROM px WHERE k > 1000")" "100"

# ---- ROLLBACK PREPARED of a DELETE and of an UPDATE -----------------------------------
before="$(rows)"
psql_run "BEGIN; DELETE FROM px WHERE k % 20 = 0; PREPARE TRANSACTION 'px_del';" >/dev/null
# Without these premises a PREPARE that failed would leave the table unchanged, and
# the checks after it would compare it with itself (OffgridwithJD, review of #1428).
check "premise: the DELETE is prepared" "$(q "SELECT count(*) FROM pg_prepared_xacts WHERE gid = 'px_del'")" "1"
check "while a DELETE is prepared, another session still sees every row" "$(rows)" "$before"
psql_run "ROLLBACK PREPARED 'px_del';" >/dev/null
check "ROLLBACK PREPARED of a DELETE leaves every row as it was" "$(rows)" "$before"
psql_run "BEGIN; UPDATE px SET v = v || '!' WHERE k % 20 = 1; PREPARE TRANSACTION 'px_upd';" >/dev/null
check "premise: the UPDATE is prepared" "$(q "SELECT count(*) FROM pg_prepared_xacts WHERE gid = 'px_upd'")" "1"
psql_run "ROLLBACK PREPARED 'px_upd';" >/dev/null
check "ROLLBACK PREPARED of an UPDATE leaves every row as it was, none duplicated" "$(rows)" "$before"

# ---- a row lock carried across PREPARE --------------------------------------------------
psql_run "BEGIN; UPDATE px SET v = 'prepared' WHERE k = 7; PREPARE TRANSACTION 'px_row';" >/dev/null
P -c "SET application_name = 'px_rowc'; SET statement_timeout = '60s';" -c "UPDATE px SET v = 'other' WHERE k = 7;" \
	>"$PGC_WORKDIR/px_rowc.out" 2>&1 &
rc=$!
waited="$(waits_on_lock px_rowc)"
psql_run "COMMIT PREPARED 'px_row';" >/dev/null
wait "$rc"
check "an UPDATE of a row a prepared transaction updated waits, then fails with 40001 after COMMIT PREPARED" \
	"$waited|$(sqlstate_of "$PGC_WORKDIR/px_rowc.out")|$(q "SELECT count(*) || '/' || min(v) FROM px WHERE k = 7")" "yes|40001|1/prepared"

# ---- a prepared insert across a crash -------------------------------------------------
crash_cluster() {
	local pm i
	pm="$(head -1 "$PGC_PGDATA/postmaster.pid" 2>/dev/null || true)"
	[ -n "$pm" ] && { kill -9 -"$pm" 2>/dev/null || true; kill -9 "$pm" 2>/dev/null || true; }
	for i in $(seq 1 100); do { [ -n "$pm" ] && kill -0 "$pm" 2>/dev/null; } || break; sleep 0.1; done
	sleep 1
}
restart_cluster() {
	local attempt
	for attempt in 1 2 3 4 5 6; do
		pgc_pg "pg_ctl -D '$PGC_PGDATA' -l '$PGC_LOGFILE' start -w" >/dev/null 2>&1 && { echo ok; return; }
		sleep 2
	done
	echo "restart failed"
}
psql_run "CREATE TABLE px_heap (k int PRIMARY KEY, v text);
          INSERT INTO px_heap SELECT g, md5(g::text) FROM generate_series(5001, 10000) g;
          BEGIN; INSERT INTO px SELECT g, md5(g::text) FROM generate_series(5001, 10000) g; PREPARE TRANSACTION 'px_crash';" >/dev/null
crash_cluster
back="$(restart_cluster)"
check "a prepared insert survives a crash: the cluster came back and still holds the prepared transaction" \
	"$back|$(q "SELECT count(*) FROM pg_prepared_xacts WHERE gid = 'px_crash'")" "ok|1"
psql_run "COMMIT PREPARED 'px_crash';" >/dev/null
check "and after COMMIT PREPARED its rows are heap's" \
	"$(q "SELECT count(*) || '/' || md5(string_agg(k || ':' || v, ',' ORDER BY k)) FROM px WHERE k > 5000")" \
	"$(q "SELECT count(*) || '/' || md5(string_agg(k || ':' || v, ',' ORDER BY k)) FROM px_heap")"

pgc_summary
