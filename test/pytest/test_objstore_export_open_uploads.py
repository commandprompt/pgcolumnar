"""A cancelled remote export leaves none of its uploads open (#1743): the pytest twin
of `test/objstore_export_open_uploads.sh`, every check by the shell's name.

A parallel_export_parquet to s3:// is ended while its workers' multipart uploads are
open. The dispatcher's cleanup must list the uploads under the prefix and abort each
one whose key is one of the export's own part names, and leave the two seeded foreign
uploads under the same prefix (part-0004.parquet and notes.parquet) open. Five arms:
the dispatcher is cancelled while every worker sends parts (__part_delay__ holds each
part's answer); it is cancelled while every worker waits for the answer to
CreateMultipartUpload (__uploads_delay__ holds it after the upload exists); the
workers are terminated in that same window; the dispatcher is cancelled there and
cancelled again while the first cancel's cleanup DELETEs a part key (__delete_delay__
holds each DELETE, so the second cancel lands in the module's best-effort catch, #1741);
and the dispatcher is cancelled there
while the store does not answer the listing (__list_uploads_delay__), where the
cleanup's bounded wait must give up and the export still report its own cancel,
promptly.

Then the messages name what the caller gave, never the dummy key "s3://bucket/_" a
bucket-level request is resolved from (#1869). Worker 0's part PUT is refused
(__put_status__), so the run fails and the cleanup lists the prefix's uploads, which
the store refuses (__list_uploads_status__): the dispatcher LOGs what the cleanup
discarded, and that line must name the prefix it listed. And with no region in the
postmaster's environment, the export's endpoint check and read_parquet's directory
listing must each name the prefix in the region refusal.

A PRIVATE CLUSTER, because the postmaster starts with AWS_* in its environment, which
the shared cluster cannot do: every AWS_* variable of this process is removed and the
fixture's own are set. A backend that died does not pass as one that survived:
survival also reads the private cluster's server log for killed backends.
"""
import os
import pathlib
import re
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
OWN = re.compile(r"^part-000[0-3]\.parquet$")


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


def _open_keys(work, prefix):
    """The open uploads under bucket/prefix/ as names relative to it, or None when the
    server's record is missing."""
    try:
        lines = (work / "__open_uploads__").read_text(errors="replace").splitlines()
    except OSError:
        return None
    head = f"{BUCKET}/{prefix}/"
    return [ln[len(head):] for ln in lines if ln.startswith(head)]


def _own_open(work, prefix):
    keys = _open_keys(work, prefix)
    if keys is None:
        return query_error("the server's __open_uploads__ record is missing")
    return sum(1 for k in keys if OWN.match(k))


def _foreign_open(work, prefix):
    keys = _open_keys(work, prefix)
    if keys is None:
        return query_error("the server's __open_uploads__ record is missing")
    return " ".join(sorted(f"{BUCKET}/{prefix}/{k}" for k in keys if not OWN.match(k)))


def _part_puts(s3log, prefix):
    pat = re.compile(rf"^PUT /{BUCKET}/{prefix}/part-[0-9]+\.parquet .*partNumber=")
    try:
        return sum(1 for ln in pathlib.Path(s3log).read_text(errors="replace").splitlines()
                   if pat.match(ln))
    except OSError:
        return 0


def _wait_unanswered(work, s3log, prefix):
    for _ in range(300):
        if _own_open(work, prefix) == 4:
            break
        time.sleep(0.1)
    return f"{_own_open(work, prefix)} open, {_part_puts(s3log, prefix)} parts"


def _wait_sending(work, s3log, prefix):
    for _ in range(300):
        if _own_open(work, prefix) == 4 and _part_puts(s3log, prefix) >= 4:
            break
        time.sleep(0.1)
    if _part_puts(s3log, prefix) >= 4:
        return f"{_own_open(work, prefix)} open, sending parts"
    return f"{_own_open(work, prefix)} open, {_part_puts(s3log, prefix)} parts"


def _completed_parts(work, prefix):
    d = work / BUCKET / prefix
    return sum(1 for p in d.iterdir() if p.name.startswith("part-") and p.name.endswith(".parquet")) \
        if d.is_dir() else 0


def _start_export(dsn, prefix, got):
    def export():
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute(f"SELECT pgcolumnar.parallel_export_parquet('ou_big'::regclass, "
                             f"'s3://{BUCKET}/{prefix}', 4)")
            got["outcome"] = "no error: the export completed"
        except psycopg.Error as exc:
            got["outcome"] = f"{exc.sqlstate}: {exc.diag.message_primary}"
    t = threading.Thread(target=export)
    t.start()
    return t


def _outcome(dsn, sql):
    """-> "no error" or "SQLSTATE: message" for a statement expected to fail."""
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            conn.execute(sql)
        return "no error: the statement completed"
    except psycopg.Error as exc:
        return f"{exc.sqlstate}: {exc.diag.message_primary}"


REFUSED_LISTING = re.compile(r'could not clean up the open uploads under "[^"]*": '
                             r'columnar: access to "([^"]*)" was denied')
REGION_REFUSAL = re.compile(r'^columnar: reading "([^"]*)" requires a region option')


def _cleanup_named(log, offset):
    """-> the URL the cleanup's LOG line says was denied, from the server log past
    byte `offset`, or a query_error() saying what was there."""
    try:
        with open(log, "rb") as fh:
            fh.seek(offset)
            tail = fh.read().decode(errors="replace")
    except OSError as exc:
        return query_error(f"server log unreadable: {exc}")
    m = REFUSED_LISTING.search(tail)
    if m is None:
        return query_error(f"no cleanup LOG line naming an access denial past byte {offset}")
    return m.group(1)


def _region_named(dsn, sql):
    """-> the URL the region refusal names, or a query_error() saying what happened."""
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            conn.execute(sql)
        return query_error("no region refusal: the statement completed")
    except psycopg.Error as exc:
        m = REGION_REFUSAL.match(exc.diag.message_primary or "")
        return m.group(1) if m else query_error(f"no region refusal: {_message(exc)}")


def _cancel(dsn):
    _value(dsn, "SELECT count(pg_cancel_backend(pid)) FROM pg_stat_activity "
                "WHERE query LIKE '%parallel_export_parquet%' AND backend_type = 'client backend' "
                "AND pid <> pg_backend_pid()")


def _terminate_workers(dsn):
    _value(dsn, "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
                "WHERE backend_type = 'pgcolumnar parallel_export worker'")


@pytest.fixture(scope="module")
def ou(pgc_cluster):
    from pgc_cluster import Cluster, pick_port

    work = pathlib.Path(tempfile.mkdtemp(prefix="pgc-openup-"))
    os.chmod(work, 0o777)
    (work / BUCKET).mkdir()
    (work / BUCKET).chmod(0o777)
    s3log = work / "ou.log"
    s3_port = pick_port(36)
    out = work / "ou_server.out"
    seeds = []
    for p in ("op", "oc", "ot", "od"):
        seeds += ["--seed-upload", f"{BUCKET}/{p}/part-0004.parquet",
                  "--seed-upload", f"{BUCKET}/{p}/notes.parquet"]
    srv = subprocess.Popen([sys.executable, str(TEST / "objstore_http_server.py"), "--dir", str(work),
                            "--port", str(s3_port), "--log", str(s3log),
                            "--sigv4-key", AKID, "--sigv4-secret", SECRET, "--sigv4-region", REGION,
                            "--list-page-size", "2"] + seeds,
                           stdout=open(out, "w"), stderr=subprocess.STDOUT)
    for _ in range(50):
        if out.is_file() and "READY" in out.read_text(errors="replace"):
            break
        time.sleep(0.1)

    datadir = work / "data"
    datadir.mkdir()
    log = str(work / "server.log")
    cluster = Cluster(pgc_cluster.pg_config, "openup", datadir, pick_port(37))
    r = {"server up": out.read_text(errors="replace").count("READY") if out.is_file() else 0}
    if _open_keys(work, "oc") is None:
        r["seeded"] = query_error("the server's __open_uploads__ record is missing")
    else:
        r["seeded"] = " ".join(_foreign_open(work, p) for p in ("op", "oc", "ot"))
    uploads_delay = work / "__uploads_delay__"
    part_delay = work / "__part_delay__"
    list_delay = work / "__list_uploads_delay__"
    delete_delay = work / "__delete_delay__"
    try:
        cluster.initdb()
        # The part size goes in the configuration file: background workers do not
        # inherit the dispatcher's session settings.
        with open(datadir / "postgresql.conf", "a") as fh:
            fh.write("\npgcolumnar.objstore_allowed_endpoints='127.0.0.1'\nmax_worker_processes=16\n"
                     "pgcolumnar.objstore_part_size=262144\n")
        aws = {"AWS_ENDPOINT_URL": f"http://127.0.0.1:{s3_port}", "AWS_ACCESS_KEY_ID": AKID,
               "AWS_SECRET_ACCESS_KEY": SECRET, "AWS_REGION": REGION}
        started = _pg_ctl(cluster, ["-D", str(datadir), "-l", log, "-w", "start"], aws)
        if started.returncode != 0:
            raise RuntimeError(f"the private cluster did not start: {started.stderr}")
        dsn = cluster.dsn()
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            c.execute("CREATE TABLE ou_big (id int8, pad text) USING pgcolumnar")
            c.execute("SELECT pgcolumnar.set_options('ou_big', stripe_row_limit => 4000)")
            c.execute("INSERT INTO ou_big SELECT g, md5(g::text)||md5((g+1)::text) "
                      "FROM generate_series(1,400000) g")
        crashes0 = _logcount(log, "terminated by signal")

        # parts: the dispatcher is cancelled while every worker sends parts
        s3log.write_text("")
        part_delay.write_text("20\n")
        got = {}
        t = _start_export(dsn, "op", got)
        r["op sending"] = _wait_sending(work, s3log, "op")
        _cancel(dsn)
        t.join(300)
        part_delay.unlink()
        r["op outcome"] = got.get("outcome", query_error("the export did not return"))
        r["op own"] = _own_open(work, "op")
        r["op foreign"] = _foreign_open(work, "op")
        r["op survived"] = _survived(dsn, log, crashes0)

        # cancel: the dispatcher is cancelled while every POST ?uploads is held
        s3log.write_text("")
        uploads_delay.write_text("20\n")
        got = {}
        t = _start_export(dsn, "oc", got)
        r["oc unanswered"] = _wait_unanswered(work, s3log, "oc")
        _cancel(dsn)
        t.join(300)
        uploads_delay.unlink()
        r["oc outcome"] = got.get("outcome", query_error("the export did not return"))
        r["oc own"] = _own_open(work, "oc")
        r["oc foreign"] = _foreign_open(work, "oc")
        r["oc left"] = _completed_parts(work, "oc")
        r["oc survived"] = _survived(dsn, log, crashes0)

        # terminate: the workers are terminated while every POST ?uploads is held
        s3log.write_text("")
        uploads_delay.write_text("20\n")
        got = {}
        t = _start_export(dsn, "ot", got)
        r["ot unanswered"] = _wait_unanswered(work, s3log, "ot")
        _terminate_workers(dsn)
        t.join(300)
        uploads_delay.unlink()
        outcome = got.get("outcome", query_error("the export did not return"))
        r["ot outcome"] = re.sub(r"worker [0-9]+ failed", "worker N failed", outcome)
        r["ot own"] = _own_open(work, "ot")
        r["ot foreign"] = _foreign_open(work, "ot")
        r["ot survived"] = _survived(dsn, log, crashes0)

        # double cancel: a second cancel lands in the first cancel's part DELETE
        s3log.write_text("")
        uploads_delay.write_text("20\n")
        delete_delay.write_text("3\n")
        errstart0 = _logcount(log, "errstart was not called")
        got = {}
        t = _start_export(dsn, "od", got)
        r["od unanswered"] = _wait_unanswered(work, s3log, "od")
        _cancel(dsn)
        # The cleanup's first part DELETE (no query: not an upload abort) is logged on
        # arrival and answered 3 s later.
        deleting = re.compile(rf"^DELETE /{BUCKET}/od/part-[0-9]+\.parquet - ")
        r["od deleting"] = "no"
        for _ in range(100):
            if any(deleting.match(ln) for ln in s3log.read_text(errors="replace").splitlines()):
                r["od deleting"] = "yes"
                break
            time.sleep(0.1)
        _cancel(dsn)
        # Later DELETEs, the upload aborts among them, are answered at once.
        delete_delay.unlink()
        t.join(300)
        uploads_delay.unlink()
        r["od outcome"] = got.get("outcome", query_error("the export did not return"))
        errstart1 = _logcount(log, "errstart was not called")
        r["od errstart"] = str(errstart1 - errstart0) \
            if isinstance(errstart0, int) and isinstance(errstart1, int) \
            else query_error(f"server log unreadable: {errstart0} / {errstart1}")
        r["od own"] = _own_open(work, "od")
        r["od foreign"] = _foreign_open(work, "od")
        r["od survived"] = _survived(dsn, log, crashes0)

        # silent listing: the store does not answer ListMultipartUploads
        s3log.write_text("")
        uploads_delay.write_text("20\n")
        list_delay.write_text("120\n")
        got = {}
        t = _start_export(dsn, "os", got)
        r["os unanswered"] = _wait_unanswered(work, s3log, "os")
        t0 = time.monotonic()
        _cancel(dsn)
        t.join(300)
        elapsed = time.monotonic() - t0
        uploads_delay.unlink()
        list_delay.unlink()
        lists = re.compile(rf"^GET /{BUCKET} .*uploads=")
        r["os listed"] = str(sum(1 for ln in s3log.read_text(errors="replace").splitlines()
                                 if lists.match(ln)))
        r["os outcome"] = got.get("outcome", query_error("the export did not return"))
        r["os prompt"] = "yes" if elapsed <= 30 else f"no, after {int(elapsed)} s"
        r["os survived"] = _survived(dsn, log, crashes0)

        # refused upload listing: the cleanup's LOG names the prefix it listed (#1869)
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE TABLE ou_small (id int8) USING pgcolumnar")
            c.execute("INSERT INTO ou_small SELECT g FROM generate_series(1,1000) g")
        s3log.write_text("")
        list_status = work / "__list_uploads_status__"
        put_status = work / "__put_status__"
        list_status.write_text("403\n")
        put_status.write_text(f"403 {BUCKET}/or/part-0000.parquet\n")
        offset = os.path.getsize(log)
        r["or outcome"] = _outcome(dsn, "SELECT pgcolumnar.parallel_export_parquet("
                                        f"'ou_small'::regclass, 's3://{BUCKET}/or', 2)")
        list_status.unlink()
        put_status.unlink()
        r["or listed"] = str(sum(1 for ln in s3log.read_text(errors="replace").splitlines()
                                 if lists.match(ln)))
        r["or url"] = _cleanup_named(log, offset)

        # no region: each refusal names the prefix the caller gave (#1869)
        _pg_ctl(cluster, ["-D", str(datadir), "-m", "fast", "-w", "stop"], {})
        noregion = {k: v for k, v in aws.items() if k != "AWS_REGION"}
        started = _pg_ctl(cluster, ["-D", str(datadir), "-l", log, "-w", "start"], noregion)
        if started.returncode != 0:
            raise RuntimeError(f"the private cluster did not restart: {started.stderr}")
        r["nr export"] = _region_named(dsn, "SELECT pgcolumnar.parallel_export_parquet("
                                            f"'ou_small'::regclass, 's3://{BUCKET}/nr', 2)")
        r["nr read"] = _region_named(dsn, "SELECT count(*) FROM pgcolumnar.read_parquet("
                                          f"'s3://{BUCKET}/nr/') AS t(id int8)")
        yield r
    finally:
        _pg_ctl(cluster, ["-D", str(datadir), "-m", "immediate", "-w", "stop"], {})
        srv.kill()
        srv.wait()
        shutil.rmtree(work, ignore_errors=True)


def test_open_uploads_fixture_server_is_up(ou, expect):
    expect.text(str(ou["server up"]), "1", "premise: sigv4 fixture server is up")


def test_the_foreign_uploads_were_open_before_the_exports(ou, expect):
    expect.text(ou["seeded"],
                f"{BUCKET}/op/notes.parquet {BUCKET}/op/part-0004.parquet "
                f"{BUCKET}/oc/notes.parquet {BUCKET}/oc/part-0004.parquet "
                f"{BUCKET}/ot/notes.parquet {BUCKET}/ot/part-0004.parquet",
                "premise: the foreign uploads were open before the exports")


def test_parts_cancel_landed_while_every_worker_sent_parts(ou, expect):
    expect.text(ou["op sending"], "4 open, sending parts",
                "premise: parts: every worker was sending parts when the cancel landed")


def test_parts_reports_its_own_cancel(ou, expect):
    expect.text(ou["op outcome"], "57014: canceling statement due to user request",
                "parts: the export reports its own cancel (57014)")


def test_parts_leaves_no_upload_of_the_export_open(ou, expect):
    expect.num(ou["op own"], 0, "parts: no upload of this export is left open")


def test_parts_leaves_the_foreign_uploads_open(ou, expect):
    expect.text(ou["op foreign"], f"{BUCKET}/op/notes.parquet {BUCKET}/op/part-0004.parquet",
                "parts: the foreign uploads under the prefix are left open")


def test_parts_the_server_survived(ou, expect):
    expect.text(ou["op survived"], "1", "parts: the server survived")


def test_cancel_landed_while_every_create_was_unanswered(ou, expect):
    expect.text(ou["oc unanswered"], "4 open, 0 parts",
                "premise: cancel: every worker's CreateMultipartUpload was unanswered when the cancel landed")


def test_cancel_reports_its_own_cancel(ou, expect):
    expect.text(ou["oc outcome"], "57014: canceling statement due to user request",
                "cancel: the export reports its own cancel (57014)")


def test_cancel_leaves_no_upload_of_the_export_open(ou, expect):
    expect.num(ou["oc own"], 0, "cancel: no upload of this export is left open")


def test_cancel_leaves_the_foreign_uploads_open(ou, expect):
    expect.text(ou["oc foreign"], f"{BUCKET}/oc/notes.parquet {BUCKET}/oc/part-0004.parquet",
                "cancel: the foreign uploads under the prefix are left open")


def test_cancel_leaves_no_completed_object(ou, expect):
    expect.num(ou["oc left"], 0, "cancel: no completed object remains at the prefix")


def test_cancel_the_server_survived(ou, expect):
    expect.text(ou["oc survived"], "1", "cancel: the server survived")


def test_terminate_landed_while_every_create_was_unanswered(ou, expect):
    expect.text(ou["ot unanswered"], "4 open, 0 parts",
                "premise: terminate: every worker's CreateMultipartUpload was unanswered "
                "when the workers were terminated")


def test_terminate_reports_that_a_worker_failed(ou, expect):
    expect.text(ou["ot outcome"],
                "XX000: pgcolumnar.parallel_export_parquet worker N failed: "
                "worker exited without reporting a result",
                "terminate: the export reports that a worker failed")


def test_terminate_leaves_no_upload_of_the_export_open(ou, expect):
    expect.num(ou["ot own"], 0, "terminate: no upload of this export is left open")


def test_terminate_leaves_the_foreign_uploads_open(ou, expect):
    expect.text(ou["ot foreign"], f"{BUCKET}/ot/notes.parquet {BUCKET}/ot/part-0004.parquet",
                "terminate: the foreign uploads under the prefix are left open")


def test_terminate_the_server_survived(ou, expect):
    expect.text(ou["ot survived"], "1", "terminate: the server survived")


def test_double_cancel_first_cancel_landed_while_every_create_was_unanswered(ou, expect):
    expect.text(ou["od unanswered"], "4 open, 0 parts",
                "premise: double cancel: every worker's CreateMultipartUpload was unanswered "
                "when the first cancel landed")


def test_double_cancel_second_cancel_landed_in_a_part_delete(ou, expect):
    expect.text(ou["od deleting"], "yes",
                "premise: double cancel: the first cancel's cleanup was deleting a part "
                "when the second cancel landed")


def test_double_cancel_reports_the_exports_own_cancel(ou, expect):
    expect.text(ou["od outcome"], "57014: canceling statement due to user request",
                "double cancel: the export reports its own cancel (57014)")


def test_double_cancel_logs_no_errstart(ou, expect):
    expect.text(ou["od errstart"], "0",
                "double cancel: the server log has no 'errstart was not called'")


def test_double_cancel_leaves_no_upload_of_the_export_open(ou, expect):
    expect.num(ou["od own"], 0, "double cancel: no upload of this export is left open")


def test_double_cancel_leaves_the_foreign_uploads_open(ou, expect):
    expect.text(ou["od foreign"], f"{BUCKET}/od/notes.parquet {BUCKET}/od/part-0004.parquet",
                "double cancel: the foreign uploads under the prefix are left open")


def test_double_cancel_the_server_survived(ou, expect):
    expect.text(ou["od survived"], "1", "double cancel: the server survived")


def test_silent_listing_cancel_landed_while_every_create_was_unanswered(ou, expect):
    expect.text(ou["os unanswered"], "4 open, 0 parts",
                "premise: silent listing: every worker's CreateMultipartUpload was unanswered "
                "when the cancel landed")


def test_silent_listing_the_cleanup_asked_for_open_uploads(ou, expect):
    expect.text(ou["os listed"], "1", "silent listing: the cleanup asked the store for its open uploads")


def test_silent_listing_reports_its_own_cancel(ou, expect):
    expect.text(ou["os outcome"], "57014: canceling statement due to user request",
                "silent listing: the export reports its own cancel (57014)")


def test_silent_listing_returned_promptly(ou, expect):
    expect.text(ou["os prompt"], "yes", "silent listing: the export returned within 30 s of the cancel")


def test_silent_listing_the_server_survived(ou, expect):
    expect.text(ou["os survived"], "1", "silent listing: the server survived")


def test_refused_upload_listing_export_fails_on_the_refused_part(ou, expect):
    expect.text(ou["or outcome"],
                "28000: pgcolumnar.parallel_export_parquet worker 0 failed: columnar: access to "
                f"\"s3://{BUCKET}/or/part-0000.parquet\" was denied (HTTP 403: AccessDenied)",
                "premise: refused upload listing: the export fails on worker 0's refused part")


def test_refused_upload_listing_the_cleanup_listed_once(ou, expect):
    expect.text(ou["or listed"], "1",
                "premise: refused upload listing: the cleanup asked the store for its open uploads once")


def test_refused_upload_listing_names_the_listed_prefix(ou, expect):
    expect.text(ou["or url"], f"s3://{BUCKET}/or/",
                "refused upload listing: the server log names the prefix that was listed, not a dummy key")


def test_no_region_export_names_the_prefix(ou, expect):
    expect.text(ou["nr export"], f"s3://{BUCKET}/nr",
                "no region: parallel_export_parquet's refusal names the prefix it was given, not a dummy key")


def test_no_region_read_parquet_names_the_prefix(ou, expect):
    expect.text(ou["nr read"], f"s3://{BUCKET}/nr/",
                "no region: read_parquet's directory listing refusal names the prefix it was given, "
                "not a dummy key")
