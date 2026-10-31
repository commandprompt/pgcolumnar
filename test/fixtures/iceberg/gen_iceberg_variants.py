#!/usr/bin/env python3
"""Derive variants of the committed `warehouse` Iceberg table at test time (#1815, #1825).

Usage:  gen_iceberg_variants.py <dir holding a copy of warehouse/db>

The suites copy test/fixtures/iceberg/warehouse/db to a work directory, then run
this script on that copy. It writes the variants next to the table and prints one
`name=path` line per variant, for the suite to read. It needs only the Python
standard library, so it runs wherever the suites run.

The variants:

  gz_infix    The current metadata.json, gzip-compressed, named *.gz.metadata.json.
              Java Iceberg writes this name when write.metadata.compression-codec
              is gzip (#1815).
  gz_suffix   The same bytes, named *.metadata.json.gz, as older writers name it.
  gz_bomb     A gzip file that inflates to 64 MiB + 1 byte: one byte past the
              reader's metadata cap.
  gz_trunc    The first half of gz_infix: a gzip stream cut short.
  lower       A copy of the table (db/lower) whose manifest records the data
              files' file_format as "parquet", the spelling the Iceberg spec uses
              (#1825).
  mixed       A copy of the table (db/mixed) whose metadata records the location
              as s3://bucket77/pgc_ice/db/events while the manifest list and the
              manifests record every path as s3a://bucket77/pgc_ice/... (#1825).

The two copied tables keep their own metadata, so a suite reads each one by its
own metadata path. The Avro rewrite keeps every string the same length, so no
length prefix inside a record changes.
"""
import glob
import gzip
import json
import os
import shutil
import sys
import zlib

RECORDED = "file:///tmp/pgc_ice_wh"
# the same length as RECORDED, so a byte-for-byte swap keeps every Avro length prefix
S3A_ROOT = "s3a://bucket77/pgc_ice"
S3_ROOT = "s3://bucket77/pgc_ice"
CAP = 64 * 1024 * 1024
assert len(RECORDED) == len(S3A_ROOT)


def _read_long(b, i):
    shift = acc = 0
    while True:
        c = b[i]
        i += 1
        acc |= (c & 0x7F) << shift
        shift += 7
        if not c & 0x80:
            return (acc >> 1) ^ -(acc & 1), i


def _long(n):
    u = (n << 1) ^ (n >> 63)
    out = bytearray()
    while True:
        c = u & 0x7F
        u >>= 7
        out.append(c | 0x80 if u else c)
        if not u:
            return bytes(out)


def rewrite_ocf(path, pairs):
    """Swap each (old, new) byte string inside every data block of an Avro file."""
    b = open(path, "rb").read()
    assert b[:4] == b"Obj\x01", path
    i, meta = 4, {}
    while True:
        n, i = _read_long(b, i)
        if n == 0:
            break
        if n < 0:
            _, i = _read_long(b, i)
            n = -n
        for _ in range(n):
            kl, i = _read_long(b, i)
            k = b[i:i + kl]
            i += kl
            vl, i = _read_long(b, i)
            meta[k] = b[i:i + vl]
            i += vl
    sync = b[i:i + 16]
    i += 16
    codec = meta.get(b"avro.codec", b"null")
    out = bytearray(b[:i])
    swapped = 0
    while i < len(b):
        count, i = _read_long(b, i)
        size, i = _read_long(b, i)
        data = b[i:i + size]
        i += size
        assert b[i:i + 16] == sync, path
        i += 16
        raw = zlib.decompress(data, -15) if codec == b"deflate" else data
        for old, new in pairs:
            assert len(old) == len(new)
            swapped += raw.count(old)
            raw = raw.replace(old, new)
        if codec == b"deflate":
            c = zlib.compressobj(6, zlib.DEFLATED, -15)
            data = c.compress(raw) + c.flush()
        else:
            data = raw
        out += _long(count) + _long(len(data)) + data + sync
    open(path, "wb").write(bytes(out))
    return swapped


def current_metadata(table):
    """The writer's newest NNNNN-<uuid>.metadata.json, never a variant a suite added."""
    names = [p for p in glob.glob(os.path.join(table, "metadata", "[0-9]*-*.metadata.json"))
             if not p.endswith(".gz.metadata.json")]
    return sorted(names)[-1]


def main(root):
    db = os.path.join(root, "db")
    events = os.path.join(db, "events")
    md = current_metadata(events)
    body = open(md, "rb").read()
    base = md[:-len(".metadata.json")]
    out = {}

    # lower: the data file's file_format spelled as the spec spells it
    lower = os.path.join(db, "lower")
    shutil.copytree(events, lower)
    m = json.load(open(current_metadata(lower)))
    snap = next(s for s in m["snapshots"] if s["snapshot-id"] == m["current-snapshot-id"])
    ml = snap["manifest-list"].replace(RECORDED + "/db/events", lower)
    n = 0
    for mf in glob.glob(os.path.join(lower, "metadata", "*-m*.avro")):
        n += rewrite_ocf(mf, [(b"PARQUET", b"parquet")])
    if n == 0:
        sys.exit("gen_iceberg_variants: no PARQUET file_format found to lowercase")
    assert os.path.exists(ml), ml
    out["lower"] = current_metadata(lower)

    # mixed: location s3://, every recorded path s3a://
    mixed = os.path.join(db, "mixed")
    shutil.copytree(events, mixed)
    for f in glob.glob(os.path.join(mixed, "metadata", "*.avro")):
        if rewrite_ocf(f, [(RECORDED.encode(), S3A_ROOT.encode())]) == 0:
            sys.exit(f"gen_iceberg_variants: no recorded path found in {f}")
    mmd = current_metadata(mixed)
    text = open(mmd).read().replace(RECORDED, S3A_ROOT)
    m = json.loads(text)
    m["location"] = S3_ROOT + "/db/events"
    open(mmd, "w").write(json.dumps(m))
    out["mixed"] = mmd

    # the gzip files last: they sit in events/metadata, which the copies above copy
    out["gz_infix"] = base + ".gz.metadata.json"
    out["gz_suffix"] = base + ".metadata.json.gz"
    for name in ("gz_infix", "gz_suffix"):
        open(out[name], "wb").write(gzip.compress(body))
    out["gz_bomb"] = os.path.join(events, "metadata", "bomb.gz.metadata.json")
    open(out["gz_bomb"], "wb").write(gzip.compress(b" " * (CAP + 1)))
    out["gz_trunc"] = os.path.join(events, "metadata", "trunc.gz.metadata.json")
    whole = open(out["gz_infix"], "rb").read()
    open(out["gz_trunc"], "wb").write(whole[:len(whole) // 2])

    for p in out.values():
        os.chmod(p, 0o644)
    for k, v in out.items():
        print(f"{k}={v}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
