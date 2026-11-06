# ---- a suite that declares accounting must produce it on every exit (#1233) --
#
# Every one of the 252 suites that calls pgc_setup also calls pgc_summary, so
# every one of them DECLARES accounting. pgc_setup has EIGHT exit paths and
# calls pgc_summary on none of them, so on any of those eight a declared suite
# exited without producing the line it promised.
#
# NOT A FALSE PASS: rc is 1 and the FATAL is correct. What was lost is the
# machine-readable line, and six readers consume it -- run_all_versions.sh's
# reconciliation, pgc_vacuity.py, test_check_records.py,
# test_residual_is_counted.py, test_suite_accounting.py and smoke.sh. To every
# one of them a declared suite with no accounting is indistinguishable from a
# suite that died mid-run. run_all_versions.sh already names the case in the
# reconciliation's own comment, so the comment is older than the defect.
#
# THE REPAIR IS THE TRAP, WHICH IS ONE SITE RATHER THAN EIGHT, and these arms
# are about that choice as much as about the output: a per-exit patch cannot
# cover the ninth exit, and the trap covers it by construction.

_da_lib="$PGC_TESTDIR/lib.sh"

check_num "lib.sh is where the exits and the trap both live" \
	"$(grep -c '^pgc_on_exit()' "$_da_lib")" "1"

# THE POPULATION, DERIVED RATHER THAN ASSERTED. Both halves matter: the number
# of exits the trap has to cover, and that no OTHER helper exits, because a
# helper that exits without reaching the summary would be mislabelled.
# ANCHORED, because the first version of this arm counted NINE. `grep -c 'exit '`
# matches the substring in `trap pgc_on_exit EXIT` -- the very line this change
# adds -- so the guard's own code satisfied its pattern and the count moved by
# one for a reason that is not an exit. That is #1222 and #1227 one more time,
# in an arm written by the person who fixed them.
_da_setup_exits="$(sed -n '/^pgc_setup()/,/^}/p' "$_da_lib" | sed 's/#.*//' |
	grep -cE '(^|[^_A-Za-z0-9])exit( |$)')"
check_num "premise: pgc_setup still has the eight exits this covers" \
	"$_da_setup_exits" "8"
check_num "and pgc_setup still calls pgc_summary on none of them" \
	"$(sed -n '/^pgc_setup()/,/^}/p' "$_da_lib" | sed 's/#.*//' | grep -c 'pgc_summary')" "0"

# THE TRAP IS WIRED TO THE WRAPPER, not to the bare teardown. Asserting the
# function exists is not asserting anything calls it.
check_num "the EXIT trap runs the wrapper, so every exit reaches the accounting" \
	"$(sed 's/#.*//' "$_da_lib" | grep -c 'trap pgc_on_exit EXIT')" "1"
check_num "and nothing still arms the bare teardown" \
	"$(sed 's/#.*//' "$_da_lib" | grep -c 'trap pgc_teardown EXIT')" "0"

# THE STATUS MUST SURVIVE THE TRAP. A trap that runs commands overwrites `$?`,
# so the wrapper has to capture it on its first line; a FATAL is rc=1 and must
# stay rc=1. Read from the function's own text rather than assumed.
_da_first="$(sed -n '/^pgc_on_exit()/,/^}/p' "$_da_lib" | sed 's/#.*//' | grep -vE '^\s*$' | sed -n '2p' | tr -d '\t ')"
check_text "the wrapper captures the exit status before anything overwrites it" \
	"$_da_first" 'local_rc=$?'

# DRIVEN, not read: the emitter is evalled out of lib.sh and exercised on both
# branches. Per selftest 320, a part that recomputes a rule tests the world
# instead of the code.
# SAVE AND RESTORE THE SUITE'S OWN COUNTERS, AROUND EACH DRIVING BLOCK.
#
# A selftest part is SOURCED, so an assignment here clobbers the live counters
# of the run it is part of. The first version set PGC_CHECKS=7 and left them
# set: the selftest's final summary read `checks run: 10 ... 1 unrunnable` and
# `INCOMPLETE` on a run that had executed over eleven hundred checks, and it
# exited 67. Every arm in this part passed while it happened. An implausible
# TOTAL was the only tell, and `pgc_reconcile_records` in run_all_versions.sh
# computes exactly that -- it did not fire because a bare
# `bash test/harness_selftest.sh` never reaches it, which is how both sessions
# drive suites while developing.
#
# SAVED IMMEDIATELY BEFORE EACH BLOCK, not once at the top. A single snapshot
# taken at the start and restored later would wipe the counts of every arm that
# ran in between -- a lossy restore that looks like a careful one. The second
# version did that and the assertion below caught it: got 1145 want 1144.
_da_snap() { printf '%s|%s|%s|%s|%s|%s' "$PGC_CHECKS" "$PGC_PASSED" "$PGC_FAILED" \
	"$PGC_UNRUN" "$PGC_SKIPPED" "${PGC_SUMMARY_PRINTED:-0}"; }
_da_put() {
	IFS='|' read -r PGC_CHECKS PGC_PASSED PGC_FAILED PGC_UNRUN PGC_SKIPPED PGC_SUMMARY_PRINTED <<-EOF
	$1
	EOF
}

_da_sv="$(_da_snap)"
PGC_SUMMARY_PRINTED=0 PGC_CHECKS=7 PGC_PASSED=5 PGC_FAILED=1 PGC_UNRUN=1 PGC_SKIPPED=0
_da_out="$(pgc_exit_accounting 2>&1)"
_da_put "$_da_sv"
_da_ok1="$([ "$(_da_snap)" = "$_da_sv" ] && echo yes || echo no)"

check_num "a suite that never reached its summary is given the accounting it owes" \
	"$(printf '%s\n' "$_da_out" | grep -cE '^accounting: 5 passed \+ 1 failed \+ 1 unrunnable \+ 0 skipped = 7$')" "1"
check_num "and it is marked TERMINATED, so a reader can tell it from a full run" \
	"$(printf '%s\n' "$_da_out" | grep -c 'TERMINATED before its summary')" "1"

# THE OTHER BRANCH, which is the one that stops a normal run printing twice.
_da_sv="$(_da_snap)"
PGC_SUMMARY_PRINTED=1
_da_twice="$(pgc_exit_accounting 2>&1 | grep -c .)"
_da_put "$_da_sv"
_da_ok2="$([ "$(_da_snap)" = "$_da_sv" ] && echo yes || echo no)"
check_num "a suite that DID reach its summary is not given a second one" \
	"$_da_twice" "0"

# THE SHAPE THE READERS MATCH. pgc_log_shows_accounting anchors on this exact
# line; a differently-shaped one would leave the reconciliation still blind, so
# the emitted line is checked against the reader's own pattern rather than
# against a copy of it.
_da_rv="$PGC_TESTDIR/run_all_versions.sh"
check_num "premise: the reader that consumes this line is where it was" \
	"$(grep -c '^pgc_log_shows_accounting()' "$_da_rv")" "1"
_da_pat="$(sed -n '/^pgc_log_shows_accounting()/,/^}/p' "$_da_rv" | grep -oE "'\^accounting:[^']*'" | tr -d "'")"
check_num "premise: the reader's own pattern was extracted, not guessed" \
	"$(printf '%s' "$_da_pat" | grep -c .)" "1"
_da_sv="$(_da_snap)"
PGC_SUMMARY_PRINTED=0 PGC_CHECKS=7 PGC_PASSED=5 PGC_FAILED=1 PGC_UNRUN=1 PGC_SKIPPED=0
_da_shape="$(pgc_exit_accounting 2>&1 | grep -cE "$_da_pat")"
_da_put "$_da_sv"
_da_ok3="$([ "$(_da_snap)" = "$_da_sv" ] && echo yes || echo no)"
check_num "the line it emits is the line the reconciliation reads" \
	"$_da_shape" "1"

# AND WHAT A READER CONCLUDES FROM IT, which is the gap this change came through
# (@OffgridwithJD, review). The arms above assert the line is EMITTED and that
# the reader's pattern MATCHES it. Neither asserts what anyone DECIDES from it,
# and the first version of this change collapsed three states into one verdict
# for every consumer while every arm here stayed green.
#
# Before the trap emitted accounting, the presence of that line meant "this
# suite reached its summary", because pgc_summary was the only thing that
# printed it. `yes` keeps that meaning; `terminated` is the new state and gets
# its own value rather than being folded into either neighbour. Folding it into
# `yes` loses the mid-run death -- a `set -e` abort RUNS the EXIT trap, so only
# SIGKILL leaves no accounting at all. Folding it into `no` restores the false
# "declared but never accounted" the trap was added to remove.
#
# The reader is EVALLED out of run_all_versions.sh rather than restated, per
# selftest 320.
_da_rd="$(sed -n '/^pgc_log_shows_accounting()/,/^}/p' "$_da_rv")"
eval "$_da_rd"
check_text "premise: the reader is callable once evalled" \
	"$(type -t pgc_log_shows_accounting)" "function"

_da_tmp="$(mktemp -d)"
_da_mk() {	# _da_mk FILE TAIL
	printf 'checks run: 3\naccounting: 2 passed + 1 failed + 0 unrunnable + 0 skipped = 3\n%s\n' "$2" > "$1"
}
_da_mk "$_da_tmp/done.log" "x.sh: FAILED"
_da_mk "$_da_tmp/term.log" "x.sh: TERMINATED before its summary"
printf 'checks run: 3\n' > "$_da_tmp/none.log"

check_text "a completed suite still reads as having reached its summary" \
	"$(pgc_log_shows_accounting "$_da_tmp/done.log")" "yes"
check_text "a terminated suite reads as terminated, not as a completed one" \
	"$(pgc_log_shows_accounting "$_da_tmp/term.log")" "terminated"
check_text "and a log with no accounting still reads as none" \
	"$(pgc_log_shows_accounting "$_da_tmp/none.log")" "no"
rm -rf "$_da_tmp"

# AND THE COUNTERS THIS PART BORROWED ARE BACK AFTER EVERY BLOCK, asserted
# rather than assumed. The restore is a line nobody checks; this is the line
# that fails when a fourth driving block forgets one. Each verdict is frozen at
# the moment of its own restore, because a comparison made later would include
# the arms that ran in between.
check_text "the suite's own counters are restored after every driving block" \
	"$_da_ok1 $_da_ok2 $_da_ok3" "yes yes yes"

# AND THE FALSE-POSITIVE SURFACE, MEASURED RATHER THAN ASSERTED EMPTY.
#
# THE FIRST VERSION OF THIS ARM CLAIMED NO DECLARED SUITE HAS A BARE EXIT AND
# WANTED 0. That was false: there are about thirty such sites.
#
# IT READ 0 BECAUSE THE `grep` THAT COUNTED IT WAS NOT GNU grep. On the author's
# host `grep` is a shell FUNCTION wrapping another tool, and it returns 0 for
# `(^|[^_[:alnum:]])exit( |$)` where `/usr/bin/grep` returns 3 on the same input
# and 2 on test/smoke.sh. The pattern is correct. The instrument was not, and
# the first diagnosis written here blamed GNU ERE semantics for a defect that
# does not exist -- which would have sent the next reader to the regex instead
# of to their own shell. Corrected by @OffgridwithJD, who measured three greps
# against one input rather than accepting the explanation.
#
# Suites are unaffected: they run in the container against GNU grep 3.12. This
# bites only measurements made by hand, which is where the claims get made.
# The container's own run of this arm said 30 and was dismissed as litter; the
# container was right.
#
# WHAT IS ACTUALLY TRUE, and it is the property that matters: every bare exit in
# a declared suite is either an ERROR path, which SHOULD be marked TERMINATED,
# or is preceded by pgc_summary, in which case the flag above suppresses the
# second summary. The dangerous case is a DELIBERATE clean exit with no summary,
# because that is correct behaviour this would mislabel.
#
# There are FOUR `exit 0` sites in declared suites and every one reaches its
# summary first, by one route or the other:
#
#     isolation           pgc_summary on the previous line
#     objstore_module     pgc_summary on the previous line
#     temporal            pgc_summary on the previous line
#     logical_subscriber  pgc_summary on the SAME line
#
# Both routes are checked, which is why the arm looks at the same line as well
# as the one above it. A first count said three and missed logical_subscriber,
# for the same reason as everything else in this comment: it was taken with the
# wrapper rather than with GNU grep.
#
# This is what would break if a fifth were added without a summary.
_da_e0=0
_da_e0_bad=0
for _da_f in "$PGC_TESTDIR"/*.sh; do
	[ "$(basename "$_da_f")" = "lib.sh" ] && continue
	# grep -c, NEVER grep -q, downstream of a pipe: grep -q exits at its first
	# match and closes the pipe, sed takes EPIPE, and under pipefail the
	# PIPELINE reports failure although grep MATCHED. Selftest 040 and
	# run_all_versions.sh both carry this story from #473 and #476, and the
	# harness's own arm caught this part committing it.
	[ "$(sed 's/#.*//' "$_da_f" | grep -cE '(^|[^_A-Za-z0-9])pgc_setup[^_A-Za-z0-9]')" -gt 0 ] || continue
	while IFS= read -r _da_l; do
		[ -n "$_da_l" ] || continue
		_da_e0=$((_da_e0 + 1))
		_da_n="${_da_l%%:*}"
		case "$_da_l" in *pgc_summary*) continue;; esac
		[ "$(sed 's/#.*//' "$_da_f" | sed -n "$((_da_n - 1))p" |
			grep -c 'pgc_summary')" -gt 0 ] ||
			_da_e0_bad=$((_da_e0_bad + 1))
	done <<-EOF
	$(sed 's/#.*//' "$_da_f" | grep -nE '(^|[;&|][[:space:]]*|^[[:space:]]+)exit[[:space:]]+0[[:space:]]*$' | grep -v "'")
	EOF
done
check_text "premise: declared suites do have clean exits of their own to check" \
	"$(if [ "$_da_e0" -ge 4 ]; then echo yes; else echo "no ($_da_e0, want >= 4)"; fi)" "yes"
check_num "every clean exit in a declared suite reaches its summary first" \
	"$_da_e0_bad" "0"
