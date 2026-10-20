"""The corpus must not report on source it never built.

@jdatcmd demonstrated the defect this closes: with
`#error THIS SOURCE IS BROKEN AND CANNOT BUILD` appended to
src/columnar_projection.c and nothing rebuilt,

    pytest                          -> 25 passed, exit 0
    bash test/native_projection.sh  -> FATAL: the build failed ..., exit 1

The bash harness has refused that since #536. This one did not, because it never
built, never installed and never compared anything -- `Cluster.so_md5` printed a
fingerprint that nothing read.

The refusal now comes from `pgc_build_and_install` in test/lib.sh, driven from
Python, so there is one implementation rather than two that can drift.

Two levels of arm here, deliberately. The injected-runner arms pin what the
Python side does with a verdict. The `bash` arms pin the SHELL PLUMBING -- the
sourcing, the quoting and the exit-status path -- which an injected runner
cannot reach and which is where a wrong quote would hide.

WHAT THIS FILE REACHES INTO, AND WHAT IT NO LONGER DOES (#432).

CONTEXT.md's independence rule: the two harnesses are parallel in functionality and
independent in implementation, and a pytest test that drives `test/lib.sh` is the
first measurement wearing a Python wrapper -- it agrees with the shell by
construction and can never report it wrong. This file was the largest item on that
inventory.

The fingerprint and manifest arms no longer go through it. Their subject is
`test/pgc_fingerprint.py`, which has been the ONE implementation since #907;
`pgc_source_fingerprint` and `pgc_source_manifest` in lib.sh are thin wrappers that
shell out to exactly that module, so the old path was
python -> bash -> lib.sh -> python3 -> the module. They call the module now, in
process where the property allows and through the module's own CLI where it needs a
different user or locale. Measured before converting a single arm: the fingerprint is
byte-identical through both paths, the manifest is identical line for line, and both
agree across LC_ALL=C, C.UTF-8 and en_US.UTF-8.

WHAT STILL GOES THROUGH bash, and why each one is not a wrapper to be removed:

  * `pgc_write_source_stamp`, `pgc_source_stamp_path`, `pgc_freshness_report` and
    `pgc_freshness_verdict` are pure shell. They are the shell harness's OWN
    implementation, not a wrapper over shared code, so an arm here is a second
    harness testing the first. Those belong to the shell harness and are the next
    step, not this one.
  * `test_the_two_fingerprint_implementations_cover_the_same_inputs` reaches across
    ON PURPOSE and is the one arm that should. Its subject is that neither side
    carries a private copy, which cannot be expressed without touching both.
    CONTEXT.md's rule allows exactly this -- "say which, and say why" -- so this is
    the saying: it caught four defects in one day (#907), it is the guard that
    reddens on the first edit if a private implementation returns, and the property
    is the relationship rather than either side.
  * `test_the_fix_does_not_rebaseline_stamps_already_on_disk` embeds the PREVIOUS
    shell algorithm as a fixture and compares today's value against it. The fixture
    is a file the test builds itself, which the rule permits, but it calls
    `pgc_source_build_dirs` out of the real lib.sh for its directory list. Feeding it
    the module's own `build_dirs` would make it self-contained; that is a change to
    a historical-parity arm and it is not in this one.
"""

import hashlib
import inspect
import os
import sys
import tempfile
import shutil
import pwd
import pathlib
import re
import subprocess

import pytest

from pgc_cluster import (build_and_install, build_once, make_cluster,
                         source_manifest,
                         source_fingerprint)


class _Proc:
    def __init__(self, rc, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_a_failed_build_raises_rather_than_returning(expect):
    with pytest.raises(RuntimeError) as exc:
        build_and_install("/nowhere", "/bin/false", "18",
                          runner=lambda argv: _Proc(1, err="compiler said no"))
    expect.at_least(str(exc.value).count("refusing"), 1,
                    "the refusal says it is refusing")
    expect.at_least(str(exc.value).count("compiler said no"), 1,
                    "and carries the build's own output, not a summary")


def test_the_refusal_names_the_tree_it_refused(expect):
    """A reader with several worktrees needs to know WHICH source failed."""
    with pytest.raises(RuntimeError) as exc:
        build_and_install("/root/some-worktree", "/bin/false", "18",
                          runner=lambda argv: _Proc(1))
    expect.at_least(str(exc.value).count("/root/some-worktree"), 1,
                    "the message names the source directory")


def test_a_successful_build_is_silent(expect):
    """Positive control: the guard must not fire on a build that worked."""
    build_and_install("/nowhere", "/bin/false", "18",
                      runner=lambda argv: _Proc(0))
    expect.num(1, 1, "a returncode of 0 raises nothing")


def _fake_tree(tmp_path, verdict_rc):
    """A tree whose test/lib.sh defines the function with a fixed verdict.

    It reports its argument count and each argument on stderr (#1339), so an arm can
    see the quoting and the order rather than only the exit status.
    """
    (tmp_path / "test").mkdir(parents=True, exist_ok=True)
    (tmp_path / "test" / "lib.sh").write_text(
        "pgc_build_and_install() {\n"
        "  echo '-- building'\n"
        "  printf 'ARGS %s [%s] [%s] [%s]\\n' \"$#\" \"$1\" \"$2\" \"$3\" >&2\n"
        f"  [ {verdict_rc} -eq 0 ] || {{ echo 'FATAL: the build failed' >&2; return 1; }}\n"
        "  return 0\n"
        "}\n"
    )
    return tmp_path


def test_the_shell_path_really_refuses(tmp_path, expect):
    """The real bash invocation, not an injected runner: sourcing, quoting and
    the exit status all have to work for the refusal to arrive."""
    # A SPACE AND A `$` IN THE PATH (#1339). The tree used to sit in a plain tmp_path
    # and the fake ignored its arguments, so dropping the quotes or swapping the
    # arguments left both arms green.
    tree = _fake_tree(tmp_path / "broken tree $HOME", 1)
    with pytest.raises(RuntimeError) as exc:
        build_and_install(tree, "/bin/false", "18")
    expect.at_least(str(exc.value).count("FATAL: the build failed"), 1,
                    "the shell's own FATAL reaches the Python caller")
    expect.contains(str(exc.value), f"ARGS 3 [{tree}] [/bin/false] [18]",
                    "and lib.sh received the tree, pg_config and major, each one whole")


def test_the_shell_path_accepts_a_good_build(tmp_path, expect):
    """Control for the arm above, through the same plumbing."""
    tree = _fake_tree(tmp_path / "ok", 0)
    build_and_install(tree, "/bin/false", "18")
    expect.num(1, 1, "a zero verdict through real bash raises nothing")


def test_a_missing_lib_sh_is_a_refusal_not_a_pass(tmp_path, expect):
    """If lib.sh cannot be sourced there is no guard at all, so the harness must
    fail rather than proceed ungated."""
    with pytest.raises(RuntimeError) as exc:
        build_and_install(tmp_path / "empty", "/bin/false", "18")
    expect.at_least(str(exc.value).count("refusing"), 1,
                    "an unsourceable lib.sh refuses")


def _answerable(tmp_path, name="cfg"):
    """A pg_config that ANSWERS, plus the libdir it names.

    Three arms below used to pass `/bin/pg_config`, whose answer depends on the
    machine: it resolves here and may not on a CI runner. Once `build_once` observes
    the prefix, "does the skip happen" would depend on that, and a guard must not.
    The fixture supplies the answer so the arms mean the same thing everywhere.
    """
    libdir = tmp_path / (name + "_lib")
    libdir.mkdir(exist_ok=True)
    return _pg_config_answering(tmp_path, libdir), libdir


def _installs_into(libdir, calls):
    """A runner that does what an install does: record the call and write the library.

    THE SAME BYTES EACH TIME, so an unchanged prefix still matches its marker. The
    runners these replace wrote nothing, so the prefix held no library and the arms
    passed only because build_once certified "absent" (#1339), which it now refuses.
    """
    def run(argv):
        calls.append(argv)
        (pathlib.Path(libdir) / "pgcolumnar.so").write_bytes(b"build")
        return _Proc(0)
    return run


def test_build_once_builds_once_and_then_skips(tmp_path, expect):
    """The xdist workers share one prefix, so the install is serialised rather
    than skipped."""
    # A fingerprintable tree: the marker is keyed on source content, so a tree
    # with nothing to compile deliberately never certifies and always rebuilds.
    tree = _tree_with_source(tmp_path, "ok2", "int a = 1;\n")
    pgc, libdir = _answerable(tmp_path)
    lock = str(tmp_path / "lock")
    calls = []
    counting = _installs_into(libdir, calls)

    first = build_once(tree, str(pgc), "18", lock_path=lock, runner=counting)
    second = build_once(tree, str(pgc), "18", lock_path=lock, runner=counting)
    expect.text(first, "built", "the first caller builds")
    expect.text(second, "already-built", "the second finds the marker")
    expect.num(len(calls), 1, "and the build ran exactly once")


def test_build_once_serialises_concurrent_callers(tmp_path, expect):
    """The docstring above is about workers that call AT ONCE, and two calls in a row
    say nothing about that: with the lock removed they still build once, because the
    first has written its marker before the second starts (#1339). Two threads call
    together here, with a runner slow enough that the second arrives mid-build; the
    lock makes it wait and then find the marker."""
    import threading
    import time

    tree = _tree_with_source(tmp_path, "ok4", "int a = 1;\n")
    pgc, libdir = _answerable(tmp_path, "concurrent")
    lock = str(tmp_path / "lock4")
    calls = []
    quick = _installs_into(libdir, calls)

    def slow(argv):
        time.sleep(1.0)
        return quick(argv)

    results = []
    threads = [threading.Thread(target=lambda: results.append(
        build_once(tree, str(pgc), "18", lock_path=lock, runner=slow))) for _ in range(2)]
    for t in threads:
        t.start()
        time.sleep(0.2)
    for t in threads:
        t.join()
    expect.text(",".join(sorted(results)), "already-built,built",
                "two callers at once: one builds and the other waits for its marker")
    expect.num(len(calls), 1, "and the concurrent build ran exactly once")


def test_build_once_rebuilds_for_a_different_prefix(tmp_path, expect):
    """Keying the marker on 'did anyone build' would reintroduce the defect for
    anyone who runs the corpus against two majors in turn.

    THE PREFIX MUST BE THE ONLY DIFFERENCE (#1339). The first version passed two
    pg_config paths that do not exist, so no library was ever observed, no marker
    was ever written, and every call built whatever the key held: with the prefix
    and major dropped from the key, it stayed green. Here both prefixes answer, and
    answer the SAME library directory, at the same major, and a control shows the
    marker is written and honoured for an unchanged prefix.
    """
    tree = _tree_with_source(tmp_path, "ok3", "int a = 1;\n")
    lock = str(tmp_path / "lock2")
    shared = tmp_path / "shared_lib"
    shared.mkdir()
    pfx = []
    for n in ("pfx_a", "pfx_b"):
        (tmp_path / n).mkdir()
        pfx.append(str(_pg_config_answering(tmp_path / n, shared)))
    calls = []
    counting = _installs_into(shared, calls)

    build_once(tree, pfx[0], "18", lock_path=lock, runner=counting)
    build_once(tree, pfx[1], "18", lock_path=lock, runner=counting)
    expect.num(len(calls), 2, "a different prefix builds again")
    again = build_once(tree, pfx[1], "18", lock_path=lock, runner=counting)
    expect.text(f"{again}|{len(calls)}", "already-built|2",
                "control: the same prefix again finds the marker and does not build")


def _tree_with_source(tmp_path, name, body):
    """A fake tree that is fingerprintable: it has src/*.c and a Makefile."""
    tree = _fake_tree(tmp_path / name, 0)
    (tree / "src").mkdir(parents=True, exist_ok=True)
    (tree / "src" / "columnar.c").write_text(body)
    (tree / "Makefile").write_text("all:\n\ttrue\n")
    return tree


def test_editing_the_source_rebuilds(tmp_path, expect):
    """The marker is keyed on the source fingerprint, not just the prefix.

    Keying on pg_config and major alone would skip the build after a source
    edit -- the staleness this whole guard exists to stop, reintroduced by the
    optimisation meant to make the guard cheap.
    """
    tree = _tree_with_source(tmp_path, "edited", "int a = 1;\n")
    pgc, libdir = _answerable(tmp_path)
    lock = str(tmp_path / "lock3")
    calls = []
    counting = _installs_into(libdir, calls)

    build_once(tree, str(pgc), "18", lock_path=lock, runner=counting)
    build_once(tree, str(pgc), "18", lock_path=lock, runner=counting)
    expect.num(len(calls), 1, "unchanged source builds once")

    (tree / "src" / "columnar.c").write_text("int a = 2;\n")
    build_once(tree, str(pgc), "18", lock_path=lock, runner=counting)
    expect.num(len(calls), 2, "an edited source builds again")


def _pg_config_answering(tmp_path, libdir):
    """A real executable that answers --pkglibdir, so the test drives the
    production path rather than a seam added for the test."""
    pgc = tmp_path / "pg_config"
    pgc.write_text("#!/bin/sh\n"
                   "case \"$1\" in\n"
                   "  --pkglibdir) echo %s ;;\n"
                   "  *) echo unsupported >&2; exit 1 ;;\n"
                   "esac\n" % libdir)
    pgc.chmod(0o755)
    return pgc


def test_build_once_rebuilds_when_another_process_replaced_the_library(tmp_path, expect):
    """#956. The marker answers "did this layer build this source", and it is read
    as "does the prefix hold that build". Those are different claims.

    Measured twice in one day: a perf run installed a pre-#945 library into the
    shared prefix, and the corpus then reported ten failures here and nineteen on
    @jdatcmd's box, with the code entirely innocent. The source never changed, so
    the source fingerprint matched and the build was skipped.
    """
    tree = _tree_with_source(tmp_path, "replaced", "int a = 1;\n")
    libdir = tmp_path / "libdir"
    libdir.mkdir()
    so = libdir / "pgcolumnar.so"
    pgc = _pg_config_answering(tmp_path, libdir)
    lock = str(tmp_path / "lock956")

    calls = []

    def installing(argv):
        # An install WRITES the library, which is what makes this fixture faithful:
        # the thing the marker should be describing is a file on disk.
        calls.append(argv)
        so.write_bytes(b"build-%d" % len(calls))
        return _Proc(0)

    # PREMISE: the fake pg_config really answers, or the arm below is vacuous.
    probe = subprocess.run([str(pgc), "--pkglibdir"], capture_output=True, text=True)
    expect.num(probe.returncode, 0, "premise: the fake pg_config answers --pkglibdir")
    expect.text(probe.stdout.strip(), str(libdir), "and it names the library directory")

    expect.text(build_once(tree, str(pgc), "18", lock_path=lock, runner=installing),
                "built", "the first caller builds")
    expect.text(build_once(tree, str(pgc), "18", lock_path=lock, runner=installing),
                "already-built", "an untouched prefix still skips, as before")
    expect.num(len(calls), 1, "so the build ran once")

    # A THIRD PARTY overwrites the installed library. Source unchanged, prefix
    # unchanged, major unchanged -- only the artifact differs.
    so.write_bytes(b"someone-elses-build")
    expect.text(build_once(tree, str(pgc), "18", lock_path=lock, runner=installing),
                "built", "a replaced library rebuilds rather than certifying")
    expect.num(len(calls), 2, "and the build actually ran, rather than the verdict alone changing")


def test_build_once_rebuilds_when_the_library_was_deleted(tmp_path, expect):
    """Absent is not fresh. A prefix someone cleaned must not read as built."""
    tree = _tree_with_source(tmp_path, "deleted", "int a = 1;\n")
    libdir = tmp_path / "libdir2"
    libdir.mkdir()
    so = libdir / "pgcolumnar.so"
    pgc = _pg_config_answering(tmp_path, libdir)
    lock = str(tmp_path / "lock956b")
    calls = []

    def installing(argv):
        calls.append(argv)
        so.write_bytes(b"build")
        return _Proc(0)

    build_once(tree, str(pgc), "18", lock_path=lock, runner=installing)
    expect.num(len(calls), 1, "the first caller builds")
    so.unlink()
    expect.text(build_once(tree, str(pgc), "18", lock_path=lock, runner=installing),
                "built", "a deleted library rebuilds")
    expect.num(len(calls), 2, "and the build ran")


def test_an_install_that_leaves_no_library_is_refused(tmp_path, expect):
    """Absent is not fresh, and that includes absent straight after an install (#1339).

    The install reported success and the prefix holds no library: a broken install
    target. build_once used to write "absent" into the marker and certify it, so the
    next call skipped the build over a prefix with nothing in it.
    """
    tree = _tree_with_source(tmp_path, "noinstall", "int a = 1;\n")
    pgc, libdir = _answerable(tmp_path)
    lock = str(tmp_path / "lock-noinstall")
    with pytest.raises(RuntimeError) as exc:
        build_once(tree, str(pgc), "18", lock_path=lock, runner=lambda argv: _Proc(0))
    expect.contains(str(exc.value), "no library",
                    "an install that leaves no library is refused, and says why")
    expect.num(1 if pathlib.Path(lock + ".done").exists() else 0, 0,
               "and no marker was written, so the next call cannot skip")


def test_a_prefix_that_cannot_be_observed_never_certifies(tmp_path, expect):
    """The degraded path FAILS CLOSED (#956 review, @jdatcmd).

    Writing `unobserved` into the marker made two consecutive unobservable calls
    match and skip -- a fail-open inside a change about a fail-open. @jdatcmd
    constructed it rather than arguing it. Writing no marker at all costs one
    condition and removes the need to reason about reachability.
    """
    tree = _tree_with_source(tmp_path, "unobservable", "int a = 1;\n")
    lock = str(tmp_path / "lock956c")
    calls = []

    def counting(argv):
        calls.append(argv)
        return _Proc(0)

    expect.text(build_once(tree, "/nonexistent/pg_config", "18",
                           lock_path=lock, runner=counting),
                "built", "an unobservable prefix builds")
    expect.text(build_once(tree, "/nonexistent/pg_config", "18",
                           lock_path=lock, runner=counting),
                "built", "and builds AGAIN rather than certifying a prefix it cannot see")
    expect.num(len(calls), 2, "so the build really ran both times")
    expect.num(1 if pathlib.Path(lock + ".done").exists() else 0, 0,
               "and no marker was written, so there is nothing to match against")


def test_the_fingerprint_reads_content_not_mtime(tmp_path, expect):
    """A checkout or a branch switch rewrites mtimes without changing what
    compiles, and `git stash` does the reverse. Content is the honest input."""
    tree = _tree_with_source(tmp_path, "mtime", "int a = 1;\n")
    before = source_fingerprint(tree)
    (tree / "src" / "columnar.c").touch()
    expect.text(source_fingerprint(tree), before,
                "touching a file does not change the fingerprint")
    (tree / "src" / "columnar.c").write_text("int a = 2;\n")
    expect.differ(source_fingerprint(tree), before,
                  "changing its content does")


def test_an_unfingerprintable_tree_always_rebuilds(tmp_path, expect):
    """No fingerprint means no key, and no key must mean rebuild rather than
    skip. `unknown` is never allowed to read as `fresh` anywhere in this
    harness."""
    tree = _fake_tree(tmp_path / "nosrc", 0)   # no src/, no Makefile
    expect.text(repr(source_fingerprint(tree)), "None",
                "a tree with nothing to compile has no fingerprint")
    lock = str(tmp_path / "lock4")
    # AN ANSWERING PREFIX THAT HOLDS A LIBRARY (#1339). With `/bin/pg_config` the arm
    # depended on the machine: where that path does not exist the prefix is
    # unobservable, no marker is written, and both calls build whatever the
    # fingerprint says -- so removing the `fp and` guard left it green there.
    pgc, libdir = _answerable(tmp_path)
    calls = []

    def installing(argv):
        calls.append(argv)
        (libdir / "pgcolumnar.so").write_bytes(b"build")
        return _Proc(0)

    build_once(tree, str(pgc), "18", lock_path=lock, runner=installing)
    build_once(tree, str(pgc), "18", lock_path=lock, runner=installing)
    expect.num(len(calls), 2, "both calls build, because neither could be certified")


# ---------------------------------------------------------------------------
# The ORDER of build and server start, which is not a detail.
#
# shared_preload_libraries maps the library at postmaster start, so a cluster
# started before the install keeps the OLD .so mapped for its whole life. The
# build reports success and every test still measures the previous branch's
# code -- the guard defeated by the order of two lines. This is how it showed
# up: the first run after a source change failed to start a cluster, and the
# next run passed because the install had already landed.


class _C:
    """Just enough Cluster to drive the verdict."""
    from pgc_cluster import Cluster
    server_binary_verdict = Cluster.server_binary_verdict


def test_a_server_older_than_the_library_is_refused(expect):
    expect.text(_C().server_binary_verdict(so_mtime=200, postmaster_epoch=100),
                "predates", "the .so was written after the server started")


def test_a_server_started_after_the_library_is_fresh(expect):
    expect.text(_C().server_binary_verdict(so_mtime=100, postmaster_epoch=200),
                "fresh", "the server started after the .so was installed")


def test_equal_timestamps_are_fresh_not_predates(expect):
    """The exact boundary. A one-second-resolution mtime and a start time in the
    same second must not read as stale, or every fast run refuses itself."""
    expect.text(_C().server_binary_verdict(so_mtime=100, postmaster_epoch=100),
                "fresh", "same second is not stale")


def test_an_unreadable_side_is_unknown_not_fresh(expect):
    """`unknown` never reads as `fresh` anywhere in this harness."""
    expect.text(_C().server_binary_verdict(so_mtime=None, postmaster_epoch=100),
                "unknown", "no .so mtime")
    expect.text(_C().server_binary_verdict(so_mtime=100, postmaster_epoch=None),
                "unknown", "no postmaster start time")
    expect.text(_C().server_binary_verdict(so_mtime="x", postmaster_epoch="y"),
                "unknown", "non-numeric epochs")


# ---------------------------------------------------------------------------
# TWO FINDINGS FROM @linuxhikerpm, both reproduced against the branch before
# these arms were written. They are infrastructure guarantees rather than
# corpus coverage: the first runs stale code under a FRESH verdict, and the
# second leaves state behind precisely on failed setup, where repeated runs
# need isolation most.


def _tree_with_objstore(tmp_path, name):
    """A tree shaped like this repository's: src/ plus a SEPARATELY built
    module the top-level Makefile reaches by recursion."""
    tree = tmp_path / name
    (tree / "src").mkdir(parents=True, exist_ok=True)
    (tree / "src" / "columnar.c").write_text("int a = 1;\n")
    (tree / "objstore").mkdir(parents=True, exist_ok=True)
    (tree / "objstore" / "module.c").write_text("int b = 1;\n")
    (tree / "objstore" / "Makefile").write_text("all:\n\ttrue\n")
    (tree / "Makefile").write_text("all:\n\t$(MAKE) -C objstore\n")
    (tree / "test").mkdir(parents=True, exist_ok=True)
    (tree / "test" / "lib.sh").write_text(
        "pgc_build_and_install() { return 0; }\n")
    return tree


def test_the_fingerprint_covers_a_separately_built_module(tmp_path, expect):
    """An objstore/ edit must move the fingerprint.

    Reproduced by @linuxhikerpm on `5f3dedb`: `source_fingerprint` read
    `src/*` and the top-level Makefile only, so editing the recursed module
    left the hash unchanged --

        objstore_before=2799803eaeac objstore_after=2799803eaeac
        builds=1 second=already-built

    -- and the second run certified a stale module as current. This is the
    Python form of the gap #898 closes in `test/lib.sh`, and #897 carries an
    INDEPENDENT implementation, so rebasing #898 would not have fixed it.
    """
    tree = _tree_with_objstore(tmp_path, "fp")
    before = source_fingerprint(tree)
    # Not a premise (#1339): source_fingerprint is the code under test, so a change to
    # it can redden this.
    expect.text(repr(before is None), "False",
                "the tree fingerprints at all")

    (tree / "objstore" / "module.c").write_text("int b = 2;\n")
    after = source_fingerprint(tree)
    expect.differ(after, before,
                  "editing a separately built module moves the fingerprint")

    (tree / "objstore" / "module.c").write_text("int b = 1;\n")
    expect.text(source_fingerprint(tree), before,
                "and restoring it restores the fingerprint")


def test_an_objstore_edit_forces_a_second_build(tmp_path, expect):
    """The consequence, end to end, which is what the finding was about."""
    tree = _tree_with_objstore(tmp_path, "fp2")
    pgc, libdir = _answerable(tmp_path, "objstore")
    lock = str(tmp_path / "lock_obj")
    calls = []
    counting = _installs_into(libdir, calls)

    build_once(tree, str(pgc), "18", lock_path=lock, runner=counting)
    build_once(tree, str(pgc), "18", lock_path=lock, runner=counting)
    # Not a premise (#1339): build_once's skip is the behaviour under test.
    expect.num(len(calls), 1, "an unchanged tree builds once")

    (tree / "objstore" / "module.c").write_text("int b = 2;\n")
    build_once(tree, str(pgc), "18", lock_path=lock, runner=counting)
    expect.num(len(calls), 2, "an edited objstore module builds again")


def test_make_cluster_leaves_nothing_behind_when_setup_fails(tmp_path, expect):
    """make_cluster owns its temporary tree until it successfully returns.

    Reproduced by @linuxhikerpm: `root` is created by `mkdtemp` and then
    `Cluster()`, `initdb()`, `start()` and `is_ours()` run with no cleanup
    guard --

        make_cluster_error=RuntimeError
        new_roots=1 leaked=['/tmp/pgc-pytest-777-h3phhtxc']

    -- and `conftest.py` cannot clean it, because the tuple assignment
    `cluster, root = make_cluster(...)` never completes when it raises. The
    handled `is_ours()` path leaked too: it stopped the cluster and left the
    directory.
    """
    # THE DIRECTORY make_cluster CREATED, recorded rather than globbed for (#1339).
    # A glob of /tmp/pgc-pytest-* missed every tree when TMPDIR points elsewhere,
    # which is where mkdtemp puts them, so a leak there passed as "none".
    made = []
    real_mkdtemp = tempfile.mkdtemp

    def recording(*a, **k):
        made.append(real_mkdtemp(*a, **k))
        return made[-1]

    # NARROW, and named rather than caught broadly. The broad-except guard that
    # arrives with the vacuity inventory refuses `except Exception` here and is
    # right to: after one failed statement psycopg raises for every later one, so
    # a broad catch hides the real error and all its successors. A missing
    # pg_config raises FileNotFoundError out of the subprocess layer; anything
    # else escapes and fails the test loudly, which is what should happen to an
    # error this arm did not predict.
    raised = "no"
    tempfile.mkdtemp = recording
    try:
        make_cluster("/nonexistent/bin/pg_config", "gw77")
    except (FileNotFoundError, RuntimeError, OSError):
        raised = "yes"
    finally:
        tempfile.mkdtemp = real_mkdtemp

    expect.text(raised, "yes", "premise: setup really failed, so the arm is not vacuous")
    expect.num(len(made), 1, "premise: make_cluster created its tree before it failed")
    leaked = [d for d in made if pathlib.Path(d).exists()]
    expect.text(", ".join(leaked) or "none", "none",
                "a failed make_cluster leaves no directory behind")


def test_the_cleanup_guard_has_the_shape_the_leak_needs(expect):
    """The arm above proves the tree is gone. It cannot prove WHY, and three of
    the four properties that make the guard work are invisible to it.

    `make_cluster` fails one way in that test: a missing `pg_config`. The guard
    also has to survive a KeyboardInterrupt, stop a postmaster it already started,
    and re-raise rather than return None. Each needs a different failure to
    observe, and two of them cannot be provoked from a test at all -- you cannot
    deliver SIGINT into `initdb` reliably, and a cluster that started is a cluster
    this arm would then have to stop.

    So these are SOURCE checks, and they are labelled as such rather than sold as
    behavioural. They moved here from `test/selftest/380`, which read this file as
    text across the harness boundary. A shell part can pin this text; what it
    cannot do is what the arm above does. Reading our own module is not a
    cross-harness reference, so the property lives where its subject lives.
    """
    import inspect
    body = inspect.getsource(make_cluster)

    # Cut to the handler. These shapes occur elsewhere in the module, and a check
    # over the whole file reports a guard present that lives in another function.
    expect.at_least(len(body.splitlines()), 15,
                    "premise: the source of the function itself was read")

    for fragment, name in (
        ("except BaseException:",
         "the handler catches BaseException, so an interrupt cleans up too"),
        ("cluster.stop()",
         "and it stops a partially started cluster"),
        ("shutil.rmtree(root",
         "and it removes the tree"),
    ):
        expect.num(body.count(fragment), 1, name)

    # The bare re-raise, anchored to a line: `raise` appears inside this function
    # in the is_ours() refusal too, as `raise RuntimeError(...)`, and a substring
    # count would read that as the re-raise and pass with the re-raise deleted.
    bare = [ln for ln in body.splitlines() if ln.strip() == "raise"]
    expect.num(len(bare), 1,
               "and the original error is re-raised rather than swallowed")


def test_this_module_keeps_no_private_fingerprint(expect):
    """One implementation, in `test/pgc_fingerprint.py`, since #907.

    A second copy reappearing in a caller is the defect rather than a detail: it
    agrees with the shared one until it does not, and nothing says when that was.
    Also a source check, and also moved out of `test/selftest/380`.
    """
    import pgc_cluster
    src = pathlib.Path(inspect.getsourcefile(pgc_cluster)).read_text(encoding="utf-8")
    expect.at_least(len(src), 200, "premise: the module's own source was read")
    # Every digest and every source walk, not one spelling of each: a private copy
    # hashed with sha256, or walking with os.walk, passed when only "hashlib.md5" and
    # one glob were refused (#1339). pgc_cluster calls no hashlib function at all,
    # and its one rglob is the chown walk over a datadir, not a source walk.
    for fragment, name in (
        ("hashlib.", "no private digest of any kind"),
        ('glob("*/Makefile")', "and no private build-directory walk"),
        ("os.walk(", "and no private directory walk by os.walk"),
        ('rglob("*.c")', "and no private walk over source files"),
    ):
        expect.num(src.count(fragment), 0, name)


# ---------------------------------------------------------------------------
# THE PYTEST TWIN of test/selftest/340's stamp arms, owed under jd's rule of
# 2026-09-23... 2026-09-09: every test written twice. The .sh half could not
# have one until test/pytest/ existed on main, which it now does (#897).
#
# These drive the SHELL functions through bash rather than reimplementing them,
# for the reason that keeps being proved this week: a second implementation of
# one idea drifts, and the drift is invisible until someone diffs the two.


# The tree this corpus belongs to, derived the same way conftest.py derives it.
SRCDIR = pathlib.Path(__file__).resolve().parents[2]


def _sh(expr):
    """Evaluate one `lib.sh` expression against THIS tree, and return its stdout.

    THE TREE IS NOT A PARAMETER, and it used to look like one. This took `srcdir`,
    every caller passed a fixture tree, and the body sourced the module-global
    `SRCDIR` regardless -- so nine call sites read as "evaluate against this tree"
    while the tree was discarded (#933).

    It could not have been otherwise: the functions under test live in `lib.sh`, and
    no fixture tree contains one. Measured -- honouring the parameter gives rc=1 and
    `No such file or directory`, so every arm would have measured a failed `source`
    instead of the function. The expressions that DO need the fixture interpolate it
    themselves, which is why dropping the parameter changes no behaviour.
    """
    script = f'. "{SRCDIR}/test/lib.sh" || exit 1; {expr}'
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    return p.stdout.strip(), p.returncode


# ---- the fingerprint machinery, called where it LIVES ---------------------------
#
# These used to go python -> bash -> test/lib.sh -> python3 -> test/pgc_fingerprint.py.
# The last step is the ONE implementation (#907); the two before it are lib.sh's thin
# wrapper, so the round trip made every one of these arms a test of the shell harness
# as well as the module. That is the coupling CONTEXT.md's independence rule names: a
# pytest test that drives `test/lib.sh` is the first measurement wearing a Python
# wrapper, so it agrees with the shell by construction and can never report it wrong.
#
# Calling the module is not "testing a different thing". Measured on a fixture tree,
# before any arm was converted: the fingerprint is byte-identical through both paths,
# the manifest is byte-identical line for line, and both agree across LC_ALL=C,
# C.UTF-8 and en_US.UTF-8. What is gone is the wrapper, not the subject.
#
# WHAT STILL GOES THROUGH bash, and why, is listed in the module docstring: the arms
# whose subject IS lib.sh's own behaviour rather than the module's computation.


_FP_MODULE = pathlib.Path(__file__).resolve().parent.parent / "pgc_fingerprint.py"


def _fp_of(tree):
    """The fingerprint of a tree, from the one implementation.

    `""` rather than None for "could not be computed", which is the shape these arms
    already compared against when they read it out of the shell.
    """
    return source_fingerprint(tree) or ""


def _mf_of(tree):
    """The manifest of a tree, from the one implementation.

    Returned RAW, so None -- the module's "a digest failed" -- reaches the arm and
    breaks it loudly instead of being smoothed into an empty manifest. That
    distinction is the module's whole reason for returning None, and an `or ""` here
    would be this file re-creating the defect the module exists to refuse.
    """
    return source_manifest(tree)


def _fp_cli(tree, user="", env=None):
    """The fingerprint, from the module's own CLI in a separate process.

    OUT OF PROCESS because two properties need it: reading as an unprivileged user
    (root ignores `chmod 000`, so an in-process call cannot see a denied read), and
    running under a different locale. Neither needs the shell harness -- the module
    ships the CLI that `test/lib.sh` itself invokes, so this is the same entry point
    lib.sh uses with lib.sh taken out of the path.

    Its contract, which is why the arms below read the same values they did through
    bash: `fingerprint DIR` prints the hash and exits 0, and prints EMPTY when a digest
    could not be read. lib.sh's wrapper passes that through unchanged.
    """
    argv = [sys.executable, str(_FP_MODULE), "fingerprint", str(tree)]
    scratch = None
    if user:
        # THE UNPRIVILEGED USER MUST REACH THE INTERPRETER AND THE MODULE, not only
        # the tree. A checkout under a 0750 home, or a venv under a 0700 directory,
        # left both unreachable: the read failed with EACCES before it began, the
        # premise "the tree fingerprints at all" read empty, and the arm reported a
        # product failure that was the box's permissions (#1337, measured as root on
        # a 0750 $HOME). The module is stdlib-only, so a copy of it in a traversable
        # directory and the base interpreter are the same entry point.
        scratch = pathlib.Path(tempfile.mkdtemp(prefix="pgc-fpmod-"))
        scratch.chmod(0o755)
        mod = scratch / _FP_MODULE.name
        shutil.copyfile(_FP_MODULE, mod)
        mod.chmod(0o644)
        base = getattr(sys, "_base_executable", None) or sys.executable
        argv = ["runuser", "-u", user, "--", base, str(mod), "fingerprint", str(tree)]
    e = dict(os.environ)
    if env:
        e.update(env)
    try:
        p = subprocess.run(argv, capture_output=True, text=True, env=e)
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
    return p.stdout.strip(), p.returncode


def _tree_with_module(tmp_path, name):
    t = tmp_path / name
    (t / "src").mkdir(parents=True, exist_ok=True)
    (t / "objstore").mkdir(parents=True, exist_ok=True)
    (t / "src" / "a.c").write_text("int a;\n")
    (t / "objstore" / "b.c").write_text("int b;\n")
    (t / "objstore" / "Makefile").write_text("all:\n\ttrue\n")
    (t / "Makefile").write_text("all:\n\t$(MAKE) -C objstore\n")
    (t / "pgcolumnar.control").write_text("x\n")
    return t


def test_moving_bytes_between_files_moves_the_shell_fingerprint(tmp_path, expect):
    """`xargs -0 cat | md5sum` could not see a repartition."""
    t = _tree_with_module(tmp_path, "rp")
    before = _fp_of(t)
    expect.at_least(len(before), 12, "premise: the tree fingerprints at all")
    (t / "src" / "a.c").write_text("int a;\nint b;\n")
    (t / "objstore" / "b.c").write_text("")
    after = _fp_of(t)
    expect.text(str(after != before), "True",
                "moving bytes between files moves the fingerprint")


def test_the_two_fingerprint_implementations_cover_the_same_inputs(tmp_path, expect):
    """THE PROPERTY THE TWO IMPLEMENTATIONS MUST SHARE, and the one they did not.

    `source_fingerprint` in pgc_cluster.py says in its own docstring that it uses
    "the same input set as pgc_source_fingerprint in test/lib.sh". It did not:
    the shell hashes each build directory's `*.c`, `*.h` AND `Makefile`, while
    the Python read only `*.c` and `*.h` there. Editing `objstore/Makefile` --
    which changes how that module builds -- moved the shell hash and not the
    Python one, so `build_once` certified a stale module as current:

        baseline                    shell=45be41a5c47b  python=bea88c7d79ca
        objstore/Makefile edited    shell=cfb8f4553041  python=bea88c7d79ca

    That is @linuxhikerpm's finding one layer over: they found the .c files
    missing from the Python side, and the Makefiles were still missing after it
    was fixed.

    THEY ARE NOW ONE IMPLEMENTATION (#907), so the requirement has strengthened
    from "the same edit moves both" to "both are the same value". That is worth
    asserting rather than merely allowing: equality is what makes the shell's
    stamp readable by the Python harness and back, and if someone reintroduces a
    private copy on either side this arm reddens on the first edit rather than on
    the first edit that happens to diverge.
    """
    t = _tree_with_module(tmp_path, "cover")
    for edit, path, body in (
        ("a source file", t / "src" / "a.c", "int a = 2;\n"),
        ("a module source", t / "objstore" / "b.c", "int b = 2;\n"),
        ("a module Makefile", t / "objstore" / "Makefile", "all:\n\ttrue # x\n"),
        ("the top-level Makefile", t / "Makefile", "all:\n\t$(MAKE) -C objstore # x\n"),
        ("the control file", t / "pgcolumnar.control", "y\n"),
    ):
        sh_before, _ = _sh(f'pgc_source_fingerprint "{t}"')
        py_before = source_fingerprint(t)
        old = path.read_text()
        path.write_text(body)
        sh_after, _ = _sh(f'pgc_source_fingerprint "{t}"')
        py_after = source_fingerprint(t)
        path.write_text(old)
        expect.text(f"{sh_after != sh_before} {py_after != py_before}", "True True",
                    f"editing {edit} moves both fingerprints")
        expect.text(f"{sh_before} {sh_after}", f"{py_before} {py_after}",
                    f"and one implementation gives one value, editing {edit}")


# ---------------------------------------------------------------------------
# THE TWIN of test/selftest/340's fingerprint-integrity arms. Two observers, one
# implementation: these drive the real shell functions through bash rather than
# reimplementing them, for the reason four fingerprint defects in one day proved
# — a second implementation of one idea drifts, and the drift is invisible until
# someone diffs the two.


def _sh_fp(expr, path_env=None, env=None):
    """Evaluate a lib.sh expression, optionally with a stubbed PATH."""
    script = f'. "{SRCDIR}/test/lib.sh" || exit 1; {expr}'
    e = dict(os.environ)
    if env:
        e.update(env)
    if path_env:
        e["PATH"] = path_env + ":" + e.get("PATH", "")
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=e)
    return p.stdout.strip(), p.returncode


def _fp_tree(tmp_path, name="t"):
    t = tmp_path / name
    (t / "src").mkdir(parents=True, exist_ok=True)
    (t / "objstore").mkdir(parents=True, exist_ok=True)
    (t / "src" / "a.c").write_text("int a;\n")
    (t / "src" / "b.c").write_text("int b;\n")
    (t / "src" / "c.c").write_text("int c;\n")
    (t / "objstore" / "m.c").write_text("int m;\n")
    (t / "objstore" / "Makefile").write_text("all:\n\ttrue\n")
    (t / "Makefile").write_text("all:\n\ttrue\n")
    (t / "pgcolumnar.control").write_text("x\n")
    return t


def _unprivileged_user():
    """A user that is not root, or None.

    THE MECHANISM HAD TO CHANGE WHEN THE IMPLEMENTATION DID. These arms used to
    stub `md5sum` on PATH, because the shell forked it once per file. The digest
    is now hashlib inside test/pgc_fingerprint.py, which no PATH can reach, so a
    stub would have left both arms passing while testing nothing -- the exact
    shape this suite exists to refuse.

    A real read failure needs a real reader who is denied, and root is denied
    nothing: chmod 000 is invisible to it. Measured, before this was written:

        as root      : 28a7149e07ae   <- reads the mode-000 file anyway
        as postgres  : ''             <- the failure the arm needs
    """
    if os.geteuid() != 0:
        return ""               # already unprivileged; read in this process
    for name in ("postgres", "nobody"):
        try:
            pwd.getpwnam(name)
            return name
        except KeyError:
            continue
    return None


def _readable_tree(name):
    """A fingerprintable tree an unprivileged user can traverse.

    Not under tmp_path: pytest's directories are mode 0700 and owned by the user
    running the session, so a second user cannot walk into them and every arm
    below would fail for the wrong reason.
    """
    root = pathlib.Path(tempfile.mkdtemp(prefix="pgc-fpfail-"))
    t = root / name
    (t / "src").mkdir(parents=True)
    for n, body in (("a.c", "int a;\n"), ("b.c", "int b;\n"), ("c.c", "int c;\n")):
        (t / "src" / n).write_text(body)
    (t / "Makefile").write_text("all:\n\ttrue\n")
    (t / "pgcolumnar.control").write_text("x\n")
    for d in (root, t, t / "src"):
        d.chmod(0o755)
    for f in t.rglob("*"):
        if f.is_file():
            f.chmod(0o644)
    return root, t


def test_a_failed_digest_yields_no_fingerprint_rather_than_a_wrong_one(expect):
    """A digest that FAILED must not look like one that succeeded.

    The shell substituted an EMPTY digest for a failed md5sum and returned rc=0,
    so one failed read among many silently changed the whole hash and the
    function gave three different confident answers for one unchanged tree.
    """
    user = _unprivileged_user()
    if user is None:
        expect.cannot_run("MISSING_DEPENDENCY",
                          "no non-root user to read as; root ignores chmod 000")
        return
    root, t = _readable_tree("t")
    try:
        base, _ = _fp_cli(t, user=user)
        expect.at_least(len(base), 12, "premise: the tree fingerprints at all")

        # Premise for the mechanism itself: the unprivileged reader must agree
        # with a privileged one while nothing is denied, or the arm below would
        # be measuring the user switch rather than the failure.
        mine, _ = _fp_cli(t)
        expect.text(base, mine, "premise: the unprivileged read agrees while readable")

        for name in ("b.c", "c.c"):
            f = t / "src" / name
            f.chmod(0o000)
            got, _ = _fp_cli(t, user=user)
            f.chmod(0o644)
            expect.text(got or "empty", "empty",
                        f"an unreadable {name} yields no fingerprint, not a wrong one")

        after, _ = _fp_cli(t, user=user)
        expect.text(after, base, "control: and the tree fingerprints again once readable")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_one_tree_hashes_one_way_however_the_path_is_spelled(tmp_path, expect):
    """`${f#"$dir"/}` strips a prefix that must match character for character."""
    t = _fp_tree(tmp_path, "s")
    link = tmp_path / "s_link"
    link.symlink_to(t)
    plain = _fp_of(t)
    expect.at_least(len(plain), 12, "premise: the fixture fingerprints at all")
    for label, spelling in (
        ("a trailing slash", f"{t}/"),
        ("a /./ segment", f"{t}/./"),
        ("a /src/.. segment", f"{t}/src/.."),
        ("a symlink", str(link)),
    ):
        got = _fp_of(spelling)
        expect.text(got, plain, f"{label} hashes the same tree the same way")
    _cwd = os.getcwd()
    try:
        os.chdir(t)
        rel = _fp_of(".")
    finally:
        os.chdir(_cwd)
    expect.text(rel, plain, "a relative path hashes the same tree the same way")


def test_the_fix_does_not_rebaseline_stamps_already_on_disk(tmp_path, expect):
    """A COMPATIBILITY assertion, not a tidiness one.

    Capturing the per-file lines to inspect them strips the trailing newline the
    old straight-pipe into md5sum included. Without restoring it the same
    unchanged tree hashes differently before and after the fix, every stamp on
    disk reads `stale`, and a fix for false FATALs becomes a false FATAL for
    everyone holding a built worktree. The matrix cannot catch this: it copies a
    fresh tree and re-stamps every run.
    """
    t = _fp_tree(tmp_path, "c")
    previous = r'''
_prev() { local dir="$1" d
  { while IFS= read -r d; do [ -n "$d" ] || continue
      find "$d" -maxdepth 1 -type f \( -name '*.c' -o -name '*.h' -o -name 'Makefile' \) -print0 2>/dev/null
    done < <(pgc_source_build_dirs "$dir")
    find "$dir" -maxdepth 1 -type f \( -name 'Makefile' -o -name '*.control' -o -name '*.sql' \) -print0 2>/dev/null
  } | sort -z | while IFS= read -r -d '' _f; do
      printf '%s %s\n' "${_f#"$dir"/}" "$(md5sum < "$_f" 2>/dev/null | cut -d' ' -f1)"
    done | md5sum | cut -c1-12; }
'''
    now, _ = _sh_fp(f'pgc_source_fingerprint "{t}"')
    before, _ = _sh_fp(previous + f'_prev "{t}"')
    expect.text(now, before,
                "the fixed fingerprint equals what the previous implementation produced")


def test_the_fingerprint_still_moves_on_a_real_change(tmp_path, expect):
    """Without this the spelling arms are vacuous.

    "Every spelling agrees" is satisfied perfectly by a fingerprint that ignores
    its input, so the set needs one arm proving the hash still moves.
    """
    t = _fp_tree(tmp_path, "m")
    before = _fp_of(t)
    (t / "src" / "a.c").write_text("int a = 2;\n")
    after = _fp_of(t)
    expect.text(str(after != before), "True", "a real content change moves the fingerprint")
    (t / "src" / "a.c").write_text("int a;\n")
    restored = _fp_of(t)
    expect.text(restored, before, "and restoring the content restores it")


def test_a_tree_with_nothing_hashable_reports_no_fingerprint(tmp_path, expect):
    """The hash of an empty stream is a stable, comparable value: two empty
    trees would have "matched".

    THE PREMISE IS LOAD-BEARING AND WAS MISSING. This arm asserts an EMPTY
    result, and an empty result is also what a harness that cannot run at all
    produces: point the helper at a `lib.sh` that does not exist and stdout is
    `''`, so `got or "empty"` is `"empty"` and the arm passes green over a
    completely broken tree. That is the same shape as the `expect.refusal`
    defect @OffgridwithJD found on main -- a failure that produces exactly the
    value the test expects. The populated-tree premise below fails first when
    the harness is broken, which is what makes the empty assertion mean
    anything.
    """
    live = _fp_tree(tmp_path, "hollow_premise")
    base = _fp_of(live)
    expect.at_least(len(base), 12,
                    "premise: the same helper returns a fingerprint for a real tree")
    empty = tmp_path / "hollow"
    empty.mkdir()
    got = _fp_of(empty)
    expect.text(got or "empty", "empty", "an unhashable tree yields no fingerprint")


def _mf_tree(tmp_path, name):
    t = tmp_path / name
    (t / "src").mkdir(parents=True, exist_ok=True)
    (t / "objstore").mkdir(parents=True, exist_ok=True)
    (t / "src" / "a.c").write_text("int a;\n")
    (t / "src" / "h.h").write_text("void h(void);\n")
    (t / "objstore" / "m.c").write_text("int m;\n")
    (t / "objstore" / "Makefile").write_text("all:\n\ttrue\n")
    (t / "Makefile").write_text("all:\n\ttrue\n")
    (t / "pgcolumnar.control").write_text("x\n")
    return t


def test_the_manifest_names_what_the_fingerprint_hashed(tmp_path, expect):
    """Twelve hex characters cannot say which file moved.

    Two CI failures reported `source now a735c673b129, binary built from
    6d122a7158d5` and nothing else -- identically, across two branches, two
    majors and two build directories, with the fingerprint fix present in one of
    them. A bare hash made the second occurrence another sample rather than an
    answer.
    """
    t = _mf_tree(tmp_path, "mf")
    out = _mf_of(t)
    lines = [l for l in out.splitlines() if l.strip()]
    expect.num(len(lines), 6, "the manifest names every file the fingerprint hashes")
    shaped = [l for l in lines if re.fullmatch(r"[A-Za-z0-9_./-]+ [0-9a-f]{32}", l)]
    expect.num(len(shaped), 6, "each line is a tree-relative path and a digest")
    expect.num(len([l for l in lines if l.startswith("/")]), 0,
               "the manifest is tree-relative, never absolute")


def test_the_fingerprint_is_the_hash_of_the_manifest(tmp_path, expect):
    """One is defined as the other, so the two cannot drift apart."""
    t = _mf_tree(tmp_path, "mh")
    fp = _fp_of(t)
    via = hashlib.md5((_mf_of(t) + "\n").encode()).hexdigest()[:12]
    expect.text(fp, via, "the fingerprint is the hash of the manifest")


def test_an_added_file_is_named_rather_than_merely_changing_the_hash(tmp_path, expect):
    """The class the two CI failures fall into, and the one a hash cannot report.

    An addition is the only class that explains one deviant value from two
    different build directories: the manifest carries the path RELATIVE to the
    tree, so the same file added under matrix-17 and matrix-18 contributes the
    same line.
    """
    t = _mf_tree(tmp_path, "add")
    before = _mf_of(t)
    (t / "src" / "zz_appeared.c").write_text("int zz;\n")
    after = _mf_of(t)
    gained = set(after.splitlines()) - set(before.splitlines())
    expect.num(len(gained), 1, "exactly one manifest line appears")
    expect.text(sorted(gained)[0].split()[0], "src/zz_appeared.c",
                "and it names the file that appeared")


def test_no_selftest_part_writes_into_the_live_source_tree(expect):
    """A suite that runs beside others must not write into the tree they read.

    `test/selftest/340` used to write `objstore/.pgc_fingerprint_probe.c` into
    $PGC_SRCDIR to prove that a new file under a recursed directory moves the
    fingerprint. `harness_selftest` runs IN the matrix, so at PGC_JOBS=4 it
    created that file in the shared build directory while sibling suites
    fingerprinted concurrently, and whichever sampled inside the window reported

        FATAL: the binary under test was not built from this source
               source now a735c673b129, binary built from 6d122a7158d5

    against a tree that was correct. Four pull requests and two wrong diagnoses
    before @linuxhikerpm found it by reading the suite.

    WHY THIS IS A STATIC SCAN AND NOT A BEFORE/AFTER RUN. I wrote the behavioural
    version first: fingerprint the tree, run the suite, fingerprint again. It
    CANNOT CATCH THIS DEFECT. The probe was created and `rm -f`'d inside the same
    suite, so the tree is byte-identical by the time the run ends and the
    comparison passes. The damage is done to whoever samples DURING the window,
    and an after-the-fact observer is blind to it by construction. Sampling
    concurrently instead would make the arm racy -- it would pass whenever the
    timing missed. So the observable property is the one in the source: no part
    directs a write at the live tree.

    ITS LIMIT, stated because I have spent today objecting to guards that catch
    one spelling: this recognises a redirection whose target mentions the tree
    root variables the parts actually use. A write reaching the tree by some other
    route -- a `cp` destination, a path assembled elsewhere -- would evade it. The
    `.sh` half asserts the concrete probe path is outside the tree and reddens
    with `got [INSIDE ...]`; between them they cover this instance and the obvious
    generalisations of it, not every conceivable one.
    """
    parts = sorted((SRCDIR / "test" / "selftest").glob("*.sh"))
    expect.at_least(len(parts), 5, "premise: the selftest parts were found")

    def offenders_in(text):
        """Redirections that land in the live tree, following one indirection.

        The real defect is written in two steps -- `_bd_probe="$_bd_root/..."`
        and then `printf ... > "$_bd_probe"` -- so a scan that only looks for the
        tree root INSIDE a redirection target misses it entirely. That was this
        arm's first version, and it passed against the reverted defect.
        """
        root = r"(?:PGC_SRCDIR|_bd_root)"
        tainted = set(re.findall(r'^\s*([A-Za-z_][A-Za-z0-9_]*)=\"?\$\{?' + root + r'\b',
                                 text, re.M))
        names = "|".join([root] + sorted(re.escape(t) for t in tainted))
        redirect = re.compile(r'>\s*"?\$\{?(?:' + names + r')\b')
        found = []
        for n, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if redirect.search(line):
                found.append(n)
        return found

    bad = []
    for part in parts:
        bad += [f"{part.name}:{n}" for n in offenders_in(part.read_text())]
    expect.text(", ".join(bad) or "none", "none",
                "no selftest part directs a write at the live source tree")

    # PREMISES: the scan must be able to see each shape it claims to cover, or it
    # passes on a pattern that matches nothing -- the shape it exists to refuse.
    expect.num(len(offenders_in('printf x > "$PGC_SRCDIR/objstore/p.c"\n')), 1,
               "premise: a direct write at the tree root is seen")
    expect.num(len(offenders_in('_p="$_bd_root/objstore/p.c"\nprintf x > "$_p"\n')), 1,
               "premise: and the INDIRECT form, which is the defect's own shape")
    expect.num(len(offenders_in('_p="$_bd_copy/objstore/p.c"\nprintf x > "$_p"\n')), 0,
               "control: a write into a COPY is not an offender")


def test_one_tree_hashes_one_way_however_the_locale_is_set(expect):
    """The defect the single implementation removed on the way.

    The shell sorted its manifest with `sort -z`, which uses LOCALE COLLATION,
    and no locale is pinned anywhere in this harness. So the same tree
    fingerprinted differently depending on whose machine it was:

        LC_ALL=C            6d122a7158d5
        LC_ALL=en_US.UTF-8  0b59bd75fa4f

    en_US.UTF-8 is a common desktop default. A developer stamping a tree there
    and a CI runner reading it under C.UTF-8 disagree, and the disagreement is a
    FATAL naming a stale binary against a tree that is perfectly clean -- the
    exact false FATAL this controller exists to prevent, arriving from the
    environment rather than from the source.

    The filenames matter: `_` and `-` are what the two collations order
    differently, and `columnar_arrow.c` beside `columnar-arrow.c` is not a
    contrived pair in this tree.
    """
    root = pathlib.Path(tempfile.mkdtemp(prefix="pgc-locale-"))
    try:
        t = root / "t"
        (t / "src").mkdir(parents=True)
        for n in ("columnar_arrow.c", "columnar-arrow.c", "columnarXarrow.c",
                  "Columnar.c", "columnar.c"):
            (t / "src" / n).write_text(f"int x; /* {n} */\n")
        (t / "Makefile").write_text("all:\n\ttrue\n")
        (t / "pgcolumnar.control").write_text("x\n")

        available = subprocess.run(["locale", "-a"], capture_output=True,
                                   text=True).stdout.lower()
        wanted = [l for l in ("c", "c.utf8", "en_us.utf8") if l in available]
        if len(wanted) < 2:
            expect.cannot_run("MISSING_DEPENDENCY",
                              f"fewer than two locales installed: {wanted}")
            return

        seen = {}
        for loc in wanted:
            got, _ = _fp_cli(t, env={"LC_ALL": loc, "LANG": loc})
            seen[loc] = got
        expect.at_least(len(seen[wanted[0]]), 12,
                        "premise: the tree fingerprints at all")
        expect.num(len(set(seen.values())), 1,
                   f"one tree, one fingerprint, across {len(wanted)} locales: {seen}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_a_symlinked_src_is_skipped_like_any_other_symlinked_build_dir(tmp_path, expect):
    """`src` was the one directory exempt from the module's own symlink rule.

    `build_dirs()` added `root/"src"` unconditionally and then applied
    `if not d.is_dir() or d.is_symlink(): continue` to every other candidate.
    `find -P` does not descend a symlinked directory argument, so the shell this
    module replaced hashed nothing there while the module walked it. Measured on
    a tree whose src/ was a symlink:

        find -P on the symlinked src/   printed nothing
        the module's manifest           src/a.c, src/h.h
        old shell fingerprint           9861a3f1fbd1
        module fingerprint              9eff36abd48e

    A stamp written before the port then read `stale` on a clean tree, which is
    the false FATAL the controller exists to prevent.
    """
    real = tmp_path / "real"
    real.mkdir()
    (real / "a.c").write_text("int a;\n")
    (real / "h.h").write_text("void h(void);\n")
    t = tmp_path / "tree"
    t.mkdir()
    (t / "src").symlink_to(real)
    (t / "Makefile").write_text("all:\n\ttrue\n")
    (t / "pgcolumnar.control").write_text("x\n")

    expect.text(str((t / "src").is_symlink()), "True", "premise: src really is a symlink")
    expect.num(len(list(real.iterdir())), 2,
               "premise: and the target holds sources find would otherwise hash")

    out = _mf_of(t)
    lines = [l for l in out.splitlines() if l.strip()]
    expect.num(len([l for l in lines if l.startswith("src/")]), 0,
               "a symlinked src contributes nothing, as find -P contributes nothing")
    expect.num(len(lines), 2, "so the tree fingerprints from its root files alone")

    # CONTROL: without this, "skip src entirely" would satisfy the arm above.
    real_tree = tmp_path / "realsrc"
    (real_tree / "src").mkdir(parents=True)
    (real_tree / "src" / "a.c").write_text("int a;\n")
    (real_tree / "Makefile").write_text("all:\n\ttrue\n")
    (real_tree / "pgcolumnar.control").write_text("x\n")
    out2 = _mf_of(real_tree)
    expect.num(len([l for l in out2.splitlines() if l.startswith("src/a.c ")]), 1,
               "control: a real src directory is still hashed")

# ---- a helper may not take a parameter it never reads (#933) -------------------
#
# `_sh(srcdir, expr)` took a tree and ignored it: the docstring said "evaluate one
# lib.sh expression against a tree", nine call sites passed a fixture tree, and the
# body sourced the module-global SRCDIR -- the real source tree -- instead.
#
# WHICH READING WAS INTENDED IS A MEASUREMENT, not a judgement, and it settles the
# fix. Every caller passes a tree built by `_tree_with_module` or `_tree_with_source`,
# and none of those contains `test/lib.sh`:
#
#     fixture tree holds: ['Makefile', 'objstore', 'pgcolumnar.control']
#     honouring srcdir:   rc=1, "No such file or directory" -- the source fails
#     sourcing SRCDIR:    rc=0, the function under test runs
#
# So the parameter could never have worked, and the arms mean the real tree. The
# parameter was noise that made nine call sites read as something they were not.
#
# TEST FUNCTIONS ARE EXCLUDED, because their parameters are pytest FIXTURES:
# requesting one has an effect whether or not the body reads it, and three in this
# corpus are legitimately unread.
#
# PYTEST HOOKS ARE EXCLUDED TOO, and that is not a hatch. A hook's signature IS
# pytest's API -- arguments arrive by NAME -- so declaring a parameter you do not read
# is how a hook says which of them it wants. Three of the four the scan found before
# this change were hooks: `pytest_collection_modifyitems(config)`,
# `pytest_xdist_node_collection_finished(node)` and `pytest_sessionfinish(exitstatus)`.
# Only `_sh` was a defect, so the budget was 1 and is now 0.


def _unread_parameters(source, filename="<probe>"):
    """-> [(function, parameter, line)] for helpers that ignore an argument."""
    import ast

    out = []
    for fn in ast.walk(ast.parse(source)):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if fn.name.startswith("test_") or fn.name.startswith("pytest_"):
            continue
        names = [a.arg for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs]
        names = [n for n in names if n not in ("self", "cls")]
        if not names:
            continue
        read = {n.id for n in ast.walk(fn)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        for n in names:
            if n not in read:
                out.append((f"{filename}:{fn.lineno}", fn.name, n))
    return out


def test_the_unread_parameter_scan_finds_one(expect):
    """The positive control, on the exact shape `_sh` had."""
    found = _unread_parameters(
        "def _helper(srcdir, expr):\n"
        "    return run(GLOBAL, expr)\n", "probe.py")
    expect.num(len(found), 1, "a helper ignoring its first argument is found")
    expect.text(found[0][2], "srcdir", "and the parameter is named")


def test_the_unread_parameter_scan_spares_fixtures_and_hooks(expect):
    """The false-positive budget, which is what makes the zero below mean something."""
    for label, src in (
        ("a test function's unread fixture",
         "def test_x(tmp_path, expect):\n    expect.num(1, 1, 'n')\n"),
        ("a pytest hook's unread argument",
         "def pytest_sessionfinish(session, exitstatus):\n    do(session)\n"),
        ("a parameter read only inside an f-string",
         "def _h(tree):\n    return f'{tree}/x'\n"),
        ("a parameter read only in a nested function",
         "def _h(tree):\n    def inner():\n        return tree\n    return inner\n"),
        ("a parameter read only in a comprehension",
         "def _h(items):\n    return [i for i in items]\n"),
    ):
        expect.num(len(_unread_parameters(src)), 0, f"not flagged: {label}")


def test_no_helper_in_this_corpus_takes_a_parameter_it_never_reads(expect):
    """The population. One before this change -- `_sh(srcdir, expr)` -- and none now.

    Corpus-wide rather than this file only: the defect was here, but the class is not.
    """
    import pathlib

    here = pathlib.Path(__file__).parent
    files = sorted(here.glob("*.py"))
    expect.at_least(len(files), 10, "premise: the scan has a corpus to read")
    found = []
    for f in files:
        found += _unread_parameters(f.read_text(encoding="utf-8"), f.name)
    expect.text(repr(found), "[]", "no helper ignores an argument it was handed")
