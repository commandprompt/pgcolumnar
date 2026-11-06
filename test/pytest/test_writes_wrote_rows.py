"""A write that wrote nothing must not pass as a fixture that built something.

`INSERT ... SELECT ... WHERE false` writes no rows and raises nothing. psycopg
reports `INSERT 0 0` with `rowcount == 0`, and until this guard existed nobody in
the corpus read either: the fixture built the wrong situation, and every assertion
downstream of it compared two empty things. That is `insert-wrote-no-rows` in
VACUITY_MODES.md section 3.5.

THE TAG IS THE SIGNAL, NOT THE SQL TEXT. A zero `rowcount` alone cannot tell a
write that wrote nothing from a SELECT that matched nothing -- both are 0. The
server's own command tag separates them, so this guard never parses SQL. Measured
on PG 18 against a pgcolumnar table, which is where these values come from:

    statusmessage       rowcount   statement
    CREATE TABLE              -1   CREATE TABLE t (i int) USING pgcolumnar
    INSERT 0 5                 5   INSERT INTO t SELECT g FROM generate_series(1,5) g
    INSERT 0 0                 0   INSERT ... WHERE false
    UPDATE 0                   0   UPDATE t SET i = i WHERE i > 100
    DELETE 0                   0   DELETE FROM t WHERE i > 100
    SELECT 0                   0   SELECT * FROM t WHERE false
    SET                       -1   SET search_path TO public
    TRUNCATE TABLE            -1   TRUNCATE t

THE ARMS HERE NEED NO DATABASE, and that is deliberate rather than convenient: the
thing under test is the classifier and the refusal, which a stub cursor carrying
those two measured fields exercises exactly. The separate question -- whether the
connection the tests actually use is watched at all -- is a different property and
cannot be proven here. It has its own arm in `test_connection.py`, for the reason
#917 had to learn twice: proving a function and proving its call site are two
proofs, and the second one is the one that goes missing.
"""

import re

import pgc_vacuity


class _Cur:
    """The two fields of a psycopg cursor this guard reads, and nothing else."""

    def __init__(self, statusmessage, rowcount):
        self.statusmessage = statusmessage
        self.rowcount = rowcount


def _writes(nodeid):
    return pgc_vacuity._WRITES.get(nodeid, [])


def test_a_write_that_wrote_nothing_is_recorded(expect, request):
    nodeid = request.node.nodeid + "::probe-1"
    pgc_vacuity.note_write(nodeid, _Cur("INSERT 0 0", 0))
    recorded = _writes(nodeid)
    expect.num(len(recorded), 1, "an INSERT reporting 0 rows is recorded")
    expect.num(recorded[0].count, 0, "and the count it carries is the zero")
    expect.text(recorded[0].tag, "INSERT", "and the tag is the command, not the SQL")
    pgc_vacuity._WRITES.pop(nodeid, None)


def test_update_and_delete_are_writes_too(expect, request):
    for tag, message in (("UPDATE", "UPDATE 0"), ("DELETE", "DELETE 0")):
        nodeid = request.node.nodeid + "::" + tag
        pgc_vacuity.note_write(nodeid, _Cur(message, 0))
        recorded = _writes(nodeid)
        expect.num(len(recorded), 1, f"a {tag} reporting 0 rows is recorded")
        # The count and the tag, as the INSERT arm reads them: the refusal acts on the
        # count and names the tag, so a record carrying neither refuses nothing (#1339).
        expect.num(recorded[0].count, 0, f"and the {tag}'s count is the zero")
        expect.text(recorded[0].tag, tag, f"and its tag is {tag}")
        pgc_vacuity._WRITES.pop(nodeid, None)


def test_a_select_matching_nothing_is_not_a_write(expect, request):
    """The arm the rowcount-only version of this guard would fail.

    `SELECT 0` and `INSERT 0 0` both carry rowcount 0. A guard keyed on the count
    alone would refuse every test whose last statement was a SELECT over an empty
    result, which is a legitimate and common thing to assert.
    """
    nodeid = request.node.nodeid + "::select"
    pgc_vacuity.note_write(nodeid, _Cur("SELECT 0", 0))
    expect.num(len(_writes(nodeid)), 0, "a SELECT returning no rows is not a write")
    pgc_vacuity._WRITES.pop(nodeid, None)


def test_ddl_is_not_a_write(expect, request):
    for message in ("CREATE TABLE", "SET", "TRUNCATE TABLE", "DROP SCHEMA"):
        nodeid = request.node.nodeid + "::" + message.replace(" ", "_")
        pgc_vacuity.note_write(nodeid, _Cur(message, -1))
        expect.num(len(_writes(nodeid)), 0, f"{message} is not a write that wrote rows")
        pgc_vacuity._WRITES.pop(nodeid, None)


def test_a_write_that_wrote_rows_needs_no_acknowledgement(expect, request):
    nodeid = request.node.nodeid + "::ok"
    pgc_vacuity.note_write(nodeid, _Cur("INSERT 0 5", 5))
    recorded = _writes(nodeid)
    expect.num(len(recorded), 1, "a write that wrote rows is still recorded")
    expect.num(recorded[0].count, 5, "with the count it reported")
    pgc_vacuity._WRITES.pop(nodeid, None)


# ---- the guard, through a real inner run -------------------------------------
#
# An inner run rather than a direct call on the hook, because the property is "the
# test FAILS", and only a run can report that. Same shape as test_layer.py's arms.

_PROLOGUE = """
    import pgc_vacuity

    class _Cur:
        def __init__(self, statusmessage, rowcount):
            self.statusmessage = statusmessage
            self.rowcount = rowcount
"""


def _inner(pytester, body):
    pytester.makeconftest("pytest_plugins = ['pgc_vacuity']")
    pytester.makepyfile(_PROLOGUE + body)
    return pytester.runpytest("-p", "pgc_vacuity")


def test_an_unacknowledged_zero_row_write_fails_the_test(expect, pytester):
    result = _inner(pytester, """
    def test_fixture_built_nothing(expect, request):
        pgc_vacuity.note_write(request.node.nodeid, _Cur("INSERT 0 0", 0))
        expect.num(1, 1, "an assertion, so the no-assertion guard is not what fires")
    """)
    expect.outcomes(result, "a test whose write wrote nothing does not pass",
                    passed=0, failed=1)
    # ONE PATTERN PER CALL. `expect.refusal` anchors each pattern to a single `E`
    # line, and pytest WORD-WRAPS a long traceback line -- so two patterns need two
    # lines and a multi-word phrase can straddle the wrap. Measured: matching
    # "wrote no rows" failed against a message that contained it, which reads as
    # "the guard did not fire". Single unbreakable tokens, one per call.
    expect.refusal(result, "and the refusal names the mode", r"insert-wrote-no-rows")
    expect.refusal(result, "and it names the command that moved nothing", r"INSERT")


def test_a_write_that_wrote_rows_passes_unacknowledged(expect, pytester):
    """The guard refuses a zero, not a write.

    This was a record-level check, "nothing about it is a zero-row write", which
    could not fail once the count read 5: one record carrying 5 is never a zero
    (#1339). The property is the test's OUTCOME, so only a run can show it. A guard
    that refused every unacknowledged write passed this whole file before this arm.
    """
    result = _inner(pytester, """
    def test_fixture_built_five(expect, request):
        pgc_vacuity.note_write(request.node.nodeid, _Cur("INSERT 0 5", 5))
        expect.num(1, 1, "an assertion, so the no-assertion guard is not what fires")
    """)
    expect.outcomes(result, "a test whose write wrote rows passes without naming it",
                    passed=1, failed=0)


def test_expect_wrote_acknowledges_the_zero(expect, pytester):
    """A zero-row write is legitimate when it is the thing being asserted.

    A negative control -- a DELETE that must delete nothing -- is a real test, and
    the guard must not make it unwritable. Naming the zero is the acknowledgement.
    """
    result = _inner(pytester, """
    def test_deliberately_wrote_nothing(expect, request):
        cur = _Cur("DELETE 0", 0)
        pgc_vacuity.note_write(request.node.nodeid, cur)
        expect.wrote(cur, 0, "the delete matched nothing, which is the point")
    """)
    expect.outcomes(result, "naming the zero lets the test pass", passed=1, failed=0)


def test_expect_wrote_refuses_a_count_that_is_not_a_count(expect, pytester):
    """rowcount is -1 when the statement produced no count.

    Comparing -1 with an expected number would read as a mismatch, which is the
    right verdict for the wrong reason. The same trap `expect.rowcount` records.
    """
    result = _inner(pytester, """
    def test_no_count_available(expect, request):
        expect.wrote(_Cur("CREATE TABLE", -1), 0, "a DDL statement has no row count")
    """)
    expect.outcomes(result, "a -1 is refused rather than compared", passed=0, failed=1)
    expect.refusal(result, "and the refusal says the count is not one",
                   r"no-count-available")


def test_expect_wrote_still_compares(expect, pytester):
    """The acknowledgement is not a waiver: a wrong count is still a failure."""
    result = _inner(pytester, """
    def test_wrote_the_wrong_number(expect, request):
        cur = _Cur("INSERT 0 3", 3)
        pgc_vacuity.note_write(request.node.nodeid, cur)
        expect.wrote(cur, 5, "the fixture claims five rows")
    """)
    expect.outcomes(result, "acknowledging a count does not excuse a wrong one",
                    passed=0, failed=1)
    # THE PATTERN IS NOT DECORATION. With only `failed=1` this arm passed before
    # `expect.wrote` existed at all, satisfied by the AttributeError from the
    # missing function -- a green for a reason that had nothing to do with the
    # comparison. Naming the numbers is what makes the red the right red.
    expect.refusal(result, "and the refusal names both counts", r"got 3 want 5")


def test_several_writes_and_only_the_empty_one_is_named(expect, pytester):
    """The refusal must name WHICH write wrote nothing, not that one did.

    A fixture that runs four INSERTs and gets nothing from the third is the real
    shape. A message saying only "a write wrote no rows" sends the reader to the
    wrong statement.
    """
    result = _inner(pytester, """
    def test_three_writes_one_empty(expect, request):
        n = request.node.nodeid
        pgc_vacuity.note_write(n, _Cur("INSERT 0 5", 5))
        pgc_vacuity.note_write(n, _Cur("UPDATE 0", 0))
        pgc_vacuity.note_write(n, _Cur("INSERT 0 7", 7))
        expect.num(1, 1, "an assertion")
    """)
    expect.outcomes(result, "the empty write fails the test", passed=0, failed=1)
    expect.refusal(result, "and the UPDATE is the one named", r"UPDATE")
    # "NOT THE INSERTs" NEEDS ITS OWN READING. The pattern above matches a refusal that
    # names every write too, and one that did passed this file (#1339). Read over the
    # joined output, because pytest may wrap "#1 INSERT" across two lines.
    joined = " ".join(result.outlines)
    expect.num(len(re.findall(r"#[13]\s+INSERT", joined)), 0,
               "and neither INSERT is named")
    expect.at_least(len(re.findall(r"#2\s+UPDATE", joined)), 1,
                    "and the UPDATE is named by its position, #2")
    expect.refusal(result, "and the mode is named", r"insert-wrote-no-rows")

# ---- two holes I found by attacking my own guard ------------------------------


def test_wrote_refuses_a_statement_that_is_not_a_write(expect, pytester):
    """`expect.wrote` must not accept a SELECT.

    A SELECT that matched nothing reports `SELECT 0` with `rowcount == 0`, so
    `expect.wrote(cur, 0, ...)` compared 0 with 0 and passed -- asserting "this
    write wrote no rows" about a statement that is not a write at all. It reads as
    a deliberate zero and pins nothing, which is this document's whole subject in
    the assertion meant to close it.
    """
    result = _inner(pytester, """
    def test_wrote_about_a_select(expect, request):
        expect.wrote(_Cur("SELECT 0", 0), 0, "a select is not a write")
    """)
    expect.outcomes(result, "wrote() refuses a non-write statement", passed=0, failed=1)
    expect.refusal(result, "and the refusal names the reason", r"not-a-write")


def test_the_acknowledgement_names_one_write_and_not_its_twin(expect, pytester):
    """Acknowledging one zero must not acknowledge a DIFFERENT identical zero.

    Two writes can carry the same tag and the same count: one accidental, one
    deliberate. Matching on (tag, count) alone acknowledged whichever came first,
    so naming the deliberate one could mark the accidental one as named. The test
    still failed -- one unacknowledged write remained -- but the refusal named the
    wrong statement, which is the same defect as a wrong line number in a
    traceback.

    The write is now carried on the cursor that ran it, so an acknowledgement is
    about that statement rather than about a pair of numbers.
    """
    # THREE WRITES, AND THE ORDINAL IS WHAT DISCRIMINATES. With two writes both
    # spellings of this guard print "#1" and the arm passes either way -- it did,
    # before I changed the shape. The accidental write is the SECOND of three here,
    # so value-matching (which acknowledges the accidental one and leaves the
    # deliberate one, then numbers the survivors from 1) prints "#1", and
    # identity-matching with an absolute ordinal prints "#2". Only one of those is
    # the statement a reader has to go and look at.
    result = _inner(pytester, """
    def test_two_identical_zeros(expect, request):
        n = request.node.nodeid
        fine = _Cur("INSERT 0 5", 5)
        accidental = _Cur("INSERT 0 0", 0)
        deliberate = _Cur("INSERT 0 0", 0)
        for c in (fine, accidental, deliberate):
            pgc_vacuity.note_write(n, c)
        expect.wrote(deliberate, 0, "the third one is deliberate")
    """)
    expect.outcomes(result, "the accidental zero is still unacknowledged",
                    passed=0, failed=1)
    expect.refusal(result, "and it is named by its position among ALL the writes",
                   r"#2")


def test_acknowledging_both_identical_zeros_passes(expect, pytester):
    """The control for the arm above: naming both is legitimate and must pass.

    Without this, the arm above is satisfied by a guard that can never be
    satisfied, which is a refusal that has stopped being a test.
    """
    result = _inner(pytester, """
    def test_two_named_zeros(expect, request):
        n = request.node.nodeid
        first = _Cur("DELETE 0", 0)
        second = _Cur("DELETE 0", 0)
        pgc_vacuity.note_write(n, first)
        pgc_vacuity.note_write(n, second)
        expect.wrote(first, 0, "the first delete matched nothing")
        expect.wrote(second, 0, "and so did the second")
    """)
    expect.outcomes(result, "naming both zeros passes", passed=1, failed=0)


class _Conn:
    """A connection's one setting this guard reads, and the cursor it hands out."""

    def __init__(self):
        self.autocommit = True

    def cursor(self):
        return _Cur("SELECT 0", 0)


def _try_set(obj, attr, value):
    """Set `attr` on `obj` -> 'set', or 'refused: <message>'."""
    try:
        setattr(obj, attr, value)
    except AttributeError as exc:
        return f"refused: {exc}"
    return "set"


def test_a_setting_on_the_watched_connection_is_refused_not_dropped(expect, request):
    """Setting an attribute on the wrapper must not pass for setting it on the connection.

    The wrapper forwards reads, so `pgc_conn.autocommit` reads the real value. With
    no `__setattr__`, `pgc_conn.autocommit = False` made a new attribute on the
    wrapper and left the real connection in autocommit, so each statement committed
    and `rollback()` undid nothing (#1420). Refusing it makes the mistake loud.
    """
    real = _Conn()
    watched = pgc_vacuity.watch_writes(real, request.node.nodeid + "::conn")
    expect.text(str(watched.autocommit), "True",
                "premise: a setting read through the watched connection is the real one")
    outcome = _try_set(watched, "autocommit", False)
    expect.text(outcome.split(":")[0], "refused",
                "setting autocommit on the watched connection is refused, not kept on the wrapper")
    expect.text("yes" if "autocommit" in outcome and "BEGIN" in outcome else outcome, "yes",
                "and the refusal names the attribute and what to do instead")
    expect.text(str(real.autocommit), "True", "and the real connection is unchanged")


def test_a_setting_on_a_watched_cursor_is_refused_not_dropped(expect, request):
    """The same for the cursor proxy: `cur.row_factory = ...` would be dropped too."""
    watched = pgc_vacuity.watch_writes(_Conn(), request.node.nodeid + "::cur").cursor()
    expect.text(type(watched).__name__, "_WatchedCursor",
                "premise: the connection hands out a watched cursor")
    outcome = _try_set(watched, "row_factory", None)
    expect.text(outcome.split(":")[0], "refused",
                "setting an attribute on a watched cursor is refused, not kept on the wrapper")
    expect.text("yes" if "row_factory" in outcome else outcome, "yes",
                "and the refusal names the attribute")
