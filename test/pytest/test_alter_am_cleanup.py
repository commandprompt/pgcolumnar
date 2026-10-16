"""DROP TABLE after SET ACCESS METHOD heap still takes the relid-keyed catalogs with it,
and the drop hook is harmless where the extension is absent (#304, #1341 item 70). The
pytest twin of `test/alter_am_cleanup.sh`, every check by the shell's name.

pgcolumnar.options and projection_declaration are keyed by relid, so a table converted
away from columnar still has rows there, and the drop hook must clear them although the
table is heap by then. The hook is armed in every database, because the library is
preloaded: where the extension was never installed, or has been dropped, it must not
open a catalog that is not there, since an ERROR in an object-access hook aborts the
statement, and every table rewrite drops a transient relation through it. It gates on
pgcolumnar.options resolving, not the schema (DROP EXTENSION leaves the schema), and
skips only the extension's own members, not everything in its schema.

Each cleanup arm compares the catalogs with their state just before its own table was
made, so one leak cannot redden the arms after it. Each statement against the other
databases is its own arm, and the REFRESH arm has a matview of its own made WITH NO
DATA, so neither inherits another's failure. Four databases, each this file's own.
"""
import psycopg
import pytest

SNAPSHOT = ("SELECT (SELECT count(*) FROM pgcolumnar.storage) || '/' || "
            "(SELECT count(*) FROM pgcolumnar.projection) || '/' || "
            "(SELECT count(*) FROM pgcolumnar.row_group) || '/' || "
            "(SELECT count(*) FROM pgcolumnar.options) || '/' || "
            "(SELECT count(*) FROM pgcolumnar.projection_declaration)")
EXT_COUNT = "SELECT count(*) FROM pg_extension WHERE extname = 'pgcolumnar'"


def _admin(cluster, sql):
    with psycopg.connect(cluster.dsn(), autocommit=True) as c:
        c.execute(sql)


def _q(cluster, db, sql):
    """-> the first column as text, or '' when the query cannot run (the shell's 2>/dev/null)."""
    try:
        with psycopg.connect(cluster.dsn(dbname=db), autocommit=True) as c:
            return str(c.execute(sql).fetchone()[0])
    except psycopg.Error:
        return ""


def _dbrun(cluster, db, *sqls):
    """-> 'rc=0', or 'rc=1 ' and the first error, so a red names the statement and how."""
    try:
        with psycopg.connect(cluster.dsn(dbname=db), autocommit=True) as c:
            for sql in sqls:
                c.execute(sql)
        return "rc=0"
    except psycopg.Error as exc:
        return f"rc=1 ERROR:  {exc.diag.message_primary or exc}"[:132]


@pytest.fixture
def dbs(pgc_cluster):
    """-> a factory for fresh databases this test owns, dropped afterwards."""
    made = []

    def make(name, extension):
        _admin(pgc_cluster, f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        _admin(pgc_cluster, f"CREATE DATABASE {name}")
        made.append(name)
        if extension:
            _dbrun(pgc_cluster, name, "CREATE EXTENSION pgcolumnar")
        return name

    yield make
    for name in made:
        _admin(pgc_cluster, f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def test_drop_after_converting_to_heap_cleans_up(pgc_cluster, dbs, expect):
    db = dbs("aac_main", True)
    snap = lambda: _q(pgc_cluster, db, SNAPSHOT)  # noqa: E731
    run = lambda *s: _dbrun(pgc_cluster, db, *s)  # noqa: E731
    base = snap()
    run("CREATE TABLE aac_t (id int, a int, b text) USING pgcolumnar",
        "SELECT pgcolumnar.set_options('aac_t', stripe_row_limit => 5000, sort_by => ARRAY['id'])",
        "SELECT pgcolumnar.add_projection('aac_t','p1',ARRAY['a','b'],ARRAY['a'])",
        "INSERT INTO aac_t SELECT g, g, 'x' FROM generate_series(1,200) g")
    expect.text("yes" if snap() != base else "no", "yes", "premise: options and a projection declaration were recorded")
    run("ALTER TABLE aac_t SET ACCESS METHOD heap")
    expect.text(_q(pgc_cluster, db, "SELECT am.amname FROM pg_class c JOIN pg_am am ON am.oid = c.relam "
                                    "WHERE c.relname = 'aac_t'"), "heap", "the table is heap after the conversion")
    # a round trip back to columnar should still see the declared options
    expect.text(_q(pgc_cluster, db, "SELECT stripe_row_limit FROM pgcolumnar.options WHERE regclass = 'aac_t'::regclass"),
                "5000", "converting to heap does not drop the declared options")
    run("DROP TABLE aac_t")
    expect.text(snap(), base, "DROP after SET ACCESS METHOD heap leaves no options or declarations")

    pre_rt = snap()
    run("CREATE TABLE aac_rt (id int) USING pgcolumnar",
        "SELECT pgcolumnar.set_options('aac_rt', stripe_row_limit => 8000)",
        "ALTER TABLE aac_rt SET ACCESS METHOD heap",
        "ALTER TABLE aac_rt SET ACCESS METHOD pgcolumnar")
    expect.text(_q(pgc_cluster, db, "SELECT stripe_row_limit FROM pgcolumnar.options WHERE regclass = 'aac_rt'::regclass"),
                "8000", "options survive a heap round-trip")
    run("DROP TABLE aac_rt")
    expect.text(snap(), pre_rt, "a round-tripped table still cleans up on DROP")

    pre_h = snap()
    run("CREATE TABLE aac_h (id int)", "DROP TABLE aac_h")
    expect.text(snap(), pre_h, "dropping a heap table that was never columnar leaves the catalogs alone")

    run("CREATE TABLE aac_h_ctl (id int)")
    expect.text(run("VACUUM FULL aac_h_ctl"), "rc=0", "control: VACUUM FULL still succeeds where the extension IS installed")

    # a table in the extension's schema is ours to clean, not ours to skip
    pre_in = snap()
    run("CREATE TABLE pgcolumnar.aac_inschema (id int) USING pgcolumnar",
        "SELECT pgcolumnar.set_options('pgcolumnar.aac_inschema', stripe_row_limit => 7000)")
    expect.text(_q(pgc_cluster, db, "SELECT count(*) FROM pgcolumnar.options "
                                    "WHERE regclass = 'pgcolumnar.aac_inschema'::regclass"), "1",
                "premise: a table in the extension's schema recorded its options")
    run("DROP TABLE pgcolumnar.aac_inschema")
    expect.text(snap(), pre_in, "dropping a table that lives in the extension's schema still cleans up")


def test_the_hook_is_harmless_where_the_extension_never_was(pgc_cluster, dbs, expect):
    own = dbs("aac_own", True)
    nocx = dbs("aac_nocx", False)
    run = lambda *s: _dbrun(pgc_cluster, nocx, *s)  # noqa: E731
    expect.text(_q(pgc_cluster, nocx, EXT_COUNT), "0", "premise: the extension is absent in the second database")
    expect.text(_q(pgc_cluster, own, EXT_COUNT), "1", "premise: and installed in this suite's own database")
    expect.text(run("CREATE TABLE nx (i int primary key, t text)"), "rc=0",
                "a plain table is created in a database without the extension")
    expect.text(run("DROP TABLE nx"), "rc=0", "and dropped there, without the extension")
    expect.text(run("CREATE TEMP TABLE nxt (i int)", "DROP TABLE nxt"), "rc=0",
                "an explicit DROP of a temp table succeeds there")
    expect.text(run("BEGIN", "CREATE TEMP TABLE nxo (i int) ON COMMIT DROP", "COMMIT"), "rc=0",
                "and ON COMMIT DROP commits there")
    # the rewrite paths, each dropping a transient relation through the hook
    expect.text(run("CREATE TABLE rw (i int primary key, t text)",
                    "INSERT INTO rw SELECT g, g::text FROM generate_series(1,100) g"), "rc=0",
                "premise: a table to rewrite exists in that database")
    expect.text(run("VACUUM FULL rw"), "rc=0", "VACUUM FULL succeeds without the extension")
    expect.text(run("CLUSTER rw USING rw_pkey"), "rc=0", "CLUSTER succeeds without the extension")
    expect.text(run("ALTER TABLE rw ALTER COLUMN t TYPE varchar(64)"), "rc=0",
                "ALTER COLUMN TYPE succeeds without the extension")
    expect.text(run("CREATE MATERIALIZED VIEW rwm AS SELECT * FROM rw"), "rc=0",
                "CREATE MATERIALIZED VIEW succeeds without the extension")
    expect.text(run("CREATE MATERIALIZED VIEW rwr AS SELECT * FROM rw WITH NO DATA"), "rc=0",
                "premise: a matview to refresh exists, made WITH NO DATA")
    expect.text(run("REFRESH MATERIALIZED VIEW rwr"), "rc=0",
                "and REFRESH MATERIALIZED VIEW does too, without the extension")
    expect.text(run("TRUNCATE rw"), "rc=0", "TRUNCATE succeeds without the extension")


def test_a_relation_older_than_the_extension_is_cleaned_up(pgc_cluster, dbs, expect):
    pre = dbs("aac_pre", False)
    run = lambda *s: _dbrun(pgc_cluster, pre, *s)  # noqa: E731
    expect.text(run("CREATE TABLE early (id int)"), "rc=0", "premise: a table exists before the extension does")
    expect.text(run("CREATE EXTENSION pgcolumnar"), "rc=0", "premise: and the extension is installed after it")
    expect.text(run("ALTER TABLE early SET ACCESS METHOD pgcolumnar",
                    "SELECT pgcolumnar.set_options('early', stripe_row_limit => 6000)"), "rc=0",
                "premise: the pre-existing table converts to columnar and records options")
    expect.text(_q(pgc_cluster, pre, "SELECT count(*) FROM pgcolumnar.options WHERE regclass = 'early'::regclass"),
                "1", "premise: its options row is really there")
    expect.text(run("ALTER TABLE early SET ACCESS METHOD heap", "DROP TABLE early"), "rc=0",
                "and dropping it after SET ACCESS METHOD heap leaves no orphan")
    expect.text(_q(pgc_cluster, pre, "SELECT count(*) FROM pgcolumnar.options"), "0",
                "the pre-extension table's options row is gone")


def test_the_gate_is_on_the_options_table_not_the_schema(pgc_cluster, dbs, expect):
    dx = dbs("aac_dropx", False)
    run = lambda *s: _dbrun(pgc_cluster, dx, *s)  # noqa: E731
    expect.text(run("CREATE EXTENSION pgcolumnar"), "rc=0", "premise: the extension installs in a third database")
    expect.text(run("CREATE TABLE dx (id int, a int, b text) USING pgcolumnar",
                    "SELECT pgcolumnar.set_options('dx', stripe_row_limit => 5000)",
                    "SELECT pgcolumnar.add_projection('dx','p1',ARRAY['a','b'],ARRAY['a'])"), "rc=0",
                "premise: with a columnar table carrying both relid-keyed catalog rows")
    expect.text(_q(pgc_cluster, dx, "SELECT (SELECT count(*) FROM pgcolumnar.options) || '/' || "
                                    "(SELECT count(*) FROM pgcolumnar.projection_declaration)"), "1/1",
                "premise: those rows are really there before the extension goes")
    # the member skip is what stops the hook opening a sibling catalog DROP EXTENSION removed
    expect.text(run("DROP EXTENSION pgcolumnar CASCADE"), "rc=0", "DROP EXTENSION succeeds with those catalogs populated")
    expect.text(_q(pgc_cluster, dx, "SELECT count(*) FROM pg_namespace WHERE nspname = 'pgcolumnar'"), "1",
                "premise: DROP EXTENSION left the schema behind")
    expect.text(_q(pgc_cluster, dx, "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                                    "WHERE n.nspname = 'pgcolumnar' AND c.relname = 'options'"), "0",
                "and took pgcolumnar.options with it")
    expect.text(run("CREATE TABLE dxh (i int primary key, t text)"), "rc=0", "a plain table is created after DROP EXTENSION")
    expect.text(run("DROP TABLE dxh"), "rc=0", "and dropped there, after DROP EXTENSION")
    expect.text(run("CREATE TEMP TABLE dxt (i int)", "DROP TABLE dxt"), "rc=0",
                "an explicit DROP of a temp table succeeds after DROP EXTENSION")
    expect.text(run("CREATE TABLE dxr (i int primary key, t text)",
                    "INSERT INTO dxr SELECT g, g::text FROM generate_series(1,100) g"), "rc=0",
                "premise: a table to rewrite exists after DROP EXTENSION")
    expect.text(run("VACUUM FULL dxr"), "rc=0", "VACUUM FULL succeeds after DROP EXTENSION")
    expect.text(run("CLUSTER dxr USING dxr_pkey"), "rc=0", "CLUSTER succeeds after DROP EXTENSION")
    expect.text(run("ALTER TABLE dxr ALTER COLUMN t TYPE varchar(64)"), "rc=0", "ALTER COLUMN TYPE succeeds after DROP EXTENSION")
    expect.text(run("CREATE MATERIALIZED VIEW dxm AS SELECT * FROM dxr"), "rc=0",
                "CREATE MATERIALIZED VIEW succeeds after DROP EXTENSION")
    expect.text(run("CREATE MATERIALIZED VIEW dxr_m AS SELECT * FROM dxr WITH NO DATA"), "rc=0",
                "premise: a matview to refresh exists after DROP EXTENSION, made WITH NO DATA")
    expect.text(run("REFRESH MATERIALIZED VIEW dxr_m"), "rc=0", "and REFRESH MATERIALIZED VIEW does too, after DROP EXTENSION")
    expect.text(run("TRUNCATE dxr"), "rc=0", "TRUNCATE succeeds after DROP EXTENSION")
