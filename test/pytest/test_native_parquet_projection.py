"""Parquet column-projection pushdown (Phase G). The pytest twin of
`test/native_parquet_projection.sh`, every check by the shell's name (#1341).

The FDW scan decodes only the column chunks a query references. Asserted: each
projection, a qual on an unselected column, and a projection behind a dropped column
return the generator's rows; the EXPLAIN "Columns Read" counter reflects each
projection (0 for count(*), 4 for SELECT *, a qual column counted); and a column whose
data page is smashed is never decoded when it is projected out.

The oracle is built in SQL from the generator `test/craft_parquet_projection.py` wrote
the file from, never read back through read_parquet, which shares the scan's decoders.
The crafter runs as a subprocess so the fixture never imports pyarrow: each test
declines under the shell's skip name first, and only then asks for the fixture.

ON A PRIVATE DATABASE; each test asserts one reading (#1339). A failed reading is a
query_error(), never a value: two failed reads never agree.
"""
import pathlib
import re
import subprocess
import sys

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_native_parquet_projection"
CRAFTER = pathlib.Path(__file__).resolve().parents[1] / "craft_parquet_projection.py"
COLS = "a int, b int8, c text, d float8"


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _value(dsn, sql):
    """-> the first column of the first row as text, or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    if row is None or row[0] is None:
        return query_error(f"no value from {sql}")
    return str(row[0])


def _set_hash(dsn, query):
    """-> the shell's pgc_set_hash: md5 of the sorted row texts, EMPTY for no rows."""
    return _value(dsn, "SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), 'EMPTY') "
                       f"FROM (SELECT _row::text AS t FROM ( {query} ) _row) _s")


def _cols_read(dsn, query):
    """-> the scan's EXPLAIN ANALYZE "Columns Read" count, as the shell's cols_read."""
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            lines = [r[0] for r in conn.execute(
                f"EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) {query}").fetchall()]
    except psycopg.Error as exc:
        return query_error(_message(exc))
    for line in lines:
        m = re.search(r"Columns Read:\s*(\d+)", line)
        if m:
            return m.group(1)
    return query_error(f"no Columns Read line for {query}")


def _setup(dsn, w):
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        conn.execute("CREATE SERVER pq FOREIGN DATA WRAPPER pgcolumnar_parquet")
        conn.execute(f"CREATE FOREIGN TABLE ft ({COLS}) SERVER pq OPTIONS (path '{w}/proj.parquet')")
        conn.execute(f"CREATE TABLE h ({COLS})")
        # the generator the crafter wrote proj.parquet from, never the reader under test
        conn.execute("INSERT INTO h SELECT g, g * 2, 's' || (g % 20), g::float8 / 3.0 "
                     "FROM generate_series(0, 4999) g")
        conn.execute(f"CREATE FOREIGN TABLE ftd (z int, {COLS}) SERVER pq "
                     f"OPTIONS (path '{w}/proj.parquet')")
        conn.execute("ALTER FOREIGN TABLE ftd DROP COLUMN z")
        conn.execute(f"CREATE FOREIGN TABLE ftc (g int, bad int) SERVER pq "
                     f"OPTIONS (path '{w}/corrupt.parquet')")
        conn.execute("CREATE FUNCTION pgc_try_proj(q text) RETURNS text LANGUAGE plpgsql AS $$ "
                     "BEGIN EXECUTE q; RETURN 'OK'; "
                     "EXCEPTION WHEN OTHERS THEN RETURN 'ERROR'; END $$")


@pytest.fixture(scope="module")
def npj(pgc_cluster, server_dir):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    dsn = pgc_cluster.dsn(dbname=DB)
    w = server_dir / "npj"
    w.mkdir(exist_ok=True)
    w.chmod(0o777)
    subprocess.run([sys.executable, str(CRAFTER), str(w)], capture_output=True, text=True)
    for f in w.iterdir():
        f.chmod(0o644)
    try:
        try:
            # A setup step that fails is not raised: every reading below then fails
            # by its own name, as the shell's do, rather than erroring all 17 tests.
            _setup(dsn, w)
        except psycopg.Error:
            pass
        r = {}
        for sel in ("a", "c", "a, d", "b, c, d", "*"):
            r[f"proj {sel}"] = (_set_hash(dsn, f"SELECT {sel} FROM ft"), _set_hash(dsn, f"SELECT {sel} FROM h"))
        r["qual"] = (_set_hash(dsn, "SELECT a FROM ft WHERE c = 's7'"),
                     _set_hash(dsn, "SELECT a FROM h  WHERE c = 's7'"))
        r["cr one"] = _cols_read(dsn, "SELECT a FROM ft")
        r["cr two"] = _cols_read(dsn, "SELECT a, d FROM ft")
        r["cr star"] = _cols_read(dsn, "SELECT * FROM ft")
        r["cr count"] = _cols_read(dsn, "SELECT count(*) FROM ft")
        r["drop proj"] = (_set_hash(dsn, "SELECT d FROM ftd"), _set_hash(dsn, "SELECT d FROM h"))
        r["drop cr"] = _cols_read(dsn, "SELECT d FROM ftd")
        r["cr qual"] = _cols_read(dsn, "SELECT a FROM ft WHERE c = 's7'")
        tryq = lambda q: _value(dsn, f"SELECT pgc_try_proj($q${q}$q$)")
        r["good"] = tryq("SELECT count(g) FROM ftc")
        r["count"] = tryq("SELECT count(*) FROM ftc")
        r["bad"] = tryq("SELECT count(bad) FROM ftc")
        r["sum"] = _value(dsn, "SELECT sum(g)::text FROM ftc")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")




def test_the_a_projection_matches_the_oracle(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; projection suite needs it") is None:
        return
    got, want = request.getfixturevalue("npj")["proj a"]
    expect.text(got, want, "projection [a] == oracle")


def test_the_c_projection_matches_the_oracle(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; projection suite needs it") is None:
        return
    got, want = request.getfixturevalue("npj")["proj c"]
    expect.text(got, want, "projection [c] == oracle")


def test_the_a_d_projection_matches_the_oracle(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; projection suite needs it") is None:
        return
    got, want = request.getfixturevalue("npj")["proj a, d"]
    expect.text(got, want, "projection [a, d] == oracle")


def test_the_b_c_d_projection_matches_the_oracle(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; projection suite needs it") is None:
        return
    got, want = request.getfixturevalue("npj")["proj b, c, d"]
    expect.text(got, want, "projection [b, c, d] == oracle")


def test_the_star_projection_matches_the_oracle(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; projection suite needs it") is None:
        return
    got, want = request.getfixturevalue("npj")["proj *"]
    expect.text(got, want, "projection [*] == oracle")


def test_a_qual_on_an_unselected_column_matches_the_oracle(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; projection suite needs it") is None:
        return
    got, want = request.getfixturevalue("npj")["qual"]
    expect.text(got, want, "qual on unselected column == oracle")


def test_one_column_reads_one(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; projection suite needs it") is None:
        return
    expect.text(request.getfixturevalue("npj")["cr one"], "1", "one column reads 1")


def test_two_columns_read_two(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; projection suite needs it") is None:
        return
    expect.text(request.getfixturevalue("npj")["cr two"], "2", "two columns read 2")


def test_select_star_reads_four(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; projection suite needs it") is None:
        return
    expect.text(request.getfixturevalue("npj")["cr star"], "4", "select star reads 4")


def test_count_star_reads_zero(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; projection suite needs it") is None:
        return
    expect.text(request.getfixturevalue("npj")["cr count"], "0", "count(*) reads 0")


def test_after_a_dropped_column_the_d_projection_matches_the_oracle(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; projection suite needs it") is None:
        return
    got, want = request.getfixturevalue("npj")["drop proj"]
    expect.text(got, want, "after a dropped column, projection [d] == oracle")


def test_after_a_dropped_column_one_column_reads_one(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; projection suite needs it") is None:
        return
    expect.text(request.getfixturevalue("npj")["drop cr"], "1", "after a dropped column, one column reads 1")


def test_a_selected_column_and_a_qual_column_read_two(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; projection suite needs it") is None:
        return
    expect.text(request.getfixturevalue("npj")["cr qual"], "2", "selected 1 + qual 1 reads 2")


def test_selecting_the_good_column_skips_the_corrupt_one(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; projection suite needs it") is None:
        return
    expect.text(request.getfixturevalue("npj")["good"], "OK", "selecting the good column skips the corrupt one")


def test_count_star_decodes_nothing_and_survives_the_corruption(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; projection suite needs it") is None:
        return
    expect.text(request.getfixturevalue("npj")["count"], "OK", "count(*) decodes nothing, survives corruption")


def test_selecting_the_corrupt_column_does_error(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; projection suite needs it") is None:
        return
    expect.text(request.getfixturevalue("npj")["bad"], "ERROR", "selecting the corrupt column does error")


def test_the_good_column_values_are_still_correct(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; projection suite needs it") is None:
        return
    expect.text(request.getfixturevalue("npj")["sum"], "499500", "good-column values are still correct")
