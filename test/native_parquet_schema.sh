#!/usr/bin/env bash
#
# pgColumnar external-Parquet scan core (Phase G). pgcolumnar.parquet_schema(path)
# reads a server-side Parquet file's footer and reports its top-level columns with
# the PostgreSQL type each maps to (a nested column read_parquet takes leaf by
# leaf: one row per leaf, #1893). This suite is the round-trip differential: it
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

# ---- timestamp vs timestamptz: the UTC flag the exporter writes (#1706) -------
# A timestamp is a wall time with no zone; a timestamptz is an instant. Parquet says
# which through TIMESTAMP's isAdjustedToUTC, and the legacy TIMESTAMP_MICROS
# converted type implies true. The exporter used to write both columns as
# TIMESTAMP_MICROS, so every other reader took a wall time for a UTC instant: under
# a New York session, 12:00 came back as 12:00 UTC, 08:00 local. pgcolumnar's own
# import ignores the flag, so its round trip hid it. pyarrow is the independent
# reader. The round trip needs no pyarrow and stays outside the guard.
TSF="$PGC_WORKDIR/tsflag.parquet"
psql_run "CREATE TABLE tsf (ts timestamp, tz timestamptz) USING pgcolumnar;"
psql_run "INSERT INTO tsf VALUES ('2024-06-01 12:00:00', '2024-06-01 12:00:00-04');"
psql_run "SELECT pgcolumnar.export_parquet('tsf', '$TSF');"
check_text "read_parquet returns the exported timestamp and timestamptz unchanged" \
	"$(PGOPTIONS='-c timezone=America/New_York' q "SELECT ts || '|' || tz FROM pgcolumnar.read_parquet('$TSF') AS x(ts timestamp, tz timestamptz);")" \
	"2024-06-01 12:00:00|2024-06-01 12:00:00-04"
if python3 -c 'import pyarrow.parquet' 2>/dev/null; then
	tsf_read() {	# FIELD WHAT -> the field's tz ("none" when it has none) or its first value
		python3 - "$TSF" "$1" "$2" 2>&1 <<'PY' | tail -1
import sys, pyarrow.parquet as pq
path, field, what = sys.argv[1:4]
col = pq.read_table(path).column(field)
if what == "tz":
    print(col.type.tz if getattr(col.type, "tz", None) else "none")
else:
    print(col[0].as_py())
PY
	}
	check_text "an exported timestamp column is not UTC-adjusted (pyarrow reads no time zone)" \
		"$(tsf_read ts tz)" "none"
	check_text "an exported timestamptz column is UTC-adjusted (pyarrow reads UTC)" \
		"$(tsf_read tz tz)" "UTC"
	check_text "pyarrow reads an exported timestamp back as its wall time" \
		"$(tsf_read ts value)" "2024-06-01 12:00:00"
	check_text "pyarrow reads an exported timestamptz back as its instant" \
		"$(tsf_read tz value)" "2024-06-01 16:00:00+00:00"
else
	for _nps_arm in \
		"an exported timestamp column is not UTC-adjusted (pyarrow reads no time zone)" \
		"an exported timestamptz column is UTC-adjusted (pyarrow reads UTC)" \
		"pyarrow reads an exported timestamp back as its wall time" \
		"pyarrow reads an exported timestamptz back as its instant"
	do
		check_skip "$_nps_arm" \
			"SKIP  $_nps_arm (pyarrow not available to read the export)" \
			"pyarrow not available"
	done
fi

# ---- nested columns report by their top-level names (#1893) -----------------
# parquet_schema used to list a nested column by its leaf names: a list column as
# "element", a struct's fields as "a" and "b". A user who copied those names into a
# read_parquet column list got names the file does not have. Each top-level column
# now gets one row, with the type read_parquet reads it as: an array type for a
# list of a scalar, and the field list for a struct, which the user declares as a
# composite type. The exporter writes both shapes, so this arm needs no pyarrow.
NEST="$PGC_WORKDIR/nested.parquet"
psql_run "CREATE TYPE nest_s AS (a int8, b text);"
psql_run "CREATE TABLE nest (l int8[], s nest_s, n int4) USING pgcolumnar;"
psql_run "INSERT INTO nest VALUES ('{1,2}', ROW(1, 'x'), 1), ('{3}', ROW(2, 'y'), 2);"
psql_run "SELECT pgcolumnar.export_parquet('nest', '$NEST');"
check_text "parquet_schema lists a nested file by its top-level column names (#1893)" \
	"$(q "SELECT string_agg(column_name, ',' ORDER BY ord) FROM (SELECT column_name, row_number() OVER () AS ord FROM pgcolumnar.parquet_schema('$NEST')) s;")" \
	"l,s,n"
check_text "parquet_schema reports a list column as an array type (#1893)" \
	"$(q "SELECT data_type FROM pgcolumnar.parquet_schema('$NEST') WHERE column_name = 'l';")" \
	"bigint[]"
check_text "parquet_schema reports a struct column as a composite with its field types (#1893)" \
	"$(q "SELECT data_type FROM pgcolumnar.parquet_schema('$NEST') WHERE column_name = 's';")" \
	"composite (a bigint, b text)"
check_text "read_parquet reads a nested file with the reported names and types (#1893)" \
	"$(q "SELECT string_agg(l::text || (s).a || (s).b || n, ';' ORDER BY n) FROM pgcolumnar.read_parquet('$NEST') AS t(l bigint[], s nest_s, n integer);")" \
	"{1,2}1x1;{3}2y2"
# ---- nested shapes read_parquet takes leaf by leaf (#1893 review) ------------
# read_parquet binds every leaf by position, so a map, a list of structs, a
# struct holding a list or a struct of a struct still reads: one declared column
# per leaf, an array where the leaf repeats. parquet_schema lists such a column
# one row per leaf, named by its path in the file, so its rows are the column
# list read_parquet needs. A leaf under two repeated levels (a list of lists)
# has no faithful type: read_parquet refuses it (#1917), so it reports NULL.
# craft_parquet_nested.py writes the files, one writer for both twins.
if python3 -I "$(dirname "${BASH_SOURCE[0]}")/craft_parquet_nested.py" "$PGC_WORKDIR"; then
	schema_rows() {	# schema_rows FILE -> name:type,... in file order
		q "SELECT string_agg(column_name || ':' || coalesce(data_type, 'NULL'), ',' ORDER BY ord)
		   FROM (SELECT column_name, data_type, row_number() OVER () AS ord
		         FROM pgcolumnar.parquet_schema('$PGC_WORKDIR/$1.parquet')) s;"
	}
	check_text "parquet_schema lists a map column one row per leaf, each an array (#1893)" \
		"$(schema_rows map)" "m.key_value.key:text[],m.key_value.value:bigint[],n:integer"
	check_text "parquet_schema lists a list of a two-field struct one row per leaf (#1893)" \
		"$(schema_rows los)" "l.list.element.x:bigint[],l.list.element.y:text[],n:integer"
	check_text "parquet_schema lists a list of a one-field struct by its leaf path (#1893)" \
		"$(schema_rows lofs)" "l.list.element.x:bigint[],n:integer"
	check_text "parquet_schema lists a struct holding a list by its leaf path (#1893)" \
		"$(schema_rows shl)" "s.x.list.element:bigint[],n:integer"
	check_text "parquet_schema lists a struct of a struct one scalar row per leaf (#1893)" \
		"$(schema_rows sos)" "s.t.a:bigint,s.t.b:text,n:integer"
	check_text "parquet_schema lists a struct holding a map one row per leaf (#1893)" \
		"$(schema_rows smap)" "s.m.key_value.key:text[],s.m.key_value.value:bigint[],n:integer"
	check_text "parquet_schema reports NULL for the leaves of a list of maps, repeated twice (#1893)" \
		"$(schema_rows lmap)" "l.list.element.key_value.key:NULL,l.list.element.key_value.value:NULL,n:integer"
	check_text "parquet_schema reports NULL for the leaf of a list of lists, repeated twice (#1893)" \
		"$(schema_rows lol)" "l.list.element.list.element:NULL,n:integer"
	check_text "parquet_schema reports a legacy two-level list by its name as an array (#1893)" \
		"$(schema_rows twolevel)" "l:bigint[],n:integer"
	# The rows are the column list: declare one column per row, under its quoted
	# name, with its type, and read every file whose rows all have a type. Not
	# twolevel: read_parquet reads its REQUIRED repeated leaf as empty arrays, an
	# older defect of the reader, not of parquet_schema (#1916).
	read_rows=""
	for f in map los lofs shl sos smap; do
		cols=$(q "SELECT string_agg(quote_ident(column_name) || ' ' || data_type, ', ' ORDER BY ord)
		          FROM (SELECT column_name, data_type, row_number() OVER () AS ord
		                FROM pgcolumnar.parquet_schema('$PGC_WORKDIR/$f.parquet')) s;")
		got=$(q "SELECT string_agg(t::text, ';') FROM pgcolumnar.read_parquet('$PGC_WORKDIR/$f.parquet') AS t($cols);" 2>&1 | head -1)
		read_rows="$read_rows$f=$got "
	done
	check_text "read_parquet reads each nested file with the column list parquet_schema reports (#1893)" \
		"${read_rows% }" \
		'map=("{k,j}","{1,2}",1);({z},{3},2) los=({1},{a},1);("{2,3}","{b,c}",2) lofs=("{1,2}",1);({3},2) shl=("{1,2}",1);({3},2) sos=(1,x,1);(2,y,2) smap=({k},{1},1);({z},{3},2)'
	# A leaf under two repeated levels read into a one-dimensional array used to
	# flatten: [[1], [2, 3]] read as {1,2,3}, with no error (#1917). The bind now
	# refuses it with 42804, as it refuses other mismatches. The reading is the
	# SQLSTATE and message, or the rows that came back instead.
	nested_refusal() {	# nested_refusal SQL -> "SQLSTATE message", or "read: ROWS"
		local out state
		out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
			-d "$PGC_DB" -qtA 2>&1 <<SQLEOF
\\set VERBOSITY verbose
$1;
SQLEOF
)"
		state="$(sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\): \(.*\)/\1 \2/p' <<<"$out" | head -1)"
		if [ -n "$state" ]; then echo "$state"; else echo "read: $(tr '\n' ';' <<<"$out")"; fi
	}
	check_text "read_parquet refuses a list of lists declared as a one-dimensional array (#1917)" \
		"$(nested_refusal "SELECT string_agg(t::text, ';') FROM pgcolumnar.read_parquet('$PGC_WORKDIR/lol.parquet') AS t(l bigint[], n int)")" \
		'42804 Parquet column for array "l" is nested 2 lists deep, but pgcolumnar.read_parquet reads one list level into an array'
	check_text "read_parquet refuses a list of maps declared as one-dimensional arrays (#1917)" \
		"$(nested_refusal "SELECT string_agg(t::text, ';') FROM pgcolumnar.read_parquet('$PGC_WORKDIR/lmap.parquet') AS t(k text[], v bigint[], n int)")" \
		'42804 Parquet column for array "k" is nested 2 lists deep, but pgcolumnar.read_parquet reads one list level into an array'
	psql_run "CREATE TABLE lol_imp (l bigint[], n int) USING pgcolumnar;"
	check_text "import_parquet refuses a list of lists for a one-dimensional array column (#1917)" \
		"$(nested_refusal "SELECT pgcolumnar.import_parquet('lol_imp', '$PGC_WORKDIR/lol.parquet')")|$(q "SELECT count(*) FROM lol_imp;")" \
		'42804 Parquet column for array "l" is nested 2 lists deep, but pgcolumnar.import_parquet reads one list level into an array|0'
	# nullable is the column's own OPTIONAL flag: a REQUIRED list of REQUIRED
	# elements is not nullable, though its leaf has a definition level (the
	# repeated group's). The leaf rule reported l as nullable.
	check_text "parquet_schema reports a REQUIRED list or struct as not nullable, an OPTIONAL list as nullable (#1893)" \
		"$(q "SELECT string_agg(column_name || ':' || nullable::text, ',' ORDER BY ord)
		      FROM (SELECT column_name, nullable, row_number() OVER () AS ord
		            FROM pgcolumnar.parquet_schema('$PGC_WORKDIR/req.parquet')) s;")" \
		"l:false,s:false,o:true"
	# A leaf row's nullable asks whether an OPTIONAL element sits on the leaf's
	# path above its first repeated level: a null there leaves the row without a
	# value. Below that level a null is one missing array element, not a missing
	# row. In leafnull only o's leaves pass that test; m's optional value and l's
	# optional element and x all sit below the repeated group.
	check_text "parquet_schema reports a leaf row nullable only when an OPTIONAL element sits above its first repeated level (#1893)" \
		"$(q "SELECT string_agg(column_name || ':' || nullable::text, ',' ORDER BY ord)
		      FROM (SELECT column_name, nullable, row_number() OVER () AS ord
		            FROM pgcolumnar.parquet_schema('$PGC_WORKDIR/leafnull.parquet')) s;")" \
		"m.key_value.key:false,m.key_value.value:false,l.list.element.x:false,o.key_value.key:true,o.key_value.value:true"
	# The composite text is the field list for CREATE TYPE, so a field name that
	# needs quotes must come quoted. Measure the text, the CREATE TYPE built from
	# it, and the read through that type.
	qtxt=$(q "SELECT data_type FROM pgcolumnar.parquet_schema('$PGC_WORKDIR/qname.parquet') WHERE column_name = 's';")
	qmade=$(psql_run "CREATE TYPE nest_q AS ${qtxt#composite };" 2>&1 | head -1 | sed 's/^ERROR:  //')
	qread=$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At \
		-c "SELECT string_agg((s).\"Weird Name\" || '/' || n, ';' ORDER BY n) FROM pgcolumnar.read_parquet('$PGC_WORKDIR/qname.parquet') AS t(s nest_q, n integer);" \
		2>&1 | head -1 | sed 's/^ERROR:  //')
	check_text "parquet_schema quotes a struct field name that needs quotes, so CREATE TYPE takes the composite text (#1893)" \
		"$qtxt|${qmade:-created}|$qread" \
		'composite ("Weird Name" smallint)|created|1/1;2/2'
else
	for c in "parquet_schema lists a map column one row per leaf, each an array (#1893)" \
		"parquet_schema lists a list of a two-field struct one row per leaf (#1893)" \
		"parquet_schema lists a list of a one-field struct by its leaf path (#1893)" \
		"parquet_schema lists a struct holding a list by its leaf path (#1893)" \
		"parquet_schema lists a struct of a struct one scalar row per leaf (#1893)" \
		"parquet_schema lists a struct holding a map one row per leaf (#1893)" \
		"parquet_schema reports NULL for the leaves of a list of maps, repeated twice (#1893)" \
		"parquet_schema reports NULL for the leaf of a list of lists, repeated twice (#1893)" \
		"parquet_schema reports a legacy two-level list by its name as an array (#1893)" \
		"read_parquet reads each nested file with the column list parquet_schema reports (#1893)" \
		"read_parquet refuses a list of lists declared as a one-dimensional array (#1917)" \
		"read_parquet refuses a list of maps declared as one-dimensional arrays (#1917)" \
		"import_parquet refuses a list of lists for a one-dimensional array column (#1917)" \
		"parquet_schema reports a REQUIRED list or struct as not nullable, an OPTIONAL list as nullable (#1893)" \
		"parquet_schema reports a leaf row nullable only when an OPTIONAL element sits above its first repeated level (#1893)" \
		"parquet_schema quotes a struct field name that needs quotes, so CREATE TYPE takes the composite text (#1893)"; do
		check_skip "$c" "SKIP  $c (craft_parquet_nested.py failed: pyarrow missing?)" "no fixtures"
	done
fi

pgc_summary
