#!/usr/bin/env python3
#
# Deterministic byte-level mutator for the Parquet decode fuzzer (issue #214).
#
# Every mutant is a pure function of (seed file, seed integer), so a crash
# reproduces exactly from the two values printed with it. Nothing here is
# random at run time.
#
# The mutation set is chosen for what a hand-written Thrift and Parquet reader
# gets wrong, not for uniform coverage of the byte space. A flat bit-flipper
# spends nearly all of its budget inside page payloads, where the worst outcome
# is a wrong value; the interesting failures are in the footer, the schema
# chain, and any field the reader trusts as a length, a count, or an offset.
#
# Usage:  python3 test/parquet_mutate.py SEEDFILE SEED OUTFILE
#         python3 test/parquet_mutate.py --schema-sweep SEEDFILE OUTDIR
#
import os
import random
import struct
import sys

# Values that break arithmetic on a field the reader treats as a size or a
# count: zero, the negatives, the signed and unsigned ceilings, the ones that
# overflow a 32-bit multiply by a small element width, and the ones that overflow
# a 64-bit size_t multiply.
#
# The 64-bit row is why this list grew (#570/#571). A count of ~2^61 makes
# sizeof(Datum) * count = 8 * 2^61 wrap size_t to a small value, so palloc
# succeeds at a size far below what the count implies and the decode writes past
# it. A merely-large count like 10^9 is rejected cleanly by MaxAllocSize; only the
# wrapping magnitudes near 2^61..2^63 are dangerous, and the old ceiling of
# 0x7FFFFFFF (2^31) could not reach them. num_values and num_rows are i64 thrift
# fields, so those magnitudes have to be produced as wide varints (below), not
# only as plain 8-byte windows.
EXTREMES = [
    0, 1, -1, 2, -2,
    0x7F, 0x80, 0xFF,
    0x7FFF, 0x8000, 0xFFFF,
    0x7FFFFFFF, 0x80000000, 0xFFFFFFFF,
    0x40000000, 0x20000000, 0x10000000,
    # 64-bit size_t multiply-wrap magnitudes (#570/#571)
    0x2000000000000000, 0x4000000000000000, 0x1FFFFFFFFFFFFFFF,
    0x7FFFFFFFFFFFFFFF, 0x8000000000000000, 0xFFFFFFFFFFFFFFFF,
]


def _flip_bits(b, rnd):
    """Classic bit flips. Cheap, and the only mutation that reaches deep into a
    compressed page where structure is opaque to us."""
    n = rnd.randint(1, 16)
    for _ in range(n):
        i = rnd.randrange(len(b))
        b[i] ^= 1 << rnd.randrange(8)


def _extreme_int(b, rnd):
    """Overwrite a 4- or 8-byte little-endian window with a boundary value.
    Thrift varints are not aligned, so this is scattershot by design: it hits
    plain-encoded lengths, page header fields and offsets alike."""
    width = rnd.choice((4, 4, 4, 8))
    if len(b) < width:
        return
    i = rnd.randrange(len(b) - width + 1)
    v = rnd.choice(EXTREMES)
    # Mask to the window width and pack unsigned, so a value at or above the
    # signed ceiling (the 2^63 wrap magnitudes) writes its bit pattern rather
    # than raising. The reader interprets those bytes as its own signed field.
    mask = (1 << (width * 8)) - 1
    b[i:i + width] = struct.pack("<Q" if width == 8 else "<I", v & mask)


def _varint_extreme(b, rnd):
    """Thrift compact protocol encodes lengths, list sizes and the i64 count
    fields as varints, so a boundary value has to be written as a varint to be
    read as one. Writes a full-width varint in place, up to ten bytes, which is
    what a large i64 count looks like on the wire.

    This reaches list and element counts (the #210 class: a schema child count is
    a varint the reader trusts) AND the i64 num_values/num_rows (the #570/#571
    class). num_values is a zigzag i64, so a raw varint v is read as
    (v >> 1) ^ -(v & 1); an even 2^62 therefore reads as +2^61, exactly the
    size_t multiply-wrap magnitude. The old five-byte, 2^31-ceiling form could not
    encode a value that large, which is why the fuzzer never reached this class.
    Ten bytes covers the full uint64 range."""
    width = 10
    if len(b) < width:
        return
    i = rnd.randrange(len(b) - width + 1)
    v = rnd.choice(EXTREMES) & 0xFFFFFFFFFFFFFFFF
    out = bytearray()
    while True:
        byte = v & 0x7F
        v >>= 7
        if v:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            break
    out = (out + bytearray(b"\x80" * width))[:width]
    # a run of 0x80 continuation bytes with no terminator would be an unterminated
    # varint; cap the last byte so the value ends inside the window.
    out[width - 1] &= 0x7F
    b[i:i + width] = out


def _truncate(b, rnd):
    """A short read is the commonest malformed file in the wild, and every
    bound in the reader has to hold when the bytes simply stop."""
    if len(b) < 16:
        return
    keep = rnd.randrange(4, len(b))
    del b[keep:]


def _footer_len(b, rnd):
    """The last 8 bytes are the 4-byte footer length and the PAR1 magic. The
    reader seeks backwards by that length, so it is the single field with the
    most leverage over where parsing starts."""
    if len(b) < 8:
        return
    v = rnd.choice(EXTREMES + [len(b), len(b) - 8, len(b) * 2])
    b[len(b) - 8:len(b) - 4] = struct.pack("<I", v & 0xFFFFFFFF)


def _magic(b, rnd):
    """Head or tail magic. Cheap, and it proves the reader checks both rather
    than trusting the file because one end looked right."""
    if len(b) < 8:
        return
    if rnd.random() < 0.5:
        b[0:4] = bytes(rnd.randrange(256) for _ in range(4))
    else:
        b[len(b) - 4:] = bytes(rnd.randrange(256) for _ in range(4))


def _splice(b, rnd):
    """Duplicate a byte range in place. In a Thrift struct chain this can repeat
    a nesting construct, which is how a deep schema arises from a shallow seed
    without hand-building one."""
    if len(b) < 32:
        return
    start = rnd.randrange(len(b) - 16)
    end = min(len(b), start + rnd.randrange(4, 256))
    chunk = bytes(b[start:end])
    reps = rnd.choice((2, 4, 8, 32, 128))
    at = rnd.randrange(len(b))
    b[at:at] = chunk * reps
    del b[64 * 1024 * 1024:]        # keep a runaway splice from eating the box


def _delete(b, rnd):
    """Remove a run, shifting every later offset. Offsets stored in the footer
    then point into the wrong place, which is the bounds case that a pure
    overwrite mutation never produces."""
    if len(b) < 64:
        return
    start = rnd.randrange(len(b) - 32)
    end = min(len(b), start + rnd.randrange(1, 64))
    del b[start:end]


def _deep_nest(b, rnd):
    """Replace the footer with a chain of nested Thrift structs.

    This one is not a random mutation and is not meant to be. Byte-level
    mutation of a shallow file effectively never produces deep nesting: 250
    mutants against a build with both #210 guards deliberately removed produced
    no crash at all, because the depth needed to exhaust the stack does not
    arise by chance from flipping bits in a 1.5 kB file. The bug class that
    motivated this whole suite was invisible to it.

    Thrift compact encodes a field header as (delta << 4) | type, and type 0x0C
    is a struct, so 0x1C opens a nested struct with field delta 1. A run of them
    is a nesting chain of exactly that length, and skipping an unknown struct
    recurses once per level.

    The file is rebuilt rather than patched: header magic, the chain as the
    entire footer, the footer length, and the trailing magic, which is the
    smallest thing the reader will accept as far as the footer parse.
    """
    depth = rnd.choice((1000, 10000, 100000, 400000, 1000000))
    chain = b"\x1c" * depth
    out = bytearray(b"PAR1")
    out += chain
    out += struct.pack("<I", len(chain))
    out += b"PAR1"
    b[:] = out


def _deep_schema(b, rnd):
    """The same idea aimed at the second #210 vector.

    `walk_schema` recurses on num_children read from the footer, and it runs
    only after the footer parses, so a chain that dies in the footer parse never
    reaches it. This builds a list-typed field whose elements are structs,
    nested repeatedly, so the parse has to descend a schema chain rather than
    skip an unknown one.
    """
    depth = rnd.choice((5000, 50000, 200000))
    # field 1, type struct, repeated: each level re-opens field 1 of its parent.
    unit = b"\x1c"
    out = bytearray(b"PAR1")
    body = unit * depth + b"\x00" * min(depth, 4096)
    out += body
    out += struct.pack("<I", len(body))
    out += b"PAR1"
    b[:] = out


MUTATIONS = [
    (_flip_bits, 3),
    (_extreme_int, 4),
    (_varint_extreme, 4),
    (_truncate, 2),
    (_footer_len, 3),
    (_magic, 1),
    (_splice, 3),
    (_delete, 2),
    (_deep_nest, 2),
    (_deep_schema, 2),
]


def mutate(data, seed):
    rnd = random.Random(seed)
    b = bytearray(data)
    pool = []
    for fn, weight in MUTATIONS:
        pool.extend([fn] * weight)
    for _ in range(rnd.randint(1, 3)):
        rnd.choice(pool)(b, rnd)
    return bytes(b)


def schema_span(b):
    """-> (start, end) of the footer's schema list (FileMetaData field 2), as byte
    offsets into the whole file. A plain Thrift compact walk; the seeds are valid."""
    flen = struct.unpack("<I", bytes(b[-8:-4]))[0]
    st = {"pos": len(b) - 8 - flen}

    def byte():
        v = b[st["pos"]]
        st["pos"] += 1
        return v

    def uvar():
        shift = out = 0
        while True:
            v = byte()
            out |= (v & 0x7F) << shift
            if not v & 0x80:
                return out
            shift += 7

    def lhdr():
        v = byte()
        n = (v >> 4) & 0x0F
        if n == 0x0F:
            n = uvar()
        return n, v & 0x0F

    def fhdr(last):
        v = byte()
        if v == 0:
            return 0, 0
        t, d = v & 0x0F, v >> 4
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
        start = st["pos"]
        ft, fid = fhdr(last)
        if ft == 0:
            raise ValueError("no schema list in the footer")
        last = fid
        skip(ft)
        if fid == 2:
            return start, st["pos"]


def schema_sweep(src, outdir):
    """The sweep that found #1855, narrowed to the schema: one copy of SRC per byte of
    the footer's schema list and per low-nibble value 0..15, skipping the copy that
    changes nothing. Writes OUTDIR/sweep-<offset>-<nibble>.parquet, prints each path."""
    with open(src, "rb") as f:
        data = f.read()
    start, end = schema_span(data)
    os.makedirs(outdir, exist_ok=True)
    for off in range(start, end):
        for nib in range(16):
            v = (data[off] & 0xF0) | nib
            if v == data[off]:
                continue
            b = bytearray(data)
            b[off] = v
            path = os.path.join(outdir, "sweep-%d-%d.parquet" % (off, nib))
            with open(path, "wb") as f:
                f.write(bytes(b))
            os.chmod(path, 0o644)
            print(path)


def main():
    if sys.argv[1] == "--schema-sweep":
        schema_sweep(sys.argv[2], sys.argv[3])
        return
    src, seed, dst = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    with open(src, "rb") as f:
        data = f.read()
    out = mutate(data, seed)
    with open(dst, "wb") as f:
        f.write(out)
    os.chmod(dst, 0o644)


if __name__ == "__main__":
    main()
