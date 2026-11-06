#!/usr/bin/env bash
#
# pgColumnar #415: the maintenance daemon (pgcolumnar_autovacuum). Off by
# default; when on, a launcher + per-database workers run ONLY the online
# ShareUpdateExclusiveLock verbs (compact_rewrite, recluster) that autovacuum
# cannot reach, gated by pgcolumnar.maintenance_due().
#
# The arms that matter:
#   off      with the daemon off, a deleted-heavy table is NOT touched -- the
#            control that keeps "it compacted" from being vacuously true.
#   on       enabling it (SIGHUP) makes the daemon compact that table within a
#            few naptimes: the dead rows the DELETE left are physically gone.
#   present  the launcher is actually running (pg_stat_activity), so the on-arm
#            cannot pass because of something else.
#
# Timing: naptime is set to 2s, so a poll of ~30s covers several sweeps.
#
# Usage:  test/autovacuum.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
export PGC_EXTRA_CONF="pgcolumnar.autovacuum_naptime=2
pgcolumnar.autovacuum_compact_threshold=0.1
pgcolumnar.autovacuum_recluster_threshold=0.05"
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

CG=1000
deleted_rows() { q "SELECT COALESCE(sum(deletedrows),0) FROM pgcolumnar.stats('$1');"; }

# left_alone BEFORE AFTER -> "unchanged" when a table that had dead rows still has
# the same number, else what happened. Two missing readings agreed as unchanged,
# and so did a table with no dead rows to leave alone.
left_alone() {
	if ! pgc_is_number "$1" || ! pgc_is_number "$2"; then echo "no reading ([$1] -> [$2])"
	elif [ "$1" -le 0 ]; then echo "no reading (no dead rows to leave alone)"
	elif [ "$1" = "$2" ]; then echo unchanged
	else echo "changed: $1 -> $2"; fi
}
mk_deleted() {  # mk_deleted <table> -- 20k rows, 40% scattered-deleted (> threshold)
	psql_run "CREATE TABLE $1 (id int, v int, pad text) USING pgcolumnar;"
	psql_run "SELECT pgcolumnar.set_options('$1', chunk_group_row_limit => $CG);"
	psql_run "INSERT INTO $1 SELECT g, g%100, md5(g::text) FROM generate_series(1,20000) g;"
	psql_run "DELETE FROM $1 WHERE (id * 2654435761)::bigint % 100 < 40;"
	psql_run "SELECT pgcolumnar.compact('$1');"   # retire fully-dead (none: scattered)
}

# ---- premise: the launcher is registered and running ------------------------
# Not premises (#1339): the launcher and the setting's default are pgcolumnar's own,
# so a repository change reddens either -- with the launcher never registered, all
# of this suite goes red.
check "the maintenance launcher is running" \
	"$(q "SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'pgcolumnar autovacuum launcher';")" "1"
check "the daemon is OFF by default" "$(q "SHOW pgcolumnar.autovacuum;")" "off"

# ---- control: OFF -> a deleted-heavy table is left alone ---------------------
mk_deleted av_off
OFF_BEFORE="$(deleted_rows av_off)"
check "premise: av_off starts with dead rows above the threshold" \
	"$([ "${OFF_BEFORE:-0}" -gt 4000 ] && echo yes || echo no)" "yes"
sleep 8   # several naptimes; the daemon is off, so nothing should change
OFF_AFTER="$(deleted_rows av_off)"
check "OFF: the daemon did not touch the table (dead rows unchanged)" \
	"$(left_alone "$OFF_BEFORE" "$OFF_AFTER")" "unchanged"

# ---- enable: ON -> the deleted-heavy table gets compacted -------------------
mk_deleted av_on
ON_BEFORE="$(deleted_rows av_on)"
check "premise: av_on starts with dead rows above the threshold" \
	"$([ "${ON_BEFORE:-0}" -gt 4000 ] && echo yes || echo no)" "yes"

psql_run "ALTER SYSTEM SET pgcolumnar.autovacuum = on;"
q "SELECT pg_reload_conf();" >/dev/null
check "the daemon is now ON" "$(q "SHOW pgcolumnar.autovacuum;")" "on"

# poll up to ~30s for the daemon to compact av_on (dead rows -> gone)
ON_AFTER="$ON_BEFORE"
for _ in $(seq 1 15); do
	sleep 2
	ON_AFTER="$(deleted_rows av_on)"
	[ "${ON_AFTER:-1}" = "0" ] && break
done
check "ON: the daemon compacted the deleted-heavy table (dead rows now 0)" "$ON_AFTER" "0"
# and the live data is intact (compact_rewrite drops only dead rows)
check_num "ON: the surviving rows are unchanged" \
	"$(q "SELECT count(*) FROM av_on;")" \
	"$(q "SELECT count(*) FROM generate_series(1,20000) g WHERE NOT ((g * 2654435761)::bigint % 100 < 40);")"

# ---- the recluster verb: the daemon folds appended decay back into the run ---
# (re-enable for this arm)
psql_run "ALTER SYSTEM SET pgcolumnar.autovacuum = on;"
q "SELECT pg_reload_conf();" >/dev/null
psql_run "CREATE TABLE av_rc (ts int, v int) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('av_rc', chunk_group_row_limit => $CG);"
psql_run "INSERT INTO av_rc SELECT g, g%100 FROM generate_series(1,20000) g;"
psql_run "SELECT pgcolumnar.recluster('av_rc','ts');"   # establishes the run + records the key
psql_run "INSERT INTO av_rc SELECT g, g%100 FROM generate_series(1,5000) g;"  # 20% appended decay
check "premise: av_rc has a recorded key and appended decay" \
	"$(q "SELECT (sort_key IS NOT NULL AND appended_groups > 0) FROM pgcolumnar.sort_status('av_rc');")" "t"
RC_AFTER="$(q "SELECT appended_groups FROM pgcolumnar.sort_status('av_rc');")"
for _ in $(seq 1 15); do
	sleep 2
	RC_AFTER="$(q "SELECT appended_groups FROM pgcolumnar.sort_status('av_rc');")"
	[ "${RC_AFTER:-1}" = "0" ] && break
done
check "ON: the daemon reclustered the decayed table (appended groups folded to 0)" "$RC_AFTER" "0"
psql_run "ALTER SYSTEM SET pgcolumnar.autovacuum = off;"
q "SELECT pg_reload_conf();" >/dev/null
sleep 1

# ---- and OFF again stops it: a fresh deleted table is left alone -------------
psql_run "ALTER SYSTEM SET pgcolumnar.autovacuum = off;"
q "SELECT pg_reload_conf();" >/dev/null
sleep 1
mk_deleted av_off2
OFF2_BEFORE="$(deleted_rows av_off2)"
sleep 8
check "OFF-again: a new deleted table is left alone after disabling" \
	"$(left_alone "$OFF2_BEFORE" "$(deleted_rows av_off2)")" "unchanged"

pgc_summary
