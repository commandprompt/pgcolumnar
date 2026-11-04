"""Scale validation: online compaction, rewrite, reclustering and physical reclaim at
half a million rows (Phase F, #2018). The pytest twin of `test/native_scale.sh`, every
check by the shell's name.

The small suites prove the logic on a handful of row groups. This one proves the
mutation, clustering and reclaim machinery holds at scale and across many cycles. Every
step is compared against a heap mirror with an order-independent fingerprint. Then four
rounds of {delete a 1/8 slice, compact_rewrite} run with no inserts, so the only writes
are compaction's, and the file must plateau: round 4 is no larger than round 2. Each
round's delete must remove its whole slice, so every round does reclaim work (#2024).

SCALE_N sets the row count, as it does for the shell suite. The default is the shell's,
500000. The pytest-cluster jobs in ci.yml and nightly.yml set SCALE_N=200000, so the
jobs stay inside their 40-minute limit. At 200000 this twin still fails a recluster
that errors and a rewrite that stops reusing freed space.
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


def _maint(cur, sql):
    """Run one maintenance call and return its result, or its error text.

    The shell suite's maint() does the same. A failed recluster or compact_rewrite
    rolls back, so the parity checks still pass and the file stops changing, which
    the plateau check reads as success. So the result is checked by name, and the
    error text is what a failure prints (OffgridwithJD, review of #2021).
    """
    import psycopg          # deferred, as conftest does
    try:
        return _one(cur, sql)
    except psycopg.Error as e:
        return f"{type(e).__name__}: {e}".strip()


def _is_count(result):
    """'yes' when the call returned a group count, else the whole result."""
    ok = isinstance(result, int) and not isinstance(result, bool)
    return "yes" if ok else f"no: [{result}]"


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
        rew = _maint(cur, "SELECT pgcolumnar.compact_rewrite('n', 0.1)")
        print(f"-- compact_rewrite rewrote {rew} groups")
        expect.text(_is_count(rew), "yes", "compact_rewrite returned a group count")
        expect.rows(_fp(cur, "n"), _fp(cur, "h"), "parity after compact_rewrite")
        # Rewritten rows carry new row numbers and need fresh index entries, so an
        # indexed range lookup must match the heap exactly.
        expect.num(_one(cur, f"SELECT count(*) FROM n WHERE id BETWEEN 1 AND {N // 2}"),
                   _one(cur, f"SELECT count(*) FROM h WHERE id BETWEEN 1 AND {N // 2}"),
                   "indexed lookup after rewrite")

        # Online recluster by c; the results do not change. This is the slow step.
        rec = _maint(cur, "SELECT pgcolumnar.recluster('n', 'c')")
        print(f"-- recluster processed {rec} groups")
        expect.text(_is_count(rec), "yes", "recluster returned a group count")
        expect.rows(_fp(cur, "n"), _fp(cur, "h"), "parity after recluster")

        # ---- physical reclaim: steady state across repeated compaction ----
        # Round 4 deletes id % 8 = 5, not 4 (#2024). Every id with id % 8 = 4 also
        # has id % 4 = 0, and those rows went in the first delete. So round 4
        # deleted nothing, rewrote nothing, and the steady-state check compared
        # round 3 with round 2. Each round's delete is now counted: it must remove
        # all (N - s) // 8 + 1 rows of its slice from each table.
        slices = (1, 2, 3, 5)
        sizes = []
        for r in (1, 2, 3, 4):
            s = slices[r - 1]
            for t in ("h", "n"):
                cur.execute(f"DELETE FROM {t} WHERE id % 8 = {s}")
                expect.wrote(cur, (N - s) // 8 + 1, f"reclaim round {r} deletes its whole id % 8 slice from {t}")
            # Wait for the horizon first (#2010): a rewrite reuses a freed range only
            # when no transaction is older than the one that freed it. Rounds 2 to 4,
            # the rounds the steady-state check reads, wait. Round 1 reuses what the
            # recluster freed, and no check compares its size.
            if r >= 2:
                blockers, calls = wait_freed_range_past_horizon(pgc_conn, "n")
                print(f"-- round {r} horizon blockers/calls before the rewrite: {blockers}/{calls}")
                expect.num(blockers, 0,
                           f"round {r}: no transaction older than the newest freed range was open before the rewrite")
            rw = _maint(cur, "SELECT pgcolumnar.compact_rewrite('n', 0.02)")
            expect.text(_is_count(rw), "yes", f"reclaim round {r}: compact_rewrite returned a group count")
            expect.rows(_fp(cur, "n"), _fp(cur, "h"), f"parity after reclaim round {r}")
            sizes.append(_one(cur, "SELECT pg_relation_size('n')"))
            print(f"-- round {r} file size: {sizes[-1]}")

        # Rounds 2 to 4 each delete a slice and reuse earlier frees, so the file
        # must not grow past round 2.
        expect.text("yes" if sizes[3] <= sizes[1] else f"no (round 2 {sizes[1]}, round 4 {sizes[3]})",
                    "yes", "reclaim reached steady state (round 4 <= round 2)")
