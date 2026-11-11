"""A chunk size that wraps the response bound is refused, and the backend survives.

An HTTP/1.1 server may send a body in chunks, each led by its size in hex. The
object-store reader adds each size to what it has read so far and refuses a body
that would pass the buffer it is filling. Before this fix the sum was signed
64-bit addition: after a 1-byte chunk, the size 7fffffffffffffff wrapped the sum
negative, the bound passed, and the reader wrote the rest of the body past the
end of the buffer. A hostile server, or anyone who can change a plain http://
reply, killed the backend with SIGSEGV, which restarts the whole cluster.

Two readers had the bound:

- os_read_body, which fills a ranged GET into a fixed buffer
  (read_parquet, parquet_schema and every other remote file read);
- os_slurp_body, which reads a whole reply into a growing buffer under a cap
  (the Iceberg REST client and S3 LIST).

Each check below drives one of them from this file's own server on 127.0.0.1,
on a port chosen at run time, allowed by host and port in
pgcolumnar.objstore_allowed_endpoints. The last check is the control: a
legitimate chunked reply of the exact length still reads.
"""
import io
import re
import socket
import threading
import time

import psycopg
import pytest

# The hostile chunk size: INT64_MAX. After one byte it wraps a signed sum.
WRAP = b"7fffffffffffffff"
# Bytes sent after the hostile size: what the old code wrote past the buffer.
FLOOD = b"B" * 300_000


def _chunked(parts):
    """-> a chunked body carrying `parts` as its chunks, then the last chunk."""
    out = b"".join(b"%x\r\n%s\r\n" % (len(p), p) for p in parts)
    return out + b"0\r\n\r\n"


class _Server:
    """An HTTP server on 127.0.0.1 that serves one object, every reply chunked.

    HEAD answers 200 with the object's length and a strong ETag. A GET with a
    Range header answers 206 with the asked-for bytes; a GET without one answers
    200 with the whole object. `frame(data)` turns the bytes to send into the
    chunked body, so a check can send a hostile framing for any request.
    Every reply carries Connection: close.
    """

    def __init__(self, obj, frame):
        self.obj = obj
        self.frame = frame
        self.requests = []
        self._stop = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                self._serve(conn)
            except OSError:
                pass        # the reader hung up, which a refusal does
            finally:
                conn.close()

    def _serve(self, conn):
        conn.settimeout(5)
        head = b""
        while b"\r\n\r\n" not in head:
            got = conn.recv(4096)
            if not got:
                return
            head += got
        lines = head.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")
        method, path = lines[0].split()[:2]
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        self.requests.append((method, path, headers.get("range")))
        size = len(self.obj)
        if method == "HEAD":
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n"
                         b"ETag: \"e1\"\r\nConnection: close\r\n\r\n" % size)
            return
        m = re.fullmatch(r"bytes=(\d+)-(\d+)", headers.get("range", ""))
        if m:
            first, last = int(m.group(1)), min(int(m.group(2)), size - 1)
            status = b"206 Partial Content"
            extra = b"Content-Range: bytes %d-%d/%d\r\n" % (first, last, size)
            data = self.obj[first:last + 1]
        else:
            status, extra, data = b"200 OK", b"", self.obj
        conn.sendall(b"HTTP/1.1 " + status + b"\r\n" + extra +
                     b"ETag: \"e1\"\r\nTransfer-Encoding: chunked\r\n"
                     b"Connection: close\r\n\r\n")
        conn.sendall(self.frame(data))
        # Let the reader take everything before the socket closes, so a
        # close does not reset bytes it has not read yet.
        conn.shutdown(socket.SHUT_WR)
        try:
            while conn.recv(4096):
                pass
        except OSError:
            pass

    def close(self):
        self._stop.set()
        self.thread.join(timeout=10)
        self.sock.close()


def _await_up(dsn):
    """Wait, at most 60 s, for the server to answer. After a crash the postmaster
    restarts every backend, and the next check must not read crash recovery."""
    for _ in range(120):
        try:
            with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as conn:
                conn.execute("SELECT 1")
            return
        except psycopg.Error:
            time.sleep(0.5)


def _refused(pgc_cluster, server, sql, sqlstate, expect, what):
    """Run `sql` on one connection, assert it raises `sqlstate`, then assert the
    same connection still answers SELECT 1: the backend survived."""
    dsn = pgc_cluster.dsn()
    _await_up(dsn)
    conn = psycopg.connect(dsn, autocommit=True, connect_timeout=10)
    try:
        conn.execute("SET pgcolumnar.objstore_allowed_endpoints = "
                     f"'127.0.0.1:{server.port}'")
        with pytest.raises(psycopg.Error) as exc:
            conn.execute(sql).fetchall()
        expect.sqlstate(exc.value, sqlstate, f"{what}: an ordinary SQL error")
        try:
            alive = conn.execute("SELECT 1").fetchone()[0]
        except psycopg.Error:
            alive = 0
        expect.num(alive, 1, f"{what}: the same connection answers SELECT 1 after it")
    finally:
        conn.close()
        _await_up(dsn)


def _ranged(server):
    return (f"SELECT * FROM pgcolumnar.parquet_schema("
            f"'http://127.0.0.1:{server.port}/a.parquet')")


def test_a_chunk_size_that_wraps_after_a_first_chunk_is_refused_on_a_ranged_read(
        pgc_cluster, expect):
    # One byte, then INT64_MAX: total + size wraps a signed sum.
    server = _Server(b"\0" * 4096, lambda _data: b"1\r\nA\r\n" + WRAP + b"\r\n" + FLOOD)
    try:
        _refused(pgc_cluster, server, _ranged(server), "08P01", expect,
                 "a wrapping chunk size on a ranged GET")
    finally:
        server.close()


def test_a_chunk_size_past_64_bits_as_the_first_chunk_is_refused_on_a_ranged_read(
        pgc_cluster, expect):
    # No prior chunk, and a size that does not fit 64 bits at all.
    server = _Server(b"\0" * 4096,
                     lambda _data: b"1ffffffffffffffff\r\n" + FLOOD)
    try:
        _refused(pgc_cluster, server, _ranged(server), "08P01", expect,
                 "a first chunk size past 64 bits on a ranged GET")
    finally:
        server.close()


def test_a_chunk_size_that_wraps_the_cap_is_refused_on_a_rest_catalog_reply(
        pgc_cluster, expect):
    # The REST client reads GET /v1/config through os_slurp_body.
    server = _Server(b"{}", lambda _data: b"1\r\n{\r\n" + WRAP + b"\r\n" + FLOOD)
    sql = (f"SELECT pgcolumnar.iceberg_rest_table_location("
           f"'http://127.0.0.1:{server.port}/cat', 'db', 'events')")
    try:
        _refused(pgc_cluster, server, sql, "54000", expect,
                 "a wrapping chunk size on a REST catalog reply")
    finally:
        server.close()


def _pieces(data):
    """Split `data` into chunks of 1, 7, 1000, 1000, ... bytes, the last one short."""
    out, at = [], 0
    for size in (1, 7):
        out.append(data[at:at + size])
        at += size
    while at < len(data):
        out.append(data[at:at + 1000])
        at += 1000
    return [p for p in out if p]


def test_a_valid_chunked_reply_of_the_exact_length_still_reads(pgc_cluster, expect):
    pa = expect.require_module("pyarrow", name="pyarrow writes the control file")
    if pa is None:
        return
    pq = expect.require_module("pyarrow.parquet", name="pyarrow writes the control file")
    if pq is None:
        return
    n = 5000
    buf = io.BytesIO()
    pq.write_table(pa.table({"v": pa.array(range(n), pa.int64())}), buf,
                   row_group_size=1000, compression="NONE", use_dictionary=False)

    dsn = pgc_cluster.dsn()
    _await_up(dsn)
    server = _Server(buf.getvalue(), lambda data: _chunked(_pieces(data)))
    rest = _Server(b'{"metadata-location": "s3://b/t/metadata/1.json"}',
                   lambda data: _chunked(_pieces(data)))
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            conn.execute("SET pgcolumnar.objstore_allowed_endpoints = "
                         f"'127.0.0.1:{server.port},127.0.0.1:{rest.port}'")
            got = conn.execute(
                f"SELECT count(*), sum(v) FROM pgcolumnar.read_parquet("
                f"'http://127.0.0.1:{server.port}/a.parquet') AS t(v int8)").fetchone()
            loc = conn.execute(
                f"SELECT pgcolumnar.iceberg_rest_table_location("
                f"'http://127.0.0.1:{rest.port}/cat', 'db', 'events')").fetchone()[0]
    finally:
        server.close()
        rest.close()
    expect.num(got[0], n, "a chunked ranged read returns every row")
    expect.num(int(got[1]), n * (n - 1) // 2, "a chunked ranged read returns the right values")
    expect.at_least(sum(1 for r in server.requests if r[0] == "GET" and r[2]), 2,
                    "the rows came through ranged GETs, each answered chunked")
    expect.num(1 if loc == "s3://b/t/metadata/1.json" else 0, 1,
               "a chunked REST catalog reply is read whole")
