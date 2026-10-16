#!/usr/bin/env bash
#
# A crash part-way through maintenance loses nothing (#1341). compact_rewrite,
# recluster, compact and vacuum retire row groups and write new ones. Two crash
# points, each followed by crash recovery:
#
#   - the function has run but its transaction has not committed, and another
#     session has checkpointed, so its new groups and catalog rows may be on disk.
#     Recovery must leave the table as it was before the function ran;
#   - the function has committed, and nothing has checkpointed since, so its work
#     exists only in WAL. Recovery must leave the table as the function left it.
#
# The table has row groups of 1000 rows, two rows in five deleted, one group deleted
# whole, and an index. After each recovery the rows must match by content, the row
# groups by count and rows, and an index scan must agree with them: with the state
# before, and with a scan of the table after.
#
# Usage:  test/maintenance_crash.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# crash_cluster: SIGKILL the postmaster's process group, as recovery.sh does, and
# wait for it to be gone.
crash_cluster() {
	local pm i
	pm="$(head -1 "$PGC_PGDATA/postmaster.pid" 2>/dev/null || true)"
	if [ -n "$pm" ]; then
		kill -9 -"$pm" 2>/dev/null || true
		kill -9 "$pm" 2>/dev/null || true
	fi
	for i in $(seq 1 100); do
		{ [ -n "$pm" ] && kill -0 "$pm" 2>/dev/null; } || break
		sleep 0.1
	done
	sleep 1
}
restart_cluster() {	# -> ok, once the cluster is back after crash recovery
	local attempt
	for attempt in 1 2 3 4 5 6; do
		if pgc_pg "pg_ctl -D '$PGC_PGDATA' -l '$PGC_LOGFILE' start -w" >/dev/null 2>&1; then
			echo ok; return 0
		fi
		sleep 2
	done
	echo "restart failed"
}
redo_count() {	# how many times the server has logged a crash recovery so far
	local n
	n="$(pgc_pg "grep -c 'database system was not properly shut down' '$PGC_LOGFILE'" 2>/dev/null | tail -1)"
	echo "${n:-0}"	# grep -c prints 0 and exits 1 when nothing matches: no second 0
}

rows() {
	q "SELECT count(*) || '/' || md5(string_agg(id || ':' || v || ':' || w, ',' ORDER BY id)) FROM mc" | grep -E '^[0-9]+/' | tail -1
}
RG_SQL="SELECT count(*) || '/' || coalesce(sum(row_count), 0) FROM pgcolumnar.row_group
         WHERE storage_id = pgcolumnar.get_storage_id('mc')"
BY_INDEX_SETS="SET enable_seqscan = off; SET enable_bitmapscan = off; SET pgcolumnar.enable_custom_scan = off;"
BY_INDEX_SQL="SELECT count(*) || '/' || md5(string_agg(id || ':' || v || ':' || w, ',' ORDER BY id))
	     FROM mc WHERE id BETWEEN 1 AND 10000"
by_index() {
	# pgcolumnar.enable_custom_scan = off as well, and a premise that this plan is an
	# Index Scan: enable_seqscan alone leaves the columnar custom scan on, and this
	# whole-table range planned as Custom Scan (PgColumnarScan). Measured (#1339):
	# with the index fetch reading the neighbouring row, every "by index" arm stayed
	# green, because none of them read the index.
	q "$BY_INDEX_SETS $BY_INDEX_SQL" | grep -E '^[0-9]+/' | tail -1
}
state() {	# the table, as "<rows>|<row groups>|<rows by index>"
	echo "$(rows)|$(q "$RG_SQL" | grep -E '^[0-9]+/' | tail -1)|$(by_index)"
}
fresh_table() {
	psql_run "DROP TABLE IF EXISTS mc;
	          SET pgcolumnar.stripe_row_limit = 1000;
	          CREATE TABLE mc (id int, v int, w text) USING pgcolumnar;
	          INSERT INTO mc SELECT g, (g * 7919) % 10007, md5(g::text) FROM generate_series(1, 10000) g;
	          CREATE INDEX mc_id ON mc (id);
	          DELETE FROM mc WHERE id % 5 IN (1, 2) OR id BETWEEN 3001 AND 4000;
	          CHECKPOINT;" >/dev/null
}

fresh_table
check "premise: the by-index reading is an Index Scan through mc_id" \
	"$(pgc_uses_row_fetch "$BY_INDEX_SETS" "$BY_INDEX_SQL")" yes

FUNCS=("pgcolumnar.compact_rewrite('mc', 0.0)" "pgcolumnar.recluster('mc', 'v')" "pgcolumnar.compact('mc')" "pgcolumnar.vacuum('mc')")

# ---- uncommitted at the crash ---------------------------------------------------------
for f in "${FUNCS[@]}"; do
	name="${f#pgcolumnar.}"; name="${name%%(*}"
	fresh_table
	before="$(state)"
	redo0="$(redo_count)"
	# The open session names itself with its row groups as it sees them, so the suite
	# can tell the function did some work without reading output a SIGKILL may lose.
	# A function that changed nothing would be trivially crash-safe (OffgridwithJD,
	# review of #1423).
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq \
		-c "BEGIN; SELECT $f; SELECT set_config('application_name', 'mc_open ' || ($RG_SQL), false); SELECT pg_sleep(600);" >/dev/null 2>&1 &
	bg=$!
	opened=no; inside=""
	for _i in $(seq 1 300); do
		inside="$(q "SELECT substr(application_name, 9) FROM pg_stat_activity WHERE application_name LIKE 'mc_open %'")"
		[ -n "$inside" ] && { opened=yes; break; }
		sleep 0.1
	done
	check "$name changed the row groups in its open transaction" \
		"$([ -n "$inside" ] && [ "$inside" != "$(cut -d'|' -f2 <<<"$before")" ] && echo changed || echo "unchanged (${inside:-not seen})")" "changed"
	psql_run "CHECKPOINT;" >/dev/null
	crash_cluster
	wait "$bg" 2>/dev/null
	check "$name ran in its open transaction, and the cluster came back after a crash" \
		"$opened|$(restart_cluster)|$(r1="$(redo_count)"; [ "$r1" -gt "$redo0" ] && echo recovered || echo "no recovery logged ($redo0 before, $r1 after)")" \
		"yes|ok|recovered"
	check "after a crash with $name uncommitted, the table is as it was, by scan and by index" \
		"$(state)" "$before"
	# The arm above compares after with before, both through by_index, so an index
	# that was already wrong before the crash agrees with itself there. This one
	# holds the index to the rows a scan reads, now.
	check "after a crash with $name uncommitted, the index reads the rows the scan reads" \
		"$(by_index)" "$(rows)"
done

# ---- committed, not checkpointed ----------------------------------------------------
for f in "${FUNCS[@]}"; do
	name="${f#pgcolumnar.}"; name="${name%%(*}"
	fresh_table
	before_rows="$(rows)"
	before_rg="$(q "$RG_SQL" | grep -E '^[0-9]+/' | tail -1)"
	psql_run "SELECT $f;" >/dev/null
	after="$(state)"
	check "$name changed the row groups before the crash" \
		"$(a="$(cut -d'|' -f2 <<<"$after")"; [ -n "$a" ] && [ "$a" != "$before_rg" ] && echo changed || echo "unchanged ($a)")" "changed"
	redo0="$(redo_count)"
	crash_cluster
	check "$name committed, and the cluster came back after a crash before any checkpoint" \
		"$(restart_cluster)|$(r1="$(redo_count)"; [ "$r1" -gt "$redo0" ] && echo recovered || echo "no recovery logged ($redo0 before, $r1 after)")" "ok|recovered"
	check "after a crash with $name committed, the table is as $name left it, with the same rows" \
		"$(state)|$(rows)" "$after|$before_rows"
	check "after a crash with $name committed, the index reads the rows the scan reads" \
		"$(by_index)" "$(rows)"
done

pgc_summary
