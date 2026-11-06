"""Every job that runs the suites installs the same extension packages (#1382).

The pytest half of selftest part 640, with its own reader: Python, line by line,
where the part uses awk. The check names match.

Suites such as partman_columnar and approx_aggregates need a PGDG extension
package (postgresql-N-partman, -hll, -tdigest), and record a FAIL without it
(#1215). The nightly's coverage job ran those suites without the packages, and
the nightly went red (#1382). So the list is derived, not trusted: every job that
runs the suites must install every extension package any such job installs.

A job runs the suites when a line that is not a comment calls run_all_versions.sh
or run_coverage.sh, or runs `pytest -q` in a job whose key names a cluster. The
suite jobs are also named as a set, so a renamed cluster job cannot drop out
quietly. Needs no cluster and no driver.
"""

import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent
WORKFLOWS = HERE.parent.parent / ".github" / "workflows"
CI = WORKFLOWS / "ci.yml"
NIGHTLY = WORKFLOWS / "nightly.yml"

JOB_KEY = re.compile(r"^  ([A-Za-z_-]+):\s*$")
RUNS = re.compile(r"run_all_versions\.sh|run_coverage\.sh")
PACKAGE = re.compile(r"postgresql-(?:\$\{\{ matrix\.pg \}\}|\d+)-([a-z0-9]+)")


def suite_jobs(path):
    """{job: set of package suffixes} for each job in `path` that runs the suites."""
    jobs, job, in_jobs = {}, None, False
    runs, packages = set(), {}
    for line in path.read_text().splitlines():
        if line.startswith("jobs:"):
            in_jobs = True
            continue
        if not in_jobs:
            continue
        key = JOB_KEY.match(line)
        if key:
            job = key.group(1)
            packages[job] = set()
            continue
        if job is None or line.lstrip().startswith("#"):
            continue
        if RUNS.search(line) or ("/pytest -q" in line and "cluster" in job):
            runs.add(job)
        packages[job].update(PACKAGE.findall(line))
    for name in packages:
        if name in runs:
            jobs[name] = packages[name]
    return jobs


def missing(*paths):
    """Space-joined "file:job:pkg" for each package a suite job lacks, sorted.

    A workflow it could not compare is named as "file:unreadable" or
    "file:no-suite-job", so an empty answer can only mean nothing is missing.
    """
    found, out = {}, []
    for path in paths:
        if not path.is_file():
            out.append(f"{path.name}:unreadable")
            continue
        found[path] = suite_jobs(path)
        if not found[path]:
            out.append(f"{path.name}:no-suite-job")
    every = set().union(*(p for jobs in found.values() for p in jobs.values()))
    for path, jobs in found.items():
        for job, packages in jobs.items():
            out.extend(f"{path.name}:{job}:{p}" for p in every - packages)
    return " ".join(sorted(out))


FIXTURE = """\
jobs:
  a:
    steps:
      - run: apt-get install postgresql-${{ matrix.pg }}-partman postgresql-${{ matrix.pg }}-hll
      - run: bash test/run_all_versions.sh x
  b:
    steps:
      - run: apt-get install postgresql-18 postgresql-server-dev-18 postgresql-18-partman
      - run: bash test/run_coverage.sh x
  c:
    steps:
      # c runs no suite, though this comment names run_all_versions.sh
      - run: apt-get install postgresql-18
"""


def test_both_workflows_are_present(expect):
    expect.text("yes" if CI.is_file() and NIGHTLY.is_file() else "no", "yes",
                "premise: both workflows are present")


def test_the_detector_names_what_it_finds_and_what_it_cannot_read(tmp_path, expect):
    # Proven on workflows made to fail, so the real check cannot pass by finding
    # nothing to compare.
    w = tmp_path / "w.yml"
    w.write_text(FIXTURE)
    expect.text(missing(w), "w.yml:b:hll",
                "the detector names a suite job that lacks a package the others install")

    expect.text(missing(tmp_path / "absent.yml"), "absent.yml:unreadable",
                "the detector names a workflow it cannot read")

    nosuite = tmp_path / "nosuite.yml"
    nosuite.write_text("jobs:\n  c:\n" + FIXTURE.split("  c:\n", 1)[1])
    expect.text(missing(nosuite), "nosuite.yml:no-suite-job",
                "the detector names a workflow in which it finds no suite job")


def test_the_suite_jobs_are_the_five_known(expect):
    # Not a premise: renaming, adding or deleting a job reddens it (#1236).
    got = " ".join(sorted(f"{p.name}:{job}" for p in (CI, NIGHTLY) for job in suite_jobs(p)))
    expect.text(got, "ci.yml:pytest-cluster ci.yml:suites nightly.yml:coverage "
                     "nightly.yml:pytest-cluster nightly.yml:suites",
                "the suite-running jobs are exactly the five this part knows")


def test_every_suite_job_installs_every_extension_package(expect):
    expect.text(missing(CI, NIGHTLY) or "none", "none",
                "every job that runs the suites installs every extension package (#1382)")
