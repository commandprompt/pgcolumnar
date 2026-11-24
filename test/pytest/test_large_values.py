"""Megabyte-scale values are stored exactly (#1341). The pytest twin of
`test/large_values.sh`, every check by the shell's name.

A columnar table has no TOAST table: the writer detoasts each value and stores it
inline. Each value goes into heap first and is copied to a columnar table, and the
two must agree on each value's length and md5: 1 MB and two 16 MB incompressible
values and a 200 MB compressible one, then an UPDATE that takes a value heap has
TOASTed.

Rows whose column passes 1 GB in one row group, or in one chunk group, load and are
stored exactly (#2071): the writer closes a row group early instead of building a
buffer past what one allocation may hold. An opt-in arm does the same with about
1.1 GB of incompressible text and compression off; before #2071 that INSERT failed
with 54000. That arm needs several GB of memory, so it is opt-in, as the shell's is:
it runs only with PGC_RUN_OVER_1GB=1, and otherwise declines as UNMET_PRECONDITION,
which expected_unrunnable.txt lists for every major (#432, the same shape as
test_objstore_s3_read.py's real-S3 arm).
"""
import os

import psycopg


def _one(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


def _mb(n, salt):
    return f"(SELECT string_agg(md5(g::text || '{salt}'), '') FROM generate_series(1, {n} * 32768) g)"


OVER = "1.1 GB of incompressible text in one chunk group's rows loads, and the server stays up"
DIGEST = "SELECT string_agg(id || ':' || length(big) || ':' || md5(big), ' ' ORDER BY id) FROM "


def test_megabyte_values_are_stored_exactly(pgc_conn, expect):
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE lv_heap (id int, big text)")
        cur.execute("CREATE TABLE lv (id int, big text) USING pgcolumnar")
        cur.execute(f"INSERT INTO lv_heap VALUES (1, {_mb(1, 'a')}), (2, {_mb(16, 'b')}), (3, {_mb(16, 'c')}), "
                    f"(4, repeat('z', 200 * 1024 * 1024))")
    expect.text(_one(pgc_conn, "SELECT string_agg(length(big)::text, ',' ORDER BY id) FROM lv_heap"),
                "1048576,16777216,16777216,209715200",
                "premise: heap holds a 1 MB, two 16 MB and a 200 MB value")
    with pgc_conn.cursor() as cur:
        cur.execute("INSERT INTO lv SELECT * FROM lv_heap")
    expect.text(_one(pgc_conn, DIGEST + "lv"), _one(pgc_conn, DIGEST + "lv_heap"),
                "the values copied into a columnar table are stored exactly, by length and md5")

    with pgc_conn.cursor() as cur:
        cur.execute("UPDATE lv_heap SET big = big || 'x' WHERE id = 2")
    expect.text(str(_one(pgc_conn, "SELECT pg_column_size(big) < length(big) OR pg_column_size(big) > 8192 "
                                   "FROM lv_heap WHERE id = 2")),
                "True", "premise: heap stores the new 16 MB value TOASTed, not inline")
    with pgc_conn.cursor() as cur:
        cur.execute("UPDATE lv SET big = h.big FROM lv_heap h WHERE lv.id = h.id AND h.id = 2")
    expect.text(_one(pgc_conn, DIGEST + "lv WHERE id = 2"), _one(pgc_conn, DIGEST + "lv_heap WHERE id = 2"),
                "an UPDATE that takes a TOASTed heap value stores it exactly in the columnar row")
    expect.text(_one(pgc_conn, DIGEST + "lv WHERE id <> 2"), _one(pgc_conn, DIGEST + "lv_heap WHERE id <> 2"),
                "and the other rows are unchanged")


def _load(conn, sql):
    """The INSERT's command tag. A failed load raises, so the test fails on the
    server's own error rather than on a comparison."""
    with conn.cursor() as cur:
        cur.execute(sql)
        return f"INSERT 0 {cur.rowcount}"


def _summary(conn, table, value_sql):
    return _one(conn, f"SELECT count(*) || ',' || sum(id) || ',' || sum(length(t)) || ',' "
                      f"|| count(*) FILTER (WHERE t <> {value_sql}) FROM {table}")


def test_rows_past_1gb_per_row_group_or_chunk_group_load(pgc_conn, expect):
    """#2071. The writer used to cut row groups and chunk groups by row count alone.
    With the default limits, 150000 rows of 8 KB, or 10000 rows of 120 KB, put 1.2 GB
    of one column into one buffer, and the INSERT failed with "string buffer exceeds
    maximum allowed length". The values compress to almost nothing."""
    expect.text(_one(pgc_conn, "SELECT current_setting('pgcolumnar.stripe_row_limit') || ',' "
                               "|| current_setting('pgcolumnar.chunk_group_row_limit')"),
                "150000,10000",
                "premise: the row limits are the defaults, 150000 per row group and 10000 per chunk group")
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE lv_rg (id int, t text) USING pgcolumnar")
        cur.execute("CREATE TABLE lv_cg (id int, t text) USING pgcolumnar")
        cur.execute("SET statement_timeout = '600s'")
    tag = _load(pgc_conn, "INSERT INTO lv_rg SELECT g, repeat('y', 8000) FROM generate_series(1, 150000) g")
    got = _summary(pgc_conn, "lv_rg", "repeat('y', 8000)")
    expect.text(f"{tag}|{got}",
                "INSERT 0 150000|150000,11250075000,1200000000,0",
                "150000 rows of 8 KB text, 1.2 GB for one row group, load and are stored exactly")
    tag = _load(pgc_conn, "INSERT INTO lv_cg SELECT g, repeat('x', 120000) FROM generate_series(1, 10000) g")
    got = _summary(pgc_conn, "lv_cg", "repeat('x', 120000)")
    expect.text(f"{tag}|{got}",
                "INSERT 0 10000|10000,50005000,1200000000,0",
                "10000 rows of 120 KB text, 1.2 GB for one chunk group, load and are stored exactly")
    with pgc_conn.cursor() as cur:
        cur.execute("DROP TABLE lv_rg, lv_cg")


def test_incompressible_text_past_1gb_loads(pgc_cluster, expect):
    """About 1.1 GB of incompressible text in one chunk group's rows, compression off,
    so no codec makes the data smaller (#2071; before it, the INSERT failed with 54000).
    A failure is read as its SQLSTATE, and the table and the server are read on fresh
    connections, so a crash is not a pass."""
    if os.environ.get("PGC_RUN_OVER_1GB", "0") != "1":
        expect.cannot_run("UNMET_PRECONDITION",
                          "PGC_RUN_OVER_1GB is not 1; the arm needs several GB of memory",
                          name=OVER)
        return
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS lv_huge")
        c.execute("CREATE TABLE lv_huge (id int, big text) USING pgcolumnar")
    state = "committed"
    try:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as c:
            c.execute("SET statement_timeout = '900s'")
            c.execute("SET pgcolumnar.compression = none")
            c.execute("INSERT INTO lv_huge SELECT k, " + _mb(1, "k' || k || '")
                      + " FROM generate_series(1, 1100) k")
    except psycopg.Error as exc:
        state = exc.sqlstate or "no SQLSTATE"

    def fresh(sql):
        try:
            with psycopg.connect(pgc_cluster.dsn(), autocommit=True, connect_timeout=10) as c:
                return str(c.execute(sql).fetchone()[0])
        except psycopg.Error as exc:
            return f"unreachable ({str(exc).splitlines()[0]})"

    got = fresh("SELECT count(*) || ',' || sum(length(big)) FROM lv_huge")
    expect.text(f"{state}|{got}|{fresh('SELECT 1')}", "committed|1100,1153433600|1", OVER)
