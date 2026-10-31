#!/usr/bin/env python3
# Generate a hand-crafted Apache Iceberg v2 table whose columns went through the
# spec's legal type promotions (#1789): int -> long, float -> double, and
# decimal(10,2) -> decimal(20,2). Data files written before the promotion keep
# the old physical types (INT32, FLOAT, a 5-byte decimal); files written after it
# carry the new ones (INT64, DOUBLE, a 9-byte decimal). A reader must widen the
# old files to the current schema's types. A date column, dt (field 5), is in
# every file and never promoted.
#
# The table is built by hand, as gen_delete_fixture.py builds its own, because no
# available writer emits equality deletes and the sequence numbers must be exact.
# The Parquet files are real pyarrow files with real field ids. The manifests,
# manifest lists and metadata.json are hand-encoded.
#
#   db/t/data/old.parquet     schema 0 types, dictionary-encoded, ids 1..5
#   db/t/data/old2.parquet    schema 0 types, PLAIN-encoded,      ids 6..7
#   db/t/data/new.parquet     schema 1 types,                     ids 8..9
#   db/t/data/eqdel-k32.parquet  equality delete on k, INT32 (written before
#                                the promotion), k = 0
#   db/t/data/eqdel-k64.parquet  equality delete on k, INT64 (written after
#                                it), k = 2
#
# Three snapshots, one metadata.json each, all with current schema 1:
#
#   old.metadata.json    old + old2 (seq 1): only pre-promotion files
#   mixed.metadata.json  old + old2 (seq 1), new (seq 3)
#   eq.metadata.json     the mixed files plus eqdel-k32 (seq 2) and
#                        eqdel-k64 (seq 4). eqdel-k32 applies to the seq-1
#                        files only (id 3); eqdel-k64 applies to all three
#                        (ids 4, 7, 9). Survivors: 1, 2, 5, 6, 8.
#
# The oracle is in the suites (test/iceberg_scan.sh and its twin), derived from
# the values below.
#
# Usage:  gen_promotion_fixture.py test/fixtures/iceberg/warehouse_promo
import json
import os
import shutil
import sys
import datetime
from decimal import Decimal

import pyarrow as pa
import pyarrow.parquet as pq

OUT = sys.argv[1]
ROOT = "/tmp/pgc_ice_promo"                         # stable recorded root
LOC = f"file://{ROOT}/db/t"

shutil.rmtree(OUT, ignore_errors=True)
DATA = os.path.join(OUT, "db", "t", "data")
MD = os.path.join(OUT, "db", "t", "metadata")
os.makedirs(DATA)
os.makedirs(MD)


# ---- Avro OCF encoding (as gen_delete_fixture.py) --------------------------
def zz(n):
    u = (n << 1) ^ (n >> 63) if n < 0 else (n << 1)
    out = bytearray()
    while True:
        b = u & 0x7f
        u >>= 7
        out.append(b | 0x80 if u else b)
        if not u:
            break
    return bytes(out)


def s(bs):
    return zz(len(bs)) + bs


def ocf(schema_json, records):
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
        {"name": "file_size_in_bytes", "type": "long"},
        {"name": "equality_ids",
         "type": ["null", {"type": "array", "items": "int"}]}]}}]}).encode()

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


def entry(seq, content, name, rows, eq_ids=None):
    eqb = zz(0) if eq_ids is None else \
        zz(1) + zz(len(eq_ids)) + b"".join(zz(i) for i in eq_ids) + zz(0)
    return zz(1) + zz(1) + zz(seq) + zz(content) + s(f"{LOC}/data/{name}".encode()) + \
        s(b"PARQUET") + zz(rows) + zz(1000) + eqb


def mfile(name, content, seq, files, rows):
    return s(f"{LOC}/metadata/{name}".encode()) + zz(1000) + zz(0) + zz(content) + \
        zz(seq) + zz(seq) + zz(SNAP) + zz(files) + zz(0) + zz(0) + zz(rows) + zz(0) + zz(0)


# ---- the Parquet files -----------------------------------------------------
def fld(name, typ, fid):
    return pa.field(name, typ, metadata={b"PARQUET:field_id": str(fid).encode()})


# dt (an Iceberg date, also INT32 in Parquet) is never promoted: it is the
# control that a widening keyed only on the physical type would wrongly accept
OLD = pa.schema([fld("id", pa.int64(), 1), fld("k", pa.int32(), 2),
                 fld("f", pa.float32(), 3), fld("d", pa.decimal128(10, 2), 4),
                 fld("dt", pa.date32(), 5)])
NEW = pa.schema([fld("id", pa.int64(), 1), fld("k", pa.int64(), 2),
                 fld("f", pa.float64(), 3), fld("d", pa.decimal128(20, 2), 4),
                 fld("dt", pa.date32(), 5)])


def table(schema, id_, k, f, d):
    return pa.table({"id": pa.array(id_, schema.field("id").type),
                     "k": pa.array(k, schema.field("k").type),
                     "f": pa.array(f, schema.field("f").type),
                     "d": pa.array([Decimal(x) for x in d], schema.field("d").type),
                     "dt": pa.array([datetime.date(2020, 1, i) for i in id_], pa.date32())},
                    schema=schema)


# the int32 extremes and FLT_MAX: a value read as the wrong width, or a float
# reinterpreted as a double, cannot come out right
pq.write_table(table(OLD, [1, 2, 3, 4, 5],
                     [-2147483648, -1, 0, 2, 2147483647],
                     [1.5, -2.25, 0.5, 1024.75, 3.4028234663852886e38],
                     ["1.25", "-3.50", "0.00", "99999999.99", "-0.01"]),
               os.path.join(DATA, "old.parquet"), row_group_size=3)
pq.write_table(table(OLD, [6, 7], [7, 2], [-0.125, 2.0], ["10.10", "2.00"]),
               os.path.join(DATA, "old2.parquet"), use_dictionary=False)
pq.write_table(table(NEW, [8, 9], [5000000000, 2], [3.25, 0.1],
                     ["123456789012345678.90", "-1.00"]),
               os.path.join(DATA, "new.parquet"))

K32 = pa.schema([fld("k", pa.int32(), 2)])
K64 = pa.schema([fld("k", pa.int64(), 2)])
pq.write_table(pa.table({"k": pa.array([0], pa.int32())}, schema=K32),
               os.path.join(DATA, "eqdel-k32.parquet"))
pq.write_table(pa.table({"k": pa.array([2], pa.int64())}, schema=K64),
               os.path.join(DATA, "eqdel-k64.parquet"))

# ---- manifests -------------------------------------------------------------
SNAP = 5544332211009988
open(os.path.join(MD, "m-old.avro"), "wb").write(
    ocf(ENTRY_SCHEMA, [entry(1, 0, "old.parquet", 5), entry(1, 0, "old2.parquet", 2)]))
open(os.path.join(MD, "m-new.avro"), "wb").write(
    ocf(ENTRY_SCHEMA, [entry(3, 0, "new.parquet", 2)]))
open(os.path.join(MD, "m-eq32.avro"), "wb").write(
    ocf(ENTRY_SCHEMA, [entry(2, 2, "eqdel-k32.parquet", 1, [2])]))
open(os.path.join(MD, "m-eq64.avro"), "wb").write(
    ocf(ENTRY_SCHEMA, [entry(4, 2, "eqdel-k64.parquet", 1, [2])]))

M_OLD = mfile("m-old.avro", 0, 1, 2, 7)
M_NEW = mfile("m-new.avro", 0, 3, 1, 2)
M_EQ32 = mfile("m-eq32.avro", 1, 2, 1, 1)
M_EQ64 = mfile("m-eq64.avro", 1, 4, 1, 1)

SCHEMA0 = {"schema-id": 0, "type": "struct", "fields": [
    {"id": 1, "name": "id", "required": False, "type": "long"},
    {"id": 2, "name": "k", "required": False, "type": "int"},
    {"id": 3, "name": "f", "required": False, "type": "float"},
    {"id": 4, "name": "d", "required": False, "type": "decimal(10, 2)"},
    {"id": 5, "name": "dt", "required": False, "type": "date"}]}
SCHEMA1 = {"schema-id": 1, "type": "struct", "fields": [
    {"id": 1, "name": "id", "required": False, "type": "long"},
    {"id": 2, "name": "k", "required": False, "type": "long"},
    {"id": 3, "name": "f", "required": False, "type": "double"},
    {"id": 4, "name": "d", "required": False, "type": "decimal(20, 2)"},
    {"id": 5, "name": "dt", "required": False, "type": "date"}]}


def emit(tag, manifests, seq):
    ml = f"ml-{tag}.avro"
    open(os.path.join(MD, ml), "wb").write(ocf(MFILE_SCHEMA, manifests))
    open(os.path.join(MD, f"{tag}.metadata.json"), "w").write(json.dumps({
        "format-version": 2, "location": LOC, "last-column-id": 5,
        "current-schema-id": 1, "schemas": [SCHEMA0, SCHEMA1],
        "partition-specs": [{"spec-id": 0, "fields": []}], "default-spec-id": 0,
        "current-snapshot-id": SNAP,
        "snapshots": [{"snapshot-id": SNAP, "sequence-number": seq, "timestamp-ms": 0,
                       "manifest-list": f"{LOC}/metadata/{ml}",
                       "summary": {"operation": "overwrite"}, "schema-id": 1}]}))


emit("old", [M_OLD], 1)
emit("mixed", [M_OLD, M_NEW], 3)
emit("eq", [M_OLD, M_EQ32, M_NEW, M_EQ64], 4)
