"""A corrupt pgcolumnar catalog row is refused cleanly, or degrades safely, and no
backend dies (#1341). The pytest twin of `test/corruption.sh`, every check by the
shell's name.

The reader trusts the lengths, counts, offsets and descriptors the catalog stores.
Each scenario corrupts one of those fields on a fresh table and asserts that a read
raises XX001 (data_corrupted), or for the fields the reader tolerates, that it does
not crash. A crash is read from the server log ("terminated by signal"), counted
from the scenario's start. A client error alone cannot tell a refusal from a crash,
and a liveness probe passes once crash recovery has finished. Each corrupting UPDATE
must change a catalog row, or its scenario would pass on a table nobody corrupted.

A PRIVATE CLUSTER, because a crash would drop every session on the server.
"""
import pathlib
import shutil
import tempfile

import psycopg
import pytest

from pgc_vacuity import query_error

NO_SUCH_STORAGE = False     # True points every corruption at a storage id that does not exist


@pytest.fixture(scope="module")
def cor(pgc_cluster):
    """-> the private cluster, started, with the extension created."""
    from pgc_cluster import Cluster, pick_port

    work = pathlib.Path(tempfile.mkdtemp(prefix="pgc-corrupt-"))
    work.chmod(0o777)
    datadir = work / "data"
    datadir.mkdir()
    cluster = Cluster(pgc_cluster.pg_config, "corrupt", datadir, pick_port(16))
    try:
        cluster.initdb()
        cluster.start()
        with psycopg.connect(cluster.dsn(), autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        yield cluster
    finally:
        cluster.stop()
        shutil.rmtree(work, ignore_errors=True)


def _one(cluster, sql):
    """-> the first column of the first row, or a distinct failure sentinel."""
    try:
        with psycopg.connect(cluster.dsn(), autocommit=True, connect_timeout=5) as c:
            return c.execute(sql).fetchone()[0]
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0])


def _crashes(cluster):
    """-> how many backends the server log says died, or a failed reading when the
    log can't be read: a blind count must not read as "no crashes"."""
    try:
        return (cluster.datadir / "server.log").read_text(errors="replace").count("terminated by signal")
    except OSError as exc:
        return query_error(f"crash count unreadable: {exc}")


class _Scenario:
    """A fresh table c, the crash count at its start, and the probes the shell runs."""

    def __init__(self, cluster):
        self.cluster = cluster
        self.crashes = _crashes(cluster)
        with psycopg.connect(cluster.dsn(), autocommit=True) as c:
            c.execute("DROP TABLE IF EXISTS c")
            c.execute("CREATE TABLE c (id int, r bigint, t text) USING pgcolumnar")
            # r random: poorly compressible; id monotonic; t low-cardinality: dictionary
            c.execute("INSERT INTO c SELECT g, (random() * 9e18)::bigint, 'v' || (g % 5) "
                      "FROM generate_series(1, 20000) g")
        self.sid = 0 if NO_SUCH_STORAGE else _one(cluster, "SELECT pgcolumnar.get_storage_id('c')")

    def corrupt(self, update):
        """`update`, with {sid} filled. -> whether it changed a catalog row, as text."""
        return str(_one(self.cluster, f"WITH u AS ({update.format(sid=self.sid)} RETURNING 1) "
                                      "SELECT count(*) > 0 FROM u"))

    def refused(self, sql):
        """-> the SQLSTATE sql raised, or what happened instead. A crash ends the
        connection with no SQLSTATE at all."""
        try:
            with psycopg.connect(self.cluster.dsn(), autocommit=True) as c:
                row = c.execute(sql).fetchone()
            return f"no error ({row})"
        except psycopg.Error as exc:
            return exc.sqlstate or f"no error ({str(exc).splitlines()[0]})"

    def read(self, sql):
        """Run sql for its effect on the backend; its answer is not the subject."""
        try:
            with psycopg.connect(self.cluster.dsn(), autocommit=True) as c:
                c.execute(sql).fetchall()
        except psycopg.Error:
            pass

    def alive(self):
        # probe first: the postmaster logs a backend's death when it reaps it, so a
        # count read before the server answers again can miss the crash it is for
        up = _one(self.cluster, "SELECT 1") == 1
        now = _crashes(self.cluster)
        if not (isinstance(self.crashes, int) and isinstance(now, int)):
            return now if isinstance(self.crashes, int) else self.crashes
        crashed = now - self.crashes
        return "yes" if up and crashed == 0 else f"no ({crashed} backend crashes)"


def test_a_corrupt_byte_length(cor, expect):
    s = _Scenario(cor)
    field = "byte_length"
    expect.text(s.corrupt("UPDATE pgcolumnar.row_group SET byte_length = byte_length + 123456 "
                          "WHERE storage_id = {sid}"), "True",
                f"premise: the {field} corruption changed a catalog row")
    expect.text(s.refused("SELECT count(*), sum(r) FROM c"), "XX001", "byte_length corruption errors")
    expect.text(s.alive(), "yes", "alive after byte_length")


def test_a_corrupt_row_count(cor, expect):
    s = _Scenario(cor)
    field = "row_count"
    expect.text(s.corrupt("UPDATE pgcolumnar.row_group SET row_count = row_count + 100000 "
                          "WHERE storage_id = {sid}"), "True",
                f"premise: the {field} corruption changed a catalog row")
    expect.text(s.refused("SELECT sum(r) FROM c"), "XX001", "row_count corruption errors")
    expect.text(s.alive(), "yes", "alive after row_count")


def test_a_corrupt_page_length(cor, expect):
    s = _Scenario(cor)
    field = "page_length"
    expect.text(s.corrupt("UPDATE pgcolumnar.column_chunk SET page_length = page_length + 123456 "
                          "WHERE storage_id = {sid}"), "True",
                f"premise: the {field} corruption changed a catalog row")
    expect.text(s.refused("SELECT sum(r) FROM c"), "XX001", "page_length corruption errors")
    expect.text(s.alive(), "yes", "alive after page_length")


def test_a_corrupt_value_count_degrades_safely(cor, expect):
    s = _Scenario(cor)
    field = "value_count"
    expect.text(s.corrupt("UPDATE pgcolumnar.column_chunk SET value_count = value_count + 100000 "
                          "WHERE storage_id = {sid}"), "True",
                f"premise: the {field} corruption changed a catalog row")
    s.read("SELECT sum(r) FROM c")
    expect.text(s.alive(), "yes", "alive after value_count")


def test_a_corrupt_encoding_descriptor(cor, expect):
    s = _Scenario(cor)
    field = "encoding_descriptor"
    expect.text(s.corrupt("UPDATE pgcolumnar.column_chunk SET encoding_descriptor = '\\xffffffff'::bytea "
                          "WHERE storage_id = {sid} AND column_index = 1"), "True",
                f"premise: the {field} corruption changed a catalog row")
    expect.text(s.refused("SELECT * FROM c"), "XX001", "bad descriptor errors")
    expect.text(s.alive(), "yes", "alive after bad descriptor")


def test_a_negative_column_index(cor, expect):
    s = _Scenario(cor)
    field = "column_index"
    expect.text(s.corrupt("UPDATE pgcolumnar.column_chunk SET column_index = -1 "
                          "WHERE storage_id = {sid} AND column_index = 1"), "True",
                f"premise: the {field} corruption changed a catalog row")
    s.read("SELECT count(*) FROM c")
    expect.text(s.alive(), "yes", "alive after column_index=-1")


def test_a_corrupt_bloom_header_is_no_filter(cor, expect):
    """A huge nbits over a short buffer. Looking up an absent value answers 0 whether
    the filter is ignored or wrongly says "absent", so a value the table holds is
    looked up too: a false negative is the bloom's one way to lose rows."""
    s = _Scenario(cor)
    hit = _one(cor, "SELECT r FROM c WHERE id = 7777")
    field = "bloom filter"
    expect.text(s.corrupt("UPDATE pgcolumnar.bloom SET filter = '\\x0000004006'::bytea "
                          "WHERE storage_id = {sid}"), "True",
                f"premise: the {field} corruption changed a catalog row")
    expect.num(_one(cor, "SELECT count(*) FROM c WHERE r = -999"), 0, "corrupt bloom equality still correct")
    expect.num(_one(cor, f"SELECT count(*) FROM c WHERE r = {hit}"), 1,
               "corrupt bloom: a value the table holds is still found")
    expect.text(s.alive(), "yes", "alive after bloom corruption")
