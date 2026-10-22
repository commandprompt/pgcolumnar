"""A type rewrite must keep pgcolumnar.storage.relation_oid on the live table.

A rewriting ALTER COLUMN TYPE writes the storage row under the transient
relation make_new_heap builds. That relation is dropped after the swap, and
the user's OID is what later lookups use. The rows stay readable.

Independent of test/rewrite_storage_oid.sh: same public seam (the storage
row for get_storage_id compared with the live regclass), own table, own
row count, own type change.
"""

import pytest

from pgc_vacuity import query_error

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


# Every other statement that rewrites, or would, keeps the storage row (#1339).
#
# The tests above rewrite with one ALTER form (a type change), a matview,
# CREATE TABLE AS and REFRESH. The repair gate keys on the parse node, not on
# the subcommand, so a gate narrowed to the one form measured above would pass
# all of them. Each case runs one more form on its own fresh table, writes one
# more row, and reads one value made of these parts, in this order:
#
#   survived  the server answers and its log gained no "terminated by signal"
#             line since the case started. A reconnect alone would pass a
#             backend that died and was restarted.
#   outcome   what each statement did: "ok", or "SQLSTATE message".
#   rewrote   (forms that must rewrite) the table's storage id changed.
#   points    the storage row names the live table.
#   count     the table holds every row.
#   digest    md5 of every id and note equals the same md5 over the generator
#             (generate_series plus the extra row), not over the table read
#             back, so a rewrite that keeps the rows but loses a value is red.
#   extra     (forms that must rewrite, where the form changes the table) what
#             it changed is there: the new column, filled for every row, the
#             new type, the access method after the round trip.
#
# Four forms are REFUSED today and rewrite nothing: SET UNLOGGED (and so SET
# UNLOGGED then SET LOGGED), VACUUM FULL and CLUSTER. Their cases accept exactly
# two outcomes per statement: the exact refusal (0A000 and its message), or
# success. Both read "refused or ok"; anything else, a crash or another error,
# reads as itself. Either way the rest of the value must hold, so the day a form
# is allowed, the case says the rewrite it then does keeps every row and value.
#
# EVERY READING IS TAKEN ON A FRESH CONNECTION, in a module fixture, and a failed
# one is a query_error(): a backend that dies takes every session with it, and a
# test reading through pgc_conn would end in OperationalError and record nothing.
# "survived" comes first, so a value never starts with a failed query's sentinel.
#
# Independent of the shell twin: own database, own table names, own row count.
FORM_DB = "rewrite_forms_py"
FORM_ROWS = 650
DIGEST = "md5(string_agg(id || ':' || coalesce(note, '∅'), ',' ORDER BY id))"
UNLOGGED_REFUSAL = "0A000 unlogged columnar tables are not supported"
# (name, table, the count after the extra row, statements, extra reading or None).
# A statement is (sql, the refusal it may give: None, "unlogged" or "cluster").
# A form with any refusal is one refused today. The counts are literals
# (FORM_ROWS + 1 = 651, or 1 after TRUNCATE) so compare_to_bash.py can read the
# names out of this table.
FORMS = [
    ("a rewrite by ADD COLUMN with a volatile default keeps the storage row on the live table and every row",
     "pf_addcol", 651, [("ALTER TABLE pf_addcol ADD COLUMN r float8 DEFAULT random()", None)],
     ("SELECT format_type(atttypid, atttypmod) || ' ' || (SELECT count(r) FROM pf_addcol)"
      " FROM pg_attribute WHERE attrelid = 'pf_addcol'::regclass AND attname = 'r'",
      "double precision 651")),
    ("a type change to varchar(20) keeps the storage row on the live table and every row",
     "pf_varchar", 651, [("ALTER TABLE pf_varchar ALTER COLUMN note TYPE varchar(20)", None)],
     ("SELECT format_type(atttypid, atttypmod) FROM pg_attribute"
      " WHERE attrelid = 'pf_varchar'::regclass AND attname = 'note'",
      "character varying(20)")),
    ("SET ACCESS METHOD heap and back keeps the storage row on the live table and every row",
     "pf_am", 651, [("ALTER TABLE pf_am SET ACCESS METHOD heap", None),
                    ("ALTER TABLE pf_am SET ACCESS METHOD pgcolumnar", None)],
     ("SELECT a.amname FROM pg_class c JOIN pg_am a ON a.oid = c.relam"
      " WHERE c.oid = 'pf_am'::regclass", "pgcolumnar")),
    ("SET UNLOGGED keeps the storage row on the live table and every row",
     "pf_unlog", 651, [("ALTER TABLE pf_unlog SET UNLOGGED", "unlogged")], None),
    ("SET UNLOGGED then SET LOGGED keeps the storage row on the live table and every row",
     "pf_relog", 651, [("ALTER TABLE pf_relog SET UNLOGGED", "unlogged"),
                       ("ALTER TABLE pf_relog SET LOGGED", "unlogged")], None),
    ("VACUUM FULL keeps the storage row on the live table and every row",
     "pf_vacfull", 651, [("VACUUM FULL pf_vacfull", "cluster")], None),
    ("CLUSTER USING an index keeps the storage row on the live table and every row",
     "pf_cluster", 651, [("CLUSTER pf_cluster USING pf_cluster_i", "cluster")], None),
    # TRUNCATE gives the table a new storage id and no storage row until the
    # next write, so this case reads the row that write creates: one row, and
    # the digest of that row alone (a generator of 0 rows plus the extra row).
    ("TRUNCATE then a write gives the new storage row the live table and only the new row",
     "pf_trunc", 1, [("TRUNCATE pf_trunc", None)], None),
]


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _value(dsn, sql):
    """-> the first column of the first row as text, or the failure as a query_error()."""
    import psycopg

    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    if row is None or row[0] is None:
        return query_error(f"no value from {sql}")
    return f"{row[0]}"


def _outcome(dsn, sql, settings=()):
    """-> "ok", "SQLSTATE message" for an error, or a query_error() with no SQLSTATE
    (a backend that died). Its own session."""
    import psycopg

    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for s in settings:
                conn.execute(s)
            conn.execute(sql)
    except psycopg.Error as exc:
        if not exc.sqlstate:
            return query_error(_message(exc))
        return f"{exc.sqlstate} {exc.diag.message_primary}"
    return "ok"


def _crashes(cluster):
    try:
        return (cluster.datadir / "server.log").read_text(errors="replace").count("terminated by signal")
    except OSError as exc:
        return query_error(f"crash count unreadable: {exc}")


def _up(dsn):
    """Wait up to 60 s for the server to accept a connection, as the shell's up()."""
    import time

    import psycopg

    for _ in range(60):
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute("SELECT 1")
            return
        except psycopg.Error:
            time.sleep(1)


def _survived(dsn, cluster, before):
    up, now = _value(dsn, "SELECT 1"), _crashes(cluster)
    if not isinstance(before, int) or not isinstance(now, int):
        return query_error(f"crash count unreadable: {before} / {now}")
    return "1" if up == "1" and now == before else f"no ([{up}], {now - before} backend crashes)"


def _rest(dsn, table, genrows):
    """-> points|count|digest after the follow-up write; the digest compared with
    generate_series(1, genrows) plus that row."""
    write = _outcome(dsn, f"INSERT INTO {table} VALUES (0, 'after')")
    points = _value(dsn, f"SELECT (relation_oid = '{table}'::regclass)::int FROM pgcolumnar.storage"
                         f" WHERE storage_id = pgcolumnar.get_storage_id('{table}')")
    count = _value(dsn, f"SELECT count(*) FROM {table}")
    digest = _value(dsn, f"SELECT ((SELECT {DIGEST} FROM {table})"
                         f" = (SELECT {DIGEST} FROM (SELECT g AS id, 'p' || g AS note"
                         f" FROM generate_series(1, {genrows}) g UNION ALL SELECT 0, 'after') s))::int")
    if write != "ok":
        points = f"{points} (the extra row: {write})"
    return f"{points}|{count}|{digest}"


@pytest.fixture(scope="module")
def form_readings(pgc_cluster):
    """Every form's value, keyed by table, read on fresh connections."""
    import psycopg

    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {FORM_DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {FORM_DB}")
    dsn = pgc_cluster.dsn(dbname=FORM_DB)
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION pgcolumnar")
            major = c.execute("SELECT current_setting('server_version_num')::int / 10000").fetchone()[0]
        refusals = {
            "unlogged": UNLOGGED_REFUSAL,
            "cluster": "0A000 columnar: REPACK, CLUSTER and VACUUM FULL are not supported yet"
            if major >= 19 else "0A000 columnar: CLUSTER and VACUUM FULL are not supported yet",
        }
        r = {}
        for _name, table, want, statements, extra in FORMS:
            with psycopg.connect(dsn, autocommit=True) as c:
                c.execute(f"CREATE TABLE {table} (id int, note text) USING pgcolumnar")
                c.execute(f"INSERT INTO {table} SELECT g, 'p' || g FROM generate_series(1, {FORM_ROWS}) g")
                if table == "pf_cluster":
                    c.execute("CREATE INDEX pf_cluster_i ON pf_cluster (id)")
            refused = any(kind for _sql, kind in statements)
            sid = _value(dsn, f"SELECT pgcolumnar.get_storage_id('{table}')")
            before = _crashes(pgc_cluster)
            outs = []
            for sql, kind in statements:
                got = _outcome(dsn, sql)
                print(f"-- {table}: {sql} -> {got}")
                if kind and got in (refusals[kind], "ok"):
                    got = "refused or ok"
                outs.append(got)
            _up(dsn)
            survived = _survived(dsn, pgc_cluster, before)
            if refused:
                r[table] = f"{survived}|{';'.join(outs)}|{_rest(dsn, table, FORM_ROWS)}"
                continue
            rewrote = _value(dsn, f"SELECT (pgcolumnar.get_storage_id('{table}') <> '{sid}')::int")
            value = f"{survived}|{';'.join(outs)}|{rewrote}|{_rest(dsn, table, want - 1)}"
            if extra:
                value += f"|{_value(dsn, extra[0])}"
            r[table] = value

        # SELECT INTO is CREATE TABLE AS under another spelling (OBJECT_TABLE).
        # The access method comes from default_table_access_method, set on the
        # statement's own session.
        before = _crashes(pgc_cluster)
        got = _outcome(dsn, f"SELECT g AS id, 'p' || g AS note INTO pf_into FROM generate_series(1, {FORM_ROWS}) g",
                       ["SET default_table_access_method = pgcolumnar"])
        print(f"-- pf_into: SELECT INTO -> {got}")
        _up(dsn)
        survived = _survived(dsn, pgc_cluster, before)
        am = _value(dsn, "SELECT a.amname FROM pg_class c JOIN pg_am a ON a.oid = c.relam"
                         " WHERE c.relname = 'pf_into'")
        r["pf_into"] = f"{survived}|{got}|{am}|{_rest(dsn, 'pf_into', FORM_ROWS)}"
        yield r
    finally:
        _up(pgc_cluster.dsn())
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {FORM_DB} WITH (FORCE)")


def _want(want, statements, extra):
    if any(kind for _sql, kind in statements):
        return f"1|{';'.join('refused or ok' for _ in statements)}|1|{want}|1"
    return f"1|{';'.join('ok' for _ in statements)}|1|1|{want}|1" + (f"|{extra[1]}" if extra else "")


@pytest.mark.parametrize("name,table,want,statements,extra", FORMS, ids=[f[1] for f in FORMS])
def test_other_rewriting_forms_keep_the_storage_row(form_readings, expect, name, table, want,
                                                    statements, extra):
    expect.text(form_readings[table], _want(want, statements, extra), name)


def test_select_into_a_columnar_table_keeps_the_storage_row(form_readings, expect):
    expect.text(form_readings["pf_into"], f"1|ok|pgcolumnar|1|{FORM_ROWS + 1}|1",
                "SELECT INTO a columnar table leaves its storage row on the table, with every row")
