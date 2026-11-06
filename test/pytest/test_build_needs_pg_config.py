"""A build with no pg_config must refuse, not wait on stdin (#1344).

The pytest half of selftest part 630, with its own driver: subprocess and a pipe
this test holds open, not the part's FIFO and timeout(1), so the twins share the
rule and not the mechanism.

The Makefile reads CC out of the server's Makefile.global with a sed whose file is
`$(PG_CONFIG) --pgxs` rewritten. With no usable pg_config that path is empty and sed
reads make's stdin: an open stdin hangs the build, a closed one lets it exit 0
having done nothing. Measured on main at 3677825.

make runs with -n on the real Makefile: the parse, where the probe and the refusal
both live, happens in full, and nothing is built or removed. Needs no cluster and
no driver.
"""

import os
import pathlib
import signal
import subprocess

HERE = pathlib.Path(__file__).resolve().parent
SRCDIR = HERE.parent.parent
LIMIT = 20


def _make_without_pg_config(tmp_path, stdin_open):
    """make -n clean with PG_CONFIG naming nothing.

    stdin is a pipe nobody writes (stdin_open) or /dev/null. Returns (rc, output);
    rc None means it was still running at LIMIT seconds. The process group is
    killed, so the sed in make's $(shell) does not outlive it.
    """
    rd, wr = os.pipe() if stdin_open else (subprocess.DEVNULL, None)
    proc = subprocess.Popen(
        ["make", "-n", "-C", str(SRCDIR), "clean",
         f"PG_CONFIG={tmp_path / 'no-such' / 'pg_config'}"],
        stdin=rd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        start_new_session=True)
    if wr is not None:
        os.close(rd)
    try:
        out, _ = proc.communicate(timeout=LIMIT)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
        rc = None
    finally:
        if wr is not None:
            os.close(wr)
    return rc, out.decode(errors="replace")


def test_with_stdin_open_it_ends_and_names_pg_config(tmp_path, expect):
    expect.text(repr((SRCDIR / "Makefile").is_file()), "True",
                "premise: the selftest knows where the Makefile it drives is")

    # The fixture's own proof that this stdin is one a reader waits on.
    rd, wr = os.pipe()
    try:
        subprocess.run(["cat"], stdin=rd, stdout=subprocess.DEVNULL, timeout=2)
        waited = "no (reached EOF)"
    except subprocess.TimeoutExpired:
        waited = "yes"
    finally:
        os.close(rd)
        os.close(wr)
    expect.text(waited, "yes",
                "premise: a reader of this stdin waits rather than reaching EOF")

    rc, out = _make_without_pg_config(tmp_path, stdin_open=True)
    print(f"-- make with no pg_config: rc {rc}: {' '.join(out.split())[:200]}")
    expect.text("yes" if rc is not None else f"no (still running at {LIMIT}s)", "yes",
                "a make with no usable pg_config ends instead of waiting on stdin")
    # make's own error line, not any line: -n echoes the objstore sub-make's command,
    # which carries PG_CONFIG= whether make refused or not.
    refusal = [ln for ln in out.splitlines() if "***" in ln and "PG_CONFIG" in ln]
    expect.text("yes" if refusal else "no", "yes",
                "and the refusal is make's own error, naming PG_CONFIG")


def test_with_stdin_closed_it_still_refuses(tmp_path, expect):
    # The other face: with stdin closed sed finds EOF, the parse goes on, and make
    # exits 0. A test of its own, so this arm does not ride on the hang.
    rc2, out2 = _make_without_pg_config(tmp_path, stdin_open=False)
    print(f"-- the same make, stdin closed: rc {rc2}")
    # THIS refusal, not any non-zero exit: a Makefile that fails to parse exits 2
    # as well, and would satisfy a bare rc test (#1346 review, jdatcmd).
    errs = [ln for ln in out2.splitlines() if "***" in ln]
    refused = rc2 not in (None, 0) and any("PG_CONFIG" in ln for ln in errs)
    expect.text("yes" if refused else f"no (rc {rc2}: {errs[0] if errs else 'no make error'})", "yes",
                "and with stdin closed it refuses, rather than exiting 0 having done nothing")
