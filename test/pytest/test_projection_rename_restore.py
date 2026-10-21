"""A projection's declaration follows a column rename, so it rebuilds after pg_dump and
restore (#888). The pytest twin of `test/projection_rename_restore.sh`, every check by
the shell's name (#1341).

A projection is declared by column names, and restore rebuilds it from the declaration.
A column renamed after the declaration must be renamed in it too, on the table itself
and on a partition renamed through its parent, or the restored declaration names a
column that no longer exists. Asserted: the live projections still read after the
renames; pg_dump and restore succeed; and both projections rebuild in the restored
database with every row.

ON PRIVATE DATABASES: the source, and the database it is restored into.
"""
import pathlib
import subprocess

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_projection_rename_restore"
RESTORE_DB = f"{DB}_renamed"


def _tool(cluster, name, *args):
    """-> the tool's exit status, or query_error() when it could not be run at all: a missing
    binary raised OSError out of the fixture and errored every test, none by name, where
    the shell records a failed pg_dump (#1625 review)."""
    try:
        return subprocess.run([str(pathlib.Path(cluster.bindir) / name), "-h", "127.0.0.1", "-p", str(cluster.port),
                               "-U", "postgres", *args], capture_output=True, text=True).returncode
    except OSError as exc:
        return query_error(f"{name} could not run: {exc}")


def _one(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            return str(conn.execute(sql).fetchone()[0])
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


@pytest.fixture(scope="module")
def pn(pgc_cluster, server_dir):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        for d in (DB, RESTORE_DB):
            a.execute(f"DROP DATABASE IF EXISTS {d} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        with psycopg.connect(dsn, autocommit=True) as conn:
            for sql in [
                "CREATE EXTENSION IF NOT EXISTS pgcolumnar",
                "CREATE TABLE prr (id int, payload text, sort_key int) USING pgcolumnar",
                "SELECT pgcolumnar.add_projection('prr', 'by_sort', ARRAY['id','sort_key'], ARRAY['sort_key'])",
                "INSERT INTO prr SELECT g, 'row-' || g, (g * 7) % 100 FROM generate_series(1,2000) g",
                "ALTER TABLE prr RENAME COLUMN sort_key TO renamed_key",
            ]:
                conn.execute(sql)
        r = {"live": _one(dsn, "SELECT count(*) FROM pgcolumnar.read_projection('prr','by_sort')")}
        with psycopg.connect(dsn, autocommit=True) as conn:
            for sql in [
                "CREATE TABLE prr_parent (id int, sort_key int) PARTITION BY RANGE (id)",
                "CREATE TABLE prr_child PARTITION OF prr_parent FOR VALUES FROM (0) TO (100) USING pgcolumnar",
                "SELECT pgcolumnar.add_projection('prr_child', 'child_sort', ARRAY['id','sort_key'], ARRAY['sort_key'])",
                "INSERT INTO prr_parent SELECT g, g % 10 FROM generate_series(1,99) g",
                "ALTER TABLE prr_parent RENAME COLUMN sort_key TO renamed_key",
            ]:
                conn.execute(sql)
        r["child live"] = _one(dsn, "SELECT count(*) FROM pgcolumnar.read_projection('prr_child','child_sort')")

        dump = str(server_dir / "projection-rename.sql")
        r["dump"] = _tool(pgc_cluster, "pg_dump", "-d", DB, "-f", dump)
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"CREATE DATABASE {RESTORE_DB}")
        r["restore"] = _tool(pgc_cluster, "psql", "-d", RESTORE_DB, "-v", "ON_ERROR_STOP=1", "-q", "-f", dump)
        rdsn = pgc_cluster.dsn(dbname=RESTORE_DB)
        r["rebuilt"] = _one(rdsn, "SELECT pgcolumnar.rebuild_projections('prr')")
        r["rebuilt rows"] = _one(rdsn, "SELECT count(*) FROM pgcolumnar.read_projection('prr','by_sort')")
        r["child rebuilt"] = _one(rdsn, "SELECT pgcolumnar.rebuild_projections('prr_child')")
        r["child rows"] = _one(rdsn, "SELECT count(*) FROM pgcolumnar.read_projection('prr_child','child_sort')")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            for d in (DB, RESTORE_DB):
                a.execute(f"DROP DATABASE IF EXISTS {d} WITH (FORCE)")


def test_the_live_projection_reads_after_the_rename(pn, expect):
    expect.text(pn["live"], "2000", "the live projection still reads after the rename")


def test_the_child_projection_reads_after_a_parent_rename(pn, expect):
    expect.text(pn["child live"], "99", "the child projection still reads after a parent rename")


def test_pg_dump_succeeds(pn, expect):
    expect.num(pn["dump"], 0, "pg_dump succeeds")


def test_restore_succeeds(pn, expect):
    expect.num(pn["restore"], 0, "restore succeeds")


def test_the_renamed_projection_rebuilds_after_restore(pn, expect):
    expect.text(pn["rebuilt"], "1", "the renamed projection rebuilds after restore")


def test_the_rebuilt_projection_holds_every_row(pn, expect):
    expect.text(pn["rebuilt rows"], "2000", "the rebuilt projection contains every restored row")


def test_the_descendant_declaration_follows_the_parent_rename(pn, expect):
    expect.text(pn["child rebuilt"], "1", "the descendant declaration follows the parent rename")


def test_the_rebuilt_child_projection_holds_every_row(pn, expect):
    expect.text(pn["child rows"], "99", "the rebuilt child projection contains every restored row")
