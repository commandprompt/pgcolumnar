#!/usr/bin/env bash
#
# SQL TRUNCATE must not leave the old storage's catalog rows behind.
#
# DROP deletes metadata for the relation's current storage id. TRUNCATE
# installs a new relfilenode and a new storage id, and used to leave the old
# catalog rows keyed by the retired id. DROP after that only cleaned the new
# id, so each truncate-and-reload cycle leaked one storage's worth of
# row_group, column_chunk, zone_map and bloom rows.
#
# Measured before the fix, one insert-then-TRUNCATE left the catalog at the
# post-insert counts instead of the empty baseline; a second insert doubled
# them. Counting rather than sampling is deliberate, as in drop_cleanup.sh:
# a leak of one row per truncate is exactly the size of this defect.
#
# Usage:  test/truncate_cleanup.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

snapshot() {
	q "SELECT (SELECT count(*) FROM pgcolumnar.storage) || '/' ||
		(SELECT count(*) FROM pgcolumnar.projection) || '/' ||
		(SELECT count(*) FROM pgcolumnar.row_group) || '/' ||
		(SELECT count(*) FROM pgcolumnar.column_chunk) || '/' ||
		(SELECT count(*) FROM pgcolumnar.zone_map) || '/' ||
		(SELECT count(*) FROM pgcolumnar.bloom) || '/' ||
		(SELECT count(*) FROM pgcolumnar.delete_vector);" | tail -1
}

base="$(snapshot)"

psql_run "CREATE TABLE tcl_plain (id int, v text) USING pgcolumnar;
	INSERT INTO tcl_plain SELECT g, 'x' || g FROM generate_series(1, 5000) g;" >/dev/null
grew="$(snapshot)"
check "insert did add metadata" \
	"$(awk -v a="$base" -v b="$grew" 'BEGIN { print (a == b) ? "no" : "yes" }')" "yes"

psql_run "TRUNCATE tcl_plain;" >/dev/null
check "TRUNCATE returns the catalog to the empty baseline" "$(snapshot)" "$base"
check_num "TRUNCATE leaves no rows" "$(q 'SELECT count(*) FROM tcl_plain')" "0"

psql_run "INSERT INTO tcl_plain SELECT g, 'x' || g FROM generate_series(1, 5000) g;" >/dev/null
psql_run "TRUNCATE tcl_plain;" >/dev/null
psql_run "INSERT INTO tcl_plain SELECT g, 'x' || g FROM generate_series(1, 5000) g;" >/dev/null
psql_run "TRUNCATE tcl_plain;" >/dev/null
check "three truncate-and-reload cycles still sit at the baseline" "$(snapshot)" "$base"

psql_run "DROP TABLE tcl_plain;" >/dev/null
check "DROP after TRUNCATE leaves nothing" "$(snapshot)" "$base"

# a projection has its own storage id; TRUNCATE used to leak that too
base="$(snapshot)"
psql_run "CREATE TABLE tcl_proj (id int, a int, b text) USING pgcolumnar;" >/dev/null
psql_run "SELECT pgcolumnar.add_projection('tcl_proj','tcl_p',ARRAY['a','b'],ARRAY['a']);" >/dev/null
psql_run "INSERT INTO tcl_proj SELECT g, g % 50, 'b' || g FROM generate_series(1, 5000) g;" >/dev/null
grew="$(snapshot)"
check "the projection did add metadata" \
	"$(awk -v a="$base" -v b="$grew" 'BEGIN { print (a == b) ? "no" : "yes" }')" "yes"

psql_run "TRUNCATE tcl_proj;" >/dev/null
check "TRUNCATE takes the projection's retired storage with it" "$(snapshot)" "$base"

psql_run "DROP TABLE tcl_proj;" >/dev/null
check "DROP of the truncated projected table leaves nothing" "$(snapshot)" "$base"

# repetition, which is where an unbounded leak shows
base="$(snapshot)"
for i in $(seq 1 10); do
	psql_run "CREATE TABLE tcl_rep (id int, v text) USING pgcolumnar;
		INSERT INTO tcl_rep SELECT g, 'x' || g FROM generate_series(1, 2000) g;
		TRUNCATE tcl_rep;
		DROP TABLE tcl_rep;" >/dev/null
done
check "ten create-insert-truncate-drop cycles leave nothing" "$(snapshot)" "$base"

pgc_summary
