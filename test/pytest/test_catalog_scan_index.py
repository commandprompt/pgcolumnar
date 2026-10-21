"""Twelve metadata scans must not read their catalog whole (#1207).

Twelve `systable_beginscan` calls in `columnar_metadata.c` passed `InvalidOid` on
a key an existing index starts with, so each read its whole catalog to answer a
question about one table: three on `free_space`, `row_group_exists` (reached by
EVERY DELETE), four on `storage`, four on `projection_declaration`. The
catalogs are shared by every columnar table in the database, so each read was
charged for every other table's rows. The fix routes each through
`pgcolumnar_scan_index_oid()`, which probes at or above
`pgcolumnar.index_min_blocks` pages.

WHAT IS ASSERTED, AND WHY IT IS A PATH. `test_catalog_delete_index.py` asserts
buffers and never `seq_scan`, because a build that DECLINES a probe on a small
catalog is correct and a path assertion fires on it. That holds only where
declining can be right. Every arm here runs after 350 other projected tables
have put each target catalog AT OR ABOVE the threshold, and a premise asserts it
against the live setting; there the resolver never declines, so a sequential
scan during the operation is one of these sites still reading whole. Two cost
measures were tried first and a revert survived both; `catalog_scan_index.sh`
records how.

Independent of `test/catalog_scan_index.sh`: its own population (bigint keys,
350 tables, groups 1 and 3 retired rather than 0 and 2), its own row counts, and
its own effect values.

ON A PRIVATE DATABASE (`pgc_own_db`): the premises are about catalog size, and
must not depend on what other files left behind. `pg_stat_reset()` is per
database; this file must not run concurrently with another that reads
statistics.
"""

NOISE = 350


def _q(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0] if cur.description else None


def _make_target(conn, expect, t, name, projection=True):
    """Seven 1000-row groups:

        group 0   retired               a free range, so the rewrite can reuse one
        group 5   every other row dead  a group compact_rewrite will write
        group 6   retired               a TRAILING free range for truncate()

    and a projection over both columns, except for the end-truncation arm: a
    projection is its own storage in the same file, written after the base's
    groups, so it leaves no tail to reclaim.

    The compaction is its own statement on an autocommit connection: compact()
    cannot retire rows deleted by a transaction that has not committed.
    """
    with conn.cursor() as cur:
        cur.execute(f"CREATE TABLE {t} (a bigint, b text) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options(%s, stripe_row_limit => 1000)", (t,))
        cur.execute(f"INSERT INTO {t} SELECT g, 'r' || g FROM generate_series(1,7000) g")
        cur.execute(f"DELETE FROM {t} WHERE a <= 1000 OR a > 6000")
        if projection:
            cur.execute("SELECT pgcolumnar.add_projection(%s, 'p', '{a,b}', '{a}')", (t,))
        cur.execute("SELECT pgcolumnar.compact(%s)", (t,))
        cur.execute(f"DELETE FROM {t} WHERE a BETWEEN 5001 AND 6000 AND a % 2 = 1")
    expect.num(
        _q(conn, "SELECT count(*) FROM pgcolumnar.free_space "
                 f"WHERE storage_id = pgcolumnar.get_storage_id('{t}')"),
        2,
        f"premise: the {name} target holds two free ranges, one of them trailing",
    )


def _work(conn, catalog, statements):
    """(buffers, seq_scan, idx_scan) for `catalog` while `statements` ran.

    FLUSH BEFORE THE RESET: one connection holds this whole file, so the
    fixture's own pending statistics would otherwise land on the reading
    (measured on test_catalog_delete_index.py). Each statement is its own
    transaction on the autocommit connection, which pgcolumnar.truncate()
    requires.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_stat_force_next_flush()")
        cur.execute("SELECT pg_stat_reset()")
        for stmt in statements:
            cur.execute(stmt)
        cur.execute("RESET pgcolumnar.enable_end_truncation")
        cur.execute("SELECT pg_stat_force_next_flush()")
        cur.execute(
            "SELECT coalesce(io.heap_blks_read,0) + coalesce(io.heap_blks_hit,0) "
            "     + coalesce(io.idx_blks_read,0) + coalesce(io.idx_blks_hit,0), "
            "       coalesce(st.seq_scan,0), coalesce(st.idx_scan,0) "
            "FROM pg_statio_all_tables io JOIN pg_stat_all_tables st USING (relid) "
            "WHERE io.schemaname = 'pgcolumnar' AND io.relname = %s",
            (catalog,),
        )
        row = cur.fetchone()
    # None, not zeros, when the statistics row is missing: (0, 0, 0) passed "does not
    # read it whole" with nothing measured (#1339).
    return tuple(int(v) for v in row) if row else (None, None, None)


def test_catalog_scans_do_not_read_their_catalog_whole(pgc_own_db, expect, server_dir):
    conn = pgc_own_db
    with conn.cursor() as cur:
        for i in range(NOISE):
            t = f"other_{i}"
            cur.execute(f"CREATE TABLE {t} (a bigint, b text) USING pgcolumnar")
            cur.execute("SELECT pgcolumnar.set_options(%s, stripe_row_limit => 1000)", (t,))
            cur.execute(f"INSERT INTO {t} SELECT g, 'n' || g FROM generate_series(1,4000) g")
            cur.execute("SELECT pgcolumnar.add_projection(%s, 'p', '{a}', '{a}')", (t,))
            # Groups 1 and 3: NOT adjacent, so two free ranges rather than one.
            cur.execute(f"DELETE FROM {t} WHERE (a - 1) / 1000 IN (1, 3)")
            cur.execute("SELECT pgcolumnar.compact(%s)", (t,))
        for c in ("storage", "row_group", "free_space", "projection_declaration"):
            cur.execute(f"VACUUM pgcolumnar.{c}")

    min_blocks = int(_q(conn, "SHOW pgcolumnar.index_min_blocks"))
    for c in ("storage", "row_group", "free_space", "projection_declaration"):
        pages = _q(conn, f"SELECT pg_relation_size('pgcolumnar.{c}') / 8192")
        print(f"-- pgcolumnar.{c}: {pages} pages")
        expect.at_least(
            pages,
            min_blocks,
            f"premise: pgcolumnar.{c} is at least index_min_blocks pages",
        )

    load = str(server_dir / "scan_index_load.txt")
    with conn.cursor() as cur:
        cur.execute(
            "COPY (SELECT g, 'q' || g FROM generate_series(20001,21500) g) "
            "TO '%s' WITH (FORMAT text)" % load
        )

    # (name, catalog, statements with {t}, effect with {t}, want, projection)
    # 7000 rows less groups 0 and 6 and half of group 5: 4500 live.
    arms = [
        ("an insert into an existing table", "storage",
         ["INSERT INTO {t} SELECT g, 'y' FROM generate_series(8001,8050) g"],
         "SELECT count(*)::text FROM {t}", "4550", True),
        ("an insert into a table created in the same transaction", "storage",
         ["BEGIN",
          "CREATE TABLE {t}_new (a bigint, b text) USING pgcolumnar",
          "INSERT INTO {t}_new SELECT g, 'z' FROM generate_series(1,75) g",
          "COMMIT"],
         "SELECT count(*)::text FROM {t}_new", "75", True),
        ("a parallel bulk load", "storage",
         ["SELECT pgcolumnar.parallel_copy('{t}', '%s', 2)" % load],
         "SELECT count(*)::text FROM {t}", "6000", True),
        ("a one-row delete", "row_group",
         ["DELETE FROM {t} WHERE a = 2500"],
         "SELECT count(*)::text FROM {t}", "4499", True),
        ("an online rewrite", "free_space",
         ["SELECT pgcolumnar.compact_rewrite('{t}', 0.2, 0)"],
         "SELECT sum(deletedrows)::text FROM pgcolumnar.stats('{t}')", "0", True),
        ("an end truncation", "free_space",
         ["SET pgcolumnar.enable_end_truncation = on",
          "SELECT pgcolumnar.truncate('{t}')"],
         "SELECT count(*)::text FROM pgcolumnar.free_space "
         "WHERE storage_id = pgcolumnar.get_storage_id('{t}')", "1", False),
        ("a sorted rewrite", "storage",
         ["SELECT pgcolumnar.vacuum_sorted('{t}', 'a')"],
         "SELECT (sorted_through IS NOT NULL)::text FROM pgcolumnar.storage "
         "WHERE storage_id = pgcolumnar.get_storage_id('{t}')", "true", True),
        ("renaming a projected column", "projection_declaration",
         ["ALTER TABLE {t} RENAME COLUMN b TO b2"],
         "SELECT count(*)::text FROM pgcolumnar.projection_declaration "
         "WHERE rel = '{t}'::regclass AND 'b2' = ANY(columns)", "1", True),
        ("truncating a projected table", "projection_declaration",
         ["TRUNCATE {t}"],
         "SELECT count(*)::text FROM {t}", "0", True),
        ("dropping a projection", "projection_declaration",
         ["SELECT pgcolumnar.drop_projection('{t}', 'p')"],
         "SELECT count(*)::text FROM pgcolumnar.projection_declaration "
         "WHERE rel = '{t}'::regclass", "0", True),
        ("dropping a projected table", "projection_declaration",
         ["DROP TABLE {t}"],
         "SELECT (to_regclass('{t}') IS NULL)::text", "true", True),
    ]

    # The arms below are a loop over a list, and a loop that never ran asserts
    # nothing while the premises above still pass. Counted as MEASURED, after the
    # loop, not as the list's length (#1339): an arm whose statistics never came back
    # still made eleven.
    expect.num(len(arms), 11, "premise: the arm list names eleven operations")
    measured = 0
    for n, (name, cat, stmts, effect, want, projection) in enumerate(arms, 1):
        t = f"target_{n}"
        _make_target(conn, expect, t, name, projection)
        bufs, seq, idx = _work(conn, cat, [s.format(t=t) for s in stmts])
        print(f"-- {name}: pgcolumnar.{cat} buffers={bufs} seq_scan={seq} idx_scan={idx}")
        if isinstance(seq, int) and isinstance(idx, int):
            measured += 1
        # Not premises (#1339): each operation and its catalog access are pgcolumnar's.
        expect.text(
            _q(conn, effect.format(t=t)),
            want,
            f"{name} took effect",
        )
        expect.at_least(
            seq + idx if isinstance(seq, int) and isinstance(idx, int) else None,
            1,
            f"{name} reached pgcolumnar.{cat}",
        )
        expect.num(seq, 0, f"{name} does not read pgcolumnar.{cat} whole")
    expect.num(measured, 11, "premise: eleven operations are measured")
