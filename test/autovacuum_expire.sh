#!/usr/bin/env bash
#
# pgColumnar #1290: the maintenance daemon runs pgcolumnar.expire for every
# table that declares a retention, when -- and only when -- both
# pgcolumnar.autovacuum and pgcolumnar.autovacuum_expire are on. The second
# switch is off by default (the owner's decision on #1290): expire deletes
# rows, so turning the daemon on must not start deleting them by itself.
#
# The arms that matter:
#   off      daemon on, expire off: an aged-out table keeps its rows, while a
#            deleted-heavy table IS compacted in the same window, so the daemon
#            was demonstrably running passes and chose not to expire.
#   on       turning expire on retires the aged-out rows within a few naptimes;
#            no row inside the retention is lost; a table with no retention,
#            holding the same old timestamps, keeps every row and is never
#            named in a "skipped" line.
#   isolate  an expire that errors (its TTL column was dropped) does not roll
#            back a compact_rewrite due on the same table in the same pass.
#   yield    the daemon holds ShareUpdateExclusiveLock inside expire (the only
#            verb due on that table) and gives it up to a session's
#            AccessExclusive request well inside the hold.
#
# Timing: naptime is 2s, so the polls below cover several passes.
#
# Usage:  test/autovacuum_expire.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
export PGC_EXTRA_CONF="pgcolumnar.autovacuum=on
pgcolumnar.autovacuum_naptime=2
pgcolumnar.autovacuum_compact_threshold=0.1
deadlock_timeout=1s"
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

CG=1000
OLD="now() - interval '30 days'"
aged() { q "SELECT count(*) FROM $1 WHERE ts < now() - interval '1 day';"; }
fresh() { q "SELECT count(*) FROM $1 WHERE ts >= now() - interval '1 day';"; }
deleted_rows() { q "SELECT COALESCE(sum(deletedrows),0) FROM pgcolumnar.stats('$1');"; }

# mk_ttl <table>: two row groups 30 days old and one fresh, retention 1 day.
mk_ttl() {
	psql_run "CREATE TABLE $1 (ts timestamptz, v int) USING pgcolumnar;"
	psql_run "SELECT pgcolumnar.set_options('$1', chunk_group_row_limit => $CG, stripe_row_limit => $CG,
	          ttl_column => 'ts', ttl_interval => '1 day');"
	psql_run "INSERT INTO $1 SELECT $OLD + g * interval '1 second', g FROM generate_series(1, 2*$CG) g;"
	psql_run "INSERT INTO $1 SELECT now() + g * interval '1 second', g FROM generate_series(1, $CG) g;"
}

# poll <seconds> <command> <want>: re-run the command until it prints want.
poll() {
	local i got
	for i in $(seq 1 "$1"); do
		got="$(eval "$2")"
		[ "$got" = "$3" ] && break
		sleep 1
	done
	printf '%s' "$got"
}

# Not premises (#1339): the launcher, its compaction pass and expire's refusal are
# pgcolumnar's own, so a repository change reddens each -- with the launcher never
# registered, or expire raising the wrong SQLSTATE. The zone-date premise below reads
# only the fixture's arithmetic, and stays one.
check "the maintenance launcher is running" \
	"$(q "SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'pgcolumnar autovacuum launcher';")" "1"
check "the daemon's expire switch exists and is OFF by default" \
	"$(q "SELECT current_setting('pgcolumnar.autovacuum') || '/' || current_setting('pgcolumnar.autovacuum_expire');")" "on/off"

# ---- the fixture is one an expire would change ------------------------------
mk_ttl ex_copy
check_num "a manual expire on the same fixture retires its two aged groups" \
	"$(q "SELECT pgcolumnar.expire('ex_copy');")" "2"

# ---- OFF: the daemon runs, compacts, and does not expire --------------------
mk_ttl ex_off
psql_run "CREATE TABLE ex_busy (id int, v int) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('ex_busy', chunk_group_row_limit => $CG);"
psql_run "INSERT INTO ex_busy SELECT g, g FROM generate_series(1, 20000) g;"
psql_run "DELETE FROM ex_busy WHERE (id * 2654435761)::bigint % 100 < 40;"
check "the daemon compacted a deleted-heavy table, so it was running passes" \
	"$(poll 40 'deleted_rows ex_busy' 0)" "0"
sleep 4   # two more passes over ex_off
check_num "OFF: with autovacuum_expire off the daemon retired nothing" "$(aged ex_off)" "$((2*CG))"

# ---- ON: aged rows go, fresh rows and the no-retention table stay -----------
psql_run "CREATE TABLE ex_none (ts timestamptz, v int) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('ex_none', chunk_group_row_limit => $CG, stripe_row_limit => $CG);"
psql_run "INSERT INTO ex_none SELECT $OLD + g * interval '1 second', g FROM generate_series(1, 2*$CG) g;"
psql_run "ALTER SYSTEM SET pgcolumnar.autovacuum_expire = on;"
psql_run "SELECT pg_reload_conf();"
check_num "ON: the daemon expired the aged-out rows without being asked" \
	"$(poll 40 'aged ex_off' 0)" "0"
check_num "ON: no row inside the retention was lost" "$(fresh ex_off)" "$CG"
check_num "ON: a table with no retention keeps every row" "$(aged ex_none)" "$((2*CG))"
check "ON: the daemon logged what it retired" \
	"$(grep -c 'pgcolumnar autovacuum: expire .*ex_off retired 2 row groups' "$PGC_LOGFILE" 2>/dev/null | awk '{print ($1 >= 1) ? "yes" : "no"}')" "yes"
check_num "ON: a table with no retention is never named in a skipped line" \
	"$(grep -c 'pgcolumnar autovacuum: skipped .*ex_none' "$PGC_LOGFILE" 2>/dev/null)" "0"
# Two more passes find nothing left to retire in ex_off. A line per table per
# pass, forever, is noise an administrator learns to ignore (OffgridwithJD on
# #1307), so a pass that retires nothing logs below LOG.
sleep 5
check_num "a pass that retires nothing logs nothing at LOG for that table" \
	"$(grep -c 'pgcolumnar autovacuum: expire .*ex_off retired 0 row groups' "$PGC_LOGFILE" 2>/dev/null)" "0"

# ---- isolate: a failing expire does not undo the compaction -----------------
# Built with expire off, so neither verb runs until both conditions are set.
psql_run "ALTER SYSTEM SET pgcolumnar.autovacuum_expire = off;"
psql_run "SELECT pg_reload_conf();"
sleep 3
psql_run "CREATE TABLE ex_bad (id int, ts timestamptz, v int) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('ex_bad', chunk_group_row_limit => $CG, ttl_column => 'ts', ttl_interval => '1 day');"
psql_run "INSERT INTO ex_bad SELECT g, now(), g FROM generate_series(1, 20000) g;"
psql_run "DELETE FROM ex_bad WHERE (id * 2654435761)::bigint % 100 < 40;"
psql_run "ALTER TABLE ex_bad DROP COLUMN ts;"
# q() discards stderr, so the error is read as its SQLSTATE from psql directly.
sqlstate_of() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-qtA -v VERBOSITY=sqlstate -c "$1" 2>&1 | sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\).*/\1/p' | head -1
}
check "expire on a table whose TTL column is gone fails with undefined_column" \
	"$(sqlstate_of "SELECT pgcolumnar.expire('ex_bad');")" "42703"
psql_run "ALTER SYSTEM SET pgcolumnar.autovacuum_expire = on;"
psql_run "SELECT pg_reload_conf();"
check "isolate: a failing expire does not roll back the compaction due on the same table" \
	"$(poll 40 'deleted_rows ex_bad' 0)" "0"

# ---- yield: the daemon gives up expire's lock to an AccessExclusive request --
# ex_hold has a retention and nothing else due -- no deletes, no sort key -- so
# a ShareUpdateExclusiveLock the daemon worker holds on it is expire's. The
# other retention tables go first: with the hold on, the worker spends 30s in
# expire on EACH of them, and reached ex_hold only after the poll gave up.
psql_run "DROP TABLE ex_copy, ex_off, ex_bad;"
psql_run "ALTER SYSTEM SET pgcolumnar.maintenance_hold_ms = 30000;"
psql_run "SELECT pg_reload_conf();"
# One transaction, so the worker that lists them sees all three, in creation
# order: ex_hold, then ex_gone, which is dropped while the worker holds
# ex_hold, then ex_after, which the same pass must still reach.
psql_run "BEGIN;
	CREATE TABLE ex_hold (ts timestamptz, v int) USING pgcolumnar;
	CREATE TABLE ex_gone (ts timestamptz, v int) USING pgcolumnar;
	CREATE TABLE ex_after (ts timestamptz, v int) USING pgcolumnar;
	SELECT pgcolumnar.set_options(t, chunk_group_row_limit => $CG, stripe_row_limit => $CG,
	       ttl_column => 'ts', ttl_interval => '1 day') FROM unnest(ARRAY['ex_hold','ex_gone','ex_after']::regclass[]) t;
	INSERT INTO ex_hold SELECT $OLD + g * interval '1 second', g FROM generate_series(1, 2*$CG) g;
	INSERT INTO ex_gone SELECT $OLD + g * interval '1 second', g FROM generate_series(1, 2*$CG) g;
	INSERT INTO ex_after SELECT $OLD + g * interval '1 second', g FROM generate_series(1, 2*$CG) g;
	COMMIT;"
holds() {
	q "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid
	   WHERE l.relation = 'ex_hold'::regclass AND l.mode = 'ShareUpdateExclusiveLock'
	     AND l.granted AND a.backend_type = 'pgcolumnar autovacuum worker';"
}
check "the daemon holds ShareUpdateExclusiveLock inside expire (the hold window is open)" \
	"$(poll 40 holds 1)" "1"
psql_run "DROP TABLE ex_gone;"
t0=$(date +%s)
STATE="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
	-d "$PGC_DB" -qtA 2>&1 <<SQLEOF | sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\).*/\1/p' | head -1
\\set VERBOSITY sqlstate
SET statement_timeout = '15s';
BEGIN; LOCK TABLE ex_hold IN ACCESS EXCLUSIVE MODE; COMMIT;
SQLEOF
)"
elapsed=$(( $(date +%s) - t0 ))
check "yield: the AccessExclusive lock on a table in expire was granted" "${STATE:-granted}" "granted"
check "yield: and it was granted fast (<= 8s, far under the 30s hold)" \
	"$([ "$elapsed" -le 8 ] 2>/dev/null && echo fast || echo "slow(${elapsed}s)")" "fast"
sleep 1
check "yield: the daemon logged cancelling expire" \
	"$([ "$(grep -c 'pgcolumnar autovacuum: skipped .*ex_hold.* canceling statement' "$PGC_LOGFILE" 2>/dev/null)" -ge 1 ] && echo yes || echo no)" "yes"
# After the cancel the same worker goes on down its list: ex_gone no longer
# exists, and ex_after is expired (after its own 30s hold).
check_num "dropped: and the pass goes on to expire the next table" "$(poll 45 'aged ex_after' 0)" "0"
check "dropped: a table dropped mid-pass is logged as skipped" \
	"$([ "$(grep -c 'pgcolumnar autovacuum: skipped expire on .*ex_gone' "$PGC_LOGFILE" 2>/dev/null)" -ge 1 ] && echo yes || echo no)" "yes"
psql_run "ALTER SYSTEM RESET pgcolumnar.maintenance_hold_ms;"
psql_run "SELECT pg_reload_conf();"

# ---- which TimeZone the daemon's expire cuts a date retention at (#1341) ------
#
# For a date or timestamp retention column the cutoff is a calendar date, so it
# depends on the zone. how-to.md says the daemon uses the server's TimeZone. Measured
# (PG17), it does, and it follows a change once reloaded, but a database-level
# ALTER DATABASE ... SET TimeZone overrides it, as it does for any session connecting
# to that database. Two zones 25 hours apart, so they are never on the same date.
#
# One row group per day, six days either side of today (UTC); the expire retires the
# groups wholly before the cutoff, so the first surviving day IS the cutoff the
# daemon computed: today in its zone, less the one-day retention.
tz_mk() {	# tz_mk TABLE: built with the daemon's expire off, so no pass sees half of it
	psql_run "CREATE TABLE $1 (d date, v int) USING pgcolumnar;"
	psql_run "SELECT pgcolumnar.set_options('$1', chunk_group_row_limit => 1000, stripe_row_limit => 1000,
	          ttl_column => 'd', ttl_interval => '1 day');"
	for k in $(seq -6 6); do
		psql_run "INSERT INTO $1 SELECT (now() AT TIME ZONE 'UTC')::date + $k, g FROM generate_series(1, 100) g;"
	done
}
tz_cut() { q "SELECT ((now() AT TIME ZONE '$1')::date - 1)::text;"; }
tz_expire() {	# tz_expire on|off  (silent: tz_first's output is its answer)
	psql_run "ALTER SYSTEM SET pgcolumnar.autovacuum_expire = $1;" >/dev/null
	psql_run "SELECT pg_reload_conf();" >/dev/null
	[ "$1" = off ] && sleep 5
}
# tz_first TABLE ZONE -> "match" if the first surviving day is the zone's cutoff, read
# just before and just after the daemon's passes (a zone's midnight may fall
# between); otherwise the day it holds and the two cutoffs it was compared with.
tz_first() {
	local before after got last="" same=0 i
	before="$(tz_cut "$2")"
	tz_expire on
	for i in $(seq 1 60); do
		got="$(q "SELECT min(d)::text FROM $1;")"
		if [ "$got" = "$last" ] && [ "$got" != "$tz_start" ]; then
			same=$((same + 1)); [ "$same" -ge 8 ] && break
		else
			same=0
		fi
		last="$got"; sleep 1
	done
	after="$(tz_cut "$2")"
	tz_expire off
	{ [ "$got" = "$before" ] || [ "$got" = "$after" ]; } && echo match || echo "$got (want $before or $after)"
}
psql_run "ALTER SYSTEM RESET pgcolumnar.maintenance_hold_ms;"
tz_expire off
psql_run "ALTER SYSTEM SET timezone = 'Pacific/Kiritimati';"
psql_run "SELECT pg_reload_conf();"
tz_mk tz_k
tz_start="$(q "SELECT min(d)::text FROM tz_k;")"
check "premise: the two zones are on different dates, so every arm below can tell them apart" \
	"$([ "$(tz_cut Pacific/Kiritimati)" != "$(tz_cut Pacific/Pago_Pago)" ] && echo apart || echo same)|$(q "SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = pgcolumnar.get_storage_id('tz_k');")" \
	"apart|13"
check "the daemon cuts a date retention at the server's TimeZone" "$(tz_first tz_k Pacific/Kiritimati)" "match"
psql_run "ALTER SYSTEM SET timezone = 'Pacific/Pago_Pago';"
psql_run "SELECT pg_reload_conf();"
sleep 3
tz_mk tz_p
check "and follows a change to it once the configuration is reloaded" "$(tz_first tz_p Pacific/Pago_Pago)" "match"
psql_run "ALTER DATABASE \"$PGC_DB\" SET timezone = 'Pacific/Kiritimati';"
tz_mk tz_d
check "a database-level TimeZone overrides the server's for the daemon too" "$(tz_first tz_d Pacific/Kiritimati)" "match"
psql_run "ALTER DATABASE \"$PGC_DB\" RESET timezone;"
psql_run "ALTER SYSTEM RESET timezone;"
psql_run "SELECT pg_reload_conf();"

pgc_summary
