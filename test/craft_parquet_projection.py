"""Write the files test/native_parquet_projection.sh and its pytest twin read.

    python3 test/craft_parquet_projection.py DIR

DIR/proj.parquet     a, b, c, d over 5000 rows in row groups of 1000, from the same
                     generator the suites rebuild in SQL as their oracle:
                     a = i, b = 2i, c = 's' || (i % 20), d = i / 3.0
DIR/corrupt.parquet  g, bad over 1000 rows, with the second column's data page
                     smashed, so decoding `bad` fails while `g` and the footer stay
                     intact.

Both harnesses run this one crafter, so a file can never differ between them.
"""
import sys

import pyarrow as pa
import pyarrow.parquet as pq

W = sys.argv[1]
n = 5000
pq.write_table(pa.table({
    "a": pa.array(range(n), pa.int32()),
    "b": pa.array([i * 2 for i in range(n)], pa.int64()),
    "c": pa.array([f"s{i % 20}" for i in range(n)]),
    "d": pa.array([i / 3.0 for i in range(n)], pa.float64()),
}), f"{W}/proj.parquet", row_group_size=1000)

pq.write_table(pa.table({"g": pa.array(range(1000), pa.int32()),
                         "bad": pa.array(range(1000), pa.int32())}),
               f"{W}/corrupt.parquet", row_group_size=1000, use_dictionary=False)
md = pq.ParquetFile(f"{W}/corrupt.parquet").metadata.row_group(0).column(1)
off = md.data_page_offset
raw = bytearray(open(f"{W}/corrupt.parquet", "rb").read())
# smash the second column's page header and the start of its data, leaving the
# first column and the footer intact
for i in range(off, off + 32):
    raw[i] = 0xFF
open(f"{W}/corrupt.parquet", "wb").write(bytes(raw))
print(f"  corrupted column 'bad' at offset {off}", file=sys.stderr)
