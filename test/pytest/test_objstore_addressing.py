"""The gs:// scheme and S3 virtual-host addressing (#621): the pytest twin of
`test/objstore_addressing.sh`, every check by the shell's name (#1341).

Reads go through the local SigV4 fixture server (test/objstore_http_server.py, the
shell's own) started with --virtual-host-base s3.local, so a read that returns data
proves the signer signed the bytes the endpoint expects. The answer alone does not
prove the addressing: the fixture serves the same object either way, so every
addressing check also reads the request path the server LOGGED -- the bucket in the
path under path-style, the key alone under virtual-host -- and a key with a space,
'+' and '=' must arrive URI-encoded exactly once. A prefix or pattern read, and a
failed parallel export's upload cleanup, list the bucket: under virtual-host that LIST
must go to the bucket root, "/", and the fixture answers any other path with the 404
S3 gives (#1783).

A REFUSAL IS NOT ANY ERROR: with no endpoint, s3:// must be refused 28000 naming
AWS_ENDPOINT_URL, and gs:// must reach its default host, which this cluster's
allow-list refuses 42501 naming storage.googleapis.com:443 -- not a mistyped default,
not some other error, not a crash with no message.

The parquet objects are written by a python3 child, as the shell's heredoc writes them.

A PRIVATE CLUSTER, because the postmaster is restarted with AWS_* in its environment,
which the shared cluster cannot do. It starts with every AWS_* variable of this process
removed. Virtual-host needs pgc-bucket.s3.local to resolve to this host, as the shell
does; where it does not, the first test declines under the shell's pgc_skip message and
the rest decline, as they do without pyarrow.
"""
import os
import pathlib
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
AKID, SECRET, REGION, BUCKET = "PGCTESTKEYID", "pgctest-secret", "pgc-test-1", "pgc-bucket"
ENCKEY = "dir sp/a+b=c.parquet"
RD = "SELECT count(*), sum(id) FROM pgcolumnar.read_parquet"
VIRTUAL = "SET pgcolumnar.objstore_s3_addressing='virtual'"
PATH = "SET pgcolumnar.objstore_s3_addressing='path'"


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _row(dsn, *sqls):
    """Run `sqls` on one connection -> the last one's first row, columns joined by a
    space, or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            for sql in sqls[:-1]:
                conn.execute(sql)
            row = conn.execute(sqls[-1]).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    if row is None or any(v is None for v in row):
        return query_error(f"no value from {sqls[-1]}")
    return " ".join(str(v) for v in row)


def _refusal(dsn, sql, phrase, label):
    """-> "<SQLSTATE><label>" when `sql` raised with `phrase` in its message, the bare
    SQLSTATE when the message lacks it, or what happened instead."""
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        if not exc.sqlstate:
            return query_error(_message(exc))
        return exc.sqlstate + (label if phrase in str(exc) else "")
    return "no error"


def _gets(log):
    """-> the request paths of the GETs the fixture logged, or a query_error()."""
    try:
        lines = pathlib.Path(log).read_text(errors="replace").splitlines()
    except OSError as exc:
        return query_error(f"request log unreadable: {exc}")
    return [f[1] for f in (l.split() for l in lines) if len(f) > 1 and f[0] == "GET"]


def _sent(log, want, unwanted=None):
    """-> "yes" when the server received a GET for `want` (and none for `unwanted`),
    else the paths it did receive."""
    paths = _gets(log)
    if not isinstance(paths, list):
        return paths
    if want in paths and (unwanted is None or unwanted not in paths):
        return "yes"
    return f"no: the server received {paths}"


def _lists(log, want):
    """-> "<n> to /, <m> elsewhere": the request paths of the logged GETs whose query
    carries `want` (list-type=2 for ListObjectsV2, uploads for ListMultipartUploads),
    as the shell's lists() counts them, or a query_error()."""
    try:
        lines = pathlib.Path(log).read_text(errors="replace").splitlines()
    except OSError as exc:
        return query_error(f"request log unreadable: {exc}")
    root = other = 0
    for f in (l.split() for l in lines):
        if len(f) > 2 and f[0] == "GET" and any(
                p == want or p.startswith(want + "=") for p in f[2].split("&")):
            if f[1] == "/":
                root += 1
            else:
                other += 1
    return f"{root} to /, {other} elsewhere"


def _mv_open(work):
    """-> how many uploads at pgc-bucket/mv/part-0000.parquet the fixture's
    __open_uploads__ lists, as the shell's mv_open counts them (0 when absent)."""
    try:
        lines = (work / "__open_uploads__").read_text(errors="replace").splitlines()
    except OSError:
        return 0
    return sum(1 for l in lines if l == f"{BUCKET}/mv/part-0000.parquet")


def _mv_count(log, method, path, query_key):
    """-> how many logged `method` requests went to `path` with `query_key` in their
    query, as the shell's mv_parts and mv_aborts count them."""
    try:
        lines = pathlib.Path(log).read_text(errors="replace").splitlines()
    except OSError:
        return 0
    return sum(1 for f in (l.split() for l in lines)
               if len(f) > 2 and f[0] == method and f[1] == path and query_key in f[2])


def _error_has(dsn, sqls, phrase):
    """Run `sqls` on one connection -> "1" when the last one raised with `phrase` in
    its message, else what happened instead."""
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            for sql in sqls:
                conn.execute(sql)
    except psycopg.Error as exc:
        return "1" if phrase in str(exc) else query_error(_message(exc))
    return "no error"


def _resolves(host):
    try:
        return bool(socket.getaddrinfo(host, None))
    except OSError:
        return False


def _write_parquet(path, ids):
    """Write one int8 column `id` with a python3 child, as the shell's heredoc does; the
    first test names the shell's pyarrow skip through require_module. -> None, or the
    decline when pyarrow is absent. Any other failure raises."""
    w = subprocess.run([sys.executable, "-c",
                        "import sys, pyarrow as pa, pyarrow.parquet as pq\n"
                        "pq.write_table(pa.table({'id': pa.array([int(i) for i in sys.argv[2:]], pa.int64())}),"
                        " sys.argv[1])", str(path)] + [str(i) for i in ids], capture_output=True, text=True)
    if w.returncode == 0:
        return None
    if "No module named 'pyarrow" in w.stderr:
        return ("MISSING_DEPENDENCY", "pyarrow is not installed")
    raise RuntimeError(f"writing {path} failed: {w.stderr.strip()}")


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
    """Restart the private cluster with env, and wait for it as the shell's
    pg_restart_env does: up to 15 s for SELECT 1."""
    _pg_ctl(cluster, ["-D", str(datadir), "-m", "fast", "-w", "stop"], {})
    started = _pg_ctl(cluster, ["-D", str(datadir), "-l", log, "-w", "start"], env)
    for _ in range(30):
        if started.returncode == 0 and _row(cluster.dsn(), "SELECT 1") == "1":
            return
        time.sleep(0.5)
    raise RuntimeError(f"cluster did not come back after env restart: {started.stderr.strip()}")


@pytest.fixture(scope="module")
def osa(pgc_cluster):
    if not _resolves(f"{BUCKET}.s3.local"):
        yield {"absent": ("UNAVAILABLE_ENDPOINT", f"{BUCKET}.s3.local does not resolve")}
        return
    from pgc_cluster import Cluster, pick_port

    work = pathlib.Path(tempfile.mkdtemp(prefix="pgc-osa-"))
    os.chmod(work, 0o777)
    (work / BUCKET / "dir sp").mkdir(parents=True)
    (work / BUCKET / "pfx").mkdir()
    wrote = _write_parquet(work / BUCKET / "vh.parquet", [1, 2, 3, 4]) or \
        _write_parquet(work / BUCKET / ENCKEY, [5, 6, 7]) or \
        _write_parquet(work / BUCKET / "pfx" / "a.parquet", [10, 20]) or \
        _write_parquet(work / BUCKET / "pfx" / "b.parquet", [30])
    if wrote is not None:
        shutil.rmtree(work, ignore_errors=True)
        yield {"absent": wrote}
        return

    s3_port = pick_port(29)
    out, s3log = work / "s3_server.out", str(work / "s3.log")
    srv = subprocess.Popen([sys.executable, str(TEST / "objstore_http_server.py"), "--dir", str(work),
                            "--port", str(s3_port), "--log", s3log,
                            "--sigv4-key", AKID, "--sigv4-secret", SECRET, "--sigv4-region", REGION,
                            "--virtual-host-base", "s3.local"],
                           stdout=open(out, "w"), stderr=subprocess.STDOUT)
    for _ in range(50):
        if out.is_file() and "READY" in out.read_text(errors="replace"):
            break
        time.sleep(0.1)

    datadir = work / "data"
    datadir.mkdir()
    log = str(work / "server.log")
    cluster = Cluster(pgc_cluster.pg_config, "osa", datadir, pick_port(28))
    r = {"absent": None, "server up": out.read_text(errors="replace").count("READY") if out.is_file() else 0}

    def logged(key, *sqls):
        pathlib.Path(s3log).write_text("")
        r[key] = _row(dsn, *sqls)

    try:
        cluster.initdb()
        with open(datadir / "postgresql.conf", "a") as fh:
            fh.write("\npgcolumnar.objstore_allowed_endpoints='s3.local'\n"
                     "pgcolumnar.objstore_part_size=262144\n")
        started = _pg_ctl(cluster, ["-D", str(datadir), "-l", log, "-w", "start"], {})
        if started.returncode != 0:
            raise RuntimeError(f"the private cluster did not start: {started.stderr}")
        dsn = cluster.dsn()
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")

        creds = {"AWS_ACCESS_KEY_ID": AKID, "AWS_SECRET_ACCESS_KEY": SECRET, "AWS_REGION": REGION}
        _restart(cluster, datadir, log, dict(creds, AWS_ENDPOINT_URL=f"http://s3.local:{s3_port}"))
        vh, enc = f"('s3://{BUCKET}/vh.parquet') AS t(id int8)", f"('s3://{BUCKET}/{ENCKEY}') AS t(id int8)"
        logged("path read", RD + vh)
        r["path sent"] = _sent(s3log, f"/{BUCKET}/vh.parquet")
        logged("path enc read", RD + enc)
        r["path enc sent"] = _sent(s3log, f"/{BUCKET}/dir%20sp/a%2Bb%3Dc.parquet")

        logged("gs read", RD + f"('gs://{BUCKET}/vh.parquet') AS t(id int8)")
        _row(dsn, "CREATE TABLE gct (id int8) USING pgcolumnar",
             "INSERT INTO gct VALUES (5),(6),(7) RETURNING 1")
        r["gs write"] = _row(dsn, f"SELECT (pgcolumnar.export_parquet('gct', 'gs://{BUCKET}/out.parquet') > 0)::text")
        r["gs readback"] = _row(dsn, RD + f"('gs://{BUCKET}/out.parquet') AS t(id int8)")

        logged("vhost read", VIRTUAL, RD + vh)
        r["vhost sent"] = _sent(s3log, "/vh.parquet", unwanted=f"/{BUCKET}/vh.parquet")
        logged("vhost enc read", VIRTUAL, RD + enc)
        r["vhost enc sent"] = _sent(s3log, "/dir%20sp/a%2Bb%3Dc.parquet")

        pfx, pat = f"('s3://{BUCKET}/pfx/') AS t(id int8)", f"('s3://{BUCKET}/pfx/*.parquet') AS t(id int8)"
        logged("path prefix read", RD + pfx)
        r["path prefix list"] = _lists(s3log, "list-type=2")
        logged("vhost prefix read", VIRTUAL, RD + pfx)
        r["vhost prefix list"] = _lists(s3log, "list-type=2")
        logged("vhost pattern read", VIRTUAL, RD + pat)
        r["vhost pattern list"] = _lists(s3log, "list-type=2")

        _row(dsn, "CREATE TABLE mp_big (id int8, pad text) USING pgcolumnar",
             "INSERT INTO mp_big SELECT g, md5(g::text)||md5((g+1)::text) "
             "FROM generate_series(1,60000) g RETURNING 1")
        pathlib.Path(s3log).write_text("")
        (work / "__part_status__").write_text("500 2\n")
        r["vhost export fails"] = _error_has(
            dsn, [VIRTUAL, f"SELECT pgcolumnar.parallel_export_parquet('mp_big'::regclass, 's3://{BUCKET}/mp', 1)"],
            "parallel_export_parquet worker 0 failed")
        (work / "__part_status__").unlink()
        r["vhost uploads list"] = _lists(s3log, "uploads")

        # A worker terminated while its CreateMultipartUpload is unanswered never
        # learns the upload id: only the dispatcher's cleanup can abort that upload.
        pathlib.Path(s3log).write_text("")
        (work / "__uploads_delay__").write_text("20\n")
        bg = subprocess.Popen(
            [os.path.join(cluster.bindir, "psql"), "-h", "127.0.0.1", "-p", str(cluster.port),
             "-U", "postgres", "-d", "postgres", "-Atq", "-c", VIRTUAL,
             "-c", f"SELECT pgcolumnar.parallel_export_parquet('mp_big'::regclass, 's3://{BUCKET}/mv', 1)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(300):
            if _mv_open(work) == 1:
                break
            time.sleep(0.1)
        r["mv premise"] = f"{_mv_open(work)} open, {_mv_count(s3log, 'PUT', f'/{BUCKET}/mv/part-0000.parquet', 'partNumber=')} parts"
        _row(dsn, "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
                  "WHERE backend_type = 'pgcolumnar parallel_export worker'")
        try:
            bg.wait(timeout=120)
        except subprocess.TimeoutExpired:
            bg.kill()
            bg.wait()
        (work / "__uploads_delay__").unlink()
        r["mv aborted"] = (f"{_mv_open(work)} open, "
                           f"{_mv_count(s3log, 'DELETE', '/mv/part-0000.parquet', 'uploadId=')}"
                           " aborted by DELETE /mv/part-0000.parquet?uploadId=")
        logged("path again read", PATH, RD + vh)
        r["path again sent"] = _sent(s3log, f"/{BUCKET}/vh.parquet")

        _restart(cluster, datadir, log, creds)
        r["s3 no endpoint"] = _refusal(dsn, RD + vh, "AWS_ENDPOINT_URL", " naming AWS_ENDPOINT_URL")
        r["gs no endpoint"] = _refusal(
            dsn, RD + f"('gs://{BUCKET}/vh.parquet') AS t(id int8)",
            'endpoint "storage.googleapis.com:443" is not in pgcolumnar.objstore_allowed_endpoints',
            " by the allow-list naming storage.googleapis.com:443")
        yield r
    finally:
        _pg_ctl(cluster, ["-D", str(datadir), "-m", "immediate", "-w", "stop"], {})
        srv.kill()
        srv.wait()
        shutil.rmtree(work, ignore_errors=True)


def _declined(osa, expect):
    if osa["absent"] is None:
        return False
    expect.cannot_run(*osa["absent"])
    return True


def test_the_fixture_server_is_up(osa, expect):
    """The shell skips the whole suite without pyarrow, or where the virtual-host name
    does not resolve, under these names."""
    if expect.require_module("pyarrow", name="pyarrow is required to write the fixture") is None:
        return
    if osa["absent"] is not None:
        expect.cannot_run(*osa["absent"],
                          name="pgc-bucket.s3.local does not resolve; add it to /etc/hosts (-> 127.0.0.1)")
        return
    expect.text(str(osa["server up"]), "1", "premise: fixture up")


def test_an_s3_path_style_read(osa, expect):
    if _declined(osa, expect):
        return
    expect.text(osa["path read"], "4 10", "s3:// path-style read")


def test_path_style_puts_the_bucket_in_the_request_path(osa, expect):
    if _declined(osa, expect):
        return
    expect.text(osa["path sent"], "yes", "path-style put the bucket in the request path")


def test_a_key_needing_encoding_reads_back_path_style(osa, expect):
    if _declined(osa, expect):
        return
    expect.text(osa["path enc read"], "3 18", "a key with a space, '+' and '=' reads back (path-style)")


def test_the_path_style_request_path_is_the_key_encoded_once(osa, expect):
    if _declined(osa, expect):
        return
    expect.text(osa["path enc sent"], "yes", "and its path-style request path is the key URI-encoded once")


def test_a_gs_read_round_trips(osa, expect):
    if _declined(osa, expect):
        return
    expect.text(osa["gs read"], "4 10", "gs:// read round-trips (scheme handled, HMAC creds reused)")


def test_a_gs_write_round_trips(osa, expect):
    if _declined(osa, expect):
        return
    expect.text(osa["gs write"], "true", "gs:// write round-trips")


def test_the_gs_written_object_reads_back(osa, expect):
    if _declined(osa, expect):
        return
    expect.text(osa["gs readback"], "3 18", "gs:// read-back of the written object")


def test_a_virtual_host_read_equals_the_path_style_read(osa, expect):
    if _declined(osa, expect):
        return
    expect.text(osa["vhost read"], "4 10",
                "virtual-host read == path-style read (same object, different addressing)")


def test_virtual_host_puts_the_key_alone_in_the_path(osa, expect):
    if _declined(osa, expect):
        return
    expect.text(osa["vhost sent"], "yes", "virtual-host put the KEY ALONE in the path (no bucket segment)")


def test_a_key_needing_encoding_reads_back_virtual_host(osa, expect):
    if _declined(osa, expect):
        return
    expect.text(osa["vhost enc read"], "3 18", "a key with a space, '+' and '=' reads back (virtual-host)")


def test_the_virtual_host_request_path_is_the_key_alone_encoded_once(osa, expect):
    if _declined(osa, expect):
        return
    expect.text(osa["vhost enc sent"], "yes",
                "and its virtual-host request path is the key alone, URI-encoded once")


def test_an_s3_prefix_read_path_style(osa, expect):
    if _declined(osa, expect):
        return
    expect.text(osa["path prefix read"], "3 60", "s3:// prefix read, path-style")


def test_the_path_style_list_puts_the_bucket_in_the_path(osa, expect):
    if _declined(osa, expect):
        return
    expect.text(osa["path prefix list"], "0 to /, 1 elsewhere",
                "and its path-style LIST put the bucket in the request path")


def test_an_s3_prefix_read_virtual_host(osa, expect):
    """#1783: the LIST went to /pgc-bucket on the bucket's host name, a GetObject of
    the key "pgc-bucket" to S3, and the read failed with HTTP 404."""
    if _declined(osa, expect):
        return
    expect.text(osa["vhost prefix read"], "3 60", "s3:// prefix read, virtual-host")


def test_the_virtual_host_list_goes_to_the_bucket_root(osa, expect):
    if _declined(osa, expect):
        return
    expect.text(osa["vhost prefix list"], "1 to /, 0 elsewhere",
                "and its virtual-host LIST went to the bucket root")


def test_an_s3_pattern_read_virtual_host(osa, expect):
    if _declined(osa, expect):
        return
    expect.text(osa["vhost pattern read"], "3 60", "s3:// pattern read, virtual-host")


def test_the_pattern_read_virtual_host_list_goes_to_the_bucket_root(osa, expect):
    if _declined(osa, expect):
        return
    expect.text(osa["vhost pattern list"], "1 to /, 0 elsewhere",
                "and the pattern read's virtual-host LIST went to the bucket root")


def test_the_virtual_host_export_fails_in_a_worker(osa, expect):
    if _declined(osa, expect):
        return
    expect.text(osa["vhost export fails"], "1", "premise: the virtual-host export fails in a worker")


def test_the_virtual_host_upload_cleanup_lists_at_the_bucket_root(osa, expect):
    """#1783: the dispatcher's ListMultipartUploads carried the same bucket segment."""
    if _declined(osa, expect):
        return
    expect.text(osa["vhost uploads list"], "1 to /, 0 elsewhere",
                "and its virtual-host upload cleanup listed the uploads at the bucket root")


def test_a_terminated_workers_upload_is_open_and_unanswered(osa, expect):
    if _declined(osa, expect):
        return
    expect.text(osa["mv premise"], "1 open, 0 parts",
                "premise: a worker's upload is open and its id unanswered when the worker is terminated")


def test_the_virtual_host_upload_cleanup_aborts_the_open_upload(osa, expect):
    """#1783: the cleanup's listing got a 404, which the cleanup discards, so the
    upload the terminated worker left stayed open."""
    if _declined(osa, expect):
        return
    expect.text(osa["mv aborted"], "0 open, 1 aborted by DELETE /mv/part-0000.parquet?uploadId=",
                "and the virtual-host upload cleanup aborted the upload the worker left open")


def test_addressing_path_puts_the_bucket_back_in_the_path(osa, expect):
    if _declined(osa, expect):
        return
    expect.text(osa["path again read"], "4 10",
                "removal proof: with addressing=path the bucket is back in the path")


def test_the_request_path_carries_the_bucket_again(osa, expect):
    if _declined(osa, expect):
        return
    expect.text(osa["path again sent"], "yes", "and the request path carried the bucket again")


def test_s3_with_no_endpoint_demands_one(osa, expect):
    if _declined(osa, expect):
        return
    expect.text(osa["s3 no endpoint"], "28000 naming AWS_ENDPOINT_URL",
                "s3:// with no endpoint demands one (names AWS_ENDPOINT_URL)")


def test_gs_with_no_endpoint_goes_to_storage_googleapis_com(osa, expect):
    if _declined(osa, expect):
        return
    expect.text(osa["gs no endpoint"], "42501 by the allow-list naming storage.googleapis.com:443",
                "gs:// with no endpoint goes to storage.googleapis.com (the allow-list refuses that host by name)")
