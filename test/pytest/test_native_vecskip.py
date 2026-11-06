"""Per-vector skipping: inside a row group the predicate cannot prune whole, the
1024-value vectors its zone maps rule out are neither decoded nor emitted (Phase D5b,
#512, #1341). The pytest twin of `test/native_vecskip.sh`, every check by the shell's
name.

One row group of eight vectors with tight, disjoint id ranges, and a range inside
vector 0: exactly seven vectors are skipped and no whole group is removed, and the
rows equal a heap mirror's. The count alone cannot say WHICH mechanism skipped:
without the zone-map rule, #452's by-value refinement decodes the id column of every
vector and rules the same seven out. So the decodes and the by-value count are read as
well (1 and 0). The vectorized aggregate must report the counter too, with the same
seven, and the premises pin that it really is that node.
"""
ROWS = 8192
GEN = f"SELECT g AS id, (g % 97) AS v FROM generate_series(1, {ROWS}) g"
RANGE = "SELECT id FROM n WHERE id BETWEEN 100 AND 200"
AGGQ = "SELECT count(*), sum(v) FROM n WHERE id BETWEEN 100 AND 200"


def _one(cur, sql):
    cur.execute(sql)
    return cur.fetchone()[0]


def _rows(cur, sql):
    cur.execute(sql)
    return cur.fetchall()


def _plan(cur, sql, agg=False):
    """-> the EXPLAIN ANALYZE text; the ungrouped vector aggregate on when `agg`."""
    if agg:
        cur.execute("SET pgcolumnar.enable_ungrouped_vector_agg = on")
    try:
        return "\n".join(r[0] for r in _rows(cur, f"EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) {sql}"))
    finally:
        if agg:
            cur.execute("RESET pgcolumnar.enable_ungrouped_vector_agg")


def _counter(plan, label):
    for line in plan.splitlines():
        if label in line:
            digits = "".join(ch if ch.isdigit() else " " for ch in line[line.index(label) + len(label):]).split()
            return int(digits[0]) if digits else None
    return None


def test_per_vector_skipping(pgc_conn, expect):
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE h (id int, v int)")
        cur.execute("CREATE TABLE n (id int, v int) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('n', stripe_row_limit => 16384, chunk_group_row_limit => 1024)")
        cur.execute(f"INSERT INTO h {GEN}")
        cur.execute(f"INSERT INTO n {GEN}")

        # the node, before any counter is read out of it
        expect.text("yes" if "Columnar Projected Columns" in _plan(cur, RANGE) else "no", "yes",
                    "the plan under test is a columnar custom scan")
        expect.num(_one(cur, "SELECT count(*) FROM n"), ROWS, "row count")
        expect.num(_one(cur, "SELECT count(*) FROM pgcolumnar.row_group "
                             "WHERE storage_id = pgcolumnar.get_storage_id('n')"), 1, "single row group")

        expect.row_set(_rows(cur, "SELECT id, v FROM n WHERE id BETWEEN 100 AND 200"),
                       _rows(cur, "SELECT id, v FROM h WHERE id BETWEEN 100 AND 200"), "range parity")
        plan = _plan(cur, RANGE)
        expect.num(_counter(plan, "Columnar Vectors Skipped"), 7, "the seven vectors outside the range are skipped")
        expect.num(_counter(plan, "Columnar Chunk Groups Removed by Filter"), 0, "no whole group removed")
        expect.text(f"{_counter(plan, 'Columnar Vector Decodes')}|{_counter(plan, 'Columnar Vectors Ruled Out by Value')}",
                    "1|0", "the zone maps ruled them out without decoding them: one vector decoded, none ruled out by value")
        expect.num(_counter(_plan(cur, "SELECT id FROM n WHERE id > 0"), "Columnar Vectors Skipped"), 0,
                   "non-selective removes no vectors")

        # the vectorized aggregate must report the counter too, on the same predicate
        agg = _plan(cur, AGGQ, agg=True)
        expect.text("yes" if "Columnar Vectorized Aggregates" in agg else "no", "yes",
                    "premise: the aggregate arm really is a vectorized aggregate")
        expect.text("yes" if "Columnar Projected Columns" in agg else "no", "no", "premise: and it is not the scalar scan")
        expect.text("yes" if "Columnar Vectors Skipped" in agg else "no", "yes",
                    "the vectorized aggregate reports Columnar Vectors Skipped")
        expect.num(_counter(agg, "Columnar Vectors Skipped"), 7, "and the aggregate skips the same seven vectors")

        for lo, hi, name in ((2000, 5000, "cross-vector range parity"), (8000, 9000, "last-vector range parity")):
            expect.row_set(_rows(cur, f"SELECT id, v FROM n WHERE id BETWEEN {lo} AND {hi}"),
                           _rows(cur, f"SELECT id, v FROM h WHERE id BETWEEN {lo} AND {hi}"), name)
        expect.num(_one(cur, "SELECT count(*) FROM n WHERE id BETWEEN 100000 AND 200000"), 0, "empty range parity")
