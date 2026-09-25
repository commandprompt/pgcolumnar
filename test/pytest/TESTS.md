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
- [35. test_projection_privilege.py: the projection read helpers are a privilege boundary](#35-test_projection_privilegepy-the-projection-read-helpers-are-a-privilege-boundary)
- [36. test_compare_to_bash.py: the parity tool reads the NAME](#36-test_compare_to_bashpy-the-parity-tool-reads-the-name)
- [37. test_iceberg_fdw.py: the Iceberg FDW's pruning surface](#37-test_iceberg_fdwpy-the-iceberg-fdws-pruning-surface)
- [38. test_objstore_endpoint_userinfo.py: userinfo in an object-store endpoint](#38-test_objstore_endpoint_userinfopy-userinfo-in-an-object-store-endpoint)
- [39. test_hilbert_cluster.py: the Hilbert clustering SQL surface](#39-test_hilbert_clusterpy-the-hilbert-clustering-sql-surface)
- [40. test_sorted_pathkeys.py: when a scan may claim its rows are ordered](#40-test_sorted_pathkeyspy-when-a-scan-may-claim-its-rows-are-ordered)
- [41. test_projections.py: a second copy of some columns, kept honest](#41-test_projectionspy-a-second-copy-of-some-columns-kept-honest)
- [42. test_compression_reaches_the_cascade.py: the codec setting decides encodings too](#42-test_compression_reaches_the_cascadepy-the-codec-setting-decides-encodings-too)
- [43. test_pgxn_metadata.py: the published distribution metadata, which nothing read](#43-test_pgxn_metadatapy-the-published-distribution-metadata-which-nothing-read)
- [44. test_native_chunk_length_bound.py: a truncated chunk length cannot fetch](#44-test_native_chunk_length_boundpy-a-truncated-chunk-length-cannot-fetch)
- [45. test_native_fetch_coalesce.py: index fetch I/O is not per-column](#45-test_native_fetch_coalescepy-index-fetch-io-is-not-per-column)
- [46. test_parallel_am_scan.py: a table-AM parallel scan must share work](#46-test_parallel_am_scanpy-a-table-am-parallel-scan-must-share-work)
- [47. test_index_fetch_penalty_crossover.py: the correlated range must not fetch](#47-test_index_fetch_penalty_crossoverpy-the-correlated-range-must-not-fetch)
- [48. test_parallel_scan_cost.py: a parallel custom scan must not divide I/O](#48-test_parallel_scan_costpy-a-parallel-custom-scan-must-not-divide-io)
- [49. test_residual_is_counted.py: a residual must be counted, not subtracted](#49-test_residual_is_countedpy-a-residual-must-be-counted-not-subtracted)
- [50. test_collation_pinned.py: comm's inputs must be sorted the same way](#50-test_collation_pinnedpy-comms-inputs-must-be-sorted-the-same-way)
- [51. test_projection_scan_cost.py: a covering projection is not priced at half](#51-test_projection_scan_costpy-a-covering-projection-is-not-priced-at-half)
- [52. test_record_names_its_major.py: a record must name its major](#52-test_record_names_its_majorpy-a-record-must-name-its-major)
- [53. test_analyze_reltuples.py: ANALYZE must estimate the row count, not zero](#53-test_analyze_reltuplespy-analyze-must-estimate-the-row-count-not-zero)
- [54. test_projection_update.py: UPDATE must fan the new row number out to projections](#54-test_projection_updatepy-update-must-fan-the-new-row-number-out-to-projections)
- [55. test_projection_drop_column.py: DROP COLUMN must not invalidate a projection](#55-test_projection_drop_columnpy-drop-column-must-not-invalidate-a-projection)
- [56. test_encode_post_codec.py: an encoding must be smaller after the codec](#56-test_encode_post_codecpy-an-encoding-must-be-smaller-after-the-codec)
- [57. test_validity_elision.py: a column with no nulls must store no validity bitmap](#57-test_validity_elisionpy-a-column-with-no-nulls-must-store-no-validity-bitmap)
- [58. test_native_index_fetch_stripe_cost.py: the fetch penalty reads the table's own row-group limit](#58-test_native_index_fetch_stripe_costpy-the-fetch-penalty-reads-the-tables-own-row-group-limit)
- [59. test_scan_decode_cost.py: a scan is priced for the columns it decodes and their width](#59-test_scan_decode_costpy-a-scan-is-priced-for-the-columns-it-decodes-and-their-width)
- [60. test_index_fetch_penalty_width.py: a wider prefix must give up on per-row fetches sooner](#60-test_index_fetch_penalty_widthpy-a-wider-prefix-must-give-up-on-per-row-fetches-sooner)
- [61. test_native_reclaim_cycles.py: repeated compaction must not fight its own free list](#61-test_native_reclaim_cyclespy-repeated-compaction-must-not-fight-its-own-free-list)
- [62. test_native_reclaim_frag.py: coalescing must merge, stay correct, and cost nothing](#62-test_native_reclaim_fragpy-coalescing-must-merge-stay-correct-and-cost-nothing)
- [63. test_native_vacuum_race.py: compaction must not drop a concurrent commit](#63-test_native_vacuum_racepy-compaction-must-not-drop-a-concurrent-commit)
- [64. test_native_delete_vector_index.py: reading the delete vector uses its index](#64-test_native_delete_vector_indexpy-reading-the-delete-vector-uses-its-index)
- [65. test_native_delete_visibility_paths.py: a deleted row is invisible on every path](#65-test_native_delete_visibility_pathspy-a-deleted-row-is-invisible-on-every-path)
- [66. test_temporal.py: a columnar table enforces temporal constraints like a heap](#66-test_temporalpy-a-columnar-table-enforces-temporal-constraints-like-a-heap)
- [67. test_native_parquet_dict_oob.py: a crafted dictionary index must not read out of bounds](#67-test_native_parquet_dict_oobpy-a-crafted-dictionary-index-must-not-read-out-of-bounds)
- [68. test_advisory_lock_class.py: no lock an insert takes may be reachable from SQL](#68-test_advisory_lock_classpy-no-lock-an-insert-takes-may-be-reachable-from-sql)
- [69. test_index_am_support.py: the index methods the page claims must work](#69-test_index_am_supportpy-the-index-methods-the-page-claims-must-work)
- [70. test_analyze_differential.py: our statistics must have the shape core writes](#70-test_analyze_differentialpy-our-statistics-must-have-the-shape-core-writes)
- [71. test_native_groupagg.py: the grouped vectorized aggregate must answer what core answers](#71-test_native_groupaggpy-the-grouped-vectorized-aggregate-must-answer-what-core-answers)
- [72. test_analyze_function.py: statistics collected by reading, not by sampling](#72-test_analyze_functionpy-statistics-collected-by-reading-not-by-sampling)
- [73. test_assertion_carries_its_measurement.py: a failure must say what it measured](#73-test_assertion_carries_its_measurementpy-a-failure-must-say-what-it-measured)
- [74. test_base_scan_io.py: a base scan is not priced from sibling projection pages](#74-test_base_scan_iopy-a-base-scan-is-not-priced-from-sibling-projection-pages)
- [75. test_range_pruning.py: a range prunes on overlap, containment, and under its own collation](#75-test_range_pruningpy-a-range-prunes-on-overlap-containment-and-under-its-own-collation)
- [76. test_docs_upgrade_chain.py: the documented upgrade chain must be the one that ships](#76-test_docs_upgrade_chainpy-the-documented-upgrade-chain-must-be-the-one-that-ships)
- [77. test_projection_parallel.py: a covering projection can be a parallel scan](#77-test_projection_parallelpy-a-covering-projection-can-be-a-parallel-scan)
- [78. test_ttl_expire.py: the one function that deletes rows, tested twice](#78-test_ttl_expirepy-the-one-function-that-deletes-rows-tested-twice)
- [79. test_projection_scan_io.py: a covering projection is not priced from the base table's pages](#79-test_projection_scan_iopy-a-covering-projection-is-not-priced-from-the-base-tables-pages)
- [80. test_catalog_plan_index.py: planning pays for these catalogs only when they hold something](#80-test_catalog_plan_indexpy-planning-pays-for-these-catalogs-only-when-they-hold-something)
- [81. test_catalog_delete_index.py: retiring a row group costs no more for a bigger database](#81-test_catalog_delete_indexpy-retiring-a-row-group-costs-no-more-for-a-bigger-database)
- [82. test_rewrite_storage_oid.py: a type rewrite keeps the storage row on the live table](#82-test_rewrite_storage_oidpy-a-type-rewrite-keeps-the-storage-row-on-the-live-table)

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
| `test_an_expected_unrunnable_test_leaves_the_run_green` | #1163. The same declaration the three arms above exit 67 for, listed for this major, exits 0, so the difference is the list and not the declaration |
| `test_an_unlisted_unrunnable_test_is_named_and_refused` | the control beside it: an empty list refuses the same run, and names the test rather than counting it |
| `test_a_listed_test_that_runs_makes_the_list_stale` | the other direction. A feature arriving is a fact the list must stop claiming, and nothing else would notice: the test simply starts passing |
| `test_a_listed_test_the_run_never_collected_is_not_a_stale_row` | and the premise that direction owes (#1204). `allowed - actual` reads as "listed, did not decline", which is only answerable for a test the run COLLECTED. A one-file run on PG 15-17 collected none of the others and exited 67 while its own file passed, telling the reader to edit a list that was correct |
| `test_the_list_is_read_for_the_running_major_only` | a row for 15 does not excuse a decline on 18, which is what a flat set would do |
| `test_an_unreadable_pg_config_fails_closed` | no major means no judgement, and no judgement means the old behaviour. The dangerous reading is the other one: unknown major treated as expect-anything |
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

### Before you write a `cannot_run`: it exits 67, and CI acts on that

A run containing one unrunnable check exits **67** — `EXIT_INCOMPLETE`, deliberately the
same number as `lib.sh`'s `PGC_EXIT_INCOMPLETE`, so a runner learns the code once. pytest
itself only uses 0-6, so it collides with nothing.

**The `pytest (cluster tests)` job runs pytest bare under `set -euo pipefail`.** So 67
fails the step, and the job goes red on a check that did exactly what it was supposed to
do. Measured on 2026-09-14: that leg exits 0 with **0 unrun**, so nothing in the cluster
half was producing one and nothing was absorbing it. The next legitimately-unrunnable
cluster arm is the first, and it turns the job red.

The guard half is not in the same position today, but the reasoning is the same.

So, in order:

1. **Try to remove the precondition.** `sorted_pathkeys`' `parallel_copy` arm needed
   `max_prepared_transactions` raised before the postmaster starts. That is a line in
   `pgc_cluster`'s `postgresql.conf`, so the arm runs and the question disappears. Prefer
   this whenever the precondition is something this harness controls.
2. **If it is not yours to control, weigh what refusing costs.** `cannot_run` records
   under the REASON CODE, not under a check name, so a ported arm that refuses emits
   NONE of the bash names it would have carried and the suite must be declared in
   `INCOMPLETE` (#1040 phase 0b). Refusing is not free even before CI sees it.
3. **Only then refuse**, and say in the PR that the cluster job's exit code changes.

The general shape, worth recognising away from here: a truthful "could not evaluate"
sharing one channel with "something is wrong", and a caller that cannot tell them apart.
`orphan-scan` has the same problem with its exit 1 (#1015).

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
| `test_the_inequality_scan_does_not_flag_honest_code` | eleven shapes it must not flag, five of them real int() calls from this corpus |
| `test_the_operator_list_is_what_ast_offers` | #1030, `_COMPARE_OPS` pinned against `ast`, so a new operator cannot narrow the rule |
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
| `test_every_test_file_has_a_NUMBERED_section_of_its_own` | a section written as an unnumbered `###` is invisible to every other arm: not in the numbering, not in the contents, and the file is still NAMED so the coverage arm is satisfied — `test_iceberg_fdw.py` shipped that way in #1057 |
| `test_a_section_table_names_only_the_tests_its_own_file_defines` | presence is not placement: the coverage arm matches with `t not in text` over the whole document, so it accepts any permutation of every table in it — measured on `main`, three sections were describing other files' tests, and two more arrived in #1164's review |
| `test_a_row_under_the_wrong_heading_is_named` | the removal proof and its control, plus the case that decides the rule's shape: a table carried by no heading at all, which is how a quarter of this document is written |
| `test_a_numbered_section_has_a_body_of_its_own` | the next one down: a heading with no body still NAMES its file, so a section inserted into the gap between another heading and its body leaves every existing arm green — `## 37. test_iceberg_fdw.py` sat directly above `## 38.` with the Iceberg body under the userinfo title |
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
| `test_a_killed_run_can_still_reach_its_teardown` | #1170. SIGTERM must reach the fixture's `finally` or a killed run leaks its cluster. Asserts the disposition in effect while a cluster is alive, which is the wiring; calling the helper directly would prove only that a two-line function raises |
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
| `test_the_membership_report_names_a_database_free_file_left_undeclared` | the hole itself, on a fixture, with the control beside it |
| `test_the_membership_report_names_a_declared_file_that_needs_a_cluster` | the other direction: a listed file that starts using a cluster fixture |
| `test_the_membership_report_names_a_declared_file_that_is_gone` | a rename is still caught, and as its own kind rather than as a cluster need |
| `test_the_gate_runs_the_membership_decision_rather_than_only_this_file` | selftest 350 runs the decision, and the command line it uses works |
| `test_ci_derives_the_file_list_rather_than_repeating_it` | the CI job asks this module for `NO_CLUSTER`, names no file literally, and states no count |
| `test_the_job_installs_no_database_driver` | the job asserts psycopg is absent rather than assuming it |
| `test_the_cluster_job_runs_the_other_half_and_derives_it` | the complement of `NO_CLUSTER` is RUN, derived not listed, and asserts the driver IS present |
| `test_both_pytest_jobs_assert_how_many_tests_they_collected` | both jobs pass `--pgc-expect-tests` from the tracked file, and each guards the read |
| `test_each_expected_count_is_stated_exactly_once` | `expected_tests.txt` states each key on one line — a keep-both merge duplicates it, `awk` then hands the flag a multi-line value, and the arm above could not see it because it builds a dict |
| `test_a_duplicated_count_is_caught_and_the_dict_form_is_not` | **removal proof**: the same fixture read both ways, so the dict form is shown keeping the LAST line while the line form names the duplicate |
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

**A keep-both merge of this file duplicates a key.** CI then says something unrecognisable.
`ci.yml` reads the value with `awk '$1=="guard_tests"{print $2}'`, which prints one line per
match. Two matches make `WANT` multi-line, `test -n "$WANT"` still passes, and the flag
refuses it:

    pytest: error: argument --pgc-expect-tests: invalid int value: '284\n280\n283'
    exit 4

So it **fails closed**, which is why these arms are about legibility rather than a hole. What
exit 4 does not say is that a line is duplicated. The reader has to work back from an int
parse error to a merge resolution.

**Not hypothetical.** Three PRs were open at once, each moving `guard_tests`, and resolving all
three keep-both produced exactly that. Keep-both is right for a changelog and wrong for a
key-value file, and nothing in the tree said so. The arm that read this file built
`nums[f[0]] = int(f[1])`, which is the same shape `read_ledger` had before #982 — one file over.

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

The format is six tab-separated columns keyed on the first three:
`suite`, `part`, `check name`, `majors`, `last observed red`, `mutations` — the last two
each a `-` or a `;`-separated **set**, accumulated rather than overwritten.

`majors` is the set of PostgreSQL majors the check has been observed on, added by #1010.
It is a FIELD and not part of the key: keying on it would store one fact once per major,
which on a measured full matrix meant 32,360 rows to express 105 keys' worth of
difference. This paragraph said **five** columns and omitted `majors` from the day that
column landed until #1048.

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

### `test_a_mutation_with_two_targets_can_be_recorded_when_both_are_named`

A mutation with two GENUINE targets is ordinary: reverting the
`enable_join_runtime_filter` boot value reddens two checks, and neither is collateral
because both read the default directly. The tool could not tell that from "one target
and one bystander" and resolved the ambiguity by recording NOTHING, so the column
could never hold the entry it most exists for -- the one saying which checks share a
cause. `--target` makes the caller assert the attribution, as `--reds-are-real` makes
them assert that a red is real. Six arms: both named permits it and records both; one
named of two is still refused and names the unclaimed check; a target that did not
fail is refused and names it; and `--target` without `--mutation` is refused.

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

### `test_a_log_must_be_able_to_say_which_tree_it_came_from`

`orphan-scan` reports a ledger row no record in its part matches, and a row whose
check was ADDED after the log was written produces exactly that signal. Nothing in a
RESULT record dates it against a tree, so a stale log and a genuinely deleted check
are indistinguishable. `test/lib.sh` already writes `-- source: <fingerprint>`;
`--expect-source` reads it and refuses a log from another tree. Opt-in, because a
hand caller may not know its own build. Nine arms, including the control that a log
FROM the named tree is still read with its orphan intact -- without which the refusal
arms prove only that the flag refuses everything -- and the case of a log naming no
fingerprint at all, which would otherwise satisfy "does not disagree".

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

### `test_the_ledger_refuses_two_rows_sharing_one_key`

The same class one level down, found while building the arm below. `read_ledger` did
`rows[(f[0], f[1], f[2])] = [...]`, so a duplicated key in the **tracked file** collapsed
silently and the **last line won**. Measured on a two-line fixture, both orders:

    never first, then 2026-09-01   survivor last_red='2026-09-01'
    2026-09-01 first, then never   survivor last_red='never'      <- the red is GONE

So line order decided whether a recorded red observation survived. A merge that keeps both
sides of a changed row turns `ever red` back into `never`. That is the corruption #918 and
#925 exist to prevent, arriving from the opposite direction to #982's.

**Nothing else could catch it, and bounding the census cannot.**
`check_ledger_budget.txt` says `checks_never_observed_red` is a CENSUS and must not become a
ceiling. Every new check enters as `never`, so bounding it deadlocks. The gate compares the
budget's number with the ledger's, and both come from the same dict, so they agree either
way. Measured: with the budget regenerated alongside, an erased red passes the gate at
**rc=0**.

It is refused as an **integrity failure** (rc=2) rather than a gate verdict, beside the other
inputs that do not parse. A ledger that cannot be trusted is not a gate result.

### `test_a_ledger_with_no_duplicate_key_still_loads`

The false-positive budget, and the premise the refusal needs. Two rows differing only in the
NAME are two checks and must load, which is the ordinary case for every part in the tree. The
arm also asserts `rows=3`, so the refusal cannot pass by eating a row.

### `test_the_gate_refuses_two_checks_sharing_one_ledger_key`

A row is keyed on `(suite, part, name)`, so two checks with the same name in one part
share a row. Nothing is mis-recorded while both pass. The hazard is exact: when one goes
red the row records `ever red` and its namesake inherits a red observation nothing
attacked. `checks_never_observed_red` then falls by one for a check nobody attacked,
which is the census #918 and #925 exist to make trustworthy.

**`merge` already printed this and returned 0**, which is how three of them sat in one
part of `selftest/400` for a day (#982). The instance was fixed by `c3b13aed`; this is
the mechanism.

**The gate could not see it at all, by construction.** `cmd_gate` builds its records as
`sorted({(s, p, n, m) for ...})`. A set collapses the duplicate before any arm can count
it. So the same canonicalisation that makes the rest of the gate correct made this one
class unreachable. The count now comes from the raw records, through `_by_run`.

Measured on main at `03c6c9c8`. A real `harness_selftest` run emits **934 RESULT records
over 934 distinct keys, 0 collisions**. The gate run against the COMMITTED ledger and
budget returns 0 with no shared-key line. A full PG 18 matrix says the same for the whole
corpus: 247 suites, RC=0, zero shared keys.

### `test_a_shared_key_is_refused_in_a_suite_the_ledger_does_not_cover`

**The refusal covers every suite, unlike the new-check refusal.** That one is restricted
for a reason. It cannot know which of an uncovered suite's checks are new. This one needs
no history. Two records, one key, one log is decidable from the log alone.

Why that matters rather than being a detail. The ledger covers **four suites of 253**. A
refusal restricted the same way would close the class in four places only. The next
collision would then arrive in one of the other 249 and sit there until that suite is
seeded. This arm is what stops the restriction being copied in by habit.

**Measured before widening it**, because a gate that reddens 250 unmeasured suites is a gate
somebody turns off. A full PG 18 matrix ran with the refusal armed for every suite:

    247 suites ran, RC=0, ALL VERSIONS PASSED, zero shared keys

Check names are static, so one major's matrix measures this class completely rather than
sampling it.

### `test_the_same_check_in_two_runs_is_not_a_shared_key`

The distinction the refusal must not lose. One check observed on two days is two records
for one key and is the normal case. It is how the ledger accumulates evidence at all.
Only a repeat inside ONE log is a collision, which is why the count goes through
`_by_run` rather than over the logs together. Written the other way it would reject
every multi-day merge.

### `test_a_clean_run_is_not_refused_for_a_shared_key`

The false-positive budget: two different names in one part pass, and the gate returns 0.

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

### `test_the_reconciliation_is_built_from_the_printed_total`

A **source-text pin**, and the only kind of check that can reach this property.

The arm below asserts that a truncated display is visible. It cannot assert that the
reconciliation is *computed from what was printed*, because wherever the display prints
every bucket `sum(emitted)` and `sum(dist.values())` are equal by construction — no
fixture reachable from outside `cmd_merge` separates them. Measured: a wording-preserving
swap to `sum(dist.values())` passed the whole file, 29 passed.

So the guarantee rested on a comment. @OffgridwithJD objected that **comments rot where
arms do not**, which is right, and this is the weaker check CONTEXT.md keeps for exactly
this case: *"a grep over source text is the weaker kind of check and is still worth
writing; premise it on the call site existing, or it approves a file that no longer has
one."*

It proves nothing about behaviour. It refuses to let the source drift back silently.

| | |
| --- | --- |
| control | 30 passed |
| wording-preserving swap to `sum(dist.values())` | **1 failed**, this arm |
| reconciliation line deleted (the premise) | **1 failed**, this arm |

### `test_the_merge_summary_distinguishes_a_minority_major_set_from_a_uniform_one`

`merge` printed a **union** over rows, and a union cannot represent a minority set. Merge
rows carrying `{18}` into a ledger whose rows carry `{15,16,17,18,19}` and the union does
not move, so the line was **byte-identical on a correct merge and an incorrect one**. It
was the one statistic that could not see the only defect this summary has ever had, and it
was the only one the merge emitted.

That defect occurred twice in three hours, to the same person, with a written note about it
in between:

| | rows written | against | caught by |
| --- | --- | --- | --- |
| #1041 | 12 at `18` | 934 at `15;16;17;18;19` | CI's `suites (PG 17)` leg |
| #1042 | 8 at `18` | 1209 at `15;16;17;18;19` | a manual `uniq -c`, locally |

Both times the merge printed `majors ... 15, 16, 17, 18, 19`. **The operator was not
ignoring the output; the output agreed with them.** A roll-up that cannot represent the
failure is worse than no summary, because it actively confirms the wrong answer.

It now prints the distribution, says `NOT UNIFORM` when there is more than one set, and
prints `rows N = sum of buckets printed N` beside it.

**The reconciliation counts what was PRINTED, not what was counted**, and the difference is
the whole value of it. `sum(dist.values())` equals `len(rows)` by construction — `dist`
consumes `rows.values()` exactly once — so a reconciliation built from it guards the one
step that cannot go wrong. Truncating the display loop drops a bucket, and the **minority**
one at that, while such a line still balances at `5 = 5`. Found by @OffgridwithJD reviewing
this change. An arm re-adds the printed counts from the output, which is the only way
something outside the tool can tell the two sources apart.

The arm holds the **discrimination**, not the wording: it merges the same two checks two
ways and requires the two summaries to differ. Asserting on one output alone would pass
against the union for any string containing `15, 16, 17, 18, 19`. The correct arm uses
**five logs**, not one log naming five majors — the same name twice in one log is a
duplicate sharing a row, which is a different thing and would make the control unfaithful.

Removal proof: restoring union semantics while **keeping** the new output shape reddens the
discrimination assertion, so what is load-bearing is the distribution and not the rewording.

Reporting only. Whether merge should **refuse** a non-uniform result is a live design
question and is deliberately not settled here (#1048).

### A merged row that covers fewer majors than the ledger (#1071)

That design question is now settled the other way round, and the answer is a WARNING
rather than a refusal.

`merge` writes a row covering only the majors it was handed. A contributor runs the
suite on ONE major, merges that log, and the row lands with `majors = 18`. The gate
considers a row only where its majors intersect the run's, so `suites (PG 18)` matches
it and is green while `suites (PG 17)` reads it as a check never seen and reddens —
naming the contributor's own checks `(on major 17)`, which reads as though their suite
is broken on 17 when it passes there.

Five authors in a row hit it, including the person who wrote the tool, on a PR that was
itself about ledger hygiene: #1039, #1063, #1065, #1068, #1070. The tool already had
what it needed — the distribution it prints for its summary is computed from the same
rows — so it could see the new row was anomalous and said nothing.

| test | what it holds |
| --- | --- |
| `test_merge_warns_when_it_writes_a_strict_subset_of_the_ledgers_majors` | a single-major merge into a five-major ledger warns exactly once, and the same checks merged from five logs do not warn at all |
| `test_the_warning_names_the_majors_the_gate_will_redden_on` | the warning names the missing majors, read out of the text rather than searched for loosely — `"15" in output` is also satisfied by the prevailing set printed beside it |
| `test_seeding_a_ledger_with_no_prevailing_set_is_not_warned` | an empty ledger has nothing to be a subset of, and a warning that fires when nothing is wrong stops being read |
| `test_a_row_carrying_a_major_the_ledger_has_never_seen_is_not_a_subset` | the predicate is strict subset, not inequality, so adding a new major is not warned about |
| `test_the_warning_is_not_a_refusal` | the merge still succeeds and the row is still written — seeding one major at a time is legitimate |
| `test_an_existing_rows_widening_is_not_reported_as_a_subset` | only rows the merge touched are candidates, or a partly-seeded ledger reprints its own history every time |

Held as a **discrimination**, like the arm above it: the same checks merged correctly
must not warn. An assertion on the bad output alone would pass against a tool that
warned unconditionally, which would train the warning out of being read.

The shell twin is `test/selftest/520-a-merged-row-must-cover-the-majors.sh`. Same
properties, own fixtures, and neither file names the other.

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
| `test_cannot_run_records_under_its_name_and_still_counts_as_unrun` | the record carries the bash check's name while the outcome stays UNRUN (#1131) |
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

### The standing arm graded a hand-written list, and nothing enforced it (#1046)

The arm below grades the pairs it is GIVEN. A pair that existed and was not given to it
was not graded, and nothing said so -- the arm passed, grading the ones it knew about,
and reported a clean verdict for a tree it had not fully looked at. **Absent-from-the-list
and no-gap-found produced the same green.**

Latent throughout: the declared set happened to equal the tree, so nothing was ever
silently ungraded. It would have gone live the moment a ninth pair landed undeclared,
which is what forgetting one line looks like.

`COMPLETE` and `INCOMPLETE` are now declared at module scope and asserted in both
directions, the shape `SHELL_REFERENCES` uses in `test_harness_deps.py`:

| | |
| --- | --- |
| a new COMPLETE pair omitted | reddens, with the stem named |
| a new INCOMPLETE pair omitted | reddens, with the stem named |
| a known gap, declared with its reason | does not redden |
| a declared stem whose pair has been deleted | reddens |

**It forbids one thing, and that was the decision rather than an oversight.** Today an
incomplete pair may land declaring nothing. Here it must carry a stem and a reason --
the escape hatch is attached rather than the case forbidden, but a porter who lands a
pair that does not reach zero now has to say so. The cost today is zero: 8 pairs exist,
8 are declared, `INCOMPLETE` starts empty, and the first person it costs is the next
porter, who is the person it is for.

A pair is `test_<stem>.py` beside `test/<stem>.sh`, **derived** rather than listed. The
24 pytest files with no matching suite are the harness's own guards and are correctly
not pairs; deriving the population keeps them out without a second exemption list.

**And hoisting the list made an existing guard fire.** The standing arm's loop now
iterates a module-level name rather than a literal, so
`test_loop_coverage_premise.py` demanded a cardinality premise -- an empty `COMPLETE`
would leave every arm unrun and the verdict comparison trivially equal. The premise is
`at_least(len(complete), 8)`, and the sweep caught its absence the moment the list moved.

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

## 35. test_projection_privilege.py: the projection read helpers are a privilege boundary

`read_projection()` and `reconstruct_via_projection()` opened a caller-supplied regclass
and returned its contents with no privilege check, and `CREATE FUNCTION` grants EXECUTE to
PUBLIC. `reconstruct` rebuilds NON-COVERED columns from the base by row number, so the
projection was never the bound on what leaked: one projection on any column exposed the
whole row (#562). A caller holding SELECT but restricted by an RLS policy received every
row (#563).

### Four refusals, four SQLSTATEs, and why the bash suite needed the message

`projection_privilege.sh` decided four things by matching error text, and two of them were
load-bearing rather than decorative. Its own comment says why:

> Both layers reject this role, so a bare "refused" stays true if the REVOKE is deleted and
> the C check catches it instead -- measured: with the REVOKE removed this suite still
> passed 14 of 14.

Both ACL layers raise 42501, so the code alone does not separate them. What separates them
is the fixture:

| the caller | the call | outcome |
| --- | --- | --- |
| no EXECUTE | a projection that does not exist, on a table it MAY read | `42501` -- the body never ran |
| EXECUTE | the same call | `42704` -- it ran and reached the lookup |
| EXECUTE, no SELECT | a real projection on a table it may NOT read | `42501` -- the base ACL |
| SELECT, under a policy | the same | `0A000` -- `ERRCODE_FEATURE_NOT_SUPPORTED` |

The refusal is attributed by what the code REACHED. That is a fact about execution, and no
rephrasing of either message can move it. Both harnesses now do this: the shell half reads
the SQLSTATE through `psql -v VERBOSITY=sqlstate`.

### Two orderings the source asserts and nothing tested

| arm | what breaks without it |
| --- | --- |
| `test_the_base_acl_is_checked_before_the_projection_is_looked_up` | a caller with no SELECT learns whether a named projection exists on a table it may not read |
| `test_the_acl_is_checked_before_rls_so_no_privilege_discloses_no_rls_state` | a caller with no privilege is told the table has RLS enabled, which ordinary SQL does not disclose |

### Every arm

| test | what it holds |
| --- | --- |
| `test_the_premises_the_fixture_is_what_the_suite_assumes` | each role opens its own session, the owner gets rows from both helpers, and reconstruct really does return the non-covered column |
| `test_a_role_with_only_schema_usage_is_refused` | layer one, the SQL grant, per function |
| `test_a_role_with_execute_but_no_select_is_refused` | layer two, the C check, reached only because EXECUTE was granted |
| `test_which_layer_refused_without_reading_the_message` | which of the two, decided by what the code reached rather than by wording |
| `test_the_base_acl_is_checked_before_the_projection_is_looked_up` | the first ordering |
| `test_a_role_with_select_still_reads` | THE CONTROL: a bar that refused everyone is not a fix |
| `test_a_policy_restricted_caller_is_refused` | RLS, as `0A000` rather than a phrase |
| `test_the_acl_is_checked_before_rls_so_no_privilege_discloses_no_rls_state` | the second ordering |

The second is a correction `src/columnar_vacuum.c` records being made in review. Neither
had a test in either harness.

### Removal proof

Five mutations, each asserted to apply at both call sites before the run:

| mutation | pytest | shell |
| --- | --- | --- |
| drop the base ACL check | 4 arms red | -- |
| drop the RLS refusal | 2 arms red | -- |
| RLS **before** the ACL check | the RLS-ordering arm alone | the RLS-ordering arm alone |
| ACL below the projection lookup but above its raise | no arm red, correctly: 42501 still wins | same |
| ACL below the not-found **raise** | both ordering arms | both, reporting `got [42704] want [42501]` |

The fourth row is the one that says the ordering arms measure an ordering rather than the
presence of a check. The fifth is the disclosure itself, printed.

## 36. test_compare_to_bash.py: the parity tool reads the NAME

`compare_to_bash.py` decides whether a port is one-for-one with its bash suite, which is
#432's definition of done. It was reading the wrong argument.

The python side was matched with `expect\.\w+\([^)]*?"([^"]+)"...`, and `[^)]*?` is lazy,
so it stopped at the FIRST quoted argument:

| call | name read |
| --- | --- |
| `expect.num(got, 1, NAME)` | `NAME` -- correct, which is why it looked right |
| `expect.sqlstate(err, "42501", NAME)` | `"42501"` |
| `expect.text(got, "none", NAME)` | `"none"` |

So every SQLSTATE assertion was read as the literal `42501`, reported as an "extra" the
bash suite lacks, while the real property was reported MISSING. #432's ports are exactly
the ones replacing a grep on a message with a SQLSTATE assertion, so **the tool went blind
in proportion to the work being done well.**

### Measured over the tree

| pair | missing before | after |
| --- | --- | --- |
| differential | 6 | 0 |
| hilbert_locality | 13 | 0 |
| native_ownership | 1 | 0 |
| native_projection | 0 | 0 |
| projection_privilege | 23 | 0 |
| stats_privilege | 9 | 0 |
| zonemap_boundaries | 9 | 0 |
| **total** | **61** | **0** |

Of the 61, 34 were never missing. The rest were real, and four of them are closed here:
`stats_privilege` had invented a name for a property the bash suite already named, and
`zonemap_boundaries` was missing its liveness premise outright. Neither was visible while
the tool was reporting the wrong string.

### What the parser reads

| shape | read as |
| --- | --- |
| the last argument | the name, for the 14 helpers that put it there |
| the last argument of `refusal`, `cannot_run`, `plan_marker`, `plan_node` | NOT the name -- see below |
| an f-string | a `{}` template, matched against bash interpolations reduced the same way |
| `"a" if cond else "b"` | both arms |
| `@pytest.mark.parametrize("func,name", ROWS)` | the `name` column, resolved through module constants |
| anything else | nothing -- absent is better than wrong |

`$1` is the commonest interpolation in a bash check name and the first version of the
template reducer missed every one of them, because its pattern required `[A-Za-z_]` after
the dollar.

### The name is not always the last argument (#1036)

The fix above replaced "the first quoted argument" with "the last argument", and that is
true of 14 of `Expect`'s 18 helpers. It is not a property of the helpers, only of most of
them, and the four exceptions were then read wrong in silence -- the last argument is a
real string in each case, so a wrong name looks exactly like a right one.

| call | what the last argument is | the name it records |
| --- | --- | --- |
| `refusal(result, name, *patterns)` | a message PATTERN | `name`, argument 1 |
| `cannot_run(reason, detail="")` | the DETAIL of one run | `reason`, argument 0 |
| `plan_marker(plan, key, name=None)` | a plan KEY | the `name=` keyword only |
| `plan_node(plan, ..., name=None)` | a field of the NODE | the `name=` keyword only |

`refusal` is the worst of the four: the name goes MISSING and a fragment of an error
message arrives as an EXTRA, so one call produces two false entries -- the same defect the
section above closes, one helper along.

`plan_marker` and `plan_node` contribute NOTHING when called without `name=`. The key is
not the name even then, only a fragment of one (`plan_marker` records
`name or f"plan carries {key!r}"`), and reporting no name states MISSING rather than
inventing one.

**Measured over the tree** at `73e8e3d`, with the table as the only variable (the count
is labelled with the tree because it moves as pairs are added):

| pair | extras before | after |
| --- | --- | --- |
| hilbert_locality | 3 | 2 |
| every other pair | unchanged | unchanged |
| **total** | **68** | **67** |

Two false extras went (`Columnar Projected Columns`, a `plan_marker` key; and `the two
partitions are not different ({})`, a `cannot_run` detail) and one appeared in their place:
`UNMET_PRECONDITION`, the reason code `cannot_run` actually records. No pair's verdict
moved, because `rc` is driven by MISSING and extras never moved it -- which is why nothing
caught this.

**`UNMET_PRECONDITION` is an extra only because the tool cannot see the bash side of it**,
and saying otherwise would be the same mistake one level down. `hilbert_locality.sh:574`
and three lines after it DO check that property:

    check_unrunnable "box $box: groups read, Z-order" UNMET_PRECONDITION ...

The bash extractor reads `check(_num|_ratio|_text|_timing)?`, and `check_unrunnable`
matches no branch of it. Widening that regex by that one alternative and changing nothing
else takes `hilbert_locality` from `rc=0 missing=0` to **`rc=1 missing=2`** -- `box $box:
groups read, Hilbert` and `box $box: groups read, Z-order` -- with every other pair
unchanged. The port emits ONE record named `UNMET_PRECONDITION` where bash emits four per
box, and two of them have no counterpart in the port at all.

That gap is NOT caused by the change above; the change is what made it visible, and it is
filed as #1040 rather than widened here, because widening the regex reddens a pair and is
a port's worth of work rather than a tool fix.

Derived from `test/lib.sh` rather than swept for, because three different sweeps gave
three different totals: **`lib.sh` defines 8 check helpers, the tool reads 5, and 3 are
invisible** -- `check_unrunnable`, `check_skip`, `check_ratio_needs_quiet_machine`.
Individual suites define four more of their own (`check_structure`,
`check_reconstruct`, `check_split_happened` in `parallel_copy.sh`, `check_float` in
`parquet_export_stats.sh`), invisible to the same regex.

**50 invisible invocations over `test/*.sh`**, reconciled between two agents and two
independent methods, which agree helper for helper: `check_unrunnable` 25, `check_skip` 23,
`check_ratio_needs_quiet_machine` 2.

**The population is half the number.** `test/*.sh` is the 265 top-level suites, which are
the only files the tool grades. Globbing `test/**/*.sh` instead adds the harness selftests
and gives **56**, the extra 6 all in `test/selftest/`, which `compare_to_bash.py` never
reads. Neither number is wrong; a number without its population is.

`test/selftest/` is out of scope for a second reason as well: it is the SHELL harness's
own self-test, and the two harnesses stay independent, so counting it into a claim about
what the pytest parity tool grades would cross that line even if the tool could read it.

**And `git grep` will not give you that population.** Git pathspecs are wildmatch without
`FNM_PATHNAME`, so `*` crosses `/` and the natural spelling is silently recursive:

    git grep -e check_unrunnable REV -- 'test/*.sh'           9 files, 4 under selftest/
    git grep -e check_unrunnable REV -- ':(glob)test/*.sh'    5 files, 0 under selftest/

Measuring the number at an older revision means reaching for `git grep`, where the
top-level spelling LOOKS right and is not. Use `:(glob)`.

Getting there took four sweeps that read 89, 64, 54 and 50, and the three wrong ones were
not method-sensitivity -- they were two defects, both worth knowing because any later
re-derivation meets them:

- **A `\bNAME\s` sweep counts each helper's own definition line.** `lib.sh:1231` is
  `check_unrunnable() {<TAB># check_unrunnable NAME REASON_CODE DETAIL` -- the trailing
  USAGE COMMENT repeats the name followed by a space, so the definition matches as though
  it were a call. Same shape at `lib.sh:1407`. Two more matches were ordinary prose. That
  is 89 (definitions included) and 54 (comments included).
- **A command-position match misses an invocation after `&&`.** `hilbert_curve.sh:321` is
  `[ -n "$_a" ] && check_unrunnable "$_a" "$2" "$3"`. Anchoring on `^` alone gives 24 for
  that helper rather than 25.

Strip trailing comments as well as whole-line ones, exclude definitions, and accept a call
after `;`, `&&` or `||`, and the number is reproducible.

`refusal` moved no pair either: it is used only by `test_raises_sqlstate.py` and
`test_guards_pinned.py`, neither of which has a bash twin. Its arm drives the real
extractor rather than a pair.

### A second coincidence, inside the clause that fixed the first

`-1` is a claim about the CALL SITE. The drift guard reads the SIGNATURE. They agree only
while no OPTIONAL parameter sits after the name, because an optional one can still be
passed positionally:

| written | read as |
| --- | --- |
| `expect.rows(got, want, "THE NAME", "the reason")` | `the reason` |
| `expect.plan_marker(plan, "key", "THE NAME")` | nothing at all |

Both were legal, both read wrong, and every guard here stayed green. The second is worse:
a DROPPED name reports the bash property MISSING, and MISSING is what drives `rc`.

Latent rather than live -- no call site in the tree passes a trailing optional
positionally -- but #1037 makes `allow_empty` a reason STRING, which is exactly the
argument somebody writes positionally next to a name.

**Closed in the signatures rather than patched in the reader.** `rows`, `row_set`,
`plan_marker` and `plan_node` now take everything after the name as keyword-only, so the
wrong call is a `TypeError` instead of a silently misread name:

    Expect.rows() takes 4 positional arguments but 5 were given

`test_no_later_argument_can_overtake_the_name` holds it, and it is a signature fact, which
is what this guard is already good at reading. `cannot_run` needs no change: its name is
argument 0 and nothing after it can overtake it.

**The table is a hand-written derived value, so it is pinned.** The tool is deliberately
standalone (`ast`, `re`, `sys`) and cannot import `Expect` to ask where each name sits.
`test_the_tools_table_agrees_with_the_signatures_it_describes` reads the real signatures
out of `pgc_vacuity.py`, recomputes every entry, and fails with the helper named when the
two disagree.

### The BASH side had the same blind spot, and it shipped that way (#1040)

Everything above is about the python side. The bash side read five of the eight check
helpers `lib.sh` defines:

    check  check_num  check_text  check_ratio  check_timing        READ
    check_unrunnable  check_skip  check_ratio_needs_quiet_machine  INVISIBLE

A property asserted through one of the three was never reported MISSING and could not
move `rc`, so **a pair could grade one-for-one on the strength of the grader's blind
spot.** `hilbert_locality` was exactly that: two of the four properties its unrunnable
branch records had no counterpart in the port, and #1041 closed them.

It never drifted out of date. `0cbf574` introduced the pattern, and `check_unrunnable`
already had 21 call sites that day.

All eight take the check NAME as `$1`, so one pattern serves them all. That is a
property of these helpers rather than of bash, which is why the drift guard re-reads it
from `lib.sh` instead of trusting it.

`check_ratio` is a prefix of `check_ratio_needs_quiet_machine`, and **the old pattern
shape could not read the longer one at all**: `check(?:_num|_ratio|_text|_timing)?\s+"`
matches `check_ratio`, wants whitespace, finds `_needs...`, backtracks to the empty
option, wants whitespace after `check`, and fails. Measured on a fixture holding both,
the old form reads `['short']` and the current one reads `['short', 'long']`.

**The entries are listed longest-first for readability, and that ordering is NOT what
makes it work.** Python's `re` backtracks across alternatives, so a pure reorder reads
both names identically -- measured, and the arm stays green under it. Said explicitly
because the list LOOKS as though its order is load-bearing, and the arm pins the pattern
shape rather than the order.

**Suite-local helpers are out of scope, asserted rather than assumed.** Four suites
define one of their own (`check_structure`, `check_reconstruct`, `check_split_happened`
in `parallel_copy.sh`, `check_float` in `parquet_export_stats.sh`) and none has a pytest
twin, so none is graded. An arm holds both halves, so the day one is ported the grader's
limit is stated rather than discovered.

The population is `check` or `check_<something>`, **not** `check[a-z_]*`: the loose form
also matches `checks_in` in `decode_interrupts.sh`, a counting utility that returns a
number and records nothing.

### Removal proof

| mutation | red |
| --- | --- |
| take the first string argument, as the regex did | the regression arm, the unreadable-name arm, and the whole-tree arm |
| drop the conditional-name case | its own arm, and the whole-tree arm |
| drop parametrize resolution | its own arm, and the whole-tree arm |
| read the name column by position instead of by its declared name | its own arm, and the whole-tree arm |
| delete the `_NAME_ARG` table entirely | all four #1036 arms |
| drop the `refusal` entry | its own arm, and the drift guard |
| `plan_marker` `None` -> `-1`, taking the key | its own arm, and the drift guard |
| `cannot_run` `0` -> `-1`, taking the detail | its own arm |
| a wrong entry for a helper no BEHAVIOURAL arm covers (`at_least`) | the drift guard, and the whole-tree arm -- the four behavioural arms stay green, which is the point of it |
| add a helper to `Expect` whose name is not last | the drift guard, naming it |
| revert any one of the four `*` keyword-only markers | `test_no_later_argument_can_overtake_the_name`, naming the helper |

`test_the_ported_suites_in_this_tree_are_graded_one_for_one` catches all four. It is the
arm that matters: a guard over invented sources proves the extractor reads python, not that
the tool grades THIS tree.

### Every arm

| test | what it holds |
| --- | --- |
| `test_the_name_is_the_last_argument_not_the_first_string` | the regression, over three helpers, one of which always worked |
| `test_a_name_bound_by_a_loop_over_a_literal_table_is_read` | a `for` over a literal table has its NAME column read, and `_py_names` returns it |
| `test_a_literal_column_survives_an_interpolated_neighbour` | the column is read cell by cell, so an f-string in another column does not drop five literal labels |
| `test_a_table_that_is_not_literal_contributes_nothing` | a module constant, a comprehension and a computed label are refused rather than guessed, with a control |
| `test_the_loop_reader_invents_nothing_in_this_corpus` | every name it returns appears verbatim in the file, and it is exactly three files |
| `test_a_parametrised_family_expands_to_the_names_bash_unrolls` | a `f"{col} range"` over a literal container becomes one concrete name per member, matchable but not counted as an assertion |
| `test_dropping_a_member_brings_the_divergence_back_named` | the property that makes expansion right and widening wrong: remove a column and the bash name it covered is reported BY NAME |
| `test_a_templated_pair_is_not_orphaned_by_the_expansion` | the additive constraint, run against `hilbert_locality`, `hilbert_cluster` and `native_ownership` -- no `differential` fixture catches it |
| `test_the_expansion_refuses_what_it_cannot_spell` | a module constant (`1e-6` renders `1e-06`), two distinct columns, a non-literal container -- with a control |
| `test_the_expansion_reads_only_the_name_argument` | through `_name_argument` and nothing else; the unrestricted form emits SQL as check names |
| `test_a_call_whose_name_is_not_a_literal_contributes_nothing` | absent beats wrong: a false green on a parity tool loses a property in both harnesses |
| `test_an_fstring_name_becomes_a_template` | a runtime-built name is compared by shape |
| `test_a_conditional_name_carries_both_of_its_arms` | `"a" if c else "b"` states two properties |
| `test_a_parametrized_name_is_resolved_from_the_decorator` | the idiom a repeated bash property should be ported to, with a no-`name` decorator as the control |
| `test_the_parametrize_reader_takes_the_column_called_name` | the declared column, not position |
| `test_the_two_harnesses_interpolations_land_on_one_template` | bash and python spell interpolation differently and must meet |
| `test_refusal_names_its_second_argument_not_its_last_pattern` | the name is in the middle; the last argument is a pattern |
| `test_refusal_with_no_pattern_is_not_the_arm_that_proves_it` | the control: that shape reads the same under either rule, so it proves nothing alone |
| `test_cannot_run_without_a_name_states_no_property` | a declaration with no name is not a property; the reason CODE is not a check name (#1131) |
| `test_cannot_run_carries_a_check_name_when_it_is_given_one` | and with `name=` it is harvested under the bash gate's name, the old positional shape untouched |
| `test_a_helper_whose_name_is_optional_takes_it_only_from_the_keyword` | `plan_marker` and `plan_node` carry no name positionally; absent beats a key |
| `test_the_tools_table_agrees_with_the_signatures_it_describes` | the drift guard: every entry re-derived from the real signatures |
| `test_no_later_argument_can_overtake_the_name` | nothing after the name may be passed positionally, so `-1` is true of every CALL and not just every signature |
| `test_the_extractor_reads_every_recorder_lib_sh_defines` | the BASH-side drift guard, derived from what a `lib.sh` function DOES rather than what it is named, and checking each name's ARGUMENT POSITION as well as membership |
| `test_a_lib_sh_wrapper_that_forwards_a_name_is_read` | `diff_query`, `diff_query_ordered`, `pgc_pass` and `pgc_fail` record through `check`/`pgc_record`, and the name they carry is the suite's own |
| `test_the_wrapper_whose_name_is_the_second_argument` | `pgc_skip`'s name is `$2`; reading `$1` extracts the CAPABILITY, which is a wrong name rather than an absent one |
| `test_the_derivation_finds_a_wrapper_planted_in_a_fixture` | the derivation on a fixture where the answer is known: four forwarding shapes found, and a function owning its own literal name rejected |
| `test_the_comment_stripper_keeps_a_parameter_expansion` | `${shape#*|}` and `$#` are not comments; `#` opens one only at a word boundary |
| `test_an_empty_helper_group_fabricates_names_rather_than_reading_none` | an empty alternation matches everywhere, so a position with no helper would invent `$PGC_DB` as a check name rather than read none |
| `test_a_suites_own_forwarding_wrapper_is_read` | a suite's own wrapper that forwards a bare positional has its names read from the CALL SITES |
| `test_a_composing_wrapper_is_left_alone` | a wrapper that COMPOSES its name already states a template; refusing it would break three COMPLETE pairs |
| `test_a_helper_whose_name_cannot_be_resolved_is_refused` | a helper taking a LIST of names in one argument is refused by name, not skipped |
| `test_the_refusal_names_exactly_the_suites_it_refuses` | the refused SET is pinned by name, not counted; both directions, so an entry cannot outlive its cause |
| `test_a_bare_interpolation_is_not_published_as_a_name` | a forwarding wrapper's `{}` is dropped: it names nothing and can match a wholly-interpolated port name |
| `test_the_grader_itself_refuses_the_suite_it_cannot_read` | `main` exits 2 and prints no verdict, with a readable suite as the control |
| `test_a_helper_reaching_only_the_primitive_is_found` | the closure is seeded from `pgc_record`, not the `check` family; four suites turn on it |
| `test_a_longer_helper_name_is_not_shadowed_by_a_shorter_one` | `check_ratio` must not eat `check_ratio_needs_quiet_machine` |
| `test_the_suite_local_helpers_are_known_and_excluded` | the four suite-local helpers, and that none of their suites is graded |
| `test_every_pair_in_the_tree_is_declared` | the declaration is asserted BOTH ways, so a new pair cannot be silently ungraded |
| `test_the_ported_suites_in_this_tree_are_graded_one_for_one` | the standing arm: every pair in the tree, graded |

## 37. test_iceberg_fdw.py: the Iceberg FDW's pruning surface

Ports `test/iceberg_fdw.sh`. 74 of its 76 check names, one for one; the two it cannot
carry are `pgc_skip`'s refusal names, which are structural and declared in
`INCOMPLETE` with their reason.

| test | asserts |
| --- | --- |
| `test_the_fdw_reads_the_whole_table` | the premise: five rows, and no predicate prunes no files |
| `test_a_partition_predicate_returns_iceberg_scans_rows` | the same-oracle read -- `iceberg_scan` cannot prune, so it cannot over-prune |
| `test_a_partition_predicate_prunes_the_other_file` | an identity partition drops the file that cannot match |
| `test_a_value_in_no_partition_prunes_everything` | a value in no partition prunes both files and returns nothing |
| `test_a_non_partition_column_prunes_by_file_metrics` | a non-partition column prunes on the manifest's min/max |
| `test_a_metrics_pruned_scan_still_returns_its_rows` | and the rows survive the pruning |
| `test_a_value_outside_every_files_metrics_prunes_all` | a value outside every file's range, against the oracle |
| `test_a_date_partition_is_not_over_pruned` | #660: a date cell the FDW cannot convert must be READ, never NULL-filled and pruned |
| `test_a_bucket_partition_keeps_only_the_matching_bucket` | `bucket[8]`: our murmur3 agrees with the one pyiceberg wrote |
| `test_a_bucket_pruned_equality_returns_its_row` | and the kept file holds the row |
| `test_a_range_predicate_cannot_bucket_prune` | the hash destroys order, so a range prunes by metrics only |
| `test_a_truncate_partition_prunes_by_range` | `truncate[100]` with metrics disabled, so the transform is the only mechanism |
| `test_a_truncate_pruned_scan_still_returns_its_rows` | and the rows survive |
| `test_the_bucket_table_reads_whole_and_matches_the_murmur3_oracle` | the bucket table's premise and its same-oracle read |
| `test_the_truncate_table_reads_whole_and_matches_the_oracle` | the truncate table's premise and its same-oracle read |
| `test_a_day_pruned_scan_still_returns_its_rows` | and the rows survive a `day()` pruning |
| `test_a_day_partition_on_a_date_prunes` | `day(dt)`, whose cell is Iceberg days from 1970 against PostgreSQL's from 2000 |
| `test_the_day_table_reads_whole_and_crosschecks_the_epoch` | the oracle read that catches a wrong epoch offset |
| `test_a_coarse_temporal_transform_prunes` | year/month/day/hour on timestamp, date and timestamptz |
| `test_a_coarse_temporal_transform_does_not_over_prune` | the boundary bucket must be READ: a coarse bucket spans a range |
| `test_the_two_temporal_tables_cover_the_same_cases` | the two literal tables have not drifted apart |
| `test_the_year_table_reads_whole_and_keeps_the_boundary_file` | the boundary case stated alone |
| `test_a_year_equality_prunes_to_one_file` | an equality narrows to the constant's own bucket |
| `test_a_year_on_date_equality_prunes_to_one_file` | the same on a date column |
| `test_an_unknown_table_option_is_refused` | the validator, by SQLSTATE `HV00D` rather than by message text |
| `test_a_plan_with_no_pruning_marker_is_not_read_as_zero` | a plan that never mentions `Files Pruned` is not read as 0; needs no server |



## 38. test_objstore_endpoint_userinfo.py: userinfo in an object-store endpoint

Not a port and not a pair: `objstore_endpoint_userinfo.sh` does not exist. These assert
the same properties as `test/objstore_userinfo.sh`'s endpoint arms, independently,
through the python harness.

| test | asserts |
| --- | --- |
| `test_a_userinfo_endpoint_is_refused` | both shapes refuse at `22023`, naming userinfo and naming the ENDPOINT rather than the s3:// URL |
| `test_the_guard_fires_without_a_region_configured` | the placement: with no region set the refusal is userinfo, not the region demand |
| `test_a_clean_endpoint_is_not_refused_as_userinfo` | the control -- a clean endpoint gets past the guard and fails for another reason |
| `test_an_at_sign_in_the_object_key_is_not_userinfo` | the other direction: `@` is legal in a key and is untouched |

## 39. test_hilbert_cluster.py: the Hilbert clustering SQL surface

The port of `test/hilbert_cluster.sh` (#432, #889's SQL half). The bash suite pins the SQL
surface of `pgcolumnar.cluster_hilbert` and `recluster_hilbert`, the recorded
`sorted_kind`, the self-gate and the daemon. `test/hilbert_curve.sh` pins the CURVE itself
in C and neither file re-tests the other; this port keeps that division.

**45 collected tests, 184 checks, over the bash suite's eight arms.** Graded one-for-one:
`compare_to_bash.py` reports **0 MISSING** both as the tool ships (124 bash checks) and
with #1044's widened extractor (133), the difference being the suite's nine
`check_unrunnable` sites, all of which are twinned.

### Where the port asserts something the original gets for free

Two of them, and both are the same class: **wherever a port replaces a STRUCTURAL
guarantee with a PROCEDURAL one, it owes an arm the original does not need** (@jdatcmd).

The bash suite hands the daemon's naptime and thresholds to the server through
`PGC_EXTRA_CONF`, so they are in `postgresql.conf` before the postmaster starts and the
suite cannot run without them. `pgc_cluster.py` has no such hook, so the port sets them
with `ALTER SYSTEM` and a reload -- available because all three are `PGC_SIGHUP`. That can
silently not take effect, and then every S7 arm still passes: at default naptime the daemon
still acts and the poll still sees the tail fold. So the three values are read back from
the server. The arm failed on its first run with `got '2s/0.2/0.05'` -- `SHOW` returns the
unit -- which is the cheapest demonstration that it reads the server rather than restating
the `ALTER SYSTEM` above it.

The same for `max_parallel_workers_per_gather = 0`: the fixture SET it and nothing read it
back until the parity tool reported the bash suite's premise as MISSING.

### A reload is not a read

`ALTER SYSTEM SET pgcolumnar.autovacuum = on`, `pg_reload_conf()`, then `SHOW` on the same
connection returned **`off`**. A reload signals the postmaster and a backend already open
absorbs it at its next command boundary; this module runs every statement through ONE
connection, by design. The bash suite never meets it because every `q` is a fresh `psql`.
`_show_fresh()` opens a new session for post-reload reads -- the port of what the original
gets for free, not a workaround, and inherited by any later port that changes
postmaster-level state.

### Three refusals that the port must express differently

`expect.cannot_run()` records under the REASON code, so two refusals in one test collapse
onto a single `UNMET_PRECONDITION` record. Every conditional `check_unrunnable` in the bash
suite is therefore its own test here: S3's two curve comparisons, S4's three
layout-after-a-gated-call arms, and S7's two daemon-layout arms.

### What buys the curve, and what does not

Four arms refuse a relabelled Z-order implementation, and no others: S3's two
`three different physical orders` arms, S4(d)'s `is NOT the ZORDER rewrite`, and S7's
`is NOT the zorder layout`. **S5 is green on a relabelled implementation by construction** --
over one column the Hilbert index and the Morton index are both the identity -- so it buys
the surface and the recorded kind and must never be read as evidence of Hilbertness.

### Every arm

| test | what it holds |
| --- | --- |
| `test_the_two_digests_are_the_instruments_this_file_thinks_they_are` | the ordered oracle is order-sensitive and the set oracle is not; invisible to the parity tool because the bash names live in `lib.sh` |
| `test_parallelism_is_off_so_a_scan_order_is_a_fact_about_the_layout` | a digest is about the layout and not about scheduling |
| `test_the_refusal_fixture_and_its_roles_are_this_runs` | the fixture holds rows, the role owns nothing, can open a session, and holds EXECUTE and schema USAGE -- so a 42501 can only be the owner check |
| `test_each_new_verb_refuses_what_its_sibling_refuses` | four inputs x two verb pairs, each new verb's SQLSTATE compared against the established verb's on the identical input |
| `test_the_probe_can_report_both_success_and_an_unreachable_session` | the probe can say noerror, and can say it never reached the server |
| `test_the_reorder_fixture_is_measurable_before_anything_moves` | 20 groups, the set matches the heap mirror, the order digest is stable, the plan is the columnar scan |
| `test_cluster_hilbert_reorders_without_changing_the_row_set` | same rows, different order -- neither half stands alone |
| `test_physlayout_is_blind_to_an_eager_rewrite` | the instrument's limit, pinned on plain `cluster()` so it cannot be perturbed by #889 |
| `test_the_three_fixtures_and_the_owner_role_are_measurable` | three fixtures from one generator, and the catalog really is closed to the owner |
| `test_each_verb_moved_its_own_table_from_its_own_baseline` | without this, "different from each other" is satisfied by insert order |
| `test_the_kind_is_recorded_and_the_owner_can_read_it` | both routes: the superuser catalog and the owner's reporter |
| `test_the_owner_alone_can_tell_the_three_kinds_apart` | asserted as a NAMED SET, so no NULL can stand in for one |
| `test_the_three_kinds_stand_for_three_physical_orders` | the eager verb's curve defence, UNRUN unless both legs moved |
| `test_the_gate_is_closed_on_an_already_hilbert_table` | (a) 0 groups, kind untouched |
| `test_the_gated_call_left_the_layout_byte_identical` | UNRUN unless the return really was 0 |
| `test_the_same_call_does_work_once_a_tail_is_appended` | the positive control: the fifth direction, and what the daemon depends on |
| `test_the_gate_closes_again_on_the_refolded_table` | (a3) |
| `test_the_refolded_layout_is_byte_identical_again` | (a3), gated the same way |
| `test_the_gate_opens_for_a_zorder_table_over_the_same_columns` | (b), with the label asserted WITH the bytes |
| `test_plain_recluster_is_a_noop_on_a_hilbert_table` | (c) THE RULING: the curve is sticky |
| `test_the_noop_recluster_left_the_layout_byte_identical` | (c), gated |
| `test_a_different_key_rewrites_in_both_directions` | (d) the gate must DISCRIMINATE |
| `test_the_online_hilbert_rewrite_is_not_the_zorder_rewrite` | the online verb's only curve defence, gated on both rewrites |
| `test_over_one_column_both_curves_are_the_identity` | S5: surface and identity, never the curve |
| `test_vacuum_sorted_leaves_a_hilbert_table_alone` | S6, the reading of the ruling this suite pins |
| `test_vacuum_sorted_still_works_on_a_table_with_no_recorded_kind` | the removal proof: it must not no-op on everything |
| `test_the_daemon_fixtures_are_built_with_the_daemon_off` | the launcher is up, the daemon is off, and the thresholds took |
| `test_the_two_hand_driven_references_are_built_and_folded` | both references folded, and av_hi does not yet match |
| `test_the_two_references_are_two_different_layouts` | UNRUN unless both folded |
| `test_the_daemon_reclustered_the_decayed_hilbert_table` | THE RULING through the daemon, with its own log line naming the dispatch |
| `test_the_layout_the_daemon_produced_is_a_hilbert_layout` | gated on av_hi not already matching |
| `test_the_daemons_layout_is_not_the_zorder_layout` | the daemon's curve defence, gated the same way |
| `test_the_install_script_and_the_catalog_agree_on_the_symbol_set` | S8, symbols resolved from the AS clause and never derived |
| `test_each_new_verb_is_installed_and_its_symbol_declared` | installed once, C, and declared |
| `test_each_new_verb_has_its_siblings_signature` | args, VARIADIC element and return type, compared against the sibling rather than retyped |


## 40. test_sorted_pathkeys.py: when a scan may claim its rows are ordered

Ports `test/sorted_pathkeys.sh` (#432), all 110 of its check names, one for one.
The bash suite pins one decision: when a columnar
scan may hand the planner PATHKEYS -- a promise that the rows come out in a stated order,
which lets the planner drop the Sort above it. A wrong promise is not a slow plan, it is
WRONG ROWS, because nothing downstream re-checks the order.

So every arm here is in one of three shapes, and the file is organised by them rather than
by feature:

- **CLAIM.** The relation really is ordered, the Sort really does disappear.
- **REFUSAL.** Something made the claim untrue -- an append, an UPDATE, a rewrite, a
  collation change -- and the Sort must come BACK.
- **ANSWER.** The rows themselves, against a heap table built from the same data. This is
  the shape that catches a wrong claim, because a plan check alone cannot: a scan that
  promises an order it does not keep produces a plan that looks right.

An ANSWER arm is not a duplicate of its CLAIM arm. Dropping the Sort is only correct if the
rows arrive sorted anyway, and only the heap comparison can say whether they did.

### The arm that needed a cluster setting, not a workaround

`pgcolumnar.parallel_copy` prepares one transaction per worker, and
`max_prepared_transactions` cannot be raised without restarting the postmaster. The
default is 0, so asking for fewer workers does not help: any number of workers is one
too many.

`pgc_cluster` therefore sets it where it writes `postgresql.conf`, at the value
`lib.sh` gives this suite through `PGC_EXTRA_CONF`. The alternative -- refusing the arm
with `expect.cannot_run` -- was measured and rejected for two reasons. It would have
lost three of the bash suite's names outright, because `cannot_run` records under the
REASON CODE rather than under a name (#1040 phase 0b). And it would have turned the
`pytest (cluster tests)` job RED: an unrunnable check exits 67, the job runs pytest
under `set -euo pipefail`, and no file in that half had ever produced one. Measured:
the cluster leg exits 0 today with 0 unrun.

The arm asserts `pg_prepared_xacts` is empty afterwards. A prepared transaction left
behind holds its locks until someone resolves it, and this cluster is session-scoped --
so a leak would not fail this test, it would wedge every file that runs after it.

### What the port asserts that the original gets for free

`psycopg` returns a PostgreSQL array as a python list, so `{k,j}` arrives as `['k','j']`
and a text comparison against the bash suite's expected output would fail for a reason that
has nothing to do with ordering. The port casts to `::text` in SQL instead of comparing
python objects, so both harnesses are reading the same string the server produced.

The COPY arms need a directory the SERVER can write. `tmp_path` is under
`/tmp/pytest-of-root/`, mode 700, which the backend cannot reach -- so a `server_dir`
fixture makes a world-writable one. The bash suite never meets this because it runs its
psql as the same user.

| test | asserts |
| --- | --- |
| `test_the_fixture_really_is_ordered` | the premise: the rows are in the order the test is about, measured by inversions rather than assumed |
| `test_a_real_ordering_loses_the_sort` | the CLAIM: an order the rows are actually in drops the Sort |
| `test_an_order_the_rows_are_not_in_keeps_the_sort` | the control: a different order must still pay for a Sort |
| `test_the_columnar_answer_matches_heap_in_order` | the ANSWER: the rows, against a heap built from the same data |
| `test_a_constant_leading_key_is_skipped` | a leading key with one distinct value cannot prove the second key's order |
| `test_a_run_with_an_appended_tail_is_not_an_ordered_relation` | rows appended past the run end the ordering, however sorted the run still is |
| `test_the_tail_answer_matches_heap` | and the rows after the append are still right |
| `test_a_zorder_run_is_not_a_sort_on_its_lead_column` | Z-order interleaves bits, so it orders NEITHER column on its own |
| `test_a_declared_sort_key_is_an_intention_not_a_layout` | a declared key on an unsorted relation is a statement of intent, not evidence |
| `test_an_unsorted_vacuum_retracts_the_ordered_path` | a vacuum that rewrites without sorting must retract the claim |
| `test_a_type_change_rewrite_drops_the_mark` | a rewriting ALTER TYPE changes the values, so the old mark cannot survive it |
| `test_one_updated_row_is_a_row_outside_the_run` | a single UPDATE appends, and one row outside the run is enough |
| `test_the_mark_follows_a_rename` | #778: the mark is stored by name, so a RENAME COLUMN must carry it |
| `test_a_recorded_name_that_no_longer_resolves_is_not_a_claim` | a name that resolves to nothing must retract rather than fall through |
| `test_the_guc_turns_the_claim_off` | the GUC is a real off switch, checked with the Sort back |
| `test_a_ctas_relation_claims_nothing` | CTAS writes rows in whatever order the query produced; nothing records an order |
| `test_a_collatable_sort_column_is_not_claimed` | text order is collation-dependent, so a run sorted under one collation is not sorted under another |
| `test_a_collation_alter_changes_the_order_without_rewriting` | the mechanism: ALTER COLLATION changes the ORDER while the bytes stay put |
| `test_a_domain_and_an_array_carry_their_base_collation` | a domain over text and a text[] inherit the collatability, and the refusal with it |
| `test_a_composite_is_claimed_and_postgres_closes_the_hole` | a composite of two texts, and where PostgreSQL itself refuses first |
| `test_an_enum_add_value_before_does_not_renumber` | `ADD VALUE ... BEFORE` inserts a sort order without renumbering, so a sorted run stays sorted |
| `test_a_cached_ordered_plan_is_retracted` | a plan cached while ordered must be retracted by INSERT, INSERT ... SELECT and a plain append |
| `test_a_cached_plan_is_retracted_by_copy` | the same through COPY, which takes a different write path |
| `test_a_cached_plan_is_retracted_under_parallel_flush` | and under `parallel_flush`, where the rows arrive from workers |
| `test_a_cached_plan_is_retracted_across_backends_by_parallel_copy` | the cross-BACKEND case, the only write path where the invalidation crosses a process boundary; asserts the rows loaded before asserting the retraction, and that no prepared transaction leaked |
| `test_truncate_restarts_numbering_in_a_new_storage` | TRUNCATE gives a new relfilenode, so nothing from the old storage may carry |
| `test_a_reclaiming_rewrite_retracts_while_the_rows_stay_ordered` | the hard case: the rows stay in order and the claim must still go, because the run boundaries moved |
| `test_a_query_that_cannot_use_the_order_does_not_pay_to_decide` | deciding the claim must not read buffers for a query that cannot use it |
| `test_a_projection_does_not_lend_its_order_to_the_base_relation` | a sorted projection is a different relation; its order is not the base table's |
| `test_a_plain_gather_never_sits_above_a_scan_claiming_an_order` | Gather does not preserve order, so the two must never be stacked |


## 41. test_projections.py: a second copy of some columns, kept honest

Ports `test/projections.sh` (#432), all 75 of its check names, one for one.

A projection is a second copy of some columns. Every property here is about the copy
staying honest: it holds the rows the base holds, it loses the rows the base loses, it
survives a vacuum that renumbers every row underneath it, and the planner reads it only
when it can answer the whole query from it.

So a wrong projection is a WRONG ANSWER, not a slow one. A scan that reads a stale
projection returns rows the base no longer has, and nothing downstream re-checks.

The file is organised by what can make the copy diverge, not by feature:

| group | what can go wrong |
| --- | --- |
| CATALOG | `add_projection` records the wrong thing, or accepts what it should refuse |
| FAN-OUT | a write reaches the base and not the copy -- including a DELETE, whose liveness comes from the base's delete vector |
| RECONSTRUCT | a column the projection does not store is fetched from the base BY ROW NUMBER; if that linkage drifts the rows pair up wrongly |
| PLANNER | a covering projection is chosen when it can answer, and must not be when it cannot |
| REBUILD | `pgcolumnar.vacuum` compacts the base into fresh row numbers; a projection left on the old numbering is keyed to rows that mean something else |
| MVCC | an old snapshot must not see rows committed after it -- through a projection scan as much as through the base |
| LIFECYCLE | a dropped table's declaration (#304), and a projection added or dropped mid-transaction (#875) |

### Four places the port asserts more than the original

This is the first port where the difference is worth a section, because in one of them
the port is **strictly stronger** and a reader comparing the two should know which way.

**`expect_fail` becomes `expect.sqlstate`.** The original's own helper runs the
statement and passes when it errors AT ALL, so a misspelt table name satisfies every one
of its eight refusal arms. Each code below was MEASURED against this build, and they are
all distinct, so each arm now names the refusal it is for:

```
duplicate name          42710      add on heap table       42809
unknown column          42703      drop base               22023
empty columns           22023      drop unknown            42704
duplicate column        42701      read_projection base    42704
sort key not in columns 22023
```

The names are the bash suite's; the assertions are not.

**The EXPLAIN grep becomes a typed field.** `grep -c 'Columnar Projection: pc'` is a
substring test over text. The plan carries `"Columnar Projection": "pc"` as a property,
so the port reads the value -- and the mutation proof confirms it reads the NAME rather
than the presence: forcing the planner to refuse gives `got None want 'pc'`. The two
NEGATIVE arms use `expect.plan_marker(absent=True)`, which refuses an empty plan, because
a plan that never arrived looks exactly like a plan carrying no projection.

**`pgc_set_hash` becomes `expect.row_set`.** Order-blind by declaration rather than by
construction. A hash mismatch says two hashes differ; a row-set mismatch says which row.

**The second session is a second connection.** The original drives a background
`psql -f fifo` and waits by polling its output file for a token, up to 200 times at
0.1s. A second `psycopg` connection removes the wait rather than shortening it: the
query returns when it returns. The original's two arms that exist only to NAME that
polling timeout -- `session A opened snapshot` and `session A responded post-commit` --
are carried here as the positive facts they are the negative of.

### Arrays are cast in SQL

`psycopg` returns a PostgreSQL array as a Python list, so `{1,2,3}` arrives as
`[1, 2, 3]`. Casting `::text` in the query keeps both harnesses comparing the string the
server produced, rather than comparing a Python object against a brace literal and
failing for a reason that has nothing to do with projections.

| test | asserts |
| --- | --- |
| `test_the_catalog_is_empty_until_the_first_projection_is_added` | the base projection is recorded LAZILY, so the catalog holds nothing before the first add |
| `test_the_first_add_records_the_base_and_the_new_projection` | both rows appear at once, with the base's columns, empty sort key, name and shared storage id |
| `test_a_second_projection_may_have_no_sort_key` | a projection without a sort key, and three distinct storage ids |
| `test_a_bad_projection_is_refused_by_its_own_code` | seven refusals, each by its measured SQLSTATE rather than by "it errored" |
| `test_a_projection_on_a_heap_table_is_refused` | the refusal that is about the ACCESS METHOD, not the arguments |
| `test_drop_removes_one_projection_and_leaves_the_rest` | drop is surgical, and the table is still readable after the DDL |
| `test_a_projection_added_late_is_back_filled_from_the_existing_rows` | a projection added after the rows exist is populated from them, not left empty |
| `test_a_write_fans_out_to_every_projection` | the write path: both projections match the base, by rows and by count |
| `test_projection_chunks_carry_skip_metadata` | the min/max that makes choosing the projection worth anything |
| `test_a_delete_reaches_the_projection_through_the_base_delete_vector` | liveness comes from the BASE, so a delete that never touches the copy still removes its rows |
| `test_fan_out_spans_more_than_one_row_group` | one group is the case where a numbering bug cannot show |
| `test_the_base_projection_cannot_be_read_by_name` | `base` names a catalog row, not something `read_projection` addresses |
| `test_columns_the_projection_lacks_are_reconstructed_from_the_base` | the row-number linkage between copy and base |
| `test_reconstruction_survives_deletes_and_nulls` | where a drifting row number shows first: missing rows and absent values |
| `test_a_covering_sort_key_query_reads_the_projection` | the projection is chosen, AND the rows match a heap oracle -- a plan check alone cannot say the rows were right |
| `test_the_guc_is_an_off_switch` | the off switch really switches off |
| `test_a_query_naming_an_uncovered_column_falls_back_to_the_base` | choosing a projection that lacks `b` would drop the column, not merely cost more |
| `test_a_projection_scan_reflects_deletes` | the scan path, against the oracle, after a delete and over the full range |
| `test_vacuum_rebuilds_the_projection_against_the_compacted_base` | survives, is still chosen, and still matches the oracle on fresh row numbers |
| `test_a_second_vacuum_renumbers_again_and_stays_correct` | ONCE IS NOT THE PROPERTY: a rebuild reading the pre-vacuum numbering is right the first time |
| `test_an_old_snapshot_never_sees_rows_committed_after_it` | REPEATABLE READ through a projection scan, the case no single-session test can reach |
| `test_dropping_a_table_removes_only_its_own_declaration` | #304: one orphan used to abort `rebuild_projections()` for every other table |
| `test_the_rebuild_repairs_an_orphan_rather_than_aborting_on_it` | a database from an older build already holds orphans, so the rebuild must clean rather than abort |
| `test_a_projection_added_mid_transaction_receives_the_later_writes` | #875: a write before the add latches an EMPTY writer list and every later write skips silently |
| `test_the_control_a_transaction_with_no_write_before_the_add` | the control -- that path always worked and must stay working |
| `test_a_projection_dropped_mid_transaction_stops_receiving_writes` | the same latch with the opposite sign, including the orphan storage it would leave |
| `test_the_control_a_drop_in_its_own_transaction` | pins the arm above to the CACHE rather than to `drop_projection`'s own cleanup |

## 42. test_compression_reaches_the_cascade.py: the codec setting decides encodings too

`pgcolumnar.compression` reads as a codec choice, and the documentation said exactly
that: "default codec for new chunks". It is not only that. It also decides which
lightweight encodings a chunk gets, and nothing named the coupling until #1076.

`columnar_encoding.c` returns "FSST helps" UNCONDITIONALLY when the codec is `none`,
before it looks at the corpus or at `fsst_min_gain_percent`:

```c
if (compressionType == COLUMNAR_COMPRESSION_NONE)
    return true;
```

The reasoning is sound. Whether FSST is worth keeping depends on the size AFTER the
codec, because what lands on disk is the encoded stream compressed, and with no codec
the encoded length already IS the stored length. The consequence is that `none` is a
DIFFERENT cascade rather than the same cascade with a step removed.

**Every arm here is a differential, and that is not decoration.** "FSST is kept under
`none`" on its own is satisfied by any corpus FSST always wins on, which would make the
file green and empty. So each arm loads the same corpus at the same margin WITH a codec,
where FSST is dropped, and the only variable between the two loads is the setting under
test.

| test | what it establishes |
| --- | --- |
| `test_the_codec_setting_decides_whether_fsst_is_kept` | margin 90, same corpus: dropped with `zstd`, kept with `none` |
| `test_the_margin_is_never_consulted_when_there_is_no_codec` | margin 99 is the maximum the GUC accepts, and it still does not drop FSST under `none` |
| `test_the_corpus_is_marginal_rather_than_one_fsst_always_wins` | the premise the other two rest on: with a codec, margin 0 keeps and margin 90 drops |
| `test_the_rows_survive_every_combination` | the invariant, against a heap mirror, so a decision arm cannot pass while data changes |

The third is the one that makes the rest mean something. A corpus FSST wins or loses
outright cannot show the coupling at all, because the margin never gets a say.

**Not a port and not a pair.** `test/fsst_margin.sh` asserts the same property, and the
two share no corpus, no helper and no byte layout. Each parses
`pgcolumnar.column_chunk.encoding_descriptor` itself: a 6-byte header, then one 13-byte
entry per vector whose first byte is the encoding type. Reading past the entry count
would score the chunk's shared symbol table bytes as encoding types, so both bound the
scan by the count rather than by the descriptor's length.

Removal proof, run on both harnesses: deleting the early return and rebuilding moves the
`.so` from `c8e5c790dbac` to `6455728725e5` and reddens exactly the codec arms, while
every content invariant stays green. The mutant still writes correct rows, which is the
point of keeping the invariant in the file: this is a decision changing, not corruption.

## 43. test_pgxn_metadata.py: the published distribution metadata, which nothing read

`META.json` is what PGXN receives. It hardcodes the version twice and names the base
install script by filename, and **no suite, no Makefile rule and no CI step ever read
it**. It went stale for the whole alpha4 cycle:

```
version                    1.0.0-alpha.3     while VERSION said 1.0-alpha4
provides.pgcolumnar.file   pgcolumnar--1.0-alpha3.sql
```

**The filename is the half that matters.** `pgcolumnar--1.0-alpha3.sql` does not
exist. This repository opens each cycle by RENAMING the base script to the new
version, so the published metadata named a file the distribution does not contain.
The two version strings were only the visible symptom, and a reader comparing them
against `VERSION` would have called it a cosmetic lag.

| test | what it establishes |
| --- | --- |
| `test_the_version_derivation_maps_the_forms_this_project_uses` | the transform itself, before anything relies on it |
| `test_meta_json_states_the_version_the_VERSION_file_holds` | both version fields, against the source of truth |
| `test_the_script_meta_json_names_is_in_the_published_distribution` | the defect that actually shipped |
| `test_every_sql_file_meta_json_could_name_is_shipped` | the neighbouring failure: an upgrade script dropped from the archive |

**The first exists because a derivation is a claim too.** PGXN requires three-part
semver, so `1.0-alpha4` publishes as `1.0.0-alpha.4`, and the mapping is computed
rather than hardcoded so a future `1.0-beta1` is covered. If that transform were
wrong, every other arm would compare against a wrong expectation and could pass or
fail for reasons having nothing to do with `META.json`.

**The fourth covers what the third cannot see.** `provides.file` is ONE filename, so
an `export-ignore` that dropped a different install script -- an upgrade path --
would leave the third arm green while `ALTER EXTENSION ... UPDATE` broke for anyone
who installed from PGXN.

`git archive` rather than `os.path.exists` throughout, because the question is what
the DISTRIBUTION contains and not what the working tree holds. An `export-ignore`
attribute can drop a file that is plainly present on disk, and this repository has
been bitten by one before.

**Not a port and not a pair.** `docs_style.sh` asserts the same properties through
its own parse and its own `git archive`; neither file names the other.

Removal proof, run on both harnesses: restore `META.json` as it shipped and the two
substantive arms redden on each side while every premise stays green. The premises
hold because the file still parses and still names *a* script -- it names the wrong
one, which is exactly the distinction the arms draw.

## 44. test_native_chunk_length_bound.py: a truncated chunk length cannot fetch

A column chunk's `page_length` is `uint64` in the catalog. Both decode entry
points used to cast the value stream to `uint32`. Adding 2^32 leaves the low
32 bits unchanged, so an index fetch silently read the original stream and
returned the row. A sequential scan already refused, because the chunk no
longer fitted its row group.

This file asserts the SQLSTATE, not a cost number. The poison is a catalog
UPDATE; the property is that a fetch raises XX001 and the backend survives.

Independent of `test/native_chunk_length_bound.sh`. Same public seam, own

### Every arm

| test | what it asserts |
| --- | --- |
| `test_native_chunk_length_bound` | a point lookup uses the index and returns the row; after `page_length` grows by 2^32, both the fetch and a sequential scan raise XX001 and the backend survives each |

## 45. test_native_fetch_coalesce.py: index fetch I/O is not per-column

Index fetch used to pin once per column: validity bitmap, then the value stream,
two `PgColumnarReadLogicalData` calls each. Sequential scan already coalesces
adjacent chunk ranges into one read. Adjacent columns sit back to back, so a
wide fetch of a small group was many pins of the same pages.

The public seam is executor buffer pins on `EXPLAIN (ANALYZE, BUFFERS)`, not
wall clock. Planning pins grow with the target list and are excluded. After the
fix, fetching every projected column must not pin once per extra column.

Independent of `test/native_fetch_coalesce.sh`. Same public seam, own fixture,
own observations. Assertion names match the shell suite.

| test | what it asserts |
| --- | --- |
| `test_native_fetch_coalesce` | a point lookup uses the index and returns the projected values; executor pins for one column and for every column are both measurable, and the wide fetch does not pin once per column |
| `test_the_validity_copy_is_bounded_before_the_chunk_is_read` | the bound on the validity copy precedes the copy, read as positions in the coalescing helper rather than as the presence of both statements -- the overread it guards had both |
## 46. test_parallel_am_scan.py: a table-AM parallel scan must share work

The port of `test/parallel_am_scan.sh`. With the custom scan off, Parallel Seq
Scan goes through the table AM. `phs_nallocated` was a first-wins flag: one
backend claimed the whole scan and every launched worker reported 0 rows.
The custom-scan path already claims distinct row groups; this pair pins the
AM path to the same property.

Public seam: `EXPLAIN ANALYZE` worker rows on a Parallel Seq Scan. Leader
participation is off so the two launched workers are the claimers under
test. The shell twin uses its own table (`pam`, 50000 rows, groups of 100);
this file uses `ampar`, 80000 rows, groups of 200. Assertion names match.

### Every arm

| test | what it holds |
| --- | --- |
| `test_parallel_am_scan` | the serial plan is a Seq Scan, not a custom scan; the parallel plan is a Seq Scan under Gather with two workers launched; a parallel AM scan returns the same count as serial; both launched workers produced rows |
| `test_a_parallel_index_build_covers_the_whole_table` | a parallel index build requests workers and indexes every row -- compared as count and SUM through the index against a sequential scan, because a group read twice cancelling a group skipped leaves the count right |

The load-bearing assertion is `workers share the table-AM scan, it is not a
single claimer`. It is unreachable while `phs_nallocated` is first-wins, and
reachable only when each worker claims its own row groups.

## 47. test_index_fetch_penalty_crossover.py: the correlated range must not fetch

#913. A fetching index scan on a correlated key is priced below the custom scan
through ~50,000 rows, while it does about 27x the work. The penalty term exists
for this; the measurement says it is too small. Split from #766, which closed
on the opposite question.

This file asserts the PLAN, not a cost number. Costs drift with the constants.
The chosen node is the property.

Independent of `test/index_fetch_penalty_crossover.sh`. Same public seam, own
fixture, own observations. Assertion names match the shell suite.

### Every arm

| test | what it asserts |
| --- | --- |
| `test_index_fetch_penalty_crossover` | a 50,000-row correlated range uses the custom scan; a point lookup still uses the index; both paths agree on the aggregate; a clustered ORDER BY stays on the index |

## 48. test_parallel_scan_cost.py: a parallel custom scan must not divide I/O

The port of `test/parallel_scan_cost.sh`. The partial path priced itself as
`serial_startup + (serial_run / workers)`. Core seqscan divides CPU only and
leaves disk I/O whole. Dividing the whole run quotes an I/O-dominated scan at
half its serial cost with two workers, so Gather beat honestly costed
alternatives.

Public seam: `EXPLAIN` of a columnar scan. This file raises `seq_page_cost` so
I/O dominates the serial run; the shell twin does the same with a different
page-cost and its own table. CPU terms stay at their defaults so the parallel
path is still a little cheaper than serial and Gather still appears -- the
number this suite exists to read. Assertion names match the shell suite.


### Every arm

| test | what it holds |
| --- | --- |
| `test_parallel_scan_cost` | the serial plan is a columnar scan with no Gather; the parallel plan is a columnar scan under Gather with two workers; both have a positive run cost; an I/O-dominated parallel scan is not priced at serial/workers |

The load-bearing assertion classifies the ratio `serial_run / parallel_run` as
`io-kept` (below 1.35) rather than `halved` (2.000 on the unfixed path). It is
unreachable by dividing the whole run, and reachable only if I/O remains.

## 49. test_residual_is_counted.py: a residual must be counted, not subtracted

#999 and #1006, filed independently by both sessions off the same runs. Every
PG 17 matrix report on `main` printed a count that cannot exist:

```
suites that ran: 243 of 252 (skipped: 9, incomplete: 0)
of those, 248 accounted for their checks and -5 did not
```

PG 18 printed `-2` the same day. The two terms count different populations:
`suites_ran` excludes a skipped suite, while `_acc_any` counts every registered
suite whose log shows an accounting line -- and a skipped suite still prints one,
because `pgc_summary` emits it on every exit path before it decides the status.

Nothing caught it because the residual was DERIVED. `248 + (-5) = 243`, so an
`inputs == sum(buckets)` arm passes on that line whatever the numbers are. It is
the error `pgc_summary` warns about eight lines below its own counter, committed
one level up.

Public seam: the two readers and the summary block, taken out of
`run_all_versions.sh` and run against files in `tmp_path`. The block is extracted
rather than retyped, because a retyped block is a second implementation and
agrees with itself. Independent of
`test/selftest/510-a-residual-must-be-counted.sh`: same properties, own fixtures,
and neither file names the other.

### Every arm

| test | what it holds |
| --- | --- |
| `test_the_residual_names_the_suites_that_ran_and_did_not_account` | the debt is a set of names, and the reader exits clean |
| `test_a_skipped_suite_that_accounted_does_not_become_a_debt` | on the live shape the residual is empty while the same reader still finds a real debt, and the skipped-but-accounted suites are their own named category |
| `test_the_old_subtraction_goes_negative_on_that_same_input` | the control: three ran minus five accounted is the `-2` the runner printed |
| `test_the_readers_sort_their_own_inputs` | `comm` on unsorted input yields a wrong set silently, so the sort lives inside the reader |
| `test_the_two_buckets_partition_the_suites_that_ran` | `inputs == sum(buckets)`, and neither bucket is the other's leftover |
| `test_the_summary_block_was_extracted_rather_than_an_empty_range` | an empty extraction runs nothing and reports clean, so the block is asserted before it is used |
| `test_the_printed_breakdown_takes_a_count_a_count_can_take` | the line a reader sees carries no negative, and names the skipped-but-accounted category |
| `test_the_same_block_counts_and_names_a_real_debt` | the same block still counts and names a genuine debt, so it is not printing `0 did not` unconditionally |
| `test_no_code_path_subtracts_the_wide_population_from_the_ran_count` | the defect's shape is refused at the source, over CODE lines only -- the comment explaining it quotes it verbatim |

The load-bearing assertion is that the printed breakdown takes a count a count
can take. It is unreachable while the residual is a subtraction across two
populations, and reachable only once it is a set difference over the names.
## 50. test_collation_pinned.py: comm's inputs must be sorted the same way

#552 established the rule and #1112 found the hole. `comm` requires both inputs
sorted in ITS collation and does not check: fed a mismatch it writes `input is not
in sorted order` to stderr and prints a result anyway, so where stderr lands in a
log nobody reads, a wrong set arrives looking like an answer.

The inputs are not collation-insensitive. On real suite names,
`pgc_setup`/`pg_dump_roundtrip` and `projections`/`projection_update` both swap
between `C` and `en_US.UTF-8`.

**Two halves, and the second is the one that was missed.** `LC_ALL=C comm <(sort a)
<(sort b)` pins only comm's own comparison: the substitutions run in subshells of
the **parent** and inherit its locale. A guard accepting `LC_ALL=C` anywhere on the
line would bless exactly the form a reader writes after reading the guard's name.

Public seam: the shell corpus under `test/*.sh`. Read independently of
`test/selftest/070-and-comm-s-two-inputs-must.sh` -- same corpus, own
implementation, own planted probes, and neither file names the other.

### Every arm

| test | what it holds |
| --- | --- |
| `test_the_corpus_has_files_using_comm_so_the_sweep_is_not_vacuous` | the sweep spans more than one file, printed from the data |
| `test_every_sort_feeding_a_comm_pins_its_collation` | no suite using `comm` leaves a sort on the caller's locale |
| `test_every_comm_pins_its_own_comparison` | and the `comm` itself is pinned, which is a separate claim |
| `test_the_detector_catches_the_process_substituted_form` | the hole #1112 names: not a pipeline, so a pipe pattern cannot see it |
| `test_the_detector_still_catches_the_piped_form` | widening did not trade #552's case away |
| `test_a_line_with_one_of_two_sorts_pinned_is_caught` | the half-pinned form, which is what a partial fix produces |
| `test_pinning_only_the_comm_does_not_pin_its_substitutions` | a pinned `comm` over unpinned sorts is still a breach, while its comm half is satisfied |
| `test_a_fully_pinned_line_is_not_flagged` | without which the detector could be "flag everything" and every arm above still passes |
| `test_prose_describing_the_rule_does_not_violate_it` | a comment containing the forbidden form is not a breach -- it flagged `run_all_versions.sh` for its own text before the guard skipped comments |
| `test_an_unpinned_comm_is_caught_and_a_word_containing_comm_is_not` | `command` and an identifier containing `comm` are not comms |

Both corpus arms report zero on this tree, measured before the file was written, so
the detector is proved by planting rather than by the corpus. The load-bearing arm
is the process-substituted form: unreachable by a pipe pattern, and reachable only
once the detector reads substitutions too.

## 51. test_projection_scan_cost.py: a covering projection is not priced at half

The covering-projection path took the base custom-scan run cost and multiplied
by 0.5. That constant does not depend on the restriction, so a tight range on
the sort key was quoted the same as a loose one. The projection is stored
sorted on that key; the planner number has to move with selectivity.

This file asserts the PLANNER ratio, not a runtime. Public seam: `EXPLAIN` of
a columnar scan with `pgcolumnar.enable_projection_scan` on and off. The
shell twin uses its own table (`prsc`, 20000 rows, 5 percent vs 50 percent);
this file uses `pscost`, 24000 rows, 1800 vs 12000. The misattributed-selectivity
arm uses `prsk`/`kind` on the shell side and `psmis`/`flag` here. Assertion
names match.

| test | what it asserts |
| --- | --- |
| `test_projection_scan_cost` | the table and covering projection exist; the tight and loose plans use that projection; without the GUC they are base columnar scans; every compared scan has a positive run cost; a tight covering projection is cheaper relative to the base than a loose one; the two ratios are not both 0.5; a non-sort-key restriction does not cheapen a covering projection |
## 52. test_record_names_its_major.py: a record must name its major

#1121. `pgc_record` writes `${PGC_MAJOR:-unknown}` and `PGC_MAJOR` is set inside
`pgc_setup`, so a suite that records but never calls `pgc_setup` writes every
check against the literal string `unknown`.

The gate matches a ledger row only where its majors intersect the majors the run
observed, and **no run ever observes `unknown`**. Such a check cannot be seeded,
and a row for it could never be matched again. Measured on PG 17 before the fix:

```
smoke 9/9   audit 31/31   objstore_stash_recovery 17/17   phase2 42/42
phase3 32/32   phase4 38/38   phase5 36/36   phase6 43/43
---- 248 of 248 records named no major ----
```

**A static rule cannot do this job, and two attempts failed in different
directions.** "Defines no `check()` of its own" finds 5 and misses `audit`, whose
own `check()` body calls `pgc_record`. "The file contains the string
`pgc_record`" finds 7 and misses `objstore_stash_recovery`, which uses `lib.sh`'s
`check()` so the string never appears in it. The truth was 8 both times. Whether
a suite records is a runtime property, so the guard reads the records.

Public seam: the RESULT record format and the runner's text. Read independently
of `test/selftest/530-a-record-must-name-its-major.sh`, which evals the shell
reader out of the runner while this parses the format directly. Neither file
names the other.

### Every arm

| test | what it holds |
| --- | --- |
| `test_a_clean_log_counts_none_while_a_mixed_one_counts_its_own` | asserted as a pair, because zero is also what a wrong field number and a broken parser produce |
| `test_every_offending_record_is_counted_not_just_the_first` | a suite can record some checks before `pgc_setup` and some after |
| `test_a_log_with_no_records_is_not_an_offender` | carrying no records is not naming a bad major |
| `test_the_word_in_a_reason_field_is_not_an_offending_record` | field six, not the line -- with the control that the same word in field six IS caught |
| `test_a_short_record_does_not_crash_or_count` | a truncated line has no field six |
| `test_the_runner_reads_every_suites_log_and_fails_the_major` | the wiring, bounded to the guard's own block |

The load-bearing assertion is the last one's bound. The runner has many later
`verfail=1` lines, so an unbounded search finds one whatever the guard does --
which is exactly how the shell twin's first version of that arm stayed green
against the line removed. Mutation testing caught it.

## 53. test_analyze_reltuples.py: ANALYZE must estimate the row count, not zero

Port of `analyze_reltuples.sh` (#432). A block was mapped to its row group by comparing
offsets that had `COLUMNAR_FIRST_LOGICAL_OFFSET` subtracted from one side and not the
other, so `reltuples` came back 0 for a 10,000-row table and the planner believed every
columnar table smaller than one stripe was empty.

heap is the oracle: ANALYZE samples, so the columnar estimate is not required to be exact,
only as close as heap's on the same data.

Two assertions the bash suite does not make. Its helper builds the columnar and heap
tables with separate inserts and compares their estimates without checking they hold the
same rows, so this port asserts both counts. And the arm named `20 stripes: within 5% of
actual` sets `pgcolumnar.stripe_row_limit` but checks only the estimate, which a
single-group table also passes; this port reads `pgcolumnar.row_group` and fails first if
the fixture is not the shape the name claims.

### Every arm

| test | what it holds |
| --- | --- |
| `test_analyze_estimates_the_row_count` | every arm, from the four paired columnar/heap sizes through the multi-stripe geometry, the delete, and the clustered `n_distinct` |

## 54. test_projection_update.py: UPDATE must fan the new row number out to projections

Port of `projection_update.sh` (#432). UPDATE is delete-old plus insert-new, and the
insert half used to skip fan-out, so `read_projection` returned no rows and a covering
projection scan answered as if the updated rows had been deleted.

Row sets are compared as sorted tuples in Python rather than through `pgc_set_hash`. That
keeps the two harnesses independent by construction, and a failure prints the rows that
differ instead of two unequal hashes.

The port also pins the affected row counts. An UPDATE whose WHERE matched nothing leaves
both sides identical and every arm below it green, having exercised no fan-out at all.

### Every arm

| test | what it holds |
| --- | --- |
| `test_update_fans_the_new_row_number_out_to_projections` | every arm: the covering scan before and after updating a projected column, then a non-projected one, with `read_projection` and `reconstruct_via_projection` |

## 55. test_projection_drop_column.py: DROP COLUMN must not invalidate a projection

Port of `projection_drop_column.sh` (#432). Projections are extension metadata rather
than `pg_depend` objects, so PostgreSQL accepted `DROP COLUMN` on a projected column and
the next INSERT failed in `lookup_type_cache`.

SQLSTATE rather than message text: `2BP01` for the dependency refusal and `42501` for the
non-owner. The bash suite gets there by running `psql` with `VERBOSITY verbose` and
extracting the code with `sed`; the port reads `exc.sqlstate`.

`SET ROLE` rather than a login role, which is the opposite of
`test_projection_privilege.py` and for the reason that file gives: real logins are needed
when the ACL layers are the subject, and only add ways to fail when ownership is.

The two non-owner arms exist to show a stranger cannot tell a projected column from an
unprojected one. The bash suite asserts each against the literal `42501`; the port also
compares them to each other, so indistinguishability is asserted rather than implied.

### Every arm

| test | what it holds |
| --- | --- |
| `test_drop_column_is_refused_while_a_projection_depends_on_it` | every arm: the two non-owner refusals, the dependency refusal, writability afterwards, the unrelated column, and the partitioned parent |

## 56. test_encode_post_codec.py: an encoding must be smaller after the codec

#1132. `PgColumnarEncodeChunk` compares every candidate against `bestLen`,
which starts at `rawLen`, and every comparison is on UNCOMPRESSED bytes. The
block codec runs afterwards, once, over the whole encoded region, defaulting to
zstd level 3. Bit-packing whitens a stream the codec was exploiting, so an
encoding that shrank the bytes can **enlarge** the stored chunk.

FSST already decided post-codec through `PgColumnarFsstHelpsCompressed`. This
file asks the same question of the encoders that had no such gate. Measured on
ClickBench `hits_0.parquet`, 16 of 77 fixed-width columns were stored larger
encoded than raw, costing 7.17% of their stored bytes.

**THE FIXTURE IS THE ARGUMENT, and five synthetic shapes failed to reproduce the
defect before the sixth did.** The worst real column, `ClientEventTime`, is a
HEAVY TAIL: rare outliers stretch the range while the typical value stays in a
narrow band. That splits the two cost models exactly -- frame-of-reference
prices by RANGE and must size every value for the outliers, while zstd prices by
BYTE REDUNDANCY and the typical value's high bytes are constant. Repetition
alone does not reproduce it (measured 0.71x, encoding winning), so the tail is
load-bearing and the first premise asserts it.

**That premise was a coin flip in its first version.** It read the 0.1st-to-99.9th
percentile spread, which with one outlier in a thousand lands exactly ON the
boundary: from one seed it measured 4,994 on one run and 53,786,536 on another,
green then red with no code change between. It reads the 1st-to-99th percentile
now, measured at 4905 / 4905 / 4904 across three runs.

Public seam: the encoding descriptor and `column_chunk.page_length`. Independent
of `test/encode_post_codec.sh`, which reads the descriptor through `get_byte()`
in SQL while this decodes it in Python, over its own cluster, its own table
names and its own corpus constants. Neither file names the other.

### Every arm

| test | what it holds |
| --- | --- |
| `test_a_chunk_is_never_stored_larger_than_unencoded` | the arm, its two premises, and both controls |
| `test_the_decision_never_changes_what_comes_back_out` | the invariant the size arms exist to prove is not vacuous |

The controls are the load-bearing half. Declining every encoding would satisfy
"never larger than unencoded" and redden nothing, so the repeating column ships
beside the tail one and asserts that encoding is still chosen where it wins.

## 57. test_validity_elision.py: a column with no nulls must store no validity bitmap

#1130. A column chunk's page was always `[validity bitmap][encoded values]`. The
bitmap is one bit per row, written RAW ahead of the block codec, which therefore
never saw it -- so a column holding no null still stored `ceil(rows / 8)` bytes
of `0xFF` for ever. Measured on ClickBench `hits_0.parquet` (1,000,000 rows, 105
columns, no null in any of them): **16.80% of everything stored**, and 99.5% of
the page on a well-encoded column whose values came to 135 bytes.

The writer now omits the bitmap for a chunk that holds no null and says so in the
descriptor's new flags byte (`NO_VALIDITY`, descriptor version 3).

**THE MEASUREMENT IS EXACT, not a ratio.** With the block codec off the page is
exactly `[validity][encoded]`, so `page_length - sum(encLen)` IS the bitmap, with
nothing else in it. The arms assert `0` and `ceil(rows / 8)` rather than a
threshold, so a wrong answer cannot hide inside a tolerance. The suite turns the
codec off for that reason; with one, the same subtraction reads a compression
ratio and calls it a bitmap.

**THE FETCH PATH IS A SECOND READER, and it is the reason this file has a third
test.** A scan reaches a chunk through `pgcolumnar_native_load_group`; an index
scan reaches a row through `pgcolumnar_fetch_get_row`, which reads the chunk's
bytes itself. The first implementation fixed the scan and not the fetch, and the
PG17 matrix went red in 33 suites -- `native_index`'s point lookup returned NO
ROW for a row that is there. Worse, the two are CORRELATED: the coalesced read
skips a chunk whose `page_length` is below the group's bitmap size, and eliding
the bitmap is exactly what takes a well-encoded chunk below it. A fixture built
to exercise the elision is therefore systematically the fixture that lands on
the reader most likely to have been left behind.

**TWO ARRANGEMENTS MAKE THE FETCH ARMS REAL, and both were found by an arm that
failed rather than by reasoning.** The table carries a KEY column beside the
measured one, because an index on the only column is answered by an Index Only
Scan that never calls the table AM at all -- the first version of these arms
passed against a build whose fetch path was provably broken. And
`pgcolumnar.enable_custom_scan` is off, because `enable_seqscan` does not govern
the columnar custom scan: without it the plan was
`Custom Scan (PgColumnarScan)`, a scan wearing a fetch's name. The premise arm
asserts the PLAN NODE rather than the row, because a row that comes back is no
evidence about which reader produced it.

Public seam: `pgcolumnar.column_chunk` and the encoding descriptor. Independent
of `test/validity_elision.sh`, which reads the descriptor through `get_byte()`
in SQL and turns the codec off with `ALTER DATABASE`, while this decodes the
descriptor in Python bytes and uses a session `SET` -- which it can, because one
connection serves the whole test and the shell twin's `psql_run` opens a new
session per statement. Neither file names the other.

### Every arm

| test | what it holds |
| --- | --- |
| `test_a_column_with_no_nulls_stores_no_validity_bitmap` | the size arms and the five premises they rest on |
| `test_the_layout_change_returns_the_same_rows` | the invariant the size arms exist to prove is not vacuous |
| `test_the_guards_the_elision_added_refuse_a_lying_catalog` | both new guards, poisoned into firing, asserted by SQLSTATE |
| `test_the_fetch_path_reads_an_elided_chunk_correctly` | the second reader, with the plan asserted rather than assumed |

**FIVE PREMISES, AND EACH ANSWERS A WAY THE HEADLINE ARM READS 0 WITHOUT
MEASURING ANYTHING.** The residual arm expects `0`, which is also what
`coalesce(sum(...), 0)` returns over an empty set, so the chunk count is
asserted beside it. The exact size assumes one row group, so the group count is
asserted. And the subtraction is the bitmap only while the block codec is off,
so the setting is read back rather than assumed — `SET` and `ALTER DATABASE`
are statements that have to have taken effect.

**THE PER-CHUNK ARM IS THE ONE THAT CANNOT BE PASSED BY A PER-GROUP DECISION.**
Both size arms are satisfied by a writer that decides elision once per row
group: one fixture's group holds no null anywhere and the other's holds some.
The null-bearing fixture carries a null-free KEY column in the same row group,
so only a per-chunk decision elides one and keeps the other.

**AND IT CARRIES ITS OWN PREMISE, BESIDE IT RATHER THAN WITH THE OTHERS.** It
expects `0`, and `0` is also what the residual returns when it summed nothing:
a column index off the end of the table answers exactly as a correctly elided
bitmap does. @OffgridwithJD measured that against the first version of this
pair -- pointing that one residual at `column_index 99` left all 24 checks green
in BOTH harnesses, because `_residual` returns the chunk count and two of its
three call sites discarded it. The two residuals that keep discarding it are the
ones a bug makes LARGE, which is the distinction worth carrying: a premise is
owed wherever the failure mode and the pass look the same.

**THE GUARD ARMS ASSERT SQLSTATE `XX001`**, not message text: that is
`ERRCODE_DATA_CORRUPTED` and comes only from a guard that ran. One poisons the
`NO_VALIDITY` flag onto a chunk that holds nulls; the other poisons the row
count. Both also assert the backend survived. The second arm records a refuted
idea as well: bounding the synthesized bitmap by the row group's byte length
looks right and is wrong, because an elided group of 360 bytes legitimately
needs 12,500 bytes of bits — which is the saving, not a defect. It was measured
when that bound made a plain `SELECT` raise on a correct table.

The second fixture is the load-bearing control. Deleting the bitmap
unconditionally satisfies "a column with no nulls stores none" and loses every
null in the table, so the column that still needs its bitmap is asserted beside
the one that does not -- and the null count is asserted separately, because both
`EXCEPT ALL` arms are satisfied by a table that agrees on values and not on
nulls.

## 58. test_native_index_fetch_stripe_cost.py: the fetch penalty reads the table's own row-group limit

Port of `native_index_fetch_stripe_cost.sh` (#432). The index-fetch penalty prices one
row-group decode, so its size is the limit in force for that table -- the per-table
`stripe_row_limit` option when the owner set one, not only the session GUC (#806).

The property is a response rather than a number: move the option, and the estimate must
move. A cost value would pin this box's cost constants instead.

**The port asserts the plan SHAPE before it compares the two prices, and the shell suite
does not.** Measured on this fixture, PG18 assert build:

| rows matched | limit 2,000 | limit 150,000 |
| --- | --- | --- |
| 1,000 | Index Scan 51.70 | Index Scan 808.98 |
| 20,000 | Index Scan 735.26 | Index Scan 1752.26 |
| 50,000 | Index Scan 1827.50 | Custom Scan 2004.50 |
| 100,000 | Index Scan 3657.00 | Custom Scan 2004.50 |

At the shell suite's 100,000 rows the second arm is no longer an index scan, so the two
numbers it compares are the prices of two different plans. They differ and the check
passes, but a plan flip is one of the things a changed penalty causes, so that comparison
cannot separate "the penalty re-priced this fetch" from "the penalty moved the planner
onto another node". The port matches at 1,000 rows, where both arms stay on the index.

It also carries the attribution cell the original leaves out. With
`enable_index_fetch_penalty = off` the same two arms price identically (35.20 and 35.20
against 51.70 and 808.98), which names the penalty as the term carrying the option rather
than leaving any cost term that reads it equally satisfying.

### Every arm

| test | what it holds |
| --- | --- |
| `test_native_index_fetch_stripe_cost` | every arm: the cost is a real estimate, both arms are the same plan shape and that shape is the index fetch, the estimate moves with the per-table option, and with the penalty off it does not |

## 59. test_scan_decode_cost.py: a scan is priced for the columns it decodes and their width

Port of `scan_decode_cost.sh` (#432). Two defects in one cost term. The custom scan
inherited the heap seqscan cost, whose CPU term is per row, so decoding nine columns was
priced what decoding one was (#503); the fix that followed charged per decoded value,
which left a 324-byte text column charged what a 4-byte int4 column is (#768). #171 bounds
the correction from the other side: raising the full-scan cost must not push a point
lookup off its index.

**The fixtures are smaller than the shell suite's, and the ratios were measured before
they were shrunk.** The shell suite writes 2,000,000 rows twice.

| rows | narrow (1 col) | wide (9 cols) | ratio | insert |
| --- | --- | --- | --- | --- |
| 100,000 | 1250.9 | 5266.0 | 4.210 | 0.08s |
| 200,000 | 2501.8 | 10530.0 | 4.209 | 0.15s |
| 500,000 | 6254.2 | 26322.0 | 4.209 | 0.35s |
| 1,000,000 | 12508.5 | 52645.0 | 4.209 | 0.70s |
| 2,000,000 | 25016.9 | 105288.0 | 4.209 | 1.42s |

The ratio the arm bounds at 1.5 is flat to three decimals across a 20x range, so the extra
rows buy the assertion nothing. 200,000 is kept rather than the cheapest cell because at
the default 150,000-row group limit it spans more than one row group.

The point lookup is the one arm size actually decides, and its boundary was measured
rather than guessed: at 50,000 rows the plan is a Custom Scan and the arm would fail; from
100,000 it is an Index Scan. 200,000 is one doubling of margin.

**What the `#171` arm can and cannot catch**, measured after review. Two mutations, each
x1,000,000:

| mutation | the `#171` arm |
| --- | --- |
| the scan decode charge — this file's own subject | **passes** (`Index Scan`); the #503 ratio moves 4.209 to 16.999 |
| the index-fetch penalty — a neighbouring subsystem | **reddens**: `got 'Custom Scan' want 'index'` |

A dearer scan makes the index more attractive, so no over-charge of the term this file is
about can take a point lookup off its index. The arm is a real guard, but on
`pgcolumnar_index_fetch_penalty`. It is not evidence that the decode charge is bounded
from the other side, and nothing in either harness bounds that today.

### Every arm

| test | what it holds |
| --- | --- |
| `test_scan_decode_cost` | every arm: both scans priced, nine decoded columns cost materially more than one, the widths are in the statistics, four wide text columns cost more than eight narrow int ones, and a point lookup still takes its index |

## 60. test_index_fetch_penalty_width.py: a wider prefix must give up on per-row fetches sooner

Port of `index_fetch_penalty_width.sh` (#432). The penalty's CPU term counted the decoded
prefix's columns, so a 68-byte text column was charged what a 4-byte int4 column was. The
consequence is an inverted ordering, not merely an under-charge: the model let the wide
table fetch about 3x more rows than the narrow one before switching to a scan, when the
wide table can afford about 3x fewer (#803).

`nproj` is the decoded PREFIX length, so width cannot be varied at a fixed prefix inside
one table. Hence two tables of identical shape whose columns differ only in type at each
position.

**The ladder reaches below the shell suite's first rung, and that is a fix rather than a
preference.** `FW <= FN` is satisfied by `FW = 0`, and 0 is what an over-charged width
weight produces as well as what "gives up immediately" produces. The shell ladder starts
at 20 and the wide table's flip point is between 5 and 7 rows, so every rung it tries is
already past it. Measured, penalty on:

| k | rows | `ifw_n` | `ifw_w` |
| --- | --- | --- | --- |
| 1 | 0 | Index Scan | Index Scan |
| 5 | 2 | Index Scan | Index Scan |
| 10 | 5 | Index Scan | Index Scan |
| 20 | 7 | Index Scan | **Custom Scan** |

With four rungs below 20, `FW` is a measured 5 rather than a floor, and the arm gained
`premise: and the wide table fetches by index somewhere on it too`. The shell suite has
the same one-sidedness; it is reported rather than changed here, because this pull
request adds no bash check and so moves no ledger row.

**The row-group limit is a per-table option here, not a cluster setting.** The shell suite
pins `pgcolumnar.stripe_row_limit=20000` in the cluster config, because the writing and
the planning session must not disagree about it (#806). A pytest cluster is shared by
every test in the session, so a cluster-wide setting for one file is not available -- and
is not needed: `set_options(stripe_row_limit => ...)` before the write is durable, belongs
to the table rather than to a session, and is what both the writer and the planner read.
The geometry is then read back from `pgcolumnar.row_group` as a premise (20 groups on each
arm) rather than assumed from the option.

Plan shape is read from `FORMAT JSON` by exact `Node Type` equality rather than by
grepping the plan text, which a property line carrying the same words can satisfy.

### Every arm

| test | what it holds |
| --- | --- |
| `test_index_fetch_penalty_width` | every arm: both fixtures complete, equal row-group counts above the fetch cache, the wide prefix really wider, both prefixes under the cache cap, the penalty is what moves the plan, the narrow table fetches somewhere on the ladder, and the wide table gives up no later |

## 61. test_native_reclaim_cycles.py: repeated compaction must not fight its own free list

Port of `native_reclaim_cycles.sh` (#432). `PgColumnarAllocateFreeSpace` consumed a
`free_space` row without a `CommandCounterIncrement`, so a `compact_rewrite` that
allocated more than once in one command re-selected the row it had just consumed and
died with "tuple already updated by self" (#84).

**The port catches that defect and the shell suite does not** (#1138). Delete the fix and
the shell suite still reports 12 passed / 0 failed. The cause is
`pgcolumnar.reclaim_coalesce`, which defaults ON: compaction merges adjacent freed
ranges, so the free list holds one or two rows however much is freed, and one command
never allocates from it twice. Measured on the shell suite's fixture, `free_space` rows
before each cycle: 0, 1, 2, 2, 2.

This file runs its cycles with coalescing off, which keeps the ranges separate, and
asserts the free list is fragmented before relying on it. Two cells, each built from its
own source and printing its own `.so` hash:

| build | result |
| --- | --- |
| fix present (`.so e95880e45673`) | 30 groups, free list steady at 18, clean cycles |
| fix removed (`.so 42bb17933a55`) | `compact_rewrite cycle 1 returns a count (no self-conflict): got 'bad:tuple already updated by self'` |

Row sets are compared as sorted tuples in Python rather than through `pgc_set_hash`, so
the two harnesses stay independent by construction.

### Every arm

| test | what it holds |
| --- | --- |
| `test_native_reclaim_cycles` | every arm: the fixture's groups and its fragmented free list, initial parity, then per cycle that `compact_rewrite` returned a count and parity held, and that the file reaches a steady state |

## 62. test_native_reclaim_frag.py: coalescing must merge, stay correct, and cost nothing

Port of `native_reclaim_frag.sh` (#432). The same retire+recluster workload runs with
`pgcolumnar.reclaim_coalesce` on and off. Both modes must agree on the data while
disagreeing on the free list: fewer `free_space` rows with coalescing on is the direct,
deterministic evidence that the coalesce path ran. Measured: 1 row on, 18 off.

The original wraps its whole build-and-compact sequence in `{ ... } >/dev/null 2>&1`, so
a failed `INSERT`, a refused `set_options` or an errored `compact` leaves exactly the
state a successful one does, and the arms below would compare two empty tables and
report parity. The port asserts the row count per mode before anything is compared, so
"both modes agree" cannot be satisfied by both modes having done nothing.

### Every arm

| test | what it holds |
| --- | --- |
| `test_native_reclaim_frag` | every arm: the row counts per mode before and after the block delete, parity with the heap mirror in both modes, that coalescing leaves fewer free_space rows, and that it never costs file size |

## 63. test_native_vacuum_race.py: compaction must not drop a concurrent commit

Port of `native_vacuum_race.sh` (#432). `pgcolumnar_compact_relation` used the caller's
pre-lock statement snapshot, so a row group committed after that snapshot was invisible
to the rewrite and was destroyed by the relfilenode swap (#295). Covered for `vacuum()`,
`vacuum_sorted()` and `cluster()`.

**The shell suite races on the clock; this one does not.** The original backgrounds a
`psql` running `SELECT pg_sleep(5)` inside its transaction, sleeps 2 in the shell, and
inserts during the gap — and redirects to `/dev/null` the one observation that would
prove the race happened. If session B's snapshot were taken after A's commit, B would
simply see all 150 rows, the maintenance call would keep them, and every check would
pass having tested nothing.

The port uses two connections and no sleeps, and asserts the interleaving: B sees 50
before A commits, and **still** sees 50 after. The second premise is the load-bearing
one — it says A's rows are genuinely outside B's snapshot, which is the precondition for
the defect and exactly what a timing slip destroys.

Removal proof: take the caller's transaction snapshot instead of a fresh post-lock one,
and `vacuum()` destroys 100 of 150 rows — the arm reports `got 50 want 150`.

The three maintenance calls are driven from an inline literal loop table, because an
f-string name would reach `compare_to_bash` as one template and could not match the
three literal names the shell suite states.

### Every arm

| test | what it holds |
| --- | --- |
| `test_native_vacuum_race` | every arm: per maintenance call, that B pinned a snapshot of 50 and that A's commit stayed outside it, then that all 150 rows survive; and that `vacuum()` preserves the exact row set rather than only the count |

## 64. test_native_delete_vector_index.py: reading the delete vector uses its index

Port of `native_delete_vector_index.sh` (#432). `PgColumnarReadDeleteVectorList` and its
siblings passed `InvalidOid` / `indexOK=false` to `systable_beginscan`, so every call
sequentially scanned `pgcolumnar.delete_vector` filtered by scan key — once per row group
while a scan builds its liveness cache.

**The count is scoped to this table's storage, which is not a style choice.** The shell
suite counts `pgcolumnar.delete_vector` whole, against a cluster it created moments
earlier. A pytest worker shares one cluster across the corpus and that catalog is
database-wide, so unscoped the arm would pass or fail on test ORDER — reproducible only in
a suite run.

**The measured scan runs on a second connection, and that is load-bearing (#1146).** The
shell suite sends every statement through its own `psql`, so the scan it measures is
always made by a session that did not write. Measured on this fixture:

| the scan runs in… | `idx_scan` | `seq_scan` |
| --- | --- | --- |
| the session that wrote | 62 | **20** |
| a fresh session | 21 | 0 |

Twenty sequential catalog scans, one per row group, survived in the writing session, and
#1146 fixed that. `PgColumnarUpsertDeleteVector` ran once per row group passing
`InvalidOid`; attributed with an elog probe at every `delete_vector` scan site, 20 were
that call site against `seq_scan=20 idx_scan=41`, and afterwards `seq_scan=0 idx_scan=61`.

This file now asserts BOTH sessions. The second test holds the connection across the
DELETE and reads the counters as a delta over it, which is the only way the writing-session
path is observable at all: every `q` in the shell harness is its own psql, so the scan it
measures is always made by a session that did no writing.

### Every arm

| test | what it holds |
| --- | --- |
| `test_native_delete_vector_index` | every arm: the fixture's rows and row groups, one delete_vector row per group scoped to this storage, that the cache-building scan ran and was correct, that it used the index and did not sequentially scan, and that the deletes are still applied |
| `test_the_writing_session_does_not_sequentially_scan_the_delete_vector` | the session that DID the deleting: the fixture's row groups, that the DELETE wrote one delete_vector row per group so the zero is not vacuous, that it reached the catalog at all, and that it took no sequential scan |

## 65. test_native_delete_visibility_paths.py: a deleted row is invisible on every path

Port of `native_delete_visibility_paths.sh` (#432). The delete-vector fold and the per-row
bit test are single-sourced in `pgcolumnar_merge_delete_vectors` and `dv_row_deleted`, and
several independent paths consult them.

**The shell suite names four paths and takes two.** None of the `enable_*` scan GUCs
governs `Custom Scan (PgColumnarScan)`, so three of its arms plan the same node:

| arm, as the shell suite runs it | plan actually taken |
| --- | --- |
| sequential scan (row-emitting path) | Custom Scan |
| index / bitmap scan | Custom Scan |
| index-only scan | Custom Scan |
| aggregate over the whole table | Custom Scan (vectorized aggregate) |

The property is true — each returns 4286 — but three arms are one piece of evidence
counted three times. `pgcolumnar.enable_custom_scan = off` is the switch that moves it,
and with it each named path is reachable and still correct: Seq Scan, Index Scan and Index
Only Scan all return 4286. This port forces the path each arm is named for and **asserts
the node before reading the count**.

It also asserts what the shell suite assumes in a comment: that the deletes span more than
one row group, since each group builds its own mask; and that the rolled-back delete left
its row in place, so the in-transaction arm cannot be satisfied by a delete that persisted.

### Every arm

| test | what it holds |
| --- | --- |
| `test_native_delete_visibility_paths` | every arm: the row count and group span, then the live set through a Seq Scan, an Index Scan, an Index Only Scan and the columnar aggregate — each with its plan asserted — then one deleted and one live row through the index, and a delete read back inside its own transaction |

## 66. test_temporal.py: a columnar table enforces temporal constraints like a heap

Port of `temporal.sh` (#432). PostgreSQL 18 adds `WITHOUT OVERLAPS` primary keys, 19 adds
`UPDATE ... FOR PORTION OF`, and both run through the index and constraint machinery
pgColumnar integrates with. The suite builds a heap/columnar pair and asserts they agree.

**This is the first pair unblocked by #1131.** `temporal.sh` gates on `btree_gist` — a
`WITHOUT OVERLAPS` key needs a GiST index over the scalar part — and records that refusal
under a NAME. Until `expect.cannot_run` could carry a name the string was inexpressible
and the pair reported `missing: 1` however faithful the rest was.

**Two refusals, and only one of them is a property.** The version refusal is deliberately
UNNAMED: on 15, 16 and 17 the shell suite prints a note, calls `pgc_summary` and exits
without recording anything, so there is no name to match and an unnamed `cannot_run`
states none. The `btree_gist` refusal is named, because the shell suite names it.

**One arm the original does not have.** Both PK arms are satisfied by a table that
rejected everything, including the rows that should have gone in — and two empty tables
compare equal, so the hash comparison that follows has the same hole. The port asserts the
accepted rows are present before comparing.

Removal proof: there is no pgcolumnar-specific temporal code to delete, since the suite
guards an integration with core's constraint machinery. The mutation is at the only seam
that expresses "columnar stopped enforcing it" — drop the constraint from the columnar
table alone, and `overlapping insert rejected (columnar)` reddens with `got 'ok' want
'err'` while the heap arm stays green.

### Every arm

| test | what it holds |
| --- | --- |
| `test_temporal` | every arm: heap and columnar agree on accepting non-overlapping rows and rejecting an overlapping one, the accepted rows are really there, the PK contents match, and on 19 the FOR PORTION OF update and its result set match |

## 67. test_native_parquet_dict_oob.py: a crafted dictionary index must not read out of bounds

Port of `native_parquet_dict_oob.sh` (#432). The RLE_DICTIONARY path bounds-checked a
file-controlled index with a SIGNED comparison, so an index with the high bit set
sign-extends to a negative int, slips past the check, and reads about 16 GB past the
dictionary. One crafted file, readable by anyone who may call `read_parquet`, took the
cluster down with SIGSEGV.

**The gate carries the shell suite's name**, which #1131 made possible: `pgc_skip fixture
"dictionary-index OOB fixtures are missing"` records under that name, and before #1131 a
port could emit only the reason code.

**The fixtures are copied, not read in place.** The server runs as the `postgres` OS user
while the checkout belongs to whoever cloned it, and `read_parquet` opens the file as the
server — so a fixture left in the source tree is a path the backend may not be able to
read, and that failure would look exactly like the rejection this suite exists to prove.

Removal proof — restore the signed comparison:

```
the crafted file answered: consuming input failed: server closed the connection unexpectedly
attack: the out-of-range dictionary index is rejected with an error: got 'no (...)' want 'yes'
```

On a real crash it is the rejection arm that reddens and the two arms after it never run,
because the connection they would use is gone. They are not redundant: an out-of-bounds
read that lands on mapped memory returns garbage without raising and without dying, and
then the rejection arm fails while those two pass — a quieter defect, and asserting all
three is what tells the two worlds apart in the report.

### Every arm

| test | what it holds |
| --- | --- |
| `test_native_parquet_dict_oob` | every arm: the benign file still reads its four rows, the crafted index is refused with an error, the backend is still alive afterwards, the server log exists, and it records no segfault |

## 68. test_advisory_lock_class.py: no lock an insert takes may be reachable from SQL

Port of `advisory_lock_class.sh` (#432). `locktag_field4` says which advisory space a tag
belongs to, and PostgreSQL's own functions own exactly two values: 1 for the int8 key form
and 2 for the two-int4 form. A lock the extension takes in either shares a space with
anything a user can take from SQL, and a user lock on the same tag then blocks a columnar
insert forever — which was #430.

**The port asserts a stronger property than the original, because the original's is
unfalsifiable at the moment it matters (#1154).** The shell suite discovers "the lock the
insert took" as `ORDER BY objsubid DESC LIMIT 1`. An insert takes more than one:

| build | locks held by the inserting session |
| --- | --- |
| fixed | `[(16572, 77, 103), (2, 1410065408, 102)]` |
| unique-key class regressed to 2 | `[(2, 1410065408, 102), (16572, 77, 2)]` |

The maximum is the unique-key lock only while the unique-key lock is the highest-numbered
one. Regress it and the maximum becomes 102, STORAGE_ROW, which is still unreachable — so
the suite reports the property holding while the lock under test sits in the SQL space,
and the later arm contends for the wrong tag and passes too.

This file collects **every** advisory lock the insert holds and asserts none is in a
SQL-reachable class, and contends for every addressable tag rather than one. Under the
same mutation it reddens naming the offender:

```
the lock an insert takes is not in a SQL-reachable class:
    got 'reachable [(16572, 77, 2)]' want 'unreachable'
```

**No sleeps and no polling.** The shell suite backgrounds a `psql` running `pg_sleep(30)`,
polls `pg_locks` up to sixty times for the lock to appear and again for it to go, and must
`pg_terminate_backend` rather than kill the client — its own comment records that killing
the client left the server holding the transaction and "reported the same result with and
without the fix". With real connections a second session's `INSERT` returns when the lock
is held and closing the connection ends the transaction, so there is no window to wait on.

**The skip is per test.** `cannot_run` declares a whole test unrunnable while `check_skip`
skips one check, so the SQL-form arm is its own test and the duplicate-key arm runs
regardless.

### Every arm

| test | what it holds |
| --- | --- |
| `test_the_lock_an_insert_takes_is_not_in_a_sql_reachable_class` | the lock is enabled, the insert's locks are visible, none is SQL-reachable, and the discovering session leaves nothing held |
| `test_a_user_advisory_lock_on_that_tag_does_not_block_an_insert` | a user session holding every addressable tag in class 2 does not block a columnar insert |
| `test_a_duplicate_key_is_still_rejected` | the lock still does its job, so removing the collision by removing the lock does not pass |
## 69. test_index_am_support.py: the index methods the page claims must work

`docs/features.md` said "`CREATE INDEX` builds btree and hash indexes over a
columnar table" from the day it was written. GiST and SP-GiST build over one too,
and answer their operators, and a reader of that sentence concluded the opposite
and asked whether range columns were supported at all. They are. The sentence was
wrong by omission, and nothing could see that it was.

**THIS FILE READS THE DOCUMENT RATHER THAN CARRYING A LIST.** The precedent is
`doc_parallel_premise`, which extracts the published query out of
`limitations.md` for the same reason: a suite holding its own copy of a claim
cannot see the page drift away from it. The access methods are taken from the
backticked names in that sentence, and every name found is exercised.

So the failure modes are proven by mutating the DOCUMENT rather than the code:

| mutation | result |
| --- | --- |
| add `gin` to the sentence, with no probe for it | `premise: gin, which the page claims, has a probe in this suite` fails |
| remove every method name from the sentence | `premise: the features page names at least one index access method` fails; the loop runs zero times and cannot pass vacuously |
| **`builds btree, hash and gist but NOT spgist`** | `premise: the claim bullet holds the claim and nothing else` fails, in both harnesses |
| **the denial written after `columnar table`** | the same premise fails, in both harnesses |
| **`...though spgist cannot be chosen`** | the same premise fails — see below |
| **legitimate prose added to the claim bullet** | the same premise fails, on purpose |

**THE LAST TWO ARE THE ONES THAT NEARLY SHIPPED, and @OffgridwithJD wrote them.**
A name-harvesting extractor reads `NOT spgist` as a claim that SP-GiST is
supported. The page would then say a method does not work while this file proves
it does, by name, and reports green -- worse than no coupling at all, because the
tracker says the page is pinned so nobody re-reads it.

The second edit was subtler and defeated something else: the two halves had
different windows. The shell stopped at `columnar table` and this file's regex
stopped just before it, so a denial written after that phrase gave four names on
one side and three on the other. Both passed. They disagreed. Nothing compared
them. Independence buys two sets of blind spots, not none, and the fix is that
both halves now read the same unit -- the whole list item.

**THE CLAIM IS WHITELISTED, AND THE FIRST ATTEMPT WAS A DENYLIST THAT LASTED ONE
ROUND.** The version that shipped to review refused a window containing `not`,
`never`, `except`, `unsupported`, `rather than` or `but no`. @OffgridwithJD broke
it with one more edit:

    ...and `gist` but NOT `spgist` indexes        refused, correctly
    ...though `spgist` cannot be chosen           PASSED

`\bnot\b` has no word boundary before the `not` inside `cannot`. Their own
correction is the reason the fix is not another token: **a denylist of negations
is the losing game they had told me to avoid**, one round at a time — `isn't`,
`no longer`, `save for`, and eventually a sentence that inverts the claim with no
negation token in it at all.

So the bullet has exactly ONE legitimate form, and anything else is refused:

    - `CREATE INDEX` builds `a`, `b` and `c` indexes over a columnar table.

Prose added to that bullet is refused too, **even when it is perfectly true**,
and the refusal asks for its own sentence. The page gives it one: the note about
this suite reading the list now lives in the next bullet, where it cannot change
what the claim says.

**AND THE FIRST WHITELIST BROKE A RULE THIS FILE'S HEADER CITES.** Matching the
form with `claim_line | grep -qE ...` is exactly the shape `selftest/080` refuses
(#486): `grep -q` exits as soon as it has its answer, the writer takes EPIPE, and
under `pipefail` the pipeline reports the pattern ABSENT whatever the string
held — always in the direction that sends someone hunting a defect that is not
there. `harness_selftest` caught both instances, which is the answer to why that
rule is a suite rather than a convention: the header of this very file cites it,
and the code two functions below it did it anyway. The match is a `[[ =~ ]]` test
with no pipeline now.

**THE HOLE IT CANNOT CLOSE IS NAMED RATHER THAN LEFT IMPLICIT.** The pair reads
ONE bullet, so an exclusion written in a NEIGHBOURING bullet — "spgist is not
supported", on its own — contradicts the claim and nothing here sees it.
@OffgridwithJD found the sharp version: the refusal message says *"split the
claim and the exclusion into separate sentences"*, and an author who follows that
instruction exactly lands in the one arrangement the guard is blind to.

Widening the window is not the answer, because every unrelated edit to the
section would become a refusal. So the page says where an exclusion belongs
instead — `docs/limitations.md`, with its own evidence — and the refusal message
says so too. **A guard that steers authors into its blind spot is worse than one
that admits to having it.**

**THE FORM PREMISE IS ASSERTED BEFORE THE NAMES PREMISE**, because it is the
cause and the other is the consequence: a bullet neither half can read yields no
names. pytest stops at its first failing assertion, so putting the cause second
would have reported only "the page names nothing" and left the reader to work out
why.

**THE SCAN PATH IS THE ORACLE, NOT A LITERAL.** Each method's answer is compared
against the same query run with every index path disabled. A literal would pin
this fixture; the property is that the two paths agree. A predicate matching no
row would satisfy that trivially, so each method also asserts its predicate
matches something.

**WHY THE RANGE METHODS ARE ON THE PAGE AT ALL.** Overlap and containment never
prune chunk groups on a scan: the scan resolves an operator through the column
type's btree family and takes the five btree strategies, while `&&` and `@>`
belong to GiST families. So for a range column the index is not an optimisation,
it is the only fast path, and the last arm asserts the plan rather than a timing.

**GIN AND BRIN ARE DELIBERATELY ABSENT.** Both build on a columnar table. Neither
has been observed in a plan the planner chose, and building is not the same as
being usable, so neither is claimed on the page or probed here. Adding either to
the page without a probe turns this file red, which is the point.

Independent of `test/index_am_support.sh`: the same seams -- the document,
`CREATE INDEX`, and the two plans -- but its own cluster, its own table name, its
own row count, and the methods extracted with a Python regex rather than with
awk. Neither file names the other.

### Every arm

| arm | what it holds |
| --- | --- |
| `premise: the features page names at least one index access method` | the extractor found something, so a green loop means work happened |
| `premise: the claim bullet holds the claim and nothing else` | the bullet is the one form a name-reader can read, so the names ARE the claim |
| `premise: the fixture holds every row` | the corpus is what the probes assume |
| `premise: <am>, which the page claims, has a probe in this suite` | a claim on the page without evidence here is a failure, not a skip |
| `<am> builds an index over a columnar table` | the method builds |
| `premise: the <am> predicate matches rows at all` | the comparison below is not two empty sets agreeing |
| `<am> answers its operator with the same rows the scan returns` | the index path and the scan path agree |
| `premise: an overlap query reaches the row through a GiST index` | the plan, asserted, for the claim that ranges have an index path |

### Every test

The arms above are the pair's, and they are the same on both sides. The pytest
half spends them all in one function, because the page is read once and every
method it names is probed in the loop that reading produces.

| test | what it holds |
| --- | --- |
| `test_every_access_method_the_page_claims_builds_and_answers` | the whole pair, in one function: the claim bullet is refused unless it is the one readable form, the page named at least one method, and each method named builds an index and answers its operator with the rows the scan returns |

**THE NAMES PREMISE COUNTS, it does not flag.** It reads
`expect.at_least(len(ams), 1, ...)` rather than a `"named"` string, for two
reasons. The shell twin already counts, with `wc -l`, so the counted form is the
faithful one. And `test_loop_coverage_premise.py` recognises a cardinality premise
only as `len(...)` inside a counted assertion outside the loop, so the string
form left the loop below reading as unpremised.

## 70. test_analyze_differential.py: our statistics must have the shape core writes

Port of `analyze_differential.sh` (#432). `pgcolumnar.analyze()` writes through
`pg_restore_attribute_stats`, which takes `VARIADIC "any"` and validates each
argument's type at run time. **A mistyped argument is not an error.** The function
emits a WARNING, sets that argument to NULL, and returns cleanly having stored
nothing.

**Values cannot be the oracle here, and that is why the pair exists.**
`pg_restore_attribute_stats` leaves kinds it was not given in place, which is
correct and fatal to attribution: a statistic we failed to write is still in the
catalog wearing core's shape, so every value assertion reads core's work and
reports it as ours. So core ANALYZE is the oracle and the comparison is over
SHAPE — for each statistic kind written for a column, the operator and the
collation stored beside it must match what core stored for the same kind of the
same column. A dropped argument leaves the slot absent, which a shape comparison
sees at once.

**The refusal is named, and that is #1131.** On 15, 16 and 17 the shell suite
declines through `check_skip`, which RECORDS the refusal under the name "the
differential analyze path". Until `expect.cannot_run` could carry a name no port
could emit that string, and `compare_to_bash` reported it MISSING however faithful
the rest was. The precondition — the server major — exists on both sides, which is
the case resolution 1 unblocks. On 15 and 17 the pair runs one check and declines,
and the run exits 67.

**One arm the shell reaches only on failure.** `analyze_differential.sh` names
"could not read the server major, so the gate below cannot be trusted" inside a
`pgc_fail` that fires only when the version is unreadable. The port asserts the
same property on every run, under the same name, so the gate below it is known to
have been decided on a real number rather than on an empty string that compared
less-than 18.

**Independent of the shell suite at every seam.** The shell materialises an
`ad_shape` table, because each `q()` opens its own connection, and pairs
kind/operator/collation with `unnest ... WITH ORDINALITY` joined three ways; this
file holds one connection, reads the five slots as three arrays, and zips them in
Python. The shell greps psql's output for `WARNING`; this file registers a psycopg
notice handler, so the fact the suite is named for is observed through two
unrelated channels. Frequencies are checked one most-common value at a time
through a parameterised count, so a failure names the value that disagreed rather
than counting disagreements.

**The element type of the stored array is deliberately not compared**, on both
sides. `pg_statistic.stavalues1` is declared `anyarray`, so `pg_typeof` returns the
constant string `anyarray` for every row ever stored. A probe built on it reports
every slot as mismatched — core's own included — so it tests the expectation rather
than the code.

### Removal proof

Three mutations of `pgcolumnar--1.0-alpha4.sql`, each asserted to have matched its
anchor exactly once and to have been restored byte-identical. The control is 27
checks passed.

| cell | mutation | what reddens |
| --- | --- | --- |
| B | `mcvfreqs::real[]` → `mcvfreqs::float8[]` | `and without a WARNING, which is how pg_restore_attribute_stats drops an argument`, got 10 want 0 — five columns, each warning twice, because the pair rule drops `most_common_vals` with it |
| C | frequency divided by the non-null count instead of the row count | `every most-common value of i exists with exactly its stored frequency`, got 2 want 0 |
| D | `'histogram_bounds', hist::text` → a typed NULL | `and a histogram for exactly the four core gave one`, got 0 want 4 |

Cell B is the failure mode the suite is named for: the call succeeds, nothing is
raised, and the statistic is simply missing. It is caught by the notice handler
before any later arm runs, which is why only one arm appears in that row.

### Every test

| test | what it holds |
| --- | --- |
| `test_analyze_differential` | the whole pair in one function, because the shell suite gates the whole of itself on the major: the fixture loaded and is genuinely nullable, both awkward text values are present, core wrote a most-common list for all five columns and a histogram for the four with a tail, every statistic was cleared before we wrote, `pgcolumnar.analyze()` raised nothing and warned about nothing, we wrote the same kinds for the same columns carrying the same operator and collation, every most-common value exists with the frequency stored for it, the comma and the quote survived the round trip, and boolean got no histogram |

## 71. test_native_groupagg.py: the grouped vectorized aggregate must answer what core answers

Port of `native_groupagg.sh` (#289, #432). The grouped path fires for
`SELECT <keys>, agg(col) ... [WHERE ...] GROUP BY <keys>` over one columnar
relation, and scatters each surviving row into an open-addressing hash table whose
per-group accumulators fold in scan order.

**Two oracles, and neither is the other's substitute.** A heap mirror holds the
same rows, so exact aggregates must agree byte for byte — but it cannot judge float
summation order, because the two storage types do not scan in the same order and
float addition is not associative. A toggle differential runs the same query over
the SAME columnar rows with the path off and on; both arms read in the same order,
so even float sums must be identical. That is what validates the order-preserving
accumulators.

**Every comparison carries a node premise**, and that is the whole reason the file
is shaped this way. A query the node quietly rejects runs the scalar Agg in BOTH
arms, so the comparison is two identical runs agreeing with each other. That is how
`sum(real)` returning 0 got through an earlier version of the shell suite.

**The collation arms are their own test.** `check_skip` skips one check;
`cannot_run` declares a whole TEST unrunnable. Putting the ICU-gated pair in a
function with anything else would make the refusal wider than the shell suite's.

### The one arm that differs from the shell suite, and why (#1162)

`native_groupagg.sh` asks ten aggregates over ten DIFFERENT columns to cost more
than one over one. A scan projecting ten columns costs more than one projecting one
whatever the folding charge is, so **the arm passes against the exact defect #349
fixed.** Measured, with the folding charge stripped of its `naggs` factor:

| shape | control | mutant |
| --- | --- | --- |
| 1 aggregate over 1 column | 350.50 | 350.50 |
| 10 aggregates over 10 columns (what the shell suite asks) | 1253.01 | **803.01** |
| 10 aggregates over 1 column, path forced | 800.50 | **350.50** |

This file holds the projection fixed — ten `avg(a)` over one column — and forces
the path, so both arms really are the grouped node. The difference is then exactly
the term under test: 450.00 = `cpu_operator_cost` × 20,000 rows × 9 extra
aggregates, and 0.00 under the mutation. Forcing matters: with the charge applied
the planner prices that shape OUT of the grouped path, so without
`enable_hashagg=off; enable_sort=off` the comparison silently changes which node it
reads.

Two premises come with it, which the shell suite does not have: both costed plans
are asserted to BE the grouped node, so a planner change that stopped choosing it
cannot leave the arm comparing core's Agg with itself.

### Independent of the shell suite at every seam

| | shell | port |
| --- | --- | --- |
| the node premise | `grep` `EXPLAIN` text for the marker line | `EXPLAIN (FORMAT JSON)`, asking whether a node carries the PROPERTY, through `plan_marker`, which refuses an empty plan |
| the oracle | `md5(string_agg(...))` computed by the server | sorted row tuples compared in Python, so a disagreement prints rows |
| the GUC | set on the DATABASE, because each `q()` opens a connection | set on the SESSION, which holds one |

### Removal proof

Four mutations of `src/columnar_vector.c`, each asserted to have matched its anchor
exactly once and to have been restored byte-identical, on the Debian PostgreSQL 18
the CI cluster job uses. The `.so` hash is recorded per cell, because a mutation
that never reached the binary is the failure a red arm cannot show.

| cell | mutation | `.so` | what reddens |
| --- | --- | --- | --- |
| A | control | `37d535c9dd69` | 85 pass |
| B | the deterministic-collation gate on a group key | `77a1d36c89a0` | `plan: non-deterministic collation key falls back` — the key is present and should not be |
| C | the folding charge stops scaling with the aggregate count | `f9dc2a9d5faa` | `ten aggregates cost more than one (#349)` — `1=350.5 10=350.5` |
| D | the group-estimate bound is never applied | `7d25ab46e1d9` | `the bound is accurate for a truncating time key, not just smaller` (got 299940 want 12) **and** `while a matching-type predicate still gets one` |
| E | the pseudoconstant (one-time) qual rejection | `f0ef8381416d` | `regress B2: gating WHERE falls back (not the node)` |

### The ICU refusal, and where it does and does not fire

`test_a_non_deterministic_collation_key_falls_back` needs a non-deterministic ICU
collation. Measured rather than assumed:

```
/usr/local/pg{15,16,17,18,19}a   --without-icu   the test declines, run exits 67
/usr/lib/postgresql/18           --with-icu      the test runs, 85 checks, rc 0
```

The second is the `pg_config` the `pytest (cluster tests)` job is given, so **CI
runs these arms rather than declining them.** The five-major local gate exits 67 on
every major for this pair, which is the designed third state and not a failure.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_grouped_node_is_chosen_for_the_shapes_it_supports` | a text key, an hour expression and the q4 shape all plan as the grouped node, and the GUC off plans an ordinary Agg |
| `test_the_grouped_path_agrees_with_a_heap_mirror` | eight exact-aggregate shapes, each with the node asserted first, against the same rows in a heap table |
| `test_the_mirror_still_agrees_after_deletes_and_an_added_column` | the mirror and the toggle both survive a delete and an `ADD COLUMN ... DEFAULT` |
| `test_the_accumulators_fold_identically_with_the_path_off_and_on` | the float and average accumulators, and the exact ones, byte-identical off versus on |
| `test_min_max_keep_the_display_scale_core_keeps` | numeric values equal by value and differing in display scale keep the later one on a tie |
| `test_a_lone_negative_zero_keeps_its_sign` | summing `-0.0` prints `-0`, which folding into `+0.0` would lose |
| `test_a_group_count_over_the_cap_stops_rather_than_growing` | over the cap the node errors naming `groupagg_max_groups`, and the default cap runs the high-cardinality key correctly |
| `test_an_output_built_on_a_key_falls_back_and_still_answers` | `f(key)` output and a bare `GROUP BY` with no aggregate both fall back, and the answer is still right |
| `test_a_non_deterministic_collation_key_falls_back` | the fallback happens and the grouping is case-insensitive; declines under the shell suite's own name where ICU is absent |
| `test_sum_real_matches_heap_rather_than_returning_zero` | blocker 1: the node fires, matches heap, and is not zero |
| `test_a_gating_where_is_honoured_rather_than_dropped` | blocker 2: a one-time filter makes the node decline, and both the false and true gates answer as heap does |
| `test_avg_float8_overflows_the_way_core_overflows` | the node fires and raises out of range, which a row comparison cannot express |
| `test_degenerate_inputs_produce_no_groups` | a predicate matching nothing, and an empty table |
| `test_the_path_pays_for_the_folding_it_does` | the isolated #349 arm and its two node premises |
| `test_the_group_estimate_bound_is_accurate_and_narrow` | the bound is 12 and is an upper bound; an informed estimate is untouched; no range, a lookalike function and a non-time key each get no bound |
| `test_a_mixed_timestamp_predicate_gets_no_bound` | a cross-type predicate under a non-UTC TimeZone gets none, while the matching-type one still gets a bound |

## 72. test_analyze_function.py: statistics collected by reading, not by sampling

Port of `analyze_function.sh` (#414, #432). Core ANALYZE samples 30,000 rows, and on a
table of any size those rows are spread across every row group, so every group is
decoded for every column. `pgcolumnar.analyze()` reads ONE column instead, and the
statistics that come out of a full read are EXACT where core's are estimates.
**Exactness is the observable a sampled implementation cannot fake**, which is why it
is what gets asserted.

### The refusal carries the shell suite's name, and only once (#1131)

On 15, 16 and 17 `analyze_function.sh` declines through `check_skip`, which RECORDS
the refusal under the name `pgcolumnar.analyze()` — the statistics are written through
`pg_restore_attribute_stats`, which core added in 18.

This file is FOUR tests, split by fixture so only the first pays the 500,000-row
build. That has a consequence for the refusal: the shell prints ONE `check_skip` and
exits, so **exactly one test names it and the other three decline UNNAMED**. An
unnamed `cannot_run` states no property, which is right — naming all four would
publish three checks the original does not have.

Verified at RUNTIME, not just in source, by spying on `Expect._record` — because
`compare_to_bash` grades the NAME and proves the string is PRESENT, never that
anything emits it:

```
PG18   56 bash names, 55 emitted   (the refusal does not fire above 17)
PG17   verdict UNRUN: 'pgcolumnar.analyze()'
```

### Every fixture is built to defeat a specific false pass

* `k` is one row in ten NULL. **0.1 is a number a sampler reaches whenever it is
  lucky**, so `k` alone cannot discriminate.
* `k7` is one row in SEVEN NULL, so the truth is 71428/500000 = 0.142856. Core's
  estimate is always a whole number of sampled rows over 30,000, and there is no whole
  `k` with `k/30000 = 0.142856` — it would need 4285.68. **Core cannot report this
  fraction whatever it draws**, so the discrimination does not depend on luck.
* `skew` holds 1,000,000 in exactly one row of 500,000 and under 100 elsewhere.
* `cat` has three repeated values, a unique tail and one row in ten NULL, so dividing
  frequencies by the non-null count instead of the row count gives 0.2/0.12/0.06 —
  three individually plausible numbers that are all wrong.

### What is printed rather than asserted

Whether core's sampled number happens to land on the truth is a fact about a random
draw, not about this extension. The shell suite retracted two such gates (#475, #487)
after one failed a correct suite about one run in 130. This file prints the same
figures and asserts only against independently counted truth.

### Independent of the shell suite

The shell greps psql's output for ERROR and WARNING; this registers a psycopg notice
handler and catches `psycopg.Error`. The shell parses the documentation table with
`awk` bounded at the next `###`; this parses the same section with a regex bounded the
same way, and for the same recorded reason — a `sed` range ending at `/^## /` once ran
past and compared against seventeen names from three sections.

### Removal proof

Three mutations of `pgcolumnar--1.0-alpha4.sql`, on the Debian PostgreSQL 18 the CI
cluster job is given. Each asserted its anchor matched exactly once and that the file
changed; each restored byte-identical. Control: 55 checks passed.

| cell | mutation | what reddens |
| --- | --- | --- |
| B | the per-column target replaced by the global default | `the histogram honours the column's statistics target, not the global default` (got 101 want 11) **and** `histogram_bounds are core's positional stride` (got `{1,2,...,11}` want `{1,4,7,11}`) |
| C | the `SET STATISTICS 0` skip disabled | `pgcolumnar.analyze() ran for the zero-target column without raising` — got `22012: division by zero` |
| D | the null fraction divided by the non-null count | `reports null_frac exactly` (got 0.11111111 want 0.1) **and** `null_frac counts live rows, not rows a DELETE left behind` (got 0.153846 want 0.133333) |

**THE `.so` HASH IS NOT THE INSTRUMENT HERE, and printing it would be worse than
omitting it.** These mutations are in the extension SQL, so the C is unchanged and the
library hashes identically in all four cells — four identical hashes would look like
evidence and be none. What establishes that the mutant reached the server is the
`.sql` md5 moving per cell, and that `.sql` is in `pgc_fingerprint`'s `ROOT_SUFFIXES`,
so the build fingerprint moves and `build_once` reinstalls. Verified rather than
assumed: `542af67fbc25` unmutated, `1cd8f666717b` mutated, `542af67fbc25` restored.

### Every test

| test | what it holds |
| --- | --- |
| `test_analyze_function` | the 500,000-row fixture: null_frac exact on a fraction core's sample cannot express, n_distinct exact from one column, the statistics it does not compute left in place, the histogram's true maximum and its smallest non-most-common bound, no most-common value inside the histogram, every bound a value the column holds, the three MCV frequencies over total rows rather than non-null rows, the per-column statistics target honoured, and a column at `SET STATISTICS 0` left alone |
| `test_histogram_bounds_are_a_positional_stride` | eleven distinct rows at target 3, where core's `values[floor(i*(nv-1)/(nhist-1))]` and `percentile_disc`'s `ceil(p*nv)-1` land on different values; the expectation comes from an independent oracle AND a hand-worked figure that must agree first |
| `test_null_frac_counts_live_rows_not_ones_a_delete_left` | #485: null_frac came from the zone maps, which count what was WRITTEN, so one pg_stats row carried two statistics normalised against different populations; both must imply the same table |
| `test_the_documented_statistics_are_the_ones_written` | the list in `docs/sql-reference.md` parsed and compared against what the function populates, plus the two negatives the doc states in prose |


## 73. test_assertion_carries_its_measurement.py: a failure must say what it measured

    expect.num(1 if abs(on - off) <= 5 else 0, 1, "...")   ->   got 0 want 1

Four measured buffer counts, reduced to a boolean before the assert. The failure
message carries none of them, so the red says only that the arm failed — which the
word FAILED already said.

**WHAT IT COST, MEASURED (#1164).** A PG 15 leg reddened one arm of
`sorted_pathkeys`, and two sessions spent an afternoon unable to say whether the
ORACLE (`|on - off| <= 5`, a six-buffer spurious gap) or the CONTROL
(`order_on > order_off + 5`, a fourteen-buffer collapse) had failed. Those are
different defects with different owners. Four further experiments were aimed at a
target whose identity was unknown, and it is still unknown.

**TWO BUGS, ONE SYMPTOM.** The runner also captured each leg into a shell variable
and grepped it for summary lines, so the full output never reached disk. That was
repaired separately. **Repairing it alone would still have left `got 0 want 1`**,
because the measurement was discarded before the message was built.

### The rule

An `IfExp` first argument is refused when BOTH hold: the failing set has more than
one member, AND neither branch carries the value. `is None`, `in`, `==` and
nothing-versus-something (`x > 0`, `n >= 1`) are determinate — the boolean already
names the failing state.

**Nothing-versus-something is determinate whatever the left side LOOKS like.** An
earlier draft keyed on the operand being a bare `Name` and so flagged
`(st['ret'] or 0) > 0`; the `or 0` is a None-guard, not a measurement, and refusing
it was syntax standing in for semantics.

**The one exception**, and it is why the carve-out cannot be "the comparator is 0":

    'yes' if min(t_proj_run, l_proj_run, t_base_run, l_base_run) > 0 else 'no'

The comparator IS 0 and the boolean still hides which of four measurements went
non-positive.

### Why this and not a parity check

`compare_to_bash` grades the check NAME. Two arms grade `missing: 0` while one
prints its measurement and the other prints `got 0 want 1` — which is how two of
the four pairs repaired under #1164 turned out to be **port regressions**, where
the shell original had carried its numbers all along. Name parity does not preserve
diagnostics, and no comparison of two independent implementations ever will.

### The population, measured before this was written

    expect.*(<IfExp>, ...)              190
      + both branches constant          148
        + a LOSSY test                   26   <- repaired under #1164

### The shell twin is shaped differently, on purpose

`test/selftest/540-an-arm-must-carry-its-measurement.sh` asserts a tracked list
(`test/lossy_arms.tsv`, 77 rows across 52 suites) rather than zero, because that
corpus is not at zero. The gate refuses both directions: an unlisted offender fails
by name, and a repaired arm fails until its row is removed, so the list may only
shrink. **The asymmetry is the honest answer**: the two corpora are in different
states, and a guard asserting zero over both would claim a property this tree does
not have.

### What it does not catch, and why each is left open

Three shapes are missed. Each was planted against the real sweep and then counted in
the corpus; none is live, and the file's header carries the measurement behind each.

| shape | why it is left open |
| --- | --- |
| an aggregate not spelled `min`, `max` or `sum` — `sorted(runs)[0] > 0` | closing it needs a longer list of names, which the next spelling escapes, or type information this has none of. The two subscript-of-a-call conditionals in the corpus are `str.split(...)[0]` under `==` or `not in`, so both are determinate |
| a branch that is a named constant — `KEPT if ratio < 1.35 else HALVED` | all four `Name`/`Attribute` branches today sit on determinate tests, so tightening flags none of them; what it would flag is `test_parallel_am_scan.py:225`, which does carry its number |
| a conditional in a later argument position | all three build a check NAME rather than a measured value, so scanning `args[1:]` refuses correct sites to catch a shape that does not occur |

### Every test

| test | what it holds |
| --- | --- |
| `test_no_arm_collapses_its_measurement_to_a_constant` | the corpus itself; green on arrival, which is the point |
| `test_the_sweep_finds_the_arms_it_is_meant_to_classify` | the coverage premise: a sweep that parsed nothing reports the same clean result |
| `test_a_lossy_arm_is_caught` | the removal proof, with a carrying arm as its control |
| `test_a_determinate_arm_is_not_caught` | six shapes whose failing set is a single value, driven rather than described |
| `test_an_aggregate_over_several_operands_stays_in_scope` | `min(a,b,c,d) > 0` is lossy, `min(a) > 0` is not |
| `test_a_chained_comparison_is_examined_pair_by_pair` | `0 < sel < 20000` hides its middle operand; an earlier draft scored every chain determinate |
| `test_the_carve_out_is_driven_at_its_boundary` | `> 0` and `>= 1` exactly, written either way round; `r > 1` and `r >= 0` are not the same shape and were being excused |
| `test_a_comparison_wrapped_in_anything_is_still_examined` | `any(r <= 0 for r in runs)` is the natural rewrite of `min(runs) > 0`, so the escape hatch is closed rather than left beside the door |
| `test_a_boolean_combination_is_examined_operand_by_operand` | one lossy operand is enough; a determinate one beside it is no excuse |

## 74. test_base_scan_io.py: a base scan is not priced from sibling projection pages

Port of `base_scan_io.sh`. A base columnar scan inherited `rel->pages` from
`smgrnblocks` of the relation file. That file holds the base plus every
projection stored beside it. Adding a covering projection does not make the
base scan read more bytes; the planner must not quote it as if it did.

Public seam: `EXPLAIN` cost of a base scan (`pgcolumnar.enable_projection_scan
= off`) before and after a sibling projection lands, with `seq_page_cost`
raised and CPU terms zeroed so the run is pages. The shell twin uses `bsio` /
`bynid` / 20000 rows; this file uses `bpages` / `onrid` / 30000 rows.
Assertion names match.

| test | what it holds |
| --- | --- |
| `test_base_scan_io` | the table exists; the plan is a base columnar scan with no covering projection name and a positive run cost; a covering projection then exists and enlarged the file; the later plan is still a base scan; the run cost is not priced from sibling projection pages |

## 75. test_range_pruning.py: a range prunes on overlap, containment, and under its own collation

    SELECT id FROM rp_col WHERE span && tstzrange('2024-03-01', '2024-03-01 06:00')

A zone map recorded `minimum` and `maximum` only. Those are the range type's btree
ordering, which sorts by lower bound and then upper bound, so the largest range is
not the one reaching furthest right: a unit holding `[1,2)` and `[3,100)` has the
same `maximum` as one holding `[1,2)` and `[3,4)`. Neither `&&` nor `@>` can be
answered from that, and both read every chunk group. `zone_map.max_upper` records
the greatest upper bound, and the scan skips a unit only when the summary proves it
cannot match.

**THE HEAP IS THE ORACLE.** A pruning defect returns FEWER rows rather than an
error, so every predicate is answered by a heap table built from the same generator
and the row IDENTIFIERS are compared, not the counts.

**THE SCATTERED ZERO IS AN ASSERTION, NOT AN OMISSION.** On a column whose spans are
not clustered, every group holds a bound near the maximum and no summary can exclude
any of them, so `Columnar Chunk Groups Read` equals `Columnar Chunk Groups Total`.

**AND A RANGE COMPARES UNDER THE COLLATION IT WAS DECLARED WITH**, which the type
cache carries as `rng_collation`. Reading the ELEMENT type's `typcollation` instead
made the writer summarise under one ordering and the reader prune under another, and
the scan MISSED ROWS. Not reachable with a built-in range type, so it needs a
user-defined range over a collatable subtype with an explicit non-default
`COLLATION`. Found by @jdatcmd reviewing #1144.

`test_range_pruning` asserts the counters, the heap oracle and the scattered zero.
`test_a_range_prunes_under_its_declared_collation` asserts the collation property,
on a user-defined range type it creates for the purpose.

Independent of `test/range_pruning.sh`: same public seam, own fixture, own row
counts, own observations. Neither file reads or runs the other.

## 76. test_docs_upgrade_chain.py: the documented upgrade chain must be the one that ships

Two claims travel in one sentence. It names the installed versions a single
`ALTER EXTENSION pgcolumnar UPDATE` can start from, and the version it arrives at.
Both are knowable from the tree:

| claim | where the tree answers it |
| --- | --- |
| the starting versions | the `pgcolumnar--A--B.sql` files, WALKED, not read as the set of `A` |
| the destination | `default_version` in `pgcolumnar.control` |

Nothing compared them. Opening the `1.0-alpha5` cycle bumped `VERSION`, the control
file, the version badge and `META.json`, and every version arm in `docs_style.sh`
stayed green over three documents that were wrong:

| document | starting versions it named | destination it named |
| --- | --- | --- |
| `CHANGELOG.md` | 4 of 5, `1.0-alpha4` missing | `1.0-alpha4` |
| `docs/installation.md` | all 5 | none a machine can find: "reaches **it** from" |
| `docs/limitations.md` | 3 of 5, and a hand-typed "Three such scripts" | `1.0-alpha3`, two cycles stale |

### The two errors cancel if you count

`CHANGELOG.md` named `1.0-alpha4` once too few as a starting version and once too
many as the destination. **The set of versions in the sentence was therefore exactly
right**, and a rule comparing that set against the tree would have passed a sentence
in which both halves were wrong. So the two claims are read separately, each against
its own source on disk.

### Scoped to the sentence, not the paragraph

The paragraph around the claim in `docs/installation.md` names the destination twice
more, in prose that is correct. A paragraph-wide reading counts those as starting
versions and reports a document that is right as wrong.

### A document this rule cannot read is named, with its reason

`reaches it from every previously published version` is correct English carrying no
destination a machine can find. An arm that skipped such a file would go quiet
exactly where the prose drifted, so the readable-form arm reports
`installation.md=no-destination` rather than passing over it. It also counts the
claim sentences and refuses a file that makes the claim twice, because reading only
the first leaves the second unchecked.

### The claim is reachability, not membership

"a **single** `ALTER EXTENSION pgcolumnar UPDATE` reaches `1.0-alpha5` from **any**
of them" says the shipped scripts form an unbroken chain. Reading the `A` side of
each filename cannot see that. @OffgridwithJD broke the chain without editing a
document:

    pgcolumnar--1.0-alpha2--1.0-alpha3.sql -> pgcolumnar--1.0-alpha2--1.0-alphaX.sql

`1.0-alpha2` still starts a script, so the set of starting versions does not move,
and every arm passed on a tree where three of the five named versions cannot
arrive in one command. So the versions are **walked**: follow `A--B` to the script
starting at `B`, and keep the version only if the walk ends at `default_version`.
That subsumes the membership test, because a version whose target starts nothing
drops out of the expectation.

A walk needs the chain to be a walk, so a second premise refuses any version that
starts more than one script rather than picking one of them silently.

| test | what it pins |
| --- | --- |
| `test_the_tree_states_a_chain_to_compare_against` | the scripts, the control file and the population are non-empty, no version starts two scripts, and the walk reaches `default_version` from somewhere |
| `test_every_document_states_the_chain_in_a_readable_form` | one claim sentence per file, carrying both halves |
| `test_every_document_names_the_versions_that_reach_default_version` | the starting versions equal the versions the walk reaches from |
| `test_every_document_names_default_version_as_the_destination` | the destination equals `default_version` |

### Removal proof, nine ways

Each mutation was applied to a copy of the tree, run, and reverted; all four files
were confirmed byte-identical to the source afterwards.

| mutation | what went red |
| --- | --- |
| `CHANGELOG.md` loses `1.0-alpha4` from its list | the starting-versions arm |
| `CHANGELOG.md` destination back to `1.0-alpha4` | the destination arm |
| `docs/installation.md` destination back to "reaches it" | the readable-form arm, and the destination arm |
| a second claim sentence in `docs/limitations.md` | the readable-form arm, `limitations.md=claims=2` |
| `pgcolumnar--1.0-alpha4--1.0-alpha5.sql` moved aside | the starting-versions arm, with the **want** side moving |
| `default_version` blanked | the control-file premise |
| the marker phrase erased in all three documents | the population premise |
| `1.0-alpha2--1.0-alpha3.sql` renamed to `--1.0-alphaX.sql` | the reachability arm, with the **want** side falling to two versions |
| a second script copied to `1.0-alpha3--1.0-alpha9.sql` | the one-script-per-version premise, `1.0-alpha3=2` |

The fifth row is the one that shows the arm reads the disk rather than agreeing with
itself: the documents did not change and the expectation did. The eighth is the one
a set comparison cannot produce at all.

### The splitter: what was claimed, what was measured

@OffgridwithJD expected a markdown list marker to SEVER the claim if the sentence
were moved into numbered step 3 of `docs/installation.md`. It does not. The claim
was moved there and **both halves read the whole sentence with and without any
masking**, because a marker precedes a sentence rather than sitting inside it. No
arrangement was found in which one falls inside the claim, and that half of the
finding was withdrawn.

**The over-match was live, and that half is measured.** The first version masked
any number followed by period-space, which also protects a sentence ENDING in a
number and merges it with the next one. Sentences found on the unmodified
documents:

**Measured at `acc4116d`, and the revision is the load-bearing part.** These
documents grow, so the counts drift with them and the rule does not: `CHANGELOG.md`
read 66 boundaries lost at `5649eba`, 69 at `197602f` and 71 here. A frozen number
in a note about a growing file goes stale by construction.

| document | column 0 only | any number | boundaries lost |
| --- | ---: | ---: | ---: |
| `docs/limitations.md` | 699 | 690 | 9 |
| `docs/installation.md` | 74 | 72 | 2 |
| `CHANGELOG.md` | 3957 | 3886 | 71 |

No verdict moved, because none of those merged pairs put a stray version token
into the claim sentence. **That is a property of today's prose, not of the rule**,
which is why the tight form ships.

### Column 0, and why that is measured rather than chosen

The first tight version allowed an indented marker and read **3939** on
`CHANGELOG.md` where @OffgridwithJD read **3942**. Reconciling the three rather
than splitting the difference found all three to be wrapped prose:

```
  4286. The port forces the path each arm is named for and a...
  1000. Every narrowing floors, so an instant before the epo...
  1000. The constant mis-sized every scan and corrupted join...
```

A sentence ending in a number, wrapped so the number starts an indented line, is
the same over-match one indent to the right. Every real ordered-list marker in
these documents sits at column 0: three in `docs/installation.md`, one in
`docs/limitations.md`, none in `CHANGELOG.md`.

### The twins fail differently, and this is where it shows

The shell half keeps its own tally and compares strings, so an empty population
leaves its three comparison arms passing on nothing and only the premise red. The
pytest half runs under the vacuity layer, and `expect.text` refuses an empty
expectation outright:

    VacuityError: ... the expected text is empty, so anything empty satisfies it.

So the same mutation produces one red arm in one harness and four in the other. That
is the reason to keep both: the shell twin needs its premise to stay honest, and the
pytest twin is refused before it can report a vacuous pass.

The shell twin is six arms in `test/docs_style.sh`. It folds the file with `tr` and
cuts sentences with `sed`; this half splits on a lookbehind and collects with `re`.
The two halves share no code.

## 77. test_projection_parallel.py: a covering projection can be a parallel scan

The covering-projection path was a serial CustomPath (`parallel_aware = false`,
`parallel_safe = false`) while the parallel base scan was a partial path with
no projection name. Those cannot both be true of one plan: either Gather wins
and the projection is dropped, or the serial projection wins and the workers
are dropped.

The executor already partitions whatever storage `BeginCustomScan` opened (the
DSM stripe counter is attached to `readState`), so a covering scan can be
parallel. A partial covering-projection path is now offered.

This file asserts the PLANNER shape, EXPLAIN ANALYZE worker rows, and the
query's count. Gather in the plan is not enough: a single claimer still
returns the covering rows once. Public seam: `EXPLAIN` / `EXPLAIN
(ANALYZE, VERBOSE)` of a covering projection query, plus `count(*)`. The
shell twin uses `cvppar` / `byik` / 32000 rows / `ik BETWEEN 40 AND 8039`;
this file uses `pcvgath` / `onskey` / 50000 rows / `skey BETWEEN 200 AND
12299`. Assertion names match.

It also walks a page-cost ladder (#1209). Core leaves disk I/O whole and
amortises only CPU, so raising `seq_page_cost` until the base relation's I/O
dominates its decode CPU must cost the parallel covering path; if the partial
covering total divided both, an I/O-bound scan would be quoted at 1/N and Gather
would keep winning however expensive the pages became. It is not a guard on the
clamp in that block: `unclamped - clamped = (pre - projRun) * (1 - 1/divisor)`
is strictly positive, so removing the clamp makes the partial path dearer and no
plan moves. The threshold is NOT scale-invariant: it grows with the row count, because
`cpuRun` scales with N while `basePagesRead - projPages` came out at 2 pages
regardless. Measured at `seq_page_cost = 4096`, 20,000 rows binds and both
32,000 and 50,000 do not. The arm is safe by margin, 1000000 being roughly 150x
the largest threshold observed, rather than by invariance, and no rung near a
crossover is asserted.

| test | what it asserts |
| --- | --- |
| `test_projection_parallel` | the table and covering projection exist; a serial covering query uses the projection; a parallel base scan is available when the projection is off; a covering projection can be a parallel scan; a parallel covering projection returns the covering rows once; EXPLAIN ANALYZE launched two workers and printed a rows= line for each; both launched workers produced rows; the covering plan is parallel at the default page cost; a page cost that makes I/O dominate costs the parallel covering path |

## 78. test_ttl_expire.py: the one function that deletes rows, tested twice

`pgcolumnar.expire` retires a row group whose every row is past a declared
retention, from the zone map alone: no decode, no rewrite, no scan of the data. It
is the only function in the tree whose failure mode is **losing data** rather than
reporting a wrong number, which is why it is a poor candidate for one harness
(#1188).

### The arm that constrains the feature

Retiring a group whose rows are all expired is the feature. Retiring one that still
holds live rows is data loss. Those two are the same operation on a fixture where
no group spans the cutoff, so every fixture that can straddle is built to straddle:

```
5,000 rows three minutes apart span about ten days, 1,000 to a group,
retention four days  ->  the cutoff falls INSIDE a group
```

**Measured.** Change the retire decision to read the group's `minimum` instead of
its `maximum` -- which is "drop every group holding an expired row", the data-loss
implementation -- and the shell twin says:

```
FAIL  NO row still inside the retention was dropped (#403 item 5a): got [3000] want [3560]
```

560 live rows. The pytest half reddens the same property in the same mutation.

### Two more ways a maximum is not every row

| case | why the maximum lies | the arm |
| --- | --- | --- |
| a NULL in the retention column | the zone map's maximum covers non-NULL values only, so old timestamps plus NULLs look fully expired | `expire does not retire a group that still holds NULL retention rows` |
| a NULL that was later DELETED | `null_count` is recorded at write time and never revised, so reading it as the live count keeps the group **forever** | `a group whose only NULLs have been deleted is retired` |

The second trades data loss for permanent over-retention, which is quieter and not
better. Its control -- a group whose NULLs are still live -- is what stops the fix
being "retire everything" wearing a delete.

### The index-only scan

`expire` retires live groups without going through the delete vector, so the
visibility-map bits `VACUUM` set stay on and an index-only scan can answer from the
index alone about a group that is no longer there.

**The plan-shape premise cannot fail for the thing under test.** The shape is
decided by `relallvisible` and by `enable_seqscan`/`enable_bitmapscan` being off,
not by the bit the fix clears: stop writing bits and the plan is unchanged. So the
bits are asserted directly, beside the plan.

### What the port substitutes, and what it therefore asserts

| the shell gets | structurally | the port uses | and asserts |
| --- | --- | --- | --- |
| `pgcolumnar.stripe_row_limit=1000` | baked into the cluster config before the postmaster starts (#806) | `set_options(..., stripe_row_limit => 1000)`, catalog state that travels with the table | the resulting group count, per fixture |
| `ALTER DATABASE ... SET` for the index-only settings | every fresh backend inherits them | `SET` on the one connection | the plan they are meant to produce |

**A procedural substitute for a structural guarantee has to be asserted**, because
it can fail silently where the original cannot. 1000 is exactly `set_options`'
floor, and a refusal there prints and does not abort, so a future change to that
floor would leave one large row group where nothing straddles and several arms
would stop asserting anything.

### The timezone arms, and the trap in porting them

`timestamptz_timestamp` converts through the session's `TimeZone`, so the cutoff a
`date` column is compared against is the calling session's own date. The arm pins
the **rule**, not a difference: asserting that two zones disagree would be a coin
toss on the time of day. Each zone asserts the identity that holds in every zone at
every instant, and both cutoffs are printed.

**The zone list must match the shell twin's**, because the zone is interpolated
into the check name. `compare_to_bash.py` reads `premise: in $_tz ...` and matches
it against `f"premise: in {tz} ..."` by template, so a port over different zones
asserts every property correctly and still cannot be graded against the suite it
mirrors. That trap is invisible from the source: 47 call sites yield 51 runtime
names, and the issue that asked for this port said 43.

### Removal proof

| mutation | pytest | shell |
| --- | --- | --- |
| `maximum` -> `minimum` in the retire decision | the straddle test red | `NO row still inside the retention was dropped` red |
| the live-NULL guard removed | two tests red | -- |
| the deleted-NULL arm neutered | one test red | -- |
| restored | 51 pass + 0 fail | 51 checks, PASSED |

Each mutation was asserted to have matched its anchor exactly once and to compile;
the `.so` moved `fe47f5f61e27` -> `7cc3b854ddaf` and back, and the source was
restored byte-identical.

| test | what it pins |
| --- | --- |
| `test_expire_retires_expired_groups_and_keeps_the_straddling_one` | the feature, and the straddling group that constrains it |
| `test_a_null_retention_value_keeps_its_group` | a live NULL the maximum cannot speak for |
| `test_an_index_only_scan_does_not_return_retired_rows` | the visibility-map bits over a group `expire` removed |
| `test_a_non_positive_retention_is_refused` | a cutoff in the future would retire live groups: `22023`, with a positive control |
| `test_a_deleted_null_stops_pinning_its_group` | `null_count` is a write-time record, not a live count |
| `test_a_date_retention_column_truncates_toward_keeping` | the rounding, which only this arm notices |
| `test_the_cutoff_is_the_sessions_own_date` | the cutoff is the session's, asserted as an identity rather than as a difference |

The shell twin is `test/ttl_expire.sh`, 51 checks. The two halves share no code:
different tables, different row counts, different retention windows, and the
refusals asserted by SQLSTATE here (`55000` for no declared retention, `22023` for
a non-positive interval) where the shell greps its message.

## 79. test_projection_scan_io.py: a covering projection is not priced from the base table's pages

Port of `projection_scan_io.sh`. A covering projection scan inherited the base
custom-scan I/O term, `seq_page_cost * rel->pages`. That page count is the whole
relation file: the base plus every projection stored beside it. The covering
path reads only the projection's own row groups.

Public seam: `EXPLAIN` cost of a covering projection against `pg_relation_size`
of the table, with `seq_page_cost` raised and CPU terms zeroed so the run is
pages. A lookup that cannot find the projection's storage must fall back to
`rel->pages`, not one page. The shell twin uses `psio` / `byik` / 24000 rows;
this file uses `pciot` / `onck` / 36000 rows. Assertion names match.

`pgcolumnar_projection_pages` has THREE returns and the first test reaches two:
the miss and the covering arm. The third, `if (pages < 1) pages = 1`, needs a
projection that is FOUND and whose storage holds no row groups -- not a small
one. `COLUMNAR_PAGE_ROUND_UP` rounds every group to a whole page, so one row
already returns through the covering arm at `pages = 1`, and an arm built on "a
tiny projection" reports green having never reached the line (#1208). The empty
fixtures are `psio_e` and `pciot_e`.

| test | what it holds |
| --- | --- |
| `test_projection_scan_io` | the table and covering projection exist; the plan uses that projection; the covering scan has a positive run cost; the projection occupies a minority of the relation; the covering run is not priced from the base table's pages; a covering path whose storage cannot be found is not priced as one page |
| `test_an_empty_covering_projection_is_priced_as_one_page` | the projection is found and its storage holds no row groups; an empty covering projection is still priced as one page, so it is still chosen |

## 80. test_catalog_plan_index.py: planning pays for these catalogs only when they hold something

Port of `catalog_plan_index.sh`. A plan that asks what one columnar table was written with, and whether it has a projection, sequentially scanned `pgcolumnar.options` and `pgcolumnar.projection`. Both catalogs already have a primary key on the column the scan key names, and #1198 made planning use it.

THAT WAS A REGRESSION ON THE DEFAULT CONFIGURATION, AND THIS FILE NOW SAYS SO. A row reaches `options` only when `set_options` is called and `projection` only when a projection is added, so an installation doing neither has both empty. At zero rows the heap the probe replaces is zero pages, the scan is literally free, and the probe cannot win however large the database grows -- there is no crossover to be above. Measured on main before the fix: six index buffers per plan, flat at 10, 200 and 1000 columnar tables alike. #1217 routes those seven sites through the size check shipped in #1213, and the cost returns to nothing.

FOUR ARMS WERE REMOVED RATHER THAN REPAIRED. They asserted `idx_scan >= 1` and `seq_scan == 0` on each catalog -- which path was taken, not how much work was done -- and they fail against the build that made planning strictly cheaper. That is a guard firing on correct code, the same defect #1213 removed from `catalog_delete_index.sh`. It was predicted before the change was written: the prediction named those four arms, the direction each would move, and the storage arms as the ones that must not move, and all three held.

ON A PRIVATE DATABASE, for the two tests whose claims are about catalog size. `pgc_conn` gives a private schema and these catalogs are per database, so inside the corpus other files have already populated them; `pgc_own_db` in `conftest.py` gives a database of its own. That fixture exists because the same trap bit `test_catalog_delete_index.py` first -- see section 81.

THE BULK OF THE PROJECTIONS GOES ON A TABLE THAT IS NOT THE MEASURED ONE. A probe's cost scales with the rows matching its key, not with the size of the catalog. The shell twin's first draft put 300 projections on the measured table, so every row matched, and it read the probe LOSING at seven pages -- which would have been recorded as "the threshold is wrong for the planner path". A premise now asserts the measured table owns a small share.

The two halves share no fixture: the shell suite drives `projection` to 7 pages with 300 projections on a noise table and measures 50 plans; this one uses 260 and a different measured table, and reads the work per catalog rather than summing in SQL.

`pg_stat_reset()` is database-wide. The corpus runs serially within a worker.

### Every test

| test | what it holds |
| --- | --- |
| `test_catalog_plan_index` | the measured table's row count, that the filtered scan returned every row, and that planning a `count(*)` and a two-relation join reach `pgcolumnar.storage` through `storage_pkey` |
| `test_planning_costs_nothing_on_the_default_configuration` | that this fixture really is the default configuration with both catalogs empty, that forcing the probe costs something so the instrument measured, that planning costs less than probing both, and that it touches the empty catalogs **not at all** |
| `test_planning_still_probes_a_populated_catalog` | that `projection` is larger than the threshold so the two paths differ, that the measured table owns a small share of it, that reading it whole costs something, and that planning costs less than reading it whole |

## 81. test_catalog_delete_index.py: retiring a row group costs no more for a bigger database

Port of `catalog_delete_index.sh`. `delete_group_rows()` opens its catalog from a `const char *tableName` PARAMETER and `PgColumnarDeleteGroupMetadata` calls it five times, so one `systable_beginscan` in the source was five sequential reads per retired group at run time. The `pgcolumnar` catalogs are shared by every columnar table in the database, so each read was charged for every other table's rows.

THE FIX IS NOT "USE THE INDEX", so the test cannot be "was the index used". A probe is a btree descent plus a heap fetch plus two catcache lookups, which on a catalog of a few pages is MORE work than reading the whole thing and far less on a large one. Both sizes occur in one installation. So these arms measure at three catalog sizes and each carries only the claim its size can support.

THEY ASSERT THE WORK AND NEVER THE ACCESS PATH: buffers out of `pg_statio_all_tables`, heap and index both. An earlier version asserted `seq_scan == 0` and `idx_scan >= 1` per catalog; run against the build that chooses by catalog size -- cheaper at every size measured -- it failed 13 of 21 arms, the same 13 a full revert reddens. A test that cannot tell a revert from an improvement fires on correct code, and a guard that fires on correct code gets switched off.

NOT A CONSTANT ANYWHERE. Every buffer count is compared against another taken in the same run from the same build, because the numbers are not the same on every major: the same fixture reads 848 buffers on PG17, 845 on PG15 and 895 on PG19. `pgcolumnar.index_min_blocks` decides the path, so the same work is measured two or three ways and the readings are compared to each other.

WHAT MAKES THE COMPARISON TRUSTWORTHY, AND THE TWO FLOORS THAT DID NOT. Compaction writes to `row_group` and `free_space`, so the next compaction reads more of them and two readings from identical code drift apart. A floor of one buffer let a full revert through: fifteen arms of sixteen passed against code with the fix removed. A floor of ten parts per thousand let it through too, because under a revert the drift reaches fourteen to sixteen -- and the two arms that floor was meant to protect were themselves worth only twenty-two and twenty-six, so they were measuring the sequence, not the fix. The floor is now 100 parts per thousand; each phase compacts a control table at the same setting as the measured one and refuses to report on top of the drift; and the control sits ADJACENT to the default, because drift accumulates with distance and a control three steps away once reported 32 against a margin of 31. Two arms were deleted rather than rescued. What a fixture cannot measure, this file does not assert.

Measured, this build against three mutations, in parts per thousand:

| arm | real | revert | probe-always | default replaced |
| --- | ---: | ---: | ---: | ---: |
| P1, 6 catalog pages | 284 | 0 | 12 | 284 (passes) |
| A1, 22 pages | 237 | -1 | -2 | -1 |
| B1, 76 pages | 1823 | 0 | -1 | 0 |
| growth, A to B | 960 | 1 | 22 | 1 |
| vacuum | 714 | 0 | 0 | 0 |

The vacuum arm has no positional confound at all, because a VACUUM is repeatable where a compaction is not: all three of its readings come from one table and only the setting differs. It is vacuumed small on purpose -- the gap widens as the catalog grows and narrows as the vacuumed table grows, and an earlier draft that vacuumed a thirty-group table swung between 200 and 750 from run to run on a base of ten buffers.

THE PORT RUNS ON A PRIVATE DATABASE, not the private schema `pgc_conn` gives every other test here. That trade -- isolation without an initdb per test -- is right almost everywhere and wrong for this file: the `pgcolumnar` catalogs are per DATABASE and shared by the whole session, and every claim here is about how big they are. Run alone, the file saw six catalog pages at phase 0 and P1 was worth 223 parts per thousand; run after the other fifty-one cluster files it saw thirty-nine and P1 was worth 65, under the floor. CI found that and a local run of one file could not. A premise now counts the columnar relations in each phase, so a shared database arrives as a named failure rather than as a weak number -- and on its first run that premise caught a different defect too, a table left behind by a refactor that nothing used.

The two halves share no code and no fixture. The shell suite uses ten- and forty-group tables, retires every OTHER group and adds 200,000 rows of deep noise; this one uses twelve and forty-five, retires the LAST half, and adds 150,000. The shell suite sums the work in SQL; this one reads it per catalog and sums in Python, and prints the per-catalog breakdown for every setting -- which is how the drift was traced to `row_group` alone, the one catalog a compaction writes to.

THE ONE THING THIS HALF HAS TO DO THAT THE SHELL HALF DOES NOT is flush the statistics before resetting them. This harness holds one connection for the whole file, so the writes leave pending statistics in the backend that `pg_stat_reset()` does not clear; they are flushed afterwards and land on top of the reading. Measured on an otherwise identical single-session fixture: `row_group idx=36 seq=15` without the flush, `idx=32 seq=0` with it. The fifteen were the test's own `DELETE`. The shell half runs every statement in a fresh backend, which flushes on exit, so it cannot reach this state.

`pg_stat_reset()` is database-wide. The corpus runs serially within a worker.

### Every test

| test | what it holds |
| --- | --- |
| `test_retiring_a_group_costs_no_more_for_a_bigger_database` | per phase: three identical targets, the emptied groups retired, every survivor kept, all six catalogs read, and two compactions at the same setting agreeing well inside the floor. Then that the default does less work than probing every catalog at six pages, less than reading every one whole at twenty-two and at seventy-six, and that its cost grows far less with the database than reading whole does |
| `test_vacuum_reads_less_of_row_group_than_reading_it_whole` | that the vacuum walked this table's groups, that two vacuums at the same setting agree, and that its `row_group` work is below what reading that catalog whole costs -- the `PgColumnarComputeAllVisibleGroups` read, which the compaction path never reaches |
| `test_dropping_a_table_reads_less_of_the_catalogs_than_reading_them_whole` | that a DROP costs less catalog work than reading the seven catalogs whole, and -- below the threshold -- less than forcing a probe. The subject is `delete_rows_by_storage_id`, one parameterised line serving seven catalogs on the DROP/TRUNCATE path, which nothing covered before (#1207). Measured in buffers, never the access path, and on a fixture sixty tables wide because `pgc_own_db` gives this file a private database where the shell twin shares a cluster |

## 82. test_rewrite_storage_oid.py: a type rewrite keeps the storage row on the live table

Port of `rewrite_storage_oid.sh`. `ALTER COLUMN ... TYPE` writes `pgcolumnar.storage.relation_oid` as the transient relation and then drops it. The live table's OID no longer finds the row. The rows themselves stay readable.

### Every test

| test | what it holds |
| --- | --- |
| `test_rewrite_storage_oid` | the storage row matches the live regclass before the rewrite, the row count survives, an unrewritten table still matches itself, and the rewritten table's storage row matches the live regclass afterwards |
