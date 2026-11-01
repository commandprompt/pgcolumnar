"""craft_parquet_nested.py DIR: write the nested Parquet files that
test/native_parquet_schema.sh and its pytest twin read (#1893). One writer for
both twins, so neither can drift from the other. Needs pyarrow.

Each file holds one nested column and an int32 column n, two rows:

  map.parquet    m map<string, int64>
  los.parquet    l list<struct<x int64, y string>>
  lofs.parquet   l list<struct<x int64>>          (one leaf, at depth 4)
  shl.parquet    s struct<x list<int64>>           (one leaf, at depth 4)
  sos.parquet    s struct<t struct<a int64, b string>>
  smap.parquet   s struct<m map<string, int64>>
  lmap.parquet   l list<map<string, int64>>
  lol.parquet    l list<list<int64>>               (max_rep 2)
  twolevel.parquet  l as a top-level `repeated int64`, the legacy two-level
                 list. pyarrow cannot write that shape, so it is a REQUIRED
                 list<REQUIRED int64> with its footer schema rewritten: the two
                 groups go and the leaf becomes `repeated int64 l`. Both shapes
                 have max_def 1 and max_rep 1, so the pages stay valid.

  qname.parquet  s struct<"Weird Name" int16>      (a field name that needs quotes)

req.parquet has no n: l REQUIRED list<REQUIRED int64>, s REQUIRED
struct<a REQUIRED int64>, o OPTIONAL list<REQUIRED int64>.

leafnull.parquet has no n: m REQUIRED map<string, OPTIONAL int64>, l REQUIRED
list<OPTIONAL struct<x OPTIONAL int64>>, o OPTIONAL map<string, OPTIONAL int64>.
Only o has an OPTIONAL element above its first repeated level.
"""
import os
import sys

import pyarrow as pa
import pyarrow.parquet as pq

W = sys.argv[1]
I64, STR = pa.int64(), pa.string()
MAP = pa.map_(STR, I64)
N = pa.array([1, 2], pa.int32())


def write(name, col, typ, rows):
    pq.write_table(pa.table({col: pa.array(rows, typ), "n": N}), f"{W}/{name}.parquet")


write("map", "m", MAP, [[("k", 1), ("j", 2)], [("z", 3)]])
write("los", "l", pa.list_(pa.struct([("x", I64), ("y", STR)])),
      [[{"x": 1, "y": "a"}], [{"x": 2, "y": "b"}, {"x": 3, "y": "c"}]])
write("lofs", "l", pa.list_(pa.struct([("x", I64)])), [[{"x": 1}, {"x": 2}], [{"x": 3}]])
write("shl", "s", pa.struct([("x", pa.list_(I64))]), [{"x": [1, 2]}, {"x": [3]}])
write("sos", "s", pa.struct([("t", pa.struct([("a", I64), ("b", STR)]))]),
      [{"t": {"a": 1, "b": "x"}}, {"t": {"a": 2, "b": "y"}}])
write("smap", "s", pa.struct([("m", MAP)]), [{"m": [("k", 1)]}, {"m": [("z", 3)]}])
write("lmap", "l", pa.list_(MAP), [[[("k", 1)]], [[("z", 3)]]])
write("lol", "l", pa.list_(pa.list_(I64)), [[[1], [2, 3]], [[4]]])
write("qname", "s", pa.struct([("Weird Name", pa.int16())]), [{"Weird Name": 1}, {"Weird Name": 2}])

req = pa.schema([
    pa.field("l", pa.list_(pa.field("element", I64, nullable=False)), nullable=False),
    pa.field("s", pa.struct([pa.field("a", I64, nullable=False)]), nullable=False),
    pa.field("o", pa.list_(pa.field("element", I64, nullable=False)), nullable=True),
])
pq.write_table(pa.Table.from_pylist([{"l": [1], "s": {"a": 1}, "o": [5]},
                                     {"l": [2], "s": {"a": 2}, "o": None}], schema=req),
               f"{W}/req.parquet")


leafnull = pa.schema([
    pa.field("m", MAP, nullable=False),
    pa.field("l", pa.list_(pa.field("element", pa.struct([("x", I64)]), nullable=True)), nullable=False),
    pa.field("o", MAP, nullable=True),
])
pq.write_table(pa.Table.from_pylist([{"m": [("k", 1)], "l": [{"x": 1}], "o": [("j", 2)]},
                                     {"m": [("z", None)], "l": [None], "o": None}], schema=leafnull),
               f"{W}/leafnull.parquet")


# ---- the legacy two-level list, by a footer rewrite ---------------------------
# A small Thrift compact decoder and encoder: a struct is a list of
# [field id, compact type, value]. A bool field keeps its value in the type.
class Dec:
    def __init__(self, b):
        self.b, self.p = b, 0

    def byte(self):
        c = self.b[self.p]
        self.p += 1
        return c

    def varint(self):
        out = shift = 0
        while True:
            c = self.byte()
            out |= (c & 0x7F) << shift
            if not c & 0x80:
                return out
            shift += 7

    def zz(self):
        u = self.varint()
        return (u >> 1) ^ -(u & 1)

    def value(self, t, in_list=False):
        if t in (1, 2):
            return self.byte() if in_list else None
        if t == 3:
            return self.byte()
        if t in (4, 5, 6):
            return self.zz()
        if t == 7:
            self.p += 8
            return self.b[self.p - 8:self.p]
        if t == 8:
            n = self.varint()
            self.p += n
            return self.b[self.p - n:self.p]
        if t in (9, 10):
            c = self.byte()
            n, et = (c >> 4) & 0x0F, c & 0x0F
            if n == 0x0F:
                n = self.varint()
            return [et, [self.value(et, True) for _ in range(n)]]
        if t == 12:
            return self.struct()
        raise ValueError(f"compact type {t}")

    def struct(self):
        fields, last = [], 0
        while True:
            c = self.byte()
            if c == 0:
                return fields
            t, d = c & 0x0F, c >> 4
            fid = last + d if d else self.zz()
            last = fid
            fields.append([fid, t, self.value(t)])


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
    if t in (1, 2):
        return bytes([v]) if in_list else b""
    if t == 3:
        return bytes([v])
    if t in (4, 5, 6):
        return enc_zz(v)
    if t == 7:
        return bytes(v)
    if t == 8:
        return enc_varint(len(v)) + bytes(v)
    if t in (9, 10):
        et, items = v
        hdr = bytes([(len(items) << 4) | et]) if len(items) < 15 else bytes([0xF0 | et]) + enc_varint(len(items))
        return hdr + b"".join(enc_value(et, x, True) for x in items)
    if t == 12:
        return enc_struct(v)
    raise ValueError(f"compact type {t}")


def enc_struct(fields):
    out, last = bytearray(), 0
    for fid, t, v in fields:
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


TWO = f"{W}/twolevel.parquet"
two = pa.schema([pa.field("l", pa.list_(pa.field("element", I64, nullable=False)), nullable=False),
                 pa.field("n", pa.int32())])
pq.write_table(pa.Table.from_pylist([{"l": [1, 2], "n": 1}, {"l": [3], "n": 2}], schema=two), TWO,
               store_schema=False)
raw = open(TWO, "rb").read()
flen = int.from_bytes(raw[-8:-4], "little")
base = len(raw) - 8 - flen
meta = Dec(raw[base:base + flen]).struct()
schema = fld(meta, 2)[2][1]                  # list<SchemaElement>
names = [bytes(fld(e, 4)[2]) for e in schema]
assert names == [b"schema", b"l", b"list", b"element", b"n"], names
leaf = [f for f in schema[3] if f[0] != 3] + [[3, 5, 2]]   # repetition_type REPEATED
fld(leaf, 4)[2] = b"l"
leaf.sort(key=lambda f: f[0])
schema[1:4] = [leaf]
for rg in fld(meta, 4)[2][1]:                # each row group: the l chunk's path_in_schema
    cmd = fld(fld(rg, 1)[2][1][0], 3)[2]
    fld(cmd, 3)[2] = [8, [b"l"]]
foot = enc_struct(meta)
open(TWO, "wb").write(raw[:base] + foot + len(foot).to_bytes(4, "little") + b"PAR1")
# pyarrow, the independent reader, must read the rewritten file back unchanged
got = pq.read_table(TWO).to_pylist()
assert got == [{"l": [1, 2], "n": 1}, {"l": [3], "n": 2}], got

for f in os.listdir(W):
    if f.endswith(".parquet"):
        os.chmod(os.path.join(W, f), 0o644)
