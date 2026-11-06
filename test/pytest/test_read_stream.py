"""Reads through PostgreSQL's read stream return what the synchronous path returns
(#1341's twin of `test/read_stream.sh`, every check by the shell's name).

`pgcolumnar.enable_read_stream` routes a row group's block reads through a read stream
(PostgreSQL 17+), which can prefetch and, on 18+, use asynchronous I/O. Five query shapes
over a multi-stripe table are compared with a heap twin loaded from the same generator,
once with the setting on and once off, so each path is checked on its own.

THE SETTING IS READ BACK BEFORE EACH MODE. It arrives by ALTER DATABASE SET, whose
failure the shell's q drops, so a setting that never took ran both modes down the same
path: with it misspelled and the synchronous path broken, the shell passed 11 of 11.
A comparison is a set hash of each side, and a failed side reads as its own error.
A setting that cannot be applied is the read-back's value, so those two red by name.

WHAT THE [off] ARMS VOUCH FOR. The read-back proves the setting reached the session,
not which path a read took; nothing here observes the path. With the stream path taken
whatever the setting says, and the synchronous path copying one byte late, all 13 pass.
So the [off] arms vouch for the synchronous path only while the setting routes reads.
On PostgreSQL 15 and 16 the stream path is compiled out: [on] and [off] run the same code.

ON A PRIVATE DATABASE, built by a module fixture in the shell's order.
"""
import hashlib

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_read_stream"
SHAPES = {
    "full scan": "SELECT id, k, v, t FROM %T",
    "filtered": "SELECT id, v FROM %T WHERE k = 7",
    "range": "SELECT id, t FROM %T WHERE id BETWEEN 10000 AND 20000",
    "aggregate": "SELECT k, count(*), sum(v) FROM %T GROUP BY k",
    "wide sample": "SELECT * FROM %T WHERE id % 997 = 0",
}
NAMES = [
    "[on] full scan", "[on] filtered", "[on] range", "[on] aggregate", "[on] wide sample",
    "[off] full scan", "[off] filtered", "[off] range", "[off] aggregate", "[off] wide sample",
]


def _set_hash(dsn, sql):
    """-> an order-free hash of the rows, or a failed query."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            rows = sorted(repr(r) for r in conn.execute(sql).fetchall())
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
    return f"{len(rows)}:{hashlib.md5(chr(10).join(rows).encode()).hexdigest()}"


def _one(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            return str(conn.execute(sql).fetchone()[0])
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


@pytest.fixture(scope="module")
def rs(pgc_cluster):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            conn.execute("CREATE TABLE t_heap (id int, k int, v bigint, t text) USING heap")
            conn.execute("CREATE TABLE t_col (id int, k int, v bigint, t text) USING pgcolumnar")
            conn.execute("SELECT pgcolumnar.set_options('t_col', stripe_row_limit => 2000, chunk_group_row_limit => 1000)")
            conn.execute("INSERT INTO t_heap SELECT g, g%100, g::bigint*3, repeat('x', (g%40)+1) "
                         "FROM generate_series(1,50000) g")
            conn.execute("INSERT INTO t_col SELECT * FROM t_heap")
        r = {"stripes": _one(dsn, "SELECT count(*) FROM pgcolumnar.row_group "
                                  "WHERE storage_id = pgcolumnar.get_storage_id('t_col')")}
        for mode in ("on", "off"):
            # The setting failing to apply is what the read-back checks test, so its
            # error is a value they red on by name (#1522), and the comparisons still run.
            failed = None
            try:
                with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
                    a.execute(f"ALTER DATABASE {DB} SET pgcolumnar.enable_read_stream = {mode}")
            except psycopg.Error as exc:
                failed = query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
            r[f"setting {mode}"] = failed or _one(dsn, "SHOW pgcolumnar.enable_read_stream")
            for label, sql in SHAPES.items():
                h = _set_hash(dsn, sql.replace("%T", "t_heap"))
                c = _set_hash(dsn, sql.replace("%T", "t_col"))
                r[f"[{mode}] {label}"] = (c, h)
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_multiple_stripes_present(rs, expect):
    n = rs["stripes"]
    got = ("yes" if int(n) >= 10 else f"no ({n})") if n.isdigit() else f"no ({n})"
    expect.text(got, "yes", "multiple stripes present")


def test_a_fresh_session_reads_the_setting_on(rs, expect):
    expect.text(rs["setting on"], "on", "[on] a fresh session reads enable_read_stream as on")


def test_a_fresh_session_reads_the_setting_off(rs, expect):
    expect.text(rs["setting off"], "off", "[off] a fresh session reads enable_read_stream as off")


@pytest.mark.parametrize("name", NAMES)
def test_a_shape_matches_its_heap_twin(rs, name, expect):
    got, want = rs[name]
    expect.text(got, want, name)
