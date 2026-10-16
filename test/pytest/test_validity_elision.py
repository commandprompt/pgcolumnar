"""A column with no nulls must not store a validity bitmap (#1130).

`flush_one_column` builds the page as `[validity][finalData]`. The bitmap is one
bit per row, allocated unconditionally and appended RAW, ahead of the block
codec, which therefore never sees it -- so a column that holds no null at all
still stored `ceil(rows / 8)` bytes of `0xFF` for ever.

Measured on ClickBench `hits_0.parquet` (1,000,000 rows, 105 columns, no null in
any of them): 16.80% of everything stored. On a column that encodes well it
dominates the page -- 99.5% on a sorted bigint, where the values themselves came
to 135 bytes.

THE MEASUREMENT IS EXACT, not a ratio. With the block codec off the page is
exactly `[validity][encoded]`, so

    page_length - sum(encLen over the chunk's vectors)

IS the bitmap, with nothing else in it. So the arms below assert 0 and
`ceil(rows / 8)`, and a wrong answer cannot hide inside a tolerance.

Independent of `test/validity_elision.sh` per CONTEXT.md: the same public seams
-- `pgcolumnar.column_chunk` and the encoding descriptor -- but its own cluster,
its own table names, its own corpus size, and the descriptor decoded here in
Python bytes rather than through `get_byte()` in SQL. The codec is turned off
here with a session `SET`, which this harness can rely on because one connection
serves the whole test; the shell twin cannot, and uses `ALTER DATABASE`. The two
agree on the property, not on the implementation.

THE FETCH PATH IS A SECOND READER and it gets its own arms. A scan reaches a
chunk through `pgcolumnar_native_load_group`; an index scan reaches a row
through `pgcolumnar_fetch_get_row`, which reads the chunk's bytes itself.
Elision makes that path MORE likely for the columns it helps most: the coalesced
read skips a chunk whose `page_length` is below the group's bitmap size, and
dropping the bitmap is exactly what takes a well-encoded chunk below it. So a
fixture built to exercise the elision is systematically the fixture that lands
on the reader most likely to be left unpatched -- which is what happened here:
the first implementation fixed the scan path only, and 33 suites went red.

TWO ARRANGEMENTS MAKE THE FETCH ARMS REAL, and both were found by an arm that
failed rather than by reasoning:

  * the table has a KEY column beside the measured one, because an index on the
    only column is answered by an Index Only Scan that never calls the table AM;
  * `pgcolumnar.enable_custom_scan` is off, because `enable_seqscan` does not
    govern the columnar custom scan and the planner otherwise answers from
    `Custom Scan (PgColumnarScan)` -- a scan, not a fetch.

The premise arm asserts the plan, not the row, for that reason: a row that comes
back says nothing about which reader produced it.
"""

ROWS = 80000                 # own corpus size; the shell twin uses another
NULL_EVERY = 10              # one row in this many is NULL in the second fixture

HEADER_LEN = 6               # version u8, flags u8, vectorCount u32
ENTRY_LEN = 13               # type u8, valueCount u32, rawLen u32, encLen u32
OFF_ENCLEN = 9               # within an entry

MEASURED_COLUMN = 1          # v, the column whose null-ness differs; 0 is the key
KEY_COLUMN = 0               # k, null-free in both fixtures


def _residual(cur, table, column=MEASURED_COLUMN):
    """`page_length - sum(encLen)` over one column's chunks, and how many.

    With the block codec off that difference IS the validity bitmap. The vector
    count bounds the walk: reading entries to the descriptor's length would
    score the trailing shared-table region as vectors and subtract bytes that
    are not there.

    THE COUNT IS RETURNED BESIDE THE SUM because a sum over nothing is 0, which
    is exactly what the headline arm wants to see. A residual of 0 is evidence
    only together with the number of chunks it was summed over.

    The column is an argument because the property is per CHUNK, and the
    sharpest statement of that is two columns of one row group answering
    differently.
    """
    cur.execute(
        """
        SELECT c.page_length, c.encoding_descriptor
          FROM pgcolumnar.column_chunk c
          JOIN pgcolumnar.storage s ON s.storage_id = c.storage_id
         WHERE s.relation_oid = %s::regclass
           AND c.column_index = %s
        """,
        (table, column),
    )
    total = 0
    chunks = 0
    for page_length, desc in cur.fetchall():
        blob = bytes(desc)
        if len(blob) < HEADER_LEN:
            continue
        chunks += 1
        count = int.from_bytes(blob[2:HEADER_LEN], "little")
        enc = 0
        for i in range(count):
            at = HEADER_LEN + i * ENTRY_LEN + OFF_ENCLEN
            if at + 4 <= len(blob):
                enc += int.from_bytes(blob[at:at + 4], "little")
        total += int(page_length) - enc
    return total, chunks


def _row_groups(cur, table):
    cur.execute(
        """
        SELECT count(*) FROM pgcolumnar.row_group r
          JOIN pgcolumnar.storage s ON s.storage_id = r.storage_id
         WHERE s.relation_oid = %s::regclass
        """,
        (table,),
    )
    return int(cur.fetchone()[0])


def _load(cur):
    """Both fixtures, and their heap mirrors, with the block codec off.

    NO CODEC, deliberately. With one, `page_length` is the COMPRESSED encoded
    region and the subtraction above stops being the bitmap size: the arms would
    then be reading a compression ratio and calling it a bitmap.
    """
    cur.execute("SET pgcolumnar.compression = 'none'")

    cur.execute("CREATE TABLE vb_full (k bigint, v bigint) USING pgcolumnar")
    cur.execute(f"INSERT INTO vb_full SELECT g, g FROM generate_series(1, {ROWS}) g")
    # THE HEAP MIRRORS ARE WRITTEN FROM THE GENERATOR, NOT COPIED (#1339): a copy
    # reads through the reader under test, so a read defect lands on both sides.
    cur.execute("CREATE TABLE vb_full_h (k bigint, v bigint)")
    cur.execute(f"INSERT INTO vb_full_h SELECT g, g FROM generate_series(1, {ROWS}) g")

    cur.execute("CREATE TABLE vb_nulls (k bigint, v bigint) USING pgcolumnar")
    cur.execute(
        f"INSERT INTO vb_nulls SELECT g, CASE WHEN g % {NULL_EVERY} = 0 THEN NULL "
        f"ELSE g END FROM generate_series(1, {ROWS}) g"
    )
    cur.execute("CREATE TABLE vb_nulls_h (k bigint, v bigint)")
    cur.execute(
        f"INSERT INTO vb_nulls_h SELECT g, CASE WHEN g % {NULL_EVERY} = 0 THEN NULL "
        f"ELSE g END FROM generate_series(1, {ROWS}) g"
    )


def _count(cur, sql, args=()):
    cur.execute(sql, args)
    return int(cur.fetchone()[0])


def test_a_column_with_no_nulls_stores_no_validity_bitmap(pgc_conn, expect):
    """The size arms, with the premise each of them rests on.

    The second fixture is not decoration. Deleting the bitmap unconditionally
    satisfies the first arm and loses every null in the table, so the column
    that still needs its bitmap is asserted beside the one that does not.
    """
    with pgc_conn.cursor() as c:
        _load(c)

        full_res, full_chunks = _residual(c, "vb_full")
        nulls_res, _ = _residual(c, "vb_nulls")
        key_res, key_chunks = _residual(c, "vb_nulls", KEY_COLUMN)

        # THE PREMISE THE EXACT NUMBER RESTS ON. With a block codec,
        # page_length is the COMPRESSED encoded region and the subtraction stops
        # being the bitmap: the arms would read a compression ratio and call it
        # a bitmap. Read back rather than assumed -- the SET is a statement that
        # has to have taken effect.
        c.execute("SHOW pgcolumnar.compression")
        expect.text(
            c.fetchone()[0], "none",
            "premise: the block codec is off, so the residual is the bitmap and nothing else",
        )

        expect.text(
            "measured" if full_chunks > 0 else "measured-nothing", "measured",
            "premise: the residual was summed over chunks that exist",
        )

        # ceil(ROWS / 8) is ONE group's bitmap. A fixture split across two groups
        # stores two of them, and would match the expected total only by an
        # accident of rounding.
        expect.text(
            f"{_row_groups(c, 'vb_full')}{_row_groups(c, 'vb_nulls')}", "11",
            "premise: each fixture is a single row group, which is what the expected size assumes",
        )

        full_nulls = _count(c, "SELECT count(*) FROM vb_full WHERE v IS NULL")
        expect.num(full_nulls, 0, "the full column reads back no nulls at all")  # an arm: counted through the reader (#1339)

        some_nulls = _count(c, "SELECT count(*) FROM vb_nulls WHERE v IS NULL")
        expect.text(
            "has-nulls" if some_nulls > 0 else "none", "has-nulls",
            "the second fixture holds nulls, so its bitmap is load-bearing",
        )

        expect.num(full_res, 0,
                   "a column with no nulls stores no validity bitmap")
        expect.num(nulls_res, (ROWS + 7) // 8,
                   "while a column with nulls still stores one, sized one bit per row")

        # ITS OWN PREMISE, BESIDE IT RATHER THAN WITH THE OTHERS, so the two
        # move together. The arm below expects 0, and 0 is also what the
        # residual returns when it summed nothing -- a column index off the end
        # of the table answers exactly as a correctly elided bitmap does.
        # Measured by @OffgridwithJD against the first version of this file:
        # `KEY_COLUMN = 99` left all 24 checks green. `_residual` returns the
        # count for this reason and two of its three call sites may discard it,
        # because their residual is one that a bug makes LARGE.
        expect.text(
            "measured" if key_chunks > 0 else "measured-nothing", "measured",
            "premise: the per-chunk arm's residual was summed over chunks that exist",
        )

        # THE ONLY ARM THAT SAYS THE DECISION IS PER CHUNK. Both arms above are
        # satisfied by a writer deciding once per ROW GROUP: one fixture's group
        # holds no null anywhere and the other's holds some. vb_nulls carries a
        # null-free key column in the SAME row group as its null-bearing one.
        expect.num(key_res, 0,
                   "a null-free column elides its bitmap beside a null-bearing one "
                   "in the same row group")


def test_the_layout_change_returns_the_same_rows(pgc_conn, expect):
    """The invariant the size arms exist to protect.

    A layout change that loses or shifts a null is a data-loss bug, not a size
    regression, and the null count is asserted separately because both EXCEPT
    ALL arms are satisfied by a table that agrees on values and not on nulls.
    """
    with pgc_conn.cursor() as c:
        _load(c)

        for label, tbl in (("full", "vb_full"), ("nulls", "vb_nulls")):
            expect.num(
                _count(c, f"SELECT count(*) FROM (SELECT k, v FROM {tbl} "
                          f"EXCEPT ALL SELECT k, v FROM {tbl}_h) d"),
                0, f"the {label} column reads back exactly what the heap holds",
            )
            expect.num(
                _count(c, f"SELECT count(*) FROM (SELECT k, v FROM {tbl}_h "
                          f"EXCEPT ALL SELECT k, v FROM {tbl}) d"),
                0, f"the {label} column holds no row the heap does not",
            )
            expect.num(
                _count(c, f"SELECT count(*) FROM {tbl} WHERE v IS NULL"),
                _count(c, f"SELECT count(*) FROM {tbl}_h WHERE v IS NULL"),
                f"the {label} column preserves its null count",
            )


def _guard(exc, phrase, label):
    """`SQLSTATE|label` when the error is the guard whose message holds `phrase`;
    otherwise `SQLSTATE|another guard: <message>`, so a red names what fired."""
    if exc is None:
        return "NOERR"
    msg = getattr(getattr(exc, "diag", None), "message_primary", None) or str(exc)
    state = getattr(exc, "sqlstate", None) or "none"
    return f"{state}|{label}" if phrase in msg else f"{state}|another guard: {msg}"


def test_the_guards_the_elision_added_refuse_a_lying_catalog(pgc_conn, expect):
    """The two guards, each corrupted into firing.

    Neither is reachable from a table written correctly, so the catalog is
    poisoned the way corruption.sh poisons the fields that predate this change.

    THE SQLSTATE AND THE GUARD'S OWN MESSAGE. `XX001` is ERRCODE_DATA_CORRUPTED
    and comes only from a guard that ran; a message grep alone is equally
    satisfied by a missing function, a bad argument or a login failure. But
    sixteen guards in the reader raise XX001, so the SQLSTATE alone cannot say
    WHICH ran (#1339): `_guard` asks for both.
    """
    import psycopg

    with pgc_conn.cursor() as c:
        _load(c)

        # GUARD 1: the chunk CLAIMS it stored no bitmap while holding fewer
        # values than the group has rows. vb_nulls' v column holds 90% of them,
        # so setting the flag on it is exactly the lie the guard exists for.
        c.execute(
            """
            UPDATE pgcolumnar.column_chunk c
               SET encoding_descriptor = set_byte(c.encoding_descriptor, 1, 1)
              FROM pgcolumnar.storage s
             WHERE s.storage_id = c.storage_id
               AND s.relation_oid = 'vb_nulls'::regclass
               AND c.column_index = %s
            """,
            (MEASURED_COLUMN,),
        )
        try:
            c.execute("SELECT sum(v) FROM vb_nulls")
            raised = None
        except psycopg.Error as exc:
            raised = exc
        # WHICH GUARD, not only that one ran (#1339). XX001 comes from sixteen guards in
        # the reader: with this one disabled, the scan still raised XX001 and the arm
        # stayed green. So the message must be this guard's own.
        expect.text(_guard(raised, "claims no nulls but does not account for every row",
                           "the no-nulls guard"),
                    "XX001|the no-nulls guard",
                    "a chunk claiming no bitmap while it holds fewer values than rows is refused")
        expect.num(_count(c, "SELECT 1"), 1, "and the backend survives that refusal")

        # GUARD 2: a row count the chunk cannot hold. The descriptor still
        # accounts for its own rows, so the claim is false in the other
        # direction and the reader refuses BEFORE synthesizing half a gigabyte
        # of bits.
        #
        # BOUNDING THE SYNTHESIZED BITMAP BY THE ROW GROUP'S BYTE LENGTH DOES
        # NOT WORK: an elided group of two well-encoded bigint columns measured
        # 360 bytes on disk against the 12,500 bytes of bits it no longer
        # stores. That is the saving, not a defect.
        c.execute(
            """
            UPDATE pgcolumnar.row_group r
               SET row_count = 4000000000
              FROM pgcolumnar.storage s
             WHERE s.storage_id = r.storage_id
               AND s.relation_oid = 'vb_full'::regclass
            """
        )
        try:
            c.execute("SELECT sum(v) FROM vb_full")
            raised = None
        except psycopg.Error as exc:
            raised = exc
        # Refused BEFORE anything is allocated means by the value-count guard, which
        # runs first; the MaxAllocSize check after it is a backstop that 500 MB of
        # bits does not reach. With the value-count guard disabled, the bitmap was
        # allocated, a later guard raised XX001, and this arm stayed green (#1339).
        expect.text(_guard(raised, "claims no nulls but does not account for every row",
                           "the no-nulls guard"),
                    "XX001|the no-nulls guard",
                    "an implausible row count on an elided chunk is refused before anything is allocated")
        expect.num(_count(c, "SELECT 1"), 1, "and the backend survives that refusal too")


def test_the_fetch_path_reads_an_elided_chunk_correctly(pgc_conn, expect):
    """The second reader, which the scan arms above cannot speak for.

    The premise asserts the PLAN. A row that comes back is no evidence about
    which reader produced it, and the two readers disagree only for a chunk that
    elided its bitmap -- which is why the first version of these arms passed
    against a build whose fetch path returned no row at all for a row that is
    there.
    """
    with pgc_conn.cursor() as c:
        _load(c)
        c.execute("CREATE INDEX vb_full_k ON vb_full (k)")
        c.execute("CREATE INDEX vb_nulls_k ON vb_nulls (k)")

        c.execute("SET pgcolumnar.enable_custom_scan = off")
        c.execute("SET enable_seqscan = off")
        c.execute("SET enable_bitmapscan = off")
        c.execute("SET max_parallel_workers_per_gather = 0")

        c.execute("EXPLAIN (COSTS OFF) SELECT v FROM vb_full WHERE k = 12345")
        plan = [row[0] for row in c.fetchall()]
        expect.num(
            sum(1 for line in plan if line.lstrip().startswith("Index Scan using")), 1,
            "the arms below reach the row through an index scan",
        )

        # AND THE SHAPE THE NEXT ARMS ACTUALLY RUN. The premise above plans a
        # point query; these plan a join, which the planner may serve
        # differently. Asserting one and running the other is how an arm ends up
        # licensed by a plan nobody produced.
        c.execute(
            "EXPLAIN (COSTS OFF) SELECT count(*) FROM vb_full c "
            "JOIN vb_full_h h ON h.k = c.k WHERE c.v IS DISTINCT FROM h.v"
        )
        join_plan = [row[0] for row in c.fetchall()]
        expect.num(
            sum(1 for line in join_plan
                if line.lstrip().lstrip("-> ").startswith("Index Scan using vb_full_k")), 1,
            "premise: the join arms below reach the columnar side by index too",
        )

        for label, tbl in (("full", "vb_full"), ("nulls", "vb_nulls")):
            expect.num(
                _count(c, f"SELECT count(*) FROM {tbl} c JOIN {tbl}_h h ON h.k = c.k "
                          f"WHERE c.v IS DISTINCT FROM h.v"),
                0, f"the {label} column fetched by index matches the heap, row for row",
            )

        # ONE ROW AT A TIME, at positions where a bitmap read out of the wrong
        # bytes lands on a neighbour or reports a present row as NULL. The join
        # above is satisfied by a fetch that returns the row it was asked for;
        # these are not.
        bad = []
        for pos in (1, 2, 3, 8, 9, 4097, ROWS // 2, ROWS - 1, ROWS):
            c.execute("SELECT v FROM vb_full WHERE k = %s", (pos,))
            row = c.fetchone()
            got = None if row is None else row[0]
            if got != pos:
                bad.append(f"k={pos}(got={got})")
        expect.text(
            " ".join(bad) if bad else "same", "same",
            "single-row fetches through the elided path return the row asked for",
        )

        # THE NULL-BEARING COLUMN NEEDS ITS OWN ONE-ROW PROBE, and a null row in
        # it. vb_nulls keeps its bitmap, so these exercise the other branch of
        # the same per-chunk decision -- and the row that is genuinely NULL is
        # the one a fetch that lost the bitmap answers wrongly in the
        # safe-looking direction.
        bad_n = []
        for pos in (9, 10, 11, 4096, 4097, ROWS // 2, ROWS - 1, ROWS):
            want = None if pos % NULL_EVERY == 0 else pos
            c.execute("SELECT v FROM vb_nulls WHERE k = %s", (pos,))
            row = c.fetchone()
            got = None if row is None else row[0]
            if got != want:
                bad_n.append(f"k={pos}(got={got} want={want})")
        expect.text(
            " ".join(bad_n) if bad_n else "same", "same",
            "single-row fetches of the null-bearing column return its nulls as nulls",
        )
