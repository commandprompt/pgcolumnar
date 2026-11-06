#!/usr/bin/env bash
#
# pgColumnar concurrent unique-key insert test (tracking issue #5).
#
# A columnar row's DATA is invisible to other backends until its stripe is
# flushed at statement end, but its btree index entry (with the eagerly reserved
# synthetic TID) is written immediately. So while transaction T1's inserting
# statement is still in flight (row buffered, not flushed), a second transaction
# T2 inserting the SAME key finds T1's index entry but cannot resolve the row:
# pgcolumnar_index_fetch_tuple returns false for a row still in T1's private write
# buffer. The btree dirty-snapshot uniqueness check then treats the entry as dead
# and T2 inserts a duplicate -- two live rows with the same unique key.
#
# The fix serializes inserters of the same unique key with a transaction-scoped
# advisory lock taken inside the table AM insert path (columnar_unique.c), before
# the executor's btree uniqueness check runs. The second inserter blocks until
# the first commits (and has therefore flushed its row), at which point the
# ordinary btree check sees the committed duplicate and raises unique_violation.
#
# Making the race deterministic
# ------------------------------
# The bug only manifests while T1's row is UNFLUSHED. A completed INSERT
# statement flushes at executor-end, so a plain "T1 inserts, then T2 inserts"
# would let T2 see T1's flushed-but-uncommitted stripe via the dirty snapshot and
# block on T1's xid even without the fix. To hold T1 mid-statement -- key row
# buffered and indexed, but statement not yet ended, so not yet flushed -- T1
# runs a two-row INSERT whose SECOND row calls a function that blocks on an
# advisory lock held by a separate "holder" session. The first row (the real
# key) is inserted and indexed; the second row's evaluation blocks. While T1
# hangs there:
#   * lock DISABLED (pgcolumnar.enable_unique_insert_lock=off in T2): T2 does NOT
#     block, inserts a duplicate -> final count 2. This reproduces the bug.
#   * lock ENABLED (default): T2 blocks on the key lock T1 holds -> after T1
#     commits, T2 gets a unique_violation -> final count 1. This is the fix.
# The barrier that makes it deterministic is a poll on pg_stat_activity for a
# real heavyweight-lock wait, never a sleep.
#
# Scenarios: same-key (fails-before/passes-after via the GUC), opclass-equal-but-
# byte-different keys (numeric 1.0 vs 1.00, and citext case when available) to
# prove opclass-safe hashing, different keys not blocking (preserved
# concurrency), multi-column unique index, partial unique index (inside and
# outside the predicate), NULLS DISTINCT (no false conflict) and NULLS NOT
# DISTINCT (PG15+), a same-statement duplicate, and an aborted first inserter.
#
# Written fresh for pgColumnar; it reuses no upstream test file or expected
# output. Derived from the format/interface spec, the issue #5 design analysis,
# and the public PostgreSQL API.
#
# Usage:
#   test/unique_conc.sh [PG_CONFIG]
#
# PG_CONFIG defaults to /usr/local/pg17/bin/pg_config. Run as a user that may
# "runuser -u postgres" (e.g. root) when the current user is not postgres.


# Own harness rather than lib.sh; portlib carries the port band.
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
# THE NAME IS FIXED PER WAIT KIND, with the session and sentinel in pgc_fail's
# DETAIL. The ledger is keyed on (suite, part, name), so interpolating
# "$name/$label" into the name would mint rows nobody can enumerate and therefore
# nobody can seed.
#
# NO GREEN RUN EXECUTES THIS. It records only on the timeout path, so a passing
# suite emits nothing here and the record stream is unchanged.
#
# lib.sh's pgc_fail, not a direct pgc_record (#1341): the pytest twin's grading
# reads names through lib.sh's recorders, and a suite that records on its own is
# one the twin check cannot read.
wait_timeout() {	# wait_timeout FIXED-NAME DETAIL
	pgc_fail "$1" "timed out waiting for $2"
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
PG_MAJOR="$("$PG_CONFIG" --version | sed -E 's/^[^0-9]*([0-9]+).*/\1/')"

WORKDIR="$(mktemp -d /tmp/pgcolumnar-uconc.XXXXXX)"
PGDATA="$WORKDIR/data"
LOGFILE="$WORKDIR/server.log"

port_is_free() {
	if command -v ss >/dev/null 2>&1; then
		# grep -c on a captured value, not a pipe into grep -q. A spurious
		# EPIPE here answers "nothing is listening" for a port that IS taken,
		# and the suite then starts a cluster on an occupied port.
		_pif="$(ss -Htln "sport = :$1" 2>/dev/null || true)"
		[ "$(grep -c ":$1" <<<"$_pif" || true)" -eq 0 ]
	else
		! (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
	fi
}
# This cluster listens on a unix socket only (listen_addresses='' below), so the
# number never binds a TCP port and the ephemeral race cannot reach it. Drawn from
# portlib's band regardless: a port picker that is safe by accident, in a file
# that says nothing about why, is one edit away from being unsafe.
pick_port() {
	local p i
	for i in $(seq 1 100); do
		p=$(( PGC_PORT_LO + RANDOM % (PGC_PORT_HI - PGC_PORT_LO) ))
		if port_is_free "$p"; then echo "$p"; return 0; fi
	done
	echo $(( PGC_PORT_LO + RANDOM % (PGC_PORT_HI - PGC_PORT_LO) ))
}
PORT="${PGC_PORT:-$(pick_port)}"

echo "== pgColumnar concurrent unique-key insert test (issue #5) =="
echo "PG_CONFIG=$PG_CONFIG (major $PG_MAJOR)"
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
	for p in "${SESS_PIDS[@]:-}"; do kill "$p" >/dev/null 2>&1 || true; done
	run_pg "pg_ctl -D '$PGDATA' stop -m immediate -w" >/dev/null 2>&1 || true
	if [ -f "$PGDATA/postmaster.pid" ]; then
		pmpid="$(head -n1 "$PGDATA/postmaster.pid" 2>/dev/null)"
		if [ -n "${pmpid:-}" ] && [ "$pmpid" -gt 1 ] 2>/dev/null; then
			kill -9 "$pmpid" >/dev/null 2>&1 || true
		fi
	fi
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
run_pg "echo \"listen_addresses=''\" >> '$PGDATA/postgresql.conf'"
run_pg "echo \"unix_socket_directories='$WORKDIR'\" >> '$PGDATA/postgresql.conf'"
# Cap any real hang so a bug cannot masquerade as a pass by blocking forever.
run_pg "echo \"lock_timeout=60000\" >> '$PGDATA/postgresql.conf'"
# The bucket count is part of the advisory lock tag, so it is fixed at server
# start rather than set per session: two backends that disagreed would hash the
# same key to different buckets and not serialize at all. A large prime keeps
# unrelated keys out of the same bucket, so a serialization this suite observes
# is the real guarantee and not two keys colliding into one lock.
run_pg "echo \"pgcolumnar.unique_lock_buckets=100003\" >> '$PGDATA/postgresql.conf'"
echo "-- start"
run_pg "pg_ctl -D '$PGDATA' -l '$LOGFILE' start -w" >/dev/null
run_pg "createdb -h '$WORKDIR' -p $PORT uconc"

PSQL="psql -h '$WORKDIR' -p $PORT -d uconc -qAtX -v ON_ERROR_STOP=1"
SPSQL="psql -h '$WORKDIR' -p $PORT -d uconc -qAtX"
ctl_q() { run_pg "$PSQL -c \"$1\""; }
# Same, but keeps going on error and captures the message, for checks whose
# subject is the error itself.
ctl_qe() { run_pg "$SPSQL -c \"$1\" 2>&1"; }

# lib.sh's `check` records every result (#965), and since #1341 this suite uses
# it rather than a local wrapper around pgc_record. The local one printed the
# measured value on a PASS too. lib.sh's prints it on a FAIL, which is when it is
# read, and the twin check can read the names, which it could not before.
# pgc_summary at the end prints the accounting line the matrix reconciles.

# --- persistent interactive sessions over FIFOs (as in test/concurrency.sh) ---
start_session() {  # name
	local name="$1"
	local infile="$WORKDIR/$name.in"
	local outfile="$WORKDIR/$name.out"
	run_pg "mkfifo '$infile'; touch '$outfile'"
	run_pg "$SPSQL >'$outfile' 2>&1 <'$infile'" &
	SESS_PIDS+=("$!")
	exec {fd}<>"$infile"
	eval "FD_$name=$fd"
}
send() {  # name sql...
	local name="$1"; shift
	local fd; eval "fd=\$FD_$name"
	printf '%s\n' "$*" >&"$fd"
}
send_wait() {  # name label sql...
	local name="$1" label="$2"; shift 2
	local fd outfile="$WORKDIR/$name.out" i=0
	eval "fd=\$FD_$name"
	printf '%s\n' "$*" >&"$fd"
	printf '\\echo <<%s>>\n' "$label" >&"$fd"
	while ! grep -q "<<$label>>" "$outfile" 2>/dev/null; do
		sleep 0.05; i=$((i + 1))
		if [ "$i" -ge 1200 ]; then
			wait_timeout "a bounded wait for a command's sentinel completed" "$name/$label"; return 1
		fi
	done
	return 0
}
wait_sentinel() {  # name label
	local name="$1" label="$2" outfile i=0
	outfile="$WORKDIR/$name.out"
	while ! grep -q "<<$label>>" "$outfile" 2>/dev/null; do
		sleep 0.05; i=$((i + 1))
		if [ "$i" -ge 1200 ]; then
			wait_timeout "a bounded wait for a standalone sentinel completed" "$name/$label"; return 1
		fi
	done
	return 0
}
# wait_blocked APP [asserted]: with "asserted", the caller's next check counts the
# waiter itself, so a wait that runs out is that check's failure and is not
# recorded twice (#1341: a mutant that took no key lock recorded a second, fixed-
# name failure per arm, which no green run emits and so no ledger row can hold).
# It also gives up after 10s, as a waiter that is going to block does so at once.
wait_blocked() {
	local app="$1" i=0 n limit=1200
	[ "${2:-}" = asserted ] && limit=200
	while :; do
		n="$(ctl_q "SELECT count(*) FROM pg_stat_activity WHERE application_name='$app' AND wait_event_type='Lock';")"
		[ "$n" = "1" ] && return 0
		sleep 0.05; i=$((i + 1))
		if [ "$i" -ge "$limit" ]; then
			[ "${2:-}" = asserted ] && return 1
			wait_timeout "a bounded wait for a session to block completed" "$app"; return 1
		fi
	done
}
wait_idle_intx() {  # application_name
	local app="$1" i=0 st
	while :; do
		st="$(ctl_q "SELECT state FROM pg_stat_activity WHERE application_name='$app';")"
		[ "$st" = "idle in transaction" ] && return 0
		sleep 0.05; i=$((i + 1))
		if [ "$i" -ge 1200 ]; then
			wait_timeout "a bounded wait for a session to reach idle-in-transaction completed" "$app"; return 1
		fi
	done
}
# does session $1's cumulative output contain the substring $2?
out_has() { grep -qF -- "$2" "$WORKDIR/$1.out" 2>/dev/null; }
# returned SESSION LABEL -> "returned" when the session's INSERT came back inside a
# live transaction, which the SELECT of LABEL after it shows, else "refused". The
# no-wait arms give T2 a 5s lock_timeout, so an INSERT that waits is refused well
# inside the 60s bounded wait. Without it, the server's 60s lock_timeout and the
# wait ran out together, T2 was no longer waiting when the count was read, and
# these arms could not fail (#1341, measured with a mutant that hashes only a key's
# first column: 33/33).
returned() {
	out_has "$1" "$2" && echo returned || echo refused
}
# violated SESSION INDEX -> yes when the session's output holds a 23505 naming the
# index. Its psql runs with VERBOSITY verbose, so the SQLSTATE is printed: the
# index name alone would also match another error that named it (#1341).
violated() {
	out_has "$1" "ERROR:  23505: duplicate key value violates unique constraint \"$2\"" && echo yes || echo no
}

ctl_q "CREATE EXTENSION pgcolumnar;" >/dev/null
# A row whose second value blocks on a holder-session advisory lock, holding T1
# mid-INSERT (its first, real-key row already buffered and indexed but the
# statement not yet ended, hence not yet flushed). plpgsql so it is never
# inlined or constant-folded; anyelement so one function serves every key type.
# The body is single-quoted rather than dollar-quoted on purpose: this command
# passes through two bash layers (ctl_q -> run_pg -> bash -lc), and a $$ tag
# would be expanded to a PID by the inner shell. The body contains no single
# quotes, so single-quoting is safe.
ctl_q "CREATE FUNCTION cq_block(k bigint, v anyelement) RETURNS anyelement LANGUAGE plpgsql VOLATILE AS 'BEGIN PERFORM pg_advisory_xact_lock(k); RETURN v; END;';" >/dev/null

# Fail fast if the helper is not usable (e.g. a quoting regression), rather than
# letting every lock-wait barrier time out.
if [ "$(ctl_q "SELECT cq_block(1::bigint, 42);")" != "42" ]; then
	echo "FATAL: cq_block helper is not callable"
	exit 1
fi

CITEXT=0
if run_pg "$SPSQL -c \"CREATE EXTENSION IF NOT EXISTS citext;\"" >/dev/null 2>&1; then
	CITEXT=1
fi

start_session s1
start_session s2
start_session sh   # holder of the mid-statement pause lock
send s1 "SET application_name='cc_s1';"
send s2 "SET application_name='cc_s2';"
send s2 "\\set VERBOSITY verbose"
# This suite's cluster is started with the large prime above. Pin both halves of
# what that buys: a session cannot change the count out from under the advisory
# lock tag, and the cluster really is running the count the suite needs.
# The output is captured before it is matched: piping ctl_qe straight into
# grep -q makes grep exit on the first match, the psql upstream take SIGPIPE,
# and the pipeline report failure under `set -o pipefail` even though the
# message was there, which turns this into a check that can never pass.
bucket_set_err="$(ctl_qe 'SET pgcolumnar.unique_lock_buckets = 1;')"
check "the bucket count cannot be changed per session" \
	"$(grep -qE 'ERROR:.*cannot be changed' <<<"$bucket_set_err" && echo OK || echo "NO ERROR")" "OK"
check "the cluster runs the bucket count the suite needs" \
	"$(ctl_q 'SHOW pgcolumnar.unique_lock_buckets;')" "100003"

HKEY=1000   # holder advisory-lock key, unique per pause

# pause_s1 <insert-sql-using-cq_block(HKEY,...)>
# Leaves T1 blocked mid-INSERT with its real-key row buffered+indexed, unflushed.
pause_s1() {
	send_wait sh "lk_$HKEY" "SELECT pg_advisory_lock($HKEY);"
	send_wait s1 "bg_$HKEY" "BEGIN;"
	send s1 "$1"
	send s1 "\\echo <<s1done_$HKEY>>"
	wait_blocked cc_s1
}
# release the pause and let T1's INSERT finish (flush at executor-end)
release_s1() {
	send_wait sh "ul_$HKEY" "SELECT pg_advisory_unlock($HKEY);"
	wait_sentinel s1 "s1done_$HKEY"
}

# ===========================================================================
# Scenario 1: same key. Fails-before (lock disabled) and passes-after (default).
# ===========================================================================
ctl_q "CREATE TABLE s_int (k int) USING pgcolumnar;" >/dev/null
ctl_q "CREATE UNIQUE INDEX s_int_uidx ON s_int (k);" >/dev/null

# --- 1a: lock DISABLED reproduces the bug (T2 does not block; duplicate) ------
HKEY=1001
pause_s1 "INSERT INTO s_int SELECT CASE WHEN g=2 THEN cq_block($HKEY, -1) ELSE 1 END FROM generate_series(1,2) g;"
send_wait s2 "s2off_beg" "BEGIN;"
send s2 "SET LOCAL pgcolumnar.enable_unique_insert_lock = off;"
send s2 "SET LOCAL lock_timeout = '5s';"
# T1 holds key 1 unflushed. With the lock off, T2 must NOT block; it inserts a
# duplicate and commits. (With the fix on, it would block here instead.)
send_wait s2 "s2off_ins" "INSERT INTO s_int VALUES (1);"
send_wait s2 "s2off_com" "COMMIT;"
release_s1
send_wait s1 "s1_com_1a" "COMMIT;"
check "1a pre-fix: lock disabled lets both same-key inserts commit (bug)" \
	"$(ctl_q "SELECT count(*) FROM s_int WHERE k = 1;")" "2"

# --- 1b: lock ENABLED (default) serializes and raises unique_violation --------
ctl_q "TRUNCATE s_int;" >/dev/null
HKEY=1002
pause_s1 "INSERT INTO s_int SELECT CASE WHEN g=2 THEN cq_block($HKEY, -1) ELSE 1 END FROM generate_series(1,2) g;"
send_wait s2 "s2on_beg" "BEGIN;"
send s2 "INSERT INTO s_int VALUES (1);"
send s2 "\\echo <<s2on_ins>>"
wait_blocked cc_s2 asserted
check "1b post-fix: same-key second inserter blocks" \
	"$(ctl_q "SELECT count(*) FROM pg_stat_activity WHERE application_name='cc_s2' AND wait_event_type='Lock';")" "1"
release_s1
send_wait s1 "s1_com_1b" "COMMIT;"   # releases the key lock; T2 unblocks
wait_sentinel s2 s2on_ins             # T2's INSERT completed (with an error)
check "1b post-fix: blocked inserter got a unique_violation" \
	"$(violated s2 s_int_uidx)" "yes"
send_wait s2 "s2on_rb" "ROLLBACK;"
check "1b post-fix: exactly one row with key 1" \
	"$(ctl_q "SELECT count(*) FROM s_int WHERE k = 1;")" "1"

# ===========================================================================
# Scenario 2: opclass-equal but byte-different keys must serialize.
# numeric 1.0 vs 1.00 are equal under numeric equality; a raw-byte hash would
# split them and miss the conflict. The fix hashes with the numeric hash proc.
# ===========================================================================
ctl_q "CREATE TABLE s_num (v numeric) USING pgcolumnar;" >/dev/null
ctl_q "CREATE UNIQUE INDEX s_num_uidx ON s_num (v);" >/dev/null

# pre-fix contrast: byte-different equal keys both commit when the lock is off
HKEY=1003
pause_s1 "INSERT INTO s_num SELECT CASE WHEN g=2 THEN cq_block($HKEY, (-1)::numeric) ELSE 1.0 END FROM generate_series(1,2) g;"
send_wait s2 "n_off_beg" "BEGIN;"
send s2 "SET LOCAL pgcolumnar.enable_unique_insert_lock = off;"
send s2 "SET LOCAL lock_timeout = '5s';"
send_wait s2 "n_off_ins" "INSERT INTO s_num VALUES (1.00);"
send_wait s2 "n_off_com" "COMMIT;"
release_s1
send_wait s1 "s1_com_2a" "COMMIT;"
check "2a pre-fix: numeric 1.0 and 1.00 both commit with lock off (bug)" \
	"$(ctl_q "SELECT count(*) FROM s_num WHERE v = 1.0;")" "2"

ctl_q "TRUNCATE s_num;" >/dev/null
HKEY=1004
pause_s1 "INSERT INTO s_num SELECT CASE WHEN g=2 THEN cq_block($HKEY, (-1)::numeric) ELSE 1.0 END FROM generate_series(1,2) g;"
send_wait s2 "n_on_beg" "BEGIN;"
send s2 "INSERT INTO s_num VALUES (1.00);"   # byte-different, opclass-equal to 1.0
send s2 "\\echo <<n_on_ins>>"
wait_blocked cc_s2 asserted
check "2b post-fix: numeric 1.00 serializes behind 1.0 (opclass-safe hash)" \
	"$(ctl_q "SELECT count(*) FROM pg_stat_activity WHERE application_name='cc_s2' AND wait_event_type='Lock';")" "1"
release_s1
send_wait s1 "s1_com_2b" "COMMIT;"
wait_sentinel s2 n_on_ins
check "2b post-fix: numeric equal-but-different-bytes got unique_violation" \
	"$(violated s2 s_num_uidx)" "yes"
send_wait s2 "n_on_rb" "ROLLBACK;"
check "2b post-fix: exactly one row equal to 1.0" \
	"$(ctl_q "SELECT count(*) FROM s_num WHERE v = 1.0;")" "1"

# citext case difference, when the extension is available
if [ "$CITEXT" = 1 ]; then
	ctl_q "CREATE TABLE s_ci (v citext) USING pgcolumnar;" >/dev/null
	ctl_q "CREATE UNIQUE INDEX s_ci_uidx ON s_ci (v);" >/dev/null
	HKEY=1005
	pause_s1 "INSERT INTO s_ci SELECT CASE WHEN g=2 THEN cq_block($HKEY, 'zzz'::citext) ELSE 'ABC'::citext END FROM generate_series(1,2) g;"
	send_wait s2 "ci_beg" "BEGIN;"
	send s2 "INSERT INTO s_ci VALUES ('abc');"   # equals 'ABC' under citext
	send s2 "\\echo <<ci_ins>>"
	wait_blocked cc_s2 asserted
	check "2c post-fix: citext 'abc' serializes behind 'ABC' (case-folded hash)" \
		"$(ctl_q "SELECT count(*) FROM pg_stat_activity WHERE application_name='cc_s2' AND wait_event_type='Lock';")" "1"
	release_s1
	send_wait s1 "s1_com_2c" "COMMIT;"
	wait_sentinel s2 ci_ins
	check "2c post-fix: citext case-insensitive duplicate got unique_violation" \
		"$(violated s2 s_ci_uidx)" "yes"
	send_wait s2 "ci_rb" "ROLLBACK;"
	check "2c post-fix: exactly one row equal to 'abc'" \
		"$(ctl_q "SELECT count(*) FROM s_ci WHERE v = 'abc';")" "1"
else
	check_skip "the 2c citext case" "SKIP  2c citext case test (citext extension not available)" "citext extension not available"
fi

# ===========================================================================
# Scenario 3: different keys must NOT block each other (preserved concurrency).
# ===========================================================================
ctl_q "CREATE TABLE s_diff (k int) USING pgcolumnar;" >/dev/null
ctl_q "CREATE UNIQUE INDEX s_diff_uidx ON s_diff (k);" >/dev/null
HKEY=1006
pause_s1 "INSERT INTO s_diff SELECT CASE WHEN g=2 THEN cq_block($HKEY, -1) ELSE 10 END FROM generate_series(1,2) g;"
send_wait s2 "d_beg" "BEGIN;"
send s2 "SET LOCAL lock_timeout = '5s';"
send_wait s2 "d_ins" "INSERT INTO s_diff VALUES (20);"
send_wait s2 "d_ok" "SELECT 'returned_d';"
[ "$(returned s2 returned_d)" = returned ] && wait_idle_intx cc_s2
check "3 different keys do not block each other" \
	"$(ctl_q "SELECT count(*) FROM pg_stat_activity WHERE application_name='cc_s2' AND wait_event_type='Lock';")|$(returned s2 returned_d)" "0|returned"
send_wait s2 "d_com" "COMMIT;"
release_s1
send_wait s1 "s1_com_3" "COMMIT;"
check "3 both distinct keys present" \
	"$(ctl_q "SELECT string_agg(k::text, ',' ORDER BY k) FROM s_diff WHERE k IN (10,20);")" "10,20"

# ===========================================================================
# Scenario 4: multi-column unique index.
# ===========================================================================
ctl_q "CREATE TABLE s_mc (a int, b int) USING pgcolumnar;" >/dev/null
ctl_q "CREATE UNIQUE INDEX s_mc_uidx ON s_mc (a, b);" >/dev/null
# equal composite key (1,2) must serialize and conflict
HKEY=1007
pause_s1 "INSERT INTO s_mc SELECT CASE WHEN g=2 THEN cq_block($HKEY, -1) ELSE 1 END, 2 FROM generate_series(1,2) g;"
send_wait s2 "mc_beg" "BEGIN;"
send s2 "INSERT INTO s_mc VALUES (1, 2);"
send s2 "\\echo <<mc_ins>>"
wait_blocked cc_s2 asserted
check "4 multi-column equal key serializes" \
	"$(ctl_q "SELECT count(*) FROM pg_stat_activity WHERE application_name='cc_s2' AND wait_event_type='Lock';")" "1"
release_s1
send_wait s1 "s1_com_4a" "COMMIT;"
wait_sentinel s2 mc_ins
check "4 multi-column duplicate got unique_violation" \
	"$(violated s2 s_mc_uidx)" "yes"
send_wait s2 "mc_rb" "ROLLBACK;"
check "4 exactly one (1,2) row" \
	"$(ctl_q "SELECT count(*) FROM s_mc WHERE a=1 AND b=2;")" "1"
# a composite key differing in one column must NOT block
HKEY=1008
pause_s1 "INSERT INTO s_mc SELECT CASE WHEN g=2 THEN cq_block($HKEY, -1) ELSE 5 END, 6 FROM generate_series(1,2) g;"
send_wait s2 "mc2_beg" "BEGIN;"
send s2 "SET LOCAL lock_timeout = '5s';"
send_wait s2 "mc2_ins" "INSERT INTO s_mc VALUES (5, 7);"
send_wait s2 "mc2_ok" "SELECT 'returned_mc2';"
[ "$(returned s2 returned_mc2)" = returned ] && wait_idle_intx cc_s2
check "4 multi-column differing key does not block" \
	"$(ctl_q "SELECT count(*) FROM pg_stat_activity WHERE application_name='cc_s2' AND wait_event_type='Lock';")|$(returned s2 returned_mc2)" "0|returned"
send_wait s2 "mc2_com" "COMMIT;"
release_s1
send_wait s1 "s1_com_4b" "COMMIT;"

# ===========================================================================
# Scenario 5: partial unique index (uniqueness only where active).
# ===========================================================================
ctl_q "CREATE TABLE s_part (k int, active boolean) USING pgcolumnar;" >/dev/null
ctl_q "CREATE UNIQUE INDEX s_part_uidx ON s_part (k) WHERE active;" >/dev/null
# inside the predicate: (1,true) vs (1,true) must serialize and conflict
HKEY=1009
pause_s1 "INSERT INTO s_part SELECT CASE WHEN g=2 THEN cq_block($HKEY, -1) ELSE 1 END, true FROM generate_series(1,2) g;"
send_wait s2 "pt_beg" "BEGIN;"
send s2 "INSERT INTO s_part VALUES (1, true);"
send s2 "\\echo <<pt_ins>>"
wait_blocked cc_s2 asserted
check "5 partial index, row inside predicate serializes" \
	"$(ctl_q "SELECT count(*) FROM pg_stat_activity WHERE application_name='cc_s2' AND wait_event_type='Lock';")" "1"
release_s1
send_wait s1 "s1_com_5a" "COMMIT;"
wait_sentinel s2 pt_ins
check "5 partial index in-predicate duplicate got unique_violation" \
	"$(violated s2 s_part_uidx)" "yes"
send_wait s2 "pt_rb" "ROLLBACK;"
check "5 exactly one active row with k=1" \
	"$(ctl_q "SELECT count(*) FROM s_part WHERE k=1 AND active;")" "1"
# outside the predicate: (2,false) held by T1, T2 inserts (2,false); the index
# does not cover these rows, so T2 must NOT block and both rows are kept.
HKEY=1010
pause_s1 "INSERT INTO s_part SELECT CASE WHEN g=2 THEN cq_block($HKEY, -1) ELSE 2 END, false FROM generate_series(1,2) g;"
send_wait s2 "pt2_beg" "BEGIN;"
send s2 "SET LOCAL lock_timeout = '5s';"
send_wait s2 "pt2_ins" "INSERT INTO s_part VALUES (2, false);"
send_wait s2 "pt2_ok" "SELECT 'returned_pt2';"
[ "$(returned s2 returned_pt2)" = returned ] && wait_idle_intx cc_s2
check "5 partial index, rows outside predicate do not serialize" \
	"$(ctl_q "SELECT count(*) FROM pg_stat_activity WHERE application_name='cc_s2' AND wait_event_type='Lock';")|$(returned s2 returned_pt2)" "0|returned"
send_wait s2 "pt2_com" "COMMIT;"
release_s1
send_wait s1 "s1_com_5b" "COMMIT;"
check "5 both inactive k=2 rows kept (predicate not enforced)" \
	"$(ctl_q "SELECT count(*) FROM s_part WHERE k=2 AND NOT active;")" "2"

# ===========================================================================
# Scenario 6: NULL handling.
# ===========================================================================
# NULLS DISTINCT (default): two NULL keys never conflict -> no block, both kept.
ctl_q "CREATE TABLE s_nd (k int) USING pgcolumnar;" >/dev/null
ctl_q "CREATE UNIQUE INDEX s_nd_uidx ON s_nd (k);" >/dev/null
HKEY=1011
pause_s1 "INSERT INTO s_nd SELECT CASE WHEN g=2 THEN cq_block($HKEY, -1) ELSE NULL END FROM generate_series(1,2) g;"
send_wait s2 "nd_beg" "BEGIN;"
send s2 "SET LOCAL lock_timeout = '5s';"
send_wait s2 "nd_ins" "INSERT INTO s_nd VALUES (NULL);"
send_wait s2 "nd_ok" "SELECT 'returned_nd';"
[ "$(returned s2 returned_nd)" = returned ] && wait_idle_intx cc_s2
check "6 NULLS DISTINCT: two NULL keys do not block" \
	"$(ctl_q "SELECT count(*) FROM pg_stat_activity WHERE application_name='cc_s2' AND wait_event_type='Lock';")|$(returned s2 returned_nd)" "0|returned"
send_wait s2 "nd_com" "COMMIT;"
release_s1
send_wait s1 "s1_com_6a" "COMMIT;"
check "6 NULLS DISTINCT: both NULL rows kept" \
	"$(ctl_q "SELECT count(*) FROM s_nd WHERE k IS NULL;")" "2"

# NULLS NOT DISTINCT: NULL keys DO conflict -> serialize and violate. It is
# PG15's, the oldest major this tree builds, so the PG < 15 skip that stood here
# was a branch no run could take (#1341).
ctl_q "CREATE TABLE s_nn (k int) USING pgcolumnar;" >/dev/null
ctl_q "CREATE UNIQUE INDEX s_nn_uidx ON s_nn (k) NULLS NOT DISTINCT;" >/dev/null
HKEY=1012
pause_s1 "INSERT INTO s_nn SELECT CASE WHEN g=2 THEN cq_block($HKEY, -1) ELSE NULL END FROM generate_series(1,2) g;"
send_wait s2 "nn_beg" "BEGIN;"
send s2 "INSERT INTO s_nn VALUES (NULL);"
send s2 "\\echo <<nn_ins>>"
wait_blocked cc_s2 asserted
check "6 NULLS NOT DISTINCT: second NULL key serializes" \
	"$(ctl_q "SELECT count(*) FROM pg_stat_activity WHERE application_name='cc_s2' AND wait_event_type='Lock';")" "1"
release_s1
send_wait s1 "s1_com_6b" "COMMIT;"
wait_sentinel s2 nn_ins
check "6 NULLS NOT DISTINCT: second NULL got unique_violation" \
	"$(violated s2 s_nn_uidx)" "yes"
send_wait s2 "nn_rb" "ROLLBACK;"
check "6 NULLS NOT DISTINCT: exactly one NULL row" \
	"$(ctl_q "SELECT count(*) FROM s_nn WHERE k IS NULL;")" "1"

# ===========================================================================
# Scenario 7: same-statement duplicate is still caught (no regression).
# ===========================================================================
ctl_q "CREATE TABLE s_ss (k int) USING pgcolumnar;" >/dev/null
ctl_q "CREATE UNIQUE INDEX s_ss_uidx ON s_ss (k);" >/dev/null
ss_err="$(run_pg "$SPSQL -v VERBOSITY=verbose -c \"INSERT INTO s_ss SELECT 7 FROM generate_series(1,2);\" 2>&1" )"
check "7 same-statement duplicate raises unique_violation" \
	"$(grep -qF 'ERROR:  23505: duplicate key value violates unique constraint "s_ss_uidx"' <<<"$ss_err" && echo yes || echo no)" "yes"
check "7 same-statement duplicate inserted no rows" \
	"$(ctl_q "SELECT count(*) FROM s_ss;")" "0"

# ===========================================================================
# Scenario 8: aborted first inserter -> second inserter succeeds.
# Here T1's INSERT completes and flushes (uncommitted) and holds the key lock;
# T2 blocks on that lock; T1 ROLLBACK releases it and discards the stripe, so
# T2's re-check finds no committed row and the insert succeeds.
# ===========================================================================
ctl_q "CREATE TABLE s_ab (k int) USING pgcolumnar;" >/dev/null
ctl_q "CREATE UNIQUE INDEX s_ab_uidx ON s_ab (k);" >/dev/null
send_wait s1 "ab_beg" "BEGIN;"
send_wait s1 "ab_ins" "INSERT INTO s_ab VALUES (1);"   # completes, flushes, holds lock
send_wait s2 "ab_beg2" "BEGIN;"
send s2 "INSERT INTO s_ab VALUES (1);"
send s2 "\\echo <<ab_ins2>>"
wait_blocked cc_s2 asserted
check "8 second inserter blocks behind uncommitted first" \
	"$(ctl_q "SELECT count(*) FROM pg_stat_activity WHERE application_name='cc_s2' AND wait_event_type='Lock';")" "1"
send_wait s1 "ab_rb" "ROLLBACK;"   # first inserter aborts; row never committed
wait_sentinel s2 ab_ins2           # T2's INSERT now completes (should succeed)
send_wait s2 "ab_com" "COMMIT;"
check "8 after first aborts, second insert succeeds; one row" \
	"$(ctl_q "SELECT count(*) FROM s_ab WHERE k=1;")" "1"

send s1 "\\q"
send s2 "\\q"
send sh "\\q"

pgc_summary
