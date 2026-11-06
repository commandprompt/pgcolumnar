"""TABLESAMPLE on a columnar table raises 0A000 rather than answering every row. The pytest
twin of `test/tablesample.sh`, every check by the shell's name (#1341).

docs/limitations.md says TABLESAMPLE is unsupported and raises. The access method's
sample callbacks already raise 0A000, but the planner hooks did not look at
`rte->tablesample`, so the custom scan or a vectorized aggregate replaced Sample Scan and
answered every row: 10000 of 10000 where heap returns about 1000. Asserted: five sampled
shapes raise 0A000; an unsampled count, and the same sample on a heap table, raise
nothing.

A STATEMENT IS READ AS ITS SQLSTATE: the code it raised, "00000" when it ran, or
"no reading" when the server could not be reached. A failed connection used to read
"00000", so with the server down both controls passed.
"""
import psycopg
import pytest

DB = "pgc_tablesample"
# The shell's cluster runs with max_parallel_workers_per_gather = 0, and whether the
# grouped shape reaches the custom scan when the planner hook is gone depends on it:
# left at its default here, that shape planned a Sample Scan, which raises anyway.
NOPAR = "SET max_parallel_workers_per_gather = 0"

SAMPLED = [
    ("count(*) TABLESAMPLE raises 0A000 rather than answering every row",
     "SELECT count(*) FROM n TABLESAMPLE BERNOULLI(10) REPEATABLE (7)", ()),
    ("sum() TABLESAMPLE raises 0A000 rather than folding the whole table",
     "SELECT sum(id) FROM n TABLESAMPLE BERNOULLI(10) REPEATABLE (7)", ()),
    ("SYSTEM TABLESAMPLE raises 0A000 as well",
     "SELECT count(*) FROM n TABLESAMPLE SYSTEM(10) REPEATABLE (7)", ()),
    ("a projected TABLESAMPLE scan raises 0A000 rather than returning every id",
     "SELECT id FROM n TABLESAMPLE BERNOULLI(10) REPEATABLE (7)", ()),
    ("grouped TABLESAMPLE raises 0A000 rather than grouping every row",
     "SELECT id % 10, count(*) FROM n TABLESAMPLE BERNOULLI(10) REPEATABLE (7) GROUP BY 1",
     ("SET pgcolumnar.enable_group_vectorization = on",)),
]
CONTROLS = [
    ("control: an unsampled count reports success, so the probe is not stuck on 0A000",
     "SELECT count(*) FROM n"),
    ("control: the same TABLESAMPLE on a heap table raises nothing",
     "SELECT count(*) FROM h TABLESAMPLE BERNOULLI(10) REPEATABLE (7)"),
]


def _sqlstate(dsn, sql, settings=()):
    """-> the SQLSTATE `sql` raised, "00000" when it ran, or "no reading" when the server
    could not be reached."""
    try:
        conn = psycopg.connect(dsn, autocommit=True, connect_timeout=5)
    except psycopg.OperationalError as exc:
        return f"no reading ({str(exc).splitlines()[0] if str(exc) else type(exc).__name__})"
    with conn:
        try:
            for s in (NOPAR,) + tuple(settings):
                conn.execute(s)
            conn.execute(sql).fetchall()
        except psycopg.Error as exc:
            return exc.sqlstate or f"no reading ({type(exc).__name__})"
    return "00000"


@pytest.fixture(scope="module")
def ts(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            conn.execute("CREATE TABLE n (id int, v text) USING pgcolumnar")
            conn.execute("INSERT INTO n SELECT g, 'x' || g FROM generate_series(1, 10000) g")
            rows = conn.execute("SELECT count(*) FROM n").fetchone()[0]
            conn.execute("CREATE TABLE h (id int, v text)")
            conn.execute("INSERT INTO h SELECT g, 'x' || g FROM generate_series(1, 10000) g")
        yield dsn, rows
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_the_table_holds_its_rows(ts, expect):
    expect.num(ts[1], 10000, "premise: the table holds 10000 rows")


@pytest.mark.parametrize("name,sql,settings", SAMPLED)
def test_a_sampled_shape_raises_0a000(ts, name, sql, settings, expect):
    expect.text(_sqlstate(ts[0], sql, settings), "0A000", name)


@pytest.mark.parametrize("name,sql", CONTROLS)
def test_a_control_raises_nothing(ts, name, sql, expect):
    expect.text(_sqlstate(ts[0], sql), "00000", name)
