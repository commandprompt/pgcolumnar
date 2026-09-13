"""Exact zone-map comparison boundaries and pruning effectiveness (#831)."""


def _nodes(plan):
    for root in plan:
        stack = [root["Plan"]]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(node.get("Plans", ()))


def _plan(conn, qual):
    with conn.cursor() as cur:
        cur.execute("SET pgcolumnar.enable_bloom_filter=off")
        cur.execute("SET pgcolumnar.enable_vectorization=off")
        cur.execute(
            "EXPLAIN (ANALYZE, FORMAT JSON, COSTS OFF, TIMING OFF, SUMMARY OFF) "
            f"SELECT id FROM zb_c WHERE {qual}"
        )
        return cur.fetchone()[0]


def _removed(plan):
    return next(
        node.get("Columnar Chunk Groups Removed by Filter", 0)
        for node in _nodes(plan)
        if "Columnar Chunk Groups Total" in node
    )


def _ids(conn, table, qual):
    with conn.cursor() as cur:
        cur.execute(f"SELECT id FROM {table} WHERE {qual} ORDER BY id")
        return [row[0] for row in cur]


def test_exact_zonemap_boundaries(pgc_conn, expect):
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE zb_h(id int, v int)")
        cur.execute("CREATE TABLE zb_c(id int, v int) USING pgcolumnar")
        cur.execute(
            "SELECT pgcolumnar.set_options('zb_c', stripe_row_limit => 1000)"
        )
        cur.execute("INSERT INTO zb_h SELECT g,g FROM generate_series(1,2000) g")
        cur.execute("INSERT INTO zb_c SELECT * FROM zb_h")
        cur.execute(
            "SELECT count(*) FROM pgcolumnar.row_group "
            "WHERE storage_id=pgcolumnar.get_storage_id('zb_c')"
        )
        expect.num(cur.fetchone()[0], 2,
                   "premise: the boundary fixture has two row groups")

    expect.num(
        _removed(_plan(pgc_conn, "v < 1001")), 1,
        "< excludes the group whose minimum equals the constant",
    )
    expect.rows(
        _ids(pgc_conn, "zb_c", "v <= 1001"),
        _ids(pgc_conn, "zb_h", "v <= 1001"),
        "<= keeps the row at a row-group minimum",
    )
    expect.num(
        _removed(_plan(pgc_conn, "v <= 1001")), 0,
        "premise: <= at the second-group minimum removes no group",
    )
    expect.rows(
        _ids(pgc_conn, "zb_c", "v >= 1000"),
        _ids(pgc_conn, "zb_h", "v >= 1000"),
        ">= keeps the row at a row-group maximum",
    )
    expect.num(
        _removed(_plan(pgc_conn, "v >= 1000")), 0,
        "premise: >= at the first-group maximum removes no group",
    )
    expect.num(
        _removed(_plan(pgc_conn, "v > 1000")), 1,
        "> excludes the group whose maximum equals the constant",
    )
    expect.num(
        _removed(_plan(pgc_conn, "v = 1001")), 1,
        "= excludes the group lying wholly below the constant",
    )
    # THE LIVENESS PREMISE, last, as in the bash suite.
    #
    # Every arm above reads a plan or a row set, and a backend that died partway through
    # would leave the arms that already ran green and the rest unrun. `pgc_summary`
    # accounting catches a missing arm, but only this says the session that produced the
    # answers was still the one answering at the end. It is cheap and it is the difference
    # between "the arms passed" and "the arms passed on a live server".
    with pgc_conn.cursor() as cur:
        cur.execute("SELECT 1")
        expect.num(cur.fetchone()[0], 1, "backend alive")
