"""A deleted row is invisible through every access path, and a live one visible.

The delete-vector fold (OR the group's bitmaps into one mask) and the per-row bit test
are consulted by several independent paths: the columnar scan, the heap sequential scan,
the index fetch, the index-only scan, and the buffered read-your-writes path. The fold is
single-sourced in `pgcolumnar_merge_delete_vectors` and the bit test in `dv_row_deleted`,
so a deleted row must be invisible through all of them identically.

Independent of test/native_delete_visibility_paths.sh: same public seam (the live set as
each path reports it), own fixture, own observations. Assertion names match the shell
suite so the two can be compared by name, not by importing each other.

THE SHELL SUITE NAMES FOUR PATHS AND TAKES TWO. It sets `enable_seqscan=off` and
`enable_indexonlyscan=on` and trusts the names. Measured on this fixture, those settings
do not move the plan, because **none of the `enable_*` scan GUCs governs
`Custom Scan (PgColumnarScan)`**:

        arm as the shell suite runs it            plan actually taken
        sequential scan (row-emitting path)       Custom Scan
        index / bitmap scan                       Custom Scan
        index-only scan                           Custom Scan
        aggregate over the whole table            Custom Scan (vectorized aggregate)

Three arms named for three paths exercise one. The property they assert is true -- each
returns 4286 -- but three of the four are the same evidence counted three times.

`pgcolumnar.enable_custom_scan = off` is the switch that does move it, and with it each
named path is reachable and still correct:

        forced                                    plan            live set
        heap sequential                           Seq Scan            4286
        index / bitmap                            Index Scan          4286
        index-only                                Index Only Scan     4286

So this port forces the path each arm is named for and ASSERTS THE NODE before reading
the count. The shell suite now does the same, with the same premise names (#1339).
"""
import psycopg

ROWS = 5000
GROUP = 1000
LIVE = ROWS - ROWS // 7          # 5000 - 714
DELETED_ID, LIVE_ID = 700, 701


def _nodes(plan):
    stack = [plan[0]["Plan"]]
    while stack:
        node = stack.pop(0)
        yield node
        stack.extend(node.get("Plans") or ())


def _run(conn, schema, sets, sql):
    """-> (node types, the scalar the query returns), under `sets`.

    RESET ALL and then RESTORE THE SEARCH PATH. The fixture puts each test in a
    private schema by setting `search_path` on the connection, and RESET ALL is not
    selective -- it resets that too, and the next statement reports `relation "t"
    does not exist`. Resetting is still right: each arm must start from the same
    place or it inherits the previous arm's `enable_*` settings, which is how an arm
    ends up measuring a path it did not ask for.
    """
    with conn.cursor() as cur:
        cur.execute("RESET ALL")
        cur.execute(f'SET search_path TO "{schema}", public')
        for s in sets:
            cur.execute(s)
        cur.execute(f"EXPLAIN (FORMAT JSON, COSTS OFF) {sql}")
        types = [n["Node Type"] for n in _nodes(cur.fetchone()[0])]
        cur.execute(sql)
        return types, cur.fetchone()[0]


NO_COLUMNAR = "SET pgcolumnar.enable_custom_scan = off"


def test_native_delete_visibility_paths(pgc_conn, expect):
    with pgc_conn.cursor() as cur:
        cur.execute("SELECT current_schema()")
        schema = cur.fetchone()[0]
        cur.execute("CREATE TABLE t (id int, v int) USING pgcolumnar")
        cur.execute(f"SELECT pgcolumnar.set_options('t', stripe_row_limit => {GROUP})")
        cur.execute(f"INSERT INTO t SELECT g, g FROM generate_series(1,{ROWS}) g")
        cur.execute("CREATE UNIQUE INDEX t_id ON t (id)")
        cur.execute("SELECT count(*) FROM t")
        expect.num(cur.fetchone()[0], ROWS, "premise: the table holds all 5000 rows")
        cur.execute("DELETE FROM t WHERE id % 7 = 0")
        cur.execute(
            "SELECT count(*) FROM pgcolumnar.storage s "
            "JOIN pgcolumnar.row_group rg USING (storage_id) "
            "WHERE s.relation_oid = 't'::regclass")
        groups = cur.fetchone()[0]
    print(f"-- row groups: {groups}, live set should be {LIVE}")
    # EACH GROUP BUILDS ITS OWN MASK, so one group would test the fold of a single
    # bitmap. The shell suite's comment says five groups and nothing checks it; read
    # back from the catalog rather than assumed from the option that asked for it.
    expect.at_least(groups, 2, "premise: the deletes span more than one row group")

    # ---- the four whole-set paths --------------------------------------------
    types, got = _run(pgc_conn, schema, [NO_COLUMNAR, "SET enable_indexscan=off",
                                 "SET enable_bitmapscan=off",
                                 "SET enable_indexonlyscan=off"],
                      "SELECT count(*) FROM t WHERE v >= 0")
    print(f"-- sequential: {' > '.join(types)}")
    expect.contains(types, "Seq Scan", "premise: the sequential arm really plans a Seq Scan")
    expect.num(got, LIVE, "sequential scan sees the live set (row-emitting path)")

    types, got = _run(pgc_conn, schema, [NO_COLUMNAR, "SET enable_seqscan=off",
                                 "SET enable_indexonlyscan=off"],
                      "SELECT count(*) FROM t WHERE id >= 0")
    print(f"-- index/bitmap: {' > '.join(types)}")
    expect.contains(types, "Index Scan", "premise: the index arm really plans an Index Scan")
    expect.num(got, LIVE, "index / bitmap scan sees the live set")

    types, got = _run(pgc_conn, schema, [NO_COLUMNAR, "SET enable_seqscan=off",
                                 "SET enable_indexonlyscan=on"],
                      "SELECT count(id) FROM t WHERE id >= 0")
    print(f"-- index-only: {' > '.join(types)}")
    expect.contains(types, "Index Only Scan",
                    "premise: the index-only arm really plans an Index Only Scan")
    expect.num(got, LIVE, "index-only scan sees the live set")

    # The columnar path, left to the planner, which answers this from the vectorized
    # aggregate rather than by emitting rows -- a different consumer of the same fold.
    types, got = _run(pgc_conn, schema, [], "SELECT count(*) FROM t")
    print(f"-- whole-table aggregate: {' > '.join(types)}")
    expect.contains(types, "Custom Scan",
                    "premise: the whole-table aggregate stays on the columnar path")
    expect.num(got, LIVE, "aggregate over the whole table sees the live set")

    # ---- one row, both ways, through the index fetch --------------------------
    _types, got = _run(pgc_conn, schema, [NO_COLUMNAR, "SET enable_seqscan=off"],
                       f"SELECT count(*) FROM t WHERE id = {DELETED_ID}")
    expect.num(got, 0, "a deleted row is invisible via the index")
    _types, got = _run(pgc_conn, schema, [NO_COLUMNAR, "SET enable_seqscan=off"],
                       f"SELECT count(*) FROM t WHERE id = {LIVE_ID}")
    expect.num(got, 1, "a live row is visible via the index")

    # ---- the buffered path: read your own uncommitted delete ------------------
    with pgc_conn.cursor() as cur:
        cur.execute("RESET ALL")
        cur.execute(f'SET search_path TO "{schema}", public')
        cur.execute("BEGIN")
        cur.execute("DELETE FROM t WHERE id = 8")
        cur.execute("SELECT count(*) FROM t WHERE id = 8")
        in_txn = cur.fetchone()[0]
        cur.execute("ROLLBACK")
    expect.num(in_txn, 0,
               "a row deleted in-transaction is invisible in the same transaction")

    # AND THE ROLLBACK REALLY ROLLED BACK, so the arm above cannot be satisfied by a
    # delete that simply persisted.
    with pgc_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM t WHERE id = 8")
        expect.num(cur.fetchone()[0], 1,
                   "the rolled-back delete left the row in place")


def test_a_delete_whose_qual_deletes_another_row_matches_heap(pgc_conn, expect):
    """A DELETE whose qual deletes another row of the same table (#1394). The inner
    DELETE's scan start writes the outer statement's buffered marks, and its statement
    end writes its own mark to the same row group, both in one command. The second
    write updated a delete_vector row the first had written in that command, and
    failed with "attempted to update invisible tuple". Run on heap too; the columnar
    answer must be heap's."""
    with pgc_conn.cursor() as cur:
        cur.execute("""CREATE FUNCTION del_next(i int, t text) RETURNS int LANGUAGE plpgsql AS $$
BEGIN EXECUTE format('DELETE FROM %s WHERE id = %s', t, i + 1); RETURN 1; END $$""")
    got = {}
    for am in ("pgcolumnar", "heap"):
        with pgc_conn.cursor() as cur:
            cur.execute(f"CREATE TABLE dn_{am} (id int, v int) USING {am}")
            cur.execute(f"INSERT INTO dn_{am} SELECT g, 0 FROM generate_series(1, 20) g")
            try:
                cur.execute(f"DELETE FROM dn_{am} WHERE id % 2 = 0 AND del_next(id, 'dn_{am}') IS NOT NULL")
                state = "ok"
            except psycopg.Error as e:
                state = e.sqlstate or "?"
            cur.execute(f"SELECT count(*) || '|' || coalesce(sum(id), 0) FROM dn_{am}")
            got[am] = f"{state}|{cur.fetchone()[0]}"
    expect.text(got["pgcolumnar"], got["heap"],
                "a DELETE whose qual deletes another row of the table succeeds and leaves heap's rows (#1394)")
