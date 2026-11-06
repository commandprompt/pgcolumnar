#!/usr/bin/env python3
# Generate a hand-crafted Apache Iceberg v2 table with MANY data files and MANY
# position-delete rows, for #1868: iceberg_scan must find each data file's own
# position deletes without walking every position-delete row once per data file.
#
# Written to its own warehouse (warehouse_posmany, recorded root
# /tmp/pgc_ice_posmany) so regenerating it never touches warehouse_del.
#
#   32 data files d00..d31, 8 rows each (ids f*100 + r), sequence number 5.
#   Even files are recorded as file:///tmp/..., odd files as bare /tmp/...
#
#   posdel-a.parquet (seq 5, applies: data_seq <= delete_seq), per file f:
#     (other spelling, 0)       ordinal 0, named file:// where the data entry is
#                               bare and bare where it is file://
#     (same spelling, k)        k = 1 + f % 7
#     (other spelling, k)       for f % 4 == 0: the same row again, spelled the
#                               other way (both land in one group; deduplicated)
#   plus ("s3://" + d05's bare path, 7): a different scheme, so NOT d05's row 7
#   plus (d99, 1): a data file that is not in the table, so it deletes nothing
#
#   posdel-b.parquet (seq 4, older than the data, so never applied), per file f:
#     (same spelling, 2)
#
# The oracle (expected_posmany.json) is the 256 rows minus posdel-a's deletes of
# ordinals 0 and k in each file, and the counts the scan's DEBUG1 lines report:
# one index build over every row, one group per normalized path, and per data
# file only the rows naming it, found with one group-key comparison.
import sys, os, json, shutil, hashlib
import pyarrow as pa, pyarrow.parquet as pq

OUT = sys.argv[1]                                   # fixtures/iceberg/warehouse_posmany
ROOT = "/tmp/pgc_ice_posmany"                       # stable recorded root
LOC = f"file://{ROOT}/db/t"
NFILES, NROWS, DATA_SEQ = 32, 8, 5

shutil.rmtree(OUT, ignore_errors=True)
os.makedirs(os.path.join(OUT, "db", "t", "metadata"))
os.makedirs(os.path.join(OUT, "db", "t", "data"))


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


def ocf_multi(schema_json, records):
    sync = b"\x00" * 16
    meta = zz(2) + s(b"avro.schema") + s(schema_json) + s(b"avro.codec") + s(b"null") + zz(0)
    body = b"".join(records)
    return b"Obj\x01" + meta + sync + zz(len(records)) + zz(len(body)) + body + sync


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


def entry(seq, content, path, rows):
    return zz(1) + zz(1) + zz(seq) + zz(content) + s(path.encode()) + \
        s(b"PARQUET") + zz(rows) + zz(1000)


def mfile(path, content, seq, snap, files, rows):
    return s(path.encode()) + zz(1000) + zz(0) + zz(content) + zz(seq) + zz(seq) + \
        zz(snap) + zz(files) + zz(0) + zz(0) + zz(rows) + zz(0) + zz(0)


DSCHEMA = pa.schema([
    pa.field("id", pa.int64(), metadata={b"PARQUET:field_id": b"1"}),
    pa.field("region", pa.string(), metadata={b"PARQUET:field_id": b"2"}),
    pa.field("amount", pa.int32(), metadata={b"PARQUET:field_id": b"3"}),
])
PSCHEMA = pa.schema([
    pa.field("file_path", pa.string(), nullable=False,
             metadata={b"PARQUET:field_id": b"2147483546"}),
    pa.field("pos", pa.int64(), nullable=False,
             metadata={b"PARQUET:field_id": b"2147483545"}),
])


def bare(f):
    return f"{ROOT}/db/t/data/d{f:02d}.parquet"


def spelled(f, scheme):
    return ("file://" + bare(f)) if scheme else bare(f)


rows = {}
data_entries = []
for f in range(NFILES):
    ids = [f * 100 + r for r in range(NROWS)]
    regions = ["eu" if (f + r) % 2 == 0 else "us" for r in range(NROWS)]
    amounts = [i * 10 for i in ids]
    rows[f] = list(zip(ids, regions, amounts))
    # row groups of 3, so the deleted ordinals span row-group boundaries
    pq.write_table(pa.table({"id": pa.array(ids, pa.int64()),
                             "region": pa.array(regions, pa.string()),
                             "amount": pa.array(amounts, pa.int32())}, schema=DSCHEMA),
                   os.path.join(OUT, "db", "t", "data", f"d{f:02d}.parquet"),
                   row_group_size=3)
    data_entries.append(entry(DATA_SEQ, 0, spelled(f, f % 2 == 0), NROWS))

a_rows, b_rows = [], []
deleted = {f: set() for f in range(NFILES)}
for f in range(NFILES):
    same = f % 2 == 0
    k = 1 + f % 7
    a_rows.append((spelled(f, not same), 0))
    a_rows.append((spelled(f, same), k))
    deleted[f] |= {0, k}
    if f % 4 == 0:
        a_rows.append((spelled(f, not same), k))
    b_rows.append((spelled(f, same), 2))
S3_ROW = ("s3://" + bare(5), 7)                      # not d05: another scheme
ORPHAN_ROW = (spelled(99, True), 1)                  # no such data file
a_rows += [S3_ROW, ORPHAN_ROW]

for name, rr in (("posdel-a.parquet", a_rows), ("posdel-b.parquet", b_rows)):
    pq.write_table(pa.table({"file_path": pa.array([p for p, _ in rr], pa.string()),
                             "pos": pa.array([q for _, q in rr], pa.int64())},
                            schema=PSCHEMA),
                   os.path.join(OUT, "db", "t", "data", name))

md = os.path.join(OUT, "db", "t", "metadata")
SNAP = 6655443322110099
open(os.path.join(md, "data-manifest-posmany.avro"), "wb").write(
    ocf_multi(ENTRY_SCHEMA, data_entries))
open(os.path.join(md, "delete-manifest-posmany.avro"), "wb").write(
    ocf_multi(ENTRY_SCHEMA, [entry(DATA_SEQ, 1, f"{LOC}/data/posdel-a.parquet", len(a_rows)),
                             entry(DATA_SEQ - 1, 1, f"{LOC}/data/posdel-b.parquet", len(b_rows))]))
open(os.path.join(md, "manifest-list-posmany.avro"), "wb").write(
    ocf_multi(MFILE_SCHEMA, [
        mfile(f"{LOC}/metadata/data-manifest-posmany.avro", 0, DATA_SEQ, SNAP, NFILES, NFILES * NROWS),
        mfile(f"{LOC}/metadata/delete-manifest-posmany.avro", 1, DATA_SEQ, SNAP, 2,
              len(a_rows) + len(b_rows))]))
schema = {"schema-id": 0, "type": "struct", "fields": [
    {"id": 1, "name": "id", "required": False, "type": "long"},
    {"id": 2, "name": "region", "required": False, "type": "string"},
    {"id": 3, "name": "amount", "required": False, "type": "int"}]}
open(os.path.join(md, "posmany.metadata.json"), "w").write(json.dumps({
    "format-version": 2, "location": LOC, "current-schema-id": 0,
    "schemas": [schema], "current-snapshot-id": SNAP,
    "snapshots": [{"snapshot-id": SNAP, "sequence-number": DATA_SEQ, "timestamp-ms": 0,
                   "manifest-list": f"{LOC}/metadata/manifest-list-posmany.avro",
                   "summary": {"operation": "overwrite"}, "schema-id": 0}]}))


# the oracle, from the lists above: a delete row names data file f when its path
# equals f's after dropping a leading file:// (s3:// is kept, so it names no
# local file)
def key(p):
    return p[7:] if p.startswith("file://") else p


all_rows = a_rows + b_rows
groups = {}
for p, _ in all_rows:
    groups[key(p)] = groups.get(key(p), 0) + 1
per_file = [groups.get(bare(f), 0) for f in range(NFILES)]
survivors = [r for f in range(NFILES) for i, r in enumerate(rows[f]) if i not in deleted[f]]
surv_text = ",".join(str(r[0]) for r in survivors)
oracle = {
    "files": NFILES,
    "data_rows": NFILES * NROWS,
    "surviving_ids": [r[0] for r in survivors],
    "surviving_md5": hashlib.md5(surv_text.encode()).hexdigest(),
    "index": {"builds": 1, "rows": len(all_rows), "paths": len(groups)},
    # compared: group keys compared in the lookups, one per data file that has a
    # group when every key sits in its own bucket
    "probe": {"files": NFILES, "compared": sum(1 for n in per_file if n),
              "visited": sum(per_file), "longest": max(per_file),
              "applied": sum(1 for p, _ in a_rows if key(p) in {bare(f) for f in range(NFILES)})},
}
open(os.path.join(OUT, "expected_posmany.json"), "w").write(json.dumps(oracle, indent=2) + "\n")
print("index:", oracle["index"], "probe:", oracle["probe"], "survivors:", len(survivors))
