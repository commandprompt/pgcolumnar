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

pgc_summary
