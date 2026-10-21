"""pgColumnar's local file opens are race-free: no stat-before-open TOCTOU (#644/#686).
The pytest twin of `test/local_open_race_free.sh`, every check by the shell's name
(#1341).

The race itself cannot be tested deterministically, so, as in the shell, the guard is
the source's shape. PgColumnarOpenLocalRegularFile and the parallel-copy partition
coordinator must open with O_NONBLOCK in the open call itself, and must stat nothing
before opening (fstat of the held fd is the one stat allowed). The racy
PgColumnarRejectNonRegularFile helper must be gone from a tree the sweep demonstrably
read. Functionally, read_parquet and read_avro_manifest on a FIFO must fail with the
opener's own refusal -- XX001 and "is not a regular file" -- rather than hang, and the
backend must still answer.

A body that was not read is a failed reading, never a count of 0. The FIFO lives in
`server_dir`, because the backend opens it. A query still running after 5s is a HANG:
the FIFO is then released by opening its write end non-blocking, so the backend's
open(2) returns and the fixture can finish.
"""
import os
import pathlib
import re
import threading

import psycopg
import pytest

from pgc_vacuity import query_error

DB = "pgc_local_open_race_free"
SRC = pathlib.Path(__file__).resolve().parents[2] / "src"
OPEN_RE = re.compile(r"(OpenTransientFile|[^a-z_]open)\([^)]*O_NONBLOCK")
STAT_RE = re.compile(r"(^|[^a-z_])l?stat\(")
REFUSAL = re.compile(r'^columnar: ".*" is not a regular file$')


def _body(path, name):
    """The function NAME in PATH, from its definition line to the next function at
    column zero: the shell's awk, line for line."""
    try:
        lines = pathlib.Path(path).read_text(errors="replace").splitlines()
    except OSError:
        return ""
    out, on = [], False
    for i, line in enumerate(lines, 1):
        if line.startswith(f"{name}("):
            on = True
        elif on and i > 1 and re.match(r"^[A-Za-z_].*\(", line):
            break
        if on:
            out.append(line)
    return "\n".join(out)


def _count_in(body, pattern):
    if not body:
        return query_error("no body read")
    return sum(1 for line in body.splitlines() if pattern.search(line))


def _sweep(needle):
    """-> (files read under src, lines naming NEEDLE), grep -rc's two readings."""
    files = lines = 0
    for p in SRC.rglob("*"):
        if not p.is_file():
            continue
        try:
            data = p.read_bytes()
        except OSError:
            continue
        files += 1
        lines += sum(1 for line in data.split(b"\n") if needle in line)
    return files, lines


def _fifo_refusal(dsn, sql, fifo):
    """-> "<sqlstate> not a regular file" for the opener's refusal, the bare SQLSTATE
    for any other error, or HANG when the statement is still running after 5s."""
    result = {}

    def run():
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute(sql)
            result["v"] = "no error"
        except psycopg.Error as exc:
            msg = exc.diag.message_primary or ""
            result["v"] = (f"{exc.sqlstate} not a regular file" if REFUSAL.match(msg)
                           else exc.sqlstate or query_error(str(exc).splitlines()[0]))

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(5)
    if t.is_alive():
        try:
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        except OSError:
            pass
        t.join(10)
        return "HANG"
    return result.get("v", query_error("the statement left no outcome"))


@pytest.fixture(scope="module")
def lo(pgc_cluster, server_dir):
    obj, pc = SRC / "columnar_objstore.c", SRC / "columnar_parallel_copy.c"
    opener = _body(obj, "PgColumnarOpenLocalRegularFile")
    coord = _body(pc, "pcopy_partition_aligned_offsets")
    files, helper = _sweep(b"PgColumnarRejectNonRegularFile")
    r = {"opener": opener, "opener open": _count_in(opener, OPEN_RE), "opener fstat": _count_in(opener, re.compile(r"fstat\(")),
         "opener stat": _count_in(opener, STAT_RE), "files": files, "helper": helper,
         "coord open": _count_in(coord, OPEN_RE), "coord stat": _count_in(coord, STAT_RE)}
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        fifo = server_dir / "lo.fifo"
        os.mkfifo(fifo)
        os.chmod(fifo, 0o666)
        r["parquet"] = _fifo_refusal(dsn, f"SELECT * FROM pgcolumnar.read_parquet('{fifo}') AS t(c int)", fifo)
        r["avro"] = _fifo_refusal(dsn, f"SELECT count(*) FROM pgcolumnar.read_avro_manifest('{fifo}')", fifo)
        try:
            with psycopg.connect(dsn, autocommit=True) as c:
                r["up"] = c.execute("SELECT 1").fetchone()[0]
        except psycopg.Error as exc:
            r["up"] = query_error(str(exc).splitlines()[0])
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def _some(n):
    return ("yes" if n >= 1 else "no") if isinstance(n, int) else n


def test_the_race_free_opener_exists(lo, expect):
    expect.text("yes" if lo["opener"] else "no", "yes", "the race-free opener exists")


def test_the_opener_opens_o_nonblock(lo, expect):
    expect.text(_some(lo["opener open"]), "yes", "the opener opens O_NONBLOCK (a FIFO cannot block the open)")


def test_the_opener_fstats_the_fd_it_holds(lo, expect):
    expect.text(_some(lo["opener fstat"]), "yes", "the opener fstats the fd it holds (checks what it opened)")


def test_the_opener_never_stats_a_path_before_opening_it(lo, expect):
    expect.text(str(lo["opener stat"]), "0", "the opener never stats a path before opening it (no TOCTOU)")


def test_the_recursive_sweep_read_source_files(lo, expect):
    n = lo["files"]
    expect.text("yes" if n >= 5 else f"read [{n}] files", "yes",
                "premise: the recursive sweep read source files under src/")


def test_the_racy_stat_before_open_helper_is_gone(lo, expect):
    expect.text(str(lo["helper"]), "0", "the racy stat-before-open helper is gone")


def test_the_partition_coordinator_opens_o_nonblock(lo, expect):
    expect.text(_some(lo["coord open"]), "yes",
                "the partition coordinator opens O_NONBLOCK (a FIFO cannot block its open)")


def test_the_partition_coordinator_never_stats_a_path_before_opening_it(lo, expect):
    expect.text(str(lo["coord stat"]), "0",
                "the partition coordinator never stats a path before opening it (no TOCTOU)")


def test_read_parquet_on_a_fifo_is_a_clean_error(lo, expect):
    expect.text(str(lo["parquet"]), "XX001 not a regular file", "read_parquet on a FIFO is a clean error, not a hang")


def test_read_avro_manifest_on_a_fifo_is_a_clean_error(lo, expect):
    expect.text(str(lo["avro"]), "XX001 not a regular file",
                "read_avro_manifest on a FIFO is a clean error, not a hang")


def test_backend_still_up_after_the_fifo_refusals(lo, expect):
    expect.text(str(lo["up"]), "1", "backend still up after the FIFO refusals")
