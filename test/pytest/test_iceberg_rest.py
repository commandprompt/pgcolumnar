"""Iceberg REST Catalog client, 7a (#388 phase 7): the pytest twin of
`test/iceberg_rest.sh`, every check by the shell's name (#1341).

iceberg_rest_table_location(catalog, ns, table) resolves a table named by a REST
catalog into its current metadata location, against the shell's own hermetic fixture
(test/iceberg_rest_server.py), which verifies the Authorization: Bearer header. The
token lives in the POSTMASTER ENVIRONMENT (PGCOLUMNAR_ICEBERG_REST_TOKEN), never in
SQL, so it never reaches the server log or the catalog's request log. A wrong token
and no token are refused 28000 by the catalog; an endpoint off the allow-list and a
link-local host are refused 42501, each by its own guard (the message is read, since
both raise 42501); an over-cap response is refused 54000, an unknown table 42P01 and
a non-object config XX001, each by its own message.

A BACKEND THAT DIED DOES NOT PASS AS ONE THAT SURVIVED: survival also reads the
private cluster's server log for killed backends, and an unreadable log fails it.

A PRIVATE CLUSTER, because the postmaster is restarted with the token in its
environment, which the shared cluster cannot do. It starts with the token variable of
this process removed. Without the object-store module the first test declines under
the shell's pgc_skip message and the rest decline.
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
TOKEN = "s3cr3t-rest-token-do-not-log"
TOKEN_ENV = "PGCOLUMNAR_ICEBERG_REST_TOKEN"
MDLOC = "file:///tmp/pgc_ice_rest/db/events/metadata/00001.metadata.json"
LOC = "SELECT pgcolumnar.iceberg_rest_table_location('{}','db','{}')"


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _wait_up(dsn, seconds=30):
    """Wait, bounded, for the server to answer, so a crash in one arm fails only that
    arm and the survival check, not every reading taken during crash recovery."""
    for _ in range(seconds * 2):
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


def _refusal(dsn, sql, phrase, label, setting=None):
    """-> "<SQLSTATE><label>" when `sql` raised with `phrase` in its message, the bare
    SQLSTATE when the message lacks it, or what happened instead. `setting` is SET on
    the same session first."""
    _wait_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            if setting:
                conn.execute(setting)
            conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        if not exc.sqlstate:
            return query_error(_message(exc))
        return exc.sqlstate + (label if phrase in str(exc) else "")
    return "no error"


def _count(path, needle):
    """-> how many lines of `path` hold `needle`, or a failed reading."""
    try:
        return sum(needle in line for line in pathlib.Path(path).read_text(errors="replace").splitlines())
    except OSError as exc:
        return query_error(f"{path} unreadable: {exc}")


def _lines(path):
    try:
        return sum(1 for line in pathlib.Path(path).read_text(errors="replace").splitlines() if line)
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
    """pg_ctl with an explicit environment: this process's token variable removed, then
    `extra_env` added. Dropped to postgres when running as root, as the harness does."""
    unset = ["-u", TOKEN_ENV] if TOKEN_ENV in os.environ else []
    cmd = ["env"] + unset + [f"{k}={v}" for k, v in extra_env.items()] + \
          [os.path.join(cluster.bindir, "pg_ctl")] + args
    if os.geteuid() == 0:
        cmd = ["runuser", "-u", "postgres", "--"] + cmd
    return subprocess.run(cmd, capture_output=True, text=True)


def _restart(cluster, datadir, log, env):
    """Restart with `env`; a start that fails is retried, as a transient port or lock
    clash on a busy host should not read as every later arm failing. A start that never
    succeeds leaves the readings to fail by name (connection refused)."""
    _pg_ctl(cluster, ["-D", str(datadir), "-m", "fast", "-w", "stop"], {})
    for _ in range(5):
        if _pg_ctl(cluster, ["-D", str(datadir), "-l", log, "-w", "start"], env).returncode == 0:
            return True
        time.sleep(1)
    return False


def _rest_server(work, name, port, *args):
    """Start the shell's REST fixture; -> (process, its READY count)."""
    out = work / f"{name}.out"
    proc = subprocess.Popen([sys.executable, str(TEST / "iceberg_rest_server.py"), "--port", str(port),
                             "--log", str(work / f"{name}.log"), "--metadata-location", MDLOC, *args],
                            stdout=open(out, "w"), stderr=subprocess.STDOUT)
    for _ in range(50):
        if out.is_file() and "READY" in out.read_text(errors="replace"):
            break
        time.sleep(0.1)
    return proc, (out.read_text(errors="replace").count("READY") if out.is_file() else 0)


def _objstore_installed(pg_config):
    try:
        lib = subprocess.run([pg_config, "--pkglibdir"], capture_output=True, text=True).stdout.strip()
    except OSError:
        return False
    return bool(lib) and (pathlib.Path(lib) / "pgcolumnar_objstore.so").is_file()


@pytest.fixture(scope="module")
def ir(pgc_cluster):
    if not _objstore_installed(pgc_cluster.pg_config):
        yield None
        return
    from pgc_cluster import Cluster, pick_port

    work = pathlib.Path(tempfile.mkdtemp(prefix="pgc-icerest-"))
    os.chmod(work, 0o777)
    rest_port = pick_port(31)
    srv, ready = _rest_server(work, "rest", rest_port, "--token", TOKEN,
                              "--namespace", "db", "--table", "events")
    cat = f"http://127.0.0.1:{rest_port}"
    bad, bad_port = None, pick_port(32)
    datadir = work / "data"
    datadir.mkdir()
    log = str(work / "server.log")
    rest_log = str(work / "rest.log")
    cluster = Cluster(pgc_cluster.pg_config, "icerest", datadir, pick_port(33))
    r = {"server up": ready}
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
        before = _crashes(log)

        _restart(cluster, datadir, log, {TOKEN_ENV: TOKEN})
        r["resolve"] = _value(dsn, LOC.format(cat, "events"))
        r["auth header"] = _count(rest_log, "GET /v1/namespaces/db/tables/events AUTH=yes")

        def run(*stmts):
            _wait_up(dsn)
            with psycopg.connect(dsn, autocommit=True) as c:
                for s in stmts:
                    c.execute(s)

        run("ALTER SYSTEM SET log_statement='all'", "SELECT pg_reload_conf()")
        _value(dsn, LOC.format(cat, "events"))
        run("ALTER SYSTEM SET log_statement='none'", "SELECT pg_reload_conf()")
        r["log lines"] = _lines(log)
        r["token in pg log"] = _count(log, TOKEN)
        r["token in rest log"] = _count(rest_log, TOKEN)

        _restart(cluster, datadir, log, {TOKEN_ENV: "wrong-token"})
        r["wrong token"] = _refusal(dsn, LOC.format(cat, "events"),
                                    "the REST catalog refused the request", " by the catalog")
        _restart(cluster, datadir, log, {})
        r["anonymous"] = _refusal(dsn, LOC.format(cat, "events"),
                                  "the REST catalog refused the request", " by the catalog")
        _restart(cluster, datadir, log, {TOKEN_ENV: TOKEN})

        r["off list"] = _refusal(dsn, LOC.format(f"http://127.0.0.2:{rest_port}", "events"),
                                 "is not in pgcolumnar.objstore_allowed_endpoints", " by the allow-list",
                                 "SET pgcolumnar.objstore_allowed_endpoints='127.0.0.1'")
        r["link local"] = _refusal(dsn, LOC.format("http://169.254.169.254/", "events"),
                                   "resolves into a link-local range", " by the link-local guard",
                                   "SET pgcolumnar.objstore_allowed_endpoints='169.254.169.254'")
        r["over cap"] = _refusal(dsn, LOC.format(cat, "toobig"), "exceeded", " by the cap",
                                 "SET pgcolumnar.objstore_allowed_endpoints='127.0.0.1'")
        r["unknown"] = _refusal(dsn, LOC.format(cat, "nosuch"), "the REST catalog has no such table",
                                " by the catalog's 404", "SET pgcolumnar.objstore_allowed_endpoints='127.0.0.1'")
        bad, _ = _rest_server(work, "badcfg", bad_port, "--bad-config")
        r["bad config"] = _refusal(dsn, LOC.format(f"http://127.0.0.1:{bad_port}", "events"),
                                   "config response is not a JSON object", " by the config guard",
                                   "SET pgcolumnar.objstore_allowed_endpoints='127.0.0.1'")
        bad.kill()
        bad.wait()
        bad = None
        r["survived"] = _survived(dsn, log, before)
        yield r
    finally:
        _pg_ctl(cluster, ["-D", str(datadir), "-m", "immediate", "-w", "stop"], {})
        for p in (srv, bad):
            if p is not None:
                p.kill()
                p.wait()
        shutil.rmtree(work, ignore_errors=True)


def _declined(ir, expect):
    if ir is not None:
        return False
    expect.cannot_run("MISSING_DEPENDENCY", "the object-store module is not installed")
    return True


def test_the_rest_fixture_server_is_up(ir, expect):
    """The shell skips the whole suite without python3 or the object-store module, under
    these names."""
    expect.require_module("json", name="python3 is needed")
    if ir is None:
        expect.cannot_run("MISSING_DEPENDENCY", "pgcolumnar_objstore.so is not in the pkglibdir",
                          name="the object-store module is not installed")
        return
    expect.text(str(ir["server up"]), "1", "premise: rest fixture server is up")


def test_loadtable_resolves_the_current_metadata_location(ir, expect):
    if _declined(ir, expect):
        return
    expect.text(ir["resolve"], MDLOC, "loadTable resolves the current metadata-location")


def test_the_loadtable_request_carried_an_authorization_header(ir, expect):
    if _declined(ir, expect):
        return
    expect.text(str(ir["auth header"]), "1", "the loadTable request carried an Authorization header")


def test_the_rest_pg_server_log_holds_lines_to_search(ir, expect):
    if _declined(ir, expect):
        return
    n = ir["log lines"]
    expect.text("yes" if isinstance(n, int) and n > 0 else f"no ({n})", "yes",
                "premise: the PG server log holds lines to search")


def test_the_rest_token_never_appears_in_the_server_log(ir, expect):
    if _declined(ir, expect):
        return
    expect.text(str(ir["token in pg log"]), "0", "the token never appears in the server (PG) log")


def test_the_rest_token_never_appears_in_the_catalog_request_log(ir, expect):
    if _declined(ir, expect):
        return
    expect.text(str(ir["token in rest log"]), "0", "the token value never appears in the catalog request log")


def test_a_wrong_bearer_token_is_refused(ir, expect):
    if _declined(ir, expect):
        return
    expect.text(ir["wrong token"], "28000 by the catalog", "a wrong bearer token is refused (28000)")


def test_an_anonymous_request_to_a_token_protected_catalog_is_refused(ir, expect):
    if _declined(ir, expect):
        return
    expect.text(ir["anonymous"], "28000 by the catalog",
                "an anonymous request to a token-protected catalog is refused (28000)")


def test_a_catalog_endpoint_off_the_allow_list_is_refused(ir, expect):
    if _declined(ir, expect):
        return
    expect.text(ir["off list"], "42501 by the allow-list", "a catalog endpoint off the allow-list is refused (42501)")


def test_a_link_local_catalog_host_is_refused_even_if_allow_listed(ir, expect):
    if _declined(ir, expect):
        return
    expect.text(ir["link local"], "42501 by the link-local guard",
                "a link-local catalog host is refused even if allow-listed (42501)")


def test_an_over_cap_catalog_response_is_refused(ir, expect):
    if _declined(ir, expect):
        return
    expect.text(ir["over cap"], "54000 by the cap", "an over-cap catalog response is refused (54000)")


def test_an_unknown_rest_table_surfaces_a_clean_not_found(ir, expect):
    if _declined(ir, expect):
        return
    expect.text(ir["unknown"], "42P01 by the catalog's 404", "an unknown table surfaces a clean not-found (42P01)")


def test_a_non_object_catalog_config_is_refused(ir, expect):
    if _declined(ir, expect):
        return
    expect.text(ir["bad config"], "XX001 by the config guard",
                "a non-object catalog config is refused, not walked (XX001)")


def test_the_backend_is_still_up_after_the_rest_refusals(ir, expect):
    if _declined(ir, expect):
        return
    expect.text(ir["survived"], "1", "backend still up after the REST refusals")
