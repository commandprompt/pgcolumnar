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

from pgc_horizon import freed_range_blockers, retry_past_horizon
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


def _retry(cluster, sql, want):
    """-> what `sql` (compact or truncate) returned in all, waiting for the horizon
    (#1987): with one call, a snapshot held by another session made it do nothing."""
    try:
        with psycopg.connect(cluster.dsn(), autocommit=True, connect_timeout=5) as c:
            total, calls = retry_past_horizon(c, sql, want)
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0])
    print(f"-- [{sql}] returned {total} in {calls} calls")
    return total


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
        # a row group of its own (one INSERT is one flush), then all of it deleted;
        # no autovacuum from here, which would retire the dead groups itself (#1987)
        _run(pri, "ALTER TABLE r SET (autovacuum_enabled = false)")
        _run(pri, "INSERT INTO r SELECT g, 'doomed' || g FROM generate_series(50001,51000) g",
             "DELETE FROM r WHERE v LIKE 'doomed%'")
        rg_before = _q(pri, RG_SQL)
        retired = _retry(pri, "SELECT pgcolumnar.compact('r')", 1)
        expect.text(f"{retired}|{rg_before - _q(pri, RG_SQL)}", "1|1",
                    "compact retired the fully deleted row group on the primary")
        assert _caught_up(pri, sb), "the standby did not catch up (sync 6)"
        expect.hash(_hash(sb, "r"), _hash(pri, "r"), "compact replays identically")

        # The SMGR truncate, the one direct XLogInsert. End truncation is opt-in, and
        # the trailing rows need row groups of their own that compact frees first.
        # One statement per call: set_options and compact must commit before the next.
        _run(pri, "ALTER DATABASE postgres SET pgcolumnar.enable_end_truncation = on")
        _run(pri, "CREATE TABLE t (id int, v text) USING pgcolumnar WITH (autovacuum_enabled = false)")
        _run(pri, "SELECT pgcolumnar.set_options('t', stripe_row_limit => 1000, chunk_group_row_limit => 1000)")
        _run(pri, "INSERT INTO t SELECT g, md5(g::text) FROM generate_series(1,30000) g")
        assert _caught_up(pri, sb), "the standby did not catch up (sync 7)"
        _run(pri, "DELETE FROM t WHERE id > 15000")
        expect.num(_retry(pri, "SELECT pgcolumnar.compact('t')", 15), 15,
                   "compact retired the 15 trailing row groups of t on the primary")
        pri_before = _q(pri, "SELECT pg_relation_size('t')")
        trunc = _retry(pri, "SELECT pgcolumnar.truncate('t')", 1)
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

        _standby_feedback_arms(pri, sb, sb_dir, expect)
    finally:
        for c in (rs, sb, pri):
            if c is not None:
                c.stop()
        shutil.rmtree(work, ignore_errors=True)


def _feedback_via(pri, sb, where, tries=60):
    """-> "yes" once the primary holds an xmin not newer than the standby snapshot's,
    in the slot pgc_standby and not in a walsender (`where` = "slot"), or the other
    way round ("walsender"). Waits up to 30 s; otherwise what it read."""
    hx = sx = wx = None
    for _ in range(tries):
        hx = _q(sb, "SELECT backend_xmin::text FROM pg_stat_activity WHERE application_name = 'rp_sb_reader'")
        sx = _q(pri, "SELECT coalesce(xmin::text, '-') FROM pg_replication_slots WHERE slot_name = 'pgc_standby'")
        wx = _q(pri, "SELECT coalesce(string_agg(coalesce(backend_xmin::text, '-'), ','), 'none') "
                     "FROM pg_stat_activity WHERE backend_type = 'walsender'")
        if isinstance(hx, str) and hx:
            if where == "slot" and wx == "-" and isinstance(sx, str) and sx != "-":
                if _q(pri, f"SELECT age('{sx}'::xid) >= age('{hx}'::xid)") is True:
                    return "yes"
            if where == "walsender" and sx is None and wx not in ("-", "none") and "," not in str(wx):
                if _q(pri, f"SELECT age('{wx}'::xid) >= age('{hx}'::xid)") is True:
                    return "yes"
        time.sleep(0.5)
    return f"no (slot xmin={'no slot' if sx is None else sx}, walsender xmin={wx}, standby snapshot xmin={hx or 'none'})"


def _hs_cycle(pri, sb, upto, where):
    """DELETE the rows of hs above `upto`, let the standby replay it, open a snapshot
    on the standby, compact, and read truncate's answer and the helper's count while
    it is open. Then end it and truncate again, waiting for the horizon.
    -> (feedback premise, truncate while held, helper count while held, blocks after)."""
    _run(pri, f"DELETE FROM hs WHERE id > {upto}")
    assert _caught_up(pri, sb), f"the standby did not catch up (hs delete above {upto})"
    hold = psycopg.connect(sb.dsn(), autocommit=True, application_name="rp_sb_reader")
    try:
        hold.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
        hold.execute("SELECT count(*) FROM hs")
        via = _feedback_via(pri, sb, where)
        retired = _retry(pri, "SELECT pgcolumnar.compact('hs')", 3)
        print(f"-- compact of hs retired {retired}")
        held = _q(pri, "SELECT pgcolumnar.truncate('hs')")
        with psycopg.connect(pri.dsn(), autocommit=True) as c:
            n = freed_range_blockers(c, "hs")
        print(f"-- horizon blockers of hs: {n}")
    finally:
        hold.close()
    after = _retry(pri, "SELECT pgcolumnar.truncate('hs')", 1)
    return via, held, n, after


def _counted(n):
    return "yes" if isinstance(n, int) and n >= 1 else f"no ({'none' if n is None else n})"


def _standby_feedback_arms(pri, sb, sb_dir, expect):
    """Scenario 4 (#1999). With hot_standby_feedback on, the standby sends the xmin of
    its oldest snapshot to the primary, which keeps it in the slot the standby streams
    through or, with no slot, in the walsender's own backend_xmin (a walsender has no
    database). Either one holds truncate back, and the horizon helper must count
    both, or a check that wants 0 blockers could pass while a standby holds truncate
    back. The slot arm runs first, because the walsender arm drops the slot."""
    _conf(sb_dir, "hot_standby_feedback = on", "wal_receiver_status_interval = 1")
    _q(sb, "SELECT pg_reload_conf()")
    _run(pri, "CREATE TABLE hs (id int) USING pgcolumnar WITH (autovacuum_enabled = false)")
    _run(pri, "SELECT pgcolumnar.set_options('hs', stripe_row_limit => 1000, chunk_group_row_limit => 1000)")
    _run(pri, "INSERT INTO hs SELECT g FROM generate_series(1, 12000) g")

    via, held, n, after = _hs_cycle(pri, sb, 9000, "slot")
    expect.text(via, "yes", "premise: the standby's feedback reaches the primary through slot pgc_standby")
    expect.num(held, 0, "premise: truncate is held while the standby snapshot is open, through the slot")
    expect.text(_counted(n), "yes", "the horizon helper counts a replication slot that holds a standby's xmin")
    expect.at_least(after, 1, "truncate reclaims once the standby snapshot held through the slot ends")

    # no slot from here, and the slot dropped so its last xmin does not hold the horizon
    with open(sb_dir / "postgresql.auto.conf", "a") as fh:
        fh.write("\nprimary_slot_name = ''\n")
    _q(sb, "SELECT pg_reload_conf()")
    for _ in range(60):
        if _q(pri, "SELECT count(*) FROM pg_replication_slots WHERE slot_name = 'pgc_standby' AND NOT active") == 1:
            break
        time.sleep(0.5)
    _q(pri, "SELECT pg_drop_replication_slot('pgc_standby')")
    via, held, n, after = _hs_cycle(pri, sb, 6000, "walsender")
    expect.text(via, "yes", "premise: the standby's feedback reaches the primary through its walsender, with no slot")
    expect.num(held, 0, "premise: truncate is held while the standby snapshot is open, through the walsender")
    expect.text(_counted(n), "yes", "the horizon helper counts a walsender that holds a standby's xmin")
    expect.at_least(after, 1, "truncate reclaims once the standby snapshot held through the walsender ends")
