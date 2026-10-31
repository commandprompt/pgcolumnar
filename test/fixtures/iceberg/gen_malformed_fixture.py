#!/usr/bin/env python3
# Hand-crafted MALFORMED Iceberg fixtures for the read-path robustness suite
# (test/iceberg_malformed.sh). Each targets a defect the adversarial re-audit of
# #388 phase-4a (#644) found and proved on the bench: a crafted manifest that the
# reader must refuse cleanly (never crash), and a faithful v1 manifest the reader
# must read (never wrongly refuse). No available writer emits these shapes, so the
# manifests / manifest lists / metadata.json are hand-encoded Avro / JSON and the
# data Parquet is a real pyarrow file. All paths use a stable recorded root so the
# committed fixture carries no machine-specific path; the reader rebases at read
# time and the suite relocates the tree first.
#
# Variants written under OUT:
#   nullpath      v2 manifest whose data_file.file_path is union[null,string]=null
#                 -> was a NULL-deref SIGSEGV in iceberg_scan/iceberg_data_files
#   nullpath_ctl  same, file_path present (control: reads one file / five rows)
#   arrayfield/evil.avro  an Avro OCF whose record schema "fields" element is a
#                 JSON array [[0]] -> was an assert-abort / OOB heap read
#   arrayfield/good.avro  same framing, a well-formed field object (control)
#   enum_ctl      two data files, one manifest (control: ten rows, two files)
#   content3_old / contentm1_old / content3_new / status3_old / mfcontent5
#                 enum_ctl with one data_file.content, manifest_entry.status or
#                 manifest_file.content outside the spec (#1823)
#   content4g_old / status4g_old / mfcontent4g
#                 the same three fields at 2^32 or 2^32+1, outside int32 (#1823)
import sys, os, json, shutil
import pyarrow as pa, pyarrow.parquet as pq

OUT = sys.argv[1]                                   # fixtures/iceberg/warehouse_malformed
ROOT = "/tmp/pgc_ice_malformed"                     # stable recorded root
shutil.rmtree(OUT, ignore_errors=True)
os.makedirs(OUT)


# ---- Avro OCF encoding helpers (as in gen_delete_fixture.py) --------------
def zz(n):
    u = (n << 1) ^ (n >> 63) if n < 0 else (n << 1)
    out = bytearray()
    while True:
        b = u & 0x7f; u >>= 7
        out.append(b | 0x80 if u else b)
        if not u:
            break
    return bytes(out)


def s(bs):
    return zz(len(bs)) + bs


def ocf(schema_json, records):
    sync = b"\x00" * 16
    meta = zz(2) + s(b"avro.schema") + s(schema_json) + s(b"avro.codec") + s(b"null") + zz(0)
    out = b"Obj\x01" + meta + sync
    for rec in records:
        out += zz(1) + zz(len(rec)) + rec + sync
    return out


ICE_FIELDS = [
    {"id": 1, "name": "id", "required": False, "type": "long"},
    {"id": 2, "name": "region", "required": False, "type": "string"},
    {"id": 3, "name": "amount", "required": False, "type": "int"}]
SCHEMA = {"schema-id": 0, "type": "struct", "fields": ICE_FIELDS}
SNAP = 4242


def write_data_parquet(dest, ids=(1, 2, 3, 4, 5)):
    data = pa.table({
        "id": pa.array(list(ids), pa.int64()),
        "region": pa.array(["eu", "eu", "us", "us", "us"], pa.string()),
        "amount": pa.array([10, 20, 30, 40, 50], pa.int32()),
    }, schema=pa.schema([
        pa.field("id", pa.int64(), metadata={b"PARQUET:field_id": b"1"}),
        pa.field("region", pa.string(), metadata={b"PARQUET:field_id": b"2"}),
        pa.field("amount", pa.int32(), metadata={b"PARQUET:field_id": b"3"}),
    ]))
    pq.write_table(data, dest, row_group_size=2)


def metadata_json(loc, ml_name, fmt_version, snap_seq):
    m = {"format-version": fmt_version, "location": loc,
         "current-snapshot-id": SNAP,
         "snapshots": [{"snapshot-id": SNAP, "timestamp-ms": 0,
                        "manifest-list": f"{loc}/metadata/{ml_name}",
                        "summary": {"operation": "append"}}]}
    if fmt_version == 1:
        m["schema"] = {"type": "struct", "fields": ICE_FIELDS}
    else:
        m["current-schema-id"] = 0
        m["schemas"] = [SCHEMA]
        m["snapshots"][0]["sequence-number"] = snap_seq
        m["snapshots"][0]["schema-id"] = 0
    return json.dumps(m)


# ================================================== nullpath (v2, #2 crash) ==
# data_file.file_path declared union[null,string]; the null branch (index 0)
# decodes to a NULL path the reader must refuse, not dereference.
ENTRY_NULLPATH = json.dumps({"type": "record", "name": "manifest_entry", "fields": [
    {"name": "status", "type": "int"},
    {"name": "sequence_number", "type": ["null", "long"]},
    {"name": "data_file", "type": {"type": "record", "name": "df", "fields": [
        {"name": "content", "type": "int"},
        {"name": "file_path", "type": ["null", "string"]},
        {"name": "file_format", "type": "string"},
        {"name": "record_count", "type": "long"},
        {"name": "file_size_in_bytes", "type": "long"}]}}]}).encode()

MFILE_V2 = json.dumps({"type": "record", "name": "manifest_file", "fields": [
    {"name": "manifest_path", "type": "string"},
    {"name": "manifest_length", "type": "long"},
    {"name": "partition_spec_id", "type": "int"},
    {"name": "content", "type": "int"},
    {"name": "sequence_number", "type": ["null", "long"]},
    {"name": "min_sequence_number", "type": "long"},
    {"name": "added_snapshot_id", "type": "long"}]}).encode()


def mfile_v2(path, content, seq, snap):
    return s(path.encode()) + zz(1000) + zz(0) + zz(content) + \
        (zz(1) + zz(seq)) + zz(seq) + zz(snap)


def build_nullpath(name, file_path_present):
    root = os.path.join(OUT, name)
    md = os.path.join(root, "db", "t", "metadata")
    dd = os.path.join(root, "db", "t", "data")
    os.makedirs(md); os.makedirs(dd)
    loc = f"file://{ROOT}/{name}/db/t"
    write_data_parquet(os.path.join(dd, "data.parquet"))
    if file_path_present:
        fp = (zz(1) + s(f"{loc}/data/data.parquet".encode()))     # union branch 1
    else:
        fp = zz(0)                                                # union branch 0: null
    ent = zz(1) + (zz(1) + zz(5)) + zz(0) + fp + s(b"PARQUET") + zz(5) + zz(1000)
    open(os.path.join(md, "dm.avro"), "wb").write(ocf(ENTRY_NULLPATH, [ent]))
    open(os.path.join(md, "ml.avro"), "wb").write(
        ocf(MFILE_V2, [mfile_v2(f"{loc}/metadata/dm.avro", 0, 5, SNAP)]))
    open(os.path.join(md, "t.metadata.json"), "w").write(
        metadata_json(loc, "ml.avro", 2, 5))


build_nullpath("nullpath", False)
build_nullpath("nullpath_ctl", True)


# ================================== arrayfield (bare Avro OCF, #7 crash) ==
af = os.path.join(OUT, "arrayfield")
os.makedirs(af)
evil = json.dumps({"type": "record", "name": "manifest_entry",
                   "fields": [[0]]}).encode()          # a field element that is an ARRAY
good = json.dumps({"type": "record", "name": "manifest_entry",
                   "fields": [{"name": "status", "type": "int"}]}).encode()
open(os.path.join(af, "evil.avro"), "wb").write(ocf(evil, []))
open(os.path.join(af, "good.avro"), "wb").write(ocf(good, []))

# ============================ nullseq (v2, #644 defect #4) ==
# A manifest_file.sequence_number that is the union's NULL branch was decoded
# silently as 0. A v2/v3 list always carries a concrete number, so a null is
# corrupt: an ADDED entry that must inherit it would take 0 and mis-apply an
# older delete -> a dropped row.
def mfile_v2_seq(path, content, seq, snap):
    # seq None -> the sequence_number union's NULL branch (branch 0); else branch 1
    seqb = zz(0) if seq is None else (zz(1) + zz(seq))
    return s(path.encode()) + zz(1000) + zz(0) + zz(content) + seqb + \
        zz(seq or 0) + zz(snap)


def write_posdel_parquet(dest, data_path, positions):
    t = pa.table({"file_path": pa.array([data_path] * len(positions), pa.string()),
                  "pos": pa.array(positions, pa.int64())},
                 schema=pa.schema([
                     pa.field("file_path", pa.string(), nullable=False,
                              metadata={b"PARQUET:field_id": b"2147483546"}),
                     pa.field("pos", pa.int64(), nullable=False,
                              metadata={b"PARQUET:field_id": b"2147483545"})]))
    pq.write_table(t, dest)


# --- decode-level: a bare manifest list read directly by read_manifest_list ---
nsml = os.path.join(OUT, "nullseq_ml"); os.makedirs(nsml)
open(os.path.join(nsml, "ml.avro"), "wb").write(
    ocf(MFILE_V2, [mfile_v2_seq(f"file://{ROOT}/x/dm.avro", 0, None, SNAP)]))
open(os.path.join(nsml, "ml_ctl.avro"), "wb").write(
    ocf(MFILE_V2, [mfile_v2_seq(f"file://{ROOT}/x/dm.avro", 0, 5, SNAP)]))


# ============================ nullminseq (#686/#691 cosmetic) ==
# A manifest_file.min_sequence_number that is the union's NULL branch was decoded
# silently as 0. It is not consumed by the delete rules, so this is cosmetic, but
# read_manifest_list should report SQL NULL, matching sequence_number.
MFILE_NULLMIN = json.dumps({"type": "record", "name": "manifest_file", "fields": [
    {"name": "manifest_path", "type": "string"},
    {"name": "manifest_length", "type": "long"},
    {"name": "partition_spec_id", "type": "int"},
    {"name": "content", "type": "int"},
    {"name": "sequence_number", "type": ["null", "long"]},
    {"name": "min_sequence_number", "type": ["null", "long"]},
    {"name": "added_snapshot_id", "type": "long"}]}).encode()


def mfile_nullmin(path, content, seq, minseq, snap):
    seqb = zz(0) if seq is None else (zz(1) + zz(seq))
    minb = zz(0) if minseq is None else (zz(1) + zz(minseq))
    return s(path.encode()) + zz(1000) + zz(0) + zz(content) + seqb + minb + zz(snap)


nmml = os.path.join(OUT, "nullminseq_ml"); os.makedirs(nmml)
open(os.path.join(nmml, "ml.avro"), "wb").write(
    ocf(MFILE_NULLMIN, [mfile_nullmin(f"file://{ROOT}/x/dm.avro", 0, 5, None, SNAP)]))
open(os.path.join(nmml, "ml_ctl.avro"), "wb").write(
    ocf(MFILE_NULLMIN, [mfile_nullmin(f"file://{ROOT}/x/dm.avro", 0, 5, 7, SNAP)]))


# --- scan-level: a data file whose ADDED entry inherits the null manifest seq,
#     plus a seq-3 position delete dropping ordinal 0 (id 1) of data.parquet ----
def build_nullseq(name, data_mfile_seq):
    root = os.path.join(OUT, name)
    md = os.path.join(root, "db", "t", "metadata")
    dd = os.path.join(root, "db", "t", "data")
    os.makedirs(md); os.makedirs(dd)
    loc = f"file://{ROOT}/{name}/db/t"
    write_data_parquet(os.path.join(dd, "data.parquet"))                 # ids 1..5
    write_posdel_parquet(os.path.join(dd, "posdel.parquet"),
                         f"{loc}/data/data.parquet", [0])                # drop id 1
    dfp = zz(1) + s(f"{loc}/data/data.parquet".encode())                 # file_path branch 1
    dent = zz(1) + zz(0) + zz(0) + dfp + s(b"PARQUET") + zz(5) + zz(1000)   # status1, seq NULL, content0
    open(os.path.join(md, "dm.avro"), "wb").write(ocf(ENTRY_NULLPATH, [dent]))
    xfp = zz(1) + s(f"{loc}/data/posdel.parquet".encode())
    xent = zz(1) + (zz(1) + zz(3)) + zz(1) + xfp + s(b"PARQUET") + zz(1) + zz(500)  # status1, seq3, content1
    open(os.path.join(md, "xm.avro"), "wb").write(ocf(ENTRY_NULLPATH, [xent]))
    open(os.path.join(md, "ml.avro"), "wb").write(ocf(MFILE_V2, [
        mfile_v2_seq(f"{loc}/metadata/dm.avro", 0, data_mfile_seq, SNAP),  # THE null under test
        mfile_v2_seq(f"{loc}/metadata/xm.avro", 1, 3, SNAP)]))
    open(os.path.join(md, "t.metadata.json"), "w").write(
        metadata_json(loc, "ml.avro", 2, 5))


build_nullseq("nullseq", None)      # RED   : data manifest seq is NULL
build_nullseq("nullseq_ctl", 5)     # control: data manifest seq is 5 (concrete)


# ============================ danglingschema (#644 defect #8 stale-schema) ==
# STALE top-level schema maps "amount" -> field id 4 (a SECOND int column,
# values 100..500); the correct schema (schemas[0]) maps "amount" -> id 3
# (10..50). Both are int, so a dangling fallback is a SILENT wrong read -- same
# type, different values -- NOT masked by a type mismatch (id-vs-int would be).
STALE_FIELDS = [
    {"id": 4, "name": "amount", "required": False, "type": "int"},
    {"id": 1, "name": "id",     "required": False, "type": "long"},
    {"id": 2, "name": "region", "required": False, "type": "string"}]


def write_data_parquet4(dest):
    data = pa.table({
        "id": pa.array([1, 2, 3, 4, 5], pa.int64()),
        "region": pa.array(["eu", "eu", "us", "us", "us"], pa.string()),
        "amount": pa.array([10, 20, 30, 40, 50], pa.int32()),
        "other": pa.array([100, 200, 300, 400, 500], pa.int32()),
    }, schema=pa.schema([
        pa.field("id", pa.int64(), metadata={b"PARQUET:field_id": b"1"}),
        pa.field("region", pa.string(), metadata={b"PARQUET:field_id": b"2"}),
        pa.field("amount", pa.int32(), metadata={b"PARQUET:field_id": b"3"}),
        pa.field("other", pa.int32(), metadata={b"PARQUET:field_id": b"4"}),
    ]))
    pq.write_table(data, dest, row_group_size=2)


def build_schema_variant(name, meta_obj):
    root = os.path.join(OUT, name)
    md = os.path.join(root, "db", "t", "metadata")
    dd = os.path.join(root, "db", "t", "data")
    os.makedirs(md); os.makedirs(dd)
    loc = f"file://{ROOT}/{name}/db/t"
    write_data_parquet4(os.path.join(dd, "data.parquet"))
    fp = zz(1) + s(f"{loc}/data/data.parquet".encode())          # union branch 1: present
    ent = zz(1) + (zz(1) + zz(5)) + zz(0) + fp + s(b"PARQUET") + zz(5) + zz(1000)
    open(os.path.join(md, "dm.avro"), "wb").write(ocf(ENTRY_NULLPATH, [ent]))
    open(os.path.join(md, "ml.avro"), "wb").write(
        ocf(MFILE_V2, [mfile_v2(f"{loc}/metadata/dm.avro", 0, 5, SNAP)]))
    m = dict(meta_obj)
    m["location"] = loc
    m["current-snapshot-id"] = SNAP
    m["snapshots"] = [{"snapshot-id": SNAP, "timestamp-ms": 0,
                       "manifest-list": f"{loc}/metadata/ml.avro",
                       "summary": {"operation": "append"},
                       "sequence-number": 5, "schema-id": 0}]
    open(os.path.join(md, "t.metadata.json"), "w").write(json.dumps(m))


# dangling: current-schema-id 99 is absent from schemas[]; a stale top-level
# "schema" exists -> main silently binds through it (amount -> id 1 -> 1..5)
build_schema_variant("danglingschema", {
    "format-version": 2, "current-schema-id": 99,
    "schemas": [SCHEMA],
    "schema": {"type": "struct", "fields": STALE_FIELDS}})
# control: SAME table, current-schema-id 0 resolves in schemas[] (amount -> 10..50)
build_schema_variant("danglingschema_ctl", {
    "format-version": 2, "current-schema-id": 0, "schemas": [SCHEMA]})
# legacy control: no "schemas" array, no current-schema-id, only top-level
# "schema" -> the else-branch must read it (all 5 rows, amount 10..50)
build_schema_variant("legacyschema", {
    "format-version": 2, "schema": {"type": "struct", "fields": ICE_FIELDS}})


# ============================ nullmanifestpath (#691, live #644-class crash) ==
# A hostile manifest-list whose manifest_file.manifest_path is the union NULL
# branch. The reader decodes against the embedded (untrusted) schema, so this is
# reachable; without a guard ice_rebase strncmp(NULL)s and crashes the backend.
MFILE_NULLPATH = json.dumps({"type": "record", "name": "manifest_file", "fields": [
    {"name": "manifest_path", "type": ["null", "string"]},
    {"name": "manifest_length", "type": "long"},
    {"name": "partition_spec_id", "type": "int"},
    {"name": "content", "type": "int"},
    {"name": "sequence_number", "type": ["null", "long"]},
    {"name": "min_sequence_number", "type": "long"},
    {"name": "added_snapshot_id", "type": "long"}]}).encode()

nmp = os.path.join(OUT, "nullmanifestpath", "db", "t", "metadata")
os.makedirs(nmp)
nmp_loc = f"file://{ROOT}/nullmanifestpath/db/t"
# manifest_path null branch (zz(0)); the rest minimal and well-formed
mfrec = zz(0) + zz(1000) + zz(0) + zz(0) + (zz(1) + zz(5)) + zz(5) + zz(SNAP)
open(os.path.join(nmp, "ml.avro"), "wb").write(ocf(MFILE_NULLPATH, [mfrec]))
open(os.path.join(nmp, "t.metadata.json"), "w").write(
    metadata_json(nmp_loc, "ml.avro", 2, 5))


# ============================ enum fields (#1823) ==
# Two data files in one data manifest: old.parquet (ids 1..5, entry seq 4) and
# new.parquet (ids 6..10, entry seq 5). The Iceberg spec allows only 0, 1 and 2 for
# data_file.content and for manifest_entry.status, and only 0 and 1 for
# manifest_file.content. An unknown data_file.content was sorted as an equality
# delete: on the OLDEST file it was then skipped as never applicable, so that
# file's five rows vanished with no error; on the newest it failed with a
# misleading "has no equality_ids". Each variant below changes ONE value of the
# control; the reader must refuse it as corrupt metadata, naming the value.
def build_enum(name, old_content=0, new_content=0, old_status=1, mf_content=0):
    root = os.path.join(OUT, name)
    md = os.path.join(root, "db", "t", "metadata")
    dd = os.path.join(root, "db", "t", "data")
    os.makedirs(md); os.makedirs(dd)
    loc = f"file://{ROOT}/{name}/db/t"
    write_data_parquet(os.path.join(dd, "old.parquet"))                  # ids 1..5
    write_data_parquet(os.path.join(dd, "new.parquet"), range(6, 11))    # ids 6..10
    ents = []
    for fname, status, seq, content in (("old", old_status, 4, old_content),
                                        ("new", 1, 5, new_content)):
        fp = zz(1) + s(f"{loc}/data/{fname}.parquet".encode())
        ents.append(zz(status) + (zz(1) + zz(seq)) + zz(content) + fp +
                    s(b"PARQUET") + zz(5) + zz(1000))
    open(os.path.join(md, "dm.avro"), "wb").write(ocf(ENTRY_NULLPATH, ents))
    open(os.path.join(md, "ml.avro"), "wb").write(
        ocf(MFILE_V2, [mfile_v2(f"{loc}/metadata/dm.avro", mf_content, 5, SNAP)]))
    open(os.path.join(md, "t.metadata.json"), "w").write(
        metadata_json(loc, "ml.avro", 2, 5))


build_enum("enum_ctl")                          # control: 10 rows, 2 files listed
build_enum("content3_old", old_content=3)       # was: 5 rows, no error
build_enum("contentm1_old", old_content=-1)     # was: 5 rows, no error
build_enum("content3_new", new_content=3)       # was: "has no equality_ids"
build_enum("status3_old", old_status=3)         # was: read as live data
build_enum("mfcontent5", mf_content=5)          # was: read as a data manifest
# A value outside int32 was cast to int32 before the range check, so 2^32 wrapped
# to 0 and 2^32+1 to 1, both valid: each read as live data with no error.
build_enum("content4g_old", old_content=2**32)  # was: 10 rows, read as data
build_enum("status4g_old", old_status=2**32 + 1)  # was: 10 rows, read as added
build_enum("mfcontent4g", mf_content=2**32)     # was: read as a data manifest


print("malformed fixtures written under", OUT)
