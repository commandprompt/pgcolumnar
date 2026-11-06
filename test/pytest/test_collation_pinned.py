"""`comm`'s two inputs must be sorted in one collation, and the pin must reach them.

#552 established the rule and #1112 found the hole. `comm` requires both inputs sorted
in ITS collation and does not check: fed a mismatch it writes `input is not in sorted
order` to stderr and prints a result anyway, so in a harness whose stderr lands in a log
nobody reads, a wrong set arrives looking like an answer.

The inputs are not collation-insensitive. Measured on real suite names,
`pgc_setup`/`pg_dump_roundtrip` and `projections`/`projection_update` both swap between
`C` and `en_US.UTF-8`.

TWO HALVES, AND THE SECOND IS THE ONE THAT WAS MISSED. `LC_ALL=C comm <(sort a) <(sort b)`
pins only comm's own comparison: the process substitutions run in subshells of the
PARENT and inherit ITS locale. A guard that accepted `LC_ALL=C` anywhere on the line
would bless exactly the form a reader writes after reading the guard's name.

CODE LINES ONLY. The shell guard used to scan every line, prose included, so a comment
explaining the rule violated it -- a note reading "reads only the piped form" contained
the literal string the guard grepped for and flagged its own file.

Read INDEPENDENTLY of `test/selftest/070-and-comm-s-two-inputs-must.sh`: same corpus,
own implementation, own planted probes, and neither file names the other. The shell part
is a set of `grep` pipelines over the same files; this walks them in Python.
"""

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]
SUITES = REPO / "test"

# `sort` IMMEDIATELY after a pipe or an opening process substitution. A pinned form
# reads `| LC_ALL=C sort` or `<(LC_ALL=C sort`, which puts text between the two and so
# does not match -- that is what makes a MIXED line fail without further work, since an
# unpinned sort on the same line still matches.
# AND AT THE START OF A COMMAND (#1339): `sort a > a.s; sort b > b.s; comm a.s b.s` sorts
# into files that comm then reads, and that form has no pipe or substitution before its
# sort, so the pattern above alone never saw it.
_UNPINNED_SORT = re.compile(r"(\|\s*sort(\s|$)|<\(\s*sort(\s|$)|(^|;|&&|\|\||\$\()\s*sort(\s|$))")
_COMM = re.compile(r"(^|[^_\w])comm\s")
_PINNED_COMM = re.compile(r"LC_ALL=C\s+comm\s")


def _code_lines(text):
    """Every line that is not a comment. Prose about the rule is not a breach of it."""
    return [l for l in text.splitlines() if not l.lstrip().startswith("#")]


def _unpinned_sorts(text):
    return [l for l in _code_lines(text) if _UNPINNED_SORT.search(l)]


def _unpinned_comms(text):
    return [l for l in _code_lines(text)
            if _COMM.search(l) and not _PINNED_COMM.search(l)]


def _files_using_comm():
    out = []
    for p in sorted(SUITES.glob("*.sh")):
        t = p.read_text()
        if any(_COMM.search(l) for l in _code_lines(t)):
            out.append(p)
    return out


# ---- the corpus ------------------------------------------------------------


def test_the_corpus_has_files_using_comm_so_the_sweep_is_not_vacuous(expect):
    """The arms below report "none". Without this they report none of nothing."""
    names = [p.name for p in _files_using_comm()]
    expect.at_least(len(names), 1, "at least one suite still uses comm")
    # Printed from the data rather than retyped, so this cannot go stale.
    # FLAGGED BY THE SIGNATURE AND NOT A DEFECT, fixed anyway because it is one
    # word: `at_least(len(names), 1)` above already guarantees >= 1, so this fails
    # only at exactly 1 and the boolean is lossless. A static rule cannot see that a
    # PRECEDING assertion narrowed the failing set -- which is the interesting false
    # positive in this class.
    expect.text("many" if len(names) >= 2 else f"only {len(names)}", "many",
                f"and more than one does, so the sweep spans files: {names}")


def test_every_sort_feeding_a_comm_pins_its_collation(expect):
    offenders = {}
    for p in _files_using_comm():
        bad = _unpinned_sorts(p.read_text())
        if bad:
            offenders[p.name] = bad
    expect.num(len(offenders), 0,
               f"no suite using comm leaves a sort on the caller's locale: {offenders}")


def test_every_comm_pins_its_own_comparison(expect):
    """Separate from the arm above, because the prefix does not reach the inputs.

    Both have to hold: `comm` compares in its own locale, and each substitution sorts
    in the parent's.
    """
    offenders = {}
    for p in _files_using_comm():
        bad = _unpinned_comms(p.read_text())
        if bad:
            offenders[p.name] = bad
    expect.num(len(offenders), 0,
               f"no comm is left on the caller's locale: {offenders}")


# ---- the detector, proved by planting --------------------------------------
#
# Both corpus arms report zero today, measured before this file was written. An arm
# that can only ever report "none" is a check that cannot fail, so every form the
# detector must catch is planted, and the forms it must NOT catch are planted too.


def test_the_detector_catches_the_process_substituted_form(expect):
    """The hole #1112 names: not a pipeline, so a pipe pattern cannot see it."""
    expect.num(len(_unpinned_sorts('\tcomm -23 <(sort "$1") <(sort "$2")\n')), 1,
               "an unpinned process-substituted sort is caught")


def test_the_detector_still_catches_the_piped_form(expect):
    """The case #552 already covered. Widening must not trade one for the other."""
    expect.num(len(_unpinned_sorts("\tcat a | sort > b\n")), 1,
               "an unpinned piped sort is caught")


def test_a_sort_into_a_file_that_comm_reads_is_caught(expect):
    """The form with no pipe: sorted into files first, then compared (#1339)."""
    expect.num(len(_unpinned_sorts('\tsort a > a.s; sort b > b.s; LC_ALL=C comm -23 a.s b.s\n')), 1,
               "an unpinned sort at the start of a command is caught")
    expect.num(len(_unpinned_sorts('\tLC_ALL=C sort a > a.s; LC_ALL=C sort b > b.s\n')), 0,
               "control: the same sorts pinned are not caught")


def test_a_line_with_one_of_two_sorts_pinned_is_caught(expect):
    """The half-pinned form, which is what a reader writes after a partial fix."""
    expect.num(len(_unpinned_sorts('\tcomm -23 <(LC_ALL=C sort "$1") <(sort "$2")\n')), 1,
               "pinning one of two sorts is not pinning the line")


def test_pinning_only_the_comm_does_not_pin_its_substitutions(expect):
    """The trap inside the trap, and the reason the two arms are separate."""
    text = '\tLC_ALL=C comm -23 <(sort "$1") <(sort "$2")\n'
    expect.num(len(_unpinned_sorts(text)), 1,
               "a pinned comm over unpinned sorts is still a breach")
    expect.num(len(_unpinned_comms(text)), 0,
               "while the comm half of that same line is satisfied")


def test_a_fully_pinned_line_is_not_flagged(expect):
    """Without this the detector could be 'flag everything' and every arm above passes."""
    text = '\tLC_ALL=C comm -23 <(LC_ALL=C sort "$1") <(LC_ALL=C sort "$2")\n'
    expect.num(len(_unpinned_sorts(text)), 0, "a fully pinned line is clean")
    expect.num(len(_unpinned_comms(text)), 0, "and so is its comm")


def test_prose_describing_the_rule_does_not_violate_it(expect):
    """A rule that cannot be written down is a rule people stop writing down.

    This exact comment flagged `run_all_versions.sh` for its own text before the
    shell guard learned to skip comments.
    """
    text = "# the guard reads only the `| sort` form and misses <(sort ...)\n"
    expect.num(len(_unpinned_sorts(text)), 0,
               "a comment containing the forbidden form is not a breach")
    expect.num(len(_unpinned_comms("# comm needs both inputs sorted the same way\n")), 0,
               "and neither is a comment mentioning comm")


def test_an_unpinned_comm_is_caught_and_a_word_containing_comm_is_not(expect):
    """`_COMM` must not fire on `_cm_unpinned_comms` or `command`."""
    expect.num(len(_unpinned_comms("\tcomm -23 a b\n")), 1, "a bare comm is caught")
    expect.num(len(_unpinned_comms("\tcommand -v sort\n")), 0,
               "and `command` is not a comm")
    expect.num(len(_unpinned_comms("\t_my_comm_helper a b\n")), 0,
               "nor is an identifier containing it")
