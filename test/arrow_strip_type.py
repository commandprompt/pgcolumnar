#!/usr/bin/env python3
#
# Remove one Arrow field's `type` table from an Arrow IPC stream, keeping its
# `type_type` tag (#2186).
#
# import_arrow once checked a field's tag against the target column only when
# the field had a type table. A file whose tag says FloatingPoint but which has
# no table passed no check, so its float64 bits imported into a bigint column.
# pyarrow never writes such a file, so the suites build one from a pyarrow file
# with this helper.
#
# How it works: the first message of a stream is the Schema, framed as a
# 0xFFFFFFFF continuation, a 4-byte metadata length and a FlatBuffer. The helper
# walks Message -> Schema -> fields -> Field (and Field.children for a path
# longer than one) to the chosen Field table. A FlatBuffers table finds its
# fields through a vtable, and a slot whose vtable entry is 0 is absent. The
# helper copies that Field's vtable to the end of the Schema metadata, zeroes
# slot 3 (`type`) in the copy, and points the Field at the copy. It never edits
# a vtable in place, because pyarrow shares one vtable between identical
# tables, and an edit there would strip the type of other fields too. The
# metadata length grows by the padded copy. The Schema message has no body, so
# nothing else moves.
#
# The helper then reads the result back and reports, for the chosen field, its
# tag and whether a type table is still present. A caller compares that report
# with what it expects, so a patch that missed is caught by the check itself.
#
# Usage:  python3 test/arrow_strip_type.py IN OUT PATH
#         PATH is field indices joined by '.', such as 0 (the first top-level
#         field) or 0.0 (the first child of the first field).
# Prints: "tag=<type_type> type=<present|absent>" for the chosen field.
#
import struct
import sys

CONT = b"\xff\xff\xff\xff"
FIELD_TYPE_TYPE, FIELD_TYPE, FIELD_CHILDREN = 2, 3, 5


def _u16(b, p):
    return struct.unpack_from("<H", b, p)[0]


def _i32(b, p):
    return struct.unpack_from("<i", b, p)[0]


def _u32(b, p):
    return struct.unpack_from("<I", b, p)[0]


def _vtable(b, tab):
    return tab - _i32(b, tab)


def _slot(b, tab, i):
    """-> the position of field `i` of the table at `tab`, or 0 when absent."""
    vt = _vtable(b, tab)
    size = _u16(b, vt)
    off = 4 + 2 * i
    if off >= size:
        return 0
    v = _u16(b, vt + off)
    return tab + v if v else 0


def _deref(b, p):
    return p + _u32(b, p)


def _field(meta, path):
    """-> the position of the Field table that `path` names, in `meta`."""
    msg = _deref(meta, 0)
    schema = _deref(meta, _slot(meta, msg, 2))          # Message.header
    vec = _deref(meta, _slot(meta, schema, 1))          # Schema.fields
    for depth, idx in enumerate(path):
        if idx >= _u32(meta, vec):
            raise SystemExit("arrow_strip_type: no field %r" % (path,))
        tab = _deref(meta, vec + 4 + 4 * idx)
        if depth + 1 < len(path):
            vec = _deref(meta, _slot(meta, tab, FIELD_CHILDREN))
    return tab


def _schema_frame(data):
    """-> (start of the metadata, its length) for the stream's first message."""
    if data[:4] != CONT:
        raise SystemExit("arrow_strip_type: not an Arrow IPC stream")
    return 8, _i32(data, 4)


def describe(data, path):
    """-> 'tag=<type_type> type=<present|absent>' for the field `path` names."""
    start, mlen = _schema_frame(data)
    meta = data[start:start + mlen]
    tab = _field(meta, path)
    tp = _slot(meta, tab, FIELD_TYPE_TYPE)
    tag = meta[tp] if tp else 0
    return "tag=%d type=%s" % (tag, "present" if _slot(meta, tab, FIELD_TYPE) else "absent")


def strip(data, path):
    """-> `data` with the `type` table of the field `path` names removed."""
    start, mlen = _schema_frame(data)
    meta = bytearray(data[start:start + mlen])
    tab = _field(meta, path)
    vt = _vtable(meta, tab)
    size = _u16(meta, vt)
    copy = bytearray(meta[vt:vt + size])
    if 4 + 2 * FIELD_TYPE < size:
        struct.pack_into("<H", copy, 4 + 2 * FIELD_TYPE, 0)
    # the copy goes at the end, 8-byte aligned, as Arrow pads metadata
    newvt = len(meta)
    meta += copy
    meta += b"\0" * (-len(meta) % 8)
    struct.pack_into("<i", meta, tab, tab - newvt)
    out = bytearray(data[:start]) + meta + data[start + mlen:]
    struct.pack_into("<i", out, 4, len(meta))
    return bytes(out)


def main():
    if len(sys.argv) != 4:
        raise SystemExit("usage: arrow_strip_type.py IN OUT PATH")
    path = [int(x) for x in sys.argv[3].split(".")]
    with open(sys.argv[1], "rb") as f:
        data = strip(f.read(), path)
    with open(sys.argv[2], "wb") as f:
        f.write(data)
    print(describe(data, path))


if __name__ == "__main__":
    main()
