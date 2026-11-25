#!/usr/bin/env python3
"""The sanitizer gate, run through the pytest twins (#224, #2038).

The ordinary matrix is assert builds, none instrumented, so a memory-safety defect
that does not fault -- an unaligned load, an out-of-bounds read that lands inside
the process -- passes green on all of them. #225 was exactly that: an unaligned
varlena-header read on the plain write path, benign on x86_64.

This runs the pytest twins of a subset of the suites against the ASAN+UBSAN build
from test/build_san.sh, with violations made fatal (abort_on_error), so a violation
kills the process that hit it. It is run_san.sh's gate with the twins in place of
the shell suites, and it decides the same way:

- the build must carry -fsanitize=address,undefined and -fno-sanitize=function, the
  flags the gate depends on, read from its own Makefile.global;
- a suite FAILS on a failed run or on ANY sanitizer report, and is DECLINED when its
  run declined a test (pgc_vacuity's exit 67);
- the gate fails on any failed suite, is INCOMPLETE on any declined one, and does
  not pass when no suite ran.

REPORTS ARE READ FROM FILES, NOT FROM OUTPUT. run_san.sh grepped each suite's
output, which saw a report only when the suite printed it. Here ASAN_OPTIONS and
UBSAN_OPTIONS carry `log_path`, so every instrumented process -- a backend, initdb,
pg_config, a cluster a twin started itself -- writes its report to
`<suite's log directory>/san.<pid>`. A report is charged to the suite that was
running, and the directory is kept for the log collector.

The subset and its proof are run_san.sh's. Undo #225 in its TRUE form -- delete the
PgColumnarVarSizeAnyUnaligned helper and restore VARSIZE_ANY at the three call
sites -- and the gate goes red, while the ordinary matrix stays green. The gate's
stated limit is run_san.sh's too: clang stops emitting the alignment check across
an inline boundary at -O1, so an unaligned read inside an inlined wrapper is not
reported here.

Usage:  test/pytest/run_san.py [SAN_PREFIX]     (default /usr/local/pg18_san)
        test/pytest/run_san.py --list-suites
  PGC_SAN_SUITES=<list>    override the suite subset
  PGC_SAN_FUZZ_ITERS=<n>   fuzzer mutants under sanitizers (default 200)
  PGC_SAN_LOGS=<dir>       where the per-suite report directories go
"""

import os
import pathlib
import re
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
SRCDIR = HERE.parent.parent

# The subset: the write, read, encode and import/decode paths, where the hand-rolled
# buffer walks live. run_san.sh's, by name; test_sanitizer_gate.py holds the two equal
# while the shell gate exists.
SUITES = ("smoke native_writer native_roundtrip native_encoding native_zonemap "
          "write_fsst_compressed write_minmax_fastpath encode_effort encode_invariants "
          "native_dml native_skip native_fetch_position native_fetch_cache "
          "arrow_import arrow_export parquet_import parquet_export native_read_parquet "
          "native_parquet_schema hardening corruption differential fuzz_arrow "
          "fuzz_parquet").split()

FLAGS = ("-fsanitize=address,undefined", "-fno-sanitize=function")

# pg_config and the other tools leak by design at exit, and initdb trips
# stack-use-after-return; build_san.sh's options, plus the two that make a
# violation fatal and send its report to a file.
ASAN = "detect_leaks=0:detect_stack_use_after_return=0:abort_on_error=1:print_stacktrace=1"
UBSAN = "halt_on_error=1:abort_on_error=1:print_stacktrace=1"

EXIT_INCOMPLETE = 67  # pgc_vacuity.EXIT_INCOMPLETE: a test declined.
TIMEOUT = 900
SANITIZER_LINE = re.compile(r"runtime error:|AddressSanitizer|UndefinedBehaviorSanitizer|"
                            r"SUMMARY: .*Sanitizer")


def twin_files(suites, here=HERE, complete=None):
    """([twin path], [refused "suite(why)"]) for the suites, in order.

    A suite is run by `test_<suite>.py` only when that twin is graded complete
    against the shell suite (test_compare_to_bash.COMPLETE). A twin that misses
    checks would sanitize less than the shell gate did, and say nothing.
    """
    if complete is None:
        from test_compare_to_bash import COMPLETE
        complete = set(COMPLETE)
    files, refused = [], []
    for s in suites:
        f = here / f"test_{s}.py"
        if not f.is_file():
            refused.append(f"{s}(no twin)")
        elif s not in complete:
            refused.append(f"{s}(twin not graded complete)")
        else:
            files.append(f)
    return files, refused


def missing_flags(makefile_global):
    """The sanitizer flags the gate depends on that the build's Makefile.global lacks."""
    return [f for f in FLAGS if f not in makefile_global]


def reports(logdir):
    """["file: first sanitizer line"] for every report the sanitizers wrote in `logdir`."""
    d = pathlib.Path(logdir)
    if not d.is_dir():
        return []
    out = []
    for f in sorted(p for p in d.iterdir() if p.is_file()):
        text = f.read_text(errors="replace")
        line = next((ln.strip() for ln in text.splitlines() if SANITIZER_LINE.search(ln)),
                    next((ln.strip() for ln in text.splitlines() if ln.strip()),
                         "(empty report)"))
        out.append(f"{f.name}: {line}")
    return out


def verdict(rc, found):
    """PASS, DECLINED or FAIL for one suite's run."""
    if found:
        return "FAIL"
    if rc == 0:
        return "PASS"
    if rc == EXIT_INCOMPLETE:
        return "DECLINED"
    return "FAIL"


def gate(results):
    """(final line, exit code) over [(suite, verdict)]."""
    if not results:
        return "SANITIZER GATE FAILED: no suite ran", 1
    failed = sum(1 for _, v in results if v == "FAIL")
    declined = sum(1 for _, v in results if v == "DECLINED")
    if failed:
        return "SANITIZER GATE FAILED", 1
    if declined:
        # ANY decline, as run_san.sh: a sanitizer run is worth what it covered.
        return f"SANITIZER GATE INCOMPLETE: {declined} of {len(results)} suites declined", 1
    return "SANITIZER GATE PASSED", 0


def _pytest(argv, env, timeout):
    try:
        p = subprocess.run(argv, cwd=HERE, env=env, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ""
        return 124, (out if isinstance(out, str) else out.decode(errors="replace"))


def _build(pgc, env=None):
    """A clean instrumented build and install: objects from an ordinary build of the
    same major would otherwise be reused, uninstrumented."""
    subprocess.run(["make", "-s", "-C", str(SRCDIR), "clean"], capture_output=True, env=env)
    p = subprocess.run(["make", "-s", "-C", str(SRCDIR), f"PG_CONFIG={pgc}", "install"],
                       capture_output=True, text=True, env=env)
    return p.returncode == 0, (p.stdout + p.stderr)[-3000:]


def _say(line):
    print(line, flush=True)


def main(argv, runner=_pytest, suites=None, build=None, out=_say):
    if suites is None:
        suites = os.environ.get("PGC_SAN_SUITES", "").split() or SUITES
    if argv[:1] == ["--list-suites"]:
        for s in suites:
            out(s)
        return 0

    san = pathlib.Path(argv[0] if argv else "/usr/local/pg18_san")
    pgc = san / "bin" / "pg_config"
    if not os.access(pgc, os.X_OK):
        out(f"FAIL  no sanitizer build at {san}; run test/build_san.sh first")
        return 1

    env = dict(os.environ, ASAN_OPTIONS=ASAN, UBSAN_OPTIONS=UBSAN)
    pgxs = subprocess.run([str(pgc), "--pgxs"], capture_output=True, text=True, env=env)
    mk = pathlib.Path(pgxs.stdout.strip()).parent.parent / "Makefile.global"
    lacking = missing_flags(mk.read_text() if mk.is_file() else "")
    if lacking:
        out(f"FAIL  {san} is not the expected ASAN+UBSAN build")
        out(f"      (its Makefile.global lacks {' and '.join(lacking)}; "
            "rebuild with test/build_san.sh)")
        return 1

    files, refused = twin_files(suites)
    if refused:
        out(f"FAIL  no complete pytest twin to sanitize: {' '.join(refused)}")
        return 1

    out(f"-- building the extension instrumented against {san}")
    ok, log = (build or (lambda p: _build(p, env)))(pgc)
    if not ok:
        out("FAIL  extension build against the sanitizer PostgreSQL")
        out(log)
        return 1

    logs = pathlib.Path(os.environ.get("PGC_SAN_LOGS") or tempfile.mkdtemp(prefix="pgc-san-"))
    iters = os.environ.get("PGC_SAN_FUZZ_ITERS", "200")
    out(f"-- running the subset's twins under ASAN+UBSAN (fatal); reports in {logs}")
    results = []
    for s, f in zip(suites, files):
        d = logs / s
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o1777)
        log_path = f"log_path={d / 'san'}"
        suite_env = dict(env, ASAN_OPTIONS=f"{ASAN}:{log_path}",
                         UBSAN_OPTIONS=f"{UBSAN}:{log_path}", PGC_ITERS=iters)
        rc, output = runner([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                             "--pg-config", str(pgc), str(f)], suite_env, TIMEOUT)
        found = reports(d)
        v = verdict(rc, found)
        results.append((s, v))
        if v == "PASS":
            out(f"  PASS  {s}")
        elif v == "DECLINED":
            out(f"  DECLINED  {s}  (rc={rc}: a test declined)")
        else:
            out(f"  FAIL  {s}  (rc={rc}, sanitizer_reports={len(found)})")
            shown = found[:4] or [ln for ln in output.splitlines()
                                  if re.search(r"FAILED|Error", ln)][:4]
            for ln in shown:
                out(f"        {ln}")
    failed = sum(1 for _, v in results if v == "FAIL")
    declined = sum(1 for _, v in results if v == "DECLINED")
    out(f"-- {len(results)} suites under sanitizers, {failed} failed, {declined} declined")
    line, rc = gate(results)
    out(line)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
