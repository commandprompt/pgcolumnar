"""The pytest cluster jobs have a timeout with room above their measured time (#2232).

The `pytest-cluster` job in ci.yml and in nightly.yml runs the whole cluster half of
the corpus, serially, under `timeout-minutes`. At 40 minutes it was cancelled on a
healthy run: run 46017483389, PG 17, attempt 1, with every test up to 96% passing.
The same code passed on attempt 2 in 35.8 minutes.

Measured on 2026-11-27 over the last 60 ci.yml runs (114 finished cluster jobs that
succeeded, 2026-11-26 23:03 to 2026-11-27 14:51 UTC): median 31.2 minutes on PG 17
and 31.9 on PG 18, slowest 37.8 and 38.0. The nightly's five legs took up to 37.5.

So the rule, on both workflows: the timeout is at least one and a half times the
slowest measured job (57 minutes), so a slow runner does not cancel a passing run;
and at most twice it (76 minutes), so a hung run still stops within the hour and a
quarter. Change MEASURED_MAX_MINUTES only with new measurements, said where it is
set. Reads the workflows as text; needs no cluster and no driver.
"""

import math
import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent
WORKFLOWS = HERE.parent.parent / ".github" / "workflows"

# The slowest successful pytest-cluster job measured: PG 18, ci.yml run 46012888428,
# 2026-11-27, 38.0 minutes.
MEASURED_MAX_MINUTES = 38

TIMEOUT = re.compile(r"^    timeout-minutes:\s*(\d+)\s*$")


def job_timeout(text, name="pytest-cluster"):
    """The job's own `timeout-minutes`, or None.

    Only the job-level key counts: four spaces deep, inside the job, on a line that is
    not a comment. A step's timeout, or a comment quoting one, is not the job's.
    """
    lines = text.splitlines()
    try:
        start = lines.index(f"  {name}:")
    except ValueError:
        return None
    for line in lines[start + 1:]:
        if re.match(r"^  [A-Za-z][\w-]*:\s*$", line):
            return None
        m = TIMEOUT.match(line)
        if m:
            return int(m.group(1))
    return None


def test_the_reader_takes_only_the_jobs_own_timeout(expect):
    text = ("jobs:\n"
            "  pytest-cluster:\n"
            "    # timeout-minutes: 99\n"
            "    runs-on: ubuntu-latest\n"
            "    timeout-minutes: 60\n"
            "    steps:\n"
            "      - run: true\n"
            "        timeout-minutes: 5\n"
            "  build:\n"
            "    timeout-minutes: 15\n")
    expect.num(job_timeout(text), 60, "the job's own timeout is read, not a comment's")
    expect.text(repr(job_timeout(text.replace("    timeout-minutes: 60\n", ""))), "None",
                "a job with no timeout of its own reads as none, not as a step's or the next job's")
    expect.text(repr(job_timeout(text, "absent")), "None", "a missing job reads as none")


def test_each_cluster_job_timeout_has_its_margin(expect):
    low = math.ceil(1.5 * MEASURED_MAX_MINUTES)
    high = 2 * MEASURED_MAX_MINUTES
    for wf in ("ci.yml", "nightly.yml"):
        t = job_timeout((WORKFLOWS / wf).read_text())
        expect.text(repr(t is not None), "True", f"premise: {wf}'s pytest-cluster job has a timeout")
        expect.text("yes" if t is not None and t >= low else f"{t} < {low}", "yes",
                    f"{wf}: the cluster job's timeout is at least 1.5 times the slowest measured job")
        expect.text("yes" if t is not None and t <= high else f"{t} > {high}", "yes",
                    f"{wf}: the cluster job's timeout is at most twice the slowest measured job")
