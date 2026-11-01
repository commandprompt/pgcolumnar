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

        # ---- Alibaba Cloud instance metadata, outside 169.254/16 (#1848) -----
        # 100.100.100.200 sits in 100.64.0.0/10, which carrier-grade NAT uses, so
        # the guard refuses the one address, not the /10. statement_timeout 3s
        # bounds the read: without the guard a connect() is tried, and 08006 or
        # 57014 then shows that it was.
        set_list("100.100.100.200, 100.100.100.201, 127.0.0.1")
        ali = "SELECT count(*) FROM pgcolumnar.read_parquet('http://100.100.100.200:1/x.parquet') AS (a int8)"
        expect.text(_state(pgc_cluster, ali, pre="SET statement_timeout = '3s'")[0], "42501",
                    "Alibaba metadata 100.100.100.200: refused 42501 despite being listed")
        import psycopg
        try:
            with psycopg.connect(f"host=127.0.0.1 port={pgc_cluster.port} user=postgres dbname=postgres",
                                 autocommit=True) as c:
                c.execute("SET statement_timeout = '3s'")
                c.execute(ali)
            detail = "no error"
        except psycopg.Error as e:
            # The address when the DETAIL names it, else the whole error as the measurement.
            detail = e.diag.message_detail or ""
            detail = "100.100.100.200" if "100.100.100.200" in detail else f"{e.sqlstate}: {e}"
        expect.text(detail, "100.100.100.200", "Alibaba metadata: the refusal detail names 100.100.100.200")
        nxt = _state(pgc_cluster,
                     "SELECT count(*) FROM pgcolumnar.read_parquet('http://100.100.100.201:1/x.parquet') AS (a int8)",
                     pre="SET statement_timeout = '3s'")[0]
        nxt = "connect tried" if nxt in ("08006", "57014") else (nxt or "no error")
        expect.text(nxt, "connect tried",
                    "100.100.100.201 (next to Alibaba metadata): not refused, a connect is tried")
        expect.num(_state(pgc_cluster, q)[2], 1000, "Alibaba metadata: the listed ordinary host still works beside it")
    finally:
        with pgc_conn.cursor() as cur:
            cur.execute("ALTER SYSTEM RESET pgcolumnar.objstore_allowed_endpoints")
            cur.execute("SELECT pg_reload_conf()")
        srv.shutdown()


# ---- metadata addresses beyond 169.254/16 and fe80::/10 (#1808) -------------------
# AWS serves IPv6 instance metadata at fd00:ec2::254, and a NAT64 path reaches
# 169.254.169.254 as 64:ff9b::a9fe:a9fe. A URL cannot carry an IPv6 literal, so a host
# NAME must resolve to these addresses. A small getaddrinfo() shim, loaded into a
# private server with LD_PRELOAD, maps names under pgc-shim.invalid to fixed addresses
# and passes every other name to the real resolver. No DNS, no /etc/hosts edit. The
# shell twin compiles its own copy of the same shim.
_SHIM_C = r'''
#define _GNU_SOURCE
#include <dlfcn.h>
#include <netdb.h>
#include <stdlib.h>
#include <string.h>
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>

static const char *const map[][2] = {
	{"ok", "127.0.0.1"},
	{"imds6", "fd00:ec2::254"},
	{"nat64", "64:ff9b::a9fe:a9fe"},
	{"nat64local", "64:ff9b:1::a9fe:a9fe"},
	{"mapped", "::ffff:169.254.169.254"},
	{"compat", "::169.254.169.254"},
	{"gcp6", "fd20:ce::254"},
	{"local48", "64:ff9b:1:a9fe:a9:fe00::"},
	{"local56", "64:ff9b:1:a9:fe:a9fe::"},
	{"local64", "64:ff9b:1:0:a9:fea9:fe00::"},
	{"over48", "64:ff9b:1:aa9:fe:500::"},
	{"ctl96", "64:ff9b:1::a00:5"},
	{"loop6", "::ffff:127.0.0.1"},
	{"ula", "fd00:ec3::254"},
	{"n64ok", "64:ff9b::a9ff:a9fe"},
	{"n64wide", "64:ff9b::1:a9fe:a9fe"},
	{"local2", "64:ff9b:2::a9fe:a9fe"},
	{"compat9", "::1:0:a9fe:a9fe"},
	{"l48only", "64:ff9b:1:a9fe:0:100::"},
	{"gce253", "fd20:ce::253"},
	{"n64sec", "64:ff9b::aafe:a9fe"},
	{"alimapped", "::ffff:100.100.100.200"},
	{"alicompat", "::100.100.100.200"},
	{"alinat64", "64:ff9b::6464:64c8"},
	{"alilocal", "64:ff9b:1::6464:64c8"},
	{"alilocal48", "64:ff9b:1:6464:64:c800::"},
	{"ali56", "64:ff9b:1:64:64:64c8::"},
	{"ali64", "64:ff9b:1:0:64:6464:c800::"},
	{"alinext", "64:ff9b::6464:64c9"},
};

int
getaddrinfo(const char *node, const char *service,
			const struct addrinfo *hints, struct addrinfo **res)
{
	static const char sfx[] = ".pgc-shim.invalid";
	size_t		n = node ? strlen(node) : 0;

	if (n > sizeof(sfx) - 1 && strcmp(node + n - (sizeof(sfx) - 1), sfx) == 0)
	{
		size_t		len = n - (sizeof(sfx) - 1);

		for (size_t i = 0; i < sizeof(map) / sizeof(map[0]); i++)
		{
			int			v6 = strchr(map[i][1], ':') != NULL;
			size_t		salen = v6 ? sizeof(struct sockaddr_in6) : sizeof(struct sockaddr_in);
			struct addrinfo *ai;
			int			port = service ? atoi(service) : 0;

			if (strlen(map[i][0]) != len || strncmp(node, map[i][0], len) != 0)
				continue;
			ai = calloc(1, sizeof(*ai) + salen);
			if (ai == NULL)
				return EAI_MEMORY;
			ai->ai_family = v6 ? AF_INET6 : AF_INET;
			ai->ai_socktype = SOCK_STREAM;
			ai->ai_protocol = IPPROTO_TCP;
			ai->ai_addrlen = salen;
			ai->ai_addr = (struct sockaddr *) (ai + 1);
			if (v6)
			{
				struct sockaddr_in6 *s6 = (struct sockaddr_in6 *) ai->ai_addr;

				s6->sin6_family = AF_INET6;
				s6->sin6_port = htons(port);
				inet_pton(AF_INET6, map[i][1], &s6->sin6_addr);
			}
			else
			{
				struct sockaddr_in *s4 = (struct sockaddr_in *) ai->ai_addr;

				s4->sin_family = AF_INET;
				s4->sin_port = htons(port);
				inet_pton(AF_INET, map[i][1], &s4->sin_addr);
			}
			*res = ai;
			return 0;
		}
		return EAI_NONAME;
	}
	return ((int (*) (const char *, const char *, const struct addrinfo *,
					  struct addrinfo **)) dlsym(RTLD_NEXT, "getaddrinfo"))
		(node, service, hints, res);
}
'''


def test_metadata_addresses_behind_ipv6_forms(pgc_cluster, server_dir, expect, monkeypatch):
    import pathlib
    import shutil
    import subprocess
    import tempfile
    import psycopg
    from pgc_cluster import Cluster, pick_port

    pa = expect.require_module("pyarrow", name="pyarrow is required to write the fixture")
    if pa is None:
        return
    pq = expect.require_module("pyarrow.parquet", name="pyarrow is required to write the fixture")
    pq.write_table(pa.table({"a": pa.array(range(1000), pa.int64())}), str(server_dir / "fixture.parquet"))
    _Ranged.root = str(server_dir)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Ranged)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    work = pathlib.Path(tempfile.mkdtemp(prefix="pgc-gaishim-"))
    work.chmod(0o777)
    datadir = work / "data"
    datadir.mkdir()
    cluster = Cluster(pgc_cluster.pg_config, "gaishim", datadir, pick_port(40))
    try:
        (work / "gai_shim.c").write_text(_SHIM_C)
        cc = subprocess.run(["cc", "-shared", "-fPIC", "-o", str(work / "gai_shim.so"),
                             str(work / "gai_shim.c"), "-ldl"], capture_output=True, text=True)
        expect.text(cc.stderr.strip() or "ok", "ok", "premise: the resolver shim compiles")
        (work / "gai_shim.so").chmod(0o755)
        cluster.initdb()
        monkeypatch.setenv("LD_PRELOAD", str(work / "gai_shim.so"))
        cluster.start()
        monkeypatch.delenv("LD_PRELOAD")
        expect.text(_state(cluster, "SELECT 'up'")[2], "up",
                    "premise: the server restarts with the resolver shim")
        with psycopg.connect(cluster.dsn(), autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            c.execute("ALTER SYSTEM SET pgcolumnar.objstore_allowed_endpoints = "
                      "'ok.pgc-shim.invalid, imds6.pgc-shim.invalid, nat64.pgc-shim.invalid, "
                      "nat64local.pgc-shim.invalid, mapped.pgc-shim.invalid, compat.pgc-shim.invalid, "
                      "gcp6.pgc-shim.invalid, local48.pgc-shim.invalid, local56.pgc-shim.invalid, "
                      "local64.pgc-shim.invalid, over48.pgc-shim.invalid, ctl96.pgc-shim.invalid, "
                      "loop6.pgc-shim.invalid, ula.pgc-shim.invalid, n64ok.pgc-shim.invalid, "
                      "n64wide.pgc-shim.invalid, local2.pgc-shim.invalid, compat9.pgc-shim.invalid, "
                      "l48only.pgc-shim.invalid, gce253.pgc-shim.invalid, n64sec.pgc-shim.invalid, "
                      "alimapped.pgc-shim.invalid, alicompat.pgc-shim.invalid, alinat64.pgc-shim.invalid, "
                      "alilocal.pgc-shim.invalid, alilocal48.pgc-shim.invalid, ali56.pgc-shim.invalid, "
                      "ali64.pgc-shim.invalid, alinext.pgc-shim.invalid'")
            c.execute("SELECT pg_reload_conf()")
        time.sleep(0.5)

        def shim_read(name):
            # statement_timeout 3s: without the guard a connect() here can hang,
            # and 57014 then shows that it was tried.
            return _state(cluster, f"SELECT count(*) FROM pgcolumnar.read_parquet("
                                   f"'http://{name}.pgc-shim.invalid:1/x.parquet') AS (a int8)",
                          pre="SET statement_timeout = '3s'")

        ok = (f"SELECT count(*) FROM pgcolumnar.read_parquet("
              f"'http://ok.pgc-shim.invalid:{port}/fixture.parquet') AS (a int8)")
        expect.num(_state(cluster, ok)[2], 1000,
                   "premise: a shim name that maps to 127.0.0.1 reads the fixture")
        expect.text(shim_read("imds6")[0], "42501",
                    "IPv6 IMDS fd00:ec2::254: refused 42501 despite being listed")
        expect.text(shim_read("nat64")[0], "42501",
                    "NAT64 64:ff9b::a9fe:a9fe: refused 42501 despite being listed")
        expect.text(shim_read("nat64local")[0], "42501",
                    "NAT64 local-use 64:ff9b:1::a9fe:a9fe: refused 42501 despite being listed")
        expect.text(shim_read("mapped")[0], "42501",
                    "IPv4-mapped ::ffff:169.254.169.254: refused 42501 despite being listed")
        expect.text(shim_read("compat")[0], "42501",
                    "IPv4-compatible ::169.254.169.254: refused 42501 despite being listed")
        expect.text(shim_read("gcp6")[0], "42501",
                    "GCP IPv6 metadata fd20:ce::254: refused 42501 despite being listed")
        # The local-use prefix 64:ff9b:1::/48 may be cut at /48, /56, /64 or /96 (RFC 6052),
        # so 169.254.169.254 can sit at four byte positions. One check per position.
        expect.text(shim_read("local48")[0], "42501",
                    "NAT64 local-use /48 64:ff9b:1:a9fe:a9:fe00::: refused 42501 despite being listed")
        # 169.254.169.254 repeats itself, so the /48 address above also matches the
        # /64 position. 169.254.0.1 at /48 matches the /48 position only.
        expect.text(shim_read("l48only")[0], "42501",
                    "NAT64 local-use /48 only 64:ff9b:1:a9fe:0:100:: (169.254.0.1): refused 42501 "
                    "despite being listed")
        expect.text(shim_read("local56")[0], "42501",
                    "NAT64 local-use /56 64:ff9b:1:a9:fe:a9fe::: refused 42501 despite being listed")
        expect.text(shim_read("local64")[0], "42501",
                    "NAT64 local-use /64 64:ff9b:1:0:a9:fea9:fe00::: refused 42501 despite being listed")
        # The guard cannot know the operator's prefix length, so it checks all four
        # positions at once and fails closed: 10.169.254.5 at /48 puts 169 and 254 where
        # the /56 layout looks. This over-refusal is documented in best-practices.md.
        expect.text(shim_read("over48")[0], "42501",
                    "NAT64 local-use /48 10.169.254.5 (64:ff9b:1:aa9:fe:500::): refused 42501, "
                    "documented over-refusal")
        # Control: an ordinary address under the local-use prefix is not refused; a
        # connect is tried (08006 unreachable, or 57014 at the 3s timeout).
        ctl = shim_read("ctl96")[0]
        ctl = "connect tried" if ctl in ("08006", "57014") else (ctl or "no error")
        expect.text(ctl, "connect tried",
                    "NAT64 local-use /96 10.0.0.5 (64:ff9b:1::a00:5): not refused, a connect is tried")
        # Near misses: each address sits just outside a refused range, so it is not
        # refused and the read reaches connect(). Without these, a guard that refused
        # too much (every IPv6 address, all of fc00::/7, 169.0.0.0/8, 64:ff9b::/32)
        # would pass every check above. The first reads the fixture over IPv6; the rest
        # prove a connect was tried (08006 or 57014). Anything else prints.
        def connect_tried(name):
            st = shim_read(name)[0]
            return "connect tried" if st in ("08006", "57014") else (st or "no error")

        loop6 = (f"SELECT count(*) FROM pgcolumnar.read_parquet("
                 f"'http://loop6.pgc-shim.invalid:{port}/fixture.parquet') AS (a int8)")
        st, _, val = _state(cluster, loop6)
        # A failed read prints its SQLSTATE, so the failure shows what refused it.
        expect.num(val if val is not None else f"error {st}", 1000,
                   "IPv4-mapped loopback ::ffff:127.0.0.1: not refused, the read works")
        expect.text(connect_tried("ula"), "connect tried",
                    "ULA fd00:ec3::254 (outside fd00:ec2::/32): not refused, a connect is tried")
        expect.text(connect_tried("n64ok"), "connect tried",
                    "NAT64 64:ff9b::a9ff:a9fe (169.255.169.254): not refused, a connect is tried")
        expect.text(connect_tried("n64wide"), "connect tried",
                    "64:ff9b::1:a9fe:a9fe (outside 64:ff9b::/96): not refused, a connect is tried")
        expect.text(connect_tried("local2"), "connect tried",
                    "64:ff9b:2::a9fe:a9fe (outside 64:ff9b:1::/48): not refused, a connect is tried")
        expect.text(connect_tried("compat9"), "connect tried",
                    "::1:0:a9fe:a9fe (outside ::/96): not refused, a connect is tried")
        expect.text(connect_tried("gce253"), "connect tried",
                    "fd20:ce::253 (next to fd20:ce::254): not refused, a connect is tried")
        expect.text(connect_tried("n64sec"), "connect tried",
                    "NAT64 64:ff9b::aafe:a9fe (170.254.169.254): not refused, a connect is tried")
        # 100.100.100.200 inside the same IPv6 forms (#1848). On an IPv6-only network
        # with DNS64, a name whose A record is 100.100.100.200 resolves only to
        # 64:ff9b::6464:64c8, so each form needs its own refusal. The /48 local-use
        # layout puts the address at bytes 6, 7, 9 and 10, /56 at 7, 9, 10 and 11, and
        # /64 at 9 to 12. 100.100.100.201 under the well-known prefix is the control: a
        # connect is tried.
        expect.text(shim_read("alimapped")[0], "42501",
                    "IPv4-mapped ::ffff:100.100.100.200: refused 42501 despite being listed")
        expect.text(shim_read("alicompat")[0], "42501",
                    "IPv4-compatible ::100.100.100.200: refused 42501 despite being listed")
        expect.text(shim_read("alinat64")[0], "42501",
                    "NAT64 64:ff9b::6464:64c8 (100.100.100.200): refused 42501 despite being listed")
        expect.text(shim_read("alilocal")[0], "42501",
                    "NAT64 local-use 64:ff9b:1::6464:64c8 (100.100.100.200): refused 42501 despite being listed")
        expect.text(shim_read("alilocal48")[0], "42501",
                    "NAT64 local-use /48 64:ff9b:1:6464:64:c800:: (100.100.100.200): refused 42501 "
                    "despite being listed")
        expect.text(shim_read("ali56")[0], "42501",
                    "NAT64 local-use /56 64:ff9b:1:64:64:64c8:: (100.100.100.200): refused 42501 "
                    "despite being listed")
        expect.text(shim_read("ali64")[0], "42501",
                    "NAT64 local-use /64 64:ff9b:1:0:64:6464:c800:: (100.100.100.200): refused 42501 "
                    "despite being listed")
        expect.text(connect_tried("alinext"), "connect tried",
                    "NAT64 64:ff9b::6464:64c9 (100.100.100.201): not refused, a connect is tried")
        # The range when the DETAIL names it, else the whole error as the measurement.
        try:
            with psycopg.connect(cluster.dsn(), autocommit=True) as c:
                c.execute("SET statement_timeout = '3s'")
                c.execute("SELECT count(*) FROM pgcolumnar.read_parquet("
                          "'http://imds6.pgc-shim.invalid:1/x.parquet') AS (a int8)")
            detail = "no error"
        except psycopg.Error as e:
            d = e.diag.message_detail or ""
            detail = "fd00:ec2::/32" if "fd00:ec2::/32" in d else (e.diag.message_primary or "?")
        expect.text(detail, "fd00:ec2::/32", "IPv6 IMDS: the refusal detail names fd00:ec2::/32")
    finally:
        cluster.stop()
        shutil.rmtree(work, ignore_errors=True)
        srv.shutdown()
