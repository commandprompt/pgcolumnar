#!/usr/bin/env bash
#
# pgColumnar online-rewrite concurrency stress (Phase F3b). While several backends
# run INSERT/DELETE/UPDATE over disjoint id ranges, a compactor loop concurrently
# runs pgcolumnar.compact_rewrite (online rewrite of partially-deleted groups) and
# pgcolumnar.compact (retire fully-dead groups). A DELETE/UPDATE that races a
# rewrite of its group is aborted with a serialization failure by the conflict
# protocol; the worker retries. Because the id ranges are disjoint the final
# logical state is deterministic, so t_col must end equal to the heap oracle --
# proving the online rewrite never loses or resurrects a row under concurrent
# writes. This is the decisive correctness test for F3b's ShareUpdateExclusiveLock
# path.
#
# Usage:  test/native_rewrite_conc.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

make_pair "id int, cat int, v bigint, txt text"
q "SELECT pgcolumnar.set_options('t_col', chunk_group_row_limit => 500, stripe_row_limit => 1000);" >/dev/null

WORKERS=6
PER=3000

# Run one autocommit statement, retrying on the serialization failures the rewrite
# conflict protocol raises (each failed attempt rolls back fully, so a retry
# reapplies exactly once). Returns non-zero only if it never succeeds.
runsql() {
	local sql="$1" errf="$2" t=0
	until env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
			-d "$PGC_DB" -v ON_ERROR_STOP=1 -q -c "$sql" >/dev/null 2>>"$errf"; do
		t=$((t + 1))
		[ "$t" -ge 200 ] && return 1
	done
	return 0
}

# Compactor loop: hammer the online rewrite + fully-dead retirement concurrently.
STOP="$PGC_WORKDIR/stop"
rm -f "$STOP" "$PGC_WORKDIR/compactor.out"
(
	while [ ! -f "$STOP" ]; do
		env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
			-d "$PGC_DB" -qAt -c "SELECT pgcolumnar.compact_rewrite('t_col', 0.0);" \
			>>"$PGC_WORKDIR/compactor.out" 2>>"$PGC_WORKDIR/compactor.err" || true
		env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
			-d "$PGC_DB" -q -c "SELECT pgcolumnar.compact('t_col');" \
			>/dev/null 2>>"$PGC_WORKDIR/compactor.err" || true
	done
) &
comp_pid=$!

echo "-- launching $WORKERS workers against a concurrent compactor"
pids=()
for w in $(seq 0 $((WORKERS - 1))); do
	lo=$((w * PER + 1))
	hi=$(((w + 1) * PER))
	errf="$PGC_WORKDIR/w.$w.err"
	(
		# inserts never conflict with the rewrite (only delete/update do)
		runsql "INSERT INTO t_heap SELECT g, g%5, g*3, md5(g::text) FROM generate_series($lo,$hi) g;" "$errf" || exit 1
		runsql "INSERT INTO t_col  SELECT g, g%5, g*3, md5(g::text) FROM generate_series($lo,$hi) g;" "$errf" || exit 1
		runsql "DELETE FROM t_heap WHERE id BETWEEN $lo AND $hi AND id % 7 = 0;" "$errf" || exit 1
		runsql "DELETE FROM t_col  WHERE id BETWEEN $lo AND $hi AND id % 7 = 0;" "$errf" || exit 1
		runsql "UPDATE t_heap SET v = v + 1 WHERE id BETWEEN $lo AND $hi AND id % 5 = 0;" "$errf" || exit 1
		runsql "UPDATE t_col  SET v = v + 1 WHERE id BETWEEN $lo AND $hi AND id % 5 = 0;" "$errf" || exit 1
	) &
	pids+=($!)
done

fail=0
for pid in "${pids[@]}"; do
	wait "$pid" || fail=1
done
touch "$STOP"
wait "$comp_pid" 2>/dev/null || true
check "all workers succeeded (retried past serialization failures)" "$fail" "0"

# THE RACE MUST HAVE HAPPENED, or the oracle comparison below proves nothing about
# it. While the workers ran, the compactor must have moved rows (compact_rewrite
# returns the live rows it moved) -- a rewrite that never ran alongside the writers
# leaves the conflict protocol untested.
moved="$(awk '/^[0-9]+$/ {s += $1} END {print s + 0}' "$PGC_WORKDIR/compactor.out" 2>/dev/null)"
echo "-- compactor moved $moved rows during the workers' window; serialization retries: $(cat "$PGC_WORKDIR"/w.*.err 2>/dev/null | grep -c 'row group was compacted concurrently')"
check "the compactor rewrote groups while the workers were writing" \
	"$([ -n "$moved" ] && [ "$moved" -gt 0 ] 2>/dev/null && echo yes || echo "no (moved [$moved])")" "yes"

# A final settling pass, then the columnar table must equal the heap oracle.
q "SELECT pgcolumnar.compact_rewrite('t_col', 0.0);" >/dev/null
q "SELECT pgcolumnar.compact('t_col');" >/dev/null

diff_query "conc-rewrite whole-row" "SELECT * FROM %T"
diff_query "conc-rewrite count/sum" "SELECT count(*), sum(v), count(distinct cat) FROM %T"
diff_query "conc-rewrite range"     "SELECT count(*) FROM %T WHERE id BETWEEN 4000 AND 12000"
check "row count sane" "$([ "$(q 'SELECT count(*) FROM t_col;')" -gt 0 ] && echo ok)" "ok"

# ---- the online rewrite does not block readers or writers --------------------
#
# The point of F3b is ShareUpdateExclusiveLock: a rewrite runs alongside the
# workload. Nothing above could see a rewrite that took a stronger lock -- the
# workers would simply wait for it and still finish, and the oracle would still
# match. So a compact_rewrite is held mid-verb with its lock taken
# (pgcolumnar.maintenance_hold_ms), and a reader and a writer, each with a short
# lock_timeout, must finish while it still holds it.
BEFORE="$(q 'SELECT count(*) FROM t_col;')"
(
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-qAt -v ON_ERROR_STOP=1 -c "SET pgcolumnar.maintenance_hold_ms = 12000;" \
		-c "SELECT pgcolumnar.compact_rewrite('t_col', 0.0);" >"$PGC_WORKDIR/holder.out" 2>&1
	echo "rc=$?" >>"$PGC_WORKDIR/holder.out"
) &
holder_pid=$!
# the holder's lock on t_col, by any backend running compact_rewrite: wait (bounded) until it is taken
held() {
	q "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a USING (pid)
	   WHERE l.relation = 't_col'::regclass AND l.granted AND a.query LIKE '%compact_rewrite%'
	     AND a.pid <> pg_backend_pid();"
}
for _ in $(seq 1 50); do [ "$(held)" = "1" ] && break; sleep 0.2; done
check "a compact_rewrite holds its lock on t_col mid-verb" "$(held)" "1"
check_num "a reader with a 3 s lock_timeout finishes while the rewrite holds its lock" \
	"$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-qAt -v ON_ERROR_STOP=1 -c "BEGIN; SET LOCAL lock_timeout = '3s'; SELECT count(*) FROM t_col; COMMIT;" 2>/dev/null | head -1)" \
	"$BEFORE"
check "a writer with a 3 s lock_timeout inserts while the rewrite holds its lock" \
	"$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-qAt -v ON_ERROR_STOP=1 -c "BEGIN; SET LOCAL lock_timeout = '3s'; INSERT INTO t_col VALUES (-1, 0, 0, 'probe'); COMMIT;" \
		>/dev/null 2>&1 && echo inserted || echo refused)" "inserted"
check "and the rewrite still held its lock when both had finished" "$(held)" "1"
wait "$holder_pid"
check "the held rewrite itself completed" "$(tail -1 "$PGC_WORKDIR/holder.out")" "rc=0"

# ---- an online rewrite and an open writer cannot deadlock ----------------------
#
# Issue #2074. Two advisory locks meet here. The chunk-group lock (class 101)
# guards a group's delete vector. The storage-row lock (class 102) is taken by
# every row-group flush and held to commit, so it serializes writers to a table.
# A writer can take the two in either order, and holds both to commit:
#
#   - update shape: an UPDATE flushes its new row (102), then its delete
#     vector (101).
#   - delete shape: a DELETE whose marks a later scan flushes (101), then an
#     INSERT flushed at COMMIT (102).
#
# So a rewrite that waits for one class while it holds the other closes a cycle
# with one of the shapes. On main the rewrites took 101 and then waited on 102,
# which deadlocked with the update shape. Taking 102 first and then waiting on
# 101 deadlocked with the delete shape instead. The rewrite must never wait while
# it holds either lock.
#
# To place each cycle deterministically, an open writer first takes its first
# lock and nothing else. The rewrite, released from its 3 s hold, runs until
# pg_locks shows it waiting on that lock. Only then does the writer take its
# second lock on a group the rewrite covers, and commit. Both must commit.
#
# dl_race VERB-SQL SHAPE -> "waited=<t|f> writer=<ok|error> rewrite=<ok|error>"
dl_race() {
	local verb="$1" shape="$2" first second cls
	case "$shape" in
	update) first="INSERT INTO dl VALUES (0, 0);" cls=102
		second="UPDATE dl SET bal = bal + 1 WHERE id = 5;" ;;
	delete) first="DELETE FROM dl WHERE id = 5;" cls=101
		second="INSERT INTO dl VALUES (0, 0);" ;;
	esac
	q "DROP TABLE IF EXISTS dl;
	   CREATE TABLE dl (id int, bal int) USING pgcolumnar;
	   SELECT pgcolumnar.set_options('dl', stripe_row_limit => 1000, chunk_group_row_limit => 100);
	   INSERT INTO dl SELECT g, 1000 FROM generate_series(1, 3000) g;
	   DELETE FROM dl WHERE id <= 1000 AND id % 3 = 0;" >/dev/null
	rm -f "$PGC_WORKDIR/dl.rw" "$PGC_WORKDIR/dl.up"
	(
		env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
			-qAt -v ON_ERROR_STOP=1 -c "SET pgcolumnar.maintenance_hold_ms = 3000;" \
			-c "SELECT $verb;" >"$PGC_WORKDIR/dl.rw" 2>&1
		echo "rc=$?" >>"$PGC_WORKDIR/dl.rw"
	) &
	local rw_pid=$!
	# wait (bounded) until the rewrite holds its table lock, so it is inside its hold
	for _ in $(seq 1 50); do
		[ "$(q "SELECT count(*) FROM pg_locks WHERE relation = 'dl'::regclass AND granted
		        AND mode = 'ShareUpdateExclusiveLock' AND pid <> pg_backend_pid();")" = "1" ] && break
		sleep 0.1
	done
	local waiting="EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory'
	               AND objsubid = $cls AND NOT granted AND pid <> pg_backend_pid())"
	# The scan after the first statement flushes it, so the writer holds its first lock.
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-qAt -v ON_ERROR_STOP=1 >"$PGC_WORKDIR/dl.up" 2>&1 <<SQL
BEGIN;
$first
SELECT count(*) > 0 FROM dl;
DO \$\$
BEGIN
	FOR i IN 1..300 LOOP
		EXIT WHEN $waiting;
		PERFORM pg_sleep(0.1);
	END LOOP;
END \$\$;
SELECT 'waited=' || $waiting;
$second
COMMIT;
SQL
	local up_rc=$?
	wait "$rw_pid"
	echo "$(grep -o 'waited=[tf]' "$PGC_WORKDIR/dl.up" || echo waited=none)" \
		"writer=$([ "$up_rc" = 0 ] && echo ok || echo error)" \
		"rewrite=$([ "$(tail -1 "$PGC_WORKDIR/dl.rw")" = "rc=0" ] && echo ok || echo error)"
	grep -h 'deadlock detected' "$PGC_WORKDIR/dl.up" "$PGC_WORKDIR/dl.rw" >&2
}
for verb in "pgcolumnar.recluster('dl', 'id')" "pgcolumnar.compact_rewrite('dl', 0.0)"; do
	name="${verb%%(*}"; name="${name#pgcolumnar.}"
	got="$(dl_race "$verb" update)"
	check "a $name waited on the storage-row lock of an open writer" \
		"$(grep -o 'waited=[a-z]*' <<<"$got")" "waited=t"
	check "a single-row UPDATE from that writer and the $name both commit, with no deadlock" \
		"$(sed 's/^waited=[a-z]* //; s/writer=/update=/' <<<"$got")" "update=ok rewrite=ok"
	check "and the UPDATE beside the $name took effect" \
		"$(q "SELECT bal FROM dl WHERE id = 5;")" "1001"
	got="$(dl_race "$verb" delete)"
	check "a $name waited on the chunk-group lock of an open writer that deleted and scanned" \
		"$(grep -o 'waited=[a-z]*' <<<"$got")" "waited=t"
	check "a DELETE, a scan and an INSERT from that writer and the $name both commit, with no deadlock" \
		"$(sed 's/^waited=[a-z]* //' <<<"$got")" "writer=ok rewrite=ok"
	check "and the DELETE and INSERT beside the $name took effect" \
		"$(q "SELECT count(*) FILTER (WHERE id = 5) || ' ' || count(*) FILTER (WHERE id = 0) FROM dl;")" "0 1"
done

# ---- the rewrite gives its locks back, and its waiting has a bound ------------
#
# Review of #2139 at 630f63e3. lock_rewrite_groups waits for one lock at a time
# while it holds no other, and gives up after pgcolumnar.rewrite_lock_timeout.
# Each arm below reddens one way of getting that wrong:
#
#   - gives back: the rewrite must give back the group locks it took in a round
#     before it waits on a later busy group. The writer deletes in group 1, the
#     rewrite waits on it, then the writer updates group 0. A rewrite that kept
#     group 0 deadlocks with the writer.
#   - after the wait: the rewrite must give back the busy group's lock once its
#     wait ends. W1 holds group 0's lock and W2 the storage-row lock. When W1
#     commits, the rewrite waits on W2. Then W2 updates group 0. A rewrite that
#     kept group 0 deadlocks with W2.
#   - same transaction: a verb run after a DELETE in its own transaction already
#     holds that group's lock, and must count it as taken.
#   - bound: under steady deletes, and behind a writer that does not commit, the
#     verb stops with the documented lock error within the limit. The writer
#     that does not commit is a deleter (it holds a group lock, 101) and, from
#     the review at 2fe1d31f, also an inserter (it holds the storage-row lock,
#     102), so each of the two waits has its own deadline check.
#   - lock_timeout: each wait lowers the session's lock_timeout to the time
#     left (review at 2fe1d31f). After the verb, on success and after its 55P03,
#     the session's lock_timeout must be what the user set. A user lock_timeout
#     shorter than the time left must win, and end the verb with PostgreSQL's
#     own lock-timeout error, not the rewrite_lock_timeout one.

dlp() { env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" "$@"; }

# dl_make [extra]: the dl table; "extra" adds committed deletes in group 1 (ids 1001-2000)
dl_make() {
	q "DROP TABLE IF EXISTS dl;
	   CREATE TABLE dl (id int, bal int) USING pgcolumnar;
	   SELECT pgcolumnar.set_options('dl', stripe_row_limit => 1000, chunk_group_row_limit => 100);
	   INSERT INTO dl SELECT g, 1000 FROM generate_series(1, 3000) g;
	   DELETE FROM dl WHERE id <= 1000 AND id % 3 = 0;" >/dev/null
	[ "${1:-}" = extra ] && q "DELETE FROM dl WHERE id BETWEEN 1001 AND 2000 AND id % 3 = 0;" >/dev/null
	return 0
}

# dl_rewrite_bg VERB-SQL: run the verb with a 3 s hold in the background, output in dl.rw;
# returns once it holds its table lock, so it is inside its hold
dl_rewrite_bg() {
	rm -f "$PGC_WORKDIR/dl.rw"
	(
		dlp -qAt -v ON_ERROR_STOP=1 -c "SET pgcolumnar.maintenance_hold_ms = 3000;" \
			-c "SELECT $1;" >"$PGC_WORKDIR/dl.rw" 2>&1
		echo "rc=$?" >>"$PGC_WORKDIR/dl.rw"
	) &
	DL_RW_PID=$!
	for _ in $(seq 1 50); do
		[ "$(q "SELECT count(*) FROM pg_locks WHERE relation = 'dl'::regclass AND granted
		        AND mode = 'ShareUpdateExclusiveLock' AND pid <> pg_backend_pid();")" = "1" ] && break
		sleep 0.1
	done
}

# dl_wait_sql CONDITION: a DO block that waits (bounded, 30 s) until CONDITION holds
dl_wait_sql() {
	printf 'DO $$ BEGIN FOR i IN 1..300 LOOP EXIT WHEN %s; PERFORM pg_sleep(0.1); END LOOP; END $$;\n' "$1"
}
dl_waiting() {
	echo "EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND objsubid = $1 AND NOT granted AND pid <> pg_backend_pid())"
}
# dl_rw_wait: wait for the background verb (not from a subshell: it is this shell's
# child), then DL_RW is ok or error
dl_rw_wait() {
	wait "$DL_RW_PID"
	DL_RW=$([ "$(tail -1 "$PGC_WORKDIR/dl.rw")" = "rc=0" ] && echo ok || echo error)
}

# dl_giveback VERB-SQL -> "waited=<t|f> writer=<ok|error> rewrite=<ok|error>"
dl_giveback() {
	dl_make extra
	dl_rewrite_bg "$1"
	dlp -qAt -v ON_ERROR_STOP=1 >"$PGC_WORKDIR/dl.w" 2>&1 <<SQL
BEGIN;
DELETE FROM dl WHERE id = 1501;
SELECT count(*) > 0 FROM dl;
$(dl_wait_sql "$(dl_waiting 101)")
SELECT 'waited=' || $(dl_waiting 101);
UPDATE dl SET bal = bal + 1 WHERE id = 5;
SELECT count(*) > 0 FROM dl;
COMMIT;
SQL
	local w_rc=$?
	dl_rw_wait
	echo "$(grep -o 'waited=[tf]' "$PGC_WORKDIR/dl.w" || echo waited=none)" \
		"writer=$([ "$w_rc" = 0 ] && echo ok || echo error)" "rewrite=$DL_RW"
	grep -h 'deadlock detected' "$PGC_WORKDIR/dl.w" "$PGC_WORKDIR/dl.rw" >&2
}

# dl_afterwait VERB-SQL -> "waited102=<t|f> w1=<ok|error> w2=<ok|error> rewrite=<ok|error>"
dl_afterwait() {
	dl_make
	dl_rewrite_bg "$1"
	(
		dlp -qAt -v ON_ERROR_STOP=1 >"$PGC_WORKDIR/dl.w1" 2>&1 <<SQL
BEGIN;
DELETE FROM dl WHERE id = 5;
SELECT count(*) > 0 FROM dl;
$(dl_wait_sql "$(dl_waiting 101)")
$(dl_wait_sql "EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND objsubid = 102 AND granted)")
COMMIT;
SQL
		echo "rc=$?" >>"$PGC_WORKDIR/dl.w1"
	) &
	local w1_pid=$!
	dlp -qAt -v ON_ERROR_STOP=1 >"$PGC_WORKDIR/dl.w2" 2>&1 <<SQL
$(dl_wait_sql "$(dl_waiting 101)")
BEGIN;
INSERT INTO dl VALUES (0, 0);
SELECT count(*) > 0 FROM dl;
$(dl_wait_sql "$(dl_waiting 102)")
SELECT 'waited102=' || $(dl_waiting 102);
UPDATE dl SET bal = bal + 1 WHERE id = 7;
SELECT count(*) > 0 FROM dl;
COMMIT;
SQL
	local w2_rc=$?
	wait "$w1_pid"
	dl_rw_wait
	echo "$(grep -o 'waited102=[tf]' "$PGC_WORKDIR/dl.w2" || echo waited102=none)" \
		"w1=$([ "$(tail -1 "$PGC_WORKDIR/dl.w1")" = "rc=0" ] && echo ok || echo error)" \
		"w2=$([ "$w2_rc" = 0 ] && echo ok || echo error)" "rewrite=$DL_RW"
	grep -h 'deadlock detected' "$PGC_WORKDIR/dl.w1" "$PGC_WORKDIR/dl.w2" "$PGC_WORKDIR/dl.rw" >&2
}

# dl_sametxn VERB-SQL -> "rc=<n> verb=<value>" for the verb after a DELETE in its own transaction
dl_sametxn() {
	dl_make
	local out rc
	out="$(dlp -qAt -v ON_ERROR_STOP=1 2>&1 <<SQL
SET statement_timeout = '20s';
BEGIN;
DELETE FROM dl WHERE id = 7;
SELECT $1;
COMMIT;
SQL
)"
	rc=$?
	echo "rc=$rc verb=$(head -1 <<<"$out")"
	[ "$rc" = 0 ] || echo "$out" >&2
}

# dl_ms: milliseconds since the epoch
dl_ms() { echo $(( $(date +%s%N) / 1000000 )); }

# dl_starve VERB-SQL -> "<elapsed ms> <ok|SQLSTATE>" and dl.del holds the deleters' errors:
# eight deleters each delete one random row, flush it with a scan and hold the group lock
# for 200 ms, in a loop; the verb runs with a 2 s pgcolumnar.rewrite_lock_timeout
dl_starve() {
	q "DROP TABLE IF EXISTS dl;
	   CREATE TABLE dl (id int, bal int) USING pgcolumnar;
	   SELECT pgcolumnar.set_options('dl', stripe_row_limit => 1000, chunk_group_row_limit => 100);
	   INSERT INTO dl SELECT g, 1000 FROM generate_series(1, 20000) g;
	   DELETE FROM dl WHERE id % 3 = 0;" >/dev/null
	local stop="$PGC_WORKDIR/dl.stop" pids=() c
	rm -f "$stop" "$PGC_WORKDIR"/dl.del.*
	for c in $(seq 1 8); do
		(
			while [ ! -f "$stop" ]; do
				a=$(( (RANDOM * 32768 + RANDOM) % 20000 + 1 ))
				dlp -qAt -v ON_ERROR_STOP=1 -c "BEGIN; DELETE FROM dl WHERE id = $a;
					SELECT count(*) FROM dl WHERE id = $a; SELECT pg_sleep(0.2); COMMIT;" \
					>/dev/null 2>>"$PGC_WORKDIR/dl.del.$c"
			done
		) &
		pids+=($!)
	done
	sleep 2
	local t0 out rc
	t0=$(dl_ms)
	out="$(dlp -qAt -v ON_ERROR_STOP=1 -v VERBOSITY=verbose 2>&1 <<SQL
SET statement_timeout = '30s';
SET pgcolumnar.rewrite_lock_timeout = '2s';
SELECT $1;
SQL
)"
	rc=$?
	echo "$(( $(dl_ms) - t0 )) $([ "$rc" = 0 ] && echo ok || grep -o 'ERROR:  [0-9A-Z]\{5\}' <<<"$out" | head -1 | cut -c9-)"
	touch "$stop"
	wait "${pids[@]}"
	[ "$rc" = 0 ] || echo "$out" >&2
}

# dl_held VERB-SQL WRITER-SQL CLASS HOLD SETTINGS
#   -> "<elapsed ms> <ok|SQLSTATE> <names-limit|stock|other> lock_timeout=<value>":
# a writer runs WRITER-SQL, which takes the lock of CLASS (101 or 102), and keeps its
# transaction open for HOLD seconds. Once it holds that lock, one session runs SETTINGS,
# the verb, and then reads its own lock_timeout. "names-limit" means the error names
# pgcolumnar.rewrite_lock_timeout; "stock" means it is PostgreSQL's own lock-timeout error.
dl_held() {
	dl_make
	(
		dlp -qAt -v ON_ERROR_STOP=1 -c "BEGIN; $2; SELECT pg_sleep($4); COMMIT;" >/dev/null 2>&1
	) &
	local w_pid=$! t0 out ms
	dlp -qAt -c "$(dl_wait_sql "EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND objsubid = $3 AND granted)")" >/dev/null
	t0=$(dl_ms)
	out="$(dlp -qAt -v VERBOSITY=verbose 2>&1 <<SQL
SET statement_timeout = '30s';
$5
SELECT $1;
SELECT 'lock_timeout=' || current_setting('lock_timeout');
SQL
)"
	ms=$(( $(dl_ms) - t0 ))
	echo "$ms" \
		"$(grep -q '^ERROR:' <<<"$out" && grep -o 'ERROR:  [0-9A-Z]\{5\}' <<<"$out" | head -1 | cut -c9- || echo ok)" \
		"$(if grep -q 'pgcolumnar.rewrite_lock_timeout' <<<"$out"; then echo names-limit
		   elif grep -q 'canceling statement due to lock timeout' <<<"$out"; then echo stock
		   else echo other; fi)" \
		"$(grep -o '^lock_timeout=.*' <<<"$out" || echo lock_timeout=none)"
	wait "$w_pid"
	grep -q '^ERROR:' <<<"$out" && echo "$out" >&2
	return 0
}

# the writers dl_held runs: a deleter flushes its delete with a scan and so holds its group's
# lock (101); an inserter flushes its row with a scan and so holds the storage-row lock (102)
DL_DELETER="DELETE FROM dl WHERE id = 5; SELECT count(*) FROM dl"
DL_INSERTER="INSERT INTO dl VALUES (0, 0); SELECT count(*) FROM dl"

# dl_finished VERB-SQL -> "<ok|SQLSTATE> lock_timeout=<value>": with no writer, one session
# runs the verb with a 10 s rewrite_lock_timeout and then reads its own lock_timeout
dl_finished() {
	dl_make
	local out
	out="$(dlp -qAt -v VERBOSITY=verbose 2>&1 <<SQL
SET pgcolumnar.rewrite_lock_timeout = '10s';
SELECT $1;
SELECT 'lock_timeout=' || current_setting('lock_timeout');
SQL
)"
	echo "$(grep -q '^ERROR:' <<<"$out" && grep -o 'ERROR:  [0-9A-Z]\{5\}' <<<"$out" | head -1 | cut -c9- || echo ok)" \
		"$(grep -o '^lock_timeout=.*' <<<"$out" || echo lock_timeout=none)"
	grep -q '^ERROR:' <<<"$out" && echo "$out" >&2
	return 0
}

declare -A DL_SAMETXN_WANT=([recluster]=3 [compact_rewrite]=1)
for verb in "pgcolumnar.recluster('dl', 'id')" "pgcolumnar.compact_rewrite('dl', 0.0)"; do
	name="${verb%%(*}"; name="${name#pgcolumnar.}"
	got="$(dl_giveback "$verb")"
	check "a $name gave back its group locks before it waited on a later busy group" \
		"$(grep -o 'waited=[a-z]*' <<<"$got")" "waited=t"
	check "a DELETE in group 1 then an UPDATE in group 0 from one writer and the $name both commit" \
		"$(sed 's/^waited=[a-z]* //' <<<"$got")" "writer=ok rewrite=ok"
	check "and the DELETE in group 1 and the UPDATE in group 0 beside the $name took effect" \
		"$(q "SELECT count(*) FILTER (WHERE id = 1501) || ' ' || max(bal) FILTER (WHERE id = 5) FROM dl;")" "0 1001"
	got="$(dl_afterwait "$verb")"
	check "a $name waited on the storage-row lock after the group lock it waited for came free" \
		"$(grep -o 'waited102=[a-z]*' <<<"$got")" "waited102=t"
	check "two writers and the $name all commit when the second writer updates that group" \
		"$(sed 's/^waited102=[a-z]* //' <<<"$got")" "w1=ok w2=ok rewrite=ok"
	check "and both writers' changes beside the $name took effect" \
		"$(q "SELECT count(*) FILTER (WHERE id = 5) || ' ' || count(*) FILTER (WHERE id = 0) || ' ' || max(bal) FILTER (WHERE id = 7) FROM dl;")" "0 1 1001"
	check "a $name after a DELETE in its own transaction commits and returns its count" \
		"$(dl_sametxn "$verb")" "rc=0 verb=${DL_SAMETXN_WANT[$name]}"
	check "and the row deleted before the $name in its transaction stays deleted" \
		"$(q "SELECT count(*) FROM dl WHERE id = 7;")" "0"
	got="$(dl_starve "$verb")"
	check "a $name under steady deletes ends within its 2 s rewrite_lock_timeout plus 4 s" \
		"$(awk '{print ($1 < 6000) ? "yes" : "no (" $1 " ms)"}' <<<"$got")" "yes"
	check "and the $name either finished or stopped with the lock error 55P03" \
		"$(awk '{print ($2 == "ok" || $2 == "55P03") ? "ok-or-55P03" : $2}' <<<"$got")" "ok-or-55P03"
	check "and no deleter beside the $name hit a deadlock" \
		"$(cat "$PGC_WORKDIR"/dl.del.* 2>/dev/null | grep -c 'deadlock detected')" "0"
	got="$(dl_held "$verb" "$DL_DELETER" 101 8 "SET pgcolumnar.rewrite_lock_timeout = '1s';")"
	check "a $name behind a writer that stays open stops with 55P03 naming rewrite_lock_timeout" \
		"$(cut -d' ' -f2-3 <<<"$got")" "55P03 names-limit"
	check "and the $name stops within its 1 s rewrite_lock_timeout plus 3 s" \
		"$(awk '{print ($1 < 4000) ? "yes" : "no (" $1 " ms)"}' <<<"$got")" "yes"
	check "and the session's lock_timeout after the $name's 55P03 is back to 0" \
		"$(cut -d' ' -f4 <<<"$got")" "lock_timeout=0"
	got="$(dl_held "$verb" "$DL_INSERTER" 102 8 "SET pgcolumnar.rewrite_lock_timeout = '1s';")"
	check "a $name behind an inserter that stays open stops with 55P03 naming rewrite_lock_timeout" \
		"$(cut -d' ' -f2-3 <<<"$got")" "55P03 names-limit"
	check "and the $name behind that inserter stops within its 1 s rewrite_lock_timeout plus 3 s" \
		"$(awk '{print ($1 < 4000) ? "yes" : "no (" $1 " ms)"}' <<<"$got")" "yes"
	check "after a $name that finished, the session's lock_timeout is back to 0" \
		"$(dl_finished "$verb")" "ok lock_timeout=0"
	got="$(dl_held "$verb" "$DL_DELETER" 101 6 "SET lock_timeout = '500ms'; SET pgcolumnar.rewrite_lock_timeout = '5s';")"
	check "a $name under a shorter lock_timeout of 500 ms stops with the stock lock-timeout error" \
		"$(cut -d' ' -f2-3 <<<"$got")" "55P03 stock"
	check "and the $name stops within that 500 ms lock_timeout plus 1.5 s" \
		"$(awk '{print ($1 < 2000) ? "yes" : "no (" $1 " ms)"}' <<<"$got")" "yes"
done

pgc_summary
