"""Iceberg REST catalog VENDED credentials (#656): the pytest twin of
`test/iceberg_rest_vended.sh`, every check by the shell's name (#1341).

A REST catalog returns, in its loadTable reply, short-lived scoped storage credentials,
and the reader must sign its S3 reads with THOSE, never with an ambient environment
credential. The postmaster holds no AWS_* credential, the warehouse is served by the
SigV4-verifying S3 fixture (test/objstore_http_server.py, the shell's own), and the
catalog (test/iceberg_rest_server.py, the shell's own) vends that fixture's key, so a
green read can only be a read signed with the vended credentials. Asserted: the vended
read returns the table's survivors; the vended secret appears in neither the server log
(read under log_statement = 'all') nor the S3 request log; a WRONG vended secret is
refused by S3 with its own 403 message, not by "no credentials"; of two
storage-credentials entries the longest matching prefix wins, and the flat config is
wrong too, so the array must have been read; a vended endpoint off the allow-list is
refused by the allow-list itself; a catalog that vends nothing falls back to ambient
credentials; and no backend died on the way. Each file gets its own longest-prefix
credential (#1820): with split metadata/ and data/ entries the data/ key signs the data
files, the flat config covers a file no prefix matches, the longest prefix wins when it
is listed before a broader one, an entry with a key id and no secret is ignored without
killing the backend, and a file nothing vended covers is refused even when the server
holds ambient keys S3 accepts.

A BACKEND THAT DIED DOES NOT PASS AS ONE THAT SURVIVED: survival also reads the private
cluster's server log for killed backends, and an unreadable log fails it.

A PRIVATE CLUSTER, because the postmaster is restarted with the catalog token and, for
the ambient arm, AWS_* in its environment, which the shared cluster cannot do. Every
AWS_* variable of this process is removed before each start.
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
FX = TEST / "fixtures" / "iceberg"
AKID, SECRET, REGION, BUCKET = "VENDEDKEYID", "vended-secret-shh", "pgc-test-1", "pgc-bucket"
RESTTOK = "rest-token-vend"
COLS = "AS t(id bigint, region text, amount int)"


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _await_up(dsn):
    """Wait, at most 60 s, until the server answers, so a reading after a crash reads the
    table rather than crash recovery. The crash itself is still caught: _survived counts it."""
    for _ in range(120):
        try:
            with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as conn:
                conn.execute("SELECT 1")
            return
        except psycopg.Error:
            time.sleep(0.5)


def _value(dsn, sql, setup=()):
    """-> the first column of the first row as text, or the failure as a query_error()."""
    _await_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            for s in setup:
                conn.execute(s)
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    if row is None or row[0] is None:
        return query_error(f"no value from {sql}")
    return str(row[0])


def _refusal(dsn, sql, phrase, setup=()):
    """-> the SQLSTATE when `sql` is refused with `phrase` in its message, "SQLSTATE
    (another refusal: MESSAGE)" when something else refused it, or what happened instead.
    28000 is also "no credentials", so the wrong-secret arm counts only the 403."""
    _await_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            for s in setup:
                conn.execute(s)
            conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        if not exc.sqlstate:
            return query_error(_message(exc))
        msg = exc.diag.message_primary or ""
        return exc.sqlstate if phrase in msg else f"{exc.sqlstate} (another refusal: {msg})"
    return "no error"


def _count_in(path, needle):
    """-> how many lines of `path` hold `needle`, or a query_error() when it can't be read."""
    try:
        return str(sum(1 for line in pathlib.Path(path).read_text(errors="replace").splitlines()
                       if line and needle in line))
    except OSError as exc:
        return query_error(f"{path} unreadable: {exc}")


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
    _pg_ctl(cluster, ["-D", str(datadir), "-m", "fast", "-w", "stop"], {})
    return _pg_ctl(cluster, ["-D", str(datadir), "-l", log, "-w", "start"], env).returncode == 0


def _ready(out):
    for _ in range(50):
        if out.is_file() and "READY" in out.read_text(errors="replace"):
            return
        time.sleep(0.1)


def _module_installed(pg_config):
    try:
        lib = subprocess.run([pg_config, "--pkglibdir"], capture_output=True, text=True).stdout.strip()
    except OSError:
        return False
    return (pathlib.Path(lib) / "pgcolumnar_objstore.so").is_file()


@pytest.fixture(scope="module")
def irv(pgc_cluster):
    if not (FX / "warehouse_del" / "db" / "t" / "metadata" / "apply.metadata.json").is_file():
        yield "fixture"
        return
    if not _module_installed(pgc_cluster.pg_config):
        yield "objstore"
        return
    from pgc_cluster import Cluster, pick_port

    work = pathlib.Path(tempfile.mkdtemp(prefix="pgc-icevend-"))
    os.chmod(work, 0o777)
    bucket = work / BUCKET
    shutil.copytree(FX / "warehouse_del" / "db", bucket / "db")
    for p in [bucket, *bucket.rglob("*")]:
        p.chmod(0o777 if p.is_dir() else 0o666)
    procs = []

    s3_port = pick_port(30)
    s3out = work / "s3.out"
    procs.append(subprocess.Popen(
        [sys.executable, str(TEST / "objstore_http_server.py"), "--dir", str(work),
         "--port", str(s3_port), "--log", str(work / "s3.log"),
         "--sigv4-key", AKID, "--sigv4-secret", SECRET, "--sigv4-region", REGION],
        stdout=open(s3out, "w"), stderr=subprocess.STDOUT))
    _ready(s3out)
    s3ep = f"http://127.0.0.1:{s3_port}"

    def start_rest(*extra):
        """-> the catalog URI of a fresh REST catalog, picked only after the last one bound."""
        port = pick_port(32)
        out = work / f"rest_{port}.out"
        procs.append(subprocess.Popen(
            [sys.executable, str(TEST / "iceberg_rest_server.py"), "--port", str(port),
             "--log", str(work / f"rest_{port}.log"), "--namespace", "db", "--table", "t",
             "--metadata-location", f"s3://{BUCKET}/db/t/metadata/apply.metadata.json", *extra],
            stdout=open(out, "w"), stderr=subprocess.STDOUT))
        _ready(out)
        return f"http://127.0.0.1:{port}"

    def scan(cat, cols=COLS):
        return f"pgcolumnar.iceberg_rest_scan('{cat}','db','t') {cols}"

    vend = ["--token", RESTTOK, "--vend-key", AKID, "--vend-region", REGION]
    datadir = work / "data"
    datadir.mkdir()
    log = str(work / "server.log")
    cluster = Cluster(pgc_cluster.pg_config, "icevend", datadir, pick_port(31))
    r = {"s3 up": s3out.read_text(errors="replace").count("READY") if s3out.is_file() else 0}
    try:
        cluster.initdb()
        tok = {"PGCOLUMNAR_ICEBERG_REST_TOKEN": RESTTOK}
        started = _pg_ctl(cluster, ["-D", str(datadir), "-l", log, "-w", "start"], tok)
        if started.returncode != 0:
            try:
                tail = "".join(pathlib.Path(log).read_text(errors="replace").splitlines(True)[-20:])
            except OSError as exc:
                tail = f"(server log unreadable: {exc})"
            raise RuntimeError(f"the private cluster did not start: {started.stderr}\n{tail}")
        dsn = cluster.dsn()
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            c.execute("ALTER SYSTEM SET pgcolumnar.objstore_allowed_endpoints = '127.0.0.1'")
            c.execute("SELECT pg_reload_conf()")
        before = _crashes(log)

        cat = start_rest(*vend, "--vend-secret", SECRET, "--vend-endpoint", s3ep)
        r["core"] = _value(dsn, "SELECT string_agg(id || '|' || region || '|' || amount, E'\\n' ORDER BY id)"
                                f" FROM {scan(cat)}")

        # the secret is only a SigV4 signing seed, never a SQL argument: read under
        # log_statement = 'all', then look for it
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("ALTER SYSTEM SET log_statement = 'all'")
            c.execute("SELECT pg_reload_conf()")
        _value(dsn, f"SELECT count(*) FROM {scan(cat)}")
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("ALTER SYSTEM SET log_statement = 'none'")
            c.execute("SELECT pg_reload_conf()")
        lines = _count_in(log, "")
        r["log lines"] = ("yes" if int(lines) > 0 else "no") if lines.isdigit() else lines
        r["secret pg"] = _count_in(log, SECRET)
        r["secret s3"] = _count_in(work / "s3.log", SECRET)

        bad = start_rest(*vend, "--vend-secret", "wrong-vended-secret", "--vend-endpoint", s3ep)
        r["wrong"] = _refusal(dsn, f"SELECT * FROM {scan(bad)}", "was denied (HTTP 403")

        sc = start_rest(*vend, "--vend-secret", SECRET, "--vend-endpoint", s3ep,
                        "--storage-cred-prefix", f"s3://{BUCKET}/db/t")
        r["prefix"] = _value(dsn, f"SELECT count(*) FROM {scan(sc)}")

        # each file gets its OWN longest-prefix match (#1820); neither prefix covers
        # the other, so one choice made against the metadata-location is wrong
        tl = f"s3://{BUCKET}/db/t"
        good = [*vend, "--vend-secret", SECRET, "--vend-endpoint", s3ep]
        split = start_rest(*good, "--storage-cred", f"{tl}/metadata/=good",
                           "--storage-cred", f"{tl}/data/=good")
        r["split"] = _value(dsn, f"SELECT count(*) FROM {scan(split)}")
        splitbad = start_rest(*good, "--storage-cred", f"{tl}/metadata/=good",
                              "--storage-cred", f"{tl}/data/=bad")
        r["split bad"] = _refusal(dsn, f"SELECT * FROM {scan(splitbad)}",
                                  '.parquet" was denied (HTTP 403')
        flat = start_rest(*good, "--storage-cred", f"{tl}/data/=good", "--flat-config", "good")
        r["flat"] = _value(dsn, f"SELECT count(*) FROM {scan(flat)}")
        # the longer prefix listed FIRST, a broader bad one after it: a reader that
        # kept the last match instead of the longest reads the bad key
        longfirst = start_rest(*good, "--storage-cred", f"{tl}/=good",
                               "--storage-cred", f"s3://{BUCKET}/=bad")
        r["long first"] = _value(dsn, f"SELECT count(*) FROM {scan(longfirst)}")
        # a key id with no secret vends nothing: ignored, the broader good entry signs,
        # and the backend survives it
        akid_before = _crashes(log)
        akidonly = start_rest(*good, "--storage-cred", f"s3://{BUCKET}/=good",
                              "--storage-cred", f"{tl}/=akidonly")
        r["akid only"] = _value(dsn, f"SELECT count(*) FROM {scan(akidonly)}")
        r["akid only survived"] = _survived(dsn, log, akid_before)
        # read after the restart below gives the server ambient keys S3 accepts
        dataonly = start_rest(*good, "--storage-cred", f"{tl}/data/=bad")

        off = start_rest(*vend, "--vend-secret", SECRET, "--vend-endpoint", f"http://127.0.0.2:{s3_port}")
        r["off list"] = _refusal(dsn, f"SELECT * FROM {scan(off)}",
                                 "is not in pgcolumnar.objstore_allowed_endpoints",
                                 setup=("SET pgcolumnar.objstore_allowed_endpoints = '127.0.0.1'",))

        amb = start_rest("--token", RESTTOK)
        _restart(cluster, datadir, log, dict(tok, AWS_ENDPOINT_URL=s3ep, AWS_ACCESS_KEY_ID=AKID,
                                             AWS_SECRET_ACCESS_KEY=SECRET, AWS_REGION=REGION))
        _await_up(dsn)
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("ALTER SYSTEM SET pgcolumnar.objstore_allowed_endpoints = '127.0.0.1'")
            c.execute("SELECT pg_reload_conf()")
        r["ambient"] = _value(dsn, f"SELECT count(*) FROM {scan(amb)}")
        r["uncovered"] = _refusal(dsn, f"SELECT * FROM {scan(dataonly)}",
                                  "no vended storage credential covers")
        r["survived"] = _survived(dsn, log, before)
        yield r
    finally:
        _pg_ctl(cluster, ["-D", str(datadir), "-m", "immediate", "-w", "stop"], {})
        for p in procs:
            p.kill()
            p.wait()
        shutil.rmtree(work, ignore_errors=True)


def _declined(irv, expect):
    if isinstance(irv, dict):
        return False
    if irv == "fixture":
        expect.cannot_run("ABSENT_FIXTURE", "the committed iceberg fixtures are missing")
    else:
        expect.cannot_run("MISSING_DEPENDENCY", "pgcolumnar_objstore.so is not in the pkglibdir")
    return True


def test_the_vended_suite_can_run_here(irv, expect):
    """The shell skips the whole suite under each of these names."""
    expect.require_module("json", name="python3 is needed")
    if irv == "objstore":
        expect.cannot_run("MISSING_DEPENDENCY", "pgcolumnar_objstore.so is not in the pkglibdir",
                          name="the object-store module is not installed")
        return
    if irv == "fixture":
        expect.cannot_run("ABSENT_FIXTURE", "the committed iceberg fixtures are not in this tree",
                          name="iceberg fixtures are missing")
        return
    expect.text(str(irv["s3 up"]), "1", "premise: the SigV4 S3 fixture is up")


def test_a_rest_scan_reads_via_vended_credentials(irv, expect):
    if _declined(irv, expect):
        return
    expect.text(irv["core"], "1|eu|10\n3|us|30\n5|us|50",
                "iceberg_rest_scan reads via vended credentials (no ambient creds set)")


def test_the_server_log_holds_lines_to_search(irv, expect):
    if _declined(irv, expect):
        return
    expect.text(irv["log lines"], "yes", "premise: the PG server log holds lines to search")


def test_the_vended_secret_is_not_in_the_server_log(irv, expect):
    if _declined(irv, expect):
        return
    expect.text(irv["secret pg"], "0", "the vended secret never appears in the PG server log")


def test_the_vended_secret_is_not_in_the_s3_request_log(irv, expect):
    if _declined(irv, expect):
        return
    expect.text(irv["secret s3"], "0", "the vended secret never appears in the S3 request log")


def test_a_wrong_vended_secret_is_refused_by_s3(irv, expect):
    if _declined(irv, expect):
        return
    expect.text(irv["wrong"], "28000", "a wrong vended secret is refused by S3, not bypassed (28000)")


def test_the_longest_storage_credentials_prefix_wins(irv, expect):
    if _declined(irv, expect):
        return
    expect.text(irv["prefix"], "3", "storage-credentials longest-prefix match selects the right creds")


def test_split_credentials_each_sign_their_own_files(irv, expect):
    if _declined(irv, expect):
        return
    expect.text(irv["split"], "3", "split metadata/ and data/ credentials each sign their own files")


def test_the_data_credential_signs_a_data_file(irv, expect):
    if _declined(irv, expect):
        return
    expect.text(irv["split bad"], "28000",
                "the data/ credential, not the metadata/ one, signs a data file (28000)")


def test_the_flat_config_signs_an_uncovered_file(irv, expect):
    if _declined(irv, expect):
        return
    expect.text(irv["flat"], "3", "the flat config signs a file no storage-credentials prefix covers")


def test_the_longest_prefix_wins_when_listed_first(irv, expect):
    if _declined(irv, expect):
        return
    expect.text(irv["long first"], "3", "the longest prefix wins when it is listed before a broader one")


def test_an_entry_with_a_key_id_and_no_secret_is_ignored(irv, expect):
    if _declined(irv, expect):
        return
    expect.text(irv["akid only"], "3", "an entry with a key id and no secret is ignored, not used")


def test_the_backend_survives_an_entry_with_no_secret(irv, expect):
    if _declined(irv, expect):
        return
    expect.text(irv["akid only survived"], "1",
                "backend still up after an entry with a key id and no secret")


def test_a_vended_endpoint_off_the_allow_list_is_refused(irv, expect):
    if _declined(irv, expect):
        return
    expect.text(irv["off list"], "42501", "vended creds for an off-allow-list endpoint are refused (42501)")


def test_a_catalog_vending_nothing_falls_back_to_ambient(irv, expect):
    if _declined(irv, expect):
        return
    expect.text(irv["ambient"], "3", "a table vending no creds still reads with ambient (cfg NULL path)")


def test_an_uncovered_file_is_refused_not_read_ambient(irv, expect):
    if _declined(irv, expect):
        return
    expect.text(irv["uncovered"], "28000",
                "a file no vended credential covers is refused, not read ambient (28000)")


def test_the_backend_survives_the_vended_arms(irv, expect):
    if _declined(irv, expect):
        return
    expect.text(irv["survived"], "1", "backend still up after the vended-credential arms")
