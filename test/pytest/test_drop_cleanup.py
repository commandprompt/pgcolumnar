"""Dropping a columnar materialized view takes its options row with it, and REFRESH
keeps it (#1265, #1341). Part of the pytest twin of `test/drop_cleanup.sh`: these
arms only, by the shell's names, and the pair is declared INCOMPLETE.

The drop hook returned before it examined anything that was not an ordinary table, so
an options row describing a columnar matview outlived the relation. The row is planted
directly, not through set_options: the subject is the drop hook, and set_options has a
relkind guard of its own, which while it refused a matview would leave no row and let
"0 rows afterwards" pass on a hook that does nothing. Widening the hook means a
refresh that dropped the relation would discard the user's options, so REFRESH is
asserted too, and an unrelated table's row is the control against a hook that deletes
every row.
"""


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
