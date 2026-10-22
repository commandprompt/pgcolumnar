"""The skip loops of an attacker-authored file are cancellable: the pytest twin of
`test/decode_skip_interrupts.sh`, every check by the shell's name (#1341).

PgColumnarThriftSkip walks every unrecognised field of a Parquet footer. Its list arm
loops a file-declared count up to 0xFFFFFFFF, and a bool element consumes zero bytes,
so a sub-2 KB footer holding 256 lists of 4.3e9 bools spins for hours unless the loop
yields. The functional arm writes that footer, as the shell's heredoc does, and wants a
1 s statement_timeout to cancel parquet_schema() over it:

- by the TIMEOUT: 57014 with the timeout's own message, so a guard that refused the
  list under the cancel code does not pass;
- within 3 s of the statement's start, so the cancel lands per skipped value: a check
  per struct field also ends in 57014, but only once a whole list has been walked
  (7 s here, inside the 30 s cap that decides a hang);
- with no backend killed since the scenario began, read from the server log: a fresh
  SELECT 1 alone answers once crash recovery is over, and passed with an abort() in
  the list arm.

av_skip's window is one array<null> block, capped at 50M zero-byte skips, too small for
a timeout to resolve, so it is held by shape, as the shell holds it: a check before the
switch, on the path every element and record field takes. So is PgColumnarThriftSkip's.
Both read the source with its comments blanked: with the call written as
/* CHECK_FOR_INTERRUPTS(); */ the shell's counts passed (#1341).

INDEPENDENT OF THE SHELL. The shell awks a perl-blanked file and times the cancel in a
DO block; this matches with `re` and times the statement from the client.
"""
import pathlib
import re
import struct
import threading
import time

import psycopg
import pytest

from pgc_vacuity import query_error

SRC = pathlib.Path(__file__).resolve().parents[2] / "src"
CRASH = "terminated by signal"
TIMEOUT_MS, BOUND_MS, CAP_S = 1000, 3000, 30


def _uleb(n):
    out = bytearray()
    while True:
        b, n = n & 0x7f, n >> 7
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def _skip_bomb(path):
    """PAR1 + FileMetaData + length + PAR1, the metadata one unrecognised struct field
    (0x1c) holding 256 list<bool> fields (0x19, then 0xf1: long-form size, element
    TC_BOOL_TRUE) of 0xFFFFFFFF elements each, then the struct's and the footer's STOP."""
    meta = b"\x1c" + (b"\x19\xf1" + _uleb(0xFFFFFFFF)) * 256 + b"\x00\x00"
    path.write_bytes(b"PAR1" + meta + struct.pack("<I", len(meta)) + b"PAR1")
    path.chmod(0o644)


def _crashes(cluster):
    """-> how many backends the server log says died, or a failed reading: a blind
    count must not read as "no crashes"."""
    try:
        return (cluster.datadir / "server.log").read_text(errors="replace").count(CRASH)
    except OSError as exc:
        return query_error(f"crash count unreadable: {exc}")


def _one(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as c:
            return c.execute(sql).fetchone()[0]
    except psycopg.Error as exc:
        return query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _probe(dsn, path):
    """-> ("SQLSTATE: message", ms from the statement's start to its error), ("NOERR",
    ms), a failed reading and None when the connection was lost (a crash), or ("HANG",
    None) when the statement had not returned within CAP_S."""
    out = {}

    def run():
        t0 = time.monotonic()
        try:
            with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as c:
                c.execute(f"SET statement_timeout = {TIMEOUT_MS}")
                t0 = time.monotonic()
                c.execute(f"SELECT count(*) FROM pgcolumnar.parquet_schema('{path}')").fetchall()
            out["r"] = ("NOERR", round((time.monotonic() - t0) * 1000))
        except psycopg.Error as exc:
            ms = round((time.monotonic() - t0) * 1000)
            if exc.sqlstate is None:
                # no error was raised: the connection was lost, so there is no time
                # of a cancel to read
                out["r"] = (query_error(str(exc).splitlines()[0] if str(exc) else type(exc).__name__), None)
            else:
                out["r"] = (f"{exc.sqlstate}: {exc.diag.message_primary}", ms)

    # a daemon thread: a backend that never yields cannot be cancelled or terminated,
    # so its client is abandoned, as the shell's KILL abandons psql
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(CAP_S)
    return out.get("r", ("HANG", None))


def _survived(dsn, cluster, before):
    """-> "yes" when the server answers and no backend died since `before`. Waits up to
    60 s for SELECT 1 first: the postmaster logs a death when it reaps the backend, so
    the count must follow the server's return."""
    for _ in range(60):
        if _one(dsn, "SELECT 1") == 1:
            break
        time.sleep(1)
    now = _crashes(cluster)
    if not (isinstance(before, int) and isinstance(now, int)):
        return "no (crash count unreadable)"
    return "yes" if _one(dsn, "SELECT 1") == 1 and now == before else f"no ({now - before} backend crashes)"


def _checks_before_switch(file, fn, switch):
    """-> how many CHECK_FOR_INTERRUPTS calls fn makes before `switch`, its comments
    blanked; a failed reading when the file, the function or the switch is not there."""
    try:
        text = (SRC / file).read_text()
    except OSError as exc:
        return query_error(f"{file} unreadable: {exc}")
    text = re.sub(r"/\*.*?\*/", lambda c: "\n" * c.group(0).count("\n"), text, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    m = re.search(r"^%s\(.*?^}" % re.escape(fn), text, re.M | re.S)
    if not m:
        return query_error(f"{fn} not found in {file}")
    cut = m.group(0).find(switch)
    if cut < 0:
        return query_error(f"{switch} not found in {fn}")
    return str(m.group(0)[:cut].count("CHECK_FOR_INTERRUPTS"))


@pytest.fixture(scope="module")
def dsi(pgc_cluster, server_dir):
    """The functional arm, run once for its three checks, each reading a value."""
    dsn = pgc_cluster.dsn()
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
    # server_dir, not tmp_path: the backend reads the file
    bomb = server_dir / "skip_bomb.parquet"
    _skip_bomb(bomb)
    before = _crashes(pgc_cluster)
    r = {"probe": _probe(dsn, bomb)}
    print(f"probe: {r['probe']}")
    r["alive"] = _survived(dsn, pgc_cluster, before)
    return r


def test_the_thrift_skip_bomb_is_cancelled_by_the_timeout(dsi, expect):
    expect.text(dsi["probe"][0],
                "57014: canceling statement due to statement timeout",
                "Thrift skip-bomb footer is cancellable, not an uninterruptible hang")


def test_the_cancel_lands_inside_the_skip_loop(dsi, expect):
    said, ms = dsi["probe"]
    expect.text("yes" if ms is not None and said != "NOERR" and ms < BOUND_MS else f"no ({said}, {ms} ms)", "yes",
                "the cancel lands within 3 s of the statement's start under a 1 s timeout")


def test_the_backend_survived_the_skip_bomb(dsi, expect):
    expect.text(dsi["alive"], "yes", "backend survived the Thrift skip-bomb footer")


def test_thrift_skip_checks_for_interrupts_before_its_switch(expect):
    expect.text(_checks_before_switch("columnar_thrift.c", "PgColumnarThriftSkip", "switch (ftype)"), "1",
                "PgColumnarThriftSkip checks for interrupts")


def test_av_skip_checks_for_interrupts_before_its_switch(expect):
    expect.text(_checks_before_switch("columnar_avro.c", "av_skip", "switch (s->kind)"), "1",
                "av_skip checks for interrupts per element (before the switch, not just per block)")
