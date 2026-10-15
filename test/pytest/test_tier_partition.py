"""pgcolumnar.tier_partition moves a cold partition to Parquet, exactly once (#1295).

The owner's decision on #1295: atomic, with a brief ACCESS EXCLUSIVE on the
parent. Export under the partition's SHARE lock, then detach, drop, create the
foreign table under the partition's own name, and attach it with its own bounds.

"EXACTLY ONCE" IS TESTED, NOT ARGUED. `pgcolumnar.tier_fault_after` raises an
error right after the step it names, and each fault arm first requires that the
error raised IS the injected one: a missing function also fails and leaves the
rows where they were.

Two findings the shell twin made while the code was written, held here too:
- `pg_stat_file(...) IS NOT NULL` is false for an existing file, so the
  existing-path refusal never fired, and the export overwrote a file a foreign
  partition was reading.
- without the SHARE lock, a write in flight when the move starts was lost: the
  export did not see it, DETACH waited for it, DROP took it with the partition.

ON A PRIVATE DATABASE (`pgc_own_db`): foreign servers are per database. A local
path stands in for object storage. Independent of `test/tier_partition.sh`: its
own columns, months, row counts, paths and in-flight row.
"""

import threading
import time

import psycopg

BOUND = "FOR VALUES FROM ('2026-03-01 00:00:00+00') TO ('2026-04-01 00:00:00+00')"


def _one(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    return row[0] if row else None


def _sqlstate(conn, sql):
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        return "ok"
    except psycopg.Error as e:
        return e.sqlstate or "?"


def _errtext(conn, sql):
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        return "ok"
    except psycopg.Error as e:
        return str(e).splitlines()[0]


def _build(conn, t):
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
        cur.execute(f"CREATE TABLE {t} (at timestamptz NOT NULL, sensor int, reading float8,"
                    " label text) PARTITION BY RANGE (at)")
        cur.execute(f"CREATE TABLE {t}_mar PARTITION OF {t} FOR VALUES FROM ('2026-03-01')"
                    " TO ('2026-04-01') USING pgcolumnar")
        cur.execute(f"CREATE TABLE {t}_apr PARTITION OF {t} FOR VALUES FROM ('2026-04-01')"
                    " TO ('2026-05-01') USING pgcolumnar")
        cur.execute(f"INSERT INTO {t} SELECT '2026-03-01'::timestamptz + g * interval '7 min',"
                    " g % 17, (g * 31 % 997) / 7.0, 'r' || (g * 13 % 101)"
                    " FROM generate_series(0, 12340) g")


def _digest(conn, t):
    # Every row decoded: an unfiltered count/sum is answered from the zone maps.
    return _one(conn, f"SELECT count(*) || '|' || md5(string_agg(at::text || sensor || reading"
                      f" || label, ',' ORDER BY at)) FROM {t}")


def _mar_shape(conn, t):
    return _one(conn, "SELECT c.relkind::text || ':' || coalesce(a.amname, '-') || ':'"
                      " || pg_get_expr(c.relpartbound, c.oid) FROM pg_inherits i"
                      " JOIN pg_class c ON c.oid = i.inhrelid LEFT JOIN pg_am a ON a.oid = c.relam"
                      f" WHERE i.inhparent = '{t}'::regclass AND c.relname = '{t}_mar'")


def _tier(t, path):
    return f"SELECT pgcolumnar.tier_partition('{t}_mar', 'tq', '{path}')"


def test_tier_partition_moves_a_partition_exactly_once(pgc_own_db, pgc_cluster, server_dir, expect):
    conn = pgc_own_db
    with conn.cursor() as cur:
        cur.execute("SET timezone = 'UTC'")
        cur.execute("CREATE SERVER tq FOREIGN DATA WRAPPER pgcolumnar_parquet")
    _build(conn, "s")
    expect.num(_one(conn, "SELECT count(*) FROM s"), 12341, "premise: the parent holds every row")
    mar = _one(conn, "SELECT count(*) FROM s_mar")
    expect.num(mar, 6378, "premise: the cold partition holds its share")
    before = _digest(conn, "s")
    expect.text(_mar_shape(conn, "s"), f"r:pgcolumnar:{BOUND}",
                "premise: the cold partition is local and columnar, with its bound")
    sid = _one(conn, "SELECT pgcolumnar.get_storage_id('s_mar')")

    path = server_dir / "s_mar.parquet"
    expect.text(str(_one(conn, _tier("s", path))), str(mar),
                "tier_partition moves the cold partition and returns the rows it moved")
    kind = _one(conn, "SELECT coalesce((SELECT relkind::text FROM pg_class"
                      " WHERE oid = to_regclass('s_mar')), 'absent')")
    # The relkind rides along: with nothing moved the digest is trivially unchanged.
    expect.text(f"{_digest(conn, 's')}|{kind}", f"{before}|f",
                "the answers over the parent are the same after the move")
    expect.text(_mar_shape(conn, "s"), f"f:-:{BOUND}",
                "the cold partition is now a foreign partition with its own name and bound")
    expect.text(_one(conn, "SELECT srvname || ':' || array_to_string(ftoptions, ',')"
                           " FROM pg_foreign_table t JOIN pg_foreign_server s ON s.oid = t.ftserver"
                           " WHERE t.ftrelid = 's_mar'::regclass"),
                f"tq:path={path}", "the foreign partition reads the exported file")
    expect.text("read" if sid is not None else "unread", "read",
                "premise: the storage id was read before the move")
    expect.num(_one(conn, f"SELECT count(*) FROM pgcolumnar.storage WHERE storage_id = {sid or 0}"), 0,
               "the local partition leaves no pgcolumnar storage row behind")
    expect.num(_one(conn, f"SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = {sid or 0}"), 0,
               "nor any row-group row")

    def plan(sql):
        with conn.cursor() as cur:
            cur.execute("EXPLAIN " + sql)
            return "\n".join(r[0] for r in cur.fetchall())
    p_mar = plan("SELECT min(reading) FROM s WHERE at < '2026-03-15'")
    p_apr = plan("SELECT min(reading) FROM s WHERE at >= '2026-04-12'")
    expect.text(f"{p_mar.count('Foreign Scan')}|{p_mar.count('s_apr')}", "1|0",
                "a query on the tiered range scans only the foreign partition")
    expect.text(f"{p_apr.count('Foreign Scan')}|{p_apr.count('s_apr')}|{p_mar.count('Foreign Scan')}",
                "0|1|1", "a query on the other range scans only the local partition, beside a foreign one")
    expect.text(_sqlstate(conn, "INSERT INTO s VALUES ('2026-03-09', 1, 1, 'x')"), "0A000",
                    "an insert routed to the tiered range is refused")

    # Refusals, before anything moves.
    rb = _digest(conn, "s")
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE solo (at timestamptz, v int) USING pgcolumnar")
        cur.execute("CREATE TABLE s_may PARTITION OF s FOR VALUES FROM ('2026-05-01')"
                    " TO ('2026-06-01') USING heap")
        cur.execute("CREATE TABLE s_rest PARTITION OF s DEFAULT USING pgcolumnar")
        cur.execute("CREATE SERVER ti FOREIGN DATA WRAPPER pgcolumnar_iceberg")
    expect.text(_sqlstate(conn, f"SELECT pgcolumnar.tier_partition('solo', 'tq', '{server_dir}/solo.parquet')"),
                    "42809", "a table that is not a partition is refused")
    expect.text(_sqlstate(conn, f"SELECT pgcolumnar.tier_partition('s_may', 'tq', '{server_dir}/may.parquet')"),
                    "42809", "a partition that is not columnar is refused")
    expect.text(_sqlstate(conn, f"SELECT pgcolumnar.tier_partition('s_rest', 'tq', '{server_dir}/rest.parquet')"),
                    "0A000", "a DEFAULT partition is refused")
    # Before anything moves (#1339): a refusal from a later step, after the export,
    # would leave the file behind with the same SQLSTATE.
    other = _sqlstate(conn, f"SELECT pgcolumnar.tier_partition('s_apr', 'ti', '{server_dir}/apr.parquet')")
    expect.text(f"{other}|{'file' if (server_dir / 'apr.parquet').exists() else 'nofile'}",
                "42809|nofile", "a server of another wrapper is refused")
    expect.text(_sqlstate(conn, f"SELECT pgcolumnar.tier_partition('s_apr', 'tq', '{path}')"),
                    "58P02", "an existing path is refused, so no file a table reads is overwritten")
    expect.text(_digest(conn, "s"), rb, "and the refusals moved nothing")
    expect.text(_one(conn, "SELECT relkind::text FROM pg_class WHERE oid = 's_apr'::regclass"), "r",
                "the other partition is still local after the refusals")

    # The parent's lock, and a rollback of the whole call.
    _build(conn, "k")
    kb = _digest(conn, "k")
    with conn.cursor() as cur:
        cur.execute("BEGIN")
        cur.execute(_tier("k", server_dir / "k_mar.parquet"))
        moved = cur.fetchone()[0]
        cur.execute("SELECT coalesce(string_agg(mode, ',' ORDER BY mode), 'none') FROM pg_locks"
                    " WHERE relation = 'k'::regclass AND pid = pg_backend_pid() AND granted")
        locks = cur.fetchone()[0]
        cur.execute("ROLLBACK")
    expect.num(moved, mar, "inside the transaction the call moved the cold partition")
    expect.text("AccessExclusiveLock" if "AccessExclusiveLock" in locks else locks, "AccessExclusiveLock",
                "the call holds ACCESS EXCLUSIVE on the parent until commit, as the docs say")
    expect.text(f"{_digest(conn, 'k')}|{_mar_shape(conn, 'k')}", f"{kb}|r:pgcolumnar:{BOUND}",
                "a rollback after the whole call leaves the rows local, exactly once")

    # A write in flight when the move starts.
    _build(conn, "w")
    wb = _one(conn, "SELECT count(*) FROM w")
    writer = psycopg.connect(pgc_cluster.dsn(dbname=_one(conn, "SELECT current_database()")))
    try:
        writer.execute("INSERT INTO w_mar VALUES ('2026-03-31 23:58:00+00', 5, 5.5, 'in flight')")
        out = {}

        def mover():
            t0 = time.monotonic()
            out["moved"] = _one(conn, _tier("w", server_dir / "w_mar.parquet"))
            out["ms"] = int((time.monotonic() - t0) * 1000)
        th = threading.Thread(target=mover)
        th.start()
        time.sleep(1.5)
        writer.commit()
        th.join(60)
    finally:
        writer.close()
    print(f"-- the move waited {out.get('ms')} ms for the writer")
    expect.at_least(out.get("ms", 0), 1000,
                    "premise: the move started while the insert was in flight, and waited for it")
    total = _one(conn, "SELECT count(*) FROM w")
    flight = _one(conn, "SELECT count(*) FROM w WHERE label = 'in flight'")
    expect.text(f"{out.get('moved')}|{total}|{flight}",
                f"{mar + 1}|{wb + 1}|1", "a write in flight when the move starts is in the exported rows")

    # A failure after each step.
    for step in ("export", "detach", "drop", "create", "attach"):
        _build(conn, "f")
        fb = _digest(conn, "f")
        with conn.cursor() as cur:
            cur.execute(f"SET pgcolumnar.tier_fault_after = '{step}'")
        err = _errtext(conn, _tier("f", server_dir / f"f_{step}.parquet"))
        with conn.cursor() as cur:
            cur.execute("RESET pgcolumnar.tier_fault_after")
        expect.text(f"injected fault after {step}" if f"injected fault after {step}" in err else err,
                    f"injected fault after {step}",
                    f"premise: the call fails with the fault injected after {step}")
        expect.text(f"{_digest(conn, 'f')}|{_mar_shape(conn, 'f')}", f"{fb}|r:pgcolumnar:{BOUND}",
                    f"a failure after {step} leaves the rows exactly once, still local")
        expect.num(_one(conn, "SELECT count(*) FROM pg_foreign_table t JOIN pg_class c ON c.oid = t.ftrelid"
                              " WHERE c.relname LIKE 'f\\_%'"), 0,
                   f"and no foreign table behind it after {step}")


def _dsn(pgc_cluster, conn):
    return pgc_cluster.dsn(dbname=_one(conn, "SELECT current_database()"))


def _relkind(conn, t):
    return _one(conn, f"SELECT coalesce((SELECT relkind::text FROM pg_class WHERE oid = to_regclass('{t}_mar')), 'absent')")


def test_tier_partition_refuses_what_it_cannot_move_and_survives_concurrency(
        pgc_own_db, pgc_cluster, server_dir, expect):
    """The #1295 review's five points, each an arm (@jdatcmd).

    1. A writer that commits between the caller's snapshot and the lock: at
       REPEATABLE READ or SERIALIZABLE the export would miss its row and DROP
       would take it, so those levels are refused; at READ COMMITTED it moves.
    2. A UNIQUE index on the parent, and 3. anything that depends on the
       partition, are refused before anything moves, leaving no file.
    4. An insert racing the swap, and a parent held by a long reader.
    5. A retry over the file a failed attempt left.
    """
    conn = pgc_own_db
    dsn = _dsn(pgc_cluster, conn)
    with conn.cursor() as cur:
        cur.execute("SET timezone = 'UTC'")
        cur.execute("CREATE SERVER tq FOREIGN DATA WRAPPER pgcolumnar_parquet")
    mar = 6378

    # 1. Isolation.
    for n, lvl in ((0, "read committed"), (1, "repeatable read"), (2, "serializable")):
        t = f"iso{n}"
        _build(conn, t)
        before = _one(conn, f"SELECT count(*) FROM {t}")
        out = {}

        def mover():
            with psycopg.connect(dsn, autocommit=True) as m:
                m.execute(f"BEGIN ISOLATION LEVEL {lvl}")
                out["seen"] = m.execute(f"SELECT count(*) FROM {t}_mar").fetchone()[0]
                time.sleep(1.5)
                try:
                    out["moved"] = m.execute(_tier(t, server_dir / f"{t}.parquet")).fetchone()[0]
                    m.execute("COMMIT")
                except psycopg.Error as e:
                    out["state"] = e.sqlstate
                    m.execute("ROLLBACK")
        th = threading.Thread(target=mover)
        th.start()
        time.sleep(0.7)
        with conn.cursor() as cur:
            cur.execute(f"INSERT INTO {t}_mar VALUES ('2026-03-20 09:30:00+00', 4, 4.5, 'committed between')")
        th.join(60)
        expect.num(out.get("seen"), mar, f"premise: at {lvl} the caller's snapshot was taken before the writer committed")
        expect.text(f"{_one(conn, f'SELECT count(*) FROM {t}')}|"
                    f"{_one(conn, f'SELECT count(*) FROM {t} WHERE label = ' + chr(39) + 'committed between' + chr(39))}",
                    f"{before + 1}|1", f"at {lvl} a row committed between the caller's snapshot and the lock survives")
        if lvl == "read committed":
            expect.text(f"{out.get('moved')}|{_relkind(conn, t)}", f"{mar + 1}|f",
                        "at read committed the move goes ahead and exports that row")
        else:
            expect.text(f"{out.get('state')}|{_relkind(conn, t)}", "25000|r",
                        f"at {lvl} the move is refused, and the rows stay local")

    # 2 and 3. Refused before anything moves, and no file left.
    def refused(t, path):
        return f"{_digest(conn, t)}|{_relkind(conn, t)}|{'file' if path.exists() else 'nofile'}"

    _build(conn, "uq")
    with conn.cursor() as cur:
        cur.execute("CREATE UNIQUE INDEX uq_key ON uq (at)")
    ub = _digest(conn, "uq")
    p = server_dir / "uq.parquet"
    expect.text(f"{_sqlstate(conn, _tier('uq', p))}|{refused('uq', p)}", f"0A000|{ub}|r|nofile",
                "a parent with a UNIQUE index is refused before anything moves")

    # A server that does not exist (#1341): refused by its own branch, before the
    # export. Core's own error would come later, with the file already written.
    _build(conn, "ns")
    nb = _digest(conn, "ns")
    p = server_dir / "ns.parquet"
    ns_state = _sqlstate(conn, f"SELECT pgcolumnar.tier_partition('ns_mar', 'tq_no_such', '{p}')")
    expect.text(f"{ns_state}|{refused('ns', p)}", f"42704|{nb}|r|nofile",
                "a server that does not exist is refused before anything moves")

    # The fault hook's own guard (#1341).
    expect.text(_sqlstate(conn, "SET pgcolumnar.tier_fault_after = 'exprot'"), "22023",
                "a fault step that is not one of tier_partition's is refused")

    _build(conn, "dep")
    db = _digest(conn, "dep")
    # Each dependent made and removed one statement at a time, with a premise that
    # it exists: a multi-statement string is one transaction, and a failed CREATE
    # once rolled back the previous case's DROP, so an arm passed on a leftover.
    # Names WRITTEN AT THE CALL: compare_to_bash resolves string literals there.
    def run_each(stmts):
        for st in stmts:
            with conn.cursor() as cur:
                cur.execute(st)

    def dep_case(i, make, premise_sql, undo):
        run_each(make)
        p = server_dir / f"dep{i}.parquet"
        got = f"{_one(conn, premise_sql)}|{_sqlstate(conn, _tier('dep', p))}|{refused('dep', p)}"
        run_each(undo)
        return got

    expect.text(dep_case(0, ["CREATE VIEW dep_view AS SELECT at, reading FROM dep_mar"],
                         "SELECT count(*) FROM pg_views WHERE viewname = 'dep_view'", ["DROP VIEW dep_view"]),
                f"1|2BP01|{db}|r|nofile", "a partition a view depends on is refused before anything moves")
    expect.text(dep_case(1, ["ALTER TABLE dep_mar ENABLE ROW LEVEL SECURITY",
                             "CREATE POLICY dep_pol ON dep_mar USING (sensor <> 3)"],
                         "SELECT count(*) FROM pg_policy WHERE polname = 'dep_pol'",
                         ["DROP POLICY dep_pol ON dep_mar", "ALTER TABLE dep_mar DISABLE ROW LEVEL SECURITY"]),
                f"1|2BP01|{db}|r|nofile", "a partition with a row-level policy is refused before anything moves")
    expect.text(dep_case(2, ["CREATE FUNCTION dep_trg() RETURNS trigger LANGUAGE plpgsql AS $$BEGIN RETURN NEW; END$$",
                             "CREATE TRIGGER dep_own BEFORE DELETE ON dep_mar FOR EACH ROW EXECUTE FUNCTION dep_trg()"],
                         "SELECT count(*) FROM pg_trigger WHERE tgname = 'dep_own'", ["DROP TRIGGER dep_own ON dep_mar"]),
                f"1|2BP01|{db}|r|nofile", "a partition with a trigger of its own is refused before anything moves")
    # A foreign key REFERENCING a columnar table is refused by pgcolumnar itself,
    # so the case that arises is one the partition holds.
    expect.text(dep_case(3, ["CREATE TABLE dep_sensor (id int PRIMARY KEY) USING heap",
                             "INSERT INTO dep_sensor SELECT g FROM generate_series(0, 16) g",
                             "ALTER TABLE dep_mar ADD CONSTRAINT dep_fk FOREIGN KEY (sensor) REFERENCES dep_sensor (id)"],
                         "SELECT count(*) FROM pg_constraint WHERE conname = 'dep_fk'",
                         ["ALTER TABLE dep_mar DROP CONSTRAINT dep_fk"]),
                f"1|2BP01|{db}|r|nofile", "a partition with a foreign key of its own is refused before anything moves")
    expect.num(_one(conn, _tier("dep", server_dir / "dep_free.parquet")), mar,
               "with the dependents gone, the same partition moves")

    # What the foreign table carries over.
    _build(conn, "acl")
    with conn.cursor() as cur:
        cur.execute("DO $$ BEGIN CREATE ROLE tier_reader_py; EXCEPTION WHEN duplicate_object THEN NULL; END $$")
        cur.execute("GRANT SELECT ON acl_mar TO tier_reader_py")
        cur.execute("COMMENT ON TABLE acl_mar IS 'March, archived'")
    expect.num(_one(conn, _tier("acl", server_dir / "acl.parquet")), mar,
               "the cold partition was moved with its grant and comment set")
    expect.text(_one(conn, "SELECT relkind::text || '|' || has_table_privilege('tier_reader_py', 'acl_mar', 'SELECT')"
                           " || '|' || coalesce(obj_description('acl_mar'::regclass, 'pg_class'), '-')"
                           " FROM pg_class WHERE oid = 'acl_mar'::regclass"),
                "f|true|March, archived", "the foreign partition keeps the partition's grants and comment")
    with conn.cursor() as cur:
        cur.execute("REVOKE ALL ON acl_mar FROM tier_reader_py")

    # 4. An insert through the parent racing the swap.
    _build(conn, "race")
    rb = _one(conn, "SELECT count(*) FROM race")
    out = {}

    def racer():
        with psycopg.connect(dsn, autocommit=True) as m:
            m.execute("SET pgcolumnar.tier_fault_after = 'pause'")
            try:
                out["moved"] = m.execute(_tier("race", server_dir / "race.parquet")).fetchone()[0]
            except psycopg.Error as e:
                out["mover"] = e.sqlstate
    th = threading.Thread(target=racer)
    th.start()
    time.sleep(0.8)
    try:
        with psycopg.connect(dsn, autocommit=True) as w:
            w.execute("INSERT INTO race VALUES ('2026-03-28 18:00:00+00', 9, 9.9, 'raced')")
        out["writer"] = "ok"
    except psycopg.Error as e:
        out["writer"] = e.sqlstate
    th.join(60)
    print(f"-- race: {out}")
    raced = _one(conn, "SELECT count(*) FROM race WHERE label = 'raced'")
    expect.num(1 if ("moved" in out) != ("mover" in out) else 0, 1,
               "premise: one side of the race gave way or both went through, and nothing hung")
    # The want comes from the writer, not from the table (#1339): a committed insert
    # is there exactly once, a refused one not at all. With `raced` on both sides, a
    # row the writer was told had committed and the move then dropped passed.
    w_ok = 1 if out.get("writer") == "ok" else 0
    expect.text(f"{_one(conn, 'SELECT count(*) FROM race')}|{raced}", f"{rb + w_ok}|{w_ok}",
                "an insert racing the move leaves every row exactly once, whichever side gives way")

    # A parent held by a long reader, and lock_wait.
    _build(conn, "held")
    hb = _digest(conn, "held")
    reader = psycopg.connect(dsn, autocommit=True)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM held").fetchone()
        t0 = time.monotonic()
        state = _sqlstate(conn, "SELECT pgcolumnar.tier_partition('held_mar', 'tq', "
                                f"'{server_dir}/held.parquet', lock_wait => '800 milliseconds')")
        ms = int((time.monotonic() - t0) * 1000)
        reader.execute("ROLLBACK")
    finally:
        reader.close()
    print(f"-- gave up after {ms} ms")
    expect.text(f"{state}|{'prompt' if ms < 3500 else f'took {ms} ms'}", "55P03|prompt",
                "a parent held by a long reader times the move out, as lock_wait says")
    expect.text(f"{_digest(conn, 'held')}|{_relkind(conn, 'held')}", f"{hb}|r",
                "and the timed-out move leaves the rows local, with nothing attached")

    # The caller's lock_timeout survives the call.
    _build(conn, "lt")
    with conn.cursor() as cur:
        cur.execute("BEGIN")
        cur.execute("SET LOCAL lock_timeout = '13s'")
        cur.execute("SELECT pgcolumnar.tier_partition('lt_mar', 'tq', "
                    f"'{server_dir}/lt.parquet', lock_wait => '2 seconds')")
        lt_moved = cur.fetchone()[0]
        cur.execute("SHOW lock_timeout")
        lt = cur.fetchone()[0]
        cur.execute("ROLLBACK")
    expect.num(lt_moved, mar, "the move ran inside the caller's transaction")
    expect.text(lt, "13s", "after the move the caller's lock_timeout is the caller's again")

    # Names and columns the foreign table must reproduce (#1317 review): each
    # failed after the export in the first version. Each moves a small partition,
    # which must then read back the same through its parent.
    def mini(t, extra=""):
        with conn.cursor() as cur:
            cur.execute(f"CREATE TABLE {t} (at timestamptz NOT NULL, reading int{extra}) PARTITION BY RANGE (at)")
            cur.execute(f"CREATE TABLE {t}_mar PARTITION OF {t} FOR VALUES FROM ('2026-03-01') TO ('2026-04-01')"
                        " USING pgcolumnar")
            cur.execute(f"INSERT INTO {t} (at, reading) SELECT '2026-03-01'::timestamptz + g * interval '5 h', g * 7"
                        " FROM generate_series(0, 119) g")

    def moved_reads(t, cols):
        before = _one(conn, f"SELECT md5(string_agg({cols}, ',' ORDER BY at)) FROM {t}")
        try:
            moved = str(_one(conn, f"SELECT pgcolumnar.tier_partition('{t}_mar', 'tq', '{server_dir}/{t.replace(chr(34), '').replace(' ', '_').replace('.', '_')}.parquet')"))
        except psycopg.Error as e:
            moved = e.sqlstate or "?"
        after = _one(conn, f"SELECT md5(string_agg({cols}, ',' ORDER BY at)) FROM {t}")
        kind = _one(conn, f"SELECT relkind::text FROM pg_class WHERE oid = '{t}_mar'::regclass")
        return f"{moved}|{kind}|{'same' if before and before == after else 'differs'}"

    with conn.cursor() as cur:
        cur.execute('CREATE SCHEMA "Cold Store"')
    mini('"Cold Store".cs')
    expect.text(moved_reads('"Cold Store".cs', "at::text || reading"), "120|f|same",
                "a partition in a schema whose name needs quoting moves and reads back the same")
    with conn.cursor() as cur:
        cur.execute('DO $$ BEGIN CREATE ROLE "Archive Owner"; EXCEPTION WHEN duplicate_object THEN NULL; END $$')
    mini("co")
    with conn.cursor() as cur:
        cur.execute('ALTER TABLE co_mar OWNER TO "Archive Owner"')
    expect.text(f"{moved_reads('co', 'at::text || reading')}|"
                f"{_one(conn, 'SELECT relowner::regrole::text FROM pg_class WHERE oid = ' + repr('co_mar') + '::regclass')}",
                '120|f|same|"Archive Owner"',
                "a partition owned by a role whose name needs quoting moves, and keeps that owner")
    with conn.cursor() as cur:
        cur.execute('DO $$ BEGIN CREATE ROLE "Audit Reader"; EXCEPTION WHEN duplicate_object THEN NULL; END $$')
    mini("cg")
    with conn.cursor() as cur:
        cur.execute('GRANT SELECT ON cg_mar TO "Audit Reader"')
    expect.text(f"{moved_reads('cg', 'at::text || reading')}|"
                f"{_one(conn, 'SELECT has_table_privilege(' + repr('Audit Reader') + ', ' + repr('cg_mar') + ', ' + repr('SELECT') + ')')}",
                "120|f|same|True", "a grant to a role whose name needs quoting is carried to the foreign partition")
    with conn.cursor() as cur:
        cur.execute('REVOKE ALL ON cg_mar FROM "Audit Reader"')
    mini("cc", ', label text COLLATE "C"')
    with conn.cursor() as cur:
        cur.execute("UPDATE cc SET label = 'L' || reading")
    expect.text(moved_reads("cc", "at::text || reading || label"), "120|f|same",
                "a column with its own collation moves and reads back the same")
    mini("cs2", ", doubled int GENERATED ALWAYS AS (reading * 2) STORED")
    expect.text(moved_reads("cs2", "at::text || reading || doubled"), "120|f|same",
                "a stored generated column moves and reads back the same")

    # 5. A retry over the file a failed attempt left.
    _build(conn, "again")
    ab = _digest(conn, "again")
    p = server_dir / "again.parquet"
    with conn.cursor() as cur:
        cur.execute("SET pgcolumnar.tier_fault_after = 'export'")
    err = _errtext(conn, _tier("again", p))
    with conn.cursor() as cur:
        cur.execute("RESET pgcolumnar.tier_fault_after")
    expect.text(f"{'injected fault after export' if 'injected fault after export' in err else err}|"
                f"{'file' if p.exists() else 'nofile'}", "injected fault after export|file",
                "premise: the first attempt failed after export and left its file")
    try:
        retried = str(_one(conn, _tier("again", p)))
    except psycopg.Error as e:
        retried = f"error {e.sqlstate}"
    expect.text(f"{retried}|{_digest(conn, 'again')}|{_relkind(conn, 'again')}",
                f"{mar}|{ab}|f", "a retry over the failed attempt's file moves the partition, exactly once")


def test_tier_partition_moves_a_virtual_generated_column(pgc_own_db, server_dir, expect):
    """PostgreSQL 18's virtual generated columns: the foreign table declares the
    same expression, and the moved rows read back the same (#1317 review)."""
    conn = pgc_own_db
    major = int(_one(conn, "SHOW server_version_num")) // 10000
    if major < 18:
        expect.cannot_run(
            "UNSUPPORTED_MAJOR",
            f"virtual generated columns arrived in PostgreSQL 18; this server is {major}",
            name="a virtual generated column moves and reads back the same")
        return
    with conn.cursor() as cur:
        cur.execute("CREATE SERVER tq FOREIGN DATA WRAPPER pgcolumnar_parquet")
        cur.execute("CREATE TABLE cv (at timestamptz NOT NULL, reading int,"
                    " tripled int GENERATED ALWAYS AS (reading * 3) VIRTUAL) PARTITION BY RANGE (at)")
        cur.execute("CREATE TABLE cv_mar PARTITION OF cv FOR VALUES FROM ('2026-03-01') TO ('2026-04-01')"
                    " USING pgcolumnar")
        cur.execute("INSERT INTO cv (at, reading) SELECT '2026-03-01'::timestamptz + g * interval '9 h', g"
                    " FROM generate_series(0, 70) g")
    digest = "SELECT md5(string_agg(at::text || reading || tripled, ',' ORDER BY at)) FROM cv"
    before = _one(conn, digest)
    moved = _one(conn, f"SELECT pgcolumnar.tier_partition('cv_mar', 'tq', '{server_dir}/cv.parquet')")
    kind = _one(conn, "SELECT relkind::text FROM pg_class WHERE oid = 'cv_mar'::regclass")
    expect.text(f"{moved}|{kind}|{'same' if before and before == _one(conn, digest) else 'differs'}",
                "71|f|same", "a virtual generated column moves and reads back the same")


def test_tier_partition_runs_on_the_callers_own_privileges(pgc_own_db, pgc_cluster, server_dir, expect):
    """tier_partition is not SECURITY DEFINER, so a role that does not own the table is
    refused by its own privileges (#1341). Made SECURITY DEFINER, it would run as the
    extension's owner and let any caller replace a partition it does not own. Both roles
    hold every other privilege the call needs, so ownership is all that separates them."""
    conn = pgc_own_db
    db = _one(conn, "SELECT current_database()")
    with conn.cursor() as cur:
        cur.execute("SET timezone = 'UTC'")
        cur.execute("CREATE SERVER tq FOREIGN DATA WRAPPER pgcolumnar_parquet")
        for r in ("tier_own_py", "tier_peer_py"):
            cur.execute(f"DO $$ BEGIN CREATE ROLE {r} LOGIN NOSUPERUSER; "
                        "EXCEPTION WHEN duplicate_object THEN NULL; END $$")
            cur.execute(f"GRANT pg_write_server_files TO {r}")
            cur.execute(f"GRANT USAGE ON FOREIGN SERVER tq TO {r}")
            cur.execute(f"GRANT USAGE, CREATE ON SCHEMA public TO {r}")
            cur.execute(f"GRANT USAGE ON SCHEMA pgcolumnar TO {r}")
    # The owner's control has a table of its own (pw), so a refusal that let the peer
    # through cannot make the control fail for the peer's reason.
    for t in ("po", "pw"):
        _build(conn, t)
        with conn.cursor() as cur:
            for r in (t, f"{t}_mar", f"{t}_apr"):
                cur.execute(f"ALTER TABLE {r} OWNER TO tier_own_py")
    with conn.cursor() as cur:
        cur.execute("GRANT SELECT ON po, po_mar TO tier_peer_py")
    before = _digest(conn, "po")

    def as_role(role, sql):
        try:
            with psycopg.connect(f"host=127.0.0.1 port={pgc_cluster.port} user={role} dbname={db}",
                                 autocommit=True) as c:
                c.execute("SET timezone = 'UTC'")
                c.execute(sql)
            return "ok"
        except psycopg.Error as e:
            return e.sqlstate or "?"

    owners = _one(conn, "SELECT string_agg(DISTINCT pg_get_userbyid(relowner), ',') FROM pg_class"
                        " WHERE relname IN ('po', 'po_mar', 'po_apr', 'pw', 'pw_mar', 'pw_apr')")
    supers = _one(conn, "SELECT count(*) FROM pg_roles WHERE rolname IN ('tier_own_py', 'tier_peer_py') AND rolsuper")
    expect.text(f"{owners}|{supers}", "tier_own_py|0",
                "premise: tier_own owns the parent and its partitions, and neither role is a superuser")
    expect.text(as_role("tier_peer_py", "SELECT count(*) FROM po_mar"), "ok",
                "premise: tier_peer can open a session and read the partition")
    peer_path = server_dir / "po_peer.parquet"
    expect.text(as_role("tier_peer_py", f"SELECT pgcolumnar.tier_partition('po_mar', 'tq', '{peer_path}')"),
                "42501", "a role that does not own the table is refused by its own privileges (42501)")
    expect.text(f"{_digest(conn, 'po')}|{_mar_shape(conn, 'po')}", f"{before}|r:pgcolumnar:{BOUND}",
                "and the refused call leaves the partition local and attached, every row once")
    expect.num(_one(conn, "SELECT count(*) FROM pg_foreign_table t JOIN pg_class c ON c.oid = t.ftrelid"
                          " WHERE c.relname LIKE 'po\\_%'"), 0, "with no foreign table behind it")
    # The LOCK ... IN SHARE MODE ahead of the export is the first statement to need
    # more than SELECT, so the refusal comes before any file is written.
    expect.text("written" if peer_path.exists() else "absent", "absent", "and the refused call wrote no export file")
    expect.text(as_role("tier_own_py", f"SELECT pgcolumnar.tier_partition('pw_mar', 'tq', '{server_dir / 'pw_own.parquet'}')")
                + f"|{_mar_shape(conn, 'pw')}", f"ok|f:-:{BOUND}",
                "a non-superuser owner tiers its own partition")
