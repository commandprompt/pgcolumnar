#!/usr/bin/env python3
"""Craft Parquet files whose footer declares counts the data does not have.

Locate a Thrift-compact varint structurally, splice a new encoding in, and fix
the 4-byte footer length in the trailer. Everything changed lives in the footer,
which sits AFTER every data page, so the page offsets stay valid.

Thrift field ids (parquet.thrift):
  FileMetaData:    3 num_rows(i64) 4 row_groups(list)
  RowGroup:        1 columns(list) 2 total_byte_size(i64) 3 num_rows(i64)
  ColumnChunk:     3 meta_data(ColumnMetaData)
  ColumnMetaData:  1 type 2 encodings 3 path_in_schema 4 codec 5 num_values(i64)
"""
import sys, pyarrow as pa, pyarrow.parquet as pq


class R:
    """A cursor over `raw` with Thrift-compact primitives."""
    def __init__(self, raw, pos): self.raw, self.pos = raw, pos

    def u8(self):
        b = self.raw[self.pos]; self.pos += 1; return b

    def varint(self):
        sh = out = 0
        while True:
            b = self.u8(); out |= (b & 0x7F) << sh
            if not b & 0x80: return out
            sh += 7

    def zigzag(self):
        u = self.varint(); return (u >> 1) ^ -(u & 1)

    def list_hdr(self):
        b = self.u8(); sz, et = (b >> 4) & 0x0F, b & 0x0F
        if sz == 0x0F: sz = self.varint()
        return sz, et

    def skip(self, t):
        if t in (1, 2): return
        if t == 3: self.pos += 1
        elif t in (4, 5, 6): self.zigzag()
        elif t == 7: self.pos += 8
        elif t == 8:
            n = self.varint(); self.pos += n   # read length, THEN skip data;
            #                                    `pos += varint()` clobbers varint's own advance
        elif t in (9, 10):
            n, et = self.list_hdr()
            for _ in range(n): self.skip(et)
        elif t == 12:
            self.skip_struct()
        else:
            raise SystemExit("unexpected thrift type %d at %d" % (t, self.pos))

    def field(self, last):
        """Read a field header. Returns (type, fid, new_last); type 0 = STOP."""
        b = self.u8()
        if b == 0: return 0, 0, last
        t = b & 0x0F; d = b >> 4
        fid = last + d if d else self.zigzag()
        return t, fid, fid

    def skip_struct(self):
        last = 0
        while True:
            t, _fid, last = self.field(last)
            if t == 0: return
            self.skip(t)

    def enter_list(self):
        """A list<struct>: return the element count; caller reads each struct."""
        n, et = self.list_hdr()
        assert et == 12, "expected list<struct>, got element type %d" % et
        return n


def _find_i64(raw, path):
    """path: list of (field_id, kind) where kind is 'struct'|'list-struct'|'i64'.
    Returns (start, end) byte range of the i64 varint at the end of the path."""
    r = R(raw, len(raw) - 8 - int.from_bytes(raw[-8:-4], "little"))
    for fid_want, kind in path:
        last = 0
        while True:
            t, fid, last = r.field(last)
            if t == 0:
                raise SystemExit("field %d not found" % fid_want)
            if fid == fid_want:
                if kind == "i64":
                    s = r.pos; r.zigzag(); return s, r.pos
                if kind == "list-struct":
                    r.enter_list()           # position now at first element struct
                break
            r.skip(t)
    raise SystemExit("path exhausted")


def find(raw, what):
    if what == "num_rows":
        return _find_i64(raw, [(4, "list-struct"), (3, "i64")])
    if what == "file_num_rows":                  # FileMetaData 3 num_rows(i64)
        return _find_i64(raw, [(3, "i64")])
    if what == "num_values":
        return _find_i64(raw, [(4, "list-struct"), (1, "list-struct"),
                               (3, "struct"), (5, "i64")])
    raise SystemExit("unknown field " + what)


def zz_bytes(v):
    u = (v << 1) ^ (v >> 63) if v < 0 else (v << 1)
    out = bytearray()
    while True:
        b = u & 0x7F; u >>= 7
        out.append(b | 0x80 if u else b)
        if not u: return bytes(out)


def craft(src, dst, what, value):
    raw = bytearray(open(src, "rb").read())
    s, e = find(raw, what)
    raw[s:e] = zz_bytes(value)
    flen = int.from_bytes(raw[-8:-4], "little") + (len(zz_bytes(value)) - (e - s))
    raw[-8:-4] = flen.to_bytes(4, "little")
    open(dst, "wb").write(bytes(raw))


if __name__ == "__main__":
    W = sys.argv[1]
    pq.write_table(pa.table({"a": pa.array(list(range(10, 18)), pa.int32())}),
                   f"{W}/honest.parquet", compression="none")
    raw = bytearray(open(f"{W}/honest.parquet", "rb").read())
    # self-check against the honest file before crafting anything
    for what in ("num_rows", "num_values"):
        s, e = find(raw, what)
        got = R(raw, s).zigzag()
        assert got == 8, "self-check: %s decoded %d, expected 8" % (what, got)
    craft(f"{W}/honest.parquet", f"{W}/bignv.parquet",  "num_values", 1 << 62)
    craft(f"{W}/honest.parquet", f"{W}/bignr.parquet",  "num_rows",   40)
    craft(f"{W}/honest.parquet", f"{W}/hugenr.parquet", "num_rows",   5_000_000)

    # Repeated (LIST) column: num_values counts ELEMENTS, not records (#571). One
    # record of eight elements has num_values = 8, so num_rows = 8 slips past the
    # num_rows <= num_values check yet drives the row loop past the one record.
    # A valid three-record list is crafted too, to prove the per-record guard
    # does not over-reject a legitimately multi-row list.
    pq.write_table(pa.table({"a": pa.array([[1, 2, 3, 4, 5, 6, 7, 8]], pa.list_(pa.int32()))}),
                   f"{W}/list_honest.parquet", compression="none")
    craft(f"{W}/list_honest.parquet", f"{W}/list_bad.parquet", "num_rows", 8)
    # The footer's own total must agree with the row groups (#1822), or that check
    # refuses the file first and the per-record guard is never reached.
    craft(f"{W}/list_bad.parquet", f"{W}/list_bad.parquet", "file_num_rows", 8)
    pq.write_table(pa.table({"a": pa.array([[1, 2], [3], [4, 5, 6, 7, 8]], pa.list_(pa.int32()))}),
                   f"{W}/list_valid3.parquet", compression="none")
    print("self-check OK; crafted scalar bignv/bignr/hugenr and list_honest/list_bad/list_valid3")
