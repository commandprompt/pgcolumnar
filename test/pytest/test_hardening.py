"""Corrupted native metadata is refused or ignored, never trusted. The pytest twin of
`test/hardening.sh`, every check by the shell's name (#1341).

An invalid encoding descriptor on a chunk must raise a clean error (XX001), not crash
the backend. A malformed bloom filter must be ignored, read conservatively as "may
match", so a probe for a value that is present still finds it. Both are catalog
byteas, so a plain UPDATE reaches them; a premise asserts each UPDATE replaced exactly
one row, or the checks after it would read uncorrupted metadata and pass.

"Clean" is read as the SQLSTATE: an empty result is also what a crash and a missing
table give. A crash is read from the server log, counted from the corruption: a
liveness probe alone passes once crash recovery has finished. Each reading has a
connection of its own, opened once any recovery is over, as the shell's psql calls
are, so one crash cannot fail the scenario after it.

A PRIVATE CLUSTER, because a crash would drop every session on the server.
"""
import pathlib
import shutil
import tempfile
import time

import psycopg
import pytest

from pgc_vacuity import query_error

# the shell's bytes exactly: '\\x...' in a standard string is the ASCII text "\x...",
# which bytea's escape format stores as written
BAD_DESCRIPTOR = r"'\\xffffffff'::bytea"
BAD_BLOOM = r"'\\x00'::bytea"
PRESENT = "SELECT id FROM {t} WHERE k = ((7*2654435761)%100000)::bigint"
ABSENT = "SELECT count(*) FROM {t} WHERE k = 424242424"


def _connect(cluster):
    """A connection, after waiting out any crash recovery."""
    deadline = time.monotonic() + 60
    while True:
        try:
            return psycopg.connect(cluster.dsn(), autocommit=True, connect_timeout=5)
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)


def _failed(exc):
    return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _one(cluster, sql):
    try:
        with _connect(cluster) as c:
            return str(c.execute(sql).fetchone()[0])
    except psycopg.Error as exc:
        return _failed(exc)


def _tag(cluster, sql):
    """-> the command tag sql returns, as psql prints it ("UPDATE 1")."""
    try:
        with _connect(cluster) as c:
            cur = c.execute(sql)
            return cur.statusmessage or ""
    except psycopg.Error as exc:
        return _failed(exc)


def _rows(cluster, sql):
    try:
        with _connect(cluster) as c:
            return c.execute(sql).fetchall()
    except psycopg.Error as exc:
        return _failed(exc)


def _sqlstate(cluster, sql):
    """-> the SQLSTATE sql raised, or what happened instead: a crash ends the
    connection with no SQLSTATE at all."""
    try:
        with _connect(cluster) as c:
            rows = c.execute(sql).fetchall()
        return f"no error ({len(rows)} rows)"
    except psycopg.Error as exc:
        return exc.sqlstate or f"no error ({str(exc).splitlines()[0]})"


def _crashes(cluster):
    try:
        return (cluster.datadir / "server.log").read_text(errors="replace").count("terminated by signal")
    except OSError:
        return 0


def _alive(cluster, before):
    """-> "yes" when the server answers and no backend died since `before`."""
    up = _one(cluster, "SELECT 1") == "1"
    died = _crashes(cluster) - before
    return "yes" if up and died == 0 else f"no ({died} backend crashes)"


def _steps(cluster, *sqls):
    # a step that fails is printed and left for the checks to see
    for sql in sqls:
        try:
            with _connect(cluster) as c:
                c.execute(sql)
        except psycopg.Error as exc:
            print(f"-- step failed: {str(exc).splitlines()[0] if str(exc) else exc!r}")


def _pair(cluster, defs, load, *between):
    _steps(cluster, "DROP TABLE IF EXISTS t_heap", "DROP TABLE IF EXISTS t_col",
           f"CREATE TABLE t_heap ({defs}) USING heap",
           f"CREATE TABLE t_col ({defs}) USING pgcolumnar", *between,
           f"INSERT INTO t_heap {load}", "INSERT INTO t_col SELECT * FROM t_heap")


@pytest.fixture(scope="module")
def hard(pgc_cluster):
    """Every step of the shell, in its order, each reading recorded as a value so each
    check is its own test and one red cannot stop the rest."""
    from pgc_cluster import Cluster, pick_port

    work = pathlib.Path(tempfile.mkdtemp(prefix="pgc-hardening-"))
    work.chmod(0o777)
    datadir = work / "data"
    datadir.mkdir()
    cluster = Cluster(pgc_cluster.pg_config, "hardening", datadir, pick_port(25))
    try:
        cluster.initdb()
        cluster.start()
        _steps(cluster, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        r = {}

        _pair(cluster, "id int, v bigint", "SELECT g, g*2 FROM generate_series(1,3000) g")
        r["descriptor lands"] = _tag(
            cluster, f"UPDATE pgcolumnar.column_chunk SET encoding_descriptor = {BAD_DESCRIPTOR} "
                     "WHERE storage_id = pgcolumnar.get_storage_id('t_col') AND column_index = 1")
        before = _crashes(cluster)
        r["descriptor"] = _sqlstate(cluster, "SELECT * FROM t_col")
        r["descriptor alive"] = _alive(cluster, before)

        _pair(cluster, "id int, k bigint",
              "SELECT g, ((g*2654435761)%100000)::bigint FROM generate_series(1,8000) g",
              "SELECT pgcolumnar.set_options('t_col', chunk_group_row_limit => 1000, "
              "stripe_row_limit => 20000)")
        r["bloom lands"] = _tag(
            cluster, f"UPDATE pgcolumnar.bloom SET filter = {BAD_BLOOM} "
                     "WHERE storage_id = pgcolumnar.get_storage_id('t_col') AND column_index = 1")
        before = _crashes(cluster)
        for key, sql in (("present", PRESENT), ("absent", ABSENT)):
            r[key] = (_rows(cluster, sql.format(t="t_col")), _rows(cluster, sql.format(t="t_heap")))
        r["bloom alive"] = _alive(cluster, before)
        yield r
    finally:
        cluster.stop()
        shutil.rmtree(work, ignore_errors=True)


def test_the_corrupt_descriptor_replaces_one_chunks(hard, expect):
    expect.text(hard["descriptor lands"], "UPDATE 1", "premise: the corrupt descriptor replaces one chunk's")


def test_an_invalid_descriptor_raises_an_error(hard, expect):
    expect.text(hard["descriptor"], "XX001", "invalid descriptor raises error")


def test_the_backend_is_alive_after_an_invalid_descriptor(hard, expect):
    expect.text(hard["descriptor alive"], "yes", "backend alive after invalid descriptor")


def test_the_malformed_bloom_replaces_ks_filter(hard, expect):
    expect.text(hard["bloom lands"], "UPDATE 1", "premise: the malformed bloom replaces k's filter")


def test_a_corrupt_bloom_still_finds_a_present_value(hard, expect):
    expect.row_set(*hard["present"], "corrupt bloom present")


def test_a_corrupt_bloom_still_counts_an_absent_value(hard, expect):
    expect.row_set(*hard["absent"], "corrupt bloom absent")


def test_the_backend_is_alive_after_a_corrupt_bloom(hard, expect):
    expect.text(hard["bloom alive"], "yes", "backend alive after corrupt bloom")
