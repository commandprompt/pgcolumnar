#!/usr/bin/env python3
#
# Seed corpus for the Arrow IPC decode fuzzer (test/fuzz_arrow.sh, issue #214).
#
# The sibling of test/parquet_corpus.py, for the other hand-rolled parser. Every
# file here is a VALID Arrow IPC *stream* (as pgcolumnar.export_arrow writes and
# pgcolumnar.import_arrow reads): a Schema message, one or more RecordBatch
# messages, and an end-of-stream marker, each message being a 0xFFFFFFFF
# continuation, a 4-byte little-endian metadata length, a FlatBuffers metadata
# block, and a body. The fuzzer mutates copies; a seed that is already malformed
# teaches the mutator nothing because the reader rejects it before the decode.
#
# import_arrow accepts a flat schema only -- int2/4/8, float4/8, bool, text
# (Utf8), bytea (Binary); any other column type is rejected -- so the corpus is
# flat by design. Breadth here is over the physical decoders the FlatBuffers
# metadata dispatches to (each width, the validity/offset/data buffer triple, the
# null and multi-batch shapes), because a mutation is only as interesting as the
# path the surrounding bytes reach.
#
# Each file's line carries the PostgreSQL column list that matches its schema, so
# the driver can build a target table without a schema-introspection function
# (Arrow has no parquet_schema equivalent).
#
# Usage:  python3 test/arrow_corpus.py OUTDIR
# Writes OUTDIR/<name>.arrows and prints one "<name> <path> <coldef>" line each.
#
import os
import sys

import pyarrow as pa
import pyarrow.ipc as ipc

# Arrow array type -> the PostgreSQL column type import_arrow maps it back to.
PGTYPE = {
    "int16": "int2",
    "int32": "int4",
    "int64": "int8",
    "float": "float4",
    "double": "float8",
    "bool": "bool",
    "string": "text",
    "binary": "bytea",
    # Temporal carriers. Both Date widths and both Time widths map to one
    # PostgreSQL type each, so the file's declared unit is the only thing that
    # says how wide a value is -- which is what the mutations here get to attack.
    "date32[day]": "date",
    "date64[ms]": "date",
    "time32[s]": "time",
    "time32[ms]": "time",
    "time64[us]": "time",
    "time64[ns]": "time",
    "timestamp[s]": "timestamp",
    "timestamp[ms]": "timestamp",
    "timestamp[us]": "timestamp",
    "timestamp[ns]": "timestamp",
}


def coldef(schema):
    """The PostgreSQL column list matching an Arrow schema, or None if any field
    is a type import_arrow does not accept (that seed would only exercise the
    schema-mismatch check, never the decode)."""
    parts = []
    for f in schema:
        t = str(f.type)
        pg = PGTYPE.get(t)
        if pg is None:
            return None
        parts.append("%s %s" % (f.name, pg))
    return ", ".join(parts)


def w(outdir, name, batches, cols=None):
    """Write one stream seed from a list of RecordBatches (>1 exercises the
    multi-batch read loop) and report it with its column list. Pass cols
    explicitly for list and struct schemas, whose PostgreSQL type (an array or a
    named composite) cannot be derived from the Arrow schema alone."""
    if not batches:
        return
    schema = batches[0].schema
    if cols is None:
        cols = coldef(schema)
    if cols is None:
        print("SKIP %s (unmapped type)" % name, file=sys.stderr)
        return
    path = os.path.join(outdir, name + ".arrows")
    try:
        with ipc.new_stream(pa.OSFile(path, "wb"), schema) as writer:
            for b in batches:
                writer.write_batch(b)
    except Exception as e:                  # noqa: BLE001 - report and continue
        print("SKIP %s (%s)" % (name, type(e).__name__), file=sys.stderr)
        return
    print("%s %s %s" % (name, path, cols))


def batch(cols):
    return pa.record_batch({k: v for k, v in cols})


def main():
    outdir = sys.argv[1]
    os.makedirs(outdir, exist_ok=True)
    n = 200

    # --- one file per accepted physical type: a mutation lands in a decoder that
    # is actually reached rather than in a type the file never uses.
    w(outdir, "int16", [batch([("c0", pa.array(range(n), pa.int16()))])])
    w(outdir, "int32", [batch([("c0", pa.array(range(n), pa.int32()))])])
    w(outdir, "int64", [batch([("c0", pa.array(range(n), pa.int64()))])])
    w(outdir, "float", [batch([("c0", pa.array([i * 0.5 for i in range(n)], pa.float32()))])])
    w(outdir, "double", [batch([("c0", pa.array([i * 0.25 for i in range(n)], pa.float64()))])])
    w(outdir, "bool", [batch([("c0", pa.array([i % 2 == 0 for i in range(n)], pa.bool_()))])])
    w(outdir, "string", [batch([("c0", pa.array(["v%d" % i for i in range(n)]))])])
    w(outdir, "binary", [batch([("c0", pa.array([b"\x00\x01%d" % i for i in range(n)], pa.binary()))])])

    # --- temporal carriers and units. The decoder takes its stride and its
    # scale factor from the file's own Date/Time/Timestamp tables, so a mutation
    # in those bytes reaches arithmetic no other seed exercises. Without these
    # the unit-aware decode path was unreachable from this corpus entirely.
    S2000 = 946684800
    NOON = 12 * 3600
    w(outdir, "date32", [batch([("c0", pa.array(
        [S2000 // 86400 + i for i in range(n)], pa.date32()))])])
    w(outdir, "date64", [batch([("c0", pa.array(
        [(S2000 + i * 86400) * 1000 for i in range(n)], pa.date64()))])])
    w(outdir, "time32_s", [batch([("c0", pa.array(
        [(NOON + i) % 86400 for i in range(n)], pa.time32("s")))])])
    w(outdir, "time32_ms", [batch([("c0", pa.array(
        [((NOON + i) % 86400) * 1000 for i in range(n)], pa.time32("ms")))])])
    w(outdir, "time64_us", [batch([("c0", pa.array(
        [((NOON + i) % 86400) * 10**6 for i in range(n)], pa.time64("us")))])])
    w(outdir, "time64_ns", [batch([("c0", pa.array(
        [((NOON + i) % 86400) * 10**9 for i in range(n)], pa.time64("ns")))])])
    w(outdir, "ts_s", [batch([("c0", pa.array(
        [S2000 + i for i in range(n)], pa.timestamp("s")))])])
    w(outdir, "ts_ms", [batch([("c0", pa.array(
        [(S2000 + i) * 1000 for i in range(n)], pa.timestamp("ms")))])])
    w(outdir, "ts_us", [batch([("c0", pa.array(
        [(S2000 + i) * 10**6 for i in range(n)], pa.timestamp("us")))])])
    w(outdir, "ts_ns", [batch([("c0", pa.array(
        [(S2000 + i) * 10**9 for i in range(n)], pa.timestamp("ns")))])])

    # --- null shapes: the validity bitmap is a separate buffer and a separate
    # decode path from the values.
    w(outdir, "nulls_some", [batch([("c0", pa.array(
        [None if i % 3 == 0 else i for i in range(n)], pa.int32()))])])
    w(outdir, "nulls_all", [batch([("c0", pa.array([None] * n, pa.int32()))])])
    w(outdir, "string_nulls", [batch([("c0", pa.array(
        [None if i % 2 else "s%d" % i for i in range(n)]))])])

    # --- varlena offsets: the string/binary decoder trusts an offsets buffer and
    # a data buffer whose lengths have to agree. Empty strings exercise zero-width
    # offset runs.
    w(outdir, "string_empty", [batch([("c0", pa.array(["" for _ in range(n)]))])])

    # --- the widest table import_arrow accepts (16 columns), so the schema field
    # vector and the per-column buffer set are long.
    wide = [("c%d" % c, pa.array(range(50), pa.int32())) for c in range(16)]
    w(outdir, "wide16", [batch(wide)])

    # --- several record batches in one stream: the read loop advances message to
    # message, and a length or continuation error mid-stream is its own case.
    three = [batch([("c0", pa.array(range(i * 100, i * 100 + 100), pa.int64())),
                    ("c1", pa.array(["b%d" % j for j in range(100)]))]) for i in range(3)]
    w(outdir, "multibatch", three)

    # --- an empty batch still has a schema message and a record-batch message
    # with zero-length buffers.
    w(outdir, "empty", [batch([("c0", pa.array([], pa.int32()))])])

    # --- mixed columns so the per-column buffer offsets in the record batch have
    # to be walked in order rather than assumed uniform.
    w(outdir, "mixed", [batch([
        ("a", pa.array(range(n), pa.int64())),
        ("b", pa.array([i * 0.5 for i in range(n)], pa.float64())),
        ("c", pa.array(["m%d" % (i % 7) for i in range(n)])),
        ("d", pa.array([i % 2 == 0 for i in range(n)], pa.bool_())),
    ])])

    # --- list and struct: the recursive decode paths. import_arrow builds an
    # A_LIST node for an array-typed target column and an A_STRUCT node for a
    # composite one, recursively, and decodes with offset-driven child reads --
    # the higher-risk shape a flat corpus never reaches. Their PostgreSQL types
    # are passed explicitly; the driver pre-creates the composite type the struct
    # seeds name (pgc_fuzz_xy) before it builds the target tables.
    w(outdir, "list_int", [batch([("a", pa.array(
        [[i, i + 1] for i in range(n)], pa.list_(pa.int32())))])], cols="a int4[]")
    w(outdir, "list_text", [batch([("a", pa.array(
        [["k%d" % (i % 5), "v%d" % i] for i in range(n)], pa.list_(pa.string())))])],
      cols="a text[]")
    w(outdir, "list_nulls", [batch([("a", pa.array(
        [None if i % 4 == 0 else [i, None, i + 1] for i in range(n)],
        pa.list_(pa.int32())))])], cols="a int4[]")
    w(outdir, "list_empty", [batch([("a", pa.array(
        [[] for _ in range(n)], pa.list_(pa.int32())))])], cols="a int4[]")

    xy = pa.struct([("x", pa.int32()), ("y", pa.string())])
    w(outdir, "struct_xy", [batch([("a", pa.array(
        [{"x": i, "y": "s%d" % i} for i in range(n)], xy))])],
      cols="a pgc_fuzz_xy")
    w(outdir, "struct_nulls", [batch([("a", pa.array(
        [None if i % 3 == 0 else {"x": i, "y": None} for i in range(n)], xy))])],
      cols="a pgc_fuzz_xy")
    # list of struct: both recursions at once (offsets into a struct child).
    w(outdir, "list_struct", [batch([("a", pa.array(
        [[{"x": i, "y": "e%d" % i}] for i in range(n)], pa.list_(xy)))])],
      cols="a pgc_fuzz_xy[]")


if __name__ == "__main__":
    main()
