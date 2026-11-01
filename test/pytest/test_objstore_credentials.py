"""Object-store credentials: placement, per-user resolution, half a credential, no leak,
and ambient credentials as a privilege (#393 M4): the pytest twin of
`test/objstore_credentials.sh` (#1341). Every check carries the shell's name.

Non-secret config (endpoint, region) lives on the SERVER and secrets only on a USER
MAPPING, refused HV00D anywhere else at DDL time. Ambient (postmaster environment)
credentials are a privilege: a superuser, or a mapping a superuser marked
credentials_required 'false'.

THE FIXTURE SERVER IS THIS FILE'S OWN: an http.server in a thread that serves one
Parquet file from a bucket directory and verifies every request's AWS Signature
Version 4 itself, from the AWS spec with the standard library, so a green read proves
the C signer and an independent implementation agree on the canonical request. A
request it cannot verify gets 403. It records every request, so "refused before any
request" is measured, not inferred.

A PRIVATE CLUSTER, because phase 2 restarts the postmaster with AWS_* in its
environment, which the shared cluster cannot do. Phase 1 starts it with every AWS_*
variable of this process REMOVED, so no ambient credential can make a phase-1 read
pass. The roles are this cluster's own.
"""
import datetime
import hashlib
import hmac
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

AKID, SECRET, BADSECRET = "PGCTESTKEYID", "pgctest-good-secret", "pgctest-wrong-secret"
REGION, BUCKET = "pgc-test-1", "pgc-bucket"
G, NCOLS, RPG = 3, 4, 6000
AUTH = re.compile(r"^AWS4-HMAC-SHA256 Credential=([^/]+)/(\d{8})/([^/]+)/s3/aws4_request,\s*"
                  r"SignedHeaders=([^,]+),\s*Signature=([0-9a-f]{64})$")
RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _uri_encode(s):
    return urllib.parse.quote(s, safe="-_.~")


def _canonical_query(raw):
    """Each name and value decoded, then strictly re-encoded, sorted by name then value."""
    if not raw:
        return ""
    pairs = []
    for part in raw.split("&"):
        k, _, v = part.partition("=")
        pairs.append((_uri_encode(urllib.parse.unquote_plus(k)), _uri_encode(urllib.parse.unquote_plus(v))))
    return "&".join(f"{k}={v}" for k, v in sorted(pairs))


def _signature(secret, method, raw_path, raw_query, headers, signed, amzdate, datestamp, region, payload):
    canon = "".join(f"{h}:{' '.join((headers.get(h) or '').split())}\n" for h in signed)
    creq = "\n".join([method, raw_path, _canonical_query(raw_query), canon, ";".join(signed), payload])
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amzdate, f"{datestamp}/{region}/s3/aws4_request",
                         hashlib.sha256(creq.encode()).hexdigest()])
    key = ("AWS4" + secret).encode()
    for part in (datestamp, region, "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    return hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()


class _SigV4(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _verified(self):
        self.server.requests.append((self.command, self.path))
        m = AUTH.match(self.headers.get("Authorization", ""))
        ok = False
        if m and m.group(1) == AKID and m.group(3) == REGION:
            raw_path, _, raw_query = self.path.partition("?")
            want = _signature(SECRET, self.command, raw_path, raw_query, self.headers,
                              m.group(4).split(";"), self.headers.get("x-amz-date", ""), m.group(2),
                              REGION, self.headers.get("x-amz-content-sha256", ""))
            ok = hmac.compare_digest(want, m.group(5))
        if not ok:
            body = b'<?xml version="1.0"?><Error><Code>SignatureDoesNotMatch</Code></Error>'
            self.send_response(403)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        return ok

    def _file(self):
        return pathlib.Path(self.server.root) / urllib.parse.unquote(self.path.partition("?")[0]).lstrip("/")

    def do_HEAD(self):
        if not self._verified():
            return
        f = self._file()
        if not f.is_file():
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(f.stat().st_size))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self):
        if not self._verified():
            return
        f = self._file()
        if not f.is_file():
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = f.read_bytes()
        m = RANGE.match((self.headers.get("Range") or "").replace(" ", ""))
        if not m:
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if m.group(1) == "":
            lo, hi = max(0, len(data) - int(m.group(2))), len(data) - 1
        else:
            lo = int(m.group(1))
            hi = min(int(m.group(2)) if m.group(2) else len(data) - 1, len(data) - 1)
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {lo}-{hi}/{len(data)}")
        self.send_header("Content-Length", str(hi - lo + 1))
        self.end_headers()
        self.wfile.write(data[lo:hi + 1])


def _pg_ctl(cluster, args, extra_env):
    """pg_ctl with an explicit environment: every AWS_* of this process removed, then
    `extra_env` added. Dropped to postgres when running as root, as the harness does."""
    unset = [a for k in os.environ if k.startswith("AWS_") for a in ("-u", k)]
    cmd = ["env"] + unset + [f"{k}={v}" for k, v in extra_env.items()] + \
          [os.path.join(cluster.bindir, "pg_ctl")] + args
    if os.geteuid() == 0:
        cmd = ["runuser", "-u", "postgres", "--"] + cmd
    return subprocess.run(cmd, capture_output=True, text=True)


def test_object_store_credentials(pgc_cluster, expect):
    pa = expect.require_module("pyarrow", name="pyarrow is required to write the fixture")
    if pa is None:
        return
    import psycopg
    # Through require_module, as the import guard requires: pyarrow.parquet is a
    # submodule a bare "import pyarrow" does not load.
    pq = expect.require_module("pyarrow.parquet", name="pyarrow is required to write the fixture")
    if pq is None:
        return
    from pgc_cluster import Cluster, pick_port

    work = pathlib.Path(tempfile.mkdtemp(prefix="pgc-creds-"))
    os.chmod(work, 0o777)
    (work / BUCKET).mkdir()
    os.chmod(work / BUCKET, 0o777)
    fixture = work / BUCKET / "fixture.parquet"
    n = G * RPG
    pq.write_table(pa.table({f"c{i}": pa.array([(i + 1) * v for v in range(n)], pa.int64())
                             for i in range(NCOLS)}),
                   str(fixture), row_group_size=RPG, data_page_size=4096, compression="NONE",
                   use_dictionary=False)
    os.chmod(fixture, 0o644)
    # An Iceberg table for the pgcolumnar_iceberg arms (#1887), served at s3://BUCKET/db/t
    # as the shell serves it. noapply.metadata.json reads 5 rows.
    fx = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "iceberg" / "warehouse_del" / "db"
    shutil.copytree(fx, work / BUCKET / "db")
    for p in [work / BUCKET / "db", *(work / BUCKET / "db").rglob("*")]:
        p.chmod(0o777 if p.is_dir() else 0o666)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _SigV4)
    srv.root, srv.requests = str(work), []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    import urllib.request
    try:
        urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/{BUCKET}/fixture.parquet",
                                                      method="HEAD"), timeout=5)
        answered = "unsigned request admitted"
    except urllib.error.HTTPError as e:
        answered = "403" if e.code == 403 else f"http {e.code}"
    except OSError as e:
        answered = f"no answer: {e}"
    expect.text(answered, "403", "premise: sigv4 fixture server is up")

    datadir = work / "data"
    datadir.mkdir()
    cluster = Cluster(pgc_cluster.pg_config, "creds", datadir, pick_port(9))
    log = str(work / "server.log")
    started = False
    try:
        cluster.initdb()
        with open(datadir / "postgresql.conf", "a") as fh:
            fh.write("\npgcolumnar.objstore_allowed_endpoints='127.0.0.1'\n")
        out = _pg_ctl(cluster, ["-D", str(datadir), "-l", log, "-w", "start"], {})
        started = out.returncode == 0
        if not started:
            raise RuntimeError(f"the private cluster did not start: {out.stderr}")
        dsn = cluster.dsn()

        def sql(stmt, role="postgres"):
            """-> (SQLSTATE or '', first value or None, primary message or '')."""
            try:
                with psycopg.connect(dsn.replace("user=postgres", f"user={role}"), autocommit=True) as c:
                    cur = c.execute(stmt)
                    row = cur.fetchone() if cur.description else None
                    return "", (row[0] if row else None), ""
            except psycopg.Error as e:
                return e.sqlstate or "?", None, e.diag.message_primary or ""

        def count(stmt, role="postgres"):
            """-> the count, or -1 when the read is refused: a refusal is a red arm, not a crash."""
            state, value, _ = sql(stmt, role)
            return -1 if state or value is None else value

        def run(*stmts):
            with psycopg.connect(dsn, autocommit=True) as c:
                for s in stmts:
                    c.execute(s)

        def table_hash(t):
            return sql(f"SELECT md5(string_agg(x::text, ',' ORDER BY x::text)) FROM {t} x")[1]

        cols = ", ".join(f"c{i} int8" for i in range(NCOLS))
        run("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        for r in ("cred_alice", "cred_bob", "cred_carol"):
            run(f"CREATE ROLE {r} LOGIN", f"GRANT pg_read_server_files TO {r}")

        # ---- placement: the validator split
        expect.text(sql("CREATE SERVER badsrv FOREIGN DATA WRAPPER pgcolumnar_parquet"
                        " OPTIONS (secret_access_key 'x')")[0], "HV00D", "placement: secret on a SERVER is HV00D")
        run("CREATE SERVER tmpsrv FOREIGN DATA WRAPPER pgcolumnar_parquet")
        expect.text(sql(f"CREATE FOREIGN TABLE ft_bad ({cols}) SERVER tmpsrv OPTIONS (endpoint 'x')")[0],
                    "HV00D", "placement: endpoint on a TABLE is HV00D")
        run(f"CREATE SERVER csrv FOREIGN DATA WRAPPER pgcolumnar_parquet"
            f" OPTIONS (endpoint 'http://127.0.0.1:{port}', region '{REGION}')")
        expect.num(count("SELECT count(*) FROM pg_foreign_server WHERE srvname = 'csrv'"), 1,
                   "placement: endpoint and region on the SERVER are accepted")
        expect.text(sql(f"CREATE USER MAPPING FOR postgres SERVER csrv"
                        f" OPTIONS (access_key_id '{AKID}', secret_access_key '{SECRET}')")[0] or "ok", "ok",
                    "placement: a credential on a USER MAPPING is accepted")
        expect.text(sql("CREATE USER MAPPING FOR cred_alice SERVER csrv OPTIONS (endpoint 'x')")[0], "HV00D",
                    "placement: endpoint on a USER MAPPING is HV00D")
        run("GRANT USAGE ON FOREIGN SERVER csrv TO cred_alice, cred_bob, cred_carol",
            f"CREATE USER MAPPING FOR cred_alice SERVER csrv OPTIONS (access_key_id '{AKID}', secret_access_key '{SECRET}')",
            f"CREATE USER MAPPING FOR cred_bob SERVER csrv OPTIONS (access_key_id '{AKID}', secret_access_key '{BADSECRET}')")
        expect.text(sql("ALTER USER MAPPING FOR cred_alice SERVER csrv OPTIONS (ADD credentials_required 'false')",
                        "cred_alice")[0], "42501",
                    "placement: a non-superuser cannot set credentials_required false (42501)")
        expect.text(sql("CREATE USER MAPPING FOR cred_carol SERVER csrv OPTIONS (credentials_required 'false')")[0]
                    or "ok", "ok", "placement: a superuser can (for carol, used below)")

        run(f"CREATE FOREIGN TABLE flocal ({cols}) SERVER csrv OPTIONS (path '{fixture}')",
            f"CREATE FOREIGN TABLE fs3 ({cols}) SERVER csrv OPTIONS (path 's3://{BUCKET}/fixture.parquet')",
            "GRANT SELECT ON flocal, fs3 TO cred_alice, cred_bob, cred_carol")

        # ---- phase 1: no AWS environment anywhere
        run("DROP USER MAPPING FOR postgres SERVER csrv")
        expect.text(sql("SELECT count(*) FROM fs3")[0], "28000",
                    "premise: without mapping or environment, even a superuser gets 28000")
        run(f"CREATE USER MAPPING FOR postgres SERVER csrv OPTIONS (access_key_id '{AKID}', secret_access_key '{SECRET}')")
        expect.text(table_hash("fs3") or "unread", table_hash("flocal") or "local unread",
                    "catalog: mapping + server options reach the wire (differential)")
        expect.num(count("SELECT count(*) FROM fs3"), n, "catalog: count(*) with catalog credentials only")
        expect.num(count("SELECT count(*) FROM fs3", "cred_alice"), n,
                   "per-user: alice (good secret in her mapping) reads")
        expect.text(sql("SELECT count(*) FROM fs3", "cred_bob")[0], "28000",
                    "per-user: bob (wrong secret in his mapping) is refused 28000")
        expect.text(sql("SELECT count(*) FROM fs3", "cred_carol")[0], "28000",
                    "ambient-is-privilege: carol (credentials_required=false, but no env exists) is 28000")

        # ---- half a credential (#1341): refused before any request, by its own message
        for r in ("cred_dave", "cred_erin"):
            run(f"CREATE ROLE {r} LOGIN", f"GRANT pg_read_server_files TO {r}",
                f"GRANT USAGE ON FOREIGN SERVER csrv TO {r}", f"GRANT SELECT ON fs3 TO {r}")
        run(f"CREATE USER MAPPING FOR cred_dave SERVER csrv OPTIONS (access_key_id '{AKID}')",
            f"CREATE USER MAPPING FOR cred_erin SERVER csrv OPTIONS (secret_access_key '{SECRET}')")
        half = '28000 columnar: the user mapping for server "csrv" carries only half a credential'
        before = len(srv.requests)
        sql("SELECT count(*) FROM fs3", "cred_alice")
        expect.at_least(len(srv.requests) - before, 1,
                        "control: a full mapping's read reaches the endpoint, so the log counts requests")
        before = len(srv.requests)

        def refusal(role):
            state, _, msg = sql("SELECT count(*) FROM fs3", role)
            return f"{state} {msg}" if state else "ok"

        expect.text(refusal("cred_dave"), half,
                    "a mapping with an access key and no secret is refused before any request (28000)")
        expect.text(refusal("cred_erin"), half, "and one with a secret and no access key is refused the same way")
        expect.num(len(srv.requests) - before, 0, "and neither half-credential read reached the endpoint")

        # ---- no leak
        state, _, bob_msg = sql("SELECT count(*) FROM fs3", "cred_bob")
        with psycopg.connect(dsn, autocommit=True) as c:
            explain = "\n".join(r[0] for r in c.execute("EXPLAIN (VERBOSE, COSTS OFF) SELECT * FROM fs3").fetchall())
        expect.num(sum(s in bob_msg for s in (SECRET, BADSECRET)), 0, "no-leak: bob's verbose error carries no secret")
        expect.text("yes" if state else "no", "yes", "premise: bob's error was a real refusal, not silence")
        expect.num(sum(s in explain for s in (SECRET, BADSECRET)), 0, "no-leak: EXPLAIN VERBOSE carries no secret")
        expect.num(count(f"SELECT count(*) FROM pg_foreign_server WHERE srvoptions::text LIKE '%{SECRET}%'"), 0,
                   "no-leak: srvoptions carry no secret (by construction)")

        # ---- a PUBLIC mapping (#1341): the user's own mapping first, then PUBLIC. Frank
        # has none of his own; bob's own carries the wrong secret, which a PUBLIC mapping
        # holding the right one must not override. Dropped before phase 2.
        run("CREATE ROLE cred_frank LOGIN", "GRANT pg_read_server_files TO cred_frank",
            "GRANT USAGE ON FOREIGN SERVER csrv TO cred_frank", "GRANT SELECT ON fs3 TO cred_frank",
            f"CREATE USER MAPPING FOR PUBLIC SERVER csrv OPTIONS (access_key_id '{AKID}', secret_access_key '{SECRET}')")
        expect.num(count("SELECT count(*) FROM fs3", "cred_frank"), n,
                   "a role with no mapping of its own reads through the PUBLIC mapping")
        expect.text(sql("SELECT count(*) FROM fs3", "cred_bob")[0], "28000",
                    "a role's own mapping is used before the PUBLIC one (bob's wrong secret is still refused 28000)")
        run("DROP USER MAPPING FOR PUBLIC SERVER csrv")

        # ---- phase 2: the environment is present, and ambient is still a privilege
        _pg_ctl(cluster, ["-D", str(datadir), "-m", "fast", "-w", "stop"], {})
        started = _pg_ctl(cluster, ["-D", str(datadir), "-l", log, "-w", "start"],
                          {"AWS_ENDPOINT_URL": f"http://127.0.0.1:{port}", "AWS_ACCESS_KEY_ID": AKID,
                           "AWS_SECRET_ACCESS_KEY": SECRET, "AWS_REGION": REGION}).returncode == 0
        expect.num(count("SELECT count(*) FROM fs3", "cred_carol"), n,
                   "ambient: carol (credentials_required=false) now reads via the environment")
        run("DROP USER MAPPING FOR cred_carol SERVER csrv")
        expect.text(sql("SELECT count(*) FROM fs3", "cred_carol")[0], "28000",
                    "ambient: without her mapping, carol is refused despite the environment")

        # ---- #1887: the pgcolumnar_iceberg wrapper reads with the server environment's
        # credentials. It takes no server or user-mapping options, so carol, refused
        # above on the pgcolumnar_parquet table for want of a mapping, reads through it.
        def outcome(stmt, role="postgres"):
            """-> the value as text, or "SQLSTATE message" of the error."""
            state, value, msg = sql(stmt, role)
            return f"{state} {msg}" if state else str(value)

        expect.text(outcome("CREATE SERVER isrv_bad FOREIGN DATA WRAPPER pgcolumnar_iceberg"
                            f" OPTIONS (endpoint 'http://127.0.0.1:{port}')"),
                    'HV00D invalid option "endpoint" for pgcolumnar_iceberg',
                    "a pgcolumnar_iceberg server refuses an endpoint option (#1887)")
        run("CREATE SERVER isrv FOREIGN DATA WRAPPER pgcolumnar_iceberg",
            "CREATE FOREIGN TABLE fice (id bigint, region text, amount int) SERVER isrv"
            f" OPTIONS (metadata_path 's3://{BUCKET}/db/t/metadata/noapply.metadata.json')",
            "GRANT USAGE ON FOREIGN SERVER isrv TO cred_carol", "GRANT SELECT ON fice TO cred_carol")
        expect.text(outcome(f"CREATE USER MAPPING FOR cred_carol SERVER isrv OPTIONS (access_key_id '{AKID}')"),
                    'HV00D invalid option "access_key_id" for pgcolumnar_iceberg',
                    "a pgcolumnar_iceberg user mapping refuses an access_key_id option (#1887)")
        expect.text(outcome("SELECT count(*) FROM pg_user_mappings WHERE usename = 'cred_carol'"), "0",
                    "premise: carol has no user mapping on either server (#1887)")
        expect.text(outcome("SELECT count(*) FROM fs3", "cred_carol"),
                    f'28000 columnar: no credentials for "s3://{BUCKET}/fixture.parquet"',
                    "ambient: carol, with no mapping, is refused on the pgcolumnar_parquet table: "
                    "no credentials (#1887)")
        expect.text(outcome("SELECT count(*) FROM fice", "cred_carol"), "5",
                    "ambient: carol, with no mapping, reads 5 rows through the pgcolumnar_iceberg table (#1887)")
        run("DROP USER MAPPING FOR postgres SERVER csrv")
        expect.num(count("SELECT count(*) FROM fs3"), n,
                   "ambient: a superuser with no mapping reads via the environment")
    finally:
        if started:
            _pg_ctl(cluster, ["-D", str(datadir), "-m", "immediate", "-w", "stop"], {})
        srv.shutdown()
        shutil.rmtree(work, ignore_errors=True)
