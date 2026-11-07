"""The pytest cluster job runs on every (major, architecture) the suites job runs on (#2038).

The shell suites cannot be retired while a leg of the matrix runs them and nothing
else. The nightly's `suites` job runs PG 18 on aarch64 as well as x86_64, because a
missing barrier in concurrent code is invisible under x86_64's stronger store
ordering and a defect on aarch64. Its `pytest-cluster` job ran x86_64 only, so on
aarch64 only the shell suites executed.

So the property is derived, not listed: in each workflow, every (major,
architecture) the `suites` job runs, `pytest-cluster` runs too. A matrix is read
from its `include:` entries, or else from its `pg:` and `runner:` lists, with a
literal `runs-on:` as the runner when the matrix names none. A runner label with
`arm` in it is aarch64; any other is x86_64. Needs no cluster and no driver.
"""

import itertools
import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent
WORKFLOWS = HERE.parent.parent / ".github" / "workflows"

JOB_KEY = re.compile(r"^  ([A-Za-z_-]+):\s*$")
RUNS_ON = re.compile(r"^\s+runs-on:\s*([A-Za-z0-9._-]+)\s*$")
LIST = re.compile(r"^\s+(pg|runner):\s*\[([^\]]*)\]")
INCLUDE = re.compile(r"^\s+-\s*\{([^}]*)\}")
FIELD = re.compile(r"(pg|runner):\s*'?([A-Za-z0-9._-]+)'?")


def _arch(runner):
    return "aarch64" if "arm" in runner else "x86_64"


def legs(text):
    """{job: set of (major, arch)} for every job in a workflow's text."""
    jobs, job, in_jobs = {}, None, False
    for line in text.splitlines():
        if line.startswith("jobs:"):
            in_jobs = True
            continue
        if not in_jobs:
            continue
        key = JOB_KEY.match(line)
        if key:
            job = key.group(1)
            jobs[job] = {"runs_on": None, "pg": [], "runner": [], "include": []}
            continue
        if job is None or line.lstrip().startswith("#"):
            continue
        j = jobs[job]
        m = RUNS_ON.match(line)
        if m:
            j["runs_on"] = m.group(1)
            continue
        m = LIST.match(line)
        if m:
            j[m.group(1)] = [v.strip().strip("'\"") for v in m.group(2).split(",") if v.strip()]
            continue
        m = INCLUDE.match(line)
        if m:
            entry = dict(FIELD.findall(m.group(1)))
            if "pg" in entry:
                j["include"].append(entry)
    out = {}
    for name, j in jobs.items():
        if j["include"]:
            pairs = {(e["pg"], e.get("runner", j["runs_on"] or "?")) for e in j["include"]}
        else:
            runners = j["runner"] or ([j["runs_on"]] if j["runs_on"] else [])
            pairs = set(itertools.product(j["pg"], runners))
        out[name] = {(pg, _arch(r)) for pg, r in pairs}
    return out


def uncovered(*paths):
    """Space-joined "file:pg/arch" for each suites leg pytest-cluster lacks, sorted.

    A workflow it cannot compare is named, "file:unreadable", "file:no-suites-legs" or
    "file:no-pytest-cluster", so an empty answer can only mean every leg is covered.
    """
    out = []
    for path in paths:
        if not path.is_file():
            out.append(f"{path.name}:unreadable")
            continue
        jobs = legs(path.read_text())
        if not jobs.get("suites"):
            out.append(f"{path.name}:no-suites-legs")
            continue
        if "pytest-cluster" not in jobs:
            out.append(f"{path.name}:no-pytest-cluster")
            continue
        out.extend(f"{path.name}:{pg}/{arch}"
                   for pg, arch in jobs["suites"] - jobs["pytest-cluster"])
    return " ".join(sorted(out))


FIXTURE = """\
jobs:
  suites:
    runs-on: ${{ matrix.runner }}
    strategy:
      matrix:
        include:
          - { pg: '17', runner: ubuntu-latest }
          - { pg: '18', runner: ubuntu-latest }
          - { pg: '18', runner: ubuntu-24.04-arm }
  pytest-cluster:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        pg: ['17', '18']
"""


def test_the_reader_names_a_leg_pytest_lacks_and_what_it_cannot_read(tmp_path, expect):
    """The reader, on fixtures: each matrix form read, each gap named, each blind spot
    named rather than read as covered."""
    got = legs(FIXTURE)
    expect.text(" ".join(sorted(f"{p}/{a}" for p, a in got["suites"])),
                "17/x86_64 18/aarch64 18/x86_64",
                "an include matrix is read as its (major, architecture) legs")
    expect.text(" ".join(sorted(f"{p}/{a}" for p, a in got["pytest-cluster"])),
                "17/x86_64 18/x86_64",
                "a pg list under a literal runs-on is read as that runner's legs")
    lists = legs("jobs:\n  b:\n    runs-on: ${{ matrix.runner }}\n    strategy:\n"
                 "      matrix:\n        pg: ['15']\n        runner: [ubuntu-latest, ubuntu-24.04-arm]\n")
    expect.text(" ".join(sorted(f"{p}/{a}" for p, a in lists["b"])), "15/aarch64 15/x86_64",
                "pg and runner lists are read as their product")

    w = tmp_path / "w.yml"
    w.write_text(FIXTURE)
    expect.text(uncovered(w), "w.yml:18/aarch64",
                "a suites leg pytest-cluster does not run is named")
    w.write_text(FIXTURE.replace("pg: ['17', '18']\n",
                                 "include:\n          - { pg: '17', runner: ubuntu-latest }\n"
                                 "          - { pg: '18', runner: ubuntu-latest }\n"
                                 "          - { pg: '18', runner: ubuntu-24.04-arm }\n")
                 .replace("  pytest-cluster:\n    runs-on: ubuntu-latest",
                          "  pytest-cluster:\n    runs-on: ${{ matrix.runner }}"))
    expect.text(uncovered(w) or "none", "none", "control: with the leg added, nothing is named")
    w.write_text(FIXTURE.replace("  pytest-cluster:", "  pytest-other:"))
    expect.text(uncovered(w), "w.yml:no-pytest-cluster",
                "a workflow with no pytest-cluster job is named, not read as covered")
    w.write_text("jobs:\n  lint:\n    runs-on: ubuntu-latest\n")
    expect.text(uncovered(w), "w.yml:no-suites-legs",
                "a workflow with no suites legs is named, not read as covered")
    expect.text(uncovered(tmp_path / "absent.yml"), "absent.yml:unreadable",
                "a workflow that cannot be read is named")


def test_pytest_cluster_runs_every_leg_the_suites_job_runs(expect):
    """The property, on the repository's own workflows."""
    expect.text(uncovered(WORKFLOWS / "ci.yml", WORKFLOWS / "nightly.yml") or "none", "none",
                "pytest-cluster runs every (major, architecture) the suites job runs")
