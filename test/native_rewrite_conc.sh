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

# dl_make [extra|all]: the dl table; "extra" adds committed deletes in group 1 (ids 1001-2000),
# "all" in groups 1 and 2 (ids 1001-3000)
dl_make() {
	q "DROP TABLE IF EXISTS dl;
	   CREATE TABLE dl (id int, bal int) USING pgcolumnar;
	   SELECT pgcolumnar.set_options('dl', stripe_row_limit => 1000, chunk_group_row_limit => 100);
	   INSERT INTO dl SELECT g, 1000 FROM generate_series(1, 3000) g;
	   DELETE FROM dl WHERE id <= 1000 AND id % 3 = 0;" >/dev/null
	[ "${1:-}" = extra ] && q "DELETE FROM dl WHERE id BETWEEN 1001 AND 2000 AND id % 3 = 0;" >/dev/null
	[ "${1:-}" = all ] && q "DELETE FROM dl WHERE id BETWEEN 1001 AND 3000 AND id % 3 = 0;" >/dev/null
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

# dl_verb_held: SQL for how many group locks (101) the session that waits on a group lock
# holds, or 'none' when no session waits on one
dl_verb_held() {
	echo "CASE WHEN NOT $(dl_waiting 101) THEN 'none' ELSE (SELECT count(*) FROM pg_locks g
	   WHERE g.locktype = 'advisory' AND g.objsubid = 101 AND g.granted AND g.pid IN
	   (SELECT w.pid FROM pg_locks w WHERE w.locktype = 'advisory' AND w.objsubid = 101
	    AND NOT w.granted AND w.pid <> pg_backend_pid()))::text END"
}

# dl_giveback VERB-SQL [ID] [MAKE] -> "waited=<t|f> writer=<ok|error> rewrite=<ok|error>":
# one writer deletes ID (default 1501, group 1) and flushes it, waits until the verb waits
# on a group lock, then updates id 5 (group 0); MAKE is dl_make's argument (default extra).
# dl.w then holds "held=<n|none>": the group locks the verb held while it waited
dl_giveback() {
	dl_make "${3-extra}"
	dl_rewrite_bg "$1"
	dlp -qAt -v ON_ERROR_STOP=1 >"$PGC_WORKDIR/dl.w" 2>&1 <<SQL
BEGIN;
DELETE FROM dl WHERE id = ${2:-1501};
SELECT count(*) > 0 FROM dl;
$(dl_wait_sql "$(dl_waiting 101)")
SELECT 'waited=' || $(dl_waiting 101);
SELECT 'held=' || $(dl_verb_held);
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
# dl_late VERB-SQL stmt|xact -> "<ok|SQLSTATE>": with no writer and a 1 s limit, the
# session sleeps 1.5 s first, in the verb's own statement (stmt) or in an earlier statement
# of the verb's transaction (xact), then runs the verb
dl_late() {
	dl_make
	local out sql
	if [ "$2" = stmt ]; then
		sql="SET pgcolumnar.rewrite_lock_timeout = '1s';
SELECT pg_sleep(1.5) IS NULL, $1;"
	else
		sql="BEGIN;
SET pgcolumnar.rewrite_lock_timeout = '1s';
SELECT pg_sleep(1.5);
SELECT $1;
COMMIT;"
	fi
	out="$(dlp -qAt -v VERBOSITY=verbose -v ON_ERROR_STOP=1 2>&1 <<SQL
SET statement_timeout = '30s';
$sql
SQL
)"
	grep -q '^ERROR:' <<<"$out" && grep -o 'ERROR:  [0-9A-Z]\{5\}' <<<"$out" | head -1 | cut -c9- || echo ok
	grep -q '^ERROR:' <<<"$out" && echo "$out" >&2
	return 0
}

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
	check "and the $name under steady deletes finished, with no 55P03" \
		"$(awk '{print $2}' <<<"$got")" "ok"
	got="$(dl_held "$verb" "$DL_DELETER" 101 8 "SET pgcolumnar.rewrite_lock_timeout = '1s';")"
	check "a $name behind a writer that stays open stops with 55P03 naming rewrite_lock_timeout" \
		"$(cut -d' ' -f2-3 <<<"$got")" "55P03 names-limit"
	check "and the $name stops within its 1 s rewrite_lock_timeout plus 0.8 s" \
		"$(awk '{print ($1 < 1800) ? "yes" : "no (" $1 " ms)"}' <<<"$got")" "yes"
	check "and the session's lock_timeout after the $name's 55P03 is back to 0" \
		"$(cut -d' ' -f4 <<<"$got")" "lock_timeout=0"
	got="$(dl_held "$verb" "$DL_INSERTER" 102 8 "SET pgcolumnar.rewrite_lock_timeout = '1s';")"
	check "a $name behind an inserter that stays open stops with 55P03 naming rewrite_lock_timeout" \
		"$(cut -d' ' -f2-3 <<<"$got")" "55P03 names-limit"
	check "and the $name behind that inserter stops within its 1 s rewrite_lock_timeout plus 0.8 s" \
		"$(awk '{print ($1 < 1800) ? "yes" : "no (" $1 " ms)"}' <<<"$got")" "yes"
	check "after a $name that finished, the session's lock_timeout is back to 0" \
		"$(dl_finished "$verb")" "ok lock_timeout=0"
	got="$(dl_held "$verb" "$DL_DELETER" 101 6 "SET lock_timeout = '500ms'; SET pgcolumnar.rewrite_lock_timeout = '5s';")"
	check "a $name under a shorter lock_timeout of 500 ms stops with the stock lock-timeout error" \
		"$(cut -d' ' -f2-3 <<<"$got")" "55P03 stock"
	check "and the $name stops within that 500 ms lock_timeout plus 1.5 s" \
		"$(awk '{print ($1 < 2000) ? "yes" : "no (" $1 " ms)"}' <<<"$got")" "yes"
done


# ---- a longer user lock_timeout, the storage-row lock kept, a limit of 0, max_groups ----
#
# Review of #2139 at 686aed4e. Each arm reddens one way of getting that wrong:
#
#   - longer lock_timeout: a user lock_timeout longer than the time left must not
#     replace the limit. With lock_timeout = 20 s and a 1 s limit, behind a deleter
#     held for 8 s, the verb stops with 55P03 naming the setting within 1.8 s, and the
#     session gets its 20 s back.
#   - storage-row lock kept: the rewrite keeps the storage-row lock (102) from
#     lock_rewrite_groups to commit. A rewrite that gave it back would take it again,
#     waiting, to flush its new group while it holds its group locks. A writer that
#     inserts and then deletes in one of those groups closes the #2074 cycle. To place
#     the writer in that window, an expression index calls dl_pause(), which sleeps
#     8 s on the rewrite's first row when the session sets pgc.pause. At that point
#     the rewrite holds its group locks and has not flushed.
#   - limit 0: pgcolumnar.rewrite_lock_timeout = 0 means wait without a limit. With no
#     writer the verb finishes. Behind a deleter held for 3 s it waits and finishes.
#   - max_groups: compact_rewrite cuts its candidates to max_groups before it locks
#     them. With deletes in groups 0 and 1, compact_rewrite('dl', 0.0, 1) in an open
#     transaction holds one group lock, and a delete in group 1 commits within a 1 s
#     lock_timeout.

# dl_wait_activity_sql CONDITION: dl_wait_sql for a CONDITION on pg_stat_activity. A DO block
# is one transaction, and pg_stat_activity keeps the snapshot it took first for the whole
# transaction, so each try clears it
dl_wait_activity_sql() {
	printf 'DO $$ BEGIN FOR i IN 1..300 LOOP PERFORM pg_stat_clear_snapshot(); EXIT WHEN %s; PERFORM pg_sleep(0.1); END LOOP; END $$;\n' "$1"
}

# dl_pause_bg VERB-SQL: a fresh dl with an expression index on dl_pause(id), which sleeps
# 8 s on id 1 when the session sets pgc.pause; then the verb starts in the background with
# pgc.pause on and a 20 s rewrite_lock_timeout, output in dl.rw. While it sleeps, the verb
# has taken its group locks and has not flushed. DL_PAUSED matches its pg_stat_activity row
# (as a)
DL_PAUSED="a.wait_event = 'PgSleep' AND a.query LIKE 'SELECT pgcolumnar.%'"
dl_pause_bg() {
	dl_make
	q "CREATE OR REPLACE FUNCTION dl_pause(i int) RETURNS int LANGUAGE plpgsql IMMUTABLE AS \$\$
	   BEGIN
	       IF i = 1 AND current_setting('pgc.pause', true) = 'on' THEN PERFORM pg_sleep(8); END IF;
	       RETURN i;
	   END \$\$;
	   CREATE INDEX dl_pause_idx ON dl (dl_pause(id));" >/dev/null
	rm -f "$PGC_WORKDIR/dl.rw"
	(
		dlp -qAt -v ON_ERROR_STOP=1 -c "SET pgc.pause = 'on';" \
			-c "SET pgcolumnar.rewrite_lock_timeout = '20s';" \
			-c "SELECT $1;" >"$PGC_WORKDIR/dl.rw" 2>&1
		echo "rc=$?" >>"$PGC_WORKDIR/dl.rw"
	) &
	DL_RW_PID=$!
}

# dl_paused VERB-SQL -> "groups=<yes|no> storage=<n> writer=<ok|error> rewrite=<ok|error>":
# the verb pauses on its first row with its group locks taken; pg_locks shows whether it
# also holds the storage-row lock; then a writer inserts, scans, deletes in group 0, scans
# and commits
dl_paused() {
	dl_pause_bg "$1"
	local paused="$DL_PAUSED"
	# one session waits for the pause and reads pg_locks, so a slow connect cannot miss it
	local locks
	locks="$(dlp -qAt 2>&1 <<SQL
$(dl_wait_activity_sql "EXISTS (SELECT 1 FROM pg_stat_activity a WHERE $paused AND a.pid <> pg_backend_pid())")
SELECT 'groups=' || CASE WHEN count(*) FILTER (WHERE l.objsubid = 101) > 0 THEN 'yes' ELSE 'no' END
       || ' storage=' || count(*) FILTER (WHERE l.objsubid = 102)
FROM pg_locks l JOIN pg_stat_activity a USING (pid)
WHERE l.locktype = 'advisory' AND l.granted AND $paused;
SQL
)"
	dlp -qAt -v ON_ERROR_STOP=1 >"$PGC_WORKDIR/dl.w" 2>&1 <<SQL
BEGIN;
INSERT INTO dl VALUES (-1, 0);
SELECT count(*) FROM dl WHERE id = -1;
DELETE FROM dl WHERE id = 2;
SELECT count(*) FROM dl WHERE id = -1;
COMMIT;
SQL
	local w_rc=$?
	dl_rw_wait
	echo "$locks writer=$([ "$w_rc" = 0 ] && echo ok || echo error) rewrite=$DL_RW"
	grep -h 'ERROR' "$PGC_WORKDIR/dl.w" "$PGC_WORKDIR/dl.rw" >&2
	return 0
}

# dl_unlimited VERB-SQL -> "<ok|SQLSTATE>": with no writer, the verb with a limit of 0
dl_unlimited() {
	dl_make
	local out
	out="$(dlp -qAt -v VERBOSITY=verbose 2>&1 <<SQL
SET statement_timeout = '30s';
SET pgcolumnar.rewrite_lock_timeout = 0;
SELECT $1;
SQL
)"
	grep -q '^ERROR:' <<<"$out" && { grep -o 'ERROR:  [0-9A-Z]\{5\}' <<<"$out" | head -1 | cut -c9-; echo "$out" >&2; return 0; }
	echo ok
}

# dl_capped -> "verb=<value> locks=<n> delete=<ok|error>": compact_rewrite('dl', 0.0, 1) in
# an open transaction on a table with deletes in groups 0 and 1; the transaction counts its
# own group locks and stays open while a second session deletes a row of group 1, which the
# cut left out
dl_capped() {
	dl_make extra
	rm -f "$PGC_WORKDIR/dl.cap"
	(
		dlp -qAt -v ON_ERROR_STOP=1 >"$PGC_WORKDIR/dl.cap" 2>&1 <<SQL
BEGIN;
SELECT 'verb=' || pgcolumnar.compact_rewrite('dl', 0.0, 1);
SELECT 'locks=' || count(*) FROM pg_locks WHERE pid = pg_backend_pid() AND locktype = 'advisory' AND objsubid = 101 AND granted;
SELECT pg_sleep(10);
COMMIT;
SQL
	) &
	local cap_pid=$! d_rc
	dlp -qAt -c "$(dl_wait_activity_sql "EXISTS (SELECT 1 FROM pg_stat_activity WHERE wait_event = 'PgSleep' AND query LIKE 'SELECT pg_sleep(10)%' AND pid <> pg_backend_pid())")" >/dev/null
	dlp -qAt -v ON_ERROR_STOP=1 >"$PGC_WORKDIR/dl.del" 2>&1 <<SQL
SET lock_timeout = '1s';
BEGIN;
DELETE FROM dl WHERE id = 1502;
SELECT count(*) FROM dl;
COMMIT;
SQL
	d_rc=$?
	wait "$cap_pid"
	echo "$(grep -o '^verb=.*' "$PGC_WORKDIR/dl.cap" || echo verb=none)" \
		"$(grep -o '^locks=.*' "$PGC_WORKDIR/dl.cap" || echo locks=none)" \
		"delete=$([ "$d_rc" = 0 ] && echo ok || echo error)"
	grep -h 'ERROR' "$PGC_WORKDIR/dl.cap" "$PGC_WORKDIR/dl.del" >&2
	return 0
}

for verb in "pgcolumnar.recluster('dl', 'id')" "pgcolumnar.compact_rewrite('dl', 0.0)"; do
	name="${verb%%(*}"; name="${name#pgcolumnar.}"
	got="$(dl_held "$verb" "$DL_DELETER" 101 8 "SET lock_timeout = '20s'; SET pgcolumnar.rewrite_lock_timeout = '1s';")"
	check "a $name under a longer lock_timeout of 20 s stops with 55P03 naming rewrite_lock_timeout" \
		"$(cut -d' ' -f2-3 <<<"$got")" "55P03 names-limit"
	check "and the $name under that 20 s lock_timeout stops within its 1 s rewrite_lock_timeout plus 0.8 s" \
		"$(awk '{print ($1 < 1800) ? "yes" : "no (" $1 " ms)"}' <<<"$got")" "yes"
	check "and the session's lock_timeout after that $name's 55P03 is back to 20s" \
		"$(cut -d' ' -f4 <<<"$got")" "lock_timeout=20s"
	got="$(dl_paused "$verb")"
	check "a $name paused after it took its group locks also holds the storage-row lock" \
		"$(cut -d' ' -f1-2 <<<"$got")" "groups=yes storage=1"
	check "a writer that inserts and then deletes in a group the paused $name holds and the $name both commit" \
		"$(cut -d' ' -f3-4 <<<"$got")" "writer=ok rewrite=ok"
	check "and the insert and the delete beside the paused $name took effect" \
		"$(q "SELECT count(*) FILTER (WHERE id = 2) || ' ' || count(*) FILTER (WHERE id = -1) FROM dl;")" "0 1"
	check "a $name with rewrite_lock_timeout 0 and no writer finishes" \
		"$(dl_unlimited "$verb")" "ok"
	got="$(dl_held "$verb" "$DL_DELETER" 101 3 "SET pgcolumnar.rewrite_lock_timeout = 0;")"
	check "a $name with rewrite_lock_timeout 0 behind a deleter held for 3 s waits for it and finishes" \
		"$(awk '{print ($2 == "ok" && $1 >= 1500) ? "ok-after-wait" : $2 " " $3 " (" $1 " ms)"}' <<<"$got")" "ok-after-wait"
done

got="$(dl_capped)"
check "a compact_rewrite with max_groups 1 in an open transaction rewrites one group and holds one group lock" \
	"$(cut -d' ' -f1-2 <<<"$got")" "verb=1 locks=1"
check "and a delete in the group that max_groups left out, with a 1 s lock_timeout, commits while that transaction is open" \
	"$(cut -d' ' -f3 <<<"$got")" "delete=ok"

# ---- the 10 s default, recluster's last group ----
#
# Review of #2139 at b1d94859. Each arm reddens one way of getting that wrong:
#
#   - the default: pgcolumnar.rewrite_lock_timeout defaults to 10 s, and every session
#     that does not set it runs on that, the maintenance daemon too. A default of 0 would
#     wait without a limit. A fresh session shows 10s, and each verb run with no SET
#     behind a deleter held for 15 s stops with 55P03 naming the setting after 9 to 12 s.
#   - recluster's last group: recluster locks every group it rewrites, the last one too.
#     It pauses on its first row with its group locks taken, and a delete of id 2500, in
#     the last group, with a 1 s lock_timeout stops at its group lock. Had it gone through,
#     the recluster would write the row back from its own snapshot.

# dl_paused_last -> "delete=<ok|stock|other> rewrite=<ok|error> row2500=<n>": a recluster
# pauses with its group locks taken; then one session deletes id 2500, in the last group,
# with a 1 s lock_timeout and flushes the delete with a scan
dl_paused_last() {
	dl_pause_bg "pgcolumnar.recluster('dl', 'id')"
	local out d_rc
	# one session waits for the pause and deletes, so a slow connect cannot miss it
	out="$(dlp -qAt -v ON_ERROR_STOP=1 2>&1 <<SQL
$(dl_wait_activity_sql "EXISTS (SELECT 1 FROM pg_stat_activity a WHERE $DL_PAUSED AND a.pid <> pg_backend_pid())")
SET lock_timeout = '1s';
BEGIN;
DELETE FROM dl WHERE id = 2500;
SELECT count(*) FROM dl WHERE id = 2500;
COMMIT;
SQL
)"
	d_rc=$?
	dl_rw_wait
	echo "delete=$(if [ "$d_rc" = 0 ]; then echo ok
		elif grep -q 'canceling statement due to lock timeout' <<<"$out"; then echo stock
		else echo other; fi)" \
		"rewrite=$DL_RW" \
		"row2500=$(q "SELECT count(*) FROM dl WHERE id = 2500;")"
	grep -h 'ERROR' "$PGC_WORKDIR/dl.rw" >&2
	grep 'ERROR' <<<"$out" >&2
	return 0
}

check "a fresh session's pgcolumnar.rewrite_lock_timeout is the documented default of 10s" \
	"$(q "SHOW pgcolumnar.rewrite_lock_timeout;")" "10s"
for verb in "pgcolumnar.recluster('dl', 'id')" "pgcolumnar.compact_rewrite('dl', 0.0)"; do
	name="${verb%%(*}"; name="${name#pgcolumnar.}"
	got="$(dl_held "$verb" "$DL_DELETER" 101 15 "")"
	check "a $name with no SET behind a deleter held for 15 s stops with 55P03 naming rewrite_lock_timeout" \
		"$(cut -d' ' -f2-3 <<<"$got")" "55P03 names-limit"
	check "and the $name with no SET stops at the 10 s default, after 9 to 12 s" \
		"$(awk '{print ($1 >= 9000 && $1 < 12000) ? "yes" : "no (" $1 " ms)"}' <<<"$got")" "yes"
done
got="$(dl_paused_last)"
check "a delete in the last group of a paused recluster, with a 1 s lock_timeout, stops with the stock lock-timeout error" \
	"$(cut -d' ' -f1 <<<"$got")" "delete=stock"
check "and the paused recluster commits, and row 2500 is gone exactly when that delete committed" \
	"$(awk '{c = (($1 == "delete=ok" && $3 == "row2500=0") || ($1 != "delete=ok" && $3 == "row2500=1")) ? "yes" : "no (" $1 " " $3 ")"; print $2 " " c}' <<<"$got")" "rewrite=ok yes"

# ---- a busy last group, a limit of 0 under a user lock_timeout ----
#
# Review of #2139 at 42763bd3. Each arm reddens one way of getting that wrong:
#
#   - a busy last group: before it waits on a busy group, the rewrite gives back every
#     group lock it took in that round, not only the last one. The arms above put the
#     busy group second, so one lock was held. Here all three groups have deletes, so
#     compact_rewrite rewrites all three too, and the writer deletes id 2501 in group 2,
#     the last one.
#     The verb takes groups 0 and 1 and finds group 2 busy. The writer waits until the
#     verb waits, then updates id 5 in group 0. A rewrite that kept group 0 through its
#     wait closes the #2074 cycle, and the deadlock detector aborts it.
#   - a limit of 0 under a user lock_timeout: with pgcolumnar.rewrite_lock_timeout = 0
#     the wait runs under the user's own lock_timeout, and its expiry is PostgreSQL's
#     own lock-timeout error. It must not say the limit ran out: there is no limit.

for verb in "pgcolumnar.recluster('dl', 'id')" "pgcolumnar.compact_rewrite('dl', 0.0)"; do
	name="${verb%%(*}"; name="${name#pgcolumnar.}"
	got="$(dl_giveback "$verb" 2501 all)"
	check "a $name gave back its group locks before it waited on its last group, which was busy" \
		"$(grep -o 'waited=[a-z]*' <<<"$got")" "waited=t"
	check "a DELETE in the last group then an UPDATE in group 0 from one writer and the $name both commit" \
		"$(sed 's/^waited=[a-z]* //' <<<"$got")" "writer=ok rewrite=ok"
	check "and the DELETE in the last group and the UPDATE in group 0 beside the $name took effect" \
		"$(q "SELECT count(*) FILTER (WHERE id = 2501) || ' ' || max(bal) FILTER (WHERE id = 5) FROM dl;")" "0 1001"
	check "while the $name waited on its busy last group, it held no group lock" \
		"$(grep -o 'held=[a-z0-9]*' "$PGC_WORKDIR/dl.w" || echo held=missing)" "held=0"
	got="$(dl_held "$verb" "$DL_DELETER" 101 4 "SET pgcolumnar.rewrite_lock_timeout = 0; SET lock_timeout = '500ms';")"
	check "a $name with rewrite_lock_timeout 0 under a lock_timeout of 500 ms stops with the stock lock-timeout error" \
		"$(cut -d' ' -f2-3 <<<"$got")" "55P03 stock"
	check "and the $name with rewrite_lock_timeout 0 stops within that 500 ms lock_timeout plus 1.5 s" \
		"$(awk '{print ($1 < 2000) ? "yes" : "no (" $1 " ms)"}' <<<"$got")" "yes"
done

# ---- no group lock held through the wait, the limit's clock starts at the call ----
#
# Review of #2139 at 1b2b7ceb.
#
#   - no group lock held: the give-back frees each group lock it took in the round, not
#     some other one. Every give-back arm's writer updates id 5, in group 0, so a rewrite
#     that freed group 0 twice and kept group 1 commits beside it. Instead, the arm above
#     reads pg_locks while the verb waits on its busy last group: the waiting session must
#     hold no granted group lock (class 101). This covers every index at once.
#   - the clock starts at the call: pgcolumnar.rewrite_lock_timeout bounds the waiting
#     itself, so its deadline counts from the moment the verb starts to take its locks.
#     A deadline counted from the statement's or the transaction's start would charge the
#     verb for whatever ran before it, such as the maintenance daemon's compact_rewrite
#     before its recluster in one transaction (#2194). With no writer and a 1 s limit, a
#     session sleeps 1.5 s, in the verb's own statement and in an earlier statement of its
#     transaction, and then the verb must finish.

for verb in "pgcolumnar.recluster('dl', 'id')" "pgcolumnar.compact_rewrite('dl', 0.0)"; do
	name="${verb%%(*}"; name="${name#pgcolumnar.}"
	check "a $name with a 1 s rewrite_lock_timeout, after 1.5 s of sleep earlier in its own statement, finishes" \
		"$(dl_late "$verb" stmt)" "ok"
	check "a $name with a 1 s rewrite_lock_timeout, after 1.5 s of sleep earlier in its transaction, finishes" \
		"$(dl_late "$verb" xact)" "ok"
done

# ---- the rewrite turnstile: forward progress under steady writers ----
#
# Issue #2185. The rewrite never waits in the lock manager while it holds a lock, so
# writers never queued behind it, and under steady deletes it stopped with 55P03 (the
# "finished, with no 55P03" arm above). Now it holds its storage's turnstile (advisory
# class 107) while it takes its locks, and a transaction that holds no row-group or
# storage-row lock yet polls the turnstile, for at most its deadlock_timeout, before it
# takes its first one. Each arm reddens one way of getting that wrong:
#
#   - waits: a new transaction waits at the turnstile while the rewrite waits on a busy
#     group. Its session wrote and committed before, so a mark that outlived that
#     transaction would let it skip.
#   - bounded: it stops waiting after its deadlock_timeout (1 s), though the rewrite
#     still waits; a writer is never held longer than that.
#   - holder skips: a transaction that already holds a group lock, or the storage-row
#     lock, does not wait (the rewrite may be waiting for that very lock). Its
#     deadlock_timeout is 3 s, so a wait would show.
#   - given back: compact_rewrite gives the turnstile back once it has its locks, so a
#     delete in a group it does not rewrite, with a 3 s deadlock_timeout, goes on at
#     once while the rewrite pauses on its first row.

# dl_turnstile VERB-SQL -> "n=<ms> h=<ms> h2=<ms> rewrite=<ok|error>": D deletes id 1501
# (group 1) and stays open 6 s; H deletes id 2997 (group 2) and stays open; H2 inserts
# a row, so it holds the storage-row lock, and stays open. The verb starts behind them
# with a 20 s limit. Once the verb waits on the storage-row lock, H2 deletes id 47 and
# commits. Once the verb waits on a group lock, H deletes id 2996, in its own group, and
# N, whose session committed a delete of id 49 first, deletes id 50 in a fresh
# transaction. Each reports how long its last delete took
dl_ms_sql() {
	echo "round(extract(epoch FROM clock_timestamp() - current_setting('pgc.t0')::timestamptz) * 1000)"
}
dl_turnstile() {
	dl_make extra
	rm -f "$PGC_WORKDIR/dl.d" "$PGC_WORKDIR/dl.h" "$PGC_WORKDIR/dl.h2" "$PGC_WORKDIR/dl.n" "$PGC_WORKDIR/dl.rw"
	(
		dlp -qAt -v ON_ERROR_STOP=1 -c "BEGIN; DELETE FROM dl WHERE id = 1501;
			SELECT count(*) FROM dl; SELECT pg_sleep(6); COMMIT;" >"$PGC_WORKDIR/dl.d" 2>&1
	) &
	local d_pid=$!
	(
		dlp -qAt -v ON_ERROR_STOP=1 >"$PGC_WORKDIR/dl.h" 2>&1 <<SQL
SET deadlock_timeout = '3s';
BEGIN;
DELETE FROM dl WHERE id = 2997;
SELECT count(*) FROM dl;
$(dl_wait_sql "$(dl_waiting 101)")
SELECT set_config('pgc.t0', clock_timestamp()::text, false) IS NULL;
DELETE FROM dl WHERE id = 2996;
SELECT count(*) FROM dl WHERE id = 2996;
SELECT 'h=' || $(dl_ms_sql);
COMMIT;
SQL
	) &
	local h_pid=$!
	(
		dlp -qAt -v ON_ERROR_STOP=1 >"$PGC_WORKDIR/dl.h2" 2>&1 <<SQL
SET deadlock_timeout = '3s';
BEGIN;
INSERT INTO dl VALUES (0, 0);
SELECT count(*) FROM dl;
$(dl_wait_sql "$(dl_waiting 102)")
SELECT set_config('pgc.t0', clock_timestamp()::text, false) IS NULL;
DELETE FROM dl WHERE id = 47;
SELECT count(*) FROM dl WHERE id = 47;
SELECT 'h2=' || $(dl_ms_sql);
COMMIT;
SQL
	) &
	local h2_pid=$!
	dlp -qAt -c "$(dl_wait_sql "(SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objsubid = 101 AND granted) >= 2
		AND EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND objsubid = 102 AND granted)")" >/dev/null
	rm -f "$PGC_WORKDIR/dl.rw"
	(
		dlp -qAt -v ON_ERROR_STOP=1 -c "SET pgcolumnar.rewrite_lock_timeout = '20s';" \
			-c "SELECT $1;" >"$PGC_WORKDIR/dl.rw" 2>&1
		echo "rc=$?" >>"$PGC_WORKDIR/dl.rw"
	) &
	DL_RW_PID=$!
	dlp -qAt -v ON_ERROR_STOP=1 >"$PGC_WORKDIR/dl.n" 2>&1 <<SQL
SET deadlock_timeout = '1s';
DELETE FROM dl WHERE id = 49;
$(dl_wait_sql "$(dl_waiting 101)")
SELECT set_config('pgc.t0', clock_timestamp()::text, false) IS NULL;
BEGIN;
DELETE FROM dl WHERE id = 50;
SELECT count(*) FROM dl WHERE id = 50;
COMMIT;
SELECT 'n=' || $(dl_ms_sql);
SQL
	wait "$d_pid" "$h_pid" "$h2_pid"
	dl_rw_wait
	echo "$(grep -o '^n=.*' "$PGC_WORKDIR/dl.n" || echo n=none)" \
		"$(grep -o '^h=.*' "$PGC_WORKDIR/dl.h" || echo h=none)" \
		"$(grep -o '^h2=.*' "$PGC_WORKDIR/dl.h2" || echo h2=none)" "rewrite=$DL_RW"
	grep -h 'ERROR' "$PGC_WORKDIR/dl.d" "$PGC_WORKDIR/dl.h" "$PGC_WORKDIR/dl.h2" "$PGC_WORKDIR/dl.n" "$PGC_WORKDIR/dl.rw" >&2
	return 0
}

# dl_given_back -> "groups=<yes|no> p=<ms> rewrite=<ok|error>": compact_rewrite pauses on its
# first row with its lock on group 0 taken; then a delete of id 1502, in group 1, which it
# does not rewrite, runs with a 3 s deadlock_timeout and reports how long it took
dl_given_back() {
	dl_pause_bg "pgcolumnar.compact_rewrite('dl', 0.0)"
	local out
	out="$(dlp -qAt -v ON_ERROR_STOP=1 2>&1 <<SQL
$(dl_wait_activity_sql "EXISTS (SELECT 1 FROM pg_stat_activity a WHERE $DL_PAUSED AND a.pid <> pg_backend_pid())")
SELECT 'groups=' || CASE WHEN count(*) > 0 THEN 'yes' ELSE 'no' END
FROM pg_locks l JOIN pg_stat_activity a USING (pid)
WHERE l.locktype = 'advisory' AND l.objsubid = 101 AND l.granted AND $DL_PAUSED;
SET deadlock_timeout = '3s';
SELECT set_config('pgc.t0', clock_timestamp()::text, false) IS NULL;
BEGIN;
DELETE FROM dl WHERE id = 1502;
SELECT count(*) FROM dl WHERE id = 1502;
COMMIT;
SELECT 'p=' || $(dl_ms_sql);
SQL
)"
	dl_rw_wait
	echo "$(grep -o '^groups=.*' <<<"$out" || echo groups=none)" \
		"$(grep -o '^p=.*' <<<"$out" || echo p=none)" "rewrite=$DL_RW"
	grep 'ERROR' <<<"$out" >&2
	grep -h 'ERROR' "$PGC_WORKDIR/dl.rw" >&2
	return 0
}

# dl_ms_in WORD LO HI: "yes" when WORD=<ms> in the input is a number in [LO, HI), else what it was
dl_ms_in() {
	awk -v w="$1" -v lo="$2" -v hi="$3" '{for (i = 1; i <= NF; i++) if (index($i, w "=") == 1) {
		v = substr($i, length(w) + 2); print (v ~ /^[0-9]+$/ && v + 0 >= lo && v + 0 < hi) ? "yes" : "no (" $i ")"; exit }
		print "no (" w " missing)"}'
}

for verb in "pgcolumnar.recluster('dl', 'id')" "pgcolumnar.compact_rewrite('dl', 0.0)"; do
	name="${verb%%(*}"; name="${name#pgcolumnar.}"
	got="$(dl_turnstile "$verb")"
	check "a new transaction, in a session that wrote before, waits at the turnstile while the $name waits on a busy group" \
		"$(dl_ms_in n 800 100000 <<<"$got")" "yes"
	check "and it stops waiting after its 1 s deadlock_timeout, within 2.5 s, while the $name still waits" \
		"$(dl_ms_in n 0 2500 <<<"$got")" "yes"
	check "a transaction that already holds a group lock does not wait at the $name's turnstile, under 1 s against its 3 s deadlock_timeout" \
		"$(dl_ms_in h 0 1000 <<<"$got")" "yes"
	check "a transaction that holds the storage-row lock does not wait at the $name's turnstile, under 1 s against its 3 s deadlock_timeout" \
		"$(dl_ms_in h2 0 1000 <<<"$got")" "yes"
	check "and the $name behind those writers finishes" "$(grep -o 'rewrite=[a-z]*' <<<"$got")" "rewrite=ok"
	check "and every write beside the $name's turnstile took effect" \
		"$(q "SELECT count(*) FILTER (WHERE id IN (47, 49, 50, 1501, 2996, 2997)) || ' ' || count(*) FILTER (WHERE id = 0) FROM dl;")" "0 1"
done
got="$(dl_given_back)"
check "a compact_rewrite paused after it took its group lock holds it" "$(cut -d' ' -f1 <<<"$got")" "groups=yes"
check "and a delete in a group it does not rewrite, with a 3 s deadlock_timeout, goes on within 1.5 s: the turnstile was given back" \
	"$(dl_ms_in p 0 1500 <<<"$got")" "yes"
check "and that paused compact_rewrite commits" "$(cut -d' ' -f3 <<<"$got")" "rewrite=ok"

pgc_summary
