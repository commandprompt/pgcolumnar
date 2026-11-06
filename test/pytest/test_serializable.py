"""SERIALIZABLE refuses a write skew on a columnar table, as on heap (#1341). The
pytest twin of `test/serializable.sh`, every check by the shell's name.

Two transactions each read what the other is about to change, then each writes
something the other's read depended on. At SERIALIZABLE one must fail with 40001.
Four shapes, each on a columnar table and on heap: two doctors taken off call (in one
row group, then in two), and two sessions putting someone on call in an empty ward
(read by a scan of the whole table, then through an index). The first transaction
commits; SSI refuses the second with 40001 ("read/write dependencies"); the
invariant holds. Which statement raises differs: heap's second COMMIT, columnar's
second write, which waits for the first commit. The checks hold the guarantee, not
the step; each error is tagged with its step, and a failure prints them. At
REPEATABLE READ the same interleavings must commit both and break the invariant,
the control that makes a 40001 from anything but SSI fail the checks.

WHERE THE PROTECTION COMES FROM: heap SSI on columnar's catalog tables, which a
serializable read of a columnar table reads and a write later changes. An arm pins
the SIRead locks there, so a change that stopped reading the catalog that way would
turn it red rather than drop the protection silently.

The two sessions are two connections. The second write runs in a thread, because
on columnar it can wait for the first session to commit (see `_skew`).
"""
import psycopg

AMS = ("pgcolumnar", "heap")


def _error(conn, sql, tag):
    """'' when sql runs, else '<tag>:<sqlstate>:<ssi or message>'."""
    try:
        conn.execute(sql)
        return ""
    except psycopg.Error as exc:
        msg = str(exc).splitlines()[0]
        return f"{tag}:{exc.sqlstate or '?'}:{'ssi' if 'read/write dependencies' in msg else msg}"


def _skew(dsn, schema, read, write1, write2, level="SERIALIZABLE"):
    """-> '<s1>|<s2>' for the interleaving in the module docstring, at `level`.

    The second write runs in a thread. On heap it returns at once; on columnar a
    write to a row group the first session is changing waits for that session to
    commit, as limitations.md says, so the first COMMIT is sent once the second write
    has returned or is seen waiting on a lock. Driving the two in strict turn would
    wait on itself.
    """
    import threading
    import time

    s1 = psycopg.connect(dsn, autocommit=True)
    s2 = psycopg.connect(dsn, autocommit=True)
    try:
        errs = {1: [], 2: []}
        for c in (s1, s2):
            c.execute(f'SET search_path TO "{schema}", public')
        pid2 = s2.execute("SELECT pg_backend_pid()").fetchone()[0]
        for n, c, sql, tag in ((1, s1, f"BEGIN ISOLATION LEVEL {level}", "r1"), (2, s2, f"BEGIN ISOLATION LEVEL {level}", "r2"),
                               (1, s1, read, "r1"), (2, s2, read, "r2"), (1, s1, write1, "w1")):
            st = _error(c, sql, tag)
            if st:
                errs[n].append(st)
        t = threading.Thread(target=lambda: (lambda st: st and errs[2].append(st))(_error(s2, write2, "w2")))
        t.start()
        with psycopg.connect(dsn, autocommit=True) as watch:
            for _ in range(300):
                if not t.is_alive() or watch.execute(
                        "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = %s", (pid2,)).fetchone()[0]:
                    break
                time.sleep(0.1)
        st = _error(s1, "COMMIT", "c1")
        if st:
            errs[1].append(st)
        t.join(60)
        st = _error(s2, "COMMIT", "c2")
        if st:
            errs[2].append(st)
        e2 = " ".join(errs[2])
        # s2 refused once by SSI, at its write or its COMMIT, reads as 40001/ssi
        if len(errs[2]) == 1 and errs[2][0] in ("w2:40001:ssi", "c2:40001:ssi"):
            e2 = "40001/ssi"
        return f"{' '.join(errs[1])}|{e2}"
    finally:
        s1.close()
        s2.close()


def _one(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


def test_serializable_refuses_a_write_skew(pgc_cluster, pgc_conn, expect):
    schema = _one(pgc_conn, "SELECT current_schema()")
    with pgc_conn.cursor() as cur:
        for am in AMS:
            cur.execute(f"CREATE TABLE doc1_{am} (id int, on_call bool) USING {am}")
            cur.execute(f"INSERT INTO doc1_{am} VALUES (1, true), (2, true)")
            cur.execute(f"CREATE TABLE doc2_{am} (id int, on_call bool) USING {am}")
            cur.execute(f"INSERT INTO doc2_{am} VALUES (1, true)")
            cur.execute(f"INSERT INTO doc2_{am} VALUES (2, true)")
            cur.execute(f"CREATE TABLE ward_{am} (id int, ward int, on_call bool) USING {am}")
            cur.execute(f"INSERT INTO ward_{am} SELECT g, g % 50, false FROM generate_series(1, 5000) g")
            cur.execute(f"CREATE TABLE wardi_{am} (id int, ward int, on_call bool) USING {am}")
            cur.execute(f"INSERT INTO wardi_{am} SELECT g, g % 50, false FROM generate_series(1, 5000) g")
            cur.execute(f"CREATE INDEX wardi_{am}_w ON wardi_{am} (ward)")
            cur.execute(f"ANALYZE wardi_{am}")
    groups = "/".join(str(_one(pgc_conn, f"SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = "
                                         f"pgcolumnar.get_storage_id('{t}_pgcolumnar')")) for t in ("doc1", "doc2"))
    expect.text(groups, "1/2", "premise: the second doctors table holds its two rows in two row groups, the first in one")

    reader = psycopg.connect(pgc_cluster.dsn(), autocommit=True)
    try:
        reader.execute(f'SET search_path TO "{schema}", public')
        reader.execute("BEGIN ISOLATION LEVEL SERIALIZABLE")
        reader.execute("SELECT count(*) FROM doc1_pgcolumnar WHERE on_call")
        rg, dv = reader.execute(
            "SELECT count(*) FILTER (WHERE c.relname IN ('row_group', 'row_group_pkey')), "
            "count(*) FILTER (WHERE c.relname IN ('delete_vector', 'delete_vector_pkey')) "
            "FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
            "WHERE l.mode = 'SIReadLock' AND l.pid = pg_backend_pid()").fetchone()
        reader.execute("COMMIT")
    finally:
        reader.close()
    expect.text("held" if rg >= 1 and dv >= 1 else f"{rg}/{dv}", "held",
                "a serializable read of a columnar table holds SIRead locks on columnar's row_group and delete_vector catalog")

    def both(stem, read, w1, w2, invariant, level="SERIALIZABLE"):
        out = []
        for am in AMS:
            t = f"{stem}_{am}"
            got = _skew(pgc_cluster.dsn(), schema, read.replace("%t", t), w1.replace("%t", t), w2.replace("%t", t),
                        level)
            out.append(f"{got}|{_one(pgc_conn, invariant.replace('%t', t))}")
        return "  ".join(out)

    off = ("SELECT count(*) FROM %t WHERE on_call", "UPDATE %t SET on_call = false WHERE id = 1",
           "UPDATE %t SET on_call = false WHERE id = 2", "SELECT count(*) FROM %t WHERE on_call")
    expect.text(both("doc1", *off), "|40001/ssi|1  |40001/ssi|1",
                "two doctors in one row group each taken off call: SSI refuses one of the two with 40001, and one stays on call, as on heap")
    expect.text(both("doc2", *off), "|40001/ssi|1  |40001/ssi|1", "the same with the two doctors in two row groups")
    on = ("INSERT INTO %t VALUES (101, 7, true)", "INSERT INTO %t VALUES (102, 7, true)",
          "SELECT count(*) FROM %t WHERE ward = 7 AND on_call")
    expect.text(both("ward", "SELECT count(*) FROM %t WHERE ward = 7 AND on_call", *on), "|40001/ssi|1  |40001/ssi|1",
                "two sessions that each see an empty ward by a scan of the whole table and each put someone on call: "
                "SSI refuses one of the two with 40001, as on heap")
    plans = []
    for am in AMS:
        with pgc_conn.cursor() as cur:
            for s in ("SET enable_seqscan = off", "SET enable_bitmapscan = off", "SET pgcolumnar.enable_custom_scan = off"):
                cur.execute(s)
            cur.execute(f"EXPLAIN (COSTS OFF) SELECT count(*) FROM wardi_{am} WHERE ward = 7 AND on_call")
            plan = "\n".join(r[0] for r in cur.fetchall())
            for s in ("enable_seqscan", "enable_bitmapscan", "pgcolumnar.enable_custom_scan"):
                cur.execute(f"RESET {s}")
        plans.append(str(int(f"Scan using wardi_{am}_w" in plan and "Index" in plan)))
    expect.text("|".join(plans), "1|1", "premise: the ward lookup is an index scan on both tables")
    idx = ("SET LOCAL enable_seqscan = off; SET LOCAL enable_bitmapscan = off; "
           "SET LOCAL pgcolumnar.enable_custom_scan = off; SELECT count(*) FROM %t WHERE ward = 7 AND on_call")
    expect.text(both("wardi", idx, *on), "|40001/ssi|1  |40001/ssi|1", "the same when both read the ward through an index")

    # ---- the control: REPEATABLE READ lets both commit ----
    for am in AMS:
        for sql in (f"UPDATE doc1_{am} SET on_call = true", f"UPDATE doc2_{am} SET on_call = true",
                    f"DELETE FROM ward_{am} WHERE on_call", f"DELETE FROM wardi_{am} WHERE on_call"):
            pgc_conn.execute(sql)
    rr = "REPEATABLE READ"
    expect.text(f"{both('doc1', *off, rr)} / {both('doc2', *off, rr)}", "||0  ||0 / ||0  ||0",
                "at REPEATABLE READ both doctors' transactions commit and no one stays on call, in one row group and "
                "in two, as on heap")
    expect.text(f"{both('ward', 'SELECT count(*) FROM %t WHERE ward = 7 AND on_call', *on, rr)} / "
                f"{both('wardi', idx, *on, rr)}", "||2  ||2 / ||2  ||2",
                "at REPEATABLE READ both ward transactions commit and two are on call, by a scan and by an index, "
                "as on heap")
