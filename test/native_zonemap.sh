#!/usr/bin/env bash
#
# pgColumnar native zone maps, storage side (Phase D5a): a native-format (PGCN v1)
# table now records a Small Materialized Aggregate per 1024-value vector and per
# column chunk in pgcolumnar.zone_map (min, max, value_count, null_count; sum
# lands in D5b). D5a computes and stores them but does not yet read them, so this
# suite validates the catalog contents directly: the row structure, the exact
# value/null counts against the heap oracle, min/max presence and width, per-vector
# bounds, that the rows are removed when the table is dropped, and that adding zone
# maps did not perturb the read path (set-hash parity with a heap mirror).
#
# Usage:  test/native_zonemap.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# 6144 rows, a 2048-row group limit and the fixed 1024 vector length give a clean
# 3 groups x 2 vectors per column chunk. Four orderable columns with distinct null
# patterns exercise value_count/null_count and min/max.
#
# v is NOT ascending (#1341). With every column ascending, a vector's first value is
# always its minimum, so a writer that never lowered its min past the first value
# stored exactly the right bytes, and every check here passed 18/18 (nominupd).
# A permutation of 1..6150 keeps v's values, nulls and counts what they were.
GEN="SELECT g,
       (g * 10)::bigint,
       'lbl-' || (g % 50),
       CASE WHEN g % 7 = 0 THEN NULL ELSE ((g::bigint * 7919) % 6151)::int END
  FROM generate_series(1, 6144) g"

psql_run "CREATE TABLE h (id int, k bigint, label text, v int);"
psql_run "CREATE TABLE n (id int, k bigint, label text, v int) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('n', stripe_row_limit => 2048, chunk_group_row_limit => 1024);"
psql_run "INSERT INTO h $GEN;"
psql_run "INSERT INTO n $GEN;"

SID="$(q "SELECT pgcolumnar.get_storage_id('n');")"
zq() { q "SELECT $1 FROM pgcolumnar.zone_map WHERE storage_id = $SID;"; }

# A fixed-width bound is stored as the value's own bytes (PgColumnarEncodeValue),
# native order: little-endian on every platform CI runs. zm_le reads such a bound
# back as an integer, so the bounds can be compared with the heap's.
psql_run "CREATE FUNCTION zm_le(b bytea) RETURNS bigint LANGUAGE sql IMMUTABLE AS
	\$\$SELECT sum((get_byte(b, i)::bigint) << (8 * i))::bigint
	    FROM generate_series(0, length(b) - 1) i\$\$;"
# bounds_match KIND MINMAX -> how many zone rows of id (0), k (1) and v (3) hold the
# heap's own bound for their group and vector. KIND is "vector" (vector_index >= 0,
# 18 rows: 3 groups x 2 vectors x 3 columns) or "chunk" (vector_index = -1, 9 rows).
bounds_match() {
	local vfilter="vector_index >= 0" side="$2"
	[ "$1" = chunk ] && vfilter="vector_index = -1"
	q "WITH z AS (
	       SELECT dense_rank() OVER (ORDER BY group_number) - 1 AS g, column_index AS c,
	              vector_index AS vi, zm_le($side) AS got
	         FROM pgcolumnar.zone_map
	        WHERE storage_id = $SID AND column_index IN (0, 1, 3) AND $vfilter),
	     hv AS (
	       SELECT (id - 1) / 2048 AS g, ((id - 1) % 2048) / 1024 AS vi,
	              min(id) AS min0, max(id) AS max0, min(k) AS min1, max(k) AS max1,
	              min(v) AS min3, max(v) AS max3
	         FROM h GROUP BY 1, 2
	       UNION ALL
	       SELECT (id - 1) / 2048, -1, min(id), max(id), min(k), max(k), min(v), max(v)
	         FROM h GROUP BY 1)
	   SELECT count(*) FROM z JOIN hv USING (g, vi)
	    WHERE z.got = CASE z.c WHEN 0 THEN hv.${side:0:3}0 WHEN 1 THEN hv.${side:0:3}1
	                           ELSE hv.${side:0:3}3 END;"
}

check "row count" "$(q 'SELECT count(*) FROM n;')" "6144"
check "read parity (no regression)" \
	"$(pgc_set_hash 'SELECT id, k, label, v FROM n')" \
	"$(pgc_set_hash 'SELECT id, k, label, v FROM h')"

# 3 groups x 4 columns whole-chunk rows (vector_index = -1).
check "whole-chunk zone rows" "$(zq 'count(*) FILTER (WHERE vector_index = -1)')" "12"
# 3 groups x 2 vectors x 4 columns per-vector rows.
check "per-vector zone rows" "$(zq 'count(*) FILTER (WHERE vector_index >= 0)')" "24"

# Every whole-chunk row covers its row group's rows; every vector covers 1024.
check "whole-chunk value+null = group rows" \
	"$(zq 'count(*) FILTER (WHERE vector_index = -1 AND value_count + null_count <> 2048)')" \
	"0"
check "per-vector value+null = 1024" \
	"$(zq 'count(*) FILTER (WHERE vector_index >= 0 AND value_count + null_count <> 1024)')" \
	"0"

# Counts must match the heap oracle exactly, summed over the whole-chunk rows.
# column_index is 0-based: id=0, k=1, label=2, v=3.
check_num "value_count sums (v, nullable)" \
	"$(zq 'sum(value_count) FILTER (WHERE vector_index = -1 AND column_index = 3)')" \
	"$(q 'SELECT count(v) FROM h;')"
check_num "null_count sums (v, nullable)" \
	"$(zq 'sum(null_count) FILTER (WHERE vector_index = -1 AND column_index = 3)')" \
	"$(q 'SELECT count(*) - count(v) FROM h;')"
check "value_count sums (id, no nulls)" \
	"$(zq 'sum(value_count) FILTER (WHERE vector_index = -1 AND column_index = 0)')" \
	"6144"
check "null_count zero (id, no nulls)" \
	"$(zq 'sum(null_count) FILTER (WHERE vector_index = -1 AND column_index = 0)')" \
	"0"

# Per-vector value counts of a column sum to its whole-chunk value count, per group.
check "per-vector value_count folds into chunk (k)" \
	"$(zq 'count(*) FILTER (WHERE column_index = 1 AND vector_index = -1
	        AND value_count <> (SELECT sum(z2.value_count)
	                            FROM pgcolumnar.zone_map z2
	                            WHERE z2.storage_id = pgcolumnar.zone_map.storage_id
	                              AND z2.group_number = pgcolumnar.zone_map.group_number
	                              AND z2.column_index = 1 AND z2.vector_index >= 0))')" \
	"0"

# Orderable columns carry min/max; fixed-width widths are exact.
check "min/max present for all columns" "$(zq 'count(*) FILTER (WHERE minimum IS NULL OR maximum IS NULL)')" "0"
check "id (int4) min width = 4 bytes" \
	"$(zq 'count(*) FILTER (WHERE column_index = 0 AND octet_length(minimum) <> 4)')" "0"
check "k (int8) max width = 8 bytes" \
	"$(zq 'count(*) FILTER (WHERE column_index = 1 AND octet_length(maximum) <> 8)')" "0"

# The bounds' VALUES, not only their presence and width (#1341). Presence and width
# held for a max never raised past a vector's first value (nomaxupd), a min never
# lowered (nominupd) and a chunk max never folded past its first vector (foldmax):
# main passed 18/18 under each.
check "premise: v is not ascending within a vector, so a stale minimum would show" \
	"$(q "SELECT count(*) > 0 FROM (
	        SELECT (id - 1) / 1024 AS vec, min(v) AS mn,
	               (array_agg(v ORDER BY id) FILTER (WHERE v IS NOT NULL))[1] AS first
	          FROM h GROUP BY 1) s WHERE first <> mn;")" "t"
check_num "every vector's minimum of id, k and v is the heap's" "$(bounds_match vector minimum)" "18"
check_num "every vector's maximum of id, k and v is the heap's" "$(bounds_match vector maximum)" "18"
check_num "every whole-chunk minimum of id, k and v is the heap's" "$(bounds_match chunk minimum)" "9"
check_num "every whole-chunk maximum of id, k and v is the heap's" "$(bounds_match chunk maximum)" "9"

# D5b populates sum for int2/int4 columns (id, v) as an exact numeric; other
# types (k bigint, label text) keep a null sum. column_index: id=0, v=3, k=1, label=2.
check "sum present for int4 columns" \
	"$(zq 'count(*) FILTER (WHERE column_index IN (0,3) AND value_count > 0 AND sum IS NULL)')" \
	"0"
check "sum null for non-int4 columns" \
	"$(zq 'count(*) FILTER (WHERE column_index IN (1,2) AND sum IS NOT NULL)')" \
	"0"
check_num "whole-chunk sum(id) matches heap" \
	"$(zq 'sum(sum) FILTER (WHERE column_index = 0 AND vector_index = -1)')" \
	"$(q 'SELECT sum(id) FROM h;')"

# Zone maps are removed with the relation.
psql_run "DROP TABLE n;"
check "zone maps cleaned on drop" \
	"$(q "SELECT count(*) FROM pgcolumnar.zone_map WHERE storage_id = $SID;")" "0"

pgc_summary
