#!/usr/bin/env bash
#
# pgColumnar: vacuum, vacuum_sorted and cluster check ownership BEFORE taking the
# AccessExclusiveLock (#568), so an unprivileged caller cannot queue in the lock
# FIFO and block readers of a table it does not own.
#
# A plain "a non-owner is refused" test is NOT a removal proof here: on unfixed
# main the non-owner IS refused, just after table_open has already taken (or
# queued for) the exclusive lock. The observable that separates the two orderings
# is contention. This suite holds an AccessExclusiveLock on the table in another
# session and then calls the function as a non-owner with a short lock_timeout:
#
#   fix (privilege first): refused with "must be owner" immediately, no lock wait.
#   main (lock first):     the caller queues behind the held lock and hits the
#                          lock_timeout -- proof it requested the exclusive lock
#                          before its ownership was ever checked.
#
# So the deny arms assert the refusal is an OWNERSHIP error and not a lock
# timeout. On main they see a lock timeout and go red; that red is the proof.
#
# Usage:  test/vacuum_lock_privilege.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

psql_run "CREATE TABLE victim (id int, v int) USING pgcolumnar;"
psql_run "INSERT INTO victim SELECT g, g FROM generate_series(1,2000) g;"
psql_run "REVOKE ALL ON victim FROM PUBLIC;"
psql_run "DROP ROLE IF EXISTS t_unpriv;"
psql_run "CREATE ROLE t_unpriv NOSUPERUSER LOGIN;"
psql_run "GRANT USAGE ON SCHEMA pgcolumnar TO t_unpriv;"

as_super() { env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq -v ON_ERROR_STOP=0 "$@" 2>&1; }

# Classify an unprivileged call made WHILE an exclusive lock is held on victim.
# Returns: owner (refused by ownership, before the lock) | locktimeout (queued
# for the lock, so ownership was checked too late) | other | success.
outcome_under_contention() {	# sql -> owner|locktimeout|other|success
	local out
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U t_unpriv \
		-d "$PGC_DB" -Atq -v ON_ERROR_STOP=0 \
		-c "SET lock_timeout='4s';" -c "$1" 2>&1)"
	case "$out" in
		*"must be owner"*|*"permission denied for"*) echo owner ;;
		*"lock timeout"*|*"canceling statement due to lock"*) echo locktimeout ;;
		*ERROR*) echo "other:${out:0:50}" ;;
		*) echo success ;;
	esac
}

# ---- premises ---------------------------------------------------------------
check_num "premise: the caller can open a session" \
	"$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U t_unpriv -d "$PGC_DB" -Atq -c 'SELECT 1;' 2>&1)" 1
check "premise: the caller is not the owner of victim" \
	"$(q "SELECT pg_get_userbyid(relowner)='t_unpriv' FROM pg_class WHERE oid='victim'::regclass;")" "f"
check "premise: victim is a columnar table" \
	"$(q "SELECT a.amname FROM pg_class c JOIN pg_am a ON a.oid=c.relam WHERE c.oid='victim'::regclass;")" "pgcolumnar"

# ---- hold an AccessExclusiveLock on victim in a background session ----------
# The holder keeps a transaction open with the lock until it is released below,
# by name, after the last probe. Its own lock conflicts with the exclusive lock
# vacuum/cluster take, so an unfixed caller queues behind it. Not a fixed sleep:
# under a mutant that queues every probe, eight probes at 4 s each outlast a
# 30 s window and the last ones would run uncontended (#1381 review).
holder_log="$PGC_WORKDIR/holder.log"
as_super -c "BEGIN;" -c "LOCK TABLE victim IN ACCESS EXCLUSIVE MODE;" -c "SELECT pg_sleep(3600) /* vlp_holder */;" -c "ROLLBACK;" > "$holder_log" 2>&1 &
holder_pid=$!
# Wait until the lock is actually granted before probing, so the test is not racing
for _ in $(seq 1 40); do
	held="$(q "SELECT count(*) FROM pg_locks l JOIN pg_class c ON c.oid=l.relation WHERE c.relname='victim' AND l.mode='AccessExclusiveLock' AND l.granted;")"
	[ "${held:-0}" -ge 1 ] && break
	sleep 0.25
done
check "premise: an AccessExclusiveLock on victim is held before the probes" "${held:-0}" "1"

# ---- the deny arms: refused by ownership, not by a lock wait ----------------
check "vacuum refuses a non-owner before taking the exclusive lock" \
	"$(outcome_under_contention "SELECT pgcolumnar.vacuum('victim'::regclass);")" "owner"
check "vacuum_sorted refuses a non-owner before taking the exclusive lock" \
	"$(outcome_under_contention "SELECT pgcolumnar.vacuum_sorted('victim'::regclass, 'id');")" "owner"
check "cluster refuses a non-owner before taking the exclusive lock" \
	"$(outcome_under_contention "SELECT pgcolumnar.cluster('victim'::regclass, 'id');")" "owner"

# ---- #749: the same ordering for the five maintenance/projection entries -----
#
# add_projection takes ShareLock and the other four take
# ShareUpdateExclusiveLock, all of which conflict with the AccessExclusiveLock
# held above, so an unfixed caller queues exactly as vacuum did. Asserting only
# the SQLSTATE would NOT be a removal proof: on unfixed main these calls are
# also eventually refused, just after the lock request. Contention is what
# separates the two orderings, which is the argument this file already makes.
check "add_projection refuses a non-owner before taking its lock (#749)" \
	"$(outcome_under_contention "SELECT pgcolumnar.add_projection('victim'::regclass, 'p1', ARRAY['id']);")" "owner"
check "drop_projection refuses a non-owner before taking its lock (#749)" \
	"$(outcome_under_contention "SELECT pgcolumnar.drop_projection('victim'::regclass, 'p1');")" "owner"
check "recluster refuses a non-owner before taking its lock (#749)" \
	"$(outcome_under_contention "SELECT pgcolumnar.recluster('victim'::regclass, 'id');")" "owner"
check "compact_rewrite refuses a non-owner before taking its lock (#749)" \
	"$(outcome_under_contention "SELECT pgcolumnar.compact_rewrite('victim'::regclass, 0.2);")" "owner"
check "compact refuses a non-owner before taking its lock (#749)" \
	"$(outcome_under_contention "SELECT pgcolumnar.compact('victim'::regclass);")" "owner"
check "truncate refuses a non-owner before taking its lock (#1384)" \
	"$(outcome_under_contention "SELECT pgcolumnar.truncate('victim'::regclass);")" "owner"

# A non-owner asking about a relation they do not own must learn only that they
# are not the owner. drop_projection checked the relation TYPE first, so it
# reported "is not a columnar table" for an arbitrary heap relation to a caller
# with no rights to it, while the four entries beside it did not (#749 review).
psql_run "CREATE TABLE heapvictim (id int);" >/dev/null 2>&1
psql_run "REVOKE ALL ON heapvictim FROM PUBLIC;" >/dev/null 2>&1
notowner_heap() {	# sql -> owner | typedisclosed | other
	local out
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U t_unpriv \
		-d "$PGC_DB" -Atq -v ON_ERROR_STOP=0 -c "$1" 2>&1)"
	case "$out" in
		*"must be owner"*|*"permission denied for"*) echo owner ;;
		*"is not a columnar table"*) echo typedisclosed ;;
		*) echo "other:${out:0:40}" ;;
	esac
}
check "drop_projection tells a non-owner nothing about the relation type (#749)" \
	"$(notowner_heap "SELECT pgcolumnar.drop_projection('heapvictim'::regclass, 'p1');")" "owner"
check "compact does the same, which is the behaviour drop_projection now matches" \
	"$(notowner_heap "SELECT pgcolumnar.compact('heapvictim'::regclass);")" "owner"
check "truncate tells a non-owner nothing about the relation type (#1384)" \
	"$(notowner_heap "SELECT pgcolumnar.truncate('heapvictim'::regclass);")" "owner"

# The caller must never enter the lock queue. WATCHED WHILE A REFUSED PROBE RUNS:
# asked after every probe had exited, this could not fail, because a timed-out
# request leaves the queue with its session (#1381 review). A vacuum probe runs in
# the background and pg_locks is polled until it ends: a caller refused before its
# lock is gone at once, one that queued is seen waiting for its whole lock_timeout.
probe_out="$PGC_WORKDIR/queue_probe.out"
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U t_unpriv -d "$PGC_DB" -Atq \
	-v ON_ERROR_STOP=0 -c "SET lock_timeout='4s';" -c "SELECT pgcolumnar.vacuum('victim'::regclass);" \
	> "$probe_out" 2>&1 &
probe_pid=$!
queued_max=0
while kill -0 "$probe_pid" 2>/dev/null; do
	n="$(q "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid WHERE l.relation='victim'::regclass AND a.usename='t_unpriv' AND NOT l.granted;")"
	case "$n" in ''|*[!0-9]*) queued_max="unreadable:$n"; break ;; esac
	[ "$n" -gt "$queued_max" ] && queued_max="$n"
	sleep 0.1
done
wait "$probe_pid" 2>/dev/null
check "the refused caller never queued a lock request on victim, watched while it ran" "$queued_max" "0"
check "premise: the lock is still held after the last probe" \
	"$(q "SELECT count(*) FROM pg_locks WHERE relation='victim'::regclass AND mode='AccessExclusiveLock' AND granted;")" "1"

# release the holder, by the tag on its statement
as_super -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE query LIKE '%vlp_holder%' AND pid <> pg_backend_pid();" >/dev/null 2>&1
wait "$holder_pid" 2>/dev/null || true

# ran_ok ROLE SQL -> "ok" only when psql ran SQL as ROLE and exited 0, "failed" otherwise.
# Positive evidence (#1381, #1400 review): counting ERROR lines, or reading an empty
# SQLSTATE as success, also passes a session that never connected.
ran_ok() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U "$1" -d "$PGC_DB" \
		-X -qtA -v ON_ERROR_STOP=1 -c "$2" >/dev/null 2>&1 && echo ok || echo failed
}
# ---- allow arm: the owner is not broken (uncontended) -----------------------
check "the owner can still vacuum the table" \
	"$(ran_ok postgres "SELECT pgcolumnar.vacuum('victim'::regclass);")" "ok"

# The positive controls for the #749 arms. Without these, a guard that refused
# every caller would satisfy every deny arm above.
check "the owner can still compact the table (#749 control)" \
	"$(ran_ok postgres "SELECT pgcolumnar.compact('victim'::regclass);")" "ok"
check "the owner can still add and drop a projection (#749 control)" \
	"$(ran_ok postgres "SELECT pgcolumnar.add_projection('victim'::regclass, 'p1', ARRAY['id']);
	                     SELECT pgcolumnar.drop_projection('victim'::regclass, 'p1');")" "ok"
# The #1384 control reads truncate's own answer, a block count, so a session that
# never ran the call cannot satisfy it.
check "the owner can still truncate the table, and is told how many blocks it returned (#1384 control)" \
	"$(tr_out="$(as_super -c "SELECT pgcolumnar.truncate('victim'::regclass);")"; [[ "$tr_out" =~ ^[0-9]+$ ]] && echo counted || echo none)" "counted"

pgc_summary
