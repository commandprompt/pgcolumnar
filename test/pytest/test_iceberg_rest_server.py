"""Iceberg REST catalog FOREIGN SERVER and USER MAPPING credentials (#656): the pytest
twin of `test/iceberg_rest_server.sh`, every check by the shell's name (#1341).

The iceberg_rest_* functions accept a FOREIGN SERVER name in place of a catalog URI:
the server holds catalog_uri (and optionally a warehouse, sent on GET /v1/config), and
the current role's USER MAPPING holds the bearer token or an OAuth2 client credential,
role-private and never a SQL argument or a log line. Against the shell's own REST
fixture (test/iceberg_rest_server.py), which 401s a wrong or absent token and logs every
request: a server-name call authenticates with the mapping token and with no token in
the environment; the token and the OAuth2 client secret never reach the server log; a
wrong token, a missing one, and a wrong OAuth2 secret are each refused by their own
message; a half OAuth2 credential is refused before any request, even beside a static
token; a static token wins over a complete pair, with no token request; secrets are rejected on a SERVER and catalog_uri on a USER MAPPING; an
oauth_token_uri off the endpoint allow-list (link-local or any other host) is refused
by the allow-list itself; the URI form works with the environment token; and a
non-superuser never borrows the environment token unless a superuser marked its
mapping credentials_required 'false'.

A STATEMENT THAT RAN IS NOT A SERVER THAT DIDN'T ANSWER: an "accepted" arm reads "ok"
only when the statement ran, and a failed connection fails it.

A BACKEND THAT DIED DOES NOT PASS AS ONE THAT SURVIVED: survival also reads the private
cluster's server log for killed backends, and every reading first waits for the server.

A PRIVATE CLUSTER, because the postmaster is restarted without and then with the token
in its environment, which the shared cluster cannot do; its roles and servers are its
own. Every AWS_* variable of this process is removed before each start.
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
TOKEN = "s3cr3t-mapping-token-xyz"
MDLOC = "file:///tmp/pgc_rest/db/events/metadata/00001.metadata.json"
OCID, OCSEC, OATOKEN = "client-abc", "s3cr3t-oauth-secret", "minted-bearer-777"
LOCATE = "SELECT pgcolumnar.iceberg_rest_table_location('{}','db','events')"


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _dsn_as(dsn, role):
    return dsn.replace("user=postgres", f"user={role}")


def _await_up(dsn):
    """Wait, at most 60 s, until the server answers, so a reading after a crash reads the
    catalog rather than crash recovery. The crash itself is still caught: _survived counts it."""
    for _ in range(120):
        try:
            with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as conn:
                conn.execute("SELECT 1")
            return
        except psycopg.Error:
            time.sleep(0.5)


def _value(dsn, sql):
    """-> the first column of the first row as text, or the failure as a query_error()."""
    _await_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    if row is None or row[0] is None:
        return query_error(f"no value from {sql}")
    return str(row[0])


def _run(dsn, sql):
    """Run a setup statement; a failure here shows in the readings that depend on it."""
    _await_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            conn.execute(sql)
    except psycopg.Error:
        pass


def _sqlstate(dsn, sql):
    """-> the SQLSTATE `sql` failed with, "ok" when it ran, or a query_error() when the
    server could not be asked: a dead server is not a statement that was accepted."""
    _await_up(dsn)
    try:
        conn = psycopg.connect(dsn, autocommit=True, connect_timeout=10)
    except psycopg.Error as exc:
        return query_error(_message(exc))
    try:
        with conn:
            conn.execute(sql)
    except psycopg.Error as exc:
        return exc.sqlstate or query_error(_message(exc))
    return "ok"


def _refusal(dsn, sql, phrase):
    """-> the SQLSTATE when `sql` is refused with `phrase` in its message, "SQLSTATE
    (another refusal: MESSAGE)" when something else refused it, or what happened instead.
    28000 and 42501 each come from more than one guard here."""
    _await_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        if not exc.sqlstate:
            return query_error(_message(exc))
        msg = exc.diag.message_primary or ""
        return exc.sqlstate if phrase in msg else f"{exc.sqlstate} (another refusal: {msg})"
    return "no error"


def _lines(path):
    """-> the non-empty lines of `path`, or a query_error() when it can't be read."""
    try:
        return [l for l in pathlib.Path(path).read_text(errors="replace").splitlines() if l]
    except OSError as exc:
        return query_error(f"{path} unreadable: {exc}")


def _count_in(path, needle):
    lines = _lines(path)
    return lines if isinstance(lines, str) else str(sum(1 for l in lines if needle in l))


def _has_lines(path):
    lines = _lines(path)
    return lines if isinstance(lines, str) else ("yes" if lines else "no")


def _nlines(path):
    lines = _lines(path)
    return lines if isinstance(lines, str) else len(lines)


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
    unset += ["-u", "PGCOLUMNAR_ICEBERG_REST_TOKEN"]
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


def _set_log_statement(dsn, value):
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(f"ALTER SYSTEM SET log_statement = '{value}'")
        c.execute("SELECT pg_reload_conf()")


@pytest.fixture(scope="module")
def irs(pgc_cluster):
    if not _module_installed(pgc_cluster.pg_config):
        yield None
        return
    from pgc_cluster import Cluster, pick_port

    work = pathlib.Path(tempfile.mkdtemp(prefix="pgc-icesrv-"))
    os.chmod(work, 0o777)
    procs = []

    def start_rest(log, *extra):
        port = pick_port(33)
        out = work / f"rest_{port}.out"
        procs.append(subprocess.Popen(
            [sys.executable, str(TEST / "iceberg_rest_server.py"), "--port", str(port), "--log", str(log),
             "--namespace", "db", "--table", "events", "--metadata-location", MDLOC, *extra],
            stdout=open(out, "w"), stderr=subprocess.STDOUT))
        _ready(out)
        return port, out

    rest_log = work / "rest.log"
    port, rest_out = start_rest(rest_log, "--token", TOKEN)
    cat = f"http://127.0.0.1:{port}"
    datadir = work / "data"
    datadir.mkdir()
    log = str(work / "server.log")
    cluster = Cluster(pgc_cluster.pg_config, "icesrv", datadir, pick_port(34))
    r = {"rest up": rest_out.read_text(errors="replace").count("READY") if rest_out.is_file() else 0}
    try:
        cluster.initdb()
        # NO token in the environment: only a USER MAPPING can authenticate.
        started = _pg_ctl(cluster, ["-D", str(datadir), "-l", log, "-w", "start"], {})
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
            c.execute(f"CREATE SERVER cat FOREIGN DATA WRAPPER pgcolumnar_iceberg_catalog"
                      f" OPTIONS (catalog_uri '{cat}')")
            c.execute(f"CREATE USER MAPPING FOR postgres SERVER cat OPTIONS (token '{TOKEN}')")
        before = _crashes(log)

        # ---- a server-name call authenticates with the mapping token
        r["mapping"] = _value(dsn, LOCATE.format("cat"))
        r["auth header"] = _count_in(rest_log, "GET /v1/namespaces/db/tables/events AUTH=yes")

        # ---- the mapping token never appears in the server log
        _set_log_statement(dsn, "all")
        _value(dsn, LOCATE.format("cat"))
        _set_log_statement(dsn, "none")
        r["log lines token"] = _has_lines(log)
        r["token in log"] = _count_in(log, TOKEN)

        # ---- a wrong mapping token, then none at all
        _run(dsn, "ALTER USER MAPPING FOR postgres SERVER cat OPTIONS (SET token 'wrong-token')")
        r["wrong token"] = _refusal(dsn, LOCATE.format("cat"), "refused the request (HTTP 401")
        _run(dsn, "DROP USER MAPPING FOR postgres SERVER cat")
        r["no token"] = _refusal(dsn, LOCATE.format("cat"), "refused the request (HTTP 401")
        _run(dsn, f"CREATE USER MAPPING FOR postgres SERVER cat OPTIONS (token '{TOKEN}')")

        # ---- validator: secrets only on a user mapping, uri only on a server
        r["token on server"] = _sqlstate(dsn, "CREATE SERVER bad1 FOREIGN DATA WRAPPER pgcolumnar_iceberg_catalog"
                                              f" OPTIONS (catalog_uri '{cat}', token 'x')")
        r["uri on mapping"] = _sqlstate(dsn, "ALTER USER MAPPING FOR postgres SERVER cat OPTIONS (ADD catalog_uri 'x')")

        # ---- #656: a warehouse server option is accepted and sent on GET /v1/config
        r["warehouse"] = _sqlstate(dsn, "CREATE SERVER whcat FOREIGN DATA WRAPPER pgcolumnar_iceberg_catalog"
                                        f" OPTIONS (catalog_uri '{cat}', warehouse 'wh1')")
        _run(dsn, f"CREATE USER MAPPING FOR postgres SERVER whcat OPTIONS (token '{TOKEN}')")
        r["warehouse location"] = _value(dsn, LOCATE.format("whcat"))
        r["warehouse sent"] = _count_in(rest_log, "GET /v1/config?warehouse=wh1 ")

        # ---- OAuth2 client-credentials: mint a bearer, then use it
        olog = work / "orest.log"
        oport, _ = start_rest(olog, "--token", OATOKEN, "--oauth-client-id", OCID, "--oauth-client-secret", OCSEC)
        ocat = f"http://127.0.0.1:{oport}"
        _run(dsn, f"CREATE SERVER ocat FOREIGN DATA WRAPPER pgcolumnar_iceberg_catalog OPTIONS (catalog_uri '{ocat}')")
        _run(dsn, f"CREATE USER MAPPING FOR postgres SERVER ocat"
                  f" OPTIONS (oauth_client_id '{OCID}', oauth_client_secret '{OCSEC}')")
        r["oauth read"] = _value(dsn, LOCATE.format("ocat"))
        r["oauth post"] = _count_in(olog, "POST /v1/oauth/tokens")
        r["oauth bearer"] = _count_in(olog, "GET /v1/namespaces/db/tables/events AUTH=yes")
        r["secret in fixture log"] = _count_in(olog, OCSEC)
        _set_log_statement(dsn, "all")
        _value(dsn, LOCATE.format("ocat"))
        _set_log_statement(dsn, "none")
        r["log lines secret"] = _has_lines(log)
        r["secret in log"] = _count_in(log, OCSEC)

        _run(dsn, "ALTER USER MAPPING FOR postgres SERVER ocat OPTIONS (SET oauth_client_secret 'wrong-secret')")
        r["wrong secret"] = _refusal(dsn, LOCATE.format("ocat"), "refused the OAuth2")
        _run(dsn, "ALTER USER MAPPING FOR postgres SERVER ocat OPTIONS (DROP oauth_client_secret)")
        # "before any request" is counted: the fixture logs every request it gets
        n0 = _nlines(olog)
        half = _refusal(dsn, LOCATE.format("ocat"), "incomplete OAuth2 credential")
        n1 = _nlines(olog)
        r["half"] = f"{half}|{n1 - n0}" if isinstance(n0, int) and isinstance(n1, int) else query_error(
            f"fixture log unreadable: {n0} / {n1}")
        # a static token does not excuse a half pair (#1794); both halves, then back to id-only
        _run(dsn, "ALTER USER MAPPING FOR postgres SERVER ocat OPTIONS (ADD token 'static-tok')")
        n0 = _nlines(olog)
        half = _refusal(dsn, LOCATE.format("ocat"), "incomplete OAuth2 credential")
        n1 = _nlines(olog)
        r["half id token"] = f"{half}|{n1 - n0}" if isinstance(n0, int) and isinstance(n1, int) else query_error(
            f"fixture log unreadable: {n0} / {n1}")
        _run(dsn, "ALTER USER MAPPING FOR postgres SERVER ocat"
                  f" OPTIONS (DROP oauth_client_id, ADD oauth_client_secret '{OCSEC}')")
        n0 = _nlines(olog)
        half = _refusal(dsn, LOCATE.format("ocat"), "incomplete OAuth2 credential")
        n1 = _nlines(olog)
        r["half secret token"] = f"{half}|{n1 - n0}" if isinstance(n0, int) and isinstance(n1, int) else query_error(
            f"fixture log unreadable: {n0} / {n1}")
        _run(dsn, "ALTER USER MAPPING FOR postgres SERVER ocat"
                  f" OPTIONS (DROP token, DROP oauth_client_secret, ADD oauth_client_id '{OCID}')")
        r["oauth on mapping"] = _sqlstate(dsn, "ALTER USER MAPPING FOR postgres SERVER ocat"
                                               " OPTIONS (ADD oauth_scope 'catalog')")
        # a token wins over a complete pair and mints no bearer. The secret is wrong, so
        # a mint would be refused; the catalog takes only the fixture's minted bearer,
        # given here as the static token. "no error" means the static token was sent;
        # the count is the requests to the token endpoint.
        _run(dsn, "ALTER USER MAPPING FOR postgres SERVER ocat"
                  f" OPTIONS (ADD token '{OATOKEN}', ADD oauth_client_secret 'wrong-secret')")
        tw0 = _lines(olog)
        won = _refusal(dsn, LOCATE.format("ocat"), "@@ no refusal is expected @@")
        tw1 = _lines(olog)
        r["token wins"] = (f"{won}|{sum(1 for l in tw1[len(tw0):] if 'POST /v1/oauth/tokens' in l)}"
                           if isinstance(tw0, list) and isinstance(tw1, list)
                           else query_error(f"fixture log unreadable: {tw0} / {tw1}"))
        _run(dsn, "ALTER USER MAPPING FOR postgres SERVER ocat OPTIONS (DROP token, DROP oauth_client_secret)")
        r["oauth on server"] = _sqlstate(dsn, "CREATE SERVER bad2 FOREIGN DATA WRAPPER pgcolumnar_iceberg_catalog"
                                              f" OPTIONS (catalog_uri '{ocat}', oauth_client_secret 'x')")

        # ---- SSRF: a hostile oauth_token_uri is refused by the endpoint allow-list
        _run(dsn, f"ALTER USER MAPPING FOR postgres SERVER ocat OPTIONS (ADD oauth_client_secret '{OCSEC}')")
        _run(dsn, "ALTER USER MAPPING FOR postgres SERVER ocat"
                  " OPTIONS (ADD oauth_token_uri 'http://169.254.169.254/v1/oauth/tokens')")
        r["link-local"] = _refusal(dsn, LOCATE.format("ocat"), "is not in pgcolumnar.objstore_allowed_endpoints")
        _run(dsn, "ALTER USER MAPPING FOR postgres SERVER ocat"
                  " OPTIONS (SET oauth_token_uri 'http://10.11.12.13/v1/oauth/tokens')")
        r["off list"] = _refusal(dsn, LOCATE.format("ocat"), "is not in pgcolumnar.objstore_allowed_endpoints")

        # ---- the URI form still works when the environment token is present
        _restart(cluster, datadir, log, {"PGCOLUMNAR_ICEBERG_REST_TOKEN": TOKEN})
        _await_up(dsn)
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("ALTER SYSTEM SET pgcolumnar.objstore_allowed_endpoints = '127.0.0.1'")
            c.execute("SELECT pg_reload_conf()")
        r["uri form"] = _value(dsn, LOCATE.format(cat))

        # ---- a non-superuser does not borrow the server's environment token
        _run(dsn, "CREATE ROLE ice_r LOGIN NOSUPERUSER")
        for g in ("GRANT pg_read_server_files TO ice_r", "GRANT USAGE ON SCHEMA pgcolumnar TO ice_r",
                  "GRANT USAGE ON FOREIGN SERVER cat TO ice_r"):
            _run(dsn, g)
        ice = _dsn_as(dsn, "ice_r")
        n0 = _nlines(rest_log)
        refused = _sqlstate(ice, LOCATE.format("cat"))
        n1 = _nlines(rest_log)
        r["ice_r refused"] = f"{refused}|{n1 - n0}" if isinstance(n0, int) and isinstance(n1, int) else query_error(
            f"fixture log unreadable: {n0} / {n1}")
        _run(dsn, "CREATE USER MAPPING FOR ice_r SERVER cat")
        r["ice_r own flag"] = _sqlstate(ice, "ALTER USER MAPPING FOR ice_r SERVER cat"
                                             " OPTIONS (ADD credentials_required 'false')")
        _run(dsn, "ALTER USER MAPPING FOR ice_r SERVER cat OPTIONS (ADD credentials_required 'false')")
        r["ice_r control"] = _value(ice, LOCATE.format("cat"))

        r["survived"] = _survived(dsn, log, before)
        yield r
    finally:
        _pg_ctl(cluster, ["-D", str(datadir), "-m", "immediate", "-w", "stop"], {})
        for p in procs:
            p.kill()
            p.wait()
        shutil.rmtree(work, ignore_errors=True)


def _declined(irs, expect):
    if irs is not None:
        return False
    expect.cannot_run("MISSING_DEPENDENCY", "pgcolumnar_objstore.so is not in the pkglibdir")
    return True


def test_the_rest_server_suite_can_run_here(irs, expect):
    """The shell skips the whole suite under each of these names."""
    expect.require_module("json", name="python3 is needed")
    if irs is None:
        expect.cannot_run("MISSING_DEPENDENCY", "pgcolumnar_objstore.so is not in the pkglibdir",
                          name="the object-store module is not installed")
        return
    expect.text(str(irs["rest up"]), "1", "premise: rest fixture up")


def test_a_server_name_call_reads_via_the_mapping_token(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["mapping"], MDLOC, "iceberg_rest_table_location('cat',...) reads via the mapping token")


def test_the_request_carried_an_authorization_header(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["auth header"], "1", "the request carried an Authorization header")


def test_the_server_log_holds_lines_for_the_mapping_token(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["log lines token"], "yes", "premise: the PG server log holds lines to search, for the mapping token")


def test_the_mapping_token_is_not_in_the_server_log(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["token in log"], "0", "the mapping token never appears in the PG server log")


def test_a_wrong_mapping_token_is_refused(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["wrong token"], "28000", "a wrong mapping token is refused (28000)")


def test_no_mapping_and_no_env_token_is_refused(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["no token"], "28000", "no mapping and no env token is refused, not bypassed (28000)")


def test_a_token_on_the_server_is_rejected(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["token on server"], "HV00D", "a token option on the SERVER is rejected (HV00D)")


def test_a_catalog_uri_on_the_mapping_is_rejected(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["uri on mapping"], "HV00D", "a catalog_uri option on the USER MAPPING is rejected (HV00D)")


def test_a_warehouse_option_on_the_server_is_accepted(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["warehouse"], "ok", "a warehouse option on the SERVER is accepted (not HV00D)")


def test_a_warehouse_server_resolves_a_location(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["warehouse location"], MDLOC, "a warehouse-configured server resolves a table location")


def test_the_warehouse_is_sent_on_get_config(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["warehouse sent"], "1", "the warehouse was sent on GET /v1/config (?warehouse=wh1)")


def test_an_oauth_mapping_reads_by_a_minted_bearer(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["oauth read"], MDLOC, "a server with an OAuth2 mapping reads by a minted bearer")


def test_the_oauth_token_endpoint_was_called(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["oauth post"], "1", "the OAuth2 token endpoint was called (POST /v1/oauth/tokens)")


def test_load_table_carried_the_minted_bearer(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["oauth bearer"], "1", "loadTable then carried the minted bearer (Authorization present)")


def test_the_client_secret_is_not_in_the_fixture_log(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["secret in fixture log"], "0", "the client secret never appears in the fixture request log")


def test_the_server_log_holds_lines_for_the_client_secret(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["log lines secret"], "yes", "premise: the PG server log holds lines to search, for the client secret")


def test_the_client_secret_is_not_in_the_server_log(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["secret in log"], "0", "the client secret never appears in the PG server log")


def test_a_wrong_oauth_client_secret_is_refused(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["wrong secret"], "28000", "a wrong OAuth2 client secret is refused (28000)")


def test_a_half_oauth_credential_is_refused_before_any_request(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["half"], "28000|0",
                "a half OAuth2 credential (id, no secret) is refused before any request (28000)")


def test_a_half_oauth_id_is_refused_even_with_a_static_token(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["half id token"], "28000|0",
                "a half OAuth2 credential (id, no secret) is refused even with a static token (28000)")


def test_a_half_oauth_secret_is_refused_even_with_a_static_token(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["half secret token"], "28000|0",
                "a half OAuth2 credential (secret, no id) is refused even with a static token (28000)")


def test_oauth_options_are_accepted_on_a_mapping(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["oauth on mapping"], "ok", "oauth options are accepted on a USER MAPPING (no error)")


def test_a_static_token_wins_over_a_complete_oauth_pair(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["token wins"], "no error|0",
                "a static token and a complete OAuth2 pair read by the token, with no token request")


def test_an_oauth_option_on_the_server_is_rejected(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["oauth on server"], "HV00D", "an oauth option on the SERVER is rejected (HV00D)")


def test_a_link_local_token_uri_is_refused_by_the_allow_list(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["link-local"], "42501", "a link-local oauth_token_uri is refused by the allow-list (42501)")


def test_an_off_list_token_uri_host_is_refused(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["off list"], "42501", "an off-allow-list oauth_token_uri host is refused (42501)")


def test_the_uri_form_works_with_the_env_token(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["uri form"], MDLOC, "the catalog-URI form still works with the env token")


def test_a_non_superuser_does_not_borrow_the_env_token(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["ice_r refused"], "28000|0",
                "a non-superuser with no token mapping is refused (28000) despite the environment token, before any request")


def test_a_non_superuser_cannot_waive_its_own_credentials(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["ice_r own flag"], "42501",
                "and it cannot mark its own mapping credentials_required false (42501)")


def test_a_superuser_waiver_lets_the_role_read_by_the_env_token(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["ice_r control"], MDLOC,
                "control: with credentials_required false set by a superuser, the same role reads by the environment token")


def test_the_backend_survives_the_rest_server_arms(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["survived"], "1", "backend still up")
