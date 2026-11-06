"""A prepared transaction, seen from other sessions and across a crash (#1341). The
pytest twin of `test/prepared_across_sessions.sh`, every check by the shell's name.

prepared_writes covers PREPARE and COMMIT PREPARED in one session. This covers what
another session sees while a transaction is prepared; the unique-key and row locks a
prepared transaction still holds (a second writer waits, then fails once COMMIT
PREPARED runs); ROLLBACK PREPARED of a DELETE and an UPDATE; and a prepared insert
surviving a crash and committing after recovery.

A PRIVATE CLUSTER, because the last arm crashes the postmaster (`-m immediate`).
"""
import pathlib
import shutil
import tempfile
import threading
import time

import psycopg

ROWS = "SELECT count(*) || '/' || md5(string_agg(k || ':' || v, ',' ORDER BY k)) FROM px"


def _one(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            row = c.execute(sql).fetchone()
            return row[0] if row else None
    except psycopg.Error as exc:
        return f"ERROR: {exc.sqlstate}"


def _run(dsn, *stmts):
    with psycopg.connect(dsn, autocommit=True) as c:
        for s in stmts:
            c.execute(s)


def _try(dsn, *stmts):
    """_run, but a failure is left for the premise after it to report, as the shell does."""
    try:
        _run(dsn, *stmts)
    except psycopg.Error:
        pass


def _contend(dsn, name, sql, release):
    """Run `sql` as session `name`; once it waits on a lock, run `release`.
    -> '<waited>|<sqlstate or ok>'."""
    result = {}

    def go():
        try:
            with psycopg.connect(dsn, autocommit=True) as c:
                c.execute(f"SET application_name = '{name}'")
                c.execute("SET statement_timeout = '60s'")
                c.execute(sql)
            result["out"] = "ok"
        except psycopg.Error as exc:
            result["out"] = exc.sqlstate or "?"

    t = threading.Thread(target=go)
    t.start()
    waited = "no"
    for _ in range(300):
        if _one(dsn, f"SELECT count(*) FROM pg_stat_activity WHERE application_name = '{name}' "
                     "AND wait_event_type = 'Lock'") == 1:
            waited = "yes"
            break
        time.sleep(0.1)
    _run(dsn, release)
    t.join(60)
    return f"{waited}|{result.get('out', 'still running')}"


def test_a_prepared_transaction_across_sessions_and_a_crash(pgc_cluster, expect):
    from pgc_cluster import Cluster, pick_port

    work = pathlib.Path(tempfile.mkdtemp(prefix="pgc-prepx-"))
    work.chmod(0o777)
    datadir = work / "data"
    datadir.mkdir()
    cluster = Cluster(pgc_cluster.pg_config, "prepx", datadir, pick_port(14))
    try:
        cluster.initdb()
        with open(datadir / "postgresql.conf", "a") as fh:
            fh.write("\nmax_prepared_transactions=4\n")
        cluster.start()
        dsn = cluster.dsn()
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        expect.text(_one(dsn, "SHOW max_prepared_transactions"), "4", "premise: prepared transactions are enabled")
        _run(dsn, "CREATE TABLE px (k int PRIMARY KEY, v text) USING pgcolumnar",
             "INSERT INTO px SELECT g, 'r' || g FROM generate_series(1, 1000) g")

        _run(dsn, "BEGIN", "INSERT INTO px SELECT g, 'p' || g FROM generate_series(1001, 1100) g",
             "PREPARE TRANSACTION 'px_ins'")
        expect.num(_one(dsn, "SELECT count(*) FROM px WHERE k > 1000"), 0,
                   "a prepared insert is invisible to another session")
        got = _contend(dsn, "px_dup", "INSERT INTO px VALUES (1050, 'dup')", "COMMIT PREPARED 'px_ins'")
        row = _one(dsn, "SELECT count(*) || '/' || min(v) FROM px WHERE k = 1050")
        expect.text(f"{got}|{row}",
                    "yes|23505|1/p1050",
                    "a second insert of a key the prepared transaction holds waits, then fails with 23505 after COMMIT PREPARED")
        expect.num(_one(dsn, "SELECT count(*) FROM px WHERE k > 1000"), 100,
                   "and after COMMIT PREPARED the other session sees all the prepared rows")

        before = _one(dsn, ROWS)
        # A PREPARE that failed would leave the table unchanged and the checks after
        # it comparing it with itself, so its failure is reported by the premise.
        _try(dsn, "BEGIN", "DELETE FROM px WHERE k % 20 = 0", "PREPARE TRANSACTION 'px_del'")
        expect.num(_one(dsn, "SELECT count(*) FROM pg_prepared_xacts WHERE gid = 'px_del'"), 1,
                   "premise: the DELETE is prepared")
        expect.text(_one(dsn, ROWS), before, "while a DELETE is prepared, another session still sees every row")
        _run(dsn, "ROLLBACK PREPARED 'px_del'")
        expect.text(_one(dsn, ROWS), before, "ROLLBACK PREPARED of a DELETE leaves every row as it was")
        _try(dsn, "BEGIN", "UPDATE px SET v = v || '!' WHERE k % 20 = 1", "PREPARE TRANSACTION 'px_upd'")
        expect.num(_one(dsn, "SELECT count(*) FROM pg_prepared_xacts WHERE gid = 'px_upd'"), 1,
                   "premise: the UPDATE is prepared")
        _run(dsn, "ROLLBACK PREPARED 'px_upd'")
        expect.text(_one(dsn, ROWS), before, "ROLLBACK PREPARED of an UPDATE leaves every row as it was, none duplicated")

        _run(dsn, "BEGIN", "UPDATE px SET v = 'prepared' WHERE k = 7", "PREPARE TRANSACTION 'px_row'")
        got = _contend(dsn, "px_rowc", "UPDATE px SET v = 'other' WHERE k = 7", "COMMIT PREPARED 'px_row'")
        row = _one(dsn, "SELECT count(*) || '/' || min(v) FROM px WHERE k = 7")
        expect.text(f"{got}|{row}",
                    "yes|40001|1/prepared",
                    "an UPDATE of a row a prepared transaction updated waits, then fails with 40001 after COMMIT PREPARED")

        _run(dsn, "CREATE TABLE px_heap (k int PRIMARY KEY, v text)",
             "INSERT INTO px_heap SELECT g, md5(g::text) FROM generate_series(5001, 10000) g",
             "BEGIN", "INSERT INTO px SELECT g, md5(g::text) FROM generate_series(5001, 10000) g",
             "PREPARE TRANSACTION 'px_crash'")
        cluster.stop()
        try:
            cluster.start()
            back = "ok"
        except RuntimeError as exc:
            back = f"restart failed: {exc}"
        held = _one(dsn, "SELECT count(*) FROM pg_prepared_xacts WHERE gid = 'px_crash'")
        expect.text(f"{back}|{held}",
                    "ok|1",
                    "a prepared insert survives a crash: the cluster came back and still holds the prepared transaction")
        _run(dsn, "COMMIT PREPARED 'px_crash'")
        expect.text(_one(dsn, "SELECT count(*) || '/' || md5(string_agg(k || ':' || v, ',' ORDER BY k)) FROM px WHERE k > 5000"),
                    _one(dsn, "SELECT count(*) || '/' || md5(string_agg(k || ':' || v, ',' ORDER BY k)) FROM px_heap"),
                    "and after COMMIT PREPARED its rows are heap's")
    finally:
        try:
            for gid in ("px_ins", "px_del", "px_upd", "px_row", "px_crash"):
                _one(cluster.dsn(), f"ROLLBACK PREPARED '{gid}'")
        finally:
            cluster.stop()
            shutil.rmtree(work, ignore_errors=True)
