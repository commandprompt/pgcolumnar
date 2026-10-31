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
