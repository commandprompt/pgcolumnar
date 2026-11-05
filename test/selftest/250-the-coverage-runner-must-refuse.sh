# ---- the coverage runner must refuse a run that captured no counters --------
#
# #740. The nightly coverage job failed every night from the night it was added
# (2026-07-31) and had never measured anything. It runs under sudo, so the build
# is root-owned, while lib.sh runs the server as `postgres` when it is root
# (lib.sh:150). gcov writes each .gcda beside its object, as the process that ran
# the code, so the backend could not create one and `lcov --capture` found
# nothing.
#
# What kept it unexamined for 25 nights was the ORDER of the failure. lcov failing
# on an empty tree reports "capture produced nothing", which reads as a broken
# tool rather than as a permission problem, so nobody looked. run_coverage.sh now
# refuses the run itself, before lcov, and says where the counters should have
# been. That ordering is the thing worth pinning: with the refusal after the
# capture it would be dead code and the misleading message would be back.

_cov="$TESTDIR/run_coverage.sh"
check "premise: the coverage runner is present and parses" \
	"$(bash -n "$_cov" 2>/dev/null && echo yes || echo no)" "yes"

_cov_guard=$(grep -n 'no .gcda counters were written' "$_cov" | cut -d: -f1 | head -1)
# The CAPTURE specifically. The runner also calls `lcov --directory ...
# --zerocounters` BEFORE the suites, at a lower line number, and matching that
# one made this check compare the guard against the wrong call and report a
# defect that was not there.
_cov_lcov=$(grep -n 'lcov --directory .*--capture' "$_cov" | cut -d: -f1 | head -1)

check "premise: both the counter refusal and the lcov capture were located" \
	"$([ -n "$_cov_guard" ] && [ -n "$_cov_lcov" ] && echo yes || echo no)" "yes"

# The whole point: refuse BEFORE lcov turns an empty tree into a tooling error.
check "the coverage runner refuses zero counters before it calls lcov (#740)" \
	"$([ -n "$_cov_guard" ] && [ -n "$_cov_lcov" ] &&
	   [ "$_cov_guard" -lt "$_cov_lcov" ] && echo yes || echo no)" "yes"

# ---- and the counters must be redirected somewhere always writable ---------
#
# Chowning the object directories to the server user was the first fix and it is
# NOT sufficient: creating a file needs execute on every ANCESTOR too, and in CI
# the tree sits under the runner's home. With an ancestor at mode 700 the chown
# succeeds, the directories are writable in themselves, and zero counters are
# still written. GCOV_PREFIX sidesteps the whole question by sending them to
# /tmp, which is world-writable and world-traversable.
#
# Two ORDERING facts carry the fix, and order is what these check. Neither is
# visible from the presence of the lines alone.
_cov_export=$(grep -n '^export GCOV_PREFIX' "$_cov" | cut -d: -f1 | head -1)
_cov_run=$(grep -n 'bash "\$SRCDIR/test/\${s}\.sh"' "$_cov" | cut -d: -f1 | head -1)
_cov_back=$(grep -n 'counters returned beside their objects' "$_cov" | cut -d: -f1 | head -1)

check "premise: the redirect, the suite invocation and the copy-back were located" \
	"$([ -n "$_cov_export" ] && [ -n "$_cov_run" ] && [ -n "$_cov_back" ] &&
	   echo yes || echo no)" "yes"

# Set after the suites have run, it redirects nothing.
check "GCOV_PREFIX is exported before the suites run (#740)" \
	"$([ -n "$_cov_export" ] && [ -n "$_cov_run" ] &&
	   [ "$_cov_export" -lt "$_cov_run" ] && echo yes || echo no)" "yes"

# Copied back after the refusal, the refusal always sees zero.
check "the counters are returned beside their objects before the refusal (#740)" \
	"$([ -n "$_cov_back" ] && [ -n "$_cov_guard" ] &&
	   [ "$_cov_back" -lt "$_cov_guard" ] && echo yes || echo no)" "yes"

# ---- and it must count the directory that is actually captured -------------
#
# Reported on the #745 review. The refusal counted .gcda across the WHOLE tree
# while `lcov --capture` is scoped to src/. objstore/ is a separate shared
# library built alongside this one (Makefile:127) and contributes its own .gcno
# and .gcda, so a tree with zero counters in src/ and one in objstore/ gives the
# guard a non-zero count, it passes, and lcov captures nothing: the "capture
# produced nothing" message this guard exists to pre-empt, back again.
#
# Constructed and confirmed rather than argued: 33 .gcno in src/ with no
# counters, one .gcda in objstore/, tree-wide count 1 (passes), src-scoped
# count 0 (refuses).
#
# Compared as SOURCE TEXT, so this pins the two to each other. Widening the
# capture later without widening the count re-opens the same hole.
_cov_count_dir=$(grep '^_gcda=\$(find ' "$_cov" |
	sed -n 's/.*find "\([^"]*\)".*/\1/p' | head -1)
_cov_cap_dir=$(grep 'lcov --directory .*--capture' "$_cov" |
	sed -n 's/.*--directory "\([^"]*\)".*/\1/p' | head -1)

check "premise: the guard's count directory and the capture's were both located" \
	"$([ -n "$_cov_count_dir" ] && [ -n "$_cov_cap_dir" ] && echo yes || echo no)" "yes"

check "the zero-counter guard counts the directory lcov captures (#740)" \
	"$_cov_count_dir" "$_cov_cap_dir"

# ---- and when it refuses, it must look where the redirect put them ---------
#
# Reported on the #745 cloud review. The refusal's second job is to separate a
# path mismatch from a permission refusal, which look identical from "0
# counters" and have different fixes. It did that with `find / -xdev`, and
# -xdev by definition will not cross a mount boundary, while GCOV_PREFIX
# defaults under /tmp -- a separate tmpfs on both boxes this was measured on,
# this project's dev container and the development host. How common that is
# elsewhere I have not measured. Counters sitting exactly where the
# redirect put them are invisible to that walk, so the run reports "nothing
# wrote them" and sends the reader to permissions.
#
# Measured, one file under a tmpfs /tmp and none elsewhere on the root
# filesystem: -xdev found 0, the same walk without it found 1, probing the
# prefix found 1.
#
# So GCOV_PREFIX must be asked BEFORE the tree-wide walk. Asked after, the
# walk's answer wins and the misdiagnosis returns; present but unordered, this
# reads as fixed and is not.
_cov_prefix_probe=$(grep -n '_stray=\$(find "\$GCOV_PREFIX"' "$_cov" | cut -d: -f1 | head -1)
_cov_xdev_probe=$(grep -n 'find / -xdev' "$_cov" | cut -d: -f1 | head -1)

check "premise: both stray-counter probes were located" \
	"$([ -n "$_cov_prefix_probe" ] && [ -n "$_cov_xdev_probe" ] && echo yes || echo no)" "yes"

check "the refusal looks in GCOV_PREFIX before the tree-wide walk (#740)" \
	"$([ -n "$_cov_prefix_probe" ] && [ -n "$_cov_xdev_probe" ] &&
	   [ "$_cov_prefix_probe" -lt "$_cov_xdev_probe" ] && echo yes || echo no)" "yes"

# ---- and the copy-back must not write outside the tree ---------------------
#
# Reported on the #745 review. $GCOV_PREFIX is a fixed path at mode 1777 and the
# copy-back runs as root under sudo, while _dest is the found path with the
# prefix stripped. Without a constraint, any local unprivileged user chooses both
# the content and the destination: plant $GCOV_PREFIX/<anywhere>/x.gcda and root
# copies it to <anywhere>/x.gcda.
#
# Demonstrated with the loop exactly as it stood, a file planted as `postgres`
# and written by root to a directory outside the tree, then blocked by the
# constraint while a legitimate counter under $SRCDIR/src still copied. Both
# arms, because a containment that refuses everything is not a fix.
#
# Constrained to names ending .gcda and not reachable on GitHub's single-tenant
# ephemeral runner, but reachable on any shared or developer box, which this
# script's header invites by documenting sudo.
_cov_contain=$(grep -n '"\$SRCDIR"/\*)' "$_cov" | cut -d: -f1 | head -1)
_cov_cp=$(grep -n 'cp -p "\$_f" "\$_dest"' "$_cov" | cut -d: -f1 | head -1)

check "premise: the containment and the copy were both located" \
	"$([ -n "$_cov_contain" ] && [ -n "$_cov_cp" ] && echo yes || echo no)" "yes"

# Placed after the copy it is not a containment, it is a comment.
check "the copy-back refuses a destination outside the tree (#740)" \
	"$([ -n "$_cov_contain" ] && [ -n "$_cov_cp" ] &&
	   [ "$_cov_contain" -lt "$_cov_cp" ] && echo yes || echo no)" "yes"

# ---- the per-file table must be computed, not parsed out of lcov --list ------
#
# #974. The nightly's "least covered first" table printed
# `columnar_parquet_codec.c | 2.0% 100|3200% 2| - 0` for a file whose records say
# `LF:100 LH:100 FNF:2 FNH:2 BRF:64 BRH:47`. A file at 100% presented as 2.0% and
# ranked the worst in the tree, four of the named files between 94% and 100%, and
# function rates above 100% on their face. The same command on the same tracefile
# gave correct rows on lcov 2.0-1 and wrong ones on the runner's 2.0-4ubuntu2, so
# the table moved with a distro patch rather than with the data.
#
# A rate is a division of two integers the tracefile states outright, so the fix is
# to do the division. `lcov --summary` keeps its job: it was right on both builds.
#
# THE ARMS DRIVE THE GENERATOR over a synthetic tracefile rather than grepping it.
# A static check that the runner calls the right script cannot tell whether the
# script is correct, and "the table is wrong" was the defect.
_covtab="$TESTDIR/pgc_coverage_table.py"
check "premise: the table generator is present and parses" \
	"$([ -f "$_covtab" ] && python3 -c "import ast,sys;ast.parse(open(sys.argv[1]).read())" "$_covtab" 2>/dev/null && echo yes || echo no)" \
	"yes"

# COUNTED OVER CODE, NOT OVER TEXT. The first version of these three greps counted
# comment lines, and two of them failed on MY OWN comments in the runner explaining
# why the call was replaced -- a guard defeated by the sentence documenting it. That
# is the third time today a comment carrying a token tripped a grep that wanted the
# call: a `pgc_summary` mention in #969 and a `shellcheck` mention in #972 were the
# others. Strip comments first, so a future explanation cannot redden the arm.
_cov_code() {	# _cov_code PATTERN -> count of matching NON-comment lines
	grep -vE '^[[:space:]]*#' "$_cov" | grep -cE "$1"
}
check "the coverage runner computes the table instead of parsing lcov --list (#974)" \
	"$(_cov_code 'pgc_coverage_table\.py')" "1"
check "and no lcov --list call survives in the runner (#974)" \
	"$(_cov_code 'lcov --list')" "0"
check "while the lcov --summary call stays, being correct on both builds (#974)" \
	"$(_cov_code 'lcov --summary')" "1"
# The premise for all three: stripping comments must not strip the code. Without it
# a grep that matches nothing reports the same 0 as a grep over an emptied file.
_cov_tools="$(grep -vE '^[[:space:]]*#' "$_cov" | grep -cE 'genhtml|lcov')"
check "premise: stripping comments leaves the runner's code behind" \
	"$([ "$_cov_tools" -ge 2 ] && echo yes || echo "no ($_cov_tools code lines name genhtml or lcov)")" \
	"yes"

# Counters chosen so every cell is distinguishable from every failure mode:
#   worst.c    partially covered, must sort FIRST
#   perfect.c  100% lines -- the shape the nightly printed as 2.0%
#   nobranch.h no branch counter at all -- must print `-`, never 0.0%, or it
#              sorts to the top and reads as the least covered file in the tree
_covtf="$(mktemp "${TMPDIR:-/tmp}/pgc-covtab.XXXXXX")"
{
	printf 'TN:\nSF:/x/src/worst.c\nFNF:4\nFNH:1\nBRF:10\nBRH:2\nLF:100\nLH:25\nend_of_record\n'
	printf 'TN:\nSF:/x/src/perfect.c\nFNF:2\nFNH:2\nBRF:64\nBRH:47\nLF:100\nLH:100\nend_of_record\n'
	printf 'TN:\nSF:/x/src/nobranch.h\nFNF:0\nFNH:0\nLF:8\nLH:4\nend_of_record\n'
} > "$_covtf"
_covout="$(python3 "$_covtab" "$_covtf" --limit 0 2>&1)"

check "premise: the generator produced a row for each of the three files" \
	"$(printf '%s\n' "$_covout" | grep -cE '^\s+(worst\.c|perfect\.c|nobranch\.h)')" "3"

check "the least covered file sorts first (#974)" \
	"$(printf '%s\n' "$_covout" | grep -oE '(worst|perfect|nobranch)\.[ch]' | head -1)" "worst.c"

check "a file at 100% reads 100.0%, not a small number (#974)" \
	"$(printf '%s\n' "$_covout" | awk '/perfect\.c/ {print $2}')" "100.0%"

check "the rate carries hit/found so a reader can check it (#974)" \
	"$(printf '%s\n' "$_covout" | awk '/worst\.c/ {print $2, $3}')" "25.0% 25/100"

# The one that matters for ordering: an absent counter is not a zero. A `-` keeps
# the file where its line rate puts it; a 0.0% would claim it is the worst branch
# coverage in the tree on the strength of having no branches.
#
# THE PROPERTY FIRST, POSITION SECOND. The first version asserted the dash by field
# number and got it wrong -- absent cells collapse to one token each, so the dashes
# are $4 and $5, not $6. The property does not depend on where the cell lands.
# ANCHORED, because `0\.0%` is a SUBSTRING of `50.0%` and this row's line rate is
# exactly that. The unanchored form reported one match and read as the generator
# printing a zero rate for an absent counter, which it does not.
check "a row with absent counters never reads as 0.0% (#974)" \
	"$(printf '%s\n' "$_covout" | awk '/nobranch\.h/' | grep -cE '(^|[^0-9.])0\.0%')" "0"
check "an absent counter prints a dash instead (#974)" \
	"$(printf '%s\n' "$_covout" | awk '/nobranch\.h/ {print $4, $5}')" "- -"

check "and the function rate is computed too, not copied (#974)" \
	"$(printf '%s\n' "$_covout" | awk '/worst\.c/ {print $4, $5}')" "25.0% 1/4"

rm -f "$_covtf"
unset _covtab _covtf _covout
unset -f _cov_code
