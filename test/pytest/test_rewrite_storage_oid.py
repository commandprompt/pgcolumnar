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


def _points_at(conn, relname):
    return _one(
        conn,
        f"SELECT (relation_oid = '{relname}'::regclass)::int"
        f"  FROM pgcolumnar.storage"
        f" WHERE storage_id = pgcolumnar.get_storage_id('{relname}')",
    )


def test_a_matview_created_with_data_points_at_itself(pgc_conn, expect):
    """The same defect on a different statement (#1275).

    `CREATE MATERIALIZED VIEW ... AS` with data builds a transient, fills it and
    swaps, exactly as a rewriting `ALTER` does, so the storage row is written
    with the transient's OID and the swap leaves it naming a relation that no
    longer exists. Measured before the fix: the row named a dropped 16527 while
    the matview was 16523.

    `REFRESH MATERIALIZED VIEW` already repaired it, because that node type was
    in the repair gate. `CreateTableAsStmt` was not.

    THE `CREATE TABLE ... AS` ARM IS THE CONTROL THAT NARROWS THE CLAIM. Same
    parse node, and it does NOT have the defect -- it fills the relation it
    created instead of swapping a transient in. Without it the fix would
    reasonably have been written for the node type as a whole, which is broader
    than anything measured asked for.
    """
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE mv_src (id int, note text) USING pgcolumnar")
        cur.execute("INSERT INTO mv_src SELECT g, 'n'||g FROM generate_series(1,900) g")
        cur.execute("CREATE MATERIALIZED VIEW mv_data USING pgcolumnar AS"
                    " SELECT id, note FROM mv_src")
        cur.execute("CREATE TABLE mv_cta USING pgcolumnar AS SELECT id, note FROM mv_src")

    # A ROW COUNT FIRST, so the arms below are about a relation that was
    # actually populated. A matview created WITH NO DATA writes no storage row
    # at all, and every arm here would then be asking about nothing.
    expect.num(
        _one(pgc_conn, "SELECT count(*) FROM mv_data"),
        900,
        "premise: the matview holds the rows it was created with",
    )
    expect.num(
        _one(pgc_conn, "SELECT count(*) FROM mv_cta"),
        900,
        "premise: the CREATE TABLE AS table holds them too",
    )

    mv, cta = _points_at(pgc_conn, "mv_data"), _points_at(pgc_conn, "mv_cta")
    print(f"-- mv={mv} cta={cta}")

    # THE CONTROL, AND IT MUST PASS BEFORE THE FIX AS WELL AS AFTER. If this
    # ever reads 0 the claim below is no longer about matviews specifically and
    # the remedy is a different one.
    expect.num(
        cta, 1, "premise: CREATE TABLE ... AS leaves its storage row pointing at itself"
    )
    expect.num(
        mv, 1, "a matview created WITH DATA leaves its storage row pointing at itself"
    )

    # AND REFRESH MUST STILL WORK. It repaired this before the fix, through a
    # different node type, so an arm here is what says the fix did not displace
    # the path that already worked.
    with pgc_conn.cursor() as cur:
        cur.execute("REFRESH MATERIALIZED VIEW mv_data")
    expect.num(
        _one(pgc_conn, "SELECT count(*) FROM mv_data"),
        900,
        "premise: the refreshed matview still holds every row",
    )
    expect.num(
        _points_at(pgc_conn, "mv_data"), 1, "and REFRESH still leaves it pointing at itself"
    )
