"""Indexes and constraints on a columnar table: the pytest twin of `test/phase4.sh`,
every check by the shell's name (#1341).

Btree and hash indexes are built over a columnar table and read back, a delete is
honoured by an index read, unique and primary-key duplicates are refused across
statements and within one, NOT NULL and CHECK refuse their rows, a heap table
converts to columnar and back with its values, and with
pgcolumnar.enable_index_only_scan off a covering query plans a plain index scan.

EVERY INDEX READ GOES THROUGH THE INDEX FETCH. With only enable_seqscan off the
planner answers a point lookup with the columnar custom scan, which never calls
pgcolumnar_index_fetch_tuple, and a count with an index-only scan. So the reads set
IDX, the settings the plan checks assert under, and an index fetch that hands back
the wrong row reddens the point reads by name.

A REFUSAL IS ITS OWN SQLSTATE: 23505 for a duplicate key, 23502 for a NULL in a NOT
NULL column, 23514 for a CHECK. A failure of any other kind, or a backend that died
on the statement, is a reading that does not match.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each
test asserts one reading (#1339).
"""
import psycopg
import pytest

from pgc_vacuity import QUERY_ERROR, query_error

DB = "pgc_phase4"
IDX = ("SET enable_seqscan = off", "SET pgcolumnar.enable_custom_scan = off",
       "SET pgcolumnar.enable_index_only_scan = off")
SEQOFF = ("SET enable_seqscan = off",)
AM = "SELECT amname FROM pg_am m JOIN pg_class c ON c.relam = m.oid WHERE c.relname = '{}'"


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _run(dsn, sql):
    """A setup step. Its failure is not swallowed into a pass: every reading after it
    measures the state it left, and the readings are what the tests assert."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.encode())
    except psycopg.Error as exc:
        return _message(exc)
    return ""


def _value(dsn, sql, sets=()):
    """-> the first column of the first row as text, or a failed reading."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for s in sets:
                conn.execute(s)
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return query_error(f"no value from {sql}") if row is None or row[0] is None else str(row[0])


def _plan(dsn, sql, sets=()):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for s in sets:
                conn.execute(s)
            return "\n".join(r[0] for r in conn.execute("EXPLAIN (COSTS OFF) " + sql))
    except psycopg.Error as exc:
        return query_error(_message(exc))


def _index_scan(plan):
    """-> "Index Scan" when the plan is a plain index scan, else what it was."""
    if str(plan).startswith(QUERY_ERROR):
        return plan
    if "Index Scan" in plan and "Index Only Scan" not in plan:
        return "Index Scan"
    return query_error("plan was: " + " | ".join(l.strip() for l in plan.splitlines()))


def _refusal(dsn, sql):
    """-> the SQLSTATE the statement failed with, "succeeded", or a failed reading
    when no SQLSTATE came back (a lost connection carries none)."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)
    except psycopg.Error as exc:
        return exc.sqlstate if exc.sqlstate else query_error("no SQLSTATE: " + _message(exc))
    return "succeeded"


@pytest.fixture(scope="module")
def p4(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        v = lambda sql, sets=(): _value(dsn, sql, sets)
        r = {}
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")

        _run(dsn, "CREATE TABLE bt (a int, b text) USING pgcolumnar; "
                  "INSERT INTO bt SELECT g, 'r'||g FROM generate_series(1,20000) g; "
                  "CREATE INDEX bt_a_idx ON bt (a)")
        r["bt plan"] = _index_scan(_plan(dsn, "SELECT b FROM bt WHERE a = 12345", IDX))
        r["bt point"] = v("SELECT b FROM bt WHERE a = 12345", IDX)
        r["bt range count"] = v("SELECT count(*) FROM bt WHERE a BETWEEN 100 AND 199", IDX)
        r["bt range first"] = v("SELECT b FROM bt WHERE a BETWEEN 100 AND 199 ORDER BY a LIMIT 1", IDX)
        r["bt miss"] = v("SELECT count(*) FROM bt WHERE a = 999999", IDX)

        _run(dsn, "CREATE TABLE ht (a int, b text) USING pgcolumnar; "
                  "INSERT INTO ht SELECT g, 'h'||g FROM generate_series(1,20000) g; "
                  "CREATE INDEX ht_a_hash ON ht USING hash (a)")
        r["ht plan"] = _index_scan(_plan(dsn, "SELECT b FROM ht WHERE a = 5000", IDX))
        r["ht point"] = v("SELECT b FROM ht WHERE a = 5000", IDX)
        r["ht point2"] = v("SELECT b FROM ht WHERE a = 17777", IDX)
        r["ht miss"] = v("SELECT count(*) FROM ht WHERE a = 999999", IDX)

        _run(dsn, "CREATE TABLE uq (id int PRIMARY KEY, v text) USING pgcolumnar; "
                  "INSERT INTO uq VALUES (1,'a'),(2,'b'),(3,'c')")
        r["uq initial"] = v("SELECT count(*) FROM uq")
        r["uq cross"] = _refusal(dsn, "INSERT INTO uq VALUES (2, 'dup')")
        r["uq within"] = _refusal(dsn, "INSERT INTO uq VALUES (10,'x'),(10,'y')")
        r["uq unchanged"] = v("SELECT count(*) FROM uq")
        _run(dsn, "INSERT INTO uq VALUES (4,'d')")
        r["uq new row"] = v("SELECT v FROM uq WHERE id = 4", IDX)
        r["uq final"] = v("SELECT count(*) FROM uq")
        _run(dsn, "CREATE TABLE uq2 (id int, v text) USING pgcolumnar; "
                  "CREATE UNIQUE INDEX uq2_id ON uq2 (id); "
                  "INSERT INTO uq2 SELECT g, 'u'||g FROM generate_series(1,5000) g")
        r["uq2 dup"] = _refusal(dsn, "INSERT INTO uq2 VALUES (2500, 'dup')")
        r["uq2 count"] = v("SELECT count(*) FROM uq2")

        _run(dsn, "CREATE TABLE ck (a int NOT NULL, b int CHECK (b > 0)) USING pgcolumnar; "
                  "INSERT INTO ck VALUES (1, 10), (2, 20)")
        r["not null"] = _refusal(dsn, "INSERT INTO ck VALUES (NULL, 5)")
        r["check"] = _refusal(dsn, "INSERT INTO ck VALUES (3, -1)")
        r["ck unchanged"] = v("SELECT count(*) FROM ck")

        _run(dsn, "CREATE TABLE di (a int, b text) USING pgcolumnar; "
                  "INSERT INTO di SELECT g, 'd'||g FROM generate_series(1,20000) g; "
                  "CREATE INDEX di_a_idx ON di (a)")
        _run(dsn, "DELETE FROM di WHERE a BETWEEN 5000 AND 5100")
        r["di plan"] = _index_scan(_plan(dsn, "SELECT count(*) FROM di WHERE a BETWEEN 4900 AND 5200", IDX))
        r["deleted point"] = v("SELECT count(*) FROM di WHERE a = 5050", IDX)
        r["deleted range"] = v("SELECT count(*) FROM di WHERE a BETWEEN 5000 AND 5100", IDX)
        r["survivors"] = v("SELECT count(*) FROM di WHERE a BETWEEN 4900 AND 5200", IDX)
        r["neighbor"] = v("SELECT b FROM di WHERE a = 4999", IDX)
        _run(dsn, "UPDATE di SET a = a + 1000000 WHERE a = 8000")
        r["old gone"] = v("SELECT count(*) FROM di WHERE a = 8000", IDX)
        r["new found"] = v("SELECT b FROM di WHERE a = 1008000", IDX)

        _run(dsn, "CREATE TABLE conv (a int, b text); "
                  "INSERT INTO conv SELECT g, 'v'||g FROM generate_series(1,10000) g")
        r["conv am0"] = v(AM.format("conv"))
        _run(dsn, "SELECT pgcolumnar.alter_table_set_access_method('conv', 'pgcolumnar')")
        r["conv am1"] = v(AM.format("conv"))
        r["conv col count"] = v("SELECT count(*) FROM conv")
        r["conv col value"] = v("SELECT b FROM conv WHERE a = 7777")
        r["conv col sum"] = v("SELECT sum(a) FROM conv")
        _run(dsn, "SELECT pgcolumnar.alter_table_set_access_method('conv', 'heap')")
        r["conv am2"] = v(AM.format("conv"))
        r["conv heap count"] = v("SELECT count(*) FROM conv")
        r["conv heap value"] = v("SELECT b FROM conv WHERE a = 7777")
        r["conv heap sum"] = v("SELECT sum(a) FROM conv")

        _run(dsn, "CREATE TABLE ios (a int, b int) USING pgcolumnar; "
                  "INSERT INTO ios SELECT g, g*2 FROM generate_series(1,20000) g; "
                  "CREATE INDEX ios_a_idx ON ios (a)")
        r["ios off"] = _index_scan(_plan(dsn, "SELECT a FROM ios WHERE a = 100", IDX))
        r["covering"] = v("SELECT a FROM ios WHERE a = 100", SEQOFF)
        full = _plan(dsn, "SELECT * FROM ios WHERE a = 100",
                     ("SET enable_indexscan = off", "SET enable_bitmapscan = off"))
        r["full"] = full if str(full).startswith(QUERY_ERROR) else (
            "available" if ("Seq Scan" in full or "Custom Scan (PgColumnarScan)" in full)
            else query_error("plan was: " + " | ".join(l.strip() for l in full.splitlines())))
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


# ---- btree ----------------------------------------------------------------------

def test_btree_plan_is_index_scan(p4, expect):
    expect.text(p4["bt plan"], "Index Scan", "btree plan is index scan")


def test_btree_point(p4, expect):
    expect.text(p4["bt point"], "r12345", "btree point")


def test_btree_range_count(p4, expect):
    expect.text(p4["bt range count"], "100", "btree range count")


def test_btree_range_first(p4, expect):
    expect.text(p4["bt range first"], "r100", "btree range first")


def test_btree_miss(p4, expect):
    expect.text(p4["bt miss"], "0", "btree miss")


# ---- hash -----------------------------------------------------------------------

def test_hash_plan_is_index_scan(p4, expect):
    expect.text(p4["ht plan"], "Index Scan", "hash plan is index scan")


def test_hash_point(p4, expect):
    expect.text(p4["ht point"], "h5000", "hash point")


def test_hash_point2(p4, expect):
    expect.text(p4["ht point2"], "h17777", "hash point2")


def test_hash_miss(p4, expect):
    expect.text(p4["ht miss"], "0", "hash miss")


# ---- unique / primary key ---------------------------------------------------------

def test_uq_initial(p4, expect):
    expect.text(p4["uq initial"], "3", "uq initial")


def test_uq_cross_statement_duplicate_refused(p4, expect):
    expect.text(p4["uq cross"], "23505", "uq cross-stmt dup")


def test_uq_within_statement_duplicate_refused(p4, expect):
    expect.text(p4["uq within"], "23505", "uq within-stmt dup")


def test_uq_unchanged(p4, expect):
    expect.text(p4["uq unchanged"], "3", "uq unchanged")


def test_uq_new_row(p4, expect):
    expect.text(p4["uq new row"], "d", "uq new row")


def test_uq_final_count(p4, expect):
    expect.text(p4["uq final"], "4", "uq final count")


def test_uq2_duplicate_refused(p4, expect):
    expect.text(p4["uq2 dup"], "23505", "uq2 dup")


def test_uq2_count(p4, expect):
    expect.text(p4["uq2 count"], "5000", "uq2 count")


# ---- NOT NULL and CHECK -------------------------------------------------------------

def test_not_null_refused(p4, expect):
    expect.text(p4["not null"], "23502", "not null reject")


def test_check_refused(p4, expect):
    expect.text(p4["check"], "23514", "check reject")


def test_ck_unchanged(p4, expect):
    expect.text(p4["ck unchanged"], "2", "ck unchanged")


# ---- an index read honours the delete vector ----------------------------------------

def test_deleted_range_plan_is_index_scan(p4, expect):
    expect.text(p4["di plan"], "Index Scan", "deleted range plan is index scan")


def test_deleted_point(p4, expect):
    expect.text(p4["deleted point"], "0", "deleted point")


def test_deleted_range(p4, expect):
    expect.text(p4["deleted range"], "0", "deleted range")


def test_survivors_around(p4, expect):
    expect.text(p4["survivors"], "200", "survivors around")


def test_neighbor_present(p4, expect):
    expect.text(p4["neighbor"], "d4999", "neighbor present")


def test_updated_old_gone(p4, expect):
    expect.text(p4["old gone"], "0", "updated old gone")


def test_updated_new_found(p4, expect):
    expect.text(p4["new found"], "d8000", "updated new found")


# ---- heap <-> columnar conversion ------------------------------------------------

def test_conv_starts_heap(p4, expect):
    expect.text(p4["conv am0"], "heap", "conv starts heap")


def test_conv_now_columnar(p4, expect):
    expect.text(p4["conv am1"], "pgcolumnar", "conv now columnar")


def test_conv_col_count(p4, expect):
    expect.text(p4["conv col count"], "10000", "conv col count")


def test_conv_col_value(p4, expect):
    expect.text(p4["conv col value"], "v7777", "conv col value")


def test_conv_col_sum(p4, expect):
    expect.text(p4["conv col sum"], "50005000", "conv col sum")


def test_conv_back_heap(p4, expect):
    expect.text(p4["conv am2"], "heap", "conv back heap")


def test_conv_heap_count(p4, expect):
    expect.text(p4["conv heap count"], "10000", "conv heap count")


def test_conv_heap_value(p4, expect):
    expect.text(p4["conv heap value"], "v7777", "conv heap value")


def test_conv_heap_sum(p4, expect):
    expect.text(p4["conv heap sum"], "50005000", "conv heap sum")


# ---- index-only scans turned off -----------------------------------------------------

def test_ios_off_plain_index_scan(p4, expect):
    expect.text(p4["ios off"], "Index Scan", "IOS off: plain index scan")


def test_covering_value(p4, expect):
    expect.text(p4["covering"], "100", "covering value")


def test_full_table_scan_available(p4, expect):
    expect.text(p4["full"], "available", "full-table scan available")
