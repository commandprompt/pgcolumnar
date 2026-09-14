"""TESTS.md must document every test in this directory.

THE TWIN RULE. Every test in this tree is written twice, once as a `.sh` suite and
once here, in the same change. This file is the pytest half of
`test/selftest/350-the-pytest-corpus-must-be.sh`, and the two are not
interchangeable:

  * The `.sh` half is the one with TEETH. `harness_selftest` is registered in
    `SUITES`, so it runs in the matrix and in CI. Nothing runs pytest -- not
    `run_all_versions.sh`, not any workflow under `.github/` -- so a guard written
    only here would never fire in the gate.
  * This half is the one a person running the corpus by hand gets, and it is
    where a failure arrives with the offenders as a Python list rather than as a
    string assembled by shell.

WHY THE GUARD EXISTS AT ALL. TESTS.md says its job is "what each test asserts, and
why it exists". It went stale inside a single rework: the corpus grew from 25 tests
in three files to 54 in five, and the two new files -- 29 tests, every one added by
the rework that answered a review -- were named nowhere in it, while the header
still read "Twenty-five tests in three files".

A partial index of something claiming completeness reads as a total one. A reader
who opens a file whose stated purpose is completeness does not then go and count
the tests. That is the same defect class the vacuity layer refuses one level down:
a report that looks like coverage and is not.
"""

import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent
DOC = HERE / "TESTS.md"

# The bold, fixed-form totals line. Written in a form that can be read back
# precisely so it can be checked: prose that says "twenty-five" cannot be compared
# with anything, which is how the stale header survived being read many times.
TOTALS = re.compile(r"^\*\*(\d+) tests in (\d+) files\.\*\*", re.M)


def corpus_tests(directory):
    """-> {filename: [test name, ...]} for every test_*.py in `directory`."""
    found = {}
    for f in sorted(pathlib.Path(directory).glob("test_*.py")):
        found[f.name] = re.findall(r"^def (test_\w+)", f.read_text(), re.M)
    return found


def undocumented(directory, doc_path):
    """-> sorted list of file and test names the document does not name."""
    text = pathlib.Path(doc_path).read_text()
    missing = []
    for name, tests in corpus_tests(directory).items():
        if name not in text:
            missing.append(name)
        missing.extend(t for t in tests if t not in text)
    return sorted(missing)


def stated_totals_in(text):
    """-> (tests, files) the TEXT claims, or None if it states none."""
    m = TOTALS.search(text)
    return (int(m.group(1)), int(m.group(2))) if m else None


def stated_totals(doc_path):
    """-> (tests, files) the document claims, or None if it states none."""
    return stated_totals_in(pathlib.Path(doc_path).read_text())


def _fixture(tmp_path, doc_body):
    """A two-test corpus and a document, so the arms can drive a KNOWN answer."""
    d = tmp_path / "corpus"
    d.mkdir(exist_ok=True)
    (d / "test_one.py").write_text(
        "def test_alpha(expect):\n    pass\ndef test_beta(expect):\n    pass\n")
    doc = d / "DOC.md"
    doc.write_text(doc_body)
    return d, doc


# ---------------------------------------------------------------------------
# The real corpus. Three properties, each mechanical.
# ---------------------------------------------------------------------------

def test_the_sweep_finds_the_corpus_rather_than_an_empty_glob(expect):
    """A sweep that found nothing reports "nothing missing" and is
    indistinguishable from a sweep that works. This is the premise the other two
    arms rest on, and it is asserted rather than assumed."""
    found = corpus_tests(HERE)
    expect.at_least(len(found), 3, "premise: the sweep found the corpus files")
    expect.at_least(sum(len(v) for v in found.values()), 20,
                    "premise: and the tests inside them")


def test_every_file_and_test_is_named_in_the_document(expect):
    """The property that went wrong. Named rather than counted, so a failure says
    WHICH test is undocumented instead of only how many."""
    missing = undocumented(HERE, DOC)
    expect.text(", ".join(missing) or "none", "none",
                "every test file and every test in the corpus is named in TESTS.md")


def documented_but_absent(directory, doc):
    """Names the DOCUMENT claims that the corpus does not have.

    THE SWEEP ABOVE GOES ONE WAY ONLY. `undocumented()` computes tests on disk
    that the document fails to name, and nothing computed the reverse. So a test
    DELETED or RENAMED while its entry survived was caught by the totals line and
    by nothing else -- and the totals line is a merge target whose correct value
    is a function of the merge, so it is the half most likely to be removed
    (#908). Removing it while this direction was uncovered would have retired a
    check silently, which is the move this file exists to prevent.

    Driven against the real functions, on the corpus that shipped:

        on disk           (1, 1)      # test_one.py holds test_alpha
        document states   (2, 1)      # "test_one.py: test_alpha and test_beta"

        the NAMING arm  : []          <- says nothing is wrong
        the TOTALS arm  : DISAGREE    <- the only arm that reddens

    A BACKTICKED NAME, not any occurrence. The document discusses fixtures and
    hypothetical tests in prose, and a bare-word sweep would report those as
    missing. Backticks are how this document already marks a real identifier, and
    the false-positive budget over the corpus was measured before this was
    written rather than after: 127 backticked names, 2 of which were genuinely
    absent, and both were real defects rather than noise.
    """
    found = corpus_tests(directory)
    on_disk_fns = {n for names in found.values() for n in names}
    on_disk_files = set(found)
    named = set(re.findall(r"`(test_[A-Za-z0-9_]*(?:\.py)?)`", doc.read_text()))
    # A SORTED LIST, like undocumented() beside it. It returned a preformatted
    # "[n: a b c]" string first, copying the bash twin's shape rather than its
    # Python neighbour's, and the two-return-types-one-concept split immediately
    # cost something real: a check written against it read `x in ("[]", "")`,
    # which is False for an empty LIST, and reported a defect as unreproducible.
    return sorted({n for n in named if n.endswith(".py")} - on_disk_files) \
         + sorted({n for n in named if not n.endswith(".py")} - on_disk_fns)


def test_a_documented_test_that_does_not_exist_is_named(expect):
    """The document must not claim a test the corpus does not have.

    It did. `test_layer_rejects_an_absence_assertion_over_an_empty_plan` and its
    control `..._allows_an_absence_assertion_over_a_real_plan` were named in the
    test_layer.py section and existed nowhere: the work is real but lives in
    test_guards_pinned.py as `test_plan_marker_refuses_an_absence_claim_over_an_empty_plan`,
    and is documented correctly there. Two rows claimed coverage under names that
    had never been written, and every other arm in this file passed over them --
    which is the point.
    """
    expect.text(", ".join(documented_but_absent(HERE, DOC)) or "none", "none",
                "every test the document names exists in the corpus")


def test_a_document_naming_a_test_that_was_deleted_is_caught(tmp_path, expect):
    """The removal proof, on a fixture: the shape the real defect had.

    Without this the arm above passes on a healthy tree, which is exactly what an
    arm that computes nothing also does.
    """
    (tmp_path / "test_one.py").write_text("def test_alpha(expect):\n    pass\n")
    doc = tmp_path / "DOC.md"
    doc.write_text("**1 tests in 1 files.**\n`test_one.py`: `test_alpha` and `test_beta`\n")
    expect.text(", ".join(documented_but_absent(tmp_path, doc)), "test_beta",
                "a documented test that does not exist is named, not passed over")
    doc.write_text("**1 tests in 1 files.**\n`test_one.py`: `test_alpha`\n")
    expect.text(", ".join(documented_but_absent(tmp_path, doc)) or "none", "none",
                "control: a document naming only what exists is clean")


def test_a_documented_file_that_does_not_exist_is_caught(tmp_path, expect):
    """A whole file can go the same way, and it is how a rename usually shows up."""
    (tmp_path / "test_one.py").write_text("def test_alpha(expect):\n    pass\n")
    doc = tmp_path / "DOC.md"
    doc.write_text("`test_one.py` and `test_gone.py`: `test_alpha`\n")
    expect.text(", ".join(documented_but_absent(tmp_path, doc)), "test_gone.py",
                "a documented file that does not exist is named")


def test_no_test_name_is_defined_twice_in_the_corpus(expect):
    """The premise the set-equality argument needs, and it was missing (@jdatcmd).

    #919 argues that removing the totals line costs nothing because the two
    sweeps give set EQUALITY between the document and the corpus. That is true of
    NAMES and it is not true of DEFINITION COUNTS, which is what a total counts:

        two files defining test_shared_shape
            definitions on disk           2
            distinct names                1
            undocumented()                clean
            documented_but_absent()       clean
            -> BOTH ARMS GREEN, and the counts differ

    Measured, not argued. It cannot happen today -- 139 definitions against 139
    distinct names, zero duplicates -- so the conclusion was true in fact but not
    by construction, which is the difference between an argument and a guard.

    IT CLOSES SOMETHING REAL BEYOND THE ARGUMENT. `undocumented()` asks whether a
    name appears in the document at all, so a test defined TWICE and documented
    ONCE reads as fully covered. The second definition is invisible to every arm
    here, and pytest runs both.
    """
    found = corpus_tests(HERE)
    names = [n for tests in found.values() for n in tests]
    expect.at_least(len(names), 20, "premise: the corpus was found")
    dupes = sorted({n for n in names if names.count(n) > 1})
    expect.text(", ".join(dupes) or "none", "none",
                "no test name is defined twice in the corpus")
    expect.num(len(names), len(set(names)),
               "so definitions and distinct names are the same count")


def test_a_name_defined_in_two_files_is_caught(tmp_path, expect):
    """The removal proof, and the exact shape that defeats the equality argument."""
    (tmp_path / "test_a.py").write_text("def test_shared_shape(expect):\n    pass\n")
    (tmp_path / "test_b.py").write_text("def test_shared_shape(expect):\n    pass\n")
    found = corpus_tests(tmp_path)
    names = [n for tests in found.values() for n in tests]
    expect.num(len(names), 2, "premise: both definitions were seen")
    expect.num(len(set(names)), 1, "and they share one name")
    expect.text(", ".join(sorted({n for n in names if names.count(n) > 1})),
                "test_shared_shape",
                "a name defined in two files is named, not passed over")


def test_the_document_states_no_totals_for_a_merge_to_get_wrong(expect):
    """TESTS.md must NOT carry a totals line (#908, step 2).

    It used to, and `selftest/350` compared it against the corpus, which is what
    made it a claim rather than decoration. The problem was never the check: it
    was that the number was WRITTEN rather than DERIVED, and its correct value is
    a function of the MERGE rather than of either branch. It collided on
    essentially every rebase touching the corpus -- ten times in one day, both
    sides wrong every time, so there was no side to pick.

    REMOVING IT COSTS NOTHING, and that is provable rather than hopeful. The two
    sweeps together are strictly stronger than any count:

        test_every_file_and_test_is_named_in_the_document
            every test on disk is named here          (disk  subset of  document)
        test_a_documented_test_that_does_not_exist_is_named
            every name here exists on disk            (document  subset of  disk)

    Two subsets in opposite directions is set EQUALITY, so the documented set and
    the corpus are the same set, and any count over one equals the count over the
    other. A stated total was a derived value written by hand.

    WHY THIS IS AN ARM AND NOT JUST A DELETION. Nothing stops the next person
    adding the sentence back -- it reads like an improvement. This arm is what
    makes its absence a decision rather than an accident, and `stated_totals`
    stays for it: the reader still has to work, or "no totals line" would be
    indistinguishable from "cannot find one".
    """
    expect.text(repr(stated_totals(DOC)), "None",
                "TESTS.md states no totals line for a merge to get wrong")

    # And the reader that reports it must still be able to FIND one, or the arm
    # above passes because the parser is broken rather than because the line is
    # gone -- the exact shape this corpus exists to refuse.
    expect.text(repr(stated_totals_in("**7 tests in 3 files.** and prose")),
                "(7, 3)",
                "premise: the reader still finds a totals line when one is there")


def test_the_corpus_counts_are_reported_rather_than_written(expect):
    """The counts do not vanish; they move to where they cannot go stale.

    A number nobody maintains is better than a wrong one, but a number nobody can
    SEE is worse than both. The harness prints them every run, computed from the
    corpus, so a reader gets the same information without the document asserting
    anything.
    """
    found = corpus_tests(HERE)
    total = sum(len(v) for v in found.values())
    expect.at_least(total, 20, "premise: the corpus was found, so a count means something")
    expect.num(len(found), len({f for f in found}), "each file counted once")
    print(f"\nCORPUS: {total} test functions in {len(found)} files")


def test_a_fully_documented_corpus_reports_nothing_missing(tmp_path, expect):
    """Control. A guard with a bad false-positive rate gets switched off, and then
    the guard it replaced is gone too."""
    d, doc = _fixture(tmp_path, "**2 tests in 1 files.**\ntest_one.py: test_alpha and test_beta\n")
    expect.text(", ".join(undocumented(d, doc)) or "none", "none",
                "control: a fully documented corpus reports nothing missing")


def test_an_undocumented_test_is_named_rather_than_passed_over(tmp_path, expect):
    """The exact shape that shipped: the file is named, one test inside it is not."""
    d, doc = _fixture(tmp_path, "**2 tests in 1 files.**\ntest_one.py: test_alpha\n")
    expect.text(", ".join(undocumented(d, doc)), "test_beta",
                "an undocumented test is named rather than passed over")


def test_an_undocumented_file_is_caught_with_the_tests_inside_it(tmp_path, expect):
    """How 29 tests went missing at once: two whole files were never named."""
    d, doc = _fixture(tmp_path, "**2 tests in 1 files.**\nnothing about the corpus at all\n")
    expect.text(", ".join(undocumented(d, doc)), "test_alpha, test_beta, test_one.py",
                "an undocumented file is caught along with the tests inside it")


def test_a_document_with_no_totals_line_states_none(tmp_path, expect):
    """`None` must not read as "the totals happen to match". Absent is its own
    answer, the same way `unknown` never reads as `fresh` elsewhere here."""
    d, doc = _fixture(tmp_path, "test_one.py: test_alpha and test_beta\n")
    expect.text(repr(stated_totals(doc)), "None",
                "a document stating no totals reports None, not a match")


def test_a_stated_total_that_disagrees_with_disk_is_visible(tmp_path, expect):
    """The count arm's own red. A document can name every test and still lie about
    how many there are."""
    d, doc = _fixture(tmp_path, "**9 tests in 4 files.**\ntest_one.py: test_alpha and test_beta\n")
    found = corpus_tests(d)
    expect.text(repr(stated_totals(doc)), "(9, 4)", "the document states 9 in 4")
    expect.text(repr((sum(len(v) for v in found.values()), len(found))), "(2, 1)",
                "while the fixture on disk holds 2 in 1")
    expect.differ(stated_totals(doc),
                  (sum(len(v) for v in found.values()), len(found)),
                  "a stated total that disagrees with disk does not compare equal")


# ---------------------------------------------------------------------------
# VACUITY_MODES.md counts itself, and the count is checked.
#
# @jdatcmd found README.md and VACUITY_MODES.md disagreeing about how many modes
# the layer refuses, and could not check either because the document offered NO
# COUNTING RULE. A document whose subject is claims that cannot be checked should
# not make one. Section 1a now defines a mode as a backticked kebab id of three
# or more words; this asserts the numbers 1a states are the numbers on disk.

MODE_ID = re.compile(r"`([a-z0-9]+(?:-[a-z0-9]+){2,})`")
MODES_DOC = HERE / "VACUITY_MODES.md"


def _named_modes_in(text):
    """-> (refused, not_refused, all) per section 1a's rule, over TEXT.

    A seam, so the rule can be reached with a fixture. `test/selftest/350` had one
    and this did not: the rule was implemented twice and self-tested once, on the
    side that cannot run the corpus it counts. Its edge cases now live here with it
    -- an id of fewer than three words, an id named twice, stopping at the next
    heading, and section 3's back-references.
    """
    chunks = {}
    for chunk in re.split(r"^## ", text, flags=re.M):
        head = chunk.splitlines()[0] if chunk.strip() else ""
        chunks[head] = set(MODE_ID.findall(chunk))
    refused = next((v for k, v in chunks.items() if k.startswith("2.")), set())
    not_refused = next((v for k, v in chunks.items() if k.startswith("3.")), set())
    # Section 3 keeps a back-reference to every mode that moved into section 2
    # ("`X` is now closed"), so a mode can be named in both. Section 2 wins: a
    # refused mode is refused. Without this the same id is counted in two states
    # and the totals stop adding up -- measured at 25 + 50 against 72 named.
    not_refused = not_refused - refused
    return refused, not_refused, set().union(*chunks.values()) if chunks else set()


def _named_modes():
    """The same rule, over the document on disk."""
    return _named_modes_in(MODES_DOC.read_text())


def _stated_row(text, label):
    """-> the number in the VALUE cell of section 1a's row for LABEL, or None.

    The value cell, never the first number on the line. The labels themselves
    contain digits -- "named in section 2, refused today" -- so reading the first
    number returns the 2 from "section 2". `test/selftest/350` shipped exactly that
    bug and read 2 and 3 for totals of 21 and 51, and its own fixture could not see
    it because there the label digit and the value were both 2.
    """
    m = re.search(rf"\|[^|\n]*{re.escape(label)}[^|\n]*\|\s*\**(\d+)", text)
    return None if m is None else int(m.group(1))


def test_the_mode_inventory_states_its_own_totals_correctly(expect):
    """The numbers in section 1a must be the numbers on disk.

    Not a tidiness check: these totals are how a reader decides whether a gap is
    covered, and they were wrong in two files at once with no way to tell.
    """
    refused, not_refused, allm = _named_modes()
    expect.at_least(len(allm), 20, "premise: the counting rule finds modes at all")

    doc = MODES_DOC.read_text()
    for label, got in (("refused today", len(refused)),
                       ("not refused", len(not_refused)),
                       ("named in this document", len(allm))):
        row = re.search(rf"\|[^|\n]*{re.escape(label)}[^|\n]*\|\s*\**(\d+)", doc)
        expect.text(repr(row is not None), "True",
                    f"section 1a states a total for {label!r}")
        expect.num(int(row.group(1)), got,
                   f"the stated total for {label!r} is the number on disk")


def test_the_readme_and_the_inventory_agree_on_what_is_refused(expect):
    """They did not, and neither could be checked against anything.

    README.md said 23 refused while the inventory named 21 — the run's number
    against the document's, with nothing to distinguish them.
    """
    refused, _, _ = _named_modes()
    readme = (HERE / "README.md").read_text()
    expect.at_least(readme.count(f"{len(refused)} refused"), 1,
                    "README.md quotes the number of modes actually named as refused")


def test_the_inventory_accounts_for_every_mode_the_run_found(expect):
    """The 'named nowhere here' row is the gap this document admits to.

    It is arithmetic between numbers the document states, so it can go stale on
    its own: an editor who transcribes a missing mode updates the named total and
    leaves the gap row claiming a gap that has closed. The enumeration's own 79 is
    history -- it is not on disk, and this does not pretend to check it.
    """
    doc = MODES_DOC.read_text()

    def row(label):
        m = re.search(rf"\|[^|\n]*{re.escape(label)}[^|\n]*\|\s*\**(\d+)", doc)
        expect.text(repr(m is not None), "True", f"section 1a states {label!r}")
        return int(m.group(1))

    refused, not_refused, _ = _named_modes()
    expect.num(len(refused) + len(not_refused), row("named in this document"),
               "the two section totals sum to the document total")
    expect.num(row("produced by the enumeration run") - row("named in this document"),
               row("named nowhere here"),
               "the admitted gap is the run's total minus what is written down")


def test_the_prose_totals_match_the_counted_modes(expect):
    """Section 1a's table was not the only place a total lived.

    Three sentences outside it still asserted the run's 23 after the table said 21 --
    section 2's opening, the closing paragraph, and TESTS.md. A table that is checked
    and prose that is not means the drift simply moves into the prose, which is where
    it was in the first place.

    The run's own 23 appears once on purpose, as history, and is not touched here:
    what is gated is every sentence that states what the layer refuses TODAY.
    """
    refused, _, _ = _named_modes()
    n = len(refused)
    for path, pattern in (
        (MODES_DOC, r"(\d+) of the 79"),
        (MODES_DOC, r"known to refuse (\d+) demonstrated modes"),
        (HERE / "TESTS.md", r"This layer refuses (\d+)"),
    ):
        m = re.search(pattern, path.read_text())
        expect.text(repr(m is not None), "True",
                    f"{path.name} states a refused total matching {pattern!r}")
        expect.num(int(m.group(1)), n,
                   f"{path.name}: the prose total is the number of ids named")

def test_the_two_halves_of_the_refused_sentence_sum_to_the_named_total(expect):
    """TESTS.md states the split twice in one sentence: how many modes the layer
    refuses, and how many it does not. Only the first half was gated.

    `selftest/350`'s TESTS.md total arm matches `This layer refuses [0-9]+`, which is the
    half a change to the layer naturally updates. Closing a mode and updating that number
    left "The other 47" behind, and 26 + 47 = 73 against the 72 the inventory names --
    a contradiction introduced by the very change that fixed the other half, and caught
    by nothing. Reported by @jdatcmd.

    So both halves are read here, and checked against the inventory's own count of the
    ids it names rather than against a number typed twice.
    """
    doc = (HERE / "TESTS.md").read_text()
    m = re.search(r"This layer refuses (\d+)\s*\n?of them\.\*\*\s*The other (\d+)", doc)
    expect.text("found" if m else "missing", "found",
                "premise: the sentence states both halves in a form this arm can read")
    refused, other = int(m.group(1)), int(m.group(2))
    named = len(_named_modes()[0]) + len(_named_modes()[1])
    expect.num(refused + other, named,
               "the two halves sum to the number of modes the inventory names")
    expect.num(refused, len(_named_modes()[0]),
               "and the refused half is the count of ids section 2 claims")


# A bullet ENTRY is the unit, not a line. The duplicate that motivated this is a
# two-line bullet, and a line-keyed sweep cannot see it: line 1 of the first copy
# and line 1 of the second are not adjacent. Four sweeps in this tree have now
# failed by keying on the wrong unit, so the unit is named here and fixtured below.
def _bullet_entries(text, floor=40):
    """-> [(1-based start line, the entry joined)] for every bullet in TEXT.

    A continuation is an indented non-blank line under a bullet, which is how every
    multi-line entry in the inventory is written. Entries shorter than FLOOR
    characters are dropped: the inventory legitimately repeats short bullets such as
    a bare id, and a rule that flagged those would be switched off.
    """
    out, cur, start = [], None, 0
    for i, line in enumerate(text.splitlines()):
        if re.match(r"^\s*[-*] ", line):
            if cur is not None:
                out.append((start, cur))
            cur, start = [line.strip()], i + 1
        elif cur is not None and line.strip() and line[:1] in " \t":
            cur.append(line.strip())
        elif cur is not None:
            out.append((start, cur))
            cur = None
    if cur is not None:
        out.append((start, cur))
    return [(s, " ".join(b)) for s, b in out if len(" ".join(b)) >= floor]


def _duplicated_entries(text):
    """-> [(first line, repeat line, the entry)] for every entry written twice."""
    seen, dupes = {}, []
    for start, entry in _bullet_entries(text):
        if entry in seen:
            dupes.append((seen[entry], start, entry))
        else:
            seen[entry] = start
    return dupes


def test_the_inventory_names_no_entry_twice(expect):
    """A duplicated entry double-states the inventory, and the count guard is blind
    to it BY CONSTRUCTION rather than by accident.

    `_named_modes_in` builds `set(MODE_ID.findall(chunk))` per section, so every
    total it states is over distinct ids. Measured on the document that motivated
    this, with the second copy of a six-id bullet present and then deleted:

        with the duplicate      (28, 44, 72)
        without the duplicate   (28, 44, 72)

    So no existing arm here can fail on it, and none did: the duplicate sat in
    section 3.4 while `test_the_mode_inventory_states_its_own_totals_correctly`,
    `test_the_prose_totals_match_the_counted_modes` and the sum arm above were all
    green. The cost is to the reader rather than to the totals -- a six-id bullet
    written twice reads as two distinct groups of open modes -- which is why this is
    an arm over entries and not a correction to the counting rule. Deduping ids is
    right; the totals must not move because someone pasted a line twice.

    The counterpart to `test_no_test_name_is_defined_twice_in_the_corpus`, for the
    document rather than the corpus.
    """
    doc = MODES_DOC.read_text()
    entries = _bullet_entries(doc)
    expect.at_least(len(entries), 20, "premise: the rule finds entries to compare")
    dupes = _duplicated_entries(doc)
    expect.text(
        "; ".join(f"lines {a} and {b}" for a, b, _ in dupes) or "none", "none",
        "the inventory names no entry twice")


def test_a_duplicated_entry_is_caught_on_a_fixture(expect):
    """Prove the rule fires, and fires on the shape that got through.

    Two lines, not one, because a one-line fixture would pass against a sweep keyed
    on adjacent identical LINES -- the sweep that missed the real duplicate.
    """
    entry = ("- `a-mode-named-once`, `a-second-mode-here`,\n"
             "  `a-third-mode-on-the-continuation-line`\n")
    clean = "## 3.4 A section\n\n" + entry + "\n- `something-else-entirely-here`, `and-another-mode-id`\n"
    expect.num(len(_duplicated_entries(clean)), 0,
               "premise: the clean fixture is not flagged")
    spliced = clean.replace(entry, entry + "\n" + entry, 1)
    dupes = _duplicated_entries(spliced)
    expect.num(len(dupes), 1, "the duplicated two-line entry is caught")
    expect.num(dupes[0][0], 3, "and the FIRST copy's line number is reported")
    expect.at_least(dupes[0][1], 4, "with the repeat's line after it")


def test_a_short_repeated_bullet_is_not_flagged(expect):
    """The rule's false-positive budget, stated rather than assumed.

    The inventory repeats short bullets -- a bare id under two headings is ordinary
    -- and a guard that reddened on those would be removed, taking the real rule
    with it. Measured at the floor: the same bullet below it passes, above it fails.
    """
    short = "## 3.4 A section\n\n- `a-b-c`\n\n- `a-b-c`\n"
    expect.num(len(_duplicated_entries(short)), 0,
               "a repeated bullet under the length floor is not a duplicate")
    long_id = "- `" + "a-b-c-" * 9 + "d`\n"
    doubled = "## 3.4 A section\n\n" + long_id + "\n" + long_id
    expect.at_least(len(_bullet_entries(doubled)), 2,
                    "premise: the long fixture clears the floor")
    expect.num(len(_duplicated_entries(doubled)), 1,
               "and the same bullet above the floor IS a duplicate")




# ---------------------------------------------------------------------------
# The counting rule's own edges, and the row reader's.
#
# These moved from `test/selftest/350` (#432). The rule is implemented on both
# sides -- that is the two-harness design, parallel in functionality -- but it was
# SELF-TESTED only on the shell side, which cannot run the corpus it counts. A rule
# with no fixtures is a rule that passes because the document happens to agree with
# it today.
# ---------------------------------------------------------------------------

_FIXTURE_DOC = """## 1a. Counting

| named in section 2, refused today | **2** |
| named in section 3, not refused | **1** |

## 2. Refused

`one-two-three` and `four-five-six`, and `one-two-three` again.

## 3. Not refused

`seven-eight-nine`. And `one-two-three` is now closed.

## 4. Something else

`ten-eleven-twelve`
"""


def test_the_counting_rule_counts_a_fixture_as_the_document_says(expect):
    """Section 2 names two distinct ids; section 3 names one, after its
    back-reference to a section 2 id is discounted."""
    refused, not_refused, allm = _named_modes_in(_FIXTURE_DOC)
    expect.text(", ".join(sorted(refused)), "four-five-six, one-two-three",
                "section 2's ids, deduplicated")
    expect.text(", ".join(sorted(not_refused)), "seven-eight-nine",
                "section 3's ids, minus the back-reference section 2 already claims")
    expect.num(len(allm), 4, "and every id in the document is seen once")


def test_an_id_of_fewer_than_three_words_is_not_a_mode(expect):
    """The rule is three or more words. Two is an ordinary backticked phrase, and
    counting it would make every `foo-bar` in prose a mode."""
    doc = "## 2. Refused\n\n`one-two` and `alpha` and `one-two-three`\n"
    refused, _, _ = _named_modes_in(doc)
    expect.text(", ".join(sorted(refused)), "one-two-three",
                "only the three-word id counts")


def test_the_counter_stops_at_the_next_heading(expect):
    """Section 2's count must not reach into section 4. Without the stop, every id
    below section 2 would be refused, and the totals would agree with nothing."""
    refused, _, _ = _named_modes_in(_FIXTURE_DOC)
    expect.text("ten-eleven-twelve" in refused and "yes" or "no", "no",
                "an id in a later section is not counted as refused")


def test_the_row_reader_takes_the_value_not_a_digit_in_the_label(expect):
    """The labels contain digits. Reading the first number on the line returns the
    2 from "section 2" -- which `test/selftest/350` did, reporting 2 and 3 for
    totals of 21 and 51. The fixture there could not see it, because the label
    digit and the value were both 2.

    So this fixture makes them DIFFER: the label says "section 2" and the value is
    9. A reader that takes the label's digit answers 2 and fails here.
    """
    doc = "| named in section 2, refused today | **9** |\n"
    expect.num(_stated_row(doc, "refused today"), 9,
               "the value cell is read, not the digit inside the label")


def test_an_absent_row_is_none_rather_than_a_number_that_happens_to_match(expect):
    """A missing row must be distinguishable from a row stating zero. Returning 0
    for both would make a document that states nothing agree with a corpus that
    counts nothing."""
    expect.text(repr(_stated_row("## 1a. Counting\n\nno table here\n", "refused today")),
                "None", "an absent row reads as None")
    expect.num(_stated_row("| refused today | **0** |\n", "refused today"), 0,
               "and a row stating zero reads as 0")


def test_a_stated_total_that_disagrees_with_the_ids_is_visible(expect):
    """The arm this whole block protects: the document's number against the ids on
    disk. With the fixture's own table it agrees; change the table and it must not."""
    refused, not_refused, _ = _named_modes_in(_FIXTURE_DOC)
    expect.num(_stated_row(_FIXTURE_DOC, "refused today"), len(refused),
               "control: the fixture's stated refused total is its id count")
    wrong = _FIXTURE_DOC.replace("refused today | **2** |", "refused today | **7** |")
    expect.differ(_stated_row(wrong, "refused today"), len(refused),
                  "and a disagreeing total does not compare equal")


# ---------------------------------------------------------------------------
# Every contents-list link must reach a heading.
#
# Moved from `test/selftest/350` (#432), where the subject was this directory's own
# documents. TESTS.md's contents list gained an entry whose anchor stripped the
# underscores out of a file name -- `#14-testharnessdepspy-…` against a heading
# GitHub renders as `#14-test_harness_depspy-…` -- so the link went nowhere while
# eleven entries above it kept the underscores. Neither coverage sweep could see
# it: both look for NAMES, and a broken link still contains the name it points at.
# A reader finds out by clicking.
#
# ONE DIRECTION ON PURPOSE. Every link must reach a heading; the reverse needs an
# exemption list, because "## Contents" is a heading no entry links to, and a
# hand-maintained exemption list is the thing this corpus keeps deleting.
# ---------------------------------------------------------------------------

_LINK = re.compile(r"\]\(#([A-Za-z0-9_-]+)\)")
_HEADING = re.compile(r"^#{2,} +(.+)$", re.M)


def _github_anchor(heading):
    """GitHub's rule: lowercase, drop anything that is not a letter, digit, space,
    hyphen or underscore, then spaces to hyphens. The dot in `.py` and the colon
    after it go; the underscores stay."""
    kept = [c for c in heading.lower() if c.isalnum() or c in " _-"]
    return "".join(kept).replace(" ", "-")


def _unresolved_links(text):
    """-> sorted anchors that no heading in TEXT produces."""
    have = {_github_anchor(h) for h in _HEADING.findall(text)}
    return sorted({a for a in _LINK.findall(text) if a not in have})


def test_every_test_file_has_a_NUMBERED_section_of_its_own(expect):
    """A SECTION AT THE WRONG LEVEL IS INVISIBLE TO EVERY OTHER ARM (#1024).

    The arm above catches a section NUMBER taken twice, which is the collision #1024
    describes, and it does catch it -- planted, two arms redden. What nothing caught is
    a test file whose section was written as an unnumbered `###` instead of a numbered
    `##`: it is not in the numbering, so `1..N with no gap` never sees it; it is not in
    the contents, so the link arms never see it; and the file IS named in the document,
    so the coverage arm is satisfied.

    That is not hypothetical. `test_iceberg_fdw.py` shipped that way in #1057 and sat
    undetected until this arm was written -- one person writing a heading at the wrong
    level, where the collision needs two PRs in flight.

    MEASURED BEFORE WRITING IT: 33 test files, 32 with a numbered section, one without,
    and that one was the defect. The rule was already true everywhere else, which is
    why it can be asserted rather than declared as a goal.
    """
    text = (HERE / "TESTS.md").read_text(encoding="utf-8")
    files = sorted(p.name for p in HERE.glob("test_*.py"))
    numbered = set(re.findall(r"^## \d+\. (test_\w+\.py)", text, re.M))

    expect.at_least(len(files), 20,
                    "premise: the corpus was found, so the comparison is not vacuous")
    missing = [f for f in files if f not in numbered]
    expect.text(", ".join(missing) or "none", "none",
                "every test file has a NUMBERED top-level section, so none is "
                "documented outside the numbering the arms above check")

    # AND THE OTHER DIRECTION, so a section cannot outlive the file it documents --
    # the same both-ways shape the declaration arms use.
    gone = [n for n in sorted(numbered) if n not in files]
    expect.text(", ".join(gone) or "none", "none",
                "and every numbered section names a file that exists")


def test_the_anchor_rule_drops_punctuation_and_keeps_underscores(expect):
    """The derivation, on the heading the defect was found in."""
    # Assembled, for the reason given in the arm below: a literal corpus file name
    # in a string makes the membership classifier read this file as driving that
    # one. The anchor on the right keeps its literal form, because the dot is gone
    # from it and so it is no longer that file's name.
    heading = "14. " + "test" + "_harness" + "_deps" + ".py: the harness must self-test"
    expect.text(_github_anchor(heading),
                "14-test_harness_depspy-the-harness-must-self-test",
                "the dot and the colon go, the underscores stay")


def test_an_anchor_that_strips_the_underscores_is_caught(expect):
    """The exact shape that shipped, and a control beside it. Without the control a
    sweep that matches nothing reports the same clean answer."""
    # THE FILENAME IS ASSEMBLED, NOT WRITTEN. A literal `test_harness_deps.py` in
    # this string makes `_mentioned_files` in that very file read this one as a
    # driver of it, and the membership classifier then calls this file
    # cluster-bound. That is CONTEXT.md's rule 3 -- "a word that merely looks like
    # a filename" -- and it cost two red arms before the cause was found. The
    # heading here is sample markdown, not a reference.
    _f = "test" + "_harness" + "_deps" + ".py"
    good = (f"## 14. {_f}: the harness\n"
            "- [14. x](#14-test_harness_depspy-the-harness)\n")
    bad = (f"## 14. {_f}: the harness\n"
           "- [14. x](#14-testharnessdepspy-the-harness)\n")
    expect.text(", ".join(_unresolved_links(good)) or "none", "none",
                "control: an anchor that keeps the underscores resolves")
    expect.text(", ".join(_unresolved_links(bad)),
                "14-testharnessdepspy-the-harness",
                "and one that strips them is named rather than passed over")


def test_every_in_document_link_in_this_directory_reaches_a_heading(expect):
    """The real sweep, over every document beside this file.

    With a coverage premise: a sweep over an empty file list reports nothing
    broken, which is what a correct set of documents reports too.
    """
    docs = sorted(HERE.glob("*.md"))
    expect.at_least(len(docs), 3, "premise: the sweep found this directory's documents")
    total_links = 0
    for doc in docs:
        text = doc.read_text(encoding="utf-8")
        total_links += len(_LINK.findall(text))
        expect.text(", ".join(_unresolved_links(text)) or "none", "none",
                    f"every in-document link in {doc.name} reaches a heading")
    expect.at_least(total_links, 15,
                    "premise: and it parsed links rather than finding none")


# A NUMBERED CONTENTS LIST MUST BE IN ORDER, which the link arms above cannot see.
# They ask whether a link RESOLVES, and a shuffled list resolves perfectly. #1023's
# merge put TESTS.md's TOC at `29, 31, 30` against sections `29, 30, 31`, and every
# arm here stayed green.
_NUMBERED_TOC = re.compile(r"^- \[(\d+)\. ", re.M)
_NUMBERED_SECTION = re.compile(r"^## (\d+)\. ", re.M)


def _numbering(text):
    """-> (toc numbers, section numbers) as they appear, in document order."""
    return ([int(n) for n in _NUMBERED_TOC.findall(text)],
            [int(n) for n in _NUMBERED_SECTION.findall(text)])


def _gaps(nums):
    """-> [(a, b)] for every adjacent pair that is not b == a + 1."""
    return [(a, b) for a, b in zip(nums, nums[1:]) if b != a + 1]


def test_the_contents_list_is_numbered_in_order(expect):
    """TESTS.md's contents list and its sections must both count 1..N with no gap.

    THE SHAPE THIS CLOSES, measured rather than imagined. After #1023 merged, the
    document on main read:

        TOC       ... 29, 31, 30      three out-of-order transitions once 32 arrived
        sections  ... 29, 30, 31      contiguous and correct

    so the list disagreed with the order a reader scrolls through, and the two arms
    above were green throughout: both orders RESOLVE, which is all they ask. A
    contents list whose numbers are shuffled is a list the reader cannot scan, and it
    is the first thing anyone adding a section copies.

    BOTH SEQUENCES, not just the TOC. The collision that produced this is a section
    NUMBER taken twice, so the sections are where a duplicate shows up first, and
    `1..N with no gap` catches a duplicate and an omission in one rule.
    """
    text = (HERE / "TESTS.md").read_text(encoding="utf-8")
    toc, sections = _numbering(text)
    expect.at_least(len(toc), 20, "premise: the rule found a numbered contents list")
    expect.at_least(len(sections), 20, "premise: and it found numbered sections")
    expect.num(toc[0], 1, "the contents list starts at 1")
    expect.text(str(_gaps(toc)) if _gaps(toc) else "none", "none",
                "the contents list is numbered 1..N with no gap or inversion")
    expect.text(str(_gaps(sections)) if _gaps(sections) else "none", "none",
                "and the sections are numbered 1..N with no gap or inversion")
    expect.num(len(toc), len(sections),
               "with one contents entry per section")


def test_a_shuffled_contents_list_is_caught_on_a_fixture(expect):
    """The removal proof, on the exact shape that shipped.

    `29, 31, 30` rather than a single swap, because that is what the merge produced
    and because a rule keyed only on "is it sorted" would also flag a list that is
    merely missing an entry. Both are caught here, and named apart.
    """
    clean = "- [1. a](#a)\n- [2. b](#b)\n- [3. c](#c)\n\n## 1. a\n\n## 2. b\n\n## 3. c\n"
    toc, sections = _numbering(clean)
    expect.num(len(_gaps(toc)), 0, "premise: the clean fixture is not flagged")
    expect.num(len(sections), 3, "premise: and it read the sections too")

    shuffled = clean.replace("- [2. b](#b)\n- [3. c](#c)", "- [3. c](#c)\n- [2. b](#b)")
    stoc, _ = _numbering(shuffled)
    expect.text(str(_gaps(stoc)), "[(1, 3), (3, 2)]",
                "the inversion is caught, and named as the two transitions it is")

    missing = clean.replace("- [2. b](#b)\n", "")
    mtoc, _ = _numbering(missing)
    expect.text(str(_gaps(mtoc)), "[(1, 3)]",
                "and an omitted entry is caught as a gap, not confused with an inversion")


# ---------------------------------------------------------------------------
# Section 5's "what to add next" list must not name work that is already done.
#
# WHY. Section 5 is the one part of this document whose only reader is someone about
# to BUILD something, and it is the only part with no mechanism. Sections 1a, 2 and 3
# are all checked against the ids on disk; section 5 was prose. Entry 1 said "the
# constant exists and nothing writes it" long after `query_error()` existed and
# `test_failed_query_sentinel.py` had ten arms over it, so the next person to take the
# list would have built something that was already there.
#
# THAT ALMOST HAPPENED FOR REAL, one document over. A bad enumeration of
# `test/selftest/340` made an existing block look like a coverage gap, and the
# duplicate was written and proven to discriminate before the duplication was noticed
# (#432). A stale "what to add next" is the same defect with the wrong answer written
# down in advance.
#
# WHAT IS CHECKABLE, and what is not. "Has this work been done" is not mechanical:
# entry 1's work landed under four different test names, so asking whether the NAMED
# test exists would have passed and said nothing. What is mechanical is the anchor:
#
#   * every entry names at least one mode id, so it is tied to the inventory at all
#   * an un-struck entry's ids are in section 3 (not refused) and NOT in section 2
#
# An entry whose id has reached section 2 is done by the document's own accounting,
# whatever it is called in the corpus. And an entry naming no id cannot be checked
# against anything, which is how entry 1 stayed wrong.
# ---------------------------------------------------------------------------

_NEXT_ITEM = re.compile(r"^(\d+)\. (.*?)(?=^\d+\. |\Z)", re.M | re.S)


def _next_steps_entries(text):
    """-> [(number, struck, {mode ids})] for section 5's numbered list."""
    body = ""
    for chunk in re.split(r"^## ", text, flags=re.M):
        if chunk.startswith("5."):
            # DROP THE HEADING LINE. The section is "## 5. What to add next", and with
            # the "## " split away the heading itself begins "5. " at the start of the
            # string -- which the item pattern matches, inventing an entry 5 that is
            # the title. It collided with the real entry 5 and reported 3 items in a
            # two-item fixture, which is how it was caught.
            body = chunk.split("\n", 1)[1] if "\n" in chunk else ""
            break
    out = []
    for m in _NEXT_ITEM.finditer(body):
        item = m.group(2)
        out.append((int(m.group(1)), "~~" in item, set(MODE_ID.findall(item))))
    return out


def test_the_next_steps_list_is_anchored_to_the_inventory(expect):
    """Every entry names a mode id, so the entry can be checked at all."""
    entries = _next_steps_entries(MODES_DOC.read_text())
    expect.at_least(len(entries), 4, "premise: the list was parsed, not missed")
    unanchored = sorted(n for n, _struck, ids in entries if not ids)
    expect.text(", ".join(str(n) for n in unanchored) or "none", "none",
                "every entry in section 5 names at least one mode id")


def test_no_open_next_step_names_work_the_document_calls_done(expect):
    """An un-struck entry whose id has reached section 2 is stale.

    Section 2 is "refused today". An entry still listed as work to do, naming an id
    the document itself has moved into section 2, sends the next reader to build
    something this document says exists.
    """
    refused, not_refused, _ = _named_modes_in(MODES_DOC.read_text())
    entries = _next_steps_entries(MODES_DOC.read_text())
    expect.at_least(len(refused), 20, "premise: section 2's ids were found")

    stale = sorted(
        "%d:%s" % (n, i)
        for n, struck, ids in entries if not struck
        for i in sorted(ids) if i in refused
    )
    expect.text(", ".join(stale) or "none", "none",
                "no open entry names an id section 2 already claims as refused")


def test_a_stale_next_step_is_caught_on_a_fixture(expect):
    """The removal proof, on a document built to be wrong — and its control.

    Without the control, a parser that finds no entries reports the same clean
    answer as a correct list.
    """
    doc = (
        "## 1a. Counting\n\n"
        "## 2. Refused\n\n`one-two-three` is refused.\n\n"
        "## 3. Not refused\n\n`four-five-six` is not.\n\n"
        "## 5. What to add next, in order\n\n"
        "1. `write_something` — closes `four-five-six`.\n"
        "2. ~~`write_other` — closes `one-two-three`.~~ **Done.**\n"
    )
    entries = _next_steps_entries(doc)
    expect.num(len(entries), 2, "premise: both fixture entries were parsed")
    expect.text(repr([(n, st) for n, st, _ in entries]), "[(1, False), (2, True)]",
                "and the strike-through is what marks one done")

    refused, _, _ = _named_modes_in(doc)
    stale = ["%d:%s" % (n, i) for n, st, ids in entries if not st
             for i in sorted(ids) if i in refused]
    expect.text(", ".join(stale) or "none", "none",
                "control: the open entry names an UNrefused id, so it is not stale")

    # Now move the open entry's id into section 2, which is what "done" looks like.
    moved = doc.replace("`four-five-six` is not.", "moved away.").replace(
        "`one-two-three` is refused.", "`one-two-three` and `four-five-six` are refused.")
    refused2, _, _ = _named_modes_in(moved)
    entries2 = _next_steps_entries(moved)
    stale2 = ["%d:%s" % (n, i) for n, st, ids in entries2 if not st
              for i in sorted(ids) if i in refused2]
    expect.text(", ".join(stale2), "1:four-five-six",
                "and an open entry whose id reached section 2 is named")


def test_a_numbered_section_has_a_body_of_its_own(expect):
    """A numbered heading immediately followed by another heading documents nothing.

    THE COVERAGE ARM CANNOT SEE THIS. `test_every_test_file_has_a_NUMBERED_section_of_its_own`
    asks whether each file is NAMED by a numbered heading, and a heading with no body is
    still a heading -- so a section inserted between another section's heading and its
    body leaves both files named and one of them documented under the wrong title. That is
    how `## 37. test_iceberg_fdw.py` came to sit directly above `## 38.`, with the Iceberg
    body attached to the userinfo heading: a merge put the new section in the gap, and
    every existing arm stayed green.

    So this reads the STRUCTURE rather than the names: between one numbered heading and
    the next there must be something that is not another heading and not blank.
    """
    text = (HERE / "TESTS.md").read_text(encoding="utf-8")
    lines = text.split("\n")
    heads = [i for i, l in enumerate(lines) if re.match(r"^## \d+[a-z]?\. ", l)]
    expect.at_least(len(heads), 20,
                    "premise: the numbered headings were found at all")

    empty = []
    for k, i in enumerate(heads):
        end = heads[k + 1] if k + 1 < len(heads) else len(lines)
        body = [l for l in lines[i + 1:end] if l.strip() and not l.startswith("#")]
        if not body:
            empty.append(lines[i][3:].split(":")[0].strip())
    expect.text(", ".join(empty) or "none", "none",
                "every numbered section carries a body of its own")

    # Control: plant the shape and prove the reader names it, so a green above is the
    # absence of the defect rather than the absence of a working check.
    planted = lines[:heads[1]] + ["## 999. planted.py: nothing follows this", ""] + lines[heads[1]:]
    heads2 = [i for i, l in enumerate(planted) if re.match(r"^## \d+[a-z]?\. ", l)]
    found = []
    for k, i in enumerate(heads2):
        end = heads2[k + 1] if k + 1 < len(heads2) else len(planted)
        if not [l for l in planted[i + 1:end] if l.strip() and not l.startswith("#")]:
            found.append(planted[i][3:].split(":")[0].strip())
    expect.text(", ".join(found), "999. planted.py",
                "control: a planted empty section is named by the same reader")
