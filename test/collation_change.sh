#!/usr/bin/env bash
#
# A column's collation changed without a rewrite (#1327).
#
# ALTER COLUMN ... TYPE text COLLATE <other> keeps the relfilenode: the type is
# the same, so PostgreSQL rewrites nothing. The zone maps kept the min and max
# computed under the OLD collation, and the reader compared them under the new
# one, so chunk-group pruning and the min/max path skipped rows the new
# collation orders differently. Reported by @jdatcmd while reviewing #1325, on
# main at 036d452: min|max read B|m where heap read a|m, and WHERE s < 'B'
# returned nothing where heap returned a,b.
#
# THE ORACLE IS HEAP: the same rows, the same ALTER, the same queries.
#
# A collation that orders 'a' before 'B' is needed, which "C" does not. libc's
# en_US.utf8 is used. A server without one is an environment defect (pgc_skip
# fails it unless PGC_ALLOW_MISSING_EN_US_COLLATION waives it), never a pass on a
# collation that cannot show the bug.
#
# Usage:  test/collation_change.sh [PG_CONFIG]

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

q "CREATE EXTENSION IF NOT EXISTS pgcolumnar;" >/dev/null

COLL="$(q "SELECT collname FROM pg_collation WHERE collname IN ('en_US.utf8', 'en_US.UTF-8', 'en_US')
           AND collencoding IN (-1, pg_char_to_encoding(current_setting('server_encoding')))
           ORDER BY collname LIMIT 1;")"
[ -n "$COLL" ] || pgc_skip en_us_collation "the server has no en_US collation to change a C column to"
check "premise: the new collation orders a lower-case letter before an upper-case one that C puts first" \
	"$(q "SELECT ('a' < 'B' COLLATE \"$COLL\")::text || '|' || ('a' < 'B' COLLATE \"C\")::text;")" "true|false"

# Three row groups, each holding upper- and lower-case values, so a group's C
# minimum is an upper-case letter that the new collation sorts AFTER some of its
# lower-case values: under C every group's minimum is >= 'B', so a stale zone map
# prunes every group from WHERE s < 'B'.
for t in cc ch; do
	am="USING pgcolumnar"; [ "$t" = ch ] && am=""
	q "CREATE TABLE $t (k int, s text COLLATE \"C\") $am;" >/dev/null
done
q "SELECT pgcolumnar.set_options('cc', stripe_row_limit => 1000, chunk_group_row_limit => 1000);" >/dev/null
for t in cc ch; do
	q "INSERT INTO $t SELECT g, CASE g % 4 WHEN 0 THEN 'a' WHEN 1 THEN 'B' WHEN 2 THEN 'm' ELSE 'b' END || (g % 7)
	   FROM generate_series(1, 3000) g;" >/dev/null
done
check "premise: the columnar table has several row groups" \
	"$(q "SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = pgcolumnar.get_storage_id('cc');")" "3"
# THE EXTENT EXISTED, AND HELD C'S BOUND (#1339). The arm after the change accepts
# "none", because the fix forgets the extent; without this one, a column_extent()
# that never returns a row passed it too.
check "before the change the column's extent is the C collation's bound" \
	"$(q "SELECT coalesce((SELECT minimum || '|' || maximum FROM pgcolumnar.column_extent('cc', 's')), 'none');")" \
	"$(q "SELECT min(s) || '|' || max(s) FROM ch;")"
before="$(q "SELECT pg_relation_filenode('cc');")"
for t in cc ch; do
	q "ALTER TABLE $t ALTER COLUMN s TYPE text COLLATE \"$COLL\";" >/dev/null
done
check "premise: the collation change rewrote nothing" "$(q "SELECT pg_relation_filenode('cc');")" "$before"

same() {	# same SQL-with-%T -> "same", or both answers when they differ
	local c h
	c="$(q "${1//%T/cc}")"; h="$(q "${1//%T/ch}")"
	# An answer is required (#1339): two empty answers are not the same answer.
	[ -n "$c" ] && [ "$c" = "$h" ] && echo same || echo "columnar [$c] heap [$h]"
}
check "after the collation change min and max are heap's" "$(same "SELECT min(s) || '|' || max(s) FROM %T;")" "same"
check "and a range below an upper-case bound returns heap's rows" \
	"$(same "SELECT count(*) || ':' || string_agg(DISTINCT s, ',' ORDER BY s) FROM %T WHERE s < 'B';")" "same"
check "and an equality on a value C sorts first returns heap's rows" \
	"$(same "SELECT count(*) FROM %T WHERE s = 'a0';")" "same"

# The extent (#1325) stores a min and max per column too; column_extent() prints
# what the planner would read, which must not be the old collation's.
check "the column's extent is not the old collation's bound" \
	"$(q "SELECT coalesce((SELECT minimum || '|' || maximum FROM pgcolumnar.column_extent('cc', 's')), 'none')
	         IN ('none', (SELECT min(s) || '|' || max(s) FROM ch))::text;")" "true"

# Rows written after the change are under the new collation; the answers stay heap's.
for t in cc ch; do
	q "INSERT INTO $t SELECT g, 'A' || (g % 5) FROM generate_series(3001, 4200) g;" >/dev/null
done
check "rows written after the change join heap's answer" \
	"$(same "SELECT count(*) || ':' || min(s) || '|' || max(s) FROM %T WHERE s < 'B';")" "same"

# Through a partitioned parent: the ALTER recurses to a columnar partition.
q "CREATE TABLE pp (k int, s text COLLATE \"C\") PARTITION BY RANGE (k);
   CREATE TABLE pp1 PARTITION OF pp FOR VALUES FROM (0) TO (100000) USING pgcolumnar;
   INSERT INTO pp SELECT k, s FROM ch WHERE k <= 3000;
   ALTER TABLE pp ALTER COLUMN s TYPE text COLLATE \"$COLL\";" >/dev/null
check "a collation change made through a partitioned parent reaches the columnar partition" \
	"$(q "SELECT (SELECT count(*) FROM pp1 WHERE s < 'B') = (SELECT count(*) FROM ch WHERE k <= 3000 AND s < 'B');")" "t"

# ---- what else trusted the old ordering ----------------------------------------
#
# vacuum_sorted stores the rows in the column's order, and the scan can claim
# that order to the planner (#751), dropping the Sort an ORDER BY would need.
# Under the new collation the stored order is not the column's order. The claim
# already refuses every collatable column for exactly this reason, so this arm
# is green on main; it is the guard that keeps that refusal, dated by a mutation
# that removes it.
q "CREATE TABLE vs (k int, s text COLLATE \"C\") USING pgcolumnar;
   CREATE TABLE vh (k int, s text COLLATE \"C\");
   INSERT INTO vs SELECT k, s FROM ch WHERE k <= 3000;
   INSERT INTO vh SELECT k, s FROM ch WHERE k <= 3000;
   SELECT pgcolumnar.vacuum_sorted('vs', 's');" >/dev/null
for t in vs vh; do
	q "ALTER TABLE $t ALTER COLUMN s TYPE text COLLATE \"$COLL\";" >/dev/null
done
same2() {	# same2 SQL-with-%T over vs and vh
	local c h
	c="$(q "${1//%T/vs}")"; h="$(q "${1//%T/vh}")"
	# An answer is required (#1339): two empty answers are not the same answer.
	[ -n "$c" ] && [ "$c" = "$h" ] && echo same || echo "columnar [$c] heap [$h]"
}
check "a table sorted under the old collation orders ORDER BY ... LIMIT as heap does" \
	"$(same2 "SELECT string_agg(s, ',') FROM (SELECT s FROM %T ORDER BY s, k LIMIT 6) x;")" "same"

# A projection sorted on the column: its rows and its zone maps are in the old order.
q "CREATE TABLE pj (k int, s text COLLATE \"C\") USING pgcolumnar;
   INSERT INTO pj SELECT k, s FROM ch WHERE k <= 3000;
   SELECT pgcolumnar.add_projection('pj', 'pj_by_s', columns => ARRAY['s', 'k'], sort_key => ARRAY['s']);
   ALTER TABLE pj ALTER COLUMN s TYPE text COLLATE \"$COLL\";" >/dev/null
check "a projection sorted on the column answers a range as heap does" \
	"$(q "SELECT (SELECT count(*) FROM pj WHERE s < 'B') = (SELECT count(*) FROM ch WHERE k <= 3000 AND s < 'B');")" "t"

# ---- a type change without a rewrite (#1341 item 19) ---------------------------
#
# int4 to oid is a binary-coercible cast, so ALTER COLUMN ... TYPE oid rewrites
# nothing, but oid orders unsigned: -5 is 4294967291. The zone maps, the extent, a
# vacuum_sorted order and a projection's sort all kept int4's order, as they kept
# the old collation's above. Every group holds negative and positive values, so
# under int4 each group's maximum is under 3000, and a stale zone map prunes every
# group from WHERE c > 3000000000.
for t in tc th; do
	am="USING pgcolumnar"; [ "$t" = th ] && am=""
	q "CREATE TABLE $t (k int, c int) $am;" >/dev/null
done
q "SELECT pgcolumnar.set_options('tc', stripe_row_limit => 1000, chunk_group_row_limit => 1000);" >/dev/null
for t in tc th; do
	q "INSERT INTO $t SELECT g, CASE WHEN g % 2 = 0 THEN g ELSE -g END FROM generate_series(1, 3000) g;" >/dev/null
done
check "premise: the int table has several row groups" \
	"$(q "SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = pgcolumnar.get_storage_id('tc');")" "3"
before="$(q "SELECT pg_relation_filenode('tc');")"
for t in tc th; do
	q "ALTER TABLE $t ALTER COLUMN c TYPE oid;" >/dev/null
done
check "premise: the change from int4 to oid rewrote nothing" "$(q "SELECT pg_relation_filenode('tc');")" "$before"
same_as() {	# same_as COLUMNAR HEAP SQL-with-%T -> "same", or both answers when they differ
	local c h
	c="$(q "${3//%T/$1}")"; h="$(q "${3//%T/$2}")"
	[ -n "$c" ] && [ "$c" = "$h" ] && echo same || echo "columnar [$c] heap [$h]"
}
check "after int4 to oid min and max are heap's" "$(same_as tc th "SELECT min(c) || '|' || max(c) FROM %T;")" "same"
check "and a range above every int4 value returns heap's rows" \
	"$(same_as tc th "SELECT count(*) || ':' || min(c) || '|' || max(c) FROM %T WHERE c > 3000000000;")" "same"
check "and an equality on a value that was negative returns heap's rows" \
	"$(same_as tc th "SELECT count(*) FROM %T WHERE c = 4294967295;")" "same"
check "the int column's extent is not int4's bound" \
	"$(q "SELECT coalesce((SELECT minimum || '|' || maximum FROM pgcolumnar.column_extent('tc', 'c')), 'none')
	         IN ('none', (SELECT min(c) || '|' || max(c) FROM th))::text;")" "true"
# A table sorted on the int column, and a projection sorted on it: both are in
# int4's order, which is not oid's.
q "CREATE TABLE ts (k int, c int) USING pgcolumnar;
   CREATE TABLE tsh (k int, c int);
   INSERT INTO ts SELECT g, CASE WHEN g % 2 = 0 THEN g ELSE -g END FROM generate_series(1, 3000) g;
   INSERT INTO tsh SELECT g, CASE WHEN g % 2 = 0 THEN g ELSE -g END FROM generate_series(1, 3000) g;
   SELECT pgcolumnar.vacuum_sorted('ts', 'c');
   ALTER TABLE ts ALTER COLUMN c TYPE oid;
   ALTER TABLE tsh ALTER COLUMN c TYPE oid;" >/dev/null
check "a table sorted on int4 orders ORDER BY ... LIMIT as heap does after int4 to oid" \
	"$(same_as ts tsh "SELECT string_agg(c::text, ',') FROM (SELECT c FROM %T ORDER BY c, k LIMIT 6) x;")" "same"
q "CREATE TABLE tp (k int, c int) USING pgcolumnar;
   INSERT INTO tp SELECT g, CASE WHEN g % 2 = 0 THEN g ELSE -g END FROM generate_series(1, 3000) g;
   SELECT pgcolumnar.add_projection('tp', 'tp_by_c', columns => ARRAY['c', 'k'], sort_key => ARRAY['c']);
   ALTER TABLE tp ALTER COLUMN c TYPE oid;" >/dev/null
check "a projection sorted on the int column answers a range as heap does after int4 to oid" \
	"$(same_as tp tsh "SELECT count(*) || ':' || min(c) FROM %T WHERE c > 3000000000;")" "same"

pgc_summary
