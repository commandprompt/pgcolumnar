"""craft_parquet_hardening.py DIR [WAREHOUSE]: write the Parquet files
test/native_parquet_hardening.sh and its pytest twin read: the narrowing binds (out of
range and in range), the unsigned integer files (#1814), a two-column file whose row
group carries one column chunk, string cells that are not valid UTF-8 (#1821), and
footers whose Thrift is malformed (#1822). Given the committed Iceberg warehouse (test/fixtures/iceberg/warehouse), it also
writes two copies of its events table under DIR: one whose eu data file holds an
invalid string, and one whose manifest records the eu partition value as invalid
bytes. It also writes the footers that do not match their pages (#1855, #1852) and
the INT96 values that are not timestamps (#1870), the pages whose decompressed
length is not their header's (#1910) and a group with a negative num_children
(#1921). One writer for both twins, so
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

# ---- unsigned integers (#1814) ------------------------------------------------
# pyarrow writes uint32 as INT32 and uint64 as INT64, annotated UINT_32/UINT_64 and
# INT(bitWidth, isSigned=false). The upper half of each range has the sign bit set,
# so a signed decode reads 2^31 as -2147483648 and 2^64-1 as -1.
pq.write_table(pa.table({"u8": pa.array([1, 200, 255], pa.uint8()),
                         "u16": pa.array([1, 40000, 65535], pa.uint16()),
                         "u32": pa.array([1, 2**31, 2**32 - 1], pa.uint32()),
                         "u64": pa.array([1, 2**63, 2**64 - 1], pa.uint64())}),
               f"{W}/unsigned.parquet")
pq.write_table(pa.table({"c": pa.array([1, 2**31, 2**32 - 1], pa.uint32())}),
               f"{W}/u32.parquet")
pq.write_table(pa.table({"c": pa.array([1, 2**63, 2**64 - 1], pa.uint64())}),
               f"{W}/u64.parquet")
# 2^32-1 alone: its signed reading is -1, which fits smallint, so only an unsigned
# decode can see that it does not
pq.write_table(pa.table({"c": pa.array([1, 2**32 - 1], pa.uint32())}),
               f"{W}/u32_max.parquet")

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


# ---- malformed footer Thrift (#1822) ---------------------------------------
# A Thrift compact field header carries its type in the low nibble. Types 14 and
# 15 are not compact types. Type 13 is UUID, a 16-byte value that parquet.thrift
# does not use. Type 11 (map) and a list of bools are valid but were skipped as
# zero bytes. Either way the parser lost its place in the footer and
# could return a wrong row count with no error. These files are built from one
# good file by decoding its footer into a tree and encoding it again, so they do
# not depend on how a pyarrow version lays the footer out.
GOOD = f"{W}/footer_good.parquet"
pq.write_table(pa.table({"id": pa.array(range(3000), pa.int64()),
                         "name": [f"r{i}" for i in range(3000)]}),
               GOOD, row_group_size=1000)      # 3 row groups of 1000 rows
good = open(GOOD, "rb").read()
gflen = int.from_bytes(good[-8:-4], "little")
gbase = len(good) - 8 - gflen
gfoot = good[gbase:gbase + gflen]


class Dec:
    """Decode a compact struct to [(fid, type, value, header_offset)]."""

    def __init__(self, buf):
        self.b, self.p = buf, 0

    def varint(self):
        out = shift = 0
        while True:
            c = self.b[self.p]; self.p += 1
            out |= (c & 0x7F) << shift
            if not c & 0x80:
                return out
            shift += 7

    def zz(self):
        u = self.varint()
        return (u >> 1) ^ -(u & 1)

    def value(self, t, in_list=False):
        if t in (1, 2):
            if in_list:                       # a bool in a list takes one byte
                c = self.b[self.p]; self.p += 1
                return c
            return None                       # a bool field: value is in the header
        if t == 3:
            c = self.b[self.p]; self.p += 1
            return c
        if t in (4, 5, 6):
            return self.zz()
        if t == 7:
            v = self.b[self.p:self.p + 8]; self.p += 8
            return v
        if t == 8:
            n = self.varint()
            v = self.b[self.p:self.p + n]; self.p += n
            return v
        if t in (9, 10):
            c = self.b[self.p]; self.p += 1
            n, et = (c >> 4) & 0x0F, c & 0x0F
            if n == 0x0F:
                n = self.varint()
            return (et, [self.value(et, True) for _ in range(n)])
        if t == 12:
            return self.struct()
        raise ValueError(f"type {t}")

    def struct(self):
        fields, last = [], 0
        while True:
            hdr = self.p
            c = self.b[self.p]; self.p += 1
            if c == 0:
                return fields
            t, d = c & 0x0F, c >> 4
            fid = last + d if d else self.zz()
            last = fid
            fields.append([fid, t, self.value(t), gbase + hdr])


def enc_varint(n):
    out = bytearray()
    while True:
        if n < 0x80:
            out.append(n)
            return bytes(out)
        out.append((n & 0x7F) | 0x80)
        n >>= 7


def enc_zz(n):
    return enc_varint((n << 1) ^ (n >> 63))


def enc_value(t, v, in_list=False):
    if t == 13:                               # UUID: 16 raw bytes
        assert len(v) == 16, len(v)
        return bytes(v)
    if t in (0, 14, 15) and in_list:          # not a compact type: raw bytes, for crafting
        return bytes(v)
    if t in (1, 2):
        return bytes([v]) if in_list else b""
    if t == 3:
        return bytes([v])
    if t in (4, 5, 6):
        return enc_zz(v)
    if t in (7, 8):
        return bytes(v) if t == 7 else enc_varint(len(v)) + bytes(v)
    if t in (9, 10):
        et, items = v
        hdr = bytes([(len(items) << 4) | et]) if len(items) < 15 else bytes([0xF0 | et]) + enc_varint(len(items))
        return hdr + b"".join(enc_value(et, x, True) for x in items)
    if t == 11:                               # map: (ktype, vtype, [(k, v)])
        kt, vt, items = v
        if not items:
            return b"\x00"
        return (enc_varint(len(items)) + bytes([(kt << 4) | vt])
                + b"".join(enc_value(kt, k, True) + enc_value(vt, x, True) for k, x in items))
    if t == 12:
        return enc_struct(v)
    raise ValueError(f"type {t}")


def enc_struct(fields):
    out, last = bytearray(), 0
    for f in fields:
        fid, t, v = f[0], f[1], f[2]
        if 0 < fid - last <= 15:
            out.append(((fid - last) << 4) | t)
        else:
            out.append(t)
            out += enc_zz(fid)
        last = fid
        out += enc_value(t, v)
    out.append(0)
    return bytes(out)


def fld(fields, fid):
    return next(f for f in fields if f[0] == fid)


def write_footer(name, foot):
    open(f"{W}/{name}.parquet", "wb").write(good[:gbase] + foot + len(foot).to_bytes(4, "little") + b"PAR1")


def write_mutant(name, offset, nibble=0x0F):
    b = bytearray(good)
    b[offset] = (b[offset] & 0xF0) | nibble  # 15 by default: not a compact type
    open(f"{W}/{name}.parquet", "wb").write(bytes(b))


fmd = Dec(gfoot).struct()
rgs = fld(fmd, 4)[2][1]
assert len(rgs) == 3, len(rgs)

# the two named mutants: the row_groups header, and row group 3's num_rows header
write_mutant("nibble_row_groups", fld(fmd, 4)[3])
write_mutant("nibble_num_rows", fld(rgs[2], 3)[3])


def headers(fields):
    """Every field header offset in the footer, nested ones included."""
    for f in fields:
        yield f[3]
        if f[1] == 12:
            yield from headers(f[2])
        elif f[1] in (9, 10) and f[2][0] == 12:
            for s in f[2][1]:
                yield from headers(s)


offs = sorted(headers(fmd))
for i, off in enumerate(offs):
    write_mutant(f"nibble_sweep_{i:04d}", off)
open(f"{W}/nibble_sweep.count", "w").write(f"{len(offs)}\n")
# The same headers set to type 13 (UUID), which the skip steps over as 16 bytes.
# A copy may then read, but only with the right answer.
for i, off in enumerate(offs):
    write_mutant(f"uuid_sweep_{i:04d}", off, 0x0D)


def strip(fields):
    return [[f[0], f[1], strip(f[2]) if f[1] == 12 else
             (f[2][0], [strip(s) for s in f[2][1]]) if f[1] in (9, 10) and f[2][0] == 12 else f[2]]
            for f in fields]


def variant(name, edit):
    m = strip(fmd)
    edit(m)
    write_footer(name, enc_struct(m))


variant("footer_reencoded", lambda m: None)
variant("no_row_groups", lambda m: m.remove(fld(m, 4)))
variant("no_rg_num_rows", lambda m: fld(m, 4)[2][1][2].remove(fld(fld(m, 4)[2][1][2], 3)))
variant("no_file_num_rows", lambda m: m.remove(fld(m, 3)))
variant("file_num_rows_2999", lambda m: fld(m, 3).__setitem__(2, 2999))
# The direction that loses rows: row group 3 says 999 rows, the footer still 3000.
# num_rows below a column's value count is legal (a repeated column), so only the
# total check refuses it. pyarrow reads it as 3000 rows.
variant("rg3_999", lambda m: fld(fld(m, 4)[2][1][2], 3).__setitem__(2, 999))
variant("file_num_rows_3001", lambda m: fld(m, 3).__setitem__(2, 3001))
# an unknown field of each skipped-wrong shape, placed before row_groups so a
# skip that consumes too few bytes loses row_groups
variant("unknown_map_field", lambda m: m.insert(m.index(fld(m, 4)),
                                                [100, 11, (8, 5, [(b"a", 1), (b"b", 2)])]))
variant("unknown_bool_list_field", lambda m: m.insert(m.index(fld(m, 4)),
                                                      [101, 9, (1, [1, 2, 1])]))
# A UUID field and a list of UUIDs: valid compact values, 16 bytes each, that a
# future parquet.thrift could add. A skip of the wrong size loses row_groups or
# reads 0xff as a field header of type 15.
variant("unknown_uuid_field", lambda m: m.insert(m.index(fld(m, 4)),
                                                 [102, 13, bytes(range(16))]))
variant("unknown_uuid_list_field", lambda m: m.insert(m.index(fld(m, 4)),
                                                      [103, 9, (13, [b"\xff" * 16, bytes(range(16))])]))
# A list whose element type, 14, is not a compact type. The size of its elements
# is unknown. Skipped as zero bytes, the list vanished and the file read.
variant("list_of_type_14", lambda m: m.insert(m.index(fld(m, 4)),
                                              [104, 9, (14, [b"", b"", b""])]))


# A list of 4294967295 bytes or doubles as the footer's last field, with the
# footer ending a few bytes in. A skip that steps past the footer's end without
# an error runs all 4294967295 elements before anything notices.
def off_end(name, etype, tail):
    m = strip(fmd)
    raw = bytes([0x09]) + enc_zz(105) + bytes([0xF0 | etype]) + enc_varint(0xFFFFFFFF) + tail
    write_footer(name, enc_struct(m)[:-1] + raw)


off_end("byte_list_off_end", 3, b"")
off_end("double_list_off_end", 7, b"\x00\x00\x00")

# A list of 4294967295 elements of type 15 as the footer's last field. Type 15 is
# not a compact type, so the skip must refuse the first element. Skipped as zero
# bytes, each element passes and the loop runs 4294967295 times.
m = strip(fmd)
write_footer("list_elem15_huge", enc_struct(m)[:-1] + bytes([0x09]) + enc_zz(106)
             + bytes([0xFF]) + enc_varint(0xFFFFFFFF) + b"\x00")

# The row_groups header with its type nibble set to 0 but its field-id delta kept.
# Only the byte 0x00 is STOP; a type-0 nibble with a delta is corrupt. Read as STOP,
# it ended the footer early, and only the required-field checks caught it.
assert good[fld(fmd, 4)[3]] >> 4 != 0, hex(good[fld(fmd, 4)[3]])
write_mutant("nibble0_row_groups", fld(fmd, 4)[3], 0x00)
print(f"  footer {gflen} bytes, {len(offs)} field headers swept", file=sys.stderr)


# ---- one spelling of the unsigned mark at a time (#1814) ------------------------
# pyarrow marks an unsigned column twice: ConvertedType UINT_32 (SchemaElement field
# 6) and LogicalType INTEGER(32, isSigned=false) (field 10). Writers may emit either
# alone, and the reader reads both, so each file below keeps exactly one: without
# them, the reader could ignore either spelling and every test would still pass.
# The schema element is re-encoded with the other field left out, which keeps the
# footer well-formed Thrift.
def schema_c(buf):
    """-> (start, end, fields) of the footer's SchemaElement for column "c", with
    fields a list of (compact type, field id, value bytes)."""
    flen = int.from_bytes(buf[-8:-4], "little")
    st = {"pos": len(buf) - 8 - flen}

    def byte():
        b = buf[st["pos"]]
        st["pos"] += 1
        return b

    def uvar():
        shift = out = 0
        while True:
            b = byte()
            out |= (b & 0x7F) << shift
            if not b & 0x80:
                return out
            shift += 7

    def zz():
        u = uvar()
        return (u >> 1) ^ -(u & 1)

    def lhdr():
        b = byte()
        n = (b >> 4) & 0x0F
        if n == 0x0F:
            n = uvar()
        return n, b & 0x0F

    def fhdr(last):
        b = byte()
        if b == 0:
            return 0, 0
        t, d = b & 0x0F, b >> 4
        return t, (last + d if d else zz())

    def skip(t):
        if t in (1, 2):
            return
        if t == 3:
            st["pos"] += 1
        elif t in (4, 5, 6):
            zz()
        elif t == 7:
            st["pos"] += 8
        elif t == 8:
            n = uvar()                       # before the +=, which reads pos first
            st["pos"] += n
        elif t in (9, 10):
            n, et = lhdr()
            for _ in range(n):
                skip(et)
        elif t == 12:
            last = 0
            while True:
                ft, last2 = fhdr(last)
                if ft == 0:
                    break
                last = last2
                skip(ft)

    # FileMetaData field 2 is the schema: list<SchemaElement>; [1] is column "c"
    last = 0
    while True:
        ft, fid = fhdr(last)
        assert ft != 0, "no schema list"
        last = fid
        if fid == 2 and ft == 9:
            n, et = lhdr()
            assert n == 2 and et == 12, (n, et)
            skip(12)                         # the root element
            start = st["pos"]
            fields, flast = [], 0
            while True:
                t, f = fhdr(flast)
                if t == 0:
                    break
                flast = f
                vstart = st["pos"]
                skip(t)
                fields.append((t, f, bytes(buf[vstart:st["pos"]])))
            return start, st["pos"], fields
        skip(ft)


def enc_hdr(t, fid, last):
    """-> a compact-protocol field header for field `fid` of type `t` after `last`."""
    d = fid - last
    if 0 < d <= 15:
        return bytes([(d << 4) | t])
    z = (fid << 1) ^ (fid >> 15)
    out = bytearray([t])
    while True:
        if z < 0x80:
            out.append(z)
            return bytes(out)
        out.append((z & 0x7F) | 0x80)
        z >>= 7


def one_mark(src, dst, drop_fid):
    buf = bytearray(open(src, "rb").read())
    flen = int.from_bytes(buf[-8:-4], "little")
    start, end, fields = schema_c(buf)
    assert {6, 10} <= {f for _t, f, _v in fields}, [f for _t, f, _v in fields]
    out, olast = bytearray(), 0
    for t, f, v in fields:
        if f == drop_fid:
            continue
        out += enc_hdr(t, f, olast) + v
        olast = f
    out.append(0)
    buf[start:end] = out
    buf[-8:-4] = (flen + len(out) - (end - start)).to_bytes(4, "little")
    # the rewritten footer parses, and holds the one mark it should
    kept = {f for _t, f, _v in schema_c(buf)[2]}
    assert drop_fid not in kept and ({6, 10} - {drop_fid}) <= kept, kept
    open(dst, "wb").write(bytes(buf))


one_mark(f"{W}/u32.parquet", f"{W}/u32_logical_only.parquet", 6)
one_mark(f"{W}/u32.parquet", f"{W}/u32_converted_only.parquet", 10)


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


# ---- a schema repetition_type that does not match the pages (#1855) -------------
# pyarrow writes `id` OPTIONAL (repetition_type 1, stored as the zigzag byte 0x02),
# so every data page carries definition levels. rep_required.parquet says REQUIRED
# instead, and rep_invalid.parquet says 7, which is no repetition_type at all. The
# reader used to decode the definition levels as values and return a wrong sum with
# no error. Three row groups of 1000 rows, as in the issue.
def repetition_byte(buf, name):
    """-> the offset of the one-byte repetition_type value of schema element `name`."""
    flen = int.from_bytes(buf[-8:-4], "little")
    st = {"pos": len(buf) - 8 - flen}

    def byte():
        b = buf[st["pos"]]
        st["pos"] += 1
        return b

    def uvar():
        shift = out = 0
        while True:
            b = byte()
            out |= (b & 0x7F) << shift
            if not b & 0x80:
                return out
            shift += 7

    def lhdr():
        b = byte()
        n = (b >> 4) & 0x0F
        if n == 0x0F:
            n = uvar()
        return n, b & 0x0F

    def fhdr(last):
        b = byte()
        if b == 0:
            return 0, 0
        t, d = b & 0x0F, b >> 4
        if d:
            return t, last + d
        u = uvar()
        return t, (u >> 1) ^ -(u & 1)

    def skip(t):
        if t in (1, 2):
            return
        if t == 3:
            st["pos"] += 1
        elif t in (4, 5, 6):
            uvar()
        elif t == 7:
            st["pos"] += 8
        elif t == 8:
            n = uvar()
            st["pos"] += n
        elif t in (9, 10):
            n, et = lhdr()
            for _ in range(n):
                skip(et)
        elif t == 12:
            last = 0
            while True:
                ft, f = fhdr(last)
                if ft == 0:
                    break
                last = f
                skip(ft)

    last = 0
    while True:
        ft, fid = fhdr(last)
        assert ft != 0, "no schema list"
        last = fid
        if fid == 2 and ft == 9:
            n, et = lhdr()
            assert et == 12, et
            for _ in range(n):
                rep_at, el_name, flast = None, None, 0
                while True:
                    t, f = fhdr(flast)
                    if t == 0:
                        break
                    flast = f
                    if f == 3:
                        rep_at = st["pos"]
                    if f == 4:
                        ln = uvar()
                        el_name = bytes(buf[st["pos"]:st["pos"] + ln]).decode()
                        st["pos"] += ln
                        continue
                    skip(t)
                if el_name == name:
                    assert rep_at is not None, name
                    return rep_at
            raise AssertionError(f"no schema element {name}")
        skip(ft)


pq.write_table(pa.table({"id": pa.array(range(3000), pa.int64()),
                         "name": pa.array([f"n{i}" for i in range(3000)])}),
               f"{W}/rep_base.parquet", row_group_size=1000)
_rep = bytearray(open(f"{W}/rep_base.parquet", "rb").read())
_at = repetition_byte(_rep, "id")
assert _rep[_at] == 0x02, hex(_rep[_at])         # zigzag 1: OPTIONAL
for _name, _byte in (("rep_required", 0x00),      # zigzag 0: REQUIRED
                     ("rep_invalid", 0x0E)):      # zigzag 7: not a repetition_type
    _out = bytearray(_rep)
    _out[_at] = _byte
    open(f"{W}/{_name}.parquet", "wb").write(bytes(_out))
print(f"  id repetition_type at footer byte "
      f"{_at - (len(_rep) - 8 - int.from_bytes(_rep[-8:-4], 'little'))}", file=sys.stderr)


# ---- a repetition_type that does not match a repetition histogram (#1855) -------
# pyarrow writes a list<int64> column as l (OPTIONAL group) > list (REPEATED group) >
# element (OPTIONAL int64), so element's pages carry repetition levels 0 and 1, and
# its chunk carries a repetition level histogram of 2 entries. lst_optional.parquet
# says the `list` group is OPTIONAL (zigzag 1, 0x02) instead of REPEATED (zigzag 2,
# 0x04), so the schema gives element a maximum repetition level of 0. The definition
# levels still fit (3 either way), so only the repetition histogram can tell.
pq.write_table(pa.table({"l": pa.array([[1, 2], [3], [], None, [4, 5, 6]], pa.list_(pa.int64()))}),
               f"{W}/lst_base.parquet")
_lst = bytearray(open(f"{W}/lst_base.parquet", "rb").read())
_at = repetition_byte(_lst, "list")
assert _lst[_at] == 0x04, hex(_lst[_at])         # zigzag 2: REPEATED
_lst[_at] = 0x02                                  # zigzag 1: OPTIONAL
open(f"{W}/lst_optional.parquet", "wb").write(bytes(_lst))


# ---- INT96 values the decode must refuse (#1870) ---------------------------------
# A REQUIRED INT96 column, PLAIN and uncompressed, so each value's 12 bytes (8 of
# nanoseconds in the day, then 4 of Julian day, little-endian) sit in the file as
# written and can be patched in place. The second value is the one patched:
#   i96_nanos_day.parquet   nanoseconds 86400e9, one whole day: no valid value has it
#   i96_nanos_neg.parquet   nanoseconds -1
#   i96_jday_neg.parquet    Julian day -1, before timestamp's first day
# i96_short.parquet keeps the values and the page's num_values of 3, but says the
# page is 24 bytes long instead of 36. The third value's 12 bytes still follow in
# the file, so a decode that read past the page's end would return a whole value.
_I96_VALS = [1784851200_000_000_000, 1784851200_123_456_789, 1784851200_500_000_000]
pq.write_table(pa.table([pa.array(_I96_VALS, pa.timestamp("ns", tz="UTC"))],
                        schema=pa.schema([pa.field("t", pa.timestamp("ns", tz="UTC"), nullable=False)])),
               f"{W}/i96_req.parquet", use_deprecated_int96_timestamps=True,
               use_dictionary=False, compression="NONE", write_statistics=False)
_i96 = bytes(open(f"{W}/i96_req.parquet", "rb").read())
_day, _ns = divmod(_I96_VALS[1], 86400 * 10**9)
_v1 = _ns.to_bytes(8, "little") + (2440588 + _day).to_bytes(4, "little")
assert _i96.count(_v1) == 1, _i96.count(_v1)
_v1_at = _i96.index(_v1)
for _name, _new in (("i96_nanos_day", (86400 * 10**9).to_bytes(8, "little") + _v1[8:]),
                    ("i96_nanos_neg", (-1).to_bytes(8, "little", signed=True) + _v1[8:]),
                    ("i96_jday_neg", _v1[:8] + (-1).to_bytes(4, "little", signed=True))):
    open(f"{W}/{_name}.parquet", "wb").write(_i96[:_v1_at] + _new + _i96[_v1_at + 12:])
# The data page header begins at byte 4: type (field 1, i32: header 0x15) DATA_PAGE
# 0x00, then uncompressed_page_size and compressed_page_size (fields 2 and 3, i32),
# each the zigzag varint 0x48 for 36. 0x30 says 24. Both stay one byte long.
assert _i96[4:10] == b"\x15\x00\x15\x48\x15\x48", _i96[4:10].hex()
open(f"{W}/i96_short.parquet", "wb").write(_i96[:4] + b"\x15\x00\x15\x30\x15\x30" + _i96[10:])


# ---- a schema element with no name, and a repetition_type wider than int (#1882) --
# Both are footer_good.parquet with one edit to the `id` schema element (element 1).
# no_schema_name.parquet drops its name (field 4), a required field of SchemaElement.
# rep_wide_nohist.parquet sets its repetition_type (field 3) to 2^32, which a cast to
# int truncates to 0 (REQUIRED). It also drops every chunk's size statistics (column
# meta_data field 16), so no level histogram can catch the mismatch: the pages still
# carry definition levels. rep_nohist.parquet drops only the size statistics, so the
# suite can show that the file reads without them.
def _drop_size_stats(m):
    for rg in fld(m, 4)[2][1]:
        for ch in fld(rg, 1)[2][1]:
            md = fld(ch, 3)[2]
            md[:] = [f for f in md if f[0] != 16]


def _rep_wide(m):
    _drop_size_stats(m)
    el = fld(m, 2)[2][1][1]
    assert fld(el, 4)[2] == b"id", fld(el, 4)[2]
    fld(el, 3)[2] = 2**32


variant("no_schema_name", lambda m: fld(m, 2)[2][1][1].remove(fld(fld(m, 2)[2][1][1], 4)))
variant("rep_nohist", _drop_size_stats)
variant("rep_wide_nohist", _rep_wide)


# ---- a page whose decompressed length is not its header's (#1910) ----------------
# 100 nullable INT64 values, PLAIN, one data page each, no statistics. The page
# header begins at byte 4, and its uncompressed_page_size (field 2) is a two-byte
# zigzag varint. Each copy changes only that varint, to a value of the same width:
#   page_snappy_v1_long.parquet   SNAPPY v1 page: 807 bytes decompress, header says 808
#   page_snappy_v1_short.parquet  the same page: header says 806
#   page_snappy_v2_long.parquet   SNAPPY v2 page: 3 level bytes + 800 values; header
#                                 says 804, so 801 value bytes are due and 800 come out
#   page_none_v1_long.parquet     UNCOMPRESSED v1 page: header says 808 of 807 bytes.
#                                 pyarrow reads it (the bytes are there, the codec
#                                 copies nothing), so it is the control that reads.
# pyarrow refuses every SNAPPY copy ("Page didn't decompress to expected size").
_PAGE_T = pa.table({"id": pa.array(range(1, 101), pa.int64())})
for _name, _kw, _old, _new in (
        ("snappy_v1_long", dict(compression="SNAPPY"), b"\x15\xce\x0c\x15\xea", b"\x15\xd0\x0c\x15\xea"),
        ("snappy_v1_short", dict(compression="SNAPPY"), b"\x15\xce\x0c\x15\xea", b"\x15\xcc\x0c\x15\xea"),
        ("snappy_v2_long", dict(compression="SNAPPY", data_page_version="2.0"),
         b"\x15\xc6\x0c\x15\xc4", b"\x15\xc8\x0c\x15\xc4"),
        ("none_v1_long", dict(compression="NONE"), b"\x15\xce\x0c\x15\xce", b"\x15\xd0\x0c\x15\xce")):
    _base = f"{W}/page_{_name.rsplit('_', 1)[0]}.parquet"
    pq.write_table(_PAGE_T, _base, use_dictionary=False, write_statistics=False, **_kw)
    _pg = open(_base, "rb").read()
    assert _pg[6:11] == _old, (_name, _pg[4:16].hex())
    open(f"{W}/page_{_name}.parquet", "wb").write(_pg[:6] + _new + _pg[11:])


# ---- a group whose num_children is negative (#1921) ------------------------------
# a bigint, s struct<x bigint>, b bigint. grp_neg_children.parquet sets s's
# num_children (field 5 of schema element 2) to -1. The schema walk took it as a
# group with no children: x then read as the root's third child, b was never
# reached, and parquet_schema stopped at s and returned one row with no error.
pq.write_table(pa.table({"a": pa.array([1, 2, 3], pa.int64()),
                         "s": pa.array([{"x": 4}, {"x": 5}, {"x": 6}],
                                       pa.struct([("x", pa.int64())])),
                         "b": pa.array([7, 8, 9], pa.int64())}),
               f"{W}/grp_base.parquet")
_grp = open(f"{W}/grp_base.parquet", "rb").read()
_glen = int.from_bytes(_grp[-8:-4], "little")
_gstart = len(_grp) - 8 - _glen
_gm = strip(Dec(_grp[_gstart:_gstart + _glen]).struct())
_gs = fld(_gm, 2)[2][1][2]
assert fld(_gs, 4)[2] == b"s" and fld(_gs, 5)[2] == 1, _gs
fld(_gs, 5)[2] = -1
_gfoot = enc_struct(_gm)
open(f"{W}/grp_neg_children.parquet", "wb").write(
    _grp[:_gstart] + _gfoot + len(_gfoot).to_bytes(4, "little") + b"PAR1")
