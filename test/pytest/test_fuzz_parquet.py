"""The Parquet decoder survives malformed input (#214, #210). The pytest twin of
`test/fuzz_parquet.sh`, every check by the shell's name.

Seeds come from `test/parquet_corpus.py`; a seed is kept only if `parquet_schema`
renders its column list, so a mutant exercises the decode and not only the argument
check. Each iteration mutates one seed with `test/parquet_mutate.py` -- a pure function
of (seed file, integer seed) -- and feeds it to every entry point that parses one:
parquet_schema, read_parquet, and import_parquet, which also reaches the insert path. The property is the
shell's: a malformed file makes the backend raise an ERROR, never die, hang or trip a
sanitizer, classified from the server log first and the client's error second.

Two #1341 arms: a seed corpus that is empty, or whose schemas the reader will not render, is
a named check rather than an unnamed FAIL; and every mutant must be generated and
run through its three statements, where a mutate that failed was skipped and a run that generated almost
nothing passed.

THE RESULT ORACLE (#1852). The read statement returns the row count and an md5 of every
row as text. A mutant that reads without an error must give the seed's answer, or the
answer pgColumnar gives for pyarrow's rewrite of the mutant (`test/parquet_oracle.py`).
Any other answer is a wrong answer, including a different answer from a mutant pyarrow
refuses, which is how #1855 looked. After the random mutants, the schema of the int64
seed is swept: one copy per byte of the footer's schema list and per low-nibble value.

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

from pgc_vacuity import QUERY_ERROR, query_error

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
    with the client's message and the statement's last value (the read's answer). The shell's order: sanitizer and crash from the log,
    then a timeout, then a lost connection, then an ERROR."""
    log.since()
    failure = None
    rows = []
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            conn.execute("SET statement_timeout = '20s'")
            rows = conn.execute(sql).fetchall()
    except psycopg.Error as exc:
        failure = exc
    # the read statement's one value, the answer the oracle judges
    answer = "" if not rows or rows[-1][0] is None else str(rows[-1][0])
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
        return "gone", message, answer
    new = log.since()
    if SANITIZER.search(new):
        _cluster_back(dsn)
        return "san", message, answer
    if CRASHED.search(new):
        _cluster_back(dsn)
        return "crash", message, answer
    if kind == "hang":
        return "hang", message, answer
    if kind == "lost":
        _cluster_back(dsn)
        return "crash", message, answer
    if kind in ("error", "unreachable"):
        return "error", message, answer
    return "clean", message, answer


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


def _answer_sql(path, cols):
    return ("SELECT count(*) || '/' || coalesce(md5(string_agg(t::text, chr(10))), '-') "
            f"FROM pgcolumnar.read_parquet('{path}') AS t({cols})")


def _answer(dsn, path, cols):
    """-> the answer for `path`, or the failure as a query_error(), unique, so two
    failed reads never compare equal."""
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            conn.execute("SET statement_timeout = '20s'")
            row = conn.execute(_answer_sql(path, cols)).fetchone()
    except psycopg.Error as exc:
        return query_error(_message(exc))
    return "" if row is None or row[0] is None else str(row[0])


def _oracle(mut, rewrite):
    """-> (rc, last line): pyarrow's rewrite of `mut`, as the shell runs it."""
    rewrite.unlink(missing_ok=True)
    try:
        out = subprocess.run([sys.executable, str(TEST / "parquet_oracle.py"), str(mut), str(rewrite)],
                             capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return 124, "pyarrow timed out"
    lines = (out.stdout + out.stderr).strip().splitlines()
    return out.returncode, lines[-1] if lines else ""


def _mutate(kind, src, seed, dst):
    dst.unlink(missing_ok=True)
    ok = subprocess.run([sys.executable, str(TEST / f"{kind}_mutate.py"), str(src), str(seed), str(dst)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if ok and dst.exists():
        dst.chmod(0o644)
        return True
    return False


DB = "pgc_fuzz_parquet"


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
        lines = _seeds("parquet", corpus)
        r = {"seed files": len(lines)}
        seeds = []
        for line in lines:
            path = line.rsplit(" ", 1)[-1]
            pathlib.Path(path).chmod(0o644)
            cols = _value(dsn, "SELECT string_agg(format('%I %s', column_name, data_type), ', ') "
                               f"FROM pgcolumnar.parquet_schema('{path}')")
            if cols.startswith("QUERY_ERROR") or not cols:
                continue
            seeds.append((path, cols))
        r["rendered"] = len(seeds)

        rewrite = server_dir / "rewrite.parquet"
        seedans = []
        agree, nread, differ = 0, 0, []
        for path, cols in seeds:
            ans = _answer(dsn, path, cols)
            seedans.append(ans)
            if ans.startswith(QUERY_ERROR):
                continue
            nread += 1
            _oracle(pathlib.Path(path), rewrite)
            rw = _answer(dsn, rewrite, cols)
            if rw == ans:
                agree += 1
            else:
                differ.append(f"{pathlib.Path(path).name}: [{ans}] vs [{rw}]")
        r["seeds agree"] = f"{agree}/{nread}" + (f" (differ: {' '.join(differ)})" if differ else "")
        r["seeds read"] = nread
        o = dict(same=0, agree=0, wrong=0, unjudged=0, first="")

        def judge(idx, mut, out, label):
            got, want = out, seedans[idx]
            if got == want:
                o["same"] += 1
                return
            rc, why = _oracle(mut, rewrite)
            if rc == 0:
                rw = _answer(dsn, rewrite, seeds[idx][1])
                if rw == got:
                    o["agree"] += 1
                    return
                if rw.startswith(QUERY_ERROR):
                    o["unjudged"] += 1
                    return
                why = f"pyarrow reads [{rw}]"
            elif rc != 2:
                o["unjudged"] += 1
                return
            o["wrong"] += 1
            detail = f"{label}: pgColumnar read [{got}] without an error; seed answer [{want}]; {why}"
            o["first"] = o["first"] or detail
            print(f"  FINDING [wrong] {detail}")

        mut = server_dir / "mutant.parquet"
        log = _Log(pgc_cluster.datadir / "server.log")
        n = dict(crash=0, hang=0, san=0, error=0, clean=0, gen=0)
        print(f"-- seed={SEED} iters={ITERS}; reproduce with PGC_SEED={SEED}")
        _run(dsn, "DROP TABLE IF EXISTS fz_target")
        gone = False
        for i in range(ITERS if seeds else 0):
            path, cols = seeds[i % len(seeds)]
            ms = SEED + i
            if not _mutate("parquet", path, ms, mut):
                continue
            n["gen"] += 1
            for setup, sql in ((None, f"SELECT * FROM pgcolumnar.parquet_schema('{mut}')"),
                               (None, _answer_sql(mut, cols)),
                               (f"DROP TABLE IF EXISTS fz_target; CREATE TABLE fz_target ({cols}) USING pgcolumnar",
                                f"SELECT pgcolumnar.import_parquet('fz_target', '{mut}')")):
                if setup:
                    _run(dsn, setup)
                kind, message, out = _outcome(dsn, log, sql)
                if kind == "gone":
                    gone = True
                    break
                if kind in ("crash", "hang", "san"):
                    print(f"  FINDING [{kind}] seed={ms} from {pathlib.Path(path).name}: {message}")
                n[kind] += 1
                if kind == "clean" and "read_parquet" in sql:
                    judge(i % len(seeds), mut, out, f"seed={ms} from {pathlib.Path(path).name}")
            if gone:
                print(f"FATAL: cluster unreachable at seed={ms}")
                break
        print(f"-- done: {ITERS} mutants, {n['error']} rejected with ERROR, {n['clean']} accepted")
        r.update(n)

        # the schema sweep of the int64 seed (#1852, #1855)
        sweep_idx = next((k for k, (p, _c) in enumerate(seeds) if pathlib.Path(p).name == "int64.parquet"), None)
        sweep_gen = sweep_ran = 0
        if sweep_idx is not None and not gone:
            out = subprocess.run([sys.executable, str(TEST / "parquet_mutate.py"), "--schema-sweep",
                                  seeds[sweep_idx][0], str(server_dir / "sweep")],
                                 capture_output=True, text=True)
            files = [l for l in out.stdout.splitlines() if l.strip()]
            sweep_gen = len(files)
            print(f"-- schema sweep: {sweep_gen} copies of int64.parquet")
            for f in files:
                pathlib.Path(f).chmod(0o644)
                kind, message, ans = _outcome(dsn, log, _answer_sql(f, seeds[sweep_idx][1]))
                if kind == "gone":
                    break
                sweep_ran += 1
                tag = pathlib.Path(f).stem
                if kind in ("crash", "hang", "san"):
                    print(f"  FINDING [{kind}] {tag} of int64.parquet: {message}")
                    r[kind] += 1
                if kind == "clean":
                    judge(sweep_idx, pathlib.Path(f), ans, f"{tag} of int64.parquet (byte offset, low nibble)")
        r["sweep"] = f"{sweep_ran}/{sweep_gen}"
        r["sweep want"] = f"{sweep_gen}/{sweep_gen}" if sweep_gen > 0 else "at least one copy"
        print(f"-- oracle: {o['same']} same as the seed, {o['agree']} agree with pyarrow, "
              f"{o['wrong']} wrong, {o['unjudged']} not judged")
        r["oracle"] = o
        yield r
    finally:
        with psycopg.connect(pgc_cluster.dsn(), autocommit=True) as a:
            a.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


def test_the_parquet_seed_corpus_generator_produced_files(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; the Parquet fuzzer needs it to build seeds") is None:
        return
    fz = request.getfixturevalue("fz")  # after the decline: the seeds need pyarrow
    expect.text("yes" if fz["seed files"] > 0 else "no", "yes", "the seed corpus generator produced files")


def test_the_parquet_reader_renders_some_pristine_seeds_schema(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; the Parquet fuzzer needs it to build seeds") is None:
        return
    fz = request.getfixturevalue("fz")  # after the decline: the seeds need pyarrow
    expect.text("yes" if fz["rendered"] > 0 else "no", "yes", "the reader renders some pristine seed's schema")


def test_no_parquet_mutant_crashed_the_backend(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; the Parquet fuzzer needs it to build seeds") is None:
        return
    fz = request.getfixturevalue("fz")  # after the decline: the seeds need pyarrow
    expect.num(fz["crash"], 0, "no mutant crashed the backend")


def test_no_parquet_mutant_hung_the_decode(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; the Parquet fuzzer needs it to build seeds") is None:
        return
    fz = request.getfixturevalue("fz")  # after the decline: the seeds need pyarrow
    expect.num(fz["hang"], 0, "no mutant hung the decode")


def test_no_parquet_mutant_tripped_a_sanitizer(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; the Parquet fuzzer needs it to build seeds") is None:
        return
    fz = request.getfixturevalue("fz")  # after the decline: the seeds need pyarrow
    expect.num(fz["san"], 0, "no mutant tripped a sanitizer")


def test_every_parquet_mutant_was_generated_and_ran_its_statements(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; the Parquet fuzzer needs it to build seeds") is None:
        return
    fz = request.getfixturevalue("fz")  # after the decline: the seeds need pyarrow
    ran = fz["error"] + fz["clean"] + fz["crash"] + fz["hang"] + fz["san"]
    expect.text(f"{fz['gen']}/{ran}", f"{ITERS}/{ITERS * 3}", "every mutant was generated and ran its three statements")


def test_the_parquet_corpus_reached_the_decoder(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; the Parquet fuzzer needs it to build seeds") is None:
        return
    fz = request.getfixturevalue("fz")  # after the decline: the seeds need pyarrow
    expect.text("yes" if fz["error"] > 0 else f"no (errors={fz['error']} clean={fz['clean']})", "yes",
                "the corpus reached the decoder at all")


def test_the_oracles_rewrite_of_every_pristine_seed_reads_the_seeds_answer(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; the Parquet fuzzer needs it to build seeds") is None:
        return
    fz = request.getfixturevalue("fz")  # after the decline: the seeds need pyarrow
    n = fz["seeds read"]
    expect.text(fz["seeds agree"], f"{n}/{n}" if n > 0 else "some seed reads",
                "the oracle's rewrite of every pristine seed reads the seed's answer")


def test_no_parquet_mutant_that_reads_returned_a_wrong_answer(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; the Parquet fuzzer needs it to build seeds") is None:
        return
    fz = request.getfixturevalue("fz")  # after the decline: the seeds need pyarrow
    o = fz["oracle"]
    expect.text(f"{o['wrong']}" + (f" (first: {o['first']})" if o["first"] else ""), "0",
                "no mutant that reads returned a wrong answer")


def test_every_schema_sweep_copy_ran_its_read(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; the Parquet fuzzer needs it to build seeds") is None:
        return
    fz = request.getfixturevalue("fz")  # after the decline: the seeds need pyarrow
    expect.text(fz["sweep"], fz["sweep want"], "every schema-sweep copy ran its read")


def test_the_result_oracle_compared_some_answers(request, expect):
    if expect.require_module("pyarrow.parquet", name="pyarrow not available; the Parquet fuzzer needs it to build seeds") is None:
        return
    fz = request.getfixturevalue("fz")  # after the decline: the seeds need pyarrow
    o = fz["oracle"]
    expect.text("yes" if o["same"] + o["agree"] > 0
                else f"no (same={o['same']} agree={o['agree']} unjudged={o['unjudged']})", "yes",
                "the result oracle compared some answers")
