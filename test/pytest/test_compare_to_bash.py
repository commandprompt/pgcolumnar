"""`compare_to_bash.py` must read the assertion's NAME, not some other argument (#432).

The parity tool is what decides whether a port is one-for-one with its bash suite, which
is #432's definition of done. So the tool is a claim like any other, and it was wrong in a
way that pointed directly at the work it grades.

THE DEFECT. The python side was matched with a regex:

    expect\\.\\w+\\([^)]*?"([^"]+)"\\s*(?:,[^)]*)?\\)

`[^)]*?` is lazy, so it stopped at the FIRST quoted argument. For `expect.num(got, 1,
NAME)` that is the name, and the tool looked correct on every arm anyone checked. For a
helper whose WANT is itself a string it is not:

    expect.sqlstate(err, "42501", NAME)   -> read "42501" as the name
    expect.text(got, "none", NAME)        -> read "none"

Every SQLSTATE assertion was therefore read as the literal `42501`, reported as an "extra"
name the bash suite does not have, while the real property was reported MISSING. #432's
ports are precisely the ones replacing a grep on an error message with a SQLSTATE
assertion, so the tool went blind in proportion to the work being done well. Measured over
the seven pairs in the tree: **61 bash properties reported missing, of which 34 were not
missing at all.** Two whole pairs flipped from `PORT IS INCOMPLETE` to complete.

WHY A GUARD AND NOT JUST A FIX. Nothing could see this. The tool's own output was the only
evidence either way, and its verdict for a correct port was a plausible-looking list of
names that really were absent from the port -- absent because the tool had matched a
different string, which is not visible from the list. `test_hilbert_locality.py` records
somebody working around it by rewriting their test file until the count fell, and
concluding the rest needed a change to this tool. It did.

THE ARMS BELOW DRIVE THE REAL EXTRACTORS, never a copy. A python twin of a python rule
would agree with itself.
"""

import ast
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from compare_to_bash import _as_names, _parametrized_names, _py_names, _template  # noqa: E402


def _names(src):
    return _py_names(src)


def test_the_name_is_the_last_argument_not_the_first_string(expect):
    """THE REGRESSION. Three helpers, one of which always worked.

    `expect.num` is the control: its want is a number, so the old regex happened to reach
    the name and the tool looked correct. Without that arm this test would pass over a
    rule that returns the last argument of nothing at all.
    """
    src = (
        'def t(expect):\n'
        '    expect.sqlstate(err, "42501", "a role with no privilege is refused")\n'
        '    expect.text(got, "none", "no key is stated twice")\n'
        '    expect.num(got, 1, "the owner reads its own table")\n'
    )
    got = _names(src)
    expect.text(", ".join(sorted(got)),
                "a role with no privilege is refused, no key is stated twice, "
                "the owner reads its own table",
                "each helper contributes its NAME and not its want")
    expect.num(len(got), 3, "three assertions, three names")
    expect.num(sum(1 for n in got if n in ("42501", "none")), 0,
               "and no want is mistaken for a name, which is the defect this closes")


def test_a_call_whose_name_is_not_a_literal_contributes_nothing(expect):
    """Better absent than wrong.

    A name the tool cannot read must be reported MISSING, which a person then fixes.
    Guessing at it reports the wrong string as PRESENT, and a false green on a parity tool
    is how a property ends up asserted in neither harness.
    """
    src = 'def t(expect):\n    expect.sqlstate(err, "42501", some_variable)\n'
    expect.num(len(_names(src)), 0,
               "an unreadable name yields nothing rather than the want beside it")


def test_an_fstring_name_becomes_a_template(expect):
    """Both harnesses build some names at runtime. The shape is what can be compared."""
    src = 'def t(expect):\n    expect.num(got, 1, f"premise: {r} can open a session")\n'
    expect.text(_names(src)[0], "premise: {} can open a session",
                "the interpolated part is reduced to a placeholder")


def test_a_conditional_name_carries_both_of_its_arms(expect):
    """`"a" if cond else "b"` asserts two properties depending on the arm taken.

    Reading one of them reports the other MISSING, which is the same false red as reading
    the wrong argument, one level in.
    """
    src = ('def t(expect):\n'
           '    expect.num(got, 1, "the owner reads" if f == "read" else "the owner writes")\n')
    expect.text(", ".join(sorted(_names(src))), "the owner reads, the owner writes",
                "both arms of a conditional name are collected")


def test_a_parametrized_name_is_resolved_from_the_decorator(expect):
    """The idiom a repeated bash property should be ported to.

    When the bash suite states the same property once per function, the port writes the arm
    once and parametrises it, carrying the bash name as a parameter. If the tool cannot see
    those names it reports every one of them MISSING, which pushes a port away from the one
    idiom that keeps the two harnesses one-to-one.

    The control is the second decorator: a parametrize with no `name` column must
    contribute nothing, or the tool would harvest every parameter in the file as an
    assertion name and report a pile of extras.
    """
    src = (
        'USAGE_ONLY = (\n'
        '    ("read_projection", "a role with only schema USAGE is refused"),\n'
        '    ("reconstruct_via_projection", "and is refused reconstruct"),\n'
        ')\n'
        '@pytest.mark.parametrize("func,name", USAGE_ONLY)\n'
        'def t(expect, func, name):\n'
        '    expect.sqlstate(err, "42501", name)\n'
        '@pytest.mark.parametrize("func", ["read_projection", "reconstruct"])\n'
        'def u(expect, func):\n'
        '    expect.num(got, 1, "an unrelated property")\n'
    )
    got = _names(src)
    expect.num(int("a role with only schema USAGE is refused" in got), 1,
               "a parametrized name is resolved through the module-level constant")
    expect.num(int("and is refused reconstruct" in got), 1, "for every row of it")
    expect.num(int("read_projection" in got), 0,
               "while the OTHER column of the same decorator is not a name")
    expect.num(int("reconstruct" in got), 0,
               "and a parametrize with no name column contributes nothing")


def test_the_parametrize_reader_takes_the_column_called_name(expect):
    """Position is not the rule; the declared column is.

    A port that writes `parametrize("name,func", ...)` states the same properties, and a
    reader keyed on position silently harvests the function names instead.
    """
    tree = ast.parse(
        'ROWS = (("the property", "read_projection"),)\n'
        '@pytest.mark.parametrize("name,func", ROWS)\n'
        'def t(name, func):\n    pass\n'
    )
    expect.text(", ".join(_parametrized_names(tree)), "the property",
                "the name column is found by its declared name, whatever its position")


def test_the_two_harnesses_interpolations_land_on_one_template(expect):
    """What makes a template match mean anything: bash and python spell it differently."""
    expect.text(_template("non-owner refused: ${1%%(*}"), "non-owner refused: {}",
                "a bash parameter expansion is reduced to a placeholder")
    expect.text(_template("non-owner refused: {}"), "non-owner refused: {}",
                "and an f-string template is already in that form, so the two meet")
    expect.text(_template("premise: $PGC_PORT is open"), "premise: {} is open",
                "a bare variable reference too")


def test_the_ported_suites_in_this_tree_are_graded_one_for_one(expect):
    """THE STANDING ARM, and the reason this file is not only about fixtures.

    A guard over invented sources proves the extractor reads python. It cannot prove the
    tool grades THIS tree, which is the claim #432 rests on. So the pairs that are declared
    complete are asserted complete here, and a later edit that breaks parity fails with the
    pair named rather than the whole gate going red for an unrelated reason.

    Only the pairs that reach zero today are listed. A pair with a real gap is not pinned to
    its gap: that would turn the gap into the expected state.
    """
    from compare_to_bash import main
    import contextlib
    import io

    root = HERE.parent.parent
    # EVERY pair in the tree. When a new port lands it belongs here, and when one
    # cannot reach zero the reason belongs in its own file rather than in an omission
    # from this list.
    complete = ["differential", "hilbert_locality", "native_ownership",
                "native_projection", "projection_privilege", "stats_privilege",
                "zonemap_boundaries"]
    verdicts = {}
    for stem in complete:
        sh, py = root / "test" / f"{stem}.sh", HERE / f"test_{stem}.py"
        expect.num(int(sh.exists() and py.exists()), 1, f"premise: both halves of {stem} exist")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(str(sh), str(py))
        verdicts[stem] = rc
    expect.text(", ".join(f"{k}={v}" for k, v in sorted(verdicts.items())),
                ", ".join(f"{k}=0" for k in sorted(complete)),
                "every pair declared one-for-one still grades one-for-one")
