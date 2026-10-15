"""The per-column extent (#1304 option 5, for #1292 phase 2).

pgcolumnar.column_extent holds, per (storage, column), rows that are inserted and
deleted but never updated: each committing writer adds one for what it flushed, and
a recompute over every visible zone map replaces the rows it saw with one complete
row. The planner trusts the rows only when a complete one is visible; their fold is
then a true bound. pgcolumnar.column_extent(rel, col) shows that bound, and returns
no row when the planner would fall back to the zone maps.

THE ORACLE is the data: min() and max() of the column, printed by the same output
function. Equality only once deleted rows are retired or rewritten; the one case
that may legitimately be wider (a rolled-back savepoint that flushed a group) is
asserted as containment.

ON A PRIVATE DATABASE (`pgc_own_db`): the role, and the transaction-local scan
counters. Independent of `test/column_extent.sh`: its own row counts, values, times
and helpers; the check names match, so the pair is graded one for one.
"""

import psycopg

GB = "SET pgcolumnar.enable_group_vectorization = on"


def _one(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    return row[0] if row else None


def _run(conn, *sqls):
    with conn.cursor() as cur:
        for sql in sqls:
            cur.execute(sql)


def _ext(conn, table, col):
    """'true|min|max', 'none' when no complete extent, or 'error <SQLSTATE>'."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT complete::text || '|' || coalesce(minimum, '-') || '|' || coalesce(maximum, '-')"
                        f" FROM pgcolumnar.column_extent('{table}', '{col}')")
            row = cur.fetchone()
    except psycopg.Error as e:
        return f"error {e.sqlstate}"
    return row[0] if row else "none"


def _truth(conn, table, col):
    return _one(conn, f"SELECT 'true|' || min({col})::text || '|' || max({col})::text FROM {table}")


def _sid(conn, table):
    return _one(conn, f"SELECT pgcolumnar.get_storage_id('{table}')")


def _small_groups(conn, table):
    _run(conn, f"SELECT pgcolumnar.set_options('{table}', stripe_row_limit => 1000,"
               " chunk_group_row_limit => 1000)")


def test_the_extent_is_the_columns_own_order(pgc_own_db, expect):
    conn = pgc_own_db
    expect.num(_one(conn, "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
                          " WHERE n.nspname = 'pgcolumnar' AND p.proname = 'column_extent'"), 1,
               "premise: the extent function exists")
    # Byte order is not value order for any of these: little-endian ints across 256,
    # signed values, numeric scale, and "C" text where 'Q' sorts before 'b'.
    _run(conn, 'CREATE TABLE ty (ts timestamptz, i int, n numeric, d date, s text COLLATE "C") USING pgcolumnar')
    _small_groups(conn, "ty")
    _run(conn, "INSERT INTO ty SELECT '2025-11-03 08:00:00+00'::timestamptz + g * interval '47 s',"
               " (g * 53) % 4001 - 2000, ((g * 6301) % 30011 - 15005) / 11.0, date '2025-03-01' + (g % 523),"
               " chr(81 + (g % 10)) || chr(98 + (g % 20)) FROM generate_series(1, 7300) g")
    expect.at_least(_one(conn, f"SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = {_sid(conn, 'ty')}"),
                    6, "premise: the typed fixture spans several row groups")
    for c in ("ts", "i", "n", "d", "s"):
        expect.text(_ext(conn, "ty", c), _truth(conn, "ty", c),
                    f"the extent of ty.{c} is the data's min and max, in the type's order")


def _plan(pgc_cluster, dbname):
    """(zone_map scans, the estimate) planning a bucketed GROUP BY on pl, in one transaction."""
    with psycopg.connect(pgc_cluster.dsn(dbname=dbname)) as c:
        with c.cursor() as cur:
            cur.execute(GB)
            cur.execute("EXPLAIN SELECT date_trunc('minute', ts) b, avg(v) FROM pl GROUP BY b")
            plan = "\n".join(r[0] for r in cur.fetchall())
            cur.execute("SELECT pg_stat_get_xact_numscans('pgcolumnar.zone_map'::regclass)"
                        " + pg_stat_get_xact_numscans('pgcolumnar.zone_map_pkey'::regclass)")
            scans = cur.fetchone()[0]
        c.rollback()
    est = plan.split("rows=", 1)[1].split(" ", 1)[0] if "rows=" in plan else ""
    return scans, est


def test_the_planner_reads_the_extent_not_the_zone_maps(pgc_own_db, pgc_cluster, expect):
    conn = pgc_own_db
    db = _one(conn, "SELECT current_database()")
    _run(conn, "CREATE TABLE pl (ts timestamptz, v float8) USING pgcolumnar")
    _small_groups(conn, "pl")
    _run(conn, "INSERT INTO pl SELECT '2025-07-01'::timestamptz + g * interval '3 s', g"
               " FROM generate_series(1, 60000) g", "ANALYZE pl")
    scans_with, est_with = _plan(pgc_cluster, db)
    expect.num(scans_with, 0, "planning a bucketed GROUP BY scans zone_map not at all when the extent is complete")
    _run(conn, f"DELETE FROM pgcolumnar.column_extent WHERE storage_id = {_sid(conn, 'pl')}")
    scans_without, est_without = _plan(pgc_cluster, db)
    expect.num(scans_without, 1, "without the extent rows the planner falls back to one zone_map scan")
    expect.text(f"rows={est_with}", f"rows={est_without}", "and the estimate is the one the zone maps give")
    expect.text(_ext(conn, "pl", "ts"), "none", "and with no extent rows the function reports no complete extent")
    _run(conn, "INSERT INTO pl VALUES ('2025-06-30 00:00:00+00', 0)")
    expect.text(_ext(conn, "pl", "ts"), _truth(conn, "pl", "ts"),
                "a writer that finds no complete extent recomputes one over every group")


def test_writers_concurrent_aborted_and_prepared(pgc_own_db, pgc_cluster, expect):
    conn = pgc_own_db
    dsn = pgc_cluster.dsn(dbname=_one(conn, "SELECT current_database()"))
    _run(conn, "CREATE TABLE cw (ts timestamptz, v int) USING pgcolumnar")
    _small_groups(conn, "cw")
    _run(conn, "INSERT INTO cw SELECT '2025-09-09'::timestamptz + g * interval '2 s', g FROM generate_series(1, 1500) g")

    # Ordinary writers are serialized by the storage-row lock from first flush to
    # commit; parallel_copy's loaders are the ones that flush into one storage at
    # once, each through PREPARE. The first and last rows of the file are far apart.
    path = _one(conn, "SELECT current_setting('data_directory')") + "/ce_py_cw.txt"
    _run(conn, "COPY (SELECT '2018-03-03'::timestamptz + g * interval '13 hours', g"
               f" FROM generate_series(1, 5200) g) TO '{path}' WITH (FORMAT text)")
    expect.num(_one(conn, f"SELECT pgcolumnar.parallel_copy('cw', '{path}', 3)"), 5200,
               "parallel_copy's loaders, flushing into one table at once, all commit")
    expect.text(_ext(conn, "cw", "ts"), _truth(conn, "cw", "ts"), "and the extent covers what they loaded")

    with psycopg.connect(dsn) as c:
        _run(c, "INSERT INTO cw SELECT '1995-01-01'::timestamptz + g * interval '2 s', g FROM generate_series(1, 2600) g")
        c.rollback()
    expect.text(_ext(conn, "cw", "ts"), _truth(conn, "cw", "ts"),
                "a writer that rolls back leaves the extent as the data has it")

    with psycopg.connect(dsn) as c:
        _run(c, "INSERT INTO cw VALUES ('2025-09-10', 1)", "SAVEPOINT s",
             "INSERT INTO cw SELECT '1985-01-01'::timestamptz + g * interval '2 s', g FROM generate_series(1, 2600) g",
             "ROLLBACK TO s")
        c.commit()
    expect.text(_one(conn, "SELECT CASE WHEN e.complete AND e.minimum::timestamptz <= (SELECT min(ts) FROM cw)"
                           " AND e.maximum::timestamptz >= (SELECT max(ts) FROM cw) THEN 'yes' ELSE 'no' END"
                           " FROM pgcolumnar.column_extent('cw', 'ts') e"), "yes",
                "a savepoint rolled back after flushing may widen the extent, never narrow it")

    _run(conn, "CREATE TABLE tp (ts timestamptz, v int) USING pgcolumnar")
    _small_groups(conn, "tp")
    _run(conn, "INSERT INTO tp SELECT '2025-09-09'::timestamptz + g * interval '2 s', g FROM generate_series(1, 1500) g")
    # COMMIT PREPARED from another session: in the preparing session it crashes the
    # backend on main, a separate bug (the write states outlive PREPARE).
    with psycopg.connect(dsn, autocommit=True) as c:
        _run(c, "BEGIN",
             "INSERT INTO tp SELECT '2044-02-02'::timestamptz + g * interval '2 s', g FROM generate_series(1, 2600) g",
             "PREPARE TRANSACTION 'ce_py_2pc'")
    _run(conn, "COMMIT PREPARED 'ce_py_2pc'")
    expect.text(_ext(conn, "tp", "ts"), _truth(conn, "tp", "ts"),
                "a prepared transaction's groups are inside the extent after COMMIT PREPARED")

    with psycopg.connect(dsn) as c:
        _run(c, "SAVEPOINT s", "CREATE TABLE gone (ts timestamptz) USING pgcolumnar",
             "INSERT INTO gone SELECT now() FROM generate_series(1, 2500)", "ROLLBACK TO s")
        c.commit()
    expect.num(_one(conn, "SELECT count(*) FROM pgcolumnar.column_extent e WHERE NOT EXISTS"
                          " (SELECT 1 FROM pgcolumnar.storage s WHERE s.storage_id = e.storage_id)"), 0,
               "a table created in a rolled-back savepoint leaves no extent rows")


def test_a_transaction_reads_its_own_flushed_groups(pgc_own_db, pgc_cluster, expect):
    """Its extent row is written at pre-commit, but its groups are visible to it as
    soon as they flush; a plan made in between must not bound the column by the
    committed rows alone. The oracle is the same transaction with the extent rows
    deleted, which sends the planner to the zone maps."""
    conn = pgc_own_db
    dsn = pgc_cluster.dsn(dbname=_one(conn, "SELECT current_database()"))
    _run(conn, "CREATE TABLE ow (ts timestamptz, v int) USING pgcolumnar")
    _small_groups(conn, "ow")
    _run(conn, "INSERT INTO ow SELECT '2025-03-03'::timestamptz + g * interval '2 s', g FROM generate_series(1, 1800) g",
         "ANALYZE ow")
    sid = _sid(conn, "ow")
    own = "INSERT INTO ow SELECT '1999-09-09'::timestamptz + g * interval '1 min', g FROM generate_series(1, 2700) g"
    plan = "EXPLAIN SELECT date_trunc('minute', ts) b, avg(v) FROM ow GROUP BY b"

    def estimate(cur):
        cur.execute(plan)
        text = "\n".join(r[0] for r in cur.fetchall())
        return text.split("rows=", 1)[1].split(" ", 1)[0] if "rows=" in text else ""

    with psycopg.connect(dsn) as c:
        with c.cursor() as cur:
            cur.execute(own)
            cur.execute("SELECT CASE WHEN e.complete AND e.minimum::timestamptz <= (SELECT min(ts) FROM ow)"
                        " AND e.maximum::timestamptz >= (SELECT max(ts) FROM ow) THEN 'yes' ELSE 'no' END"
                        " FROM pgcolumnar.column_extent('ow', 'ts') e")
            row = cur.fetchone()
            inside = row[0] if row else "none"
        c.rollback()
    expect.text(inside, "yes", "a transaction's own flushed groups are inside the extent it reads before commit")

    with psycopg.connect(dsn) as c:
        with c.cursor() as cur:
            cur.execute(GB)
            cur.execute(own)
            with_extent = estimate(cur)
            cur.execute(f"DELETE FROM pgcolumnar.column_extent WHERE storage_id = {sid}")
            from_zone_maps = estimate(cur)
        c.rollback()
    expect.text("two" if with_extent and from_zone_maps else f"[{with_extent}] [{from_zone_maps}]", "two",
                "premise: the transaction's plans gave two estimates")
    expect.text(f"rows={with_extent}", f"rows={from_zone_maps}",
                "and a plan made in that transaction estimates as the zone maps do")


def test_a_shrink_is_recomputed_never_guessed(pgc_own_db, expect):
    conn = pgc_own_db
    _run(conn, "CREATE TABLE ex (ts timestamptz, v int) USING pgcolumnar")
    _run(conn, "SELECT pgcolumnar.set_options('ex', stripe_row_limit => 1000, chunk_group_row_limit => 1000,"
               " ttl_column => 'ts', ttl_interval => '60 days')")
    _run(conn, "INSERT INTO ex SELECT now() - interval '700 days' + g * interval '90 s', g FROM generate_series(1, 2500) g",
         "INSERT INTO ex SELECT now() - interval '2 days' + g * interval '7 s', g FROM generate_series(1, 2500) g")
    before = _ext(conn, "ex", "ts")
    # The extent was there before expire (#1339): "it did move" means nothing otherwise.
    expect.text(before, _truth(conn, "ex", "ts"), "before expire the extent covers the table")
    retired = _one(conn, "SELECT pgcolumnar.expire('ex')")
    expect.text("yes" if (retired or 0) >= 1 else f"no ({retired})", "yes",
                "expire retires the old row groups with the extent in place")
    after = _ext(conn, "ex", "ts")
    expect.text(after, _truth(conn, "ex", "ts"), "after expire the extent is recomputed to the data that is left")
    expect.text("yes" if before != after else "no", "yes",
                "and it did move: the retired groups held the old minimum")

    _run(conn, "CREATE TABLE cr (ts timestamptz, v int) USING pgcolumnar")
    _small_groups(conn, "cr")
    _run(conn, "INSERT INTO cr SELECT '2025-05-05'::timestamptz + g * interval '5 min', g FROM generate_series(1, 4500) g",
         "DELETE FROM cr WHERE v > 4200 OR v <= 650")
    rewritten = _one(conn, "SELECT pgcolumnar.compact_rewrite('cr', 0.1)")
    expect.text("yes" if (rewritten or 0) >= 1 else f"no ({rewritten})", "yes",
                "compact_rewrite rewrites the groups the deletes thinned")
    expect.text(_ext(conn, "cr", "ts"), _truth(conn, "cr", "ts"), "after compact_rewrite the extent is the live rows'")

    _run(conn, "CREATE TABLE vr (ts timestamptz, v int) USING pgcolumnar")
    _small_groups(conn, "vr")
    _run(conn, "INSERT INTO vr SELECT '2025-05-05'::timestamptz + g * interval '5 min', g FROM generate_series(1, 3400) g",
         "DELETE FROM vr WHERE v > 2000", "VACUUM vr")
    expect.text(_ext(conn, "vr", "ts"), _truth(conn, "vr", "ts"),
                "after VACUUM retires a fully deleted group the extent is the live rows'")

    # recluster, named by the changelog with the three above (#1341 item 75): the
    # deleted rows are not written back, and they held the minimum.
    _run(conn, "CREATE TABLE rc (ts timestamptz, v int) USING pgcolumnar")
    _small_groups(conn, "rc")
    _run(conn, "INSERT INTO rc SELECT '2025-05-05'::timestamptz + g * interval '3 min', (g * 104729) % 2711"
               " FROM generate_series(1, 3600) g",
         "DELETE FROM rc WHERE ts < '2025-05-05'::timestamptz + interval '1500 min'")
    before = _ext(conn, "rc", "ts")
    reclustered = _one(conn, "SELECT pgcolumnar.recluster('rc', 'ts', 'v')")
    expect.text("yes" if (reclustered or 0) >= 1 else f"no ({reclustered})", "yes",
                "recluster rewrites the table's row groups")
    after = _ext(conn, "rc", "ts")
    expect.text(after, _truth(conn, "rc", "ts"), "after recluster the extent is the live rows'")
    expect.text("yes" if before and before != after else f"no ({before})", "yes",
                "and it did move: the deleted rows held the old minimum")


def test_no_complete_extent_where_the_zone_maps_cannot_give_one(pgc_own_db, expect):
    conn = pgc_own_db
    _run(conn, "CREATE TABLE ac (ts timestamptz) USING pgcolumnar")
    _small_groups(conn, "ac")
    _run(conn, "INSERT INTO ac SELECT '2025-03-03'::timestamptz + g * interval '9 s' FROM generate_series(1, 2400) g",
         "ALTER TABLE ac ADD COLUMN v int DEFAULT 11",
         "INSERT INTO ac SELECT '2025-04-04'::timestamptz + g * interval '9 s', g FROM generate_series(1, 2400) g")
    expect.text(_ext(conn, "ac", "v"), "none",
                "a column added after older groups has no complete extent (those groups have no zone map for it)")
    expect.text(_ext(conn, "ac", "ts"), _truth(conn, "ac", "ts"),
                "while the column that was there from the start still has one")
    # A recompute (VACUUM after a fully deleted group) must refuse a column whose
    # values have no ordering, not claim it holds none.
    _run(conn, "CREATE TABLE no (p point, k int) USING pgcolumnar")
    _small_groups(conn, "no")
    _run(conn, "INSERT INTO no SELECT point(g, -g), g FROM generate_series(1, 2400) g",
         "DELETE FROM no WHERE k > 2000", "VACUUM no")
    expect.text(_ext(conn, "no", "p"), "none", "a type with no ordering gets no extent")


def test_housekeeping_and_privilege(pgc_own_db, pgc_cluster, expect):
    conn = pgc_own_db
    dsn = pgc_cluster.dsn(dbname=_one(conn, "SELECT current_database()"))
    _run(conn, "CREATE TABLE hk (ts timestamptz, v int) USING pgcolumnar")
    for k in range(1, 51):
        _run(conn, f"INSERT INTO hk VALUES ('2025-01-01'::timestamptz + interval '{k} hours', {k})")
    rows = _one(conn, f"SELECT count(*) FROM pgcolumnar.column_extent WHERE storage_id = {_sid(conn, 'hk')}"
                      " AND column_index = 0")
    expect.text("yes" if 1 <= rows <= 9 else f"no ({rows})", "yes",
                "fifty committing writers leave a bounded number of extent rows")
    expect.text(_ext(conn, "hk", "ts"), _truth(conn, "hk", "ts"), "and the extent still covers all fifty")
    old = _sid(conn, "hk")
    _run(conn, "TRUNCATE hk")
    expect.num(_one(conn, f"SELECT count(*) FROM pgcolumnar.column_extent WHERE storage_id = {old}"), 0,
               "TRUNCATE removes the old storage's extent rows")
    _run(conn, "CREATE TABLE dr (ts timestamptz) USING pgcolumnar",
         "INSERT INTO dr SELECT now() FROM generate_series(1, 10)")
    old = _sid(conn, "dr")
    # Rows to remove (#1339): a count of zero after the DROP proves nothing without it.
    expect.text(str(_one(conn, f"SELECT count(*) > 0 FROM pgcolumnar.column_extent WHERE storage_id = {old}")),
                "True", "before DROP the table has extent rows to remove")
    _run(conn, "DROP TABLE dr")
    expect.num(_one(conn, f"SELECT count(*) FROM pgcolumnar.column_extent WHERE storage_id = {old}"), 0,
               "DROP TABLE removes them")

    _run(conn, "DO $$ BEGIN CREATE ROLE ce_py; EXCEPTION WHEN duplicate_object THEN NULL; END $$",
         "GRANT USAGE ON SCHEMA pgcolumnar TO ce_py",
         "CREATE TABLE IF NOT EXISTS priv (ts timestamptz) USING pgcolumnar",
         "INSERT INTO priv VALUES (now())")

    def as_role(sql):
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("SET ROLE ce_py")
            try:
                c.execute(sql).fetchall()
                return "ok"
            except psycopg.Error as e:
                return e.sqlstate or "?"
    expect.text(as_role("SELECT count(*) FROM pgcolumnar.column_extent"), "42501",
                "the extent catalog is not readable by an ordinary role (it holds column values)")
    expect.text(as_role("SELECT * FROM pgcolumnar.column_extent('public.priv', 'ts')"), "42501",
                "nor is a table's extent through the function, without SELECT on the table")
    # The control (#1339): given SELECT, the same role reads it. Without this, a
    # function refused to everyone passed the arm above.
    _run(conn, "GRANT SELECT ON priv TO ce_py")
    expect.text(as_role("SELECT * FROM pgcolumnar.column_extent('public.priv', 'ts')"), "ok",
                "and with SELECT on the table the same role reads its extent")
