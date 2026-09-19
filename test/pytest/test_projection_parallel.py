"""A covering projection scan must be able to run in parallel.

PgColumnarSetRelPathlist offers a covering projection as a serial CustomPath
(parallel_aware = false, parallel_safe = false) and a parallel base scan as a
partial path with no projection name. Those cannot both be true of one plan:
either Gather wins and the projection is dropped, or the serial projection
wins and the workers are dropped.

Gather in the plan is not enough. A partial path that no worker claims a
stripe from still looks parallel, and a single claimer still returns the
covering rows once. The load-bearing arms are EXPLAIN ANALYZE: two workers
launched, and both produced rows. Same public seam parallel_am_scan already
uses for the table-AM claimer.

This file asserts the PLANNER shape, ANALYZE worker rows, and the query's
count. Independent of test/projection_parallel.sh: same public seam, own
fixture, own observations. Assertion names match the shell suite so the
two can be compared by name, not by importing each other.
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


def _gather(plan):
    for node in _nodes(plan):
        if node.get("Node Type") == "Gather":
            return node
    return None


def _shape(plan):
    has_g = _gather(plan) is not None
    node = _custom_scan(plan)
    has_p = bool(node and node.get("Columnar Projection"))
    if has_g and has_p:
        return "gather+projection"
    if has_g:
        return "gather-only"
    if has_p:
        return "projection-only"
    return "neither"


def _worker_rows(plan):
    rows = []
    scan = _custom_scan(plan)
    if scan is None:
        return rows
    for worker in scan.get("Workers") or ():
        if "Actual Rows" in worker:
            rows.append(worker["Actual Rows"])
    return rows


def _apply_parallel(cur):
    cur.execute("SET parallel_setup_cost = 0")
    cur.execute("SET parallel_tuple_cost = 0")
    cur.execute("SET parallel_leader_participation = off")
    cur.execute("SET min_parallel_table_scan_size = 0")
    cur.execute("SET jit = off")
    cur.execute("SET pgcolumnar.enable_ungrouped_vector_agg = off")
    cur.execute("SET pgcolumnar.enable_group_vectorization = off")


def _plan(conn, sql, workers, projection_scan, analyze=False):
    opts = (
        "ANALYZE, VERBOSE, TIMING OFF, SUMMARY OFF, "
        if analyze
        else ""
    )
    with conn.cursor() as cur:
        _apply_parallel(cur)
        cur.execute(f"SET max_parallel_workers_per_gather = {workers}")
        cur.execute(
            "SET pgcolumnar.enable_projection_scan = "
            + ("on" if projection_scan else "off")
        )
        cur.execute(f"EXPLAIN ({opts}FORMAT JSON, COSTS OFF) " + sql)
        return cur.fetchone()[0]


def _count(conn, sql, workers, projection_scan):
    with conn.cursor() as cur:
        _apply_parallel(cur)
        cur.execute(f"SET max_parallel_workers_per_gather = {workers}")
        cur.execute(
            "SET pgcolumnar.enable_projection_scan = "
            + ("on" if projection_scan else "off")
        )
        cur.execute(sql)
        return cur.fetchone()[0]


def test_projection_parallel(pgc_conn, expect):
    n = 50000
    lo = 200
    hi = 12299
    want = hi - lo + 1
    with pgc_conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE pcvgath (skey int, payload int, filler text) "
            "USING pgcolumnar"
        )
        # The covering projection is stored sorted on skey, so the
        # BETWEEN range occupies consecutive groups. 400 matching rows
        # at the 100-row floor is four groups. 12000 matching rows is
        # 120 groups, plus 8x md5 decode work -- different geometry
        # from the shell twin, same reason: a short range finishes
        # in one worker before the other claims.
        cur.execute(
            "SELECT pgcolumnar.set_options('pcvgath', stripe_row_limit => 2000, "
            "chunk_group_row_limit => 100)"
        )
        # Include filler so each claimed group has real decode work.
        # A two-int projection can finish before the second worker claims.
        cur.execute(
            "SELECT pgcolumnar.add_projection('pcvgath', 'onskey', "
            "ARRAY['skey','payload','filler'], ARRAY['skey'])"
        )
        # Scrambled insert, different N / stripe / hash salt from the shell twin.
        cur.execute(
            f"INSERT INTO pcvgath SELECT g, g % 23, repeat(md5((g + 9)::text), 8) "
            f"FROM generate_series(1, {n}) g ORDER BY md5((g + 9)::text)"
        )
        # Two workers, matching the shell twin's claimer arms and
        # parallel_am_scan. Three workers on this fixture can leave one idle.
        cur.execute("ALTER TABLE pcvgath SET (parallel_workers = 2)")
        cur.execute("ANALYZE pcvgath")
        cur.execute("SELECT count(*) FROM pcvgath")
        expect.num(cur.fetchone()[0], n, "premise: the table holds every inserted row")
        cur.execute(
            "SELECT count(*) FROM pgcolumnar.projection_declaration "
            "WHERE rel = 'pcvgath'::regclass AND name = 'onskey'"
        )
        expect.num(cur.fetchone()[0], 1, "premise: a covering projection exists")

    sql = (
        f"SELECT skey, payload, filler FROM pcvgath "
        f"WHERE skey BETWEEN {lo} AND {hi}"
    )
    count_sql = (
        f"SELECT count(*) FROM pcvgath WHERE skey BETWEEN {lo} AND {hi}"
    )

    serial = _plan(pgc_conn, sql, 0, True)
    par_off = _plan(pgc_conn, sql, 2, False)
    par_on = _plan(pgc_conn, sql, 2, True)
    par_on_ana = _plan(pgc_conn, sql, 2, True, analyze=True)
    par_on_count = _count(pgc_conn, count_sql, 2, True)

    worker_rows = _worker_rows(par_on_ana)
    n_busy = sum(1 for r in worker_rows if r and r > 0)
    launched = (_gather(par_on_ana) or {}).get("Workers Launched")

    print("-- serial:", _shape(serial))
    print("-- parallel, projection off:", _shape(par_off))
    print("-- parallel, projection on:", _shape(par_on))
    print(f"-- worker rows {worker_rows} busy={n_busy} launched={launched}")
    print(f"-- parallel covering count={par_on_count} want={want}")

    expect.text(
        _shape(serial),
        "projection-only",
        "premise: a serial covering query uses the projection",
    )
    expect.text(
        _shape(par_off),
        "gather-only",
        "premise: a parallel base scan is available when the projection is off",
    )
    expect.text(
        _shape(par_on),
        "gather+projection",
        "a covering projection can be a parallel scan",
    )
    expect.num(
        par_on_count,
        want,
        "a parallel covering projection returns the covering rows once",
    )
    expect.num(
        launched,
        2,
        "premise: EXPLAIN ANALYZE launched two workers",
    )
    expect.num(
        len(worker_rows),
        2,
        "premise: ANALYZE printed a rows= line per launched worker",
    )
    expect.num(
        n_busy,
        2,
        "workers share the covering projection scan, it is not a single claimer",
    )
