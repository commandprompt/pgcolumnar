"""Write every Parquet file native_parquet_streaming.sh and its twin read, into DIR.

Usage: craft_parquet_streaming.py DIR

Exit status, as the shell reads it:
  0  every file was written
  3  no room even for the oversized sparse file's metadata
  4  the sparse file did not reach its logical size (no sparse files here)
  5  the footer offset to patch is ambiguous; refusing to guess
  6  a crafted file is missing after writing
  7  the source file has no dictionary page to duplicate

3 and 4 mean the box cannot build the oversized file, which the shell records as a
named skip; any other failure means the crafted files are not all there, which it
records as a failed check rather than letting each refusal pass on "could not open
file". The construction of each file, and why, is described beside it below.
"""
import os
import sys

import pyarrow as pa
import pyarrow.parquet as pq

W = sys.argv[1]


def parts(path):
    raw = open(path, "rb").read()
    n = int.from_bytes(raw[-8:-4], "little")
    return raw[-8 - n:-8], n, raw[4:-8 - n]   # footer, its length, page area


# ---- a real small file, and an oversized sparse one carrying its footer ------------
src = os.path.join(W, "small.parquet")
pq.write_table(pa.table({"id": pa.array([1, 2, 3], pa.int32()),
                         "v": pa.array(["a", "b", "c"])}), src)
data = open(src, "rb").read()
metalen = int.from_bytes(data[-8:-4], "little")
meta = data[-8 - metalen:-8]

# comfortably over MaxAllocSize (1GB - 1), which is what palloc() rejected
SIZE = 1600 * 1024 * 1024
st = os.statvfs(W)
if st.f_bavail * st.f_frsize < 64 * 1024 * 1024:
    sys.exit(3)
big = os.path.join(W, "huge_sparse.parquet")
with open(big, "wb") as f:
    f.write(b"PAR1")
    f.truncate(SIZE)                 # the hole; never read by the footer path
    f.seek(SIZE - 8 - metalen)
    f.write(meta)
    f.write(metalen.to_bytes(4, "little"))
    f.write(b"PAR1")
if os.path.getsize(big) != SIZE:
    sys.exit(4)

# ---- crafted files: each keeps a valid footer and breaks one thing the page loop
# must not trust. The footer comes from a real uncompressed file, so a zeroed page
# area really parses as page headers rather than failing in the codec first.
plain = os.path.join(W, "plain.parquet")
pq.write_table(pa.table({"id": pa.array(list(range(200)), pa.int32())}),
               plain, compression="none")
meta, metalen, body = parts(plain)


def build(name, body_bytes, m=None, ml=None):
    with open(os.path.join(W, name), "wb") as f:
        f.write(b"PAR1")
        f.write(body_bytes)
        f.write(m if m is not None else meta)
        f.write((ml if ml is not None else metalen).to_bytes(4, "little"))
        f.write(b"PAR1")


# 1. a large zero-filled page area of a REQUIRED column (no definition levels, so the
#    zero-value page is reached), sparse: each zero byte parses as an empty v1 data
#    page, so without the progress guard the loop walks it one byte at a time.
req = os.path.join(W, "plain_req.parquet")
pq.write_table(pa.table({"id": pa.array(list(range(200)), pa.int32())},
                        schema=pa.schema([pa.field("id", pa.int32(), nullable=False)])),
               req, compression="none")
rmeta, rmetalen, rbody = parts(req)
ZSIZE = 1600 * 1024 * 1024
with open(os.path.join(W, "zero_pages.parquet"), "wb") as f:
    f.write(b"PAR1")
    f.truncate(ZSIZE - 8 - rmetalen)
    f.seek(ZSIZE - 8 - rmetalen)
    f.write(rmeta)
    f.write(rmetalen.to_bytes(4, "little"))
    f.write(b"PAR1")


# 2. a page header (built by hand, Thrift compact) declaring a compressed_size far
#    past end of file, written over the start of the chunk the footer points at.
def zz(v):
    u = (v << 1) ^ (v >> 63) if v < 0 else (v << 1)
    out = bytearray()
    while True:
        b = u & 0x7F
        u >>= 7
        out.append(b | (0x80 if u else 0))
        if not u:
            return bytes(out)


def fh(delta, t):
    return bytes([(delta << 4) | t])


I32, STRUCT, STOP = 5, 12, b"\x00"
BIG = 500 * 1024 * 1024          # past end of file, but under MaxAllocSize
synth = (fh(1, I32) + zz(0) + fh(1, I32) + zz(64) + fh(1, I32) + zz(BIG)
         + fh(2, STRUCT) + fh(1, I32) + zz(200) + fh(1, I32) + zz(0)
         + fh(1, I32) + zz(3) + fh(1, I32) + zz(3) + STOP + STOP)
build("bad_size.parquet", synth + body[len(synth):])

# 3. the page area is noise, so the page header does not parse at all.
build("noise_pages.parquet", bytes((i * 37 + 11) & 0xFF for i in range(len(body))))

# 4. the footer points the SECOND column chunk's start offset to -2^20: negative, so
#    it passes the loop's "pos < len" and reaches the header read's offset check.
two = os.path.join(W, "two.parquet")
pq.write_table(pa.table({"id": pa.array(list(range(200)), pa.int32()),
                         "v": pa.array(["r%04d" % i for i in range(200)])}),
               two, compression="none", use_dictionary=False)
tmeta, tmetalen, tbody = parts(two)
tcol = pq.ParquetFile(two).metadata.row_group(0).column(1)
tstart = tcol.dictionary_page_offset or tcol.data_page_offset
pat = zz(tstart)
if tmeta.count(pat) != 1:
    sys.exit(5)
patched = tmeta.replace(pat, zz(-(1 << 20)))
build("bad_offset.parquet", tbody, patched, len(patched))

# 5. two dictionary pages in one chunk (the format allows at most one).
dmd = pq.ParquetFile(plain).metadata.row_group(0).column(0)
if not dmd.dictionary_page_offset or dmd.data_page_offset <= dmd.dictionary_page_offset:
    sys.exit(7)
dlen = dmd.data_page_offset - dmd.dictionary_page_offset
doff = dmd.dictionary_page_offset - 4
build("two_dicts.parquet", body[:doff] + body[doff:doff + dlen] * 2 + body[doff + dlen:])

# 6. a DATA_PAGE_V2 whose declared level lengths exceed the page (see the shell for
#    why this rejection does not isolate the v2 guard).
synth_v2 = (fh(1, I32) + zz(3) + fh(1, I32) + zz(64) + fh(1, I32) + zz(32)
            + fh(5, STRUCT) + fh(1, I32) + zz(200) + fh(1, I32) + zz(0)
            + fh(1, I32) + zz(200) + fh(1, I32) + zz(0) + fh(1, I32) + zz(64)
            + fh(1, I32) + zz(0) + fh(1, 2) + STOP + STOP)
build("v2_levels.parquet", synth_v2 + body[len(synth_v2):])

# an all-null column: pyarrow writes it with an EMPTY dictionary page
pq.write_table(pa.table({"c": pa.array([None] * 100, pa.int32())}),
               os.path.join(W, "allnull.parquet"), use_dictionary=True, compression="none")

# #2077: a valid data page with no values. pyarrow writes one for a dictionary-
#    encoded list column when it flushes pages often: this file's chunk has data
#    pages with 1, 0 and 1 values, and pyarrow reads it back as {yy} and NULL.
pq.write_table(pa.table({"ls": pa.array([["yy"], None], pa.list_(pa.string()))}),
               os.path.join(W, "empty_data_page.parquet"),
               use_dictionary=True, data_page_size=1, write_batch_size=1,
               compression="none")


# #2077, built by hand so it does not depend on what a pyarrow version writes: empty
#    v1 data pages (num_values 0) inserted between the first two data pages of a
#    200-row PLAIN column written as four pages. Each empty page's body is a zero
#    definition-level length (4 bytes) and no values. The footer's chunk sizes grow
#    by the inserted bytes, so pyarrow reads the one-page file too, as 200 rows. One
#    empty page must read; a run of 1000 is refused, so a crafted file cannot make
#    the page loop walk a large region a few bytes at a time. (pyarrow itself reads
#    the run file as only the 50 rows before the run.)
def empty_data_page():
    return (fh(1, I32) + zz(0) + fh(1, I32) + zz(4) + fh(1, I32) + zz(4)
            + fh(2, STRUCT) + fh(1, I32) + zz(0) + fh(1, I32) + zz(0)
            + fh(1, I32) + zz(3) + fh(1, I32) + zz(3) + STOP + STOP + bytes(4))


def varint(buf, i):
    v = s = 0
    while True:
        b = buf[i]
        i += 1
        v |= (b & 0x7F) << s
        s += 7
        if not b & 0x80:
            return v, i


def skip_value(buf, i, t):
    """-> the offset after one Thrift compact value of type t at buf[i]."""
    if t in (1, 2):
        return i
    if t == 3:
        return i + 1
    if t in (4, 5, 6):
        return varint(buf, i)[1]
    if t == 7:
        return i + 8
    if t == 8:
        n, i = varint(buf, i)
        return i + n
    if t in (9, 10):
        n, et = buf[i] >> 4, buf[i] & 0x0F
        i += 1
        if n == 15:
            n, i = varint(buf, i)
        for _ in range(n):
            i = skip_value(buf, i, et)
        return i
    if t == 12:
        while buf[i] != 0:
            b = buf[i]
            i += 1
            if not b >> 4:               # long form: the field id follows
                i = varint(buf, i)[1]
            i = skip_value(buf, i, b & 0x0F)
        return i + 1
    sys.exit(5)


def page_end(buf, i):
    """-> the offset after the page whose header starts at buf[i]: its header, then
    compressed_size bytes. pyarrow writes fields 1, 2 and 3 (type, uncompressed_size,
    compressed_size) first, each a one-byte field header and a zigzag varint."""
    end = skip_value(buf, i, 12)
    _, j = varint(buf, i + 1)            # type
    _, j = varint(buf, j + 1)            # uncompressed_size
    csz, _ = varint(buf, j + 1)          # compressed_size
    return end + (csz >> 1)


paged = os.path.join(W, "paged.parquet")
pq.write_table(pa.table({"id": pa.array(list(range(200)), pa.int32())}), paged,
               compression="none", use_dictionary=False, data_page_size=1,
               write_batch_size=50)
pmeta, pmetalen, pbody = parts(paged)
pmd = pq.ParquetFile(paged).metadata.row_group(0).column(0)
if (pmd.dictionary_page_offset or pmd.total_compressed_size != pmd.total_uncompressed_size
        or pbody[pmd.data_page_offset - 4] != fh(1, I32)[0]):
    sys.exit(5)
psplit = page_end(pbody, pmd.data_page_offset - 4)


def with_empty_pages(name, count):
    extra = empty_data_page() * count
    old = zz(pmd.total_compressed_size)
    if pmeta.count(old) < 1:
        sys.exit(5)
    m = pmeta.replace(old, zz(pmd.total_compressed_size + len(extra)))
    build(name, pbody[:psplit] + extra + pbody[psplit:], m, len(m))


with_empty_pages("one_empty_page.parquet", 1)
with_empty_pages("empty_page_run.parquet", 1000)

# #620: four row groups of 1000 rows, for the FDW's Row Groups Decoded counter
n = 4000
pq.write_table(pa.table({"id": pa.array(range(n), pa.int32()),
                         "v": pa.array([i * 2 for i in range(n)], pa.int64())}),
               os.path.join(W, "stream_groups.parquet"), row_group_size=1000)
if pq.ParquetFile(os.path.join(W, "stream_groups.parquet")).num_row_groups != 4:
    sys.exit(6)

for name in ("zero_pages", "bad_size", "noise_pages", "bad_offset", "two_dicts",
             "v2_levels", "allnull", "empty_data_page", "one_empty_page",
             "empty_page_run", "stream_groups"):
    if not os.path.exists(os.path.join(W, name + ".parquet")):
        sys.exit(6)
