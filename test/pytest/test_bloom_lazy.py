"""A scan reads the bloom filters of the groups it examines and the columns it filters
on, and no others (#310, #314). The pytest twin of `test/bloom_lazy.sh`, every check by
the shell's name (#1341).

Two tables differ only in how many groups a one-group equality scan must skip (6 and
30), so the bloom blocks they fetch, read from pg_stat_get_blocks_fetched on
pgcolumnar.bloom, must not grow with the skipped groups; and on a wide table where no
group is excluded, six equality predicates must fetch more than one. The answers are
compared with a heap mirror, with and without bloom filters.

A READING IS NEVER A DEFAULT. A missing block count is a failed query rather than 0,
and the kept group must have read at least one block, so "nothing was read" cannot
pass as "nothing extra was read". ON A PRIVATE DATABASE, built once by a module
fixture in the shell's order; each test asserts one reading (#1339).
"""
import re

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_bloom_lazy"
COLS = "a int, b int, c int, d int, e int, f int, g1 int, h int, i int, j int, k int"
VALS = "g,g,g,g,g,g,g,g,g,g,g % 1000"
PER_GROUP = 20000
Q_SMALL = "SELECT count(*) FROM small WHERE seg = 3"
Q_LARGE = "SELECT count(*) FROM large WHERE seg = 3"
ALL = "SELECT count(*) FROM large WHERE k = 7"
ONE_PRED = "SELECT count(*) FROM wide WHERE p0 = 1"
SIX_PRED = ("SELECT count(*) FROM wide WHERE p0 = 1 AND p1 = 1 AND p2 = 1 AND p3 = 1 "
            "AND p4 = 1 AND p5 = 1")


# The shell's cluster runs with no parallel workers; left at the default, a Gather
# splits the scan and its per-worker counts disagree from one run to the next.
SERIAL = "-c max_parallel_workers_per_gather=0"


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _run(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True, options=SERIAL) as conn:
            conn.execute(sql.encode())  # simple protocol: several statements, as psql -c
    except psycopg.Error as exc:
        return _message(exc)
    return ""


def _value(dsn, sql, settings=()):
    """-> the first column of the first row, or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True, options=SERIAL) as conn:
            for s in settings:
                conn.execute(s)
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    if row is None or row[0] is None:
        return query_error(f"no value from {sql}")
    return row[0]


def _plan_count(dsn, sql, label):
    """-> the number EXPLAIN ANALYZE reports after `label`, or a failed reading."""
    try:
        with psycopg.connect(dsn, autocommit=True, options=SERIAL) as conn:
            plan = "\n".join(r[0] for r in conn.execute(f"EXPLAIN (ANALYZE, TIMING OFF, COSTS OFF) {sql}"))
    except psycopg.Error as exc:
        return query_error(_message(exc))
    m = re.search(re.escape(label) + r": (\d+)", plan)
    return int(m.group(1)) if m else query_error(f"no '{label}' in the plan")


def _fetched(dsn):
    return _value(dsn, "SELECT pg_stat_get_blocks_fetched('pgcolumnar.bloom'::regclass)")


def _bloom_blocks(dsn, sql):
    """-> the bloom blocks one run of `sql` fetched, or a failed reading. The query's
    backend forces its own stats out before the second reading is taken."""
    before = _fetched(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True, options=SERIAL) as conn:
            conn.execute(sql)
            conn.execute("SELECT pg_stat_force_next_flush()")
            conn.execute("SELECT 1")
    except psycopg.Error as exc:
        return query_error(_message(exc))
    after = _fetched(dsn)
    if not isinstance(before, int) or not isinstance(after, int):
        return query_error(f"no reading: before {before}, after {after}")
    return after - before


def _set_hash(dsn, sql):
    return _value(dsn, "SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), 'EMPTY') "
                       f"FROM (SELECT _row::text AS t FROM ({sql}) _row) _s")


def _build(dsn, table, groups):
    _run(dsn, f"DROP TABLE IF EXISTS {table}")
    _run(dsn, f"CREATE TABLE {table} (seg int, {COLS}) USING pgcolumnar")
    _run(dsn, f"SELECT pgcolumnar.set_options('{table}', stripe_row_limit => {PER_GROUP}, "
              "chunk_group_row_limit => 1024)")
    _run(dsn, f"INSERT INTO {table} SELECT g/{PER_GROUP}, {VALS} "
              f"FROM generate_series(0, {PER_GROUP * groups - 1}) g")
    _run(dsn, f"ANALYZE {table}")


@pytest.fixture(scope="module")
def blz(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r = {}
        _build(dsn, "small", 6)
        _build(dsn, "large", 30)
        r["small total"] = _plan_count(dsn, Q_SMALL, "Chunk Groups Total")
        r["large total"] = _plan_count(dsn, Q_LARGE, "Chunk Groups Total")
        r["small read"] = _plan_count(dsn, Q_SMALL, "Chunk Groups Read")
        r["large read"] = _plan_count(dsn, Q_LARGE, "Chunk Groups Read")
        r["small blk"] = _bloom_blocks(dsn, Q_SMALL)
        r["large blk"] = _bloom_blocks(dsn, Q_LARGE)
        r["all read"] = _plan_count(dsn, ALL, "Chunk Groups Read")
        r["all total"] = _plan_count(dsn, ALL, "Chunk Groups Total")
        r["all blk"] = _bloom_blocks(dsn, ALL)

        _run(dsn, f"CREATE TABLE h_mirror (seg int, {COLS})")
        _run(dsn, f"INSERT INTO h_mirror SELECT g/{PER_GROUP}, {VALS} FROM generate_series(0, 599999) g")
        for key, where in (("seg", "seg = 3"), ("absent", "k = -999"), ("scattered", "a = 123456"),
                           ("two", "seg = 3 AND k = 60001"), ("range", "seg BETWEEN 4 AND 9")):
            r[f"{key} col"] = _value(dsn, f"SELECT count(*) FROM large WHERE {where}")
            r[f"{key} heap"] = _value(dsn, f"SELECT count(*) FROM h_mirror WHERE {where}")
        r["hash col"] = _set_hash(dsn, "SELECT seg, a, k FROM large")
        r["hash heap"] = _set_hash(dsn, "SELECT seg, a, k FROM h_mirror")
        off = ["SET pgcolumnar.enable_bloom_filter = off"]
        r["off scattered"] = _value(dsn, "SELECT count(*) FROM large WHERE a = 123456", off)
        r["off absent"] = _value(dsn, "SELECT count(*) FROM large WHERE k = -999", off)

        _run(dsn, "CREATE TABLE wide (p0 int, p1 int, p2 int, p3 int, p4 int, p5 int, "
                  "q0 int, q1 int, q2 int, q3 int, q4 int, q5 int) USING pgcolumnar")
        _run(dsn, "SELECT pgcolumnar.set_options('wide', stripe_row_limit => 20000, chunk_group_row_limit => 1024)")
        _run(dsn, "INSERT INTO wide SELECT g % 500, g % 500, g % 500, g % 500, g % 500, g % 500, "
                  "g % 500, g % 500, g % 500, g % 500, g % 500, g % 500 FROM generate_series(1, 400000) g")
        _run(dsn, "ANALYZE wide")
        r["wide read"] = _plan_count(dsn, ONE_PRED, "Chunk Groups Read")
        r["wide total"] = _plan_count(dsn, ONE_PRED, "Chunk Groups Total")
        r["one blk"] = _bloom_blocks(dsn, ONE_PRED)
        r["six blk"] = _bloom_blocks(dsn, SIX_PRED)
        r["one answer"] = _value(dsn, ONE_PRED)
        r["six answer"] = _value(dsn, SIX_PRED)
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _both(a, b):
    return isinstance(a, int) and isinstance(b, int)


def test_the_small_table_has_the_groups_it_should(blz, expect):
    expect.text(str(blz["small total"]), "6", "the small table has the groups it should")


def test_the_large_table_has_the_groups_it_should(blz, expect):
    expect.text(str(blz["large total"]), "30", "the large table has the groups it should")


def test_the_small_scan_keeps_one_group(blz, expect):
    expect.text(str(blz["small read"]), "1", "the small scan keeps one group")


def test_the_large_scan_keeps_one_group(blz, expect):
    expect.text(str(blz["large read"]), "1", "the large scan keeps one group")


def test_bloom_reads_do_not_follow_the_skipped_groups(blz, expect):
    s, l = blz["small blk"], blz["large blk"]
    if not _both(s, l):
        got = query_error(f"no reading: [{s}] and [{l}]")
    elif s < 1:
        got = "no reading (the kept group read no bloom block)"
    else:
        got = "yes" if l - s < 12 else f"no ({s} blocks for 6 groups, {l} for 30)"
    expect.text(got, "yes", "bloom reads do not follow the skipped groups")


def test_the_control_query_really_does_keep_every_group(blz, expect):
    expect.text(str(blz["all read"]), str(blz["all total"]), "the control query really does keep every group")


def test_a_scan_that_keeps_every_group_reads_more(blz, expect):
    a, l = blz["all blk"], blz["large blk"]
    got = ("yes" if a > l else f"no ({a} against {l})") if _both(a, l) else query_error(f"no reading: {a} / {l}")
    expect.text(got, "yes", "a scan that keeps every group reads more")


def test_equality_on_the_segment_key_agrees_with_the_heap(blz, expect):
    expect.text(str(blz["seg col"]), str(blz["seg heap"]), "equality on the segment key agrees with the heap")


def test_equality_that_no_group_holds_agrees_with_the_heap(blz, expect):
    expect.text(str(blz["absent col"]), str(blz["absent heap"]), "equality that no group holds agrees with the heap")


def test_equality_on_a_scattered_column_agrees_with_the_heap(blz, expect):
    expect.text(str(blz["scattered col"]), str(blz["scattered heap"]),
                "equality on a scattered column agrees with the heap")


def test_two_equality_predicates_agree_with_the_heap(blz, expect):
    expect.text(str(blz["two col"]), str(blz["two heap"]), "two equality predicates agree with the heap")


def test_a_range_predicate_still_agrees_with_the_heap(blz, expect):
    expect.text(str(blz["range col"]), str(blz["range heap"]), "a range predicate still agrees with the heap")


def test_full_set_parity_with_the_heap_mirror(blz, expect):
    expect.text(blz["hash col"], blz["hash heap"], "full set parity")


def test_results_agree_with_bloom_filters_disabled(blz, expect):
    expect.text(str(blz["off scattered"]), str(blz["scattered heap"]), "results agree with bloom filters disabled")


def test_an_absent_value_agrees_with_bloom_filters_disabled(blz, expect):
    expect.text(str(blz["off absent"]), str(blz["absent heap"]), "an absent value agrees with bloom filters disabled")


def test_no_wide_group_is_excluded(blz, expect):
    expect.text(str(blz["wide read"]), str(blz["wide total"]), "no group is excluded, so every predicate is reached")


def test_bloom_reads_follow_the_columns_a_query_filters_on(blz, expect):
    o, s = blz["one blk"], blz["six blk"]
    got = ("yes" if s > o else f"no ({o} against {s})") if _both(o, s) else query_error(f"no reading: {o} / {s}")
    expect.text(got, "yes", "bloom reads follow the columns a query filters on")


def test_one_predicate_costs_well_less_than_six(blz, expect):
    o, s = blz["one blk"], blz["six blk"]
    got = ("yes" if o * 2 < s else f"no ({o} against {s})") if _both(o, s) else query_error(f"no reading: {o} / {s}")
    expect.text(got, "yes", "one predicate costs well less than six")


def test_the_filtered_answers_agree_with_a_heap_mirror(blz, expect):
    expect.text(str(blz["one answer"]), "800", "the filtered answers agree with a heap mirror")


def test_six_predicates_give_the_same_answer_as_one_here(blz, expect):
    expect.text(str(blz["six answer"]), "800", "six predicates give the same answer as one here")
