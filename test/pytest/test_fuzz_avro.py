"""The Avro manifest decoder survives malformed input (#388). The pytest twin of
`test/fuzz_avro.sh`, every check by the shell's name.

Each iteration writes one mutant with `test/avro_corpus.py` -- a pure function of one
integer seed over the committed pyiceberg manifest and manifest list, alternating
between the two by seed parity -- and reads it through `read_avro_manifest` or
`read_manifest_list`. The property is the shell's: a malformed file makes the backend
raise an ERROR, never die, hang or trip a sanitizer. Outcomes are classified as the
shell classifies them, from the server log written during the statement first and the
client's error second.

Two anti-vacuity arms carry the shell's #1341 fixes. The mutant is removed before it
is generated, so a generator that writes nothing is counted rather than read as the
previous file; and a statement that failed opening the file did not reach the decoder.

PGC_SEED and PGC_ITERS are the shell's knobs, with its defaults, and the seed is
printed so a failure reproduces exactly.
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
FX = TEST / "fixtures" / "iceberg"
CORPUS = TEST / "avro_corpus.py"
SEED = int(os.environ.get("PGC_SEED", "20260814"))
ITERS = int(os.environ.get("PGC_ITERS", "300"))

SANITIZER = re.compile(r"AddressSanitizer|runtime error:|UndefinedBehaviorSanitizer|LeakSanitizer")
CRASHED = re.compile(r"was terminated by signal|server process .* exited with|crashed")
LOST = re.compile(r"server closed the connection|connection to server was lost")
FILE_ERROR = re.compile(r"could not open|No such file|Permission denied")


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


@pytest.fixture(scope="module")
def fa(pgc_cluster, server_dir):
    dsn = pgc_cluster.dsn()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS pgcolumnar")
    mut = server_dir / "mutant.avro"
    base, base_list = FX / "manifest-0.avro", FX / "manifest-list.avro"
    rd_manifest = f"SELECT count(*) FROM pgcolumnar.read_avro_manifest('{mut}')"
    rd_list = f"SELECT count(*) FROM pgcolumnar.read_manifest_list('{mut}')"

    def generate(src, arg):
        mut.unlink(missing_ok=True)
        subprocess.run([sys.executable, str(CORPUS), str(src), str(arg), str(mut)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if mut.exists():
            mut.chmod(0o644)
            return True
        return False

    r = {}
    generate(base, "valid")
    r["control manifest"] = _value(dsn, rd_manifest)
    generate(base_list, "valid")
    r["control list"] = _value(dsn, rd_list)

    log = _Log(pgc_cluster.datadir / "server.log")
    n = dict(crash=0, hang=0, san=0, error=0, clean=0, fresh=0, fileerr=0)
    print(f"-- fuzzing {ITERS} Avro manifest mutants (seed base {SEED}); reproduce with PGC_SEED={SEED}")
    for i in range(ITERS):
        s = SEED + i
        src, rd = (base, rd_manifest) if i % 2 == 0 else (base_list, rd_list)
        if generate(src, s):
            n["fresh"] += 1
        kind, message = _outcome(dsn, log, rd)
        if kind == "gone":
            print(f"FATAL: cluster gone at seed={s}")
            break
        if kind in ("crash", "hang", "san"):
            print(f"  FINDING [{kind}] seed={s}: {message}")
        n[kind] += 1
        if kind == "error" and FILE_ERROR.search(message):
            n["fileerr"] += 1
    print(f"-- done: {ITERS} mutants, {n['error']} rejected with ERROR, {n['clean']} accepted clean")
    r.update(n)
    yield r


def _counts(fa):
    return fa


def test_the_valid_manifest_reaches_the_avro_decoder(fa, expect):
    expect.text(_counts(fa)["control manifest"], "2", "control: the valid manifest reaches the decoder (2 entries)")


def test_the_valid_manifest_list_reaches_the_avro_decoder(fa, expect):
    expect.text(_counts(fa)["control list"], "1", "control: the valid manifest-list reaches the decoder (1 entry)")


def test_no_avro_mutant_crashed_the_backend(fa, expect):
    expect.num(_counts(fa)["crash"], 0, "no mutant crashed the backend")


def test_no_avro_mutant_hung_the_decoder(fa, expect):
    expect.num(_counts(fa)["hang"], 0, "no mutant hung the decoder")


def test_no_avro_mutant_tripped_a_sanitizer(fa, expect):
    expect.num(_counts(fa)["san"], 0, "no mutant tripped a sanitizer")


def test_every_avro_mutant_ran(fa, expect):
    c = _counts(fa)
    expect.num(c["error"] + c["clean"], ITERS, "every mutant ran (errors + clean == iters)")


def test_every_avro_iteration_wrote_a_fresh_mutant(fa, expect):
    expect.num(_counts(fa)["fresh"], ITERS, "every iteration wrote a fresh mutant")


def test_the_avro_corpus_reached_the_decoder(fa, expect):
    c = _counts(fa)
    reached = c["error"] - c["fileerr"] + c["clean"]
    expect.text("yes" if reached > 0 else f"no ({c['fileerr']} of {c['error']} errors were opening the file)",
                "yes", "the corpus reached the decoder at all")


def test_the_avro_manifest_fixtures_are_present(expect):
    """The shell skips the whole suite without the committed fixtures, under this name."""
    if not (FX / "manifest-0.avro").is_file():
        expect.cannot_run("ABSENT_FIXTURE", "the committed Iceberg manifest fixtures are not in this tree",
                          name="iceberg manifest fixtures are missing")
        return
    expect.at_least((FX / "manifest-list.avro").stat().st_size, 1,
                    "premise: the committed manifest-list fixture is not empty")


def test_python_can_hash_the_avro_corpus_seeds(expect):
    """The shell's other skip name: the corpus seeds its mutations through hashlib."""
    try:
        import hashlib as _hashlib
    except ImportError:
        expect.cannot_run("MISSING_DEPENDENCY", "hashlib is not importable", name="python3 is required")
        return
    expect.text(_hashlib.sha256(b"").hexdigest()[:8], "e3b0c442",
                "premise: hashlib digests as the corpus expects, so its mutants are the shell's")
