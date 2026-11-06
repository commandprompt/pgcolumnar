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
        # Same per-row-group page rounding the cost model uses.
        cur.execute(
            "SELECT coalesce(sum(ceil(rg.byte_length::numeric / (8192 - 24))),0) "
            "FROM pgcolumnar.row_group rg "
            "JOIN pgcolumnar.projection p ON p.proj_storage_id = rg.storage_id "
            "JOIN pgcolumnar.storage s ON s.storage_id = p.storage_id "
            "WHERE s.relation_oid = 'pciot'::regclass AND p.name = 'onck'"
        )
        proj_pages = float(cur.fetchone()[0])

    rel_pages = rel_bytes / 8192.0
    base_io = rel_pages * 1000.0
    ratio = (c_run / base_io) if base_io > 0 else 0.0
    want_run = proj_pages * 1000.0
    base = _plan(pgc_conn, sql, False)
    base_node = _custom_scan(base)
    base_run = base_node["Total Cost"] - base_node["Startup Cost"]
    print(
        f"-- cover_run={c_run} rel_pages={rel_pages} "
        f"base_io={base_io} ratio={ratio:.3f}"
    )
    print(
        f"-- proj_bytes={proj_bytes} rel_bytes={rel_bytes} "
        f"proj_pages={proj_pages} want_run={want_run}"
    )
    print(f"-- base_run={base_run}")

    expect.text(
        "minority" if rel_bytes > 0 and proj_bytes < rel_bytes * 0.7
        else f"majority proj_bytes={proj_bytes} rel_bytes={rel_bytes}",
        "minority",
        "premise: the covering projection occupies a minority of the relation",
    )
    # Band around seq_page_cost * proj_pages, not a ceiling against base_io.
    delta = abs(c_run - want_run) / want_run if want_run > 0 else 1.0
    expect.text(
        f"off-band got={c_run} want={want_run}"
        if delta > 0.05 else "proj-pages",
        "proj-pages",
        "a covering projection is not priced from the base table's pages",
    )

    # Lookup failure must not make the covering path cheaper than the base
    # file. Returning 1 page is essentially free; rel->pages cannot undercut
    # the base scan. Own table (pciot / onck), not the shell twin's.
    with pgc_conn.cursor() as cur:
        cur.execute(
            "UPDATE pgcolumnar.projection p SET proj_storage_id = 0 "
            "FROM pgcolumnar.storage s "
            "WHERE p.storage_id = s.storage_id "
            "AND s.relation_oid = 'pciot'::regclass "
            "AND p.name = 'onck' AND p.projection_id > 0"
        )
        cur.execute(
            "SELECT count(*) FROM pgcolumnar.projection p "
            "JOIN pgcolumnar.storage s ON s.storage_id = p.storage_id "
            "WHERE s.relation_oid = 'pciot'::regclass "
            "AND p.name = 'onck' AND p.proj_storage_id = 0"
        )
        expect.num(
            cur.fetchone()[0],
            1,
            "premise: the projection row still exists after its storage id is cleared",
        )

    miss = _plan(pgc_conn, sql, True)
    miss_node = _custom_scan(miss)
    expect.text(
        (miss_node or {}).get("Columnar Projection") or "none",
        "onck",
        "premise: a covering path is still offered when projection storage cannot be found",
    )
    # THE ORACLE AND THE ARM MUST BE READ IN THE SAME CATALOG STATE (#1155 review).
    # The UPDATE above clears `proj_storage_id`, which is the SAME KEY #1180's
    # sibling-pages walk reads. A `base_run` captured before it is priced with the
    # projection's pages subtracted; a `miss_run` captured after it is priced from
    # the whole file. The ratio then spans two different trees rather than
    # measuring one.
    #
    # Composed with main carrying #1180 the old order read
    # `miss_run=41991.6 base_run=22000 miss_ratio=1.909` and failed; re-reading
    # the oracle here gives `base_run=42000` and `miss_ratio=1.000`.
    #
    # Before #1180 both states gave the same `rel->pages` and the ratio was
    # exactly 1.000, so nothing here could have noticed.
    miss_base = _custom_scan(_plan(pgc_conn, sql, False))
    miss_base_run = miss_base["Total Cost"] - miss_base["Startup Cost"]
    miss_run = miss_node["Total Cost"] - miss_node["Startup Cost"]
    miss_ratio = (miss_run / miss_base_run) if miss_base_run > 0 else 0.0
    print(f"-- miss_run={miss_run} base_run={miss_base_run} miss_ratio={miss_ratio:.3f}")
    # Compare to the measured base run (projection off), not base_io from
    # pg_relation_size, so #1180 cannot silently shift the denominator.
    expect.text(
        f"moved miss_ratio={miss_ratio:.3f} (miss_run={miss_run}, base_run={miss_base_run})"
        if miss_ratio < 0.8 or miss_ratio > 1.25 else "not-one-page",
        "not-one-page",
        "a covering projection whose storage cannot be found is not priced as one page",
    )


def test_an_empty_covering_projection_is_priced_as_one_page(pgc_conn, expect):
    """The THIRD return of pgcolumnar_projection_pages, which the arms above never reach.

    That function has three returns. `test_projection_scan_io` reaches two of them: the
    miss (`proj_storage_id = 0` -> fallbackPages) and the covering arm. The third is
    `if (pages < 1) pages = 1`, and it is NOT the "small projection" case --
    COLUMNAR_PAGE_ROUND_UP rounds every group to a WHOLE page, so one row is already a
    full page and returns through the covering arm. Probed at all three returns:

        empty table + projection   bytes=0       pages=0   this return
        one row                    bytes=8168    pages=1   covering return
        32000 rows                 bytes=261376  pages=32  covering return

    So the state is a projection whose storage holds ZERO row groups -- a projection on a
    relation nothing has been written to -- and not a small one. An arm built on "a tiny
    projection" measures the covering return and reports green having never reached this
    line (#1208).

    THE ORACLE IS THE PLAN, NOT A COST. Priced at one page the covering path wins;
    mutated to `pages = 997` it loses and the projection leaves the plan. `_plan` already
    sets seq_page_cost = 1000, so one page against 997 is not a marginal difference, and
    a pinned cost would move with every unrelated change to the cost model.
    """
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE pciot_e (ck int, wide text) USING pgcolumnar")
        cur.execute(
            "SELECT pgcolumnar.set_options('pciot_e', stripe_row_limit => 1000, "
            "chunk_group_row_limit => 100)"
        )
        cur.execute(
            "SELECT pgcolumnar.add_projection('pciot_e', 'onck', "
            "ARRAY['ck'], ARRAY['ck'])"
        )
        cur.execute("ANALYZE pciot_e")

        # KEYED ON get_storage_id, NOT ON pgcolumnar.storage. A relation never written
        # to has NO storage catalog row at all, while get_storage_id still returns its
        # id, because that comes from the relation's metapage. Joining through
        # pgcolumnar.storage returns nothing and the premise fails for a reason that has
        # nothing to do with projections.
        cur.execute(
            "SELECT (p.proj_storage_id <> 0)::text || '/' || count(rg.*)::text "
            "FROM pgcolumnar.projection p "
            "LEFT JOIN pgcolumnar.row_group rg ON rg.storage_id = p.proj_storage_id "
            "WHERE p.storage_id = pgcolumnar.get_storage_id('pciot_e') "
            "AND p.name = 'onck' AND p.projection_id > 0 "
            "GROUP BY p.proj_storage_id"
        )
        row = cur.fetchone()
    expect.text(
        row[0] if row else "no projection row",
        "true/0",
        "premise: the empty projection is found and its storage holds no row groups",
    )

    node = _custom_scan(_plan(pgc_conn, "SELECT ck FROM pciot_e WHERE ck BETWEEN 1 AND 50", True))
    expect.text(
        (node or {}).get("Columnar Projection") or "none",
        "onck",
        "an empty covering projection is still priced as one page, so it is chosen",
    )
    # THE OTHER DIRECTION (#1339). The arm above catches over-pricing only: priced at
    # 0 pages instead of 1, the projection is cheaper still and stays chosen. `_plan`
    # sets every CPU cost to 0, so the scan's whole cost is its page cost, and a total
    # of zero means it was priced at zero pages. Measured on PG17: 5.0 with the clamp,
    # 0.0 with `if (pages < 1) pages = 1;` deleted, and the projection chosen both times.
    cost = (node or {}).get("Total Cost")
    expect.text(
        "unread" if cost is None else ("priced" if cost > 0 else f"free at {cost}"),
        "priced",
        "and its scan is not priced at zero pages",
    )
