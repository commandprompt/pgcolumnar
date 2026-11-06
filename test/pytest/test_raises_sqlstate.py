"""A raised database error must name WHICH error, and the block must hold one statement.

`VACUITY_MODES.md` section 3.4 listed `raises-too-broad` and `raises-catches-setup`
together, and the measurement that opened this file shows why they belong together.
Run on unmodified main at de8fca4, with the layer loaded and nothing skipped:

    with pytest.raises(psycopg.Error):
        conn = psycopg.connect("host=/nonexistent-socket-dir dbname=pgc")
        conn.execute("SELECT pgc_definitely_no_such_function()")
    expect.num(1, 1, "the server rejected the call")

reported `2 passed`, exit 0. The error that satisfied the claim was
`OperationalError` with `sqlstate` None -- it never reached a server, and the
statement the test is about never executed. Against a live PostgreSQL 18.4 the same
shape raised `InvalidName` 42602 from the SETUP line while the statement under test
raises `UndefinedObject` 42704: two different SQLSTATEs satisfy one `raises`.

So there are two properties, and each needs its own arm:

  1. a broad family must be narrowed to a SQLSTATE -- `psycopg.Error` covers 254 of
     them across 42 SQLSTATE classes, measured against psycopg 3.3.5;
  2. the block must hold exactly one statement, or nothing says which one raised.

THE TWO PROPERTIES CLOSE DIFFERENT AMOUNTS. Property 1 CLOSES `raises-too-broad`:
a broad family with nothing pinned cannot be collected. Property 2 only NARROWS
`raises-catches-setup`, because it counts TOP-LEVEL statements: a call to a helper
that performs the setup is one statement, and so is a `for` holding the setup and
the statement under test. Two more rules now refuse those two shapes: the block may
hold no compound statement, and it may not call a function defined in the same
file. `test_a_helper_hiding_the_setup_is_refused` and
`test_a_compound_statement_hiding_the_setup_is_refused` assert that the scan
refuses both. Other shapes still pass -- a comprehension, a tuple of calls, a
helper defined in another file -- so `raises-catches-setup` stays in
`VACUITY_MODES.md` section 3.4 rather than moving to section 2.

THE SCAN IS AST, NOT A LINE REGEX, and this file is the reason. Every arm below
writes the forbidden shape inside a `pytester.makepyfile` string, because that is
how the layer's own tests drive an inner run. A line regex fires on this file and
refuses it -- the false positive the broad-except scan already paid for once.
`test_the_raises_scan_reads_code_not_a_string_literal` pins that.
"""

import pathlib


CONF = "pytest_plugins = ['pgc_vacuity']"


def _inner(pytester, body):
    pytester.makeconftest(CONF)
    pytester.makepyfile(body)
    return pytester.runpytest("-p", "pgc_vacuity")


# ---------------------------------------------------------------------------
# The static scan: which error, and which statement.
# ---------------------------------------------------------------------------

def test_raises_requires_a_sqlstate(pytester, expect):
    """The red test VACUITY_MODES.md section 5 item 4 names, and the real offender.

    Byte for byte the shape measured as `2 passed` on main. It is refused before
    collection finishes, so it cannot run at all rather than running and passing.
    """
    pytester.makepyfile(
        """
        import psycopg
        import pytest

        def test_an_unknown_function_is_rejected(expect):
            with pytest.raises(psycopg.Error):
                conn = psycopg.connect("host=/nonexistent-socket-dir dbname=pgc")
                conn.execute("SELECT pgc_definitely_no_such_function()")
            expect.num(1, 1, "the server rejected the call")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.run_failed(result, "a broad raises with no SQLSTATE must not be collectable")
    result.stderr.fnmatch_lines(["*pytest.raises(Error) names no SQLSTATE*"])


def test_a_raises_that_pins_the_sqlstate_is_accepted(pytester, expect):
    """The positive control, and the one that matters most.

    A guard that rejects the honest form gets switched off. The honest form is the
    same `pytest.raises(psycopg.Error)` with the error named afterwards, and it
    must collect and pass.
    """
    result = _inner(pytester, '''
        import psycopg
        import pytest

        def test_an_unknown_function_is_42883(expect):
            with pytest.raises(psycopg.Error) as exc:
                raise psycopg.errors.UndefinedFunction("no such function")
            expect.sqlstate(exc.value, "42883", "an unknown function is 42883")
        ''')
    expect.outcomes(result, "the honest form collects and passes", passed=1, failed=0)


def test_a_raises_pinned_by_reading_the_field_is_accepted(pytester, expect):
    """The second honest spelling: the field read directly rather than through the
    helper. Both are claims about a typed field, so the scan accepts both."""
    result = _inner(pytester, '''
        import psycopg
        import pytest

        def test_the_field_itself(expect):
            with pytest.raises(psycopg.Error) as exc:
                raise psycopg.errors.UndefinedFunction("no such function")
            expect.text(exc.value.sqlstate, "42883", "the SQLSTATE field itself")
        ''')
    expect.outcomes(result, "reading the field counts as pinning it",
                    passed=1, failed=0)


def test_a_narrow_raises_needs_no_sqlstate(pytester, expect):
    """Control for the scope of the rule. A single-SQLSTATE class already names
    the error, so demanding a second spelling of the same claim would be noise."""
    result = _inner(pytester, '''
        import psycopg
        import pytest

        def test_narrow(expect):
            with pytest.raises(psycopg.errors.UndefinedFunction):
                raise psycopg.errors.UndefinedFunction("no such function")
            expect.num(1, 1, "the narrow class is the claim")
        ''')
    expect.outcomes(result, "a one-SQLSTATE class is not refused", passed=1, failed=0)


def test_a_raises_tuple_hides_a_broad_member(pytester, expect):
    """@jdatcmd's #905 hole, in the guard written after it.

    The broad-except scan looked only at a bare name, so `except Exception` was
    refused and `except (ValueError, Exception)` passed. A tuple is how a narrow
    claim gets widened under pressure, which is the exact moment the guard is for.
    """
    pytester.makepyfile(
        """
        import psycopg
        import pytest

        def test_tuple(expect):
            with pytest.raises((ValueError, psycopg.Error)):
                raise psycopg.errors.UndefinedFunction("no such function")
            expect.num(1, 1, "something of some family failed")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.run_failed(result, "a broad member of a tuple is still broad")
    result.stderr.fnmatch_lines(["*pytest.raises(Error) names no SQLSTATE*"])


def test_raises_exception_is_refused_like_a_broad_except(pytester, expect):
    """`except Exception` is already uncollectable; `pytest.raises(Exception)` was
    not, and it swallows the same failures for the same reason."""
    pytester.makepyfile(
        """
        import pytest

        def test_anything_at_all(expect):
            with pytest.raises(Exception):
                raise RuntimeError("the real failure")
            expect.num(1, 1, "something went wrong")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.run_failed(result, "pytest.raises(Exception) must not be collectable")
    result.stderr.fnmatch_lines(["*pytest.raises(Exception) names no SQLSTATE*"])


def test_setup_inside_a_raises_block_is_refused(pytester, expect):
    """`raises-catches-setup`, in the spelling this rule DOES close. The block is
    narrow AND pinned, and still vacuous: two statements inside it, so the failure
    may be either one.

    THE INNER FILE IS SELF-CONTAINED ON PURPOSE. Written against a bare `conn` it
    failed with `NameError`, which satisfies `run_failed` without anything having
    refused it -- the arm would have been green on a tree with no guard at all.
    With the stub below and no guard, the inner run reports `1 passed`: the SET
    raises 42704, the pin accepts it, and the ALTER the test is named for never
    runs. That is the defect, and it is what this arm has to redden.
    """
    pytester.makepyfile(
        """
        import psycopg
        import pytest

        class _Conn:
            def execute(self, sql):
                raise psycopg.errors.UndefinedObject("no such object: " + sql)

        conn = _Conn()

        def test_which_one_raised(expect):
            with pytest.raises(psycopg.errors.UndefinedObject) as exc:
                conn.execute("SET pgcolumnar.no_such_guc = 1")
                conn.execute("ALTER TABLE t SET ACCESS METHOD no_such_am")
            expect.sqlstate(exc.value, "42704", "the ALTER was refused")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.run_failed(result, "two statements in the block must not be collectable")
    result.stderr.fnmatch_lines(["*holds 2 statements, so which one raised*"])


def test_a_raises_block_with_one_statement_is_accepted(pytester, expect):
    """Control for the arm above, differing in exactly one property: the setup
    moved above the block, leaving the statement under test alone inside it."""
    result = _inner(pytester, '''
        import psycopg
        import pytest

        def test_one_statement(expect):
            with pytest.raises(psycopg.errors.UndefinedFunction) as exc:
                raise psycopg.errors.UndefinedFunction("no such function")
            expect.sqlstate(exc.value, "42883", "the statement under test failed")
        ''')
    expect.outcomes(result, "one statement in the block is the honest form",
                    passed=1, failed=0)


def test_the_raises_scan_reads_code_not_a_string_literal(pytester, expect):
    """The false positive this scan is written to avoid, pinned so it cannot return.

    The inner file contains the refused shape inside a `makepyfile` string, exactly
    as every arm in this file does. A line regex refuses it; `ast` does not see a
    handler or a call inside a string.
    """
    result = _inner(pytester, '''
        def test_quotes_the_shape(expect):
            forbidden = """
                import psycopg, pytest
                def test_inner(expect):
                    with pytest.raises(psycopg.Error):
                        conn = psycopg.connect("host=/nope")
                        conn.execute("SELECT 1")
            """
            expect.at_least(len(forbidden), 1,
                            "the shape above is a string, not code")
        ''')
    expect.outcomes(result, "the forbidden shape inside a string is not refused",
                    passed=1, failed=0)


# ---------------------------------------------------------------------------
# expect.sqlstate, the honest form the scan points at.
# ---------------------------------------------------------------------------

def test_sqlstate_refuses_a_sqlstate_class_prefix(pytester, expect):
    """"42" is a SQLSTATE CLASS. Accepting it would make the pin a prefix claim
    wearing the spelling of an exact one -- the substring defect, one layer up."""
    expect.refusal(_inner(pytester, '''
        import psycopg

        def test_prefix(expect):
            expect.sqlstate(psycopg.errors.UndefinedFunction("x"), "42", "a class")
        '''), "sqlstate refuses a two-character class", "is not a SQLSTATE")


def test_sqlstate_refuses_an_empty_expectation(pytester, expect):
    """An empty want names no error, so nothing could have failed it."""
    expect.refusal(_inner(pytester, '''
        import psycopg

        def test_empty_want(expect):
            expect.sqlstate(psycopg.errors.UndefinedFunction("x"), "", "nothing")
        '''), "sqlstate refuses an empty expectation", "is not a SQLSTATE")


def test_sqlstate_refuses_an_object_carrying_no_sqlstate(pytester, expect):
    """Passing pytest's ExceptionInfo instead of `exc.value` is the mistake that
    produces it, and it would otherwise compare None against a real code forever."""
    expect.refusal(_inner(pytester, '''
        def test_wrong_object(expect):
            expect.sqlstate(RuntimeError("not a database error"), "42883", "wrong")
        '''), "sqlstate refuses an object with no sqlstate", "carries no sqlstate")


def test_sqlstate_fails_when_the_failure_never_reached_the_server(pytester, expect):
    """The measured case. A connect to a socket that does not exist raises
    OperationalError with sqlstate None: a psycopg.Error that is no server error."""
    result = _inner(pytester, '''
        import psycopg

        def test_no_sqlstate_at_all(expect):
            exc = psycopg.OperationalError("connection failed")
            expect.sqlstate(exc, "42883", "an unknown function")
        ''')
    expect.outcomes(result, "a failure with no SQLSTATE is not the named error",
                    failed=1, passed=0)
    result.stdout.fnmatch_lines(["E*carrying no SQLSTATE*"])


def test_sqlstate_fails_on_a_different_sqlstate(pytester, expect):
    """The whole point: the error measured from the setup line was 42602 and the
    statement under test raises 42704. One `raises` accepts both; this does not."""
    result = _inner(pytester, '''
        import psycopg

        def test_the_other_error(expect):
            expect.sqlstate(psycopg.errors.InvalidName("bad name"), "42704",
                            "the ALTER was refused")
        ''')
    expect.outcomes(result, "a different SQLSTATE fails", failed=1, passed=0)
    result.stdout.fnmatch_lines(["E*got SQLSTATE '42602' want '42704'*"])


def test_sqlstate_accepts_the_exact_sqlstate(pytester, expect):
    """Positive control for all five arms above."""
    result = _inner(pytester, '''
        import psycopg

        def test_exact(expect):
            expect.sqlstate(psycopg.errors.UndefinedFunction("x"), "42883",
                            "an unknown function is 42883")
        ''')
    expect.outcomes(result, "the exact SQLSTATE passes", passed=1, failed=0)


def test_sqlstate_accepts_one_of_several_named_codes(pytester, expect):
    """This tree supports majors 15 through 19, so an error code can legitimately
    differ between them. A tuple widens the claim by exactly the codes it names."""
    result = _inner(pytester, '''
        import psycopg

        def test_either_code(expect):
            expect.sqlstate(psycopg.errors.UndefinedObject("x"), ("42883", "42704"),
                            "one of the two codes this major uses")
        ''')
    expect.outcomes(result, "a named pair of codes passes", passed=1, failed=0)


def test_sqlstate_refuses_an_empty_set_of_codes(pytester, expect):
    """The escape hatch must not become the hole. An empty tuple is satisfied by
    nothing, so it could not have passed and says nothing about which error came."""
    expect.refusal(_inner(pytester, '''
        import psycopg

        def test_no_codes(expect):
            expect.sqlstate(psycopg.errors.UndefinedFunction("x"), (), "nothing")
        '''), "sqlstate refuses an empty set of codes",
        "an empty set of SQLSTATEs is satisfied by nothing")


# ---------------------------------------------------------------------------
# What this scan must NOT do. Both of these were proven failures on a sibling
# branch in this layer, so they are arms here rather than a sentence.
# ---------------------------------------------------------------------------

def test_the_raises_scan_leaves_the_unrunnable_state_alone(pytester, expect):
    """A documented hatch the corpus does not exercise, exercised.

    `VACUITY_MODES.md` section 3.3 promises `cannot_run` is not a skip: the run
    prints `UNRUN`, counts it, and exits `EXIT_INCOMPLETE`. A sibling guard in this
    layer measured zero false positives over all 148 tests and still broke that,
    because nothing in the corpus declares itself unrunnable. The file here also
    holds a legitimate pinned `raises`, so the scan has run over it.
    """
    result = _inner(pytester, '''
        import psycopg
        import pytest

        def test_pins_its_error(expect):
            with pytest.raises(psycopg.Error) as exc:
                raise psycopg.errors.UndefinedFunction("no such function")
            expect.sqlstate(exc.value, "42883", "an unknown function is 42883")

        def test_cannot_run_here(expect):
            expect.cannot_run("MISSING_DEPENDENCY", "duckdb is not installed here")
        ''')
    expect.num(result.ret, 67, "the run still exits EXIT_INCOMPLETE")
    result.stdout.fnmatch_lines(["*checks unrunnable: 1*"])


def test_the_raises_scan_does_not_touch_a_recorder_made_in_the_body(pytester, expect):
    """A recorder fetched during the CALL phase must still satisfy the layer.

    A sibling guard read the recorder before pytest's call hook yielded, so a test
    that asks for `expect` itself was reddened with a neighbouring guard's message.
    This scan runs at collection time and cannot do that; the arm is here so a later
    edit that moves it cannot do it either.
    """
    result = _inner(pytester, '''
        import psycopg
        import pytest

        def test_asserts_in_its_own_body(request):
            expect = request.getfixturevalue("expect")
            expect.num(2 + 2, 4, "arithmetic, concluded by the test body itself")

        def test_pins_its_error(expect):
            with pytest.raises(psycopg.Error) as exc:
                raise psycopg.errors.UndefinedFunction("no such function")
            expect.sqlstate(exc.value, "42883", "an unknown function is 42883")
        ''')
    expect.outcomes(result, "a recorder made in the body still satisfies the layer",
                    passed=2, failed=0)


# ---------------------------------------------------------------------------
# THE TWO SHAPES THE ONE-STATEMENT RULE MISSED, NOW REFUSED. The one-statement
# rule counts TOP-LEVEL statements in the block. Both shapes below are one top-level
# statement that performs the setup inside the block. Each gets an arm that asserts
# the scan REFUSES it: a helper call by the same-file rule, a compound statement by
# `_COMPOUND_STATEMENTS`. `raises-catches-setup` is still NARROWED, not closed: the
# shapes that still pass are listed in `VACUITY_MODES.md` section 3.4.
# ---------------------------------------------------------------------------

def test_a_helper_hiding_the_setup_is_refused(pytester, expect):
    """Shape 1 of the two `raises-catches-setup` residuals: a CALL TO A HELPER.

    One top-level statement, a narrow class, a pinned SQLSTATE -- and the setup
    raised while the statement the test names never ran. The statement COUNT cannot
    see this, because a helper call is one statement.

    THE RULE IS NOT A DEEPER COUNT. Counting recursively would also refuse a
    legitimate single-statement loop. The claim is about which statement raised, so
    the block may not call a function DEFINED IN THE SAME FILE: such a function can
    run any number of statements, and nothing in the block says which of them failed.
    A call to an imported function or to a method is the thing under test and stays
    allowed -- that is the shape all five `pytest.raises` blocks in this corpus use.
    """
    result = _inner(pytester, '''
        import psycopg
        import pytest

        def _do_the_thing(flag):
            if flag:
                raise psycopg.errors.UndefinedObject("SET pgcolumnar.no_such_guc")
            raise psycopg.errors.UndefinedFunction("the statement under test")

        def test_the_alter_is_refused(expect):
            with pytest.raises(psycopg.errors.UndefinedObject) as exc:
                _do_the_thing(True)
            expect.sqlstate(exc.value, "42704", "the statement under test")
        ''')
    expect.run_failed(result, "a helper call inside the block must not be collectable")
    result.stderr.fnmatch_lines(["*calls _do_the_thing(), defined in this file*"])


def test_a_compound_statement_hiding_the_setup_is_refused(pytester, expect):
    """Shape 2 of the two: a COMPOUND STATEMENT holding setup and the statement.

    A `for` over two statements is ONE top-level statement, so the count rule was
    satisfied while the loop raised on its first iteration -- the setup -- and the
    statement the test names never ran.
    """
    result = _inner(pytester, '''
        import psycopg
        import pytest

        def test_the_alter_is_refused(expect):
            with pytest.raises(psycopg.errors.UndefinedObject) as exc:
                for stmt in ("SET pgcolumnar.no_such_guc = 1",
                             "ALTER TABLE t SET ACCESS METHOD no_such_am"):
                    raise psycopg.errors.UndefinedObject("no such object: " + stmt)
            expect.sqlstate(exc.value, "42704", "the statement under test")
        ''')
    expect.run_failed(result, "a loop inside the block must not be collectable")
    result.stderr.fnmatch_lines(["*holds a For, so which statement inside it raised*"])


def test_a_helper_hidden_in_an_assignment_is_refused_too(pytester, expect):
    """The rule looks ANYWHERE in the statement, not only at the whole of it.

    `x = _helper()` hides the setup exactly as well as a bare `_helper()` does, and a
    rule that matched only `Expr` would leave the assignment spelling open. Without
    this arm, narrowing the search to a bare call changes nothing and the narrowing
    is invisible -- which is how the `for` spelling came to be the only one the
    inventory named.
    """
    result = _inner(pytester, '''
        import psycopg
        import pytest

        def _setup_then_run():
            raise psycopg.errors.UndefinedObject("SET pgcolumnar.no_such_guc")

        def test_the_alter_is_refused(expect):
            with pytest.raises(psycopg.errors.UndefinedObject) as exc:
                outcome = _setup_then_run()
            expect.sqlstate(exc.value, "42704", "the statement under test")
        ''')
    expect.run_failed(result, "a helper hidden in an assignment must not be collectable")
    result.stderr.fnmatch_lines(["*calls _setup_then_run(), defined in this file*"])


def test_every_compound_statement_is_refused_not_only_a_loop(pytester, expect):
    """Every compound statement nests exactly as `for` does, so each is driven.

    The inventory named the `for` spelling. A rule that caught only that one would
    leave the other spellings of the same shape, which is the difference between
    closing a mode and closing an example of it. `_COMPOUND_STATEMENTS` lists nine
    kinds; `for` has its own test above, and this one drives the other eight.

    THE ASYNC KINDS RUN, and are not merely written. Python lets them stand only in
    an `async def`, and pytest without an async plugin FAILS an `async def` test --
    which `run_failed` would read as a refusal whatever the scan did. So the block
    sits in a coroutine the test runs with `asyncio.run`, over stdlib iterables (a
    helper defined in the file would be refused by the other rule), and the inner run
    passes when nothing refuses it, as it does for the other six kinds.
    """
    raise_x = 'raise psycopg.errors.UndefinedObject("x")'
    pad = "\n" + " " * 20
    bodies = {
        "If":        f'if True:{pad}{raise_x}',
        "While":     f'while True:{pad}{raise_x}',
        "With":      f'with open("/dev/null"):{pad}{raise_x}',
        "Try":       f'try:{pad}{raise_x}\n                finally:{pad}pass',
        "TryStar":   f'try:{pad}{raise_x}\n                except* ValueError:{pad}pass',
        "Match":     f'match 1:{pad}case _:{pad}    {raise_x}',
    }
    apad = "\n" + " " * 24
    async_bodies = {
        "AsyncFor":  f'async for _ in asyncio.as_completed([asyncio.sleep(0)]):{apad}{raise_x}',
        "AsyncWith": f'async with asyncio.timeout(60):{apad}{raise_x}',
    }
    sources = {kind: f'''
        import psycopg
        import pytest

        def test_the_alter_is_refused(expect):
            with pytest.raises(psycopg.errors.UndefinedObject) as exc:
                {body}
            expect.sqlstate(exc.value, "42704", "the statement under test")
        ''' for kind, body in bodies.items()}
    sources.update({kind: f'''
        import asyncio
        import psycopg
        import pytest

        def test_the_alter_is_refused(expect):
            async def _run():
                with pytest.raises(psycopg.errors.UndefinedObject) as exc:
                    {body}
                return exc
            exc = asyncio.run(_run())
            expect.sqlstate(exc.value, "42704", "the statement under test")
        ''' for kind, body in async_bodies.items()})
    for kind, source in sources.items():
        result = _inner(pytester, source)
        expect.run_failed(result, f"a {kind} inside the block must not be collectable")
        result.stderr.fnmatch_lines([f"*holds a {kind}, so which statement inside it raised*"])


def test_a_raises_block_calling_an_imported_function_is_accepted(pytester, expect):
    """The false-positive budget, and it is the shape the corpus actually uses.

    Four of the five `pytest.raises` blocks in `test/pytest/` call
    `build_and_install(...)`, imported from the module under test. Refusing a call
    because it is a call would refuse every one of them, so the rule turns on where
    the function is DEFINED rather than on the statement being a call.
    """
    result = _inner(pytester, '''
        import psycopg
        import pytest
        from json import loads

        def test_the_thing_under_test_raises(expect):
            with pytest.raises(ValueError) as exc:
                loads("{not json")
            expect.at_least(len(str(exc.value)), 1, "the imported call raised")
        ''')
    expect.outcomes(result, "a call to an IMPORTED function is the thing under test",
                    passed=1, failed=0)


def test_a_raises_block_calling_a_method_is_accepted(pytester, expect):
    """The fifth block's shape: a method call on an object.

    A method cannot be matched against this file's function definitions, and the
    object it belongs to is usually the thing under test. This is a stated residual
    rather than an oversight: a method that performs setup and then the statement is
    invisible to this rule, and no static rule can see inside it.
    """
    result = _inner(pytester, '''
        import psycopg
        import pytest

        class _Conn:
            def execute(self, sql):
                raise psycopg.errors.UndefinedObject("no such object: " + sql)

        def test_the_alter_is_refused(expect):
            conn = _Conn()
            with pytest.raises(psycopg.errors.UndefinedObject) as exc:
                conn.execute("ALTER TABLE t SET ACCESS METHOD no_such_am")
            expect.sqlstate(exc.value, "42704", "the ALTER was refused")
        ''')
    expect.outcomes(result, "a method call is accepted, and why is recorded",
                    passed=1, failed=0)


# ---------------------------------------------------------------------------
# The rule's own parameters must not be reachable from the tree it polices.
# ---------------------------------------------------------------------------

def test_a_conftest_cannot_switch_the_broad_family_list_off(pytester, expect):
    """A conftest that rewrites the scan's family list must change nothing.

    Every `conftest.py` under `test/pytest/` is imported before collection, so a
    module-level tuple is writable from the corpus the rule polices: four lines in a
    conftest empty the list, the scan then reports zero offences, and the suite is
    green with the guard switched off and nothing saying so.

    The list is therefore bound inside `_raises_sites` rather than at module level.
    The conftest below writes BOTH plausible spellings of the name onto the module
    and then presents the refused shape. The run must still be refused.

    WHAT THIS ARM DOES NOT COVER, and the sentence above was read as covering it
    (#924, @OffgridwithJD). Binding the list inside the reader protects the DATA.
    `_raises_sites` -- the reader itself -- is a module-level name, and
    `pgc_vacuity._raises_sites = lambda p: []` switched this exact rule off,
    measured. None of the three names this conftest writes has ever existed on the
    module, so this arm cannot fail on that: it pins that GUESSING the data name
    does not work, which is a smaller claim than it reads as.

    The reader is covered by the binding snapshot instead, and by
    `test_a_conftest_cannot_switch_off_the_raises_scan` in test_layer.py. It is not
    re-asserted here: two files claiming the same thing is how one of them stops
    being read.
    """
    pytester.makeconftest('''
        import pgc_vacuity

        pgc_vacuity._BROAD_RAISES = ()
        pgc_vacuity.broad_families = ()
        pgc_vacuity.BROAD_RAISES = ()
        ''')
    pytester.makepyfile(
        """
        import psycopg
        import pytest

        def test_an_unknown_function_is_rejected(expect):
            with pytest.raises(psycopg.Error):
                raise psycopg.errors.UndefinedFunction("no such function")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.run_failed(result, "a conftest writing the family list does not disable "
                              "the rule")
    result.stderr.fnmatch_lines(["*pytest.raises(Error) names no SQLSTATE*"])


# ---- MENTIONING THE FIELD IS NOT PINNING IT ----------------------------------
#
# `_sqlstate_pinned_names` counted any `ast.Attribute` named `sqlstate` anywhere in
# the body, so naming the field switched the broad-family rule off. Both shapes below
# collected CLEAN against that version, and both are byte-for-byte the vacuity this
# file is named for -- any of the 254 SQLSTATEs satisfies them. Reported by @jdatcmd,
# who drove the scan over six constructed files instead of reading it.
#
# The rule now requires the read to reach a CALL, and follows one hop of assignment
# so the honest `code = exc.value.sqlstate` / `expect.text(code, ...)` form is not
# refused. One hop, not two: this is a floor, and the floor is stated.


def test_a_bare_sqlstate_expression_does_not_pin_anything(pytester, expect):
    """`exc.value.sqlstate` as a statement of its own asserts nothing at all."""
    pytester.makepyfile(
        """
        import psycopg
        import pytest

        def test_an_unknown_function_is_rejected(expect):
            with pytest.raises(psycopg.Error) as exc:
                conn = psycopg.connect("host=/nonexistent-socket-dir dbname=pgc")
            exc.value.sqlstate
            expect.num(1, 1, "the server rejected the call")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.run_failed(result, "mentioning the field is not pinning it")
    result.stderr.fnmatch_lines(["*pytest.raises(Error) names no SQLSTATE*"])


def test_a_sqlstate_assigned_and_never_read_does_not_pin_anything(pytester, expect):
    """The same hole with one more step: bound to a name nothing uses."""
    pytester.makepyfile(
        """
        import psycopg
        import pytest

        def test_an_unknown_function_is_rejected(expect):
            with pytest.raises(psycopg.Error) as exc:
                conn = psycopg.connect("host=/nonexistent-socket-dir dbname=pgc")
            code = exc.value.sqlstate
            expect.num(1, 1, "the server rejected the call")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.run_failed(result, "an unread assignment is not a pin")
    result.stderr.fnmatch_lines(["*pytest.raises(Error) names no SQLSTATE*"])


def test_one_hop_through_a_local_name_is_an_honest_pin(pytester, expect):
    """The cost side. Refusing this would outlaw a form nobody should stop writing,
    and a guard that refuses honest tests is a guard somebody switches off."""
    pytester.makepyfile(
        """
        import psycopg
        import pytest

        def test_an_unknown_function_is_rejected(expect):
            with pytest.raises(psycopg.Error) as exc:
                conn = psycopg.connect("host=/nonexistent-socket-dir dbname=pgc")
            code = exc.value.sqlstate
            expect.text(code, "42883", "the function does not exist")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    # STDERR, NOT STDOUT. The layer reports a refusal as a usage error, which goes to
    # stderr, so a check of stdout for the offence could never fail.
    expect.num(sum("names no SQLSTATE" in ln for ln in result.stderr.lines), 0,
               "and the scan reports no unpinned SQLSTATE for a one-hop pin")
    expect.outcomes(result, "a pin reached through one local name is collectable",
                    passed=0, failed=1, errors=0)


# ---- THE CLASS MAY ARRIVE BY KEYWORD ----------------------------------------
#
# `if tail != "raises" or not call.args: continue` skipped the item BEFORE it was
# appended, so `pytest.raises(expected_exception=...)` was checked by neither rule --
# and the statement rule was therefore silently conditional on the class being
# positional, which this file's own prose stated unconditionally. Reported by
# @jdatcmd, who built the two forms as a pair differing in nothing else: the
# positional one reported two offences and the keyword one reported none.


def test_the_keyword_form_is_checked_by_both_rules(pytester, expect):
    """Broad, unbound, and two statements, with the class passed by keyword."""
    pytester.makepyfile(
        """
        import psycopg
        import pytest

        def test_an_unknown_function_is_rejected(expect):
            with pytest.raises(expected_exception=psycopg.Error):
                conn = psycopg.connect("host=/nonexistent-socket-dir dbname=pgc")
                conn.execute("SELECT pgc_definitely_no_such_function()")
            expect.num(1, 1, "the server rejected the call")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    expect.run_failed(result, "the keyword form is not an exemption")
    result.stderr.fnmatch_lines(["*pytest.raises(Error) names no SQLSTATE*"])
    result.stderr.fnmatch_lines(["*holds 2 statements*"])


def test_the_keyword_form_with_a_pin_is_collectable(pytester, expect):
    """And it is not refused merely for being the keyword form."""
    pytester.makepyfile(
        """
        import psycopg
        import pytest

        def test_an_unknown_function_is_rejected(expect):
            with pytest.raises(expected_exception=psycopg.Error) as exc:
                conn = psycopg.connect("host=/nonexistent-socket-dir dbname=pgc")
            expect.sqlstate(exc.value, "42883", "the function does not exist")
        """
    )
    result = pytester.runpytest("-p", "pgc_vacuity")
    # STDERR, NOT STDOUT, for the reason the one-hop arm above gives.
    expect.num(sum("names no SQLSTATE" in ln for ln in result.stderr.lines), 0,
               "and the scan reports no unpinned SQLSTATE for a pinned keyword form")
    expect.outcomes(result, "a pinned keyword form is collectable",
                    passed=0, failed=1, errors=0)


# ---- THE NEUTERING PROOF, WHICH A TEXT PIN CANNOT CARRY ---------------------
#
# A shell part that checked this scan by grepping its source (the old
# `test/selftest/440-a-raises-must-name-a-sqlstate.sh`, deleted in 294cb8cb) catches a
# rewrite or a deletion but not `False and` -- which is how a guard actually dies.
# @jdatcmd measured three faithful neuterings (prefix `False and`, rename nothing,
# leave every pinned substring in place) and all three left that part at 55 passed
# while the scan went blind.
#
# THE PROOF BELONGS HERE, not there: the shell harness and this corpus are parallel
# in functionality and do not drive each other. So this arm copies the layer, disables
# one condition faithfully, imports the copy, and requires the copy to go blind to a
# file the real layer refuses. It fails if the scan stops being load-bearing, whatever
# the neutering looks like.


def _layer_copy(tmp_path, old, new, name):
    """A copy of pgc_vacuity with ONE condition disabled, imported under its own name."""
    import importlib.util
    src = pathlib.Path(__file__).with_name("pgc_vacuity.py").read_text()
    assert old in src, "the condition to disable is not in the layer verbatim: %r" % old
    # FAITHFUL BY CONSTRUCTION: the neutering keeps the condition's text after its
    # `if`, so a grep for it still matches the twin. This is a property of `new`, not
    # of the layer, so it is asserted here once rather than measured as a check that
    # cannot fail.
    assert old.removeprefix("if ") in new, "the neutering must keep the condition's text: %r" % new
    twin = tmp_path / (name + ".py")
    twin.write_text(src.replace(old, new, 1))
    spec = importlib.util.spec_from_file_location(name, twin)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, twin


def _offence_fixture(tmp_path):
    f = tmp_path / "fixture_case.py"
    f.write_text(
        "import psycopg\n"
        "import pytest\n\n"
        "def test_it(conn, expect):\n"
        "    with pytest.raises(psycopg.Error):\n"
        "        conn.execute('SELECT pgc_no_such()')\n"
        "        conn.execute('SELECT 1')\n"
        "    expect.num(1, 1, 'rejected')\n"
    )
    return f


def test_disabling_the_sqlstate_rule_makes_the_scan_blind(tmp_path, expect):
    """Faithful: `False and` prefixed, nothing renamed, the pinned text left intact."""
    import pgc_vacuity
    case = _offence_fixture(tmp_path)
    real = [s for s in pgc_vacuity._raises_sites(case) if "names no SQLSTATE" in s]
    expect.num(len(real), 1, "the real layer reports the unpinned broad raises")
    twin, path = _layer_copy(
        tmp_path,
        "if broad and (bound is None or bound not in pinned):",
        "if False and broad and (bound is None or bound not in pinned):",
        "twin_sqlstate",
    )
    blind = [s for s in twin._raises_sites(case) if "names no SQLSTATE" in s]
    expect.num(len(blind), 0, "while the twin reports none, which no text pin can see")


def test_disabling_the_statement_rule_makes_the_scan_blind(tmp_path, expect):
    """The second condition, same shape, so neither rule rests on the other's arm."""
    import pgc_vacuity
    case = _offence_fixture(tmp_path)
    real = [s for s in pgc_vacuity._raises_sites(case) if "statements" in s]
    expect.num(len(real), 1, "the real layer reports the two-statement block")
    twin, path = _layer_copy(
        tmp_path,
        "if sites and len(node.body) != 1:",
        "if False and sites and len(node.body) != 1:",
        "twin_statements",
    )
    blind = [s for s in twin._raises_sites(case) if "statements" in s]
    expect.num(len(blind), 0, "while the twin reports none")

def test_the_mode_this_layer_only_narrows_is_still_listed_as_open(expect):
    """`raises-catches-setup` must stay in VACUITY_MODES.md section 3.

    Property 1 CLOSES `raises-too-broad`. Property 2 only narrows
    `raises-catches-setup`: the helper-call and compound-statement shapes are refused
    (asserted above), but a comprehension, a tuple of calls and a helper in another
    file still pass. A document that quietly moved the
    mode to section 2 would claim a closure this scan does not make, which is the
    failure a map makes worse than a silence.

    Reading a document is the one thing this corpus and the shell harness may both do:
    they are parallel in functionality and do not drive each other, and the docs are
    where they are allowed to meet.
    """
    import re
    doc = pathlib.Path(__file__).with_name("VACUITY_MODES.md").read_text()
    # SECTION 3 RUNS TO SECTION 4, not to 3.1. Splitting on "## 3." truncated at the
    # first subsection heading and reported the mode missing from its own section --
    # my own arm failing for a reason that had nothing to do with the document.
    start = re.search(r"^## 3\. ", doc, re.M)
    end = re.search(r"^## 4\. ", doc, re.M)
    expect.text("found" if start and end else "missing", "found",
                "section 3 and section 4 both have headings to bound by")
    section3 = doc[start.end():end.start()]
    expect.at_least(doc.count("raises-catches-setup"), 1,
                    "the document still names the mode this layer only narrows")
    expect.text("open" if "raises-catches-setup" in section3 else "moved", "open",
                "and names it in the section for what is NOT refused")
    # APPEARING IN SECTION 3 IS NOT THE SAME AS BEING OPEN, and this arm passed while
    # the document said the opposite of what it exists to assert. Section 3 keeps a
    # back-reference for every mode that MOVES -- "`X` is now closed" -- so the id is
    # present in section 3 whichever state it is in. A branch that wrote
    # "`raises-catches-setup` is now closed." into section 3 and added the row to
    # section 2 satisfied the line above and moved the refused count to 29.
    #
    # The arm once split on "is now closed" for the OTHER mode, and did not apply the
    # same care to its own subject. Two further conditions, because
    # either alone can be satisfied by the wrong document: the id must appear in section
    # 3 OUTSIDE any closure back-reference, and it must not appear in section 2 at all.
    # Found by taking the closure claim seriously enough to measure the residuals, which
    # showed the mode is reachable by a comprehension and by a helper one file over.
    # PER LINE, NOT BY SPLITTING, and my first version of this condition got that wrong.
    # `split("is now closed")` truncates at the FIRST back-reference in section 3, which
    # today belongs to `insert-wrote-no-rows` and sits ABOVE this mode's entry -- so the
    # check reported "only as a back-reference" about a document that names the mode
    # correctly. A positional test on a section that holds several back-references is a
    # test about their order.
    named_open = [ln for ln in section3.splitlines()
                  if "raises-catches-setup" in ln and "is now closed" not in ln]
    expect.at_least(len(named_open), 1,
                    "and names it OUTSIDE a closure back-reference, which reads the same way")
    section2 = doc[re.search(r"^## 2\. ", doc, re.M).end():start.start()]
    expect.text("absent" if "raises-catches-setup" not in section2 else "claimed refused",
                "absent",
                "and does not also claim it refused in the section for what IS refused")
    # PER LINE for the closed mode too. `split("is now closed")[0]` read section 3 only
    # up to its FIRST back-reference, which belongs to another mode and sits above
    # this one, so a line calling `raises-too-broad` open further down was never read.
    loose = [ln for ln in section3.splitlines()
             if "raises-too-broad" in ln and "is now closed" not in ln]
    expect.num(len(loose), 0,
               "and the mode this layer DOES close is not loose in section 3")
