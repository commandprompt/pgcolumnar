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

A partial port of `test/native_agg.sh`: the timestamptz arms only. The int4,
text, sum/avg, parallel and fallback arms are not ported, and
`test_compare_to_bash.py` declares the pair INCOMPLETE with that reason.
Independent of the shell twin: its own table, row count, key spacing and
delete windows.

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
    for name, where in (("(SELECT false)", "(SELECT false)"), ("now()", "now() < '2000-01-01'"),
                        ("current_user", "current_user = 'pgc_nobody'")):
        expect.text(_one(pgc_conn, f"{GATED} gn WHERE {where}"), _one(pgc_conn, f"{GATED} gh WHERE {where}"),
                    f"a WHERE {'' if name.startswith('(') else 'on '}{name} with no column in it gives heap's answer")
    expect.text(_one(pgc_conn, f"{GATED} gh WHERE (SELECT false)"), "0/-/-",
                "premise: heap's answer to a false gate is no rows")
