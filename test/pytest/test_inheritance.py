"""A partitioned parent carrying the columnar access method has no storage, and is
refused rather than read; a columnar materialized view is still columnar (#1259,
#1341). Part of the pytest twin of `test/inheritance.sh`: these arms only, by the
shell's names, and the pair is declared INCOMPLETE.

From PostgreSQL 17 a partitioned table may carry relam = pgcolumnar as the default for
its future partitions. It has no storage of its own, relfilenode 0, and
PgColumnarIsColumnarRelation used to answer "yes" from relam alone. Every caller that
then read storage handed smgropen a zero relfilenumber: an abort on an assert build,
and on a release build a failure to open ".../0/0", 58P01. So a refusal here is
42809 (wrong_object_type), not any error, and liveness is read from the server log,
since a crashed cluster restarts and a later connection succeeds.
"""
import psycopg


def _crashes(pgc_cluster):
    try:
        return (pgc_cluster.datadir / "server.log").read_text(errors="replace").count("terminated by signal")
    except OSError:
        return 0


def _sqlstate(conn, sql):
    """-> 'accepted', or the SQLSTATE the statement raised."""
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        return "accepted"
    except psycopg.Error as exc:
        return exc.sqlstate or f"no SQLSTATE ({str(exc).splitlines()[0]})"


def _one(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


def test_a_partitioned_parent_is_refused_and_its_leaf_is_not(pgc_conn, pgc_cluster, expect):
    conn = pgc_conn
    major = int(_one(conn, "SHOW server_version_num")) // 10000
    if major < 17:
        # PostgreSQL 15 and 16 refuse PARTITION BY ... USING outright, so the fixture
        # cannot exist and every "is refused" arm would pass on a missing relation.
        expect.cannot_run("UNSUPPORTED_MAJOR",
                          f"a partitioned table cannot carry an access method before PostgreSQL 17; this server is {major}",
                          name="get_storage_id on the parent is refused rather than reading storage")
        return
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE inh_pp (a int, b text) PARTITION BY RANGE (a) USING pgcolumnar")
        cur.execute("CREATE TABLE inh_pp1 PARTITION OF inh_pp FOR VALUES FROM (0) TO (100)")
        cur.execute("INSERT INTO inh_pp SELECT g, 'x' || g FROM generate_series(0, 99) g")
    expect.text(_one(conn, "SELECT am.amname::text FROM pg_class c JOIN pg_am am ON am.oid = c.relam "
                           "WHERE c.oid = 'inh_pp'::regclass"), "pgcolumnar",
                "premise: the parent carries the columnar access method")
    expect.text(_one(conn, "SELECT relkind::text FROM pg_class WHERE oid = 'inh_pp'::regclass"), "p",
                "premise: and it is partitioned")
    expect.num(_one(conn, "SELECT relfilenode::int FROM pg_class WHERE oid = 'inh_pp'::regclass"), 0,
               "premise: so it has no storage of its own")
    expect.text(_one(conn, "SELECT relkind::text FROM pg_class WHERE oid = 'inh_pp1'::regclass"), "r",
                "premise: while its leaf is an ordinary relation")

    crashes = _crashes(pgc_cluster)

    def up():
        alive = _sqlstate(conn, "SELECT 1") == "accepted"
        crashed = _crashes(pgc_cluster) - crashes
        return "up" if alive and crashed == 0 else f"DOWN ({crashed} backend crashes)"

    expect.text(_sqlstate(conn, "SELECT pgcolumnar.get_storage_id('inh_pp'::regclass)"), "42809",
                "get_storage_id on the parent is refused rather than reading storage")
    expect.text(up(), "up", "and the cluster is still up after it")
    expect.text(_sqlstate(conn, "SELECT pgcolumnar.add_projection('inh_pp'::regclass, 'p1', ARRAY['a'])"),
                "42809", "add_projection on the parent is refused rather than aborting the backend")
    expect.text(up(), "up", "and the cluster is still up after that too")

    # Without these a function that refuses everything passes the arms above.
    expect.text(_sqlstate(conn, "SELECT pgcolumnar.get_storage_id('inh_pp1'::regclass)"), "accepted",
                "control: get_storage_id on the LEAF still returns an id")
    expect.text(_sqlstate(conn, "SELECT pgcolumnar.add_projection('inh_pp1'::regclass, 'p1', ARRAY['a'])"),
                "accepted", "control: add_projection on the LEAF still works")


def test_a_columnar_materialized_view_is_still_columnar(pgc_conn, expect):
    """The arm against the obvious wrong fix: relkind == RELKIND_RELATION would exclude
    a matview too, and a matview can be columnar. RELKIND_HAS_STORAGE admits it."""
    conn = pgc_conn
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE inh_src (a int)")
        cur.execute("INSERT INTO inh_src SELECT generate_series(1, 10)")
        cur.execute("CREATE MATERIALIZED VIEW inh_mv USING pgcolumnar AS SELECT * FROM inh_src")
    expect.text(_one(conn, "SELECT relkind::text FROM pg_class WHERE oid = 'inh_mv'::regclass"), "m",
                "premise: a materialized view is relkind m")
    expect.text(_one(conn, "SELECT am.amname::text FROM pg_class c JOIN pg_am am ON am.oid = c.relam "
                           "WHERE c.oid = 'inh_mv'::regclass"), "pgcolumnar",
                "premise: and it carries the columnar access method")
    expect.text(_sqlstate(conn, "SELECT pgcolumnar.get_storage_id('inh_mv'::regclass)"), "accepted",
                "a columnar materialized view is still recognised as columnar")
    expect.num(_one(conn, "SELECT count(*) FROM inh_mv"), 10, "and it still returns its rows")
