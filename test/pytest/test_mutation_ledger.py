"""The mutation ledger: which checks have ever been seen red, and under what.

Nothing recorded whether a check had ever been red. That is the gap that let 39
checks across 35 suites ship unable to fail, three of them inside the suite whose
whole purpose is to stop exactly that.

**What this records.** That a named check *was observed red in a recorded run*. Not
that it is proven able to fail: that needs a named mutation applied deliberately, and
conflating them would put a claim in the ledger nothing measured.

**The first design deadlocked.** Bounding `checks_never_observed_red` means every
added check breaks the gate, because a new check enters as `never` -- so the only way
to land one was to raise a number the design said may only fall. It shipped at 614
rows, 614 never, ceiling 614. It is now a CENSUS, asserted to match the ledger. The
CEILING is `suites_not_covered`, which adding a check does not move, and which the
gate refuses to see raised.

**What the gate refuses** is a check the committed ledger has never seen, in a suite
the ledger covers. Regenerating the ledger is the intended fix and a reviewable diff.

These tests drive the real tool, for the same reason the other files drive the real
shell: a Python twin of a Python tool would agree with itself.
"""

import pathlib
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[2]
TOOL = REPO / "test" / "pgc_ledger.py"
RUNNER = REPO / "test" / "run_all_versions.sh"

GREEN = ("RESULT\tdemo\tpart1\tfirst check\tPASS\t\n"
         "RESULT\tdemo\tpart1\tsecond check\tPASS\t\nchecks run: 2\n")
RED = ("RESULT\tdemo\tpart1\tfirst check\tFAIL\t\n"
       "RESULT\tdemo\tpart1\tsecond check\tPASS\t\nchecks run: 2\n")


def _run(*args, cwd=None):
    r = subprocess.run(["python3", str(TOOL), *args],
                       capture_output=True, text=True, cwd=cwd)
    return r.stdout + r.stderr, r.returncode


def _w(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return str(p)


def _rows(path):
    return [l.split("\t") for l in pathlib.Path(path).read_text().splitlines() if l]


def test_bad_input_is_an_integrity_failure_not_a_clean_run(tmp_path, expect):
    """Every one of these returned rc=0 before.

    `read_records` ignored unreadable files, empty ones and short records, so a gate
    over a NONEXISTENT log reported success. An integrity failure that reads as a
    clean run is worse than no gate, because it certifies. Reported by @linuxhikerpm.
    """
    ledger = _w(tmp_path, "l.tsv", "")
    budget = _w(tmp_path, "b.txt", "suites_not_covered 0\n")
    reg = _w(tmp_path, "reg", "demo\n")
    empty = _w(tmp_path, "empty.log", "")
    short = _w(tmp_path, "short.log", "RESULT\tdemo\tpart1\tname\n")

    for label, log in (("nonexistent", str(tmp_path / "nope.log")),
                       ("empty", empty), ("malformed", short)):
        out, rc = _run("gate", "--ledger", ledger, "--budget", budget,
                       "--registered", reg, log)
        expect.num(rc, 2, f"a {label} log is an integrity failure")
        expect.num(out.count("ledger integrity failure"), 1,
                   f"and the {label} log says what was wrong with it")

    # It must stay distinguishable from a real refusal, or fail-closed just renames
    # every outcome to the same thing.
    good = _w(tmp_path, "g.log", GREEN)
    expect.num(_run("gate", "--ledger", ledger, "--budget", budget,
                    "--registered", reg, good)[1], 1,
               "a real refusal is a different status from an integrity failure")

    # --registered is required: skipping it silently is how a gate reports success
    # for a question it never asked.
    expect.num(_run("gate", "--ledger", ledger, "--budget", budget, good)[1], 2,
               "the gate refuses to run without the registered suite list")


def test_a_green_run_records_debt_and_never_a_red_observation(tmp_path, expect):
    """A green run has seen nothing go red, so merging one must never record a red
    observation -- otherwise an ordinary CI run retires the debt it exists to count."""
    ledger = _w(tmp_path, "l.tsv", "")
    _run("merge", "--ledger", ledger, _w(tmp_path, "g.log", GREEN))
    rows = _rows(ledger)
    expect.num(len(rows), 2, "merging a green run records both checks")
    expect.text(",".join(sorted({r[3] for r in rows})), "never",
                "and records neither as ever having been red")
    expect.num(len([r for r in rows if len(r) != 5]), 0, "every row has five fields")
    expect.num(pathlib.Path(ledger).read_text().count("\t\n"), 0,
               "and no row ends in a tab, which was 614 of them")


def test_the_mutation_column_accumulates_rather_than_overwriting(tmp_path, expect):
    """Last-write-wins records the most recent attack rather than the catalogue the
    column exists to become, which defeats its purpose rather than limiting it.

    And one `--mutation` copied across several logs attributes a deliberate change to
    failures it had nothing to do with. Both reported by @linuxhikerpm.
    """
    ledger = _w(tmp_path, "l.tsv", "")
    red = _w(tmp_path, "r.log", RED)
    _run("merge", "--ledger", ledger, "--date", "2026-09-10", "--mutation", "SAOP 128 -> 0", red)
    by = {r[2]: r[4] for r in _rows(ledger)}
    expect.text(by["first check"], "SAOP 128 -> 0",
                "a named mutation is recorded against the check that reddened")
    expect.text(by["second check"], "-", "and not against one that stayed green")

    _run("merge", "--ledger", ledger, "--date", "2026-09-10", "--mutation", "bloom neutered", red)
    expect.text({r[2]: r[4] for r in _rows(ledger)}["first check"],
                "SAOP 128 -> 0;bloom neutered",
                "a second mutation accumulates rather than replacing the first")

    expect.num(_run("merge", "--ledger", ledger, "--date", "2026-09-10", "--mutation", "X",
                    red, _w(tmp_path, "g.log", GREEN))[1], 2,
               "one mutation cannot be attributed across several runs at once")


def test_two_runs_of_a_check_are_not_a_duplicate_of_it(tmp_path, expect):
    """Merging the logs first cannot tell "the same check in two runs" from "the same
    name twice in one run", and reported the first as the second."""
    ledger = _w(tmp_path, "l.tsv", "")
    g = _w(tmp_path, "g.log", GREEN)
    out, _ = _run("merge", "--ledger", ledger, "--date", "2026-09-10", g, g)
    expect.num(out.count("duplicate"), 0,
               "the same check in two logs is two runs, not a duplicate")

    twice = _w(tmp_path, "twice.log",
               "RESULT\tdemo\tpart1\tsame\tPASS\t\n"
               "RESULT\tdemo\tpart1\tsame\tFAIL\t\nchecks run: 2\n")
    out, _ = _run("merge", "--reds-are-real", "--ledger", _w(tmp_path, "l2.tsv", ""), "--date", "2026-09-10", twice)
    expect.num(out.count("duplicate check name in one run, so one ledger row covers 2: "
                         "demo\tpart1\tsame"), 1,
               "the same name twice in ONE log is a duplicate, and is named")


def test_renames_are_grouped_by_part_and_scanned_against_one_run(tmp_path, expect):
    """A global positional pairing misses a real rename whenever unrelated movement in
    another part shifts the ordering.

    And given a before-log and an after-log together, the vanished name is present in
    the union and nothing appears to have gone -- a scan that silently finds nothing is
    worse than one that refuses.
    """
    ledger = _w(tmp_path, "l.tsv", "")
    before = _w(tmp_path, "b.log",
                "RESULT\tdemo\tpartA\told A\tFAIL\t\n"
                "RESULT\tdemo\tpartB\tstable B\tPASS\t\nchecks run: 2\n")
    after = _w(tmp_path, "a.log",
               "RESULT\tdemo\tpartA\tnew A\tPASS\t\n"
               "RESULT\tdemo\tpartB\tstable B\tPASS\t\n"
               "RESULT\tdemo\tpartB\tadded B\tPASS\t\nchecks run: 3\n")
    _run("merge", "--reds-are-real", "--ledger", ledger, "--date", "2026-09-01", before)

    out, rc = _run("rename-scan", "--ledger", ledger, after)
    expect.num(out.count("possible rename: old A -> new A"), 1,
               "a rename in one part survives an addition in another")
    expect.num(out.count("last red 2026-09-01"), 1,
               "and the history it is about to lose travels with it")
    expect.num(out.count("added B"), 0,
               "the addition in the other part is not called a rename")
    expect.num(rc, 1, "and a detected rename is reported as a nonzero status")

    expect.num(_run("rename-scan", "--ledger", ledger, before, after)[1], 2,
               "a before-log and an after-log together are refused, not silently empty")


def test_the_gate_refuses_a_new_check_only_in_a_suite_it_covers(tmp_path, expect):
    """The suite restriction is the MEANING of `suites_not_covered`, not a softening.

    Without it the gate refuses every check of all 250 uncovered suites and reddens the
    whole matrix on its first run -- a gate somebody turns off within the week, which is
    the failure this issue family exists to prevent. It tightens on its own as suites
    are seeded.
    """
    ledger = _w(tmp_path, "l.tsv", "")
    reg = _w(tmp_path, "reg", "demo\nother\n")
    _run("merge", "--ledger", ledger, "--date", "2026-09-10", _w(tmp_path, "g.log", GREEN))

    other = _w(tmp_path, "o.log", "RESULT\tother\tpartX\tsomething\tPASS\t\nchecks run: 1\n")
    b1 = _w(tmp_path, "b1.txt", "suites_not_covered 1\n")
    out, rc = _run("gate", "--ledger", ledger, "--budget", b1, "--registered", reg, other)
    expect.num(rc, 0, "a check in an uncovered suite is not refused")
    expect.num(out.count("not covered=1"), 1, "but that suite is counted as debt")

    _run("merge", "--ledger", ledger, "--date", "2026-09-10", other)
    other2 = _w(tmp_path, "o2.log",
                "RESULT\tother\tpartX\tsomething\tPASS\t\n"
                "RESULT\tother\tpartX\tnewly added\tPASS\t\nchecks run: 2\n")
    b0 = _w(tmp_path, "b0.txt", "suites_not_covered 0\n")
    out, rc = _run("gate", "--ledger", ledger, "--budget", b0, "--registered", reg, other2)
    expect.num(rc, 1, "once the suite is covered, a new check in it IS refused")
    expect.num(out.count("not in the ledger: other\tpartX\tnewly added"), 1,
               "and it is the new one that is named")
    expect.num(out.count("Regenerate it with"), 1,
               "and the message says how to fix it, because that is the intended action")

    # THE DEADLOCK THAT SHIPPED, as its own arm: adding a check must not require an
    # edit the design forbids.
    _run("merge", "--ledger", ledger, "--date", "2026-09-10", other2)
    expect.num(_run("gate", "--ledger", ledger, "--budget", b0,
                    "--registered", reg, other2)[1], 0,
               "regenerating the ledger lets the new check through")
    expect.text({r[2]: r[3] for r in _rows(ledger)}["newly added"], "never",
                "and it entered as debt, not as an observation nothing made")


def test_the_ceiling_may_only_fall_and_that_is_enforced(tmp_path, expect):
    """The tracked file says the ceiling may only fall. Without a mechanism that
    sentence is prose, and raising the number passed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (["git", "init", "-q", "."], ["git", "config", "user.email", "t@t"],
                ["git", "config", "user.name", "t"]):
        subprocess.run(cmd, cwd=repo, capture_output=True)
    (repo / "b.txt").write_text("suites_not_covered 5\n")
    subprocess.run(["git", "add", "b.txt"], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, capture_output=True)

    prior = subprocess.run(["git", "show", "HEAD:b.txt"], cwd=repo,
                           capture_output=True, text=True).stdout
    expect.num(prior.count("suites_not_covered 5"), 1,
               "premise: the scratch repo has a prior ceiling committed")

    ledger = _w(tmp_path, "l.tsv", "")
    reg = _w(tmp_path, "reg", "demo\n")
    _run("merge", "--ledger", ledger, "--date", "2026-09-10", _w(tmp_path, "g.log", GREEN))
    log = _w(tmp_path, "g2.log", GREEN)

    (repo / "b.txt").write_text("suites_not_covered 9\n")
    out, rc = _run("gate", "--ledger", ledger, "--budget", "b.txt",
                   "--registered", reg, "--against", "HEAD", log, cwd=repo)
    expect.num(rc, 1, "raising the ceiling above its committed value is refused")
    expect.num(out.count("was raised from 5 to 9"), 1, "and the refusal names both values")

    (repo / "b.txt").write_text("suites_not_covered 3\n")
    expect.num(_run("gate", "--ledger", ledger, "--budget", "b.txt",
                    "--registered", reg, "--against", "HEAD", log, cwd=repo)[1], 0,
               "lowering it is allowed, which is the direction the burn-down goes")


def test_the_runner_invokes_the_gate_before_it_removes_the_logs(expect):
    """A gate nothing runs is a comment -- selftest 350's phrasing about its own
    subject. Nothing in the repository called this tool: zero references in `.github/`,
    zero in the runner. Reported by @linuxhikerpm and by OffgridwithJD independently.
    """
    text = RUNNER.read_text().splitlines()
    call = [i for i, l in enumerate(text) if 'pgc_ledger.py" gate' in l]
    teardown = [i for i, l in enumerate(text) if 'rm -rf "$builddir"' in l]
    expect.num(len(call), 1, "the runner invokes the ledger gate exactly once")
    expect.at_least(len(teardown), 1, "premise: the runner removes the build directory")
    expect.text("before" if call[0] < teardown[-1] else "after", "before",
                "and it runs before the logs are removed, the only place it can")
    # The block is extracted, not a fixed-size window: a window's size is a fact
    # about formatting, and the first version measured 8 lines and broke the moment
    # the call site gained a comment.
    src = RUNNER.read_text()
    block = src[src.index("\t\t_led_rc=$?"):]
    block = block[:block.index("\t\tesac") + len("\t\tesac")]
    # Comments stripped: the block's own explanation quotes the sentences counted
    # below, so an unstripped extraction counts the documentation as an occurrence.
    block = "\n".join(l for l in block.splitlines() if not l.strip().startswith("#"))
    expect.num(block.count("verfail=1"), 2,
               "both failure arms fail the major")
    expect.num(block.count("has a check the ledger has never seen"), 1,
               "a refusal keeps the regenerate-the-ledger wording")
    expect.num(block.count("could not run the ledger gate at all"), 1,
               "and an integrity failure gets its own sentence, since regenerating "
               "the ledger would not help")


def test_the_committed_ledger_and_budget_agree(expect):
    """Both are tracked, so a change to either is a diff a reviewer sees. If they
    disagree, one was edited by hand -- the failure this design refuses."""
    ledger = REPO / "test" / "check_ledger.tsv"
    budget = REPO / "test" / "check_ledger_budget.txt"
    expect.text("yes" if ledger.exists() else "no", "yes", "the ledger is in the tree")
    expect.text("yes" if budget.exists() else "no", "yes", "the budget is in the tree")

    rows = [l.split("\t") for l in ledger.read_text().splitlines() if l]
    never = [r for r in rows if r[3] == "never"]
    red = [r for r in rows if r[3] != "never"]
    print(f"  ledger: inputs={len(rows)} | observed red={len(red)}, never={len(never)}")
    expect.num(len(red) + len(never), len(rows), "the ledger partitions")
    expect.num(len([r for r in rows if len(r) != 5]), 0, "every committed row has five fields")
    expect.num(ledger.read_text().count("\t\n"), 0, "and none ends in a tab")

    nums = {}
    for line in budget.read_text().splitlines():
        p = line.split()
        if len(p) == 2 and p[1].isdigit() and not line.startswith("#"):
            nums[p[0]] = int(p[1])
    expect.num(nums.get("checks_never_observed_red", -1), len(never),
               "the committed census matches the committed ledger")

    listed = subprocess.run(["bash", str(RUNNER), "--list-suites"],
                            capture_output=True, text=True).stdout.split()
    covered = {r[0] for r in rows}
    expect.num(nums.get("suites_not_covered", -1), len(set(listed) - covered),
               "and the ceiling matches the suites with no rows")


def test_a_log_that_does_not_parse_is_not_evidence(tmp_path, expect):
    """`len(f) >= 5` accepted four shapes the emitter cannot produce.

    A record missing its reason, a verdict outside pgc_record's vocabulary, an empty
    check name, and one record against `checks run: 2` all merged at rc=0 -- the
    ledger absorbing as evidence a log that does not parse. The ledger's subject is
    which checks have been observed red, so a malformed log is how an observation
    gets attributed to a check that never ran. Reported by @linuxhikerpm on #918.
    """
    led = _w(tmp_path, "l.tsv", "")
    cases = {
        "no reason field": "RESULT\tdemo\tpart1\ta name\tPASS\nchecks run: 1\n",
        "a verdict the emitter cannot emit": "RESULT\tdemo\tpart1\ta name\tBOGUS\t\nchecks run: 1\n",
        "an empty check name": "RESULT\tdemo\tpart1\t\tPASS\t\nchecks run: 1\n",
        "a count that disagrees with the records": "RESULT\tdemo\tpart1\ta name\tPASS\t\nchecks run: 2\n",
        "no count at all": "RESULT\tdemo\tpart1\ta name\tPASS\t\n",
    }
    for label, text in cases.items():
        log = _w(tmp_path, "bad.log", text)
        expect.num(_run("merge", "--ledger", led, "--date", "2026-09-10", log)[1], 2,
                   f"{label} is an integrity failure, not a merge")

    # THE CONTROL. Five arms all reporting 2 prove nothing if the tool has simply
    # started refusing every log.
    good = _w(tmp_path, "good.log", GREEN)
    expect.num(_run("merge", "--ledger", led, "--date", "2026-09-10", good)[1], 0,
               "control: a well-formed log still merges")


def test_last_red_may_only_move_forward(tmp_path, expect):
    """It was a plain assignment, so the answer depended on merge order.

    Merging an older log rewrote a recent observation with an older one, and an
    undated merge replaced a real date with `unknown`. A ledger whose whole subject
    is when a check was last seen red cannot let that regress.
    """
    led = _w(tmp_path, "l.tsv", "")
    red = _w(tmp_path, "red.log", RED)

    def stored():
        for line in pathlib.Path(led).read_text().splitlines():
            f = line.split("\t")
            if len(f) > 3 and f[2] == "first check":
                return f[3]
        return None

    _run("merge", "--reds-are-real", "--ledger", led, "--date", "2026-09-10", red)
    _run("merge", "--reds-are-real", "--ledger", led, "--date", "2026-09-01", red)
    expect.text(stored(), "2026-09-10", "an older observation does not overwrite a newer one")
    _run("merge", "--reds-are-real", "--ledger", led, "--date", "2026-09-20", red)
    expect.text(stored(), "2026-09-20", "and a newer one does")
    _run("merge", "--reds-are-real", "--ledger", led, red)
    expect.text(stored(), "2026-09-20", "and an undated merge does not erase a known date")
    expect.num(_run("merge", "--reds-are-real", "--ledger", led, "--date", "not-a-date", red)[1], 2,
               "a date that is not a date is refused rather than stored")


def test_a_mutation_names_one_check_not_every_casualty(tmp_path, expect):
    """One deliberate change can redden the target and whatever depended on it.

    Attributing `--mutation` to every failure records collateral damage as evidence
    that the mutation kills that check, which is the opposite of what the column is
    for. Measured on #918: a two-FAIL log recorded it against both.
    """
    led = _w(tmp_path, "l.tsv", "")
    two = _w(tmp_path, "two.log",
             "RESULT\tdemo\tpart1\tthe target\tFAIL\t\n"
             "RESULT\tdemo\tpart1\tcollateral\tFAIL\t\nchecks run: 2\n")
    out, rc = _run("merge", "--ledger", led, "--date", "2026-09-10", "--mutation", "M", two)
    expect.num(rc, 2, "--mutation across two failing checks in one run is refused")
    expect.num(out.count("2 checks failed"), 1,
               "and the refusal counts them, so the author can narrow the run")
    expect.num(_run("merge", "--reds-are-real", "--ledger", led, "--date", "2026-09-10", two)[1], 0,
               "control: the same log merges with a different reason, so the\n                  refusal above is --mutation-across-two-checks and not the log")

    one = _w(tmp_path, "one.log", RED)
    _run("merge", "--ledger", led, "--date", "2026-09-10", "--mutation", "M", one)
    rows = [l.split("\t") for l in pathlib.Path(led).read_text().splitlines() if l.strip()]
    tagged = [r[2] for r in rows if len(r) > 4 and "M" in r[4].split(";")]
    expect.rows([[n] for n in sorted(tagged)], [["first check"]],
                "and a single failure still carries it, on the check that reddened")


def test_a_reconciling_log_with_a_red_is_not_evidence_on_its_own(tmp_path, expect):
    """#946: self-consistency is not the property that matters.

    `merge` already refuses a log that does not reconcile. Both of the logs that
    poisoned this ledger on the day it landed RECONCILED: @jdatcmd's was 827 records
    against `checks run: 827` with fifteen checks red because the tree was copied
    without `.git`, and @OffgridwithJD's was one FAIL from an unfinished change. A
    rule about reconciliation would have caught neither.

    An environment red and a real regression are IDENTICAL in the log, so the tool
    cannot tell them apart and must make the caller say which it is -- the same move
    `check_ledger_budget.txt` makes when it names a census apart from a ceiling.
    """
    led = _w(tmp_path, "l.tsv", "")
    red = _w(tmp_path, "red.log", RED)

    out, rc = _run("merge", "--ledger", led, "--date", "2026-09-10", red)
    expect.num(rc, 2, "a log carrying a FAIL is refused when no reason is given")
    expect.at_least(out.count("first check"), 1,
                    "and the refusal names the check that reddened")
    expect.text("absent" if not pathlib.Path(led).read_text().strip() else "written",
                "absent", "and nothing is written, so a refused merge is not half-applied")

    # THE TWO WAYS TO SAY WHY, both of which must still work. Refusing reds outright
    # would refuse the most valuable row the ledger can hold -- a genuine CI red, which
    # has no mutation to name -- and that is the deadlock the budget file already
    # argues against for checks_never_observed_red.
    led2 = _w(tmp_path, "l2.tsv", "")
    expect.num(_run("merge", "--ledger", led2, "--date", "2026-09-10",
                    "--mutation", "M", red)[1], 0,
               "a deliberate break says so with --mutation")
    led3 = _w(tmp_path, "l3.tsv", "")
    expect.num(_run("merge", "--ledger", led3, "--date", "2026-09-10",
                    "--reds-are-real", red)[1], 0,
               "and a genuine observation says so with --reds-are-real")

    # THE CONTROL, without which this arm passes on a tool that refuses everything.
    led4 = _w(tmp_path, "l4.tsv", "")
    green = _w(tmp_path, "green.log", GREEN)
    expect.num(_run("merge", "--ledger", led4, "--date", "2026-09-10", green)[1], 0,
               "control: an all-PASS log still merges with no flag at all")

def test_the_gate_refuses_a_census_that_contradicts_its_ledger(tmp_path, expect):
    """#952: reporting the census is not enforcing it.

    `gate` prints `ledger census: rows=N | never observed red=N` and never
    compares that N to `checks_never_observed_red` in the budget. Measured on
    main: a 20-row ledger with a budget claiming 5 returned rc=0. Found by
    @OffgridwithJD composing #943 and #947: the ledger took both sets of rows
    while the budget kept whichever side won, and the tool certified the lie.

    The census is not a ceiling and must not become one. The refusal is only
    that these two numbers describe the same file and disagree, which is
    decidable from the two inputs with no prior.
    """
    lines = ["demo\tp\tc%02d\tnever\t-\n" % i for i in range(20)]
    ledger = _w(tmp_path, "l.tsv", "".join(lines))
    log = _w(tmp_path, "g.log",
             "".join("RESULT\tdemo\tp\tc%02d\tPASS\t\n" % i for i in range(20))
             + "checks run: 20\n")
    reg = _w(tmp_path, "reg", "demo\n")

    lie = _w(tmp_path, "lie.txt",
              "suites_not_covered 0\nchecks_never_observed_red 5\n")
    out, rc = _run("gate", "--ledger", ledger, "--budget", lie,
                   "--registered", reg, log)
    expect.num(rc, 1, "a budget that understates the ledger census is refused")
    expect.at_least(out.count("5"), 1, "and the refusal names the budget value")
    expect.at_least(out.count("20"), 1, "and it names the ledger value")

    ok = _w(tmp_path, "ok.txt",
             "suites_not_covered 0\nchecks_never_observed_red 20\n")
    expect.num(_run("gate", "--ledger", ledger, "--budget", ok,
                    "--registered", reg, log)[1], 0,
               "control: the same ledger passes when the census matches")

