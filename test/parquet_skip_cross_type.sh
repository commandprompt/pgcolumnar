#!/usr/bin/env bash
#
# Parquet row-group skipping uses a constant of another type in the column's
# operator family (#1712).
#
# The foreign table's group-exclusion check refused any constant whose type was
# not the column's. A bare integer literal is int4, so the most ordinary filter
# on a bigint column, `WHERE id > 150000`, read every row group; only
# `150000::int8` skipped. The same held for a float4 constant on a float8 column.
# The native columnar path already compares through the operator family's
# cross-type comparison function, and this suite asks the same of Parquet.
#
# A file of 200,000 ascending rows in 4 row groups of 50,000. Each arm reads the
# skip count from EXPLAIN (ANALYZE) and, beside it, the answer, compared with the
# same query written with a constant of the column's own type.
#
# Usage:  test/parquet_skip_cross_type.sh [PG_CONFIG]

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

if ! python3 -c 'import pyarrow.parquet' 2>/dev/null; then
	pgc_skip pyarrow "pyarrow not available; Parquet cross-type skip suite needs it"
fi

W="$PGC_WORKDIR"
NGROUPS="$(python3 - "$W" <<'PY'
import sys, pyarrow as pa, pyarrow.parquet as pq
w = sys.argv[1]
ids = list(range(200000))
pq.write_table(pa.table({"id": pa.array(ids, pa.int64()),
                         "f": pa.array([float(i) for i in ids], pa.float64())}),
               f"{w}/xt.parquet", row_group_size=50000)
# NaN sits in the second group; Parquet statistics leave it out of min and max
pq.write_table(pa.table({"f": pa.array([1.0, 2.0, 3.0, float("nan")], pa.float64())}),
               f"{w}/xn.parquet", row_group_size=2)
print(pq.ParquetFile(f"{w}/xt.parquet").metadata.num_row_groups)
PY
)"
chmod 644 "$W/xt.parquet" "$W/xn.parquet"
psql_run "CREATE SERVER pq FOREIGN DATA WRAPPER pgcolumnar_parquet;
          CREATE FOREIGN TABLE xt (id int8, f float8) SERVER pq OPTIONS (path '$W/xt.parquet');
          CREATE FOREIGN TABLE xn (f float8) SERVER pq OPTIONS (path '$W/xn.parquet');"

# skipped QUERY -> the "Row Groups Skipped" count EXPLAIN (ANALYZE) reports, or the error
skipped() {
	local out
	out="$(q "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) $1" 2>&1)"
	grep -oE 'Row Groups Skipped: [0-9]+' <<<"$out" | grep -oE '[0-9]+$' || echo "no reading: $(head -1 <<<"$out")"
}

check_num "premise: the file holds 4 row groups (pyarrow's own count)" "$NGROUPS" "4"
check_num "control: an int8 constant on the int8 column skips 3 of the 4 row groups" \
	"$(skipped "SELECT count(*) FROM xt WHERE id > 150000::int8")" "3"
check_num "an int4 constant on the int8 column skips 3 of the 4 row groups" \
	"$(skipped "SELECT count(*) FROM xt WHERE id > 150000")" "3"
check_num "and returns the rows the int8 constant returns" \
	"$(q "SELECT count(*) FROM xt WHERE id > 150000;")" "$(q "SELECT count(*) FROM xt WHERE id > 150000::int8;")"
check_num "an int4 constant in an equality on the int8 column skips 3 of the 4 row groups" \
	"$(skipped "SELECT count(*) FROM xt WHERE id = 150001")" "3"
check_num "a constant on the left of the operator is compared the same way" \
	"$(skipped "SELECT count(*) FROM xt WHERE 150000 < id")" "3"
# `<` because the float rule never skips on `>` or `>=` (a NaN row satisfies them
# and sits outside min/max), and a NEGATIVE constant because a float4 misread as
# float8 is a tiny positive number: then group 0 (min 0.0) would not be skipped.
check_num "a float4 constant below every row skips all 4 row groups of the float8 column" \
	"$(skipped "SELECT count(*) FROM xt WHERE f < -1::float4")" "4"
check_num "and returns the rows the float8 constant returns" \
	"$(q "SELECT count(*) FROM xt WHERE f < 50000::float4;")" "$(q "SELECT count(*) FROM xt WHERE f < 50000::float8;")"

# A NaN constant is read by its own type. Read as the column's float8, a float4 NaN
# is some other number, and the group holding the NaN row would be skipped.
check_num "a float4 NaN constant still finds the float8 column's NaN row" \
	"$(q "SELECT count(*) FROM xn WHERE f = 'NaN'::float4;")" "1"

pgc_summary
