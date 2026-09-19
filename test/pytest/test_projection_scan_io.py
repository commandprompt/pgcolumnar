"""A covering projection must be priced from its own pages.

PgColumnarSetRelPathlist offers a covering-projection path by scaling the
BASE scan's run cost. That run's I/O term is seq_page_cost * rel->pages,
the whole relation file. A covering projection has its own row groups.

This file asserts the PLANNER ratio, not a runtime. Independent of
test/projection_scan_io.sh: same public seam (EXPLAIN cost of a covering
projection vs the relation's pages), own fixture, own observations.
Assertion names match the shell suite so the two can be compared by name,
not by importing each other.
"""


def _nodes(plan):
    stack = [plan[0]["Plan"]]
    while stack:
        node = stack.pop(0)
        yield node
        stack.extend(node.get("Plans") or ())


def _custom_scan(plan):
    for node in _nodes(plan):
        if node.get("Node Type") == "Custom Scan":
            return node
    return None


def _plan(conn, sql, projection_scan):
    with conn.cursor() as cur:
        cur.execute("SET max_parallel_workers_per_gather = 0")
        cur.execute("SET pgcolumnar.enable_ungrouped_vector_agg = off")
        cur.execute("SET pgcolumnar.enable_group_vectorization = off")
        cur.execute("SET jit = off")
        cur.execute("SET seq_page_cost = 1000")
        cur.execute("SET cpu_tuple_cost = 0")
        cur.execute("SET cpu_operator_cost = 0")
        cur.execute("SET cpu_index_tuple_cost = 0")
        cur.execute(
            "SET pgcolumnar.enable_projection_scan = "
            + ("on" if projection_scan else "off")
        )
        cur.execute("EXPLAIN (FORMAT JSON, COSTS ON) " + sql)
        return cur.fetchone()[0]


def test_projection_scan_io(pgc_conn, expect):
    n = 36000
    with pgc_conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE pciot (ck int, wide text) USING pgcolumnar"
        )
        cur.execute(
            "SELECT pgcolumnar.set_options('pciot', stripe_row_limit => 1800, "
            "chunk_group_row_limit => 600)"
        )
        # Compressible payload, independent of the shell twin: different
        # table, N, stripe, column names, and repeat length.
        cur.execute(
            f"INSERT INTO pciot SELECT ck, repeat('w', 1100) "
            f"FROM generate_series(1, {n}) ck ORDER BY md5((ck + 3)::text)"
        )
        cur.execute(
            "SELECT pgcolumnar.add_projection('pciot', 'onck', "
            "ARRAY['ck','wide'], ARRAY['ck'])"
        )
        cur.execute("ANALYZE pciot")
        cur.execute("SELECT count(*) FROM pciot")
        expect.num(cur.fetchone()[0], n, "premise: the table holds every inserted row")
        cur.execute(
            "SELECT count(*) FROM pgcolumnar.projection_declaration "
            "WHERE rel = 'pciot'::regclass AND name = 'onck'"
        )
        expect.num(cur.fetchone()[0], 1, "premise: a covering projection exists")

    sql = f"SELECT ck, wide FROM pciot WHERE ck BETWEEN 1 AND {n}"
    cover = _plan(pgc_conn, sql, True)
    node = _custom_scan(cover)

    expect.text(
        (node or {}).get("Columnar Projection") or "none",
        "onck",
        "premise: the plan uses the covering projection",
    )

    c_run = node["Total Cost"] - node["Startup Cost"]
    expect.text(
        "yes" if c_run > 0 else "no",
        "yes",
        "premise: the covering scan has a positive run cost",
    )

    with pgc_conn.cursor() as cur:
        cur.execute("SELECT pg_relation_size('pciot')")
        rel_bytes = cur.fetchone()[0]
        cur.execute(
            "SELECT coalesce(sum(rg.byte_length),0) "
            "FROM pgcolumnar.row_group rg "
            "JOIN pgcolumnar.projection p ON p.proj_storage_id = rg.storage_id "
            "JOIN pgcolumnar.storage s ON s.storage_id = p.storage_id "
            "WHERE s.relation_oid = 'pciot'::regclass AND p.name = 'onck'"
        )
        proj_bytes = cur.fetchone()[0]

    rel_pages = rel_bytes / 8192.0
    base_io = rel_pages * 1000.0
    ratio = (c_run / base_io) if base_io > 0 else 0.0
    print(
        f"-- cover_run={c_run} rel_pages={rel_pages} "
        f"base_io={base_io} ratio={ratio:.3f}"
    )
    print(f"-- proj_bytes={proj_bytes} rel_bytes={rel_bytes}")

    expect.text(
        "minority" if rel_bytes > 0 and proj_bytes < rel_bytes * 0.7 else "majority",
        "minority",
        "premise: the covering projection occupies a minority of the relation",
    )
    expect.text(
        "base-pages" if ratio > 0.8 else "proj-pages",
        "proj-pages",
        "a covering projection is not priced from the base table's pages",
    )
