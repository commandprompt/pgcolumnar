#!/usr/bin/env bash
#
# ALTER TABLE ... ALTER COLUMN TYPE must leave pgcolumnar.storage.relation_oid
# pointing at the table that survived the rewrite.
#
# A type change rewrites through a transient relation. The storage row is
# written with that transient's OID. The swap keeps the user's OID and drops
# the transient, so a lookup by the live table finds nothing. The rows are
# still readable. The written-geometry lookup is the one reader of this
# column, and it fails closed by returning the session default.
#
# Usage:  test/rewrite_storage_oid.sh [PG_CONFIG]

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/lib/postgresql/18/bin/pg_config}"

q "CREATE EXTENSION IF NOT EXISTS pgcolumnar;" >/dev/null

q "CREATE TABLE rew_keep (id int) USING pgcolumnar;
   INSERT INTO rew_keep SELECT g FROM generate_series(1,40) g;
   CREATE TABLE rew_oid (id int, note text) USING pgcolumnar;
   INSERT INTO rew_oid SELECT g, 'n' || g FROM generate_series(1,900) g;" >/dev/null

live_rows() {
	q "SELECT (relation_oid = 'rew_oid'::regclass)::int
	   FROM pgcolumnar.storage
	   WHERE storage_id = pgcolumnar.get_storage_id('rew_oid');"
}

check_num "premise: the storage row points at the live table before the rewrite" \
	"$(live_rows)" "1"

check_num "premise: the table holds every inserted row" \
	"$(q "SELECT count(*) FROM rew_oid;")" "900"

q "ALTER TABLE rew_oid ALTER COLUMN id TYPE bigint;" >/dev/null

check_num "premise: the rewritten table still holds every row" \
	"$(q "SELECT count(*) FROM rew_oid;")" "900"

check_num "premise: a table that was not rewritten still points at itself" \
	"$(q "SELECT (relation_oid = 'rew_keep'::regclass)::int
	      FROM pgcolumnar.storage
	      WHERE storage_id = pgcolumnar.get_storage_id('rew_keep');")" "1"

echo "-- live=$(live_rows)"
check_num "a type rewrite leaves the storage row pointing at the live table" \
	"$(live_rows)" "1"

# --- and CREATE MATERIALIZED VIEW ... WITH DATA (#1275) ----------------------
#
# The same defect on a different statement. `CREATE MATERIALIZED VIEW ... AS`
# with data builds a transient, fills it, and swaps -- exactly like the type
# change above -- so the storage row is written with the transient's OID and the
# swap leaves it naming a relation that no longer exists.
#
# `REFRESH MATERIALIZED VIEW` already repairs it, because RefreshMatViewStmt is
# one of the node types the repair runs for. A CreateTableAsStmt was not.
#
# THE `CREATE TABLE ... AS` ARM IS THE CONTROL THAT NARROWS THE CLAIM. It is the
# same parse node and it does NOT have the defect -- measured before the fix,
# rows_by_relation_oid = 1 -- because it fills the relation it created rather
# than swapping a transient in. Without it the fix would reasonably have been
# written for CreateTableAsStmt as a whole, which is broader than anything
# measured asked for.
q "CREATE MATERIALIZED VIEW rew_mv USING pgcolumnar AS
     SELECT id, note FROM rew_oid;" >/dev/null
q "CREATE TABLE rew_cta USING pgcolumnar AS
     SELECT id, note FROM rew_oid;" >/dev/null

points_at() {	# points_at RELNAME -> 1 when its storage row names it
	q "SELECT (relation_oid = '$1'::regclass)::int
	   FROM pgcolumnar.storage
	   WHERE storage_id = pgcolumnar.get_storage_id('$1');"
}

# A ROW COUNT FIRST, so the arms below are about a relation that was actually
# populated. A matview created WITH NO DATA writes no storage row at all, and
# every arm here would then be asking about nothing.
check_num "premise: the matview holds the rows it was created with" \
	"$(q "SELECT count(*) FROM rew_mv;")" "900"
check_num "premise: the CREATE TABLE AS table holds them too" \
	"$(q "SELECT count(*) FROM rew_cta;")" "900"

echo "-- mv=$(points_at rew_mv) cta=$(points_at rew_cta)"

# THE CONTROL, AND IT MUST PASS BEFORE THE FIX AS WELL AS AFTER. If this ever
# reads 0 the claim below is no longer about matviews specifically, and the
# remedy is a different one.
check_num "premise: CREATE TABLE ... AS leaves its storage row pointing at itself" \
	"$(points_at rew_cta)" "1"

check_num "a matview created WITH DATA leaves its storage row pointing at itself" \
	"$(points_at rew_mv)" "1"

# AND REFRESH MUST STILL WORK. It repaired this before the fix, through a
# different node type, so an arm here is what says the fix did not displace the
# path that already worked.
q "REFRESH MATERIALIZED VIEW rew_mv;" >/dev/null
check_num "premise: the refreshed matview still holds every row" \
	"$(q "SELECT count(*) FROM rew_mv;")" "900"
check_num "and REFRESH still leaves it pointing at itself" \
	"$(points_at rew_mv)" "1"

pgc_summary
