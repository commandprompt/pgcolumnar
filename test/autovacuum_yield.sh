#!/usr/bin/env bash
#
# pgColumnar #415: the maintenance daemon YIELDS like autovacuum. When a
# statement needs a lock that conflicts with the daemon's ShareUpdateExclusiveLock
# (an ALTER/DROP/TRUNCATE takes AccessExclusiveLock, the strongest case), core's
# lock manager -- seeing the holder flagged PROC_IS_AUTOVACUUM -- cancels the
# daemon's maintenance op after deadlock_timeout, so the op releases its lock and
# the user's statement proceeds. It is a bounded hiccup, not an indefinite block.
#
# Made deterministic by pgcolumnar.maintenance_hold_ms: the daemon's
# compact_rewrite holds SUEL for that long (interruptibly). The suite catches the
# daemon mid-hold, requests AccessExclusive, and asserts the DDL returns in far
# less than the hold (the yield fired) rather than waiting it out.
#
#   yield   AccessExclusive on a table the daemon holds SUEL on returns FAST
#           (< a fraction of the hold) and succeeds -> the daemon was cancelled.
#
# The driver's removal proof deletes PROC_IS_AUTOVACUUM: then the daemon is not
# cancellable, the lock waits the full hold, statement_timeout fires, this arm
# reds (57014 instead of success).
#
# Usage:  test/autovacuum_yield.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
export PGC_EXTRA_CONF="pgcolumnar.autovacuum=on
pgcolumnar.autovacuum_naptime=2
pgcolumnar.autovacuum_compact_threshold=0.1
pgcolumnar.maintenance_hold_ms=30000
deadlock_timeout=1s"
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

CG=1000

# Not premises (#1339): the launcher, and the worker holding its lock, are
# pgcolumnar's own, so a repository change reddens them.
check "the launcher is running" \
	"$(q "SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'pgcolumnar autovacuum launcher';")" "1"
check "premise: the daemon is on with a 30s maintenance hold" \
	"$(q "SELECT current_setting('pgcolumnar.autovacuum') || '/' || current_setting('pgcolumnar.maintenance_hold_ms');")" "on/30s"

# a deleted-heavy table the daemon will compact_rewrite (and hold SUEL on)
psql_run "CREATE TABLE yt (id int, v int) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('yt', chunk_group_row_limit => $CG);"
psql_run "INSERT INTO yt SELECT g, g%100 FROM generate_series(1,20000) g;"
psql_run "DELETE FROM yt WHERE (id * 2654435761)::bigint % 100 < 40;"
psql_run "SELECT pgcolumnar.compact('yt');"

# poll (up to ~20s) for the daemon worker to be holding SUEL on yt
holds() {
	q "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid
	   WHERE l.relation = 'yt'::regclass AND l.mode = 'ShareUpdateExclusiveLock'
	     AND l.granted AND a.backend_type = 'pgcolumnar autovacuum worker';"
}
held=no
for _ in $(seq 1 40); do
	[ "$(holds)" -ge 1 ] 2>/dev/null && { held=yes; break; }
	sleep 0.5
done
check "the daemon is holding SUEL on the table (the hold window is open)" "$held" "yes"

# now request AccessExclusive; time it. statement_timeout 15s < the 30s hold, so
# a daemon that did NOT yield would make this time out (57014); a yielding daemon
# releases within ~deadlock_timeout and this returns fast and clean.
t0=$(date +%s)
# The grant is READ, not assumed: STATE was "${STATE:-granted}" over the SQLSTATE
# alone, so a psql that never connected printed none and read as granted, and its
# instant failure as fast (measured: with psql pointed at a dead port, both yield
# checks passed). 'granted' is printed only after the COMMIT, and an error's
# SQLSTATE takes precedence over it.
OUT="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
	-d "$PGC_DB" -qtA 2>&1 <<SQLEOF
\\set VERBOSITY sqlstate
SET statement_timeout = '15s';
BEGIN; LOCK TABLE yt IN ACCESS EXCLUSIVE MODE; COMMIT;
SELECT 'granted';
SQLEOF
)"
STATE="$(sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\).*/\1/p' <<<"$OUT" | head -1)"
[ -n "$STATE" ] || STATE="$(grep -x granted <<<"$OUT" || echo "no reading: $(head -1 <<<"$OUT")")"
elapsed=$(( $(date +%s) - t0 ))

check "yield: the AccessExclusive lock was granted (no timeout -> daemon yielded)" \
	"$STATE" "granted"
check "yield: and it was granted FAST (<= 8s, far under the 30s hold)" \
	"$(if [ "$STATE" != granted ]; then echo "not granted ($STATE)"
	   elif [ "$elapsed" -le 8 ] 2>/dev/null; then echo fast; else echo "slow(${elapsed}s)"; fi)" "fast"

# the daemon logged the cancel of its own maintenance op
sleep 1
check "the daemon logged cancelling its maintenance op" \
	"$([ "$(grep -c 'pgcolumnar autovacuum: skipped .* canceling statement' "$PGC_LOGFILE" 2>/dev/null)" -ge 1 ] && echo yes || echo no)" "yes"

pgc_summary
