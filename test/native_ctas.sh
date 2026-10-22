#!/usr/bin/env bash
#
# CREATE TABLE AS into a columnar table, with a parallel source plan (issue #387).
#
# PgColumnarInsertNativeStorageRow re-checks for an existing storage row under an
# advisory lock, against GetLatestSnapshot(), so the loser of a cross-transaction
# first-write race sees the winner's committed row instead of failing on
# storage_pkey. GetLatestSnapshot() raises
#
#     ERROR:  cannot update SecondarySnapshot during a parallel operation
#
# under IsInParallelMode(), and CTAS runs the whole executor in parallel mode
# whenever the source plan is parallel. So every CREATE TABLE ... USING pgcolumnar
# AS SELECT over a source large enough to be worth loading failed, on all five
# majors, with the failure arriving exactly when someone bulk loads a real table.
#
# The fix skips the lock and the fresh snapshot when the relation was created by
# this transaction, because then no other session can see it and there is no
# second writer. The condition is creation, not parallel mode: a committed empty
# columnar table can be first-written by two sessions at once, and that race is
# still real and still serialized. Both directions are asserted here.
#
# Every timing-free assertion below is paired with a PLAN assertion, because a
# CTAS that quietly ran serially would pass every value check and prove nothing.
#
# Usage:  test/native_ctas.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

ROWS=${PGC_CTAS_ROWS:-200000}

# Zeroing the parallel costs makes a modest source go parallel, so this runs in
# seconds instead of needing a table large enough to earn a parallel plan.
FORCE="SET parallel_setup_cost=0; SET parallel_tuple_cost=0;
       SET min_parallel_table_scan_size=0; SET max_parallel_workers_per_gather=2;"

# ran_ok SQL -> "ok" when psql ran SQL to the end, or what happened instead. Exit status,
# not the text "ERROR": a backend that crashed mid-statement prints "server closed the
# connection unexpectedly", no ERROR, and the old test read that as success.
ran_ok() {
	local out rc
	out="$(psql_run "$1" 2>&1)"; rc=$?
	if [ "$rc" = 0 ]; then echo ok
	elif grep -q 'ERROR' <<<"$out"; then grep -oE 'ERROR:.*' <<<"$out" | head -1
	else echo "no (psql rc $rc: $(head -1 <<<"$out"))"; fi
}
# await_up -> wait, at most 60 s, until the server answers, so a crash reds its own
# arm and the survival check, not every reading after it.
await_up() {
	local i
	for i in $(seq 1 120); do
		[ "$(q 'SELECT 1')" = 1 ] && return 0
		sleep 0.5
	done
}
# Backends killed by a signal, from the server log: a number, or nothing when the log
# can't be read.
crashes() {
	local _n
	_n="$(pgc_pg "grep -c 'terminated by signal' '$PGC_LOGFILE'" 2>/dev/null)"
	case "$_n" in '' | *[!0-9]*) ;; *) echo "$_n" ;; esac
}
survived() {	# survived BEFORE -> "1" when the server answers and no backend died since
	local _up _now
	await_up
	_up="$(q 'SELECT 1')"; _now="$(crashes)"
	if ! [[ "$1" =~ ^[0-9]+$ && "$_now" =~ ^[0-9]+$ ]]; then echo "no (crash count unreadable)"
	elif [ "$_up" = 1 ] && [ "$_now" = "$1" ]; then echo 1
	else echo "no ([$_up], $(( _now - $1 )) backend crashes)"; fi
}

psql_run "CREATE TABLE ctas_src AS
	SELECT g AS id, 'h'||(g%97) AS host, (g%1000)::float8 AS v
	FROM generate_series(1,$ROWS) g;"

crashes_before="$(crashes)"

# ---- premise: the source plan really is parallel -----------------------------
# Without this the whole suite is satisfied by a serial plan, which is the shape
# that already worked and the reason the defect went unnoticed.
plan="$(psql_run "$FORCE EXPLAIN (COSTS OFF)
	CREATE TABLE ctas_p USING pgcolumnar AS SELECT * FROM ctas_src ORDER BY host;" 2>&1)"
check "premise: the CTAS source plan is parallel" \
	"$(grep -qE 'Gather( Merge)?' <<<"$plan" && echo yes || echo "no ($(head -2 <<<"$plan" | tr '\n' ' '))")" \
	"yes"

# ---- the defect: parallel CTAS into a columnar table -------------------------
check "parallel CTAS into a columnar table succeeds (#387)" \
	"$(ran_ok "$FORCE CREATE TABLE ctas_p USING pgcolumnar AS
	    SELECT * FROM ctas_src ORDER BY host;")" "ok"
await_up
check "parallel CTAS wrote every row (#387)" \
	"$(q "SELECT count(*) FROM ctas_p")" "$ROWS"

# content, not just the row count: a load that silently dropped or reordered
# columns would satisfy a count
check "parallel CTAS content matches the source (#387)" \
	"$(q "SELECT count(*) FROM ctas_p p JOIN ctas_src s USING (id)
	      WHERE p.host = s.host AND p.v = s.v")" "$ROWS"

# ---- controls: the shapes that already worked must keep working --------------
check "serial CTAS still succeeds" \
	"$(ran_ok "SET max_parallel_workers_per_gather=0;
	    CREATE TABLE ctas_s USING pgcolumnar AS SELECT * FROM ctas_src;")" "ok"
await_up
check "serial CTAS row count" "$(q "SELECT count(*) FROM ctas_s")" "$ROWS"

check "WITH NO DATA then INSERT still succeeds" \
	"$(ran_ok "$FORCE CREATE TABLE ctas_nd USING pgcolumnar AS
	    SELECT * FROM ctas_src WITH NO DATA;
	    INSERT INTO ctas_nd SELECT * FROM ctas_src;")" "ok"
await_up
check "WITH NO DATA then INSERT row count" "$(q "SELECT count(*) FROM ctas_nd")" "$ROWS"

# one storage row per storage, however many row groups were flushed: the function
# under test is called on every flush and has to stay idempotent
check "the parallel CTAS produced exactly one storage row" \
	"$(q "SELECT count(*) FROM pgcolumnar.storage s
	      JOIN pg_class c ON c.oid = s.relation_oid WHERE c.relname = 'ctas_p'")" "1"
check "backend still up after the CTAS loads" "$(survived "$crashes_before")" "1"

# ---- the direction that must NOT change --------------------------------------
# The advisory lock exists for a cross-transaction first-write race on a table both
# sessions can see. Creation invisibility is what makes the CTAS case safe, so a
# COMMITTED, EMPTY table must still serialize.
#
# The race is forced, not hoped for. Writer A first-writes the table and holds its
# transaction open; writer B first-writes it while A is still open. With the lock, B
# waits for A's commit, re-checks, finds A's storage row and goes on. Without it, B
# inserts a second storage row and fails on storage_pkey once A commits. Two plain
# concurrent INSERTs used to stand in for this: they rarely overlapped, so a writer
# that dropped the lock passed 12/12.
psql_run "CREATE TABLE ctas_race (id int, v text) USING pgcolumnar;"
crashes_race="$(crashes)"
ra="$PGC_WORKDIR/race_a.out"; rb="$PGC_WORKDIR/race_b.out"
( env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
	-Atq -v ON_ERROR_STOP=1 -c "BEGIN;
		INSERT INTO ctas_race SELECT g, 'w1-'||g FROM generate_series(1,20000) g;
		SELECT pg_sleep(4);
		COMMIT;" >"$ra" 2>&1; echo "rc=$?" >>"$ra" ) &
# B starts only once A has written and is holding its transaction open
for i in $(seq 1 60); do
	[ "$(q "SELECT count(*) FROM pg_stat_activity WHERE query LIKE '%pg_sleep(4)%'
	        AND wait_event = 'PgSleep'")" = 1 ] && break
	sleep 0.25
done
a_open="$(q "SELECT count(*) FROM pg_stat_activity WHERE query LIKE '%pg_sleep(4)%' AND wait_event = 'PgSleep'")"
( env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
	-Atq -v ON_ERROR_STOP=1 -c "INSERT INTO ctas_race
		SELECT g, 'w2-'||g FROM generate_series(1,20000) g;" >"$rb" 2>&1; echo "rc=$?" >>"$rb" ) &
wait
writer() {	# writer FILE -> "ok" when that writer's psql exited 0, else what it printed
	if grep -qx 'rc=0' "$1"; then echo ok
	elif grep -q 'ERROR' "$1"; then grep -oE 'ERROR:.*' "$1" | head -1
	else echo "no ($(grep -v '^rc=' "$1" | head -1) $(grep '^rc=' "$1"))"; fi
}
await_up
check "the second writer started while the first held its transaction open" "$a_open" "1"
check "concurrent first writes to a committed empty table both succeed" \
	"$(echo "A $(writer "$ra"), B $(writer "$rb")")" "A ok, B ok"
check "and they produced one storage row, not two" \
	"$(q "SELECT count(*) FROM pgcolumnar.storage s
	      JOIN pg_class c ON c.oid = s.relation_oid WHERE c.relname = 'ctas_race'")" "1"
check "and every row from both writers is present" \
	"$(q "SELECT count(*) FROM ctas_race")" "40000"
check "backend still up after the concurrent first writes" "$(survived "$crashes_race")" "1"

pgc_summary
