#!/usr/bin/env bash
#
# A pgcolumnar write prepared and then finished in the SAME session (#1323).
#
# PREPARE deletes TopTransactionContext without ever reaching the COMMIT/ABORT
# arms of the transaction callback, and the pending write states and the
# delete-vector buffers live in children of it. Their statics were reset only by
# those arms, so the session's next transaction -- the COMMIT PREPARED itself --
# walked a freed list at its PRE_COMMIT and the backend died of SIGSEGV.
# COMMIT PREPARED from another session never touched them, which is the path
# parallel_copy takes, so nothing in the corpus had reached it.
#
# Each arm runs its statements in ONE psql session (several -c, each its own
# transaction), and asserts what the session returns. A crash shows twice: the
# session's last answer is missing, and the server log records a backend
# terminated by a signal. Not pg_postmaster_start_time: after a backend crash the
# postmaster reinitializes without restarting, and the time does not move -- the
# first version of this suite asserted it, and it passed on a run that crashed.
# Each arm waits for the server to accept connections first, so one crash is
# reported by its own arm and not by every arm after it.
#
# Usage:  test/prepared_writes.sh [PG_CONFIG]

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
export PGC_EXTRA_CONF='max_prepared_transactions=4'
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

q "CREATE EXTENSION IF NOT EXISTS pgcolumnar;" >/dev/null
check "premise: prepared transactions are enabled" "$(q "SHOW max_prepared_transactions;")" "4"
check "premise: the server log is where a crash would be recorded" \
	"$([ -f "$PGC_LOGFILE" ] && echo present || echo "absent ($PGC_LOGFILE)")" "present"

ready() {	# wait, up to 30 s, until the server answers a query
	local i
	for i in $(seq 1 60); do
		[ "$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
			-X -At -c "SELECT 1" 2>/dev/null)" = "1" ] && return
		sleep 0.5
	done
}

session() {	# session SQL... -> the last line the one session printed
	local args=()
	ready
	for s in "$@"; do args+=(-c "$s"); done
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At \
		"${args[@]}" 2>&1 | tail -1
}

q "CREATE TABLE pw (id int, v text) USING pgcolumnar;
   INSERT INTO pw SELECT g, 'base' FROM generate_series(1, 100) g;" >/dev/null

check "an insert prepared and committed in one session leaves the session answering" \
	"$(session "BEGIN" "INSERT INTO pw SELECT g, 'two-phase' FROM generate_series(101, 3100) g" \
		"PREPARE TRANSACTION 'pw_ins'" "COMMIT PREPARED 'pw_ins'" "SELECT count(*) FROM pw")" "3100"

check "a delete prepared and committed in one session leaves the session answering" \
	"$(session "BEGIN" "DELETE FROM pw WHERE id <= 50" \
		"PREPARE TRANSACTION 'pw_del'" "COMMIT PREPARED 'pw_del'" "SELECT count(*) FROM pw")" "3050"

check "an update prepared and committed in one session leaves the session answering" \
	"$(session "BEGIN" "UPDATE pw SET v = 'changed' WHERE id BETWEEN 51 AND 150" \
		"PREPARE TRANSACTION 'pw_upd'" "COMMIT PREPARED 'pw_upd'" \
		"SELECT count(*) FILTER (WHERE v = 'changed') FROM pw")" "100"

check "an insert prepared and rolled back in one session leaves the session answering" \
	"$(session "BEGIN" "INSERT INTO pw SELECT g, 'gone' FROM generate_series(1, 2000) g" \
		"PREPARE TRANSACTION 'pw_rb'" "ROLLBACK PREPARED 'pw_rb'" "SELECT count(*) FROM pw WHERE v = 'gone'")" "0"

check "and the session's next write, after COMMIT PREPARED, lands" \
	"$(session "BEGIN" "INSERT INTO pw SELECT g, 'later' FROM generate_series(1, 10) g" \
		"PREPARE TRANSACTION 'pw_next'" "COMMIT PREPARED 'pw_next'" \
		"INSERT INTO pw SELECT g, 'after' FROM generate_series(1, 7) g" \
		"SELECT count(*) FROM pw WHERE v = 'after'")" "7"

# The index-fetch cache is the third per-transaction cache COMMIT and ABORT reset.
# An index fetch on each side of COMMIT PREPARED, in one session, same rows: it
# works without a discard at PREPARE. Measured, not assumed -- a version of the
# fix that also discarded it at PREPARE changed no arm here, so it is not in the
# fix. The premise keeps this arm honest: with the custom scan on, the plan never
# reached the cache at all, and the arm passed by testing nothing.
ready	# the arm before may have crashed the server; the premise below must not read that
q "CREATE INDEX pw_id ON pw (id);" >/dev/null
check "premise: the fetch is an index scan, which is what reaches the fetch cache" \
	"$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At \
		-c "SET enable_seqscan = off" -c "SET enable_bitmapscan = off" -c "SET pgcolumnar.enable_custom_scan = off" \
		-c "EXPLAIN SELECT string_agg(v, ',') FROM pw WHERE id IN (120, 121, 122)" 2>&1 | grep -c 'Index Scan using pw_id')" "1"
check "an index fetch before PREPARE, and the same row fetched first after COMMIT PREPARED, in one session" \
	"$(session "SET enable_seqscan = off" "SET enable_bitmapscan = off" "SET pgcolumnar.enable_custom_scan = off" \
		"BEGIN" "SELECT string_agg(v, ',') FROM pw WHERE id IN (120, 121, 122)" "PREPARE TRANSACTION 'pw_fetch'" \
		"COMMIT PREPARED 'pw_fetch'" "SELECT string_agg(v, ',') FROM pw WHERE id IN (120, 121, 122)")" "changed,changed,changed"

ready
check "no backend was terminated by a signal" \
	"$(grep -c 'was terminated by signal' "$PGC_LOGFILE")" "0"
check "and no prepared transaction is left behind" "$(q "SELECT count(*) FROM pg_prepared_xacts;")" "0"

pgc_summary
