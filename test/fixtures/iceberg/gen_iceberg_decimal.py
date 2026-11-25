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
#   db.boolt : id long, b boolean. Two data files: ids 1, 2 with b false, and
#              ids 3, 4 with b true. Iceberg writes a boolean bound as one byte.
#   db.booli : id long, b int, values 5 to 8, one data file. A foreign table
#              that declares b bool is one the reader refuses (42804), so the
#              FDW must not read b's int bounds as boolean bounds and prune.
#
# Run: ~/icevenv/bin/python gen_iceberg_decimal.py <out warehouse> <scratch root>
#      [table ...]
# with pyiceberg 0.12.0 and pyarrow. Name tables to write only those; with no
# names it writes all of them.
import decimal
import os
import shutil
import sys

import pyarrow as pa
import pyarrow.parquet as pq
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.transforms import BucketTransform
from pyiceberg.types import (BooleanType, DecimalType, IntegerType, LongType,
                             NestedField)

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
make_plain("boolt",
           [("id", LongType(), pa.int64()), ("b", BooleanType(), pa.bool_())],
           [[(1, False), (2, False)], [(3, True), (4, True)]])
make_plain("booli",
           [("id", LongType(), pa.int64()), ("b", IntegerType(), pa.int32())],
           [[(5, 5), (6, 6), (7, 7), (8, 8)]])
