"""run_san.py, the pytest sanitizer gate, decides what run_san.sh decides (#2038).

The nightly's sanitizer job ran 24 SHELL suites against the ASAN+UBSAN build, so it
was one of the three gates that kept the shell suites from being retired.
run_san.py runs the pytest twins of the same subset. These tests hold its
decisions, each on a stand-in, so they need no sanitizer build, no cluster and no
driver:

- the subset is run_san.sh's, and every suite in it has a complete twin;
- a build without the sanitizer flags is refused, as is a missing one;
- every report a sanitizer writes is found, from any process, through `log_path`;
- a suite fails on a failed run OR on any report, and a declined suite makes the
  gate incomplete, not passed;
- a gate that ran no suite does not pass.

The end-to-end proof, the #225 mutant turning the gate red on a real sanitizer
build, is in TESTS.md: it needs a sanitizer PostgreSQL, which no CI job but the
nightly's builds.
"""

import os
import pathlib
import stat
import subprocess
import sys

import run_san

HERE = pathlib.Path(__file__).resolve().parent


def _stub_prefix(tmp_path, flags):
    """A prefix whose pg_config points --pgxs at a Makefile.global carrying `flags`."""
    mk = tmp_path / "lib" / "pgxs" / "src" / "Makefile.global"
    mk.parent.mkdir(parents=True)
    mk.write_text(f"CFLAGS = -O1 {flags}\n")
    pgxs = tmp_path / "lib" / "pgxs" / "src" / "makefiles" / "pgxs.mk"
    pgxs.parent.mkdir(parents=True)
    pgxs.write_text("")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    pgc = bindir / "pg_config"
    pgc.write_text(f'#!/bin/sh\n[ "$1" = --pgxs ] && echo "{pgxs}"\n')
    pgc.chmod(pgc.stat().st_mode | stat.S_IXUSR)
    return tmp_path


def test_the_subset_is_the_shell_gates_subset(expect):
    """One subset, not two: while run_san.sh exists, it and run_san.py sanitize the
    same suites, read from the shell gate's own --list-suites."""
    shell = subprocess.run(["bash", str(HERE.parent / "run_san.sh"), "--list-suites"],
                           capture_output=True, text=True, env={**os.environ,
                                                                "PGC_SAN_SUITES": ""})
    expect.num(shell.returncode, 0, "premise: run_san.sh answers --list-suites")
    expect.text(" ".join(run_san.SUITES), " ".join(shell.stdout.split()),
                "run_san.py sanitizes the suites run_san.sh does")


def test_every_sanitized_suite_has_a_complete_twin(expect):
    files, refused = run_san.twin_files(run_san.SUITES)
    expect.text(" ".join(refused) or "none", "none",
                "every suite in the subset has a twin graded complete")
    expect.num(len(files), len(run_san.SUITES), "one twin file per suite in the subset")



def test_the_subset_runs_every_twin_that_drives_the_encoding_selftest(expect):
    """Selftest part 130's rule, over the twins: a twin that drives the C-level
    encoding selftest walks the hand-rolled encoders directly, so the sanitizer
    gate must run it. Read from each twin's text, so a new one cannot be left out."""
    drivers = sorted(p.name[len("test_"):-len(".py")] for p in HERE.glob("test_*.py")
                     if p.name != "test_sanitizer_gate.py"
                     and "debug_encoding_selftest" in p.read_text())
    expect.at_least(len(drivers), 1, "premise: at least one twin drives the encoding selftest")
    expect.text(" ".join(d for d in drivers if d not in run_san.SUITES) or "none", "none",
                "the sanitizer subset runs every twin that drives the encoding selftest")

def test_a_suite_without_a_complete_twin_is_refused(tmp_path, expect):
    (tmp_path / "test_a.py").write_text("")
    (tmp_path / "test_b.py").write_text("")
    files, refused = run_san.twin_files(["a", "b", "c"], here=tmp_path, complete={"a", "c"})
    expect.text(" ".join(f.name for f in files), "test_a.py",
                "a suite with a complete twin is run by that twin")
    expect.text(" ".join(refused), "b(twin not graded complete) c(no twin)",
                "a suite whose twin is incomplete, or absent, is refused by name")


def test_a_build_without_the_sanitizer_flags_is_refused(expect):
    full = "CFLAGS = -fsanitize=address,undefined -fno-sanitize=function -O1"
    expect.text(" ".join(run_san.missing_flags(full)) or "none", "none",
                "control: the expected sanitizer build is accepted")
    expect.text(" ".join(run_san.missing_flags("CFLAGS = -fsanitize=address,undefined")),
                "-fno-sanitize=function",
                "a build without -fno-sanitize=function is refused")
    expect.text(" ".join(run_san.missing_flags("CFLAGS = -O2")),
                "-fsanitize=address,undefined -fno-sanitize=function",
                "an ordinary build is refused, both flags named")


def test_every_report_in_the_log_directory_is_found(tmp_path, expect):
    """ASAN and UBSAN write each process's report to `log_path.<pid>`, so a report
    from a backend, a tool, or a cluster a twin started itself is found the same way."""
    expect.num(len(run_san.reports(tmp_path / "absent")), 0,
               "a log directory that was never written holds no report")
    (tmp_path / "san.4242").write_text(
        "==4242==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x1\n#0 ...\n")
    (tmp_path / "san.4243").write_text(
        "columnar_encoding.c:1135:9: runtime error: load of misaligned address\n")
    (tmp_path / "san.4244").write_text("")
    got = run_san.reports(tmp_path)
    expect.num(len(got), 3, "every file the sanitizers wrote is a report, an empty one too")
    expect.text(" | ".join(got),
                "san.4242: ==4242==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x1"
                " | san.4243: columnar_encoding.c:1135:9: runtime error: load of misaligned address"
                " | san.4244: (empty report)",
                "each report is named by its file and its first sanitizer line")


def test_a_suite_fails_on_a_failed_run_or_any_report(expect):
    expect.text(run_san.verdict(0, []), "PASS", "a clean run with no report passes")
    expect.text(run_san.verdict(0, ["san.1: runtime error: x"]), "FAIL",
                "a report fails the suite even when every test passed")
    expect.text(run_san.verdict(1, []), "FAIL", "a failed test fails the suite")
    expect.text(run_san.verdict(67, []), "DECLINED",
                "a run that declined a test (exit 67) is declined, not passed")
    expect.text(run_san.verdict(67, ["san.1: runtime error: x"]), "FAIL",
                "a report fails a declined run too")
    expect.text(run_san.verdict(5, []), "FAIL", "a run that collected nothing fails")
    expect.text(run_san.verdict(124, []), "FAIL", "a run that timed out fails")


def test_the_gate_passes_only_when_every_suite_passed(expect):
    def gate(*verdicts):
        line, rc = run_san.gate([(f"s{i}", v) for i, v in enumerate(verdicts)])
        return f"{rc} {line}"

    expect.text(gate("PASS", "PASS"), "0 SANITIZER GATE PASSED", "every suite passed")
    expect.text(gate("PASS", "FAIL", "DECLINED"), "1 SANITIZER GATE FAILED",
                "any failed suite fails the gate")
    expect.text(gate("PASS", "DECLINED"), "1 SANITIZER GATE INCOMPLETE: 1 of 2 suites declined",
                "any declined suite makes the gate incomplete")
    expect.text(gate(), "1 SANITIZER GATE FAILED: no suite ran",
                "a gate that ran no suite does not pass")


def test_each_suite_runs_with_fatal_sanitizers_logging_to_its_own_directory(tmp_path, expect):
    """main() on a stand-in sanitizer prefix and a stand-in pytest: the environment
    each suite gets, and a report written during a passing suite failing it."""
    prefix = _stub_prefix(tmp_path / "san",
                          "-fsanitize=address,undefined -fno-sanitize=function")
    seen = {}

    def runner(argv, env, timeout):
        twin = pathlib.Path(argv[-1]).name
        seen[twin] = env
        seen.setdefault("timeouts", set()).add(timeout)
        if twin == "test_native_writer.py":
            log = pathlib.Path(env["ASAN_OPTIONS"].split("log_path=")[1].split(":")[0])
            pathlib.Path(f"{log}.777").write_text("runtime error: misaligned\n")
        return 0, ""

    rc = run_san.main([str(prefix)], runner=runner, suites=["smoke", "native_writer"],
                      build=lambda pgc: (True, ""), out=lambda line: None)
    expect.num(rc, 1, "a report during a suite whose tests all passed fails the gate")
    env = seen.get("test_smoke.py", {})
    for var in ("ASAN_OPTIONS", "UBSAN_OPTIONS"):
        expect.text("yes" if "abort_on_error=1" in env.get(var, "") else "no", "yes",
                    f"{var} makes a violation abort the process")
        expect.text("yes" if "log_path=" in env.get(var, "") else "no", "yes",
                    f"{var} writes each report to the suite's log directory")
    expect.text("yes" if "detect_leaks=0" in env.get("ASAN_OPTIONS", "") else "no", "yes",
                "ASAN_OPTIONS leaves out the by-design tool leaks, as run_san.sh does")
    smoke_log = env.get("ASAN_OPTIONS", "").split("log_path=")[-1].split(":")[0]
    writer_log = seen.get("test_native_writer.py", {}).get(
        "ASAN_OPTIONS", "").split("log_path=")[-1].split(":")[0]
    expect.text("differ" if smoke_log and smoke_log != writer_log else "same", "differ",
                "each suite logs to its own directory, so a report is charged to its suite")
    expect.text(" ".join(str(t) for t in sorted(seen.get("timeouts", ()))), "900",
                "each suite is bounded at 900 seconds, as run_san.sh bounds it")
    expect.text(env.get("PGC_ITERS", ""), "200",
                "the fuzz twins run 200 mutants, as run_san.sh's default")

    rc = run_san.main([str(prefix)], runner=lambda a, env, timeout: (0 if timeout else 1, ""),
                      suites=["smoke", "native_writer"],
                      build=lambda pgc: (True, ""), out=lambda line: None)
    expect.num(rc, 0, "control: with no report and every suite passing, the gate passes")


def test_the_command_line_refuses_what_run_san_sh_refuses(tmp_path, expect):
    """Through the real entry point: --list-suites needs no build, and a missing or
    ordinary build is refused before anything runs."""
    def run(*args):
        p = subprocess.run([sys.executable, str(HERE / "run_san.py"), *args],
                           capture_output=True, text=True,
                           env={k: v for k, v in os.environ.items() if k != "PGC_SAN_SUITES"})
        return p.returncode, p.stdout

    rc, out = run("--list-suites")
    expect.num(rc, 0, "--list-suites answers with no sanitizer build")
    expect.text(" ".join(out.split()), " ".join(run_san.SUITES),
                "--list-suites prints the subset, one name per line")
    rc, out = run(str(tmp_path / "nowhere"))
    expect.text(f"{rc} {out.splitlines()[0] if out else ''}",
                f"1 FAIL  no sanitizer build at {tmp_path / 'nowhere'}; run test/build_san.sh first",
                "a missing build is refused")
    plain = _stub_prefix(tmp_path / "plain", "-O2")
    rc, out = run(str(plain))
    expect.text(f"{rc} {out.splitlines()[0] if out else ''}",
                f"1 FAIL  {plain} is not the expected ASAN+UBSAN build",
                "an ordinary build is refused")
