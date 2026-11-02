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

# nothing has been freed yet: truncate is a no-op.
check "truncate before any delete is a no-op" "$(q "SELECT pgcolumnar.truncate('n');")" "0"

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
check "second truncate is a no-op" "$(q "SELECT pgcolumnar.truncate('n');")" "0"

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
retired="$(pgc_retry_past_horizon 5 "SELECT pgcolumnar.compact('n');")"
echo "  (compact retired/calls: $retired)"
check_num "compact retired the 5 groups above id 35000" "${retired%/*}" "5"
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
guc_off="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq \
	-c "SET pgcolumnar.enable_end_truncation = off" -c "SELECT pgcolumnar.truncate('n')" 2>&1)"
check "GUC off: truncate does nothing" "$(size)|$guc_off" "$size_guard|0"
guc_on="$(pgc_retry_past_horizon 1 "SELECT pgcolumnar.truncate('n');")"
guc_on="${guc_on%/*}"
check "GUC on: truncate reclaims" \
	"$([ "$guc_on" -gt 0 ] 2>/dev/null && echo yes || echo "no ($guc_on)")" "yes"
check "GUC on: file shrank" "$([ "$(size)" -lt "$size_guard" ] && echo yes || echo "no ($(size) >= $size_guard)")" "yes"
check "parity after guard cycle" "$(hash_n)" "$(hash_h)"

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

pgc_summary
