# ---- a residual must be counted, never derived by subtraction ---------------
#
# #999 and #1006, filed independently by both sessions off the same runs. Every
# PG 17 matrix report on main printed a count that cannot exist:
#
#     suites that ran: 243 of 252 (skipped: 9, incomplete: 0)
#     of those, 248 accounted for their checks and -5 did not
#
# MINUS FIVE SUITES. PG 18 printed -2 on the same day, from 246 ran.
#
# THE TWO TERMS COUNT DIFFERENT POPULATIONS. `suites_ran` counts suites that ran,
# so a skipped suite is not in it. `_acc_any` counts suites whose LOG shows an
# accounting line, over all registered suites -- and a skipped suite still prints
# one, because `check_skip` is followed by `pgc_summary` and `pgc_summary` emits
# the accounting line on every exit path before it decides the status. So a
# skipped-but-accounted suite is in the second and not the first, and subtracting
# one from the other goes negative once there are more than none.
#
# WHY NOTHING CAUGHT IT, AND IT IS THE POINT OF THIS PART. The residual was
# DERIVED BY SUBTRACTION rather than counted, so the partition closes
# arithmetically whatever the inputs are: 248 + (-5) = 243. An
# `inputs == sum(buckets)` arm passes on that line for ANY two numbers. The house
# rule it breaks is written eight lines below the counter it governs, in
# `pgc_summary` itself:
#
#     A MEASUREMENT, not an identity. The failed count is its own counter rather
#     than `CHECKS - PASSED - UNRUN`, because a derived third term makes
#     `P + (N-P-U) + U = N` true for ANY values.
#
# The suite-level breakdown committed exactly the error the check-level
# accounting was written to prevent.
#
# AND THE EXISTING GUARD CANNOT REACH IT. Part 390 pins where the NUMERATOR comes
# from with `grep -c 'of those, $_acc_any accounted for their checks'`. That is a
# grep for the source line's text. It says nothing about the other term and
# nothing about whether the printed result is a number a count can take.
#
# SO THIS PART TESTS THE SET, NOT THE TEXT. The residual is now a set difference
# computed by `pgc_ran_without_accounting`, which is evalled out of the runner and
# fed crafted inputs here -- including the live shape, where the accounted set is
# a strict superset of the ran set. A set difference cannot go negative, so the
# defect becomes unrepresentable rather than merely detected.
#
# The skipped-but-accounted suites get their own named category, because they are
# not a debt and were being folded silently into a number phrased as one.
# ---------------------------------------------------------------------------

_rv510="$PGC_TESTDIR/run_all_versions.sh"

check "premise: the runner defines the residual reader this part evals" \
	"$(grep -c '^pgc_ran_without_accounting()' "$_rv510")" "1"
check "premise: the runner defines the intersection reader" \
	"$(grep -c '^pgc_accounted_among()' "$_rv510")" "1"

eval "$(sed -n '/^pgc_ran_without_accounting()/,/^}/p' "$_rv510")"
eval "$(sed -n '/^pgc_accounted_among()/,/^}/p' "$_rv510")"

check "premise: the residual reader is callable" \
	"$(type -t pgc_ran_without_accounting)" "function"
check "premise: the intersection reader is callable" \
	"$(type -t pgc_accounted_among)" "function"

_d510="$(mktemp -d)"

# THE LIVE SHAPE. Three suites ran; five accounted, because two that SKIPPED also
# printed an accounting line. This is 243-vs-248 in miniature, and it is the input
# that made the old expression print a negative.
printf '%s\n' alpha beta gamma            >"$_d510/ran"
printf '%s\n' alpha beta gamma delta eps  >"$_d510/acc"
printf '%s\n' delta eps                   >"$_d510/skipped"

printf '%s\n' alpha beta gamma  >"$_d510/ran2"
printf '%s\n' alpha             >"$_d510/acc2"

# WHAT THE OLD TERM DID ON THIS INPUT, computed here rather than asserted from the
# issue, so the number this part is built on is one the run produced.
_old510=$(( $(grep -c . "$_d510/ran") - $(grep -c . "$_d510/acc") ))
check "premise: the subtraction the runner used goes negative on the live shape" \
	"$_old510" "-2"

# AN EMPTY RESIDUAL AND AN ABSENT READER PRINT THE SAME THING, which is how the
# first draft of this part passed both of these arms while
# `pgc_ran_without_accounting` did not exist -- `command not found` writes nothing
# to stdout and `grep -c .` reports 0, exactly as a clean residual does. So the
# live shape is asserted TOGETHER WITH a shape that must yield names. A missing
# reader gives `0/0` and fails; only a reader that runs gives `0/2`.
check "the live shape yields an empty residual while the same reader still finds a real one" \
	"$(pgc_ran_without_accounting "$_d510/ran" "$_d510/acc" | grep -c . || true)/$(pgc_ran_without_accounting "$_d510/ran2" "$_d510/acc2" | grep -c . || true)" \
	"0/2"

check "the skipped-but-accounted suites are named, not folded into the residual" \
	"$(pgc_accounted_among "$_d510/skipped" "$_d510/acc" | tr '\n' ' ')" "delta eps "

# THE REAL DEBT STILL HAS TO SHOW, BY NAME. A residual that is empty for every
# input is a check that cannot fail, so the opposite case is asserted too.
check "a suite that ran and did not account is named by the residual" \
	"$(pgc_ran_without_accounting "$_d510/ran2" "$_d510/acc2" | tr '\n' ' ')" "beta gamma "

# NAMED, NOT COUNTED, per the rule this directory learned from nine collisions on
# one written count in a day. The count below is derived from the names the run
# produced, so the two cannot disagree.
check "the residual count agrees with the names the residual printed" \
	"$(pgc_ran_without_accounting "$_d510/ran2" "$_d510/acc2" | grep -c . || true)" "2"

# INPUTS == SUM(BUCKETS), and this time it means something: both buckets are
# counted from the data rather than one being the leftover of the other.
_r510_acc="$(pgc_accounted_among "$_d510/ran2" "$_d510/acc2" | grep -c . || true)"
_r510_not="$(pgc_ran_without_accounting "$_d510/ran2" "$_d510/acc2" | grep -c . || true)"
check "premise: the two buckets partition the suites that ran" \
	"$((_r510_acc + _r510_not))" "$(grep -c . "$_d510/ran2")"

# COMM NEEDS SORTED INPUT and the runner's files are appended in roster order,
# which is not sorted. An unsorted pair silently yields the wrong set rather than
# an error, so the sort belongs inside the reader and is asserted here.
printf '%s\n' gamma alpha beta  >"$_d510/ran_u"
printf '%s\n' beta alpha        >"$_d510/acc_u"
check "the reader sorts its own inputs rather than trusting the caller" \
	"$(pgc_ran_without_accounting "$_d510/ran_u" "$_d510/acc_u" | tr '\n' ' ')" "gamma "

# THE WIRING, not only the reader. Proving the helper correct says nothing about
# whether the summary calls it -- that is the half a previous removal proof in this
# tree missed entirely, verifying the tool while the bug sat in the four lines that
# called it.
# CODE LINES ONLY. The first draft of this arm counted 1 and failed, because the
# comment in the runner that EXPLAINS the old subtraction quotes it verbatim. That
# is the same count-versus-property trap this tree has now produced four times --
# a file-wide `fetch-depth` count, a word the checking comment contained, a count
# of `PGC_LEDGER_AGAINST`, and this. The property is that no CODE subtracts the
# wide population from the ran count; prose about it is the point of the prose.
check "the summary prints the residual from the reader, not by subtraction" \
	"$(grep -v '^[[:space:]]*#' "$_rv510" | grep -c 'suites_ran - _acc_any' || true)" "0"
# A PROPERTY, NOT AN OCCURRENCE COUNT. The summary calls this reader twice on
# purpose -- once for the names and once for the count -- so pinning "1" here would
# fail on correct code, and pinning "2" would break the next time one of them
# moves. What has to be true is that the summary reaches the reader with the ran
# file at all.
check "and the summary calls the residual reader on the suites that ran" \
	"$([ "$(grep -c 'pgc_ran_without_accounting "\$_acc_ranfile"' "$_rv510")" -ge 1 ] \
		&& echo yes || echo no)" "yes"
# SCOPED TO THE FUNCTION, not counted file-wide. An occurrence count over the
# whole runner is satisfied by the comment that explains the variable, and this
# tree has produced that error three times: a file-wide `fetch-depth` count, a
# count of a word the checking comment itself contained, and a count of
# `PGC_LEDGER_AGAINST`. What matters is that the tally function WRITES the name
# and the summary READS the file, so each is asserted where it belongs.
_fn510() { sed -n "/^$1() {/,/^}/p" "$_rv510"; }

check "premise: the tally function is findable" \
	"$(_fn510 pgc_tally_suite | grep -c 'suites_ran=')" "3"
check "the tally function records the name of every suite that ran" \
	"$(_fn510 pgc_tally_suite | grep -cE '>>"\$_acc_ranfile"')" "3"
check "and it records the name of every suite that skipped" \
	"$(_fn510 pgc_tally_suite | grep -cE '>>"\$_acc_skipfile"')" "1"

# ---- the printed line itself, not a grep for it ----------------------------
#
# The three arms above read the runner's TEXT. That proves the summary mentions
# the reader and says nothing about what it prints, which is the half a previous
# removal proof in this tree missed entirely -- it verified the tool while the bug
# sat in the four lines that called it.
#
# So the summary block is EXTRACTED from the runner and run here against a fixture
# shaped like the live defect: 3 suites ran, 2 of them skipped-and-accounted on
# top, which is 243-vs-248 in miniature. The block is taken rather than retyped,
# because a retyped block is a second implementation and agrees with itself.
_blk510="$(awk '/_acc_debt="\$\(pgc_ran_without_accounting/{f=1} f{print} f && /cannot happen/{g=1} g && /^\tfi$/{exit}' "$_rv510")"

check "premise: the summary block was extracted, not an empty range" \
	"$([ -n "$_blk510" ] && echo y || echo n)" "y"
check "premise: and it ends at its own closing brace, not mid-statement" \
	"$(printf '%s\n' "$_blk510" | tail -1)" "$(printf '\tfi')"

# The fixture: alpha/beta/gamma ran and all accounted; delta and eps SKIPPED and
# accounted too. The old line printed `3 - 5 = -2` here.
printf '%s\n' alpha beta gamma           >"$_d510/e2e_ran"
printf '%s\n' delta eps                  >"$_d510/e2e_skip"
printf '%s\n' alpha beta gamma delta eps >"$_d510/e2e_acc"

_out510="$(
	set -uo pipefail
	eval "$(sed -n '/^pgc_ran_without_accounting()/,/^}/p' "$_rv510")"
	eval "$(sed -n '/^pgc_accounted_among()/,/^}/p' "$_rv510")"
	_acc_ranfile="$_d510/e2e_ran"; _acc_skipfile="$_d510/e2e_skip"
	_acc_accounted="$_d510/e2e_acc"
	suites_ran=3; suites_skipped=2; suites_incomplete=0; verfail=0
	SUITES=(alpha beta gamma delta eps)
	echo "  suites that ran: $suites_ran of ${#SUITES[@]} (skipped: $suites_skipped, incomplete: $suites_incomplete)"
	eval "$_blk510"
	echo "verfail=$verfail"
)"

check "the printed breakdown no longer goes negative on the live shape" \
	"$(printf '%s\n' "$_out510" | grep -c 'of those, 3 accounted for their checks and 0 did not')" "1"
check "and no line in it carries a negative suite count at all" \
	"$(printf '%s\n' "$_out510" | grep -cE '(^|[^0-9-])-[0-9]+ did not' || true)" "0"
check "and the skipped-but-accounted suites are named as their own category" \
	"$(printf '%s\n' "$_out510" | grep -c '^  2 of the 2 skipped suites accounted for themselves anyway: delta eps$')" "1"
check "and the block leaves the major verdict alone when the sums agree" \
	"$(printf '%s\n' "$_out510" | grep -c '^verfail=0$')" "1"

# THE DEBT CASE THROUGH THE SAME BLOCK. An arm that only ever sees an empty
# residual cannot tell a working block from one that prints "0 did not" always.
printf '%s\n' alpha  >"$_d510/e2e_acc2"
_out510b="$(
	set -uo pipefail
	eval "$(sed -n '/^pgc_ran_without_accounting()/,/^}/p' "$_rv510")"
	eval "$(sed -n '/^pgc_accounted_among()/,/^}/p' "$_rv510")"
	_acc_ranfile="$_d510/e2e_ran"; _acc_skipfile="$_d510/e2e_skip"
	_acc_accounted="$_d510/e2e_acc2"
	suites_ran=3; suites_skipped=2; suites_incomplete=0; verfail=0
	SUITES=(alpha beta gamma delta eps)
	eval "$_blk510"
	echo "verfail=$verfail"
)"

check "a real debt is counted and named by the same block" \
	"$(printf '%s\n' "$_out510b" | grep -c 'of those, 1 accounted for their checks and 2 did not')" "1"
check "and the two suites are named, not left as a number" \
	"$(printf '%s\n' "$_out510b" | grep -c '^    ran without accounting: beta gamma$')" "1"
check "and a real debt is still not a major failure, which is the old behaviour" \
	"$(printf '%s\n' "$_out510b" | grep -c '^verfail=0$')" "1"

# THE PARTITION IS A MEASUREMENT ONLY IF NEITHER BUCKET IS A LEFTOVER (#1339). Both
# runs above keep suites_ran equal to the names that ran, so a block deriving the
# debt as `suites_ran - accounted` prints the same lines and passes them. Told 4 ran
# while the names say 3, all accounted, a counted block reports 3 + 0 != 4 and fails
# the major; a subtracting one makes 3 + 1 == 4 by construction and stays silent.
_out510c="$(
	set -uo pipefail
	eval "$(sed -n '/^pgc_ran_without_accounting()/,/^}/p' "$_rv510")"
	eval "$(sed -n '/^pgc_accounted_among()/,/^}/p' "$_rv510")"
	_acc_ranfile="$_d510/e2e_ran"; _acc_skipfile="$_d510/e2e_skip"
	_acc_accounted="$_d510/e2e_acc"
	suites_ran=4; suites_skipped=2; suites_incomplete=0; verfail=0
	SUITES=(alpha beta gamma delta eps)
	eval "$_blk510"
	echo "verfail=$verfail"
)"

check "a ran count the names do not support is reported, not absorbed" \
	"$(printf '%s\n' "$_out510c" | grep -c 'the ran breakdown does not add up: 4 ran, 3 accounted + 0 did not')" "1"
check "and it fails the major" \
	"$(printf '%s\n' "$_out510c" | grep -c '^verfail=1$')" "1"

# ---- collation, which selftest 070's regex cannot reach ---------------------
#
# Found by @OffgridwithJD reviewing this change. `comm` requires both inputs sorted
# in ITS collation, and `sort` orders by locale. Real suite names reorder: measured
# on this box, `pgc_setup`/`pg_dump_roundtrip` and `projections`/`projection_update`
# both swap between `C` and `en_US.UTF-8`, and `comm` then prints `file 1 is not in
# sorted order` and yields a wrong set.
#
# SELFTEST 070 CANNOT SEE IT. That guard (#552) requires every `| sort` in a file
# using `comm` to carry `LC_ALL=C`, and its pattern is a PIPE. These three readers
# use process substitution, so the file matched zero times and read as compliant
# while using `comm` three times.
#
# AND THE PREFIX DOES NOT REACH THE SUBSTITUTIONS. `LC_ALL=C comm <(sort ...)` pins
# only comm's own comparison: the substitutions run in subshells of the PARENT and
# inherit the parent's locale. Both halves are pinned, and both are asserted.
#
# Not live on the gate -- the container is C.UTF-8, which orders these as C does --
# so this is latent, and latent is exactly what a guard is for.
_comm510() { grep -vE '^[[:space:]]*#' "$_rv510"; }

_comm510_n="$(_comm510 | grep -cE '(^|[^_[:alnum:]])comm ')"
check "premise: the runner really does use comm, so this arm has a subject" \
	"$([ "$_comm510_n" -ge 3 ] && echo yes || echo "no ($_comm510_n comm calls)")" "yes"

check "every comm in the runner pins its collation" \
	"$(_comm510 | grep -E '(^|[^_[:alnum:]])comm ' | grep -cv 'LC_ALL=C comm ' || true)" "0"

check "and no sort feeding one is left on the caller's locale" \
	"$(_comm510 | grep -c '<(sort ' || true)" "0"

rm -rf "$_d510"
