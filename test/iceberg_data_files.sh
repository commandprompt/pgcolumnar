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

# like q, but a failed query reads as its ERROR line, so a failing check shows
# what the server said instead of an empty measurement
qe() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "$1" 2>&1 | grep -v '^DETAIL:\|^HINT:\|^CONTEXT:'
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
# equality delete file (entry content 1 or 2), which it refuses, or a removed
# file (status 2, DELETED), which it skips (#1791). Returning a removed file
# would read rows a commit took out; refusing it would refuse every table after
# a copy-on-write delete, which has no delete files. Each variant below is the
# same crafted table with one (status, content) pair; the (1, 0) control is
# listed, so a refusal is the entry's doing, not a fault in the crafted Avro.
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
# a delete file a later commit carried forward is EXISTING (status 0), and as live
# as an ADDED one: listing it as a data file would read rows it deletes
check "an EXISTING (status 0) position-delete entry in a data manifest is refused (0A000)" \
	"$(sqlstate_of "SELECT * FROM pgcolumnar.iceberg_data_files('$(entry_tbl kept_pos 0 1)')")" "0A000"
check "a DELETED (status 2) data entry is skipped, not listed and not refused" \
	"$(q "SELECT count(*) FROM pgcolumnar.iceberg_data_files('$(entry_tbl gone 2 0)')")" "0"
check "backend still up after the entry refusals" "$(q 'SELECT 1')" "1"
recovered

# ---- a copy-on-write delete: the removed file is skipped (#1791) -----------
# A real table written by pyiceberg (gen_iceberg_cow.py, warehouse_cow): ids 0..9
# appended, then a copy-on-write delete. The delete rewrites the file and records
# the old one as a DELETED (status 2) entry in the current snapshot. The table
# has no delete files, so the lister must list exactly the live files that
# pyiceberg's own scan planning lists. The premise arm proves the fixture really
# carries a status-2 entry, so the listing arms cannot pass on a table without one.
CW="$PGC_WORKDIR/cow_relocated"
rm -rf "$CW"; mkdir -p "$CW"
cp -r "$FX/warehouse_cow/db" "$CW/db"
chmod -R a+rX "$CW"
COW_ORACLE="$FX/warehouse_cow/expected_files.json"
cow_md() {  # $1 table -> its current metadata.json under the relocated copy
	python3 - "$COW_ORACLE" "$1" "$CW" <<'PY'
import json, sys
o = json.load(open(sys.argv[1]))["tables"][sys.argv[2]]
print("%s/db/%s/metadata/%s" % (sys.argv[3], sys.argv[2], o["metadata_basename"]))
PY
}
# The premise counts the DELETED (status 2) data (content 0) entries in each
# table's current snapshot twice: once in the committed Avro manifests, decoded
# here with json and zlib only, and once in the oracle. Both counts are shown,
# so an oracle that drifted from the fixture fails the arm.
COW_PREMISE="$(python3 - "$COW_ORACLE" "$FX/warehouse_cow" 2>&1 <<'PY'
import json, os, sys, zlib

def avro_records(path):
    """Every record of an Avro object container file (codec null or deflate)."""
    data, at = open(path, "rb").read(), [0]

    def raw(n):
        v = data[at[0]:at[0] + n]
        at[0] += n
        return v

    def num():
        n = shift = 0
        while True:
            c = raw(1)[0]
            n |= (c & 0x7F) << shift
            shift += 7
            if not c & 0x80:
                return (n >> 1) ^ -(n & 1)

    def blocks(read_one):  # Avro arrays and maps: counted blocks, 0 ends them
        out = []
        while True:
            n = num()
            if n == 0:
                return out
            if n < 0:
                num()
                n = -n
            out.extend(read_one() for _ in range(n))

    named = {}

    def value(t):
        if isinstance(t, list):
            return value(t[num()])
        if isinstance(t, dict):
            kind = t["type"]
            if "name" in t:
                named[t["name"]] = t
            if kind == "record":
                return {f["name"]: value(f["type"]) for f in t["fields"]}
            if kind == "enum":
                return t["symbols"][num()]
            if kind == "fixed":
                return raw(t["size"])
            if kind == "array":
                return blocks(lambda: value(t["items"]))
            if kind == "map":
                return dict(blocks(lambda: (raw(num()).decode(), value(t["values"]))))
            return value(kind)
        if t == "null":
            return None
        if t == "boolean":
            return raw(1) != b"\0"
        if t in ("int", "long"):
            return num()
        if t == "float":
            return raw(4)
        if t == "double":
            return raw(8)
        if t == "bytes":
            return raw(num())
        if t == "string":
            return raw(num()).decode()
        return value(named[t])

    if raw(4) != b"Obj\x01":
        raise ValueError("%s is not an Avro container file" % path)
    meta = dict(blocks(lambda: (raw(num()).decode(), raw(num()))))
    schema = json.loads(meta["avro.schema"])
    codec = meta.get("avro.codec", b"null")
    sync = raw(16)
    file_data, records = data, []
    while at[0] < len(file_data):
        count, block = num(), raw(num())
        if raw(16) != sync:
            raise ValueError("%s: bad sync marker" % path)
        if codec == b"deflate":
            block = zlib.decompress(block, -15)
        elif codec != b"null":
            raise ValueError("%s: codec %r" % (path, codec))
        data, saved = block, at[0]
        at[0] = 0
        records.extend(value(schema) for _ in range(count))
        data, at[0] = file_data, saved
    return records

oracle = json.load(open(sys.argv[1]))["tables"]
out = []
for name in ("cow", "cow_all"):
    md_dir = os.path.join(sys.argv[2], "db", name, "metadata")
    md = json.load(open(os.path.join(md_dir, oracle[name]["metadata_basename"])))
    snap = [s for s in md["snapshots"] if s["snapshot-id"] == md["current-snapshot-id"]][0]
    in_avro = 0
    for mf in avro_records(os.path.join(md_dir, os.path.basename(snap["manifest-list"]))):
        for e in avro_records(os.path.join(md_dir, os.path.basename(mf["manifest_path"]))):
            in_avro += e["status"] == 2 and e["data_file"]["content"] == 0
    in_oracle = sum(e["status"] == 2 and e["content"] == 0 for e in oracle[name]["entries"])
    out.append("%s: avro=%d oracle=%d" % (name, in_avro, in_oracle))
print("; ".join(out))
PY
)"
check "premise: each copy-on-write fixture's current snapshot carries a DELETED (status 2) data entry" \
	"$COW_PREMISE" "cow: avro=1 oracle=1; cow_all: avro=1 oracle=1"
COW_EXPECT="$(python3 - "$COW_ORACLE" <<'PY'
import json, sys
o = json.load(open(sys.argv[1]))["tables"]["cow"]
print("\n".join(sorted("%d|%s" % (r["record_count"], r["file_basename"]) for r in o["live_files"])))
PY
)"
check "after a copy-on-write delete, data files == pyiceberg plan_files (record_count|basename)" \
	"$(q "SELECT record_count || '|' || (regexp_match(file_path,'[^/]+\$'))[1]
	      FROM pgcolumnar.iceberg_data_files('$(cow_md cow)') ORDER BY 1")" "$COW_EXPECT"
check "after a copy-on-write delete of every row, no data files are listed" \
	"$(q "SELECT count(*) FROM pgcolumnar.iceberg_data_files('$(cow_md cow_all)')")" "0"

# ---- a delete manifest that holds only removed entries (#1829) -------------
# A rewrite or compaction that removes every old delete file leaves a delete
# manifest (content 1 in the list) whose entries all have status 2 (DELETED).
# That snapshot has no live delete file, so the lister skips the manifest. A
# list entry whose added and existing file counts are both above zero is refused
# unopened (the arm above); otherwise the lister opens the manifest and refuses
# only a live entry. Each table here has one live data file in a data manifest
# and one delete manifest holding one position-delete entry.
delmf_tbl() {  # $1 name, $2 entry status, $3 counts|nocounts, [$4 entry content, default 1]
	local d="$PGC_WORKDIR/delmf_$1/db/t"
	mkdir -p "$d/metadata"
	python3 - "$d" "$2" "$3" "${4:-1}" <<'PY'
import json, sys, os
D, STATUS, COUNTS, CONTENT = sys.argv[1], int(sys.argv[2]), sys.argv[3] == "counts", int(sys.argv[4])
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
def ent(status, content, name):
    return zz(status) + (zz(1) + zz(1)) + zz(content) + s(f"{LOC}/data/{name}".encode()) + \
           s(b"PARQUET") + zz(5) + zz(1000)
open(os.path.join(D, "metadata", "dm.avro"), "wb").write(ocf(entry, [ent(1, 0, "f.parquet")]))
open(os.path.join(D, "metadata", "dl.avro"), "wb").write(ocf(entry, [ent(STATUS, CONTENT, "pos.parquet")]))
fields = [
    {"name": "manifest_path", "type": "string"},
    {"name": "manifest_length", "type": "long"},
    {"name": "partition_spec_id", "type": "int"},
    {"name": "content", "type": "int"},
    {"name": "sequence_number", "type": ["null", "long"]},
    {"name": "min_sequence_number", "type": "long"},
    {"name": "added_snapshot_id", "type": "long"}]
if COUNTS:
    fields += [{"name": n, "type": "int"} for n in
               ("added_files_count", "existing_files_count", "deleted_files_count")]
def mf(path, content, counts):
    r = s(f"{LOC}/metadata/{path}".encode()) + zz(1000) + zz(0) + zz(content) + \
        (zz(1) + zz(1)) + zz(1) + zz(1)
    return r + b"".join(zz(c) for c in counts) if COUNTS else r
dcounts = (0, 0, 1) if STATUS == 2 else (1, 0, 0)
open(os.path.join(D, "metadata", "ml.avro"), "wb").write(ocf(
    {"type": "record", "name": "manifest_file", "fields": fields},
    [mf("dm.avro", 0, (1, 0, 0)), mf("dl.avro", 1, dcounts)]))
md = {"format-version": 2, "location": LOC, "current-snapshot-id": 1,
      "current-schema-id": 0,
      "schemas": [{"schema-id": 0, "type": "struct", "fields": [
          {"id": 1, "name": "id", "required": False, "type": "long"}]}],
      "snapshots": [{"snapshot-id": 1, "sequence-number": 1, "timestamp-ms": 0,
                     "manifest-list": f"{LOC}/metadata/ml.avro",
                     "summary": {"operation": "replace"}, "schema-id": 0}]}
open(os.path.join(D, "metadata", "v1.metadata.json"), "w").write(json.dumps(md))
PY
	chmod -R a+rX "$PGC_WORKDIR/delmf_$1"
	echo "$d/metadata/v1.metadata.json"
}
dm_list() {  # the listing as "record_count|basename", or the error
	qe "SELECT string_agg(record_count || '|' || (regexp_match(file_path,'[^/]+\$'))[1], ',')
	   FROM pgcolumnar.iceberg_data_files('$1')"
}
check "a delete manifest whose entries are all removed (status 2) is skipped, not refused (#1829)" \
	"$(dm_list "$(delmf_tbl gone 2 counts)")" "5|f.parquet"
check "a delete manifest without file counts, holding only removed entries, is skipped (#1829)" \
	"$(dm_list "$(delmf_tbl gone_nc 2 nocounts)")" "5|f.parquet"
check "a delete manifest without file counts, holding a live delete entry, is refused (0A000)" \
	"$(sqlstate_of "SELECT * FROM pgcolumnar.iceberg_data_files('$(delmf_tbl live_nc 1 nocounts)')")" "0A000"
check "a delete manifest without file counts, holding an EXISTING (status 0) delete entry, is refused (0A000)" \
	"$(sqlstate_of "SELECT * FROM pgcolumnar.iceberg_data_files('$(delmf_tbl live0_nc 0 nocounts)')")" "0A000"
# a delete manifest holds delete files only: a live DATA entry in one is corrupt
# metadata, refused rather than listed as if the list had called it a data manifest
check "a live data entry inside a delete manifest is refused as corrupt (XX001)" \
	"$(sqlstate_of "SELECT * FROM pgcolumnar.iceberg_data_files('$(delmf_tbl data_nc 1 nocounts 0)')")" "XX001"
check "backend still up after the delete-manifest arms" "$(q 'SELECT 1')" "1"
recovered

# ---- partition values render exactly, as valid text (#1816, #1791) ---------
# A real pyiceberg warehouse (gen_iceberg_partvals.py, warehouse_partvals): one
# table per partition type, identity-partitioned, one file per value. The oracle
# holds pyiceberg's own plan_files partition values and the text each renders as
# under the documented format. Before #1816 decimal and float values rendered as
# '?', and binary values were copied raw into text (invalid UTF-8, cut at a NUL).
PV="$PGC_WORKDIR/partvals_relocated"
rm -rf "$PV"; mkdir -p "$PV"
cp -r "$FX/warehouse_partvals/db" "$PV/db"
chmod -R a+rX "$PV"
PV_ORACLE="$FX/warehouse_partvals/expected_files.json"
PV_ENC="$(q "SHOW server_encoding")"
pv_md() {  # $1 table -> its current metadata.json under the relocated copy
	python3 - "$PV_ORACLE" "$1" "$PV" <<'PY'
import json, sys
o = json.load(open(sys.argv[1]))["tables"][sys.argv[2]]
print("%s/db/%s/metadata/%s" % (sys.argv[3], sys.argv[2], o["metadata_basename"]))
PY
}
pv_expect() {  # $1 table -> its oracle partitions, ' ; '-joined in byte order
	python3 - "$PV_ORACLE" "$1" "$PV_ENC" <<'PY'
import json, sys
o = json.load(open(sys.argv[1]))["tables"][sys.argv[2]]
key = "partition" if sys.argv[3] in ("UTF8", "SQL_ASCII") else "partition_non_utf8_db"
print(" ; ".join(sorted((r[key] for r in o["files"]), key=lambda v: v.encode())))
PY
}
pv_got() {  # $1 table -> its listed partitions, ' ; '-joined in byte order
	qe "SELECT string_agg(partition, ' ; ' ORDER BY partition COLLATE \"C\")
	   FROM pgcolumnar.iceberg_data_files('$(pv_md "$1")')"
}
check "a decimal(10,2) partition renders as the exact decimal (#1816)" \
	"$(pv_got dec)" "$(pv_expect dec)"
check "a decimal(38,10) partition renders as the exact decimal (#1816)" \
	"$(pv_got dec38)" "$(pv_expect dec38)"
check "a decimal(9,0) partition renders with no decimal point (#1816)" \
	"$(pv_got dec0)" "$(pv_expect dec0)"
check "a float partition renders as the shortest exact text (#1816)" \
	"$(pv_got flt)" "$(pv_expect flt)"
check "a double partition renders as the shortest exact text (#1816)" \
	"$(pv_got dbl)" "$(pv_expect dbl)"
check "a binary partition renders as hex (#1816)" \
	"$(pv_got bin)" "$(pv_expect bin)"
# convert_from verifies the bytes as UTF-8 whatever the database encoding, so an
# invalid byte fails here; the lengths show every byte was kept (no cut at a NUL)
check "every binary partition is valid UTF-8 (char lengths) (#1816)" \
	"$(qe "SELECT string_agg(length(convert_from(textsend(partition), 'UTF8'))::text, ',' ORDER BY 1)
	      FROM pgcolumnar.iceberg_data_files('$(pv_md bin)')")" "8,10"
check "string partitions keep NULL, empty, ',', '=', '%' and non-ASCII apart (#1791)" \
	"$(pv_got str)" "$(pv_expect str)"
PV_BIN_M="$(ls "$(dirname "$(pv_md bin)")"/*-m0.avro | head -1)"
check "read_avro_manifest renders the binary partitions as hex too (#1816)" \
	"$(qe "SELECT string_agg(partition, ' ; ' ORDER BY partition COLLATE \"C\")
	      FROM pgcolumnar.read_avro_manifest('$PV_BIN_M')")" "$(pv_expect bin)"

# ---- the documented format for what no writer here produces (#1816) ---------
# One crafted manifest entry whose partition record has five fields, in order:
#   a,b=c%  string 'x'               a field name is escaped as a string is
#   ctl     string 'a<TAB>b<DEL>c'   control bytes and DEL are percent-encoded
#   bad     string bytes Q FF FE Q   not UTF-8, so its high bytes are encoded too
#   fx      fixed(3) 00 ab ff        a plain fixed is hex
#   u       fixed(16) logicalType uuid, bytes 00..0f   a uuid is hex
# Field text never holds a bare ',', so split_part(partition, ',', N) is field N.
PF="$PGC_WORKDIR/partfmt/pf.avro"
mkdir -p "$(dirname "$PF")"
python3 - "$PF" <<'PY'
import json, sys
def zz(n):
    u = (n << 1) ^ (n >> 63) if n < 0 else (n << 1)
    out = bytearray()
    while True:
        b = u & 0x7f; u >>= 7
        out.append(b | 0x80 if u else b)
        if not u: break
    return bytes(out)
def s(bs): return zz(len(bs)) + bs
part = {"type": "record", "name": "r102", "fields": [
    {"name": "a,b=c%", "type": ["null", "string"]},
    {"name": "ctl", "type": ["null", "string"]},
    {"name": "bad", "type": ["null", "string"]},
    {"name": "fx", "type": ["null", {"type": "fixed", "name": "f3", "size": 3}]},
    {"name": "u", "type": ["null", {"type": "fixed", "name": "u16", "size": 16,
                                    "logicalType": "uuid"}]}]}
entry = {"type": "record", "name": "manifest_entry", "fields": [
    {"name": "status", "type": "int"},
    {"name": "data_file", "type": {"type": "record", "name": "df", "fields": [
        {"name": "content", "type": "int"},
        {"name": "file_path", "type": "string"},
        {"name": "file_format", "type": "string"},
        {"name": "partition", "type": part},
        {"name": "record_count", "type": "long"},
        {"name": "file_size_in_bytes", "type": "long"}]}}]}
pv = (zz(1) + s(b"x") + zz(1) + s(b"a\tb\x7fc") + zz(1) + s(b"Q\xff\xfeQ")
      + zz(1) + b"\x00\xab\xff" + zz(1) + bytes(range(16)))
rec = zz(1) + zz(0) + s(b"file:///tmp/pgc_ice_wh/db/t/data/f.parquet") + s(b"PARQUET") \
      + pv + zz(5) + zz(1000)
sync = b"\x00" * 16
meta = zz(2) + s(b"avro.schema") + s(json.dumps(entry).encode()) + s(b"avro.codec") + s(b"null") + zz(0)
open(sys.argv[1], "wb").write(b"Obj\x01" + meta + sync + zz(1) + zz(len(rec)) + rec + sync)
PY
chmod -R a+rX "$PGC_WORKDIR/partfmt"
pf_field() {  # $1 field numbers, e.g. "1" or "4,5" -> those fields, ','-joined
	qe "SELECT string_agg(split_part(partition, ',', n), ',' ORDER BY n)
	   FROM pgcolumnar.read_avro_manifest('$PF'), unnest('{$1}'::int[]) n"
}
check "a partition field name is percent-encoded as a string value is (#1816)" \
	"$(pf_field 1)" "a%2Cb%3Dc%25=x"
check "control bytes and DEL in a string partition are percent-encoded (#1816)" \
	"$(pf_field 2)" "ctl=a%09b%7Fc"
check "a string partition that is not UTF-8 is percent-encoded, never passed raw (#1816)" \
	"$(pf_field 3)" "bad=Q%FF%FEQ"
check "a plain fixed and a uuid partition render as hex (#1816)" \
	"$(pf_field 4,5)" 'fx=\x00abff,u=\x000102030405060708090a0b0c0d0e0f'

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
