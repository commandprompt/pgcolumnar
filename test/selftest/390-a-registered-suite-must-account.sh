# ---- a registered suite must account for its checks, or say it cannot -------
#
# The matrix prints "suites that ran: N of M" and never checks it. Two different
# things hide behind that line.
#
# The first is arithmetic nobody does. ran + skipped + incomplete is printed
# beside M and never compared with it, so a suite whose verdict the tally loop
# drops leaves the sum short and the line still reads plausibly.
#
# The second is worse, because it is live today. pgc_classify_suite_rc maps rc=0
# to PASS with no further question, and twelve registered suites exit 0 without
# ever calling pgc_summary. Measured, with a pattern tight enough to exclude
# portlib.sh -- a looser one matched it and gave both reviewers of this change the
# same wrong answer: NONE of the twelve sources test/lib.sh. Each defines its own
# check(), and ten of them keep no tally at all. So the harness cannot see their
# checks, and nothing says so. They are counted among the suites that "ran", which
# is the exact overcount #447 added that line to stop, one level further down.
#
# The number is deliberately not repeated in prose elsewhere. The reconciliation
# prints it at runtime, and a count in prose is the thing this repository keeps
# having to unlearn: nine collisions on one written number in a single day.
#
# So the fix is NOT a number. A count cannot do this job: two errors of opposite
# sign cancel, and an exempt list maintained by hand makes the count agree by
# construction -- the check then measures the list, not the run.
#
# Instead, derive MEMBERSHIP from a property each suite carries, and assert set
# equality in BOTH directions:
#
#   declared  the suite's own text calls pgc_summary
#   observed  the suite's log carries the "accounting:" line pgc_summary prints
#             on every exit path, before it decides the status
#
# Neither is a number and neither is hand-maintained. A suite that stops calling
# pgc_summary moves between the sets on its own, and the two directions catch
# opposite mistakes: declared-but-not-observed is a suite that died before it
# could account, and observed-but-not-declared is a stale reading of the source.
#
# The functions are EVALLED OUT OF run_all_versions.sh, per selftest 320: a check
# that restates the rule tests the world instead of the code.
# ---------------------------------------------------------------------------

_rv="$PGC_TESTDIR/run_all_versions.sh"

check "premise: the runner defines the declaration reader this part evals" \
	"$(grep -c '^pgc_suite_declares_accounting()' "$_rv")" "1"
check "premise: the runner defines the observation reader this part evals" \
	"$(grep -c '^pgc_log_shows_accounting()' "$_rv")" "1"
check "premise: the runner defines the reconciliation this part evals" \
	"$(grep -c '^pgc_reconcile_accounting()' "$_rv")" "1"

eval "$(sed -n '/^pgc_suite_declares_accounting()/,/^}/p' "$_rv")"
eval "$(sed -n '/^pgc_log_shows_accounting()/,/^}/p' "$_rv")"
eval "$(sed -n '/^pgc_reconcile_accounting()/,/^}/p' "$_rv")"
check "premise: the declaration reader evalled out of the runner is callable" \
	"$(type -t pgc_suite_declares_accounting)" "function"
check "premise: the observation reader evalled out of the runner is callable" \
	"$(type -t pgc_log_shows_accounting)" "function"
check "premise: the reconciliation evalled out of the runner is callable" \
	"$(type -t pgc_reconcile_accounting)" "function"

_acc="$PGC_WORKDIR/acc"; mkdir -p "$_acc"

# ---- the declaration reader ------------------------------------------------

printf '. "$(dirname "$0")/lib.sh"\ncheck "x" a a\npgc_summary\n' > "$_acc/declares.sh"
check "a suite that calls pgc_summary declares accounting" \
	"$(pgc_suite_declares_accounting "$_acc/declares.sh")" "yes"

printf '. "$(dirname "$0")/lib.sh"\necho hi\nexit 0\n' > "$_acc/silent.sh"
check "a suite that never calls it does not" \
	"$(pgc_suite_declares_accounting "$_acc/silent.sh")" "no"

# A MENTION is not a call. The easy wrong implementation is a bare grep, and it
# reads a suite that only explains why it cannot account as though it does.
printf '. "$(dirname "$0")/lib.sh"\n# this suite cannot call pgc_summary: it has no cluster\nexit 0\n' \
	> "$_acc/mentions.sh"
check "a comment mentioning pgc_summary is not a declaration" \
	"$(pgc_suite_declares_accounting "$_acc/mentions.sh")" "no"

# Nor is a longer name that contains it.
printf '. "$(dirname "$0")/lib.sh"\npgc_summary_of_something\n' > "$_acc/prefix.sh"
check "a longer name containing pgc_summary is not a declaration" \
	"$(pgc_suite_declares_accounting "$_acc/prefix.sh")" "no"

# An ABSENT file is its own answer. Reported by OffgridwithJD reviewing #922:
# folding it into "no" classifies a registered suite whose .sh has vanished as
# exempt, and the reconciliation then reads clean -- a suite disappearing from
# the matrix, inside the check whose subject is suites going missing from the
# accounting.
check "a file that does not exist is reported absent, not exempt" \
	"$(pgc_suite_declares_accounting "$_acc/absent.sh")" "absent"
check "and absent is distinguishable from a present file that does not declare" \
	"$([ "$(pgc_suite_declares_accounting "$_acc/absent.sh")" \
		= "$(pgc_suite_declares_accounting "$_acc/silent.sh")" ] && echo same || echo different)" \
	"different"

# ---- the comment stripper follows the SHELL's rule -------------------------
#
# `sed 's/#.*$//'` strips from ANY hash, so a `#` inside a quoted string earlier
# on the line hides a pgc_summary call after it. Also reported by OffgridwithJD.
# The stripper now only treats a hash at line start or after whitespace as a
# comment, which is what the shell does.
printf '. "$(dirname "$0")/lib.sh"\nX=a#b; pgc_summary\n' > "$_acc/hashinword.sh"
check "a hash inside a word does not hide the call after it" \
	"$(pgc_suite_declares_accounting "$_acc/hashinword.sh")" "yes"

printf '. "$(dirname "$0")/lib.sh"\npgc_summary  # and a trailing comment\n' > "$_acc/trailing.sh"
check "a trailing comment after the call does not hide it" \
	"$(pgc_suite_declares_accounting "$_acc/trailing.sh")" "yes"

printf '. "$(dirname "$0")/lib.sh"\n  # pgc_summary is only mentioned here\nexit 0\n' > "$_acc/indented.sh"
check "an indented comment is still a comment" \
	"$(pgc_suite_declares_accounting "$_acc/indented.sh")" "no"

# The residual the shell rule does not cover is a hash after whitespace INSIDE a
# quoted string, and the arm for it must not be a second spelling of the hazard.
#
# The first version of this arm WAS that, and it was INVERTED: it required a
# non-whitespace character before the hash, which is a hash inside a WORD -- the
# shape the stripper handles correctly -- so it flagged the safe case and was
# blind to the dangerous one. Found by OffgridwithJD, who built the control:
#
#     psql -c "SELECT 1 # note"; pgc_summary   the reader answered NO, unflagged
#     X=a#b; pgc_summary                       the reader answered YES, FLAGGED
#
# So the arm no longer restates the hazard. It compares the reader's INPUT with
# its OUTPUT: count the call in the raw file, count it again in the stripped
# text, and if the stripped count is lower the stripper hid a call. That detects
# it for any spelling, present or future, cannot be inverted, and does not depend
# on a measurement staying true -- it IS the measurement.
_hash_pat='(^|[^_[:alnum:]])pgc_summary([^_[:alnum:]]|$)'
_hidden=0
while IFS= read -r _hs; do
	_hf="$PGC_TESTDIR/${_hs}.sh"
	[ -f "$_hf" ] || continue
	_raw="$(grep -cE "$_hash_pat" "$_hf" || true)"
	_str="$(sed 's/\(^\|[[:space:]]\)#.*$/\1/' "$_hf" | grep -cE "$_hash_pat" || true)"
	if [ "$_str" -lt "$_raw" ]; then
		# A call the stripper removed. Only a real one matters; a comment-only
		# mention losing its line is the stripper working.
		if [ "$(pgc_suite_declares_accounting "$_hf")" = no ] \
			&& grep -qE "$_hash_pat" "$_hf"; then
			_hidden=$((_hidden + 1))
			echo "    the comment stripper hides a pgc_summary call in $_hs.sh"
		fi
	fi
done < <(listed_suites)
check "the stripper hides no pgc_summary call in any registered suite" "$_hidden" "0"

# And prove that arm can fire, on a file built to trip it. Without this the zero
# above is satisfied by an arm that never looks at anything.
printf '. "$(dirname "$0")/lib.sh"\npsql -c "SELECT 1 # note"; pgc_summary\n' > "$_acc/hidden.sh"
_h_raw="$(grep -cE "$_hash_pat" "$_acc/hidden.sh" || true)"
_h_str="$(sed 's/\(^\|[[:space:]]\)#.*$/\1/' "$_acc/hidden.sh" | grep -cE "$_hash_pat" || true)"
check "premise: the fixture really does hide its call from the stripper" \
	"$([ "$_h_str" -lt "$_h_raw" ] && echo hidden || echo "raw=$_h_raw str=$_h_str")" "hidden"
check "and the reader answers no on it, which is the wrong answer the arm catches" \
	"$(pgc_suite_declares_accounting "$_acc/hidden.sh")" "no"

# ---- the observation reader ------------------------------------------------
#
# pgc_summary prints the accounting line before every exit path, so it is present
# on a pass, a failure, a skip and an incomplete alike. That is what makes it the
# runtime twin of the declaration rather than a synonym for PASSED.

printf 'checks run: 3\nchecks unrunnable: 0\naccounting: 3 passed + 0 failed + 0 unrunnable + 0 skipped = 3\nx.sh: PASSED\n' > "$_acc/pass.log"
check "a passing log shows accounting" "$(pgc_log_shows_accounting "$_acc/pass.log")" "yes"

printf 'accounting: 1 passed + 2 failed + 0 unrunnable + 0 skipped = 3\nx.sh: FAILED\n' > "$_acc/fail.log"
check "and so does a failing one, which is the point" \
	"$(pgc_log_shows_accounting "$_acc/fail.log")" "yes"

printf 'accounting: 0 passed + 0 failed + 0 unrunnable + 0 skipped = 0\nx.sh: SKIPPED (ran no checks)\n' > "$_acc/skip.log"
check "and a skip, which reached the summary and counted zero" \
	"$(pgc_log_shows_accounting "$_acc/skip.log")" "yes"

printf 'accounting: 2 passed + 0 failed + 1 unrunnable + 0 skipped = 3\nx.sh: INCOMPLETE\n' > "$_acc/inc.log"
check "and an incomplete" "$(pgc_log_shows_accounting "$_acc/inc.log")" "yes"

printf 'x.sh: PASSED\n' > "$_acc/bare.log"
check "a log claiming PASSED without the accounting line shows none" \
	"$(pgc_log_shows_accounting "$_acc/bare.log")" "no"

printf 'this suite prints the word accounting: in prose\n' > "$_acc/prose.log"
check "and prose containing the word does not count as the line" \
	"$(pgc_log_shows_accounting "$_acc/prose.log")" "no"

# THE ^ ANCHOR, which nothing above exercises. The prose fixture is refused by the
# regex SHAPE, not by the anchor, so removing ^ from the reader left every arm
# green -- reported by OffgridwithJD. The distinguishing input is a well-formed
# accounting line that does NOT start the line, which is what a nested or indented
# suite run produces. Inert on real data today (0 non-line-start occurrences
# across 246 PG17 logs and 244 PG18), so this closes a coverage gap rather than a
# live defect.
printf '  accounting: 3 passed + 0 failed + 0 unrunnable + 0 skipped = 3\nx.sh: PASSED\n' \
	> "$_acc/indented_acc.log"
check "premise: the fixture carries a well-formed accounting line, just indented" \
	"$(grep -c 'accounting: 3 passed + 0 failed + 0 unrunnable + 0 skipped = 3' "$_acc/indented_acc.log")" "1"
check "an accounting line that does not start its line is refused" \
	"$(pgc_log_shows_accounting "$_acc/indented_acc.log")" "no"

check "an absent log shows no accounting rather than erroring" \
	"$(pgc_log_shows_accounting "$_acc/absent.log")" "no"

# ---- and the reader must be shown the PRODUCER's own output -----------------
#
# Every log above is a literal typed into this file, and pytest types the same
# four again, and the format string itself lives a third time in lib.sh's
# pgc_summary. Three hand-written copies of one line: a wording drift in the
# PRODUCER leaves both harnesses green while the reader answers "no" for every
# real suite, which would redden the whole matrix on both majors having passed
# its own tests.
#
# That is the pipefail lesson surviving on the other reader, and it was found by
# OffgridwithJD, who measured it: one realistic rewording of lib.sh:1507 flips the
# reader to "no" on a real log with no arm going red.
#
# So run a REAL two-line suite and feed the reader its actual stdout. This is the
# only arm here that survives a change to the format.
_realsuite="$_acc/real.sh"
printf '. "%s/lib.sh"\ncheck "x" a a\npgc_summary\n' "$PGC_TESTDIR" > "$_realsuite"
_reallog="$_acc/real.log"
bash "$_realsuite" > "$_reallog" 2>&1 || true

check "premise: the real suite ran and reached its summary" \
	"$(grep -c ': PASSED$' "$_reallog")" "1"
check "premise: and produced exactly one accounting line to be read" \
	"$(grep -c '^accounting: ' "$_reallog")" "1"
check "the reader accepts the line the producer actually emits" \
	"$(pgc_log_shows_accounting "$_reallog")" "yes"

# The control that this arm is not simply insensitive: the same real log with its
# accounting line reworded must be refused.
sed 's/^accounting: /accounting summary: /' "$_reallog" > "$_acc/real_drifted.log"
check "premise: the drift changed the line the reader looks for" \
	"$(grep -c '^accounting: ' "$_acc/real_drifted.log")" "0"
check "and a reworded producer line is refused, so the arm can fail" \
	"$(pgc_log_shows_accounting "$_acc/real_drifted.log")" "no"

# ---- the reconciliation, in both directions --------------------------------

_declared="$_acc/declared"; _observed="$_acc/observed"

printf 'alpha\nbeta\ngamma\n' > "$_declared"
printf 'alpha\nbeta\ngamma\n' > "$_observed"
check "equal sets reconcile" \
	"$(pgc_reconcile_accounting "$_declared" "$_observed" >/dev/null 2>&1 && echo ok || echo asymmetric)" "ok"

# The direction that catches the false green: a suite said it would account and
# no accounting line appeared, so it died before reaching pgc_summary. Today that
# reads PASS whenever the shell happened to exit 0.
printf 'alpha\nbeta\n' > "$_observed"
check "a declared suite that produced no accounting is caught" \
	"$(pgc_reconcile_accounting "$_declared" "$_observed" >/dev/null 2>&1 && echo ok || echo asymmetric)" "asymmetric"
check "and it is NAMED, so the reader does not have to diff two lists" \
	"$(pgc_reconcile_accounting "$_declared" "$_observed" 2>&1 | grep -c '^[[:space:]]*declared but never accounted: gamma$')" "1"

# The opposite direction: an accounting line from a suite whose source says it
# cannot produce one. That means the reading of the source is stale, and it is
# the failure an exempt list maintained by hand can never report.
printf 'alpha\nbeta\n' > "$_declared"
printf 'alpha\nbeta\ngamma\n' > "$_observed"
check "an undeclared suite that DID account is caught too" \
	"$(pgc_reconcile_accounting "$_declared" "$_observed" >/dev/null 2>&1 && echo ok || echo asymmetric)" "asymmetric"
check "and it is named as the opposite fault, not the same one" \
	"$(pgc_reconcile_accounting "$_declared" "$_observed" 2>&1 | grep -c '^[[:space:]]*accounted but never declared: gamma$')" "1"

# Both at once must report both. One error masking the other is how a count
# passes while two suites are wrong in opposite directions -- the exact failure
# a count cannot distinguish from correctness.
printf 'alpha\ndelta\n' > "$_declared"
printf 'alpha\ngamma\n' > "$_observed"
check "opposite errors do not cancel: both directions are reported" \
	"$(pgc_reconcile_accounting "$_declared" "$_observed" 2>&1 | grep -cE '^[[:space:]]*(declared but never accounted: delta|accounted but never declared: gamma)$')" "2"

# inputs == sum(buckets), printed from the data, per the house rule.
printf 'alpha\nbeta\ngamma\n' > "$_declared"
printf 'beta\ngamma\ndelta\n' > "$_observed"
check "the reconciliation prints inputs == sum(buckets)" \
	"$(pgc_reconcile_accounting "$_declared" "$_observed" 2>&1 | grep -c 'inputs=4 .*both=2.*declared only=1.*accounted only=1.*sum=4')" "1"

# ---- and the RUNNER must call it, not merely define it ----------------------
#
# Selftest 320 records what testing a function and not its caller costs here: the
# classifier was right and the loop threw the answer away. So pin the call site
# and pin that its result can fail the major.

# Count CALLS, not mentions. The first version of this arm matched the comment
# on the definition line as readily as the call below it -- the same
# mention-for-a-call mistake pgc_suite_declares_accounting exists to refuse,
# committed by the arm that asserts it.
check "the runner calls the reconciliation, not merely defines it" \
	"$(grep -c '[^_[:alnum:]]pgc_reconcile_accounting "' "$_rv")" "1"
check "premise: and that count excludes the definition line, which mentions it" \
	"$(grep -c '^pgc_reconcile_accounting()' "$_rv")" "1"
check "and a failed reconciliation sets the per-major failure flag" \
	"$(grep -A6 'pgc_reconcile_accounting "\$_acc_declared"' "$_rv" | grep -c 'verfail=1')" "1"

# ---- the suites the driver deliberately never ran ---------------------------
#
# PGC_SKIP_TIMING drops four suites on every CI run. They call pgc_summary and
# correctly produce no accounting line, because nothing executed them. Without a
# term for that the reconciliation goes red for the one reason that is not a
# defect, and a check that cries wolf on every CI run is a check nobody reads.
#
# The driver records the decision where it makes it. These arms hold that the
# term EXCUSES only what the driver actually recorded, and cannot be used to
# excuse anything else.

_notdisp="$_acc/notdispatched"

printf 'alpha\nbeta\ngamma\n' > "$_declared"
printf 'alpha\nbeta\n' > "$_observed"
printf 'gamma\n' > "$_notdisp"
check "a declared suite the driver never dispatched reconciles" \
	"$(pgc_reconcile_accounting "$_declared" "$_observed" "$_notdisp" >/dev/null 2>&1 && echo ok || echo asymmetric)" "ok"

# The same inputs WITHOUT the record must still be caught, or the term is not
# doing any work and the arm above is satisfied by a function that ignores it.
check "and without that record the same run is still caught" \
	"$(pgc_reconcile_accounting "$_declared" "$_observed" >/dev/null 2>&1 && echo ok || echo asymmetric)" "asymmetric"

# A suite cannot both have reached its summary and not have been dispatched.
# Taking the union would absorb this silently, so it is asserted on its own.
printf 'alpha\nbeta\n' > "$_declared"
printf 'alpha\nbeta\n' > "$_observed"
printf 'beta\n' > "$_notdisp"
check "a suite recorded as never dispatched that DID account is caught" \
	"$(pgc_reconcile_accounting "$_declared" "$_observed" "$_notdisp" >/dev/null 2>&1 && echo ok || echo asymmetric)" "asymmetric"
check "and it is named as that fault, not as one of the other two" \
	"$(pgc_reconcile_accounting "$_declared" "$_observed" "$_notdisp" 2>&1 \
		| grep -c '^[[:space:]]*both accounted and recorded as never dispatched: beta$')" "1"

# The record cannot excuse a suite that never declared accounting in the first
# place: that is still the stale-reading direction.
printf 'alpha\n' > "$_declared"
printf 'alpha\n' > "$_observed"
printf 'zeta\n' > "$_notdisp"
check "the record cannot introduce a suite the source never declared" \
	"$(pgc_reconcile_accounting "$_declared" "$_observed" "$_notdisp" 2>&1 \
		| grep -c '^[[:space:]]*accounted but never declared: zeta$')" "1"

# ---- and the DRIVER must write that record ---------------------------------
#
# The term is only honest if the branch that decides not to run a suite is the
# thing that records it. Pin the write to that branch, beside the forged log it
# sits next to.

# ANCHORED ON THE echo, NOT ON ITS REDIRECTION (#1073). This read `>"..."` and the
# runner now appends, because the batch fingerprint is written to the log first and
# a truncating redirect would erase it. The arm's subject -- that the branch which
# decides not to dispatch is the thing that records it -- did not change; only the
# operator did, and the arm went red for a reason unrelated to what it asserts.
check "the skip branch records the suite it did not dispatch" \
	"$(grep -A8 'echo "\$s.sh: SKIPPED (ran no checks)" >>*"\$builddir/\${s}.log"' "$_rv" \
		| grep -c 'accounting.notdispatched')" "1"
check "and the reconciliation is given that record" \
	"$(grep -c 'pgc_reconcile_accounting "\$_acc_declared" "\$_acc_observed" "\$_acc_notdisp"' "$_rv")" "1"

# ---- and the identity must be able to FAIL ---------------------------------
#
# inputs == sum(buckets) is printed beside every reconciliation, per the house
# rule. Printing it is not the same as checking it, and asserting it is worth
# nothing unless something can make it false.
#
# Measured, not argued. Two mutations:
#
#   compute _inputs from the buckets instead of from the files -- the arithmetic
#   above becomes P + D + O == P + D + O, and NOTHING reddens. That is why
#   _inputs is counted from the two files by a separate route.
#
#   drop the sort before comm -- comm then reports garbage buckets, and the
#   totals diverge. That is the fault this identity actually guards, and it is
#   selftest 070's subject arriving in a second place.
#
# So the arm is the second mutation, applied to a twin evalled here.

eval "$(sed -n '/^pgc_reconcile_accounting()/,/^}/p' "$_rv" \
	| sed 's|LC_ALL=C sort -u "$_decl" 2>/dev/null|cat "$_decl" 2>/dev/null|' \
	| sed 's|LC_ALL=C sort -u "$_obs"  2>/dev/null|cat "$_obs" 2>/dev/null|' \
	| sed 's/^pgc_reconcile_accounting()/pgc_reconcile_unsorted_twin()/')"

check "premise: the unsorted twin is callable" \
	"$(type -t pgc_reconcile_unsorted_twin)" "function"
# A clean pass reads the same whether the code is load-bearing or the mutation
# never applied, so assert the twin really lost its sort.
check "premise: the mutation applied -- the twin no longer sorts its inputs" \
	"$(type pgc_reconcile_unsorted_twin | grep -c 'LC_ALL=C sort -u \"\$_decl\"')" "0"
check "premise: and the real function still does" \
	"$(type pgc_reconcile_accounting | grep -c 'LC_ALL=C sort -u \"\$_decl\"')" "1"

printf 'gamma\nbeta\nalpha\n' > "$_declared"
printf 'delta\ngamma\nbeta\n' > "$_observed"
check "the identity catches comm reading unsorted input" \
	"$(pgc_reconcile_unsorted_twin "$_declared" "$_observed" 2>&1 | grep -c 'does not add up')" "1"
check "and the real function reconciles the same input, so the arm is not noise" \
	"$(pgc_reconcile_accounting "$_declared" "$_observed" 2>&1 | grep -c 'does not add up')" "0"

# ---- the readers, run over the REAL population ------------------------------
#
# Everything above uses fixtures. A reader that works on four synthetic files and
# not on the 251 registered suites has been tested against the world it was
# written for. So run the declaration reader over the actual list and print the
# partition, per the rule that a list-derived claim shows inputs == sum(buckets).
#
# No count is asserted. The number of exempt suites is not a fact about
# correctness, and pinning it here would make this arm a second copy of a
# hand-maintained list -- which is the thing the whole design removes.

# THREE buckets, not two. Folding "absent" into "does not declare" is the
# conflation the reader was just fixed for, and repeating it here would leave the
# real population the one place it still happened.
# The population is counted by a SECOND ROUTE, not by the loop that classifies it.
# The first version incremented _reg in the same loop body as the buckets, so the
# sum equalled it for ANY reader -- OffgridwithJD proved it passes with an
# always-yes reader and with an always-no reader alike. A total derived from the
# loop that produces the buckets is an identity, which is the shape this file
# spends its length refusing.
#
# WHAT THIS ARM IS, said plainly so the next reader does not overrate it: a
# COVERAGE check. It fails when the classification loop does not see every
# registered suite -- a future `continue`, a read that drops a line, a list that
# changes between the two reads. It is NOT a check on the reader's correctness;
# the two arms below it, which require both buckets to be occupied, are what
# catch a reader answering the same way for everything.
_reg="$(listed_suites | grep -c . || true)"
_decl_n=0; _exempt_n=0; _absent_n=0
while IFS= read -r _s; do
	case "$(pgc_suite_declares_accounting "$PGC_TESTDIR/${_s}.sh")" in
		yes)	_decl_n=$((_decl_n + 1)) ;;
		absent)	_absent_n=$((_absent_n + 1)); echo "    registered but has no file: $_s.sh" ;;
		*)	_exempt_n=$((_exempt_n + 1)) ;;
	esac
done < <(listed_suites)

echo "  registered=$_reg | declares accounting=$_decl_n, does not=$_exempt_n, absent=$_absent_n | sum=$((_decl_n + _exempt_n + _absent_n))"

check "premise: the registered list is not empty, so the partition means something" \
	"$([ "$_reg" -gt 0 ] && echo yes || echo no)" "yes"
check "the partition over the real suite list adds up" \
	"$((_decl_n + _exempt_n + _absent_n))" "$_reg"
check "every registered suite has a file" "$_absent_n" "0"

# Both buckets must be occupied, or the reader is answering the same way for
# everything and the arms above would pass just as happily.
check "the reader does not answer yes for every registered suite" \
	"$([ "$_exempt_n" -gt 0 ] && echo yes || echo no)" "yes"
check "nor no for every one of them" \
	"$([ "$_decl_n" -gt 0 ] && echo yes || echo no)" "yes"

# ---- the declaration reader must survive `set -o pipefail` ------------------
#
# A REGRESSION ARM. The first version of pgc_suite_declares_accounting piped sed
# into `grep -q`, and this file runs under `set -o pipefail`. grep -q exits the
# moment it matches, closing the pipe while sed is still writing; sed takes EPIPE
# and exits non-zero, and pipefail reports the whole pipeline as failed even
# though grep matched. The function then answered "no" for a suite that plainly
# calls pgc_summary.
#
# It was caught here, and only here: on the real population two of the longest
# suites -- hilbert_curve, the longest at 1,899 lines, and analyze_function, the
# third at 806 -- read as not declaring
# accounting inside this run and as declaring it outside. Selftest 040 carries
# the same story from #473 and #476, where it named different innocent suites on
# every run.
#
# The arm is a file long enough to lose the race, with the call at the TOP so a
# matcher that exits early exits early.
_bigsuite="$_acc/big.sh"
{
	printf '. "$(dirname "$0")/lib.sh"\npgc_summary\n'
	_i=0
	while [ "$_i" -lt 40000 ]; do printf 'echo padding line %s\n' "$_i"; _i=$((_i + 1)); done
} > "$_bigsuite"

check "premise: pipefail is on, which is the condition the bug needs" \
	"$(set -o | grep -cE '^pipefail[[:space:]]+on$')" "1"
check "premise: the fixture is long enough to lose the race" \
	"$([ "$(wc -l < "$_bigsuite")" -gt 10000 ] && echo yes || echo no)" "yes"

check "a long suite that calls pgc_summary still declares accounting" \
	"$(pgc_suite_declares_accounting "$_bigsuite")" "yes"

# And prove the arm can fail. The twin is the SHAPE that was wrong, restated
# rather than extracted, because the wrong version is no longer in the tree.
#
# It lives in a QUOTED HEREDOC, like selftest 080's own control and for the same
# reason: 080 now sweeps every producer piped into an early-exit reader, so a
# deliberate demonstration of the forbidden shape has to be text being written to
# a file rather than a pipeline this suite runs. Exempted by property, not by a
# line number.
_grepq_twin_sh="$_acc/grepq_twin.sh"
cat > "$_grepq_twin_sh" <<'TWIN'
set -uo pipefail
sed 's/#.*$//' "$1" \
	| grep -qE '(^|[^_[:alnum:]])pgc_summary([^_[:alnum:]]|$)' && echo yes || echo no
TWIN
check "premise: the twin script was written and is runnable" \
	"$([ -s "$_grepq_twin_sh" ] && echo yes || echo no)" "yes"
check "the grep -q shape is the one that gets this wrong under pipefail" \
	"$(bash "$_grepq_twin_sh" "$_bigsuite")" "no"
check "and it agrees with the real reader on a SHORT file, which is why it survived review" \
	"$(bash "$_grepq_twin_sh" "$_acc/declares.sh")" \
	"$(pgc_suite_declares_accounting "$_acc/declares.sh")"

# ---- the POPULATION, which the symmetry check above cannot see --------------
#
# pgc_reconcile_accounting reconciles the DECLARED set against the OBSERVED one.
# Both are derived from the suites themselves, and the two directions catch
# opposite mistakes -- but the registered set is not one of its inputs, so a
# registered suite in NEITHER set is outside the universe being reconciled.
# Driven from the function: with all three files empty it prints
# `inputs=0 | both=0 ... sum=0` and returns 0, whatever SUITES holds.
#
# Reported by @linuxhikerpm, who put it structurally: treating absence of a
# declaration as absence from the population preserves the overcount. The title
# of this change claims to reconcile registered suites against accounted ones,
# and that claim needs the registered set as an input.
#
# So the population is its own check, over its own four buckets. A suite is
# ACCOUNTED when its log carries evidence it counted its checks -- either
# lib.sh's accounting line, or its own `checks run:` line, which is what
# bench_guards prints from private counters (docs_style did until #1341). Runtime-observable in
# both cases, and derived rather than declared, so a suite that adopts either
# mechanism leaves the debt bucket on its own.

check "premise: the runner defines the population reconciliation" \
	"$(grep -c '^pgc_reconcile_population()' "$_rv")" "1"
check "premise: and the accounted reader that feeds it" \
	"$(grep -c '^pgc_log_shows_any_accounting()' "$_rv")" "1"

eval "$(sed -n '/^pgc_log_shows_any_accounting()/,/^}/p' "$_rv")"
eval "$(sed -n '/^pgc_reconcile_population()/,/^}/p' "$_rv")"
check "premise: the population reconciliation is callable" \
	"$(type -t pgc_reconcile_population)" "function"

# ---- the accounted reader takes EITHER mechanism ----------------------------

printf 'accounting: 1 passed + 0 failed + 0 unrunnable + 0 skipped = 1\nx.sh: PASSED\n' > "$_acc/lib.log"
check "a log carrying lib.sh's accounting line is accounted" \
	"$(pgc_log_shows_any_accounting "$_acc/lib.log")" "yes"

printf 'checks run: 9\ndocs_style.sh: PASSED\n' > "$_acc/self.log"
check "and a log carrying only its OWN checks-run line is accounted too" \
	"$(pgc_log_shows_any_accounting "$_acc/self.log")" "yes"

printf 'some output\nPASSED\n' > "$_acc/none.log"
check "a log carrying neither is not accounted" \
	"$(pgc_log_shows_any_accounting "$_acc/none.log")" "no"

# ---- the red arm @linuxhikerpm asked for, exactly as asked ------------------

_reg_f="$_acc/registered"; _acct_f="$_acc/accounted"; _debt_f="$_acc/debt"
printf 'alpha\n' > "$_reg_f"; : > "$_acct_f"; : > "$_notdisp"; : > "$_debt_f"
check "a registered suite that is accounted by nothing FAILS" \
	"$(pgc_reconcile_population "$_reg_f" "$_acct_f" "$_notdisp" "$_debt_f" >/dev/null 2>&1 \
		&& echo ok || echo unaccounted)" "unaccounted"
check "and it is named, which the symmetry check could never do" \
	"$(pgc_reconcile_population "$_reg_f" "$_acct_f" "$_notdisp" "$_debt_f" 2>&1 \
		| grep -c '^[[:space:]]*registered but accounted by nothing: alpha$')" "1"

# Each of the three ways out must actually let it out, or the bucket is a name
# for "always fails" and the debt file is the only thing doing any work.
printf 'alpha\n' > "$_acct_f"; : > "$_notdisp"; : > "$_debt_f"
check "a suite that accounted passes" \
	"$(pgc_reconcile_population "$_reg_f" "$_acct_f" "$_notdisp" "$_debt_f" >/dev/null 2>&1 \
		&& echo ok || echo unaccounted)" "ok"
: > "$_acct_f"; printf 'alpha\n' > "$_notdisp"
check "a suite the driver never dispatched passes" \
	"$(pgc_reconcile_population "$_reg_f" "$_acct_f" "$_notdisp" "$_debt_f" >/dev/null 2>&1 \
		&& echo ok || echo unaccounted)" "ok"
: > "$_notdisp"; printf 'alpha\n' > "$_debt_f"
check "a suite recorded as known debt passes" \
	"$(pgc_reconcile_population "$_reg_f" "$_acct_f" "$_notdisp" "$_debt_f" >/dev/null 2>&1 \
		&& echo ok || echo unaccounted)" "ok"

# The debt file excuses ONLY what it names. A new unaccounted suite must fail
# even while the known ten are excused -- that is the whole point of recording
# them by name rather than as a count.
printf 'alpha\nbeta\n' > "$_reg_f"; : > "$_acct_f"; printf 'alpha\n' > "$_debt_f"
check "a NEW unaccounted suite fails even while the known debt is excused" \
	"$(pgc_reconcile_population "$_reg_f" "$_acct_f" "$_notdisp" "$_debt_f" 2>&1 \
		| grep -c '^[[:space:]]*registered but accounted by nothing: beta$')" "1"
check "and the excused one is not named as a failure" \
	"$(pgc_reconcile_population "$_reg_f" "$_acct_f" "$_notdisp" "$_debt_f" 2>&1 \
		| grep -c '^[[:space:]]*registered but accounted by nothing: alpha$')" "0"

# THE REAL INPUT IS UNSORTED (#1339). --list-suites prints SUITES order, and every
# fixture above is one name or already sorted, so the function's own `sort -u`
# could be dropped with every arm here green while comm read unsorted input.
printf 'd\nb\na\nc\n' > "$_reg_f"; printf 'a\n' > "$_acct_f"; printf 'b\n' > "$_notdisp"; printf 'c\n' > "$_debt_f"
check "an unsorted registered list partitions as a sorted one does" \
	"$(pgc_reconcile_population "$_reg_f" "$_acct_f" "$_notdisp" "$_debt_f" 2>/dev/null \
		| grep -c 'registered=4 | accounted=1, not dispatched=1, known debt=1, unaccounted=1 | sum=4')" "1"
: > "$_notdisp"

# Debt that no longer exists is debt that should have been removed. A name in the
# file that is not registered, or that now accounts, means the file is stale --
# and a stale debt file is how a burn-down stops burning down.
printf 'alpha\n' > "$_reg_f"; printf 'alpha\n' > "$_acct_f"; printf 'alpha\n' > "$_debt_f"
check "a suite that now accounts but is still listed as debt is reported" \
	"$(pgc_reconcile_population "$_reg_f" "$_acct_f" "$_notdisp" "$_debt_f" 2>&1 \
		| grep -c '^[[:space:]]*listed as debt but now accounts: alpha$')" "1"

printf 'alpha\n' > "$_reg_f"; printf 'alpha\n' > "$_acct_f"; printf 'gone\n' > "$_debt_f"
check "and debt naming a suite that is not registered is reported too" \
	"$(pgc_reconcile_population "$_reg_f" "$_acct_f" "$_notdisp" "$_debt_f" 2>&1 \
		| grep -c '^[[:space:]]*listed as debt but not registered: gone$')" "1"

# inputs == sum(buckets) over the REGISTERED population, printed per the house
# rule. Like the symmetry check's, it cannot be false on the DATA -- the buckets
# are built by successive subtraction from the registered set, so their sum equals
# it identically. What it guards is comm reading unsorted input. The arms above,
# on the unaccounted bucket, are the ones that carry weight.
printf 'a\nb\nc\nd\n' > "$_reg_f"
printf 'a\n' > "$_acct_f"; printf 'b\n' > "$_notdisp"; printf 'c\n' > "$_debt_f"
check "the population partitions, and prints inputs == sum(buckets)" \
	"$(pgc_reconcile_population "$_reg_f" "$_acct_f" "$_notdisp" "$_debt_f" 2>&1 \
		| grep -c 'registered=4 .*accounted=1, not dispatched=1, known debt=1, unaccounted=1 | sum=4')" "1"

# ---- and the RUNNER must call it, with the real registered set --------------

check "the runner calls the population reconciliation" \
	"$(grep -c '[^_[:alnum:]]pgc_reconcile_population "' "$_rv")" "1"
# The property, not a count of a substring: the registered file must be written
# from the SUITES array itself. The first version of this arm asserted the name
# appeared twice, which is a fact about how many times a variable is spelled --
# it fails when the code is refactored and passes when the file is filled from
# the wrong source.
check "and the registered file is written from the SUITES array itself" \
	"$(grep -cF 'printf '"'"'%s\n'"'"' "${SUITES[@]}" >"$_acc_registered"' "$_rv")" "1"
check "and a failed population reconciliation fails the major" \
	"$(grep -A4 'pgc_reconcile_population "\$_acc_registered"' "$_rv" | grep -c 'verfail=1')" "1"

# The debt file is tracked, so a change to it is a diff a reviewer sees -- which
# is the whole reason it is a file and not a number in the environment.
check "the debt file is in the tree" \
	"$([ -f "$PGC_TESTDIR/suites_without_accounting.txt" ] && echo yes || echo no)" "yes"

# ---- one report, one answer to "how many accounted" (#928) ---------------------
#
# A single PG 17 matrix report printed two different answers three lines apart:
#
#   population reconciliation: registered=251 | accounted=237, ... | sum=251
#   of those, 235 accounted for their checks and 7 did not
#
# 237 and 235, both describing suites that accounted for their checks, differing by
# exactly 2. The population line counted with the WIDE reader and the breakdown line
# derived from the NARROW one, so the figure phrased as a problem -- the second --
# overstated the debt. The breakdown exists to stop an overcount, and deriving it from
# the narrower reader reintroduced a smaller version of the same overcount in the line
# added to close it.
#
# THE GAP IS A DIFFERENT MECHANISM, NOT A DEBT. The wide reader also accepts a suite
# that prints its own `checks run:` line. Measured on this tree: of the twelve
# registered suites that never call `pgc_summary`, exactly two emit a tally of their own
# -- `bench_guards` and `docs_style` -- and the other ten keep none. The two are the 2.
# (#1341 moved docs_style onto lib.sh; the run prints whoever remains.)
#
# THE NAMES ARE PRINTED BY THE RUN, not counted here: a third suite adopting its own
# mechanism appears without anyone editing a number.

check "premise: the runner defines the own-mechanism difference this part evals" \
	"$(grep -c '^pgc_own_mechanism_suites()' "$_rv")" "1"
eval "$(sed -n '/^pgc_own_mechanism_suites()/,/^}/p' "$_rv")"
eval "$(sed -n '/^pgc_log_shows_any_accounting()/,/^}/p' "$_rv")"
check "premise: it is callable" "$(type -t pgc_own_mechanism_suites)" "function"
check "premise: and so is the wide reader it is paired with" \
	"$(type -t pgc_log_shows_any_accounting)" "function"

# THE TWO READERS MUST DISAGREE ON EXACTLY ONE SHAPE, which is the whole premise. A log
# carrying only `checks run:` is accounted by the wide reader and not by the narrow one.
_o28="$PGC_WORKDIR/acc928"; mkdir -p "$_o28"
# THE FIXTURE CARRIES THE SHAPE THE NARROW READER ACTUALLY WANTS, and that shape
# moved under this branch: lib.sh's accounting line gained a `skipped` term, so the
# four-term form this fixture first wrote stopped being accepted. The premise arm
# below is what said so -- it went red on the rebase with `got [no] want [yes]`,
# which is precisely the job of a premise that asserts a fixture really is in the
# state the test needs. Without it the two readers would have agreed on this log for
# the wrong reason and the arm about their disagreement would have been vacuous.
#
# THREE OF THE CHECKS BELOW ARE ARMS, NOT PREMISES. They call run_all_versions.sh's
# own readers, so a change to those readers reddens them: two mutations of the
# narrow reader did, on all five majors (#1313). A check a repository change can
# redden is not a premise (#1236), so they carry no prefix. They measure the
# readers part 590 also drives, which is why 590's mutations redden part 390.
printf 'accounting: 3 passed + 0 failed + 0 unrunnable + 0 skipped = 3\nx.sh: PASSED\n' > "$_o28/libsh.log"
printf 'checks run: 9\nowntally.sh: PASSED\n' > "$_o28/own.log"
printf 'some output\nPASSED\n' > "$_o28/neither.log"
check "a lib.sh accounting line is seen by the narrow reader" \
	"$(pgc_log_shows_accounting "$_o28/libsh.log")" "yes"
check "a private tally is NOT seen by the narrow reader" \
	"$(pgc_log_shows_accounting "$_o28/own.log")" "no"
check "premise: but IS seen by the wide one, which is where the 2 came from" \
	"$(pgc_log_shows_any_accounting "$_o28/own.log")" "yes"
check "and a log with neither is seen by neither" \
	"$(pgc_log_shows_any_accounting "$_o28/neither.log")$(pgc_log_shows_accounting "$_o28/neither.log")" "nono"

# The difference names the private-tally suite and nothing else.
printf 'alpha\ngamma\n' > "$_o28/narrow"
printf 'alpha\nbeta\ngamma\n' > "$_o28/wide"
check "the own-mechanism difference names the suite the readers disagree about" \
	"$(pgc_own_mechanism_suites "$_o28/narrow" "$_o28/wide" | tr '\n' ' ')" "beta "
check "and names nothing when the two readers agree" \
	"$(pgc_own_mechanism_suites "$_o28/wide" "$_o28/wide" | grep -c . || true)" "0"
# UNSORTED INPUT, because the real files are appended in SUITES order and `comm` on
# unsorted input answers wrongly without saying so.
printf 'gamma\nalpha\n' > "$_o28/narrow_unsorted"
printf 'gamma\nbeta\nalpha\n' > "$_o28/wide_unsorted"
check "and sorts its inputs, because the runner appends them in SUITES order" \
	"$(pgc_own_mechanism_suites "$_o28/narrow_unsorted" "$_o28/wide_unsorted" | tr '\n' ' ')" "beta "

# And the headline must come from the SAME file the population line counts.
#
# THE LINE WAS REWORDED BY #999 AND THIS ARM FOLLOWS THE PROPERTY, NOT THE TEXT.
# The headline used to BE `$_acc_any`, the count of the wide file. It is now the
# intersection of the suites that RAN with that same wide file, because
# `suites_ran - _acc_any` subtracts two different populations and printed `-5` on
# every PG 17 matrix. What #928 established is unchanged and is what is asserted
# here: the headline derives from `_acc_accounted`, never from `_acc_observed`.
check "the breakdown headline counts the wide set, not the narrow one" \
	"$(grep -c '_acc_ranacc="\$(pgc_accounted_among "\$_acc_ranfile" "\$_acc_accounted"' "$_rv")" "1"
check "and the headline a reader sees is that number" \
	"$(grep -c 'of those, \$_acc_ranacc accounted for their checks' "$_rv")" "1"
check "and the narrow file is still not what the headline counts" \
	"$(grep -c 'of those, \$_acc_ran accounted' "$_rv")" "0"
check "and the population reconciliation counts that same file" \
	"$(grep -c '_acc_any="\$(grep -c \. "\$_acc_accounted"' "$_rv")" "1"
# THE LABEL WAS REWORDED AND THIS ARM FOLLOWS IT. It used to grep "by their own
# mechanism", which was read as "emits no RESULT records" twice in one night by two
# different readers, and a planning number came out of the misreading: twelve suites
# called unseedable when ten of them emit records and only two do not.
#
# THE DISCLAIMER IN THAT LABEL IS NOT SEPARATELY PINNED, and the reason is worth
# stating rather than leaving as an omission. Pinning it means either a second check
# or renaming this one to match a wider assertion. A second check costs a ledger row;
# renaming this one ORPHANS its existing row -- and `main` currently has no tool to
# remove an orphan, which is exactly what #983 is about. So the honest sequence is:
# #993 lands the prune, and then this arm can be renamed and widened in the change
# that regenerates the ledger anyway. Until then the reword is protected by this grep
# breaking on any further edit, and by the comment at the echo itself.
check "premise: and the narrow count is still printed, as the lib.sh half" \
	"$(grep -c "printed lib.sh's accounting line" "$_rv")" "1"
