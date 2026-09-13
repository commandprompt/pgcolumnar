"""A fetching index scan on a correlated key is priced below the custom scan
through ~50,000 rows, while it does about 27x the work (#913).

The penalty term exists for this. The measurement says it is too small. This
file asserts the PLAN, not a cost number: costs drift with the constants, the
chosen node is the property.

Independent of test/index_fetch_penalty_crossover.sh: same public seam, own
fixture, own observations. Assertion names match the shell suite so the two
can be compared by name, not by importing each other.
"""


def _scan_node(plan):
    """First scan node in the tree, matching the shell suite's grep."""
    stack = [plan[0]["Plan"]]
    while stack:
        node = stack.pop(0)
        t = node["Node Type"]
        if t in (
            "Index Scan",
            "Index Only Scan",
            "Bitmap Heap Scan",
            "Custom Scan",
            "Seq Scan",
        ):
            return t
        stack.extend(node.get("Plans", ()))
    return ""


def _plan(conn, sql):
    with conn.cursor() as cur:
        cur.execute("SET enable_seqscan=off")
        cur.execute("SET enable_bitmapscan=off")
        cur.execute("SET enable_indexonlyscan=off")
        cur.execute("SET max_parallel_workers_per_gather=0")
        cur.execute("SET jit=off")
        cur.execute("SET pgcolumnar.enable_vectorization=off")
        cur.execute("SET pgcolumnar.enable_ungrouped_vector_agg=off")
        cur.execute("SET pgcolumnar.enable_group_vectorization=off")
        cur.execute(f"EXPLAIN (FORMAT JSON, COSTS OFF) {sql}")
        return cur.fetchone()[0]


def test_index_fetch_penalty_crossover(pgc_conn, expect):
    n = 1_000_000
    k_range = 50_000
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE ifc (id int, a int, b int) USING pgcolumnar")
        cur.execute(
            f"INSERT INTO ifc SELECT g, g % 10, g % 100 FROM generate_series(1, {n}) g"
        )
        cur.execute("CREATE INDEX ifc_id ON ifc(id)")
        cur.execute("ANALYZE ifc")
        cur.execute("SELECT count(*) FROM ifc")
        expect.num(cur.fetchone()[0], n, f"premise: the table holds all {n} rows")
        cur.execute(
            "SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE i.indrelid = 'ifc'::regclass AND c.relname = 'ifc_id'"
        )
        expect.num(cur.fetchone()[0], 1, "premise: the btree on id exists")

    point = _plan(pgc_conn, "SELECT sum(a) FROM ifc WHERE id = 1")
    expect.text(
        _scan_node(point),
        "Index Scan",
        "a selective point lookup still uses the index",
    )

    ranged = _plan(pgc_conn, f"SELECT sum(a) FROM ifc WHERE id <= {k_range}")
    expect.text(
        _scan_node(ranged),
        "Custom Scan",
        f"a {k_range}-row correlated range uses the custom scan, not a fetching index",
    )

    with pgc_conn.cursor() as cur:
        cur.execute("SET enable_seqscan=off")
        cur.execute("SET enable_bitmapscan=off")
        cur.execute("SET pgcolumnar.enable_custom_scan=off")
        cur.execute(f"SELECT sum(a) FROM ifc WHERE id <= {k_range}")
        idx_sum = cur.fetchone()[0]
        cur.execute("SET pgcolumnar.enable_custom_scan=on")
        cur.execute("SET enable_indexscan=off")
        cur.execute(f"SELECT sum(a) FROM ifc WHERE id <= {k_range}")
        cs_sum = cur.fetchone()[0]
    expect.num(idx_sum, cs_sum, f"both paths return the same aggregate at {k_range}")

    n_ord = 300_000
    with pgc_conn.cursor() as cur:
        cur.execute("RESET ALL")
        cur.execute("CREATE TABLE ifc_cl (id int, payload text) USING pgcolumnar")
        cur.execute(
            "INSERT INTO ifc_cl SELECT g, repeat('x', 48) "
            f"FROM generate_series(1, {n_ord}) g"
        )
        cur.execute("CREATE INDEX ifc_cl_id ON ifc_cl(id)")
        cur.execute("ANALYZE ifc_cl")
        cur.execute("SET max_parallel_workers_per_gather=0")
        cur.execute("SET random_page_cost=1.0")
        cur.execute(
            "EXPLAIN (FORMAT JSON, COSTS OFF) SELECT * FROM ifc_cl ORDER BY id"
        )
        ordered = cur.fetchone()[0]
    expect.text(
        _scan_node(ordered),
        "Index Scan",
        "the fetch penalty leaves a clustered ORDER BY on its index",
    )
