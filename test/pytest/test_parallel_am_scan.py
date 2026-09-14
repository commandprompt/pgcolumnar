"""A table-AM parallel scan must share work across workers.

With the custom scan off, Parallel Seq Scan goes through the AM. The AM
used to treat phs_nallocated as a first-wins flag: one backend claimed
the whole scan and the others marked themselves exhausted. Workers
launched, one backend read.

Independent of test/parallel_am_scan.sh: same public seam (EXPLAIN ANALYZE
of a Parallel Seq Scan), own fixture, own observations. Assertion names
match the shell suite so the two can be compared by name, not by importing
each other. Leader participation is off so the two launched workers are
the claimers under test. Many small row groups keep both workers busy
before either finishes the table.
"""


def _nodes(plan):
    stack = [plan[0]["Plan"]]
    while stack:
        node = stack.pop(0)
        yield node
        stack.extend(node.get("Plans") or ())


def _first(plan, node_type):
    for node in _nodes(plan):
        if node.get("Node Type") == node_type:
            return node
    return None


def _worker_rows(plan):
    rows = []
    scan = _first(plan, "Seq Scan")
    if scan is None:
        return rows
    for worker in scan.get("Workers") or ():
        if "Actual Rows" in worker:
            rows.append(worker["Actual Rows"])
    return rows


def _plan(conn, workers, analyze=False):
    opts = "ANALYZE, VERBOSE, TIMING OFF, SUMMARY OFF, " if analyze else "VERBOSE, "
    with conn.cursor() as cur:
        cur.execute(f"SET max_parallel_workers_per_gather = {workers}")
        cur.execute(
            f"EXPLAIN ({opts}FORMAT JSON, COSTS OFF) SELECT id FROM ampar"
        )
        return cur.fetchone()[0]


def test_parallel_am_scan(pgc_conn, expect):
    n = 80000
    with pgc_conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE ampar (id int, k int, payload text) USING pgcolumnar"
        )
        cur.execute(
            "SELECT pgcolumnar.set_options('ampar', "
            "chunk_group_row_limit => 200, stripe_row_limit => 1000)"
        )
        cur.execute(
            f"INSERT INTO ampar SELECT g, g, md5(g::text) "
            f"FROM generate_series(1, {n}) g"
        )
        cur.execute("ALTER TABLE ampar SET (parallel_workers = 2)")
        cur.execute("ANALYZE ampar")
        cur.execute("SELECT count(*) FROM ampar")
        expect.num(cur.fetchone()[0], n, "premise: the table holds every inserted row")

        cur.execute("SET pgcolumnar.enable_custom_scan = off")
        cur.execute("SET parallel_setup_cost = 0")
        cur.execute("SET parallel_tuple_cost = 0")
        cur.execute("SET min_parallel_table_scan_size = 0")
        cur.execute("SET jit = off")
        cur.execute("SET parallel_leader_participation = off")

    serial = _plan(pgc_conn, 0)
    parallel = _plan(pgc_conn, 2)
    analyzed = _plan(pgc_conn, 2, analyze=True)

    expect.text(
        "Seq Scan" if _first(serial, "Seq Scan") else "none",
        "Seq Scan",
        "premise: with the custom scan off the serial plan is a Seq Scan",
    )
    expect.text(
        "none" if _first(serial, "Custom Scan") is None else "Custom Scan",
        "none",
        "premise: the serial plan is not a columnar custom scan",
    )
    expect.text(
        "Gather" if _first(parallel, "Gather") is not None else "none",
        "Gather",
        "premise: the parallel plan has Gather",
    )
    expect.num(
        (_first(parallel, "Gather") or {}).get("Workers Planned"),
        2,
        "premise: the parallel plan uses two workers",
    )
    expect.text(
        "Seq Scan" if _first(analyzed, "Seq Scan") else "none",
        "Seq Scan",
        "premise: the parallel plan is still a Seq Scan, not a custom scan",
    )
    expect.num(
        (_first(analyzed, "Gather") or {}).get("Workers Launched"),
        2,
        "premise: EXPLAIN ANALYZE launched two workers",
    )

    with pgc_conn.cursor() as cur:
        cur.execute("SET max_parallel_workers_per_gather = 0")
        cur.execute("SELECT count(*) FROM ampar")
        serial_cnt = cur.fetchone()[0]
        cur.execute("SET max_parallel_workers_per_gather = 2")
        cur.execute("SELECT count(*) FROM ampar")
        par_cnt = cur.fetchone()[0]
    expect.num(
        par_cnt,
        serial_cnt,
        "a parallel table-AM scan returns the same row count as serial",
    )

    worker_rows = _worker_rows(analyzed)
    expect.num(
        len(worker_rows),
        2,
        "premise: ANALYZE printed a rows= line per launched worker",
    )
    n_busy = sum(1 for r in worker_rows if r and r > 0)
    print(f"-- worker rows {worker_rows} busy={n_busy}")
    expect.num(
        n_busy,
        2,
        "workers share the table-AM scan, it is not a single claimer",
    )
