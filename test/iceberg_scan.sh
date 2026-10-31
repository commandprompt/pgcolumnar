#!/usr/bin/env bash
#
# pgColumnar Iceberg table scan (#388 phase 3c). Reads a real Apache Iceberg
# table (pyiceberg-written, committed under test/fixtures/iceberg/warehouse/,
# now including its data/ Parquet files) at its current snapshot, resolving each
# output column name to a schema field id and reading every data file projected
# by those ids. The headline property: a column read by a name the schema gives
# but the data file does NOT physically carry still reads, because Iceberg
# selects by field id, not name.
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

# deletes (apply position, refuse equality) are covered by iceberg_deletes.sh

# ---- metadata and manifest shapes other writers produce (#1815, #1825) -----
# qe SQL -> the rows, or the server's ERROR line, so a failed read shows its cause
qe() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "$1" 2>&1 | sed -n '/^ERROR:/{p;q};p'
}
# refusal_of SQL PHRASE LABEL -> "<SQLSTATE><LABEL>" when SQL raised with PHRASE in its
# message, else "<SQLSTATE>: <message>", or "no error (<first line>)"
refusal_of() {
	local out state msg
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -qtA 2>&1 <<SQLEOF
\\set VERBOSITY verbose
$1;
SQLEOF
)"
	state="$(sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\): .*/\1/p' <<<"$out" | head -1)"
	msg="$(sed -n 's/^ERROR:  [0-9A-Z]\{5\}: //p' <<<"$out" | head -1)"
	if [ -z "$state" ]; then echo "no error ($(head -1 <<<"$out"))"
	elif grep -qF "$2" <<<"$msg"; then echo "$state$3"
	else echo "$state: $msg"; fi
}
# the variants: gzip copies of the current metadata.json, a gzip bomb, a cut-short gzip
# stream, a table whose manifest spells file_format "parquet", and a table whose location
# is s3:// while its recorded paths are s3a://. Written next to the relocated table.
VARIANTS="$(python3 "$FX/gen_iceberg_variants.py" "$DEST" 2>&1)"
var() { sed -n "s/^$1=//p" <<<"$VARIANTS"; }
# the path, or a value no file can have, so a missing variant fails its arm loudly
vpath() { local p; p="$(var "$1")"; echo "${p:-no variant $1: $VARIANTS}"; }
SNAPCOLS="snapshot_id || '|' || coalesce(parent_snapshot_id::text, '-') || '|' || sequence_number || '|' || manifest_list"
PLAIN_SNAP="$(q "SELECT $SNAPCOLS FROM pgcolumnar.iceberg_current_snapshot('$MD')")"
ROWS5="$(printf '1|eu|10\n2|eu|20\n3|us|30\n4|us|40\n5|us|50')"
check "iceberg_current_snapshot reads a .gz.metadata.json file as it reads the plain file" \
	"$(qe "SELECT $SNAPCOLS FROM pgcolumnar.iceberg_current_snapshot('$(vpath gz_infix)')")" \
	"${PLAIN_SNAP:-no snapshot from the plain file}"
check "iceberg_current_snapshot reads a .metadata.json.gz file as it reads the plain file" \
	"$(qe "SELECT $SNAPCOLS FROM pgcolumnar.iceberg_current_snapshot('$(vpath gz_suffix)')")" \
	"${PLAIN_SNAP:-no snapshot from the plain file}"
check "iceberg_scan reads the rows of a table whose metadata is .gz.metadata.json" \
	"$(qe "SELECT id || '|' || region || '|' || amount
	       FROM pgcolumnar.iceberg_scan('$(vpath gz_infix)') AS t(id bigint, region text, amount int)
	       ORDER BY id")" "$ROWS5"
check "iceberg_scan reads the rows of a table whose metadata is .metadata.json.gz" \
	"$(qe "SELECT id || '|' || region || '|' || amount
	       FROM pgcolumnar.iceberg_scan('$(vpath gz_suffix)') AS t(id bigint, region text, amount int)
	       ORDER BY id")" "$ROWS5"
# 64 MiB + 1 byte once inflated, from about 64 KiB on disk: the cap holds on the
# inflated size, so a small file cannot make the backend allocate without bound
check "a gzip metadata file that inflates past the 64 MiB cap is refused (54000)" \
	"$(refusal_of "SELECT * FROM pgcolumnar.iceberg_current_snapshot('$(vpath gz_bomb)')" \
	   "inflates past" " past the cap")" "54000 past the cap"
check "a cut-short gzip metadata file is refused as corrupt gzip (XX001)" \
	"$(refusal_of "SELECT * FROM pgcolumnar.iceberg_current_snapshot('$(vpath gz_trunc)')" \
	   "gzip" " naming gzip")" "XX001 naming gzip"
# gzip allows several members back to back (cat a.gz b.gz); the text is all of them
check "iceberg_current_snapshot reads a gzip metadata file of two members as it reads the plain file" \
	"$(qe "SELECT $SNAPCOLS FROM pgcolumnar.iceberg_current_snapshot('$(vpath gz_multi)')")" \
	"${PLAIN_SNAP:-no snapshot from the plain file}"
# zero bytes after the gzip stream are padding, not a second member
check "iceberg_current_snapshot reads a gzip metadata file followed by zero padding" \
	"$(qe "SELECT $SNAPCOLS FROM pgcolumnar.iceberg_current_snapshot('$(vpath gz_padded)')")" \
	"${PLAIN_SNAP:-no snapshot from the plain file}"
# az:// counts as remote, but no object-store module reads it. The refusal names the
# URL as one it cannot read remotely (0A000), not "could not open file" as for a
# local path. Its wording depends on whether the object-store module is installed.
check "an az:// metadata path is refused as a remote URL it cannot read (0A000)" \
	"$(refusal_of "SELECT * FROM pgcolumnar.iceberg_current_snapshot('az://b/t/metadata/v1.metadata.json')" \
	   'reading "az://' " remote refusal")" "0A000 remote refusal"
check "iceberg_scan reads a data file whose file_format is lowercase parquet" \
	"$(qe "SELECT id || '|' || region || '|' || amount
	       FROM pgcolumnar.iceberg_scan('$(vpath lower)') AS t(id bigint, region text, amount int)
	       ORDER BY id")" "$ROWS5"
check "iceberg_scan reads a table whose location is s3:// and whose recorded paths are s3a://" \
	"$(qe "SELECT id || '|' || region || '|' || amount
	       FROM pgcolumnar.iceberg_scan('$(vpath mixed)') AS t(id bigint, region text, amount int)
	       ORDER BY id")" "$ROWS5"

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
