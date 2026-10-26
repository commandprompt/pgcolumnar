"""DROP TABLE takes the whole table's metadata with it, projections included, and a
columnar materialized view takes its options row (#1265, #1341, #432). The pytest twin
of `test/drop_cleanup.sh`, every check by the shell's name.

A projection keeps its own storage, so the drop hook that deleted only the relation's
own storage id left the projection's row groups, chunks, zone maps, blooms and
projection rows behind: ten create-and-drop cycles took pgcolumnar.projection from 36
rows to 56. Each arm counts EVERY catalog that hangs off a storage id, re-captured just
before its own work, so it measures the increment its own step adds; the last is
absolute, for a standing leak no single step increases. A private database, so the
absolute arm counts this module's tables and nothing another module left.

The drop hook returned before it examined anything that was not an ordinary table, so
an options row describing a columnar matview outlived the relation. The row is planted
directly, not through set_options: the subject is the drop hook, and set_options has a
relkind guard of its own, which while it refused a matview would leave no row and let
"0 rows afterwards" pass on a hook that does nothing. Widening the hook means a
refresh that dropped the relation would discard the user's options, so REFRESH is
asserted too, and an unrelated table's row is the control against a hook that deletes
every row.
"""


import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_drop_cleanup"

SNAPSHOT = ("SELECT (SELECT count(*) FROM pgcolumnar.storage) || '/' || "
            "(SELECT count(*) FROM pgcolumnar.projection) || '/' || "
            "(SELECT count(*) FROM pgcolumnar.row_group) || '/' || "
            "(SELECT count(*) FROM pgcolumnar.column_chunk) || '/' || "
            "(SELECT count(*) FROM pgcolumnar.zone_map) || '/' || "
            "(SELECT count(*) FROM pgcolumnar.bloom) || '/' || "
            "(SELECT count(*) FROM pgcolumnar.delete_vector)")


def _value(dsn, sql):
    """-> the first column of the first row as text, or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            row = c.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0])
    return query_error(f"no value from {sql}") if row is None or row[0] is None else str(row[0])


def _exec(dsn, *sqls):
    """Each statement on its own, as the shell's psql_run calls are; a failure is
    printed and the scenario goes on, so the reading after it is what fails."""
    with psycopg.connect(dsn, autocommit=True) as c:
        for sql in sqls:
            try:
                c.execute(sql)
            except psycopg.Error as exc:
                print(f"-- {sql[:60]}: {str(exc).splitlines()[0]}")


@pytest.fixture(scope="module")
def dcl(pgc_cluster):
    """Every reading the storage-cleanup arms assert, in the shell's order."""
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _exec(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r = {}
        snap = lambda: _value(dsn, SNAPSHOT)

        base = snap()
        _exec(dsn, "CREATE TABLE dcl_plain (id int, v text) USING pgcolumnar",
              "INSERT INTO dcl_plain SELECT g, 'x' || g FROM generate_series(1, 5000) g",
              "DELETE FROM dcl_plain WHERE id % 5 = 0", "DROP TABLE dcl_plain")
        r["plain"] = (snap(), base)

        base = snap()
        _exec(dsn, "CREATE TABLE dcl_proj (id int, a int, b text) USING pgcolumnar",
              "SELECT pgcolumnar.add_projection('dcl_proj','dcl_p',ARRAY['a','b'],ARRAY['a'])",
              "INSERT INTO dcl_proj SELECT g, g % 50, 'b' || g FROM generate_series(1, 5000) g")
        grew = snap()
        r["grew"] = "no" if grew == base else "yes"
        _exec(dsn, "DROP TABLE dcl_proj")
        r["proj"] = (snap(), base)

        base = snap()
        _exec(dsn, "CREATE TABLE dcl_multi (id int, a int, b text, c int) USING pgcolumnar",
              "SELECT pgcolumnar.add_projection('dcl_multi','m1',ARRAY['a'],ARRAY['a'])",
              "SELECT pgcolumnar.add_projection('dcl_multi','m2',ARRAY['b','c'],ARRAY['c'])",
              "INSERT INTO dcl_multi SELECT g, g%10, 'z'||g, g*3 FROM generate_series(1, 4000) g",
              "SELECT pgcolumnar.drop_projection('dcl_multi','m1')", "DROP TABLE dcl_multi")
        r["multi"] = (snap(), base)

        base = snap()
        for i in range(1, 11):
            _exec(dsn, f"CREATE TABLE dcl_c{i} (id int, a int) USING pgcolumnar",
                  f"SELECT pgcolumnar.add_projection('dcl_c{i}','p{i}',ARRAY['a'],ARRAY['a'])",
                  f"INSERT INTO dcl_c{i} SELECT g, g FROM generate_series(1, 500) g",
                  f"DROP TABLE dcl_c{i}")
        r["cycles"] = (snap(), base)

        r["orphans"] = _value(dsn, "SELECT count(*) FROM pgcolumnar.storage s WHERE NOT EXISTS "
                                   "(SELECT 1 FROM pg_class c WHERE c.oid = s.relation_oid)")

        _exec(dsn, "DROP TABLE IF EXISTS dcl_live",
              "CREATE TABLE dcl_live (id int, a int) USING pgcolumnar",
              "SELECT pgcolumnar.add_projection('dcl_live','lp',ARRAY['a'],ARRAY['a'])",
              "INSERT INTO dcl_live SELECT g, g % 20 FROM generate_series(1, 3000) g")
        r["live"] = (_value(dsn, "SELECT count(*) || '/' || sum(a) FROM dcl_live"),
                     _value(dsn, "SELECT count(*) || '/' || sum(g % 20) FROM generate_series(1,3000) g"))
        r["live proj"] = _value(dsn, "SELECT count(*) FROM pgcolumnar.read_projection('dcl_live','lp')")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_a_plain_table_leaves_nothing_behind(dcl, expect):
    got, want = dcl["plain"]
    expect.text(got, want, "a plain table leaves nothing behind")


def test_the_projection_did_add_metadata(dcl, expect):
    expect.text(dcl["grew"], "yes", "the projection did add metadata")


def test_dropping_it_takes_the_projections_metadata_too(dcl, expect):
    got, want = dcl["proj"]
    expect.text(got, want, "dropping it takes the projection's metadata too")


def test_two_projections_one_dropped_by_hand_leave_nothing(dcl, expect):
    got, want = dcl["multi"]
    expect.text(got, want, "two projections, one dropped by hand, leave nothing")


def test_ten_create_and_drop_cycles_leave_nothing(dcl, expect):
    got, want = dcl["cycles"]
    expect.text(got, want, "ten create-and-drop cycles leave nothing")


def test_no_storage_row_refers_to_a_missing_relation(dcl, expect):
    expect.text(dcl["orphans"], "0", "no storage row refers to a missing relation")


def test_a_live_table_with_a_projection_still_reads_correctly(dcl, expect):
    got, want = dcl["live"]
    expect.text(got, want, "a live table with a projection still reads correctly")


def test_and_its_projection_is_still_readable(dcl, expect):
    expect.text(dcl["live proj"], "3000", "and its projection is still readable")


def _one(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


def _run(conn, *sqls):
    with conn.cursor() as cur:
        for sql in sqls:
            cur.execute(sql)


def test_a_columnar_matview_takes_its_options_row_with_it(pgc_conn, expect):
    conn = pgc_conn
    _run(conn, "CREATE TABLE dcl_mv_src (a int)", "INSERT INTO dcl_mv_src VALUES (1), (2)",
         "CREATE MATERIALIZED VIEW dcl_mv USING pgcolumnar AS SELECT a FROM dcl_mv_src")
    expect.text(_one(conn, "SELECT c.relkind::text || am.amname::text FROM pg_class c JOIN pg_am am "
                           "ON am.oid = c.relam WHERE c.oid = 'dcl_mv'::regclass"), "mpgcolumnar",
                "premise: the matview is relkind m on the columnar access method")
    _run(conn, "INSERT INTO pgcolumnar.options (regclass, stripe_row_limit) VALUES ('dcl_mv'::regclass, 123)")
    expect.num(_one(conn, "SELECT count(*) FROM pgcolumnar.options WHERE regclass = 'dcl_mv'::regclass"), 1,
               "premise: the matview carries an options row for the hook to clear")
    oid = _one(conn, "SELECT 'dcl_mv'::regclass::oid")
    _run(conn, "DROP MATERIALIZED VIEW dcl_mv")
    expect.num(_one(conn, f"SELECT count(*) FROM pgcolumnar.options WHERE regclass = {oid}"), 0,
               "dropping a columnar matview takes its options row with it")


def test_refresh_keeps_a_matviews_options_row(pgc_conn, expect):
    conn = pgc_conn
    _run(conn, "CREATE TABLE dcl_mv_ref_src (a int)", "INSERT INTO dcl_mv_ref_src VALUES (1), (2)",
         "CREATE MATERIALIZED VIEW dcl_mv_ref USING pgcolumnar AS SELECT a FROM dcl_mv_ref_src",
         "SELECT pgcolumnar.set_options('dcl_mv_ref', stripe_row_limit => 100000)")
    expect.text(_one(conn, "SELECT stripe_row_limit::text FROM pgcolumnar.options "
                           "WHERE regclass = 'dcl_mv_ref'::regclass"), "100000",
                "premise: the matview holds the options row set through set_options")
    _run(conn, "INSERT INTO dcl_mv_ref_src VALUES (3)", "REFRESH MATERIALIZED VIEW dcl_mv_ref")
    expect.num(_one(conn, "SELECT count(*) FROM dcl_mv_ref"), 3, "premise: the refresh actually rewrote the matview")
    expect.text(_one(conn, "SELECT stripe_row_limit::text FROM pgcolumnar.options "
                           "WHERE regclass = 'dcl_mv_ref'::regclass"), "100000",
                "REFRESH keeps the matview's options row")


def test_an_unrelated_options_row_survives(pgc_conn, expect):
    """Without it a hook that deletes every options row passes the arms above."""
    conn = pgc_conn
    _run(conn, "CREATE TABLE dcl_opt_keep (a int) USING pgcolumnar",
         "INSERT INTO pgcolumnar.options (regclass, stripe_row_limit) VALUES ('dcl_opt_keep'::regclass, 123)",
         "CREATE TABLE dcl_other_src (a int)",
         "CREATE MATERIALIZED VIEW dcl_other USING pgcolumnar AS SELECT a FROM dcl_other_src",
         "DROP MATERIALIZED VIEW dcl_other")
    expect.num(_one(conn, "SELECT count(*) FROM pgcolumnar.options WHERE regclass = 'dcl_opt_keep'::regclass"), 1,
               "control: an unrelated options row survives that drop")
    oid = _one(conn, "SELECT 'dcl_opt_keep'::regclass::oid")
    _run(conn, "DROP TABLE dcl_opt_keep")
    expect.num(_one(conn, f"SELECT count(*) FROM pgcolumnar.options WHERE regclass = {oid}"), 0,
               "control: an ordinary columnar table still takes its own options row")
