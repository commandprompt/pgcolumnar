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


# WHERE THE NAME SITS, for the helpers where it is not the last argument (#1036).
#
# The rule for most of `Expect` is "the name is the last argument", and for 14 of its 18
# helpers that is true. It is not a property of the helpers, though, only of most of them,
# and the four below were read wrong in silence: the last argument is a real string in each
# case, so a wrong name looked exactly like a right one.
#
#   refusal(result, name, *patterns)      the last argument is a PATTERN
#   cannot_run(reason, detail="", *, name=)  the name is the KEYWORD or nothing (#1131)
#   plan_marker(plan, key, name=None)     the last argument is a plan KEY
#   plan_node(plan, ..., name=None)       the last argument describes the NODE
#
# A value here is the index of the call argument carrying the name; `None` means no
# positional argument carries it and only a `name=` keyword can. `-1`, the default for
# every helper not listed, means the last one.
#
# This is a hand-written derived value, so it is pinned: the drift guard in
# `test_compare_to_bash.py` re-derives every entry from the real signatures in
# `pgc_vacuity.py` and fails with the helper named when the two disagree. Add a helper
# whose name is not last and that arm goes red before this table is wrong in the field.
_NAME_ARG = {
    "refusal": 1,
    # #1131: `cannot_run` states a property only when it is GIVEN a check name. Before
    # that it recorded under its reason CODE and this table said 0, which harvested the
    # code as though it were a check name -- a name no bash suite has, reported as an
    # extra on every pair that declined to run. `None` means only a `name=` keyword
    # carries it, so a declaration with no name now states nothing, which is the same
    # rule `_names_in` applies when it drops a bare `{}`: a wrong name is worse than an
    # absent one.
    "cannot_run": None,
    "plan_marker": None,
    "plan_node": None,
    # #1215: both take the check name as a required KEYWORD, like cannot_run's.
    "declines": None,
    "require_module": None,
}


def _name_argument(node):
    """-> the AST node holding this `expect.<helper>(...)` call's name, or None.

    Factored out because three places now need the SAME answer -- `_py_names`, the
    loop reader below, and any future one. Two copies of "which argument is the name"
    is the defect `_NAME_ARG` exists to prevent, one level up.
    """
    func = node.func
    if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
            and func.value.id == "expect"):
        return None
    for kw in node.keywords:
        if kw.arg == "name":
            return kw.value
    if not node.args:
        return None
    idx = _NAME_ARG.get(func.attr, -1)
    if idx is None or (idx != -1 and len(node.args) <= idx):
        return None
    return node.args[idx]


def _loop_names(tree):
    """-> every name supplied by a `for` over a LITERAL table (#1045 class 2).

    The loop analogue of `_parametrized_names`, and the same idiom one level down:

        for label, sql in (("allnull column scan",  "SELECT * FROM %T"),
                           ("allnull column count", "SELECT count(allnull) FROM %T")):
            c, h = p.both(sql)
            expect.row_set(c, h, label)

    The name reaching `expect` is a variable, so reading only the call site reports
    every such property MISSING -- 46 names across 15 sites, 37 of them in
    `differential`, whose port is behaviourally complete.

    READ PER ELEMENT, NOT PER ROW. `ast.literal_eval` on the whole table fails when
    any OTHER column holds an f-string, which is exactly `differential`'s
    mismatched-collation table: five literal labels beside one interpolated query.
    Reading the label column element by element with `_as_names` keeps them.

    ONLY A COLUMN ACTUALLY USED AS A NAME. A loop variable that is merely mentioned in
    the body is not a name, and harvesting it would invent properties out of SQL
    strings -- the failure `_as_names` exists to refuse.
    """
    out = []
    for loop in [n for n in ast.walk(tree) if isinstance(n, ast.For)]:
        if not isinstance(loop.iter, (ast.Tuple, ast.List)):
            continue
        target = loop.target
        if isinstance(target, ast.Name):
            columns = {target.id: None}
        elif isinstance(target, ast.Tuple):
            columns = {e.id: i for i, e in enumerate(target.elts)
                       if isinstance(e, ast.Name)}
        else:
            continue

        used = set()
        for stmt in loop.body:
            for node in ast.walk(stmt):
                if not isinstance(node, ast.Call):
                    continue
                arg = _name_argument(node)
                if isinstance(arg, ast.Name) and arg.id in columns:
                    used.add(arg.id)
        for ident in sorted(used):
            index = columns[ident]
            values, readable = [], True
            for row in loop.iter.elts:
                if index is None:
                    cell = row
                elif isinstance(row, (ast.Tuple, ast.List)) and index < len(row.elts):
                    cell = row.elts[index]
                else:
                    readable = False
                    break
                got = _as_names(cell)
                if not got:
                    readable = False
                    break
                values += got
            if readable:
                out += values
    return out


def _literal_members(node, consts):
    """-> the elements of a literal container, or None when it is not one.

    Accepts the container itself, a module constant naming one, and `sorted(...)` of
    either -- which is how a port writes a parametrize source it wants ordered. A
    constant whose VALUE is not a literal container gives None, because
    `ast.literal_eval` has already refused it.
    """
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
            and node.func.id == "sorted" and len(node.args) == 1:
        inner = _literal_members(node.args[0], consts)
        return None if inner is None else sorted(inner)
    if isinstance(node, ast.Name):
        value = consts.get(node.id)
        return list(value) if isinstance(value, (list, tuple, set, dict)) else None
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        out = []
        for element in node.elts:
            try:
                out.append(ast.literal_eval(element))
            except (ValueError, TypeError, SyntaxError):
                return None
        return out
    return None


def _expanded_names(tree):
    """-> a CONCRETE name per member for an f-string parametrised over one column.

    #1045 class 3. A bash suite unrolls a family as literals where its port
    parametrises it, so the two never match:

        bash   diff_query "c_int range" ... "c_text range"        11 literals
        port   @pytest.mark.parametrize("col", sorted(RANGES))
               expect.row_set(c, h, f"{col} range")               1 template

    Resolving `RANGES` to its 11 keys emits 11 concrete names, matched LITERALLY on
    both sides. The alternative -- widening `_template` so a literal matches a
    template -- resolves the same 17 and gives up the ability to ever detect them
    going wrong: `c_bytea range` would match `{} range` whether or not the port
    covers `c_bytea`. Measured: drop `c_bytea` from `RANGES` and expansion reports it
    by name, where widening cannot.

    THESE NAMES ARE ADDITIVE. `_py_names` appends them BESIDE the template rather
    than instead of it, and that is a constraint rather than a convenience. Where BOTH
    sides are templated the template IS the match -- 11 of `hilbert_locality`'s 30
    bash names and 10 of `hilbert_cluster`'s match that way -- so replacing the port's
    template orphans them. Measured: replacing breaks three green pairs and takes
    `differential` to 13 rather than 0. It is also INDEPENDENT of the refusals below:
    those names are matched against bash names that are themselves templated, so the
    orphaning happens whether or not anything else is ever expanded.

    WHAT IT REFUSES, each costing a false MISSING at worst:

    - a module constant in the name. `FLOAT_RTOL = 1e-6` renders as `1e-06`, a
      different spelling from its own source, and the bash side carries no such name.
      A resolvable value is not one that renders the way the other side spells it.
    - two or more DISTINCT parametrised columns. Stacked parametrize is a cartesian
      product, and expanding one while holding the other invents names that exist
      nowhere. Three sites, all in `hilbert_cluster`, which grades clean today and
      stays clean because refusing means not ADDING. Build the product when a pair
      needs it, against a real example.
    - anything that is not a bare `Name`, and any name that is not a parametrised
      column.

    AND IT READS ONLY THE NAME ARGUMENT, through `_name_argument` and nothing else.
    Expanding every f-string in a body instead emits 240 names where this emits 144,
    96 of them SQL -- `SELECT id, c_int FROM %T` as a check name. That form resolves
    the 17 too, so it LOOKS like it works and only its own output says otherwise.
    """
    consts = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            try:
                consts[node.targets[0].id] = ast.literal_eval(node.value)
            except (ValueError, TypeError, SyntaxError):
                pass

    out = []
    for func in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        columns = {}
        for dec in func.decorator_list:
            if not (isinstance(dec, ast.Call)
                    and getattr(dec.func, "attr", "") == "parametrize"
                    and len(dec.args) >= 2):
                continue
            declared = _as_names(dec.args[0])
            if not declared:
                continue
            names = [c.strip() for c in declared[0].split(",")]
            members = _literal_members(dec.args[1], consts)
            # ONE COLUMN ONLY. A multi-column decorator hands each test a ROW, and
            # reading a row as a column's members is the same defect as reading a
            # table per row -- it produces values that were never in that position.
            if members is not None and len(names) == 1:
                columns[names[0]] = members

        if not columns:
            continue
        for node in ast.walk(func):
            if not isinstance(node, ast.Call):
                continue
            name = _name_argument(node)
            if not isinstance(name, ast.JoinedStr):
                continue
            interpolated = [v.value for v in name.values
                            if isinstance(v, ast.FormattedValue)]
            if not interpolated:
                continue
            if not all(isinstance(e, ast.Name) and e.id in columns
                       for e in interpolated):
                continue
            wanted = {e.id for e in interpolated}
            if len(wanted) != 1:
                continue
            column = wanted.pop()
            for member in columns[column]:
                out.append("".join(
                    piece.value if isinstance(piece, ast.Constant) else str(member)
                    for piece in name.values))
    return out


def _py_names(src):
    """Every assertion name in the port, by parsing rather than matching.

    The name is the LAST argument of an `expect.<helper>(...)` call, or the value of
    a `name=` keyword, read through `_as_names` so a conditional carries both of
    its arms. Four helpers put it somewhere else and are read through `_NAME_ARG`.
    """
    tree = ast.parse(src)
    out = _parametrized_names(tree) + _loop_names(tree)
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
        idx = _NAME_ARG.get(func.attr, -1)
        if idx is None:
            continue
        if idx != -1 and len(node.args) <= idx:
            continue
        out.extend(_as_names(node.args[idx]))
    return out


def _matchable_names(src):
    """-> what the port's names can be matched AGAINST, which is more than it asserts.

    `_py_names` answers "what does this port assert", and the report prints that
    number. `_expanded_names` answers a different question: which concrete spellings
    of those same assertions a bash literal could be compared with. One parametrised
    arm asserting `f"{col} range"` over 11 columns is ONE assertion and ELEVEN
    spellings, and conflating them makes the header lie -- measured, `differential`
    would report 274 named assertions where the port has 99.

    So the expansion feeds the MISSING calculation and nothing else. It is not an
    assertion, it is not `extra`, and it is not counted.
    """
    return _py_names(src) + _expanded_names(ast.parse(src))


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


# EVERY CHECK HELPER `lib.sh` DEFINES, and the pattern built from it (#1040).
#
# This read five of the eight. `check_unrunnable`, `check_skip` and
# `check_ratio_needs_quiet_machine` matched no branch, so a bash property asserted
# through any of them was INVISIBLE: never reported MISSING, never able to move `rc`,
# and therefore a pair could grade one-for-one because the grader could not see the
# gap. `hilbert_locality` was exactly that -- two of the four properties its
# unrunnable branch records had no counterpart in the port.
#
# All eight take the check NAME as `$1`, so one pattern serves them all; that is a
# property of these helpers rather than of bash, and the drift guard re-reads it.
#
# `check_ratio` is a prefix of `check_ratio_needs_quiet_machine`, and the OLD pattern
# shape could not read the longer one at all: `check(?:_num|_ratio|_text|_timing)?\s+"`
# matches `check_ratio`, needs whitespace, finds `_needs...`, backtracks to the empty
# option, needs whitespace after `check`, and fails. Measured on a fixture holding both:
# the old form reads ['short'], this one reads ['short', 'long'].
#
# The entries are written longest-first for readability. **That ordering is NOT what
# makes it work** -- Python's `re` backtracks across alternatives, so a pure reorder
# reads both names identically (measured). An arm pins the BEHAVIOUR rather than the
# order, because the order is the thing that looks load-bearing and is not.
#
# WHICH ARGUMENT CARRIES THE NAME, per helper. This is the bash mirror of
# `_NAME_ARG` above, and it exists for the same reason: a helper's name is not
# always its first argument, and a pattern that assumes so reads the wrong string
# rather than no string.
#
# `pgc_skip <capability> <message>` is the case that forced it (#1045). Its check
# name is `$2`; `$1` is a capability, written bare at 68 of its 70 call sites and
# QUOTED at the other two. A single pattern keyed to the first quoted argument
# therefore reads the name almost everywhere and the CAPABILITY at
# `pgc_skip "test_decoding" "..."` -- and a wrong name is worse than an absent one,
# because it can never be matched by a port and is reported MISSING for ever.
#
# `diff_query` and `diff_query_ordered` are the other half: `lib.sh` wrappers that
# forward their `$1` into `check`. The NAME is in the suite, only the RECORDER is
# in `lib.sh`, and the extractor read neither. 225 names across 59 suites were
# invisible, 80 of them in `differential`, which graded one-for-one on 6 of its 86.
#
# Hand-written so the tool stays standalone, and pinned like `_NAME_ARG`: the drift
# guard in `test_compare_to_bash.py` DERIVES this table from `lib.sh` -- membership
# and position both -- and fails with the helper named when the two disagree.
# The primitive every recorder reaches, and the argument IT names its check in. The
# seed of the closure below; named rather than inlined so an arm can assert that no
# suite calls it directly.
_RECORD_PRIMITIVE = "pgc_record"
_RECORD_NAME_ARG = 2

_BASH_NAME_ARG = {
    "check_ratio_needs_quiet_machine": 1,
    "check_unrunnable": 1,
    "diff_query_ordered": 1,
    "diff_query": 1,
    "check_timing": 1,
    "check_ratio": 1,
    "check_text": 1,
    "check_skip": 1,
    "check_num": 1,
    "check": 1,
    "pgc_pass": 1,
    "pgc_fail": 1,
    "pgc_skip": 2,
}

# The membership view of the table, kept because that is what the reader wants when
# the question is "does the extractor know about X".
_BASH_HELPERS = tuple(_BASH_NAME_ARG)


def _strip_comments(text):
    r"""-> the text with shell comments removed, and NOTHING else removed.

    `#` starts a comment only at a word boundary. `${shape#*|}` and `$#` are not
    comments, and cutting at the first `#` truncates the line to something that
    parses as a different program. That exact slip has produced two wrong counts in
    this repo, so the fixtures for it are in the arm below rather than in a comment.
    """
    out = []
    for line in text.splitlines():
        res, i, quote = [], 0, None
        while i < len(line):
            ch = line[i]
            if quote:
                if ch == quote:
                    quote = None
                res.append(ch)
            elif ch in "\"'":
                quote = ch
                res.append(ch)
            elif ch == "#" and (i == 0 or line[i - 1] in " \t;&|()"):
                break
            else:
                res.append(ch)
            i += 1
        out.append("".join(res))
    return "\n".join(out)


def _bodies(text):
    """-> [(function name, body)] with each body ended by ITS OWN closing brace.

    Per-line brace depth, not `find("\n}")`: 199 definitions in this tree are written
    on one line (`q() { psql ...; }`), and a scan for a brace in the first column
    swallows every following definition into the first one's body.
    """
    out, lines = [], text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"[ \t]*([A-Za-z_][A-Za-z0-9_]*)[ \t]*\(\)[ \t]*\{", line)
        if not m:
            continue
        depth = line.count("{") - line.count("}")
        body, j = [line[m.end():]], i + 1
        while j < len(lines) and depth > 0:
            depth += lines[j].count("{") - lines[j].count("}")
            body.append(lines[j])
            j += 1
        out.append((m.group(1), "\n".join(body)))
    return out


def _words(text):
    """-> the shell words of a call's argument list, quotes kept."""
    return re.findall(r'"[^"]*"|\S+', text)


def _derive_recorders(lib, seed=("pgc_record", 2)):
    """-> {helper: which argument holds the check name}, derived from what lib.sh DOES.

    THE POPULATION IS THE POINT (#1045). The #1040 guard derived its population by
    SPELLING -- every `lib.sh` function whose name begins `check`. It was green for
    weeks while `diff_query` went unread, and correctly so: `diff_query` was never in
    its population. The guard was not broken; the definition of the thing it guards
    was. 225 names across 59 suites were outside it.

    So: start from `pgc_record`, the primitive that actually records, and take the
    closure. A function is a recorder at position N when it passes its own `$N` --
    directly, or renamed once through a `local` -- into the name slot of a helper
    already known to be one. `diff_query` calls `check`, which calls `pgc_record`.
    One level of indirection was the entire gap.

    The seed is not returned. It is `lib.sh`'s own primitive and no suite calls it,
    which the arm below asserts rather than assumes: the day a suite calls it, the
    extractor has to learn it and this stops being true quietly.
    """
    lib = _strip_comments(lib)
    defs = _bodies(lib)
    known = {seed[0]: seed[1]}

    changed = True
    while changed:
        changed = False
        for fn, body in defs:
            if fn in known:
                continue
            aliases = {m.group(1): int(m.group(2)) for m in
                       re.finditer(r'\b([A-Za-z_][A-Za-z0-9_]*)="\$\{?(\d+)\}?"', body)}
            for rec, pos in sorted(known.items()):
                for m in re.finditer(r'\b' + rec + r'([ \t]+.*)$', body, re.M):
                    args = _words(m.group(1))
                    if len(args) < pos:
                        continue
                    slot = args[pos - 1]
                    inner = re.fullmatch(r'"\$\{?([A-Za-z_0-9]+)\}?"', slot)
                    if not inner:
                        continue          # a literal, or something not a bare $x
                    tok = inner.group(1)
                    n = int(tok) if tok.isdigit() else aliases.get(tok)
                    if n is None:
                        continue
                    known[fn] = n
                    changed = True
                    break
                if fn in known:
                    break

    del known[seed[0]]
    return known

def _template(name):
    """-> the name with every interpolation reduced to `{}`.

    Applied to both sides, so `non-owner refused: ${1%%(*}` and the f-string
    `f"non-owner refused: {fn}"` land on the same string.
    """
    return re.sub(r"\{[^{}]*\}", "{}", _BASH_INTERP.sub("{}", name))


# One shell word: a double-quoted string, or a run of non-space. Used only to STEP
# OVER the arguments before the name, never to capture one.
_WORD = r'(?:"[^"]*"|\S+)'


def _pattern_for(pos, helpers):
    """-> the regex reading a name from argument `pos` of any of `helpers`.

    `\b` before the alternation, so `check` does not match inside `pgc_check_thing`.
    The alternatives read longest-first, which LOOKS decisive and is not: python's
    `re` backtracks across them. The pattern SHAPE is what makes
    `check_ratio_needs_quiet_machine` readable, and an arm holds that.

    AN EMPTY `helpers` WOULD BE A DISASTER RATHER THAN A NO-OP. `(?:)` matches the
    empty string anywhere, so the pattern degenerates to "any word then any quoted
    string" and the tool FABRICATES names -- measured on `zonemap_boundaries.sh`,
    which contains no position-2 helper at all: `$PGC_DB`, `$(dirname `, `2`. Those
    are then reported as bash properties the port is missing, for ever, because no
    port can assert `$PGC_DB`. Silent junk is the worst of the three outcomes here.
    """
    if not helpers:
        raise ValueError(f"no helper names its check at argument {pos}: an empty "
                         f"alternation matches everywhere and would fabricate names")
    # `(_WORD + separator) * (pos - 1)`, NOT `_WORD * (pos - 1) + separator`. The
    # second form is byte-identical at positions 1 and 2 and wrong from 3 on: it runs
    # the word matchers together with no whitespace between them, so a position-3
    # helper reads NOTHING. Latent -- no helper is at 3 -- and it is the same shape as
    # the defect this function exists to close: a builder that silently mis-builds the
    # case nobody exercises. The `if not helpers` guard above cannot see it, because
    # `helpers` is not empty.
    return (r'\b(?:' + "|".join(helpers) + r')\s+'
            + (_WORD + r'\s+') * (pos - 1)
            + r'"([^"]+)"')


# Built from the POSITIONS the table actually uses, not from a hard-coded 1 and 2, so
# an empty group cannot be constructed: a position exists here only because some
# helper has it.
_BASH_PATTERNS = tuple(
    _pattern_for(pos, tuple(h for h, p in _BASH_NAME_ARG.items() if p == pos))
    for pos in sorted(set(_BASH_NAME_ARG.values())))

# Kept: the position-1 pattern is what the shadowing arm reads. Built by ASKING for
# position 1 rather than taking `_BASH_PATTERNS[0]`, which is the position-1 pattern
# only because 1 sorts first -- correct today and correct by accident.
_BASH_PATTERN = _pattern_for(1, tuple(h for h, p in _BASH_NAME_ARG.items() if p == 1))


def _bash_names(text):
    """-> every check name the suite states, each read from the argument that holds it.

    A LIST, not a set, because the caller reports both the total and the distinct
    count and the difference between them is information: a suite asserting the same
    property twice is a different thing from one asserting it once.
    """
    out = []
    for pat in _BASH_PATTERNS:
        out.extend(re.findall(pat, text))
    return out


def _suite_recorders(text):
    """-> ({helper: which argument holds the name}, [helpers whose name is unreadable]).

    A SUITE'S OWN RECORDERS, derived from its own definitions by the rule that already
    works for `lib.sh`: seed from the shared table, and any function forwarding a bare
    positional into a known recorder's name slot is itself a recorder (#1053).

    TWO SHAPES, AND ONLY ONE IS A GAP. The distinction is the whole of this function:

        COMPOSE   check "non-owner refused: ${1%%(*}"    the definition states a
                                                        TEMPLATE naming the property,
                                                        and it covers every call site
        FORWARD   check_text "$label" ...                the definition states nothing;
                                                        the NAME is at the call sites

    A composing wrapper is already read, correctly, out of the suite file -- which is
    why `native_ownership` grades one-for-one today. Treating it as unreadable and
    refusing it would have broken three COMPLETE pairs to fix nothing; measured, at 32
    suites refused including `hilbert_cluster`, `hilbert_locality` and
    `native_ownership`. So only FORWARDING wrappers are returned here, and the call
    sites are where their names are read.

    The unreadable list is the refuse half: a helper that reaches a recorder with a
    name slot this cannot resolve at all. Skipping it silently is how 147 names in 14
    suites came to be ungraded.
    """
    body_text = _strip_comments(text)
    known = dict(_BASH_NAME_ARG)
    known[_RECORD_PRIMITIVE] = _RECORD_NAME_ARG
    forwarding, unreadable = {}, []

    changed = True
    while changed:
        changed = False
        for fn, body in _bodies(body_text):
            if fn in known or fn in unreadable:
                continue
            aliases = {m.group(1): int(m.group(2)) for m in
                       re.finditer(r'\b([A-Za-z_][A-Za-z0-9_]*)="\$\{?(\d+)\}?"', body)}
            for rec, pos in sorted(known.items()):
                m = re.search(r"\b" + rec + r"([ \t]+.*)$", body, re.M)
                if not m:
                    continue
                args = _words(m.group(1))
                if len(args) < pos:
                    continue
                slot = args[pos - 1]
                bare = re.fullmatch(r'"\$\{?([A-Za-z_0-9]+)\}?"', slot)
                if bare:
                    token = bare.group(1)
                    n = int(token) if token.isdigit() else aliases.get(token)
                    if n is None:
                        # Reaches a recorder, and which argument carries the name
                        # cannot be decided. REFUSE rather than skip.
                        unreadable.append(fn)
                    else:
                        known[fn] = n
                        forwarding[fn] = n
                    changed = True
                    break
                # A literal or a composed name: the definition states the property and
                # `_bash_names` already reads it. Not a forwarder, not a refusal.
                break
    return forwarding, sorted(unreadable)


def _names_in(text):
    """-> every check name the suite states, including through its OWN wrappers.

    A BARE `{}` IS DROPPED. A forwarding wrapper's definition reads as `"$label"`,
    which reduces to the template `{}` -- a property with no content. Published, it
    sits in MISSING naming nothing a port could assert, and it MATCHES a port name
    that is entirely one interpolation, which is a spurious pass. 17 of them were
    being published. A wrong name is worse than an absent one, which is the argument
    #1051 turned on.
    """
    forwarding, _ = _suite_recorders(text)
    names = list(_bash_names(text))
    for helper, pos in sorted(forwarding.items()):
        names += re.findall(_pattern_for(pos, (helper,)), _strip_comments(text))
    # THE FILTER IS APPLIED ONCE, AT THE END, AND TO BOTH SOURCES. A forwarder calling
    # another forwarder -- `ans() { ansp "$1" h c "$2"; }` -- is a call site like any
    # other to the pattern, and it yields `$1`. Filtering only the definitions left
    # that one through, which the fixture below caught.
    return [n for n in names if _template(n) != "{}"]


def main(bash_file, py_file):
    """-> the exit status: 1 when a bash property has no counterpart."""
    bash_src = open(bash_file).read()

    # THE REFUSE HALF (#1053). A helper that reaches a recorder whose name argument
    # cannot be resolved makes every name it carries invisible, and grading the rest
    # would report a verdict about a suite the tool has only partly read. That is the
    # shape this whole issue is about, so it is a refusal rather than a silent skip.
    _forwarding, unreadable = _suite_recorders(bash_src)
    if unreadable:
        print(f"REFUSED: {bash_file} defines {len(unreadable)} helper(s) that record a "
              f"check under a name this cannot resolve:")
        for helper in unreadable:
            print(f"  unreadable  {helper}")
        print()
        print("Every check they carry is invisible, so any verdict here would be about "
              "the part of the suite that happens to be readable. Give the helper a "
              "name argument in a position the extractor can see, or record directly.")
        return 2

    py_src = open(py_file).read()
    bash_names = _names_in(bash_src)
    # NOTHING READ IS NOT NOTHING MISSING (#2032). A suite that records through its own
    # echo rather than a recorder yields no names, and an empty set has no member the
    # port lacks: `missing: 0`, "covered", against any port at all.
    if not bash_names:
        print(f"REFUSED: no check name could be read from {bash_file}.")
        print()
        print("A verdict over zero names says nothing about the suite. Record its checks "
              "through a lib.sh recorder so the extractor can see them.")
        return 2
    py_names = _py_names(py_src)
    # WHAT THE PORT ASSERTS is `py_names`, and that is what the header reports and
    # what `extra` lists. What a bash literal may be MATCHED against is wider, because
    # one parametrised arm stands for several concrete names (#1045 class 3).
    matchable = set(_matchable_names(py_src))

    bset, pset = set(bash_names), set(py_names)

    print(f"bash checks: {len(bash_names)} ({len(bset)} distinct)")
    print(f"pytest named assertions: {len(py_names)} ({len(pset)} distinct)")
    print()

    literal = bset & matchable
    # Only names with no literal partner are considered as templates, so a template
    # match can never hide a literal one or be double-counted.
    b_left = bset - literal
    p_left = pset - literal
    p_templates = {_template(n) for n in matchable - literal}
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
