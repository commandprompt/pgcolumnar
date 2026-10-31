#!/usr/bin/env python3
# Generate the Apache Iceberg warehouse for the declared-typmod pruning checks in
# test/iceberg_fdw.sh (#1790). The reader applies a declared typmod to every value
# it returns, so a value can read back different from the one the writer stored.
# The FDW prunes files by the value the writer stored. These two tables hold a
# value that a typmod changes in a way that moves it into another partition's
# answer, so pruning on the stored value would drop a row the query must return.
# Column metrics are disabled, so the partition value is the only pruning input.
#
#   db.byhour_frac : id bigint, ts timestamp; hour(ts).
#                    id 1 at 2021-03-01 00:59:59.7 (hour bucket 00),
#                    id 2 at 2021-03-01 05:00:00   (hour bucket 05).
#                    Read as timestamp(0), id 1 rounds to 01:00:00.
#   db.byregion_pad: id bigint, region string; identity(region).
#                    id 1 with 'eu  ' (two trailing spaces), id 2 with 'us'.
#                    Read as varchar(2), id 1 reads 'eu'.
#
# Run: ~/icevenv/bin/python gen_iceberg_typmod.py <out warehouse> <scratch root>
# with pyiceberg 0.12.0, pyarrow and pyiceberg-core.
import datetime
import os
import shutil
import sys

import pyarrow as pa
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.transforms import HourTransform, IdentityTransform
from pyiceberg.types import LongType, NestedField, StringType, TimestampType

OUT = sys.argv[1]
ROOT = sys.argv[2]

shutil.rmtree(ROOT, ignore_errors=True)
os.makedirs(ROOT, exist_ok=True)
cat = SqlCatalog("c", uri=f"sqlite:///{ROOT}/cat.db", warehouse=f"file://{ROOT}")
cat.create_namespace("db")


def make(name, col, col_type, arrow, transform, rows):
    schema = Schema(
        NestedField(1, "id", LongType(), required=False),
        NestedField(2, col, col_type, required=False),
    )
    spec = PartitionSpec(
        PartitionField(source_id=2, field_id=1000, transform=transform,
                       name=col + "_part"))
    data = pa.table({
        "id": pa.array([r[0] for r in rows], pa.int64()),
        col: pa.array([r[1] for r in rows], arrow),
    })
    t = cat.create_table(f"db.{name}", schema=schema, partition_spec=spec,
                         properties={"write.metadata.metrics.default": "none"})
    t.append(data)
    loc = t.metadata.location.replace("file://", "")
    dst = os.path.join(OUT, "db", name)
    shutil.rmtree(dst, ignore_errors=True)
    os.makedirs(dst)
    shutil.copytree(os.path.join(loc, "metadata"), os.path.join(dst, "metadata"))
    shutil.copytree(os.path.join(loc, "data"), os.path.join(dst, "data"))
    parts = sorted(repr(task.file.partition[0]) for task in t.scan().plan_files())
    print(f"{name}: partitions={parts}")


make("byhour_frac", "ts", TimestampType(), pa.timestamp("us"), HourTransform(),
     [(1, datetime.datetime(2021, 3, 1, 0, 59, 59, 700000)),
      (2, datetime.datetime(2021, 3, 1, 5, 0, 0))])
make("byregion_pad", "region", StringType(), pa.string(), IdentityTransform(),
     [(1, "eu  "), (2, "us")])
