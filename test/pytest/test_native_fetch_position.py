"""A fetch by row number costs the same wherever the row sits in its group (#143, #1341).
The pytest twin of `test/native_fetch_position.sh`, every check by the shell's name.

A cached group still walked to the row: counting the validity bits below it, then
decoding and discarding that many values, so a fetch cost O(row) and fetching a whole
group was quadratic. A rank prefix over the validity bitmap and an offset table for
varying-length columns make it O(1). Values first, against a heap mirror over fixed,
varying-length and heavily-null columns, through an index scan; then two timing ratios
(far end against near end, and a doubled group), each the median of three passes; then
the two indexes in pgcolumnar_fetch_row's source.
"""
import os
import pathlib
import re
import statistics
import time

import psycopg
import pytest

# the shell reads PGC_FETCHPOS_ROWS, and so does this
ROWS = int(os.environ.get("PGC_FETCHPOS_ROWS", "120000"))
SRC = pathlib.Path(__file__).resolve().parents[2] / "src"
# enable_seqscan=off leaves the columnar custom scan on, and with only that every query
# here scanned rather than fetched by row number; each measurement asserts its plan
FORCE = ("SET enable_seqscan = off", "SET enable_bitmapscan = off", "SET max_parallel_workers_per_gather = 0",
         "SET pgcolumnar.enable_custom_scan = off")


def _one(c, sql):
    return c.execute(sql).fetchone()[0]


def _forced(c):
    for s in FORCE:
        c.execute(s)


def _reads_index(c, sql, index):
    """-> 'yes' when `sql`'s plan is an Index Scan using `index`, else its first scan line."""
    plan = "\n".join(r[0] for r in c.execute(f"EXPLAIN (COSTS OFF) {sql}"))
    if f"Index Scan using {index} " in plan:
        return "yes"
    first = next((l.strip() for l in plan.splitlines() if "Scan" in l or "Update" in l), plan)
    return f"no: {first}"


def _rows_or_error(c, sql):
    """-> the first value, or the error as a value: a corrupt fetch is a named red."""
    try:
        return _one(c, sql)
    except psycopg.Error as exc:
        return f"error: {exc.diag.message_primary}"


def _median_ms(c, sql):
    """-> the median of three timed passes of `sql`, in milliseconds."""
    times = []
    for _ in range(3):
        t0 = time.monotonic()
        c.execute(sql)
        times.append((time.monotonic() - t0) * 1000.0)
    return statistics.median(times)


@pytest.fixture(scope="module")
def fpos(pgc_cluster):
    """-> a connection to a database holding fp_c (one row group) and its heap mirror."""
    db = "pgc_fetchpos"
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {db}")
    try:
        with psycopg.connect(pgc_cluster.dsn(dbname=db), autocommit=True) as c:
            c.execute("CREATE EXTENSION pgcolumnar")
            # every fourth row null in the nullable columns, so rank and row number diverge
            c.execute(f"SET pgcolumnar.stripe_row_limit = {ROWS}")
            c.execute(f"SET pgcolumnar.chunk_group_row_limit = {ROWS}")
            c.execute("CREATE TABLE fp_c (id int, fixed bigint, txt text, sparse int, stxt text) USING pgcolumnar")
            c.execute(f"""INSERT INTO fp_c
                SELECT g, g * 7, repeat('x', 1 + (g % 40)) || g,
                       CASE WHEN g % 4 = 0 THEN NULL ELSE g END,
                       CASE WHEN g % 4 = 0 THEN NULL ELSE 's' || g END
                FROM generate_series(1, {ROWS}) g""")
            c.execute("CREATE INDEX fp_c_id ON fp_c (id)")
            c.execute("CREATE TABLE fp_h (LIKE fp_c)")
            c.execute("INSERT INTO fp_h SELECT * FROM fp_c")
            yield c
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


def test_every_fetched_value_matches_the_heap_mirror(fpos, expect):
    c = fpos
    expect.num(_one(c, "SELECT count(*) FROM pgcolumnar.row_group r "
                       "JOIN pgcolumnar.storage s ON s.storage_id = r.storage_id "
                       "WHERE s.relation_oid = 'fp_c'::regclass"), 1, "the rows are in one row group")
    _forced(c)
    expect.text(_reads_index(c, "SELECT count(*) FROM fp_c c JOIN fp_h h USING (id) "
                                "WHERE c.fixed IS DISTINCT FROM h.fixed", "fp_c_id"), "yes",
                "premise: the mirror comparison fetches through the index")
    expect.text(_reads_index(c, "SELECT fixed FROM fp_c WHERE id = 1001", "fp_c_id"), "yes",
                "premise: a single-row fetch goes through the index")
    expect.text(str(_rows_or_error(c, """SELECT count(*) FROM fp_c c JOIN fp_h h USING (id)
                          WHERE c.fixed IS DISTINCT FROM h.fixed
                             OR c.txt IS DISTINCT FROM h.txt
                             OR c.sparse IS DISTINCT FROM h.sparse
                             OR c.stxt IS DISTINCT FROM h.stxt""")), "0", "every column matches the heap mirror")
    # one row at a time, where an off-by-one in the rank would land on a neighbour
    row = "fixed || '|' || txt || '|' || coalesce(sparse::text, 'N') || '|' || coalesce(stxt, 'N')"
    bad = []
    for pos in (1, 2, 3, 4, 5, 999, 1000, 1001, ROWS // 2, ROWS - 1, ROWS):
        got = _rows_or_error(c, f"SELECT {row} FROM fp_c WHERE id = {pos}")
        want = _one(c, f"SELECT {row} FROM fp_h WHERE id = {pos}")
        if got != want:
            bad.append(f"id={pos}(got={got} want={want})")
    expect.text(" ".join(bad) or "same", "same", "single-row fetches are right at every position")


def test_the_cost_does_not_depend_on_the_position(fpos, expect):
    c = fpos
    _forced(c)
    n = ROWS // 20
    # a read, not an UPDATE: an UPDATE writes its rows into a new group, so only the first
    # pass fetched from the far end and the median was always a later one
    read = "SELECT sum(fixed), sum(length(txt)) FROM fp_c WHERE id BETWEEN {} AND {}"
    expect.text(_reads_index(c, read.format(ROWS - n + 1, ROWS), "fp_c_id"), "yes",
                "premise: the timed reads fetch through the index")
    near = _median_ms(c, read.format(1, n))
    far = _median_ms(c, read.format(ROWS - n + 1, ROWS))
    print(f"-- {n} fetches, median of three: first tenth {near:.0f} ms, last tenth {far:.0f} ms")
    # a ratio of 3 catches the quadratic walk (1,001,374 ms against 614 ms) with orders to spare
    expect.text("yes" if near > 0 and far / near < 3 else f"no (near={near:.0f}ms far={far:.0f}ms)", "yes",
                "fetching from the far end of a group costs about what the near end does")


def test_a_bigger_group_does_not_cost_more_per_fetch(fpos, expect):
    c = fpos
    for name, rows in (("fp_small", ROWS // 2), ("fp_big", ROWS)):
        c.execute(f"SET pgcolumnar.stripe_row_limit = {rows}")
        c.execute(f"SET pgcolumnar.chunk_group_row_limit = {rows}")
        c.execute(f"CREATE TABLE {name} (id int, v bigint, t text) USING pgcolumnar")
        c.execute(f"INSERT INTO {name} SELECT g, g * 3, 't' || g FROM generate_series(1, {rows}) g")
        c.execute(f"CREATE INDEX {name}_id ON {name} (id)")
    _forced(c)
    # the rows fetched sit at the END of each group, where the old walk was longest
    k = ROWS // 20
    read = "SELECT sum(v), sum(length(t)) FROM {} WHERE id > {}"
    expect.text(_reads_index(c, read.format("fp_big", ROWS - k), "fp_big_id"), "yes",
                "premise: the doubled group's reads fetch through its index")
    small = _median_ms(c, read.format("fp_small", ROWS // 2 - k))
    big = _median_ms(c, read.format("fp_big", ROWS - k))
    print(f"-- {k} fetches at the far end, median of three: {ROWS // 2} rows took {small:.0f} ms, {ROWS} took {big:.0f} ms")
    expect.text("yes" if small > 0 and big / small < 1.8 else f"no (small={small:.0f}ms big={big:.0f}ms)", "yes",
                "doubling the row group does not double the cost of the same fetches")


def test_the_indexes_that_make_it_constant_are_in_the_fetch(expect):
    m = re.search(r"^pgcolumnar_fetch_row\(.*?^}", (SRC / "columnar_reader.c").read_text(), re.S | re.M)
    fetch = m.group(0) if m else ""
    expect.text("yes" if fetch else "no", "yes", "premise: pgcolumnar_fetch_row was extracted, not an empty range")
    expect.text("yes" if "present = pgcolumnar_rank_before" in fetch else "no", "yes",
                "the rank comes from a prefix rather than a loop over earlier rows")
    expect.text("yes" if "entry->valOffset[c][present]" in fetch else "no", "yes",
                "a varying-length column reaches its value through an offset table")
