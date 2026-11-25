#!/usr/bin/env python3
# Generate a hand-crafted Apache Iceberg table whose data file carries NO
# Parquet field ids, read through a schema.name-mapping.default table property
# (#388 name mapping). An id-less data file is what an Iceberg "imported data
# file" (a migrated Hive/Spark dataset) looks like; the reader must bind its
# columns by name through the mapping instead of by id.
#
# Deterministic and additive, like gen_delete_fixture.py: the data Parquet is a
# real pyarrow file (written with NO PARQUET:field_id metadata), and the
# manifest, manifest list, and metadata.json are hand-encoded Avro / JSON with a
# stable recorded root, so the committed fixture regenerates byte-identically
# and the reader rebases the paths at read time.
#
# Layout under OUT (test/fixtures/iceberg/warehouse_nm):
#   db/t/data/data.parquet         id-less columns id/region/amount, 5 rows
#   db/t/data/data-alias.parquet   id-less columns rid/region/amount (alias arm)
#   db/t/metadata/data-manifest.avro       -> data.parquet
#   db/t/metadata/data-manifest-alias.avro -> data-alias.parquet
#   db/t/metadata/manifest-list-<tag>.avro
#   db/t/metadata/<tag>.metadata.json      one per arm, carrying the mapping
import sys
import os
import json
import shutil
import pyarrow as pa
import pyarrow.parquet as pq

OUT = sys.argv[1]                                   # fixtures/iceberg/warehouse_nm
ROOT = "/tmp/pgc_ice_nm"                            # stable recorded root
LOC = f"file://{ROOT}/db/t"

shutil.rmtree(OUT, ignore_errors=True)
os.makedirs(os.path.join(OUT, "db", "t", "metadata"))
os.makedirs(os.path.join(OUT, "db", "t", "data"))
md = os.path.join(OUT, "db", "t", "metadata")


# ---- Avro OCF encoding helpers (as in gen_delete_fixture.py) ---------------
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


def ocf(schema_json, record_bytes):
    sync = b"\x00" * 16
    meta = zz(2) + s(b"avro.schema") + s(schema_json) + s(b"avro.codec") + s(b"null") + zz(0)
    return b"Obj\x01" + meta + sync + zz(1) + zz(len(record_bytes)) + record_bytes + sync


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
    return zz(status) + (zz(1) + zz(seq)) + zz(content) + s(path.encode()) + \
        s(fmt.encode()) + zz(rows) + zz(size)


def mfile(path, content, seq, snap, files, rows):
    return s(path.encode()) + zz(1000) + zz(0) + zz(content) + zz(seq) + zz(seq) + \
        zz(snap) + zz(files) + zz(0) + zz(0) + zz(rows) + zz(0) + zz(0)


# ---- the id-less data Parquet (real pyarrow, NO field-id metadata) ---------
ROWS = [(1, "eu", 10), (2, "eu", 20), (3, "us", 30), (4, "us", 40), (5, "us", 50)]


def write_idless(name, id_col):
    """An id-less parquet with columns (id_col, region, amount). No field is
    given PARQUET:field_id metadata, so every column reads as field_id = -1."""
    t = pa.table({
        id_col: pa.array([r[0] for r in ROWS], pa.int64()),
        "region": pa.array([r[1] for r in ROWS], pa.string()),
        "amount": pa.array([r[2] for r in ROWS], pa.int32()),
    })
    pq.write_table(t, os.path.join(OUT, "db", "t", "data", name), row_group_size=2)
    return f"{LOC}/data/{name}"


DATA = write_idless("data.parquet", "id")
DATA_ALIAS = write_idless("data-alias.parquet", "rid")


def write_idless_2col():
    """An id-less file with only id and region -- the amount column was added to
    the table schema after this file was written, so it is absent here."""
    t = pa.table({
        "id": pa.array([r[0] for r in ROWS], pa.int64()),
        "region": pa.array([r[1] for r in ROWS], pa.string()),
    })
    pq.write_table(t, os.path.join(OUT, "db", "t", "data", "data-2col.parquet"),
                   row_group_size=2)
    return f"{LOC}/data/data-2col.parquet"


DATA_2COL = write_idless_2col()

SNAP = 5544332211009988
SEQ = 1
DM = f"{LOC}/metadata/data-manifest.avro"
DMA = f"{LOC}/metadata/data-manifest-alias.avro"
DM2 = f"{LOC}/metadata/data-manifest-2col.avro"
open(os.path.join(md, "data-manifest.avro"), "wb").write(
    ocf(ENTRY_SCHEMA, entry(1, SEQ, 0, DATA, "PARQUET", 5, 1000)))
open(os.path.join(md, "data-manifest-alias.avro"), "wb").write(
    ocf(ENTRY_SCHEMA, entry(1, SEQ, 0, DATA_ALIAS, "PARQUET", 5, 1000)))
open(os.path.join(md, "data-manifest-2col.avro"), "wb").write(
    ocf(ENTRY_SCHEMA, entry(1, SEQ, 0, DATA_2COL, "PARQUET", 5, 1000)))


def emit(tag, fields, mapping, dm=DM):
    """Write a (manifest-list, metadata.json) pair for one arm. `fields` is the
    schema fields list; `mapping` is the schema.name-mapping.default value (a
    Python object serialized to the JSON *string* the property holds), or None
    to omit the property entirely."""
    ml_name = f"manifest-list-{tag}.avro"
    sync = b"\x00" * 16
    meta = zz(2) + s(b"avro.schema") + s(MFILE_SCHEMA) + s(b"avro.codec") + s(b"null") + zz(0)
    ml = b"Obj\x01" + meta + sync
    rec = mfile(dm, 0, SEQ, SNAP, 1, 5)
    ml += zz(1) + zz(len(rec)) + rec + sync
    open(os.path.join(md, ml_name), "wb").write(ml)
    props = {} if mapping is None else {"schema.name-mapping.default": json.dumps(mapping)}
    metadata = {
        "format-version": 2, "location": LOC, "current-schema-id": 0,
        "schemas": [{"schema-id": 0, "type": "struct", "fields": fields}],
        "partition-specs": [{"spec-id": 0, "fields": []}], "default-spec-id": 0,
        "properties": props, "current-snapshot-id": SNAP,
        "snapshots": [{"snapshot-id": SNAP, "sequence-number": SEQ,
                       "timestamp-ms": 0,
                       "manifest-list": f"{LOC}/metadata/{ml_name}",
                       "summary": {"operation": "append"}, "schema-id": 0}],
    }
    open(os.path.join(md, f"{tag}.metadata.json"), "w").write(json.dumps(metadata))


SCHEMA_STD = [{"id": 1, "name": "id", "required": False, "type": "long"},
              {"id": 2, "name": "region", "required": False, "type": "string"},
              {"id": 3, "name": "amount", "required": False, "type": "int"}]
MAP_STD = [{"field-id": 1, "names": ["id"]},
           {"field-id": 2, "names": ["region"]},
           {"field-id": 3, "names": ["amount"]}]

# nmapply: the id-less file binds by name; all 5 rows read
emit("nmapply", SCHEMA_STD, MAP_STD)

# nmrename: the schema field for id 1 is named "ident" (the table was renamed
# after the file was written), the mapping carries the OLD file name "id", the
# file column is still "id". Reading proves output name -> schema id -> mapping
# name -> file column, not output name -> file column.
emit("nmrename",
     [{"id": 1, "name": "ident", "required": False, "type": "long"},
      {"id": 2, "name": "region", "required": False, "type": "string"},
      {"id": 3, "name": "amount", "required": False, "type": "int"}],
     [{"field-id": 1, "names": ["id"]},
      {"field-id": 2, "names": ["region"]},
      {"field-id": 3, "names": ["amount"]}])

# nmalias: id 1 has two mapping names; the file uses the second ("rid")
emit("nmalias", SCHEMA_STD,
     [{"field-id": 1, "names": ["id", "rid"]},
      {"field-id": 2, "names": ["region"]},
      {"field-id": 3, "names": ["amount"]}],
     dm=DMA)

# nmsubset: project only (id, amount); name binding under projection
emit("nmsubset", SCHEMA_STD, MAP_STD)

# nmnomap: an id-less file with NO name-mapping property -> refused, naming it
emit("nmnomap", SCHEMA_STD, None)

# nmdup: the name "id" appears under two field ids -> corrupt (uniqueness)
emit("nmdup", SCHEMA_STD,
     [{"field-id": 1, "names": ["id"]},
      {"field-id": 2, "names": ["id"]},
      {"field-id": 3, "names": ["amount"]}])

# nmmissing: the mapping has no name for "amount". The file physically has an
# amount column, but with no mapping entry the reader cannot bind it to field 3,
# so per Column Projection rung 4 amount reads as null (not an error, and not
# the physical bytes -- there is no binding).
emit("nmmissing", SCHEMA_STD,
     [{"field-id": 1, "names": ["id"]},
      {"field-id": 2, "names": ["region"]}])

# nmevolve: the headline case -- an imported file written before "amount" was
# added to the schema. The file has only id and region; the mapping maps all
# three. amount is absent from the file, so it reads as null while id and
# region return real data.
emit("nmevolve", SCHEMA_STD, MAP_STD, dm=DM2)

# nmbigid: a mapping field-id beyond int32. A silent (int) truncation would
# alias it onto a real field id and bind the wrong column; the reader must
# refuse the mapping.
emit("nmbigid", SCHEMA_STD,
     [{"field-id": (1 << 32) + 1, "names": ["id"]},
      {"field-id": 2, "names": ["region"]},
      {"field-id": 3, "names": ["amount"]}])

# ---- equality deletes over the id-less file (#2088, additive) --------------
# An imported table that later receives an equality delete (a Flink upsert, a
# merge-on-read DELETE). The delete file carries real field ids, as any Iceberg
# writer's does; the data file still carries none. The delete columns must bind
# through the same name mapping the data read uses. Everything below ADDS files;
# nothing written above is touched.
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
DEL_SEQ = 2


def write_eqdel(name, col, field_id, typ, values):
    """An equality-delete Parquet with one column carrying PARQUET:field_id."""
    schema = pa.schema([pa.field(col, typ, metadata={b"PARQUET:field_id": str(field_id).encode()})])
    pq.write_table(pa.table({col: pa.array(values, typ)}, schema=schema),
                   os.path.join(OUT, "db", "t", "data", name))
    return f"{LOC}/data/{name}"


def write_eqdel_cols(name, cols):
    """An equality-delete Parquet with several columns, each carrying
    PARQUET:field_id. `cols` is a list of (name, field_id, type, values)."""
    schema = pa.schema([pa.field(c, t, metadata={b"PARQUET:field_id": str(f).encode()})
                        for c, f, t, _ in cols])
    pq.write_table(pa.table({c: pa.array(v, t) for c, _, t, v in cols}, schema=schema),
                   os.path.join(OUT, "db", "t", "data", name))
    return f"{LOC}/data/{name}"


def eq_entry(del_path, eq_ids, nrows):
    """One manifest entry for an equality-delete file on the fields eq_ids."""
    return zz(1) + (zz(1) + zz(DEL_SEQ)) + zz(2) + s(del_path.encode()) + \
        s(b"PARQUET") + zz(nrows) + zz(1000) + zz(1) + \
        zz(len(eq_ids)) + b"".join(zz(i) for i in eq_ids) + zz(0)


def emit_eq(tag, fields, mapping, del_path, eq_id, nrows, more=()):
    """One arm: the id-less data.parquet (seq 1) plus one equality delete on
    field eq_id (seq 2), under the given schema and name mapping. eq_id may be a
    list for a delete keyed on several columns. `more` adds further delete
    files to the same manifest, each a (del_path, eq_ids, nrows) tuple."""
    xm_name = f"eq-manifest-{tag}.avro"
    dels = [(del_path, eq_id if isinstance(eq_id, list) else [eq_id], nrows), *more]
    sync = b"\x00" * 16
    xm = b"Obj\x01" + zz(2) + s(b"avro.schema") + s(ENTRY_SCHEMA_EQ) + \
        s(b"avro.codec") + s(b"null") + zz(0) + sync
    recs = b"".join(eq_entry(*d) for d in dels)
    xm += zz(len(dels)) + zz(len(recs)) + recs + sync
    open(os.path.join(md, xm_name), "wb").write(xm)
    ml_name = f"manifest-list-{tag}.avro"
    meta = zz(2) + s(b"avro.schema") + s(MFILE_SCHEMA) + s(b"avro.codec") + s(b"null") + zz(0)
    ml = b"Obj\x01" + meta + sync
    for r in (mfile(DM, 0, SEQ, SNAP, 1, 5),
              mfile(f"{LOC}/metadata/{xm_name}", 1, DEL_SEQ, SNAP, len(dels),
                    sum(d[2] for d in dels))):
        ml += zz(1) + zz(len(r)) + r + sync
    open(os.path.join(md, ml_name), "wb").write(ml)
    metadata = {
        "format-version": 2, "location": LOC, "current-schema-id": 0,
        "schemas": [{"schema-id": 0, "type": "struct", "fields": fields}],
        "partition-specs": [{"spec-id": 0, "fields": []}], "default-spec-id": 0,
        "properties": {"schema.name-mapping.default": json.dumps(mapping)},
        "current-snapshot-id": SNAP,
        "snapshots": [{"snapshot-id": SNAP, "sequence-number": DEL_SEQ,
                       "timestamp-ms": 0,
                       "manifest-list": f"{LOC}/metadata/{ml_name}",
                       "summary": {"operation": "overwrite"}, "schema-id": 0}],
    }
    open(os.path.join(md, f"{tag}.metadata.json"), "w").write(json.dumps(metadata))


EQ_ID = write_eqdel("eqdel-id.parquet", "id", 1, pa.int64(), [2, 4])
EQ_REGION = write_eqdel("eqdel-region.parquet", "region", 2, pa.string(), ["eu"])
EQ_AMOUNT = write_eqdel("eqdel-amount.parquet", "amount", 3, pa.int32(), [30])

# nmeq: delete id IN (2, 4) -> rows 1, 3, 5
emit_eq("nmeq", SCHEMA_STD, MAP_STD, EQ_ID, 1, 2)
# nmeqstr: delete region = 'eu' -> rows 3, 4, 5
emit_eq("nmeqstr", SCHEMA_STD, MAP_STD, EQ_REGION, 2, 1)
# nmequnbound: the mapping has no name for "amount", so the delete column binds
# to nothing in the data file. The delete must still be refused, as a delete
# column missing from an older data file is; reading it as null would keep the
# row the delete removes.
emit_eq("nmequnbound", SCHEMA_STD,
        [{"field-id": 1, "names": ["id"]},
         {"field-id": 2, "names": ["region"]}],
        EQ_AMOUNT, 3, 1)
# nmeqamt: the same amount = 30 delete under the full mapping, which binds
# amount -> row 3 goes. This is the positive control for nmequnbound: it shows
# the refusal there comes from the missing mapping entry, not from deletes on
# amount (or on the last mapped column) being refused.
emit_eq("nmeqamt", SCHEMA_STD, MAP_STD, EQ_AMOUNT, 3, 1)

# Deletes on more than one column. Every arm above has one delete file with one
# equality id, so the probe reads one column; a probe that drops the mapping
# whenever it reads more than one passes them all. These arms read two.
EQ_ID_REGION = write_eqdel_cols("eqdel-id-region.parquet",
                                [("id", 1, pa.int64(), [2, 4]),
                                 ("region", 2, pa.string(), ["eu", "eu"])])
EQ_ID_AMOUNT = write_eqdel_cols("eqdel-id-amount.parquet",
                                [("id", 1, pa.int64(), [3]),
                                 ("amount", 3, pa.int32(), [30])])
# nmeq2col: one delete keyed on (id, region), as a Flink upsert keyed on both
# writes. It deletes (2,'eu') and (4,'eu'); row 4 is in 'us', so it stays.
emit_eq("nmeq2col", SCHEMA_STD, MAP_STD, EQ_ID_REGION, [1, 2], 2)
# nmeq2files: two one-column delete files on one data file, id IN (2, 4) and
# amount = 30 -> rows 1 and 5.
emit_eq("nmeq2files", SCHEMA_STD, MAP_STD, EQ_ID, 1, 2, more=[(EQ_AMOUNT, [3], 1)])
# nmeq2unbound: a delete keyed on (id, amount) under a mapping with no name for
# amount. As in nmequnbound the delete must be refused: reading amount as null
# would match no delete row and keep row 3.
emit_eq("nmeq2unbound", SCHEMA_STD,
        [{"field-id": 1, "names": ["id"]},
         {"field-id": 2, "names": ["region"]}],
        EQ_ID_AMOUNT, [1, 3], 1)

oracle = {"rows": [{"id": r[0], "region": r[1], "amount": r[2]} for r in ROWS]}
open(os.path.join(OUT, "expected.json"), "w").write(json.dumps(oracle, indent=2))
print("name-mapping fixtures written under", OUT)
