"""Parquet compression-codec read coverage (Phase G). The pytest twin of
`test/native_parquet_codecs.sh`, every check by the shell's name (#1341).

Files pyarrow writes with GZIP, ZSTD and LZ4_RAW, plus an uncompressed control, are
read back through `read_parquet` and the FDW, and a gzip page whose header declares an
uncompressed size of -1 must be refused with the reader's own decode error. Asserted:
each file carries the codec it claims (read from its own metadata, so no arm passes on
a file pyarrow left uncompressed); every file decodes to the generator's rows, built in
SQL from the same generator the crafter wrote them from -- not to the uncompressed file
read back through the reader under test, which a defect after decompression corrupts
alike; each FDW count and a sum read every value; and the crafted header is a clean
refusal that kills no backend.

The files come from `test/craft_parquet_codecs.py`, the shell's own crafter, run as a
subprocess so the fixture never imports pyarrow: each test declines under the shell's
skip name first, and only then asks for the fixture.

ON A PRIVATE DATABASE; each test asserts one reading (#1339). A failed reading is a
query_error(), never a value: two failed reads never agree.
"""
import pathlib
import subprocess
import sys

import psycopg
import pytest

from pgc_vacuity import QUERY_ERROR, query_error

DB = "pgc_native_parquet_codecs"
CRAFTER = pathlib.Path(__file__).resolve().parents[1] / "craft_parquet_codecs.py"
COLS = "id int, grp int, val float8, s text"
GEN = ("SELECT g::int, (g % 50)::int, (g * 1.5)::float8, 'row-' || (g % 100) "
       "FROM generate_series(0, 19999) g")


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


def _num(dsn, sql):
    v = _value(dsn, sql)
    try:
        return int(v)
    except ValueError:
        return v if v.startswith(QUERY_ERROR) else query_error(f"not a number: {v}")


def _set_hash(dsn, query):
    """-> the shell's pgc_set_hash: md5 of the sorted row texts, EMPTY for no rows."""
    return _value(dsn, "SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), 'EMPTY') "
                       f"FROM (SELECT _row::text AS t FROM ( {query} ) _row) _s")


def _crashes(cluster):
    try:
        text = (cluster.datadir / "server.log").read_text(errors="replace")
    except OSError as exc:
        return query_error(f"server log unreadable: {exc}")
    return sum(1 for line in text.splitlines() if "terminated by signal" in line)


def _survived(cluster, dsn, before):
    """-> "1" when the server answers and no backend died since `before`, as the shell's."""
    up = _value(dsn, "SELECT 1")
    now = _crashes(cluster)
    if not (isinstance(before, int) and isinstance(now, int)):
        return "no (crash count unreadable)"
    if up == "1" and now == before:
        return "1"
    return f"no ([{up}], {now - before} backend crashes)"


def _verdict(dsn, sql):
    """-> CLEAN DECODE ERROR when `sql` is refused with the reader's own decode error,
    "no error" when it runs, or the error it raised instead."""
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        return "CLEAN DECODE ERROR" if "could not decode" in str(exc) else query_error(_message(exc))
    return "no error"


@pytest.fixture(scope="module")
def npc(pgc_cluster, server_dir):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        w = server_dir / "npc"
        w.mkdir(exist_ok=True)
        w.chmod(0o777)
        crafted = subprocess.run([sys.executable, str(CRAFTER), str(w)], capture_output=True, text=True)
        for f in w.iterdir():
            f.chmod(0o644)
        codecs = {}
        try:
            for line in (w / "codecs.txt").read_text().splitlines():
                k, _, v = line.partition(" ")
                codecs[k] = v
        except OSError as exc:
            codecs = {}
            crafted_err = f"codecs.txt unreadable: {exc}"
        else:
            crafted_err = ""
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            conn.execute("CREATE SERVER pq FOREIGN DATA WRAPPER pgcolumnar_parquet")
            for c in ("gzip", "zstd", "lz4"):
                conn.execute(f"CREATE FOREIGN TABLE fc_{c} ({COLS}) SERVER pq "
                             f"OPTIONS (path '{w}/c_{c}.parquet')")

        def rp(c):
            return _set_hash(dsn, f"SELECT * FROM pgcolumnar.read_parquet('{w}/c_{c}.parquet') AS t({COLS})")

        def codec(c):
            if c in codecs:
                return codecs[c]
            why = crafted_err or (crafted.stderr.strip().splitlines() or ["no codec recorded"])[-1]
            return f"unread ({why})"

        r = {f"codec {c}": codec(c) for c in ("none", "gzip", "zstd", "lz4")}
        r["gen"] = _set_hash(dsn, GEN)
        for c in ("none", "gzip", "zstd", "lz4"):
            r[f"rows {c}"] = rp(c)
        for c in ("gzip", "zstd", "lz4"):
            r[f"fdw {c}"] = _num(dsn, f"SELECT count(*) FROM fc_{c}")
        r["gzip sum"] = _num(dsn, f"SELECT sum(id) FROM pgcolumnar.read_parquet('{w}/c_gzip.parquet') AS t({COLS})")
        before = _crashes(pgc_cluster)
        r["hdr"] = _verdict(dsn, f"SELECT * FROM pgcolumnar.read_parquet('{w}/badhdr.parquet') AS t(a int)")
        r["hdr survived"] = _survived(pgc_cluster, dsn, before)
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_the_uncompressed_file_is_uncompressed(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; codec suite needs it to write compressed files") is None:
        return
    r = request.getfixturevalue("npc")  # after the decline: the crafter needs pyarrow
    expect.text(r["codec none"], "UNCOMPRESSED", "premise: the uncompressed file is UNCOMPRESSED")


def test_the_gzip_file_is_gzip_compressed(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; codec suite needs it to write compressed files") is None:
        return
    r = request.getfixturevalue("npc")  # after the decline: the crafter needs pyarrow
    expect.text(r["codec gzip"], "GZIP", "premise: the gzip file is GZIP-compressed")


def test_the_zstd_file_is_zstd_compressed(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; codec suite needs it to write compressed files") is None:
        return
    r = request.getfixturevalue("npc")  # after the decline: the crafter needs pyarrow
    expect.text(r["codec zstd"], "ZSTD", "premise: the zstd file is ZSTD-compressed")


def test_the_lz4_file_is_lz4_compressed(request, expect):
    # pyarrow names the LZ4_RAW codec "LZ4" in its metadata; the reader decodes only LZ4_RAW.
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; codec suite needs it to write compressed files") is None:
        return
    r = request.getfixturevalue("npc")  # after the decline: the crafter needs pyarrow
    expect.text(r["codec lz4"], "LZ4", "premise: the lz4 file is LZ4-compressed")


def test_uncompressed_decodes_to_the_generators_rows(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; codec suite needs it to write compressed files") is None:
        return
    r = request.getfixturevalue("npc")  # after the decline: the crafter needs pyarrow
    expect.text(r["rows none"], r["gen"], "uncompressed decodes to the generator's rows")


def test_gzip_decodes_to_the_generators_rows(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; codec suite needs it to write compressed files") is None:
        return
    r = request.getfixturevalue("npc")  # after the decline: the crafter needs pyarrow
    expect.text(r["rows gzip"], r["gen"], "gzip decodes to the generator's rows")


def test_zstd_decodes_to_the_generators_rows(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; codec suite needs it to write compressed files") is None:
        return
    r = request.getfixturevalue("npc")  # after the decline: the crafter needs pyarrow
    expect.text(r["rows zstd"], r["gen"], "zstd decodes to the generator's rows")


def test_lz4_decodes_to_the_generators_rows(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; codec suite needs it to write compressed files") is None:
        return
    r = request.getfixturevalue("npc")  # after the decline: the crafter needs pyarrow
    expect.text(r["rows lz4"], r["gen"], "lz4 decodes to the generator's rows")


def test_the_gzip_fdw_counts_every_row(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; codec suite needs it to write compressed files") is None:
        return
    r = request.getfixturevalue("npc")  # after the decline: the crafter needs pyarrow
    expect.num(r["fdw gzip"], 20000, "gzip FDW count")


def test_the_zstd_fdw_counts_every_row(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; codec suite needs it to write compressed files") is None:
        return
    r = request.getfixturevalue("npc")  # after the decline: the crafter needs pyarrow
    expect.num(r["fdw zstd"], 20000, "zstd FDW count")


def test_the_lz4_fdw_counts_every_row(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; codec suite needs it to write compressed files") is None:
        return
    r = request.getfixturevalue("npc")  # after the decline: the crafter needs pyarrow
    expect.num(r["fdw lz4"], 20000, "lz4 FDW count")


def test_the_gzip_sum_matches_the_generator(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; codec suite needs it to write compressed files") is None:
        return
    r = request.getfixturevalue("npc")  # after the decline: the crafter needs pyarrow
    expect.num(r["gzip sum"], 199990000, "gzip sum matches the generator")


def test_a_crafted_uncompressed_size_is_a_clean_decode_error(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; codec suite needs it to write compressed files") is None:
        return
    r = request.getfixturevalue("npc")  # after the decline: the crafter needs pyarrow
    expect.text(r["hdr"], "CLEAN DECODE ERROR", "crafted uncompressed size gives a clean decode error")


def test_the_backend_survives_the_crafted_header(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; codec suite needs it to write compressed files") is None:
        return
    r = request.getfixturevalue("npc")  # after the decline: the crafter needs pyarrow
    expect.text(r["hdr survived"], "1", "backend survived the crafted header")
