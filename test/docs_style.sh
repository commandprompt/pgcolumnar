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
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
SRCDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# lib.sh's check() AND pgc_summary, NOT A PRIVATE TALLY (#1341). This suite kept its
# own counter and emitted no RESULT records, so no ledger row could ever be seeded for
# it and its pytest twin had nothing to be compared with. It now records like every
# other suite. Like wal_envelope.sh it needs no cluster, so pgc_setup is skipped and
# the counters check() expects are provided here. PGC_MAJOR comes from the pg_config
# the runner passes every suite, so a record names the major it ran on (#1121).
PGC_MAJOR="$(pgc_major_of "${1:-}")"
PGC_CHECKS=0
PGC_FAIL=0

# A property this suite cannot compare in a checkout without release tags is a NOTE,
# printed for a reader and counted as nothing; see the changelog block below for why it
# is not recorded unrunnable. note() is not lib.sh's: it is defined here.
#
# Reaching for lib.sh's helper of that name here is `command not found`: it prints
# nothing, counts nothing and fails nothing while the suite reports PASSED.
# Measured, and the reason this is a note rather than a second check helper.
note() {	# note TEXT
	echo "--  $1"
}

command -v python3 >/dev/null || { echo "FAIL  python3 not found"; exit 1; }

# WHAT A READER SEES, NOT WHAT THE FILE HOLDS (#1341 port). Every arm below that asks
# whether a page SAYS something read the raw file, so the sentence passed from inside
# an HTML comment that no reader ever sees. Measured on main: the 1024-floor line moved
# into `<!-- -->`, "join key" kept only in a comment, and a stale VERSION citation with
# the right one hidden beside it each left the whole suite green. `_md` is the page as
# rendered prose: the file with its HTML comments removed.
_md() {	# _md FILE -> the file's text without <!-- --> comments
	python3 -c 'import re, sys
sys.stdout.write(re.sub(r"<!--.*?-->", "", open(sys.argv[1], encoding="utf-8").read(), flags=re.S))' "$1"
}

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
# A COMMENTED-OUT ENTRY IS NOT IN THE NAV. The raw grep counted `# - Roadmap: roadmap.md`,
# so a nav entry disabled with `#` passed: measured on main.
nav_roadmap=$(sed -E 's/(^|[[:space:]])#.*//' "$SRCDIR/mkdocs.yml" | grep -c "roadmap.md" || true)
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

# THE pg_partman HOW-TO KEEPS ITS OWN HEADING. An edit that inserted a section
# before it replaced the heading instead (3627327, #1315), and every check here
# passed: the steps were still valid Markdown, now filed under "Count distinct
# values and percentiles approximately". The section is found by its heading and
# must open with the pg_partman paragraph its steps belong to.
partman_open="$(awk '$0 == "## Manage time partitions with pg_partman" {f=1; next}
	f && NF {print; exit}' <(_md "$SRCDIR/docs/how-to.md"))"
check "the how-to keeps its pg_partman section under its own heading" \
	"$(printf '%s' "$partman_open" | grep -c '^pg_partman can create and drop columnar partitions')" "1"

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
import re
text = re.sub(r"<!--.*?-->", "", Path(sys.argv[1]).read_text(encoding="utf-8"), flags=re.S)
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
import re
text = re.sub(r"<!--.*?-->", "", Path(sys.argv[1]).read_text(encoding="utf-8"), flags=re.S).lower()
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
	# grep -c, not -q: an early-exit reader under pipefail turns a match into a failure
	if [ "$(_md "$1" | grep -cE 'stripe_row_limit.*1024|1024.*stripe_row_limit')" -gt 0 ]; then
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
		END { for (k in a) if (k in f) { print "yes"; exit } print "no" }' <(_md "$1")
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
_verdocs="$(grep -rln 'recorded in `VERSION`' "$SRCDIR/CHANGELOG.md" "$SRCDIR/README.md" "$SRCDIR/docs" 2>/dev/null | LC_ALL=C sort)"
check "premise: at least one document cites the VERSION file" \
	"$([ -n "$_verdocs" ] && echo yes || echo no)" "yes"

_stale=""
for _d in $_verdocs; do
	[ "$(_md "$_d" | grep -c "\`$_ver\`, recorded in \`VERSION\`")" -gt 0 ] || _stale="$_stale $(basename "$_d")"
done
check "every document citing VERSION quotes the version VERSION holds" \
	"$(printf '%s' "$_stale" | sed 's/^ //')" ""

# ---- and the VERSION BADGE, which is a third place the version is written ----
#
# `badges/version.svg` renders the version as an image, and README.md repeats it
# in that image's alt text. Neither is prose, so neither the VERSION check above
# nor the published-release check below can see them, and the badge sat at
# `1.0-alpha3` for the whole alpha4 cycle. It is the first thing on the GitHub
# page, so it is the version most readers see and the last one anything checked.
#
# THE SVG CARRIES THE STRING THREE TIMES -- aria-label, title, and the rendered
# text node -- and a fix that updates one of them looks right in a browser while
# leaving the accessible name stale. All three are compared.
_badge="$SRCDIR/badges/version.svg"
check "premise: the version badge is present" \
	"$([ -f "$_badge" ] && echo yes || echo no)" "yes"

_badgehits="$(grep -c -- "$_ver" "$_badge" 2>/dev/null || echo 0)"
_badgeold="$(grep -oE '1\.0-[a-z]+[0-9]*' "$_badge" 2>/dev/null | LC_ALL=C sort -u | grep -vxF "$_ver" | tr '\n' ' ' | sed 's/ $//')"
check "the version badge names no version other than VERSION's" \
	"$_badgeold" ""
check "premise: and it names VERSION's version at all" \
	"$([ "${_badgehits:-0}" -ge 1 ] && echo yes || echo no)" "yes"

# README's alt text is the badge's accessible name and drifts separately from the
# image it describes.
check "README's badge alt text names the version VERSION holds" \
	"$(grep -oE 'alt="Version [^"]*"' "$SRCDIR/README.md" | sed 's/.*alt="Version //; s/"$//')" \
	"$_ver"

# ---- the OTHER version claim, which the check above cannot see --------------
#
# A second version sentence sits beside the first: "the latest published
# pre-release is `vX`". It drifted for a whole cycle in THREE documents while the
# check above stayed green, because that check only looks at files carrying the
# phrase "recorded in `VERSION`", and this is a different sentence:
#
#     README.md             v1.0-alpha2    while v1.0-alpha3 was tagged
#     docs/roadmap.md       v1.0-alpha2
#     docs/installation.md  v1.0-alpha2    and it named the tree's version wrong too
#     CHANGELOG.md          v1.0-alpha3    the only one right
#
# `docs/installation.md` is the one that shows why the scope mattered. It never
# writes the phrase "recorded in `VERSION`", so it was outside the other check
# entirely, and it also carried a stale heading and a stale upgrade chain.
#
# THIS IS AN AGREEMENT CHECK, NOT A COMPARISON, and that is a real limit. No
# tracked file holds "the newest tag" the way VERSION holds the version, and
# reading `git tag` fails in a tree copied without `.git`, which this harness is
# run from. So it catches one document drifting away from the others, which is
# what happened. It CANNOT catch every document being stale together, and the
# release procedure carries that step instead.
_pubdocs="$(grep -rln 'latest published pre-release' \
	"$SRCDIR/CHANGELOG.md" "$SRCDIR/README.md" "$SRCDIR/docs" 2>/dev/null | LC_ALL=C sort)"
check "premise: at least one document names the latest published pre-release" \
	"$([ -n "$_pubdocs" ] && echo yes || echo no)" "yes"

# BOTH WORD ORDERS, because the four documents do not agree on one. Three write
# "latest published pre-release is `vX`" and CHANGELOG.md writes "`vX` is the
# latest published pre-release". A pattern for one order silently parses three
# files and skips the fourth, which is the REFERENCE document, so the check would
# have compared the copies to each other and exempted the original.
#
# The per-file count is what makes that visible: a file that mentions the claim
# and yields no version is a file this rule cannot see, and it is named rather
# than skipped.
# shellcheck disable=SC2086
_pubvers="$(for _d in $_pubdocs; do _md "$_d"; done \
	| grep -oE 'latest published pre-release is `v[^`]*`|`v[^`]*` is the latest published pre-release' \
	| grep -oE '`v[^`]*`' | tr -d '`' | LC_ALL=C sort -u)"

_pubunparsed=""
for _d in $_pubdocs; do
	[ "$(_md "$_d" | grep -cE 'latest published pre-release is `v[^`]*`|`v[^`]*` is the latest published pre-release')" -gt 0 ] \
		|| _pubunparsed="$_pubunparsed $(basename "$_d")"
done
check "every document making the claim states it in a form this rule can read" \
	"$(printf '%s' "$_pubunparsed" | sed 's/^ //')" ""

# A DISTINCT SENTINEL, not "". An empty extraction would otherwise compare "" to
# "" and report PASS having read nothing, which is #1096's defect exactly: the
# premise caught it there and the headline still said PASS.
check "every document names the same latest published pre-release" \
	"$([ "$(printf '%s\n' "$_pubvers" | grep -c .)" = 1 ] \
	   && printf '%s' "$_pubvers" \
	   || printf 'DISAGREE:%s' "$(printf '%s' "$_pubvers" | tr '\n' ',' | sed 's/,$//')")" \
	"$(printf '%s\n' "$_pubvers" | head -1)"

# ---- and the UPGRADE CHAIN, which is a fourth place a version is written ----
#
# Two documents tell a reader which installed versions one `ALTER EXTENSION
# pgcolumnar UPDATE` can start from, and which version it arrives at. Both facts
# are knowable from the tree: the shipped `pgcolumnar--A--B.sql` files give the
# set of starting versions, and `pgcolumnar.control` gives the arrival.
#
# Neither was compared against anything. Opening the `1.0-alpha5` cycle bumped
# VERSION, the control file, the badge and META.json, and every check above went
# green, while CHANGELOG.md kept saying that the chain starts at four versions
# and arrives at `1.0-alpha4`. Five ship and it arrives at `1.0-alpha5`.
#
# THE TWO ERRORS CANCEL IF YOU COUNT, which is why this reads the two claims
# separately. CHANGELOG.md named `1.0-alpha4` once too few as a source and once
# too many as the destination, so the set of versions in the sentence was exactly
# right. A rule comparing that set against the tree would have passed on a
# sentence in which both halves were wrong.
#
# SCOPED TO THE SENTENCE, not the paragraph. The surrounding paragraph in
# docs/installation.md also names the destination twice in prose that is correct,
# so a paragraph-wide reading counts the destination as a starting version.
#
# AND THE CLAIM IS REACHABILITY, NOT MEMBERSHIP. "a SINGLE update reaches X from
# ANY of them" says the scripts form an unbroken chain, and reading only the `A`
# side of each filename cannot see that. Reported by @OffgridwithJD, who broke the
# chain without editing a document:
#
#     pgcolumnar--1.0-alpha2--1.0-alpha3.sql -> pgcolumnar--1.0-alpha2--1.0-alphaX.sql
#
# `1.0-alpha2` still starts a script, so the set of starting versions does not
# move and all four arms passed, on a tree where three of the five named versions
# cannot reach `default_version` in one command. So the population below is
# WALKED: from each starting version, follow `A--B` to the script starting at `B`,
# and keep it only if the walk ends at `default_version`. That subsumes the
# membership test, because a version whose target starts nothing drops out.
_upgsrc="$(ls "$SRCDIR"/pgcolumnar--*--*.sql 2>/dev/null \
	| sed 's|.*/pgcolumnar--||; s|\.sql$||; s|--.*||' \
	| LC_ALL=C sort -u | tr '\n' ' ' | sed 's/ $//')"
check "premise: the tree ships upgrade scripts to derive the chain from" \
	"$([ -n "$_upgsrc" ] && echo yes || echo no)" "yes"

_defver="$(sed -n "s/^[[:space:]]*default_version[[:space:]]*=[[:space:]]*'\([^']*\)'.*/\1/p" \
	"$SRCDIR/pgcolumnar.control")"
check "premise: the control file names a default_version to arrive at" \
	"$([ -n "$_defver" ] && echo yes || echo no)" "yes"

# NO VERSION MAY START TWO SCRIPTS, or the walk below would pick one of them and
# report on a chain the reader does not have. Checked rather than assumed,
# because picking silently is how a walk becomes an opinion.
_upgbranch=""
for _s in $_upgsrc; do
	_n="$(ls "$SRCDIR"/pgcolumnar--"$_s"--*.sql 2>/dev/null | wc -l)"
	[ "$_n" = 1 ] || _upgbranch="$_upgbranch $_s=$_n"
done
check "each shipped version starts exactly one upgrade script, so the chain is a walk" \
	"$(printf '%s' "$_upgbranch" | sed 's/^ //')" ""

# THE WALK. From each starting version, follow `A--B` to the script starting at
# `B` until nothing starts there. Keep the version only if it arrived at
# `default_version`. The step count is bounded because a mis-generated pair of
# scripts can form a cycle, and a guard that hangs is a guard that gets removed.
_upg_step() {	# _upg_step FROM -> the version its script targets, or empty
	ls "$SRCDIR"/pgcolumnar--"$1"--*.sql 2>/dev/null | head -1 \
		| sed "s|.*/pgcolumnar--$1--||; s|\.sql$||"
}
_upgreach=""
for _s in $_upgsrc; do
	_cur="$_s"
	_steps=0
	while [ "$_cur" != "$_defver" ] && [ "$_steps" -lt 50 ]; do
		_nxt="$(_upg_step "$_cur")"
		[ -n "$_nxt" ] || break
		_cur="$_nxt"
		_steps=$((_steps + 1))
	done
	[ "$_cur" = "$_defver" ] && _upgreach="$_upgreach $_s"
done
_upgreach="$(printf '%s' "$_upgreach" | sed 's/^ //')"
check "premise: some shipped version reaches default_version, so the walk found a chain" \
	"$([ -n "$_upgreach" ] && echo yes || echo no)" "yes"

# ONE SENTENCE PER FILE, and the count is checked rather than assumed: `grep -m1`
# reads the first and a second would go unread, which is the silent half of the
# same shape the published-release arm above was bitten by.
# A LIST MARKER BEGINS A LINE, and nothing else does. `docs/installation.md`
# yields two fragments that are nothing but "2." and "3." without this, which
# @OffgridwithJD raised expecting it to SEVER the claim if the sentence were
# moved into numbered step 3.
#
# IT DOES NOT SEVER ANYTHING, AND THAT IS STATED RATHER THAN IMPLIED. The claim
# was moved into step 3 and both halves read the whole sentence with and without
# any masking, because a marker PRECEDES a sentence rather than sitting inside
# it. No arrangement was found in which one falls inside the claim.
#
# WHAT IS MEASURED IS THE OVER-MATCH, AND IT WAS LIVE. The first version of this
# masked ANY number followed by period-space, which also protects a sentence
# ENDING in a number and merges it with the next one. On the unmodified
# documents, sentences found by each rule:
#
# MEASURED AT acc4116d, AND THE SHA IS THE LOAD-BEARING PART. These documents
# grow, so the counts drift with them: CHANGELOG.md read 66 lost at 5649eba, 69 at
# 197602f and 71 here, with nothing about the rule changing. A frozen number in a
# comment about a growing file goes stale by construction, which is the lesson
# test/check_ledger_budget.txt already carries about its own example.
#
#     docs/limitations.md    699 column-0    690 any-number    9 lost
#     docs/installation.md    74              72               2 lost
#     CHANGELOG.md          3957            3886              71 lost
#
# THE NAMED LINES BELOW ARE THE DURABLE HALF. They do not drift, and they are what
# the rule was decided on.
#
# No verdict moved, because none of those merged pairs put a stray version token
# into the claim sentence. That is a property of today's prose, not of the rule,
# which is why the tight form is the one that ships.
#
# COLUMN 0, NOT "LINE-INITIAL AFTER INDENT", and that distinction was measured
# rather than chosen. Allowing an indented marker read 3939 on CHANGELOG.md
# against @OffgridwithJD's 3942, and reconciling the three rather than splitting
# the difference found them all to be WRAPPED PROSE, not list items:
#
#       4286. The port forces the path each arm is named for and a...
#       1000. Every narrowing floors, so an instant before the epo...
#       1000. The constant mis-sized every scan and corrupted join...
#
# A sentence ending in a number, wrapped so the number starts an indented line,
# is the same over-match one indent to the right. Every real ordered-list marker
# in these documents sits at column 0: three in docs/installation.md, one in
# docs/limitations.md, none in CHANGELOG.md.
_upg_sentences() {	# _upg_sentences FILE -> one sentence per line
	# MASKED PER LINE, BEFORE `tr` JOINS THEM, because a list marker BEGINS a
	# line and nothing else does. The first version masked any number followed
	# by period-space, which also protects a sentence ENDING in a number and
	# merges it with the next one -- demonstrated by @OffgridwithJD with
	# `The corpus held 1444. ` injected ahead of the claim.
	_md "$1" | sed -E 's/^([0-9]+)\. /\1.@LM@/' \
		| tr '\n' ' ' \
		| sed 's/\. /.\n/g' \
		| sed 's/@LM@/ /g'
}
_upg_claim() {	# _upg_claim FILE -> the sentence making the upgrade-chain claim
	_upg_sentences "$1" | grep -m1 -E 'previously (shipped|published) version'
}
_upg_claims() {	# _upg_claims FILE -> how many sentences make it
	_upg_sentences "$1" | grep -cE 'previously (shipped|published) version'
}
# The destination is removed before the starting versions are collected, because
# the two claims share one sentence and each has its own arm below.
_upg_sources() {
	_upg_claim "$1" | sed 's/reaches `[^`]*`//g' \
		| grep -oE '`1\.0-[a-z0-9]+`' | tr -d '`' \
		| LC_ALL=C sort -u | tr '\n' ' ' | sed 's/ $//'
}
_upg_target() {
	_upg_claim "$1" | grep -oE 'reaches `[^`]*`' | sed 's/reaches `//; s/`$//' \
		| LC_ALL=C sort -u | tr '\n' ' ' | sed 's/ $//'
}

_upgdocs="$(grep -rlE 'previously (shipped|published) version' \
	"$SRCDIR/CHANGELOG.md" "$SRCDIR/README.md" "$SRCDIR/docs" 2>/dev/null | LC_ALL=C sort)"
check "premise: at least one document states the upgrade chain" \
	"$([ -n "$_upgdocs" ] && echo yes || echo no)" "yes"

# NAMED WITH ITS REASON, not just named. A file this rule cannot read is the way
# the rule goes quiet, so the arm says which half was missing rather than leaving
# a reader to rediscover it.
_upgunread=""
for _d in $_upgdocs; do
	_b="$(basename "$_d")"
	[ "$(_upg_claims "$_d")" = 1 ] || _upgunread="$_upgunread $_b:claims=$(_upg_claims "$_d")"
	[ -n "$(_upg_sources "$_d")" ] || _upgunread="$_upgunread $_b:no-starting-versions"
	[ -n "$(_upg_target "$_d")" ] || _upgunread="$_upgunread $_b:no-destination"
done
check "every document stating the upgrade chain states it in a form this rule can read" \
	"$(printf '%s' "$_upgunread" | sed 's/^ //')" ""

# BOTH SIDES CARRY THE VERSIONS (#1164). An arm that names only the offending
# file reports which document is wrong and not what is wrong with it, and the
# reader then has to re-derive the tree's own answer to find out.
_upggot=""
_upgwant=""
for _d in $_upgdocs; do
	_upggot="$_upggot $(basename "$_d")=[$(_upg_sources "$_d")]"
	_upgwant="$_upgwant $(basename "$_d")=[$_upgreach]"
done
check "every such document names the versions that reach default_version in one update" \
	"${_upggot# }" "${_upgwant# }"

_upggot=""
_upgwant=""
for _d in $_upgdocs; do
	_upggot="$_upggot $(basename "$_d")=[$(_upg_target "$_d")]"
	_upgwant="$_upgwant $(basename "$_d")=[$_defver]"
done
check "every such document names default_version as the version one UPDATE reaches" \
	"${_upggot# }" "${_upgwant# }"
# ---- and META.json, which NOTHING read at all ------------------------------
#
# `META.json` is the PGXN distribution metadata. It hardcodes the version TWICE
# and names the base install script by filename, and no suite, no Makefile rule
# and no CI step ever read it. It went stale for the whole alpha4 cycle:
#
#     version                    1.0.0-alpha.3     while VERSION said 1.0-alpha4
#     provides.pgcolumnar.file   pgcolumnar--1.0-alpha3.sql
#
# THE FILENAME IS THE PART THAT MATTERS. `pgcolumnar--1.0-alpha3.sql` does not
# exist: it was RENAMED to the alpha4 name when the cycle opened, which is how
# this repository makes each cycle's base script. So the published metadata named
# a file the distribution does not contain, and the version strings were only the
# visible half.
#
# The two version forms differ by convention and that is not a bug: PGXN requires
# three-part semver, so `1.0-alpha4` is published as `1.0.0-alpha.4`. The mapping
# is derived here rather than hardcoded, so a future `1.0-beta1` is covered too.
_meta="$SRCDIR/META.json"
check "premise: META.json is present and parses" \
	"$(python3 -c "import json,sys;json.load(open(sys.argv[1]));print('yes')" "$_meta" 2>/dev/null || echo no)" "yes"

# VERSION `1.0-alpha4` -> PGXN `1.0.0-alpha.4`: pad the numeric part to three
# components, and put a dot before the suffix's trailing digits.
_pgxn_ver="$(printf '%s' "$_ver" | awk -F- '{
	n = $1; c = split(n, p, "."); while (c < 3) { n = n ".0"; c++ }
	if (NF > 1) { s = $2; sub(/[0-9]+$/, ".&", s); print n "-" s } else print n
}')"
check "premise: the PGXN form was derived from VERSION, not empty" \
	"$([ -n "$_pgxn_ver" ] && echo yes || echo no)" "yes"

_meta_get() {	# _meta_get DOTTED.KEY -> that value of META.json, or empty
	python3 -c "
import json,sys
m=json.load(open(sys.argv[1]))
for k in sys.argv[2].split('.'): m=m[k]
print(m)" "$_meta" "$1" 2>/dev/null
}
# TWO LITERAL CHECKS, not a loop: a name built from a loop variable is invisible to the
# twin grader, which reads only literal names.
check "META.json version is the version VERSION holds, in PGXN form" \
	"$(_meta_get version)" "$_pgxn_ver"
check "META.json provides.pgcolumnar.version is the version VERSION holds, in PGXN form" \
	"$(_meta_get provides.pgcolumnar.version)" "$_pgxn_ver"

# The defect that actually shipped. `git archive` rather than a filesystem test,
# because what matters is whether the DISTRIBUTION contains it -- an export-ignore
# rule could drop a file that is present in the tree.
_meta_file="$(python3 -c "
import json,sys
print(json.load(open(sys.argv[1]))['provides']['pgcolumnar']['file'])" "$_meta" 2>/dev/null)"
check "premise: META.json names a base install script" \
	"$([ -n "$_meta_file" ] && echo yes || echo no)" "yes"
check "the script META.json names is in the published distribution" \
	"$(cd "$SRCDIR" && git archive HEAD 2>/dev/null | tar -t 2>/dev/null | grep -cx "$_meta_file")" "1"
# Shipped is not enough: it must be the BASE script (#1339). `provides.file` is the
# script a fresh install runs, and every upgrade script ships too, so naming
# `pgcolumnar--1.0-alpha4--1.0-alpha5.sql` passed the arm above.
check "META.json names the base install script of the version VERSION holds" \
	"$_meta_file" "pgcolumnar--$_ver.sql"

# AND THE SCRIPTS META.json CANNOT NAME. `provides.file` is ONE filename, so the arm
# above cannot notice an `export-ignore` that drops a DIFFERENT install script -- an
# upgrade path. That breaks `ALTER EXTENSION ... UPDATE` for anyone who installed
# from PGXN, and nothing else here would see it.
#
# IT IS HERE AND NOT ONLY IN THE PYTEST TWIN FOR A RELEASE REASON. The pytest guards
# run in CI; the five-major shell matrix is the release gate. An arm protecting the
# upgrade path for published installs belongs in the gate that runs before a tag.
# Raised in review of #1086, where it existed only on the python side.
_meta_ship="$(cd "$SRCDIR" && git archive HEAD 2>/dev/null | tar -t 2>/dev/null)"
_meta_ondisk="$(cd "$SRCDIR" && ls pgcolumnar--*.sql 2>/dev/null)"
check "premise: the tree has install scripts to check" \
	"$([ -n "$_meta_ondisk" ] && echo yes || echo no)" "yes"
#
# NO PIPE INTO AN EARLY-EXIT READER (#486). The first version of this loop was
# `printf '%s\n' "$_meta_ship" | grep -qx "$_ms"`, which is exactly the shape
# selftest/080 refuses: a builtin writing a captured string into a reader that
# exits on its first match. `$_meta_ship` is the whole `git archive | tar -t`
# listing, 4999 bytes on this tree -- right at the pipe-buffer boundary where the
# shape works almost every time and then does not.
#
# The newline sentinels on both sides give the anchored match `grep -x` was
# providing, without a subprocess. Caught by 080 in review of #1086, which is the
# rule doing its job on arrival: the arm was fine in the pytest twin and only
# became subject to 080 when it entered the shell harness.
_meta_missing=""
for _ms in $_meta_ondisk; do
	case $'\n'"$_meta_ship"$'\n' in
		*$'\n'"$_ms"$'\n'*)	;;
		*)	_meta_missing="$_meta_missing $_ms" ;;
	esac
done
# A SENTINEL, not an empty expectation: comparing against "" passes on anything
# empty, including a sweep that produced nothing at all.
check "every pgcolumnar--*.sql in the tree is in the published distribution" \
	"$([ -z "$_meta_missing" ] && echo "all shipped" || printf '%s' "${_meta_missing# }")" \
	"all shipped"


# ---- the changelog's shared anchor, and the guard the fix needs (#996) -------
#
# Every PR that adds an entry inserts as the first child of one heading, so any
# two conflict for a reason unrelated to either change. Nine of 27 merges in one
# day touched this file, and three merge commits that day exist only to resolve
# it. `.gitattributes` now gives CHANGELOG.md a UNION merge driver, which takes
# both sides with no marker.
#
# MEASURED ON THE REAL PAIR, #1098 and #1106, both of which add a new
# `## [Unreleased]` section:
#
#     default 3-way   rc=1, 2 conflict markers
#     merge=union     rc=0, 0 markers, ONE `## [Unreleased]`, both entries intact
#
# The section headers are not doubled because union emits identical lines once:
# 7640 + 57 + 52 = 7749 against an actual 7744, and the 5 are the shared
# `## [Unreleased]` / blank / `### Fixed` / blank prefix.
#
# UNION'S HAZARD IS REAL AND THIS FILE MEETS IT AT EVERY RELEASE. Union keeps
# both sides of a divergent hunk silently, so where one branch EDITS a line that
# another appends beneath, the append survives under the edit. A release cut
# edits exactly that line -- `## [Unreleased]` becomes `## [1.0-alphaN] - date`.
# Reproduced: a PR appending an entry, merged into a release cut, lands that
# entry INSIDE the section that just shipped, with rc=0 and no marker. Today the
# same case conflicts and a human sees it.
#
# So the driver ships with the check below, which is what makes it safe: an entry
# cannot appear in a released section after that release's tag without this
# saying so.
check "CHANGELOG.md has a union merge driver, so two entries do not conflict" \
	"$(grep -c '^CHANGELOG\.md[[:space:]]\+merge=union$' "$SRCDIR/.gitattributes")" "1"

# Portable awk: no gawk-only three-argument match(). Identical output under mawk,
# gawk and this box's default awk, checked rather than assumed.
_cl_versions() {	# stdin: a CHANGELOG -> one DATED section version per line
	awk '
		/^## \[[^]]+\] - / {
			line = $0; sub(/^## \[/, "", line); sub(/\].*$/, "", line); print line
		}
	'
}

# THE KEY IS THE ENTRY, NOT A COUNT. Counting would let one post-tag entry be
# swapped for another with the arm still green -- the same "an aggregate that
# balances" failure this repository has been finding all week, one level up.
_cl_entries() {	# _cl_entries VERSION; stdin: a CHANGELOG -> that section's entry first lines
	awk -v want="$1" '
		/^## \[/ {
			insec = 0; line = $0
			if (line ~ /^## \[[^]]+\] - /) {
				sub(/^## \[/, "", line); sub(/\].*$/, "", line)
				if (line == want) insec = 1
			}
			next
		}
		insec && /^- / { print }
	'
}

_cl_git() { git -C "$SRCDIR" "$@" 2>/dev/null; }
_cl_allowfile="$SRCDIR/test/changelog_post_tag.txt"

# STALE LOCAL TAGS ARE THE FAILURE MODE HERE, and it is not hypothetical: the
# first version of this check reported `v1.0-alpha3` as shipping no dated section
# at all, because this tree's local tag was 5 commits behind the server and
# `git fetch` NEVER moves a tag that already exists. That produced a wrong
# narrative, a wrong false-positive budget, and a green arm where the tree is
# actually in violation. Caught by jdatcmd, who checked their own refs against
# `git ls-remote` before saying so.
#
# If an arm below fails and the section looks right, check the tag before
# believing it:
#
#     git ls-remote --tags origin 'refs/tags/v1.0-alpha*'
#     git fetch --tags --force origin
if ! _cl_git rev-parse --git-dir >/dev/null; then
	_cl_why="no git repository in the tree under test"
elif [ -z "$(_cl_git tag -l 'v1.0-alpha*')" ]; then
	# CI checks out at depth 1 with no tags, so NONE of this runs there and a
	# green check on this job says nothing about it. It runs locally and in the
	# five-major release gate, which is where a release is cut and therefore
	# where an entry can be filed into a closed section.
	_cl_why="no release tags in this checkout"
else
	_cl_why=""
fi

_cl_seen=0
_cl_usedrows=""
_cl_nosec=""
_cl_bad=""
_cl_lost=""
# THREE CHECKS WITH FIXED NAMES, each naming the versions it found wrong in its
# reading. One check per version per property put the version in the NAME, which no
# twin grader can read and which grows by two names every release (#1341 port).
while read -r _cl_v; do
	[ -n "$_cl_v" ] || continue
	_cl_seen=$((_cl_seen + 1))
	[ -z "$_cl_why" ] || continue
	_cl_tagged="$(_cl_git show "v$_cl_v:CHANGELOG.md" | _cl_entries "$_cl_v" | LC_ALL=C sort)"
	if [ -z "$_cl_tagged" ]; then
		_cl_nosec="$_cl_nosec $_cl_v"
		continue
	fi
	_cl_nowents="$(_cl_entries "$_cl_v" < "$SRCDIR/CHANGELOG.md" | LC_ALL=C sort)"
	# LC_ALL=C on BOTH the sorts and the comm. `comm` compares byte-wise and does
	# not check that its inputs agree; fed two collations it returns wrong lines
	# rather than an error. Selftest 070 enforces this over the whole file, which is
	# why the sorts above that predate this block are pinned too.
	_cl_extra="$(LC_ALL=C comm -13 <(printf '%s\n' "$_cl_tagged") <(printf '%s\n' "$_cl_nowents"))"
	_cl_gone="$(LC_ALL=C comm -23 <(printf '%s\n' "$_cl_tagged") <(printf '%s\n' "$_cl_nowents"))"
	_cl_allowed="$(awk -F'\t' -v v="$_cl_v" '$1==v && $2 != "" {print $2}' "$_cl_allowfile" 2>/dev/null | LC_ALL=C sort)"
	[ -n "$_cl_allowed" ] && _cl_usedrows="$_cl_usedrows$_cl_allowed
"
	[ "$_cl_extra" = "$_cl_allowed" ] \
		|| _cl_bad="$_cl_bad $_cl_v: added [$(printf '%s' "$_cl_extra" | tr '\n' '|')] recorded [$(printf '%s' "$_cl_allowed" | tr '\n' '|')]"
	[ -z "$_cl_gone" ] || _cl_lost="$_cl_lost $_cl_v: [$(printf '%s' "$_cl_gone" | tr '\n' '|')]"
done <<CLEOF
$(_cl_versions < "$SRCDIR/CHANGELOG.md")
CLEOF
# WITHOUT TAGS THESE ARE NOTES, as they always were: the nightly coverage job checks
# out with no tags and counts any non-zero suite as failed, so recording them unrunnable
# there would redden a job for a checkout's shape. Every job that runs this suite to
# decide anything (ci suites, nightly suites) fetches the release tags.
if [ -n "$_cl_why" ]; then
	note "every dated changelog section has its own section at its release tag: not compared ($_cl_why)"
	note "every dated changelog section holds what its tag shipped, plus only what is recorded: not compared ($_cl_why)"
	note "every dated changelog section still holds every entry its tag shipped: not compared ($_cl_why)"
else
	check "every dated changelog section has its own section at its release tag" "${_cl_nosec# }" ""
	check "every dated changelog section holds what its tag shipped, plus only what is recorded" "${_cl_bad# }" ""
	check "every dated changelog section still holds every entry its tag shipped" "${_cl_lost# }" ""
fi
$(_cl_versions < "$SRCDIR/CHANGELOG.md")
CLEOF

# The sweep is a claim. An empty one would make every arm above vanish and this
# suite would report clean having compared no section at all.
check "premise: the changelog sweep found dated release sections" \
	"$([ "$_cl_seen" -ge 3 ] && echo yes || echo no)" "yes"

# ---- and the recorded exceptions are themselves checked ---------------------
#
# A file of allowances is a second place to be wrong. Two arms: every row must
# carry a reason, and no row may be stale -- an allowance for an entry that is no
# longer extra would silently widen what the arms above accept.
_cl_rows="$(grep -cE '^[^#]' "$_cl_allowfile" 2>/dev/null || true)"
check "premise: the post-tag allowance file is present and readable" \
	"$([ -f "$_cl_allowfile" ] && echo yes || echo no)" "yes"
_cl_noreason="$(awk -F'\t' '/^[^#]/ && NF > 0 && $3 == "" {print $1 " " $2}' "$_cl_allowfile" 2>/dev/null)"
check "every recorded post-tag entry carries a reason" "$_cl_noreason" ""
if [ -z "$_cl_why" ]; then
	_cl_declared="$(awk -F'\t' '/^[^#]/ && $2 != "" {print $2}' "$_cl_allowfile" 2>/dev/null | LC_ALL=C sort)"
	_cl_stale="$(LC_ALL=C comm -23 <(printf '%s\n' "$_cl_declared") <(printf '%s\n' "$_cl_usedrows" | LC_ALL=C sort -u))"
	check "no recorded post-tag entry is stale, so the allowance cannot widen silently" \
		"$_cl_stale" ""
else
	note "no recorded post-tag entry is stale, so the allowance cannot widen silently: not compared ($_cl_why)"
fi


# ---- the union driver's instruction is written down (#1116) ------------------
#
# GitHub does not read `.gitattributes`, so a PR shows CONFLICTING even where the
# driver resolves the merge cleanly. A contributor who clicks "Update branch" gets
# the conflict the driver exists to remove. Measured on four branches: clean
# locally, CONFLICTING on the web, throughout.
#
# The driver without the instruction is worse than either alone -- it makes the
# badge lie and gives nobody the reason -- so the two are asserted together.
# NOT A PREMISE: it is the same repository fact as the driver check above, and removing
# that line from .gitattributes reddens both. A premise must not go red under a change
# to what the suite checks (#1339), so this is an ordinary check.
check "and CHANGELOG.md still has the union merge driver those instructions are about" \
	"$(grep -c '^CHANGELOG\.md[[:space:]]\+merge=union$' "$SRCDIR/.gitattributes")" "1"
check "and CONTEXT.md tells a contributor what a CONFLICTING badge means" \
	"$(grep -c 'CONFLICTING badge on CHANGELOG.md means rebase locally' "$SRCDIR/CONTEXT.md")" "1"
check "and names the command rather than only the problem" \
	"$(grep -c 'git rebase origin/main' "$SRCDIR/CONTEXT.md")" "1"
check "and warns against the button that reintroduces the conflict" \
	"$(grep -ci 'do not click' "$SRCDIR/CONTEXT.md")" "1"
# ANCHORED ON ONE LINE. The first spelling of this arm spanned the prose's line
# break -- "the tree it is / merging into" -- so a line-based grep found 0 on the
# unmutated tree and the arm was red for the sentence it was asserting. The mutation
# then reddened it too, which looked like a working proof and was two failures
# agreeing. Same class as a drift-guard that does not join line continuations.
check "and says WHY rebasing works where merging does not" \
	"$(grep -c 'merging \*\*into\*\*' "$SRCDIR/CONTEXT.md")" "1"


# A QUERY CANNOT ASK ANOTHER DATABASE WHAT EXTENSIONS IT HAS (#1288). pg_extension is a
# per-database catalog, so a statement that reads pg_database and tests pg_extension in the
# same breath answers for the CURRENT database only: it lists every connectable database or
# none. docs/installation.md shipped exactly that as "the databases that need the upgrade".
# Read per statement inside ```sql blocks, so prose that explains the rule cannot trip it.
_xdb_stmts="$(for f in $docs; do
	awk '/^[[:space:]]*```sql/ {b=1; next} b && /^[[:space:]]*```/ {b=0; print ";"; next} b {printf "%s ", $0}' "$f"
done | tr ';' '\n')"
# The failure carries the count it measured: selftest part 540 refuses an arm that
# answers only "no".
_xdb_n="$(printf '%s\n' "$_xdb_stmts" | grep -c 'SELECT')"
check "premise: the scan read SQL statements from the documents" \
	"$([ "$_xdb_n" -ge 10 ] && echo yes || echo "no ($_xdb_n SELECT statements, want at least 10)")" "yes"
check "no documented query tests pg_extension while listing databases" \
	"$(printf '%s\n' "$_xdb_stmts" | grep 'pg_database' | grep -c 'pg_extension')" "0"


pgc_summary
