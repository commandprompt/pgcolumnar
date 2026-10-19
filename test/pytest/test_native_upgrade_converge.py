"""The upgrade window: this library over the previous release's catalog, before ALTER
EXTENSION UPDATE (#1341 item 66). Part of the pytest twin of
`test/native_upgrade_converge.sh`: these arms only, by the shell's names, and the pair
is declared INCOMPLETE.

docs/installation.md steps 1 and 2 put a new library in place; step 3, ALTER EXTENSION
UPDATE, brings each database's catalog up to it. Between them a database runs the new
library over the old catalog, and the library deletes from catalog tables the old one
does not have (retired_group, new in 1.0-alpha5). Reads and writes must work there; an
operation that needs a missing table must be refused cleanly, change nothing and name
step 3; and after step 3 every one of them must work on the same rows.

The previous release is the version the shipped upgrade script to the current
default_version starts from, and its base script is staged from test/fixtures into the
extension directory for the test, as the shell does, then removed.
"""
import pathlib
import re
import shutil
import subprocess

import psycopg
import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = REPO / "test" / "fixtures"
BACKUP = ".pytest-bak"


def _extdir(pgc_cluster):
    sharedir = subprocess.run([pgc_cluster.pg_config, "--sharedir"], capture_output=True,
                              text=True, check=True).stdout.strip()
    return pathlib.Path(sharedir) / "extension"


def _stage(extdir, versions):
    """Copy each version's fixture into `extdir`, keeping what was there. -> staged paths."""
    staged = []
    for v in versions:
        dst = extdir / f"pgcolumnar--{v}.sql"
        backup = dst.with_name(dst.name + BACKUP)
        # A backup already here is the original, left by a run that died: taking it
        # again would copy the installed fixture over it.
        if dst.exists() and not backup.exists():
            shutil.copy2(dst, backup)
        shutil.copy2(FIXTURES / f"pgcolumnar--{v}.sql", dst)
        staged.append(dst)
    return staged


def _unstage(staged):
    """Put back what `_stage` found, or remove what it added."""
    for dst in staged:
        backup = dst.with_name(dst.name + BACKUP)
        if backup.exists():
            shutil.move(backup, dst)
        else:
            dst.unlink(missing_ok=True)


def _target_and_previous():
    target = re.search(r"^default_version\s*=\s*'([^']+)'",
                       (REPO / "pgcolumnar.control").read_text(), re.M).group(1)
    prev = [p.name[len("pgcolumnar--"):-len(f"--{target}.sql")]
            for p in REPO.glob(f"pgcolumnar--*--{target}.sql")]
    return target, prev


@pytest.fixture
def window_db(pgc_cluster):
    """-> (dsn, previous version) of a database at the previous release's catalog."""
    target, prev = _target_and_previous()
    assert len(prev) == 1, f"one upgrade script should lead to {target}, found {prev}"
    prev = prev[0]
    staged = _stage(_extdir(pgc_cluster), [prev])

    def admin(sql):
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as c:
            c.execute(sql)

    admin("DROP DATABASE IF EXISTS pgc_upgrade_window")
    admin("CREATE DATABASE pgc_upgrade_window")
    dsn = pgc_cluster.dsn(dbname="pgc_upgrade_window")
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(f"CREATE EXTENSION pgcolumnar VERSION '{prev}'")
        yield dsn, prev
    finally:
        admin("DROP DATABASE IF EXISTS pgc_upgrade_window WITH (FORCE)")
        _unstage(staged)


def _one(dsn, sql):
    with psycopg.connect(dsn, autocommit=True) as c:
        return c.execute(sql).fetchone()[0]


def _refusal(dsn, sql):
    """-> 'SQLSTATE|names-the-update' or '|no-hint', or 'accepted'."""
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(sql)
        return "accepted"
    except psycopg.Error as exc:
        hint = exc.diag.message_hint or ""
        return f"{exc.sqlstate}|{'names-the-update' if 'ALTER EXTENSION pgcolumnar UPDATE' in hint else 'no-hint'}"


def _succeeds(dsn, *sqls):
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            for sql in sqls:
                c.execute(sql)
        return "succeeded"
    except psycopg.Error as exc:
        return f"refused ({exc.sqlstate}: {str(exc).splitlines()[0]})"


def test_a_run_after_one_that_died_keeps_the_original(tmp_path, expect):
    """A run that dies before `_unstage` leaves the fixture installed and the original in
    the backup. The next run must not back up again: that copies the fixture over the
    original, and the prefix's own file is gone for good."""
    v = "1.0-alpha4"
    dst = tmp_path / f"pgcolumnar--{v}.sql"
    shutil.copy2(FIXTURES / dst.name, dst)
    original = "-- the prefix's own install script\n"
    dst.with_name(dst.name + BACKUP).write_text(original)
    _unstage(_stage(tmp_path, [v]))
    left = dst.read_text() if dst.exists() else None
    expect.text({original: "the prefix's own file", None: "no file"}.get(
                    left, "the fixture" if left == (FIXTURES / dst.name).read_text()
                    else "some other file"),
                "the prefix's own file",
                "a run after one that died restores the prefix's own file, not the fixture")


def test_the_upgrade_window(window_db, expect):
    dsn, prev = window_db
    expect.text(_one(dsn, "SELECT extversion FROM pg_extension WHERE extname = 'pgcolumnar'"), prev,
                "premise: the window database runs this library over the previous release's catalog")
    expect.num(_one(dsn, "SELECT count(*) FROM pg_class WHERE relname = 'retired_group' "
                         "AND relnamespace = 'pgcolumnar'::regnamespace"), 0,
               "premise: that catalog has no retired_group, which this library deletes from")
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("CREATE TABLE w (a int, b text) USING pgcolumnar")
        c.execute("INSERT INTO w SELECT g, 'x' || g FROM generate_series(1, 20000) g")
        c.execute("DELETE FROM w WHERE a % 3 = 0")

    def rows():
        return _one(dsn, "SELECT count(*) || '|' || sum(a) FROM w")

    expect.text(rows(), "13334|133346667", "in the window, a columnar table is written, deleted from and read")
    expect.text(_refusal(dsn, "DROP TABLE w"), "42P01|names-the-update",
                "in the window, DROP TABLE is refused and names ALTER EXTENSION UPDATE")
    expect.text(_refusal(dsn, "TRUNCATE w"), "42P01|names-the-update",
                "in the window, TRUNCATE is refused and names ALTER EXTENSION UPDATE")
    expect.text(_refusal(dsn, "SELECT pgcolumnar.vacuum('w')"), "42P01|names-the-update",
                "in the window, pgcolumnar.vacuum() is refused and names ALTER EXTENSION UPDATE")
    expect.text(rows(), "13334|133346667", "in the window, the refused operations changed nothing")

    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("ALTER EXTENSION pgcolumnar UPDATE")
    expect.text(rows(), "13334|133346667", "after ALTER EXTENSION UPDATE, the same rows are there")
    vacuumed = _succeeds(dsn, "SELECT pgcolumnar.vacuum('w')")
    expect.text(rows() if vacuumed == "succeeded" else vacuumed, "13334|133346667",
                "after ALTER EXTENSION UPDATE, pgcolumnar.vacuum() succeeds and keeps them")
    expect.text(_succeeds(dsn, "TRUNCATE w", "DROP TABLE w"), "succeeded",
                "after ALTER EXTENSION UPDATE, TRUNCATE and DROP TABLE succeed")
