"""An assertion that collapses a measurement to a constant cannot say what failed.

    expect.num(1 if abs(on - off) <= 5 else 0, 1, "...")   ->   got 0 want 1

Four measured buffer counts, reduced to a boolean BEFORE the assert. The failure
message carries none of them, so a red says only that the arm failed -- which the
word FAILED already said.

WHAT IT COST, MEASURED RATHER THAN IMAGINED (#1164). A PG 15 leg reddened
`test_a_query_that_cannot_use_the_order_does_not_pay_to_decide`, and two sessions
spent an afternoon unable to say whether the ORACLE (`|on - off| <= 5`, a six-buffer
spurious gap) or the CONTROL (`order_on > order_off + 5`, a fourteen-buffer collapse)
had failed. Those are different defects with different owners -- one of them a
planning-buffer question and the other a timing question -- and the run could not tell
them apart. Four further experiments were then aimed at a target whose identity was
unknown.

TWO BUGS, ONE SYMPTOM, and the second is the one this file is about. The runner also
captured each leg into a shell variable and grepped it for summary lines, so the full
output never reached disk. That was repaired separately. **Repairing it alone would
still have left `got 0 want 1`**, because the measurement was discarded before the
message was built. A complete log of an empty sentence is still empty.

THE RULE, and it is narrower than "do not use a conditional expression".

An `IfExp` first argument is refused when BOTH of these hold:

  (a) the failing set has MORE THAN ONE MEMBER, so the boolean does not determine
      which state was reached; AND
  (b) NEITHER BRANCH carries the value, so the message cannot recover it.

`is None`, `is not None`, `in`, `==` and `!=` are determinate: knowing the boolean
already names the failing state. So is a nothing-versus-something comparison -- `x > 0`,
`n >= 1` -- because exactly one value fails, and `got 'no' want 'yes'` fixes it.

**Nothing-versus-something is determinate WHATEVER THE LEFT SIDE LOOKS LIKE.** That
was learned the expensive way: an earlier draft keyed on the left operand being a bare
`Name`, which flagged `(st['ret'] or 0) > 0` and `st['anylog'] >= 1`. The `or 0` is a
None-guard and the subscript is a lookup; neither is a measurement, and refusing them
was syntax standing in for semantics.

THE ONE EXCEPTION, which is why the carve-out cannot be "the comparator is 0 or 1":

    'yes' if min(t_proj_run, l_proj_run, t_base_run, l_base_run) > 0 else 'no'

The comparator IS 0, and the boolean still hides WHICH of four measurements went
non-positive. An aggregate over several operands is a measurement however it is
compared, so it stays in scope.

A TEST MAY BE A BOOLEAN COMBINATION, and both reviewers' first implementations got
this right or wrong by accident rather than by design. One bailed on anything that was
not a `Compare`, and silently scored `rng > 1000000000 and spread < 100000` as
determinate. The other walked the whole tree for comparisons, which reached inside
`BoolOp` for the wrong reason. It is stated here as a rule: every operand of a boolean
combination is examined, and one lossy operand is enough.

WHY THIS AND NOT A PARITY CHECK. `compare_to_bash` grades the check NAME. Two arms
grade `missing: 0` while one prints its measurement and the other prints `got 0 want 1`,
and nothing in the harness can see the difference -- which is how two of the four pairs
repaired under #1164 turned out to be PORT REGRESSIONS, where the shell original had
carried its numbers all along and the port dropped them. Name parity does not preserve
diagnostics, and no comparison of two independent implementations ever will. This
refuses the shape directly instead.

THE POPULATION, measured over the corpus before this was written rather than after:

    expect.*(<IfExp>, ...)                  190
      + both branches constant              148
        + a LOSSY test                       26   <- refused

The 26 were repaired under #1164, so this arm reaches zero on arrival. That is the
point rather than a weakness: the property is true today and nothing was holding it
there. `test_a_lossy_arm_is_caught` is what says the sweep can still see one.

THE SHELL CORPUS IS A DIFFERENT NUMBER AND ITS GUARD IS SHAPED DIFFERENTLY. Its twin
-- `test/selftest/540-an-arm-must-carry-its-measurement.sh` -- asserts a tracked list,
`test/lossy_arms.tsv`, that may only shrink, not zero. The asymmetry is deliberate: the
two corpora are in different states, and a guard that asserted zero over both would be
claiming a property this tree does not have.

NO COUNT OF THAT LIST IS REPEATED HERE. This paragraph carried one -- "55 such sites
across 36 files" -- which was three drafts out of date by the time the branch was
pushed, while the two places that state the figure (the shell header and TESTS.md) had
been updated to 77 rows across 52 suites. A number in a third file is a number nobody
updates. The list is the count.

WHAT THIS DOES NOT CATCH, MEASURED RATHER THAN ASSUMED. Each was planted against the
real sweep and then counted in the corpus; none is live, and each is left open for a
reason rather than by oversight.

  AN AGGREGATE THAT IS NOT SPELLED `min`, `max` OR `sum`:

      'yes' if sorted(runs)[0] > 0 else 'no'      MISSED
      'yes' if min(runs) > 0 else 'no'            caught

  `sorted(runs)[0]` is `min(runs)` written differently, so the aggregate rule is
  spelling-sensitive. It is the same shape as `any(r <= 0 for r in runs)` from the round
  the walk closed, and the argument that closed that one does not carry here: an author
  told "your `min` arm is refused" reaches for `any()`, not for a subscript of a sort.
  Closing it needs either a longer list of names -- which the next spelling escapes
  again -- or reasoning about what indexing a sorted sequence means, which is type
  information this has no access to. TWO subscript-of-a-call conditionals exist in the
  corpus, `test_failed_query_sentinel.py:322` and `test_raises_sqlstate.py:869`, and
  both are `str.split(...)[0]` under `==` or `not in`, so both are determinate and
  correctly unflagged. Found by @OffgridwithJD.

  A BRANCH THAT IS A NAMED CONSTANT reads as carrying, because `_branch_carries` asks
  only whether the branch is a bare `Constant`:

      KEPT, HALVED = 'io-kept', 'halved'
      expect.text(KEPT if ratio < 1.35 else HALVED, KEPT, 'n')      MISSED

  Four `Name`/`Attribute` branches sit in `args[0]` conditionals today -- `bounds`,
  `wrote`, `old` and `proj_err.sqlstate` -- and every one of them is on a determinate
  test (`is not None`, `in`, a truthiness check), so tightening would not flag any of
  them. What it would flag is `test_parallel_am_scan.py:225`, whose `else` branch is a
  nested conditional ending in an f-string that does carry the number. So the rule "a
  branch that is not a bare constant carries" is right for this tree, and the shape that
  defeats it is one nobody writes.

  ONLY `args[0]` IS EXAMINED. Three conditionals sit in a later argument position --
  `test_projection_drop_column.py:119` and `test_projection_privilege.py:185` and `:191`
  -- and all three build a check NAME rather than a measured value. Scanning `args[1:]`
  would refuse three correct sites to catch a shape that does not occur.
"""

import ast
import pathlib

HERE = pathlib.Path(__file__).resolve().parent

# Comparisons whose failing state the boolean already names.
DETERMINATE_OPS = (ast.Is, ast.IsNot, ast.In, ast.NotIn, ast.Eq, ast.NotEq)
ORDERING_OPS = (ast.Lt, ast.LtE, ast.Gt, ast.GtE)


def _counted(node):
    """An `expect.<assertion>(...)` call. `cannot_run` declares a test unrunnable
    rather than concluding anything, so it is not an assertion to police."""
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "expect"
            and node.func.attr != "cannot_run")


def _aggregates(node):
    """`min(a, b, c)` and friends: one number standing for several measurements.

    The boolean hides WHICH operand failed, so these stay in scope even when the
    comparator is 0 -- which is the case a comparator-only carve-out gets wrong.

    ARGUMENT COUNT IS NOT OPERAND COUNT, and an earlier draft keyed on the wrong
    one (`len(node.args) > 1`). `min(runs)` takes ONE argument and aggregates a
    whole sequence -- `min` of a scalar is a TypeError, so a one-argument call is
    iterable by construction and hides exactly as much as the spelled-out form.
    Caught by @OffgridwithJD, who also noticed the control below had been passing
    for a reason the code did not give.
    """
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in {"min", "max", "sum"} and len(node.args) >= 1)


# Mirroring an ordering operator, so `0 < r` and `r > 0` are one rule rather
# than two special cases.
_MIRROR = {ast.Lt: ast.Gt, ast.Gt: ast.Lt, ast.LtE: ast.GtE, ast.GtE: ast.LtE}


def _pair_is_lossy(lhs, op, rhs):
    """One (left, operator, right) triple of a comparison."""
    if isinstance(op, DETERMINATE_OPS):
        return False
    if not isinstance(op, ORDERING_OPS):
        return False
    # WRITTEN EITHER WAY ROUND. `0 < r` is `r > 0` with the operands swapped, and
    # keying the carve-out on the right-hand side alone refused it -- a FALSE
    # POSITIVE ON CORRECT CODE, which is the failure that gets a guard switched
    # off and takes its rule with it. Mirror once, then apply one rule; a second
    # special case would have to be kept in step with the direction logic below.
    if isinstance(lhs, ast.Constant) and not isinstance(rhs, ast.Constant):
        lhs, op, rhs = rhs, _MIRROR[type(op)](), lhs
    # Nothing versus something -- `x > 0`, `n >= 1` -- where exactly one value
    # fails, so the boolean already fixes it. Keyed on the COMPARATOR, never on
    # the shape of the left side.
    #
    # THE DIRECTION IS PART OF THE CARVE-OUT and a refactor dropped it once. Only
    # `>` and `>=` are nothing-versus-something. `r <= 0` against the same literal
    # fails for EVERY POSITIVE r, which is a set rather than a value, so it hides
    # `r` and stays in scope. Caught by `any(r <= 0 for r in runs)` reading as
    # determinate -- a false negative introduced while fixing a different one.
    # EXACTLY `> 0` AND `>= 1`, not "an ordering operator against 0 or 1". Both
    # spellings fail at exactly one value, which is what makes the boolean
    # sufficient. Nothing else does, and the looser form let two shapes through:
    #
    #   r > 1    fails at 0 AND 1 -- a set, so it hides r
    #   r >= 0   cannot fail at all for a count -- a vacuous arm, not a premise
    #
    #
    # AND `r >= 0` IS CAUGHT FOR A REASON THAT DEPENDS ON THE OPERAND. Over a
    # COUNT it cannot fail at all -- a vacuous arm wearing a premise's name, which
    # is a different defect from hiding an operand. Over a SIGNED value it is an
    # ordinary test that fails for every negative r, which is a set, so it hides r
    # in the usual way. An AST sweep cannot tell the two apart, so it is refused
    # either way and the message names the second reading. For the count case that
    # diagnosis is imprecise: a reader following it would add the value to the
    # branch and still have an arm that cannot fail. Recorded rather than fixed --
    # distinguishing them needs type information this has no access to, and an arm
    # that guessed would be worse than a message that is occasionally imprecise.
    # Neither was in the corpus and neither was reported; they were found by
    # driving the boundary after @OffgridwithJD's mirror finding sent me back to
    # it. A carve-out is a claim like any other and this one was not measured.
    if ((isinstance(op, ast.Gt) and isinstance(rhs, ast.Constant) and rhs.value == 0)
            or (isinstance(op, ast.GtE) and isinstance(rhs, ast.Constant)
                and rhs.value == 1)):
        return _aggregates(lhs)
    return True


def _comparison_is_lossy(test):
    """True when the boolean discards an operand the reader would need.

    EVERY PAIR OF A CHAINED COMPARISON IS EXAMINED. An earlier draft opened with
    `len(test.ops) == 1` and scored `0 < sel < 20000` as determinate -- a shape
    that hides `sel` and has many failing states. Two live chained comparisons
    exist in this corpus and both are saved by an f-string on the other branch,
    so neither was a live miss; but the guard was not what protected them, and
    deleting either f-string would have made the arm undiagnosable in silence.
    """
    if not isinstance(test, ast.Compare):
        return False
    left = test.left
    for op, right in zip(test.ops, test.comparators):
        if _pair_is_lossy(left, op, right):
            return True
        left = right
    return False


def _is_lossy(test):
    """Lossy if ANY comparison anywhere inside the test is lossy.

    EVERY COMPARISON, WHEREVER IT SITS, and this is a walk rather than a list of
    node types on purpose. An earlier draft handled `BoolOp` and `Compare` and
    returned False for everything else -- a FAIL-OPEN default, which scored a
    comparison wrapped in anything at all as determinate:

        'yes' if any(r <= 0 for r in runs) else 'no'      MISSED
        'yes' if all(r > t for r in runs) else 'no'       MISSED
        1 if not abs(on - off) > 5 else 0                 MISSED

    **`any(r <= 0 for r in runs)` is the natural rewrite of `min(runs) > 0`.** So
    the shape the aggregate rule refuses had an escape hatch one keystroke away,
    and an author told "your `min` arm is refused" is likely to reach for exactly
    it. @OffgridwithJD found all three by planting them, and made that argument:
    close it for the evasion path, not because any site is live. None was --
    three `any`/`all` sites and eight negations in the corpus, every one either
    determinate or already carrying.

    The justification for walking is that **the boolean collapses the WHOLE
    expression**, however deeply a comparison is nested inside it, so nesting
    cannot make a hidden operand recoverable.
    """
    return any(_comparison_is_lossy(n) for n in ast.walk(test)
               if isinstance(n, ast.Compare))


def _branch_carries(node):
    """A branch that is not a bare constant carries the value into the message.

    An f-string does; a descriptive word like `'halved'` does not. Descriptive is
    not the same as carrying: `got 'halved' want 'io-kept'` tells the reader the
    verdict they already had from the word FAILED, and not the ratio behind it.
    """
    return not isinstance(node, ast.Constant)


def collapsing_arms(directory, classified=None):
    """-> ["file:line  <test source>", ...] for every arm that discards its measurement.

    `classified`, when given, collects "file:line" for every conditional arm the sweep
    examined, so the coverage premise below can count the sweep's own work rather than
    a second walk of its own that a broken sweep would not share (#1339)."""
    out = []
    for path in sorted(pathlib.Path(directory).glob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for call in [n for n in ast.walk(tree) if _counted(n)]:
            if not call.args or not isinstance(call.args[0], ast.IfExp):
                continue
            arg = call.args[0]
            if classified is not None:
                classified.append(f"{path.name}:{call.lineno}")
            if _branch_carries(arg.body) or _branch_carries(arg.orelse):
                continue
            if _is_lossy(arg.test):
                out.append(f"{path.name}:{call.lineno}  {ast.unparse(arg.test)[:60]}")
    return out


def test_no_arm_collapses_its_measurement_to_a_constant(expect):
    """The corpus itself. Green on arrival, and that is the point."""
    offenders = collapsing_arms(HERE)
    expect.text(", ".join(offenders) or "none", "none",
                "no assertion discards its measurement before comparing")


def test_the_sweep_finds_the_arms_it_is_meant_to_classify(expect):
    """The coverage premise this file's own sweep needs.

    A sweep that parses nothing reports no offenders, which is exactly what a clean
    corpus reports. So count what it CLASSIFIED, not only what it rejected -- the
    same shape `test_loop_coverage_premise` needs for the same reason.
    """
    # THE SWEEP'S OWN COUNT. This walked the corpus a second time with its own glob
    # and its own filter, so a sweep that examined nothing -- its glob broken, say --
    # still met the premise while reporting "none" for the corpus (#1339).
    classified = []
    collapsing_arms(HERE, classified)
    expect.at_least(len(classified), 100,
                    "premise: the sweep found conditional arms to classify")


def _fixture(tmp_path, body):
    d = tmp_path / "corpus"
    d.mkdir(exist_ok=True)
    (d / "test_probe.py").write_text(body)
    return d


def test_a_lossy_arm_is_caught(tmp_path, expect):
    """The removal proof, with its control beside it.

    The fixture is the real shape with one property removed, rather than an empty
    file: an empty file is caught by a sweep that does nothing at all.
    """
    lossy = _fixture(tmp_path, "def test_x(expect):\n"
                               "    expect.num(1 if abs(on - off) <= 5 else 0, 1, 'n')\n")
    expect.num(len(collapsing_arms(lossy)), 1,
               "a tolerance collapsed to a boolean is caught")

    carried = _fixture(tmp_path, "def test_x(expect):\n"
                                 "    expect.text(f'differs by {d}' if d > 5 else 'within 5',\n"
                                 "                'within 5', 'n')\n")
    expect.num(len(collapsing_arms(carried)), 0,
               "control: an arm whose branch carries the value is not caught")


def test_a_determinate_arm_is_not_caught(tmp_path, expect):
    """The carve-out, driven rather than described.

    Each of these has exactly one failing state, so the boolean already names it.
    Without this the rule would refuse most of the corpus and be switched off.
    """
    for src, why in (
        ("1 if rows > 0 else 0", "nothing versus something"),
        ("1 if n >= 1 else 0", "the same, spelled with a floor"),
        ("1 if (st['ret'] or 0) > 0 else 0", "a None-guard is not a measurement"),
        ("1 if st['anylog'] >= 1 else 0", "a subscript is a lookup, not a measurement"),
        ("1 if exc is not None else 0", "identity"),
        ("1 if 'wrote' in buckets else 0", "membership"),
    ):
        d = _fixture(tmp_path, f"def test_x(expect):\n    expect.num({src}, 1, 'n')\n")
        expect.num(len(collapsing_arms(d)), 0, f"determinate, so not caught: {why}")


def test_an_aggregate_over_several_operands_stays_in_scope(tmp_path, expect):
    """The exception that stops the carve-out being 'the comparator is 0 or 1'.

    `min(a, b, c, d) > 0` compares against 0 and still hides which of four
    measurements went non-positive.
    """
    d = _fixture(tmp_path, "def test_x(expect):\n"
                           "    expect.text('yes' if min(a, b, c, d) > 0 else 'no', 'yes', 'n')\n")
    expect.num(len(collapsing_arms(d)), 1,
               "an aggregate over several operands is lossy even against 0")

    # ARGUMENT COUNT IS NOT OPERAND COUNT. `min(runs)` takes one argument and
    # aggregates a sequence -- `min` of a scalar raises -- so it hides exactly as
    # much as the spelled-out form and must be caught too.
    one_arg = _fixture(tmp_path, "def test_x(expect):\n"
                                 "    expect.text('yes' if min(runs) > 0 else 'no', 'yes', 'n')\n")
    expect.num(len(collapsing_arms(one_arg)), 1,
               "a single-argument min aggregates a sequence, so it is lossy too")

    # EVERY AGGREGATE THE RULE NAMES, not min alone: a classifier that lost max or
    # sum from its set passed this test while min stayed (#1339).
    for agg in ("max", "sum"):
        d = _fixture(tmp_path, "def test_x(expect):\n"
                               f"    expect.text('yes' if {agg}(a, b, c, d) > 0 else 'no', 'yes', 'n')\n")
        expect.num(len(collapsing_arms(d)), 1,
                   f"{agg} over several operands is lossy against 0 as well")

    plain = _fixture(tmp_path, "def test_x(expect):\n"
                               "    expect.text('yes' if rows > 0 else 'no', 'yes', 'n')\n")
    expect.num(len(collapsing_arms(plain)), 0,
               "control: a bare name against 0 is nothing-versus-something, not an aggregate")


def test_a_chained_comparison_is_examined_pair_by_pair(tmp_path, expect):
    """`0 < sel < 20000` hides `sel` and has many failing states.

    An earlier draft opened with `len(test.ops) == 1` and scored every chained
    comparison determinate. Two exist in this corpus; both are saved by an f-string
    on the other branch, so neither was a live miss -- but the guard was not what
    protected them.
    """
    chained = _fixture(tmp_path, "def test_x(expect):\n"
                                 "    expect.num(1 if 0 < sel < 20000 else 0, 1, 'n')\n")
    expect.num(len(collapsing_arms(chained)), 1,
               "a chained comparison is examined pair by pair")

    det = _fixture(tmp_path, "def test_x(expect):\n"
                             "    expect.num(1 if a is not None is not b else 0, 1, 'n')\n")
    expect.num(len(collapsing_arms(det)), 0,
               "control: a chain of determinate operators stays determinate")


def test_the_carve_out_is_driven_at_its_boundary(tmp_path, expect):
    """`> 0` and `>= 1` exactly, written either way round.

    The carve-out is a claim like any other and an earlier draft had not measured
    it: `rhs.value in (0, 1)` for either operator let `r > 1` through (it fails at
    0 AND 1, a set) and `r >= 0` too (it cannot fail at all). Both are driven here
    so the next person changing this rule finds out immediately.

    The mirrored spellings are the other half. `0 < r` is `r > 0` with the operands
    swapped, and refusing it was a FALSE POSITIVE ON CORRECT CODE -- the failure
    that gets a guard switched off and takes its rule with it.
    """
    cases = (
        ("1 if r > 0 else 0", 0, "nothing versus something"),
        ("1 if r >= 1 else 0", 0, "the same, spelled with a floor"),
        ("1 if 0 < r else 0", 0, "mirrored: 0 < r is r > 0"),
        ("1 if 1 <= r else 0", 0, "mirrored: 1 <= r is r >= 1"),
        ("1 if r > 1 else 0", 1, "fails at 0 AND 1, so it hides r"),
        ("1 if r >= 0 else 0", 1, "cannot fail at all, so it is not a premise"),
        ("1 if 1 < r else 0", 1, "mirrored: 1 < r is r > 1, still lossy"),
    )
    for src, want, why in cases:
        d = _fixture(tmp_path, f"def test_x(expect):\n    expect.num({src}, 1, 'n')\n")
        expect.num(len(collapsing_arms(d)), want, f"boundary: {why}")


def test_a_comparison_wrapped_in_anything_is_still_examined(tmp_path, expect):
    """The fail-open default, closed.

    An earlier draft handled `BoolOp` and `Compare` and returned False for every
    other node type, so a comparison inside a generator or behind a `not` was
    scored determinate. `any(r <= 0 for r in runs)` is the natural rewrite of
    `min(runs) > 0`, so refusing the aggregate while passing this one leaves the
    escape hatch next to the door.
    """
    for src, why in (
        ("'yes' if any(r <= 0 for r in runs) else 'no'", "a generator inside any()"),
        ("'yes' if all(r > t for r in runs) else 'no'", "a generator inside all()"),
        ("1 if not abs(on - off) > 5 else 0", "a negated comparison"),
    ):
        d = _fixture(tmp_path, f"def test_x(expect):\n    expect.text({src}, 'yes', 'n')\n")
        expect.num(len(collapsing_arms(d)), 1, f"lossy however it is wrapped: {why}")

    # THE CONTROL, so the walk is not simply flagging everything it can reach: a
    # determinate comparison stays determinate at any depth.
    ok = _fixture(tmp_path, "def test_x(expect):\n"
                            "    expect.text('yes' if any(r is None for r in runs) else 'no', 'no', 'n')\n")
    expect.num(len(collapsing_arms(ok)), 0,
               "control: a determinate comparison is determinate however it is wrapped")


def test_a_boolean_combination_is_examined_operand_by_operand(tmp_path, expect):
    """One lossy operand is enough, and a determinate one does not excuse it.

    Both reviewers' first implementations got this right or wrong by accident: one
    bailed on anything that was not a `Compare`, the other reached inside `BoolOp`
    while looking for something else.
    """
    mixed = _fixture(tmp_path, "def test_x(expect):\n"
                               "    expect.num(1 if req is not None and req >= 2 else 0, 1, 'n')\n")
    expect.num(len(collapsing_arms(mixed)), 1,
               "a determinate operand does not excuse a lossy one beside it")

    both_ok = _fixture(tmp_path, "def test_x(expect):\n"
                                 "    expect.num(1 if a is not None and b > 0 else 0, 1, 'n')\n")
    expect.num(len(collapsing_arms(both_ok)), 0,
               "control: a combination of determinate operands is determinate")
