#!/usr/bin/env python3
# Generate a hand-crafted Apache Iceberg v2 table that carries POSITION DELETES,
# for #388 phase 4a. No available writer emits merge-on-read deletes (pyiceberg
# 0.11.1 falls back to copy-on-write), so the whole table is constructed: the
# data and position-delete Parquet files are written with pyarrow (real Parquet,
# real field ids), and the manifests, manifest list, and metadata.json are
# hand-encoded Avro / JSON so the data/delete sequence numbers are exact.
#
# Layout, all rooted at a stable recorded path so the committed fixture carries
# no machine-specific path (the reader rebases it at read time):
#
#   db/t/metadata/v1.metadata.json
#   db/t/metadata/manifest-list.avro   -> data manifest (content 0, seq 1)
#                                         delete manifest (content 1, seq 2)
#   db/t/metadata/data-manifest.avro   -> data.parquet   (content 0)
#   db/t/metadata/delete-manifest.avro -> posdel.parquet (content 1)
#   db/t/data/data.parquet             id/region/amount, 5 rows
#   db/t/data/posdel.parquet           (file_path, pos) dropping rows 1 and 3
#
# The oracle is the five data rows minus the two deleted positions.
import sys, os, json, shutil
import pyarrow as pa, pyarrow.parquet as pq

OUT = sys.argv[1]                                   # fixtures/iceberg/warehouse_del
ROOT = "/tmp/pgc_ice_del"                           # stable recorded root
LOC = f"file://{ROOT}/db/t"

shutil.rmtree(OUT, ignore_errors=True)
os.makedirs(os.path.join(OUT, "db", "t", "metadata"))
os.makedirs(os.path.join(OUT, "db", "t", "data"))


# ---- Avro OCF encoding helpers --------------------------------------------
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


def ocf(schema_json, record_bytes):
    sync = b"\x00" * 16
    meta = zz(2) + s(b"avro.schema") + s(schema_json) + s(b"avro.codec") + s(b"null") + zz(0)
    return b"Obj\x01" + meta + sync + zz(1) + zz(len(record_bytes)) + record_bytes + sync


# a manifest_entry with a reduced data_file (the fields our decoder projects)
ENTRY_SCHEMA = json.dumps({"type": "record", "name": "manifest_entry", "fields": [
    {"name": "status", "type": "int"},
    {"name": "sequence_number", "type": ["null", "long"]},
    {"name": "data_file", "type": {"type": "record", "name": "df", "fields": [
        {"name": "content", "type": "int"},
        {"name": "file_path", "type": "string"},
        {"name": "file_format", "type": "string"},
        {"name": "record_count", "type": "long"},
        {"name": "file_size_in_bytes", "type": "long"}]}}]}).encode()

MFILE_SCHEMA = json.dumps({"type": "record", "name": "manifest_file", "fields": [
    {"name": "manifest_path", "type": "string"},
    {"name": "manifest_length", "type": "long"},
    {"name": "partition_spec_id", "type": "int"},
    {"name": "content", "type": "int"},
    {"name": "sequence_number", "type": "long"},
    {"name": "min_sequence_number", "type": "long"},
    {"name": "added_snapshot_id", "type": "long"},
    {"name": "added_files_count", "type": "int"},
    {"name": "existing_files_count", "type": "int"},
    {"name": "deleted_files_count", "type": "int"},
    {"name": "added_rows_count", "type": "long"},
    {"name": "existing_rows_count", "type": "long"},
    {"name": "deleted_rows_count", "type": "long"}]}).encode()


def entry(status, seq, content, path, fmt, rows, size):
    # seq None -> encode the sequence_number union as null (branch 0), so the
    # reader must inherit it from the manifest; else branch 1 with the value
    seqbytes = zz(0) if seq is None else (zz(1) + zz(seq))
    return zz(status) + seqbytes + zz(content) + s(path.encode()) + \
        s(fmt.encode()) + zz(rows) + zz(size)


def mfile(path, content, seq, snap, files, rows):
    return s(path.encode()) + zz(1000) + zz(0) + zz(content) + zz(seq) + zz(seq) + \
        zz(snap) + zz(files) + zz(0) + zz(0) + zz(rows) + zz(0) + zz(0)


# ---- the data and delete Parquet (real pyarrow) ---------------------------
data = pa.table({
    "id": pa.array([1, 2, 3, 4, 5], pa.int64()),
    "region": pa.array(["eu", "eu", "us", "us", "us"], pa.string()),
    "amount": pa.array([10, 20, 30, 40, 50], pa.int32()),
}, schema=pa.schema([
    pa.field("id", pa.int64(), metadata={b"PARQUET:field_id": b"1"}),
    pa.field("region", pa.string(), metadata={b"PARQUET:field_id": b"2"}),
    pa.field("amount", pa.int32(), metadata={b"PARQUET:field_id": b"3"}),
]))
# write in small row groups (2 rows each -> 3 groups for 5 rows) so the delete
# ordinals span row-group boundaries: position 1 is in group 0, position 3 in
# group 1, exercising pq_read_rows' cross-group file-ordinal accounting.
pq.write_table(data, os.path.join(OUT, "db", "t", "data", "data.parquet"),
               row_group_size=2)

DATA_PATH = f"{LOC}/data/data.parquet"
DEL_PATH = f"{LOC}/data/posdel.parquet"
DELETED_POS = [1, 3]                                 # drop rows 1 and 3 (id 2, id 4)
posdel = pa.table({
    "file_path": pa.array([DATA_PATH] * len(DELETED_POS), pa.string()),
    "pos": pa.array(DELETED_POS, pa.int64()),
}, schema=pa.schema([
    pa.field("file_path", pa.string(), nullable=False,
             metadata={b"PARQUET:field_id": b"2147483546"}),
    pa.field("pos", pa.int64(), nullable=False,
             metadata={b"PARQUET:field_id": b"2147483545"}),
]))
pq.write_table(posdel, os.path.join(OUT, "db", "t", "data", "posdel.parquet"))

# a second position-delete file whose file_path column names a DIFFERENT data
# file (not in the snapshot), to prove per-file scoping: applied against
# data.parquet it must delete nothing, because the paths do not match.
WRONG_DEL_PATH = f"{LOC}/data/posdel-wrong.parquet"
posdel_wrong = pa.table({
    "file_path": pa.array([f"{LOC}/data/OTHER.parquet"] * len(DELETED_POS), pa.string()),
    "pos": pa.array(DELETED_POS, pa.int64()),
}, schema=posdel.schema)
pq.write_table(posdel_wrong, os.path.join(OUT, "db", "t", "data", "posdel-wrong.parquet"))

# ---- defect #5: corrupt position-delete `pos` values (#644) ----------------
# A position delete's (file_path, pos) are both REQUIRED and pos is a
# non-negative ordinal. These three files exercise the guard.
# null pos: silently dropped by the old `!n1 && !n2`, leaving the row.
NULLPOS_PATH = f"{LOC}/data/posdel-nullpos.parquet"
pq.write_table(pa.table({
    "file_path": pa.array([DATA_PATH, DATA_PATH], pa.string()),
    "pos": pa.array([3, None], pa.int64()),          # pos 3 (id 4) valid, one null
}, schema=pa.schema([
    pa.field("file_path", pa.string(), nullable=False,
             metadata={b"PARQUET:field_id": b"2147483546"}),
    pa.field("pos", pa.int64(), nullable=True,        # nullable so None is writable
             metadata={b"PARQUET:field_id": b"2147483545"}),
])), os.path.join(OUT, "db", "t", "data", "posdel-nullpos.parquet"))

# negative pos: cast to a huge uint64 at apply time, silently inert.
NEGPOS_PATH = f"{LOC}/data/posdel-negpos.parquet"
pq.write_table(pa.table({
    "file_path": pa.array([DATA_PATH, DATA_PATH], pa.string()),
    "pos": pa.array([3, -1], pa.int64()),             # pos 3 valid, -1 corrupt
}, schema=posdel.schema), os.path.join(OUT, "db", "t", "data", "posdel-negpos.parquet"))

# pos == 0: the RISK control -- the first row is a LEGITIMATE delete target.
ZEROPOS_PATH = f"{LOC}/data/posdel-zeropos.parquet"
pq.write_table(pa.table({
    "file_path": pa.array([DATA_PATH], pa.string()),
    "pos": pa.array([0], pa.int64()),                 # deletes row 0 == id 1
}, schema=posdel.schema), os.path.join(OUT, "db", "t", "data", "posdel-zeropos.parquet"))

# ---- the manifests, manifest list, metadata -------------------------------
md = os.path.join(OUT, "db", "t", "metadata")
SNAP = 7766554433221100
DM = f"{LOC}/metadata/data-manifest.avro"
DATA_SEQ = 5
open(os.path.join(md, "data-manifest.avro"), "wb").write(
    ocf(ENTRY_SCHEMA, entry(1, DATA_SEQ, 0, DATA_PATH, "PARQUET", 5, 1000)))

schema = {"schema-id": 0, "type": "struct", "fields": [
    {"id": 1, "name": "id", "required": False, "type": "long"},
    {"id": 2, "name": "region", "required": False, "type": "string"},
    {"id": 3, "name": "amount", "required": False, "type": "int"}]}


def emit_variant(tag, del_content, del_seq, entry_seq="explicit", del_file=DEL_PATH):
    """Write a (delete manifest, manifest list, metadata) triple: a delete file
    of the given data_file content (1 position, 2 equality) at data sequence
    number del_seq, over the shared data manifest (content 0, seq 1). entry_seq
    "explicit" writes del_seq on the entry; None leaves it null so the reader must
    inherit del_seq from the manifest_file in the manifest list. del_file is the
    position-delete Parquet the entry points at."""
    xm_name = f"delete-manifest-{tag}.avro"
    ml_name = f"manifest-list-{tag}.avro"
    eseq = del_seq if entry_seq == "explicit" else None
    open(os.path.join(md, xm_name), "wb").write(
        ocf(ENTRY_SCHEMA, entry(1, eseq, del_content, del_file, "PARQUET",
                                len(DELETED_POS), 500)))
    xm = f"{LOC}/metadata/{xm_name}"
    sync = b"\x00" * 16
    meta = zz(2) + s(b"avro.schema") + s(MFILE_SCHEMA) + s(b"avro.codec") + s(b"null") + zz(0)
    ml = b"Obj\x01" + meta + sync
    for rec in (mfile(DM, 0, DATA_SEQ, SNAP, 1, 5),
                mfile(xm, 1, del_seq, SNAP, 1, len(DELETED_POS))):
        ml += zz(1) + zz(len(rec)) + rec + sync
    open(os.path.join(md, ml_name), "wb").write(ml)
    metadata = {
        "format-version": 2, "location": LOC, "current-schema-id": 0,
        "schemas": [schema], "current-snapshot-id": SNAP,
        "snapshots": [{
            "snapshot-id": SNAP, "sequence-number": max(DATA_SEQ, del_seq),
            "timestamp-ms": 0,
            "manifest-list": f"{LOC}/metadata/{ml_name}",
            "summary": {"operation": "overwrite"}, "schema-id": 0}],
    }
    open(os.path.join(md, f"{tag}.metadata.json"), "w").write(json.dumps(metadata))


# apply:   a position delete at seq 5, EQUAL to the data's seq 5 -> applies, per
#          the spec's data_seq <= delete_seq (the same-commit upsert case). This
#          is the boundary that distinguishes the correct >= from a strict >.
# noapply: a position delete at seq 4, OLDER than the data's seq 5 -> no-op.
# equality: an equality (content 2) delete -> refused, not applied.
emit_variant("apply", 1, DATA_SEQ)
emit_variant("noapply", 1, DATA_SEQ - 1)
emit_variant("equality", 2, DATA_SEQ)
# a position delete whose entry sequence number is NULL, inheriting seq 5 from
# the manifest (the way real writers record it); it applies over the seq-5 data
emit_variant("inherit", 1, DATA_SEQ, entry_seq=None)
# a position delete (applicable by sequence) whose rows name a DIFFERENT data
# file; per-file scoping means it must delete nothing from data.parquet
emit_variant("wrongpath", 1, DATA_SEQ, del_file=WRONG_DEL_PATH)

# defect #5: a position delete at the applicable seq 5 whose pos column is
# corrupt (null / negative) -- must be REFUSED (XX001), not silently dropped.
emit_variant("nullpos", 1, DATA_SEQ, del_file=NULLPOS_PATH)
emit_variant("negpos",  1, DATA_SEQ, del_file=NEGPOS_PATH)
# pos == 0 is valid (deletes the first row); proves the guard is `< 0`, not `<= 0`.
emit_variant("zeropos", 1, DATA_SEQ, del_file=ZEROPOS_PATH)

# spec violation: an EXISTING (status 0) data entry with a NULL sequence number.
# The spec inherits a null sequence number only for ADDED (status 1) entries; on
# an EXISTING entry it is corrupt, and inheriting the (too-new) manifest number
# could wrongly keep rows a delete should remove. The reader must refuse it.
sync = b"\x00" * 16
mlmeta = zz(2) + s(b"avro.schema") + s(MFILE_SCHEMA) + s(b"avro.codec") + s(b"null") + zz(0)
open(os.path.join(md, "data-manifest-badseq.avro"), "wb").write(
    ocf(ENTRY_SCHEMA, entry(0, None, 0, DATA_PATH, "PARQUET", 5, 1000)))
DMB = f"{LOC}/metadata/data-manifest-badseq.avro"
mlb = b"Obj\x01" + mlmeta + sync
recb = mfile(DMB, 0, DATA_SEQ, SNAP, 1, 5)
mlb += zz(1) + zz(len(recb)) + recb + sync
open(os.path.join(md, "manifest-list-badseq.avro"), "wb").write(mlb)
open(os.path.join(md, "badseq.metadata.json"), "w").write(json.dumps({
    "format-version": 2, "location": LOC, "current-schema-id": 0,
    "schemas": [schema], "current-snapshot-id": SNAP,
    "snapshots": [{"snapshot-id": SNAP, "sequence-number": DATA_SEQ, "timestamp-ms": 0,
                   "manifest-list": f"{LOC}/metadata/manifest-list-badseq.avro",
                   "summary": {"operation": "append"}, "schema-id": 0}]}))

# ===================== 4b: equality deletes (additive) ======================
# Everything below ADDS files; nothing written above is touched, so the merged
# 4a fixture bytes never churn. Design: design/ISSUE_388_PHASE4_DELETES.md, 4b.

# manifest_entry schema whose data_file carries equality_ids (Iceberg data_file
# field 135, list<int>). Only the equality variants use it; the 4a manifests
# keep their original schema (and its exact bytes).
ENTRY_SCHEMA_EQ = json.dumps({"type": "record", "name": "manifest_entry", "fields": [
    {"name": "status", "type": "int"},
    {"name": "sequence_number", "type": ["null", "long"]},
    {"name": "data_file", "type": {"type": "record", "name": "df", "fields": [
        {"name": "content", "type": "int"},
        {"name": "file_path", "type": "string"},
        {"name": "file_format", "type": "string"},
        {"name": "record_count", "type": "long"},
        {"name": "file_size_in_bytes", "type": "long"},
        {"name": "equality_ids",
         "type": ["null", {"type": "array", "items": "int"}]}]}}]}).encode()


def entry_eq(status, seq, content, path, fmt, rows, size, eq_ids):
    """One manifest entry against ENTRY_SCHEMA_EQ. eq_ids None encodes the
    union's null branch (required to be null for non-equality content; corrupt
    for content 2); a list encodes one array block of zig-zag ints, then the
    zero terminator, per the Avro array block protocol."""
    seqbytes = zz(0) if seq is None else (zz(1) + zz(seq))
    if eq_ids is None:
        eqb = zz(0)
    else:
        eqb = zz(1)
        if eq_ids:
            eqb += zz(len(eq_ids)) + b"".join(zz(i) for i in eq_ids)
        eqb += zz(0)
    return zz(status) + seqbytes + zz(content) + s(path.encode()) + \
        s(fmt.encode()) + zz(rows) + zz(size) + eqb


def ocf_multi(schema_json, records):
    """An OCF whose single block holds several records (ocf() writes one)."""
    sync = b"\x00" * 16
    meta = zz(2) + s(b"avro.schema") + s(schema_json) + s(b"avro.codec") + s(b"null") + zz(0)
    body = b"".join(records)
    return b"Obj\x01" + meta + sync + zz(len(records)) + zz(len(body)) + body + sync


def mfile_spec(path, content, seq, snap, files, rows, spec_id):
    """mfile() with an explicit partition_spec_id (mfile hardcodes 0)."""
    return s(path.encode()) + zz(1000) + zz(spec_id) + zz(content) + zz(seq) + zz(seq) + \
        zz(snap) + zz(files) + zz(0) + zz(0) + zz(rows) + zz(0) + zz(0)


# ---- the second data file (nulls) and the equality-delete Parquet files -----
# data2 exists for the null-matching arm: id 6 has region NULL. Same schema and
# field ids as data.parquet.
data2 = pa.table({
    "id": pa.array([6, 7], pa.int64()),
    "region": pa.array([None, "eu"], pa.string()),
    "amount": pa.array([60, 70], pa.int32()),
}, schema=data.schema)
pq.write_table(data2, os.path.join(OUT, "db", "t", "data", "data2.parquet"),
               row_group_size=2)
DATA2_PATH = f"{LOC}/data/data2.parquet"

F_ID = pa.field("id", pa.int64(), metadata={b"PARQUET:field_id": b"1"})
F_REGION = pa.field("region", pa.string(), metadata={b"PARQUET:field_id": b"2"})
F_AMOUNT = pa.field("amount", pa.int32(), metadata={b"PARQUET:field_id": b"3"})
F_EXTRA9 = pa.field("extra", pa.int64(), metadata={b"PARQUET:field_id": b"9"})


def write_eqdel(name, cols):
    """cols: list of (pa.field, values). Returns the recorded path."""
    t = pa.table({f.name: pa.array(v, f.type) for f, v in cols},
                 schema=pa.schema([f for f, _ in cols]))
    pq.write_table(t, os.path.join(OUT, "db", "t", "data", name))
    return f"{LOC}/data/{name}"


# ids [2, 4]: the value-match arm (and, at an equal seq, the boundary arm)
EQ_ID = write_eqdel("eqdel-id.parquet", [(F_ID, [2, 4])])
# id [5]: half of the two-file arm, and the equality half of the mixed arm
EQ_ID5 = write_eqdel("eqdel-id5.parquet", [(F_ID, [5])])
# region ['eu']: the other half of the two-file arm
EQ_REGION = write_eqdel("eqdel-region.parquet", [(F_REGION, ["eu"])])
# region [NULL]: null matches null (IS NULL semantics), and ONLY null
EQ_NULL = write_eqdel("eqdel-null.parquet", [(F_REGION, [None])])
# (id, region) rows (3,'eu') and (4,'us'): AND-not-OR -- (3,'eu') matches no row
EQ_MULTI = write_eqdel("eqdel-multi.parquet", [(F_ID, [3, 4]), (F_REGION, ["eu", "us"])])
# id 2 plus a deliberately WRONG amount: only equality_ids=[1] may define the
# match, so the wrong amount must be ignored and id 2 still deleted
EQ_EXTRA = write_eqdel("eqdel-extra.parquet", [(F_ID, [2]), (F_AMOUNT, [999])])
# a long column with field id 9, present here but absent from data.parquet
EQ_NINE = write_eqdel("eqdel-nine.parquet", [(F_EXTRA9, [42])])

# the equality variants' schema: the data fields plus a timestamp field (8, for
# the unsupported-type refusal) and a long field (9, absent from data.parquet,
# for the missing-column error). A separate dict so the 4a metadata stays
# byte-identical.
schema_eq = {"schema-id": 0, "type": "struct", "fields": [
    {"id": 1, "name": "id", "required": False, "type": "long"},
    {"id": 2, "name": "region", "required": False, "type": "string"},
    {"id": 3, "name": "amount", "required": False, "type": "int"},
    {"id": 8, "name": "created", "required": False, "type": "timestamp"},
    {"id": 9, "name": "extra", "required": False, "type": "long"}]}

# spec 0 is unpartitioned (equality deletes under it are GLOBAL); spec 1 is
# partitioned (an equality delete under it must be refused until phase 5)
PARTITION_SPECS = [
    {"spec-id": 0, "fields": []},
    {"spec-id": 1, "fields": [
        {"name": "region", "transform": "identity", "source-id": 2, "field-id": 1000}]},
]

# a second data manifest carrying data2.parquet at the same data seq 5
DM2 = f"{LOC}/metadata/data-manifest-2.avro"
open(os.path.join(md, "data-manifest-2.avro"), "wb").write(
    ocf(ENTRY_SCHEMA, entry(1, DATA_SEQ, 0, DATA2_PATH, "PARQUET", 2, 1000)))


def emit_eq_variant(tag, delete_entries, with_data2=False, del_spec_id=0,
                    extra_data=()):
    """delete_entries: list of (content, seq, path, eq_ids, nrows). One delete
    manifest holds them all; the manifest list points at the shared data
    manifest(s) plus that delete manifest (partition_spec_id del_spec_id).
    extra_data: further (data manifest path, row count) pairs at DATA_SEQ."""
    xm_name = f"delete-manifest-{tag}.avro"
    ml_name = f"manifest-list-{tag}.avro"
    recs = [entry_eq(1, seq, content, path, "PARQUET", nrows, 500, eq_ids)
            for (content, seq, path, eq_ids, nrows) in delete_entries]
    open(os.path.join(md, xm_name), "wb").write(ocf_multi(ENTRY_SCHEMA_EQ, recs))
    xm = f"{LOC}/metadata/{xm_name}"
    max_del_seq = max(seq for (_, seq, _, _, _) in delete_entries)
    sync = b"\x00" * 16
    meta = zz(2) + s(b"avro.schema") + s(MFILE_SCHEMA) + s(b"avro.codec") + s(b"null") + zz(0)
    ml = b"Obj\x01" + meta + sync
    mrecs = [mfile(DM, 0, DATA_SEQ, SNAP, 1, 5)]
    if with_data2:
        mrecs.append(mfile(DM2, 0, DATA_SEQ, SNAP, 1, 2))
    for (xdm, xrows) in extra_data:
        mrecs.append(mfile(xdm, 0, DATA_SEQ, SNAP, 1, xrows))
    mrecs.append(mfile_spec(xm, 1, max_del_seq, SNAP, len(delete_entries),
                            sum(n for (_, _, _, _, n) in delete_entries),
                            del_spec_id))
    for rec in mrecs:
        ml += zz(1) + zz(len(rec)) + rec + sync
    open(os.path.join(md, ml_name), "wb").write(ml)
    metadata = {
        "format-version": 2, "location": LOC, "current-schema-id": 0,
        "schemas": [schema_eq], "partition-specs": PARTITION_SPECS,
        "default-spec-id": 0, "current-snapshot-id": SNAP,
        "snapshots": [{
            "snapshot-id": SNAP, "sequence-number": max(DATA_SEQ, max_del_seq),
            "timestamp-ms": 0,
            "manifest-list": f"{LOC}/metadata/{ml_name}",
            "summary": {"operation": "overwrite"}, "schema-id": 0}],
    }
    open(os.path.join(md, f"{tag}.metadata.json"), "w").write(json.dumps(metadata))


EQ_SEQ = DATA_SEQ + 1            # strictly newer than the data: applies
# value arms (survivors justified inline; the data is rows 1..5 above, plus
# data2's (6, NULL, 60) and (7, 'eu', 70) where with_data2 is set)
emit_eq_variant("eqapply", [(2, EQ_SEQ, EQ_ID, [1], 2)])
#   deletes id in {2,4} -> survivors 1,3,5
emit_eq_variant("eqboundary", [(2, DATA_SEQ, EQ_ID, [1], 2)])
#   the SAME delete at seq EQUAL to the data's: the strict-< rule says it does
#   NOT apply (opposite boundary from position deletes) -> all 5 rows survive
emit_eq_variant("eqmulti", [(2, EQ_SEQ, EQ_MULTI, [1, 2], 2)])
#   (3,'eu') matches nothing (row 3 is 'us': AND, not OR); (4,'us') deletes row
#   4 -> survivors 1,2,3,5
emit_eq_variant("eqextra", [(2, EQ_SEQ, EQ_EXTRA, [1], 1)])
#   equality_ids=[1] only: the wrong amount (999) is ignored, id 2 deleted ->
#   survivors 1,3,4,5
emit_eq_variant("eqnull", [(2, EQ_SEQ, EQ_NULL, [2], 1)], with_data2=True)
#   region NULL matches ONLY id 6's NULL region -> survivors 1,2,3,4,5,7
emit_eq_variant("eqtwo", [(2, EQ_SEQ, EQ_ID5, [1], 1),
                          (2, EQ_SEQ, EQ_REGION, [2], 1)])
#   two delete files with different equality_ids: id=5 and region='eu' ->
#   deletes 1,2 (eu) and 5 -> survivors 3,4
emit_eq_variant("eqmixed", [(1, DATA_SEQ, DEL_PATH, None, 2),
                            (2, EQ_SEQ, EQ_ID5, [1], 1)])
#   position deletes (ordinals 1,3 = ids 2,4; seq 5 >= 5 applies) UNION the
#   equality delete of id 5 -> survivors 1,3

# ---- many equality-delete rows (#1817) --------------------------------------
# The reader once tested every data row against every delete row, so the cost
# grew with data rows x delete rows. This variant is big enough to show that
# cost in the probe's comparison count, and small enough to commit. One extra
# data file holds ids 1001..5000. Every 500th id has a NULL region; the others
# cycle eu/us/ap. Two delete files apply to it, plus to the five rows of
# data.parquet, which none of the keys match.
MANY_IDS = list(range(1001, 5001))
MANY_REGIONS = ["eu", "us", "ap"]


def many_region(i):
    return None if i % 500 == 0 else MANY_REGIONS[i % 3]


data_many = pa.table({
    "id": pa.array(MANY_IDS, pa.int64()),
    "region": pa.array([many_region(i) for i in MANY_IDS], pa.string()),
    "amount": pa.array([i % 1000 for i in MANY_IDS], pa.int32()),
}, schema=data.schema)
pq.write_table(data_many, os.path.join(OUT, "db", "t", "data", "data-many.parquet"))
DM_MANY = f"{LOC}/metadata/data-manifest-many.avro"
open(os.path.join(md, "data-manifest-many.avro"), "wb").write(
    ocf(ENTRY_SCHEMA, entry(1, DATA_SEQ, 0, f"{LOC}/data/data-many.parquet",
                            "PARQUET", len(MANY_IDS), 1000)))
# on id: 1990 keys that match nothing, then 10 that match, last, so a linear
# walk reaches them only after every miss
MANY_ID_HITS = [1001 + 37 * k for k in range(10)]
MANY_ID_KEYS = list(range(900001, 901991)) + MANY_ID_HITS
EQ_MANY_ID = write_eqdel("eqdel-many-id.parquet", [(F_ID, MANY_ID_KEYS)])
# on (id, region): 990 pairs with the right id and a wrong region (AND, not
# OR; id 2500's region is NULL, so its 'eu' pair is a value against a NULL),
# 8 exact pairs, 2 NULL-region pairs that match NULL-region rows, and 1
# NULL-region pair whose row has a region (NULL matches only NULL)
MANY_PAIR = [(i, next(r for r in MANY_REGIONS if r != many_region(i)))
             for i in range(2001, 2991)]
MANY_PAIR += [(i, many_region(i)) for i in range(3001, 3009)]
MANY_PAIR += [(1500, None), (4000, None), (1003, None)]
EQ_MANY_PAIR = write_eqdel("eqdel-many-pair.parquet",
                           [(F_ID, [p[0] for p in MANY_PAIR]),
                            (F_REGION, [p[1] for p in MANY_PAIR])])
emit_eq_variant("eqmany", [(2, EQ_SEQ, EQ_MANY_ID, [1], len(MANY_ID_KEYS)),
                           (2, EQ_SEQ, EQ_MANY_PAIR, [1, 2], len(MANY_PAIR))],
                extra_data=[(DM_MANY, len(MANY_IDS))])
# the oracle, computed rather than hand-listed: a row goes when its id is a
# key, or its (id, region) is a pair, with NULL equal only to NULL
_many_rows = [(i, r) for i, r in [(1, "eu"), (2, "eu"), (3, "us"), (4, "us"),
                                  (5, "us")]] + [(i, many_region(i)) for i in MANY_IDS]
_many_keys = set(MANY_ID_KEYS)
_many_pairs = set(MANY_PAIR)
MANY_SURVIVORS = [i for i, r in _many_rows
                  if i not in _many_keys and (i, r) not in _many_pairs]
assert len(MANY_SURVIVORS) == 5 + 4000 - 20, len(MANY_SURVIVORS)
# refusal / corruption arms
emit_eq_variant("eqnoids", [(2, EQ_SEQ, EQ_ID, None, 2)])
#   content=2 with null equality_ids is corrupt metadata (spec: required)
emit_eq_variant("eqpart", [(2, EQ_SEQ, EQ_ID, [1], 2)], del_spec_id=1)
#   a partition-scoped equality delete: refused (globalizing it over-deletes)
emit_eq_variant("eqtype", [(2, EQ_SEQ, EQ_ID, [8], 2)])
#   field 8 is a timestamp: no supported mapping, refused before any file opens
emit_eq_variant("eqmissing", [(2, EQ_SEQ, EQ_NINE, [9], 1)])
#   field 9 reads fine from the delete file but is absent from data.parquet:
#   the probe read errors loudly (the reader's missing-field-id error)
emit_eq_variant("eqescape", [(2, EQ_SEQ, "file:///etc/hostname", [1], 1)])
#   a delete path outside the table root: the path boundary refuses it
emit_eq_variant("eqdotdot", [(2, EQ_SEQ, f"{LOC}/../../../../etc/hostname",
                              [1], 1)])
#   a delete path that starts under the table root then climbs out with "..":
#   passes the recorded-root prefix check, then the containment must reject the
#   ".." (canonicalize for a local root, the ".."-segment guard for a remote one)
emit_eq_variant("eqpctdot", [(2, EQ_SEQ,
                              f"{LOC}/%2e%2e/%2e%2e/%2e%2e/%2e%2e/etc/hostname",
                              [1], 1)])
#   the same climb-out with an encoded NUL in front of it. %00 decodes to a NUL
#   byte, and every check past that point is NUL-terminated string work: the next
#   decode pass measures with strlen and the ".."-segment walk uses strchr, so
#   both stop at the NUL and never reach the "../" behind it. The guard must
#   refuse on the encoded NUL itself rather than try to scan past it.
emit_eq_variant("eqnulpct", [(2, EQ_SEQ,
                              f"{LOC}/%00%2e%2e/%2e%2e/%2e%2e/%2e%2e/etc/hostname",
                              [1], 1)])
#   the same climb-out, but each ".." percent-encoded as "%2e%2e": a literal
#   ".."-segment guard sees no "..", yet an http(s) origin/proxy that decodes
#   "%2e%2e" -> ".." before serving would escape the table location. The remote
#   guard must decode-and-reject it (harmless on a local root: "%2e%2e" is just
#   a directory name that does not exist, so realpath fails rather than escapes)

# ---- audit arms (multi-agent adversarial audit of 4b) ----------------------

# equality_ids carrying a value beyond int32: a silent (int32) truncation would
# alias field 2^32+2 onto the real field 2 and delete rows the manifest never
# named; the decoder must refuse the manifest instead
emit_eq_variant("eqbigid", [(2, EQ_SEQ, EQ_REGION, [(1 << 32) + 2], 1)])

# a manifest list whose schema OMITS partition_spec_id: the field would decode
# as 0 (the unpartitioned spec) and silently defeat the partition-scope guard;
# the reader must refuse to scope an equality delete without it
MFILE_SCHEMA_NOSPEC = json.dumps({"type": "record", "name": "manifest_file", "fields": [
    {"name": "manifest_path", "type": "string"},
    {"name": "manifest_length", "type": "long"},
    {"name": "content", "type": "int"},
    {"name": "sequence_number", "type": "long"},
    {"name": "min_sequence_number", "type": "long"},
    {"name": "added_snapshot_id", "type": "long"},
    {"name": "added_files_count", "type": "int"},
    {"name": "existing_files_count", "type": "int"},
    {"name": "deleted_files_count", "type": "int"},
    {"name": "added_rows_count", "type": "long"},
    {"name": "existing_rows_count", "type": "long"},
    {"name": "deleted_rows_count", "type": "long"}]}).encode()


def mfile_nospec(path, content, seq, snap, files, rows):
    return s(path.encode()) + zz(1000) + zz(content) + zz(seq) + zz(seq) + \
        zz(snap) + zz(files) + zz(0) + zz(0) + zz(rows) + zz(0) + zz(0)


def emit_nospec_variant():
    tag = "eqnospec"
    xm_name = f"delete-manifest-{tag}.avro"
    open(os.path.join(md, xm_name), "wb").write(
        ocf_multi(ENTRY_SCHEMA_EQ, [entry_eq(1, EQ_SEQ, 2, EQ_ID, "PARQUET", 2, 500, [1])]))
    xm = f"{LOC}/metadata/{xm_name}"
    sync = b"\x00" * 16
    # the data manifest keeps the full schema; only the delete manifest's
    # manifest-list record omits partition_spec_id, so the two schemas differ
    # and the list needs the reduced schema for both rows -- write BOTH rows
    # under the reduced schema (the data row's spec id is never consulted)
    meta = zz(2) + s(b"avro.schema") + s(MFILE_SCHEMA_NOSPEC) + s(b"avro.codec") + s(b"null") + zz(0)
    ml = b"Obj\x01" + meta + sync
    for rec in (mfile_nospec(DM, 0, DATA_SEQ, SNAP, 1, 5),
                mfile_nospec(xm, 1, EQ_SEQ, SNAP, 1, 2)):
        ml += zz(1) + zz(len(rec)) + rec + sync
    open(os.path.join(md, f"manifest-list-{tag}.avro"), "wb").write(ml)
    metadata = {
        "format-version": 2, "location": LOC, "current-schema-id": 0,
        "schemas": [schema_eq], "partition-specs": PARTITION_SPECS,
        "default-spec-id": 0, "current-snapshot-id": SNAP,
        "snapshots": [{
            "snapshot-id": SNAP, "sequence-number": EQ_SEQ, "timestamp-ms": 0,
            "manifest-list": f"{LOC}/metadata/manifest-list-{tag}.avro",
            "summary": {"operation": "overwrite"}, "schema-id": 0}],
    }
    open(os.path.join(md, f"{tag}.metadata.json"), "w").write(json.dumps(metadata))


emit_nospec_variant()

# a partition-specs entry that MATCHES the delete's spec id but lacks the
# required "fields" key: treating it as unpartitioned would silently globalize
# a possibly partition-scoped delete; the reader must refuse (corrupt metadata)
def emit_nofields_variant():
    tag = "eqnofields"
    # reuse eqapply's delete manifest and list (spec id 0); only the metadata
    # differs, its spec 0 entry carrying no "fields"
    metadata = {
        "format-version": 2, "location": LOC, "current-schema-id": 0,
        "schemas": [schema_eq],
        "partition-specs": [{"spec-id": 0}],
        "default-spec-id": 0, "current-snapshot-id": SNAP,
        "snapshots": [{
            "snapshot-id": SNAP, "sequence-number": EQ_SEQ, "timestamp-ms": 0,
            "manifest-list": f"{LOC}/metadata/manifest-list-eqapply.avro",
            "summary": {"operation": "overwrite"}, "schema-id": 0}],
    }
    open(os.path.join(md, f"{tag}.metadata.json"), "w").write(json.dumps(metadata))


emit_nofields_variant()

# an equality delete that is never applicable (its sequence number does not
# exceed any data file's) must be SKIPPED entirely, not validated: this variant
# reuses the unsupported-type delete (field 8, timestamp) at the data's own
# sequence number, so a reader that validates ineligible deletes refuses where
# the correct result is all five rows untouched
emit_eq_variant("eqstaletype", [(2, DATA_SEQ, EQ_ID, [8], 2)])

# a v1-shaped manifest: no sequence_number column in the entry schema at all,
# and an EXISTING (status 0) entry. The spec defaults every file's sequence
# number to 0 when the column is absent (a v1 manifest), unlike a v2 manifest
# whose EXISTING entry carries an explicit null (corrupt, the badseq arm).
ENTRY_SCHEMA_V1 = json.dumps({"type": "record", "name": "manifest_entry", "fields": [
    {"name": "status", "type": "int"},
    {"name": "data_file", "type": {"type": "record", "name": "df", "fields": [
        {"name": "content", "type": "int"},
        {"name": "file_path", "type": "string"},
        {"name": "file_format", "type": "string"},
        {"name": "record_count", "type": "long"},
        {"name": "file_size_in_bytes", "type": "long"}]}}]}).encode()


def entry_v1(status, content, path, fmt, rows, size):
    return zz(status) + zz(content) + s(path.encode()) + \
        s(fmt.encode()) + zz(rows) + zz(size)


def emit_v1seq_variant():
    tag = "v1seq"
    open(os.path.join(md, f"data-manifest-{tag}.avro"), "wb").write(
        ocf(ENTRY_SCHEMA_V1, entry_v1(0, 0, DATA_PATH, "PARQUET", 5, 1000)))
    dmv = f"{LOC}/metadata/data-manifest-{tag}.avro"
    sync = b"\x00" * 16
    meta = zz(2) + s(b"avro.schema") + s(MFILE_SCHEMA) + s(b"avro.codec") + s(b"null") + zz(0)
    ml = b"Obj\x01" + meta + sync
    rec = mfile(dmv, 0, 0, SNAP, 1, 5)
    ml += zz(1) + zz(len(rec)) + rec + sync
    open(os.path.join(md, f"manifest-list-{tag}.avro"), "wb").write(ml)
    metadata = {
        "format-version": 2, "location": LOC, "current-schema-id": 0,
        "schemas": [schema], "current-snapshot-id": SNAP,
        "snapshots": [{
            "snapshot-id": SNAP, "sequence-number": DATA_SEQ, "timestamp-ms": 0,
            "manifest-list": f"{LOC}/metadata/manifest-list-{tag}.avro",
            "summary": {"operation": "append"}, "schema-id": 0}],
    }
    open(os.path.join(md, f"{tag}.metadata.json"), "w").write(json.dumps(metadata))


emit_v1seq_variant()

# =============== 4c: v3 deletion vectors, Puffin files (additive) ===========
# As with 4b: everything below only ADDS files. Puffin blobs are built from
# pyroaring's portable 64-bit roaring serialization (verified byte-for-byte
# against the RoaringFormatSpec during 4c research) wrapped per the Iceberg
# Puffin spec: len(4,BE) | magic D1 D3 39 64 | vector | CRC-32(4,BE, zlib
# polynomial, over magic+vector). See design/ISSUE_388_PHASE4_DELETES.md (4c).
import struct
import zlib
from pyroaring import BitMap64

# manifest_entry schema whose data_file carries the three v3 DV fields
ENTRY_SCHEMA_DV = json.dumps({"type": "record", "name": "manifest_entry", "fields": [
    {"name": "status", "type": "int"},
    {"name": "sequence_number", "type": ["null", "long"]},
    {"name": "data_file", "type": {"type": "record", "name": "df", "fields": [
        {"name": "content", "type": "int"},
        {"name": "file_path", "type": "string"},
        {"name": "file_format", "type": "string"},
        {"name": "record_count", "type": "long"},
        {"name": "file_size_in_bytes", "type": "long"},
        {"name": "referenced_data_file", "type": ["null", "string"]},
        {"name": "content_offset", "type": ["null", "long"]},
        {"name": "content_size_in_bytes", "type": ["null", "long"]}]}}]}).encode()


def entry_dv(status, seq, content, path, fmt, rows, size, ref, coff, csize):
    """One manifest entry against ENTRY_SCHEMA_DV. ref/coff/csize None encode
    the unions' null branch (a plain Parquet position-delete entry)."""
    seqb = zz(0) if seq is None else (zz(1) + zz(seq))
    refb = zz(0) if ref is None else (zz(1) + s(ref.encode()))
    coffb = zz(0) if coff is None else (zz(1) + zz(coff))
    csb = zz(0) if csize is None else (zz(1) + zz(csize))
    return zz(status) + seqb + zz(content) + s(path.encode()) + \
        s(fmt.encode()) + zz(rows) + zz(size) + refb + coffb + csb


DV_MAGIC = b"\xD1\xD3\x39\x64"


def dv_vector(positions, run_opt=False):
    bm = BitMap64(positions)
    if run_opt:
        bm.run_optimize()
    return bm.serialize(), len(bm)


def dv_blob(vec, bad_magic=False, bad_crc=False):
    magic = b"\xD1\xD3\x39\x65" if bad_magic else DV_MAGIC
    body = magic + vec
    crc = zlib.crc32(body) & 0xFFFFFFFF
    if bad_crc:
        crc ^= 0xFF
    return struct.pack(">I", len(body)) + body + struct.pack(">I", crc)


def puffin(blobs):
    """blobs: list of {data, ref, card, [codec], [type]}. Returns
    (file_bytes, [(offset, length), ...] in blob order)."""
    out = b"PFA1"
    metas = []
    locs = []
    for b in blobs:
        off = len(out)
        out += b["data"]
        locs.append((off, len(b["data"])))
        m = {"type": b.get("type", "deletion-vector-v1"), "fields": [],
             "snapshot-id": -1, "sequence-number": -1,
             "offset": off, "length": len(b["data"]),
             "properties": {"referenced-data-file": b["ref"],
                            "cardinality": str(b["card"])}}
        if b.get("codec"):
            m["compression-codec"] = b["codec"]
        metas.append(m)
    payload = json.dumps({"blobs": metas}).encode()
    out += b"PFA1" + payload + struct.pack("<i", len(payload)) + \
        b"\x00\x00\x00\x00" + b"PFA1"
    return out, locs


def flags_set_bit0(data):
    """Set the footer flags 'compressed' bit without recompressing anything;
    a reader that ignores flags would misparse, one that checks refuses."""
    ba = bytearray(data)
    ba[-8] |= 1
    return bytes(ba)


# a Parquet position delete that targets data2.parquet ordinal 0 (id 6), for
# the per-file-supersede arm: a DV on data.parquet must not disable it
posdel_d2 = pa.table({
    "file_path": pa.array([DATA2_PATH], pa.string()),
    "pos": pa.array([0], pa.int64()),
}, schema=posdel.schema)
pq.write_table(posdel_d2, os.path.join(OUT, "db", "t", "data", "posdel-data2.parquet"))
POSDEL_D2 = f"{LOC}/data/posdel-data2.parquet"


def emit_dv_variant(tag, dv_specs, extra_entries=(), with_data2=False,
                    fmt_version=3, mutate=None):
    """dv_specs: list of {positions, ref, seq, [run_opt], [bad_magic],
    [bad_crc], [codec], [rows] (record_count override), [coff_shift],
    [entry_path] (file_path override), [noref]}. One Puffin file carries all of
    the variant's blobs. extra_entries: raw ENTRY_SCHEMA_DV entry bytes
    appended to the delete manifest (e.g. Parquet posdel entries). mutate: a
    function applied to the finished Puffin bytes (e.g. flags_set_bit0)."""
    blobs = []
    for sp in dv_specs:
        vec, card = dv_vector(sp["positions"], sp.get("run_opt", False))
        blobs.append({"data": dv_blob(vec, sp.get("bad_magic", False),
                                      sp.get("bad_crc", False)),
                      "ref": sp["ref"], "card": card,
                      "codec": sp.get("codec")})
    pf, locs = puffin(blobs)
    if mutate is not None:
        pf = mutate(pf)
    pf_name = f"dv-{tag}.puffin"
    open(os.path.join(OUT, "db", "t", "data", pf_name), "wb").write(pf)
    PF_PATH = f"{LOC}/data/{pf_name}"
    recs = []
    for sp, (off, ln), b in zip(dv_specs, locs, blobs):
        card = int(b["card"])
        recs.append(entry_dv(
            1, sp["seq"], 1, sp.get("entry_path", PF_PATH), "PUFFIN",
            sp.get("rows", card), len(pf),
            None if sp.get("noref") else sp["ref"],
            off + sp.get("coff_shift", 0), ln))
    recs.extend(extra_entries)
    xm_name = f"delete-manifest-{tag}.avro"
    open(os.path.join(md, xm_name), "wb").write(ocf_multi(ENTRY_SCHEMA_DV, recs))
    xm = f"{LOC}/metadata/{xm_name}"
    max_seq = max(sp["seq"] for sp in dv_specs)
    sync = b"\x00" * 16
    meta = zz(2) + s(b"avro.schema") + s(MFILE_SCHEMA) + s(b"avro.codec") + s(b"null") + zz(0)
    ml = b"Obj\x01" + meta + sync
    mrecs = [mfile(DM, 0, DATA_SEQ, SNAP, 1, 5)]
    if with_data2:
        mrecs.append(mfile(DM2, 0, DATA_SEQ, SNAP, 1, 2))
    mrecs.append(mfile(xm, 1, max_seq, SNAP, len(recs), 0))
    for rec in mrecs:
        ml += zz(1) + zz(len(rec)) + rec + sync
    open(os.path.join(md, f"manifest-list-{tag}.avro"), "wb").write(ml)
    metadata = {
        "format-version": fmt_version, "location": LOC, "current-schema-id": 0,
        "schemas": [schema], "partition-specs": [{"spec-id": 0, "fields": []}],
        "default-spec-id": 0, "current-snapshot-id": SNAP,
        "snapshots": [{
            "snapshot-id": SNAP, "sequence-number": max(DATA_SEQ, max_seq),
            "timestamp-ms": 0,
            "manifest-list": f"{LOC}/metadata/manifest-list-{tag}.avro",
            "summary": {"operation": "overwrite"}, "schema-id": 0}],
    }
    open(os.path.join(md, f"{tag}.metadata.json"), "w").write(json.dumps(metadata))


# value arms (data rows 1..5; DV positions are file ordinals)
emit_dv_variant("dvapply", [{"positions": [1, 3], "ref": DATA_PATH, "seq": DATA_SEQ}])
#   THE <= boundary: DV at the data's own seq applies -> ids 2,4 gone -> 1,3,5
emit_dv_variant("dvnoapply", [{"positions": [1, 3], "ref": DATA_PATH,
                               "seq": DATA_SEQ - 1}])
#   strictly older DV applies to nothing -> all 5 rows
emit_dv_variant("dvsupersede", [{"positions": [1], "ref": DATA_PATH, "seq": DATA_SEQ}],
                extra_entries=[entry_dv(1, DATA_SEQ, 1, DEL_PATH, "PARQUET",
                                        2, 500, None, None, None)])
#   an applicable DV supersedes the (also applicable) Parquet posdel {1,3} for
#   the same data file: only ordinal 1 drops -> 1,3,4,5 (union would drop 3)
emit_dv_variant("dvother", [{"positions": [1], "ref": DATA_PATH, "seq": DATA_SEQ}],
                extra_entries=[entry_dv(1, DATA_SEQ, 1, POSDEL_D2, "PARQUET",
                                        1, 500, None, None, None)],
                with_data2=True)
#   supersede is per data file: the posdel on data2 (ordinal 0 -> id 6) still
#   applies alongside the DV on data -> 1,3,4,5,7
emit_dv_variant("dvwide", [{"positions": [1] + list(range(65536, 70000)) +
                            [(1 << 32) + 5],
                            "ref": DATA_PATH, "seq": DATA_SEQ}])
#   run-form containers plus a second 64-bit bucket; only ordinal 1 is in
#   range -> 1,3,4,5
emit_dv_variant("dvrun", [{"positions": list(range(2, 100)), "ref": DATA_PATH,
                           "seq": DATA_SEQ, "run_opt": True}])
#   cookie 12347 with a run container; ordinals 2,3,4 drop -> ids 1,2
emit_dv_variant("dvbitset", [{"positions": list(range(0, 10000, 2)),
                              "ref": DATA_PATH, "seq": DATA_SEQ}])
#   a bitset container (cardinality 5000 > 4096); ordinals 0,2,4 drop -> 2,4
emit_dv_variant("dvtwo", [{"positions": [1], "ref": DATA_PATH, "seq": DATA_SEQ},
                          {"positions": [0], "ref": DATA2_PATH, "seq": DATA_SEQ}],
                with_data2=True)
#   two DV blobs in ONE Puffin file, each for its own data file -> 1,3,4,5,7
# refusal / corruption arms
emit_dv_variant("dvdup", [{"positions": [1], "ref": DATA_PATH, "seq": DATA_SEQ},
                          {"positions": [3], "ref": DATA_PATH, "seq": DATA_SEQ}])
#   two DV entries for ONE data file: the spec allows readers to refuse
emit_dv_variant("dvbadcrc", [{"positions": [1], "ref": DATA_PATH,
                              "seq": DATA_SEQ, "bad_crc": True}])
emit_dv_variant("dvbadmagic", [{"positions": [1], "ref": DATA_PATH,
                                "seq": DATA_SEQ, "bad_magic": True}])
emit_dv_variant("dvoffmismatch", [{"positions": [1], "ref": DATA_PATH,
                                   "seq": DATA_SEQ, "coff_shift": 4}])
#   manifest content_offset disagrees with the footer's offset
emit_dv_variant("dvnoref", [{"positions": [1], "ref": DATA_PATH,
                             "seq": DATA_SEQ, "noref": True}])
emit_dv_variant("dvbadcount", [{"positions": [1], "ref": DATA_PATH,
                                "seq": DATA_SEQ, "rows": 99}])
#   record_count must equal the DV cardinality
emit_dv_variant("dvcompressed", [{"positions": [1], "ref": DATA_PATH,
                                  "seq": DATA_SEQ, "codec": "zstd"}])
#   deletion-vector-v1 must not declare a compression codec
emit_dv_variant("dvflags", [{"positions": [1], "ref": DATA_PATH,
                             "seq": DATA_SEQ}], mutate=flags_set_bit0)
#   a compressed footer is refused, not misparsed
emit_dv_variant("dvv2", [{"positions": [1], "ref": DATA_PATH,
                          "seq": DATA_SEQ}], fmt_version=2)
#   deletion vectors are v3-only; a v2 table carrying one is refused
emit_dv_variant("dvescape", [{"positions": [1], "ref": DATA_PATH,
                              "seq": DATA_SEQ,
                              "entry_path": "file:///etc/hostname"}])
#   a Puffin path outside the table root is stopped by the path boundary

# ---- 4c audit arms (multi-agent adversarial audit findings) ----------------

def emit_dv_raw(tag, blob_bytes, ref, card, manifest_off, manifest_size,
                footer_off, footer_size, extra_pad=b""):
    """A DV variant with hand-controlled offsets: the footer blob metadata says
    (footer_off, footer_size) and the manifest entry says (manifest_off,
    manifest_size), independently of where blob_bytes actually sits. For the
    bounds-check arms, where the recorded offsets are deliberately impossible."""
    out = b"PFA1"
    real_off = len(out)
    out += blob_bytes + extra_pad
    payload = json.dumps({"blobs": [{
        "type": "deletion-vector-v1", "fields": [],
        "snapshot-id": -1, "sequence-number": -1,
        "offset": footer_off, "length": footer_size,
        "properties": {"referenced-data-file": ref,
                       "cardinality": str(card)}}]}).encode()
    out += b"PFA1" + payload + struct.pack("<i", len(payload)) + \
        b"\x00\x00\x00\x00" + b"PFA1"
    pf_name = f"dv-{tag}.puffin"
    open(os.path.join(OUT, "db", "t", "data", pf_name), "wb").write(out)
    PF_PATH = f"{LOC}/data/{pf_name}"
    rec = entry_dv(1, DATA_SEQ, 1, PF_PATH, "PUFFIN", card, len(out),
                   ref, manifest_off, manifest_size)
    xm_name = f"delete-manifest-{tag}.avro"
    open(os.path.join(md, xm_name), "wb").write(ocf_multi(ENTRY_SCHEMA_DV, [rec]))
    xm = f"{LOC}/metadata/{xm_name}"
    sync = b"\x00" * 16
    meta = zz(2) + s(b"avro.schema") + s(MFILE_SCHEMA) + s(b"avro.codec") + s(b"null") + zz(0)
    ml = b"Obj\x01" + meta + sync
    for rc in (mfile(DM, 0, DATA_SEQ, SNAP, 1, 5),
               mfile(xm, 1, DATA_SEQ, SNAP, 1, 0)):
        ml += zz(1) + zz(len(rc)) + rc + sync
    open(os.path.join(md, f"manifest-list-{tag}.avro"), "wb").write(ml)
    open(os.path.join(md, f"{tag}.metadata.json"), "w").write(json.dumps({
        "format-version": 3, "location": LOC, "current-schema-id": 0,
        "schemas": [schema], "partition-specs": [{"spec-id": 0, "fields": []}],
        "default-spec-id": 0, "current-snapshot-id": SNAP,
        "snapshots": [{"snapshot-id": SNAP, "sequence-number": DATA_SEQ,
                       "timestamp-ms": 0,
                       "manifest-list": f"{LOC}/metadata/manifest-list-{tag}.avro",
                       "summary": {"operation": "overwrite"}, "schema-id": 0}]}))


# a content_offset + content_size that would overflow int64 if added: each is
# ~2^62, so the sum wraps negative. A bounds check that adds the operands would
# pass it into a wild-pointer read; testing each operand against the file size
# refuses it. The footer blob repeats the same impossible numbers so the
# manifest/footer cross-check is satisfied first.
_vec, _card = dv_vector([1])
_blob = dv_blob(_vec)
_BIG = 0x4000000000000000
emit_dv_raw("dvbigoff", _blob, DATA_PATH, _card, _BIG, _BIG, _BIG, _BIG)

# a run container whose start + length exceeds the 16-bit container range: the
# second position would carry into the container-key bits and fabricate an
# ordinal in another container. Hand-build the roaring bytes (pyroaring will
# not emit an invalid one): one bucket, key 0; cookie 12347 (run), one
# container marked as run, run (start=65535, length-1=1) -> 65535, 65536.
def _roaring_bad_run():
    v = struct.pack("<Q", 1)              # nbuckets = 1
    v += struct.pack("<I", 0)             # bucket key 0
    v += struct.pack("<I", 12347)         # cookie SERIAL_COOKIE (has runs), size-1=0 in high bits
    v += bytes([0x01])                    # run bitset: container 0 is a run
    v += struct.pack("<HH", 0, 1)         # descriptive header: key 0, cardinality-1 = 1
    v += struct.pack("<H", 1)             # one run
    v += struct.pack("<HH", 65535, 1)     # run start 65535, length-1 = 1 -> 65535, 65536
    return v


_badrun = dv_blob(_roaring_bad_run())
# card=2 so the record_count check is not what fires; the run overflow is
emit_dv_raw("dvrunoverflow", _badrun, DATA_PATH, 2, 4, len(_badrun), 4,
            len(_badrun))

# two DV entries for one data file, referenced with and without the file://
# scheme: the per-file merge strips the scheme, so the one-DV-per-file check
# must too, or the union of both vectors would apply
_ref_stripped = DATA_PATH.replace("file://", "")
_blob_a = dv_blob(dv_vector([1])[0])
_blob_b = dv_blob(dv_vector([3])[0])


def emit_dvdupscheme():
    tag = "dvdupscheme"
    out = b"PFA1"
    off_a = len(out); out += _blob_a
    off_b = len(out); out += _blob_b
    payload = json.dumps({"blobs": [
        {"type": "deletion-vector-v1", "fields": [], "snapshot-id": -1,
         "sequence-number": -1, "offset": off_a, "length": len(_blob_a),
         "properties": {"referenced-data-file": DATA_PATH, "cardinality": "1"}},
        {"type": "deletion-vector-v1", "fields": [], "snapshot-id": -1,
         "sequence-number": -1, "offset": off_b, "length": len(_blob_b),
         "properties": {"referenced-data-file": _ref_stripped, "cardinality": "1"}},
    ]}).encode()
    out += b"PFA1" + payload + struct.pack("<i", len(payload)) + \
        b"\x00\x00\x00\x00" + b"PFA1"
    open(os.path.join(OUT, "db", "t", "data", f"dv-{tag}.puffin"), "wb").write(out)
    PF = f"{LOC}/data/dv-{tag}.puffin"
    recs = [entry_dv(1, DATA_SEQ, 1, PF, "PUFFIN", 1, len(out),
                     DATA_PATH, off_a, len(_blob_a)),
            entry_dv(1, DATA_SEQ, 1, PF, "PUFFIN", 1, len(out),
                     _ref_stripped, off_b, len(_blob_b))]
    xm_name = f"delete-manifest-{tag}.avro"
    open(os.path.join(md, xm_name), "wb").write(ocf_multi(ENTRY_SCHEMA_DV, recs))
    xm = f"{LOC}/metadata/{xm_name}"
    sync = b"\x00" * 16
    meta = zz(2) + s(b"avro.schema") + s(MFILE_SCHEMA) + s(b"avro.codec") + s(b"null") + zz(0)
    ml = b"Obj\x01" + meta + sync
    for rc in (mfile(DM, 0, DATA_SEQ, SNAP, 1, 5),
               mfile(xm, 1, DATA_SEQ, SNAP, 2, 0)):
        ml += zz(1) + zz(len(rc)) + rc + sync
    open(os.path.join(md, f"manifest-list-{tag}.avro"), "wb").write(ml)
    open(os.path.join(md, f"{tag}.metadata.json"), "w").write(json.dumps({
        "format-version": 3, "location": LOC, "current-schema-id": 0,
        "schemas": [schema], "partition-specs": [{"spec-id": 0, "fields": []}],
        "default-spec-id": 0, "current-snapshot-id": SNAP,
        "snapshots": [{"snapshot-id": SNAP, "sequence-number": DATA_SEQ,
                       "timestamp-ms": 0,
                       "manifest-list": f"{LOC}/metadata/manifest-list-{tag}.avro",
                       "summary": {"operation": "overwrite"}, "schema-id": 0}]}))


emit_dvdupscheme()

# =============== phase 5: partition-scoped equality deletes ==================
# A table partitioned by identity(region) (spec 1). An equality delete carries
# a partition tuple and applies only to data files of the same partition. The
# manifest entry's data_file now includes the partition struct, so the reader
# decodes the typed tuple and matches it. grp is 9 on every row, so a value
# match on grp alone cannot distinguish partitions -- only the partition
# scoping does, which is the point. See design/ISSUE_388_PHASE5_PARTITIONS.md.
ENTRY_SCHEMA_PART = json.dumps({"type": "record", "name": "manifest_entry", "fields": [
    {"name": "status", "type": "int"},
    {"name": "sequence_number", "type": ["null", "long"]},
    {"name": "data_file", "type": {"type": "record", "name": "dfp", "fields": [
        {"name": "content", "type": "int"},
        {"name": "file_path", "type": "string"},
        {"name": "file_format", "type": "string"},
        {"name": "record_count", "type": "long"},
        {"name": "file_size_in_bytes", "type": "long"},
        {"name": "partition", "type": {"type": "record", "name": "pt", "fields": [
            {"name": "region", "type": ["null", "string"]}]}},
        {"name": "equality_ids",
         "type": ["null", {"type": "array", "items": "int"}]}]}}]}).encode()

# a partition struct with a DOUBLE field, for the uncomparable-cell arm
ENTRY_SCHEMA_PARTF = json.dumps({"type": "record", "name": "manifest_entry", "fields": [
    {"name": "status", "type": "int"},
    {"name": "sequence_number", "type": ["null", "long"]},
    {"name": "data_file", "type": {"type": "record", "name": "dfpf", "fields": [
        {"name": "content", "type": "int"},
        {"name": "file_path", "type": "string"},
        {"name": "file_format", "type": "string"},
        {"name": "record_count", "type": "long"},
        {"name": "file_size_in_bytes", "type": "long"},
        {"name": "partition", "type": {"type": "record", "name": "ptf", "fields": [
            {"name": "amt", "type": ["null", "double"]}]}},
        {"name": "equality_ids",
         "type": ["null", {"type": "array", "items": "int"}]}]}}]}).encode()


def entry_part(status, seq, content, path, rows, size, region, eq_ids,
               schema_double=False, dval=0.0):
    seqb = zz(1) + zz(seq)
    if schema_double:
        partb = zz(1) + struct.pack("<d", dval)   # union[null,double] branch 1
    else:
        partb = zz(0) if region is None else (zz(1) + s(region.encode()))
    if eq_ids is None:
        eqb = zz(0)
    else:
        eqb = zz(1) + zz(len(eq_ids)) + b"".join(zz(i) for i in eq_ids) + zz(0)
    return zz(status) + seqb + zz(content) + s(path.encode()) + \
        s(b"PARQUET") + zz(rows) + zz(size) + partb + eqb


# id-partitioned data: region eu (ids 1,2) and region us (ids 3,4,5), grp=9 all
P_SCHEMA = pa.schema([
    pa.field("id", pa.int64(), metadata={b"PARQUET:field_id": b"1"}),
    pa.field("region", pa.string(), metadata={b"PARQUET:field_id": b"2"}),
    pa.field("grp", pa.int32(), metadata={b"PARQUET:field_id": b"3"}),
])
pq.write_table(pa.table({"id": pa.array([1, 2], pa.int64()),
                         "region": pa.array(["eu", "eu"], pa.string()),
                         "grp": pa.array([9, 9], pa.int32())}, schema=P_SCHEMA),
               os.path.join(OUT, "db", "t", "data", "part-eu.parquet"))
pq.write_table(pa.table({"id": pa.array([3, 4, 5], pa.int64()),
                         "region": pa.array(["us", "us", "us"], pa.string()),
                         "grp": pa.array([9, 9, 9], pa.int32())}, schema=P_SCHEMA),
               os.path.join(OUT, "db", "t", "data", "part-us.parquet"))
PART_EU = f"{LOC}/data/part-eu.parquet"
PART_US = f"{LOC}/data/part-us.parquet"
# the equality-delete Parquet: grp column (field id 3), value 9
EQ_GRP = write_eqdel("eqdel-grp.parquet",
                     [(pa.field("grp", pa.int32(),
                                metadata={b"PARQUET:field_id": b"3"}), [9])])

P_SCHEMA_JSON = {"schema-id": 0, "type": "struct", "fields": [
    {"id": 1, "name": "id", "required": False, "type": "long"},
    {"id": 2, "name": "region", "required": False, "type": "string"},
    {"id": 3, "name": "grp", "required": False, "type": "int"}]}
# data manifest: both partitioned data files under spec 1, data seq 5
PDM = f"{LOC}/metadata/part-data-manifest.avro"
open(os.path.join(md, "part-data-manifest.avro"), "wb").write(ocf_multi(
    ENTRY_SCHEMA_PART, [
        entry_part(1, DATA_SEQ, 0, PART_EU, 2, 1000, "eu", None),
        entry_part(1, DATA_SEQ, 0, PART_US, 3, 1000, "us", None)]))


# spec 2 exists only so a delete can be written under a spec no data file uses
# (partition evolution), exercising the cross-spec refusal.
PART_SPECS_P5 = PARTITION_SPECS + [
    {"spec-id": 2, "fields": [
        {"name": "region", "transform": "identity", "source-id": 2, "field-id": 1000}]}]


def emit_part_variant(tag, del_region, del_schema=ENTRY_SCHEMA_PART,
                      double_part=False, del_spec_id=1):
    """A partition-scoped equality delete on grp=9, tagged with partition
    region=del_region under partition spec del_spec_id, over the two spec-1
    partitioned data files. Survivors depend on which partition it scopes to."""
    xm_name = f"delete-manifest-{tag}.avro"
    if double_part:
        rec = entry_part(1, EQ_SEQ, 2, EQ_GRP, 1, 500, None, [3],
                         schema_double=True, dval=1.5)
    else:
        rec = entry_part(1, EQ_SEQ, 2, EQ_GRP, 1, 500, del_region, [3])
    open(os.path.join(md, xm_name), "wb").write(ocf_multi(del_schema, [rec]))
    xm = f"{LOC}/metadata/{xm_name}"
    sync = b"\x00" * 16
    meta = zz(2) + s(b"avro.schema") + s(MFILE_SCHEMA) + s(b"avro.codec") + s(b"null") + zz(0)
    ml = b"Obj\x01" + meta + sync
    for rec in (mfile_spec(PDM, 0, DATA_SEQ, SNAP, 2, 5, 1),
                mfile_spec(xm, 1, EQ_SEQ, SNAP, 1, 1, del_spec_id)):
        ml += zz(1) + zz(len(rec)) + rec + sync
    open(os.path.join(md, f"manifest-list-{tag}.avro"), "wb").write(ml)
    open(os.path.join(md, f"{tag}.metadata.json"), "w").write(json.dumps({
        "format-version": 2, "location": LOC, "current-schema-id": 0,
        "schemas": [P_SCHEMA_JSON], "partition-specs": PART_SPECS_P5,
        "default-spec-id": 1, "current-snapshot-id": SNAP,
        "snapshots": [{"snapshot-id": SNAP, "sequence-number": EQ_SEQ,
                       "timestamp-ms": 0,
                       "manifest-list": f"{LOC}/metadata/manifest-list-{tag}.avro",
                       "summary": {"operation": "overwrite"}, "schema-id": 0}]}))


# eqpart_apply: delete scoped to region=eu -> only eu rows (1,2) deleted
emit_part_variant("eqpart_apply", "eu")
# eqpart_nomatch: delete scoped to region=zz, same spec as the data. No data
# file is in partition zz, so the delete applies to nothing -- a legitimate
# no-op, NOT an error: all five rows survive.
emit_part_variant("eqpart_nomatch", "zz")
# eqpart_crossspec: delete under spec 2, which NO data file uses (all data is
# spec 1). The reader cannot resolve cross-spec partition matching, and
# silently ignoring the delete would under-delete -> refuse 0A000.
emit_part_variant("eqpart_crossspec", "eu", del_spec_id=2)
# eqpart_incomparable: a double partition cell -> refuse 0A000
emit_part_variant("eqpart_incomparable", None, del_schema=ENTRY_SCHEMA_PARTF,
                  double_part=True)


# eqpart_datanospec: corrupt metadata where the DATA file's manifest_file entry
# carries no partition_spec_id (a nullable union encoding null) while the
# partitioned delete's does. Without the data-side guard this would silently
# under-delete (the data file's spec id defaults to 0, mismatches the delete's,
# and the delete that should apply is skipped). The manifest list uses a schema
# whose partition_spec_id is [null,int] so one row can omit it and the other not.
MFILE_SCHEMA_NULLSPEC = json.dumps({"type": "record", "name": "manifest_file", "fields": [
    {"name": "manifest_path", "type": "string"},
    {"name": "manifest_length", "type": "long"},
    {"name": "partition_spec_id", "type": ["null", "int"]},
    {"name": "content", "type": "int"},
    {"name": "sequence_number", "type": "long"},
    {"name": "min_sequence_number", "type": "long"},
    {"name": "added_snapshot_id", "type": "long"},
    {"name": "added_files_count", "type": "int"},
    {"name": "existing_files_count", "type": "int"},
    {"name": "deleted_files_count", "type": "int"},
    {"name": "added_rows_count", "type": "long"},
    {"name": "existing_rows_count", "type": "long"},
    {"name": "deleted_rows_count", "type": "long"}]}).encode()


def mfile_nullspec(path, content, seq, snap, files, rows, spec_id):
    """mfile with partition_spec_id as a [null,int] union: spec_id None -> null
    (the corrupt data-file case), else branch 1 with the value."""
    sb = zz(0) if spec_id is None else (zz(1) + zz(spec_id))
    return s(path.encode()) + zz(1000) + sb + zz(content) + zz(seq) + zz(seq) + \
        zz(snap) + zz(files) + zz(0) + zz(0) + zz(rows) + zz(0) + zz(0)


def emit_part_datanospec():
    tag = "eqpart_datanospec"
    xm_name = f"delete-manifest-{tag}.avro"
    open(os.path.join(md, xm_name), "wb").write(ocf_multi(
        ENTRY_SCHEMA_PART, [entry_part(1, EQ_SEQ, 2, EQ_GRP, 1, 500, "eu", [3])]))
    xm = f"{LOC}/metadata/{xm_name}"
    sync = b"\x00" * 16
    meta = zz(2) + s(b"avro.schema") + s(MFILE_SCHEMA_NULLSPEC) + s(b"avro.codec") + s(b"null") + zz(0)
    ml = b"Obj\x01" + meta + sync
    for rec in (mfile_nullspec(PDM, 0, DATA_SEQ, SNAP, 2, 5, None),   # data: no spec id
                mfile_nullspec(xm, 1, EQ_SEQ, SNAP, 1, 1, 1)):        # delete: spec 1
        ml += zz(1) + zz(len(rec)) + rec + sync
    open(os.path.join(md, f"manifest-list-{tag}.avro"), "wb").write(ml)
    open(os.path.join(md, f"{tag}.metadata.json"), "w").write(json.dumps({
        "format-version": 2, "location": LOC, "current-schema-id": 0,
        "schemas": [P_SCHEMA_JSON], "partition-specs": PART_SPECS_P5,
        "default-spec-id": 1, "current-snapshot-id": SNAP,
        "snapshots": [{"snapshot-id": SNAP, "sequence-number": EQ_SEQ,
                       "timestamp-ms": 0,
                       "manifest-list": f"{LOC}/metadata/manifest-list-{tag}.avro",
                       "summary": {"operation": "overwrite"}, "schema-id": 0}]}))


emit_part_datanospec()

# oracle: the surviving rows (id, region, amount), deleted positions removed
rows = [(1, "eu", 10), (2, "eu", 20), (3, "us", 30), (4, "us", 40), (5, "us", 50)]
survive = [r for i, r in enumerate(rows) if i not in DELETED_POS]
oracle = {"deleted_positions": DELETED_POS,
          "surviving": [{"id": r[0], "region": r[1], "amount": r[2]} for r in survive],
          "data_seq": DATA_SEQ, "delete_seq": DATA_SEQ,
          # equality arms: surviving ids per variant, hand-derived above where
          # each variant is emitted
          "eq_surviving": {
              "eqapply": [1, 3, 5],
              "eqboundary": [1, 2, 3, 4, 5],
              "eqmulti": [1, 2, 3, 5],
              "eqextra": [1, 3, 4, 5],
              "eqnull": [1, 2, 3, 4, 5, 7],
              "eqtwo": [3, 4],
              "eqmixed": [1, 3],
              "eqmany": MANY_SURVIVORS,
          },
          # deletion-vector arms: surviving ids, hand-derived at each variant
          "dv_surviving": {
              "dvapply": [1, 3, 5],
              "dvnoapply": [1, 2, 3, 4, 5],
              "dvsupersede": [1, 3, 4, 5],
              "dvother": [1, 3, 4, 5, 7],
              "dvwide": [1, 3, 4, 5],
              "dvrun": [1, 2],
              "dvbitset": [2, 4],
              "dvtwo": [1, 3, 4, 5, 7],
          },
          # partition-scoped equality-delete arm: eu rows (1,2) deleted, us kept
          "part_surviving": {"eqpart_apply": [3, 4, 5]}}
open(os.path.join(OUT, "expected_deletes.json"), "w").write(json.dumps(oracle, indent=2))
print("surviving rows:", oracle["surviving"])
print("deleted positions:", DELETED_POS)
print("equality survivors:", oracle["eq_surviving"])
