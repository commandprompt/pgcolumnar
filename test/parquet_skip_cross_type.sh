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
# a naive timestamp column in 2 row groups: group 0 spans New York's 2024-03-10
# spring-forward gap (ids 0-2), group 1 is the hour before it (ids 3-5)
import datetime as dt
gap = [dt.datetime(2024, 3, 10, h, m) for h, m in ((2, 0), (2, 30), (3, 0), (1, 0), (1, 10), (1, 20))]
pq.write_table(pa.table({"id": pa.array(range(6), pa.int64()), "ts": pa.array(gap, pa.timestamp("us"))}),
               f"{w}/xg.parquet", row_group_size=3)
print(pq.ParquetFile(f"{w}/xt.parquet").metadata.num_row_groups,
      pq.ParquetFile(f"{w}/xg.parquet").metadata.num_row_groups)
PY
)"
GGROUPS="${NGROUPS#* }"
NGROUPS="${NGROUPS%% *}"
chmod 644 "$W/xt.parquet" "$W/xn.parquet" "$W/xg.parquet"
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

# A timestamp column and a timestamptz constant (OffgridwithJD's review of #1737).
# The cross-type comparison converts each statistic, a wall time, to an instant in
# the session zone, and that conversion is not monotonic across a spring-forward
# gap. In New York, group 0's max 03:00 becomes 07:00Z, yet its row 02:30 becomes
# 07:30Z. Every session below runs in New York; the heap copy is the oracle.
psql_run "CREATE FOREIGN TABLE xg (id int8, ts timestamp) SERVER pq OPTIONS (path '$W/xg.parquet');
          CREATE TABLE xg_heap AS SELECT * FROM xg;"
qny() { PGOPTIONS="-c TimeZone=America/New_York" q "$1"; }
ids() { qny "SELECT coalesce(string_agg(id::text, ',' ORDER BY id), 'none') FROM $1 WHERE $2;"; }
check_num "premise: the gap file holds 2 row groups (pyarrow's own count)" "$GGROUPS" "2"
check_text "premise: in New York the heap copy finds row 1 for ts > 07:15Z, ts >= 07:15Z and ts = 07:30Z" \
	"$(ids xg_heap "ts > '2024-03-10 07:15+00'::timestamptz") $(ids xg_heap "ts >= '2024-03-10 07:15+00'::timestamptz") $(ids xg_heap "ts = '2024-03-10 07:30+00'::timestamptz")" \
	"1 1 1"
check_text "a timestamptz constant after a spring-forward gap finds the timestamp column's row the heap finds (>)" \
	"$(ids xg "ts > '2024-03-10 07:15+00'::timestamptz")" "$(ids xg_heap "ts > '2024-03-10 07:15+00'::timestamptz")"
check_text "a timestamptz constant after a spring-forward gap finds the timestamp column's row the heap finds (>=)" \
	"$(ids xg "ts >= '2024-03-10 07:15+00'::timestamptz")" "$(ids xg_heap "ts >= '2024-03-10 07:15+00'::timestamptz")"
check_text "a timestamptz constant equal to a row's instant after a spring-forward gap finds that row, as the heap does" \
	"$(ids xg "ts = '2024-03-10 07:30+00'::timestamptz")" "$(ids xg_heap "ts = '2024-03-10 07:30+00'::timestamptz")"
check_num "control: a timestamp constant on the timestamp column skips 1 of the 2 row groups" \
	"$(PGOPTIONS="-c TimeZone=America/New_York" skipped "SELECT count(*) FROM xg WHERE ts > '2024-03-10 02:00'::timestamp")" "1"
check_text "and returns the rows the heap returns" \
	"$(ids xg "ts > '2024-03-10 02:00'::timestamp")" "$(ids xg_heap "ts > '2024-03-10 02:00'::timestamp")"

pgc_summary
