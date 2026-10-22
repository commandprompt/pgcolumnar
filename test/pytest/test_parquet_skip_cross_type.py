"""Parquet row-group skipping uses a constant of another type in the column's operator
family (#1712). The pytest twin of `test/parquet_skip_cross_type.sh`, every check by the
shell's name.

The group-exclusion check refused any constant whose type was not the column's, so
`WHERE id > 150000` (an int4 literal) on a bigint column read every row group. Over a
file of 200,000 ascending rows in 4 row groups, each arm reads the skip count from
EXPLAIN (ANALYZE): int4 constants on the int8 column (greater-than, equality, and the
constant on the left), a float4 constant on the float8 column, the answers compared with
the same query written in the column's own type, and a float4 NaN constant that must
still find the float8 column's NaN row.

The files are written by `python3` with pyarrow, as the shell writes them, so no test
imports pyarrow: each declines by the shell's skip name first, then asks for the module
fixture. ON A PRIVATE DATABASE.
"""
import re
import subprocess
import sys

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_parquet_skip_cross_type"

WRITER = r'''
import sys, pyarrow as pa, pyarrow.parquet as pq
w = sys.argv[1]
ids = list(range(200000))
pq.write_table(pa.table({"id": pa.array(ids, pa.int64()),
                         "f": pa.array([float(i) for i in ids], pa.float64())}),
               f"{w}/xt.parquet", row_group_size=50000)
pq.write_table(pa.table({"f": pa.array([1.0, 2.0, 3.0, float("nan")], pa.float64())}),
               f"{w}/xn.parquet", row_group_size=2)
print(pq.ParquetFile(f"{w}/xt.parquet").metadata.num_row_groups)
'''


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


def _run(dsn, sql):
    """-> None, or query_error() when the statement failed."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)
    except psycopg.Error as exc:
        return _failed(exc)
    return None


def _skipped(dsn, query):
    """-> the "Row Groups Skipped" count of EXPLAIN (ANALYZE), or query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            plan = "\n".join(r[0] for r in conn.execute(
                f"EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) {query}").fetchall())
    except psycopg.Error as exc:
        return _failed(exc)
    m = re.search(r"Row Groups Skipped: (\d+)", plan)
    return int(m.group(1)) if m else query_error(f"no skip count in: {plan.splitlines()[:1]}")


@pytest.fixture(scope="module")
def pxt(pgc_cluster, server_dir):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        w = server_dir
        wrote = subprocess.run([sys.executable, "-c", WRITER, str(w)], capture_output=True, text=True)
        for f in ("xt", "xn"):
            if (w / f"{f}.parquet").exists():
                (w / f"{f}.parquet").chmod(0o644)
        r = {"groups": int(wrote.stdout.strip()) if wrote.returncode == 0 and wrote.stdout.strip().isdigit()
             else query_error(f"the writer failed: {wrote.stderr.strip().splitlines()[-1:]}")}
        # A failed statement here makes every later reading a query_error, which
        # reddens its check by name rather than erroring the fixture.
        for sql in ("CREATE EXTENSION IF NOT EXISTS pgcolumnar",
                    "CREATE SERVER pq FOREIGN DATA WRAPPER pgcolumnar_parquet",
                    f"CREATE FOREIGN TABLE xt (id int8, f float8) SERVER pq OPTIONS (path '{w}/xt.parquet')",
                    f"CREATE FOREIGN TABLE xn (f float8) SERVER pq OPTIONS (path '{w}/xn.parquet')"):
            _run(dsn, sql)
        r["int8 skip"] = _skipped(dsn, "SELECT count(*) FROM xt WHERE id > 150000::int8")
        r["int4 skip"] = _skipped(dsn, "SELECT count(*) FROM xt WHERE id > 150000")
        r["int4 rows"] = _value(dsn, "SELECT count(*) FROM xt WHERE id > 150000")
        r["int8 rows"] = _value(dsn, "SELECT count(*) FROM xt WHERE id > 150000::int8")
        r["int4 eq skip"] = _skipped(dsn, "SELECT count(*) FROM xt WHERE id = 150001")
        r["left skip"] = _skipped(dsn, "SELECT count(*) FROM xt WHERE 150000 < id")
        r["float4 skip"] = _skipped(dsn, "SELECT count(*) FROM xt WHERE f < -1::float4")
        r["float4 rows"] = _value(dsn, "SELECT count(*) FROM xt WHERE f < 50000::float4")
        r["float8 rows"] = _value(dsn, "SELECT count(*) FROM xt WHERE f < 50000::float8")
        r["nan rows"] = _value(dsn, "SELECT count(*) FROM xn WHERE f = 'NaN'::float4")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _readings(request, expect):
    """Decline by the shell's skip name first; only then build the fixture."""
    if expect.require_module("pyarrow.parquet",
                             name="pyarrow not available; Parquet cross-type skip suite needs it") is None:
        return None
    return request.getfixturevalue("pxt")


def test_pxt_the_file_holds_four_row_groups(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.num(r["groups"], 4, "premise: the file holds 4 row groups (pyarrow's own count)")


def test_pxt_an_int8_constant_skips_three_groups(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.num(r["int8 skip"], 3, "control: an int8 constant on the int8 column skips 3 of the 4 row groups")


def test_pxt_an_int4_constant_skips_three_groups(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.num(r["int4 skip"], 3, "an int4 constant on the int8 column skips 3 of the 4 row groups")


def test_pxt_the_int4_constant_returns_the_int8_rows(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.num(r["int4 rows"], r["int8 rows"], "and returns the rows the int8 constant returns")


def test_pxt_an_int4_equality_skips_three_groups(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.num(r["int4 eq skip"], 3,
                   "an int4 constant in an equality on the int8 column skips 3 of the 4 row groups")


def test_pxt_a_constant_on_the_left_is_compared_the_same_way(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.num(r["left skip"], 3, "a constant on the left of the operator is compared the same way")


def test_pxt_a_float4_constant_below_every_row_skips_all_groups(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.num(r["float4 skip"], 4,
                   "a float4 constant below every row skips all 4 row groups of the float8 column")


def test_pxt_the_float4_constant_returns_the_float8_rows(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.num(r["float4 rows"], r["float8 rows"], "and returns the rows the float8 constant returns")


def test_pxt_a_float4_nan_still_finds_the_nan_row(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.num(r["nan rows"], 1, "a float4 NaN constant still finds the float8 column's NaN row")
