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

ON A PRIVATE DATABASE, built once by a module fixture: the document names its
tables, `readings` and `devices`, so they cannot carry a per-test prefix. The fixture
runs every step in the shell's order and records each reading; each test asserts one,
so one red cannot stop the next from running (#1339, as #1517-#1519 do). Independent of
`test/latest_reading.sh`: its own extraction of the document (Python, not awk),
its own data, device count and row count.
"""

import pathlib
import re

import psycopg
import pytest

from pgc_vacuity import query_error

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


def _try(fn, *args):
    """A reading, or the error that replaced it: a failed step reddens its own test
    rather than erroring the fixture and every test with it."""
    try:
        return fn(*args)
    except psycopg.Error as exc:
        return query_error(exc.diag.message_primary)


@pytest.fixture(scope="module")
def recipe(pgc_cluster):
    """-> the readings, taken in the shell's order: the trap before the document's
    index exists, the indexed form after."""
    db = "pgc_latest_reading"
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {db}")
    got = {"blocks": _recipe_blocks()}
    try:
        with psycopg.connect(pgc_cluster.dsn(dbname=db), autocommit=True) as conn:
            conn.execute("CREATE EXTENSION pgcolumnar")
            conn.execute("CREATE TABLE readings (device int, ts timestamptz, v float8) USING pgcolumnar")
            conn.execute("CREATE TABLE devices (device int PRIMARY KEY)")
            conn.execute(f"INSERT INTO devices SELECT g FROM generate_series(1, {DEVICES}) g")
            _try(conn.execute,
                 f"INSERT INTO readings SELECT 1 + (h % {DEVICES}), '2025-12-01'::timestamptz"
                 f" + (h % {ROWS}) * interval '30 s', (h % 613)::float8"
                 f" FROM (SELECT (hashint8(g) & 9223372036854775807) AS h"
                 f"       FROM generate_series(1::bigint, {ROWS}) g) x")
            got["count"] = _try(_one, conn, "SELECT count(*) FROM readings")
            got["oracle"] = str(_try(_rows, conn, "SELECT DISTINCT ON (device) device, ts FROM readings"
                                                  " ORDER BY device, ts DESC"))
            got["pages"] = _try(_one, conn, "SELECT pg_relation_size('readings') / 8192")
            if len(got["blocks"]) == 3:
                index_sql, group_sql, lateral_sql = got["blocks"]
                got["trap"] = _try(_blocks, conn, lateral_sql)
                got["group rows"] = str(_try(_rows, conn, group_sql))
                got["group blocks"] = _try(_blocks, conn, group_sql)
                _try(conn.execute, index_sql)
                got["indexes"] = _try(_one, conn, "SELECT count(*) FROM pg_indexes WHERE tablename = 'readings'")
                got["lateral rows"] = str(_try(_rows, conn, lateral_sql))
                got["lateral blocks"] = _try(_blocks, conn, lateral_sql)
            print(f"-- table {got['pages']} pages; readings: "
                  + ", ".join(f"{k}={got.get(k)}" for k in ("trap", "group blocks", "lateral blocks")))
            yield got
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


def _int(v, what=""):
    """A block count, or 0 with the error printed, so an error or an absent step
    fails the bound it was meant to meet by name, with its reason in the output."""
    if isinstance(v, int):
        return v
    print(f"-- {what or 'reading'}: not a count: {v}")
    return 0


def _num(v, what=""):
    """A count for expect.num, or -1 with the error printed: expect.num refuses a
    non-number with a VacuityError rather than a named red (#1522 review)."""
    return v if isinstance(v, int) else _int(v, what) - 1


def test_the_documents_three_recipe_blocks_were_found(recipe, expect):
    expect.num(len(recipe["blocks"]), 3, "premise: the document's three recipe blocks were found")


def test_the_table_holds_every_row(recipe, expect):
    expect.num(_num(recipe["count"], "count"), ROWS, "premise: the table holds every row")


def test_without_the_btree_the_lateral_form_reads_the_table_once_per_device(recipe, expect):
    expect.at_least(_int(recipe.get("trap")), _int(recipe["pages"], "pages") * DEVICES // 2,
                    "without the btree the LATERAL form reads the table once per device")


def test_the_group_by_form_returns_what_distinct_on_returns(recipe, expect):
    expect.text(recipe.get("group rows", "absent"), recipe["oracle"],
                "the GROUP BY form returns what DISTINCT ON returns")


def test_the_group_by_form_reads_the_table_once(recipe, expect):
    # The document says this form reads the table once (#1339). Rows alone cannot tell
    # it from a per-device subquery, which returns the same rows and reads the table
    # once per device. Reading nothing also reads under the table, so a zero is refused.
    grp = _int(recipe.get("group blocks"))
    expect.at_least(_int(recipe["pages"], "pages") + 1 - grp if grp else 0, 1,
                    "the GROUP BY form reads the table once")


def test_the_documents_index_exists(recipe, expect):
    expect.num(_num(recipe.get("indexes", 0), "indexes"), 1, "premise: the document's index exists")


def test_the_lateral_form_returns_what_distinct_on_returns(recipe, expect):
    expect.text(recipe.get("lateral rows", "absent"), recipe["oracle"],
                "the LATERAL form returns what DISTINCT ON returns")


def test_the_indexed_lateral_form_read_the_table_at_all(recipe, expect):
    # Reading nothing also reads under a tenth of the table, so a zero is refused.
    expect.at_least(_int(recipe.get("lateral blocks")), 1,
                    "premise: the indexed LATERAL form read the table at all")


def test_with_the_btree_the_lateral_form_reads_under_a_tenth_of_the_table(recipe, expect):
    lat = _int(recipe.get("lateral blocks"))
    expect.at_least(_int(recipe["pages"], "pages") - 10 * lat if lat else 0, 1,
                    "with the btree the LATERAL form reads under a tenth of the table")
