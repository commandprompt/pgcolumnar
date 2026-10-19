"""Zone min/max through the direct comparison path (#155). The pytest twin of
`test/write_minmax_fastpath.sh`, every check by the shell's name (#1341).

Integer-family columns compare their values directly when the writer tracks a chunk's
minimum and maximum, testing the maximum first so an ascending load needs one comparison
per value. The risk is not a crash: a zone map drives row-group skipping, so a minimum
that is too high or a maximum that is too low skips a group that holds matching rows and
the query silently returns fewer. So the checks are differential against a heap mirror,
over predicates that sit on chunk-group bounds. Floats stay off the fast path: btree
orders NaN above everything, and a C comparison would never make NaN a chunk's maximum.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading, so one red cannot stop the next (#1339). A failed query is a
`query_error()`, and a comparison of two readings counts a failure on either side as a
mismatch, never as agreement.
"""
import pathlib
import re

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_write_minmax_fastpath"
ROWS = 200000
SRC = pathlib.Path(__file__).resolve().parents[2] / "src"
SKIPPED = re.compile(r"Columnar Chunk Groups Removed by Filter: ([0-9]+)")
NARROW = "SELECT count(*) FROM mm_c WHERE asc_i BETWEEN 19999 AND 20001"

# Predicates on and around chunk-group boundaries: a group is skipped or kept wrongly
# only at its edges.
PREDICATES = [
    ("i4_eq", "i4 = 5000"),
    ("i4_bound", "i4 BETWEEN 4999 AND 5001"),
    ("i4_range", f"i4 > {ROWS // 2} AND i4 < {ROWS // 2 + 17}"),
    ("i2_eq", "i2 = 12345"),
    ("i2_range", "i2 BETWEEN 100 AND 200"),
    ("i8_range", "i8 > 4999000000 AND i8 < 5001000000"),
    ("date_range", "d BETWEEN '2000-06-01' AND '2000-06-03'"),
    ("ts_range", "ts > '2000-01-01 01:00:00' AND ts < '2000-01-01 01:00:10'"),
    ("tz_range", "tz > '2000-01-01 01:00:00+00' AND tz < '2000-01-01 01:00:10+00'"),
    ("asc_range", "asc_i BETWEEN 19999 AND 20001"),
    ("desc_range", "desc_i BETWEEN 19999 AND 20001"),
    ("rand_range", "rand_i BETWEEN 500 AND 600"),
    ("neg_range", "neg_i BETWEEN -20001 AND -19999"),
    ("null_range", "nullable_i BETWEEN 4999 AND 5001"),
    ("null_isnull", "nullable_i IS NULL"),
    ("text_eq", "t = 't5000'"),
]
COLUMNS = ["i2", "i4", "i8", "d", "ts", "tz", "asc_i", "desc_i", "rand_i", "neg_i",
           "nullable_i", "t"]


def _failed(exc):
    return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _rows(dsn, *sqls):
    """-> the last statement's rows, first column as text, on a connection of its own,
    or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for sql in sqls:
                cur = conn.execute(sql)
            return ["" if r[0] is None else str(r[0]) for r in cur]
    except psycopg.Error as exc:
        return _failed(exc)


def _value(dsn, *sqls):
    rows = _rows(dsn, *sqls)
    return rows if isinstance(rows, str) else (rows[0] if rows else "")


def _disagree(label, c, h):
    """-> "" when both readings are real and equal, else the label with both sides."""
    failed = any(isinstance(v, str) and v.startswith("QUERY_ERROR") for v in (c, h))
    return f"{label}(columnar={c} heap={h})" if failed or c == "" or c != h else ""


def _load(dsn, sqls):
    with psycopg.connect(dsn, autocommit=True) as c:
        for sql in sqls:
            try:
                c.execute(sql)
            except psycopg.Error as exc:
                print(f"-- step failed: {str(exc).splitlines()[0] if str(exc) else exc!r}")


@pytest.fixture(scope="module")
def mm(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _load(dsn, ["CREATE EXTENSION IF NOT EXISTS pgcolumnar",
                    # small chunk groups, so there are many zone maps to get wrong
                    "SET pgcolumnar.stripe_row_limit = 20000",
                    "SET pgcolumnar.chunk_group_row_limit = 5000",
                    "CREATE TABLE mm_c (i2 smallint, i4 int, i8 bigint, d date, ts timestamp, "
                    "tz timestamptz, asc_i int, desc_i int, rand_i int, neg_i int, "
                    "nullable_i int, t text) USING pgcolumnar",
                    "INSERT INTO mm_c SELECT (g % 30000)::smallint, g, g::bigint * 1000000, "
                    "'2000-01-01'::date + (g % 10000), "
                    "'2000-01-01'::timestamp + (g || ' sec')::interval, "
                    "'2000-01-01'::timestamptz + (g || ' sec')::interval, "
                    f"g, {ROWS} - g, (g * 7919) % 100000, -g, "
                    "CASE WHEN g % 5 = 0 THEN NULL ELSE g END, 't' || g "
                    f"FROM generate_series(1, {ROWS}) g",
                    "CREATE TABLE mm_h (LIKE mm_c)",
                    "INSERT INTO mm_h SELECT * FROM mm_c"])
        groups = _value(dsn, "SELECT count(*) FROM pgcolumnar.row_group r JOIN pgcolumnar.storage s "
                             "ON s.storage_id = r.storage_id WHERE s.relation_oid = 'mm_c'::regclass")
        print(f"-- {ROWS} rows, {groups} row groups")

        r = {"ranges": " ".join(filter(None, (
            _disagree(label, _value(dsn, f"SELECT count(*) FROM mm_c WHERE {p}"),
                      _value(dsn, f"SELECT count(*) FROM mm_h WHERE {p}"))
            for label, p in PREDICATES)))}
        bounds = "min({c})::text || '|' || max({c})::text FROM {t}"
        r["bounds"] = " ".join(filter(None, (
            _disagree(col, _value(dsn, "SELECT " + bounds.format(c=col, t="mm_c")),
                      _value(dsn, "SELECT " + bounds.format(c=col, t="mm_h")))
            for col in COLUMNS)))

        plan = _rows(dsn, f"EXPLAIN (COSTS OFF) {NARROW}")
        r["columnar"] = plan if isinstance(plan, str) else (
            "yes" if any("Columnar Projected Columns" in line for line in plan) else "no")
        lines = _rows(dsn, f"EXPLAIN (ANALYZE, COSTS off, TIMING off, SUMMARY off) {NARROW}")
        r["skipped"] = lines if isinstance(lines, str) else next(
            (m.group(1) for m in map(SKIPPED.search, lines) if m), "")
        print(f"-- chunk groups skipped for a narrow range: {r['skipped'] or 'none reported'}")

        _load(dsn, ["SET pgcolumnar.stripe_row_limit = 20000",
                    "SET pgcolumnar.chunk_group_row_limit = 20000",
                    "CREATE TABLE mm_f (f4 real, f8 double precision) USING pgcolumnar",
                    # the special values END the one chunk group: arriving first, NaN
                    # would be the min and max before any comparison ran
                    "INSERT INTO mm_f SELECT "
                    "CASE WHEN g = 5001 THEN 'NaN'::real WHEN g = 5002 THEN 'Infinity'::real "
                    "WHEN g = 5003 THEN '-Infinity'::real ELSE g::real END, "
                    "CASE WHEN g = 5001 THEN 'NaN'::float8 WHEN g = 5002 THEN 'Infinity'::float8 "
                    "WHEN g = 5003 THEN '-Infinity'::float8 ELSE g::float8 END "
                    "FROM generate_series(1, 5003) g",
                    "CREATE TABLE mm_fh (LIKE mm_f)",
                    "INSERT INTO mm_fh SELECT * FROM mm_f"])
        r["float groups"] = _value(dsn, "SELECT count(*) FROM pgcolumnar.row_group r JOIN pgcolumnar.storage s "
                                        "ON s.storage_id = r.storage_id WHERE s.relation_oid = 'mm_f'::regclass")
        for k, sql in (("max f8", "SELECT max(f8)::text FROM {t}"),
                       ("max f4", "SELECT max(f4)::text FROM {t}"),
                       ("nan", "SELECT count(*) FROM {t} WHERE f8 = 'NaN'"),
                       # one equality per infinity: an OR never prunes a chunk group
                       ("+inf", "SELECT count(*) FROM {t} WHERE f8 = 'Infinity'"),
                       ("-inf", "SELECT count(*) FROM {t} WHERE f8 = '-Infinity'")):
            r[k] = (_value(dsn, sql.format(t="mm_f")), _value(dsn, sql.format(t="mm_fh")))
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_every_column_returns_the_same_rows_as_heap(mm, expect):
    expect.text(mm["ranges"] or "none", "none",
                "every column returns the same rows as heap under range predicates")


def test_min_and_max_match_heap_on_every_column(mm, expect):
    expect.text(mm["bounds"] or "same", "same", "min and max match heap on every column")


def test_the_narrow_range_plan_is_a_columnar_custom_scan(mm, expect):
    expect.text(mm["columnar"], "yes", "the plan under test is a columnar custom scan")


def test_a_narrow_range_still_skips_row_groups(mm, expect):
    s = mm["skipped"]
    expect.text(s if s.startswith("QUERY_ERROR") else
                ("yes" if s.isdigit() and int(s) > 0 else f"no ({s or 'unreported'})"), "yes",
                "a narrow range still skips row groups")


def test_the_float_fixture_is_one_chunk_group(mm, expect):
    expect.text(mm["float groups"], "1", "the float fixture is one chunk group")


@pytest.mark.parametrize("key, name", [
    ("max f8", "max of a float column with NaN matches heap"),
    ("max f4", "max of a real column with NaN matches heap"),
    ("nan", "a row holding NaN is still found by an equality predicate"),
    ("+inf", "Infinity is found by an equality predicate"),
    ("-inf", "-Infinity is found by an equality predicate"),
])
def test_a_float_reading_matches_heap(mm, key, name, expect):
    columnar, heap = mm[key]
    expect.text(columnar, heap, name)


def test_float_types_are_not_on_the_direct_comparison_list(expect):
    text = (SRC / "columnar_write_state.c").read_text()
    expect.num(len(re.findall(r"COLUMNAR_FASTCMP_F(32|64)", text)), 0,
               "float types are not on the direct comparison list")
