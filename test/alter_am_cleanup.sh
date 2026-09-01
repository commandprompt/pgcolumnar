#!/usr/bin/env bash
#
# pgColumnar: DROP TABLE after SET ACCESS METHOD heap must still take the
# relid-keyed catalogs with it.
#
# Storage-id catalogs (row_group, zone_map, ...) are already gone after the
# rewrite onto heap storage. pgcolumnar.options and projection_declaration are
# keyed by relid. The drop hook used to look at the access method and return
# before touching them, so converting away from columnar and then dropping left
# those rows behind. config_dump emits a bare oid for a relid that no longer
# resolves, and rebuild_projections() aborts on an orphan declaration -- the
# same blast radius #304 closed for a plain DROP of a still-columnar table.
#
# Usage:  test/alter_am_cleanup.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# catalogs keyed by storage id, then the two keyed by relid
snapshot() {
	q "SELECT (SELECT count(*) FROM pgcolumnar.storage) || '/' ||
		(SELECT count(*) FROM pgcolumnar.projection) || '/' ||
		(SELECT count(*) FROM pgcolumnar.row_group) || '/' ||
		(SELECT count(*) FROM pgcolumnar.options) || '/' ||
		(SELECT count(*) FROM pgcolumnar.projection_declaration);" | tail -1
}

base="$(snapshot)"

psql_run "CREATE TABLE aac_t (id int, a int, b text) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('aac_t', stripe_row_limit => 5000,
                                        sort_by => ARRAY['id']);"
psql_run "SELECT pgcolumnar.add_projection('aac_t','p1',ARRAY['a','b'],ARRAY['a']);"
psql_run "INSERT INTO aac_t SELECT g, g, 'x' FROM generate_series(1,200) g;"

grew="$(snapshot)"
check "premise: options and a projection declaration were recorded" \
	"$(awk -v a="$base" -v b="$grew" 'BEGIN { print (a == b) ? "no" : "yes" }')" "yes"

psql_run "ALTER TABLE aac_t SET ACCESS METHOD heap;"
check "the table is heap after the conversion" \
	"$(q "SELECT am.amname FROM pg_class c JOIN pg_am am ON am.oid = c.relam
	      WHERE c.relname = 'aac_t'")" "heap"

# Storage-id catalogs are already gone; the relid-keyed rows are what DROP
# used to miss. Do not require them to vanish at convert time: a round-trip
# back to columnar should still see the declared options.
check "converting to heap does not drop the declared options" \
	"$(q "SELECT stripe_row_limit FROM pgcolumnar.options
	      WHERE regclass = 'aac_t'::regclass")" "5000"

psql_run "DROP TABLE aac_t;"
check "DROP after SET ACCESS METHOD heap leaves no options or declarations" \
	"$(snapshot)" "$base"

# Round-trip: convert away and back, then DROP a still-columnar table.
psql_run "CREATE TABLE aac_rt (id int) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('aac_rt', stripe_row_limit => 8000);"
psql_run "ALTER TABLE aac_rt SET ACCESS METHOD heap;"
psql_run "ALTER TABLE aac_rt SET ACCESS METHOD pgcolumnar;"
check "options survive a heap round-trip" \
	"$(q "SELECT stripe_row_limit FROM pgcolumnar.options
	      WHERE regclass = 'aac_rt'::regclass")" "8000"
psql_run "DROP TABLE aac_rt;"
check "a round-tripped table still cleans up on DROP" "$(snapshot)" "$base"

# Control: a never-columnar heap must not be charged for this either.
psql_run "CREATE TABLE aac_h (id int);"
psql_run "DROP TABLE aac_h;"
check "dropping a heap table that was never columnar leaves the catalogs alone" \
	"$(snapshot)" "$base"

pgc_summary
