"""A second cancel during a remote export's cleanup (#1741): the pytest twin of
`test/objstore_export_double_cancel.sh`, every check by the shell's name.

A parallel_export_parquet to s3:// is cancelled while its workers upload, and again
while the dispatcher's cleanup DELETE is held in flight by the fixture server's
__delete_delay__ hook. The export must report its own cancel, 57014, and the server log
must not gain "errstart was not called": the module's best-effort catch used to empty
the whole error stack, the first cancel's error included.

A PRIVATE CLUSTER, because the postmaster starts with AWS_* in its environment, which
the shared cluster cannot do: every AWS_* variable of this process is removed and the
fixture's own are set. A backend that died does not pass as one that survived: survival also reads
the private cluster's server log for killed backends.
"""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import psycopg
import pytest

from pgc_vacuity import query_error

TEST = pathlib.Path(__file__).resolve().parents[1]
AKID, SECRET, REGION, BUCKET = "PGCTESTKEYID", "pgctest-secret-shhh", "pgc-test-1", "pgc-bucket"


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _value(dsn, sql):
    """-> the first column of the first row as text, or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    if row is None or row[0] is None:
        return query_error(f"no value from {sql}")
    return str(row[0])


def _logcount(log, phrase):
    try:
        return pathlib.Path(log).read_text(errors="replace").count(phrase)
    except OSError as exc:
        return query_error(f"server log unreadable: {exc}")


def _survived(dsn, log, before):
    up, now = _value(dsn, "SELECT 1"), _logcount(log, "terminated by signal")
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


def _lines(path, prefix):
    try:
        return sum(1 for ln in pathlib.Path(path).read_text(errors="replace").splitlines()
                   if ln.startswith(prefix))
    except OSError:
        return 0


def _wait_for(path, prefix, tries):
    for _ in range(tries):
        if _lines(path, prefix) >= 1:
            return "yes"
        time.sleep(0.1)
    return "no"


def _cancel(dsn):
    _value(dsn, "SELECT count(pg_cancel_backend(pid)) FROM pg_stat_activity "
                "WHERE query LIKE '%parallel_export_parquet%' AND backend_type = 'client backend' "
                "AND pid <> pg_backend_pid()")


@pytest.fixture(scope="module")
def dc(pgc_cluster):
    from pgc_cluster import Cluster, pick_port

    work = pathlib.Path(tempfile.mkdtemp(prefix="pgc-dcancel-"))
    os.chmod(work, 0o777)
    (work / BUCKET).mkdir()
    (work / BUCKET).chmod(0o777)
    s3log = work / "dc.log"
    s3_port = pick_port(29)
    out = work / "dc_server.out"
    srv = subprocess.Popen([sys.executable, str(TEST / "objstore_http_server.py"), "--dir", str(work),
                            "--port", str(s3_port), "--log", str(s3log),
                            "--sigv4-key", AKID, "--sigv4-secret", SECRET, "--sigv4-region", REGION],
                           stdout=open(out, "w"), stderr=subprocess.STDOUT)
    for _ in range(50):
        if out.is_file() and "READY" in out.read_text(errors="replace"):
            break
        time.sleep(0.1)

    datadir = work / "data"
    datadir.mkdir()
    log = str(work / "server.log")
    cluster = Cluster(pgc_cluster.pg_config, "dcancel", datadir, pick_port(28))
    r = {"server up": out.read_text(errors="replace").count("READY") if out.is_file() else 0}
    try:
        cluster.initdb()
        with open(datadir / "postgresql.conf", "a") as fh:
            fh.write("\npgcolumnar.objstore_allowed_endpoints='127.0.0.1'\nmax_worker_processes=16\n")
        aws = {"AWS_ENDPOINT_URL": f"http://127.0.0.1:{s3_port}", "AWS_ACCESS_KEY_ID": AKID,
               "AWS_SECRET_ACCESS_KEY": SECRET, "AWS_REGION": REGION}
        started = _pg_ctl(cluster, ["-D", str(datadir), "-l", log, "-w", "start"], aws)
        if started.returncode != 0:
            raise RuntimeError(f"the private cluster did not start: {started.stderr}")
        dsn = cluster.dsn()
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            c.execute("CREATE TABLE dc_big (id int8, pad text) USING pgcolumnar")
            c.execute("SELECT pgcolumnar.set_options('dc_big', stripe_row_limit => 4000)")
            c.execute("INSERT INTO dc_big SELECT g, md5(g::text)||md5((g+1)::text) "
                      "FROM generate_series(1,3000000) g")

        s3log.write_text("")
        delay = work / "__delete_delay__"
        delay.write_text("2\n")
        delay.chmod(0o644)
        crashes0 = _logcount(log, "terminated by signal")
        errstart0 = _logcount(log, "errstart was not called")
        got = {}

        def export():
            try:
                with psycopg.connect(dsn, autocommit=True) as conn:
                    conn.execute("SET pgcolumnar.objstore_part_size = 262144")
                    conn.execute(f"SELECT pgcolumnar.parallel_export_parquet('dc_big'::regclass, "
                                 f"'s3://{BUCKET}/dc', 4)")
                got["outcome"] = "no error: the export completed"
            except psycopg.Error as exc:
                got["outcome"] = f"{exc.sqlstate}: {exc.diag.message_primary}"

        t = threading.Thread(target=export)
        t.start()
        r["wrote"] = _wait_for(s3log, f"PUT /{BUCKET}/dc/part-", 300)
        _cancel(dsn)
        r["deleting"] = _wait_for(s3log, f"DELETE /{BUCKET}/dc/", 100)
        _cancel(dsn)
        t.join(300)
        delay.unlink()
        r["outcome"] = got.get("outcome", query_error("the export did not return"))
        errstart1 = _logcount(log, "errstart was not called")
        r["errstart"] = str(errstart1 - errstart0) if isinstance(errstart0, int) and isinstance(errstart1, int) \
            else query_error("server log unreadable")
        prefix = work / BUCKET / "dc"
        r["left"] = sum(1 for p in prefix.iterdir() if p.name.startswith("part-") and p.name.endswith(".parquet")) \
            if prefix.is_dir() else 0
        r["survived"] = _survived(dsn, log, crashes0)
        yield r
    finally:
        _pg_ctl(cluster, ["-D", str(datadir), "-m", "immediate", "-w", "stop"], {})
        srv.kill()
        srv.wait()
        shutil.rmtree(work, ignore_errors=True)


def test_the_double_cancel_sigv4_fixture_server_is_up(dc, expect):
    expect.text(str(dc["server up"]), "1", "premise: sigv4 fixture server is up")


def test_a_worker_was_uploading_before_the_first_cancel(dc, expect):
    expect.text(dc["wrote"], "yes", "premise: a worker was uploading before the first cancel")


def test_the_cleanup_was_deleting_when_the_second_cancel_landed(dc, expect):
    expect.text(dc["deleting"], "yes",
                "premise: the first cancel's cleanup was deleting when the second cancel landed")


def test_the_export_reports_its_own_cancel(dc, expect):
    expect.text(dc["outcome"], "57014: canceling statement due to user request",
                "double cancel: the export reports its own cancel (57014)")


def test_the_server_log_has_no_errstart_was_not_called(dc, expect):
    expect.text(dc["errstart"], "0", "double cancel: the server log has no 'errstart was not called'")


def test_no_completed_object_remains_at_the_prefix(dc, expect):
    expect.num(dc["left"], 0, "double cancel: no completed object remains at the prefix")


def test_the_server_survived(dc, expect):
    expect.text(dc["survived"], "1", "double cancel: the server survived")
