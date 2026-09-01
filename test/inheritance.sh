#!/usr/bin/env bash
#
# pgColumnar: a legacy INHERITS parent must plan as Append of its children, not
# as a custom scan (or vectorized aggregate) of the parent's storage alone.
#
# PgColumnarSetRelPathlist and the ungrouped vector-aggregate path both run on
# the inheritance appendrel (RELKIND_RELATION with rte->inh set). A scan added
# there reads only the parent's own file. Measured: parent 1 row, child 5000
# rows, SELECT count(*) FROM parent returned 1 where the heap mirror returned
# 5001, from a plan with no Append. Grouped vector aggregation already refused
# this shape; the other two hooks did not.
#
# Declarative partitions are a different RTE (RELKIND_PARTITIONED_TABLE) and
# already go through Append of OTHER_MEMBER_REL children. That path stays.
#
# Usage:  test/inheritance.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

plan() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -Atq -c "$1"
}

psql_run "CREATE TABLE inh_h (id int, v text);"
psql_run "CREATE TABLE inh_hc () INHERITS (inh_h);"
psql_run "INSERT INTO inh_h VALUES (1, 'parent');"
psql_run "INSERT INTO inh_hc VALUES (2, 'child');"

psql_run "CREATE TABLE inh_p (id int, v text) USING pgcolumnar;"
psql_run "CREATE TABLE inh_c () INHERITS (inh_p) USING pgcolumnar;"
psql_run "INSERT INTO inh_p VALUES (1, 'parent');"
psql_run "INSERT INTO inh_c VALUES (2, 'child');"

check "INHERITS parent includes the child (heap oracle)" \
	"$(q "SELECT count(*) FROM inh_h")" "2"
check "INHERITS parent includes the child (columnar)" \
	"$(q "SELECT count(*) FROM inh_p")" "2"
check "ONLY parent is still just the parent" \
	"$(q "SELECT count(*) FROM ONLY inh_p")" "1"
check "the child is visible through the parent" \
	"$(q "SELECT v FROM inh_p WHERE id = 2")" "child"
check "row identities match the heap" \
	"$(pgc_set_hash 'SELECT id, v FROM inh_p')" \
	"$(pgc_set_hash 'SELECT id, v FROM inh_h')"

star_plan="$(plan "EXPLAIN (COSTS OFF) SELECT * FROM inh_p;")"
check "SELECT * from an INHERITS parent plans an Append" \
	"$(printf '%s' "$star_plan" | grep -c Append)" "1"

count_plan="$(plan "EXPLAIN (COSTS OFF) SELECT count(*) FROM inh_p;")"
check "count(*) from an INHERITS parent plans an Append" \
	"$(printf '%s' "$count_plan" | grep -c Append)" "1"

# A cheap parent and a large child is the shape that made the custom scan win:
# the appendrel is costed from the parent's empty-looking storage.
psql_run "INSERT INTO inh_c SELECT g, 'c'||g FROM generate_series(10,5000) g;"
psql_run "INSERT INTO inh_hc SELECT g, 'c'||g FROM generate_series(10,5000) g;"
psql_run "ANALYZE inh_p; ANALYZE inh_c; ANALYZE inh_h; ANALYZE inh_hc;"

check "a large child is still visible through the parent" \
	"$(q "SELECT count(*) FROM inh_p")" \
	"$(q "SELECT count(*) FROM inh_h")"
check "filtered rows match the heap" \
	"$(pgc_set_hash 'SELECT id FROM inh_p WHERE id > 100')" \
	"$(pgc_set_hash 'SELECT id FROM inh_h WHERE id > 100')"

vec_count="$(q "SET pgcolumnar.enable_ungrouped_vector_agg = on;
                SELECT count(*) FROM inh_p;")"
check "ungrouped vector aggregation also sees the child rows" \
	"$(printf '%s' "$vec_count" | tail -1)" \
	"$(q "SELECT count(*) FROM inh_h")"

# Declarative partitions were the #436 case: the custom scan MUST still fire on
# the child, or this refusal of rte->inh would put them back on a seqscan.
psql_run "CREATE TABLE inh_prt (id int, v text) PARTITION BY RANGE (id);"
psql_run "CREATE TABLE inh_prt1 PARTITION OF inh_prt FOR VALUES FROM (0) TO (100000)
          USING pgcolumnar;"
psql_run "INSERT INTO inh_prt SELECT g, 'p'||g FROM generate_series(1,2000) g;"
check "a partitioned parent still returns every row" \
	"$(q "SELECT count(*) FROM inh_prt")" "2000"
prt_plan="$(plan "EXPLAIN (COSTS OFF) SELECT * FROM inh_prt;")"
check "and the partition is still a PgColumnarScan (#436)" \
	"$(printf '%s' "$prt_plan" | grep -c 'Custom Scan (PgColumnarScan)')" "1"

pgc_summary
