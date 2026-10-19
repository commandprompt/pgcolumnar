#!/usr/bin/env bash
#
# pgColumnar Iceberg data-file resolver (#388 phase 3b). Chains a real Apache
# Iceberg table (written by pyiceberg, committed under
# test/fixtures/iceberg/warehouse/) from its metadata.json through the manifest
# list and manifests to the live data files at the current snapshot, and asserts
# them against pyiceberg's own view (expected_files.json). The warehouse is
# committed at a fixed recorded root and copied elsewhere at test time, so the
# path rebasing (recorded location -> actual location) is exercised for real.
#
# Usage:  test/iceberg_data_files.sh [PG_CONFIG]

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
[ -d "$WH/db/events/metadata" ] || pgc_skip fixture "iceberg warehouse fixture is missing"
python3 -c 'import json, zlib' 2>/dev/null || pgc_skip python "python3 (json, zlib) is needed"

# copy the committed warehouse somewhere else, so the recorded root baked into
# the fixture differs from where it now sits: that difference is the rebasing.
DEST="$PGC_WORKDIR/relocated"
rm -rf "$DEST"; mkdir -p "$DEST"
cp -r "$WH/db" "$DEST/db"
chmod -R u+rwX "$DEST"
MD="$(ls "$DEST"/db/events/metadata/*.metadata.json | sort | tail -1)"

# ---- data files match pyiceberg's oracle -----------------------------------
# oracle rows "record_count|region|basename", sorted
ORACLE="$(python3 - "$FX/warehouse/expected_files.json" <<'PY'
import json, sys
o = json.load(open(sys.argv[1]))
rows = ["%d|%s|%s" % (r["record_count"], r["region"], r["file_basename"])
        for r in o["data_files"]]
print("\n".join(sorted(rows)))
PY
)"
DECODED="$(q "SELECT record_count || '|' || split_part(partition,'=',2) || '|' ||
                     (regexp_match(file_path,'[^/]+\$'))[1]
              FROM pgcolumnar.iceberg_data_files('$MD') ORDER BY 1")"
check "data files == pyiceberg oracle (record_count|region|basename)" \
	"$DECODED" "$ORACLE"

check "every data file is PARQUET" \
	"$(q "SELECT bool_and(file_format='PARQUET') FROM pgcolumnar.iceberg_data_files('$MD')")" "t"

# ---- rebasing proof: paths point at the ACTUAL location, not the recorded --
# the fixture's recorded root is /tmp/pgc_ice_wh; the resolver must return paths
# under the actual (realpath-resolved) location, never the recorded root.
AROOT="$(realpath "$DEST/db/events")"
check "returned paths are rebased under the actual location" \
	"$(q "SELECT bool_and(file_path LIKE '$AROOT/%') FROM pgcolumnar.iceberg_data_files('$MD')")" "t"
check "no returned path leaks the recorded root" \
	"$(q "SELECT count(*) FROM pgcolumnar.iceberg_data_files('$MD')
	      WHERE file_path LIKE '/tmp/pgc_ice_wh/%'")" "0"

# ---- a path outside the table location is refused, NOT read ----------------
# The arbitrary-file-read boundary. A tampered metadata.json can point the
# manifest-list anywhere; the resolver must refuse before opening it. Three
# payloads, each a distinct bypass of a naive prefix check:
#   (a) a plainly foreign absolute path (no shared prefix)
#   (b) a ".." traversal that shares the location prefix but escapes when
#       re-rooted -- the boundary must canonicalize and reject it
#   (c) a sibling that shares the byte prefix but is not on a path boundary
# All must be 22023 ("not under / escapes the table location"). Critically NOT
# XX001 (bad Avro magic): an XX001 means the backend OPENED and read the file,
# i.e. the boundary was bypassed. The traversal target is a real server file
# (/etc/hostname) precisely so a bypass reads it and returns XX001, making the
# regression visible.
LOC="$(python3 -c "import json;print(json.load(open('$MD'))['location'].replace('file://',''))")"
# the tampered variants must sit in the REAL table's metadata dir, so the
# resolver derives the same actual location (<dir>/.. of the metadata file) and a
# symlink planted there resolves under it -- writing them elsewhere would derive a
# wrong actual root and mask the boundary under a "file not found".
MDDIR="$(dirname "$MD")"
# After a survival check, wait (up to 60s) until the server accepts connections
# again. A refusal that crashes the backend fails its own arm and the survival check;
# without the wait, whether the NEXT arm also failed on the recovery depended on how
# long the python step before it took (#1341).
recovered() {
	local i
	for i in $(seq 240); do
		[ "$(q 'SELECT 1')" = "1" ] && return 0
		sleep 0.25
	done
}

esc_case() {  # $1 payload manifest-list, writes a metadata variant, echoes its path
	local payload="$1" out="$MDDIR/esc_$2.metadata.json"
	python3 - "$MD" "$out" "$payload" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
m["snapshots"][-1]["manifest-list"] = sys.argv[3]
json.dump(m, open(sys.argv[2], "w"))
PY
	chmod 644 "$out"; echo "$out"
}
check "a foreign absolute manifest-list is refused (22023)" \
	"$(sqlstate_of "SELECT * FROM pgcolumnar.iceberg_data_files('$(esc_case "file:///etc/passwd" a)')")" "22023"
check "a .. traversal manifest-list is refused, not read (22023, not XX001)" \
	"$(sqlstate_of "SELECT * FROM pgcolumnar.iceberg_data_files('$(esc_case "file://$LOC/../../../../../../../etc/hostname" b)')")" "22023"
check "a sibling-prefix manifest-list is refused (22023)" \
	"$(sqlstate_of "SELECT * FROM pgcolumnar.iceberg_data_files('$(esc_case "file://${LOC}EVIL/x.avro" c)')")" "22023"
# (d) a SYMLINK under the location that targets a file outside it. The attacker
# writes the warehouse, so they can plant a symlink; a lexical check passes it
# (its own path is under the location) and open() follows it out. The boundary
# must resolve symlinks (realpath) and reject. Target a real EXISTING file
# outside the location (not a fixed system path like /etc/hostname, which is
# absent on some hosts -- a broken symlink resolves to ENOENT and the boundary
# would refuse with 58P01 before the containment check, masking this arm) so
# realpath resolves and the containment refusal (22023) is what is exercised.
OUTSIDE="$PGC_WORKDIR/outside_symlink_target"
printf 'outside\n' > "$OUTSIDE"
ln -sf "$OUTSIDE" "$DEST/db/events/metadata/sneaky.avro"
check "a symlink under the location targeting outside is refused (22023, not XX001)" \
	"$(sqlstate_of "SELECT * FROM pgcolumnar.iceberg_data_files('$(esc_case "file://$LOC/metadata/sneaky.avro" d)')")" "22023"
check "backend still up after the traversal attempts" "$(q 'SELECT 1')" "1"
recovered

# ---- deletes are refused, not ignored --------------------------------------
# pyiceberg 0.11.1 cannot write merge-on-read deletes, so craft the deny input:
# a table whose current snapshot's manifest-list carries a delete manifest
# (manifest_file.content = 1). The resolver must refuse (0A000) on seeing it,
# without opening the delete manifest.
DT="$PGC_WORKDIR/deltbl/db/t"
mkdir -p "$DT/metadata"
python3 - "$DT" <<'PY'
import json, sys, zlib, os
DT = sys.argv[1]
def varint(u):
    out = bytearray()
    while True:
        b = u & 0x7f; u >>= 7
        out.append(b | 0x80 if u else b)
        if not u: break
    return bytes(out)
def zz(n): return varint((n << 1) ^ (n >> 63) & 0xFFFFFFFFFFFFFFFF) if n < 0 else varint(n << 1)
def s(bs): return zz(len(bs)) + bs
# a reduced manifest_file schema carrying exactly the fields the reader projects,
# with content declared so a delete manifest (content=1) is expressible.
schema = json.dumps({"type":"record","name":"manifest_file","fields":[
    {"name":"manifest_path","type":"string"},
    {"name":"manifest_length","type":"long"},
    {"name":"partition_spec_id","type":"int"},
    {"name":"content","type":"int"},
    {"name":"sequence_number","type":"long"},
    {"name":"min_sequence_number","type":"long"},
    {"name":"added_snapshot_id","type":"long"},
    {"name":"added_files_count","type":"int"},
    {"name":"existing_files_count","type":"int"},
    {"name":"deleted_files_count","type":"int"},
    {"name":"added_rows_count","type":"long"},
    {"name":"existing_rows_count","type":"long"},
    {"name":"deleted_rows_count","type":"long"}]}).encode()
# one manifest_file record: content = 1 (a delete manifest)
rec  = s(b"file:///tmp/pgc_ice_wh/db/t/metadata/delete-m0.avro")  # manifest_path
rec += zz(100)          # manifest_length
rec += zz(0)            # partition_spec_id
rec += zz(1)            # content = 1  <-- the delete manifest
rec += zz(1) + zz(1)    # sequence_number, min_sequence_number
rec += zz(1)            # added_snapshot_id
rec += zz(1) + zz(0) + zz(0)   # added/existing/deleted files_count
rec += zz(1) + zz(0) + zz(0)   # added/existing/deleted rows_count
sync = b"\x00" * 16
meta = zz(2) + s(b"avro.schema") + s(schema) + s(b"avro.codec") + s(b"null") + zz(0)
ocf  = b"Obj\x01" + meta + sync
ocf += zz(1) + zz(len(rec)) + rec + sync        # one block, null codec
open(os.path.join(DT, "metadata", "delete-list.avro"), "wb").write(ocf)
# a metadata.json whose current snapshot points at that crafted delete-list
md = {
    "format-version": 2,
    "location": "file:///tmp/pgc_ice_wh/db/t",
    "current-snapshot-id": 1,
    "snapshots": [{
        "snapshot-id": 1, "sequence-number": 1, "timestamp-ms": 0,
        "manifest-list": "file:///tmp/pgc_ice_wh/db/t/metadata/delete-list.avro",
        "summary": {"operation": "overwrite"}, "schema-id": 0}],
}
open(os.path.join(DT, "metadata", "v1.metadata.json"), "w").write(json.dumps(md))
PY
check "a snapshot with a delete manifest is refused (feature_not_supported 0A000)" \
	"$(sqlstate_of "SELECT * FROM pgcolumnar.iceberg_data_files('$DT/metadata/v1.metadata.json')")" "0A000"
check "backend still up after the delete refusal" "$(q 'SELECT 1')" "1"
recovered

# ---- a delete ENTRY inside a data manifest is refused too (#1341 item 49) --
# The arm above stops at the manifest LIST. A data manifest (content 0 in the
# list) can still hold an entry the lister must not return: a position or
# equality delete file (entry content 1 or 2), or a removed file (status 2,
# DELETED). Returning a removed file would read rows a commit took out. Each
# variant below is the same crafted table with one (status, content) pair; the
# (1, 0) control is listed, so a refusal is the entry's doing, not a fault in
# the crafted Avro.
entry_tbl() {  # $1 name, $2 status, $3 content; echoes the metadata path
	local d="$PGC_WORKDIR/entry_$1/db/t"
	mkdir -p "$d/metadata"
	python3 - "$d" "$2" "$3" <<'PY'
import json, sys, os
D, STATUS, CONTENT = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
LOC = "file:///tmp/pgc_ice_wh/db/t"
def zz(n):
    u = (n << 1) ^ (n >> 63) if n < 0 else (n << 1)
    out = bytearray()
    while True:
        b = u & 0x7f; u >>= 7
        out.append(b | 0x80 if u else b)
        if not u: break
    return bytes(out)
def s(bs): return zz(len(bs)) + bs
def ocf(schema, recs):
    sync = b"\x00" * 16
    meta = zz(2) + s(b"avro.schema") + s(json.dumps(schema).encode()) + s(b"avro.codec") + s(b"null") + zz(0)
    out = b"Obj\x01" + meta + sync
    for r in recs: out += zz(1) + zz(len(r)) + r + sync
    return out
entry = {"type": "record", "name": "manifest_entry", "fields": [
    {"name": "status", "type": "int"},
    {"name": "sequence_number", "type": ["null", "long"]},
    {"name": "data_file", "type": {"type": "record", "name": "df", "fields": [
        {"name": "content", "type": "int"},
        {"name": "file_path", "type": "string"},
        {"name": "file_format", "type": "string"},
        {"name": "record_count", "type": "long"},
        {"name": "file_size_in_bytes", "type": "long"}]}}]}
ent = zz(STATUS) + (zz(1) + zz(1)) + zz(CONTENT) + s(f"{LOC}/data/f.parquet".encode()) + \
      s(b"PARQUET") + zz(5) + zz(1000)
open(os.path.join(D, "metadata", "dm.avro"), "wb").write(ocf(entry, [ent]))
mfile = {"type": "record", "name": "manifest_file", "fields": [
    {"name": "manifest_path", "type": "string"},
    {"name": "manifest_length", "type": "long"},
    {"name": "partition_spec_id", "type": "int"},
    {"name": "content", "type": "int"},
    {"name": "sequence_number", "type": ["null", "long"]},
    {"name": "min_sequence_number", "type": "long"},
    {"name": "added_snapshot_id", "type": "long"}]}
mf = s(f"{LOC}/metadata/dm.avro".encode()) + zz(1000) + zz(0) + zz(0) + \
     (zz(1) + zz(1)) + zz(1) + zz(1)        # content 0: a DATA manifest
open(os.path.join(D, "metadata", "ml.avro"), "wb").write(ocf(mfile, [mf]))
md = {"format-version": 2, "location": LOC, "current-snapshot-id": 1,
      "current-schema-id": 0,
      "schemas": [{"schema-id": 0, "type": "struct", "fields": [
          {"id": 1, "name": "id", "required": False, "type": "long"}]}],
      "snapshots": [{"snapshot-id": 1, "sequence-number": 1, "timestamp-ms": 0,
                     "manifest-list": f"{LOC}/metadata/ml.avro",
                     "summary": {"operation": "append"}, "schema-id": 0}]}
open(os.path.join(D, "metadata", "v1.metadata.json"), "w").write(json.dumps(md))
PY
	chmod -R a+rX "$PGC_WORKDIR/entry_$1"
	echo "$d/metadata/v1.metadata.json"
}
EC="$(entry_tbl ctl 1 0)"
check "control: a data manifest with one added data entry lists that one file" \
	"$(q "SELECT count(*) FROM pgcolumnar.iceberg_data_files('$EC')")" "1"
# status 0 is EXISTING: a live file in a manifest that a later commit rewrote or
# merged. A lister that refused every entry but ADDED would pass every other arm
# here and fail an ordinary table after its second append.
check "an EXISTING (status 0) data entry is listed, as an ADDED one is" \
	"$(q "SELECT count(*) FROM pgcolumnar.iceberg_data_files('$(entry_tbl kept 0 0)')")" "1"
check "a position-delete entry in a data manifest is refused (0A000)" \
	"$(sqlstate_of "SELECT * FROM pgcolumnar.iceberg_data_files('$(entry_tbl pos 1 1)')")" "0A000"
check "an equality-delete entry in a data manifest is refused (0A000)" \
	"$(sqlstate_of "SELECT * FROM pgcolumnar.iceberg_data_files('$(entry_tbl eq 1 2)')")" "0A000"
check "a DELETED (status 2) data entry is refused, not listed (0A000)" \
	"$(sqlstate_of "SELECT * FROM pgcolumnar.iceberg_data_files('$(entry_tbl gone 2 0)')")" "0A000"
check "backend still up after the entry refusals" "$(q 'SELECT 1')" "1"
recovered

# ---- no current snapshot: zero rows ----------------------------------------
NOSNAP="$PGC_WORKDIR/no_current.metadata.json"
python3 - "$MD" "$NOSNAP" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
m.pop("current-snapshot-id", None)
json.dump(m, open(sys.argv[2], "w"))
PY
chmod 644 "$NOSNAP"
check "a table with no current snapshot yields no data files" \
	"$(q "SELECT count(*) FROM pgcolumnar.iceberg_data_files('$NOSNAP')")" "0"

# ---- privilege: needs pg_read_server_files ---------------------------------
# The role gets USAGE on the schema, so the refusal is the function's own gate (EXECUTE
# it already has, through PUBLIC). Without USAGE the 42501 is "permission denied for
# schema pgcolumnar" and the function never runs, so the arm requires the gate's own
# message, naming pg_read_server_files, and not only the SQLSTATE they share.
DROP_UNPRIV="DO \$\$BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'icedf_unpriv')
	THEN EXECUTE 'DROP OWNED BY icedf_unpriv'; END IF; END\$\$;
DROP ROLE IF EXISTS icedf_unpriv;"
psql_run "$DROP_UNPRIV
CREATE ROLE icedf_unpriv LOGIN;
GRANT USAGE ON SCHEMA pgcolumnar TO icedf_unpriv;"
icedf_out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U icedf_unpriv -d "$PGC_DB" \
	-qtA 2>&1 <<SQL
\\set VERBOSITY verbose
SELECT count(*) FROM pgcolumnar.iceberg_data_files('$MD');
SQL
)"
# Only the ERROR line's message counts, as the twin reads message_primary: a role
# named in a HINT or DETAIL line is not the gate saying so.
icedf_err="${icedf_out#*ERROR:  }"; icedf_err="${icedf_err%%$'\n'*}"
case "$icedf_out" in
	*"ERROR:  "*) ;;
	*) icedf_err="" ;;
esac
case "$icedf_err" in
	"42501: "*pg_read_server_files*) icedf_got=42501 ;;
	*) icedf_got="${icedf_out%%$'\n'*}"; icedf_got="${icedf_got:-no output}" ;;
esac
check "an unprivileged role is refused (42501)" "$icedf_got" "42501"
psql_run "$DROP_UNPRIV"

pgc_summary
