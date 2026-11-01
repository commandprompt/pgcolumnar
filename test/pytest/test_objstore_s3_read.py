"""SigV4-signed s3:// reads (#393 M2): the pytest twin of `test/objstore_s3_read.sh`,
every check by the shell's name (#1341).

Reads go through the local fixture server (test/objstore_http_server.py, the shell's
own) in SigV4 mode: it recomputes every request's signature with python's stdlib and
refuses 403 on any mismatch, requires the session token inside the signed set, and
verifies one tamper bucket against a different secret. So every green read proves the
C signer and an independent implementation agree on every byte, with the key, region
and token the postmaster's environment supplied. Reads are compared with the
byte-identical local file, and the request count with the unsigned arithmetic.

A REFUSAL IS NOT ANY ERROR: five guards share 28000 (no endpoint, no region, no
secret, a 403, a missing token), so each refusal must name its own cause: the
tamper bucket's must name the HTTP 403. AWS_REGION must
outrank AWS_DEFAULT_REGION, which must be the fallback.

A BACKEND THAT DIED DOES NOT PASS AS ONE THAT SURVIVED: a refusal's ERROR reaches the
client before abort cleanup runs, so survival also reads the private cluster's server
log for killed backends, and every reading first waits for the server, so a crash
fails its own reading and survival rather than every reading after it.

A PRIVATE CLUSTER, because the postmaster is restarted with AWS_* in its environment,
which the shared cluster cannot do. It starts with every AWS_* variable of this process
removed. The parquet fixture is written by a python3 child, as the shell's heredoc
writes it (this module imports no pyarrow); without pyarrow every test declines under
the shell's skip message. The shell's optional real-S3 arm runs only when
PGC_S3_INTEGRATION_ENDPOINT is set, and declines otherwise.
"""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

import psycopg
import pytest

from pgc_vacuity import query_error

TEST = pathlib.Path(__file__).resolve().parents[1]
AKID, SECRET, REGION, BUCKET = "PGCTESTKEYID", "pgctest-secret-shhh", "pgc-test-1", "pgc-bucket"
TOKEN = "pgctest-session-token"
G, NCOLS, ROWS_PER_GROUP = 3, 8, 8000
COLDEFS = ", ".join(f"c{i} int8" for i in range(NCOLS))
SUM_ALL = " + ".join(f"sum(c{i})" for i in range(NCOLS))

WRITE_FIXTURE = r'''
import sys, shutil
import pyarrow as pa, pyarrow.parquet as pq
G, NCOLS, RPG = (int(a) for a in sys.argv[2:5])
n = G * RPG
cols = {("c%d" % i): pa.array([(i + 1) * v for v in range(n)], pa.int64())
        for i in range(NCOLS)}
pq.write_table(pa.table(cols), sys.argv[1], row_group_size=RPG,
               data_page_size=1024, compression="NONE", use_dictionary=False)
shutil.copyfile(sys.argv[1],
                sys.argv[1].rsplit("/", 1)[0] + "/dir with space/f 1.parquet")
'''

# The versioned overwrite pair (#1867), as the shell writes it: one layout and byte
# length, c0 >= 0 in every row of the first and c0 < 0 in every row of .next.
WRITE_VSWAP = r'''
import os, sys
import pyarrow as pa, pyarrow.parquet as pq
G, RPG = (int(a) for a in sys.argv[2:4])
first = sys.argv[1]
for path, sign in ((first, 1), (first + ".next", -1)):
    c0 = pa.array([sign * v - (sign < 0) for v in range(G * RPG)], pa.int64())
    pq.write_table(pa.table({"c0": c0}), path, row_group_size=RPG,
                   compression="NONE", use_dictionary=False, write_statistics=False)
assert os.path.getsize(first) == os.path.getsize(first + ".next")
'''
VSWAP_SQL = ("SELECT 'old=' || count(*) FILTER (WHERE c0 >= 0) || ' new=' || "
             "count(*) FILTER (WHERE c0 < 0) FROM fs3vswap")


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _wait_up(dsn):
    """Wait up to 15 s for the server, as the shell's readings do after a refusal."""
    for _ in range(30):
        try:
            with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as conn:
                conn.execute("SELECT 1")
            return
        except psycopg.Error:
            time.sleep(0.5)


def _value(dsn, sql):
    """-> the first column of the first row as text, or the failure as a query_error()."""
    _wait_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    if row is None or row[0] is None:
        return query_error(f"no value from {sql}")
    return str(row[0])


def _set_hash(dsn, sql):
    """The shell's pgc_set_hash: an order-independent md5 of the result's rows."""
    return _value(dsn, "SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), 'EMPTY') "
                       f"FROM (SELECT _row::text AS t FROM ({sql}) _row) _s")


def _sqlstate(dsn, sql):
    """-> the SQLSTATE `sql` raised, or what happened instead."""
    _wait_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        return exc.sqlstate or query_error(_message(exc))
    return "no error"


def _refusal(dsn, sql, phrase, label):
    """-> "<SQLSTATE><label>" when `sql` raised with `phrase` in its message, the bare
    SQLSTATE when the message lacks it, or what happened instead."""
    _wait_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        if not exc.sqlstate:
            return query_error(_message(exc))
        return exc.sqlstate + (label if phrase in str(exc) else "")
    return "no error"


def _rows_or_refusal(dsn, sql, phrase, label):
    """The shell's refusal_of when `sql` succeeds: -> "no error (<first value>)", else
    what _refusal gives."""
    _wait_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        if not exc.sqlstate:
            return query_error(_message(exc))
        return exc.sqlstate + (label if phrase in str(exc) else "")
    return f"no error ({row[0] if row else ''})"


def _log_fields(log):
    """-> the request log's lines split into fields (METHOD path query range)."""
    try:
        return [l.split() for l in pathlib.Path(log).read_text(errors="replace").splitlines()]
    except OSError as exc:
        return query_error(f"request log unreadable: {exc}")


def _requests(log):
    try:
        return len(pathlib.Path(log).read_text(errors="replace").splitlines())
    except OSError as exc:
        return query_error(f"request log unreadable: {exc}")


def _crashes(log):
    try:
        return pathlib.Path(log).read_text(errors="replace").count("terminated by signal")
    except OSError as exc:
        return query_error(f"crash count unreadable: {exc}")


def _survived(dsn, log, before):
    _wait_up(dsn)
    up, now = _value(dsn, "SELECT 1"), _crashes(log)
    if not isinstance(before, int) or not isinstance(now, int):
        return query_error(f"crash count unreadable: {before} / {now}")
    return "1" if up == "1" and now == before else f"no ([{up}], {now - before} backend crashes)"


def _https_want(pg_config):
    """The shell's HTTPS_WANT: 08006 when the object-store module links libssl (the
    handshake fails against this plain-HTTP fixture), else 0A000 (refused outright)."""
    try:
        libdir = subprocess.run([pg_config, "--pkglibdir"], capture_output=True, text=True,
                                check=True).stdout.strip()
        ldd = subprocess.run(["ldd", os.path.join(libdir, "pgcolumnar_objstore.so")],
                             capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        return query_error(f"the module's link set is unreadable: {exc}")
    return "08006" if "libssl" in ldd else "0A000"


def _pg_ctl(cluster, args, extra_env):
    """pg_ctl with an explicit environment: every AWS_* of this process removed, then
    `extra_env` added. Dropped to postgres when running as root, as the harness does."""
    unset = [a for k in os.environ if k.startswith("AWS_") for a in ("-u", k)]
    cmd = ["env"] + unset + [f"{k}={v}" for k, v in extra_env.items()] + \
          [os.path.join(cluster.bindir, "pg_ctl")] + args
    if os.geteuid() == 0:
        cmd = ["runuser", "-u", "postgres", "--"] + cmd
    return subprocess.run(cmd, capture_output=True, text=True)


def _restart(cluster, datadir, log, env):
    """Restart the private cluster with env and wait for it, as pg_restart_env does:
    up to 15 s for SELECT 1. A cluster that does not come back is one error here."""
    _wait_up(cluster.dsn())  # a backend that crashed in the last reading: let recovery finish
    _pg_ctl(cluster, ["-D", str(datadir), "-m", "fast", "-w", "stop"], {})
    started = _pg_ctl(cluster, ["-D", str(datadir), "-l", log, "-w", "start"], env)
    for _ in range(30):
        if started.returncode == 0 and _value(cluster.dsn(), "SELECT 1") == "1":
            return
        time.sleep(0.5)
    raise RuntimeError(f"cluster did not come back after env restart: {started.stderr.strip()}")


def _integration(cluster, datadir, log, dsn, local):
    """The shell's phase 4, against a real S3 implementation. -> None when
    PGC_S3_INTEGRATION_ENDPOINT is unset or the upload helper failed (the shell then
    records nothing), else (s3 hash, local hash)."""
    ep = os.environ.get("PGC_S3_INTEGRATION_ENDPOINT", "")
    if not ep:
        return None
    key, secret = os.environ.get("PGC_S3_INTEGRATION_KEY", ""), os.environ.get("PGC_S3_INTEGRATION_SECRET", "")
    region = os.environ.get("PGC_S3_INTEGRATION_REGION", "garage")
    ib, ikey = os.environ.get("PGC_S3_INTEGRATION_BUCKET", "pgcolumnar-test"), f"m2/integration-{os.getpid()}.parquet"
    _restart(cluster, datadir, log, {"AWS_ENDPOINT_URL": ep, "AWS_ACCESS_KEY_ID": key,
                                     "AWS_SECRET_ACCESS_KEY": secret, "AWS_REGION": region})
    try:
        up = subprocess.run(["aws", "--endpoint-url", ep, "s3", "cp", str(local), f"s3://{ib}/{ikey}"],
                            env=dict(os.environ, AWS_ACCESS_KEY_ID=key, AWS_SECRET_ACCESS_KEY=secret,
                                     AWS_DEFAULT_REGION=region), capture_output=True)
    except OSError:
        return None
    if up.returncode != 0:
        return None
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(f"CREATE FOREIGN TABLE fs3real ({COLDEFS}) SERVER s3srv OPTIONS (path 's3://{ib}/{ikey}')")
    return _set_hash(dsn, "SELECT * FROM fs3real"), _set_hash(dsn, "SELECT * FROM flocal")


@pytest.fixture(scope="module")
def osr(pgc_cluster):
    from pgc_cluster import Cluster, pick_port

    work = pathlib.Path(tempfile.mkdtemp(prefix="pgc-s3read-"))
    os.chmod(work, 0o777)
    bucket = work / BUCKET
    (bucket / "dir with space").mkdir(parents=True)
    fixture = bucket / "fixture.parquet"
    r = {}
    srv = None
    cluster = None
    datadir = work / "data"
    try:
        made = subprocess.run([sys.executable, "-c", WRITE_FIXTURE, str(fixture), str(G), str(NCOLS),
                               str(ROWS_PER_GROUP)], capture_output=True, text=True)
        if made.returncode != 0:
            raise RuntimeError(f"the parquet fixture was not written: {made.stderr.strip()}")
        vswap = bucket / "vswap.parquet"
        made = subprocess.run([sys.executable, "-c", WRITE_VSWAP, str(vswap), str(G), str(ROWS_PER_GROUP)],
                              capture_output=True, text=True)
        if made.returncode != 0:
            raise RuntimeError(f"the versioned pair was not written: {made.stderr.strip()}")
        r["vswap sizes"] = (str(vswap.with_name("vswap.parquet.next").stat().st_size),
                            str(vswap.stat().st_size))
        for p in [bucket, *bucket.rglob("*")]:
            p.chmod(0o777 if p.is_dir() else 0o666)
        r["fixture written"] = "yes" if fixture.is_file() and fixture.stat().st_size > 0 else "no"

        s3_port = pick_port(31)
        out = work / "s3_server.out"
        s3log = str(work / "s3.log")
        srv = subprocess.Popen([sys.executable, str(TEST / "objstore_http_server.py"), "--dir", str(work),
                                "--port", str(s3_port), "--log", s3log,
                                "--sigv4-key", AKID, "--sigv4-secret", SECRET, "--sigv4-region", REGION,
                                "--sigv4-token", TOKEN, "--tamper-bucket", "tamperbkt"],
                               stdout=open(out, "w"), stderr=subprocess.STDOUT)
        for _ in range(50):
            if out.is_file() and "READY" in out.read_text(errors="replace"):
                break
            time.sleep(0.1)
        r["server up"] = str(out.read_text(errors="replace").count("READY") if out.is_file() else 0)

        datadir.mkdir()
        log = str(work / "server.log")
        cluster = Cluster(pgc_cluster.pg_config, "s3read", datadir, pick_port(30))
        cluster.initdb()
        with open(datadir / "postgresql.conf", "a") as fh:
            fh.write("\npgcolumnar.objstore_allowed_endpoints='127.0.0.1'\n")
        started = _pg_ctl(cluster, ["-D", str(datadir), "-l", log, "-w", "start"], {})
        if started.returncode != 0:
            raise RuntimeError(f"the private cluster did not start: {started.stderr}")
        dsn = cluster.dsn()
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            c.execute("CREATE SERVER s3srv FOREIGN DATA WRAPPER pgcolumnar_parquet")
            for name, path in (("flocal", str(fixture)), ("fs3", f"s3://{BUCKET}/fixture.parquet"),
                               ("fs3enc", f"s3://{BUCKET}/dir with space/f 1.parquet"),
                               ("fs3tamper", "s3://tamperbkt/fixture.parquet"),
                               ("fs3miss", f"s3://{BUCKET}/no_such.parquet")):
                c.execute(f"CREATE FOREIGN TABLE {name} ({COLDEFS}) SERVER s3srv OPTIONS (path '{path}')")
            # Bucket "vswap" selects the server's versioned mode (#1867).
            c.execute("CREATE FOREIGN TABLE fs3vswap (c0 int8) SERVER s3srv "
                      f"OPTIONS (path 's3://vswap/{BUCKET}/vswap.parquet')")

        # phase 1: no AWS environment at all. The baseline comes first: a backend that
        # dies in this read must move the count.
        before = _crashes(log)
        r["no env"] = _refusal(dsn, "SELECT count(*) FROM fs3",
                               "requires AWS_ENDPOINT_URL in the server environment", " naming AWS_ENDPOINT_URL")

        # phase 2: the full ambient environment; AWS_DEFAULT_REGION names a region the
        # fixture refuses, so AWS_REGION must win
        endpoint = f"http://127.0.0.1:{s3_port}"
        _restart(cluster, datadir, log, {"AWS_ENDPOINT_URL": endpoint, "AWS_ACCESS_KEY_ID": AKID,
                                         "AWS_SECRET_ACCESS_KEY": SECRET, "AWS_REGION": REGION,
                                         "AWS_DEFAULT_REGION": "pgc-wrong-1", "AWS_SESSION_TOKEN": TOKEN})
        r["outranks"] = _value(dsn, "SELECT count(*) FROM fs3")
        r["full"] = (_set_hash(dsn, "SELECT * FROM fs3"), _set_hash(dsn, "SELECT * FROM flocal"))
        r["projection"] = (_set_hash(dsn, "SELECT c1, c6 FROM fs3"), _set_hash(dsn, "SELECT c1, c6 FROM flocal"))
        r["encoded"] = (_set_hash(dsn, "SELECT * FROM fs3enc"), _set_hash(dsn, "SELECT * FROM flocal"))
        r["count"] = _value(dsn, "SELECT count(*) FROM fs3")
        _wait_up(dsn)
        open(s3log, "w").close()
        _value(dsn, f"SELECT {SUM_ALL} FROM fs3")
        r["requests"] = _requests(s3log)
        _wait_up(dsn)
        open(s3log, "w").close()
        r["vswap"] = _rows_or_refusal(dsn, VSWAP_SQL, "changed while it was being read",
                                      " changed while it was being read")
        fields = _log_fields(s3log)
        if isinstance(fields, list):
            gets = [f for f in fields if len(f) > 2 and f[0] == "GET" and "vswap" in f[1]]
            r["vswap gets"] = len(gets)
            r["vswap no id"] = sum(1 for f in gets if not f[2].startswith("versionId=3HL4%2Bkq%2F"))
        else:
            r["vswap gets"] = r["vswap no id"] = fields
        r["tamper"] = _refusal(dsn, "SELECT count(*) FROM fs3tamper",
                               "was denied (HTTP 403", " as a 403")
        r["miss"] = _refusal(dsn, "SELECT count(*) FROM fs3miss", "does not exist (HTTP 404)", " as a 404")

        # phase 2b: AWS_DEFAULT_REGION alone is the fallback
        _restart(cluster, datadir, log, {"AWS_ENDPOINT_URL": endpoint, "AWS_ACCESS_KEY_ID": AKID,
                                         "AWS_SECRET_ACCESS_KEY": SECRET, "AWS_DEFAULT_REGION": REGION,
                                         "AWS_SESSION_TOKEN": TOKEN})
        r["fallback"] = _value(dsn, "SELECT count(*) FROM fs3")

        # phase 2c: no region at all, then no secret
        _restart(cluster, datadir, log, {"AWS_ENDPOINT_URL": endpoint, "AWS_ACCESS_KEY_ID": AKID,
                                         "AWS_SECRET_ACCESS_KEY": SECRET, "AWS_SESSION_TOKEN": TOKEN})
        r["no region"] = _refusal(dsn, "SELECT count(*) FROM fs3", "requires a region option", " naming the region")
        _restart(cluster, datadir, log, {"AWS_ENDPOINT_URL": endpoint, "AWS_ACCESS_KEY_ID": AKID,
                                         "AWS_REGION": REGION, "AWS_SESSION_TOKEN": TOKEN})
        r["no secret"] = _refusal(dsn, "SELECT count(*) FROM fs3",
                                  "requires AWS_SECRET_ACCESS_KEY in the server environment",
                                  " naming AWS_SECRET_ACCESS_KEY")

        # phase 3: an https endpoint is never silently downgraded
        _restart(cluster, datadir, log, {"AWS_ENDPOINT_URL": f"https://127.0.0.1:{s3_port}",
                                         "AWS_ACCESS_KEY_ID": AKID, "AWS_SECRET_ACCESS_KEY": SECRET,
                                         "AWS_REGION": REGION})
        want = _https_want(pgc_cluster.pg_config)
        r["https"] = (f"{_sqlstate(dsn, 'SELECT count(*) FROM fs3')} for this build", f"{want} for this build")
        r["survived"] = _survived(dsn, log, before)

        r["integration"] = _integration(cluster, datadir, log, dsn, fixture)
        yield r
    finally:
        if cluster is not None:
            _pg_ctl(cluster, ["-D", str(datadir), "-m", "immediate", "-w", "stop"], {})
        if srv is not None:
            srv.kill()
            srv.wait()
        shutil.rmtree(work, ignore_errors=True)


def _readings(request, expect):
    """-> the fixture's readings, or None after declining as the shell skips."""
    if expect.require_module("pyarrow.parquet", name="pyarrow is required to write the fixture") is None:
        return None
    return request.getfixturevalue("osr")


def test_the_fixture_is_written(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["fixture written"], "yes", "premise: fixture written")


def test_the_sigv4_fixture_server_is_up(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["server up"], "1", "premise: sigv4 fixture server is up")


def test_an_s3_read_with_no_environment_is_refused_naming_the_endpoint(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["no env"], "28000 naming AWS_ENDPOINT_URL", "config: s3 read with no environment is 28000")


def test_aws_region_outranks_aws_default_region(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["outranks"], str(G * ROWS_PER_GROUP), "region: AWS_REGION outranks AWS_DEFAULT_REGION")


def test_a_full_scan_over_s3_reads_what_the_local_file_reads(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(*r["full"], "A: full scan s3 == local")


def test_a_projection_over_s3_reads_what_the_local_file_reads(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(*r["projection"], "A: projection s3 == local")


def test_a_percent_encoded_key_over_s3_reads_what_the_local_file_reads(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(*r["encoded"], "A: percent-encoded key s3 == local")


def test_count_over_s3(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["count"], str(G * ROWS_PER_GROUP), "A: count(*) over s3")


def test_the_signed_run_logged_requests(request, expect):
    r = _readings(request, expect)
    if r is not None:
        n = r["requests"]
        expect.text("yes" if isinstance(n, int) and n > 0 else f"no ({n})", "yes",
                    "premise: signed run logged requests")


def test_signing_left_the_request_arithmetic_unchanged(request, expect):
    """open = HEAD + 3 GETs, data = one GET per column chunk."""
    r = _readings(request, expect)
    if r is not None:
        expect.text(str(r["requests"]), str(4 + G * NCOLS), "B: signing left the request arithmetic unchanged")


def test_the_versioned_overwrite_pair_has_one_byte_length(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(*r["vswap sizes"], "premise: the versioned overwrite pair has one byte length")


def test_a_signed_versioned_read_overwritten_mid_read_finishes_on_the_version_it_opened(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["vswap"], f"no error (old={G * ROWS_PER_GROUP} new=0)",
                    "C: a signed versioned read overwritten mid-read finishes on the version it opened")


def test_the_signed_versioned_read_logged_gets(request, expect):
    r = _readings(request, expect)
    if r is not None:
        n = r["vswap gets"]
        expect.text("yes" if isinstance(n, int) and n > 0 else f"no ({n})", "yes",
                    "premise: the signed versioned read logged GETs")


def test_every_signed_ranged_get_of_a_versioned_read_names_the_version(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.num(r["vswap no id"], 0, "C: every signed ranged GET of a versioned read names the version")


def test_the_tamper_bucket_surfaces_its_403_as_28000(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["tamper"], "28000 as a 403", "403: tamper bucket surfaces 28000")


def test_a_verified_request_for_a_missing_key_still_reaches_404(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["miss"], "58P01 as a 404", "403 ordering premise: verified requests still reach 404 = 58P01")


def test_aws_default_region_is_the_fallback(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["fallback"], str(G * ROWS_PER_GROUP), "region: AWS_DEFAULT_REGION is the fallback")


def test_an_s3_read_with_no_region_is_refused_naming_it(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["no region"], "28000 naming the region", "config: s3 read with no region is refused naming it")


def test_an_s3_read_with_no_secret_is_refused_naming_it(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["no secret"], "28000 naming AWS_SECRET_ACCESS_KEY",
                    "config: s3 read with no AWS_SECRET_ACCESS_KEY is refused naming it")


def test_an_https_endpoint_is_not_downgraded(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(*r["https"], "https endpoint is not downgraded")


def test_the_backend_is_still_up_after_the_s3_refusals(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["survived"], "1", "backend still up after the s3 refusals")


def test_a_real_s3_implementation_reads_what_the_local_file_reads(request, expect):
    r = _readings(request, expect)
    if r is None:
        return
    if r["integration"] is None:
        expect.cannot_run("UNMET_PRECONDITION",
                          "PGC_S3_INTEGRATION_ENDPOINT names no real S3 implementation (or its upload "
                          "failed), so the shell records nothing either",
                          name="integration: real S3 implementation == local")
        return
    expect.text(*r["integration"], "integration: real S3 implementation == local")
