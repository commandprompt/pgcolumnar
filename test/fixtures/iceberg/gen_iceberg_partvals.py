#!/usr/bin/env python3
# Generate an Apache Iceberg warehouse whose tables are identity-partitioned on
# the value types that iceberg_data_files used to render wrongly (#1816, #1791):
#
#   db.dec     decimal(10,2)   rendered '?' before #1816
#   db.dec38   decimal(38,10)  a 16-byte fixed, the widest Iceberg decimal
#   db.dec0    decimal(9,0)    scale 0: no decimal point
#   db.flt     float           rendered '?'
#   db.dbl     double          rendered '?'
#   db.bin     binary          raw bytes copied into text (invalid UTF-8, a NUL
#                              cut the value short)
#   db.str     string          NULL and '' both rendered 's=', and ',' '=' were
#                              not escaped (#1791)
#
# Each table has two columns, `id long` and the partition column `v`, and one
# data file per partition value. Like gen_iceberg_warehouse.py, the tables are
# written at a STABLE root (argv[2], default /tmp/pgc_ice_partvals) and only
# their metadata/ subtrees are committed: the lister reads manifests, not data.
#
# The oracle (expected_files.json) holds, per table, the values written, the
# text each one must render as under the documented format
# (docs/sql-reference.md, iceberg_data_files), and the partition values that
# pyiceberg's own scan planning returns (plan_files), as Python reprs:
#   decimal   the exact decimal, scale digits after the point     v=-0.50
#   float     the shortest text that reads back as the same value  v=0.1
#   binary    \x and lowercase hex, as bytea prints it            v=\xfffe
#   string    the value with '%', ',', '=' and control bytes
#             percent-encoded                                      v=a%2Cb%3Dc
#   NULL      the field name alone, with no '='                    v
# Non-ASCII strings are kept as they are in a UTF8 or SQL_ASCII database; the
# oracle records the UTF8 form, and the alternative for other encodings.
#
# Written with pyiceberg 0.12.0 and pyarrow 26.0.0.
import json
import os
import shutil
import struct
import sys
from decimal import Decimal

import pyarrow as pa
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.transforms import IdentityTransform
from pyiceberg.types import (BinaryType, DecimalType, DoubleType, FloatType,
                             LongType, NestedField, StringType)

OUT = sys.argv[1]                                   # fixtures/iceberg/warehouse_partvals
ROOT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/pgc_ice_partvals"

TABLES = [
    ("dec", DecimalType(10, 2),
     # -2.56 is unscaled -256, low byte 0x00: the negation must carry
     [Decimal("-0.50"), Decimal("12.25"), Decimal("0.05"), Decimal("-0.05"),
      Decimal("-2.56")]),
    ("dec38", DecimalType(38, 10),
     [Decimal("-1234567890123456789012345678.0123456789"), Decimal("0E-10")]),
    ("dec0", DecimalType(9, 0), [Decimal("12"), Decimal("-7"), Decimal("0")]),
    ("flt", FloatType(), [1.5, -2.25, 0.1]),
    ("dbl", DoubleType(), [1.5, -2.25, 0.1]),
    ("bin", BinaryType(), [b"\xff\xfe", b"a\x00b"]),
    ("str", StringType(), ["", None, "a,b=c", "50%", "plain", "café"]),
]


def pct(raw, keep_high):
    """Percent-encode '%', ',', '=', control bytes and DEL; bytes >= 0x80 too unless
    keep_high (a valid UTF-8 value in a UTF8 or SQL_ASCII database)."""
    out = bytearray()
    for b in raw:
        if b in b"%,=" or b < 0x20 or b == 0x7F or (b >= 0x80 and not keep_high):
            out += ("%%%02X" % b).encode()
        else:
            out.append(b)
    return out.decode("utf-8")


def shortest_float32(v):
    for digits in range(1, 10):
        s = "%.*g" % (digits, v)
        if struct.unpack("<f", struct.pack("<f", float(s)))[0] == \
           struct.unpack("<f", struct.pack("<f", v))[0]:
            return s
    raise ValueError(v)


def render(typ, v, keep_high=True):
    if v is None:
        return "v"
    if isinstance(typ, DecimalType):
        return "v=" + format(v, "f")
    if isinstance(typ, FloatType):
        return "v=" + shortest_float32(v)
    if isinstance(typ, DoubleType):
        return "v=" + repr(v)
    if isinstance(typ, BinaryType):
        return "v=\\x" + v.hex()
    raw = v.encode("utf-8")
    return "v=" + pct(raw, keep_high)


shutil.rmtree(ROOT, ignore_errors=True)
os.makedirs(ROOT, exist_ok=True)
cat = SqlCatalog("c", uri=f"sqlite:///{ROOT}/cat.db", warehouse=f"file://{ROOT}")
cat.create_namespace("db")

shutil.rmtree(OUT, ignore_errors=True)
os.makedirs(OUT, exist_ok=True)
oracle = {"recorded_root": ROOT, "tables": {}}

for name, typ, vals in TABLES:
    schema = Schema(NestedField(1, "id", LongType(), required=False),
                    NestedField(2, "v", typ, required=False))
    spec = PartitionSpec(PartitionField(2, 1000, IdentityTransform(), "v"))
    t = cat.create_table(f"db.{name}", schema=schema, partition_spec=spec,
                         properties={"format-version": "2"})
    t.append(pa.Table.from_pylist([{"id": i, "v": v} for i, v in enumerate(vals)],
                                  schema=t.schema().as_arrow()))
    t = cat.load_table(f"db.{name}")

    loc = t.metadata.location.replace("file://", "")
    shutil.copytree(os.path.join(loc, "metadata"),
                    os.path.join(OUT, "db", name, "metadata"))

    # The rendering is derived from the values WRITTEN. plan_files agrees for
    # every value but one: pyiceberg 0.12.0 reads the double 0.1 back as
    # 0.10000000149011612 (float32 precision), although the manifest holds the
    # exact double 0.1 (bytes 9a9999999999b93f). The planned values are kept
    # beside the rendering so that difference stays visible.
    planned = sorted(repr(task.file.partition[0]) for task in t.scan().plan_files())
    rows = sorted(({"value": repr(v), "partition": render(typ, v),
                    "partition_non_utf8_db": render(typ, v, keep_high=False)}
                   for v in vals), key=lambda r: r["partition"])
    oracle["tables"][name] = {
        "metadata_basename": os.path.basename(t.metadata_location),
        "type": str(typ),
        "files": rows,
        "planned_values": planned,
    }

with open(os.path.join(OUT, "expected_files.json"), "w") as f:
    json.dump(oracle, f, indent=2, sort_keys=True, ensure_ascii=False)

print("partvals root:", ROOT)
print(json.dumps(oracle, indent=2, sort_keys=True, ensure_ascii=False))
