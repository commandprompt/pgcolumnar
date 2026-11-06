"""Native per-chunk bloom filters (Phase D5b). The pytest twin of `test/native_bloom.sh`,
every check by the shell's name.

A native table carries a bloom filter per column chunk over hashable columns, so an
equality probe on an unsorted column skips row groups that provably lack the value,
where min/max cannot. The values are spread so every group's range spans the domain:
groups are skipped only with the bloom on, and the answer matches a heap mirror.
"""
import re

import psycopg
import pytest

from pgc_vacuity import query_error

HASH = ("SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), $e$EMPTY$e$) "
        "FROM (SELECT _row::text AS t FROM ({}) _row) _s")
# a well-spread tag over a large domain: each 2048-row group's [min,max] spans almost
# all of it, so min/max cannot prune an in-range probe and the bloom can
GEN = "SELECT g, ((g * 104729) % 1000000) AS tag FROM generate_series(1, 20480) g"
REMOVED = re.compile(r"Columnar Chunk Groups Removed by Filter: (\d+)")


def _one(c, sql):
    try:
        return str(c.execute(sql).fetchone()[0])
    except psycopg.Error as exc:
        return query_error(exc.diag.message_primary)


def _skipped(c, sql, bloom):
    """Groups the scan removed with the bloom `bloom`, or -1 when the plan reports no
    counter: a plan without the columnar scan must not read as "removed nothing"."""
    c.execute(f"SET pgcolumnar.enable_bloom_filter = {bloom}")
    try:
        plan = "\n".join(r[0] for r in c.execute(
            f"EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) {sql}"))
    finally:
        c.execute("RESET pgcolumnar.enable_bloom_filter")
    m = REMOVED.search(plan)
    return int(m.group(1)) if m else -1


@pytest.fixture(scope="module")
def bloom(pgc_cluster):
    """-> (conn, probe): the two tables, and a tag that really occurs, from the heap."""
    db = "pgc_bloom"
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {db}")
    try:
        with psycopg.connect(pgc_cluster.dsn(dbname=db), autocommit=True) as c:
            c.execute("CREATE EXTENSION pgcolumnar")
            c.execute("CREATE TABLE h (id int, tag int)")
            c.execute("CREATE TABLE n (id int, tag int) USING pgcolumnar")
            c.execute("SELECT pgcolumnar.set_options('n', stripe_row_limit => 2048, chunk_group_row_limit => 1024)")
            c.execute(f"INSERT INTO h {GEN}")
            c.execute(f"INSERT INTO n {GEN}")
            yield c, _one(c, "SELECT tag FROM h WHERE id = 7777")
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


# one check per test, so one red cannot stop the next (#1519 review)
def test_row_count(bloom, expect):
    expect.text(_one(bloom[0], "SELECT count(*) FROM n"), "20480", "row count")


def test_bloom_rows_written(bloom, expect):
    # the hashable tag column, one filter per row group
    expect.text(_one(bloom[0], "SELECT count(*) FROM pgcolumnar.bloom WHERE storage_id = "
                               "pgcolumnar.get_storage_id('n') AND column_index = 1"), "10", "bloom rows written")


def test_the_plan_under_test_is_a_columnar_custom_scan(bloom, expect):
    c, probe = bloom
    plan = "\n".join(r[0] for r in c.execute(f"EXPLAIN (COSTS OFF) SELECT id FROM n WHERE tag = {probe}"))
    expect.text("yes" if "Columnar Projected Columns" in plan else "no", "yes",
                "the plan under test is a columnar custom scan")


def test_equality_probe_parity(bloom, expect):
    c, probe = bloom
    expect.text(_one(c, HASH.format(f"SELECT id, tag FROM n WHERE tag = {probe}")),
                _one(c, HASH.format(f"SELECT id, tag FROM h WHERE tag = {probe}")), "equality probe parity")


def test_bloom_on_skips_groups(bloom, expect):
    c, probe = bloom
    expect.at_least(_skipped(c, f"SELECT id FROM n WHERE tag = {probe}", "on"), 1, "bloom on skips groups")


def test_bloom_off_skips_nothing(bloom, expect):
    # the values are spread so every group's min/max spans the domain
    c, probe = bloom
    expect.num(_skipped(c, f"SELECT id FROM n WHERE tag = {probe}", "off"), 0,
               "bloom off skips nothing (min/max cannot)")


def test_absent_value_parity(bloom, expect):
    expect.text(_one(bloom[0], "SELECT count(*) FROM n WHERE tag = 999999999"),
                _one(bloom[0], "SELECT count(*) FROM h WHERE tag = 999999999"), "absent value parity")
