"""The HTTP ranged byte source (#393 M1): the pytest twin of
`test/objstore_http_read.sh`, every check by the shell's name (#1341).

Reads go through the shell's own fixture server (test/objstore_http_server.py), which
logs one line per request: `METHOD path query range`. That log is the seam.

A  a foreign table over http://127.0.0.1 returns what the byte-identical local file
   returns.
B  the request count, ASSERTED: buffered == 4 + G * width (1 HEAD and 3 ranged GETs to
   open, one GET per needed chunk), unbuffered >= 10x that, and every GET carried a
   Range header.
C  A REFUSAL IS NOT ANY ERROR. Many guards share 08006 and 08P01, so each refusal must
   carry its own guard's SQLSTATE AND message: an unreachable port, a 404, a server
   that ignores Range, a short 206 body, EOF mid-body, and a fresh connection the
   server hangs up on (asked once, never retried). A keep-alive connection the server
   dropped is the one failure that is retried, and the read equals the local one. An
   object overwritten between two ranged GETs is refused, and a weak ETag is never
   sent as If-Match (#1805).
D  statement_timeout cancels a transfer the server wedged mid-body, within 10 s.
E  only an s3:// key is a pattern (#1785): a query string on an http URL reaches the
   server, and a bracketed IPv6 host is refused by name.
Survival: the server answers and the server log's "terminated by signal" count has
not moved -- waited for first, since a crashed server comes back.

Every statement runs on its own connection, as the shell's psql calls do, so each
read opens the object afresh and the request counts are the shell's.

The parquet fixture is written by a python3 child, as the shell's heredoc writes it.
"""
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

import psycopg
import pytest

from pgc_vacuity import query_error

TEST = pathlib.Path(__file__).resolve().parents[1]
G, NCOLS, ROWS_PER_GROUP = 3, 8, 8000
COLDEFS = ", ".join(f"c{i} int8" for i in range(NCOLS))
SUM_ALL = " + ".join(f"sum(c{i})" for i in range(NCOLS))
K_FULL, K_PROJ = 4 + G * NCOLS, 4 + G * 2
ALLOW = "SET pgcolumnar.objstore_allowed_endpoints = '127.0.0.1'"
SCHEMA = "pgc_objstore_http_read"

WRITER = """
import sys
import pyarrow as pa, pyarrow.parquet as pq
G, NCOLS, RPG = %d, %d, %d
n = G * RPG
cols = {("c%%d" %% i): pa.array([(i + 1) * v for v in range(n)], pa.int64())
        for i in range(NCOLS)}
pq.write_table(pa.table(cols), sys.argv[1], row_group_size=RPG, data_page_size=1024,
               compression="NONE", use_dictionary=False)
assert pq.ParquetFile(sys.argv[1]).metadata.num_row_groups == G
""" % (G, NCOLS, ROWS_PER_GROUP)

# The overwrite pair (#1805), as the shell writes it: one layout and byte length,
# c0 >= 0 in every row of the first and c0 < 0 in every row of the second.
SWAP_WRITER = """
import os, sys
import pyarrow as pa, pyarrow.parquet as pq
G, RPG = %d, %d
for first in sys.argv[1:]:
    for path, sign in ((first, 1), (first + ".next", -1)):
        c0 = pa.array([sign * v - (sign < 0) for v in range(G * RPG)], pa.int64())
        pq.write_table(pa.table({"c0": c0}), path, row_group_size=RPG,
                       compression="NONE", use_dictionary=False, write_statistics=False)
    assert os.path.getsize(first) == os.path.getsize(first + ".next")
""" % (G, ROWS_PER_GROUP)
SWAP_SQL = ("SELECT 'old=' || count(*) FILTER (WHERE c0 >= 0) || ' new=' || "
            "count(*) FILTER (WHERE c0 < 0) FROM fswap")
SWAP79_SQL = SWAP_SQL.replace("fswap", "fswap79")

# The length-change pairs (#1867), as the shell writes them: .next has one more row
# group, so it is longer, and its first G groups sit at the first file's offsets.
LENSWAP_WRITER = """
import os, sys
import pyarrow as pa, pyarrow.parquet as pq
G, RPG = %d, %d
for first in sys.argv[1:]:
    for path, sign, groups in ((first, 1, G), (first + ".next", -1, G + 1)):
        c0 = pa.array([sign * v - (sign < 0) for v in range(groups * RPG)], pa.int64())
        pq.write_table(pa.table({"c0": c0}), path, row_group_size=RPG,
                       compression="NONE", use_dictionary=False, write_statistics=False)
""" % (G, ROWS_PER_GROUP)
# The shrink pair (#1888 review): the .next file holds one row, so it is shorter
# than the offset of every chunk after the swap, and those GETs get 416.
SHRINK_WRITER = """
import sys
import pyarrow as pa, pyarrow.parquet as pq
G, RPG = %d, %d
first = sys.argv[1]
for path, rows in ((first, list(range(G * RPG))), (first + ".next", [-1])):
    pq.write_table(pa.table({"c0": pa.array(rows, pa.int64())}), path, row_group_size=RPG,
                   compression="NONE", use_dictionary=False, write_statistics=False)
""" % (G, ROWS_PER_GROUP)
NOPIN = "SET pgcolumnar.objstore_pin_reads = off"
HASH_SQL = ("SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), 'EMPTY') FROM "
            "(SELECT _r::text AS t FROM (SELECT c3, c7 FROM %s) _r) _s")


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _connect(dsn):
    conn = psycopg.connect(dsn, autocommit=True, connect_timeout=10)
    conn.execute(ALLOW)
    conn.execute(f"SET search_path = {SCHEMA}, public")
    return conn


def _value(dsn, sql):
    """-> the first row's columns joined by a space, or a query_error()."""
    try:
        with _connect(dsn) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    if row is None or any(v is None for v in row):
        return query_error(f"no value from {sql}")
    return " ".join(str(v) for v in row)


def _set_hash(dsn, sql):
    """The shell's pgc_set_hash: md5 of the rows' text, sorted; EMPTY for no rows."""
    return _value(dsn, "SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), $e$EMPTY$e$) "
                       f"FROM (SELECT _row::text AS t FROM ( {sql} ) _row) _s")


def _await_server(dsn):
    """The shell's await_server: wait up to 30 s for SELECT 1 -> what it last returned.
    A crash restarts the server, so every reading after one waits for it: otherwise
    the next reading fails on recovery instead of measuring its own guard."""
    up = None
    for _ in range(60):
        up = _value(dsn, "SELECT 1")
        if up == "1":
            break
        time.sleep(0.5)
    return up


def _refusal(dsn, sql, phrase):
    """The shell's refusal_of: -> "SQLSTATE: phrase" when `sql` failed with `phrase` in
    its primary message, else the SQLSTATE and the message it did give, or what came
    back instead of an error."""
    _await_server(dsn)
    try:
        with _connect(dsn) as conn:
            rows = conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        if not exc.sqlstate:
            return query_error(_message(exc))
        primary = exc.diag.message_primary or ""
        return f"{exc.sqlstate}: " + (phrase if phrase in primary else primary)
    out = " ".join(" ".join(str(v) for v in row) for row in rows)
    return f"no error: {out[:200]}"


def _nopin(dsn, sql, phrase):
    """`sql` in a session with pgcolumnar.objstore_pin_reads off, read as the shell's
    refusal_of reads "$NOPIN; sql": a failed SET is the reading too."""
    _await_server(dsn)
    try:
        with _connect(dsn) as conn:
            conn.execute(NOPIN)
            rows = conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        if not exc.sqlstate:
            return query_error(_message(exc))
        primary = exc.diag.message_primary or ""
        return f"{exc.sqlstate}: " + (phrase if phrase in primary else primary)
    out = " ".join(" ".join(str(v) for v in row) for row in rows)
    return f"no error: {out[:200]}"


def _requests(log):
    """-> the request log's lines split into fields, or a query_error()."""
    try:
        return [l.split() for l in pathlib.Path(log).read_text(errors="replace").splitlines()]
    except OSError as exc:
        return query_error(f"request log unreadable: {exc}")


def _count(log, keep=lambda f: True):
    lines = _requests(log)
    return lines if not isinstance(lines, list) else sum(1 for f in lines if keep(f))


def _crashes(log):
    try:
        return pathlib.Path(log).read_text(errors="replace").count("terminated by signal")
    except OSError as exc:
        return query_error(f"crash count unreadable: {exc}")


def _write_fixture(path):
    """-> None, or the decline when pyarrow is absent. Any other failure raises."""
    w = subprocess.run([sys.executable, "-c", WRITER, str(path)], capture_output=True, text=True)
    if w.returncode == 0:
        return None
    if "No module named 'pyarrow" in w.stderr:
        return ("MISSING_DEPENDENCY", "pyarrow is not installed")
    raise RuntimeError(f"writing {path} failed: {w.stderr.strip()}")


@pytest.fixture(scope="module")
def ohr(pgc_cluster):
    from pgc_cluster import pick_port

    work = pathlib.Path(tempfile.mkdtemp(prefix="pgc-ohr-"))
    work.chmod(0o755)
    fixture = work / "fixture.parquet"
    wrote = _write_fixture(fixture)
    if wrote is not None:
        shutil.rmtree(work, ignore_errors=True)
        yield {"absent": wrote}
        return
    fixture.chmod(0o644)
    swap, swap79 = work / "swap.parquet", work / "swap79.parquet"
    swapoff, vswap = work / "swapoff.parquet", work / "vswap.parquet"
    vswapq = work / "vswapq.parquet"
    subprocess.run([sys.executable, "-c", SWAP_WRITER, str(swap), str(swap79), str(swapoff), str(vswap),
                    str(vswapq)],
                   check=True, capture_output=True, text=True)
    swap_sizes = (swap.with_name("swap.parquet.next").stat().st_size, swap.stat().st_size)
    lenswap, lenswapoff = work / "lenswap.parquet", work / "lenswapoff.parquet"
    subprocess.run([sys.executable, "-c", LENSWAP_WRITER, str(lenswap), str(lenswapoff)],
                   check=True, capture_output=True, text=True)
    lenswap_grew = (lenswap.with_name("lenswap.parquet.next").stat().st_size
                    - lenswap.stat().st_size > 0)
    lenshrink = work / "lenshrink.parquet"
    subprocess.run([sys.executable, "-c", SHRINK_WRITER, str(lenshrink)],
                   check=True, capture_output=True, text=True)
    shrank = (lenshrink.stat().st_size
              - lenshrink.with_name("lenshrink.parquet.next").stat().st_size > 60000)
    for f in (swap, swap79, swapoff, vswap, vswapq, lenswap, lenswapoff, lenshrink):
        f.chmod(0o644)
        f.with_name(f.name + ".next").chmod(0o644)

    port = pick_port(31)
    out, hlog = work / "http_server.out", str(work / "http.log")
    srv = subprocess.Popen([sys.executable, str(TEST / "objstore_http_server.py"), "--dir", str(work),
                            "--port", str(port), "--log", hlog],
                           stdout=open(out, "w"), stderr=subprocess.STDOUT)
    for _ in range(50):
        if out.is_file() and "READY" in out.read_text(errors="replace"):
            break
        time.sleep(0.1)

    dsn, plog = pgc_cluster.dsn(), pgc_cluster.datadir / "server.log"
    base = f"http://127.0.0.1:{port}"
    r = {"absent": None, "fixture": "yes" if fixture.stat().st_size > 0 else "no",
         "server up": out.read_text(errors="replace").count("READY") if out.is_file() else 0,
         "swap sizes": swap_sizes, "lenswap grew": "1" if lenswap_grew else "0",
         "shrank": "1" if shrank else "0"}

    def logged(sql):
        """Truncate the request log, run `sql`, -> how many requests it logged."""
        pathlib.Path(hlog).write_text("")
        _value(dsn, sql)
        return _count(hlog)

    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
            c.execute("DROP SERVER IF EXISTS ohr_srv CASCADE")
            c.execute(f"CREATE SCHEMA {SCHEMA}")
            c.execute("CREATE SERVER ohr_srv FOREIGN DATA WRAPPER pgcolumnar_parquet")
            dead = pick_port(33)
            for name, path in (("flocal", str(fixture)), ("fhttp", f"{base}/fixture.parquet"),
                               ("fdead", f"http://127.0.0.1:{dead}/fixture.parquet"),
                               ("f404", f"{base}/no_such.parquet"),
                               ("fnorange", f"{base}/norange/fixture.parquet"),
                               ("fshort", f"{base}/short/fixture.parquet"),
                               ("ftrunc", f"{base}/trunc/fixture.parquet"),
                               ("fdropka", f"{base}/dropka/fixture.parquet"),
                               ("fhangup", f"{base}/hangup/fixture.parquet"),
                               ("fstall", f"{base}/stall/fixture.parquet"),
                               ("fweak", f"{base}/weak/fixture.parquet"),
                               ("fetag79", f"{base}/etag79/fixture.parquet"),
                               ("flongetag", f"{base}/longetag/fixture.parquet"),
                               ("ffickle", f"{base}/fickle/fixture.parquet"),
                               ("fvswapoff", f"{base}/vswap/fixture.parquet"),
                               ("fvnull", f"{base}/vnull/fixture.parquet"),
                               ("fvlong", f"{base}/vlong/fixture.parquet"),
                               ("fvdeny", f"{base}/vdeny/fixture.parquet")):
                c.execute(f"CREATE FOREIGN TABLE {SCHEMA}.{name} ({COLDEFS}) SERVER ohr_srv "
                          f"OPTIONS (path '{path}')")
            c.execute(f"CREATE FOREIGN TABLE {SCHEMA}.fswap (c0 int8) SERVER ohr_srv "
                      f"OPTIONS (path '{base}/swap/swap.parquet')")
            c.execute(f"CREATE FOREIGN TABLE {SCHEMA}.fswap79 (c0 int8) SERVER ohr_srv "
                      f"OPTIONS (path '{base}/swap79/swap79.parquet')")
            for name, path in (("fswapoff", "swap/swapoff.parquet"), ("fvswap", "vswap/vswap.parquet"),
                               ("flenswap", "lenswap/lenswap.parquet"),
                               ("flenswapoff", "lenswap/lenswapoff.parquet"),
                               ("flenshrink", "lenswap/lenshrink.parquet")):
                c.execute(f"CREATE FOREIGN TABLE {SCHEMA}.{name} (c0 int8) SERVER ohr_srv "
                          f"OPTIONS (path '{base}/{path}')")
        r["crashes0"] = _crashes(plog)

        r["A full"] = (_set_hash(dsn, "SELECT * FROM fhttp"), _set_hash(dsn, "SELECT * FROM flocal"))
        r["A proj"] = (_set_hash(dsn, "SELECT c1, c6 FROM fhttp"),
                       _set_hash(dsn, "SELECT c1, c6 FROM flocal"))
        r["A count"] = _value(dsn, "SELECT count(*) FROM fhttp")

        r["full buf"] = logged(f"SELECT {SUM_ALL} FROM fhttp")
        r["proj buf"] = logged("SELECT sum(c1) + sum(c6) FROM fhttp")
        pathlib.Path(hlog).write_text("")
        try:
            with _connect(dsn) as conn:
                conn.execute("SET pgcolumnar.objstore_buffered = off")
                conn.execute(f"SELECT {SUM_ALL} FROM fhttp").fetchall()
        except psycopg.Error:
            pass                     # the count below is the reading, as in the shell
        r["full unbuf"] = _count(hlog)

        pathlib.Path(hlog).write_text("")
        _value(dsn, "SELECT sum(c0) FROM fhttp")
        r["gets"] = _count(hlog, lambda f: f[0] == "GET")
        r["norange"] = _count(hlog, lambda f: f[0] == "GET" and len(f) > 3 and f[3] == "-")

        r["C dead"] = _refusal(dsn, "SELECT count(*) FROM fdead", "could not connect to")
        r["C 404"] = _refusal(dsn, "SELECT count(*) FROM f404", "does not exist (HTTP 404)")
        r["C norange"] = _refusal(dsn, "SELECT count(*) FROM fnorange", "ignored a Range request")
        r["C short"] = _refusal(dsn, "SELECT count(*) FROM fshort", "requested bytes")
        r["C trunc"] = _refusal(dsn, "SELECT count(*) FROM ftrunc", "closed mid-body")
        _await_server(dsn)
        r["C dropka"] = (_set_hash(dsn, "SELECT c2, c5 FROM fdropka"),
                         _set_hash(dsn, "SELECT c2, c5 FROM flocal"))
        pathlib.Path(hlog).write_text("")
        r["C hangup"] = _refusal(dsn, "SELECT count(*) FROM fhangup", "failed before a response")
        r["C hangup asked"] = _count(hlog)
        _await_server(dsn)
        r["C swap"] = _refusal(dsn, SWAP_SQL, "changed while it was being read")
        _await_server(dsn)
        r["C weak"] = (_set_hash(dsn, "SELECT c3, c7 FROM fweak"),
                       _set_hash(dsn, "SELECT c3, c7 FROM flocal"))
        r["C swap79"] = _refusal(dsn, SWAP79_SQL, "changed while it was being read")
        _await_server(dsn)
        r["C etag79"] = (_set_hash(dsn, "SELECT c3, c7 FROM fetag79"),
                         _set_hash(dsn, "SELECT c3, c7 FROM flocal"))
        r["C longetag"] = _refusal(dsn, "SELECT 'rows=' || count(*) FROM flongetag",
                                   "changed while it was being read")
        r["C fickle"] = _refusal(dsn, "SELECT 'rows=' || count(*) FROM ffickle",
                                 "changed while it was being read")

        # #1866: the pin turned off, in the session that reads
        changed = "changed while it was being read"
        r["C nopin fickle"] = (
            _nopin(dsn, "SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), 'EMPTY') FROM "
                        "(SELECT _r::text AS t FROM (SELECT c3, c7 FROM ffickle) _r) _s", changed),
            "no error: " + str(_set_hash(dsn, "SELECT c3, c7 FROM flocal")))
        r["C nopin swap"] = _nopin(dsn, SWAP_SQL.replace("fswap", "fswapoff"), changed)
        # #1867: a versioned read, then a length change shown by Content-Range
        _await_server(dsn)
        pathlib.Path(hlog).write_text("")
        r["C vswap"] = _refusal(dsn, SWAP_SQL.replace("fswap", "fvswap"), changed)
        lines = _requests(hlog)
        if isinstance(lines, list):
            vgets = [f for f in lines if len(f) > 2 and f[0] == "GET" and "vswap" in f[1]]
            r["C vswap gets"] = len(vgets)
            r["C vswap no id"] = sum(1 for f in vgets if not f[2].startswith("versionId=3HL4%2Bkq%2F"))
        else:
            r["C vswap gets"] = r["C vswap no id"] = lines
        _await_server(dsn)
        r["C lenswap"] = _refusal(dsn, SWAP_SQL.replace("fswap", "flenswap"), changed)
        _await_server(dsn)
        r["C nopin lenswap"] = _nopin(dsn, SWAP_SQL.replace("fswap", "flenswapoff"), changed)
        # #1888 review: a shrink below a later GET's offset gets 416
        _await_server(dsn)
        r["C lenshrink"] = _refusal(dsn, "SELECT 'sum=' || sum(c0) FROM flenshrink", changed)

        def vnamed(part):
            lines = _requests(hlog)
            if not isinstance(lines, list):
                return lines
            return sum(1 for f in lines if len(f) > 2 and f[0] == "GET" and part in f[1]
                       and re.search(r"(^|&)versionId=", f[2]))

        local_c3c7 = _set_hash(dsn, "SELECT c3, c7 FROM flocal")
        for key, table, part, reader, phrase in (
                ("C nopin vswap", "fvswapoff", "/vswap/fixture", _nopin, changed),
                ("C vnull", "fvnull", "/vnull/fixture", _refusal, changed),
                ("C vlong", "fvlong", "/vlong/fixture", _refusal, changed),
                ("C vdeny", "fvdeny", "/vdeny/fixture", _refusal, "AccessDenied")):
            _await_server(dsn)
            pathlib.Path(hlog).write_text("")
            got = reader(dsn, HASH_SQL % table, phrase)
            r[key] = (f"{got}; GETs naming a version: {vnamed(part)}", local_c3c7)

        _await_server(dsn)
        t0 = time.monotonic()
        try:
            with _connect(dsn) as conn:
                conn.execute("SET statement_timeout = '2s'")
                conn.execute("SELECT count(*) FROM fstall").fetchall()
            r["D state"] = "no error"
        except psycopg.Error as exc:
            r["D state"] = exc.sqlstate or query_error(_message(exc))
        r["D elapsed"] = time.monotonic() - t0

        # E (#1785): only an s3:// key is a pattern
        pathlib.Path(hlog).write_text("")
        r["E query"] = _value(dsn, f"SELECT count(*) FROM pgcolumnar.read_parquet("
                                   f"'{base}/fixture.parquet?raw=true') AS t({COLDEFS})")
        r["E headq"] = _count(hlog, lambda f: f[:3] == ["HEAD", "/fixture.parquet", "raw=true"])
        r["E reqs"] = _count(hlog)
        # #1904: the version pin joins the URL's own query with '&'
        _await_server(dsn)
        pathlib.Path(hlog).write_text("")
        got = _refusal(dsn, "SELECT 'old=' || count(*) FILTER (WHERE c0 >= 0) || ' new=' || "
                            "count(*) FILTER (WHERE c0 < 0) FROM pgcolumnar.read_parquet("
                            f"'{base}/vswap/vswapq.parquet?raw=true') AS t(c0 int8)", changed)
        lines = _requests(hlog)
        if isinstance(lines, list):
            qgets = [f for f in lines if len(f) > 2 and f[:2] == ["GET", "/vswap/vswapq.parquet"]]
            nots = [f[2] for f in qgets if not f[2].startswith("raw=true&versionId=3HL4%2Bkq%2F")]
            bad = f"{len(nots)} (first: {nots[0]})" if nots else 0
            r["E vquery"] = (f"{got}; GETs without raw=true&versionId=: {bad} of {len(qgets)}", len(qgets))
        else:
            r["E vquery"] = (f"{got}; GETs unreadable: {lines}", lines)
        r["E ipv6"] = _refusal(dsn, f"SELECT count(*) FROM pgcolumnar.read_parquet("
                                    f"'http://[::1]:{port}/fixture.parquet') AS t({COLDEFS})",
                               "IPv6 literal hosts are not supported")

        r["up"] = _await_server(dsn)
        r["crashes1"] = _crashes(plog)
        yield r
    finally:
        try:
            with psycopg.connect(dsn, autocommit=True) as c:
                c.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
                c.execute("DROP SERVER IF EXISTS ohr_srv CASCADE")
        except psycopg.Error:
            pass
        srv.kill()
        srv.wait()
        shutil.rmtree(work, ignore_errors=True)


def _declined(ohr, expect):
    if ohr["absent"] is None:
        return False
    expect.cannot_run(*ohr["absent"])
    return True


def _positive(n):
    return "yes" if isinstance(n, int) and n > 0 else f"no ({n})"


def test_the_http_read_fixture_is_written(ohr, expect):
    """The shell skips the whole suite without pyarrow, under this name."""
    if expect.require_module("pyarrow", name="pyarrow is required to write the fixture") is None:
        return
    if _declined(ohr, expect):
        return
    expect.text(ohr["fixture"], "yes", "premise: fixture written")


def test_the_http_read_fixture_server_is_up(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.num(ohr["server up"], 1, "premise: fixture server is up")


def test_a_full_scan_over_http_equals_the_local_file(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.hash(*ohr["A full"], "A: full scan http == local")


def test_a_projection_over_http_equals_the_local_file(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.hash(*ohr["A proj"], "A: projection http == local")


def test_count_over_http(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.text(ohr["A count"], str(G * ROWS_PER_GROUP), "A: count(*) over http")


def test_the_buffered_run_logged_requests(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.text(_positive(ohr["full buf"]), "yes", "premise: buffered run logged requests")


def test_the_unbuffered_run_logged_requests(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.text(_positive(ohr["full unbuf"]), "yes", "premise: unbuffered run logged requests")


def test_buffered_full_width_requests(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.num(ohr["full buf"], K_FULL, "B: buffered full-width requests (K = 4 + G*NCOLS)")


def test_buffered_two_column_requests(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.num(ohr["proj buf"], K_PROJ, "B: buffered 2-col requests (K = 4 + G*2)")


def test_unbuffered_is_at_least_ten_times_k(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.at_least(ohr["full unbuf"], 10 * K_FULL, "B: unbuffered >= 10x K")


def test_the_buffered_to_unbuffered_request_ratio(ohr, expect):
    """The shell's check_ratio: a / b rounded to two places, within 0.1x; a zero side
    measured nothing."""
    if _declined(ohr, expect):
        return
    a, b = ohr["full buf"], ohr["full unbuf"]
    if not isinstance(a, int) or not isinstance(b, int) or a == 0 or b == 0:
        got = f"nothing measured: a=[{a}] b=[{b}]"
    else:
        ratio = float(f"{a / b:.2f}")
        got = "within the 0.1x bound" if ratio <= 0.1 else f"{ratio}x (a={a} b={b})"
    expect.text(got, "within the 0.1x bound", "B: buffered/unbuffered request ratio")


def test_the_range_audit_phase_logged_gets(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.text(_positive(ohr["gets"]), "yes", "premise: the range-audit phase logged GETs")


def test_every_get_carried_a_range_header(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.num(ohr["norange"], 0, "B: every GET carried a Range header")


def test_an_unreachable_endpoint_is_refused(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.text(ohr["C dead"], "08006: could not connect to", "C: unreachable endpoint SQLSTATE")


def test_a_404_is_refused(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.text(ohr["C 404"], "58P01: does not exist (HTTP 404)", "C: http 404 SQLSTATE")


def test_a_range_ignoring_server_is_refused(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.text(ohr["C norange"], "08P01: ignored a Range request", "C: range-ignoring server SQLSTATE")


def test_a_short_206_body_is_refused(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.text(ohr["C short"], "08P01: requested bytes", "C: a short 206 body is refused")


def test_eof_mid_body_is_refused(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.text(ohr["C trunc"], "08006: closed mid-body", "C: EOF mid-body is refused")


def test_a_dropped_keep_alive_connection_is_retried(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.hash(*ohr["C dropka"], "C: a dropped keep-alive connection is retried (http == local)")


def test_a_server_that_hangs_up_is_refused(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.text(ohr["C hangup"], "08006: failed before a response",
                "C: a server that hangs up on a fresh connection is refused")


def test_a_fresh_connection_is_not_retried(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.num(ohr["C hangup asked"], 1, "C: and that fresh connection is not retried (one request logged)")


def test_the_overwrite_pair_has_one_byte_length(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.text(*(str(n) for n in ohr["swap sizes"]), "premise: the overwrite pair has one byte length")


def test_an_object_overwritten_mid_read_is_refused(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.text(ohr["C swap"], "55000: changed while it was being read",
                "C: an object overwritten mid-read is refused")


def test_a_weak_etag_is_not_sent_as_if_match(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.hash(*ohr["C weak"], "C: a weak ETag is not sent as If-Match (http == local)")


def test_a_79_character_etag_still_pins_the_read(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.text(ohr["C swap79"], "55000: changed while it was being read",
                "C: a 79-character ETag still pins the read")


def test_a_79_character_etag_reads_an_unchanged_object_whole(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.hash(*ohr["C etag79"],
                "C: a 79-character ETag reads an unchanged object whole (http == local)")


def test_an_80_character_etag_is_not_sent_cut_short(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.text(ohr["C longetag"], f"no error: rows={G * ROWS_PER_GROUP}",
                "C: an 80-character ETag is not sent cut short as If-Match")


def test_a_server_whose_etag_changes_on_every_request_is_refused(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.text(ohr["C fickle"], "55000: changed while it was being read",
                "C: a server whose ETag changes on every request is refused")


def test_the_length_change_pair_differs_in_length(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.text(ohr["lenswap grew"], "1", "premise: the length-change pair differs in length")


def test_with_the_pin_off_a_fickle_etag_server_is_read_whole(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.text(*ohr["C nopin fickle"],
                "C: with pgcolumnar.objstore_pin_reads off, a server whose ETag changes on every "
                "request is read whole (http == local)")


def test_with_the_pin_off_an_overwrite_mid_read_is_read_unpinned(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.text(ohr["C nopin swap"], f"no error: old={ROWS_PER_GROUP} new={(G - 1) * ROWS_PER_GROUP}",
                "C: with pgcolumnar.objstore_pin_reads off, an object overwritten mid-read is read unpinned")


def test_a_versioned_object_overwritten_mid_read_is_read_from_its_version(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.text(ohr["C vswap"], f"no error: old={G * ROWS_PER_GROUP} new=0",
                "C: a versioned object overwritten mid-read is read whole from the version the read opened")


def test_the_versioned_read_logged_gets(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.text(_positive(ohr["C vswap gets"]), "yes", "premise: the versioned read logged GETs")


def test_every_ranged_get_of_a_versioned_read_names_the_version(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.num(ohr["C vswap no id"], 0, "C: every ranged GET of a versioned read names the version")


def test_an_object_whose_length_changes_mid_read_is_refused(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.text(ohr["C lenswap"], "55000: changed while it was being read",
                "C: an object whose length changes mid-read is refused (Content-Range total)")


def test_with_the_pin_off_a_length_change_is_still_refused(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.text(ohr["C nopin lenswap"], "55000: changed while it was being read",
                "C: with pgcolumnar.objstore_pin_reads off, an object whose length changes mid-read "
                "is still refused")


def test_the_shrink_pairs_new_file_is_shorter_than_its_old_one(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.text(ohr["shrank"], "1",
                "premise: the shrink pair's new file is shorter than its old one (bytes old-new)")


def test_an_object_that_shrinks_below_a_later_gets_offset_is_refused(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.text(ohr["C lenshrink"], "55000: changed while it was being read",
                "C: an object that shrinks mid-read below a later GET's offset is refused (HTTP 416)")


def _versioned(ohr, key, named):
    got, local = ohr[key]
    return got, f"no error: {local}; GETs naming a version: {named}"


def test_with_the_pin_off_no_get_names_the_version(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.text(*_versioned(ohr, "C nopin vswap", 0),
                "C: with pgcolumnar.objstore_pin_reads off, no ranged GET of a versioned object "
                "names the version")


def test_a_version_id_of_null_is_not_sent_as_a_pin(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.text(*_versioned(ohr, "C vnull", 0), "C: a version id of null is not sent as a pin")


def test_a_version_id_longer_than_1024_bytes_is_not_sent_cut_short(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.text(*_versioned(ohr, "C vlong", 0),
                "C: a version id longer than 1024 bytes is not sent cut short")


def test_a_versioned_get_answered_403_is_sent_again_without_the_version(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.text(*_versioned(ohr, "C vdeny", 1),
                "C: a versioned GET answered 403 is sent again without the version, and the read "
                "finishes")


def test_a_stalled_transfer_is_cancelled(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.text(ohr["D state"], "57014", "D: stalled transfer cancelled with query_canceled")


def test_the_http_read_cancel_latency_is_bounded(ohr, expect):
    if _declined(ohr, expect):
        return
    s = ohr["D elapsed"]
    expect.text("within 10s" if s <= 10 else f"{s:.1f}s", "within 10s", "D: cancel latency bounded (<=10s)")


def test_an_http_url_with_a_query_string_reads_the_object(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.text(ohr["E query"], str(G * ROWS_PER_GROUP),
                "E: an http URL with a query string (?raw=true) reads the object, not a listing")


def test_the_query_string_reached_the_server(ohr, expect):
    if _declined(ohr, expect):
        return
    n = ohr["E headq"]
    got = "carried" if isinstance(n, int) and n >= 1 else \
        f"not carried ({n} HEADs of /fixture.parquet?raw=true in {ohr['E reqs']} requests)"
    expect.text(got, "carried", "E: and its HEAD carried the query string to the server")


def test_a_versioned_read_of_an_http_url_with_a_query_string_adds_the_version_with_an_ampersand(ohr, expect):
    if _declined(ohr, expect):
        return
    got, n = ohr["E vquery"]
    expect.text(got, f"no error: old={G * ROWS_PER_GROUP} new=0; GETs without raw=true&versionId=: 0 of {n}",
                "E: a versioned read of an http URL with a query string adds the version with '&' and "
                "reads the version it opened")


def test_an_ipv6_literal_http_host_is_refused(ohr, expect):
    if _declined(ohr, expect):
        return
    expect.text(ohr["E ipv6"], "0A000: IPv6 literal hosts are not supported",
                "E: an IPv6 literal http host is refused as IPv6, not as a listing")


def test_the_server_survives(ohr, expect):
    if _declined(ohr, expect):
        return
    c0, c1, up = ohr["crashes0"], ohr["crashes1"], ohr["up"]
    if not isinstance(c0, int) or not isinstance(c1, int):
        got = "no (crash count unreadable)"
    elif up == "1" and c1 == c0:
        got = "1"
    else:
        got = f"no ([{up}], {c1 - c0} backend crashes)"
    expect.text(got, "1", "survival: the server answers and no backend crashed")
