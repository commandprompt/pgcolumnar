"""A CR or LF in an object-store URL is refused before any request is sent: the pytest
twin of `test/objstore_crlf.sh` (#1341). Every check carries the shell's name.

A URL path is written into the request line, so a CR/LF in it would split the
request. The guard is in the single connect path, so both read_parquet and the REST
catalog client's http_request are covered. Each arm asserts the refusal names CR or LF,
and the read arm also asserts the listener received nothing carrying the injected line.

The listener is this file's own socket in a thread, as the shell twin's is a script
under `test/`; the two harnesses do not call each other.
"""
import socket
import threading

import pytest


class _Listener:
    """Accept at most one connection on 127.0.0.1 and keep whatever bytes it sends."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.sock.settimeout(5)
        self.port = self.sock.getsockname()[1]
        self.data = b""
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        conn.settimeout(2)
        try:
            while len(self.data) < 4096:
                chunk = conn.recv(1024)
                if not chunk:
                    break
                self.data += chunk
        except OSError:
            pass
        try:
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi")
        finally:
            conn.close()

    def close(self):
        self.thread.join(timeout=10)
        self.sock.close()


def _err(conn, sql):
    """-> the primary message of the error `sql` raises, or ''."""
    import psycopg
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            if cur.description:
                cur.fetchall()
        return ""
    except psycopg.Error as e:
        return e.diag.message_primary or ""


def _alive(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        return cur.fetchone()[0]


def test_a_cr_or_lf_in_a_url_is_refused_before_it_is_sent(pgc_conn, expect):
    with pgc_conn.cursor() as cur:
        cur.execute("SET pgcolumnar.objstore_allowed_endpoints = '127.0.0.1'")

    lst = _Listener()
    url = f"http://127.0.0.1:{lst.port}/x\r\nX-Injected: yes"
    msg = _err(pgc_conn, f"SELECT * FROM pgcolumnar.read_parquet('{url}') AS t(v int)")
    lst.close()
    expect.num(1 if ("CR or LF" in msg) else 0, 1,
               "a CR/LF in the URL path is rejected before the request is sent")
    expect.num(1 if b"X-Injected" in lst.data else 0, 0,
               "no injected line reached the listener (no request splitting)")
    expect.num(_alive(pgc_conn), 1, "backend alive")

    # The REST catalog client goes through the same connect path.
    cat = f"http://127.0.0.1:{lst.port}/x\r\nX-Injected: yes"
    with pgc_conn.cursor() as cur:
        cur.execute("DROP SERVER IF EXISTS crlf_cat_py CASCADE")
        cur.execute(f"CREATE SERVER crlf_cat_py FOREIGN DATA WRAPPER pgcolumnar_iceberg_catalog"
                    f" OPTIONS (catalog_uri '{cat}')")
        cur.execute("CREATE USER MAPPING FOR CURRENT_USER SERVER crlf_cat_py OPTIONS (token 'x')")
    msg = _err(pgc_conn, "SELECT pgcolumnar.iceberg_rest_table_location('crlf_cat_py', 'db', 'events')")
    expect.num(1 if ("CR or LF" in msg) else 0, 1,
               "a CR/LF in a REST catalog_uri is rejected too (objstore_http_request path)")
    expect.num(_alive(pgc_conn), 1, "backend alive after the REST attempt")
