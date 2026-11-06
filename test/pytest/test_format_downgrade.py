"""An alpha4 binary refuses a table this build wrote, and still reads one it only read (#1137).

1.0-alpha5 stops writing a validity bitmap for a chunk with no nulls (#1130), and an
alpha4 binary misreads such a chunk without refusing it: with the real v1.0-alpha4
library, 9 of 40 single-row fetches returned NULL for a row that holds a value. The
fix is the stamp. A table this build WRITES carries format_version 2, alpha4 refuses
any stamp but 1, and a table this build only READS keeps 1.

THE CLAIM IS ABOUT TWO BINARIES, so this test uses both: it builds v1.0-alpha4,
writes with it, upgrades to this tree, writes again, then puts the old library back
under the upgraded catalog -- which is what a package rollback does. Refusal is
asserted by SQLSTATE (0A000) on a scan and on an index fetch with the custom scan
off, the path that returned the silent NULLs.

ON A CLUSTER OF ITS OWN, and the prefix is put back exactly. The session cluster
shares the install prefix, so every `pgcolumnar*` file in the library and extension
directories is saved first -- bytes and mtime -- and restored in a `finally` by
atomic rename, with anything the old install added removed. The mtime matters: the
harness compares the library's mtime against the postmaster's start, and a newer
library would make later files refuse to run. A copy written over a library a
running postmaster has mapped corrupts it, so nothing is written in place.

Independent of `test/format_downgrade.sh`: its own tables, row counts and keys, and
it restores the prefix by snapshot where the shell suite reinstalls.
"""

import hashlib
import os
import pathlib
import shutil
import subprocess
import tempfile

import psycopg

OLD_REF = "v1.0-alpha4"
ROWS = 2500
# sum(1..2500)
FULL_SUM = ROWS * (ROWS + 1) // 2
SRCDIR = pathlib.Path(__file__).resolve().parent.parent.parent


def _beside_other_workers(env):
    """True when this process may share the install prefix with other workers.

    pytest-xdist sets PYTEST_XDIST_WORKER in every worker and
    PYTEST_XDIST_WORKER_COUNT to the number of workers. A serial run sets
    neither. A worker with no stated count is treated as not alone: guessing
    "alone" is the direction that breaks someone else's test.
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


def _snapshot(dirs):
    """{path: (bytes, (atime, mtime))} for every pgcolumnar* file in `dirs`."""
    snap = {}
    for d in dirs:
        for p in pathlib.Path(d).glob("pgcolumnar*"):
            if p.is_file():
                st = p.stat()
                snap[p] = (p.read_bytes(), (st.st_atime, st.st_mtime))
    return snap


def _restore(snap, dirs):
    """Put the prefix back exactly: same files, same bytes, same mtimes."""
    for d in dirs:
        for p in pathlib.Path(d).glob("pgcolumnar*"):
            if p.is_file() and p not in snap:
                p.unlink()
    for p, (data, times) in snap.items():
        tmp = p.with_name(p.name + ".pgc-restore")
        tmp.write_bytes(data)
        os.utime(tmp, times)
        os.replace(tmp, p)


def _place(src, dst):
    """Install one file by atomic rename, never by writing into a mapped one."""
    tmp = pathlib.Path(str(dst) + ".pgc-place")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


def _sqlstate(dsn, statements):
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            for s in statements:
                c.execute(s)
        return "ok"
    except psycopg.Error as e:
        return e.sqlstate or "?"


def test_an_alpha4_binary_refuses_what_this_build_wrote(pgc_cluster, expect):
    from pgc_cluster import Cluster, pick_port

    if _beside_other_workers(os.environ):
        expect.cannot_run("UNMET_PRECONDITION",
                          "swaps the library in the shared install prefix, which a backend "
                          "another xdist worker starts would load; run this file without -n")
        return

    pg_config = pgc_cluster.pg_config
    libdir, sharedir = pgc_cluster.libdir, pgc_cluster.sharedir
    extdir = os.path.join(sharedir, "extension")

    # The old source. CI checks out without tags, so fetch the one this needs.
    have = subprocess.run(["git", "-C", str(SRCDIR), "rev-parse", "-q", "--verify",
                           f"{OLD_REF}^{{commit}}"], capture_output=True)
    if have.returncode != 0:
        subprocess.run(["git", "-C", str(SRCDIR), "fetch", "-q", "--depth", "1", "origin",
                        f"refs/tags/{OLD_REF}:refs/tags/{OLD_REF}"], capture_output=True)
    have = subprocess.run(["git", "-C", str(SRCDIR), "rev-parse", "-q", "--verify",
                           f"{OLD_REF}^{{commit}}"], capture_output=True)
    if have.returncode != 0:
        expect.cannot_run("ABSENT_FIXTURE", f"{OLD_REF} is not in this clone and could "
                          "not be fetched")
        return

    work = pathlib.Path(tempfile.mkdtemp(prefix="pgc-downgrade-"))
    os.chmod(work, 0o777)
    snap = _snapshot([libdir, extdir])
    expect.at_least(len(snap), 2, "premise: the prefix holds the tree's own install to put back")
    cluster = None
    try:
        old = work / "old"
        _run(["git", "clone", "-q", "--shared", str(SRCDIR), str(old)])
        _run(["git", "-C", str(old), "checkout", "-q", "--detach", OLD_REF])
        built = subprocess.run(["make", "-C", str(old), f"PG_CONFIG={pg_config}",
                                f"-j{os.cpu_count() or 2}"], capture_output=True, text=True)
        expect.num(built.returncode, 0, "premise: the old build compiles")
        old_so = old / "pgcolumnar.so"
        new_so = work / "new.so"
        shutil.copyfile(os.path.join(libdir, "pgcolumnar.so"), new_so)
        expect.text("differ" if old_so.read_bytes() != new_so.read_bytes() else "same",
                    "differ", "premise: the two libraries differ")

        # Install the old build: library AND its SQL and control files.
        _run(["make", "-C", str(old), f"PG_CONFIG={pg_config}", "install"])
        datadir = work / "data"
        datadir.mkdir()
        cluster = Cluster(pg_config, "downgrade", datadir, pick_port(7))
        cluster.initdb()
        cluster.start()
        dsn = cluster.dsn()

        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION pgcolumnar")
            for t in ("kept", "grown"):
                c.execute(f"CREATE TABLE {t} (k int, body text) USING pgcolumnar")
                c.execute(f"SELECT pgcolumnar.set_options('{t}', compression => 'none')")
                c.execute(f"INSERT INTO {t} SELECT g, repeat(md5((g*3)::text), 5)"
                          f" FROM generate_series(1,{ROWS}) g")
                c.execute(f"CREATE INDEX ON {t} (k)")
            stored = [c.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                      for t in ("kept", "grown")]
        expect.text(f"{stored[0]}|{stored[1]}", f"{ROWS}|{ROWS}",
                    "premise: the old build stored both tables")

        # This tree: put the session's own install back, upgrade, and write.
        cluster.stop()
        _restore(snap, [libdir, extdir])
        cluster.start()
        with psycopg.connect(dsn, autocommit=True) as c:
            try:
                c.execute("ALTER EXTENSION pgcolumnar UPDATE")
                upd = "ok"
            except psycopg.Error as e:
                upd = str(e).splitlines()[0]
            expect.text(upd, "ok", "ALTER EXTENSION UPDATE succeeds")
            got = c.execute("SELECT count(*), sum(k) FROM kept").fetchone()
            expect.text(f"{got[0]}|{got[1]}", f"{ROWS}|{FULL_SUM}",
                        "this build reads the table the old build wrote")
            c.execute(f"INSERT INTO grown SELECT g, repeat(md5((g*3)::text), 5)"
                      f" FROM generate_series({ROWS + 1},{ROWS + 80}) g")
            c.execute("CREATE TABLE fresh (k int, body text) USING pgcolumnar")
            c.execute("SELECT pgcolumnar.set_options('fresh', compression => 'none')")
            c.execute(f"INSERT INTO fresh SELECT g, repeat(md5((g*3)::text), 5)"
                      f" FROM generate_series(1,{ROWS}) g")
            c.execute("CREATE INDEX ON fresh (k)")
            stamps = " ".join(str(c.execute(
                "SELECT format_version FROM pgcolumnar.storage"
                f" WHERE storage_id = pgcolumnar.get_storage_id('{t}')").fetchone()[0])
                for t in ("kept", "grown", "fresh"))
        expect.text(stamps, "1 2 2", "stamps after this build: read, appended, new")

        # Roll the library back under the upgraded catalog.
        cluster.stop()
        _place(old_so, os.path.join(libdir, "pgcolumnar.so"))
        cluster.start()
        expect.text("old" if pathlib.Path(libdir, "pgcolumnar.so").read_bytes()
                    == old_so.read_bytes() else "other", "old",
                    "premise: the server now runs the old library")

        def fetch(t):
            return _sqlstate(dsn, ["SET pgcolumnar.enable_custom_scan = off",
                                   "SET enable_seqscan = off", "SET enable_bitmapscan = off",
                                   f"SELECT body FROM {t} WHERE k = 777"])

        with psycopg.connect(dsn, autocommit=True) as c:
            got = c.execute("SELECT count(*), sum(k) FROM kept").fetchone()
        expect.text(f"{got[0]}|{got[1]}", f"{ROWS}|{FULL_SUM}",
                    "the old build still reads a table this build only read")
        # THE VALUE, NOT ONLY "IT RAN" (#1339). The defect #1137 measured was a fetch
        # that SUCCEEDS and returns NULL for a row holding a value, so reading only the
        # SQLSTATE passed it. Compared with the generator, not with a read.
        with psycopg.connect(dsn, autocommit=True) as c:
            for st in ("SET pgcolumnar.enable_custom_scan = off", "SET enable_seqscan = off",
                       "SET enable_bitmapscan = off"):
                c.execute(st)
            row = c.execute("SELECT coalesce(body, '<NULL>') FROM kept WHERE k = 777").fetchone()
        expect.text(row[0] if row else "no row", hashlib.md5(b"2331").hexdigest() * 5,
                    "and fetches from it by index")
        expect.text(_sqlstate(dsn, ["SELECT count(body) FROM grown"]), "0A000",
                    "the old build refuses a table this build appended to, on a scan")
        expect.text(fetch("grown"), "0A000", "the old build refuses a table this build appended to, on an index fetch")
        expect.text(_sqlstate(dsn, ["SELECT count(body) FROM fresh"]), "0A000",
                    "the old build refuses a table this build wrote, on a scan")
        expect.text(fetch("fresh"), "0A000", "the old build refuses a table this build wrote, on an index fetch")
    finally:
        if cluster is not None:
            cluster.stop()
        _restore(snap, [libdir, extdir])
        shutil.rmtree(work, ignore_errors=True)


def test_the_library_swap_declines_beside_other_workers(expect):
    """The swap puts alpha4's library in the SHARED prefix for a few seconds.

    A backend that another xdist worker starts during that window loads alpha4
    against this tree's catalogs, and fails in an unrelated test, at random, in a
    way a serial run cannot reproduce. Atomic renames protect processes that had
    already mapped the library, not new ones. So the test declines -- named,
    UNMET_PRECONDITION, never a pass -- whenever it is not alone. Found by
    @jdatcmd reviewing #1301.

    Driven through the decision itself, with the environments pytest-xdist sets,
    so both directions are asserted without starting workers.
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
