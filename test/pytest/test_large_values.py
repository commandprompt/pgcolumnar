"""Megabyte-scale values are stored exactly (#1341). The pytest twin of
`test/large_values.sh`, every check by the shell's name.

A columnar table has no TOAST table: the writer detoasts each value and stores it
inline. Each value goes into heap first and is copied to a columnar table, and the
two must agree on each value's length and md5: 1 MB and two 16 MB incompressible
values and a 200 MB compressible one, then an UPDATE that takes a value heap has
TOASTed.

A chunk over 1 GB is past what one allocation may hold, and is refused with 54000,
committing nothing, while the server stays up. That arm needs several GB of memory, so
it is opt-in, as the shell's is: it runs only with PGC_RUN_OVER_1GB=1, and otherwise
declines as UNMET_PRECONDITION, which expected_unrunnable.txt lists for every major
(#432, the same shape as test_objstore_s3_read.py's real-S3 arm).
"""
import os

import psycopg


def _one(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


def _mb(n, salt):
    return f"(SELECT string_agg(md5(g::text || '{salt}'), '') FROM generate_series(1, {n} * 32768) g)"


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


def test_a_chunk_over_1gb_is_refused(pgc_cluster, expect):
    """About 1.1 GB of incompressible text in one chunk group, compression off, so the
    chunk cannot fit one allocation. The refusal is read as its SQLSTATE, and the table
    and the server are read on fresh connections, so a crash is not a pass."""
    if os.environ.get("PGC_RUN_OVER_1GB", "0") != "1":
        expect.cannot_run("UNMET_PRECONDITION",
                          "PGC_RUN_OVER_1GB is not 1; the arm needs several GB of memory",
                          name="a chunk over 1 GB is refused with 54000, commits nothing, and the server stays up")
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

    expect.text(f"{state}|{fresh('SELECT count(*) FROM lv_huge')}|{fresh('SELECT 1')}", "54000|0|1",
                "a chunk over 1 GB is refused with 54000, commits nothing, and the server stays up")
