"""Columnar tables survive pg_upgrade across majors (#239, #1341). The pytest twin of
`test/pg_upgrade.sh`, every check by the shell's name.

pg_upgrade carries relation files and the pgcolumnar.* catalogs to a new major. A
cluster on the OLD major gets a columnar table with non-default options, deletes and an
index; an empty cluster on the NEW major, the extension installed on both, is upgraded
in place of it; and the rows, their content, the options, the index and writability
must all survive. The index arm runs with the columnar scan off: right after pg_upgrade
there are no planner statistics, and the planner took the columnar scan for the range,
so without that the arm never touched the index.

TWO MAJORS AT ONCE, like the shell, which is why neither runs in the matrix: the new
major is --pg-config and the old one is PGC_UPGRADE_FROM_PG_CONFIG. Without it, or
with an old major not below the new one, the test declines.
"""
import os
import pathlib
import re
import shutil
import subprocess
import tempfile

import psycopg

REPO = pathlib.Path(__file__).resolve().parents[2]


def _cfg(pg_config, flag):
    return subprocess.run([pg_config, flag], capture_output=True, text=True, check=True).stdout.strip()


def _major(pg_config):
    # "PostgreSQL 19rc1" is 19: the leading digits, not every digit
    return int(re.match(r"\d+", _cfg(pg_config, "--version").split()[1]).group(0))


def _sh(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def _q(port, sql):
    """-> the first column of the first row, or '' when the query cannot run."""
    try:
        with psycopg.connect(f"host=127.0.0.1 port={port} user=postgres dbname=postgres", autocommit=True,
                             connect_timeout=10) as c:
            row = c.execute(sql).fetchone()
            return "" if row is None else row[0]
    except psycopg.Error:
        return ""


def _run(port, *sqls):
    with psycopg.connect(f"host=127.0.0.1 port={port} user=postgres dbname=postgres", autocommit=True) as c:
        for sql in sqls:
            c.execute(sql)


def _cluster(bindir, data, port):
    """initdb -k (data checksums on both sides, or pg_upgrade refuses the pair) and conf."""
    _sh([f"{bindir}/initdb", "-k", "-D", str(data), "-A", "trust"])
    with open(data / "postgresql.conf", "a") as fh:
        fh.write(f"\nport={port}\nlisten_addresses='127.0.0.1'\nshared_preload_libraries='pgcolumnar'\n")


def _ctl(bindir, data, action, log=None):
    cmd = [f"{bindir}/pg_ctl", "-D", str(data), "-w"]
    cmd += (["-l", str(log), "start"] if action == "start" else ["-m", "fast", "stop"])
    return _sh(cmd).returncode


def test_pg_upgrade_preserves_a_columnar_table(pgc_cluster, expect):
    from pgc_cluster import pick_port

    old_pgc = os.environ.get("PGC_UPGRADE_FROM_PG_CONFIG", "")
    new_pgc = pgc_cluster.pg_config
    if not old_pgc or not os.access(old_pgc, os.X_OK):
        expect.cannot_run("UNMET_PRECONDITION",
                          "PGC_UPGRADE_FROM_PG_CONFIG names no older major's pg_config, so there is nothing to "
                          "upgrade from; the matrix runs one major at a time",
                          name="pg_upgrade exited cleanly")
        return
    old_maj, new_maj = _major(old_pgc), _major(new_pgc)
    if new_maj <= old_maj:
        expect.cannot_run("UNMET_PRECONDITION", f"PG{old_maj} -> PG{new_maj} is not an upgrade",
                          name="pg_upgrade exited cleanly")
        return
    old_bin, new_bin = _cfg(old_pgc, "--bindir"), _cfg(new_pgc, "--bindir")

    work = pathlib.Path(tempfile.mkdtemp(prefix="pgc-upgrade-"))
    work.chmod(0o777)
    old_data, new_data = work / "old", work / "new"
    old_port, new_port = pick_port(17), pick_port(18)
    try:
        # The extension on the OLD major, built from a copy so the two majors' objects
        # never mix; conftest already installed it for the new one.
        build = work / "src"
        shutil.copytree(REPO, build, ignore=shutil.ignore_patterns(".git", "*.o", "*.so", "*.bc", "__pycache__"))
        _sh(["make", "-C", str(build), "clean", f"PG_CONFIG={old_pgc}"])
        built = _sh(["make", "-C", str(build), f"PG_CONFIG={old_pgc}"]).returncode == 0 and \
            _sh(["make", "-C", str(build), "install", f"PG_CONFIG={old_pgc}"]).returncode == 0
        assert built, "the extension did not build and install for the old major"

        _cluster(old_bin, old_data, old_port)
        _ctl(old_bin, old_data, "start", work / "old.log")
        _run(old_port, "CREATE EXTENSION pgcolumnar",
             "CREATE TABLE c (id int, v text, n numeric) USING pgcolumnar",
             # non-default options, so the options catalog row is carried, not only the pages
             "SELECT pgcolumnar.set_options('c', stripe_row_limit => 4000, chunk_group_row_limit => 800, "
             "compression => 'zstd')",
             "INSERT INTO c SELECT g, md5(g::text), (g::numeric) / 7 FROM generate_series(1, 50000) g",
             "DELETE FROM c WHERE id % 13 = 0",
             "CREATE INDEX c_id ON c (id)")
        content = "SELECT md5(string_agg(t::text, '' ORDER BY t::text)) FROM c t"
        opts = ("SELECT stripe_row_limit || '/' || chunk_group_row_limit || '/' || compression "
                "FROM pgcolumnar.options WHERE regclass = 'c'::regclass")
        old_hash, old_count, old_opts = _q(old_port, content), _q(old_port, "SELECT count(*) FROM c"), _q(old_port, opts)
        old_range = _q(old_port, "SELECT count(*) FROM c WHERE id BETWEEN 100 AND 200")
        expect.text("yes" if isinstance(old_count, int) and old_count > 0 else "no", "yes",
                    "old cluster has rows to preserve")
        expect.text(old_opts, "4000/800/zstd", "old cluster recorded non-default options")
        _ctl(old_bin, old_data, "stop")

        _cluster(new_bin, new_data, new_port)
        _ctl(new_bin, new_data, "start", work / "new-pre.log")
        # a pass must not come from the table having been made here
        expect.num(_q(new_port, "SELECT count(*) FROM pg_class WHERE relname = 'c'"), 0,
                   "the new cluster does NOT have the table before the upgrade")
        expect.num(int(_q(new_port, "SHOW server_version_num")) // 10000, new_maj, "and is genuinely the new major")
        _ctl(new_bin, new_data, "stop")

        up = _sh([f"{new_bin}/pg_upgrade", "-b", old_bin, "-B", new_bin, "-d", str(old_data), "-D", str(new_data),
                  "-p", str(old_port), "-P", str(new_port), "-U", "postgres"], cwd=work)
        expect.num(up.returncode, 0, "pg_upgrade exited cleanly")
        _ctl(new_bin, new_data, "start", work / "new.log")

        expect.num(_q(new_port, "SELECT 1"), 1, "the new cluster starts after the upgrade")
        expect.num(int(_q(new_port, "SHOW server_version_num") or 0) // 10000, new_maj, "we are on the new major")
        expect.num(_q(new_port, "SELECT count(*) FROM pg_extension WHERE extname = 'pgcolumnar'"), 1,
                   "the extension is present")
        expect.text(_q(new_port, "SELECT a.amname::text FROM pg_class c JOIN pg_am a ON a.oid = c.relam "
                                 "WHERE c.relname = 'c'"), "pgcolumnar", "the table is still a columnar table")
        expect.num(_q(new_port, "SELECT count(*) FROM c"), old_count, "row count preserved")
        expect.text(_q(new_port, content), old_hash, "content preserved byte for byte")
        expect.text(_q(new_port, opts), old_opts, "per-table options preserved")
        with psycopg.connect(f"host=127.0.0.1 port={new_port} user=postgres dbname=postgres", autocommit=True) as c:
            c.execute("SET enable_seqscan = off")
            c.execute("SET pgcolumnar.enable_custom_scan = off")
            plan = "\n".join(r[0] for r in c.execute(
                "EXPLAIN (COSTS off) SELECT count(*) FROM c WHERE id BETWEEN 100 AND 200"))
            uses = sum(1 for line in plan.splitlines()
                       if ("Index Scan" in line or "Index Only Scan" in line) and "c_id" in line)
            expect.num(uses, 1, "premise: the range lookup takes the upgraded index")
            # compared with the OLD cluster's answer, not with another query on the new one
            expect.num(c.execute("SELECT count(*) FROM c WHERE id BETWEEN 100 AND 200").fetchone()[0], old_range,
                       "the index still answers the same range")
        _run(new_port, "INSERT INTO c SELECT g, 'post', 1 FROM generate_series(900001, 900100) g")
        expect.num(_q(new_port, "SELECT count(*) FROM c WHERE v = 'post'"), 100,
                   "the upgraded table still accepts writes")
        expect.num(_q(new_port, "SELECT count(*) FROM c"), old_count + 100,
                   "and reads back the new rows with the old ones")
    finally:
        for b, d in ((old_bin, old_data), (new_bin, new_data)):
            if (d / "postmaster.pid").exists():
                _sh([f"{b}/pg_ctl", "-D", str(d), "-m", "immediate", "-w", "stop"])
        shutil.rmtree(work, ignore_errors=True)
