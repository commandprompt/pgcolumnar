#!/usr/bin/env bash
#
# pgColumnar #2194: the maintenance daemon runs each verb in a transaction of its own.
#
# A verb keeps its locks to commit: the storage-row lock (class 102) and the locks of
# the row groups it rewrote (class 101). When both verbs were due, av_maintain_one ran
# compact_rewrite and then recluster in one transaction. The recluster then waited for
# a busy group while it still held the storage-row lock the compact_rewrite kept, and
# a writer that held that group's lock and waited for the storage-row lock closed the
# #2074 cycle: deadlock detected (40P01).
#
#   deadlock  The issue's steps, driven by the daemon. A writer deletes id 2501 and
#             flushes it (it holds that group's 101). The daemon's compact_rewrite
#             pauses on its first row (an expression index sleeps 3 s, once per
#             daemon process), holding 102. The writer then inserts, so it waits for
#             102. The writer's deadlock_timeout is 6 s and the daemon's 20 s, so if
#             the recluster waits on the writer's group while it holds 102, the
#             writer's own check finds the cycle. With a commit between the verbs the
#             writer gets 102 when the compact_rewrite commits, and commits. Both
#             verbs must still run in that one pass, one after the other.
#   limit     The same with pgcolumnar.rewrite_lock_timeout 1.5 s, under the
#             compact_rewrite's 3 s. A recluster charged for the compact_rewrite's
#             time, or one that waits on the writer while it holds the
#             compact_rewrite's lock, stops with 55P03 and the daemon logs a skip.
#   yield     A verb the daemon is cancelled out of ends the table's pass. A LOCK
#             TABLE with a 1 s deadlock_timeout cancels the paused compact_rewrite.
#             The daemon's next verb on the table must be the next pass's
#             compact_rewrite, not this pass's recluster.
#
# Usage:  test/autovacuum_verb_txn.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
export PGC_EXTRA_CONF="pgcolumnar.autovacuum_naptime=1
pgcolumnar.autovacuum_compact_threshold=0.05
pgcolumnar.autovacuum_recluster_threshold=0.05
deadlock_timeout=20s"
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

avp() { env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" "$@"; }

# the daemon worker's pg_stat_activity row (as a) while it sleeps in dl_pause
AV_PAUSED="a.backend_type = 'pgcolumnar autovacuum worker' AND a.wait_event = 'PgSleep'"

# av_wait_sql CONDITION: a DO block that waits (bounded, 60 s) until CONDITION holds; it
# clears the pg_stat_activity snapshot at each try
av_wait_sql() {
	printf 'DO $$ BEGIN FOR i IN 1..600 LOOP PERFORM pg_stat_clear_snapshot(); EXIT WHEN %s; PERFORM pg_sleep(0.1); END LOOP; END $$;\n' "$1"
}

# av_switch on|off: the daemon, by ALTER SYSTEM and a reload. Off also waits (bounded,
# 30 s) until no worker is left
av_switch() {
	q "ALTER SYSTEM SET pgcolumnar.autovacuum = $1;" >/dev/null
	q "SELECT pg_reload_conf();" >/dev/null
	[ "$1" = off ] || return 0
	for _ in $(seq 1 300); do
		[ "$(q "SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'pgcolumnar autovacuum worker';")" = 0 ] && return 0
		sleep 0.1
	done
}

# av_make: a fresh dl, due for both verbs. Three groups of 1000 rows are reclustered by
# id, which records the key; 300 rows appended after that make it due for recluster,
# and deletes of a third of the first group due for compact_rewrite. The expression
# index calls dl_pause, which sleeps 3 s on its first call in a daemon worker
av_make() {
	q "DROP TABLE IF EXISTS dl;
	   CREATE TABLE dl (id int, bal int) USING pgcolumnar;
	   SELECT pgcolumnar.set_options('dl', stripe_row_limit => 1000, chunk_group_row_limit => 100);
	   INSERT INTO dl SELECT g, 1000 FROM generate_series(1, 3000) g;
	   SELECT pgcolumnar.recluster('dl', 'id');
	   INSERT INTO dl SELECT g, 1000 FROM generate_series(3001, 3300) g;
	   DELETE FROM dl WHERE id <= 1000 AND id % 3 = 0;
	   CREATE OR REPLACE FUNCTION dl_pause(i int) RETURNS int LANGUAGE plpgsql IMMUTABLE AS \$\$
	   BEGIN
	       IF current_setting('pgc.paused', true) IS DISTINCT FROM 'on' AND EXISTS
	          (SELECT 1 FROM pg_stat_activity WHERE pid = pg_backend_pid()
	           AND backend_type = 'pgcolumnar autovacuum worker') THEN
	           PERFORM set_config('pgc.paused', 'on', false);
	           PERFORM pg_sleep(3);
	       END IF;
	       RETURN i;
	   END \$\$;
	   CREATE INDEX dl_pause_idx ON dl (dl_pause(id));" >/dev/null
}

# av_events FROM: the daemon's lines about public.dl in the server log past byte FROM,
# as one word each (compact_rewrite, recluster or skipped), space-separated
av_events() {
	tail -c +"$(( $1 + 1 ))" "$PGC_LOGFILE" \
		| grep -o 'pgcolumnar autovacuum: \(compact_rewrite\|recluster\|skipped\) public\.dl\b' \
		| awk '{print $3}' | paste -sd' ' -
}

# av_one_pass FROM: "one worker" when the first compact_rewrite and the first recluster the
# daemon logged about dl past FROM came from the same worker process (one pass), else what
# was seen
av_one_pass() {
	local c r
	c="$(tail -c +"$(( $1 + 1 ))" "$PGC_LOGFILE" | grep 'pgcolumnar autovacuum: compact_rewrite public\.dl\b' | head -1 | grep -o '\[[0-9]*\]')"
	r="$(tail -c +"$(( $1 + 1 ))" "$PGC_LOGFILE" | grep 'pgcolumnar autovacuum: recluster public\.dl\b' | head -1 | grep -o '\[[0-9]*\]')"
	if [ -n "$c" ] && [ "$c" = "$r" ]; then echo "one worker"; else echo "compact_rewrite ${c:-none}, recluster ${r:-none}"; fi
}

# av_wait_event FROM WORD: wait (bounded, 40 s) until the daemon logs WORD about dl past FROM
av_wait_event() {
	for _ in $(seq 1 400); do
		grep -qw "$2" <<<"$(av_events "$1")" && return 0
		sleep 0.1
	done
}

av_logsize() { wc -c <"$PGC_LOGFILE"; }

# av_log FROM: the daemon's lines and every error in the server log past byte FROM, on
# stderr, for a failing run
av_log() {
	tail -c +"$(( $1 + 1 ))" "$PGC_LOGFILE" | grep -E 'pgcolumnar autovacuum:|ERROR|DETAIL' >&2
}

check "the maintenance launcher is running" \
	"$(q "SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'pgcolumnar autovacuum launcher';")" "1"

# ---- a writer beside both verbs of one daemon pass -----------------------------------

# the writer's condition: the daemon sleeps in its compact_rewrite holding the storage-row lock
AV_PAUSED_102="EXISTS (SELECT 1 FROM pg_stat_activity a JOIN pg_locks l ON l.pid = a.pid
                WHERE $AV_PAUSED AND l.locktype = 'advisory' AND l.objsubid = 102 AND l.granted)"

# av_writer_pass LIMIT: a fresh dl; the daemon runs with pgcolumnar.rewrite_lock_timeout
# LIMIT beside a writer that deletes id 2501 and flushes it, waits until the daemon sleeps
# in its compact_rewrite holding the storage-row lock, then inserts and commits. The
# writer's output, then rc=<n>, is in av.w; AV_FROM is where the pass starts in the log
av_writer_pass() {
	av_make
	q "ALTER SYSTEM SET pgcolumnar.rewrite_lock_timeout = '$1';" >/dev/null
	rm -f "$PGC_WORKDIR/av.w"
	(
		avp -qAt -v ON_ERROR_STOP=1 >"$PGC_WORKDIR/av.w" 2>&1 <<SQL
SET deadlock_timeout = '6s';
SET statement_timeout = '60s';
BEGIN;
DELETE FROM dl WHERE id = 2501;
SELECT count(*) FROM dl WHERE id = 2501;
$(av_wait_sql "$AV_PAUSED_102")
SELECT 'paused=' || $AV_PAUSED_102;
INSERT INTO dl VALUES (0, 0);
SELECT count(*) FROM dl WHERE id = 0;
COMMIT;
SQL
		echo "rc=$?" >>"$PGC_WORKDIR/av.w"
	) &
	local w_pid=$!
	# the daemon starts only once the writer holds its group lock
	for _ in $(seq 1 100); do
		[ "$(q "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objsubid = 101 AND granted;")" -ge 1 ] && break
		sleep 0.1
	done
	AV_FROM=$(av_logsize)
	av_switch on
	wait "$w_pid"
	av_wait_event "$AV_FROM" recluster
	av_switch off
	q "ALTER SYSTEM RESET pgcolumnar.rewrite_lock_timeout;" >/dev/null
	q "SELECT pg_reload_conf();" >/dev/null
	av_log "$AV_FROM"
	grep -h 'ERROR' "$PGC_WORKDIR/av.w" >&2
	return 0
}

# deadlock: the default 10 s limit. A recluster that waits on the writer's group while it
# holds the compact_rewrite's storage-row lock closes the cycle before its limit, and the
# writer's own deadlock check, at 6 s, finds it.
av_make
check "dl is due for both compact_rewrite and recluster at the daemon's thresholds" \
	"$(q "SELECT compact_rewrite_due || ' ' || recluster_due FROM pgcolumnar.maintenance_due('dl', 0.05, 0.05);")" "true true"
av_writer_pass 10s
check "the writer saw the daemon paused in its compact_rewrite, holding the storage-row lock" \
	"$(grep -o '^paused=.*' "$PGC_WORKDIR/av.w" || echo paused=none)" "paused=true"
check "the writer that deleted and then inserted beside the daemon's two verbs commits, with no deadlock" \
	"$(tail -1 "$PGC_WORKDIR/av.w")" "rc=0"
check "and the writer's DELETE and INSERT took effect" \
	"$(q "SELECT count(*) FILTER (WHERE id = 2501) || ' ' || count(*) FILTER (WHERE id = 0) FROM dl;")" "0 1"
check "the daemon logged compact_rewrite and then recluster on dl in that pass" \
	"$(av_events "$AV_FROM" | cut -d' ' -f1-2)" "compact_rewrite recluster"
check "and the daemon's compact_rewrite and recluster on dl came from one worker, one pass" \
	"$(av_one_pass "$AV_FROM")" "one worker"
check "no session hit a deadlock beside the daemon's pass" \
	"$(tail -c +"$(( AV_FROM + 1 ))" "$PGC_LOGFILE" | grep -c 'deadlock detected')" "0"

# limit: 1.5 s, under the compact_rewrite's 3 s. The recluster must not be charged for the
# compact_rewrite's time, nor wait on the writer while it holds the compact_rewrite's
# storage-row lock; either way it would stop with 55P03, and the daemon would log a skip.
av_writer_pass 1500ms
check "beside a 1.5 s rewrite_lock_timeout, the writer saw the paused compact_rewrite" \
	"$(grep -o '^paused=.*' "$PGC_WORKDIR/av.w" || echo paused=none)" "paused=true"
check "and that writer commits" "$(tail -1 "$PGC_WORKDIR/av.w")" "rc=0"
check "the daemon's recluster, with a 1.5 s rewrite_lock_timeout, finished after a compact_rewrite of 3 s" \
	"$(av_events "$AV_FROM" | cut -d' ' -f1-2)" "compact_rewrite recluster"

# ---- yield: a cancelled verb ends the table's pass -----------------------------------

av_make
FROM=$(av_logsize)
av_switch on
out="$(avp -qAt -v VERBOSITY=verbose 2>&1 <<SQL
SET deadlock_timeout = '1s';
SET statement_timeout = '60s';
$(av_wait_sql "EXISTS (SELECT 1 FROM pg_stat_activity a WHERE $AV_PAUSED)")
SELECT 'paused=' || EXISTS (SELECT 1 FROM pg_stat_activity a WHERE $AV_PAUSED);
BEGIN;
LOCK TABLE dl IN ACCESS EXCLUSIVE MODE;
SELECT 'granted';
SELECT pg_sleep(3);
COMMIT;
SQL
)"
av_wait_event "$FROM" recluster
av_switch off
av_log "$FROM"
check "the locker saw the daemon paused in its compact_rewrite" \
	"$(grep -o '^paused=.*' <<<"$out" || echo paused=none)" "paused=true"
check "a LOCK TABLE with a 1 s deadlock_timeout was granted beside the paused compact_rewrite" \
	"$(grep -x granted <<<"$out" || echo "not granted: $(grep ERROR <<<"$out" | head -1)")" "granted"
check "after its cancelled compact_rewrite, the daemon's next verb on dl is the next pass's compact_rewrite, not a recluster" \
	"$(av_events "$FROM" | cut -d' ' -f1-3)" "skipped compact_rewrite recluster"

pgc_summary
