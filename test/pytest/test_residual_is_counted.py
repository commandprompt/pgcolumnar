"""A residual must be counted from the names, never derived by subtraction.

#999 and #1006, filed independently by both sessions off the same runs. Every PG 17
matrix report on `main` printed a count that cannot exist::

    suites that ran: 243 of 252 (skipped: 9, incomplete: 0)
    of those, 248 accounted for their checks and -5 did not

Minus five suites. PG 18 printed -2 the same day, from 246 ran.

THE TWO TERMS COUNT DIFFERENT POPULATIONS. ``suites_ran`` excludes a skipped suite;
``_acc_any`` counts every registered suite whose LOG shows an accounting line, and a
skipped suite still prints one, because ``pgc_summary`` emits it on every exit path
before it decides the status. Subtracting one from the other goes negative as soon as
any suite skips and accounts.

WHY NOTHING CAUGHT IT. The residual was DERIVED, so the partition closes
arithmetically for any inputs: 248 + (-5) = 243. An ``inputs == sum(buckets)`` arm
passes on that line whatever the numbers are. It is the error ``pgc_summary`` warns
about eight lines below its own counter, committed one level up.

These tests drive the SHELL out of `run_all_versions.sh` rather than reimplementing
it in Python. A Python twin would be a second implementation and would agree with
itself; the house rule asks for two observers of one implementation. The bash-side
part, `test/selftest/510-a-residual-must-be-counted.sh`, asserts the same properties
through its own harness and neither file names the other.
"""

import pathlib
import re
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[2]
RUNNER = REPO / "test" / "run_all_versions.sh"

READERS = ("pgc_ran_without_accounting", "pgc_accounted_among")


def _extract(name):
    """The text of one shell function, taken from the runner itself."""
    out, keep = [], False
    for line in RUNNER.read_text().splitlines():
        if line.startswith(f"{name}() "):
            keep = True
        if keep:
            out.append(line)
            if line == "}":
                break
    return "\n".join(out)


def _summary_block():
    """The summary's residual block, taken from the runner rather than retyped.

    A retyped block is a second implementation and agrees with itself, which is
    exactly what these tests must not do.
    """
    out, keep, saw_end = [], False, False
    for line in RUNNER.read_text().splitlines():
        if '_acc_debt="$(pgc_ran_without_accounting' in line:
            keep = True
        if keep:
            out.append(line)
            if "cannot happen" in line:
                saw_end = True
            elif saw_end and line == "\tfi":
                break
    return "\n".join(out)


def _sh(script):
    """Run a bash script under the shell options the harness itself uses."""
    r = subprocess.run(["bash", "-c", "set -uo pipefail\n" + script],
                       capture_output=True, text=True)
    return r.stdout, r.returncode


def _call(name, *args):
    body = "\n".join(_extract(n) for n in READERS)
    return _sh(body + "\n" + " ".join([name] + [f'"{a}"' for a in args]) + "\n")


def _write(tmp_path, name, *names):
    p = tmp_path / name
    p.write_text("".join(n + "\n" for n in names))
    return str(p)


def _live_shape(tmp_path):
    """THE LIVE SHAPE, written once for every arm that reads it. Three suites ran;
    five accounted, because two that SKIPPED also printed an accounting line. This is
    243-vs-248 in miniature, the input that made the old expression print a negative.
    -> (ran, skipped, acc), each a file of names."""
    return (_write(tmp_path, "ran", "alpha", "beta", "gamma"),
            _write(tmp_path, "skipped", "delta", "eps"),
            _write(tmp_path, "acc", "alpha", "beta", "gamma", "delta", "eps"))


def _lines(path):
    return sum(1 for ln in pathlib.Path(path).read_text().splitlines() if ln)


# ---- the readers ------------------------------------------------------------


def test_the_residual_names_the_suites_that_ran_and_did_not_account(tmp_path, expect):
    """The debt is a set of names, and the names are what a reader acts on.

    An empty residual and an ABSENT reader print the same thing -- `command not
    found` writes nothing and a clean residual writes nothing -- so the empty case
    is never asserted alone in this file.
    """
    ran = _write(tmp_path, "ran", "alpha", "beta", "gamma")
    acc = _write(tmp_path, "acc", "alpha")

    out, rc = _call("pgc_ran_without_accounting", ran, acc)
    expect.num(rc, 0, "the reader exits clean")
    expect.text(out.split(), ["beta", "gamma"],
                "the two suites that ran without accounting are named")


def test_a_skipped_suite_that_accounted_does_not_become_a_debt(tmp_path, expect):
    """THE LIVE SHAPE, which is 243-vs-248 in miniature.

    Three suites ran and all three accounted; two more SKIPPED and accounted as
    well, so the accounted set is a strict superset of the ran set. This is the
    input on which the old expression printed a negative.
    """
    ran, skipped, acc = _live_shape(tmp_path)

    debt, _ = _call("pgc_ran_without_accounting", ran, acc)
    named, _ = _call("pgc_ran_without_accounting", ran, _write(tmp_path, "acc2", "alpha"))

    # Asserted as a PAIR. A reader that does not exist satisfies the empty half and
    # fails this, which is how the bash twin's first draft passed while its subject
    # was undefined.
    expect.text((debt.split(), named.split()), ([], ["beta", "gamma"]),
                "the live shape is empty while the same reader still finds a real debt")

    skipacc, _ = _call("pgc_accounted_among", skipped, acc)
    expect.text(skipacc.split(), ["delta", "eps"],
                "and the skipped suites that accounted are their own named category")


def test_the_old_subtraction_goes_negative_on_that_same_input(tmp_path, expect):
    """The control that makes the arm above mean something.

    Without this, "the residual is empty" is satisfied by any input at all. The
    defect is that THREE minus FIVE is what the runner printed.

    COMPUTED FROM THE INPUT, as the shell twin does (#1339). This read `3 - 5` as two
    literals, which is -2 whatever the arm above was fed: with every fixture file
    written empty, it still passed.
    """
    ran, _skipped, acc = _live_shape(tmp_path)
    expect.num(_lines(ran) - _lines(acc), -2,
               "premise: the subtraction the runner used goes negative on the live shape")


def test_the_readers_sort_their_own_inputs(tmp_path, expect):
    """`comm` on unsorted input yields a WRONG SET silently rather than an error.

    The runner appends these files in roster order, which is not sorted, so the sort
    has to live inside the reader. A caller-sorts contract is one every future caller
    can break quietly.
    """
    ran = _write(tmp_path, "ran_u", "gamma", "alpha", "beta")
    acc = _write(tmp_path, "acc_u", "beta", "alpha")

    out, _ = _call("pgc_ran_without_accounting", ran, acc)
    expect.text(out.split(), ["gamma"],
                "unsorted input still yields the one suite that did not account")


def test_the_two_buckets_partition_the_suites_that_ran(tmp_path, expect):
    """inputs == sum(buckets), and here it is a measurement rather than an identity.

    Neither bucket is the other's leftover: one is the intersection and the other the
    difference, both counted from the names.
    """
    ran = _write(tmp_path, "ran", "alpha", "beta", "gamma")
    acc = _write(tmp_path, "acc", "alpha")

    did, _ = _call("pgc_accounted_among", ran, acc)
    didnt, _ = _call("pgc_ran_without_accounting", ran, acc)
    expect.num(len(did.split()) + len(didnt.split()), 3,
               "the accounted and the debt add up to the suites that ran")


# ---- the wiring, not only the readers ---------------------------------------


def test_the_summary_block_was_extracted_rather_than_an_empty_range(expect):
    """A block that extracted to nothing runs nothing and reports clean.

    The same shape as hashing an empty extraction: both sides agree because neither
    side has content. Asserted before the block is used for anything.
    """
    blk = _summary_block()
    # A SENTINEL, not an empty-vs-empty comparison. `expect.text` refuses an empty
    # expectation for the same reason this arm exists: anything empty satisfies it.
    expect.text("nonempty" if blk.strip() else "empty", "nonempty",
                "the block is not an empty range")
    expect.text(blk.splitlines()[-1], "\tfi",
                "and it ends at its own closing fi, not mid-statement")


def _run_block(tmp_path, acc_names, expect_names=("alpha", "beta", "gamma"), suites_ran=3):
    ran = _write(tmp_path, "e2e_ran", *expect_names)
    skipped = _write(tmp_path, "e2e_skip", "delta", "eps")
    acc = _write(tmp_path, "e2e_acc_" + str(len(acc_names)), *acc_names)
    script = (
        "\n".join(_extract(n) for n in READERS) + "\n"
        + f'_acc_ranfile="{ran}"; _acc_skipfile="{skipped}"; _acc_accounted="{acc}"\n'
        + f"suites_ran={suites_ran}; suites_skipped=2; suites_incomplete=0; verfail=0\n"
        + "SUITES=(alpha beta gamma delta eps)\n"
        + _summary_block() + "\n"
        + 'echo "verfail=$verfail"\n'
    )
    return _sh(script)


def test_the_printed_breakdown_takes_a_count_a_count_can_take(tmp_path, expect):
    """The block a reader actually sees, run against the shape that printed -5.

    Proving the readers correct says nothing about what the summary prints with
    them. A previous removal proof in this tree verified the tool while the bug sat
    in the four lines that called it.
    """
    out, _ = _run_block(tmp_path, ["alpha", "beta", "gamma", "delta", "eps"])

    expect.num(out.count("of those, 3 accounted for their checks and 0 did not"), 1,
               "the breakdown no longer goes negative on the live shape")
    expect.num(len([ln for ln in out.splitlines()
                    if re.search(r"(?<![0-9])-[0-9]+ did not", ln)]), 0,
               "and no line in it carries a negative suite count")
    expect.num(out.count(
        "2 of the 2 skipped suites accounted for themselves anyway: delta eps"), 1,
        "and the skipped-but-accounted suites are named as their own category")
    expect.num(out.count("verfail=0"), 1,
               "and a clean partition leaves the major verdict alone")


def test_the_same_block_counts_and_names_a_real_debt(tmp_path, expect):
    """An arm that only ever sees an empty residual cannot tell a working block
    from one that prints "0 did not" unconditionally."""
    out, _ = _run_block(tmp_path, ["alpha"])

    expect.num(out.count("of those, 1 accounted for their checks and 2 did not"), 1,
               "the debt is counted from the names")
    expect.num(out.count("ran without accounting: beta gamma"), 1,
               "and the suites are named rather than left as a number")
    expect.num(out.count("verfail=0"), 1,
               "and a debt is still not a major failure, which is the old behaviour")


def test_a_ran_count_the_names_do_not_support_fails_the_major(tmp_path, expect):
    """THE PARTITION IS A MEASUREMENT ONLY IF NEITHER BUCKET IS A LEFTOVER (#1339).

    The arms above keep suites_ran equal to the names that ran, so a block that
    derived the debt as `suites_ran - accounted` prints the same strings and passed
    them all (measured: 9 passed with that subtraction in the runner). Here the
    runner is told 4 suites ran while the names say 3, all accounted. Counted from
    the names, 3 + 0 != 4, and the block must say so and fail the major; derived by
    subtraction, 3 + 1 == 4 by construction and it stays silent.
    """
    out, _ = _run_block(tmp_path, ["alpha", "beta", "gamma"], suites_ran=4)
    expect.num(out.count("the ran breakdown does not add up: 4 ran, 3 accounted + 0 did not"), 1,
               "a ran count the names do not support is reported, not absorbed")
    expect.num(out.count("verfail=1"), 1,
               "and it fails the major")


# ---- the tally that feeds the readers (#1339) --------------------------------
#
# SCOPED TO THE FUNCTION, as selftest 510 scopes it: an occurrence count over the
# whole runner is satisfied by a comment naming the variable. What matters is that
# the tally function WRITES each name the readers later read.


def test_the_tally_function_records_every_suite_that_ran_or_skipped(expect):
    tally = _extract("pgc_tally_suite")
    expect.num(tally.count("suites_ran="), 3, "premise: the tally function is findable")
    expect.num(len(re.findall(r'>>"\$_acc_ranfile"', tally)), 3,
               "the tally function records the name of every suite that ran")
    expect.num(len(re.findall(r'>>"\$_acc_skipfile"', tally)), 1,
               "and it records the name of every suite that skipped")


# ---- collation (#1339) ---------------------------------------------------------
#
# `comm` needs both inputs sorted in ITS collation, and `sort` orders by locale: real
# suite names reorder between C and en_US.UTF-8, and `comm` then yields a wrong set.
# `LC_ALL=C comm <(sort ...)` pins only comm itself, because the substitutions inherit
# the PARENT's locale, so both halves are asserted. Code lines only: comments in the
# runner quote the unpinned form to explain it.


def _code_lines():
    return [ln for ln in RUNNER.read_text().splitlines() if not re.match(r"\s*#", ln)]


def test_every_comm_in_the_runner_pins_its_collation(expect):
    comms = [ln for ln in _code_lines() if re.search(r"(^|[^_\w])comm ", ln)]
    expect.at_least(len(comms), 3,
                    "premise: the runner really does use comm, so this arm has a subject")
    expect.num(sum(1 for ln in comms if "LC_ALL=C comm " not in ln), 0,
               "every comm in the runner pins its collation")
    expect.num(sum(1 for ln in _code_lines() if "<(sort " in ln), 0,
               "and no sort feeding one is left on the caller's locale")


def test_no_code_path_subtracts_the_wide_population_from_the_ran_count(expect):
    """The defect's shape, refused at the source.

    CODE LINES ONLY. The runner's comment explaining the old subtraction quotes it
    verbatim, so a whole-file grep counts the prose that exists to describe it --
    the count-versus-property trap this tree has produced four times now.
    """
    code = [ln for ln in RUNNER.read_text().splitlines()
            if not ln.lstrip().startswith("#")]
    expect.num(len([ln for ln in code if "suites_ran - _acc_any" in ln]), 0,
               "no code line subtracts the wide count from the ran count")
