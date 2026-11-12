"""run_coverage.py, the coverage report over the pytest corpus, keeps run_coverage.sh's
refusals (#2038).

The nightly's coverage job drove every SHELL suite against a `--coverage` build, so
it was one of the three gates that kept the shell suites from being retired.
run_coverage.py drives the pytest cluster files instead. It is still a report, not
a gate: no threshold, and nothing fails on the number.

What it must not do is report coverage it did not measure. Selftest part 250 holds
run_coverage.sh to that by the ORDER of its lines (#740). These tests hold
run_coverage.py to it by BEHAVIOUR, on a stand-in tree with stand-in make, pytest,
lcov and genhtml, so they need no cluster, no driver and no lcov:

- the build and the corpus are instrumented (COPT=--coverage) and the counters are
  redirected (GCOV_PREFIX) before anything runs;
- counters written under GCOV_PREFIX are returned beside their objects, never to a
  destination outside the tree, before the counters are counted;
- zero counters are refused before lcov is called, from the directory lcov
  captures, and the refusal looks in GCOV_PREFIX before walking the filesystem;
- a corpus that ran nothing measures nothing, and a failing corpus still writes
  its report but does not exit 0;
- the table comes from pgc_coverage_table.py, never `lcov --list` (#974);
- the corpus files resolve from wherever the runner is called, and pytest runs in
  this directory so its conftest (and `--pg-config`) is loaded;
- the tree and the install are left uninstrumented however the run ends, so the
  next ordinary run on a shared prefix does not test the --coverage library.
"""

import os
import pathlib
import re
import subprocess
import sys

import run_coverage

HERE = pathlib.Path(__file__).resolve().parent


class Stand:
    """Stand-in tools: records every command with the environment it got, and lets a
    test decide what pytest exits with and which counters it writes."""

    def __init__(self, srcdir, pytest_rc=0, write=(), found=""):
        self.srcdir, self.pytest_rc, self.write, self.found = srcdir, pytest_rc, write, found
        self.calls, self.cwds, self.prefix_mode = [], [], None

    def __call__(self, argv, env=None, cwd=None):
        self.calls.append((list(argv), dict(env or {})))
        self.cwds.append(cwd)
        if "pytest" in argv:
            prefix = pathlib.Path(env["GCOV_PREFIX"])
            self.prefix_mode = prefix.stat().st_mode & 0o7777
            for rel in self.write:
                f = pathlib.Path(str(prefix) + str(rel))
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text("counters")
            # pgc_vacuity prints its unrunnable notes AFTER pytest's summary line.
            return self.pytest_rc, ("..F.\n"
                                    "FAILED test_x.py::test_z - AssertionError: got 67 want 0\n"
                                    "3 failed, 5349 passed in 1830.16s\n"
                                    "UNRUN  test_x.py::test_y: UNMET_PRECONDITION: no endpoint\n"
                                    "  -- edit test/pytest/expected_unrunnable.txt, with the reason.\n")
        if argv[:1] == ["find"]:
            return 0, self.found
        if argv[:1] == ["lcov"] and "--summary" in argv:
            return 0, "  lines......: 50.0% (1 of 2 lines)\n"
        return 0, ""

    def named(self, word):
        return [a for a, _ in self.calls if word in a or (a and a[0] == word)]


def _tree(tmp_path):
    src = tmp_path / "tree" / "src"
    src.mkdir(parents=True)
    (src / "columnar_reader.gcno").write_text("notes")
    (src / "columnar_writer.gcno").write_text("notes")
    return tmp_path / "tree"


def _main(tmp_path, stand, srcdir):
    lines = []
    rc = run_coverage.main(["/x/pg_config"], run=stand, srcdir=srcdir,
                           files=["test_a.py", "test_b.py"],
                           out_dir=tmp_path / "out", gcov_prefix=tmp_path / "gcov",
                           have=lambda tool: True, out=lines.append)
    return rc, lines


def test_the_build_and_the_corpus_are_instrumented_and_redirected(tmp_path, expect):
    srcdir = _tree(tmp_path)
    stand = Stand(srcdir, write=[srcdir / "src" / "columnar_reader.gcda"])
    rc, lines = _main(tmp_path, stand, srcdir)
    expect.num(rc, 0, "control: a passing corpus that wrote counters reports and exits 0")
    corpus_at = next(i for i, (a, _) in enumerate(stand.calls) if "pytest" in a)
    makes = [(a, e) for a, e in stand.calls[:corpus_at] if a[:1] == ["make"] and "clean" not in a]
    expect.at_least(len(makes), 2, "premise: the build and the install were both run")
    expect.text(" ".join(sorted({"yes" if "COPT=--coverage" in a else "no" for a, _ in makes})),
                "yes", "every build and install before the corpus is instrumented with "
                "COPT=--coverage")
    py = [(a, e) for a, e in stand.calls if "pytest" in a]
    expect.num(len(py), 1, "the corpus runs once")
    env = py[0][1]
    expect.text(env.get("GCOV_PREFIX", ""), str(tmp_path / "gcov"),
                "GCOV_PREFIX is set for the corpus, so counters land where they are collected")
    expect.text(env.get("GCOV_PREFIX_STRIP", ""), "0", "and keeps each counter's full path")
    expect.text(env.get("COPT", ""), "--coverage",
                "the corpus's own build keeps the instrumentation if it rebuilds")
    expect.text(" ".join(pathlib.Path(a).name for a in py[0][0][-2:]), "test_a.py test_b.py",
                "the corpus is the files given")
    expect.text(oct(stand.prefix_mode or 0), oct(0o1777),
                "GCOV_PREFIX is writable by the server's user, and sticky, before the corpus runs")
    expect.text(next((ln for ln in lines if ln.startswith("-- corpus:")), ""),
                "-- corpus: rc=0, 3 failed, 5349 passed in 1830.16s",
                "the corpus line carries pytest's summary, not the note printed after it")
    first_make = next(i for i, (a, _) in enumerate(stand.calls) if a[:1] == ["make"])
    expect.text("clean" if "clean" in stand.calls[first_make][0] else "not clean", "clean",
                "the tree is cleaned first, so no uninstrumented object is reused")


def test_the_corpus_resolves_from_the_callers_directory(tmp_path, monkeypatch, expect):
    """The nightly calls the runner from the repository root. From there bare file
    names resolve to nothing, and pytest, loading no conftest, refuses --pg-config."""
    srcdir = _tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    names = run_coverage.corpus()[:2]
    expect.num(len(names), 2, "premise: two real corpus files to hand the runner")
    stand = Stand(srcdir, write=[srcdir / "src" / "columnar_reader.gcda"])
    lines = []
    run_coverage.main(["/x/pg_config"], run=stand, srcdir=srcdir, files=names,
                      out_dir=tmp_path / "out", gcov_prefix=tmp_path / "gcov",
                      have=lambda tool: True, out=lines.append)
    i = next(i for i, (a, _) in enumerate(stand.calls) if "pytest" in a)
    argv, cwd = stand.calls[i][0], pathlib.Path(stand.cwds[i] or os.getcwd())
    expect.text(" ".join(f for f in argv[-2:] if not (cwd / f).is_file()) or "none", "none",
                "each corpus file resolves from the directory pytest runs in")
    expect.text("yes" if (cwd / "conftest.py").is_file() else "no", "yes",
                "pytest runs where the corpus's conftest is, so --pg-config is known")


def _ends_uninstrumented(stand):
    """The make calls after the corpus last touched the tree: what they leave."""
    makes = [(a, e) for a, e in stand.calls if a[:1] == ["make"]]
    tail = makes[-2:]
    if len(tail) < 2 or "clean" not in tail[0][0] or "install" not in tail[1][0]:
        return "not restored: " + " | ".join(" ".join(a[1:]) for a, _ in makes[-2:])
    a, e = tail[1]
    if any("COPT" in x for x in a) or "COPT" in e or "GCOV_PREFIX" in e:
        return "restored with --coverage still set"
    return "uninstrumented"


def test_the_tree_and_the_install_are_left_uninstrumented(tmp_path, expect):
    """A later ordinary run on the shared prefix must not find the --coverage
    library installed and its objects current. The restore runs however the run
    ends: reported, refused for zero counters, or failed at the build."""
    srcdir = _tree(tmp_path)
    stand = Stand(srcdir, write=[srcdir / "src" / "columnar_reader.gcda"])
    rc, _ = _main(tmp_path, stand, srcdir)
    expect.num(rc, 0, "premise: a reported run")
    expect.text(_ends_uninstrumented(stand), "uninstrumented",
                "a reported run ends by cleaning and installing an uninstrumented build")
    caps = [i for i, (a, _) in enumerate(stand.calls) if "--capture" in a]
    cleans = [i for i, (a, _) in enumerate(stand.calls) if a[:1] == ["make"] and "clean" in a]
    expect.num(len(caps), 1, "premise: lcov captured once")
    expect.at_least(cleans[-1], caps[0] + 1,
                    "and cleans only after lcov has read the counters (call index)")
    srcdir = _tree(tmp_path / "refused")
    stand = Stand(srcdir)
    rc, _ = _main(tmp_path / "refused", stand, srcdir)
    expect.num(rc, 1, "premise: a run refused for zero counters")
    expect.text(_ends_uninstrumented(stand), "uninstrumented", "a refused run is restored too")

    class FailsInstall(Stand):
        def __call__(self, argv, env=None, cwd=None):
            rc, log = super().__call__(argv, env, cwd)
            if "install" in argv and "COPT=--coverage" in argv:
                return 2, "install failed"
            return rc, log
    srcdir = _tree(tmp_path / "failed")
    stand = FailsInstall(srcdir)
    rc, _ = _main(tmp_path / "failed", stand, srcdir)
    expect.num(rc, 1, "premise: a run whose instrumented install failed")
    expect.text(_ends_uninstrumented(stand), "uninstrumented",
                "a run that failed after the instrumented build is restored too")

    class FailsRestore(Stand):
        def __call__(self, argv, env=None, cwd=None):
            rc, log = super().__call__(argv, env, cwd)
            if "install" in argv and "COPT=--coverage" not in argv:
                return 2, "install failed"
            return rc, log
    srcdir = _tree(tmp_path / "unrestored")
    stand = FailsRestore(srcdir, write=[srcdir / "src" / "columnar_reader.gcda"])
    rc, lines = _main(tmp_path / "unrestored", stand, srcdir)
    expect.num(len(stand.named("genhtml")), 1, "premise: the run reported before restoring")
    expect.num(rc, 1, "a reported run whose uninstrumented reinstall failed exits 1, since "
                      "the --coverage library may still be installed")
    expect.text(next((ln for ln in lines if ln.startswith("FAIL")), ""),
                "FAIL  the uninstrumented reinstall failed: the --coverage library may "
                "still be installed", "and says why")


def test_a_counters_directory_that_cannot_be_emptied_is_refused(tmp_path, monkeypatch, expect):
    """A GCOV_PREFIX left by another user cannot be emptied. Reusing it would mix an
    earlier run's counters into this report, so the run refuses, naming it, before
    the corpus runs; and the tree is still restored."""
    srcdir = _tree(tmp_path)
    left = tmp_path / "gcov" / "tmp" / "old" / "src" / "columnar_reader.gcda"
    left.parent.mkdir(parents=True)
    left.write_text("an earlier run's counters")
    # As rmtree(ignore_errors=True) behaves on a directory another user owns.
    monkeypatch.setattr(run_coverage.shutil, "rmtree", lambda *a, **k: None)
    stand = Stand(srcdir, write=[srcdir / "src" / "columnar_reader.gcda"])
    rc, lines = _main(tmp_path, stand, srcdir)
    expect.num(rc, 1, "a counters directory that could not be emptied fails the run")
    expect.num(len(stand.named("pytest")), 0, "the corpus does not run into it")
    expect.text(next((ln for ln in lines if ln.startswith("FAIL")), ""),
                f"FAIL  cannot empty the counters directory {tmp_path / 'gcov'}: remove it, "
                "or set PGC_COV_GCOV_PREFIX", "the refusal names the directory")
    expect.text(_ends_uninstrumented(stand), "uninstrumented", "and the tree is restored")


def test_counters_come_back_beside_their_objects_and_never_outside_the_tree(tmp_path, expect):
    srcdir = _tree(tmp_path)
    outside = tmp_path / "elsewhere" / "x.gcda"
    outside.parent.mkdir()
    outside.write_text("")
    stand = Stand(srcdir, write=[srcdir / "src" / "columnar_reader.gcda", outside,
                                 srcdir / "src" / "gone" / "y.gcda"])
    rc, lines = _main(tmp_path, stand, srcdir)
    expect.text("yes" if (srcdir / "src" / "columnar_reader.gcda").is_file() else "no", "yes",
                "a counter under GCOV_PREFIX is returned beside its object")
    expect.text(outside.read_text() or "untouched", "untouched",
                "a counter whose destination is outside the tree is not copied there")
    expect.text("yes" if not (srcdir / "src" / "gone").exists() else "no", "yes",
                "a counter whose object directory does not exist is not created")
    expect.text(next((ln for ln in lines if "returned beside" in ln), ""),
                "-- counters returned beside their objects: 1",
                "the count of returned counters is reported")


def test_zero_counters_are_refused_before_lcov_runs(tmp_path, expect):
    srcdir = _tree(tmp_path)
    stray = tmp_path / "elsewhere" / "src" / "columnar_reader.gcda"
    stand = Stand(srcdir, write=[stray])
    stray.parent.mkdir(parents=True)
    stray.write_text("")
    rc, lines = _main(tmp_path, stand, srcdir)
    expect.num(rc, 1, "no counter in the tree is refused")
    expect.num(len(stand.named("--capture")), 0, "lcov --capture is never called after a refusal")
    expect.num(len(stand.named("genhtml")), 0, "nor is genhtml")
    text = "\n".join(lines)
    expect.text("yes" if "no .gcda counters were written" in text else "no", "yes",
                "the refusal says no counter was written")
    expect.text("yes" if "path mismatch" in text and str(tmp_path / "gcov") in text else "no",
                "yes", "it names counters it finds under GCOV_PREFIX as a path mismatch, "
                "before any walk of the filesystem")

    srcdir = _tree(tmp_path / "walk")
    ours = f"/var/lib/elsewhere{srcdir / 'src'}/columnar_reader.gcda"
    stand = Stand(srcdir, found=f"/root/pgc740/src/columnar_reader.gcda\n{ours}\n")
    rc, lines = _main(tmp_path / "walk", stand, srcdir)
    text = "\n".join(lines)
    expect.num(len(stand.named("find")), 1, "premise: with nothing under GCOV_PREFIX, it walks")
    expect.text("named" if ours in text else "missed", "named",
                "a counter for this tree's src/ found by the walk is named as a path mismatch")
    expect.text("named" if "/root/pgc740/" in text else "not named", "not named",
                "another tree's leftover counter is not, since it is not this run's mismatch")


def test_the_refusal_counts_the_directory_lcov_captures(tmp_path, expect):
    srcdir = _tree(tmp_path)
    (srcdir / "other").mkdir()
    (srcdir / "other" / "z.gcda").write_text("not under src")
    stand = Stand(srcdir)
    rc, _ = _main(tmp_path, stand, srcdir)
    expect.num(rc, 1, "a counter outside src/ does not satisfy the guard, because lcov "
                      "captures src/ only")
    stand = Stand(srcdir, write=[srcdir / "src" / "columnar_reader.gcda"])
    _main(tmp_path, stand, srcdir)
    cap = stand.named("--capture")
    expect.num(len(cap), 1, "premise: with a counter in src/, lcov captures")
    expect.text(cap[0][cap[0].index("--directory") + 1], str(srcdir / "src"),
                "lcov captures the directory the guard counted")


def test_a_corpus_that_ran_nothing_or_failed_is_not_a_clean_report(tmp_path, expect):
    srcdir = _tree(tmp_path)
    # A counter is written anyway (the build's own probes could), so the zero-counter
    # refusal does not answer for this rule.
    stand = Stand(srcdir, pytest_rc=5, write=[srcdir / "src" / "columnar_reader.gcda"])
    rc, lines = _main(tmp_path, stand, srcdir)
    expect.num(rc, 1, "a corpus that collected nothing exits nonzero, counters or not")
    expect.text("yes" if any("NO TEST RAN" in ln for ln in lines) else "no", "yes",
                "and says it measured no coverage")
    stand = Stand(srcdir, pytest_rc=1, write=[srcdir / "src" / "columnar_reader.gcda"])
    rc, lines = _main(tmp_path, stand, srcdir)
    expect.num(rc, 1, "a failing corpus exits nonzero")
    expect.num(len(stand.named("genhtml")), 1, "but still writes its report")
    expect.text(" | ".join(ln.strip() for ln in lines if "FAILED " in ln),
                "FAILED test_x.py::test_z - AssertionError: got 67 want 0",
                "and names each failed test in its own output, so a red nightly says "
                "which test without its log artifact being fetched (#2054)")
    stand = Stand(srcdir, pytest_rc=67, write=[srcdir / "src" / "columnar_reader.gcda"])
    rc, _ = _main(tmp_path, stand, srcdir)
    expect.num(rc, 0, "a corpus that declined a test is not a failure here, as run_coverage.sh "
                      "did not fail a skipped suite: this is a report, not a gate")


def test_the_table_is_computed_not_parsed_from_lcov_list(tmp_path, expect):
    srcdir = _tree(tmp_path)
    stand = Stand(srcdir, write=[srcdir / "src" / "columnar_reader.gcda"])
    _main(tmp_path, stand, srcdir)
    expect.num(len(stand.named("--list")), 0, "no lcov --list call (#974)")
    expect.num(len(stand.named("--summary")), 1, "one lcov --summary call (#974)")
    table = [a for a, _ in stand.calls if any(str(x).endswith("pgc_coverage_table.py") for x in a)]
    expect.num(len(table), 1, "the table comes from pgc_coverage_table.py (#974)")
    expect.text(" ".join(table[0][-3:]), f"{tmp_path / 'out' / 'coverage.info'} --limit 20",
                "over the captured coverage.info, the 20 least covered files")


def test_the_corpus_is_the_cluster_files_less_the_prefix_swappers(expect):
    from test_harness_deps import NO_CLUSTER

    files = run_coverage.corpus()
    every = sorted(p.name for p in HERE.glob("test_*.py") if p.name not in NO_CLUSTER)
    expect.text(" ".join(f for f in files if f in NO_CLUSTER) or "none", "none",
                "no guard file is in the corpus: it executes no extension code")
    expect.text(" ".join(sorted(set(every) - set(files))),
                " ".join(sorted(f for f in run_coverage.SWAPS_THE_PREFIX if (HERE / f).is_file())),
                "every cluster file is in the corpus but the ones that swap another build "
                "into the prefix, as run_coverage.sh left out their shell twins")
    expect.num(len(run_coverage.SWAPS_THE_PREFIX), 3,
               "premise: the three prefix-swapping files are each named, so the loop below runs")
    for f, why in run_coverage.SWAPS_THE_PREFIX.items():
        expect.at_least(len(why), 20, f"{f}: the reason it is left out is stated")


# Built, not written: the phrase a test records when the checkout has no release tags.
TAGLESS = "no release tags" + " in this checkout"
TAG_FETCH = re.compile(r"git fetch\b.*refs/tags/v1\.0-alpha")


def _job_lines(text, runner):
    """{job: its non-comment lines} for each job in a workflow that runs `runner`."""
    jobs, job, in_jobs = {}, None, False
    for line in text.splitlines():
        if line.startswith("jobs:"):
            in_jobs = True
            continue
        m = re.match(r"^  ([A-Za-z_-]+):\s*$", line) if in_jobs else None
        if m:
            job = m.group(1)
            jobs[job] = []
        elif job and not line.lstrip().startswith("#"):
            jobs[job].append(line)
    return {j: ls for j, ls in jobs.items() if any(runner in ln for ln in ls)}


def test_the_coverage_job_fetches_the_release_tags_its_corpus_reads(expect):
    """actions/checkout fetches no tags. The coverage job ran from a plain checkout, so
    the arms that compare against a release tag recorded UNMET_PRECONDITION and the
    unrunnable accounting failed the nightly every night (#2054). Derived: the corpus
    holds a file that reads the tags, so every job that runs it fetches them first."""
    readers = [f for f in run_coverage.corpus() if TAGLESS in (HERE / f).read_text()]
    expect.at_least(len(readers), 1, "premise: a corpus file reads the release tags")
    jobs = {}
    for wf in sorted((HERE.parent.parent / ".github" / "workflows").glob("*.yml")):
        for job, lines in _job_lines(wf.read_text(), "run_coverage.py").items():
            jobs[f"{wf.name}:{job}"] = lines
    expect.at_least(len(jobs), 1, "premise: a workflow job runs run_coverage.py")
    for name, lines in sorted(jobs.items()):
        # A job with no fetch counts it at its end, so the gap is never positive.
        fetch = next((i for i, ln in enumerate(lines) if TAG_FETCH.search(ln)), len(lines))
        run = next(i for i, ln in enumerate(lines) if "run_coverage.py" in ln)
        expect.at_least(run - fetch, 1,
                        f"{name} fetches the v1.0-alpha tags, lines before it runs run_coverage.py")


def test_the_command_line_refuses_a_missing_tool_or_pg_config(tmp_path, expect):
    empty = tmp_path / "bin"
    empty.mkdir()

    def run(path, *args):
        p = subprocess.run([sys.executable, str(HERE / "run_coverage.py"), *args],
                           capture_output=True, text=True, env=dict(os.environ, PATH=str(path)))
        return p.returncode, (p.stdout + p.stderr).strip().splitlines()

    rc, out = run(empty, "/x/pg_config")
    expect.text(f"{rc} {out[0] if out else ''}", "1 FAIL  lcov not found", "no lcov is refused")
    t = empty / "lcov"
    t.write_text("#!/bin/sh\n")
    t.chmod(0o755)
    rc, out = run(empty, "/x/pg_config")
    expect.text(f"{rc} {out[0] if out else ''}", "1 FAIL  genhtml not found",
                "no genhtml is refused, with lcov present")
    for tool in ("lcov", "genhtml"):
        t = empty / tool
        t.write_text("#!/bin/sh\n")
        t.chmod(0o755)
    rc, out = run(empty, str(tmp_path / "nowhere" / "pg_config"))
    expect.text(f"{rc} {out[0] if out else ''}",
                f"1 FAIL  no pg_config at {tmp_path / 'nowhere' / 'pg_config'}",
                "a missing pg_config is refused")
