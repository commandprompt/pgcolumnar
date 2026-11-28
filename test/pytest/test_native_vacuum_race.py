"""Compaction must not drop rows committed while it waited for its lock.

`pgcolumnar_compact_relation` used the caller's pre-lock statement snapshot, so a row
group committed after that snapshot was invisible to the rewrite and was destroyed by
the relfilenode swap. The fix takes a fresh snapshot after the lock (#295). Covered
for `vacuum()`, `vacuum_sorted()` and `cluster()` -- the Z-order twin goes through the
same path.

Independent of test/native_vacuum_race.sh: same public seam (the maintenance
functions), own fixture, own observations. Assertion names match the shell suite so
the two can be compared by name, not by importing each other.

THE WINDOW IS THE LOCK WAIT, AT READ COMMITTED. Session A inserts 100 rows and holds
its transaction open. Session B runs one statement that counts the table and then
calls the maintenance op; the count runs at once under the statement snapshot, and
the op waits for its AccessExclusiveLock. Once B is seen waiting, A commits. The
premise asserts both halves: B's statement counted 50, and B was waiting.

This used to pin B's snapshot at REPEATABLE READ before A's commit and run the op
inside that transaction. That is #2167's case: the op made A's rows visible to B's
old snapshot. Since #2167 the op refuses there with 40001, which
test_rewrite_snapshot.py checks, so this twin moved to the window #295 names.
"""
import threading
import time

import psycopg

BEFORE = 50
AFTER = 150
OPTS = "-c statement_timeout=60s -c lock_timeout=30s"


def _race(pgc_cluster, conn, table, call):
    """-> ("<what B's statement counted>|<B was seen waiting>", rows that survive)."""
    with conn.cursor() as cur:
        cur.execute(f"CREATE TABLE {table} (id int) USING pgcolumnar")
        cur.execute(f"INSERT INTO {table} SELECT g FROM generate_series(1,{BEFORE}) g")
        cur.execute("SELECT current_schema()")
        schema = cur.fetchone()[0]

    a = psycopg.connect(pgc_cluster.dsn(), autocommit=False, options=OPTS)
    b = psycopg.connect(pgc_cluster.dsn(), autocommit=True, options=OPTS)
    out = {}
    try:
        a.execute(f'SET search_path TO "{schema}", public')
        b.execute(f'SET search_path TO "{schema}", public')
        a.execute(f"INSERT INTO {table} SELECT g FROM generate_series({BEFORE + 1},{AFTER}) g")
        bpid = b.execute("SELECT pg_backend_pid()").fetchone()[0]

        def run_b():
            try:
                out["count"] = b.execute(f"SELECT (SELECT count(*) FROM {table}), {call}").fetchone()[0]
            except psycopg.Error as e:
                out["count"] = e.sqlstate

        t = threading.Thread(target=run_b)
        t.start()
        waiting = "no"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            w = conn.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (bpid,)).fetchone()
            if w and w[0] == "Lock":
                waiting = "yes"
                break
            time.sleep(0.1)
        a.commit()
        t.join(60)
    finally:
        a.close()
        b.close()
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        return f"{out.get('count')}|{waiting}", cur.fetchone()[0]


def test_native_vacuum_race(pgc_cluster, pgc_conn, expect):
    # THE TABLE IS INLINE AND THE NAMES ARE LITERAL, deliberately. Built with an
    # f-string these would reach `compare_to_bash` as one template, `{} keeps rows
    # committed ...`, which cannot match the three literal names the bash suite
    # states -- so all three would be reported MISSING. An inline loop table is the
    # idiom the grader reads per element (#1045 class 2).
    for table, call, label, name, premise in (
        ("r_vac", "pgcolumnar.vacuum('r_vac')", "vacuum()",
         "vacuum() keeps rows committed during its snapshot window (#295)",
         "premise: vacuum() -- A's rows are outside B's snapshot"),
        ("r_vsort", "pgcolumnar.vacuum_sorted('r_vsort','id')", "vacuum_sorted()",
         "vacuum_sorted() keeps rows committed during its snapshot window (#295)",
         "premise: vacuum_sorted() -- A's rows are outside B's snapshot"),
        ("r_clu", "pgcolumnar.cluster('r_clu','id')", "cluster()",
         "cluster() keeps rows committed during its snapshot window (#295)",
         "premise: cluster() -- A's rows are outside B's snapshot"),
    ):
        seen, got = _race(pgc_cluster, pgc_conn, table, call)
        # _race reads what B's statement counted; it is asserted here, in the loop, so the grader reads
        # each literal name (#1045 class 2).
        expect.text(seen, f"{BEFORE}|yes", premise)
        print(f"-- {label}: {got} rows survive")
        expect.num(got, AFTER, name)

    # AND THE DATA IS CORRECT, NOT ONLY THE COUNT. A rewrite that lost 20 rows and
    # duplicated 20 others counts to 150.
    with pgc_conn.cursor() as cur:
        cur.execute(
            f"SELECT count(DISTINCT id) FROM r_vac WHERE id BETWEEN 1 AND {AFTER}"
        )
        expect.num(cur.fetchone()[0], AFTER, "vacuum() preserves the exact row set")
        # For each call, not only vacuum() (#1339): a vacuum_sorted() or cluster()
        # rewrite that lost 20 rows and duplicated 20 others still counted 150.
        cur.execute(
            f"SELECT count(DISTINCT id) FROM r_vsort WHERE id BETWEEN 1 AND {AFTER}"
        )
        expect.num(cur.fetchone()[0], AFTER, "vacuum_sorted() preserves the exact row set")
        cur.execute(
            f"SELECT count(DISTINCT id) FROM r_clu WHERE id BETWEEN 1 AND {AFTER}"
        )
        expect.num(cur.fetchone()[0], AFTER, "cluster() preserves the exact row set")
