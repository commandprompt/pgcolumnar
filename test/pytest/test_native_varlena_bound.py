"""A varlena's stored length is bounded by its buffer. The pytest twin of
`test/native_varlena_bound.sh`, every check by the shell's name.

A varying-length value in a native chunk or a zone map's min/max carries its own
length prefix, and every decode site reads it through
PgColumnarVarSizeAnyUnalignedBounded, which refuses a length (or an external-TOAST
tag) that would read past the buffer. The zone map's min/max is a catalog bytea, so
a plain UPDATE reaches it: a 5-byte value whose 4-byte header claims ~1 GB, then a
predicate that consults it. Unbounded, that is a ~1 GB memcpy from 5 bytes and a
SIGSEGV; bounded, a clean XX001 and a surviving backend. A crash is read from the
server log, counted from the corruption: a liveness probe alone passes once crash
recovery has finished.

A PRIVATE CLUSTER, because a crash would drop every session on the server.
"""
import pathlib
import shutil
import tempfile
import time

import psycopg
import pytest

from pgc_vacuity import query_error

# the text column's whole-chunk zone map: column 0 is the int id, whose min/max is no varlena
WHERE = "storage_id = {sid} AND column_index = 1 AND vector_index = -1"
# 0xfcffffff is a 4-byte varlena header claiming 0x3fffffff bytes; the value is 5
OVERLONG = r"'\xfcffffff00'::bytea"
# 0x01 is the 1-byte external-TOAST tag, which native storage never holds
EXTERNAL = r"'\x0102030405060708090a0b0c0d0e0f1011'::bytea"
# the header bytes are little-endian's layout
EXACT, ONE_PAST, CUT_HEADER = r"'\x077631'::bytea", r"'\x097631'::bytea", r"'\x0c0000'::bytea"


def _connect(cluster):
    """A connection, after waiting out any crash recovery: each scenario starts on
    a server that accepts it, so one crash cannot fail the scenarios after it."""
    deadline = time.monotonic() + 60
    while True:
        try:
            return psycopg.connect(cluster.dsn(), autocommit=True, connect_timeout=5)
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)


def _one(cluster, sql):
    try:
        with _connect(cluster) as c:
            return str(c.execute(sql).fetchone()[0])
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0])


def _refused(cluster, sql):
    """-> the SQLSTATE sql raised, or what happened instead: a crash ends the
    connection with no SQLSTATE at all."""
    try:
        with _connect(cluster) as c:
            row = c.execute(sql).fetchone()
        return f"no error ({row})"
    except psycopg.Error as exc:
        return exc.sqlstate or f"no error ({str(exc).splitlines()[0]})"


def _refusal(cluster, sql):
    """-> "SQLSTATE: message" of the error sql raised: the SQLSTATE alone cannot tell
    which bound refused, since with the header bound gone the length check after it
    raises the same XX001 (#1524 review)."""
    try:
        with _connect(cluster) as c:
            row = c.execute(sql).fetchone()
        return f"no error ({row})"
    except psycopg.Error as exc:
        if exc.sqlstate is None:
            return f"no error ({str(exc).splitlines()[0]})"
        return f"{exc.sqlstate}: {exc.diag.message_primary}"


def _crashes(cluster):
    try:
        return (cluster.datadir / "server.log").read_text(errors="replace").count("terminated by signal")
    except OSError:
        return 0


def _scenario(cluster, sql):
    """-> (the SQLSTATE sql raised, whether the backend survived it)."""
    before = _crashes(cluster)
    state = _refused(cluster, sql)
    up = _one(cluster, "SELECT 1") == "1"
    died = _crashes(cluster) - before
    return state, "yes" if up and died == 0 else f"no ({died} backend crashes)"


@pytest.fixture(scope="module")
def vb(pgc_cluster):
    """Every step of the shell, in its order, each reading recorded as a value so each
    check is its own test and one red cannot stop the rest."""
    from pgc_cluster import Cluster, pick_port

    work = pathlib.Path(tempfile.mkdtemp(prefix="pgc-varlena-"))
    work.chmod(0o777)
    datadir = work / "data"
    datadir.mkdir()
    cluster = Cluster(pgc_cluster.pg_config, "varlena", datadir, pick_port(24))
    try:
        cluster.initdb()
        cluster.start()
        with _connect(cluster) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            c.execute("CREATE TABLE t (id int, txt text) USING pgcolumnar")
            c.execute("INSERT INTO t SELECT g, 'v' || g FROM generate_series(1, 2000) g")
            sid = c.execute("SELECT pgcolumnar.get_storage_id('t')").fetchone()[0]
            # the untouched bounds, to put back: native_zone_excludes decodes the minimum
            # BEFORE the maximum, so a corrupt minimum left in place would answer for
            # the maximum's check, which would then never reach the maximum
            where = WHERE.format(sid=sid)
            orig_min, orig_max = c.execute(f"SELECT minimum, maximum FROM pgcolumnar.zone_map WHERE {where}").fetchone()

        def put(col, value, params=()):
            # a write that fails is printed and left for the checks to see
            try:
                with _connect(cluster) as c:
                    c.execute(f"UPDATE pgcolumnar.zone_map SET {col} = {value} WHERE {where}", params)
            except psycopg.Error as exc:
                print(f"-- zone map write failed: {str(exc).splitlines()[0]}")

        r = {"zone maps": _one(cluster, f"SELECT count(*) FROM pgcolumnar.zone_map WHERE {where}")}
        put("minimum", OVERLONG)
        r["min"] = _scenario(cluster, "SELECT count(*) FROM t WHERE txt > 'a'")
        put("minimum", "%s", (orig_min,))
        r["restored"] = _one(cluster, "SELECT count(*) FROM t WHERE txt < 'zzz'")
        put("maximum", OVERLONG)
        r["max"] = _scenario(cluster, "SELECT count(*) FROM t WHERE txt < 'zzz'")
        put("maximum", "%s", (orig_max,))
        put("minimum", EXTERNAL)
        r["external"] = _scenario(cluster, "SELECT count(*) FROM t WHERE txt > 'a'")
        # the boundary (#1524 review): a length far past the stream cannot tell a
        # correct bound from an off-by-one. 0x07 is a 1-byte header saying 3 bytes,
        # header included: 'v1', exactly the stream; 0x09 says 4, one past it. Then
        # 3 bytes whose first is a 4-byte header, which itself runs past the end, read
        # by its message: the length check after the header bound raises the same XX001.
        put("minimum", EXACT)
        r["exact"] = _one(cluster, "SELECT count(*) FROM t WHERE txt > 'a'")
        put("minimum", ONE_PAST)
        r["one past"] = _refused(cluster, "SELECT count(*) FROM t WHERE txt > 'a'")
        put("minimum", CUT_HEADER)
        r["cut header"] = _refusal(cluster, "SELECT count(*) FROM t WHERE txt > 'a'")
        yield r
    finally:
        cluster.stop()
        shutil.rmtree(work, ignore_errors=True)


def test_the_txt_column_has_a_whole_chunk_zone_map_to_corrupt(vb, expect):
    expect.text(vb["zone maps"], "1", "the txt column has a whole-chunk zone map to corrupt")


def test_a_min_with_an_over_long_varlena_length_is_refused(vb, expect):
    expect.text(vb["min"][0], "XX001", "a min with an over-long varlena length is refused, not an OOB read (XX001)")


def test_backend_survived_the_corrupt_minimum(vb, expect):
    expect.text(vb["min"][1], "yes", "backend survived the corrupt minimum")


def test_with_the_minimum_restored_the_range_read_is_clean(vb, expect):
    expect.text(vb["restored"], "2000", "with the minimum restored, the range read is clean")


def test_a_max_with_an_over_long_varlena_length_is_refused(vb, expect):
    expect.text(vb["max"][0], "XX001", "a max with an over-long varlena length is refused, not an OOB read (XX001)")


def test_backend_survived_the_corrupt_maximum(vb, expect):
    expect.text(vb["max"][1], "yes", "backend survived the corrupt maximum")


def test_an_external_toast_tagged_min_value_is_refused(vb, expect):
    expect.text(vb["external"][0], "XX001", "an external-TOAST-tagged min value is refused (XX001)")


def test_backend_survived_the_external_tagged_minimum(vb, expect):
    expect.text(vb["external"][1], "yes", "backend survived the external-tagged minimum")


def test_a_min_whose_length_exactly_fits_its_buffer_reads_cleanly(vb, expect):
    expect.text(vb["exact"], "2000", "a min whose length exactly fits its buffer reads cleanly")


def test_a_min_whose_length_runs_one_byte_past_its_buffer_is_refused(vb, expect):
    expect.text(vb["one past"], "XX001", "a min whose length runs one byte past its buffer is refused (XX001)")


def test_a_4_byte_header_cut_off_by_the_buffers_end_is_refused(vb, expect):
    expect.text(vb["cut header"], "XX001: pgcolumnar: varlena header runs past the value stream end",
                "a 4-byte header cut off by the buffer's end is refused (XX001)")
