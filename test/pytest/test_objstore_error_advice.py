"""An object-store refusal gives advice that fits the refusal (#1796): the pytest twin
of `test/objstore_error_advice.sh`, every check by the shell's name.

Three facets of the issue. B: the 403 hint named the AWS_* environment of the server
process for every refused key, including a key from a user mapping or one a REST
catalog vended; it must name the source of the refused key. C: the allow-list hint
suggested the host without its port, as the whole list; it must suggest the host:port
the message names, appended to the current list, with any quote in it doubled. D: a
single-PUT export bigger than the socket buffers, which the store refused before
reading the body, lost the HTTP status; the 403 and its hint must survive on an http://
endpoint. D over TLS (#1902): the same refusal from an https:// store still lost its
status, since a failed send leaves a TLS session unreadable. A write whose body is over
1 MiB now sends Expect: 100-continue and waits up to a second for the store to answer
its headers, so the refusal arrives before the body is sent; 100 Continue, or no answer
in time, sends the body, a late 100 is passed over, and 417 sends the request again
without Expect. A wait that runs out stops Expect for the handle's later writes, so a
multipart export to a store that ignores it sends it on one part only, and a 200 to the
headers alone still gets the body (#1903 review). Each kind of store is a hook file of
the shell's fixture.

One SigV4 fixture with a refused bucket, the same fixture again over https with a
certificate the postmaster trusts through SSL_CERT_FILE (made by the openssl CLI, as the
shell makes it), (--tamper-bucket refuses every request there
403 before any body is read, and closes with the body unread), three REST catalogs over
the committed iceberg warehouse (one vends a wrong secret in its flat config, one in a
storage-credentials entry, one vends nothing), and a
postmaster whose environment holds the fixture's right key, so in the mapping and
vended arms the environment would have read the object.

A PRIVATE CLUSTER, because the postmaster starts with AWS_* in its environment, which
the shared cluster cannot do: every AWS_* variable of this process is removed and the
fixture's own are set. Survival also reads the private cluster's server log for killed
backends.
"""
import os
import pathlib
import re
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
AKID, SECRET, REGION = "PGCTESTKEYID", "pgctest-secret-shhh", "pgc-test-1"
BUCKET, DENIED, RESTTOK = "pgc-bucket", "pgc-denied", "rest-token-advice"
ENV_HINT = "Check the AWS_* credential environment of the server process."
MAPPING_HINT = ("Check access_key_id and secret_access_key in the user mapping the current "
                "role uses for this server.")
VENDED_HINT = "The storage credentials the catalog vended for this table were refused."
ICE_COLS = "AS t(id bigint, region text, amount int)"


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _value(dsn, sql, setup=()):
    """-> the first column of the first row as text, or the failure as a query_error()."""
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


def _refusal(dsn, sql, setup=()):
    """-> "SQLSTATE: message | HINT: hint" ("| no hint" without one) for the error `sql`
    raises, or a query_error() when it ran or the server could not be asked."""
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            for s in setup:
                conn.execute(s)
            conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        if not exc.sqlstate:
            return query_error(_message(exc))
        hint = exc.diag.message_hint
        return f"{exc.sqlstate}: {exc.diag.message_primary} | " + \
            (f"HINT: {hint}" if hint else "no hint")
    return query_error("no error: the statement completed")


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
def ea(pgc_cluster):
    if not _module_installed(pgc_cluster.pg_config):
        yield "objstore"
        return
    if shutil.which("openssl") is None:
        yield "openssl"
        return
    if not (FX / "warehouse_del" / "db" / "t" / "metadata" / "apply.metadata.json").is_file():
        yield "fixture"
        return
    from pgc_cluster import Cluster, pick_port

    work = pathlib.Path(tempfile.mkdtemp(prefix="pgc-advice-"))
    os.chmod(work, 0o777)
    bucket = work / BUCKET
    shutil.copytree(FX / "warehouse_del" / "db", bucket / "db")
    (work / DENIED).mkdir()
    for p in [bucket, work / DENIED, *bucket.rglob("*")]:
        p.chmod(0o777 if p.is_dir() else 0o666)
    s3log = work / "s3.log"
    procs = []
    outs = []

    s3_port = pick_port(42)
    s3out = work / "s3.out"
    outs.append(s3out)
    procs.append(subprocess.Popen(
        [sys.executable, str(TEST / "objstore_http_server.py"), "--dir", str(work),
         "--port", str(s3_port), "--log", str(s3log),
         "--sigv4-key", AKID, "--sigv4-secret", SECRET, "--sigv4-region", REGION,
         "--tamper-bucket", DENIED],
        stdout=open(s3out, "w"), stderr=subprocess.STDOUT))
    _ready(s3out)
    s3ep = f"http://127.0.0.1:{s3_port}"

    def start_rest(slot, *extra):
        port = pick_port(slot)
        out = work / f"rest_{port}.out"
        outs.append(out)
        procs.append(subprocess.Popen(
            [sys.executable, str(TEST / "iceberg_rest_server.py"), "--port", str(port),
             "--log", str(work / f"rest_{port}.log"), "--namespace", "db", "--table", "t",
             "--metadata-location", f"s3://{BUCKET}/db/t/metadata/apply.metadata.json", *extra],
            stdout=open(out, "w"), stderr=subprocess.STDOUT))
        _ready(out)
        return f"http://127.0.0.1:{port}"

    cat_bad = start_rest(43, "--token", RESTTOK, "--vend-key", AKID,
                         "--vend-secret", "wrong-vended-secret", "--vend-region", REGION,
                         "--vend-endpoint", s3ep)
    cat_none = start_rest(44, "--token", RESTTOK)

    # the same fixture over https (#1902)
    pki = work / "pki"
    pki.mkdir()
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(pki / "srv.key"), "-subj", "/CN=127.0.0.1",
                    "-addext", "subjectAltName=IP:127.0.0.1", "-days", "2",
                    "-out", str(pki / "srv.pem")], capture_output=True)
    for f in pki.iterdir():
        f.chmod(0o644)
    pki.chmod(0o755)
    tls_port = pick_port(47)
    tlsout = work / "tls.out"
    procs.append(subprocess.Popen(
        [sys.executable, str(TEST / "objstore_http_server.py"), "--dir", str(work),
         "--port", str(tls_port), "--log", str(work / "tls.log"),
         "--sigv4-key", AKID, "--sigv4-secret", SECRET, "--sigv4-region", REGION,
         "--tamper-bucket", DENIED, "--tls-cert", str(pki / "srv.pem"),
         "--tls-key", str(pki / "srv.key")],
        stdout=open(tlsout, "w"), stderr=subprocess.STDOUT))
    _ready(tlsout)

    datadir = work / "data"
    datadir.mkdir()
    log = str(work / "server.log")
    cluster = Cluster(pgc_cluster.pg_config, "advice", datadir, pick_port(45))
    r = {"up": sum(o.read_text(errors="replace").count("READY") for o in outs if o.is_file()),
         "tls up": tlsout.read_text(errors="replace").count("READY") if tlsout.is_file() else 0}
    try:
        cluster.initdb()
        with open(datadir / "postgresql.conf", "a") as fh:
            fh.write("\npgcolumnar.objstore_allowed_endpoints='127.0.0.1'\n"
                     "pgcolumnar.objstore_part_size=67108864\n")
        aws = {"AWS_ENDPOINT_URL": s3ep, "AWS_ACCESS_KEY_ID": AKID,
               "AWS_SECRET_ACCESS_KEY": SECRET, "AWS_REGION": REGION,
               "PGCOLUMNAR_ICEBERG_REST_TOKEN": RESTTOK, "SSL_CERT_FILE": str(pki / "srv.pem")}
        started = _pg_ctl(cluster, ["-D", str(datadir), "-l", log, "-w", "start"], aws)
        if started.returncode != 0:
            raise RuntimeError(f"the private cluster did not start: {started.stderr}")
        dsn = cluster.dsn()
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
            c.execute("CREATE TABLE ea_small USING pgcolumnar AS "
                      "SELECT g::int8 AS id FROM generate_series(1,1000) g")
            c.execute("CREATE TABLE ea_mid USING pgcolumnar AS SELECT g::int8 AS id, "
                      "md5(g::text)||md5((g*7)::text) AS t FROM generate_series(1,300000) g")
        crashes0 = _logcount(log, "terminated by signal")
        _value(dsn, f"SELECT pgcolumnar.export_parquet('ea_small', 's3://{BUCKET}/small.parquet')")
        read_small = (f"SELECT count(*) FROM pgcolumnar.read_parquet('s3://{BUCKET}/small.parquet') "
                      "AS t(id int8)")
        r["env reads"] = _value(dsn, read_small)

        # B: the 403 hint names the source of the refused key
        r["env refused"] = _refusal(dsn, "SELECT count(*) FROM pgcolumnar.read_parquet("
                                         f"'s3://{DENIED}/small.parquet') AS t(id int8)")
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE SERVER ea_s3 FOREIGN DATA WRAPPER pgcolumnar_parquet "
                      f"OPTIONS (endpoint '{s3ep}', region '{REGION}')")
            c.execute("CREATE USER MAPPING FOR postgres SERVER ea_s3 "
                      f"OPTIONS (access_key_id '{AKID}', secret_access_key '{SECRET}')")
            c.execute("CREATE FOREIGN TABLE ea_one (id int8) SERVER ea_s3 "
                      f"OPTIONS (path 's3://{BUCKET}/small.parquet')")
            c.execute("CREATE FOREIGN TABLE ea_glob (id int8) SERVER ea_s3 "
                      f"OPTIONS (path 's3://{BUCKET}/sm*.parquet')")
        r["mapping reads"] = _value(dsn, "SELECT count(*) FROM ea_one")
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("ALTER USER MAPPING FOR postgres SERVER ea_s3 "
                      "OPTIONS (SET secret_access_key 'wrong')")
        r["mapping refused"] = _refusal(dsn, "SELECT count(*) FROM ea_one")
        r["mapping glob refused"] = _refusal(dsn, "SELECT count(*) FROM ea_glob")
        r["catalog env reads"] = _value(dsn, "SELECT count(*) FROM pgcolumnar.iceberg_rest_scan("
                                             f"'{cat_none}','db','t') {ICE_COLS}")
        r["vended refused"] = re.sub(r'/metadata/[^"]*\.metadata\.json"', '/metadata/M.metadata.json"',
                                     _refusal(dsn, "SELECT count(*) FROM pgcolumnar.iceberg_rest_scan("
                                                   f"'{cat_bad}','db','t') {ICE_COLS}"))
        # the same through a storage-credentials entry (#1861): no flat config, and
        # the one entry, covering the whole bucket, holds a key the fixture refuses
        cat_entry = start_rest(46, "--token", RESTTOK, "--vend-key", AKID,
                               "--vend-secret", SECRET, "--vend-region", REGION,
                               "--vend-endpoint", s3ep, "--storage-cred", f"s3://{BUCKET}/=bad")
        r["vended entry refused"] = re.sub(
            r'/metadata/[^"]*\.metadata\.json"', '/metadata/M.metadata.json"',
            _refusal(dsn, "SELECT count(*) FROM pgcolumnar.iceberg_rest_scan("
                          f"'{cat_entry}','db','t') {ICE_COLS}"))

        # C: the allow-list hint adds host:port to the current list
        def listed(value):
            return (f"SET pgcolumnar.objstore_allowed_endpoints = '{value}'",)
        r["list empty"] = _refusal(dsn, read_small, listed(""))
        r["list add"] = _refusal(dsn, read_small, listed("127.0.0.1:1,example.com"))
        quoted = _refusal(dsn, read_small, listed("o''host"))
        r["list quote"] = quoted.split("| HINT: ", 1)[1] if "| HINT: " in quoted else quoted
        m = re.search(r"objstore_allowed_endpoints = '(.*)'; SELECT pg_reload_conf\(\);$", r["list add"])
        r["list permits"] = _value(dsn, read_small, listed(m.group(1) if m else "unparsed"))

        # D: a single-PUT export refused before the body was read keeps its status
        s3log.write_text("")
        _value(dsn, f"SELECT pgcolumnar.export_parquet('ea_mid', 's3://{BUCKET}/mid.parquet')")
        lines = s3log.read_text(errors="replace").splitlines()
        puts = sum(1 for ln in lines if ln.startswith(f"PUT /{BUCKET}/mid.parquet "))
        posts = sum(1 for ln in lines if ln.startswith(f"POST /{BUCKET}/mid.parquet "))
        # the size comes from the same export to a local file, so the premise does not
        # depend on the remote write it sizes (a write that never sends its body, #1902)
        mid = work / "mid_local.parquet"
        _value(dsn, f"SELECT pgcolumnar.export_parquet('ea_mid', '{mid}')")
        size = mid.stat().st_size if mid.is_file() else 0
        r["mid one put"] = "yes" if puts == 1 and posts == 0 and size > 4194304 \
            else f"no ({puts} PUT, {posts} POST, {size} bytes)"
        r["small denied"] = _refusal(dsn, "SELECT pgcolumnar.export_parquet('ea_small', "
                                          f"'s3://{DENIED}/s.parquet')")
        r["mid denied"] = _refusal(dsn, "SELECT pgcolumnar.export_parquet('ea_mid', "
                                        f"'s3://{DENIED}/m.parquet')")

        # D over TLS (#1902): the store answers the headers before the body is sent
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE SERVER ea_tls FOREIGN DATA WRAPPER pgcolumnar_parquet "
                      f"OPTIONS (endpoint 'https://127.0.0.1:{tls_port}', region '{REGION}')")
            c.execute("CREATE USER MAPPING FOR postgres SERVER ea_tls "
                      f"OPTIONS (access_key_id '{AKID}', secret_access_key '{SECRET}')")
        expect_log = work / "__expect_log__"
        expect_log.write_text("")
        expect_log.chmod(0o666)

        def tls_export(rel, key, cols, args=", 'ea_tls'", setup=()):
            try:
                with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
                    for st in setup:
                        conn.execute(st)
                    n = conn.execute(f"SELECT pgcolumnar.export_parquet('{rel}', "
                                     f"'s3://{BUCKET}/{key}'{args})").fetchone()[0]
            except psycopg.Error as exc:
                if not exc.sqlstate:
                    return query_error(_message(exc))
                hint = exc.diag.message_hint
                return f"{exc.sqlstate}: {exc.diag.message_primary} | " + \
                    (f"HINT: {hint}" if hint else "no hint")
            back = _value(dsn, f"SELECT count(*) FROM pgcolumnar.read_parquet("
                               f"'s3://{BUCKET}/{key}') AS t({cols})")
            return f"{n} exported, {back} read back"

        def expect_lines(pred):
            try:
                return sum(1 for ln in expect_log.read_text(errors="replace").splitlines() if pred(ln))
            except OSError as exc:
                return query_error(f"the Expect log is unreadable: {exc}")

        mid_cols = "id int8, t text"
        r["tls mid"] = tls_export("ea_mid", "tls_mid.parquet", mid_cols)
        r["tls mid expect"] = str(expect_lines(
            lambda ln: ln == f"PUT /{BUCKET}/tls_mid.parquet 100-continue"))
        small = tls_export("ea_small", "tls_small.parquet", "id int8")
        small_expect = expect_lines(lambda ln: f" /{BUCKET}/tls_small.parquet " in ln)
        r["tls small"] = "yes" if small == "1000 exported, 1000 read back" and small_expect == 0 \
            else f"no ({small}; {small_expect} requests with Expect)"
        r["tls denied"] = _refusal(dsn, "SELECT pgcolumnar.export_parquet('ea_mid', "
                                        f"'s3://{DENIED}/tm.parquet', 'ea_tls')")
        hook = work / "__expect_ignore__"
        hook.write_text("")
        r["tls ignored"] = tls_export("ea_mid", "tls_ignored.parquet", mid_cols)
        hook.unlink()
        hook = work / "__expect_delay__"
        hook.write_text("2\n")
        r["tls late"] = tls_export("ea_mid", "tls_late.parquet", mid_cols)
        hook.unlink()
        hook = work / "__expect_417__"
        hook.write_text("")
        r["tls 417"] = tls_export("ea_mid", "tls_417.parquet", mid_cols)
        hook.unlink()
        # a store that ignores Expect costs the wait once per connection (#1903 review):
        # 2 MiB parts make the mid-size export several parts over 1 MiB
        hook = work / "__expect_ignore__"
        hook.write_text("")
        s3log.write_text("")
        mp = tls_export("ea_mid", "mp_ignored.parquet", mid_cols, "",
                        ("SET pgcolumnar.objstore_part_size = 2097152",))
        hook.unlink()
        parts = sum(1 for ln in s3log.read_text(errors="replace").splitlines()
                    if ln.startswith(f"PUT /{BUCKET}/mp_ignored.parquet partNumber="))
        r["mp parts"] = "yes" if mp == "300000 exported, 300000 read back" and parts >= 3 \
            else f"no ({mp}; {parts} parts)"
        r["mp expect"] = str(expect_lines(
            lambda ln: ln == f"PUT /{BUCKET}/mp_ignored.parquet 100-continue"))
        # a 2xx to the headers alone: the body is sent anyway, and the object settles
        # after the fixture has read it (#1903 review)
        hook = work / "__expect_early_200__"
        hook.write_text("")
        e200 = _value(dsn, "SELECT pgcolumnar.export_parquet('ea_mid', "
                           f"'s3://{BUCKET}/early200.parquet')")
        hook.unlink()
        obj, prev = work / BUCKET / "early200.parquet", -1
        for _ in range(25):
            size = obj.stat().st_size if obj.is_file() else -1
            if size > 0 and size == prev:
                break
            prev = size
            time.sleep(0.2)
        r["early 200"] = f"{e200} | " + _value(
            dsn, f"SELECT count(*) FROM pgcolumnar.read_parquet('s3://{BUCKET}/early200.parquet') "
                 f"AS t({mid_cols})")
        # a store that answers neither 100 nor the refusal within the wait: #1895's
        # case again, and over http the early answer is still read after the failed send
        hook = work / "__deny_delay__"
        hook.write_text("2\n")
        r["late denied"] = _refusal(dsn, "SELECT pgcolumnar.export_parquet('ea_mid', "
                                         f"'s3://{DENIED}/late.parquet')")
        hook.unlink()
        r["survived"] = _survived(dsn, log, crashes0)
        r["port"] = s3_port
        yield r
    finally:
        _pg_ctl(cluster, ["-D", str(datadir), "-m", "immediate", "-w", "stop"], {})
        for p in procs:
            p.kill()
            p.wait()
        shutil.rmtree(work, ignore_errors=True)


def _declined(ea, expect):
    if isinstance(ea, dict):
        return False
    if ea == "fixture":
        expect.cannot_run("ABSENT_FIXTURE", "the committed iceberg fixtures are missing")
    elif ea == "openssl":
        expect.cannot_run("MISSING_DEPENDENCY", "the openssl CLI is not installed")
    else:
        expect.cannot_run("MISSING_DEPENDENCY", "pgcolumnar_objstore.so is not in the pkglibdir")
    return True


def test_the_advice_suite_can_run_here(ea, expect):
    """The shell skips the whole suite under each of these names."""
    if ea == "objstore":
        expect.cannot_run("MISSING_DEPENDENCY", "pgcolumnar_objstore.so is not in the pkglibdir",
                          name="the object-store module is not installed")
        return
    if ea == "openssl":
        expect.cannot_run("MISSING_DEPENDENCY", "the openssl CLI is not installed",
                          name="the openssl CLI is required to make the https fixture's certificate")
        return
    if ea == "fixture":
        expect.cannot_run("ABSENT_FIXTURE", "the committed iceberg fixtures are not in this tree",
                          name="iceberg fixtures are missing")
        return
    expect.text(str(ea["up"]), "3", "premise: the SigV4 S3 fixture and both REST catalogs are up")


def test_the_https_fixture_is_up(ea, expect):
    if _declined(ea, expect):
        return
    expect.text(str(ea["tls up"]), "1", "premise: the https SigV4 fixture is up")


def test_the_environment_key_writes_and_reads(ea, expect):
    if _declined(ea, expect):
        return
    expect.text(ea["env reads"], "1000",
                "premise: the server environment's key writes and reads s3://pgc-bucket/small.parquet")


def test_an_environment_key_refusal_hints_at_the_environment(ea, expect):
    if _declined(ea, expect):
        return
    expect.text(ea["env refused"],
                f'28000: columnar: access to "s3://{DENIED}/small.parquet" was denied (HTTP 403) '
                f"| HINT: {ENV_HINT}",
                "environment key refused: the 403 hint names the server environment")


def test_the_foreign_table_reads_with_the_right_mapping_secret(ea, expect):
    if _declined(ea, expect):
        return
    expect.text(ea["mapping reads"], "1000",
                "premise: the foreign table reads with the right secret in the user mapping")


def test_a_user_mapping_key_refusal_hints_at_the_mapping(ea, expect):
    if _declined(ea, expect):
        return
    expect.text(ea["mapping refused"],
                f'28000: columnar: access to "s3://{BUCKET}/small.parquet" was denied (HTTP 403) '
                f"| HINT: {MAPPING_HINT}",
                "user mapping key refused: the 403 hint names the user mapping")


def test_a_user_mapping_key_refused_on_a_glob_listing_hints_at_the_mapping(ea, expect):
    if _declined(ea, expect):
        return
    expect.text(ea["mapping glob refused"],
                f'28000: columnar: access to "s3://{BUCKET}/" was denied '
                f"(HTTP 403: SignatureDoesNotMatch) | HINT: {MAPPING_HINT}",
                "user mapping key refused on a glob's listing: the 403 hint names the user mapping")


def test_the_iceberg_table_reads_through_a_catalog_vending_nothing(ea, expect):
    if _declined(ea, expect):
        return
    expect.text(ea["catalog env reads"], "3",
                "premise: the iceberg table reads through a catalog that vends nothing "
                "(server environment)")


def test_a_vended_key_refusal_hints_at_the_vended_credentials(ea, expect):
    if _declined(ea, expect):
        return
    expect.text(ea["vended refused"],
                f'28000: columnar: access to "s3://{BUCKET}/db/t/metadata/M.metadata.json" was '
                f"denied (HTTP 403) | HINT: {VENDED_HINT}",
                "vended key refused: the 403 hint names the catalog's vended credentials")


def test_a_vended_entry_key_refusal_hints_at_the_vended_credentials(ea, expect):
    if _declined(ea, expect):
        return
    expect.text(ea["vended entry refused"],
                f'28000: columnar: access to "s3://{BUCKET}/db/t/metadata/M.metadata.json" was '
                f"denied (HTTP 403) | HINT: {VENDED_HINT}",
                "vended key refused through a storage-credentials entry: the 403 hint names the "
                "catalog's vended credentials")


def test_an_allow_list_refusal_with_an_empty_list_suggests_host_and_port(ea, expect):
    if _declined(ea, expect):
        return
    port = ea["port"]
    expect.text(ea["list empty"],
                f'42501: columnar: endpoint "127.0.0.1:{port}" is not in '
                "pgcolumnar.objstore_allowed_endpoints | HINT: A superuser can add it: ALTER SYSTEM "
                f"SET pgcolumnar.objstore_allowed_endpoints = '127.0.0.1:{port}'; "
                "SELECT pg_reload_conf();",
                "allow-list refusal, empty list: the hint suggests host:port")


def test_an_allow_list_refusal_keeps_the_list_and_appends_host_and_port(ea, expect):
    if _declined(ea, expect):
        return
    port = ea["port"]
    expect.text(ea["list add"],
                f'42501: columnar: endpoint "127.0.0.1:{port}" is not in '
                "pgcolumnar.objstore_allowed_endpoints | HINT: A superuser can add it: ALTER SYSTEM "
                "SET pgcolumnar.objstore_allowed_endpoints = "
                f"'127.0.0.1:1,example.com,127.0.0.1:{port}'; SELECT pg_reload_conf();",
                "allow-list refusal: the hint keeps the current entries and appends host:port")


def test_an_allow_list_refusal_doubles_a_quote_in_the_list(ea, expect):
    if _declined(ea, expect):
        return
    expect.text(ea["list quote"],
                "A superuser can add it: ALTER SYSTEM SET pgcolumnar.objstore_allowed_endpoints = "
                f"'o''host,127.0.0.1:{ea['port']}'; SELECT pg_reload_conf();",
                "allow-list refusal: a quote in the current list is doubled in the hint")


def test_the_list_the_hint_suggests_permits_the_endpoint(ea, expect):
    if _declined(ea, expect):
        return
    expect.text(ea["list permits"], "1000",
                "allow-list refusal: the list the hint suggests permits the endpoint")


def test_the_mid_size_export_is_one_put(ea, expect):
    if _declined(ea, expect):
        return
    expect.text(ea["mid one put"], "yes",
                "premise: the mid-size export is one PUT of more than 4 MiB")


def test_a_small_export_to_the_refused_bucket_reports_the_403(ea, expect):
    if _declined(ea, expect):
        return
    expect.text(ea["small denied"],
                f'28000: columnar: access to "s3://{DENIED}/s.parquet" was denied '
                f"(HTTP 403: SignatureDoesNotMatch) | HINT: {ENV_HINT}",
                "a small export to the refused bucket reports the 403 and its hint")


def test_a_mid_size_export_refused_early_keeps_the_403(ea, expect):
    if _declined(ea, expect):
        return
    expect.text(ea["mid denied"],
                f'28000: columnar: access to "s3://{DENIED}/m.parquet" was denied '
                f"(HTTP 403: SignatureDoesNotMatch) | HINT: {ENV_HINT}",
                "mid-size export refused before its body was read: the 403 and its hint survive")


def test_a_mid_size_export_over_https_answered_100_continue_writes_the_object(ea, expect):
    if _declined(ea, expect):
        return
    expect.text(ea["tls mid"], "300000 exported, 300000 read back",
                "a mid-size export over https whose store answers 100 Continue writes the object")


def test_the_mid_size_put_over_https_carries_expect(ea, expect):
    if _declined(ea, expect):
        return
    expect.text(ea["tls mid expect"], "1", "the mid-size PUT over https carried Expect: 100-continue")


def test_a_small_export_over_https_sends_no_expect(ea, expect):
    if _declined(ea, expect):
        return
    expect.text(ea["tls small"], "yes", "a small export over https sends no Expect header")


def test_a_mid_size_export_over_https_refused_early_keeps_the_403(ea, expect):
    if _declined(ea, expect):
        return
    expect.text(ea["tls denied"],
                f'28000: columnar: access to "s3://{DENIED}/tm.parquet" was denied '
                f"(HTTP 403: SignatureDoesNotMatch) | HINT: {MAPPING_HINT}",
                "mid-size export over https refused before its body was sent: the 403 and its hint survive")


def test_a_store_that_ignores_expect_gets_the_body_after_the_wait(ea, expect):
    if _declined(ea, expect):
        return
    expect.text(ea["tls ignored"], "300000 exported, 300000 read back",
                "a mid-size export over https to a store that ignores Expect is sent after the wait")


def test_a_late_100_continue_is_passed_over_for_the_final_answer(ea, expect):
    if _declined(ea, expect):
        return
    expect.text(ea["tls late"], "300000 exported, 300000 read back",
                "a 100 Continue that comes after the wait is passed over for the final answer")


def test_a_417_sends_the_export_again_without_expect(ea, expect):
    if _declined(ea, expect):
        return
    expect.text(ea["tls 417"], "300000 exported, 300000 read back",
                "a store that answers 417 to Expect gets the export again without it")


def test_the_multipart_export_to_an_expect_ignoring_store_is_several_parts(ea, expect):
    if _declined(ea, expect):
        return
    expect.text(ea["mp parts"], "yes",
                "premise: the multipart export to a store that ignores Expect is 3 or more parts, "
                "all read back")


def test_a_store_that_ignores_expect_gets_it_on_one_part_only(ea, expect):
    if _declined(ea, expect):
        return
    expect.text(ea["mp expect"], "1",
                "a multipart export to a store that ignores Expect sends it on one part only")


def test_a_200_before_the_body_still_sends_the_body(ea, expect):
    if _declined(ea, expect):
        return
    expect.text(ea["early 200"], "300000 | 300000",
                "a single PUT answered 200 before its body still sends the body, and the object "
                "holds the rows")


def test_a_refusal_after_the_expect_wait_keeps_the_403_over_http(ea, expect):
    if _declined(ea, expect):
        return
    expect.text(ea["late denied"],
                f'28000: columnar: access to "s3://{DENIED}/late.parquet" was denied '
                f"(HTTP 403: SignatureDoesNotMatch) | HINT: {ENV_HINT}",
                "mid-size export refused after the Expect wait ran out (http): the 403 and its hint survive")


def test_the_server_survived_the_refusals(ea, expect):
    if _declined(ea, expect):
        return
    expect.text(ea["survived"], "1", "the server survived the refusals")
