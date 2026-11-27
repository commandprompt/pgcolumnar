# ---- the sanitizer subset must cover the C-level encoding selftest -----------
#
# test/run_san.sh runs a SUBSET of the suites, and a suite that is not in it is
# not sanitized -- silently, since nothing reports the omission.
#
# encode_invariants is the only suite that drives pgcolumnar_debug_encoding_selftest,
# which exercises bitunpack at every width 1..64 across counts 1,2,3,7,8,9,17,64,129
# plus a derived count per width. That matters because it is the only fixture that
# crosses bitunpack's fast/tail boundary in both directions: nFast is 0 until the
# encoded body reaches nine bytes, so small counts are all tail. Measured with a
# probe build, counting backends that reached the tail loop:
#
#     encode_invariants  21
#     differential        0
#
# differential's chunks are large enough that nFast == n throughout, so a
# sanitizer pass that includes differential and not encode_invariants covers one
# of bitunpack's two paths -- over exactly the code #514 rewrote.
#
# Asked as "every suite that drives the selftest" rather than by name, so moving
# the selftest to another suite cannot quietly narrow this.
# Ask the runner rather than parsing the source (CONTEXT.md, #473) -- but with
# PGC_SAN_SUITES cleared, because the claim under test is about the SHIPPED
# DEFAULT, not about whatever an operator overrode it with for one run.
#
# Both halves are load-bearing and each fixes a different defect. Asking the
# runner means this cannot drift from what run_san.sh actually iterates. Clearing
# the variable means a developer with an override exported does not get a red
# from a check that is not about their override -- which is what a plain
# --list-suites here produces, verified: with PGC_SAN_SUITES='smoke differential'
# the runner reports no encode_invariants and this check would fail while the
# shipped default is perfectly correct.
_san_suites="$(env -u PGC_SAN_SUITES bash "$PGC_SRCDIR/test/run_san.sh" --list-suites 2>/dev/null)"
check "premise: run_san.sh's default subset was found and is non-empty" \
	"$([ -n "$_san_suites" ] && echo yes || echo no)" "yes"

_san_missing=""
_san_drivers=0
for _f in "$PGC_SRCDIR"/test/*.sh; do
	# No self-skip. This block used to live in test/harness_selftest.sh, which
	# this loop globs, so it matched itself -- the same self-match that makes
	# `pgrep -f <pattern>` find its own command line. It now lives in
	# test/selftest/, which `test/*.sh` does not glob, so the searcher is no
	# longer in the searched set. The skip is removed rather than left in place
	# because a condition that can never be true is a check that can never fail,
	# and the differential in #554 proves _san_drivers is unchanged by the move.
	grep -q 'debug_encoding_selftest' "$_f" || continue
	_san_drivers=$((_san_drivers + 1))
	_b="$(basename "$_f" .sh)"
	grep -qw "$_b" <<<"$_san_suites" || _san_missing="$_san_missing $_b"
done
check "premise: at least one suite drives the C-level encoding selftest" \
	"$([ "$_san_drivers" -ge 1 ] && echo yes || echo no)" "yes"

check "the sanitizer subset runs every suite that drives the encoding selftest" \
	"$(printf '%s' "$_san_missing" | sed 's/^ //')" ""


# export_sink, BY NAME (#2201). Its check "fatal: no backend died by a signal
# during the FATAL arms" is the only detector of mutant m3 from the #2142
# reviews: a local sink listed before AllocateFile, so a failed open rolled back
# to a savepoint leaves a freed sink listed for the next FATAL to walk. A PG 18
# release build stays green under m3 (5 of 5 runs); an ASan build reports it.
# So the suite is worth running only on an instrumented build, and this subset
# is where that happens. Named rather than derived: no property of the suite's
# text says "only a sanitizer sees this".
#
# BOTH GATES, because the nightly runs test/pytest/run_san.py, not run_san.sh.
# Reading run_san.sh alone left run_san.py free to drop the suite with this
# check green. run_san.py is asked the same way, through its own --list-suites
# with the override cleared; it needs no build to answer. A run_san.py that
# cannot answer prints nothing, and the check then names it as missing.
_san_py_suites="$(env -u PGC_SAN_SUITES python3 "$PGC_SRCDIR/test/pytest/run_san.py" --list-suites 2>/dev/null)"
_es_missing=""
grep -qx export_sink <<<"$_san_suites" || _es_missing="$_es_missing run_san.sh"
grep -qx export_sink <<<"$_san_py_suites" || _es_missing="$_es_missing run_san.py"
check "the sanitizer subset runs export_sink, whose freed-sink mutant only an instrumented build sees" \
	"$(printf '%s' "${_es_missing:- none}" | sed 's/^ //')" "none"

# ---- NOT a check: FSST slack coverage, and why there is no arm for it --------
#
# decode_fsst_shared stores each symbol as one fixed 8-byte write and allocates
# FSST_DECODE_SLACK bytes past rawLen so the tail of that write stays inside the
# allocation (#768). Nothing in the ordinary matrix can see the slack removed:
# the over-write lands 1 to 7 bytes past a palloc'd chunk, which does not fault
# and changes no answer. The sanitizer catches it precisely -- with the slack
# deleted, write_fsst_compressed reports
# `heap-buffer-overflow ... columnar_encoding.c in decode_fsst_shared`.
#
# I wrote an arm here requiring the subset to keep a suite that drives FSST, and
# then removed it, because it cannot fail. `encode_effort = full` is the DEFAULT,
# so FSST decoding is exercised by essentially every suite that stores text, and
# the subset holds many: native_encoding, write_fsst_compressed, differential,
# arrow_export among them. Measured: deleting all three of the suites I first
# named from the subset left the check green, because others still matched.
#
# So the coverage is not at risk in the way an arm here would guard, and a check
# that cannot go red is worse than none -- it reads as protection. The comment
# stays because the REASON matters: if the sanitizer subset ever narrowed to
# suites that store no varlena data, the slack would lose its only detector.
