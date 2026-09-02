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

# ---- more than one row group, and no ANALYZE between delete and measurement --
#
# Two holes in the arms above, both of which let a broken estimate look right.
#
# 1. The fixture is 100,000 rows against a default stripe_row_limit of 150,000,
#    so it holds ONE row group. Anything the estimate does per group is
#    exercised for a single iteration, and a bug that handled only the first
#    group would pass.
#
# 2. `ANALYZE` runs immediately before every measurement, so pg_class.reltuples
#    is freshly correct at each one. That is not what the callback reads, but it
#    means a fixture cannot distinguish "the estimate subtracts deletes" from
#    "something else supplied a correct number".
#
# This section fixes both: a smaller stripe_row_limit to get several groups
# cheaply, deletes confined to the LAST group, and the measurement taken with a
# DELIBERATELY STALE reltuples -- ANALYZE runs before the delete and not after.

psql_run "CREATE TABLE ed_m (id int, v text) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('ed_m', stripe_row_limit => 20000);"
psql_run "INSERT INTO ed_m SELECT g, 'x' || g FROM generate_series(1, 100000) g;"
psql_run "ANALYZE ed_m;"

ed_m_groups="$(q "SELECT count(*) FROM pgcolumnar.row_group rg
                  JOIN pgcolumnar.storage s ON s.storage_id = rg.storage_id
                  WHERE s.relation_oid = 'ed_m'::regclass")"
check "premise: the fixture spans several row groups, not one" \
	"$(awk -v v="$ed_m_groups" 'BEGIN { print (v+0 >= 3) ? "several" : "only " v }')" "several"

ed_m_stale="$(q "SELECT reltuples::bigint FROM pg_class WHERE relname = 'ed_m'")"
check "premise: reltuples is 100000 before the delete" \
	"$(awk -v v="$ed_m_stale" 'BEGIN { print (v+0 >= 90000) ? "full" : "low " v }')" "full"

# Delete only from the HIGHEST row numbers, so a bug that subtracts deletes for
# the first group alone leaves the estimate untouched and this arm reddens.
psql_run "DELETE FROM ed_m WHERE id > 60000;"

# NO ANALYZE here, on purpose. reltuples still says 100000. If the estimate
# reported reltuples, or summed row_count without subtracting, it would say
# ~100000. Only reading the delete vector gets near 60000.
ed_m_after="$(plan_rows 'SELECT * FROM ed_m')"
echo "-- ed_m: groups=$ed_m_groups stale reltuples=$ed_m_stale estimate after delete=$ed_m_after"
check "the estimate follows the deletes with a stale reltuples and several groups" \
	"$(awk -v v="$ed_m_after" 'BEGIN {
	       print (v+0 >= 55000 && v+0 <= 65000) ? "live" : "wrong " v }')" "live"

check "control: and the live count really is 60000" \
	"$(q 'SELECT count(*) FROM ed_m')" "60000"

# The other direction: with no deletes at all the estimate must be the full
# table, or "follows the deletes" is satisfied by an estimate that is simply low.
psql_run "CREATE TABLE ed_n (id int, v text) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('ed_n', stripe_row_limit => 20000);"
psql_run "INSERT INTO ed_n SELECT g, 'x' || g FROM generate_series(1, 100000) g;"
ed_n_est="$(plan_rows 'SELECT * FROM ed_n')"
check "control: an undeleted table of the same shape estimates its full size" \
	"$(awk -v v="$ed_n_est" 'BEGIN { print (v+0 >= 90000) ? "full" : "low " v }')" "full"

pgc_summary
