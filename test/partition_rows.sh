#!/usr/bin/env bash
#
# Rows that cross columnar partitions, and keys enforced through a partitioned
# parent (#1341). No test moved a row between columnar partitions with an UPDATE,
# or put a PRIMARY KEY on a parent whose partitions are columnar.
#
# A row-moving UPDATE is a delete in one partition and an insert in another, and
# PostgreSQL asks the access method to report a concurrent writer of the moved row
# as a serialization failure rather than letting it find nothing. A key on the
# parent is one unique index per partition, each reached through the partition's
# own access method and its unique-insert lock.
#
# Each arm runs on a parent with two columnar partitions and on one with two heap
# partitions, and heap is the answer.
#
# Usage:  test/partition_rows.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

for am in pgcolumnar heap; do
	psql_run "CREATE TABLE pr_$am (id int PRIMARY KEY, k int, v text) PARTITION BY RANGE (id);
	          CREATE TABLE pr_${am}_1 PARTITION OF pr_$am FOR VALUES FROM (0) TO (1000) USING $am;
	          CREATE TABLE pr_${am}_2 PARTITION OF pr_$am FOR VALUES FROM (1000) TO (3000) USING $am;
	          INSERT INTO pr_$am SELECT g, g % 7, 'r' || g FROM generate_series(1, 999) g;"
done

outcome() {	# outcome SQL -> ok, or the SQLSTATE the statement failed with
	local out st
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-X -qtA -v ON_ERROR_STOP=1 -v VERBOSITY=sqlstate -c "$1" 2>&1)"
	st="$(sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\).*/\1/p' <<<"$out")"
	echo "${st:-ok}"
}
both() {	# both SQL -> "<columnar outcome>|<heap outcome>", %p standing for the parent
	echo "$(outcome "${1//%p/pr_pgcolumnar}")|$(outcome "${1//%p/pr_heap}")"
}
answers() {	# answers SQL -> "<columnar answer>|<heap answer>", %p standing for the parent
	echo "$(q "${1//%p/pr_pgcolumnar}" | tail -1)|$(q "${1//%p/pr_heap}" | tail -1)"
}
same() {	# same SQL -> "same" when both parents answer alike, else both answers
	local a; a="$(answers "$1")"
	[ "${a%%|*}" = "${a#*|}" ] && echo same || echo "$a"
}
CONTENT="SELECT count(*) || '/' || md5(string_agg(id || ':' || k || ':' || v, ',' ORDER BY id)) FROM %p"
SPLIT="SELECT (SELECT count(*) FROM %p_1) || '/' || (SELECT count(*) FROM %p_2)"

# ---- a row-moving UPDATE ----------------------------------------------------------
check "an UPDATE moving 49 rows to the other partition succeeds, as on heap" \
	"$(both "UPDATE %p SET id = id + 1000 WHERE id < 50")" "ok|ok"
check "and each partition holds what heap's does" "$(answers "$SPLIT")" "950/49|950/49"
check "and the parent holds the same rows as heap's" "$(same "$CONTENT")" "same"
IDX="SET enable_seqscan = off; SET enable_bitmapscan = off;"
check "the primary key finds a moved row at its new key, and not at its old one, as on heap" \
	"$(answers "$IDX SELECT count(*) FILTER (WHERE id = 1001) || '/' || count(*) FILTER (WHERE id = 1) FROM %p WHERE id IN (1, 1001)")" \
	"1/0|1/0"

# ---- the key on the parent ----------------------------------------------------------
check "a duplicate key inserted through the parent is refused (23505), as on heap" \
	"$(both "INSERT INTO %p VALUES (500, 0, 'dup')")" "23505|23505"
check "a key a moved row gave up can be used again, as on heap" \
	"$(both "INSERT INTO %p VALUES (1, 0, 'again')")" "ok|ok"
check "a duplicate of a moved row's new key is refused in its new partition (23505), as on heap" \
	"$(both "INSERT INTO %p VALUES (1001, 0, 'dup')")" "23505|23505"

# race PARENT HOLDER CONTENDER KEY -> "<contender waited>|<contender outcome>|<rows matching
# KEY afterwards>". HOLDER runs in its own transaction, then queues for an advisory
# lock a gate session holds. The contender must be seen waiting before the gate ends,
# and the holder then commits. Events, not sleeps.
pr_wait_for() {	# pr_wait_for APPLICATION_NAME -> its pid
	local i pid=""
	for i in $(seq 1 300); do
		pid="$(q "SELECT pid FROM pg_stat_activity WHERE application_name = '$1'")"
		[ -n "$pid" ] && break; sleep 0.1
	done
	echo "$pid"
}
race() {
	local p="$1" holder="$2" contender="$3" key="$4" out gate i waited=no
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq \
		-c "SELECT pg_advisory_lock(1341); SET application_name = 'pr_gate'; SELECT pg_sleep(600);" >/dev/null 2>&1 &
	local gb=$!
	gate="$(pr_wait_for pr_gate)"
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq -v ON_ERROR_STOP=1 \
		-c "BEGIN; $holder; SET application_name = 'pr_holder'; SELECT pg_advisory_lock_shared(1341); COMMIT;" \
		>"$PGC_WORKDIR/pr_holder.out" 2>&1 &
	local hb=$!
	pr_wait_for pr_holder >/dev/null
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq \
		-v ON_ERROR_STOP=1 -v VERBOSITY=sqlstate \
		-c "SET application_name = 'pr_contender'; SET statement_timeout = '60s'; $contender" \
		>"$PGC_WORKDIR/pr_contender.out" 2>&1 &
	local cb=$!
	for i in $(seq 1 300); do
		[ "$(q "SELECT count(*) FROM pg_stat_activity WHERE application_name = 'pr_contender' AND wait_event_type = 'Lock'")" = 1 ] && { waited=yes; break; }
		sleep 0.1
	done
	q "SELECT pg_terminate_backend(${gate:-0})" >/dev/null
	wait "$gb" 2>/dev/null; wait "$hb"; wait "$cb"
	out="$(sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\).*/\1/p' "$PGC_WORKDIR/pr_contender.out")"
	echo "$waited|${out:-ok}|$(q "SELECT count(*) FROM $p WHERE $key" | tail -1)"
}
both_race() {	# both_race HOLDER CONTENDER KEY -> "<columnar>  <heap>", %p standing for the parent
	echo "$(race pr_pgcolumnar "${1//%p/pr_pgcolumnar}" "${2//%p/pr_pgcolumnar}" "$3")  $(race pr_heap "${1//%p/pr_heap}" "${2//%p/pr_heap}" "$3")"
}

# ---- concurrent writers ----------------------------------------------------------
check "a second insert of the same new key through the parent waits, then is refused (23505), and one row is left, as on heap" \
	"$(both_race "INSERT INTO %p VALUES (1500, 0, 'a')" "INSERT INTO %p VALUES (1500, 0, 'b')" "id = 1500")" \
	"yes|23505|1  yes|23505|1"
check "a second session moving a row the first is moving waits, then fails with 40001, and the row exists once, as on heap" \
	"$(both_race "UPDATE %p SET id = id + 1000 WHERE id = 60" "UPDATE %p SET id = id + 1000 WHERE id = 60" "id IN (60, 1060)")" \
	"yes|40001|1  yes|40001|1"
check "an UPDATE in place of a row another session is moving waits, then fails with 40001, as on heap" \
	"$(both_race "UPDATE %p SET id = id + 1000 WHERE id = 70" "UPDATE %p SET v = 'changed' WHERE id = 70" "id IN (70, 1070) AND v <> 'changed'")" \
	"yes|40001|1  yes|40001|1"
check "and after the races the parents hold the same rows" "$(same "$CONTENT")" "same"

# ---- memory through a keyed UPDATE ---------------------------------------------
# On a keyed table each updated or moved row is checked at once against the
# committed delete marks of its row group (#1421). Those reads must not stay in the
# statement's memory: kept, they cost one copy of the group's delete bitmap per row,
# 1.2 GB for 50,000 rows in OffgridwithJD's measurement (review of #1422). One
# group of 50,000 rows with every even row deleted, so its bitmap is in the
# catalog; pm_mem records the backend's context bytes at the first and the last row
# an UPDATE reaches, and the growth between them is what the statement kept.
# pm_build NAME: a keyed parent NAME, its first partition holding rows 1..50000 in one
# row group with every even row deleted
pm_build() {
	psql_run "CREATE TABLE $1 (id int PRIMARY KEY, v int) PARTITION BY RANGE (id);
	          CREATE TABLE $1_1 PARTITION OF $1 FOR VALUES FROM (0) TO (100000) USING pgcolumnar;
	          CREATE TABLE $1_2 PARTITION OF $1 FOR VALUES FROM (100000) TO (300000) USING pgcolumnar;
	          INSERT INTO $1 SELECT g, 0 FROM generate_series(1, 50000) g;
	          DELETE FROM $1 WHERE id % 2 = 0;"
}
psql_run "CREATE TABLE pm_mem (at text, id int, bytes bigint);
          CREATE FUNCTION pm_mem(at text, id int) RETURNS int LANGUAGE plpgsql AS \$\$
          BEGIN
              IF id IN (1, 49999) THEN
                  INSERT INTO pm_mem SELECT at, id, sum(total_bytes) FROM pg_backend_memory_contexts;
              END IF;
              RETURN 0;
          END \$\$;"
pm_build pmu; pm_build pmm
growth() {	# growth AT -> "readings|growth in MB" for the UPDATE labelled AT
	q "SELECT count(*) || '|' || coalesce(((max(bytes) FILTER (WHERE id = 49999) -
	          max(bytes) FILTER (WHERE id = 1)) / 1048576)::text, '') FROM pm_mem WHERE at = '$1'"
}
bounded() {	# bounded AT -> yes when both readings exist and the growth is under 32 MB
	local g; g="$(growth "$1")"
	[ "${g%%|*}" = 2 ] && [ -n "${g#*|}" ] && [ "${g#*|}" -lt 32 ] && echo yes || echo "no (readings|MB: $g)"
}
marks() {	# marks PARENT -> "live rows|delete_vector rows of its first partition"
	q "SELECT count(*) || '|' || (SELECT count(*) FROM pgcolumnar.delete_vector
	   WHERE storage_id = pgcolumnar.get_storage_id('$1_1')) FROM $1"
}
check "each keyed table's group carries delete marks before its UPDATE" \
	"$(marks pmu) $(marks pmm)" "25000|1 25000|1"
psql_run "UPDATE pmu SET v = v + 1 + pm_mem('update', id);" >/dev/null
check "an UPDATE of 25,000 keyed rows keeps under 32 MB of memory between its first and last row" \
	"$(bounded update)" "yes"
psql_run "UPDATE pmm SET id = id + 100000 + pm_mem('move', id);" >/dev/null
check "an UPDATE moving 25,000 keyed rows to the other partition keeps under 32 MB likewise" \
	"$(bounded move)" "yes"
check "and each UPDATE reached every row once" \
	"$(q "SELECT (SELECT sum(v) FROM pmu) || '/' || (SELECT count(*) FROM pmm_1) || '/' || (SELECT count(*) FROM pmm_2)")" "25000/0/25000"

pgc_summary
