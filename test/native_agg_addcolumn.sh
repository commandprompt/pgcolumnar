#!/usr/bin/env bash
#
# pgColumnar metadata aggregates over a column added by ALTER TABLE.
#
# An ungrouped, unfiltered aggregate is answered from row group metadata. A
# column added by ALTER TABLE ADD COLUMN has no chunk, and so no zone map, in any
# row group written before it existed: its value for those rows is the
# attribute's missing value, which the reader supplies through getmissingattr and
# a zone map cannot describe at all.
#
# Folding such a group from its zone maps dropped every one of those rows in
# silence. On main before the fix, over 3,000 rows and a column added with
# DEFAULT 7:
#
#     count(d)  0     against 3000 from a scan
#     sum(d)    null  against 21000
#     min(d)    null  against 7
#
# No error, and the scan path disagreed with the metadata path on the same query,
# which is the shape this project treats as the worst kind of defect.
#
# The oracle throughout is the same query with pgcolumnar.enable_vectorization
# off, which reads the rows. Comparing the two paths against each other is
# stronger than comparing either to a hand-computed value: it is the property
# that has to hold, and it holds whatever the data is.
#
# Usage:  test/native_agg_addcolumn.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

ROWS=${PGC_ADDCOL_ROWS:-3000}

# Both paths must agree; the scan path is the oracle.
#
# The aggregate is selected BARE. An earlier version of this file wrote
# "SELECT (count(d))::text", and the cast is enough to stop the planner choosing
# the metadata path at all -- so both sides fell back to a scan, agreed with each
# other, and the suite passed against the unfixed build. Anything wrapped around
# the aggregate here silently turns this file into a test of nothing.
#
# A NULL answer prints as "(null)" through psql itself, and the oracle's setting
# rides in PGOPTIONS rather than a SET, so a failed query on either side prints
# nothing and check_text refuses it. The earlier form mapped empty to "(null)" and
# kept only the last line, which for a failed oracle was the SET's own tag: two
# failures disagreed only because one printed "SET".
#
# A reading that crashes the backend puts the server through recovery, so each
# reading first waits until it answers: a crash fails its own arm, not whichever
# arms happen to run while recovery is under way.
qn() {	# SQL [PGOPTIONS]
	local _i
	for _i in $(seq 120); do [ "$(q 'SELECT 1;')" = "1" ] && break; sleep 0.5; done
	env PATH="$PGC_BINDIR:$PATH" PGOPTIONS="${2:-}" psql -h 127.0.0.1 -p "$PGC_PORT" \
		-U postgres -d "$PGC_DB" -At -P 'null=(null)' -c "$1" 2>/dev/null || true
}
both() {  # label, aggregate expression, table
	check_text "$1" "$(qn "SELECT $2 FROM $3;")" \
		"$(qn "SELECT $2 FROM $3;" "-c pgcolumnar.enable_vectorization=off")"
}

# --- a column added with a constant default ------------------------------------

psql_run "DROP TABLE IF EXISTS ac_t;
	CREATE TABLE ac_t (id int) USING pgcolumnar;
	INSERT INTO ac_t SELECT g FROM generate_series(1, $ROWS) g;
	ALTER TABLE ac_t ADD COLUMN d int DEFAULT 7;
	ALTER TABLE ac_t ADD COLUMN e text DEFAULT 'zz';" >/dev/null

check "the metadata aggregate path is the one under test" \
	"$(q "EXPLAIN (COSTS off) SELECT count(d) FROM ac_t;" \
		| grep -c 'Columnar Vectorized Aggregates')" "1"
# the oracle's setting arrives through PGOPTIONS, where a mistyped or unloaded GUC
# would leave both sides on the metadata path; an empty plan is "no plan", so a
# failed EXPLAIN cannot pass as the scan
path_of() {	# PGOPTIONS -> metadata | scan | no plan
	local plan
	plan="$(qn "EXPLAIN (COSTS off) SELECT count(d) FROM ac_t;" "$1")"
	case "$plan" in
		'') echo "no plan" ;;
		*'Columnar Vectorized Aggregates'*) echo metadata ;;
		*) echo scan ;;
	esac
}
check "premise: the oracle reads the rows" \
	"$(path_of "-c pgcolumnar.enable_vectorization=off")" "scan"
# Both paths read an old group's added column from the reader's missing value, so a
# reader that lost it would leave them agreeing on the wrong answer: the oracle's own
# answer is pinned once, to the rows and the default (7) that went in
check_text "the scan reads an added column's default for the rows written before it" \
	"$(qn "SELECT count(d) || '|' || sum(d) FROM ac_t;" "-c pgcolumnar.enable_vectorization=off")" \
	"$ROWS|$((ROWS * 7))"

both "count over an added int column"   "count(d)" ac_t
both "sum over an added int column"     "sum(d)"   ac_t
both "min over an added int column"     "min(d)"   ac_t
both "max over an added int column"     "max(d)"   ac_t
both "avg over an added int column"     "avg(d)"   ac_t
both "count over an added text column"  "count(e)" ac_t
both "min over an added text column"    "min(e)"   ac_t

# count(*) never reads a column, so it was right before the fix too; it is here
# to catch a fix that breaks the case that already worked
both "count(*) is unaffected" "count(*)" ac_t

# --- a column added without a default ------------------------------------------

# The old rows are genuinely null here, so count(col) really is 0 for them. This
# is the case the broken code got right by accident, and it must stay right.
psql_run "DROP TABLE IF EXISTS ac_n;
	CREATE TABLE ac_n (id int) USING pgcolumnar;
	INSERT INTO ac_n SELECT g FROM generate_series(1, $ROWS) g;
	ALTER TABLE ac_n ADD COLUMN d int;" >/dev/null

both "count over an added column with no default" "count(d)" ac_n
both "sum over an added column with no default"   "sum(d)"   ac_n

# --- rows written after the column exists --------------------------------------

# Now the table has groups on both sides of the ALTER: older ones with no chunk
# for d, newer ones with a real chunk and a zone map. The aggregate has to fold
# both, which is the case a fix that simply ignores older groups would fail.
psql_run "INSERT INTO ac_t SELECT g, 100, 'new'
	FROM generate_series($((ROWS + 1)), $((ROWS + 500))) g;" >/dev/null

both "count across groups written either side of the ALTER" "count(d)" ac_t
both "sum across groups written either side of the ALTER"   "sum(d)"   ac_t
both "max across groups written either side of the ALTER"   "max(d)"   ac_t
both "min over text across both sides"                      "min(e)"   ac_t

# and a delete, so the per-group delete handling and the missing-column handling
# have to work together
psql_run "DELETE FROM ac_t WHERE id = 5;" >/dev/null
both "count with both a delete and an added column" "count(d)" ac_t
both "sum with both a delete and an added column"   "sum(d)"   ac_t

pgc_summary
