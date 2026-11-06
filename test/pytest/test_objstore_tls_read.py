"""TLS for the object-store module (#393 M3): the pytest twin of
`test/objstore_tls_read.sh`, every check by the shell's name (#1341).

A refusal matrix over the shell's own fixture server (test/objstore_http_server.py)
and the shell's own PKI, made by the openssl CLI: one server whose certificate is good
(CA-1, SAN IP:127.0.0.1), one that signs SigV4 over TLS, and four that must each be
refused for a different reason -- a wrong host, an expired certificate, an untrusted
CA, and a self-signed leaf. The good arm is the over-restriction proof: a module that
refuses everything cannot pass it.

A REFUSAL IS NOT ANY ERROR: each must be 08006 with the module's own message naming
the URL it could not reach, and with OpenSSL's verification reason for THAT
certificate as the whole DETAIL. A cleartext request to a TLS port also fails 08006,
and a different verification failure also mentions a certificate; neither passes.
Each also carries the premise that the handshake was attempted -- the fixture logs
HANDSHAKE at accept, before the handshake can fail -- and waits for SELECT 1
afterwards, so a crash fails its own checks, which the server log's "terminated by
signal" count reports at the end.

Trust comes only from the postmaster environment, which OpenSSL's default verify
paths honour: SSL_CERT_FILE for every arm but E, which restarts with SSL_CERT_DIR
alone (CA-1 under its subject hash) and must still read the good server.

The PKI, the parquet fixture and the logs live in `server_dir`, because the backend
reads the CA and the local fixture. The parquet object is written by a python3 child,
as the shell's heredoc writes it.

A PRIVATE CLUSTER, because the postmaster is restarted with SSL_CERT_* and AWS_* in
its environment, which the shared cluster cannot do. It starts with every AWS_* and
SSL_CERT_* variable of this process removed.
"""
import os
import pathlib
import shutil
import subprocess
import sys
import time

import psycopg
import pytest

from pgc_vacuity import query_error

TEST = pathlib.Path(__file__).resolve().parents[1]
AKID, SECRET, REGION, BUCKET = "PGCTESTKEYID", "pgctest-secret-shhh", "pgc-test-1", "pgc-bucket"
G, NCOLS, ROWS_PER_GROUP = 3, 8, 8000
COLDEFS = ", ".join(f"c{i} int8" for i in range(NCOLS))
# label -> (foreign table, server, OpenSSL's verification reason)
REFUSED = {
    "wrong-host": ("fwrong", "wronghost", "IP address mismatch"),
    "expired": ("fexp", "expired", "certificate has expired"),
    "untrusted-ca": ("funtr", "untrusted", "unable to get local issuer certificate"),
    "self-signed": ("fself", "selfsigned", "self-signed certificate"),
}
CRASH = "terminated by signal"


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _rows(dsn, *sqls):
    """Run `sqls` on one connection -> the last one's rows, or a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            for sql in sqls[:-1]:
                conn.execute(sql)
            return conn.execute(sqls[-1]).fetchall()
    except psycopg.Error as exc:
        failed = _message(exc)
    return query_error(failed)


def _one(dsn, sql):
    """-> the first column of the first row, or a query_error()."""
    rows = _rows(dsn, sql)
    if not isinstance(rows, list):
        return rows
    if not rows or rows[0][0] is None:
        return query_error(f"no value from {sql}")
    return rows[0][0]


def _refusal(dsn, *sqls):
    """-> {"state", "message", "detail", "rows"} of the last of `sqls`: what its error
    said, or "no error" and the rows it returned."""
    err = None
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            for sql in sqls[:-1]:
                conn.execute(sql)
            rows = conn.execute(sqls[-1]).fetchall()
    except psycopg.Error as exc:
        err = exc
    if err is None:
        return {"state": "no error", "message": "no error", "detail": "no error", "rows": len(rows)}
    said = _message(err)
    return {"state": err.sqlstate or query_error(said),
            "message": err.diag.message_primary or query_error(said),
            "detail": err.diag.message_detail or query_error(f"no DETAIL: {said}"),
            "rows": 0}


def _wait_up(dsn):
    """A crashed backend fails its own check, not the next arm's: wait up to 15 s for
    SELECT 1, as the shell's wait_up does."""
    for _ in range(30):
        if _one(dsn, "SELECT 1") == 1:
            return
        time.sleep(0.5)


def _count(path, prefix):
    """-> the lines of `path` that start with (or, for the server log, contain)
    `prefix`, or a query_error()."""
    try:
        lines = pathlib.Path(path).read_text(errors="replace").splitlines()
    except OSError as exc:
        return query_error(f"{path} unreadable: {exc}")
    return sum(1 for l in lines if (prefix in l if prefix == CRASH else l.startswith(prefix)))


def _openssl(*args):
    return subprocess.run(["openssl"] + [str(a) for a in args], capture_output=True, text=True)


def _pki(pki):
    """The shell's PKI, by the same openssl commands. -> None, or what failed."""
    pki.mkdir()

    def ca(name):
        _openssl("genrsa", "-out", pki / f"{name}.key", "2048")
        _openssl("req", "-x509", "-new", "-key", pki / f"{name}.key", "-subj", f"/CN=pgc-test-{name}",
                 "-days", "2", "-out", pki / f"{name}.pem")

    def cert(name, issuer, san, *extra):
        _openssl("genrsa", "-out", pki / f"{name}.key", "2048")
        _openssl("req", "-new", "-key", pki / f"{name}.key", "-subj", f"/CN={name}", "-out", pki / f"{name}.csr")
        (pki / f"{name}.ext").write_text(f"subjectAltName={san}\n")
        _openssl("x509", "-req", "-in", pki / f"{name}.csr", "-CA", pki / f"{issuer}.pem",
                 "-CAkey", pki / f"{issuer}.key", "-CAcreateserial", "-days", "2",
                 "-extfile", pki / f"{name}.ext", *extra, "-out", pki / f"{name}.pem")

    ca("ca1")
    ca("ca2")
    cert("good", "ca1", "IP:127.0.0.1")
    cert("goodsig", "ca1", "IP:127.0.0.1")
    cert("wronghost", "ca1", "DNS:other.example")
    # -not_before/-not_after need OpenSSL 3.4; on an older one, a zero-lifetime
    # certificate, as the shell falls back to. The premise proves it expired.
    cert("expired", "ca1", "IP:127.0.0.1", "-not_before", "20200101000000Z", "-not_after", "20200102000000Z")
    if not (pki / "expired.pem").is_file() or (pki / "expired.pem").stat().st_size == 0:
        cert("expired", "ca1", "IP:127.0.0.1", "-days", "0")
    cert("untrusted", "ca2", "IP:127.0.0.1")
    _openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", pki / "selfsigned.key",
             "-subj", "/CN=selfsigned", "-addext", "subjectAltName=IP:127.0.0.1",
             "-days", "2", "-out", pki / "selfsigned.pem")
    (pki / "cadir").mkdir()
    h = _openssl("x509", "-hash", "-noout", "-in", pki / "ca1.pem").stdout.strip()
    shutil.copy(pki / "ca1.pem", pki / "cadir" / f"{h}.0")
    for f in ("ca1", "ca2", "good", "goodsig", "wronghost", "expired", "untrusted", "selfsigned"):
        p = pki / f"{f}.pem"
        if not p.is_file() or p.stat().st_size == 0:
            return f"PKI generation failed for {f}"
    return None


def _write_parquet(path):
    """The shell's fixture, written by a python3 child as its heredoc does. -> None, or
    the decline when pyarrow is absent. Any other failure raises."""
    w = subprocess.run([sys.executable, "-c",
                        "import sys, pyarrow as pa, pyarrow.parquet as pq\n"
                        "G, NCOLS, RPG = (int(a) for a in sys.argv[2:5])\n"
                        "n = G * RPG\n"
                        "cols = {('c%d' % i): pa.array([(i + 1) * v for v in range(n)], pa.int64())"
                        " for i in range(NCOLS)}\n"
                        "pq.write_table(pa.table(cols), sys.argv[1], row_group_size=RPG,"
                        " data_page_size=1024, compression='NONE', use_dictionary=False)\n",
                        str(path), str(G), str(NCOLS), str(ROWS_PER_GROUP)], capture_output=True, text=True)
    if w.returncode == 0:
        return None
    if "No module named 'pyarrow" in w.stderr:
        return ("MISSING_DEPENDENCY", "pyarrow is not installed")
    raise RuntimeError(f"writing {path} failed: {w.stderr.strip()}")


def _pg_ctl(cluster, args, extra_env):
    """pg_ctl with an explicit environment: every AWS_* and SSL_CERT_* of this process
    removed, then `extra_env` added. Dropped to postgres when running as root, as the
    harness does."""
    unset = [a for k in os.environ if k.startswith(("AWS_", "SSL_CERT_")) for a in ("-u", k)]
    cmd = ["env"] + unset + [f"{k}={v}" for k, v in extra_env.items()] + \
          [os.path.join(cluster.bindir, "pg_ctl")] + args
    if os.geteuid() == 0:
        cmd = ["runuser", "-u", "postgres", "--"] + cmd
    return subprocess.run(cmd, capture_output=True, text=True)


def _restart(cluster, datadir, log, env):
    """Restart the private cluster with env, and wait up to 15 s for SELECT 1."""
    _pg_ctl(cluster, ["-D", str(datadir), "-m", "fast", "-w", "stop"], {})
    started = _pg_ctl(cluster, ["-D", str(datadir), "-l", log, "-w", "start"], env)
    for _ in range(30):
        if started.returncode == 0 and _one(cluster.dsn(), "SELECT 1") == 1:
            return
        time.sleep(0.5)
    raise RuntimeError(f"cluster did not come back after env restart: {started.stderr.strip()}")


@pytest.fixture(scope="module")
def otr(pgc_cluster, server_dir):
    if shutil.which("openssl") is None:
        yield {"absent": ("MISSING_DEPENDENCY", "the openssl CLI is not installed"), "openssl": False}
        return
    work = server_dir
    (work / BUCKET).mkdir()
    wrote = _write_parquet(work / BUCKET / "fixture.parquet")
    if wrote is not None:
        yield {"absent": wrote, "openssl": True}
        return
    from pgc_cluster import Cluster, pick_port

    pki, tlslog = work / "pki", str(work / "tls.log")
    failed = _pki(pki)
    if failed is not None:
        raise RuntimeError(failed)
    r = {"absent": None, "openssl": True}
    r["expired premise"] = "valid" if _openssl("x509", "-in", pki / "expired.pem", "-checkend", "0").returncode == 0 \
        else "expired"

    srvs, port = [], {}
    for slot, (name, extra) in enumerate([
            ("good", []),
            ("goodsig", ["--sigv4-key", AKID, "--sigv4-secret", SECRET, "--sigv4-region", REGION]),
            ("wronghost", []), ("expired", []), ("untrusted", []), ("selfsigned", [])]):
        port[name] = pick_port(40 + slot)
        out = work / f"srv_{name}.out"
        srvs.append(subprocess.Popen(
            [sys.executable, str(TEST / "objstore_http_server.py"), "--dir", str(work),
             "--port", str(port[name]), "--log", tlslog,
             "--tls-cert", str(pki / f"{name}.pem"), "--tls-key", str(pki / f"{name}.key")] + extra,
            stdout=open(out, "w"), stderr=subprocess.STDOUT))
        for _ in range(50):
            if out.is_file() and "READY" in out.read_text(errors="replace"):
                break
            time.sleep(0.1)
    r["servers up"] = sum(1 for n in port if "READY" in (work / f"srv_{n}.out").read_text(errors="replace"))

    datadir = work / "data"
    datadir.mkdir()
    log = str(work / "server.log")
    cluster = Cluster(pgc_cluster.pg_config, "otr", datadir, pick_port(39))
    try:
        cluster.initdb()
        with open(datadir / "postgresql.conf", "a") as fh:
            fh.write("\npgcolumnar.objstore_allowed_endpoints='127.0.0.1'\n")
        started = _pg_ctl(cluster, ["-D", str(datadir), "-l", log, "-w", "start"], {})
        if started.returncode != 0:
            raise RuntimeError(f"the private cluster did not start: {started.stderr}")
        dsn = cluster.dsn()
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        _restart(cluster, datadir, log, {
            "SSL_CERT_FILE": str(pki / "ca1.pem"), "AWS_ENDPOINT_URL": f"https://127.0.0.1:{port['goodsig']}",
            "AWS_ACCESS_KEY_ID": AKID, "AWS_SECRET_ACCESS_KEY": SECRET, "AWS_REGION": REGION})
        crashes_before = _count(log, CRASH)

        def url(name, prefix=""):
            return f"https://127.0.0.1:{port[name]}/{prefix}{BUCKET}/fixture.parquet"

        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE SERVER tlssrv FOREIGN DATA WRAPPER pgcolumnar_parquet")
            for tbl, path in [("flocal", str(work / BUCKET / "fixture.parquet")), ("fgood", url("good")),
                              ("fs3tls", f"s3://{BUCKET}/fixture.parquet"), ("fwrong", url("wronghost")),
                              ("fexp", url("expired")), ("funtr", url("untrusted")),
                              ("fself", url("selfsigned")), ("fstall", url("good", "stall/"))]:
                c.execute(f"CREATE FOREIGN TABLE {tbl} ({COLDEFS}) SERVER tlssrv OPTIONS (path '{path}')")

        r["local"] = _rows(dsn, "SELECT * FROM flocal")
        r["A https"] = _rows(dsn, "SELECT * FROM fgood")
        r["A count"] = _one(dsn, "SELECT count(*) FROM fgood")
        r["A2 s3"] = _rows(dsn, "SELECT * FROM fs3tls")

        for label, (tbl, name, _) in REFUSED.items():
            hs0 = _count(tlslog, "HANDSHAKE")
            got = _refusal(dsn, f"SELECT count(*) FROM {tbl}")
            hs1 = _count(tlslog, "HANDSHAKE")
            _wait_up(dsn)
            got["attempted"] = "yes" if isinstance(hs0, int) and isinstance(hs1, int) and hs1 > hs0 \
                else f"no: HANDSHAKE lines {hs0} -> {hs1}"
            got["url"] = url(name)
            r[label] = got

        t0 = time.monotonic()
        r["D state"] = _refusal(dsn, "SET statement_timeout = '2s'", "SELECT count(*) FROM fstall")["state"]
        r["D elapsed"] = time.monotonic() - t0
        _wait_up(dsn)

        _restart(cluster, datadir, log, {"SSL_CERT_DIR": str(pki / "cadir")})
        r["E count"] = _one(dsn, "SELECT count(*) FROM fgood")

        after = _count(log, CRASH)
        r["crashes"] = after - crashes_before if isinstance(after, int) and isinstance(crashes_before, int) \
            else query_error(f"crash count unreadable: {crashes_before} -> {after}")
        yield r
    finally:
        _pg_ctl(cluster, ["-D", str(datadir), "-m", "immediate", "-w", "stop"], {})
        for s in srvs:
            s.kill()
            s.wait()


def _declined(otr, expect):
    if otr["absent"] is None:
        return False
    expect.cannot_run(*otr["absent"])
    return True


def test_the_expired_certificate_is_genuinely_expired(otr, expect):
    """The shell skips the whole suite without pyarrow or the openssl CLI, under these
    names."""
    if expect.require_module("pyarrow", name="pyarrow is required to write the fixture") is None:
        return
    if not otr["openssl"]:
        expect.cannot_run(*otr["absent"], name="openssl CLI is required to generate the fixture PKI")
        return
    expect.text(otr["expired premise"], "expired", "premise: expired certificate is genuinely expired")


def test_six_tls_servers_are_up(otr, expect):
    if _declined(otr, expect):
        return
    expect.num(otr["servers up"], 6, "premise: six TLS servers are up")


def test_an_https_read_equals_the_local_read(otr, expect):
    if _declined(otr, expect):
        return
    expect.row_set(otr["A https"], otr["local"], "A: https differential == local")


def test_count_over_https(otr, expect):
    if _declined(otr, expect):
        return
    expect.num(otr["A count"], G * ROWS_PER_GROUP, "A: count(*) over https")


def test_s3_over_https_equals_the_local_read(otr, expect):
    if _declined(otr, expect):
        return
    expect.row_set(otr["A2 s3"], otr["local"], "A2: s3 over https differential == local")


@pytest.mark.parametrize("label", sorted(REFUSED))
def test_the_handshake_was_attempted(otr, expect, label):
    if _declined(otr, expect):
        return
    expect.text(otr[label]["attempted"], "yes", f"premise: {label} handshake was attempted")


@pytest.mark.parametrize("label", sorted(REFUSED))
def test_the_certificate_is_refused_08006(otr, expect, label):
    if _declined(otr, expect):
        return
    expect.text(otr[label]["state"], "08006", f"C: {label} refused with 08006")


@pytest.mark.parametrize("label", sorted(REFUSED))
def test_the_refusal_is_the_tls_one(otr, expect, label):
    if _declined(otr, expect):
        return
    expect.text(otr[label]["message"], f'columnar: could not establish TLS to "{otr[label]["url"]}"',
                f"C: {label} refusal is the TLS one")


@pytest.mark.parametrize("label", sorted(REFUSED))
def test_the_refusal_names_its_reason(otr, expect, label):
    """The whole DETAIL: OpenSSL's verification reason for this certificate."""
    if _declined(otr, expect):
        return
    expect.text(otr[label]["detail"], f"Certificate verification failed: {REFUSED[label][2]}.",
                f"C: {label} names its reason")


@pytest.mark.parametrize("label", sorted(REFUSED))
def test_the_refused_read_returned_no_rows(otr, expect, label):
    if _declined(otr, expect):
        return
    expect.num(otr[label]["rows"], 0, f"C: {label} returned no rows")


def test_a_stalled_tls_transfer_is_cancelled(otr, expect):
    if _declined(otr, expect):
        return
    expect.text(otr["D state"], "57014", "D: stalled TLS transfer cancelled with query_canceled")


def test_the_cancel_latency_is_bounded(otr, expect):
    if _declined(otr, expect):
        return
    got = "bounded" if otr["D elapsed"] <= 10 else f"slow({otr['D elapsed']:.0f}s)"
    expect.text(got, "bounded", "D: cancel latency bounded (<=10s)")


def test_ssl_cert_dir_alone_trusts_the_ca(otr, expect):
    if _declined(otr, expect):
        return
    expect.num(otr["E count"], G * ROWS_PER_GROUP, "E: SSL_CERT_DIR alone trusts the CA (count(*) over https)")


def test_no_backend_was_terminated_by_a_signal(otr, expect):
    if _declined(otr, expect):
        return
    expect.num(otr["crashes"], 0, "survival: no backend was terminated by a signal")
