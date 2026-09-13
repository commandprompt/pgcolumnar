# The pytest corpus: what each test asserts, and why it exists

Reference for anyone reading, running, or adding to `test/pytest/`. The design and
the decisions behind the harness are in `design/ISSUE_432_PYTEST_HARNESS.md`. This
file covers the tests themselves.

This file names every test in the corpus and says what each one asserts. **It
states no totals**, and that is deliberate (#908).

A count here was a claim whose correct value is a function of the MERGE rather
than of either branch, so it collided on essentially every rebase that touched
the corpus -- ten times in one day, and both sides wrong every time.

It was also redundant, and the argument needs THREE arms rather than the two it
was first written with. `test_every_file_and_test_is_named_in_the_document`
requires every test on disk to be named here;
`test_a_documented_test_that_does_not_exist_is_named` requires every name here to
exist on disk; and `test_no_test_name_is_defined_twice_in_the_corpus` requires
those names to be UNIQUE. The first two give equality of the two NAME SETS, which
is not equality of DEFINITION COUNTS -- two files defining one name leave both
arms green while the counts differ (@jdatcmd). With uniqueness as well, a count
over this document is a count over the corpus, and a number added nothing except
a thing to get wrong.

The harness prints the counts on every run, where they cannot go stale. Note that
a count of test FUNCTIONS is not the count of collected ITEMS -- parametrized
tests expand -- so `--pgc-expect-tests` takes the run's own collected count and is
documented in README.md beside the invocation that uses it.

The harness tests come first, because a harness that can report a false green
makes every other result in this directory worthless.

That ratio is not an accident of taste. Two of those files exist because a reviewer
neutered the guards one at a time and found most of them deletable with the suite
still green, and because the corpus once reported 25 passed against source carrying
`#error`. Both are recorded below in the sections for the files that close them.

The totals in bold above are checked. `test/selftest/350-the-pytest-corpus-must-be.sh`
reads them back and compares them with the corpus on disk, and also requires every
file and every test here to be named in this document -- because this file went stale
inside a single rework, and a partial index of something claiming completeness reads
as a total one.

Every measured fact quoted below was run. Where a test encodes a number or a
behaviour, the source of that number is named.

## Contents

- [1. How to read a test in here](#1-how-to-read-a-test-in-here)
- [2. The assertion vocabulary](#2-the-assertion-vocabulary)
- [3. test_layer.py: the guards, testing themselves](#3-test_layerpy-the-guards-testing-themselves)
- [4. test_guards_pinned.py: every refusal, pinned to its own message](#4-test_guards_pinnedpy-every-refusal-pinned-to-its-own-message)
- [5. test_build_refusal.py: never report on source you did not build](#5-test_build_refusalpy-never-report-on-source-you-did-not-build)
- [6. test_docs_cover_the_corpus.py: this document, checked](#6-test_docs_cover_the_corpuspy-this-document-checked)
- [7. test_connection.py: the cluster and the direct connection](#7-test_connectionpy-the-cluster-and-the-direct-connection)
- [8. test_native_projection.py: the ported suite](#8-test_native_projectionpy-the-ported-suite)
- [9. test_ordered.py: the ordered oracle](#9-test_orderedpy-the-ordered-oracle)
- [10. test_runshape.py: the shape of the run itself](#10-test_runshapepy-the-shape-of-the-run-itself)
- [11. test_zonemap_boundaries.py: exact boundaries](#11-test_zonemap_boundariespy-exact-boundaries)
- [12. test_saop_element_pushdown.py: scattered set pruning](#12-test_saop_element_pushdownpy-scattered-set-pruning)
- [13. test_hilbert_locality.py: what the Hilbert curve buys](#13-test_hilbert_localitypy-what-the-hilbert-curve-buys)
- [14. test_suite_accounting.py: the matrix accounting for its own suites](#14-test_suite_accountingpy-the-matrix-accounting-for-its-own-suites)
- [15. test_harness_deps.py: the harness must self-test without a database](#15-test_harness_depspy-the-harness-must-self-test-without-a-database)
- [16. test_harness_deps_classifier.py: the classifier, in the file the gate runs](#16-test_harness_deps_classifierpy-the-classifier-in-the-file-the-gate-runs)
- [17. Adding a test](#17-adding-a-test)
- [18. What this corpus does NOT yet refuse](#18-what-this-corpus-does-not-yet-refuse)
- [19. Traps this corpus records](#19-traps-this-corpus-records)
- [20. test_raises_sqlstate.py: which error, and which statement](#20-test_raises_sqlstatepy-which-error-and-which-statement)
- [21. test_failed_query_sentinel.py: a failed query is not a comparison](#21-test_failed_query_sentinelpy-a-failed-query-is-not-a-comparison)
- [22. test_writes_wrote_rows.py: a write that wrote nothing](#22-test_writes_wrote_rowspy-a-write-that-wrote-nothing)
- [23. test_mutation_ledger.py: which checks have ever been red](#23-test_mutation_ledgerpy-which-checks-have-ever-been-red)
- [24. test_loop_coverage_premise.py: a loop that never ran asserted nothing](#24-test_loop_coverage_premisepy-a-loop-that-never-ran-asserted-nothing)
- [25. test_join_runtime_filter.py: serial join runtime filter](#25-test_join_runtime_filterpy-serial-join-runtime-filter)
- [26. test_check_records.py: every counted assertion is a record](#26-test_check_recordspy-every-counted-assertion-is-a-record)
- [27. test_skip_loop_arms.py: a skipped arm records under its own name](#27-test_skip_loop_armspy-a-skipped-arm-records-under-its-own-name)
- [28. test_docs_join_clustering.py: the runtime filter's layout precondition](#28-test_docs_join_clusteringpy-the-runtime-filters-layout-precondition)
- [29. test_join_vector_agg.py: ungrouped fold over a unique-key join](#29-test_join_vector_aggpy-ungrouped-fold-over-a-unique-key-join)
- [30. test_differential.py: the heap oracle, all seven parts](#30-test_differentialpy-the-heap-oracle-all-seven-parts)
- [31. test_native_ownership.py: every maintenance function is owner-only](#31-test_native_ownershippy-every-maintenance-function-is-owner-only)
- [32. test_stats_privilege.py: stats is readable only by a caller who may read the table](#32-test_stats_privilegepy-stats-is-readable-only-by-a-caller-who-may-read-the-table)
- [33. test_docs_table_structure.py: a table must stay a table](#33-test_docs_table_structurepy-a-table-must-stay-a-table)
- [34. test_docs_stripe_floor.py: the stripe floor is below a vector](#34-test_docs_stripe_floorpy-the-stripe-floor-is-below-a-vector)

## 1. How to read a test in here

Three rules apply to every test, and they are enforced rather than requested.

**A test must make a counted assertion.** Counted means it went through the
`expect` fixture. A bare Python `assert` is allowed but does not satisfy the
requirement, so a body that computes and concludes nothing fails. This applies to
the tests that test the guards, too. An exemption there would be the first step to
exempting everything.

**Every assertion carries a name.** The name is the first thing a reader sees in a
failure, and for a ported test it is the same string the bash check uses, which is
what lets `compare_to_bash.py` diff the two suites by property.

**A premise gets its own assertion.** If a test deletes rows and then compares two
things, it asserts that the delete removed something first. Otherwise both
comparisons are satisfied by a table that never changed.

## 2. The assertion vocabulary

All of these live on the `expect` fixture, in `pgc_vacuity.py`. Each refuses its own
degenerate cases, and refusing raises `VacuityError` rather than failing an
assertion, so the two read differently in output.

| helper | asserts | refuses |
| --- | --- | --- |
| `num(got, want, name)` | two numbers are equal | anything that is not a number, including `bool`, and including the string `"100"` that `psql -At` would have given |
| `at_least(got, floor, name)` | `got >= floor` | non-numbers, and a floor of zero or less, which any count satisfies |
| `rows(got, want, name, allow_empty=None)` | two result sets are equal | both sides empty, unless `allow_empty` gives a reason |
| `row_set(got, want, name, allow_empty=None)` | two result sets are equal **ignoring order** | what `rows` refuses |
| `ordered_rows(got, want, name)` | two sequences are equal **in order** | two empty sequences, and a sequence whose elements are all identical, where order cannot be observed |
| `ordering_observable(forward, reverse, name)` | this fixture can distinguish order at all | a fixture that reads identically both ways |
| `hash(got, want, name)` | two oracle hashes are equal | comparing an object against itself, either side being a `QUERY_ERROR` sentinel, both sides empty |
| `text(got, want, name)` | two strings are equal | an empty expectation, which anything empty satisfies |
| `plan_marker(plan, key, name, absent=False)` | some plan node carries a `Columnar` property key | nothing; `absent=True` inverts it |
| `plan_node(plan, node_type=, provider=)` | some node matches those fields exactly | being called with neither field, which would assert nothing |
| `outcomes(result, name, **want)` | an inner pytest run's outcomes | being called with no expectation |
| `run_failed(result, name)` | an inner run exited non-zero | nothing |
| `refusal(result, name, *patterns)` | an inner run failed **and** its output carries each pattern | being called with no pattern, which is an outcome-only assertion wearing a better name |
| `cannot_run(reason, detail)` | declares the test unrunnable | a reason outside the closed list |

`refusal` is the helper the whole of section 4 turns on. Asserting that an inner run
failed is not the same as asserting that a named guard fired: several guards are
subsumed by a neighbouring one, so the inner run fails either way and an
outcome-only assertion cannot tell which. Requiring the message is the same move as
asserting on a SQLSTATE rather than on prose -- name the contract, not the symptom.

**Which oracle you pick is an assertion, not a formatting choice.** `row_set`
ignores order by declaration; `ordered_rows` asserts it. The collection scan refuses
`sorted()` or `set()` feeding `ordered_rows`, because that reads as an ordering claim
and is not one. This is `pgc_seq_hash`, `diff_query_ordered` and
`pgc_check_ordered_oracle` ported, including that third one's control: the set oracle
must be order-blind BY DESIGN, or an ordered oracle could quietly be implemented as a
set one and every ordering test would go silent while staying green.

`rows` compares row sets rather than `md5(string_agg(...))`. That asserts the same
property as the bash oracle by a stronger means: a hash mismatch says two hashes
differ, a row-set mismatch says which row. It also avoids recomputing the hash in
Python, where encoding or collation could make identical rows hash differently.

`UNRUNNABLE_REASONS` is the closed list `lib.sh` already uses:
`MISSING_DEPENDENCY`, `UNSUPPORTED_MAJOR`, `ABSENT_FIXTURE`,
`UNAVAILABLE_ENDPOINT`, `UNMET_PRECONDITION`.

## 3. test_layer.py: the guards, testing themselves

These eighteen run pytest inside pytest through the `pytester` fixture. Each
writes a small test file, runs it with the plugin loaded, and asserts on the INNER
run's outcome. That is what proves a guard REFUSES, rather than assuming it.

Each row names the measured bare-pytest behaviour the guard exists to stop. Every
one of those eight measurements exited 0.

| test | the guard | bare pytest, measured |
| --- | --- | --- |
| `test_layer_rejects_a_test_with_no_assertion` | a test that concludes nothing fails | `1 passed`, exit 0 |
| `test_a_counted_assertion_passes` | **positive control**: a real assertion still passes | — |
| `test_layer_rejects_two_empty_results` | empty compared with empty is refused | `2 passed`, exit 0 |
| `test_layer_allows_an_empty_result_when_declared` | **escape hatch**: an empty result with a reason passes | — |
| `test_layer_rejects_a_self_comparison` | a value compared against itself is refused | it cannot fail, so it passes |
| `test_layer_rejects_a_substring_plan_match` | a plan field is matched exactly, not by substring | `"ColumnarScan" in "…PgColumnarScan…"` passes |
| `test_layer_matches_the_exact_provider` | **positive control**: the real name matches | — |
| `test_layer_rejects_a_bare_skip` | a bare `@pytest.mark.skip` fails the run | `2 skipped`, exit 0 |
| `test_layer_fails_on_a_collected_count_mismatch` | a run that collects fewer tests than expected fails | a filtered run exits 5, widely treated as fine |
| `test_layer_refuses_a_zero_expectation` | `--pgc-expect-tests 0` is refused | it would be satisfied by collecting nothing |
| `test_an_unrunnable_test_does_not_leave_the_run_green` | a test declaring itself unrunnable exits 67 | **`1 passed`, exit 0** |
| `test_an_unrunnable_test_names_its_reason_and_its_detail` | the `UNRUN` line carries reason and detail | nothing was printed at all |
| `test_a_real_failure_outranks_an_unrunnable_test` | a run with both exits 1, not 67 | — |
| `test_a_run_with_nothing_unrunnable_still_exits_zero` | **control**: a green run is untouched | — |
| `test_layer_rejects_psycopgs_no_count_sentinel` | `rowcount` of `-1` is refused | `-1` and `1` are both numbers, so `num` compares them happily |
| `test_layer_rejects_a_broad_except_in_a_test_file` | a broad `except` is uncollectable | it was forbidden in a COMMENT, which enforces nothing |

Six of the eighteen are controls rather than guards. They are not decoration. A guard
with a bad false-positive rate gets switched off, and then the guard it replaced is
gone too. `test_a_counted_assertion_passes` and
`test_layer_matches_the_exact_provider` exist so that a guard which starts
rejecting good tests reddens here first.

The last four came from checking the layer against the 79-mode inventory in
`VACUITY_MODES.md` rather than from reasoning about it, and **all three guards they
added had been passing silently**. Two are worth stating in full because the shape
recurs.

**`plan_marker(absent=True)` returned a pass against `[]`.** An absence assertion is
satisfied by nothing being there at all, which is the case most worth catching: a
plan that failed to arrive looks exactly like a plan that legitimately lacks the
node. Absence claims need a premise that the thing which could carry the marker
exists — the same reason `at_least` refuses a floor of zero.

**A broad `except` was forbidden in a comment, which enforces nothing.** After any
failed statement psycopg raises `InFailedSqlTransaction` for every later one, so a
single `except Exception` hides the real error and all its successors. Written first
as a line regex, the guard immediately rejected this layer's own tests, because the
forbidden shape appears inside a `pytester.makepyfile` string. It now parses with
`ast`, where a handler inside a string literal is not an `ExceptHandler` node. **A
line regex over source cannot tell code from a string** — the same mistake as
matching a plan by substring, and a guard that rejects legitimate tests is a guard
somebody switches off.

The escape hatches are deliberately more expensive to type than the honest form.
`allow_empty` takes a reason, not `True`. `--pgc-expect-tests` takes the real
number. `cannot_run` takes a reason from a closed list. None of them can become the
default by being shorter.

### The third state, and the hole it left

**`cannot_run` reported a pass.** It wrote `self.unrunnable` and nothing read it,
so a test that declared itself unrunnable printed `1 passed` and exited 0 —
measured, not inferred. A write-only field, the same shape
`test/selftest/320-a-check-that-could-not-run.sh` polices in the runner, where an
INCOMPLETE branch set a variable the verdict never read.

**It made the layer's own escape hatch its largest hole.** A bare
`@pytest.mark.skip` FAILS the run. The honest-looking alternative greened
silently, so the layer refused the cheap dishonest escape and permitted the
expensive-looking one. An escape hatch that costs nothing is the default.

The run now ends `EXIT_INCOMPLETE`, which is 67 — deliberately the same number as
`PGC_EXIT_INCOMPLETE` in `lib.sh:58`, because a runner that learns the code should
learn it once. pytest itself uses 0–6, so 67 collides with nothing. The reason and
detail print in lib.sh's shape:

```
UNRUN  test_probe.py::test_cannot: ABSENT_FIXTURE: the parquet corpus was not built
checks unrunnable: 1
```

**Failure still dominates**, exactly as in lib.sh: a run with both a failure and an
unrunnable test is a failure, because the failure is the more urgent fact. The
override only ever moves a run off zero. Measured in all four combinations, serial
and under `-n 2`:

```
unrunnable only          exit 67    exit 67  (-n 2)
unrunnable + a failure   exit  1    exit  1  (-n 2)
```

The xdist column is not decoration. The declaration travels to the controller as a
`user_property` on the test report, because a worker's own exit status is discarded
by xdist and a variable held in the worker process would never be seen. The
collector is held on the **config**, not in a module global, because `pytester`
runs the layer's own tests in-process: a module-level list would leak an inner
run's declarations into the outer session and exit the whole corpus INCOMPLETE.

The structural half of this is pinned in the gate by
`test/selftest/360-an-unrunnable-pytest-test-must.sh`, which is greppable from a
checkout with nothing installed — it asserts the field is read, that the read
reaches the exit status, that the override is conditional, and that the two
harnesses agree on 67. Against the pre-fix layer it reddens six arms.


### A collection-time refusal must keep its reason under xdist

#963. `pytest_collection_modifyitems` raises `pytest.UsageError`. Serial, that is
rc 4 and the sentence on stderr. Under `-n` pytest still runs
`pytest_collection_finish` in a `finally`, so the worker tells the controller it
collected the tests and then exits; xdist's `worker_workerfinished` asserts a
worker that collected tests must not finish with them pending, and the reader
gets a 35-line INTERNALERROR, rc 1, and no sentence.

The table is the test. A VacuityError raised inside a test body is the control:
it is a normal failure and must stay rc 1 with the sentence, in both modes, so
a fix that moved the wrong hook reddens here.

| test | asserts |
| --- | --- |
| `test_a_bare_skip_refusal_keeps_its_reason_under_xdist` | collection skip: rc 4, sentence, no INTERNALERROR, serial and `-n 2` |
| `test_a_broad_except_refusal_keeps_its_reason_under_xdist` | the same for a second collection-time rule, so the defect is the hook |
| `test_an_in_test_vacuity_refusal_is_unchanged_under_xdist` | **control**: a body that concludes nothing stays rc 1 with the sentence |
| `test_a_collection_refusal_is_not_also_reported_as_a_silent_loss` | a refusal is not ALSO reported as `lost them silently`, serial and `-n 2` |
| `test_a_genuine_silent_loss_is_still_reported` | **control**: an item collected and never reported, with no refusal, is still named |

**A loud refusal is not a silent loss (#991).** A collection-time refusal printed its
sentence and then, directly beneath it, `VACUITY: N collected test(s) never reported an
outcome, so the run lost them silently`. The run did not lose them silently — it refused
them loudly, one line above — so a reader who typed one bare skip got the right diagnosis
plus a second finding sending them after a test that was never lost. `collected - reported`
is the right set difference and the wrong *meaning*: a silent loss is when nobody said
anything, and here the layer itself stopped the run.

Every refusal goes through `_collection_usage_error`, so recording it there covers all five
call sites with one assignment, and the reconciliation skips **only** that problem. The
setup-skip problem still prints: a fixture removing every test that depends on it is not
something a refusal accounts for. The control above is what keeps dropping a problem from
becoming dropping the guard — it is one edit away.

Serial was the only path left after #963, because clearing `items[:]` in the worker already
emptied the controller's `collected` set under `-n`.

A worker records the sentence on `workeroutput` and clears the items so no ids
cross. The controller re-raises `UsageError` from `pytest_testnodedown`, which
is the process serial already used. Refusing `-n` would drop a runner this
layer already registers an xdist hook for. Turning the refusal into a test
failure would keep rc 1, which is the same code a failing test gives.

**The shell harness needs no equivalent.** The subject is the collection hook
in `pgc_vacuity.py`. A shell part that greps or inspects that module is the
coupling selftest 350 and 360 deleted.

### An A/B whose arms agree measures nothing

`expect.differ(a, b, name)` is the assertion `mutation-arm-unobservable` says nobody
writes. Before it the layer had **eight** helpers asserting equality and **one**
asserting inequality — `ordering_observable`, specific to a forward/reverse pair — so
the general case was hand-rolled.

| test | asserts |
| --- | --- |
| `test_layer_requires_ab_arms_to_differ` | two identical arms fail, naming the mode |
| `test_differ_names_both_arms_when_they_agree` | the refusal carries the value both arms held |
| `test_differ_passes_when_the_arms_differ` | the positive control |
| `test_differ_counts_as_an_assertion` | `differ` alone is a concluded test |
| `test_differ_refuses_a_failed_query_on_either_side` | a failed arm is refused, left and right |
| `test_differ_refuses_two_failed_queries` | **the inverse of #930's trap**; see below |
| `test_the_inequality_scan_finds_a_planted_offence` | the AST scan fires on both spellings |
| `test_the_inequality_scan_does_not_flag_honest_code` | five shapes it must not flag |
| `test_no_test_in_this_corpus_hand_rolls_an_inequality` | the population is zero, across 17 files |
| `test_a_conftest_cannot_switch_off_the_order_collapse_scan` | #924, the route still open after #958 |
| `test_a_conftest_cannot_switch_off_the_broad_except_scan` | the same hatch, a second scan |
| `test_a_conftest_cannot_switch_off_the_raises_scan` | and a third |
| `test_the_refusal_names_the_binding_that_changed` | an honest run, refused, naming the conftest's name |
| `test_a_conftest_cannot_stub_an_expect_method_so_a_false_claim_passes` | a conftest replacing `Expect.num` **on the class** with a stub that still counts is refused at collection, naming `Expect.num` |
| `test_stubbing_the_recorder_still_fails_closed_by_count` | **control**: stubbing `_record` still fails via count 0, so the public-method snapshot did not swallow the recorder's own protection |
| `test_a_new_attribute_on_the_layer_is_not_a_rebind` | the control: a check that fires on anything is not a check |
| `test_the_rebind_does_not_leak_into_this_session` | the binding is restored, so `pytester` does not poison the outer run |

**A conftest is loaded from the directory being policed, into the policing
interpreter, before collection.** So every module-level name in `pgc_vacuity` is
writable by the code it judges. #958 closed the datum one exploit used; the three
scans that read such data are module-level names one frame further out, and each was
a two-line conftest away from being a no-op:

```
GUARD               no conftest    with `pgc_vacuity.<scan> = lambda p: []`
order collapse      REFUSED rc=4   PASSED rc=0
broad except        REFUSED rc=4   PASSED rc=0
raises not pinned   REFUSED rc=4   PASSED rc=0
```

The layer now snapshots its own bindings at import and refuses a run in which any of
them changed, which covers names added after it was written. It is a cost guard, not
a lock: the criterion #924 set is that silencing a rule must cost more than stating a
reason. **The shell harness needs no equivalent** — `selftest/260` greps `lib.sh` from
a separate process and never sources the file it judges.

**#967 closes the class, not the instance.** `pgc_vacuity.Expect.num = stub` is
refused at collection. `expect.num = stub` on the instance, or a subclass yielded
by an overridden `expect` fixture, still keep the count and drop the comparison:
`Expect.__dict__` is unchanged, so the snapshot cannot see them. A stub that still
records satisfies the zero-assertion backstop. Those two routes stay #967.


**Two failed queries are not two observable arms.** `query_error()` produces a value
unique per occurrence precisely so two failures cannot compare **equal** and pass an
equality assertion. That uniqueness makes them compare **unequal**, so an arms-differ
assertion passes on a pair of statements that both blew up — the defect arriving
through the fix for it. Measured: two calls give `QUERY_ERROR.1.<detail>` and
`QUERY_ERROR.2.<detail>`.

**The hand-rolled idiom threw both values away.** `expect.num(int(after != before), 1,
...)` reports `got 0 want 1` when it fails, and a reader cannot tell arms that were
both empty from arms that were both wrong from arms correctly identical. Three defects,
one message.

**The scan is AST rather than a line regex**, for the reason the `pytest.raises` scan
records: the two paragraphs in this tree that describe the old idiom quote it verbatim,
so a text sweep flags its own documentation.

**And the scan found a site the manual count missed.** Grepping for before/after naming
found two. The scan found three — the third spelled `int(stated == disk) == 0`, the same
assertion with the comparison inverted, which no search for `!=` would reach.

## 4. test_guards_pinned.py: every refusal, pinned to its own message

**Why this file exists.** @jdatcmd neutered each guard in the layer in turn and
found **11 of 17 deletable with `test_layer.py` still green**. Repeating the census
over the whole corpus after the ordered oracle landed gave 12 of 17; the two extra
were guards added later, so this is not a defect of the original layer that
subsequent work happened to avoid. It is the shape the layer was in.

Two causes, and they need the same remedy.

**Never driven.** `test_layer.py` never called `text()`, `at_least()`,
`plan_marker()` or `cannot_run()` at all. A guard nothing calls cannot be observed
to work.

**Driven, but pinned by nothing.** This is the interesting half. Neuter the
both-empty guard in `ordered_rows` and the UNOBSERVABLE guard fires on the same
input. The inner run still fails, so an assertion on outcomes alone still passes.
The guard is unreachable **by subsumption** rather than untested, and an arm that
asserts only "something failed" cannot tell the two apart.

So every arm here goes through `expect.refusal`, which requires the message as well
as the failure.

| test | the refusal it pins |
| --- | --- |
| `test_num_refuses_a_string_that_looks_like_a_number` | `num("100", "100", …)` — the psql-text defect this harness exists to remove |
| `test_num_accepts_real_numbers` | **control**: a genuine numeric comparison still passes |
| `test_text_refuses_an_empty_expectation` | an empty expected string, which anything empty satisfies |
| `test_at_least_refuses_a_non_number` | a bound taken from text |
| `test_rows_refuses_a_flag_where_it_documents_a_reason` | `allow_empty=True` satisfied a truthiness test and carried nothing, so the escape hatch cost LESS to type than the honest assertion |
| `test_rows_accepts_a_reason` | **control**: the documented form still works, or the refusal above is a wall |
| `test_row_set_inherits_the_reason_requirement` | `row_set` delegates to `rows`, so it inherits the refusal rather than routing around it |
| `test_at_least_refuses_a_floor_of_zero` | a floor every possible value clears |
| `test_at_least_accepts_a_real_bound` | **control**: `at_least(7, 3, …)` passes |
| `test_plan_node_refuses_no_criteria` | called with neither `node_type` nor `provider` |
| `test_outcomes_refuses_no_expectation` | called with no expectation at all |
| `test_cannot_run_refuses_a_reason_outside_the_closed_list` | the escape hatch cannot be widened by inventing a reason |
| `test_hash_refuses_self_comparison` | a value compared against itself |
| `test_hash_refuses_a_LEFT_error_sentinel` | a `QUERY_ERROR` on the left |
| `test_hash_refuses_a_RIGHT_error_sentinel` | the mirror, which one arm never covered |
| `test_hash_refuses_two_empties` | two distinct empty values |
| `test_plan_marker_present_arm_fails_when_the_key_is_absent` | the arm that makes "did the columnar scan run" answerable |
| `test_plan_marker_present_arm_passes_when_the_key_is_there` | **control** |
| `test_plan_marker_absent_arm_fails_when_the_key_is_present` | the arm that pins the vector-aggregate trap |
| `test_plan_marker_absent_arm_passes_on_a_plan_that_lacks_the_key` | **control** |
| `test_plan_marker_refuses_an_absence_claim_over_an_empty_plan` | the hole under both arms |
| `test_refusal_itself_refuses_an_empty_pattern_list` | the new helper must not become the defect it removes |
| `test_the_empty_plan_refusal_precedes_the_arms_it_protects` | the refusal's **position**: no arm may answer ahead of it |

**`allow_empty` documented a rule the code did not enforce (#1031).** `rows` documents the
argument as taking *"a REASON, not a flag"*. The sentence gives the rationale too: the hatch
should cost more to type than the honest assertion. One line below sits a truthiness test. So
`allow_empty=True` passed and carried nothing, and the hatch cost less rather than more.

Measured before the refusal: `allow_empty=True` and `allow_empty=1` both passed, 3 passed.
`row_set` forwards the argument, so it inherited the hole, which is why it has its own arm.

The check fires whenever the argument is given, not only when both sides turn out empty.
Otherwise a flag form in a test whose sides happen to be non-empty passes today and refuses on
the day the data changes. That is the worst moment to learn it.

Two live sites used the flag form and **both were substantively correct**. Each had its
population premise on the line above, and one stated the argument in a comment. They now carry
that reason in the argument, where an audit of `allow_empty=` can read it.

### plan_marker, and the three ways it could not fail

@jdatcmd named this one first: *"both of its arms can be deleted independently
with the suite green. Under one of those mutations the premise can never fail, so
the provider-trap test would silently be about an ordinary plan."*

It is the worst place in the layer for that to be true. `plan_marker` is the
faithful port of `pgc_is_columnar_scan`, and `test_connection.py` calls it three
times — once as the **premise** that the vectorized aggregate engaged. A premise
that cannot fail turns its test into a test about an ordinary plan, and nothing
goes red while it happens.

**A third hole sat underneath both arms.** An absence claim is satisfied by
nothing being there at all: `plan_marker([], key, absent=True)` gave `1 passed`,
exit 0, because a plan that never arrived looks exactly like a plan that
legitimately lacks the node. That is now a `VacuityError`, and it is refused for
the present arm too — an empty plan means the `EXPLAIN` did not arrive, so
neither question can be answered.

**The empty-plan refusal is pinned by position, not only by behaviour.** A guard
that sits after the code it protects is a guard that never runs. So one arm reads
`plan_marker`'s own source and asserts the empty-plan refusal comes before both
arms. Two measurements say what that arm is worth today:

- Move the refusal to the end of the function and the arm fails; leave it where it
  is and it passes. It discriminates.
- With the refusal moved to the end, `plan_marker([], absent=True)` **still
  refuses**, because `plan_marker` has no early return for the absent arm. So the
  order is not load-bearing right now.

It is therefore prospective insurance: the day someone adds an early return, the
refusal stops being reachable and this arm is the only thing that says so. The
check was a shell part (`test/selftest/370`) until the two harnesses were
separated; a shell part can pin the text of a Python function but cannot run it,
so the arm moved here and 370 was deleted.

The four arm tests are behavioural rather than refusals, because `plan_marker`'s
two arms raise `AssertionError`: `expect.refusal` does not apply and
`expect.outcomes` is the right instrument. Their value is not their own green,
which they had before the guards were pinned. It is the census:

```
unmutated                      38c951eb7dda   5 passed
present arm neutered           dc066341dba2   1 failed  <- its own arm, and only it
absent arm neutered            7ce63404d821   1 failed  <- its own arm, and only it
empty-plan guard neutered      a4e9d763e77c   1 failed  <- its own arm, and only it
restored                       38c951eb7dda   byte-exact
```

**Each mutation reddens exactly one test, and it is that test's own.** That is
the property worth having: it proves the three are distinguishable rather than
subsumed, which "something went red" cannot.
| `test_ordered_rows_both_empty_names_its_own_refusal` | the sequence oracle's both-empty refusal, pinned to ITS message |
| `test_ordering_observable_both_empty_names_its_own_refusal` | the premise check's both-empty refusal, pinned to ITS message |
| `test_refusal_itself_refuses_an_empty_pattern_list` | the new helper must not become the defect it removes |

The two ordered-oracle rows are the subsumption case in its purest form. Both
guards refuse a both-empty comparison, and so does `rows()` underneath them, so an
arm asserting only "the inner run failed" passes with any one of the three deleted.
Each is pinned to its own message, which is the only way the three stay
distinguishable.

Three of these carry reasoning that is easy to lose.

**The sentinel arms name the SIDE.** A single arm asserting "is a failed query"
left both sentinel guards unheld. Neuter the left guard and the comparison itself
still fails the inner run — subsumption by the ordinary assertion, not by another
guard. With the side named, a left guard that stops working can no longer be
covered by the right one or by the comparison.

**`test_hash_refuses_two_empties` is reachable only with two DISTINCT empties.**
The self-comparison guard above it is `got is want`, an identity test, and CPython
interns `""` — so `expect.hash("", "", …)` trips *that* guard and never reaches
this one. Written the obvious way, the arm would have passed while asserting
nothing about the guard it names. Prove an input can reach a guard before asserting
the guard fires.

**`test_refusal_itself_refuses_an_empty_pattern_list` closes the loop.**
`refusal(result, name)` with no pattern is exactly the outcome-only assertion that
caused most of the unheld guards. The helper introduced to fix the problem refuses
to be used that way.

### What the census says now

Run the way the reviewer ran it — each guard neutered alone, the mutation asserted
to have applied, the file restored and compared byte-for-byte afterwards, and
`inputs == sum(buckets)` asserted:

```
base branch  before   13 guards    3 HELD   10 UNHELD
base branch  after    13 guards   13 HELD    0 UNHELD
full stack   after    18 guards   18 HELD    0 UNHELD
```

## 5. test_build_refusal.py: never report on source you did not build

**Why this file exists.** @jdatcmd appended
`#error THIS SOURCE IS BROKEN AND CANNOT BUILD` to `src/columnar_projection.c`,
rebuilt nothing, and ran both harnesses:

```
pytest                          ->  25 passed, exit 0
bash test/native_projection.sh  ->  FATAL: the build failed …, exit 1
```

The bash harness has refused that since #536. This corpus did not, because it never
built, never installed and never compared anything. `Cluster.so_md5` printed a
fingerprint that nothing read — a number on the screen is not a guard.

**The first fix was insufficient and was deleted rather than kept.** Comparing the
installed `.control` and `.sql` against source cannot catch `#error` in a `.c` file:
both artifacts stay byte-identical. The refusal now comes from
`pgc_build_and_install` in `test/lib.sh`, driven from Python, so there is one
implementation rather than two that can drift.

**Two levels of arm, deliberately.** The injected-runner arms pin what the Python
side does with a verdict. The `bash` arms pin the shell plumbing — the sourcing, the
quoting and the exit-status path — which an injected runner cannot reach and which
is where a wrong quote would hide.

| test | asserts |
| --- | --- |
| `test_a_failed_build_raises_rather_than_returning` | the refusal raises, says it is refusing, and carries the build's own output rather than a summary |
| `test_the_refusal_names_the_tree_it_refused` | the message names the source directory; a reader with several worktrees needs to know which |
| `test_a_successful_build_is_silent` | **control**: the guard does not fire on a build that worked |
| `test_the_shell_path_really_refuses` | the shell's own `FATAL` reaches the Python caller, through real bash |
| `test_the_shell_path_accepts_a_good_build` | **control** for the arm above, through the same plumbing |
| `test_a_missing_lib_sh_is_a_refusal_not_a_pass` | an unsourceable `lib.sh` means no guard at all, so it must refuse rather than proceed ungated |
| `test_build_once_builds_once_and_then_skips` | the workers share one prefix, so the install is serialised rather than skipped |
| `test_build_once_rebuilds_for_a_different_prefix` | running against two majors in turn rebuilds for each |
| `test_editing_the_source_rebuilds` | the marker is keyed on the source fingerprint, not just the prefix |
| `test_the_fingerprint_reads_content_not_mtime` | `touch` does not move the fingerprint; an edit does |
| `test_an_unfingerprintable_tree_always_rebuilds` | no fingerprint means no key, and no key must mean rebuild |
| `test_a_server_older_than_the_library_is_refused` | `predates` |
| `test_a_server_started_after_the_library_is_fresh` | `fresh` |
| `test_equal_timestamps_are_fresh_not_predates` | the exact boundary: the same second is not stale |
| `test_an_unreadable_side_is_unknown_not_fresh` | three unreadable shapes all give `unknown` |
| `test_the_fingerprint_covers_a_separately_built_module` | an `objstore/` edit moves the hash |
| `test_an_objstore_edit_forces_a_second_build` | and forces a rebuild, end to end |
| `test_make_cluster_leaves_nothing_behind_when_setup_fails` | a failed setup leaks no directory |
| `test_the_cleanup_guard_has_the_shape_the_leak_needs` | **source check**: the guard catches `BaseException`, stops the cluster, removes the tree, and re-raises |
| `test_this_module_keeps_no_private_fingerprint` | **source check**: no second digest implementation in this caller |

**Two of these are SOURCE checks, and they say so.** `test_make_cluster_leaves_nothing_behind_when_setup_fails`
provokes a real failed setup and asserts no directory is left; that is the property.
But `make_cluster` fails exactly one way in that arm — a missing `pg_config` — while
three more properties decide whether the guard works at all: it must survive a
`KeyboardInterrupt`, stop a postmaster it already started, and re-raise rather than
return `None`. Two of those cannot be provoked from a test (you cannot deliver SIGINT
into `initdb` reliably, and a cluster that started is one the arm would then have to
stop), so they are read off `inspect.getsource(make_cluster)` instead.

They arrived from `test/selftest/380`, which read this file as text across the harness
boundary. Reading our own module is not a cross-harness reference; a shell part
grepping it is the thing CONTEXT.md refuses. What the shell part could never do is the
behavioural arm above it.

Five mutations say the source arms discriminate, each asserted to have applied and
each leaving the module well-formed, restored byte-for-byte afterwards:

| mutation of `pgc_cluster.py` | what reddens |
| --- | --- |
| `except BaseException:` narrowed to `except Exception:` | the source arm |
| `cluster.stop()` removed | the source arm |
| the bare `raise` turned into `pass` | the source arm **and** the behavioural one |
| a private `hashlib.md5` added | the no-private-digest arm |
| `shutil.rmtree(root, …)` removed | the source arm **and** the behavioural one |

The two that redden both are the two whose effect reaches the filesystem. The three
that redden only the source arm are exactly the properties the behavioural arm cannot
see, which is why they are written down separately rather than folded into it.
| `test_moving_bytes_between_files_moves_the_shell_fingerprint` | the digest sees a repartition |
| `test_the_two_fingerprint_implementations_cover_the_same_inputs` | **the two implementations move on the same edits** |

### The build/start ORDER, which is not a detail

`shared_preload_libraries` maps the library at postmaster start, so **a cluster
started before the install keeps the OLD `.so` mapped for its whole life.** The
build reports success and every test still measures the previous branch's code —
the guard defeated by the order of two lines.

The first fix here had exactly that defect: the build ran *after* `make_cluster`,
which does `initdb` and starts the server. It surfaced as a flake — the first run
after the alpha4 rebase gave 15 cluster-start errors and the second run passed.
**A flake that clears on a second run is what a stale-binary defect looks like from
outside.**

### The twin of `selftest/340`, and the fourth instance of one defect

These four drive the **shell** functions through `bash` rather than
reimplementing them, and they are the pytest half of `test/selftest/340`'s stamp
arms, owed under the twin rule and payable only once `test/pytest/` reached
`main` with #897.

The last one is the interesting one. `source_fingerprint` in `pgc_cluster.py`
says in its own docstring that it uses *"the same input set as
`pgc_source_fingerprint` in `test/lib.sh`"*. It did not. The shell hashes each
build directory's `*.c`, `*.h` **and `Makefile`**; this side read only the
sources, so editing `objstore/Makefile` — which changes how that module builds —
moved one hash and not the other:

```
baseline                    shell=45be41a5c47b  python=bea88c7d79ca
objstore/Makefile edited    shell=cfb8f4553041  python=bea88c7d79ca
```

`build_once` then certified a stale module as current. **That is
@linuxhikerpm's finding one layer over**: they found the module's *sources*
missing from this implementation, and the module's *Makefile* was still missing
after that was fixed.

The arm asserts the property the docstring always claimed, and not more: the two
hashes are **not** required to be equal — they are different digests over the
same files, used independently — but **the same edit must move both**. It walks
five edits: a source, a module source, a module Makefile, the top-level
Makefile, and the control file.

Two implementations of one idea have now been separately wrong, separately
fixed, and a third party had to find each. That is the argument for making them
one.

### Two findings from @linuxhikerpm, both about infrastructure rather than coverage

**The fingerprint read `src/` only.** `objstore/` is a separately built shared
library the top-level Makefile reaches by recursion, so editing
`objstore/module.c` left the hash unchanged and `build_once` certified a stale
module as current:

```
objstore_before=2799803eaeac objstore_after=2799803eaeac
builds=1 second=already-built
```

That is the same gap #898 closes in `test/lib.sh` — but this is an **independent
implementation**, so rebasing #898 would not have fixed it. `source_build_dirs`
now derives the set by the same rule the build follows: `src/`, plus any
directory carrying its own Makefile. The hash also mixes in each file's path
relative to the tree rather than its bare name, because with two build
directories `src/module.c` and `objstore/module.c` would otherwise be
interchangeable.

**`make_cluster` leaked its tree when setup failed.** It created `root` with
`mkdtemp` and then ran `initdb`, `start` and `is_ours` with no cleanup guard:

```
make_cluster_error=RuntimeError
new_roots=1 leaked=['/tmp/pgc-pytest-777-h3phhtxc']
```

`conftest.py` cannot clean up after it, because `cluster, root = make_cluster(…)`
never completes when the call raises. The handled `is_ours()` path leaked too —
it stopped the cluster and left the directory. Every exit that is not a
successful return now stops whatever was started and removes the tree, catching
`BaseException` so an interrupt during `initdb` cleans up like an error does.

Both are pinned structurally in the gate by
`test/selftest/380-the-pytest-cluster-helpers.sh`, which requires the *glob*
rather than the name — the only way to tell a derivation from a list that
happens to be complete today.

### `unknown` never reads as `fresh`

Three of the verdict arms exist to keep that true. `server_binary_verdict` returns
one of `fresh`, `predates` or `unknown`, and `unknown` is what an unreadable mtime,
an unreadable postmaster start time, or a non-numeric epoch all produce. The
boundary arm is separate on purpose: mtime resolution is one second, so a run fast
enough to install and start within the same second must not refuse itself.

### The fingerprint's own integrity, and why it needed six more arms

Six tests, added with the fix that closed three defects in `pgc_source_fingerprint`
itself. The subject is the instrument every other arm in this section depends on:
if the fingerprint can be wrong, `never report on source you did not build` reports
on nothing.

`test_a_failed_digest_yields_no_fingerprint_rather_than_a_wrong_one` is the arm
here, and THE MECHANISM CHANGED WITH THE IMPLEMENTATION. It used to drive the real
shell function with a **stub `md5sum`** on `PATH`, because the shell forked one
per file. The digest now lives in `test/pgc_fingerprint.py` and uses `hashlib`,
which no `PATH` can reach, so the stub would have left both arms green while
testing nothing — the exact shape this corpus exists to refuse.

A real read failure needs a real reader who is denied, and **root is denied
nothing**: `chmod 000` is invisible to it. Measured before the arms were
rewritten:

    as root      28a7149e07ae   <- reads the mode-000 file regardless
    as postgres  (empty)        <- the failure the arm needs

So the tree is built outside any mode-0700 directory and read by a second user,
and where no such user exists the arm records `expect.cannot_run` rather than
passing.

**The verdict that follows is the shell harness's property, and it is asserted
there (#432).** `pgc_freshness_verdict` is pure shell, so an arm here could only
reach it by driving `lib.sh` — which is the coupling the two-harness rule removes.
`test/selftest/340` holds it, and holds more of it than this corpus did: it loops
over two unreadable files rather than one, and it carries the premise this side
lacked, that the unprivileged read AGREES with the privileged one while nothing is
denied. Without that premise the arms measure the user switch rather than the
permission denial. `test_one_tree_hashes_one_way_however_the_locale_is_set` pins the defect
the single implementation removed on the way: `sort -z` used locale collation and
nothing pinned a locale, so one tree hashed two ways —
`LC_ALL=C` gave `6d122a7158d5` and `LC_ALL=en_US.UTF-8` gave `0b59bd75fa4f`.

The verdict is the property that matters, and the reason it is worth an arm at all
is the asymmetry. `stale` is a FATAL; `unknown` prints `freshness UNVERIFIED` and
runs the suites. Since #959 there are two more: a **replaced library** is also a
FATAL — the source can be unchanged while another tree has overwritten the shared
prefix, which is how `matches the binary under test` came to be printed above
somebody else's binary — and a stamp written before #959, which records no library
digest, reports the source claim it earned and says the library is UNVERIFIED rather
than implying it was checked. The asymmetry is the whole argument for the change: a false
UNVERIFIED costs a line of output, a false FATAL costs a matrix **and** teaches
people to re-run past a freshness check, which is the failure this controller
exists to prevent.

`test_one_tree_hashes_one_way_however_the_path_is_spelled` pins five spellings —
trailing slash, `/./`, `/src/..`, a symlink, and a relative `.` — against the plain
path. Three of them disagreed before the fix, because `${f#"$dir"/}` strips a
prefix that has to match character for character.

`test_the_fix_does_not_rebaseline_stamps_already_on_disk` is a **compatibility**
assertion rather than a tidiness one, and it is the arm that would have caught the
worst version of this change. Detecting a failed digest means capturing the
per-file lines to inspect them, and `$(...)` strips the trailing newline that the
old straight pipe into `md5sum` included. Without restoring it, the same unchanged
tree hashes differently before and after the fix, every stamp already on disk reads
`stale`, and a fix for false FATALs becomes a false FATAL for everyone holding a
built worktree. The matrix cannot catch that: it copies a fresh tree and re-stamps
every run, so it lands on developers and on nobody's CI. The arm transcribes the
previous implementation and requires the same answer.

`test_the_fingerprint_still_moves_on_a_real_change` is the control without which
the spelling arms are vacuous — "every spelling agrees" is satisfied perfectly by a
fingerprint that ignores its input.

`test_a_tree_with_nothing_hashable_reports_no_fingerprint` closes the last one: the
hash of an empty stream is a stable, comparable value, so two trees with no source
would have *matched*.

**That last arm is the only one in this set with a real observation behind it
rather than a model, and it was not the case it was written for.** It shipped as
"a legitimate empty tree". @OffgridwithJD then observed the WHOLE manifest coming
back empty under process pressure, on a read-only bind mount where content was
excluded by construction: two distinct fingerprints over a tree incapable of
changing, and the deviant value was `d41d8cd98f00`, which is md5 of the empty
string — not a corrupted manifest but *no* manifest, hashed confidently. Measured
here as an A/B with the real `md5sum` and no stub, 20 samples per cell:

    true fingerprint = eebe35d6eaed ; md5("") = d41d8cd98f00

    OLD ulimit -u 45   correct=19  md5("")=1   refused=0  other=0  | sum=20 of 20
    OLD ulimit -u 40   correct=8   md5("")=11  refused=0  other=1  | sum=20 of 20
    NEW ulimit -u 45   correct=20  md5("")=0   refused=0  other=0  | sum=20 of 20
    NEW ulimit -u 40   correct=20  md5("")=0   refused=0  other=0  | sum=20 of 20

At `ulimit -u 40` the old function returns a confident answer about nothing in 11
runs of 20. The guard covers it structurally rather than statistically: a
non-empty `out` has at least one line, so `md5("")` is not a reachable return
value.

### When it refuses, it says what it hashed

Six more tests, added after two CI failures reported *the same pair of hashes and
nothing else* — `source now a735c673b129, binary built from 6d122a7158d5`,
identically, across two branches, two majors and two build directories, with the
fingerprint fix present in one of them. **A bare hash made the second occurrence
another sample rather than an answer.**

So the manifest is a function in its own right, `pgc_source_fingerprint` is
defined as its hash — the two cannot drift apart, and one test asserts exactly
that — and the FATAL path prints it through `pgc_freshness_report`.

`test_an_added_file_is_named_rather_than_merely_changing_the_hash` is the arm
aimed at the open question. An addition is the only class that explains one
deviant value from two different build directories, because the manifest carries
the path RELATIVE to the tree: the same file appearing under `matrix-17` and
`matrix-18` contributes the same line and therefore the same hash. The test
requires the diff to name the file rather than report that something changed.

`test_the_manifest_names_what_the_fingerprint_hashed` pins the shape of each line
— a tree-relative path and a 32-character digest, never an absolute path, because
an absolute path in the digest is the spelling defect returning by another route.
`test_the_fingerprint_is_the_hash_of_the_manifest` is the arm that keeps the two
from drifting.

**`pgc_freshness_report` is the shell harness's, and `test/selftest/340` asserts it
(#432).** Two arms here used to: one that the report names each hashed file and
states how many, and one that an empty manifest says `(empty -- nothing under ...)`
rather than printing nothing, because a silent empty dump reads as *the manifest
was fine*. Both drove `lib.sh` to reach a pure-shell function, which is the
coupling the two-harness rule removes, and 340 already held both properties. The
reason they exist is worth keeping even though the arms moved: the alternative was
asserting that the source CALLS the function, which is the shape this suite refuses
everywhere else, so the report is a function precisely so that an arm can drive it.

### The suite that wrote into the tree the other suites were reading

`test_no_selftest_part_writes_into_the_live_source_tree` scans the parts for a
redirection aimed at the live tree.

`test/selftest/340` used to write `objstore/.pgc_fingerprint_probe.c` into
`$PGC_SRCDIR`, to prove that a new file under a recursed directory moves the
fingerprint. `harness_selftest` runs IN the matrix, so at `PGC_JOBS=4` it created
that file in the shared build directory while sibling suites fingerprinted
concurrently, and whichever sampled inside that window reported `FATAL: the binary
under test was not built from this source` against a tree that was correct. The
path is tree-relative and the content fixed, so the deviant value was *identical*
across majors, build directories and branches — which is what made it look like a
real staleness. It cost four pull requests and two wrong diagnoses before
@linuxhikerpm found it by reading the suite.

The arm now probes a hardlinked COPY of the tree. The intent survives, because the
defect it was written for was that the *real* tree's `objstore/` was not being
read, and a hand-built fixture could not have caught that — so a premise requires
the copy to discover the same build directories as the real tree. **That premise
immediately earned itself**: the first fix hardlinked across a filesystem
boundary, `cp -al` failed after creating the destination, `cp -a` then copied the
tree *inside* it, and the copy's build directories came out as `bfix src` rather
than `objstore src`.

**A BEFORE/AFTER RUN CANNOT CATCH THIS, and that is worth recording because it
was my first attempt.** Fingerprint the tree, run the suite, fingerprint again:
the probe was created and `rm -f`'d inside the same suite, so the tree is
byte-identical by the time the run ends and the comparison passes. The damage is
done to whoever samples DURING the window, and an after-the-fact observer is blind
to it by construction. Sampling concurrently instead would make the arm racy — it
would pass whenever the timing missed. So the observable property is the one in
the source: no part directs a write at the live tree.

Its limit is stated in the test: it recognises a redirection whose target mentions
the tree-root variables the parts actually use, and a write reaching the tree by
another route would evade it. It carries three premises of its own — that the scan
recognises a write at `$PGC_SRCDIR`, that it recognises one through `$_bd_root`,
and that a write into a COPY is *not* flagged — because a pattern that matches
nothing would otherwise pass this arm silently.

`test_a_symlinked_src_is_skipped_like_any_other_symlinked_build_dir` closes the
one directory that was exempt from the module's own rule. `build_dirs()` added
`root/"src"` unconditionally and applied the symlink test to every other
candidate, so a tree whose `src/` is a symlink hashed differently across the
port — `find -P` does not descend a symlinked directory argument, so the shell
hashed nothing there while the module walked it. It carries a control, because
"skip src entirely" would satisfy the arm without it.

**What is still not guarded**, named here rather than left for someone to find: a
TRUNCATED manifest — `find` returning fewer files rather than none — would produce
a plausible wrong hash that neither the per-file sentinel nor the empty-manifest
guard can see. It has not been observed. The boundary of this change is "the three
observed variants are closed", not "the function is now infallible".

### A helper that took a tree and ignored it

`_sh(srcdir, expr)` read as "evaluate one `lib.sh` expression against a tree" — the
docstring said so, nine call sites passed a fixture tree, and the body sourced the
module-global `SRCDIR`, the real source tree, instead. Whatever those arms measured, it
was not parameterised by the tree they were handed (#933).

**Which reading was intended is a measurement, not a judgement.** Every caller passes a
tree built by `_tree_with_module` or `_tree_with_source`, and none of those contains
`test/lib.sh`:

```
fixture tree holds: ['Makefile', 'objstore', 'pgcolumnar.control']
honouring srcdir:   rc=1, "No such file or directory" -- the source fails
sourcing SRCDIR:    rc=0, the function under test runs
```

So the parameter could never have worked: honouring it would have made every one of
those arms measure a failed `source` rather than the function. The arms mean the real
tree, the parameter was noise, and it is gone. The expressions that DO need the fixture
interpolate it themselves, which is why dropping it changes no behaviour.

| test | asserts |
| --- | --- |
| `test_the_unread_parameter_scan_finds_one` | the scan fires on the shape `_sh` had |
| `test_the_unread_parameter_scan_spares_fixtures_and_hooks` | five shapes it must not flag |
| `test_no_helper_in_this_corpus_takes_a_parameter_it_never_reads` | the population, corpus-wide: one before, none now |

**The class, not the instance.** The scan is corpus-wide because the defect was here and
the class is not.

**Two exclusions, both real rather than hatches.** A **test** function's parameters are
pytest fixtures: requesting one has an effect whether or not the body reads it, and three
in this corpus are legitimately unread. A **hook**'s signature is pytest's API — arguments
arrive by name — so declaring one you do not read is how a hook says which it wants.
Three of the four the scan found before this change were hooks:
`pytest_collection_modifyitems(config)`, `pytest_xdist_node_collection_finished(node)`
and `pytest_sessionfinish(exitstatus)`. Only `_sh` was a defect, so the budget was 1 and
is now 0.

### The marker answered a different question from the one it was read as (#956)

`build_once()` skips the build when a marker matches. The marker used to record
`pg_config`, the major and the **source** fingerprint, so it answered *"did this layer
last build this source?"* — and it was read as *"does the prefix hold that build?"*
Those are different claims, and the gap is not hypothetical: it cost two debugging
sessions in one day. A measurement run installed a pre-#945 library into the shared
prefix, and the corpus then reported **10 failures** here and **19** on @jdatcmd's box,
with the code entirely innocent. The source had not changed, so the old key matched.

The installed library is now part of the key. Anything may write that prefix — the shell
harness, a perf run, a manual install, another worktree — and stopping them is not the
fix; noticing is.

| test | asserts |
| --- | --- |
| `test_build_once_rebuilds_when_another_process_replaced_the_library` | a third party overwriting the library rebuilds instead of certifying, and the build actually runs |
| `test_build_once_rebuilds_when_the_library_was_deleted` | absent is not fresh |
| `test_a_prefix_that_cannot_be_observed_never_certifies` | a prefix that cannot be seen is never certified: no marker, so the next call builds |

**Why a per-source constant cannot work.** The library's digest is not a function of the
source: the build path is compiled in. @jdatcmd measured `2c9559d087b0` and
`757591c69d32` from one commit with nothing but the build directory differing. So
"this source should produce digest X" is false as soon as anyone builds elsewhere, which
is every worktree and every `devloop` arm. What is recorded instead is **the digest that
was installed when the marker was written** — a claim about this prefix over time.

**The degraded path fails CLOSED**, which it did not in the first version of this change.
When the prefix cannot be observed, **no marker is written at all**, so the next call finds
nothing to match and builds.

The first version recorded `unobserved` in the marker instead, reasoning that a degraded
decision should be readable. @jdatcmd constructed the hole rather than arguing about it:
two consecutive unobservable calls match each other and skip, which is a fail-open inside
a change about a fail-open. It was only reachable with an injected stub runner, because
`build_and_install` shells `make PG_CONFIG=<that>` and raises when it fails — but that
argument depends on `build_and_install` staying unable to succeed without a usable
`pg_config`, and nothing enforces it. One condition removes the argument.

**And the fixtures now answer, which is what makes the closure safe.** Three arms passed
`/bin/pg_config`, whose answer depends on the machine: it resolves in this container and
a CI runner may not have it. Once the prefix is observed, "does the skip happen" would
depend on that, and a guard must not. `_answerable()` supplies a `pg_config` that really
answers `--pkglibdir`, so those arms assert exactly what they asserted before, everywhere.
Verified by running the module in a mount namespace with `/bin/pg_config` bound to
`/dev/null`: premise asserted (`installed_library` returns `None` there), **39 passed**.

**The digest is computed once.** `so_md5()` already fingerprinted the installed library
with `md5sum`, so `installed_library()` shares that path rather than adding a second way
to digest one artifact. `test_this_module_keeps_no_private_fingerprint` caught the first
attempt, which reached for `hashlib` — the guard was right, and the fix is better for it.


## 6. test_docs_cover_the_corpus.py: this document, checked

**THE SWEEP GOES BOTH WAYS NOW (#908).** `undocumented()` computes tests on disk
the document fails to name; `documented_but_absent()` computes the reverse. Only
the second catches a test that is DELETED or RENAMED while its entry survives —
until it existed, that case was held by the totals line alone, and the totals line
is a merge target whose correct value is a function of the merge. Removing it
while this direction was uncovered would have retired a check silently.

**A BACKTICKED TEST NAME IS A CLAIM THAT IT EXISTS.** That is the rule the arm
enforces, and it has a consequence for prose: a name that is gone is written
WITHOUT backticks, because backticking it would assert it is still there. This
paragraph is the first place that bit — the arm reddened on my own description of
the defect.

It found two on the corpus that shipped. The rows named
test_layer_rejects_an_absence_assertion_over_an_empty_plan and a control beside
it, in section 3, and neither had ever been written. The work is real and lives in
`test_guards_pinned.py` as
`test_plan_marker_refuses_an_absence_claim_over_an_empty_plan`, documented
correctly in section 4 — so two rows claimed coverage under names never written,
and every other arm here passed over them.


The file you are reading is checked mechanically, because it went stale inside a
single rework and nothing noticed. The corpus grew from 25 tests in three files to
54 in five; the two new files, 29 tests, were named nowhere here, and the header
still said "Twenty-five tests in three files".

**A partial index of something claiming completeness reads as a total one.** A
reader who opens a file whose stated purpose is completeness does not then go and
count the tests. That is the same defect the vacuity layer refuses one level down:
a report that looks like coverage and is not.

Three properties, each mechanical:

- every `test_*.py` file in this directory is named in TESTS.md
- every `def test_` in those files is named in TESTS.md
- the totals TESTS.md states are the totals on disk

The third is what the stale header got wrong, and neither of the first two would
have caught it: a document can name every test and still miscount them. That is why
the totals are written in a fixed, parseable form -- prose that says "twenty-five"
cannot be compared with anything, which is how the wrong header survived being read
many times.

| test | asserts |
| --- | --- |
| `test_the_sweep_finds_the_corpus_rather_than_an_empty_glob` | **premise**: the sweep saw files and tests, so "nothing missing" means something |
| `test_every_file_and_test_is_named_in_the_document` | every file and test is named here, and a failure says WHICH |
| `test_a_documented_test_that_does_not_exist_is_named` | the reverse sweep: the document may not claim a test the corpus lacks |
| `test_a_document_naming_a_test_that_was_deleted_is_caught` | **removal proof**: the shape the real defect had, on a fixture |
| `test_a_documented_file_that_does_not_exist_is_caught` | a whole file can go the same way, which is how a rename shows up |
| `test_the_document_states_no_totals_for_a_merge_to_get_wrong` | the totals line must not come back; its absence is a decision, not an accident |
| `test_no_test_name_is_defined_twice_in_the_corpus` | the premise the set-equality argument needs: names must be unique |
| `test_a_name_defined_in_two_files_is_caught` | **removal proof**: the shape that defeats the argument, on a fixture |
| `test_the_corpus_counts_are_reported_rather_than_written` | the counts move to the run's output, where they cannot go stale |
| `test_a_fully_documented_corpus_reports_nothing_missing` | **control**: no false positive on a complete document |
| `test_an_undocumented_test_is_named_rather_than_passed_over` | the exact shape that shipped: file named, one test inside it not |
| `test_the_mode_inventory_states_its_own_totals_correctly` | the totals in VACUITY_MODES.md section 1a are the modes on disk |
| `test_the_readme_and_the_inventory_agree_on_what_is_refused` | README.md quotes the inventory's number, so the two cannot drift apart again |
| `test_the_inventory_accounts_for_every_mode_the_run_found` | the admitted gap row is the run's total minus what is written down |
| `test_the_prose_totals_match_the_counted_modes` | every sentence stating what the layer refuses today carries the counted number, not just the table |
| `test_the_two_halves_of_the_refused_sentence_sum_to_the_named_total` | TESTS.md states the split twice in one sentence, and BOTH halves are checked against the inventory's own count — the gated half alone let 26 + 47 = 73 past a named total of 72 |
| `test_the_inventory_names_no_entry_twice` | no bullet entry in VACUITY_MODES.md is written twice — the count guards dedupe ids, so a duplicated entry moves no total and nothing could fail on it |
| `test_a_duplicated_entry_is_caught_on_a_fixture` | **removal proof**: the shape that got through, which is a TWO-LINE bullet, on a fixture with its clean control |
| `test_a_short_repeated_bullet_is_not_flagged` | the rule's false-positive budget, measured at the length floor: below it a repeated bullet is ordinary, above it is a duplicate |
| `test_an_undocumented_file_is_caught_with_the_tests_inside_it` | how 29 tests went missing at once |
| `test_a_document_with_no_totals_line_states_none` | absent totals report `None`, which must not read as "they match" |
| `test_a_stated_total_that_disagrees_with_disk_is_visible` | the count arm's own red |
| `test_the_counting_rule_counts_a_fixture_as_the_document_says` | the mode rule, on a fixture: deduplicated, and section 3 minus its back-references |
| `test_an_id_of_fewer_than_three_words_is_not_a_mode` | the rule is three words, so `one-two` in prose is not a mode |
| `test_the_counter_stops_at_the_next_heading` | section 2's count must not reach into section 4 |
| `test_the_row_reader_takes_the_value_not_a_digit_in_the_label` | the labels contain digits; reading the first number returns the 2 from "section 2" |
| `test_an_absent_row_is_none_rather_than_a_number_that_happens_to_match` | a missing row must not read as a row stating zero |
| `test_a_stated_total_that_disagrees_with_the_ids_is_visible` | the inventory arm's own red, with the agreeing control beside it |
| `test_the_anchor_rule_drops_punctuation_and_keeps_underscores` | GitHub's derivation, on the heading the defect was found in |
| `test_an_anchor_that_strips_the_underscores_is_caught` | the exact broken link that shipped, with a control |
| `test_every_in_document_link_in_this_directory_reaches_a_heading` | every contents-list link resolves, with a coverage premise |
| `test_the_contents_list_is_numbered_in_order` | the contents list and the sections both count 1..N with no gap or inversion — the link arms above ask only whether a link RESOLVES, and a shuffled list resolves perfectly |
| `test_a_shuffled_contents_list_is_caught_on_a_fixture` | **removal proof**: the `29, 31, 30` shape that shipped, with a clean control and an omitted entry named apart from an inversion |
| `test_the_next_steps_list_is_anchored_to_the_inventory` | every section 5 entry names a mode id, so the entry can be checked at all |
| `test_no_open_next_step_names_work_the_document_calls_done` | an un-struck entry whose id reached section 2 is stale work to do |
| `test_a_stale_next_step_is_caught_on_a_fixture` | **removal proof**: the shape, planted, with the control beside it |


**Section 5 was the last unchecked part of VACUITY_MODES.md, and it was wrong (#432).**
1a, 2 and 3 are all compared against the ids on disk; "what to add next" was prose.
Entry 1 still said *"the constant exists and nothing writes it"* long after
`query_error()` existed and `test_failed_query_sentinel.py` had ten arms over it — the
most expensive place in the document for a stale sentence, because its only reader is
someone about to build something. The near-miss one document over is the argument: a
bad enumeration of `test/selftest/340` made an existing block look like a gap, and the
duplicate was written and proven to discriminate before anyone noticed.

**What is checkable is the anchor, not the work.** Entry 1's work landed under four
test names, none of them the one the entry proposed, so asking whether the NAMED test
exists would have passed and said nothing. So the arms check that every entry names a
mode id, and that no un-struck entry names an id section 2 already claims.

With every entry now struck, the second arm has nothing to refuse on the real
document. That is what the fixture arm is for.

The fixture arms exist because everything above them passes on a healthy tree, which
is exactly what a guard that does nothing also does. They run the identical functions
over a corpus built to be wrong.

**That sentence said "the five fixture arms" and nothing counted them.** Five was right
when it was written, at `3d6e1216` on 2026-09-09. It counted the arms taking `tmp_path`.
There are eight of those now, and thirteen fixture arms in total. So the number had gone
stale in the document whose subject is stale documents.

It is removed rather than corrected. One paragraph up,
`test_the_document_states_no_totals_for_a_merge_to_get_wrong` already decided that for the
same reason. A count in prose that no arm reads is a claim waiting to go wrong. The arms
are listed in the table above, where a reader can count them.

### The twin, and which half has teeth

**This section said "Nothing runs pytest. Not `run_all_versions.sh`, not any
workflow under `.github/`", and that is no longer true.** CI has a `pytest-guards`
job: it installs pytest pinned from `requirements-test.txt`, asserts `psycopg` is
absent, derives the file list from `NO_CLUSTER` in `test_harness_deps.py`, and runs
it. This file is in that list. So a guard written only here DOES fire in the gate,
and the argument that made the `.sh` half the enforcement has gone.

That argument was load-bearing, and it was stale in three places at once — here,
in `test/selftest/360`, and in `test/selftest/380` — each saying the behavioural
half could not run in CI. **A stale justification for keeping coverage in the wrong
place is harder to find than a missing check, because nothing reddens.** Nothing
was wrong; the reason was.

So the duplication is being removed in the direction the two-harness rule requires
(#432). `test/selftest/350`'s arms over this corpus and over this directory's
documents are the ones that had to read across the boundary, and they are gone; the
properties they held that this file did not yet test — the mode-counting rule's
edges and the contents-list anchor rule — moved here, where their subject is. What
stays in `350` is its arms over `ci.yml`, whose subject is the workflow rather than
either harness.

This guard reddened on its own arrival, which is the only reason it is known to
work here: adding this file moved the corpus from `(54, 5)` to `(62, 6)` and the
totals arm failed with `got '(54, 5)' want '(62, 6)'` until this section was
written.

## 7. test_connection.py: the cluster and the direct connection

| test | asserts |
| --- | --- |
| `test_cluster_fixture_gives_a_typed_connection` | `count(*)` arrives as a Python `int`, and its type is `int` |
| `test_the_extension_is_installed_and_columnar` | the fixture's cluster has `pgcolumnar` at the expected version |
| `test_the_block_compression_default_is_pinned_to_its_measurement` | `zstd:3` is still the default, pinned to #890 phase 1: the cascade runs first, so zstd works on already-reduced bytes and is +76.6% smaller at 0.92x the scan time on `rep` and +12.0% at 0.92x on `mix`. A pin, so changing it is deliberate |
| `test_a_columnar_table_round_trips_with_real_types` | `numeric` is `Decimal`, `float8` is `float`, `bytea` is `bytes`, an array is a `list` |
| `test_the_plan_shows_a_columnar_scan` | the plan arrives as parsed Python, and the scan ran |
| `test_the_provider_name_does_not_identify_a_scan` | **pins a trap**; see below |
| `test_each_test_gets_its_own_schema` | the schema is test-private and first on `search_path` |
| `test_the_worker_owns_its_own_cluster` | the port is the one derived from THIS worker's id |
| `test_the_cluster_refuses_a_foreign_server` | the identity check can return False |
| `test_the_connection_the_tests_use_is_watched` | writes through `pgc_conn` reach the zero-row guard, on both the connection and a handed-out cursor |
| `test_the_acknowledgement_is_by_cursor_against_the_real_driver` | the write is stamped on the object the caller holds, which a stub cannot prove |

Two of these deserve their reasoning stated.

**`test_the_worker_owns_its_own_cluster`** asserts `port == PORT_BASE + slot` for its
own worker, not merely that the port is an integer. The mapping from worker id to
port is injective, so if every worker's port matches its own id then no two workers
share one. Asserting "the port is an int" would have passed with every worker
sitting on 54600.

**`test_the_cluster_refuses_a_foreign_server`** points the identity check at a
datadir that is not ours and requires `False`. `pg_ctl -w` proves only that
SOMETHING answers on the port, and `lib.sh` added this check because a foreign
cluster answering would let every later assertion run against the wrong server. A
guard that has never returned False is not known to work.

### The trap that `test_the_provider_name_does_not_identify_a_scan` pins

Measured on 18.4. `Custom Plan Provider == "PgColumnarScan"` does **not** mean "a
columnar scan ran":

```
plain scan            'Custom Scan' provider='PgColumnarScan'  Columnar Projected Columns present
ungrouped vector agg  'Custom Scan' provider='PgColumnarScan'  Columnar Projected Columns ABSENT
grouped vector agg    'Custom Scan' provider='PgColumnarScan'  Columnar Projected Columns ABSENT
```

`columnar_vector.c:806` assigns the aggregate node `&pgcolumnar_scan_methods`, whose
`CustomName` is `PgColumnarScan` (`columnar_customscan.c:167`). So every pgcolumnar
node reports that provider. With the vectorized aggregate engaged the plan is a
single node and the aggregate has absorbed the scan, yet the provider still matches.

`pgc_is_columnar_scan` in `lib.sh` greps for `Columnar Projected Columns`, which is
emitted only by the scan's callback (`columnar_customscan.c:3631`) and never by
either aggregate callback. So the faithful port is `plan_marker`, and the provider
predicate answers a weaker question.

`PgColumnarAgg` never appears in a plan at all. It is the `CustomName` of a
`CustomPathMethods`, and EXPLAIN prints the scan methods' name.

This test asserts all three facts, so reverting to the provider predicate reddens
here rather than passing quietly. The first version of this harness used the
provider predicate and was wrong in exactly this way.

## 8. test_native_projection.py: the ported suite

A complete port of `test/native_projection.sh`, chosen because it is 55 lines, has 8
assertions, does no process work, and depends on nothing timing-related.

| bash check | pytest test |
| --- | --- |
| `fp fan-out matches base (a,c)` | `test_fp_fanout_matches_base` |
| `fq fan-out matches base (b)` | `test_fq_fanout_matches_base` |
| `fp row count matches base` | `test_fp_row_count_matches_base` |
| `fp storage is native` | `test_fp_storage_is_native` |
| `fp has zone maps (native skip metadata)` | `test_fp_has_zone_maps` |
| `fp reflects deletes (a,c)` | `test_fp_reflects_deletes` |
| `fp count after delete matches base` | `test_fp_reflects_deletes` |
| `fp spans multiple projection row groups` | `test_fp_spans_multiple_row_groups` |

Eight checks map to seven tests, because one test carries two of them. That is why
`compare_to_bash.py` compares names and not counts.

The port adds one assertion the original lacks: `premise: the DELETE removed rows`.
Without it, both delete arms are satisfied by a projection that never changed.

Two mechanical differences from the original, both deliberate:

- The fan-out comparisons compare row sets, not `md5(string_agg(...))`.
- `fp has zone maps` and `fp spans multiple row groups` stay numeric through
  `at_least`. The original turns each into the string `yes` or `no` via
  `[ "$(...)" -ge 1 ]`, which converts a number to text and then compares text. A
  non-number becomes `no` there and is refused here.

### How this port is proved

Running it green proves little on its own. It is proved by the differential:

```
ARM A unmutated          .so 8370e9b1beba   bash 8 passed 0 failed   pytest 7 passed 0 failed
ARM B fan-out neutered   .so 9e9510593777   bash 0 passed 8 failed   pytest 0 passed 7 failed
```

The mutation makes `PgColumnarProjectionFanoutRow` return without writing. Each arm
builds and installs once, and both harnesses print the `.so` md5 they measured, so
an arm where the two differ is void rather than reported.

## 9. test_ordered.py: the ordered oracle

`lib.sh` has two oracles and this port had one. `pgc_set_hash` sorts before hashing,
so a bash test naming `ORDER BY` and comparing with `diff_query` cannot fail on
order; `pgc_seq_hash` and `diff_query_ordered` are the ones that can. These nine
tests port that pair and its premise check.

| test | the guard | what it stops |
| --- | --- | --- |
| `test_ordered_rows_refuses_a_sequence_whose_order_is_unobservable` | an ordered claim over a constant sequence is refused | forward equals reverse, so order asserts nothing |
| `test_ordered_rows_refuses_two_empty_sequences` | two empty sides are refused here too | inherits `rows()`'s refusal instead of losing it |
| `test_ordered_rows_accepts_a_real_ordering` | **positive control** | a genuine ordered claim still passes |
| `test_ordered_rows_fails_on_the_wrong_order` | the oracle detects order | proves it can fail, not merely that it permits |
| `test_layer_refuses_sorting_the_input_to_an_ordered_claim` | `sorted()` feeding `ordered_rows` is uncollectable, found by AST | `ordered_rows(sorted(got), sorted(want))` cannot fail on order |
| `test_layer_refuses_a_name_bound_to_a_sorted_call` | `g = sorted(got)` one line above the claim is the same collapse | the inline spelling was the only one caught, so the guard was blind to the version least likely to be noticed |
| `test_a_conftest_cannot_switch_off_the_order_collapse_guard` | a conftest rebinding `_ORDER_KILLERS` cannot silence the scan | two lines of conftest is less to type than the honest form, and that is the hatch the layer forbids |
| `test_layer_refuses_a_list_sorted_in_place` | `got.sort()` kills the order and leaves the name spelled the same | nothing at the call site says anything happened |
| `test_layer_allows_a_name_sorted_after_the_claim` | **control** | a name sorted AFTER the claim did not affect it; refusing that would be a false red |
| `test_the_order_killer_scan_is_one_function_deep` | **pinned limit** | a sort behind a helper is not caught, and this arm reddens if that documented limit ever moves |
| `test_ordering_observable_requires_the_two_directions_to_differ` | a fixture reading the same forwards and backwards is refused | the premise `pgc_check_ordered_oracle` asserts in bash |
| `test_ordering_observable_passes_when_the_directions_differ` | **positive control** | a real fixture is untouched |
| `test_the_two_oracles_are_different_instruments` | the set oracle and the sequence oracle must disagree on a permutation | if they agree, one of them is not the instrument it claims to be |
| `test_row_set_still_refuses_two_empty_sides` | **regression control** | adding the sequence oracle must not weaken the set one |

The third property is the one worth reading twice. Two oracles that always agree are
one oracle with two names, and a suite built on them would pass every ordering claim
by construction. The test feeds both a permutation and requires the set oracle to
accept while the sequence oracle rejects.

The AST scan matters for the same reason the broad-`except` scan does. A line regex
for `sorted(` fired inside the `pytester.makepyfile` string of the test that tests
it, so the guard rejected its own corpus. Walking the tree and looking at real call
nodes is the only version that distinguishes code from a string holding code.

## 10. test_runshape.py: the shape of the run itself

The other guards ask whether a test asserted anything. These six ask whether the
**run** did. Each of the three failure shapes turns a whole session green rather
than a single test, which is why they were built before the rest of the backlog.

| test | the guard | bare pytest, measured |
| --- | --- | --- |
| `test_layer_fails_when_a_collected_test_never_reports` | the reported node-id set is reconciled against the collected one | 6 collected, 5 reported; the crash is named, the lost test is not |
| `test_layer_accepts_a_run_where_every_test_reports` | **positive control** | an honest parallel run is untouched |
| `test_layer_rejects_a_parametrize_over_an_empty_list` | an empty parameter set fails the run | `1 skipped`, exit 0 |
| `test_layer_accepts_a_parametrize_with_cases` | **positive control** | a real parameter set is untouched |
| `test_layer_rejects_a_fixture_that_skips` | a skip arriving during setup fails the run | every dependent test skips, exit 0 |
| `test_layer_allows_a_declared_unrunnable_test` | **escape hatch and control** | `expect.cannot_run` records a counted assertion instead of skipping |
| `test_layer_allows_a_deliberately_selected_subset` | `-k` is a deliberate act, not tests lost | the guard reported 15 deselected tests as never reported and failed a healthy run |
| `test_layer_allows_an_explicitly_deselected_test` | `--deselect` reaches the same hook by another route | pinned separately so one fix cannot cover only one spelling |
| `test_a_run_that_both_deselects_and_loses_a_test_still_fails` | **the distinguishing arm** | subtracting the deselected ids is right only if a genuinely lost test is still caught |

Half of these are controls, and deliberately so: a run-shape guard fires on the whole
session, so a false positive costs the entire suite rather than one test.

Read that first row precisely, because bare pytest is not silent here: it exits 1
and prints `worker 'gw1' crashed while running 'test_loss.py::test_kills'`. What it
never mentions is `test_loss.py::test_d`, which was collected, assigned to the dead
worker, and never ran. Measured on a 6-test corpus under `-n 2
--max-worker-restart=0`: 6 collected, 5 node-ids reported, and the missing one
appears in no line of the output. A suite whose crash happens to land on a test
already expected to fail therefore reports exactly what you expected while running
fewer tests than you wrote.

Two things about the reconciliation took a measurement to get right. Under `xdist`
the **workers** collect, not the controller, so the controller's collected set stayed
empty and the guard was present and blind until it also listened to
`pytest_xdist_node_collection_finished`. And the state has to live on a per-config
plugin instance rather than module globals: `pytester.runpytest()` runs the inner
session in-process, so module-level sets leaked between these tests and the sessions
they drive. The corpus reported 44 passed and exited 1.

The empty-parametrize refusal carries its own message rather than folding into the
bare-skip refusal. When a corpus glob matches nothing, the cause the reader needs to
see is the corpus, not the marker.

### Where a guard runs decides what the run reports

A guard implemented as a **fixture teardown** cannot fail the test it guards. pytest has
already recorded the call phase as passed, so the refusal arrives as a separate `ERROR`
on the same node-id and the test's own outcome stays `passed`. Anything counting
passes — `--pgc-expect-tests`, a CI summary, a human reading "N passed" — sees a pass.

Measured, the same `AssertionError` raised from each place:

| raised from | the run reports |
| --- | --- |
| a `pytest_runtest_call` wrapper | `1 failed` |
| a fixture teardown | `1 passed, 1 error` |

This layer's vacuity guard is in a `pytest_runtest_call` wrapper, which is why a test
that concludes nothing is *failed* rather than passed-with-an-error. Two arms keep that
from being an accident, and the second is the control: without it the first passes
whatever phase the guard is in, because "a vacuous test fails" is equally true of a
correctly-placed guard and of no guard at all beside an unrelated failure.

| test | what it asserts | how it fails |
| --- | --- | --- |
| `test_the_vacuity_guard_fails_the_test_rather_than_erroring_beside_it` | a test concluding nothing is `failed`, with no error | moving the guard into the `expect` fixture's teardown reddens it |
| `test_a_guard_in_a_teardown_would_report_a_pass_which_is_why_it_is_not_there` | a teardown refusal leaves the test reported `passed` with an error beside it | the mode stated as a measurement rather than a warning |

This closes the part of `guard-as-teardown-fixture-still-reports-passed`
(`VACUITY_MODES.md` 3.7) that is about **this layer's own guard placement**. It does not
close the family: a guard anyone adds later in a teardown is still a guard that cannot
fail its test, and nothing refuses that shape in general.

## 11. test_zonemap_boundaries.py: exact boundaries

### `test_exact_zonemap_boundaries`

Pairs with `test/zonemap_boundaries.sh`. Two monotonic 1,000-row groups put
`1001` exactly at the second group's minimum and `1000` exactly at the first
group's maximum. Heap-row comparisons pin that `<= 1001` and `>= 1000` keep
their boundary rows. Work-done counters, with bloom disabled, pin the
correctness-preserving cases: `< 1001`, `> 1000`, and `= 1001` each remove one
group.

The five one-token strategy mutations make the corresponding assertion fail:
`<=` and `>=` lose one row, while `<`, `>`, and `=` remain row-correct but
remove no group. This distinguishes correctness coverage from
pruning-effectiveness coverage rather than relying on incidental fixtures
elsewhere in the matrix.

## 12. test_saop_element_pushdown.py: scattered set pruning

### `test_scattered_saop_prunes_each_element`

Ports the #752 additions to `test/native_saop_pushdown.sh`. A monotonic 40,000-row
fixture has twenty row groups. The scattered set `{100,20100,38100}` spans the
table, so its old `[min,max]` hull removes zero groups while per-element pruning
removes seventeen. The contiguous `{100,101,102}` set is the negative control:
its hull and its elements both remove nineteen groups. Exact 128- and 129-element
arms pin both sides of the bounded fallback.

A by-reference text set with three values plus NULL pins the NULL compaction
whose absence dereferences a null Datum. Its integer companion proves NULL
removal retains pruning. The `ov` fixture gives every group the same `[10,88]`
zone, so a set of absent odd values can remove groups only through the
per-element bloom loop.

The shell and pytest forms were both run red before implementation (`0`, wanted
`17`), green afterward, then red again with the per-element threshold mutated to
zero. The fixture row count and columnar plan marker are premises, and both query
answers are checked independently of the pruning counters.

## 13. test_hilbert_locality.py: what the Hilbert curve buys

The pytest twin of `test/hilbert_locality.sh`, written in the same change under the
owner's rule of 2026-09-09 that every new test ships in both harnesses. Twelve tests.

It measures one thing -- how many chunk groups a range query reads under Z-order
against Hilbert -- and spends most of its arms refusing to measure it when the
comparison would be meaningless.

| test | what it refuses |
| --- | --- |
| `test_every_layout_verb_ran_without_raising` | a verb that raised looks identical to a verb that no-opped |
| `test_the_source_holds_the_rows_both_arms_will_load` | an empty fixture |
| `test_both_arms_hold_the_identical_row_multiset` | the two arms holding DIFFERENT DATA, which made the first pilot's ratio a fact about the data rather than the curve |
| `test_the_fixture_is_two_dimensional` | a fixture where one column does not span, so the curve has nothing to interleave |
| `test_both_arms_have_the_group_count_measured` | a group meaning a different unit on each arm |
| `test_the_two_partitions_differ` | the case where the curves cut the SAME partition, where no query can separate them |
| `test_two_tables_on_the_same_curve_are_one_partition` | the null control: same curve twice must be one partition |
| `test_dense_dyadic_grid_is_one_partition` | the dense power-of-two grid, which provably cannot separate the curves |
| `test_both_arms_plan_as_a_columnar_scan` | a counter read from a plan that is not the columnar scan |
| `test_parallelism_is_off_so_a_counter_is_a_fact_about_the_layout` | a per-worker counter read as a whole-query one |
| `test_the_predicates_are_usable_and_the_denominators_match` | unequal denominators, and zero usable skip predicates |
| `test_groups_read_over_sixty_placements` | nothing -- this is the measurement |

**The two controls REFUSE rather than returning 1.0.** Both are cases where the
curves genuinely cut the same partition, so a ratio would be arithmetic on two
identical numbers. A harness that reported `1.0000` there would look like a
measurement and be an artifact.

**Why sixty placements and not one.** A single query-box origin measures where that
box happened to land. At one origin the differences were 1, 1, 0 and 1 groups, and
the ratio read `2.000` off a single group.

**What the twin does NOT carry**, and the bash suite does: the exact-integer pins.
The twin asserts Hilbert reads fewer groups at every box; `test/hilbert_locality.sh`
pins the eight counts exactly. Its header records why -- for a CURVE change the
digest pins upstream catch it first and the integers add nothing, so their real
domain is a changed READER at an unchanged layout.

## 14. test_suite_accounting.py: the matrix accounting for its own suites

`test_suite_accounting.py` holds the matrix runner to its own arithmetic.

`run_all_versions.sh` prints `suites that ran: N of M` and never checks it, and twelve
registered suites exit 0 without ever calling `pgc_summary`. Measured with a pattern
tight enough to exclude `portlib.sh` -- a looser one matched it and gave both reviewers
of this change the same wrong answer: **none** of the twelve sources `test/lib.sh`.
Each defines its own `check()`, and ten keep no tally at all, so the harness cannot see
their checks. Counted among the suites that
"ran", they are the overcount #447 added that line to stop, one level further down.

A count cannot close this. Two errors of opposite sign cancel, and an exempt list
maintained by hand makes the count agree by construction -- the check then measures
the list rather than the run. So membership is derived from a property each suite
carries, and the two readings are reconciled as SETS, in both directions:

| reading | where it comes from |
| --- | --- |
| declared | the suite's own text calls `pgc_summary` |
| observed | the suite's log carries the `accounting:` line `pgc_summary` prints before every exit path |

Neither is a number and neither is hand-maintained. A suite that stops calling
`pgc_summary` moves between the sets on its own.

These tests drive the SHELL functions out of `run_all_versions.sh` rather than
reimplementing them in Python. A Python twin would be a second implementation and
would agree with itself; the house rule asks for two observers of one implementation.
They run under `set -o pipefail`, because `harness_selftest.sh` does.

### `test_a_suite_that_calls_pgc_summary_declares_accounting`

The property is the CALL. A comment mentioning `pgc_summary`, and a longer name
containing it, are both refused -- a claim satisfied by prose is the failure the whole
design exists to avoid.

### `test_the_accounting_line_is_read_on_every_exit_path`

Pass, failure, skip and incomplete all carry the line, which is what makes it the
runtime twin of the declaration rather than a synonym for PASSED.

### `test_the_reconciliation_names_both_directions`

Declared-but-not-accounted is a suite that died before reaching its summary; today
that reads PASS whenever the shell happened to exit 0. Accounted-but-not-declared is a
stale reading of the source, which a hand-maintained list can never report.

### `test_opposite_errors_do_not_cancel`

Both directions are reported from one run. One error masking the other is exactly what
a count cannot distinguish from correctness.

### `test_the_driver_s_own_non_dispatch_record_excuses_only_what_it_names`

`PGC_SKIP_TIMING` drops four suites on every CI run; they declare accounting and
correctly produce none. The driver records that decision where it makes it, rather
than leaving it to be inferred from the log the driver forges. The record excuses only
what it names, and a suite that both accounted and was recorded as never dispatched
fails.

### `test_the_printed_identity_can_actually_fail`

`inputs == sum(buckets)` is printed beside every reconciliation. Computing `inputs`
FROM the buckets makes the line true for any values and reddens nothing, which is why
it is counted from the two files by a separate route. Dropping the sort before `comm`
makes the totals diverge, and that is the fault the identity guards.

### `test_the_reader_accepts_the_line_the_producer_actually_emits`

Every other log in the file is a literal, and the shell half types the same four again,
and the format string lives a third time in `pgc_summary`. Three hand-written copies of
one line: a wording drift in the **producer** leaves both harnesses green while the
reader answers "no" for every real suite, reddening the whole matrix on both majors.
So this arm runs a real suite and feeds the reader its actual stdout, with a reworded
control to show it can fail.

### `test_the_partition_over_the_registered_suites_adds_up`

The readers run over the real registered suite list. No count is asserted: how many
suites are exempt is not a fact about correctness, and pinning it would be a second
copy of the list this design removes. What is asserted is that the partition covers
the population and that both buckets are occupied.

### `test_the_accounted_reader_takes_either_runtime_mechanism`

Two runtime-observable mechanisms exist: `pgc_summary`'s accounting line, used by 239
suites, and a suite's own `checks run:` line, which `bench_guards` and `docs_style`
print from private counters without ever sourcing `lib.sh`. A reader that knew only the
first would call those two unaccounted, which is false.

### `test_a_registered_suite_accounted_by_nothing_fails_by_name`

The defect @linuxhikerpm blocked #922 on. `pgc_reconcile_accounting` takes the declared
and observed sets, both derived from the suites themselves, so a registered suite in
neither is **outside the universe it reconciles** — with all its inputs empty it reports
complete symmetry and returns 0, whatever `SUITES` holds. Treating absence of a
declaration as absence from the population preserves the overcount.

`pgc_reconcile_population` takes the registered set as an input and puts every
registered suite in exactly one of four buckets: accounted, not dispatched, known debt,
or unaccounted — and unaccounted fails, by name. Each of the three ways out is asserted
to actually let a suite out, or the bucket would be a name for "always fails".

### `test_the_debt_file_excuses_only_what_it_names`

Debt is recorded by name rather than as a count, which is what makes it a burn-down: a
new unaccounted suite fails while the known ones are excused. Debt that is no longer
debt — a suite that now accounts, or one no longer registered — is reported, so the
burn-down cannot stall silently. Those two are reported rather than fatal: a gate that
reddens the moment someone *fixes* something teaches people not to fix things.

### `test_the_population_partitions_and_prints_its_identity`

`inputs == sum(buckets)` over the registered population, printed per the house rule.
Like the symmetry check's identity it **cannot** be false on the data — the four buckets
are built by successive subtraction from the registered set, so their sum equals it
identically, measured at 0 firings over 400 random four-set inputs while the real bucket
findings fired on 353. What it guards is `comm` reading unsorted input, which produces
buckets that are not a partition at all.

### `test_the_debt_file_is_tracked_and_holds_only_registered_suites`

`test/suites_without_accounting.txt` is tracked so that adding a name is a diff a
reviewer sees — the whole reason it is a file and not a number in the environment. Every
name in it must be a registered suite.

### `test_the_declaration_reader_survives_pipefail_on_a_long_suite`

A regression arm. The first implementation piped `sed` into `grep -q`; grep exits on
match, sed takes EPIPE, and `pipefail` reports the pipeline as failed. The reader
answered "no" for a suite that plainly calls `pgc_summary`. It is a race, so it
reproduces on long files and not short ones -- it passed every fixture and failed only
on the real population, naming two of the longest suites. Selftest 040 carries the same
story from #473 and #476.

## 15. test_harness_deps.py: the harness must self-test without a database

`conftest.py` imported psycopg at module scope, and conftest is imported before
every run, so a **database driver was a hard requirement of the whole corpus** --
including every test that never opens a connection. With psycopg absent the run
did not fail a test, it failed to COLLECT:

    ImportError while loading conftest '.../conftest.py'
    conftest.py:15: in <module>
        import psycopg
    E   ModuleNotFoundError: No module named 'psycopg'

With the import deferred into the two fixtures that connect, the database-free
files run and pass with no driver installed; with it at module scope, none of them
do. That coupling is half of why the guard-testing part of this corpus cannot run
where the gate runs (README.md, "This is not in the gate yet").

**WHICH FILES NEED NO DATABASE IS DECIDED, NOT DECLAIMED.** `NO_CLUSTER` in that
module is the declaration, and the gate's job runs exactly it. The property is
computed from the corpus by an ast walk, and the two must agree in BOTH
directions.

The missing direction was the one that loses coverage. The only arm over the list
asked whether the files it names EXIST, so a database-free file nobody added was
simply absent from the job: every arm stayed green and nothing said so. It had
already happened twice -- `test_build_refusal.py` and `test_layer.py` both need no
database and neither was listed -- and a concurrent branch adds a third. A floor of four on the list length did not help: the list had four
entries, so the floor was satisfied by the state it was meant to police.

AN AST WALK RATHER THAN A LINE REGEX, because three shapes here defeat a grep: a
file may name the driver in a docstring, discuss a cluster fixture in prose, or
BUILD another test as a string for `pytester`. This layer has already paid for that
lesson once -- the broad-except refusal was first written as a line regex and
rejected its own tests, because the forbidden shape appears inside a
`makepyfile` string.

AND NEEDING A DATABASE IS NOT IMPORTING THE DRIVER. A test reaches a cluster
through a FIXTURE and may import nothing, so a file is cluster-bound if it imports
the driver at module scope (which kills collection outright), if any test or
fixture in it requests -- directly or transitively -- a fixture that reaches a
cluster, or if it DRIVES a cluster-bound file as a subprocess. The connecting
fixtures are read off `conftest.py` rather than named in the classifier.

| test | asserts |
| --- | --- |
| `test_the_guard_half_of_the_corpus_runs_without_a_database_driver` | the no-cluster files collect and pass with `import psycopg` shimmed to raise |
| `test_a_cluster_test_still_needs_the_driver` | **control**: deferring made the IMPORT lazy, not the database optional |
| `test_conftest_imports_no_database_driver_at_module_scope` | the regression named in one line, for whoever edits conftest next |
| `test_the_declaration_is_exactly_the_database_free_half` | `NO_CLUSTER` equals the property, both ways, so an undeclared database-free file is named |
| `test_the_declaration_names_each_file_once` | the list's cardinality, which `membership_report`'s set comparison cannot see. Measured: pytest deduplicates the paths, so the cost is the job's own printed file count, not a double run |
| `test_the_partition_accounts_for_every_file_in_the_corpus` | **premise**: every file lands in exactly one bucket, and neither bucket is the whole corpus |
| `test_the_cluster_fixtures_are_read_off_conftest_rather_than_named_here` | the roots of the property are derived from `conftest.py`, not typed |
| `test_the_classifier_tells_a_plain_file_from_one_that_requests_a_cluster` | the base case and its control, over a fixture corpus |
| `test_the_classifier_follows_a_cluster_fixture_through_a_local_wrapper` | a module-local fixture wrapping `pgc_cluster` is followed |
| `test_the_classifier_catches_a_module_scope_driver_import` | an eager import kills collection, so the file cannot run in the job |
| `test_the_classifier_is_not_fooled_by_prose_that_names_the_driver` | a docstring, a block-comment string, a generated test, and a file merely discussed |
| `test_the_classifier_takes_a_fixture_that_provisions_without_connecting` | the second signal, isolated: a fixture that starts a cluster and imports no driver |
| `test_the_classifier_follows_a_conftest_fixture_that_connects_indirectly` | a conftest fixture reaching a cluster through a sibling, importing nothing itself |
| `test_the_classifier_does_not_read_a_helpers_parameter_as_a_fixture` | pytest resolves names for tests and fixtures, not for helpers |
| `test_the_classifier_follows_a_file_that_drives_a_cluster_bound_file` | this file's own shape: driving a cluster-bound file inherits what it needs |
| `test_the_membership_report_names_a_database_free_file_left_undeclared` | the hole itself, on a fixture, with the control beside it |
| `test_the_membership_report_names_a_declared_file_that_needs_a_cluster` | the other direction: a listed file that starts using a cluster fixture |
| `test_the_membership_report_names_a_declared_file_that_is_gone` | a rename is still caught, and as its own kind rather than as a cluster need |
| `test_the_gate_runs_the_membership_decision_rather_than_only_this_file` | selftest 350 runs the decision, and the command line it uses works |
| `test_ci_derives_the_file_list_rather_than_repeating_it` | the CI job asks this module for `NO_CLUSTER`, names no file literally, and states no count |
| `test_the_job_installs_no_database_driver` | the job asserts psycopg is absent rather than assuming it |
| `test_the_cluster_job_runs_the_other_half_and_derives_it` | the complement of `NO_CLUSTER` is RUN, derived not listed, and asserts the driver IS present |
| `test_both_pytest_jobs_assert_how_many_tests_they_collected` | both jobs pass `--pgc-expect-tests` from the tracked file, and each guards the read |
| `test_the_shell_reference_detector_sees_code_and_not_prose` | the premise: a docstring is prose, a string passed to bash is a reference, an f-string counts once |
| `test_the_harness_independence_inventory_is_exactly_what_the_corpus_does` | CONTEXT.md's inventory, asserted in both directions |

**THIS IS NOW IN THE GATE.** `.github/workflows/ci.yml` runs a `pytest-guards`
job: no database, no build, an interpreter and the two pinned runner packages.
The file list is derived from `NO_CLUSTER` in this module and the pins from
`requirements-test.txt`, so neither is a second copy that can go stale -- and
three arms above hold it to that. **The job states no count and neither does this
section**: it prints how many files it ran and pytest prints how many tests
passed, so the numbers reach a reader from the run. A written count is a
hand-maintained derived value, and the one in the job's comment was wrong the day
it was written (#908).

BUT THE ARMS IN THIS FILE DO NOT RUN IN THE GATE, and that is why
`test/selftest/350-the-pytest-corpus-must-be.sh` runs the membership decision
through this module's command line. The corpus is not in `SUITES` (README.md), and
the `pytest-guards` job runs the database-free files -- which this file is not,
because its control arm needs a real cluster. A guard that does not run is a
comment, so the decision has a copy with teeth, exactly as the documentation
sweep in that part does.

A SHIM RATHER THAN AN UNINSTALL. Uninstalling psycopg would test the machine
rather than the harness, could not run beside anything else, and would leave the
environment broken if the test died. A module that raises on import, first on the
path, is the same observation and reversible by construction. Both behavioural
arms assert the shim actually bites before believing anything it produces.

### The harness-independence inventory, as a mechanism

CONTEXT.md's rule is that the two harnesses are parallel in functionality and
independent in implementation: **a pytest test that drives `test/lib.sh` is the first
measurement wearing a Python wrapper**, so it agrees with the shell by construction and
can never report it wrong. Its inventory of what still reaches across was **prose** —
falsifiable by hand, but nothing reddened when a new reference appeared. #923 nearly
landed a fourth coupled file, and what caught it was a person reading.

`SHELL_REFERENCES` declares the three files that reach across and **why each one does**,
and the arm asserts set equality in both directions. A new file that reaches in reddens
it; a file that stops reaching and is left in the declaration reddens it too — which is
what stops the list rotting into a permanent exemption, the way every hand-maintained
exempt list in this tree has gone wrong.

**A file-level guard, which is what the rule asks for and also the most it can honestly
be.** Within a flagged file it cannot tell a path joined onto the real tree from the
same name joined onto a `tmp_path`: both are the string `lib.sh`, and only the dataflow
says which. `test_build_refusal.py` contains both, and CONTEXT.md already records the
fake ones as rule 2 rather than references. So the assertion is over the **set of
files**, and each entry carries the mechanism a reader needs to check it by hand.

**Two things the first version got wrong, both found by running it.** It counted an
f-string as two references, because the pieces of one are `Constant` nodes of their own.
And it flagged **this file**, because the declaration's own descriptions named the shell
files — four files where the tree has three. The descriptions now name the mechanism
without the filenames, and the detector's fixtures assemble the name from fragments. A
scan flagging its own test data is the third time that shape cost a measurement in one
session.

## 16. test_harness_deps_classifier.py: the classifier, in the file the gate runs

`test_harness_deps.py` defines the classifier that decides which files the
`pytest (harness guards, no database)` job runs — and that file is itself classified
cluster-bound, correctly, because it hands real cluster-bound file names to pytest in
a subprocess. So the job's file list is `NO_CLUSTER`, and the code that computes
`NO_CLUSTER` was the one thing the job never ran.

@linuxhikerpm measured the consequence: disabling transitive conftest-fixture closure
left shell selftest 350 green and every CI-selected test passing, and only the excluded
targeted test failed. A load-bearing branch could break with both real gates green.

These arms drive a corpus they write in `tmp_path` and read nothing from the real tree,
so this file needs no database and is declared in `NO_CLUSTER`. It imports the
classifier as a **library**, which does not make it cluster-bound: the propagation rule
reads string constants naming corpus files, not imports.

### The classifier's branches

| test | what it asserts |
| --- | --- |
| `test_the_classifier_tells_a_plain_file_from_one_that_requests_a_cluster` | the base case and its control |
| `test_the_classifier_follows_a_cluster_fixture_through_a_local_wrapper` | a local fixture wrapping a conftest root |
| `test_the_classifier_catches_a_module_scope_driver_import` | importing the driver at module scope is enough |
| `test_the_classifier_is_not_fooled_by_prose_that_names_the_driver` | a docstring naming psycopg is not an import |
| `test_the_classifier_takes_a_fixture_that_provisions_without_connecting` | a fixture that provisions but never connects is still a root |
| `test_the_classifier_follows_a_conftest_fixture_that_connects_indirectly` | the transitive closure inside conftest, which is the branch that was ungated |
| `test_the_classifier_does_not_read_a_helpers_parameter_as_a_fixture` | a helper's parameter is not a request |
| `test_the_classifier_follows_a_file_that_drives_a_cluster_bound_file` | driving another file inherits what it needs |

### Ordinary pytest dependency forms

The classifier read module-level `def`s and positional parameters. pytest resolves a
fixture through five more shapes, and @linuxhikerpm built a direct fixture in each:
every one was classified database-free and then failed under the no-driver shim.
Measured against the classifier as it was:

| form | before | after |
| --- | --- | --- |
| a test method inside a class | **free** | bound |
| `@pytest.mark.usefixtures` | **free** | bound |
| a keyword-only fixture parameter | **free** | bound |
| `request.getfixturevalue` | **free** | bound |
| an aliased cluster root in `conftest.py` | **free** | bound |
| module-level positional | bound | bound |

The fifth needed a shape the review did not give. An alias on the *test* side is caught
anyway, because the underlying fixture still takes the root positionally; it is an alias
on the **root**, in conftest, that hides it — the root was recorded under the def's name
while a test requests it under the alias. Measured: `roots=['_mk']` before, `roots=['conn']` after.

`request.getfixturevalue` is not supported and not banned. The name is computed at run
time, so no AST can resolve it, and such a file is classified **cluster-bound** —
wrong in the direction that costs CI time rather than the direction that greens a gate
over tests nothing ran.

| test | what it asserts |
| --- | --- |
| `test_a_test_method_inside_a_class_is_a_fixture_request` | pytest collects `test_*` methods of a class |
| `test_usefixtures_is_a_fixture_request_without_a_parameter` | a dependency with no parameter |
| `test_a_keyword_only_parameter_is_a_fixture_request` | `def test_x(*, pgc_conn)` |
| `test_a_dynamic_request_is_treated_as_cluster_bound` | unresolvable means conservative, not free |
| `test_an_aliased_cluster_root_is_found_under_the_name_tests_request` | the root is read under its requestable name |
| `test_a_plain_test_and_a_helpers_parameter_stay_database_free` | the cost side: a rule that calls everything bound would empty the gate |
| `test_usefixtures_on_a_class_reaches_its_methods` | pytest applies a class decorator to every method, which is the form class-method descent exists to serve |
| `test_a_module_level_pytestmark_reaches_every_test` | `pytestmark = pytest.mark.usefixtures(...)` is a dependency of every test in the file and of no signature |
| `test_a_pytestmark_written_as_a_list_reaches_every_test_too` | the list form is what a file uses once it has two marks |
| `test_an_unrelated_class_decorator_does_not_bind_anything` | the cost side: only `usefixtures` is a dependency, or every parametrised class would be cluster-bound |
| `test_a_file_whose_generated_tests_import_the_driver_is_driver_dependent` | cluster-free and still unrunnable where there is no driver, so the job's list is the intersection of two properties |
| `test_a_file_that_only_PARSES_a_driver_import_is_job_runnable` | the control: a driver import in a string nothing runs is not a dependency, and reading only the string would exclude this very file |
| `test_prose_naming_the_driver_is_not_a_driver_dependency` | a docstring naming psycopg is a sentence about code |

## 17. Adding a test

0. **Write it twice.** Every test in this tree ships as a `.sh` suite and a pytest
   test **in the same change** (jd, 2026-09-09). Not ported later, not one or the
   other. Only the `.sh` half runs in the gate today, and only the pytest half gets
   typed results and a real connection, so a test that exists in one harness is not
   finished. Where the two differ in force, say which is which in both headers.
1. Write the failing test first and run it. Confirm it fails for the reason you
   intend, not because a helper or module is missing. A red on `ImportError` proves
   only that a file is absent.
2. Give every assertion a name. Porting a bash check means reusing its exact name
   string.
3. Assert the premise. If a fixture is supposed to write rows, assert that it did.
4. If the test needs an escape hatch, give it a reason rather than a flag.
5. Run `compare_to_bash.py` if you are porting, and expect it to report every bash
   property as covered.
6. Run serially and with `-n 4`. A test that passes only in one of those is
   order-dependent or shares state.
7. If you add a guard, add the red test that proves it fires, and a control that
   proves it does not fire on a legitimate test.
8. Run `test/harness_selftest.sh`. **The harness's own selftests police this
   directory too.** `test/selftest/300-a-test-script-must-be-runnable.sh` requires
   that any file declaring an interpreter be executable, and the first version of
   `compare_to_bash.py` was mode 644 with a `#!/usr/bin/env python3` line. That
   failed the selftest on both majors of the matrix, which is how it was found. A
   new directory under `test/` inherits every rule the old ones follow.

## 18. What this corpus does NOT yet refuse

`VACUITY_MODES.md` is the inventory: 79 ways a pytest harness can report a pass while
asserting nothing, 73 of them demonstrated by an actual run. **This layer refuses 28
of them.** The other 44, of which 43 were demonstrated, are listed there with the
refusal design each would need and the order worth building them in.

Read it before adding a test. One gap is most likely to affect a new test now.

**`insert-wrote-no-rows` closed, and this paragraph used to be the gap.** It said a
write was not required to have written anything, which stopped being true when
section 22 landed: every write the test connection runs is recorded from the server's
command tag, and a test that ran one reporting 0 rows fails unless it named the zero.
The sentence is rewritten rather than deleted because a reader who knew the gap needs
to find out where it went -- the same reason `VACUITY_MODES.md` keeps a
back-reference for every mode that moves.

**A `pytest.raises` block can still catch a failure from its own setup.** Section 17
closed `raises-too-broad` — a broad family with no SQLSTATE pinned does not collect —
and only NARROWED `raises-catches-setup`. The block must hold one top-level
statement, so two shapes still walk past it: a call to a helper that performs the
setup, and a compound statement such as a `for` holding the setup and the statement
under test. Both are pinned by arms that assert the scan reports nothing on them, and
`VACUITY_MODES.md` section 3.4 says what would close the mode.

## 19. Traps this corpus records

Recorded because each one produced a confident wrong result before it was caught,
and all are the same family as the defect the layer exists to prevent.

**A `UsageError` is written to stderr.** Two of the layer's tests assert on a
message and both were checking stdout at first. They still exited non-zero, so
`assert result.ret != 0` passed and the tests looked correct. Every message
assertion here now names its stream.

**A red test can fail for the wrong reason.** The first guard's red state was
`ImportError: No module named 'pgc_vacuity'`. The module had to exist and simply
not guard yet before the red meant anything.

**`git checkout` restores source, not the installed library.** After the mutation
arm, the source was clean and `/usr/local/pg18a` still held the mutated `.so`, so
the next run tested a mutated binary against clean sources. The harness prints its
`.so` fingerprint on every run, which is the only reason this was visible.

**Counting is a fragile instrument.** A `grep -c " PASSED"` reported 6 of 7 tests,
because the first test's outcome shares a line with a fixture's print. A `head` in
the differential truncated its own summary line. Both produced a report that looked
complete. This is why the property comparison reads names.

**`ps | grep "[p]attern"` can match its own shell.** The bracket protects the
enclosing command line only while the plain word appears nowhere else in it. A probe
whose body also contained `/tmp/pgc-pytest-*` counted its own invocation as a leaked
process. Walking `/proc/<pid>/cmdline` is the reliable instrument.

`test_one_tree_hashes_one_way_however_the_locale_is_set` requires one tree to
give one fingerprint across every installed locale.

## 20. test_raises_sqlstate.py: which error, and which statement

Numbered 17 rather than inserted after section 4, where a reader looking for a
per-file section would expect it. Renumbering twelve headings and their Contents
anchors while sibling branches are editing this file buys a reader nothing and
costs a merge; the Contents entry above is what makes it findable.

**What this file is for.** `pytest.raises(psycopg.Error)` claims that one of 254
SQLSTATEs arrived, across 42 SQLSTATE classes — counted against psycopg 3.3.5 by
asking how many classes in `psycopg.errors` carry a `sqlstate` and subclass that
family. It does not claim even that much. Measured on this tree before the guard
landed:

    with pytest.raises(psycopg.Error):
        conn = psycopg.connect("host=/nonexistent-socket-dir dbname=pgc")
        conn.execute("SELECT pgc_definitely_no_such_function()")
    expect.num(1, 1, "the server rejected the call")

reported `1 passed`, exit 0. What satisfied the claim was `OperationalError` with
`sqlstate` None: the connect failed, nothing reached a server, and the statement the
test is about never executed. Against a live PostgreSQL 18.4 the same shape raises
`InvalidName` 42602 from the SETUP line while the statement under test raises
`UndefinedObject` 42704 — two different SQLSTATEs, one `raises`, one green test.

### Both directions are enforced, and they close different amounts

A `pytest.raises` over `Error`, `DatabaseError`, `Exception` or `BaseException` must
pin a SQLSTATE, and the block must hold exactly one top-level statement whatever the
class. Neither is a convention: both are an `ast` walk in
`pytest_collection_modifyitems`, so an offending file does not collect at all rather
than collecting and passing.

**The first rule closes `raises-too-broad`.** It moved to `VACUITY_MODES.md`
section 2.

**The second only MITIGATES `raises-catches-setup`, which stays in section 3.4.**
It counts TOP-LEVEL statements, so it removes the spelling where the setup sits on
the line above — and two shapes walk straight past it, each being one statement that
performs the setup inside the block:

- **a helper call.** `_setup_then_run(conn)` is one statement, and the setup runs
  inside the helper.
- **a compound statement.** A `for` over the setup and the statement under test is
  one statement holding two; an `if`, a `with` or a `try` nests the same way.

Both were measured reporting `1 passed`, exit 0 and **zero offences**, with the setup
raising and the statement under test never running. **Both are now refused**, and the
arms that recorded them as residuals assert the refusal instead. Counting statements
recursively would catch both and would also refuse a legitimate single-statement
loop; what would close the mode is a claim about WHICH statement raised, and
`VACUITY_MODES.md` section 5 carries it as the next entry.

### Why the scan parses instead of grepping

Every arm below writes the forbidden shape inside a `pytester.makepyfile` string,
because that is how the layer's own tests drive an inner run. A line regex fires on
those strings and refuses the file that proves the guard — the false positive the
broad-`except` scan already paid for once. Swept over `test/pytest/*.py`, the AST
finds **5** `pytest.raises` call sites and reports **0** offences, while a
`pytest.raises(` line regex matches **35** lines, **30** of them inside a string
literal or a comment. `test_the_raises_scan_reads_code_not_a_string_literal` pins
it.

### The rule's own parameters are not reachable from the corpus

The list of broad families is bound **inside** the scan, not at module level. Every
`conftest.py` under `test/pytest/` is imported before collection, so a module-level
tuple is writable from the tree the rule polices — `import pgc_vacuity` then
`pgc_vacuity.<the tuple> = ()` — after which the scan reports zero offences for ever
and the suite is green with the guard switched off and nothing saying so.
`test_a_conftest_cannot_switch_the_broad_family_list_off` writes three plausible
spellings of the name onto the module and requires the refusal to still arrive.
Rebinding the scan FUNCTION from a conftest is still possible; that is true of every
name in every Python plugin, and `test_guards_pinned.py` is what
notice a scan that stopped being called.

### The static half

**THE ARMS LIVE HERE AND NOWHERE ELSE.** An earlier version of this work carried a
shell mirror, `test/selftest/440-a-raises-must-name-a-sqlstate.sh`, which checked this
scan by grepping its source: 44 of its 55 checks were `grep -c` against the function's
text and it invoked `python3` zero times. @jdatcmd showed what that cannot do —
three faithful neuterings (`False and` prefixed, nothing renamed, every pinned
substring left in place) left the part at 55 passed while the scan went blind.

The mirror is gone, for two reasons that point the same way. A text pin cannot see a
disabled arm, so the proof has to RUN the scan; and the shell harness and this corpus
are **parallel in functionality without driving each other** — a shell part whose whole
subject is this file's source text is a dependency, not a parallel guard. So the
neutering proof is the two `test_disabling_*` arms above, which copy the layer, disable
one condition faithfully, and require the copy to go blind.

### The arms

| test | what it pins |
| --- | --- |
| `test_raises_requires_a_sqlstate` | the red test `VACUITY_MODES.md` section 5 names, byte for byte the shape that reported `1 passed` on main |
| `test_a_raises_that_pins_the_sqlstate_is_accepted` | the positive control that matters most: the honest form must still collect and pass |
| `test_a_raises_pinned_by_reading_the_field_is_accepted` | the second honest spelling, `exc.value.sqlstate` read directly, is a claim about a typed field too |
| `test_a_narrow_raises_needs_no_sqlstate` | scope control: a one-SQLSTATE class already names the error, so a second spelling would be noise |
| `test_a_raises_tuple_hides_a_broad_member` | @jdatcmd's #905 hole, closed before shipping: `(ValueError, psycopg.Error)` is still broad |
| `test_raises_exception_is_refused_like_a_broad_except` | `except Exception` was already uncollectable; `pytest.raises(Exception)` swallows the same failures |
| `test_setup_inside_a_raises_block_is_refused` | two statements in the block: narrow and pinned, and still unable to say which raised |
| `test_a_raises_block_with_one_statement_is_accepted` | the control for it, differing in exactly one property — the setup moved above the block |
| `test_the_raises_scan_reads_code_not_a_string_literal` | the false positive the scan is AST-based to avoid, pinned so it cannot return |
| `test_sqlstate_refuses_a_sqlstate_class_prefix` | `"42"` is a SQLSTATE CLASS — a prefix claim wearing the spelling of an exact one |
| `test_sqlstate_refuses_an_empty_expectation` | an empty `want` names no error, so nothing could have failed it |
| `test_sqlstate_refuses_an_object_carrying_no_sqlstate` | passing `exc` instead of `exc.value` would compare `None` against a real code for ever |
| `test_sqlstate_fails_when_the_failure_never_reached_the_server` | the measured case: `OperationalError` with `sqlstate` None is a `psycopg.Error` that is no server error |
| `test_sqlstate_fails_on_a_different_sqlstate` | the whole point: the setup raised 42602 and the statement under test raises 42704 |
| `test_sqlstate_accepts_the_exact_sqlstate` | positive control for the refusals above |
| `test_sqlstate_accepts_one_of_several_named_codes` | majors 15 through 19 can differ, so a tuple widens the claim by exactly the codes it names |
| `test_sqlstate_refuses_an_empty_set_of_codes` | and an empty tuple is satisfied by nothing, so the hatch is not the hole |
| `test_the_raises_scan_leaves_the_unrunnable_state_alone` | a documented hatch the corpus never exercises: `cannot_run` still prints `UNRUN`, counts it, and exits 67 with this scan loaded |
| `test_the_raises_scan_does_not_touch_a_recorder_made_in_the_body` | a test that fetches `expect` itself still satisfies the layer, because this scan runs at collection time |
| `test_a_helper_hiding_the_setup_is_refused` | a call to a function **defined in the same file** cannot say which statement raised |
| `test_a_compound_statement_hiding_the_setup_is_refused` | a `for` holding the setup and the statement under test is one top-level statement, and refused |
| `test_a_helper_hidden_in_an_assignment_is_refused_too` | the rule looks anywhere in the statement: `x = _helper()` hides the setup as well as a bare call |
| `test_every_compound_statement_is_refused_not_only_a_loop` | `if`, `while`, `with` and `try` nest the same way, so all nine compound kinds are refused |
| `test_a_raises_block_calling_an_imported_function_is_accepted` | the budget: four of the five blocks in this corpus call an imported function |
| `test_a_raises_block_calling_a_method_is_accepted` | the fifth block's shape, accepted, with the residual it leaves stated |
| `test_a_conftest_cannot_switch_the_broad_family_list_off` | the rule's own family list is not writable from the corpus it polices |
| `test_a_bare_sqlstate_expression_does_not_pin_anything` | `exc.value.sqlstate` as a statement of its own asserts nothing, so mentioning the field is not pinning it |

### How `raises-catches-setup` narrowed, and what is left

The count rule refuses a block holding more than one top-level statement. Two shapes
are **one** statement and still hide the setup inside the block, so the count saw
nothing:

```python
with pytest.raises(psycopg.errors.UndefinedObject) as exc:
    _setup_then_run(conn)                       # a helper call: one statement

with pytest.raises(psycopg.errors.UndefinedObject) as exc:
    for stmt in (setup_sql, sql_under_test):    # a compound: one statement
        conn.execute(stmt)                      # holding two
```

The fix is **not** a recursive count — that would also refuse a legitimate
single-statement loop. It is a claim about which statement raised, in two rules:

- **No compound statement.** All nine kinds Python has, looked up by name rather than
  written out so a missing `TryStar` or `Match` is not a NameError at import.
- **No call to a function defined in the same file**, anywhere in the statement — a
  helper hides as well in `x = _helper()` as in a bare call. A call to an **imported**
  function or to a **method** is the thing under test and stays allowed.

**The rule turns on where the function is defined, not on the statement being a call**,
and that is what makes the budget zero. Measured over the corpus: five
`pytest.raises` blocks, four calling `build_and_install` (imported) and one calling a
method, and the scan reports **no offence** on any of them.

### What is still reachable, measured

The mode stays in `VACUITY_MODES.md` section 3, and the refused count did not move,
because two ordinary spellings still reach it:

| shape | verdict |
| --- | --- |
| a `for` loop over two statements | refused |
| the same two as a **list comprehension** | allowed |
| the same two as a **tuple of calls** | allowed |
| a helper defined in **another file** | allowed |
| an honest one-statement helper defined in **this** file | refused — a false positive |

A comprehension and a tuple are **expressions**, not compound statements, so a rule
about statement kinds cannot see them. And `local_defs` is built from one file, so
moving the helper one file over defeats it. Neither is a contrivance; both are ordinary
Python. The last row is the rule's cost rather than a gap — an honest single-statement
local helper is refused, and the author must inline it.

A method that performs setup and then the statement is invisible for the same reason,
and no static rule can see inside it.

**The arm that should have caught the overclaim did not.**
`test_the_mode_this_layer_only_narrows_is_still_listed_as_open` required the mode to be
named in section 3 — and section 3 keeps a back-reference for every mode that *moves*
("`X` is now closed"), so the id is present in section 3 whichever state the document
claims. A first version of this work wrote the closure into section 3, added the row to
section 2, moved the count to 29, and that arm passed. It now also requires the mode to
be named outside a closure back-reference and to be absent from section 2; all three
shapes of the overclaim redden it. Residuals named by @jdatcmd on review.
| `test_a_sqlstate_assigned_and_never_read_does_not_pin_anything` | the same hole one step on: bound to a name nothing uses |
| `test_one_hop_through_a_local_name_is_an_honest_pin` | the cost side — `code = exc.value.sqlstate` then `expect.text(code, ...)` stays collectable |
| `test_the_keyword_form_is_checked_by_both_rules` | `pytest.raises(expected_exception=...)` is not an exemption from either rule |
| `test_the_keyword_form_with_a_pin_is_collectable` | and it is not refused merely for being the keyword form |
| `test_disabling_the_sqlstate_rule_makes_the_scan_blind` | the neutering proof: a copy of the layer with `False and` prefixed, nothing renamed, goes blind while still containing the pinned text |
| `test_disabling_the_statement_rule_makes_the_scan_blind` | the same for the second condition, so neither rule rests on the other's arm |
| `test_the_mode_this_layer_only_narrows_is_still_listed_as_open` | `raises-catches-setup` must stay in section 3 of the mode inventory |
## 21. test_failed_query_sentinel.py: a failed query is not a comparison

`error-swallowed-to-empty`: two queries raise, a helper turns each into the same
value, and they compare equal. The test is green and has asserted nothing about
either query.

`lib.sh` closed this by PRODUCING the sentinel with a sequence number per failure,
`res="QUERY_ERROR.$seq"`, so two failures can never compare equal. The port had the
constant `QUERY_ERROR = "QUERY_ERROR"`, a comment claiming it was "unique per
occurrence" — which is false of a constant — and a refusal in exactly one assertion.

Measured before this file existed, with a sentinel on both sides:

| assertion | before | after |
| --- | --- | --- |
| `expect.hash` | refused | refused |
| `expect.text` | **passed** | refused |
| `expect.rows` | **passed** | refused |
| `expect.row_set` | **passed** | refused |
| `expect.ordered_rows` | **passed** | refused |
| `expect.num`, `at_least`, `rowcount` | refused, by their type guards | unchanged |

So four of the five comparisons accepted two failed queries as agreement.

**The mechanism is the refusal; `query_error()` is the second line.** A non-unique
sentinel is safe against the layer, because no comparison accepts one at all. It is
not safe against a helper that compares by hand, which is why the producer exists and
why new code should use it. The SQL-side sentinel in `test_hilbert_locality.py`
cannot use it — it is produced by `coalesce(...)` inside the query — and does not need
to, for the same reason.

**The exclusion is derived from the signature too (#938).** Selection was the only
rule, so `wrote(cur, want, name)` sat outside because its first parameter is not
called `got`. That is the right answer for a cursor, and it would also have been the
answer for a comparison whose first parameter was `left`. Exclusion is now a
positive match on the kind of the left operand (`cur`, `result`, `plan`, `exc`,
`reason`), and `inputs == selected + excluded` fails when a method matches neither
rule. A declared list of method names is not the fix: a new method whose first
parameter is `cur` is excluded for the same reason `wrote` is.


| test | what it asserts | how it could fail |
| --- | --- | --- |
| `test_the_comparison_surface_is_what_this_file_thinks_it_is` | the derivation finds the layer's `(got, want)` assertions | a renamed or removed assertion makes the arm below vacuous |
| `test_the_shape_table_covers_every_comparison_the_layer_offers` | every derived comparison has a declared valid pair | an assertion added to the layer is silently outside the arm below |
| `test_every_public_assertion_is_selected_or_excluded` | every public Expect method is selected or excluded, with no residue | a method whose first parameter is not `got` lands in neither bucket |
| `test_wrote_is_excluded_because_its_left_operand_is_a_cursor` | `wrote` is out because the left operand is a cursor | a name-list would exclude it for being called `wrote` |
| `test_a_caller_supplied_value_not_named_got_fails_the_partition` | a value comparison named `left` is residue, not silently excluded | the hole #938 names: a future assertion not called `got` |
| `test_every_comparison_refuses_a_failed_query_on_either_side` | each comparison refuses a sentinel on the left and on the right | a comparison that compares instead of refusing; the arm distinguishes "refused" from "failed" |
| `test_row_set_refuses_before_it_maps_rather_than_after` | `row_set` refuses a sentinel that arrived as a cell | `row_set` reprs its rows before delegating, so a refusal only in `rows` cannot see it |
| `test_the_producer_is_unique_per_occurrence` | fifty calls are fifty distinct values, all carrying the prefix | a producer that returns a constant, which is what the comment used to claim |
| `test_the_constant_alone_is_not_unique_which_is_why_the_producer_exists` | the control: the bare constant equals itself | compared in plain Python, because the layer now refuses to compare two sentinels |
| `test_the_refusal_cannot_be_switched_off_from_the_corpus_it_polices` | sentinels minted WHILE ARMED are still refused after the global is rewritten | every conftest is imported before collection, so a module global is writable by the corpus the rule polices |
| `test_a_hardcoded_sentinel_survives_the_same_rewrite` | a sentinel no producer minted — the corpus writes three, one from inside SQL — is still refused after a rewrite | a matcher reading a rewritable global would stop seeing them |
| `test_the_ordering_premise_refuses_a_failed_reading` | `ordering_observable` refuses a failed reading on either side | with the old shared constant two failed readings were identical and it went RED; unique sentinels differ, so it passed and greenlit every ordered assertion resting on it |
| `test_a_legitimate_comparison_is_untouched` | equal text, rows, sets and numbers still pass | a refusal that also refuses real data is not a refusal |

**Each refusal is proved load-bearing.** Removing the one call from each assertion,
one at a time, with `__pycache__` cleared between runs:

| refusal removed from | the arm names | arms reddened |
| --- | --- | --- |
| `row_set` | `row_set COMPARED a failed query` | 2 (also the delegation arm) |
| `ordered_rows` | `ordered_rows COMPARED a failed query` | 1 |
| `rows` | `rows COMPARED a failed query` | 1 |
| `hash` | `hash COMPARED a failed query` | 1 |
| `text` | `text COMPARED a failed query` | 2 (also the hatch arm, which is phrased over `text`) |

The cache matters: the five deleted lines are byte-identical, so three of the five
mutations leave the file the same size and Python reuses the stale bytecode. Without
`rm -rf __pycache__` between runs, mutations 3, 4 and 5 report the same failure and
the table reads as though two refusals did not bite.

## 22. test_writes_wrote_rows.py: a write that wrote nothing

`INSERT ... SELECT ... WHERE false` writes no rows and raises nothing. The fixture
it was supposed to build does not exist, and every assertion below it then compares
two empty things. That is `insert-wrote-no-rows` in `VACUITY_MODES.md` section 3.5,
and before this guard nothing in the corpus read either the count or the command.

**The tag decides, not the row count.** `SELECT 0` and `INSERT 0 0` both carry
`rowcount == 0`, so a guard keyed on the count alone would refuse every test whose
last statement was a SELECT over an empty result — a legitimate and common
assertion. `statusmessage` is the server's own command tag, so this guard never
parses SQL. Measured on PG 18 against a pgcolumnar table:

```
statusmessage       rowcount   statement
CREATE TABLE              -1   CREATE TABLE t (i int) USING pgcolumnar
INSERT 0 5                 5   INSERT INTO t SELECT g FROM generate_series(1,5) g
INSERT 0 0                 0   INSERT ... WHERE false
UPDATE 0                   0   UPDATE t SET i = i WHERE i > 100
DELETE 0                   0   DELETE FROM t WHERE i > 100
SELECT 0                   0   SELECT * FROM t WHERE false
SET                       -1   SET search_path TO public
TRUNCATE TABLE            -1   TRUNCATE t
```

**A deliberate zero stays writable.** A DELETE that must match nothing is a real
negative control, and `expect.wrote(cur, 0, name)` is how a test says so: it
compares the count and marks the write as named. An unnamed zero fails the test.
The acknowledgement is not a waiver — a wrong count still fails.

**The refusal runs in the CALL phase, not a teardown.** #931 measured that a guard
run as a teardown fixture reports the test it guards as PASSED and fails
separately, so a reader sees a green test beside an error.

| test | asserts |
| --- | --- |
| `test_a_write_that_wrote_nothing_is_recorded` | an `INSERT 0 0` is recorded, with its count and its tag |
| `test_update_and_delete_are_writes_too` | `UPDATE 0` and `DELETE 0` are writes, not only INSERT |
| `test_a_select_matching_nothing_is_not_a_write` | `SELECT 0` is not a write; **the arm a count-only guard fails** |
| `test_ddl_is_not_a_write` | `CREATE TABLE`, `SET`, `TRUNCATE TABLE`, `DROP SCHEMA` are not writes |
| `test_a_write_that_wrote_rows_needs_no_acknowledgement` | a write that moved rows is recorded and needs no naming |
| `test_an_unacknowledged_zero_row_write_fails_the_test` | the inner run fails, and the message names the mode and the command |
| `test_expect_wrote_acknowledges_the_zero` | naming the zero lets a negative control pass |
| `test_expect_wrote_refuses_a_count_that_is_not_a_count` | `rowcount == -1` is refused rather than compared |
| `test_expect_wrote_still_compares` | acknowledging a count does not excuse a wrong one |
| `test_several_writes_and_only_the_empty_one_is_named` | with three writes and one empty, the refusal names the empty one |
| `test_wrote_refuses_a_statement_that_is_not_a_write` | `expect.wrote` on a `SELECT 0` is refused, not compared |
| `test_the_acknowledgement_names_one_write_and_not_its_twin` | naming one zero does not acknowledge a different identical zero |
| `test_acknowledging_both_identical_zeros_passes` | the control for that arm: naming both is legitimate |

### Two properties, two files, on purpose

Every arm above runs with **no database**. A stub cursor carrying the two measured
fields exercises the classifier and the refusal exactly, which is the whole of what
those arms claim.

It is not the whole of the guard. Whether the connection the tests actually use is
wrapped at all is a different claim, and no driver-free arm can make it:
`test_the_connection_the_tests_use_is_watched` in section 7 does, through a real
`INSERT ... WHERE false`, and through both `conn.execute` and a cursor the
connection handed out — because the corpus uses both, 24 sites and 42 sites, and a
proxy watching only the connection would leave most of the corpus unwatched.

Splitting them is not tidiness. #917's pytest twin tested the reconciler's body and
left the runner's CALL to it uncovered: removing the call kept the pytest half at
9 passed while the shell half went red by one. Proving a function and proving its
call site are two proofs, and the second is the one that goes missing.

### What the command tag cannot see

Measured by @jdatcmd on PG 15.18 and PG 17.10, twelve statement shapes each through
psycopg 3.3.5: `statusmessage` is never absent and its wording is byte-identical
across both majors, which is the premise this guard rests on.

Four shapes **write rows and report a tag that is not a write**, so this guard does
not see them: a data-modifying CTE and a `SELECT` of an inserting function both report
`SELECT`, a `DO` block reports `DO`, and a `CALL` reports `CALL`. **Zero occur in the
corpus** — the writes today are 15 `INSERT`, 14 `COPY`, 2 `DELETE` and 1 `UPDATE` — so
this is a residual to state rather than a gap to close. Writing an arm for a shape
nothing uses would be an instrument with nothing exercising it.

### The stamp is a call site too

The acknowledgement is carried on the cursor the caller holds. The first version
stamped the **raw** psycopg cursor, which cannot take a new attribute at all, so the
stamp was swallowed by its own `except` on every real write and `wrote()` fell back to
matching by `(tag, count)` — which made the absolute ordinal name the wrong statement
in exactly the case the ordinal was added for. The two fixes rest on each other.

Measured, against PostgreSQL through the real driver:

```
connection.cursor()        stampable=False   (Cursor: AttributeError)
conn.execute() return      stampable=False   (Cursor: AttributeError)
ServerCursor               stampable=False   (ServerCursor: AttributeError)
the arms' _Cur stub        stampable=True
```

**The driver-free arms could not see it**, and that is the lesson rather than the bug:
they proved the identity mechanism on an object that differs from the real one in
exactly the respect under test. The arm that catches it is cluster-bound, because a
real cursor is the only thing that can show it — which is the same sentence as the
wiring arm's, one level down. Found by @jdatcmd.

### Two holes found by attacking this guard, after it was green

Both were found by asking what the guard would accept rather than what it refuses,
and both are recorded because the first version shipped green with them.

**`expect.wrote` accepted a SELECT.** A query matching nothing reports `SELECT 0`
with `rowcount == 0`, so `expect.wrote(cur, 0, name)` compared 0 with 0 and passed --
asserting "this write wrote no rows" about a statement that is not a write. It reads
as a deliberate zero and pins nothing, which is this document's own subject appearing
inside the assertion written to close it. A non-write tag is now refused.

**The refusal numbered the wrong thing.** It enumerated the empty writes it was about
to print, so `#1` meant "the first one I am complaining about" and identified no
statement -- a reader counting writes in the source went to the wrong line. The
ordinal is now the write's position among ALL the test's writes.

That one also made an arm that could not discriminate. With two writes, both the old
and the new numbering print `#1`, so the arm passed either way; the arm now uses
three writes with the empty one second, where the old numbering says `#1` and the new
one says `#2`. An acknowledgement is also matched by the cursor that ran the
statement rather than by `(tag, count)`, because two writes can carry the same tag
and the same count -- one accidental, one deliberate -- and matching on the pair
marked the accidental one as named and reported the deliberate one instead.

### What made the arms themselves wrong twice

Recorded because both produced a green that meant nothing.

**An arm asserting only `failed=1` passed before the feature existed.**
`test_expect_wrote_still_compares` ran an inner test that called a function not yet
written, got an `AttributeError`, and reported a pass — satisfied by a failure that
had nothing to do with the comparison. Naming the numbers in the message is what
makes the red the right red.

**A multi-word pattern can straddle pytest's word wrap.** `expect.refusal` anchors
each pattern to one `E` line, and pytest wraps a long traceback line. Matching
`wrote no rows` failed against a message that contained it, which reads exactly
like "the guard did not fire". The refusal now leads with the mode's own
kebab-case id, which is one token and cannot be split, and each arm matches one
token per call.

## 23. test_mutation_ledger.py: which checks have ever been red

Nothing recorded whether a check had ever been red. That is the gap that let **39 checks
across 35 suites** ship unable to fail, three of them inside the suite whose whole
purpose is to stop exactly that.

It records that a named check **was observed red in a recorded run**. Not that it is
proven able to fail: that needs a named mutation applied deliberately, and conflating
the two would put a claim in the ledger nothing measured.

### The first design deadlocked, and the fix is the distinction

Bounding `checks_never_observed_red` means **every added check breaks the gate**, because
a new check enters as `never` — so the only way to land one was to raise a number the
design said may only fall. It shipped at 614 rows, 614 `never`, ceiling 614.

| number | kind | why |
| --- | --- | --- |
| `suites_not_covered` | **ceiling**, monotone | adding a check to a covered suite does not move it |
| `checks_never_observed_red` | **census**, asserted | every new check enters as `never`, so bounding it deadlocks |

What the gate refuses is a check the committed ledger has never seen, **in a suite the
ledger covers**. Regenerating the ledger is the intended fix and a reviewable diff.

The format is five tab-separated columns keyed on the first three:
`suite`, `part`, `check name`, `last observed red`, `mutations` — the last a `-` or a
`;`-separated **set**, accumulated rather than overwritten.

`run_all_versions.sh` invokes the gate before it removes the build directory, which is
the only place a matrix run can reach every suite's log.

### `test_bad_input_is_an_integrity_failure_not_a_clean_run`

A nonexistent log, an empty one and a record missing its verdict all returned **rc=0**.
An integrity failure that reads as a clean run is worse than no gate, because it
certifies. They now return 2, distinguishable from a real refusal at 1, and
`--registered` is required rather than silently skipped.

### `test_a_green_run_records_debt_and_never_a_red_observation`

A green run has observed nothing go red, so merging one must never record a red
observation — otherwise an ordinary CI run retires the debt the ledger exists to count.

### `test_the_mutation_column_accumulates_rather_than_overwriting`

Last-write-wins records the most recent attack rather than the catalogue the column
exists to become. One `--mutation` copied across several logs attributes a deliberate
change to failures it had nothing to do with, and is refused.

### `test_a_log_that_does_not_parse_is_not_evidence`

`read_records` accepted `len(f) >= 5`, so a record missing its reason, a verdict
outside `pgc_record`'s vocabulary, an empty check name, one record against
`checks run: 2`, and a log with no count at all all merged at rc=0. The ledger
absorbed as evidence a log that does not parse, which is how an observation gets
attributed to a check that never ran. Five refusals and a control, because five
arms all reporting rc=2 prove nothing if the tool has started refusing everything.
The BOGUS-verdict refusal names the verdict, so a field-count failure cannot satisfy
the arm.

### `test_last_red_may_only_move_forward`

The date was a plain assignment, so the answer depended on merge order: an older
log rewrote a recent observation, and an undated merge replaced a real date with
`unknown`. A free-form `--date` was stored verbatim, so a typo became an
observation date the ledger treated as authoritative.

### `test_a_mutation_names_one_check_not_every_casualty`

One deliberate change can redden the target and whatever depended on it.
Attributing `--mutation` to every failure records collateral damage as evidence
that the mutation kills that check. A run with more than one failing check is
refused with the count, and a single failure still carries the mutation on the
check that reddened.

### `test_a_reconciling_log_with_a_red_is_not_evidence_on_its_own`

`merge` already refuses a log that does not **reconcile**, and reconciliation is not
the property that matters: both logs that poisoned this ledger on the day it landed
reconciled. One was 827 records against `checks run: 827`, with fifteen checks red
because the tree had been copied without `.git`; the other was a single `FAIL` from
an unfinished change.

An environment red and a real regression are identical in the log, so the tool cannot
tell them apart and makes the caller say which it is: `--mutation NAME` for a
deliberate break, `--reds-are-real` for a genuine observation. Refusing reds outright
was rejected — a real CI red is the most valuable row the ledger holds and has no
mutation to name. An all-`PASS` log still merges with no flag, which is the control.
See #946.

### `test_two_runs_of_a_check_are_not_a_duplicate_of_it`

Merging logs first cannot tell *the same check in two runs* from *the same name twice in
one run*, and reported the first as the second.

### `test_renames_are_grouped_by_part_and_scanned_against_one_run`

A global positional pairing misses a real rename whenever unrelated movement in another
part shifts the ordering. Given a before-log and an after-log together the vanished name
is present in the union, so the scan **refuses** rather than silently finding nothing.

### `test_an_orphan_row_is_named_and_the_unscanned_rows_are_counted`

`rename-scan` pairs an appearance with a disappearance, so an **unpaired** disappearance
— a check deleted, or renamed in a run where nothing appeared — printed `vanished=N` and
refused nothing. Two rows in the committed ledger named checks that no longer existed;
the census counted both and every run returned 0.

The assertion that matters is the **scope**. A row in a part the run does not contain is
not an orphan, because the run cannot speak about it — counting those as present would
let a one-suite log certify the whole ledger. So the scan states how many rows it could
not speak about, and this test pins that number as well as the orphan it found.

### `test_a_part_that_skipped_is_unprunable_because_absence_is_not_removal`

The first version of `--prune` **deleted a suite**. One SKIP record put the part in the
run's `parts`, so every other row of that suite became an orphan, and the prune removed them
while reporting `not checked=0` and `rc=0` — the most confident output the tool can produce.
`not checked` protects a part the run does not contain; a part *contained but skipped
wholesale* fell in the gap between the two.

The rule is deliberately broader than that case: a SKIP **anywhere** in the part means some
arm did not run, so the run cannot tell a deleted check from one skipped under a name that
does not match it. One skipped timing check blocks pruning that whole part, which is the
direction a deleting command should err in.

The control is the half that matters — the same two rows must still be pruned when the
part's record is a `PASS`, or this is simply a tool that refuses to prune anything.

### `test_prune_drops_a_historyless_orphan_and_refuses_one_carrying_history`

The catalogue of what has been seen red is what the ledger exists to be, and no run can
recreate it. `--prune` therefore refuses the **whole** prune when any orphan carries
history, rather than removing the safe ones and leaving a partial job for whoever reads
the output. A historyless orphan is removed and named as it goes; the row in the part the
run never mentioned survives, which is the control that the scope holds under a write.

### `test_the_gate_refuses_a_new_check_only_in_a_suite_it_covers`

The suite restriction is the *meaning* of `suites_not_covered`, not a softening: without
it the gate refuses every check of every uncovered suite and reddens the whole matrix
on its first run. It tightens on its own as suites are seeded, and the deadlock that
shipped is pinned as its own arm — regenerating the ledger lets a new check through.

### `test_the_ceiling_may_only_fall_and_that_is_enforced`

The tracked file says the ceiling may only fall. Without a mechanism that is prose, and
raising the number passed. The gate compares against the previously committed value.

### `test_the_runner_invokes_the_gate_before_it_removes_the_logs`

A gate nothing runs is a comment. Nothing in the repository called this tool: zero
references in `.github/`, zero in the runner.

### `test_the_committed_ledger_and_budget_agree`

If they disagree, one was edited by hand. `suites_not_covered` is 250 of 251, so the
gate cannot refuse a new check in 250 suites — a real limit, counted rather than hidden,
which falls as suites are seeded.

### `test_the_gate_refuses_a_census_that_contradicts_its_own_ledger`

The arm above asserts the two committed files agree. This one asserts the **tool
refuses a pair that does not** — because the gate printed `ledger census: rows=N`
and never compared it to the budget's claim, returning 0 on a fifteen-row lie
(#952). Reporting is not enforcing.

It is decidable from the two inputs alone, with no prior and no `--against`, and
that is the point rather than an economy. The disagreement is created by a **merge**:
two PRs each re-derive the census from the same base, the ledger then takes both sets
of rows, and the budget keeps whichever side won the conflict. A check that needed the
prior could not speak about the commit that creates the problem. Three PRs in flight
at once set 769, 762 and 800 from a base of 756, and no two of them composed.

Refused in **both** directions, which is what separates it from a ceiling: a ceiling
refuses a rise, and bounding this number deadlocks, as `check_ledger_budget.txt`
argues. Absence of the field is reported rather than refused, because absence is not a
contradiction — and because every other gate fixture in both harnesses states only
`suites_not_covered`, so refusing there would redden about twenty arms testing
something else. What holds the committed budget to naming both numbers is the arm
above.

### `test_a_record_that_does_not_name_its_major_is_not_evidence`

A log can only be trusted about the major it says it came from (#1010). The record
carried suite, part, name, verdict and reason, so the major was whatever the caller
asserted -- the `--date not-a-date` failure one field over, and worse, because a major
decides WHICH CHECKS CAN EXIST. `analyze_differential.sh:61` emits one record on PG15-17
and a suite's worth on PG18+; `fk_referencing.sh:287` emits different check NAMES in its
two branches.

The six-field record must stop reconciling, or the field is optional and a log without
it keeps being merged on the caller's word. Four invalid majors are refused
(`eighteen`, `18.2`, `pg18`, empty) with a well-formed control, and `unknown` is
accepted -- `harness_selftest` never references `PGC_MAJOR`, so 907 committed rows have
no major to name even in principle.

### `test_the_census_reports_the_major_it_read`

Read and discarded is indistinguishable from not read at all. `census` is how a human
checks a log before merging it, so a major the tool validates and then drops cannot be
audited. Asserted on the FIELD rather than a substring: `18` appears inside a check name
or a reason just as happily.

### `test_a_rows_major_set_accumulates_rather_than_replacing`

A check exists on a SET of majors, and the set is a row's field rather than part of its key
(#1010). Keying on the major would store one fact once per major: measured on a full matrix
at `4d7c75ae`, 6367 of 6472 checks are identical on PG15 and PG18, so `(major, check)` would
hold 6472 x 5 = 32,360 rows to express 105 keys' worth of difference. Keeping the key at
`(suite, part, name)` is also what keeps `checks_never_observed_red` counting CHECKS, true to
its own name.

The set accumulates, for the reason the mutation column and last-red both do: merging a PG15
log after a PG18 log must not make the check stop existing on 18. `unknown` is a member of the
set like any number: `PGC_MAJOR` is set in `pgc_setup`, and 14 suites need no cluster so never
call it -- 544 of 6753 records on a full pg18 matrix.

### `test_a_run_speaks_only_for_the_majors_the_row_claims`

A PG15 run cannot orphan a check the ledger says exists only on PG18. This is the direction
the missing dimension actually broke; `gate` is not it, because a PG18-only check never
appears in a PG15 log and the gate stays correct by never being asked.

Until #1010 it was saved only by the SKIP rule, and that was luck:
`analyze_differential` emits a `check_skip` on PG15-17 so its part was unprunable, while
`fk_referencing:287` emits `check` in its older-major branch and has no SKIP at all. The
fixture therefore carries **no skip**, or the arm would prove the wrong mechanism. The scope
is an INTERSECTION, so a row claiming `15;18` is checked on both -- a stronger claim held to
both tests -- and the control deletes a check on its own major to show that a real
disappearance is still named.

### `test_the_gate_cannot_refuse_a_check_on_a_major_it_has_never_seen`

The same argument as `suites_not_covered`, one dimension over. The gate cannot refuse a new
check in a suite it has never seen, and a major it holds no rows for is the identical
problem: adding PG20 would make every check new at once and redden the whole run, which is a
gate somebody turns off. The control shows a new check IS refused on a covered major, so the
arm does not merely prove the gate refuses nothing.

## 24. test_loop_coverage_premise.py: a loop that never ran asserted nothing

**Why this file exists.** `assert-inside-a-loop-over-zero-rows` in VACUITY_MODES.md 3.5
is two shapes, and the layer already refused one of them without anyone recording that
it did.

**The half already refused.** When a loop's body holds the test's ONLY counted
assertions, a zero-trip loop leaves the count at 0 and `pytest_runtest_call` raises
`VacuityError`. Measured on a planted test rather than read off the hook:

```
only assertion inside a zero-trip loop   VacuityError: made no counted assertion
the same loop with one row               1 passed
```

**The half that was open.** When the test *also* asserts outside the loop, the count is
non-zero, the test passes, and the loop's assertions simply never ran. Nothing noticed.
That is the shape a query returning no rows produces, and the shape a glob matching
nothing produces.

**The population, measured before the arm was written:**

| shape | loops | state |
| --- | ---: | --- |
| non-empty by construction (literal, `range`, local literal) | 20 | cannot be zero-trip |
| derived, loop holds the only assertions | 0 | already refused |
| derived, **with** assertions outside the loop | 2 | at risk, now guarded |

Both at-risk loops already carried a premise, so this arm is **green on arrival**. That
is the point rather than a weakness: the property was true of the corpus and nothing was
holding it there, so what this catches is the third one. It is not insurance against an
imagined shape — it has a population of two and locks it in.

| test | asserts |
| --- | --- |
| `test_every_at_risk_loop_carries_a_coverage_premise` | the corpus itself: every derived loop carrying an assertion has a cardinality premise |
| `test_the_sweep_finds_the_loops_it_is_meant_to_police` | **premise**: the sweep classified loops, because one that parses nothing reports no offenders either |
| `test_a_loop_with_no_premise_is_caught` | **removal proof**: the real shape with the premise removed, with the clean control beside it |
| `test_a_bounded_loop_needs_no_premise` | a literal, a `range` and a local dict's `.items()` are all exempt, because demanding a premise there would be noise a reader edits away |
| `test_the_layer_already_refuses_a_loop_holding_every_assertion` | the measured half, so this file does not claim the whole mode — and that the sweep deliberately skips that shape rather than double-reporting it |

### Why the rule is looser than the property, and what is left

The honest requirement is *a premise bounding the cardinality of **this** iterable*. What
is enforced is *a counted assertion outside the loop that takes `len(...)` of something*.

The two differ, and the reason is dataflow. `test_harness_deps.py`'s loop iterates
`sorted(found)` while its premise bounds `len(files)` — `found` is built from `files` in
a preceding loop. A rule that demanded the names match would reject correct code, which
is how a guard gets switched off. **So the residual is a loop whose premise bounds the
wrong collection**, which a reviewer catches and a sweep does not. 3.5 names it.


## 25. test_join_runtime_filter.py: serial join runtime filter

Pytest twin of `test/native_join_runtime_filter.sh`. The two files are independent:
each builds its own fixtures and expected values. They share only the public
EXPLAIN names and the SQL answers.

### `test_join_runtime_filter_defaults_on`

SHOW is `on` with no SET. A serial inner Hash Join then shows the coordinator
without enabling the GUC in the session. The skip numbers live in the clustered
test below.

### `test_serial_join_runtime_filter`

Clustered integer keys. The coordinator wraps core Hash Join, the build tap
omits NULL, and nineteen of twenty groups are removed. LEFT, SEMI, ANTI, and
CROSS plans are refused. Answers match both filter-off and a heap twin.

### `test_scattered_join_runtime_bloom`

Scattered keys keep every group. Bloom must reject most non-matches on
`Runtime Filter Rows Rejected`. The hull cannot be the thing that avoids work.

### `test_cross_type_int4_int8_bloom`

int4 fact vs int8 dimension. Both sides hash. Interval stays off.

### `test_collation_mismatch_bloom_only`

Operator collation is not the fact attribute collation. Interval stays off.
Bloom may still reject.

### `test_runtime_filter_rebuilds_on_lateral_rescan`

A correlated LATERAL rebuilds the filter for each outer parameter.

### `test_saturated_build_disables_bloom`

A build side past the on-disk bloom cap disables Bloom rather than saturating.

### `test_three_table_join_order_unchanged`

Wrapping the columnar-outer hash join must not reorder the other inputs.

### `test_empty_build_runtime_filter`

An empty dimension still uses the coordinator and returns no join rows.

### `test_projection_outer_is_not_wrapped`

A covering projection scan stays an unwrapped Hash Join outer.

### `test_early_limit_matches_heap`

`ORDER BY ... LIMIT` still matches a heap control. The coordinator used to
crash on this shape when it drained the tap through `ExecProcNode`.

### `test_fact_qual_with_late_mat_off_matches_heap`

A non-key fact-table qual with late materialization off. The attach used to
force the two-pass path with only the join key decoded, so the qual dropped
every row. Heap is the oracle. Independent of the shell conjunction arm.

## 26. test_check_records.py: every counted assertion is a record

#937, first phase. The shell harness makes counting and recording the same call,
so no path can do either alone, and then reconciles the totals. **The pytest half
reaches the same property through Python instead of through the shell's format**,
which is what "parallel in functionality only" requires: nothing here reads,
sources or derives from `test/*.sh`.

It is reached more strongly, because Python can remove the possibility rather than
police it. The count is not a second variable kept in step with the records:

```python
@property
def count(self):
    return len(self._records)
```

Measured before this file existed: `_counted()` at 15 call sites, `self.count`
incremented by one line and read by one, and **zero** per-assertion records.

| test | what it pins |
|---|---|
| `test_each_counted_assertion_appends_exactly_one_record` | three assertions leave three records, from a premise of zero |
| `test_the_count_is_the_record_stream` | the count tracks the records at every step, not only at the end |
| `test_the_count_cannot_be_moved_without_a_record` | **the construction proof**: the count has no setter |
| `test_a_record_names_the_assertion_that_made_it` | the names, in order |
| `test_a_refused_assertion_leaves_no_record` | a `VacuityError` is not an outcome; the stream is not a log of attempts |
| `test_a_name_carrying_a_separator_survives_the_record` | a tab and a newline in a name round-trip byte-identical |

**The construction arm is the one that matters.** A test checking only that the
count agrees with the records would pass on an implementation keeping two numbers
that happen to be updated together — which is exactly the drift the shell side
needs a reconciliation to catch. Asserting the count cannot be written at all is
what makes the agreement structural.

**The separator arm looks redundant and is not.** There is no separator in an
object, so it cannot fail today. It exists because the moment somebody formats
these records into a line, the shell's tab-and-newline defect returns — measured
there as a record of four fields for a tabbed name and two lines for a newline —
and without this arm nothing would say so.

### Phase 2: the verdict is resolved where the outcome is known

| test | what it pins |
|---|---|
| `test_a_failed_assertion_records_fail_even_when_the_test_catches_it` | the refutation's arm |
| `test_the_failure_reason_is_the_assertions_own_message` | one message per failure, not two that can drift |
| `test_the_assertions_before_a_failure_keep_their_verdicts` | verdicts are per assertion, not per test |
| `test_a_delegated_assertion_records_fail_too` | a failure raised by pytest's own `assert_outcomes` |
| `test_a_refusal_still_leaves_no_record` | the boundary a refusal must stay outside of |
| `test_every_refusal_precedes_its_record` | the static half, so that boundary cannot drift |
| `test_every_recording_method_resolves_its_verdict` | all 15, derived from the module rather than listed |
| `test_a_recording_method_takes_exactly_one_record_per_call` | the invariant the resolution rests on, pinned after a mutation showed it was assumed |
| `test_wrapping_a_method_twice_changes_nothing` | what the drift arm cannot see, measured and deliberately not guarded |

**The first design was wrong and the corpus is what refuted it.** Resolving each
verdict from the exception in `pytest_runtest_call` assumed a raise ends the test.
Proving a guard refuses means *catching* the `AssertionError`, which five tests
here do. Measured before the fix:

```
count before/mid/after: 0 / 1 / 2
  record 0  'this comparison must fail'   verdict PASS   <- this one RAISED
  record 1  'and the test continues'      verdict PASS
1 passed
```

A genuinely failed assertion stayed `PASS`, in a passing test, with nothing
reaching the hook to correct it. The resolution now happens **inside** the
assertion call, before any `except` in the test body can see the error.

**It is a wrapper rather than a verdict passed at the call site** because
`outcomes` and `refusal` delegate to pytest's `assert_outcomes`, which raises a
message this layer never composes — there is no verdict for the call site to pass.

**A refusal marks nothing, with no special case** for `VacuityError` being an
`AssertionError` subclass: every `VacuityError` is raised *before* its record is
taken, so no record exists to mark. That is scanned rather than trusted.

### Phase 3: the session reconciles, and the check can fail

| test | what it pins |
|---|---|
| `test_the_session_totals_are_reconciled` | the positive control: a clean run reports and does not refuse |
| `test_the_total_separates_records_from_passes_and_from_tests` | records 5, passes 4, tests 2 — three distinct numbers |
| `test_the_two_values_are_not_aliases_of_one_list` | the attack that fails: an **in-place** removal is refused too |
| `test_a_record_lost_in_transport_is_refused` | **the arm this phase exists for** |
| `test_a_verdict_outside_the_closed_set_is_refused` | the schema half |
| `test_the_recorder_is_only_observed_once_and_both_sides_of_that_are_blind` | **the limit**, pinned in both directions |

**The obvious reconciliation here is vacuous by construction, and phase 1 made it
so on purpose.** `count` *is* `len(self._records)`, so checking one against the
other compares a value with its own definition. Partitioning the records into
PASS/FAIL/UNRUN and asserting the parts sum to the whole is the same trap wearing
a hat — the buckets are derived from the list being counted. #937 records that the
shell side shipped `inputs == sum(buckets)` **twice** and that both were caught
only by mutating them.

So the two quantities come by different routes:

```
held      len(recorder.records), read in the process that RAN the test
arrived   the list read back off the report AFTER it was built -- crossing the
          report boundary, and under -n a process boundary as well
```

`_UnrunnableCollector` is why the second route has to exist at all: a worker's
state is invisible to the controller, so the value travels on the report.
Measured on the pinned runner, `user_properties` survive that crossing intact.

**What it catches:** a record dropped or mangled between the report being built and
the report being read, and a verdict outside the closed set.

**What it does not:** anything that changes the recorder outside the single
instant it is read, in **either** direction — a record appended after, or removed
before. Both are invisible and the run passes. It also does not catch a record
that is present, transported, well-formed and **wrong**; that is phase 2's job.

The earlier version of this section named only the later half. The **earlier** half
is the more reachable one: a late append needs someone outside the layer, while an
early loss is what a bug inside the recorder would look like.

**The reason is not xdist**, which an earlier version also claimed. The `expect`
fixture's teardown pops the recorder, so nothing after `makereport` can read it in
a single process either. And it is not unfixable: keeping the final *count* in a
session-level map past teardown and reconciling at worker-side `sessionfinish`
would close it — unbuilt and unmeasured, so named rather than planned.

So it is a transport check rather than a completeness check, and both halves of the
gap are pinned by an arm instead of described by a sentence.

**The two values are not aliases**, which is the objection worth recording because
the attack on it fails: the transport arm drops a record with a slice, which copies,
so an in-place `value.pop()` was injected instead — still refused. That shows
separate storage **in a single process**, which the xdist run cannot, since
serialisation copies everything by definition.

**The arms inject the failure from a conftest**, because no in-tree code drops a
record — an arm that waits for a real defect to appear is not evidence the check
can fail. `tryfirst=True` on those hooks is load-bearing: a wrapper's post-`yield`
code runs in the reverse of call order, so `trylast` made the injector run *before*
the layer attached anything, the inner run passed, and the arm read exactly like a
reconciliation that does not fire.


## 27. test_skip_loop_arms.py: a skipped arm records under its own name

#994. A site skipped a whole block under **one** name that none of its arms had. When
the condition failed those arms produced no record at all, so a reader could not tell
which did not run and the ledger could not tell a skipped arm from a deleted one — a
skipped arm's row has no matching record, exactly as a removed check's would.

The convention that fixes it was already in the tree: skip under each arm's own name,
in a loop over those names. The loop then **duplicates** the names, so a rename in the
sibling branch desynchronises the two silently, and the skip records under a name
nothing emits — the failure the fix exists to remove, reintroduced by an edit nobody
thought was risky.

### Why this exists separately from `test/selftest/470`

`470` shipped in #998 with no pytest half, against the owner's rule that a test in one
harness is not finished. This is that half, and it is **not a port**.

| | measures | can it see |
| --- | --- | --- |
| `test/selftest/470` | the real corpus | a loop that drifted from its arms |
| `test_skip_loop_arms.py` | the instrument | a classifier that stopped being able to tell |

A classifier that filed every site as `armless` would report zero mismatches, and
`470`'s population premises would still pass on whatever loops remained. So this file
drives the same tool over **planted trees whose right answer is known** and asserts the
classification itself. Two measurements of one property: the shell says the corpus is
clean, this says the thing that reads the corpus can tell the difference.

It drives `.github/scripts/skip-loop-arms.py` by subprocess and never the shell
harness — a python tool rather than `test/lib.sh`, so this is a second measurement and
not the first one wearing a Python wrapper.

| test | what it pins |
| --- | --- |
| `test_a_loop_naming_its_siblings_arms_is_compared_and_agrees` | the clean site is **compared**, not quietly filed as armless or interpolated |
| `test_a_rename_in_the_sibling_branch_is_caught` | the whole point: a drifted name is a mismatch |
| `test_the_mismatch_names_the_loop_that_drifted_and_not_the_clean_one` | it names the **line it broke**, not merely that something is wrong |
| `test_an_armless_branch_is_counted_as_armless_and_not_compared` | a site nobody compared and a site that agreed are not the same number |
| `test_an_interpolated_sibling_is_reported_rather_than_compared_wrongly` | it declines to compare a site where a literal comparison would be **false in both directions** |
| `test_every_loop_is_classified_into_exactly_one_category` | `compared + armless + interpolated == loops`, so nothing fell out of the report |

### Removal proofs

Each mutation was asserted to apply before the run, because a clean pass reads
identically whether the code is load-bearing or the edit never landed.

| mutation | goes red |
| --- | --- |
| stop reporting mismatches | `..._rename_in_the_sibling_branch_is_caught`, `..._differ_by_exactly_the_rename` |
| compare the interpolated site literally | `..._interpolated_sibling_is_reported...`, `..._classified_into_exactly_one_category` |

The second is the one worth keeping: removing the tool's *refusal* to compare produces
a **false** mismatch on a correct site, and a guard that manufactures a red is the guard
people switch off.

| mutation | goes red |
| --- | --- |
| report a mismatch but name the **wrong line** | `..._mismatch_names_the_loop_that_drifted...` |
| return 2 from `main()`, counters still correct | **all six** |

That third one is why the control is shaped as it is. Its first version compared two
trees — clean reports nothing, drifted reports something — and **a classifier naming the
wrong line passes that**: `0/1` either way. Measured, on the mutant above. So the test
plants both loops in one file and pins `two.sh:12`, which is the only form that can tell
"it found my bug" from "it found something".

### The exit code is asserted in the helper, not in one test

Every test here reads the tool's stdout, and **none of them would notice the tool
becoming unusable**. Measured: returning `2` from `main()` while still printing correct
counters left all six green at 18 checks. The shell half catches that through
`|| _sk_out="TOOL FAILED"` — so the pair was stronger than this half alone, and on that
side it was a *premise* that failed while the headline arm stayed green.

So `_sweep` asserts `returncode == 0` once, covering all six. Under the same mutation
all six now fail. Reported by @OffgridwithJD, whose first probe of it was invalid and
said so: an `sys.exit(2)` appended after the `__main__` guard applied cleanly and changed
nothing, `rc` still 0. **Asserting that a mutation applied is not asserting that the
behaviour moved** — both halves, or the proof is of the edit rather than of the code.

### The partition is not the guard

`compared + armless + interpolated == loops` is the strongest line in the file and the
cheapest to satisfy wrongly: a classifier that filed **everything** as `armless` satisfies
it perfectly. It is load-bearing only because the per-bucket tests assert that a known
site lands in the right bucket; the identity then says nothing else escaped. Both halves
or neither.

## 28. test_docs_join_clustering.py: the runtime filter's layout precondition

#752's skip is measured. The how-to named the GUC and not the layout that
makes group skip a no-op. These two tests read the published pages. They
do not import the shell suite.

| test | asserts |
| --- | --- |
| `test_how_to_names_join_key_clustering_for_the_runtime_filter` | the star-schema how-to section names clustering on the join key |
| `test_best_practices_names_join_key_clustering_for_a_fact_table` | best-practices names that join key, not only timestamps |

The shell twin is two checks in `test/docs_style.sh`. Documentation may
identify the two files as counterparts. That is the only cross-reference.

Public seams: `docs/how-to.md` and `docs/best-practices.md`.
## 29. test_join_vector_agg.py: ungrouped fold over a unique-key join

Pytest twin of `test/native_join_vector_agg.sh`. The two files are independent:
each builds its own fixtures and expected values. They share only the public
EXPLAIN name `Columnar Vectorized Aggregates` and the SQL answers.

### `test_unique_join_keeps_the_ungrouped_fold`

A unique-key inner join is a filter of the fact table. With the ungrouped GUC
on, EXPLAIN shows `Columnar Vectorized Aggregates`. The GUC-off plan is core
Agg. The answer matches GUC-off and a heap twin. The dimension holds a subset
of the fact keys, so a fold that skipped membership would disagree with heap.

### `test_duplicate_dim_keys_refuse_the_join_fold`

Duplicate dimension keys would multiply fact rows. EXPLAIN has no vectorized
agg node. The answer still matches a heap twin of the same join.

### `test_left_join_refuses_the_join_fold`

A LEFT join is not a fact-table filter. The target list names a dimension
column so the planner cannot drop the join. EXPLAIN has no vectorized agg
node. The answer matches a heap twin, including unmatched fact rows.

### `test_extra_join_filter_refuses_the_fold`

A Join Filter besides the hash clause is not a membership test. EXPLAIN has
no vectorized agg node. The sum matches GUC-off and a heap twin of the same
join.

### `test_inequality_join_filter_refuses_the_fold`

A non-equi join clause is the same kind of extra Join Filter. EXPLAIN has no
vectorized agg node. The sum matches a heap twin.

## 30. test_differential.py: the heap oracle, all seven parts

The governing property of `test/differential.sh`, and the reason it is the largest suite in
the tree: load the same data into a heap table and a columnar one, and every query must
answer identically. Heap is the oracle, so this catches encode/decode, null-handling and
chunk-skipping bugs **generically** rather than one at a time.

This is the **whole suite** ported -- all seven parts, in the order the bash suite runs them:
the type matrix, the boundary conditions, the lightweight encodings, aggregates over nulls and
deletes, bloom equality skipping, a wide projection, and the covering `count(*)`.

Part 1, the type matrix, is twenty columns covering every type the suite exercises, 12,000
rows, a **different null modulus per column** so no two columns share a null pattern, and
small chunk-group and stripe limits so there is something to skip.

Names are the bash suite's character for character, which is what lets `compare_to_bash.py`
diff the two harnesses by property. A port that renames a check asserts the same thing and
reports a different one.

**Row lists, not hashes.** `lib.sh` compares `md5(string_agg(...))` because bash has no
structured result. Here the rows themselves are compared, so a failure prints what differs,
and the vacuity layer can see a both-sides-empty comparison -- which a hash cannot.

**Three of the bash arms cannot fail, and this port says so rather than reproducing them.**
Each was found by the vacuity layer refusing the comparison, then measured:

| arm | why it cannot fail | what the port asserts |
|---|---|---|
| `c_int eq` | probes `c_int = 600`; c_int is `g*7-100`, so 600 needs g=100, and `100%5=0` makes that row NULL. 0 rows, both sides `EMPTY` | keeps it with a stated reason, adds `c_int eq present` on 607 (g=101, untouched by any null modulus) |
| `c_ztext is null` | c_ztext is `CASE WHEN g%2=0 THEN '' ELSE 'z'||g END` -- never NULL. 0 rows IS NULL, 6000 `= ''` | the empty-string count, which a decoder confusing `''` with NULL would move |
| `c_f4`/`c_f8 sum/avg` | asserts exact equality of a float sum, which has no single answer | 1e-6 relative, stated, with a control that the bound is tight enough to have a direction |

The float case is worth the detail. Measured on **heap alone**, one table, three row orders,
`extra_float_digits = 3`:

    ORDER BY id        -0.27597385772197924
    ORDER BY id DESC   -0.2759738577219848
    ORDER BY c_f8      -0.2759738578545523

Three answers from one access method. So "columnar equals heap exactly" is false by
construction, and the bash arm passes because `pgc_set_hash` hashes the **text** rendering
and psql's default precision rounds the difference away at some magnitudes and not others.
That is a real tolerance, implicit and magnitude-dependent. Exact comparison is kept for
int, bigint, smallint and numeric, where a tolerance would hide the defect the arm exists to
find.

### `test_the_matrix_fixture_is_what_it_claims`

A differential suite over an empty table passes every comparison, and one whose data fits in
a single chunk group proves nothing about skipping. Row count, chunk groups and stripes are
asserted.

The fixture is **module-scoped** because 12,000 rows over twenty columns is too slow to
rebuild per assertion, and that costs `pgc_conn`'s write watch -- so these three arms are
what `watch_writes` would otherwise have done. The first version queried
`pgcolumnar.chunk_group`, which does not exist; a guessed catalog name is a premise arm that
errors instead of asserting.

### `test_the_whole_row_agrees_across_every_column`

Every column of every row in one comparison. The per-column arms localise a failure; this
one catches a bug that only appears when columns are read together -- a projection offset, a
shared null bitmap.

### `test_every_column_projects_counts_and_places_its_nulls`

Projection, non-null count, and the **positions** of the nulls, per column. Positions and not
just the count: a decoder that loses the null bitmap's alignment returns the right number of
nulls in the wrong rows.

### `test_min_and_max_agree_for_every_ordered_type`

min/max exercises the comparison operator the zone map also uses, so this and the range arms
are the same property from two directions. uuid and bytea order under btree but have no
min/max aggregate; their ordering is covered by the range predicates.

### `test_sum_and_avg_agree_for_every_numeric_type`

sum and avg read every non-null value, so they catch a decode error min/max cannot: min/max
touch two rows, these touch all of them. See the float tolerance above.

### `test_a_range_predicate_agrees_for_every_type_that_has_one`

Range predicates drive chunk-group skipping. A wrong zone map shows here and nowhere else:
the rows are present and correct, and the scan never looks at the group holding them.

### `test_an_equality_predicate_agrees_for_every_type_that_has_one`

Equality is what a bloom filter prunes on, and what a hash can get wrong for a type whose
equality is not byte equality -- jsonb and int[] are here for that.

### `test_an_ordered_projection_agrees_in_order`

`row_set` is order-blind deliberately, which is right for every arm above and wrong for
these: a query that asks for an order is the only kind that can be wrong about one. The
premise comes first, because an oracle that cannot tell forward from reverse would pass both
arms while asserting nothing.

### `test_a_compound_predicate_over_several_columns_agrees`

Four columns of four types in one WHERE, where a per-column arm cannot reach: the scan
combines their skip decisions, and a predicate right alone can be wrong in conjunction.


## 31. test_native_ownership.py: every maintenance function is owner-only

Port of `test/native_ownership.sh` (#432). The maintenance functions rewrite data,
reclaim space, or take strong locks, so they are owner-only like VACUUM and CLUSTER.

### Two things the bash suite cannot assert

**The SQLSTATE, not the message.** `native_ownership.sh` greps the output for
`must be owner`. `CLAUDE.md` states the rule that breaks: *"Assert SQLSTATE, not
error text: 42501 comes only from `aclcheck_error`."* The refusal is
`aclcheck_error(ACLCHECK_NOT_OWNER, OBJECT_TABLE, ...)` at
`src/columnar_vacuum.c:189` and `:205`. A text grep passes whatever code the server
attached, so the day a refusal is raised as 22023 the bash suite stays green and
every client switching on SQLSTATE breaks.

**It does not conflate refusal with login.** The bash suite runs each call through a
separate `psql` as a role that must be able to connect; if that role could not log
in, the grep finds nothing and the arm fails for a reason unrelated to ownership.
`SET ROLE` changes the effective user for permission checks without authenticating.

A third arm states the ordering the bash comment asserts in prose: the check fires
before the work, so a non-owner is refused for a projection that does not exist
rather than told it is missing.

### The premise arm, and what measuring it corrected

Every refusal arm carries `premise: alice reaches the table`. `pgc_conn` puts each
test in a private schema, and without USAGE on it the refusals would be about
something else.

**What that something else is turned out not to be what the docstring first said.**
Measured by removing the grant: alice gets `42P01 relation "n" does not exist`,
because an unqualified name resolves through `search_path` and an unusable schema is
skipped. So the arms FAIL rather than falsely pass, and the premise's value is that
it fails first and names reachability. The false-pass case is real but narrower: a
QUALIFIED reference into a schema without USAGE raises `42501 permission denied for
schema`, the ownership refusal's own SQLSTATE from a different check.

| test | what it pins |
| --- | --- |
| `test_non_owner_is_refused_with_42501` | nine arms, one per function, each 42501 and each carrying the reachability premise |
| `test_the_owner_is_allowed` | the control: a gate that refused everyone would satisfy all nine |
| `test_the_check_fires_before_the_work` | a non-owner is refused for a projection that does not exist |

### compare_to_bash.py cannot grade this pair

Both sides build their names at runtime -- bash as `non-owner refused: ${1%%(*}`,
pytest as an f-string -- so the tool reports `PORT IS INCOMPLETE` for a port that is
complete. Measured across the corpus: **81 of 253 suites** carry at least one
interpolated check name, 252 of 4345 names overall. The verdict is a false red for a
third of the suites, which bounds how much of #432's parity the tool can certify.
## Part 2: boundary conditions

Part 1 asks whether the two access methods agree about DATA. Part 2 asks whether they agree
at the SIZES where the format's structure changes. Each fixture is built per test rather than
shared, because a different geometry each time is the whole point -- a module fixture would
have to pick one.

### `test_an_empty_table_agrees_and_the_agreement_is_not_vacuous`

`empty scan` compares two empty results, which `pgc_set_hash` renders as `EMPTY` on both
sides. That is not nothing -- a scan that invented a row would break it -- but the vacuity
layer refuses it by default, so the reason is stated and a **positive control** is added: the
same query returns a row once one exists. `empty count` and `empty agg` are not vacuous,
because 0 and a row of NULLs are values.

### `test_a_single_row_agrees`

One row is the smallest geometry that stores anything: a stripe, a chunk group and a value
stream all of length one. A format that assumes a full vector anywhere breaks here.

### `test_the_chunk_group_boundary_is_exact_and_the_data_survives_it`

N-1, N, N+1 around a 100-row limit, for two limits. The GROUP COUNT is asserted as well as
the data, because the data can agree while the geometry is wrong: a writer that never closes
a group produces one group and the right rows, and only the count says so.

### `test_the_stripe_boundary_is_exact_and_the_data_survives_it`

The same question one level up, at 1000 -- the floor `set_options` enforces, so the smallest
legal stripe and the most boundaries per row. It is also below one 1024-value vector, which
#1017 measures as a compression cliff; that is a SIZE question and this is a CORRECTNESS one.

### `test_a_column_that_is_entirely_null_agrees`

A column with no values has no zone-map minimum or maximum, and a scan treating a missing
range as "matches nothing" loses every row of the TABLE rather than of the column. `minmax`
is the arm that sees it.

### `test_a_whole_chunk_group_that_is_null_agrees`

The case a column-wide NULL cannot reach: skip decisions are per group, so a null group
between two non-null ones is where a wrong "cannot match" prunes live rows. The range arm
straddles the boundary deliberately.

### `test_the_empty_string_stays_distinct_from_null`

A varlena column stores `''` as a zero-length value and NULL as a bit, so a decoder that
loses the bitmap returns `''` where NULL was. Both counts are asserted, not just the total,
because they move in opposite directions.

### `test_a_wide_row_of_sixty_one_columns_agrees`

Sixty-one columns, where a per-column offset error shows and a narrow table hides it. Its
premise arm counts columns **via `regclass`**, not `information_schema.columns` by name: the
unqualified form counts every table called `t_col` in every schema, including the module
fixture's, and reported 81.

## Parts 3 to 7

### `test_the_integer_encodings_round_trip`

Four shapes in one table, each the input a different encoding is chosen for: constant deltas
for delta-of-delta, four distinct values for a dictionary, one value for a constant column, and
a hash-spread bigint for none of them. One table rather than four, because the verdict is per
column and a writer applying one column's to another would pass a single-shape table.
`compression => 'none'` so the codec cannot compress the damage away.

### `test_the_float_and_timestamp_encodings_round_trip`

Gorilla on a random walk and delta-of-delta on a fixed interval. **This fixture is why `_pair`
generates once and copies**: measured on its own generator, 2000 rows, regenerated gives 2000
rows differing and copied gives 0. min/max rather than sum for the floats, because a float sum
has no single right answer -- part 1 measures three from heap alone by row order.

### `test_the_dictionary_encoding_round_trips_including_varlena`

Four values, six values, and an md5 per row in one table, so the per-column verdict is visible.
`GROUP BY` is the arm a whole-row comparison cannot replace: it reads the column through the
grouping path rather than the projection path.

### `test_an_uncompressed_table_still_round_trips`

Encoding is independent of the codec. Without this arm every encoding above is only read back
through a codec, and a bug the codec happens to mask would never show.

### `test_aggregates_agree_with_nulls_and_deletes_present`

A row group carrying a delete cannot be answered from the value stream, so the scan falls back
per group -- and a fallback that double-counts shows in `count(*)` while every other arm stays
green. The delete is asserted to have removed exactly 400 rows, because a DELETE that matched
nothing would leave the fast path untested.

### `test_bloom_equality_agrees_on_numeric_and_uuid_keys`

Hash-spread keys, so min/max cannot prune and only a bloom can. **The spread is asserted**, not
assumed: each chunk's key range must span at least 90,000 of the domain, because a fixture that
quietly became ordered would make every arm here pass on zone maps alone and say nothing about
blooms. The absent-value probe is derived from the data rather than guessed.

### `test_text_bloom_equality_agrees_including_a_mismatched_collation`

**The bash arm probes a value that is not there, and it is the subtlest unfalsifiable arm this
port found.** `tk` is `'k' || ((g*2654435761)%50000)` over 16,000 rows of a 50,000-wide domain,
so 32% of values appear and `tk = 'k100'` matches 0 rows -- measured. A bloom wrongly pushed
under a mismatched collation would skip the chunks holding the match and also return 0, so the
one defect the arm exists to detect produces the answer it expects. The port probes a present
value and adds the direction it must fail in.

### `test_a_selective_filter_with_a_wide_projection_agrees`

Four selectivities from one row to most. `wide nomatch` is the second arm in the file that
cannot fail alone, and gets the same treatment as `empty scan`: a stated reason and a positive
control at a value that is present.

### `test_a_covering_count_agrees_with_the_path_on_and_off`

An UPDATE appends the new row and masks the old, so the stored per-group count and the visible
count diverge -- which is the arithmetic the metadata path has to get right and a plain scan
gets right for free. Run both ways through `enable_vectorization`, which is what distinguishes
"the fast path is correct" from "the fast path was not taken". `SET` on the connection rather
than the bash suite's `ALTER DATABASE`, which exists because each psql there is a new session.
## 32. test_stats_privilege.py: stats is readable only by a caller who may read the table

Port of `test/stats_privilege.sh` (#560, ported for #432).

`stats()` is SECURITY DEFINER and does its own privilege check, because GRANTing
SELECT on `pgcolumnar.zone_map` to PUBLIC would publish per-column minimum, maximum
and sum for every columnar table.

**The trap the suite exists to catch.** Inside a SECURITY DEFINER function the
effective user is the function owner, so `pg_class_aclcheck(relid, GetUserId(), ...)`
checks the superuser who installed the extension and returns `ACLCHECK_OK` for every
relation. It looks like a correct check and refuses nobody.

### What the port asserts that the bash suite cannot

`stats_privilege.sh` decides the refusal with `grep -c 'permission denied for table'`.
`CLAUDE.md` names the rule: 42501 comes only from `aclcheck_error`, while a grep for
"permission denied" is also satisfied by other refusals. **`permission denied for
schema` matches it too**, which is not hypothetical — it is the confusion measured
while porting `native_ownership`. This port asserts 42501 **and** that the message
names the table, so neither half carries the arm alone.

**Real logins, not `SET ROLE`.** Unlike the ownership port, session-opening is a
property this suite tests, so `SET ROLE` would assert it away. Each role connects.

| test | what it pins |
| --- | --- |
| `test_the_premises_each_role_is_what_the_suite_assumes` | each premise run BY the role it is about |
| `test_who_may_read_the_stats` | owner, GRANTed reader and superuser all succeed |
| `test_a_role_with_no_privilege_is_refused` | the bar: 42501 **and** the table named |

### A helper that turned a driver detail into a product claim

psycopg3 returns the **first** statement's result for a multi-statement execute, so
`SET search_path ...; SELECT ...` hands back the SET's empty result. The first
version collapsed that into a 0 and the arm reported *"the OWNER cannot read its
stats"* — a product failure, from a driver behaviour. The helper now issues the SET
as its own execute, and every call site asserts the error is `None` rather than
folding it into a value.
## 33. test_docs_table_structure.py: a table must stay a table

`docs_style.sh` enforced seven rules over every user-facing page. Sentence length, the idiom
list, em and en dashes, prose double-hyphens, conflict markers, the nav entry, and every
`VERSION` citation. **All seven are about prose.** So a table that had stopped being a table
passed the gate whose purpose is keeping those pages readable (#1026).

The measured case. A note and a second table were spliced into the middle of
`configuration.md`'s `set_options` argument table. That left six of the nine arguments as a
headerless block, and `docs_style.sh` passed with 14 checks.

It was the second splice that day. The first gave the GUC table no blank lines, which made an
`awk RS=''` guard read two GUC rows as one record and pass on `main`. Both are the same fact:
a markdown table is a contiguous run of `|` lines, and a blank line is structural.

The rule is in `plain_language_check.py` beside the other four, because that file already
walks every page and reports the per-file counts the shell asserts. It tracks fences **by
line** rather than stripping them with a regex. The regex form loses line numbers, and a
report that cannot say where is one somebody has to re-derive.

| arm | what it holds |
| --- | --- |
| `test_a_well_formed_table_is_not_flagged` | THE CONTROL, first: a rule flagging every table would catch the defect and be switched off the same day |
| `test_rows_orphaned_by_a_splice_are_flagged_with_their_line` | the defect, and the line the orphaned block starts at |
| `test_a_pipe_inside_a_fenced_code_block_is_not_a_table` | a shell pipeline in a fence is not a table -- with the unfenced control, or the arm passes because nothing is ever flagged |
| `test_an_unclosed_fence_does_not_swallow_the_rest_of_the_file` | the case the regex form gets wrong; state-tracking under-reports, which is the safe direction |
| `test_the_documents_the_gate_checks_are_clean` | the false-positive budget as a standing arm rather than a number measured once |

**Measured before landing**, which is what a static guard in this tree owes. 0 across the
gate's own scope, and 5 elsewhere in the tree. All five are real: 3 in this file and 2 in a
design document, none of which the gate covers.

## 34. test_docs_stripe_floor.py: the stripe floor is below a vector

A vector is a fixed 1024 values (`COLUMNAR_NATIVE_VECTOR_LENGTH`). A row group
smaller than one never fills it, so the chunk-shared FSST symbol table is not built
and a text column is stored plain.

Measured on 200,000 rows, one text column, `compression = none`, against 12,800,000
raw bytes, two identical passes:

| `stripe_row_limit` | FSST tables | stored | of raw |
| --- | --- | --- | --- |
| 1000 | 0 | 13,625,000 | **106.4%** |
| 1200 | 166 of 167 | 6,998,031 | 54.7% |
| 2000 | 100 of 100 | 6,990,641 | 54.6% |

**The accepted minimum is 1000**, enforced in `set_options`, so the most aggressive
legal setting is the one that pays this — and at it the column costs more than
storing the bytes uncompressed. `docs/administration.md` tells a reader to *lower*
this setting for point-lookup-heavy tables, which is the path in, so the warning has
to sit in the block that gives the advice rather than in a reference table.

### Why one line, and why the section as well

Three signals were tried. Two were born green on `main`:

| signal | on `main` |
| --- | --- |
| blank-line block | **passes** — `configuration.md`'s GUC table has no blank lines, so `stripe_row_limit`'s row shares a block with `chunk_group_row_limit`'s "fixed 1024-value vectors" |
| three-line window | **passes** — those rows are adjacent |
| one line naming both | **0 on all three pages** |

One line is also a claim about the prose: the floor has to be stated in a sentence,
not inferred from two neighbouring tokens. That is why `best-practices.md` names the
setting and the number together.

**And one line alone says nothing about WHERE.** @OffgridwithJD moved the line out of
the advice block to the end of `administration.md` — **402 lines away** — and the arm
still passed while its name claimed the floor was stated "beside the advice to lower
the setting". Reproduced here before anything changed.

So the `administration.md` arm asserts the **section**: the floor and the lowering
advice must sit under one `## ` heading, both under `## Row-group sizing` today. A
heading is a declared boundary, which is exactly what the paragraph reader lacked —
blank lines are absent inside a markdown table and arbitrary in prose.

| test | what it pins |
| --- | --- |
| `test_configuration_states_the_floor_where_it_documents_the_setting` | the floor is on the setting's own line |
| `test_administration_states_it_in_the_section_that_says_to_lower_it` | it is in the **same section** as the advice that leads there |
| `test_best_practices_carries_the_floor_with_the_load_sizing_advice` | the load-sizing guidance states it too |

### Removal proof, three ways

| mutation | result |
| --- | --- |
| `main`'s three pages | all three arms red |
| the floor line moved 402 lines from the advice | the administration arm red, the other two green |
| the branch as it stands | all three green |

The second row is the one the page-wide version could not produce.

**The proof itself broke once and said so.** `git stash` on the three pages stopped
reverting them the moment the change was committed rather than staged, so the
"restore main's pages" step was restoring the branch's own pages and every arm passed.
Checking the files out from `origin/main` explicitly is what makes the row mean
anything.

The shell twin is three arms in `docs_style.sh`: `grep` for the two one-line pages and
an awk heading walker for `administration.md`. The two halves share no code.
