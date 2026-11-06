"""The first thing anyone runs: a columnar table stores rows and gives them back (#1). The
pytest twin of `test/smoke.sh`, every check by the shell's name (#1341).

Asserted: 100,000 rows round-trip through count, a filter and an ordered LIMIT; a table
with a NULL keeps it as NULL and keeps its neighbours' values; DROP TABLE leaves no
row-group metadata behind; and the native catalog tables exist.

The shell starts a throwaway cluster of its own with pgcolumnar preloaded. The twin uses
the harness cluster, which preloads it too, on a private database, so the orphan count
reads only what this module created. A failed step stops the fixture, as the shell's
`set -e` stops the script: every reading is a value or a query_error().
"""
import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_smoke"


@pytest.fixture(scope="module")
def sm(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        r = {}
        try:
            with psycopg.connect(pgc_cluster.dsn(dbname=DB), autocommit=True) as conn:
                one = lambda sql: conn.execute(sql).fetchone()[0]
                conn.execute("CREATE EXTENSION pgcolumnar")
                conn.execute("CREATE TABLE t (a int, b text) USING pgcolumnar")
                conn.execute("INSERT INTO t SELECT g, g::text FROM generate_series(1, 100000) g")
                r["total"] = one("SELECT count(*) FROM t")
                r["filtered"] = one("SELECT count(*) FROM t WHERE a < 50")
                r["first3"] = "".join(f"{row[0]}," for row in
                                      conn.execute("SELECT a || '|' || b FROM t ORDER BY a LIMIT 3"))
                r["nulls"] = one("SELECT count(*) FROM t WHERE b IS NULL")
                conn.execute("CREATE TABLE n (a int, b text) USING pgcolumnar")
                conn.execute("INSERT INTO n VALUES (1,'x'),(2,NULL),(3,'z')")
                r["nrows"] = one("SELECT count(*) FROM n")
                r["nnull"] = one("SELECT count(*) FROM n WHERE b IS NULL")
                r["nval"] = one("SELECT b FROM n WHERE a = 3")
                conn.execute("DROP TABLE t")
                conn.execute("DROP TABLE n")
                r["orphans"] = one("SELECT count(*) FROM pgcolumnar.row_group")
                r["native"] = one("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                                  "WHERE n.nspname='pgcolumnar' AND c.relkind='r' "
                                  "AND c.relname IN ('storage','row_group','column_chunk','zone_map')")
        except psycopg.Error as exc:
            failed = query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
            for k in ("total", "filtered", "first3", "nulls", "nrows", "nnull", "nval", "orphans", "native"):
                r.setdefault(k, failed)
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_count_star(sm, expect):
    expect.num(sm["total"], 100000, "count(*)")


def test_count_where_a_lt_50(sm, expect):
    expect.num(sm["filtered"], 49, "count where a<50")


def test_order_by_a_limit_3(sm, expect):
    expect.text(sm["first3"], "1|1,2|2,3|3,", "order by a limit 3")


def test_no_nulls_in_t_b(sm, expect):
    expect.num(sm["nulls"], 0, "no nulls in t.b")


def test_null_table_rows(sm, expect):
    expect.num(sm["nrows"], 3, "null table rows")


def test_null_table_nulls(sm, expect):
    expect.num(sm["nnull"], 1, "null table nulls")


def test_null_table_value(sm, expect):
    expect.text(sm["nval"], "z", "null table value")


def test_orphan_stripes(sm, expect):
    expect.num(sm["orphans"], 0, "orphan stripes")


def test_native_catalog_tables(sm, expect):
    expect.num(sm["native"], 4, "native catalog tables")
