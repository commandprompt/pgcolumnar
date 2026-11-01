"""A run is not disturbed by other sessions on a shared box (#1915). The pytest twin
of `test/harness_isolation.sh`, every check by the shell's name.

Two hazards from the shared dev container:

1. A postmaster started inside `flock <lock> cmd` held the lock until it stopped,
   because every process the command starts inherits the descriptor. This harness
   starts servers through `subprocess`, which closes every descriptor above 2 in the
   child (`close_fds`), so a cluster it starts under a held lock must not hold it.
   Measured here on a real cluster that `make_cluster` starts in a child process run
   under flock(1), left running after that process exits.
2. Another session can install into the shared prefix during a run. The session now
   records the library digest it started with, and its teardown raises when the
   digest changed (`require_library_unchanged`), so the run ends failed. The
   replacement is made on a copy; the shared library is never touched.

The Python harness's own machinery throughout; it does not source lib.sh.
"""
import fcntl
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

import pgc_cluster as pc

HERE = pathlib.Path(__file__).resolve().parent


def _fds_on(pid, path):
    """-> (descriptors of `pid` on `path`, all descriptors of `pid`)."""
    fd_dir = pathlib.Path(f"/proc/{pid}/fd")
    try:
        links = [os.readlink(fd_dir / n) for n in os.listdir(fd_dir)]
    except OSError:
        return 0, 0
    return sum(1 for link in links if link == str(path)), len(links)


def _lock_free(path):
    """-> flock -n's status on `path`: 0 free, 1 held."""
    return subprocess.run(["flock", "-n", str(path), "true"]).returncode


@pytest.fixture
def lock(tmp_path):
    path = tmp_path / "run.lock"
    path.touch()
    os.chmod(tmp_path, 0o777)
    os.chmod(path, 0o666)
    return path


def test_control_a_plain_child_under_flock_holds_the_lock(lock, expect):
    pid = int(subprocess.run(
        ["flock", str(lock), "bash", "-c", "sleep 60 >/dev/null 2>&1 & echo $!"],
        capture_output=True, text=True, check=True).stdout.strip())
    try:
        held, _ = _fds_on(pid, lock)
        expect.text(f"{held} held, lock free={_lock_free(lock)}", "1 held, lock free=1",
                    "control: a child of a plain shell under flock holds the lock")
    finally:
        subprocess.run(["kill", str(pid)])


START = """
import sys
sys.path.insert(0, {here!r})
import pgc_cluster
cluster, root = pgc_cluster.make_cluster({cfg!r}, "gw-isolation")
print(cluster.datadir)
print(root)
"""


def _postmaster_pid(datadir):
    try:
        return int((datadir / "postmaster.pid").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 0


def test_a_cluster_started_under_a_held_lock_does_not_hold_it(pgc_cluster, lock, expect):
    proc = subprocess.run(
        ["flock", str(lock), sys.executable, "-c",
         START.format(here=str(HERE), cfg=pgc_cluster.pg_config)],
        capture_output=True, text=True)
    lines = proc.stdout.split()
    assert proc.returncode == 0 and len(lines) == 2, proc.stderr[-2000:]
    datadir, root = pathlib.Path(lines[0]), pathlib.Path(lines[1])
    try:
        pm = _postmaster_pid(datadir)
        held, total = _fds_on(pm, lock)
        alive = 1 if pm and pathlib.Path(f"/proc/{pm}").exists() and total > 0 else 0
        expect.num(alive, 1, "premise: the postmaster outlives the flock command that started it")
        expect.text(f"{held} of {total}", f"0 of {total}",
                    "a postmaster started under a held lock holds no descriptor on it")
        expect.num(_lock_free(lock), 0, "another run takes that lock while the postmaster runs")
    finally:
        stopper = pc.Cluster(pgc_cluster.pg_config, "gw-isolation", datadir, 0)
        stopper._started = True
        stopper.stop()
        shutil.rmtree(root, ignore_errors=True)


def test_a_lock_the_harness_took_itself_stays_held(lock, expect):
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        expect.num(_lock_free(lock), 1, "a lock the harness's own process took stays held")


def test_the_session_records_the_library_it_runs(pgc_cluster, expect):
    expect.text(f"[{getattr(pgc_cluster, 'so_at_start', '')}]",
                f"[{pc.installed_library(pgc_cluster.pg_config)}]",
                "setup records the digest of the library it verified")


@pytest.fixture
def copy_of_library(pgc_cluster, tmp_path):
    so = tmp_path / "pgcolumnar.so"
    shutil.copyfile(pgc_cluster.so_path, so)
    return so


def _report(so, recorded):
    fn = getattr(pc, "library_change_report", None)
    return fn(so, recorded) if fn else "pgc_cluster has no library_change_report"


def test_a_replaced_library_is_reported(copy_of_library, expect):
    before = pc._md5_of(copy_of_library)
    expect.text(f"report={_report(copy_of_library, before)}", "report=None",
                "an unchanged library is not reported")
    with open(copy_of_library, "ab") as fh:
        fh.write(b"x")
    after = pc._md5_of(copy_of_library)
    report = str(_report(copy_of_library, before))
    expect.text(f"{int(before in report and after in report)} report naming both: {report}",
                f"1 report naming both: {report}",
                "a library replaced after setup is reported with both digests")


def test_a_run_whose_library_changed_fails(copy_of_library, expect):
    before = pc._md5_of(copy_of_library)
    with open(copy_of_library, "ab") as fh:
        fh.write(b"x")
    fn = getattr(pc, "require_library_unchanged", None)
    try:
        if fn is None:
            raise LookupError("pgc_cluster has no require_library_unchanged")
        fn(copy_of_library, before)
        outcome = "the run went on"
    except RuntimeError as exc:
        outcome = f"failed: {type(exc).__name__}" if "changed during this run" in str(exc) \
            else f"failed for another reason: {exc}"
    except LookupError as exc:
        outcome = str(exc)
    expect.text(outcome, "failed: RuntimeError", "a run whose library changed mid-run ends failed")
