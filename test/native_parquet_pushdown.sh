#!/usr/bin/env bash
#
# pgColumnar Parquet FDW predicate / row-group skipping (Phase G). The FDW reads
# each row group's min/max statistics and skips groups that a pushable col-op-const
# clause proves empty; the executor still rechecks every returned row, so a skip can
# only save work, never drop rows. It asserts correctness against a heap oracle
# (skipping never changes results) AND that groups are actually skipped (via
# EXPLAIN ANALYZE's "Row Groups Skipped" counter).
#
# The file under test is written by pyarrow, deliberately: this suite is about
# the READ side, and an independent writer keeps it from passing because our
# reader and our writer share a misunderstanding. Our own exporter wrote no
# statistics at all until #850 (1.0-alpha3), which is why that gap survived here
# unseen; test/parquet_export_stats.sh covers the write side.
#
# Usage:  test/native_parquet_pushdown.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

if ! python3 -c 'import pyarrow.parquet' 2>/dev/null; then
	pgc_skip pyarrow "pyarrow not available; predicate-pushdown suite needs it to write stats"
fi

PARQ="$PGC_WORKDIR/stats.parquet"
# 200000 rows sorted by id, 4 row groups of 50000 -> disjoint id ranges
# [0,50000) [50000,100000) [100000,150000) [150000,200000), each with min/max stats.
python3 - "$PARQ" <<'PY'
import sys, pyarrow as pa, pyarrow.parquet as pq
n = 200000
ids = pa.array(range(n), pa.int32())
vals = pa.array([i * 1.5 for i in range(n)], pa.float64())
tbl = pa.table({"id": ids, "val": vals})
pq.write_table(tbl, sys.argv[1], row_group_size=50000)
t = pq.ParquetFile(sys.argv[1])
assert t.num_row_groups == 4, t.num_row_groups
PY

# heap oracle with identical data
psql_run "CREATE TABLE h (id int, val float8);"
psql_run "INSERT INTO h SELECT g, g*1.5 FROM generate_series(0,199999) g;"

psql_run "CREATE SERVER pq FOREIGN DATA WRAPPER pgcolumnar_parquet;"
psql_run "CREATE FOREIGN TABLE ft (id int4, val float8) SERVER pq OPTIONS (path '$PARQ');"

# ---- correctness: skipping never changes results (vs heap oracle) -----------
check "full scan == oracle" \
	"$(pgc_set_hash "SELECT * FROM ft")" "$(pgc_set_hash "SELECT * FROM h")"
for pred in \
	"id = 60000" \
	"id BETWEEN 60000 AND 70000" \
	"id < 100" \
	"id >= 199900" \
	"id > 60000 AND id < 60010" \
	"id <= 5 OR id >= 199995" \
	"val > 90000 AND val < 90030"
do
	check "predicate [$pred] == oracle" \
		"$(pgc_set_hash "SELECT * FROM ft WHERE $pred")" \
		"$(pgc_set_hash "SELECT * FROM h  WHERE $pred")"
done

# ---- the row-group edges: an inclusive bound skips nothing it matches --------
# Group 0 ends at 49999 and group 1 starts at 50000. A predicate whose constant is
# a group's max or min is where an inclusive comparison read as exclusive skips a
# group that holds the matching row -- and every predicate above stays clear of
# them (#1496 review): with `col >= c` skipping on max <= c, this suite was green.
for pred in \
	"id >= 49999" \
	"id > 49998" \
	"id <= 50000" \
	"id < 50001" \
	"id = 49999" \
	"id = 50000"
do
	check "row-group edge [$pred] == oracle" \
		"$(pgc_set_hash "SELECT * FROM ft WHERE $pred")" \
		"$(pgc_set_hash "SELECT * FROM h  WHERE $pred")"
done

# ---- skipping actually happens (EXPLAIN ANALYZE counter) --------------------
skipped_for() {  # skipped_for WHERE
	q "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF)
	   SELECT count(*) FROM ft WHERE $1" \
		| grep 'Row Groups Skipped' | grep -oE '[0-9]+' | head -1
}
skipped_for_t() {  # skipped_for_t TABLE WHERE
	q "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF)
	   SELECT count(*) FROM $1 WHERE $2" \
		| grep 'Row Groups Skipped' | grep -oE '[0-9]+' | head -1
}
groups_for() {  # groups_for WHERE  -> total groups reported
	q "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF)
	   SELECT count(*) FROM ft WHERE $1" \
		| grep 'Row Groups:' | grep -oE '[0-9]+' | head -1
}

check "reports 4 row groups" "$(groups_for 'id >= 0')" "4"
# a value in group 1 ([50000,100000)) -> other 3 groups skip
check "range in one group skips 3" "$(skipped_for 'id BETWEEN 60000 AND 70000')" "3"
# equality in group 0 -> 3 skipped
check "point in group 0 skips 3" "$(skipped_for 'id = 123')" "3"
# open range hitting only the last group -> 3 skipped
check "id >= 199900 skips 3" "$(skipped_for 'id >= 199900')" "3"
# open range hitting only the first group -> 3 skipped
check "id < 100 skips 3" "$(skipped_for 'id < 100')" "3"
# A float predicate confined to group 1 (val = id*1.5; [90000,90030] -> id in
# [60000,60020]). Only 2 groups skip, not 3: float >= and > are never pushed down,
# because Parquet excludes NaN from min/max while PostgreSQL sorts NaN above every
# value, so a NaN row can satisfy them while the stats look empty. The <= side
# still skips the two groups above the range.
check "float predicate skips 2 (>= not pushed, NaN-unsafe)" \
	"$(skipped_for 'val >= 90000 AND val <= 90030')" "2"
check "float < still skips" "$(skipped_for 'val < 1.0')" "3"
# a predicate spanning all groups -> 0 skipped
check "spanning predicate skips 0" "$(skipped_for 'id >= 0 AND id < 200000')" "0"
# no predicate -> 0 skipped
check "no predicate skips 0" "$(skipped_for 'true')" "0"

# ---- NaN soundness: skipping must never drop a NaN row ---------------------
# Parquet writers exclude NaN when computing min/max, so a group holding a NaN
# advertises ordinary finite bounds. PostgreSQL orders NaN above every value and
# treats NaN = NaN as true, so col > c, col >= c and col = 'NaN' can all match a
# row the statistics do not describe. Skipping on those would drop it silently,
# and the executor's recheck cannot recover a group that emitted nothing.
NANP="$PGC_WORKDIR/nan.parquet"
python3 - "$NANP" <<'PY'
import sys, pyarrow as pa, pyarrow.parquet as pq
vals = [float(i) for i in range(4000)]
vals[500] = float('nan')          # one NaN, in row group 0
tbl = pa.table({"id": pa.array(range(4000), pa.int32()),
                "val": pa.array(vals, pa.float64())})
pq.write_table(tbl, sys.argv[1], row_group_size=1000)
assert pq.ParquetFile(sys.argv[1]).num_row_groups == 4
PY

psql_run "CREATE FOREIGN TABLE ftn (id int4, val float8) SERVER pq OPTIONS (path '$NANP');"
psql_run "CREATE TABLE hn (id int, val float8);"
psql_run "INSERT INTO hn SELECT g, CASE WHEN g = 500 THEN 'NaN'::float8 ELSE g END
          FROM generate_series(0,3999) g;"

for pred in \
	"val > 1e300" \
	"val >= 1e300" \
	"val = 'NaN'::float8" \
	"val > 3998" \
	"val < 10" \
	"val <= 10" \
	"val = 1234"
do
	check "NaN-safe [$pred] == oracle" \
		"$(pgc_set_hash "SELECT * FROM ftn WHERE $pred")" \
		"$(pgc_set_hash "SELECT * FROM hn  WHERE $pred")"
done

# ---- inverted statistics must never drive a skip ---------------------------
# Parquet's UINT_32 logical type is stored as physical INT32 but its statistics
# are ordered UNSIGNED, while the reader decodes signed. A group spanning the
# sign boundary therefore reports min=1, max=-1294967296: an inverted interval.
# Every skip test assumes min <= max, so without a guard the equality and range
# tests skip groups that really do contain matching rows. The values themselves
# read back fine as int4, so this is a well-formed file producing bad bounds.
#
# (The same inversion arises from narrowing a wide INT32 into int2, but that now
# raises rather than wrapping -- see native_parquet_hardening.sh -- so it cannot
# be used to reach this code path.)
INVP="$PGC_WORKDIR/inverted.parquet"
python3 - "$INVP" <<'PY'
import sys, pyarrow as pa, pyarrow.parquet as pq
pq.write_table(pa.table({"c": pa.array([1, 3000000000], pa.uint32())}),
               sys.argv[1], row_group_size=2)
st = pq.ParquetFile(sys.argv[1]).metadata.row_group(0).column(0).statistics
assert st.min == 1 and st.max == 3000000000, (st.min, st.max)
PY

psql_run "CREATE FOREIGN TABLE fti (c int4) SERVER pq OPTIONS (path '$INVP');"
# oracle: the signed reading of those two stored values, which is what the data
# path yields; 3000000000 unsigned is -1294967296 as int4
psql_run "CREATE TABLE hi (c int4);"
psql_run "INSERT INTO hi VALUES (1), (-1294967296);"

check "inverted stats: unfiltered == oracle" \
	"$(pgc_set_hash "SELECT * FROM fti")" "$(pgc_set_hash "SELECT * FROM hi")"
for pred in \
	"c = 1" \
	"c = -1294967296" \
	"c >= 1" \
	"c <= -1294967296" \
	"c < 0" \
	"c > 0"
do
	check "inverted-stats [$pred] == oracle" \
		"$(pgc_set_hash "SELECT * FROM fti WHERE $pred")" \
		"$(pgc_set_hash "SELECT * FROM hi  WHERE $pred")"
done
check "inverted stats skip nothing" "$(skipped_for_t fti 'c = 1')" "0"

# ---- read_parquet over the same file ignores stats (reads all) but matches --
check "read_parquet same file == oracle" \
	"$(pgc_set_hash "SELECT * FROM pgcolumnar.read_parquet('$PARQ') AS t(id int4, val float8)")" \
	"$(pgc_set_hash "SELECT * FROM h")"

# ---- an INT32/INT64-backed DECIMAL is skippable ----------------------------
#
# A numeric column is only skippable when the file stores it as an INT32 or INT64
# DECIMAL, because those are the physical types the statistics decode from; a
# byte-array DECIMAL is filtered but never skipped. That distinction is newly
# reachable (a numeric target could not bind to an integer leaf before), and a
# scan that stops skipping still returns correct rows, so without this the
# regression would be silent.

if python3 -c 'import pyarrow.parquet' 2>/dev/null; then
	python3 - "$PGC_WORKDIR" <<'PYDEC'
import decimal, os, sys
import pyarrow as pa, pyarrow.parquet as pq
W = sys.argv[1]
# four row groups with disjoint ranges, so a predicate can exclude three
vals = [decimal.Decimal(i) / 100 for i in range(0, 40000)]
t = pa.table({"d": pa.array(vals, pa.decimal128(18, 2))})
pq.write_table(t, os.path.join(W, "dec_push.parquet"),
               store_decimal_as_integer=True, compression="none",
               row_group_size=10000)
f = pq.ParquetFile(os.path.join(W, "dec_push.parquet"))
if f.metadata.row_group(0).column(0).physical_type != "INT64":
    sys.exit("decimal was not stored as INT64")
if f.metadata.num_row_groups != 4:
    sys.exit("expected 4 row groups")
PYDEC
	if [ $? -ne 0 ]; then
		# ONE SKIP PER ARM, UNDER THE ARM'S OWN NAME (#994).
		for _pd_n in "INT64-backed DECIMAL: predicate skips 3 of 4 groups" \
				"INT64-backed DECIMAL: skipping did not drop rows" \
				"INT64-backed DECIMAL: unfiltered scan skips nothing" \
				"typmod: a numeric(18,1) column returns every row that rounds to 100.0" \
				"typmod: a numeric(18,1) column skips no group for d = 100.0" \
				"typmod: a numeric(18,2) column, the file's own typmod, still skips 3 of 4 groups"; do
			check_skip "$_pd_n" \
				"SKIP  $_pd_n (could not build the integer-DECIMAL pushdown file)" \
				"could not build the fixture file"
		done
	else
		psql_run "CREATE FOREIGN TABLE ftdec (d numeric) SERVER pq
		          OPTIONS (path '$PGC_WORKDIR/dec_push.parquet');"
		# values 0.00 .. 399.99 across four groups of 100.00 each: a range inside
		# the second group must exclude the other three
		check "INT64-backed DECIMAL: predicate skips 3 of 4 groups" \
			"$(skipped_for_t ftdec "d BETWEEN 120.00 AND 130.00")" "3"
		check "INT64-backed DECIMAL: skipping did not drop rows" \
			"$(q 'SELECT count(*) FROM ftdec WHERE d BETWEEN 120.00 AND 130.00;')" "1001"
		check "INT64-backed DECIMAL: unfiltered scan skips nothing" \
			"$(skipped_for_t ftdec 'd >= 0')" "0"
		# A declared typmod that rounds (#1790): numeric(18,1) reads 99.95 .. 99.99
		# as 100.0, but they sit in the first group, whose stored max is 99.99. A
		# skip decided on the stored statistics would drop them, so a column whose
		# typmod can change a value is never skipped. The file's own typmod,
		# numeric(18,2), changes nothing, so it keeps skipping.
		psql_run "CREATE FOREIGN TABLE ftdec1 (d numeric(18,1)) SERVER pq
		          OPTIONS (path '$PGC_WORKDIR/dec_push.parquet');"
		psql_run "CREATE FOREIGN TABLE ftdec2 (d numeric(18,2)) SERVER pq
		          OPTIONS (path '$PGC_WORKDIR/dec_push.parquet');"
		check "typmod: a numeric(18,1) column returns every row that rounds to 100.0" \
			"$(q 'SELECT count(*) FROM ftdec1 WHERE d = 100.0;')" "10"
		check "typmod: a numeric(18,1) column skips no group for d = 100.0" \
			"$(skipped_for_t ftdec1 'd = 100.0')" "0"
		check "typmod: a numeric(18,2) column, the file's own typmod, still skips 3 of 4 groups" \
			"$(skipped_for_t ftdec2 "d BETWEEN 120.00 AND 130.00")" "3"
	fi
else
	# ONE SKIP PER ARM, UNDER THE ARM'S OWN NAME (#994). The OUTER gate: without
	# pyarrow none of the three arms runs, and a single skip named for none of them
	# left all three with no record.
	for _pd_o in "INT64-backed DECIMAL: predicate skips 3 of 4 groups" \
			"INT64-backed DECIMAL: skipping did not drop rows" \
			"INT64-backed DECIMAL: unfiltered scan skips nothing" \
			"typmod: a numeric(18,1) column returns every row that rounds to 100.0" \
			"typmod: a numeric(18,1) column skips no group for d = 100.0" \
			"typmod: a numeric(18,2) column, the file's own typmod, still skips 3 of 4 groups"; do
		check_skip "$_pd_o" \
			"SKIP  $_pd_o (pyarrow not available)" \
			"pyarrow not available"
	done
fi


# ---- a time type at full precision keeps skipping (#1790) --------------------
# A declared typmod that can change a value turns row-group skipping off for the
# column. timestamp(6) is microsecond precision, the precision the file stores,
# so it changes nothing and must keep skipping. timestamp(3) rounds: each group's
# last value, hh:59:59.9996, reads as the next hour, beyond the group's stored
# max, so that column must skip nothing and return the rounded row.
TS_ARMS=("typmod: a timestamp(6) column still skips 3 of 4 groups"
	"typmod: a timestamp(3) column skips no group for t >= 03:00"
	"typmod: a timestamp(3) column returns the row that rounds up to 03:00")
if python3 -c 'import pyarrow.parquet' 2>/dev/null; then
	python3 - "$PGC_WORKDIR" <<'PYTS'
import datetime, os, sys
import pyarrow as pa, pyarrow.parquet as pq
W = sys.argv[1]
# four row groups, one per hour of 2021-01-01: 999 whole seconds, then hh:59:59.9996
base = datetime.datetime(2021, 1, 1)
vals = []
for g in range(4):
    h = base + datetime.timedelta(hours=g)
    vals += [h + datetime.timedelta(seconds=k) for k in range(999)]
    vals.append(h + datetime.timedelta(minutes=59, seconds=59, microseconds=999600))
pq.write_table(pa.table({"t": pa.array(vals, pa.timestamp("us"))}),
               os.path.join(W, "ts_push.parquet"), compression="none",
               row_group_size=1000)
if pq.ParquetFile(os.path.join(W, "ts_push.parquet")).metadata.num_row_groups != 4:
    sys.exit("expected 4 row groups")
PYTS
	if [ $? -ne 0 ]; then
		# ONE SKIP PER ARM, UNDER THE ARM'S OWN NAME (#994).
		for _pt in "${TS_ARMS[@]}"; do
			check_skip "$_pt" "SKIP  $_pt (could not build the timestamp pushdown file)" \
				"could not build the fixture file"
		done
	else
		psql_run "CREATE FOREIGN TABLE ftts6 (t timestamp(6)) SERVER pq
		          OPTIONS (path '$PGC_WORKDIR/ts_push.parquet');"
		psql_run "CREATE FOREIGN TABLE ftts3 (t timestamp(3)) SERVER pq
		          OPTIONS (path '$PGC_WORKDIR/ts_push.parquet');"
		check "typmod: a timestamp(6) column still skips 3 of 4 groups" \
			"$(skipped_for_t ftts6 "t >= TIMESTAMP '2021-01-01 03:00:00'")" "3"
		check "typmod: a timestamp(3) column skips no group for t >= 03:00" \
			"$(skipped_for_t ftts3 "t >= TIMESTAMP '2021-01-01 03:00:00'")" "0"
		check "typmod: a timestamp(3) column returns the row that rounds up to 03:00" \
			"$(q "SELECT count(*) FROM ftts3 WHERE t >= TIMESTAMP '2021-01-01 03:00:00'")" "1001"
	fi
else
	for _pt in "${TS_ARMS[@]}"; do
		check_skip "$_pt" "SKIP  $_pt (pyarrow not available)" "pyarrow not available"
	done
fi

pgc_summary
