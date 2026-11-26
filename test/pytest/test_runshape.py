"""The three modes that turn a WHOLE RUN green rather than one test.

Each is measured in VACUITY_MODES.md and each is worth more than the per-assertion
guards, because a single occurrence silently removes many tests at once.
"""
import pathlib
import re



def test_layer_fails_when_a_collected_test_never_reports(pytester, expect):
    """A crashed xdist worker loses its remaining tests and pytest says nothing.

    Measured with --max-worker-restart=0: 8 collected, summary "1 failed, 6 passed",
    and one named test never reported. Counting collected tests cannot see it; only
    reconciling the reported node-ids against the collected ones can.
    """
    pytester.makepyfile(
        """
        import os, signal
        def test_a(expect): expect.num(1, 1, "a")
        def test_b(expect): expect.num(1, 1, "b")
        def test_kills_its_worker(expect):
            os.kill(os.getpid(), signal.SIGKILL)
        def test_d(expect): expect.num(1, 1, "d")
        def test_e(expect): expect.num(1, 1, "e")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity", "-n", "2",
                                "--max-worker-restart=0")
    expect.run_failed(result, "a lost test must fail the run")
    # AND BY RECONCILIATION, COUNTED (#1339). The killed worker fails the run by itself,
    # so the arm above holds with reconciliation removed; only its message says the
    # lost test was found. That was an uncounted fnmatch line; it is an arm now.
    out = result.stdout.str()
    expect.text("named" if "never reported an outcome" in out
                else f"not named: {out.strip().splitlines()[-1:]!r}",
                "named", "and the reconciliation names the test that never reported")


def test_layer_accepts_a_run_where_every_test_reports(pytester, expect):
    """The control: reconciliation must not fire on an honest parallel run."""
    pytester.makepyfile(
        """
        def test_a(expect): expect.num(1, 1, "a")
        def test_b(expect): expect.num(1, 1, "b")
        def test_c(expect): expect.num(1, 1, "c")
        def test_d(expect): expect.num(1, 1, "d")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity", "-n", "2")
    expect.outcomes(result, "an honest parallel run is untouched", passed=4, failed=0)
    # AND IT EXITS 0 (#1339). Reconciliation acts at session finish, on the exit
    # status, not on the outcomes: with it firing on every run, the four tests still
    # passed and the arm above stayed green.
    expect.num(int(result.ret), 0, "and the honest parallel run exits 0")


def test_layer_rejects_a_parametrize_over_an_empty_list(pytester, expect):
    """A corpus glob that matches nothing becomes one 's' and exit 0.

    Measured: pytest generates a single SKIPPED placeholder for an empty argvalues
    list. The suite reads as run and asserted nothing.
    """
    pytester.makepyfile(
        """
        import pytest
        CORPUS = []            # a glob that matched nothing
        @pytest.mark.parametrize("path", CORPUS)
        def test_decode_roundtrip(path, expect):
            expect.num(1, 1, "would run if the corpus were not empty")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.run_failed(result, "an empty parameter set must fail the run")
    result.stderr.fnmatch_lines(["*empty parameter set*"])


def test_layer_accepts_a_parametrize_with_cases(pytester, expect):
    """The control: a real parameter set is untouched."""
    pytester.makepyfile(
        """
        import pytest
        @pytest.mark.parametrize("n", [1, 2, 3])
        def test_cases(n, expect):
            expect.num(n, n, "a real case")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(result, "a real parameter set runs", passed=3, failed=0)


def test_layer_rejects_a_fixture_that_skips(pytester, expect):
    """A session fixture calling pytest.skip() skips every dependent test.

    "The cluster would not start" becomes exit 0. This is the single largest blast
    radius in the inventory: one skip removes an entire suite while it reads green.
    """
    pytester.makepyfile(
        conftest="""
        import pytest
        @pytest.fixture(scope="session")
        def cluster():
            pytest.skip("the cluster would not start")
        """,
        test_uses_cluster="""
        def test_one(cluster, expect): expect.num(1, 1, "one")
        def test_two(cluster, expect): expect.num(2, 2, "two")
        """,
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.run_failed(result, "a fixture that skips must fail the run")
    result.stdout.fnmatch_lines(["*skipped during setup*"])


# A SKIP OR XFAIL IN THE TEST BODY (#1339, pgc_vacuity.py:2578). The setup guard above
# sees only `report.when == "setup"`, and the marker scan only `skip` and `skipif`. A
# body that calls pytest.skip(), pytest.xfail() or pytest.importorskip() raises past
# `pytest_runtest_call`'s guards, and an `xfail` marker turns a failing assertion
# into "xfailed". Each exits 0 with no record of what did not run.


def test_layer_rejects_a_skip_in_the_test_body(pytester, expect):
    """`if major < 18: pytest.skip("old")` removes the test and reads green."""
    pytester.makepyfile(
        """
        import pytest
        def test_old_major(expect):
            pytest.skip("old")
            expect.num(1, 1, "never reached")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.run_failed(result, "a skip in the test body must fail the run")
    result.stdout.fnmatch_lines(["*skipped or xfailed while running*"])


def test_layer_rejects_an_xfail_in_the_test_body(pytester, expect):
    """pytest.xfail() in the body reports "xfailed", which pytest counts as success."""
    pytester.makepyfile(
        """
        import pytest
        def test_flaky(expect):
            pytest.xfail("flaky")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.run_failed(result, "an xfail in the test body must fail the run")
    result.stdout.fnmatch_lines(["*skipped or xfailed while running*"])


def test_layer_rejects_an_importorskip_in_the_test_body(pytester, expect):
    """importorskip skips without passing through expect.cannot_run's closed list."""
    pytester.makepyfile(
        """
        import pytest
        def test_needs_a_module(expect):
            pytest.importorskip("pgc_no_such_module_anywhere")
            expect.num(1, 1, "never reached")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.run_failed(result, "an importorskip in the test body must fail the run")
    result.stdout.fnmatch_lines(["*skipped or xfailed while running*"])


def test_layer_rejects_an_xfail_marker(pytester, expect):
    """xfail_strict catches an XPASS. It does not catch the failure xfail hides.

    A pgColumnar change that makes this count 99 is reported "1 xfailed", rc 0.
    """
    pytester.makepyfile(
        """
        import pytest
        @pytest.mark.xfail(reason="flaky")
        def test_count(expect):
            expect.num(99, 100, "rows")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.run_failed(result, "an xfail marker must fail the run")
    result.stderr.fnmatch_lines(["*carries a bare @pytest.mark.xfail*"])


def test_layer_allows_a_declared_unrunnable_test(pytester, expect):
    """The control and the escape hatch: expect.cannot_run stays available.

    A test that declares itself unrunnable with a reason from the closed list is the
    supported way to not run, and it must not be caught by the fixture-skip guard.
    """
    pytester.makepyfile(
        """
        def test_declares_itself(expect):
            expect.cannot_run("MISSING_DEPENDENCY", "no iceberg endpoint here")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(result, "a declared unrunnable test is allowed", passed=1, failed=0)
    # AND IT EXITS INCOMPLETE, NOT FAILED (#1339). The guards act on the exit status,
    # not the outcomes: with the setup-skip guard catching every test, this run still
    # read 1 passed. 67 is the declared-unrunnable state; 0 would be a silent pass and
    # 1 a refusal.
    from pgc_vacuity import EXIT_INCOMPLETE
    expect.num(int(result.ret), EXIT_INCOMPLETE,
               "and a declared unrunnable run exits incomplete, not failed")


# ---------------------------------------------------------------------------
# DESELECTION IS NOT LOSS.
#
# The reconciliation above compares reported node-ids against collected ones, and
# `pytest_collection_modifyitems` fires BEFORE pytest's own -k and -m filtering has
# removed anything. So every deselected test looked like a test that vanished:
#
#     $ pytest -q test_layer.py -k "refus"
#     1 passed, 15 deselected
#     VACUITY: 15 collected test(s) never reported an outcome ...
#     exit 1
#
# A false red on a healthy run, from the guard whose subject is false greens. It is
# worse than a missed true red: the response to it is to stop using the plugin.
# ---------------------------------------------------------------------------

def test_layer_allows_a_deliberately_selected_subset(pytester, expect):
    """`-k` must not be reported as tests lost.

    Asking for a subset is a deliberate act by whoever typed the command; a test
    lost to a crashed worker is not. `pytest_deselected` is where pytest tells the
    two apart, so it is where the guard learns the difference.
    """
    pytester.makepyfile(
        """
        def test_alpha(expect): expect.num(1, 1, "alpha")
        def test_beta(expect): expect.num(2, 2, "beta")
        def test_gamma(expect): expect.num(3, 3, "gamma")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity", "-k", "alpha")
    expect.outcomes(result, "a -k subset is a healthy run", passed=1, failed=0)
    expect.num(result.ret, 0, "and it exits 0 rather than on the run-shape guard")


def test_layer_allows_an_explicitly_deselected_test(pytester, expect):
    """--deselect reaches the same hook by a different route, so it is pinned too."""
    pytester.makepyfile(
        """
        def test_alpha(expect): expect.num(1, 1, "alpha")
        def test_beta(expect): expect.num(2, 2, "beta")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity",
                                "--deselect", "test_layer_allows_an_explicitly"
                                              "_deselected_test.py::test_beta")
    expect.outcomes(result, "an explicit deselection is a healthy run",
                    passed=1, failed=0, deselected=1)
    expect.num(result.ret, 0, "and it exits 0")


def test_a_run_that_both_deselects_and_loses_a_test_still_fails(pytester, expect):
    """The two must stay distinguishable, or the fix is just a blindfold.

    Subtracting the deselected ids from the collected set is the right fix only if
    the set still holds every test that was genuinely lost. An over-broad
    subtraction would pass this file's other arms and silently retire the guard, so
    this arm deselects AND kills a worker in one run and requires the red.
    """
    pytester.makepyfile(
        """
        import os, signal
        def test_skipme_alpha(expect): expect.num(1, 1, "deselected by -k")
        def test_skipme_beta(expect): expect.num(2, 2, "deselected by -k")
        def test_keep_a(expect): expect.num(1, 1, "a")
        def test_keep_b(expect): expect.num(1, 1, "b")
        def test_keep_kills_its_worker(expect):
            os.kill(os.getpid(), signal.SIGKILL)
        def test_keep_d(expect): expect.num(1, 1, "d")
        def test_keep_e(expect): expect.num(1, 1, "e")
        def test_keep_f(expect): expect.num(1, 1, "f")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity", "-k", "keep", "-n", "2",
                                "--max-worker-restart=0")
    expect.run_failed(result, "a lost test is still caught when others were deselected")
    result.stdout.fnmatch_lines(["*never reported*"])


# ---- WHERE a guard runs decides what the run reports -------------------------
#
# `guard-as-teardown-fixture-still-reports-passed` (VACUITY_MODES.md 3.7). A guard
# implemented as a fixture teardown cannot fail the test it guards: pytest has
# already recorded the call phase as passed, so the refusal arrives as a separate
# ERROR on the same node-id and the test's own outcome stays `passed`. Anything
# counting passes -- `--pgc-expect-tests`, a CI summary, a human reading "N passed"
# -- sees a pass.
#
# This layer's own vacuity guard is in a `pytest_runtest_call` wrapper, which is the
# right place, and these two arms are why that stops being an accident. Measured,
# the same AssertionError raised from each place:
#
#     from a pytest_runtest_call wrapper   1 failed
#     from a fixture teardown              1 passed, 1 error
#
# The second arm is the control. Without it the first passes whatever phase the
# guard is in, because "a vacuous test fails" is true of a correctly-placed guard
# and of no guard at all plus an unrelated failure.


def test_the_vacuity_guard_fails_the_test_rather_than_erroring_beside_it(pytester, expect):
    """A test that concludes nothing must be FAILED, not passed-with-an-error."""
    pytester.makepyfile(
        """
        def test_asserts_nothing(expect):
            pass
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(result, "a test that concludes nothing is failed, not errored",
                    passed=0, failed=1, errors=0)


def test_a_guard_in_a_teardown_would_report_a_pass_which_is_why_it_is_not_there(pytester, expect):
    """The control, and the mode stated as a measurement rather than a warning.

    The body asserts something, so the vacuity guard is satisfied; the refusal comes
    from the teardown. pytest reports the test PASSED and adds an error, which is the
    shape that makes a guard in a teardown unable to fail what it guards.
    """
    pytester.makepyfile(
        """
        import pytest

        @pytest.fixture
        def guard_in_teardown():
            yield
            raise AssertionError("a guard placed in a teardown instead")

        def test_body(guard_in_teardown, expect):
            expect.num(1, 1, "the body itself is fine")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(result, "a teardown refusal leaves the test reported as passed",
                    passed=1, failed=0, errors=1)


def test_two_workers_taking_a_fixture_port_do_not_collide(pytester, monkeypatch, expect):
    """Two xdist workers asking `pick_port` for the same slot get different ports (#1742).

    A twin that starts its own S3 or HTTP fixture server takes its port from
    `pick_port(slot)` in a module fixture. Under `-n 2` a module's tests are split
    across workers, so each worker runs that fixture. On main the port depended on
    the slot only: both workers probed the same free port and the second bind failed
    with `Address already in use` (#1635, #1714, #1721). CI ran the cluster files
    serially, so only the guard job and developers met it.

    The inner tests take their port, wait until both workers have one, and only then
    bind it, so the probe-then-bind race happens every run rather than by chance. A
    subprocess, because xdist's workers are.
    """
    import pathlib
    here = pathlib.Path(__file__).resolve().parent
    meet = pytester.path / "meet"
    meet.mkdir()
    monkeypatch.setenv("PYTHONPATH", f"{pytester.path}:{here}")
    monkeypatch.setenv("PGC_PORT_MEET", str(meet))
    pytester.makepyfile(test_ports="""
        import os, pathlib, socket, time
        from pgc_cluster import pick_port

        MEET = pathlib.Path(os.environ["PGC_PORT_MEET"])

        def _wait_for(suffix, n):
            for _ in range(600):
                if len(list(MEET.glob("*" + suffix))) >= n:
                    return
                time.sleep(0.05)

        def _take(expect, me):
            port = pick_port(27)
            (MEET / (me + ".pick")).write_text(
                os.environ.get("PYTEST_XDIST_WORKER", "none") + " " + str(port))
            _wait_for(".pick", 2)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(("127.0.0.1", port))
                    s.listen()
                    got = "bound"
                except OSError as e:
                    got = "bind failed: " + str(e)
                (MEET / (me + ".tried")).write_text(got)
                _wait_for(".tried", 2)
            expect.text(got, "bound", "this worker alone holds port " + str(port))

        def test_first(expect):
            _take(expect, "first")

        def test_second(expect):
            _take(expect, "second")
        """)
    result = pytester.runpytest_subprocess("-p", "pgc_vacuity", "-p", "no:cacheprovider",
                                           "-n", "2")
    picks = sorted(p.read_text().split() for p in meet.glob("*.pick"))
    workers = sorted({w for w, _ in picks})
    ports = sorted({port for _, port in picks})
    # The premise: the race is only measured if the two picks ran on two workers.
    expect.text(" ".join(workers), "gw0 gw1",
                "premise: the two inner tests ran on two different workers")
    expect.num(len(ports), 2, "the two workers were given two different ports")
    out = result.stdout.str()
    expect.text("green" if result.ret == 0 else f"exit {result.ret}: "
                f"{[l for l in out.splitlines() if 'bind failed' in l or 'FAILED' in l][:4]!r}",
                "green", "and each worker bound its own port, so the inner run is green")


# ---- every slot the corpus asks for fits a worker's block (#1742 review) ----------
#
# A slot that does not fit raises at fixture time, on the cluster job, as every test
# of that file erroring. These two read the corpus instead, so the next twin that asks
# for a slot past the block fails here, in the guard job, by name. Measured before
# the stride was raised: main's twins ask for slots 32 to 35, and a 64-port block
# holds 0 to 31.
_SLOT_CALL = re.compile(r"\bpick_port\(\s*(\d+)\s*[,)]")


def _corpus_slots():
    here = pathlib.Path(__file__).resolve().parent
    found = {}
    for f in sorted(here.glob("*.py")):
        for m in _SLOT_CALL.finditer(f.read_text()):
            found.setdefault(int(m.group(1)), []).append(f.name)
    return found


def test_every_literal_fixture_slot_fits_a_workers_block(expect):
    from pgc_cluster import WORKER_STRIDE
    slots = _corpus_slots()
    expect.at_least(len(slots), 5, "premise: the corpus scan found the fixture slots in use")
    too_big = sorted(f"{s} ({', '.join(sorted(set(files)))})" for s, files in slots.items()
                     if not s * 2 < WORKER_STRIDE)
    expect.text(", ".join(too_big) or "none", "none",
                "every literal pick_port slot fits a worker's block of ports")


def test_the_highest_slot_in_the_corpus_gets_a_port(tmp_path, monkeypatch, expect):
    from pgc_cluster import aux_band, pick_port
    # pick_port reserves the port it returns (#2165). In the user's real directory
    # that reservation would block a port nothing binds, for as long as this
    # process runs, and the pick would depend on what other runs reserved.
    monkeypatch.setenv("PGC_PORT_RESERVE_DIR", str(tmp_path / "reserved"))
    top = max(_corpus_slots())
    lo, hi = aux_band(32768)
    try:
        port = pick_port(top, floor=32768, is_free=lambda p: True, worker_id="gw3")
        reading = "in the band" if lo <= port < hi else f"outside the band: {port}"
    except ValueError as exc:
        reading = f"refused: {exc}"
    expect.text(reading, "in the band",
                f"the highest slot the corpus uses ({top}) gets a port in the AUX band")


# ---- two runs at once never share a fixture port (#2165) ---------------------------
#
# The worker blocks above keep one run's workers apart. Two RUNS at once are another
# matter: run_all_versions runs suites in parallel, and a shell suite and a twin share
# the AUX band. A picker proves a port free and returns it, and nothing holds the port
# until the caller binds it, so two pickers that start from the same port are both
# told it. In CI iceberg_rest_scan and iceberg_rest_vended both got 31732, and the
# scan's arms got HTTP 401 from the vended suite's REST fixture.
#
# Each arm forces that collision: every picker starts at the same port, and nothing
# binds until all of them have picked, which is the probe-then-bind gap held open. On
# main every arm is red. Each pick also sleeps a second between its probe and its
# reservation (PGC_PORT_PICK_DELAY), so without the lock the pickers would all probe
# the port free before any reserved it. The pickers share a reservation directory of their own, so
# the arms neither see nor leave reservations in the user's real one.
_PORTLIB = pathlib.Path(__file__).resolve().parent.parent / "portlib.sh"
_SHELL_PICK = '. "$1"; pgc_pick_free_port "$PGC_AUX_PORT_LO" "$PGC_AUX_PORT_HI" "$2"'
_TWIN_PICK = ("import sys; sys.path.insert(0, sys.argv[1]); from pgc_cluster import pick_port; "
              "print(pick_port(27, worker_id='gw0'))")


def _pick_env(tmp_path, name="reserved"):
    import os
    # PGC_PORT_PICK_DELAY holds each pick's probe-to-reservation gap open for a
    # second, so the pickers overlap there unless the lock serialises them.
    env = dict(os.environ, PGC_PORT_RESERVE_DIR=str(tmp_path / name), PGC_PORT_PICK_DELAY="1")
    env.pop("PYTEST_XDIST_WORKER", None)
    return env


def _shell_picker(env, seed):
    import subprocess
    return subprocess.Popen(["bash", "-c", _SHELL_PICK, "pick", str(_PORTLIB), str(seed)],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _twin_picker(env):
    import subprocess
    import sys
    return subprocess.Popen([sys.executable, "-c", _TWIN_PICK,
                             str(pathlib.Path(__file__).resolve().parent)],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _picked(procs):
    """-> the ports the pickers printed, in order, or their errors."""
    out = []
    for p in procs:
        so, se = p.communicate(timeout=120)
        out.append(so.strip() if p.returncode == 0 and so.strip().isdigit()
                   else f"rc={p.returncode}: {se.strip()[-200:]}")
    return out


def _bind_all(ports):
    """Binds every picked port at once, as the fixtures would: 'bound' or why not."""
    import socket
    held, got = [], []
    try:
        for p in ports:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            held.append(s)
            try:
                s.bind(("127.0.0.1", int(p)))
                got.append("bound")
            except (OSError, ValueError) as e:
                got.append(f"{p}: {e}")
    finally:
        for s in held:
            s.close()
    return " ".join(got)


def test_four_shell_suites_picking_from_one_seed_get_four_ports(tmp_path, expect):
    """Four shell suites pick at once with one seed, so every walk starts at one port."""
    alone = _picked([_shell_picker(_pick_env(tmp_path, f"alone{i}"), 7) for i in range(2)])
    expect.num(len(set(alone)), 1,
               f"premise: two shell pickers that share no reservations take one port: {alone}")
    env = _pick_env(tmp_path)
    ports = _picked([_shell_picker(env, 7) for _ in range(4)])
    expect.num(len(set(ports)), 4, f"four shell suites picking at once get four ports: {ports}")
    expect.text(_bind_all(ports), "bound bound bound bound",
                "and each shell suite binds the port it was given")


def test_two_twin_runs_picking_one_slot_get_two_ports(tmp_path, expect):
    """Two pytest runs (not two workers of one run) ask for one slot of one worker."""
    alone = _picked([_twin_picker(_pick_env(tmp_path, f"alone{i}")) for i in range(2)])
    expect.num(len(set(alone)), 1,
               f"premise: two twin pickers that share no reservations take one port: {alone}")
    env = _pick_env(tmp_path)
    ports = _picked([_twin_picker(env) for _ in range(2)])
    expect.num(len(set(ports)), 2, f"two twin runs picking one slot get two ports: {ports}")
    expect.text(_bind_all(ports), "bound bound", "and each twin run binds the port it was given")


def test_a_shell_suite_and_a_twin_never_share_a_port(tmp_path, expect):
    """The shell's seed 54 starts at the twin's slot 27 of gw0: lo + 54 for both."""
    import os
    import subprocess
    alone = _picked([_shell_picker(_pick_env(tmp_path, "alone0"), 54),
                     _twin_picker(_pick_env(tmp_path, "alone1"))])
    expect.num(len(set(alone)), 1,
               f"premise: the shell's seed 54 and the twin's slot 27 take one port alone: {alone}")
    env = _pick_env(tmp_path)
    ports = _picked([_shell_picker(env, 54), _twin_picker(env)])
    expect.num(len(set(ports)), 2, f"a shell suite and a twin picking at once get two ports: {ports}")
    expect.text(_bind_all(ports), "bound bound", "and each binds the port it was given")
    # The arms above share a directory through PGC_PORT_RESERVE_DIR. Without it, the
    # two harnesses must still pick the same default, or they never see each other.
    plain = {k: v for k, v in os.environ.items() if k != "PGC_PORT_RESERVE_DIR"}
    shell_dir = subprocess.run(["bash", "-c", '. "$1"; pgc_port_reserve_dir', "dir", str(_PORTLIB)],
                               env=plain, capture_output=True, text=True).stdout.strip()
    import pgc_cluster
    saved = os.environ.pop("PGC_PORT_RESERVE_DIR", None)
    try:
        twin_dir = getattr(pgc_cluster, "port_reserve_dir", lambda: "(pgc_cluster has no port_reserve_dir)")()
    finally:
        if saved is not None:
            os.environ["PGC_PORT_RESERVE_DIR"] = saved
    expect.text(twin_dir, shell_dir or "(the shell named no directory)",
                "the twin and the shell reserve ports in the same directory by default")


# ---- a fixture that never says READY stops the run (#2165) -------------------------
#
# Both REST twins start a fixture and wait for its READY through pgc_cluster's
# await_ready. They used to wait 5 s and go on, so their arms read whatever answered
# on the port: in CI another suite's REST fixture, which gave HTTP 401. The twins
# reach await_ready only on the path where READY arrives, so nothing there notices a
# raise turned into a return (review of #2208). These drive it against real
# processes. The never-READY arm passes wait=1, so it takes a second, not 30.
def _fixture(tmp_path, name, code):
    import subprocess
    import sys
    out = tmp_path / f"{name}.out"
    with open(out, "w") as fh:
        proc = subprocess.Popen([sys.executable, "-c", code], stdout=fh, stderr=subprocess.STDOUT)
    return out, proc


def _await(out, proc, what, wait):
    """-> ("returned" or "raised: <message>", seconds taken)."""
    import time
    from pgc_cluster import await_ready
    t0 = time.monotonic()
    try:
        await_ready(out, proc, what, wait=wait)
        got = "returned"
    except RuntimeError as exc:
        got = f"raised: {exc}"
    return got.rstrip(), time.monotonic() - t0


def test_a_fixture_that_exits_without_ready_stops_the_run(tmp_path, expect):
    out, proc = _fixture(tmp_path, "exits",
                         "print('bind: Address already in use', flush=True); raise SystemExit(3)")
    got, took = _await(out, proc, "test fixture on port 1", 20)
    expect.text(got, "raised: the test fixture on port 1 did not start (exited): "
                "bind: Address already in use",
                "a fixture that exits without READY stops the run, naming it")
    expect.text("early" if took < 10 else f"after {took:.1f} s of 20", "early",
                "and the wait ends when the fixture exits, not at the limit")


def test_a_fixture_that_says_ready_late_is_waited_for(tmp_path, expect):
    out, proc = _fixture(tmp_path, "late",
                         "import time; time.sleep(1); print('READY', flush=True); time.sleep(30)")
    try:
        got, _ = _await(out, proc, "late fixture", 10)
    finally:
        proc.kill()
        proc.wait()
    expect.text(got, "returned", "a fixture that says READY late is waited for")


def test_a_fixture_that_never_says_ready_stops_the_run(tmp_path, expect):
    out, proc = _fixture(tmp_path, "silent", "import time; time.sleep(30)")
    try:
        got, _ = _await(out, proc, "silent fixture", 1)
    finally:
        proc.kill()
        proc.wait()
    expect.text(got, "raised: the silent fixture did not start "
                "(still running with no READY after 1 s):",
                "a fixture that never says READY stops the run, naming it")


# ---- how long a reservation holds (#2165, review of #2208) ---------------------------
#
# A reservation holds while the pid it names runs, at any age, and for 120 s after
# the pick once that pid has gone. Each arm learns the port a picker takes alone,
# rewrites that port's reservation with a chosen owner and age, and asks the same
# picker again in the same directory: a held reservation moves the pick off that
# port. Both pickers, since each harness applies the rule itself. The dead owner is
# pid_max + 1, which no process can have. The 60 s and 121 s ages sit either side of
# the 120 s limit with room for a slow second, so a limit cut to 5 s or 2 s fails
# the first arm and one raised past 121 s fails the second.
def _quiet_env(rdir):
    import os
    env = dict(os.environ, PGC_PORT_RESERVE_DIR=str(rdir))
    for k in ("PGC_PORT_PICK_DELAY", "PYTEST_XDIST_WORKER"):
        env.pop(k, None)
    return env


def _reserve(rdir, port, owner, age):
    import os
    import time
    then = time.time() - age
    res = rdir / str(port)
    res.write_text(f"{owner} {int(then)}\n")
    os.utime(res, (then, then))


def _repick(tmp_path, owner, age):
    """-> 'shell: same|moved, twin: same|moved' for a reservation of the port each
    picker takes alone, rewritten with `owner` and `age`."""
    seen = []
    for name, start in (("shell", lambda env: _shell_picker(env, 54)), ("twin", _twin_picker)):
        rdir = tmp_path / f"{name}-{owner}-{age}"
        env = _quiet_env(rdir)
        first = _picked([start(env)])[0]
        if not first.isdigit():
            seen.append(f"{name}: {first}")
            continue
        _reserve(rdir, first, owner, age)
        again = _picked([start(env)])[0]
        seen.append(f"{name}: " + ("same" if again == first else
                                   "moved" if again.isdigit() else again))
    return ", ".join(seen)


def _dead_pid():
    import pathlib as _p
    return int(_p.Path("/proc/sys/kernel/pid_max").read_text()) + 1


def test_a_dead_pickers_reservation_holds_for_sixty_seconds(tmp_path, expect):
    expect.text(_repick(tmp_path, _dead_pid(), 60), "shell: moved, twin: moved",
                "a reservation 60 s old whose picker has gone blocks both pickers")


def test_a_dead_pickers_reservation_lapses_after_the_limit(tmp_path, expect):
    expect.text(_repick(tmp_path, _dead_pid(), 121), "shell: same, twin: same",
                "a reservation 121 s old whose picker has gone blocks neither picker")


def test_a_live_pickers_reservation_holds_at_any_age(tmp_path, expect):
    """The owner is this test's own process, which runs throughout."""
    import os
    expect.text(_repick(tmp_path, os.getpid(), 3600), "shell: moved, twin: moved",
                "a reservation an hour old whose picker still runs blocks both pickers")
