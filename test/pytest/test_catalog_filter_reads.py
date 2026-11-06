"""stats() and sort_status() read a table's storage id once, not once per catalog row
(#1637). The pytest twin of `test/catalog_filter_reads.sh`, every check by the shell's
name.

Both functions filtered a shared catalog with `storage_id =
pgcolumnar.get_storage_id(rel)`. In the filter of a sequential scan that STABLE call
runs once per catalog row, and each call reads the table's first page, so the cost grew
with every other columnar table in the database. Asserted, after ANALYZE, with 20 other
tables of 5 groups each: each call reads at most 5 blocks of the table, counted by
pg_stat_get_xact_blocks_fetched inside the call's own transaction in a fresh session.
Premises show the catalog is large enough and has statistics, and that the counter
counts. Controls show each function still reports only the table's own 3 groups.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order.
"""
import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_catalog_filter_reads"
OTHERS = 20
CEILING = 5


def _failed(exc):
    return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _value(dsn, sql):
    """-> the first column of the first row, or query_error() carrying the failure."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return _failed(exc)
    return None if row is None else row[0]


def _reads(dsn, sql):
    """-> the blocks of t this backend fetched while SQL ran, in SQL's own transaction
    and a fresh session, as the shell's reads(); query_error() when SQL failed."""
    try:
        with psycopg.connect(dsn) as conn:
            conn.execute(sql).fetchall()
            n = conn.execute("SELECT pg_stat_get_xact_blocks_fetched('t'::regclass)").fetchone()[0]
            conn.commit()
    except psycopg.Error as exc:
        return _failed(exc)
    return n


def _at_least(got, floor):
    """The shell's at_least: GOT when it falls short of FLOOR, else FLOOR; a non-number
    passes through, so expect.num refuses it."""
    return got if not isinstance(got, int) or got < floor else floor


def _at_most(got, ceiling):
    """The shell's at_most: GOT when it is over CEILING, else CEILING; a non-number
    passes through, so expect.num refuses it rather than reading it as in bound."""
    return got if not isinstance(got, int) or got > ceiling else ceiling


@pytest.fixture(scope="module")
def cfr(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            conn.execute("CREATE TABLE t (a int, b int) USING pgcolumnar")
            conn.execute("SELECT pgcolumnar.set_options('t', stripe_row_limit => 1000)")
            conn.execute("INSERT INTO t SELECT g, g FROM generate_series(1, 3000) g")
            for n in range(1, OTHERS + 1):
                conn.execute(f"CREATE TABLE o{n} (a int) USING pgcolumnar")
                # five statements, five row groups
                for _ in range(5):
                    conn.execute(f"INSERT INTO o{n} SELECT g FROM generate_series(1, 10) g")
            conn.execute("ANALYZE")
        r = {
            "groups": _value(dsn, "SELECT count(*) FROM pgcolumnar.row_group"),
            "stats tables": _value(dsn, "SELECT count(DISTINCT tablename)::text FROM pg_stats "
                                        "WHERE schemaname = 'pgcolumnar' "
                                        "AND tablename IN ('row_group', 'storage')"),
            "one call": _reads(dsn, "SELECT pgcolumnar.get_storage_id('t')"),
            "stats reads": _reads(dsn, "SELECT count(*) FROM pgcolumnar.stats('t')"),
            "stats groups": _value(dsn, "SELECT count(*) FROM pgcolumnar.stats('t')"),
            "sort_status reads": _reads(dsn, "SELECT * FROM pgcolumnar.sort_status('t')"),
            "sort_status groups": _value(dsn, "SELECT total_groups FROM pgcolumnar.sort_status('t')"),
        }
        print(f"\n-- stats('t') read {r['stats reads']} blocks of t"
              f"\n-- sort_status('t') read {r['sort_status reads']} blocks of t")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_the_catalog_holds_other_tables_row_groups(cfr, expect):
    expect.num(_at_least(cfr["groups"], 100), 100,
               "premise: other tables put at least 100 row groups in the catalog")


def test_the_catalogs_have_statistics(cfr, expect):
    expect.text(cfr["stats tables"], "2",
                "premise: pgcolumnar.row_group and pgcolumnar.storage have statistics")


def test_the_counter_counts_one_call(cfr, expect):
    expect.num(_at_least(cfr["one call"], 1), 1, "premise: one get_storage_id call reads t")


def test_stats_reads_the_storage_id_once(cfr, expect):
    expect.num(_at_most(cfr["stats reads"], CEILING), CEILING,
               "stats() reads the storage id once, not once per catalog row group")


def test_stats_reports_only_the_tables_groups(cfr, expect):
    expect.num(cfr["stats groups"], 3, "control: stats() reports t's 3 row groups and no other table's")


def test_sort_status_reads_the_storage_id_once(cfr, expect):
    expect.num(_at_most(cfr["sort_status reads"], CEILING), CEILING,
               "sort_status() reads the storage id once, not once per catalog row")


def test_sort_status_counts_only_the_tables_groups(cfr, expect):
    expect.num(cfr["sort_status groups"], 3,
               "control: sort_status() counts t's 3 row groups and no other table's")
