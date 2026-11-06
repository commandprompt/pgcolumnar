"""DELETE, UPDATE and MERGE on a native-format columnar table agree with a heap mirror
(#1341). The pytest twin of `test/native_dml.sh`, every check by the shell's name.

Each statement runs on a heap table and on a columnar one with stripes of 1000 rows,
and every result is compared: the surviving rows, counts, aggregates and index lookups.
After deletes, an aggregate must not be answered from zone maps that still describe the
deleted rows. The deletes include the table's smallest and largest ids, so min and max
move and an answer that ignored the deletes differs from heap's; min and max are taken
over k too, a column the primary key does not order. An index lookup of a deleted row
must find nothing, and the lookups are asserted to take the index.
"""


def _one(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


def _row(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()


def _rows(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def _run(conn, *sqls):
    with conn.cursor() as cur:
        for sql in sqls:
            cur.execute(sql)


GEN = "SELECT g, (g * 10)::bigint, 'row-' || g FROM generate_series(1, 5000) g"


def test_delete_update_and_merge_match_heap(pgc_conn, expect):
    conn = pgc_conn
    _run(conn, "CREATE TABLE h (id int PRIMARY KEY, k bigint, v text)",
         "CREATE TABLE n (id int PRIMARY KEY, k bigint, v text) USING pgcolumnar",
         "SELECT pgcolumnar.set_options('n', stripe_row_limit => 1000)",
         f"INSERT INTO h {GEN}", f"INSERT INTO n {GEN}")

    for t in ("h", "n"):
        _run(conn, f"DELETE FROM {t} WHERE id % 7 = 0 OR id IN (1, 5000)")
    expect.text(_one(conn, "SELECT min(id) || '|' || max(id) || '|' || min(k) || '|' || max(k) FROM h"),
                "2|4999|20|49990", "premise: the deletes moved the smallest and largest id and k")
    expect.num(_one(conn, "SELECT count(*) FROM n"), _one(conn, "SELECT count(*) FROM h"), "count after delete")
    expect.row_set(_rows(conn, "SELECT id, k, v FROM n"), _rows(conn, "SELECT id, k, v FROM h"),
                   "rows after delete match heap")
    expect.num(_one(conn, "SELECT sum(k) FROM n"), _one(conn, "SELECT sum(k) FROM h"), "sum(k) after delete")
    expect.rows([_row(conn, "SELECT min(id), max(id), min(k), max(k) FROM n")],
                [_row(conn, "SELECT min(id), max(id), min(k), max(k) FROM h")], "min/max after delete")
    expect.num(_one(conn, "SELECT count(*) FROM n WHERE id % 7 = 0"), 0, "deleted rows gone")

    # id 4444 is live (4444 % 7 = 6); id 4445 was deleted (4445 % 7 = 0)
    _run(conn, "SET enable_seqscan = off")
    plan = "\n".join(r[0] for r in _rows(conn, "EXPLAIN (COSTS off) SELECT count(*) FROM n WHERE id = 4445"))
    expect.num(sum(1 for line in plan.splitlines() if "Index" in line and "using n_pkey" in line), 1,
               "premise: the lookups take the primary-key index")
    expect.text(_one(conn, "SELECT v FROM n WHERE id = 4444"), _one(conn, "SELECT v FROM h WHERE id = 4444"),
                "index finds live row")
    expect.num(_one(conn, "SELECT count(*) FROM n WHERE id = 4445"), 0, "index skips deleted row")
    _run(conn, "RESET enable_seqscan")

    for t in ("h", "n"):
        _run(conn, f"UPDATE {t} SET k = k + 1 WHERE id % 5 = 0")
    expect.num(_one(conn, "SELECT count(*) FROM n"), _one(conn, "SELECT count(*) FROM h"), "count after update")
    expect.row_set(_rows(conn, "SELECT id, k, v FROM n"), _rows(conn, "SELECT id, k, v FROM h"),
                   "rows after update match heap")
    expect.num(_one(conn, "SELECT sum(k) FROM n"), _one(conn, "SELECT sum(k) FROM h"), "sum(k) after update")

    import psycopg
    try:
        _run(conn, "INSERT INTO n VALUES (4444, 0, 'x')")
        dup = "accepted"
    except psycopg.Error as exc:
        dup = exc.sqlstate
    expect.text(dup, "23505", "PK still enforced after DML")

    _run(conn, "CREATE TABLE src (id int, k bigint)", "INSERT INTO src VALUES (1, 999), (999001, 5), (999002, 6)")
    for t in ("h", "n"):
        _run(conn, f"MERGE INTO {t} t USING src s ON t.id = s.id "
                   "WHEN MATCHED THEN UPDATE SET k = s.k "
                   "WHEN NOT MATCHED THEN INSERT (id, k, v) VALUES (s.id, s.k, 'm')")
    expect.num(_one(conn, "SELECT count(*) FROM n"), _one(conn, "SELECT count(*) FROM h"), "count after merge")
    expect.row_set(_rows(conn, "SELECT id, k, v FROM n"), _rows(conn, "SELECT id, k, v FROM h"),
                   "rows after merge match heap")
