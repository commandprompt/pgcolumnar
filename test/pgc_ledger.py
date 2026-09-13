#!/usr/bin/env python3
"""The mutation ledger: which checks have ever been seen red, and under what.

Nothing recorded whether a check had ever been red. That is the gap that let 39
checks across 35 suites ship unable to fail, three of them inside the suite whose
whole purpose is to stop exactly that. The gate answered "did anything print FAIL"
and had never answered "could anything print FAIL".

WHAT THIS RECORDS, AND WHAT IT DOES NOT
---------------------------------------
It records that a named check WAS OBSERVED RED in a recorded run. It does NOT
claim the check is proven able to fail: that needs a named mutation applied
deliberately, and conflating the two would put a claim in the ledger that nothing
measured -- the `defeated: 0` shape from VACUITY_MODES section 1.

THE TWO NUMBERS ARE DIFFERENT KINDS OF THING, and the first design got this wrong
in a way that deadlocked.
-------------------------------------------------------------------------------
`checks_never_observed_red` is a CENSUS. It cannot be a ceiling: every new check
enters the ledger as `never`, so bounding it means every added check breaks the
gate, and the only way to land one is to raise a number the design says may only
fall. That is a deadlock, not a budget. It is reported, and it falls as checks are
attacked.

`suites_not_covered` IS a ceiling, and a monotone one, because adding a check to a
covered suite does not move it. It falls as suites are seeded, and it may only
fall: the gate compares the working value against the previously committed one and
refuses an increase, so widening the debt is an edit a reviewer sees AND a gate
refuses, rather than either alone.

THE COMPARISON RUNS BOTH WAYS
-----------------------------
`gate` refuses a check the ledger has never seen. `orphan-scan` reports a ledger
row no record in its own part matches -- the other direction, which for a long
time printed `vanished=N` and refused nothing while two rows named checks that no
longer existed. It reports rather than gates, for a measured reason given in its
own docstring: an absent record does not yet mean a removed check.

WHAT THE GATE REFUSES
---------------------
A check the committed ledger has never seen. That is the allowlist the issue asks
for -- existing checks are grandfathered, a new one is named and refused until the
ledger is regenerated, which is a reviewable one-line diff and the INTENDED action
rather than a forbidden one.

WHAT FEEDS IT
-------------
`run_all_versions.sh` merges every suite's log before it removes the build
directory, so every matrix run feeds a ledger -- locally and in CI. The committed
ledger is updated deliberately, by running `merge` against a real run and
committing the diff. CI verifies; humans commit. A ledger that CI rewrote by
itself would be a file nobody reads changing under everybody.

It is not only mutation runs. Every real CI red fills it, every flake, every
bisect. A mutation run is the deliberate accelerator.

REGENERATING ACROSS A REBASE, in this order, and the order is the point
-----------------------------------------------------------------------
Paid four times on one PR before it was written down. A rebase moves the LEDGER
without moving the BUDGET: git merges both sides' rows into the tsv and keeps one
side's number in the budget, so the committed pair contradicts itself before
anything is run. Four arms then fail and all four trace to that one cause -- one
asserts the pair agrees, three run the real gate, which correctly refuses a
contradiction. Diagnosing it from those four failures costs an hour.

    1. rebase onto the new base FIRST
    2. DERIVE to reconcile       the budget from the merged tsv, before running
    3. run the suite             on the REBASED tree, and guard the log
    4. merge                     the guarded run
    5. prune                     orphans, if any
    6. DERIVE again              the final census, read back from the file

Two derives, not one: step 2 makes the tree self-consistent so the suite can pass at
all, step 6 records the result of steps 4 and 5. Both are READ BACK from the tsv --
`old + n` is right once and wrong every time after.

And re-run whatever your evidence names whose FILES moved in the rebase. A gate
statement is a claim about a tree, and a rebase silently changes which tree; saying
which suites you re-ran and which you did not is part of the claim.

FAIL CLOSED
-----------
An unreadable file, an empty one, or a record with too few fields is an ERROR.
Silently skipping them made every integrity failure indistinguishable from a clean
run: a gate over a nonexistent log returned success.

FORMAT
------
Tab separated, five columns, keyed on the first three:

    suite <TAB> part <TAB> check name <TAB> last observed red <TAB> mutations

`last observed red` is a date or `never`. `mutations` is `-`, or a `;`-separated
SET of the mutations that have reddened this check -- accumulated, not overwritten,
because a column that keeps only the last one records the most recent attack
rather than the catalogue it exists to become.
"""

import argparse
import os
import pathlib
import re
import subprocess
import sys

NEVER = "never"
UNKNOWN = "unknown"

# ISO 8601 date, and nothing else. A free-form string was accepted verbatim, so a
# typo became an observation date the ledger then treated as authoritative --
# measured: `--date not-a-date` stored `not-a-date`.
_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def _newer(current, incoming):
    """The later of two observations, with `unknown` and `never` below every date.

    LAST RED MAY ONLY MOVE FORWARD. It was a plain assignment, so merging an older
    log rewrote a recent observation with an older one, and merging an undated log
    replaced a real date with `unknown` -- both measured on #918, both silently.
    A ledger whose whole subject is "when was this last seen red" cannot let the
    answer regress because of the order somebody merged in.
    """
    rank = {NEVER: 0, UNKNOWN: 1}
    if rank.get(incoming, 2) < rank.get(current, 2):
        return current
    if rank.get(incoming, 2) > rank.get(current, 2):
        return incoming
    return max(current, incoming) if incoming not in rank else current
NONE = "-"

# suite, part, name, MAJORS, last-red, mutations.
#
# THE MAJOR IS A FIELD, NOT PART OF THE KEY (#1010), and that is the whole design. A
# check's EXISTENCE depends on the major -- analyze_differential.sh:61 emits ONE record on
# PG15-17 and N on PG18+, and fk_referencing.sh:287 emits DIFFERENT CHECK NAMES in its two
# branches -- so the ledger has to record WHERE a check exists. It does not follow that the
# major belongs in the key.
#
# Measured on a full matrix at 4d7c75ae, 252 suites on PG15 and PG18: 6367 of 6472 checks
# are identical on both majors and 105 exist on exactly one. Keying on the major would hold
# 6472 x 5 = 32,360 rows to express those 105 -- about 247 duplicate rows for every row
# that differs, each a second copy of one observation, which is the shape
# `a-repeated-measurement-must-be-idempotent` is about.
#
# Keeping the key at (suite, part, name) also keeps `checks_never_observed_red` counting
# CHECKS. Under a (major, check) key it would count PAIRS, and "5800 checks" in a tree with
# 1150 of them is a number that lies by its own name -- the defect check_ledger_budget.txt
# exists to argue against.
#
# A SET, sorted and ";"-separated: "15;18". `unknown` is a token in it like any other, and
# a real case rather than a courtesy: 14 suites need no cluster, so they never call
# pgc_setup -- which is where PGC_MAJOR is set -- and every record they emit carries it.
# Measured on a full pg18 matrix, 544 of 6753 records: audit, concurrency,
# decode_interrupts, hilbert_curve, objstore_stash_recovery, phase2-6, smoke, unique_conc,
# update_conc, wal_envelope. All three suites the ledger covers today DO set it.
#
# NO WILDCARD: "every major observed" would change meaning the day a major is added to the
# matrix, inheriting a claim nothing measured.
FIELDS = 6
MAJOR_SEP = ";"


class LedgerError(Exception):
    """An integrity failure. Never silently skipped."""


# The verdicts pgc_record can emit, and the only ones a record may carry. Kept
# here as the reader's own list rather than derived from lib.sh: a python tool
# reading a shell file to learn its vocabulary is the coupling CONTEXT.md
# forbids, and the drift is caught by the arm that plants each verdict instead.
VERDICTS = ("PASS", "FAIL", "UNRUN", "SKIP")

# RESULT plus suite, part, name, verdict, major, reason.
RECORD_FIELDS = 7

# THE MAJOR A RECORD WAS OBSERVED UNDER, and the one word that means "the harness
# never set one" (#1010).
#
# A check's EXISTENCE depends on the major, so the major is part of what a record
# identifies rather than decoration on it: analyze_differential.sh:61 emits ONE
# record on PG15-17 and N on PG18+, and fk_referencing.sh:287 emits DIFFERENT CHECK
# NAMES in its two branches, so the two majors' key sets are disjoint. A grep for
# PGC_MAJOR does not find all of them either -- native_repack.sh,
# pg19_vacuum_options.sh and native_dml.sh gate on server_version_num and never
# mention it -- which is why this is a field and not a convention.
#
# `unknown` is lib.sh's OWN word for a field the harness did not set, used there for an
# unset suite and part, and it is a REAL case rather than a courtesy: PGC_MAJOR is set in
# pgc_setup, and 14 suites need no cluster so never call it. Measured on a full pg18
# matrix, 544 of 6753 records carry it -- audit, concurrency, decode_interrupts,
# hilbert_curve, objstore_stash_recovery, phase2-6, smoke, unique_conc, update_conc,
# wal_envelope. None of the three suites the ledger covers today is one.
MAJOR_UNKNOWN = "unknown"
_MAJOR = re.compile(r"^(?:[0-9]+|unknown)$")


def read_records(paths, *, require_nonempty=True):
    """[(suite, part, name, verdict, major)] for every RESULT line in the given logs.

    Fails closed, and VALIDATES rather than merely counting. `len(f) < 5` accepted
    a record missing its reason, a verdict outside the emitter's vocabulary, an
    empty check name, and one record against `checks run: 2` -- all measured
    returning 0 while the ledger absorbed them as evidence. Evidence that does not
    parse is not evidence, and the ledger's whole subject is which checks have
    been observed red: a malformed log is how an observation gets attributed to a
    check that never ran. Reported by @linuxhikerpm on #918.

    The reconciliation against `checks run:` is here as well as in the runner
    because the two answer different questions. The runner asks whether the suite
    it just ran was internally consistent; this asks whether a log handed to the
    ledger, possibly from another machine or another day, can be trusted at all.
    """
    out = []
    for p in paths:
        path = pathlib.Path(p)
        try:
            text = path.read_text(errors="replace")
        except OSError as e:
            raise LedgerError(f"cannot read {p}: {e}") from e
        found = 0
        stated = None
        for n, line in enumerate(text.splitlines(), 1):
            m = re.match(r"^checks run: ([0-9]+)$", line)
            if m:
                stated = int(m.group(1))
                continue
            if not line.startswith("RESULT\t"):
                continue
            f = line.split("\t")
            if len(f) != RECORD_FIELDS:
                raise LedgerError(
                    f"{p}:{n}: a record has {RECORD_FIELDS - 1} fields -- suite, part, "
                    f"name, verdict, major, reason; got {len(f) - 1}")
            if not f[1] or not f[2] or not f[3]:
                raise LedgerError(
                    f"{p}:{n}: a record with an empty suite, part or name names no check")
            if f[4] not in VERDICTS:
                raise LedgerError(
                    f"{p}:{n}: verdict {f[4]!r} is not one of {', '.join(VERDICTS)}, "
                    f"so this log was not written by pgc_record")
            # THE MAJOR IS VALIDATED, not stored verbatim. A free-form --date was
            # accepted once and a typo became an authoritative observation date;
            # this is the same field one column over, and the consequence is worse
            # because a major decides WHICH CHECKS CAN EXIST. `eighteen`, `18.2`
            # and `pg18` are not majors, and a log carrying one is not evidence
            # about any major at all.
            if not _MAJOR.match(f[5]):
                raise LedgerError(
                    f"{p}:{n}: major {f[5]!r} is neither a number nor "
                    f"{MAJOR_UNKNOWN!r}, so this record names no version it was "
                    f"observed under")
            out.append((f[1], f[2], f[3], f[4], f[5]))
            found += 1
        if require_nonempty and found == 0:
            raise LedgerError(f"{p}: no RESULT records, so there is nothing to reconcile")
        if found and stated is None:
            raise LedgerError(
                f"{p}: {found} record(s) but no `checks run:` line, so the log never "
                f"reached its summary and cannot be reconciled")
        if stated is not None and found != stated:
            raise LedgerError(
                f"{p}: {found} record(s) against `checks run: {stated}` -- the log does "
                f"not reconcile with itself, so it is not evidence about either number")
    return out


def read_ledger(path):
    """{(suite, part, name): [{majors}, last_red, {mutations}]}.

    Keyed on the part as well as the name: harness_selftest sources 40-odd parts
    into one shell and phrases its premises to be COPIED, so a name-only key is a
    key of check NAMES rather than of checks.

    AND NOT ON THE MAJOR, which is the third field's job instead (#1010). A check's
    existence depends on the major, so the ledger must record WHERE a check exists; it
    does not follow that the major belongs in the key. Measured on a full matrix at
    4d7c75ae, 6367 of 6472 checks are identical on PG15 and PG18, so a (major, check)
    key would hold five copies of one observation for 98% of the file. Keeping the key
    here is also what keeps `checks_never_observed_red` counting CHECKS rather than
    pairs.

    The majors field is a SET and it accumulates in `merge`. `unknown` is a member of
    it like any number: PGC_MAJOR is set in pgc_setup, and 14 suites need no cluster so
    never call it -- 544 of 6753 records on a full pg18 matrix. None of the three suites
    the ledger covers today is one, so every migrated row names real majors; the token
    matters for the suites coverage reaches next.
    """
    rows = {}
    p = pathlib.Path(path)
    if not p.exists():
        return rows
    for n, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        f = line.split("\t")
        if len(f) != FIELDS:
            raise LedgerError(f"{path}:{n}: a ledger row needs {FIELDS} fields, got {len(f)}")
        # EVERY token validated, not just the first. A ledger is hand-edited far more
        # often than a log is generated, so the typed-into file needs the check at least
        # as much. A free-form --date was accepted verbatim once and a typo became an
        # authoritative observation; a major decides which checks can exist.
        majors = {m for m in f[3].split(MAJOR_SEP) if m}
        if not majors:
            raise LedgerError(
                f"{path}:{n}: a row names no major, so it says nothing about where its "
                f"check exists")
        for m in sorted(majors):
            if not _MAJOR.match(m):
                raise LedgerError(
                    f"{path}:{n}: major {m!r} is neither a number nor "
                    f"{MAJOR_UNKNOWN!r}, so this row names no version it applies to")
        muts = set() if f[5] == NONE else {m for m in f[5].split(";") if m}
        key = (f[0], f[1], f[2])
        # A REPEATED KEY, REFUSED RATHER THAN OVERWRITTEN. This was
        # `rows[key] = [...]`, so a duplicated row collapsed silently and the LAST line
        # won. Measured on a two-line fixture, both orders:
        #
        #     never first, then 2026-09-01   survivor last_red='2026-09-01'
        #     2026-09-01 first, then never   survivor last_red='never'  <- the red is GONE
        #
        # So line order decided whether a recorded red observation survived, and a merge
        # that keeps both sides of a changed row turns `ever red` back into `never` --
        # the corruption #918 and #925 exist to prevent.
        #
        # NOTHING ELSE CAN CATCH IT, and bounding the census cannot. The budget file says
        # `checks_never_observed_red` is a CENSUS and must not become a ceiling, because
        # every new check enters as `never` and bounding it deadlocks. The gate compares
        # the budget's number with the ledger's, and both come from this dict, so they
        # agree either way. Measured: with the budget regenerated alongside, an erased red
        # passes the gate at rc=0.
        #
        # The same shape as the shared-key refusal in `cmd_gate`, one level down: there a
        # set hid two checks in one RUN, here a dict hid two rows in one FILE.
        if key in rows:
            raise LedgerError(
                f"{path}:{n}: a ledger row repeats a key already in this file: "
                f"{f[0]}\t{f[1]}\t{f[2]}. One key is one check, and the later row would "
                f"silently replace the earlier -- which loses a recorded red if the later "
                f"row says {NEVER!r}. Keep one row per check.")
        rows[key] = [majors, f[4] or NEVER, muts]
    return rows


def write_ledger(path, rows):
    lines = []
    for (suite, part, name), (majors, red, muts) in sorted(rows.items()):
        # No trailing tab. An empty last field is trailing whitespace on every
        # row, which `git diff --check` reports and which made 614 of them.
        lines.append("\t".join((suite, part, name, MAJOR_SEP.join(sorted(majors)), red,
                                ";".join(sorted(muts)) if muts else NONE)))
    pathlib.Path(path).write_text("\n".join(lines) + ("\n" if lines else ""))


def _by_run(paths):
    """[(path, {(suite, part, name): [(verdict, major)]})] -- one entry per LOG.

    Per log, because the same check appearing in two logs is two RUNS of it, while
    twice in one log is a duplicate name sharing a ledger row. Merging the logs
    first cannot tell those apart, and reported the first as the second.
    """
    runs = []
    for p in paths:
        seen = {}
        for suite, part, name, verdict, major in read_records([p]):
            seen.setdefault((suite, part, name), []).append((verdict, major))
        runs.append((p, seen))
    return runs


def cmd_census(args):
    for suite, part, name, verdict, major in read_records(args.logs):
        print(f"{suite}\t{part}\t{name}\t{verdict}\t{major}")
    return 0


def cmd_merge(args):
    rows = read_ledger(args.ledger)
    runs = _by_run(args.logs)

    if args.date != UNKNOWN and not _DATE.match(args.date):
        raise LedgerError(
            f"--date {args.date!r} is not an ISO date (YYYY-MM-DD). A ledger row's "
            f"last-red is compared against other dates, so a free-form string is not "
            f"an observation")

    if args.mutation and len(runs) > 1:
        raise LedgerError(
            "--mutation names one deliberate change, so it cannot be attributed across "
            f"{len(runs)} logs at once: merge them one run at a time")

    # A MUTATION NAMES ONE CHECK, and a run that mutates one thing can redden
    # several: the target, plus whatever depended on it. Attributing the mutation
    # to every failure records collateral damage as evidence that the mutation
    # kills that check, which is the opposite of what this column is for.
    # Measured on #918: a two-FAIL log merged with --mutation MUTATION_A recorded
    # it against both. Reported by @linuxhikerpm.
    if args.mutation:
        failed = sorted({key for _p, seen in runs for key, vs in seen.items()
                         if any(v == "FAIL" for v, _m in vs)})
        if len(failed) > 1:
            listed = "\n".join(f"      {s}\t{p}\t{n}" for s, p, n in failed[:6])
            more = "" if len(failed) <= 6 else f"\n      ... and {len(failed) - 6} more"
            raise LedgerError(
                f"--mutation names one check, but {len(failed)} checks failed in this "
                f"run:\n{listed}{more}\n    Attributing it to all of them would record "
                f"collateral damage as evidence. Merge without --mutation, or narrow the "
                f"run to the check the mutation targets.")

    # A RED NEEDS A REASON (#946). `merge` already refuses a log that does not
    # RECONCILE, and reconciliation is not the property that matters: both logs that
    # poisoned this ledger on the day it landed reconciled. One was 827 records
    # against `checks run: 827` with fifteen checks red because the tree was copied
    # without `.git`; the other was one FAIL from an unfinished change. Two
    # independent routes on day one, from the two people who knew the tool best, and
    # a third -- a run against a stale .so -- costs no imagination at all.
    #
    # An environment red and a real regression are IDENTICAL in the log. Nothing in a
    # RESULT record says which, so the tool makes the caller assert it rather than
    # guess, the same way check_ledger_budget.txt names a census apart from a ceiling.
    #
    # NOT "refuse FAILs unless --mutation". A genuine CI red is the most valuable row
    # this ledger can hold and it has no mutation to name, so that rule would refuse
    # precisely the entry the ledger exists for -- the deadlock the budget file
    # already argues against for checks_never_observed_red.
    #
    # Refused BEFORE any row is built, so a declined merge is never half-applied.
    if not args.mutation and not args.reds_are_real:
        reddened = sorted({key for _p, seen in runs
                           for key, vs in seen.items()
                           if any(v == "FAIL" for v, _m in vs)})
        if reddened:
            listed = "\n".join(f"      {s}\t{p}\t{n}" for s, p, n in reddened[:6])
            more = "" if len(reddened) <= 6 else f"\n      ... and {len(reddened) - 6} more"
            raise LedgerError(
                f"{len(reddened)} check(s) are red in these logs and nothing says "
                f"why:\n{listed}{more}\n    A log can reconcile perfectly and still "
                f"be evidence about your environment rather than about the code -- a "
                f"tree without .git, an unfinished change, a stale .so. Pass "
                f"--mutation NAME if you broke it deliberately, or --reds-are-real if "
                f"this is a genuine observation of the code under test.")

    for path, seen in runs:
        for key, pairs in sorted(seen.items()):
            verdicts = [v for v, _m in pairs]
            if key not in rows:
                # A check this ledger has never seen enters as DEBT. A green run
                # has observed nothing go red, so merging one must never record a
                # red observation.
                rows[key] = [set(), NEVER, set()]
            # THE MAJOR SET ACCUMULATES, for the reason the mutation column and
            # last-red both do: merging a PG15 log after a PG18 log must not make the
            # check stop existing on 18. A plain assignment was measured doing exactly
            # that to last-red on #918, silently, and the order a human merges logs in
            # is not a fact about the code.
            rows[key][0].update(m for _v, m in pairs)
            if "FAIL" in verdicts:
                rows[key][1] = _newer(rows[key][1], args.date)
                if args.mutation:
                    # A SET. Keeping only the last one records the most recent
                    # attack rather than the catalogue this column exists to
                    # become.
                    rows[key][2].add(args.mutation)
        for key, pairs in sorted(seen.items()):
            verdicts = [v for v, _m in pairs]
            if len(verdicts) > 1:
                print(f"    duplicate check name in one run, so one ledger row covers "
                      f"{len(verdicts)}: {key[0]}\t{key[1]}\t{key[2]}")

    write_ledger(args.ledger, rows)
    seen_all = {k for _, s in runs for k in s}
    red = sum(1 for v in rows.values() if v[1] != NEVER)
    majors = sorted(set().union(*(v[0] for v in rows.values())) if rows else set())
    print(f"  ledger: rows={len(rows)} | runs={len(runs)}, distinct checks this merge={len(seen_all)}, "
          f"observed red ever={red}, never={len(rows) - red}")
    print(f"    majors the ledger now claims rows for: {', '.join(majors) or 'none'}")
    return 0


def cmd_rename_scan(args):
    """A name that appeared while another disappeared, WITHIN ONE PART, is a rename.

    Keyed by the display string, a rename loses history and reads exactly like a
    brand-new check that has never been red -- the one state this ledger exists to
    distinguish. Detected and named rather than silently reset.

    Grouped by (suite, part) before pairing. A global positional zip misses a real
    rename whenever unrelated movement in another part shifts the ordering.

    Scanned against ONE run. Given a before-log and an after-log together, the
    vanished name is present in the union and nothing appears to have gone.
    """
    runs = _by_run(args.logs)
    if len(runs) > 1:
        raise LedgerError(
            f"rename-scan compares ONE run against the ledger, but got {len(runs)} logs: "
            "the union of a before-log and an after-log hides the disappearance")
    rows = read_ledger(args.ledger)
    now = set(runs[0][1])
    # The majors this run actually observed. A row claiming only another major is not a
    # candidate for a rename here: pairing its disappearance with an appearance would
    # report a rename between two checks that were never the same check.
    run_majors = {m for pairs in runs[0][1].values() for _v, m in pairs}

    parts = {(s, p) for s, p, _ in now}
    known = {k for k in rows
             if (k[0], k[1]) in parts and (rows[k][0] & run_majors)}

    rc = 0
    n_app = n_van = 0
    for part in sorted(parts):
        app = sorted(k[2] for k in now - known if (k[0], k[1]) == part)
        van = sorted(k[2] for k in known - now if (k[0], k[1]) == part)
        n_app += len(app)
        n_van += len(van)
        for new, old in zip(app, van):
            was = rows.get((part[0], part[1], old), [set(), NEVER, set()])[1]
            print(f"    possible rename: {old} -> {new} "
                  f"(in {part[0]}/{part[1]}, history: last red {was})")
            rc = 1
    print(f"  rename scan: appeared={n_app}, vanished={n_van}, "
          f"majors this run observed={', '.join(sorted(run_majors)) or 'none'}")
    return rc


def cmd_orphan_scan(args):
    """A ledger row that no record in its OWN PART matches: the unpaired half.

    `rename-scan` pairs an appearance with a disappearance. An unpaired
    disappearance -- a check deleted, or renamed in a run where nothing appeared --
    was printed as `vanished=N` and refused nothing. Two such rows sat in the
    committed ledger naming checks that no longer existed; the census counted both,
    and every run returned 0 while the note scrolled past. A guard that compels one
    list and ignores the second manufactures the confidence that the thing is
    handled.

    SCOPED TO THE PARTS THE RUN CONTAINS, and the scope is REPORTED, not assumed.
    A one-suite log has nothing to say about another suite's rows. Counting those
    as present would make a single-suite run certify the whole ledger, so they are
    counted OUT LOUD as `not checked` instead.

    WHY THIS REPORTS AND IS NOT WIRED INTO THE GATE. The blocker this paragraph used
    to name has been REMOVED and the paragraph is kept because the conclusion has not
    changed. Part 340 recorded ONE skip under a DIFFERENT name ("the unreadable-source
    refusal") when the box had no non-root user to read as, rather than skipping its
    two named arms; on such a box two committed rows had no matching record and were
    not removed checks, so a gate refusing on absence would have reddened a correct
    run. #994 and #998 made the conversion -- part 340 now calls check_skip under each
    premise's own name, and the stand-in survives only in a comment explaining what it
    used to do.

    So the stated precondition is met, and arming this is now a DECISION rather than a
    dependency. It is not taken here, because "the one blocker I measured is gone" is
    not the same claim as "no blocker remains", and the second needs its own run across
    the parts that skip. Nothing in the tree invokes this subcommand -- not
    run_all_versions.sh, not either workflow -- so the direction #1010 fixes in it is
    latent today and the fix is a precondition for arming rather than a live repair.

    A ROW CARRYING HISTORY IS NEVER PRUNED. The catalogue of what has been seen
    red is the thing this ledger exists to be, and no run can recreate it. Dropping
    an entry because a name moved is the precise loss `rename-scan` was written to
    prevent, so `--prune` refuses the WHOLE prune when any orphan carries history
    rather than removing the safe ones and leaving a partial job to be finished by
    whoever reads the output.

    AND A PART HOLDING ANY SKIP IS UNPRUNABLE, which is the first version's worst
    bug rather than a refinement of it. `not checked` protects a part the run does
    not contain at all. A part the run CONTAINS BUT SKIPPED WHOLESALE fell between
    the two: one SKIP record put the part in `parts`, every other row of that suite
    became an orphan, and `--prune` deleted the suite while reporting
    `not checked=0` and rc=0 -- the most confident output the tool can produce.
    Measured on a three-row fixture for `analyze_differential`, whose run is one
    SKIP on PG17; reported by @pgcolumnar-9b reviewing this change.

    THE RULE IS BROADER THAN THAT CASE ON PURPOSE. A SKIP anywhere in the part
    means some arm did not run, so the run cannot distinguish "this row's check was
    deleted" from "this row's check was skipped under a name that does not match
    it" -- which is #994's defect, at suite granularity instead of branch
    granularity. That is the same sentence this docstring already uses about part
    340, so writing the hole one level up was not an oversight I get to call
    subtle.

    It is deliberately conservative: one skipped timing check blocks pruning that
    whole part. Prune is a rare, deliberate act; a refusal costs a sentence and a
    deletion costs history no run can recreate.

    THE EXIT CODES, stated because a caller only ever sees the code:

        0   nothing left to report: no orphan and nothing unprunable
        1   something is still there -- an orphan, or a row in a part that SKIPPED
        2   an integrity failure, or a prune refused because history would be lost

    `not checked` does NOT set 1, and since #1010 that is the difference between a usable
    tool and one that always returns 1. A run observes ONE major, so every row claiming
    only another major is a row it does not contain. Outstanding means "this run found
    something wrong with a row it could see", and a row from another major is not that.
    The earlier wording said "a row this run cannot speak for", which describes both
    categories and matched only one.

    `--prune` returning 0 when it had pruned NOTHING was the first version's subtler
    bug, reported by @jdatcmd in review. A caller that scans, sees 1, re-runs with
    `--prune` and sees 0 reads "it pruned them" -- when nothing was pruned and nothing
    could be. Prose covers a human; a script sees only the code. So 0 now means the
    ledger and the run agree, and anything outstanding keeps the 1 the scan gave.
    """
    runs = _by_run(args.logs)
    if len(runs) > 1:
        raise LedgerError(
            f"orphan-scan compares ONE run against the ledger, but got {len(runs)} logs: "
            "the union of a before-log and an after-log hides the disappearance")
    rows = read_ledger(args.ledger)
    verdicts = runs[0][1]
    now = set(verdicts)

    # THE MAJORS THIS RUN OBSERVED (#1010). This is the direction the missing dimension
    # actually broke. A row the ledger claims only for PG18 looks exactly like a deleted
    # check to a PG15 run, and the fourth category already says the true thing about it:
    # "this run does not contain them, so it cannot speak about them". So no new category
    # and no grandfather rule -- the scope gains an intersection and the row lands in
    # `not checked`.
    #
    # It was saved until now only by the SKIP rule, and that was luck.
    # analyze_differential emits a check_skip on PG15-17, so its part was unprunable;
    # fk_referencing:287 emits `check` in its older-major branch and has no SKIP at all,
    # so once that suite is seeded a PG15 run would have called its two PG17+ checks
    # deleted. Measured at 4d7c75ae: 24 keys shared, 2 only on PG18, 1 only on PG15.
    #
    # AN INTERSECTION, not equality: a row claiming 15;18 is in scope on a PG15 run AND on
    # a PG18 run. That is the point of the set -- a stronger claim is held to both tests.
    run_majors = {m for pairs in verdicts.values() for _v, m in pairs}

    parts = {(s, p) for s, p, _ in now}
    # A part holding ANY SKIP cannot speak about absence: see the docstring.
    skipped_parts = {(s, p) for (s, p, _), v in verdicts.items()
                     if any(x == "SKIP" for x, _m in v)}
    checkable = {k for k in rows
                 if (k[0], k[1]) in parts and (rows[k][0] & run_majors)}
    unchecked = sorted(set(rows) - checkable)
    absent = sorted(checkable - now)
    orphans = [k for k in absent if (k[0], k[1]) not in skipped_parts]
    unprunable = [k for k in absent if (k[0], k[1]) in skipped_parts]

    with_history = [k for k in orphans
                    if rows[k][1] != NEVER or rows[k][2]]
    historyless = [k for k in orphans if k not in with_history]

    for k in orphans:
        majors, last, muts = rows[k]
        if k in with_history:
            print(f"    ORPHAN CARRYING HISTORY: {k[0]}\t{k[1]}\t{k[2]} "
                  f"(claims {MAJOR_SEP.join(sorted(majors))}, last red {last}, "
                  f"mutations: {';'.join(sorted(muts)) or NONE})")
        else:
            print(f"    orphan: {k[0]}\t{k[1]}\t{k[2]} "
                  f"(claims {MAJOR_SEP.join(sorted(majors))}, no history)")

    # The parts the run never mentioned, named rather than counted alone: a number
    # with no names is a number nobody can act on.
    if unchecked:
        silent = sorted({(k[0], k[1]) for k in unchecked})
        print(f"    not checked: {len(unchecked)} row(s) in {len(silent)} part(s) this run "
              f"does not contain, so it cannot speak about them: "
              + ", ".join(f"{a}/{b}" for a, b in silent[:5])
              + (" ..." if len(silent) > 5 else ""))

    # NAMED, not just counted: a number with no names is a number nobody can act on.
    if unprunable:
        parts_named = sorted({(k[0], k[1]) for k in unprunable})
        print(f"    unprunable: {len(unprunable)} row(s) in {len(parts_named)} part(s) that "
              f"SKIPPED at least one check, so absence there is not removal: "
              + ", ".join(f"{a}/{b}" for a, b in parts_named[:5])
              + (" ..." if len(parts_named) > 5 else ""))
        for k in unprunable[:5]:
            print(f"      {k[0]}\t{k[1]}\t{k[2]}")

    print(f"  orphan scan: majors this run observed="
          f"{', '.join(sorted(run_majors)) or 'none'}")
    print(f"  orphan scan: parts in the run={len(parts)}, rows in those parts={len(checkable)}, "
          f"orphans={len(orphans)} ({len(with_history)} carrying history), "
          f"unprunable={len(unprunable)}, not checked={len(unchecked)}")
    # The four categories must account for every row, or a row went missing in the
    # classification itself -- which is the failure this tool exists to report.
    matched = len(set(rows) & now)
    if matched + len(orphans) + len(unprunable) + len(unchecked) != len(rows):
        raise LedgerError(
            f"classification lost rows: matched {matched} + orphans {len(orphans)} + "
            f"unprunable {len(unprunable)} + not checked {len(unchecked)} != {len(rows)} "
            f"ledger rows -- every row must land in exactly one of the four")

    if not args.prune:
        return 1 if (orphans or unprunable) else 0

    if unprunable:
        print(f"    not pruning {len(unprunable)} row(s) in a part that skipped: the run did "
              "not exercise those checks, so their absence is not removal")

    if with_history:
        print(f"    refusing to prune: {len(with_history)} orphan row(s) carry history, and "
              "the catalogue is what this ledger is for -- no run can recreate it")
        print("      reconcile them instead: rename the ledger row to the check's new name, "
              "or say in the commit why the history may go")
        return 2

    if not historyless:
        # Nothing WAS pruned. If anything is still outstanding the caller must not read
        # that as success, so the scan's own verdict stands.
        return 1 if unprunable else 0

    for k in historyless:
        print(f"    pruned: {k[0]}\t{k[1]}\t{k[2]} "
              f"(claimed {MAJOR_SEP.join(sorted(rows[k][0]))})")
        del rows[k]
    write_ledger(args.ledger, rows)
    print(f"  orphan prune: removed {len(historyless)} row(s), the ledger now holds {len(rows)}")
    # Pruning some of it is not finishing it.
    return 1 if unprunable else 0


def read_budget(path):
    out = {}
    text = pathlib.Path(path).read_text()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            out[parts[0]] = int(parts[1])
    return out


def _resolve_against(path, spec):
    """Which ref carries the prior ceiling. Fails closed rather than guessing.

    NOT a hardcoded remote name. `origin` is per-clone: in a contributor's setup it
    is their fork, and OffgridwithJD measured theirs 446 commits behind upstream.
    Comparing against a stale main makes this check WEAKER, never falsely red --
    the ceiling may only fall, so an older main carries a higher one, and a raise
    passes whenever the stale prior is high enough. It fails open while printing a
    line that reads like the enforcement happened, which is the same shape as the
    absolute-path bug one level down: compared against the wrong thing, rather than
    could not compare.

    So:

      GITHUB_BASE_REF   in CI this names the PR's target branch, which IS the prior
                        by definition. Its remote-tracking ref must exist -- if the
                        checkout did not fetch it, that is an error, not a fallback.
      main@{upstream}   outside CI, ask git rather than a convention. The configured
                        upstream of the local main is the answer to "which main is
                        mine", per clone.

    Anything else is an error. A gate that quietly enforces less than it claims is
    the thing this whole change exists to refuse, and a fallback that says so is
    still a gate enforcing less.
    """
    if spec != "auto":
        return spec
    repo = pathlib.Path(path).resolve().parent

    def _rev(ref):
        r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "-q", ref],
                           capture_output=True, text=True)
        return ref if r.returncode == 0 else None

    base = os.environ.get("GITHUB_BASE_REF", "").strip()
    if base:
        for cand in (f"refs/remotes/origin/{base}", base):
            if _rev(cand):
                return cand
        raise LedgerError(
            f"GITHUB_BASE_REF is {base!r} but no ref for it resolves here, so the prior "
            "ceiling cannot be read. The checkout needs to fetch the base branch")

    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "main@{upstream}"],
                       capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    raise LedgerError(
        "no trustworthy prior ceiling: GITHUB_BASE_REF is unset and the local main has no "
        "configured upstream. Naming a remote would compare against whatever `origin` "
        "happens to be in this clone, which is how a fork 446 commits stale gets treated "
        "as the prior")


def _behind(path, ref):
    """" (N commits behind HEAD)" when the prior lags, "" when it does not.

    A reader can see WHICH ref was compared against. Without this they cannot see
    that it is 446 commits stale, which is the difference between knowing the
    comparison happened and knowing what it was worth.
    """
    repo = pathlib.Path(path).resolve().parent
    r = subprocess.run(["git", "-C", str(repo), "rev-list", "--count", f"{ref}..HEAD"],
                       capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip().isdigit():
        return " (distance from HEAD unknown)"
    n = int(r.stdout.strip())
    return "" if n == 0 else f" ({n} commit{'s' if n != 1 else ''} behind HEAD)"


def _committed_budget(path, ref):
    """The budget as of `ref`, or None when the file does not exist there.

    None means "this change introduces the file", which is not a raise. Every
    OTHER failure -- an unresolvable ref, a path outside its repository, a budget
    naming no ceiling -- raises, because asked to compare and unable is a different
    thing from nothing to compare.

    `git show REF:PATH` needs a REPO-RELATIVE path. The production caller passes an
    absolute one inside a copied build directory, so the first version returned
    None and printed "no prior ceiling to compare" -- a message that reads like a
    pass while the ceiling it was asked to enforce went unchecked. That is the
    fail-open shape this whole change is about, in the code that closes it.
    Reported by OffgridwithJD.

    So the path is resolved here rather than demanded of the caller, and every
    failure to resolve it is an ERROR. Asked to compare, unable to compare, is not
    the same as nothing to compare.
    """
    abspath = pathlib.Path(path).resolve()
    try:
        top = subprocess.run(["git", "-C", str(abspath.parent), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, OSError) as e:
        raise LedgerError(
            f"--against {ref} was given, but {path} is not inside a git repository, "
            "so the prior ceiling cannot be read") from e
    try:
        rel = abspath.relative_to(pathlib.Path(top).resolve())
    except ValueError as e:
        raise LedgerError(f"{path} resolves outside its own repository at {top}") from e

    # DOES THE REF EXIST, asked before anything is read from it. This check used to
    # live inside the `auto` resolver, so it covered the production call site and
    # nothing else: an EXPLICIT ref that did not resolve fell through to the
    # file-absent branch and was reported as the bootstrap case -- rc=0, with a
    # message asserting "this change introduces it" about a ref that does not
    # exist, one clause after saying the distance from HEAD was unknown. The code
    # knew it could not resolve the ref and contradicted itself in one sentence.
    #
    # Resolving belongs here, where every caller passes through, so that the
    # file-absent branch below describes only what it claims: a file missing at a
    # ref that IS there. Reported by OffgridwithJD, who scoped it precisely --
    # unreachable from the runner, which always passes `auto`, and a trap for the
    # harness arms and anyone driving the tool by hand.
    if subprocess.run(["git", "-C", top, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}"],
                      capture_output=True, text=True).returncode != 0:
        raise LedgerError(
            f"--against {ref} was given, but that ref does not resolve here, so the prior "
            "ceiling cannot be read")
    # A FILE THAT DOES NOT EXIST AT THE PRIOR HAS NO CEILING TO VIOLATE. This
    # returns None and the caller notes it, rather than erroring, and the
    # distinction is the whole of it: introducing the budget is not raising it.
    #
    # The gate caught its own bootstrap the first time it ran in CI -- #925's base
    # is #923's branch, where check_ledger_budget.txt does not exist because this
    # PR adds it, so `auto` resolved the base correctly, found no prior, failed
    # closed, and reddened the matrix. Correct behaviour for a rule that could not
    # be satisfied: a PR introducing the file could never pass its own gate.
    #
    # It is not a hole. Deleting the budget on a branch and re-adding it with a
    # higher ceiling does not reach here, because the file still exists at the
    # prior and the comparison happens. Only a genuinely new file gets the note,
    # and a genuinely new budget file is reviewed as a new file.
    #
    # An unresolvable REF stays an error above, and a malformed budget stays one
    # below. Asked to compare and unable is different from nothing to compare.
    try:
        blob = subprocess.run(["git", "-C", top, "show", f"{ref}:{rel.as_posix()}"],
                              capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, OSError):
        return None
    out = {}
    for line in blob.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        f = line.split()
        if len(f) == 2 and f[1].isdigit():
            out[f[0]] = int(f[1])
    if "suites_not_covered" not in out:
        raise LedgerError(f"the budget at {ref} names no suites_not_covered")
    return out


def cmd_gate(args):
    rows = read_ledger(args.ledger)
    budget = read_budget(args.budget)
    # The records, WITH the major each was observed under: the refusal is about a
    # (check, major) pair even though the ledger is keyed on the check.
    records = sorted({(s, p, n, m) for s, p, n, _v, m in read_records(args.logs)})

    rc = 0

    covered_suites = {k[0] for k in rows}
    # AND THE SAME ARGUMENT FOR THE MAJOR (#1010). The gate cannot refuse a new check on a
    # major it holds no rows for, for the identical reason it cannot in a suite it has
    # never seen: it has no idea which of that major's checks are new. Adding PG20 to the
    # matrix would otherwise redden every check at once, which is a gate somebody turns
    # off -- the failure this issue family exists to prevent. It tightens on its own the
    # moment one run on that major is merged.
    covered_majors = set().union(*(v[0] for v in rows.values())) if rows else set()

    # TWO CHECKS SHARING ONE LEDGER KEY, refused rather than noted (#982).
    #
    # A row is keyed on (suite, part, name), so two checks with the same name in one
    # part share a row. Nothing is mis-recorded while both pass, and the hazard is
    # exact: when one goes red the row records `ever red` and its namesake inherits a
    # red observation nothing attacked. `checks_never_observed_red` then falls by one
    # for a check nobody attacked, and that census is what #918 and #925 exist to make
    # trustworthy.
    #
    # `merge` has printed this since #982 was filed and returns 0, which is how three
    # of them sat in one part of selftest/400 for a day. THE GATE COULD NOT SEE IT AT
    # ALL: `records` above is a set, and a set collapses the duplicate before any arm
    # can count it. The same canonicalisation that makes the rest of this function
    # correct made this one class unreachable, so the count comes from the raw records.
    #
    # PER LOG, via _by_run, because one check observed in two logs is two RUNS of it --
    # the normal case, and how the ledger accumulates evidence at all. Only a repeat
    # inside one log is a collision.
    #
    # EVERY SUITE, DELIBERATELY UNLIKE THE NEW-CHECK REFUSAL BELOW. That one is
    # restricted to suites the ledger covers because it cannot know which of an
    # uncovered suite's checks are new. This one needs no history: two records, one
    # key, one log is decidable from the log alone. Refusing everywhere is what makes
    # the class impossible rather than impossible in the four suites seeded so far, and
    # the next collision is likelier to arrive in one of the other 249.
    #
    # MEASURED BEFORE WIDENING IT, because a gate that reddens 250 unmeasured suites is
    # a gate somebody turns off. A full PG 18 matrix run with this refusal armed for
    # every suite: 247 suites ran, RC=0, ALL VERSIONS PASSED, zero shared keys. Check
    # names are static, so one major's matrix is a complete measurement of this class
    # rather than a sample of it.
    shared = []
    for path, seen in _by_run(args.logs):
        for (suite, part, name), verdicts in sorted(seen.items()):
            if len(verdicts) > 1:
                shared.append((path, suite, part, name, [v for v, _m in verdicts]))
    for path, suite, part, name, verdicts in shared:
        print(f"    one ledger key covers {len(verdicts)} checks in {path}: "
              f"{suite}\t{part}\t{name}\t({', '.join(verdicts)})")
    if shared:
        print(f"    {len(shared)} ledger key(s) cover more than one check. Give each "
              f"check a name that says which it is; a shared key records one check's "
              f"red against the other.")
        rc = 1

    # THE REFUSAL: a check the committed ledger has never seen, IN A SUITE THE
    # LEDGER COVERS.
    #
    # The suite restriction is not a softening, it is the meaning of
    # suites_not_covered: the gate cannot refuse a new check in a suite it has
    # never seen, because it has no idea which of that suite's checks are new.
    # Without it the gate refuses every check of every uncovered suite and
    # reddens the whole matrix on the first run -- which is a gate somebody turns
    # off, the failure mode this issue family exists to prevent.
    #
    # It tightens on its own as suites are seeded, and the ceiling is what forces
    # that direction.
    unknown = []
    for key in records:
        if key[0] not in covered_suites or key[3] not in covered_majors:
            continue
        # A row is a claim about WHERE the check exists, so a known check seen on a major
        # its row does not name is refused too: widening that claim is a ledger edit a
        # reviewer should see, not something a run does silently.
        if key[3] not in rows.get((key[0], key[1], key[2]), [set()])[0]:
            unknown.append(key)
    for suite, part, name, major in unknown:
        print(f"    not in the ledger: {suite}\t{part}\t{name}\t(on major {major})")
    if unknown:
        print(f"    {len(unknown)} check(s) the ledger has never seen. Regenerate it with:")
        print(f"      python3 test/pgc_ledger.py merge --ledger {args.ledger} --date <today> <log>")
        rc = 1

    # A CENSUS, not a ceiling. Bounding it deadlocks: every new check enters as
    # `never`, so the only way to land one would be to raise a number the design
    # says may only fall.
    never = sum(1 for v in rows.values() if v[1] == NEVER)
    print(f"  ledger census: rows={len(rows)} | never observed red={never}, "
          f"ever red={len(rows) - never}, new this run={len(unknown)}")
    # THE MAJORS, reported beside the census because the restriction above is invisible
    # otherwise -- a gate that quietly enforces less than it claims is what the ledger
    # design refuses. A run on an uncovered major must SAY so rather than pass quietly.
    run_majors = sorted({k[3] for k in records})
    print(f"  ledger majors: covered={', '.join(sorted(covered_majors)) or 'none'} | "
          f"this run observed {', '.join(run_majors) or 'none'}")
    for m in run_majors:
        if m not in covered_majors:
            print(f"    the ledger holds no row for major {m}, so it cannot refuse a new "
                  f"check there -- merge one run on it and this tightens")

    # THE CENSUS IS COMPARED, NOT ONLY PRINTED (#952). Reporting is not enforcing:
    # the line above stated the true number while the budget claimed another, and
    # rc was 0 on a fifteen-row lie.
    #
    # DECIDABLE FROM THE TWO INPUTS ALONE. It needs no prior and no `--against`,
    # which is the whole point: the case it catches is a MERGE COMMIT, where two
    # PRs each re-derived the census from the same base, the ledger then takes both
    # sets of rows, and the budget keeps whichever side won the conflict. A check
    # that needed the prior could not speak about the commit that creates the
    # disagreement.
    #
    # STILL NOT A CEILING, and this does not make it one. A ceiling refuses a RISE,
    # and bounding this number deadlocks -- every added check enters as `never`, so
    # landing one would require raising a number the design says may only fall.
    # That argument is in check_ledger_budget.txt and nothing here changes it. What
    # is refused is a CONTRADICTION, in either direction, which is what "a
    # measurement that must be true" means.
    stated = budget.get("checks_never_observed_red")
    if stated is None:
        # Absence is not a contradiction, and a silent skip is not acceptable
        # either, so it is said out loud. Measured reason for not refusing: every
        # other gate fixture in both harnesses writes a budget stating only
        # suites_not_covered. What holds the COMMITTED budget to naming both is a
        # separate arm in each harness.
        print("    the budget names no checks_never_observed_red, so nothing asserts "
              "the census")
    elif stated != never:
        print(f"    the budget states checks_never_observed_red {stated}, the ledger "
              f"holds {never}: these describe the same file and disagree")
        print("      re-derive it from a run on THIS tree. Arithmetic across merges "
              "has been right by accident and is not evidence.")
        rc = 1
    else:
        print(f"    census stated {stated}, ledger holds {never}: they agree")

    if not args.registered:
        raise LedgerError(
            "--registered is required: without the registered suite list the coverage "
            "claim cannot be made, and skipping it silently is how a gate reports success "
            "for a question it never asked")
    registered = {w for w in pathlib.Path(args.registered).read_text().split() if w}
    if not registered:
        raise LedgerError(f"{args.registered}: no registered suites listed")
    uncovered = sorted(registered - {k[0] for k in rows})
    want = budget.get("suites_not_covered")
    print(f"  ledger coverage: registered={len(registered)} | covered={len(registered) - len(uncovered)}, "
          f"not covered={len(uncovered)}, ceiling={want if want is not None else 'unset'}")
    if want is None:
        print("    the budget names no suites_not_covered, so nothing bounds the coverage debt")
        return 1
    if len(uncovered) > want:
        print(f"    suites_not_covered: {len(uncovered)} exceeds the ceiling of {want}")
        rc = 1

    # MONOTONE, mechanically. The tracked file says the ceiling may only fall;
    # without this that sentence is prose and raising the number passes.
    if args.against:
        ref = _resolve_against(args.budget, args.against)
        # HOW FAR BEHIND THE PRIOR IS, printed beside it.
        #
        # `main@{upstream}` is the per-clone answer to "which main is mine", and
        # in a contributor's setup it resolves to their FORK -- OffgridwithJD's is
        # 446 commits behind upstream. Naming the ref told a reader WHICH prior
        # was used; it did not tell them what the comparison was worth. The
        # direction still fails open: an older main carries a higher ceiling, so a
        # raise passes whenever the stale prior is high enough.
        #
        # There is no better ref to pick that does not guess, so the weakness is
        # made visible instead. It costs nothing when the number is 0.
        shown = f"{ref}{_behind(args.budget, ref)}"
        prior = _committed_budget(args.budget, ref)
        if prior is None:
            print(f"    no budget at {shown}: this change introduces it, so there is no "
                  f"prior ceiling it could have raised")
            return rc
        p_want = prior["suites_not_covered"]
        if want > p_want:
            print(f"    suites_not_covered was raised from {p_want} to {want} "
                  f"(against {shown}): the ceiling may only fall")
            rc = 1
        else:
            print(f"    ceiling against {shown}: {p_want} -> {want}, which does not rise")
    return rc


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("census", help="print suite/part/name/verdict for each record")
    c.add_argument("logs", nargs="+")
    c.set_defaults(fn=cmd_census)

    m = sub.add_parser("merge", help="merge a run's records into the ledger")
    m.add_argument("--ledger", required=True)
    m.add_argument("--date", default="unknown")
    m.add_argument("--mutation", default="")
    m.add_argument("--reds-are-real", action="store_true",
                   help="the FAIL records in these logs are a genuine observation "
                        "of the code under test, not an artifact of the environment")
    m.add_argument("logs", nargs="+")
    m.set_defaults(fn=cmd_merge)

    r = sub.add_parser("rename-scan", help="report names that look renamed")
    r.add_argument("--ledger", required=True)
    r.add_argument("logs", nargs="+")
    r.set_defaults(fn=cmd_rename_scan)

    o = sub.add_parser("orphan-scan",
                       help="refuse a ledger row no record in its own part matches")
    o.add_argument("--ledger", required=True)
    o.add_argument("--prune", action="store_true",
                   help="remove orphan rows that carry no history; refuse the whole "
                        "prune if any of them does")
    o.add_argument("logs", nargs="+")
    o.set_defaults(fn=cmd_orphan_scan)

    g = sub.add_parser("gate", help="refuse a check the ledger has never seen")
    g.add_argument("--ledger", required=True)
    g.add_argument("--budget", required=True)
    g.add_argument("--registered", default="",
                   help="file listing every registered suite (required)")
    g.add_argument("--against", default="",
                   help="'auto' to resolve the prior from GITHUB_BASE_REF or main@{upstream}, "
                        "or an explicit git ref. Fails closed when no trustworthy prior exists")
    g.add_argument("logs", nargs="+")
    g.set_defaults(fn=cmd_gate)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except LedgerError as e:
        print(f"    ledger integrity failure: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
