#!/usr/bin/env bash
#
# pgColumnar: DROP TABLE must take the whole table's metadata with it.
#
# The drop hook deleted metadata for the relation's own storage id. A projection
# keeps its own storage, so its row groups, column chunks, zone maps, bloom
# filters and projection rows were left behind, describing a table that no
# longer exists and reachable from nothing.
#
# Measured before the fix, ten create-and-drop cycles of a table with one
# projection: pgcolumnar.projection went from 36 rows to 56 and stayed there.
# In a workload that creates and drops such tables the catalog grows without
# bound, and every row of it is about a relation that is gone.
#
# The physical file goes either way -- the projection's data lives in the
# relation's own storage, which DROP removes -- so this is catalog debris rather
# than leaked disk. That is the reason it went unnoticed.
#
# Counting rather than sampling is deliberate. A check that a dropped table's
# rows are "mostly gone" would pass on a leak of one row per drop, which is
# exactly the size of this one.
#
# WHICH CALL SITE THESE ARMS HOLD, for whoever is picking a gate list after
# touching the object-access hook. Remove the DROP hook's call to
# pgcolumnar_delete_storage_tree() -- src/columnar_tableam.c, in the
# OAT_DROP arm of pgcolumnar_object_access(), :2615 as of 815dd0a -- and five
# of these eight arms redden, measured on pg18a:
#
#   a plain table leaves nothing behind
#   dropping it takes the projection's metadata too
#   two projections, one dropped by hand, leave nothing
#   ten create-and-drop cycles leave nothing
#   no storage row refers to a missing relation
#
# The same mutation reddens four of test/alter_am_cleanup.sh's forty-five. So
# this file is the one to run for that call site, which is not obvious from
# either file's subject: alter_am_cleanup is named for SET ACCESS METHOD.
#
# Note the shape of the five. Four of them re-capture `base` immediately before
# their own work. Grep `base="$(snapshot)"` to find them, once per arm: a line
# number cited here shifts every time this comment is edited, which is how the
# first draft of this paragraph came to cite four lines that had moved.
#
# So each of the four measures the increment its own step adds rather than
# inheriting an earlier failure. They do NOT cascade: a leak that has already
# happened is absorbed into the next baseline. That is why all four redden
# under the mutation instead of one reddening and three following -- every drop
# leaks and each arm detects its own. The `want` values show it: each is the
# previous `got`.
#
# The fifth, "no storage row refers to a missing relation", is absolute
# (got [25] want [0]). It covers the case the other four cannot see by
# construction: a STANDING leak that no single step increases gives every
# relative arm a zero increment, so all four pass and only this one fires.
#
# Usage:  test/drop_cleanup.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# every catalog that hangs off a storage id, as one comparable string
snapshot() {
	q "SELECT (SELECT count(*) FROM pgcolumnar.storage) || '/' ||
		(SELECT count(*) FROM pgcolumnar.projection) || '/' ||
		(SELECT count(*) FROM pgcolumnar.row_group) || '/' ||
		(SELECT count(*) FROM pgcolumnar.column_chunk) || '/' ||
		(SELECT count(*) FROM pgcolumnar.zone_map) || '/' ||
		(SELECT count(*) FROM pgcolumnar.bloom) || '/' ||
		(SELECT count(*) FROM pgcolumnar.delete_vector);" | tail -1
}

# --- 1. a plain table, which already cleaned up ------------------------------

base="$(snapshot)"
psql_run "CREATE TABLE dcl_plain (id int, v text) USING pgcolumnar;
	INSERT INTO dcl_plain SELECT g, 'x' || g FROM generate_series(1, 5000) g;
	DELETE FROM dcl_plain WHERE id % 5 = 0;" >/dev/null
psql_run "DROP TABLE dcl_plain;" >/dev/null
check "a plain table leaves nothing behind" "$(snapshot)" "$base"

# --- 2. a table with a projection --------------------------------------------

base="$(snapshot)"
psql_run "CREATE TABLE dcl_proj (id int, a int, b text) USING pgcolumnar;" >/dev/null
psql_run "SELECT pgcolumnar.add_projection('dcl_proj','dcl_p',ARRAY['a','b'],ARRAY['a']);" >/dev/null
psql_run "INSERT INTO dcl_proj SELECT g, g % 50, 'b' || g FROM generate_series(1, 5000) g;" >/dev/null

grew="$(snapshot)"
check "the projection did add metadata" \
	"$(awk -v a="$base" -v b="$grew" 'BEGIN { print (a == b) ? "no" : "yes" }')" "yes"

psql_run "DROP TABLE dcl_proj;" >/dev/null
check "dropping it takes the projection's metadata too" "$(snapshot)" "$base"

# --- 3. several projections, and one dropped by hand first -------------------

base="$(snapshot)"
psql_run "CREATE TABLE dcl_multi (id int, a int, b text, c int) USING pgcolumnar;" >/dev/null
psql_run "SELECT pgcolumnar.add_projection('dcl_multi','m1',ARRAY['a'],ARRAY['a']);" >/dev/null
psql_run "SELECT pgcolumnar.add_projection('dcl_multi','m2',ARRAY['b','c'],ARRAY['c']);" >/dev/null
psql_run "INSERT INTO dcl_multi SELECT g, g%10, 'z'||g, g*3 FROM generate_series(1, 4000) g;" >/dev/null
psql_run "SELECT pgcolumnar.drop_projection('dcl_multi','m1');" >/dev/null
psql_run "DROP TABLE dcl_multi;" >/dev/null
check "two projections, one dropped by hand, leave nothing" "$(snapshot)" "$base"

# --- 4. repetition, which is where an unbounded leak shows -------------------

# One drop leaking a handful of rows is easy to miss in a single comparison and
# impossible to miss over ten.
base="$(snapshot)"
for i in 1 2 3 4 5 6 7 8 9 10; do
	psql_run "CREATE TABLE dcl_c$i (id int, a int) USING pgcolumnar;" >/dev/null 2>&1
	psql_run "SELECT pgcolumnar.add_projection('dcl_c$i','p$i',ARRAY['a'],ARRAY['a']);" >/dev/null 2>&1
	psql_run "INSERT INTO dcl_c$i SELECT g, g FROM generate_series(1, 500) g;" >/dev/null 2>&1
	psql_run "DROP TABLE dcl_c$i;" >/dev/null 2>&1
done
check "ten create-and-drop cycles leave nothing" "$(snapshot)" "$base"

# --- 5. no storage row may outlive its relation ------------------------------

check "no storage row refers to a missing relation" \
	"$(q "SELECT count(*) FROM pgcolumnar.storage s
		WHERE NOT EXISTS (SELECT 1 FROM pg_class c WHERE c.oid = s.relation_oid);" | tail -1)" \
	"0"

# --- 6. the table still works, which a too-eager cleanup would break ---------

psql_run "DROP TABLE IF EXISTS dcl_live;
	CREATE TABLE dcl_live (id int, a int) USING pgcolumnar;" >/dev/null
psql_run "SELECT pgcolumnar.add_projection('dcl_live','lp',ARRAY['a'],ARRAY['a']);" >/dev/null
psql_run "INSERT INTO dcl_live SELECT g, g % 20 FROM generate_series(1, 3000) g;" >/dev/null
check "a live table with a projection still reads correctly" \
	"$(q "SELECT count(*) || '/' || sum(a) FROM dcl_live;" | tail -1)" \
	"$(q "SELECT count(*) || '/' || sum(g % 20) FROM generate_series(1,3000) g;" | tail -1)"
check "and its projection is still readable" \
	"$(q "SELECT count(*) FROM pgcolumnar.read_projection('dcl_live','lp');" | tail -1)" "3000"

# --- 7. a columnar MATERIALIZED VIEW takes its options row with it (#1265) ---
#
# The drop hook returned before it examined anything that was not an ordinary
# table (`if (get_rel_relkind(objectId) != RELKIND_RELATION) return;`), so an
# options row describing a columnar matview outlived the relation: a row keyed
# to a dead oid, reachable from nothing and cleared by nothing. Measured on all
# five majors before the fix -- the matview left 1 row, an ordinary columnar
# table in the same run left 0.
#
# THE ROW IS PLANTED DIRECTLY rather than through set_options, and that is
# deliberate. The subject of this suite is the DROP HOOK, and set_options
# carries its own relkind guard (#1265 widened both). Going through set_options
# would make a red here ambiguous between the two, and -- worse -- while that
# guard refused a matview, no row would exist and "0 rows afterwards" would be
# satisfied by a build whose hook does nothing at all. An INSERT reaches the
# hook question with one variable.

psql_run "DROP MATERIALIZED VIEW IF EXISTS dcl_mv; DROP TABLE IF EXISTS dcl_mv_src;
	CREATE TABLE dcl_mv_src (a int);
	INSERT INTO dcl_mv_src VALUES (1),(2);
	CREATE MATERIALIZED VIEW dcl_mv USING pgcolumnar AS SELECT a FROM dcl_mv_src;" >/dev/null

check "premise: the matview is relkind m on the columnar access method" \
	"$(q "SELECT c.relkind::text || am.amname FROM pg_class c
		JOIN pg_am am ON am.oid = c.relam WHERE c.relname = 'dcl_mv';" | tail -1)" \
	"mpgcolumnar"

psql_run "INSERT INTO pgcolumnar.options (regclass, stripe_row_limit)
	VALUES ('dcl_mv'::regclass, 123);" >/dev/null
check "premise: the matview carries an options row for the hook to clear" \
	"$(q "SELECT count(*) FROM pgcolumnar.options
		WHERE regclass = 'dcl_mv'::regclass;" | tail -1)" "1"

dcl_mv_oid="$(q "SELECT 'dcl_mv'::regclass::oid::text;" | tail -1)"
psql_run "DROP MATERIALIZED VIEW dcl_mv;" >/dev/null
check "dropping a columnar matview takes its options row with it" \
	"$(q "SELECT count(*) FROM pgcolumnar.options WHERE regclass = $dcl_mv_oid;" | tail -1)" \
	"0"

# REFRESH MUST NOT TAKE THE ROW. This is the risk the widening introduces, and
# it is here because the hook's own comment names it: "every REWRITE drops a
# transient pg_temp_<oid> through this hook, so VACUUM FULL, CLUSTER, ALTER
# TABLE ... ALTER COLUMN TYPE and CREATE MATERIALIZED VIEW take the same path".
# While the hook returned early for a matview, none of those could reach a
# matview's options row. Now that it does not, a refresh that dropped the
# relation rather than swapping its storage would silently discard the options
# the user set -- and the user would find out at the next write, not at the
# refresh. An arm is cheaper than that.

psql_run "DROP MATERIALIZED VIEW IF EXISTS dcl_mv_ref; DROP TABLE IF EXISTS dcl_mv_ref_src;
	CREATE TABLE dcl_mv_ref_src (a int);
	INSERT INTO dcl_mv_ref_src VALUES (1),(2);
	CREATE MATERIALIZED VIEW dcl_mv_ref USING pgcolumnar AS SELECT a FROM dcl_mv_ref_src;" >/dev/null
psql_run "SELECT pgcolumnar.set_options('dcl_mv_ref', stripe_row_limit => 100000);" >/dev/null
check "premise: the matview holds the options row set through set_options" \
	"$(q "SELECT stripe_row_limit::text FROM pgcolumnar.options
		WHERE regclass = 'dcl_mv_ref'::regclass;" | tail -1)" "100000"

psql_run "INSERT INTO dcl_mv_ref_src VALUES (3);" >/dev/null
psql_run "REFRESH MATERIALIZED VIEW dcl_mv_ref;" >/dev/null
check "premise: the refresh actually rewrote the matview" \
	"$(q "SELECT count(*) FROM dcl_mv_ref;" | tail -1)" "3"
check "REFRESH keeps the matview's options row" \
	"$(q "SELECT stripe_row_limit::text FROM pgcolumnar.options
		WHERE regclass = 'dcl_mv_ref'::regclass;" | tail -1)" "100000"

psql_run "DROP MATERIALIZED VIEW dcl_mv_ref;" >/dev/null

# The control, in the same run and by the same route. Without it a hook that
# deletes every options row unconditionally passes the arm above.
psql_run "DROP TABLE IF EXISTS dcl_opt_keep;
	CREATE TABLE dcl_opt_keep (a int) USING pgcolumnar;" >/dev/null
psql_run "INSERT INTO pgcolumnar.options (regclass, stripe_row_limit)
	VALUES ('dcl_opt_keep'::regclass, 123);" >/dev/null
check "control: an unrelated options row survives that drop" \
	"$(q "SELECT count(*) FROM pgcolumnar.options
		WHERE regclass = 'dcl_opt_keep'::regclass;" | tail -1)" "1"

dcl_keep_oid="$(q "SELECT 'dcl_opt_keep'::regclass::oid::text;" | tail -1)"
psql_run "DROP TABLE dcl_opt_keep;" >/dev/null
check "control: an ordinary columnar table still takes its own options row" \
	"$(q "SELECT count(*) FROM pgcolumnar.options WHERE regclass = $dcl_keep_oid;" | tail -1)" \
	"0"

pgc_summary
