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
# instead of reading as null on every row (#1788).
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
