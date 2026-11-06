"""Compaction must not drop rows committed after its caller's statement snapshot.

`pgcolumnar_compact_relation` used the caller's pre-lock statement snapshot, so a row
group committed after that snapshot was invisible to the rewrite and was destroyed by
the relfilenode swap. The fix takes a fresh snapshot after the lock (#295). Covered
for `vacuum()`, `vacuum_sorted()` and `cluster()` -- the Z-order twin goes through the
same path.

Independent of test/native_vacuum_race.sh: same public seam (the maintenance
functions), own fixture, own observations. Assertion names match the shell suite so
the two can be compared by name, not by importing each other.

THE SHELL SUITE RACES ON THE CLOCK; THIS ONE DOES NOT, AND THAT IS THE POINT. The
original backgrounds a psql that runs `SELECT pg_sleep(5)` inside its transaction,
sleeps 2 in the shell, and inserts during the gap. It works, but the interleaving it
needs is produced by two sleeps rather than asserted, and the suite throws away the
one observation that would prove the race happened: session B's snapshot count is
redirected to /dev/null. If B's snapshot were taken AFTER A's commit -- a slow box, a
loaded box, a sleep that lands differently -- B would simply see all 150 rows, the
maintenance call would keep them all, and every check would pass having tested
nothing.

Two connections and no sleeps make the ordering explicit, and the premises assert it:
B sees 50 before A commits, and B still sees 50 after. The second is the load-bearing
one. It says A's rows are genuinely outside B's snapshot, which is the whole
precondition for the defect -- and it is exactly what a timing slip would destroy.
"""
import psycopg

BEFORE = 50
AFTER = 150


def _race(pgc_cluster, conn, expect, table, call, label):
    """Run one maintenance call under a snapshot pinned before a concurrent commit."""
    with conn.cursor() as cur:
        cur.execute(f"CREATE TABLE {table} (id int) USING pgcolumnar")
        cur.execute(
            f"INSERT INTO {table} SELECT g FROM generate_series(1,{BEFORE}) g"
        )
        cur.execute("SELECT current_schema()")
        schema = cur.fetchone()[0]

    # SESSION B. A second connection, in A's private schema, holding an explicit
    # REPEATABLE READ transaction so its snapshot is pinned for the whole window.
    b = psycopg.connect(pgc_cluster.dsn(), autocommit=False)
    try:
        with b.cursor() as bc:
            bc.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            bc.execute(f'SET search_path TO "{schema}", public')
            bc.execute(f"SELECT count(*) FROM {table}")
            pinned = bc.fetchone()[0]
        expect.num(
            pinned, BEFORE,
            f"premise: {label} -- B pinned a snapshot holding {BEFORE} rows",
        )

        # SESSION A commits while B's snapshot is held.
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {table} "
                f"SELECT g FROM generate_series({BEFORE + 1},{AFTER}) g"
            )

        # THE PRECONDITION FOR THE DEFECT, ASSERTED RATHER THAN TIMED. If B could
        # see A's rows there would be nothing for a stale snapshot to lose, and the
        # maintenance call below would keep all 150 for a reason that has nothing to
        # do with the fix.
        with b.cursor() as bc:
            bc.execute(f"SELECT count(*) FROM {table}")
            still = bc.fetchone()[0]

        with b.cursor() as bc:
            bc.execute(f"SELECT {call}")
        b.commit()
    finally:
        b.close()

    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        return still, cur.fetchone()[0]


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
        still, got = _race(pgc_cluster, pgc_conn, expect, table, call, label)
        # _race reads B's count; it is asserted here, in the loop, so the grader reads
        # each literal name (#1045 class 2).
        expect.num(still, BEFORE, premise)
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
