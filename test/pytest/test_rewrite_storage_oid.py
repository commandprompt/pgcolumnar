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
        # Five checks here read product state and are arms, not premises (#1339,
        # #1236): the row counts after a rewrite, a matview, CREATE TABLE AS and REFRESH,
        # and the untouched table's storage row. A bulk load that drops its rows, or
        # a repoint of the wrong storage row, reddens them.
        expect.num(
            cur.fetchone()[0],
            ROWS,
            "the rewritten table still holds every row",
        )
        cur.execute(
            "SELECT (relation_oid = 'keep_side'::regclass)::int "
            "FROM pgcolumnar.storage "
            "WHERE storage_id = pgcolumnar.get_storage_id('keep_side')"
        )
        expect.num(
            cur.fetchone()[0],
            1,
            "a table that was not rewritten still points at itself",
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
        "the matview holds the rows it was created with",
    )
    expect.num(
        _one(pgc_conn, "SELECT count(*) FROM mv_cta"),
        900,
        "the CREATE TABLE AS table holds them too",
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
        "the refreshed matview still holds every row",
    )
    expect.num(
        _points_at(pgc_conn, "mv_data"), 1, "and REFRESH still leaves it pointing at itself"
    )


def test_the_repair_gate_leaves_untouched_partitions_alone(pgc_conn, pgc_cluster, expect):
    """An ALTER that does not recurse must not make the gate open other partitions.

    The repair gate runs for every inheritor of an ALTER's target. DETACH,
    DETACH CONCURRENTLY, ATTACH and ALTER TABLE ONLY lock only the parent and
    the partition they name, and the gate opened every OTHER partition with
    NoLock to read its storage id: an Assert in relation_open on an
    assert-enabled build, an unlocked open on a release build. Two columnar
    partitions are needed; with one, nothing else is left to open.

    The fix skips a relation the statement holds no lock on. So each arm reads
    the blocks of the untouched partition in pg_statio, flushed by the ALTER's
    own session: the unfixed gate reads its metapage, the fixed gate reads
    nothing of it, on every build. A session the ALTER aborted flushes nothing
    and reports None, which the arm refuses rather than reading as zero.

    Independent of the shell twin: its own tables and row counts, and DETACH
    CONCURRENTLY runs on an autocommit connection of its own.
    """
    import psycopg

    schema = _one(pgc_conn, "SELECT current_schema()")

    def setup():
        with pgc_conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS gp, gp_a, gp_b, gp_c CASCADE")
            cur.execute("CREATE TABLE gp (ts timestamptz, n int) PARTITION BY RANGE (ts)")
            cur.execute("CREATE TABLE gp_a PARTITION OF gp FOR VALUES FROM ('2025-06-01')"
                        " TO ('2025-07-01') USING pgcolumnar")
            cur.execute("CREATE TABLE gp_b PARTITION OF gp FOR VALUES FROM ('2025-07-01')"
                        " TO ('2025-08-01') USING pgcolumnar")
            cur.execute("CREATE TABLE gp_c (ts timestamptz, n int) USING pgcolumnar")
            cur.execute("INSERT INTO gp SELECT '2025-06-01'::timestamptz + g * interval '2 h', g"
                        " FROM generate_series(1, 600) g")

    def gp_a_blocks_during(statement):
        """Blocks of gp_a read while `statement` ran in a session of its own."""
        with pgc_conn.cursor() as cur:
            cur.execute("SELECT pg_stat_force_next_flush()")
            cur.execute("SELECT pg_stat_reset()")
        try:
            other = psycopg.connect(pgc_cluster.dsn(), autocommit=True)
            try:
                other.execute(f'SET search_path TO "{schema}", public')
                other.execute(statement)
                other.execute("SELECT pg_stat_force_next_flush()")
            finally:
                other.close()
        except psycopg.OperationalError:
            return None
        return _one(pgc_conn, "SELECT coalesce(heap_blks_read,0) + coalesce(heap_blks_hit,0)"
                              " FROM pg_statio_user_tables WHERE relname = 'gp_a'"
                              f" AND schemaname = '{schema}'")

    setup()
    # June at 2 h steps from 02:00 on the 1st: 359 rows; the rest are July's.
    expect.text(f"{_one(pgc_conn, 'SELECT count(*) FROM gp_a')}|"
                f"{_one(pgc_conn, 'SELECT count(*) FROM gp_b')}",
                "359|241", "premise: both columnar partitions hold their rows")

    expect.at_least(gp_a_blocks_during("SELECT count(*) FROM gp_a") or 0, 1,
                    "control: reading lp_1 shows up as blocks read from it")

    # (statement, the shell twin's name for it). A literal, so the loop below
    # cannot run zero times.
    pairs = [
        ("ALTER TABLE ONLY gp ALTER COLUMN n SET DEFAULT 0",
         "ONLY lp ALTER COLUMN v SET DEFAULT 0"),
        ("ALTER TABLE gp ATTACH PARTITION gp_c FOR VALUES FROM ('2025-08-01') TO ('2025-09-01')",
         "lp ATTACH PARTITION lp_3 FOR VALUES FROM ('2026-03-01') TO ('2026-04-01')"),
        ("ALTER TABLE gp DETACH PARTITION gp_b", "lp DETACH PARTITION lp_2"),
        ("ALTER TABLE gp DETACH PARTITION gp_b CONCURRENTLY",
         "lp DETACH PARTITION lp_2 CONCURRENTLY"),
    ]
    for alter, name in pairs:
        setup()
        n = gp_a_blocks_during(alter)
        print(f"-- gp_a blocks read during: {alter} -> {n}")
        expect.num(n, 0,
                   f"the repair gate does not read a partition the ALTER did not touch: {name}")

    setup()
    with pgc_conn.cursor() as cur:
        cur.execute("ALTER TABLE gp ALTER COLUMN n TYPE bigint")
    expect.text(
        _one(pgc_conn,
             "SELECT string_agg((s.relation_oid = c.oid)::int::text, '' ORDER BY c.relname)"
             " FROM pg_class c JOIN pgcolumnar.storage s"
             " ON s.storage_id = pgcolumnar.get_storage_id(c.oid::regclass)"
             f" WHERE c.relname IN ('gp_a', 'gp_b') AND c.relnamespace = '{schema}'::regnamespace"),
        "11",
        "and a rewrite through the parent still repoints every partition's storage row",
    )
