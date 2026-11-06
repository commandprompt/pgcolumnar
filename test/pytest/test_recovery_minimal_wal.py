"""At wal_level=minimal, a table created or truncated in the transaction that loads it
survives a crash (#1341). The pytest twin of `test/recovery_minimal_wal.sh`, every
check by the shell's name.

PostgreSQL skips WAL for such a table and makes it durable at COMMIT: below
wal_skip_threshold it writes the new pages to WAL, at or above it it fsyncs the files.
Each arm loads a columnar table and a heap mirror in one transaction, commits, reads
the committed row count (so a load that failed cannot pass as two empty tables that
agree), crashes the server, restarts it into crash recovery, and compares the two.
Both branches run: wal_skip_threshold 0 and 64MB.

A PRIVATE CLUSTER, at wal_level=minimal and stopped with `-m immediate` for each crash;
the shell kills the postmaster's process group instead, which leaves the same redo.
"""
import pathlib
import shutil
import tempfile

import psycopg

LOAD = "SELECT g, (g * 7919) % 10007, md5(g::text) FROM generate_series(1, 50000) g"


def _one(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            return c.execute(sql).fetchone()[0]
    except psycopg.Error:
        return None


def _digest(dsn, table):
    return _one(dsn, f"SELECT count(*) || '/' || md5(string_agg(id || ':' || v || ':' || w, ',' ORDER BY id)) "
                     f"FROM {table}") or ""


def _redo_count(log):
    try:
        return log.read_text(errors="replace").count("database system was not properly shut down")
    except OSError:
        return 0


def test_tables_skipping_wal_survive_a_crash(pgc_cluster, expect):
    from pgc_cluster import Cluster, pick_port

    work = pathlib.Path(tempfile.mkdtemp(prefix="pgc-minwal-"))
    work.chmod(0o777)
    datadir = work / "data"
    datadir.mkdir()
    cluster = Cluster(pgc_cluster.pg_config, "minwal", datadir, pick_port(13))
    log = datadir / "server.log"
    try:
        cluster.initdb()
        with open(datadir / "postgresql.conf", "a") as fh:
            fh.write("\nwal_level=minimal\nmax_wal_senders=0\n")
        cluster.start()
        dsn = cluster.dsn()
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        expect.text(_one(dsn, "SHOW wal_level"), "minimal", "premise: the cluster runs at wal_level=minimal")

        def crash_after(sql, table):
            r0 = _redo_count(log)
            try:
                with psycopg.connect(dsn, autocommit=True) as c:
                    c.execute(sql)
            except psycopg.Error:
                pass
            n = _one(dsn, f"SELECT count(*) FROM {table}")
            cluster.stop()
            try:
                cluster.start()
                back = "ok"
            except RuntimeError as exc:
                back = f"restart failed: {exc}"
            r1 = _redo_count(log)
            recovered = "recovered" if r1 > r0 else f"no recovery logged ({r0} before, {r1} after)"
            return f"{n if n is not None else 'no table'}|{back}|{recovered}"

        for thr in ("0", "64MB"):
            got = crash_after(f"BEGIN; SET LOCAL wal_skip_threshold = '{thr}'; "
                              f"CREATE TABLE mw_c_{thr} (id int, v int, w text) USING pgcolumnar; "
                              f"CREATE TABLE mw_h_{thr} (id int, v int, w text); "
                              f"INSERT INTO mw_c_{thr} {LOAD}; INSERT INTO mw_h_{thr} {LOAD}; COMMIT", f"mw_c_{thr}")
            expect.text(got, "50000|ok|recovered",
                        f"a table created and loaded in one transaction at wal_skip_threshold {thr} committed its rows, "
                        f"and the server came back through recovery")
            h = _digest(dsn, f"mw_h_{thr}")
            expect.text(f"{_digest(dsn, f'mw_c_{thr}')}|{h.split('/')[0]}", f"{h}|50000",
                        f"and the columnar table holds what its heap mirror holds, at wal_skip_threshold {thr}")
        for thr in ("0", "64MB"):
            got = crash_after(f"BEGIN; SET LOCAL wal_skip_threshold = '{thr}'; TRUNCATE mw_c_{thr}, mw_h_{thr}; "
                              f"INSERT INTO mw_c_{thr} SELECT id + 1, v, w FROM ({LOAD}) s(id, v, w); "
                              f"INSERT INTO mw_h_{thr} SELECT id + 1, v, w FROM ({LOAD}) s(id, v, w); COMMIT",
                              f"mw_c_{thr}")
            expect.text(got, "50000|ok|recovered",
                        f"a table truncated and reloaded in one transaction at wal_skip_threshold {thr} committed its "
                        f"rows, and the server came back through recovery")
            h = _digest(dsn, f"mw_h_{thr}")
            expect.text(f"{_digest(dsn, f'mw_c_{thr}')}|{h.split('/')[0]}", f"{h}|50000",
                        f"and after the truncate the columnar table holds what its heap mirror holds, at "
                        f"wal_skip_threshold {thr}")
    finally:
        cluster.stop()
        shutil.rmtree(work, ignore_errors=True)
