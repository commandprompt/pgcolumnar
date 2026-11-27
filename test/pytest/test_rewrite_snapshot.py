"""A rewrite inside a REPEATABLE READ or SERIALIZABLE transaction must not change what
that transaction sees (#2167).

The verbs read the newest committed rows and write them into groups the calling
transaction owns, so before #2167 a REPEATABLE READ transaction that ran one saw rows
inserted after its snapshot and lost rows deleted after it. Heap's CLUSTER keeps each
row's xmin and xmax. Now, at those levels, the verb raises 40001 when its read and the
transaction's snapshot differ over the groups it rewrites, and runs when they agree.

The maintenance daemon forces READ COMMITTED, as core's autovacuum does, so a
default_transaction_isolation of repeatable read does not make it refuse on a busy table.

Independent of test/rewrite_snapshot.sh: same public seam, own fixtures. Check names
match the shell twin so the two compare by name. Every other arm is sequential: A takes its
snapshot, B commits, A runs the verb. Both sessions carry a statement_timeout and a
lock_timeout, so a hang fails the arm.
"""

import re
import time

import psycopg

OPTS = "-c statement_timeout=60s -c lock_timeout=30s"


def _msg(tbl):
    return f'could not rewrite table "{tbl}": rows changed after this transaction\'s snapshot'


def _counts(conn, tbl):
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) || '|' || coalesce(sum(id), 0) FROM {tbl}")
        return cur.fetchone()[0]


def _change(name, tbl):
    return {
        "none": ["SELECT 1"],
        "ins": [f"INSERT INTO {tbl} VALUES (70, 30)"],
        "del": [f"DELETE FROM {tbl} WHERE id = 3"],
        "both": [f"INSERT INTO {tbl} VALUES (70, 30)", f"DELETE FROM {tbl} WHERE id = 3"],
        "replace": [f"DELETE FROM {tbl} WHERE id BETWEEN 101 AND 105",
                    f"SELECT pgcolumnar.compact_rewrite('{tbl}', 0.0)"],
    }[name]


def _race(cluster, conn, tbl, am, iso, change, verbs, pre=(), second=False):
    """-> "ok|<A's count|sum after the verbs>|<count|sum after commit>", or
    "<SQLSTATE>|<message>|<count|sum after the rollback>".

    ids 1-50 in one group (51-60 deleted), plus 101-110 in a second when `second`.
    `pre` runs in A's transaction after its snapshot, before the verbs.
    """
    schema = conn.execute("SELECT current_schema()").fetchone()[0]
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {tbl}")
        cur.execute(f"CREATE TABLE {tbl} (id int PRIMARY KEY, v int) USING {am}")
        cur.execute(f"CREATE INDEX {tbl}_v ON {tbl} (v)")
        cur.execute(f"INSERT INTO {tbl} SELECT g, 100 - g FROM generate_series(1, 60) g")
        cur.execute(f"DELETE FROM {tbl} WHERE id > 50")
        if second:
            cur.execute(f"INSERT INTO {tbl} SELECT g, g FROM generate_series(101, 110) g")
    a = psycopg.connect(cluster.dsn(), autocommit=True, options=OPTS)
    try:
        a.execute(f'SET search_path TO "{schema}"')
        a.execute(f"BEGIN ISOLATION LEVEL {iso}")
        a.execute(f"SELECT count(*) FROM {tbl}")            # takes the snapshot
        with psycopg.connect(cluster.dsn(), autocommit=True, options=OPTS) as b:
            b.execute(f'SET search_path TO "{schema}"')
            for sql in _change(change, tbl):
                b.execute(sql)
        try:
            for sql in pre:
                a.execute(sql)
            for verb in verbs:
                a.execute(verb)
            after = _counts(a, tbl)
            a.execute("COMMIT")
            got = f"ok|{after}"
        except psycopg.Error as e:
            got = f"{e.sqlstate}|{e.diag.message_primary}"
            a.execute("ROLLBACK")
    finally:
        a.close()
    return f"{got}|{_counts(conn, tbl)}"


def _own(tbl):
    return [f"INSERT INTO {tbl} VALUES (80, 20)", f"DELETE FROM {tbl} WHERE id = 4"]


def test_heap_cluster_keeps_the_snapshot(pgc_cluster, pgc_conn, expect):
    """CLUSTER copies each row with its xmin and xmax: row 70, inserted after the
    snapshot, stays invisible, and row 3, deleted after it, stays visible."""
    expect.text(_race(pgc_cluster, pgc_conn, "rs_heap", "heap", "REPEATABLE READ", "both",
                      ["CLUSTER rs_heap USING rs_heap_v"]),
                "ok|50|1275|50|1342",
                "heap control: CLUSTER at REPEATABLE READ keeps a row inserted and a row deleted after the snapshot as the snapshot saw them")


def test_recluster_refuses_an_insert_after_the_snapshot(pgc_cluster, pgc_conn, expect):
    """Before #2167 A counted 51|1345: row 70 became visible."""
    expect.text(_race(pgc_cluster, pgc_conn, "rs_rc_ins", "pgcolumnar", "REPEATABLE READ", "ins",
                      ["SELECT pgcolumnar.recluster('rs_rc_ins', 'v')"]),
                f"40001|{_msg('rs_rc_ins')}|51|1345",
                "recluster at REPEATABLE READ raises 40001 when a row was inserted after the snapshot")


def test_recluster_refuses_a_delete_after_the_snapshot(pgc_cluster, pgc_conn, expect):
    """Before #2167 A counted 49|1272: row 3 vanished."""
    expect.text(_race(pgc_cluster, pgc_conn, "rs_rc_del", "pgcolumnar", "REPEATABLE READ", "del",
                      ["SELECT pgcolumnar.recluster('rs_rc_del', 'v')"]),
                f"40001|{_msg('rs_rc_del')}|49|1272",
                "recluster at REPEATABLE READ raises 40001 when a row was deleted after the snapshot")


def test_recluster_refuses_at_serializable(pgc_cluster, pgc_conn, expect):
    """SERIALIZABLE keeps its first snapshot too."""
    expect.text(_race(pgc_cluster, pgc_conn, "rs_rc_ser", "pgcolumnar", "SERIALIZABLE", "ins",
                      ["SELECT pgcolumnar.recluster('rs_rc_ser', 'v')"]),
                f"40001|{_msg('rs_rc_ser')}|51|1345",
                "recluster at SERIALIZABLE raises 40001 when a row was inserted after the snapshot")


def test_compact_rewrite_refuses_a_delete_in_its_group(pgc_cluster, pgc_conn, expect):
    """Before #2167 A counted 49|1272."""
    expect.text(_race(pgc_cluster, pgc_conn, "rs_cr_del", "pgcolumnar", "REPEATABLE READ", "del",
                      ["SELECT pgcolumnar.compact_rewrite('rs_cr_del', 0.0)"]),
                f"40001|{_msg('rs_cr_del')}|49|1272",
                "compact_rewrite at REPEATABLE READ raises 40001 when a row in a group it rewrites was deleted after the snapshot")


def test_cluster_refuses_an_insert_after_the_snapshot(pgc_cluster, pgc_conn, expect):
    """Before #2167 A counted 51|1345."""
    expect.text(_race(pgc_cluster, pgc_conn, "rs_cl_ins", "pgcolumnar", "REPEATABLE READ", "ins",
                      ["SELECT pgcolumnar.cluster('rs_cl_ins', 'v')"]),
                f"40001|{_msg('rs_cl_ins')}|51|1345",
                "cluster at REPEATABLE READ raises 40001 when a row was inserted after the snapshot")


def test_cluster_refuses_a_delete_after_the_snapshot(pgc_cluster, pgc_conn, expect):
    """Before #2167 A counted 49|1272."""
    expect.text(_race(pgc_cluster, pgc_conn, "rs_cl_del", "pgcolumnar", "REPEATABLE READ", "del",
                      ["SELECT pgcolumnar.cluster('rs_cl_del', 'v')"]),
                f"40001|{_msg('rs_cl_del')}|49|1272",
                "cluster at REPEATABLE READ raises 40001 when a row was deleted after the snapshot")


def test_cluster_refuses_groups_another_compact_rewrite_replaced(pgc_cluster, pgc_conn, expect):
    """B deletes five rows of the second group, then its compact_rewrite replaces both
    groups. The two snapshots list as many groups, with no deleted rows in the new ones;
    only the numbers differ. Before #2167 A counted 55 of the 60 rows it saw."""
    expect.text(_race(pgc_cluster, pgc_conn, "rs_cl_rep", "pgcolumnar", "REPEATABLE READ", "replace",
                      ["SELECT pgcolumnar.cluster('rs_cl_rep', 'v')"], second=True),
                f"40001|{_msg('rs_cl_rep')}|55|1815",
                "cluster at REPEATABLE READ raises 40001 when another session's compact_rewrite replaced its groups after the snapshot")


def test_recluster_hilbert_refuses_an_insert_after_the_snapshot(pgc_cluster, pgc_conn, expect):
    """The Hilbert verb reaches the online rewrite with another curve (#2249 review, B2).
    Before #2167 A counted 51|1345."""
    expect.text(_race(pgc_cluster, pgc_conn, "rs_rh_ins", "pgcolumnar", "REPEATABLE READ", "ins",
                      ["SELECT pgcolumnar.recluster_hilbert('rs_rh_ins', 'v')"]),
                f"40001|{_msg('rs_rh_ins')}|51|1345",
                "recluster_hilbert at REPEATABLE READ raises 40001 when a row was inserted after the snapshot")


def test_cluster_hilbert_refuses_an_insert_after_the_snapshot(pgc_cluster, pgc_conn, expect):
    """The Hilbert verb reaches the whole-table rewrite with another curve (#2249 review,
    B2). Before #2167 A counted 51|1345."""
    expect.text(_race(pgc_cluster, pgc_conn, "rs_ch_ins", "pgcolumnar", "REPEATABLE READ", "ins",
                      ["SELECT pgcolumnar.cluster_hilbert('rs_ch_ins', 'v')"]),
                f"40001|{_msg('rs_ch_ins')}|51|1345",
                "cluster_hilbert at REPEATABLE READ raises 40001 when a row was inserted after the snapshot")


def test_vacuum_refuses_a_change_after_the_snapshot(pgc_cluster, pgc_conn, expect):
    """Before #2167 A counted 50|1342."""
    expect.text(_race(pgc_cluster, pgc_conn, "rs_va", "pgcolumnar", "REPEATABLE READ", "both",
                      ["SELECT pgcolumnar.vacuum('rs_va')"]),
                f"40001|{_msg('rs_va')}|50|1342",
                "vacuum at REPEATABLE READ raises 40001 when rows were inserted and deleted after the snapshot")


def test_vacuum_sorted_refuses_a_change_after_the_snapshot(pgc_cluster, pgc_conn, expect):
    """Before #2167 A counted 50|1342."""
    expect.text(_race(pgc_cluster, pgc_conn, "rs_vs", "pgcolumnar", "REPEATABLE READ", "both",
                      ["SELECT pgcolumnar.vacuum_sorted('rs_vs', 'v')"]),
                f"40001|{_msg('rs_vs')}|50|1342",
                "vacuum_sorted at REPEATABLE READ raises 40001 when rows were inserted and deleted after the snapshot")


def test_every_verb_runs_with_nothing_changed(pgc_cluster, pgc_conn, expect):
    """Each verb in its own statement: two rewrites in one statement is another matter."""
    expect.text(_race(pgc_cluster, pgc_conn, "rs_none", "pgcolumnar", "REPEATABLE READ", "none",
                      ["SELECT pgcolumnar.compact_rewrite('rs_none', 0.0)",
                       "SELECT pgcolumnar.recluster('rs_none', 'v')",
                       "SELECT pgcolumnar.cluster('rs_none', 'v')",
                       "SELECT pgcolumnar.vacuum('rs_none')",
                       "SELECT pgcolumnar.vacuum_sorted('rs_none', 'v')"]),
                "ok|50|1275|50|1275",
                "at REPEATABLE READ, with nothing changed after the snapshot, all five verbs run and the transaction sees the rows it saw")


def test_compact_rewrite_runs_past_an_insert_elsewhere(pgc_cluster, pgc_conn, expect):
    """compact_rewrite compares only the groups it rewrites; A still does not see row 70."""
    expect.text(_race(pgc_cluster, pgc_conn, "rs_cr_ins", "pgcolumnar", "REPEATABLE READ", "ins",
                      ["SELECT pgcolumnar.compact_rewrite('rs_cr_ins', 0.0)"]),
                "ok|50|1275|51|1345",
                "at REPEATABLE READ, compact_rewrite runs when the only change after the snapshot is a row in a group it does not rewrite")


def test_recluster_runs_after_own_writes(pgc_cluster, pgc_conn, expect):
    """A's own insert and delete are seen by both snapshots alike."""
    expect.text(_race(pgc_cluster, pgc_conn, "rs_own_recluster", "pgcolumnar", "REPEATABLE READ", "none",
                      ["SELECT pgcolumnar.recluster('rs_own_recluster', 'v')"],
                      pre=_own("rs_own_recluster")),
                "ok|50|1351|50|1351",
                "at REPEATABLE READ, recluster runs after the transaction's own insert and delete, and it sees both")


def test_cluster_runs_after_own_writes(pgc_cluster, pgc_conn, expect):
    """A's own insert and delete are seen by both snapshots alike."""
    expect.text(_race(pgc_cluster, pgc_conn, "rs_own_cluster", "pgcolumnar", "REPEATABLE READ", "none",
                      ["SELECT pgcolumnar.cluster('rs_own_cluster', 'v')"],
                      pre=_own("rs_own_cluster")),
                "ok|50|1351|50|1351",
                "at REPEATABLE READ, cluster runs after the transaction's own insert and delete, and it sees both")


def test_read_committed_sees_what_its_statement_sees(pgc_cluster, pgc_conn, expect):
    """Each statement takes a new snapshot, so the verb's statement already sees the
    other session's commit."""
    expect.text(_race(pgc_cluster, pgc_conn, "rs_rcom", "pgcolumnar", "READ COMMITTED", "both",
                      ["SELECT pgcolumnar.recluster('rs_rcom', 'v')"]),
                "ok|50|1342|50|1342",
                "at READ COMMITTED, recluster in a transaction runs, and the transaction sees rows committed before it")


DAEMON = (("default_transaction_isolation", "'repeatable read'"), ("pgcolumnar.autovacuum_naptime", "1"),
          ("pgcolumnar.autovacuum_compact_threshold", "0.1"), ("pgcolumnar.maintenance_hold_ms", "3000"),
          ("pgcolumnar.autovacuum", "on"))


def test_the_daemon_runs_at_read_committed(pgc_cluster, pgc_conn, expect):
    """The daemon takes its snapshot before the verb's locks. At a default of REPEATABLE
    READ that snapshot would be the transaction's, and a delete committed while the verb
    waited would make it refuse. The daemon forces READ COMMITTED, as core's autovacuum
    does. Its compact_rewrite holds its lock for 3 s before it lists and locks its
    groups; the delete commits inside that hold. Settings by ALTER SYSTEM and a reload,
    reset at the end, since this corpus shares one cluster per worker."""
    schema = pgc_conn.execute("SELECT current_schema()").fetchone()[0]
    log = pgc_cluster.datadir / "server.log"
    start = len(log.read_text(errors="replace"))
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE rs_av (id int, v int) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('rs_av', chunk_group_row_limit => 1000)")
        cur.execute("INSERT INTO rs_av SELECT g, g FROM generate_series(1, 2000) g")
        cur.execute("DELETE FROM rs_av WHERE id % 5 = 0 AND id <= 1000")
    admin = psycopg.connect(pgc_cluster.dsn(), autocommit=True)
    try:
        for k, v in DAEMON:
            admin.execute(f"ALTER SYSTEM SET {k} = {v}")
        admin.execute("SELECT pg_reload_conf()")
        time.sleep(1)
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as fresh:
            settings = fresh.execute("SELECT current_setting('default_transaction_isolation') || '/' || "
                                     "current_setting('pgcolumnar.autovacuum')").fetchone()[0]
        held = "no"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            n = pgc_conn.execute(
                "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                "WHERE l.relation = 'rs_av'::regclass AND l.mode = 'ShareUpdateExclusiveLock' "
                "AND l.granted AND a.backend_type = 'pgcolumnar autovacuum worker'").fetchone()[0]
            if n == 1:
                held = "yes"
                break
            time.sleep(0.5)
        pgc_conn.execute("DELETE FROM rs_av WHERE id = 3")
        outcome = "no log line for rs_av"
        mine = re.compile(rf"pgcolumnar autovacuum: (compact_rewrite|skipped) {re.escape(schema)}\.rs_av(: (.*))?$")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            hits = [m for m in map(mine.search, log.read_text(errors="replace")[start:].splitlines()) if m]
            if hits:
                outcome = "compact_rewrite" if hits[0].group(1) == "compact_rewrite" else f"skipped: {hits[0].group(3)}"
                break
            time.sleep(0.5)
        count = pgc_conn.execute("SELECT count(*) FROM rs_av").fetchone()[0]
    finally:
        for k, _ in DAEMON:
            admin.execute(f"ALTER SYSTEM RESET {k}")
        admin.execute("SELECT pg_reload_conf()")
        admin.close()
    expect.text(settings, "repeatable read/on",
                "premise: a new session's default_transaction_isolation is repeatable read, with the daemon on")
    expect.text(f"{held}|{outcome}|{count}", "yes|compact_rewrite|1799",
                "under default_transaction_isolation = repeatable read, the daemon's compact_rewrite rewrites a group a delete changed while it held its lock")
