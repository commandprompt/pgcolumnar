"""pgColumnar pytest harness: the vacuity-refusal layer.

A vacuity defect is a test that reports PASS while asserting nothing. Bare pytest
permits it in eight measured ways, all exiting 0, so this plugin is loaded for
every pgColumnar test rather than offered as a convention. The measurements are in
design/ISSUE_432_PYTEST_HARNESS.md section 4.

Two rules run the file:

  1. A test passes only if it made at least one COUNTED assertion. Counted means it
     went through the `expect` recorder. A bare Python `assert` is not forbidden,
     it simply does not satisfy the requirement, so a body that computes and
     concludes nothing fails.
  2. Every comparison refuses its own degenerate cases. Both sides empty, a value
     against itself, a substring where a typed field was meant.

The bash harness earned each of these. `check_num`, `check_ratio` and `check_text`
were added for issue #418 after "empty compared with empty" printed PASS, and
`check_num` refuses two identical md5 hashes for the same reason.
"""

import ast
import numbers
import pathlib
import sys

import functools
import itertools

import pytest

# The sentinel a failed query yields, mirroring lib.sh's `res="QUERY_ERROR.$seq"`.
#
# THE COMMENT HERE USED TO CLAIM "unique per occurrence so two failing queries can
# never compare equal", WHICH WAS FALSE OF A CONSTANT. `QUERY_ERROR == QUERY_ERROR`,
# so two failures that both assigned it compared EQUAL, and a test comparing one
# failed query against another passed. Measured before the fix: `expect.text`,
# `expect.rows`, `expect.row_set` and `expect.ordered_rows` all passed with the
# sentinel on both sides.
#
# lib.sh does not have this problem because its sentinel is PRODUCED, with a
# sequence number, by the one helper every suite calls. The port had the constant
# and no producer, so uniqueness was a sentence rather than a mechanism.
#
# QUERY_ERROR stays as the PREFIX every refusal matches on. `query_error()` is the
# producer, and it is what a caller should use.
QUERY_ERROR = "QUERY_ERROR"
EMPTY = "EMPTY"

_query_error_seq = itertools.count(1)


# THE PREFIX IS BOUND AT DEFINITION TIME, in a default argument, and that is the whole
# mechanism rather than a style choice.
#
# The first version read the module global `QUERY_ERROR` in both the producer and the
# refusal. Every `conftest.py` under test/pytest/ is imported before collection, so a
# corpus file can rewrite that global -- and because BOTH sides read it, they moved
# together and the refusal matched whatever the prefix had just been set to. The arm
# that was supposed to catch this minted its sentinels AFTER rewriting, so it could not
# fail for the property it named: tautological, and @jdatcmd measured it.
#
# The faithful hatch mints while armed and rewrites afterwards, which is what a corpus
# file actually does. Measured against the first version: with the rewrite, two minted
# sentinels were COMPARED instead of refused, and a corpus file doing it end to end
# reported `1 passed` over two failed queries -- `error-swallowed-to-empty`
# reintroduced through the hatch the change claimed to close.
#
# A default argument is evaluated once, when the function is defined, and is not read
# from the module namespace afterwards. So rewriting `pgc_vacuity.QUERY_ERROR` changes
# neither what is minted nor what is refused. `QUERY_ERROR` stays exported because the
# corpus names it in literals, and it is no longer what the mechanism reads.
def query_error(detail="", _prefix=QUERY_ERROR):
    """The value a failed query yields: unique per occurrence, by construction.

    Two failures can never compare equal, which is the whole mechanism -- a helper
    that turns every failure into one falsy value makes "both queries failed" look
    exactly like "both queries agreed". lib.sh closed this with a sequence number
    per failure and this is the port of that, not of the constant.

    The sequence is per process. Under xdist each worker is its own process, so two
    workers can mint the same number -- which is harmless, because a comparison only
    ever happens inside one test, and the refusals below match the PREFIX rather
    than any particular number.
    """
    n = next(_query_error_seq)
    return f"{_prefix}.{n}.{detail}" if detail else f"{_prefix}.{n}"


def _failed_query(v, _prefix=QUERY_ERROR):
    """Is this value a failed query's sentinel? Matches the prefix, at any depth.

    A sentinel arrives as a CELL inside a row as often as it arrives as a whole
    side -- `[(QUERY_ERROR,)]` is what a one-column query that failed looks like
    after a helper swallowed the error -- so the walk is the point rather than a
    convenience. Strings only: a tuple is walked, not tested.
    """
    if isinstance(v, str):
        return v.startswith(_prefix)
    if isinstance(v, (list, tuple, set, frozenset)):
        return any(_failed_query(x, _prefix) for x in v)
    return False

# Reasons a test may declare itself unrunnable. Closed, exactly as lib.sh keeps it
# closed, so "skipped" cannot become a way to stop asserting things quietly.
UNRUNNABLE_REASONS = (
    "MISSING_DEPENDENCY",
    "UNSUPPORTED_MAJOR",
    "ABSENT_FIXTURE",
    "UNAVAILABLE_ENDPOINT",
    "UNMET_PRECONDITION",
)

# Keyed by nodeid rather than held on the item, so an xdist worker sees only its
# own tests and two workers cannot share a counter.
_RECORDERS = {}

# Writes seen during one test, keyed by nodeid exactly as _RECORDERS is, and for the
# same reason: `pytester` runs this layer's own tests IN-PROCESS, so an inner run
# imports this module and a single shared list would mix the two sessions together.
_WRITES = {}

# The command tags that mean rows were supposed to move. The server reports these,
# so this guard never parses SQL -- see test_writes_wrote_rows.py for the measured
# table of statusmessage against rowcount. MERGE is PG 15+, and is listed because a
# port that starts using it should not silently fall outside the guard.
_WRITE_TAGS = ("INSERT", "UPDATE", "DELETE", "MERGE", "COPY")


class _Record:
    """One counted assertion's outcome. #937.

    THE COUNT IS THIS LIST'S LENGTH, which is the whole design. The shell harness
    keeps `PGC_CHECKS` and a record stream honest by reconciling them, because in
    bash they must be two things. Here they need not be, so the count is a derived
    property and `_records` is the only state -- nothing can increment a count
    without a record existing, by construction rather than by discipline.

    AN OBJECT, NOT A FORMATTED LINE, and that is load-bearing rather than
    idiomatic. The shell's record is tab separated, so `pgc_record` had to strip
    tabs AND newlines out of a check name: measured there, a tab gave a record of
    four fields and a newline gave two lines. A name is carried here as an
    attribute, so there is no separator to smuggle -- and test_check_records.py
    asserts that with a name holding both, because the moment these become a line
    the whole class of defect returns and nothing would say so.

    The verdict is filled in by the phase that resolves it from the outcome; a
    record created here is an assertion that ran, which is the fact the call site
    knows.
    """

    __slots__ = ("name", "verdict", "reason")

    def __init__(self, name, verdict="PASS", reason=""):
        self.name = name
        self.verdict = verdict
        self.reason = reason


class _Write:
    """One write statement's outcome.

    THE ORDINAL IS AMONG ALL THE TEST'S WRITES, not among the empty ones. The
    refusal numbered the survivors it was about to print, so "#1" meant "the first
    one I am complaining about" and identified no statement -- a reader counting
    writes in the source went to the wrong line. Found by attacking this guard with
    three writes where the empty one is the second: the filtered number said #1 and
    the real answer was #2.
    """

    __slots__ = ("tag", "count", "acknowledged", "ordinal")

    def __init__(self, tag, count, ordinal):
        self.tag = tag
        self.count = count
        self.ordinal = ordinal
        self.acknowledged = False


def note_write(nodeid, cur):
    """Record a statement if it was a write, from the cursor the server answered on.

    THE TAG DECIDES, NOT THE ROW COUNT. `SELECT 0` and `INSERT 0 0` both carry
    rowcount 0, so a guard keyed on the count alone would refuse every test whose
    last statement was a SELECT over an empty result -- a legitimate assertion.
    `statusmessage` is the server's own command tag, which separates them without
    this code ever looking at the SQL.
    """
    message = getattr(cur, "statusmessage", None)
    if not message:
        return None
    tag = str(message).split(" ", 1)[0].upper()
    if tag not in _WRITE_TAGS:
        return None
    count = getattr(cur, "rowcount", -1)
    seen = _WRITES.setdefault(nodeid, [])
    w = _Write(tag, count, len(seen) + 1)
    seen.append(w)
    # STAMPED ON THE OBJECT THAT RAN IT, so an acknowledgement is about a statement
    # rather than about a pair of numbers. Two writes can carry the same tag and the
    # same count -- one accidental, one deliberate -- and matching on those
    # acknowledged whichever came first, marking the accidental one as named and
    # reporting the deliberate one instead.
    #
    # THIS STAMP REACHES A STUB AND NOT A REAL CURSOR, measured rather than assumed: a
    # `psycopg.Cursor`, a `ServerCursor` and the cursor `conn.execute` returns all
    # raise AttributeError here, so on every real write this `except` swallowed it and
    # `wrote` fell back to matching by value -- which made the absolute ordinal name
    # the wrong statement in exactly the case it was added for. The caller-facing
    # object is stamped by `_WatchedCursor` instead, and that is the stamp `wrote`
    # finds. This one serves the layer's own arms, which pass a stub.
    # Found by @jdatcmd, whose point was that the stub is stampable and the real
    # cursor is not, so the arms could not see it.
    try:
        cur._pgc_write = w
    except (AttributeError, TypeError):
        pass
    return w

# lib.sh:58 PGC_EXIT_INCOMPLETE. The same number deliberately: a suite that could
# not evaluate something exits 67 there, and a runner that learns the code learns
# it once. pytest itself uses 0-6 (`pytest.ExitCode`), so 67 collides with
# nothing.
EXIT_INCOMPLETE = 67

# THE CLOSED SET A RECORD'S VERDICT MUST COME FROM. #937 phase 3.
#
# lib.sh carries the same four values and `pgc_record` refuses an unknown one
# rather than dropping the check -- dropping it would leave the count bumped with
# no outcome recorded, which is the reconciliation failure itself. SKIP has no
# counterpart here: a bare skip is refused at collection, and the honest form is
# expect.cannot_run(), which records UNRUN.
RECORD_VERDICTS = ("PASS", "FAIL", "UNRUN")


class VacuityError(AssertionError):
    """Raised when an assertion could not have failed, or asserted nothing."""


def _is_number(v):
    # bool is an int in Python. A count that is True rather than 1 is a bug, not a
    # number, so it is refused rather than silently compared.
    return isinstance(v, numbers.Number) and not isinstance(v, bool)


def _empty(v):
    return v is None or (hasattr(v, "__len__") and len(v) == 0)


def _is_sqlstate(v):
    # Five characters of [0-9A-Z], per SQL/PostgreSQL. Explicit ranges rather than
    # str.isdigit(), which is True for other scripts' digits.
    return (isinstance(v, str) and len(v) == 5
            and all(("0" <= c <= "9") or ("A" <= c <= "Z") for c in v))


def _plan_nodes(node):
    """Every node of an EXPLAIN (FORMAT JSON) tree, as parsed by psycopg."""
    if isinstance(node, dict):
        yield node
        for key in ("Plan", "Plans"):
            child = node.get(key)
            if isinstance(child, dict):
                yield from _plan_nodes(child)
            elif isinstance(child, list):
                for entry in child:
                    yield from _plan_nodes(entry)
    elif isinstance(node, list):
        for entry in node:
            yield from _plan_nodes(entry)


def _resolving(method):
    """Set the verdict of the record this call took, from the outcome. #937 phase 2.

    WHY NOT FROM THE EXCEPTION IN `pytest_runtest_call`. That was the first
    design, and @OffgridwithJD refuted it: proving a guard REFUSES means catching
    the AssertionError, which five tests in this corpus do (test_ordered.py:243,
    test_failed_query_sentinel.py:236, :326, :357, :382). Measured before this:

        count before/mid/after: 0 / 1 / 2
          record 0  'this comparison must fail'   verdict PASS   <- this RAISED
          record 1  'and the test continues'      verdict PASS
        1 passed

    A genuinely failed assertion stayed PASS, in a passing test, with no exception
    reaching the hook. Here the resolution is INSIDE the assertion call, so it
    happens before any `except` in the test body can see the error.

    WHY A WRAPPER RATHER THAN A VERDICT PASSED AT THE CALL SITE. `outcomes` and
    `refusal` delegate to pytest's own `assert_outcomes`, which raises a message
    this layer never composes -- there is no verdict for the call site to pass. A
    wrapper covers those without the assertion methods knowing they are wrapped.

    IT MARKS THE RECORD THIS CALL TOOK, BY INDEX -- AND TODAY THAT IS THE SAME
    RECORD AS THE LAST ONE. The first version of this comment claimed the index
    form was needed to survive nesting, and a mutation refuted it: replacing
    `self._records[taken]` with `self._records[-1]` left all 235 tests green,
    because nothing distinguishes them. `row_set` delegates to `rows`, but
    `row_set` takes no record of its own, so one call appends at most one record
    and the two expressions always name it.

    The index form is kept because it stays correct if that stops being true, and
    the invariant it depends on is now PINNED rather than assumed:
    test_check_records.py asserts every recording method takes exactly one record
    per call. If someone writes one that records twice, that arm reddens and this
    comment is still true -- which is the opposite of how the first version of it
    would have aged.

    WRAPPING TWICE CHANGES NOTHING, and the drift arm deliberately does not look
    for it. The marker sits on the outer wrapper, so a doubly-wrapped method is
    indistinguishable from a singly-wrapped one -- @OffgridwithJD named that as the
    gap most likely to be reached. Measured: both wrappers compute the same
    `taken` and write the same verdict and reason, because the inner call appends
    nothing before the outer one measures. An arm against a change that alters no
    behaviour would be a false red waiting to happen.

    A REFUSAL MARKS NOTHING, and that needs no special case for VacuityError being
    an AssertionError subclass: every VacuityError in the recording methods is
    raised BEFORE the record is taken, so no record exists to mark. That is not an
    accident to rely on -- test_check_records.py scans the module and fails if
    anyone adds one after.
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        taken = len(self._records)
        try:
            return method(self, *args, **kwargs)
        except AssertionError as exc:
            if len(self._records) > taken:
                rec = self._records[taken]
                rec.verdict = "FAIL"
                rec.reason = str(exc)
            raise

    wrapper._pgc_resolves_verdict = True
    return wrapper


class Expect:
    """Records assertions, and refuses the ones that could not have failed."""

    def __init__(self, nodeid):
        self.nodeid = nodeid
        self._records = []
        self.unrunnable = None

    @property
    def records(self):
        """The assertions this test has concluded, in the order it concluded them.

        A TUPLE, so a caller cannot append to the stream without going through the
        recorder. `pytest_runtest_call` reaches the list itself to resolve the
        verdict of the assertion that raised; everything else reads this.
        """
        return tuple(self._records)

    @property
    def count(self):
        """How many assertions were counted. NOT a second variable.

        There is no setter, deliberately. An arm in test_check_records.py asserts
        that assigning to it raises, because a test checking only that the count
        AGREES with the records would pass on an implementation that keeps two
        numbers and happens to update both -- which is exactly the drift the shell
        side needs a reconciliation to catch.
        """
        return len(self._records)

    # -- the recorder -------------------------------------------------------
    def _refuse_failed_query(self, name, got, want):
        """Refuse a comparison where either side is a failed query.

        ONE definition, called by every comparison, so an assertion added later
        inherits it instead of being the next hole. `hash` had its own copy of this
        and four other assertions had none: `text`, `rows`, `row_set` (which
        delegates to `rows`) and `ordered_rows` each passed with the sentinel on
        both sides, which is `error-swallowed-to-empty` exactly -- two queries
        raise, a helper turns each into the same value, and they compare equal.
        """
        for side, v in (("left", got), ("right", want)):
            if _failed_query(v):
                raise VacuityError(
                    f"{name}: the {side} side is a failed query: {v!r}. Two failures "
                    f"compare equal, so this assertion cannot fail. Use "
                    f"query_error() so each failure is distinct, and assert the "
                    f"failure you expect rather than comparing two of them."
                )

    def _record(self, name, verdict="PASS", reason=""):
        """Count an assertion by recording it. One operation, no other path.

        Called BEFORE the comparison, at every site, because that is where the
        call site knows the assertion ran. The verdict is not passed in: passing it
        would need the outcome, which would split this back into two steps.

        SO EVERY RECORD IS `PASS` UNTIL A LATER PHASE SETS IT, AND THAT PHASE
        CANNOT RESOLVE IT FROM THE EXCEPTION. The first version of this comment
        argued it could: assertions in a body are sequential and a raise ends the
        test, so the failing assertion would be the last record. **That is false
        here, and the corpus is what makes it false** -- proving a guard refuses
        means catching the AssertionError, which five tests do
        (test_ordered.py:243, test_failed_query_sentinel.py:236, :326, :357, :382).
        Driven on this branch:

            count before/mid/after: 0 / 1 / 2
              record 0  'this comparison must fail'   verdict PASS   <- this RAISED
              record 1  'and the test continues'      verdict PASS
            1 passed

        A genuinely failed assertion stays PASS, in a passing test, and nothing
        reaches `pytest_runtest_call` to correct it. Found by @OffgridwithJD
        attacking the argument rather than the code.

        The verdict therefore has to be set on the comparison's own path, where
        the outcome is known and no propagation is needed. That stays one
        operation; it is phase 2's work and is not claimed here. What IS claimed
        here is the count, which the probe above shows is 2 and correct.
        """
        self._records.append(_Record(name, verdict, reason))

    # -- numbers -----------------------------------------------------------
    @_resolving
    def num(self, got, want, name):
        """Compare two numbers. Refuses anything that is not a number.

        A string "100" compared with 100 is the psql-text-parsing bug this harness
        exists to remove, so it is refused rather than coerced.
        """
        if not _is_number(got) or not _is_number(want):
            raise VacuityError(
                f"{name}: num() needs numbers on both sides, got "
                f"{type(got).__name__}={got!r} and {type(want).__name__}={want!r}. "
                f"A text comparison here is the defect this harness removes."
            )
        self._record(name)
        if got != want:
            raise AssertionError(f"{name}: got {got!r} want {want!r}")

    def row_set(self, got, want, name, allow_empty=None):
        """Compare two result sets as SETS, order deliberately ignored.

        The counterpart to ordered_rows, and the port of pgc_set_hash. It exists so
        that ignoring order is DECLARED rather than smuggled in by sorting at the
        call site: `ordered_rows(sorted(x), ...)` reads like an ordering claim and is
        not one, which is why the collection scan refuses it.

        pgc_check_ordered_oracle asserts three things, and this is the third: the set
        oracle must be order-blind BY DESIGN. Without a control proving the two
        instruments differ, an ordered oracle could quietly be implemented as a set
        one and every ordering test in the tree would go silent.
        """
        # BEFORE the repr mapping, not after. row_set hands `rows` a list of repr
        # STRINGS, and `repr(("QUERY_ERROR.1",))` is `"('QUERY_ERROR.1',)"` -- which
        # does not start with the prefix, so the refusal inside `rows` cannot see a
        # sentinel that arrived as a cell. Delegating an assertion does not delegate
        # its refusals when the delegation transforms the data.
        self._refuse_failed_query(name, got, want)
        self.rows(sorted(map(repr, got)), sorted(map(repr, want)), name,
                  allow_empty=allow_empty)

    # -- ordered sequences ---------------------------------------------------
    @_resolving
    def ordered_rows(self, got, want, name):
        """Compare two sequences IN ORDER, refusing the cases where order says nothing.

        This is the port of `pgc_seq_hash` and `diff_query_ordered`, which the harness
        has had since #418 and this layer did not. It compares the sequences rather
        than hashing them, for the same reason `rows` does: a mismatch names the
        position, where a hash mismatch only says two hashes differ.

        THE REFUSAL THAT MATTERS IS THE SECOND ONE. A sequence whose elements are all
        equal reads the same forwards and backwards, so an ordering claim about it
        cannot fail. That is `pgc_check_ordered_oracle`'s premise inverted: the bash
        version proves its oracle order-sensitive by requiring forward != reverse on a
        known fixture, and the same requirement applied to a caller's data is what
        stops an ordered assertion being decorative.
        """
        g, w = list(got), list(want)
        if not g and not w:
            raise VacuityError(
                f"{name}: both sequences are empty, so this comparison could not "
                f"have failed. Use rows(..., allow_empty='why') if empty is the point."
            )
        self._refuse_failed_query(name, g, w)
        if len(set(map(repr, g))) < 2 and len(set(map(repr, w))) < 2:
            raise VacuityError(
                f"{name}: order cannot be observed in these sequences. Every element "
                f"is the same, so the reverse ordering is identical and the claim "
                f"asserts nothing beyond what rows() already asserts."
            )
        self._record(name)
        if g != w:
            for i, (a, b) in enumerate(zip(g, w)):
                if a != b:
                    raise AssertionError(
                        f"{name}: first difference at position {i}: got {a!r} want {b!r}"
                    )
            raise AssertionError(
                f"{name}: same prefix, different length: got {len(g)} rows want {len(w)}"
            )

    @_resolving
    def ordering_observable(self, forward, reverse, name):
        """Assert this fixture can distinguish order at all, before relying on it.

        `pgc_check_ordered_oracle` ported. Read the same rows both ways and require
        the two to differ: a fixture that reads identically forwards and backwards
        supports no ordering claim, and every ordered assertion over it is vacuous
        however carefully it is written.
        """
        # A FAILED QUERY ON EITHER SIDE, BEFORE ANYTHING ELSE. This assertion takes
        # (forward, reverse) rather than (got, want), so it sat outside the refusal --
        # and making the sentinel UNIQUE turned a loud red into a silent pass here.
        # Measured by @jdatcmd: with the old constant, two failed readings were
        # identical and this arm went RED; with two minted sentinels they differ, so it
        # went GREEN and greenlit every ordered assertion resting on the premise. That
        # was the one place the producer made the layer strictly weaker than before.
        self._refuse_failed_query(name, forward, reverse)
        f, r = list(forward), list(reverse)
        if not f and not r:
            raise VacuityError(f"{name}: both directions are empty.")
        self._record(name)
        if f == r:
            raise AssertionError(
                f"{name}: the forward and reverse readings are identical, so nothing "
                f"in this fixture can detect an ordering error. Give it rows whose "
                f"order is observable before asserting order."
            )

    # -- inequality ----------------------------------------------------------
    @_resolving
    def differ(self, got, want, name):
        """Assert that two arms of an A/B are observably different.

        `mutation-arm-unobservable`: both arms produce the identical answer and both
        are green, because the assertion that would catch it is the one nobody writes.
        Before this the layer had eight helpers asserting equality and one asserting
        inequality -- `ordering_observable`, specific to a forward/reverse pair -- so
        the general case was hand-rolled as `expect.num(int(after != before), 1, ...)`
        at two sites. That idiom throws BOTH VALUES AWAY: when it fails it says
        `got 0 want 1`, and a reader cannot tell arms that were both empty from arms
        that were both wrong from arms correctly identical. Three defects, one message.

        TWO FAILED QUERIES ARE NOT TWO ARMS, and that refusal is the inverse of the
        one #930 added. `query_error()` makes each failure UNIQUE precisely so two
        failures cannot compare EQUAL and pass an equality assertion -- which makes
        them compare UNEQUAL, so an arms-differ assertion passes on a pair of
        statements that both blew up. Measured: two calls give
        `QUERY_ERROR.1.<detail>` and `QUERY_ERROR.2.<detail>`, which are `!=`. The fix
        for one direction opened the other, which is why this is checked rather than
        inherited from the equality helpers' refusal.
        """
        for side, v in (("left", got), ("right", want)):
            if _failed_query(v):
                raise VacuityError(
                    # ONE UNBREAKABLE TOKEN FIRST: pytest word-wraps a long traceback
                    # line, so an arm matching a multi-word phrase against a single
                    # `E` line can miss a message that contains it.
                    f"{name}: failed-query-is-not-an-arm: the {side} arm is a failed "
                    f"query: {v!r}. query_error() makes each failure unique, so two "
                    f"failures do not compare equal -- which means they DIFFER, and "
                    f"this assertion would report the mutation as observable. Assert "
                    f"the failure you expect instead of differencing two of them."
                )
        self._record(name)
        if got == want:
            raise AssertionError(
                f"{name}: arms-do-not-differ: {got!r} on both arms. An A/B whose arms "
                f"agree cannot show that the thing between them did anything."
            )

    # -- row counts ---------------------------------------------------------
    def rowcount(self, got, want, name):
        """Compare a row count, refusing psycopg's "no count available" sentinel.

        cursor.rowcount is -1 when the statement produced no count, and measured on
        a live server it is 1 for an unfetched SELECT -- neither is a number of
        rows. Both are numbers, so expect.num compares them happily: num(-1, -1)
        passes. A count that matters should come from count(*) or from len() of the
        rows actually fetched.
        """
        for side, v in (("left", got), ("right", want)):
            if v == -1:
                raise VacuityError(
                    f"{name}: the {side} side is -1, which is psycopg's "
                    f"\"no row count available\" and not a number of rows."
                )
        self.num(got, want, name)

    def wrote(self, cur, want, name):
        """Assert how many rows a write actually wrote, and acknowledge a zero.

        Two jobs in one call, deliberately. It compares the count, and it marks the
        write as NAMED so the session guard does not refuse it. A zero-row write is
        legitimate when it is the thing being asserted -- a DELETE that must match
        nothing is a real negative control -- and the way to say so is to say the
        number. An unnamed zero stays a failure.

        A rowcount of -1 is refused rather than compared, for the reason
        `expect.rowcount` records: it is psycopg's "no count available", so DDL
        reaches this with -1 and comparing it to 0 would read as a mismatch, which
        is the right verdict for the wrong reason.
        """
        count = getattr(cur, "rowcount", None)
        if count is None:
            raise VacuityError(
                f"{name}: wrote() needs the cursor the statement ran on, and "
                f"{type(cur).__name__} has no rowcount."
            )
        if count == -1:
            raise VacuityError(
                # Same reason as above for the single token.
                f"{name}: no-count-available: the statement reported -1, which is "
                f"psycopg's \"no row count\" and not a number of rows. A statement "
                f"with no count is not a write whose rows can be asserted."
            )
        tag = str(getattr(cur, "statusmessage", "") or "").split(" ", 1)[0].upper()
        if tag not in _WRITE_TAGS:
            raise VacuityError(
                # A SELECT matching nothing reports `SELECT 0` with rowcount 0, so
                # this compared 0 with 0 and PASSED -- asserting "this write wrote no
                # rows" about a statement that is not a write. It read as a deliberate
                # zero and pinned nothing, which is this layer's own subject appearing
                # inside the assertion meant to close it.
                f"{name}: not-a-write: the statement reported tag "
                f"{tag or '(none)'!s}, which is not one of {', '.join(_WRITE_TAGS)}. "
                f"wrote() asserts how many rows a WRITE moved; for a query that "
                f"returned no rows, assert the rows."
            )
        # BY IDENTITY FIRST: the write stamped on this cursor is the statement this
        # call is about. The value match is the fallback for a cursor that could not
        # be stamped, and it is why the ordinal in the refusal is absolute.
        w = getattr(cur, "_pgc_write", None)
        if w is not None and not w.acknowledged:
            w.acknowledged = True
        else:
            for candidate in _WRITES.get(self.nodeid, ()):
                if not candidate.acknowledged and candidate.count == count \
                        and candidate.tag == tag:
                    candidate.acknowledged = True
                    break
        self.num(count, want, name)

    # -- row sets ----------------------------------------------------------
    @_resolving
    def rows(self, got, want, name, allow_empty=None):
        """Compare two result sets. Refuses two empty sides unless declared.

        Both sides empty is issue #418: it passes while asserting nothing, because
        a query that failed to return anything looks exactly like one that
        correctly returned nothing. `allow_empty` takes a REASON, not a flag, so
        the escape hatch costs more to type than the honest assertion.
        """
        self._refuse_failed_query(name, got, want)
        # THE REASON IS ENFORCED, not merely documented (#1031). This read
        # `not allow_empty`, a truthiness test, so `allow_empty=True` satisfied it and
        # carried nothing -- which made the escape hatch cost LESS to type than the honest
        # assertion, the opposite of what the docstring above argues for. Measured before
        # this check: `allow_empty=True` and `allow_empty=1` both passed.
        #
        # CHECKED WHENEVER IT IS GIVEN, not only when both sides turn out to be empty. A
        # flag form in a test whose sides happen to be non-empty would otherwise pass
        # today and refuse on the day the data changes, which is the worst moment to
        # learn it.
        #
        # `row_set` forwards this argument, so it inherits the refusal rather than
        # offering a way around it.
        if allow_empty is not None and not (isinstance(allow_empty, str)
                                            and allow_empty.strip()):
            raise VacuityError(
                f"{name}: allow_empty takes a REASON, not a flag, and got "
                f"{allow_empty!r}. The hatch exists so an empty-on-both-sides "
                f"comparison carries its justification where someone auditing "
                f"allow_empty= can read it. Pass allow_empty='why it is empty'."
            )
        if _empty(got) and _empty(want) and not allow_empty:
            raise VacuityError(
                f"{name}: both sides are empty, so this comparison could not have "
                f"failed. If an empty result is the point, pass "
                f"allow_empty='why it is empty'."
            )
        self._record(name)
        if list(got) != list(want):
            raise AssertionError(f"{name}: got {got!r} want {want!r}")

    # -- hashes and oracles ------------------------------------------------
    @_resolving
    def hash(self, got, want, name):
        """Compare two oracle hashes. Refuses self-comparison and error sentinels."""
        if got is want:
            raise VacuityError(
                f"{name}: the same object is compared against itself, so this "
                f"could not have failed."
            )
        # The same definition the others use. This was the only assertion that
        # refused a sentinel, and it did so with its own copy of the test.
        self._refuse_failed_query(name, got, want)
        if _empty(got) and _empty(want):
            raise VacuityError(f"{name}: both hashes are empty.")
        self._record(name)
        if got != want:
            raise AssertionError(f"{name}: got {got!r} want {want!r}")

    # -- text --------------------------------------------------------------
    @_resolving
    def text(self, got, want, name):
        """Compare text exactly. Refuses an empty expectation and a failed query."""
        self._refuse_failed_query(name, got, want)
        if _empty(want):
            raise VacuityError(
                f"{name}: the expected text is empty, so anything empty satisfies it."
            )
        self._record(name)
        if got != want:
            raise AssertionError(f"{name}: got {got!r} want {want!r}")

    # -- SQLSTATE ----------------------------------------------------------
    @_resolving
    def sqlstate(self, exc, want, name):
        """Assert a raised database error carries EXACTLY this SQLSTATE.

        `pytest.raises(psycopg.Error)` asserts that one of 254 SQLSTATEs arrived,
        across 42 SQLSTATE classes -- measured against psycopg 3.3.5 in the audit
        container by counting the classes in `psycopg.errors` that carry a
        `sqlstate` and subclass `psycopg.Error`. An unrelated failure of the same
        family satisfies it, and the worst case is not even a server error: a
        connect to a socket that does not exist raises `OperationalError` with
        `sqlstate` None, having never reached a server at all.

        So this is the typed field that says WHICH error, and it is the same move
        `plan_marker` makes: a typed field rather than a substring of a message.
        `str(exc.value).count("does not exist")` is the grep this layer exists to
        remove, wearing a different spelling.

        `want` may be a tuple when an error code legitimately differs across
        majors -- this tree supports 15 through 19. Every member is still checked
        to be a real SQLSTATE, so a tuple widens the claim by exactly the codes it
        names and no further.

        Refuses, rather than compares:

        - a `want` that is not five characters of [0-9A-Z]. `""` and `None` are
          satisfied by nothing, and `"42"` is a SQLSTATE CLASS -- a prefix claim
          wearing the spelling of an exact one.
        - an `exc` with no `sqlstate` attribute at all. The usual cause is passing
          pytest's `ExceptionInfo` instead of `exc.value`, which would otherwise
          compare `None` against a real SQLSTATE for ever.
        """
        wants = tuple(want) if isinstance(want, (tuple, list)) else (want,)
        if not wants:
            raise VacuityError(
                f"{name}: an empty set of SQLSTATEs is satisfied by nothing, so "
                f"this could not have passed and asserts nothing about which "
                f"error arrived."
            )
        for w in wants:
            if not _is_sqlstate(w):
                raise VacuityError(
                    f"{name}: {w!r} is not a SQLSTATE. A SQLSTATE is five "
                    f"characters of [0-9A-Z]; a two-character class is a prefix "
                    f"claim, and an empty one names no error."
                )
        if not hasattr(exc, "sqlstate"):
            raise VacuityError(
                f"{name}: a {type(exc).__name__} carries no sqlstate, so this "
                f"comparison is about the wrong object. Pass the exception itself: "
                f"`exc.value` inside a `with pytest.raises(...) as exc` block, not "
                f"`exc`."
            )
        self._record(name)
        got = exc.sqlstate
        if got is None:
            raise AssertionError(
                f"{name}: a {type(exc).__name__} carrying no SQLSTATE, so the "
                f"failure never reached the server: {exc}. Wanted {want!r}."
            )
        if got not in wants:
            raise AssertionError(f"{name}: got SQLSTATE {got!r} want {want!r}")

    # -- plans -------------------------------------------------------------
    @_resolving
    def plan_node(self, plan, node_type=None, provider=None, name=None):
        """Assert a node exists, by EXACT equality on a typed EXPLAIN JSON field.

        `EXPLAIN (FORMAT JSON)` arrives from psycopg as parsed Python, so there is
        no text to grep, and equality on a typed field cannot be satisfied by a
        superstring the way `grep ColumnarScan` was by `PgColumnarScan`.

        BUT `provider="PgColumnarScan"` DOES NOT MEAN "a columnar SCAN". Measured:
        the vectorized aggregate node reuses the scan's registered methods
        (`columnar_vector.c:806` assigns `&pgcolumnar_scan_methods`), so every
        pgcolumnar node reports that provider. With the vector aggregate engaged
        there is a single Custom Scan node carrying `Columnar Vectorized
        Aggregates` and NO `Columnar Projected Columns`, and this predicate still
        says yes. `pgc_is_columnar_scan` says no, because it greps for the marker.

        So use `plan_marker` to ask "did the columnar SCAN run". Use this to ask
        "is there a pgcolumnar node at all", which is a weaker and rarer question.
        """
        if node_type is None and provider is None:
            raise VacuityError(
                "plan_node() needs node_type or provider, or it asserts nothing."
            )
        label = name or f"plan has node_type={node_type!r} provider={provider!r}"

        seen_types, seen_providers = [], []
        for node in _plan_nodes(plan):
            nt = node.get("Node Type")
            pv = node.get("Custom Plan Provider")
            if nt is not None:
                seen_types.append(nt)
            if pv is not None:
                seen_providers.append(pv)
            if node_type is not None and nt != node_type:
                continue
            if provider is not None and pv != provider:
                continue
            self._record(name)
            return node

        raise AssertionError(
            f"{label}: no node whose fields match exactly. "
            f"Node Type values present: {seen_types!r}. "
            f"Custom Plan Provider values present: {seen_providers!r}."
        )

    # -- bounds -------------------------------------------------------------
    @_resolving
    def at_least(self, got, floor, name):
        """Assert got >= floor. Both sides must be numbers.

        The bash harness spells this as a yes/no string built by `[ ... -ge N ]`,
        which turns a number into text and then compares text. Keeping it numeric
        means a non-number is refused instead of silently becoming "no".
        """
        if not _is_number(got) or not _is_number(floor):
            raise VacuityError(
                f"{name}: at_least() needs numbers, got "
                f"{type(got).__name__}={got!r} and {type(floor).__name__}={floor!r}"
            )
        if floor <= 0:
            raise VacuityError(
                f"{name}: a floor of {floor!r} is satisfied by any count, so this "
                f"asserts nothing."
            )
        self._record(name)
        if not got >= floor:
            raise AssertionError(f"{name}: got {got!r}, wanted at least {floor!r}")

    # -- the layer's own tests ---------------------------------------------
    @_resolving
    def refusal(self, result, name, *patterns):
        """The inner run failed, AND it failed for the REASON named.

        `outcomes(result, failed=1)` alone is satisfied by any refusal, so a
        guard whose neighbour catches the same input is pinned by nothing. A
        mutation census over this layer found 12 of 17 guards deletable with the
        corpus still green, and two of those were UNREACHABLE-by-subsumption
        rather than untested: neuter `ordered_rows`'s both-empty guard and the
        unobservable guard fires on the same input, so the inner run still fails
        and an outcome-only assertion still passes (@jdatcmd, #897 review).

        Every pattern must appear. Naming the message is what makes the arm
        about one guard instead of about the layer in general.
        """
        if not patterns:
            raise VacuityError(
                f"{name}: refusal() with no pattern asserts only that something "
                f"failed, which is the defect it exists to remove."
            )
        self._record(name)
        result.assert_outcomes(failed=1, passed=0)
        # ANCHORED TO pytest's ERROR-LINE PREFIX, and that is the whole point.
        #
        # This was `f"*{p}*"`, which searches the inner run's WHOLE stdout --
        # and pytest prints the enclosing function's SOURCE in a traceback,
        # including lines that never executed. So the pattern matched the
        # guard's own string literal in the traceback rather than anything the
        # guard produced. Measured: with `hash()`'s left-sentinel guard
        # neutered, the inner output still contains
        #
        #     raise VacuityError(f"{name}: the left side is a failed query: ...")
        #     E  AssertionError: a failed query on the left: got ... want ...
        #
        # and `*the left side is a failed query*` matched the first line. Every
        # message in a function is printed whenever anything in it fails.
        #
        # THAT IS THE DEFECT THIS HELPER EXISTS TO PREVENT, IN THIS HELPER.
        # `outcomes(failed=1)` is satisfied by any refusal; requiring the message
        # was meant to fix it, and matching printed source meant it did not --
        # it was satisfied by any failure in a function whose source contains the
        # phrase. A census over the layer found SEVEN guards unheld this way.
        #
        # `E` is the prefix pytest puts on the raised-exception lines of a
        # traceback, so the phrase must now appear in a message rather than
        # anywhere in the file.
        result.stdout.fnmatch_lines([f"E*{p}*" for p in patterns])

    @_resolving
    def outcomes(self, result, name, **want):
        """Assert on an INNER pytest run's outcomes, and count it.

        The layer's own tests run pytest inside pytest, so their assertions are
        about another run rather than about a query. They are still assertions and
        the rule still applies to them: the guard has no exemption for the tests
        that prove the guard. Adding one would be the first step to exempting
        everything else.
        """
        if not want:
            raise VacuityError(
                f"{name}: outcomes() with no expectation asserts nothing."
            )
        self._record(name)
        result.assert_outcomes(**want)

    @_resolving
    def run_failed(self, result, name):
        """Assert an inner run exited non-zero, and count it."""
        self._record(name)
        if result.ret == 0:
            raise AssertionError(
                f"{name}: the inner run exited 0, so nothing refused it."
            )

    @_resolving
    def plan_marker(self, plan, key, name=None, absent=False):
        """Assert a plan node carries (or does not carry) a Columnar property KEY.

        This is the faithful port of `pgc_is_columnar_scan` (`lib.sh`), which greps
        `EXPLAIN` output for `Columnar Projected Columns`. That marker is emitted
        only by the scan's explain callback (`columnar_customscan.c:3631`) and never
        by either aggregate callback, so its presence is what distinguishes a
        columnar scan from a vectorized aggregate that absorbed one.

        Presence of a KEY, not equality of a VALUE, because the marker's value is a
        count that legitimately varies. `absent=True` asserts the opposite, which is
        how a test pins that a plan is NOT a scan.
        """
        label = name or f"plan {'lacks' if absent else 'carries'} {key!r}"
        nodes = list(_plan_nodes(plan))

        # A PLAN THAT DID NOT ARRIVE LOOKS EXACTLY LIKE ONE THAT LACKS THE NODE.
        # With `absent=True` that is a pass: the claim "nothing here carries the
        # marker" is satisfied by there being nothing here. Measured before this
        # guard: `expect.plan_marker([], "Columnar Projected Columns",
        # absent=True)` gave `1 passed`, exit 0.
        #
        # That is the worst place in this layer for a silent pass. `absent=True`
        # is how the vector-aggregate trap is pinned, and test_connection.py uses
        # plan_marker as the PREMISE that the aggregate engaged -- a premise that
        # cannot fail turns its test into one about an ordinary plan.
        #
        # Refused for the present arm too, and deliberately: an empty plan means
        # the EXPLAIN did not arrive, so neither question can be answered. The
        # present arm would fail anyway, but it would fail with "no node carries
        # it, Columnar keys present: []", which diagnoses the wrong thing.
        if not nodes:
            raise VacuityError(
                f"{label}: the plan has no nodes, so nothing here could carry "
                f"or lack {key!r}. An EXPLAIN that did not arrive is not an "
                f"answer to either question."
            )

        found, seen = False, set()
        for node in nodes:
            seen.update(k for k in node if k.startswith("Columnar"))
            if key in node:
                found = True

        self._record(name)
        if absent and found:
            raise AssertionError(f"{label}: the key is present and should not be.")
        if not absent and not found:
            raise AssertionError(
                f"{label}: no node carries it. Columnar keys present: {sorted(seen)!r}"
            )

    # -- the third state ---------------------------------------------------
    @_resolving
    def cannot_run(self, reason, detail=""):
        """Declare this test unrunnable. Not a pass, and not a silent skip.

        THE STATE HAS TO COST SOMETHING OR IT IS A SKIP WITH BETTER MANNERS. It
        did not, at first: this wrote `self.unrunnable` and nothing read it, so a
        test calling this reported `1 passed` and exit 0. A write-only field --
        the same shape selftest 320 polices in the runner, where an INCOMPLETE
        branch set a flag the verdict never read. It made the layer's own escape
        hatch its largest hole: a bare `@pytest.mark.skip` FAILS the run, while
        the honest-looking alternative greened silently.

        The run now ends `EXIT_INCOMPLETE` unless something failed outright, and
        the reason and detail are printed. See `_UnrunnableCollector` below.
        """
        if reason not in UNRUNNABLE_REASONS:
            raise VacuityError(
                f"unrunnable reason {reason!r} is not one of {UNRUNNABLE_REASONS}"
            )
        self.unrunnable = (reason, detail)
        # UNRUN, NOT PASS. An assertion that declined to run is an outcome like any
        # other -- #937 property 4 -- and the shell's verdict vocabulary has the same
        # four values for the same reason. The record is named by the reason CODE,
        # which is from a closed list, so the stream stays keyable when the detail is
        # free text.
        self._record(name=reason, verdict="UNRUN", reason=detail)


@pytest.fixture
def expect(request):
    rec = Expect(request.node.nodeid)
    _RECORDERS[request.node.nodeid] = rec
    yield rec
    _RECORDERS.pop(request.node.nodeid, None)


class _WatchedCursor:
    """A psycopg cursor that reports every write it runs to the guard.

    A PROXY RATHER THAN A SUBCLASS, because psycopg builds cursors itself and the
    connection is what hands them out. `__getattr__` forwards everything this class
    does not name, so the cursor keeps its whole API -- iteration, context manager,
    fetchall, description -- and only `execute` grows a side effect.
    """

    def __init__(self, cur, nodeid):
        self._cur = cur
        self._nodeid = nodeid
        # SET IN __init__ so it is always an INSTANCE attribute. Without it the first
        # lookup falls through to `__getattr__`, which forwards to the raw cursor and
        # raises -- readable through `getattr(..., None)`, but it would make the
        # absence of a stamp indistinguishable from a cursor that has not run yet.
        self._pgc_write = None

    def execute(self, *args, **kwargs):
        result = self._cur.execute(*args, **kwargs)
        # THE PROXY IS WHAT GETS STAMPED, because the proxy is what the caller holds
        # and a real psycopg cursor cannot take the attribute at all. `__getattr__`
        # never intercepts this, because the instance really has it.
        self._pgc_write = note_write(self._nodeid, self._cur)
        # psycopg returns the cursor itself, so hand back the WATCHED one: a caller
        # writing `for row in cur.execute(...)` must not escape the proxy.
        return self if result is self._cur else result

    def executemany(self, *args, **kwargs):
        result = self._cur.executemany(*args, **kwargs)
        self._pgc_write = note_write(self._nodeid, self._cur)
        return result

    def __getattr__(self, attr):
        return getattr(self._cur, attr)

    def __iter__(self):
        return iter(self._cur)

    def __enter__(self):
        self._cur.__enter__()
        return self

    def __exit__(self, *exc):
        return self._cur.__exit__(*exc)


class _WatchedConnection:
    """A psycopg connection whose cursors are watched. Same proxy argument."""

    def __init__(self, conn, nodeid):
        self._conn = conn
        self._nodeid = nodeid

    def execute(self, *args, **kwargs):
        cur = self._conn.execute(*args, **kwargs)
        watched = _WatchedCursor(cur, self._nodeid)
        # Stamped on the proxy handed back, for the reason _WatchedCursor.execute
        # gives: this is the object the test holds and passes to `wrote`.
        watched._pgc_write = note_write(self._nodeid, cur)
        return watched

    def cursor(self, *args, **kwargs):
        return _WatchedCursor(self._conn.cursor(*args, **kwargs), self._nodeid)

    def __getattr__(self, attr):
        return getattr(self._conn, attr)

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)


def watch_writes(conn, nodeid):
    """Wrap a connection so its writes reach the guard. Used by conftest."""
    return _WatchedConnection(conn, nodeid)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    """Fail a test that concluded nothing, after its body has run.

    After the body, deliberately. A test that raised has already failed, and its
    assertion count is not the interesting fact about it.
    """
    result = yield
    rec = _RECORDERS.get(item.nodeid)
    if rec is None or rec.count == 0:
        raise VacuityError(
            f"vacuity guard: {item.name} made no counted assertion. "
            f"A test that concludes nothing must not report a pass. "
            f"Use the `expect` fixture, or declare it unrunnable with a reason."
        )
    # A WRITE THAT WROTE NOTHING BUILT THE WRONG FIXTURE, and the assertions below
    # it then compared two empty things. `INSERT ... SELECT ... WHERE false` raises
    # nothing and reports `INSERT 0 0`; before this, nobody in the corpus read
    # either field. That is `insert-wrote-no-rows` in VACUITY_MODES.md section 3.5.
    #
    # IN THE CALL PHASE, not a teardown fixture. #931 measured that a guard run as a
    # teardown reports the test it guards as PASSED and fails separately, so a
    # reader sees a green test beside an error. Raising here fails the test itself.
    #
    # NAMED INDIVIDUALLY, because a fixture that runs four writes and gets nothing
    # from the third is the real shape, and "a write wrote no rows" sends the reader
    # to the wrong statement.
    empty = [w for w in _WRITES.pop(item.nodeid, ()) if w.count == 0 and not w.acknowledged]
    if empty:
        which = ", ".join(f"#{w.ordinal} {w.tag}" for w in empty)
        raise VacuityError(
            # ONE UNBREAKABLE TOKEN FIRST. pytest word-wraps a long traceback line,
            # and an arm matching a multi-word phrase against one line then matches
            # nothing -- which reads as "the guard did not fire". Measured here:
            # "wrote no rows" straddled the wrap. The kebab-case id is the mode's
            # own name in VACUITY_MODES.md and cannot be split.
            f"vacuity guard: insert-wrote-no-rows in {item.name}: {which} moved "
            f"no rows. A write that wrote nothing built the fixture the assertions "
            f"above it then measured as empty. Assert the count with "
            f"expect.wrote(cur, n, ...) -- naming a deliberate zero is what "
            f"separates a negative control from a broken fixture."
        )
    return result


class _UnrunnableCollector:
    """Gathers the unrunnable declarations of ONE session.

    Held on the config rather than in a module global, because `pytester` runs
    the layer's own tests IN-PROCESS: an inner run imports this same module, so a
    module-level list would leak the inner run's declarations into the outer
    session and exit the whole corpus INCOMPLETE. One collector per config is one
    per session, inner runs included.
    """

    def __init__(self):
        self.items = []

    def pytest_runtest_logreport(self, report):
        # This hook fires on the CONTROLLER for reports received from xdist
        # workers, which is why the declaration travels as a user_property
        # rather than in a variable the worker process owns. A worker's own
        # exit status is discarded by xdist; the controller's is the run's.
        if report.when != "call":
            return
        for key, value in getattr(report, "user_properties", ()):
            if key == "pgc_unrunnable":
                reason, _, detail = value.partition("\n")
                self.items.append((report.nodeid, reason, detail))




class _RecordCollector:
    """Reconciles what each test's recorder HELD against what ARRIVED. #937 phase 3.

    THE OBVIOUS RECONCILIATION IS VACUOUS HERE, BY CONSTRUCTION, and phase 1 made
    it so deliberately. `count` IS `len(self._records)`, so checking one against
    the other compares a value with its own definition. Partitioning the records
    into PASS/FAIL/UNRUN and asserting the parts sum to the whole is the same trap
    in a hat -- the buckets are derived from the list being counted. #937 records
    that the shell side shipped `inputs == sum(buckets)` twice and that both were
    caught only by mutating them; a third would be worse for having been warned.

    So the two quantities come by different routes:

        held      len(recorder.records), read in the process that RAN the test
        arrived   the list read back off the report AFTER it was built, crossing
                  the report boundary and, under -n, a process boundary too

    `_UnrunnableCollector` above is why the second route has to exist at all: a
    worker's state is invisible to the controller, so the value travels on the
    report. Measured on the pinned runner, user_properties survive that crossing
    intact -- which is what makes this a reconciliation rather than a formality.

    WHAT IT CATCHES: a record dropped or mangled between the report being built
    and the report being read, and a verdict outside the closed set.

    WHAT IT DOES NOT, and the first version of this comment claimed the first of
    these, wrongly:

    * ANYTHING THAT CHANGES THE RECORDER OUTSIDE THE SINGLE INSTANT IT IS READ,
      in EITHER direction. Both values come from one read, so:

          a record appended AFTER the read    invisible, run passes
          a record removed BEFORE the read    invisible, run passes

      Measured both ways. The first version of this comment named only the later
      half, and @OffgridwithJD injected the earlier one -- which is the MORE
      reachable of the two, because a late append needs someone outside the layer
      while an early loss is what a bug inside the recorder would look like.

      THE REASON IS NOT XDIST. An earlier version said the controller has no
      recorder to consult under -n. The real reason needs no xdist: the `expect`
      fixture's teardown pops the recorder, so nothing after makereport can read
      it in a single process either.

      AND IT IS NOT UNFIXABLE, which that version also implied. @OffgridwithJD's
      proposal: keep the final COUNT -- an int, not the records -- in a
      session-level map that survives teardown, and reconcile the sum at
      worker-side sessionfinish, where the worker has its own slice and needs
      nothing from the controller. Unbuilt and unmeasured here, so it is named
      rather than planned. An arm in test_check_records.py pins both halves of
      the gap so neither can be claimed away.

    * A record that is present, transported and well-formed, and WRONG. That is
      phase 2's job.

    So this is a transport check, not a completeness check, and calling it the
    latter would be the third vacuous reconciliation #937 warns about wearing the
    clothes of the two it already names.
    """

    def __init__(self):
        self.records = []      # (nodeid, verdict, name)
        self.offences = []

    def pytest_runtest_logreport(self, report):
        if report.when != "call":
            return
        held = None
        arrived = None
        for key, value in getattr(report, "user_properties", ()):
            if key == "pgc_records_held":
                held = value
            elif key == "pgc_records":
                arrived = list(value)
        if held is None and arrived is None:
            return
        if arrived is None:
            arrived = []
        if held != len(arrived):
            self.offences.append(
                f"{report.nodeid}: the recorder held {held} record(s) and "
                f"{len(arrived)} arrived"
            )
        for verdict, name in arrived:
            if verdict not in RECORD_VERDICTS:
                self.offences.append(
                    f"{report.nodeid}: record {name!r} carries the verdict "
                    f"{verdict!r}, which is not one of {RECORD_VERDICTS}"
                )
            self.records.append((report.nodeid, verdict, name))


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    """Carry an unrunnable declaration out on the report itself.

    `user_properties` is serialised across the xdist boundary; an attribute of
    our own would not be.
    """
    report = yield
    if call.when == "call":
        rec = _RECORDERS.get(item.nodeid)
        if rec is not None and rec.unrunnable:
            reason, detail = rec.unrunnable
            report.user_properties.append(("pgc_unrunnable", f"{reason}\n{detail}"))
        # BOTH ROUTES, and they are attached separately on purpose (#937 phase 3).
        # `pgc_records_held` is a number read from the recorder HERE; `pgc_records`
        # is the stream itself. Deriving the count from the stream on the far side
        # would compare the stream with itself, which is the vacuous shape this
        # phase exists to avoid.
        #
        # PLAIN TUPLES, not _Record objects: user_properties are serialised across
        # the xdist boundary, and an object that failed to serialise would break
        # the transport this check exists to watch.
        if rec is not None:
            report.user_properties.append(("pgc_records_held", rec.count))
            report.user_properties.append(
                ("pgc_records", [(r.verdict, r.name) for r in rec.records]))
    return report


def pytest_terminal_summary(terminalreporter):
    """Print the third state in lib.sh's shape, then the run's own totals.

    `UNRUN  <name>: <REASON>: <detail>`, then the count. A state that does not
    say why is a skip with better manners, and a state with no count cannot be
    reconciled against the total.

    THE TOTAL IS COUNTED FROM THE RECORDS, NOT FROM THE TESTS. Those agree
    whenever every test makes exactly one claim, which is what a hand-written
    fixture reaches for first -- so an arm in test_check_records.py uses four
    claims in one test and one in another, where a per-test count would say 2 and
    the records say 5.
    """
    collector = getattr(terminalreporter.config, "pgc_unrunnable", None)
    if collector is not None and collector.items:
        terminalreporter.write_line("")
        for nodeid, reason, detail in collector.items:
            terminalreporter.write_line(f"UNRUN  {nodeid}: {reason}: {detail}")
        terminalreporter.write_line(f"checks unrunnable: {len(collector.items)}")

    records = getattr(terminalreporter.config, "pgc_records", None)
    if records is None or not records.records:
        return
    tally = {v: 0 for v in RECORD_VERDICTS}
    for _nodeid, verdict, _name in records.records:
        if verdict in tally:
            tally[verdict] += 1
    terminalreporter.write_line(f"checks run: {len(records.records)}")
    terminalreporter.write_line(
        "accounting: "
        + " + ".join(f"{tally[v]} {v.lower()}" for v in RECORD_VERDICTS)
        + f" = {sum(tally.values())}"
    )


def pytest_sessionfinish(session, exitstatus):
    """An unrunnable test must not leave the run green, and neither must a
    record stream that does not reconcile.

    FAILURE STILL DOMINATES, exactly as in lib.sh: a run with both a failure and
    an unrunnable test is a failure, because the failure is the more urgent fact.
    So this only ever moves a run OFF zero, and never off a non-zero status.
    """
    # THE RECONCILIATION, FIRST, because it is a statement about whether the run
    # can be believed at all rather than about one test (#937 phase 3).
    #
    # WRITTEN TO STDERR AND FORCED OFF ZERO rather than raised. A UsageError here
    # is not reported cleanly -- the session is already finishing -- and this must
    # not depend on an exception surviving a hook that other plugins also wrap.
    records = getattr(session.config, "pgc_records", None)
    if records is not None and records.offences:
        sys.stderr.write(
            "the pgColumnar vacuity layer refuses this run: the record stream "
            "does not reconcile, so the totals above describe something other "
            "than what the assertions did:\n"
        )
        for offence in records.offences:
            sys.stderr.write(f"  {offence}\n")
        sys.stderr.write(
            "  -- a record created after the report was built, or dropped in "
            "transport, is invisible to every other check in this layer.\n"
        )
        sys.stderr.flush()
        if session.exitstatus == 0:
            session.exitstatus = EXIT_INCOMPLETE

    collector = getattr(session.config, "pgc_unrunnable", None)
    if collector is None or not collector.items:
        return
    # Both, not just the argument: _RunShape may already have escalated this run
    # to 1 for a lost test, and `exitstatus` is the value from before that.
    if exitstatus == 0 and session.exitstatus == 0:
        session.exitstatus = EXIT_INCOMPLETE


# THE RUN'S OWN SHAPE, HELD PER SESSION.
#
# Three modes remove many tests at once while the run reads green, so they are worth
# more than any per-assertion guard. Counting collected tests cannot see them: a
# crashed xdist worker loses its remaining tests and the collected count is still
# right. Measured under --max-worker-restart=0: 8 collected, summary "1 failed,
# 6 passed", one named test never reported, and pytest printed no warning.
#
# AN INSTANCE PER CONFIG, NOT MODULE GLOBALS. pytester.runpytest() runs the inner
# session IN-PROCESS, so module-level sets are shared between the layer's own tests
# and the sessions they drive. Measured before this was fixed: 44 tests passed and
# the run exited 1, because the outer session had inherited every inner run's
# collected ids and setup skips. State that belongs to a session has to live on the
# session.
class _RunShape:
    def __init__(self):
        self.collected = set()
        self.reported = set()
        self.setup_skips = []

    def pytest_collection_modifyitems(self, items):
        # Fires in the controller when running serially, and in each worker under
        # xdist. Harmless in a worker: the worker's own sessionfinish returns early.
        self.collected.update(i.nodeid for i in items)

    def pytest_deselected(self, items):
        """Deselection is not loss, and the difference is the whole guard.

        `pytest_collection_modifyitems` above fires before pytest's own -k and -m
        filtering has removed anything, so without this hook every deselected test
        looks like a test that vanished without reporting. Measured before the fix:
        `pytest -q test_layer.py -k refus` gave "1 passed, 15 deselected" and then
        exit 1 with "15 collected test(s) never reported an outcome". That is a
        false red on a healthy run, produced by the guard whose subject is false
        greens -- and the first thing anyone does about it is stop using -k.

        Asking for a subset is a deliberate act by whoever typed the command. A
        test lost to a crashed worker is not. This hook is where pytest tells the
        difference, so it is where the guard has to learn it.
        """
        self.collected.difference_update(i.nodeid for i in items)

    def pytest_xdist_node_collection_finished(self, node, ids):
        """Under xdist the WORKERS collect, not the controller.

        Measured: with -n 2 the controller's collected set stayed empty, so the
        reconciliation had nothing to compare and a crashed worker's lost tests went
        unreported -- the guard was there and blind. xdist hands the controller each
        node's collected ids through this hook, which is the only place the
        controller learns what was found.
        """
        self.collected.update(ids)

    def pytest_testnodedown(self, node, error):
        """Re-raise a collection refusal the worker could not (#963).

        Per-config, because pytester inner runs share this interpreter and a
        module-level hook would fire for the outer session's nodes too.
        """
        wo = getattr(node, "workeroutput", None) or {}
        msg = wo.get("pgc_vacuity_refusal")
        if msg:
            raise pytest.UsageError(msg)

    def pytest_runtest_logreport(self, report):
        """Record that a test produced an outcome, and catch a skip during SETUP.

        A skip in setup is how one fixture removes every test that depends on it: a
        session fixture calling pytest.skip() turns "the cluster would not start"
        into exit 0. expect.cannot_run does not skip, it records a counted
        assertion, so any skip arriving here came from somewhere else.
        """
        if report.when == "call" or (report.when == "setup"
                                     and report.outcome != "passed"):
            self.reported.add(report.nodeid)
        if report.when == "setup" and report.skipped:
            self.setup_skips.append(report.nodeid)

    def pytest_sessionfinish(self, session, exitstatus):
        # Only the process holding the whole picture can reconcile: an xdist worker
        # sees a slice, and the controller receives every worker's reports.
        if hasattr(session.config, "workerinput"):
            return
        # A LOUD REFUSAL IS NOT A SILENT LOSS (#991). On a collection-time refusal
        # every collected item is accounted for BY the refusal: nothing ran, the layer
        # said so, and the sentence is printed immediately above this one. Reporting
        # "the run lost them silently" there gave a reader two findings where there is
        # one, and sent them looking for a lost test that was never lost -- while the
        # guard whose whole subject is a SILENT loss fired on the single event that is
        # the opposite of silent.
        #
        # Only this problem is dropped. The setup-skip problem below still prints: a
        # fixture removing every test that depends on it is not something the refusal
        # covers, and the two are independent findings.
        refused = getattr(session.config, "_pgc_vacuity_refused", None)
        problems = []
        missing = sorted(self.collected - self.reported)
        if missing and not refused:
            problems.append(
                f"{len(missing)} collected test(s) never reported an outcome, so the "
                f"run lost them silently: " + ", ".join(missing[:5])
                + (" ..." if len(missing) > 5 else "")
            )
        if self.setup_skips:
            problems.append(
                f"{len(self.setup_skips)} test(s) were skipped during setup, which is "
                f"how one fixture removes every test that depends on it: "
                + ", ".join(sorted(self.setup_skips)[:5])
                + (" ..." if len(self.setup_skips) > 5 else "")
                + " -- use expect.cannot_run(REASON, detail) in the test instead"
            )
        if problems:
            print("\nVACUITY: " + " AND ".join(problems))
            session.exitstatus = 1


def pytest_configure(config):
    # Two independent per-session mechanisms, both registered here because a
    # plugin module may define pytest_configure only once.
    collector = _UnrunnableCollector()
    config.pluginmanager.register(collector, "pgc_unrunnable_collector")
    config.pgc_unrunnable = collector
    config.pluginmanager.register(_RunShape(), f"pgc_runshape_{id(config)}")
    records = _RecordCollector()
    config.pluginmanager.register(records, f"pgc_records_{id(config)}")
    config.pgc_records = records


def pytest_addoption(parser):
    parser.addoption(
        "--pgc-expect-tests",
        action="store",
        type=int,
        default=None,
        help="how many tests this run must collect; a mismatch fails the run",
    )


def _collection_usage_error(session, config, items, msg):
    """Refuse collection as a UsageError, including under xdist (#963).

    Serial: raise UsageError. wrap_session sets rc 4 and pytest.main prints
    `ERROR: <msg>` on stderr.

    Under xdist the same raise happens inside a worker. pytest still runs
    `pytest_collection_finish` in a `finally`, so the worker tells the
    controller it collected the tests, then exits. The controller's
    `worker_workerfinished` then asserts that a worker which collected tests
    must not finish with them still pending -- a 35-line INTERNALERROR, rc 1,
    and the sentence is gone. Measured.

    So a worker does not raise. It records the sentence on workeroutput,
    clears the items so collection_finish sends no ids, and sets shouldfail
    so worker_workerfinished does not take the crashitem branch even if a
    race leaves one. The controller re-raises UsageError from
    pytest_testnodedown, which is the process wrap_session already knows
    how to print.
    """
    # RECORDED BEFORE EITHER BRANCH, so the reconciliation in _RunShape cannot call
    # this a silent loss (#991). Every refusal in the layer comes through here, which
    # is the only reason one assignment covers all five call sites.
    config._pgc_vacuity_refused = msg

    if hasattr(config, "workerinput"):
        wo = getattr(config, "workeroutput", None)
        if wo is None:
            config.workeroutput = {}
            wo = config.workeroutput
        wo["pgc_vacuity_refusal"] = msg
        items[:] = []
        session.shouldfail = msg
        return
    raise pytest.UsageError(msg)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_finish(session):
    """Assert the run's own shape, so a filtered or truncated run cannot be green.

    lib.sh does the equivalent in pgc_summary, which reconciles passed plus failed
    plus unrunnable against the total and fails when the arithmetic does not close.

    tryfirst so a refusal clears session.items before xdist's collection_finish
    sends the ids. Sending first is the INTERNALERROR in `_collection_usage_error`.
    """
    want = session.config.getoption("--pgc-expect-tests")
    if want is None:
        return
    if want <= 0:
        _collection_usage_error(
            session, session.config, session.items,
            f"--pgc-expect-tests {want} would be satisfied by a run that collected "
            f"nothing, so it asserts nothing. Give the real number.",
        )
        return
    got = len(session.items)
    if got != want:
        _collection_usage_error(
            session, session.config, session.items,
            f"collected {got} test(s) but expected {want}. A run that quietly "
            f"collects fewer tests than it should is a green that means nothing.",
        )
        return


# A broad except in a test swallows the failure the test exists to find.
#
# After ANY failed statement psycopg raises InFailedSqlTransaction for every later
# one, so a single `except Exception` around a test body hides the real error AND
# every error after it. The layer used to forbid this in a comment, which enforces
# nothing: measured, a test using the forbidden shape passed with no complaint.
#
# PARSED, NOT GREPPED. The first version matched lines with a regex and immediately
# fired on this file's own tests, because they contain the forbidden shape inside a
# `pytester.makepyfile` string. A guard that rejects a legitimate test is a guard
# somebody switches off, and a line regex over source cannot tell code from a string
# literal -- the same mistake as matching a plan by substring. ast can: a handler
# inside a string is not an ExceptHandler node.
# An ordered claim whose inputs were SORTED cannot fail on order.
#
# ordered_rows is order-sensitive, so the vacuity is introduced at the call site:
# `expect.ordered_rows(sorted(got), sorted(want))` compares two sequences that were
# just put in the same order. This is the collapse VACUITY_MODES.md records as
# set-oracle-on-an-ordered-claim, and lib.sh has no equivalent because bash has no
# sorted() to reach for.
#
# Parsed, not grepped, for the same reason as the except scan below.
# THE KILLER LIST IS BOUND AT DEFINITION TIME, in a default argument, and that
# is the whole mechanism rather than a style choice (#924).
#
# `_ORDER_KILLERS` used to live as a module-level name. A conftest imported
# before collection rebound it to () and the order-collapse refusal stopped
# firing for every test in that directory, with no reason recorded. Two lines,
# less to type than the honest form, which is the hatch the layer's own
# false-positive budget forbids. Measured on main: the same collapse test was
# uncollectable with no extra file, and reported `1 passed` with only
#
#     import pgc_vacuity
#     pgc_vacuity._ORDER_KILLERS = ()
#
# The layer already closed this shape for QUERY_ERROR. The same default-argument
# bind is used here: the tuple is evaluated once, when the factory is defined,
# and is not read from the module namespace afterwards. There is no module-level
# name left to rebind.
def _order_guard(_killers=("sorted", "set", "frozenset")):
    def _order_killed_names(fn):
        """Names bound to an order-killing value earlier in one function body.

        -> {name: (lineno, how)}

        The inline spelling is only the shortest way to write the collapse. These two
        are the same defect and read as more careful code, which is worse:

            g = sorted(got)                 # bound to an order-killing call
            expect.ordered_rows(g, want)

            got.sort()                      # killed in place
            expect.ordered_rows(got, want)

        WHAT THIS DOES NOT SEE, stated because a guard's blind spots are part of its
        meaning: it is one function deep, so a helper that sorts and returns is invisible;
        it does not follow aliases (`h = g`), attributes (`self.rows.sort()`), branches,
        or a name re-bound to something honest after being killed. It is a floor, not a
        proof of order-sensitivity. The suite's own removal proofs are what establish
        that an ordered claim can actually fail on order.
        """
        killed = {}
        for node in ast.walk(fn):
            # X = sorted(...) / set(...) / frozenset(...)
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                f = node.value.func
                if isinstance(f, ast.Name) and f.id in _killers:
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            killed.setdefault(t.id, (node.lineno, f"{f.id}()"))
            # X.sort() -- in place, and the name keeps its spelling at the call site
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                f = node.value.func
                if (isinstance(f, ast.Attribute) and f.attr == "sort"
                        and isinstance(f.value, ast.Name)):
                    killed.setdefault(f.value.id, (node.lineno, ".sort()"))
        return killed


    def _sorted_ordered_sites(path):
        try:
            tree = ast.parse(pathlib.Path(path).read_text())
        except (OSError, SyntaxError):
            return []
        out = []
        name = pathlib.Path(path).name
        # Per function, because a killed name means nothing outside the body that
        # killed it, and a module-level walk would carry one test's `g` into the next.
        fns = [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for fn in fns:
            killed = _order_killed_names(fn)
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                if not (isinstance(f, ast.Attribute) and f.attr in ("ordered_rows",
                                                                    "ordering_observable")):
                    continue
                for arg in node.args:
                    if (isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name)
                            and arg.func.id in _killers):
                        out.append(
                            f"{name}:{node.lineno} {arg.func.id}() feeds an ordered claim"
                        )
                    elif isinstance(arg, ast.Name) and arg.id in killed:
                        where, how = killed[arg.id]
                        # Only a kill that already happened. A name sorted AFTER the
                        # claim was made did not affect it, and flagging that would be
                        # a false red -- the thing this whole layer exists to refuse.
                        if where < node.lineno:
                            out.append(
                                f"{name}:{node.lineno} {arg.id} was order-killed by "
                                f"{how} at line {where} and feeds an ordered claim"
                            )
        return out


    return _order_killed_names, _sorted_ordered_sites


_order_killed_names, _sorted_ordered_sites = _order_guard()


def _broad_except_sites(path):
    try:
        tree = ast.parse(pathlib.Path(path).read_text())
    except (OSError, SyntaxError):
        return []
    out = []
    name = pathlib.Path(path).name
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        t = node.type
        if t is None:
            out.append(f"{name}:{node.lineno} bare except")
            continue
        # A TUPLE HANDLER IS THE SHAPE PEOPLE ACTUALLY WRITE.
        #
        # This looked only at a bare `ast.Name`, so `except Exception:` was
        # refused and `except (ValueError, Exception):` passed (@jdatcmd, #905
        # review). Measured against the real layer, three spellings of one
        # swallow:
        #
        #     except Exception:               -> refused
        #     except (ValueError, Exception): -> PASSED   <- the hole
        #     except BaseException:           -> refused
        #
        # A tuple is how this gets written when someone starts with a specific
        # exception and widens it under pressure, which is the exact moment the
        # guard is for -- so the hole was in the case the guard most needed to
        # cover. Any member of the tuple being broad makes the handler broad.
        members = t.elts if isinstance(t, ast.Tuple) else [t]
        for m in members:
            if isinstance(m, ast.Name) and m.id in ("Exception", "BaseException"):
                out.append(f"{name}:{node.lineno} except {m.id}")
                break
    return out


def _walk_own(node):
    """Walk one function body, NOT descending into a nested def or lambda.

    A nested function is its own scope. `ast.walk` would attribute its `with`
    blocks to the outer function as well, reporting one site twice under two
    different sets of pinned names.
    """
    stack = list(getattr(node, "body", []))
    while stack:
        child = stack.pop()
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield child
        stack.extend(ast.iter_child_nodes(child))


def _raises_class_names(arg):
    """The tail identifiers a pytest.raises() first argument names.

    `psycopg.Error` -> ["Error"]. `(ValueError, psycopg.Error)` -> both.

    A TUPLE IS THE SHAPE PEOPLE ACTUALLY WRITE, and it is how a narrow claim gets
    widened under pressure. The broad-except scan paid for that lesson already:
    it looked only at a bare `ast.Name`, so `except Exception` was refused while
    `except (ValueError, Exception)` passed (@jdatcmd, #905 review). Same hole,
    same shape, closed here before it was shipped rather than after.
    """
    out = []
    for node in (arg.elts if isinstance(arg, ast.Tuple) else [arg]):
        if isinstance(node, ast.Name):
            out.append(node.id)
        elif isinstance(node, ast.Attribute):
            out.append(node.attr)
    return out


def _root_name(node):
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _sqlstate_pinned_names(fn):
    """Names whose SQLSTATE this function body ASSERTS something about.

    Two spellings, because both are honest and the scan must accept whichever the
    caller chose:

        expect.sqlstate(exc.value, "42883", name)        # the helper
        expect.text(exc.value.sqlstate, "42883", name)   # the field, read directly

    THE ATTRIBUTE HAS TO REACH A CALL. The first version counted any `ast.Attribute`
    named `sqlstate` anywhere in the body, so MENTIONING the field switched the rule
    off. Measured, both collecting clean against the first version and both being the
    exact vacuity this rule is named for -- any of the 254 SQLSTATEs satisfies them:

        exc.value.sqlstate                    # a bare expression, asserts nothing
        code = exc.value.sqlstate             # assigned, never read

    Reported by @jdatcmd, who ran the scanner over six constructed files rather than
    reading it.

    So a read counts when it is an ARGUMENT to a call, and one hop of assignment is
    followed -- `code = exc.value.sqlstate` then `expect.text(code, ...)` is honest and
    common, and refusing it would be a false positive on a form nobody should have to
    stop writing. A second hop is not followed: this is a floor, and the floor is
    stated rather than implied.
    """
    pinned = set()
    # Names a sqlstate read was assigned to, and names that appear as call arguments.
    assigned_from_sqlstate = {}
    call_arg_names = set()
    for node in _walk_own(fn):
        if isinstance(node, ast.Call):
            for arg in list(node.args) + [k.value for k in node.keywords]:
                for sub in ast.walk(arg):
                    if isinstance(sub, ast.Name):
                        call_arg_names.add(sub.id)
                    if isinstance(sub, ast.Attribute) and sub.attr == "sqlstate":
                        root = _root_name(sub.value)
                        if root:
                            pinned.add(root)
        if isinstance(node, ast.Assign):
            for sub in ast.walk(node.value):
                if isinstance(sub, ast.Attribute) and sub.attr == "sqlstate":
                    root = _root_name(sub.value)
                    for t in node.targets:
                        if isinstance(t, ast.Name) and root:
                            assigned_from_sqlstate[t.id] = root
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "sqlstate"):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name):
                    pinned.add(sub.id)
    # One hop: assigned from a sqlstate read, and later handed to a call.
    for local, root in assigned_from_sqlstate.items():
        if local in call_arg_names:
            pinned.add(root)
    return pinned


# Every statement that can HOLD other statements. Built by lookup rather than
# written out, because `TryStar` and `Match` exist only on newer Pythons and a
# missing name would be a NameError at import rather than a rule that quietly does
# less. The inventory named only the `for` spelling; a rule catching only that one
# would leave three spellings of the same shape, which is closing an example rather
# than a mode.
_COMPOUND_STATEMENTS = tuple(
    c for c in (getattr(ast, n, None) for n in (
        "For", "AsyncFor", "While", "If", "With", "AsyncWith", "Try", "TryStar",
        "Match",
    )) if c is not None
)


def _raises_sites(path):
    """Every `with pytest.raises(...)` in one file, and what is wrong with it.

    PARSED, NOT GREPPED, and `test_layer.py` is why. The layer's own tests drive an
    inner pytest run, so the forbidden shape appears inside a `pytester.makepyfile`
    STRING in the very file that proves the guard. A line regex fires on it. That
    is the false positive the broad-except scan already paid for once, and a guard
    that rejects legitimate tests gets switched off -- after which the thing it
    replaced is gone too. A call inside a string literal is not an `ast.Call`.

    WHAT THIS DOES NOT SEE, stated because a guard's blind spots are part of its
    meaning. It reads `with` blocks inside functions, so a `raises` at module level
    or used as a plain call (`pytest.raises(E, fn, arg)`) is invisible. It counts
    TOP-LEVEL statements in the block, so a single `for` or `if` holding several
    statements counts as one, and a call to a helper that performs the setup counts
    as one as well -- those two shapes are why `raises-catches-setup` stays open in
    `VACUITY_MODES.md` section 3.4, and each has an arm in
    `test_raises_sqlstate.py` asserting this scan reports nothing on it. It does
    not follow a SQLSTATE pin into a helper, and it matches a pin by name, so an
    unrelated argument that happens to share the bound name's spelling would
    satisfy it. It is a floor, not a proof that the assertion is about the
    statement under test.
    """
    # A BROAD pytest.raises IS SATISFIED BY AN UNRELATED FAILURE OF THE SAME FAMILY.
    #
    # Measured against psycopg 3.3.5 in the audit container, by counting the classes
    # in `psycopg.errors` that carry a `sqlstate` and subclass each family:
    #
    #     psycopg.Error             254 SQLSTATEs   42 SQLSTATE classes
    #     psycopg.DatabaseError     254             42
    #     psycopg.OperationalError   88             15
    #     psycopg.DataError          68              1
    #     psycopg.ProgrammingError   57             10
    #     psycopg.InternalError      20              5
    #     psycopg.IntegrityError      7              1
    #     psycopg.NotSupportedError   1              1
    #     psycopg.Warning             0              0
    #     psycopg.InterfaceError      0              0
    #
    # `pytest.raises(psycopg.Error)` therefore claims "one of 254 server errors
    # arrived", and does not even claim that: measured on this tree, a connect to a
    # socket that does not exist raises OperationalError with sqlstate None, so
    #
    #     with pytest.raises(psycopg.Error):
    #         conn = psycopg.connect("host=/nonexistent-socket-dir dbname=pgc")
    #         conn.execute("SELECT pgc_definitely_no_such_function()")
    #
    # reported `1 passed`, exit 0, with the statement under test never executed.
    #
    # WHY THIS LIST AND NOT THE WHOLE FAMILY, and it is the measurement above
    # deciding it rather than taste. `Warning` and `InterfaceError` cover ZERO
    # SQLSTATEs, so demanding one of them would be a guard nobody could satisfy --
    # and an unsatisfiable guard is how a guard gets switched off. The four
    # intermediate DB-API classes are not refused either: narrowing to one of them
    # is already a real claim about the error, and OperationalError legitimately
    # arrives with no SQLSTATE when the connection itself failed.
    #
    # WHY IT IS BOUND HERE AND NOT AT MODULE LEVEL. A rule's own parameters must not
    # be reachable from the tree the rule polices. Any `conftest.py` under
    # `test/pytest/` is imported before collection, so a module-level tuple can be
    # rewritten from the corpus:
    #
    #     import pgc_vacuity
    #     pgc_vacuity.<the tuple> = ()
    #
    # after which this scan reports zero offences for ever and the suite is green.
    # Bound inside the function, those lines do nothing -- the name is not looked up
    # in the module namespace at all. (Rebinding this FUNCTION from a conftest is
    # still possible. That is true of every name in every Python plugin and is not
    # something the placement of a tuple can fix. What notices a scan that stopped
    # being CALLED is selftest 440, which requires the wiring line in the collection
    # hook, plus the 5 arms in `test_raises_sqlstate.py` that match the refusal on
    # stderr -- measured, as mutation M5 of that suite's removal proof: deleting the
    # wiring reddens those same 5. `test_guards_pinned.py` is the layer's census of
    # "every refusal pinned to its own message" and does NOT yet carry these two;
    # adding them there is the honest next step, and saying so is better than citing
    # a file that does not mention them.)
    broad_families = ("Error", "DatabaseError", "Exception", "BaseException")

    try:
        tree = ast.parse(pathlib.Path(path).read_text())
    except (OSError, SyntaxError):
        return []
    out = []
    name = pathlib.Path(path).name
    # EVERY FUNCTION THIS FILE DEFINES, nested ones included. A `pytest.raises` block
    # whose one statement calls one of these is the helper shape: the helper can run
    # any number of statements and nothing in the block says which of them failed. A
    # call to an IMPORTED function, or a method, is the thing under test -- which is
    # the shape all five blocks in this corpus use, so the rule turns on where the
    # function is DEFINED rather than on the statement being a call.
    local_defs = {n.name for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        pinned = _sqlstate_pinned_names(fn)
        for node in _walk_own(fn):
            if not isinstance(node, (ast.With, ast.AsyncWith)):
                continue
            sites = []
            for item in node.items:
                call = item.context_expr
                if not isinstance(call, ast.Call):
                    continue
                f = call.func
                tail = (f.attr if isinstance(f, ast.Attribute)
                        else f.id if isinstance(f, ast.Name) else None)
                if tail != "raises":
                    continue
                # THE CLASS MAY ARRIVE BY KEYWORD. `not call.args` skipped the item
                # BEFORE it was appended, so `pytest.raises(expected_exception=E)`
                # was checked by neither rule -- and the statement rule was therefore
                # silently conditional on the class being positional, which the
                # documentation stated unconditionally. Reported by @jdatcmd, who
                # built the positional and keyword forms as a pair that differ in
                # nothing else: the positional one was an offence and the keyword one
                # was clean.
                expected = None
                if call.args:
                    expected = call.args[0]
                else:
                    for kw in call.keywords:
                        if kw.arg == "expected_exception":
                            expected = kw.value
                            break
                if expected is None:
                    continue
                sites.append(item)
                bound = (item.optional_vars.id
                         if isinstance(item.optional_vars, ast.Name) else None)
                broad = [c for c in _raises_class_names(expected)
                         if c in broad_families]
                if broad and (bound is None or bound not in pinned):
                    # THE OFFENCE PHRASE STAYS ON ONE SOURCE LINE, and the reason
                    # has CHANGED. It was selftest 440, which grepped this source for
                    # the phrase and counted the copies, so a split into
                    # `"... names no " f"SQLSTATE"` read identically at runtime while
                    # the count saw one where it wanted two. **Selftest 440 no longer
                    # exists** -- #927 deleted it under the harness-independence rule,
                    # because a shell part asserting a text pin cannot prove a python
                    # arm is caught. Nothing greps this source for the phrase today, so
                    # the one-line form is now a convention rather than a guarded
                    # property. What IS still load-bearing is the RUNTIME string: the
                    # arms in test_raises_sqlstate.py match it against stderr, and a
                    # split f-string would not change that at all.
                    where = f"{name}:{call.lineno}"
                    out.append(
                        f"{where} pytest.raises({broad[0]}) names no SQLSTATE"
                    )
            # THE RAISER HAS TO BE THE STATEMENT UNDER TEST. Once per `with`, not
            # once per item: two raises in one `with` share one body.
            if sites and len(node.body) != 1:
                # One source line for this phrase too, for the reason above.
                held = f"{name}:{node.lineno} the pytest.raises block holds"
                out.append(
                    f"{held} {len(node.body)} statements, "
                    f"so which one raised is not pinned"
                )
            # AND ONE STATEMENT IS NOT ENOUGH, which is the half `raises-catches-setup`
            # stayed open on. Two shapes are one top-level statement and still hide the
            # setup inside the block, so the count rule above saw nothing:
            #
            #     with pytest.raises(...): _setup_then_run(conn)   # a helper call
            #     with pytest.raises(...):                         # a compound
            #         for stmt in (setup, under_test): run(stmt)
            #
            # Both were measured reporting `1 passed`, exit 0, zero offences, with the
            # setup raising and the statement under test never running. The fix is not a
            # RECURSIVE count -- that would also refuse a legitimate single-statement
            # loop -- it is a claim about which statement raised.
            elif sites:
                only = node.body[0]
                kind = type(only).__name__
                if isinstance(only, _COMPOUND_STATEMENTS):
                    # One source line for the phrase, as above.
                    out.append(
                        f"{name}:{node.lineno} the pytest.raises block holds a {kind}, "
                        f"so which statement inside it raised is not pinned"
                    )
                else:
                    # ANYWHERE IN THE STATEMENT, not only as the whole of it: a helper
                    # hides just as well in `x = _helper()` or `assert _helper()` as it
                    # does in a bare call.
                    called = sorted({
                        n.func.id for n in ast.walk(only)
                        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                        and n.func.id in local_defs
                    })
                    if called:
                        out.append(
                            f"{name}:{node.lineno} the pytest.raises block calls "
                            f"{called[0]}(), defined in this file, so which statement "
                            f"raised is not pinned"
                        )
    return out


# THE LAYER'S OWN BINDINGS, AND WHY THEY ARE CHECKED RATHER THAN HIDDEN (#924).
#
# pytest imports `conftest.py` FROM THE DIRECTORY IT IS POLICING into the policing
# interpreter, before collection, with no opt-out. Every module-level name here is
# therefore writable by the code this layer judges. That is what conftest is for; it
# is a defect here only because this layer's job is to refuse, and a refusal that can
# be deleted in two lines is a suggestion.
#
# THE SHELL HARNESS HAS NO EQUIVALENT, and not because bash is simpler: its policing
# runs in a DIFFERENT PROCESS from the code it polices. `selftest/260` reads
# `$TESTDIR/lib.sh` with grep and awk and never sources the file it judges, so
# nothing a policed suite writes can reach the judge's namespace.
#
# #958 closed the datum one exploit used, by binding the killer list in a default
# argument. The three scans that READ such data are module-level names themselves,
# one frame further out. Measured on main 226f805 with the pinned runner, each was a
# two-line conftest away from being a no-op:
#
#     GUARD               no conftest    with `pgc_vacuity.<scan> = lambda p: []`
#     order collapse      REFUSED rc=4   PASSED rc=0
#     broad except        REFUSED rc=4   PASSED rc=0
#     raises not pinned   REFUSED rc=4   PASSED rc=0
#
# SO THE FIX IS NOT A FOURTH NAME MOVED OUT OF REACH. Plugging names one at a time is
# what reopened #924 after #958, and the transitive closure from the eight hooks is
# 31 of this module's 47 names -- `ast` among them. The layer notices instead that one
# of its own bindings CHANGED, which covers the names added after this was written.
#
# THE SNAPSHOT IS EXACT WITH NO ALLOWLIST because this module contains no `global`
# statement anywhere: after import, every module-level binding here is constant.
# Verified rather than assumed, and it is the property that makes a bare identity
# comparison correct.
#
# A NEW ATTRIBUTE IS NOT A REBIND. `pgc_vacuity._something_new = 1` changes no
# behaviour, and refusing it would make this a tripwire on the mere act of importing
# the module -- the false-positive engine this layer's own budget forbids. The arm
# for that is a control in test_layer.py.
#
# WHAT IT STILL DOES NOT STOP, stated because a guard's blind spots are part of its
# meaning. Anything sharing the interpreter can eventually win: reaching into
# `pytest_collection_modifyitems.__defaults__` reaches the closure below. The
# criterion #924 set is COST, not impossibility -- the hatch must cost more than
# stating a reason, and that shape is unmistakably deliberate where two lines of
# assignment are not. It also checks at COLLECTION only, so a test that rebinds a
# name inside its own body and restores it is untouched; two arms in
# test_failed_query_sentinel.py do exactly that, legitimately.
#
# Module bindings are not the only writable surface. `Expect.num = a stub` leaves
# the name `Expect` pointing at the same class (#967), so the snapshot below cannot
# see it. Public methods of Expect are snapshotted separately, by identity, the
# same way. Leading-underscore names including `_record` are excluded: stubbing
# the recorder leaves the count at 0 and `pytest_runtest_call` refuses the test,
# which is a different mechanism and must stay the one that fires.
def _binding_guard():
    snapshot = {}
    missing = object()

    def arm(ns):
        snapshot.update({k: v for k, v in ns.items() if not k.startswith("__")})

    def changed(ns):
        return sorted(k for k, v in snapshot.items() if ns.get(k, missing) is not v)

    # RESTORED BEFORE THE REFUSAL, not after it, and this is load-bearing rather than
    # tidiness. `pytester` runs its inner session IN-PROCESS on this same module
    # object, so a rebind made by an inner conftest stays made for every test that
    # follows in the outer run -- the hazard `_RunShape` already paid for once. The
    # arms that prove this refusal would otherwise poison the rest of their own file.
    def restore(ns):
        ns.update(snapshot)

    return arm, changed, restore


def _public_attr_guard(cls):
    """Snapshot the public attributes of a class, by identity.

    #964's module snapshot cannot see `Expect.num = a stub`: the binding
    `Expect` is unchanged. This is the next frame (#967), and it is the
    CLASS dictionary. An instance attribute or a subclass yielded by an
    overridden fixture is a different object: `Expect.__dict__` is untouched,
    so this snapshot cannot see it. Names that start with `_` are excluded,
    so `Expect._record` stays the control: stubbing it leaves the count at 0
    and is refused by `pytest_runtest_call`, not here.
    """
    snapshot = {}
    missing = object()

    def arm():
        snapshot.update(
            {k: v for k, v in cls.__dict__.items() if not k.startswith("_")}
        )

    def changed():
        return sorted(
            f"{cls.__name__}.{k}"
            for k, v in snapshot.items()
            if cls.__dict__.get(k, missing) is not v
        )

    def restore():
        for k, v in snapshot.items():
            setattr(cls, k, v)

    return arm, changed, restore


_arm_bindings, _changed_bindings, _restore_bindings = _binding_guard()
_arm_expect, _changed_expect, _restore_expect = _public_attr_guard(Expect)


def pytest_collection_modifyitems(session, config, items,
                                  _changed=_changed_bindings,
                                  _restore=_restore_bindings,
                                  _changed_methods=_changed_expect,
                                  _restore_methods=_restore_expect):
    """Refuse a bare skip, which exits 0 and reads as success.

    Measured: two skipped tests report `2 skipped` and exit 0. A skip is allowed
    only through expect.cannot_run(), which names a reason from a closed list.
    """
    # FIRST, because every rule below is read through a name a conftest can write.
    # Captured in this signature at DEFINITION time, so rebinding `_changed_bindings`
    # or `_restore_bindings` on the module does not reach what runs here.
    _rebound = _changed(globals())
    _methods = _changed_methods()
    if _rebound or _methods:
        if _rebound:
            _restore(globals())
        if _methods:
            _restore_methods()
        names = _rebound + _methods
        # #963's reporter rather than a bare `raise`: a UsageError raised in a
        # WORKER never reaches the controller, so the refusal arrived as a bare
        # exit code. Both surfaces this hook now guards report through it.
        _collection_usage_error(
            session, config, items,
            "the pgColumnar vacuity layer refuses this run: a conftest or plugin "
            "rebound the layer's own "
            + ("names " if len(names) > 1 else "name ")
            + ", ".join(names)
            + " -- a rule this layer enforces is read through that binding, so the "
            "run would have reported on rules that were switched off. The bindings "
            "have been restored. If a check is wrong for your case, say so where the "
            "run records it: expect.cannot_run(REASON, detail), which names a reason "
            "from a closed list, or fix the test the rule is objecting to.",
        )
        return

    offenders = []
    seen_files = set()
    for item in items:
        for marker in ("skip", "skipif"):
            mk = item.get_closest_marker(marker)
            if mk is None:
                continue
            why = str(mk.kwargs.get("reason", "")) or (str(mk.args[0]) if mk.args else "")
            if "empty parameter set" in why:
                continue    # reported below, with a message about the real cause
            offenders.append(f"{item.name} carries a bare @pytest.mark.{marker}")
        f = str(getattr(item, "fspath", "") or "")
        if f and f not in seen_files:
            seen_files.add(f)
            for site in _broad_except_sites(f):
                offenders.append(f"{site} catches Exception broadly")
            offenders.extend(_sorted_ordered_sites(f))
            offenders.extend(_raises_sites(f))

    # An empty parametrize is not a bare skip and deserves its own message: pytest
    # generates ONE skipped placeholder for an empty argvalues list, so a corpus glob
    # that matched nothing turns a data-driven suite into a single "s" and exit 0.
    empty_params = []
    for item in items:
        m = item.get_closest_marker("skip")
        reason = ""
        if m is not None:
            reason = str(m.kwargs.get("reason", "")) or (
                str(m.args[0]) if m.args else "")
        if "empty parameter set" in reason:
            empty_params.append(f"{item.name}: {reason}")
    if empty_params:
        _collection_usage_error(
            session, config, items,
            "the pgColumnar vacuity layer refuses this run: a parametrize over an "
            "empty parameter set produces one skipped placeholder and exits 0, so a "
            "corpus that matched nothing reads as a suite that ran: "
            + "; ".join(empty_params)
            + " -- assert the corpus is non-empty before parametrizing over it.",
        )
        return
    if offenders:
        # One hook, two offences, so the message must say which. An earlier version
        # reused the skip wording and told a reader with a broad `except` to call
        # expect.cannot_run, which would not have helped them.
        skips = [o for o in offenders if "@pytest.mark." in o]
        excepts = [o for o in offenders if "catches Exception broadly" in o]
        ordered = [o for o in offenders if "feeds an ordered claim" in o]
        raises_broad = [o for o in offenders if "names no SQLSTATE" in o]
        # THE FILTER IS THE COMMON TAIL OF ALL THREE PHRASES. It was the exact
        # sentence of the statement-COUNT rule, so the two rules added for the helper
        # and compound shapes refused the run and then printed NOTHING -- the layer
        # said "refuses this run: ." and the arms could not tell a fired rule from an
        # unfired one. Measured: both new arms reddened on a missing message while the
        # refusal itself was working.
        raises_setup = [o for o in offenders if "raised is not pinned" in o]
        parts = []
        if skips:
            parts.append(
                "a bare skip is refused, because it exits 0 and reads as success: "
                + "; ".join(skips)
                + " -- use expect.cannot_run(REASON, detail) so the run cannot go quiet"
            )
        if excepts:
            parts.append(
                "a broad except swallows the failure the test exists to find, and "
                "after one failed statement psycopg raises for every later one: "
                + "; ".join(excepts)
                + " -- catch the specific exception class instead"
            )
        if ordered:
            parts.append(
                "sorted() or set() feeding an ordered claim removes the very "
                "ordering it asserts: "
                + "; ".join(ordered)
                + " -- pass the rows in the order the query returned them"
            )
        if raises_broad:
            parts.append(
                "pytest.raises over a whole error family is satisfied by an "
                "unrelated failure of the same family, and psycopg.Error covers "
                "254 SQLSTATEs while a failed connect carries none at all: "
                + "; ".join(raises_broad)
                + " -- pin the error with expect.sqlstate(exc.value, '42883', name)"
                  ", or name the specific exception class"
            )
        if raises_setup:
            parts.append(
                "a pytest.raises block must say WHICH statement raised, or a "
                "failure in the SETUP passes for a failure in the statement under "
                "test -- more than one statement, a compound statement holding "
                "several, or a call to a helper defined in the same file all hide it: "
                + "; ".join(raises_setup)
                + " -- move the setup above the block, leaving the statement under "
                  "test alone inside it"
            )
        _collection_usage_error(
            session, config, items,
            "the pgColumnar vacuity layer refuses this run: " + ". ".join(parts) + ".",
        )
        return


# ARMED HERE, AT THE BOTTOM, because a snapshot taken earlier would miss every name
# defined after it -- including this hook. Import order is what makes this safe: the
# module is fully executed before pytest imports any conftest, so nothing the policed
# tree writes can be in the snapshot.
_arm_bindings(globals())
_arm_expect()
