"""The Arrow IPC decoder survives malformed input (#214). The pytest twin of
`test/fuzz_arrow.sh`, every check by the shell's name.

Seeds come from `test/arrow_corpus.py`; a seed is kept only if `import_arrow` accepts it
pristine into a table of its own column list, so a mutant exercises the decode and not
only the schema check. Each iteration mutates one seed with `test/arrow_mutate.py` --
a pure function of (seed file, integer seed) -- and imports it. The property is the
shell's: a malformed file makes the backend raise an ERROR, never die, hang or trip a
sanitizer, classified from the server log first and the client's error second.

Two #1341 arms: a seed corpus that is empty, or that the importer rejects outright, is
a named check rather than an unnamed FAIL; and every mutant must be generated and
imported, where a mutate that failed was skipped and a run that generated almost
nothing passed.

PGC_SEED and PGC_ITERS are the shell's knobs, with its defaults.
"""
import os
import pathlib
import re
import subprocess
import sys
import time

import psycopg
import pytest

from pgc_vacuity import query_error

TEST = pathlib.Path(__file__).resolve().parents[1]
SEED = int(os.environ.get("PGC_SEED", "20260728"))
ITERS = int(os.environ.get("PGC_ITERS", "100"))

SANITIZER = re.compile(r"AddressSanitizer|runtime error:|UndefinedBehaviorSanitizer|LeakSanitizer")
CRASHED = re.compile(r"was terminated by signal|server process .* exited with|crashed")
LOST = re.compile(r"server closed the connection unexpectedly|connection to server was lost|terminating connection")


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


def _run(dsn, sql):
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)
        return True
    except psycopg.Error:
        return False


def _seeds(kind, corpus_dir):
    """-> the corpus generator's lines, "<name> <path> [column list]"."""
    out = subprocess.run([sys.executable, str(TEST / f"{kind}_corpus.py"), str(corpus_dir)],
                         capture_output=True, text=True)
    return [l for l in out.stdout.splitlines() if l.strip()]


def _mutate(kind, src, seed, dst):
    dst.unlink(missing_ok=True)
    ok = subprocess.run([sys.executable, str(TEST / f"{kind}_mutate.py"), str(src), str(seed), str(dst)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if ok and dst.exists():
        dst.chmod(0o644)
        return True
    return False


DB = "pgc_fuzz_arrow"


@pytest.fixture(scope="module")
def fz(pgc_cluster, server_dir):
    with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
        a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        a.execute(f"CREATE DATABASE {DB}")
    try:
        dsn = pgc_cluster.dsn(dbname=DB)
        _run(dsn, "CREATE EXTENSION IF NOT EXISTS pgcolumnar")
        corpus = server_dir / "corpus"
        corpus.mkdir(exist_ok=True)
        corpus.chmod(0o777)
        lines = _seeds("arrow", corpus)
        r = {"seed files": len(lines)}
        _run(dsn, "DROP TYPE IF EXISTS pgc_fuzz_xy CASCADE; CREATE TYPE pgc_fuzz_xy AS (x int, y text)")
        seeds = []
        for line in lines:
            parts = line.split(" ", 2)
            if len(parts) < 3 or not parts[2].strip():
                continue
            path, cols, tab = parts[1], parts[2], f"arrt_{len(seeds)}"
            pathlib.Path(path).chmod(0o644)
            if not _run(dsn, f"DROP TABLE IF EXISTS {tab}; CREATE TABLE {tab} ({cols}) USING pgcolumnar"):
                continue
            if _run(dsn, f"SELECT pgcolumnar.import_arrow('{tab}', '{path}')"):
                _run(dsn, f"TRUNCATE {tab}")
                seeds.append((path, tab))
        r["pristine"] = len(seeds)

        mut = server_dir / "mutant.arrows"
        log = _Log(pgc_cluster.datadir / "server.log")
        n = dict(crash=0, hang=0, san=0, error=0, clean=0, gen=0)
        print(f"-- seed={SEED} iters={ITERS}; reproduce with PGC_SEED={SEED}")
        for i in range(ITERS if seeds else 0):
            path, tab = seeds[i % len(seeds)]
            ms = SEED + i
            if not _mutate("arrow", path, ms, mut):
                continue
            n["gen"] += 1
            _run(dsn, f"TRUNCATE {tab}")
            kind, message = _outcome(dsn, log, f"SELECT pgcolumnar.import_arrow('{tab}', '{mut}')")
            if kind == "gone":
                print(f"FATAL: cluster unreachable at seed={ms}")
                break
            if kind in ("crash", "hang", "san"):
                print(f"  FINDING [{kind}] seed={ms} from {pathlib.Path(path).name}: {message}")
            n[kind] += 1
        print(f"-- done: {ITERS} mutants, {n['error']} rejected with ERROR, {n['clean']} accepted")
        r.update(n)
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_the_arrow_seed_corpus_generator_produced_files(request, expect):
    if expect.require_module("pyarrow.ipc", name="pyarrow not available; the Arrow fuzzer needs it to build seeds") is None:
        return
    fz = request.getfixturevalue("fz")  # after the decline: the seeds need pyarrow
    expect.text("yes" if fz["seed files"] > 0 else "no", "yes", "the seed corpus generator produced files")


def test_the_arrow_importer_accepts_some_pristine_seed(request, expect):
    if expect.require_module("pyarrow.ipc", name="pyarrow not available; the Arrow fuzzer needs it to build seeds") is None:
        return
    fz = request.getfixturevalue("fz")  # after the decline: the seeds need pyarrow
    expect.text("yes" if fz["pristine"] > 0 else "no", "yes", "the importer accepts some pristine seed")


def test_no_arrow_mutant_crashed_the_backend(request, expect):
    if expect.require_module("pyarrow.ipc", name="pyarrow not available; the Arrow fuzzer needs it to build seeds") is None:
        return
    fz = request.getfixturevalue("fz")  # after the decline: the seeds need pyarrow
    expect.num(fz["crash"], 0, "no mutant crashed the backend")


def test_no_arrow_mutant_hung_the_decode(request, expect):
    if expect.require_module("pyarrow.ipc", name="pyarrow not available; the Arrow fuzzer needs it to build seeds") is None:
        return
    fz = request.getfixturevalue("fz")  # after the decline: the seeds need pyarrow
    expect.num(fz["hang"], 0, "no mutant hung the decode")


def test_no_arrow_mutant_tripped_a_sanitizer(request, expect):
    if expect.require_module("pyarrow.ipc", name="pyarrow not available; the Arrow fuzzer needs it to build seeds") is None:
        return
    fz = request.getfixturevalue("fz")  # after the decline: the seeds need pyarrow
    expect.num(fz["san"], 0, "no mutant tripped a sanitizer")


def test_every_arrow_mutant_was_generated_and_imported(request, expect):
    if expect.require_module("pyarrow.ipc", name="pyarrow not available; the Arrow fuzzer needs it to build seeds") is None:
        return
    fz = request.getfixturevalue("fz")  # after the decline: the seeds need pyarrow
    ran = fz["error"] + fz["clean"] + fz["crash"] + fz["hang"] + fz["san"]
    expect.text(f"{fz['gen']}/{ran}", f"{ITERS}/{ITERS}", "every mutant was generated and ran the import")


def test_the_arrow_corpus_reached_the_decoder(request, expect):
    if expect.require_module("pyarrow.ipc", name="pyarrow not available; the Arrow fuzzer needs it to build seeds") is None:
        return
    fz = request.getfixturevalue("fz")  # after the decline: the seeds need pyarrow
    expect.text("yes" if fz["error"] > 0 else f"no (errors={fz['error']} clean={fz['clean']})", "yes",
                "the corpus reached the decoder at all")
