#!/usr/bin/env bash
#
# pgColumnar: every rewrite rebuilds the distinct-count sketches (#1309 phase 4).
#
# A rewrite writes a table's rows again into new row groups. Phase 3's fold unions
# the sketches of clean row groups and scans the rest, so a rewritten storage must
# carry a sketch for every row group it wrote, describing exactly the rows it
# kept, and no sketch may survive for a row group that no longer exists. Each
# rewrite below starts from the same table: sketch_columns => a, five row groups
# of 1,000 rows, a third of them deleted. Afterwards three things are read:
#
#   - every row group of the base storage has its sketch, and no sketch row names
#     a row group that does not exist;
#   - the union of the base storage's sketches equals the registers recomputed in
#     SQL from the LIVE rows (hashint8extended, seed 0, the low 11 bits pick the
#     register, the rank is one more than the trailing zeros of the rest), so the
#     deleted rows are gone from the sketches too;
#   - for the rewriting ALTER forms, the table's own stripe_row_limit held.
#
# THE ALTER FORMS ARE THE REASON THIS SUITE EXISTS. A type change, ADD COLUMN with
# a volatile default, SET ACCESS METHOD and REFRESH MATERIALIZED VIEW fill a
# transient relation (pg_temp_<oid>) and swap it in, and the writer looked up the
# transient's OID in pgcolumnar.options and found nothing: the rewritten rows had
# 0 sketches for 4 row groups, and one row group of 3,334 rows under a
# stripe_row_limit of 1,000.
#
# Usage:  test/native_sketch_rewrite.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# The table every rewrite starts from: 5,000 rows in row groups of 1,000, a third
# of them deleted, sketched on a.
base_table() {	# name [access method of the table]
	psql_run "CREATE TABLE $1 (a bigint, b int) USING pgcolumnar;
		SELECT pgcolumnar.set_options('$1', stripe_row_limit => 1000,
			chunk_group_row_limit => 1000, sketch_columns => ARRAY['a']);
		INSERT INTO $1 SELECT g, g FROM generate_series(1, 5000) g;
		DELETE FROM $1 WHERE a % 3 = 0;" >/dev/null
}

# "groups|sketches|orphans" for a relation's base storage. An orphan is a sketch
# row, of any storage, naming a row group that does not exist.
shape() {	# relation
	q "SELECT (SELECT count(*) FROM pgcolumnar.row_group r
			WHERE r.storage_id = pgcolumnar.get_storage_id('$1'))
		|| '|' || (SELECT count(*) FROM pgcolumnar.sketch k
			WHERE k.storage_id = pgcolumnar.get_storage_id('$1'))
		|| '|' || (SELECT count(*) FROM pgcolumnar.sketch k WHERE NOT EXISTS (
			SELECT 1 FROM pgcolumnar.row_group r
			WHERE r.storage_id = k.storage_id AND r.group_number = k.group_number));"
}

# t when the union of the base storage's sketches is the registers of the live rows
union_is_live() {	# relation
	q "WITH u AS (
		SELECT md5(decode(string_agg(lpad(to_hex(m), 2, '0'), '' ORDER BY i), 'hex')) d
		FROM (SELECT i, coalesce(max(get_byte(k.registers, i)), 0) m
			  FROM generate_series(0, 2047) i
			  LEFT JOIN pgcolumnar.sketch k
			    ON k.storage_id = pgcolumnar.get_storage_id('$1') AND k.column_index = 0
			  GROUP BY i) x),
	  h AS (SELECT hashint8extended(a, 0) h FROM $1),
	  r AS (SELECT (h & 2047)::int idx, (h >> 11) & 9007199254740991 w FROM h),
	  k AS (SELECT idx, max(CASE WHEN w = 0 THEN 54
			ELSE round(log(2, (w & -w)::numeric))::int + 1 END) rank FROM r GROUP BY idx),
	  l AS (SELECT md5(decode(string_agg(lpad(to_hex(coalesce(k.rank, 0)), 2, '0'), ''
			ORDER BY i), 'hex')) d FROM generate_series(0, 2047) i LEFT JOIN k ON k.idx = i)
	SELECT u.d = l.d FROM u, l;"
}

# whole: groups == sketches > 0 and no orphan
whole() {	# relation
	local s; s="$(shape "$1")"
	IFS='|' read -r g k o <<<"$s"
	[ -n "$g" ] && [ "$g" -gt 0 ] && [ "$g" = "$k" ] && [ "$o" = 0 ] && echo yes || echo "no ($s)"
}

# --- 0. the starting point -----------------------------------------------------
base_table sr_ctl
check "before any rewrite each row group has its sketch, and the deleted rows are still in them" \
	"$(whole sr_ctl)|$(union_is_live sr_ctl)" "yes|f"

# --- 1. the pgColumnar rewrites ------------------------------------------------
# Each check is named in full, not built from a variable: the twin grader reads
# check names literally.
rewritten() {	# relation verb-sql -> the base table, then the rewrite
	base_table "$1"
	psql_run "$2" >/dev/null
}
groups_of() {	# relation -> its base storage's row groups
	q "SELECT count(*) FROM pgcolumnar.row_group r
		WHERE r.storage_id = pgcolumnar.get_storage_id('$1');"
}
rewritten sr_vac "SELECT pgcolumnar.vacuum('sr_vac');"
check "pgcolumnar.vacuum: every row group has its sketch, and no sketch outlives its row group" "$(whole sr_vac)" "yes"
check "pgcolumnar.vacuum: the sketches describe exactly the live rows" "$(union_is_live sr_vac)" "t"
rewritten sr_cr "SELECT pgcolumnar.compact_rewrite('sr_cr', 0.0);"
check "compact_rewrite: every row group has its sketch, and no sketch outlives its row group" "$(whole sr_cr)" "yes"
check "compact_rewrite: the sketches describe exactly the live rows" "$(union_is_live sr_cr)" "t"
rewritten sr_rc "SELECT pgcolumnar.recluster('sr_rc', 'b');"
check "recluster: every row group has its sketch, and no sketch outlives its row group" "$(whole sr_rc)" "yes"
check "recluster: the sketches describe exactly the live rows" "$(union_is_live sr_rc)" "t"
rewritten sr_vs "SELECT pgcolumnar.vacuum_sorted('sr_vs', 'b');"
check "vacuum_sorted: every row group has its sketch, and no sketch outlives its row group" "$(whole sr_vs)" "yes"
check "vacuum_sorted: the sketches describe exactly the live rows" "$(union_is_live sr_vs)" "t"
rewritten sr_tr "TRUNCATE sr_tr;
	INSERT INTO sr_tr SELECT g, g FROM generate_series(1, 5000) g WHERE g % 3 <> 0;"
check "TRUNCATE and reinsert: every row group has its sketch, and no sketch outlives its row group" "$(whole sr_tr)" "yes"
check "TRUNCATE and reinsert: the sketches describe exactly the live rows" "$(union_is_live sr_tr)" "t"

# --- 2. the rewriting ALTER forms ----------------------------------------------
# 3,334 live rows under a stripe_row_limit of 1,000 are four row groups; a rewrite
# that ignored the table's options wrote one.
rewritten sr_at "ALTER TABLE sr_at ALTER COLUMN b TYPE bigint;"
check "ALTER COLUMN TYPE: every row group has its sketch, and no sketch outlives its row group" "$(whole sr_at)" "yes"
check "ALTER COLUMN TYPE: the sketches describe exactly the live rows" "$(union_is_live sr_at)" "t"
check "ALTER COLUMN TYPE: the rewrite keeps the table's stripe_row_limit" "$(groups_of sr_at)" "4"
rewritten sr_ac "ALTER TABLE sr_ac ADD COLUMN c float8 DEFAULT random();"
check "ADD COLUMN with a volatile default: every row group has its sketch, and no sketch outlives its row group" "$(whole sr_ac)" "yes"
check "ADD COLUMN with a volatile default: the sketches describe exactly the live rows" "$(union_is_live sr_ac)" "t"
check "ADD COLUMN with a volatile default: the rewrite keeps the table's stripe_row_limit" "$(groups_of sr_ac)" "4"
rewritten sr_am "ALTER TABLE sr_am SET ACCESS METHOD heap;
	ALTER TABLE sr_am SET ACCESS METHOD pgcolumnar;"
check "SET ACCESS METHOD heap and back: every row group has its sketch, and no sketch outlives its row group" "$(whole sr_am)" "yes"
check "SET ACCESS METHOD heap and back: the sketches describe exactly the live rows" "$(union_is_live sr_am)" "t"
check "SET ACCESS METHOD heap and back: the rewrite keeps the table's stripe_row_limit" "$(groups_of sr_am)" "4"

# The options are the table's for every setting, not only the sketch: a table set to
# compression none came back from a type change compressed (block codec 3).
psql_run "CREATE TABLE sr_cz (a bigint, b text) USING pgcolumnar;
	SELECT pgcolumnar.set_options('sr_cz', compression => 'none');
	INSERT INTO sr_cz SELECT g, repeat('x', 200) || g FROM generate_series(1, 20000) g;
	ALTER TABLE sr_cz ALTER COLUMN a TYPE numeric;" >/dev/null
check "ALTER COLUMN TYPE: the rewrite keeps the table's compression" \
	"$(q "SELECT string_agg(DISTINCT block_codec::text, ',') FROM pgcolumnar.column_chunk c
		WHERE c.storage_id = pgcolumnar.get_storage_id('sr_cz');")" "0"

# A materialized view refreshes the same way: into a transient, then a swap.
psql_run "CREATE TABLE sr_src (a bigint, b int);
	INSERT INTO sr_src SELECT g, g FROM generate_series(1, 5000) g WHERE g % 3 <> 0;
	CREATE MATERIALIZED VIEW sr_mv USING pgcolumnar AS SELECT a, b FROM sr_src WITH NO DATA;
	SELECT pgcolumnar.set_options('sr_mv', stripe_row_limit => 1000,
		chunk_group_row_limit => 1000, sketch_columns => ARRAY['a']);
	REFRESH MATERIALIZED VIEW sr_mv;" >/dev/null
check "REFRESH MATERIALIZED VIEW: every row group has its sketch, and no sketch outlives its row group" \
	"$(whole sr_mv)" "yes"
check "REFRESH MATERIALIZED VIEW: the sketches describe exactly the live rows" "$(union_is_live sr_mv)" "t"
check "REFRESH MATERIALIZED VIEW: the refresh keeps the view's stripe_row_limit" \
	"$(q "SELECT count(*) FROM pgcolumnar.row_group r
		WHERE r.storage_id = pgcolumnar.get_storage_id('sr_mv');")" "4"

# REFRESH ... CONCURRENTLY builds its transient under ExclusiveLock, not
# AccessExclusiveLock; the mapping must take that lock too.
psql_run "CREATE UNIQUE INDEX sr_mv_a ON sr_mv (a);
	INSERT INTO sr_src SELECT g, g FROM generate_series(5001, 6000) g;
	REFRESH MATERIALIZED VIEW CONCURRENTLY sr_mv;" >/dev/null
check "REFRESH MATERIALIZED VIEW CONCURRENTLY: every row group has its sketch, and no sketch outlives its row group" \
	"$(whole sr_mv)" "yes"
check "REFRESH MATERIALIZED VIEW CONCURRENTLY: the sketches describe exactly the live rows" "$(union_is_live sr_mv)" "t"

# A USER table that happens to be named like a transient borrows nothing: its
# creator holds no ExclusiveLock on the relation the name points at. Without that
# check, CREATE TABLE pg_temp_<oid> AS took the other table's stripe limit and
# sketches (@jdatcmd, #1775 review).
base_table sr_donor
donor_oid="$(q "SELECT 'sr_donor'::regclass::oid;")"
psql_run "CREATE TABLE \"pg_temp_$donor_oid\" USING pgcolumnar AS SELECT g::bigint AS a, g AS b
	FROM generate_series(1, 3000) g;" >/dev/null
check "a user table named pg_temp_<oid> keeps its own defaults: one row group and no sketch" \
	"$(q "SELECT (SELECT count(*) FROM pgcolumnar.row_group r
			WHERE r.storage_id = pgcolumnar.get_storage_id('\"pg_temp_$donor_oid\"'))
		|| '|' || (SELECT count(*) FROM pgcolumnar.sketch k
			WHERE k.storage_id = pgcolumnar.get_storage_id('\"pg_temp_$donor_oid\"'));")" "1|0"

pgc_summary
