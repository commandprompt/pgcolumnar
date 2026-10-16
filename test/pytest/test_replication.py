"""Physical replication and standby redo of columnar tables (#241, #1341). The pytest
twin of `test/replication.sh`, every check by the shell's name.

Every WAL record the extension emits is a core type: data pages go through
log_newpage and the one direct insert is an SMGR truncate. So a standby replays
columnar WAL with core redo and needs the module only to READ a columnar table. A
private primary gets a columnar table and a projection, pg_basebackup makes a
streaming standby of it, and a second backup is brought up on its own with no
upstream at all, so what it holds came from the backup bytes alone. Then inserts,
deletes, updates, vacuum, compact and an end truncation are replayed and compared
with the primary, and a standby running WITHOUT the module must still replay.

Each replay arm is about a write the primary made. Vacuum must have rewritten the
table into a new relfilenode, compact must have retired a row group (measured: after
vacuum it had nothing to retire), and the truncate must have shrunk the primary,
or the standby would be compared against a state nothing changed.

PRIVATE CLUSTERS, three of them: the standby and the restore need a primary they can
back up, with end truncation on for its database.
"""
import pathlib
import shutil
import subprocess
import tempfile
import time

import psycopg

from pgc_vacuity import query_error

PROJ_SQL = ("SELECT count(*) FROM pgcolumnar.projection "
            "WHERE storage_id = pgcolumnar.get_storage_id('r') AND projection_id > 0")
RG_SQL = "SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = pgcolumnar.get_storage_id('r')"
PROJ_HASH = "SELECT md5(string_agg(x, '' ORDER BY x)) FROM pgcolumnar.read_projection('r','{p}') x"


def _q(cluster, sql):
    """-> the first column of the first row, or a distinct failure sentinel."""
    try:
        with psycopg.connect(cluster.dsn(), autocommit=True, connect_timeout=5) as c:
            row = c.execute(sql).fetchone()
            return None if row is None else row[0]
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0])


def _run(cluster, *sqls):
    with psycopg.connect(cluster.dsn(), autocommit=True) as c:
        for sql in sqls:
            c.execute(sql)


def _hash(cluster, table):
    return _q(cluster, f"SELECT md5(string_agg(t::text, '' ORDER BY t::text)) FROM {table} t")


def _answers(cluster, tries=240):
    for _ in range(tries):
        if _q(cluster, "SELECT 1") == 1:
            return True
        time.sleep(0.5)
    return False


def _caught_up(primary, standby, tries=360):
    """Has the standby replayed at least the primary's current LSN? Polled, three
    minutes, like the shell: a slow standby is not a broken one."""
    target = _q(primary, "SELECT pg_current_wal_lsn()::text")
    for _ in range(tries):
        if _q(standby, f"SELECT pg_last_wal_replay_lsn() >= '{target}'::pg_lsn") is True:
            return True
        time.sleep(0.5)
    return False


def _basebackup(primary, datadir, *extra):
    return subprocess.run([str(pathlib.Path(primary.bindir) / "pg_basebackup"), "-h", "127.0.0.1",
                           "-p", str(primary.port), "-U", "postgres", "-D", str(datadir), *extra,
                           "-X", "stream", "-c", "fast"], capture_output=True, text=True)


def _conf(datadir, *lines):
    """Append settings; a later line wins, so this overrides what the backup copied."""
    with open(datadir / "postgresql.conf", "a") as fh:
        fh.write("\n" + "\n".join(lines) + "\n")


def test_a_streaming_standby_and_a_restore_replay_columnar_tables(pgc_cluster, expect):
    from pgc_cluster import Cluster, pick_port

    work = pathlib.Path(tempfile.mkdtemp(prefix="pgc-repl-"))
    work.chmod(0o777)
    (work / "primary").mkdir()
    pri = Cluster(pgc_cluster.pg_config, "repl", work / "primary", pick_port(20))
    sb_dir, rs_dir = work / "standby", work / "restore"
    sb = rs = None
    try:
        pri.initdb()
        pri.start()
        _run(pri, "CREATE EXTENSION pgcolumnar",
             "CREATE TABLE r (id int, v text) USING pgcolumnar",
             "INSERT INTO r SELECT g, 'row' || g FROM generate_series(1,20000) g",
             # declared BEFORE the backup, so the backup's bytes must contain it
             "SELECT pgcolumnar.add_projection('r', 'r_pre', ARRAY['id','v'], ARRAY['id'])")
        pri_proj = _q(pri, PROJ_SQL)
        expect.num(pri_proj, 1, "the primary has a projection to preserve, before the backup runs")
        pri_pre_rows = _q(pri, "SELECT count(*) FROM pgcolumnar.read_projection('r','r_pre')")
        expect.num(pri_pre_rows, 20000, "and it reads on the primary")

        # -C -S: a physical slot, so the primary keeps the WAL the standby still needs
        _basebackup(pri, sb_dir, "-R", "-C", "-S", "pgc_standby")
        expect.text("yes" if (sb_dir / "PG_VERSION").exists() else "no", "yes",
                    "pg_basebackup produced a data directory")
        sb = Cluster(pgc_cluster.pg_config, "repl-sb", sb_dir, pick_port(21))
        _conf(sb_dir, f"port={sb.port}", f"unix_socket_directories='{sb_dir}'")
        sb.start()
        _answers(sb)
        expect.num(_q(sb, "SELECT 1"), 1, "the standby accepts connections")
        expect.num(_q(pri, "SELECT count(*) FROM pg_replication_slots "
                           "WHERE slot_name = 'pgc_standby' AND slot_type = 'physical'"), 1,
                   "a physical slot exists to hold WAL for the standby")
        expect.text(str(_q(pri, "SELECT active FROM pg_replication_slots WHERE slot_name = 'pgc_standby'")),
                    "True", "and the standby is actually connected through it")

        # the controls: were this the primary, every comparison below would agree
        expect.text(str(_q(sb, "SELECT pg_is_in_recovery()")), "True", "the standby is in recovery")
        expect.text("yes" if _q(sb, "SHOW data_directory") != _q(pri, "SHOW data_directory") else "no", "yes",
                    "the standby is a different cluster from the primary")

        assert _caught_up(pri, sb), "the standby did not catch up (sync 1)"
        expect.hash(_hash(sb, "r"), _hash(pri, "r"), "the base backup carried the initial rows")
        expect.num(_q(sb, PROJ_SQL), pri_proj, "pg_basebackup carried the projection catalog row, unlike pg_dump (#266)")
        expect.num(_q(sb, "SELECT count(*) FROM pgcolumnar.read_projection('r','r_pre')"), pri_pre_rows,
                   "and the projection's storage came across intact and readable")
        expect.hash(_q(sb, PROJ_HASH.format(p="r_pre")), _q(pri, PROJ_HASH.format(p="r_pre")),
                    "with the same content the primary reads")

        # a projection made AFTER the backup reaches the standby by replay
        _run(pri, "SELECT pgcolumnar.add_projection('r', 'r_post', ARRAY['id','v'], ARRAY['id'])")
        expect.num(_q(pri, PROJ_SQL), 2, "the primary now has a second, post-backup projection")
        assert _caught_up(pri, sb), "the standby did not catch up (sync 2)"
        expect.num(_q(sb, PROJ_SQL), 2, "streaming replay carried a projection created after the backup")
        expect.num(_q(sb, "SELECT count(*) FROM pgcolumnar.read_projection('r','r_post')"), pri_pre_rows,
                   "and that one reads on the standby too")

        # The same claim with streaming taken out: no -R, so no standby.signal and no
        # upstream. What this cluster holds came from the backup bytes alone.
        _basebackup(pri, rs_dir)
        expect.text("yes" if (rs_dir / "PG_VERSION").exists() else "no", "yes",
                    "the second base backup produced a data directory")
        rs = Cluster(pgc_cluster.pg_config, "repl-rs", rs_dir, pick_port(22))
        _conf(rs_dir, f"port={rs.port}", f"unix_socket_directories='{rs_dir}'")
        rs.start()
        _answers(rs)
        expect.num(_q(rs, "SELECT 1"), 1, "the restored cluster accepts connections")
        expect.text(str(_q(rs, "SELECT pg_is_in_recovery()")), "False",
                    "the restored cluster is not in recovery (it is not following anyone)")
        expect.num(_q(rs, "SELECT count(*) FROM pg_stat_wal_receiver"), 0, "and has no upstream configured")
        rs_dd = _q(rs, "SHOW data_directory")
        expect.text("yes" if rs_dd not in (_q(pri, "SHOW data_directory"), _q(sb, "SHOW data_directory")) else "no",
                    "yes", "and is a different cluster from the primary and the standby")
        # two empty results would agree, so the primary side is pinned first
        rs_pri_count = _q(pri, PROJ_SQL)
        rs_pri_hash = _q(pri, PROJ_HASH.format(p="r_pre"))
        expect.num(rs_pri_count, 2, "the primary still has both projections when the backup is taken")
        expect.text("yes" if isinstance(rs_pri_hash, str) and rs_pri_hash else "no", "yes",
                    "and a non-empty projection to compare against")
        expect.num(_q(rs, PROJ_SQL), rs_pri_count, "a physical backup alone preserves the projections, unlike pg_dump (#266)")
        expect.hash(_q(rs, PROJ_HASH.format(p="r_pre")), rs_pri_hash, "and its storage, readable with the same content")
        expect.num(_q(rs, "SELECT count(*) FROM pgcolumnar.read_projection('r','r_post')"), pri_pre_rows,
                   "and the later projection is in this backup as well, so it is not stale")
        rs.stop()

        # Positive control: with replay paused, a change on the primary must NOT appear.
        _q(sb, "SELECT pg_wal_replay_pause()")
        _run(pri, "INSERT INTO r SELECT g, 'paused' || g FROM generate_series(90001,90100) g")
        time.sleep(1)
        expect.num(_q(sb, "SELECT count(*) FROM r WHERE v LIKE 'paused%'"), 0,
                   "with replay paused the standby does NOT see the new rows")
        _q(sb, "SELECT pg_wal_replay_resume()")
        assert _caught_up(pri, sb), "the standby did not catch up (sync 3)"
        expect.num(_q(sb, "SELECT count(*) FROM r WHERE v LIKE 'paused%'"), 100, "after resuming, the standby sees them")

        # scenario 1: the ordinary write paths
        _run(pri, "INSERT INTO r SELECT g, 'more' || g FROM generate_series(20001,40000) g",
             "DELETE FROM r WHERE id % 7 = 0",
             "UPDATE r SET v = v || '-u' WHERE id % 11 = 0")
        assert _caught_up(pri, sb), "the standby did not catch up (sync 4)"
        expect.hash(_hash(sb, "r"), _hash(pri, "r"), "insert, delete and update replay identically")
        expect.num(_q(sb, "SELECT count(*) FROM r"), _q(pri, "SELECT count(*) FROM r"), "and the row counts agree")

        # scenario 2: each replay arm is about a write the primary made
        fn_before = _q(pri, "SELECT pg_relation_filenode('r')")
        _run(pri, "SELECT pgcolumnar.vacuum('r')")
        fn_after = _q(pri, "SELECT pg_relation_filenode('r')")
        expect.text("yes" if isinstance(fn_before, int) and fn_after != fn_before else "no", "yes",
                    "vacuum rewrote r into a new relfilenode on the primary")
        assert _caught_up(pri, sb), "the standby did not catch up (sync 5)"
        expect.hash(_hash(sb, "r"), _hash(pri, "r"), "vacuum replays identically")
        # a row group of its own (one INSERT is one flush), then all of it deleted
        _run(pri, "INSERT INTO r SELECT g, 'doomed' || g FROM generate_series(50001,51000) g",
             "DELETE FROM r WHERE v LIKE 'doomed%'")
        rg_before = _q(pri, RG_SQL)
        retired = _q(pri, "SELECT pgcolumnar.compact('r')")
        expect.text(f"{retired}|{rg_before - _q(pri, RG_SQL)}", "1|1",
                    "compact retired the fully deleted row group on the primary")
        assert _caught_up(pri, sb), "the standby did not catch up (sync 6)"
        expect.hash(_hash(sb, "r"), _hash(pri, "r"), "compact replays identically")

        # The SMGR truncate, the one direct XLogInsert. End truncation is opt-in, and
        # the trailing rows need row groups of their own that compact frees first.
        # One statement per call: set_options and compact must commit before the next.
        _run(pri, "ALTER DATABASE postgres SET pgcolumnar.enable_end_truncation = on")
        _run(pri, "CREATE TABLE t (id int, v text) USING pgcolumnar")
        _run(pri, "SELECT pgcolumnar.set_options('t', stripe_row_limit => 1000, chunk_group_row_limit => 1000)")
        _run(pri, "INSERT INTO t SELECT g, md5(g::text) FROM generate_series(1,30000) g")
        assert _caught_up(pri, sb), "the standby did not catch up (sync 7)"
        _run(pri, "DELETE FROM t WHERE id > 15000")
        _run(pri, "SELECT pgcolumnar.compact('t')")
        pri_before = _q(pri, "SELECT pg_relation_size('t')")
        trunc = _q(pri, "SELECT pgcolumnar.truncate('t')")
        expect.text("yes" if isinstance(trunc, int) and trunc > 0 else f"no (returned {trunc})", "yes",
                    "the primary actually truncated (record was emitted)")
        pri_after = _q(pri, "SELECT pg_relation_size('t')")
        assert _caught_up(pri, sb), "the standby did not catch up (sync 8)"
        sb_after = _q(sb, "SELECT pg_relation_size('t')")
        expect.text("yes" if isinstance(sb_after, int) and sb_after == pri_after
                    else f"no (primary {pri_before}->{pri_after}, standby {sb_after})", "yes",
                    "the standby's main fork shrank to match the primary")
        expect.num(_q(sb, "SELECT count(*) FROM t"), _q(pri, "SELECT count(*) FROM t"),
                   "the truncate did not cost rows on the standby")
        expect.hash(_hash(sb, "t"), _hash(pri, "t"), "content still matches after the truncate record")
        expect.text(str(_q(sb, "SELECT pg_is_in_recovery()")), "True",
                    "the standby is still in recovery after the truncate record")

        # Scenario 3: a standby WITHOUT the module still replays, because every record
        # is a core type. If this fails, the extension has grown WAL semantics.
        _run(pri, "CREATE TABLE r2 (id int, v text) USING pgcolumnar",
             "INSERT INTO r2 SELECT g, 'x' || g FROM generate_series(1,5000) g")
        sb.stop()
        _conf(sb_dir, "shared_preload_libraries=''")
        sb.start()
        _answers(sb)
        expect.num(_q(sb, "SELECT 1"), 1, "a standby without pgcolumnar loaded still starts")
        expect.num(_q(sb, "SELECT count(*) FROM pg_extension WHERE extname='pgcolumnar' "
                          "AND '' = current_setting('shared_preload_libraries')"), 1,
                   "and it is not running the module")
        _run(pri, "INSERT INTO r2 SELECT g, 'y' || g FROM generate_series(5001,9000) g")
        expect.num(_q(pri, "SELECT count(*) FROM r2"), 9000,
                   "premise: the primary wrote r2's second batch while the standby ran without the module")
        target = _q(pri, "SELECT pg_current_wal_lsn()::text")
        _caught_up(pri, sb, tries=120)
        expect.text(str(_q(sb, f"SELECT pg_last_wal_replay_lsn() >= '{target}'::pg_lsn")), "True",
                    "it replayed columnar WAL past the primary's LSN without the module")
        expect.text(str(_q(sb, "SELECT pg_is_in_recovery()")), "True",
                    "and did not PANIC (still in recovery, still answering)")

        # the bytes are there; reload the module and the same standby reads them
        sb.stop()
        _conf(sb_dir, "shared_preload_libraries='pgcolumnar'")
        sb.start()
        _answers(sb)
        assert _caught_up(pri, sb), "the standby did not catch up (sync 9)"
        expect.hash(_hash(sb, "r2"), _hash(pri, "r2"), "with the module back, the replayed table reads correctly")
    finally:
        for c in (rs, sb, pri):
            if c is not None:
                c.stop()
        shutil.rmtree(work, ignore_errors=True)
