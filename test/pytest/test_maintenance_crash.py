"""A crash part-way through maintenance loses nothing (#1341). The pytest twin of
`test/maintenance_crash.sh`, every check by the shell's name.

compact_rewrite, recluster, compact and vacuum retire row groups and write new ones.
Two crash points, each followed by crash recovery:

  - the function has run but its transaction has not committed, and another session
    has checkpointed. Recovery must leave the table as it was before;
  - the function has committed, and nothing has checkpointed since, so its work exists
    only in WAL. Recovery must leave the table as the function left it.

A PRIVATE CLUSTER, because each crash takes the postmaster down. The shell kills the
postmaster's process group; this side stops it with `-m immediate`, which leaves the
same work for the next start: "database system was not properly shut down", and redo.
"""
import pathlib
import shutil
import tempfile

import psycopg

ROWS = ("SELECT count(*) || '/' || md5(string_agg(id || ':' || v || ':' || w, ',' ORDER BY id)) FROM mc")
GROUPS = ("SELECT count(*) || '/' || coalesce(sum(row_count), 0) FROM pgcolumnar.row_group "
          "WHERE storage_id = pgcolumnar.get_storage_id('mc')")


def _one(dsn, sql, setup=()):
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            for s in setup:
                c.execute(s)
            return c.execute(sql).fetchone()[0]
    except psycopg.Error as exc:
        return f"ERROR: {str(exc).splitlines()[0]}"


BY_INDEX = ("SET enable_seqscan = off", "SET enable_bitmapscan = off", "SET pgcolumnar.enable_custom_scan = off")


def _state(dsn):
    # the custom scan off too, and asserted by a premise: with only seqscan off this
    # whole-table range planned as Custom Scan (PgColumnarScan) and read no index
    by_index = _one(dsn, ROWS + " WHERE id BETWEEN 1 AND 10000", BY_INDEX)
    return f"{_one(dsn, ROWS)}|{_one(dsn, GROUPS)}|{by_index}"


def _fresh_table(dsn):
    with psycopg.connect(dsn, autocommit=True) as c:
        for s in ("DROP TABLE IF EXISTS mc", "SET pgcolumnar.stripe_row_limit = 1000",
                  "CREATE TABLE mc (id int, v int, w text) USING pgcolumnar",
                  "INSERT INTO mc SELECT g, (g * 7919) % 10007, md5(g::text) FROM generate_series(1, 10000) g",
                  "CREATE INDEX mc_id ON mc (id)",
                  "DELETE FROM mc WHERE id % 5 IN (1, 2) OR id BETWEEN 3001 AND 4000",
                  "CHECKPOINT"):
            c.execute(s)


def _redo_count(log):
    try:
        return log.read_text(errors="replace").count("database system was not properly shut down")
    except OSError:
        return 0


def test_a_crash_part_way_through_maintenance_loses_nothing(pgc_cluster, expect):
    from pgc_cluster import Cluster, pick_port

    work = pathlib.Path(tempfile.mkdtemp(prefix="pgc-mcrash-"))
    work.chmod(0o777)
    datadir = work / "data"
    datadir.mkdir()
    cluster = Cluster(pgc_cluster.pg_config, "mcrash", datadir, pick_port(11))
    log = datadir / "server.log"
    try:
        cluster.initdb()
        cluster.start()
        dsn = cluster.dsn()
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")

        _fresh_table(dsn)
        with psycopg.connect(dsn, autocommit=True) as c:
            for st in BY_INDEX:
                c.execute(st)
            plan = [r[0] for r in c.execute("EXPLAIN (COSTS OFF) " + ROWS + " WHERE id BETWEEN 1 AND 10000")]
        expect.text("yes" if any("Index Scan using" in line for line in plan) else "no", "yes",
                    "premise: the by-index reading is an Index Scan through mc_id")

        for f in ("compact_rewrite('mc', 0.0)", "recluster('mc', 'v')", "compact('mc')", "vacuum('mc')"):
            name = f.split("(")[0]
            _fresh_table(dsn)
            before = _state(dsn)
            redo0 = _redo_count(log)
            holder = psycopg.connect(dsn, autocommit=True)
            holder.execute("BEGIN")
            holder.execute(f"SELECT pgcolumnar.{f}")
            opened = "yes"
            # A function that changed nothing would be trivially crash-safe.
            inside = holder.execute(GROUPS).fetchone()[0]
            expect.text("changed" if inside != before.split("|")[1] else f"unchanged ({inside})", "changed",
                        f"{name} changed the row groups in its open transaction")
            _one(dsn, "CHECKPOINT")
            cluster.stop()
            try:
                holder.close()
            except psycopg.Error:
                pass
            try:
                cluster.start()
                came_back = "ok"
            except RuntimeError as exc:
                came_back = f"restart failed: {exc}"
            r1 = _redo_count(log)
            recovered = "recovered" if r1 > redo0 else f"no recovery logged ({redo0} before, {r1} after)"
            expect.text(f"{opened}|{came_back}|{recovered}", "yes|ok|recovered",
                        f"{name} ran in its open transaction, and the cluster came back after a crash")
            expect.text(_state(dsn), before,
                        f"after a crash with {name} uncommitted, the table is as it was, by scan and by index")

        for f in ("compact_rewrite('mc', 0.0)", "recluster('mc', 'v')", "compact('mc')", "vacuum('mc')"):
            name = f.split("(")[0]
            _fresh_table(dsn)
            before_rows = _one(dsn, ROWS)
            before_groups = _one(dsn, GROUPS)
            _one(dsn, f"SELECT pgcolumnar.{f}")
            after = _state(dsn)
            expect.text("changed" if after.split("|")[1] != before_groups else f"unchanged ({after.split('|')[1]})",
                        "changed", f"{name} changed the row groups before the crash")
            redo0 = _redo_count(log)
            cluster.stop()
            try:
                cluster.start()
                came_back = "ok"
            except RuntimeError as exc:
                came_back = f"restart failed: {exc}"
            r1 = _redo_count(log)
            recovered = "recovered" if r1 > redo0 else f"no recovery logged ({redo0} before, {r1} after)"
            expect.text(f"{came_back}|{recovered}", "ok|recovered",
                        f"{name} committed, and the cluster came back after a crash before any checkpoint")
            expect.text(f"{_state(dsn)}|{_one(dsn, ROWS)}", f"{after}|{before_rows}",
                        f"after a crash with {name} committed, the table is as {name} left it, with the same rows")
    finally:
        cluster.stop()
        shutil.rmtree(work, ignore_errors=True)
