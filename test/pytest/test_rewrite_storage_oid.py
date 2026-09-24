"""A type rewrite must keep pgcolumnar.storage.relation_oid on the live table.

A rewriting ALTER COLUMN TYPE writes the storage row under the transient
relation make_new_heap builds. That relation is dropped after the swap, and
the user's OID is what later lookups use. The rows stay readable.

Independent of test/rewrite_storage_oid.sh: same public seam (the storage
row for get_storage_id compared with the live regclass), own table, own
row count, own type change.
"""

ROWS = 1400


def _one(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    return row[0] if row else None


def test_rewrite_storage_oid(pgc_conn, expect):
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE keep_side (n int) USING pgcolumnar")
        cur.execute("INSERT INTO keep_side SELECT g FROM generate_series(1,25) g")
        cur.execute("CREATE TABLE rewrite_live (n int, label text) USING pgcolumnar")
        cur.execute(
            f"INSERT INTO rewrite_live SELECT g, 'r' || g FROM generate_series(1,{ROWS}) g"
        )
        cur.execute(
            "SELECT (relation_oid = 'rewrite_live'::regclass)::int "
            "FROM pgcolumnar.storage "
            "WHERE storage_id = pgcolumnar.get_storage_id('rewrite_live')"
        )
        expect.num(
            cur.fetchone()[0],
            1,
            "premise: the storage row points at the live table before the rewrite",
        )
        cur.execute("SELECT count(*) FROM rewrite_live")
        expect.num(
            cur.fetchone()[0],
            ROWS,
            "premise: the table holds every inserted row",
        )
        cur.execute("ALTER TABLE rewrite_live ALTER COLUMN n TYPE bigint")
        cur.execute("SELECT count(*) FROM rewrite_live")
        expect.num(
            cur.fetchone()[0],
            ROWS,
            "premise: the rewritten table still holds every row",
        )
        cur.execute(
            "SELECT (relation_oid = 'keep_side'::regclass)::int "
            "FROM pgcolumnar.storage "
            "WHERE storage_id = pgcolumnar.get_storage_id('keep_side')"
        )
        expect.num(
            cur.fetchone()[0],
            1,
            "premise: a table that was not rewritten still points at itself",
        )
        pointed = _one(
            pgc_conn,
            "SELECT (relation_oid = 'rewrite_live'::regclass)::int "
            "FROM pgcolumnar.storage "
            "WHERE storage_id = pgcolumnar.get_storage_id('rewrite_live')",
        )
        print(f"-- live={pointed}")
        expect.num(
            pointed,
            1,
            "a type rewrite leaves the storage row pointing at the live table",
        )
