"""Iceberg over object storage (#388 phase 6): the pytest twin of
`test/iceberg_objstore.sh`, every check by the shell's name (#1341).

iceberg_scan reads a table whose metadata, manifests, data files and delete files are
served over S3 by the local SigV4 fixture server (test/objstore_http_server.py, the
shell's own), and must read exactly what the filesystem suites read: position deletes,
equality deletes, deletion vectors and a name-mapped id-less file, against the same
oracles. A read with no AWS environment is refused 28000; recorded delete paths that
leave the table location (outside it, a literal "..", a percent-encoded "..", and one
behind an encoded NUL) are refused 22023, never fetched; and an endpoint not on
pgcolumnar.objstore_allowed_endpoints is refused by the allow-list itself (42501 with
its own message), not by whatever the connect attempt raises.

A BACKEND THAT DIED DOES NOT PASS AS ONE THAT SURVIVED: survival also reads the
private cluster's server log for killed backends, and an unreadable log fails it.

A PRIVATE CLUSTER, because the postmaster is restarted with AWS_* in its environment,
which the shared cluster cannot do. It starts with every AWS_* variable of this process
removed. Where the committed fixtures are missing, the first test fails under the
shell's pgc_skip message and the rest decline.
"""
import json
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
FX = TEST / "fixtures" / "iceberg"
AKID, SECRET, REGION, BUCKET = "PGCTESTKEYID", "pgctest-secret-shhh", "pgc-test-1", "pgc-bucket"
MDROOT = f"s3://{BUCKET}/db/t/metadata"
NMROOT = f"s3://{BUCKET}/db/t_nm/metadata"
COLS = "AS t(id bigint, region text, amount int)"


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


def _sqlstate(dsn, sql):
    """-> the SQLSTATE `sql` raised, or what happened instead."""
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        return exc.sqlstate or query_error(_message(exc))
    return "no error"


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


def _crashes(log):
    try:
        return pathlib.Path(log).read_text(errors="replace").count("terminated by signal")
    except OSError as exc:
        return query_error(f"crash count unreadable: {exc}")


def _survived(dsn, log, before):
    up, now = _value(dsn, "SELECT 1"), _crashes(log)
    if not isinstance(before, int) or not isinstance(now, int):
        return query_error(f"crash count unreadable: {before} / {now}")
    return "1" if up == "1" and now == before else f"no ([{up}], {now - before} backend crashes)"


def _oracle(which):
    """-> the surviving ids the filesystem oracle lists, or a value no scan prints."""
    kind, meta = which.split(":")
    try:
        o = json.loads((FX / "warehouse_del" / "expected_deletes.json").read_text())
        return ",".join(str(i) for i in o[{"eq": "eq_surviving", "dv": "dv_surviving"}[kind]][meta])
    except (OSError, ValueError, KeyError) as exc:
        return f"no oracle for {which} ({exc})"


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
    pg_restart_env does: up to 15 s for SELECT 1. A cluster that does not come back is
    one error here, not a failure blamed on every later check (#1635 review)."""
    _pg_ctl(cluster, ["-D", str(datadir), "-m", "fast", "-w", "stop"], {})
    started = _pg_ctl(cluster, ["-D", str(datadir), "-l", log, "-w", "start"], env)
    for _ in range(30):
        if started.returncode == 0 and _value(cluster.dsn(), "SELECT 1") == "1":
            return
        time.sleep(0.5)
    raise RuntimeError(f"cluster did not come back after env restart: {started.stderr.strip()}")


@pytest.fixture(scope="module")
def iob(pgc_cluster):
    if not (FX / "warehouse_del" / "db" / "t" / "metadata" / "apply.metadata.json").is_file():
        yield None
        return
    from pgc_cluster import Cluster, pick_port

    work = pathlib.Path(tempfile.mkdtemp(prefix="pgc-iceobj-"))
    os.chmod(work, 0o777)
    bucket = work / BUCKET
    shutil.copytree(FX / "warehouse_del" / "db", bucket / "db")
    shutil.copytree(FX / "warehouse_nm" / "db" / "t", bucket / "db" / "t_nm")
    for p in [bucket, *bucket.rglob("*")]:
        p.chmod(0o777 if p.is_dir() else 0o666)

    s3_port = pick_port(27)
    out = work / "s3_server.out"
    srv = subprocess.Popen([sys.executable, str(TEST / "objstore_http_server.py"), "--dir", str(work),
                            "--port", str(s3_port), "--log", str(work / "s3.log"),
                            "--sigv4-key", AKID, "--sigv4-secret", SECRET, "--sigv4-region", REGION],
                           stdout=open(out, "w"), stderr=subprocess.STDOUT)
    for _ in range(50):
        if out.is_file() and "READY" in out.read_text(errors="replace"):
            break
        time.sleep(0.1)

    datadir = work / "data"
    datadir.mkdir()
    log = str(work / "server.log")
    cluster = Cluster(pgc_cluster.pg_config, "iceobj", datadir, pick_port(26))
    r = {"server up": out.read_text(errors="replace").count("READY") if out.is_file() else 0}
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
        scan = "pgcolumnar.iceberg_scan('{}/{}.metadata.json') " + COLS

        # The baseline comes before the no-env read, where the shell takes it: a backend
        # that dies in that read must move the count (#1635 review).
        before = _crashes(log)
        r["no env"] = _sqlstate(dsn, f"SELECT count(*) FROM {scan.format(MDROOT, 'apply')}")

        aws = {"AWS_ENDPOINT_URL": f"http://127.0.0.1:{s3_port}", "AWS_ACCESS_KEY_ID": AKID,
               "AWS_SECRET_ACCESS_KEY": SECRET, "AWS_REGION": REGION}
        _restart(cluster, datadir, log, aws)
        r["pos"] = _value(dsn, f"SELECT count(*) FROM {scan.format(MDROOT, 'apply')}")
        r["pos gone"] = _value(dsn, f"SELECT count(*) FROM {scan.format(MDROOT, 'apply')} WHERE id IN (2,4)")
        r["eq"] = _value(dsn, f"SELECT string_agg(id::text, ',' ORDER BY id) FROM {scan.format(MDROOT, 'eqapply')}")
        r["dv"] = _value(dsn, f"SELECT string_agg(id::text, ',' ORDER BY id) FROM {scan.format(MDROOT, 'dvapply')}")
        r["noapply"] = _value(dsn, f"SELECT count(*) FROM {scan.format(MDROOT, 'noapply')}")
        r["nm"] = _value(dsn, f"SELECT count(*) FROM {scan.format(NMROOT, 'nmapply')}")
        r["after reads"] = _survived(dsn, log, before)
        for key, meta in (("escape", "eqescape"), ("dotdot", "eqdotdot"),
                          ("pctdot", "eqpctdot"), ("nulpct", "eqnulpct")):
            r[key] = _sqlstate(dsn, f"SELECT * FROM {scan.format(MDROOT, meta)}")
        r["after refusal"] = _survived(dsn, log, before)

        _restart(cluster, datadir, log, dict(aws, AWS_ENDPOINT_URL=f"http://127.0.0.2:{s3_port}"))
        r["endpoint"] = _refusal(dsn, f"SELECT count(*) FROM {scan.format(MDROOT, 'apply')}",
                                 "is not in pgcolumnar.objstore_allowed_endpoints", " by the allow-list")
        r["after endpoint"] = _survived(dsn, log, before)
        yield r
    finally:
        _pg_ctl(cluster, ["-D", str(datadir), "-m", "immediate", "-w", "stop"], {})
        srv.kill()
        srv.wait()
        shutil.rmtree(work, ignore_errors=True)


def _declined(iob, expect):
    if iob is not None:
        return False
    expect.cannot_run("ABSENT_FIXTURE", "the committed iceberg fixtures are missing")
    return True


def test_the_iceberg_objstore_fixtures_are_present(iob, expect):
    """The shell skips the whole suite without the committed fixtures, under this name."""
    if iob is None:
        expect.cannot_run("ABSENT_FIXTURE", "the committed iceberg fixtures are not in this tree",
                          name="iceberg fixtures are missing")
        return
    expect.require_module("json", name="python3 is needed")
    expect.text(str(iob["server up"]), "1", "premise: sigv4 fixture server is up")


def test_an_s3_iceberg_read_with_no_aws_environment_is_refused(iob, expect):
    if _declined(iob, expect):
        return
    expect.text(iob["no env"], "28000", "config: an s3 iceberg read with no AWS environment is refused (28000)")


def test_position_deletes_read_over_s3(iob, expect):
    if _declined(iob, expect):
        return
    expect.text(iob["pos"], "3", "position deletes read over s3 (surviving == filesystem oracle)")


def test_the_position_deleted_ids_are_gone_over_s3(iob, expect):
    if _declined(iob, expect):
        return
    expect.text(iob["pos gone"], "0", "the position-deleted ids are gone over s3")


def test_equality_deletes_read_over_s3(iob, expect):
    if _declined(iob, expect):
        return
    expect.text(iob["eq"], _oracle("eq:eqapply"), "equality deletes read over s3 (survivors == oracle)")


def test_deletion_vectors_read_over_s3(iob, expect):
    if _declined(iob, expect):
        return
    expect.text(iob["dv"], _oracle("dv:dvapply"), "deletion vectors read over s3 (survivors == oracle)")


def test_a_scan_whose_delete_does_not_apply_reads_all_rows_over_s3(iob, expect):
    if _declined(iob, expect):
        return
    expect.text(iob["noapply"], "5", "a scan whose delete does not apply reads all rows over s3")


def test_name_mapping_reads_an_id_less_file_over_s3(iob, expect):
    if _declined(iob, expect):
        return
    expect.text(iob["nm"], "5", "name mapping reads an id-less file over s3 (5 rows)")


def test_the_backend_is_still_up_after_the_s3_reads(iob, expect):
    if _declined(iob, expect):
        return
    expect.text(iob["after reads"], "1", "backend still up after the s3 reads")


def test_a_delete_path_outside_the_table_location_is_refused_over_s3(iob, expect):
    if _declined(iob, expect):
        return
    expect.text(iob["escape"], "22023", "a delete path outside the table location is refused over s3 (22023)")


def test_a_dotdot_escaping_delete_path_is_refused_over_s3(iob, expect):
    if _declined(iob, expect):
        return
    expect.text(iob["dotdot"], "22023", "a dotdot-escaping delete path is refused over s3 (22023)")


def test_a_percent_encoded_dotdot_delete_path_is_refused_over_s3(iob, expect):
    if _declined(iob, expect):
        return
    expect.text(iob["pctdot"], "22023", "a percent-encoded dotdot delete path is refused over s3 (22023)")


def test_a_percent_encoded_dotdot_behind_an_encoded_nul_is_refused_over_s3(iob, expect):
    if _declined(iob, expect):
        return
    expect.text(iob["nulpct"], "22023",
                "a percent-encoded dotdot behind an encoded NUL is refused over s3 (22023)")


def test_the_backend_is_still_up_after_the_s3_refusal(iob, expect):
    if _declined(iob, expect):
        return
    expect.text(iob["after refusal"], "1", "backend still up after the s3 refusal")


def test_an_endpoint_not_on_the_allow_list_is_refused(iob, expect):
    if _declined(iob, expect):
        return
    expect.text(iob["endpoint"], "42501 by the allow-list",
                "an endpoint not on objstore_allowed_endpoints is refused")


def test_the_backend_is_still_up_after_the_endpoint_refusal(iob, expect):
    if _declined(iob, expect):
        return
    expect.text(iob["after endpoint"], "1", "backend still up after the endpoint refusal")
