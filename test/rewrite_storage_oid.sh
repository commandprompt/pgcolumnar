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

pgc_summary
