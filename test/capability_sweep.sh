#!/usr/bin/env bash
#
# pgColumnar: no check may vanish when an optional capability is absent (#1185).
#
# A suite that runs fewer checks because a capability is missing reads exactly
# like a suite that HAS fewer checks. The accounting reconciles what ran against
# what was recorded, so a block that records nothing when it does not run is
# invisible by construction: `0 unrunnable + 0 skipped`, and PASSED. Measured on
# arrow_import.sh before #1159 fixed it, PG17:
#
#     with pyarrow     72 passed + 0 failed + 0 unrunnable + 0 skipped   PASSED
#     without          16 passed + 0 failed + 0 unrunnable + 0 skipped   PASSED
#
# THE INVARIANT IS NOT "THE NAME SETS MUST MATCH". Measured over all 30 pyarrow
# suites, 25 of them decline as a whole: one FAIL record saying the capability is
# required, and nothing else runs. That is loud and it is not this defect. A
# guard demanding equality would refuse 25 correct suites, and a guard that
# refuses correct code gets switched off and takes its rule with it.
#
# So: EITHER the absent run records every name the present run does, OR it
# records a refusal. Anything else is a check that vanished without a trace.
#
#     A  names preserved, a skip under each arm's own name     5
#     B  loud refusal: one FAIL record, nothing else ran      25
#     C  SILENT LOSS                                           0   <- must stay 0
#
# WHY THIS IS NOT A MATRIX SUITE. Two runs of 30 suites is 211 seconds, which is
# a nightly rather than something every leg of every PR pays. It is a nightly
# STEP rather than a registered suite so it does not nest inside
# run_all_versions.sh, which already runs six suites at once.
#
# It is NOT gated on PGC_SKIP_TIMING: ci.yml and nightly.yml both set it, so a
# guard behind that variable never runs anywhere.
#
# Usage:  test/capability_sweep.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

PG_CONFIG="${1:-/usr/local/pg17/bin/pg_config}"
SRCDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TESTDIR="$SRCDIR/test"

if [ ! -x "$PG_CONFIG" ]; then
	pgc_fail "no such pg_config: $PG_CONFIG"
	pgc_summary
fi
PGC_MAJOR="$(pgc_major_of "$PG_CONFIG")"
PGC_CAP_SELF="$(basename "${BASH_SOURCE[0]}")"
PGC_CAP_SELF_PATH="$(realpath "${BASH_SOURCE[0]}" 2>/dev/null)"

# ---- the population, derived twice and asserted ----------------------------
#
# grep -c, NEVER grep -q, DOWNSTREAM OF A PIPE. `grep -q` exits at its first
# match and closes the pipe; sed takes EPIPE and exits non-zero; under
# `set -o pipefail` the PIPELINE reports that failure although grep MATCHED, so
# the file is silently dropped. It is a race, so it drops a DIFFERENT file each
# run. Measured five times over one unchanging tree while building this file:
# grep -q gave 30, 29, 29, 29, 30 and grep -c gave 30 every time. The first
# version of this sweep therefore swept 29 suites and never ran
# native_parquet_streaming.sh. run_all_versions.sh carries the same story in
# pgc_suite_declares_accounting, from #473 and #476.
#
# AND IT EXCLUDES ITSELF, which is not housekeeping. This file names pyarrow in
# code -- the shim writes `raise ImportError` into a pyarrow.py and the premises
# import it -- so it matches its own population and the first run of it SWEPT
# ITSELF: one recursive call per suite, each bounded only by the 900s timeout,
# 20 minutes in before anything else had run. A guard whose own text satisfies
# its pattern is the defect #1222 and #1227 are about; this is the same shape
# with a wall clock instead of a count.
pgc_cap_population() {	# pgc_cap_population DIR PATTERN -> one suite name per line
	local f n b
	for f in "$1"/*.sh; do
		b="$(basename "$f")"
		# BY REALPATH, NOT BY THE NAME THIS WAS INVOKED AS (@jdatcmd, review).
		# The first version compared basenames against `basename $0`. Reached
		# under any other name -- a symlink, a staged copy, a rename -- the skip
		# stops matching the real file, the file re-enters the population, and
		# the recursion is back. A realpath cannot be fooled that way.
		[ "$(realpath "$f" 2>/dev/null)" = "$PGC_CAP_SELF_PATH" ] && continue
		n="$(sed 's/#.*//' "$f" | grep -c "$2")"
		[ "$n" -gt 0 ] && printf '%s\n' "$b"
	done
	return 0
}

NARROW="$(pgc_cap_population "$TESTDIR" 'import pyarrow')"
WIDE="$(pgc_cap_population "$TESTDIR" 'pyarrow')"
n_narrow="$(printf '%s\n' "$NARROW" | grep -c .)"
n_wide="$(printf '%s\n' "$WIDE" | grep -c .)"

# SETS, NOT SIZES (@jdatcmd, review). Comparing counts is sound only because
# every line matching `import pyarrow` also matches `pyarrow`, so NARROW is a
# subset of WIDE and equal sizes means equal sets. Nothing states that subset
# relation, and the day one pattern stops being a subset of the other the counts
# can agree over different sets while this stays green. comm costs nothing.
check_num "premise: the two derivations of the population name the same suites" \
	"$(LC_ALL=C comm -3 <(printf '%s\n' "$NARROW" | LC_ALL=C sort) \
		<(printf '%s\n' "$WIDE" | LC_ALL=C sort) | grep -c .)" "0"
check_text "premise: the population is the size this tree holds" \
	"$(if [ "$n_narrow" -ge 30 ]; then echo yes; else echo "no ($n_narrow, want >= 30)"; fi)" "yes"

# NOT IN ITS OWN POPULATION, asserted rather than assumed. Measured the other
# way first: without the exclusion the sweep runs itself once per suite, 20
# minutes in before anything else has run.
#
# THE FIRST VERSION OF THIS ARM COULD NOT FAIL (@jdatcmd, review). It grepped the
# population for `$PGC_CAP_SELF` -- the same variable the producer had just
# skipped on -- so it was 0 by construction, and it was blind to the case it
# exists for: reached under another name, the skip missed the real file, the
# file re-entered the population, and the arm still read 0 because it looked for
# the name that was SKIPPED rather than the file that was THERE.
#
# Two arms now, neither reusing that variable. The first is a LITERAL, which no
# invocation can rename; the second asks whether any member IS this file, by
# realpath, which is the property the exclusion is about.
check_num "the sweep is not in the population it sweeps" \
	"$(printf '%s\n' "$NARROW" | grep -c '^capability_sweep\.sh$')" "0"
check_num "and no member of the population is this very file" \
	"$(_cap_n=0
	   for _cap_b in $NARROW; do
		[ "$(realpath "$TESTDIR/$_cap_b" 2>/dev/null)" = "$PGC_CAP_SELF_PATH" ] &&
			_cap_n=$((_cap_n + 1))
	   done
	   echo "$_cap_n")" "0"

# ---- the shim, and its own premises before anything uses it ----------------
#
# A directory first on PYTHONPATH holding a pyarrow.py that raises ImportError.
# IF THE MASK FAILS, BOTH RUNS ARE THE PRESENT RUN: every name set matches
# trivially and this sweep passes on every suite including a broken one. Its
# pass and its vacuity look identical, so the mask is asserted here and again
# per suite below (@jdatcmd, #1185 review).
#
# The middle two arms matter most: a shim that broke python3 generally would
# report every suite as shrinking, with total confidence.
SHIM="$(mktemp -d)"
printf 'raise ImportError("pyarrow masked by test/capability_sweep.sh")\n' > "$SHIM/pyarrow.py"

cap_rc() {	# cap_rc PYTHONPATH_VALUE CODE -> exit status
	if [ -n "$1" ]; then
		PYTHONPATH="$1" python3 -c "$2" >/dev/null 2>&1
	else
		python3 -c "$2" >/dev/null 2>&1
	fi
	echo "$?"
}

check_num "the shim makes pyarrow unimportable" \
	"$(cap_rc "$SHIM" 'import pyarrow')" "1"
check_num "and pyarrow.parquet with it" \
	"$(cap_rc "$SHIM" 'import pyarrow.parquet')" "1"
check_num "premise: while python itself still runs under the shim" \
	"$(cap_rc "$SHIM" 'print(2 + 2)')" "0"
check_num "premise: and the standard library still imports under it" \
	"$(cap_rc "$SHIM" 'import json, sys, os, csv')" "0"
check_num "premise: and pyarrow IS importable without the shim, so absence is the shim's doing" \
	"$(cap_rc "" 'import pyarrow')" "0"

# ---- the verdict, a pure function so the selftest can drive it -------------
#
# Three inputs and four outcomes. It takes the numbers rather than the logs so it
# can be exercised without running a suite, the same reason pgc_freshness_verdict
# and pgc_build_needs_clean take theirs.
#
# DECLINED IS ITS OWN OUTCOME (#1215, the owner's rule of 2026-10-12): a missing
# capability may be bypassed as a SKIP, but no test may be silent. A suite that
# loses its names and records a SKIP naming the capability has said what it did
# not run; a suite that loses them and records nothing has not. Folding the first
# into `silent` reddened the sweep on the 25 suites the rule allows to skip, and
# folding it into `preserved` would hide them. So the two stay apart, and `silent`
# must still be zero.
pgc_capability_verdict() {	# pgc_capability_verdict LOST FAILS NAMED_SKIPS -> verdict
	local lost="${1:-}" fails="${2:-}" skips="${3:-}"
	case "$lost" in ''|*[!0-9]*) echo unknown; return ;; esac
	case "$fails" in ''|*[!0-9]*) echo unknown; return ;; esac
	case "$skips" in ''|*[!0-9]*) echo unknown; return ;; esac
	if [ "$lost" -eq 0 ]; then
		echo preserved
	elif [ "$fails" -ge 1 ]; then
		echo refused
	elif [ "$skips" -ge 1 ]; then
		echo declined
	else
		echo silent
	fi
}

# Its four cases are asserted by selftest part 610, which evals this text: they
# need no suite run, and a sweep built on a verdict that misfiled one case would
# count it wrongly thirty times over and still reconcile.

cap_names() {	# cap_names LOGFILE -> the recorded check names, sorted
	grep -oP '^RESULT\t[^\t]*\t[^\t]*\t\K[^\t]*' "$1" 2>/dev/null | LC_ALL=C sort -u
}

# ---- the sweep -------------------------------------------------------------
#
# One unskipped priming run first, so PGC_SKIP_BUILD below is honest. Without it
# every suite dies on "the binary under test was not built from this source" --
# the binary current, the stamp stale -- and 60 runs report a defect that is not
# there.
echo "-- priming the build so PGC_SKIP_BUILD is honest"
unset PGC_SKIP_BUILD
prime_log="$(mktemp)"
( cd "$SRCDIR" && bash "$TESTDIR/smoke.sh" "$PG_CONFIG" ) > "$prime_log" 2>&1
prime_rc=$?
check_num "premise: the priming run built and installed cleanly" "$prime_rc" "0"

n_preserved=0
n_refused=0
n_declined=0
n_silent=0
silent_names=""
declined_names=""
mask_failures=0

for s in $NARROW; do
	stem="${s%.sh}"
	w_log="$(mktemp)"
	o_log="$(mktemp)"
	( cd "$SRCDIR" && PGC_SKIP_BUILD=1 timeout 900 bash "$TESTDIR/$s" "$PG_CONFIG" ) > "$w_log" 2>&1
	( cd "$SRCDIR" && PGC_SKIP_BUILD=1 PYTHONPATH="$SHIM" timeout 900 bash "$TESTDIR/$s" "$PG_CONFIG" ) > "$o_log" 2>&1
	# THE MASK, RE-ASSERTED IN THE SAME ENVIRONMENT THE SUITE JUST RAN IN. A
	# PYTHONPATH that did not survive into the suite's own python would make
	# both runs the present run.
	[ "$(cap_rc "$SHIM" 'import pyarrow')" = 1 ] || mask_failures=$((mask_failures + 1))
	lost="$(LC_ALL=C comm -23 <(cap_names "$w_log") <(cap_names "$o_log") | grep -c .)"
	fails="$(grep -cP '^RESULT\t.*\tFAIL\t' "$o_log")"
	# A SKIP COUNTS ONLY IF ITS REASON NAMES THE CAPABILITY, read from the record
	# rather than from the exit code: exit 66 also means a major without the
	# feature, which is not this.
	skips="$(awk -F'\t' '$1 == "RESULT" && $5 == "SKIP" && $7 ~ /pyarrow/' "$o_log" | grep -c .)"
	case "$(pgc_capability_verdict "$lost" "$fails" "$skips")" in
		preserved) n_preserved=$((n_preserved + 1)) ;;
		refused)   n_refused=$((n_refused + 1)) ;;
		declined)  n_declined=$((n_declined + 1)); declined_names="$declined_names $s" ;;
		*)         n_silent=$((n_silent + 1)); silent_names="$silent_names $stem" ;;
	esac
	rm -f "$w_log" "$o_log"
done

echo "-- preserved=$n_preserved refused=$n_refused declined=$n_declined silent=$n_silent of $n_narrow"
[ -n "$silent_names" ] && echo "-- silent:$silent_names"

check_num "the mask held for every run in the sweep" "$mask_failures" "0"
check_num "premise: every suite in the population was classified" \
	"$((n_preserved + n_refused + n_declined + n_silent))" "$n_narrow"
check_num "no suite loses a check without recording a skip or a refusal" \
	"$n_silent" "0"

# THE SUITES THAT DECLINE ARE THE SUITES THAT ASK TO, derived from the source and
# compared as SETS. A count would let a suite that stopped declining trade places
# with one that started; a literal 25 would go stale the day a suite is added. A
# suite that stops calling pgc_skip pyarrow leaves this side, and must then read
# preserved, or the silent arm above catches it.
DECLINERS="$(for _cap_b in $NARROW; do
	# grep -c, NOT grep -q: -q exits at the first match, sed dies of SIGPIPE, and
	# under pipefail the pipeline fails, so a suite that DOES call pgc_skip drops
	# out at random. Measured here: three of 25 dropped on one run. The header of
	# this file records the same race for the population itself.
	[ "$(sed 's/#.*//' "$TESTDIR/$_cap_b" | grep -cE 'pgc_skip[[:space:]]+"?pyarrow')" -gt 0 ] && printf '%s\n' "$_cap_b"
done | LC_ALL=C sort)"
check_num "premise: some suites in the population call pgc_skip pyarrow" \
	"$([ -n "$DECLINERS" ] && echo 1 || echo 0)" "1"
_cap_diff="$(LC_ALL=C comm -3 <(printf '%s\n' "$DECLINERS") \
	<(printf '%s\n' $declined_names | LC_ALL=C sort) | tr -d '\t' | grep .)"
[ -n "$_cap_diff" ] && echo "-- decline mismatch: $(printf '%s ' $_cap_diff)"
check_num "the suites that decline are exactly the suites that call pgc_skip pyarrow" \
	"$(printf '%s\n' "$_cap_diff" | grep -c .)" "0"

rm -rf "$SHIM" "$prime_log"
pgc_summary
