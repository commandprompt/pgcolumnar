# ---- the detection primitive itself -----------------------------------------
# The assertions above are invariants: they hold even with the identity check
# removed, because a squatter holding the port makes bind genuinely fail and the
# retry happens anyway. They do not, on their own, prove the guard works. The
# case the guard exists for is pg_ctl -w reporting success while another
# postmaster owns the port, and that timing cannot be synthesised reliably here.
#
# So pin the mechanism directly instead: pointed at a foreign cluster,
# pgc_cluster_datadir must report *that* cluster, which is exactly what makes the
# comparison in pgc_setup reject it. If this reports our own directory, or
# nothing, the guard would wave a foreign cluster through.
_saved_port="$PGC_PORT"
PGC_PORT="$SQ_PORT"
_foreign="$(pgc_cluster_datadir)"
PGC_PORT="$_saved_port"

check "detection reports a foreign cluster's directory" \
	"$(pgc_norm_path "$_foreign")" "$(pgc_norm_path "$SQ_DIR/data")"
check "detection distinguishes it from ours" \
	"$([ "$(pgc_norm_path "$_foreign")" = "$(pgc_norm_path "$PGC_PGDATA")" ] \
		&& echo same || echo different)" "different"

# Drive the guard predicate itself, not just its inputs: it must accept our own
# cluster and reject the foreign one. This is the decision the start loop makes.
check "guard accepts our own cluster" \
	"$(pgc_cluster_is_ours && echo ours || echo foreign)" "ours"
_saved_port="$PGC_PORT"
PGC_PORT="$SQ_PORT"
_verdict="$(pgc_cluster_is_ours && echo ours || echo foreign)"
PGC_PORT="$_saved_port"
check "guard rejects a foreign cluster" "$_verdict" "foreign"

# ---------------------------------------------------------------------------
# Every suite must be registered in the matrix.
#
# A suite that run_all_versions.sh does not list is never run by any gate. It
# passes review, it sits in the tree, and the first change to the code under it
# breaks it silently. This has happened repeatedly: four consecutive PRs added a
# suite without registering it, and two older suites (native_reclaim_reconcile
# among them) had never been run by a gate at all.
#
# The allowlist is deliberately short and each entry needs a reason, because the
# easy way to satisfy this check is to add a name to it.
# ---------------------------------------------------------------------------

TESTDIR="$(cd "$PGC_TESTDIR" && pwd)"
RUNNER="$TESTDIR/run_all_versions.sh"

# Not suites: the two shared libraries (lib.sh, and portlib.sh which lib.sh and
# the standalone suites source for their port), the two runners, and the two developer helpers
# that build rather than test. build_all_versions compiles against every major
# and is run before merging a change that touches a version guard; it takes no
# cluster and reports per major, so the matrix cannot run it as a suite.
# native_scale was listed here as opt-in, for a row count the matrix "should not
# carry", and so no job ran it (#2018). Measured at its default 500000 rows on a
# heavily loaded 8-core box: 157 s and at most 120 MB of server memory. That is
# far inside a suites job, so it is a registered suite now. pg_upgrade takes two pg_configs (an old major and a new one)
# rather than one, so the matrix cannot invoke it the way it invokes a suite; it
# is a second gate run explicitly, like run_san.
# build_san builds the ASAN+UBSAN PostgreSQL and run_san is the
# sanitizer gate (#224): they are a separate instrumented build and its runner,
# not a suite the ordinary five-major matrix can carry.
not_a_suite() {
	case "$1" in
		lib|portlib|run_all_versions|build_all_versions|devloop|rebuild|build_san|run_san|run_coverage|pg_upgrade|extension_upgrade|format_downgrade|capability_sweep) return 0 ;;
		*) return 1 ;;
	esac
}

# the SUITES=( ... ) array, flattened to one name per line
# Ask the runner, rather than parsing its source. stderr is dropped because a
# runner carrying the stray-name mistake reports "command not found" on the way
# past it, which is the diagnosis and not this function's output.
#
# Asked ONCE, for the real runner, and cached. The checks below call this inside
# two loops over every test file, so the first version forked a fresh bash 250-odd
# times. Under a six-way matrix that is slow and, worse, fragile: a transient
# failure to fork returns an empty list, and an empty list reads as "that suite is
# unregistered". It did exactly that in the #473 matrix, failing on PG16 and PG17
# with four names each, different names each time, while PG15/18/19 passed. An
# intermittent red naming innocent suites is the worst kind, so the premise below
# makes an empty answer say what it is.
#
# THAT DIAGNOSIS WAS WRONG, or at best incomplete, and the caching did not cure
# the symptom it was written for. The real cause is this file's own `set -o
# pipefail` meeting a reader that exits early:
#
#     listed_suites | grep -qx "$name"
#
# `grep -q` returns the moment it matches, which closes the pipe while printf is
# still writing. printf then takes EPIPE and exits non-zero, and under pipefail
# the PIPELINE reports that failure even though grep matched -- so a registered
# suite is recorded as unregistered. It is a race between grep exiting and printf
# finishing, which is why it never reproduces locally, why it names innocent
# suites, and why it names DIFFERENT ones each run.
#
# Measured directly rather than reasoned about: 4,000 names, matching the first,
# 200 attempts. With pipefail, 18 false negatives. Without it, 0. It surfaced
# again on #476's CI (PG18) with "unregistered: parquet_nested_import" beside a
# "printf: write error: Broken pipe" from line 209, on a run whose own summary
# listed that suite as having passed.
#
# The fix is to stop piping. The membership test below is a case over the cached
# string, which cannot lose a race it no longer runs.
_SUITE_LIST="$(bash "$RUNNER" --list-suites 2>/dev/null)"
check "premise: the runner answered --list-suites, so the two checks below mean something" \
	"$([ -n "$_SUITE_LIST" ] && echo yes || echo "no (empty)")" "yes"

listed_suites() {
	if [ $# -gt 0 ]; then
		bash "$1" --list-suites 2>/dev/null
	else
		printf '%s\n' "$_SUITE_LIST"
	fi
}

