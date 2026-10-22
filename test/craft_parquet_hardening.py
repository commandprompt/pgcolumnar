"""craft_parquet_hardening.py DIR: write the Parquet files test/native_parquet_hardening.sh
and its pytest twin read: the narrowing binds (out of range and in range) and a
two-column file whose row group carries one column chunk. One writer for both
twins, so neither can drift from the other. Needs pyarrow."""
import sys

import pyarrow as pa
import pyarrow.parquet as pq

W = sys.argv[1]

# ---- narrowing: out-of-range values must raise, not wrap --------------------
# int2 bound over INT32: 40000 does not fit int16 (would wrap to -25536)
pq.write_table(pa.table({"c": pa.array([30000, 40000], pa.int32())}),
               f"{W}/i2_bad.parquet")
# same bind, all values in range: must keep working
pq.write_table(pa.table({"c": pa.array([-30000, 0, 30000], pa.int32())}),
               f"{W}/i2_ok.parquet")
# time bound over INT64 micros: beyond one day is not a time
pq.write_table(pa.table({"c": pa.array([0, 86400000000 + 1], pa.int64())}),
               f"{W}/time_bad.parquet")
pq.write_table(pa.table({"c": pa.array([0, 3600000000], pa.int64())}),
               f"{W}/time_ok.parquet")
# date bound over INT32 days since epoch. INT32 max does NOT overflow the epoch
# offset (2147483647 - 10957 stays in range); it is rejected by IS_VALID_DATE.
pq.write_table(pa.table({"c": pa.array([0, 2147483647], pa.int32())}),
               f"{W}/date_bad.parquet")
# INT32 min is what overflows the subtraction itself, exercising the other branch
pq.write_table(pa.table({"c": pa.array([0, -2147483648], pa.int32())}),
               f"{W}/date_ovf.parquet")
pq.write_table(pa.table({"c": pa.array([0, 19000], pa.int32())}),
               f"{W}/date_ok.parquet")
# timestamp bound over INT64 micros: INT64 min overflows the epoch-offset
# subtraction. INT64 max does NOT -- it still lands inside PostgreSQL's timestamp
# range (year ~294276), so it is deliberately not the case tested here.
pq.write_table(pa.table({"c": pa.array([0, -9223372036854775808], pa.int64())}),
               f"{W}/ts_bad.parquet")
pq.write_table(pa.table({"c": pa.array([0, 1700000000000000], pa.int64())}),
               f"{W}/ts_ok.parquet")

# ---- malformed row-group chunk list ----------------------------------------
pq.write_table(pa.table({"a": pa.array([1, 2, 3], pa.int32()),
                         "b": pa.array([4, 5, 6], pa.int32())}),
               f"{W}/two.parquet")

# Walk the Thrift-compact footer to the exact byte holding the row group's
# `columns` list header, then claim it holds one fewer chunk than it does.
# Blind byte-searching is not reliable here: the same byte value occurs in the
# data pages, so the header has to be located structurally.
raw = bytearray(open(f"{W}/two.parquet", "rb").read())
flen = int.from_bytes(raw[-8:-4], "little")
base = len(raw) - 8 - flen          # start of FileMetaData
pos = base

def varint():
    global pos
    shift = 0; out = 0
    while True:
        b = raw[pos]; pos += 1
        out |= (b & 0x7F) << shift
        if not b & 0x80:
            return out
        shift += 7

def zigzag():
    u = varint()
    return (u >> 1) ^ -(u & 1)

def list_hdr():
    global pos
    b = raw[pos]; pos += 1
    size = (b >> 4) & 0x0F
    et = b & 0x0F
    if size == 0x0F:
        size = varint()
    return size, et

def skip(t):
    global pos
    if t in (1, 2):          return                 # bool: value is in the header
    if t == 3:               pos += 1               # byte
    elif t in (4, 5, 6):     zigzag()               # i16/i32/i64
    elif t == 7:             pos += 8               # double
    elif t == 8:             pos += varint()        # binary
    elif t in (9, 10):
        n, et = list_hdr()
        for _ in range(n): skip(et)
    elif t == 12:
        last = 0
        while True:
            ft, _fid, last = field(last)
            if ft == 0: break
            skip(ft)

def field(last):
    global pos
    b = raw[pos]; pos += 1
    if b == 0:
        return 0, 0, last
    t = b & 0x0F
    delta = b >> 4
    fid = last + delta if delta else zigzag()
    return t, fid, fid

# FileMetaData -> field 4 (row_groups) -> first RowGroup -> field 1 (columns)
target = None
last = 0
while True:
    ft, fid, last = field(last)
    if ft == 0:
        break
    if fid == 4 and ft == 9:                 # row_groups: list<RowGroup>
        nrg, _et = list_hdr()
        assert nrg >= 1, nrg
        rlast = 0
        while True:
            rft, rfid, rlast = field(rlast)
            if rft == 0:
                break
            if rfid == 1 and rft == 9:       # columns: list<ColumnChunk>
                target = pos                 # byte holding this list's header
                n, et = list_hdr()
                assert n == 2, f"expected 2 column chunks, got {n}"
                skip(et)                     # past ColumnChunk[0]
                start2 = pos
                skip(et)                     # past ColumnChunk[1]
                end2 = pos
                break
            skip(rft)
        break
    skip(ft)

assert target is not None, "row group columns list not found"
# Remove ColumnChunk[1] outright and say so in the header, so the footer stays a
# well-formed Thrift struct. Decrementing the count alone would desync the parser
# and fail for an unrelated reason; the only defect must be that the row group
# carries fewer chunks than the schema has leaves.
assert (raw[target] >> 4) == 2, hex(raw[target])
raw[target] = (1 << 4) | (raw[target] & 0x0F)
removed = end2 - start2
del raw[start2:end2]
raw[-8:-4] = (flen - removed).to_bytes(4, "little")
open(f"{W}/short_chunks.parquet", "wb").write(bytes(raw))
print(f"  columns list at {target}: 2 chunks -> 1, removed {removed} footer bytes",
      file=sys.stderr)
