"""An encoding is chosen pre-codec and stored post-codec (#1132).

`PgColumnarEncodeChunk` picks the smallest candidate against `bestLen`, which
starts at `rawLen`, and every comparison is on UNCOMPRESSED bytes. The block
codec runs afterwards, once, over the whole encoded region. So an encoding that
shrinks the bytes can still ENLARGE the stored chunk, because bit-packing
whitens a stream the codec was exploiting.

FSST already decides post-codec through `PgColumnarFsstHelpsCompressed`. This
is the same question asked of the encoders that had no such gate.

Independent of `test/encode_post_codec.sh` per CONTEXT.md: same public seams --
the encoding descriptor and `column_chunk.page_length` -- but its own cluster,
its own table names, its own corpus constants, and the descriptor decoded here
in Python rather than through `get_byte()` in SQL. The two agree on the
property, not on the implementation.

THE FIXTURE IS THE ARGUMENT. Measured on ClickBench `hits_0.parquet`, the worst
column was `ClientEventTime`: stored 2.06x larger encoded than raw. Its shape is
a heavy tail -- rare outliers stretch the range while the typical value stays in
a narrow band -- which splits the two cost models exactly:

  * FOR prices by RANGE, so every value is sized for the outliers;
  * zstd prices by BYTE REDUNDANCY, and the typical value's high bytes are
    constant, so it compresses what FOR spent bits on.

Repetition alone does NOT reproduce it (measured 0.71x, encoding winning), which
is why the first premise asserts the tail rather than assuming it.
"""

ROWS = 120000
BAND = 4000              # width of the band the typical value sits in
TAIL_ONE_IN = 1000       # one row in this many is an outlier
TAIL_SPAN = 1700000000   # how far the outliers reach back

NONE_ENCODING_TYPE = 0


def _descriptor_encodings(cur, table):
    """Encoding type of every vector of column 0, read from the descriptor.

    6-byte header -- version, a flags byte (a reserved zero before #1130), then
    the vector count as uint32 little-endian -- followed by that many 13-byte
    entries whose first byte is the encoding type. The count bounds the scan:
    reading to the descriptor's length would score the trailing shared-table
    region as encoding types.
    """
    cur.execute(
        """
        SELECT c.encoding_descriptor
          FROM pgcolumnar.column_chunk c
          JOIN pgcolumnar.storage s ON s.storage_id = c.storage_id
         WHERE s.relation_oid = %s::regclass
           AND c.column_index = 0
        """,
        (table,),
    )
    out = []
    for (desc,) in cur.fetchall():
        blob = bytes(desc)
        if len(blob) < 6:
            continue
        count = int.from_bytes(blob[2:6], "little")
        for i in range(count):
            at = 6 + i * 13
            if at < len(blob):
                out.append(blob[at])
    return out


def _value_bytes(cur, table):
    """Stored bytes of the value stream: pages minus the validity bitmap.

    The bitmap is one bit per row and is written raw ahead of the codec, so it
    is subtracted to leave a number that moves only with the encoding decision.

    SUBTRACTED ONLY WHERE THERE IS ONE (#1130). A chunk holding no null stores no
    bitmap and sets bit 0 of the descriptor's flags byte; subtracting
    unconditionally would remove bytes that were never written. These fixtures
    hold no nulls, so every chunk takes the zero branch today -- the condition is
    here so a fixture that gains one cannot silently change what is measured.
    """
    cur.execute(
        """
        SELECT coalesce(sum(c.page_length) - sum(
                 CASE WHEN octet_length(c.encoding_descriptor) >= 6
                       AND (get_byte(c.encoding_descriptor, 1) & 1) = 1
                      THEN 0 ELSE (c.value_count + 7) / 8 END), 0)
          FROM pgcolumnar.column_chunk c
          JOIN pgcolumnar.storage s ON s.storage_id = c.storage_id
         WHERE s.relation_oid = %s::regclass
           AND c.column_index = 0
        """,
        (table,),
    )
    return int(cur.fetchone()[0])


def _load(cur, table, expr):
    cur.execute(f"DROP TABLE IF EXISTS {table}")
    cur.execute(f"DROP TABLE IF EXISTS {table}_h")
    cur.execute("SET pgcolumnar.compression = 'zstd'")
    cur.execute("SELECT setseed(0.11)")
    cur.execute(f"CREATE TABLE {table} (v bigint) USING pgcolumnar")
    cur.execute(
        f"INSERT INTO {table} SELECT {expr} FROM generate_series(1, {ROWS}) g"
    )
    # THE HEAP MIRROR IS WRITTEN FROM THE SAME SEEDED GENERATOR, NOT COPIED (#1339):
    # a copy reads through the decoder under test, so a decode defect lands on both
    # sides. Reseeded so random() repeats the columnar table's sequence.
    cur.execute(f"CREATE TABLE {table}_h (v bigint)")
    cur.execute("SELECT setseed(0.11)")
    cur.execute(
        f"INSERT INTO {table}_h SELECT {expr} FROM generate_series(1, {ROWS}) g"
    )


TAIL_EXPR = (
    f"CASE WHEN random() < {1.0 / TAIL_ONE_IN} "
    f"THEN 31525449 + floor(random() * {TAIL_SPAN})::bigint "
    f"ELSE 1739000000 + floor(random() * {BAND})::bigint END"
)
REP_EXPR = "1700000000 + (g % 86400)::bigint"


def test_a_chunk_is_never_stored_larger_than_unencoded(pgc_conn, expect):
    """The arm, with the control that stops it being satisfied by giving up.

    Storing the chunk unencoded is always available, so no encoding decision may
    land above it. Declining every encoding would satisfy that and redden
    nothing, so the repeating column ships beside the tail one.
    """
    with pgc_conn.cursor() as c:
        _load(c, "pc_tail", TAIL_EXPR)
        _load(c, "pc_rep", REP_EXPR)

        c.execute("SELECT max(v) - min(v) FROM pc_tail")
        rng = int(c.fetchone()[0])
        # 1st to 99th percentile, NOT 0.1st to 99.9th. One row in a thousand is
        # an outlier, so a 99.9th percentile sits exactly ON the boundary and
        # reads either the band width or the full range depending on whether the
        # draw produced a hair more or fewer outliers than expected. Measured
        # both ways from one seed: 4,994 and 53,786,536.
        c.execute(
            "SELECT percentile_disc(0.99) WITHIN GROUP (ORDER BY v)"
            "     - percentile_disc(0.01) WITHIN GROUP (ORDER BY v) FROM pc_tail"
        )
        spread = int(c.fetchone()[0])

        expect.text(
            "tail-shaped" if rng > 1000000000 and spread < 100000
            else f"range {rng} (needs > 1e9), spread {spread} (needs < 1e5)",
            "tail-shaped",
            "premise: the tail fixture's range is set by outliers, not by its typical value",
        )

        c.execute("SELECT (SELECT count(*) FROM pc_tail), (SELECT count(*) FROM pc_rep)")
        n_tail, n_rep = c.fetchone()
        expect.text(
            f"{n_tail}/{n_rep}", f"{ROWS}/{ROWS}",
            "premise: both fixtures hold every row",
        )

        raw = ROWS * 8
        tail_bytes = _value_bytes(c, "pc_tail")
        rep_bytes = _value_bytes(c, "pc_rep")

        expect.text(
            "not-inflated" if tail_bytes <= raw * 0.35
            else f"{tail_bytes} bytes over the {raw * 0.35:.0f} ceiling (raw {raw})",
            "not-inflated",
            "a chunk is not stored larger than it would be with no encoding at all",
        )

        rep_encodings = _descriptor_encodings(c, "pc_rep")
        expect.text(
            "encoded" if any(e != NONE_ENCODING_TYPE for e in rep_encodings) else "declined",
            "encoded",
            "while a column where encoding genuinely wins still encodes",
        )
        expect.text(
            "small" if rep_bytes <= raw * 0.10
            else f"{rep_bytes} bytes over the {raw * 0.10:.0f} ceiling (raw {raw})",
            "small",
            "and that column stays far below the no-encoding size, so the win is real",
        )


def test_the_decision_never_changes_what_comes_back_out(pgc_conn, expect):
    """The invariant the size arms exist to prove is not vacuous.

    A decision that changes the rows is a data-loss bug, not a size regression,
    and swapping the descriptor for the chunk is exactly where that would go
    wrong.
    """
    with pgc_conn.cursor() as c:
        _load(c, "pc_tail", TAIL_EXPR)
        _load(c, "pc_rep", REP_EXPR)

        for label, tbl in (("tail", "pc_tail"), ("rep", "pc_rep")):
            c.execute(
                f"SELECT count(*) FROM (SELECT v FROM {tbl} "
                f"EXCEPT ALL SELECT v FROM {tbl}_h) d"
            )
            expect.num(
                int(c.fetchone()[0]), 0,
                f"the {label} column reads back exactly what the heap holds",
            )
            c.execute(
                f"SELECT count(*) FROM (SELECT v FROM {tbl}_h "
                f"EXCEPT ALL SELECT v FROM {tbl}) d"
            )
            expect.num(
                int(c.fetchone()[0]), 0,
                f"the {label} column holds no row the heap does not",
            )
            c.execute(f"SELECT coalesce(sum(v), 0) FROM {tbl}")
            got = int(c.fetchone()[0])
            c.execute(f"SELECT coalesce(sum(v), 0) FROM {tbl}_h")
            want = int(c.fetchone()[0])
            expect.num(got, want, f"the {label} column preserves its checksum")
