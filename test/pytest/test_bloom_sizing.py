"""A bloom filter is sized by its DISTINCT count, not its value count (#467). The pytest
twin of `test/bloom_sizing.sh`, every check by the shell's name (#1341).

Two columns of one row group differ only in cardinality, so a difference in their
stored filter sizes (`octet_length` of `pgcolumnar.bloom.filter`) can only be
cardinality: sized by value count they are identical, which is the defect. A smaller
filter must still skip, so a scattered fixture with ten row groups, where zone maps
can exclude nothing, must report groups removed by the probe; and the answer must not
change with the filter off. Above the distinct cap a column is refused a filter, while
a five-value column of the same row group keeps its own.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading, so one red cannot stop the next (#1339). A reading is a value or
a `query_error()`, never a default: a missing filter row is not a size.
"""
import re

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_bloom_sizing"
ROWS = 20000
BIG = 300000
SKIP_ROWS = 20000
REMOVED = "Columnar Chunk Groups Removed by Filter"


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _run(dsn, sql):
    """-> "" when the statements ran, else the error: the shell's psql_run."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.encode())  # simple protocol: several statements, as psql -c
    except psycopg.Error as exc:
        return _message(exc)
    return ""


def _value(dsn, sql, settings=()):
    """-> the first column of the first row, or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for s in settings:
                conn.execute(s)
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    if row is None or row[0] is None:
        return query_error(f"no value from {sql}")
    return row[0]


def _plan(dsn, sql, settings=()):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for s in settings:
                conn.execute(s)
            return "\n".join(r[0] for r in conn.execute(sql))
    except psycopg.Error as exc:
        return query_error(_message(exc))


def _removed(dsn, probe, settings):
    """-> the chunk groups the scan removed for `k = probe`, or a failed reading."""
    plan = _plan(dsn, f"EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF) SELECT count(*) FROM bsk WHERE k = {probe}",
                 ["SET max_parallel_workers_per_gather = 0"] + settings)
    for line in plan.splitlines():
        if REMOVED in line:
            m = re.search(r"\d+", line[line.index(REMOVED) + len(REMOVED):])
            if m:
                return int(m.group(0))
    return query_error(f"no '{REMOVED}' line in the plan: {plan.splitlines()[0] if plan else ''}")


@pytest.fixture(scope="module")
def bsz(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r = {}

        # one table, one row group, two columns that differ only in cardinality
        _run(dsn, "DROP TABLE IF EXISTS bs; CREATE TABLE bs (lo int, hi int, pad text) USING pgcolumnar; "
                  f"SELECT pgcolumnar.set_options('bs', stripe_row_limit => {ROWS}); "
                  f"INSERT INTO bs SELECT g % 5, g, md5(g::text) FROM generate_series(1, {ROWS}) g; ANALYZE bs;")
        sid = _value(dsn, "SELECT pgcolumnar.get_storage_id('bs')")
        r["rows"] = _value(dsn, "SELECT count(*) FROM bs")
        r["groups"] = _value(dsn, f"SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = {sid}")
        r["lo distinct"] = _value(dsn, "SELECT count(DISTINCT lo) FROM bs")
        r["hi distinct"] = _value(dsn, "SELECT count(DISTINCT hi) FROM bs")
        for key, col in (("lo", 0), ("hi", 1)):
            r[key] = _value(dsn, f"SELECT octet_length(filter) FROM pgcolumnar.bloom WHERE storage_id = {sid} "
                                 f"AND column_index = {col} ORDER BY group_number LIMIT 1")
        r["absent"] = _value(dsn, "SELECT count(*) FROM bs WHERE lo = 999")
        r["present"] = _value(dsn, "SELECT count(*) FROM bs WHERE lo = 3")
        same = "SELECT md5(string_agg(hi::text, ',' ORDER BY hi)) FROM bs WHERE lo = 2"
        r["on"] = _value(dsn, same)
        r["off"] = _value(dsn, same, ["SET pgcolumnar.enable_bloom_filter = off"])

        # the filter must still skip: scattered values, so zone maps exclude nothing
        _run(dsn, "DROP TABLE IF EXISTS bsk; CREATE TABLE bsk (k int, pad text) USING pgcolumnar; "
                  "SELECT pgcolumnar.set_options('bsk', stripe_row_limit => 2000); "
                  f"INSERT INTO bsk SELECT (g * 7919) % {SKIP_ROWS}, md5(g::text) "
                  f"FROM generate_series(1, {SKIP_ROWS}) g; ANALYZE bsk;")
        r["skip groups"] = _value(dsn, "SELECT count(*) FROM pgcolumnar.row_group "
                                       "WHERE storage_id = pgcolumnar.get_storage_id('bsk')")
        r["skipped on"] = _removed(dsn, 4242, [])
        r["skipped off"] = _removed(dsn, 4242, ["SET pgcolumnar.enable_bloom_filter = off"])

        # above the distinct cap, beside a five-value column that must keep its filter
        _run(dsn, "DROP TABLE IF EXISTS bsx; CREATE TABLE bsx (u bigint, pad text, lo int) USING pgcolumnar; "
                  f"SELECT pgcolumnar.set_options('bsx', stripe_row_limit => {BIG}); "
                  f"INSERT INTO bsx SELECT g, md5(g::text), g % 5 FROM generate_series(1, {BIG}) g; ANALYZE bsx;")
        r["big group"] = _value(dsn, "SELECT max(row_count) FROM pgcolumnar.row_group "
                                     "WHERE storage_id = pgcolumnar.get_storage_id('bsx')")
        r["big distinct"] = _value(dsn, "SELECT count(DISTINCT u) FROM bsx")
        r["refused kept"] = _value(dsn, "SELECT count(*) FILTER (WHERE column_index = 0) || '/' || "
                                        "count(*) FILTER (WHERE column_index = 2) "
                                        "FROM pgcolumnar.bloom WHERE storage_id = pgcolumnar.get_storage_id('bsx')")
        r["big rows"] = _value(dsn, "SELECT count(*) FROM bsx")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_every_row_of_the_sizing_table_loaded(bsz, expect):
    expect.text(str(bsz["rows"]), str(ROWS), "premise: every row loaded")


def test_the_sizing_table_is_one_row_group(bsz, expect):
    expect.text(str(bsz["groups"]), "1", "premise: the whole table is one row group")


def test_the_low_cardinality_column_really_is_low(bsz, expect):
    expect.text(str(bsz["lo distinct"]), "5", "premise: the low-cardinality column really is low")


def test_the_high_cardinality_column_really_is_high(bsz, expect):
    expect.text(str(bsz["hi distinct"]), str(ROWS), "premise: the high-cardinality column really is high")


def test_the_low_cardinality_filter_size_is_a_measurement(bsz, expect):
    expect.text(str(bsz["lo"]), "a size" if not isinstance(bsz["lo"], int) else str(bsz["lo"]),
                "premise: the low-cardinality filter size is a measurement")


def test_the_high_cardinality_filter_size_is_a_measurement(bsz, expect):
    expect.text(str(bsz["hi"]), "a size" if not isinstance(bsz["hi"], int) else str(bsz["hi"]),
                "premise: the high-cardinality filter size is a measurement")


def test_a_five_value_column_does_not_get_a_unique_columns_filter(bsz, expect):
    lo, hi = bsz["lo"], bsz["hi"]
    got = (("yes" if lo * 8 <= hi else f"no ({lo} bytes against {hi})")
           if isinstance(lo, int) and isinstance(hi, int) else query_error(f"no sizes: {lo} / {hi}"))
    expect.text(got, "yes", "a five-value column does not get a unique column's filter (#467)")


def test_a_value_that_is_absent_is_still_reported_absent(bsz, expect):
    expect.text(str(bsz["absent"]), "0", "a value that is absent is still reported absent")


def test_a_value_that_is_present_is_still_found(bsz, expect):
    expect.text(str(bsz["present"]), str(ROWS // 5), "and a value that is present is still found")


def test_results_are_identical_with_the_bloom_filter_and_without_it(bsz, expect):
    expect.text(bsz["on"], bsz["off"], "results are identical with the filter and without it")


def test_the_skip_fixture_has_many_row_groups(bsz, expect):
    expect.text(str(bsz["skip groups"]), "10", "premise: the skip fixture has many row groups to skip")


def test_the_skip_counter_is_reported_with_the_probe_on(bsz, expect):
    v = bsz["skipped on"]
    expect.text(str(v), str(v) if isinstance(v, int) else "a count",
                "premise: the skip counter is reported with the probe on")


def test_the_skip_counter_is_reported_with_the_probe_off(bsz, expect):
    v = bsz["skipped off"]
    expect.text(str(v), str(v) if isinstance(v, int) else "a count",
                "premise: the skip counter is reported with the probe off")


def test_zone_maps_alone_skip_nothing_on_the_bloom_fixture(bsz, expect):
    expect.text(str(bsz["skipped off"]), "0", "premise: zone maps alone skip nothing on this fixture")


def test_the_bloom_filter_still_skips_groups_it_can_prove_empty(bsz, expect):
    v = bsz["skipped on"]
    got = ("yes" if v > 0 else f"no ({v} removed)") if isinstance(v, int) else v
    expect.text(got, "yes", "the filter still skips groups it can prove empty (#467)")


def test_one_row_group_holds_more_than_the_refusal_threshold(bsz, expect):
    expect.text(str(bsz["big group"]), str(BIG), "premise: one row group holds more than the refusal threshold")


def test_the_values_above_the_threshold_really_are_distinct(bsz, expect):
    expect.text(str(bsz["big distinct"]), str(BIG), "premise: those values really are distinct")


def test_a_column_above_the_distinct_cap_is_refused_a_filter(bsz, expect):
    expect.text(bsz["refused kept"], "0/1",
                "a column above the distinct cap is refused a filter, not given a bad one")


def test_the_large_load_itself_succeeded(bsz, expect):
    expect.text(str(bsz["big rows"]), str(BIG), "and the load itself succeeded, rather than erroring on the way")
