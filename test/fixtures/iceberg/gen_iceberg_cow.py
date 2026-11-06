#!/usr/bin/env python3
# Generate an Apache Iceberg warehouse whose current snapshots came from a
# copy-on-write delete, for the iceberg_data_files suite (#1791). A copy-on-write
# delete rewrites the data file it touches and records the old file in the new
# snapshot's manifest as a DELETED entry (status 2). That entry is not live: the
# lister must skip it, not list it and not refuse the table.
#
# Two tables, both format version 2, one column `id long`:
#   db.cow      ids 0..9 appended, then delete id >= 5. The current snapshot
#               holds the rewritten 5-row file (status 1) and the removed
#               10-row file (status 2).
#   db.cow_all  ids 0..9 appended, then every row deleted. The current
#               snapshot holds only the removed file (status 2).
#
# Like gen_iceberg_warehouse.py, the tables are written at a STABLE root
# (argv[2], default /tmp/pgc_ice_cow) and only their metadata/ subtrees are
# committed: the lister reads manifests, not data files.
#
# The oracle (expected_files.json) holds, per table, the live data files
# pyiceberg's own scan planning returns (plan_files) and every manifest entry
# of the current snapshot with its status, so a test can confirm the fixture
# really carries a status-2 entry.
#
# Written with pyiceberg 0.12.0 and pyarrow 26.0.0.
import json
import os
import shutil
import sys

import pyarrow as pa
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.expressions import AlwaysTrue, GreaterThanOrEqual
from pyiceberg.schema import Schema
from pyiceberg.types import LongType, NestedField

OUT = sys.argv[1]                                   # fixtures/iceberg/warehouse_cow
ROOT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/pgc_ice_cow"

shutil.rmtree(ROOT, ignore_errors=True)
os.makedirs(ROOT, exist_ok=True)
cat = SqlCatalog("c", uri=f"sqlite:///{ROOT}/cat.db", warehouse=f"file://{ROOT}")
cat.create_namespace("db")

schema = Schema(NestedField(1, "id", LongType(), required=False))
data = pa.table({"id": pa.array(list(range(10)), pa.int64())})

shutil.rmtree(OUT, ignore_errors=True)
os.makedirs(OUT, exist_ok=True)
oracle = {"recorded_root": ROOT, "tables": {}}

for name, predicate in (("cow", GreaterThanOrEqual("id", 5)), ("cow_all", AlwaysTrue())):
    t = cat.create_table(f"db.{name}", schema=schema,
                         properties={"format-version": "2",
                                     "write.delete.mode": "copy-on-write"})
    t.append(data)
    t.delete(predicate)
    t = cat.load_table(f"db.{name}")

    loc = t.metadata.location.replace("file://", "")
    shutil.copytree(os.path.join(loc, "metadata"),
                    os.path.join(OUT, "db", name, "metadata"))

    io = t.io
    snap = t.current_snapshot()
    entries = []
    for m in snap.manifests(io):
        for e in m.fetch_manifest_entry(io, discard_deleted=False):
            entries.append({"file_basename": os.path.basename(e.data_file.file_path),
                            "record_count": e.data_file.record_count,
                            "content": int(e.data_file.content),
                            "status": int(e.status)})
    live = []
    for task in t.scan().plan_files():
        live.append({"file_basename": os.path.basename(task.file.file_path),
                     "record_count": task.file.record_count,
                     "delete_files": len(task.delete_files)})
    oracle["tables"][name] = {
        "metadata_basename": os.path.basename(t.metadata_location),
        "operation": str(snap.summary.operation).split(".")[-1].lower(),
        "entries": sorted(entries, key=lambda r: (r["status"], r["file_basename"])),
        "live_files": sorted(live, key=lambda r: r["file_basename"]),
        "rows": t.scan().to_arrow().num_rows,
    }

with open(os.path.join(OUT, "expected_files.json"), "w") as f:
    json.dump(oracle, f, indent=2, sort_keys=True)

print("cow root:", ROOT)
print(json.dumps(oracle, indent=2, sort_keys=True))
