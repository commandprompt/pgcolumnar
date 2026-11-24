"""A columnar table writes the native format, judged by the catalog rows it produces,
and DROP clears them (#1341). The pytest twin of `test/native_writer.sh`, every check
by the shell's name.

5000 rows, a quarter of them NULL in v, under a 1000-row stripe limit: one storage row
at format version 2 with 1024-value vectors, five row groups holding 5000 rows, and ten
column chunks (five groups, two columns) counting 10000 values. Dropping the table
deletes the storage, row-group and column-chunk rows for its storage id. The first test
reads no data back; native_roundtrip does that.

The second test is #2073: the writer refuses row numbers past the item-pointer
range rather than wrapping the block number, and pgcolumnar.row_tid refuses them too.
It does read the table back, to show that no row number wrapped.
"""

import psycopg
import pytest


def _one(cur, sql):
    cur.execute(sql)
    return cur.fetchone()[0]


def test_the_native_writer_by_its_catalog_rows(pgc_conn, expect):
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE nw (id int, v text) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('nw', stripe_row_limit => 1000)")
        cur.execute("INSERT INTO nw SELECT g, CASE WHEN g % 4 = 0 THEN NULL ELSE 'v'||g END "
                    "FROM generate_series(1, 5000) g")
        sid = _one(cur, "SELECT pgcolumnar.get_storage_id('nw')")
        of = lambda sql: _one(cur, sql.format(sid=sid))  # noqa: E731
        expect.num(of("SELECT count(*) FROM pgcolumnar.storage WHERE storage_id = {sid}"), 1,
                   "native storage row written")
        expect.num(of("SELECT format_version FROM pgcolumnar.storage WHERE storage_id = {sid}"), 2,
                   "native format version recorded")
        expect.num(of("SELECT vector_length FROM pgcolumnar.storage WHERE storage_id = {sid}"), 1024,
                   "native vector length recorded")
        expect.num(of("SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = {sid}"), 5,
                   "native row groups (5000 rows / 1000 limit)")
        expect.num(int(of("SELECT sum(row_count) FROM pgcolumnar.row_group WHERE storage_id = {sid}")), 5000,
                   "native row-group row total")
        expect.num(of("SELECT count(*) FROM pgcolumnar.column_chunk WHERE storage_id = {sid}"), 10,
                   "native column chunks (5 groups x 2 cols)")
        expect.num(int(of("SELECT sum(value_count) FROM pgcolumnar.column_chunk WHERE storage_id = {sid}")), 10000,
                   "native column-chunk value total")

        cur.execute("DROP TABLE nw")
        expect.num(of("SELECT count(*) FROM pgcolumnar.storage WHERE storage_id = {sid}"), 0,
                   "drop clears native storage row")
        expect.num(of("SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = {sid}"), 0,
                   "drop clears native row groups")
        expect.num(of("SELECT count(*) FROM pgcolumnar.column_chunk WHERE storage_id = {sid}"), 0,
                   "drop clears native column chunks")


def test_row_numbers_past_the_item_pointer_range_are_refused(pgc_conn, expect):
    """#2073. A row number maps to an item pointer whose block number holds 32 bits,
    so there is a largest row number. Each transaction reserves stripe_row_limit row
    numbers. At stripe_row_limit 2147483647, single-row inserts in their own
    transactions use up the range after 582 rows, so the 583rd must be refused with
    54000 (program limit exceeded). Before the fix it was accepted: its block number
    wrapped, its item pointer fell below the rows before it, and an index scan
    missed it. pgcolumnar.vacuum renumbers the rows, so inserts work again after it.

    pgcolumnar.row_tid maps a row number by the same arithmetic, and any role may call
    it. It must refuse a row number at or past the limit with 22003, as it refuses one
    below 1. The largest row number, 1249835482844, is the last offset (291) of block
    MaxBlockNumber (4294967294). It is checked last: on a build with assertions, a
    row_tid without the check fails an Assert and restarts the server.
    """
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE nw_rn (id int, v text) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('nw_rn', stripe_row_limit => 2147483647)")
        cur.execute("CREATE INDEX nw_rn_id ON nw_rn (id)")
        # autocommit: every INSERT is its own transaction and its own reservation
        for i in range(1, 583):
            cur.execute("INSERT INTO nw_rn VALUES (%s, %s)", (i, f"v{i}"))
        with pytest.raises(psycopg.Error) as refused:
            cur.execute("INSERT INTO nw_rn VALUES (583, 'v583')")
        expect.sqlstate(refused.value, "54000",
                        "a reservation past the largest row number is refused (54000)")
        expect.num(_one(cur, "SELECT count(*) FROM nw_rn"), 582,
                   "the rows committed before the refusal are kept")
        expect.num(_one(cur, "SELECT count(*) FROM (SELECT ctid, lag(ctid) OVER (ORDER BY id) AS p "
                             "FROM nw_rn) s WHERE ctid <= p"), 0,
                   "no committed row's item pointer wraps below the row before it")
        cur.execute("BEGIN")
        for guc in ("pgcolumnar.enable_custom_scan", "enable_seqscan", "enable_bitmapscan",
                    "pgcolumnar.enable_index_fetch_penalty"):
            cur.execute(f"SET LOCAL {guc} = off")
        expect.num(_one(cur, "SELECT count(*) FROM nw_rn WHERE id > 0"), 582,
                   "an index scan finds every committed row")
        cur.execute("ROLLBACK")
        cur.execute("SELECT pgcolumnar.vacuum('nw_rn')")
        cur.execute("INSERT INTO nw_rn VALUES (601, 'after')")
        expect.text(_one(cur, "SELECT count(*) || '|' || count(*) FILTER (WHERE id = 601) FROM nw_rn"),
                    "583|1", "pgcolumnar.vacuum renumbers the rows, so inserts work again")
        cur.execute("DROP TABLE nw_rn")

        def row_tid(n):
            try:
                return str(_one(cur, f"SELECT pgcolumnar.row_tid({n})"))
            except psycopg.Error as e:
                return e.sqlstate
        expect.text(f"{row_tid(1249835482844)}|{row_tid(1249835482845)}|{row_tid(9223372036854775807)}",
                    "(4294967294,291)|22003|22003",
                    "row_tid maps the largest row number and refuses the ones past it (22003)")
