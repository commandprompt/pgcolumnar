"""import_arrow into a table with an exclusion constraint enforces it, as DML does
(follow-up to #153). The pytest twin of `test/import_exclusion.sh`, every check by the
shell's name (#1341).

EXCLUDE USING btree (k WITH =) is an exclusion constraint and not a unique index, so
index_insert alone does not enforce it. A clean import succeeds and its rows reach the
index; importing the same keys again raises and leaves the row count alone, as an
ordinary INSERT of one of them does. A rewrite that moves rows -- one key in ten
deleted first, so compact_rewrite really rewrites a group -- must not check, because
the row it replaces is still visible.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading (#1339). The arrow file lives in `server_dir`, because the
backend writes and reads it.
"""
import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_import_exclusion"
ROWS = 500


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _value(dsn, sql, sets=()):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for s in sets:
                conn.execute(s)
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return query_error(f"no value from {sql}") if row is None or row[0] is None else row[0]


def _error(dsn, sql):
    """-> the error's text, or '' when SQL succeeded."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)
    except psycopg.Error as exc:
        return str(exc)
    return ""


@pytest.fixture(scope="module")
def ix(pgc_cluster, server_dir):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        arrow = server_dir / "pgc_excl.arrow"
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            c.execute("CREATE TABLE ex_src (k int, v text) USING pgcolumnar")
            c.execute(f"INSERT INTO ex_src SELECT g, 'r' || g FROM generate_series(1, {ROWS}) g")
            c.execute(f"SELECT pgcolumnar.export_arrow('ex_src', '{arrow}')")
            c.execute("CREATE TABLE ex_t (k int, v text) USING pgcolumnar")
            c.execute("ALTER TABLE ex_t ADD CONSTRAINT ex_t_k EXCLUDE USING btree (k WITH =)")
        r = {"kind": _value(dsn, "SELECT indisunique || '/' || indisexclusion FROM pg_index i "
                                 "JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = 'ex_t_k'")}
        r["import"] = _value(dsn, f"SELECT pgcolumnar.import_arrow('ex_t', '{arrow}')")
        r["rows"] = _value(dsn, "SELECT count(*) FROM ex_t")
        r["index"] = _value(dsn, f"SELECT count(*) FROM ex_t WHERE k = {ROWS // 2}",
                            ("SET pgcolumnar.enable_custom_scan = off", "SET enable_seqscan = off"))
        r["violation"] = _error(dsn, f"SELECT pgcolumnar.import_arrow('ex_t', '{arrow}')")
        r["rows after"] = _value(dsn, "SELECT count(*) FROM ex_t")
        r["dml"] = _error(dsn, f"INSERT INTO ex_t VALUES ({ROWS // 2}, 'dup')")
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("DELETE FROM ex_t WHERE k % 10 = 0")
        r["rewrote"] = _value(dsn, "SELECT pgcolumnar.compact_rewrite('ex_t', 0.0)")
        r["survived"] = _value(dsn, "SELECT count(*) FROM ex_t")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _raised(err):
    return "yes" if "exclusion constraint" in err else f"no ({err})"


def test_the_constraint_is_an_exclusion_constraint_not_a_unique_index(ix, expect):
    expect.text(str(ix["kind"]), "false/true", "the constraint is an exclusion constraint, not a unique index")


def test_importing_rows_that_violate_nothing_succeeds(ix, expect):
    expect.text(str(ix["import"]), str(ROWS), "importing rows that violate nothing succeeds")


def test_and_the_rows_are_there(ix, expect):
    expect.text(str(ix["rows"]), str(ROWS), "and the rows are there")


def test_an_index_scan_finds_an_imported_row(ix, expect):
    expect.text(str(ix["index"]), "1", "an index scan finds an imported row")


def test_importing_rows_that_violate_the_constraint_raises(ix, expect):
    expect.text(_raised(ix["violation"]), "yes", "importing rows that violate the constraint raises")


def test_the_failed_import_left_the_row_count_alone(ix, expect):
    expect.text(str(ix["rows after"]), str(ROWS), "and the failed import left the row count alone")


def test_an_ordinary_insert_of_the_same_violation_also_raises(ix, expect):
    expect.text(_raised(ix["dml"]), "yes", "an ordinary INSERT of the same violation also raises")


def test_a_rewrite_of_the_same_table_still_succeeds(ix, expect):
    n = ix["rewrote"]
    expect.text("yes" if isinstance(n, int) and n >= 1 else f"no ({n})", "yes",
                "a rewrite of the same table still succeeds")


def test_and_the_rows_survived_it(ix, expect):
    expect.text(str(ix["survived"]), str(ROWS - ROWS // 10), "and the rows survived it")
