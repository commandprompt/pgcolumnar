"""pgcolumnar.enable_bloom_filter governs the write side too. The pytest twin of
`test/bloom_setting.sh`, every check by the shell's name (#1341).

The setting was consulted only by the reader, so turning it off still hashed every
value and stored a filter for every chunk. With it off nothing is written; and a chunk
written with no filter must still answer equality, because a reader that treated "no
filter" as "no match" would skip real rows silently. A projection is written through
its own write state, so its own filters are counted.

ON A PRIVATE DATABASE, built once by a module fixture in the shell's order; each test
asserts one reading, so one red cannot stop the next (#1339). A reading carries what
it was read over -- the rows a filterless table holds, the rows two tables were
compared on -- so a load that failed cannot pass as "no filters" or "no mismatch".
"""
import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_bloom_setting"
ROWS = 20000
PRESENT = f"k{ROWS // 2}"


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _run(dsn, sql):
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


def _filters(dsn, table):
    return _value(dsn, "SELECT count(*) FROM pgcolumnar.bloom f JOIN pgcolumnar.storage s "
                       f"ON s.storage_id = f.storage_id WHERE s.relation_oid = '{table}'::regclass")


def _build(dsn, table, setting):
    _run(dsn, f"DROP TABLE IF EXISTS {table}; SET pgcolumnar.enable_bloom_filter = {setting}; "
              f"CREATE TABLE {table} (id int, v text) USING pgcolumnar;")
    _run(dsn, f"SET pgcolumnar.enable_bloom_filter = {setting}; "
              f"INSERT INTO {table} SELECT g, 'k' || g FROM generate_series(1, {ROWS}) g;")


def _probe(dsn, table, setting, value):
    return _value(dsn, f"SELECT count(*) FROM {table} WHERE v = '{value}'",
                  [f"SET pgcolumnar.enable_bloom_filter = {setting}"])


@pytest.fixture(scope="module")
def bst(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r = {}
        _build(dsn, "bs_on", "on")
        r["on filters"] = _filters(dsn, "bs_on")
        _build(dsn, "bs_off", "off")
        r["off filters"] = _filters(dsn, "bs_off")
        r["off rows"] = _value(dsn, "SELECT count(*) FROM bs_off")
        for setting in ("on", "off"):
            r[f"off/{setting} present"] = _probe(dsn, "bs_off", setting, PRESENT)
            r[f"off/{setting} absent"] = _probe(dsn, "bs_off", setting, "nosuchvalue")
        r["on/off present"] = _probe(dsn, "bs_on", "off", PRESENT)
        r["identical"] = _value(dsn, "SELECT count(*) FILTER (WHERE a.v IS DISTINCT FROM b.v) || ' of ' || count(*) "
                                     "FROM bs_on a FULL JOIN bs_off b USING (id)")

        # projections follow the same decision, through their own write state
        _run(dsn, "DROP TABLE IF EXISTS bs_p; SET pgcolumnar.enable_bloom_filter = on; "
                  "CREATE TABLE bs_p (id int, v text) USING pgcolumnar; "
                  "SELECT pgcolumnar.add_projection('bs_p', 'bs_p_proj', ARRAY['v'], ARRAY['v']);")
        _run(dsn, "SET pgcolumnar.enable_bloom_filter = on; "
                  f"INSERT INTO bs_p SELECT g, 'p' || g FROM generate_series(1, {ROWS}) g;")
        r["proj filters"] = _value(dsn, "SELECT count(*) FROM pgcolumnar.bloom WHERE storage_id IN "
                                        "(SELECT proj_storage_id FROM pgcolumnar.projection WHERE name = 'bs_p_proj')")
        r["proj read"] = _value(dsn, f"SELECT count(*) FROM bs_p WHERE v = 'p{ROWS // 2}'")
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _some(n):
    return ("some" if n > 0 else f"none ({n})") if isinstance(n, int) else n


def test_filters_are_written_when_the_setting_is_on(bst, expect):
    expect.text(_some(bst["on filters"]), "some", "filters are written when the setting is on")


def test_no_filters_are_written_when_the_setting_is_off(bst, expect):
    f, n = bst["off filters"], bst["off rows"]
    got = f"{f} filters over {n} rows" if isinstance(f, int) and isinstance(n, int) else query_error(f"{f} / {n}")
    expect.text(got, f"0 filters over {ROWS} rows", "no filters are written when the setting is off")


def test_written_off_read_on_finds_a_present_value(bst, expect):
    expect.text(str(bst["off/on present"]), "1", "written off, read with the setting on: present value found")


def test_written_off_read_on_does_not_find_an_absent_value(bst, expect):
    expect.text(str(bst["off/on absent"]), "0", "written off, read with the setting on: absent value not found")


def test_written_off_read_off_finds_a_present_value(bst, expect):
    expect.text(str(bst["off/off present"]), "1", "written off, read with the setting off: present value found")


def test_written_off_read_off_does_not_find_an_absent_value(bst, expect):
    expect.text(str(bst["off/off absent"]), "0", "written off, read with the setting off: absent value not found")


def test_written_on_read_off_finds_a_present_value(bst, expect):
    expect.text(str(bst["on/off present"]), "1", "written on, read with the setting off: present value found")


def test_a_full_scan_of_the_unfiltered_table_is_complete(bst, expect):
    expect.text(str(bst["off rows"]), str(ROWS), "a full scan of the unfiltered table is complete")


def test_the_filtered_and_unfiltered_tables_hold_identical_data(bst, expect):
    expect.text(bst["identical"], f"0 of {ROWS}", "the two tables hold identical data")


def test_a_projection_still_gets_filters_when_the_setting_is_on(bst, expect):
    expect.text(_some(bst["proj filters"]), "some", "a projection still gets filters when the setting is on")


def test_the_projected_table_reads_correctly(bst, expect):
    expect.text(str(bst["proj read"]), "1", "the projected table reads correctly")
