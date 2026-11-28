#!/usr/bin/env python3
# Generate the Apache Iceberg warehouse for the decimal pruning checks in
# test/iceberg_fdw.sh (#2086). Each table holds an Iceberg decimal column stored
# the way Spark stores a decimal of precision 18 or less: as a Parquet INT32 or
# INT64 holding the unscaled value. The reader lets a foreign table declare such
# a column int or bigint, and returns the unscaled integer.
#
# Iceberg writes a decimal's lower and upper bounds as the unscaled value in
# big-endian two's complement. An integer bound is little-endian. A reader that
# decodes a decimal bound as an integer gets a wrong range and can prune a file
# that holds a matching row.
#
#   db.dec9  : id bigint, d decimal(9,2) as Parquet INT32, unpartitioned.
#              Rows (1, 100000.00) and (2, 200000.00); unscaled 10000000 and
#              20000000. One data file, with column metrics.
#   db.dec18 : the same rows, d decimal(18,2) as Parquet INT64.
#   db.dec9_bucket : the dec9 rows, partitioned by bucket[8](d), one file per
#              bucket, written by pyiceberg (0.12 stores a decimal as an
#              integer). Iceberg hashes a decimal's unscaled big-endian bytes,
#              not a long, so a bucket computed from an int constant is wrong.
#   db.pfx   : id long, d_x long, d decimal(9,2) as Parquet INT32. Rows
#              (1, 5, 100000.00) and (2, 6, 200000.00), one data file. The long
#              field d_x comes before d and its name starts with "d", so a
#              field-type lookup that matches names by prefix reads d as a long.
#   db.pfx2  : id long, d long, d_x decimal(9,2) as Parquet INT32, the same
#              rows. Here the long d comes first and its name is a prefix of
#              d_x, so a lookup that compares only the field name's length
#              reads d_x as a long. pfx tests the other direction.
#   db.boolt : id long, b boolean. Two data files: ids 1, 2 with b false, and
#              ids 3, 4 with b true. Iceberg writes a boolean bound as one byte.
#   db.booli : id long, b int, values 5 to 8, one data file. A foreign table
#              that declares b bool is one the reader refuses (42804), so the
#              FDW must not read b's int bounds as boolean bounds and prune.
#   db.booll : id long, b long, values 5 to 8, one data file. The same refusal
#              for a bool column over an Iceberg long.
#   db.byint : k int, partitioned by bucket[8](k), k = 1 to 8, one file per
#              bucket. Iceberg hashes an int as an 8-byte long, so the FDW may
#              prune by bucket on an Iceberg int. Writing it needs
#              pyiceberg-core for the bucket transform.
#   db.two   : k int, d decimal(9,2), partitioned by bucket[2](d) then
#              bucket[4](k). k = 1 to 16, d = 1.00 in every row, so the files
#              split by k's bucket only: four files. The bucket map must skip
#              the gated decimal field and still keep k's bucket field.
#   db.prom  : id long, r string, v int promoted to long (#2190), identity
#              partitioned by r, with column metrics. d0 (1, 'a', -5) and
#              d1 (2, 'b', 7) were added before the promotion and store v as
#              Parquet INT32; d2 (3, 'c', 9) was added after it and stores v as
#              INT64. The reader refuses an int column over d2 only, so a filter
#              that prunes d2 used to hide the refusal.
#   db.boolp : id long, b boolean, identity partitioned by b (#2190). One file
#              with ids 1, 2 (b false), one with ids 3, 4 (b true). The
#              partition cells are 0 and 1, so an int column's filter could
#              prune on them while the reader refuses an int over a boolean.
#   db.timep : id long, t time, identity partitioned by t (#2190). One file
#              with ids 1, 2 (t 01:00), one with id 3 (t 02:00). The cells are
#              microseconds, a long, which an int column's filter could prune
#              on while the reader refuses an int over a time (Parquet INT64).
#
# Run: ~/icevenv/bin/python gen_iceberg_decimal.py <out warehouse> <scratch root>
#      [table ...]
# with pyiceberg 0.12.0, pyiceberg-core 0.10.1 and pyarrow. Name tables to write only those; with no
# names it writes all of them.
import datetime
import decimal
import os
import shutil
import sys

import pyarrow as pa
import pyarrow.parquet as pq
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.transforms import BucketTransform, IdentityTransform
from pyiceberg.types import (BooleanType, DecimalType, IntegerType, LongType,
                             NestedField, StringType, TimeType)

OUT = sys.argv[1]
ROOT = sys.argv[2]
ONLY = set(sys.argv[3:])

shutil.rmtree(ROOT, ignore_errors=True)
os.makedirs(ROOT, exist_ok=True)
cat = SqlCatalog("c", uri=f"sqlite:///{ROOT}/cat.db", warehouse=f"file://{ROOT}")
cat.create_namespace("db")
D = decimal.Decimal
ROWS = [(1, D("100000.00")), (2, D("200000.00"))]


def make(name, precision, spec, files, append=False):
    if ONLY and name not in ONLY:
        return
    schema = Schema(
        NestedField(1, "id", LongType(), required=False),
        NestedField(2, "d", DecimalType(precision, 2), required=False),
    )
    t = cat.create_table(f"db.{name}", schema=schema, partition_spec=spec)
    loc = t.metadata.location.replace("file://", "")
    os.makedirs(os.path.join(loc, "data"), exist_ok=True)
    paths = []
    for i, rows in enumerate(files):
        data = pa.table({
            "id": pa.array([r[0] for r in rows], pa.int64()),
            "d": pa.array([r[1] for r in rows], pa.decimal128(precision, 2)),
        })
        if append:
            t.append(data)
            continue
        f = os.path.join(loc, "data", f"d{i}.parquet")
        pq.write_table(data, f, store_decimal_as_integer=True)
        paths.append(f"file://{f}")
    if paths:
        t.add_files(paths)
    publish(name, t, loc, 2)


def make_plain(name, fields, files):
    """An unpartitioned table with one data file per entry of `files`.
    `fields` lists (name, Iceberg type, pyarrow type); field ids run from 1.
    `files` lists rows, each a tuple in field order."""
    if ONLY and name not in ONLY:
        return
    schema = Schema(*[NestedField(i + 1, f[0], f[1], required=False)
                      for i, f in enumerate(fields)])
    t = cat.create_table(f"db.{name}", schema=schema)
    loc = t.metadata.location.replace("file://", "")
    os.makedirs(os.path.join(loc, "data"), exist_ok=True)
    paths = []
    for i, rows in enumerate(files):
        data = pa.table({f[0]: pa.array([r[k] for r in rows], f[2])
                         for k, f in enumerate(fields)})
        f = os.path.join(loc, "data", f"d{i}.parquet")
        pq.write_table(data, f, store_decimal_as_integer=True)
        paths.append(f"file://{f}")
    t.add_files(paths)
    publish(name, t, loc, len(fields))


def make_bucket_int(name):
    """An Iceberg int k = 1 to 8, partitioned by bucket[8](k), appended by
    pyiceberg so that pyiceberg computes the buckets."""
    if ONLY and name not in ONLY:
        return
    schema = Schema(NestedField(1, "k", IntegerType(), required=False))
    spec = PartitionSpec(PartitionField(source_id=1, field_id=1000,
                                        transform=BucketTransform(8),
                                        name="k_bucket"))
    t = cat.create_table(f"db.{name}", schema=schema, partition_spec=spec)
    loc = t.metadata.location.replace("file://", "")
    t.append(pa.table({"k": pa.array(range(1, 9), pa.int32())}))
    publish(name, t, loc, 1)


def make_bucket_two(name):
    """k int and d decimal(9,2), partitioned by bucket[2](d) then bucket[4](k),
    appended by pyiceberg so that pyiceberg computes the buckets."""
    if ONLY and name not in ONLY:
        return
    schema = Schema(NestedField(1, "k", IntegerType(), required=False),
                    NestedField(2, "d", DecimalType(9, 2), required=False))
    spec = PartitionSpec(
        PartitionField(source_id=2, field_id=1000, transform=BucketTransform(2),
                       name="d_bucket"),
        PartitionField(source_id=1, field_id=1001, transform=BucketTransform(4),
                       name="k_bucket"))
    t = cat.create_table(f"db.{name}", schema=schema, partition_spec=spec)
    loc = t.metadata.location.replace("file://", "")
    t.append(pa.table({
        "k": pa.array(range(1, 17), pa.int32()),
        "d": pa.array([D("1.00")] * 16, pa.decimal128(9, 2)),
    }))
    publish(name, t, loc, 1)


def make_promoted(name):
    """id long, r string, v int: two files added while v is an int (Parquet
    INT32), then v promoted to long and one file added (INT64). Identity
    partitioned by r, so each file has its own partition value."""
    if ONLY and name not in ONLY:
        return
    schema = Schema(NestedField(1, "id", LongType(), required=False),
                    NestedField(2, "r", StringType(), required=False),
                    NestedField(3, "v", IntegerType(), required=False))
    spec = PartitionSpec(PartitionField(source_id=2, field_id=1000,
                                        transform=IdentityTransform(), name="r"))
    t = cat.create_table(f"db.{name}", schema=schema, partition_spec=spec)
    loc = t.metadata.location.replace("file://", "")
    os.makedirs(os.path.join(loc, "data"), exist_ok=True)

    def put(i, row, vtype):
        f = os.path.join(loc, "data", f"d{i}.parquet")
        pq.write_table(pa.table({"id": pa.array([row[0]], pa.int64()),
                                 "r": pa.array([row[1]], pa.string()),
                                 "v": pa.array([row[2]], vtype)}), f)
        return f"file://{f}"
    t.add_files([put(0, (1, "a", -5), pa.int32()), put(1, (2, "b", 7), pa.int32())])
    with t.update_schema() as u:
        u.update_column("v", LongType())
    t = cat.load_table(f"db.{name}")
    t.add_files([put(2, (3, "c", 9), pa.int64())])
    publish(name, t, loc, 3)


def make_bool_identity(name):
    """id long, b boolean, identity partitioned by b, appended by pyiceberg so
    that pyiceberg computes the partition values."""
    if ONLY and name not in ONLY:
        return
    schema = Schema(NestedField(1, "id", LongType(), required=False),
                    NestedField(2, "b", BooleanType(), required=False))
    spec = PartitionSpec(PartitionField(source_id=2, field_id=1000,
                                        transform=IdentityTransform(), name="b"))
    t = cat.create_table(f"db.{name}", schema=schema, partition_spec=spec)
    loc = t.metadata.location.replace("file://", "")
    t.append(pa.table({"id": pa.array([1, 2, 3, 4], pa.int64()),
                       "b": pa.array([False, False, True, True], pa.bool_())}))
    publish(name, t, loc, 2)


def make_time_identity(name):
    """id long, t time, identity partitioned by t, appended by pyiceberg."""
    if ONLY and name not in ONLY:
        return
    schema = Schema(NestedField(1, "id", LongType(), required=False),
                    NestedField(2, "t", TimeType(), required=False))
    spec = PartitionSpec(PartitionField(source_id=2, field_id=1000,
                                        transform=IdentityTransform(), name="t"))
    t = cat.create_table(f"db.{name}", schema=schema, partition_spec=spec)
    loc = t.metadata.location.replace("file://", "")
    hour = datetime.time
    t.append(pa.table({"id": pa.array([1, 2, 3], pa.int64()),
                       "t": pa.array([hour(1), hour(1), hour(2)], pa.time64("us"))}))
    publish(name, t, loc, 2)


def publish(name, t, loc, fid):
    """Copy the table's metadata and data into the output warehouse, and print
    each data file's partition and its bounds for field `fid`."""
    t = cat.load_table(f"db.{name}")
    dst = os.path.join(OUT, "db", name)
    shutil.rmtree(dst, ignore_errors=True)
    os.makedirs(dst)
    shutil.copytree(os.path.join(loc, "metadata"), os.path.join(dst, "metadata"))
    shutil.copytree(os.path.join(loc, "data"), os.path.join(dst, "data"))
    for task in t.scan().plan_files():
        print(f"{name}: {os.path.basename(task.file.file_path)} "
              f"partition={task.file.partition!r} "
              f"lower={task.file.lower_bounds.get(fid)!r} "
              f"upper={task.file.upper_bounds.get(fid)!r}")


make("dec9", 9, PartitionSpec(), [ROWS])
make("dec18", 18, PartitionSpec(), [ROWS])
make("dec9_bucket", 9,
     PartitionSpec(PartitionField(source_id=2, field_id=1000,
                                  transform=BucketTransform(8), name="d_bucket")),
     [ROWS], append=True)
make_plain("pfx",
           [("id", LongType(), pa.int64()), ("d_x", LongType(), pa.int64()),
            ("d", DecimalType(9, 2), pa.decimal128(9, 2))],
           [[(1, 5, D("100000.00")), (2, 6, D("200000.00"))]])
make_plain("pfx2",
           [("id", LongType(), pa.int64()), ("d", LongType(), pa.int64()),
            ("d_x", DecimalType(9, 2), pa.decimal128(9, 2))],
           [[(1, 5, D("100000.00")), (2, 6, D("200000.00"))]])
make_plain("boolt",
           [("id", LongType(), pa.int64()), ("b", BooleanType(), pa.bool_())],
           [[(1, False), (2, False)], [(3, True), (4, True)]])
make_plain("booli",
           [("id", LongType(), pa.int64()), ("b", IntegerType(), pa.int32())],
           [[(5, 5), (6, 6), (7, 7), (8, 8)]])
make_plain("booll",
           [("id", LongType(), pa.int64()), ("b", LongType(), pa.int64())],
           [[(5, 5), (6, 6), (7, 7), (8, 8)]])
make_bucket_int("byint")
make_bucket_two("two")
make_promoted("prom")
make_bool_identity("boolp")
make_time_identity("timep")
