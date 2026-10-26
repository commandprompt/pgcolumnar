"""pgcolumnar.approx_count_distinct, the aggregate (#1309 phase 2). The pytest twin of
`test/native_sketch_agg.sh`, every check by the shell's name.

A HyperLogLog over the type's extended hash (seed 0), with the writer's 2048 registers
and register rule, and a fixed estimator: the raw estimate, or linear counting at most
5m/2 with a zero register. One dataset is held within 4 standard errors (9.2%) and to a
pinned value; each pin was recomputed independently in SQL from core's extended hash.
Over 50 disjoint sets the RMS relative error must sit between 0.5 and 1.5 standard
errors, and the mean bias within 3 standard errors of the mean. NULLs are not counted;
a forced parallel plan returns the serial answer; a type with no extended hash and a
nondeterministic collation are refused with 0A000.

ON A PRIVATE DATABASE, built once by a module fixture; each test asserts one reading.
"""
import re

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_native_sketch_agg"
# no debug_parallel_query: PostgreSQL 15 calls it force_parallel_mode (see the shell)
PAR = ("SET max_parallel_workers_per_gather = 4", "SET parallel_setup_cost = 0",
       "SET parallel_tuple_cost = 0", "SET min_parallel_table_scan_size = 0")
DIST = ("SELECT sqrt(avg(e * e)) AS rms, avg(e) AS bias FROM ("
        "SELECT (pgcolumnar.approx_count_distinct(g) - 20000) / 20000.0 AS e "
        "FROM generate_series(1, 50) k, LATERAL generate_series(k * 1000000::bigint, "
        "k * 1000000::bigint + 19999) g GROUP BY k) s")


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _run(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.encode())
    except psycopg.Error as exc:
        return _message(exc)
    return ""


def _value(dsn, sql, settings=()):
    """-> the first column of the first row as text, or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for s in settings:
                conn.execute(s)
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    if row is None or row[0] is None:
        return query_error(f"no value from {sql}")
    return str(row[0])


def _sqlstate(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)
    except psycopg.Error as exc:
        return exc.sqlstate or query_error(_message(exc))
    return "noerror"


def _within(dsn, table, column):
    return _value(dsn, "SELECT CASE WHEN abs(e - x) <= 0.092 * x THEN 'within' ELSE e || ' for ' || x END "
                       f"FROM (SELECT pgcolumnar.approx_count_distinct({column}) e, "
                       f"count(DISTINCT {column}) x FROM {table}) s")


def _est(dsn, table, column):
    return _value(dsn, f"SELECT pgcolumnar.approx_count_distinct({column}) FROM {table}")


def _same(a, b):
    """"same" when both readings are the same NUMBER: two failed reads are not."""
    if not (isinstance(a, str) and a.isdigit()):
        return f"not a number: [{a}] vs [{b}]"
    return "same" if a == b else f"[{a}] vs [{b}]"


@pytest.fixture(scope="module")
def nsa(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        _run(dsn, "CREATE TABLE ak (a bigint, t text) USING pgcolumnar; "
                  "INSERT INTO ak SELECT g, 'key-' || g FROM generate_series(1, 100000) g; "
                  "CREATE TABLE akh (a bigint, t text); INSERT INTO akh SELECT * FROM ak;")
        r = {}
        r["distinct"] = _value(dsn, "SELECT count(DISTINCT a) FROM ak")
        r["100k within"] = _within(dsn, "ak", "a")
        r["100k"] = _est(dsn, "ak", "a")
        r["text within"] = _within(dsn, "ak", "t")
        r["text"] = _est(dsn, "ak", "t")
        r["heap"] = _same(_est(dsn, "akh", "a"), r["100k"])
        r["rms"] = _value(dsn, "SELECT CASE WHEN rms BETWEEN 0.0115 AND 0.0345 THEN 'yes' "
                               f"ELSE 'no: rms ' || round(rms::numeric, 4) END FROM ({DIST}) d")
        r["bias"] = _value(dsn, "SELECT CASE WHEN abs(bias) <= 3 * 0.023 / sqrt(50) THEN 'yes' "
                                f"ELSE 'no: bias ' || round(bias::numeric, 4) END FROM ({DIST}) d")
        _run(dsn, "CREATE TABLE ak10 AS SELECT g::bigint AS a FROM generate_series(1, 10) g; "
                  "CREATE TABLE ak1k AS SELECT g::bigint AS a FROM generate_series(1, 1000) g; "
                  "CREATE TABLE ak9k AS SELECT g::bigint AS a FROM generate_series(1, 9000) g;")
        r["10"] = _est(dsn, "ak10", "a")
        r["1k within"] = _within(dsn, "ak1k", "a")
        r["9k within"] = _within(dsn, "ak9k", "a")
        r["small"] = f"{_est(dsn, 'ak1k', 'a')}|{_est(dsn, 'ak9k', 'a')}"
        # ten values, where linear counting is exact: a NULL counted as a value reads 11
        _run(dsn, "CREATE TABLE akn AS SELECT g::bigint AS a FROM generate_series(1, 10) g "
                  "UNION ALL SELECT NULL FROM generate_series(1, 500)")
        r["nulls"] = _same(_est(dsn, "akn", "a"), _est(dsn, "ak10", "a"))
        r["empty"] = _value(dsn, "SELECT pgcolumnar.approx_count_distinct(a) FROM ak WHERE false")
        r["all null"] = _value(dsn, "SELECT pgcolumnar.approx_count_distinct(NULL::bigint) "
                                    "FROM generate_series(1, 100)")
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                for s in PAR:
                    conn.execute(s)
                plan = [row[0] for row in conn.execute(
                    "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) "
                    "SELECT pgcolumnar.approx_count_distinct(a) FROM akh").fetchall()]
            r["par plan"] = sum(1 for l in plan if "Partial Aggregate" in l
                                or re.search(r"Workers Launched: [1-9]", l))
        except psycopg.Error as exc:
            r["par plan"] = query_error(_message(exc))
        r["par"] = _same(_value(dsn, "SELECT pgcolumnar.approx_count_distinct(a) FROM akh", PAR),
                         _est(dsn, "akh", "a"))
        r["nohash"] = _sqlstate(dsn, "SELECT pgcolumnar.approx_count_distinct(point(g, g)) "
                                     "FROM generate_series(1, 3) g")
        _run(dsn, "CREATE COLLATION IF NOT EXISTS ak_ci "
                  "(provider = icu, locale = 'und-u-ks-level2', deterministic = false)")
        if _value(dsn, "SELECT count(*) FROM pg_collation WHERE collname = 'ak_ci'") == "1":
            r["nondet"] = _sqlstate(dsn, "SELECT pgcolumnar.approx_count_distinct(t COLLATE ak_ci) FROM ak")
        else:
            r["nondet"] = None
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_the_table_holds_100000_distinct_values(nsa, expect):
    expect.text(nsa["distinct"], "100000", "premise: the table holds 100000 distinct values")


def test_100000_bigints_are_within_4_standard_errors(nsa, expect):
    expect.text(nsa["100k within"], "within",
                "approx_count_distinct of 100000 distinct bigints is within 4 standard errors of the exact count")


def test_100000_bigints_equal_the_pin(nsa, expect):
    expect.text(nsa["100k"], "101372", "and equals its pinned value")


def test_a_text_column_is_within_4_standard_errors(nsa, expect):
    expect.text(nsa["text within"], "within", "a text column is within 4 standard errors too")


def test_the_text_column_equals_the_pin(nsa, expect):
    expect.text(nsa["text"], "106986", "and equals its pinned value, the recomputation's")


def test_a_heap_table_gives_the_columnar_answer(nsa, expect):
    expect.text(nsa["heap"], "same", "a heap table gives the columnar table's answer")


def test_the_rms_error_matches_the_theory(nsa, expect):
    expect.text(nsa["rms"], "yes", "over 50 sets the RMS relative error is between 0.5 and 1.5 standard errors")


def test_the_mean_bias_is_small(nsa, expect):
    expect.text(nsa["bias"], "yes", "and the mean bias is within 3 standard errors of the mean")


def test_ten_distinct_values_estimate_ten(nsa, expect):
    expect.text(nsa["10"], "10", "ten distinct values estimate ten")


def test_1000_values_are_within_4_standard_errors(nsa, expect):
    expect.text(nsa["1k within"], "within", "1000 distinct values are within 4 standard errors (linear counting)")


def test_9000_values_are_within_4_standard_errors(nsa, expect):
    expect.text(nsa["9k within"], "within", "9000 distinct values are within 4 standard errors (the raw estimate)")


def test_the_small_counts_equal_their_pins(nsa, expect):
    expect.text(nsa["small"], "1010|9026", "and the small counts equal their pinned values")


def test_nulls_are_not_counted(nsa, expect):
    expect.text(nsa["nulls"], "same", "NULLs are not counted")


def test_an_empty_input_estimates_0(nsa, expect):
    expect.text(nsa["empty"], "0", "an empty input estimates 0")


def test_an_all_null_input_estimates_0(nsa, expect):
    expect.text(nsa["all null"], "0", "an all-NULL input estimates 0")


def test_the_parallel_plan_runs_workers(nsa, expect):
    expect.num(nsa["par plan"], 2, "premise: the parallel plan runs a partial aggregate in workers")


def test_a_parallel_plan_returns_the_serial_answer(nsa, expect):
    expect.text(nsa["par"], "same", "a parallel plan returns the serial answer")


def test_the_aggregate_refuses_a_type_with_no_extended_hash(nsa, expect):
    expect.text(nsa["nohash"], "0A000", "a type with no extended hash is refused")


def test_the_aggregate_refuses_a_nondeterministic_collation(nsa, expect):
    if nsa["nondet"] is None:
        expect.cannot_run("MISSING_DEPENDENCY",
                          "this server has no ICU, so no nondeterministic collation can be created",
                          name="a nondeterministic collation is refused")
        return
    expect.text(nsa["nondet"], "0A000", "a nondeterministic collation is refused")
