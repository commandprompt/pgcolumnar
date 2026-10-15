#!/usr/bin/env bash
#
# pgColumnar concurrent same-row UPDATE test (tracking issue #5, the UPDATE half).
#
# A columnar UPDATE is delete-old + insert-new (src/columnar_tableam.c
# pgcolumnar_tuple_update): it marks the old row deleted in the delete vector and
# appends a fresh row with a new row number. It always returns TM_Ok and takes no
# lock on the row identity, and pgcolumnar_tuple_lock raises COLUMNAR_UNSUPPORTED,
# so core never runs EvalPlanQual. Two sessions updating the SAME row therefore
# each keep their own new version. The old row is deleted once (idempotent) and
# both new rows survive, so the row is DUPLICATED and one update is LOST. A heap
# serializes transparently: at READ COMMITTED EvalPlanQual re-applies the second
# update to the first's new version, at REPEATABLE READ the second gets a
# serialization_failure. Columnar does neither. This is the known limitation
# documented in docs/limitations.md (Concurrency section).
#
# THE OBSERVED MECHANISM (probed on the bench, not assumed):
# The second writer DOES block, but on the wrong lock. A columnar UPDATE flushes
# its delete mark at the statement's executor-end and holds the chunk-group
# advisory lock (issue #4) until commit. So s2's UPDATE of the same row blocks on
# that chunk-group lock (wait_event_type='Lock', wait_event='advisory'), exactly
# as a heap s2 blocks on the row lock. The chunk-group lock serializes the
# delete-vector read-modify-write; it does NOT serialize the row identity, so both
# appends still happen and the duplicate survives after s2 unblocks and commits.
# Both engines therefore reach wait_event_type='Lock', so ONE wait_blocked barrier
# forces the overlap on both arms. The engines diverge only in the final state and
# in whether the losing writer errors, which is exactly what the heap oracle
# measures.
#
# This suite is RED-first: the checks encode the CORRECT (post-fix) behavior, so
# every columnar arm FAILS on today's code and PASSES once same-row writes
# serialize. A heap table runs the byte-identical interleaving as the oracle, and
# a non-overlapping serial columnar arm is the removal-proof control: it stays
# GREEN today, proving the RED is caused by the forced OVERLAP and not a broken
# update. Barriers are pg_stat_activity polls for a real lock wait, never sleeps;
# lock_timeout caps any genuine hang so a wedge cannot masquerade as a pass.
#
# The minimal fix makes READ COMMITTED STRICTER than a heap: the loser gets a
# retryable serialization_failure instead of a transparent EvalPlanQual re-apply.
# So at RC the columnar final VALUE is not compared to the heap oracle (only the
# no-duplicate row count and the loser's SQLSTATE are). At REPEATABLE READ the
# fix makes the two engines match fully. Whether retryable-40001-at-RC is an
# acceptable close of #5, or #5 stays open until a heap-transparent EvalPlanQual
# path exists, is a maintainer decision this suite does not prejudge.
#
# SCOPE OF THE DEFECT, as MEASURED here (not assumed): the lost update reproduces
# at READ COMMITTED and REPEATABLE READ. SERIALIZABLE is ALREADY safe on today's
# code -- the losing writer gets a 40001 with no fix, because the delete-vector
# read-modify-write conflict (or SSI) fires at that level. So the SERIALIZABLE arm
# is a REGRESSION guard, GREEN today and expected to stay GREEN, not part of the
# RED. The fix's job is to bring RC and RR up to the safety SERIALIZABLE already
# has.
#
# Written fresh for pgColumnar; it reuses no upstream test file or expected
# output. Derived from the format/interface spec, the issue #5 design analysis,
# and the public PostgreSQL API.
#
# Usage:
#   test/update_conc.sh [PG_CONFIG]
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
# THE NAME IS FIXED PER WAIT KIND, with the session and sentinel in pgc_record's
# REASON field. The ledger is keyed on (suite, part, name), so interpolating
# "$name/$label" into the name would mint rows nobody can enumerate and therefore
# nobody can seed.
#
# NO GREEN RUN EXECUTES THIS. It records only on the timeout path, so a passing
# suite emits nothing here and the record stream is unchanged.
wait_timeout() {	# wait_timeout FIXED-NAME DETAIL
	pgc_record FAIL "$1" "FAIL  $1 (timed out waiting for $2)" "$2"
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

WORKDIR="$(mktemp -d /tmp/pgcolumnar-upconc.XXXXXX)"
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
# The cluster listens on a unix socket only (listen_addresses='' below), so the
# number never binds a TCP port; it is really just the socket-file name. Drawn
# from portlib's band regardless, so the choice is safe by intent, not accident.
pick_port() {
	local p i
	for i in $(seq 1 100); do
		p=$(( PGC_PORT_LO + RANDOM % (PGC_PORT_HI - PGC_PORT_LO) ))
		if port_is_free "$p"; then echo "$p"; return 0; fi
	done
	echo $(( PGC_PORT_LO + RANDOM % (PGC_PORT_HI - PGC_PORT_LO) ))
}
PORT="${PGC_PORT:-$(pick_port)}"

echo "== pgColumnar concurrent same-row UPDATE test (issue #5) =="
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
run_pg "echo \"lock_timeout=30000\" >> '$PGDATA/postgresql.conf'"
echo "-- start"
run_pg "pg_ctl -D '$PGDATA' -l '$LOGFILE' start -w" >/dev/null
run_pg "createdb -h '$WORKDIR' -p $PORT upconc"

PSQL="psql -h '$WORKDIR' -p $PORT -d upconc -qAtX -v ON_ERROR_STOP=1"
SPSQL="psql -h '$WORKDIR' -p $PORT -d upconc -qAtX"
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
check() {  # name got want
	if [ "$2" = "$3" ]; then
		pgc_record PASS "$1" "PASS  $1: $2"
	else
		pgc_record FAIL "$1" "FAIL  $1: got [$2] want [$3]"
		fail=1
	fi
}

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
wait_blocked() {  # application_name
	local app="$1" i=0 n
	while :; do
		n="$(ctl_q "SELECT count(*) FROM pg_stat_activity WHERE application_name='$app' AND wait_event_type='Lock';")"
		[ "$n" = "1" ] && return 0
		sleep 0.05; i=$((i + 1))
		if [ "$i" -ge 1200 ]; then
			wait_timeout "a bounded wait for a session to block completed" "$app"; return 1
		fi
	done
}

# psql sets :SQLSTATE after each command. We echo a per-arm-unique marker
# followed by that variable, so each arm's outcome is read from its OWN marker
# (the session out file accumulates every arm). '00000' is success.
ss_of() {  # session marker -> the 5-char SQLSTATE of that arm's last command
	grep -oE "$2 [0-9A-Z]{5}" "$WORKDIR/$1.out" | tail -1 | sed "s/^$2 //"
}

ctl_q "CREATE EXTENSION pgcolumnar;" >/dev/null
ctl_q "CREATE TABLE t_heap (id int, v int);" >/dev/null
ctl_q "CREATE TABLE t_col  (id int, v int) USING pgcolumnar;" >/dev/null

seed1() { ctl_q "TRUNCATE $1;" >/dev/null; ctl_q "INSERT INTO $1 VALUES (1,0);" >/dev/null; }
seedN() { ctl_q "TRUNCATE $1;" >/dev/null; ctl_q "INSERT INTO $1 SELECT g,0 FROM generate_series(1,$2) g;" >/dev/null; }
cnt()  { ctl_q "SELECT count(*) FROM $1 WHERE id=1;"; }
vals() { ctl_q "SELECT coalesce(string_agg(v::text, ',' ORDER BY v),'') FROM $1 WHERE id=1;"; }

start_session s1
start_session s2
send s1 "SET application_name='cc_s1';"
send s2 "SET application_name='cc_s2';"

# ---------------------------------------------------------------------------
# uu <tbl> <iso> <marker> : update-vs-update. s1 sets v=10, s2 sets v=20, both
# on id=1 under isolation <iso>, forced to overlap. Distinct constants (10/20,
# never v+1) keep count, value-set and SQLSTATE from coinciding across the
# duplicated and the composed states. Leaves RES_CNT/RES_VALS/RES_SS set.
# ---------------------------------------------------------------------------
uu() {  # tbl iso marker
	local tbl="$1" iso="$2" m="$3"
	seed1 "$tbl"
	send_wait s1 "${m}b1" "BEGIN ISOLATION LEVEL $iso;"
	send_wait s2 "${m}b2" "BEGIN ISOLATION LEVEL $iso;"
	send_wait s1 "${m}u1" "UPDATE $tbl SET v=10 WHERE id=1;"
	send s2 "UPDATE $tbl SET v=20 WHERE id=1;"
	send s2 "\\echo ${m}SS :SQLSTATE"
	send s2 "\\echo <<${m}u2>>"
	wait_blocked cc_s2 || true
	send_wait s1 "${m}c1" "COMMIT;"
	wait_sentinel s2 "${m}u2"
	send_wait s2 "${m}c2" "COMMIT;"
	RES_CNT="$(cnt "$tbl")"; RES_VALS="$(vals "$tbl")"; RES_SS="$(ss_of s2 "${m}SS")"
}

echo; echo "### update-vs-update, READ COMMITTED"
uu t_heap "READ COMMITTED" uurc_h; H_CNT=$RES_CNT; H_VALS=$RES_VALS; H_SS=$RES_SS
uu t_col  "READ COMMITTED" uurc_c; C_CNT=$RES_CNT; C_VALS=$RES_VALS; C_SS=$RES_SS
echo "-- heap: count=$H_CNT vals={$H_VALS} sqlstate=$H_SS ; col: count=$C_CNT vals={$C_VALS} sqlstate=$C_SS"
check "uu/RC heap oracle composes both updates (one row)" "$H_CNT" "1"
check "uu/RC columnar keeps a single row (no duplicate)"  "$C_CNT" "1"
check "uu/RC columnar loser gets serialization_failure"  "$C_SS"  "40001"
# RC value is intentionally NOT compared to heap: the minimal fix makes the loser
# retry (col ends {10}) where a heap re-applies transparently (heap ends {20}).

echo; echo "### update-vs-update, REPEATABLE READ"
uu t_heap "REPEATABLE READ" uurr_h; H_CNT=$RES_CNT; H_VALS=$RES_VALS; H_SS=$RES_SS
uu t_col  "REPEATABLE READ" uurr_c; C_CNT=$RES_CNT; C_VALS=$RES_VALS; C_SS=$RES_SS
echo "-- heap: count=$H_CNT vals={$H_VALS} sqlstate=$H_SS ; col: count=$C_CNT vals={$C_VALS} sqlstate=$C_SS"
check "uu/RR heap oracle: loser serialization_failure"   "$H_SS"  "40001"
check "uu/RR columnar count matches heap oracle"         "$C_CNT" "$H_CNT"
check "uu/RR columnar value-set matches heap oracle"     "$C_VALS" "$H_VALS"
check "uu/RR columnar loser gets serialization_failure"  "$C_SS"  "40001"

echo; echo "### update-vs-update, SERIALIZABLE"
uu t_heap "SERIALIZABLE" uus_h; H_CNT=$RES_CNT; H_VALS=$RES_VALS; H_SS=$RES_SS
uu t_col  "SERIALIZABLE" uus_c; C_CNT=$RES_CNT; C_VALS=$RES_VALS; C_SS=$RES_SS
echo "-- heap: count=$H_CNT vals={$H_VALS} sqlstate=$H_SS ; col: count=$C_CNT vals={$C_VALS} sqlstate=$C_SS"
check "uu/SER columnar count matches heap oracle"        "$C_CNT" "$H_CNT"
check "uu/SER columnar loser gets serialization_failure" "$C_SS"  "40001"

# ---------------------------------------------------------------------------
# update-vs-delete, RC: s1 UPDATEs id=1, s2 DELETEs id=1. A heap deletes the live
# (updated) version -> count(id=1)=0. Columnar today: s2 sees the pre-update row,
# marks it deleted (idempotent), s1's new appended version survives -> count=1,
# the DELETE is lost. Post-fix the loser gets 40001. The distinguishing signal at
# RC is the SQLSTATE (count stays 1 whether the delete is lost or the loser
# aborts), so the crisp assertion is on SQLSTATE.
# ---------------------------------------------------------------------------
ud() {  # tbl marker
	local tbl="$1" m="$2"
	seed1 "$tbl"
	send_wait s1 "${m}b1" "BEGIN ISOLATION LEVEL READ COMMITTED;"
	send_wait s2 "${m}b2" "BEGIN ISOLATION LEVEL READ COMMITTED;"
	send_wait s1 "${m}u1" "UPDATE $tbl SET v=10 WHERE id=1;"
	send s2 "DELETE FROM $tbl WHERE id=1;"
	send s2 "\\echo ${m}SS :SQLSTATE"
	send s2 "\\echo <<${m}d2>>"
	wait_blocked cc_s2 || true
	send_wait s1 "${m}c1" "COMMIT;"
	wait_sentinel s2 "${m}d2"
	send_wait s2 "${m}c2" "COMMIT;"
	RES_CNT="$(cnt "$tbl")"; RES_SS="$(ss_of s2 "${m}SS")"
}
echo; echo "### update-vs-delete, READ COMMITTED"
ud t_heap udrc_h; H_CNT=$RES_CNT; H_SS=$RES_SS
ud t_col  udrc_c; C_CNT=$RES_CNT; C_SS=$RES_SS
echo "-- heap: count=$H_CNT sqlstate=$H_SS ; col: count=$C_CNT sqlstate=$C_SS"
check "ud/RC heap oracle: delete applies to the live row (count 0)" "$H_CNT" "0"
check "ud/RC columnar loser gets serialization_failure"             "$C_SS"  "40001"

# ---------------------------------------------------------------------------
# delete-vs-update, RC: s1 DELETEs id=1, s2 UPDATEs id=1. Heap: the row is gone,
# s2 updates nothing -> count 0. Columnar today: s2 re-inserts a successor to a
# row s1 committed-deleted -> count 1, a committed DELETE silently undone. Here
# the fix ALSO restores final-state equality: the loser aborts (40001), the delete
# stays, count 0 == heap. So both count and SQLSTATE are asserted.
# ---------------------------------------------------------------------------
du() {  # tbl marker
	local tbl="$1" m="$2"
	seed1 "$tbl"
	send_wait s1 "${m}b1" "BEGIN ISOLATION LEVEL READ COMMITTED;"
	send_wait s2 "${m}b2" "BEGIN ISOLATION LEVEL READ COMMITTED;"
	send_wait s1 "${m}d1" "DELETE FROM $tbl WHERE id=1;"
	send s2 "UPDATE $tbl SET v=20 WHERE id=1;"
	send s2 "\\echo ${m}SS :SQLSTATE"
	send s2 "\\echo <<${m}u2>>"
	wait_blocked cc_s2 || true
	send_wait s1 "${m}c1" "COMMIT;"
	wait_sentinel s2 "${m}u2"
	send_wait s2 "${m}c2" "COMMIT;"
	RES_CNT="$(cnt "$tbl")"; RES_SS="$(ss_of s2 "${m}SS")"
}
echo; echo "### delete-vs-update, READ COMMITTED"
du t_heap durc_h; H_CNT=$RES_CNT; H_SS=$RES_SS
du t_col  durc_c; C_CNT=$RES_CNT; C_SS=$RES_SS
echo "-- heap: count=$H_CNT sqlstate=$H_SS ; col: count=$C_CNT sqlstate=$C_SS"
check "du/RC heap oracle: committed delete stands (count 0)" "$H_CNT" "0"
check "du/RC columnar count matches heap oracle"             "$C_CNT" "$H_CNT"
check "du/RC columnar loser gets serialization_failure"      "$C_SS"  "40001"

# ---------------------------------------------------------------------------
# self-update in ONE statement (no concurrency, fully deterministic): UPDATE .. FROM
# (VALUES(1),(1)) matches id=1 twice. A heap raises "tuple to be updated was
# already modified by an operation triggered by the current command" and leaves
# one row. Columnar today applies both -> two rows. Post-fix the AM returns
# TM_SelfModified and core raises the same error. The crisp RED is the row count.
# ---------------------------------------------------------------------------
self_update() {  # tbl
	local tbl="$1"
	seed1 "$tbl"
	ctl_q "UPDATE $tbl t SET v=v+1 FROM (VALUES (1),(1)) s(id) WHERE t.id=s.id;" >/dev/null 2>&1 || true
	echo "$(cnt "$tbl")"
}
echo; echo "### self-update in one statement"
H_CNT="$(self_update t_heap)"; C_CNT="$(self_update t_col)"
echo "-- heap: count=$H_CNT ; col: count=$C_CNT"
check "self-update heap oracle keeps one row" "$H_CNT" "1"
check "self-update columnar keeps one row (no self-duplicate)" "$C_CNT" "1"

# ---------------------------------------------------------------------------
# multi-row overlapping ranges, RC: s1 updates id 1..120, s2 updates id 80..200
# (overlap 80..120 = 41 rows). Heap total stays 200. Columnar today appends 41
# duplicates -> 241. Post-fix the loser aborts (40001), leaving 200. Both the
# total row count and the SQLSTATE are asserted.
# ---------------------------------------------------------------------------
multirow() {  # tbl marker
	local tbl="$1" m="$2"
	seedN "$tbl" 200
	send_wait s1 "${m}b1" "BEGIN ISOLATION LEVEL READ COMMITTED;"
	send_wait s2 "${m}b2" "BEGIN ISOLATION LEVEL READ COMMITTED;"
	send_wait s1 "${m}u1" "UPDATE $tbl SET v=v+1 WHERE id BETWEEN 1 AND 120;"
	send s2 "UPDATE $tbl SET v=v+1 WHERE id BETWEEN 80 AND 200;"
	send s2 "\\echo ${m}SS :SQLSTATE"
	send s2 "\\echo <<${m}u2>>"
	wait_blocked cc_s2 || true
	send_wait s1 "${m}c1" "COMMIT;"
	wait_sentinel s2 "${m}u2"
	send_wait s2 "${m}c2" "COMMIT;"
	RES_TOT="$(ctl_q "SELECT count(*) FROM $tbl;")"; RES_SS="$(ss_of s2 "${m}SS")"
}
echo; echo "### multi-row overlapping updates, READ COMMITTED"
multirow t_heap mrrc_h; H_TOT=$RES_TOT; H_SS=$RES_SS
multirow t_col  mrrc_c; C_TOT=$RES_TOT; C_SS=$RES_SS
echo "-- heap: total=$H_TOT sqlstate=$H_SS ; col: total=$C_TOT sqlstate=$C_SS"
check "multirow heap oracle total unchanged (200)" "$H_TOT" "200"
check "multirow columnar total matches heap (no per-row duplicates)" "$C_TOT" "$H_TOT"
check "multirow columnar loser gets serialization_failure" "$C_SS" "40001"

# ---------------------------------------------------------------------------
# CONTROL (removal proof): the SAME two columnar updates run NON-overlapping in
# time -- s1 commits fully before s2 begins. No barrier. This MUST stay a single
# row on today's code, proving the columnar update mechanism is itself correct and
# the RED above is caused specifically by the forced overlap. If this showed 2 the
# suite would be measuring an always-broken update, not a concurrency bug.
# ---------------------------------------------------------------------------
echo; echo "### CONTROL: serial (non-overlapping) columnar updates"
seed1 t_col
send_wait s1 ser_b1 "BEGIN;"; send_wait s1 ser_u1 "UPDATE t_col SET v=10 WHERE id=1;"; send_wait s1 ser_c1 "COMMIT;"
send_wait s2 ser_b2 "BEGIN;"; send_wait s2 ser_u2 "UPDATE t_col SET v=20 WHERE id=1;"; send_wait s2 ser_c2 "COMMIT;"
echo "-- col serial: count=$(cnt t_col) vals={$(vals t_col)}"
check "CONTROL serial columnar keeps one row (GREEN today; proves the RED is the overlap)" "$(cnt t_col)" "1"
check "CONTROL serial columnar keeps the last writer's value" "$(vals t_col)" "20"

# ---------------------------------------------------------------------------
# A covering UNIQUE(id) index. The loser must get the same retryable
# serialization_failure (40001) as without one, as docs/limitations.md promises
# ("needs no unique index"). It used to get unique_violation (23505): the new
# version's unique-key lock waited on the winner's identical key, and the unique
# check fired before the flush that raises 40001 (#1421). A heap composes with no
# error. The primary fixture above still has no unique index, so this arm and that
# one exercise the two paths separately.
# ---------------------------------------------------------------------------
echo; echo "### a covering UNIQUE(id): the loser still gets 40001 (#1421)"
ctl_q "CREATE TABLE t_colu (id int, v int) USING pgcolumnar;" >/dev/null
ctl_q "CREATE UNIQUE INDEX t_colu_uidx ON t_colu (id);" >/dev/null
ctl_q "CREATE TABLE t_heapu (id int, v int);" >/dev/null
ctl_q "CREATE UNIQUE INDEX t_heapu_uidx ON t_heapu (id);" >/dev/null
uu t_heapu "READ COMMITTED" uxh_; H_CNT=$RES_CNT; H_SS=$RES_SS
uu t_colu  "READ COMMITTED" uxc_; C_CNT=$RES_CNT; C_SS=$RES_SS
echo "-- heap+uniq: count=$H_CNT sqlstate=$H_SS ; col+uniq: count=$C_CNT sqlstate=$C_SS"
check "unique/RC heap composes with no error (sqlstate 00000)" "$H_SS" "00000"
check "unique/RC columnar loser gets serialization_failure (40001), as without a unique index" \
	"$C_SS|$C_CNT" "40001|1"

# ---------------------------------------------------------------------------
# savepoint: a subtransaction that updates a row then rolls back must discard both
# the delete mark and the appended new version, and must not wedge on the
# transaction-scoped row lock. Single session, deterministic. Final state must
# match the heap oracle: the pre-savepoint value survives, the rolled-back one does
# not, and the row is not duplicated.
# ---------------------------------------------------------------------------
sp_arm() {  # tbl -> "count/values" for id=1 after a savepoint rollback
	seed1 "$1"
	ctl_q "BEGIN; UPDATE $1 SET v=10 WHERE id=1; SAVEPOINT sp; UPDATE $1 SET v=99 WHERE id=1; ROLLBACK TO sp; COMMIT;" >/dev/null 2>&1
	ctl_q "SELECT count(*)||'/'||coalesce(string_agg(v::text, ',' ORDER BY v),'') FROM $1 WHERE id=1;"
}
echo; echo "### savepoint: subxact rollback discards the update, no wedge"
H_SP="$(sp_arm t_heap)"; C_SP="$(sp_arm t_col)"
echo "-- heap=$H_SP ; col=$C_SP"
check "savepoint columnar final state matches heap oracle" "$C_SP" "$H_SP"
check "savepoint columnar kept the pre-savepoint value, no duplicate (1/10)" "$C_SP" "1/10"

send s1 "\\q"
send s2 "\\q"

echo
if [ "$fail" = 0 ]; then
	echo "checks run: $PGC_CHECKS"
	echo "UPDATE CONCURRENCY TEST PASSED"
else
	echo "checks run: $PGC_CHECKS"
	echo "UPDATE CONCURRENCY TEST FAILED"
fi
exit "$fail"
