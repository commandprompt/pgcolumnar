# ---- a check that could not run is a third state, not a pass ----------------
#
# WHY THIS EXISTS. Every check in this harness has two outcomes: it printed PASS
# or it printed FAIL. When a check's INPUT is absent -- a fixture that did not
# build, a capability the server lacks, a file the probe reads that is not there
# -- the check either passes vacuously or fails for a reason that has nothing to
# do with the property under test. Neither answer is true. The suite says
# "checks run: N" either way, and a reader counting greens counts one that never
# asked its question.
#
# That is the same defect as a permanent SKIP one level down: a skip you wrote is
# a check you did not write. `pgc_skip` already refuses to let a MISSING
# DEPENDENCY read as a pass -- it FAILS unless waived deliberately. This gives a
# single CHECK the same honesty at check granularity.
#
# THE STATE MUST NOT BE A PASS AT THE SUITE LEVEL EITHER, which is the whole
# point: a suite holding an unrunnable check exits PGC_EXIT_INCOMPLETE, not 0, so
# a runner cannot report it green. Failure still dominates -- a suite with both a
# FAIL and an UNRUN is FAILED, because the failure is the more urgent fact.
#
# WHY A NEW EXIT CODE AND NOT 66. 66 means "ran no checks". A suite with one
# unrunnable check DID run checks, and collapsing the two states would lose the
# distinction between "this suite is inert" and "this suite could not evaluate
# one thing". 67 is chosen on the same grounds 66 was: bash produces 1, 2, 126,
# 127 and 128+n; psql produces 1, 2, 3; make produces 1 and 2. And as with 66,
# the code alone is not trusted -- the runner must also see the INCOMPLETE line
# in the log, because `set -e` can propagate any status an aborting command
# returns.

_tsm_fmt_cnt() { [ "$1" -eq 0 ] && { echo "[]"; return; }; echo "[$1:$2]"; }

_cur_lib="$PGC_TESTDIR/lib.sh"

check "premise: the harness library is where this part thinks it is" \
	"$([ -f "$_cur_lib" ] && echo yes || echo no)" "yes"

check "lib.sh defines check_unrunnable" \
	"$(grep -c '^check_unrunnable()' "$_cur_lib")" "1"

check "lib.sh defines the INCOMPLETE exit status" \
	"$(grep -c '^PGC_EXIT_INCOMPLETE=' "$_cur_lib")" "1"

# ---- the four states, each run as its own suite ----------------------------
#
# A source-shape fixture: it sources lib.sh and calls the primitives without
# pgc_setup, so no cluster is started and the four arms cost nothing. Each arm
# differs from the others in exactly one respect, so a single behaviour is under
# test in each.

_cur_dir="$PGC_WORKDIR/unrun"; mkdir -p "$_cur_dir"

_cur_make() {	# _cur_make NAME BODY
	{
		printf '#!/usr/bin/env bash\n'
		printf '. "%s"\n' "$_cur_lib"
		printf '%s\n' "$2"
		printf 'pgc_summary\n'
	} > "$_cur_dir/$1.sh"
	chmod 755 "$_cur_dir/$1.sh"
}

_cur_run() {	# _cur_run NAME -> "<exit> <verdict-word>"
	local out rc
	out="$(bash "$_cur_dir/$1.sh" 2>&1)"; rc=$?
	printf '%s %s' "$rc" "$(printf '%s' "$out" | grep -oE '(PASSED|FAILED|INCOMPLETE|SKIPPED)' | tail -1)"
}

_cur_out() {	# _cur_out NAME -> the whole output
	bash "$_cur_dir/$1.sh" 2>&1
}

_cur_make onlypass 'check "a" ok ok'
_cur_make passunrun 'check "a" ok ok
check_unrunnable "b" ABSENT_FIXTURE "the parquet corpus was not built"'
_cur_make failunrun 'check "a" ok NOPE
check_unrunnable "b" ABSENT_FIXTURE "the parquet corpus was not built"'
_cur_make onlyunrun 'check_unrunnable "b" ABSENT_FIXTURE "the parquet corpus was not built"'

check "a suite whose checks all passed still exits 0 PASSED" \
	"$(_cur_run onlypass)" "0 PASSED"

check "one unrunnable check makes the suite INCOMPLETE, not passed" \
	"$(_cur_run passunrun)" "67 INCOMPLETE"

# This arm reads "1 FAILED" whether or not check_unrunnable exists, because the
# fixture also holds a real failure -- so the exit code ALONE cannot distinguish
# the feature from its absence. Assert the accounting line instead, which only a
# suite that recorded BOTH states can print.
check "a failure outranks an unrunnable check, and both are still counted" \
	"$(_cur_out failunrun | grep -oE 'accounting: [0-9]+ passed \+ [0-9]+ failed \+ [0-9]+ unrunnable \+ [0-9]+ skipped')" \
	"accounting: 0 passed + 1 failed + 1 unrunnable + 0 skipped"

# The distinction 66 cannot carry: this suite RAN a check. Reporting it as
# "ran no checks" would merge "inert suite" with "could not evaluate one thing".
check "a suite of nothing but unrunnable checks is INCOMPLETE, not SKIPPED" \
	"$(_cur_run onlyunrun)" "67 INCOMPLETE"

check "and it is not reported as having run no checks" \
	"$(_cur_out onlyunrun | grep -c 'ran no checks')" "0"

# The reason travels with the state. A third state that does not say why is a
# skip with better manners.
check "the unrunnable check names itself, its reason code and its detail" \
	"$(_cur_out passunrun | grep -c '^UNRUN  b: ABSENT_FIXTURE: the parquet corpus was not built')" "1"

# Counting: an unrunnable check is still a check that was reached, so it counts
# toward the total, and it is reported separately so the total can be split.
check "an unrunnable check counts toward checks run" \
	"$(_cur_out passunrun | grep -oE 'checks run: [0-9]+' | grep -oE '[0-9]+')" "2"

check "and the unrunnable ones are reported as their own count" \
	"$(_cur_out passunrun | grep -oE 'checks unrunnable: [0-9]+' | grep -oE '[0-9]+')" "1"

check "a suite with none says so as zero rather than staying silent" \
	"$(_cur_out onlypass | grep -c 'checks unrunnable: 0')" "1"

# The reason is an enum plus a detail, not prose. Phase 3 has to group these, and
# a free-text reason would mean rewriting every call site later. A code outside
# the set is a FAILURE rather than a silent acceptance, so the enum cannot rot on
# first use by someone inventing a code.
_cur_make badcode 'check_unrunnable "b" NOT_A_REAL_CODE "x"'
check "an unrunnable reason outside the enum fails rather than being accepted" \
	"$(_cur_run badcode)" "1 FAILED"

# Every state is in a total, or it is a state that can go missing. 3,762 check
# sites is well past what anyone notices by reading.
check "the summary reconciles the three states against the total" \
	"$(_cur_out passunrun | grep -oE 'accounting: [0-9]+ passed \+ [0-9]+ failed \+ [0-9]+ unrunnable \+ [0-9]+ skipped = [0-9]+')" \
	"accounting: 1 passed + 0 failed + 1 unrunnable + 0 skipped = 2"

check "and a suite with no unrunnable checks reconciles too" \
	"$(_cur_out onlypass | grep -oE 'accounting: [0-9]+ passed \+ [0-9]+ failed \+ [0-9]+ unrunnable \+ [0-9]+ skipped = [0-9]+')" \
	"accounting: 1 passed + 0 failed + 0 unrunnable + 0 skipped = 1"

# ---- the accounting must be a MEASUREMENT, not an identity ------------------
#
# The first version of this part derived the failed count as
# CHECKS - PASSED - UNRUN and called the result an accounting line. That identity
# is true for any values: a counter can drift arbitrarily and P + (N-P-U) + U = N
# still holds, so the only reachable red was a negative. It is shape 12 from the
# audit that produced this phase -- an accounting identity guaranteed by
# construction rather than measured -- and it shipped inside the diff that exists
# to catch shape 12.
#
# It was not hypothetical. check_ratio printed PASS and never touched PGC_PASSED,
# so every passing ratio check was counted as a failure in six shipped suites
# (column_projection, int8_agg_int128, native_fetch_bigcap, native_fetch_cache,
# objstore_http_read, planner_choice_quality -- twelve call sites, none in a
# subshell). The line meant to prove the states reconcile could not see it.
#
# Three counters are now maintained INDEPENDENTLY and reconciled against a
# fourth. A helper that forgets any one of them reddens here.

_cur_make ratio 'check "a" ok ok
check_ratio "a ratio well inside its bound" 10 100 1.0'

check "a passing ratio check is counted as a pass, not a failure" \
	"$(_cur_out ratio | grep -oE 'accounting: [0-9]+ passed \+ [0-9]+ failed \+ [0-9]+ unrunnable \+ [0-9]+ skipped = [0-9]+')" \
	"accounting: 2 passed + 0 failed + 0 unrunnable + 0 skipped = 2"

check "and the suite that holds it still passes" \
	"$(_cur_run ratio)" "0 PASSED"

# A drifting counter must be visible. Pass a check through a helper that counts
# the check but records neither outcome, which is exactly what check_ratio did.
#
# The forbidden line is ASSEMBLED rather than written, because the sweep below
# greps the tree for exactly this shape and a fixture that spells it out is
# indistinguishable from the defect. A test for a pattern must not contain the
# pattern -- the sweep found this fixture on its first run and was right to.
_cur_drift="$(printf 'PGC_%s=$((PGC_%s + 1))' CHECKS CHECKS)"
_cur_make drift "check \"a\" ok ok
$_cur_drift
echo \"PASS  a check nothing counted\""

check "a counter that drifts is caught rather than absorbed" \
	"$(_cur_out drift | grep -c '^FAIL  the summary does not reconcile')" "1"

check "and the suite holding it fails rather than reporting PASSED" \
	"$(_cur_run drift)" "1 FAILED"

# ---- the counters are lib.sh's invariant, and only lib.sh may write them -----
#
# The reconciliation above turns PGC_CHECKS into an invariant that pgc_summary
# checks. A suite that bumps it directly and prints its own PASS leaves the
# totals short, and the reconciliation then reds a HEALTHY tree -- worse than the
# miscount it exists to find. projections.sh did exactly that: an expect_fail()
# with ten call sites, counting checks whose outcome nothing recorded, invisible
# for as long as it existed because nothing reconciled the totals.
#
# Fixing those ten call sites alone would leave the next expect_fail someone
# writes undetectable, which is the same argument that rejected fixing
# check_ratio's counter without a real PGC_FAILED. So the rule is swept: a direct
# write to PGC_CHECKS must record an outcome on the same line or in the lines
# around it, and pgc_pass/pgc_fail exist so a suite-local helper need not.

_cnt_dir="$PGC_TESTDIR"
_cnt_sites=()
while IFS= read -r _cnt_l; do
	_cnt_sites+=("$_cnt_l")
done < <(grep -rn 'PGC_CHECKS=\$((PGC_CHECKS' "$_cnt_dir"/*.sh "$_cnt_dir"/selftest/*.sh 2>/dev/null \
	| grep -v '/lib\.sh:' | sort)

# The premise used to require FIVE direct writes to exist, which was a premise
# about the corpus rather than about the sweep -- and #917 converted the thirteen
# that used lib.sh's accounting, so it went red for the reason the change is FOR.
# A premise that fails when the thing it guards is fixed is the wrong premise.
#
# It now asserts the sweep read something, and the classifier is proven on a
# FIXTURE below rather than on whatever the corpus happens to contain.
#
# "Read something" is the FILES scanned, not the sites found (#1341). The same
# mistake one level down: requiring one site went red when bench_guards.sh, the
# last suite keeping its own tally, moved onto lib.sh's accounting -- zero sites
# is the state the rules below exist to reach. A sweep pointed at the wrong
# directory reads no files, which this still refuses.
_cnt_files="$(ls "$_cnt_dir"/*.sh "$_cnt_dir"/selftest/*.sh 2>/dev/null | grep -c .)"
check "premise: the sweep read the corpus" \
	"$([ "${_cnt_files:-0}" -ge 100 ] && echo yes || echo "no (${_cnt_files:-0} files)")" "yes"

# A file that keeps its OWN counters and never calls pgc_summary is not bound by
# this invariant, because nothing reconciles it. Asserted rather than assumed:
# the exemption is measured from the file, not from a name list.
_cnt_bad=""; _cnt_n=0
for _cnt_l in "${_cnt_sites[@]}"; do
	_cnt_f="${_cnt_l%%:*}"
	_cnt_ln="$(printf '%s' "$_cnt_l" | cut -d: -f2)"
	grep -q 'pgc_summary' "$_cnt_f" || continue
	# grep -c on a captured window, not a pipe into grep -q; see selftest 080.
	_cnt_win="$(sed -n "$((_cnt_ln > 3 ? _cnt_ln - 3 : 1)),$((_cnt_ln + 6))p" "$_cnt_f")"
	if [ "$(grep -cE 'PGC_PASSED=|PGC_FAILED=|PGC_UNRUN=' <<<"$_cnt_win" || true)" -eq 0 ]; then
		_cnt_n=$((_cnt_n + 1))
		[ "$_cnt_n" -le 5 ] && _cnt_bad="$_cnt_bad ${_cnt_f##*/}:$_cnt_ln"
	fi
done

check "every direct write to PGC_CHECKS records an outcome too" \
	"$(_tsm_fmt_cnt "$_cnt_n" "$_cnt_bad")" "[]"

# AND, SINCE #917, THERE MUST BE NONE AT ALL among files that use lib.sh's
# accounting. That is a stronger rule than the one above and it was not
# satisfiable before: counting a check and RECORDING it are now one operation,
# pgc_record, so a direct write to PGC_CHECKS counts a check that emits no
# RESULT line -- and pgc_reconcile_records then reports a records/checks mismatch
# on top of whatever the suite was actually failing for.
#
# The two rules disagreed until the thirteen sites were converted, which
# OffgridwithJD found: selftest 320 blessed a direct write with a nearby outcome,
# while the reconciler required a record. pgc_pass and pgc_fail exist precisely so
# a suite-local helper need not touch the counters, and lib.sh's own header
# already calls them "the only supported way to add a check from outside this
# file". This arm makes that a rule rather than a description.
#
# A file that keeps its OWN counters and never calls pgc_summary is exempt for the
# same reason as above, measured from the file rather than named: bench_guards
# reuses the variable name privately and never sources lib.sh.
_cnt_lib=0; _cnt_lib_bad=""
for _cnt_l in "${_cnt_sites[@]}"; do
	_cnt_f="${_cnt_l%%:*}"
	_cnt_ln="$(printf '%s' "$_cnt_l" | cut -d: -f2)"
	[ "$(grep -c 'pgc_summary' "$_cnt_f" || true)" -ne 0 ] || continue
	_cnt_lib=$((_cnt_lib + 1))
	[ "$_cnt_lib" -le 5 ] && _cnt_lib_bad="$_cnt_lib_bad ${_cnt_f##*/}:$_cnt_ln"
done
check "no suite that uses lib.sh's accounting writes PGC_CHECKS directly" \
	"$(_tsm_fmt_cnt "$_cnt_lib" "$_cnt_lib_bad")" "[]"

# BOTH RULES, PROVEN ON A FIXTURE. With the corpus clean, two arms reading "[]"
# are satisfied by a sweep that classifies nothing, so the classifier is driven
# against files built to trip it.
_cnt_fx="$PGC_WORKDIR/cntfx"; mkdir -p "$_cnt_fx"

# Uses lib.sh's accounting, bumps the counter, records no outcome nearby: both
# rules must see it.
# The bump is ASSEMBLED from the variable name rather than written out, so these
# generator lines do not themselves carry the shape the sweep looks for. Writing
# it literally made this file flag its own three fixtures -- the same mistake
# selftest 080's control avoids by living in a quoted heredoc.
_cnt_bump="$(printf '%s=$((%s + 1))' PGC_CHECKS PGC_CHECKS)"
_cnt_out="$(printf '%s=$((%s + 1))' PGC_FAILED PGC_FAILED)"
printf 'pgc_summary\n%s\necho hi\n' "$_cnt_bump" > "$_cnt_fx/bare.sh"
# Uses lib.sh's accounting, bumps the counter, DOES record an outcome: the old
# rule lets it through, the new one does not. That difference is the change.
printf 'pgc_summary\n%s\n%s\n' "$_cnt_bump" "$_cnt_out" > "$_cnt_fx/outcome.sh"
# Keeps its own counter and never calls pgc_summary: exempt from both.
printf '%s\necho "checks run: 1"\n' "$_cnt_bump" > "$_cnt_fx/private.sh"

_cnt_fx_old() {	# the ORIGINAL rule, applied to one file
	local _f="$1" _l
	[ "$(grep -c 'pgc_summary' "$_f" || true)" -ne 0 ] || { echo exempt; return; }
	_l="$(grep -n 'PGC_CHECKS=\$((PGC_CHECKS' "$_f" | head -1 | cut -d: -f1)"
	[ -n "$_l" ] || { echo none; return; }
	if [ "$(sed -n "$((_l > 3 ? _l - 3 : 1)),$((_l + 6))p" "$_f" \
		| grep -cE 'PGC_PASSED=|PGC_FAILED=|PGC_UNRUN=' || true)" = 0 ]; then
		echo flagged
	else
		echo allowed
	fi
}
_cnt_fx_new() {	# the STRONGER rule, applied to one file
	local _f="$1"
	[ "$(grep -c 'pgc_summary' "$_f" || true)" -ne 0 ] || { echo exempt; return; }
	[ "$(grep -c 'PGC_CHECKS=\$((PGC_CHECKS' "$_f" || true)" -ne 0 ] && echo flagged || echo none
}

check "premise: the fixtures carry the shapes these rules are about" \
	"$(grep -lc 'PGC_CHECKS=\$((PGC_CHECKS' "$_cnt_fx"/*.sh 2>/dev/null | grep -c . || true)" "3"
check "the original rule flags a bump that records no outcome" \
	"$(_cnt_fx_old "$_cnt_fx/bare.sh")" "flagged"
check "and allows one that does, which is what it was written to allow" \
	"$(_cnt_fx_old "$_cnt_fx/outcome.sh")" "allowed"
check "the stronger rule flags that same allowed bump, which is the change" \
	"$(_cnt_fx_new "$_cnt_fx/outcome.sh")" "flagged"
check "and both exempt a file that keeps its own counter without lib.sh" \
	"$(_cnt_fx_old "$_cnt_fx/private.sh")/$(_cnt_fx_new "$_cnt_fx/private.sh")" "exempt/exempt"

# ---- and the RUNNER must not report an INCOMPLETE suite as a pass ------------
#
# lib.sh exiting 67 is only half the state. The runner decides what a status
# MEANS, and until now 67 reached that decision through a catch-all else: safe
# by accident, unasserted, and with the template for breaking it three lines
# above -- copy the 66 branch, and an INCOMPLETE suite becomes a SKIP that the
# matrix reports green.
#
# The classifier is EVALLED OUT OF run_all_versions.sh rather than re-stated
# here. A check that recomputes the rule tests the world instead of the code:
# selftest 070 learned that when a premise globbed bench/*.sh to prove bench/ was
# swept, which asserts the directory EXISTS and not that the sweep read it.

_rv="$PGC_TESTDIR/run_all_versions.sh"
check "premise: the runner defines the classifier this part is about to eval" \
	"$(grep -c '^pgc_classify_suite_rc()' "$_rv")" "1"

eval "$(sed -n '/^pgc_classify_suite_rc()/,/^}/p' "$_rv")"
check "premise: the classifier evalled out of the runner is callable" \
	"$(type -t pgc_classify_suite_rc)" "function"

_rvlog="$PGC_WORKDIR/rv.log"

: > "$_rvlog"
check "the runner calls a clean exit a pass" \
	"$(pgc_classify_suite_rc 0 "$_rvlog")" "PASS"

printf 'x.sh: SKIPPED (ran no checks)\n' > "$_rvlog"
check "and 66 with its line a skip" \
	"$(pgc_classify_suite_rc 66 "$_rvlog")" "SKIP"

printf 'x.sh: INCOMPLETE\n' > "$_rvlog"
check "and 67 with its line INCOMPLETE, which is not a pass" \
	"$(pgc_classify_suite_rc 67 "$_rvlog")" "INCOMPLETE"

# Two signals, the same discipline 66 has: a bare status can be produced by any
# aborting command under set -e, so the code alone must not be believed.
printf 'x.sh: PASSED\n' > "$_rvlog"
check "67 without its line is a failure, not an INCOMPLETE taken on trust" \
	"$(pgc_classify_suite_rc 67 "$_rvlog")" "FAIL"

printf 'x.sh: FAILED\n' > "$_rvlog"
check "and an ordinary failure is still a failure" \
	"$(pgc_classify_suite_rc 1 "$_rvlog")" "FAIL"

# The whole point, stated as its own arm: no status reaches PASS except 0.
check "no non-zero status is classified as a pass" \
	"$(for _rvrc in 1 2 66 67 126 127 130; do pgc_classify_suite_rc "$_rvrc" "$_rvlog"; done | grep -c '^PASS$')" \
	"0"

# ---- and the DISPATCH must act on the verdict, not merely compute it --------
#
# The eight arms above test the classifier. The classifier was right and the
# caller threw the answer away: the first INCOMPLETE branch set MAJOR_FAIL=1, a
# variable written once and read nowhere, while the major verdict reads verfail.
# So a state that had been failing the gate BY ACCIDENT -- 67 fell to the else,
# which sets verfail=1 -- was routed explicitly to a branch that could not fail
# it. A regression, introduced by the commit that made the state explicit.
#
# Testing a function and not its caller is how that survives review. The mapping
# is now its own function that the loop CALLS, and this evals that text too.

eval "$(sed -n '/^pgc_verdict_fails_major()/,/^}/p' "$_rv")"
check "premise: the major-verdict mapping evalled out of the runner is callable" \
	"$(type -t pgc_verdict_fails_major)" "function"

check "an INCOMPLETE suite fails its major" \
	"$(pgc_verdict_fails_major INCOMPLETE)" "yes"

check "and a failing suite still does" \
	"$(pgc_verdict_fails_major FAIL)" "yes"

check "while a pass does not" \
	"$(pgc_verdict_fails_major PASS)" "no"

check "and a skip does not, which is the one that must stay true" \
	"$(pgc_verdict_fails_major SKIP)" "no"

# The dispatch is the thing that was wrong, so assert the runner CALLS it in the
# INCOMPLETE branch rather than setting some variable of its own. Not a
# re-derivation of the rule: the rule is evalled above. This asserts the wiring.
check "the runner's INCOMPLETE branch calls the mapping rather than a local flag" \
	"$(grep -c 'pgc_verdict_fails_major "\$_verdict"' "$_rv")" "1"

# CODE ONLY, NOT THE PROSE THAT DESCRIBES IT (#1123). `$_rv` carries 874 comment
# lines, and one of them writing `MAJOR_FAIL=` -- a historical note about the very
# name this arm exists to keep retired -- reddens it against a correct runner.
# MEASURED, not supposed: adding that one comment line and changing nothing else
# took harness_selftest to `1080 passed + 1 failed`.
#
# A herestring, not a pipe, because this file forbids the pipe (#486, part 080).
_rv_code="$(grep -vE '^[[:space:]]*#' "$_rv")"
check "and no write-only failure flag survives in the runner" \
	"$(grep -c 'MAJOR_FAIL=' <<<"$_rv_code" || true)" "0"

unset -f pgc_verdict_fails_major
unset _rv _rvlog _rvrc
unset -f pgc_classify_suite_rc
unset _cur_drift _cnt_dir _cnt_sites _cnt_l _cnt_f _cnt_ln _cnt_bad _cnt_n
unset _cur_lib _cur_dir
unset -f _cur_make _cur_run _cur_out
