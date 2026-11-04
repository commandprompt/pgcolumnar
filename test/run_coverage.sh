#!/usr/bin/env bash
#
# Line and branch coverage over the suites, for one major.
#
# A discovery tool, not a gate. There is no threshold here and nothing in CI
# fails on the number, deliberately. A coverage threshold creates pressure to
# write tests that execute lines rather than tests that prove properties, and the
# assertions in this project's suites are the reason its defects get found. What
# the report answers is the complementary question a passing suite cannot: which
# code does nothing execute at all. For hand-rolled Parquet and Arrow decoders
# full of error paths, that is worth knowing.
#
# Expect the percentage to look unremarkable and expect that to be correct. A
# table access method carries defensive branches that should never be taken and
# columnar_compat.h carries version shims of which only one arm compiles per
# major. Read the report for holes, not for the number.
#
# Why the suites are driven directly rather than through run_all_versions.sh: the
# matrix builds its own copy of the tree in a temporary directory and removes it
# when the major finishes, which takes the .gcda counters with it. Coverage has to
# accumulate next to the objects it was compiled from, so this builds once in the
# working tree, installs that build, and runs every suite against it with
# PGC_SKIP_BUILD=1.
#
# Usage:  test/run_coverage.sh [PG_CONFIG]
#   PGC_COV_SUITES=<list>   override the suite list
#   PGC_COV_OUT=<dir>       output directory (default coverage/)
#
# Written fresh for pgColumnar.

set -uo pipefail

PGC="${1:-/usr/local/pg17/bin/pg_config}"
SRCDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${PGC_COV_OUT:-$SRCDIR/coverage}"

command -v lcov >/dev/null || { echo "FAIL  lcov not found" >&2; exit 1; }
command -v genhtml >/dev/null || { echo "FAIL  genhtml not found" >&2; exit 1; }
[ -x "$PGC" ] || { echo "FAIL  no pg_config at $PGC" >&2; exit 1; }

echo "== coverage: $("$PGC" --version)"

# COPT reaches both CFLAGS and LDFLAGS through PostgreSQL's Makefile.global, so
# one variable instruments the compile and links the runtime.
echo "-- build with --coverage"
make -C "$SRCDIR" clean >/dev/null 2>&1
if ! make -C "$SRCDIR" PG_CONFIG="$PGC" COPT="--coverage" >/dev/null 2>"$OUT.build.err"; then
	echo "FAIL  instrumented build failed" >&2
	tail -20 "$OUT.build.err" >&2
	exit 1
fi
make -C "$SRCDIR" PG_CONFIG="$PGC" COPT="--coverage" install >/dev/null 2>&1 || {
	echo "FAIL  install failed" >&2; exit 1; }

# gcov writes each .gcda beside its object, at the absolute path recorded at
# COMPILE time, and it writes it as the process that ran the code. This runner is
# invoked under sudo in CI, so the build is root-owned, while lib.sh runs the
# server as `postgres` whenever it is root (lib.sh:150). The backend could not
# create a counter file next to a root-owned object, so no .gcda was ever written
# and `lcov --capture` had nothing to find. The report has measured nothing since
# the job was added (#740).
#
# The counters are REDIRECTED rather than the source tree being opened up.
# Chowning the object directories to the server user was the first fix and it is
# not sufficient: creating a file needs execute on every ANCESTOR too. Measured
# on the runner, with the object directories already chowned:
#
#     postgres:runner 755 /home/runner/work/pgcolumnar/pgcolumnar/src
#     postgres:runner 755 /home/runner/work/pgcolumnar/pgcolumnar
#     runner:runner   755 /home/runner/work/pgcolumnar
#     runner:runner   755 /home/runner/work
#     runner:runner   750 /home/runner        <-- no execute for postgres
#     root:root       755 /home
#
# /home/runner is 750 and postgres is neither its owner nor in the runner group,
# so it cannot traverse into the workspace at all, however the leaf directories
# are owned. That route could only work by chmod'ing the runner's home, which is
# not a thing to do to a shared runner.
#
# GCOV_PREFIX makes gcov write to $GCOV_PREFIX/<absolute path>.gcda instead.
# /tmp is world-writable and world-traversable, so this works whoever the server
# runs as and wherever the tree lives. GCOV_PREFIX_STRIP=0 keeps the full path,
# which is what lets the counters be put back exactly where their .gcno are.
# runuser passes the environment through, so the postmaster and its backends
# inherit both variables.
GCOV_PREFIX="${PGC_COV_GCOV_PREFIX:-/tmp/pgc-gcov}"
GCOV_PREFIX_STRIP=0
export GCOV_PREFIX GCOV_PREFIX_STRIP
rm -rf "$GCOV_PREFIX"
mkdir -p "$GCOV_PREFIX"
chmod 1777 "$GCOV_PREFIX"
echo "-- counters redirected to $GCOV_PREFIX"

# Every suite except the drivers, the libraries, and the ones that build their
# own server or need two majors. Kept in step with harness_selftest.sh's list.
#
# The two upgrade suites are excluded TOGETHER, and that pairing is the point
# (#741). run_all_versions.sh gates both behind PGC_RUN_UPGRADE, in one block and
# for one reason: they build a second copy of the extension and need an old
# source this runner has no way to supply. pg_upgrade was excluded here when it
# was written and extension_upgrade was not, so this runner discovered it, ran it
# with no old source, and its ref fallback could not resolve in a tagless CI
# checkout. run_coverage maps only rc 66 to SKIP, so that environment shortfall
# was counted as a failed suite and the nightly read "1 failed
# ( extension_upgrade)" every night while nothing was wrong with the product.
#
# Deciding to run an opt-in guard is run_all_versions.sh's job, not this one's.
# test/selftest/220 derives the gated list from that block and asserts every name
# in it is refused here, so a third upgrade suite cannot repeat this.
not_a_suite() {
	case "$1" in
		lib|portlib|run_all_versions|build_all_versions|devloop|rebuild) return 0 ;;
		build_san|run_san|run_coverage) return 0 ;;
		pg_upgrade|extension_upgrade|format_downgrade|capability_sweep) return 0 ;;
		*) return 1 ;;
	esac
}

SUITES="${PGC_COV_SUITES:-}"
if [ -z "$SUITES" ]; then
	for f in "$SRCDIR"/test/*.sh; do
		n="$(basename "$f" .sh)"
		not_a_suite "$n" && continue
		SUITES="$SUITES $n"
	done
fi

mkdir -p "$OUT"
lcov --directory "$SRCDIR/src" --zerocounters >/dev/null 2>&1

# Below the ephemeral floor, like every other port in this tree (portlib.sh).
. "$SRCDIR/test/portlib.sh"
port="$(pgc_pick_port)"

pass=0; fail=0; failed=""; skip=0; skipped=""
for s in $SUITES; do
	port=$((port + 1))
	# PGC_SKIP_TIMING, because a --coverage build is instrumented and its wall
	# clock means nothing. The wall-clock suites assert ratios and absolute
	# timeouts; run here without the flag they fail for the instrumentation rather
	# than for the code, and this runner discovers every test/*.sh including any
	# added later. The matrix is where those numbers are taken.
	PGC_SKIP_BUILD=1 PGC_SKIP_TIMING=1 PGC_PORT="$port" \
		bash "$SRCDIR/test/${s}.sh" "$PGC" >"$OUT/${s}.log" 2>&1
	rc=$?
	if [ "$rc" = 0 ]; then
		pass=$((pass + 1))
	elif [ "$rc" = 66 ] && grep -q 'SKIPPED (ran no checks)' "$OUT/${s}.log" 2>/dev/null; then
		# Ran no checks (#447). It contributed no coverage either, so counting it
		# as a pass overstates what this report measured.
		skip=$((skip + 1)); skipped="$skipped $s"
	else
		fail=$((fail + 1)); failed="$failed $s"
	fi
done
echo "-- suites: $pass passed, $fail failed${failed:+ ($failed)}, $skip skipped${skipped:+ ($skipped)}"
# A coverage report built from nothing is not a coverage report. run_san grew this
# guard and this runner did not, so its only verdict was "nothing failed" -- which
# a box where every suite aborts satisfies perfectly.
if [ "$pass" = 0 ]; then
	echo "-- NO SUITE RAN, so this measures no coverage"
	fail=$((fail + 1))
fi

# Put the counters back beside their .gcno, which is where lcov and gcov both
# expect them. Done as this user, which is root under sudo, so no permission on
# the source tree is needed at all.
_moved=0
if [ -d "$GCOV_PREFIX" ]; then
	while IFS= read -r _f; do
		[ -n "$_f" ] || continue
		_dest="${_f#$GCOV_PREFIX}"
		# The destination must be INSIDE the tree, and this is not a tidiness
		# check. $GCOV_PREFIX is a fixed path at mode 1777 and this loop runs as
		# root under sudo, so without it any local unprivileged user chooses both
		# the content and the destination: plant
		# $GCOV_PREFIX/<anywhere>/x.gcda and root copies it to <anywhere>/x.gcda.
		# Demonstrated with the loop exactly as it stood, a file planted as
		# `postgres` and written by root outside the tree. Constrained to names
		# ending .gcda, and not reachable on GitHub's single-tenant ephemeral
		# runner, but reachable on any shared or developer box -- which this
		# script's own header invites, since it documents being run under sudo.
		case "$_dest" in
			"$SRCDIR"/*) ;;
			*) continue ;;
		esac
		[ -d "$(dirname "$_dest")" ] || continue
		cp -p "$_f" "$_dest" 2>/dev/null && _moved=$((_moved + 1))
	done <<EOF
$(find "$GCOV_PREFIX" -name '*.gcda' 2>/dev/null)
EOF
fi
echo "-- counters returned beside their objects: $_moved"

# A report built from no counters is not a report, which is the same reasoning as
# the `pass = 0` guard above and the case that has actually been happening (#740).
# Asserted HERE rather than left to lcov: `lcov --capture` failing on an empty
# tree reports "capture produced nothing", which reads as a broken tool and sent
# nobody to look at the permissions for 25 nights.
#
# Counted over the directory lcov CAPTURES, not tree-wide. objstore/ is a
# separate shared library, built alongside but never linked in (Makefile:127),
# and the report is scoped to src/ by the --extract below. So a tree-wide count
# can be non-zero while the captured directory holds nothing: one objstore
# counter satisfies the guard and leaves lcov with an empty tree, which is the
# exact "capture produced nothing" this guard exists to pre-empt.
_gcno=$(find "$SRCDIR/src" -name '*.gcno' | wc -l)
_gcda=$(find "$SRCDIR/src" -name '*.gcda' | wc -l)
echo "-- counters: $_gcda .gcda from $_gcno instrumented objects in src/"
# The whole tree, broken down, because the totals differ on the GitHub runner
# (34 .gcda from 49 .gcno) and a bare ratio invites the reading that a third of
# the objects went uncovered. Printed from the data rather than argued.
find "$SRCDIR" \( -name '*.gcno' -o -name '*.gcda' \) -printf '%h %f\n' 2>/dev/null |
	awk -v srcdir="$SRCDIR" -v root="$SRCDIR/" '
		{ ext = $2; sub(/.*\./, "", ext)
		  dir = $1
		  if (dir == srcdir) dir = "."
		  else if (index(dir, root) == 1) dir = substr(dir, length(root) + 1)
		  n[dir " " ext]++ }
		END { for (k in n) { split(k, a, " ")
			printf "     %-5s %-4s %s\n", n[k], a[2], a[1] } }' |
	sort -k3,3 -k2,2
# And NAMED, for anything outside the captured scope. The run above reported 13
# instrumented objects at the repository root and 3 under objstore/, against 1
# under objstore/ in the container, and a count is a shape rather than an
# answer. This is the line that says what they are.
_outside=$(find "$SRCDIR" -name '*.gcno' -not -path "$SRCDIR/src/*" \
	-printf '%P\n' 2>/dev/null | sort)
if [ -n "$_outside" ]; then
	echo "-- instrumented objects outside src/, which the report does not cover:"
	printf '     %s\n' $_outside | head -20
fi
if [ "$_gcda" = 0 ]; then
	echo "FAIL  no .gcda counters were written, so this measures no coverage" >&2
	echo "      gcov writes them beside the objects, as the user that ran the" >&2
	echo "      code. Check that the server user can write to the directories" >&2
	echo "      holding the .gcno files." >&2
	# Say WHERE they went, if anywhere. A path mismatch and a permission refusal
	# look identical from "0 counters", and they have different fixes.
	#
	# GCOV_PREFIX is asked FIRST, and deliberately not left to the tree-wide
	# walk below. That walk carries -xdev, which by definition will not cross a
	# mount boundary, and /tmp is a separate tmpfs on both boxes this was
	# measured on, this project's dev container and the development host.
	# How common that is elsewhere I have not measured. Counters sitting exactly
	# where the redirect put them are invisible to it, and the run then reports
	# "nothing wrote them" -- the opposite diagnosis, sending the reader to
	# permissions when the counters exist and it is the copy-back that missed.
	# Measured, one file under a tmpfs /tmp and none elsewhere: 0 found with
	# -xdev, 1 without it, 1 probing the prefix.
	#
	# The tree-wide walk is kept as the fallback, because it answers a
	# different question: gcov ignoring the redirect altogether and writing
	# beside an object outside src/, which is on the same filesystem as the
	# tree and which the src-scoped count above would not see.
	_stray=$(find "$GCOV_PREFIX" -name '*.gcda' 2>/dev/null | head -5)
	[ -n "$_stray" ] || _stray=$(find / -xdev -name '*.gcda' 2>/dev/null | head -5)
	if [ -n "$_stray" ]; then
		echo "      counters DO exist elsewhere, so this is a path mismatch:" >&2
		printf '        %s\n' $_stray >&2
	else
		echo "      no .gcda anywhere on this filesystem, so nothing wrote them" >&2
	fi
	exit 1
fi

echo "-- collect"
lcov --directory "$SRCDIR/src" --capture --output-file "$OUT/coverage.info" \
	--rc branch_coverage=1 --ignore-errors inconsistent >/dev/null 2>&1 || {
		echo "FAIL  lcov capture produced nothing" >&2; exit 1; }
# Only this project's sources; PostgreSQL's headers are not what is being measured.
lcov --extract "$OUT/coverage.info" "$SRCDIR/src/*" \
	--output-file "$OUT/coverage.info" --rc branch_coverage=1 \
	--ignore-errors inconsistent >/dev/null 2>&1

# --ignore-errors inconsistent, and only that one. lcov 2.x rejects a tracefile
# when gcov reports a line as hit while recording no evaluated branches on it,
# which gcc emits routinely for lines carrying compiler-generated branches. It is
# an artifact of the two tools' disagreement rather than bad data. "corrupt" is
# deliberately not bypassed: that one would hide a genuinely unreadable file.
genhtml "$OUT/coverage.info" --output-directory "$OUT/html" \
	--branch-coverage --legend --ignore-errors inconsistent >/dev/null 2>&1 || {
		echo "FAIL  genhtml failed" >&2; exit 1; }

echo
lcov --summary "$OUT/coverage.info" --rc branch_coverage=1 \
	--ignore-errors inconsistent 2>&1 | grep -vE "^(Reading|lcov: (WARNING|Note))" | sed 's/^/  /'
echo
# COMPUTED FROM THE TRACEFILE, not parsed out of `lcov --list` (#974). That
# command printed a 100%-covered file as 2.0% and ranked it least covered on the
# nightly runner, while giving correct rows for the same tracefile on another build
# of the same lcov 2.0. The counters it would have to get wrong -- `LF:`, `LH:` --
# are stated outright in the file, so the division happens here.
#
# `lcov --summary` above is untouched: it was right on both builds.
echo "-- per file, least covered first"
python3 "$SRCDIR/test/pgc_coverage_table.py" "$OUT/coverage.info" --limit 20
echo
echo "report: $OUT/html/index.html"

# The suites failing is worth a non-zero exit; the coverage number never is.
[ "$fail" = 0 ]
