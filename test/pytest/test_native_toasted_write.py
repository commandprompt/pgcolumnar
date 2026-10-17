"""Writing a toasted varlena (#445 detoast-once). The pytest twin of
`test/native_toasted_write.sh`, every check by the shell's name, one check per test.

The writer flattens each varlena once per row and reuses the flat value for the
encoder, the bloom hash and the min/max. The source is a heap table whose wide values
are stored pglz-compressed, so the columnar writer is handed already-toasted values,
the input that path exists for. The table is six row groups. A heap mirror is the oracle: the bytes, a range the
min/max must not over-prune, an equality, probed with the value itself so the bloom is asked, that it must not
over-prune, and a short
column that is never toasted.
"""
import psycopg
import pytest

RANGE = "SELECT count(*) FROM {} WHERE big >= 'e'"
BELOW = "SELECT count(*) FROM {} WHERE big < '1'"
EQUAL = "SELECT count(*) FROM {} WHERE big = '{}'"
DIGEST = "SELECT md5(string_agg({}, '|' ORDER BY k)) FROM {}"


def _one(c, sql):
    try:
        return str(c.execute(sql).fetchone()[0])
    except psycopg.Error as exc:
        return f"error: {exc.diag.message_primary}"


def _count(c, sql):
    """A count as a number, or the error as text: a failed query is a red by name."""
    try:
        return int(c.execute(sql).fetchone()[0])
    except psycopg.Error as exc:
        return f"error: {exc.diag.message_primary}"


@pytest.fixture(scope="module")
def toasted(pgc_cluster):
    db = "pgc_toasted"
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {db}")
    try:
        with psycopg.connect(pgc_cluster.dsn(dbname=db), autocommit=True) as c:
            c.execute("CREATE EXTENSION pgcolumnar")
            # big is ~3.2 kB and compressible, so heap stores it toasted (pglz); small
            # is never toasted, so both branches of the flatten run
            # pglz named, not defaulted: PG19's default_toast_compression is lz4
            c.execute("CREATE TABLE src (k int, big text COMPRESSION pglz, small text)")
            c.execute("INSERT INTO src SELECT g, repeat(md5((g % 777)::text), 100) || chr(65 + (g % 26)), "
                      "(g % 50)::text FROM generate_series(1, 60000) g")
            c.execute("CREATE TABLE h (k int, big text, small text)")
            c.execute("CREATE TABLE c (k int, big text, small text) USING pgcolumnar")
            # six row groups, so a zone map or a bloom can prune a group
            c.execute("SELECT pgcolumnar.set_options('c', stripe_row_limit => 10000)")
            c.execute("INSERT INTO h SELECT * FROM src")
            c.execute("INSERT INTO c SELECT * FROM src")
            yield c
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


def test_the_source_values_are_toasted(toasted, expect):
    # non-pglz means the fixture stopped toasting, and the path is no longer exercised
    expect.text(_one(toasted, "SELECT pg_column_compression(big) FROM src LIMIT 1"), "pglz",
                "premise: the source values are toasted (pglz)")


def test_toasted_values_are_byte_identical(toasted, expect):
    expect.text(_one(toasted, DIGEST.format("big", "c")), _one(toasted, DIGEST.format("big", "h")),
                "toasted values are byte-identical to the heap mirror")


def test_each_range_selects_some_rows_and_not_all(toasted, expect):
    expect.text(_one(toasted, "SELECT bool_and(n > 0 AND n < 60000) FROM (SELECT count(*) FILTER "
                              "(WHERE big >= 'e') AS n FROM h UNION ALL SELECT count(*) FILTER "
                              "(WHERE big < '1') FROM h) s"), "True",
                "premise: each range selects some rows and not all")


def test_the_min_max_zone_map_is_correct(toasted, expect):
    expect.num(_count(toasted, RANGE.format("c")), _count(toasted, RANGE.format("h")),
               "the min/max zone map is correct (range count matches heap)")


def test_the_min_max_zone_map_is_correct_below_a_bound(toasted, expect):
    # an upper bound prunes by the minimum, which the range above never asks; below a
    # typical first value, so a minimum left too high skips matching groups
    expect.num(_count(toasted, BELOW.format("c")), _count(toasted, BELOW.format("h")),
               "the min/max zone map is correct below a bound (range count matches heap)")


def _probe(c):
    """The toasted value itself, as a literal: compared with a scalar subquery the scan
    saw an InitPlan parameter, pushed no skip predicate and never asked the bloom."""
    return _one(c, "SELECT big FROM src WHERE k = 12345")


def test_the_equality_probe_reaches_the_bloom(toasted, expect):
    plan = "\n".join(r[0] for r in toasted.execute(
        f"EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) {EQUAL.format('c', _probe(toasted))}"))
    expect.num(plan.count("Columnar Usable Skip Predicates: 1"), 1,
               "the equality probe reaches the bloom (a usable skip predicate)")


def test_equality_on_a_toasted_value_matches_heap(toasted, expect):
    v = _probe(toasted)
    expect.num(_count(toasted, EQUAL.format("c", v)), _count(toasted, EQUAL.format("h", v)),
               "equality on a toasted value (bloom + recheck) matches heap")


def test_the_non_toasted_column_is_byte_identical(toasted, expect):
    expect.text(_one(toasted, DIGEST.format("small", "c")), _one(toasted, DIGEST.format("small", "h")),
                "the non-toasted column is byte-identical too")
