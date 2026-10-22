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


def _scan_rows(plan):
    """The row estimate on the first scan node, which is what the prices use."""
    stack = [plan[0]["Plan"]]
    while stack:
        node = stack.pop(0)
        if node["Node Type"] in (
            "Index Scan",
            "Index Only Scan",
            "Bitmap Heap Scan",
            "Custom Scan",
            "Seq Scan",
        ):
            return node["Plan Rows"]
        stack.extend(node.get("Plans", ()))
    return -1


# The shell's IDX_FORCE: what makes the index-forced read take the index.
IDX_FORCE = ("SET enable_seqscan=off", "SET enable_bitmapscan=off",
             "SET pgcolumnar.enable_custom_scan=off")


def _plan(conn, sql, costs=False):
    with conn.cursor() as cur:
        cur.execute("SET enable_seqscan=off")
        cur.execute("SET enable_bitmapscan=off")
        cur.execute("SET enable_indexonlyscan=off")
        cur.execute("SET max_parallel_workers_per_gather=0")
        cur.execute("SET jit=off")
        cur.execute("SET pgcolumnar.enable_vectorization=off")
        cur.execute("SET pgcolumnar.enable_ungrouped_vector_agg=off")
        cur.execute("SET pgcolumnar.enable_group_vectorization=off")
        off = "OFF" if not costs else "ON"
        cur.execute(f"EXPLAIN (FORMAT JSON, COSTS {off}) {sql}")
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
        # THE ESTIMATE MUST NOT BE A SAMPLE DRAW (#1168). ANALYZE samples 300 x
        # the statistics target rows; at the default 100 that is 30,000 of these
        # 1,000,000 and the estimate for `id <= 50000` moves every run. The arm
        # below asserts a chosen NODE where the two prices are close, so the node
        # flips when the estimate does -- it flaked twice in two days in the
        # gate, once on main. 300 x 3500 >= the table, so ANALYZE reads all of it
        # and there is no draw left to come out differently.
        cur.execute("ALTER TABLE ifc ALTER COLUMN id SET STATISTICS 3500")
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

    # ASSERTED, NOT ASSUMED. The shell twin gets its determinism from the same
    # ALTER, but a premise that only one half checks is a premise neither half
    # checks after the next edit.
    priced = _plan(pgc_conn, f"SELECT sum(a) FROM ifc WHERE id <= {k_range}", costs=True)
    expect.num(
        _scan_rows(priced),
        k_range,
        f"the {k_range}-row estimate is exact, "
        "so no arm here rests on a sample draw",
    )

    ranged = _plan(pgc_conn, f"SELECT sum(a) FROM ifc WHERE id <= {k_range}")
    expect.text(
        _scan_node(ranged),
        "Custom Scan",
        f"a {k_range}-row correlated range uses the custom scan, not a fetching index",
    )

    # BOTH PATHS AGAINST THE GENERATOR, AND THE INDEX PATH SEEN TO BE ONE (#1339). The
    # two sums used to be compared with each other only, so two equally wrong answers
    # agreed, and nothing said the "index" path fetched by index. a is g % 10, so the
    # sum over ids 1..K comes from generate_series, not from the table.
    with pgc_conn.cursor() as cur:
        cur.execute(f"SELECT sum(g % 10) FROM generate_series(1, {k_range}) g")
        gen_sum = cur.fetchone()[0]
        # ONE SET LIST FOR THE READ AND ITS PLAN, as the shell's IDX_FORCE is: the
        # plan is taken on this cursor, in this session state, right before the
        # read, so a change to how the read is forced is a change to what is planned.
        for s in IDX_FORCE:
            cur.execute(s)
        cur.execute(f"EXPLAIN (FORMAT JSON, COSTS OFF) SELECT sum(a) FROM ifc WHERE id <= {k_range}")
        forced = cur.fetchone()[0]
        cur.execute(f"SELECT sum(a) FROM ifc WHERE id <= {k_range}")
        idx_sum = cur.fetchone()[0]
        cur.execute("SET pgcolumnar.enable_custom_scan=on")
        cur.execute("SET enable_indexscan=off")
        cur.execute(f"SELECT sum(a) FROM ifc WHERE id <= {k_range}")
        cs_sum = cur.fetchone()[0]
        cur.execute("RESET enable_indexscan")
    expect.text(f"index {idx_sum}, scan {cs_sum}", f"index {gen_sum}, scan {gen_sum}",
                f"both paths return the same aggregate at {k_range}")
    expect.text(_scan_node(forced), "Index Scan", "and the index-forced path really fetches by index")

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
