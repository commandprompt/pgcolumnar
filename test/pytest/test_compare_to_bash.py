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
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from compare_to_bash import (_as_names, _bash_names, _bodies,  # noqa: E402
                             _expanded_names, _loop_names,
                             _matchable_names,
                             _derive_recorders, _names_in, _suite_recorders,
                             _parametrized_names, _py_names, _strip_comments,
                             _template, _words)


# THE PAIRS THIS TREE HOLDS, DECLARED IN BOTH DIRECTIONS (#1046).
#
# `COMPLETE` is graded by the standing arm below: every one must reach zero MISSING.
# `INCOMPLETE` is how a pair that does NOT reach zero is declared, with the reason,
# rather than being absent.
#
# WHY A DECLARATION AND NOT JUST A DERIVED LIST. Grading whatever exists would pin a
# real gap as the expected state, which the standing arm's docstring has always
# refused. Declaring the gap instead keeps the refusal and removes the silence: the
# arm asserts that COMPLETE + INCOMPLETE is exactly the set of pairs in the tree, so
#
#     a new COMPLETE pair omitted     reddens, with the stem named
#     a new INCOMPLETE pair omitted   reddens, with the stem named
#     a known gap                     declared with its reason, does not redden
#     a declared pair that vanishes   reddens
#
# IT DOES FORBID ONE THING, and the issue said this was the decision to make rather than
# assume: today an INCOMPLETE pair may land declaring NOTHING and nothing reddens. Here
# it must carry a stem and a reason. That is a new obligation on a real case -- the
# escape hatch is attached rather than the case forbidden, but a porter who lands a pair
# that does not reach zero now has to say so. Named by @OffgridwithJD, who pointed out
# that "forbids nothing that was allowed before" was the comfortable phrasing and the
# accurate one is "forbids nothing EXCEPT landing an incomplete pair silently", which is
# the thing this guard exists about.
#
# THE COST TODAY IS ZERO, measured on 20bc290: 8 pairs exist, 8 are declared, 0 exist
# undeclared and 0 are declared without existing. `INCOMPLETE` starts empty and the first
# person it costs is the next porter, who is the person it is for.
#
# Until #1046 the list was hand-written
# and nothing enforced it: the set happened to equal the tree, so nothing had ever been
# silently ungraded, and an eighth pair omitted would have left the arm passing while it
# graded seven -- absent-from-the-list and no-gap-found producing the same green.
#
# The shape is `SHELL_REFERENCES`' in `test_harness_deps.py`, asserted in both
# directions for the same reason: a one-way list rots into a permanent exemption.
COMPLETE = ["differential", "hilbert_cluster", "hilbert_locality",
            "native_ownership", "native_projection", "parallel_am_scan",
            "projection_privilege",
            "stats_privilege", "zonemap_boundaries"]

# stem -> why it does not yet reach zero. Empty today, and an entry here is a claim
# about the PORT rather than a licence: the standing arm does not grade it, so the
# reason is the only thing standing between a declared gap and a forgotten one.
INCOMPLETE = {
    "iceberg_fdw":
        "74 of its 76 bash names are asserted; the two that are not are `pgc_skip`'s "
        "refusal names, `python3 is needed` and `iceberg warehouse data files are "
        "missing`. THIS IS STRUCTURAL, not a gap in the port: `pgc_skip` records "
        "under the NAME it is given, while `expect.cannot_run` records under the "
        "REASON CODE, so a port cannot emit those two strings as check names at all. "
        "The port refuses by name-per-test through `_need`, which is the closest the "
        "python side can express. #1040's phase 0b is the open question; declared "
        "here rather than worked around by naming a passing premise after a missing "
        "dependency, which would read as an assertion that the fixture is absent.",
}



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


def test_a_name_bound_by_a_loop_over_a_literal_table_is_read(expect):
    """CLASS 2 OF #1045: the port's own names, invisible to the grader.

        for label, sql in (("allnull column scan",  "SELECT * FROM %T"),
                           ("allnull column count", "SELECT count(allnull) FROM %T")):
            c, h = p.both(sql)
            expect.row_set(c, h, label)

    `_as_names` reads a `Name` node as nothing, deliberately -- reporting a guessed
    string is worse than reporting none -- so the arm RAN AND PASSED while the grader
    reported its bash counterpart MISSING. 46 names across 15 sites, 37 of them in
    `differential`, whose port is behaviourally complete.

    This is `_parametrized_names` one level down, and the same answer: the table is
    literal, the column is a name, read the column.
    """
    tree = ast.parse(
        'for label, sql in (("allnull column scan", "SELECT * FROM %T"),\n'
        '                   ("allnull column count", "SELECT count(a) FROM %T")):\n'
        '    c, h = p.both(sql)\n'
        '    expect.row_set(c, h, label)\n')
    expect.text(", ".join(_loop_names(tree)),
                "allnull column scan, allnull column count",
                "the label column is read, in table order")

    # AND THE OTHER COLUMN IS NOT. A loop variable that is merely mentioned in the
    # body is not a name; harvesting it would invent properties out of SQL strings.
    expect.num(sum(1 for n in _loop_names(tree) if "SELECT" in n), 0,
               "and the column that is NOT a name argument contributes nothing")

    # AND IT IS WIRED IN, which is a separate fact from the reader working. Measured:
    # disconnecting `_loop_names` from `_py_names` reddened NO arm until this line
    # existed -- every one of them called the reader directly. A reader nobody consults
    # is the same defect as a guard nobody exercises, and this file has now shipped
    # three of those.
    got = _py_names(
        'for label, sql in (("allnull column scan", "SELECT * FROM %T"),):\n'
        '    expect.row_set(c, h, label)\n')
    expect.text(", ".join(got), "allnull column scan",
                "and `_py_names` returns it, so the grader actually sees it")


def test_a_literal_column_survives_an_interpolated_neighbour(expect):
    r"""READ PER ELEMENT, NOT PER ROW, and `differential` is why.

        ("textbloom collate-mismatch",
         f"SELECT count(*) FROM %T WHERE tk = '{present}' COLLATE \"C\"")

    `ast.literal_eval` on the whole table raises on that f-string, so a row-at-a-time
    reader drops all five labels beside it -- every one of them a plain literal. The
    column is read cell by cell through `_as_names` for that reason.
    """
    tree = ast.parse(
        'for label, sql in (("textbloom present", "SELECT 1"),\n'
        '                   ("textbloom collate-mismatch", f"SELECT {x}")):\n'
        '    expect.row_set(c, h, label)\n')
    expect.text(", ".join(_loop_names(tree)),
                "textbloom present, textbloom collate-mismatch",
                "a literal label survives an interpolated neighbour in the same row")


def test_a_table_that_is_not_literal_contributes_nothing(expect):
    """THE REFUSAL, because the alternative is guessing.

    A loop over a name, a comprehension, or a row whose label is itself computed
    cannot be read without running the file. `_as_names`' rule holds here too: absent
    beats wrong, because a wrong name can never be matched by the other harness and is
    reported MISSING for ever.
    """
    for src, why in (
            ('for label, sql in CASES:\n    expect.num(g, 1, label)\n',
             "a loop over a module name"),
            ('for label, sql in [(mk(i), "q") for i in r]:\n'
             '    expect.num(g, 1, label)\n', "a comprehension"),
            ('for label, sql in ((LABEL_ONE, "q"),):\n    expect.num(g, 1, label)\n',
             "a row whose label is itself a name")):
        expect.num(len(_loop_names(ast.parse(src))), 0,
                   f"{why} contributes no names rather than guessed ones")

    # THE CONTROL, so the three above are a refusal and not a broken reader.
    expect.text(", ".join(_loop_names(ast.parse(
        'for label in ("one", "two"):\n    expect.num(g, 1, label)\n'))),
        "one, two", "control: a literal table over a single column still reads")


def test_the_loop_reader_invents_nothing_in_this_corpus(expect):
    """EVERY NAME IT RETURNS IS TEXT THE FILE CONTAINS, asserted over the tree rather
    than over a fixture, because the risk this guards is a reader that CONSTRUCTS a
    string rather than finding one.
    """
    added, absent, files = 0, [], []
    for py in sorted(HERE.glob("test_*.py")):
        src = py.read_text()
        got = _loop_names(ast.parse(src))
        if got:
            files.append(py.name[5:-3])
        added += len(got)
        absent += [n for n in got if n not in src]
    expect.at_least(added, 40,
                    "premise: the reader really does add names in this tree, so the "
                    "assertion below is not vacuous")
    expect.text(", ".join(absent) or "none", "none",
                "every name the loop reader returns appears verbatim in the file it "
                "came from")
    expect.text(", ".join(files),
                "build_refusal, differential, join_runtime_filter",
                "and it is these files, so a fourth appearing is a diff a reviewer "
                "sees rather than a number that moved")


# A port that parametrises a family its bash twin unrolls, which is the whole of
# #1045 class 3. Module-level so several arms share one subject.
# WRITTEN AS CONCATENATED LINES, not a triple-quoted block, so no line of it starts
# at column 0. `test_docs_cover_the_corpus.py` finds tests with `^def (test_\w+)` over
# the file text, and a triple-quoted fixture puts its `def test_...` exactly there --
# so the guard read this fixture as an undocumented test, correctly, because a text
# scan cannot tell a fixture from a definition. Every other fixture in this file is
# written this way for the same reason.
_EXPAND_FIXTURE = (
    'RANGES = {"c_int": "c_int > 1", "c_vc": "c_vc > \'a\'"}\n'
    '@pytest.mark.parametrize("col", sorted(RANGES))\n'
    'def test_a_range_agrees(matrix, col, expect):\n'
    '    c, h = matrix.both(f"SELECT id FROM %T WHERE {RANGES[col]}")\n'
    '    expect.row_set(c, h, f"{col} range")\n'
)


def test_a_parametrised_family_expands_to_the_names_bash_unrolls(expect):
    """#1045 CLASS 3. The bash suite writes 11 literals where the port writes one
    template over 11 columns, so the two never met and the port graded INCOMPLETE for
    17 properties it asserts.

    Resolved to CONCRETE names and matched literally, which is what keeps it
    falsifiable -- see the arm below. Widening `_template` instead would match
    `c_bytea range` against `{} range` whether or not the port covers `c_bytea`.
    """
    got = _expanded_names(ast.parse(_EXPAND_FIXTURE))
    expect.text(", ".join(sorted(got)), "c_int range, c_vc range",
                "the family expands to one concrete name per member")

    # AND IT IS NOT AN ASSERTION. `_py_names` answers what the port asserts and the
    # report prints that; the expansion answers which spellings a bash literal may be
    # compared with. Conflating them made `differential` report 274 named assertions
    # where the port has 100.
    expect.num(sum(1 for n in _py_names(_EXPAND_FIXTURE) if n == "c_int range"), 0,
               "the expansion is not counted as something the port asserts")
    expect.num(sum(1 for n in _matchable_names(_EXPAND_FIXTURE) if n == "c_int range"), 1,
               "but it IS matchable, which is the only thing it is for")


def test_dropping_a_member_brings_the_divergence_back_named(expect):
    """THE PROPERTY THAT MAKES EXPANSION RIGHT AND WIDENING WRONG.

    Expansion produces literals on both sides, so the match stays decidable when the
    port changes. Remove a column from the port's container and the bash name it
    covered is reported MISSING, BY NAME. A widened template cannot do that: `{} range`
    matches whatever the port happens to cover, so it buys the 17 by permanently
    retiring the ability to notice those 17 breaking.
    """
    bash = ["c_int range", "c_vc range"]
    full = set(_matchable_names(_EXPAND_FIXTURE))
    expect.num(len([b for b in bash if b not in full]), 0,
               "premise: with both members present, neither bash name is missing")

    thinned = set(_matchable_names(_EXPAND_FIXTURE.replace(
        '"c_vc": "c_vc > \'a\'"', '')))
    lost = sorted(b for b in bash if b not in thinned
                  and _template(b) not in {_template(n) for n in thinned})
    expect.text(", ".join(lost), "c_vc range",
                "and dropping a member reports exactly the bash name it covered, by "
                "name rather than as a count")


def test_a_templated_pair_is_not_orphaned_by_the_expansion(expect):
    """THE ADDITIVE CONSTRAINT, and it is the regression this design otherwise causes.

    Where BOTH sides are templated, the TEMPLATE is the match. 11 of
    `hilbert_locality`'s 30 bash names and 10 of `hilbert_cluster`'s match that way,
    so replacing the port's template with concrete names orphans them. Measured:
    replacing breaks three green pairs and takes `differential` to 13 rather than 0.

    NO `differential` FIXTURE CATCHES THIS. The regression appears only where both
    sides are templated, and `differential`'s range and eq families are asymmetric by
    construction -- literals on one side, a template on the other. An implementer
    testing against the motivating pair alone ships the break, which is why this arm
    runs the real pair.

    It is also INDEPENDENT of the refusals below: those bash names are themselves
    templated, so dropping the port's templated form orphans them whether or not
    anything else is ever expanded.
    """
    import io, contextlib
    from compare_to_bash import main
    for stem in ("hilbert_locality", "hilbert_cluster", "native_ownership"):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = main(str(HERE.parent / f"{stem}.sh"), str(HERE / f"test_{stem}.py"))
        expect.num(rc, 0, f"{stem} still grades one-for-one after the expansion")


def test_the_expansion_refuses_what_it_cannot_spell(expect):
    """THREE REFUSALS, each costing a false MISSING at worst.

    A MODULE CONSTANT does not render the way its source spells it -- `FLOAT_RTOL =
    1e-6` becomes `1e-06` in an f-string -- and the bash side carries no such name. A
    resolvable value is not one that renders the way the other side spells it.

    TWO OR MORE DISTINCT COLUMNS is a cartesian product, and expanding one while
    holding the other invents names that exist nowhere. Three sites in this tree, all
    in `hilbert_cluster`, which grades clean and stays clean because refusing means
    not ADDING. Built when a pair needs it, against a real example.

    A NON-LITERAL CONTAINER cannot be resolved without running the file.
    """
    constant = ('FLOAT_RTOL = 1e-6\n'
                '@pytest.mark.parametrize("col", ["c_int"])\n'
                'def test_x(col, expect):\n'
                '    expect.num(g, 1, f"{col} within {FLOAT_RTOL} relative")\n')
    expect.num(len(_expanded_names(ast.parse(constant))), 0,
               "a name interpolating a module constant is refused, because 1e-6 "
               "renders as 1e-06 and bash carries neither")

    multi = ('@pytest.mark.parametrize("old", ["a"])\n'
             '@pytest.mark.parametrize("new", ["b"])\n'
             'def test_x(old, new, expect):\n'
             '    expect.num(g, 1, f"{new} has {old}\'s signature")\n')
    expect.num(len(_expanded_names(ast.parse(multi))), 0,
               "two distinct parametrised columns are refused rather than expanded "
               "one at a time")

    computed = ('@pytest.mark.parametrize("col", CASES)\n'
                'def test_x(col, expect):\n'
                '    expect.num(g, 1, f"{col} range")\n')
    expect.num(len(_expanded_names(ast.parse(computed))), 0,
               "a container that is not a literal is refused rather than guessed")

    # THE CONTROL, so the three above are decisions and not a broken expander.
    expect.num(len(_expanded_names(ast.parse(_EXPAND_FIXTURE))), 2,
               "control: the readable form still expands")


def test_the_expansion_reads_only_the_name_argument(expect):
    """AND NOTHING ELSE, through `_name_argument`.

    Expanding every f-string in a body instead emits SQL as check names -- measured on
    `differential`, 240 names where the restricted form emits 144, 96 of them
    `SELECT ... FROM %T`. That form resolves the 17 as well, so it LOOKS like it works
    and only its own output says otherwise.

    A second copy of "which argument holds the name" drifts: @jdatcmd's prototype
    returned ZERO expansions because it re-implemented the decision, and an empty list
    reads as "expansion achieves nothing" rather than as an error.
    """
    src = ('@pytest.mark.parametrize("col", ["c_int"])\n'
           'def test_x(col, expect, matrix):\n'
           '    c, h = matrix.both(f"SELECT id, {col} FROM %T")\n'
           '    expect.row_set(c, h, f"{col} project")\n')
    got = _expanded_names(ast.parse(src))
    expect.text(", ".join(got), "c_int project",
                "the name argument expands")
    expect.num(sum(1 for n in got if "SELECT" in n), 0,
               "and an f-string in another argument contributes nothing, however "
               "expandable it looks")


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


def test_refusal_names_its_second_argument_not_its_last_pattern(expect):
    """`refusal(result, name, *patterns)` puts the name in the MIDDLE.

    The last argument is a pattern -- a fragment of the message the refusal must carry --
    so the last-argument rule read a substring of an error message as the property's name.
    The real name went MISSING and the pattern arrived as an EXTRA: two false entries from
    one call, which is the same defect this file exists to close, one helper along.
    """
    src = ('def t(expect):\n'
           '    expect.refusal(result, "a role with no privilege is refused",\n'
           '                   "permission denied", "for table")\n')
    got = _names(src)
    expect.text(", ".join(sorted(got)), "a role with no privilege is refused",
                "the name is read and neither pattern is")
    expect.num(len(got), 1, "one call contributes exactly one name")


def test_refusal_with_no_pattern_is_not_the_arm_that_proves_it(expect):
    """THE CONTROL that keeps the arm above honest.

    `expect.refusal(result, NAME)` has the name last, so it is read correctly by the rule
    this change replaces AND by the rule that replaces it. An arm built only on that shape
    would pass against the defect, which is how the shape got missed in the first place.
    """
    src = 'def t(expect):\n    expect.refusal(result, "the write is refused")\n'
    expect.text(", ".join(_names(src)), "the write is refused",
                "the no-pattern shape reads the same either way, so it proves nothing alone")


def test_cannot_run_names_its_reason_not_its_detail(expect):
    """`cannot_run(reason, detail="")` records `name=reason`: the FIRST argument.

    It is the only helper whose name is argument zero, and the detail beside it is prose
    about one run -- "the two partitions are not different ({})" -- which can never match
    a bash check name. Reading it produced an extra that no bash suite could ever satisfy.
    """
    src = ('def t(expect):\n'
           '    expect.cannot_run("MISSING_DEPENDENCY",\n'
           '                      "the two partitions are not different")\n')
    got = _names(src)
    expect.text(", ".join(got), "MISSING_DEPENDENCY",
                "the reason CODE is the name, and the detail is not a name at all")
    expect.num(len(got), 1, "the detail contributes nothing")


def test_a_helper_whose_name_is_optional_takes_it_only_from_the_keyword(expect):
    """`plan_marker` and `plan_node` carry no name positionally. Better absent than wrong.

    `plan_marker(plan, key, name=None)` records `name or f"plan carries {key!r}"`, so the
    KEY is not the name even when no name is given -- it is a fragment of one. The
    last-argument rule emitted the bare key as a name the bash suite does not have, and
    `Columnar Projected Columns` duly appeared as an extra on a pair that is complete.

    With no name= the call contributes NOTHING, which reports MISSING rather than inventing
    a name: the rule this file already applies to a name it cannot read.
    """
    named = _names('def t(expect):\n'
                   '    expect.plan_marker(plan, "Columnar Projected Columns",\n'
                   '                       name="the plan projects two columns")\n')
    expect.text(", ".join(named), "the plan projects two columns",
                "the name= keyword is the name, and the key is not also collected")
    expect.num(len(named), 1, "one call, one name -- the key is not a second entry")

    bare = _names('def t(expect):\n'
                  '    expect.plan_marker(plan, "Columnar Projected Columns")\n')
    expect.num(len(bare), 0, "with no name= the key is still not a name")

    node = _names('def t(expect):\n'
                  '    expect.plan_node(plan, provider="columnar",\n'
                  '                     name="the scan is columnar")\n')
    expect.text(", ".join(node), "the scan is columnar",
                "plan_node reads its name= and not the arguments describing the node")


def test_the_tools_table_agrees_with_the_signatures_it_describes(expect):
    """THE DRIFT GUARD, and the reason the table is allowed to be a hand-written map.

    `compare_to_bash.py` is deliberately standalone -- `ast`, `re`, `sys` -- so it cannot
    import `Expect` to ask where each name sits, and a hand-written table is a derived
    value that goes stale the day somebody adds a helper. This arm is what stops that: it
    reads the REAL signatures out of `pgc_vacuity.py` and recomputes, for every public
    helper, which call argument carries the name the helper records.

    It is not a copy of the table. The table says where to look; this derives where to look
    from the source of truth and compares. A helper added with its name anywhere but last,
    or a signature reordered, fails here with the helper named.
    """
    from compare_to_bash import _NAME_ARG

    # The ONE thing a signature cannot state: which parameter becomes the record's name.
    # `cannot_run` records `name=reason`; every other helper calls its parameter `name`.
    # Pinned below against the body, so this line cannot quietly become wrong either.
    records_name_as = {"cannot_run": "reason"}

    src = (HERE / "pgc_vacuity.py").read_text()
    tree = ast.parse(src)
    klass = [n for n in ast.walk(tree)
             if isinstance(n, ast.ClassDef) and n.name == "Expect"]
    expect.num(len(klass), 1, "premise: exactly one Expect class to read")

    helpers = [f for f in klass[0].body
               if isinstance(f, ast.FunctionDef) and not f.name.startswith("_")]
    # `records` and `count` take no arguments and record no name.
    helpers = [f for f in helpers if [a.arg for a in f.args.args if a.arg != "self"]]
    expect.at_least(len(helpers), 15,
                    "premise: the Expect helpers were found, not an empty list")

    disagree, checked = [], 0
    for f in helpers:
        params = [a.arg for a in f.args.args if a.arg != "self"]
        ndef = len(f.args.defaults)
        required = params[:len(params) - ndef] if ndef else params
        param = records_name_as.get(f.name, "name")
        # Optional => no positional carries it; only a `name=` keyword can.
        want = required.index(param) if param in required else None
        got = _NAME_ARG.get(f.name, -1)
        # -1 is "the last positional". That equals the name's own index only when the
        # name really is last AT THE CALL SITE, and a `*args` AFTER it means it is not:
        # `refusal(result, name, *patterns)` declares `name` last and is still called
        # with patterns beyond it. Without that clause this arm accepted a missing
        # `refusal` entry, which is the very shape it is here to catch.
        if (got == -1 and want is not None and want == len(required) - 1
                and f.args.vararg is None):
            got = want
        checked += 1
        if got != want:
            disagree.append(f"{f.name}: table says {got!r}, signature says {want!r}")

    expect.num(checked, len(helpers), "inputs == sum(buckets): every helper was compared")
    expect.text("; ".join(disagree) or "none", "none",
                "every entry in the table matches the signature it describes")

    # The one hand-written semantic claim above, pinned against the body it describes:
    # read cannot_run's own `_record(...)` call and check which parameter it names.
    #
    fn = [f for f in helpers if f.name == "cannot_run"]
    expect.num(len(fn), 1, "premise: cannot_run is among the helpers read")
    recorded = [kw.value.id for call in ast.walk(fn[0])
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute) and call.func.attr == "_record"
                for kw in call.keywords
                if kw.arg == "name" and isinstance(kw.value, ast.Name)]
    expect.text(", ".join(recorded), "reason",
                "cannot_run really does record its reason as the name")

def test_no_later_argument_can_overtake_the_name(expect):
    """`-1` is a claim about the CALL SITE, and the arm above only reads the SIGNATURE.

    Found by @OffgridwithJD reviewing the change this file documents, inside the very
    clause that fixed the vararg coincidence. The guard asks "which parameter carries the
    name", which is a fact about the declaration. `-1` says "the last argument", which is
    a fact about the call. They agree only while no OPTIONAL parameter sits after the
    name, because an optional one may still be passed POSITIONALLY:

        expect.rows(got, want, "THE NAME", "the reason")   -> read 'the reason'
        expect.plan_marker(plan, "key", "THE NAME")        -> read nothing at all

    Both were legal, both read wrong, and every guard in this file stayed green. The
    second is the worse one: a DROPPED name reports the bash property MISSING, and
    MISSING is what drives `rc`.

    Latent rather than live -- no call site in the tree passes a trailing optional
    positionally -- but #1037 makes `allow_empty` a reason STRING, which is exactly the
    argument somebody writes positionally next to a name.

    So the property is closed in the SIGNATURES rather than patched in the reader: every
    parameter after the name is keyword-only, and this arm holds that. A wrong call is
    then a `TypeError`, not a silently misread name.
    """
    src = (HERE / "pgc_vacuity.py").read_text()
    klass = [n for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.ClassDef) and n.name == "Expect"]
    expect.num(len(klass), 1, "premise: exactly one Expect class to read")
    helpers = [f for f in klass[0].body
               if isinstance(f, ast.FunctionDef) and not f.name.startswith("_")
               and [a.arg for a in f.args.args if a.arg != "self"]]
    expect.at_least(len(helpers), 15, "premise: the helpers were found, not an empty list")

    from compare_to_bash import _NAME_ARG

    overtakable, checked = [], 0
    for f in helpers:
        params = [a.arg for a in f.args.args if a.arg != "self"]
        checked += 1
        if "name" not in params:
            # Carried only as a keyword, or named something else (`cannot_run`, whose
            # name is argument 0 and cannot be overtaken by anything after it).
            continue
        if _NAME_ARG.get(f.name, -1) is None:
            # The table says NO positional argument carries the name, so the reader
            # skips the call entirely. If `name` can still be written positionally the
            # name is DROPPED, which reports the bash property MISSING and moves `rc`.
            overtakable.append(f"{f.name}: the table reads no positional name, yet name "
                               f"can be passed positionally")
            continue
        after = params[params.index("name") + 1:]
        if after:
            overtakable.append(f"{f.name}: {', '.join(after)} can be passed positionally "
                               f"after name")

    expect.num(checked, len(helpers), "inputs == sum(buckets): every helper was examined")
    expect.text("; ".join(overtakable) or "none", "none",
                "no positional argument can be written after the name and be read as it")


def test_the_extractor_reads_every_recorder_lib_sh_defines(expect):
    r"""THE BASH-SIDE DRIFT GUARD (#1040), WIDENED FROM SPELLING TO BEHAVIOUR (#1045).

    The #1040 form derived its population with `^(check(?:_[a-z_]+)?)\(\)` -- every
    `lib.sh` function whose NAME begins `check`. That found the three helpers #1040
    was about. It could not find `diff_query`, which forwards its `$1` into `check`
    and is named nothing like it.

    **The guard was green the whole time, and it was not broken. Its population was.**
    A guard is worth exactly the set it ranges over, and this one asked what a
    function is CALLED. Measured on `bf31e2f`, before the widening:

        names the extractor read from differential.sh     6
        names differential.sh actually states            86
        corpus-wide, names no graded or future port could match     249 across 70 suites

    `_derive_recorders` takes the closure from `pgc_record` instead, so membership
    follows from what a function does. It finds five the spelling did not:
    `diff_query`, `diff_query_ordered`, `pgc_skip`, `pgc_pass`, `pgc_fail`.

    IT ALSO CHECKS THE POSITION. `pgc_skip`'s name is `$2`, and a membership-only
    guard would have gone green while the extractor read capabilities as check names
    -- a name that is WRONG rather than absent, which no port can ever match and
    which is reported MISSING for ever.
    """
    from compare_to_bash import _BASH_NAME_ARG

    lib = (HERE.parent / "lib.sh").read_text()
    derived = _derive_recorders(lib)
    expect.at_least(len(derived), 13,
                    "premise: lib.sh's recorders were derived, not an empty set")

    missing = sorted(set(derived) - set(_BASH_NAME_ARG))
    extra = sorted(set(_BASH_NAME_ARG) - set(derived))
    expect.text(", ".join(missing) or "none", "none",
                "every recorder lib.sh defines is one the extractor reads")
    expect.text(", ".join(extra) or "none", "none",
                "and the extractor claims no recorder lib.sh does not define")

    wrong = sorted(f"{h} (table ${_BASH_NAME_ARG[h]}, lib.sh ${derived[h]})"
                   for h in derived
                   if h in _BASH_NAME_ARG and derived[h] != _BASH_NAME_ARG[h])
    expect.text(", ".join(wrong) or "none", "none",
                "and each name is read from the argument lib.sh actually names it in")

    # THE SEED IS EXCLUDED, and the first version of this arm asserted that no suite
    # calls `pgc_record` directly. THAT IS FALSE: eleven files do, inside their own
    # suite-local wrappers (`audit.sh`, `unique_conc.sh`). The arm is kept and the
    # claim narrowed to the one that holds and that grading actually depends on --
    # none of those suites has a pytest twin, so none is graded and the seed's
    # absence from the table costs nothing today.
    #
    # Suite-LOCAL recorders are a separate population and out of scope here, as the
    # arm below says. Two ways of finding them give OVERLAPPING, NOT NESTED, answers,
    # and neither is a superset of the other:
    #
    #   by spelling (`check_*`, what that arm uses)   4 helpers in 2 suites
    #   by behaviour (forwards a bare positional)    27 helpers in 25 suites
    #
    # `check_float` is in both. `parallel_copy.sh`'s three are found ONLY by spelling,
    # because they compose the name rather than forward it -- `check "$label: offsets
    # well-formed"` -- so the extractor reads their shape as a template from
    # `parallel_copy.sh` itself while the 12 labels the suite supplies go unread. The
    # behaviour list is the one that matters for the queue: `sorted_pathkeys.sh`
    # defines `ans` and `ansp` and loses 19 of its 113 names, and `phase6.sh` loses 39
    # of 43. Filed rather than fixed here: this change is about the recorders `lib.sh`
    # shares, and widening both populations at once would make the removal proof
    # unreadable.
    direct = sorted(sh.name[:-3] for sh in HERE.parent.glob("*.sh")
                    if sh.name != "lib.sh"
                    and re.search(r'\bpgc_record\s+[A-Z]', _strip_comments(sh.read_text())))
    expect.at_least(len(direct), 1,
                    "premise: some suite does call pgc_record directly, so this is "
                    "not vacuous")
    twinned = sorted(d for d in direct if (HERE / f"test_{d}.py").exists())
    expect.text(", ".join(twinned) or "none", "none",
                "no suite calling pgc_record directly has a twin, so excluding the "
                "seed costs no grading today")


def test_a_lib_sh_wrapper_that_forwards_a_name_is_read(expect):
    """THE WRAPPER BLIND SPOT (#1045).

        diff_query() {
            local label="$1" tmpl="$2"
            ...
            check "$label" "$hc" "$hq"
        }

    The NAME is in the suite -- `diff_query "c_uuid range" "SELECT ..."` -- and only
    the RECORDER is in `lib.sh`. So this is NOT class 4 of #1045, where the name
    itself lives in `lib.sh` and no suite supplies one. The suite says what the
    property is called and the extractor was not reading it.

    WHAT MADE IT INVISIBLE FOR SO LONG: `differential`'s port names are separately
    unreadable, bound by a `for` loop variable (#1045 class 2). So the pair graded
    `missing: 0` on 6 of 86 bash names against 62 of 132 port names. **Two blind
    halves cannot disagree**, and the verdict read as the strongest one in the set.
    """
    from compare_to_bash import _bash_names
    src = ('diff_query "c_uuid range" "SELECT id FROM %T WHERE c_uuid > $1"\n'
           'diff_query_ordered "sorted scan" "SELECT * FROM %T ORDER BY id"\n'
           'pgc_pass "the rewrite happened"\n'
           'pgc_fail "the rewrite did not happen" "no new group"\n')
    expect.text(", ".join(sorted(_bash_names(src))),
                "c_uuid range, sorted scan, the rewrite did not happen, "
                "the rewrite happened",
                "a name passed to a lib.sh wrapper is read from the suite")


def test_the_wrapper_whose_name_is_the_second_argument(expect):
    """`pgc_skip` IS NOT `$1`, AND PUTTING IT IN THE `$1` GROUP IS WORSE THAN BLIND.

        pgc_skip() {  # pgc_skip <capability> <message>
            ...
            pgc_record FAIL "$2" "FAIL  $2"

    The capability is `$1`, the NAME is `$2`. 68 of the 70 call sites write the
    capability bare, so a pattern keyed to the first QUOTED argument reads the name
    at those 68 and the CAPABILITY at the other two:

        pgc_skip pyarrow "pyarrow not available; ..."     <- quoted arg IS the name
        pgc_skip "test_decoding" "test_decoding.so ..."   <- quoted arg is NOT

    An absent name is a gap. A WRONG name is a permanent false MISSING: no port can
    assert `test_decoding`, so no port can ever close it. Hence the position table.
    """
    from compare_to_bash import _bash_names
    expect.text(", ".join(_bash_names(
        'pgc_skip pyarrow "pyarrow not available; the suite needs it"\n')),
        "pyarrow not available; the suite needs it",
        "an unquoted capability does not stop the name being read")
    expect.text(", ".join(_bash_names(
        'pgc_skip "test_decoding" "test_decoding.so is not in the pkglibdir"\n')),
        "test_decoding.so is not in the pkglibdir",
        "and a QUOTED capability is not mistaken for the name")


def test_the_derivation_finds_a_wrapper_planted_in_a_fixture(expect):
    """THE GUARD IS ONLY WORTH ITS POPULATION, so the derivation is exercised where
    the answer is known and not only on `lib.sh`, where it is green.

    Four shapes, because the corpus has all four: a direct positional, one renamed
    through a `local`, a name in the second argument, and a one-line definition. Plus
    a CONTROL that must not be picked up -- a function supplying its OWN literal
    name, which is #1045 class 4 proper and no tuple entry can fix.
    """
    fixture = (
        'pgc_record() { PGC_CHECKS=$((PGC_CHECKS + 1)); }\n'
        'rec() {\n\tpgc_record "$1" "$2" "$3"\n}\n'
        'wrap_direct() {\n\trec PASS "$1" "PASS $1"\n}\n'
        'wrap_local() {\n\tlocal label="$1"\n\trec PASS "$label" "x"\n}\n'
        'wrap_second() {\n\trec SKIP "$2" "SKIP $2"\n}\n'
        'one_liner() { rec PASS "$1" "PASS $1"; }\n'
        'owns_its_name() {\n\trec PASS "premise: the oracle is order-sensitive" "x"\n}\n'
    )
    got = _derive_recorders(fixture)
    expect.text(", ".join(f"{k}:${v}" for k, v in sorted(got.items())),
                "one_liner:$1, rec:$2, wrap_direct:$1, wrap_local:$1, wrap_second:$2",
                "every forwarding shape is found and each carries its own position")
    expect.num(1 if "owns_its_name" in got else 0, 0,
               "control: a function supplying its OWN literal name is not a wrapper")


def test_the_comment_stripper_keeps_a_parameter_expansion(expect):
    r"""`#` STARTS A COMMENT ONLY AT A WORD BOUNDARY.

    `${shape#*|}` and `$#` are not comments. Cutting at the first `#` truncates the
    line to something that parses as a different program, and it has produced two
    wrong published counts in this repo -- once against a figure that was right, and
    once three hours after the first fix, in a function whose body was `local n=$#`.

    So the stripper's fixtures live here, where a regression reddens, rather than in
    a comment saying to be careful.
    """
    for src, want in (
            (r'x=${shape#*|}', r'x=${shape#*|}'),
            (r'local n=$#; echo $n', r'local n=$#; echo $n'),
            (r'foo  # a comment', r'foo  '),
            (r'echo "a # b"  # tail', r'echo "a # b"  ')):
        expect.text(_strip_comments(src), want,
                    f"the stripper leaves {src!r} as the shell reads it")
    # Separately, because `expect.text` refuses an empty expectation -- rightly, since
    # anything empty would satisfy it -- and a whole-line comment must strip to empty.
    expect.num(len(_strip_comments(r'# whole line')), 0,
               "and a whole-line comment strips to nothing at all")


def test_an_empty_helper_group_fabricates_names_rather_than_reading_none(expect):
    """AN EMPTY ALTERNATION IS NOT A NO-OP, IT IS A NAME GENERATOR.

    The extractor now reads a name from the argument that holds it, so it builds one
    pattern per POSITION. A position whose helper list is empty gives `(?:)`, which
    matches the empty string anywhere, and the pattern degenerates to "any word, then
    any quoted string". Measured on `zonemap_boundaries.sh`, which contains no
    position-2 helper at all:

        $(dirname
        premise: the boundary fixture has two row groups
        2
        $PGC_PORT
        $PGC_DB

    Six fabricated names, reported as bash properties the port is missing -- for
    ever, because no port can assert `$PGC_DB`. That is worse than reading nothing
    and worse than an error, and it is SILENT.

    THIS IS WHY THE POSITIONS ARE DERIVED FROM THE TABLE'S OWN VALUES. A position
    exists only because a helper has it, so the empty group cannot be built. The arm
    holds the refusal anyway, because the next person to touch this will reach for a
    literal list of positions, which is what the first version of it did.

    Found when a reviewer's stale `.pyc` left `_BASH_NAME_ARG` mid-mutation and the
    tool started emitting `$PGC_DB` out of a suite that could not produce it.
    """
    from compare_to_bash import _pattern_for, _BASH_NAME_ARG, _BASH_PATTERNS

    raised = ""
    try:
        _pattern_for(2, ())
    except ValueError as e:
        raised = "refused"
    expect.text(raised, "refused", "an empty helper group is refused, not built")

    # THE CONTROL: the same call with a member returns a pattern that reads that
    # member, so the refusal is about emptiness and not about the function.
    got = re.findall(_pattern_for(2, ("pgc_skip",)),
                     'pgc_skip pyarrow "pyarrow is needed"\n')
    expect.text(", ".join(got), "pyarrow is needed",
                "control: a group with a member builds a pattern that reads it")

    # AND THE THING THAT MAKES THE REFUSAL UNREACHABLE: every position comes from a
    # helper, so no group can be empty by construction.
    expect.num(len(_BASH_PATTERNS), len(set(_BASH_NAME_ARG.values())),
               "one pattern per position the table actually uses, so no group is empty")

    # The fabrication itself, on the real suite, with the degenerate pattern built by
    # hand -- the arm must show the harm and not only assert the refusal.
    degenerate = r'\b(?:' + "|".join(()) + r')\s+(?:"[^"]*"|\S+)\s+"([^"]+)"'
    junk = re.findall(degenerate, (HERE.parent / "zonemap_boundaries.sh").read_text())
    expect.at_least(len(junk), 1,
                    "premise: the degenerate pattern really does fabricate names from a "
                    "suite with no position-2 helper")
    expect.num(1 if "$PGC_DB" in junk else 0, 1,
               "and one of them is a shell variable, which no port could ever assert")

    # AND THE SEPARATOR, which is the same class one position further out. The
    # arguments before the name must be separated by whitespace: running the word
    # matchers together is byte-identical at 1 and 2 and reads NOTHING from 3 on.
    # Nothing sits at position 3 today, so this is the arm that would notice.
    expect.text(", ".join(re.findall(_pattern_for(3, ("demo_helper",)),
                                     'demo_helper arg1 arg2 "the check name"\n')),
                "the check name",
                "a helper naming its check at argument 3 is read, not silently missed")
    expect.text(", ".join(re.findall(_pattern_for(1, ("demo_helper",)),
                                     'demo_helper "the check name" got want\n')),
                "the check name",
                "control: position 1 still reads the argument next to the helper")


def test_a_longer_helper_name_is_not_shadowed_by_a_shorter_one(expect):
    r"""`check_ratio` is a PREFIX of `check_ratio_needs_quiet_machine`.

    THE PRE-#1040 PATTERN COULD NOT READ THE LONGER ONE AT ALL, and that is what this
    holds. `check(?:_num|_ratio|_text|_timing)?\s+"` matches `check_ratio`, wants
    whitespace, finds `_needs...`, backtracks to the empty option, wants whitespace
    after `check`, and fails. Measured on the fixture below: the old form reads
    `['short']`, the current one reads `['short', 'long']`.

    WHAT THIS ARM DOES NOT HOLD, said out loud because the code reads as though it
    does: the entries are listed longest-first, and that ordering is NOT load-bearing.
    Python's `re` backtracks across alternatives, so a PURE REORDER putting
    `check_ratio` first reads both names identically -- measured, and this arm stays
    green under it. The order is the thing that looks decisive and is not; the pattern
    SHAPE is the thing that is.
    """
    import re as _re
    from compare_to_bash import _BASH_PATTERN
    src = ('\tcheck_ratio "the short one" "$a" "$b" 2\n'
           '\tcheck_ratio_needs_quiet_machine "the long one" "$a" "$b" 2\n')
    got = _re.findall(_BASH_PATTERN, src)
    expect.text(", ".join(sorted(got)), "the long one, the short one",
                "both are read; the longer name is not eaten by the shorter")


def test_the_suite_local_helpers_are_known_and_excluded(expect):
    """Four helpers are defined by ONE suite each, and the extractor does not read
    them. That is a scope decision and it is asserted rather than left implicit.

    `compare_to_bash.py` grades a `test/<stem>.sh` against a
    `test/pytest/test_<stem>.py`. None of the four suites defining its own helper
    has a pytest twin, so none is graded and the exclusion costs nothing TODAY.
    The day one of them is ported, this arm is what says the grader cannot see it.

    The population is `check_<something>`, which is not the same as "starts with
    check": `checks_in` in `decode_interrupts.sh` is a COUNTING utility returning a
    number of interrupt checks in a function body, and records nothing. It was in
    this list until the arm printed it and the definition was read.
    """
    root = HERE.parent
    local = {}
    for sh in sorted(root.glob("*.sh")):
        if sh.name == "lib.sh":
            continue
        for h in re.findall(r'^(check_[a-z_]+)\(\)\s*\{', sh.read_text(), re.M):
            local.setdefault(h, sh.name)
    expect.text(", ".join(f"{h} ({f})" for h, f in sorted(local.items())),
                "check_float (parquet_export_stats.sh), "
                "check_reconstruct (parallel_copy.sh), "
                "check_split_happened (parallel_copy.sh), "
                "check_structure (parallel_copy.sh)",
                "the suite-local helpers are exactly these four")
    twinned = [f for h, f in local.items()
               if (HERE / f"test_{f[:-3]}.py").exists()]
    expect.num(len(twinned), 0,
               "and none of their suites has a pytest twin, so none is graded today")


def test_a_suites_own_forwarding_wrapper_is_read(expect):
    """THE SUITE-LOCAL GAP (#1053). `lib.sh`'s wrappers were #1051; these are the ones
    a suite defines for itself.

        sorted_pathkeys.sh   ans() { ansp "$1" h c "$2"; }
                             ansp() { local label="$1"; check_text "$label" ...; }

    19 of that suite's 113 names go through those two, and they are not a random 19:
    the suite pairs every `check "... plans no Sort"` with an `ans "and ... still
    answers correctly"`, because losing the Sort is only correct if the rows still
    come back in that order. So the grader saw every claim about the PLAN and none
    about the ANSWER, and a port dropping all 18 answer arms would have graded
    one-for-one.
    """
    src = ('check_text() { pgc_record "$1" "$2"; }\n'
           'ansp() {\n\tlocal label="$1"\n\tcheck_text "$label" "$2" "$3"\n}\n'
           'ans() { ansp "$1" h c "$2"; }\n'
           'ans   "and returns the same rows in the same order as heap" \'SELECT 1\'\n'
           'ansp  "and it answers in j order" h c \'SELECT 2\'\n')
    forwarding, unreadable = _suite_recorders(src)
    expect.text(", ".join(f"{k}:${v}" for k, v in sorted(forwarding.items())),
                "ans:$1, ansp:$1",
                "both forwarding shapes are found -- one through a local, one a "
                "one-line body")
    expect.text(", ".join(unreadable) or "none", "none",
                "and neither is refused, because both name positions resolve")
    expect.text(", ".join(sorted(_names_in(src))),
                "and it answers in j order, and returns the same rows in the same "
                "order as heap",
                "the names are read from the CALL SITES, which is where a forwarding "
                "wrapper's names are")


def test_a_composing_wrapper_is_left_alone(expect):
    """COMPOSE IS NOT A GAP, and treating it as one was the expensive mistake.

        native_ownership.sh   refused() { check "non-owner refused: ${1%%(*}" ...; }

    `_BASH_INTERP` reduces `${1%%(*}` to `{}`, so the definition already states
    `non-owner refused: {}` -- a real template naming a real property, covering all
    nine call sites. That is why `native_ownership` grades one-for-one today.

    MEASURED BEFORE THIS ARM EXISTED: refusing on "the name slot is not a bare
    positional" refuses 32 suites, including `hilbert_cluster`, `hilbert_locality` and
    `native_ownership` -- three pairs that are COMPLETE -- to fix nothing. The refuse
    half is right in principle and, aimed at this population, it breaks green pairs.
    """
    src = ('check() { pgc_record "$1" "$2"; }\n'
           'refused() {\n\tcheck "non-owner refused: ${1%%(*}" "$2" "$3"\n}\n'
           'refused "read_projection(x)" a b\n')
    forwarding, unreadable = _suite_recorders(src)
    expect.text(", ".join(forwarding) or "none", "none",
                "a composing wrapper is not a forwarder, so its call sites are not "
                "re-read")
    expect.text(", ".join(unreadable) or "none", "none",
                "and it is NOT refused: the definition states the property")
    expect.text(", ".join(_names_in(src)), "non-owner refused: ${1%%(*}",
                "the template is read from the definition, as it always was")


def test_a_helper_whose_name_cannot_be_resolved_is_refused(expect):
    """THE REFUSE HALF. `hilbert_curve.sh` is the whole population, and it is real:

        arms_failed() {            # arms_failed "LIST" DETAIL
            while IFS= read -r _a; do
                [ -n "$_a" ] && pgc_fail "$_a" "$2"
            done <<< "$1"
        }

    The names are newline-separated INSIDE `$1` and reach `pgc_fail` through a loop
    variable, so no rule about argument positions can read them. Silently skipping is
    how 147 names in 14 suites came to be ungraded, so this names the helper and
    refuses the suite instead.
    """
    src = ('pgc_fail() { pgc_record FAIL "$1" "$1"; }\n'
           'arms_failed() {\n\tlocal _a\n\twhile IFS= read -r _a; do\n'
           '\t\t[ -n "$_a" ] && pgc_fail "$_a" "$2"\n\tdone <<< "$1"\n}\n')
    forwarding, unreadable = _suite_recorders(src)
    expect.text(", ".join(unreadable), "arms_failed",
                "a helper that reaches a recorder under an unresolvable name is "
                "refused by name")
    expect.text(", ".join(forwarding) or "none", "none",
                "and is not silently treated as a forwarder")


def test_the_refusal_names_exactly_the_suites_it_refuses(expect):
    """A STATIC GUARD NEEDS A FALSE-POSITIVE BUDGET, measured over the tree before it
    ships rather than discovered by it. One suite of 264 is refused and it is a true
    positive; every graded pair is unchanged.

    THE SET IS PINNED, NOT THE COUNT, and that is not a style choice.
    `checks_never_observed_red` is this repo's worked example of the other shape: a
    census over the tree that every legitimate addition broke, so the only way to land
    one was to raise a number the design said may only fall, which retires the guard
    the first time it is inconvenient. A count tells a reviewer that something moved.
    A set tells them WHAT, which is the difference between a diff they can judge and a
    number they can only bump.

    Asserted in both directions, so an entry cannot outlive its cause: a suite that
    starts being refused reddens with its name, and one that stops reddens too.
    """
    refused = sorted(sh.name[:-3] for sh in HERE.parent.glob("*.sh")
                     if sh.name != "lib.sh" and _suite_recorders(sh.read_text())[1])
    expect.text(", ".join(refused), "hilbert_curve",
                "exactly these suites are refused -- `hilbert_curve`, whose helpers "
                "take a newline-separated LIST of names in one argument")
    twinned = [r for r in refused if (HERE / f"test_{r}.py").exists()]
    expect.text(", ".join(twinned) or "none", "none",
                "and none of them has a pytest twin, so the refusal grades nothing "
                "today")


def test_a_bare_interpolation_is_not_published_as_a_name(expect):
    """A FORWARDING WRAPPER'S DEFINITION STATES NO PROPERTY, and publishing `{}` for it
    is worse than publishing nothing.

    `check_text "$label"` reduces to the template `{}`. Published, it sits in MISSING
    naming nothing a port could assert -- and it MATCHES a port name that is entirely
    one interpolation, which is a pass for a property neither side named. 17 were
    being published across the corpus. A wrong name is worse than an absent one, which
    is the argument #1051 turned on.
    """
    src = ('check_text() { pgc_record "$1" "$2"; }\n'
           'ansp() {\n\tlocal label="$1"\n\tcheck_text "$label" "$2" "$3"\n}\n'
           'ansp "a real property" a b\n')
    expect.num(sum(1 for n in _bash_names(src) if _template(n) == "{}"), 1,
               "premise: the raw extractor does publish a bare {} for this shape")
    expect.text(", ".join(_names_in(src)), "a real property",
                "and the reader drops it, keeping only names that state something")


def test_the_grader_itself_refuses_the_suite_it_cannot_read(expect, tmp_path):
    """THE REFUSAL RUNS, not just the classifier that feeds it.

    `_suite_recorders` returning an unreadable helper is asserted above. That is not
    the same as `main` acting on it, and the difference is the whole value of the
    refuse half: a classifier nobody consults is a list. MEASURED -- disabling the
    refusal in `main` reddened no arm at all until this one existed, which is the same
    defect the refusal exists to prevent, one level up.

    THE PORT SIDE IS A FIXTURE, not one of this tree's real ports. Naming a real
    `test_*.py` here made `test_harness_deps.py` classify THIS file as cluster-bound,
    because a file that drives a cluster-bound file needs whatever that file needs --
    and it was right to. The arm is about the grader's refusal, not about any port, so
    it supplies its own.
    """
    import io, contextlib
    from compare_to_bash import main

    port = tmp_path / "test_stub.py"
    port.write_text("def test_x(expect):\n    expect.num(1, 1, 'a name')\n")

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = main(str(HERE.parent / "hilbert_curve.sh"), str(port))
    text = out.getvalue()
    expect.num(rc, 2, "the grader exits 2 -- neither a pass nor an ordinary MISSING -- "
                      "on a suite whose names it cannot fully read")
    expect.num(1 if "REFUSED" in text else 0, 1, "and says so")
    expect.num(1 if "arms_failed" in text and "arms_unrunnable" in text else 0, 1,
               "naming both helpers, so the reader knows what to change")
    expect.num(1 if "missing:" in text else 0, 0,
               "and prints NO verdict, because a verdict about a partly-read suite is "
               "the thing this refuses to produce")

    # THE CONTROL: a suite it CAN read still grades, so the refusal is about the
    # unreadable helper and not about every foreign suite.
    readable = tmp_path / "readable.sh"
    readable.write_text('check "a property the port also asserts" "$a" "$b"\n')
    port.write_text("def test_x(expect):\n"
                    "    expect.num(1, 1, 'a property the port also asserts')\n")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = main(str(readable), str(port))
    expect.num(rc, 0, "control: a readable suite still grades, and grades clean")
    expect.num(1 if "missing:" in out.getvalue() else 0, 1,
               "and DOES print a verdict, so the refusal above is the difference")


def test_a_helper_reaching_only_the_primitive_is_found(expect):
    """THE SEED IS `pgc_record`, NOT THE `check` FAMILY, and four suites turn on it.

        phase4.sh::assert_plan   reaches: pgc_record
        phase5.sh::assert_plan   reaches: pgc_record
        audit.sh::expect_error   reaches: pgc_record

    None of them ever calls a `check_*` helper. A closure seeded from the check family
    cannot see them, and seeding from the primitive is the difference between 145
    names read and 89 -- measured against a second implementation that seeded from
    `check*` and came up 56 short across exactly these four suites.

    This is the direct-call-versus-closure error one level up: closing over `check*`
    without closing over the thing `check*` itself closes over.
    """
    src = ('pgc_record() { PGC_CHECKS=$((PGC_CHECKS + 1)); }\n'
           'assert_plan() {\n\tlocal name="$1"\n'
           '\tpgc_record PASS "$name" "PASS  $name"\n}\n'
           'assert_plan "the plan has no Sort" "$(plan)"\n')
    forwarding, unreadable = _suite_recorders(src)
    expect.text(", ".join(f"{k}:${v}" for k, v in sorted(forwarding.items())),
                "assert_plan:$1",
                "a helper that reaches ONLY the primitive is still a recorder")
    expect.text(", ".join(_names_in(src)), "the plan has no Sort",
                "and its call sites are read")

    # AND ON THE REAL TREE, because the fixture proves the rule and not the corpus.
    real = _suite_recorders((HERE.parent / "phase4.sh").read_text())[0]
    expect.num(1 if "assert_plan" in real else 0, 1,
               "phase4.sh's assert_plan is found in the tree, not only in a fixture")


def test_every_pair_in_the_tree_is_declared(expect):
    """THE DECLARATION IS ASSERTED IN BOTH DIRECTIONS (#1046).

    The standing arm below grades the pairs it is GIVEN. Until this arm existed, a pair
    that existed and was not given to it was not graded, and nothing said so: the arm
    passed, grading the ones it knew about, and reported a clean verdict for a tree it
    had not fully looked at. **Absent-from-the-list and no-gap-found produced the same
    green.**

    Latent rather than live throughout: the declared set happened to equal the tree, so
    nothing had ever been silently ungraded. It would have gone live the moment a ninth
    pair landed undeclared, which is a thing a porter does by forgetting one line.

    BOTH DIRECTIONS, for the reason `SHELL_REFERENCES` gives in `test_harness_deps.py`:
    a declaration asserted one way rots into a permanent exemption. A pair that exists
    and is not declared reddens; a stem declared for a pair that has been deleted
    reddens too.

    A PAIR IS `test_<stem>.py` BESIDE `test/<stem>.sh`, derived from the tree rather
    than listed. 24 pytest files have no matching suite -- the harness's own guards --
    and are correctly not pairs; deriving the population is what keeps them out without
    a second exemption list to maintain.
    """
    root = HERE.parent.parent
    exist = {p.name[5:-3] for p in HERE.glob("test_*.py")
             if (root / "test" / f"{p.name[5:-3]}.sh").exists()}
    expect.at_least(len(exist), 8, "premise: the tree has pairs to find, derived not listed")

    declared = set(COMPLETE) | set(INCOMPLETE)
    expect.num(len(declared), len(COMPLETE) + len(INCOMPLETE),
               "premise: no stem is both complete and incomplete")

    undeclared = sorted(exist - declared)
    phantom = sorted(declared - exist)
    expect.text(", ".join(undeclared) or "none", "none",
                "every pair in the tree is declared, so none is silently ungraded")
    expect.text(", ".join(phantom) or "none", "none",
                "and every declared stem is a pair that exists, so the list cannot rot")

    # A DECLARED GAP MUST CARRY ITS REASON, or `INCOMPLETE` becomes a way to drop a pair
    # out of grading by naming it. The standing arm does not grade these, so the reason
    # is the only thing between a declared gap and a forgotten one.
    thin = sorted(k for k, v in INCOMPLETE.items() if len(v.strip()) < 20)
    expect.text(", ".join(thin) or "none", "none",
                "every incomplete pair says why, at more than a passing word")


def test_the_ported_suites_in_this_tree_are_graded_one_for_one(expect):
    """THE STANDING ARM, and the reason this file is not only about fixtures.

    A guard over invented sources proves the extractor reads python. It cannot prove the
    tool grades THIS tree, which is the claim #432 rests on. So the pairs that are declared
    complete are asserted complete here, and a later edit that breaks parity fails with the
    pair named rather than the whole gate going red for an unrelated reason.

    Only the pairs that reach zero today are listed. A pair with a real gap is not pinned to
    its gap: that would turn the gap into the expected state.

    THE LIST IS HAND-WRITTEN FOR THAT REASON AND NOTHING ENFORCES IT, which is a
    different thing from the reason being wrong. The comment below already says a new
    port belongs here; no arm reddens when one does not arrive. Today the list happens
    to equal the pairs that exist, so nothing has ever been silently ungraded -- but an
    eighth complete pair omitted would leave this arm passing while it graded seven,
    which is absent-from-the-list and no-gap-found producing the same green.

    #1046 TRACKS MAKING THIS ASSERTION TWO-DIRECTIONAL, in the shape `SHELL_REFERENCES`
    already uses: derive the pairs that EXIST and require the declared list to equal that
    set. It is not a tidy-up -- it changes what happens to an INCOMPLETE port, which is
    quietly absent today and would have to redden, so the issue records the design
    question rather than settling it. Kept out of the PR that added the eighth pair.
    """
    from compare_to_bash import main
    import contextlib
    import io

    root = HERE.parent.parent
    complete = COMPLETE
    # A CARDINALITY PREMISE, required because the list moved to module scope in #1046 and
    # this loop now iterates a DERIVED name rather than a literal spelled here. An empty
    # or truncated COMPLETE would leave every arm below unrun and the verdict comparison
    # trivially equal -- the whole arm passing over nothing. The sweep in
    # `test_loop_coverage_premise.py` caught the omission the moment the list was hoisted.
    #
    # IT COUNTS THE DECLARED TOTAL AGAINST THE PAIRS THAT EXIST, not `COMPLETE` against a
    # number. The first version was `at_least(len(complete), 8)` and it broke the escape
    # hatch this change exists to provide: moving one stem to INCOMPLETE takes
    # `len(COMPLETE)` to 7 and reddened the suite, so a pair could not be declared
    # incomplete without going red. @OffgridwithJD caught it; my proof that the hatch
    # worked had been run BEFORE this premise was added and never re-run against the file
    # that shipped.
    #
    # The floor was also a hard-coded 8 needing an edit the first time a ninth pair lands
    # -- the budget shape #982 argues against. Derived, it needs none.
    declared_total = len(complete) + len(INCOMPLETE)
    pairs_on_disk = {q.name[5:-3] for q in HERE.glob("test_*.py")
                     if (root / "test" / f"{q.name[5:-3]}.sh").exists()}
    expect.num(declared_total, len(pairs_on_disk),
               "premise: every pair on disk is declared somewhere, so this loop and the "
               "INCOMPLETE list account for all of them")
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
