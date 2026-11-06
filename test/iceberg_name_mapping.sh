#!/usr/bin/env bash
#
# pgColumnar Iceberg name mapping (#388). An Iceberg data file written outside
# Iceberg (a migrated dataset, an "imported data file") carries no Parquet field
# ids. iceberg_scan reads it by binding its columns by name through the table's
# schema.name-mapping.default property. The fixture is hand-crafted
# (test/fixtures/iceberg/warehouse_nm, gen_name_mapping_fixture.py): a real
# id-less pyarrow data file plus hand-encoded manifests and metadata carrying
# the mapping. The hand-derived oracle (all five rows) is cross-checked against
# pyiceberg and DuckDB by crosscheck_nm.py.
#
# Usage:  test/iceberg_name_mapping.sh [PG_CONFIG]

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

errmsg_of() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -qtA 2>&1 <<SQLEOF | sed -n 's/^ERROR:  //p' | head -1
$1;
SQLEOF
}

# the full error text (message + detail + hint), for a cause carried in a HINT
fullerr_of() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -qtA 2>&1 <<SQLEOF | tr '\n' ' '
$1;
SQLEOF
}

# Backends killed by a signal, from the server log: a number, or nothing when the
# log can't be read. A fresh connection answers again once crash recovery is over,
# so "SELECT 1" alone passed with the duplicate-name refusal turned into an abort()
# and the check run 3 s later; each survival check also requires this count to be
# where it was when its section began.
crashes() {
	local _n
	_n="$(pgc_pg "grep -c 'terminated by signal' '$PGC_LOGFILE'" 2>/dev/null)"
	case "$_n" in '' | *[!0-9]*) ;; *) echo "$_n" ;; esac
}
survived() {	# survived BEFORE -> "1" when the server answers and no backend died since
	local _up _now
	_up="$(q 'SELECT 1')"; _now="$(crashes)"
	if ! [[ "$1" =~ ^[0-9]+$ && "$_now" =~ ^[0-9]+$ ]]; then echo "no (crash count unreadable)"
	elif [ "$_up" = 1 ] && [ "$_now" = "$1" ]; then echo 1
	else echo "no ([$_up], $(( _now - $1 )) backend crashes)"; fi
	# After a crash, let recovery finish before the next section reads, so its
	# checks red on their own arms and not as collateral of this one.
	if [ "$_up" != 1 ]; then
		local _i
		for _i in $(seq 1 60); do [ "$(q 'SELECT 1')" = 1 ] && break; sleep 1; done
	fi
}

# A C file with its comments blanked out, so a source check reads code and not a
# comment that quotes it: with the bound dropped and its old text kept in a comment,
# grep over the raw file still found it. Prints nothing when the file can't be read.
code_of() {
	python3 - "$1" <<'PY_CODE' 2>/dev/null
import re, sys
s = open(sys.argv[1]).read()
s = re.sub(r"/\*.*?\*/", " ", s, flags=re.S)
print(re.sub(r"//[^\n]*", "", s))
PY_CODE
}

FX="$(dirname "${BASH_SOURCE[0]}")/fixtures/iceberg"
WHN="$FX/warehouse_nm"
[ -f "$WHN/db/t/metadata/nmapply.metadata.json" ] || pgc_skip fixture "name-mapping fixtures are missing"
python3 -c 'import json' 2>/dev/null || pgc_skip python "python3 is needed"

# relocate so the recorded root (/tmp/pgc_ice_nm) != actual root (rebasing)
DEST="$PGC_WORKDIR/nm"
rm -rf "$DEST"; mkdir -p "$DEST"
cp -r "$WHN/db" "$DEST/db"
chmod -R u+rwX "$DEST"
MDIR="$DEST/db/t/metadata"
# The fixture's own rows (gen_name_mapping_fixture.py ROWS), written in three row
# groups of 2, 2 and 1. Value checks compare every row against these, not the
# first row: with text columns read as null past the first row group, each value
# check read one row and passed, and so did the whole suite.
ROWS_ALL="1|eu|10,2|eu|20,3|us|30,4|us|40,5|us|50"
REGIONS_ALL="1:eu,2:eu,3:us,4:us,5:us"

# premise: the data file carries NO field ids (the whole point of name mapping)
check "premise: the data file carries no field ids" \
	"$(q "SELECT count(*) FROM pgcolumnar.parquet_schema('$DEST/db/t/data/data.parquet')
	      WHERE field_id IS NOT NULL")" "0"

# ---- name mapping binds an id-less file -------------------------------------
BEFORE="$(crashes)"
check "an id-less data file reads by name mapping (all 5 rows)" \
	"$(q "SELECT count(*) FROM pgcolumnar.iceberg_scan('$MDIR/nmapply.metadata.json')
	      AS t(id bigint, region text, amount int)")" "5"
check "the mapped values are correct" \
	"$(q "SELECT string_agg(id || '|' || region || '|' || amount, ',' ORDER BY id)
	      FROM pgcolumnar.iceberg_scan('$MDIR/nmapply.metadata.json')
	        AS t(id bigint, region text, amount int)")" "$ROWS_ALL"
# the mapping supplies the id->file-column binding; the schema field name may
# differ from the file's column name (a rename after the file was written).
# Output name -> schema id -> mapping name -> file column must all chain.
check "a renamed schema column still reads via the mapping name" \
	"$(q "SELECT string_agg(ident::text, ',' ORDER BY ident)
	      FROM pgcolumnar.iceberg_scan('$MDIR/nmrename.metadata.json')
	        AS t(ident bigint, region text, amount int)")" "1,2,3,4,5"
# id 1 has two mapping names; the file uses the second ("rid")
check "an alias name in the mapping resolves the file column" \
	"$(q "SELECT count(*) FROM pgcolumnar.iceberg_scan('$MDIR/nmalias.metadata.json')
	      AS t(id bigint, region text, amount int) WHERE id IN (1,2,3,4,5)")" "5"
# projecting a subset still binds each requested column by name
check "name mapping applies under column projection (id, amount)" \
	"$(q "SELECT string_agg(amount::text, ',' ORDER BY amount)
	      FROM pgcolumnar.iceberg_scan('$MDIR/nmsubset.metadata.json') AS t(id bigint, amount int)")" \
	"10,20,30,40,50"
check "backend still up after the name-mapping reads" "$(survived "$BEFORE")" "1"

# ---- refusal arms -----------------------------------------------------------
BEFORE="$(crashes)"
# an id-less file with NO name mapping is refused (there is no spec-defined
# positional fallback), and the message must name the property so the operator
# knows the fix
check "an id-less file with no name mapping is refused (22023)" \
	"$(sqlstate_of "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/nmnomap.metadata.json')
	                AS t(id bigint, region text, amount int)")" "22023"
check "...and the refusal names schema.name-mapping.default" \
	"$(fullerr_of "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/nmnomap.metadata.json')
	              AS t(id bigint, region text, amount int)" | grep -c "schema.name-mapping.default")" "1"
# a mapping that lists one name under two field ids violates uniqueness
check "a duplicate name in the mapping is refused (XX001)" \
	"$(sqlstate_of "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/nmdup.metadata.json')
	                AS t(id bigint, region text, amount int)")" "XX001"
# a mapping field-id beyond int32 would truncate and alias a real id
check "a mapping field-id beyond int32 is refused (XX001)" \
	"$(sqlstate_of "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/nmbigid.metadata.json')
	                AS t(id bigint, region text, amount int)")" "XX001"
check "backend still up after the name-mapping refusals" "$(survived "$BEFORE")" "1"

# ---- Column Projection null-fill (an absent field id reads as null) ---------
BEFORE="$(crashes)"
# the mapping omits "amount"; the file physically has an amount column, but with
# no mapping entry there is no binding, so amount reads as null (rung 4), while
# id and region return real data -- not a whole-query error
check "a projected column absent from the mapping reads null, not an error" \
	"$(q "SELECT count(*) FROM pgcolumnar.iceberg_scan('$MDIR/nmmissing.metadata.json')
	      AS t(id bigint, region text, amount int)")" "5"
check "the unmapped column is null and the mapped columns are real" \
	"$(q "SELECT string_agg(id || ':' || region, ',' ORDER BY id)
	      FROM pgcolumnar.iceberg_scan('$MDIR/nmmissing.metadata.json')
	        AS t(id bigint, region text, amount int) WHERE amount IS NULL")" "$REGIONS_ALL"
# the headline case: an imported file written before "amount" was added to the
# schema. The file lacks the column entirely; it reads as null.
check "a column added after the file was written reads null (schema evolution)" \
	"$(q "SELECT string_agg(id::text, ',' ORDER BY id)
	      FROM pgcolumnar.iceberg_scan('$MDIR/nmevolve.metadata.json')
	        AS t(id bigint, region text, amount int) WHERE amount IS NULL")" "1,2,3,4,5"
check "...and its other columns still carry real data" \
	"$(q "SELECT string_agg(id || ':' || region, ',' ORDER BY id)
	      FROM pgcolumnar.iceberg_scan('$MDIR/nmevolve.metadata.json')
	        AS t(id bigint, region text, amount int)")" "$REGIONS_ALL"
check "backend still up after the null-fill reads" "$(survived "$BEFORE")" "1"

# ---- the name cap, and the allocation that opens for it (#711) --------------
# ICE_MAX_NAME_MAPPING bounds the NAME count, and the entry count is not an
# upper bound on it: one entry carries a whole list of names, so a single entry
# can drive the count to the cap on its own. These fixtures are built that way
# on purpose -- one entry, many names -- because that is the shape where the
# array's opening capacity (taken from the entry count) and its real ceiling
# (the cap) are furthest apart, and it is the shape the growth path has to
# handle correctly.
CAP=100000
BEFORE="$(crashes)"
python3 - "$MDIR" "$CAP" <<'PY_CAP'
import json, sys
d, cap = sys.argv[1], int(sys.argv[2])
base = json.load(open(d + "/nmapply.metadata.json"))
real = json.loads(base["properties"]["schema.name-mapping.default"])

def write(tag, extra):
    md = json.loads(json.dumps(base))
    # one entry carrying `extra` unique filler names, beside the real mapping
    filler = {"field-id": 1, "names": ["f%d" % i for i in range(extra)]}
    md["properties"]["schema.name-mapping.default"] = json.dumps(real + [filler])
    open(d + "/" + tag + ".metadata.json", "w").write(json.dumps(md))

# the real mapping already contributes 3 names, so `cap - 3` reaches exactly the
# cap and `cap - 2` is the first document over it
write("nmcap_at", cap - 3)
write("nmcap_over", cap - 2)
PY_CAP

# At the cap: accepted, and the mapping still binds. A fixture that errored here
# would make the over-cap arm below vacuous, since both would just be errors.
check "a mapping with exactly the cap in names is accepted" \
	"$(q "SELECT count(*) FROM pgcolumnar.iceberg_scan('$MDIR/nmcap_at.metadata.json')
	      AS t(id bigint, region text, amount int)")" "5"

# One over: refused, by SQLSTATE and not by message text. 54000 comes from
# ERRCODE_PROGRAM_LIMIT_EXCEEDED and nothing else on this path returns it.
check "one name over the cap is refused" \
	"$(sqlstate_of "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/nmcap_over.metadata.json')
	                AS t(id bigint, region text, amount int)")" "54000"
check "...and the refusal names the cap" \
	"$(errmsg_of "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/nmcap_over.metadata.json')
	              AS t(id bigint, region text, amount int)" | grep -c "more than $CAP names")" "1"

# The opening allocation is sized by the cap, not by the count the file declares.
# This is a source check because it is not observable from SQL: the surplus is
# palloc'd and never written, and untouched pages never become resident, so it
# costs address space rather than memory and no query can see it. Measured: a
# 6.4 MB mapping moved peak RSS by 28 kB out of 1,196 MB with and without the
# fix. (That 1,196 MB is a separate and much larger defect, filed as #731; it is
# not this one, and this arm deliberately does not claim it.)
ICE_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/src/columnar_iceberg.c"
check "the opening capacity is bounded by the cap, not by the declared count" \
	"$(code_of "$ICE_SRC" | grep -c 'cap = Max(Min(ne, (uint32) ICE_MAX_NAME_MAPPING), 1)')" "1"

check "backend still up after the cap reads" "$(survived "$BEFORE")" "1"

pgc_summary
