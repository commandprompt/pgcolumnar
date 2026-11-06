"""`pgcolumnar.compression` changes which lightweight encodings a chunk gets (#1076).

The setting reads as a codec choice, and `docs/` said so: "default codec for new
chunks". It is not only that. Whether FSST is kept for a column chunk is decided by
comparing the two candidates AFTER the configured codec has run, because what
reaches disk is the encoded stream compressed -- so the decision reads the GUC. With
`none` there is no codec to compare through and the chunk-level test is skipped
entirely, keeping FSST whenever it was built.

So `none` is a DIFFERENT cascade, not the same cascade with compression removed, and
the coupling is invisible from the setting's name.

Public seam: `pgcolumnar.column_chunk.encoding_descriptor`, which records the
encoding each vector actually took. This session builds its own marginal corpus and
parses the descriptor itself; it does not share a corpus, a helper or a byte layout
with `fsst_margin.sh`, which asserts the same property through its own.

THE DIFFERENTIAL IS THE POINT. A single "FSST is kept under none" arm is satisfied
by any corpus FSST always wins on, which would make it decorative. Each arm here is
paired with the same corpus at the same margin WITH a codec, where FSST is dropped,
so the only variable between the two loads is the setting under test.
"""


ROWS = 20000

# High-entropy hex. FSST builds a symbol table that barely pays, which is the
# marginal case the margin exists to decline -- and therefore the case where the
# margin's verdict and the codec's verdict can disagree. A corpus FSST wins or loses
# outright cannot show the coupling at all.
CORPUS = "md5(g::text) || md5((g * 7)::text)"

FSST_ENCODING_TYPE = 8


def _fsst_vectors(cur, table):
    """How many vectors of column 1 chose FSST, read from the descriptor.

    The descriptor is a 6-byte header -- version, a flags byte (#1130; a
    reserved zero before it), then the vector
    count as uint32 little-endian -- followed by that many 13-byte entries whose
    first byte is the encoding type. Reading past the entries would score the
    chunk's shared symbol table bytes as encoding types, so the count bounds the
    scan rather than the descriptor's length.
    """
    cur.execute(
        """
        SELECT c.encoding_descriptor
          FROM pgcolumnar.column_chunk c
          JOIN pgcolumnar.storage s ON s.storage_id = c.storage_id
         WHERE s.relation_oid = %s::regclass
           AND c.column_index = 1
        """,
        (table,),
    )
    total = 0
    for (desc,) in cur.fetchall():
        blob = bytes(desc)
        if len(blob) < 6:
            continue
        count = int.from_bytes(blob[2:6], "little")
        for i in range(count):
            at = 6 + i * 13
            if at < len(blob) and blob[at] == FSST_ENCODING_TYPE:
                total += 1
    return total


def _load(cur, table, margin, codec=None):
    """Write the same corpus under one margin and one codec."""
    cur.execute(f"DROP TABLE IF EXISTS {table}")
    cur.execute(f"SET pgcolumnar.fsst_min_gain_percent = {margin}")
    if codec is None:
        cur.execute("RESET pgcolumnar.compression")
    else:
        cur.execute(f"SET pgcolumnar.compression = '{codec}'")
    cur.execute(f"CREATE TABLE {table} (id int, v text) USING pgcolumnar")
    cur.execute(
        f"INSERT INTO {table} SELECT g, {CORPUS} FROM generate_series(1, {ROWS}) g"
    )
    return _fsst_vectors(cur, table)


def test_the_codec_setting_decides_whether_fsst_is_kept(pgc_conn, expect):
    """Same corpus, same margin, only the codec differs, and the verdict flips.

    Public seam: the encoding descriptor. The `zstd` arm is the control -- without
    it a keep under `none` says nothing, because a corpus FSST always wins on would
    satisfy it too.
    """
    with pgc_conn.cursor() as c:
        with_codec = _load(c, "cc_zstd", 90, "zstd")
        without = _load(c, "cc_none", 90, "none")

        expect.num(with_codec, 0, "at margin 90 with a codec this corpus drops FSST")
        expect.text(
            "kept" if without > 0 else "dropped",
            "kept",
            "with no codec the same corpus keeps FSST at the same margin",
        )
        expect.text(
            "codec decided" if without > with_codec
            else f"without={without} with_codec={with_codec}",
            "codec decided",
            "so the codec is what changed the decision, not the corpus",
        )


def test_the_margin_is_never_consulted_when_there_is_no_codec(pgc_conn, expect):
    """99 is the maximum the GUC accepts, and it still does not drop FSST.

    If any margin could drop FSST under `none`, this is the one that would. It does
    not, because the keep test returns before it reads the margin -- which is a
    stronger statement than "the margin is outvoted".
    """
    with pgc_conn.cursor() as c:
        capped_with_codec = _load(c, "cc_z99", 99, "zstd")
        capped_without = _load(c, "cc_n99", 99, "none")

        expect.num(capped_with_codec, 0, "at the maximum margin a codec drops FSST")
        expect.text(
            "kept" if capped_without > 0 else "dropped",
            "kept",
            "and at the maximum margin no codec still keeps it, "
            "because the test returns before reading the margin",
        )


def test_the_corpus_is_marginal_rather_than_one_fsst_always_wins(pgc_conn, expect):
    """The condition every arm above rests on, asserted rather than assumed.

    NOT LABELLED A PREMISE (#1339): the keep-or-drop decision is pgcolumnar's own, so
    a change to it reddens these -- with the margin ignored, FSST was kept at 90.

    If FSST won outright on this corpus the margin would never get a say and the
    differentials would be measuring nothing. At margin 0 it is kept and at margin
    90 it is dropped, WITH a codec in both cases, so the corpus sits where the
    decision is actually live.
    """
    with pgc_conn.cursor() as c:
        kept = _load(c, "cc_m0", 0, "zstd")
        dropped = _load(c, "cc_m90", 90, "zstd")

        expect.text(
            "kept" if kept > 0 else "dropped",
            "kept",
            "at margin 0 with a codec this corpus keeps FSST",
        )
        expect.num(dropped, 0, "at margin 90 with a codec it drops FSST")


def test_the_rows_survive_every_combination(pgc_conn, expect):
    """The invariant the arms above exist to keep non-vacuous.

    A cascade change that altered content would be a far worse defect than any
    decision this file pins, so the same rows are read back from every combination
    and compared against a heap mirror that shares no code with the columnar path.
    """
    with pgc_conn.cursor() as c:
        c.execute("DROP TABLE IF EXISTS cc_heap")
        c.execute("CREATE TABLE cc_heap (id int, v text)")
        c.execute(
            f"INSERT INTO cc_heap SELECT g, {CORPUS} FROM generate_series(1, {ROWS}) g"
        )
        c.execute("SELECT md5(string_agg(t::text, '' ORDER BY t::text)) FROM cc_heap t")
        mirror = c.fetchone()[0]
        expect.text(
            "has content" if mirror else "empty",
            "has content",
            "premise: the heap mirror has content to compare against",
        )

        for table, margin, codec in (
            ("cc_v_none", 90, "none"),
            ("cc_v_zstd", 90, "zstd"),
            ("cc_v_zero", 0, "zstd"),
        ):
            _load(c, table, margin, codec)
            c.execute(
                f"SELECT md5(string_agg(t::text, '' ORDER BY t::text)) FROM {table} t"
            )
            got = c.fetchone()[0]
            expect.hash(got, mirror, f"{table} reads back exactly what the heap holds")
