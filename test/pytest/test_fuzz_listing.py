"""The ListObjectsV2 listing parser survives malformed input (#619). The pytest twin
of `test/fuzz_listing.sh`, every check by the shell's name.

The SigV4 fixture server (`test/objstore_http_server.py`, the shell's own) serves its
`__listing_override__` file verbatim as the listing, so each mutant from
`test/listing_corpus.py` -- a pure function of one integer seed -- reaches the C
parser exactly as a hostile endpoint's response would, through
`read_parquet('s3://pgc-bucket/lst/')`. The property is the shell's: a malformed listing
makes the backend raise an ERROR, never die, hang or trip a sanitizer, classified from
the private cluster's server log first and the client's error second.

The override is removed before each mutant is generated (#1341): with no override the
fixture serves the real listing, so a generator that writes nothing read as a clean
accept every time.

A PRIVATE CLUSTER, because the postmaster runs with AWS_* in its environment. PGC_SEED
and PGC_ITERS are the shell's knobs, with its defaults.
"""
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import shutil
import time

import psycopg
import pytest

from pgc_vacuity import query_error

TEST = pathlib.Path(__file__).resolve().parents[1]
CORPUS = TEST / "listing_corpus.py"
SEED = int(os.environ.get("PGC_SEED", "20260814"))
ITERS = int(os.environ.get("PGC_ITERS", "200"))
AKID, SECRET, REGION, BUCKET = "PGCTESTKEYID", "pgctest-secret", "pgc-test-1", "pgc-bucket"
READ = f"SELECT count(*) FROM pgcolumnar.read_parquet('s3://{BUCKET}/lst/') AS t(id int8)"

SANITIZER = re.compile(r"AddressSanitizer|runtime error:|UndefinedBehaviorSanitizer|LeakSanitizer")
CRASHED = re.compile(r"was terminated by signal|server process .* exited with|crashed")
LOST = re.compile(r"server closed the connection|connection to server was lost")


def _message(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _value(dsn, sql):
    """-> the first column of the first row as text, or the failure as a query_error()."""
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            row = conn.execute(sql).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return query_error(f"no value from {sql}") if row is None or row[0] is None else str(row[0])


class _Log:
    """The server log read from a byte offset, so an outcome is attributed to the
    statement that produced it and not to every statement after it."""

    def __init__(self, path):
        self.path, self.pos = path, 0
        self.since()

    def since(self):
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.pos)
                new = fh.read()
        except OSError:
            return ""
        self.pos += len(new)
        return new.decode(errors="replace")


def _cluster_back(dsn):
    for _ in range(60):
        if _value(dsn, "SELECT 1") == "1":
            return True
        time.sleep(0.5)
    return False


def _outcome(dsn, log, sql):
    """-> crash | san | hang | error | clean, or gone when the cluster did not return,
    with the client's message. The shell's order: sanitizer and crash from the log,
    then a timeout, then a lost connection, then an ERROR."""
    log.since()
    failure = None
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            conn.execute("SET statement_timeout = '20s'")
            conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        failure = exc
    # Classified outside the handler: the failure is kept as the exception, and its text
    # is read only to classify it, never returned as a reading.
    message, kind = "", "clean"
    if isinstance(failure, psycopg.errors.QueryCanceled):
        kind, message = "hang", _message(failure)
    elif isinstance(failure, psycopg.OperationalError):
        message = str(failure)
        kind = "lost" if LOST.search(message) else "unreachable"
    elif failure is not None:
        kind, message = "error", _message(failure)
    if kind == "unreachable" and not _cluster_back(dsn):
        return "gone", message
    new = log.since()
    if SANITIZER.search(new):
        _cluster_back(dsn)
        return "san", message
    if CRASHED.search(new):
        _cluster_back(dsn)
        return "crash", message
    if kind == "hang":
        return "hang", message
    if kind == "lost":
        _cluster_back(dsn)
        return "crash", message
    if kind in ("error", "unreachable"):
        return "error", message
    return "clean", message


def _pg_ctl(cluster, args, extra_env):
    """pg_ctl with every AWS_* of this process removed, then `extra_env` added; dropped
    to postgres when running as root, as the harness does."""
    unset = [a for k in os.environ if k.startswith("AWS_") for a in ("-u", k)]
    cmd = ["env"] + unset + [f"{k}={v}" for k, v in extra_env.items()] + \
          [os.path.join(cluster.bindir, "pg_ctl")] + args
    if os.geteuid() == 0:
        cmd = ["runuser", "-u", "postgres", "--"] + cmd
    return subprocess.run(cmd, capture_output=True, text=True)


def _build(pgc_cluster, pa, pq, held):
    """Stand up the listing fixture and run the fuzz loop, with the pyarrow modules the
    first test's require_module returned: a fixture cannot decline, so it never imports
    pyarrow itself (main's capability guard, #1655 review). `held` keeps what teardown
    must stop."""
    from pgc_cluster import Cluster, pick_port

    work = pathlib.Path(tempfile.mkdtemp(prefix="pgc-fzlst-"))
    held["work"] = work
    os.chmod(work, 0o777)
    (work / BUCKET / "lst").mkdir(parents=True)
    pq.write_table(pa.table({"id": pa.array([7, 8, 9], pa.int64())}), work / BUCKET / "lst" / "real.parquet")
    for p in [work / BUCKET, *(work / BUCKET).rglob("*")]:
        p.chmod(0o777 if p.is_dir() else 0o666)
    override = work / "__listing_override__"

    s3_port = pick_port(32)
    out = work / "s3_server.out"
    srv = subprocess.Popen([sys.executable, str(TEST / "objstore_http_server.py"), "--dir", str(work),
                            "--port", str(s3_port), "--log", str(work / "s3.log"),
                            "--sigv4-key", AKID, "--sigv4-secret", SECRET, "--sigv4-region", REGION],
                           stdout=open(out, "w"), stderr=subprocess.STDOUT)
    held["srv"] = srv
    for _ in range(50):
        if out.is_file() and "READY" in out.read_text(errors="replace"):
            break
        time.sleep(0.1)

    datadir = work / "data"
    datadir.mkdir()
    logpath = work / "server.log"
    cluster = Cluster(pgc_cluster.pg_config, "fzlst", datadir, pick_port(31))
    held["cluster"], held["datadir"] = cluster, datadir
    r = {"fixture up": out.read_text(errors="replace").count("READY") if out.is_file() else 0}
    cluster.initdb()
    with open(datadir / "postgresql.conf", "a") as fh:
        fh.write("\npgcolumnar.objstore_allowed_endpoints='127.0.0.1'\n")
    env = {"AWS_ENDPOINT_URL": f"http://127.0.0.1:{s3_port}", "AWS_ACCESS_KEY_ID": AKID,
           "AWS_SECRET_ACCESS_KEY": SECRET, "AWS_REGION": REGION}
    started = _pg_ctl(cluster, ["-D", str(datadir), "-l", str(logpath), "-w", "start"], env)
    if started.returncode != 0:
        raise RuntimeError(f"the private cluster did not start: {started.stderr.strip()}")
    dsn = cluster.dsn()
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")

    def generate(arg):
        override.unlink(missing_ok=True)
        subprocess.run([sys.executable, str(CORPUS), str(arg), str(override)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return override.exists()

    generate("valid")
    r["control"] = _value(dsn, READ)

    log = _Log(logpath)
    n = dict(crash=0, hang=0, san=0, error=0, clean=0, fresh=0)
    print(f"-- fuzzing {ITERS} listing mutants (seed base {SEED}); reproduce with PGC_SEED={SEED}")
    for i in range(ITERS):
        s = SEED + i
        if generate(s):
            n["fresh"] += 1
        kind, message = _outcome(dsn, log, READ)
        if kind == "gone":
            print(f"FATAL: cluster gone at seed={s}")
            break
        if kind in ("crash", "hang", "san"):
            print(f"  FINDING [{kind}] seed={s}: {message}")
        n[kind] += 1
    override.unlink(missing_ok=True)
    print(f"-- done: {ITERS} mutants, {n['error']} rejected with ERROR, {n['clean']} accepted clean")
    r.update(n)
    return r


@pytest.fixture(scope="module")
def fl(pgc_cluster):
    """-> run(pa, pq): builds the fixture and runs the fuzz loop once, on the first call."""
    held = {}

    def run(pa, pq):
        if "r" not in held:
            held["r"] = _build(pgc_cluster, pa, pq, held)
        return held["r"]

    try:
        yield run
    finally:
        if "cluster" in held:
            _pg_ctl(held["cluster"], ["-D", str(held["datadir"]), "-m", "immediate", "-w", "stop"], {})
        if "srv" in held:
            held["srv"].terminate()
            held["srv"].wait()
        if "work" in held:
            shutil.rmtree(held["work"], ignore_errors=True)


def _modules(expect):
    """-> (pyarrow, pyarrow.parquet) from require_module, or None when the test declined."""
    pa = expect.require_module("pyarrow", name="pyarrow is required to write the fixture")
    if pa is None:
        return None
    return pa, expect.require_module("pyarrow.parquet", name="pyarrow is required to write the fixture")


def test_the_listing_fixture_is_up(fl, expect):
    run = fl
    mods = _modules(expect)
    if mods is None:
        return
    fl = run(*mods)
    expect.num(fl["fixture up"], 1, "premise: fixture up")


def test_a_valid_listing_reaches_the_parser(fl, expect):
    run = fl
    mods = _modules(expect)
    if mods is None:
        return
    fl = run(*mods)
    expect.text(fl["control"], "3", "control: a valid listing reaches the parser and reads the object")


def test_no_listing_mutant_crashed_the_backend(fl, expect):
    run = fl
    mods = _modules(expect)
    if mods is None:
        return
    fl = run(*mods)
    expect.num(fl["crash"], 0, "no mutant crashed the backend")


def test_no_listing_mutant_hung_the_parser(fl, expect):
    run = fl
    mods = _modules(expect)
    if mods is None:
        return
    fl = run(*mods)
    expect.num(fl["hang"], 0, "no mutant hung the parser")


def test_no_listing_mutant_tripped_a_sanitizer(fl, expect):
    run = fl
    mods = _modules(expect)
    if mods is None:
        return
    fl = run(*mods)
    expect.num(fl["san"], 0, "no mutant tripped a sanitizer")


def test_every_listing_mutant_ran(fl, expect):
    run = fl
    mods = _modules(expect)
    if mods is None:
        return
    fl = run(*mods)
    expect.num(fl["error"] + fl["clean"], ITERS, "every mutant ran (errors + clean == iters)")


def test_every_listing_iteration_wrote_a_fresh_mutant(fl, expect):
    run = fl
    mods = _modules(expect)
    if mods is None:
        return
    fl = run(*mods)
    expect.num(fl["fresh"], ITERS, "every iteration wrote a fresh mutant")


def test_the_listing_corpus_reached_the_parser(fl, expect):
    run = fl
    mods = _modules(expect)
    if mods is None:
        return
    fl = run(*mods)
    expect.text("yes" if fl["error"] + fl["clean"] > 0 else "no", "yes",
                "the corpus reached the parser at all (some mutant errored or accepted)")
