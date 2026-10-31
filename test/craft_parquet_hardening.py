"""craft_parquet_hardening.py DIR [WAREHOUSE]: write the Parquet files
test/native_parquet_hardening.sh and its pytest twin read: the narrowing binds (out of
range and in range), a two-column file whose row group carries one column chunk, and
string cells that are not valid UTF-8 (#1821). Given the committed Iceberg warehouse
(test/fixtures/iceberg/warehouse), it also writes two copies of its events table
under DIR: one whose eu data file holds an invalid string, and one whose manifest
records the eu partition value as invalid bytes. One writer for both twins, so
neither can drift from the other. Needs pyarrow."""
import os
import shutil
import sys
import zlib

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

# ---- string cells that are not valid UTF-8 (#1821) -------------------------
# A Parquet STRING is UTF-8 by the spec, but nothing stops a writer putting other
# bytes there. pyarrow validates a string array built from Python values, so the
# bad cells are assembled from raw buffers: offsets plus the concatenated bytes.
def raw_strings(vals):
    offs = [0]
    for v in vals:
        offs.append(offs[-1] + len(v))
    return pa.Array.from_buffers(pa.string(), len(vals),
                                 [None, pa.array(offs, pa.int32()).buffers()[1],
                                  pa.py_buffer(b"".join(vals))])


def write_strings(name, vals, dictionary):
    path = f"{W}/{name}.parquet"
    pq.write_table(pa.table({"id": pa.array(range(1, len(vals) + 1), pa.int64()),
                             "name": raw_strings(vals)}),
                   path, use_dictionary=dictionary)
    # the PLAIN and dictionary files must really be what their names say: the two
    # reach the decoder from different call sites (a data page vs a dictionary page)
    col = pq.ParquetFile(path).metadata.row_group(0).column(1)
    assert col.has_dictionary_page == dictionary, (name, col.encodings)


# 0xff can never start a UTF-8 sequence
write_strings("str_plain_bad", [b"ok", b"\xff\xfe\xc3"], False)
# an overlong encoding of '/': 0xc0 0xaf, after a valid 'x'; repeated values make
# the dictionary worth writing, and the bad value lives only in the dictionary page
write_strings("str_dict_bad", [b"ok", b"ok", b"x\xc0\xafy", b"ok"], True)
# an embedded NUL would read as the shorter "ab"
write_strings("str_nul", [b"ok", b"ab\x00cd"], False)
# valid multibyte UTF-8, in both encodings: must still read
write_strings("str_ok", ["ok".encode(), "caf\u00e9".encode(), "\u65e5\u672c".encode()], False)
write_strings("str_ok_dict", ["caf\u00e9".encode()] * 3 + ["ok".encode()], True)

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


# ---- Iceberg: an invalid string in a data file, and in a partition value ----
# Both copies start from the committed pyiceberg table (identity-partitioned by
# region: eu holds ids 1-2, us ids 3-5). iceberg_scan rebases a relocated table.
if len(sys.argv) > 2:
    src = os.path.join(sys.argv[2], "db")

    def copy_table(name):
        dst = os.path.join(W, name, "db")
        shutil.rmtree(os.path.join(W, name), ignore_errors=True)
        shutil.copytree(src, dst)
        return os.path.join(dst, "events")

    def field(name, typ, fid):
        return pa.field(name, typ, metadata={b"PARQUET:field_id": str(fid).encode()})

    # (1) the eu data file rewritten with the same field ids (id=1, region=2,
    # amount=3) and one region cell that is not UTF-8
    t = copy_table("ice_bad")
    eu = [f for f in os.listdir(os.path.join(t, "data", "region=eu"))
          if f.endswith(".parquet")]
    assert len(eu) == 1, eu
    pq.write_table(
        pa.Table.from_arrays(
            [pa.array([1, 2], pa.int64()), raw_strings([b"\xff\xfe", b"eu"]),
             pa.array([10, 20], pa.int32())],
            schema=pa.schema([field("id", pa.int64(), 1), field("region", pa.string(), 2),
                              field("amount", pa.int32(), 3)])),
        os.path.join(t, "data", "region=eu", eu[0]))

    # (2) the manifest's eu partition value "eu" replaced by the two bytes 0xff 0xfe,
    # with the data files untouched. Avro writes a string as a zigzag length (2 -> 0x04)
    # then its bytes, so the same-length swap keeps every record well formed. The
    # manifest is deflate-compressed, so each block is inflated, patched, and
    # deflated again with its byte count rewritten.
    t = copy_table("ice_part")
    md = os.path.join(t, "metadata")
    mf = [f for f in os.listdir(md) if f.endswith("-m0.avro")]
    assert len(mf) == 1, mf
    raw = open(os.path.join(md, mf[0]), "rb").read()
    p = 0

    def rlong():
        global p
        shift = out = 0
        while True:
            b = raw[p]
            p += 1
            out |= (b & 0x7F) << shift
            if not b & 0x80:
                return (out >> 1) ^ -(out & 1)
            shift += 7

    def wlong(n):
        n = (n << 1) ^ (n >> 63)
        out = bytearray()
        while True:
            if n < 0x80:
                out.append(n)
                return bytes(out)
            out.append((n & 0x7F) | 0x80)
            n >>= 7

    assert raw[:4] == b"Obj\x01", raw[:4]
    p = 4
    codec = None
    while True:                                  # the header's metadata map
        n = rlong()
        if n == 0:
            break
        if n < 0:
            n = -n
            rlong()                              # the block's byte size
        for _ in range(n):
            ln = rlong()
            k = raw[p:p + ln]
            p += ln
            ln = rlong()
            v = raw[p:p + ln]
            p += ln
            if k == b"avro.codec":
                codec = v
    assert codec == b"deflate", codec
    sync = raw[p:p + 16]
    p += 16
    out = bytearray(raw[:p])
    swapped = 0
    while p < len(raw):
        cnt = rlong()
        size = rlong()
        data = zlib.decompress(raw[p:p + size], -15)
        p += size
        assert raw[p:p + 16] == sync
        p += 16
        swapped += data.count(b"\x04eu")
        data = data.replace(b"\x04eu", b"\x04\xff\xfe")
        c = zlib.compressobj(6, zlib.DEFLATED, -15)
        z = c.compress(data) + c.flush()
        out += wlong(cnt) + wlong(len(z)) + z + sync
    # at least the eu file's partition value (its column bounds for region may be
    # rewritten too; nothing prunes on a string bound)
    assert swapped >= 1, swapped
    open(os.path.join(md, mf[0]), "wb").write(bytes(out))
    for d, _dirs, files in os.walk(os.path.join(W, "ice_bad")):
        os.chmod(d, 0o755)
        for f in files:
            os.chmod(os.path.join(d, f), 0o644)
    for d, _dirs, files in os.walk(os.path.join(W, "ice_part")):
        os.chmod(d, 0o755)
        for f in files:
            os.chmod(os.path.join(d, f), 0o644)
    print(f"  ice_part: {swapped} eu cell(s) -> 0xff 0xfe", file=sys.stderr)
