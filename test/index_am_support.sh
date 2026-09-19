#!/usr/bin/env bash
#
# The index access methods docs/features.md CLAIMS must actually work.
#
# The page said "builds btree and hash indexes over a columnar table" for as long
# as it has existed. GiST and SP-GiST build and answer queries too, and a reader
# of that sentence concluded the opposite -- which is how this suite came to be
# written. A range column's overlap and containment queries have no other fast
# path: those operators never prune chunk groups on a scan (see
# docs/limitations.md, "Which predicates prune"), so an index is the whole story.
#
# IT READS THE DOCUMENT RATHER THAN CARRYING A LIST. A suite with its own copy of
# the list cannot see the page drift away from it, which is the defect this
# exists about. The precedent is doc_parallel_premise.sh, which extracts the
# published query from limitations.md for the same reason. The access methods are
# extracted from the backticked names in the sentence on features.md, and every
# name found is exercised here.
#
# SO ADDING A METHOD TO THE PAGE WITHOUT EVIDENCE TURNS THIS SUITE RED. A name
# with no probe below fails rather than being skipped: the claim would otherwise
# be published and untested, which is the state the page was already in.
#
# WHAT IS NOT CLAIMED. GIN and BRIN also BUILD on a columnar table. They are
# deliberately absent from the page and from this suite, because building is not
# the same as being usable and nobody has measured a plan that chooses them.
# Tracked separately; adding either to the page without a probe reddens this.
#
# Usage:  test/index_am_support.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

SRCROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FEATURES="$SRCROOT/docs/features.md"

ROWS="${PGC_INDEX_AM_ROWS:-20000}"

# The bullet the CREATE INDEX sentence lives in, as ONE normalised line.
claim_line() {
	awk '
		/CREATE INDEX. builds/ { grab = 1 }
		grab && NR > 1 && (/^$/ || (/^- / && !/CREATE INDEX. builds/)) { exit }
		grab { line = line " " $0 }
		END { gsub(/[ \t]+/, " ", line); sub(/^ /, "", line); print line }
	' "$FEATURES"
}

# THE CLAIM IS WHITELISTED, NOT SCREENED FOR NEGATIONS. The first version of this
# suite refused a window containing `not|never|except|...`, which @OffgridwithJD
# broke twice: once with the edit it was written for, and then with
#
#     ...and `gist` but NOT `spgist` indexes            refused, correctly
#     ...though `spgist` cannot be chosen               PASSED
#
# because `\bnot\b` has no word boundary before the `not` inside `cannot`. The
# fix is not another token. @OffgridwithJD's own correction is the reason: a
# denylist of negations is the losing game they had told me to avoid, one round
# at a time -- `isn'"'"'t`, `no longer`, `save for`, and finally a sentence that
# inverts the claim with no negation token in it at all.
#
# So the bullet has exactly ONE legitimate form and anything else is refused:
#
#     - `CREATE INDEX` builds `a`, `b` and `c` indexes over a columnar table.
#
# A page edit adding legitimate prose to that bullet is refused too, and that is
# the point: the refusal asks for its own sentence, which the page now gives it.
CLAIM_FORM='^- `CREATE INDEX` builds (`[a-z_]+`(, `[a-z_]+`)*( and `[a-z_]+`)?) indexes over a columnar table\.$'

# MATCHED WITH [[ =~ ]], NOT THROUGH A PIPE INTO `grep -q`. selftest/080 refuses
# that shape (#486): `grep -q` exits as soon as it has its answer, the writer
# takes EPIPE, and under `pipefail` the pipeline reports the pattern ABSENT
# whatever the string held -- always in the direction that sends someone looking
# for a defect that is not there. This file's own header cites that rule, and the
# first version of these two functions broke it twice anyway. harness_selftest
# caught it, which is the answer to why the rule is a suite and not a convention.
claim_matches() {	# 0 when the bullet is the one form this suite can read
	[[ "$(claim_line)" =~ $CLAIM_FORM ]]
}

claimed_ams() {
	claim_matches || return 0
	# `grep -o` reads to EOF and its OUTPUT is the answer rather than its exit
	# status, so it is not the shape above. Left as a pipeline deliberately.
	claim_line | grep -oE '`[a-z_]+`' | tr -d '`' | grep -v '^CREATE$'
}

AMS="$(claimed_ams | sort -u | tr '\n' ' ')"
AMS="${AMS% }"
echo "-- docs/features.md claims: ${AMS:-<nothing>}"

# THE EXTRACTOR IS A CLAIM TOO. An awk pattern that matches nothing prints
# nothing, and a loop over nothing passes every arm under it. Assert it found
# something before believing any result below.
# THE BULLET MUST HOLD THE CLAIM AND NOTHING ELSE, asserted FIRST because it is
# the premise that explains the other: a bullet this suite cannot read yields no
# names, so the names check below fails as a CONSEQUENCE. The pytest twin stops
# at its first failing assertion and would otherwise report only that. Everything below reads
# names and nothing reads prose, so a qualification written into the same bullet
# inverts the page'"'"'s meaning while every arm passes on the names it found.
CLAIM="$(claim_line)"
if claim_matches; then
	FORM="the claim and nothing else"
else
	FORM="unreadable"
	echo "-- refusing the claim bullet; it is not the one form this suite reads:"
	echo "--   $CLAIM"
	echo "-- expected: - \`CREATE INDEX\` builds \`a\`, \`b\` and \`c\` indexes over a columnar table."
	echo "-- put anything else in its own bullet; this suite reads NAMES and cannot read prose"
fi
check "premise: the claim bullet holds the claim and nothing else" \
	"$FORM" "the claim and nothing else"

check "premise: the features page names at least one index access method" \
	"$(awk -v n="$(claimed_ams | wc -l)" 'BEGIN { print (n + 0 > 0) ? "named" : "named-nothing" }')" \
	"named"


psql_run "DROP TABLE IF EXISTS iam;"
psql_run "CREATE TABLE iam (id int, txt text, span tstzrange) USING pgcolumnar;"
psql_run "INSERT INTO iam
	SELECT g, 'v' || g,
	       tstzrange(timestamptz '2020-01-01' + (g || ' minutes')::interval,
	                 timestamptz '2020-01-01' + ((g + 60) || ' minutes')::interval)
	FROM generate_series(1, $ROWS) g;"

check "premise: the fixture holds every row" \
	"$(q "SELECT count(*) FROM iam;" | tail -1)" "$ROWS"

# Each method's column, its operator, and the probe value. A method the page names
# and this table does not cover falls through to the failure below.
am_column()  { case "$1" in btree|hash) echo "id";; gist|spgist) echo "span";; *) echo "";; esac; }
am_predicate() {
	case "$1" in
		btree)  echo "id = 4242";;
		hash)   echo "id = 4242";;
		gist)   echo "span && tstzrange('2020-01-01 02:00','2020-01-01 03:00')";;
		spgist) echo "span @> timestamptz '2020-01-01 02:30'";;
		*)      echo "";;
	esac
}

# The scan path's answer, taken with no index in play, is the oracle. Comparing an
# index answer against a literal would pin today's fixture instead of the
# property, and the property is that the two paths agree.
for am in $AMS; do
	col="$(am_column "$am")"
	pred="$(am_predicate "$am")"

	check "premise: $am, which the page claims, has a probe in this suite" \
		"$([ -n "$col" ] && [ -n "$pred" ] && echo probed || echo "no probe")" \
		"probed"
	[ -n "$col" ] && [ -n "$pred" ] || continue

	psql_run "DROP INDEX IF EXISTS iam_probe;"
	built="$(q "CREATE INDEX iam_probe ON iam USING $am ($col); SELECT 'built';" | tail -1)"
	check "$am builds an index over a columnar table" "$built" "built"

	want="$(q "SET enable_indexscan = off; SET enable_indexonlyscan = off;
		SET enable_bitmapscan = off;
		SELECT count(*) FROM iam WHERE $pred;" | tail -1)"
	got="$(q "SET pgcolumnar.enable_custom_scan = off; SET enable_seqscan = off;
		SELECT count(*) FROM iam WHERE $pred;" | tail -1)"

	# THE ANSWER MUST BE NON-EMPTY as well as equal. Two paths that both return
	# nothing agree, and a predicate matching no row would satisfy the arm below
	# while exercising neither path.
	check "premise: the $am predicate matches rows at all" \
		"$(awk -v n="${want:-0}" 'BEGIN { print (n + 0 > 0) ? "matches" : "matches-nothing" }')" \
		"matches"

	check "$am answers its operator with the same rows the scan returns" "$got" "$want"
done

psql_run "DROP INDEX IF EXISTS iam_probe;"

# THE OTHER HALF OF THE PAGE'S CLAIM, and the reason the range methods are on it.
# An overlap query has no scan-side pruning, so the index is not an optimisation
# here, it is the only fast path. This asserts the plan rather than the timing.
check "premise: an overlap query reaches the row through a GiST index" \
	"$(q "SET pgcolumnar.enable_custom_scan = off; SET enable_seqscan = off;
		DROP INDEX IF EXISTS iam_probe;
		CREATE INDEX iam_probe ON iam USING gist (span);
		EXPLAIN (COSTS OFF) SELECT count(*) FROM iam
		 WHERE span && tstzrange('2020-01-01 02:00','2020-01-01 03:00');" |
		grep -cE 'Index (Only )?Scan using iam_probe')" \
	"1"

pgc_summary
