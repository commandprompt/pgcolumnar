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
    """Restart the private cluster with env and wait for it, as pg_restart_env does."""
    _pg_ctl(cluster, ["-D", str(datadir), "-m", "fast", "-w", "stop"], {})
    started = _pg_ctl(cluster, ["-D", str(datadir), "-l", log, "-w", "start"], env)
    for _ in range(30):
        if started.returncode == 0 and _row(cluster.dsn(), "SELECT 1") == "1":
            return
        time.sleep(0.5)
    raise RuntimeError(f"cluster did not come back after env restart: {started.stderr.strip()}")


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
        _restart(cluster, datadir, log, {"AWS_ENDPOINT_URL": f"http://127.0.0.1:{s3_port}",
                                         "AWS_ACCESS_KEY_ID": AKID, "AWS_SECRET_ACCESS_KEY": SECRET,
                                         "AWS_REGION": REGION})
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
