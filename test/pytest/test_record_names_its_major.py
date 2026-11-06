"""A RESULT record that names no major is a row nothing can seed.

#1121. `pgc_record` writes ``${PGC_MAJOR:-unknown}`` and `PGC_MAJOR` is set inside
`pgc_setup`, so a suite that records but never calls `pgc_setup` writes every check
against the literal string ``unknown``.

The gate matches a ledger row only where its majors intersect the majors the run
observed, and **no run ever observes ``unknown``**. So such a check cannot be seeded,
and a row for it could never be matched again. Measured on PG 17 before the fix::

    smoke 9/9   audit 31/31   objstore_stash_recovery 17/17   phase2 42/42
    phase3 32/32   phase4 38/38   phase5 36/36   phase6 43/43
    ---- 248 of 248 records named no major ----

#1109 had already fixed three more the same way; these eight were not in that sweep.

A STATIC RULE CANNOT DO THIS JOB, and two attempts failed in different directions:
"defines no `check()` of its own" misses `audit`, whose own `check()` body calls
`pgc_record`; "the file contains the string pgc_record" misses
`objstore_stash_recovery`, which uses lib.sh's `check()` so the string never appears.
Whether a suite records is a runtime property, so the guard reads the records.

THE ARMS CALL THE RUNNER'S OWN READER, `pgc_unknown_major_records`, in a subprocess
(#1339). An earlier version parsed the record format with a Python copy of the
reader, so it tested itself: the runner's reader mutated to read field five left all
six tests green. Independence from `test/selftest/530-a-record-must-name-its-major.sh`
is about the harness, not the product: this file shares no helper with that part, and
neither file names the other.
"""

import os
import pathlib
import subprocess
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[2]
RUNNER = REPO / "test" / "run_all_versions.sh"

def _unknown_major_records(text):
    """How many records name no major, by the RUNNER's reader, not a copy of it.

    The function is taken out of run_all_versions.sh and run on the text as a log
    file. A runner that no longer defines it raises here instead of answering 0,
    because 0 is also what a correct reader says about a clean log.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
        f.write(text)
        log = f.name
    try:
        r = subprocess.run(
            ["bash", "-c",
             'fn="$(sed -n "/^pgc_unknown_major_records()/,/^}/p" "$1")"; '
             '[ -n "$fn" ] || { echo "the runner defines no pgc_unknown_major_records" >&2; exit 3; }; '
             'eval "$fn"; pgc_unknown_major_records "$2"',
             "_", str(RUNNER), log],
            capture_output=True, text=True)
    finally:
        os.unlink(log)
    if r.returncode != 0 or not r.stdout.strip().isdigit():
        raise RuntimeError(f"the runner's reader did not answer: rc={r.returncode} "
                           f"out={r.stdout!r} err={r.stderr!r}")
    return int(r.stdout.strip())


GOOD = ("RESULT\tdemo\tpart1\tone\tPASS\t17\t\n"
        "RESULT\tdemo\tpart1\ttwo\tPASS\t17\t\n"
        "checks run: 2\n")

MIXED = ("RESULT\tdemo\tpart1\tone\tPASS\tunknown\t\n"
         "RESULT\tdemo\tpart1\ttwo\tPASS\tunknown\t\n"
         "RESULT\tdemo\tpart1\tthree\tPASS\t17\t\n"
         "checks run: 3\n")


def test_a_clean_log_counts_none_while_a_mixed_one_counts_its_own(expect):
    """Asserted as a PAIR.

    Zero is also what a wrong field number, an empty input and a broken parser all
    produce, so the clean case is never asserted alone.
    """
    expect.text((_unknown_major_records(GOOD), _unknown_major_records(MIXED)),
                (0, 2),
                "a clean log yields none and a mixed one yields its two")


def test_every_offending_record_is_counted_not_just_the_first(expect):
    """A suite can record some checks before `pgc_setup` and some after."""
    expect.num(_unknown_major_records(MIXED), 2,
               "both offending records are found, not just the first")


def test_a_log_with_no_records_is_not_an_offender(expect):
    expect.num(_unknown_major_records("checks run: 0\n"), 0,
               "a log carrying no records names no bad major")


def test_the_word_in_a_reason_field_is_not_an_offending_record(expect):
    """Reasons are free text. Matching the line rather than the field counts them."""
    line = "RESULT\tdemo\tpart1\tfour\tPASS\t17\tthe major was unknown at first\n"
    expect.num(_unknown_major_records(line), 0,
               "the word in a reason is not a record that names no major")
    # And the control: the same text in the MAJOR field is caught, so the arm above
    # is about the field and not about the word being absent.
    caught = "RESULT\tdemo\tpart1\tfour\tPASS\tunknown\tthe major was unknown at first\n"
    expect.num(_unknown_major_records(caught), 1,
               "while the same word in field six is")


def test_a_short_record_does_not_crash_or_count(expect):
    """A truncated line has no field six. Indexing it blindly raises."""
    expect.num(_unknown_major_records("RESULT\tdemo\tpart1\n"), 0,
               "a record too short to have a major is not counted")


# ---- the wiring -------------------------------------------------------------


def test_the_runner_reads_every_suites_log_and_fails_the_major(expect):
    """Proving a parser correct says nothing about whether the runner acts on it."""
    code = [l for l in RUNNER.read_text().splitlines()
            if not l.lstrip().startswith("#")]
    text = "\n".join(code)

    expect.num(text.count('pgc_unknown_major_records "$builddir/${s}.log"'), 1,
               "the runner calls the reader over every suite's log")

    # BOUNDED TO THE GUARD'S OWN BLOCK. The runner has many later `verfail=1` lines,
    # so an unbounded search finds one whatever this block does -- which is exactly
    # how the shell twin's first version of this arm passed against the line removed.
    block, seen = [], False
    for l in code:
        if '_unk_total" != 0' in l:
            seen = True
        if seen:
            block.append(l)
            if l == "\tfi":
                break
    expect.text("found" if block else "absent", "found",
                "premise: the guard's block was located")
    expect.num(sum(1 for l in block if "verfail=1" in l), 1,
               "and a suite naming no major fails the major rather than only printing")
    expect.num(sum(1 for l in block if "${_unk_suites}" in l), 1,
               "and the message names the offending suites rather than counting them")
