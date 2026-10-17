"""A failed query must not be comparable with another failed query (#432).

`error-swallowed-to-empty`: two queries raise, a helper turns each into the same
value, and they compare equal. The test is green and has asserted nothing about
either query.

`lib.sh` closed this by PRODUCING a sentinel with a sequence number per failure --
`res="QUERY_ERROR.$seq"` -- so two failures can never compare equal. The pytest port
had the constant `QUERY_ERROR = "QUERY_ERROR"` and a comment claiming it was "unique
per occurrence", which was false of a constant, and a refusal in exactly one
assertion. Measured before this file existed, with the sentinel on both sides:

    expect.hash          REFUSED
    expect.text          PASSED
    expect.rows          PASSED
    expect.row_set       PASSED
    expect.ordered_rows  PASSED

So four of the five comparisons accepted two failed queries as agreement.

THE MECHANISM IS THE REFUSAL, and `query_error()` is the second line rather than the
first. A non-unique sentinel is safe against the layer, because no comparison
accepts one at all; it is NOT safe against a helper that compares by hand, which is
why the producer exists and why new code should use it.
"""
import ast
import inspect
import pathlib

import pgc_vacuity
from pgc_vacuity import QUERY_ERROR, VacuityError, query_error


# Every public assertion that compares two caller-supplied values. DERIVED from the
# signature rather than listed, so an assertion added later is covered by the arm
# below instead of being the next hole -- which is how this mode survived: `hash`
# had a refusal and the four written after it did not.
#
# THE EXCLUSION IS DERIVED TOO (#938). Selection used to be the only rule, so
# "not selected" was a residue: `wrote(cur, want, name)` sat outside because its
# first parameter is not called `got`, which happens to be the right answer and
# would also be the answer for a future comparison whose first parameter was
# `left`. A method matching neither rule must fail an arm, not land in a third
# bucket. The kind of the left operand is a property of the signature; this layer
# has no annotations, so the first parameter name is how the signature records that
# kind.
def _params(fn):
    return list(inspect.signature(fn).parameters)[1:]


def _is_comparison(fn):
    params = _params(fn)
    # (forward, reverse) AS WELL AS (got, want). `ordering_observable` compares two
    # caller-supplied readings under different parameter names, so a derivation
    # keyed on "got" missed it -- and it was the one assertion the unique producer
    # made WEAKER, turning a loud red into a silent pass. Reported by @jdatcmd.
    return len(params) >= 2 and (
            (params[0] == "got" and params[1] in ("want", "floor"))
            or (params[0], params[1]) == ("forward", "reverse"))


# First-parameter names that mean the left operand is not a caller-supplied value.
# The map is keyed on a property of the signature, not on the method name: a new
# method whose first parameter is `cur` is excluded for the same reason `wrote`
# is, and a method whose first parameter is `left` matches neither rule.
_LEFT_OPERAND_KIND = {
    "cur": "cursor: the count comes from cur.rowcount, a sentinel cannot arrive",
    "result": "inner pytest run result, not a query value",
    "plan": "EXPLAIN plan, not a query value",
    "exc": "exception object, not a query value",
    "reason": "unrunnable declaration, not a comparison",
    "capability": "a decline on an optional capability (#1215), not a comparison",
    "module": "an optional module imported or declined on (#1215), not a comparison",
}


def _exclusion_reason(fn):
    params = _params(fn)
    if not params:
        return None
    return _LEFT_OPERAND_KIND.get(params[0])


def _partition(cls):
    selected, excluded, residue = [], [], []
    for name, fn in inspect.getmembers(cls, inspect.isfunction):
        if name.startswith("_"):
            continue
        sel = _is_comparison(fn)
        reason = _exclusion_reason(fn)
        if sel and reason is not None:
            residue.append((name, "matches both rules"))
        elif sel:
            selected.append((name, _params(fn)[1]))
        elif reason is not None:
            excluded.append((name, reason))
        else:
            residue.append((name, "matches neither rule"))
    return (
        sorted(selected),
        sorted(excluded),
        sorted(residue),
    )


def _comparisons():
    selected, _, _ = _partition(pgc_vacuity.Expect)
    return selected


def test_the_comparison_surface_is_what_this_file_thinks_it_is(expect):
    """premise: the derivation finds the assertions, so the arm below is not vacuous."""
    names = [n for n, _ in _comparisons()]
    expect.at_least(len(names), 5, "the layer offers at least five (got, want) comparisons")
    for required in ("hash", "text", "rows", "row_set", "ordered_rows",
                     "ordering_observable", "differ"):
        expect.num(names.count(required), 1, f"{required} is one of them")


# What a VALID pair looks like for each comparison, so the sentinel can be put in
# one side and the other side stays something the assertion would otherwise accept.
#
# DECLARED, and the premise below requires it to cover every comparison the
# derivation finds. That is the anti-drift part: an assertion added to the layer
# without an entry here fails the premise, rather than being quietly skipped by an
# arm that looked like it covered everything. The first version of this arm fudged
# the shapes in a loop and never placed a sentinel in `at_least`'s floor at all --
# it reported `at_least ACCEPTED a failed query`, which was my fixture's fault and
# not the layer's.
VALID = {
    "hash":          ("abc", "abc"),
    "text":          ("abc", "abc"),
    "num":           (7, 7),
    "rowcount":      (7, 7),
    "at_least":      (7, 1),
    "rows":          ([(1,), (2,)], [(1,), (2,)]),
    "row_set":       ([(1,), (2,)], [(2,), (1,)]),
    "ordered_rows":  ([(1,), (2,)], [(1,), (2,)]),
    # Forward and reverse must DIFFER, or the assertion refuses the fixture for a
    # different reason and the arm would pass without testing the sentinel.
    "ordering_observable": ([(1,), (2,)], [(2,), (1,)]),
    # The two arms must DIFFER for the same reason, and for `differ` the sentinel is
    # the INVERSE trap: `query_error()` makes each failure unique so two failures
    # cannot compare equal, which makes them compare UNEQUAL -- so without its
    # refusal this assertion reports two blown-up statements as an observable
    # difference. The fix for one direction opened the other.
    "differ":        ("abc", "xyz"),
    # The haystack must actually CONTAIN the needle, or the assertion fails for a
    # reason that is not the sentinel and the arm proves nothing.
    "contains":      ("abcdef", "cde"),
}

# NOT EVERY ASSERTION IS IN THIS SWEEP. `wrote` is outside it because its left
# operand is a cursor, not because its first parameter fails to be called `got`.
# The partition below is what makes that a decision rather than a residue.

# How a sentinel arrives for each: bare for a scalar comparison, and as a CELL for a
# row comparison, because that is what a one-column query that failed looks like
# after a helper swallowed the error.
def _as_side(name, sentinel):
    if name in ("rows", "row_set", "ordered_rows"):
        return [(sentinel,), (2,)]
    return sentinel


def test_the_shape_table_covers_every_comparison_the_layer_offers(expect):
    """premise: no comparison is silently outside the arm below."""
    missing = sorted(n for n, _ in _comparisons() if n not in VALID)
    expect.num(len(missing), 0, f"every comparison has a declared valid pair; missing: {missing}")


def test_every_public_assertion_is_selected_or_excluded(expect):
    """#938. No third state: a method matching neither rule is the silent hole.

    Selection remains the derivation this file already had. Exclusion is a
    POSITIVE match on the kind of the left operand, so "not selected" is no
    longer a bucket. `inputs == selected + excluded` fails when a method matches
    neither rule, which is what happens today when a parameter is not called `got`.
    """
    selected, excluded, residue = _partition(pgc_vacuity.Expect)
    public = [n for n, fn in inspect.getmembers(pgc_vacuity.Expect, inspect.isfunction)
              if not n.startswith("_")]
    expect.num(len(selected) + len(excluded), len(public),
               f"inputs {len(public)} == selected {len(selected)} + excluded {len(excluded)}")
    expect.num(len(residue), 0,
               f"no public method matches neither rule; residue: {residue}")
    for name, reason in excluded:
        expect.at_least(len(reason.split()), 3,
                        f"{name} is excluded with a stated reason, not a missing entry")


def test_wrote_is_excluded_because_its_left_operand_is_a_cursor(expect):
    """The case that showed the residue was an accident of naming."""
    excluded = dict(_partition(pgc_vacuity.Expect)[1])
    expect.num(1 if "wrote" in excluded else 0, 1, "wrote is in the excluded bucket")
    expect.num(1 if "cursor" in excluded["wrote"] else 0, 1,
               "and the reason is the cursor, not the method name")


def test_a_caller_supplied_value_not_named_got_fails_the_partition(expect):
    """Acceptance: a value comparison whose first parameter is not `got` is residue.

    A declared name-list of exclusions would put this method in the same silent
    bucket `wrote` used to occupy. The partition must go red instead.
    """
    class Probe:
        def num(self, got, want, name):
            return (got, want, name)
        def wrote(self, cur, want, name):
            return (cur, want, name)
        def eq(self, left, want, name):
            return (left, want, name)

    selected, excluded, residue = _partition(Probe)
    residue_names = [n for n, _ in residue]
    expect.num([n for n, _ in selected].count("num"), 1, "num(got, want) is selected")
    expect.num([n for n, _ in excluded].count("wrote"), 1,
               "wrote(cur, want) is excluded once the left operand is a cursor")
    expect.num(residue_names.count("eq"), 1,
               "eq(left, want) matches neither rule, so it is residue rather than excluded")


def test_every_comparison_refuses_a_failed_query_on_either_side(expect):
    """The arm that would have caught this mode, phrased over the whole surface.

    Two DISTINCT sentinel values, because the interned constant makes `got is want`
    true and `hash` would then refuse for THAT reason -- a refusal that says nothing
    about sentinels. This is the control the original `hash` arm did not have.
    """
    e = pgc_vacuity.Expect("sentinel::derivation")
    checked = 0
    for name, _second in _comparisons():
        fn = getattr(e, name)
        good_got, good_want = VALID[name]
        for side in ("left", "right"):
            s = query_error(side)
            got = _as_side(name, s) if side == "left" else good_got
            want = _as_side(name, s) if side == "right" else good_want
            try:
                fn(got, want, "an arm over a failed query")
            except VacuityError:
                checked += 1
            except AssertionError as exc:
                raise AssertionError(
                    f"{name} COMPARED a failed query on the {side} instead of refusing "
                    f"it, so a pair of failures would have compared equal: {exc}"
                )
            else:
                raise AssertionError(
                    f"{name} ACCEPTED a failed query on the {side} and passed"
                )
    expect.num(checked, 2 * len(_comparisons()),
               "every comparison refused the sentinel on both sides")


def test_row_set_refuses_before_it_maps_rather_than_after(expect):
    """The delegation case, which the first version of the fix got wrong.

    `row_set` hands `rows` a list of repr STRINGS, and `repr(("QUERY_ERROR.1",))` is
    `"('QUERY_ERROR.1',)"` -- it does not start with the prefix. So a refusal living
    only in `rows` cannot see a sentinel that arrived as a cell. Delegating an
    assertion does not delegate its refusals when the delegation transforms the data.
    """
    e = pgc_vacuity.Expect("sentinel::rowset")
    try:
        e.row_set([(query_error(),)], [(query_error(),)], "two failed one-column queries")
    except VacuityError:
        expect.num(1, 1, "row_set refuses a sentinel that arrived as a cell")
    else:
        raise AssertionError("row_set accepted a sentinel cell")


def test_the_producer_is_unique_per_occurrence(expect):
    """The property the old comment claimed and the constant did not have."""
    values = [query_error() for _ in range(50)]
    expect.num(len(set(values)), 50, "fifty occurrences are fifty distinct values")
    expect.num(sum(1 for v in values if v.startswith(QUERY_ERROR)), 50,
               "and every one of them still carries the prefix the refusals match")
    expect.text(query_error("no-partition").split(".")[-1], "no-partition",
                "a detail survives into the value, so a log line says which query failed")


def test_the_constant_alone_is_not_unique_which_is_why_the_producer_exists(expect):
    """The control for the arm above: the thing that was wrong, still measurable.

    Compared in plain Python rather than through `expect`, because the layer now
    REFUSES to compare two sentinels -- which is the whole point, and which means an
    arm about sentinel equality cannot use the assertion it is describing. The first
    version of this arm did, and was refused by the guard it exists to document.
    """
    expect.num(1 if QUERY_ERROR == QUERY_ERROR else 0, 1,
               "the bare constant equals itself, in plain Python")
    expect.num(len({QUERY_ERROR, QUERY_ERROR}), 1,
               "so two failures that both assigned it would have compared equal")
    expect.num(len({query_error(), query_error()}), 2,
               "where two calls to the producer are two values")


def test_the_refusal_cannot_be_switched_off_from_the_corpus_it_polices(expect):
    """Every conftest under test/pytest/ is imported before collection, so a corpus file
    can rewrite this module's globals. The refusal must survive that.

    THE FIRST VERSION OF THIS ARM WAS TAUTOLOGICAL and @jdatcmd measured it. It rewrote
    `pgc_vacuity.QUERY_ERROR` and THEN minted its sentinels with `query_error()`, which
    read that same global -- so producer and matcher moved together and the refusal
    matched whatever the prefix had just been set to. It could not fail for the property
    it names.

    The faithful hatch is this one: mint while the layer is armed, rewrite afterwards,
    which is what a corpus file actually does. Against the old code that COMPARED two
    sentinels instead of refusing them, and an end-to-end corpus file reported
    `1 passed` over two failed queries.

    `ZZZ_NOT_A_PREFIX` is in the list deliberately. `'Q'` alone cannot disarm a
    prefix-reading matcher, because `'QUERY_ERROR.1'.startswith('Q')` is true -- so a
    spelling that is not a prefix of the real one is the case that would have found the
    hole, and it is the case the old arm never tried.
    """
    e = pgc_vacuity.Expect("sentinel::hatch")
    original = pgc_vacuity.QUERY_ERROR
    for spelling in ("", "NOTHING_MATCHES_THIS", "Q", "ZZZ_NOT_A_PREFIX"):
        a, b = query_error("a"), query_error("b")      # minted WHILE ARMED
        # Compared in plain Python: `expect.text` now refuses a value carrying the
        # prefix, which is the point, and an arm ABOUT sentinels therefore cannot use
        # the assertion it is describing on one.
        expect.num(1 if a.split(".")[0] == original else 0, 1,
                   f"premise: minted under the real prefix ({spelling!r} case)")
        pgc_vacuity.QUERY_ERROR = spelling             # the corpus rewrites it after
        try:
            e.text(a, b, "a rewritten prefix")
        except VacuityError:
            expect.num(1, 1, f"the refusal survives the prefix being set to {spelling!r}")
        except AssertionError:
            raise AssertionError(
                f"rewriting QUERY_ERROR to {spelling!r} disarmed the refusal: the two "
                f"sentinels were COMPARED instead of refused"
            )
        else:
            raise AssertionError(f"rewriting QUERY_ERROR to {spelling!r} disarmed the refusal")
        finally:
            pgc_vacuity.QUERY_ERROR = original
    expect.num(1 if pgc_vacuity.QUERY_ERROR == original else 0, 1,
               "and the module is left as it was found")


def test_a_hardcoded_sentinel_survives_the_same_rewrite(expect):
    """The corpus hardcodes sentinel text that no producer minted --
    `'QUERY_ERROR.empty-relation'` and `f"QUERY_ERROR.no-partition-for-{table}"` in
    `test_hilbert_locality.py`, and `"QUERY_ERROR.1"` in `test_guards_pinned.py`. One of
    those is built inside SQL by `coalesce(...)`, so it can never come from
    `query_error()`. A matcher that read a rewritable global would stop seeing them.
    """
    e = pgc_vacuity.Expect("sentinel::hardcoded")
    original = pgc_vacuity.QUERY_ERROR
    left = "QUERY_ERROR.empty-relation"
    right = "".join(["QUERY_ERROR", ".empty-relation"])   # equal, distinct objects
    expect.num(1 if left is not right else 0, 1,
               "premise: the two are distinct objects, so identity guards cannot fire")
    pgc_vacuity.QUERY_ERROR = "NOTHING_MATCHES_THIS"
    try:
        e.text(left, right, "two hardcoded sentinels after a rewrite")
    except VacuityError:
        expect.num(1, 1, "a hardcoded sentinel is still refused")
    except AssertionError:
        raise AssertionError("the hardcoded sentinel was COMPARED after the rewrite")
    else:
        raise AssertionError("the hardcoded sentinel was accepted after the rewrite")
    finally:
        pgc_vacuity.QUERY_ERROR = original


def test_the_ordering_premise_refuses_a_failed_reading(expect):
    """The one assertion the unique producer made WEAKER before this arm existed.

    `ordering_observable` requires forward and reverse to differ. With the old shared
    constant two failed readings were IDENTICAL, so it went red -- loudly, for the wrong
    reason but in the right direction. With unique sentinels they differ, so it passed
    and greenlit every ordered assertion resting on the premise. Measured by @jdatcmd,
    and it is why the refusal is now the first thing that assertion does.
    """
    e = pgc_vacuity.Expect("sentinel::ordering")
    for label, fwd, rev in (
            ("both readings failed", [(query_error("f"),)], [(query_error("r"),)]),
            ("one reading failed", [(query_error("f"),), (1,)], [(1,), (2,)])):
        try:
            e.ordering_observable(fwd, rev, "the ordering premise")
        except VacuityError:
            expect.num(1, 1, f"the ordering premise refuses when {label}")
        except AssertionError:
            raise AssertionError(f"{label}: compared instead of refused")
        else:
            raise AssertionError(f"{label}: the ordering premise PASSED on a failed query")


def test_a_legitimate_comparison_is_untouched(expect):
    """The cost side. A refusal that also refuses real data is not a refusal."""
    e = pgc_vacuity.Expect("sentinel::cost")
    e.text("abc", "abc", "equal text still passes")
    e.rows([(1, "a")], [(1, "a")], "equal rows still pass")
    e.row_set([(1,), (2,)], [(2,), (1,)], "a set comparison still ignores order")
    e.num(7, 7, "equal numbers still pass")
    expect.num(1, 1, "four honest comparisons passed through the new refusal")


# A HELPER THAT MINTS ITS OWN FAILURE TEXT REOPENS THE MODE (#1525 review). Seventeen
# twins caught a failed query and returned f"error: {message}": not the producer's
# prefix, so no refusal fires, and two failures with the same message compare equal.
# Measured on test_native_skip.py's copy: with the backend aborting on one LIKE, the
# check on that query PASSED, as did two more read on the dead connection afterwards,
# and the run named no red at all.
#
# Parsed, not grepped, for the reason the layer's other scans give: a string literal
# that merely contains the shape is not an except handler.
def _minted_failure_text(source):
    """-> line numbers where an except handler builds a string starting "error" from
    the error's message. A SQLSTATE alone is a typed field, which a test compares
    with the code it expects (test_rls_query_paths.py's "ERROR: 0A000"), so an
    f-string whose every interpolation is a `.sqlstate` is not this shape."""
    def starts_error(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value.lower().startswith("error")
        if isinstance(node, ast.JoinedStr) and node.values:
            return starts_error(node.values[0]) and not all(
                isinstance(v.value, ast.Attribute) and v.value.attr == "sqlstate"
                for v in node.values if isinstance(v, ast.FormattedValue))
        return False
    tree = ast.parse(source)
    # an f-string's literal parts are judged with the f-string, not alone
    parts = {id(v) for j in ast.walk(tree) if isinstance(j, ast.JoinedStr) for v in j.values}
    hits = []
    for h in ast.walk(tree):
        if isinstance(h, ast.ExceptHandler):
            hits += [n.lineno for stmt in h.body for n in ast.walk(stmt)
                     if id(n) not in parts and starts_error(n)]
    return sorted(set(hits))


def test_the_minted_failure_scan_sees_the_shape(expect):
    """The scan's own control: it finds the shape, and not the producer's form."""
    bad = ("def f(c):\n    try:\n        return c()\n"
           "    except Exception as exc:\n        return f'error: {exc}'\n")
    good = bad.replace("f'error: {exc}'", "query_error(str(exc))")
    code = bad.replace("f'error: {exc}'", "f'error {exc.sqlstate}'")
    in_a_string = "SRC = " + repr(bad) + "\n"
    expect.text(" ".join(str(_minted_failure_text(x)) for x in (bad, good, code, in_a_string)),
                "[5] [] [] []", "the scan finds a minted failure string, and not "
                "query_error(), a SQLSTATE, or the shape inside a literal")


def test_no_test_mints_its_own_failure_text(expect):
    """A failed query becomes a value through query_error(), so it is refused, never
    compared. The corpus is every test file and helper under test/pytest/.

    The scan keys on the spelling the tree used, a handler value starting "error"
    (any case, f-string, `+`, `.format` or `%`). A handler that builds other text from
    the exception (`f"failed: {exc}"`, a bare `str(exc)`) is not seen: most such
    handlers are deliberate refusal capture, compared with a fixed message or
    SQLSTATE, and the mode is only live when that text reaches a two-sided
    comparison (#1527 review)."""
    here = pathlib.Path(__file__).resolve().parent
    hits = [f"{p.name}:{n}" for p in sorted(here.glob("*.py"))
            for n in _minted_failure_text(p.read_text())]
    expect.text(" ".join(hits) or "none", "none",
                "no except handler turns a failed query into its own error text")
