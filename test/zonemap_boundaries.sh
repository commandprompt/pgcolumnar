#!/usr/bin/env bash
#
# Exact zone-map comparison boundaries are intentional coverage (#831).
#
# Usage: test/zonemap_boundaries.sh [PG_CONFIG]

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

psql_run "CREATE TABLE zb_h(id int, v int);
	CREATE TABLE zb_c(id int, v int) USING pgcolumnar;
	SELECT pgcolumnar.set_options('zb_c', stripe_row_limit => 1000);
	INSERT INTO zb_h SELECT g,g FROM generate_series(1,2000) g;
	INSERT INTO zb_c SELECT * FROM zb_h;
	ANALYZE zb_h; ANALYZE zb_c;" >/dev/null

check "premise: the boundary fixture has two row groups" \
	"$(q "SELECT count(*) FROM pgcolumnar.row_group
	      WHERE storage_id=pgcolumnar.get_storage_id('zb_c');")" "2"

groups_removed() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" \
		-U postgres -d "$PGC_DB" -Atq \
		-c "SET pgcolumnar.enable_bloom_filter=off;
		    SET pgcolumnar.enable_vectorization=off;
		    EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF)
		    SELECT id FROM zb_c WHERE $1;" 2>&1 |
		sed -n 's/.*Columnar Chunk Groups Removed by Filter: \([0-9]*\).*/\1/p' |
		head -1
}

check "< excludes the group whose minimum equals the constant" \
	"$(groups_removed 'v < 1001')" "1"
check_text "<= keeps the row at a row-group minimum" \
	"$(pgc_set_hash "SELECT id FROM zb_c WHERE v <= 1001")" \
	"$(pgc_set_hash "SELECT id FROM zb_h WHERE v <= 1001")"
# The two "removes no group" checks are arms, not premises (#1339, #1236): a zone
# check that skipped a group whose bound EQUALS the constant reddens both.
check "<= at the second-group minimum removes no group" \
	"$(groups_removed 'v <= 1001')" "0"

check_text ">= keeps the row at a row-group maximum" \
	"$(pgc_set_hash "SELECT id FROM zb_c WHERE v >= 1000")" \
	"$(pgc_set_hash "SELECT id FROM zb_h WHERE v >= 1000")"
check ">= at the first-group maximum removes no group" \
	"$(groups_removed 'v >= 1000')" "0"

# These mutations remain row-correct, so only work-done counters can see them.
check "> excludes the group whose maximum equals the constant" \
	"$(groups_removed 'v > 1000')" "1"
check "= excludes the group lying wholly below the constant" \
	"$(groups_removed 'v = 1001')" "1"

check "backend alive" "$(q 'SELECT 1')" "1"
pgc_summary
