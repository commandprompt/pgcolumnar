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

# ---- a PARTITIONED parent carrying relam=pgcolumnar has no storage (#1259) ----
#
# From PostgreSQL 17 a partitioned table may carry relam = pgcolumnar as the
# default for its future partitions. It has no storage of its own -- relfilenode
# is 0 -- and PgColumnarIsColumnarRelation used to answer "yes" from relam
# alone. Every caller that then read storage handed smgropen a zero
# relfilenumber, which on an ASSERT build aborts the backend and restarts the
# cluster. Ten entry points were measured reaching it.
#
# THE ARMS BELOW ASSERT AN ERROR, NOT A CRASH. On a production build the old
# behaviour was not an error at all -- get_storage_id returned NULL and stats
# and sort_status built on it -- so "returns something" is not the property.
# The property is that the call is REFUSED and the cluster is still up
# afterwards, which is why each arm is followed by a liveness check rather than
# trusting the return alone.
# PG17 IS THE FLOOR AND THE ARMS BELOW ARE VACUOUS WITHOUT IT. PostgreSQL 15 and
# 16 refuse `PARTITION BY ... USING pgcolumnar` outright --
# "specifying a table access method is not supported on a partitioned table" --
# so the fixture never exists, and the two "is refused" arms then PASS for the
# wrong reason: the call is refused because the relation is not there. That is a
# vacuous green, which is worse than a red, and the premises below are what
# caught it. So they are skipped by name rather than left to pass.
if ! pgc_is_number "${PGC_MAJOR:-}"; then
	pgc_fail "premise: the server major is known, so the PG17 floor can be applied" \
		"got [${PGC_MAJOR:-<none>}]"
fi
if [ "$PGC_MAJOR" -lt 17 ]; then
	for _inh_arm in \
		"premise: the parent carries the columnar access method" \
		"premise: and it is partitioned" \
		"premise: so it has no storage of its own" \
		"premise: while its leaf is an ordinary relation" \
		"get_storage_id on the parent is refused rather than reading storage" \
		"and the cluster is still up after it" \
		"add_projection on the parent is refused rather than aborting the backend" \
		"and the cluster is still up after that too" \
		"control: get_storage_id on the LEAF still returns an id" \
		"control: add_projection on the LEAF still works"; do
		check_skip "$_inh_arm" \
			"SKIP  $_inh_arm (a partitioned table cannot carry an access method before PG17; this server is $PGC_MAJOR)" \
			"partitioned relam needs PG17+"
	done
else
psql_run "DROP TABLE IF EXISTS inh_pp CASCADE;
          CREATE TABLE inh_pp (a int, b text) PARTITION BY RANGE (a) USING pgcolumnar;
          CREATE TABLE inh_pp1 PARTITION OF inh_pp FOR VALUES FROM (0) TO (100);
          INSERT INTO inh_pp SELECT g, 'x'||g FROM generate_series(0,99) g;"
check "premise: the parent carries the columnar access method" \
	"$(q "SELECT am.amname FROM pg_class c JOIN pg_am am ON am.oid = c.relam
	      WHERE c.relname = 'inh_pp'")" "pgcolumnar"
check "premise: and it is partitioned" \
	"$(q "SELECT relkind::text FROM pg_class WHERE relname = 'inh_pp'")" "p"
check "premise: so it has no storage of its own" \
	"$(q "SELECT relfilenode FROM pg_class WHERE relname = 'inh_pp'")" "0"
check "premise: while its leaf is an ordinary relation" \
	"$(q "SELECT relkind::text FROM pg_class WHERE relname = 'inh_pp1'")" "r"

# EACH CALL IS FOLLOWED BY A LIVENESS CHECK taken from a new backend, because a
# crashed cluster restarts and a later query succeeds -- a delayed check reads
# "up" for a backend that aborted.
inh_up() { psql_run "SELECT 1;" >/dev/null 2>&1 && echo up || echo DOWN; }

check "get_storage_id on the parent is refused rather than reading storage" \
	"$(psql_run "SELECT pgcolumnar.get_storage_id('inh_pp'::regclass);" >/dev/null 2>&1 \
		&& echo accepted || echo refused)" "refused"
check "and the cluster is still up after it" "$(inh_up)" "up"

check "add_projection on the parent is refused rather than aborting the backend" \
	"$(psql_run "SELECT pgcolumnar.add_projection('inh_pp'::regclass, 'p1', ARRAY['a']);" \
		>/dev/null 2>&1 && echo accepted || echo refused)" "refused"
check "and the cluster is still up after that too" "$(inh_up)" "up"

# THE CONTROL THAT THE CALLS WORK AT ALL. Without it "refused" is satisfied by a
# function that refuses everything, and the arms above would pass on a build
# where nothing works.
check "control: get_storage_id on the LEAF still returns an id" \
	"$(psql_run "SELECT pgcolumnar.get_storage_id('inh_pp1'::regclass);" >/dev/null 2>&1 \
		&& echo accepted || echo refused)" "accepted"
check "control: add_projection on the LEAF still works" \
	"$(psql_run "SELECT pgcolumnar.add_projection('inh_pp1'::regclass, 'p1', ARRAY['a']);" \
		>/dev/null 2>&1 && echo accepted || echo refused)" "accepted"
fi

# ---- AND A MATERIALIZED VIEW IS STILL COLUMNAR (#1259) ----
#
# THIS IS THE ARM THAT DISCRIMINATES AGAINST THE OBVIOUS WRONG FIX. Narrowing
# the predicate to `relkind == RELKIND_RELATION` also excludes RELKIND_MATVIEW,
# and a materialized view CAN be columnar -- so that fix would stop recognising
# one at all 31 call sites, silently and on every path. RELKIND_HAS_STORAGE
# admits it and excludes the partitioned parent, which is the property the
# callers actually need.
psql_run "DROP MATERIALIZED VIEW IF EXISTS inh_mv;
          DROP TABLE IF EXISTS inh_src;
          CREATE TABLE inh_src (a int);
          INSERT INTO inh_src SELECT generate_series(1,10);
          CREATE MATERIALIZED VIEW inh_mv USING pgcolumnar AS SELECT * FROM inh_src;"
check "premise: a materialized view is relkind m" \
	"$(q "SELECT relkind::text FROM pg_class WHERE relname = 'inh_mv'")" "m"
check "premise: and it carries the columnar access method" \
	"$(q "SELECT am.amname FROM pg_class c JOIN pg_am am ON am.oid = c.relam
	      WHERE c.relname = 'inh_mv'")" "pgcolumnar"
check "a columnar materialized view is still recognised as columnar" \
	"$(psql_run "SELECT pgcolumnar.get_storage_id('inh_mv'::regclass);" >/dev/null 2>&1 \
		&& echo accepted || echo refused)" "accepted"
check "and it still returns its rows" "$(q "SELECT count(*) FROM inh_mv")" "10"

pgc_summary
