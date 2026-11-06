"""Online reclustering re-establishes Z-order clustering under ShareUpdateExclusiveLock
(Phase F3c, #1341). The pytest twin of `test/native_recluster.sh`, every check by the
shell's name.

pgcolumnar.recluster() rewrites a table's live rows in Z-order over the given columns
while reads and writes go on: it writes them back as fresh row groups, inserts their
index entries online, clears the visibility map over the row numbers it retires, and
retires the old groups. So a small 2D box skips far more groups afterwards, results
match a heap mirror, the unique index still enforces, and the lock is never
AccessExclusiveLock.

The index arm counts through an INDEX-ONLY scan of the unique index, after a VACUUM
has set the visibility map. The old index entries stay until a vacuum, so a recluster
that left their VM bits set would count them as well; and the plan is asserted,
because with only seqscan and the custom scan off the count came back right with the
index dropped.
"""
import psycopg

from pgc_vacuity import _plan_nodes

ROWS = 40960
GEN = (f"SELECT g, ((g::bigint * 7919) % 200)::int AS x, ((g::bigint * 104729) % 200)::int AS y, "
       f"'p' || (g % 97) AS payload FROM generate_series(1, {ROWS}) g")
BOX = "x BETWEEN 20 AND 40 AND y BETWEEN 20 AND 40"
IDX_GUCS = ("SET enable_seqscan = off", "SET enable_bitmapscan = off", "SET pgcolumnar.enable_custom_scan = off")
IDX_Q = f"SELECT count(*) FROM n WHERE id BETWEEN 1 AND {ROWS}"


def _one(cur, sql):
    cur.execute(sql)
    return cur.fetchone()[0]


def _rows(cur, sql):
    cur.execute(sql)
    return cur.fetchall()


def _plan(cur, sql):
    cur.execute(f"EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF, FORMAT JSON) {sql}")
    return cur.fetchone()[0]


def _skipped(cur, sql):
    """-> 'Columnar Chunk Groups Removed by Filter' of the plan, 0 when no node reports it."""
    return next((node["Columnar Chunk Groups Removed by Filter"] for node in _plan_nodes(_plan(cur, sql))
                 if "Columnar Chunk Groups Removed by Filter" in node), 0)


def _index_only(cur):
    """-> (the index-only count's plan nodes, its count), the scan forced to the index."""
    for guc in IDX_GUCS:
        cur.execute(guc)
    try:
        return list(_plan_nodes(_plan(cur, IDX_Q))), _one(cur, IDX_Q)
    finally:
        for guc in ("enable_seqscan", "enable_bitmapscan", "pgcolumnar.enable_custom_scan"):
            cur.execute(f"RESET {guc}")


def test_online_recluster(pgc_conn, expect):
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE h (id int, x int, y int, payload text)")
        cur.execute("CREATE TABLE n (id int, x int, y int, payload text) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('n', stripe_row_limit => 2048, chunk_group_row_limit => 1024)")
        cur.execute(f"INSERT INTO h {GEN}")
        cur.execute(f"INSERT INTO n {GEN}")
        cur.execute("CREATE UNIQUE INDEX n_id_uq ON n (id)")

        expect.num(_one(cur, "SELECT count(*) FROM n"), ROWS, "row count")
        expect.row_set(_rows(cur, f"SELECT id, x, y, payload FROM n WHERE {BOX}"),
                       _rows(cur, f"SELECT id, x, y, payload FROM h WHERE {BOX}"), "box parity (pre-recluster)")
        # the skip counts below come from the columnar custom scan and nothing else
        expect.plan_marker(_plan(cur, f"SELECT id FROM n WHERE {BOX}"), "Columnar Projected Columns",
                           name="the plan under test is a columnar custom scan")
        before = _skipped(cur, f"SELECT id FROM n WHERE {BOX}")

        # VACUUM first, so the VM is set over the groups recluster replaces. Most
        # bits, not all: measured, 511 of the 40960 rows still take a heap fetch.
        cur.execute("VACUUM n")
        nodes, _ = _index_only(cur)
        fetches = next((node["Heap Fetches"] for node in nodes if "Heap Fetches" in node), None)
        expect.text("yes" if isinstance(fetches, int) and fetches < ROWS // 2
                    else f"no ({fetches} heap fetches for {ROWS} rows)", "yes",
                    "VACUUM set the visibility map, so the index-only count answers from the index before the recluster")

        rec = _one(cur, "SELECT pgcolumnar.recluster('n', 'x', 'y')")
        print(f"-- recluster processed {rec} groups; box skip before={before}")
        expect.text("yes" if rec > 0 else "no", "yes", "reclustered the groups")
        after = _skipped(cur, f"SELECT id FROM n WHERE {BOX}")
        print(f"-- box groups skipped: before={before} after={after}")
        expect.text("yes" if after > before else f"no ({before} -> {after})", "yes",
                    "reclustering increases 2D skipping")
        expect.text("yes" if after >= 10 else f"no ({after})", "yes", "reclustering skips most groups")

        expect.row_set(_rows(cur, "SELECT id, x, y, payload FROM n"), _rows(cur, "SELECT id, x, y, payload FROM h"),
                       "full-table parity after recluster")
        expect.num(_one(cur, "SELECT count(*) FROM n"), ROWS, "row count after recluster")

        # the old entries are still in the index, so a recluster that left their VM
        # bits set would count them too
        nodes, count = _index_only(cur)
        expect.num(sum(1 for node in nodes if node.get("Node Type") == "Index Only Scan"
                       and node.get("Index Name") == "n_id_uq"), 1,
                   "premise: the count takes an index-only scan of n_id_uq")
        expect.num(count, ROWS, "index scan returns each row once")
        try:
            cur.execute("INSERT INTO n VALUES (1, 0, 0, 'x')")
            dup = None
        except psycopg.Error as exc:
            dup = exc
        expect.sqlstate(dup, "23505", "unique still enforced, by SQLSTATE 23505 and not by any failure")

        # the lock level, read inside the transaction that holds it
        cur.execute("BEGIN")
        cur.execute("SELECT pgcolumnar.recluster('n', 'x', 'y')")
        locks = _rows(cur, "SELECT count(*) FILTER (WHERE mode = 'ShareUpdateExclusiveLock'), "
                           "count(*) FILTER (WHERE mode = 'AccessExclusiveLock') "
                           "FROM pg_locks WHERE relation = 'n'::regclass AND locktype = 'relation'")[0]
        cur.execute("ROLLBACK")
        expect.num(locks[0], 1, "recluster holds ShareUpdateExclusiveLock")
        expect.num(locks[1], 0, "recluster holds no AccessExclusiveLock")
