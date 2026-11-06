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

ONE CHECK PER TEST (#1339). Three module fixtures, one per data set, take every
reading in the order the arms need it -- the deletes, the zone-map counter deltas and
the parallel plans happen in the fixture, never inside a test -- and each test asserts
one reading. A red then cannot stop the next check from running, so the twin reports
the same red set as the shell (#1514's review, item 4).

`pg_stat_reset()` is database-wide; this file must not run concurrently with
another that reads statistics.
"""

import psycopg
import pytest

from pgc_vacuity import query_error

ROWS = 300000


def _one(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    return row[0] if row else None


def _try(fn, *args, **kw):
    """A reading, or the error that replaced it, so a failed step reddens its own
    check rather than erroring the fixture and every check with it. EVERY reading in
    a fixture goes through this (#1522 review): one bare call that raises takes down
    every test on that fixture as an unnamed ERROR, which is the hiding this file's
    one-check-per-test shape exists to prevent."""
    try:
        return fn(*args, **kw)
    except psycopg.Error as exc:
        return query_error(exc.diag.message_primary)


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


def _int(v, what=""):
    """A count, or 0 with the error printed, so a bound it was meant to meet fails by
    name and the reason is in the output rather than lost (#1522 review)."""
    if isinstance(v, int):
        return v
    print(f"-- {what or 'reading'}: not a count: {v}")
    return 0


def _num(v, what=""):
    """A count for expect.num, or -1 with the error printed. expect.num refuses a
    non-number with a VacuityError rather than a named red (#1522 review)."""
    return v if isinstance(v, int) else _int(v, what) - 1


def _explain(c, sql):
    return "\n".join(r[0] for r in c.execute(sql))


def _schema_conn(pgc_cluster, schema):
    """A connection whose private schema is first on the search_path, as pgc_conn
    gives a test, but for a module fixture."""
    conn = psycopg.connect(pgc_cluster.dsn(), autocommit=True)
    conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    conn.execute(f"CREATE SCHEMA {schema}")
    conn.execute(f"SET search_path = {schema}, public")
    return conn


def _drop(conn, schema):
    try:
        conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    finally:
        conn.close()


# ---- min/max over timestamptz read no data (#1293) ---------------------------------

@pytest.fixture(scope="module")
def ts(pgc_cluster):
    c = _schema_conn(pgc_cluster, "pgc_agg_ts")
    got = {}
    try:
        c.execute("CREATE TABLE readings (sensor bigint, at timestamptz) USING pgcolumnar")
        c.execute("SELECT pgcolumnar.set_options('readings', stripe_row_limit => 2000)")
        # bigint arithmetic: the shell twin's first draft overflowed int here, the
        # INSERT failed silently, and every arm measured an empty table.
        # THE HEAP IS WRITTEN FROM THE GENERATOR, NOT COPIED (#1339). It was a CTAS
        # from `readings`, so a write path that stored every timestamp shifted, in
        # the data and the zone map alike, put the same shift on both sides and every
        # parity arm agreed with it.
        gen = ("SELECT g % 37, '2025-11-15'::timestamptz"
               f" + ((g::bigint * 104729) % {ROWS}) * interval '2 min'"
               f" FROM generate_series(1, {ROWS}) g")
        # the INSERTs too: the shell's psql_run carries on after a failed write and names
        # its reds, so a write-path mutant must not error the fixture here
        _try(c.execute, f"INSERT INTO readings {gen}")
        c.execute("CREATE TABLE readings_heap (sensor bigint, at timestamptz) USING heap")
        _try(c.execute, f"INSERT INTO readings_heap {gen}")
        got["count"] = _try(_one, c, "SELECT count(*) FROM readings")
        got["plan"] = str(_try(_explain, c, "EXPLAIN SELECT max(at) FROM readings"))
        got["decode"] = _try(_blocks, c, "readings", "SELECT max(at + interval '0 s') FROM readings")
        got["max"] = _try(_blocks, c, "readings", "SELECT max(at) FROM readings")
        got["min"] = _try(_blocks, c, "readings", "SELECT min(at) FROM readings")
        print(f"-- readings blocks: decode={got['decode']} max={got['max']} min={got['min']}")

        def both(sql):
            return (str(_try(_one, c, sql.format(t="readings"))),
                    str(_try(_one, c, sql.format(t="readings_heap"))))
        got["max parity"] = both("SELECT max(at) FROM {t}")
        got["min parity"] = both("SELECT min(at) FROM {t}")
        # The newest and oldest readings deleted: a zone map still records them.
        for t in ("readings", "readings_heap"):
            _try(c.execute, f"DELETE FROM {t} WHERE at > (SELECT max(at) FROM readings_heap) - interval '1 hour'"
                            f" OR at < (SELECT min(at) FROM readings_heap) + interval '45 min'")
        got["after delete"] = (_try(_one, c, "SELECT count(*) FROM readings"),
                               _try(_one, c, "SELECT count(*) FROM readings_heap"))
        got["max parity after"] = both("SELECT max(at) FROM {t}")
        got["min parity after"] = both("SELECT min(at) FROM {t}")
        yield got
    finally:
        _drop(c, "pgc_agg_ts")


def test_the_timestamptz_table_holds_every_row(ts, expect):
    expect.num(_num(ts["count"], "count"), ROWS, "premise: the timestamptz table holds every row inserted")


def test_max_ts_plans_the_metadata_agg_node(ts, expect):
    expect.text("yes" if "Columnar Vectorized Aggregates" in ts["plan"] else f"no ({ts['plan']})", "yes",
                "max(ts) over timestamptz plans the metadata agg node")


def test_decoding_ts_reads_the_tables_blocks(ts, expect):
    expect.at_least(_int(ts["decode"], "decode"), 10, "control: decoding ts reads the table's blocks")


@pytest.mark.parametrize("key, name", [
    ("max", "max(ts) reads under a tenth of the blocks a decode of ts reads"),
    ("min", "min(ts) reads under a tenth of the blocks a decode of ts reads"),
])
def test_min_max_ts_read_under_a_tenth_of_a_decode(ts, expect, key, name):
    # The margin is decode - 10 * reading: positive passes, and a failure
    # reports by how much it fell short.
    reading = _int(ts[key], key)
    expect.at_least(_int(ts["decode"], "decode") - 10 * reading if reading else 0, 1, name)


@pytest.mark.parametrize("key, name", [
    ("max parity", "max(timestamptz) parity"),
    ("min parity", "min(timestamptz) parity"),
    ("max parity after", "max(timestamptz) parity after deleting the newest rows"),
    ("min parity after", "min(timestamptz) parity after deleting the oldest rows"),
])
def test_timestamptz_parity(ts, expect, key, name):
    expect.text(*ts[key], name)


def test_the_deletes_removed_the_same_rows_at_both_ends(ts, expect):
    n, nh = ts["after delete"]
    # isinstance first, so an error string reds the check instead of raising TypeError
    expect.text(f"{isinstance(n, int) and n < ROWS}|{n}", f"True|{nh}",
                "the deletes removed rows at both ends, the same rows from each")


# ---- a WHERE with no column in it is applied (#1435) -------------------------------

GATED = "SELECT count(*) || '/' || coalesce(min(id)::text, '-') || '/' || coalesce(max(id)::text, '-') FROM"
# a literal list of pairs, not a dict: the twin grader reads a parametrize table, and
# `dict.items()` hides the names from it
GATES = [
    ("(SELECT false)", "a WHERE (SELECT false) with no column in it gives heap's answer"),
    ("now() < '2000-01-01'", "a WHERE on now() with no column in it gives heap's answer"),
    ("current_user = 'pgc_nobody'", "a WHERE on current_user with no column in it gives heap's answer"),
]


@pytest.fixture(scope="module")
def gate(pgc_cluster):
    """A WHERE that references no column is a gating qual: planned as a one-time
    filter, not part of the per-row quals. The ungrouped vectorized aggregate
    answered from the zone maps alone and ignored it, so a false gate returned every
    row's aggregates (#1435). Each arm is compared with heap."""
    c = _schema_conn(pgc_cluster, "pgc_agg_gate")
    try:
        c.execute("CREATE TABLE gn (id int) USING pgcolumnar")
        c.execute("CREATE TABLE gh (id int) USING heap")
        c.execute("SELECT pgcolumnar.set_options('gn', stripe_row_limit => 1000)")
        _try(c.execute, "INSERT INTO gn SELECT g FROM generate_series(1, 5000) g")
        _try(c.execute, "INSERT INTO gh SELECT g FROM generate_series(1, 5000) g")
        got = {"plan": str(_try(_explain, c, "EXPLAIN (COSTS OFF) SELECT count(*), min(id), max(id) FROM gn"))}
        for where, _ in GATES:
            got[where] = (_try(_one, c, f"{GATED} gn WHERE {where}"),
                          _try(_one, c, f"{GATED} gh WHERE {where}"))
        yield got
    finally:
        _drop(c, "pgc_agg_gate")


def test_count_min_max_with_no_where_are_the_vectorized_aggregate(gate, expect):
    expect.text("yes" if "Columnar Vectorized Aggregates" in gate["plan"] else f"no ({gate['plan']})",
                "yes", "premise: count, min and max with no WHERE are the vectorized aggregate")


@pytest.mark.parametrize("where, name", GATES)
def test_a_where_with_no_column_in_it_gives_heaps_answer(gate, expect, where, name):
    expect.text(*gate[where], name)


def test_heaps_answer_to_a_false_gate_is_no_rows(gate, expect):
    expect.text(gate["(SELECT false)"][1], "0/-/-", "premise: heap's answer to a false gate is no rows")


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
        try:
            cur.execute("EXPLAIN (COSTS OFF) " + sql)
            plan = "\n".join(r[0] for r in cur.fetchall())
        finally:
            # in a finally, so a failed EXPLAIN cannot leave the settings on (#1514 review)
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


def _zm_delta(conn, sql):
    """Zone-map scans `sql` made: the counter after it, less the counter before.

    The query's own error is recorded and the counter still read, as the shell's
    psql_run does, so the twin reports the shell's red set (#1522 review). Both twins
    therefore pass "reads no zone maps" on a count(*) that errors; the count(*)
    parity arm is what reds then."""
    before = _zm_scans(conn)
    _try(_one, conn, sql)
    return _zm_scans(conn) - before


PARITY = [
    ("count(*)", "SELECT count(*) FROM {t}"),
    ("count(col,nulls)", "SELECT count(s) FROM {t}"),
    ("sum(int4)", "SELECT sum(id) FROM {t}"),
    ("sum(int4,nulls)", "SELECT sum(s) FROM {t}"),
    ("avg(int4,nulls)", "SELECT avg(s) FROM {t}"),
    ("min(int4)", "SELECT min(id) FROM {t}"),
    ("max(int4)", "SELECT max(id) FROM {t}"),
    ("min(int4,nulls)", "SELECT min(s) FROM {t}"),
    ("min(text)", "SELECT min(label) FROM {t}"),
    ("max(text)", "SELECT max(label) FROM {t}"),
    ("multi-agg", "SELECT (count(*), sum(id), min(id), max(id))::text FROM {t}"),
    ("filtered aggregate parity", "SELECT (count(*), sum(id))::text FROM {t} WHERE id > 6000"),
    ("sum(bigint) fallback parity", "SELECT sum(k) FROM {t}"),
]


@pytest.fixture(scope="module")
def meta(pgc_cluster):
    c = _schema_conn(pgc_cluster, "pgc_agg_meta")
    got = {}
    try:
        c.execute("CREATE TABLE ah (id int, s int, k bigint, label text)")
        c.execute("CREATE TABLE an (id int, s int, k bigint, label text) USING pgcolumnar")
        c.execute("SELECT pgcolumnar.set_options('an', stripe_row_limit => 2048, "
                  "chunk_group_row_limit => 1024)")
        gen = ("SELECT g, CASE WHEN g % 5 = 0 THEN NULL ELSE g % 997 END, (g * 11)::bigint, "
               "'tag-' || (g % 37) FROM generate_series(1, 12000) g")
        _try(c.execute, f"INSERT INTO ah {gen}")
        _try(c.execute, f"INSERT INTO an {gen}")
        # the node first: every parity arm also holds for a correct fallback
        got["count node"] = _aggnode(str(_try(_plan, c, "SELECT count(*) FROM an")))
        got["sum node"] = _aggnode(str(_try(_plan, c, "SELECT sum(id), min(id), max(id) FROM an")))
        for name, sql in PARITY:
            got[name] = (str(_try(_one, c, sql.format(t="an"))), str(_try(_one, c, sql.format(t="ah"))))
        # it has to win on cost, not only exist (#133)
        got["par count"] = str(_try(_plan, c, "SELECT count(*) FROM an", parallel=True))
        got["par multi"] = str(_try(_plan, c, "SELECT count(*), sum(id), min(id), max(id) FROM an",
                                    parallel=True))
        # count(*) needs no zone map; an aggregate that needs one must be seen reading it,
        # so the zero cannot pass because the counter never moves
        got["zm count"] = _try(_zm_delta, c, "SELECT count(*) FROM an")
        got["zm sum"] = _try(_zm_delta, c, "SELECT count(*), sum(id) FROM an")
        got["filtered node"] = _aggnode(str(_try(_plan, c, "SELECT count(*), sum(id) FROM an WHERE id > 6000")))
        _try(c.execute, "CREATE TABLE ae (id int, label text) USING pgcolumnar")
        got["empty count"] = _try(_one, c, "SELECT count(*) FROM ae")
        got["empty sum"] = str(_try(_one, c, "SELECT sum(id) IS NULL FROM ae"))
        got["empty min"] = str(_try(_one, c, "SELECT min(label) IS NULL FROM ae"))
        yield got
    finally:
        _drop(c, "pgc_agg_meta")


@pytest.mark.parametrize("key, name", [
    ("count node", "count(*) uses the metadata agg node"),
    ("sum node", "sum/min/max uses the metadata agg node"),
])
def test_the_aggregate_uses_the_metadata_node(meta, expect, key, name):
    expect.text(meta[key], "yes", name)


@pytest.mark.parametrize("name, sql", PARITY)
def test_the_aggregate_answers_as_heap(meta, expect, name, sql):
    # sql is the fixture's query; read here only so the table names its own rows
    expect.text(*meta[name], name)


@pytest.mark.parametrize("key, name", [
    ("par count", "count(*) beats a parallel plan"),
    ("par multi", "multi-agg beats a parallel plan"),
])
def test_the_metadata_node_beats_a_parallel_plan(meta, expect, key, name):
    expect.text(_aggnode(meta[key]), "yes", name)


@pytest.mark.parametrize("key, name", [
    ("par count", "count(*) plan has no Gather"),
    ("par multi", "multi-agg plan has no Gather"),
])
def test_the_metadata_plan_has_no_gather(meta, expect, key, name):
    expect.text(_gather(meta[key]), "no", name)


def test_count_reads_no_zone_maps(meta, expect):
    expect.num(_num(meta["zm count"], "zm count"), 0, "count(*) reads no zone maps")


def test_an_aggregate_that_needs_zone_maps_reads_them(meta, expect):
    # the measured delta, not a yes/no (#1514 review, item 1)
    expect.at_least(_int(meta["zm sum"], "zm sum"), 1, "an aggregate that needs zone maps reads them")


def test_a_filtered_aggregate_is_not_the_metadata_node(meta, expect):
    expect.text(meta["filtered node"], "no", "filtered aggregate not metadata node")


def test_an_empty_table_counts_zero(meta, expect):
    expect.num(_num(meta["empty count"], "empty count"), 0, "empty count(*)")


@pytest.mark.parametrize("key, name", [
    ("empty sum", "empty sum NULL"),
    ("empty min", "empty min NULL"),
])
def test_an_empty_table_aggregates_to_null(meta, expect, key, name):
    expect.text(meta[key], "True", name)
