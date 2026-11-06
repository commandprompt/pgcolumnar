#!/usr/bin/env python3
# Generate an Apache Iceberg warehouse partitioned by bucket[16] of a STRING
# column, for the Iceberg FDW collation guard (#1818). Iceberg buckets a string
# by the murmur3 of its UTF-8 bytes, so 'abc', 'ABC', 'Abc' and 'aBc' land in
# four different buckets (10, 5, 0, 7) although a case-insensitive collation
# calls them equal. A reader that prunes "s = 'ABC' COLLATE <ci>" by bucket(
# 'ABC') skips three files that hold matching rows; the suite asserts it does
# not, and that a default-collation equality still prunes.
#
# db.bystr: id bigint, s text; partitioned by bucket[16](s). One row per file:
#   1 abc (10)  2 ABC (5)  3 Abc (0)  4 aBc (7)  5 x (11)  6 y (6)
#
# Both metadata/ and data/ are committed (the FDW opens data files). Run in a
# host venv with pyiceberg==0.12.0 and pyarrow:
#   gen_iceberg_bucket_text.py test/fixtures/iceberg/warehouse_bucket_text
# pyiceberg 0.12 partitions an arrow table through pyiceberg-core. Without it,
# the shim below routes the write through pyiceberg's own pure-Python bucket
# (mmh3), the same function the closing assert prints, so the buckets on disk
# are still Iceberg's and not ours.
import os
import sys
import shutil
import pyarrow as pa
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.schema import Schema
from pyiceberg.types import NestedField, LongType, StringType
from pyiceberg.partitioning import PartitionSpec, PartitionField
from pyiceberg.transforms import BucketTransform

try:
    import pyiceberg_core  # noqa: F401
except ImportError:

    def _pure_bucket(self, source):
        fn = self.transform(source)
        return lambda arr: pa.array([fn(v) for v in arr.to_pylist()], pa.int32())

    BucketTransform.pyarrow_transform = _pure_bucket

OUT = sys.argv[1]
ROOT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/pgc_ice_bucket_text"

shutil.rmtree(ROOT, ignore_errors=True)
os.makedirs(ROOT, exist_ok=True)
cat = SqlCatalog("c", uri=f"sqlite:///{ROOT}/cat.db", warehouse=f"file://{ROOT}")
cat.create_namespace("db")

schema = Schema(
    NestedField(1, "id", LongType(), required=False),
    NestedField(2, "s", StringType(), required=False),
)
spec = PartitionSpec(
    PartitionField(source_id=2, field_id=1000, transform=BucketTransform(16), name="s_bucket"))
vals = ["abc", "ABC", "Abc", "aBc", "x", "y"]
ids = list(range(1, len(vals) + 1))
data = pa.table({
    "id": pa.array(ids, pa.int64()),
    "s": pa.array(vals, pa.string()),
})

t = cat.create_table("db.bystr", schema=schema, partition_spec=spec)
t.append(data)

loc = t.metadata.location.replace("file://", "")
shutil.rmtree(OUT, ignore_errors=True)
os.makedirs(os.path.join(OUT, "db", "bystr"))
shutil.copytree(os.path.join(loc, "metadata"),
                os.path.join(OUT, "db", "bystr", "metadata"))
shutil.copytree(os.path.join(loc, "data"),
                os.path.join(OUT, "db", "bystr", "data"))

bt = BucketTransform(16).transform(StringType())
buckets = {v: bt(v) for v in vals}
assert len(set(buckets.values())) == len(vals), buckets   # one file per row
print("bucket root:", ROOT)
print("s -> bucket[16]:", buckets)
