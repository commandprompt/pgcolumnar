#!/usr/bin/env python3
"""Compare a bash suite and its pytest port PROPERTY BY PROPERTY, by name.

Counting is the wrong instrument. The bash suite has 8 checks and the port has 7
tests, and that difference is legitimate: one pytest test carries two of the bash
assertions. A count comparison calls that a defect. A name comparison does not,
and it catches the thing that matters, which is a property asserted in one
harness and nowhere in the other.

The port makes this possible by passing each assertion the SAME name string the
bash check uses. That is a convention the port must keep, so this script is also
what enforces it.

THE PYTHON SIDE IS PARSED, NOT MATCHED (#432, #897)
---------------------------------------------------

This read the pytest file with a regex and got the wrong argument:

    expect.sqlstate(err, "42501", "a role with no privilege is refused")
                         ^^^^^^^ reported as the assertion's name

`[^)]*?"([^"]+)"` is lazy, so it stops at the FIRST quoted argument. For
`expect.num(got, 1, "name")` that happens to be the name and the tool looked
correct. For any helper whose WANT is itself a string it is not:

    expect.sqlstate(err, "42501", NAME)   -> "42501"
    expect.text(got, "none", NAME)        -> "none"

So every SQLSTATE assertion was read as the literal `42501`, counted as an
"extra" name the bash suite does not have, and the real property was reported
MISSING. That is a false red aimed squarely at the ports #432 exists to produce,
which are the ones replacing a `grep` on an error message with a SQLSTATE. The
tool got blinder as the work it grades got better.

It is parsed with `ast` now, and the name is the LAST string argument of the
call, which is the convention every port already follows.

INTERPOLATED NAMES ARE MATCHED AS TEMPLATES
-------------------------------------------

Both harnesses build some names at runtime, bash as `non-owner refused: ${1%%(*}`
and pytest as an f-string. Neither can be expanded without running the suite, so
this compares the SHAPE: every interpolation on both sides becomes `{}`, and two
names match when their templates do.

That is a weaker claim than a literal match and it is reported separately rather
than folded in, because a template match says the two harnesses assert a property
of the same shape, not that they assert it over the same values. A port should
still prefer literal names.

Exit status is 1 when a bash property has no counterpart of either kind.
"""
import ast
import re
import sys


def _parametrized_names(tree):
    """-> every name supplied by a `@pytest.mark.parametrize` that declares one.

    A port of a suite whose bash half repeats a property per function writes the arm
    once and parametrises it, carrying the bash name as a parameter:

        @pytest.mark.parametrize("func,name", USAGE_ONLY)
        def test_a_role_with_only_schema_usage_is_refused(..., func, name):
            expect.sqlstate(err, "42501", name)

    The name reaching `expect` is then a variable, and reading only the call site
    reports every such property MISSING -- which would push a port AWAY from the one
    idiom that keeps the two harnesses one-to-one across a repeated property.

    Only the column actually called `name` is read, resolved through module-level
    constants, so the other parameters of the same decorator contribute nothing.
    """
    consts = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and \
                isinstance(node.targets[0], ast.Name):
            try:
                consts[node.targets[0].id] = ast.literal_eval(node.value)
            except (ValueError, TypeError, SyntaxError):
                pass

    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "parametrize" and len(node.args) >= 2):
            continue
        argnames = _as_names(node.args[0])
        if not argnames:
            continue
        cols = [c.strip() for c in argnames[0].split(",")]
        if "name" not in cols:
            continue
        idx = cols.index("name")

        values = node.args[1]
        if isinstance(values, ast.Name):
            rows = consts.get(values.id)
        else:
            try:
                rows = ast.literal_eval(values)
            except (ValueError, TypeError, SyntaxError):
                rows = None
        if rows is None:
            continue
        for row in rows:
            if len(cols) == 1:
                cell = row
            elif isinstance(row, (tuple, list)) and len(row) > idx:
                cell = row[idx]
            else:
                continue
            if isinstance(cell, str):
                out.append(cell)
    return out


def _py_names(src):
    """Every assertion name in the port, by parsing rather than matching.

    The name is the LAST argument of an `expect.<helper>(...)` call, or the value of
    a `name=` keyword, read through `_as_names` so a conditional carries both of
    its arms.
    """
    tree = ast.parse(src)
    out = _parametrized_names(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_expect = (isinstance(func, ast.Attribute)
                     and isinstance(func.value, ast.Name)
                     and func.value.id == "expect")
        for kw in node.keywords:
            if kw.arg == "name":
                out.extend(_as_names(kw.value))
        if not is_expect or not node.args:
            continue
        out.extend(_as_names(node.args[-1]))
    return out


def _as_names(node):
    """-> every string this node can evaluate to; [] when it states none.

    A LIST rather than one string, because `"a" if cond else "b"` is a name argument
    that carries two properties depending on the arm, and both are asserted by the
    suite. Reading only one of them reported the other MISSING, which is the same
    false red as reading the wrong argument, one level in.

    An f-string becomes a `{}` template. A node that is neither contributes nothing:
    reporting a name as absent is better than reporting the wrong string as present.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            else:
                parts.append("{}")
        return ["".join(parts)]
    if isinstance(node, ast.IfExp):
        return _as_names(node.body) + _as_names(node.orelse)
    return []


# `${...}`, `$(...)`, `$NAME`, and the POSITIONAL parameters. `$1` is how a bash
# helper names the thing it was called about, so it is the commonest interpolation
# in a check name and the first version of this missed every one of them.
_BASH_INTERP = re.compile(
    r'\$\{[^}]*\}|\$\([^)]*\)|\$[A-Za-z_][A-Za-z0-9_]*|\$[0-9]+|\$[@*#?]')


def _template(name):
    """-> the name with every interpolation reduced to `{}`.

    Applied to both sides, so `non-owner refused: ${1%%(*}` and the f-string
    `f"non-owner refused: {fn}"` land on the same string.
    """
    return re.sub(r"\{[^{}]*\}", "{}", _BASH_INTERP.sub("{}", name))


def main(bash_file, py_file):
    """-> the exit status: 1 when a bash property has no counterpart."""
    bash_names = re.findall(
        r'\bcheck(?:_num|_ratio|_text|_timing)?\s+"([^"]+)"', open(bash_file).read())
    py_names = _py_names(open(py_file).read())

    bset, pset = set(bash_names), set(py_names)

    print(f"bash checks: {len(bash_names)} ({len(bset)} distinct)")
    print(f"pytest named assertions: {len(py_names)} ({len(pset)} distinct)")
    print()

    literal = bset & pset
    # Only names with no literal partner are considered as templates, so a template
    # match can never hide a literal one or be double-counted.
    b_left, p_left = bset - literal, pset - literal
    p_templates = {_template(n) for n in p_left}
    templated = {n for n in b_left if _template(n) in p_templates}

    missing = sorted(b_left - templated)
    extra = sorted(n for n in p_left if _template(n) not in {_template(m) for m in templated})

    print("PROPERTIES IN THE BASH SUITE AND NOT IN THE PORT:")
    if missing:
        for n in missing:
            print(f"  MISSING  {n}")
    else:
        print("  none -- every bash property is asserted by name in the port")
    print()
    if templated:
        print("MATCHED BY TEMPLATE ONLY (both sides build the name at runtime):")
        for n in sorted(templated):
            print(f"  shape    {n}")
        print()
    print("ASSERTIONS IN THE PORT AND NOT IN THE BASH SUITE:")
    if extra:
        for n in extra:
            print(f"  extra    {n}")
    else:
        print("  none")
    print()
    print(f"literal matches: {len(literal)} | template matches: {len(templated)} | "
          f"missing: {len(missing)}")
    print("VERDICT:", "PORT IS INCOMPLETE" if missing else "every bash property is covered")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
