# ---- the ledger gate must take a prior ceiling the caller can name ----------
#
# #1104. THE NIGHTLY WENT RED ON A TAG PUSH, not a schedule, and the first telling
# of this named the wrong trigger. Six scheduled runs on main are green; the only
# red in the workflow's history is `push ref=v1.0-alpha4`. A branch checkout
# configures an upstream and `auto` resolves. A tag checkout is DETACHED, has no
# local branch, and so has none.
#
# Every major went red and no suite failed. The roster held 258 entries with zero
# `=FAIL`, and the verdict came from the ledger gate:
#
#     ledger integrity failure: no trustworthy prior ceiling: GITHUB_BASE_REF is
#     unset and the local main has no configured upstream.
#     PG15 could not run the ledger gate at all, which is not a pass
#
# `--against auto` resolves the prior from `GITHUB_BASE_REF`, which GitHub sets
# only on a `pull_request` event, or from `main@{upstream}`, which the checkout
# does not configure. The nightly runs on `schedule`, so it has neither and the
# gate fails closed. Failing closed is CORRECT: comparing a ceiling against the
# wrong prior is worse than refusing. The defect is that the caller had no way to
# say which prior it means.
#
# `run_all_versions.sh` hardcoded `--against auto`, so a caller that knows its own
# prior could not pass one. `PGC_LEDGER_AGAINST` is that way in.
#
# AND `auto` IS A TAUTOLOGY ON THE RUNS THAT PASS. `actions/checkout` fetches
# refs/heads/main into refs/remotes/origin/main and checks out main at that sha, so
# `auto` resolves to origin/main and origin/main IS HEAD. Measured: a24155b3
# against a24155b3. The ceiling was compared against the commit it was read from,
# every night, and could not have caught a raise. `parent` replaces a comparison
# that cannot fail with one that can, which is a better reason for this arm than
# the tag run that exposed it.
#
# MEASURED BEFORE THE FIX, on this tree:
#
#     --against auto      resolves here, because this clone HAS an upstream, so a
#                         local run cannot reproduce the nightly at all
#     --against HEAD~1    "ceiling against HEAD~1: 249 -> 249, which does not rise"
#     HEAD~1 at depth 1   MISSING, so the nightly also needs fetch-depth >= 2
#
# The last line is why the workflow half is not optional. Naming the ref without
# fetching the history it needs turns one failure into another.


_g500="$TESTDIR/run_all_versions.sh"

check "premise: the runner invokes the ledger gate" \
	"$(grep -c 'pgc_ledger.py" gate' "$_g500")" "1"

# THE PROPERTY: the ref is taken from the environment, not written into the file.
# A literal `--against auto` cannot be overridden by a caller that knows better.
# A PROPERTY, NOT A COUNT. The first version of this arm counted occurrences of
# the variable and broke the moment the surrounding comment mentioned it, which is
# this tree's most-repeated instrument defect and it caught me writing the check
# for it. What matters is that the POLICY SELECTION reads the variable.
check "the runner takes its prior ceiling policy from PGC_LEDGER_AGAINST" \
	"$(grep -cE '^[[:space:]]*case "\$\{PGC_LEDGER_AGAINST:-auto\}" in' "$_g500")" "1"

check "and it still defaults to auto when the caller names nothing" \
	"$(grep -c 'PGC_LEDGER_AGAINST:-auto' "$_g500")" "2"

# A POLICY, NOT A REF. Part 410 refuses a runner that names a prior, because a
# stale one makes the gate enforce less while printing that it compared. An
# environment variable taking an arbitrary ref is that hole with a longer fuse,
# so the runner accepts a closed set and nothing else.
check "the prior is chosen from a closed set of policies" \
	"$(grep -cE '^[[:space:]]+auto\|parent\)' "$_g500")" "1"

# The nightly must actually USE it, and must fetch the history that ref needs.
# Either half alone leaves the gate unable to run.
_n500="$TESTDIR/../.github/workflows/nightly.yml"
check "premise: the nightly workflow is present" \
	"$([ -f "$_n500" ] && echo yes || echo no)" "yes"

# SCOPED TO THE `suites` JOB, not counted over the file. `upgrade-guard` already
# carries `fetch-depth: 0` for an unrelated reason, so a file-wide count says
# "present" while the job that failed has none. That job is also the control this
# diagnosis rests on: it runs the same runner WITH full history and PASSED in the
# same nightly where all five `suites` jobs failed.
# THIS IS THE SCHEDULE HALF OF A PROBLEM ci.yml ALREADY SOLVED FOR PULL REQUESTS.
# `ci.yml` carries a "fetch the PR base, for the ledger ceiling comparison" step,
# guarded by `if: github.base_ref != ''`, and part 410 pins it. A scheduled run
# has no `base_ref`, so that step cannot fire, and pointing it at `origin/main`
# would compare main's ceiling against itself. `HEAD~1` is the prior that means
# something on a nightly: did the commit that landed raise the ceiling.
#
# The two are complementary rather than duplicates, and part 410 owns the PR half.
_j500() { awk -v j="  $1:" '$0==j{f=1;next} /^  [a-z][a-z0-9_-]*:$/{f=0} f' "$_n500"; }

# MATCHING THE INVOCATION, not the word -- the same trap the arm below this one
# already documents, which had not been applied here. A comment inside the job
# that NAMES run_all_versions.sh counted as a second invocation and this arm
# read 2 where the property is 1. It was found by writing such a comment
# (#1248): the guard went red on a change that added no second call. Anchoring
# at the start of the command is what a comment cannot reach, since a comment
# line begins with `#`.
check "premise: the suites job is findable and runs the matrix" \
	"$(_j500 suites | grep -cE '^[[:space:]]*bash test/run_all_versions\.sh')" "1"

check "the suites job names a prior ceiling for the gate" \
	"$(_j500 suites | grep -c 'PGC_LEDGER_AGAINST')" "1"

# MATCHING THE YAML KEY, not the word. The comment explaining this setting also
# contains "fetch-depth", so a plain grep counts the prose beside the thing it
# means to check and reports 2 where the property is 1. That is the same
# count-versus-property trap this tree keeps finding, and it found this arm.
check "and the suites job fetches the history that ref needs" \
	"$(_j500 suites | grep -cE '^[[:space:]]+fetch-depth:[[:space:]]*[0-9]+')" "1"
