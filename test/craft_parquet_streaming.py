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


# #2077, built by hand so it does not depend on what a pyarrow version writes: pages
#    with no values inserted between the data pages of a 200-row PLAIN column written
#    as four pages of 50. The footer's chunk sizes follow the inserted bytes, so
#    pyarrow reads the files with one inserted page too. (pyarrow 23 reads a file with
#    two or more empty pages in a row as only the rows before the run.)
#
#    The reader decodes an empty data page like any other: it adds no entries. So it
#    reads what pyarrow reads (a v1 page on a REQUIRED column needs no body; a v2 page
#    keeps its zero level lengths in the header) and refuses what pyarrow refuses (a
#    v1 page on a nullable column with no body has no definition-level length to
#    read; a garbage body is decoded and fails). It refuses more than 16 empty pages
#    in a row, and a DATA_PAGE whose DataPageHeader is missing.
def empty_data_page(body=bytes(4)):
    """a v1 data page with no values; the default body is a zero definition-level
    length (4 bytes)"""
    return (fh(1, I32) + zz(0) + fh(1, I32) + zz(len(body)) + fh(1, I32) + zz(len(body))
            + fh(2, STRUCT) + fh(1, I32) + zz(0) + fh(1, I32) + zz(0)
            + fh(1, I32) + zz(3) + fh(1, I32) + zz(3) + STOP + STOP + body)


def empty_v2_page():
    """a DATA_PAGE_V2 with no values, no levels and no body"""
    return (fh(1, I32) + zz(3) + fh(1, I32) + zz(0) + fh(1, I32) + zz(0)
            + fh(5, STRUCT) + fh(1, I32) + zz(0) + fh(1, I32) + zz(0)
            + fh(1, I32) + zz(0) + fh(1, I32) + zz(0) + fh(1, I32) + zz(0)
            + fh(1, I32) + zz(0) + STOP + STOP)


def index_page():
    """an INDEX_PAGE (type 1) with a 4-byte body and no type-specific header: a page
    type the reader does not decode, which pyarrow steps over. The body would fail to
    decode as a data page, so reading the file shows the page was stepped over."""
    return fh(1, I32) + zz(1) + fh(1, I32) + zz(4) + fh(1, I32) + zz(4) + STOP + b"\xff" * 4


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


def page_bounds(buf, i):
    """-> (end of header, end of page) for the page whose header starts at buf[i].
    pyarrow writes fields 1, 2 and 3 (type, uncompressed_size, compressed_size)
    first, each a one-byte field header and a zigzag varint."""
    hend = skip_value(buf, i, 12)
    _, j = varint(buf, i + 1)            # type
    _, j = varint(buf, j + 1)            # uncompressed_size
    csz, _ = varint(buf, j + 1)          # compressed_size
    return hend, hend + (csz >> 1)


def four_pages(name, nullable):
    """-> (footer, page area, [offset of each page in the page area]) of a 200-row
    PLAIN int column written as four pages of 50"""
    path = os.path.join(W, name)
    pq.write_table(pa.table({"id": pa.array(list(range(200)), pa.int32())},
                            schema=pa.schema([pa.field("id", pa.int32(), nullable=nullable)])),
                   path, compression="none", use_dictionary=False, data_page_size=1,
                   write_batch_size=50)
    m, _, b = parts(path)
    md = pq.ParquetFile(path).metadata.row_group(0).column(0)
    start = md.data_page_offset - 4
    if (md.dictionary_page_offset or md.total_compressed_size != md.total_uncompressed_size
            or b[start] != fh(1, I32)[0]):
        sys.exit(5)
    offs = [start]
    while len(offs) < 4:
        offs.append(page_bounds(b, offs[-1])[1])
    if page_bounds(b, offs[3])[1] != start + md.total_compressed_size:
        sys.exit(5)
    return m, b, offs, md.total_compressed_size


popt = four_pages("paged.parquet", True)
preq = four_pages("paged_req.parquet", False)


def with_pages(name, src, inserts):
    """write src with inserts[k] (bytes) placed after its page k (1-based)"""
    m, b, offs, total = src
    out, last = b[:offs[0]], offs[0]
    for k in (1, 2, 3):
        out += b[last:offs[k]] + inserts.get(k, b"")
        last = offs[k]
    out += b[last:]
    extra = sum(len(x) for x in inserts.values())
    if m.count(zz(total)) < 1:
        sys.exit(5)
    m = m.replace(zz(total), zz(total + extra))
    build(name, out, m, len(m))


with_pages("one_empty_page.parquet", popt, {1: empty_data_page()})
with_pages("empty_page_run.parquet", popt, {1: empty_data_page() * 1000})
with_pages("empty_run16.parquet", popt, {1: empty_data_page() * 16})
with_pages("empty_run17.parquet", popt, {1: empty_data_page() * 17})
# 16 in a row is the limit per run, not per chunk: two runs of 10 read
with_pages("empty_runs_10_10.parquet", popt, {1: empty_data_page() * 10, 2: empty_data_page() * 10})
# an empty page with no body: pyarrow reads it on a REQUIRED v1 column and as a v2
# page, and refuses it on a nullable v1 column ("Received invalid levels")
with_pages("nobody_v1_req.parquet", preq, {1: empty_data_page(b"")})
with_pages("nobody_v2.parquet", popt, {1: empty_v2_page()})
with_pages("nobody_v1_opt.parquet", popt, {1: empty_data_page(b"")})
# an empty page whose body is garbage: pyarrow refuses it ("Received invalid number
# of bytes (corrupt data page?)")
with_pages("garbage_body.parquet", popt, {1: empty_data_page(b"\xff\xff\xff\xff")})
# an INDEX_PAGE between data pages: pyarrow steps over it and reads 200 rows. Such a
# page counts toward the run limit too, so 17 in a row are refused.
with_pages("index_page.parquet", popt, {1: index_page()})
with_pages("index_run17.parquet", popt, {1: index_page() * 17})


# A DATA_PAGE whose DataPageHeader is missing: page 2's header is replaced by one with
# only type 0 and its sizes, and its body is kept. The footer's sizes shrink to match,
# and its value and row counts drop from 200 to 150, so the other three pages complete
# the chunk. Stepping over the page would read 150 rows with ids 50..99 silently gone;
# pyarrow refuses it ("Unknown encoding type for levels").
def nohdr_loss():
    m, b, offs, total = popt
    hend, pend = page_bounds(b, offs[1])
    hdr = fh(1, I32) + zz(0) + fh(1, I32) + zz(pend - hend) + fh(1, I32) + zz(pend - hend) + STOP
    out = b[:offs[1]] + hdr + b[hend:]
    shrink = (hend - offs[1]) - len(hdr)
    if m.count(zz(total)) < 1 or m.count(zz(200)) != 4:
        sys.exit(5)
    m = m.replace(zz(total), zz(total - shrink)).replace(zz(200), zz(150))
    build("nohdr_loss.parquet", out, m, len(m))


nohdr_loss()

# #2077 acceptance: a dictionary-encoded list<int64> column, snappy, small pages, so a
# compressed empty data page is in the chunk. pyarrow reads it as {1,2} NULL {3} {}.
pq.write_table(pa.table({"li": pa.array([[1, 2], None, [3], []], pa.list_(pa.int64()))}),
               os.path.join(W, "empty_page_snappy.parquet"),
               use_dictionary=True, data_page_size=1, write_batch_size=1,
               compression="snappy")

# #620: four row groups of 1000 rows, for the FDW's Row Groups Decoded counter
n = 4000
pq.write_table(pa.table({"id": pa.array(range(n), pa.int32()),
                         "v": pa.array([i * 2 for i in range(n)], pa.int64())}),
               os.path.join(W, "stream_groups.parquet"), row_group_size=1000)
if pq.ParquetFile(os.path.join(W, "stream_groups.parquet")).num_row_groups != 4:
    sys.exit(6)

for name in ("zero_pages", "bad_size", "noise_pages", "bad_offset", "two_dicts",
             "v2_levels", "allnull", "empty_data_page", "one_empty_page",
             "empty_page_run", "empty_run16", "empty_run17", "empty_runs_10_10",
             "nobody_v1_req", "nobody_v2", "nobody_v1_opt", "garbage_body", "index_page",
             "index_run17",
             "nohdr_loss", "empty_page_snappy", "stream_groups"):
    if not os.path.exists(os.path.join(W, name + ".parquet")):
        sys.exit(6)
