#!/usr/bin/env bash
#
# pgColumnar physical end-truncation (Phase F): pgcolumnar.truncate() returns
# trailing reclaimed blocks to the OS. This functional suite proves the file
# actually shrinks after deletes+compaction, data stays correct against a heap
# mirror, the table is fully usable afterward, truncation is idempotent, the GUC
# disables it, and -- critically for the corruption-safety of the abort path --
# truncate REFUSES to run inside a transaction block (its physical shrink is not
# transactional, so a rollback around it must be impossible), and a normal
# truncate followed by reinsert and compaction stays corruption-free (the
# assert-build no-overlap validator runs inside compact/truncate).
#
# Concurrency is covered separately by the isolation specs
# (specs/truncate_vs_*.spec).
#
# Usage:  test/native_truncate.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
# A prepared transaction in another database is one of the holders the last arm
# opens, and max_prepared_transactions can only be set before the server starts.
export PGC_EXTRA_CONF="max_prepared_transactions=4"
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

raw() { env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq -c "$1" 2>&1; }
size() { q "SELECT pg_relation_size('n');"; }
hash_n() { pgc_set_hash 'SELECT id, v FROM n'; }
hash_h() { pgc_set_hash 'SELECT id, v FROM h'; }

# end-truncation is opt-in (GUC default off); enable it for the whole test db.
psql_run "ALTER DATABASE $PGC_DB SET pgcolumnar.enable_end_truncation = on;"

# NO AUTOVACUUM ON THE FIXTURE (#1979): an autovacuum would retire the dead groups
# itself, and the compact calls below would then find none to retire.
psql_run "CREATE TABLE h (id int, v text) WITH (autovacuum_enabled = false);"
psql_run "CREATE TABLE n (id int, v text) USING pgcolumnar WITH (autovacuum_enabled = false);"
psql_run "SELECT pgcolumnar.set_options('n', stripe_row_limit => 1000, chunk_group_row_limit => 1000);"
psql_run "INSERT INTO h SELECT g, md5(g::text) FROM generate_series(1, 30000) g;"
psql_run "INSERT INTO n SELECT g, md5(g::text) FROM generate_series(1, 30000) g;"
check "load parity" "$(hash_n)" "$(hash_h)"
size_full="$(size)"

# NO OTHER SESSION MAY LOCK n WHEN A CHECK WANTS 0 (#1994). truncate returns 0,
# and leaves the file alone, when it cannot take its ACCESS EXCLUSIVE lock on n at
# once. That happens while another session holds a lock weaker than SHARE UPDATE
# EXCLUSIVE on n (such as ACCESS SHARE), or waits for any lock on it. Then a check
# that wants 0 passed whatever truncate would have done. So each such call runs
# through truncate_alone, and its lock counts are checked as well.
LOCKS_ON_N="SELECT count(*) FROM pg_locks
             WHERE locktype = 'relation' AND relation = 'n'::regclass
               AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
               AND pid <> pg_backend_pid()"
# truncate_alone [SQL...] -- in one session: run each SQL, count the locks other
# sessions hold or wait for on n, call truncate on n, and count them again. Each is
# its own statement, so the call is not in a transaction block. Sets trunc_locks
# ("before|after") and trunc_out (what truncate returned).
truncate_alone() {
	local s out
	local -a cmds=()
	for s in "$@"; do cmds+=(-c "$s"); done
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq \
		"${cmds[@]}" -c "$LOCKS_ON_N" -c "SELECT pgcolumnar.truncate('n')" -c "$LOCKS_ON_N" 2>&1)"
	trunc_locks="$(sed -n '1p;3p' <<<"$out" | paste -sd'|')"
	trunc_out="$(sed -n '2p' <<<"$out")"
}

# nothing has been freed yet: truncate is a no-op.
truncate_alone
check "truncate before any delete: no other session held a lock on n before or after the call" "$trunc_locks" "0|0"
check "truncate before any delete is a no-op" "$trunc_out" "0"

# fully delete the trailing half (groups 16..30), compact to free them.
#
# WAIT FOR THE HORIZON (#1979), TWICE. compact retires a dead group only when no
# transaction in the database is older than the DELETE, and truncate removes a
# freed range only when no transaction is older than the compact that freed it.
# With one call each, a snapshot held by another session left nothing to
# truncate. pgc_retry_past_horizon calls each one again, for up to 30 s, and the
# counts are checked.
psql_run "DELETE FROM h WHERE id > 15000;"
psql_run "DELETE FROM n WHERE id > 15000;"
retired="$(pgc_retry_past_horizon 15 "SELECT pgcolumnar.compact('n');")"
echo "  (compact retired/calls: $retired)"
check_num "compact retired the 15 trailing groups" "${retired%/*}" "15"
size_before="$(size)"

trunc="$(pgc_retry_past_horizon 1 "SELECT pgcolumnar.truncate('n');")"
trunc="${trunc%/*}"
echo "  (truncate returned $trunc blocks; size full=$size_full before-trunc=$size_before after=$(size))"
check "truncate returned blocks" "$([ "$trunc" -gt 0 ] 2>/dev/null && echo yes || echo "no ($trunc)")" "yes"
check "file shrank" "$([ "$(size)" -lt "$size_before" ] && echo yes || echo "no ($(size) >= $size_before)")" "yes"
check "parity after truncate" "$(hash_n)" "$(hash_h)"
check "row count after truncate" "$(q 'SELECT count(*) FROM n;')" "15000"

# idempotent: a second truncate has nothing left to do.
truncate_alone
check "second truncate: no other session held a lock on n before or after the call" "$trunc_locks" "0|0"
check "second truncate is a no-op" "$trunc_out" "0"

# fully usable after truncation: reinsert, index, indexed lookup, delete.
psql_run "INSERT INTO h SELECT g, md5(g::text) FROM generate_series(30001, 40000) g;"
psql_run "INSERT INTO n SELECT g, md5(g::text) FROM generate_series(30001, 40000) g;"
check "parity after post-truncate insert" "$(hash_n)" "$(hash_h)"
psql_run "CREATE INDEX n_id ON n (id);"
# Forced to fetch every row through n_id, and asserted. With the defaults this
# count plans an Index Only Scan (PG17 and PG18), which fetches each row only while
# the visibility map leaves its page unmarked: once a VACUUM marks them it answers
# from the index alone, whatever the fetch does. count(v), not count(*), with the
# custom scan, seqscan and bitmap scans off, so the plan is an Index Scan.
IDX_SETS="SET enable_seqscan = off; SET enable_bitmapscan = off; SET pgcolumnar.enable_custom_scan = off;"
check "premise: the indexed lookup reads n_id" \
	"$(pgc_uses_row_fetch "$IDX_SETS" "SELECT count(v) FROM n WHERE id BETWEEN 1 AND 40000")" yes
check "indexed lookup after truncate" \
	"$(q "$IDX_SETS SELECT count(v) FROM n WHERE id BETWEEN 1 AND 40000;" | tail -1)" "25000"

# GUC off: truncate is a no-op even with reclaimable trailing space.
psql_run "DELETE FROM h WHERE id > 35000;"
psql_run "DELETE FROM n WHERE id > 35000;"
# A SNAPSHOT IN ANOTHER DATABASE (#1992). truncate's horizon for a user table
# ignores a snapshot with no transaction ID in another database. So a snapshot
# taken in database postgres before the compact below must not count as holding
# the freed range. (A writer there does hold it: see the last arm.) It stays
# open until after the GUC-on truncate, which shows that truncate is not held.
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d postgres -Atq \
	-c "SET application_name = 'nt_xdb_holder'" -c "SELECT pg_sleep(120)" >/dev/null 2>&1 &
xdb_pid=$!
for _ in $(seq 1 40); do
	[ "$(q "SELECT count(*) FROM pg_stat_activity WHERE application_name = 'nt_xdb_holder' AND wait_event = 'PgSleep'")" = 1 ] && break
	sleep 0.25
done
retired="$(pgc_retry_past_horizon 5 "SELECT pgcolumnar.compact('n');")"
echo "  (compact retired/calls: $retired)"
check_num "compact retired the 5 groups above id 35000" "${retired%/*}" "5"
check_num "premise: a snapshot older than the compact is open in another database" \
	"$(q "WITH f AS (SELECT min(age(freed_xid::text::xid)) AS a FROM pgcolumnar.free_space
	                 WHERE storage_id = pgcolumnar.get_storage_id('n') AND freed_xid >= 3)
	      SELECT count(*) FROM pg_stat_activity, f
	       WHERE application_name = 'nt_xdb_holder' AND datname <> current_database()
	         AND age(backend_xmin) >= f.a;")" "1"
size_guard="$(size)"
# WAIT FOR THE HORIZON FIRST (#1988). truncate also leaves the file alone while
# any transaction older than the compact above is open. Then this arm passed
# whatever the setting said, and a build that ignored the setting stayed green.
# So the call waits until nothing holds the horizon at the freed range, and the
# count of what still holds it is checked.
blockers="$(pgc_wait_freed_range_past_horizon n)"
echo "  (horizon blockers/calls before the GUC-off call: $blockers)"
check_num "GUC off: no transaction older than the compact was open" "${blockers%/*}" "0"
# Two -c statements in one session, and the call's own answer checked. As one string,
# "SET ...; SELECT pgcolumnar.truncate('n')" is an implicit transaction block, which
# truncate refuses: the arm passed on an unchanged file whatever the GUC did, and
# stayed green with the GUC not consulted at all.
#
# AND NO OTHER SESSION MAY LOCK n (#1994): see truncate_alone.
truncate_alone "SET pgcolumnar.enable_end_truncation = off"
check "GUC off: no other session held a lock on n before or after the call" "$trunc_locks" "0|0"
check "GUC off: truncate does nothing" "$(size)|$trunc_out" "$size_guard|0"
guc_on="$(pgc_retry_past_horizon 1 "SELECT pgcolumnar.truncate('n');")"
guc_on="${guc_on%/*}"
check "GUC on: truncate reclaims" \
	"$([ "$guc_on" -gt 0 ] 2>/dev/null && echo yes || echo "no ($guc_on)")" "yes"
check "GUC on: file shrank" "$([ "$(size)" -lt "$size_guard" ] && echo yes || echo "no ($(size) >= $size_guard)")" "yes"
check_num "the other-database snapshot was still open after the GUC-on truncate" \
	"$(q "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity
	       WHERE application_name = 'nt_xdb_holder' AND backend_xmin IS NOT NULL;")" "1"
wait "$xdb_pid" 2>/dev/null
check "parity after guard cycle" "$(hash_n)" "$(hash_h)"

# THE HELPER REFUSES A TABLE WITH NO FREED RANGE (#1992). With no freed_xid to
# compare against, every comparison is NULL and the count would read 0, a pass
# that measured nothing. It must give no count instead.
psql_run "CREATE TABLE nf (id int) USING pgcolumnar WITH (autovacuum_enabled = false);"
check "a table with no freed range gives the horizon helper no count" \
	"$(pgc_wait_freed_range_past_horizon nf 2>/dev/null)" "none/1"

# TRANSACTION-BLOCK GUARD: truncate must refuse inside a transaction block, so a
# ROLLBACK can never leave the metapage highwater lowered while the transactional
# free_space deletes roll back (the inverted, corruptible state).
psql_run "DELETE FROM h WHERE id BETWEEN 30001 AND 33000;"
psql_run "DELETE FROM n WHERE id BETWEEN 30001 AND 33000;"
retired="$(pgc_retry_past_horizon 3 "SELECT pgcolumnar.compact('n');")"
echo "  (compact retired/calls: $retired)"
check_num "compact retired the 3 groups from id 30001 to 33000" "${retired%/*}" "3"
before_guard="$(size)"
errout="$(raw "BEGIN; SELECT pgcolumnar.truncate('n'); ROLLBACK;")"
check "truncate refused inside a transaction block" \
	"$(grep -qi 'cannot run inside a transaction block' <<<"$errout" && echo yes || echo no)" "yes"
check "file unchanged after refused in-txn truncate" "$(size)" "$before_guard"
check "parity after refused in-txn truncate" "$(hash_n)" "$(hash_h)"

# a normal (autocommit) truncate, then reinsert into the reclaimed low space, then
# compaction: the no-overlap validator runs inside compact_rewrite/compact and
# must stay green, and parity must hold -- i.e. no double-allocation over reused
# blocks.
psql_run "SELECT pgcolumnar.truncate('n');" >/dev/null
psql_run "INSERT INTO h SELECT g, md5(g::text) FROM generate_series(41001, 43000) g;"
psql_run "INSERT INTO n SELECT g, md5(g::text) FROM generate_series(41001, 43000) g;"
psql_run "DELETE FROM h WHERE id > 42000;"
psql_run "DELETE FROM n WHERE id > 42000;"
psql_run "SELECT pgcolumnar.compact_rewrite('n', 0.0);"
psql_run "SELECT pgcolumnar.compact('n');"
check "parity after truncate + reinsert + compaction" "$(hash_n)" "$(hash_h)"

# WHAT HOLDS TRUNCATE BACK, THE HELPER COUNTS (#1992). truncate computes its
# horizon with its own snapshot, and that snapshot's xmin is the oldest
# transaction ID running in ANY database. So a session in another database holds
# truncate back when it has a transaction ID: a writer, or a prepared
# transaction. Only a snapshot with no transaction ID there does not. A snapshot
# in this database taken just before the compact holds it too: its xmin is the
# compact's own transaction ID, so the helper must count an xmin equal to
# freed_xid. For each holder: open it after the DELETE, compact, check that
# truncate is held and that the helper counts the holder, then end the holder
# and check that truncate reclaims.
psql_run "CREATE TABLE xw (id int) USING pgcolumnar WITH (autovacuum_enabled = false);"
psql_run "SELECT pgcolumnar.set_options('xw', stripe_row_limit => 1000, chunk_group_row_limit => 1000);"
psql_run "INSERT INTO xw SELECT g FROM generate_series(1, 12000) g;"
# xw_hold APP DB SQL... -- run SQL in a background session named APP (its pid in
# xw_pid), then wait until that session sleeps in its last statement.
xw_hold() {
	local app="$1" db="$2" s
	local -a cmds=()
	shift 2
	for s in "$@"; do cmds+=(-c "$s"); done
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$db" -Atq \
		-c "SET application_name = '$app'" "${cmds[@]}" >/dev/null 2>&1 &
	xw_pid=$!
	for _ in $(seq 1 40); do
		[ "$(q "SELECT count(*) FROM pg_stat_activity WHERE application_name = '$app' AND wait_event = 'PgSleep'")" = 1 ] && break
		sleep 0.25
	done
}
# xw_end APP -- end the background session named APP.
xw_end() { q "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE application_name = '$1';" >/dev/null; wait "$xw_pid" 2>/dev/null; }
# xw_compact -- compact xw and print what it retired.
xw_compact() { echo "  (compact of xw retired/calls: $(pgc_retry_past_horizon 3 "SELECT pgcolumnar.compact('xw');"))"; }
# xw_counted -- "yes" when the helper counts at least one holder of xw's freed range.
xw_counted() {
	local n
	n="$(pgc_freed_range_blockers xw)"
	echo "  (horizon blockers of xw: ${n:-none})" >&2
	[ "${n:-0}" -ge 1 ] 2>/dev/null && echo yes || echo "no (${n:-none})"
}
# xw_reclaims -- "yes" when truncate reclaims, waiting for the horizon.
xw_reclaims() {
	local r
	r="$(pgc_retry_past_horizon 1 "SELECT pgcolumnar.truncate('xw');")"
	echo "  (truncate of xw returned/calls: $r)" >&2
	[ "${r%/*}" -gt 0 ] 2>/dev/null && echo yes || echo "no (${r%/*})"
}

psql_run "DELETE FROM xw WHERE id > 9000;"
xw_hold nt_xdb_writer postgres "BEGIN" "SELECT txid_current()" "SELECT pg_sleep(120)"
xw_compact
check "premise: truncate is held while a writer in another database is open" \
	"$(q "SELECT pgcolumnar.truncate('xw');")" "0"
check "the horizon helper counts a writer in another database" "$(xw_counted)" "yes"
xw_end nt_xdb_writer
check "truncate reclaims once the writer in another database ends" "$(xw_reclaims)" "yes"

psql_run "DELETE FROM xw WHERE id > 6000;"
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d postgres -Atq \
	-c "BEGIN" -c "SELECT txid_current()" -c "PREPARE TRANSACTION 'nt_xdb_prep'" >/dev/null 2>&1
xw_compact
check "premise: truncate is held while a prepared transaction in another database is open" \
	"$(q "SELECT pgcolumnar.truncate('xw');")" "0"
check "the horizon helper counts a prepared transaction in another database" "$(xw_counted)" "yes"
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d postgres -Atq \
	-c "ROLLBACK PREPARED 'nt_xdb_prep'" >/dev/null
check "truncate reclaims once the prepared transaction in another database ends" "$(xw_reclaims)" "yes"

psql_run "DELETE FROM xw WHERE id > 3000;"
xw_hold nt_samedb_reader "$PGC_DB" "SELECT pg_sleep(120)"
xw_compact
check "premise: truncate is held while a snapshot from just before the compact is open" \
	"$(q "SELECT pgcolumnar.truncate('xw');")" "0"
check "the horizon helper counts a snapshot from just before the compact" "$(xw_counted)" "yes"
xw_end nt_samedb_reader
check "truncate reclaims once the snapshot from just before the compact ends" "$(xw_reclaims)" "yes"

pgc_summary
