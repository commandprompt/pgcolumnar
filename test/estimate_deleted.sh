#!/usr/bin/env bash
#
# The planner must not count rows that delete_vector has marked.
#
# pgcolumnar_relation_estimate_size is the only row count the planner sees:
# core delegates, so pg_class.reltuples is ignored. The callback summed
# row_group.row_count and never subtracted delete_vector, so after
#
#     INSERT 100000 rows; DELETE 99000 of them; ANALYZE
#
# reltuples was 1000 and the true count was 1000, but EXPLAIN still said
# rows=100000. That is the same class of defect as #507: the catalog looks
# right and the plan stays wrong.
#
# heap is the control for the shape (ANALYZE updates its estimate). Columnar
# is required to report the live count from metadata, not a sampled one, so
# the figure must be exact.
#
# Usage:  test/estimate_deleted.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

plan_rows() {
	q "EXPLAIN (COSTS ON) $1" | grep -oiE 'rows=[0-9]+' | head -1 | cut -d= -f2
}

psql_run "CREATE TABLE ed_c (id int, v text) USING pgcolumnar;"
psql_run "CREATE TABLE ed_h (id int, v text);"
psql_run "INSERT INTO ed_c SELECT g, 'x' || g FROM generate_series(1, 100000) g;"
psql_run "INSERT INTO ed_h SELECT g, 'x' || g FROM generate_series(1, 100000) g;"
psql_run "ANALYZE ed_c; ANALYZE ed_h;"

check_num "premise: columnar holds 100000 rows before delete" \
	"$(q 'SELECT count(*) FROM ed_c')" "100000"
before="$(plan_rows 'SELECT * FROM ed_c')"
echo "-- rows before delete: $before"
check "premise: the estimate before delete is the full table" \
	"$(awk -v v="$before" 'BEGIN { print (v+0 >= 90000) ? "full" : "low " v }')" "full"

psql_run "DELETE FROM ed_c WHERE id > 1000;"
psql_run "DELETE FROM ed_h WHERE id > 1000;"
psql_run "ANALYZE ed_c; ANALYZE ed_h;"

check_num "premise: 1000 live rows remain" \
	"$(q 'SELECT count(*) FROM ed_c')" "1000"
relt="$(q "SELECT reltuples::bigint FROM pg_class WHERE relname='ed_c'")"
echo "-- reltuples after ANALYZE: $relt"
check "premise: ANALYZE recorded the live count in reltuples" \
	"$(awk -v v="$relt" 'BEGIN { d = (v > 1000 ? v-1000 : 1000-v); print (d <= 50) ? "close" : "off " v }')" "close"

got="$(plan_rows 'SELECT * FROM ed_c')"
echo "-- columnar EXPLAIN rows= after delete: $got"
check "the planner estimate after delete is the live count, not the physical occupancy" \
	"$(awk -v v="$got" 'BEGIN { print (v+0 == 1000) ? "live" : "stale " v }')" "live"

hgot="$(plan_rows 'SELECT * FROM ed_h')"
echo "-- heap EXPLAIN rows= after delete: $hgot"
check "control: heap is also near the live count, so the probe reads EXPLAIN" \
	"$(awk -v v="$hgot" 'BEGIN { d = (v > 1000 ? v-1000 : 1000-v); print (d <= 50) ? "close" : "off " v }')" "close"

pgc_summary
