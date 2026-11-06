"""Write the native_parquet_codecs fixtures into DIR (argv[1]), for both twins.

c_none / c_gzip / c_zstd / c_lz4.parquet hold the same 20000 rows, each written with one
codec; codecs.txt records the compression pyarrow actually stored for each file's first
column (one "<codec> <COMPRESSION>" line), so a test can prove which codec it read.
badhdr.parquet is a gzip page whose header declares uncompressed_page_size -1.

allnull_gzip / allnull_zstd / allnull_lz4.parquet hold 3 rows of (id bigint, s text) with
s NULL in every row (#1786). pyarrow writes s with a dictionary page that holds no values,
so its declared uncompressed size is 0. codecs.txt gets one "allnull_<codec> <COMPRESSION>
dict" line for each, read from column s's own metadata, so a test can prove the file has
the codec and the dictionary page it claims.

The rows are the generator both twins also compute in SQL:
    id = g, grp = g % 50, val = g * 1.5, s = 'row-' || (g % 100),  g in 0 .. 19999
Written fresh for pgColumnar.
"""
import sys

import pyarrow as pa
import pyarrow.parquet as pq

W = sys.argv[1]
N = 20000
tbl = pa.table({
    "id": pa.array(range(N), pa.int32()),
    "grp": pa.array([i % 50 for i in range(N)], pa.int32()),
    "val": pa.array([i * 1.5 for i in range(N)], pa.float64()),
    "s": pa.array([f"row-{i % 100}" for i in range(N)]),
})
with open(f"{W}/codecs.txt", "w") as out:
    for codec in ["none", "gzip", "zstd", "lz4"]:
        pq.write_table(tbl, f"{W}/c_{codec}.parquet", compression=codec)
        c = pq.ParquetFile(f"{W}/c_{codec}.parquet").metadata.row_group(0).column(0).compression
        out.write(f"{codec} {c}\n")
    allnull = pa.table({"id": pa.array([1, 2, 3], pa.int64()),
                        "s": pa.array([None] * 3, pa.string())})
    for codec in ["gzip", "zstd", "lz4"]:
        pq.write_table(allnull, f"{W}/allnull_{codec}.parquet", compression=codec)
        m = pq.ParquetFile(f"{W}/allnull_{codec}.parquet").metadata.row_group(0).column(1)
        out.write(f"allnull_{codec} {m.compression}{' dict' if m.has_dictionary_page else ''}\n")

# A gzip page whose header's uncompressed_page_size (field 2) is rewritten to zigzag(-1).
pq.write_table(pa.table({"a": pa.array([1, 2, 3], pa.int32())}),
               f"{W}/badhdr.parquet", compression="gzip", use_dictionary=False)
raw = bytearray(open(f"{W}/badhdr.parquet", "rb").read())
pos = 4                                   # the first PageHeader follows the "PAR1" magic
last = 0
patched = False
for _ in range(6):
    b = raw[pos]
    pos += 1
    t, d = b & 0x0F, b >> 4
    if t == 0:
        break
    fid = last + d if d else None
    last = fid
    start = pos
    while raw[pos] & 0x80:
        pos += 1
    pos += 1
    if fid == 2:
        assert pos - start == 1, (start, pos)
        raw[start] = 0x01                 # zigzag(-1)
        patched = True
        break
assert patched, "did not find uncompressed_page_size"
open(f"{W}/badhdr.parquet", "wb").write(bytes(raw))
