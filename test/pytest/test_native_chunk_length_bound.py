"""A column chunk's page_length is uint64 in the catalog, but both decode
entry points cast the value stream to uint32. Adding 2^32 to page_length
leaves the low 32 bits unchanged, so an index fetch silently reads the
original stream and returns the row.

Independent of test/native_chunk_length_bound.sh: same public seam, own
fixture, own observations. Assertion names match the shell suite.
"""

import pytest


def _scan_node(plan):
    stack = [plan[0]["Plan"]]
    while stack:
        node = stack.pop(0)
        t = node["Node Type"]
        if t in (
            "Index Scan",
            "Index Only Scan",
            "Bitmap Heap Scan",
            "Custom Scan",
            "Seq Scan",
        ):
            return t
        stack.extend(node.get("Plans", ()))
    return ""


def test_native_chunk_length_bound(pgc_conn, expect):
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE clb (id int, t text) USING pgcolumnar")
        cur.execute(
            "INSERT INTO clb SELECT g, 'v' || g::text FROM generate_series(1, 5000) g"
        )
        cur.execute("CREATE INDEX clb_id ON clb(id)")
        cur.execute("ANALYZE clb")
        cur.execute("SELECT pgcolumnar.get_storage_id('clb')")
        sid = cur.fetchone()[0]
        cur.execute("SET enable_seqscan=off")
        cur.execute("SET enable_bitmapscan=off")
        cur.execute("SET pgcolumnar.enable_custom_scan=off")
        cur.execute("EXPLAIN (FORMAT JSON, COSTS OFF) SELECT t FROM clb WHERE id = 1")
        plan = cur.fetchone()[0]
    expect.text(
        _scan_node(plan),
        "Index Scan",
        "premise: a point lookup uses the index, not a sequential columnar scan",
    )

    with pgc_conn.cursor() as cur:
        cur.execute("SET enable_seqscan=off")
        cur.execute("SET enable_bitmapscan=off")
        cur.execute("SET pgcolumnar.enable_custom_scan=off")
        cur.execute("SELECT t FROM clb WHERE id = 1")
        got = cur.fetchone()[0]
    expect.text(got, "v1", "premise: that fetch returns the row")

    with pgc_conn.cursor() as cur:
        cur.execute(
            "UPDATE pgcolumnar.column_chunk SET page_length = page_length + 4294967296 "
            "WHERE storage_id = %s AND column_index = 1",
            (sid,),
        )

    with pgc_conn.cursor() as cur:
        cur.execute("SET enable_seqscan=off")
        cur.execute("SET enable_bitmapscan=off")
        cur.execute("SET pgcolumnar.enable_custom_scan=off")
        with pytest.raises(Exception) as fetch_err:
            cur.execute("SELECT t FROM clb WHERE id = 1")
    expect.sqlstate(
        fetch_err.value,
        "XX001",
        "an index fetch of a chunk whose page_length is 2^32 too large is refused (XX001)",
    )

    with pgc_conn.cursor() as cur:
        cur.execute("SELECT 1")
        expect.num(cur.fetchone()[0], 1, "backend survived the truncated-length fetch")

    with pgc_conn.cursor() as cur:
        cur.execute("SET pgcolumnar.enable_custom_scan=on")
        cur.execute("SET enable_indexscan=off")
        with pytest.raises(Exception) as scan_err:
            cur.execute("SELECT t FROM clb WHERE id = 1")
    expect.sqlstate(
        scan_err.value,
        "XX001",
        "a sequential scan of the same poisoned chunk is refused (XX001)",
    )

    with pgc_conn.cursor() as cur:
        cur.execute("SELECT 1")
        expect.num(cur.fetchone()[0], 1, "backend survived the sequential refusal")
