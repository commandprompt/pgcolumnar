#!/usr/bin/env bash
# The alpha5 release notes' repair procedure works as written.
#
# Two alpha4 defects (#1211, #1275) left a table's pgcolumnar.storage row naming a
# relation that no longer exists, and the upgrade does not repair rows that already
# exist. RELEASE_NOTES_1.0-alpha5.md, "After the upgrade: repair entries alpha4 left
# wrong", gives a query that finds them and two repairs. Instructions are validated
# by performing them, so this suite performs them.
#
# THE BROKEN STATE IS MADE, NOT INHERITED. This build no longer produces it, so the
# suite writes it directly: each storage row is pointed at the OID of a table created
# and dropped for the purpose. That is the state measured on PG17 with a real alpha4
# build (#1288): relation_oid naming a dropped relation, everything else intact.
#
# THE QUERY IS READ FROM THE NOTES, not copied here. If the notes' query is edited,
# this suite runs the edited one, so the document and the check cannot drift apart.
#
# Usage:  test/storage_row_repair.sh [PG_CONFIG]

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/lib/postgresql/18/bin/pg_config}"

q "CREATE EXTENSION IF NOT EXISTS pgcolumnar;" >/dev/null

NOTES="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/RELEASE_NOTES_1.0-alpha5.md"
# The first ```sql block after the step that introduces the query.
FIND_SQL="$(awk '/find the rows that name no relation/ {f=1; next}
	f && /```sql/ {g=1; next} g && /```/ {exit} g {print}' "$NOTES" 2>/dev/null)"
# A statement in the notes ends with ';', which cannot sit inside a subquery.
FIND_SQL="${FIND_SQL%;}"
check "premise: the release notes give the query that finds wrong rows" \
	"$(printf '%s\n' "$FIND_SQL" | grep -c 'pgcolumnar.storage')" "1"

q "CREATE TABLE srr_t (id int, b int) USING pgcolumnar;
   INSERT INTO srr_t SELECT g, g FROM generate_series(1,1000) g;
   CREATE MATERIALIZED VIEW srr_mv USING pgcolumnar AS SELECT id FROM srr_t WITH DATA;
   CREATE TABLE srr_gone_a (x int); CREATE TABLE srr_gone_b (x int);" >/dev/null
gone_a="$(q "SELECT 'srr_gone_a'::regclass::oid;")"
gone_b="$(q "SELECT 'srr_gone_b'::regclass::oid;")"
q "DROP TABLE srr_gone_a, srr_gone_b;" >/dev/null

# Count of rows the notes' query reports.
found() { q "SELECT count(*) FROM ($FIND_SQL) z;"; }
names_itself() {	# names_itself RELNAME -> 1 when its storage row names it
	q "SELECT (relation_oid = '$1'::regclass)::int FROM pgcolumnar.storage
	   WHERE storage_id = pgcolumnar.get_storage_id('$1');"
}

check_num "no row names a missing relation before the damage" "$(found)" "0"

q "UPDATE pgcolumnar.storage SET relation_oid = $gone_a
	WHERE storage_id = pgcolumnar.get_storage_id('srr_t');
   UPDATE pgcolumnar.storage SET relation_oid = $gone_b
	WHERE storage_id = pgcolumnar.get_storage_id('srr_mv');" >/dev/null

check_num "the notes' query finds both rows alpha4 left wrong" "$(found)" "2"
check_text "premise: the damaged table still reads every row" \
	"$(q "SELECT count(*) || '/' || sum(b) FROM srr_t;")" "1000/500500"

# Step 2 of the notes: refresh each columnar materialized view.
q "REFRESH MATERIALIZED VIEW srr_mv;" >/dev/null
check_num "REFRESH MATERIALIZED VIEW repairs the view's row" "$(names_itself srr_mv)" "1"

# Step 3: convert a column to its own type with USING, which forces the rewrite.
q "ALTER TABLE srr_t ALTER COLUMN b TYPE int USING b;" >/dev/null
check_num "a same-type ALTER COLUMN ... USING repairs the table's row" \
	"$(names_itself srr_t)" "1"

# Step 4: the notes' query again, and the data unchanged.
check_num "after both repairs the notes' query finds nothing" "$(found)" "0"
check_text "and the repaired table holds the same rows" \
	"$(q "SELECT count(*) || '/' || sum(b) FROM srr_t;")" "1000/500500"
check_text "and so does the refreshed view" \
	"$(q "SELECT count(*) || '/' || sum(id) FROM srr_mv;")" "1000/500500"

pgc_summary
