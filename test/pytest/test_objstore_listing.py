"""ListObjectsV2 discovery over remote paths (#619): the pytest twin of
`test/objstore_listing.sh`, every check by the shell's name (#1341).

read_parquet and the FDW expand a remote prefix (trailing slash) or a remote glob into
the objects under it, through a paged ListObjectsV2 loop behind the object-store ABI;
Hive partition columns work over a remote prefix; an exact key is one GET and no LIST.
The local SigV4 fixture server (test/objstore_http_server.py, the shell's own) answers
the listing two keys a page, so the continuation-token loop runs.

EACH REFUSAL IS ITS OWN GUARD'S. The two non-progress refusals share 08P01, so each
check requires its guard's own message: with the no-keys guard removed, the replayed
page reaches the token guard and still raises 08P01. A LIST that fails (HTTP 500) must
be refused by the status check, not read as an empty prefix.

A BACKEND THAT DIED DOES NOT PASS AS ONE THAT SURVIVED: survival reads the private
cluster's server log for killed backends, and every reading waits for the server, so a
crash fails only its own check and survival.

A PRIVATE CLUSTER, restarted with AWS_* in its environment, which the shared cluster
cannot do. The parquet fixtures are written by a subprocess (this module imports no
pyarrow); without pyarrow every test declines under the shell's skip message.

THE SHELL'S REAL-S3 ARMS ARE MIRRORED (#1798), the way test_objstore_s3_read.py mirrors
its own: with PGC_S3_INTEGRATION_ENDPOINT set, the layout goes to a real S3
implementation under a per-run prefix and every remote prefix and glob is compared with
the same layout read from local disk; page/ holds 1100 one-row objects, more than one
real 1000-key page. The virtual-host arm also needs PGC_S3_INTEGRATION_VHOST_ENDPOINT,
whose bucket subdomain resolves. Otherwise these decline as UNMET_PRECONDITION, which
expected_unrunnable.txt lists for every major. A restart that does not come back is a
named red, not an error of the whole module (#1850).
"""
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import psycopg
import pytest

from pgc_vacuity import query_error

TEST = pathlib.Path(__file__).resolve().parents[1]
AKID, SECRET, REGION, BUCKET = "PGCTESTKEYID", "pgctest-secret-shhh", "pgc-test-1", "pgc-bucket"

WRITE_FIXTURES = r'''
import sys, os
import pyarrow as pa, pyarrow.parquet as pq
B = sys.argv[1]
def one(path, ids):
    pq.write_table(pa.table({"id": pa.array(ids, pa.int64())}), path)
one(os.path.join(B, "lst/part-0000.parquet"), [0, 1, 2])
one(os.path.join(B, "lst/part-0001.parquet"), [10, 11, 12])
one(os.path.join(B, "lst/part-0002.parquet"), [20, 21, 22])
one(os.path.join(B, "lst/sub/part-0003.parquet"), [30, 31, 32])
open(os.path.join(B, "lst/_SUCCESS"), "w").close()
open(os.path.join(B, "lst/notes.txt"), "w").write("not parquet\n")
one(os.path.join(B, "hive/dt=2026-01-01/part.parquet"), [1, 2, 3])
one(os.path.join(B, "hive/dt=2026-01-02/part.parquet"), [4, 5, 6])
one(os.path.join(B, "_landing/a.parquet"), [1, 2, 3])
one(os.path.join(B, "_landing/_temporary/t.parquet"), [99])
one(os.path.join(B, ".dir/a.parquet"), [4, 5, 6])
one(os.path.join(B, "glb/a.parquet"), [1, 2, 3])
one(os.path.join(B, "glb/_b.parquet"), [10, 11, 12, 13, 14])
one(os.path.join(B, "glb/.h.parquet"), [100])
'''

STUCK = ('<?xml version="1.0"?><ListBucketResult><IsTruncated>true</IsTruncated>'
         '<NextContinuationToken>STUCK</NextContinuationToken>'
         '<Contents><Key>lst/part-0000.parquet</Key></Contents></ListBucketResult>\n')
EMPTY = ('<?xml version="1.0"?><ListBucketResult><IsTruncated>true</IsTruncated>'
         '<NextContinuationToken>next-1</NextContinuationToken></ListBucketResult>\n')


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


def _row(dsn, sql):
    """-> the first row's columns joined by a space, or the failure as a query_error()."""
    _wait_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    if row is None:
        return query_error(f"no row from {sql}")
    return " ".join("" if v is None else str(v) for v in row)


def _rows(dsn, sql):
    """-> every row as text, sorted and joined by newlines, or the failure as a query_error()."""
    _wait_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            rows = conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return "\n".join(sorted(" ".join(str(v) for v in r) for r in rows)) or "EMPTY"


def _refusal(dsn, sql, phrase, label):
    """-> "<SQLSTATE><label>" when `sql`, under a 10 s statement_timeout, raised with `phrase` in its message, the bare
    SQLSTATE when the message lacks it, or what happened instead."""
    _wait_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            conn.execute("SET statement_timeout = '10s'")
            conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        if not exc.sqlstate:
            return query_error(_message(exc))
        return exc.sqlstate + (label if phrase in str(exc) else "")
    return "no error"


def _list_requests(log):
    try:
        return sum(1 for line in pathlib.Path(log).read_text(errors="replace").splitlines()
                   if "list-type=2" in line)
    except OSError as exc:
        return query_error(f"request log unreadable: {exc}")


def _key_fetched(log, path):
    """-> "logged" when the request log records a GET or HEAD of `path`, else "not logged"."""
    try:
        lines = pathlib.Path(log).read_text(errors="replace").splitlines()
    except OSError as exc:
        return query_error(f"request log unreadable: {exc}")
    hit = any(line.split(" ")[:2] in (["GET", path], ["HEAD", path]) for line in lines)
    return "logged" if hit else "not logged"


def _crashes(log):
    try:
        return pathlib.Path(log).read_text(errors="replace").count("terminated by signal")
    except OSError as exc:
        return query_error(f"crash count unreadable: {exc}")


def _survived(dsn, log, before):
    _wait_up(dsn)
    up, now = _row(dsn, "SELECT 1"), _crashes(log)
    if not isinstance(before, int) or not isinstance(now, int):
        return query_error(f"crash count unreadable: {before} / {now}")
    return "1" if up == "1" and now == before else f"no ([{up}], {now - before} backend crashes)"


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
    """Restart the private cluster with env, as pg_restart_env does. -> True once it
    answers again, False when it does not (#1850): it first waits out a crash recovery,
    because a fast stop sent during the replay did not finish in pg_ctl's 60 s."""
    _wait_up(cluster.dsn())
    _pg_ctl(cluster, ["-D", str(datadir), "-m", "fast", "-w", "stop"], {})
    _pg_ctl(cluster, ["-D", str(datadir), "-l", log, "-w", "start"], env)
    for _ in range(30):
        try:
            with psycopg.connect(cluster.dsn(), autocommit=True, connect_timeout=5) as conn:
                if conn.execute("SELECT 1").fetchone() == (1,):
                    return True
        except psycopg.Error:
            pass
        time.sleep(0.5)
    return False


WRITE_INTEGRATION = r'''
import sys, os
import pyarrow as pa, pyarrow.parquet as pq
B = sys.argv[1]
def one(path, ids):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pq.write_table(pa.table({"id": pa.array(ids, pa.int64())}), path)
one(os.path.join(B, "hid/part.parquet"), [1, 2, 3])
open(os.path.join(B, "hid/_SUCCESS"), "w").close()
one(os.path.join(B, "hid/_temporary/x.parquet"), [99])
one(os.path.join(B, "hid/.hidden.parquet"), [100])
for i in range(1100):
    one(os.path.join(B, "page/k-%04d.parquet" % i), [i])
'''

_NOT_RESTARTED = "no (the cluster did not answer SELECT 1 within 15 s of the restart)"


def _s3cli(ep, *args):
    """The aws CLI against `ep` with the integration credentials. -> CompletedProcess,
    or None when the CLI cannot be run."""
    env = dict(os.environ, AWS_ACCESS_KEY_ID=os.environ.get("PGC_S3_INTEGRATION_KEY", ""),
               AWS_SECRET_ACCESS_KEY=os.environ.get("PGC_S3_INTEGRATION_SECRET", ""),
               AWS_DEFAULT_REGION=os.environ.get("PGC_S3_INTEGRATION_REGION", "garage"))
    try:
        return subprocess.run(["aws", "--endpoint-url", ep, *args], env=env, capture_output=True, text=True)
    except OSError:
        return None


def _first_page(ep, ib, region, key, secret, prefix):
    """ListObjectsV2's first page under `prefix`, signed by curl's own SigV4 (the shell's
    s3firstpage; `aws s3api` fails to start on some aws-cli 2 / Python 3.14 installs).
    -> "KeyCount IsTruncated" ("1000 True"), or the failure as a query_error()."""
    try:
        got = subprocess.run(["curl", "-sS", "--aws-sigv4", f"aws:amz:{region}:s3", "--user", f"{key}:{secret}",
                              f"{ep}/{ib}?list-type=2&prefix={prefix}"], capture_output=True, text=True)
    except OSError as exc:
        return query_error(f"curl could not be run: {exc}")
    if got.returncode != 0:
        return query_error(f"curl failed ({got.returncode}): {got.stderr.strip()[:200]}")
    n = re.search(r"<KeyCount>([0-9]+)</KeyCount>", got.stdout)
    t = re.search(r"<IsTruncated>(true|false)</IsTruncated>", got.stdout)
    if not n or not t:
        return query_error(f"no KeyCount and IsTruncated in the reply: {got.stdout.strip()[:200]}")
    return f"{n.group(1)} {t.group(1).capitalize()}"


def _integration(cluster, datadir, log, bucket, work):
    """The shell's real-S3 block (#1798). -> None when PGC_S3_INTEGRATION_ENDPOINT is
    unset, else a dict holding "restart" and, when the upload worked, the readings."""
    ep = os.environ.get("PGC_S3_INTEGRATION_ENDPOINT", "")
    if not ep:
        return None
    ib = os.environ.get("PGC_S3_INTEGRATION_BUCKET", "pgcolumnar-test")
    region = os.environ.get("PGC_S3_INTEGRATION_REGION", "garage")
    run = f"lst-{os.getpid()}"
    local = work / "int"
    local.mkdir()
    for d in ("lst", "_landing", ".dir", "glb"):
        shutil.copytree(bucket / d, local / d)
    made = subprocess.run([sys.executable, "-c", WRITE_INTEGRATION, str(local)], capture_output=True, text=True)
    if made.returncode != 0:
        raise RuntimeError(f"the integration layout was not written: {made.stderr.strip()}")
    for p in [local, *local.rglob("*")]:
        p.chmod(0o777 if p.is_dir() else 0o666)
    key, secret = os.environ.get("PGC_S3_INTEGRATION_KEY", ""), os.environ.get("PGC_S3_INTEGRATION_SECRET", "")
    creds = {"AWS_ACCESS_KEY_ID": key, "AWS_SECRET_ACCESS_KEY": secret, "AWS_REGION": region}
    i = {"restart": "yes" if _restart(cluster, datadir, log, dict(creds, AWS_ENDPOINT_URL=ep)) else _NOT_RESTARTED}
    host = ep.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    with open(datadir / "postgresql.conf", "a") as fh:
        fh.write(f"\npgcolumnar.objstore_allowed_endpoints='127.0.0.1,{host}'\n")
    dsn = cluster.dsn()
    _row(dsn, "SELECT pg_reload_conf()")
    up = _s3cli(ep, "s3", "cp", "--recursive", "--only-show-errors", str(local), f"s3://{ib}/{run}")
    if up is None or up.returncode != 0:
        return i
    remote = f"s3://{ib}/{run}"

    def rd(root, *subs):
        """each sub's "count sum" under root, joined by "; ", as the shell's rd"""
        return "; ".join(_row(dsn, f"SELECT count(*), sum(id) FROM pgcolumnar.read_parquet("
                                   f"'{root}/{sub}') AS t(id int8)") for sub in subs)

    try:
        i["prefix"] = (_rows(dsn, f"SELECT * FROM pgcolumnar.read_parquet('{remote}/lst/') AS t(id int8)"),
                       _rows(dsn, f"SELECT * FROM pgcolumnar.read_parquet('{local}/lst/') AS t(id int8)"))
        globs = ("lst/*.parquet", "glb/*.parquet", "glb/_*.parquet", "glb/.*.parquet")
        i["glob"] = (rd(remote, *globs), rd(str(local), *globs))
        i["hidden"] = (rd(remote, "hid/", "glb/"), rd(str(local), "hid/", "glb/"))
        named = ("_landing/", ".dir/", "_landing/*.parquet", ".dir/*.parquet")
        i["named"] = (rd(remote, *named), rd(str(local), *named))
        # the server's own first page, read by another client: the premise that 1100
        # keys cannot come back in one page
        i["page cap"] = _first_page(ep, ib, region, key, secret, f"{run}/page/")
        i["paged"] = rd(remote, "page/")
        vep = os.environ.get("PGC_S3_INTEGRATION_VHOST_ENDPOINT", "")
        vhost = vep.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
        try:
            resolves = bool(vep) and bool(socket.getaddrinfo(f"{ib}.{vhost}", None))
        except OSError:
            resolves = False
        if resolves:
            with open(datadir / "postgresql.conf", "a") as fh:
                fh.write(f"\npgcolumnar.objstore_allowed_endpoints='127.0.0.1,{host},{vhost}'\n")
            i["vrestart"] = ("yes" if _restart(cluster, datadir, log, dict(creds, AWS_ENDPOINT_URL=vep))
                             else _NOT_RESTARTED)
            sql = f"SELECT count(*), sum(id) FROM pgcolumnar.read_parquet('{remote}/lst/') AS t(id int8)"
            got = {}
            for style in ("virtual", "path"):
                _wait_up(dsn)
                try:
                    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
                        conn.execute(f"SET pgcolumnar.objstore_s3_addressing = '{style}'")
                        got[style] = " ".join(str(v) for v in conn.execute(sql).fetchone())
                except psycopg.Error as exc:
                    got[style] = query_error(_message(exc))
            i["vhost"] = (got["virtual"], got["path"])
    finally:
        _s3cli(ep, "s3", "rm", "--recursive", "--only-show-errors", f"s3://{ib}/{run}/")
    return i


@pytest.fixture(scope="module")
def osl(pgc_cluster):
    from pgc_cluster import Cluster, pick_port

    work = pathlib.Path(tempfile.mkdtemp(prefix="pgc-objlist-"))
    os.chmod(work, 0o777)
    bucket = work / BUCKET
    for d in ("lst/sub", "hive/dt=2026-01-01", "hive/dt=2026-01-02", "_landing/_temporary", ".dir", "glb"):
        (bucket / d).mkdir(parents=True)
    r = {}
    srv = None
    cluster = None
    datadir = work / "data"
    try:
        made = subprocess.run([sys.executable, "-c", WRITE_FIXTURES, str(bucket)],
                              capture_output=True, text=True)
        if made.returncode != 0:
            raise RuntimeError(f"the parquet fixtures were not written: {made.stderr.strip()}")
        for p in [bucket, *bucket.rglob("*")]:
            p.chmod(0o777 if p.is_dir() else 0o666)

        s3_port = pick_port(29)
        out = work / "s3_server.out"
        s3log = str(work / "s3.log")
        # page size 2 forces the continuation-token loop for the 4-object prefix
        srv = subprocess.Popen([sys.executable, str(TEST / "objstore_http_server.py"), "--dir", str(work),
                                "--port", str(s3_port), "--log", s3log,
                                "--sigv4-key", AKID, "--sigv4-secret", SECRET, "--sigv4-region", REGION,
                                "--list-page-size", "2"],
                               stdout=open(out, "w"), stderr=subprocess.STDOUT)
        for _ in range(50):
            if out.is_file() and "READY" in out.read_text(errors="replace"):
                break
            time.sleep(0.1)
        r["fixture up"] = str(out.read_text(errors="replace").count("READY") if out.is_file() else 0)

        datadir.mkdir()
        log = str(work / "server.log")
        cluster = Cluster(pgc_cluster.pg_config, "objlist", datadir, pick_port(28))
        cluster.initdb()
        with open(datadir / "postgresql.conf", "a") as fh:
            fh.write("\npgcolumnar.objstore_allowed_endpoints='127.0.0.1'\n")
        started = _pg_ctl(cluster, ["-D", str(datadir), "-l", log, "-w", "start"], {})
        if started.returncode != 0:
            raise RuntimeError(f"the private cluster did not start: {started.stderr}")
        dsn = cluster.dsn()
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        if not _restart(cluster, datadir, log, {"AWS_ENDPOINT_URL": f"http://127.0.0.1:{s3_port}",
                                                "AWS_ACCESS_KEY_ID": AKID, "AWS_SECRET_ACCESS_KEY": SECRET,
                                                "AWS_REGION": REGION}):
            raise RuntimeError("cluster did not come back after env restart")
        before = _crashes(log)
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE SERVER s3srv FOREIGN DATA WRAPPER pgcolumnar_parquet")
        rp = f"pgcolumnar.read_parquet('s3://{BUCKET}/lst/') AS t(id int8)"

        open(s3log, "w").close()
        r["prefix"] = _row(dsn, f"SELECT count(*), sum(id) FROM {rp}")
        n = _list_requests(s3log)
        r["paged"] = ("paged" if n >= 2 else f"not paged ({n} list requests)") if isinstance(n, int) else n

        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(f"CREATE FOREIGN TABLE fprefix (id int8) SERVER s3srv OPTIONS (path 's3://{BUCKET}/lst/')")
        r["fdw rows"] = _rows(dsn, "SELECT * FROM fprefix")
        r["rp rows"] = _rows(dsn, f"SELECT * FROM {rp}")
        r["excluded"] = _row(dsn, "SELECT count(*) FROM fprefix")
        r["glob"] = _row(dsn, f"SELECT count(*), sum(id) FROM pgcolumnar.read_parquet("
                              f"'s3://{BUCKET}/lst/*.parquet') AS t(id int8)")

        # #1784: the hidden rule applies below the named prefix only, and a glob
        # matches as a local glob() does
        for sub in ("_landing/", ".dir/", "_landing/*.parquet", ".dir/*.parquet",
                    "glb/*.parquet", "glb/_*.parquet", "glb/.*.parquet", "glb/"):
            r["named " + sub] = _row(dsn, f"SELECT count(*), sum(id) FROM pgcolumnar.read_parquet("
                                          f"'s3://{BUCKET}/{sub}') AS t(id int8)")
        for sub in ("_landing/", "_landing/*.parquet", "glb/[A_]*.parquet"):
            r["named S3://" + sub] = _row(dsn, f"SELECT count(*), sum(id) FROM pgcolumnar.read_parquet("
                                               f"'S3://{BUCKET}/{sub}') AS t(id int8)")

        open(s3log, "w").close()
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(f"CREATE FOREIGN TABLE fexact (id int8) SERVER s3srv "
                      f"OPTIONS (path 's3://{BUCKET}/lst/part-0000.parquet')")
        r["exact"] = _row(dsn, "SELECT count(*), sum(id) FROM fexact")
        n = _list_requests(s3log)
        r["exact lists"] = str(n) if isinstance(n, int) else n
        r["exact logged"] = _key_fetched(s3log, f"/{BUCKET}/lst/part-0000.parquet")

        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(f"CREATE FOREIGN TABLE fhive (id int8, dt date) SERVER s3srv "
                      f"OPTIONS (path 's3://{BUCKET}/hive/', partition_columns 'dt')")
        r["hive"] = _rows(dsn, "SELECT dt, count(*) FROM fhive GROUP BY dt ORDER BY dt")
        plan = _rows(dsn, "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) "
                          "SELECT count(*) FROM fhive WHERE dt = DATE '2026-01-01'")
        pruned = [ln for ln in plan.splitlines() if "Files Pruned" in ln] if not plan.startswith("QUERY_ERROR") else []
        r["pruned"] = pruned[0].split(":")[-1].strip() if pruned else (plan if plan.startswith("QUERY_ERROR")
                                                                       else "no Files Pruned line")

        override = work / "__listing_override__"
        stmt = f"SELECT count(*) FROM {rp}"
        override.write_text(STUCK)
        r["stuck"] = _refusal(dsn, stmt, "returned a non-advancing continuation token", " by the token check")
        override.unlink()
        override.write_text(EMPTY)
        r["empty"] = _refusal(dsn, stmt, "returned a truncated page with no keys", " by the progress check")
        override.unlink()
        status = work / "__listing_status__"
        status.write_text("500\n")
        r["http500"] = _refusal(dsn, stmt, "returned HTTP 500", " by the HTTP status check")
        status.unlink()
        r["survived"] = _survived(dsn, log, before)
        r["integration"] = _integration(cluster, datadir, log, bucket, work)
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
    return request.getfixturevalue("osl")


def test_the_listing_fixture_server_is_up(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["fixture up"], "1", "premise: fixture up")


def test_read_parquet_over_a_prefix_reads_every_object_under_it(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["prefix"], "12 192",
                    "read_parquet over a prefix reads every object under it (4 parts, recursive)")


def test_the_prefix_listing_paged(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["paged"], "paged", "the prefix listing paged (more than one list-type=2 request)")


def test_the_fdw_over_a_prefix_reads_what_read_parquet_reads(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["fdw rows"], r["rp rows"], "FDW over a prefix == read_parquet over the prefix")


def test_hidden_and_non_parquet_objects_are_excluded_from_the_listing(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["excluded"], "12", "hidden (_SUCCESS) and non-parquet (notes.txt) objects are excluded")


def test_a_remote_glob_matches_only_the_top_level_parts(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["glob"], "9 99", "glob s3://.../lst/*.parquet matches the 3 top-level parts, not sub/")


@pytest.mark.parametrize("sub, want, name", [
    ("_landing/", "3 6",
     "a prefix whose own name starts with _ lists the objects below it, still skipping _temporary/"),
    (".dir/", "3 15", "a prefix whose own name starts with . lists the objects below it"),
    ("_landing/*.parquet", "3 6", "glob under a named _ directory (_landing/*.parquet) matches"),
    (".dir/*.parquet", "3 15", "glob under a named . directory (.dir/*.parquet) matches"),
    ("glb/*.parquet", "8 66", "glob glb/*.parquet matches _b.parquet but not .h.parquet, as a local glob does"),
    ("glb/_*.parquet", "5 60", "glob glb/_*.parquet matches the _ file it spells"),
    ("glb/.*.parquet", "1 100", "glob glb/.*.parquet matches the dot file it spells"),
    ("glb/", "3 6", "prefix glb/ still skips _b.parquet and .h.parquet below it"),
    ("S3://_landing/", "3 6", "an upper-case scheme prefix S3://.../_landing/ reads as s3:// does"),
    ("S3://_landing/*.parquet", "3 6",
     "an upper-case scheme glob S3://.../_landing/*.parquet matches as s3:// does"),
    ("S3://glb/[A_]*.parquet", "5 60",
     "an upper-case scheme glob S3://.../glb/[A_]*.parquet is case-sensitive after the scheme"),
])
def test_hidden_names_are_judged_below_the_named_prefix_and_globs_match_as_local(request, expect, sub, want, name):
    """#1784: a prefix or glob the user names may itself start with '_' or '.'."""
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["named " + sub], want, name)


def test_an_exact_key_read_returns_its_rows(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["exact"], "3 3", "exact-key read returns its rows")


def test_an_exact_key_read_issues_no_list(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["exact lists"], "0", "an exact-key read issues no ListObjectsV2")


def test_the_exact_key_read_reached_the_request_log(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["exact logged"], "logged", "and the request log recorded the exact-key read")


def test_hive_over_a_remote_prefix_stamps_the_partition_column(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["hive"], "2026-01-01 3\n2026-01-02 3",
                    "Hive over a remote prefix stamps the partition column")


def test_a_partition_predicate_prunes_a_remote_file(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["pruned"], "1", "a partition predicate prunes a remote file (Files Pruned = 1)")


def test_a_non_advancing_continuation_token_is_refused(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["stuck"], "08P01 by the token check",
                    "a non-advancing continuation token is refused, not an infinite paging loop")


def test_a_truncated_page_with_no_keys_is_refused(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["empty"], "08P01 by the progress check", "a truncated page with no keys is refused, not looped")


def test_a_list_that_fails_is_refused_not_read_as_empty(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["http500"], "08006 by the HTTP status check",
                    "a LIST that returns HTTP 500 is refused, not read as an empty prefix")


def test_the_backend_is_still_up_after_the_listing_refusals(request, expect):
    r = _readings(request, expect)
    if r is not None:
        expect.text(r["survived"], "1", "backend still up after the listing refusals")


_NO_REAL_S3 = ("PGC_S3_INTEGRATION_ENDPOINT names no real S3 implementation (or its upload failed), "
               "so the shell records nothing for these arms either")
_NO_VHOST = ("PGC_S3_INTEGRATION_VHOST_ENDPOINT is unset or its bucket subdomain does not resolve, "
             "so the shell records nothing for this arm either")


_ABSENT = object()


def _real_s3(request, expect, key):
    """-> the integration reading `key`; None after declining as the shell skips (no
    pyarrow); _ABSENT when the shell recorded nothing, so the caller declines by name."""
    r = _readings(request, expect)
    if r is None:
        return None
    i = r["integration"]
    return _ABSENT if i is None or key not in i else i[key]


def test_integration_the_listing_cluster_restarts_with_the_real_s3_credentials(request, expect):
    got = _real_s3(request, expect, "restart")
    if got is _ABSENT:
        expect.cannot_run("UNMET_PRECONDITION", _NO_REAL_S3,
                          name="integration: the cluster restarts with the real S3 credentials")
    elif got is not None:
        expect.text(got, "yes", "integration: the cluster restarts with the real S3 credentials")


def test_integration_a_real_s3_prefix_read_equals_the_local_directory(request, expect):
    got = _real_s3(request, expect, "prefix")
    if got is _ABSENT:
        expect.cannot_run("UNMET_PRECONDITION", _NO_REAL_S3,
                          name="integration: real S3 prefix read == local directory")
    elif got is not None:
        expect.text(got[0], got[1], "integration: real S3 prefix read == local directory")


def test_integration_a_real_s3_glob_equals_the_local_glob(request, expect):
    got = _real_s3(request, expect, "glob")
    if got is _ABSENT:
        expect.cannot_run("UNMET_PRECONDITION", _NO_REAL_S3, name="integration: real S3 glob == local glob")
    elif got is not None:
        expect.text(got[0], got[1], "integration: real S3 glob == local glob")


def test_integration_a_real_s3_skips_hidden_names_like_a_local_directory(request, expect):
    got = _real_s3(request, expect, "hidden")
    if got is _ABSENT:
        expect.cannot_run("UNMET_PRECONDITION", _NO_REAL_S3,
                          name="integration: real S3 skips hidden names like a local directory")
    elif got is not None:
        expect.text(got[0], got[1], "integration: real S3 skips hidden names like a local directory")


def test_integration_a_real_s3_prefix_under_a_hidden_directory_equals_local(request, expect):
    got = _real_s3(request, expect, "named")
    if got is _ABSENT:
        expect.cannot_run("UNMET_PRECONDITION", _NO_REAL_S3,
                          name="integration: real S3 prefix under a _ or . directory == local")
    elif got is not None:
        expect.text(got[0], got[1], "integration: real S3 prefix under a _ or . directory == local")


def test_integration_the_real_s3_serves_1100_keys_in_pages_of_at_most_1000(request, expect):
    got = _real_s3(request, expect, "page cap")
    if got is _ABSENT:
        expect.cannot_run("UNMET_PRECONDITION", _NO_REAL_S3,
                          name="integration: the real S3 serves the 1100 keys in pages of at most 1000")
    elif got is not None:
        expect.text(got, "1000 True", "integration: the real S3 serves the 1100 keys in pages of at most 1000")


def test_integration_a_real_s3_listing_pages_past_1000_keys(request, expect):
    got = _real_s3(request, expect, "paged")
    if got is _ABSENT:
        expect.cannot_run("UNMET_PRECONDITION", _NO_REAL_S3,
                          name="integration: real S3 listing pages past 1000 keys")
    elif got is not None:
        expect.text(got, "1100 604450", "integration: real S3 listing pages past 1000 keys")


def test_integration_the_cluster_restarts_with_the_virtual_host_endpoint(request, expect):
    got = _real_s3(request, expect, "vrestart")
    if got is _ABSENT:
        expect.cannot_run("UNMET_PRECONDITION", _NO_VHOST,
                          name="integration: the cluster restarts with the virtual-host endpoint")
    elif got is not None:
        expect.text(got, "yes", "integration: the cluster restarts with the virtual-host endpoint")


def test_integration_a_real_s3_virtual_host_prefix_read_equals_path_style(request, expect):
    got = _real_s3(request, expect, "vhost")
    if got is _ABSENT:
        expect.cannot_run("UNMET_PRECONDITION", _NO_VHOST,
                          name="integration: real S3 virtual-host prefix read == path-style")
    elif got is not None:
        expect.text(got[0], got[1], "integration: real S3 virtual-host prefix read == path-style")
