"""Every counted assertion produces a record, and the count is derived from them.

#937. The shell harness records every check outcome as a machine-readable line,
and `pgc_record` counts and records in one call so no path can do either alone.
`pgc_reconcile_records` then reconciles the record count against `checks run:`.

THE PYTEST HALF REACHED THE SAME PROPERTY THROUGH PYTHON RATHER THAN THROUGH THE
SHELL'S FORMAT, which is what "parallel in functionality only" requires. Nothing
here reads, sources or derives from `test/*.sh`; `test_harness_deps.py` proves
that for the whole corpus rather than this file asserting it about itself.

AND IT IS STRONGER THAN THE SHELL'S, because Python can remove the possibility
instead of policing it. The shell keeps a counter and a record stream honest by
reconciling two variables that could drift. Here the count IS the record stream:

    @property
    def count(self):
        return len(self._records)

A derived count cannot be incremented without a record existing. That is property
1 of #937 reached by construction rather than by discipline, and the arms below
attack the construction rather than trusting the sentence.

Measured before this file existed: `_counted()` at 15 call sites, `self.count`
incremented by one line and read by one (`pytest_runtest_call`), and ZERO
per-assertion records anywhere.
"""

import pgc_vacuity


def test_each_counted_assertion_appends_exactly_one_record(expect):
    """Three assertions, three records. The premise is that a fresh recorder has
    none, or the arm would pass on a recorder that ignored every call."""
    e = pgc_vacuity.Expect("records::one-each")
    expect.num(len(e.records), 0, "premise: a fresh recorder holds no records")
    e.num(1, 1, "first")
    e.num(2, 2, "second")
    e.num(3, 3, "third")
    expect.num(len(e.records), 3, "three assertions left three records")


def test_the_count_is_the_record_stream(expect):
    """Not "the count agrees with the records" -- that is the shell's property,
    and it needs a reconciliation because the two can drift. Here they are the
    same object, so the arm asserts identity of the NUMBER at every step rather
    than equality at the end."""
    e = pgc_vacuity.Expect("records::derived")
    seen = []
    for i in range(4):
        e.num(i, i, f"assertion {i}")
        seen.append((e.count, len(e.records)))
    expect.rows([f"{c}/{r}" for c, r in seen],
                ["1/1", "2/2", "3/3", "4/4"],
                "the count tracks the records at every step")


def test_the_count_cannot_be_moved_without_a_record(expect):
    """THE CONSTRUCTION PROOF, and the reason this is not just a second counter.

    A test that only checked `count == len(records)` after a run would pass on an
    implementation that keeps two variables and happens to update both. This
    asserts the count cannot be written at all, which is what makes the agreement
    structural rather than maintained.
    """
    e = pgc_vacuity.Expect("records::readonly")
    e.num(1, 1, "one assertion")
    try:
        e.count = 99
    except AttributeError:
        expect.num(e.count, 1, "the count refused to be written and did not move")
    else:
        raise AssertionError(
            "the count was assignable, so it is a second variable that can drift "
            "from the records rather than being derived from them"
        )


def test_a_record_names_the_assertion_that_made_it(expect):
    """A record that cannot be traced to a call site answers no question worth
    asking. The names are asserted IN ORDER, because the order is what lets a
    later phase say which assertion failed."""
    e = pgc_vacuity.Expect("records::named")
    e.num(1, 1, "the first question")
    e.text("a", "a", "the second question")
    e.num(2, 2, "the third question")
    expect.ordered_rows([r.name for r in e.records],
                        ["the first question", "the second question",
                         "the third question"],
                        "each record carries the name its call site gave")


def test_a_refused_assertion_leaves_no_record(expect):
    """A VacuityError means the assertion never ran, so it is not an outcome.

    This is the arm that stops the record stream becoming a log of attempts. It
    matters for the reconciliation in phase 5: a refused assertion that left a
    record would make the totals disagree with what the run reported.
    """
    e = pgc_vacuity.Expect("records::refused")
    try:
        e.num("100", 100, "a text comparison")
    except pgc_vacuity.VacuityError:
        expect.num(len(e.records), 0, "a refused assertion recorded nothing")
    else:
        raise AssertionError("num() accepted a string, so this arm tested nothing")


def test_a_name_carrying_a_separator_survives_the_record(expect):
    """#937 property 3, asserted rather than assumed.

    The shell had to strip tabs and newlines from a check name because its record
    is a tab-separated line: a name with a tab gave four fields, and a name with a
    newline gave two lines. The record here is an object, so there is no separator
    to smuggle -- but that must be a test, because the moment somebody formats
    these into a line the class of defect comes back and nothing would say so.
    """
    e = pgc_vacuity.Expect("records::separators")
    nasty = "a name with\ta tab and\na newline"
    e.num(1, 1, nasty)
    expect.num(len(e.records), 1, "a name with separators made exactly one record")
    expect.text(e.records[0].name, nasty, "and the name came back byte-identical")


# ---- phase 2: the verdict is resolved where the outcome is known -------------
#
# The first version of this file's comment argued the verdict could be resolved
# from the exception in `pytest_runtest_call`, because assertions are sequential
# and a raise ends the test. @OffgridwithJD refuted it, and this corpus is what
# refutes it: proving a guard REFUSES means catching the AssertionError, which
# five tests do. Measured before the fix:
#
#     count before/mid/after: 0 / 1 / 2
#       record 0  'this comparison must fail'   verdict PASS   <- this one RAISED
#       record 1  'and the test continues'      verdict PASS
#     1 passed
#
# A genuinely failed assertion stayed PASS, in a passing test, with nothing
# reaching the hook to correct it. So the verdict is set on the COMPARISON's own
# path instead, where the outcome is known and no propagation is needed.


def test_a_failed_assertion_records_fail_even_when_the_test_catches_it(expect):
    """THE ARM FOR THE REFUTATION, and the shape five tests in this corpus use.

    Catching the AssertionError is how a test proves a guard refuses. If catching
    it also erased the verdict, every one of those tests would be reporting on a
    record stream that says its deliberate failure passed.
    """
    e = pgc_vacuity.Expect("verdict::caught")
    try:
        e.num(1, 2, "this comparison must fail")
    except AssertionError:
        pass
    expect.num(len(e.records), 1, "premise: the failed assertion was still counted")
    expect.text(e.records[0].verdict, "FAIL",
                "and it is recorded as FAIL, not as a pass the catcher hid")


def test_the_failure_reason_is_the_assertions_own_message(expect):
    """A verdict with no reason sends the reader back to the source to find out
    what happened. The message is the one the assertion already produces, not a
    second one written for the record -- two messages for one failure is how they
    drift."""
    e = pgc_vacuity.Expect("verdict::reason")
    try:
        e.num(1, 2, "one equals two")
    except AssertionError as exc:
        raised = str(exc)
    expect.text(e.records[0].reason, raised,
                "the recorded reason is the message the assertion raised")


def test_the_assertions_before_a_failure_keep_their_verdicts(expect):
    """The verdicts are per assertion, not per test. A test that fails its third
    assertion made two real claims first, and a stream that marked the whole test
    would lose them."""
    e = pgc_vacuity.Expect("verdict::ordering")
    e.num(1, 1, "first, true")
    e.num(2, 2, "second, true")
    try:
        e.num(3, 4, "third, false")
    except AssertionError:
        pass
    e.num(5, 5, "fourth, after the catch")
    expect.ordered_rows([r.verdict for r in e.records],
                        ["PASS", "PASS", "FAIL", "PASS"],
                        "each assertion carries its own verdict, in order")


def test_a_delegated_assertion_records_fail_too(expect):
    """`outcomes` and `refusal` hand the comparison to pytest's own
    `assert_outcomes`, so the AssertionError is raised by code this layer does not
    write and carries a message it did not compose.

    That is the case a verdict passed in at the call site could not cover, and it
    is why the resolution wraps the comparison rather than describing it.
    """
    e = pgc_vacuity.Expect("verdict::delegated")

    class _FakeResult:
        ret = 0

        def assert_outcomes(self, **want):
            raise AssertionError("Outcomes do not match: expected passed=1")

    try:
        e.outcomes(_FakeResult(), "a delegated comparison", passed=1)
    except AssertionError:
        pass
    expect.num(len(e.records), 1, "premise: the delegated assertion was counted")
    expect.text(e.records[0].verdict, "FAIL",
                "and a failure raised by pytest's own code is still recorded")


def test_a_refusal_still_leaves_no_record(expect):
    """The boundary, restated for phase 2 because the resolution wraps a region
    that a refusal must stay outside of.

    Verified statically as well as here: in every recording method, each
    `VacuityError` is raised BEFORE the record is taken, so no refusal is ever
    inside the wrapped region. That is what keeps a refusal out of the stream
    without a special case for `VacuityError` being a subclass of AssertionError.
    """
    e = pgc_vacuity.Expect("verdict::refusal")
    try:
        e.num("100", 100, "a text comparison")
    except pgc_vacuity.VacuityError:
        expect.num(len(e.records), 0, "a refused assertion still records nothing")
    else:
        raise AssertionError("num() accepted a string, so this arm tested nothing")


def test_every_refusal_precedes_its_record(expect):
    """The static half of the arm above, so the invariant cannot drift silently.

    If somebody adds a `VacuityError` after the record is taken, the refusal lands
    inside the wrapped region and starts being recorded as a FAILED ASSERTION.
    Nothing else in the corpus would notice, and the stream would lie in the most
    misleading direction available -- a refusal reported as a real failure.

    THIS SCAN FOLLOWS CALLS, and the first version did not. It looked for a
    literal `raise VacuityError(...)` inside each method body, which would have
    missed a refusal raised through a helper -- and `_refuse_failed_query` is
    exactly such a helper, called by most of these methods. Measured while
    attacking this arm: 17 methods raise it directly, 18 can raise it once calls
    are followed, so the lexical scan was one method short of the real population.

    It happens that no method calls a refusing helper after its own record, so
    both scans agree today. The arm is the transitive one anyway, because the
    reason to write a guard is the case that does not exist yet.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(pgc_vacuity))
    methods = {}
    for cls in ast.walk(tree):
        if isinstance(cls, ast.ClassDef) and cls.name == "Expect":
            for fn in cls.body:
                if isinstance(fn, ast.FunctionDef):
                    methods[fn.name] = fn

    def _raises_here(fn):
        return any(isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call)
                   and isinstance(n.exc.func, ast.Name)
                   and n.exc.func.id == "VacuityError"
                   for n in ast.walk(fn))

    def _self_calls(fn):
        return [(n.func.attr, n.lineno) for n in ast.walk(fn)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Name) and n.func.value.id == "self"]

    # Close over calls, so a method that refuses only through a helper is in the
    # population too.
    refusing = {nm for nm, fn in methods.items() if _raises_here(fn)}
    growing = True
    while growing:
        growing = False
        for nm, fn in methods.items():
            if nm in refusing:
                continue
            if any(attr in refusing for attr, _ in _self_calls(fn)):
                refusing.add(nm)
                growing = True

    scanned = []
    offenders = []
    for nm, fn in methods.items():
        recs = [n.lineno for n in ast.walk(fn)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "_record"]
        if not recs:
            continue
        scanned.append(nm)
        first = min(recs)
        for n in ast.walk(fn):
            if (isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call)
                    and isinstance(n.exc.func, ast.Name)
                    and n.exc.func.id == "VacuityError" and n.lineno > first):
                offenders.append(f"{nm}:{n.lineno} raises it directly after the record")
        for attr, lineno in _self_calls(fn):
            if attr in refusing and lineno > first:
                offenders.append(f"{nm}:{lineno} calls self.{attr}() after the record")
    # THE PREMISE IS THE POPULATION, and it is the difference between "no method
    # offends" and "the scan matched no methods". An empty offender list is the
    # answer to both, and only one of them is good news.
    expect.at_least(len(scanned), 15, "premise: the scan found the recording methods")
    expect.rows(offenders, [], "no refusal is raised after its record is taken",
                allow_empty="an empty offender list is the pass, and the at_least above "
                            "is the population premise that makes it mean something")


def test_every_recording_method_resolves_its_verdict(expect):
    """THE LIST CANNOT DRIFT. A method that takes a record and is not wrapped
    records a PASS it never revisits, so its failures are invisible in the stream
    while the test still fails normally -- nothing else in the corpus would
    notice.

    Derived from the module, not from a list written here: the population is every
    method that calls `_record`, and the claim is that all of them are wrapped.
    A list would have to be updated by whoever adds the sixteenth, which is
    exactly the person who would forget.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(pgc_vacuity))
    recording = []
    for cls in ast.walk(tree):
        if not (isinstance(cls, ast.ClassDef) and cls.name == "Expect"):
            continue
        for fn in cls.body:
            if not isinstance(fn, ast.FunctionDef):
                continue
            if any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                   and n.func.attr == "_record" for n in ast.walk(fn)):
                recording.append(fn.name)

    expect.at_least(len(recording), 15,
                    "premise: the scan found the recording methods")
    unwrapped = sorted(
        nm for nm in recording
        if not getattr(getattr(pgc_vacuity.Expect, nm), "_pgc_resolves_verdict", False)
    )
    expect.rows(unwrapped, [], "every method that takes a record resolves its verdict",
                allow_empty="an empty unwrapped list is the pass, and the at_least above "
                            "is the population premise that makes it mean something")


def test_wrapping_a_method_twice_changes_nothing(expect):
    """WHAT THE ARM ABOVE CANNOT SEE, measured rather than left as a worry.

    @OffgridwithJD attacked the drift arm and named double-wrapping as the most
    reachable thing it would miss: the marker is on the outer wrapper, so a method
    wrapped twice looks exactly like one wrapped once.

    That is true, and it does not matter -- which is the answer, not an excuse.
    Both wrappers compute the same `taken` and resolve the same record to the same
    verdict and reason, because the inner call appends nothing before the outer one
    measures. Measured here rather than argued, because "I think it is harmless" is
    the sentence that has been wrong three times today.

    So the arm is not extended to catch it. A guard against a change that alters
    nothing is a false red waiting to happen, and this layer's budget forbids those
    more strictly than it forbids a gap.
    """
    e = pgc_vacuity.Expect("double::wrapped")
    original = pgc_vacuity.Expect.num
    pgc_vacuity.Expect.num = pgc_vacuity._resolving(original)
    try:
        try:
            e.num(1, 2, "a claim that is false")
        except AssertionError:
            pass
        e.num(3, 3, "a claim that is true")
    finally:
        pgc_vacuity.Expect.num = original
    expect.ordered_rows([r.verdict for r in e.records], ["FAIL", "PASS"],
                        "a doubly-wrapped method resolves exactly as a single one does")
    expect.num(e.count, 2, "and still takes one record per call")


def test_a_recording_method_takes_exactly_one_record_per_call(expect):
    """THE INVARIANT THE RESOLUTION RESTS ON, pinned because a mutation showed it
    was assumed.

    `_resolving` marks `self._records[taken]`. Replacing that with
    `self._records[-1]` left the whole corpus green, because one call appends at
    most one record and the two always name it. That is a property of the methods,
    not of the wrapper, and nothing was asserting it.

    If a method ever records twice, `[taken]` and `[-1]` stop agreeing, the
    wrapper marks the first and the second keeps a verdict nobody set. This is the
    arm that says so, rather than the comment.
    """
    e = pgc_vacuity.Expect("records::one-per-call")
    calls = [
        lambda: e.num(1, 1, "num"),
        lambda: e.text("a", "a", "text"),
        lambda: e.rows(["a"], ["a"], "rows"),
        lambda: e.ordered_rows(["a", "b"], ["a", "b"], "ordered_rows"),
        lambda: e.at_least(5, 1, "at_least"),
        lambda: e.differ("x", "y", "differ"),
        lambda: e.row_set(["a"], ["a"], "row_set -- delegates to rows"),
    ]
    deltas = []
    for call in calls:
        before = e.count
        call()
        deltas.append(e.count - before)
    expect.rows([str(d) for d in deltas], ["1"] * len(calls),
                "every call, including the delegating one, took exactly one record")


# ---- phase 3: the session's records reconcile, and the check can fail --------
#
# THE OBVIOUS RECONCILIATION HERE IS VACUOUS BY CONSTRUCTION, and phase 1 is what
# made it so. `count` IS `len(self._records)`, so reconciling the count against
# the records compares a value with its own definition. #937 warns twice that the
# shell side shipped `inputs == sum(buckets)` that could not go red, and both were
# caught only by mutating them -- shipping a third would be worse for having been
# warned.
#
# Partitioning the records into PASS/FAIL/UNRUN and checking the parts sum to the
# whole is the same trap wearing a different hat: the buckets are derived from the
# list being counted.
#
# So the reconciliation is between two routes that are genuinely different:
#
#   1. what the recorder HELD, read in the process that ran the test
#   2. what ARRIVED, read back off the report after it was built -- crossing the
#      report boundary, and under `-n` crossing a process boundary as well
#
# `_UnrunnableCollector` already records why that second route has to exist: a
# worker's own state is invisible to the controller, so the value has to travel on
# the report. Measured on the pinned runner, `user_properties` survive the xdist
# boundary intact, which is what makes route 2 available at all.


def test_the_session_totals_are_reconciled(pytester, expect):
    """The positive control. A clean run reports its totals and does not refuse."""
    pytester.makepyfile(
        """
        def test_two_claims(expect):
            expect.num(1, 1, "first")
            expect.num(2, 2, "second")

        def test_one_claim(expect):
            expect.text("a", "a", "third")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(result, "a clean run passes", passed=2, failed=0)
    result.stdout.fnmatch_lines(["*checks run: 3*"])


def test_the_total_separates_records_from_passes_and_from_tests(pytester, expect):
    """THREE DISTINCT NUMBERS, because two were not enough (@OffgridwithJD).

    My first version used an all-PASS fixture: five claims across two tests, so
    records 5 and passes 5. A totals line counted from PASSES would have been
    indistinguishable from one counted from records, and only the test count was
    separated. Measured on that fixture:

        checks run: 5
        accounting: 5 pass + 0 fail + 0 unrun = 5

    Making one of the five claims false and catching it gives three numbers that
    disagree, so the line can only be right for one reason:

        records 5    passes 4    tests 2

    One dead end recorded so it is not tried again: `cannot_run` contributes an
    UNRUN record but fails its own test, so an unrunnable fixture does not
    separate them either.
    """
    pytester.makepyfile(
        """
        def test_four_claims(expect):
            expect.num(1, 1, "a")
            expect.num(2, 2, "b")
            expect.num(3, 3, "c")
            try:
                expect.num(4, 99, "d -- deliberately false, and caught")
            except AssertionError:
                pass

        def test_one_claim(expect):
            expect.num(5, 5, "e")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(result, "premise: two tests, both passing", passed=2, failed=0)
    result.stdout.fnmatch_lines(["*checks run: 5*"])
    result.stdout.fnmatch_lines(["*accounting: 4 pass + 1 fail + 0 unrun = 5*"])


def test_a_record_lost_in_transport_is_refused(pytester, expect):
    """THE ARM THIS PHASE EXISTS FOR, and it is written before the reconciliation.

    A conftest that drops one record on its way onto the report is exactly the
    silent failure the two routes exist to catch: the recorder held three, two
    arrived, and without a reconciliation the run reports 2 and nobody knows a
    claim went missing.

    It has to be injected from a conftest because no in-tree code does this -- the
    point of the arm is that the reconciliation CAN fail, and an arm that waits for
    a real defect to appear is not evidence that it can.

    `tryfirst=True` IS LOAD-BEARING, NOT DECORATION. Both this hook and the
    layer's are wrappers, and a wrapper's code after its `yield` runs in the
    REVERSE of call order. My first version used `trylast`, which made this the
    innermost wrapper, so it ran before the layer attached anything and saw an
    empty `user_properties` -- the inner run then passed and the arm read exactly
    like a reconciliation that does not fire. Measured: the debug print inside the
    loop never executed.
    """
    pytester.makepyfile(
        """
        def test_three_claims(expect):
            expect.num(1, 1, "a")
            expect.num(2, 2, "b")
            expect.num(3, 3, "c")
        """
    )
    pytester.makeconftest(
        """
        import pytest

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_runtest_makereport(item, call):
            report = yield
            if call.when == "call":
                for i, (key, value) in enumerate(report.user_properties):
                    if key == "pgc_records" and value:
                        report.user_properties[i] = (key, value[:-1])
            return report
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.run_failed(result, "a record that did not arrive is refused")
    result.stderr.fnmatch_lines(["*held 3 record(s) and 2 arrived*"])


def test_the_two_values_are_not_aliases_of_one_list(pytester, expect):
    """The attack that FAILS, and it is stronger evidence than the xdist run.

    @OffgridwithJD's objection: the transport arm drops a record with `value[:-1]`,
    which COPIES. So the unfair-in-my-favour reading is that the report carries the
    recorder's own list and the only reason the arm fires is the slice.

    Mutating in place settles it. `value.pop()` reaches whatever object the report
    actually holds, and the run is still refused -- so `held` and `arrived` are not
    two views of one list. `held` is an int; `pgc_records` is a freshly built list
    of fresh tuples; there is no shared object to reach.

    WHY THIS IS THE STRONGER HALF. The xdist run proves the comparison survives
    serialisation. This proves the two values are not aliases, IN A SINGLE PROCESS,
    which xdist cannot show because serialisation copies everything by definition.
    """
    pytester.makepyfile(
        """
        def test_three_claims(expect):
            expect.num(1, 1, "a")
            expect.num(2, 2, "b")
            expect.num(3, 3, "c")
        """
    )
    pytester.makeconftest(
        """
        import pytest

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_runtest_makereport(item, call):
            report = yield
            if call.when == "call":
                for key, value in report.user_properties:
                    if key == "pgc_records" and value:
                        value.pop()          # IN PLACE, not a slice
            return report
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.run_failed(result, "an in-place removal is refused too")
    result.stderr.fnmatch_lines(["*held 3 record(s) and 2 arrived*"])


def test_a_verdict_outside_the_closed_set_is_refused(pytester, expect):
    """The schema half. A verdict the reader cannot key on is a record that says
    nothing, and `pgc_record` refuses the same thing on the shell side rather than
    dropping the check -- dropping it would leave the count bumped with no outcome,
    which is the reconciliation failure itself."""
    pytester.makepyfile(
        """
        def test_one_claim(expect):
            expect.num(1, 1, "a")
        """
    )
    pytester.makeconftest(
        """
        import pytest

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_runtest_makereport(item, call):
            report = yield
            if call.when == "call":
                for i, (key, value) in enumerate(report.user_properties):
                    if key == "pgc_records" and value:
                        report.user_properties[i] = (key, [("SORTOF", n) for _, n in value])
            return report
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.run_failed(result, "a verdict outside the closed set is refused")
    result.stderr.fnmatch_lines(["*SORTOF*"])


def test_the_recorder_is_only_observed_once_and_both_sides_of_that_are_blind(
        pytester, expect):
    """THE LIMIT, pinned in BOTH directions. My first version named half of it.

    Phase 3 compares two values taken from ONE read of the recorder at ONE
    instant, so anything that changes the recorder outside that instant is
    invisible. That has two halves and I pinned only the later one:

        a record appended AFTER the read     invisible -- run passes
        a record removed BEFORE the read     invisible -- run passes

    @OffgridwithJD injected the second and got a clean pass: `checks run: 2`,
    `accounting: 2 pass + 0 fail + 0 unrun = 2`, rc 0. **The early half is the
    more reachable one**, and that is the part my framing got backwards: a late
    append needs someone outside the layer to do it, while an early loss is what
    a bug inside the recorder would look like.

    THE REASON IS NARROWER THAN I WROTE, TOO. I said the controller has no
    recorder to consult under `-n`. The real reason needs no xdist at all: the
    `expect` fixture's teardown pops the recorder (`pgc_vacuity.py`, the `expect`
    fixture), so nothing after `makereport` can read it in a single process
    either.

    AND "NOT STRAIGHTFORWARDLY FIXABLE" OVERSTATED IT. @OffgridwithJD's proposal:
    keep the final COUNT -- an int, not the records -- in a session-level map that
    survives teardown, and reconcile the sum at worker-side `sessionfinish`, where
    the worker has its own slice and needs nothing from the controller. Today
    `sessionfinish` returns early for workers, which is correct for the
    collected-versus-reported check and is what forecloses this one. Unbuilt and
    unmeasured, so it is a named proposal rather than a plan.
    """
    pytester.makepyfile(
        """
        def test_three_claims(expect):
            expect.num(1, 1, "a")
            expect.num(2, 2, "b")
            expect.num(3, 3, "c")
        """
    )
    pytester.makeconftest(
        """
        import pytest
        import pgc_vacuity

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_runtest_makereport(item, call):
            report = yield
            if call.when == "call":
                rec = pgc_vacuity._RECORDERS.get(item.nodeid)
                if rec is not None:
                    rec._records.append(pgc_vacuity._Record("a record created LATE"))
            return report
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(result, "a LATE append does not refuse the run -- half the limit",
                    passed=1, failed=0)
    result.stdout.fnmatch_lines(["*checks run: 3*"])

    # THE OTHER HALF, and the more reachable one. trylast makes this the INNERMOST
    # wrapper, so it runs BEFORE the layer reads the recorder -- the mirror of the
    # tryfirst above.
    pytester.makeconftest(
        """
        import pytest
        import pgc_vacuity

        @pytest.hookimpl(wrapper=True, trylast=True)
        def pytest_runtest_makereport(item, call):
            report = yield
            if call.when == "call":
                rec = pgc_vacuity._RECORDERS.get(item.nodeid)
                if rec is not None and rec._records:
                    rec._records.pop()
            return report
        """
    )
    early = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(early, "an EARLY removal does not refuse it either",
                    passed=1, failed=0)
    early.stdout.fnmatch_lines(["*checks run: 2*"])
