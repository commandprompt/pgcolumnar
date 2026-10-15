"""The object-store endpoint allow-list, empty by default (#393): the pytest twin of
`test/objstore_allowlist.sh` (#1341). Every check carries the shell's name.

`pgcolumnar.objstore_allowed_endpoints` gates every remote scheme: empty (the default)
refuses all remote access, so pg_read_server_files is not an SSRF primitive out of the
box, and link-local ranges (169.254.0.0/16, the cloud-metadata path) are refused even
when listed. The setting is SUSET, because a USERSET list would let any role widen its
own allow-list.

The list is changed the operator's way, ALTER SYSTEM and a reload, and reset in a
`finally`, because the cluster is shared with every other test. A superuser session SET
is exercised once, as the admin override.

THE FIXTURE SERVER IS THIS FILE'S OWN: a Range-capable http.server in a thread, serving
one Parquet file from `server_dir`. The shell twin's server is a script under `test/`,
and the two harnesses do not call each other.
"""
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class _Ranged(BaseHTTPRequestHandler):
    """HEAD and GET with an optional `Range: bytes=lo-hi`, from one directory."""
    root = None

    def log_message(self, *args):
        pass

    def _path(self):
        p = os.path.join(self.root, os.path.basename(self.path.split("?")[0]))
        return p if os.path.isfile(p) else None

    def do_HEAD(self):
        p = self._path()
        if p is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(os.path.getsize(p)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self):
        p = self._path()
        if p is None:
            self.send_error(404)
            return
        data = open(p, "rb").read()
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            lo, _, hi = rng[len("bytes="):].partition("-")
            lo = int(lo)
            hi = min(int(hi) if hi else len(data) - 1, len(data) - 1)
            body = data[lo:hi + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {lo}-{hi}/{len(data)}")
        else:
            body = data
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.wfile.write(body)


def _state(cluster, sql, role="postgres", pre=None):
    """-> (sqlstate or '', primary message or '', first value or None) on a fresh session."""
    import psycopg
    dsn = f"host=127.0.0.1 port={cluster.port} user={role} dbname=postgres"
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            if pre:
                c.execute(pre)
            row = c.execute(sql).fetchone()
            return "", "", (row[0] if row else None)
    except psycopg.Error as e:
        return e.sqlstate or "?", e.diag.message_primary or "", None


def test_the_endpoint_allow_list(pgc_cluster, pgc_conn, server_dir, expect):
    pa = expect.require_module("pyarrow", name="pyarrow is required to write the fixture")
    if pa is None:
        return
    pq = expect.require_module("pyarrow.parquet", name="pyarrow is required to write the fixture")
    pq.write_table(pa.table({"a": pa.array(range(1000), pa.int64())}), str(server_dir / "fixture.parquet"))

    _Ranged.root = str(server_dir)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Ranged)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def set_list(value):
        with pgc_conn.cursor() as cur:
            cur.execute(f"ALTER SYSTEM SET pgcolumnar.objstore_allowed_endpoints = '{value}'")
            cur.execute("SELECT pg_reload_conf()")
        time.sleep(0.5)

    url = f"http://127.0.0.1:{port}/fixture.parquet"
    q = f"SELECT count(*) FROM pgcolumnar.read_parquet('{url}') AS (a int8)"
    try:
        import urllib.request
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=5) as r:
            expect.num(r.status, 200, "premise: fixture server is up")

        # ---- the default: deny everything remote ----------------------------
        setting = _state(pgc_cluster, "SELECT current_setting('pgcolumnar.objstore_allowed_endpoints')")[2]
        expect.text("empty" if setting == "" else f"set: [{setting}]", "empty",
                    "the list defaults to empty")
        state, msg, _ = _state(pgc_cluster, q)
        expect.text(state, "42501", "default: an http read is refused 42501")
        expect.text("yes" if "objstore_allowed_endpoints" in msg else "no", "yes",
                    "default: the refusal names the GUC")
        local = f"SELECT count(*) FROM pgcolumnar.read_parquet('{server_dir / 'fixture.parquet'}') AS (a int8)"
        expect.num(_state(pgc_cluster, local)[2], 1000, "default: a local read is untouched by the list")

        # ---- the list admits exactly what it names ---------------------------
        set_list("127.0.0.1")
        expect.num(_state(pgc_cluster, q)[2], 1000, "listed host: the read works")
        set_list("127.0.0.2")
        expect.text(_state(pgc_cluster, q)[0], "42501", "unlisted host: refused 42501")
        set_list(f"127.0.0.1:{port}")
        expect.num(_state(pgc_cluster, q)[2], 1000, "host:port entry, right port: works")
        set_list("127.0.0.1:1")
        expect.text(_state(pgc_cluster, q)[0], "42501", "host:port entry, wrong port: refused 42501")
        set_list("other.example, 127.0.0.1")
        expect.num(_state(pgc_cluster, q)[2], 1000, "a multi-entry list matches any entry")

        # ---- SUSET: only a superuser may change it ---------------------------
        with pgc_conn.cursor() as cur:
            cur.execute("DO $$ BEGIN CREATE ROLE allow_nobody_py LOGIN; "
                        "EXCEPTION WHEN duplicate_object THEN NULL; END $$")
        expect.num(_state(pgc_cluster, "SELECT 1", role="allow_nobody_py")[2], 1,
                   "premise: the role can open a session")
        expect.text(_state(pgc_cluster, "SET pgcolumnar.objstore_allowed_endpoints = '10.0.0.1'",
                           role="allow_nobody_py")[0], "42501",
                    "a non-superuser cannot widen the list (42501)")
        # WITH THE CONFIGURED LIST EMPTY, so only the session's own SET can admit the
        # read (#1341): with 127.0.0.1 still configured, the read passed whatever the
        # SET did.
        set_list("")
        expect.num(_state(pgc_cluster, q, pre="SET pgcolumnar.objstore_allowed_endpoints = '127.0.0.1'")[2], 1000,
                   "a superuser session SET works (admin override)")

        # ---- link-local: refused even when listed ----------------------------
        # 42501 proves the refusal came BEFORE any connection: a real connect to
        # 169.254/16 would surface 08006 or hang toward a timeout.
        set_list("169.254.169.254, 127.0.0.1")
        ll = "SELECT count(*) FROM pgcolumnar.read_parquet('http://169.254.169.254:1/x.parquet') AS (a int8)"
        state, msg, _ = _state(pgc_cluster, ll)
        expect.text(state, "42501", "link-local: refused 42501 despite being listed")
        expect.num(1 if "link-local" in msg else 0, 1, "link-local: the refusal names the range, not the list")
        expect.num(_state(pgc_cluster, q)[2], 1000, "link-local: the listed ordinary host still works beside it")
    finally:
        with pgc_conn.cursor() as cur:
            cur.execute("ALTER SYSTEM RESET pgcolumnar.objstore_allowed_endpoints")
            cur.execute("SELECT pg_reload_conf()")
        srv.shutdown()
