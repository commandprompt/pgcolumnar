#!/usr/bin/env bash
#
# Documentation style gate: the measurable plain-language rules (issue #291).
#
# The project writes its user-facing documentation to ISO 24495-1:2023, Plain
# language - Part 1: Governing principles and guidelines. A rule that nothing
# checks is a rule the next writer does not know about, and this project has been
# bitten by that shape before: an empty REGRESS made `make installcheck` report
# success while running nothing. So the rules that a machine can check are
# checked here, and a document that drifts goes red.
#
# WHAT IS NOT CLAIMED. ISO 24495-1 gives four governing principles -- relevant,
# findable, understandable, usable -- and only the third has any mechanically
# checkable content, and only in part. Its own test for "usable" is that a reader
# acts on the document successfully, which no checker performs. So a green run
# means the measurable subset holds, NOT that the documentation is plain. Saying
# so is the point: an unverifiable claim of conformity would be worse than an
# honest partial one.
#
# Two of the four checks are this project's typographic house rules rather than
# anything the standard requires: no em or en dash, and no double hyphen as a
# dash in prose. They are named as house rules wherever they appear so nobody
# mistakes a preference for a requirement.
#
# SCOPE, which is a decision rather than an oversight:
#
#   docs/*.md and README.md are user-facing prose and are checked in full.
#
#   CHANGELOG.md is a record of what happened, written at the time it happened.
#   Rewriting landed entries would edit history, so it is checked for dash
#   characters only.
#
#   design/ holds internal engineering records and is not checked. Code comments
#   are not checked either. Both explain WHY, and the reasoning in them is worth
#   more than the uniformity would be.
#
#   RELEASE_NOTES_*.md, ANNOUNCEMENT_*.md, CONTEXT.md and PROVENANCE.md are NOT
#   checked, and that was asked and answered on 2026-08-29 rather than assumed.
#   Running the checker over them by hand finds 19, 6, 23 and 87 over-long
#   sentences, so the exclusion is not a claim that they conform. A release note
#   and an announcement describe one shipped version and are not revised after
#   it; CONTEXT.md is written for agents working in this repository; and
#   PROVENANCE.md is a clean-room record whose precision outranks its sentence
#   length. The owner's decision was that these four stay outside the gate.
#
#   README.md's version marker is compared against VERSION by the check at the
#   bottom of this file. It was NOT, until 2026-08-29, and it had drifted two
#   versions as a result: it said 1.0-alpha while VERSION said 1.0-alpha3. The
#   only file that was wrong was the one file the comparison could not see.
#
# Usage:  test/docs_style.sh [PG_CONFIG]
# The argument is accepted and ignored; this suite needs no cluster.
# Written fresh for pgColumnar.

set -uo pipefail
SRCDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

checks=0
fail=0
check() {
	local name="$1" got="$2" want="$3"
	checks=$((checks + 1))
	if [ "$got" = "$want" ]; then
		echo "PASS  $name"
	else
		echo "FAIL  $name: got [$got] want [$want]"
		fail=1
	fi
}

command -v python3 >/dev/null || { echo "FAIL  python3 not found"; exit 1; }

echo "== pgColumnar test: docs_style.sh =="

# The full rules over every user-facing document.
docs=$(ls "$SRCDIR"/docs/*.md "$SRCDIR"/README.md 2>/dev/null)
out="$(python3 "$SRCDIR/test/plain_language_check.py" $docs 2>&1)"
rc=$?
echo "$out" | sed 's/^/  /'
check "every user-facing document meets the measurable plain-language rules" "$rc" "0"

# The control. A checker that examines nothing reports nothing, and this suite
# would then pass on an empty docs/ directory or a broken glob.
n=$(echo "$out" | grep -c '^  ok' || true)
check "and it actually examined the documents" \
	"$([ "$n" -ge 10 ] && echo yes || echo "no (examined $n)")" "yes"

# The roadmap has to stay reachable. It went unfound once because the only routes to it
# were a raw GitHub link and a line in the changelog (#395). A page that is not in the nav
# is not published, and nothing else would notice.
#
# Reachability is two facts, so both are asserted. The nav check alone passes when the
# PAGE is deleted and the entry is kept, which is a broken link rather than reachability.
# That case is also caught by "mkdocs build --strict" in docs.yml, which fails on a nav
# entry pointing at nothing. Half a property here and half in a workflow is how the
# missing half goes unnoticed, so both halves are stated here.
nav_roadmap=$(grep -c "roadmap.md" "$SRCDIR/mkdocs.yml" || true)
check "the roadmap is in the documentation nav" "$([ "$nav_roadmap" -ge 1 ] && echo yes || echo no)" "yes"
check "and the page that nav entry points at exists" \
	"$([ -f "$SRCDIR/docs/roadmap.md" ] && echo yes || echo no)" "yes"

# Merge conflict markers. These reached main and were published: three of them sat
# in docs/limitations.md under "Vacuum and compaction", and every other check in
# this file passed with them there, because they are valid Markdown text.
#
# The prose checker reads prose and the nav check reads mkdocs.yml. Neither asks
# whether the page is a coherent document. This does.
conflicts=$(grep -rlE '^(<<<<<<< |>>>>>>> )' "$SRCDIR/docs" "$SRCDIR"/*.md 2>/dev/null | tr '\n' ' ')
check "no document carries a merge conflict marker" \
	"$([ -z "$conflicts" ] && echo none || echo "$conflicts")" "none"

# CHANGELOG.md: dash characters only. See the scope note above.
dashes=$(grep -c '—\|–' "$SRCDIR/CHANGELOG.md" || true)
check "CHANGELOG.md carries no em or en dash" "$dashes" "0"
# ---- a star-schema fact table is clustered on the join key (#752) ----------
#
# The runtime-filter how-to named the GUC and not the layout that makes group
# skip a no-op. native_join_runtime_filter already measured it: 19 of 20 groups
# removed when the keys are local, 0 of 20 when they cycle. Nothing in docs/
# said so. These two checks read the published pages, not the test suite.
_howto_rf() {
	python3 - "$SRCDIR/docs/how-to.md" <<'PY'
from pathlib import Path
import sys
text = Path(sys.argv[1]).read_text(encoding="utf-8")
heading = "## Skip fact-table work under a star-schema join"
start = text.find(heading)
if start < 0:
    print("missing-heading")
    raise SystemExit(0)
rest = text[start + len(heading):]
nxt = rest.find("\n## ")
section = (rest if nxt < 0 else rest[:nxt]).lower()
print("yes" if ("cluster" in section and "join key" in section) else "no")
PY
}
check "how-to names clustering on the join key for the runtime filter" \
	"$(_howto_rf)" "yes"

_practices_jk() {
	python3 - "$SRCDIR/docs/best-practices.md" <<'PY'
from pathlib import Path
import sys
text = Path(sys.argv[1]).read_text(encoding="utf-8").lower()
print("yes" if "join key" in text else "no")
PY
}
check "best-practices names clustering on the join key" \
	"$(_practices_jk)" "yes"

# ---- the stripe floor is below a vector, and the pages must say so (#1017) ----
#
# A vector is a fixed 1024 values (COLUMNAR_NATIVE_VECTOR_LENGTH). A row group
# smaller than one never fills it and FSST is not applied to text columns.
# Measured, 200,000 rows, compression=none, against 12,800,000 raw bytes:
#
#     stripe_row_limit 1000   0 FSST tables   13,625,000   106.4% of raw
#     stripe_row_limit 1200   166 tables       6,998,031    54.7% of raw
#
# The ACCEPTED MINIMUM IS 1000, so the most aggressive legal setting is the one
# that pays this, and administration.md tells a reader to LOWER the setting for
# point lookups. The warning has to sit in the block that gives that advice, not
# in a reference table three pages away -- so these arms are scoped to the block
# and not to the page. An earlier version grepped whole pages and passed on main,
# which already says 1024 and FSST elsewhere.
# ONE LINE CARRYING BOTH, AND FOR administration.md THE RIGHT SECTION TOO.
#
# One line, because a blank-line block and a three-line window are both green on
# main: configuration.md's GUC table has no blank lines, so stripe_row_limit's row
# shares a block with chunk_group_row_limit's "fixed 1024-value vectors", and those
# rows are adjacent. One line naming both is 0 on all three pages on main, and it
# makes the prose state the floor in a sentence, which is what a warning needs.
#
# THE SECTION, because one line alone says nothing about WHERE. @OffgridwithJD moved
# the line out of the advice block to the end of administration.md, 402 lines away,
# and the page-wide arm still passed while its name claimed the floor was stated
# "beside the advice to lower the setting". Reproduced before changing anything.
#
# A `## ` heading is the boundary, not a blank line. That is what the paragraph
# reader got wrong: blank lines are absent inside a markdown table and arbitrary in
# prose, while a heading is declared.
_floor_line() {	# _floor_line FILE -- yes if one line names the setting and the floor
	if grep -qE 'stripe_row_limit.*1024|1024.*stripe_row_limit' "$1"; then
		echo yes
	else
		echo no
	fi
}
_floor_same_section() {	# _floor_same_section FILE ADVICE -- yes if both are under one `## `
	awk -v advice="$2" '
		/^## / { h = substr($0, 4) }
		{
			if (index(tolower($0), tolower(advice))) a[h] = 1
			if (/stripe_row_limit/ && /1024/) f[h] = 1
		}
		END { for (k in a) if (k in f) { print "yes"; exit } print "no" }' "$1"
}
check "configuration.md states the 1024 floor where it documents the setting" \
	"$(_floor_line "$SRCDIR/docs/configuration.md")" "yes"
check "administration.md states it in the section that says to lower the setting" \
	"$(_floor_same_section "$SRCDIR/docs/administration.md" "Lower this setting")" "yes"
check "best-practices.md carries it with the load-sizing advice" \
	"$(_floor_line "$SRCDIR/docs/best-practices.md")" "yes"

# ---- a document that quotes the version must quote the current one ----------
#
# Nothing reads the VERSION file mechanically: no Makefile rule, no CI step. Two
# documents cite it AND hardcode the string beside the citation:
#
#     CHANGELOG.md         the version marker is `1.0-alpha`, recorded in `VERSION`
#     docs/limitations.md  The version marker is `1.0-alpha`, recorded in `VERSION`,
#
# So a release that bumps VERSION and pgcolumnar.control, and forgets these, ships
# documentation asserting the previous version. Nothing else would notice: the
# upgrade path is gated by extension_upgrade.sh, which compares control against
# the installed extension and never reads prose.
#
# The pairing is what makes it checkable. A document that says "recorded in
# `VERSION`" is pointing at a file whose content is knowable, so the two can be
# compared instead of trusted to be edited together.
_ver="$(cat "$SRCDIR/VERSION" 2>/dev/null | tr -d '[:space:]')"
check "premise: the VERSION file has a version to compare against" \
	"$([ -n "$_ver" ] && echo yes || echo no)" "yes"

# README.md is in this list because it was NOT, and drifted two versions as a
# result: it said `1.0-alpha` while VERSION said `1.0-alpha3`. The check that
# would have caught it excluded the only file that was wrong.
_verdocs="$(grep -rln 'recorded in `VERSION`' "$SRCDIR/CHANGELOG.md" "$SRCDIR/README.md" "$SRCDIR/docs" 2>/dev/null | sort)"
check "premise: at least one document cites the VERSION file" \
	"$([ -n "$_verdocs" ] && echo yes || echo no)" "yes"

_stale=""
for _d in $_verdocs; do
	grep -q "\`$_ver\`, recorded in \`VERSION\`" "$_d" || _stale="$_stale $(basename "$_d")"
done
check "every document citing VERSION quotes the version VERSION holds" \
	"$(printf '%s' "$_stale" | sed 's/^ //')" ""


echo "checks run: $checks"
if [ "$fail" = 0 ]; then
	echo "docs_style.sh: PASSED"
	exit 0
fi
echo "docs_style.sh: FAILED"
exit 1
