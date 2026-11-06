"""Committed columnar writes survive a crash, and uncommitted ones leave nothing
(#1341). The pytest twin of `test/recovery.sh`, every check by the shell's name.

Columnar stripes are written through the buffer manager with a full-page WAL record
each, and the metadata lives in WAL-logged heap catalogs. So rows committed after
the last checkpoint must come back from WAL, a transaction open at the crash must
leave nothing visible, and a visibility-map bit cleared after the checkpoint must not
come back set from the page on disk. A heap mirror, crash-safe itself, is the oracle.

A PRIVATE CLUSTER, because each crash takes the postmaster down. The shell kills the
postmaster's process group; this side stops it with `-m immediate`, which leaves the
same work for the next start. Each crash is proven by the server log gaining a
"database system was not properly shut down" line, counted before that crash, so one
crash that recovered cannot vouch for a later one that did not happen.
"""
import pathlib
import shutil
import tempfile

import psycopg
import pytest

from pgc_vacuity import query_error


@pytest.fixture(scope="module")
def rec(pgc_cluster):
    """-> the private cluster, started, with the extension created."""
    from pgc_cluster import Cluster, pick_port

    work = pathlib.Path(tempfile.mkdtemp(prefix="pgc-recov-"))
    work.chmod(0o777)
    datadir = work / "data"
    datadir.mkdir()
    cluster = Cluster(pgc_cluster.pg_config, "recov", datadir, pick_port(13))
    try:
        cluster.initdb()
        cluster.start()
        with psycopg.connect(cluster.dsn(), autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        yield cluster
    finally:
        cluster.stop()
        shutil.rmtree(work, ignore_errors=True)


def _run(cluster, *sqls):
    with psycopg.connect(cluster.dsn(), autocommit=True) as c:
        for sql in sqls:
            c.execute(sql)


def _one(cluster, sql):
    """-> the first column of the first row, or a distinct failure sentinel."""
    try:
        with psycopg.connect(cluster.dsn(), autocommit=True) as c:
            return c.execute(sql).fetchone()[0]
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0])


def _rows(cluster, sql):
    """-> every row, or a distinct failure sentinel."""
    try:
        with psycopg.connect(cluster.dsn(), autocommit=True) as c:
            return c.execute(sql).fetchall()
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0])


def _redo_count(cluster):
    try:
        return (cluster.datadir / "server.log").read_text(errors="replace").count(
            "database system was not properly shut down")
    except OSError:
        return 0


def _crash(cluster):
    """Stop the cluster with -m immediate. The caller counts redo before calling."""
    cluster.stop()


def _crash_and_recover(cluster):
    """-> 'ok' when the restart recovered from THIS crash."""
    before = _redo_count(cluster)
    _crash(cluster)
    try:
        cluster.start()
    except RuntimeError as exc:
        return f"restart failed: {str(exc).splitlines()[0]}"
    after = _redo_count(cluster)
    return "ok" if after > before else f"no recovery since the crash ({before} before, {after} after)"


def _pair(cluster, sql):
    """sql over t_col and t_heap -> (columnar rows, heap rows), as diff_query reads."""
    return _rows(cluster, sql.replace("%T", "t_col")), _rows(cluster, sql.replace("%T", "t_heap"))


def test_the_pair_through_three_crashes(rec, expect):
    """Scenarios 1 to 3 share the pair, as in the shell: committed rows survive, an
    open transaction leaves nothing, and committed deletes survive."""
    _run(rec, "DROP TABLE IF EXISTS t_heap", "DROP TABLE IF EXISTS t_col",
         "CREATE TABLE t_heap (id int, v text, n numeric) USING heap",
         "CREATE TABLE t_col (id int, v text, n numeric) USING pgcolumnar",
         "SELECT pgcolumnar.set_options('t_col', chunk_group_row_limit => 1000, stripe_row_limit => 2000)",
         "INSERT INTO t_heap SELECT g, 'a' || g, g * 1.5 FROM generate_series(1, 3000) g",
         "INSERT INTO t_col SELECT * FROM t_heap",
         "CHECKPOINT",
         # committed after the checkpoint, so only WAL holds it
         "INSERT INTO t_heap SELECT g, 'b' || g, g * 2.5 FROM generate_series(3001, 5000) g",
         "INSERT INTO t_col SELECT g, 'b' || g, g * 2.5 FROM generate_series(3001, 5000) g")

    expect.text(_crash_and_recover(rec), "ok", "s1 recovery ran")
    expect.num(_one(rec, "SELECT count(*) FROM t_col"), 5000, "s1 col count")
    expect.num(_one(rec, "SELECT count(*) FROM t_heap"), 5000, "s1 heap count")
    col, heap = _pair(rec, "SELECT * FROM %T")
    expect.row_set(col, heap, "s1 whole-row")
    col, heap = _pair(rec, "SELECT count(*), sum(n), min(v), max(v) FROM %T")
    expect.row_set(col, heap, "s1 aggregate")
    col, heap = _pair(rec, "SELECT id FROM %T WHERE id BETWEEN 2500 AND 3500")
    expect.row_set(col, heap, "s1 range")

    # An open transaction's INSERT, its pages checkpointed to disk, then the crash.
    holder = psycopg.connect(rec.dsn(), autocommit=True, application_name="rec_s2")
    try:
        holder.execute("BEGIN")
        holder.execute("INSERT INTO t_col SELECT g, 'c' || g, g FROM generate_series(9001, 11000) g")
    except psycopg.Error:
        pass
    # An INSERT that failed leaves no xid, and nothing for recovery to roll back.
    expect.num(_one(rec, "SELECT count(*) FROM pg_stat_activity WHERE application_name = 'rec_s2' "
                         "AND backend_xid IS NOT NULL"), 1,
               "premise: s2's INSERT ran in its open transaction before the crash")
    _run(rec, "CHECKPOINT")
    s2 = _crash_and_recover(rec)
    try:
        holder.close()
    except psycopg.Error:
        pass
    expect.text(s2, "ok", "s2 recovery ran")
    expect.num(_one(rec, "SELECT count(*) FROM t_col"), 5000, "s2 col count unchanged")
    expect.num(_one(rec, "SELECT count(*) FROM t_heap"), 5000, "s2 heap count unchanged")
    expect.num(_one(rec, "SELECT count(*) FROM t_col WHERE id >= 9001"), 0, "s2 no phantom rows")
    col, heap = _pair(rec, "SELECT * FROM %T")
    expect.row_set(col, heap, "s2 whole-row")

    _run(rec, "DELETE FROM t_heap WHERE id % 7 = 0", "DELETE FROM t_col WHERE id % 7 = 0")
    after_delete = _one(rec, "SELECT count(*) FROM t_heap")
    expect.text(_crash_and_recover(rec), "ok", "s3 recovery ran")
    expect.num(_one(rec, "SELECT count(*) FROM t_col"), after_delete, "s3 col count == heap")
    expect.num(_one(rec, "SELECT count(*) FROM t_col WHERE id % 7 = 0"), 0, "s3 deletes persisted")
    col, heap = _pair(rec, "SELECT * FROM %T")
    expect.row_set(col, heap, "s3 whole-row")
    col, heap = _pair(rec, "SELECT count(*), sum(n) FROM %T")
    expect.row_set(col, heap, "s3 aggregate")


def test_a_cleared_visibility_bit_stays_cleared(rec, expect):
    """A bit set by VACUUM and checkpointed to disk, then cleared by a DELETE that is
    not checkpointed: recovery must replay the clear over the set bit on disk, or an
    index-only scan would skip the fetch of a deleted row. Block 20 is deep in row
    group 0 and block 120 deep in group 3, both far from a group boundary."""
    _run(rec, "DROP TABLE IF EXISTS rv", "DROP TABLE IF EXISTS rv_h",
         "CREATE TABLE rv (id int, v int) USING pgcolumnar",
         "INSERT INTO rv SELECT g, g * 2 FROM generate_series(1, 50000) g",
         "CREATE INDEX rv_id ON rv (id)",
         "VACUUM rv")
    expect.text(_one(rec, "SELECT pgcolumnar.vm_is_visible('rv', 20)::text"), "true",
                "s4 pre-crash group-0 block all-visible")
    expect.text(_one(rec, "SELECT pgcolumnar.vm_is_visible('rv', 120)::text"), "true",
                "s4 pre-crash group-3 block all-visible")
    _run(rec, "CHECKPOINT", "DELETE FROM rv WHERE id BETWEEN 30001 AND 40000")
    expect.text(_one(rec, "SELECT pgcolumnar.vm_is_visible('rv', 120)::text"), "false",
                "s4 pre-crash deleted block cleared")

    expect.text(_crash_and_recover(rec), "ok", "s4 recovery ran")
    expect.text(_one(rec, "SELECT pgcolumnar.vm_is_visible('rv', 120)::text"), "false",
                "s4 post-crash deleted block stays not-all-visible")
    expect.text(_one(rec, "SELECT pgcolumnar.vm_is_visible('rv', 20)::text"), "true",
                "s4 post-crash clean block still all-visible")
    _run(rec, "CREATE TABLE rv_h (id int, v int) USING heap",
         "INSERT INTO rv_h SELECT g, g * 2 FROM generate_series(1, 50000) g WHERE g NOT BETWEEN 30001 AND 40000")
    expect.num(_one(rec, "SELECT count(*) FROM rv"), _one(rec, "SELECT count(*) FROM rv_h"),
               "s4 col count == heap oracle")
    expect.row_set(_rows(rec, "SELECT id, v FROM rv"), _rows(rec, "SELECT id, v FROM rv_h"),
                   "s4 contents match oracle")


def test_a_projection_survives_a_crash(rec, expect):
    """A projection's stripes go through the same WAL-logged path as the base, so a
    projection copy committed after the checkpoint must replay."""
    _run(rec, "DROP TABLE IF EXISTS rp", "DROP TABLE IF EXISTS rp_h",
         "CREATE TABLE rp (a int, c int) USING pgcolumnar",
         "SELECT pgcolumnar.add_projection('rp', 'rpp', ARRAY['a', 'c'], ARRAY['c'])",
         "INSERT INTO rp SELECT g, (g * 7) % 1000 FROM generate_series(1, 4000) g",
         "CHECKPOINT",
         "INSERT INTO rp SELECT g, (g * 7) % 1000 FROM generate_series(4001, 8000) g")

    expect.text(_crash_and_recover(rec), "ok", "s5 recovery ran")
    _run(rec, "CREATE TABLE rp_h (a int, c int) USING heap",
         "INSERT INTO rp_h SELECT g, (g * 7) % 1000 FROM generate_series(1, 8000) g")
    expect.num(_one(rec, "SELECT count(*) FROM rp"), _one(rec, "SELECT count(*) FROM rp_h"),
               "s5 base count == heap oracle")
    expect.row_set(_rows(rec, "SELECT pgcolumnar.read_projection('rp', 'rpp')"),
                   _rows(rec, "SELECT a::text || '|' || c::text FROM rp_h"),
                   "s5 projection survives crash")
    expect.row_set(_rows(rec, "SELECT a, c FROM rp WHERE c BETWEEN 100 AND 200"),
                   _rows(rec, "SELECT a, c FROM rp_h WHERE c BETWEEN 100 AND 200"),
                   "s5 projection scan matches oracle after recovery")
