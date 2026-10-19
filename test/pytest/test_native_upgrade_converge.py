"""The pytest twin of `test/native_upgrade_converge.sh`, by the shell's names, except its
release-tag arms (see INCOMPLETE in test_compare_to_bash.py).

ALTER EXTENSION pgcolumnar UPDATE must bring a database on any released version to a
catalog identical to a fresh install of the current default_version: every function's
definition, ACL and comment, every relation, column, type and foreign-data wrapper.
The released starting points are frozen install scripts in test/fixtures. They are not
shipped, so each is staged into the extension directory for the module, keeping what
was there, and put back after. A backup already there means an earlier run died before
putting it back: it holds the original, so it is kept, and a premise says so.

The upgrade window (#1341 item 66): docs/installation.md steps 1 and 2 put a new library
in place; step 3, ALTER EXTENSION UPDATE, brings each database's catalog up to it.
Between them a database runs the new library over the old catalog, and the library
deletes from catalog tables the old one does not have (retired_group, new in
1.0-alpha5). Reads and writes must work there; an operation that needs a missing table
must be refused cleanly, change nothing and name step 3; and after step 3 every one of
them must work on the same rows.
"""
import difflib
import pathlib
import re
import shutil
import subprocess

import psycopg
import pytest

from pgc_vacuity import query_error

REPO = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = REPO / "test" / "fixtures"
BACKUP = ".pytest-bak"
# The released starting points, oldest first; the last is the previous release.
VERSIONS = ["1.0-alpha", "1.0-alpha2", "1.0-alpha3", "1.0-alpha4"]

# One line per object in the pgcolumnar schema: every function's definition, ACL and
# comment; every relation's kind, ACL and comment; every column's type, NOT NULL and
# comment; every non-base type; every foreign-data wrapper's handler and validator. The
# comments are here because pg_get_functiondef does not emit COMMENT.
SNAPSHOT = """
  SELECT 'FN|'||p.oid::regprocedure||'|'||md5(pg_get_functiondef(p.oid))||'|'||coalesce(p.proacl::text,'(def)')||'|'||md5(coalesce(obj_description(p.oid,'pg_proc'),''))
    FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='pgcolumnar'
  UNION ALL SELECT 'REL|'||c.relkind::text||'|'||c.relname||'|'||coalesce(c.relacl::text,'(def)')||'|'||md5(coalesce(obj_description(c.oid,'pg_class'),''))
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='pgcolumnar'
  UNION ALL SELECT 'COL|'||c.relname||'.'||a.attname||'|'||format_type(a.atttypid,a.atttypmod)||'|'||a.attnotnull::text||'|'||md5(coalesce(col_description(c.oid,a.attnum),''))
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_attribute a ON a.attrelid=c.oid
    WHERE n.nspname='pgcolumnar' AND c.relkind IN ('r','p') AND a.attnum>0 AND NOT a.attisdropped
  UNION ALL SELECT 'TYP|'||t.typname||'|'||t.typtype::text FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
    WHERE n.nspname='pgcolumnar' AND t.typtype<>'b'
  UNION ALL SELECT 'FDW|'||w.fdwname||'|'||coalesce(h.oid::regproc::text,'-')||'|'||coalesce(v.oid::regproc::text,'-')
    FROM pg_foreign_data_wrapper w LEFT JOIN pg_proc h ON h.oid=w.fdwhandler LEFT JOIN pg_proc v ON v.oid=w.fdwvalidator
    WHERE w.fdwname LIKE 'pgcolumnar%'
"""


def _extdir(pgc_cluster):
    sharedir = subprocess.run([pgc_cluster.pg_config, "--sharedir"], capture_output=True,
                              text=True, check=True).stdout.strip()
    return pathlib.Path(sharedir) / "extension"


def _stage(extdir, versions):
    """Copy each version's fixture into `extdir`, keeping what was there. -> staged paths."""
    staged = []
    for v in versions:
        if not (FIXTURES / f"pgcolumnar--{v}.sql").exists():
            continue
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


def _admin(pgc_cluster, sql):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as c:
        c.execute(sql)


@pytest.fixture(scope="module")
def staging(pgc_cluster):
    """Stage every fixture once for the module. -> (extdir, staged paths, versions whose
    backup was already there before staging, which means an earlier run died)."""
    extdir = _extdir(pgc_cluster)
    died = [v for v in VERSIONS if (extdir / f"pgcolumnar--{v}.sql{BACKUP}").exists()]
    staged = _stage(extdir, VERSIONS)
    try:
        yield extdir, staged, died
    finally:
        _unstage(staged)


@pytest.fixture
def window_db(pgc_cluster, staging):
    """-> (dsn, previous version) of a database at the previous release's catalog."""
    target, prev = _target_and_previous()
    assert len(prev) == 1, f"one upgrade script should lead to {target}, found {prev}"
    prev = prev[0]
    extdir, staged, _ = staging
    assert extdir / f"pgcolumnar--{prev}.sql" in staged, f"{prev} is not among the staged fixtures"

    def admin(sql):
        _admin(pgc_cluster, sql)

    admin("DROP DATABASE IF EXISTS pgc_upgrade_window")
    admin("CREATE DATABASE pgc_upgrade_window")
    dsn = pgc_cluster.dsn(dbname="pgc_upgrade_window")
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(f"CREATE EXTENSION pgcolumnar VERSION '{prev}'")
        yield dsn, prev
    finally:
        admin("DROP DATABASE IF EXISTS pgc_upgrade_window WITH (FORCE)")


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


def _snapshot(dsn):
    with psycopg.connect(dsn, autocommit=True) as c:
        return sorted("|".join(r) for r in c.execute(SNAPSHOT).fetchall())


@pytest.fixture(scope="module")
def reference(pgc_cluster):
    """-> (target version, snapshot of a fresh install of it): what every upgrade must reach."""
    target, _ = _target_and_previous()
    _admin(pgc_cluster, "DROP DATABASE IF EXISTS pgc_conv_fresh")
    _admin(pgc_cluster, "CREATE DATABASE pgc_conv_fresh")
    try:
        dsn = pgc_cluster.dsn(dbname="pgc_conv_fresh")
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(f"CREATE EXTENSION pgcolumnar VERSION '{target}'")
        yield target, _snapshot(dsn)
    finally:
        _admin(pgc_cluster, "DROP DATABASE IF EXISTS pgc_conv_fresh WITH (FORCE)")


def test_the_staged_fixtures_are_the_committed_ones(staging, expect):
    extdir, staged, died = staging
    target, _ = _target_and_previous()
    expect.text(target, "1.0-alpha5", "control default_version is 1.0-alpha5")
    expect.text(" ".join(died) or "none", "none",
                "premise: no earlier run of this suite died with a backup still staged")
    expect.num(len(staged), len(VERSIONS), "premise: every fixture named for staging was staged")
    wrong = [p.name for p in staged if p.read_bytes() != (FIXTURES / p.name).read_bytes()]
    expect.text(" ".join(wrong) or "none", "none",
                "every staged install script is the committed fixture, not a leftover")


def test_a_fresh_install_is_the_reference(reference, expect):
    target, ref = reference
    expect.at_least(len(ref), 101, f"fresh {target} install has objects to compare")


@pytest.mark.parametrize("from_", VERSIONS)
def test_each_release_upgrades_to_a_fresh_catalog(pgc_cluster, staging, reference, from_, expect):
    extdir, _, _ = staging
    target, ref = reference
    if not (extdir / f"pgcolumnar--{from_}.sql").exists():
        expect.text("missing", "present", f"fixture for {from_} present")
    db = "pgc_conv_from_" + re.sub(r"[.-]", "_", from_)
    _admin(pgc_cluster, f"DROP DATABASE IF EXISTS {db}")
    _admin(pgc_cluster, f"CREATE DATABASE {db}")
    try:
        dsn = pgc_cluster.dsn(dbname=db)
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(f"CREATE EXTENSION pgcolumnar VERSION '{from_}'")
        try:
            with psycopg.connect(dsn, autocommit=True) as c:
                c.execute(f"ALTER EXTENSION pgcolumnar UPDATE TO '{target}'")
            applied = "OK"
        except psycopg.Error as exc:
            applied = query_error(f"{exc.sqlstate}: {str(exc).splitlines()[0]}")
        expect.text(applied, "OK", f"{from_} -> {target} upgrade applies without error")
        expect.text(_one(dsn, "SELECT extversion FROM pg_extension WHERE extname = 'pgcolumnar'"),
                    target, f"{from_} is at {target} after upgrade")
        got = _snapshot(dsn)
        diverged = list(difflib.unified_diff(ref, got, "fresh", from_, n=0, lineterm=""))[:40]
        expect.text("\n".join(diverged) or "CONVERGED", "CONVERGED",
                    f"{from_} -> {target} converges to a fresh {target} catalog")
    finally:
        _admin(pgc_cluster, f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
