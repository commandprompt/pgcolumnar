"""min/max over timestamptz are answered from zone maps and read no data (#1293).

"What is the latest reading?" -- `max(ts)` -- is the time-series question. An
ungrouped, unfiltered `min`/`max` is answered from the row groups' zone maps
(Phase D5b), and a group with deletes is decoded rather than folded (#149).

THE ARM THIS FILE EXISTS FOR IS THE BLOCK COUNT. Proving the metadata node is
chosen and that the answer matches heap does not show the data went unread: a
build that kept the node and decoded every group anyway returns the same answer.
Measured on the shell twin with every group forced dirty: max(ts) read 414 blocks
against a decode's 403, and every parity arm stayed green. So each arm reads the
table's own blocks from pg_statio against a control that must decode the column.

The pytest twin of `test/native_agg.sh`, every check by the shell's name. The
timestamptz arms came first (#1293); the int4, text, sum/avg, parallel-plan,
zone-map-read, fallback and empty-table arms were ported for #1341. Independent of
the shell twin: its own tables, row counts, key spacing and delete windows.

`pg_stat_reset()` is database-wide; this file must not run concurrently with
another that reads statistics.
"""

ROWS = 300000


def _one(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    return row[0] if row else None


def _blocks(conn, table, sql):
    """Blocks of `table` read while `sql` ran.

    FLUSH BEFORE THE RESET: this connection's own pending statistics would
    otherwise land on top of the reading (test_catalog_delete_index.py).
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_stat_force_next_flush()")
        cur.execute("SELECT pg_stat_reset()")
        cur.execute(sql)
        cur.fetchall()
        cur.execute("SELECT pg_stat_force_next_flush()")
        cur.execute(
            "SELECT coalesce(heap_blks_read,0) + coalesce(heap_blks_hit,0)"
            " FROM pg_statio_user_tables WHERE relname = %s"
            " AND schemaname = current_schema()",
            (table,),
        )
        row = cur.fetchone()
    return int(row[0]) if row else None


def test_min_max_over_timestamptz_read_no_data(pgc_conn, expect):
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE readings (sensor bigint, at timestamptz) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('readings', stripe_row_limit => 2000)")
        # bigint arithmetic: the shell twin's first draft overflowed int here, the
        # INSERT failed silently, and every arm measured an empty table.
        # THE HEAP IS WRITTEN FROM THE GENERATOR, NOT COPIED (#1339). It was a CTAS
        # from `readings`, so a write path that stored every timestamp shifted, in
        # the data and the zone map alike, put the same shift on both sides and every
        # parity arm agreed with it.
        gen = ("SELECT g % 37, '2025-11-15'::timestamptz"
               f" + ((g::bigint * 104729) % {ROWS}) * interval '2 min'"
               f" FROM generate_series(1, {ROWS}) g")
        cur.execute(f"INSERT INTO readings {gen}")
        cur.execute("CREATE TABLE readings_heap (sensor bigint, at timestamptz) USING heap")
        cur.execute(f"INSERT INTO readings_heap {gen}")

    expect.num(_one(pgc_conn, "SELECT count(*) FROM readings"), ROWS,
               "premise: the timestamptz table holds every row inserted")

    with pgc_conn.cursor() as cur:
        cur.execute("EXPLAIN SELECT max(at) FROM readings")
        plan = "\n".join(r[0] for r in cur.fetchall())
    expect.text("yes" if "Columnar Vectorized Aggregates" in plan else "no", "yes",
                "max(ts) over timestamptz plans the metadata agg node")

    decode = _blocks(pgc_conn, "readings", "SELECT max(at + interval '0 s') FROM readings")
    mx = _blocks(pgc_conn, "readings", "SELECT max(at) FROM readings")
    mn = _blocks(pgc_conn, "readings", "SELECT min(at) FROM readings")
    print(f"-- readings blocks: decode={decode} max={mx} min={mn}")

    expect.at_least(decode, 10, "control: decoding ts reads the table's blocks")
    # The margin is decode - 10 * reading: positive passes, and a failure
    # reports by how much it fell short.
    expect.at_least(decode - 10 * mx, 1,
                    "max(ts) reads under a tenth of the blocks a decode of ts reads")
    expect.at_least(decode - 10 * mn, 1,
                    "min(ts) reads under a tenth of the blocks a decode of ts reads")

    def both(sql):
        return (str(_one(pgc_conn, sql.format(t="readings"))),
                str(_one(pgc_conn, sql.format(t="readings_heap"))))

    got, want = both("SELECT max(at) FROM {t}")
    expect.text(got, want, "max(timestamptz) parity")
    got, want = both("SELECT min(at) FROM {t}")
    expect.text(got, want, "min(timestamptz) parity")

    # The newest and oldest readings deleted: a zone map still records them.
    with pgc_conn.cursor() as cur:
        for t in ("readings", "readings_heap"):
            cur.execute(
                f"DELETE FROM {t} WHERE at > (SELECT max(at) FROM readings_heap) - interval '1 hour'"
                f" OR at < (SELECT min(at) FROM readings_heap) + interval '45 min'"
            )
    n, nh = (_one(pgc_conn, "SELECT count(*) FROM readings"),
             _one(pgc_conn, "SELECT count(*) FROM readings_heap"))
    expect.text(f"{n < ROWS}|{n}", f"True|{nh}",
                "the deletes removed rows at both ends, the same rows from each")
    got, want = both("SELECT max(at) FROM {t}")
    expect.text(got, want, "max(timestamptz) parity after deleting the newest rows")
    got, want = both("SELECT min(at) FROM {t}")
    expect.text(got, want, "min(timestamptz) parity after deleting the oldest rows")


GATED = "SELECT count(*) || '/' || coalesce(min(id)::text, '-') || '/' || coalesce(max(id)::text, '-') FROM"


def test_a_where_with_no_column_in_it_is_applied(pgc_conn, expect):
    """A WHERE that references no column is a gating qual: planned as a one-time
    filter, not part of the per-row quals. The ungrouped vectorized aggregate
    answered from the zone maps alone and ignored it, so a false gate returned every
    row's aggregates (#1435). Each arm is compared with heap."""
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE gn (id int) USING pgcolumnar")
        cur.execute("CREATE TABLE gh (id int) USING heap")
        cur.execute("SELECT pgcolumnar.set_options('gn', stripe_row_limit => 1000)")
        cur.execute("INSERT INTO gn SELECT g FROM generate_series(1, 5000) g")
        cur.execute("INSERT INTO gh SELECT g FROM generate_series(1, 5000) g")
        cur.execute("EXPLAIN (COSTS OFF) SELECT count(*), min(id), max(id) FROM gn")
        plan = "\n".join(r[0] for r in cur.fetchall())
    expect.text("yes" if "Columnar Vectorized Aggregates" in plan else f"no ({plan})", "yes",
                "premise: count, min and max with no WHERE are the vectorized aggregate")
    # WRITTEN OUT, so each name is the shell's literal rather than a built string
    for where, name in (
            ("(SELECT false)", "a WHERE (SELECT false) with no column in it gives heap's answer"),
            ("now() < '2000-01-01'", "a WHERE on now() with no column in it gives heap's answer"),
            ("current_user = 'pgc_nobody'",
             "a WHERE on current_user with no column in it gives heap's answer")):
        expect.text(_one(pgc_conn, f"{GATED} gn WHERE {where}"),
                    _one(pgc_conn, f"{GATED} gh WHERE {where}"), name)
    expect.text(_one(pgc_conn, f"{GATED} gh WHERE (SELECT false)"), "0/-/-",
                "premise: heap's answer to a false gate is no rows")


# ---- the int4, text, parallel, zone-map and fallback arms (#1341) ------------------

def _plan(conn, sql, parallel=False):
    with conn.cursor() as cur:
        if parallel:
            # parallel_tuple_cost keeps its default on purpose: zeroing it makes a
            # Gather of raw rows look free, and the wrong costing then still picks
            # the right plan (the shell's comment, measured there)
            cur.execute("SET max_parallel_workers_per_gather = 4")
            cur.execute("SET parallel_setup_cost = 0")
            cur.execute("SET min_parallel_table_scan_size = 0")
        cur.execute("EXPLAIN (COSTS OFF) " + sql)
        plan = "\n".join(r[0] for r in cur.fetchall())
        if parallel:
            for g in ("max_parallel_workers_per_gather", "parallel_setup_cost",
                      "min_parallel_table_scan_size"):
                cur.execute(f"RESET {g}")
    return plan


def _aggnode(plan):
    return "yes" if "Columnar Vectorized Aggregates" in plan else "no"


def _gather(plan):
    return "yes" if "Gather" in plan else "no"


def _zm_scans(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT pg_stat_force_next_flush()")
        cur.execute("SELECT coalesce(idx_scan, 0) + coalesce(seq_scan, 0) FROM pg_stat_all_tables "
                    "WHERE relid = 'pgcolumnar.zone_map'::regclass")
        return int(cur.fetchone()[0])


def test_the_metadata_aggregates_answer_as_heap_and_win_their_plans(pgc_conn, expect):
    c = pgc_conn
    with c.cursor() as cur:
        cur.execute("CREATE TABLE ah (id int, s int, k bigint, label text)")
        cur.execute("CREATE TABLE an (id int, s int, k bigint, label text) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('an', stripe_row_limit => 2048, "
                    "chunk_group_row_limit => 1024)")
        gen = ("SELECT g, CASE WHEN g % 5 = 0 THEN NULL ELSE g % 997 END, (g * 11)::bigint, "
               "'tag-' || (g % 37) FROM generate_series(1, 12000) g")
        cur.execute(f"INSERT INTO ah {gen}")
        cur.execute(f"INSERT INTO an {gen}")

    # the node first: every parity arm below also holds for a correct fallback
    expect.text(_aggnode(_plan(c, "SELECT count(*) FROM an")), "yes",
                "count(*) uses the metadata agg node")
    expect.text(_aggnode(_plan(c, "SELECT sum(id), min(id), max(id) FROM an")), "yes",
                "sum/min/max uses the metadata agg node")

    expect.text(str(_one(c, "SELECT count(*) FROM an")), str(_one(c, "SELECT count(*) FROM ah")), "count(*)")
    expect.text(str(_one(c, "SELECT count(s) FROM an")), str(_one(c, "SELECT count(s) FROM ah")),
                "count(col,nulls)")
    expect.text(str(_one(c, "SELECT sum(id) FROM an")), str(_one(c, "SELECT sum(id) FROM ah")), "sum(int4)")
    expect.text(str(_one(c, "SELECT sum(s) FROM an")), str(_one(c, "SELECT sum(s) FROM ah")),
                "sum(int4,nulls)")
    expect.text(str(_one(c, "SELECT avg(s) FROM an")), str(_one(c, "SELECT avg(s) FROM ah")),
                "avg(int4,nulls)")
    expect.text(str(_one(c, "SELECT min(id) FROM an")), str(_one(c, "SELECT min(id) FROM ah")), "min(int4)")
    expect.text(str(_one(c, "SELECT max(id) FROM an")), str(_one(c, "SELECT max(id) FROM ah")), "max(int4)")
    expect.text(str(_one(c, "SELECT min(s) FROM an")), str(_one(c, "SELECT min(s) FROM ah")),
                "min(int4,nulls)")
    expect.text(str(_one(c, "SELECT min(label) FROM an")), str(_one(c, "SELECT min(label) FROM ah")),
                "min(text)")
    expect.text(str(_one(c, "SELECT max(label) FROM an")), str(_one(c, "SELECT max(label) FROM ah")),
                "max(text)")
    with c.cursor() as cur:
        cur.execute("SELECT count(*), sum(id), min(id), max(id) FROM an")
        got = cur.fetchone()
        cur.execute("SELECT count(*), sum(id), min(id), max(id) FROM ah")
        want = cur.fetchone()
    expect.text(str(got), str(want), "multi-agg")

    # it has to win on cost, not only exist (#133)
    par_count = _plan(c, "SELECT count(*) FROM an", parallel=True)
    expect.text(_aggnode(par_count), "yes", "count(*) beats a parallel plan")
    expect.text(_gather(par_count), "no", "count(*) plan has no Gather")
    par_multi = _plan(c, "SELECT count(*), sum(id), min(id), max(id) FROM an", parallel=True)
    expect.text(_aggnode(par_multi), "yes", "multi-agg beats a parallel plan")
    expect.text(_gather(par_multi), "no", "multi-agg plan has no Gather")

    # count(*) needs no zone map; an aggregate that needs one must be seen reading it,
    # so the zero above cannot pass because the counter never moves
    before = _zm_scans(c)
    _one(c, "SELECT count(*) FROM an")
    after = _zm_scans(c)
    expect.num(after - before, 0, "count(*) reads no zone maps")
    before = _zm_scans(c)
    _one(c, "SELECT count(*), sum(id) FROM an")
    after = _zm_scans(c)
    expect.text("yes" if after - before > 0 else f"no ({after - before})", "yes",
                "an aggregate that needs zone maps reads them")

    with c.cursor() as cur:
        cur.execute("SELECT count(*), sum(id) FROM an WHERE id > 6000")
        got = cur.fetchone()
        cur.execute("SELECT count(*), sum(id) FROM ah WHERE id > 6000")
        want = cur.fetchone()
    expect.text(str(got), str(want), "filtered aggregate parity")
    expect.text(_aggnode(_plan(c, "SELECT count(*), sum(id) FROM an WHERE id > 6000")), "no",
                "filtered aggregate not metadata node")
    expect.text(str(_one(c, "SELECT sum(k) FROM an")), str(_one(c, "SELECT sum(k) FROM ah")),
                "sum(bigint) fallback parity")

    with c.cursor() as cur:
        cur.execute("CREATE TABLE ae (id int, label text) USING pgcolumnar")
    expect.num(_one(c, "SELECT count(*) FROM ae"), 0, "empty count(*)")
    expect.text(str(_one(c, "SELECT sum(id) IS NULL FROM ae")), "True", "empty sum NULL")
    expect.text(str(_one(c, "SELECT min(label) IS NULL FROM ae")), "True", "empty min NULL")
