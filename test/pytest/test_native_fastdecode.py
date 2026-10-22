"""Fast decode of attbyval fixed-width columns (#289). The pytest twin of
`test/native_fastdecode.sh`, every check by the shell's name (#1341).

The read path inlines the by-value decode (fetch_att + advance) instead of calling
PgColumnarDecodeValue; by-reference (uuid) and varlena (text, numeric) columns keep the
call path and serve as controls. Every value is compared with a heap mirror built from
the generator: every byval fixed type, NULL patterns, adversarial bit patterns, every
fixed-width encoding, per-vector skipping, deletes and ADD COLUMN.

Per-vector skipping is asserted, not assumed: the section that guards the cursor's
lockstep after a vector jump sets chunk_group_row_limit so a row group holds several
vectors, and requires the scan's own counter to report vectors skipped. With only
stripe_row_limit set, each group was one vector and no jump ever happened.

The inlined decode returns exactly what the call path returns, so a build with the fast
path switched off is indistinguishable here, and nothing in the scan reports which path
ran; this file asserts the values, which is what the fast path must not change.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading, so one red cannot stop the next (#1339). Each comparison hashes
both sides on the server exactly as lib.sh's `pgc_set_hash` does, and a failed query is
a `query_error()`, so two failures never compare equal.
"""
import re

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_native_fastdecode"
SET_HASH = ("SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), $e$EMPTY$e$) "
            "FROM (SELECT _row::text AS t FROM ( {q} ) _row) _s")
SID = "pgcolumnar.get_storage_id('t_col')"


def _failed(exc):
    return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _value(dsn, sql):
    """-> the first column of the first row as text, or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return _failed(exc)
    return "" if row is None or row[0] is None else str(row[0])


def _lines(dsn, sql):
    """-> every row's first column as text, or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            return [str(r[0]) for r in conn.execute(sql).fetchall()]
    except psycopg.Error as exc:
        return _failed(exc)


def _count(dsn, sql):
    """-> the count as an int, or the failure (or a non-number) as a query_error()."""
    v = _value(dsn, sql)
    return int(v) if v.isdigit() else v if v else query_error(f"no count from {sql}")


def _run(dsn, sql):
    """Run one statement and swallow its failure, as the shell's psql_run does: a failed
    step shows up in the readings after it."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)
    except psycopg.Error:
        pass


def _pair(dsn, defs):
    """lib.sh's make_pair."""
    _run(dsn, "DROP TABLE IF EXISTS t_heap; DROP TABLE IF EXISTS t_col")
    _run(dsn, f"CREATE TABLE t_heap ({defs}) USING heap")
    _run(dsn, f"CREATE TABLE t_col ({defs}) USING pgcolumnar")


def _load(dsn, body):
    """lib.sh's load_pair: the heap from the generator, the columnar table from the heap."""
    _run(dsn, f"INSERT INTO t_heap {body}")
    _run(dsn, "INSERT INTO t_col SELECT * FROM t_heap")


def _diff(dsn, tmpl):
    """lib.sh's diff_query: (the columnar side's hash, the heap side's hash)."""
    return (_value(dsn, SET_HASH.format(q=tmpl.replace("%T", "t_col"))),
            _value(dsn, SET_HASH.format(q=tmpl.replace("%T", "t_heap"))))


def _baseline(dsn, lo, hi):
    """-> chunks of columns lo..hi still carrying a baseline descriptor, or 'no chunks
    counted' when the query matched none: 0 of nothing is not a reading."""
    return _value(dsn, f"SELECT CASE WHEN count(*) = 0 THEN 'no chunks counted' "
                       f"ELSE (count(*) FILTER (WHERE octet_length(encoding_descriptor) < 6))::text END "
                       f"FROM pgcolumnar.column_chunk WHERE storage_id = {SID} "
                       f"AND column_index BETWEEN {lo} AND {hi}")


def _vectors_skipped(dsn, sql):
    """-> the scan's 'Columnar Vectors Skipped' under EXPLAIN ANALYZE as an int, or why
    there is none."""
    plan = _lines(dsn, f"EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) {sql}")
    if not isinstance(plan, list):
        return plan
    for line in plan:
        m = re.search(r"Columnar Vectors Skipped: (\d+)", line)
        if m:
            return int(m.group(1))
    return query_error("the plan reports no Columnar Vectors Skipped")


@pytest.fixture(scope="module")
def fd(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r = {}

        # ---- 1. every attbyval fixed type + controls, many groups ----
        _pair(dsn, "id int, b bool, i2 smallint, i4 int, i8 bigint, f4 real, f8 double precision, "
                   "d date, ts timestamp, tz timestamptz, tm time, txt text, uid uuid, num numeric")
        _run(dsn, "SELECT pgcolumnar.set_options('t_col', stripe_row_limit => 1000)")
        _load(dsn, "SELECT g, "
                   "CASE WHEN g % 13 = 0 THEN NULL ELSE (g % 2 = 0) END, "
                   "CASE WHEN g % 11 = 0 THEN NULL ELSE (g % 97 - 48)::smallint END, "
                   "CASE WHEN g %  7 = 0 THEN NULL ELSE (g * 7 - 3) END, "
                   "CASE WHEN g %  5 = 0 THEN NULL ELSE (g::bigint * 1000003 - 5) END, "
                   "CASE WHEN g %  6 = 0 THEN NULL ELSE (g * 1.5)::real END, "
                   "CASE WHEN g <= 50    THEN NULL ELSE (g * 3.14159)::float8 END, "
                   "CASE WHEN g %  8 = 0 THEN NULL ELSE DATE '2020-01-01' + g END, "
                   "CASE WHEN g > 5950   THEN NULL ELSE TIMESTAMP '2020-01-01' + (g || ' min')::interval END, "
                   "CASE WHEN g %  9 = 0 THEN NULL ELSE TIMESTAMPTZ '2020-01-01' + (g || ' min')::interval END, "
                   "CASE WHEN g % 10 = 0 THEN NULL ELSE TIME '00:00:00' + (g || ' sec')::interval END, "
                   "CASE WHEN g %  4 = 0 THEN NULL ELSE 't' || g END, "
                   "CASE WHEN g % 12 = 0 THEN NULL ELSE ('00000000-0000-0000-0000-' || lpad(g::text, 12, '0'))::uuid END, "
                   "CASE WHEN g %  3 = 0 THEN NULL ELSE (g * 0.01)::numeric END "
                   "FROM generate_series(1, 6000) g")
        r["rows"] = _value(dsn, "SELECT count(*) FROM t_col")
        plan = _lines(dsn, "EXPLAIN (COSTS OFF) SELECT * FROM t_col")
        r["columnar scan"] = (plan if not isinstance(plan, list) else
                              "yes" if any("Columnar Projected Columns" in ln for ln in plan) else "no")
        r["row groups"] = _count(dsn, f"SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = {SID}")
        r["byval descriptors"] = _baseline(dsn, 1, 10)
        r["full"] = _diff(dsn, "SELECT * FROM %T")
        r["exact aggs"] = _diff(dsn, "SELECT count(*), count(b), count(i4), count(f8), count(ts), "
                                     "sum(i2::bigint), sum(i4::bigint), sum(i8), bool_and(b), bool_or(b), "
                                     "min(i2), max(i2), min(i4), max(i4), min(i8), max(i8), "
                                     "min(f4), max(f4), min(f8), max(f8), "
                                     "min(d), max(d), min(ts), max(ts), min(tz), max(tz), min(tm), max(tm) FROM %T")
        r["grouped"] = _diff(dsn, "SELECT (i4 % 24) AS bucket, count(*), sum(i8), min(f8), max(f8), max(d) "
                                  "FROM %T WHERE i4 IS NOT NULL GROUP BY 1")
        r["by int"] = _diff(dsn, "SELECT * FROM %T WHERE i4 BETWEEN 1000 AND 2000")
        r["by bigint"] = _diff(dsn, "SELECT id, i8 FROM %T WHERE i8 < 0 OR i8 > 3000000000")
        r["by float"] = _diff(dsn, "SELECT id, f8 FROM %T WHERE f8 > 10000")
        r["by date"] = _diff(dsn, "SELECT id, d FROM %T WHERE d > DATE '2035-01-01'")

        # ---- 2. adversarial boundary bit patterns ----
        _pair(dsn, "id int, i4 int, i8 bigint, f4 real, f8 double precision")
        _run(dsn, "SELECT pgcolumnar.set_options('t_col', stripe_row_limit => 1000)")
        _run(dsn, "INSERT INTO t_heap VALUES "
                  "(1, -2147483648, -9223372036854775808, 'NaN', 'NaN'), "
                  "(2,  2147483647,  9223372036854775807, 'Infinity', 'Infinity'), "
                  "(3,  -1, -1, '-Infinity', '-Infinity'), "
                  "(4,   0,  0, 0.0, 0.0), "
                  "(5,   1,  1, -0.0, -0.0), "
                  "(6,  -2147483647, 9223372036854775806, 1.1754944e-38, 5e-324), "
                  "(7,   2147483646, -9223372036854775807, 3.4028235e38, 1.7976931348623157e308), "
                  "(8,  -123456789, 1234567890123456789, -3.4028235e38, -1.7976931348623157e308)")
        _run(dsn, "INSERT INTO t_col SELECT * FROM t_heap")
        r["boundary"] = _diff(dsn, "SELECT * FROM %T")
        r["negative zero"] = (_value(dsn, "SELECT f8::text FROM t_col WHERE id = 5"),
                              _value(dsn, "SELECT f8::text FROM t_heap WHERE id = 5"))
        r["nan"] = _value(dsn, "SELECT f4::text FROM t_col WHERE id = 1")

        # ---- 3. every fixed-width encoding, same logical data ----
        _pair(dsn, "id int, seq bigint, dod bigint, cst int, gor float8, alp float8")
        _run(dsn, "SELECT pgcolumnar.set_options('t_col', stripe_row_limit => 2048, compression => 'none')")
        _load(dsn, "SELECT g, g::bigint * 4, (g * (g + 1) / 2)::bigint, 42, "
                   "(100.0 + sin(g::float8 / 25.0) * 5.0)::float8, (g * 0.01)::float8 "
                   "FROM generate_series(1, 6000) g")
        r["encoded descriptors"] = _baseline(dsn, 1, 5)
        r["encodings"] = _diff(dsn, "SELECT * FROM %T")
        r["encoded aggs"] = _diff(dsn, "SELECT count(*), sum(seq), sum(dod), sum(cst::bigint), "
                                       "min(gor), max(gor), min(alp), max(alp) FROM %T")

        # ---- 4. per-vector skipping active ----
        _pair(dsn, "id int, k bigint, v float8")
        _run(dsn, "SELECT pgcolumnar.set_options('t_col', stripe_row_limit => 8192, chunk_group_row_limit => 1024)")
        _load(dsn, "SELECT g, g::bigint, (g * 2.5)::float8 FROM generate_series(1, 40000) g")
        r["vectors per group"] = _value(
            dsn, "SELECT CASE WHEN max(n) > 1 THEN 'yes' ELSE 'no (' || coalesce(max(n)::text, 'none') || ')' END "
                 f"FROM (SELECT count(*) n FROM pgcolumnar.zone_map WHERE storage_id = {SID} "
                 "AND vector_index >= 0 AND column_index = 0 GROUP BY group_number) s")
        r["narrow"] = _diff(dsn, "SELECT * FROM %T WHERE k BETWEEN 20000 AND 20100")
        r["skipped"] = _vectors_skipped(dsn, "SELECT * FROM t_col WHERE k BETWEEN 20000 AND 20100")
        r["two ranges"] = _diff(dsn, "SELECT id, v FROM %T WHERE k < 50 OR k BETWEEN 30000 AND 30050")
        r["agg skipping"] = _diff(dsn, "SELECT count(*), sum(k), min(v), max(v) FROM %T "
                                       "WHERE k BETWEEN 10000 AND 25000")

        # ---- 5. deleted rows ----
        _pair(dsn, "id int, i4 int, i8 bigint, f8 float8")
        _run(dsn, "SELECT pgcolumnar.set_options('t_col', stripe_row_limit => 1000)")
        _load(dsn, "SELECT g, g * 3, g::bigint * 7, (g / 3.0)::float8 FROM generate_series(1, 5000) g")
        _run(dsn, "DELETE FROM t_heap WHERE id % 4 = 0 OR id BETWEEN 1000 AND 1500")
        _run(dsn, "DELETE FROM t_col  WHERE id % 4 = 0 OR id BETWEEN 1000 AND 1500")
        r["after deletes"] = _diff(dsn, "SELECT * FROM %T")
        r["agg after deletes"] = _diff(dsn, "SELECT count(*), sum(i4::bigint), sum(i8), min(f8), max(f8) FROM %T")

        # ---- 6. ADD COLUMN: early groups lack the column ----
        _pair(dsn, "id int, a int")
        _run(dsn, "SELECT pgcolumnar.set_options('t_col', stripe_row_limit => 1000)")
        _load(dsn, "SELECT g, g * 2 FROM generate_series(1, 3000) g")
        _run(dsn, "ALTER TABLE t_heap ADD COLUMN b bigint DEFAULT 77")
        _run(dsn, "ALTER TABLE t_col  ADD COLUMN b bigint DEFAULT 77")
        _run(dsn, "INSERT INTO t_heap SELECT g, g * 2, g::bigint * 5 FROM generate_series(3001, 6000) g")
        _run(dsn, "INSERT INTO t_col  SELECT g, g * 2, g::bigint * 5 FROM generate_series(3001, 6000) g")
        r["add column"] = _diff(dsn, "SELECT * FROM %T")
        r["agg add column"] = _diff(dsn, "SELECT count(*), sum(a::bigint), sum(b), min(b), max(b) FROM %T")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_fastdecode_row_count_matches(fd, expect):
    expect.text(fd["rows"], "6000", "row count matches")


def test_fastdecode_scan_is_the_columnar_custom_scan(fd, expect):
    expect.text(fd["columnar scan"], "yes", "scan is the columnar custom scan")


def test_fastdecode_many_row_groups_written(fd, expect):
    expect.at_least(fd["row groups"], 2, "many row groups written (widen runs per group)")


def test_fastdecode_byval_columns_carry_real_descriptors(fd, expect):
    expect.text(fd["byval descriptors"], "0", "byval fixed columns carry non-baseline descriptors")


def test_fastdecode_full_projection_matches_heap(fd, expect):
    expect.text(*fd["full"], "full projection is byte-identical to heap")


def test_fastdecode_exact_aggregates_match_heap(fd, expect):
    expect.text(*fd["exact aggs"], "exact aggregates match heap")


def test_fastdecode_grouped_aggregate_matches_heap(fd, expect):
    expect.text(*fd["grouped"], "grouped aggregate matches heap")


def test_fastdecode_filtered_by_int_matches_heap(fd, expect):
    expect.text(*fd["by int"], "filtered by int matches heap")


def test_fastdecode_filtered_by_bigint_matches_heap(fd, expect):
    expect.text(*fd["by bigint"], "filtered by bigint matches heap")


def test_fastdecode_filtered_by_float_matches_heap(fd, expect):
    expect.text(*fd["by float"], "filtered by float matches heap")


def test_fastdecode_filtered_by_date_matches_heap(fd, expect):
    expect.text(*fd["by date"], "filtered by date matches heap")


def test_fastdecode_boundary_bit_patterns_round_trip(fd, expect):
    expect.text(*fd["boundary"], "boundary int/float bit patterns round-trip exactly")


def test_fastdecode_negative_zero_preserved(fd, expect):
    expect.text(*fd["negative zero"], "negative zero preserved")


def test_fastdecode_nan_preserved(fd, expect):
    expect.text(fd["nan"], "NaN", "NaN preserved")


def test_fastdecode_encoded_columns_carry_real_descriptors(fd, expect):
    expect.text(fd["encoded descriptors"], "0", "encoded columns carry non-baseline descriptors")


def test_fastdecode_all_encodings_decode_identically(fd, expect):
    expect.text(*fd["encodings"], "all encodings decode identically to heap")


def test_fastdecode_aggregates_over_encoded_columns_match(fd, expect):
    expect.text(*fd["encoded aggs"], "aggregates over encoded columns match")


def test_fastdecode_a_row_group_holds_multiple_vectors(fd, expect):
    expect.text(fd["vectors per group"], "yes", "vecskip: a row group holds multiple vectors")


def test_fastdecode_narrow_range_with_vector_skipping_matches(fd, expect):
    expect.text(*fd["narrow"], "narrow range with vector skipping matches heap")


def test_fastdecode_the_narrow_range_skips_whole_vectors(fd, expect):
    expect.at_least(fd["skipped"], 1, "the narrow range skips whole vectors")


def test_fastdecode_two_narrow_ranges_match(fd, expect):
    expect.text(*fd["two ranges"], "two narrow ranges (non-adjacent vectors) match heap")


def test_fastdecode_aggregate_under_skipping_matches(fd, expect):
    expect.text(*fd["agg skipping"], "aggregate under skipping matches heap")


def test_fastdecode_scan_after_deletes_matches(fd, expect):
    expect.text(*fd["after deletes"], "scan after scattered deletes matches heap")


def test_fastdecode_aggregate_after_deletes_matches(fd, expect):
    expect.text(*fd["agg after deletes"], "aggregate after deletes matches heap")


def test_fastdecode_scan_across_add_column_matches(fd, expect):
    expect.text(*fd["add column"], "scan across an ADD COLUMN boundary matches heap")


def test_fastdecode_aggregate_across_add_column_matches(fd, expect):
    expect.text(*fd["agg add column"], "aggregate across ADD COLUMN matches heap")
