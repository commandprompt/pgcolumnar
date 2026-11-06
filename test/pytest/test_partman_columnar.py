"""pg_partman manages columnar partitions, with one setting and not without it (#1291).

The owner's question on #1291: does pg_partman work with columnar partitions on
every major? If it does, we document and test that. Measured with pg_partman
5.4.1 on PG 15..19:

- Out of the box every partition it makes is HEAP, even on 17+ where the parent
  itself carries USING pgcolumnar. It does not take the parent's access method.
- With `default_table_access_method = pgcolumnar` where its maintenance runs,
  every partition it makes is columnar, the rows land there, and retention drops
  partitions leaving no pgcolumnar catalog row behind.
- `run_maintenance` holds ACCESS EXCLUSIVE on the parent. That is pg_partman's
  behaviour; it is asserted because docs/how-to.md tells users so.

The first arm pins the TRAP, on purpose: it is why the documentation exists, and
a pg_partman that began honouring the parent's access method must turn it red
rather than leave the warning stale.

ON A PRIVATE DATABASE (`pgc_own_db`): the recipe is a database-level setting, and
retention is checked against the whole storage catalog. pg_partman missing is
MISSING_DEPENDENCY (unrunnable, which fails the run), never a pass; CI installs
postgresql-N-partman from PGDG.

Independent of `test/partman_columnar.sh`: its own tables, partition widths,
dates, retention window and row counts.
"""


def _one(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    return row[0] if row else None


def _am_counts(conn, parent):
    return _one(conn,
        "SELECT coalesce(string_agg(amname || '=' || n, ' ' ORDER BY amname), 'none')"
        " FROM (SELECT a.amname, count(*) n FROM pg_inherits i"
        " JOIN pg_class c ON c.oid = i.inhrelid JOIN pg_am a ON a.oid = c.relam"
        f" WHERE i.inhparent = '{parent}'::regclass GROUP BY 1) x")


def _kids(conn, parent):
    return _one(conn, f"SELECT count(*) FROM pg_inherits WHERE inhparent = '{parent}'::regclass")


def test_pg_partman_makes_columnar_partitions_with_one_setting(pgc_own_db, expect):
    conn = pgc_own_db
    if _one(conn, "SELECT count(*) FROM pg_available_extensions WHERE name = 'pg_partman'") != 1:
        expect.cannot_run("MISSING_DEPENDENCY", "pg_partman is not installed for this major",
                          name="pg_partman is not installed for this major")
        return
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA pm")
        cur.execute("CREATE EXTENSION pg_partman SCHEMA pm")
    expect.num(_one(conn, "SELECT count(*) FROM pg_extension WHERE extname = 'pg_partman'"), 1,
               "premise: pg_partman is installed in this database")
    major = int(_one(conn, "SHOW server_version_num")) // 10000
    using = "USING pgcolumnar" if major >= 17 else ""

    # The trap: no setting, heap partitions.
    with conn.cursor() as cur:
        cur.execute(f"CREATE TABLE raw_events (at timestamptz NOT NULL, n bigint)"
                    f" PARTITION BY RANGE (at) {using}")
        cur.execute("SELECT pm.create_parent('public.raw_events', 'at', '1 week', p_premake := 2,"
                    " p_start_partition := (date_trunc('week', now()) - interval '1 week')::text)")
    expect.text(" ".join(x.split("=")[0] for x in _am_counts(conn, "raw_events").split()), "heap",
                "premise: without default_table_access_method pg_partman makes heap partitions")

    # The recipe.
    dbname = _one(conn, "SELECT current_database()")
    with conn.cursor() as cur:
        cur.execute(f'ALTER DATABASE "{dbname}" SET default_table_access_method = \'pgcolumnar\'')
        cur.execute("SET default_table_access_method = 'pgcolumnar'")
        cur.execute(f"CREATE TABLE metrics (at timestamptz NOT NULL, n bigint)"
                    f" PARTITION BY RANGE (at) {using}")
        cur.execute("SELECT pm.create_parent('public.metrics', 'at', '1 day', p_premake := 2,"
                    " p_start_partition := (date_trunc('day', now()) - interval '30 days')::text)")
    expect.num(_one(conn, "SELECT count(*) FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid"
                          " WHERE i.inhparent = 'metrics'::regclass"
                          " AND pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT'"), 1,
               "premise: pg_partman creates a DEFAULT partition, as docs/how-to.md says")
    kids = _kids(conn, "metrics")
    expect.text(_am_counts(conn, "metrics"), f"pgcolumnar={kids}",
                "premise: with default_table_access_method every partition pg_partman makes is columnar")

    with conn.cursor() as cur:
        cur.execute("CREATE TABLE metrics_heap (at timestamptz, n bigint) USING heap")
        for t in ("metrics", "metrics_heap"):
            cur.execute(f"INSERT INTO {t} SELECT date_trunc('day', now()) - interval '30 days'"
                        f" + g * interval '11 min', g * 3 FROM generate_series(1, 3600) g")
    expect.num(_one(conn, "SELECT count(*) FROM metrics"), 3600, "the rows went in")
    expect.text(_one(conn, "SELECT string_agg(DISTINCT a.amname, ',') FROM metrics t"
                           " JOIN pg_class c ON c.oid = t.tableoid JOIN pg_am a ON a.oid = c.relam"),
                "pgcolumnar", "and the rows land in columnar partitions")
    # Every row, in order, hashed: an unfiltered count/sum/min/max is answered from
    # zone maps and would not exercise the scan at all.
    agg = "SELECT md5(string_agg(at::text || ':' || n, ',' ORDER BY at, n)) FROM {}"
    expect.text(_one(conn, agg.format("metrics")), _one(conn, agg.format("metrics_heap")),
                "and read back as heap does")

    # Maintenance: created ahead, retention, and the parent lock.
    with conn.cursor() as cur:
        cur.execute("UPDATE pm.part_config SET premake = 5, retention = '12 days',"
                    " retention_keep_table = false WHERE parent_table = 'public.metrics'")
    before = _kids(conn, "metrics")
    # One explicit transaction, so the lock is still held when it is read.
    with conn.cursor() as cur:
        cur.execute("BEGIN")
        cur.execute("SELECT pm.run_maintenance('public.metrics')")
        cur.execute("SELECT coalesce(string_agg(DISTINCT mode, ','), 'none') FROM pg_locks"
                    " WHERE relation = 'metrics'::regclass AND pid = pg_backend_pid()")
        lock = cur.fetchone()[0]
        cur.execute("COMMIT")
    after = _kids(conn, "metrics")
    print(f"-- partitions {before} -> {after}")
    expect.text("dropped" if after < before else f"dropped {before - after}", "dropped",
                "premise: retention dropped partitions")
    expect.text(_am_counts(conn, "metrics"), f"pgcolumnar={after}",
                "premise: the partitions maintenance creates ahead are columnar too")
    expect.num(_one(conn, "SELECT count(*) FROM pgcolumnar.storage s WHERE NOT EXISTS"
                          " (SELECT 1 FROM pg_class c WHERE c.oid = s.relation_oid)"), 0,
               "retention leaves no pgcolumnar storage row behind")
    expect.num(_one(conn, "SELECT count(*) FROM pgcolumnar.row_group r WHERE NOT EXISTS"
                          " (SELECT 1 FROM pgcolumnar.storage s WHERE s.storage_id = r.storage_id)"), 0,
               "and no row-group row behind")
    expect.text(lock, "AccessExclusiveLock",
                "premise: run_maintenance holds an exclusive lock on the parent, as docs/how-to.md says")
