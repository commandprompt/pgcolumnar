#!/usr/bin/env python3
# Generate an Apache Iceberg warehouse with fixed(L) columns, for the
# FIXED_LEN_BYTE_ARRAY read arms of native_parquet_flba (#1792). Iceberg writes
# fixed(L) as a Parquet FIXED_LEN_BYTE_ARRAY of width L with no logical type,
# and parquet_schema advises bytea for it -- except at L = 16, where it advises
# uuid. The fixture carries both widths so a reader is checked against what an
# Iceberg writer really produces, not against a hand-built file.
#
# db.fixed: id bigint (field 1), fx fixed(3) (field 2), f16 fixed(16) (field 3).
#   id 1: fx 'abc', f16 bytes 0x00..0x0f
#   id 2: fx 'xyz', f16 bytes 0xff x 16
#   id 3: fx NULL,  f16 NULL
#
# Both metadata/ and data/ are committed (iceberg_scan opens the data file).
# Run in a host venv with pyiceberg + pyarrow (written with pyiceberg 0.12.0):
#   gen_iceberg_fixed.py test/fixtures/iceberg/warehouse_fixed [scratch-root]
import os
import sys
import shutil
import pyarrow as pa
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.schema import Schema
from pyiceberg.types import NestedField, LongType, FixedType

OUT = sys.argv[1]
ROOT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/pgc_ice_fixed"

shutil.rmtree(ROOT, ignore_errors=True)
os.makedirs(ROOT, exist_ok=True)
cat = SqlCatalog("c", uri=f"sqlite:///{ROOT}/cat.db", warehouse=f"file://{ROOT}")
cat.create_namespace("db")

schema = Schema(
    NestedField(1, "id", LongType(), required=False),
    NestedField(2, "fx", FixedType(3), required=False),
    NestedField(3, "f16", FixedType(16), required=False),
)
data = pa.table({
    "id": pa.array([1, 2, 3], pa.int64()),
    "fx": pa.array([b"abc", b"xyz", None], pa.binary(3)),
    "f16": pa.array([bytes(range(16)), b"\xff" * 16, None], pa.binary(16)),
})

t = cat.create_table("db.fixed", schema=schema)
t.append(data)

loc = t.metadata.location.replace("file://", "")
shutil.rmtree(OUT, ignore_errors=True)
os.makedirs(os.path.join(OUT, "db", "fixed"))
shutil.copytree(os.path.join(loc, "metadata"),
                os.path.join(OUT, "db", "fixed", "metadata"))
shutil.copytree(os.path.join(loc, "data"),
                os.path.join(OUT, "db", "fixed", "data"))
print("fixed root:", ROOT)
