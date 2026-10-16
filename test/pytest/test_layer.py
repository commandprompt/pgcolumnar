"""The vacuity-refusal layer, tested before it exists.

Every test here asserts that the layer REFUSES something bare pytest accepts.
Measured on pytest 9.1.1, all eight of the modes below exit 0 with no plugin
loaded, which is why each test asserts on the INNER run's outcome rather than on
its own arithmetic.
"""

import pytest


def test_layer_rejects_a_test_with_no_assertion(pytester, expect):
    """A test body that concludes nothing must fail, not pass.

    Bare pytest: `1 passed`, exit 0. Measured.
    """
    pytester.makeconftest("pytest_plugins = ['pgc_vacuity']")
    pytester.makepyfile(
        """
        def test_asserts_nothing():
            x = 1 + 1
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(result, "inner run outcomes", failed=1, passed=0)
    result.stdout.fnmatch_lines(["*made no counted assertion*"])


def test_a_counted_assertion_passes(pytester, expect):
    """The positive control. A guard that rejects good tests gets switched off.

    This must pass for the same plugin that fails the test above.
    """
    pytester.makepyfile(
        """
        def test_concludes_something(expect):
            expect.num(2 + 2, 4, "arithmetic still works")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(result, "inner run outcomes", passed=1, failed=0)


def test_layer_rejects_two_empty_results(pytester, expect):
    """Empty compared with empty is the defect that produced issue #418.

    Bare pytest: `2 passed`, exit 0. Measured.
    """
    pytester.makepyfile(
        """
        def test_empty_vs_empty(expect):
            got, want = [], []
            expect.rows(got, want, "two empty results")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(result, "inner run outcomes", failed=1, passed=0)
    result.stdout.fnmatch_lines(["*both sides are empty*"])


def test_layer_allows_an_empty_result_when_declared(pytester, expect):
    """An empty result is a legitimate expectation when the test says so.

    After a bare TRUNCATE the correct end state IS an empty projection, so this
    escape hatch has to exist. It requires a reason, so it cannot become the
    default by being easier to type.
    """
    pytester.makepyfile(
        """
        def test_empty_is_the_point(expect):
            expect.rows([], [], "empty after truncate", allow_empty="table was truncated")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(result, "inner run outcomes", passed=1, failed=0)


def test_layer_rejects_a_self_comparison(pytester, expect):
    """Comparing a value against itself cannot fail, so it asserts nothing."""
    pytester.makepyfile(
        """
        def test_hash_against_itself(expect):
            h = "d41d8cd98f00b204e9800998ecf8427e"
            expect.hash(h, h, "oracle against itself")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(result, "inner run outcomes", failed=1, passed=0)
    result.stdout.fnmatch_lines(["*compared against itself*"])


def test_layer_rejects_a_substring_plan_match(pytester, expect):
    """`"ColumnarScan" in plan` is exactly as wrong as `grep ColumnarScan`.

    The measured provider name is `PgColumnarScan`, so the substring passes while
    asserting the wrong thing. The helper takes a typed field and exact equality.
    """
    pytester.makepyfile(
        """
        PLAN = [{"Plan": {"Node Type": "Aggregate", "Plans": [
                    {"Node Type": "Custom Scan",
                     "Custom Plan Provider": "PgColumnarScan"}]}}]

        def test_substring_is_refused(expect):
            expect.plan_node(PLAN, provider="ColumnarScan")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(result, "inner run outcomes", failed=1, passed=0)
    result.stdout.fnmatch_lines(["*no node whose*"])


def test_layer_matches_the_exact_provider(pytester, expect):
    """The positive control for the plan helper: the real name must match."""
    pytester.makepyfile(
        """
        PLAN = [{"Plan": {"Node Type": "Aggregate", "Plans": [
                    {"Node Type": "Custom Scan",
                     "Custom Plan Provider": "PgColumnarScan"}]}}]

        def test_exact_provider(expect):
            expect.plan_node(PLAN, provider="PgColumnarScan")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(result, "inner run outcomes", passed=1, failed=0)


def test_layer_rejects_a_bare_skip(pytester, expect):
    """A bare skip exits 0 and reports success. Measured: `2 skipped`, exit 0.

    A skip has to name a reason from the closed list, so a suite cannot quietly
    stop testing anything.
    """
    pytester.makepyfile(
        """
        import pytest
        @pytest.mark.skip(reason="not today")
        def test_quietly_gone():
            assert False
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.run_failed(result, "a bare skip must not produce a zero exit")
    # A UsageError is written to stderr, not stdout. Asserting on the wrong stream
    # would have made this test pass on ret alone, which any collection error also
    # satisfies -- so the message assertion is what makes it honest.
    result.stderr.fnmatch_lines(["*bare skip*"])


def test_layer_fails_on_a_collected_count_mismatch(pytester, expect):
    """A filtered or truncated run must not be green.

    Measured: `-k` that matches nothing exits 5, and 5 is routinely treated as
    acceptable by wrappers. Collecting fewer tests than expected is the same
    accident with a zero exit, so the layer compares collected against expected.
    """
    pytester.makepyfile(
        """
        def test_one(expect): expect.num(1, 1, "one")
        def test_two(expect): expect.num(2, 2, "two")
        """
    )
    good = pytester.runpytest("-p", "pgc_vacuity", "--pgc-expect-tests", "2")
    expect.outcomes(good, "the honest run is green", passed=2, failed=0)

    short = pytester.runpytest("-p", "pgc_vacuity", "--pgc-expect-tests", "2",
                               "-k", "test_one")
    expect.run_failed(short, "a run that collected fewer than expected must fail")
    # stderr, not stdout: a UsageError is written there. Getting this wrong twice
    # while building the layer is why every message assertion here names its stream.
    short.stderr.fnmatch_lines(["*collected 1 test*expected 2*"])


def test_layer_refuses_a_zero_expectation(pytester, expect):
    """--pgc-expect-tests 0 would make the guard vacuous, so it is refused."""
    pytester.makepyfile("def test_one(expect): expect.num(1, 1, 'one')")
    result = pytester.runpytest("-p", "pgc_vacuity", "--pgc-expect-tests", "0")
    expect.run_failed(result, "an expectation of zero asserts nothing")
    # AND FOR THAT REASON (#1339). One test collected against an expectation of 0 also
    # fails the count check, so with the zero refusal removed the arm above stayed
    # green. The refusal's own words, on stderr where a UsageError is written, are what
    # only the refusal produces.
    err = result.stderr.str()
    expect.text("refused as zero" if "--pgc-expect-tests 0 would be satisfied by a run "
                "that collected nothing" in err
                else f"not refused as zero: {err.strip().splitlines()[-1:]!r}",
                "refused as zero", "and it is refused because zero asserts nothing")


# ---------------------------------------------------------------------------
# THE THIRD STATE. `cannot_run` declared a test unrunnable and the run reported
# it as a PASS, exit 0: `self.unrunnable` was written and read nowhere. That is
# a write-only field, the same shape selftest 320 polices in the runner ("no
# write-only failure flag survives"), and it made the layer's own escape hatch
# the largest vacuity hole in it -- a bare skip FAILS the run, while the
# supposedly honest alternative greened silently.
#
# lib.sh has kept this state honest since #418: an unrunnable check counts
# toward checks run, is reported separately, and the suite exits
# PGC_EXIT_INCOMPLETE (67) so no runner can call it a pass. These four arms are
# the pytest mirror of selftest 320's four, including which state dominates.


def test_an_unrunnable_test_does_not_leave_the_run_green(pytester, expect):
    """The defect itself. Measured before the fix: `1 passed`, exit 0."""
    pytester.makeconftest("pytest_plugins = ['pgc_vacuity']")
    pytester.makepyfile(
        """
        def test_cannot(expect):
            expect.cannot_run("ABSENT_FIXTURE", "the parquet corpus was not built")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.num(result.ret, 67, "an unrunnable test exits INCOMPLETE, not 0")


def test_an_unrunnable_test_names_its_reason_and_its_detail(pytester, expect):
    """A third state that does not say why is a skip with better manners.

    The reason travels with the state, exactly as lib.sh prints
    `UNRUN  <name>: <REASON>: <detail>`.
    """
    pytester.makeconftest("pytest_plugins = ['pgc_vacuity']")
    pytester.makepyfile(
        """
        def test_cannot(expect):
            expect.cannot_run("ABSENT_FIXTURE", "the parquet corpus was not built")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    result.stdout.fnmatch_lines(
        ["*UNRUN*test_cannot*ABSENT_FIXTURE*the parquet corpus was not built*"])
    expect.num(result.ret, 67, "and it is still not a pass")



# ---------------------------------------------------------------------------
# AN UNEXPECTEDLY UNRUNNABLE TEST, which is a narrower claim than the three arms
# above make and the one #1163 needed. Six tests decline on PG 15, 16 and 17
# because `pg_restore_attribute_stats` and `WITHOUT OVERLAPS` arrived in 18, so
# "any unrunnable test exits 67" made a healthy corpus red on three majors.
#
# THE MAJOR COMES FROM `--pg-config`, so these drive a STUB that prints a version
# rather than mocking the lookup. That is the wiring: the list, the major and the
# comparison, in the order a real run reads them.
# ---------------------------------------------------------------------------

def _stub_pg_config(pytester, version_text):
    """A pg_config that answers --version and nothing else."""
    path = pytester.path / "stub_pg_config"
    path.write_text(f"#!/bin/sh\necho '{version_text}'\n")
    path.chmod(0o755)
    return str(path)


# THE INNER RUN NEEDS `--pg-config` DEFINED. The real one lives in conftest.py's
# `pytest_addoption`, which an inner session does not get, so a run that passed it
# exited USAGE_ERROR rather than exercising anything. Measured on the first
# attempt: ExitCode.USAGE_ERROR against an expected 0.
_INNER_CONFTEST = """
pytest_plugins = ['pgc_vacuity']


def pytest_addoption(parser):
    parser.addoption("--pg-config", action="store", default=None)
"""


def _unrunnable_fixture(pytester, expected_rows):
    pytester.makeconftest(_INNER_CONFTEST)
    pytester.makepyfile(
        """
        def test_cannot(expect):
            expect.cannot_run("UNSUPPORTED_MAJOR", "the feature arrived in 18")
        """
    )
    listing = pytester.path / "expected_unrunnable.txt"
    listing.write_text(expected_rows)
    return str(listing)


def test_an_expected_unrunnable_test_leaves_the_run_green(pytester, expect):
    """The property the matrix could not land without.

    The same declaration the arms above exit 67 for, listed for this major,
    exits 0 -- so the difference is the list and not the declaration.
    """
    listing = _unrunnable_fixture(
        pytester, "17 test_an_expected_unrunnable_test_leaves_the_run_green.py::test_cannot\n")
    result = pytester.runpytest(
        "-p", "pgc_vacuity",
        "--pg-config", _stub_pg_config(pytester, "PostgreSQL 17.10"),
        "--pgc-expected-unrunnable", listing)
    expect.num(result.ret, 0, "a decline the list names is not a red")


def test_an_unlisted_unrunnable_test_is_named_and_refused(pytester, expect):
    """The control beside it: the same run with an EMPTY list is refused, and the
    refusal names the test rather than counting it."""
    listing = _unrunnable_fixture(pytester, "# nothing expected on any major\n")
    result = pytester.runpytest(
        "-p", "pgc_vacuity",
        "--pg-config", _stub_pg_config(pytester, "PostgreSQL 17.10"),
        "--pgc-expected-unrunnable", listing)
    expect.num(result.ret, 67, "an unlisted decline is still INCOMPLETE")
    result.stderr.fnmatch_lines(["*not expected to be*", "*test_cannot*"])


def test_a_listed_test_that_runs_makes_the_list_stale(pytester, expect):
    """The other direction, which is the half a one-way check would miss.

    A feature arriving is a fact the list should stop claiming, and nothing else
    in the corpus would notice: the test simply starts passing.
    """
    pytester.makeconftest(_INNER_CONFTEST)
    pytester.makepyfile(
        """
        def test_runs_fine(expect):
            expect.num(1, 1, "it runs")
        """
    )
    listing = pytester.path / "expected_unrunnable.txt"
    listing.write_text(
        "17 test_a_listed_test_that_runs_makes_the_list_stale.py::test_runs_fine\n")
    result = pytester.runpytest(
        "-p", "pgc_vacuity",
        "--pg-config", _stub_pg_config(pytester, "PostgreSQL 17.10"),
        "--pgc-expected-unrunnable", str(listing))
    expect.num(result.ret, 67, "a list that claims a test declines, when it runs, is refused")
    result.stderr.fnmatch_lines(["*RAN, so the list is stale*"])


def test_a_listed_test_the_run_never_collected_is_not_a_stale_row(pytester, expect):
    """`allowed - actual` is only answerable for a test the run COLLECTED (#1204).

    A one-file run collects none of the other listed decliners, so every one of
    them reads as "expected to decline and RAN". That is not a stale list, it is a
    question nobody asked, and the run exited 67 while reporting its file passed.

    MEASURED ON PG 17 BEFORE THE FIX. `pytest test_native_reclaim_cycles.py`
    passed 15 checks, failed nothing, and exited 67 naming six tests it never
    collected. Running exactly one of those six failed too, because the other five
    were still uncollected. And the message says to edit the list, which would
    delete correct rows and redden the next full run from the other direction.

    THE PREMISE IS STATED PER ROW, NOT PER RUN. Gating the whole direction on a
    full run would switch it off for subsets; intersecting with what was collected
    keeps whatever the subset can answer. The arm above is the control: a listed
    test that WAS collected and did run is still a stale row.
    """
    pytester.makeconftest(_INNER_CONFTEST)
    pytester.makepyfile(
        """
        def test_runs_fine(expect):
            expect.num(1, 1, "it runs")
        """
    )
    listing = pytester.path / "expected_unrunnable.txt"
    # A nodeid in a file this run does not contain at all.
    listing.write_text("17 test_some_other_file.py::test_elsewhere\n")
    result = pytester.runpytest(
        "-p", "pgc_vacuity",
        "--pg-config", _stub_pg_config(pytester, "PostgreSQL 17.10"),
        "--pgc-expected-unrunnable", str(listing))
    expect.num(result.ret, 0,
               "a listed test the run never collected is not judged")


def test_the_list_is_read_for_the_running_major_only(pytester, expect):
    """A row for another major must not excuse this one.

    Without this the file would behave as one flat set, and the six rows that
    belong to 15, 16 and 17 would silently excuse the same decline on 18.
    """
    listing = _unrunnable_fixture(
        pytester, "15 test_the_list_is_read_for_the_running_major_only.py::test_cannot\n")
    result = pytester.runpytest(
        "-p", "pgc_vacuity",
        "--pg-config", _stub_pg_config(pytester, "PostgreSQL 18.4"),
        "--pgc-expected-unrunnable", listing)
    expect.num(result.ret, 67, "a row for 15 does not excuse a decline on 18")


def test_an_unreadable_pg_config_fails_closed(pytester, expect):
    """No major means no judgement, and no judgement means the OLD behaviour.

    The dangerous failure here is the other one: an unknown major treated as
    "expect anything" would turn every decline green on any box where
    `pg_config` is missing.
    """
    listing = _unrunnable_fixture(
        pytester, "17 test_an_unreadable_pg_config_fails_closed.py::test_cannot\n")
    result = pytester.runpytest(
        "-p", "pgc_vacuity",
        "--pg-config", str(pytester.path / "no-such-pg-config"),
        "--pgc-expected-unrunnable", listing)
    expect.num(result.ret, 67, "an unknown major refuses rather than excuses")

def test_a_real_failure_outranks_an_unrunnable_test(pytester, expect):
    """Which state dominates, asserted rather than left to fall out.

    lib.sh: a suite with both a FAIL and an UNRUN is FAILED, because the failure
    is the more urgent fact. Exit 1, not 67.
    """
    pytester.makeconftest("pytest_plugins = ['pgc_vacuity']")
    pytester.makepyfile(
        """
        def test_cannot(expect):
            expect.cannot_run("ABSENT_FIXTURE", "no corpus")
        def test_fails(expect):
            expect.num(1, 2, "one is not two")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.num(result.ret, 1, "a failure outranks an unrunnable test")


def test_a_run_with_nothing_unrunnable_still_exits_zero(pytester, expect):
    """Control. A guard that reddens a healthy run gets switched off, and then
    the guard it replaced is gone too."""
    pytester.makeconftest("pytest_plugins = ['pgc_vacuity']")
    pytester.makepyfile(
        """
        def test_ok(expect):
            expect.num(2 + 2, 4, "arithmetic still works")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.num(result.ret, 0, "an ordinary green run is untouched")


def test_layer_rejects_psycopgs_no_count_sentinel(pytester, expect):
    """cursor.rowcount is -1 when no count is available, and 1 for an unfetched
    SELECT. Both are numbers, so expect.num compares them happily."""
    pytester.makepyfile(
        """
        def test_rowcount_sentinel(expect):
            expect.rowcount(-1, -1, "a count that is not a count")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(result, "the -1 sentinel is refused", failed=1, passed=0)
    result.stdout.fnmatch_lines(["*no row count available*"])


def _collection_refusal_row(result, expect, *, rc, reason_glob, name):
    """One row of the #963 table: exit status, the sentence, no INTERNALERROR."""
    blob = result.stdout.str() + result.stderr.str()
    expect.num(result.ret, rc, f"{name}: exit status")
    expect.num(blob.count("INTERNALERROR"), 0, f"{name}: INTERNALERROR lines")
    if rc == 4:
        # UsageError is written to stderr. The stream is the claim: any non-zero
        # exit would satisfy run_failed, including the INTERNALERROR this issue
        # exists to close.
        result.stderr.fnmatch_lines([reason_glob])
    else:
        result.stdout.fnmatch_lines([reason_glob])


@pytest.mark.parametrize("mode", ["serial", "xdist"])
def test_a_bare_skip_refusal_keeps_its_reason_under_xdist(pytester, expect, mode):
    """#963. A collection-time UsageError must stay rc 4 with the sentence,
    including under `-n 2`. Measured on main: serial printed the reason on
    stderr and exited 4; `-n 2` replaced it with a 35-line INTERNALERROR and
    exited 1.
    """
    pytester.makepyfile(
        """
        import pytest
        @pytest.mark.skip(reason="not today")
        def test_quietly_gone():
            assert False
        """
    )
    extra = ("-n", "2") if mode == "xdist" else ()
    result = pytester.runpytest("-p", "pgc_vacuity", *extra)
    _collection_refusal_row(
        result, expect, rc=4, reason_glob="*bare skip*",
        name=f"bare skip {mode}",
    )


@pytest.mark.parametrize("mode", ["serial", "xdist"])
def test_a_collection_refusal_is_not_also_reported_as_a_silent_loss(pytester, expect, mode):
    """#991. The refusal and the reconciliation contradicted each other.

    A collection-time refusal printed its sentence and then, directly beneath it:

        VACUITY: 1 collected test(s) never reported an outcome, so the run lost
                 them silently: ...

    The run did not lose it silently. It refused it LOUDLY, one line above. So a
    reader who typed one bare skip got the correct diagnosis plus a second finding
    telling them a test vanished without saying so, and the natural response is to
    go looking for a lost test that was never lost.

    `collected - reported` is the right set difference and the wrong MEANING: a
    silent loss is when nobody said anything, and here the layer itself is what
    stopped the run.

    Measured before the fix -- and note the serial row is the one that survived
    #963, because clearing `items[:]` in the worker already emptied the controller's
    `collected` set under `-n`:

        main   serial   refusal + the silent-loss line
        main   xdist    no refusal at all (that was #963)
        #963   serial   refusal + the silent-loss line   <- what this closes
        #963   xdist    refusal, no line
    """
    pytester.makepyfile(
        """
        import pytest
        @pytest.mark.skip
        def test_one_offending_arm():
            assert True
        """
    )
    extra = ("-n", "2") if mode == "xdist" else ()
    result = pytester.runpytest("-p", "pgc_vacuity", *extra)
    blob = result.stdout.str() + result.stderr.str()
    _collection_refusal_row(
        result, expect, rc=4, reason_glob="*bare skip*",
        name=f"refusal not a loss {mode}",
    )
    expect.num(blob.count("lost them silently"), 0,
               f"{mode}: the refusal is not also reported as a silent loss")
    expect.num(blob.count("VACUITY:"), 0,
               f"{mode}: and with nothing else outstanding there is no VACUITY line at all")


def test_a_genuine_silent_loss_is_still_reported(pytester, expect):
    """THE CONTROL, and the arm above is worth nothing without it.

    Dropping a problem from a reconciliation is one edit away from dropping the
    guard. So: an item collected and never reported, with NO refusal anywhere, must
    still be named -- which is the case the guard was written for, a test that
    vanishes without anybody saying so.

    The conftest removes an item AFTER the layer's own hook has recorded it, and
    without deselecting it, which is exactly the shape of a crashed xdist worker.
    `pytest_deselected` is the hook that tells a deliberate subset from a loss, and
    nothing calls it here.
    """
    pytester.makeconftest(
        """
        import pytest

        @pytest.hookimpl(trylast=True)
        def pytest_collection_modifyitems(config, items):
            del items[-1]
        """
    )
    pytester.makepyfile(
        """
        def test_the_survivor(expect):
            expect.num(1, 1, "this one runs")

        def test_vanishes_without_a_word(expect):
            expect.num(1, 1, "collected, then removed without deselection")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    blob = result.stdout.str() + result.stderr.str()
    expect.num(blob.count("lost them silently"), 1,
               "a genuine silent loss is still reported")
    expect.num(blob.count("test_vanishes_without_a_word"), 1,
               "and it is NAMED, because the next one will not be this one")
    expect.num(result.ret, 1, "and the run is red for it")


@pytest.mark.parametrize("mode", ["serial", "xdist"])
def test_a_broad_except_refusal_keeps_its_reason_under_xdist(pytester, expect, mode):
    """#963. Same table, second collection-time row. The defect is the hook,
    not the skip marker, so a second offence has to keep the sentence too.
    """
    pytester.makepyfile(
        """
        def test_swallows(expect):
            try:
                raise RuntimeError("the real failure")
            except Exception:
                pass
            expect.num(1, 1, "and then asserts something harmless")
        """
    )
    extra = ("-n", "2") if mode == "xdist" else ()
    result = pytester.runpytest("-p", "pgc_vacuity", *extra)
    _collection_refusal_row(
        result, expect, rc=4, reason_glob="*catches Exception broadly*",
        name=f"broad except {mode}",
    )


@pytest.mark.parametrize("mode", ["serial", "xdist"])
def test_an_in_test_vacuity_refusal_is_unchanged_under_xdist(pytester, expect, mode):
    """#963 control. A VacuityError raised inside a test body is a normal
    failure and reports identically under serial and `-n 2`. If this arm
    started needing rc 4, the fix would have moved the wrong hook.
    """
    pytester.makepyfile(
        """
        def test_asserts_nothing():
            x = 1 + 1
        """
    )
    extra = ("-n", "2") if mode == "xdist" else ()
    result = pytester.runpytest("-p", "pgc_vacuity", *extra)
    _collection_refusal_row(
        result, expect, rc=1, reason_glob="*made no counted assertion*",
        name=f"in-test vacuity {mode}",
    )


def test_layer_rejects_a_broad_except_in_a_test_file(pytester, expect):
    """The layer forbade this in a comment, which enforces nothing.

    After any failed statement psycopg raises InFailedSqlTransaction for every
    later one, so one `except Exception` hides the real error and all its
    successors. Measured: a test using the forbidden shape passed with no complaint.
    """
    pytester.makepyfile(
        """
        def test_swallows(expect):
            try:
                raise RuntimeError("the real failure")
            except Exception:
                pass
            expect.num(1, 1, "and then asserts something harmless")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.run_failed(result, "a broad except must not be collectable")
    result.stderr.fnmatch_lines(["*catches Exception broadly*"])

# ---- an A/B whose arms do not differ measures nothing -------------------------
#
# `mutation-arm-unobservable` in VACUITY_MODES.md section 3.5: both arms of an A/B
# produce the identical answer and both are green, because the assertion that would
# catch it -- that the arms must DIFFER -- is the one nobody writes.
#
# Measured before building: the layer had EIGHT helpers asserting equality and ONE
# asserting inequality, `ordering_observable`, which is specific to a forward/reverse
# pair. Two sites in the corpus hand-rolled the general case as
# `expect.num(int(after != before), 1, ...)`, which throws both values away: when it
# fails it says `got 0 want 1` and the reader cannot see what the two arms were.


def test_layer_requires_ab_arms_to_differ(pytester, expect):
    """Two arms that agree cannot show that the thing between them did anything.

    Bare pytest: the test passes, because nothing was asserted about the pair.
    """
    pytester.makeconftest("pytest_plugins = ['pgc_vacuity']")
    pytester.makepyfile(
        """
        def test_the_mutation_changed_nothing(expect):
            baseline = [(1, 'a'), (2, 'b')]
            mutated = [(1, 'a'), (2, 'b')]
            expect.differ(baseline, mutated, "the mutation moved the result")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    # `expect.refusal` rather than `fnmatch_lines`, and one token per call. The raw
    # matcher searches the inner run's WHOLE stdout, and pytest prints the failing
    # function's SOURCE in the traceback -- so a pattern naming a value in the test
    # matches the source line rather than anything the guard produced. The arm below
    # passed that way before this change, against a layer with no `differ` at all.
    expect.refusal(result, "two identical arms do not pass", r"arms-do-not-differ")


def test_differ_names_both_arms_when_they_agree(pytester, expect):
    """The hand-rolled idiom this replaces printed `got 0 want 1`.

    A reader of that cannot tell whether the arms were both empty, both wrong, or
    correctly identical -- which is three different defects with one message.
    """
    pytester.makepyfile(
        """
        def test_identical_arms(expect):
            expect.differ("PLAN-A", "PLAN-A", "the GUC changed the plan")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    # ANCHORED, because the value is written in the test body: with `fnmatch_lines`
    # this arm passed against an AttributeError from a `differ` that did not exist,
    # satisfied by its own source printed in the traceback. Measured, not reasoned.
    expect.refusal(result, "the refusal names the value both arms carried", r"PLAN-A")
    expect.refusal(result, "and names the mode", r"arms-do-not-differ")


def test_differ_passes_when_the_arms_differ(pytester, expect):
    """The positive control. A guard that rejects real A/B tests gets switched off."""
    pytester.makepyfile(
        """
        def test_arms_differ(expect):
            expect.differ([(1,), (2,)], [(2,), (1,)], "reversing changes the order")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(result, "differing arms pass", passed=1, failed=0)


def test_differ_counts_as_an_assertion(pytester, expect):
    """A test whose only assertion is `differ` has concluded something.

    Without this the no-assertion guard fires instead, and the arm above would pass
    for the wrong reason -- a failure, but not the one it names.
    """
    pytester.makepyfile(
        """
        def test_only_a_differ(expect):
            expect.differ(1, 2, "one is not two")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(result, "differ alone is a counted assertion", passed=1, failed=0)


def test_differ_refuses_a_failed_query_on_either_side(pytester, expect):
    """TWO FAILED QUERIES ARE NOT TWO OBSERVABLE ARMS, and this is the inverse of the
    trap #930 closed.

    `query_error()` produces a value unique per occurrence, precisely so that two
    failures cannot compare EQUAL and pass an equality assertion. That uniqueness
    makes them compare UNEQUAL, so an arms-differ assertion passes on a pair of
    statements that both blew up -- the same defect arriving through the fix for it.

    Measured: `query_error()` twice gives `QUERY_ERROR.1.<detail>` and
    `QUERY_ERROR.2.<detail>`, which are `!=`.
    """
    for side in ("left", "right"):
        pytester.makeconftest("pytest_plugins = ['pgc_vacuity']")
        args = ('pgc_vacuity.query_error("a"), "PLAN-B"' if side == "left"
                else '"PLAN-A", pgc_vacuity.query_error("b")')
        pytester.makepyfile(
            f"""
            import pgc_vacuity

            def test_one_arm_failed(expect):
                expect.differ({args}, "the arms differ")
            """
        )
        result = pytester.runpytest("-p", "pgc_vacuity")
        expect.refusal(result, f"a failed query on the {side} is not an arm",
                       r"failed query")


def test_differ_refuses_two_failed_queries(pytester, expect):
    """The shape the uniqueness fix created, written out.

    Both arms raised, both sentinels are distinct, and without the refusal the
    assertion reports that the mutation was observable.
    """
    pytester.makeconftest("pytest_plugins = ['pgc_vacuity']")
    pytester.makepyfile(
        """
        import pgc_vacuity

        def test_both_arms_failed(expect):
            a = pgc_vacuity.query_error("baseline blew up")
            b = pgc_vacuity.query_error("mutated blew up")
            expect.differ(a, b, "the mutation moved the result")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.refusal(result, "two failures are not two arms", r"failed query")

# ---- and the hand-rolled idiom cannot come back --------------------------------
#
# A helper nobody is required to use is a convention, not a mechanism, and this
# directory has a standing rule that a mode counts as refused only when a mechanism
# refuses it. `differ` on its own leaves the old spelling available, so the scan below
# makes the class unavailable rather than fixing the two instances -- which is the same
# argument as "fix the class, not the instance".
#
# AST, NOT A LINE REGEX. The two paragraphs in this tree that DESCRIBE the old idiom
# quote it verbatim, so a text sweep flags its own documentation; `ast` sees code only.
# That is the same trap the `pytest.raises` scan records, where a regex version
# refused the layer's own test suite with 22 invented offences.

_INEQ_MSG = "int() of a comparison, passed to an expect call"


# EVERY COMPARISON OPERATOR, LOOKED UP BY NAME (#1030). The first version of this
# scan required an `Eq` or a `NotEq`:
#
#     and any(isinstance(op, (ast.NotEq, ast.Eq)) for op in arg.args[0].ops)
#
# so `int(a != b)` was refused and `int(x in y)` was not. The docstring said
# `int(<Compare>)` and the code delivered `int(<Compare with Eq or NotEq>)`, and by
# the time anyone read it the scanned class was EMPTY while seven live sites sat
# outside it. A name that outran its content, which is the shape this corpus keeps
# finding in other people's documents.
#
# Named rather than enumerated as classes, so a future operator cannot silently
# narrow the rule by not being in a tuple. The list is asserted against `ast`
# itself below: if Python grows an operator, that arm reddens rather than this scan
# quietly skipping it.
_COMPARE_OPS = ("Eq", "NotEq", "Lt", "LtE", "Gt", "GtE", "Is", "IsNot", "In", "NotIn")


def _collapses_to_a_flag(node, ast=None):
    """Does this expression throw BOTH of its values away, leaving 0 or 1?

    A comparison does. So does ANY boolean combination -- and that one is worse,
    because `int(a and b)` cannot say WHICH half was false.

    THE OPERANDS DO NOT MATTER, and the first version of this got that wrong by
    requiring a Compare inside. It left `int(p.exists() and q.exists())` live in
    `test_compare_to_bash.py`, a PREMISE arm whose whole job is to say which half
    of a pair is missing, reporting `got 0 want 1` instead. Reported by @jdatcmd,
    who probed the boundary rather than reading the branch:

        int(a == b and c == d)          BoolOp of Compare -> flagged
        int(p.exists() and q.exists())  BoolOp of Call    -> was NOT flagged

    A single truthiness is still fine: `int(x)` and `int(p.exists())` have one
    value and nothing to disambiguate. It is the COMBINING that loses the answer.
    """
    import ast as _a
    ast = ast or _a
    return isinstance(node, (ast.Compare, ast.BoolOp))


def _hand_rolled_inequalities(source, filename="<probe>"):
    """-> ["file:line", ...] for `expect.X(int(a != b), ...)` and friends.

    The shape is `int(<Compare>)`, for ANY comparison operator, appearing as an
    ARGUMENT to a call on `expect` -- plus `int(<Compare> and <Compare>)`, which
    collapses two of them at once. A bare `int(a != b)` assigned to a name is not
    flagged: it asserts nothing by itself, and flagging it would be a claim about
    arithmetic rather than about an assertion.
    """
    import ast

    found = []

    class V(ast.NodeVisitor):
        def visit_Call(self, node):
            target = node.func
            is_expect = (isinstance(target, ast.Attribute)
                         and isinstance(target.value, ast.Name)
                         and target.value.id in ("expect", "e"))
            if is_expect:
                for arg in node.args:
                    if (isinstance(arg, ast.Call)
                            and isinstance(arg.func, ast.Name)
                            and arg.func.id == "int"
                            and len(arg.args) == 1
                            and _collapses_to_a_flag(arg.args[0], ast)):
                        found.append(f"{filename}:{arg.lineno}")
            self.generic_visit(node)

    V().visit(ast.parse(source))
    return found


def test_the_inequality_scan_finds_a_planted_offence(expect):
    """The scan must fire on the exact shape the two converted sites used."""
    planted = _hand_rolled_inequalities(
        "def test_x(expect):\n"
        "    expect.num(int(after != before), 1, 'moved')\n",
        "planted.py")
    expect.num(len(planted), 1, "the scan finds a hand-rolled inequality")
    expect.text(planted[0], "planted.py:2", "and names where it is")
    # at_least as well as num, because the other converted site used that one.
    expect.num(len(_hand_rolled_inequalities(
        "def test_y(expect):\n"
        "    expect.at_least(int(a != b), 1, 'moved')\n")), 1,
        "and finds it through at_least too")
    # THE INVERTED SPELLING, which is the one the manual count missed. The third site
    # wrote `int(stated == disk) == 0` -- the same assertion with the comparison
    # flipped -- so a scan that looked only for `!=` would have left it in place and
    # reported a clean corpus. Without this arm, dropping `ast.Eq` from the scan is
    # invisible.
    expect.num(len(_hand_rolled_inequalities(
        "def test_z(expect):\n"
        "    expect.num(int(stated == disk), 0, 'disagrees')\n")), 1,
        "and finds the int(a == b) spelling, not only int(a != b)")
    # EVERY OPERATOR, not just equality (#1030). The scan required an Eq or a NotEq
    # while its docstring said `int(<Compare>)`, so the scanned class was EMPTY and
    # 27 live sites sat outside it. One arm per operator family, because "any
    # Compare" is the claim and a single `in` case would not show it.
    for label, op in (("in", "'x' in got"), ("not in", "'x' not in got"),
                      ("<", "a < b"), (">", "a > b"), ("<=", "a <= b"),
                      (">=", "a >= b"), ("is", "a is b"), ("is not", "a is not b")):
        expect.num(len(_hand_rolled_inequalities(
            f"def t(expect):\n    expect.num(int({op}), 1, 'n')\n")), 1,
            f"the scan finds int(a {label} b)")
    # AND THE BOOLEAN COMBINATION, which is the worst of them: it collapses two
    # comparisons, so a failure cannot say which half broke.
    expect.num(len(_hand_rolled_inequalities(
        "def t(expect):\n    expect.num(int(a < b and a < c), 1, 'n')\n")), 1,
        "the scan finds int(<Compare> and <Compare>)")
    # THE OPERANDS DO NOT MATTER. Requiring a Compare inside the BoolOp left
    # `int(p.exists() and q.exists())` live -- a premise arm that exists to say
    # WHICH half is missing, reporting `got 0 want 1`. Reported by @jdatcmd.
    for label, src in (("calls", "int(p.exists() and q.exists())"),
                       ("plain names", "int(a and b)"),
                       ("or, not and", "int(a or b)"),
                       ("three operands", "int(a and b and c)")):
        expect.num(len(_hand_rolled_inequalities(
            f"def t(expect):\n    expect.num({src}, 1, 'n')\n")), 1,
            f"the scan finds a boolean pair of {label}, not only of comparisons")


def test_the_operator_list_is_what_ast_offers(expect):
    """The list is named rather than enumerated as classes, so this arm is what stops
    a future operator from silently narrowing the rule by not being in a tuple."""
    import ast

    offered = sorted(n for n in dir(ast)
                     if isinstance(getattr(ast, n), type)
                     and issubclass(getattr(ast, n), ast.cmpop)
                     and n != "cmpop")
    expect.text(" ".join(offered), " ".join(sorted(_COMPARE_OPS)),
                "the declared comparison operators are exactly what ast offers")


def test_the_inequality_scan_does_not_flag_honest_code(expect):
    """The false-positive budget, which a static guard needs before it ships."""
    for label, src in (
        ("a bare int()", "def t(expect):\n    expect.num(int(x), 1, 'n')\n"),
        ("a comparison not wrapped in int()",
         "def t(expect):\n    expect.differ(a, b, 'arms')\n"),
        ("an int(compare) bound to a name first",
         "def t(expect):\n    flag = int(a != b)\n    expect.num(flag, 1, 'n')\n"),
        # PASSED TO A CALL THAT IS NOT AN ASSERTION, which is what makes the
        # expect-context condition load-bearing. Without this shape, dropping that
        # condition changed nothing and the arm stayed green -- found by mutating it.
        # The scan's subject is assertions, not arithmetic: `print(int(a != b))`
        # asserts nothing and flagging it would be a false red.
        ("an int(compare) passed to a call that is not an expect",
         "def t(expect):\n    print(int(a != b))\n    expect.num(1, 1, 'n')\n"),
        ("the idiom inside a comment",
         "def t(expect):\n    # expect.num(int(a != b), 1, 'moved')\n"
         "    expect.num(1, 1, 'n')\n"),
        ("the idiom inside a string",
         "def t(expect):\n    s = \"expect.num(int(a != b), 1, 'x')\"\n"
         "    expect.text(s, s, 'n')\n"),
        # THE HONEST int() CALLS THIS CORPUS ACTUALLY MAKES (#1030). Widening the
        # scan from Eq/NotEq to every comparison is only safe if these stay out, so
        # the cost is measured here rather than assumed. Each is a real shape from
        # the tree, not an invented one.
        ("int() of a regex group, parsing a number",
         "def t(expect):\n    expect.num(int(m.group(1)), 3, 'n')\n"),
        ("int() of a driver flag with nothing richer to show",
         "def t(expect):\n    expect.num(int(writes[0].acknowledged), 1, 'n')\n"),
        ("int() of a path premise",
         "def t(expect):\n    expect.num(int(HOWTO.is_file()), 1, 'n')\n"),
        ("int() of a value num() would refuse as a string",
         "def t(expect):\n    expect.num(int(level), 2, 'n')\n"),
        # A SINGLE truthiness is honest: one value, nothing to disambiguate.
        ("int() of a single call",
         "def t(expect):\n    expect.num(int(p.exists()), 1, 'n')\n"),
    ):
        expect.num(len(_hand_rolled_inequalities(src)), 0,
                   f"not flagged: {label}")


def test_no_test_in_this_corpus_hand_rolls_an_inequality(expect):
    """The population, which is what makes the two arms above worth having.

    Two sites used the idiom before `differ` existed. Both are converted, so this is
    zero -- and the arms above are why a zero here means the scan looked rather than
    that it cannot see.
    """
    import pathlib

    here = pathlib.Path(__file__).parent
    files = sorted(here.glob("test_*.py"))
    expect.at_least(len(files), 10, "premise: the scan has a corpus to read")
    offences = []
    for f in files:
        offences += _hand_rolled_inequalities(f.read_text(encoding="utf-8"), f.name)
    expect.text(repr(offences), "[]", "no test hand-rolls an inequality")


# ---- the layer's own names are reachable from the tree it polices (#924) ----
#
# pytest imports `conftest.py` FROM THE POLICED DIRECTORY into the policing
# interpreter, before collection, with no opt-out. So every module-level name in
# pgc_vacuity is writable by the code it judges. That is not a bug in pytest; it
# is what conftest is for. It is a defect here because this layer's whole job is
# to refuse, and a refusal that can be deleted in two lines is a suggestion.
#
# #958 closed the datum one exploit used (`_ORDER_KILLERS`) by binding it in a
# default argument. The three scans that READ such data are module-level names
# themselves, one frame further out, and each is a two-line conftest away from
# being a no-op. Measured on main 226f805 with the pinned runner:
#
#     GUARD               no conftest    with the rebind
#     order collapse      REFUSED rc=4   PASSED rc=0
#     broad except        REFUSED rc=4   PASSED rc=0
#     raises not pinned   REFUSED rc=4   PASSED rc=0
#
# The fix is not another name moved out of reach. It is the layer noticing that
# one of its own bindings changed, which covers the names added after this was
# written as well as the 47 present when it was.


def _rebind(name):
    """A conftest that switches one scan off, and nothing else. Two lines."""
    return (
        "import pgc_vacuity\n"
        f"pgc_vacuity.{name} = lambda path: []\n"
    )


def test_a_conftest_cannot_switch_off_the_order_collapse_scan(pytester, expect):
    """#924, the route still open after #958."""
    pytester.makepyfile(
        """
        def test_order_collapsed(expect):
            got = ["b", "a"]
            g = sorted(got)
            expect.ordered_rows(g, ["a", "b"], "rows in order")
        """
    )
    plain = pytester.runpytest("-p", "pgc_vacuity")
    expect.run_failed(plain, "premise: the collapse is refused when nobody rebinds")
    plain.stderr.fnmatch_lines(["*order-killed*"])

    pytester.makeconftest(_rebind("_sorted_ordered_sites"))
    hatched = pytester.runpytest("-p", "pgc_vacuity")
    expect.run_failed(hatched, "and it is still refused after the scan is rebound")
    hatched.stderr.fnmatch_lines(["*_sorted_ordered_sites*"])


def test_a_conftest_cannot_switch_off_the_broad_except_scan(pytester, expect):
    """The same hatch, a different scan. Named separately because a fix that
    closed only the scan #924 happens to name would leave this one open, which
    is how #924 came back after #958."""
    pytester.makepyfile(
        """
        def test_broad(expect):
            try:
                x = 1
            except Exception:
                pass
            expect.num(x, 1, "x is one")
        """
    )
    plain = pytester.runpytest("-p", "pgc_vacuity")
    expect.run_failed(plain, "premise: the broad except is refused")
    plain.stderr.fnmatch_lines(["*catches Exception broadly*"])

    pytester.makeconftest(_rebind("_broad_except_sites"))
    hatched = pytester.runpytest("-p", "pgc_vacuity")
    expect.run_failed(hatched, "and it is still refused after the scan is rebound")
    hatched.stderr.fnmatch_lines(["*_broad_except_sites*"])


def test_a_conftest_cannot_switch_off_the_raises_scan(pytester, expect):
    """The third. The body holds a compound statement rather than a broad
    exception class, because that arm needs no driver installed and this file
    runs in the database-free job.

    My first version of this premise used `pytest.raises(ValueError)` around one
    statement, which the rule does not refuse -- so the PREMISE printed `1 passed`
    and the arm could not have shown anything being switched off.
    """
    pytester.makepyfile(
        """
        import pytest

        def test_raises_compound(expect):
            with pytest.raises(ValueError):
                for i in [1]:
                    raise ValueError("boom")
            expect.num(1, 1, "ran")
        """
    )
    plain = pytester.runpytest("-p", "pgc_vacuity")
    expect.run_failed(plain, "premise: an unpinned compound raises block is refused")
    plain.stderr.fnmatch_lines(["*not pinned*"])

    pytester.makeconftest(_rebind("_raises_sites"))
    hatched = pytester.runpytest("-p", "pgc_vacuity")
    expect.run_failed(hatched, "and it is still refused after the scan is rebound")
    hatched.stderr.fnmatch_lines(["*_raises_sites*"])


@pytest.mark.parametrize("mode", ["serial", "xdist"])
def test_a_conftest_cannot_stub_an_expect_method_so_a_false_claim_passes(pytester, expect, mode):
    """#967, the class-attribute frame. #964 snapshots module bindings.
    `Expect.num = a stub` is not a rebind of `Expect` -- the name still points
    at the same class -- so a false claim reports as a pass if the stub still
    increments the count. An instance attribute or a subclass fixture is a
    different object and is not this test.

    The three rows the issue named, and a fix must keep 1 and 3 while turning 2
    into a refusal:

        no conftest                         failed, correctly
        Expect.num stubbed, still counting  PASSED -- the hatch
        Expect._record stubbed              failed, count 0 (separate control)
    """
    pytester.makepyfile(
        """
        def test_one_equals_two(expect):
            expect.num(1, 2, "one equals two, which it does not")
        """
    )
    plain = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(plain, "premise: a false claim fails when nobody stubs",
                    failed=1, passed=0)

    import pgc_vacuity
    original = pgc_vacuity.Expect.num
    pytester.makeconftest(
        "import pgc_vacuity\n"
        "pgc_vacuity.Expect.num = "
        "lambda self, got, want, name: self._record(name)\n"
    )
    # BOTH MODES, because this surface reaches the reporter only after the merge
    # that composed #963 and #967. Before it, the method branch raised
    # `pytest.UsageError` directly -- and a UsageError raised in an xdist WORKER
    # never reaches the controller, so `-n 2` gave a bare exit code instead of the
    # sentence. Serial alone cannot see that: it is the same green either way.
    extra = ("-n", "2") if mode == "xdist" else ()
    try:
        hatched = pytester.runpytest("-p", "pgc_vacuity", *extra)
    finally:
        pgc_vacuity.Expect.num = original
    _collection_refusal_row(
        hatched, expect, rc=4, reason_glob="*Expect.num*",
        name=f"stubbed Expect.num {mode}",
    )


def test_stubbing_the_recorder_still_fails_closed_by_count(pytester, expect):
    """#967 row 3. Stubbing `_record` leaves the count at 0, so the test is
    refused for making no counted assertion. That is a different mechanism from
    the public-method snapshot, and it must stay the one that fires -- a snapshot
    of `_record` would swallow this into a collection-time refusal and the
    control would no longer mean what it says.
    """
    pytester.makepyfile(
        """
        def test_one_is_one(expect):
            expect.num(1, 1, "one is one")
        """
    )
    import pgc_vacuity
    original = pgc_vacuity.Expect._record
    pytester.makeconftest(
        "import pgc_vacuity\n"
        "pgc_vacuity.Expect._record = lambda self, name: None\n"
    )
    try:
        result = pytester.runpytest("-p", "pgc_vacuity")
    finally:
        # pytester is in-process: the inner conftest writes the shared class.
        # This arm deliberately does not snapshot `_record`, so nothing restores
        # it for us -- and the outer expect.outcomes would then count nothing.
        pgc_vacuity.Expect._record = original
    expect.outcomes(result, "stubbing _record is refused by count 0, not by snapshot",
                    failed=1, passed=0)
    result.stdout.fnmatch_lines(["*no counted assertion*"])


def test_the_refusal_names_the_binding_that_changed(pytester, expect):
    """A refusal that does not say what was rebound sends the reader to the
    wrong file. The offending test here is HONEST -- the only thing wrong with
    the run is the conftest -- so nothing but the tamper check can refuse it."""
    pytester.makepyfile(
        """
        def test_honest(expect):
            expect.num(1, 1, "one is one")
        """
    )
    plain = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(plain, "premise: an honest test passes", passed=1, failed=0)

    pytester.makeconftest(_rebind("_broad_except_sites"))
    hatched = pytester.runpytest("-p", "pgc_vacuity")
    expect.run_failed(hatched, "a rebound layer name refuses a run of honest tests")
    hatched.stderr.fnmatch_lines(["*_broad_except_sites*"])


def test_a_new_attribute_on_the_layer_is_not_a_rebind(pytester, expect):
    """The control, and it is LOAD-BEARING rather than hygiene (@OffgridwithJD).

    A guard that fired on anything touching the module would be the
    false-positive engine this layer's own budget forbids -- but it would also
    turn four EXISTING tests red, because each writes a name that has never been
    a module attribute:

        _ORDER_KILLERS    test_ordered.py:148, :169     (removed by #958)
        _BROAD_RAISES     test_raises_sqlstate.py:584
        broad_families    test_raises_sqlstate.py:585
        BROAD_RAISES      test_raises_sqlstate.py:586

    A snapshot comparison cannot flag any of them, because there is no prior
    binding to differ from. So this arm is what keeps an ADD from being tightened
    into tampering by someone who reads the check as incomplete. Verified: only
    `QUERY_ERROR` (test_failed_query_sentinel.py:321, :352) rebinds a name that
    exists, and it restores it in a `finally` without driving an inner collection.
    """
    pytester.makepyfile(
        """
        def test_honest(expect):
            expect.num(1, 1, "one is one")
        """
    )
    pytester.makeconftest(
        "import pgc_vacuity\n"
        "pgc_vacuity._a_name_the_layer_never_had = 1\n"
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(result, "a NEW attribute is not a changed binding",
                    passed=1, failed=0)


def test_the_rebind_does_not_leak_into_this_session(pytester, expect):
    """pytester runs the inner session IN-PROCESS, on the same module object, so
    a conftest that rebinds a scan leaves it rebound for every test that follows
    -- including the ones in this file. The layer already paid for that once
    (see `_RunShape`: 'AN INSTANCE PER CONFIG, NOT MODULE GLOBALS').

    So the refusal restores the binding before raising, and this asserts it by
    USING the scan afterwards rather than by comparing identities: a no-op lambda
    returns [] for everything, and the real scan finds the planted offence.
    """
    pytester.makepyfile(
        """
        def test_honest(expect):
            expect.num(1, 1, "one is one")
        """
    )
    pytester.makeconftest(_rebind("_broad_except_sites"))
    expect.run_failed(pytester.runpytest("-p", "pgc_vacuity"),
                      "the inner run is refused")

    import pgc_vacuity
    planted = pytester.makepyfile(
        planted="""
        def test_planted(expect):
            try:
                x = 1
            except Exception:
                pass
            expect.num(x, 1, "x is one")
        """
    )
    expect.num(len(pgc_vacuity._broad_except_sites(str(planted))), 1,
               "the real scan is back and still finds the offence")
