"""The native encoding descriptor's wire layout is stable (#1130, #1341). The pytest
twin of `test/native_encdesc_golden.sh`, every check by the shell's name.

The descriptor is the writer-to-reader contract for a column chunk's per-vector
encoding, laid out as (version 3): header version u8 @0, flags u8 @1, vectorCount u32
@2; then per vector type u8, valueCount u32, rawLen u32, encLen u32 (13 bytes); then a
sharedTableLen u32 trailer. A deterministic 300-row table pins the bytes at those
offsets, not which encoding was chosen. A read that DECODES closes it: count(*) and
sum(a) are answered from the catalog and the zone maps, so both stayed green with the
reader parsing rawLen and encLen from each other's offsets, and only every row's text,
against the generator's, needs the pages.
"""
import psycopg


def _one(cur, sql):
    cur.execute(sql)
    return cur.fetchone()[0]


def test_the_descriptor_layout(pgc_conn, expect):
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE d (a int, b bigint, c text) USING pgcolumnar")
        cur.execute("INSERT INTO d SELECT g, g*1000000::bigint, 'val'||(g%4) FROM generate_series(1,300) g")
        sid = _one(cur, "SELECT pgcolumnar.get_storage_id('d')")
        chunk = f"FROM pgcolumnar.column_chunk WHERE storage_id = {sid} AND column_index = {{col}}"

        def byte(col, off):
            return _one(cur, f"SELECT get_byte(encoding_descriptor, {off}) {chunk.format(col=col)}")

        def le32(col, off):
            return int(_one(cur, f"SELECT get_byte(d,{off})+get_byte(d,{off}+1)*256+get_byte(d,{off}+2)*65536"
                                 f"+get_byte(d,{off}+3)*16777216 FROM (SELECT encoding_descriptor d "
                                 f"{chunk.format(col=col)}) x"))

        for col in (0, 1, 2):
            expect.num(byte(col, 0), 3, f"column {col} descriptor version byte is 3")
        # flags: the fixture has no NULL, so every chunk says it stored no bitmap
        for col in (0, 1, 2):
            expect.num(byte(col, 1), 1, f"column {col} descriptor flags byte says the bitmap was omitted")
        expect.num(le32(0, 2), 1, "vectorCount parses as 1 (at offset 2)")
        expect.num(_one(cur, f"SELECT octet_length(encoding_descriptor) {chunk.format(col=0)}"), 23,
                   "descriptor length is header + 1 entry + empty trailer (23)")
        expect.num(le32(0, 19), 0, "trailing sharedTableLen is 0 (u32 at offset 6+13=19)")
        for col in (0, 1, 2):
            expect.num(le32(col, 7), 300, f"column {col} entry valueCount parses as 300 (at offset 7)")
        expect.num(le32(0, 11), 1200, "int column a rawLen parses as 1200 (300*4, at offset 11)")
        expect.num(le32(1, 11), 2400, "bigint column b rawLen parses as 2400 (300*8, at offset 11)")

        expect.num(_one(cur, "SELECT count(*) FROM d"), 300, "the table reads back all rows")
        expect.num(int(_one(cur, "SELECT sum(a) FROM d")), 45150, "a full-column read is correct")
        try:
            cur.execute("SELECT md5(string_agg(d::text, ',' ORDER BY a)) FROM d")
            got = cur.fetchone()[0]
        except psycopg.Error as exc:
            got = f"<read failed: {exc.diag.message_primary}>"
        expect.text(got, _one(cur, "SELECT md5(string_agg(x::text, ',' ORDER BY g)) FROM (SELECT g, "
                                   "ROW(g, g*1000000::bigint, 'val'||(g%4)) x FROM generate_series(1,300) g) s"),
                    "every row reads back as the generator wrote it (a decoding read)")
