#!/usr/bin/env bash
#
# pgColumnar concurrency regression test (tracking issue #4).
#
# Deletes are recorded by merging bits into a single shared pgcolumnar.delete_vector
# heap tuple per (storage id, stripe, chunk group). Before the fix, the delete
# path did an unguarded read-modify-write of that tuple: two transactions
# deleting different rows in the SAME chunk group could both read the old mask
# and then collide, so one transaction's delete bits were lost (a deleted row
# stayed visible) or the second deleter aborted with "tuple concurrently
# updated" / a duplicate-key error. The fix serializes the read-modify-write of
# a given chunk group with a transaction-scoped chunk-group lock and re-reads
# the committed mask before merging, so both sets of delete bits survive.
#
# This test forces the exact interleaving deterministically. It relies on the
# fact that a columnar DELETE flushes its delete marks to the catalog at the
# statement's executor-end, inside the still-open transaction. So:
#
#   session 1:  BEGIN; DELETE row A;   -- flush runs here, holds the chunk-group
#                                         write and its lock, uncommitted
#   session 2:  BEGIN; DELETE row B;   -- flush blocks: same chunk group
#   (barrier: wait until session 2 is blocked on a lock, via pg_stat_activity)
#   session 1:  COMMIT;                -- releases; session 2 unblocks
#   session 2:  COMMIT;
#
# After both commit, BOTH rows must be gone. Before the fix, session 2 either
# lost its bit or aborted, leaving its row visible; the final count then differs
# and the check fails. The barrier is a poll on pg_stat_activity for a real lock
# wait, not a fixed sleep, so the interleaving is forced, not raced.
#
# Two chunk-group states are exercised:
#   A. a delete_vector tuple already exists for the chunk group (update path)
#   B. no delete_vector tuple exists yet     (first-delete insert race)
#
# It also checks the intended concurrency is preserved: two deletes to
# DIFFERENT chunk groups do not block each other.
#
# Written fresh for pgColumnar; it reuses no upstream test file or expected
# output. Derived from the format/interface spec and the public PostgreSQL API.
#
# Usage:
#   test/concurrency.sh [PG_CONFIG]
#
# PG_CONFIG defaults to /usr/local/pg17/bin/pg_config. Run as a user that may
# "runuser -u postgres" (e.g. root) when the current user is not postgres.


# portlib.sh alone, not lib.sh: this suite carries its own harness, and the port
# band is needed before any of it runs. Sourcing portlib twice is harmless.
# lib.sh for the check vocabulary (#965). It sources portlib.sh itself
# (lib.sh:110), so this is a superset of what was here, and its top level is
# assignments and function definitions only, so sourcing it starts nothing.
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# A TIMEOUT IS A CHECK RESULT (#965). These paths printed `FAIL  timeout waiting
# for ...`, set the suite-local `fail` and returned -- touching neither PGC_CHECKS
# nor the record stream. A failing run then reconciled as `N passed + 0 failed =
# N`: an aggregate that BALANCES while asserting zero failures on a run that
# failed. That is worse than invisibility, because a missing number can be noticed
# and a balancing one cannot. Induced and measured rather than argued.
#
# THE NAME IS FIXED PER WAIT KIND, with the session and sentinel in pgc_record's
# REASON field. The ledger is keyed on (suite, part, name), so interpolating
# "$name/$label" into the name would mint rows nobody can enumerate and therefore
# nobody can seed.
#
# NO GREEN RUN EXECUTES THIS. It records only on the timeout path, so a passing
# suite emits nothing here and the record stream is unchanged.
wait_timeout() {	# wait_timeout FIXED-NAME DETAIL
	pgc_fail "$1" "timed out waiting for $2"
	fail=1
}


set -uo pipefail

PG_CONFIG="${1:-/usr/local/pg17/bin/pg_config}"
BINDIR="$("$PG_CONFIG" --bindir)"

# EVERY RECORD THIS SUITE EMITS CARRIED `major=unknown` (#965). `pgc_record` reads
# `${PGC_MAJOR:-unknown}`, and PGC_MAJOR is set by `pgc_setup` -- which this suite
# does not call, because it carries its own harness. So all of its rows named a
# major that is not a major, and a ledger keyed on (suite, part, name, majors)
# cannot seed them: the row would claim to hold on "unknown" and match no run.
# Measured before the fix, on a green run: 7 of 7 records said `unknown`.
PGC_MAJOR="$(pgc_major_of "$PG_CONFIG")"

SRCDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

WORKDIR="$(mktemp -d /tmp/pgcolumnar-conc.XXXXXX)"
PGDATA="$WORKDIR/data"
LOGFILE="$WORKDIR/server.log"

# Choose a unique, currently-free port per run so back-to-back runs (and a run
# after a hard-killed one that leaked a postmaster) never collide on a fixed
# port. PGC_PORT still overrides. TCP is disabled below regardless (the server
# listens only on a private unix socket in $WORKDIR), so the port is really just
# the socket-file name; probing a free one keeps it independent of any stray
# postmaster.
port_is_free() {  # port -> 0 if nothing is listening on it
	if command -v ss >/dev/null 2>&1; then
		# grep -c on a captured value, not a pipe into grep -q. A spurious
		# EPIPE here answers "nothing is listening" for a port that IS taken,
		# and the suite then starts a cluster on an occupied port.
		_pif="$(ss -Htln "sport = :$1" 2>/dev/null || true)"
		[ "$(grep -c ":$1" <<<"$_pif" || true)" -eq 0 ]
	else
		# fall back to a connect probe: a refused connection means free
		! (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
	fi
}
pick_port() {
	local p i
	for i in $(seq 1 100); do
		p=$(( PGC_PORT_LO + RANDOM % (PGC_PORT_HI - PGC_PORT_LO) ))
		if port_is_free "$p"; then
			echo "$p"
			return 0
		fi
	done
	# Inside the band portlib.sh carves below the ephemeral floor: inside the
	# ephemeral range, a probed-free port can still be taken by an outbound
	# connection before the cluster binds it.
	echo $(( PGC_PORT_LO + RANDOM % (PGC_PORT_HI - PGC_PORT_LO) ))   # give up probing
}
PORT="${PGC_PORT:-$(pick_port)}"

echo "== pgColumnar concurrency test (issue #4) =="
echo "PG_CONFIG=$PG_CONFIG"
echo "workdir=$WORKDIR"
echo "port=$PORT (private unix socket in workdir; TCP disabled)"

# The matrix runner installs the extension once per version and sets
# PGC_SKIP_BUILD; skip the redundant per-suite build+install then, which also
# avoids racing a concurrent suite's install into the same lib dir.
if [ -z "${PGC_SKIP_BUILD:-}" ]; then
	# THE HARNESS BUILDER, NOT A HAND-ROLLED make (#1220). Three guarantees come
	# with it and none of them were here: it asks the OBJECTS which major built
	# them (#1219), it keeps the build stamp (#536), and it refuses to run when
	# the build or the install failed instead of reporting checks against the
	# previously installed .so -- which the two discarded exit statuses below it
	# used to do silently.
	pgc_build_and_install "$SRCDIR" "$PG_CONFIG" "$PGC_MAJOR" || exit 1
fi

if [ "$(id -u)" = "0" ]; then
	RUNPG=(runuser -u postgres --)
	chown -R postgres "$WORKDIR"
else
	RUNPG=(env)
fi

run_pg() { "${RUNPG[@]}" env PATH="$BINDIR:$PATH" bash -lc "$1"; }

SESS_PIDS=()
cleanup() {
	for p in "${SESS_PIDS[@]:-}"; do
		kill "$p" >/dev/null 2>&1 || true
	done
	# Stop THIS run's cluster (never anyone else's).
	run_pg "pg_ctl -D '$PGDATA' stop -m immediate -w" >/dev/null 2>&1 || true
	# Backstop: if the postmaster is still up, kill only the pid recorded in
	# this data dir's postmaster.pid, so no unrelated cluster is touched.
	if [ -f "$PGDATA/postmaster.pid" ]; then
		pmpid="$(head -n1 "$PGDATA/postmaster.pid" 2>/dev/null)"
		if [ -n "${pmpid:-}" ] && [ "$pmpid" -gt 1 ] 2>/dev/null; then
			kill -9 "$pmpid" >/dev/null 2>&1 || true
		fi
	fi
	# Final reap: any process still tied to THIS run's unique workdir (a lingering
	# session psql that has not finished processing \q, or the postmaster). The
	# path is unique per run (mktemp) and does not appear in this script's own
	# command line, so this never touches another run or the driver.
	if command -v pkill >/dev/null 2>&1; then
		pkill -9 -f "$WORKDIR" >/dev/null 2>&1 || true
	fi
	rm -rf "$WORKDIR"
}
trap cleanup EXIT

echo "-- initdb"
run_pg "initdb -D '$PGDATA' -A trust" >/dev/null 2>&1
run_pg "echo \"port=$PORT\" >> '$PGDATA/postgresql.conf'"
run_pg "echo \"shared_preload_libraries='pgcolumnar'\" >> '$PGDATA/postgresql.conf'"
# Listen only on a private unix socket in the run's workdir, no TCP. This makes
# two runs fully independent: no shared /tmp/.s.PGSQL.PORT socket file and no TCP
# port to bind, so a leaked postmaster from an earlier run cannot poison this one.
run_pg "echo \"listen_addresses=''\" >> '$PGDATA/postgresql.conf'"
run_pg "echo \"unix_socket_directories='$WORKDIR'\" >> '$PGDATA/postgresql.conf'"
# Do not let a real hang masquerade as a pass: cap any lock wait.
run_pg "echo \"lock_timeout=60000\" >> '$PGDATA/postgresql.conf'"
echo "-- start"
run_pg "pg_ctl -D '$PGDATA' -l '$LOGFILE' start -w" >/dev/null
run_pg "createdb -h '$WORKDIR' -p $PORT conc"

# All connections go through the private unix socket (-h "$WORKDIR").
# Controller connection: stop on error, so setup problems surface immediately.
PSQL="psql -h '$WORKDIR' -p $PORT -d conc -qAtX -v ON_ERROR_STOP=1"
# Session connections: NO ON_ERROR_STOP, so a pre-fix concurrency error prints
# and the session keeps running (the final data check is what asserts), instead
# of the session dying and the test hanging on timeouts.
SPSQL="psql -h '$WORKDIR' -p $PORT -d conc -qAtX"
ctl_q() { run_pg "$PSQL -c \"$1\""; }

fail=0
# RECORDS RATHER THAN ONLY PRINTING (#965). This suite emitted human PASS lines
# and no RESULT records, so every mechanism built on the record vocabulary -- the
# ledger, the census, checks_never_observed_red, the red-observation record,
# duplicate-name detection -- was blind to all of them. The ledger did not merely
# return nothing on this log, it REFUSED it: "no RESULT records, so there is
# nothing to reconcile".
#
# `pgc_record` takes the DISPLAY whole, so the human output below is byte-for-byte
# what it was. NOT lib.sh's own `check`: that composes its own display and would
# drop the `: $got` suffix, which is the measured value rather than a label.
#
# `fail` is still set, so this suite's exit logic is untouched. Its verdict line
# stays for the same reason: under `set -euo pipefail` a failing command aborts the
# suite, and the verdict is what distinguishes finished from stopped.
#
# The `checks run:` line is READ BY NOTHING YET. The matrix gates reconciliation on
# the ACCOUNTING line via `pgc_log_shows_accounting`, and this suite emits none
# because it emits no accounting line. The line is still correct and wanted --
# it is the total the records reconcile against -- so the remaining step is a gate
# flip rather than new work (@jdatcmd, #969 review).
#
# Through lib.sh's pgc_pass and pgc_fail, not pgc_record directly: the grader's
# extractor reads the recorders lib.sh defines, and a twinned suite calling the
# seed itself is what test_compare_to_bash.py refuses (#1341). A PASS line no
# longer carries ": $got"; a FAIL still names both sides.
check() {
	local name="$1" got="$2" want="$3"
	if [ "$got" = "$want" ]; then
		pgc_pass "$name"
	else
		pgc_fail "$name" "got [$got] want [$want]"
		fail=1
	fi
}

# --- persistent interactive sessions, driven over FIFOs --------------------
#
# Each session is a psql reading from a FIFO. We keep the write end open on a
# dedicated fd so the session stays alive between commands, append a sentinel
# after each command, and poll the session's output file for it. A blocked
# command never prints its sentinel; we detect the block through
# pg_stat_activity instead.

start_session() {  # name
	local name="$1"
	local infile="$WORKDIR/$name.in"
	local outfile="$WORKDIR/$name.out"
	run_pg "mkfifo '$infile'; touch '$outfile'"
	run_pg "$SPSQL >'$outfile' 2>&1 <'$infile'" &
	SESS_PIDS+=("$!")
	# Hold the FIFO open read-write so the open never blocks and the session
	# only sees EOF when we send \q.
	exec {fd}<>"$infile"
	eval "FD_$name=$fd"
}

send() {  # name sql...
	local name="$1"; shift
	local fd
	eval "fd=\$FD_$name"
	printf '%s\n' "$*" >&"$fd"
}

# send a command and wait (bounded) for its sentinel to appear in the output
send_wait() {  # name label sql...
	local name="$1" label="$2"; shift 2
	local fd outfile="$WORKDIR/$name.out" i=0
	eval "fd=\$FD_$name"
	printf '%s\n' "$*" >&"$fd"
	printf '\\echo <<%s>>\n' "$label" >&"$fd"
	while ! grep -q "<<$label>>" "$outfile" 2>/dev/null; do
		sleep 0.05; i=$((i + 1))
		if [ "$i" -ge 1200 ]; then
			wait_timeout "a bounded wait for a command's sentinel completed" "$name/$label"
			return 1
		fi
	done
	return 0
}

# wait (bounded) for a sentinel already queued behind a blocked command
wait_sentinel() {  # name label
	local name="$1" label="$2" outfile i=0
	outfile="$WORKDIR/$name.out"
	while ! grep -q "<<$label>>" "$outfile" 2>/dev/null; do
		sleep 0.05; i=$((i + 1))
		if [ "$i" -ge 1200 ]; then
			wait_timeout "a bounded wait for a standalone sentinel completed" "$name/$label"
			return 1
		fi
	done
	return 0
}

# poll until the named backend is blocked waiting on a heavyweight lock
wait_blocked() {  # application_name
	local app="$1" i=0 n
	while :; do
		n="$(ctl_q "SELECT count(*) FROM pg_stat_activity WHERE application_name='$app' AND wait_event_type='Lock';")"
		[ "$n" = "1" ] && return 0
		sleep 0.05; i=$((i + 1))
		if [ "$i" -ge 1200 ]; then
			wait_timeout "a bounded wait for a session to block completed" "$app"
			return 1
		fi
	done
}

# poll until the named backend is idle in an open transaction (command done)
wait_idle_intx() {  # application_name
	local app="$1" i=0 st
	while :; do
		st="$(ctl_q "SELECT state FROM pg_stat_activity WHERE application_name='$app';")"
		[ "$st" = "idle in transaction" ] && return 0
		sleep 0.05; i=$((i + 1))
		if [ "$i" -ge 1200 ]; then
			wait_timeout "a bounded wait for a session to reach idle-in-transaction completed" "$app"
			return 1
		fi
	done
}

ctl_q "CREATE EXTENSION pgcolumnar;" >/dev/null

# ---------------------------------------------------------------------------
# Scenario A: delete_vector tuple already exists for the chunk group.
# All rows land in one stripe / one chunk group (default 10000 rows/group).
# An initial committed delete creates the delete_vector tuple; then two concurrent
# deletes of different rows in that same group must both survive.
# ---------------------------------------------------------------------------
ctl_q "CREATE TABLE t (id int) USING pgcolumnar;" >/dev/null
ctl_q "INSERT INTO t SELECT g FROM generate_series(1,6) g;" >/dev/null
ctl_q "DELETE FROM t WHERE id = 6;" >/dev/null   # creates the delete_vector tuple

start_session s1
start_session s2
send s1 "SET application_name='cc_s1';"
send s2 "SET application_name='cc_s2';"

send_wait s1 a_begin "BEGIN;"
# S1 deletes id=1; its executor-end flush merges the bit and holds the lock.
send_wait s1 a_del "DELETE FROM t WHERE id = 1;"

send s2 "BEGIN;"
# S2 deletes id=2 in the same chunk group; the flush blocks behind S1.
send s2 "DELETE FROM t WHERE id = 2;"
send s2 "\\echo <<a_del2>>"
wait_blocked cc_s2 || true
# On the CHUNK-GROUP lock, an advisory lock, and not merely on some lock: with
# the chunk-group lock removed the second deleter still waits, on the
# delete_vector tuple's transaction, and a count of any Lock wait passed it
# (measured) while both A arms below went red.
check "A same-group second deleter blocks" \
	"$(ctl_q "SELECT count(*) FROM pg_stat_activity WHERE application_name='cc_s2' AND wait_event_type='Lock' AND wait_event='advisory';")" \
	"1"

# Release S1; S2 must now merge (not overwrite) and commit cleanly.
send_wait s1 a_commit "COMMIT;"
wait_sentinel s2 a_del2         # S2's DELETE (a_del2) unblocked and completed
send_wait s2 a_commit "COMMIT;"

check "A both concurrent deletes survived (ids 1,2,6 gone)" \
	"$(ctl_q "SELECT string_agg(id::text, ',' ORDER BY id) FROM t;")" \
	"3,4,5"
check "A row count after concurrent deletes" \
	"$(ctl_q "SELECT count(*) FROM t;")" "3"

# ---------------------------------------------------------------------------
# Scenario B: no delete_vector tuple exists yet (first-delete insert race).
# Two concurrent first deletes of different rows in one chunk group race to
# create the initial delete_vector row; both bits must survive.
# ---------------------------------------------------------------------------
ctl_q "CREATE TABLE t2 (id int) USING pgcolumnar;" >/dev/null
ctl_q "INSERT INTO t2 SELECT g FROM generate_series(1,6) g;" >/dev/null

send_wait s1 b_begin "BEGIN;"
send_wait s1 b_del "DELETE FROM t2 WHERE id = 1;"

send s2 "BEGIN;"
send s2 "DELETE FROM t2 WHERE id = 2;"
send s2 "\\echo <<b_del2>>"
wait_blocked cc_s2 || true
check "B first-delete second deleter blocks" \
	"$(ctl_q "SELECT count(*) FROM pg_stat_activity WHERE application_name='cc_s2' AND wait_event_type='Lock' AND wait_event='advisory';")" \
	"1"

send_wait s1 b_commit "COMMIT;"
wait_sentinel s2 b_del2
send_wait s2 b_commit "COMMIT;"

check "B both first deletes survived (ids 1,2 gone)" \
	"$(ctl_q "SELECT string_agg(id::text, ',' ORDER BY id) FROM t2;")" \
	"3,4,5,6"

# ---------------------------------------------------------------------------
# Scenario C: deletes to DIFFERENT chunk groups must not serialize.
# Two stripes -> two chunk groups (each INSERT flushes its own stripe). A
# delete in each group, with the first transaction held open, must not block
# the second.
# ---------------------------------------------------------------------------
ctl_q "CREATE TABLE t3 (id int) USING pgcolumnar;" >/dev/null
ctl_q "INSERT INTO t3 SELECT g FROM generate_series(1,4) g;" >/dev/null   # stripe 1
ctl_q "INSERT INTO t3 SELECT g FROM generate_series(5,8) g;" >/dev/null   # stripe 2

send_wait s1 c_begin "BEGIN;"
send_wait s1 c_del "DELETE FROM t3 WHERE id = 1;"    # chunk group in stripe 1

# S2 deletes a row in the other stripe/chunk group; it must NOT block.
send_wait s2 c_begin "BEGIN;"
send_wait s2 c_del "DELETE FROM t3 WHERE id = 5;"    # chunk group in stripe 2
wait_idle_intx cc_s2
# Not blocked, AND its DELETE finished in a live transaction. A deleter that did
# block hits lock_timeout, errors, and then waits on nothing: with deletes to
# different groups made to serialize, "0 lock waits" passed on the aborted
# transaction (measured).
check "C different-group deleter does not block" \
	"$(ctl_q "SELECT coalesce(state, '<none>') || '/' || (wait_event_type IS NOT DISTINCT FROM 'Lock') FROM pg_stat_activity WHERE application_name='cc_s2';")" \
	"idle in transaction/false"

send_wait s1 c_commit "COMMIT;"
send_wait s2 c_commit "COMMIT;"
check "C both different-group deletes survived (ids 1,5 gone)" \
	"$(ctl_q "SELECT string_agg(id::text, ',' ORDER BY id) FROM t3;")" \
	"2,3,4,6,7,8"

# ---------------------------------------------------------------------------
# Scenarios D, E and F (#2184): a writer's two lock classes form no cycle.
#
# A writer holds the chunk-group lock (class 101) of each group it deletes from,
# and the storage-row lock (class 102), which every flush of new rows takes, both
# to commit, in whatever order its statements run. When 102 was exclusive, a
# transaction that inserted and then deleted in group g, and one that deleted in
# g and then inserted, each waited for the other, and one got 40P01. Now a writer
# whose storage row is current holds 102 in ShareLock, which other writers share,
# and only a writer that inserts or raises the storage row holds it exclusively.
# A delete raises an earlier build's stamp before it takes a group lock (E), so
# no transaction waits on 102 while it holds a 101 of the same table.
# ---------------------------------------------------------------------------
# lks APP CLASS -> that session's advisory locks of CLASS, "(waiting)" if not granted, or <none>
lks() {
	ctl_q "SELECT coalesce(string_agg(l.mode || CASE WHEN l.granted THEN '' ELSE '(waiting)' END, ',' ORDER BY l.mode || CASE WHEN l.granted THEN '' ELSE '(waiting)' END), '<none>') FROM pg_locks l JOIN pg_stat_activity a USING (pid) WHERE a.application_name = '$1' AND l.locktype = 'advisory' AND l.objsubid = $2;"
}
# poll (bounded) until the session's queued sentinel appeared or it waits on a lock
wait_settled() {  # name application_name label
	local name="$1" app="$2" label="$3" i=0
	while ! grep -q "<<$label>>" "$WORKDIR/$name.out" 2>/dev/null; do
		[ "$(ctl_q "SELECT count(*) FROM pg_stat_activity WHERE application_name='$app' AND wait_event_type='Lock';")" = "1" ] && return 0
		sleep 0.05; i=$((i + 1))
		if [ "$i" -ge 1200 ]; then
			wait_timeout "a bounded wait for a session to finish or block completed" "$app"
			return 1
		fi
	done
	return 0
}
# ERROR lines in both session outputs so far; both files only grow, so the
# difference from a mark taken at a scenario's start is that scenario's errors
err_mark() {
	local all
	all="$(printf '%s\n%s\n' "$(cat "$WORKDIR/s1.out")" "$(cat "$WORKDIR/s2.out")")"
	grep -c 'ERROR' <<<"$all" || true
}
errs_since() { echo $(( $(err_mark) - $1 )); }

# D: the steps of #2184, on a table whose storage row is current.
ctl_q "CREATE TABLE t4 (id int) USING pgcolumnar;" >/dev/null
ctl_q "INSERT INTO t4 SELECT g FROM generate_series(1,8) g;" >/dev/null
mark="$(err_mark)"
send_wait s1 d_begin "BEGIN;"
send_wait s1 d_ins "INSERT INTO t4 VALUES (0); SELECT count(*) FROM t4;"
check "D an INSERT and a scan leave s1 holding the storage-row lock in ShareLock only" \
	"$(lks cc_s1 102)" "ShareLock"
send_wait s2 d_begin "BEGIN;"
send_wait s2 d_del "DELETE FROM t4 WHERE id = 6; SELECT count(*) FROM t4;"
check "premise: D s2's DELETE holds the group lock of the row it deleted" \
	"$(lks cc_s2 101)" "ExclusiveLock"
check "D that DELETE, on a table whose storage row is current, takes no storage-row lock" \
	"$(lks cc_s2 102)" "<none>"
send s1 "DELETE FROM t4 WHERE id = 5; SELECT count(*) FROM t4;"
send s1 "\\echo <<d_del1>>"
wait_blocked cc_s1 || true
check "premise: D s1's DELETE in that group waits on s2's group lock" \
	"$(lks cc_s1 101)" "ExclusiveLock(waiting)"
# Exclusive, s2's INSERT waited on s1, and the deadlock detector ended s1.
send_wait s2 d_ins "INSERT INTO t4 VALUES (-1); SELECT count(*) FROM t4;"
check "D s2's INSERT after its DELETE returns while s1 still waits on s2's group lock" \
	"$(lks cc_s1 101)" "ExclusiveLock(waiting)"
send_wait s2 d_commit "COMMIT;"
wait_sentinel s1 d_del1
send_wait s1 d_commit "COMMIT;"
check "D the inserting-then-deleting and the deleting-then-inserting transactions both commit" \
	"$(errs_since "$mark")" "0"
check "D both DELETEs and both INSERTs took effect" \
	"$(ctl_q "SELECT count(*) FILTER (WHERE id IN (5,6)) || ' ' || count(*) FILTER (WHERE id IN (0,-1)) FROM t4;")" \
	"0 2"

# E: the same two shapes on a table an earlier build stamped. The INSERT raises
# the stamp and holds 102 exclusively; the DELETE must wait for it before it
# takes its group lock, or the two close the same cycle once.
ctl_q "CREATE TABLE t5 (id int) USING pgcolumnar;" >/dev/null
ctl_q "INSERT INTO t5 SELECT g FROM generate_series(1,8) g;" >/dev/null
ctl_q "UPDATE pgcolumnar.storage SET format_version = 1 WHERE relation_oid = 't5'::regclass;" >/dev/null
check "premise: E t5's storage row carries an earlier build's stamp" \
	"$(ctl_q "SELECT format_version FROM pgcolumnar.storage WHERE relation_oid = 't5'::regclass;")" "1"
mark="$(err_mark)"
send_wait s1 e_begin "BEGIN;"
send_wait s1 e_ins "INSERT INTO t5 VALUES (0); SELECT count(*) FROM t5;"
check "E s1's INSERT raised the stamp and holds the storage-row lock in ExclusiveLock" \
	"$(lks cc_s1 102)" "ExclusiveLock"
send_wait s2 e_begin "BEGIN;"
send s2 "DELETE FROM t5 WHERE id = 6; SELECT count(*) FROM t5;"
send s2 "\\echo <<e_del2>>"
wait_settled s2 cc_s2 e_del2
check "E a DELETE first in its transaction waits on the raiser's storage-row lock before it takes a group lock" \
	"$(lks cc_s2 102)/$(lks cc_s2 101)" "ExclusiveLock(waiting)/<none>"
send s1 "DELETE FROM t5 WHERE id = 5; SELECT count(*) FROM t5;"
send s1 "\\echo <<e_del1>>"
wait_settled s1 cc_s1 e_del1
check "E the raiser's DELETE in the waiting deleter's group returns at once" \
	"$(grep -c '<<e_del1>>' "$WORKDIR/s1.out")/$(lks cc_s1 101)" "1/ExclusiveLock"
send s1 "COMMIT;"
send s1 "\\echo <<e_c1>>"
wait_sentinel s2 e_del2
check "E once the raiser commits, the deleter holds its group lock and no storage-row lock" \
	"$(lks cc_s2 102)/$(lks cc_s2 101)" "<none>/ExclusiveLock"
send s2 "INSERT INTO t5 VALUES (-1); SELECT count(*) FROM t5;"
send s2 "COMMIT;"
send s2 "\\echo <<e_c2>>"
wait_sentinel s1 e_c1
wait_sentinel s2 e_c2
check "E the raising and the deleting transactions both commit" \
	"$(errs_since "$mark")" "0"
check "E both DELETEs and both INSERTs took effect, and the stamp is raised" \
	"$(ctl_q "SELECT count(*) FILTER (WHERE id IN (5,6)) || ' ' || count(*) FILTER (WHERE id IN (0,-1)) FROM t5;") $(ctl_q "SELECT format_version FROM pgcolumnar.storage WHERE relation_oid = 't5'::regclass;")" \
	"0 2 2"

# F: two first writers of a table with no storage row. The second waits for the
# first to insert the row; once it has, the second keeps only ShareLock, so a
# third writer does not wait for it.
ctl_q "CREATE TABLE t6 (id int) USING pgcolumnar;" >/dev/null
check "premise: F t6 has no storage row before its first write" \
	"$(ctl_q "SELECT count(*) FROM pgcolumnar.storage WHERE relation_oid = 't6'::regclass;")" "0"
mark="$(err_mark)"
send_wait s1 f_begin "BEGIN;"
send_wait s1 f_ins "INSERT INTO t6 VALUES (1); SELECT count(*) FROM t6;"
check "F the first writer holds the storage-row lock in ExclusiveLock" \
	"$(lks cc_s1 102)" "ExclusiveLock"
send_wait s2 f_begin "BEGIN;"
send s2 "INSERT INTO t6 VALUES (2); SELECT count(*) FROM t6;"
send s2 "\\echo <<f_ins2>>"
wait_blocked cc_s2 || true
check "F a second first writer waits on the storage-row lock" \
	"$(lks cc_s2 102)" "ExclusiveLock(waiting)"
send_wait s1 f_commit "COMMIT;"
wait_sentinel s2 f_ins2
check "F once the first writer commits, the second holds the storage-row lock in ShareLock only" \
	"$(lks cc_s2 102)" "ShareLock"
send_wait s1 f_ins3 "BEGIN; SET LOCAL lock_timeout = '2s'; INSERT INTO t6 VALUES (3); SELECT count(*) FROM t6; COMMIT;"
check "F a third writer's INSERT, with a 2 s lock_timeout, commits while the second is open" \
	"$(ctl_q "SELECT count(*) FROM t6 WHERE id = 3;")/$(errs_since "$mark")" "1/0"
send_wait s2 f_commit "COMMIT;"
check "F all three INSERTs took effect" \
	"$(ctl_q "SELECT string_agg(id::text, ',' ORDER BY id) FROM t6;")" "1,2,3"

# G: writers of unrelated rows that share a row-identity bucket (class 104). Once
# writers stopped waiting for each other on 102, two transactions that each ran a
# DELETE and then an UPDATE of their own rows deadlocked in concurrent_diff: each
# flush took its rows' buckets, held to commit, and the second flush of each
# waited for a bucket the other's first flush held. A flush now takes a bucket
# only if it is free; the chunk-group locks it already holds are what make a
# writer of the same rows finish first. s1 deletes 39999 rows, so it holds every
# bucket, and s2 then deletes rows of another row group.
ctl_q "CREATE TABLE t7 (id int) USING pgcolumnar;" >/dev/null
ctl_q "INSERT INTO t7 SELECT g FROM generate_series(1,40000) g;" >/dev/null
ctl_q "INSERT INTO t7 SELECT g FROM generate_series(40001,40004) g;" >/dev/null
# buckets APP -> that session's granted row-identity locks / row_lock_buckets
buckets() {
	ctl_q "SELECT count(*) || '/' || current_setting('pgcolumnar.row_lock_buckets') FROM pg_locks l JOIN pg_stat_activity a USING (pid) WHERE a.application_name = '$1' AND l.locktype = 'advisory' AND l.objsubid = 104 AND l.granted;"
}
mark="$(err_mark)"
send_wait s1 g_begin "BEGIN;"
send_wait s1 g_del "DELETE FROM t7 WHERE id BETWEEN 2 AND 40000; SELECT count(*) FROM t7;"
check "G s1's DELETE of 39999 rows takes every row-identity bucket, all free, and holds them" \
	"$(buckets cc_s1)" "1024/1024"
send_wait s2 g_del "BEGIN; SET LOCAL lock_timeout = '2s'; DELETE FROM t7 WHERE id = 40001; SELECT count(*) FROM t7;"
check "G s2's DELETE in another row group, with a 2 s lock_timeout, returns holding its group lock and no bucket" \
	"$(errs_since "$mark")/$(lks cc_s2 101)/$(lks cc_s2 104)" "0/ExclusiveLock/<none>"
send_wait s1 g_upd "UPDATE t7 SET id = 0 WHERE id = 1; SELECT count(*) FROM t7;"
send_wait s2 g_upd "UPDATE t7 SET id = 40000 WHERE id = 40002; SELECT count(*) FROM t7;"
send_wait s1 g_commit "COMMIT;"
send_wait s2 g_commit "COMMIT;"
check "G both deleting-then-updating transactions commit" \
	"$(errs_since "$mark")" "0"
check "G both DELETEs and both UPDATEs took effect" \
	"$(ctl_q "SELECT string_agg(id::text, ',' ORDER BY id) FROM t7;")" "0,40000,40003,40004"

# H: what serializes two writers of one row now is the chunk-group lock, taken for
# every group of the flush before the same-row check. s2's UPDATE marks a row of
# each of two groups; the second is the row s1 updated and has not committed.
# s2 must wait for s1 before it checks, or it finds no committed conflict, and
# both new versions of that row survive.
ctl_q "CREATE TABLE t8 (id int) USING pgcolumnar;" >/dev/null
ctl_q "INSERT INTO t8 SELECT g FROM generate_series(1,4) g;" >/dev/null
ctl_q "INSERT INTO t8 SELECT g FROM generate_series(5,8) g;" >/dev/null
send_wait s2 h_verbose "\\set VERBOSITY verbose"
mark="$(err_mark)"
send_wait s1 h_upd "BEGIN; UPDATE t8 SET id = -6 WHERE id = 6; SELECT count(*) FROM t8;"
send s2 "UPDATE t8 SET id = id + 100 WHERE id IN (2, 6);"
send s2 "\\echo <<h_upd2>>"
wait_blocked cc_s2 || true
check "premise: H s2's UPDATE waits on a group lock while s1 is open" \
	"$(lks cc_s2 101)" "ExclusiveLock,ExclusiveLock(waiting)"
send_wait s1 h_commit "COMMIT;"
wait_sentinel s2 h_upd2
check "H once s1 commits, s2's UPDATE of the same row fails with serialization_failure" \
	"$(errs_since "$mark")/$(grep -c 'ERROR:  40001: columnar: could not serialize access due to concurrent update or delete of the same row' "$WORKDIR/s2.out")" "1/1"
check "H the row s1 updated has one version, and s2 changed nothing" \
	"$(ctl_q "SELECT string_agg(id::text, ',' ORDER BY id) FROM t8;")" "-6,1,2,3,4,5,7,8"

# I: two first writers at REPEATABLE READ. The second waits for the first to
# insert the storage row; once it has committed, the re-check under the lock must
# read the latest committed state, not the transaction's snapshot, which predates
# that row, or the second inserts the row again and fails with 23505 on
# storage_pkey.
ctl_q "CREATE TABLE t9 (id int) USING pgcolumnar;" >/dev/null
check "premise: I t9 has no storage row before its first write" \
	"$(ctl_q "SELECT count(*) FROM pgcolumnar.storage WHERE relation_oid = 't9'::regclass;")" "0"
mark="$(err_mark)"
send_wait s1 i_begin "BEGIN ISOLATION LEVEL REPEATABLE READ; SELECT count(*) FROM t9;"
send_wait s2 i_begin "BEGIN ISOLATION LEVEL REPEATABLE READ; SELECT count(*) FROM t9;"
send_wait s1 i_ins "INSERT INTO t9 VALUES (1); SELECT count(*) FROM t9;"
send s2 "INSERT INTO t9 VALUES (2); SELECT count(*) FROM t9;"
send s2 "\\echo <<i_ins2>>"
wait_blocked cc_s2 || true
check "premise: I the second first writer waits on the storage-row lock" \
	"$(lks cc_s2 102)" "ExclusiveLock(waiting)"
send_wait s1 i_commit "COMMIT;"
wait_sentinel s2 i_ins2
send_wait s2 i_commit "COMMIT;"
check "I a second first writer at REPEATABLE READ commits once the first has, and the table has one storage row" \
	"$(errs_since "$mark")/$(ctl_q "SELECT string_agg(id::text, ',' ORDER BY id) FROM t9;")/$(ctl_q "SELECT count(*) FROM pgcolumnar.storage WHERE relation_oid = 't9'::regclass;")" \
	"0/1,2/1"

# J: the workload of concurrent_diff and of #2251, bounded. Six writers, five
# rounds each; a round is one transaction on its own 3000 ids: an INSERT of them
# (or, in the second table, rows already there), a DELETE of id % 7 = 0 and an
# UPDATE of id % 5 = 0. No row is shared, so no transaction may fail. Each
# statement runs under a 60 s statement_timeout and lock_timeout.
J_W=6; J_R=5; J_N=3000; J_ALL=$((J_W * J_R * J_N))
ctl_q "CREATE TABLE tj_ins (id int, v bigint) USING pgcolumnar;" >/dev/null
ctl_q "CREATE TABLE tj_pre (id int, v bigint) USING pgcolumnar;" >/dev/null
# one row group per round's ids, so writers share no group lock (class 101) and
# only buckets (class 104): a group shared by all would queue them on its lock
ctl_q "SELECT pgcolumnar.set_options('tj_pre', chunk_group_row_limit => 1000, stripe_row_limit => $J_N);" >/dev/null
ctl_q "INSERT INTO tj_pre SELECT g, g FROM generate_series(1, $J_ALL) g;" >/dev/null
j_run() {  # table insert(0|1) -> failed transactions over all writers and rounds
	local t="$1" ins="$2" w r lo hi pids=()
	for w in $(seq 0 $((J_W - 1))); do
		(
			for r in $(seq 0 $((J_R - 1))); do
				lo=$(( (w * J_R + r) * J_N + 1 )); hi=$(( lo + J_N - 1 ))
				{
					echo "BEGIN; SET LOCAL statement_timeout = '60s'; SET LOCAL lock_timeout = '60s';"
					[ "$ins" = 1 ] && echo "INSERT INTO $t SELECT g, g FROM generate_series($lo, $hi) g;"
					echo "DELETE FROM $t WHERE id BETWEEN $lo AND $hi AND id % 7 = 0;"
					echo "UPDATE $t SET v = v + 1 WHERE id BETWEEN $lo AND $hi AND id % 5 = 0;"
					echo "COMMIT;"
				} > "$WORKDIR/j.$t.$w.$r.sql"
				run_pg "$PSQL -f '$WORKDIR/j.$t.$w.$r.sql'" >/dev/null 2>>"$WORKDIR/j.$t.$w.err" ||
					echo x >> "$WORKDIR/j.$t.$w.failed"
			done
		) &
		pids+=($!)
	done
	for w in "${pids[@]}"; do wait "$w"; done
	cat "$WORKDIR"/j."$t".*.failed 2>/dev/null | wc -l | tr -d ' '
}
# the rows and sums the rounds leave, computed from the ids alone
J_WANT="$(ctl_q "SELECT count(*) || ' ' || sum(g + CASE WHEN g % 5 = 0 THEN 1 ELSE 0 END) FROM generate_series(1, $J_ALL) g WHERE g % 7 <> 0;")"
check "J six writers that each insert, delete and update their own rows, five rounds: no transaction fails" \
	"$(j_run tj_ins 1)" "0"
check "J and their rows are what the rounds leave" \
	"$(ctl_q "SELECT count(*) || ' ' || sum(v) FROM tj_ins;")" "$J_WANT"
check "J six writers that each delete and update their own existing rows, five rounds: no transaction fails (#2251)" \
	"$(j_run tj_pre 0)" "0"
check "J and those rows are what the rounds leave" \
	"$(ctl_q "SELECT count(*) || ' ' || sum(v) FROM tj_pre;")" "$J_WANT"

# K: a rewrite in a transaction that has written to the table, while another
# writer is open. Each holds the storage-row lock in ShareLock; a rewrite that
# waited for ExclusiveLock there, holding the relation's
# ShareUpdateExclusiveLock, deadlocked with the other doing the same. It refuses
# at once instead, and once no other writer is open the same rewrite runs.
send_wait s1 k_verbose "\\set VERBOSITY verbose"
for verb in "recluster:pgcolumnar.recluster('%T', 'id')" "compact_rewrite:pgcolumnar.compact_rewrite('%T', 0.0)"; do
	vname="${verb%%:*}"; t="tk_$vname"; call="${verb#*:}"; call="${call//%T/$t}"
	ctl_q "CREATE TABLE $t (id int) USING pgcolumnar;" >/dev/null
	ctl_q "SELECT pgcolumnar.set_options('$t', stripe_row_limit => 1000);" >/dev/null
	ctl_q "INSERT INTO $t SELECT g FROM generate_series(1,3000) g;" >/dev/null
	ctl_q "DELETE FROM $t WHERE id % 2 = 0;" >/dev/null
	mark="$(err_mark)"
	send_wait s1 "k_ins_$vname" "BEGIN; INSERT INTO $t VALUES (0); SELECT count(*) FROM $t;"
	send_wait s2 "k_ins_$vname" "BEGIN; INSERT INTO $t VALUES (-1); SELECT count(*) FROM $t;"
	send s1 "SELECT $call;"
	send s1 "\\echo <<k_rw_$vname>>"
	wait_settled s1 cc_s1 "k_rw_$vname"
	check "K $vname after s1's own INSERT, while s2's INSERT is open, refuses at once with 55P03" \
		"$(grep -c "<<k_rw_$vname>>" "$WORKDIR/s1.out")/$(grep -c "ERROR:  55P03: cannot rewrite table \"$t\" while another open transaction has written to it" "$WORKDIR/s1.out")" "1/1"
	send_wait s1 "k_rb_$vname" "ROLLBACK;"
	send_wait s2 "k_rw_$vname" "SELECT $call; COMMIT;"
	check "K once s1 has rolled back, s2's $vname after its own INSERT commits, and the rows are kept" \
		"$(errs_since "$mark")/$(ctl_q "SELECT count(*) || ' ' || count(*) FILTER (WHERE id IN (0,-1)) FROM $t;")" "1/1501 1"
done

# L: the group locks must be held from before the same-row check to commit, not
# only around the upsert. s3 holds the lock of the second group of s1's UPDATE, so
# s1's flush holds the first group's lock and waits. s2 then updates a row of
# that first group that s1 is updating, and waits on its lock. If s1 gave the
# lock back before its check, s2 would get it, both checks would run before
# either commits, and both new versions of the row would survive.
ctl_q "CREATE TABLE tl (id int) USING pgcolumnar;" >/dev/null
ctl_q "INSERT INTO tl SELECT g FROM generate_series(1,4) g;" >/dev/null
ctl_q "INSERT INTO tl SELECT g FROM generate_series(5,8) g;" >/dev/null
start_session s3
send s3 "SET application_name='cc_s3';"
mark="$(err_mark)"
serial_before="$(grep -c 'ERROR:  40001: columnar: could not serialize access due to concurrent update or delete of the same row' "$WORKDIR/s2.out")"
send_wait s3 l_del "BEGIN; DELETE FROM tl WHERE id = 6; SELECT count(*) FROM tl;"
send s1 "BEGIN; UPDATE tl SET id = id + 100 WHERE id IN (2, 7);"
send s1 "\\echo <<l_upd1>>"
wait_blocked cc_s1 || true
check "premise: L s1's UPDATE holds its first group's lock and waits on the second's, which s3 holds" \
	"$(lks cc_s1 101)" "ExclusiveLock,ExclusiveLock(waiting)"
send s2 "BEGIN; UPDATE tl SET id = -2 WHERE id = 2;"
send s2 "\\echo <<l_upd2>>"
wait_blocked cc_s2 || true
check "premise: L s2's UPDATE of a row s1 is updating waits on that row's group lock" \
	"$(lks cc_s2 101)" "ExclusiveLock(waiting)"
send_wait s3 l_c3 "COMMIT;"
wait_sentinel s1 l_upd1
send_wait s1 l_c1 "COMMIT;"
wait_sentinel s2 l_upd2
send_wait s2 l_c2 "COMMIT;"
check "L once s1 commits, s2's UPDATE of the same row fails with serialization_failure" \
	"$(errs_since "$mark")/$(( $(grep -c 'ERROR:  40001: columnar: could not serialize access due to concurrent update or delete of the same row' "$WORKDIR/s2.out") - serial_before ))" "1/1"
check "L the row both updated has one version, s1's" \
	"$(ctl_q "SELECT string_agg(id::text, ',' ORDER BY id) FROM tl;")" "1,3,4,5,8,102,107"

# M: a flush takes its group locks in group order, not in the order it visited
# the rows. s1's DELETE, an index nested loop over a VALUES list, visits a row of
# the third group and then one of the first, which s2 holds. Taken in visit
# order, s1 would hold the third group's lock while it waited, and s2's next
# DELETE there would close a cycle.
ctl_q "CREATE TABLE tm (id int) USING pgcolumnar;" >/dev/null
ctl_q "SELECT pgcolumnar.set_options('tm', chunk_group_row_limit => 1000, stripe_row_limit => 1000);" >/dev/null
ctl_q "INSERT INTO tm SELECT g FROM generate_series(1,3000) g;" >/dev/null
ctl_q "CREATE INDEX tm_id ON tm (id);" >/dev/null
ctl_q "ANALYZE tm;" >/dev/null
M_SET="SET LOCAL enable_hashjoin = off; SET LOCAL enable_mergejoin = off; SET LOCAL enable_seqscan = off;"
M_DEL="DELETE FROM tm USING (VALUES (1, 2510), (2, 20)) v(o, id) WHERE tm.id = v.id"
check "premise: M s1's DELETE is an index nested loop driven by the VALUES list" \
	"$(ctl_q "${M_SET//LOCAL /} EXPLAIN (COSTS OFF) $M_DEL;" | sed 's/^ *//; s/^-> *//' | cut -d' ' -f1-2 | paste -sd'|')" \
	"Delete on|Nested Loop|Values Scan|Index Scan|Index Cond:"
mark="$(err_mark)"
send_wait s2 m_del "BEGIN; DELETE FROM tm WHERE id = 10; SELECT count(*) FROM tm;"
send s1 "BEGIN; $M_SET $M_DEL;"
send s1 "\\echo <<m_del1>>"
wait_blocked cc_s1 || true
check "M s1's DELETE, which visits the third group first, waits for the first group holding no group lock" \
	"$(lks cc_s1 101)" "ExclusiveLock(waiting)"
send_wait s2 m_del2 "DELETE FROM tm WHERE id = 2520; SELECT count(*) FROM tm;"
send_wait s2 m_c2 "COMMIT;"
wait_sentinel s1 m_del1
send_wait s1 m_c1 "COMMIT;"
check "M both DELETE transactions commit, and the four rows are gone" \
	"$(errs_since "$mark")/$(ctl_q "SELECT count(*) FROM tm WHERE id IN (10, 20, 2510, 2520);")" "0/0"

send s1 "\\q"
send s2 "\\q"
send s3 "\\q"

echo
if [ "$fail" = 0 ]; then
	echo "checks run: $PGC_CHECKS"
	echo "CONCURRENCY TEST PASSED"
else
	echo "checks run: $PGC_CHECKS"
	echo "CONCURRENCY TEST FAILED"
fi
exit "$fail"
