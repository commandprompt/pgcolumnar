#!/usr/bin/env bash
#
# pgColumnar Iceberg table scan (#388 phase 3c). Reads a real Apache Iceberg
# table (pyiceberg-written, committed under test/fixtures/iceberg/warehouse/,
# now including its data/ Parquet files) at its current snapshot, resolving each
# output column name to a schema field id and reading every data file projected
# by those ids. The headline property: a column read by a name the schema gives
# but the data file does NOT physically carry still reads, because Iceberg
# selects by field id, not name. A struct, list or map column declared with a
# scalar type is refused, through iceberg_scan and the foreign-data wrapper,
# instead of reading as null on every row (#1788). And a column after a
# spec-legal type promotion (#1789) reads its pre-promotion files as the current
# type, through iceberg_scan, equality deletes and the pgcolumnar_iceberg foreign
# table.
#
# Usage:  test/iceberg_scan.sh [PG_CONFIG]

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

sqlstate_of() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -qtA 2>&1 <<SQLEOF | sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\).*/\1/p' | head -1
\\set VERBOSITY sqlstate
$1;
SQLEOF
}

# refusal SQL REGEX LABEL -> "<SQLSTATE> (LABEL)" when SQL raised an error whose
# primary message matches REGEX, "<SQLSTATE>: <message>" for any other error, or
# "no error" (then the rows it returned follow, so a red shows what was read).
refusal() {
	local out line state msg
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -qtA 2>&1 <<SQLEOF
\\set VERBOSITY verbose
$1;
SQLEOF
)"
	line="$(sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\): /\1|/p' <<<"$out" | head -1)"
	if [ -z "$line" ]; then echo "no error: $(tr '\n' ' ' <<<"$out")"; return; fi
	state="${line%%|*}"; msg="${line#*|}"
	if grep -qE -- "$2" <<<"$msg"; then echo "$state ($3)"; else echo "$state: $msg"; fi
}

FX="$(dirname "${BASH_SOURCE[0]}")/fixtures/iceberg"
WH="$FX/warehouse"
[ -f "$(ls "$WH"/db/events/data/*/*.parquet 2>/dev/null | head -1)" ] \
	|| pgc_skip fixture "iceberg warehouse data files are missing"
python3 -c 'import json' 2>/dev/null || pgc_skip python "python3 is needed"

# relocate the warehouse so the recorded root != actual root (exercises rebasing)
DEST="$PGC_WORKDIR/relocated"
rm -rf "$DEST"; mkdir -p "$DEST"
cp -r "$WH/db" "$DEST/db"
chmod -R u+rwX "$DEST"
MD="$(ls "$DEST"/db/events/metadata/*.metadata.json | sort | tail -1)"
MDDIR="$(dirname "$MD")"

# premise: the data files actually carry field ids (else id resolution is moot)
DF="$(ls "$DEST"/db/events/data/*/*.parquet | head -1)"
check "premise: the data file carries field ids (id=1,region=2,amount=3)" \
	"$(q "SELECT string_agg(field_id::text, ',' ORDER BY field_id)
	      FROM pgcolumnar.parquet_schema('$DF') WHERE field_id IS NOT NULL")" "1,2,3"

# ---- full read at the current snapshot -------------------------------------
# five rows across two data files (region=eu: 2, region=us: 3)
check "iceberg_scan reads the current snapshot's rows" \
	"$(q "SELECT id || '|' || region || '|' || amount
	      FROM pgcolumnar.iceberg_scan('$MD') AS t(id bigint, region text, amount int)
	      ORDER BY id")" \
	"$(printf '1|eu|10\n2|eu|20\n3|us|30\n4|us|40\n5|us|50')"

# ---- a type refusal names the function that was called (#1871) -------------
# The bind code is shared with read_parquet, and its refusal used to name
# columnar.read_parquet. money is a type no reader binds.
check "iceberg_scan's type refusal names iceberg_scan" \
	"$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -qtA \
		-c "SELECT * FROM pgcolumnar.iceberg_scan('$MD') AS t(id bigint, region text, amount money)" 2>&1 \
		| sed -n 's/^ERROR:  //p' | head -1)" \
	'output column "amount" has type money, which pgcolumnar.iceberg_scan does not support'

# ---- projection + reorder by field id --------------------------------------
check "iceberg_scan projects and reorders by field id (amount, id)" \
	"$(q "SELECT amount || '|' || id
	      FROM pgcolumnar.iceberg_scan('$MD') AS t(amount int, id bigint)
	      ORDER BY id")" \
	"$(printf '10|1\n20|2\n30|3\n40|4\n50|5')"

check "iceberg_scan reads a subset (region only)" \
	"$(q "SELECT string_agg(region, ',' ORDER BY region)
	      FROM pgcolumnar.iceberg_scan('$MD') AS t(region text)")" \
	"eu,eu,us,us,us"

# ---- headline: resolution is by ID, not name -------------------------------
# Rename schema field id 3 (physically "amount") to "amt2" in a copy of the
# metadata, and read AS t(amt2 int). The data file has no column named "amt2";
# only field id 3. A name-based reader would find nothing; an id-based reader
# returns the amount values.
REN="$MDDIR/renamed.metadata.json"
python3 - "$MD" "$REN" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
sid = m["current-schema-id"]
sch = [s for s in m["schemas"] if s["schema-id"] == sid][0]
for f in sch["fields"]:
    if f["id"] == 3:
        f["name"] = "amt2"          # rename in the SCHEMA; the data file is unchanged
json.dump(m, open(sys.argv[2], "w"))
PY
chmod 644 "$REN"
check "a column renamed in the schema still reads by field id (amt2 = amount)" \
	"$(q "SELECT string_agg(amt2::text, ',' ORDER BY amt2)
	      FROM pgcolumnar.iceberg_scan('$REN') AS t(amt2 int)")" \
	"10,20,30,40,50"

# ---- a name absent from the schema is refused ------------------------------
check "an output column not in the schema is refused (42703)" \
	"$(sqlstate_of "SELECT * FROM pgcolumnar.iceberg_scan('$MD') AS t(nosuch int)")" "42703"

# ---- a struct, list or map column declared scalar is refused (#1788) -------
# A copy of the metadata whose current schema also has a struct, a list and a
# map, added after the data files were written (so the files lack them). Their
# field ids (4, 6, 8) are those of the nested fields themselves. Declared with a
# scalar type, each read as null on every row instead of being refused; a file
# that does carry the column read as null the same way. Declared as a record or
# an array it is refused, and now so is every declaration: only top-level
# primitive columns can be read.
NEST="$MDDIR/nested.metadata.json"
python3 - "$MD" "$NEST" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
sid = m["current-schema-id"]
sch = [s for s in m["schemas"] if s["schema-id"] == sid][0]
sch["fields"] += [
    {"id": 4, "name": "st", "required": False, "type": {"type": "struct", "fields": [
        {"id": 5, "name": "a", "required": False, "type": "int"}]}},
    {"id": 6, "name": "li", "required": False, "type": {
        "type": "list", "element-id": 7, "element": "int", "element-required": False}},
    {"id": 8, "name": "mp", "required": False, "type": {
        "type": "map", "key-id": 9, "key": "string", "value-id": 10, "value": "long",
        "value-required": False}},
]
m["last-column-id"] = 10
json.dump(m, open(sys.argv[2], "w"))
PY
chmod 644 "$NEST"
check "iceberg_scan still reads the scalar columns beside nested fields" \
	"$(q "SELECT string_agg(id::text, ',' ORDER BY id)
	      FROM pgcolumnar.iceberg_scan('$NEST') AS t(id bigint)")" "1,2,3,4,5"
check "iceberg_scan refuses a struct column declared text (0A000)" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_scan('$NEST') AS t(id bigint, st text)" \
	   'output column "st" is a struct field' 'struct')" "0A000 (struct)"
check "iceberg_scan refuses a list column declared int4 (0A000)" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_scan('$NEST') AS t(id bigint, li int4)" \
	   'output column "li" is a list field' 'list')" "0A000 (list)"
check "iceberg_scan refuses a map column declared int8 (0A000)" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_scan('$NEST') AS t(id bigint, mp int8)" \
	   'output column "mp" is a map field' 'map')" "0A000 (map)"
psql_run "CREATE SERVER icescan_nest FOREIGN DATA WRAPPER pgcolumnar_iceberg;
	CREATE FOREIGN TABLE icescan_fnest (id bigint, st text)
	  SERVER icescan_nest OPTIONS (metadata_path '$NEST');"
check "a pgcolumnar_iceberg foreign table refuses a struct column declared text (0A000)" \
	"$(refusal "SELECT * FROM icescan_fnest" 'output column "st" is a struct field' 'struct')" \
	"0A000 (struct)"

# deletes (apply position, refuse equality) are covered by iceberg_deletes.sh

# ---- type promotion (#1789) ------------------------------------------------
# The spec's legal promotions -- int -> long, float -> double, decimal(P,S) ->
# decimal(P',S) with P' > P -- leave files written before the promotion with
# the old physical type. The reader widens them to the current schema's type.
# warehouse_promo (gen_promotion_fixture.py) has the old files (INT32, FLOAT,
# decimal(10,2)) alone, mixed with a new file (INT64, DOUBLE, decimal(20,2)),
# and under two equality deletes on the promoted column k: one written before
# the promotion (INT32, k = 0, applies to the old files: id 3) and one after
# (INT64, k = 2: ids 4, 7, 9).
#
# refused_by SQL PHRASE -> the SQLSTATE when SQL is refused with PHRASE in its
# message, "SQLSTATE (another refusal: MESSAGE)" for any other refusal, and "no
# error" when it ran.
refused_by() {
	local out st msg
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -qtA 2>&1 <<SQLEOF
\\set VERBOSITY verbose
$1;
SQLEOF
)"
	st="$(sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\): .*/\1/p' <<<"$out" | head -1)"
	msg="$(sed -n 's/^ERROR:  [0-9A-Z]\{5\}: //p' <<<"$out" | head -1)"
	if [ -z "$st" ]; then echo "no error"
	else case "$msg" in *"$2"*) echo "$st" ;; *) echo "$st (another refusal: $msg)" ;; esac
	fi
}
DESTP="$PGC_WORKDIR/promo"
rm -rf "$DESTP"; mkdir -p "$DESTP"
cp -r "$FX/warehouse_promo/db" "$DESTP/db"
chmod -R u+rwX "$DESTP"
PMD="$DESTP/db/t/metadata"
PDATA="$DESTP/db/t/data"
PROW="id || '|' || k || '|' || f || '|' || d"
PCOLS="AS t(id bigint, k bigint, f double precision, d numeric(20,2))"
OLD_ROWS='1|-2147483648|1.5|1.25
2|-1|-2.25|-3.50
3|0|0.5|0.00
4|2|1024.75|99999999.99
5|2147483647|3.4028234663852886e+38|-0.01
6|7|-0.125|10.10
7|2|2|2.00'
NEW_ROWS='8|5000000000|3.25|123456789012345678.90
9|2|0.1|-1.00'

check "premise: the old files store k, f, d as INT32, FLOAT and decimal(10,2), the new file as INT64, DOUBLE and decimal(20,2)" \
	"$(q "SELECT string_agg(f || ':' || column_name || ':' || data_type, ',' ORDER BY f, field_id)
	      FROM (VALUES ('old'), ('old2'), ('new')) AS v(f),
	           LATERAL pgcolumnar.parquet_schema('$PDATA/' || f || '.parquet')
	      WHERE field_id BETWEEN 2 AND 4")" \
	"new:k:bigint,new:f:double precision,new:d:numeric(20,2),old:k:integer,old:f:real,old:d:numeric(10,2),old2:k:integer,old2:f:real,old2:d:numeric(10,2)"

check "a promoted int -> long column reads the pre-promotion files as bigint" \
	"$(q "SELECT string_agg(id || ':' || k, ',' ORDER BY id)
	      FROM pgcolumnar.iceberg_scan('$PMD/old.metadata.json') AS t(id bigint, k bigint)")" \
	"1:-2147483648,2:-1,3:0,4:2,5:2147483647,6:7,7:2"
check "a promoted float -> double column reads the pre-promotion files as double precision" \
	"$(q "SELECT string_agg(f::text, ',' ORDER BY id)
	      FROM pgcolumnar.iceberg_scan('$PMD/old.metadata.json') AS t(id bigint, f double precision)")" \
	"1.5,-2.25,0.5,1024.75,3.4028234663852886e+38,-0.125,2"
check "a promoted decimal(10,2) -> decimal(20,2) column reads the pre-promotion files" \
	"$(q "SELECT string_agg(d::text, ',' ORDER BY id)
	      FROM pgcolumnar.iceberg_scan('$PMD/old.metadata.json') AS t(id bigint, d numeric(20,2))")" \
	"1.25,-3.50,0.00,99999999.99,-0.01,10.10,2.00"
check_text "old and new files both bind to the promoted types in one snapshot" \
	"$(q "SELECT $PROW FROM pgcolumnar.iceberg_scan('$PMD/mixed.metadata.json') $PCOLS ORDER BY id")" \
	"$OLD_ROWS
$NEW_ROWS"
check "an equality delete on a promoted column applies to old and new files" \
	"$(q "SELECT string_agg(id || ':' || k, ',' ORDER BY id)
	      FROM pgcolumnar.iceberg_scan('$PMD/eq.metadata.json') AS t(id bigint, k bigint)")" \
	"1:-2147483648,2:-1,5:2147483647,6:7,8:5000000000"
check "an equality delete on a promoted column applies when that column is not projected" \
	"$(q "SELECT string_agg(id::text, ',' ORDER BY id)
	      FROM pgcolumnar.iceberg_scan('$PMD/eq.metadata.json') AS t(id bigint)")" \
	"1,2,5,6,8"

# the foreign-data wrapper reads through the same binder
q "CREATE SERVER ice_promo FOREIGN DATA WRAPPER pgcolumnar_iceberg" >/dev/null
q "CREATE FOREIGN TABLE promo_mixed (id bigint, k bigint, f double precision, d numeric(20,2))
   SERVER ice_promo OPTIONS (metadata_path '$PMD/mixed.metadata.json')" >/dev/null
q "CREATE FOREIGN TABLE promo_eq (id bigint, k bigint)
   SERVER ice_promo OPTIONS (metadata_path '$PMD/eq.metadata.json')" >/dev/null
check_text "a pgcolumnar_iceberg foreign table reads old and new files as the promoted types" \
	"$(q "SELECT $PROW FROM promo_mixed ORDER BY id")" \
	"$OLD_ROWS
$NEW_ROWS"
check "a pgcolumnar_iceberg foreign table applies an equality delete on a promoted column" \
	"$(q "SELECT string_agg(id || ':' || k, ',' ORDER BY id) FROM promo_eq")" \
	"1:-2147483648,2:-1,5:2147483647,6:7,8:5000000000"

# Only widening: a column declared narrower than a post-promotion file is still
# the binder's refusal, so a long value can never be truncated into an int.
check "a column declared int over a post-promotion INT64 file is refused (42804)" \
	"$(refused_by "SELECT * FROM pgcolumnar.iceberg_scan('$PMD/mixed.metadata.json') AS t(k int)" \
	   "has a physical type incompatible with output column \"k\"")" "42804"
check "a column declared real over a post-promotion DOUBLE file is refused (42804)" \
	"$(refused_by "SELECT * FROM pgcolumnar.iceberg_scan('$PMD/mixed.metadata.json') AS t(f real)" \
	   "has a physical type incompatible with output column \"f\"")" "42804"
# and only a plain integer: dt is an Iceberg date, stored as INT32 like an int,
# and no promotion makes it a long
check "a date column declared bigint is refused, not widened (42804)" \
	"$(refused_by "SELECT * FROM pgcolumnar.iceberg_scan('$PMD/old.metadata.json') AS t(dt bigint)" \
	   "has a physical type incompatible with output column \"dt\"")" "42804"

# read_parquet by field id is the same binder, so it widens too; the positional
# form is not a schema-evolution read and keeps its exact type map
check "read_parquet by field id reads an INT32 column as bigint" \
	"$(q "SELECT string_agg(k::text, ',' ORDER BY k)
	      FROM pgcolumnar.read_parquet('$PDATA/old2.parquet', ARRAY[2]) AS t(k bigint)")" "2,7"
check "positional read_parquet still refuses bigint over an INT32 column (42804)" \
	"$(refused_by "SELECT * FROM pgcolumnar.read_parquet('$PDATA/old2.parquet')
	                 AS t(id bigint, k bigint, f real, d numeric(10,2), dt date)" \
	   "physical type is not compatible with target column \"k\"")" "42804"

# ---- privilege -------------------------------------------------------------
# USAGE on the schema, so the refusal is the FILE gate's: without it the role was
# refused by "permission denied for schema pgcolumnar", also 42501, and with the
# file gate removed entirely this arm still passed (measured).
psql_run "DROP ROLE IF EXISTS icescan_unpriv; CREATE ROLE icescan_unpriv LOGIN;
	GRANT USAGE ON SCHEMA pgcolumnar TO icescan_unpriv;"
check "an unprivileged role is refused (42501)" \
	"$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U icescan_unpriv -d "$PGC_DB" \
		-qtA 2>&1 <<SQL | sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\).*/\1/p' | head -1
\\set VERBOSITY sqlstate
SELECT * FROM pgcolumnar.iceberg_scan('$MD') AS t(id bigint);
SQL
)" "42501"

# The positive control the deny arm needs: a deny alone is satisfied by a gate
# that refuses everyone but the superuser, which would lock out every
# pg_read_server_files member, and that passed this suite whole (measured). A
# non-superuser holding the role must read all five rows.
psql_run "DROP ROLE IF EXISTS icescan_reader; CREATE ROLE icescan_reader LOGIN;
	GRANT pg_read_server_files TO icescan_reader;
	GRANT USAGE ON SCHEMA pgcolumnar TO icescan_reader;"
check "a pg_read_server_files member who is not superuser may scan" \
	"$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U icescan_reader -d "$PGC_DB" \
		-qtA -c "SELECT count(*) FROM pgcolumnar.iceberg_scan('$MD') AS t(id bigint);" 2>&1 | tail -1)" "5"

pgc_summary
