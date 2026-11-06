"""The index-fetch penalty charges decode CPU by column WIDTH, not a column count.

`pgcolumnar_index_fetch_penalty` prices the row-group decode a per-row index fetch
forces. Its CPU term counted the decoded prefix's columns, so a 68-byte text column
was charged exactly what a 4-byte int4 column was. The consequence is not merely an
under-charge but an INVERTED ordering: the model let the wide table fetch about 3x
MORE rows than the narrow one before switching to a scan, when the wide table can
afford about 3x fewer (#803).

This file pins the ordering, which is the part that changes a plan, and reads it from
the PLAN rather than the clock -- so it is not a timing check and PGC_SKIP_TIMING
never applies to it. The absolute row counts are deliberately not asserted: they move
with the cost constants, and the defect is the direction.

TWO TABLES OF IDENTICAL SHAPE, DIFFERING ONLY IN COLUMN TYPE. `nproj` is the decoded
PREFIX length (`pgcolumnar_scan_decode_shape` sets `*nprefix = maxatt`, #363), so
width cannot be varied at a fixed prefix inside one table. Both prefixes are four
columns by construction.

Independent of test/index_fetch_penalty_width.sh: same public seam (EXPLAIN of an
index-forced range scan), own fixture, own observations. Assertion names match the
shell suite so the two can be compared by name, not by importing each other.

THE ROW-GROUP LIMIT IS A PER-TABLE OPTION HERE, NOT A CLUSTER SETTING. The shell twin
pins `pgcolumnar.stripe_row_limit=20000` in the cluster config, because the writing
and the planning session must not disagree about it (#806). A pytest cluster is
shared by every test in the session, so a cluster-wide setting for one file's benefit
is not available -- and is not needed: `set_options(stripe_row_limit => ...)` before
the write is durable, belongs to the table rather than to a session, and is what both
the writer and the planner read. The geometry is then read BACK from
`pgcolumnar.row_group` as a premise rather than assumed from the option.
"""

N = 400_000
R = 20_000
CAP = 32 * 1024 * 1024          # COLUMNAR_FETCH_CACHE_MAX_BYTES
# FOUR RUNGS BELOW THE SHELL SUITE'S FIRST ONE. Its ladder starts at 20, and the
# wide table's flip point is between 5 and 7 rows, so every rung it tries is already
# past it and `flip_rows` returns 0 -- which satisfies the ordering arm below for the
# wrong reason. Measured, penalty on: ifw_w is an Index Scan at k=1,2,5,10 and a
# Custom Scan from k=20. With these rungs FW is a measured 5 rather than a floor.
LADDER = (1, 2, 5, 10, 20, 40, 60, 80, 100, 150, 200, 300, 400, 600, 900)

SETS = (
    "SET enable_seqscan = off",
    "SET max_parallel_workers_per_gather = 0",
    "SET random_page_cost = 1.1",
)


def _nodes(plan):
    stack = [plan[0]["Plan"]]
    while stack:
        node = stack.pop(0)
        yield node
        stack.extend(node.get("Plans") or ())


def _plan(conn, sql, *, penalty=None):
    with conn.cursor() as cur:
        for s in SETS:
            cur.execute(s)
        if penalty is not None:
            cur.execute(f"SET pgcolumnar.enable_index_fetch_penalty = {penalty}")
        cur.execute(f"EXPLAIN (FORMAT JSON, COSTS OFF) {sql}")
        return cur.fetchone()[0]


def _fetches_by_index(plan):
    """True when the plan reads this table through a per-row index fetch.

    The shell twin greps the EXPLAIN text for `Index Scan`. Node Type equality on
    parsed JSON cannot be satisfied by a property line that happens to carry the
    words, which is a real way for a plan-shape check to lie.
    """
    return any(n["Node Type"] == "Index Scan" for n in _nodes(plan))


def _one(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        row = cur.fetchone()
    return row[0] if row else None


def _flip_rows(conn, table):
    """-> the largest fetched-row count at which `table` still plans an index fetch.

    A ladder rather than a bisection, as in the twin, so a failure prints where the
    plan turned over instead of only that it did.
    """
    last = 0
    for k in LADDER:
        plan = _plan(
            conn,
            f"SELECT c1,c2 FROM {table} WHERE scat BETWEEN 1 AND {k}",
            penalty="on",
        )
        if not _fetches_by_index(plan):
            break
        last = _one(conn, f"SELECT count(*) FROM {table} WHERE scat BETWEEN 1 AND {k}")
    return last


def test_index_fetch_penalty_width(pgc_conn, expect):
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE ifw_n (id int, scat int, c1 int,  c2 int)  "
                    "USING pgcolumnar")
        cur.execute("CREATE TABLE ifw_w (id int, scat int, c1 text, c2 text) "
                    "USING pgcolumnar")
        for t in ("ifw_n", "ifw_w"):
            cur.execute(f"SELECT pgcolumnar.set_options('{t}', stripe_row_limit => {R})")
        cur.execute(
            "INSERT INTO ifw_n SELECT g, (g * 2654435761::bigint % 1000000)::int, "
            f"g, g + 1 FROM generate_series(1,{N}) g"
        )
        cur.execute(
            "INSERT INTO ifw_w SELECT g, (g * 2654435761::bigint % 1000000)::int, "
            "repeat(md5(g::text),2), repeat(md5((g+1)::text),2) "
            f"FROM generate_series(1,{N}) g"
        )
        cur.execute("CREATE INDEX ifw_n_scat ON ifw_n(scat)")
        cur.execute("CREATE INDEX ifw_w_scat ON ifw_w(scat)")
        cur.execute("ANALYZE ifw_n")
        cur.execute("ANALYZE ifw_w")

    # ---- premises ---------------------------------------------------------
    for t in ("ifw_n", "ifw_w"):
        expect.num(
            _one(pgc_conn, f"SELECT count(*) FROM {t}"), N,
            f"premise: {t} holds all {N} rows",
        )

    groups = {}
    for t in ("ifw_n", "ifw_w"):
        groups[t] = _one(
            pgc_conn,
            "SELECT count(*) FROM pgcolumnar.storage s "
            "JOIN pgcolumnar.row_group rg USING (storage_id) "
            f"WHERE s.relation_oid = '{t}'::regclass",
        )
    gn, gw = groups["ifw_n"], groups["ifw_w"]
    print(f"-- row groups: ifw_n={gn} ifw_w={gw}")
    # THE GEOMETRY IS READ BACK, NOT ASSUMED FROM THE OPTION. More groups than the
    # fetch cache holds is the whole mechanism being priced: with few enough groups
    # the cache holds everything and the pathology does not occur at all.
    expect.text(
        f"yes ({gn})" if gn == gw and (gn or 0) > 4 else f"no (n={gn} w={gw})",
        f"yes ({gn})",
        "premise: both tables have the same row-group count, and more than the "
        "fetch cache holds",
    )

    wn = _one(pgc_conn,
              "SELECT sum(avg_width) FROM pg_stats WHERE tablename = 'ifw_n'")
    ww = _one(pgc_conn,
              "SELECT sum(avg_width) FROM pg_stats WHERE tablename = 'ifw_w'")
    print(f"-- summed avg_width: ifw_n={wn} ifw_w={ww}")
    expect.text(
        "wider" if wn and ww and ww >= 4 * wn else f"NOT WIDER (n={wn} w={ww})",
        "wider",
        "premise: the wide table's prefix really is wider, by a large factor",
    )
    expect.text(
        "under" if (wn or 0) * R < CAP and (ww or 0) * R < CAP
        else f"OVER (n={(wn or 0) * R} w={(ww or 0) * R} cap={CAP})",
        "under",
        "premise: both prefixes decode well under the fetch cache cap, so the "
        "overflow blend is inactive on both arms",
    )

    # THE SUBJECT MUST BE WHAT MOVES THE PLAN. Not a premise (#1339): pgcolumnar's own
    # costing, which the penalty switched off in the source reddens. If the penalty never moves one there
    # is no ordering to check and every arm below would pass vacuously.
    sql = "SELECT c1,c2 FROM ifw_w WHERE scat BETWEEN 1 AND 400"
    pen_off = _plan(pgc_conn, sql, penalty="off")
    pen_on = _plan(pgc_conn, sql, penalty="on")
    off_idx, on_idx = _fetches_by_index(pen_off), _fetches_by_index(pen_on)
    print(f"-- penalty off -> index fetch: {off_idx} ; on -> {on_idx}")
    expect.text(
        "yes" if off_idx and not on_idx else f"no (off={off_idx} on={on_idx})",
        "yes",
        "the penalty is what moves this plan (index without it, not with it)",
    )

    # ---- the check --------------------------------------------------------
    fn = _flip_rows(pgc_conn, "ifw_n")
    fw = _flip_rows(pgc_conn, "ifw_w")
    print(f"-- last row count still fetched by index: narrow prefix {wn}B = {fn} rows,"
          f"  wide prefix {ww}B = {fw} rows")

    expect.at_least(
        fn, 1, "the narrow table does fetch by index somewhere on the ladder"
    )
    # AND THE WIDE ONE DOES TOO, which is the arm's other side. `FW <= FN` is
    # satisfied by FW = 0, and 0 is what an OVER-charged width weight produces as
    # well as what "gives up immediately" produces -- so without this the check is
    # one-sided: it catches the #803 under-charge and reports `ordered` for any
    # over-charge severe enough to take the wide table off the index entirely.
    expect.at_least(
        fw, 1, "and the wide table fetches by index somewhere on it too"
    )

    # A wider decoded prefix makes every row-group decode dearer, so the wide table
    # must abandon per-row fetches NO LATER than the narrow one. Priced by column
    # COUNT the two arms charge the same decode CPU and the wide table's larger page
    # term pushes its flip point HIGHER, which is the inversion #803 reports.
    expect.text(
        "ordered" if (fw or 0) <= (fn or 0)
        else f"INVERTED (wide {ww}B fetches to {fw} rows, narrow {wn}B only to {fn})",
        "ordered",
        "a wider decoded prefix gives up on per-row fetches no later than a narrow "
        "one (#803)",
    )
