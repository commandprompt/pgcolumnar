"""The native round trip (Phase D3). The pytest twin of `test/native_roundtrip.sh`, every
check by the shell's name.

A native-format table is read back through the native reader and must match a heap
mirror: scalars with nulls across five row groups, a filter, a projection, an
aggregate and a filtered count, compared order-independently so the heap is the
oracle. Each check is its own test.
"""
import psycopg
import pytest

HASH = ("SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), $e$EMPTY$e$) "
        "FROM (SELECT _row::text AS t FROM ({}) _row) _s")
GEN = """SELECT g,
       (g * 100000)::bigint,
       CASE WHEN g % 9 = 0 THEN NULL ELSE 'row-' || g END,
       CASE WHEN g % 6 = 0 THEN NULL ELSE (g::float8 / 7) END,
       (g % 2 = 0)
  FROM generate_series(1, 5000) g"""


def _one(c, sql):
    try:
        return str(c.execute(sql).fetchone()[0])
    except psycopg.Error as exc:
        return f"error: {exc.diag.message_primary}"


@pytest.fixture(scope="module")
def rt(pgc_cluster):
    db = "pgc_roundtrip"
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {db}")
    try:
        with psycopg.connect(pgc_cluster.dsn(dbname=db), autocommit=True) as c:
            c.execute("CREATE EXTENSION pgcolumnar")
            c.execute("CREATE TABLE h (id int, k bigint, v text, f float8, b bool)")
            c.execute("CREATE TABLE n (id int, k bigint, v text, f float8, b bool) USING pgcolumnar")
            # a small row-group limit, so the read crosses several row groups
            c.execute("SELECT pgcolumnar.set_options('n', stripe_row_limit => 1000)")
            c.execute(f"INSERT INTO h {GEN}")
            c.execute(f"INSERT INTO n {GEN}")
            yield c
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


def _pair(c, tmpl):
    """-> (columnar, heap) answers of `tmpl` with {t} for the table."""
    return _one(c, tmpl.format(t="n")), _one(c, tmpl.format(t="h"))


# one check per test, by the shell's name: one red cannot stop the next (#1518 review)
READS = (("native reads back all columns", "SELECT id, k, v, f, b FROM {t}"),
         ("native filtered scan", "SELECT id, v FROM {t} WHERE k BETWEEN 100000000 AND 200000000"),
         ("native projection (subset of columns)", "SELECT v, b FROM {t}"))
ANSWERS = (("native null handling (v)", "SELECT count(*) FROM {t} WHERE v IS NULL"),
           ("native null handling (f)", "SELECT count(*) FROM {t} WHERE f IS NULL"),
           ("native aggregate (sum over a scanned column)", "SELECT sum(k) FROM {t}"),
           ("native count with filter", "SELECT count(*) FROM {t} WHERE b"))


def test_native_row_count(rt, expect):
    # answered from metadata, so a reader that drops a row group still counts 5000
    expect.text(_one(rt, "SELECT count(*) FROM n"), "5000", "native row count")


def test_the_native_table_holds_five_row_groups(rt, expect):
    expect.text(_one(rt, "SELECT count(*) FROM pgcolumnar.row_group "
                         "WHERE storage_id = pgcolumnar.get_storage_id('n')"), "5",
                "the native table holds five row groups")


@pytest.mark.parametrize("name,sql", READS, ids=[n for n, _ in READS])
def test_a_read_matches_the_heap(rt, expect, name, sql):
    expect.text(*_pair(rt, HASH.format(sql)), name)


@pytest.mark.parametrize("name,sql", ANSWERS, ids=[n for n, _ in ANSWERS])
def test_a_null_count_or_aggregate_matches_the_heap(rt, expect, name, sql):
    expect.text(*_pair(rt, sql), name)
