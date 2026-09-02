# ---- the INCOMPLETE path must run WHOLE, not one link at a time -------------
#
# #859 shipped fifteen arms that drive pgc_classify_suite_rc and
# pgc_verdict_fails_major directly, and a reviewer verified the dispatch by
# lifting one line out of the runner and running it. Both prove LINKS. Neither
# proves the CHAIN, and the regression that got through #859 was a chain defect:
# the classifier returned INCOMPLETE correctly and the caller threw the answer
# away into a variable nothing read.
#
# Testing a function and not its caller is how a correct answer gets computed
# and discarded. So this part runs the whole thing, in five links:
#
#   1. a real suite calls check_unrunnable and pgc_summary
#        -> exits 67, writes an INCOMPLETE line and an UNRUN line
#   2. the runner's classifier reads the .log and .rc THAT SUITE produced
#        -> INCOMPLETE
#   3. the runner's tally consumes the classifier's own verdict
#        -> verfail=1, suites_ran=1, suites_incomplete=1
#   4. the runner's OWN collect loop, evalled out of run_all_versions.sh, runs
#      over both fixtures
#        -> this is the wiring: a tally the loop does not call fails HERE
#   5. the runner's OWN major-verdict branch, evalled the same way, consumes
#      the loop's outcome
#        -> SUMMARY says FAIL and overall=1
#
# Nothing is stubbed and nothing is re-derived: every function and every block
# is lifted out of run_all_versions.sh by text, and the .rc and .log are
# produced by running a suite rather than written by hand.
#
# Link 4 is the arm that did not exist in the first draft of this file. Without
# it, an extraction that defines pgc_tally_suite and leaves the old inline
# branch in the loop leaves every other arm here green over dead code -- which
# is the defect class this file exists to prevent, committed by the file itself.

_e2e_rv="$PGC_TESTDIR/run_all_versions.sh"
_e2e_dir="$PGC_WORKDIR/e2e"; mkdir -p "$_e2e_dir/test"

# PGC_WORKDIR comes from pgc_setup, which part 020 called. This part inherits it
# from an unrelated part, so it says out loud that it got one: a fixture written
# into an empty path would make every arm below test nothing.
check "premise: the selftest has a workdir to build fixtures in" \
	"$([ -n "${PGC_WORKDIR:-}" ] && [ -d "$_e2e_dir" ] && echo yes || echo no)" "yes"

# ---- link 1: two real suites, one that cannot evaluate a check and one that can

cat > "$_e2e_dir/test/e2e_incomplete.sh" <<E2EEOF
#!/usr/bin/env bash
. "$PGC_TESTDIR/lib.sh"
check "something it could evaluate" ok ok
check_unrunnable "something it could not" ABSENT_FIXTURE "the corpus was not built"
pgc_summary
E2EEOF

cat > "$_e2e_dir/test/e2e_pass.sh" <<E2EEOF
#!/usr/bin/env bash
. "$PGC_TESTDIR/lib.sh"
check "something it could evaluate" ok ok
pgc_summary
E2EEOF
chmod 755 "$_e2e_dir/test/e2e_incomplete.sh" "$_e2e_dir/test/e2e_pass.sh"

# Captured the way the runner captures it -- stdout and stderr into NAME.log,
# the status into NAME.rc, in the build directory the collect loop then reads.
# Link 4 hands that same directory to the runner's own loop as $builddir, so the
# files under test are consumed by the real code through the real path.
for _e2e_s in e2e_incomplete e2e_pass; do
	bash "$_e2e_dir/test/${_e2e_s}.sh" >"$_e2e_dir/${_e2e_s}.log" 2>&1
	echo $? >"$_e2e_dir/${_e2e_s}.rc"
done

check "a suite with an unrunnable check exits 67" \
	"$(cat "$_e2e_dir/e2e_incomplete.rc")" "67"

check "and its log carries the INCOMPLETE line the classifier needs" \
	"$(grep -c ': INCOMPLETE$' "$_e2e_dir/e2e_incomplete.log")" "1"

check "and the UNRUN line the runner prints into the matrix output" \
	"$(grep -c '^UNRUN  something it could not: ABSENT_FIXTURE:' "$_e2e_dir/e2e_incomplete.log")" "1"

# The control fixture must be a genuine pass, or link 4's discrimination is
# between two identical things.
check "control fixture: a suite whose checks all ran exits 0" \
	"$(cat "$_e2e_dir/e2e_pass.rc")" "0"

# ---- link 2: the runner's own functions, over those real files --------------
#
# Lifted by text out of run_all_versions.sh. Each extraction is premised on
# being non-empty BEFORE it is evalled, because `eval ""` succeeds silently: an
# extraction that drifted off its anchor would otherwise leave these arms
# testing whatever else happened to define the name.

_e2e_txt_classify="$(sed -n '/^pgc_classify_suite_rc()/,/^}/p' "$_e2e_rv")"
_e2e_txt_fails="$(sed -n '/^pgc_verdict_fails_major()/,/^}/p' "$_e2e_rv")"
_e2e_txt_tally="$(sed -n '/^pgc_tally_suite()/,/^}/p' "$_e2e_rv")"

check "premise: all three runner functions were extracted, not empty ranges" \
	"$([ -n "$_e2e_txt_classify" ] && echo y || echo n)$([ -n "$_e2e_txt_fails" ] && echo y || echo n)$([ -n "$_e2e_txt_tally" ] && echo y || echo n)" \
	"yyy"

# A truncated extraction evals to a syntax error, not to nothing, so the closing
# brace is asserted rather than assumed: the sed range stops at the first line
# beginning with `}`, and a body containing one would yield a fragment.
check "premise: and each extraction ends at its own closing brace" \
	"$(printf '%s\n%s\n%s\n' "$_e2e_txt_classify" "$_e2e_txt_fails" "$_e2e_txt_tally" | grep -c '^}$')" \
	"3"

eval "$_e2e_txt_classify"
eval "$_e2e_txt_fails"
eval "$_e2e_txt_tally"

check "premise: and all three are callable" \
	"$(type -t pgc_classify_suite_rc)/$(type -t pgc_verdict_fails_major)/$(type -t pgc_tally_suite)" \
	"function/function/function"

check "the runner classifies the file that suite actually produced" \
	"$(pgc_classify_suite_rc "$(cat "$_e2e_dir/e2e_incomplete.rc")" "$_e2e_dir/e2e_incomplete.log")" \
	"INCOMPLETE"

# ---- link 3: the tally, which is where the regression lived ----------------
#
# The verdict is the CLASSIFIER'S OUTPUT, not the string INCOMPLETE retyped
# here. Retyping it cuts the chain at exactly the joint this file exists to
# test, and would leave these arms green against a classifier that had stopped
# returning INCOMPLETE at all.
#
# suites_skipped starts at 5, not 0. The arm below says an INCOMPLETE suite is
# not counted as a skip; initialised to 0 that arm cannot tell "the tally left
# it alone" from "the tally zeroed it", and passes for a tally that does
# nothing whatever.

verfail=0; suites_ran=0; suites_skipped=5; suites_incomplete=0
results=""; skipped_names=""
pgc_tally_suite e2e_incomplete \
	"$(pgc_classify_suite_rc "$(cat "$_e2e_dir/e2e_incomplete.rc")" "$_e2e_dir/e2e_incomplete.log")" \
	"$_e2e_dir/e2e_incomplete.log" >"$_e2e_dir/tally_incomplete.out"

check "an INCOMPLETE suite sets the flag the major verdict actually reads" \
	"$verfail" "1"

check "and is counted as having run" "$suites_ran" "1"

check "and counted as incomplete, so the tally can say so" "$suites_incomplete" "1"

check "and is not counted as skipped, nor is the skip count disturbed" \
	"$suites_skipped" "5"

check "and appears in the results string as INCOMPLETE" \
	"$(printf '%s' "$results" | grep -c 'e2e_incomplete=INCOMPLETE ')" "1"

# The tally's stdout is what a human reads off a matrix run. A tally that
# records the verdict silently is a tally nobody can act on.
check "and the tally announces it, with the reason lifted from the log" \
	"$(grep -c '^  INCOMPLETE  e2e_incomplete (a check could not be evaluated)$' "$_e2e_dir/tally_incomplete.out")" "1"

check "and reprints the suite's own UNRUN line beneath it" \
	"$(grep -c '^      >> UNRUN  something it could not: ABSENT_FIXTURE:' "$_e2e_dir/tally_incomplete.out")" "1"

# ---- link 4: THE WIRING. the runner's own collect loop, not a copy of it ----
#
# Everything above would still pass if run_all_versions.sh defined
# pgc_tally_suite and never called it. This link evals the loop itself, out of
# the runner, and hands it the same build directory the fixtures were captured
# into. If the loop does not call the tally, verfail stays 0 here and nowhere
# else.

_e2e_txt_loop="$(awk '/^\tsuites_incomplete=/{f=1} f{print} f&&/^\tdone$/{exit}' "$_e2e_rv")"

check "premise: the runner's collect loop was extracted, not an empty range" \
	"$([ -n "$_e2e_txt_loop" ] && echo yes || echo no)" "yes"

# Membership, not adjacency, and counted from the extracted text rather than
# from the file: a reflow moves lines, and the question is whether the loop
# delegates, not where.
check "the loop delegates each verdict to pgc_tally_suite" \
	"$(printf '%s\n' "$_e2e_txt_loop" | grep -c 'pgc_tally_suite')" "1"

check "and no longer counts incompletes inline beside it" \
	"$(printf '%s\n' "$_e2e_txt_loop" | grep -c 'suites_incomplete=\$((')" "0"

SUITES=(e2e_incomplete e2e_pass)
builddir="$_e2e_dir"
verfail=0; suites_ran=0; suites_skipped=0
results=""; skipped_names=""
# 99, not 0: the loop must zero this itself. suites_ran and suites_skipped are
# reset per major by lines above the extracted chunk, so this test sets them;
# suites_incomplete is reset INSIDE the chunk, so seeding it with a value no
# correct run can produce is what makes the arm below able to see the reset.
suites_incomplete=99
eval "$_e2e_txt_loop" >"$_e2e_dir/loop.out"

check "running the real loop over both fixtures fails the major" "$verfail" "1"

check "and counts both suites as having run" "$suites_ran" "2"

check "and exactly one of them as incomplete" "$suites_incomplete" "1"

check "and neither as skipped" "$suites_skipped" "0"

check "and records each suite's own verdict in the results string" \
	"$(printf '%s' "$results" | grep -c 'e2e_incomplete=INCOMPLETE e2e_pass=PASS ')" "1"

# ---- link 5: the major's verdict, which is the end of the chain -------------
#
# The first draft of this file ended by re-deriving the runner's rule inside the
# test -- `[ "$verfail" = 0 ] && echo PASS || echo FAIL` -- which asserts
# nothing about run_all_versions.sh and is strictly weaker than the arm three
# lines above it. This evals the runner's actual branch instead, so a change to
# what the matrix PRINTS, or a dropped `overall=1`, reddens here.

_e2e_txt_verdict="$(awk '/^\tif \[ "\$verfail" = 0 \]; then/{f=1} f{print} f&&/^\tfi$/{exit}' "$_e2e_rv")"

check "premise: the major-verdict branch was extracted, not an empty range" \
	"$(printf '%s\n' "$_e2e_txt_verdict" | grep -c 'SUMMARY+=')" "2"

major=17; overall=0; SUMMARY=()
eval "$_e2e_txt_verdict"

check "with an incomplete suite in the tally the major reports FAIL" \
	"$(printf '%s\n' "${SUMMARY[0]:-<no summary line was appended>}" | cut -c1-11)" "FAIL   PG17"

check "and the run's overall status is failure" "$overall" "1"

check "and the summary line carries the incomplete count a reader needs" \
	"$(printf '%s\n' "${SUMMARY[0]:-<no summary line was appended>}" | grep -c '(2 ran, 0 skipped, 1 incomplete)')" "1"

# ---- control: the same five links, on a tree where nothing is incomplete ----
#
# Without this the arms above are satisfied by a tally and a verdict branch that
# say FAIL to everything. The control drives the SAME code over only the passing
# fixture and requires the opposite answer at every link -- including the
# results string and the tally's stdout, because a tally that records nothing at
# all also leaves verfail at 0.

SUITES=(e2e_pass)
verfail=0; suites_ran=0; suites_skipped=0; suites_incomplete=0
results=""; skipped_names=""
eval "$_e2e_txt_loop" >"$_e2e_dir/loop_pass.out"

check "control: the same loop leaves a passing suite passing" \
	"$verfail/$suites_ran/$suites_incomplete/$suites_skipped" "0/1/0/0"

check "control: and still records that it ran, and how" \
	"$(printf '%s' "$results" | grep -c 'e2e_pass=PASS ')" "1"

check "control: and still announces it" \
	"$(grep -c '^  PASS  e2e_pass$' "$_e2e_dir/loop_pass.out")" "1"

major=17; overall=0; SUMMARY=()
eval "$_e2e_txt_verdict"

check "control: and the major reports PASS" \
	"$(printf '%s\n' "${SUMMARY[0]:-<no summary line was appended>}" | cut -c1-11)" "PASS   PG17"

check "control: and leaves the run's overall status alone" "$overall" "0"

unset _e2e_rv _e2e_dir _e2e_s
unset _e2e_txt_classify _e2e_txt_fails _e2e_txt_tally _e2e_txt_loop _e2e_txt_verdict
unset verfail suites_ran suites_skipped suites_incomplete results skipped_names
unset builddir major overall _rc _verdict s
unset SUITES SUMMARY
unset -f pgc_classify_suite_rc pgc_verdict_fails_major pgc_tally_suite
