"""A parallel custom scan must not divide I/O by the worker count.

The partial path prices itself as serial_startup + (serial_run / workers).
Core seqscan divides CPU only and leaves disk I/O whole. Dividing the whole
run quotes an I/O-dominated scan at 1/N of its serial cost.

This file asserts the PLANNER ratio, not a runtime. Independent of
test/parallel_scan_cost.sh: same public seam (EXPLAIN of a columnar scan), own
fixture, own observations. Assertion names match the shell suite so the two
can be compared by name, not by importing each other.
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


def _plan(conn, workers):
    with conn.cursor() as cur:
        cur.execute(f"SET max_parallel_workers_per_gather = {workers}")
        cur.execute("EXPLAIN (FORMAT JSON, COSTS ON) SELECT id, v, t FROM pcost")
        return cur.fetchone()[0]


def test_parallel_scan_cost(pgc_conn, expect):
    n = 90000
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE pcost (id int, v int, t text) USING pgcolumnar")
        cur.execute(
            f"INSERT INTO pcost SELECT g, g, md5(g::text) FROM generate_series(1, {n}) g"
        )
        cur.execute("ALTER TABLE pcost SET (parallel_workers = 2)")
        cur.execute("ANALYZE pcost")
        cur.execute("SELECT count(*) FROM pcost")
        expect.num(cur.fetchone()[0], n, "premise: the table holds every inserted row")

        # Raise page cost so I/O dominates the serial run. CPU terms stay at
        # their defaults; the shell twin zeros those instead. Either way the
        # serial run is mostly pages, and dividing that whole run by 2 is the
        # defect.
        cur.execute("SET seq_page_cost = 1000")
        cur.execute("SET parallel_setup_cost = 0")
        cur.execute("SET parallel_tuple_cost = 0")
        cur.execute("SET parallel_leader_participation = off")
        cur.execute("SET min_parallel_table_scan_size = 0")
        cur.execute("SET jit = off")
        cur.execute("SET pgcolumnar.enable_ungrouped_vector_agg = off")
        cur.execute("SET pgcolumnar.enable_group_vectorization = off")

    serial = _plan(pgc_conn, 0)
    parallel = _plan(pgc_conn, 2)

    snode = _custom_scan(serial)
    pnode = _custom_scan(parallel)
    expect.text(
        "Custom Scan" if snode else "none",
        "Custom Scan",
        "premise: the serial plan is a columnar scan",
    )
    expect.text(
        "none" if _gather(serial) is None else "Gather",
        "none",
        "premise: the serial plan has no Gather",
    )
    expect.text(
        "Gather" if _gather(parallel) is not None else "none",
        "Gather",
        "premise: the parallel plan has Gather",
    )
    workers = (_gather(parallel) or {}).get("Workers Planned")
    expect.num(workers, 2, "premise: the parallel plan uses two workers")
    expect.text(
        "Custom Scan" if pnode else "none",
        "Custom Scan",
        "premise: the parallel plan is a columnar scan",
    )

    s_run = snode["Total Cost"] - snode["Startup Cost"]
    p_run = pnode["Total Cost"] - pnode["Startup Cost"]
    expect.text(
        "yes" if s_run > 0 and p_run > 0 else "no",
        "yes",
        "premise: both scans have a positive run cost",
    )

    ratio = s_run / p_run
    print(f"-- serial run={s_run} parallel run={p_run} ratio={ratio:.3f}")
    expect.text(
        "io-kept" if ratio < 1.35 else "halved",
        "io-kept",
        "an I/O-dominated parallel scan is not priced at serial/workers",
    )
