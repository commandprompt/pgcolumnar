"""A loop that never runs asserts nothing, and the run still reports a pass.

WHAT THIS CLOSES, AND WHAT IT DOES NOT. `assert-inside-a-loop-over-zero-rows` in
VACUITY_MODES.md 3.5 is TWO shapes, and the layer already refuses one of them.

**Already refused.** If a loop's body holds the test's ONLY counted assertions, a
zero-trip loop leaves the count at 0 and `pytest_runtest_call` raises `VacuityError`.
Measured, on a planted test rather than by reading the hook:

    only assertion inside a zero-trip loop   VacuityError: made no counted assertion
    the same loop with one row               1 passed

So that half needs nothing, and this file does not pretend to add it.

**Still open, and what this file is for.** When the test ALSO asserts outside the
loop, the count is non-zero, the test passes, and the loop's assertions simply never
ran. Nothing notices. That is the shape a query returning no rows produces, and the
one a glob matching nothing produces.

THE POPULATION, measured over the whole corpus before this was written:

    non-empty by construction (literal, range, local literal)   20  cannot be zero-trip
    derived, loop holds the only assertions                      0  already refused
    derived, WITH assertions outside the loop                    2  AT RISK

Both at-risk loops already carry a premise, so this arm goes green on arrival. That is
the point rather than a weakness: the property is true today and nothing was holding it
there, so the next loop added without a premise is what this catches. It is not
insurance against an imagined shape -- it has a population of two and locks it in.

WHY THE RULE IS LOOSER THAN THE PROPERTY, said plainly. The honest requirement is "a
premise bounding the cardinality of THIS iterable". What is enforced is "a counted
assertion outside the loop that takes `len(...)` of something". The two differ, and the
reason is dataflow: `test_harness_deps.py`'s loop iterates `sorted(found)` while its
premise bounds `len(files)` -- `found` is built FROM `files` inside the loop that
precedes it. Matching premise to iterable through that needs more than a name
comparison, and a rule that demanded the names match would reject the correct code.
So this checks that a cardinality premise EXISTS, and a reviewer checks that it is the
right one.
"""

import ast
import pathlib

HERE = pathlib.Path(__file__).resolve().parent


def _counted(node):
    """An `expect.<assertion>(...)` call. `cannot_run` is excluded: it declares a test
    unrunnable rather than concluding anything, so it is not a conclusion to count."""
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "expect"
            and node.func.attr != "cannot_run")


def _nonempty_by_construction(fn, iterable):
    """True when the iterable cannot be empty, so a premise would assert nothing.

    A literal with elements, a `range(...)`, or a name bound in this function to a
    non-empty literal -- including through `.items()`, which is how the corpus spells
    a table of cases. Twenty of the corpus's twenty-two such loops are this.
    """
    if isinstance(iterable, (ast.List, ast.Tuple, ast.Set)) and iterable.elts:
        return True
    if (isinstance(iterable, ast.Call) and isinstance(iterable.func, ast.Name)
            and iterable.func.id == "range"):
        return True
    target = None
    if (isinstance(iterable, ast.Call) and isinstance(iterable.func, ast.Attribute)
            and iterable.func.attr in ("items", "keys", "values")):
        target = iterable.func.value
    elif isinstance(iterable, ast.Name):
        target = iterable
    if isinstance(target, ast.Name):
        # EVERY binding of the name must be a non-empty literal, not just one (#1339):
        # `rows = [0]` then `rows = query()` loops over the query.
        stores = [n for n in ast.walk(fn) if isinstance(n, ast.Name)
                  and n.id == target.id and isinstance(n.ctx, ast.Store)]
        literal = [n for n in ast.walk(fn) if isinstance(n, ast.Assign)
                   and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                   and n.targets[0].id == target.id and _nonempty_literal(n.value)]
        return bool(stores) and len(stores) == len(literal)
    return False


def _nonempty_literal(value):
    if isinstance(value, ast.Dict):
        return bool(value.keys)
    return isinstance(value, (ast.List, ast.Tuple, ast.Set)) and bool(value.elts)


def _has_cardinality_premise(fn, loop):
    """A counted assertion OUTSIDE the loop whose arguments take `len(...)`, and that
    does not pin the length at 0 or below: that bounds the iterable from the wrong
    side, saying the loop runs no times (#1339)."""
    inside = {id(n) for n in ast.walk(loop)}
    for node in ast.walk(fn):
        if id(node) in inside or not _counted(node):
            continue
        if (node.func.attr in ("num", "at_most") and len(node.args) > 1
                and isinstance(node.args[1], ast.Constant) and node.args[1].value == 0):
            continue
        for arg in node.args:
            for sub in ast.walk(arg):
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                        and sub.func.id == "len"):
                    return True
    return False


def classify_loops(directory):
    """-> [(file, line, test, kind)] for every loop holding a counted assertion, with
    the kind the sweep gave it: "bounded" (non-empty by construction), "holds-all"
    (refused at runtime when zero-trip), "premised" or "unpremised". The sweep and its
    coverage premise both read THIS, so the premise measures what the sweep saw."""
    out = []
    for path in sorted(pathlib.Path(directory).glob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                   and n.name.startswith("test_")]:
            counted_in_fn = [c for c in ast.walk(fn) if _counted(c)]
            for loop in [n for n in ast.walk(fn)
                         if isinstance(n, (ast.For, ast.AsyncFor))]:
                inner = [c for c in ast.walk(loop) if _counted(c)]
                if not inner:
                    continue
                if _nonempty_by_construction(fn, loop.iter):
                    kind = "bounded"
                # The loop holding every assertion is already refused at runtime.
                elif len(counted_in_fn) == len(inner):
                    kind = "holds-all"
                elif _has_cardinality_premise(fn, loop):
                    kind = "premised"
                else:
                    kind = "unpremised"
                out.append((path.name, loop.lineno, fn.name, kind))
    return out


def unpremised_loops(directory):
    """-> ["file:line in test_name", ...] for every at-risk loop with no premise."""
    return [f"{f}:{line} in {name}" for f, line, name, kind in classify_loops(directory)
            if kind == "unpremised"]


def test_every_at_risk_loop_carries_a_coverage_premise(expect):
    """The corpus itself. Green on arrival, and that is the point."""
    offenders = unpremised_loops(HERE)
    expect.text(", ".join(offenders) or "none", "none",
                "every derived loop carrying an assertion has a cardinality premise")


def test_the_sweep_finds_the_loops_it_is_meant_to_police(expect):
    """The coverage premise this file's own sweep needs.

    A sweep that parses nothing reports no offenders, which is exactly what a clean
    corpus reports. So count the loops IT classified, not loops this test finds by a
    walk of its own (#1339): the first version re-walked the files itself, so a sweep
    reading no file at all still had a green premise beside a green verdict.
    """
    kinds = [k for *_, k in classify_loops(HERE)]
    expect.at_least(len(kinds), 15,
                    "premise: the sweep found loops carrying assertions to classify")
    # and it found the shape it exists for: derived loops with assertions outside
    expect.at_least(kinds.count("premised") + kinds.count("unpremised"), 2,
                    "and among them the at-risk loops it polices")


def test_a_loop_with_no_premise_is_caught(tmp_path, expect):
    """The removal proof, with the control beside it.

    The fixture is the real shape with one property removed -- the premise -- rather
    than an empty file, because an empty file is caught by a sweep that does nothing.
    """
    bad = tmp_path / "test_bad.py"
    bad.write_text(
        "def test_x(expect):\n"
        "    rows = query()\n"
        "    expect.num(1, 1, 'something outside the loop')\n"
        "    for r in rows:\n"
        "        expect.num(r, 1, 'never runs when rows is empty')\n",
        encoding="utf-8")
    expect.text(", ".join(unpremised_loops(tmp_path)), "test_bad.py:4 in test_x",
                "a derived loop with no cardinality premise is named")

    good = tmp_path / "test_good.py"
    good.write_text(
        "def test_y(expect):\n"
        "    rows = query()\n"
        "    expect.at_least(len(rows), 1, 'premise: the query returned rows')\n"
        "    for r in rows:\n"
        "        expect.num(r, 1, 'runs at least once')\n",
        encoding="utf-8")
    bad.unlink()
    expect.text(", ".join(unpremised_loops(tmp_path)) or "none", "none",
                "control: the same loop with a premise is clean")


def test_a_premise_in_name_only_is_still_caught(tmp_path, expect):
    """Two shapes the sweep used to wave through (#1339).

    A literal bound first and a query bound over it later is not non-empty by
    construction: the loop iterates the query. And a premise that the length is 0
    bounds the iterable from the wrong side: it says the loop runs no times.
    """
    shapes = {
        "rebound": ("    rows = [0]\n    rows = query()\n",
                    "expect.num(1, 1, 'outside')"),
        "zero": ("    rows = query()\n",
                 "expect.num(len(rows), 0, 'the length is zero')"),
    }
    for name, (bind, outside) in shapes.items():
        f = tmp_path / f"test_{name}.py"
        f.write_text(
            f"def test_{name}(expect):\n{bind}"
            f"    {outside}\n"
            "    for r in rows:\n"
            "        expect.num(r, 1, 'never runs when rows is empty')\n",
            encoding="utf-8")
        expect.text(", ".join(unpremised_loops(tmp_path)) or "none",
                    f"test_{name}.py:{3 + bind.count(chr(10))} in test_{name}",
                    f"a loop with a {name} premise is named")
        f.unlink()


def test_a_bounded_loop_needs_no_premise(tmp_path, expect):
    """A literal cannot be empty, so demanding a premise would be noise.

    Three spellings, because the corpus uses all three and a rule that rejected any of
    them would be edited away rather than obeyed.
    """
    for name, iterable in (("lit", "[1, 2]"), ("rng", "range(3)"),
                           ("dct", "cases.items()")):
        f = tmp_path / f"test_{name}.py"
        pre = "    cases = {'a': 1}\n" if name == "dct" else ""
        f.write_text(
            f"def test_{name}(expect):\n{pre}"
            "    expect.num(1, 1, 'outside')\n"
            f"    for r in {iterable}:\n"
            "        expect.num(1, 1, 'inside')\n",
            encoding="utf-8")
        expect.text(", ".join(unpremised_loops(tmp_path)) or "none", "none",
                    f"a loop over {name} is non-empty by construction")
        f.unlink()


def test_the_layer_already_refuses_a_loop_holding_every_assertion(pytester, expect):
    """The measured half, so this file does not claim the whole mode.

    `pytest_runtest_call` fails a test whose counted-assertion count is 0, which is
    what a zero-trip loop leaves behind when it holds them all. Both halves run the
    behaviour: a planted test in a child pytest, and the sweep on a planted file.
    """
    # BEHAVIOUR, NOT SOURCE (#1339): this read pytest_runtest_call's text for
    # "count == 0", which a disabled branch keeps. A planted test whose only assertion
    # sits in a zero-trip loop must be refused by the layer itself.
    pytester.makepyfile(
        "def test_x(expect):\n"
        "    for r in []:\n"
        "        expect.num(r, 1, 'never runs')\n")
    result = pytester.runpytest("-p", "pgc_vacuity")
    out = result.stdout.str()
    expect.text("refused" if result.ret != 0 and "made no counted assertion" in out
                else f"not refused (rc={result.ret}): {out.strip().splitlines()[-1:]!r}",
                "refused", "the layer fails a test that counted nothing")
    planted = pytester.path / "planted"
    planted.mkdir()
    (planted / "test_p.py").write_text(
        "def test_x(expect, rows):\n"
        "    for r in rows:\n"
        "        expect.num(r, 1, 'every assertion is here')\n")
    expect.text(repr([k for *_, k in classify_loops(planted)]), "['holds-all']",
                "and this sweep skips that shape rather than double-reporting it")
