# Running the pytest harness

This is the issue #432 pilot. It runs beside `test/*.sh`, and replaces nothing.

- `TESTS.md` in this directory documents every test and every assertion helper.
- `VACUITY_MODES.md` is the inventory of ways a pytest harness can report a false
  pass: 79 modes produced by the enumeration, 72 of them named in that
  file, 73 demonstrated by a run, and 28 refused by this layer today.
  VACUITY_MODES.md section 1a gives the counting rule and reconciles the
  run's totals against what is actually written down.
- `design/ISSUE_432_PYTEST_HARNESS.md` holds the design and the measurements
  behind each guard.

## Prerequisites

The interpreter is marked `EXTERNALLY-MANAGED`, so install into a virtual
environment rather than into system Python:

```sh
apt-get install -y python3.14-venv        # ensurepip is not in the base image
python3 -m venv /root/pyenv
/root/pyenv/bin/pip install -r test/pytest/requirements-test.txt
```

## Running it

```sh
cd test/pytest
PYTHONPATH=. /root/pyenv/bin/pytest                      # serial
PYTHONPATH=. /root/pyenv/bin/pytest -n 4                 # four workers
PYTHONPATH=. /root/pyenv/bin/pytest --pgc-expect-tests N  # assert the run's shape
PGC_PG_CONFIG=/usr/local/pg19a/bin/pg_config PYTHONPATH=. /root/pyenv/bin/pytest
```

`--pgc-expect-tests` takes the number of **collected tests**, not files. `N` above
is whatever the set you ran collects; the two numbers the gate uses are in
`expected_tests.txt`. A literal here would name the wrong quantity and go stale,
which is the mistake the previous text made: it passed 24, the number of files
`NO_CLUSTER` lists, where a test count belongs.

Each worker builds its own throwaway cluster on a port derived from its worker id,
and drops it at session end. Nothing survives a run.

## Checking a port against its bash original

```sh
/root/pyenv/bin/python test/pytest/compare_to_bash.py \
    test/native_projection.sh test/pytest/test_native_projection.py
```

It compares the two by assertion NAME and exits non-zero if the bash suite asserts
a property the port does not. A port keeps this working by passing each assertion
the same name string the bash check uses.

## Both halves are in the gate (#1016)

`test/run_all_versions.sh` does not run these tests and must not: the two harnesses stay
independent, and the shell runner invoking pytest is the cross-harness call the project
forbids. Registering the run in `SUITES` was the plan recorded in section 1a of the design
document, and it was the wrong mechanism for that reason. A second CI job is the right one.

`ci.yml` runs two:

- **`pytest-guards`** runs the files `NO_CLUSTER` names, in a venv where psycopg is
  deliberately ABSENT. That absence is what proves those files need no database.
- **`pytest-cluster`** runs the complement, with every pin from
  `requirements-test.txt` and a PGDG PostgreSQL 18 with its headers.

Both pass `--pgc-expect-tests` from `expected_tests.txt`, so a run that collects fewer
tests than it should fails instead of reporting a green that means nothing. **Adding a test
moves a number in that file**, and the diff sits next to the test that moved it.

## Adding a test: four registries, and each one reddens alone

A new test has to be entered in four hand-maintained places. None of them mentions
the others, and CI finds them one at a time, so "I fixed the registry" does not
mean the change is finished. Measured over one day of two people adding tests:
four round-trips, each failure in a different registry from the one just fixed.

1. **`expected_tests.txt`** moves, as the section above says. Which key moves
   depends on whether `test_harness_deps.py`'s `NO_CLUSTER` names the file:
   `guard_tests` if it does, `cluster_tests` if it does not. Putting the count in
   the wrong key is a silent miss, because the other job's count still matches.

   **Collect the number, do not add to it.** Parametrization expands one function
   into several, so a new test function is not reliably `+1`. Two branches cut
   from the same base both move the key, and the survivor's value is neither
   branch's:

   ```
     both branches moved cluster_tests to 487, for different tests
     the first merged, so 487 became main's value without the second test in it
     collected on the rebased tree:  488
   ```

   This collided three times in one day. The first time `486 + 1` happened to
   equal the measured 487 and the agreement was luck; the third time the luck ran
   out and the arithmetic was wrong. Run the collection.

2. **`TESTS.md`** names every file and every test.
   `test_docs_cover_the_corpus.py` fails until the new test appears there. Join
   the table of the section the file already has rather than opening a new one.

3. **`check_ledger.tsv` and `check_ledger_budget.txt`**, but only if the suite is
   already covered: the gate cannot refuse a check in a suite it has never seen.
   New rows arrive as `never`, so `checks_never_observed_red` MOVES and has to
   match the census. Regenerate with one log per gated major in a single `merge`,
   because a row covers only the majors it was merged from.

4. **`expected_unrunnable.txt`**, per major. A test that declines for an entirely
   correct reason still reddens until its reason is written down for each major
   where it declines. The file exists so that an *unexpectedly* unrunnable test
   cannot leave a run green.

### Which of those a local run catches

From `test/pytest`, the guard half exactly as CI runs it:

```sh
G="$(/root/pyenv/bin/python -c 'import sys;sys.path.insert(0,".");from test_harness_deps import NO_CLUSTER;print(" ".join(NO_CLUSTER))')"
PYTHONPATH=. /root/pyenv/bin/pytest -q \
    --pgc-expect-tests "$(awk '$1=="guard_tests"{print $2}' expected_tests.txt)" $G
```

That catches 1, 2 and the port-parity arm. It cannot catch 3, which needs a suite
run, or 4, which needs the major where the test declines.

**For 4, read the names and never the exit code.** Running the right major locally
is not sufficient. A prefix built without ICU makes a collation test decline where
the runner's does not, so the same non-zero exit appears on a tree where nothing
is wrong. Measured: CI named one entry under exit 67 and a local PG17 run named a
different one under the same exit. Read the list the layer prints under
`unrunnable on PG<major>, and not expected to be:` (`pgc_vacuity.py`, and the
exit is `EXIT_INCOMPLETE`, 67) and check whether your test is one of the names
in it.

## Warnings

The vacuity layer is loaded through `pytest.ini` and cannot be turned off by a test
file. A test that concludes nothing fails, a bare skip fails the run, and a
comparison that could not have failed is refused. If a guard blocks something
legitimate, the escape hatches take a reason rather than a flag, and every one of
them is listed in the design document. Adding a new escape hatch needs a red test
that proves the guard still fires without it.
