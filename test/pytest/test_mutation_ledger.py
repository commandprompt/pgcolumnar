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

GREEN = ("RESULT\tdemo\tpart1\tfirst check\tPASS\t18\t\n"
         "RESULT\tdemo\tpart1\tsecond check\tPASS\t18\t\nchecks run: 2\n")
RED = ("RESULT\tdemo\tpart1\tfirst check\tFAIL\t18\t\n"
       "RESULT\tdemo\tpart1\tsecond check\tPASS\t18\t\nchecks run: 2\n")


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
    expect.text(",".join(sorted({r[4] for r in rows})), "never",
                "and records neither as ever having been red")
    expect.num(len([r for r in rows if len(r) != 6]), 0, "every row has six fields")
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
    by = {r[2]: r[5] for r in _rows(ledger)}
    expect.text(by["first check"], "SAOP 128 -> 0",
                "a named mutation is recorded against the check that reddened")
    expect.text(by["second check"], "-", "and not against one that stayed green")

    _run("merge", "--ledger", ledger, "--date", "2026-09-10", "--mutation", "bloom neutered", red)
    expect.text({r[2]: r[5] for r in _rows(ledger)}["first check"],
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
               "RESULT\tdemo\tpart1\tsame\tPASS\t18\t\n"
               "RESULT\tdemo\tpart1\tsame\tFAIL\t18\t\nchecks run: 2\n")
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
                "RESULT\tdemo\tpartA\told A\tFAIL\t18\t\n"
                "RESULT\tdemo\tpartB\tstable B\tPASS\t18\t\nchecks run: 2\n")
    after = _w(tmp_path, "a.log",
               "RESULT\tdemo\tpartA\tnew A\tPASS\t18\t\n"
               "RESULT\tdemo\tpartB\tstable B\tPASS\t18\t\n"
               "RESULT\tdemo\tpartB\tadded B\tPASS\t18\t\nchecks run: 3\n")
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


def test_an_orphan_row_is_named_and_the_unscanned_rows_are_counted(tmp_path, expect):
    """The other direction of the set-compare: a ledger row no record matches.

    `rename-scan` pairs an appearance with a disappearance, so an UNPAIRED
    disappearance printed `vanished=N` and refused nothing -- two rows naming checks
    that no longer existed sat in the committed ledger while the census counted both.

    THE SCOPE IS THE ASSERTION THAT MATTERS. A row in a part the run does not contain
    is not an orphan, because the run cannot speak about it; counting those as present
    would let a one-suite log certify the whole ledger. So the scan says how many rows
    it could not speak about, and this test pins that number rather than only the
    orphan it found.
    """
    ledger = _w(tmp_path, "l.tsv", "")
    before = _w(tmp_path, "b.log",
                "RESULT\tdemo\tpart1\tstill here\tPASS\t18\t\n"
                "RESULT\tdemo\tpart1\tgone tomorrow\tPASS\t18\t\n"
                "RESULT\tdemo\tpartZ\telsewhere\tPASS\t18\t\nchecks run: 3\n")
    after = _w(tmp_path, "a.log",
               "RESULT\tdemo\tpart1\tstill here\tPASS\t18\t\nchecks run: 1\n")
    _run("merge", "--ledger", ledger, "--date", "2026-09-01", before)
    expect.num(len(_rows(ledger)), 3, "premise: the ledger holds all three rows")

    out, rc = _run("orphan-scan", "--ledger", ledger, after)
    expect.num(out.count("orphan: demo\tpart1\tgone tomorrow"), 1,
               "a row no record in its own part matches is named an orphan")
    expect.num(rc, 1, "and it is refused, not merely printed")
    expect.num(out.count("still here"), 0, "the check the run still emits is not an orphan")
    expect.num(out.count("elsewhere"), 0,
               "nor is a row in a part the run does not contain")
    expect.num(out.count("not checked=1"), 1,
               "and the scan states how many rows it could not speak about")

    expect.num(_run("orphan-scan", "--ledger", ledger, before)[1], 0,
               "a run that emits every row in its parts is clean")
    expect.num(_run("orphan-scan", "--ledger", ledger, before, after)[1], 2,
               "a before-log and an after-log together are refused, as rename-scan refuses them")


def test_prune_drops_a_historyless_orphan_and_refuses_one_carrying_history(tmp_path, expect):
    """The catalogue of what has been seen red is what this ledger exists to be.

    No run can recreate it, so dropping an entry because a name moved is the precise
    loss `rename-scan` was written to prevent. `--prune` refuses the WHOLE prune when
    any orphan carries history, rather than removing the safe ones and leaving a
    partial job for whoever reads the output.
    """
    after = _w(tmp_path, "a.log",
               "RESULT\tdemo\tpart1\tstill here\tPASS\t18\t\nchecks run: 1\n")

    plain = _w(tmp_path, "plain.tsv", "")
    _run("merge", "--ledger", plain, "--date", "2026-09-01",
         _w(tmp_path, "p.log",
            "RESULT\tdemo\tpart1\tstill here\tPASS\t18\t\n"
            "RESULT\tdemo\tpart1\tgone tomorrow\tPASS\t18\t\n"
            "RESULT\tdemo\tpartZ\telsewhere\tPASS\t18\t\nchecks run: 3\n"))
    out, rc = _run("orphan-scan", "--prune", "--ledger", plain, after)
    expect.num(out.count("pruned: demo\tpart1\tgone tomorrow"), 1,
               "a historyless orphan is pruned, and named as it goes")
    expect.num(rc, 0, "and a prune that did its job is not an error")
    names = {r[2] for r in _rows(plain)}
    expect.num(len(names), 2, "the ledger is one row shorter")
    expect.num(1 if "elsewhere" in names else 0, 1,
               "control: the row in the part the run never mentioned survives the prune")
    expect.num(_run("orphan-scan", "--prune", "--ledger", plain, after)[1], 0,
               "and a prune with nothing left to remove is clean, not an error")

    hist = _w(tmp_path, "hist.tsv", "")
    _run("merge", "--reds-are-real", "--mutation", "drop the guard", "--ledger", hist,
         "--date", "2026-09-01",
         _w(tmp_path, "h.log",
            "RESULT\tdemo\tpart1\tstill here\tPASS\t18\t\n"
            "RESULT\tdemo\tpart1\tgone tomorrow\tFAIL\t18\t\nchecks run: 2\n"))
    expect.text({r[2]: r[4] for r in _rows(hist)}["gone tomorrow"], "2026-09-01",
                "premise: the orphan now carries a date")

    out, rc = _run("orphan-scan", "--prune", "--ledger", hist, after)
    expect.num(out.count("ORPHAN CARRYING HISTORY"), 1,
               "an orphan carrying history is reported as carrying it")
    expect.num(out.count("last red 2026-09-01"), 1,
               "and the history it would lose is printed with it")
    expect.num(rc, 2, "the prune is refused")
    expect.num(out.count("the catalogue is what this ledger is for"), 1,
               "and it says why, rather than only that it refused")
    expect.num(len([r for r in _rows(hist) if r[2] == "gone tomorrow"]), 1,
               "premise: and the refusal removed NOTHING")


def test_a_part_that_skipped_is_unprunable_because_absence_is_not_removal(tmp_path, expect):
    """The first version of `--prune` deleted a suite.

    One SKIP record put the part in `parts`, so every other row of that suite became an
    orphan, and `--prune` removed them while reporting `not checked=0` and rc=0 -- the
    most confident output the tool can produce. `not checked` protects a part the run does
    not contain; a part CONTAINED BUT SKIPPED WHOLESALE fell in the gap between the two.

    The rule is deliberately broader than that case: a SKIP anywhere in the part means
    some arm did not run, so the run cannot tell a deleted check from one skipped under a
    name that does not match it. One skipped timing check blocks pruning that whole part,
    which is the direction a deleting command should err in.

    The CONTROL is the half that matters: the same two rows must still be pruned when the
    part's record is a PASS, or this is a tool that refuses to prune anything.
    """
    ledger = _w(tmp_path, "l.tsv", "")
    _run("merge", "--ledger", ledger, "--date", "2026-09-01",
         _w(tmp_path, "full.log",
            "RESULT\tdemo\tpart1\tarm one\tPASS\t18\t\n"
            "RESULT\tdemo\tpart1\tarm two\tPASS\t18\t\n"
            "RESULT\tdemo\tpart1\tthe whole thing\tPASS\t18\t\nchecks run: 3\n"))
    expect.num(len(_rows(ledger)), 3, "premise: the ledger holds all three rows")
    skipped = _w(tmp_path, "skipped.log",
                 "RESULT\tdemo\tpart1\tthe whole thing\tSKIP\t18\tno fixture on this box\n"
                 "checks run: 1\n")

    out, rc = _run("orphan-scan", "--ledger", ledger, skipped)
    expect.num(out.count("unprunable: 2 row(s)"), 1,
               "a row in a part that skipped is unprunable, not an orphan")
    expect.num(out.count("orphans=0 (0 carrying history), unprunable=2"), 1,
               "and the summary keeps the two apart")
    expect.num(rc, 1, "it is still a finding, so the scan does not return success")

    _run("orphan-scan", "--prune", "--ledger", ledger, skipped)
    expect.num(len(_rows(ledger)), 3, "--prune removes nothing from a part that skipped")
    expect.num(_run("orphan-scan", "--prune", "--ledger", ledger, skipped)[0]
               .count("the run did not exercise those checks"), 1,
               "and it says so rather than declining silently")

    passed = _w(tmp_path, "pass.log",
                "RESULT\tdemo\tpart1\tthe whole thing\tPASS\t18\t\nchecks run: 1\n")
    out, _ = _run("orphan-scan", "--prune", "--ledger", ledger, passed)
    expect.num(out.count("removed 2 row(s)"), 1,
               "control: the same rows ARE pruned when that part's record is a PASS")
    expect.num(len(_rows(ledger)), 1, "control: and the ledger really is shorter")


def test_the_gate_refuses_a_new_check_only_in_a_suite_it_covers(tmp_path, expect):
    """The suite restriction is the MEANING of `suites_not_covered`, not a softening.

    Without it the gate refuses every check of every uncovered suite and reddens the
    whole matrix on its first run -- a gate somebody turns off within the week, which is
    the failure this issue family exists to prevent. It tightens on its own as suites
    are seeded.
    """
    ledger = _w(tmp_path, "l.tsv", "")
    reg = _w(tmp_path, "reg", "demo\nother\n")
    _run("merge", "--ledger", ledger, "--date", "2026-09-10", _w(tmp_path, "g.log", GREEN))

    other = _w(tmp_path, "o.log", "RESULT\tother\tpartX\tsomething\tPASS\t18\t\nchecks run: 1\n")
    b1 = _w(tmp_path, "b1.txt", "suites_not_covered 1\n")
    out, rc = _run("gate", "--ledger", ledger, "--budget", b1, "--registered", reg, other)
    expect.num(rc, 0, "a check in an uncovered suite is not refused")
    expect.num(out.count("not covered=1"), 1, "but that suite is counted as debt")

    _run("merge", "--ledger", ledger, "--date", "2026-09-10", other)
    other2 = _w(tmp_path, "o2.log",
                "RESULT\tother\tpartX\tsomething\tPASS\t18\t\n"
                "RESULT\tother\tpartX\tnewly added\tPASS\t18\t\nchecks run: 2\n")
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
    expect.text({r[2]: r[4] for r in _rows(ledger)}["newly added"], "never",
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
    never = [r for r in rows if r[4] == "never"]
    red = [r for r in rows if r[4] != "never"]
    print(f"  ledger: inputs={len(rows)} | observed red={len(red)}, never={len(never)}")
    expect.num(len(red) + len(never), len(rows), "the ledger partitions")
    expect.num(len([r for r in rows if len(r) != 6]), 0, "every committed row has six fields")
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


def test_the_ledger_refuses_two_rows_sharing_one_key(tmp_path, expect):
    """The same class one level down, found while building the arm above.

    `read_ledger` does `rows[(f[0], f[1], f[2])] = [...]`, so a duplicated key in the
    TRACKED file collapsed silently and the LAST line won. Measured on a two-line
    fixture, both orders:

        never first, then 2026-09-01   survivor last_red='2026-09-01'
        2026-09-01 first, then never   survivor last_red='never'   <- the red is GONE

    So line order decides whether a recorded red observation survives, and a merge that
    keeps both sides of a changed row can turn `ever red` back into `never`. That is the
    exact corruption #918 and #925 exist to prevent, arriving from the opposite direction
    to #982's.

    NOTHING CAUGHT IT, and bounding the census cannot. `checks_never_observed_red` is a
    CENSUS and the budget file says it must not become a ceiling, because every new check
    enters as `never` and bounding it deadlocks. The gate only checks that the budget's
    number equals the ledger's, and both are derived from the collapsed dict, so they
    agree. Measured: with the budget regenerated alongside, the erasure passes at rc=0.
    """
    dup = _w(tmp_path, "l.tsv",
             "demo\tpart1\ta check\t18\t2026-09-01\tmut-A\n"
             "demo\tpart1\ta check\t18\tnever\t-\n")
    budget = _w(tmp_path, "b.txt", "suites_not_covered 0\n")
    reg = _w(tmp_path, "reg", "demo\n")
    log = _w(tmp_path, "r.log",
             "RESULT\tdemo\tpart1\ta check\tPASS\t18\t\nchecks run: 1\n")
    out, rc = _run("gate", "--ledger", dup, "--budget", budget, "--registered", reg, log)
    expect.num(int("a ledger row repeats a key" in out), 1,
               "a duplicated ledger key is refused by name")
    expect.num(int("a check" in out), 1, "and the row is named")
    expect.num(rc, 2, "as an integrity failure, not a gate verdict")


def test_a_ledger_with_no_duplicate_key_still_loads(tmp_path, expect):
    """The false-positive budget, and the premise the refusal above needs.

    Two rows that differ only in the NAME are two checks and must load, which is the
    ordinary case for every part in the tree.
    """
    ok = _w(tmp_path, "l.tsv",
            "demo\tpart1\tfirst check\t18\tnever\t-\n"
            "demo\tpart1\tsecond check\t18\t2026-09-01\tmut-A\n"
            "demo\tpart2\tfirst check\t18\tnever\t-\n")
    budget = _w(tmp_path, "b.txt", "suites_not_covered 0\n")
    reg = _w(tmp_path, "reg", "demo\n")
    log = _w(tmp_path, "r.log",
             "RESULT\tdemo\tpart1\tfirst check\tPASS\t18\t\nchecks run: 1\n")
    out, rc = _run("gate", "--ledger", ok, "--budget", budget, "--registered", reg, log)
    expect.num(int("a ledger row repeats a key" in out), 0,
               "three distinct keys are not a duplicate")
    expect.num(int("ledger census: rows=3" in out), 1,
               "and all three rows loaded, so the refusal did not eat one")


def test_the_gate_refuses_two_checks_sharing_one_ledger_key(tmp_path, expect):
    """#982's remaining half. `merge` PRINTS this and nothing fails on it.

    A ledger row is keyed on (suite, part, name), so two checks with the same name in
    one part share a row. Neither is mis-recorded while both pass. The hazard is
    conditional and exact: if one goes red the row records `ever red`, and its
    namesake inherits a red observation nothing attacked -- which is the census
    `checks_never_observed_red` exists to make trustworthy.

    THE GATE WAS STRUCTURALLY BLIND TO IT, which is why the note was not enough.
    `cmd_gate` builds its records as `sorted({(s, p, n, m) for ...})`, and a set
    collapses the duplicate before any arm can count it. The same canonicalisation
    that makes the rest of the gate correct made this one class unreachable.

    Measured on main at `03c6c9c8`: a real `harness_selftest` run emits 934 RESULT
    records over 934 distinct keys, 0 collisions, so this refusal is green on the tree
    it lands in. The instance #982 reported was fixed by `c3b13aed`; this is the
    mechanism that stops the next one.
    """
    # THE FIXTURE HAS TO DEFEAT THREE OTHER ROUTES TO rc=1, and an arm asserting rc == 1
    # is worth nothing until it has. Each was measured reaching 1 on its own.
    #
    #   1. THE COVERAGE CEILING. With `suites_not_covered 0` the gate returns 1 for
    #      `suites_not_covered: 1 exceeds the ceiling of 0`. The budget names 1 instead.
    #
    #   2. THE NEW-CHECK REFUSAL, which is the route I missed and @jdatcmd found by
    #      mutation. The ledger must NAME `shared name`, not merely some other check in
    #      the part. With only `some other check` listed, `demo` is a covered suite whose
    #      log carries a check the ledger has never seen, and the pre-existing refusal
    #      sets rc by itself:
    #
    #          not in the ledger: demo   part1   shared name   (on major 18)
    #
    #      Measured: with `rc = 1` deleted from the shared-key block, the old fixture's
    #      arm STILL PASSED and this one fails. That is the whole difference between an
    #      arm about this refusal and an arm about the gate returning 1.
    #
    #   3. AN UNHANDLED EXCEPTION, which also exits 1 -- an earlier draft referenced
    #      `covered_suites` before it was defined, which compiles and fails at runtime.
    #      Hence the `Traceback` arm below.
    #
    # The ledger covering the suite is NOT why it is shaped this way: the refusal applies
    # to every suite, which the next arm asserts.
    ledger = _w(tmp_path, "l.tsv",
                "demo\tpart1\tsome other check\t18\tnever\t-\n"
                "demo\tpart1\tshared name\t18\tnever\t-\n")
    budget = _w(tmp_path, "b.txt", "suites_not_covered 1\n")
    reg = _w(tmp_path, "reg", "demo\n")
    dup = _w(tmp_path, "dup.log",
             "RESULT\tdemo\tpart1\tshared name\tPASS\t18\t\n"
             "RESULT\tdemo\tpart1\tshared name\tPASS\t18\t\n"
             "checks run: 2\n")
    out, rc = _run("gate", "--ledger", ledger, "--budget", budget,
                   "--registered", reg, dup)
    expect.at_least(len(out), 20, "premise: the gate produced output to read")
    expect.num(out.count("one ledger key covers 2 checks"), 1,
               "a key covering two checks in one run is refused, and named")
    expect.num(int("part1" in out), 1, "with the part, since the part is half the key")
    expect.num(rc, 1, "and the gate fails rather than noting it")
    expect.num(int("Traceback" in out), 0,
               "premise: rc came from the refusal, not from a crash")


def test_a_shared_key_is_refused_in_a_suite_the_ledger_does_not_cover(expect, tmp_path):
    """The refusal covers EVERY suite, deliberately unlike the new-check refusal.

    `test_the_gate_refuses_a_new_check_only_in_a_suite_it_covers` is restricted because
    it cannot know which of an uncovered suite's checks are new. This one needs no
    history at all: two records, one key, one log is decidable from the log alone.

    WHY THAT MATTERS rather than being a detail. The ledger covers four suites of 253, so
    a refusal restricted the same way would close the class in four places and leave the
    next collision to arrive in one of the other 249 and sit there until that suite is
    seeded. This arm is what stops the restriction being copied in by habit.

    MEASURED BEFORE WIDENING IT, because a gate that reddens 250 unmeasured suites is a
    gate somebody turns off. A full PG 18 matrix with this refusal armed for every suite:
    247 suites ran, RC=0, ALL VERSIONS PASSED, zero shared keys. Check names are static,
    so one major's matrix measures this class completely rather than sampling it.
    """
    ledger = _w(tmp_path, "l.tsv", "")          # covers NOTHING
    budget = _w(tmp_path, "b.txt", "suites_not_covered 1\n")
    reg = _w(tmp_path, "reg", "demo\n")
    dup = _w(tmp_path, "dup.log",
             "RESULT\tdemo\tpart1\tshared name\tPASS\t18\t\n"
             "RESULT\tdemo\tpart1\tshared name\tPASS\t18\t\n"
             "checks run: 2\n")
    out, rc = _run("gate", "--ledger", ledger, "--budget", budget,
                   "--registered", reg, dup)
    expect.num(out.count("one ledger key covers 2 checks"), 1,
               "a shared key in an uncovered suite is named")
    expect.num(rc, 1, "and refused, with no row in the ledger for that suite")
    expect.num(int("Traceback" in out), 0,
               "premise: rc came from the refusal, not from a crash")
    expect.num(out.count("not in the ledger:"), 0,
               "premise: and not from the new-check refusal, which this suite escapes")


def test_the_same_check_in_two_runs_is_not_a_shared_key(tmp_path, expect):
    """The control the refusal needs, and the distinction it must not lose.

    One check observed on two days is two records for one key and is the NORMAL case --
    it is how the ledger accumulates evidence at all. Only a repeat WITHIN one log is a
    collision. Without this arm the refusal could be written as a count over all logs
    together and would reject every multi-day merge.
    """
    ledger = _w(tmp_path, "l.tsv", "demo\tpart1\tshared name\t18;19\tnever\t-\n")
    budget = _w(tmp_path, "b.txt", "suites_not_covered 0\n")
    reg = _w(tmp_path, "reg", "demo\n")
    one = _w(tmp_path, "a.log",
             "RESULT\tdemo\tpart1\tshared name\tPASS\t18\t\nchecks run: 1\n")
    two = _w(tmp_path, "b.log",
             "RESULT\tdemo\tpart1\tshared name\tPASS\t19\t\nchecks run: 1\n")
    out, rc = _run("gate", "--ledger", ledger, "--budget", budget,
                   "--registered", reg, one, two)
    expect.num(out.count("one ledger key covers"), 0,
               "the same check in two logs is not a shared key")
    expect.num(rc, 0, "so a two-run merge is not refused")


def test_a_clean_run_is_not_refused_for_a_shared_key(tmp_path, expect):
    """The false-positive budget: two DIFFERENT names in one part must pass."""
    ledger = _w(tmp_path, "l.tsv",
                "demo\tpart1\tfirst check\t18\tnever\t-\n"
                "demo\tpart1\tsecond check\t18\tnever\t-\n")
    budget = _w(tmp_path, "b.txt", "suites_not_covered 0\n")
    reg = _w(tmp_path, "reg", "demo\n")
    clean = _w(tmp_path, "c.log", GREEN)
    out, rc = _run("gate", "--ledger", ledger, "--budget", budget,
                   "--registered", reg, clean)
    expect.num(out.count("one ledger key covers"), 0,
               "two distinct names in one part are not a shared key")
    expect.num(rc, 0, "and a clean run passes the gate")


def test_the_gate_refuses_a_census_that_contradicts_its_own_ledger(tmp_path, expect):
    """#952. The gate PRINTED the census and never compared it, so rc=0 on a lie.

    Reporting is not enforcing. The case this catches is a MERGE, not a PR: two PRs
    each rewrote the census from the same base, so the composed tree keeps whichever
    side won the conflict while the ledger takes both sets of rows. Decidable from the
    two inputs alone -- no prior, no `--against` -- which is exactly why it still
    refuses on a merge commit, where the prior is the thing in question.

    Refused in BOTH directions. The census is not a ceiling and this does not make it
    one: a ceiling refuses a rise, and bounding this number deadlocks, as the budget
    file argues. What is refused here is a contradiction.
    """
    ledger = _w(tmp_path, "l.tsv", "")
    reg = _w(tmp_path, "reg", "demo\n")
    log = _w(tmp_path, "g.log", GREEN)
    _run("merge", "--ledger", ledger, "--date", "2026-09-10", log)

    # THE PREMISE, asserted: the numbers below mean nothing unless the ledger really
    # holds two rows and both are `never`.
    rows = _rows(ledger)
    expect.num(len(rows), 2, "premise: the merged ledger holds two rows")
    expect.num(len([r for r in rows if r[4] == "never"]), 2,
               "premise: both entered as never, so this ledger's census is 2")

    ok = _w(tmp_path, "ok.txt", "suites_not_covered 0\nchecks_never_observed_red 2\n")
    out, rc = _run("gate", "--ledger", ledger, "--budget", ok, "--registered", reg, log)
    expect.num(rc, 0, "a census that matches the ledger passes")
    expect.num(out.count("census stated 2, ledger holds 2"), 1,
               "and the agreement is printed, so a reader sees it was compared")

    for claim, why in (("1", "understates"), ("3", "overstates")):
        b = _w(tmp_path, "b%s.txt" % claim,
               "suites_not_covered 0\nchecks_never_observed_red %s\n" % claim)
        out, rc = _run("gate", "--ledger", ledger, "--budget", b, "--registered", reg, log)
        expect.num(rc, 1, "a census that %s the ledger is refused" % why)
        expect.num(out.count("checks_never_observed_red %s, the ledger holds 2" % claim), 1,
                   "and the refusal quotes the claim and the measurement (%s)" % why)

    # ABSENCE IS NOT A MISMATCH, and that is measured rather than preferred: every
    # other gate fixture here and in selftest 410 writes a budget stating only
    # `suites_not_covered`, so refusing on absence would redden about twenty arms
    # testing something else. What stops the COMMITTED budget dropping the field is
    # the separate arm asserting it names both numbers, in both harnesses.
    none = _w(tmp_path, "none.txt", "suites_not_covered 0\n")
    out, rc = _run("gate", "--ledger", ledger, "--budget", none, "--registered", reg, log)
    expect.num(rc, 0, "a budget stating no census is not refused")
    expect.num(out.count("names no checks_never_observed_red"), 1,
               "but the gate says so, so the skip is visible rather than silent")


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
        "a verdict the emitter cannot emit": "RESULT\tdemo\tpart1\ta name\tBOGUS\t18\t\nchecks run: 1\n",
        "an empty check name": "RESULT\tdemo\tpart1\t\tPASS\t18\t\nchecks run: 1\n",
        "a count that disagrees with the records": "RESULT\tdemo\tpart1\ta name\tPASS\t18\t\nchecks run: 2\n",
        "no count at all": "RESULT\tdemo\tpart1\ta name\tPASS\t18\t\n",
    }
    for label, text in cases.items():
        log = _w(tmp_path, "bad.log", text)
        expect.num(_run("merge", "--ledger", led, "--date", "2026-09-10", log)[1], 2,
                   f"{label} is an integrity failure, not a merge")

    # A six-field BOGUS record is a field-count failure, and that is not this
    # arm. The message has to name the verdict or the arm stopped testing it.
    bogus = _w(tmp_path, "bogus.log",
               cases["a verdict the emitter cannot emit"])
    out, _rc = _run("merge", "--ledger", led, "--date", "2026-09-10", bogus)
    expect.num(out.count("'BOGUS'"), 1,
               "and it names the verdict, so the author knows which record")

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
            if len(f) > 4 and f[2] == "first check":
                return f[4]
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
             "RESULT\tdemo\tpart1\tthe target\tFAIL\t18\t\n"
             "RESULT\tdemo\tpart1\tcollateral\tFAIL\t18\t\nchecks run: 2\n")
    out, rc = _run("merge", "--ledger", led, "--date", "2026-09-10", "--mutation", "M", two)
    expect.num(rc, 2, "--mutation across two failing checks in one run is refused")
    expect.num(out.count("2 checks failed"), 1,
               "and the refusal counts them, so the author can narrow the run")
    expect.num(_run("merge", "--reds-are-real", "--ledger", led, "--date", "2026-09-10", two)[1], 0,
               "control: the same log merges with a different reason, so the\n                  refusal above is --mutation-across-two-checks and not the log")

    one = _w(tmp_path, "one.log", RED)
    _run("merge", "--ledger", led, "--date", "2026-09-10", "--mutation", "M", one)
    rows = [l.split("\t") for l in pathlib.Path(led).read_text().splitlines() if l.strip()]
    tagged = [r[2] for r in rows if len(r) > 5 and "M" in r[5].split(";")]
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


def test_a_record_that_does_not_name_its_major_is_not_evidence(tmp_path, expect):
    """A log can only be trusted about the major it says it came from (#1010).

    The record carried suite, part, name, verdict and reason, so the major was
    whatever the caller asserted. That is the `--date not-a-date` failure one field
    over: a PG15 log merged as PG18 is misattributed silently, and a ledger whose
    subject is provenance cannot take the major on trust from its invoker.

    A check's EXISTENCE depends on the major -- analyze_differential emits one record
    on PG15-17 and N on PG18+, and fk_referencing's two branches emit different check
    NAMES -- so the major is not decoration on the record, it is part of what the
    record identifies.
    """
    ledger = _w(tmp_path, "l.tsv", "")
    budget = _w(tmp_path, "b.txt", "suites_not_covered 0\n")
    reg = _w(tmp_path, "reg", "demo\n")

    # The OLD six-field record. It has to stop reconciling, or the new field is
    # optional and a log without it keeps being merged on the caller's word.
    old = _w(tmp_path, "old.log",
             "RESULT\tdemo\tpart1\tfirst check\tPASS\t\nchecks run: 1\n")
    out, rc = _run("gate", "--ledger", ledger, "--budget", budget,
                   "--registered", reg, old)
    expect.num(rc, 2, "a record with no major field is an integrity failure")
    expect.num(out.count("major"), 1, "and the message says the major is what is missing")

    # A major that is not a major. Refused for the same reason a free-form --date was:
    # stored verbatim, it becomes an attribution nothing measured.
    for bad in ("eighteen", "18.2", "", "pg18"):
        log = _w(tmp_path, "bad.log",
                 f"RESULT\tdemo\tpart1\tfirst check\tPASS\t{bad}\t\nchecks run: 1\n")
        out, rc = _run("gate", "--ledger", ledger, "--budget", budget,
                       "--registered", reg, log)
        expect.num(rc, 2, f"the major [{bad}] is not a major, so this log is not evidence")

    # And the good form reconciles, or the arm above proves only that everything fails.
    good = _w(tmp_path, "good.log",
              "RESULT\tdemo\tpart1\tfirst check\tPASS\t18\t\nchecks run: 1\n")
    expect.num(_run("gate", "--ledger", ledger, "--budget", budget,
                    "--registered", reg, good)[1], 1,
               "a record naming its major reaches the gate's own verdict")

    # `unknown` is the emitter's word for a field the harness never set, and it is a REAL
    # case rather than a courtesy: PGC_MAJOR is set in pgc_setup, and 14 suites need no
    # cluster so never call it -- measured, 544 of 6753 records on a full pg18 matrix. It
    # must be accepted and it must stay distinguishable from a number.
    unk = _w(tmp_path, "unk.log",
             "RESULT\tdemo\tpart1\tfirst check\tPASS\tunknown\t\nchecks run: 1\n")
    expect.num(_run("gate", "--ledger", ledger, "--budget", budget,
                    "--registered", reg, unk)[1], 1,
               "a record whose harness never set a major says so and is still evidence")


def test_the_census_reports_the_major_it_read(tmp_path, expect):
    """Read and discarded is indistinguishable from not read at all (#1010).

    `census` is the subcommand that prints what the tool parsed out of a log, and it
    is how a human checks a log before merging it. A major the tool validates and then
    drops cannot be audited, and a field nobody can see is a field that goes wrong
    silently -- measured twice on this tool already (`vanished=N` refusing nothing,
    and a census that was printed but never compared).
    """
    log = _w(tmp_path, "c.log",
             "RESULT\tdemo\tpart1\tfirst check\tPASS\t18\t\n"
             "RESULT\tdemo\tpart1\tsecond check\tFAIL\t18\t\n"
             "RESULT\tdemo\tpart2\tthird check\tPASS\tunknown\t\nchecks run: 3\n")
    out, rc = _run("census", log)
    expect.num(rc, 0, "the census reads a log that names its majors")
    # The FIELD, not a substring. `18` appears inside a check name or a reason just as
    # happily, and a count that matches anywhere is a claim about the whole line.
    majors = [l.split("\t")[4] for l in out.splitlines() if l.count("\t") == 4]
    expect.num(majors.count("18"), 2, "and prints the major for each record that has one")
    expect.num(majors.count("unknown"), 1,
               "and prints `unknown` where the harness set none")


def test_a_rows_major_set_accumulates_rather_than_replacing(tmp_path, expect):
    """A check exists on a SET of majors, and the set is a row's field, not its key (#1010).

    Keying on the major would store one fact once per major. Measured on a full matrix at
    4d7c75ae: 6367 of 6472 checks are identical on PG15 and PG18, so `(major, check)`
    would hold 6472 x 5 = 32,360 rows to express 105 keys' worth of difference -- about
    247 duplicate rows per row that actually differs. The set keeps the key at
    `(suite, part, name)`, which is also what keeps `checks_never_observed_red` counting
    CHECKS, true to its own name.

    The set ACCUMULATES, for the same reason the mutation column does and last-red does:
    merging a PG15 log after a PG18 log must not make the check stop existing on 18. That
    was measured as a plain assignment twice on this tool already (#918).
    """
    ledger = _w(tmp_path, "l.tsv", "")
    r18 = _w(tmp_path, "r18.log",
             "RESULT\tdemo\tpart1\teverywhere\tPASS\t18\t\n"
             "RESULT\tdemo\tpart1\tpg18 only\tPASS\t18\t\nchecks run: 2\n")
    r15 = _w(tmp_path, "r15.log",
             "RESULT\tdemo\tpart1\teverywhere\tPASS\t15\t\nchecks run: 1\n")

    _run("merge", "--ledger", ledger, "--date", "2026-09-12", r18)
    majors = {r[2]: r[3] for r in _rows(ledger)}
    expect.text(majors["pg18 only"], "18", "a check seen once names the one major it was seen on")

    _run("merge", "--ledger", ledger, "--date", "2026-09-12", r15)
    majors = {r[2]: r[3] for r in _rows(ledger)}
    expect.text(majors["everywhere"], "15;18",
                "a second major is ADDED to the set, sorted, not written over the first")
    expect.text(majors["pg18 only"], "18",
                "and a check the second run never mentioned keeps the set it had")
    expect.num(len(_rows(ledger)), 2,
               "two checks are two rows, whatever the majors: the key is not the major")

    # `unknown` is a token in the set like any other, and what a suite needing no cluster
    # emits: PGC_MAJOR is set in pgc_setup and 14 suites never call it.
    runk = _w(tmp_path, "ru.log",
              "RESULT\tdemo\tpart1\teverywhere\tPASS\tunknown\t\nchecks run: 1\n")
    _run("merge", "--ledger", ledger, "--date", "2026-09-12", runk)
    expect.text({r[2]: r[3] for r in _rows(ledger)}["everywhere"], "15;18;unknown",
                "a harness that named no major adds its own token rather than a number")


def test_a_run_speaks_only_for_the_majors_the_row_claims(tmp_path, expect):
    """A PG15 run cannot orphan a check the ledger says exists only on PG18 (#1010).

    This is the direction the missing dimension actually broke, and `gate` is not it: the
    gate refuses a check in the LOG the ledger has not seen, and a PG18-only check does
    not appear in a PG15 log, so the gate stays correct by never being asked.
    `orphan-scan` asks the opposite question and a PG18-only row is exactly what a
    deleted check looks like on PG15.

    Until now it was saved only by the SKIP rule, and that was luck.
    analyze_differential emits a check_skip on PG15-17 so its part was unprunable;
    fk_referencing:287 emits `check` in its older-major branch and has no SKIP at all, so
    once that suite is seeded a PG15 run would call its two PG17+ checks deleted.
    Measured at 4d7c75ae: 24 keys shared, 2 only on PG18, 1 only on PG15.

    The fourth category already says the true thing -- "this run does not contain them, so
    it cannot speak about them" -- so this needs no new category and no grandfather rule.
    """
    # NO SKIP anywhere in the fixture, or the arm proves the wrong mechanism.
    ledger = _w(tmp_path, "l.tsv",
                "demo\tpart1\teverywhere\t15;18\tnever\t-\n"
                "demo\tpart1\tpg18 only\t18\tnever\t-\n"
                "demo\tpart1\tpg15 only\t15\tnever\t-\n")
    log15 = _w(tmp_path, "r15.log",
               "RESULT\tdemo\tpart1\teverywhere\tPASS\t15\t\n"
               "RESULT\tdemo\tpart1\tpg15 only\tPASS\t15\t\nchecks run: 2\n")

    out, rc = _run("orphan-scan", "--ledger", ledger, log15)
    expect.num(out.count("orphan:"), 0,
               "a PG15 run names no orphan: it saw every check the ledger claims for 15")
    expect.num(out.count("ORPHAN CARRYING HISTORY"), 0, "and none carrying history either")
    expect.num(out.count("not checked: 1 row"), 1,
               "the PG18-only row is one this run cannot speak about, not one that vanished")
    expect.num(rc, 0,
               "and a run that cannot see another major's rows is not a failure -- a run "
               "sees ONE major, so otherwise every correct run would return 1")

    # THE CONTROL. Without it the arm above proves only that nothing is ever an orphan.
    log15b = _w(tmp_path, "r15b.log",
                "RESULT\tdemo\tpart1\teverywhere\tPASS\t15\t\nchecks run: 1\n")
    out, rc = _run("orphan-scan", "--ledger", ledger, log15b)
    expect.num(out.count("orphan: demo\tpart1\tpg15 only"), 1,
               "control: a check the ledger claims for 15 and a PG15 run did not emit IS "
               "an orphan")

    # And the row claiming BOTH majors is checked on BOTH, which is the set's whole point:
    # a stronger claim is held to a stronger test.
    log18 = _w(tmp_path, "r18.log",
               "RESULT\tdemo\tpart1\tpg18 only\tPASS\t18\t\nchecks run: 1\n")
    out, rc = _run("orphan-scan", "--ledger", ledger, log18)
    expect.num(out.count("orphan: demo\tpart1\teverywhere"), 1,
               "a row claiming 15;18 is an orphan on a PG18 run that did not emit it")


def test_the_gate_cannot_refuse_a_check_on_a_major_it_has_never_seen(tmp_path, expect):
    """The same argument as suites_not_covered, one dimension over (#1010).

    The gate cannot refuse a new check in a suite it has never seen, because it has no
    idea which of that suite's checks are new. A major it has never seen is the identical
    problem: adding PG20 to the matrix would make EVERY check new on 20 and redden the
    whole run at once -- which is a gate somebody turns off, the failure this issue family
    exists to prevent.

    So the restriction is not a softening, it is the meaning of coverage. It tightens on
    its own the moment one run on that major is merged.
    """
    ledger = _w(tmp_path, "l.tsv", "demo\tpart1\tknown check\t18\tnever\t-\n")
    budget = _w(tmp_path, "b.txt", "suites_not_covered 0\n")
    reg = _w(tmp_path, "reg", "demo\n")

    # PG20: the ledger holds no row naming it, so it cannot say which of these are new.
    log20 = _w(tmp_path, "r20.log",
               "RESULT\tdemo\tpart1\tknown check\tPASS\t20\t\n"
               "RESULT\tdemo\tpart1\tbrand new\tPASS\t20\t\nchecks run: 2\n")
    out, rc = _run("gate", "--ledger", ledger, "--budget", budget, "--registered", reg, log20)
    expect.num(out.count("not in the ledger"), 0,
               "a major with no rows at all refuses nothing, as an uncovered suite does")
    expect.num(rc, 0, "so a first run on a new major is not a failure")
    expect.num(out.count("major 20"), 1, "and the gate says out loud that it covered no rows for 20")

    # THE CONTROL: on a major it HAS seen, a new check is refused. Without this the arm
    # above proves only that the gate refuses nothing at all.
    log18 = _w(tmp_path, "r18.log",
               "RESULT\tdemo\tpart1\tknown check\tPASS\t18\t\n"
               "RESULT\tdemo\tpart1\tbrand new\tPASS\t18\t\nchecks run: 2\n")
    out, rc = _run("gate", "--ledger", ledger, "--budget", budget, "--registered", reg, log18)
    expect.num(out.count("not in the ledger"), 1, "control: on a covered major a new check is named")
    expect.num(rc, 1, "and refused")

    # And a check the ledger knows, seen on a major its row does NOT claim, is refused --
    # the row is a claim about where the check exists, so widening it is a ledger edit.
    led2 = _w(tmp_path, "l2.tsv",
              "demo\tpart1\tknown check\t18\tnever\t-\n"
              "demo\tpart1\tother\t15\tnever\t-\n")
    log15 = _w(tmp_path, "r15.log",
               "RESULT\tdemo\tpart1\tknown check\tPASS\t15\t\n"
               "RESULT\tdemo\tpart1\tother\tPASS\t15\t\nchecks run: 2\n")
    out, rc = _run("gate", "--ledger", led2, "--budget", budget, "--registered", reg, log15)
    expect.num(out.count("known check"), 1,
               "a known check on a major its row does not claim is named, because the row "
               "is a claim about where it exists")
    expect.num(rc, 1, "and refused until the ledger is regenerated")
