"""The ordered oracle, tested before it refuses anything.

lib.sh has pgc_seq_hash, diff_query_ordered and pgc_check_ordered_oracle; the port
had no way to express an ordered claim at all, so a test naming ORDER BY compared
with sorted lists could not fail on order (VACUITY_MODES.md, set-oracle-on-an-
ordered-claim).
"""


def test_ordered_rows_refuses_a_sequence_whose_order_is_unobservable(pytester, expect):
    """If every element is the same, forward equals reverse and order asserts nothing.

    This is pgc_check_ordered_oracle's premise ported: the bash version proves the
    oracle is order-sensitive by requiring forward != reverse on a real fixture.
    """
    pytester.makepyfile(
        """
        def test_all_the_same(expect):
            expect.ordered_rows(['x', 'x', 'x'], ['x', 'x', 'x'], "order of identicals")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(result, "an unobservable ordering is refused", failed=1, passed=0)
    result.stdout.fnmatch_lines(["*order cannot be observed*"])


def test_ordered_rows_refuses_two_empty_sequences(pytester, expect):
    pytester.makepyfile(
        """
        def test_both_empty(expect):
            expect.ordered_rows([], [], "two empty sequences")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(result, "two empty sequences are refused", failed=1, passed=0)


def test_ordered_rows_accepts_a_real_ordering(pytester, expect):
    """The positive control: a genuine ordered claim must still pass."""
    pytester.makepyfile(
        """
        def test_real_order(expect):
            expect.ordered_rows([1, 2, 3], [1, 2, 3], "a real ordering")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(result, "a real ordering passes", passed=1, failed=0)


def test_ordered_rows_fails_on_the_wrong_order(pytester, expect):
    """And it must actually detect order, not merely permit the claim."""
    pytester.makepyfile(
        """
        def test_wrong_order(expect):
            expect.ordered_rows([3, 1, 2], [1, 2, 3], "the wrong order")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(result, "the wrong order is caught", failed=1, passed=0)


def test_layer_refuses_sorting_the_input_to_an_ordered_claim(pytester, expect):
    """`ordered_rows(sorted(got), sorted(want))` cannot fail on order.

    This is the collapse itself: the helper is order-sensitive, so the vacuity is
    introduced at the CALL SITE. Found by ast, for the same reason the broad-except
    scan parses rather than greps.
    """
    pytester.makepyfile(
        """
        def test_sorted_into_an_ordered_claim(expect):
            got = [3, 1, 2]
            expect.ordered_rows(sorted(got), sorted([1, 2, 3]), "sorted away")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.run_failed(result, "sorting the input to an ordered claim is refused")
    result.stderr.fnmatch_lines(["*sorted*ordered*"])


def test_layer_refuses_a_name_bound_to_a_sorted_call(pytester, expect):
    """The same collapse, one line apart, and it read as more careful code.

    The inline spelling was the only one caught. Splitting it across two statements
    is what a reader does when the line gets long, so the guard was strictest on the
    version most likely to be noticed by a human and blind to the version least
    likely to be.
    """
    pytester.makepyfile(
        """
        def test_sorted_via_a_name(expect):
            got = [3, 1, 2]
            g = sorted(got)
            expect.ordered_rows(g, [1, 2, 3], "sorted away, one line earlier")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.run_failed(result, "a name bound to sorted() is refused")
    result.stderr.fnmatch_lines(["*order-killed*"])


def test_layer_refuses_a_list_sorted_in_place(pytester, expect):
    """`got.sort()` kills the order and leaves the name spelled exactly as before.

    Nothing at the call site says anything happened, which makes this the hardest
    of the three to see in review and the one worth catching most.
    """
    pytester.makepyfile(
        """
        def test_sorted_in_place(expect):
            got = [3, 1, 2]
            got.sort()
            expect.ordered_rows(got, [1, 2, 3], "sorted in place")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.run_failed(result, "a list sorted in place is refused")
    result.stderr.fnmatch_lines(["*order-killed*"])


def test_layer_allows_a_name_sorted_after_the_claim(pytester, expect):
    """The control, and the reason the guard compares line numbers.

    A name sorted AFTER the ordered claim did not affect it. Refusing that would be
    a false red, which is the defect this layer exists to refuse rather than commit.
    """
    pytester.makepyfile(
        """
        def test_sorted_afterwards(expect):
            got = [1, 2, 3]
            expect.ordered_rows(got, [1, 2, 3], "a real ordering claim")
            got.sort()          # after the claim; it changed nothing about it
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(result, "sorting after the claim is not the collapse",
                    passed=1, failed=0)


def test_a_conftest_cannot_switch_off_the_order_collapse_guard(pytester, expect):
    """#924. The scan reads `_ORDER_KILLERS` from the module, and a conftest
    is imported before collection, so two lines switch the guard off.

    Measured on main: the same collapse test is uncollectable with no extra
    file, and reports `1 passed` when the only extra file is

        import pgc_vacuity
        pgc_vacuity._ORDER_KILLERS = ()

    That is less to type than the honest form, and the run records no reason.
    The layer already closed this shape for QUERY_ERROR by binding the prefix
    at definition time; the killer list was still a module-level name.
    """
    pytester.makepyfile(
        """
        def test_order_collapsed(expect):
            got = ["b", "a"]
            g = sorted(got)
            expect.ordered_rows(g, ["a", "b"], "rows in order")
        """
    )
    plain = pytester.runpytest("-p", "pgc_vacuity")
    expect.run_failed(plain, "the collapse is refused when nobody rebinds")
    plain.stderr.fnmatch_lines(["*order-killed*"])

    pytester.makeconftest(
        """
        import pgc_vacuity
        pgc_vacuity._ORDER_KILLERS = ()
        """
    )
    hatched = pytester.runpytest("-p", "pgc_vacuity")
    expect.run_failed(hatched, "and it is still refused after a conftest rebinds the name")
    hatched.stderr.fnmatch_lines(["*order-killed*"])


def test_the_order_killer_scan_is_one_function_deep(pytester, expect):
    """A named limit, pinned so it cannot quietly become a claim of completeness.

    A helper that sorts and returns is invisible to this scan. That is a real gap and
    it is recorded here rather than in prose alone: if someone widens the scan later,
    this arm reddens and tells them the documented limit has moved.
    """
    pytester.makepyfile(
        """
        def _tidy(rows):
            return sorted(rows)

        def test_sorted_behind_a_helper(expect):
            got = [3, 1, 2]
            expect.ordered_rows(_tidy(got), [1, 2, 3], "sorted behind a helper")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(result, "a sort behind a helper is NOT caught, by design",
                    passed=1, failed=0)


def test_ordering_observable_requires_the_two_directions_to_differ(pytester, expect):
    """The ported premise: a fixture that reads the same forwards and backwards
    cannot support an ordering claim at all."""
    pytester.makepyfile(
        """
        def test_premise_fails(expect):
            expect.ordering_observable(['a', 'a'], ['a', 'a'], "a flat fixture")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(result, "a flat fixture is refused as a premise", failed=1, passed=0)


def test_ordering_observable_passes_when_the_directions_differ(pytester, expect):
    pytester.makepyfile(
        """
        def test_premise_holds(expect):
            expect.ordering_observable([1, 2, 3], [3, 2, 1], "a real fixture")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(result, "a real fixture passes the premise", passed=1, failed=0)


def test_the_two_oracles_are_different_instruments(pytester, expect):
    """pgc_check_ordered_oracle's third property, ported.

    The ordered oracle must be order-SENSITIVE and the set oracle order-BLIND. Without
    this control an ordered oracle could be implemented as a set one and every
    ordering test in the tree would go silent while staying green.
    """
    pytester.makepyfile(
        """
        from pgc_vacuity import VacuityError

        FWD = [1, 2, 3]
        REV = [3, 2, 1]

        def test_the_set_oracle_is_order_blind(expect):
            # Order-blind BY DESIGN: the same rows in any order compare equal.
            expect.row_set(FWD, REV, "set oracle ignores order")

        def test_the_ordered_oracle_is_order_sensitive(expect):
            # And the ordered one must NOT: this comparison has to fail.
            # A REFUSAL IS NOT A DETECTION (#1339). VacuityError subclasses
            # AssertionError, so catching only the latter counted an oracle that
            # refuses every input as one that saw the order: with ordered_rows
            # raising VacuityError unconditionally, this test passed.
            try:
                expect.ordered_rows(FWD, REV, "ordered oracle sees order")
            except VacuityError as exc:
                raise AssertionError(f"the ordered oracle refused instead of comparing: {exc}")
            except AssertionError:
                expect.num(1, 1, "the ordered oracle refused the reversed sequence")
            else:
                raise AssertionError("the ordered oracle did not detect the reversal")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(result, "the two oracles behave differently", passed=2, failed=0)


def test_row_set_still_refuses_two_empty_sides(pytester, expect):
    """The set oracle inherits rows()'s refusal rather than losing it."""
    pytester.makepyfile(
        """
        def test_both_empty(expect):
            expect.row_set([], [], "two empty sets")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.outcomes(result, "an empty set comparison is refused", failed=1, passed=0)
