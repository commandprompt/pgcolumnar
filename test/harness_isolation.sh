#!/usr/bin/env bash
#
# pgColumnar #1915: a run is not disturbed by other sessions on a shared box.
#
# Two hazards seen in the shared dev container, neither from a code change:
#
#   1. A postmaster started inside `flock <lock> cmd` held the lock until it
#      stopped. flock(1) passes its descriptor to the command, and every
#      process the command starts inherits it. A cluster that outlived the
#      command blocked every other run of that major (25 minutes, measured by
#      the reviewer of #1906 in /proc/<pid>/fd). portlib.sh, which lib.sh and
#      every suite with its own harness load, now closes every descriptor that
#      holds a lock another process took. The
#      flock process keeps its own descriptor, so the lock still serialises
#      runs; the suite's servers no longer hold it. A lock the suite's own
#      process took stays held.
#   2. The installed library was replaced by another session between install
#      and test. pgc_setup already refuses a library this tree did not install
#      (#959). It now also records the digest it verified, and pgc_summary
#      fails the run when the library changed after setup, naming both digests.
#
# The checks measure the real mechanism: a real postmaster restarted from a
# shell that loads lib.sh under flock(1), and a run summary over a library
# that changes under it. The shared installed library is never touched: the
# replacement is made on a copy, through a pg_config that points at the copy.
#
# Usage:  test/harness_isolation.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

HI_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
HI_LOCK="$PGC_WORKDIR/run.lock"
: > "$HI_LOCK"
chmod 666 "$HI_LOCK"

hi_fds_on() {	# hi_fds_on PID FILE -> "N of M": descriptors on FILE of all M
	local all on
	all="$(ls /proc/"$1"/fd 2>/dev/null | wc -l)"
	on="$(find /proc/"$1"/fd -maxdepth 1 -lname "$2" 2>/dev/null | wc -l)"
	echo "$on of $all"
}

hi_lock_free() {	# hi_lock_free FILE -> flock -n's status: 0 free, 1 held
	flock -n "$1" true
	echo $?
}

# ---- 1. no inherited lock reaches a server --------------------------------

# Control: the detector sees a leak where one exists. A plain shell under flock
# starts a long-lived child; that child inherits the descriptor.
hi_ctl_pid="$(flock "$HI_LOCK" bash -c 'sleep 60 >/dev/null 2>&1 & echo $!')"
check_text "control: a child of a plain shell under flock holds the lock" \
	"$(hi_fds_on "$hi_ctl_pid" "$HI_LOCK" | cut -d' ' -f1) held, lock free=$(hi_lock_free "$HI_LOCK")" \
	"1 held, lock free=1"
kill "$hi_ctl_pid" 2>/dev/null
wait "$hi_ctl_pid" 2>/dev/null

# Restart this suite's own cluster from a shell that loads lib.sh under flock,
# as a person or an agent does with `flock <lock> bash test/x.sh`.
pgc_pg "pg_ctl -D '$PGC_PGDATA' stop -m fast -w" >/dev/null 2>&1
cat > "$PGC_WORKDIR/start_under_lock.sh" <<EOF
. '$HI_LIB'
PGC_BINDIR='$PGC_BINDIR'
if [ "\$(id -u)" = 0 ]; then PGC_RUNPG=(runuser -u postgres --); else PGC_RUNPG=(env); fi
pgc_pg "pg_ctl -D '$PGC_PGDATA' -l '$PGC_LOGFILE' start -w" >/dev/null 2>&1
EOF
flock "$HI_LOCK" bash "$PGC_WORKDIR/start_under_lock.sh"
hi_pm="$(pgc_pg "head -1 '$PGC_PGDATA/postmaster.pid'" 2>/dev/null | tr -dc '0-9')"
hi_pm_fds="$(ls /proc/"${hi_pm:-0}"/fd 2>/dev/null | wc -l)"

check_num "premise: the postmaster outlives the flock command that started it" \
	"$(kill -0 "${hi_pm:-0}" 2>/dev/null && [ "$hi_pm_fds" -gt 0 ] && echo 1 || echo 0)" "1"
check_text "a postmaster started under a held lock holds no descriptor on it" \
	"$(hi_fds_on "${hi_pm:-0}" "$HI_LOCK")" "0 of $hi_pm_fds"
check_num "another run takes that lock while the postmaster runs" \
	"$(hi_lock_free "$HI_LOCK")" "0"

# Not over-closed: a lock the process took itself is its own, and lib.sh must
# not release it. flock -n from the same process uses a second open file, so it
# conflicts with a lock that is still held: 1 means still held.
check_num "a lock the harness's own process took stays held" \
	"$(bash -c "exec 8>'$PGC_WORKDIR/own.lock'; flock 8; . '$HI_LIB'; flock -n '$PGC_WORKDIR/own.lock' true; echo \$?")" "1"

# ---- 2. a library replaced during the run fails the run --------------------

check_text "setup records the digest of the library it verified" \
	"[${PGC_RUN_SO_DIGEST:-}]" "[$(pgc_installed_library_digest "$PGC_PG_CONFIG")]"

# A copy of the installed library, and a pg_config whose pkglibdir is the copy.
hi_lib="$PGC_WORKDIR/fakelib"
mkdir -p "$hi_lib"
cp "$("$PGC_PG_CONFIG" --pkglibdir)/pgcolumnar.so" "$hi_lib/pgcolumnar.so"
hi_cfg="$PGC_WORKDIR/fake_pg_config"
printf '#!/bin/sh\ncase "$1" in --pkglibdir) echo %s ;; *) exec %s "$@" ;; esac\n' \
	"'$hi_lib'" "'$PGC_PG_CONFIG'" > "$hi_cfg"
chmod 755 "$hi_cfg"
hi_before="$(pgc_installed_library_digest "$hi_cfg")"

check_text "an unchanged library is not reported" \
	"rc=$(pgc_check_library_unchanged "$hi_lib/pgcolumnar.so" "$hi_before" >/dev/null 2>&1; echo $?)" "rc=0"

printf 'x' >> "$hi_lib/pgcolumnar.so"
hi_after="$(pgc_installed_library_digest "$hi_cfg")"
hi_report="$(pgc_check_library_unchanged "$hi_lib/pgcolumnar.so" "$hi_before" 2>&1; echo "rc=$?")"
check_text "a library replaced after setup is reported with both digests" \
	"$(grep -c "$hi_before.*$hi_after" <<<"$hi_report") line(s), $(tail -1 <<<"$hi_report")" \
	"1 line(s), rc=1"

# The summary of a run whose library changed under it. The run counts one
# passing check and nothing else, so the failure can come only from the
# library check, and the measurement names that line too.
cp "$("$PGC_PG_CONFIG" --pkglibdir)/pgcolumnar.so" "$hi_lib/pgcolumnar.so"
cat > "$PGC_WORKDIR/changed_run.sh" <<EOF
. '$HI_LIB'
PGC_PG_CONFIG='$hi_cfg'
PGC_RUN_SO_DIGEST="\$(pgc_installed_library_digest '$hi_cfg')"
PGC_CHECKS=1; PGC_PASSED=1
printf 'x' >> '$hi_lib/pgcolumnar.so'
pgc_summary
EOF
hi_run="$(bash "$PGC_WORKDIR/changed_run.sh" 2>&1; echo "rc=$?")"
check_text "a run whose library changed mid-run ends failed" \
	"$(grep -cE '^changed_run\.sh: FAILED$' <<<"$hi_run") FAILED, $(grep -c 'installed library changed' <<<"$hi_run") library line, $(tail -1 <<<"$hi_run")" \
	"1 FAILED, 1 library line, rc=1"

pgc_summary
