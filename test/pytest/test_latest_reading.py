"""The "latest reading per device" recipe in docs/how-to.md does what it says (#1293).

The recipe's SQL is READ FROM THE DOCUMENT, so an edit to it is what runs: a
recipe that stops returning the right rows, or stops being cheap, turns this red.
Measured on #1293 with 10,000,000 rows and 1,000 devices: DISTINCT ON read 17,204
blocks in 5.6 s; a btree on (device, ts) plus LATERAL read 1,013 blocks in 45 ms;
the same LATERAL without the btree read 8.7 million blocks in 12.6 minutes.

Asserted here: each documented form returns the rows DISTINCT ON returns; the
GROUP BY form reads the table at most once; with the btree the LATERAL form reads
under a tenth of the table, and at least one block (a query that reads nothing
would otherwise pass); without it, it reads the table about once per device.

ON A PRIVATE DATABASE (`pgc_own_db`): the document names its tables, `readings`
and `devices`, so they cannot carry a per-test prefix. Independent of
`test/latest_reading.sh`: its own extraction of the document (Python, not awk),
its own data, device count and row count.
"""

import pathlib
import re

DOC = pathlib.Path(__file__).resolve().parent.parent.parent / "docs" / "how-to.md"
SECTION = "## Find the latest reading per device"
DEVICES = 25
ROWS = 900000


def _recipe_blocks():
    text = DOC.read_text(encoding="utf-8")
    start = text.find(SECTION + "\n")
    if start < 0:
        return []
    rest = text[start + len(SECTION):]
    nxt = re.search(r"^## ", rest, re.M)
    body = rest[: nxt.start()] if nxt else rest
    return [b.strip() for b in re.findall(r"^```sql\n(.*?)^```", body, re.M | re.S)]


def _one(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    return row[0] if row else None


def _rows(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return [tuple(map(str, r)) for r in cur.fetchall()]


def _blocks(conn, sql):
    with conn.cursor() as cur:
        cur.execute("SELECT pg_stat_force_next_flush()")
        cur.execute("SELECT pg_stat_reset()")
        cur.execute(sql)
        cur.fetchall()
        cur.execute("SELECT pg_stat_force_next_flush()")
        cur.execute("SELECT coalesce(heap_blks_read,0) + coalesce(heap_blks_hit,0)"
                    " FROM pg_statio_user_tables WHERE relname = 'readings'")
        row = cur.fetchone()
    return int(row[0]) if row else None


def test_the_latest_reading_recipe_does_what_the_document_says(pgc_own_db, expect):
    conn = pgc_own_db
    blocks = _recipe_blocks()
    expect.num(len(blocks), 3, "premise: the document's three recipe blocks were found")
    if len(blocks) != 3:
        return
    index_sql, group_sql, lateral_sql = blocks

    with conn.cursor() as cur:
        cur.execute("CREATE TABLE readings (device int, ts timestamptz, v float8) USING pgcolumnar")
        cur.execute("CREATE TABLE devices (device int PRIMARY KEY)")
        cur.execute(f"INSERT INTO devices SELECT g FROM generate_series(1, {DEVICES}) g")
        cur.execute(
            f"INSERT INTO readings SELECT 1 + (h % {DEVICES}), '2025-12-01'::timestamptz"
            f" + (h % {ROWS}) * interval '30 s', (h % 613)::float8"
            f" FROM (SELECT (hashint8(g) & 9223372036854775807) AS h"
            f"       FROM generate_series(1::bigint, {ROWS}) g) x"
        )
    expect.num(_one(conn, "SELECT count(*) FROM readings"), ROWS,
               "premise: the table holds every row")

    oracle = _rows(conn, "SELECT DISTINCT ON (device) device, ts FROM readings"
                         " ORDER BY device, ts DESC")
    pages = _one(conn, "SELECT pg_relation_size('readings') / 8192")

    trap = _blocks(conn, lateral_sql)
    print(f"-- table {pages} pages; LATERAL without the btree read {trap} blocks")
    expect.at_least(trap, pages * DEVICES // 2,
                    "without the btree the LATERAL form reads the table once per device")

    expect.text(str(_rows(conn, group_sql)), str(oracle),
                "the GROUP BY form returns what DISTINCT ON returns")
    # The document says this form reads the table once (#1339). Rows alone cannot tell
    # it from a per-device subquery, which returns the same rows and reads the table
    # once per device. Reading nothing also reads under the table, so a zero is refused.
    grp = _blocks(conn, group_sql)
    print(f"-- table {pages} pages; the GROUP BY form read {grp} blocks")
    expect.at_least(pages + 1 - grp if grp else 0, 1,
                    "the GROUP BY form reads the table once")

    with conn.cursor() as cur:
        cur.execute(index_sql)
    expect.num(_one(conn, "SELECT count(*) FROM pg_indexes WHERE tablename = 'readings'"), 1,
               "premise: the document's index exists")
    expect.text(str(_rows(conn, lateral_sql)), str(oracle),
                "the LATERAL form returns what DISTINCT ON returns")
    lat = _blocks(conn, lateral_sql)
    print(f"-- with the btree the LATERAL form read {lat} blocks")
    # Reading nothing also reads under a tenth of the table, so a zero is refused.
    expect.at_least(lat, 1, "premise: the indexed LATERAL form read the table at all")
    expect.at_least(pages - 10 * lat, 1,
                    "with the btree the LATERAL form reads under a tenth of the table")
