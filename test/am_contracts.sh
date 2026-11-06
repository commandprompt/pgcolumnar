#!/usr/bin/env bash
#
# Contracts the access method owes core DML and transactions, each one stated in
# docs/ or owed by core's table-AM interface, and none of them tested before #1341.
# One section per contract, each a promise someone relies on without reading the C.
#
# Usage:  test/am_contracts.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# state SQL -> "ok" when it succeeds, or the SQLSTATE it raised. A refusal is decided by
# its code, not its wording.
state() {
	local out
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-At -v VERBOSITY=sqlstate -v ON_ERROR_STOP=1 -c "$1" 2>&1)" && { echo ok; return; }
	sed -n 's/^.*ERROR:[[:space:]]*\([0-9A-Z]\{5\}\).*$/\1/p' <<<"$out" | head -1
}

# ---- INSERT ... ON CONFLICT (docs/limitations.md:655-656) ----------------------
#
# The documented contract: DO NOTHING "works", and DO UPDATE "takes a row lock and
# raises the same error" as other row locking. Both on a table whose unique index is
# what ON CONFLICT arbitrates on -- the case anybody uses it for.
psql_run "CREATE TABLE oc (id int PRIMARY KEY, v text) USING pgcolumnar;"
psql_run "INSERT INTO oc SELECT g, 'v' || g FROM generate_series(1, 10) g;"
check "premise: the conflict table holds ten rows" "$(q 'SELECT count(*) FROM oc')" "10"

check "ON CONFLICT DO NOTHING skips the duplicate and inserts the new row" \
	"$(state "INSERT INTO oc VALUES (5, 'dup'), (11, 'new') ON CONFLICT DO NOTHING;")|$(q "SELECT count(*) || '|' || (SELECT v FROM oc WHERE id = 5) FROM oc")" \
	"ok|11|v5"
check "and so does ON CONFLICT (id) DO NOTHING, naming its arbiter" \
	"$(state "INSERT INTO oc VALUES (6, 'dup'), (12, 'new') ON CONFLICT (id) DO NOTHING;")|$(q "SELECT count(*) || '|' || (SELECT v FROM oc WHERE id = 6) FROM oc")" \
	"ok|12|v6"
check "ON CONFLICT DO UPDATE is refused as unsupported (0A000), and changes nothing" \
	"$(state "INSERT INTO oc VALUES (7, 'upd') ON CONFLICT (id) DO UPDATE SET v = excluded.v;")|$(q "SELECT count(*) || '|' || (SELECT v FROM oc WHERE id = 7) FROM oc")" \
	"0A000|12|v7"

check "ON CONFLICT DO UPDATE with no conflicting row inserts it" \
	"$(state "INSERT INTO oc VALUES (13, 'upd') ON CONFLICT (id) DO UPDATE SET v = excluded.v;")|$(q "SELECT count(*) || '|' || (SELECT v FROM oc WHERE id = 13) FROM oc")" \
	"ok|13|upd"
check "a key twice in one DO NOTHING statement is inserted once, the first time" \
	"$(state "INSERT INTO oc VALUES (30, 'first'), (30, 'second') ON CONFLICT DO NOTHING;")|$(q "SELECT count(*) || '|' || min(v) FROM oc WHERE id = 30")" \
	"ok|1|first"

# THE WITHDRAWAL PATH. A row that looked free at the pre-check but conflicts once
# inserted, because another session inserted the key and had not committed. The
# first session holds key 20 uncommitted; the second runs DO NOTHING on 20 and 21,
# waits, and must end with exactly one row 20 -- the first session's -- and its 21.
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq \
	-c "BEGIN; INSERT INTO oc VALUES (20, 'holder'); SELECT pg_sleep(3); COMMIT;" >/dev/null 2>&1 &
_oc_holder=$!
sleep 1
_oc_racer="$(state "INSERT INTO oc VALUES (20, 'racer'), (21, 'racer') ON CONFLICT DO NOTHING;")"
wait "$_oc_holder"
check "a DO NOTHING racing an uncommitted insert of the same key waits, then skips it" \
	"$_oc_racer|$(q "SELECT count(*) FILTER (WHERE id = 20) || '|' || min(v) FILTER (WHERE id = 20) || '|' || count(*) FILTER (WHERE id = 21) FROM oc")" \
	"ok|1|holder|1"
check "and the unique index answers the key with that one row" \
	"$(q "SET enable_seqscan = off; SELECT count(*) || '|' || min(v) FROM oc WHERE id = 20;" | tail -1)" "1|holder"

# THE WITHDRAWAL ITSELF, which the arm above does not reach: there the pre-check sees
# the holder's row and waits. A row is withdrawn only when another session inserts
# the key after this one's pre-check and before its own insert -- a window no
# ordering of two sessions can force. So eight sessions race on the same keys, each
# taken from the clock, for a few seconds. Measured: 0 duplicate keys on three runs;
# with the withdrawal disabled, 407 to 507.
psql_run "CREATE TABLE ocs (id bigint PRIMARY KEY, w int) USING pgcolumnar;"
_oc_sql="$PGC_WORKDIR/oc_race.sql"
for _i in $(seq 1 1500); do
	echo "INSERT INTO ocs VALUES ((extract(epoch FROM clock_timestamp()) * 200)::bigint, 1) ON CONFLICT DO NOTHING;"
done > "$_oc_sql"
_oc_pids=""
for _w in 1 2 3 4 5 6 7 8; do
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -q \
		-f "$_oc_sql" > "$PGC_WORKDIR/oc_race.$_w.out" 2>&1 &
	_oc_pids="$_oc_pids $!"
done
wait $_oc_pids
_oc_errors="$(cat "$PGC_WORKDIR"/oc_race.*.out | grep -c 'ERROR' || true)"
check "premise: the racing sessions contended for keys" \
	"$(q "SELECT (count(*) < 8 * 1500)::text FROM ocs")" "true"
check "sessions racing DO NOTHING on the same keys leave each key once, with no error" \
	"$(q "SELECT count(*) - count(DISTINCT id) FROM ocs")|$_oc_errors" "0|0"

# ---- a TRUNCATE that rolls back restores every row (#1341) -------------------------
#
# TRUNCATE swaps the table onto new storage (set_new_filelocator), so the old
# storage's rows and catalog entries must survive until the swap commits, and come
# back whole if it does not -- at transaction level and to a savepoint.
psql_run "CREATE TABLE tr (id int PRIMARY KEY, v int) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('tr', stripe_row_limit => 1000);"
psql_run "INSERT INTO tr SELECT g, g * 3 FROM generate_series(1, 5000) g;"
_tr_before="$(q "SELECT count(*) || '|' || sum(v) FROM tr")"
check "premise: the truncate table holds 5000 rows" "$_tr_before" "5000|37507500"
psql_run "BEGIN; TRUNCATE tr; ROLLBACK;"
check "a TRUNCATE rolled back at transaction level restores every row" \
	"$(q "SELECT count(*) || '|' || sum(v) FROM tr")" "$_tr_before"
check "and the primary key still finds them by index" \
	"$(q "SET enable_seqscan = off; SELECT v FROM tr WHERE id = 4321;" | tail -1)" "12963"
psql_run "BEGIN; SAVEPOINT s; TRUNCATE tr; ROLLBACK TO SAVEPOINT s; INSERT INTO tr VALUES (5001, 1); COMMIT;"
check "a TRUNCATE rolled back to a savepoint restores every row, and the transaction goes on" \
	"$(q "SELECT count(*) || '|' || sum(v) FROM tr")" "5001|37507501"
check "and the primary key still refuses a duplicate of a restored row" \
	"$(state "INSERT INTO tr VALUES (17, 0);")" "23505"

# AND THE ROWS THE PARENT BUFFERED BEFORE THE SAVEPOINT. TRUNCATE discards the cached
# write state; rows inserted earlier in the same transaction may still be in it,
# unflushed, and ROLLBACK TO SAVEPOINT must bring them back with the table. 2500 rows,
# not a multiple of the 1000-row stripe, so the last 500 are still buffered.
psql_run "BEGIN; INSERT INTO tr SELECT g, 1 FROM generate_series(6001, 8500) g; SAVEPOINT s; TRUNCATE tr; INSERT INTO tr SELECT g, 2 FROM generate_series(1, 5) g; ROLLBACK TO SAVEPOINT s; COMMIT;"
check "rows the parent buffered before a savepoint survive a TRUNCATE rolled back to it" \
	"$(q "SELECT count(*) || '|' || sum(v) FROM tr")" "7501|37510001"

# ---- an aborted inner block keeps the outer statement's rows (#1341) ---------------
#
# A plpgsql EXCEPTION block is a subtransaction. A statement inside it ends, and its
# ExecutorEnd flushes every pending write -- the OUTER statement's buffered rows and
# delete marks too. If what that flush writes belongs to the inner subtransaction,
# the block's rollback takes the outer rows with it. Each arm is run on heap too, and
# the columnar answer must be heap's.
psql_run "CREATE TABLE sx_side (id int) USING pgcolumnar;"
psql_run "CREATE FUNCTION sx_f(i int) RETURNS int LANGUAGE plpgsql AS \$\$
BEGIN
	BEGIN
		INSERT INTO sx_side VALUES (i);
		PERFORM 1 / 0;
	EXCEPTION WHEN division_by_zero THEN NULL;
	END;
	RETURN i;
END \$\$;"
for am in pgcolumnar heap; do
	psql_run "CREATE TABLE sx_$am (id int) USING $am;"
	psql_run "INSERT INTO sx_$am SELECT sx_f(g) FROM generate_series(1, 5000) g;"
done
check "an outer insert survives an aborted inner block, as on heap" \
	"$(q "SELECT count(*) || '|' || sum(id) FROM sx_pgcolumnar")" "$(q "SELECT count(*) || '|' || sum(id) FROM sx_heap")"
check "and the inner block's own rows are gone" "$(q "SELECT count(*) FROM sx_side")" "0"
for am in pgcolumnar heap; do
	psql_run "DELETE FROM sx_$am WHERE id % 2 = 0 AND sx_f(id) IS NOT NULL;"
done
check "an outer delete survives an aborted inner block, as on heap" \
	"$(q "SELECT count(*) || '|' || sum(id) FROM sx_pgcolumnar")" "$(q "SELECT count(*) || '|' || sum(id) FROM sx_heap")"

# ---- a read inside an aborted inner block keeps the outer statement's rows (#1360) -
#
# A read of the table inside the block must see the rows the outer INSERT has
# buffered so far, as heap's does, so it flushes them. That flush's catalog rows
# belonged to the block's subtransaction, and its rollback took the outer rows with
# them: 1 row of 3000 survived. The read's answer is pinned too, so a fix cannot
# trade the lost rows for a read that misses them. Every arm runs on heap as well,
# and the columnar answer must be heap's. HOW: abort (the block raises after the
# read), nested (the read is in a block that commits into one that raises), commit
# (nothing raises: the control).
psql_run "CREATE FUNCTION sx_rd(i int, t text, how text) RETURNS int LANGUAGE plpgsql AS \$\$
DECLARE n int; c text; sq text;
BEGIN
	IF how = 'once' AND i <> 2 THEN RETURN NULL; END IF;
	BEGIN
		IF how = 'delself' THEN
			c := current_setting('pgcolumnar.enable_custom_scan'); sq := current_setting('enable_seqscan');
			PERFORM set_config('pgcolumnar.enable_custom_scan', 'off', true), set_config('enable_seqscan', 'off', true);
			EXECUTE format('DELETE FROM %s WHERE id = %s', t, i + 1);
			PERFORM set_config('pgcolumnar.enable_custom_scan', c, true), set_config('enable_seqscan', sq, true);
		END IF;
		IF how = 'nested' THEN
			BEGIN
				EXECUTE format('SELECT count(*) FROM %s', t) INTO n;
			EXCEPTION WHEN no_data_found THEN NULL;
			END;
		ELSE
			EXECUTE format('SELECT count(*) FROM %s', t) INTO n;
		END IF;
		IF how <> 'commit' THEN PERFORM 1 / 0; END IF;
	EXCEPTION WHEN division_by_zero THEN NULL;
	END;
	RETURN n;
END \$\$;"
for how in abort nested commit; do
	for am in pgcolumnar heap; do
		psql_run "CREATE TABLE rd_${how}_$am (id int PRIMARY KEY, seen int) USING $am;"
		psql_run "INSERT INTO rd_${how}_$am SELECT g, sx_rd(g, 'rd_${how}_$am', '$how') FROM generate_series(1, 1000) g;"
	done
done
rd_shape() { q "SELECT count(*) || '|' || sum(id) FROM rd_$1"; }
rd_seen() { q "SELECT sum(seen) FROM rd_$1"; }
check "an outer insert survives a read of its own table inside an aborted inner block, as on heap" \
	"$(rd_shape abort_pgcolumnar)" "$(rd_shape abort_heap)"
check "and that read saw the rows the outer statement had inserted so far, as on heap" \
	"$(rd_seen abort_pgcolumnar)" "$(rd_seen abort_heap)"
rd_off="SET enable_seqscan = off; SET enable_bitmapscan = off;"
rd_all="SELECT count(*) FROM rd_abort_pgcolumnar WHERE id BETWEEN 1 AND 1000"
check "and the primary key finds every outer row by index" \
	"$(case "$(q "$rd_off EXPLAIN (COSTS OFF) $rd_all")" in *Index*) echo index ;; *) echo other ;; esac)|$(q "$rd_off $rd_all" | tail -1)" \
	"index|1000"
check "an outer insert survives a read in a nested block that commits into an aborted one, as on heap" \
	"$(rd_shape nested_pgcolumnar)|$(rd_seen nested_pgcolumnar)" "$(rd_shape nested_heap)|$(rd_seen nested_heap)"
check "a read inside an inner block that commits keeps every outer row once, as on heap" \
	"$(rd_shape commit_pgcolumnar)|$(rd_seen commit_pgcolumnar)" "$(rd_shape commit_heap)|$(rd_seen commit_heap)"
# The same for the delete marks an outer DELETE has buffered: the read flushes them,
# and the block's rollback took them away, so the deleted rows came back.
for am in pgcolumnar heap; do
	psql_run "CREATE TABLE rd_del_$am (id int, seen int) USING $am;"
	psql_run "INSERT INTO rd_del_$am SELECT g, 0 FROM generate_series(1, 1000) g;"
	psql_run "DELETE FROM rd_del_$am WHERE id % 2 = 0 AND sx_rd(id, 'rd_del_$am', 'abort') IS NOT NULL;"
done
check "an outer delete survives a read of its own table inside an aborted inner block, as on heap" \
	"$(rd_shape del_pgcolumnar)" "$(rd_shape del_heap)"
# The block deletes the next row by index before its read. That path writes no
# enclosing marks first, so the block's own mark is on disk when the read writes the
# outer marks; it must not be carried into them and written again after the abort.
for am in pgcolumnar heap; do
	psql_run "CREATE TABLE rd_ds_$am (id int PRIMARY KEY, seen int) USING $am;"
	psql_run "INSERT INTO rd_ds_$am SELECT g, 0 FROM generate_series(1, 1000) g;"
	psql_run "DELETE FROM rd_ds_$am WHERE id % 2 = 0 AND sx_rd(id, 'rd_ds_$am', 'delself') IS NOT NULL;"
done
check "and a delete the aborted block made itself stays undone, as on heap" \
	"$(rd_shape ds_pgcolumnar)" "$(rd_shape ds_heap)"
# THE COST IS LINEAR IN THE ROWS (#1403 review). Each abort undoes the catalog rows the
# block's read wrote, and the next read must write the enclosing rows out again. Kept
# as one growing stripe, that rewrote every earlier row each time: at twice the rows,
# about four times the file. A relation's size at 2n must stay under 2.5 times its size
# at n; the rows are incompressible, so the size measures what was written.
for sz in 400 800; do
	psql_run "CREATE TABLE rd_sz_$sz (id int, seen int, pad text) USING pgcolumnar;
	          INSERT INTO rd_sz_$sz SELECT g, sx_rd(g, 'rd_sz_$sz', 'abort'),
	                 md5(g::text) || md5((g + 1)::text) || md5((g + 2)::text) FROM generate_series(1, $sz) g;"
done
check "a read in an aborted block at twice the rows writes under 2.5 times the file, with every row kept" \
	"$(q "SELECT (SELECT count(*) FROM rd_sz_400) || '|' || (SELECT count(*) FROM rd_sz_800) || '|' ||
	             CASE WHEN pg_relation_size('rd_sz_800') < 2.5 * pg_relation_size('rd_sz_400') THEN 'linear'
	                  ELSE 'x' || round(pg_relation_size('rd_sz_800')::numeric / pg_relation_size('rd_sz_400'), 1) END")" \
	"400|800|linear"
# AND ONE READ COSTS ONE WRITE, NOT THE REST OF THE STATEMENT (#1403 review). Eager
# until the statement ended, a single read early in a bulk INSERT made every later
# row its own row group: 20,000 rows, 19,999 groups, 164 MB. Eager for one write, it
# writes the kept rows once more with the next row, then buffers as before.
psql_run "CREATE TABLE rd_once (id int, seen int, pad text) USING pgcolumnar;
          INSERT INTO rd_once SELECT g, sx_rd(g, 'rd_once', 'once'), md5(g::text) FROM generate_series(1, 5000) g;"
check "a single read early in a 5000-row insert keeps every row in at most 3 row groups" \
	"$(q "SELECT (SELECT count(*) FROM rd_once) || '|' ||
	             CASE WHEN (SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = pgcolumnar.get_storage_id('rd_once')) <= 3
	                  THEN 'few' ELSE (SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = pgcolumnar.get_storage_id('rd_once'))::text || ' groups' END")" \
	"5000|few"

# ---- an insert racing an uncommitted delete of its key (#1341) ----------------------
#
# Session 1 deletes key k and has not committed; session 2 inserts k. On heap session
# 2 waits for session 1: a rollback leaves k taken (23505), a commit frees it. One row
# with k either way -- never two.
psql_run "CREATE TABLE ud (k int UNIQUE, v text) USING pgcolumnar;"
psql_run "INSERT INTO ud VALUES (5, 'a'), (6, 'b'), (7, 'c');"
# race HOLD_SQL INSERT_SQL -> the inserter's state, after the holder has ended
race() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq \
		-c "BEGIN; $1; SELECT pg_sleep(3); $2;" >/dev/null 2>&1 &
	local holder=$!
	sleep 1
	local got; got="$(state "$3")"
	wait "$holder"
	echo "$got"
}
check "an insert of a key whose delete is uncommitted waits, and fails when the delete rolls back" \
	"$(race "DELETE FROM ud WHERE k = 5" "ROLLBACK" "INSERT INTO ud VALUES (5, 'racer');")|$(q "SELECT count(*) || '|' || min(v) FROM ud WHERE k = 5")" \
	"23505|1|a"
check "and succeeds when the delete commits, leaving one row with the key" \
	"$(race "DELETE FROM ud WHERE k = 6" "COMMIT" "INSERT INTO ud VALUES (6, 'racer');")|$(q "SELECT count(*) || '|' || min(v) FROM ud WHERE k = 6")" \
	"ok|1|racer"
check "an insert of a key an uncommitted UPDATE moved away fails when the update rolls back" \
	"$(race "UPDATE ud SET k = 70 WHERE k = 7" "ROLLBACK" "INSERT INTO ud VALUES (7, 'racer');")|$(q "SELECT count(*) || '|' || min(v) FROM ud WHERE k = 7")" \
	"23505|1|c"

# ---- an index built after a delete serves an older snapshot that sees the row (#1341) -
#
# Session 1 holds a REPEATABLE READ snapshot from before a committed DELETE of row 4242;
# then an index is built. Session 1 still sees row 4242, so an index scan must find it,
# as on heap, which indexes rows not yet dead to every snapshot.
psql_run "CREATE TABLE ib (id int, v int) USING pgcolumnar;"
psql_run "INSERT INTO ib SELECT g, g FROM generate_series(1, 10000) g;"
# ORDERED BY EVENTS, NOT SLEEPS. Session 1 used to sleep four seconds and assume the
# DELETE and the index build fitted inside them; on a loaded box they did not, the
# plan was taken before the index existed, and the premise read 0 (PG16, 2026-10-15).
# Now a gate session holds an advisory lock. Session 1 takes its snapshot, names
# itself, and queues for that lock; the script waits for the name, deletes, builds,
# and only then ends the gate, so session 1 plans after the index exists.
_ib_out="$PGC_WORKDIR/ib_rr.out"
ib_wait_for() {	# ib_wait_for APPLICATION_NAME -> its pid, once it has named itself
	local i pid=""
	for i in $(seq 1 300); do
		pid="$(q "SELECT pid FROM pg_stat_activity WHERE application_name = '$1'")"
		[ -n "$pid" ] && break
		sleep 0.1
	done
	echo "$pid"
}
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq \
	-c "SELECT pg_advisory_lock(4242); SET application_name = 'ib_gate'; SELECT pg_sleep(600);" >/dev/null 2>&1 &
_ib_gate=$!
_ib_gate_pid="$(ib_wait_for ib_gate)"
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq -v ON_ERROR_STOP=1 \
	-c "BEGIN ISOLATION LEVEL REPEATABLE READ; SELECT count(*) FROM ib; SET application_name = 'ib_snap';
	    SELECT pg_advisory_lock_shared(4242);
	    SET LOCAL enable_seqscan = off; SET LOCAL enable_bitmapscan = off;
	    EXPLAIN (COSTS OFF) SELECT count(*) FROM ib WHERE id = 4242;
	    SELECT 'rows:' || count(*) FROM ib WHERE id = 4242; COMMIT;" > "$_ib_out" 2>&1 &
_ib_s1=$!
_ib_snap_pid="$(ib_wait_for ib_snap)"
psql_run "DELETE FROM ib WHERE id = 4242;"
psql_run "CREATE INDEX ib_id ON ib (id);"
q "SELECT pg_terminate_backend(${_ib_gate_pid:-0})" >/dev/null
wait "$_ib_gate" 2>/dev/null
wait "$_ib_s1"
check "premise: the older snapshot was taken before the delete" \
	"$([ -n "$_ib_gate_pid" ] && [ -n "$_ib_snap_pid" ] && head -1 "$_ib_out" || echo "no (gate ${_ib_gate_pid:-none}, snapshot ${_ib_snap_pid:-none})")" "10000"
check "premise: the index exists and is valid" \
	"$(q "SELECT indisvalid FROM pg_index WHERE indexrelid = 'ib_id'::regclass")" "t"
check "premise: the older snapshot's lookup is an index scan" \
	"$(grep -cE 'Index (Only )?Scan using ib_id' "$_ib_out")" "1"
check "an index built after a delete finds the row for an older snapshot that sees it" \
	"$(sed -n 's/^rows://p' "$_ib_out")" "1"
check "and a new snapshot does not see the deleted row by that index" \
	"$(q "SET enable_seqscan = off; SET enable_bitmapscan = off; SELECT count(*) FROM ib WHERE id = 4242;" | tail -1)" "0"

# The cost of indexing deleted rows, pinned: they go in as not alive, so a UNIQUE
# index still builds over a key whose only other holder is a deleted row.
psql_run "CREATE TABLE ibu (k int) USING pgcolumnar;"
psql_run "INSERT INTO ibu VALUES (5), (6);"
psql_run "DELETE FROM ibu WHERE k = 5;"
psql_run "INSERT INTO ibu VALUES (5);"
check "a unique index builds over a key whose other holder is a deleted row" \
	"$(state "CREATE UNIQUE INDEX ibu_k ON ibu (k);")|$(q "SET enable_seqscan = off; SELECT count(*) FROM ibu WHERE k = 5;" | tail -1)" "ok|1"

# But only rows some snapshot can still see (#1407). Every deleted row went in, so
# REINDEX, documented as what reclaims stale entries, reclaimed none: 9 rows in 10
# deleted, and the rebuilt index kept its size where heap's shrank to a tenth.
for am in pgcolumnar heap; do
	psql_run "CREATE TABLE ix_$am (a int) USING $am; INSERT INTO ix_$am SELECT g FROM generate_series(1, 100000) g;
	          CREATE INDEX ix_${am}_a ON ix_$am (a); DELETE FROM ix_$am WHERE a % 10 <> 0;"
done
ix_reclaim() {	# ix_reclaim AM -> reclaimed, when REINDEX takes the index under half its size
	local before after
	before="$(q "SELECT pg_relation_size('ix_${1}_a')")"
	psql_run "REINDEX INDEX ix_${1}_a;"
	after="$(q "SELECT pg_relation_size('ix_${1}_a')")"
	[ -n "$before" ] && [ -n "$after" ] && [ "$after" -lt $((before / 2)) ] && echo reclaimed || echo "kept:$before->$after"
}
check "REINDEX after a committed delete of most rows reclaims their entries, as heap's does" \
	"$(ix_reclaim pgcolumnar)|$(ix_reclaim heap)|$(q "SET enable_seqscan = off; SELECT count(*) FROM ix_pgcolumnar WHERE a > 0;" | tail -1)" \
	"reclaimed|reclaimed|10000"

# ---- documented refusals (#1341 items 44, 51, 77) ----------------------------------
#
# Each is refused where it is chosen, as limitations.md says, and refused cleanly:
# nothing is left changed by the attempt.
rf_count() { q "SELECT count(*) || '|' || sum(a) FROM rf"; }
psql_run "CREATE TABLE rf (a int) USING pgcolumnar; INSERT INTO rf SELECT g FROM generate_series(1, 5000) g;"
check "CREATE UNLOGGED TABLE ... USING pgcolumnar is refused (0A000) and leaves no table" \
	"$(state "CREATE UNLOGGED TABLE rf_u (a int) USING pgcolumnar")|$(q "SELECT count(*) FROM pg_class WHERE relname = 'rf_u'")" "0A000|0"
check "ALTER TABLE ... SET UNLOGGED is refused (0A000), and the table stays logged with its rows" \
	"$(state "ALTER TABLE rf SET UNLOGGED")|$(q "SELECT relpersistence FROM pg_class WHERE relname = 'rf'")|$(rf_count)" "0A000|p|5000|12502500"
pgc_pg "mkdir -p -m 700 '$PGC_WORKDIR/rf_ts'"
psql_run "CREATE TABLESPACE rf_ts LOCATION '$PGC_WORKDIR/rf_ts';"
check "ALTER TABLE ... SET TABLESPACE is refused (0A000), and the table stays where it was with its rows" \
	"$(state "ALTER TABLE rf SET TABLESPACE rf_ts")|$(q "SELECT reltablespace FROM pg_class WHERE relname = 'rf'")|$(rf_count)" "0A000|0|5000|12502500"
# A failed concurrent build leaves its index behind, marked invalid, as core does for
# any CREATE INDEX CONCURRENTLY that fails after its first phase.
check "CREATE INDEX CONCURRENTLY is refused (0A000), and the index it leaves is marked invalid" \
	"$(state "CREATE INDEX CONCURRENTLY rf_cic ON rf (a)")|$(q "SELECT indisvalid FROM pg_index WHERE indexrelid = 'rf_cic'::regclass")" "0A000|f"

pgc_summary
