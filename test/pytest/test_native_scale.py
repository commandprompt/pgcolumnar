"""Scale validation: online compaction, rewrite, reclustering and physical reclaim at
half a million rows (Phase F, #2018). The pytest twin of `test/native_scale.sh`, every
check by the shell's name.

The small suites prove the logic on a handful of row groups. This one proves the
mutation, clustering and reclaim machinery holds at scale and across many cycles. Every
step is compared against a heap mirror with an order-independent fingerprint. Then four
rounds of {delete a rotating 1/8 slice, compact_rewrite} run with no inserts, so the only
writes are compaction's, and the file must plateau: round 4 is no larger than round 2.

SCALE_N sets the row count, as it does for the shell suite. The default is the shell's,
500000.
"""
import os

from pgc_horizon import wait_freed_range_past_horizon

N = int(os.environ.get("SCALE_N", "500000"))

# compute the products in bigint so a large g does not overflow int32
GEN = f"""SELECT g AS id, (g % 1000)::int AS a, (g::bigint * 7) AS b,
       ((g::bigint * 7919) % 100000)::int AS c
  FROM generate_series(1, {N}) g"""

# order-independent fingerprint of the live set (the same query on both tables)
FP = """SELECT count(*), sum(a)::bigint, sum(b)::bigint, min(c), max(c),
           bit_xor(('x' || substr(md5(id||'|'||a||'|'||b||'|'||c), 1, 16))::bit(64)::bigint)"""


def _one(cur, sql):
    cur.execute(sql)
    return cur.fetchone()[0]


def _fp(cur, table):
    cur.execute(f"{FP} FROM {table}")
    return cur.fetchall()


def test_scale_parity_and_reclaim_steady_state(pgc_conn, expect):
    with pgc_conn.cursor() as cur:
        print(f"-- loading {N} rows into heap + columnar")
        cur.execute("CREATE TABLE h (id int, a int, b bigint, c int)")
        cur.execute("CREATE TABLE n (id int, a int, b bigint, c int) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('n', stripe_row_limit => 50000, chunk_group_row_limit => 10000)")
        cur.execute(f"INSERT INTO h {GEN}")
        cur.execute(f"INSERT INTO n {GEN}")
        cur.execute("CREATE INDEX n_id ON n (id)")

        expect.num(_one(cur, "SELECT count(*) FROM n"), N, "bulk load count")
        expect.rows(_fp(cur, "n"), _fp(cur, "h"), "bulk load parity")

        # ---- correctness under mutation and clustering ----
        # Delete about 25%, then an online rewrite of the partly deleted groups.
        cur.execute("DELETE FROM h WHERE id % 4 = 0")
        cur.execute("DELETE FROM n WHERE id % 4 = 0")
        expect.rows(_fp(cur, "n"), _fp(cur, "h"), "parity after delete")
        rew = _one(cur, "SELECT pgcolumnar.compact_rewrite('n', 0.1)")
        print(f"-- compact_rewrite rewrote {rew} groups")
        expect.rows(_fp(cur, "n"), _fp(cur, "h"), "parity after compact_rewrite")
        # Rewritten rows carry new row numbers and need fresh index entries, so an
        # indexed range lookup must match the heap exactly.
        expect.num(_one(cur, f"SELECT count(*) FROM n WHERE id BETWEEN 1 AND {N // 2}"),
                   _one(cur, f"SELECT count(*) FROM h WHERE id BETWEEN 1 AND {N // 2}"),
                   "indexed lookup after rewrite")

        # Online recluster by c; the results do not change. This is the slow step.
        rec = _one(cur, "SELECT pgcolumnar.recluster('n', 'c')")
        print(f"-- recluster processed {rec} groups")
        expect.rows(_fp(cur, "n"), _fp(cur, "h"), "parity after recluster")

        # ---- physical reclaim: steady state across repeated compaction ----
        sizes = []
        for r in (1, 2, 3, 4):
            # Round 4 deletes nothing, in the shell suite too: every id with
            # id % 8 = 4 also has id % 4 = 0, and those went in the first delete.
            # So round 4 rewrites without new deletes. Said here as a count, since a
            # write that wrote nothing must be named.
            for t in ("h", "n"):
                cur.execute(f"DELETE FROM {t} WHERE id % 8 = {r}")
                if r == 4:
                    expect.wrote(cur, 0, f"round 4 deletes no rows from {t}: id % 8 = 4 went with id % 4 = 0")
            # Wait for the horizon first (#2010): a rewrite reuses a freed range only
            # when no transaction is older than the one that freed it. Rounds 2 to 4,
            # the rounds the steady-state check reads, wait. Round 1 reuses what the
            # recluster freed, and no check compares its size.
            if r >= 2:
                blockers, calls = wait_freed_range_past_horizon(pgc_conn, "n")
                print(f"-- round {r} horizon blockers/calls before the rewrite: {blockers}/{calls}")
                expect.num(blockers, 0,
                           f"round {r}: no transaction older than the newest freed range was open before the rewrite")
            cur.execute("SELECT pgcolumnar.compact_rewrite('n', 0.02)")
            expect.rows(_fp(cur, "n"), _fp(cur, "h"), f"parity after reclaim round {r}")
            sizes.append(_one(cur, "SELECT pg_relation_size('n')"))
            print(f"-- round {r} file size: {sizes[-1]}")

        # Rounds 2 to 4 reuse earlier frees, so the file must not grow past round 2.
        expect.text("yes" if sizes[3] <= sizes[1] else f"no (round 2 {sizes[1]}, round 4 {sizes[3]})",
                    "yes", "reclaim reached steady state (round 4 <= round 2)")
