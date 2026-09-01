#!/usr/bin/env bash
#
# pgColumnar: UPDATE must fan the new row number out to covering projections.
#
# A projection stores the base row number and filters with the base delete
# vector, so DELETE needs no rewrite of the projection. UPDATE is delete-old
# plus insert-new. The insert half used to skip fan-out, so the projection kept
# only the deleted number. read_projection then returned no rows, and a planner
# covering-projection scan (SELECT of projected columns with a sort-key qual)
# answered as if the updated rows had been deleted.
#
# Usage:  test/projection_update.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

psql_run "CREATE TABLE pu (a int, b text, c int) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.add_projection('pu', 'pc', ARRAY['a','c'], ARRAY['c']);"
psql_run "INSERT INTO pu SELECT g, 'r'||g, (g*7)%1000 FROM generate_series(1,20000) g;"
psql_run "CREATE TABLE pu_h (a int, b text, c int) USING heap;"
psql_run "INSERT INTO pu_h SELECT g, 'r'||g, (g*7)%1000 FROM generate_series(1,20000) g;"

check "premise: planner uses the covering projection" \
	"$(q "EXPLAIN (COSTS OFF) SELECT a, c FROM pu WHERE c BETWEEN 100 AND 200;" | grep -c 'Columnar Projection: pc')" "1"
check "premise: projection matches the heap before any update" \
	"$(pgc_set_hash "SELECT a, c FROM pu WHERE c BETWEEN 100 AND 200")" \
	"$(pgc_set_hash "SELECT a, c FROM pu_h WHERE c BETWEEN 100 AND 200")"

psql_run "UPDATE pu   SET a = a + 1 WHERE a % 10 = 0;"
psql_run "UPDATE pu_h SET a = a + 1 WHERE a % 10 = 0;"

check "covering projection scan matches heap after updating a projected column" \
	"$(pgc_set_hash "SELECT a, c FROM pu WHERE c BETWEEN 100 AND 200")" \
	"$(pgc_set_hash "SELECT a, c FROM pu_h WHERE c BETWEEN 100 AND 200")"
check "read_projection matches the live base after that update" \
	"$(pgc_set_hash "SELECT pgcolumnar.read_projection('pu','pc')")" \
	"$(pgc_set_hash "SELECT a::text || '|' || c::text FROM pu")"
check "full-table projection scan still matches heap" \
	"$(pgc_set_hash "SELECT a, c FROM pu")" \
	"$(pgc_set_hash "SELECT a, c FROM pu_h")"

# A column the projection does not store still allocates a new base row number.
psql_run "UPDATE pu   SET b = 'y' WHERE a % 7 = 0;"
psql_run "UPDATE pu_h SET b = 'y' WHERE a % 7 = 0;"

check "covering projection scan matches heap after updating a non-projected column" \
	"$(pgc_set_hash "SELECT a, c FROM pu WHERE c BETWEEN 100 AND 200")" \
	"$(pgc_set_hash "SELECT a, c FROM pu_h WHERE c BETWEEN 100 AND 200")"
check "read_projection still matches the live base" \
	"$(pgc_set_hash "SELECT pgcolumnar.read_projection('pu','pc')")" \
	"$(pgc_set_hash "SELECT a::text || '|' || c::text FROM pu")"
check "reconstruct via the projection still rebuilds every live row" \
	"$(pgc_set_hash "SELECT pgcolumnar.reconstruct_via_projection('pu','pc')")" \
	"$(pgc_set_hash "SELECT a::text || '|' || b || '|' || c::text FROM pu")"

pgc_summary
