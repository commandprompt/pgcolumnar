#!/usr/bin/env bash
#
# The benchmark's own guards (#465).
#
# bench/run_clickbench.sh publishes numbers. A benchmark that reports a failed
# load as a fast one is worse than a benchmark that does not run, because the
# number reaches documentation and nobody re-derives it.
#
# That is not hypothetical. pgcolumnar.parallel_copy prepares one transaction per
# worker, and the stock max_prepared_transactions is 0, so EVERY parallel arm
# errors out instantly. The first harness written against it printed those
# failures as
#
#     0.0s / 0.8s / 1.1s / 1.3s
#
# which is indistinguishable from perfect scaling, and is the shape a reader
# would publish. #465 records it.
#
# So the guards get a suite of their own, and it runs in the matrix even though
# the benchmark it guards does not: the benchmark needs a 15 GB download and a
# tuned cluster, while its arithmetic needs neither. Testing the decision without
# the dataset is the whole point of putting it in a file that can be sourced.
#
# Usage:  test/bench_guards.sh [PG_CONFIG]
# The argument is accepted and ignored; this suite needs no cluster.
# Written fresh for pgColumnar.
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
SRCDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# No cluster needed; pgc_setup is skipped deliberately, as in wal_envelope.sh. The
# checks go through lib.sh's check() so each one is a RESULT record the ledger and
# the pytest twin can name (#1341); the suite used to keep its own tally, which
# emitted none. PGC_MAJOR comes from the pg_config every suite is handed, and
# degrades to `unknown` rather than to a guessed major when there is none.
PGC_MAJOR="$(pgc_major_of "${1:-}" 2>/dev/null)"
PGC_CHECKS=0
PGC_FAIL=0

GUARDS="$SRCDIR/bench/cb_guards.sh"
check "premise: bench/cb_guards.sh is present to be judged" "$([ -f "$GUARDS" ] && echo yes || echo no)" "yes"
[ -f "$GUARDS" ] || pgc_summary
# shellcheck source=/dev/null
. "$GUARDS"

echo "== pgColumnar test: $(basename "$0") =="

# ---- the non-assert requirement must be ENFORCED, not merely documented -----
#
# Every bench script's header said to run against a non-assert build. They said
# so and checked nothing, and the invocations that produced the #755, #768, #752
# and #753 figures passed an --enable-cassert prefix. A header that states a
# requirement the script does not enforce is the same shape as selftest 080 not
# scanning bench/: the rule existed, the check did not.
#
# Asserted by RUNNING each runner's own guard, not by grepping for its words
# (#1341). The guard is the block from the runner's --configure capture through
# the `fi` that closes its ALLOW_CASSERT test, read with comments removed, and it
# is run against a stand-in pg_config that reports an assert build, a plain one,
# or nothing. Running the whole runner is not an option: past its guard it
# builds, loads and downloads.
#
# The greps this replaces passed with the refusal gone. Every runner's header
# mentions --enable-cassert, and the cannot-tell block's `exit 1` sits within 24
# lines of that mention, so with the assert branch's `exit 1` turned into `true`,
# with the empty-capture `exit 1` removed, with every build refused, or with the
# override ignored, main's suite passed all 60 checks. Measured on run_bench.sh.
#
# EVERY assertion still keys on shared TEXT, never on a prefix: run_profile.sh
# uses PROFILE_* where the other five use BENCH_*, so the override variable is
# read from the runner's own guard rather than spelled here.
_fake="$(mktemp -d)"
for _mode in cassert plain empty; do
	{
		echo '#!/bin/sh'
		echo 'case "$1" in'
		case "$_mode" in
			cassert) echo "  --configure) echo \"'--prefix=/nonexistent' '--enable-cassert'\" ;;" ;;
			plain) echo "  --configure) echo \"'--prefix=/nonexistent'\" ;;" ;;
			empty) echo "  --configure) exit 1 ;;" ;;
		esac
		echo '  --bindir) echo /nonexistent/bin ;;'
		echo '  --version) echo "PostgreSQL 17.0" ;;'
		echo '  *) echo /nonexistent ;;'
		echo 'esac'
	} >"$_fake/pg_config_$_mode"
	chmod +x "$_fake/pg_config_$_mode"
done
guard_of() {	# guard_of RUNNER -> its assert guard as code, comments removed, or nothing
	# Only a COMPLETE guard is returned: the capture, an ALLOW_CASSERT test and the
	# `fi` closing it, within 60 code lines. Anything else returns nothing, which
	# the checks below refuse. Returning a partial block would run whatever follows
	# it in the runner -- the build, the load, the download -- and with the assert
	# test removed it once returned the rest of run_bench.sh.
	awk '/^[[:space:]]*#/ {next}
		!on && /--configure/ && /PG_CONFIG/ {on=1}
		on {buf = buf $0 "\n"; n++}
		on && /ALLOW_CASSERT/ {seen=1}
		on && seen && /^fi$/ {printf "%s", buf; exit}
		n > 60 {exit}' "$1"
}
run_guard() {	# run_guard RUNNER MODE [ALLOW] -> "rc=N" then the guard's output
	local block var out rc
	block="$(guard_of "$1")"
	var="$(grep -oE '[A-Z_]*ALLOW_CASSERT' <<<"$block" | head -1)"
	out="$(env -i PATH=/usr/bin:/bin PG_CONFIG="$_fake/pg_config_$2" ${3:+"${var:-NO_OVERRIDE_FOUND}=1"} \
		bash -c "set -uo pipefail; $block" 2>&1)"
	rc=$?
	echo "rc=$rc"
	echo "$out"
}
for _bp in "$SRCDIR/bench/"run_*.sh; do
	_bs="$(basename "$_bp")"
	_asrt="$(run_guard "$_bp" cassert)"
	_ovrd="$(run_guard "$_bp" cassert allow)"
	_pln="$(run_guard "$_bp" plain)"
	_none="$(run_guard "$_bp" empty)"
	check "[$_bs] refuses an assert build rather than documenting the requirement" \
		"$(grep -q 'is an --enable-cassert build' <<<"$_asrt" && echo yes || echo "no: $(head -c 200 <<<"$_asrt" | tr '\n' ' ')")" "yes"
	check "[$_bs] and offers a named override rather than only refusing" \
		"$(head -1 <<<"$_ovrd")/$(grep -c 'WARNING' <<<"$_ovrd")" "rc=0/1"
	check "[$_bs] and exits non-zero rather than warning past it" "$(head -1 <<<"$_asrt")" "rc=1"
	check "[$_bs] and lets a non-assert build through" \
		"$(head -1 <<<"$_pln")/$(grep -c 'FATAL' <<<"$_pln")" "rc=0/0"
	# "not an assert build" and "could not tell" are different answers and only
	# one of them is a pass: a missing or failing pg_config must be refused.
	check "[$_bs] refuses when it CANNOT TELL, rather than treating that as a pass" \
		"$(head -1 <<<"$_none")/$(grep -c 'produced no --configure output' <<<"$_none")" "rc=1/1"
	check "[$_bs] and does not decide from an unbuffered pipe into grep -c" \
		"$([ "$(guard_of "$_bp" | grep -cE '\$\("\$PG_CONFIG" --configure[^)]*\| *grep -c')" -eq 0 ] && echo yes || echo no)" "yes"
done
rm -rf "$_fake"

# The control: the scan has to be reading files that exist, or four greps over
# nothing would report compliance.
# EVERY runner, derived from the directory rather than a hand-kept list: a new
# bench script must not be able to arrive without this guard just because nobody
# remembered to add it here. Proved by adding one -- a fresh runner with the
# guard stripped is named by the loop above.
#
# A LOWER BOUND, not an equality. The premise's job is to show the scan read
# something rather than globbing nothing; an equality would turn a legitimate new
# benchmark into a red here, which is a check that punishes the thing it exists
# to cover.
_bench_runners="$(ls "$(dirname "${BASH_SOURCE[0]}")/../bench/"run_*.sh 2>/dev/null | grep -c .)"
check "premise: every bench runner was read, not a hand-kept subset" \
	"$([ "${_bench_runners:-0}" -ge 6 ] && echo yes || echo "no (found $_bench_runners)")" "yes"

# ---- max_prepared_transactions must be preflighted, not discovered ----------
#
# Raising it needs a postmaster restart, so finding out during the load means the
# whole run is wasted. The guard has to answer before any arm is loaded.
check "the stock 0 is refused for an 8-worker parallel arm" \
	"$(cb_prepared_xacts_ok 0 8 && echo ok || echo refused)" "refused"
check "an exact match is accepted" \
	"$(cb_prepared_xacts_ok 8 8 && echo ok || echo refused)" "ok"
check "more than enough is accepted" \
	"$(cb_prepared_xacts_ok 16 8 && echo ok || echo refused)" "ok"
check "one short is refused, which is the off-by-one that would run 7 workers" \
	"$(cb_prepared_xacts_ok 7 8 && echo ok || echo refused)" "refused"

# A serial arm needs none, and must not be blocked by a setting it does not use.
check "a serial arm needs no prepared transactions" \
	"$(cb_prepared_xacts_ok 0 0 && echo ok || echo refused)" "ok"

# ---- max_worker_processes, the other setting that costs a restart -----------
#
# parallel_copy registers one background worker per loader, plus a coordinator,
# and the logical replication launcher already holds one slot. So an N-worker arm
# needs N + 2, not N. The stock default is 8, which is why an 8-worker arm fails
# at "could not register pgcolumnar parallel_copy loader 7 of 8" and leaves an
# EMPTY table -- a fast, wrong, publishable-looking result.
#
# N + 2 is measured, not reasoned. Sweeping max_worker_processes against three
# worker counts on the bench, the smallest value that loaded every row was:
#
#     workers 2 -> 4      workers 4 -> 6      workers 8 -> 10
#
# and one below each failed on the LAST loader with the table left empty.
check "the stock 8 is refused for an 8-worker arm, which is the case that bit us" \
	"$(cb_worker_slots_ok 8 8 && echo ok || echo refused)" "refused"
check "N + 1 is still refused: the coordinator needs a slot too" \
	"$(cb_worker_slots_ok 9 8 && echo ok || echo refused)" "refused"
check "N + 2 is accepted, the measured minimum" \
	"$(cb_worker_slots_ok 10 8 && echo ok || echo refused)" "ok"
check "more than enough worker slots are accepted" \
	"$(cb_worker_slots_ok 32 8 && echo ok || echo refused)" "ok"
check "the rule holds at another worker count (4 needs 6)" \
	"$(cb_worker_slots_ok 6 4 && echo ok || echo refused)" "ok"
check "and one below it does not" \
	"$(cb_worker_slots_ok 5 4 && echo ok || echo refused)" "refused"

# A serial arm registers no workers at all.
check "a serial arm needs no worker slots" \
	"$(cb_worker_slots_ok 0 0 && echo ok || echo refused)" "ok"

# Non-numeric input must be refused rather than compared. A psql that failed
# yields an empty string, and "" -ge "" is not a comparison anyone wants.
check "a missing current value is refused, not compared" \
	"$(cb_worker_slots_ok "" 8 && echo ok || echo refused)" "refused"

wmsg="$(cb_worker_slots_message 8 8)"
check "the worker-slot message names the setting" \
	"$([ "$(grep -c 'max_worker_processes' <<<"$wmsg")" -ge 1 ] && echo yes || echo no)" "yes"
check "and the value it must reach, not merely the worker count" \
	"$([ "$(grep -c '10' <<<"$wmsg")" -ge 1 ] && echo yes || echo no)" "yes"
check "and says it needs a restart" \
	"$([ "$(grep -ci 'restart' <<<"$wmsg")" -ge 1 ] && echo yes || echo no)" "yes"

# The message is the deliverable here: the operator has to know WHAT to set and
# that it costs a restart. A bare "failed" sends them to the load log, which
# reports a per-worker error and not the cause.
msg="$(cb_prepared_xacts_message 0 8)"
check "the message names the setting to change" \
	"$([ "$(grep -c 'max_prepared_transactions' <<<"$msg")" -ge 1 ] && echo yes || echo no)" "yes"
check "and the number it must reach" \
	"$([ "$(grep -c '8' <<<"$msg")" -ge 1 ] && echo yes || echo no)" "yes"
check "and says it needs a restart, which is why this runs first" \
	"$([ "$(grep -ci 'restart' <<<"$msg")" -ge 1 ] && echo yes || echo no)" "yes"

# ---- a load that lost rows is a failure, not a fast result ------------------
check "a short load is refused" \
	"$(cb_rows_ok 1999999 2000000 && echo ok || echo refused)" "refused"
check "an empty load is refused, which is what an errored parallel arm produces" \
	"$(cb_rows_ok 0 2000000 && echo ok || echo refused)" "refused"
check "an exact load is accepted" \
	"$(cb_rows_ok 2000000 2000000 && echo ok || echo refused)" "ok"
# Empty is not zero. A psql that failed produces neither.
#
# BOTH sides empty is the case that matters, and it is the only one of the three
# that a plain `[ "$got" = "$want" ]` gets wrong: one empty side is unequal to a
# number and is refused either way. The first version of this suite asserted only
# the one-sided cases, and a removal proof showed they passed with the numeric
# check deleted, which means they were testing nothing. That is the trap #418
# exists to forbid, met while writing the test for it.
check "two missing measurements are refused, not called equal" \
	"$(cb_rows_ok '' '' && echo ok || echo refused)" "refused"
check "a missing count is refused rather than compared with the expectation" \
	"$(cb_rows_ok '' 2000000 && echo ok || echo refused)" "refused"
check "and a missing expectation is refused too" \
	"$(cb_rows_ok 2000000 '' && echo ok || echo refused)" "refused"

# ---- a report must not claim a cold run it did not perform (#506) ----------
#
# The old harness tested [ -w /proc/sys/vm/drop_caches ], fell back to sudo, and
# otherwise did nothing at all, silently -- while the report printed the
# lukewarm-cold-run tag unconditionally. An unprivileged container is exactly
# such a host: the file belongs to a uid outside the namespace and refuses even
# the container's own root. So the run published a protocol claim it had not met.
#
# The claim is the deliverable being guarded here, not the drop. Whether a given
# kernel permits the drop is not something a matrix suite can decide; whether the
# report is honest about what happened is pure arithmetic, and belongs here.
tag_none="$(cb_cold_tag none 2>/dev/null)"
# Assert the premise, because the negative check below is vacuous without it: an
# absent cb_cold_tag yields an empty string, an empty string does not contain
# "lukewarm-cold-run", and "the run was not called cold" therefore PASSES against
# no implementation whatever. Seen, not reasoned about -- it passed exactly that
# way on the first red run of this suite.
check "the guard exists and emitted a tag to judge" \
	"$([ -n "$tag_none" ] && echo yes || echo no)" "yes"
check "a host that could not drop the page cache is not called a cold run" \
	"$(grep -qi 'lukewarm-cold-run' <<<"$tag_none" && echo claimed || echo not-claimed)" \
	"not-claimed"
# Matched case-sensitively and as WARM-RUN rather than as "warm": the string
# "lukewarm-cold-run" contains "warm", so a loose match would pass on precisely
# the wrong output this check exists to catch.
check "and the tag says plainly that the run was warm" \
	"$(grep -q 'WARM-RUN' <<<"$tag_none" && echo yes || echo no)" "yes"

# ---- a win is decided on the times, not on the printed string (#531) --------
#
# The defect: the report counted wins from the "%.2f" ratio it had already
# formatted for the table, so columnar ahead by less than half a percent printed
# 1.00, failed `r < 1`, and was counted a LOSS -- against the legend printed
# directly above it. The 2026-08-09 run reported 33 wins and 10 losses where the
# times give 36 and 7.
#
# The three numbers below are that run's q13, the smallest real case: heap
# 1588.095 ms, columnar 1582.859 ms, and a warm-try scatter of 0.99 percent.
check "the verdict helpers are present to be judged" \
	"$(type -t cb_verdict)/$(type -t cb_warm_spread)/$(type -t cb_band)" \
	"function/function/function"

check "a clear win is a win" "$(cb_verdict 500 1000 0.02)" "win"
check "a clear loss is a loss" "$(cb_verdict 2000 1000 0.02)" "loss"
# The regression case, stated as the defect: rounding to 1.00 must not flip the
# sign of the verdict. With no band this is a win; it must never be a loss.
check "columnar ahead by 0.33 percent is not a LOSS, which is the #531 defect" \
	"$(cb_verdict 1582.859 1588.095 0.0000)" "win"
check "and under a band wider than the gap it is a tie, not a win either" \
	"$(cb_verdict 1582.859 1588.095 0.0099)" "tie"
check "a gap just outside the band is still called" \
	"$(cb_verdict 900 1000 0.05)" "win"
check "and a loss just outside it likewise" \
	"$(cb_verdict 1100 1000 0.05)" "loss"

# Scatter is measured from the tries, and an unknown scatter must not read as
# zero: "-" through cb_band must suppress the verdict rather than license a
# strict comparison on an arm nobody measured twice.
check "spread is the fractional range over the best" "$(cb_warm_spread 100 103)" "0.0300"
check "order does not matter, and ERR tries are dropped" \
	"$(cb_warm_spread 103 100 ERR)" "0.0300"
check "a single warm try has no measurable scatter" "$(cb_warm_spread 100)" "0.0000"
check "no usable try yields no spread, not zero" "$(cb_warm_spread '' ERR)" "-"
check "a zero best is refused rather than divided by" "$(cb_warm_spread 0 5)" "-"
check "the band is the wider of the two arms" "$(cb_band 0.0100 0.0290)" "0.0290"
check "and is unknown if either arm is unknown" "$(cb_band - 0.0290)" "-"
check "an unknown band suppresses the verdict, it does not strict-compare" \
	"$(cb_verdict 1582.859 1588.095 -)" "-"
check "an errored arm has no verdict" "$(cb_verdict ERR 1000 0.02)" "-"
check "and neither does a zero baseline" "$(cb_verdict 500 0 0.02)" "-"

# ---- and the report must ASK the guard, not merely have one (#531) ---------
#
# The checks above prove cb_verdict's arithmetic. None of them proves that
# bench/run_clickbench.sh asks it anything. This suite sources bench/cb_guards.sh
# and never reads the benchmark, so the call site is invisible to it: restoring
# the #531 defect verbatim,
#
#     r1=$(ratio "$c" "$h")
#     if [ "$(awk -v r="$r1" 'BEGIN { print (r < 1) ? 1 : 0 }')" = 1 ] ...
#
# leaves every check above PASSING. That is measured and not reasoned about --
# the revert was applied to a clean tree and the suite ran 45/45 green before
# these four checks existed.
#
# These are checks over source text, which is the weaker kind, and they are here
# because the behaviour they guard needs the 15 GB download and the tuned cluster
# this suite exists to do without. The alternative is not a better test; it is no
# test, which is what the 45 above amounted to at this seam.
CB="$SRCDIR/bench/run_clickbench.sh"
check "premise: the benchmark script is present to be judged" \
	"$([ -f "$CB" ] && echo yes || echo no)" "yes"

# Matched as a WHOLE name (#1341): `r[12]=` alone also matched `rr1=`, so with
# the variables renamed this premise passed and the check below read nothing.
# Without this premise the next check is vacuous. Rename r1 and "no ratio value
# decides anything" passes against a report that has gone back to deciding on one
# under another name -- the grep finds nothing and nothing is what it approves.
check "premise: the report still captures ratio() into a display variable" \
	"$([ "$(grep -cE '(^|[^[:alnum:]_])r[12]=\$\(ratio' "$CB")" -ge 1 ] && echo yes || echo no)" "yes"

check "ratio()'s output reaches printf and nothing else" \
	"$(grep '\$r[12]' "$CB" | grep -vc 'printf')" "0"

# Matched as a CALL and not as a mention. `grep -c 'cb_verdict'` counts any
# occurrence, including one in a comment, and that is enough to approve a report
# that has gone back to deciding inline: measured, the verdict was recomputed on
# $c and $h directly with only a "might reinstate cb_verdict here" comment left
# behind, and this suite reported 49 checks PASSED. Deciding on $c and $h rather
# than on $r1 satisfies the check above at the same time, so both fall together.
#
# The realistic path is not somebody being clever. It is somebody refactoring the
# loop and leaving a TODO that names the function.
#
# `cb_verdict "` is the shape a call takes here and a mention almost never does.
# Measured on the bypassed tree: loose 1 (approves), tight 0 (rejects); on the
# good tree tight is 1 and still approves.
# Read as CODE: a comment that keeps the call's shape is not a call. With the
# verdict recomputed inline and `# was: case "$(cb_verdict "$c" ...` left behind,
# main's suite passed this check (#1341).
check "and the verdict is asked of cb_verdict rather than recomputed inline" \
	"$([ "$(grep -vE '^[[:space:]]*#' "$CB" | grep -c 'cb_verdict "')" -ge 1 ] && echo yes || echo no)" "yes"

# ---- the accelerator claim must not call a separation "variation" (#565) ----
#
# docs/benchmarks.md called q18 and q31 "run to run variation" while publishing
# them at 2.17x and 1.48x against a shared baseline, and claimed the plan was
# identical when EXPLAIN on the four-setting tuned arm shows a seven-worker
# parallel plan replaced by a serial vectorized node. Three checks, because the
# headline one goes vacuous once the sentence is removed.
DOC="$SRCDIR/docs/benchmarks.md"
check "premise: the benchmark page is present to be judged" \
	"$([ -f "$DOC" ] && echo yes || echo no)" "yes"

# 1. Frozen fixture. The extractor must find both ids in a claim wrapped across
# lines, so it stays proven after the real page no longer carries the sentence.
# Without this the headline check below is satisfiable by an extractor that
# matches nothing, which is exactly how the first draft of this guard passed.
_fx="$(mktemp)"
printf 'A lead sentence naming q18 and q31 here.\ntheir rows are therefore run to\nrun variation, not an effect.\n' > "$_fx"
check "the variation-claim extractor reads ids wrapped across two lines" \
	"$(cb_doc_variation_qids "$_fx" | tr '\n' ' ' | sed 's/ *$//')" "q18 q31"
rm -f "$_fx"

# 2. Headline. A query may be called "run to run variation" only if its own
# accelerator figures are a tie within the page's band. The page publishes no
# per-query scatter, so the band is '-' and cb_verdict returns '-', not "tie" --
# any such claim is unbanded and refused. Fires on the unfixed page (q18, q31),
# passes once the sentence is gone.
_bad=""
for _q in $(cb_doc_variation_qids "$DOC"); do
	set -- $(cb_doc_accel_figures "$DOC" "$_q")
	_v="$(cb_verdict "${2:-}" "${1:-}" "$(cb_band - -)")"
	[ "$_v" = tie ] || _bad="$_bad $_q(${1:-?}/${2:-?}:$_v)"
done
_nclaim="$(cb_doc_variation_qids "$DOC" | grep -c .)"
echo "  variation claims examined: $_nclaim"
check "no query is called run-to-run variation unless its figures are a measured tie" \
	"claims=$_nclaim$_bad" "claims=0"

# 3. Census, which never goes vacuous. The tuned arm sets FOUR settings; the page
# must name all four and the group-cap value it sets, so it cannot be described
# as the two enable_* GUCs alone (the pre-fix text) or misstate the arm.
_sec="$(awk '/^### The accelerations are off by default/{f=1; next} f&&/^#/{f=0} f' "$DOC")"
_miss=""
for _t in pgcolumnar.enable_group_vectorization pgcolumnar.enable_ungrouped_vector_agg \
	pgcolumnar.enable_parallel_vector_agg pgcolumnar.groupagg_max_groups 200000000; do
	grep -qF "$_t" <<<"$_sec" || _miss="$_miss $_t"
done
echo "  tuned-arm settings named on the page:$([ -z "$_miss" ] && echo ' all four + value' || echo "$_miss MISSING")"
check "the tuned-arm section names all four settings and the group-cap value" \
	"missing:$_miss" "missing:"

pgc_summary
