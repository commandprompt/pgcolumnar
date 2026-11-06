"""A pgcolumnar write prepared and then finished in the same session (#1323).

PREPARE frees TopTransactionContext without reaching the COMMIT/ABORT arms of the
transaction callback, and the pending write states and delete-vector buffers live
in children of it. Their statics were reset only by those arms, so the session's
next transaction -- the COMMIT PREPARED itself -- walked a freed list at its
PRE_COMMIT and the backend died of SIGSEGV. COMMIT PREPARED from another session
never touched them, which is the path parallel_copy takes.

Every arm runs in ONE connection, a transaction per statement, and asserts what
that connection returns; a crash also shows as a "terminated by signal" line the
server log gained during this test. The cluster is shared, so only the lines
written after this test began are counted.

ON A PRIVATE DATABASE (`pgc_own_db`). Independent of `test/prepared_writes.sh`: its
own table, row counts and values; the check names match.
"""

import pathlib
import time

import psycopg


def _session(dsn, *sqls):
    """Run the statements in one fresh connection, autocommit; -> the last row's
    first column as text, or 'lost' when the connection dies."""
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            row = None
            for sql in sqls:
                cur = c.execute(sql)
                row = cur.fetchone() if cur.description else row
            return "" if row is None else str(row[0])
    except psycopg.OperationalError:
        return "lost"


def _ready(dsn):
    """Wait, up to 30 s, until the server answers: an arm before may have crashed it."""
    for _ in range(60):
        if _session(dsn, "SELECT 1") == "1":
            return
        time.sleep(0.5)


def test_a_write_prepared_and_finished_in_one_session(pgc_own_db, pgc_cluster, expect):
    conn = pgc_own_db
    dsn = pgc_cluster.dsn(dbname=conn.execute("SELECT current_database()").fetchone()[0])
    log = pathlib.Path(pgc_cluster.datadir) / "server.log"
    expect.text("present" if log.is_file() else f"absent ({log})", "present",
                "premise: the server log is where a crash would be recorded")
    seen = log.stat().st_size
    expect.at_least(int(conn.execute("SHOW max_prepared_transactions").fetchone()[0]), 1,
                    "premise: prepared transactions are enabled")

    conn.execute("CREATE TABLE pq (k int, tag text) USING pgcolumnar")
    conn.execute("INSERT INTO pq SELECT g, 'seed' FROM generate_series(1, 240) g")

    expect.text(_session(dsn, "BEGIN", "INSERT INTO pq SELECT g, 'tpc' FROM generate_series(241, 2640) g",
                         "PREPARE TRANSACTION 'pq_ins'", "COMMIT PREPARED 'pq_ins'",
                         "SELECT count(*) FROM pq"), "2640",
                "an insert prepared and committed in one session leaves the session answering")
    expect.text(_session(dsn, "BEGIN", "DELETE FROM pq WHERE k <= 40",
                         "PREPARE TRANSACTION 'pq_del'", "COMMIT PREPARED 'pq_del'",
                         "SELECT count(*) FROM pq"), "2600",
                "a delete prepared and committed in one session leaves the session answering")
    expect.text(_session(dsn, "BEGIN", "UPDATE pq SET tag = 'moved' WHERE k BETWEEN 41 AND 300",
                         "PREPARE TRANSACTION 'pq_upd'", "COMMIT PREPARED 'pq_upd'",
                         "SELECT count(*) FILTER (WHERE tag = 'moved') FROM pq"), "260",
                "an update prepared and committed in one session leaves the session answering")
    expect.text(_session(dsn, "BEGIN", "INSERT INTO pq SELECT g, 'dropped' FROM generate_series(1, 1800) g",
                         "PREPARE TRANSACTION 'pq_rb'", "ROLLBACK PREPARED 'pq_rb'",
                         "SELECT count(*) FROM pq WHERE tag = 'dropped'"), "0",
                "an insert prepared and rolled back in one session leaves the session answering")
    expect.text(_session(dsn, "BEGIN", "INSERT INTO pq SELECT g, 'first' FROM generate_series(1, 12) g",
                         "PREPARE TRANSACTION 'pq_next'", "COMMIT PREPARED 'pq_next'",
                         "INSERT INTO pq SELECT g, 'second' FROM generate_series(1, 9) g",
                         "SELECT count(*) FROM pq WHERE tag = 'second'"), "9",
                "and the session's next write, after COMMIT PREPARED, lands")

    # The index-fetch cache: an index fetch on each side of COMMIT PREPARED, same rows.
    _ready(dsn)
    _session(dsn, "CREATE INDEX pq_k ON pq (k)")
    with psycopg.connect(dsn, autocommit=True) as c:
        for s in ("SET enable_seqscan = off", "SET enable_bitmapscan = off",
                  "SET pgcolumnar.enable_custom_scan = off"):
            c.execute(s)
        # The arm's own query (#1339): a premise about `k = 77` vouched for a different
        # statement than the `k IN (...)` the arm runs.
        plan = "\n".join(r[0] for r in c.execute(
            "EXPLAIN SELECT string_agg(tag, ',') FROM pq WHERE k IN (77, 78, 79)").fetchall())
    expect.num(plan.count("Index Scan using pq_k"), 1,
               "premise: the fetch is an index scan, which is what reaches the fetch cache")
    expect.text(_session(dsn, "SET enable_seqscan = off", "SET enable_bitmapscan = off",
                         "SET pgcolumnar.enable_custom_scan = off", "BEGIN",
                         "SELECT string_agg(tag, ',') FROM pq WHERE k IN (77, 78, 79)",
                         "PREPARE TRANSACTION 'pq_fetch'", "COMMIT PREPARED 'pq_fetch'",
                         "SELECT string_agg(tag, ',') FROM pq WHERE k IN (77, 78, 79)"),
                "moved,moved,moved",
                "an index fetch before PREPARE, and the same row fetched first after COMMIT PREPARED, in one session")

    # Wait for the server to be back if anything above crashed it, then read the log.
    # Its own connections: after a crash the fixture's connection is gone too.
    _ready(dsn)
    with log.open(errors="replace") as f:
        f.seek(seen)
        gained = f.read()
    expect.num(sum(1 for line in gained.splitlines() if "was terminated by signal" in line), 0,
               "no backend was terminated by a signal")
    expect.text(_session(dsn, "SELECT count(*) FROM pg_prepared_xacts WHERE gid LIKE 'pq_%'"), "0",
                "and no prepared transaction is left behind")
