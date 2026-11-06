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

send s1 "\\q"
send s2 "\\q"

echo
if [ "$fail" = 0 ]; then
	echo "checks run: $PGC_CHECKS"
	echo "CONCURRENCY TEST PASSED"
else
	echo "checks run: $PGC_CHECKS"
	echo "CONCURRENCY TEST FAILED"
fi
exit "$fail"
