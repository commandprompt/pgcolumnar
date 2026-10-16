#!/usr/bin/env bash
#
# pgColumnar external-Parquet scan core (Phase G). pgcolumnar.parquet_schema(path)
# reads a server-side Parquet file's footer and reports its leaf columns with the
# PostgreSQL type each maps to. This suite is the round-trip differential: it
# exports a columnar table covering every type the Parquet exporter emits, then
# asserts parquet_schema reports the source column types back. The exporter and
# the scan core share the type mapping, so a faithful mapping is a byte-for-byte
# inverse.
#
# A pyarrow-guarded tail writes a file with a REQUIRED (non-null) column to
# confirm the nullable flag reports both ways (the exporter only ever writes
# OPTIONAL columns).
#
# Usage:  test/native_parquet_schema.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

PARQUET="$PGC_WORKDIR/schema.parquet"

psql_run "CREATE TABLE n (
	c_i2  int2,
	c_i4  int4,
	c_i8  int8,
	c_f4  float4,
	c_f8  float8,
	c_bool bool,
	c_text text,
	c_bytea bytea,
	c_date date,
	c_time time,
	c_ts  timestamp,
	c_uuid uuid,
	c_num numeric(10,2)
) USING pgcolumnar;"

# one populated row plus an all-NULL row (columns are exported OPTIONAL either way)
psql_run "INSERT INTO n VALUES
	(2::int2, 3, 4::int8, 1.5::float4, 2.5::float8, true, 'hi',
	 '\\xdeadbeef'::bytea, '2020-01-01'::date, '12:34:56'::time,
	 '2020-01-01 12:34:56'::timestamp,
	 '11111111-1111-1111-1111-111111111111'::uuid, 123.45::numeric(10,2)),
	(NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL);"

rows="$(q "SELECT pgcolumnar.export_parquet('n', '$PARQUET');")"
check "export_parquet rows written" "$rows" "2"

# leaf-column count matches the table
check "parquet_schema column count" \
	"$(q "SELECT count(*) FROM pgcolumnar.parquet_schema('$PARQUET');")" "13"

# name-keyed so the assertion does not depend on result ordering. Each entry is
# "column_name|expected_data_type" (all exporter columns are nullable=t).
assert_col() {  # assert_col NAME EXPECTED_TYPE
	local name="$1" want="$2" got
	got="$(q "SELECT data_type FROM pgcolumnar.parquet_schema('$PARQUET') WHERE column_name = '$name';")"
	check "type of $name" "$got" "$want"
	got="$(q "SELECT nullable FROM pgcolumnar.parquet_schema('$PARQUET') WHERE column_name = '$name';")"
	check "nullable of $name" "$got" "t"
}

assert_col c_i2   "smallint"
assert_col c_i4   "integer"
assert_col c_i8   "bigint"
assert_col c_f4   "real"
assert_col c_f8   "double precision"
assert_col c_bool "boolean"
assert_col c_text "text"
assert_col c_bytea "bytea"
assert_col c_date "date"
assert_col c_time "time without time zone"
assert_col c_ts   "timestamp without time zone"
assert_col c_uuid "uuid"
assert_col c_num  "numeric(10,2)"

# unsupported / corrupt inputs are rejected, not silently empty
notpq="$PGC_WORKDIR/not.parquet"
psql_run "SELECT 1;" >/dev/null   # noop to keep ordering readable
printf 'this is not a parquet file at all' > "$notpq"
chmod 644 "$notpq"
# By the magic check, not by whatever fails next (#1341). This arm was "the query
# printed nothing", which any error satisfies: measured, with the magic check gone
# the 33-byte file is still refused, as a corrupt footer, and the arm passed. The
# message is the magic check's own.
err="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At \
	-c "SELECT count(*) FROM pgcolumnar.parquet_schema('$notpq');" 2>&1 | grep -m1 -oE 'ERROR:.*')"
check "bad magic is rejected by the magic check" \
	"$(case "$err" in *"is not a Parquet file (bad magic)"*) echo yes ;; *) echo "no (${err:-no error})" ;; esac)" "yes"

# WHY NOT `pgc_skip`, which 25 pyarrow suites use (#1215). It is terminal, and
# that is right for a suite with nothing else to do. Measured on pg18a, this
# suite is not that suite:
#
#     with pyarrow     35 passed + 0 skipped = 35
#     without pyarrow  29 passed + 6 skipped = 35
#
# 29 of the 35 arms need no pyarrow, the name set is identical in both runs --
# 35 records either way -- and the run still exits 0. Ending the suite would
# trade one silence for a larger loss. The six that do need it decline by name
# below, which is the #1159 model: the coverage lost stays visible as skips
# rather than becoming a pass.
# ---- nullable=false path (needs a REQUIRED column; only pyarrow writes one) ---
if python3 -c 'import pyarrow.parquet' 2>/dev/null; then
	REQ="$PGC_WORKDIR/required.parquet"
	python3 - "$REQ" <<'PY'
import sys, pyarrow as pa, pyarrow.parquet as pq
schema = pa.schema([
    pa.field("req_i", pa.int32(), nullable=False),
    pa.field("opt_s", pa.string(), nullable=True),
])
tbl = pa.table({"req_i": pa.array([1, 2, 3], pa.int32()),
                "opt_s": pa.array(["a", None, "c"], pa.string())}, schema=schema)
pq.write_table(tbl, sys.argv[1])
PY
	check "REQUIRED column reports nullable=f" \
		"$(q "SELECT nullable FROM pgcolumnar.parquet_schema('$REQ') WHERE column_name='req_i';")" "f"
	check "OPTIONAL column reports nullable=t" \
		"$(q "SELECT nullable FROM pgcolumnar.parquet_schema('$REQ') WHERE column_name='opt_s';")" "t"
	check "REQUIRED int32 maps to integer" \
		"$(q "SELECT data_type FROM pgcolumnar.parquet_schema('$REQ') WHERE column_name='req_i';")" "integer"

	# ---- field IDs (#388 step 2) ---------------------------------------------
	# Iceberg selects columns by SchemaElement field_id (thrift field 9), which
	# this reader used to skip. parquet_schema now reports it, with pyarrow as
	# the independent writer: ids deliberately out of order and disjoint from
	# the positional indices, so a reader that fabricated ids from position
	# could not pass. A file written WITHOUT ids reports NULL, never 0: 0 is a
	# legal field id, so absence must be distinguishable from it.
	FID="$PGC_WORKDIR/fieldids.parquet"
	python3 - "$FID" <<'PY'
import sys, pyarrow as pa, pyarrow.parquet as pq
schema = pa.schema([
    pa.field("alpha", pa.int32(), metadata={b"PARQUET:field_id": b"7"}),
    pa.field("beta", pa.string(), metadata={b"PARQUET:field_id": b"3"}),
    pa.field("gamma", pa.float64(), metadata={b"PARQUET:field_id": b"12"}),
])
tbl = pa.table({"alpha": pa.array([1], pa.int32()),
                "beta": pa.array(["x"], pa.string()),
                "gamma": pa.array([1.5], pa.float64())}, schema=schema)
pq.write_table(tbl, sys.argv[1])
PY
	check "premise: pyarrow wrote the field-id file" \
		"$([ -s "$FID" ] && echo yes)" "yes"
	check "field ids report as written, in schema order" \
		"$(q "SELECT string_agg(field_id::text, ',' ORDER BY ord) FROM (SELECT field_id, row_number() OVER () AS ord FROM pgcolumnar.parquet_schema('$FID')) s;")" \
		"7,3,12"
	check "a file without field ids reports NULL, not zero" \
		"$(q "SELECT count(*) FILTER (WHERE field_id IS NULL) || '/' || count(*) FROM pgcolumnar.parquet_schema('$REQ');")" \
		"2/2"
else
	# ONE SKIP PER ARM, UNDER THE ARM'S OWN NAME (#1185). A single summary skip
	# reconciles the accounting while six named checks disappear from the
	# recorded set, which is the shortfall that cannot be seen from the totals.
	for _nps_arm in \
		"premise: pyarrow wrote the field-id file" \
		"REQUIRED column reports nullable=f" \
		"OPTIONAL column reports nullable=t" \
		"REQUIRED int32 maps to integer" \
		"field ids report as written, in schema order" \
		"a file without field ids reports NULL, not zero"
	do
		check_skip "$_nps_arm" \
			"SKIP  $_nps_arm (pyarrow not available to write the fixture)" \
			"pyarrow not available"
	done
fi

pgc_summary
