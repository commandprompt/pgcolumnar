"""pgColumnar pytest harness: fixtures.

The vacuity layer in pgc_vacuity.py is loaded for every run through the `-p`
argument in pytest.ini, not imported here, so that a test file cannot opt out of it.

`pytester` is enabled because the layer's own tests run pytest inside pytest: a
guard is proven to REFUSE rather than assumed to.

PSYCOPG IS IMPORTED INSIDE THE FIXTURES THAT USE IT, NOT HERE, AND THAT IS
LOAD-BEARING RATHER THAN TIDINESS. conftest is imported before every run, so a
module-scope `import psycopg` made a DATABASE DRIVER a hard requirement of the
whole corpus -- including every test that never opens a connection. With the
import at module scope NO test runs without the driver, and the failure is a
conftest ImportError before collection rather than a failed test.

No count is written here. Which files need no database is decided in
test_harness_deps.py, from the corpus, and the gate's job prints what it ran:
a number in this docstring would be a hand-maintained derived value (#908).

That is the difference between "this harness needs Postgres" and "the tests that
talk to Postgres need Postgres", and it is what lets the guard-testing half of
this corpus run somewhere that has no database at all -- which is where the gate
is (README.md, "This is not in the gate yet").
"""

import os
import re
import pathlib
import shutil
import signal
import subprocess

import pytest

import pgc_vacuity

from pgc_cluster import _pg_config, build_once, make_cluster, require_library_unchanged

pytest_plugins = ["pytester"]

# Where `--pg-config` looks when neither the option nor PGC_PG_CONFIG names one (#1933).
# PG18 first, because the corpus was written against it: pg18a is the audit box's
# assert-enabled build and pgsql is PG18 in the dev container. Then the other majors,
# newest first, each as the audit box names it (bench/provision.sh WANT_PG_MATRIX) and
# as the dev container names it. Then the pg_config on PATH. A fixed path here was
# right on one box only: the dev container has no pg18a, so every cluster fixture
# failed at setup with PGC_PG_CONFIG unset.
PG_CONFIG_CANDIDATES = (
    "/usr/local/pg18a/bin/pg_config",
    "/usr/local/pgsql/bin/pg_config",
    "/usr/local/pg19a/bin/pg_config",
    "/usr/local/pg19/bin/pg_config",
    "/usr/local/pg17a/bin/pg_config",
    "/usr/local/pg17/bin/pg_config",
    "/usr/local/pg16a/bin/pg_config",
    "/usr/local/pg16/bin/pg_config",
    "/usr/local/pg15a/bin/pg_config",
    "/usr/local/pg15/bin/pg_config",
)


def resolve_pg_config(candidates=PG_CONFIG_CANDIDATES, which=shutil.which):
    """-> (path, source). The first executable candidate, else the pg_config on
    PATH, else the first candidate. The last case keeps a named path in the
    option, so the cluster fixture fails on that path rather than on an empty
    string. The source is what the run header prints (#1933)."""
    for path in candidates:
        if os.access(path, os.X_OK):
            return path, "candidate"
    on_path = which("pg_config")
    if on_path:
        return on_path, "PATH"
    return candidates[0], "first candidate, none found"


def default_pg_config(candidates=PG_CONFIG_CANDIDATES, which=shutil.which):
    """The path half of `resolve_pg_config`."""
    return resolve_pg_config(candidates, which)[0]


DEFAULT_PG_CONFIG, DEFAULT_PG_CONFIG_SOURCE = resolve_pg_config()


def pytest_addoption(parser):
    # No default here: pytest_configure fills it in, so that it knows whether
    # the value came from the option, from PGC_PG_CONFIG or from the resolver.
    parser.addoption(
        "--pg-config",
        action="store",
        default=None,
        help="pg_config of the server build under test (assert-enabled, per lib.sh); "
             "default PGC_PG_CONFIG, else the first of PG_CONFIG_CANDIDATES, else PATH",
    )


_PG_CONFIG_SOURCE = pytest.StashKey[str]()


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    """Fill in `--pg-config` and record where the value came from."""
    if config.option.pg_config:
        source = "--pg-config"
    elif os.environ.get("PGC_PG_CONFIG"):
        config.option.pg_config = os.environ["PGC_PG_CONFIG"]
        source = "PGC_PG_CONFIG"
    else:
        config.option.pg_config = DEFAULT_PG_CONFIG
        source = DEFAULT_PG_CONFIG_SOURCE
    config.stash[_PG_CONFIG_SOURCE] = source


def pg_config_line(config):
    """The line that says which build this run tested, how the pg_config was
    chosen, and what it reports. A pg_config that does not run is named with
    the error, not left out: that is the run the line matters most for."""
    path = config.getoption("--pg-config")
    try:
        version = subprocess.run([path, "--version"], capture_output=True,
                                 text=True, timeout=30, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        version = f"pg_config --version failed: {e}"
    return f"pg_config: {path} (from {config.stash[_PG_CONFIG_SOURCE]}) -- {version}"


def pytest_report_header(config):
    """Name the build under test at the top of the run (#1933, review of PR
    #1938). Before this, the only print was the cluster fixture's, which pytest
    captures, so a passing run named no pg_config unless run with -s.

    NO xdist WORKER CHECK, BECAUSE NONE IS NEEDED. A worker has no terminal
    reporter, so pytest never calls this hook or the summary below there. A
    `workerinput` guard was tried and removing it changed nothing: the -n 2 case
    of test_the_run_reports_which_pg_config_it_used_and_why still saw one line."""
    return pg_config_line(config)


def pytest_terminal_summary(terminalreporter, config):
    """The same line at the end, but only when -q or --no-header hid the
    header. CI runs with -q, and the line must reach its log too."""
    if terminalreporter.verbosity >= 0 and not config.option.no_header:
        return
    terminalreporter.write_line(pg_config_line(config))


SRCDIR = pathlib.Path(__file__).resolve().parents[2]


def major_of(version_text):
    """"PostgreSQL 18.4" -> "18". The build stamp is per major, because objects
    from another major link and then fail to load (#536)."""
    m = re.search(r"(\d+)", version_text or "")
    return m.group(1) if m else ""


def _raise_system_exit(signum, *_frame):
    """Turn SIGTERM into SystemExit so `finally` blocks run (#1170).

    128 + signum is the exit status a shell reports for a signalled process, so a
    caller reading `$?` sees 143 either way and nothing downstream has to learn a
    new number.

    THE FRAME IS TAKEN AS A VARARG BECAUSE IT IS GENUINELY UNREAD. Python calls a
    signal handler with two positional arguments, and naming the second one would
    be a parameter this function ignores -- which
    `test_no_helper_in_this_corpus_takes_a_parameter_it_never_reads` refuses,
    corpus-wide and correctly. It caught this on the first run.
    """
    raise SystemExit(128 + signum)


@pytest.fixture(scope="session")
def pgc_cluster(request, worker_id):
    """One cluster per xdist worker, built once and torn down at session end."""
    pg_config = request.config.getoption("--pg-config")

    # THE HANDLER GOES FIRST, BEFORE ANYTHING IS BUILT OR STARTED (#1170).
    # An earlier draft installed it after `make_cluster` returned, and
    # @OffgridwithJD's probe killed the run at 0s after the postmaster first
    # appeared and leaked it anyway: between the postmaster accepting
    # connections and the handler existing there is a window where the default
    # disposition still applies. That window contains the initdb and the start,
    # which is the slowest part of the fixture and exactly when somebody
    # realises they ran the wrong command.
    #
    # `make_cluster` already unwinds on BaseException -- SystemExit included --
    # so installing it here makes a cleanup that already existed reachable
    # rather than adding a second one. Measured, killing at a delay counted from
    # the moment the postmaster first appears, and reading the identity of THAT
    # postmaster rather than a count:
    #
    #     kill delay   main                        this change
    #     0s           survived, datadir present   gone, datadir removed
    #     0.5s         survived, datadir present   gone, datadir removed
    #     2s           survived, datadir present   gone, datadir removed
    #
    # THE 0s CASE NEEDED MORE THAN THE HANDLER. `Cluster.stop()` returns at once
    # when `self._started` is False, which it is when the interruption arrives
    # DURING `start()`, so an intermediate version removed the tree under a live
    # server -- a leak with no name tag anywhere on the filesystem, which is
    # worse than the one it replaced. @OffgridwithJD measured that and it is why
    # `make_cluster` now refuses to remove a directory a postmaster still holds;
    # see `_stop_abandoned` there.
    previous = None
    try:
        previous = signal.signal(signal.SIGTERM, _raise_system_exit)
    except ValueError:
        # Not the main thread of the main interpreter. Nothing to install, and
        # the run is no worse off than it was.
        previous = None

    # BUILD BEFORE THE SERVER STARTS. This ran the other way round first, and the
    # ordering was not a detail: shared_preload_libraries maps the library at
    # postmaster start, so a cluster started before the install keeps the OLD
    # .so mapped for its whole life. The build would report success and every
    # test would still measure the previous branch's code -- the same defect the
    # build guard exists to close, reintroduced by the order of two lines.
    #
    # It showed up as a flake: the first run after a source change failed to
    # start a cluster, and the next run passed because the install had already
    # landed.
    major = major_of(_pg_config(pg_config, "--version"))
    verdict = build_once(str(SRCDIR), pg_config, major)

    cluster, root = make_cluster(pg_config, worker_id)
    try:
        yield from _serve_cluster(cluster, root, verdict, worker_id)
    finally:
        if previous is not None:
            try:
                signal.signal(signal.SIGTERM, previous)
            except ValueError:
                pass


def _serve_cluster(cluster, root, verdict, worker_id):
    """Print the provenance, install the extension, serve the cluster, tear it
    down. Split out so the teardown covers `require_server_loaded_this_binary`
    too: a refusal there used to leave the cluster running, because the old
    `finally` began after it."""
    print(f"\n-- build: {verdict} from {SRCDIR}")
    # Printed for the same reason lib.sh prints it: so a reader can tell which
    # binary produced the results below.
    # The digest the session runs against. The teardown fails the session if it
    # changed before the end: another session installed into this prefix (#1915).
    cluster.so_at_start = cluster.so_md5()
    print(f"-- cluster: worker={worker_id} port={cluster.port} "
          f"{cluster.version} .so={cluster.so_at_start}")
    # And the running server is the one that loaded THAT library. @jdatcmd noted
    # a start-time check would be near-vacuous because the cluster is initdb'd
    # fresh each session, so the postmaster always starts after the .so. That is
    # true once the order above is right, and it is exactly what pins the order:
    # with the build after the start, the .so is NEWER than the postmaster and
    # this refuses.
    cluster.require_server_loaded_this_binary()
    import psycopg          # deferred: see the module docstring

    # A KILLED RUN MUST NOT LEAK ITS CLUSTER (#1170). Python does not run
    # `finally` blocks when the DEFAULT SIGTERM disposition terminates the
    # process, so the teardown below was unreachable for every interrupted run --
    # and interrupting a run is normal and correct, not an error. The postmaster
    # then survives holding its port, its shared memory and its datadir.
    #
    # Measured before this handler existed: 21 orphaned postmasters and ~2.5 GB
    # of datadirs across the two development containers, the oldest 33 hours, and
    # one container had background tasks killed for low memory as a result. A
    # leaked cluster holding a port is also the documented false-red source: a
    # later run fails for a reason that has nothing to do with the code.
    #
    #     plain try/finally, kill -TERM                 0 finally blocks ran
    #     SIGTERM handler raising SystemExit, same kill 1 finally block ran
    #
    # SIGKILL IS UNCATCHABLE and no handler will ever cover it. The stale
    # clusters that predate this were `pkill -9` and the OOM killer.
    #
    # THE SHELL HALF ALREADY HANDLES THIS AND MUST NOT BE "FIXED": lib.sh's
    # `trap pgc_teardown EXIT` already runs on TERM, INT and HUP, and naming the
    # signals explicitly makes the handler fire twice.
    try:
        with psycopg.connect(cluster.dsn(), autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        yield cluster
    finally:
        cluster.stop()
        shutil.rmtree(root, ignore_errors=True)
    # After the finally, so it runs only on a teardown with nothing in flight.
    require_library_unchanged(cluster.so_path, cluster.so_at_start)


@pytest.fixture(scope="module")
def server_dir():
    """A directory the SERVER can write, which `tmp_path` is not.

    `COPY ... TO` and `parallel_copy` are executed by the backend, and the backend runs
    as a different user: pytest's `tmp_path` lives under `/tmp/pytest-of-root/` at mode
    700, so the server cannot reach it and the error names a permission rather than the
    real cause. Opening the parents matters as much as the leaf -- one closed directory
    above makes the subtree unreachable however open the leaf is -- so this makes its
    own directory instead of trying to prise `tmp_path` open.
    """
    import tempfile
    d = pathlib.Path(tempfile.mkdtemp(prefix="pgc_server_dir_"))
    os.chmod(d, 0o777)
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def pgc_own_db(pgc_cluster, request):
    """A private DATABASE, where `pgc_conn` gives a private schema.

    `pgc_conn` is the right trade almost everywhere: isolation without paying an
    initdb per test. IT IS THE WRONG ONE FOR ANY TEST WHOSE CLAIMS ARE ABOUT THE
    SIZE OR EMPTINESS OF THE pgcolumnar CATALOGS, because those live in the
    database and are shared by every test in the session.

    Measured, and it is why this exists (#1213): a file asserting a claim about
    catalog size saw six catalog pages run alone and thirty-nine run after the
    other fifty-one cluster files, and the claim fell from 223 parts per
    thousand to 65 -- under its floor. The arm was not wrong and the code was not
    wrong; the fixture's assumption was, and it held only in the arrangement
    that had been run. CI found it and a single-file run could not.

    The extension is created on the raw connection before the wrapper goes on,
    the way pgc_conn creates its schema: that is this fixture's own DDL, not the
    test's writes, and DDL carries no row count anyway.

    A test using this must still not run concurrently with another that reads
    database-wide statistics, because pg_stat_reset() is per database and this
    one is private only to the test, not to the cluster.
    """
    import psycopg          # deferred: see the module docstring

    name = "pgc_own_" + "".join(
        ch if ch.isalnum() else "_" for ch in request.node.name
    )[:40]

    def admin(sql):
        c = psycopg.connect(pgc_cluster.dsn(), autocommit=True)
        try:
            c.execute(sql)
        finally:
            c.close()

    admin(f'DROP DATABASE IF EXISTS "{name}"')
    admin(f'CREATE DATABASE "{name}"')
    conn = psycopg.connect(pgc_cluster.dsn(dbname=name), autocommit=True)
    try:
        conn.execute("CREATE EXTENSION pgcolumnar")
        yield pgc_vacuity.watch_writes(conn, request.node.nodeid)
    finally:
        conn.close()
        admin(f'DROP DATABASE IF EXISTS "{name}"')


@pytest.fixture
def pgc_conn(pgc_cluster, request):
    """A direct connection, in a schema private to this one test.

    A private schema rather than a private database: isolation without paying an
    initdb per test. It is first on the search_path, so an unqualified CREATE TABLE
    lands in it and cannot collide with another test's fixture.

    autocommit is ON deliberately. With it off, a fixture that forgets to commit
    leaves the next connection looking at an absent table, which is measured in the
    design document as a way to make a test assert nothing.
    """
    import psycopg          # deferred: see the module docstring

    schema = "pgc_test_" + "".join(
        ch if ch.isalnum() else "_" for ch in request.node.name
    )[:48]
    conn = psycopg.connect(pgc_cluster.dsn(), autocommit=True)
    try:
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        conn.execute(f'CREATE SCHEMA "{schema}"')
        conn.execute(f'SET search_path TO "{schema}", public')
        # WATCHED, so a write that wrote nothing cannot pass unnoticed. The three
        # statements above run on the raw connection deliberately: they are this
        # fixture's own DDL, not the test's writes, and DDL carries no row count
        # anyway. See pgc_vacuity.watch_writes and test_writes_wrote_rows.py.
        yield pgc_vacuity.watch_writes(conn, request.node.nodeid)
    finally:
        try:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            conn.close()
