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
#
# Run: ~/icevenv/bin/python gen_iceberg_decimal.py <out warehouse> <scratch root>
# with pyiceberg 0.12.0 and pyarrow.
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
from pyiceberg.types import DecimalType, LongType, NestedField

OUT = sys.argv[1]
ROOT = sys.argv[2]

shutil.rmtree(ROOT, ignore_errors=True)
os.makedirs(ROOT, exist_ok=True)
cat = SqlCatalog("c", uri=f"sqlite:///{ROOT}/cat.db", warehouse=f"file://{ROOT}")
cat.create_namespace("db")
D = decimal.Decimal
ROWS = [(1, D("100000.00")), (2, D("200000.00"))]


def make(name, precision, spec, files, append=False):
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
    t = cat.load_table(f"db.{name}")
    dst = os.path.join(OUT, "db", name)
    shutil.rmtree(dst, ignore_errors=True)
    os.makedirs(dst)
    shutil.copytree(os.path.join(loc, "metadata"), os.path.join(dst, "metadata"))
    shutil.copytree(os.path.join(loc, "data"), os.path.join(dst, "data"))
    for task in t.scan().plan_files():
        print(f"{name}: {os.path.basename(task.file.file_path)} "
              f"partition={task.file.partition!r} "
              f"lower={task.file.lower_bounds.get(2)!r} "
              f"upper={task.file.upper_bounds.get(2)!r}")


make("dec9", 9, PartitionSpec(), [ROWS])
make("dec18", 18, PartitionSpec(), [ROWS])
make("dec9_bucket", 9,
     PartitionSpec(PartitionField(source_id=2, field_id=1000,
                                  transform=BucketTransform(8), name="d_bucket")),
     [ROWS], append=True)
