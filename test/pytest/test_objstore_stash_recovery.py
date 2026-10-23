"""Recovery from a stash an interrupted objstore_module run left behind: the pytest
twin of `test/objstore_stash_recovery.sh` (#1341). Every check carries the shell's
name.

objstore_module.sh moves the installed module aside to reach its absent and broken
paths, and an interrupt between a move and its restore leaves a
pgcolumnar_objstore.so.probe or .away in pkglibdir. Its start guard has to tell
debris beside a good module (remove it, continue) from the only surviving copy
(restore it, continue), and the discriminator is whether the module BESIDE the
stash is valid, not whether a stash is present.

THE SUBJECT IS THE SHELL SUITE'S START GUARD, so this file runs
`test/objstore_module.sh` as a subprocess, exactly as the shell twin does, and
asserts on its exit status and on what it left in pkglibdir. Nothing here reaches
inside the guard. Three arrangements, each from the pristine copy:

  - debris: a valid module installed, and a DIFFERENT valid module at .probe;
  - only copy: nothing installed, the real module at .probe;
  - stand-in: 19 bytes of text installed, the real module at .away.

THE INSTALLED MODULE IS SHARED, so this file must run alone. The cluster job runs
its files in one process, and the shell runner keeps the shell twin in
`runs_alone` for the same reason. A pristine copy is taken first and put back in
the fixture's finally, so a wrong guard does not leave the box in the state this
file exists to recover from.
"""
import os
import pathlib
import shutil
import subprocess
import tempfile

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SUITE = REPO / "test" / "objstore_module.sh"


def _valid(path):
    """A module is valid when nm finds the entry point: the shell's mod_is_valid."""
    if not path.exists():
        return False
    r = subprocess.run(["nm", "-D", "--defined-only", str(path)],
                       capture_output=True, text=True)
    return any(" T pgcolumnar_objstore_init" in l for l in r.stdout.splitlines())


def _same(a, b):
    return a.exists() and b.exists() and a.read_bytes() == b.read_bytes()


def _run_suite(pg_config):
    env = dict(os.environ, PGC_SKIP_BUILD="1")
    env.pop("PGC_PORT", None)
    r = subprocess.run(["bash", str(SUITE), pg_config], capture_output=True,
                       text=True, env=env, timeout=1800)
    if r.returncode != 0:
        # SAID, not swallowed: CI's log is the only place a reader sees why the inner
        # suite failed, and an exit status alone named nothing (#1717 review).
        tail = (r.stdout + r.stderr).splitlines()[-40:]
        print(f"-- {SUITE.name} exited {r.returncode}; last {len(tail)} lines:")
        for line in tail:
            print(f"   {line}")
    return r.returncode


@pytest.fixture(scope="module")
def osr(pgc_cluster):
    """-> what each arrangement looked like before the run and what it left after."""
    mod = pathlib.Path(pgc_cluster.libdir) / "pgcolumnar_objstore.so"
    probe, away = mod.with_name(mod.name + ".probe"), mod.with_name(mod.name + ".away")
    if not _valid(mod):
        return {"valid": False, "mod": str(mod)}
    fd, safe = tempfile.mkstemp(prefix="pgc-objstore-safe.")
    os.close(fd)
    safe = pathlib.Path(safe)
    shutil.copy2(mod, safe)

    def pristine():
        probe.unlink(missing_ok=True)
        away.unlink(missing_ok=True)
        shutil.copy2(safe, mod)

    o = {"valid": True}
    try:
        # debris beside a good installation: a stale build, not a copy
        pristine()
        o["d valid before"] = _valid(mod)
        shutil.copy2(mod, probe)
        with open(probe, "ab") as f:
            f.write(b"stale")
        o["d stash placed"] = probe.exists()
        o["d stash differs"] = _valid(probe) and not _same(probe, mod)
        o["d rc"] = _run_suite(pgc_cluster.pg_config)
        o["d same"] = _same(mod, safe)
        o["d stash left"] = probe.exists()
        o["d valid after"] = _valid(mod)

        # the only surviving copy, at .probe
        pristine()
        mod.rename(probe)
        o["o absent"] = not mod.exists()
        o["o stash real"] = _valid(probe)
        o["o rc"] = _run_suite(pgc_cluster.pg_config)
        o["o valid after"] = _valid(mod)
        o["o stash left"] = probe.exists()

        # a stand-in installed over the real module, which is at .away
        pristine()
        mod.rename(away)
        mod.write_bytes(b"not a shared object")
        o["s size"] = mod.stat().st_size
        o["s stand-in valid"] = _valid(mod)
        o["s stash real"] = _valid(away)
        o["s rc"] = _run_suite(pgc_cluster.pg_config)
        o["s valid after"] = _valid(mod)
        o["s stash left"] = away.exists()
    finally:
        probe.unlink(missing_ok=True)
        away.unlink(missing_ok=True)
        if not _valid(mod):
            shutil.copy2(safe, mod)
        safe.unlink()
    return o


def _unrunnable(osr, expect):
    if osr["valid"]:
        return False
    expect.cannot_run("UNMET_PRECONDITION",
                      f"no valid module installed at {osr['mod']}, so there is no state to arrange",
                      name="stash recovery")
    return True


def _yes(b):
    return "yes" if b else "no"


def _present(b):
    return "present" if b else "absent"


def test_the_session_build_installed_a_valid_module(osr, expect):
    """The shell builds here unless PGC_SKIP_BUILD is set and records a failed build
    by this name. The session fixture builds and installs before any test runs and
    raises if either fails, so what is left to observe here is the artifact: a
    build that installed no module that nm can read measured nothing below."""
    expect.text(_yes(osr["valid"]), "yes",
                "the build or install failed, so nothing below measures the guard")


# ---- debris beside a good installation must not stop the suite ----------------

def test_debris_premise_installed_module_valid(osr, expect):
    if _unrunnable(osr, expect):
        return
    expect.text(_yes(osr["d valid before"]), "yes",
                "premise: the installed module is valid before this slice arranges anything")


def test_debris_premise_stash_in_place(osr, expect):
    if _unrunnable(osr, expect):
        return
    expect.text(_yes(osr["d stash placed"]), "yes",
                "premise: a debris stash really is in place for the run below")


def test_debris_premise_stash_is_a_different_valid_module(osr, expect):
    if _unrunnable(osr, expect):
        return
    expect.text(_yes(osr["d stash differs"]), "yes",
                "premise: and the debris is a valid module that differs from the installed one")


def test_debris_suite_completes(osr, expect):
    if _unrunnable(osr, expect):
        return
    expect.num(osr["d rc"], 0,
               "the suite runs to completion with debris beside a valid module")


def test_debris_not_restored_over_the_module(osr, expect):
    if _unrunnable(osr, expect):
        return
    expect.text("same" if osr["d same"] else "replaced", "same",
                "and the installed module is still the one that was there, not the debris")


def test_debris_gone(osr, expect):
    if _unrunnable(osr, expect):
        return
    expect.text(_present(osr["d stash left"]), "absent",
                "the debris is gone afterwards")


def test_debris_module_still_valid(osr, expect):
    if _unrunnable(osr, expect):
        return
    expect.text(_yes(osr["d valid after"]), "yes",
                "and the module left installed is still valid")


# ---- a stash that is the only surviving copy must be restored, not refused ----

def test_only_copy_premise_nothing_installed(osr, expect):
    if _unrunnable(osr, expect):
        return
    expect.text(_present(not osr["o absent"]), "absent",
                "premise: nothing is installed at the module's path for this slice")


def test_only_copy_premise_stash_is_real(osr, expect):
    if _unrunnable(osr, expect):
        return
    expect.text(_yes(osr["o stash real"]), "yes",
                "premise: and the stash beside it is the real module")


def test_only_copy_suite_completes(osr, expect):
    if _unrunnable(osr, expect):
        return
    expect.num(osr["o rc"], 0,
               "the suite recovers the only surviving copy and runs to completion")


def test_only_copy_reinstalled(osr, expect):
    if _unrunnable(osr, expect):
        return
    expect.text(_yes(osr["o valid after"]), "yes",
                "the module is installed again afterwards, and valid")


def test_only_copy_stash_not_left(osr, expect):
    if _unrunnable(osr, expect):
        return
    expect.text(_present(osr["o stash left"]), "absent",
                "and the stash is not left behind for the next run to trip on")


# ---- a stand-in installed over the real module is the destructive case --------

def test_stand_in_premise_installed(osr, expect):
    if _unrunnable(osr, expect):
        return
    expect.num(osr["s size"], 19,
               "premise: a 19-byte stand-in really is installed as the module")


def test_stand_in_premise_not_a_module(osr, expect):
    if _unrunnable(osr, expect):
        return
    expect.text(_yes(osr["s stand-in valid"]), "no",
                "premise: which is not a module")


def test_stand_in_premise_stash_is_real(osr, expect):
    if _unrunnable(osr, expect):
        return
    expect.text(_yes(osr["s stash real"]), "yes",
                "premise: and the real module is the thing parked at the stash")


def test_stand_in_suite_completes(osr, expect):
    if _unrunnable(osr, expect):
        return
    expect.num(osr["s rc"], 0,
               "the suite replaces the stand-in with the real module and completes")


def test_stand_in_real_module_survived(osr, expect):
    if _unrunnable(osr, expect):
        return
    expect.text(_yes(osr["s valid after"]), "yes",
                "the real module survived, rather than being deleted with the stash")


def test_stand_in_stash_cleared(osr, expect):
    if _unrunnable(osr, expect):
        return
    expect.text(_present(osr["s stash left"]), "absent",
                "and the stash is cleared")
