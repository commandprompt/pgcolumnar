"""The matrix must reconcile the suites it registered against the ones that accounted.

`run_all_versions.sh` prints "suites that ran: N of M" and never checks it, and twelve
registered suites exit 0 without ever calling `pgc_summary`. Measured with a pattern
tight enough to exclude `portlib.sh` -- a looser one matched it and gave both reviewers
of this change the same wrong answer: **none** of the twelve sources `test/lib.sh`.
Each defines its own `check()`, and ten keep no tally at all, so the harness cannot see
their checks. Counted among the suites that
"ran", they are the overcount #447 added that line to stop, one level further down.

A count cannot close this. Two errors of opposite sign cancel, and an exempt list
maintained by hand makes the count agree by construction. So membership is derived
from a property each suite carries -- its own text calls `pgc_summary`, and its log
carries the `accounting:` line `pgc_summary` prints before every exit path -- and the
two readings are reconciled as SETS, in both directions.

These tests drive the SHELL functions out of `run_all_versions.sh` rather than
reimplementing them in Python. A Python twin would be a second implementation and
would agree with itself; the house rule asks for two observers of one implementation.
"""

import pathlib
import re
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[2]
RUNNER = REPO / "test" / "run_all_versions.sh"


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


def _call(name, *args, mutate=None):
    """Run one of the runner's functions and return (stdout, returncode)."""
    body = _extract(name)
    if mutate:
        body = mutate(body)
    # `set -o pipefail` is not decoration: harness_selftest.sh runs under it, and a
    # pipeline inside one of these functions behaves differently with it on. Driving
    # them without it would test a shell the harness never uses.
    script = ("set -uo pipefail\n" + body + "\n"
              + " ".join([name] + [f'"{a}"' for a in args]) + "\n")
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    return r.stdout, r.returncode


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return str(p)


# ---- the declaration reader -------------------------------------------------


def test_a_suite_that_calls_pgc_summary_declares_accounting(tmp_path, expect):
    """The property is the CALL. A mention is not one, and neither is a longer name.

    The easy wrong implementation is a bare grep, and it reads a suite that only
    explains why it cannot account as though it does -- a claim satisfied by prose,
    which is the failure this whole approach exists to refuse.
    """
    calls = _write(tmp_path, "calls.sh", '. lib.sh\ncheck "x" a a\npgc_summary\n')
    silent = _write(tmp_path, "silent.sh", ". lib.sh\necho hi\nexit 0\n")
    noted = _write(tmp_path, "noted.sh", ". lib.sh\n# cannot call pgc_summary here\nexit 0\n")
    longer = _write(tmp_path, "longer.sh", ". lib.sh\npgc_summary_of_something\n")

    expect.text(_call("pgc_suite_declares_accounting", calls)[0].strip(), "yes",
                "a suite that calls pgc_summary declares accounting")
    expect.text(_call("pgc_suite_declares_accounting", silent)[0].strip(), "no",
                "one that never calls it does not")
    expect.text(_call("pgc_suite_declares_accounting", noted)[0].strip(), "no",
                "a comment mentioning it is not a declaration")
    expect.text(_call("pgc_suite_declares_accounting", longer)[0].strip(), "no",
                "a longer name containing it is not a declaration")
    # An ABSENT file is its own answer. Reported by OffgridwithJD reviewing #922:
    # folding it into "no" classifies a registered suite whose .sh has vanished as
    # exempt, and the reconciliation then reads clean.
    expect.text(_call("pgc_suite_declares_accounting", str(tmp_path / "gone.sh"))[0].strip(),
                "absent", "an absent file is reported absent, not exempt")

    # The stripper follows the shell's rule -- a hash starts a comment at line
    # start or after whitespace -- so a hash inside a word cannot hide the call.
    inword = _write(tmp_path, "inword.sh", ". lib.sh\nX=a#b; pgc_summary\n")
    trailing = _write(tmp_path, "trailing.sh", ". lib.sh\npgc_summary  # trailing\n")
    indented = _write(tmp_path, "indented.sh", ". lib.sh\n  # pgc_summary here only\nexit 0\n")
    expect.text(_call("pgc_suite_declares_accounting", inword)[0].strip(), "yes",
                "a hash inside a word does not hide the call after it")
    expect.text(_call("pgc_suite_declares_accounting", trailing)[0].strip(), "yes",
                "a trailing comment after the call does not hide it")
    expect.text(_call("pgc_suite_declares_accounting", indented)[0].strip(), "no",
                "an indented comment is still a comment")


# ---- the observation reader -------------------------------------------------


def test_the_accounting_line_is_read_on_every_exit_path(tmp_path, expect):
    """`pgc_summary` prints it before pass, failure, skip and incomplete alike.

    That is what makes it the runtime twin of the declaration rather than a synonym
    for PASSED: a suite that failed still reached its summary and still accounted.
    """
    shapes = {
        "pass": "accounting: 3 passed + 0 failed + 0 unrunnable + 0 skipped = 3\nx.sh: PASSED\n",
        "fail": "accounting: 1 passed + 2 failed + 0 unrunnable + 0 skipped = 3\nx.sh: FAILED\n",
        "skip": "accounting: 0 passed + 0 failed + 0 unrunnable + 0 skipped = 0\nx.sh: SKIPPED (ran no checks)\n",
        "inc": "accounting: 2 passed + 0 failed + 1 unrunnable + 0 skipped = 3\nx.sh: INCOMPLETE\n",
    }
    for shape, text in shapes.items():
        log = _write(tmp_path, f"{shape}.log", text)
        expect.text(_call("pgc_log_shows_accounting", log)[0].strip(), "yes",
                    f"a {shape} log shows accounting")

    bare = _write(tmp_path, "bare.log", "x.sh: PASSED\n")
    prose = _write(tmp_path, "prose.log", "this suite prints accounting: in prose\n")
    expect.text(_call("pgc_log_shows_accounting", bare)[0].strip(), "no",
                "a PASSED claim without the line shows none")
    expect.text(_call("pgc_log_shows_accounting", prose)[0].strip(), "no",
                "and prose containing the word is not the line")
    expect.text(_call("pgc_log_shows_accounting", str(tmp_path / "gone.log"))[0].strip(),
                "no", "an absent log shows none rather than erroring")

    # THE ^ ANCHOR, which nothing above exercises: the prose fixture is refused by
    # the regex SHAPE, not by the anchor, so removing ^ from the reader left every
    # arm here green. Reported by OffgridwithJD. The distinguishing input is a
    # well-formed accounting line that does not start its line.
    indented = _write(tmp_path, "indented.log",
                      "  accounting: 3 passed + 0 failed + 0 unrunnable + 0 skipped = 3\nx.sh: PASSED\n")
    expect.num(pathlib.Path(indented).read_text()
               .count("accounting: 3 passed + 0 failed + 0 unrunnable + 0 skipped = 3"), 1,
               "premise: the fixture carries a well-formed line, just indented")
    expect.text(_call("pgc_log_shows_accounting", indented)[0].strip(), "no",
                "an accounting line that does not start its line is refused")

    # THE PRODUCER, ON EVERY EXIT PATH (#1339). The logs above are typed, so they show
    # the reader accepts four shapes, not that `pgc_summary` prints the line on four
    # paths. With the line printed only when nothing failed, every arm above passed.
    # So drive a real suite down each path and feed the reader what it printed.
    bodies = {
        "PASSED": 'check "x" a a',
        "FAILED": 'check "x" a b',
        "SKIPPED": ":",
        "INCOMPLETE": 'check_unrunnable "x" UNSUPPORTED_MAJOR "probe"',
    }
    for verdict, body in bodies.items():
        suite = tmp_path / f"real_{verdict}.sh"
        suite.write_text(f'. "{REPO / "test" / "lib.sh"}"\n{body}\npgc_summary\n')
        out = subprocess.run(["bash", str(suite)], capture_output=True, text=True).stdout
        log = _write(tmp_path, f"real_{verdict}.log", out)
        expect.num(len(re.findall(rf"^real_{verdict}\.sh: {verdict}", out, re.M)), 1,
                   f"the real suite reached its {verdict} exit")
        expect.text(_call("pgc_log_shows_accounting", log)[0].strip(), "yes",
                    f"and the reader finds the accounting line it printed on the way")


def test_the_reader_accepts_the_line_the_producer_actually_emits(tmp_path, expect):
    """Every log above is a literal typed into this file, and the shell half types the
    same four again, and the format string itself lives a third time in `pgc_summary`.

    Three hand-written copies of one line. A wording drift in the PRODUCER leaves both
    harnesses green while the reader answers "no" for every real suite -- which would
    redden the whole matrix on both majors, having passed its own tests. Found by
    OffgridwithJD, who measured it: one realistic rewording of lib.sh flips the reader
    on a real log with no arm going red.

    So run a REAL suite and feed the reader its actual stdout. This is the only arm in
    the file that survives a change to the format.
    """
    suite = tmp_path / "real.sh"
    suite.write_text(f'. "{REPO / "test" / "lib.sh"}"\ncheck "x" a a\npgc_summary\n')
    log = tmp_path / "real.log"
    r = subprocess.run(["bash", str(suite)], capture_output=True, text=True)
    log.write_text(r.stdout)

    expect.num(r.stdout.count("\naccounting: "), 1,
               "the real suite produced exactly one accounting line")
    expect.text(_call("pgc_log_shows_accounting", str(log))[0].strip(), "yes",
                "the reader accepts the line the producer actually emits")

    # The control that the arm is not simply insensitive.
    drifted = tmp_path / "drifted.log"
    drifted.write_text(r.stdout.replace("\naccounting: ", "\naccounting summary: "))
    expect.num(drifted.read_text().count("\naccounting: "), 0,
               "premise: the drift changed the line the reader looks for")
    expect.text(_call("pgc_log_shows_accounting", str(drifted))[0].strip(), "no",
                "and a reworded producer line is refused, so the arm can fail")


# ---- the reconciliation -----------------------------------------------------


def test_the_reconciliation_names_both_directions(tmp_path, expect):
    """Declared-but-not-accounted and accounted-but-not-declared are opposite faults.

    The first is a suite that died before reaching its summary -- today that reads
    PASS whenever the shell happened to exit 0. The second is a stale reading of the
    source, which is the failure a hand-maintained exempt list can never report.
    """
    d = _write(tmp_path, "d", "alpha\nbeta\ngamma\n")
    o = _write(tmp_path, "o", "alpha\nbeta\ngamma\n")
    expect.num(_call("pgc_reconcile_accounting", d, o)[1], 0, "equal sets reconcile")

    o = _write(tmp_path, "o", "alpha\nbeta\n")
    out, rc = _call("pgc_reconcile_accounting", d, o)
    expect.num(rc, 1, "a declared suite that never accounted is caught")
    expect.num(out.count("declared but never accounted: gamma"), 1, "and is named")

    d = _write(tmp_path, "d", "alpha\nbeta\n")
    o = _write(tmp_path, "o", "alpha\nbeta\ngamma\n")
    out, rc = _call("pgc_reconcile_accounting", d, o)
    expect.num(rc, 1, "an undeclared suite that DID account is caught")
    expect.num(out.count("accounted but never declared: gamma"), 1,
               "and is named as the opposite fault")


def test_opposite_errors_do_not_cancel(tmp_path, expect):
    """One error masking the other is exactly what a count cannot distinguish."""
    d = _write(tmp_path, "d", "alpha\ndelta\n")
    o = _write(tmp_path, "o", "alpha\ngamma\n")
    out, rc = _call("pgc_reconcile_accounting", d, o)
    expect.num(rc, 1, "a run wrong in both directions fails")
    expect.num(out.count("declared but never accounted: delta")
               + out.count("accounted but never declared: gamma"), 2,
               "and both directions are reported, not one")


def test_the_driver_s_own_non_dispatch_record_excuses_only_what_it_names(tmp_path, expect):
    """PGC_SKIP_TIMING drops four suites on every CI run.

    They declare accounting and correctly produce none, because nothing ran them.
    Without a term for that the check goes red for the one reason that is not a
    defect. The term is recorded by the branch that makes the decision rather than
    inferred from the log that branch forges.
    """
    d = _write(tmp_path, "d", "alpha\nbeta\ngamma\n")
    o = _write(tmp_path, "o", "alpha\nbeta\n")
    nd = _write(tmp_path, "nd", "gamma\n")
    expect.num(_call("pgc_reconcile_accounting", d, o, nd)[1], 0,
               "a suite the driver never dispatched reconciles")
    expect.num(_call("pgc_reconcile_accounting", d, o)[1], 1,
               "and without that record the same run is still caught")

    # A suite cannot both have reached its summary and not have been dispatched.
    d = _write(tmp_path, "d", "alpha\nbeta\n")
    o = _write(tmp_path, "o", "alpha\nbeta\n")
    nd = _write(tmp_path, "nd", "beta\n")
    out, rc = _call("pgc_reconcile_accounting", d, o, nd)
    expect.num(rc, 1, "a suite both accounted and recorded as never dispatched fails")
    expect.num(out.count("both accounted and recorded as never dispatched: beta"), 1,
               "and is named as that fault rather than one of the other two")

    d = _write(tmp_path, "d", "alpha\n")
    o = _write(tmp_path, "o", "alpha\n")
    nd = _write(tmp_path, "nd", "zeta\n")
    out, _ = _call("pgc_reconcile_accounting", d, o, nd)
    expect.num(out.count("accounted but never declared: zeta"), 1,
               "the record cannot introduce a suite the source never declared")


def test_the_printed_identity_can_actually_fail(tmp_path, expect):
    """inputs == sum(buckets) is printed beside every reconciliation. Can it be false?

    Measured rather than argued. Computing inputs FROM the buckets makes the line
    P + D + O == P + D + O and nothing reddens -- which is why inputs is counted from
    the two files by a separate route. Dropping the sort before comm makes the buckets
    garbage and the totals diverge, and that is the fault this identity guards:
    selftest 070's subject arriving in a second place.
    """
    unsorted = lambda b: (b
                          .replace('LC_ALL=C sort -u "$_decl" 2>/dev/null',
                                   'cat "$_decl" 2>/dev/null')
                          .replace('LC_ALL=C sort -u "$_obs"  2>/dev/null',
                                   'cat "$_obs" 2>/dev/null'))

    body = _extract("pgc_reconcile_accounting")
    expect.num(body.count('LC_ALL=C sort -u "$_decl"'), 1,
               "premise: the real function sorts its declared side")
    expect.num(unsorted(body).count('LC_ALL=C sort -u "$_decl"'), 0,
               "premise: the mutation applied -- the twin no longer does")

    d = _write(tmp_path, "d", "gamma\nbeta\nalpha\n")
    o = _write(tmp_path, "o", "delta\ngamma\nbeta\n")
    out_twin, _ = _call("pgc_reconcile_accounting", d, o, mutate=unsorted)
    out_real, _ = _call("pgc_reconcile_accounting", d, o)
    expect.num(out_twin.count("does not add up"), 1,
               "the identity catches comm reading unsorted input")
    expect.num(out_real.count("does not add up"), 0,
               "and does not fire on the same input unmutated, so the arm is not noise")


# ---- the readers, over the real population ----------------------------------


def test_the_partition_over_the_registered_suites_adds_up(expect):
    """A reader that works on fixtures and not on the 251 registered suites has been
    tested against the world it was written for.

    No count is asserted. How many suites are exempt is not a fact about correctness,
    and pinning it here would be a second copy of the hand-maintained list this design
    exists to remove. What is asserted is that the partition covers the population and
    that both buckets are occupied -- a reader answering the same way for everything
    would satisfy every fixture above.
    """
    listed = subprocess.run(["bash", str(RUNNER), "--list-suites"],
                            capture_output=True, text=True).stdout.split()
    expect.at_least(len(listed), 1, "premise: the runner listed its suites")

    body = _extract("pgc_suite_declares_accounting")
    script = body + '\nfor f in "$@"; do pgc_suite_declares_accounting "$f"; done\n'
    paths = [str(REPO / "test" / f"{s}.sh") for s in listed]
    verdicts = subprocess.run(["bash", "-c", script, "_"] + paths,
                              capture_output=True, text=True).stdout.split()

    yes, no = verdicts.count("yes"), verdicts.count("no")
    absent = verdicts.count("absent")
    print(f"  registered={len(listed)} | declares={yes}, does not={no}, "
          f"absent={absent} | sum={yes + no + absent}")
    # A COVERAGE check, and only that. It fails when the classification does not
    # see every registered suite -- a dropped path, a list that changed between
    # the two reads. It is NOT a check on the reader's correctness: the two arms
    # below, which require both buckets occupied, are what catch a reader that
    # answers the same way for everything. The population is counted from the
    # runner's own list, a different route from the verdicts.
    expect.num(yes + no + absent, len(listed),
               "the partition covers every registered suite")
    expect.num(absent, 0, "every registered suite has a file")
    expect.at_least(no, 1, "the reader does not answer yes for every suite")
    expect.at_least(yes, 1, "nor no for every one of them")


def test_the_declaration_reader_survives_pipefail_on_a_long_suite(tmp_path, expect):
    """A regression arm for a bug this file's first implementation shipped.

    It piped `sed` into `grep -q`. grep -q exits the moment it matches, closing the
    pipe while sed is still writing; sed takes EPIPE, and under `set -o pipefail` the
    pipeline reports that failure even though grep matched. The reader then answered
    "no" for a suite that plainly calls pgc_summary.

    It is a race, so it reproduces on long files and not short ones -- which is why it
    passed every fixture above and failed only on the real population, naming the two
    longest suites. Selftest 040 carries the same story from #473 and #476.
    """
    big = tmp_path / "big.sh"
    big.write_text(". lib.sh\npgc_summary\n" + "".join(f"echo padding {i}\n" for i in range(40000)))
    expect.at_least(len(big.read_text().splitlines()), 10000,
                    "premise: the fixture is long enough to lose the race")

    expect.text(_call("pgc_suite_declares_accounting", str(big))[0].strip(), "yes",
                "a long suite that calls pgc_summary still declares accounting")

    # The arm can fail: the grep -q shape is the one that gets this wrong. Restated
    # here rather than extracted, because the wrong version is no longer in the tree.
    twin = ("set -uo pipefail\n"
            "sed 's/#.*$//' \"$1\" | grep -qE '(^|[^_[:alnum:]])pgc_summary([^_[:alnum:]]|$)'"
            " && echo yes || echo no\n")
    got = subprocess.run(["bash", "-c", twin, "_", str(big)],
                         capture_output=True, text=True).stdout.strip()
    # Premises: a restated shape run by bash and grep, which no change here can redden.
    expect.text(got, "no",
                "premise: the grep -q shape is the one that gets this wrong under pipefail")

    short = tmp_path / "short.sh"
    short.write_text(". lib.sh\npgc_summary\n")
    got_short = subprocess.run(["bash", "-c", twin, "_", str(short)],
                               capture_output=True, text=True).stdout.strip()
    expect.text(got_short, "yes",
                "premise: and it agrees on a SHORT file, which is why it survived review")


# ---- the population, which the symmetry check cannot see --------------------


def test_the_accounted_reader_takes_either_runtime_mechanism(tmp_path, expect):
    """Two mechanisms exist in the tree, and both are runtime-observable.

    `pgc_summary` prints the accounting line and 239 suites use it. `bench_guards`
    and `docs_style` keep private counters and print their own `checks run:`; they
    never source `lib.sh`, so a reader that only knows the first would call them
    unaccounted, which is false. Both are derived rather than declared, so a suite
    that adopts either leaves the debt bucket on its own.
    """
    lib = _write(tmp_path, "lib.log",
                 "accounting: 1 passed + 0 failed + 0 unrunnable + 0 skipped = 1\nx.sh: PASSED\n")
    own = _write(tmp_path, "own.log", "checks run: 9\ndocs_style.sh: PASSED\n")
    neither = _write(tmp_path, "none.log", "some output\nPASSED\n")
    expect.text(_call("pgc_log_shows_any_accounting", lib)[0].strip(), "yes",
                "a log carrying lib.sh's accounting line is accounted")
    expect.text(_call("pgc_log_shows_any_accounting", own)[0].strip(), "yes",
                "and a log carrying only its own checks-run line is too")
    expect.text(_call("pgc_log_shows_any_accounting", neither)[0].strip(), "no",
                "a log carrying neither is not accounted")
    # THE ANCHORS (#1339). "neither" is prose, refused by the regex's shape, so
    # dropping ^ and $ from either alternative left this test green. Each input below
    # is a well-formed line that is not the line.
    for label, text in (
            ("an indented checks-run line", "  checks run: 9\n"),
            ("a checks-run line with more after the count", "checks run: 9 of 12\n"),
            ("an indented accounting line",
             "  accounting: 1 passed + 0 failed + 0 unrunnable + 0 skipped = 1\n")):
        log = _write(tmp_path, label.replace(" ", "_") + ".log", text)
        expect.text(_call("pgc_log_shows_any_accounting", log)[0].strip(), "no",
                    f"{label} is not accounted")


def test_a_registered_suite_accounted_by_nothing_fails_by_name(tmp_path, expect):
    """The defect @linuxhikerpm blocked on, stated as its own arm.

    `pgc_reconcile_accounting` takes the declared and observed sets, both derived
    from the suites themselves, so a registered suite in neither is outside the
    universe it reconciles -- with all its inputs empty it reports complete
    symmetry and returns 0, whatever SUITES holds. Treating absence of a
    declaration as absence from the population preserves the overcount.
    """
    reg = _write(tmp_path, "reg", "alpha\n")
    acct = _write(tmp_path, "acct", "")
    nd = _write(tmp_path, "nd", "")
    debt = _write(tmp_path, "debt", "")
    out, rc = _call("pgc_reconcile_population", reg, acct, nd, debt)
    expect.num(rc, 1, "a registered suite accounted by nothing fails")
    expect.num(out.count("registered but accounted by nothing: alpha"), 1,
               "and is named, which the symmetry check could never do")

    # Each way out must actually let it out, or the bucket is a name for
    # "always fails" and only the debt file is doing any work.
    for label, f in (("accounted", acct), ("not dispatched", nd), ("known debt", debt)):
        pathlib.Path(acct).write_text("")
        pathlib.Path(nd).write_text("")
        pathlib.Path(debt).write_text("")
        pathlib.Path(f).write_text("alpha\n")
        expect.num(_call("pgc_reconcile_population", reg, acct, nd, debt)[1], 0,
                   f"a suite that is {label} passes")


def test_the_debt_file_excuses_only_what_it_names(tmp_path, expect):
    """Recording debt by NAME rather than as a count is what makes this a
    burn-down: a new unaccounted suite must fail while the known ones are excused,
    and debt that is no longer debt must be reported so it cannot stall."""
    reg = _write(tmp_path, "reg", "alpha\nbeta\n")
    acct = _write(tmp_path, "acct", "")
    nd = _write(tmp_path, "nd", "")
    debt = _write(tmp_path, "debt", "alpha\n")
    out, rc = _call("pgc_reconcile_population", reg, acct, nd, debt)
    expect.num(out.count("registered but accounted by nothing: beta"), 1,
               "a NEW unaccounted suite fails while the known debt is excused")
    expect.num(out.count("registered but accounted by nothing: alpha"), 0,
               "and the excused one is not named as a failure")
    expect.num(rc, 1, "and the new one fails the reconciliation, not only prints")

    reg = _write(tmp_path, "reg", "alpha\n")
    acct = _write(tmp_path, "acct", "alpha\n")
    debt = _write(tmp_path, "debt", "alpha\n")
    out, rc = _call("pgc_reconcile_population", reg, acct, nd, debt)
    expect.num(out.count("listed as debt but now accounts: alpha"), 1,
               "a suite that now accounts but is still listed as debt is reported")
    # REPORTED, NOT FATAL: the runner says a burn-down that reddens the gate when
    # someone fixes something teaches people not to (#1339).
    expect.num(rc, 0, "and reporting it does not fail the reconciliation")

    debt = _write(tmp_path, "debt", "gone\n")
    out, rc = _call("pgc_reconcile_population", reg, acct, nd, debt)
    expect.num(out.count("listed as debt but not registered: gone"), 1,
               "and debt naming a suite that is not registered is reported")
    expect.num(rc, 0, "and that report does not fail it either")


def test_the_population_partitions_and_prints_its_identity(tmp_path, expect):
    """inputs == sum(buckets) over the REGISTERED population, printed per the house rule.

    It cannot be false on the data: the buckets are built by successive subtraction
    from the registered set, so their sum equals it identically -- 0 firings over 400
    random four-set inputs while the real bucket findings fired on 353. What it guards
    is `comm` reading unsorted input. The arm below it, on the unaccounted bucket, is
    the one that carries weight here.
    """
    reg = _write(tmp_path, "reg", "a\nb\nc\nd\n")
    acct = _write(tmp_path, "acct", "a\n")
    nd = _write(tmp_path, "nd", "b\n")
    debt = _write(tmp_path, "debt", "c\n")
    out, rc = _call("pgc_reconcile_population", reg, acct, nd, debt)
    expect.num(out.count("registered=4 | accounted=1, not dispatched=1, "
                         "known debt=1, unaccounted=1 | sum=4"), 1,
               "the population partitions and prints inputs == sum(buckets)")
    expect.num(rc, 1, "and the one outside every excuse still fails")
    # THE REAL INPUT IS UNSORTED (#1339). `--list-suites` prints SUITES order, and
    # every fixture above was already sorted, so dropping the function's own sort left
    # this test green while comm read unsorted input. The same names, out of order,
    # must partition the same way.
    reg2 = _write(tmp_path, "reg2", "d\nb\na\nc\n")
    out2, _rc2 = _call("pgc_reconcile_population", reg2, acct, nd, debt)
    expect.num(out2.count("registered=4 | accounted=1, not dispatched=1, "
                          "known debt=1, unaccounted=1 | sum=4"), 1,
               "an unsorted registered list partitions as a sorted one does")


def test_the_debt_file_is_tracked_and_holds_only_registered_suites(expect):
    """It is a tracked file so that adding a name is a diff a reviewer sees --
    which is the whole reason it is not a number in the environment."""
    debt = REPO / "test" / "suites_without_accounting.txt"
    expect.text("yes" if debt.exists() else "no", "yes", "the debt file is in the tree")
    # TRACKED, which is what the docstring claims and what makes an added name a diff
    # a reviewer sees. Existing on disk is not that: an untracked copy passed (#1339).
    tracked = subprocess.run(["git", "-C", str(REPO), "ls-files", "--error-unmatch",
                              str(debt.relative_to(REPO))], capture_output=True, text=True)
    expect.num(tracked.returncode, 0, "and git tracks it")
    names = [l.strip() for l in debt.read_text().splitlines()
             if l.strip() and not l.startswith("#")]
    expect.at_least(len(names), 1, "premise: it names some debt to check")

    listed = subprocess.run(["bash", str(RUNNER), "--list-suites"],
                            capture_output=True, text=True).stdout.split()
    strays = sorted(set(names) - set(listed))
    print(f"  debt={len(names)} registered={len(listed)} strays={strays}")
    expect.num(len(strays), 0, "every name in the debt file is a registered suite")
