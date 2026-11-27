#!/usr/bin/env bash
#
# A rewrite inside a REPEATABLE READ or SERIALIZABLE transaction must not change what
# that transaction sees (#2167).
#
# compact_rewrite, recluster, cluster, vacuum and vacuum_sorted read the newest
# committed rows, because a row they skipped would be lost when they retire the old
# groups. They write those rows into groups the calling transaction owns, and a
# transaction always sees its own rows. So a REPEATABLE READ transaction that ran one
# of them saw a row another session inserted after its snapshot, and lost a row
# another session deleted after it. Heap's CLUSTER keeps each row's xmin and xmax, so
# the transaction sees neither change (the heap control below).
#
# A row group has one xmin for all its rows, so a rewrite cannot keep them. Instead,
# at those levels, the verb compares what the transaction's snapshot and its own read
# see of the groups it rewrites. When they differ it raises a serialization failure
# (40001), and a retry, with a new snapshot, runs. When they agree it runs, and the
# transaction sees the rows it saw before.
#
# The maintenance daemon runs these verbs too, and it now forces READ COMMITTED, as
# core's autovacuum does, so a default_transaction_isolation of repeatable read does not
# make it refuse on every busy table. The last arm checks that.
#
# Every other arm is sequential: session A takes its snapshot, session B commits its
# change from inside A's script (psql \!), then A runs the verb. Each session has a
# statement_timeout and a lock_timeout, so a hang fails the arm instead of the run.
#
# Usage:  test/rewrite_snapshot.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

RS_OPTS="-c statement_timeout=60s -c lock_timeout=30s"
RS_MSG='could not rewrite table "%s": rows changed after this transaction'"'"'s snapshot'

# The other session's change, as SQL, by name. TBL is the arm's table.
rs_change() {	# rs_change NAME TBL
	case "$1" in
		none) echo "SELECT 1;" ;;
		ins) echo "INSERT INTO $2 VALUES (70, 30);" ;;
		del) echo "DELETE FROM $2 WHERE id = 3;" ;;
		both) echo "INSERT INTO $2 VALUES (70, 30); DELETE FROM $2 WHERE id = 3;" ;;
		replace) echo "DELETE FROM $2 WHERE id BETWEEN 101 AND 105; SELECT pgcolumnar.compact_rewrite('$2', 0.0);" ;;
	esac
}

# rs_race TBL AM ISO CHANGE VERB [PRE] [SECOND]
#   -> "ok|<count>|<sum of id> A reads after the verb|<count>|<sum> after commit"
#   or "<SQLSTATE>|<message>|<count>|<sum> after the rollback"
# The table holds ids 1-50 in one group (51-60 deleted, so compact_rewrite has work),
# plus ids 101-110 in a second group when SECOND is set. PRE runs in A's transaction
# after its snapshot and before the verb.
rs_race() {
	local tbl="$1" am="$2" iso="$3" change="$4" verb="$5" pre="${6:-}" second="${7:-}"
	local bsql="$PGC_WORKDIR/$tbl.b.sql" out="$PGC_WORKDIR/$tbl.a.out" err
	psql_run "DROP TABLE IF EXISTS $tbl; CREATE TABLE $tbl (id int PRIMARY KEY, v int) USING $am;
	          CREATE INDEX ${tbl}_v ON $tbl (v);
	          INSERT INTO $tbl SELECT g, 100 - g FROM generate_series(1, 60) g;
	          DELETE FROM $tbl WHERE id > 50;"
	[ -n "$second" ] && psql_run "INSERT INTO $tbl SELECT g, g FROM generate_series(101, 110) g;"
	rs_change "$change" "$tbl" > "$bsql"
	env PATH="$PGC_BINDIR:$PATH" PGOPTIONS="$RS_OPTS" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -Atq -v ON_ERROR_STOP=1 -v VERBOSITY=verbose > "$out" 2>&1 <<SQL
BEGIN ISOLATION LEVEL $iso;
SELECT 'snap|' || count(*) FROM $tbl;
\\! env PGOPTIONS="$RS_OPTS" psql -h 127.0.0.1 -p $PGC_PORT -U postgres -d $PGC_DB -q -v ON_ERROR_STOP=1 -f $bsql > /dev/null 2>&1
$pre
SELECT $verb;
SELECT 'after|' || count(*) || '|' || coalesce(sum(id), 0) FROM $tbl;
COMMIT;
SQL
	err="$(sed -n 's/^ERROR:[[:space:]]*\([0-9A-Z]\{5\}\):[[:space:]]*\(.*\)$/\1|\2/p' "$out" | head -1)"
	if [ -n "$err" ]; then
		echo "$err|$(q "SELECT count(*) || '|' || coalesce(sum(id), 0) FROM $tbl")"
	else
		echo "ok|$(sed -n 's/^after|//p' "$out" | head -1)|$(q "SELECT count(*) || '|' || coalesce(sum(id), 0) FROM $tbl")"
	fi
}
msg() { printf "$RS_MSG" "$1"; }

# ---- heap's answer ------------------------------------------------------------------
#
# CLUSTER copies each row with its xmin and xmax, so the REPEATABLE READ transaction
# still counts 50 rows summing 1275: row 70, inserted after its snapshot, stays
# invisible, and row 3, deleted after it, stays visible. The commit then shows both.
check "heap control: CLUSTER at REPEATABLE READ keeps a row inserted and a row deleted after the snapshot as the snapshot saw them" \
	"$(rs_race rs_heap heap 'REPEATABLE READ' both 'NULL; CLUSTER rs_heap USING rs_heap_v')" "ok|50|1275|50|1342"

# ---- a change after the snapshot: refused ------------------------------------------
#
# Each arm's other session inserts row 70 (a new group), deletes row 3 (a deletion in
# a group the verb rewrites), or both. Before #2167 each verb ran and A then counted
# 51|1345, 49|1272 or 50|1342. The rollback leaves the other session's change alone.
check "recluster at REPEATABLE READ raises 40001 when a row was inserted after the snapshot" \
	"$(rs_race rs_rc_ins pgcolumnar 'REPEATABLE READ' ins "pgcolumnar.recluster('rs_rc_ins', 'v')")" \
	"40001|$(msg rs_rc_ins)|51|1345"
check "recluster at REPEATABLE READ raises 40001 when a row was deleted after the snapshot" \
	"$(rs_race rs_rc_del pgcolumnar 'REPEATABLE READ' del "pgcolumnar.recluster('rs_rc_del', 'v')")" \
	"40001|$(msg rs_rc_del)|49|1272"
check "recluster at SERIALIZABLE raises 40001 when a row was inserted after the snapshot" \
	"$(rs_race rs_rc_ser pgcolumnar 'SERIALIZABLE' ins "pgcolumnar.recluster('rs_rc_ser', 'v')")" \
	"40001|$(msg rs_rc_ser)|51|1345"
check "compact_rewrite at REPEATABLE READ raises 40001 when a row in a group it rewrites was deleted after the snapshot" \
	"$(rs_race rs_cr_del pgcolumnar 'REPEATABLE READ' del "pgcolumnar.compact_rewrite('rs_cr_del', 0.0)")" \
	"40001|$(msg rs_cr_del)|49|1272"
check "cluster at REPEATABLE READ raises 40001 when a row was inserted after the snapshot" \
	"$(rs_race rs_cl_ins pgcolumnar 'REPEATABLE READ' ins "pgcolumnar.cluster('rs_cl_ins', 'v')")" \
	"40001|$(msg rs_cl_ins)|51|1345"
check "cluster at REPEATABLE READ raises 40001 when a row was deleted after the snapshot" \
	"$(rs_race rs_cl_del pgcolumnar 'REPEATABLE READ' del "pgcolumnar.cluster('rs_cl_del', 'v')")" \
	"40001|$(msg rs_cl_del)|49|1272"
# The other session deletes five rows of the second group, then its compact_rewrite
# replaces both groups with two new ones. The two snapshots list as many groups, with
# no deleted rows in the new ones, and only the group numbers differ. 60 rows at the
# snapshot; before #2167 A counted 55 after its cluster.
check "cluster at REPEATABLE READ raises 40001 when another session's compact_rewrite replaced its groups after the snapshot" \
	"$(rs_race rs_cl_rep pgcolumnar 'REPEATABLE READ' replace "pgcolumnar.cluster('rs_cl_rep', 'v')" '' second)" \
	"40001|$(msg rs_cl_rep)|55|1815"
check "vacuum at REPEATABLE READ raises 40001 when rows were inserted and deleted after the snapshot" \
	"$(rs_race rs_va pgcolumnar 'REPEATABLE READ' both "pgcolumnar.vacuum('rs_va')")" \
	"40001|$(msg rs_va)|50|1342"
check "vacuum_sorted at REPEATABLE READ raises 40001 when rows were inserted and deleted after the snapshot" \
	"$(rs_race rs_vs pgcolumnar 'REPEATABLE READ' both "pgcolumnar.vacuum_sorted('rs_vs', 'v')")" \
	"40001|$(msg rs_vs)|50|1342"

# ---- no change the verb would carry: it runs ----------------------------------------
#
# With nothing changed after the snapshot, every verb runs at REPEATABLE READ and the
# transaction still counts the 50 rows it saw. compact_rewrite compares only the
# groups it rewrites, so a row inserted into a new group does not stop it, and A still
# does not see that row. A's own insert and delete, earlier in its transaction, are
# seen by both snapshots alike, so they do not stop the verb either.
check "at REPEATABLE READ, with nothing changed after the snapshot, all five verbs run and the transaction sees the rows it saw" \
	"$(rs_race rs_none pgcolumnar 'REPEATABLE READ' none "pgcolumnar.compact_rewrite('rs_none', 0.0); SELECT pgcolumnar.recluster('rs_none', 'v');
		SELECT pgcolumnar.cluster('rs_none', 'v'); SELECT pgcolumnar.vacuum('rs_none'); SELECT pgcolumnar.vacuum_sorted('rs_none', 'v')")" \
	"ok|50|1275|50|1275"
check "at REPEATABLE READ, compact_rewrite runs when the only change after the snapshot is a row in a group it does not rewrite" \
	"$(rs_race rs_cr_ins pgcolumnar 'REPEATABLE READ' ins "pgcolumnar.compact_rewrite('rs_cr_ins', 0.0)")" \
	"ok|50|1275|51|1345"
check "at REPEATABLE READ, recluster runs after the transaction's own insert and delete, and it sees both" \
	"$(rs_race rs_own_recluster pgcolumnar 'REPEATABLE READ' none "pgcolumnar.recluster('rs_own_recluster', 'v')" \
		"INSERT INTO rs_own_recluster VALUES (80, 20); DELETE FROM rs_own_recluster WHERE id = 4;")" \
	"ok|50|1351|50|1351"
check "at REPEATABLE READ, cluster runs after the transaction's own insert and delete, and it sees both" \
	"$(rs_race rs_own_cluster pgcolumnar 'REPEATABLE READ' none "pgcolumnar.cluster('rs_own_cluster', 'v')" \
		"INSERT INTO rs_own_cluster VALUES (80, 20); DELETE FROM rs_own_cluster WHERE id = 4;")" \
	"ok|50|1351|50|1351"

# ---- READ COMMITTED -----------------------------------------------------------------
#
# Each statement takes a new snapshot, so the verb's statement already sees the other
# session's commit, and so does every statement after it.
check "at READ COMMITTED, recluster in a transaction runs, and the transaction sees rows committed before it" \
	"$(rs_race rs_rcom pgcolumnar 'READ COMMITTED' both "pgcolumnar.recluster('rs_rcom', 'v')")" \
	"ok|50|1342|50|1342"

# ---- the maintenance daemon runs at READ COMMITTED ----------------------------------
#
# The daemon opens one transaction per table and takes its snapshot before the verb's
# locks. With default_transaction_isolation = 'repeatable read' that snapshot would
# be the transaction's, and a writer that changed a group while the verb waited would
# make the verb refuse, pass after pass on a busy table. The daemon forces READ
# COMMITTED, as core's autovacuum does. Its compact_rewrite holds its lock for 3 s
# (pgcolumnar.maintenance_hold_ms) before it lists and locks its groups; a delete in
# the group it is about to rewrite commits inside that hold.
psql_run "DO \$\$DECLARE t text; BEGIN FOR t IN SELECT relname FROM pg_class WHERE relname LIKE 'rs\_%' AND relkind = 'r' LOOP EXECUTE format('DROP TABLE %I', t); END LOOP; END\$\$;"
psql_run "CREATE TABLE rs_av (id int, v int) USING pgcolumnar;
          SELECT pgcolumnar.set_options('rs_av', chunk_group_row_limit => 1000);
          INSERT INTO rs_av SELECT g, g FROM generate_series(1, 2000) g;
          DELETE FROM rs_av WHERE id % 5 = 0 AND id <= 1000;"
# one ALTER SYSTEM per psql -c: several in one -c run as one transaction, which it refuses
for set in "default_transaction_isolation = 'repeatable read'" "pgcolumnar.autovacuum_naptime = 1" \
	"pgcolumnar.autovacuum_compact_threshold = 0.1" "pgcolumnar.maintenance_hold_ms = 3000" "pgcolumnar.autovacuum = on"; do
	psql_run "ALTER SYSTEM SET $set;"
done
psql_run "SELECT pg_reload_conf();" >/dev/null
sleep 1
check "premise: a new session's default_transaction_isolation is repeatable read, with the daemon on" \
	"$(q "SELECT current_setting('default_transaction_isolation') || '/' || current_setting('pgcolumnar.autovacuum')")" \
	"repeatable read/on"
rs_av_held() {
	q "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid
	   WHERE l.relation = 'rs_av'::regclass AND l.mode = 'ShareUpdateExclusiveLock'
	     AND l.granted AND a.backend_type = 'pgcolumnar autovacuum worker'"
}
rs_av_outcome() {	# the daemon's log line for rs_av: rewrote, or skipped and why
	local i line
	for i in $(seq 1 60); do
		line="$(grep -E 'pgcolumnar autovacuum: (compact_rewrite|skipped) public\.rs_av' "$PGC_LOGFILE" | head -1)"
		[ -n "$line" ] && break
		sleep 0.5
	done
	case "$line" in
		*"compact_rewrite public.rs_av"*) echo "compact_rewrite" ;;
		*skipped*) echo "skipped: ${line#*rs_av: }" ;;
		*) echo "no log line for rs_av" ;;
	esac
}
held=no
for _ in $(seq 1 60); do
	[ "$(rs_av_held)" = 1 ] && { held=yes; break; }
	sleep 0.5
done
psql_run "DELETE FROM rs_av WHERE id = 3;"
check "under default_transaction_isolation = repeatable read, the daemon's compact_rewrite rewrites a group a delete changed while it held its lock" \
	"$held|$(rs_av_outcome)|$(q "SELECT count(*) FROM rs_av")" "yes|compact_rewrite|1799"
for set in default_transaction_isolation pgcolumnar.autovacuum pgcolumnar.autovacuum_naptime \
	pgcolumnar.autovacuum_compact_threshold pgcolumnar.maintenance_hold_ms; do
	psql_run "ALTER SYSTEM RESET $set;"
done
psql_run "SELECT pg_reload_conf();" >/dev/null

pgc_summary
