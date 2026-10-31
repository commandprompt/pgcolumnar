"""Iceberg REST catalog read and listing, 7b (#388 phase 7): the pytest twin of
`test/iceberg_rest_scan.sh`, every check by the shell's name (#1341).

iceberg_rest_scan(catalog, ns, table) must return the very rows iceberg_scan returns
for the metadata location the catalog reports: the catalog changes how a table is
NAMED, never its rows. The shell's own REST fixture (test/iceberg_rest_server.py),
which verifies the Authorization: Bearer header, points loadTable at the committed
warehouse, relocated into a work directory. Projection by field id flows through the
REST path, namespaces and tables list, and an unknown table is the catalog's own 404
(42P01, read by its message).

A BACKEND THAT DIED DOES NOT PASS AS ONE THAT SURVIVED: survival also reads the
private cluster's server log for killed backends, and an unreadable log fails it.

A PRIVATE CLUSTER, because the postmaster is restarted with the bearer token in its
environment (PGCOLUMNAR_ICEBERG_REST_TOKEN), which the shared cluster cannot do.
Without the object-store module or the committed warehouse, the first test declines
under the shell's pgc_skip message and the rest decline.
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
WH = TEST / "fixtures" / "iceberg" / "warehouse"
TOKEN = "rest-scan-token-shh"
TOKEN_ENV = "PGCOLUMNAR_ICEBERG_REST_TOKEN"


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


def _rows(dsn, sql):
    """-> every row's first column joined by newlines, as the shell's q prints them, or
    the failure as a query_error()."""
    _wait_up(dsn)
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            rows = conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return "\n".join("" if r[0] is None else str(r[0]) for r in rows)


def _refusal(dsn, sql, phrase, label):
    """-> "<SQLSTATE><label>" when `sql` raised with `phrase` in its message, the bare
    SQLSTATE when the message lacks it, or what happened instead."""
    _wait_up(dsn)
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
    up, now = _rows(dsn, "SELECT 1"), _crashes(log)
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


def _objstore_installed(pg_config):
    try:
        lib = subprocess.run([pg_config, "--pkglibdir"], capture_output=True, text=True).stdout.strip()
    except OSError:
        return False
    return bool(lib) and (pathlib.Path(lib) / "pgcolumnar_objstore.so").is_file()


def _warehouse_present():
    return any((WH / "db" / "events" / "data").glob("*/*.parquet"))


@pytest.fixture(scope="module")
def irs(pgc_cluster):
    if not _objstore_installed(pgc_cluster.pg_config):
        yield "objstore"
        return
    if not _warehouse_present():
        yield "fixture"
        return
    from pgc_cluster import Cluster, pick_port

    work = pathlib.Path(tempfile.mkdtemp(prefix="pgc-icerestscan-"))
    os.chmod(work, 0o777)
    dest = work / "wh"
    shutil.copytree(WH / "db", dest / "db")
    for p in [dest, *dest.rglob("*")]:
        p.chmod(0o777 if p.is_dir() else 0o666)
    md = sorted((dest / "db" / "events" / "metadata").glob("*.metadata.json"))[-1]
    mdloc = f"file://{md}"

    rest_port = pick_port(34)
    out = work / "rest_server.out"
    srv = subprocess.Popen([sys.executable, str(TEST / "iceberg_rest_server.py"), "--port", str(rest_port),
                            "--log", str(work / "rest.log"), "--token", TOKEN, "--namespace", "db",
                            "--table", "events", "--metadata-location", mdloc],
                           stdout=open(out, "w"), stderr=subprocess.STDOUT)
    for _ in range(50):
        if out.is_file() and "READY" in out.read_text(errors="replace"):
            break
        time.sleep(0.1)
    cat = f"http://127.0.0.1:{rest_port}"

    datadir = work / "data"
    datadir.mkdir()
    log = str(work / "server.log")
    cluster = Cluster(pgc_cluster.pg_config, "icerestscan", datadir, pick_port(35))
    fx_srvs = []
    r = {"server up": out.read_text(errors="replace").count("READY") if out.is_file() else 0, "mdloc": mdloc}
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

        cols = "AS t(id bigint, region text, amount int)"
        r["location"] = _rows(dsn, f"SELECT pgcolumnar.iceberg_rest_table_location('{cat}','db','events')")
        r["direct"] = _rows(dsn, f"SELECT id || '|' || region || '|' || amount FROM pgcolumnar.iceberg_scan('{md}') "
                                 f"{cols} ORDER BY id")
        r["via rest"] = _rows(dsn, f"SELECT id || '|' || region || '|' || amount FROM "
                                   f"pgcolumnar.iceberg_rest_scan('{cat}','db','events') {cols} ORDER BY id")
        r["projected"] = _rows(dsn, f"SELECT amount || '|' || id FROM pgcolumnar.iceberg_rest_scan('{cat}','db','events') "
                                    f"AS t(amount int, id bigint) ORDER BY id")
        r["namespaces"] = _rows(dsn, f"SELECT string_agg(ns, ',' ORDER BY ns) "
                                     f"FROM pgcolumnar.iceberg_rest_namespaces('{cat}') AS ns")
        r["tables"] = _rows(dsn, f"SELECT string_agg(t, ',' ORDER BY t) "
                                 f"FROM pgcolumnar.iceberg_rest_tables('{cat}','db') AS t")
        r["unknown"] = _refusal(dsn, f"SELECT * FROM pgcolumnar.iceberg_rest_scan('{cat}','db','nosuch') AS t(id bigint)",
                                "the REST catalog has no such table", " by the catalog's 404")
        # The catalog serves only "db": a namespace it does not have must reach it as
        # that namespace and come back as its 404, so a client that ignored its
        # namespace argument can no longer pass.
        r["list other ns"] = _refusal(dsn, f"SELECT * FROM pgcolumnar.iceberg_rest_tables('{cat}','nosuchns')",
                                      "the REST catalog has no such table list", " by the catalog's 404")
        r["scan other ns"] = _refusal(dsn, f"SELECT * FROM pgcolumnar.iceberg_rest_scan('{cat}','nosuchns','events') "
                                           f"AS t(id bigint)",
                                      "the REST catalog has no such table", " by the catalog's 404")
        # #1793: one fixture per catalog behaviour, as the shell starts them.
        def start_fx(name, *args):
            port = pick_port(36 + len(fx_srvs))
            fout = work / f"{name}.out"
            fx_srvs.append(subprocess.Popen(
                [sys.executable, str(TEST / "iceberg_rest_server.py"), "--port", str(port),
                 "--log", str(work / f"{name}.log"), "--token", TOKEN,
                 "--metadata-location", mdloc, *args],
                stdout=open(fout, "w"), stderr=subprocess.STDOUT))
            for _ in range(50):
                if fout.is_file() and "READY" in fout.read_text(errors="replace"):
                    break
                time.sleep(0.1)
            return f"http://127.0.0.1:{port}"

        def nslist(c):
            return _rows(dsn, f"SELECT string_agg(ns, ',' ORDER BY ns) FROM pgcolumnar.iceberg_rest_namespaces('{c}') AS ns")

        def tlist(c, ns):
            return _rows(dsn, f"SELECT string_agg(t, ',' ORDER BY t) FROM pgcolumnar.iceberg_rest_tables('{c}','{ns}') AS t")

        def scan(c, ns):
            return _rows(dsn, f"SELECT id || '|' || region || '|' || amount FROM "
                              f"pgcolumnar.iceberg_rest_scan('{c}','{ns}','events') {cols} ORDER BY id")

        def location(c):
            return _rows(dsn, f"SELECT pgcolumnar.iceberg_rest_table_location('{c}','db','events')")

        ncat = start_fx("nested", "--namespace", "db.x", "--table", "events", "--extra-table", "more",
                        "--extra-namespace", "db.x.y", "--extra-namespace", "aa",
                        "--namespace-separator", "%2E", "--page-size", "1")
        r["nested namespaces"] = nslist(ncat)
        r["nested tables"] = tlist(ncat, "db.x")
        r["nested scan"] = scan(ncat, "db.x")
        scat = start_fx("slashpfx", "--namespace", "db", "--table", "events", "--prefix", "catalogs/main")
        r["slash location"] = location(scat)
        r["slash scan"] = scan(scat, "db")
        r["slash namespaces"] = nslist(scat)
        r["slash tables"] = tlist(scat, "db")
        dcat = start_fx("defpfx", "--namespace", "db", "--table", "events", "--prefix", "wh1",
                        "--prefix-in-defaults")
        r["defaults location"] = location(dcat)
        r["defaults tables"] = tlist(dcat, "db")
        wcat = start_fx("whcat", "--namespace", "db", "--table", "events", "--prefix", "whp",
                        "--warehouse", "wh1")
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(f"CREATE SERVER irs_wh FOREIGN DATA WRAPPER pgcolumnar_iceberg_catalog "
                      f"OPTIONS (catalog_uri '{wcat}', warehouse 'wh1')")
            c.execute(f"CREATE USER MAPPING FOR CURRENT_USER SERVER irs_wh OPTIONS (token '{TOKEN}')")
        r["warehouse namespaces"] = nslist("irs_wh")
        r["warehouse tables"] = tlist("irs_wh", "db")

        r["survived"] = _survived(dsn, log, before)
        yield r
    finally:
        _pg_ctl(cluster, ["-D", str(datadir), "-m", "immediate", "-w", "stop"], {})
        srv.kill()
        srv.wait()
        for fx in fx_srvs:
            fx.kill()
            fx.wait()
        shutil.rmtree(work, ignore_errors=True)


def _declined(irs, expect):
    if isinstance(irs, dict):
        return False
    expect.cannot_run("MISSING_DEPENDENCY" if irs == "objstore" else "ABSENT_FIXTURE",
                      "the object-store module is not installed" if irs == "objstore"
                      else "iceberg warehouse data files are missing")
    return True


def test_the_rest_scan_fixture_server_is_up(irs, expect):
    """The shell skips the whole suite without python3, the object-store module or the
    committed warehouse, under these names."""
    expect.require_module("json", name="python3 is needed")
    if irs == "objstore":
        expect.cannot_run("MISSING_DEPENDENCY", "pgcolumnar_objstore.so is not in the pkglibdir",
                          name="the object-store module is not installed")
        return
    if irs == "fixture":
        expect.cannot_run("ABSENT_FIXTURE", "the committed iceberg warehouse is not in this tree",
                          name="iceberg warehouse data files are missing")
        return
    expect.text(str(irs["server up"]), "1", "premise: rest fixture server is up")


def test_the_catalog_reports_the_relocated_metadata_location(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["location"], irs["mdloc"], "the catalog reports the relocated metadata location")


def test_iceberg_rest_scan_returns_the_five_current_snapshot_rows(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["via rest"], "1|eu|10\n2|eu|20\n3|us|30\n4|us|40\n5|us|50",
                "iceberg_rest_scan returns the five current-snapshot rows")


def test_iceberg_rest_scan_matches_iceberg_scan_of_the_same_table(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["via rest"], irs["direct"],
                "iceberg_rest_scan matches iceberg_scan of the same table (same oracle)")


def test_iceberg_rest_scan_projects_and_reorders_by_field_id(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["projected"], "10|1\n20|2\n30|3\n40|4\n50|5",
                "iceberg_rest_scan projects and reorders by field id (amount, id)")


def test_iceberg_rest_namespaces_lists_the_catalogs_namespaces(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["namespaces"], "db", "iceberg_rest_namespaces lists the catalog's namespaces")


def test_iceberg_rest_tables_lists_a_namespaces_tables(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["tables"], "events", "iceberg_rest_tables lists a namespace's tables")


def test_iceberg_rest_scan_of_an_unknown_table_is_a_clean_not_found(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["unknown"], "42P01 by the catalog's 404",
                "iceberg_rest_scan of an unknown table is a clean not-found (42P01)")



def test_iceberg_rest_tables_of_an_absent_namespace_is_the_catalogs_404(irs, expect):
    expect.text(irs["list other ns"], "42P01 by the catalog's 404",
                "iceberg_rest_tables of a namespace the catalog does not have is the catalog's 404")


def test_iceberg_rest_scan_in_an_absent_namespace_is_the_catalogs_404(irs, expect):
    expect.text(irs["scan other ns"], "42P01 by the catalog's 404",
                "iceberg_rest_scan of a table in a namespace the catalog does not have is the catalog's 404")

def test_the_backend_is_still_up_after_the_rest_read(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["survived"], "1", "backend still up after the REST read")


ROWS5 = "1|eu|10\n2|eu|20\n3|us|30\n4|us|40\n5|us|50"


def test_nested_namespaces_iceberg_rest_namespaces_lists_every_level(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["nested namespaces"], "aa,db,db.x,db.x.y",
                "nested namespaces: iceberg_rest_namespaces lists every level, page by page")


def test_nested_namespaces_iceberg_rest_tables_lists_both_pages(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["nested tables"], "events,more",
                "nested namespaces: iceberg_rest_tables of db.x lists both pages")


def test_nested_namespaces_iceberg_rest_scan_returns_the_five_rows(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["nested scan"], ROWS5,
                "nested namespaces: iceberg_rest_scan of db.x.events returns the five rows")


def test_multi_segment_prefix_iceberg_rest_table_location_resolves(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["slash location"], irs["mdloc"],
                "multi-segment prefix: iceberg_rest_table_location resolves the table")


def test_multi_segment_prefix_iceberg_rest_scan_returns_the_five_rows(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["slash scan"], ROWS5, "multi-segment prefix: iceberg_rest_scan returns the five rows")


def test_multi_segment_prefix_iceberg_rest_namespaces_lists(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["slash namespaces"], "db",
                "multi-segment prefix: iceberg_rest_namespaces lists the namespaces")


def test_multi_segment_prefix_iceberg_rest_tables_lists(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["slash tables"], "events", "multi-segment prefix: iceberg_rest_tables lists the tables")


def test_prefix_in_config_defaults_iceberg_rest_table_location_resolves(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["defaults location"], irs["mdloc"],
                "prefix in config defaults: iceberg_rest_table_location resolves the table")


def test_prefix_in_config_defaults_iceberg_rest_tables_lists(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["defaults tables"], "events",
                "prefix in config defaults: iceberg_rest_tables lists the tables")


def test_warehouse_server_iceberg_rest_namespaces_sends_the_warehouse(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["warehouse namespaces"], "db",
                "warehouse server: iceberg_rest_namespaces sends the warehouse and lists")


def test_warehouse_server_iceberg_rest_tables_sends_the_warehouse(irs, expect):
    if _declined(irs, expect):
        return
    expect.text(irs["warehouse tables"], "events",
                "warehouse server: iceberg_rest_tables sends the warehouse and lists")
