#!/usr/bin/env python3
"""Line and branch coverage over the pytest corpus, for one major (#2038).

A discovery tool, not a gate. There is no threshold and nothing in CI fails on the
number, deliberately: a threshold creates pressure to write tests that execute
lines rather than tests that prove properties. What the report answers is the
question a passing corpus cannot: which code does nothing execute at all. Read it
for holes, not for the percentage.

run_coverage.sh drove every SHELL suite. This drives the pytest cluster files, the
complement of test_harness_deps.NO_CLUSTER, less the files that swap another build
into the prefix (SWAPS_THE_PREFIX), as the shell runner left out their shell twins.

It builds once in the working tree with COPT=--coverage (COPT reaches CFLAGS and
LDFLAGS through PostgreSQL's Makefile.global), installs that build, and runs the
corpus against it. The corpus's own build step finds the objects current and keeps
them, and COPT stays in its environment in case it rebuilds.

IT MUST NOT REPORT COVERAGE IT DID NOT MEASURE (#740). For a year the shell runner
published a report of nothing: gcov writes each .gcda beside its object, as the
process that ran the code, and the server could not write next to the build. So:

- counters are redirected with GCOV_PREFIX (full paths, GCOV_PREFIX_STRIP=0) before
  anything runs, and returned beside their objects afterwards -- never to a
  destination outside the tree, and never into a directory that does not exist;
- zero counters in src/, the directory lcov captures, are refused before lcov is
  called. The refusal looks in GCOV_PREFIX first, where a stray counter means a
  path mismatch, and only then walks the filesystem;
- a corpus that collected nothing measured nothing and exits nonzero. A corpus with
  a failing test still writes its report, and exits nonzero. One that declined a
  test (exit 67) is not a failure here: this is a report, as run_coverage.sh did
  not fail a skipped suite.

The per-file table comes from pgc_coverage_table.py, never `lcov --list`, whose
format differs between lcov versions (#974).

Usage:  test/pytest/run_coverage.py [PG_CONFIG]
  PGC_COV_FILES=<list>    override the corpus (test file names)
  PGC_COV_OUT=<dir>       output directory (default coverage/)
"""

import os
import pathlib
import re
import shutil
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
SRCDIR = HERE.parent.parent

SWAPS_THE_PREFIX = {
    "test_pg_upgrade.py": "runs pg_upgrade between two installs of its own, so the counters "
                          "it would add come from builds this report did not instrument",
    "test_extension_upgrade.py": "builds and installs v1.0-alpha into the shared prefix, "
                                 "uninstrumented, before putting this tree's install back",
    "test_format_downgrade.py": "builds and installs v1.0-alpha4 into the shared prefix, "
                                "uninstrumented, and runs it against this tree's files",
}

EXIT_NO_TESTS = 5        # pytest.ExitCode.NO_TESTS_COLLECTED
EXIT_INCOMPLETE = 67     # pgc_vacuity.EXIT_INCOMPLETE: a test declined.


def corpus(here=HERE):
    """The cluster files, sorted, less SWAPS_THE_PREFIX."""
    from test_harness_deps import NO_CLUSTER
    return sorted(p.name for p in here.glob("test_*.py")
                  if p.name not in NO_CLUSTER and p.name not in SWAPS_THE_PREFIX)


def return_counters(prefix, srcdir):
    """Copy each .gcda under `prefix` back to the path it names, inside `srcdir` only,
    and only into a directory that exists. -> how many were returned."""
    prefix, srcdir = pathlib.Path(prefix), pathlib.Path(srcdir).resolve()
    moved = 0
    if not prefix.is_dir():
        return 0
    for f in prefix.rglob("*.gcda"):
        dest = pathlib.Path("/" + str(f.relative_to(prefix)).lstrip("/"))
        if srcdir not in dest.parents or not dest.parent.is_dir():
            continue
        shutil.copy2(f, dest)
        moved += 1
    return moved


def _run(argv, env=None):
    p = subprocess.run(argv, env=env, capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def _say(line):
    print(line, flush=True)


def main(argv, run=_run, srcdir=SRCDIR, files=None, out_dir=None, gcov_prefix=None,
         have=shutil.which, out=_say):
    for tool in ("lcov", "genhtml"):
        if not have(tool):
            out(f"FAIL  {tool} not found")
            return 1
    pgc = argv[0] if argv else "/usr/local/pg17/bin/pg_config"
    if run is _run and not os.access(pgc, os.X_OK):
        out(f"FAIL  no pg_config at {pgc}")
        return 1
    given = run
    run = lambda argv, env=None: given([str(a) for a in argv], env)  # noqa: E731
    srcdir = pathlib.Path(srcdir)
    src = srcdir / "src"
    out_dir = pathlib.Path(out_dir or os.environ.get("PGC_COV_OUT") or srcdir / "coverage")
    gcov_prefix = pathlib.Path(gcov_prefix or os.environ.get("PGC_COV_GCOV_PREFIX")
                               or "/tmp/pgc-gcov")
    files = files or os.environ.get("PGC_COV_FILES", "").split() or corpus()
    env = dict(os.environ, COPT="--coverage")

    out(f"-- build with --coverage against {pgc}")
    run(["make", "-C", srcdir, "clean"], env)
    rc, log = run(["make", "-C", srcdir, f"PG_CONFIG={pgc}", "COPT=--coverage"], env)
    if rc != 0:
        out("FAIL  instrumented build failed")
        out(log[-3000:])
        return 1
    rc, log = run(["make", "-C", srcdir, f"PG_CONFIG={pgc}", "COPT=--coverage", "install"], env)
    if rc != 0:
        out("FAIL  install failed")
        out(log[-3000:])
        return 1

    # Redirected BEFORE anything runs (#740).
    shutil.rmtree(gcov_prefix, ignore_errors=True)
    gcov_prefix.mkdir(parents=True)
    os.chmod(gcov_prefix, 0o1777)
    env = dict(env, GCOV_PREFIX=str(gcov_prefix), GCOV_PREFIX_STRIP="0")
    out(f"-- counters redirected to {gcov_prefix}")
    out_dir.mkdir(parents=True, exist_ok=True)
    run(["lcov", "--directory", src, "--zerocounters"], env)

    out(f"-- running {len(files)} cluster file(s)")
    rc, log = run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                   "--pg-config", pgc, *files], env)
    (out_dir / "pytest.log").write_text(log)
    # pytest's summary line, not the last line: pgc_vacuity prints notes after it.
    summary = [ln.strip() for ln in log.splitlines()
               if re.search(r"\b\d+ (passed|failed|error|errors|skipped|deselected)\b"
                            r"|no tests ran", ln)][-1:] or ["(no summary line)"]
    out(f"-- corpus: rc={rc}, {summary[0]}")
    failed = rc not in (0, EXIT_INCOMPLETE)
    if rc == EXIT_NO_TESTS:
        out("-- NO TEST RAN, so this measures no coverage")

    out(f"-- counters returned beside their objects: {return_counters(gcov_prefix, srcdir)}")
    gcda = sorted(src.rglob("*.gcda"))
    gcno = sorted(src.rglob("*.gcno"))
    out(f"-- counters: {len(gcda)} .gcda from {len(gcno)} instrumented objects in src/")

    # Refused BEFORE lcov runs, from the directory lcov captures (#740).
    if not gcda:
        out("FAIL  no .gcda counters were written, so this measures no coverage")
        out("      gcov writes them beside the objects, as the user that ran the code.")
        stray = sorted(gcov_prefix.rglob("*.gcda"))[:5]
        if stray:
            out("      counters DO exist under GCOV_PREFIX, so this is a path mismatch:")
        else:
            rc_find, found = run(["find", "/", "-xdev", "-name", "*.gcda"], env)
            stray = found.split()[:5]
            if stray:
                out("      counters DO exist elsewhere, so this is a path mismatch:")
            else:
                out("      no .gcda anywhere on this filesystem, so nothing wrote them")
        for s in stray:
            out(f"        {s}")
        return 1

    info = out_dir / "coverage.info"
    out("-- collect")
    rc_cap, log = run(["lcov", "--directory", src, "--capture", "--output-file", info,
                       "--rc", "branch_coverage=1", "--ignore-errors", "inconsistent"], env)
    if rc_cap != 0:
        out("FAIL  lcov capture produced nothing")
        out(log[-2000:])
        return 1
    run(["lcov", "--extract", info, f"{src}/*", "--output-file", info,
         "--rc", "branch_coverage=1", "--ignore-errors", "inconsistent"], env)
    rc_html, log = run(["genhtml", info, "--output-directory", out_dir / "html",
                        "--branch-coverage", "--legend", "--ignore-errors", "inconsistent"], env)
    if rc_html != 0:
        out("FAIL  genhtml failed")
        out(log[-2000:])
        return 1
    _, summary = run(["lcov", "--summary", info, "--rc", "branch_coverage=1",
                      "--ignore-errors", "inconsistent"], env)
    out("")
    for ln in summary.splitlines():
        if not ln.startswith(("Reading", "lcov: WARNING", "lcov: Note")):
            out(f"  {ln}")
    out("")
    out("-- per file, least covered first")
    _, table = run([sys.executable, srcdir / "test" / "pgc_coverage_table.py", info,
                    "--limit", "20"], env)
    for ln in table.splitlines():
        out(ln)
    out("")
    out(f"report: {out_dir / 'html' / 'index.html'}")
    return 1 if failed or rc == EXIT_NO_TESTS else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
