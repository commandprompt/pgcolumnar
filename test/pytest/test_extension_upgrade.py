"""An install of the oldest release keeps working when this tree's library replaces it (#382).

Every SQL-callable function records its C link name in pg_proc.prosrc when the
extension is created. Rename a link name in C and the rows a released install holds
point at a symbol the new library no longer exports. Replacing only the library --
which is what a package upgrade does -- then leaves the extension inert: reading an
existing table fails with "could not find function", and so does creating one. It
compiles, it links, and every test passes on a fresh install.

THE CLAIM IS ABOUT TWO BUILDS, so this test uses both. It builds v1.0-alpha, the
release nightly.yml upgrades from, creates the extension and a table with it, puts this
tree's install in its place, runs ALTER EXTENSION UPDATE, and then requires the old
table to read and take rows, a new table to be creatable, the maintenance function to
run, every C link name to sit in the pgcolumnar namespace, and the upgraded catalog to
equal a fresh install of the new default_version.

`test_native_upgrade_converge.py` proves the convergence from every release's frozen
install script under this tree's library. It never runs an old LIBRARY, so the rows
an old build wrote and the link names an old build's CREATE EXTENSION recorded are
this file's alone.

ON A CLUSTER OF ITS OWN, and the prefix is put back exactly, as
`test_format_downgrade.py` does, through `pgc_cluster.snapshot_prefix`: every
`pgcolumnar*` file in the library, bitcode and extension directories (a `pgcolumnar*`
directory whole, as an LLVM build installs its bitcode) is saved first, bytes and
mtime, and restored in a `finally` by atomic rename, with anything the old install
added removed.

The pytest twin of `test/extension_upgrade.sh`, by the shell's names. Independent of
it: its own database names and rows, and it restores the prefix by snapshot where the
shell suite reinstalls.
"""

import os
import pathlib
import shutil
import subprocess
import tempfile

import psycopg

OLD_REF = "v1.0-alpha"
ROWS = 1200
SRCDIR = pathlib.Path(__file__).resolve().parent.parent.parent

# The shell gate's object list, one line per object in the pgcolumnar schema: every
# function's definition (which carries its link symbol), ACL and comment; every
# relation's kind, ACL and comment; every column's type, NOT NULL and comment; every
# non-base type; every foreign-data wrapper's handler and validator.
SNAPSHOT = """
  SELECT 'FN|'||p.oid::regprocedure||'|'||CASE WHEN p.prokind='a' THEN md5((SELECT ag::text FROM pg_aggregate ag WHERE ag.aggfnoid=p.oid)) ELSE md5(pg_get_functiondef(p.oid)) END||'|'||coalesce(p.proacl::text,'(def)')||'|'||md5(coalesce(obj_description(p.oid,'pg_proc'),''))
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

# A C function of the extension whose link name is outside the pgcolumnar namespace.
UNNAMESPACED = """
  SELECT coalesce(string_agg(p.proname||' -> '||p.prosrc, ', ' ORDER BY p.proname), '')
    FROM pg_proc p
    JOIN pg_depend d ON d.objid = p.oid AND d.deptype = 'e'
    JOIN pg_extension e ON e.oid = d.refobjid AND e.extname = 'pgcolumnar'
   WHERE p.prolang = (SELECT oid FROM pg_language WHERE lanname = 'c')
     AND p.prosrc !~ '^pgcolumnar'
"""


def _beside_other_workers(env):
    """True when this process may share the install prefix with other workers.

    A worker with no stated count is treated as not alone: guessing "alone" is the
    direction that breaks someone else's test.
    """
    count = env.get("PYTEST_XDIST_WORKER_COUNT")
    if count is not None:
        try:
            return int(count) > 1
        except ValueError:
            return True
    return "PYTEST_XDIST_WORKER" in env


def _run(argv, **kw):
    return subprocess.run(argv, check=True, capture_output=True, text=True, **kw)


def _default_version():
    for line in (SRCDIR / "pgcolumnar.control").read_text().splitlines():
        key, _, val = line.partition("=")
        if key.strip() == "default_version":
            return val.strip().strip("'")
    return "?"


def _catalog(dsn):
    with psycopg.connect(dsn, autocommit=True) as c:
        return sorted(r[0] for r in c.execute(SNAPSHOT).fetchall())


def _answer(c, sql):
    """-> the statement's first column, or the first line of its error."""
    try:
        row = c.execute(sql).fetchone()
        return "" if row is None or row[0] is None else str(row[0])
    except psycopg.Error as e:
        return str(e).splitlines()[0]


def test_an_old_install_works_under_this_library(pgc_cluster, expect):
    from pgc_cluster import Cluster, pick_port, restore_prefix, snapshot_prefix

    if _beside_other_workers(os.environ):
        expect.cannot_run("UNMET_PRECONDITION",
                          "swaps the install in the shared prefix, which a backend another "
                          "xdist worker starts would load; run this file without -n")
        return

    pg_config = pgc_cluster.pg_config
    libdir, sharedir = pgc_cluster.libdir, pgc_cluster.sharedir
    extdir = os.path.join(sharedir, "extension")

    # The old source. CI checks out without tags, so fetch the one this needs.
    def have_ref():
        return subprocess.run(["git", "-C", str(SRCDIR), "rev-parse", "-q", "--verify",
                               f"{OLD_REF}^{{commit}}"], capture_output=True).returncode == 0
    if not have_ref():
        subprocess.run(["git", "-C", str(SRCDIR), "fetch", "-q", "--depth", "1", "origin",
                        f"refs/tags/{OLD_REF}:refs/tags/{OLD_REF}"], capture_output=True)
    if not have_ref():
        expect.cannot_run("ABSENT_FIXTURE", f"{OLD_REF} is not in this clone and could "
                          "not be fetched")
        return

    work = pathlib.Path(tempfile.mkdtemp(prefix="pgc-extupgrade-"))
    os.chmod(work, 0o777)
    snap = snapshot_prefix(libdir, extdir)
    expect.at_least(len(snap["files"]), 2, "premise: the prefix holds the tree's own install to put back")
    cluster = None
    try:
        old = work / "old"
        _run(["git", "clone", "-q", "--shared", str(SRCDIR), str(old)])
        _run(["git", "-C", str(old), "checkout", "-q", "--detach", OLD_REF])
        built = subprocess.run(["make", "-C", str(old), f"PG_CONFIG={pg_config}",
                                f"-j{os.cpu_count() or 2}"], capture_output=True, text=True)
        expect.num(built.returncode, 0, "premise: the old build compiles")
        # The old build's library AND its SQL and control files.
        _run(["make", "-C", str(old), f"PG_CONFIG={pg_config}", "install"])

        datadir = work / "data"
        datadir.mkdir()
        cluster = Cluster(pg_config, "extupgrade", datadir, pick_port(8))
        cluster.initdb()
        cluster.start()
        with psycopg.connect(cluster.dsn(), autocommit=True) as c:
            c.execute("CREATE DATABASE eu_old")
            c.execute("CREATE DATABASE eu_fresh")
        dsn = cluster.dsn("eu_old")
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION pgcolumnar")
            c.execute("CREATE TABLE kept (k int, note text) USING pgcolumnar")
            c.execute(f"INSERT INTO kept SELECT g, 'n' || g FROM generate_series(1, {ROWS}) g")
            stored = _answer(c, "SELECT count(*) FROM kept")
            old_ver = _answer(c, "SELECT extversion FROM pg_extension WHERE extname = 'pgcolumnar'")
        # A failed count is the error's first line: fail the premise by that text,
        # rather than let int() raise a ValueError that names nothing.
        if stored.isdigit():
            expect.num(int(stored), ROWS, "premise: the old install stored its rows")
        else:
            expect.text(stored, str(ROWS), "premise: the old install stored its rows")

        # This tree's install in the old one's place: the library a package upgrade
        # replaces, and the scripts ALTER EXTENSION UPDATE needs.
        cluster.stop()
        restore_prefix(snap)
        cluster.start()

        new_default = _default_version()
        with psycopg.connect(dsn, autocommit=True) as c:
            paths = _answer(c, "SELECT count(*) FROM pg_extension_update_paths('pgcolumnar')"
                               f" WHERE source = '{old_ver}' AND target = '{new_default}'"
                               " AND path IS NOT NULL")
            # One row per (source, target), so a reachable target is exactly 1.
            if new_default == old_ver:
                expect.text(f"none needed: {old_ver}", f"none needed: {new_default}",
                            "an update path leads from the old version to the new default")
            else:
                expect.text(f"{old_ver} -> {new_default}: {paths or '0'} path",
                            f"{old_ver} -> {new_default}: 1 path",
                            "an update path leads from the old version to the new default")
            try:
                c.execute("ALTER EXTENSION pgcolumnar UPDATE")
                upd = "ok"
            except psycopg.Error as e:
                upd = str(e).splitlines()[0]
            expect.text(upd, "ok", "ALTER EXTENSION UPDATE succeeds")

            expect.num(c.execute("SELECT count(*) FROM kept").fetchone()[0], ROWS,
                       "existing rows still readable")
            c.execute("INSERT INTO kept VALUES (0, 'after')")
            expect.num(c.execute("SELECT count(*) FROM kept").fetchone()[0], ROWS + 1,
                       "insert into an existing table")
            c.execute("CREATE TABLE made (a int) USING pgcolumnar")
            c.execute("INSERT INTO made SELECT generate_series(1, 4)")
            expect.num(c.execute("SELECT count(*) FROM made").fetchone()[0], 4,
                       "new columnar table creatable")
            expect.text(_answer(c, "SELECT amname FROM pg_am WHERE amname = 'pgcolumnar'"),
                        "pgcolumnar", "access method still bound")
            # It returns void, so the answer is that the call came back, or its error.
            try:
                c.execute("SELECT pgcolumnar.vacuum('kept')")
                called = "called"
            except psycopg.Error as e:
                called = str(e).splitlines()[0]
            expect.text(called, "called", "maintenance function callable")
            expect.text(_answer(c, UNNAMESPACED) or "none outside it", "none outside it",
                        "every C function's link name is namespaced")

        with psycopg.connect(cluster.dsn("eu_fresh"), autocommit=True) as c:
            fresh_err = _answer(c, "CREATE EXTENSION pgcolumnar")
        fresh = _catalog(cluster.dsn("eu_fresh"))
        expect.text("yes" if fresh else f"no: {fresh_err}", "yes",
                    "premise: a fresh install of the new default stood up to compare against")
        if fresh:
            upgraded = _catalog(dsn)
            gone = sorted(set(fresh) - set(upgraded))
            extra = sorted(set(upgraded) - set(fresh))
            diff = "; ".join([f"- {x}" for x in gone[:10]] + [f"+ {x}" for x in extra[:10]])
            expect.text(diff or "identical", "identical",
                        "upgraded catalog is identical to a fresh install of the new default")
    finally:
        if cluster is not None:
            cluster.stop()
        restore_prefix(snap)
        shutil.rmtree(work, ignore_errors=True)


def test_the_install_swap_declines_beside_other_workers(expect):
    """The swap puts v1.0-alpha's install in the SHARED prefix while the old cluster
    is built. A backend another xdist worker starts in that window loads the old
    library against this tree's catalogs and fails in an unrelated test. So the test
    declines -- named, UNMET_PRECONDITION, never a pass -- whenever it is not alone.

    Driven through the decision itself, with the environments pytest-xdist sets.
    """
    expect.text(str(_beside_other_workers({})), "False",
                "a serial run is alone, so the swap runs")
    expect.text(str(_beside_other_workers({"PYTEST_XDIST_WORKER_COUNT": "1"})), "False",
                "one xdist worker is alone, so the swap runs")
    expect.text(str(_beside_other_workers({"PYTEST_XDIST_WORKER_COUNT": "4",
                                           "PYTEST_XDIST_WORKER": "gw2"})), "True",
                "beside three other workers the swap declines")
    expect.text(str(_beside_other_workers({"PYTEST_XDIST_WORKER": "gw0"})), "True",
                "a worker whose count is not stated is assumed not to be alone")


def test_the_prefix_is_put_back_exactly_with_its_bitcode(tmp_path, expect):
    """`make install` on an LLVM build also writes $pkglibdir/bitcode/pgcolumnar.index.bc
    and bitcode/pgcolumnar/**/*.bc. The old build's install overwrites them, and every
    file pytest runs after this one would JIT-inline against v1.0-alpha's bodies unless
    they come back too (#2036 review).

    Driven on a layout of the prefix: the tree's install, then what an old install
    does to it (overwrite, add files and a directory, and lose one), then the restore.
    """
    from pgc_cluster import restore_prefix, snapshot_prefix

    lib, ext = tmp_path / "lib", tmp_path / "share" / "extension"
    tree = ["lib/pgcolumnar.so", "lib/bitcode/pgcolumnar.index.bc",
            "lib/bitcode/pgcolumnar/src/columnar_tableam.bc",
            "lib/bitcode/pgcolumnar/src/columnar_reader.bc",
            "share/extension/pgcolumnar.control",
            "share/extension/pgcolumnar--1.0-alpha5.sql"]
    other = ["lib/plpgsql.so", "lib/bitcode/plpgsql.index.bc",
             "lib/bitcode/plpgsql/pl_exec.bc", "share/extension/plpgsql.control"]
    for i, rel in enumerate(tree + other):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"tree {rel}")
        os.utime(f, (1_000_000 + i, 1_000_000 + i))

    def state():
        return sorted(f"{p.relative_to(tmp_path)}={p.read_text()}@{int(p.stat().st_mtime)}"
                      for p in tmp_path.rglob("*") if p.is_file())

    def dirs():
        return sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_dir())

    before, before_dirs = state(), dirs()
    snap = snapshot_prefix(lib, ext)

    for rel in tree:
        (tmp_path / rel).write_text("old")
    for rel in ["lib/bitcode/pgcolumnar/src/columnar_old_only.bc",
                "lib/bitcode/pgcolumnar/old_dir/x.bc",
                "share/extension/pgcolumnar--1.0-alpha.sql"]:
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("old")
    (tmp_path / "lib/bitcode/pgcolumnar/src/columnar_reader.bc").unlink()

    restore_prefix(snap)

    after = state()
    expect.text(" ".join(sorted(set(after) - set(before))) or "none", "none",
                "nothing the old install wrote is left in the prefix")
    expect.text(" ".join(sorted(set(before) - set(after))) or "none", "none",
                "every file of the tree's install, bitcode included, is back with its bytes "
                "and mtime")
    expect.text(" ".join(sorted(set(dirs()) - set(before_dirs))) or "none", "none",
                "no directory the old install added is left in the prefix")

