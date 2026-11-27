"""The pytest cluster jobs have a timeout with room above their measured time (#2232).

The `pytest-cluster` job in ci.yml and in nightly.yml runs the whole cluster half of
the corpus, serially, under `timeout-minutes`. At 40 minutes it was cancelled on a
healthy run: run 46017483389, PG 17, attempt 1, with every test up to 96% passing.
The same code passed on attempt 2 in 35.8 minutes.

Measured on 2026-11-27 over the last 60 ci.yml runs (114 finished cluster jobs that
succeeded, 2026-11-26 23:03 to 2026-11-27 14:51 UTC): median 31.2 minutes on PG 17
and 31.9 on PG 18, slowest 37.8 and 38.0. The nightly's legs took longer later that
day: 38.9 (run 46032807880, PG 18 aarch64) and 39.8 (run 46040381293, PG 18 x86_64).
The cancelled PG 17 leg of run 46017483389 had already run 40.25 minutes (14:36:46 to
15:17:01 UTC) at `[ 96%]`, so the slowest job is at least 41 minutes, counting that
leg as a lower bound.

So the rule, on both workflows: the timeout is at least one and a half times the
slowest measured job (62 minutes), so a slow runner does not cancel a passing run;
and at most twice it (82 minutes), so a hung run still stops within an hour and a
half. Change MEASURED_MAX_MINUTES only with new measurements, said where it is
set. Reads the workflows as text; needs no cluster and no driver.
"""

import math
import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent
WORKFLOWS = HERE.parent.parent / ".github" / "workflows"

# The slowest pytest-cluster job measured, as a lower bound: PG 17, ci.yml run
# 46017483389 attempt 1, cancelled at 40.25 minutes with 96% of its tests done
# (2026-11-27). The slowest that finished: PG 18 x86_64, nightly run 46040381293,
# 39.8 minutes.
MEASURED_MAX_MINUTES = 41

# The value, then optionally a YAML comment: `timeout-minutes: 65 # why` is 65.
TIMEOUT = re.compile(r"^    timeout-minutes:\s*(\d+)\s*(#.*)?$")


def job_timeouts(text, name="pytest-cluster"):
    """Every job-level `timeout-minutes` value in the job, in order.

    Only the job-level key counts: four spaces deep, inside the job, on a line that is
    not a comment. A step's timeout, or a comment quoting one, is not the job's.
    """
    lines = text.splitlines()
    try:
        start = lines.index(f"  {name}:")
    except ValueError:
        return []
    found = []
    for line in lines[start + 1:]:
        if re.match(r"^  [A-Za-z][\w-]*:\s*$", line):
            break
        m = TIMEOUT.match(line)
        if m:
            found.append(int(m.group(1)))
    return found


def job_timeout(text, name="pytest-cluster"):
    """The job's own `timeout-minutes`, or None when it has none or more than one.

    A repeated key is refused rather than read: this reader would take the first and
    PyYAML takes the last, so the value the guard checked might not be the one run.
    """
    found = job_timeouts(text, name)
    return found[0] if len(found) == 1 else None


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
    expect.text(repr(job_timeout(text.replace("    timeout-minutes: 60\n",
                                              "    timeout-minutes: 60 # why\n"))), "60",
                "a trailing YAML comment after the value still reads as the value")
    dup = text.replace("    steps:\n", "    timeout-minutes: 30\n    steps:\n")
    expect.text(repr(job_timeouts(dup)), "[60, 30]",
                "premise: the reader sees both job-level keys of a repeated timeout")
    expect.text(repr(job_timeout(dup)), "None",
                "a job-level timeout given twice reads as none, not as either value")


def test_each_cluster_job_timeout_has_its_margin(expect):
    low = math.ceil(1.5 * MEASURED_MAX_MINUTES)
    high = 2 * MEASURED_MAX_MINUTES
    for wf in ("ci.yml", "nightly.yml"):
        t = job_timeout((WORKFLOWS / wf).read_text())
        expect.text(repr(t is not None), "True",
                    f"premise: {wf}'s pytest-cluster job has exactly one timeout of its own")
        expect.text("yes" if t is not None and t >= low else f"{t} < {low}", "yes",
                    f"{wf}: the cluster job's timeout is at least 1.5 times the slowest measured job")
        expect.text("yes" if t is not None and t <= high else f"{t} > {high}", "yes",
                    f"{wf}: the cluster job's timeout is at most twice the slowest measured job")
