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
- [25. test_native_join_runtime_filter.py: serial join runtime filter](#25-test_native_join_runtime_filterpy-serial-join-runtime-filter)
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
- [83. test_catalog_scan_index.py: twelve metadata scans do not read their catalog whole](#83-test_catalog_scan_indexpy-twelve-metadata-scans-do-not-read-their-catalog-whole)
- [84. test_native_format.py: the format stamp is a range, and an append raises it](#84-test_native_formatpy-the-format-stamp-is-a-range-and-an-append-raises-it)
- [85. test_native_agg.py: min/max over timestamptz read no data](#85-test_native_aggpy-minmax-over-timestamptz-read-no-data)
- [86. test_format_downgrade.py: an alpha4 binary refuses what this build wrote](#86-test_format_downgradepy-an-alpha4-binary-refuses-what-this-build-wrote)
- [87. test_latest_reading.py: the latest-reading recipe does what the document says](#87-test_latest_readingpy-the-latest-reading-recipe-does-what-the-document-says)
- [88. test_storage_row_repair.py: the alpha5 release notes' repair procedure works as written](#88-test_storage_row_repairpy-the-alpha5-release-notes-repair-procedure-works-as-written)
- [89. test_docs_cross_database_query.py: no documented query asks another database for its extensions](#89-test_docs_cross_database_querypy-no-documented-query-asks-another-database-for-its-extensions)
- [90. test_partman_columnar.py: pg_partman makes columnar partitions with one setting](#90-test_partman_columnarpy-pg_partman-makes-columnar-partitions-with-one-setting)
- [91. test_capability_decline.py: a missing pyarrow declines by name, and nothing declines silently](#91-test_capability_declinepy-a-missing-pyarrow-declines-by-name-and-nothing-declines-silently)
- [92. test_tid_range_scan.py: a ctid range reads only the row groups that hold it](#92-test_tid_range_scanpy-a-ctid-range-reads-only-the-row-groups-that-hold-it)
- [93. test_partition_lifecycle.py: partition_create_ahead keeps a time-partitioned parent ahead of now](#93-test_partition_lifecyclepy-partition_create_ahead-keeps-a-time-partitioned-parent-ahead-of-now)
- [94. test_approx_aggregates.py: hll and tdigest over columnar tables, the compatibility statement](#94-test_approx_aggregatespy-hll-and-tdigest-over-columnar-tables-the-compatibility-statement)
- [95. test_docs_how_to_sections.py: the pg_partman how-to keeps its own heading](#95-test_docs_how_to_sectionspy-the-pg_partman-how-to-keeps-its-own-heading)
- [96. test_autovacuum_expire.py: the daemon expires retention tables only when told to](#96-test_autovacuum_expirepy-the-daemon-expires-retention-tables-only-when-told-to)
- [97. test_rollup.py: incremental rollups fold appended row groups, recompute changed ones, and equal a full recompute](#97-test_rolluppy-incremental-rollups-fold-appended-row-groups-recompute-changed-ones-and-equal-a-full-recompute)
- [98. test_timeseries_recipes.py: the gap-fill, LOCF and interpolation recipes answer what they claim](#98-test_timeseries_recipespy-the-gap-fill-locf-and-interpolation-recipes-answer-what-they-claim)
- [99. test_suite_exit_trap.py: a suite's own EXIT trap must still stop its cluster](#99-test_suite_exit_trappy-a-suites-own-exit-trap-must-still-stop-its-cluster)
- [100. test_tier_partition.py: tier_partition moves a cold partition to Parquet, exactly once](#100-test_tier_partitionpy-tier_partition-moves-a-cold-partition-to-parquet-exactly-once)
- [101. test_prepared_writes.py: a write prepared and finished in one session leaves the backend alive](#101-test_prepared_writespy-a-write-prepared-and-finished-in-one-session-leaves-the-backend-alive)
- [102. test_column_extent.py: the per-column extent is a true bound, recomputed whenever groups go](#102-test_column_extentpy-the-per-column-extent-is-a-true-bound-recomputed-whenever-groups-go)
- [103. test_collation_change.py: a collation change without a rewrite leaves columnar answering as heap does](#103-test_collation_changepy-a-collation-change-without-a-rewrite-leaves-columnar-answering-as-heap-does)
- [104. test_build_needs_pg_config.py: a build with no pg_config refuses, it does not wait on stdin](#104-test_build_needs_pg_configpy-a-build-with-no-pg_config-refuses-it-does-not-wait-on-stdin)
- [105. test_import_export_privilege.py: import and export check the table's privilege, serial and parallel](#105-test_import_export_privilegepy-import-and-export-check-the-tables-privilege-serial-and-parallel)
- [106. test_every_suite_job_installs_the_extensions.py: every job that runs the suites installs the extension packages](#106-test_every_suite_job_installs_the_extensionspy-every-job-that-runs-the-suites-installs-the-extension-packages)
- [107. test_fdw_plan_privilege.py: planning a foreign table reads no server file for a role the scan would refuse](#107-test_fdw_plan_privilegepy-planning-a-foreign-table-reads-no-server-file-for-a-role-the-scan-would-refuse)
- [108. test_arrow_import.py: import_arrow round trips, reads a foreign file, and refuses what it cannot decode](#108-test_arrow_importpy-import_arrow-round-trips-reads-a-foreign-file-and-refuses-what-it-cannot-decode)
- [109. test_entry_point_privilege.py: every relation-taking entry point has a decided privilege bar](#109-test_entry_point_privilegepy-every-relation-taking-entry-point-has-a-decided-privilege-bar)
- [110. test_am_contracts.py: contracts the access method owes core DML and transactions](#110-test_am_contractspy-contracts-the-access-method-owes-core-dml-and-transactions)
- [111. test_objstore_credentials.py: object-store credentials live on a user mapping, and the environment is a privilege](#111-test_objstore_credentialspy-object-store-credentials-live-on-a-user-mapping-and-the-environment-is-a-privilege)
- [112. test_debug_hook_privilege.py: the debug metadata mutators are owner-only](#112-test_debug_hook_privilegepy-the-debug-metadata-mutators-are-owner-only)
- [113. test_vacuum_lock_privilege.py: maintenance refuses a non-owner before taking its lock](#113-test_vacuum_lock_privilegepy-maintenance-refuses-a-non-owner-before-taking-its-lock)
- [114. test_vm_privilege.py: the visibility-map entry points are owner-only and refuse heap](#114-test_vm_privilegepy-the-visibility-map-entry-points-are-owner-only-and-refuse-heap)
- [115. test_alter_table_scans.py: ALTER TABLE forms see the live rows and only those](#115-test_alter_table_scanspy-alter-table-forms-see-the-live-rows-and-only-those)
- [116. test_settings_reference.py: the settings reference matches the server](#116-test_settings_referencepy-the-settings-reference-matches-the-server)
- [117. test_large_values.py: megabyte-scale values are stored exactly](#117-test_large_valuespy-megabyte-scale-values-are-stored-exactly)
- [118. test_recovery_minimal_wal.py: tables that skip WAL survive a crash](#118-test_recovery_minimal_walpy-tables-that-skip-wal-survive-a-crash)
- [119. test_statement_self_visibility.py: a statement does not see its own writes](#119-test_statement_self_visibilitypy-a-statement-does-not-see-its-own-writes)
- [120. test_maintenance_rollback.py: a maintenance function that rolls back leaves the table as it was](#120-test_maintenance_rollbackpy-a-maintenance-function-that-rolls-back-leaves-the-table-as-it-was)
- [121. test_partition_rows.py: rows cross columnar partitions, and keys hold through the parent](#121-test_partition_rowspy-rows-cross-columnar-partitions-and-keys-hold-through-the-parent)
- [122. test_prepared_across_sessions.py: a prepared transaction, seen from other sessions and across a crash](#122-test_prepared_across_sessionspy-a-prepared-transaction-seen-from-other-sessions-and-across-a-crash)
- [123. test_maintenance_crash.py: a crash part-way through maintenance loses nothing](#123-test_maintenance_crashpy-a-crash-part-way-through-maintenance-loses-nothing)
- [124. test_serializable.py: SERIALIZABLE refuses a write skew on a columnar table](#124-test_serializablepy-serializable-refuses-a-write-skew-on-a-columnar-table)
- [125. test_rls_direct_storage.py: the direct-storage paths refuse a caller a policy restricts](#125-test_rls_direct_storagepy-the-direct-storage-paths-refuse-a-caller-a-policy-restricts)
- [126. test_objstore_crlf.py: a CR or LF in an object-store URL is refused before it is sent](#126-test_objstore_crlfpy-a-cr-or-lf-in-an-object-store-url-is-refused-before-it-is-sent)
- [127. test_objstore_allowlist.py: the endpoint allow-list, empty by default](#127-test_objstore_allowlistpy-the-endpoint-allow-list-empty-by-default)
- [128. test_objstore_userinfo.py: userinfo in a URL is refused on every entry point](#128-test_objstore_userinfopy-userinfo-in-a-url-is-refused-on-every-entry-point)
- [129. test_savepoint_flush.py: a savepoint rolled back after a flush](#129-test_savepoint_flushpy-a-savepoint-rolled-back-after-a-flush)
- [130. test_rls_query_paths.py: row-level security holds on every read path](#130-test_rls_query_pathspy-row-level-security-holds-on-every-read-path)
- [131. test_type_matrix.py: types the differential matrix never stored answer as heap](#131-test_type_matrixpy-types-the-differential-matrix-never-stored-answer-as-heap)
- [132. test_iceberg_data_files.py: an Iceberg table's live data files, listed inside its location](#132-test_iceberg_data_filespy-an-iceberg-tables-live-data-files-listed-inside-its-location)
- [133. test_native_skip.py: a type binary-coercible to its opclass's type prunes](#133-test_native_skippy-a-type-binary-coercible-to-its-opclasss-type-prunes)
- [134. test_unique_conc.py: concurrent inserters of one unique key serialize](#134-test_unique_concpy-concurrent-inserters-of-one-unique-key-serialize)
- [135. test_update_conc.py: two writers of one row serialize](#135-test_update_concpy-two-writers-of-one-row-serialize)
- [136. test_recovery.py: committed writes survive a crash, uncommitted ones leave nothing](#136-test_recoverypy-committed-writes-survive-a-crash-uncommitted-ones-leave-nothing)
- [137. test_native_backend_crash.py: one backend dies and committed data is intact](#137-test_native_backend_crashpy-one-backend-dies-and-committed-data-is-intact)
- [138. test_corruption.py: a corrupt catalog row is refused cleanly and no backend dies](#138-test_corruptionpy-a-corrupt-catalog-row-is-refused-cleanly-and-no-backend-dies)
- [139. test_inheritance.py: a partitioned columnar parent is refused, a columnar matview is not](#139-test_inheritancepy-a-partitioned-columnar-parent-is-refused-a-columnar-matview-is-not)
- [140. test_parquet_import.py: a partitioned columnar parent is refused by import_parquet](#140-test_parquet_importpy-a-partitioned-columnar-parent-is-refused-by-import_parquet)
- [141. test_phase5.py: set_options and reset_options agree](#141-test_phase5py-set_options-and-reset_options-agree)
- [142. test_drop_cleanup.py: a columnar matview's options row goes with its DROP](#142-test_drop_cleanuppy-a-columnar-matviews-options-row-goes-with-its-drop)
- [143. test_native_upgrade_converge.py: every release upgrades to a fresh catalog, and the window before it](#143-test_native_upgrade_convergepy-every-release-upgrades-to-a-fresh-catalog-and-the-window-before-it)
- [144. test_fk_referencing.py: a foreign key referencing a columnar table is refused where it is chosen](#144-test_fk_referencingpy-a-foreign-key-referencing-a-columnar-table-is-refused-where-it-is-chosen)
- [145. test_truncate_cleanup.py: TRUNCATE leaves no catalog rows behind, and keeps what it must](#145-test_truncate_cleanuppy-truncate-leaves-no-catalog-rows-behind-and-keeps-what-it-must)
- [146. test_native_dml.py: DELETE, UPDATE and MERGE on a native table agree with heap](#146-test_native_dmlpy-delete-update-and-merge-on-a-native-table-agree-with-heap)
- [147. test_native_dict_underfill.py: an under-filled DICT chunk is refused and no backend dies](#147-test_native_dict_underfillpy-an-under-filled-dict-chunk-is-refused-and-no-backend-dies)
- [148. test_native_decode_gating.py: an unprunable qual gates payload decode per vector](#148-test_native_decode_gatingpy-an-unprunable-qual-gates-payload-decode-per-vector)
- [149. test_native_decode_gate_width.py: decode gating runs only on a projection wide enough to pay](#149-test_native_decode_gate_widthpy-decode-gating-runs-only-on-a-projection-wide-enough-to-pay)
- [150. test_pg_dump_roundtrip.py: a columnar table survives pg_dump and restore](#150-test_pg_dump_roundtrippy-a-columnar-table-survives-pg_dump-and-restore)
- [151. test_pg_upgrade.py: a columnar table survives pg_upgrade](#151-test_pg_upgradepy-a-columnar-table-survives-pg_upgrade)
- [152. test_replication.py: physical replication and standby redo](#152-test_replicationpy-physical-replication-and-standby-redo)
- [153. test_native_recluster.py: online reclustering under ShareUpdateExclusiveLock](#153-test_native_reclusterpy-online-reclustering-under-shareupdateexclusivelock)
- [154. test_alter_am_cleanup.py: DROP after SET ACCESS METHOD heap cleans up](#154-test_alter_am_cleanuppy-drop-after-set-access-method-heap-cleans-up)
- [155. test_native_reclaim.py: physical page reclaim and the compaction threshold guards](#155-test_native_reclaimpy-physical-page-reclaim-and-the-compaction-threshold-guards)
- [156. test_native_repack.py: REPACK on a columnar table](#156-test_native_repackpy-repack-on-a-columnar-table)
- [157. test_native_join_vector_agg.py: the vectorized aggregate over a unique-key inner join](#157-test_native_join_vector_aggpy-the-vectorized-aggregate-over-a-unique-key-inner-join)
- [158. test_native_writer.py: the native writer by its catalog rows](#158-test_native_writerpy-the-native-writer-by-its-catalog-rows)
- [159. test_native_batch_fold_projection.py: the batch fold's column lists on a wide table](#159-test_native_batch_fold_projectionpy-the-batch-folds-column-lists-on-a-wide-table)
- [160. test_native_encdesc_golden.py: the encoding descriptor's wire layout](#160-test_native_encdesc_goldenpy-the-encoding-descriptors-wire-layout)
- [161. test_native_parquet_schema.py: parquet_schema round trip](#161-test_native_parquet_schemapy-parquet_schema-round-trip)
- [162. test_native_vecskip.py: per-vector skipping by the zone maps](#162-test_native_vecskippy-per-vector-skipping-by-the-zone-maps)
- [163. test_native_index_projection.py: CREATE INDEX projects only what it needs](#163-test_native_index_projectionpy-create-index-projects-only-what-it-needs)
- [164. test_native_saop_pushdown.py: IN-lists and = ANY(array) prune row groups](#164-test_native_saop_pushdownpy-in-lists-and--anyarray-prune-row-groups)
- [165. test_native_groupagg_batch.py: the grouped aggregate folds column-at-a-time](#165-test_native_groupagg_batchpy-the-grouped-aggregate-folds-column-at-a-time)
- [166. test_native_fetch_projection.py: a fetch by row number decodes only what its caller needs](#166-test_native_fetch_projectionpy-a-fetch-by-row-number-decodes-only-what-its-caller-needs)
- [167. test_native_fetch_position.py: a fetch by row number costs the same anywhere in its group](#167-test_native_fetch_positionpy-a-fetch-by-row-number-costs-the-same-anywhere-in-its-group)
- [168. test_native_fetch_cache.py: the fetch-by-row-number cache](#168-test_native_fetch_cachepy-the-fetch-by-row-number-cache)
- [169. test_native_parquet_pushdown.py: the Parquet FDW skips row groups its statistics rule out](#169-test_native_parquet_pushdownpy-the-parquet-fdw-skips-row-groups-its-statistics-rule-out)
- [170. test_native_parquet_flba.py: uuid and DECIMAL read from fixed-length byte arrays](#170-test_native_parquet_flbapy-uuid-and-decimal-read-from-fixed-length-byte-arrays)
- [171. test_native_parquet_multifile.py: directories and globs read as one relation](#171-test_native_parquet_multifilepy-directories-and-globs-read-as-one-relation)
- [172. test_native_index.py: index lookups and unique enforcement fetch by row number](#172-test_native_indexpy-index-lookups-and-unique-enforcement-fetch-by-row-number)
- [173. test_native_fetch_bigcap.py: fetching from a row group above the cache cap](#173-test_native_fetch_bigcappy-fetching-from-a-row-group-above-the-cache-cap)
- [174. test_sorted_projection.py: vacuum_sorted reorders storage and changes no answer](#174-test_sorted_projectionpy-vacuum_sorted-reorders-storage-and-changes-no-answer)
- [175. test_native_ios.py: an index-only scan skips the fetch on all-visible row groups](#175-test_native_iospy-an-index-only-scan-skips-the-fetch-on-all-visible-row-groups)
- [176. test_native_truncate.py: end-truncation shrinks the file and loses no row](#176-test_native_truncatepy-end-truncation-shrinks-the-file-and-loses-no-row)
- [177. test_native_gap.py: a write beyond EOF fills the gap and loses no row](#177-test_native_gappy-a-write-beyond-eof-fills-the-gap-and-loses-no-row)
- [178. test_native_roundtrip.py: a native table reads back what the heap holds](#178-test_native_roundtrippy-a-native-table-reads-back-what-the-heap-holds)
- [179. test_native_bloom.py: a bloom filter prunes the equality probe min/max cannot](#179-test_native_bloompy-a-bloom-filter-prunes-the-equality-probe-minmax-cannot)
- [180. test_native_toasted_write.py: a toasted value is written once and read back whole](#180-test_native_toasted_writepy-a-toasted-value-is-written-once-and-read-back-whole)
- [181. test_native_varlena_bound.py: a corrupt varlena length is refused, not read past its buffer](#181-test_native_varlena_boundpy-a-corrupt-varlena-length-is-refused-not-read-past-its-buffer)
- [182. test_native_agg_addcolumn.py: a metadata aggregate over an added column reads what the metadata cannot hold](#182-test_native_agg_addcolumnpy-a-metadata-aggregate-over-an-added-column-reads-what-the-metadata-cannot-hold)
- [183. test_native_groupagg_wide_cost.py: the grouped fold prices its own input scan as the scan node does](#183-test_native_groupagg_wide_costpy-the-grouped-fold-prices-its-own-input-scan-as-the-scan-node-does)
- [184. test_hardening.py: corrupted native metadata is refused or ignored, never trusted](#184-test_hardeningpy-corrupted-native-metadata-is-refused-or-ignored-never-trusted)
- [185. test_dependency_estimate.py: a functional-dependency estimate is not collapsed by a truncated extended MCV](#185-test_dependency_estimatepy-a-functional-dependency-estimate-is-not-collapsed-by-a-truncated-extended-mcv)
- [186. test_batch_fold_explain.py: EXPLAIN ANALYZE reports the fold that ran, not the one predicted](#186-test_batch_fold_explainpy-explain-analyze-reports-the-fold-that-ran-not-the-one-predicted)
- [187. test_zonemap_cost.py: a scan is priced for what the zone maps let it skip, and only for that](#187-test_zonemap_costpy-a-scan-is-priced-for-what-the-zone-maps-let-it-skip-and-only-for-that)
- [188. test_concurrent_diff.py: concurrent writers to one columnar relation end equal to a heap oracle](#188-test_concurrent_diffpy-concurrent-writers-to-one-columnar-relation-end-equal-to-a-heap-oracle)
- [189. test_write_minmax_fastpath.py: zone min/max through the direct comparison path are the true bounds](#189-test_write_minmax_fastpathpy-zone-minmax-through-the-direct-comparison-path-are-the-true-bounds)
- [190. test_catalog_natts.py: every Natts_ constant equals the width of the catalog it sizes](#190-test_catalog_nattspy-every-natts_-constant-equals-the-width-of-the-catalog-it-sizes)
- [191. test_write_fsst_compressed.py: FSST is kept only where it wins after the block compressor](#191-test_write_fsst_compressedpy-fsst-is-kept-only-where-it-wins-after-the-block-compressor)
- [192. test_zonemap_estimate_sample.py: the planner's zone-map sample is one-based, complete and per-column](#192-test_zonemap_estimate_samplepy-the-planners-zone-map-sample-is-one-based-complete-and-per-column)
- [193. test_wal_envelope.py: pgColumnar emits only core's WAL, inside RelationTruncate's envelope](#193-test_wal_envelopepy-pgcolumnar-emits-only-cores-wal-inside-relationtruncates-envelope)
- [194. test_arrow_nested_import.py: arrays and composites round-trip through Arrow](#194-test_arrow_nested_importpy-arrays-and-composites-round-trip-through-arrow)
- [195. test_arrow_nested.py: arrays and composites export as Arrow List and Struct](#195-test_arrow_nestedpy-arrays-and-composites-export-as-arrow-list-and-struct)
- [196. test_vm_clear_on_renumber.py: the visibility map is cleared wherever live rows are renumbered](#196-test_vm_clear_on_renumberpy-the-visibility-map-is-cleared-wherever-live-rows-are-renumbered)
- [197. test_arrow_export.py: every exported value equals the heap oracle, and only the documented refusals](#197-test_arrow_exportpy-every-exported-value-equals-the-heap-oracle-and-only-the-documented-refusals)
- [198. test_vector_agg_tlist_shape.py: the vectorized aggregate accepts a target list that contains aggregates](#198-test_vector_agg_tlist_shapepy-the-vectorized-aggregate-accepts-a-target-list-that-contains-aggregates)
- [199. test_alter_column_type.py: ALTER COLUMN TYPE converts every value, and the column really changes type](#199-test_alter_column_typepy-alter-column-type-converts-every-value-and-the-column-really-changes-type)
- [200. test_vector_agg_rescan_memory.py: rescanning a columnar aggregate does not grow query memory](#200-test_vector_agg_rescan_memorypy-rescanning-a-columnar-aggregate-does-not-grow-query-memory)
- [201. test_analyze_stats.py: ANALYZE collects statistics that agree with heap, and the planner prices what they feed](#201-test_analyze_statspy-analyze-collects-statistics-that-agree-with-heap-and-the-planner-prices-what-they-feed)
- [202. test_vacuum_stripe_count.py: pgcolumnar.vacuum refuses a stripe_count bound rather than ignoring it](#202-test_vacuum_stripe_countpy-pgcolumnarvacuum-refuses-a-stripe_count-bound-rather-than-ignoring-it)
- [203. test_bloom_sizing.py: a bloom filter is sized by its distinct count, and still skips](#203-test_bloom_sizingpy-a-bloom-filter-is-sized-by-its-distinct-count-and-still-skips)
- [204. test_vacuum_sorted_gate.py: pgcolumnar.vacuum_sorted skips its rewrite only when there is nothing to do](#204-test_vacuum_sorted_gatepy-pgcolumnarvacuum_sorted-skips-its-rewrite-only-when-there-is-nothing-to-do)
- [205. test_ungrouped_vector_agg.py: the ungrouped vectorized aggregate returns what core Agg returns](#205-test_ungrouped_vector_aggpy-the-ungrouped-vectorized-aggregate-returns-what-core-agg-returns)
- [206. test_tablesample.py: TABLESAMPLE on a columnar table raises 0A000](#206-test_tablesamplepy-tablesample-on-a-columnar-table-raises-0a000)
- [207. test_bloom_setting.py: enable_bloom_filter governs the write side too](#207-test_bloom_settingpy-enable_bloom_filter-governs-the-write-side-too)
- [208. test_sorted_mark_rename.py: a table's ordering mark follows a column rename](#208-test_sorted_mark_renamepy-a-tables-ordering-mark-follows-a-column-rename)
- [209. test_sort_status_privilege.py: sort_status answers whoever may SELECT the table, and only them](#209-test_sort_status_privilegepy-sort_status-answers-whoever-may-select-the-table-and-only-them)
- [210. test_bloom_lazy.py: a scan reads the bloom filters of the groups and columns it examines](#210-test_bloom_lazypy-a-scan-reads-the-bloom-filters-of-the-groups-and-columns-it-examines)
- [211. test_sort_status.py: sort_status reports how much of an ordered table is still in its run](#211-test_sort_statuspy-sort_status-reports-how-much-of-an-ordered-table-is-still-in-its-run)
- [212. test_smoke.py: a columnar table stores rows and gives them back](#212-test_smokepy-a-columnar-table-stores-rows-and-gives-them-back)
- [213. test_server_file_privilege.py: every server-file function is gated like COPY](#213-test_server_file_privilegepy-every-server-file-function-is-gated-like-copy)
- [214. test_autovacuum.py: the maintenance daemon compacts and reclusters only while it is on](#214-test_autovacuumpy-the-maintenance-daemon-compacts-and-reclusters-only-while-it-is-on)
- [215. test_autovacuum_yield.py: the maintenance daemon yields its lock to a waiting session](#215-test_autovacuum_yieldpy-the-maintenance-daemon-yields-its-lock-to-a-waiting-session)
- [216. test_projection_rewrite.py: a declared projection survives a rewrite](#216-test_projection_rewritepy-a-declared-projection-survives-a-rewrite)
- [217. test_avro_manifest.py: Iceberg Avro manifests and manifest lists decode, and refuse what they must](#217-test_avro_manifestpy-iceberg-avro-manifests-and-manifest-lists-decode-and-refuse-what-they-must)
- [218. test_column_projection.py: column projection reads only the columns a query references](#218-test_column_projectionpy-column-projection-reads-only-the-columns-a-query-references)
- [219. test_local_open_race_free.py: local file opens are race-free: no stat-before-open](#219-test_local_open_race_freepy-local-file-opens-are-race-free-no-stat-before-open)
- [220. test_encode_effort.py: the per-table encode_effort option](#220-test_encode_effortpy-the-per-table-encode_effort-option)
- [221. test_doc_parallel_premise.py: the parallel-scan page's narrow query reads every chunk group](#221-test_doc_parallel_premisepy-the-parallel-scan-pages-narrow-query-reads-every-chunk-group)
- [222. test_maintenance_due.py: pgcolumnar.maintenance_due reports which online verb is worth running](#222-test_maintenance_duepy-pgcolumnarmaintenance_due-reports-which-online-verb-is-worth-running)
- [223. test_eager_ordering_record.py: eager rewrites record the ordering they applied](#223-test_eager_ordering_recordpy-eager-rewrites-record-the-ordering-they-applied)
- [224. test_int8_agg_int128.py: sum and avg over bigint accumulate in 128 bits](#224-test_int8_agg_int128py-sum-and-avg-over-bigint-accumulate-in-128-bits)
- [225. test_fsst_verdict_cache.py: the FSST keep/drop verdict is cached with an age bound](#225-test_fsst_verdict_cachepy-the-fsst-keepdrop-verdict-is-cached-with-an-age-bound)
- [226. test_import_exclusion.py: import_arrow enforces an exclusion constraint, and a rewrite does not](#226-test_import_exclusionpy-import_arrow-enforces-an-exclusion-constraint-and-a-rewrite-does-not)
- [227. test_import_deferred.py: imports honour a DEFERRABLE constraint, and the check is deferred, not gone](#227-test_import_deferredpy-imports-honour-a-deferrable-constraint-and-the-check-is-deferred-not-gone)
- [228. test_index_only.py: index-only scans on a columnar table, served from the visibility map](#228-test_index_onlypy-index-only-scans-on-a-columnar-table-served-from-the-visibility-map)
- [229. test_isolation.py: deterministic isolation specs for the compaction and truncation races](#229-test_isolationpy-deterministic-isolation-specs-for-the-compaction-and-truncation-races)
- [230. test_concurrency.py: concurrent deletes into one chunk group serialize and both survive](#230-test_concurrencypy-concurrent-deletes-into-one-chunk-group-serialize-and-both-survive)
- [231. test_iceberg_scan.py: iceberg_scan reads by field id, behind the server-file gate](#231-test_iceberg_scanpy-iceberg_scan-reads-by-field-id-behind-the-server-file-gate)
- [232. test_iceberg_catalog.py: the Iceberg catalog reader resolves the named current snapshot](#232-test_iceberg_catalogpy-the-iceberg-catalog-reader-resolves-the-named-current-snapshot)
- [233. test_iceberg_name_mapping_memory.py: the Iceberg name-mapping parse does not allocate per entry](#233-test_iceberg_name_mapping_memorypy-the-iceberg-name-mapping-parse-does-not-allocate-per-entry)
- [234. test_iceberg_fdw_estimate.py: the Iceberg FDW estimates rows from the manifests](#234-test_iceberg_fdw_estimatepy-the-iceberg-fdw-estimates-rows-from-the-manifests)
- [235. test_iceberg_fdw_projection.py: the Iceberg FDW's projection pushdown never changes a result](#235-test_iceberg_fdw_projectionpy-the-iceberg-fdws-projection-pushdown-never-changes-a-result)
- [236. test_estimate_deleted.py: the planner's row estimate follows deletes](#236-test_estimate_deletedpy-the-planners-row-estimate-follows-deletes)
- [237. test_generated_columns.py: a columnar table with a generated column returns what heap does](#237-test_generated_columnspy-a-columnar-table-with-a-generated-column-returns-what-heap-does)
- [238. test_export_sink.py: the export byte sink checks every write and never leaves a partial file under the final name](#238-test_export_sinkpy-the-export-byte-sink-checks-every-write-and-never-leaves-a-partial-file-under-the-final-name)
- [239. test_groupagg_table_sizing.py: the vectorized grouped aggregate sizes its table from the estimate, bounded](#239-test_groupagg_table_sizingpy-the-vectorized-grouped-aggregate-sizes-its-table-from-the-estimate-bounded)
- [240. test_fsst_margin.py: the FSST keep/drop margin decides, and never changes content](#240-test_fsst_marginpy-the-fsst-keepdrop-margin-decides-and-never-changes-content)
- [241. test_index_delete_liveness.py: nbtree's deletion passes never destroy an entry for a live row](#241-test_index_delete_livenesspy-nbtrees-deletion-passes-never-destroy-an-entry-for-a-live-row)
- [242. test_rewrite_group_scan.py: a rewrite reads each row group once, not once per row](#242-test_rewrite_group_scanpy-a-rewrite-reads-each-row-group-once-not-once-per-row)
- [243. test_recluster_gate.py: recluster skips its rewrite when the table is already this key's run](#243-test_recluster_gatepy-recluster-skips-its-rewrite-when-the-table-is-already-this-keys-run)
- [244. test_recluster_extent.py: an online recluster records only the extent it rewrote](#244-test_recluster_extentpy-an-online-recluster-records-only-the-extent-it-rewrote)
- [245. test_reader_buffer_reuse.py: the reader reuses one group buffer, and the reuse never shows in an answer](#245-test_reader_buffer_reusepy-the-reader-reuses-one-group-buffer-and-the-reuse-never-shows-in-an-answer)
- [246. test_nologin_workers.py: a NOLOGIN role's background workers connect and act as that role](#246-test_nologin_workerspy-a-nologin-roles-background-workers-connect-and-act-as-that-role)
- [247. test_scan_direction.py: a scrollable cursor over a columnar table never invents a backward answer](#247-test_scan_directionpy-a-scrollable-cursor-over-a-columnar-table-never-invents-a-backward-answer)
- [248. test_qual_order_selectivity.py: skip predicates are probed most-selective-first](#248-test_qual_order_selectivitypy-skip-predicates-are-probed-most-selective-first)
- [249. test_pushdown_report.py: EXPLAIN reports what qual pushdown did, the same way on every columnar node](#249-test_pushdown_reportpy-explain-reports-what-qual-pushdown-did-the-same-way-on-every-columnar-node)
- [250. test_row_triggers.py: row-level triggers on a columnar table fire once per row with the right row](#250-test_row_triggerspy-row-level-triggers-on-a-columnar-table-fire-once-per-row-with-the-right-row)
- [251. test_projection_rename_restore.py: a projection's declaration follows a column rename, so it rebuilds after restore](#251-test_projection_rename_restorepy-a-projections-declaration-follows-a-column-rename-so-it-rebuilds-after-restore)
- [252. test_preimage_rewrite.py: a predicate on date_trunc of a column prunes like the range it stands for](#252-test_preimage_rewritepy-a-predicate-on-date_trunc-of-a-column-prunes-like-the-range-it-stands-for)
- [253. test_pg19_vacuum_options.py: PostgreSQL 19's parallel-autovacuum parameter is accepted on a columnar table and changes nothing](#253-test_pg19_vacuum_optionspy-postgresql-19s-parallel-autovacuum-parameter-is-accepted-on-a-columnar-table-and-changes-nothing)
- [254. test_cost_written_geometry.py: the cost model describes the geometry a table was written with](#254-test_cost_written_geometrypy-the-cost-model-describes-the-geometry-a-table-was-written-with)
- [255. test_encode_invariants.py: encoding invariants for the #155 bulk-load optimisations](#255-test_encode_invariantspy-encoding-invariants-for-the-155-bulk-load-optimisations)
- [256. test_read_stream.py: reads through a read stream return what the synchronous path returns](#256-test_read_streampy-reads-through-a-read-stream-return-what-the-synchronous-path-returns)
- [257. test_parquet_level_width.py: the Parquet level-width helper is single-sourced, unsigned and bounded](#257-test_parquet_level_widthpy-the-parquet-level-width-helper-is-single-sourced-unsigned-and-bounded)
- [258. test_parquet_nested.py: Parquet nested export writes lists and groups that read back as the generator's values](#258-test_parquet_nestedpy-parquet-nested-export-writes-lists-and-groups-that-read-back-as-the-generators-values)
- [259. test_parquet_nested_import.py: nested Parquet import round-trips arrays and composites](#259-test_parquet_nested_importpy-nested-parquet-import-round-trips-arrays-and-composites)
- [260. test_parquet_count_bounds.py: file-declared Parquet counts are bounded before use](#260-test_parquet_count_boundspy-file-declared-parquet-counts-are-bounded-before-use)
- [261. test_parallel_degree.py: identical data in heap and columnar is granted the same parallel degree](#261-test_parallel_degreepy-identical-data-in-heap-and-columnar-is-granted-the-same-parallel-degree)
- [262. test_parallel_flush_optin.py: parallel_flush stays opt-in, and the dispatch metric cannot separate the shapes](#262-test_parallel_flush_optinpy-parallel_flush-stays-opt-in-and-the-dispatch-metric-cannot-separate-the-shapes)
- [263. test_parquet_export_stats.py: the Parquet files we export carry correct per-row-group statistics](#263-test_parquet_export_statspy-the-parquet-files-we-export-carry-correct-per-row-group-statistics)
- [264. test_catalog_filter_reads.py: stats() and sort_status() read a table's storage id once](#264-test_catalog_filter_readspy-stats-and-sort_status-read-a-tables-storage-id-once)
- [265. test_iceberg_malformed.py: the Iceberg reader refuses crafted metadata cleanly, never crashing or hanging](#265-test_iceberg_malformedpy-the-iceberg-reader-refuses-crafted-metadata-cleanly-never-crashing-or-hanging)
- [266. test_iceberg_name_mapping.py: an Iceberg data file with no field ids binds by name mapping](#266-test_iceberg_name_mappingpy-an-iceberg-data-file-with-no-field-ids-binds-by-name-mapping)
- [267. test_iceberg_objstore.py: Iceberg tables read over object storage](#267-test_iceberg_objstorepy-iceberg-tables-read-over-object-storage)

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
| `test_build_once_builds_once_and_then_skips` | the first call builds, the second finds the marker, and the build ran once |
| `test_build_once_serialises_concurrent_callers` | two callers at once, with a build slow enough to overlap: one builds and the other waits on the lock and finds its marker (without the lock both build; #1339) |
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
| `test_an_install_that_leaves_no_library_is_refused` | an install that reports success and leaves no library is refused, and writes no marker; it used to be certified as "absent" (#1339) |
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
| `test_setting_autocommit_on_the_connection_the_tests_use_is_refused` | the wiring arm for #1420: `pgc_conn` itself refuses `autocommit = False` and stays in autocommit, against the real driver |

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
| `test_layer_rejects_a_skip_in_the_test_body` | `pytest.skip()` in the body fails the run (#1339) | the test reads as skipped, exit 0 |
| `test_layer_rejects_an_xfail_in_the_test_body` | `pytest.xfail()` in the body fails the run | "xfailed", which pytest counts as success |
| `test_layer_rejects_an_importorskip_in_the_test_body` | `pytest.importorskip()` in the body fails the run | a skip that bypasses `expect.cannot_run`'s closed list of reasons |
| `test_layer_rejects_an_xfail_marker` | an `xfail` marker is refused at collection, as `skip` is | `xfail_strict` catches an XPASS, but a failing test under xfail is still exit 0 |
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
runtime twin of the declaration rather than a synonym for PASSED. Typed logs show only
that the reader accepts four shapes, so a real suite is also driven down each path and
the reader is fed what `pgc_summary` printed (#1339).

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
first would call those two unaccounted, which is false. Both alternatives are anchored,
and an indented or extended line of either shape is refused (#1339).

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
reddens the moment someone *fixes* something teaches people not to fix things. Each case
reads the exit code as well as the names: 1 for the new suite, 0 for both reports (#1339).

### `test_the_population_partitions_and_prints_its_identity`

`inputs == sum(buckets)` over the registered population, printed per the house rule.
Like the symmetry check's identity it **cannot** be false on the data — the four buckets
are built by successive subtraction from the registered set, so their sum equals it
identically, measured at 0 firings over 400 random four-set inputs while the real bucket
findings fired on 353. What it guards is `comm` reading unsorted input, which produces
buckets that are not a partition at all.

### `test_the_debt_file_is_tracked_and_holds_only_registered_suites`

`test/suites_without_accounting.txt` is tracked so that adding a name is a diff a
reviewer sees — the whole reason it is a file and not a number in the environment. git
must track it, not merely find it on disk (#1339). Every name in it must be a registered
suite.

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
| `test_a_declared_file_that_needs_the_driver_is_named_as_such` | the `needs-the-driver` kind, on a fixture corpus, because no file in the real tree reaches that branch (#1339) |
| `test_the_partition_mode_prints_both_halves` | `--partition` prints each file in the half it belongs to; before this, nothing ran that mode |
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
| `test_the_minted_failure_scan_sees_the_shape` | the scan finds an except handler returning `f'error: {exc}'`, and not `query_error()`, a SQLSTATE, or the shape inside a string literal | a scan that sees nothing would make the corpus arm below vacuous |
| `test_no_test_mints_its_own_failure_text` | no except handler under test/pytest builds an "error..." string from the error's message (#1525 review) | a helper that mints its own failure text is not the producer's prefix, so no refusal fires and two failures with the same message compare equal; on main it finds 32 sites in 22 files |

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
| `test_update_and_delete_are_writes_too` | `UPDATE 0` and `DELETE 0` are writes, not only INSERT, each recorded with its zero count and its own tag |
| `test_a_select_matching_nothing_is_not_a_write` | `SELECT 0` is not a write; **the arm a count-only guard fails** |
| `test_ddl_is_not_a_write` | `CREATE TABLE`, `SET`, `TRUNCATE TABLE`, `DROP SCHEMA` are not writes |
| `test_a_write_that_wrote_rows_needs_no_acknowledgement` | a write that moved rows is recorded, with the count it reported |
| `test_a_write_that_wrote_rows_passes_unacknowledged` | the inner run passes with a write that moved rows and was never named: **the guard refuses a zero, not a write** |
| `test_an_unacknowledged_zero_row_write_fails_the_test` | the inner run fails, and the message names the mode and the command |
| `test_expect_wrote_acknowledges_the_zero` | naming the zero lets a negative control pass |
| `test_expect_wrote_refuses_a_count_that_is_not_a_count` | `rowcount == -1` is refused rather than compared |
| `test_expect_wrote_still_compares` | acknowledging a count does not excuse a wrong one |
| `test_several_writes_and_only_the_empty_one_is_named` | with three writes and one empty, the refusal names the empty one, `#2 UPDATE`, and neither INSERT |
| `test_wrote_refuses_a_statement_that_is_not_a_write` | `expect.wrote` on a `SELECT 0` is refused, not compared |
| `test_the_acknowledgement_names_one_write_and_not_its_twin` | naming one zero does not acknowledge a different identical zero |
| `test_acknowledging_both_identical_zeros_passes` | the control for that arm: naming both is legitimate |
| `test_a_setting_on_the_watched_connection_is_refused_not_dropped` | setting `autocommit` on the watched connection raises, naming the attribute and BEGIN/ROLLBACK, and the real connection keeps its value (#1420) |
| `test_a_setting_on_a_watched_cursor_is_refused_not_dropped` | the same refusal for an attribute set on a watched cursor |

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

### `test_a_mutation_named_with_a_semicolon_is_refused_rather_than_split`

The mutation column is a `;`-separated set, written back sorted, so a `--mutation`
whose text held `; ` was stored as two mutations, one a fragment starting with a space.
Every later merge re-sorted it, and three branches in a row carried the same eight
recovery_minimal_wal rows as a change of their own. `merge` now refuses such a text with
rc 2 and leaves the ledger as it was; the same text with `, and` is recorded whole.

### `test_a_ledger_row_whose_mutation_holds_a_fragment_is_refused`

The writer joins the set with a bare `;`, so a piece with whitespace at an end is a
fragment of a split sentence, never a mutation it wrote. The reader refuses the ledger
by line. Two whole mutations separated by a bare `;` still read.

### `test_the_committed_ledger_names_no_mutation_fragment`

The committed ledger, read by the same reader. Red on the tree before this change at
line 1783, the first of the 12 rows it rewords: 8 from #1427's recovery_minimal_wal
mutation and 4 older harness_selftest rows.

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

### `test_a_row_outside_the_run_majors_is_counted_once_not_twice`

The same set-compare from the other side: a row the run **does** emit, in a part the run
**does** contain, whose ledger majors do not name the running major.

`checkable` requires both conditions, and everything outside it becomes `not checked`. But
`matched` was `set(rows) & now`, computed against every ledger row rather than against the
checkable ones — the wrong side of that line. So such a row landed in `matched` **and** in
`not checked`, the four categories stopped partitioning, and the integrity assertion
refused a scan that had found nothing wrong.

It was not hypothetical. `run_all_versions.sh` reported `FAIL PG19` on a clean tree with
every suite passing: `projection_scan_io` holds eleven rows, three claiming `15;16;17;18`
while their checks still run on 19, giving `11 + 1766 = 1777` against 1774 ledger rows.
PG19 is the one major CI never runs the suites on, so nothing upstream of a local gate
could see it.

The control is what stops the test being satisfied by the wrong fix. A run **on** the
claimed major must still be clean with `not checked=0` — without it the test passes on a
build where `matched` is always zero, which would break every legitimate match.

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

### A premise the ledger has seen go red is refused (#1236)

A check named `premise:` claims no repository change can redden it. A date in its
last-observed-red column says a mutation did. One of the two is false, so the gate
refuses the row and names it. The reviewer then decides: drop the prefix, because the
check is an arm, or move the date to the check that earned it.

The gate decides this from the ledger alone, so it covers every row, not only the rows
the run observed.

| test | what it holds |
| --- | --- |
| `test_the_gate_refuses_a_premise_the_ledger_has_seen_go_red` | a dated `premise:` row makes the gate fail, and the output names the row once |
| `test_a_premise_never_observed_red_passes_the_gate` | the control: the same fixture with `never` passes, so the refusal above is this rule and not another route to rc=1 |

The shell twin is the last section of `test/selftest/410-a-check-must-have-been-red.sh`.

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

**The population**, measured when the arm was written and again on 2026-10-16 over 170
files (#1339):

| shape | when written | 2026-10-16 | state |
| --- | ---: | ---: | --- |
| non-empty by construction (literal, `range`, local literal) | 20 | 112 | cannot be zero-trip |
| derived, loop holds the only assertions | 0 | 1 | already refused |
| derived, **with** assertions outside the loop | 2 | 15 | at risk, now guarded |

Every at-risk loop carried a premise both times, so this arm was **green on arrival**.
That is the point rather than a weakness: the property was true of the corpus and nothing
was holding it there, so what this catches is the next loop added without one. The arms
read these numbers live, so this table records them and does not bound them.

| test | asserts |
| --- | --- |
| `test_every_at_risk_loop_carries_a_coverage_premise` | the corpus itself: every derived loop carrying an assertion has a cardinality premise |
| `test_the_sweep_finds_the_loops_it_is_meant_to_police` | **premise**: the sweep classified loops, because one that parses nothing reports no offenders either. It counts the sweep's own classification (`classify_loops`), not a second walk of its own, so a sweep reading no file reddens it. And the sweep parsed every `test_*.py` in the directory: a sweep reading every fourth file cleared the loop-count floors, and a count tied to the directory does not go out of date (#1339) |
| `test_a_loop_with_no_premise_is_caught` | **removal proof**: the real shape with the premise removed, with the clean control beside it |
| `test_a_premise_in_name_only_is_still_caught` | a name bound to a literal and later rebound to a query is not exempt, and a premise pinning the length at 0 is not a premise, in any argument order (#1339) |
| `test_a_bounded_loop_needs_no_premise` | a literal, a `range` and a local dict's `.items()` are all exempt, because demanding a premise there would be noise a reader edits away |
| `test_the_layer_already_refuses_a_loop_holding_every_assertion` | the measured half, so this file does not claim the whole mode: a planted test in a child pytest is refused, and the sweep classifies a planted loop of that shape as already refused rather than double-reporting it. Both run the behaviour; neither reads source (#1339) |

### Why the rule is looser than the property, and what is left

The honest requirement is *a premise bounding the cardinality of **this** iterable*. What
is enforced is *a counted assertion outside the loop that takes `len(...)` of something*.

The two differ, and the reason is dataflow. `test_harness_deps.py`'s loop iterates
`sorted(found)` while its premise bounds `len(files)` — `found` is built from `files` in
a preceding loop. A rule that demanded the names match would reject correct code, which
is how a guard gets switched off. **So the residual is a loop whose premise bounds the
wrong collection**, which a reviewer catches and a sweep does not. 3.5 names it.

Two narrower holes are closed (#1339). A name counts as non-empty by construction only
when **every** binding of it in the function is a non-empty literal. And a `len(...)`
assertion that pins the length at 0 does not count as a premise, because it says the
loop runs no times: `num` against `0` in either order, `at_most(x, 0)`, and
`at_least(0, x)`.


## 25. test_native_join_runtime_filter.py: serial join runtime filter

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

### `test_a_gating_qual_over_the_join_matches_heap`

A WHERE that names no column, `(SELECT false)` or `(SELECT true)`, over a join to a
heap and to a columnar dimension (#1437). The planner puts a gating Result over such
a join, and the coordinator expected a Hash Join there: a release build raised an
ERROR and an assert build aborted the backend. Each answer must be heap's, with
premises that the same join with no gate takes the coordinator and that heap's
true-gate answer holds rows. Independent of the shell arms.

### `test_an_empty_build_reads_no_fact_group`

An empty build from a large dimension filtered to nothing (#1447). With a dimension
that large, the hash join fetches its first outer row before it builds, so the fact
scan runs. An inner join with an empty build has no rows, so the fact table must not
be read; it was read whole, every row rejected one at a time. Premises check that the
coordinator ran and that the build was empty, and the answer must be heap's.

### `test_an_empty_build_on_one_rescan_does_not_empty_the_next`

A LATERAL whose first outer row builds nothing and whose second builds every key
(#1447). An empty build now ends the join at once, so state left over from the first
execution would answer the second with no rows. Premises check the coordinator and
that heap's answer holds the keys.

### `test_a_fact_qual_conjunction_with_late_materialization_matches_heap`

The shell's fact-qual arms on the default path, late materialization on: a conjunction that also names a non-key column (`k > 100 AND n % 3 <> 0`) must still return heap's rows under the runtime filter coordinator. A premise first shows the qual removes matching rows on heap (100 of 150), so an answer that ignored it would not equal heap's. The late-mat-off test above covers the same shape with late materialization off. Added when this file was renamed to pair with `native_join_runtime_filter.sh` and aligned to the shell's names (#1341).


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

### `test_the_delete_and_update_leave_a_count_neither_gives_alone`, `test_each_count_mode_takes_the_path_it_names`, `test_the_covering_count_agrees_with_the_heap`

An UPDATE appends the new row and masks the old, so the stored per-group count and the visible
count diverge -- which is the arithmetic the metadata path has to get right and a plain scan
gets right for free. Run both ways through `enable_vectorization`, which is what distinguishes
"the fast path is correct" from "the fast path was not taken". A fallback returns the same
count, so each mode also checks the plan: `on` must show the metadata node ("Columnar
Vectorized Aggregates"), and `off` must show `Custom Scan (PgColumnarScan)` without it.

One check per test: the `covering` module fixture takes each mode's path and count pair as
values, so an inverted switch reddens both path checks, as it does in the shell, rather than
stopping at the first. `SET` on the connection rather than the bash suite's `ALTER DATABASE`,
which exists because each psql there is a new session.

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
| `test_every_registered_suite_is_ported_or_declared_unported` | every registered suite with no twin is listed in `unported_suites.txt`, a ported one leaves it, and no listed stem is phantom (#1341); the parity goal (#432) is that file empty |
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
| `test_a_sort_key_with_no_ordering_is_refused` | #1341: a projection sorted on `json`, which has no btree ordering, is refused `0A000` when the back-fill builds its writer, and nothing of it is declared |
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
| `test_the_old_subtraction_goes_negative_on_that_same_input` | the control: three ran minus five accounted is the `-2` the runner printed, counted from the same fixture files the live-shape arm reads |
| `test_the_readers_sort_their_own_inputs` | `comm` on unsorted input yields a wrong set silently, so the sort lives inside the reader |
| `test_the_two_buckets_partition_the_suites_that_ran` | `inputs == sum(buckets)`, and neither bucket is the other's leftover |
| `test_the_summary_block_was_extracted_rather_than_an_empty_range` | an empty extraction runs nothing and reports clean, so the block is asserted before it is used |
| `test_the_printed_breakdown_takes_a_count_a_count_can_take` | the line a reader sees carries no negative, and names the skipped-but-accounted category |
| `test_the_same_block_counts_and_names_a_real_debt` | the same block still counts and names a genuine debt, so it is not printing `0 did not` unconditionally |
| `test_a_ran_count_the_names_do_not_support_fails_the_major` | told 4 ran while the names say 3, the block reports that the breakdown does not add up and fails the major; a block deriving the debt by subtraction would make it add up by construction (#1339) |
| `test_the_tally_function_records_every_suite_that_ran_or_skipped` | `pgc_tally_suite` writes every ran name to `$_acc_ranfile` (three sites) and every skipped name to `$_acc_skipfile`, read inside the function |
| `test_every_comm_in_the_runner_pins_its_collation` | every `comm` on a code line carries `LC_ALL=C`, and no `<(sort ` feeds one on the caller's locale |
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
| `test_a_sort_into_a_file_that_comm_reads_is_caught` | an unpinned `sort` at the start of a command, sorting into a file that `comm` then reads, is caught, and the same sorts pinned are not (#1339) |
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
| `test_a_delete_whose_qual_deletes_another_row_matches_heap` | a DELETE whose qual calls a function that deletes the next row of the same table succeeds and leaves heap's rows (#1394). Its inner DELETE writes the outer marks at scan start and its own at statement end, in one command, and the second write failed with "attempted to update invisible tuple" |

## 66. test_temporal.py: a columnar table enforces temporal constraints like a heap

Port of `temporal.sh` (#432). PostgreSQL 18 adds `WITHOUT OVERLAPS` primary keys, 19beta2 had
`UPDATE ... FOR PORTION OF` (19beta4 removed it, #1337), and both run through the index and
constraint machinery pgColumnar integrates with. The suite builds a heap/columnar pair and
asserts they agree.

**FOR PORTION OF is gated on the grammar, not the major (#1337).** A version gate ran those
arms on 19beta4, which cannot parse them, and failed both storage types alike. Worse,
`for portion of result matches` passed there: it compared the two tables, and two updates
that both failed leave them equal. Both twins now probe the grammar with an `EXPLAIN` that
writes nothing. The result arm also requires the portion under its new key, so a failed
update fails it. The shell suite records the three arms as SKIPs where the clause is
absent. Here they are `test_for_portion_of`, unrunnable there and listed per major in
`expected_unrunnable.txt`.

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
| `test_temporal` | heap and columnar agree on accepting non-overlapping rows and rejecting an overlapping one, the accepted rows are really there, and the PK contents match |
| `test_for_portion_of` | where the grammar has `UPDATE ... FOR PORTION OF`: the update applies on both, the tables match, and the portion is in the columnar table under its new key. Its own fixture; unrunnable where the clause is absent (15 to 18, and 19 from beta4) |

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
| `test_an_insert_takes_the_unique_key_lock_only_while_it_is_enabled` | an insert into `u` takes an advisory class with `enable_unique_insert_lock` on that it does not take with it off, read from its own session (the unique-key lock never taken no longer passes; #1339) |
| `test_a_duplicate_key_is_still_rejected` | a duplicate key is rejected -- this does not assert the lock: with the lock never taken, even a concurrent duplicate waits on the first insert and gets 23505, because the btree's uniqueness check waits on the winner (#1339) |
| `test_the_row_identity_lock_and_its_settings` | an UPDATE holds a class-104 row-identity lock with `enable_row_update_lock` on and none with it off; `row_lock_buckets` refuses `SET` (55P02) because it is fixed at server start; a 200000-row UPDATE holds `row_lock_buckets` class-104 locks, one per bucket, not one per row (#1341 item 16) |
| `test_the_row_identity_lock_cannot_be_reached_from_sql` | every advisory lock an open UPDATE holds is outside field4 1 and 2, and a second session holding each of those tags through `pg_advisory_xact_lock(int4, int4)` does not block an UPDATE of the same row (#1341 item 56) |
| `test_bulk_parallel_writer_set_by_hand_still_serialises_a_first_write` | two sessions with `bulk_parallel_writer` on each make the first write to a committed, empty table; the second waits for the first, and both rows land (#1341 item 55) |
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
| remove every method name from the sentence | `the features page names at least one index access method` fails; the loop runs zero times and cannot pass vacuously |
| **`builds btree, hash and gist but NOT spgist`** | `the claim bullet holds the claim and nothing else` fails, in both harnesses |
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
| `the features page names at least one index access method` | the extractor found something, so a green loop means work happened |
| `the claim bullet holds the claim and nothing else` | the bullet is the one form a name-reader can read, so the names ARE the claim |
| `premise: the fixture holds every row` | the corpus is what the probes assume |
| `premise: <am>, which the page claims, has a probe in this suite` | a claim on the page without evidence here is a failure, not a skip |
| `<am> builds an index over a columnar table` | the method builds |
| `premise: the <am> predicate matches rows at all` | the comparison below is not two empty sets agreeing |
| `<am> answers its operator with the same rows the scan returns` | the index path and the scan path agree |
| `an overlap query reaches the row through a GiST index` | the plan, asserted, for the claim that ranges have an index path |

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
| `test_a_key_well_under_the_gate_takes_the_node` | a key well under the gate (#1292) takes the node, and it answers the high-cardinality key as heap does |
| `test_an_output_built_on_a_key_falls_back_and_still_answers` | `f(key)` output and a bare `GROUP BY` with no aggregate both fall back, and the answer is still right |
| `test_a_non_deterministic_collation_key_falls_back` | the fallback happens and the grouping is case-insensitive; declines under the shell suite's own name where ICU is absent |
| `test_sum_real_matches_heap_rather_than_returning_zero` | blocker 1: the node fires, matches heap, and is not zero |
| `test_a_float_sum_that_overflows_raises_as_core_does` | two finite float8s, or reals, whose sum is infinite raise `22003` through the node, as core's `float8pl` and `float4pl` do with it off (#1341 item 50) |
| `test_a_gating_where_is_honoured_rather_than_dropped` | blocker 2: a one-time filter makes the node decline, and both the false and true gates answer as heap does |
| `test_avg_float8_overflows_the_way_core_overflows` | the node fires and raises out of range, which a row comparison cannot express |
| `test_degenerate_inputs_produce_no_groups` | a predicate matching nothing, and an empty table |
| `test_the_path_pays_for_the_folding_it_does` | the isolated #349 arm and its two node premises |
| `test_the_group_estimate_bound_is_accurate_and_narrow` | the bound is 12 and is an upper bound; an informed estimate is untouched; with no time bound, with only a lower one, and for `date_bin`, the estimate is between the true group count and four times it, from the zone maps (#1292); a lookalike function and a non-time key get no bound |
| `test_the_bound_reads_the_zone_maps_in_one_catalog_scan` | planning a bucketed GROUP BY over 50 row groups scans zone_map and its index once in all, not once per group (#1304) |
| `test_a_mixed_timestamp_predicate_never_narrows_the_bound` | a cross-type predicate under a non-UTC TimeZone never narrows the estimate below the true group count (#1292); the matching-type one still gets a tight bound |
| `test_on_by_default_and_gated_on_an_upper_bound` | the grouped path is on by default and taken only when an upper bound on the groups (the row count, or the time buckets) is within the cap: a bucket query over more rows than the cap is taken, a plain key over more rows is declined (answering as heap does) until the cap is raised, and a skewed key that fools ANALYZE is declined and answers every group; a cached plan run under a lower cap stops at the runtime cap with its hint (#1292, #1330 review) |
| `test_a_null_key_counts_as_a_group_in_the_bucket_bound` | three minute buckets fill the bucket bound and NULL rows add a fourth group; under a cap of 3 the path is declined and all four groups answer, where a bound without the NULL group errors (#1330 review) |

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

A COVERING PROJECTION MUST NOT DECIDE WHICH COLUMNS GET STATISTICS (#1276). `analyze()` resolved its storage id with `WHERE s.relation_oid = rel`. `relation_oid` is **not unique** in `pgcolumnar.storage` -- `storage_pkey`, on `storage_id`, is the only unique index -- because a covering projection gets its OWN row carrying the BASE table's `relation_oid`. `SELECT ... INTO` then took whichever row heap order handed back first without complaining about the second. Same root cause as #1210, which fixed the C lookup and does not touch this one.

THE HARM IS MISSING STATISTICS, NOT WRONG ONES. The values come from reading the column; `sid` is only a gate at `alpha5:1532`. A projection NARROWER than its base fails that gate for every column index it does not carry, and `CONTINUE` skips those columns in silence with a successful return. Measured: five columns with statistics became two.

A TWO-COLUMN BASE CANNOT SEE IT. The projection carries zone maps for both indexes, the gate passes either way, and the statistics come out identical to the control -- a fixture returning a stable, plausible, meaningless number. Five columns covered by one is the shape that separates them, and the indexes each storage actually carries are read from the catalog and asserted before the arm runs, because a skip and an absence look identical downstream.

TWO PREDICTIONS WERE WRITTEN DOWN BEFORE THE FIX AND BOTH ARE ARMS. The `sid IS NULL` branch keeps a real case -- a never-written columnar table has a readable metapage and no storage row, so it is still refused, `P0001`, measured rather than assumed -- and a `WITH DATA` matview starts succeeding, because its `relation_oid` is orphaned until the first `REFRESH` (#1275). **That second one is a behaviour change and not #1275 being fixed**: the orphan is still in the catalog for every other reader, and the arm carries premises saying so.

THE REFUSAL IS CAPTURED AS A VALUE, NOT A NOTICE. A `DO` block's `RAISE NOTICE` lands on the message stream rather than in a result set, so an arm reading it gets the empty string back -- which `check_text` refuses as "a side is empty", fail-closed. A helper that RETURNS the sqlstate puts the answer where the arm can see it, and a premise requires that helper to report a **success** too, or "refused" is the only thing it could ever say.

THE FIX IS NOT ONE LINE, AND A GREP IS WHY IT LOOKED LIKE ONE. `pgcolumnar."analyze"` was last defined in `pgcolumnar--1.0-alpha--1.0-alpha2.sql`, a shipped artifact that must not change, so an alpha4 install carries that body until an upgrade script replaces it. A grep for `FUNCTION pgcolumnar.analyze` cannot match `pgcolumnar."analyze"` -- the dot needs exactly one character and the text has two, `.` and `"` -- so the absence read as "new in alpha5". `native_upgrade_converge.sh` refused the incomplete change on all four upgrade paths with one diverging function hash, and the whole corrected body now goes into `pgcolumnar--1.0-alpha4--1.0-alpha5.sql` as well.

### Every test

| test | what it holds |
| --- | --- |
| `test_analyze_function` | the 500,000-row fixture: null_frac exact on a fraction core's sample cannot express, n_distinct exact from one column and as the negated fraction past core's 10% rule (#1339), the statistics it does not compute left in place with core's values (#1339), the histogram's true maximum and its smallest non-most-common bound, no most-common value inside the histogram, every bound a value the column holds, the three MCV frequencies over total rows rather than non-null rows, the per-column statistics target honoured, with exactly that many most-common values (#1339), and a column at `SET STATISTICS 0` left alone |
| `test_a_named_column_that_does_not_exist_is_refused` | #1341: `analyze(rel, ARRAY['a','nosuch'])` is refused, naming `nosuch`, and writes no statistics, not even for `a`; naming `a` alone does |
| `test_histogram_bounds_are_a_positional_stride` | eleven distinct rows at target 3, where core's `values[floor(i*(nv-1)/(nhist-1))]` and `percentile_disc`'s `ceil(p*nv)-1` land on different values; the expectation comes from an independent oracle AND a hand-worked figure that must agree first |
| `test_null_frac_counts_live_rows_not_ones_a_delete_left` | #485: null_frac came from the zone maps, which count what was WRITTEN, so one pg_stats row carried two statistics normalised against different populations; both must imply the same table |
| `test_the_documented_statistics_are_the_ones_written` | the list in `docs/sql-reference.md` parsed and compared against what the function populates, plus the two negatives the doc states in prose |
| `test_a_covering_projection_does_not_decide_which_columns_get_statistics` | #1276: five columns covered by a one-column projection, the zone-map indexes each storage carries read from the catalog before the arm runs, the heap-order flip asserted rather than assumed, that every column keeps its statistics whichever row is first, that the refusal branch still fires for a never-written table, and that a `WITH DATA` matview now resolves |


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

    SELECT count(*) FROM r_col WHERE span && tstzrange('2020-01-02 00:00+00','2020-01-02 01:00+00')

A zone map recorded `minimum` and `maximum` only. Those are the range type's btree
ordering, which sorts by lower bound and then upper bound, so the largest range is
not the one reaching furthest right: a unit holding `[1,2)` and `[3,100)` has the
same `maximum` as one holding `[1,2)` and `[3,4)`. Neither `&&` nor `@>` can be
answered from that. `zone_map.max_upper` records the greatest upper bound in three
states: a bound, NULL for no bound (an empty range), and present but empty for an
unbounded range. The scan skips a unit only when the summary proves it cannot match.

The port of `test/range_pruning.sh`, by name, one check per test (#1341, #1339). The
fixture is the shell's: 50,000 rows in one stripe, which is one row group of five
vectors, so pruning shows at both levels. Module fixtures build the tables once.

| test | what it holds |
| --- | --- |
| `test_the_clustered_fixture_holds_the_rows`, `test_the_heap_mirror_holds_the_same_rows` | both tables hold 50,000 rows |
| `test_the_range_column_is_summarised_in_several_units`, `test_every_unit_carries_an_upper_bound`, `test_a_non_range_column_carries_none` | the range column has at least 4 units, each with an upper bound; no other column has one |
| `test_the_columnar_side_returns_the_heaps_rows` | 8 heap-oracle arms, compared by count and two sums: the one-hour, week, point, never-met and late windows, the scattered table, and the two unbounded-range probes |
| `test_the_first_three_oracle_probes_are_not_vacuously_empty` | the window, week and point probes match rows on the heap |
| `test_the_overlap_qual_is_pushed_down`, `test_the_zone_map_is_probed_for_it` | the overlap qual is pushed down and probes the zone map |
| `test_a_clustered_overlap_skips_vectors`, `test_a_containment_probe_skips_vectors` | overlap and containment skip vectors |
| `test_a_late_overlap_skips_the_four_vectors_only_upper_bounds_rule_out` | the late window skips exactly the 4 vectors only upper bounds rule out |
| `test_a_window_no_row_can_meet_reads_no_chunk_group` | the never-met window reads no chunk group |
| `test_a_scattered_column_skips_nothing` | the documented zero |
| `test_the_unbounded_row_really_is_matched` | the unbounded row is matched on the heap |
| `test_the_all_empty_table_is_summarised`, `test_an_empty_range_contributes_no_bound` | the all-empty table is summarised, and records NULL |
| `test_an_unbounded_range_records_a_present_but_empty_bound` | the unbounded unit records a present but empty bound |
| `test_a_discriminating_collation_was_found`, `test_the_collated_pair`, `test_a_collated_overlap_returns_the_heaps_rows` | a range compares under its declared collation: the type's collation differs from text's, the pair holds the same rows and is summarised, four overlaps and the row ids agree with the heap |

**THE UNBOUNDED ARMS COULD NOT FAIL UNTIL #1341.** In the shell, the unbounded tables
lacked the `payload` column the fingerprint sums, so both sides errored and `check`
compared "" with "". Once that was fixed, an empty row in the same unit kept them
green: an empty range sorts first, and a unit whose minimum is empty is never
pruned. And a separate `INSERT` put the unbounded row in a row group of its own.
With all three fixed, a writer that ignores unbounded ranges reddens both arms in
both twins.

**THE COLLATION IS DISCOVERED, NOT ASSUMED.** A host with no collation ordering
differently from the database default declines the collation tests by name.

Independent of `test/range_pruning.sh`: neither file reads or runs the other.

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
| `test_expire_refuses_what_it_cannot_honour` | #1341: a non-owner holding DELETE (`42501`), a retention column that is not a time type (`42804`) and a dropped one (`42703`) are refused with every row still there, on tables whose rows are all past retention; the owner's `expire` is the control that retires them |
| `test_a_half_declaration_is_no_retention` | #1341: a retention column without an interval, or an interval without a column, is accepted by `set_options` and refused by `expire` with `55000`, every aged row kept |
| `test_a_null_retention_value_keeps_its_group` | a live NULL the maximum cannot speak for |
| `test_an_index_only_scan_does_not_return_retired_rows` | the visibility-map bits over a group `expire` removed |
| `test_a_non_positive_retention_is_refused` | a cutoff in the future would retire live groups: `22023`, with a positive control |
| `test_a_deleted_null_stops_pinning_its_group` | `null_count` is a write-time record, not a live count |
| `test_a_date_retention_column_truncates_toward_keeping` | the rounding, which only this arm notices |
| `test_the_cutoff_is_the_sessions_own_date` | the cutoff is the session's, asserted as an identity rather than as a difference, in UTC and in a zone chosen at run time to be on another date (#1339) |
| `test_a_timestamp_retention_column_uses_the_sessions_local_time` | a `timestamp` column's cutoff is the session's local now: in UTC+14, exactly the groups wholly before it are retired (#1339) |
| `test_expire_records_what_it_retires_and_nothing_else` | `expire` writes exactly the groups it retires to `pgcolumnar.retired_group` (#1294); a rolled-back `expire` and a `compact_rewrite` record nothing; dropping the table drops its rows |
| `test_a_no_op_expire_reads_the_extent_not_every_zone_map` | #1310: with the retention column's extent (#1304 option 5) past the cutoff, a no-op `expire` makes 0 zone-map scans where it made one per group; the same groups retire with or without the extent; aged rows written in `expire`'s own transaction retire; a column left with only NULLs needs no reads. Counted in a fresh connection, with the test schema's `search_path` carried over. Seen red first: 40 scans |

The shell twin is `test/ttl_expire.sh`, 67 checks since #1310, all in the mutation ledger. The two halves share no code:
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

AND pgcolumnar.storage THROUGH THE RELATION'S OWN METAPAGE (#1210). `pgcolumnar_written_stripe_row_limit` looked its row up by `relation_oid`, which has no index, so every planned query over a columnar relation swept the catalog. A sequential scan stops at the first match, so the cost was the row's POSITION rather than the catalog's size: the oldest columnar relation never paid for what came after it and the newest paid for all of it, unbounded.

AN INDEX ON `relation_oid` WAS THE ISSUE'S OWN SUGGESTION AND IS THE WRONG REMEDY. The column is not unique -- `storage_pkey`, on `storage_id`, is the only unique index on the table -- because a covering projection gets its own storage row carrying the BASE table's `relation_oid`. Measured: a table written at `stripe_row_limit` 150000 with a projection added at 7000 resolved to 150000 with the base row physically first and 7000 with it second, no value altered in between. A non-unique index returns the same ambiguous pair in index order instead of heap order, so it would only have made the wrong answer arrive faster.

THERE ARE TWO KINDS OF ARM HERE, AND THE SECOND ONE TOOK A CORRECTION TO ARRIVE AT. Two arms measure the WORK -- blocks of `pgcolumnar.storage` per planned query. One measures the ANSWER, which is what the change is for: the plan's cost must not change when the base storage row is moved later in the heap by an update that alters no value. A first attempt read a stable cost on a 30,000-row table with no useful index and concluded the wrong limit was invisible in the plan. It was the fixture -- those shapes never reach a group-sensitive term. All three call sites turn the limit into a GROUP COUNT, and on a shape that reaches one the plan is priced 301.29 against 43.86. Measured over 300,000 rows: 2078.84 against 1134.84 on an index scan and 2300.00 against 91.23 on a qual over the projected column, four of nine shapes moving.

ON A PRIVATE DATABASE for this test too, and for a reason the other two share: `pgcolumnar.storage` is per database, so inside the corpus the page count is whatever the rest of the run left behind and the position premises stop meaning what they say.

Measured on the unfixed tree in this harness's own database, 6 pages: 6 blocks for the newest relation with `seq_scan = 1`, against 2 for the oldest. With the fix, 3 and 3.

COUNTED IN THE PLAN'S OWN TRANSACTION (#1298). The block counts and the `storage` scan counts come from `pg_stat_get_xact_blocks_fetched` and `pg_stat_get_xact_numscans`, read before and after the plans in one transaction, not from the shared `pg_statio_all_tables`, which takes in other sessions' reads. Section 81 has the measurement.

### Every test

| test | what it holds |
| --- | --- |
| `test_catalog_plan_index` | the measured table's row count, that the filtered scan returned every row, and that planning a `count(*)` and a two-relation join reach `pgcolumnar.storage` through `storage_pkey` |
| `test_planning_costs_nothing_on_the_default_configuration` | that this fixture really is the default configuration with both catalogs empty, that forcing the probe costs something so the instrument measured, that planning costs less than probing both, and that it touches the empty catalogs **not at all** |
| `test_planning_still_probes_a_populated_catalog` | that `projection` is larger than the threshold so the two paths differ, that the measured table owns a small share of it, that reading it whole costs something, and that planning costs less than reading it whole |
| `test_the_written_limit_lookup_does_not_sweep_the_storage_catalog` | that the newest relation's storage row is far enough into the catalog to tell the two routes apart, that the two relations are far enough apart, that both plans reached `pgcolumnar.storage`, that planning over the newest did **not** sequentially scan it, and that the newest costs no more catalog work than the oldest |
| `test_the_written_limit_does_not_depend_on_heap_order` | that a covering projection gave the table a second storage row, that the two rows disagree about `row_group_limit`, that this query shape is priced differently under the two limits, that the no-op update moved the base row, that the base row was first before it and the projection's row after, and that the planned cost does **not** change |
| `test_a_storage_reading_takes_in_none_of_another_sessions_reads` | that a statement which reads no catalog itself, inside which a second session scans `pgcolumnar.storage` and exits, reads zero blocks of it; and, as its control, that the second session's reads do reach the shared view (#1298) |

## 81. test_catalog_delete_index.py: retiring a row group costs no more for a bigger database

Port of `catalog_delete_index.sh`. `delete_group_rows()` opens its catalog from a `const char *tableName` PARAMETER and `PgColumnarDeleteGroupMetadata` calls it five times, so one `systable_beginscan` in the source was five sequential reads per retired group at run time. The `pgcolumnar` catalogs are shared by every columnar table in the database, so each read was charged for every other table's rows.

THE FIX IS NOT "USE THE INDEX", so the test cannot be "was the index used". A probe is a btree descent plus a heap fetch plus two catcache lookups, which on a catalog of a few pages is MORE work than reading the whole thing and far less on a large one. Both sizes occur in one installation. So these arms measure at three catalog sizes and each carries only the claim its size can support.

THEY ASSERT THE WORK AND NEVER THE ACCESS PATH: buffers counted in the measured statement's own transaction, heap and index both (#1298). An earlier version asserted `seq_scan == 0` and `idx_scan >= 1` per catalog; run against the build that chooses by catalog size -- cheaper at every size measured -- it failed 13 of 21 arms, the same 13 a full revert reddens. A test that cannot tell a revert from an improvement fires on correct code, and a guard that fires on correct code gets switched off.

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

COUNTED IN THE STATEMENT'S OWN TRANSACTION, NOT FROM `pg_statio_all_tables` (#1298). That view is shared: another session's reads reach it whenever that session flushes or exits, and `pg_stat_force_next_flush()` flushes only the caller's own counts, so no reset or flush keeps them out. Measured on PG18 with a second session scanning the catalogs during one compaction: the shared reading rose by 28 to 120 buffers, the transaction's own reading by -3 to +18. `_cat_blocks` reads `pg_stat_get_xact_blocks_fetched` before and after the statements in one transaction, so pending counts left by the fixture's writes cancel out as well. The vacuum test still reads the shared view: VACUUM cannot run in a transaction block.

`pg_stat_reset()` is database-wide. The corpus runs serially within a worker.

### Every test

| test | what it holds |
| --- | --- |
| `test_retiring_a_group_costs_no_more_for_a_bigger_database` | per phase: three identical targets, the emptied groups retired, every survivor kept, all six catalogs read, and two compactions at the same setting agreeing well inside the floor. Then that the default does less work than probing every catalog at six pages, less than reading every one whole at twenty-two and at seventy-six, and that its cost grows far less with the database than reading whole does |
| `test_vacuum_reads_less_of_row_group_than_reading_it_whole` | that the vacuum walked this table's groups, that two vacuums at the same setting agree, and that its `row_group` work is below what reading that catalog whole costs -- the `PgColumnarComputeAllVisibleGroups` read, which the compaction path never reaches |
| `test_dropping_a_table_reads_less_of_the_catalogs_than_reading_them_whole` | that a DROP costs less catalog work than reading the seven catalogs whole, and -- below the threshold -- less than forcing a probe. The subject is `delete_rows_by_storage_id`, one parameterised line serving seven catalogs on the DROP/TRUNCATE path, which nothing covered before (#1207). Measured in buffers, never the access path, and on a fixture sixty tables wide because `pgc_own_db` gives this file a private database where the shell twin shares a cluster |
| `test_a_catalog_reading_takes_in_none_of_another_sessions_reads` | that a statement which reads no catalog itself, inside which a second session (run by `COPY ... TO PROGRAM`) scans `row_group` and `column_chunk` and exits, reads zero; and, as its control, that the second session's reads do reach the shared view (#1298) |

## 82. test_rewrite_storage_oid.py: a type rewrite keeps the storage row on the live table

Port of `rewrite_storage_oid.sh`. `ALTER COLUMN ... TYPE` writes `pgcolumnar.storage.relation_oid` as the transient relation and then drops it. The live table's OID no longer finds the row. The rows themselves stay readable.

AND THE SAME DEFECT ON A SECOND STATEMENT (#1275). `CREATE MATERIALIZED VIEW ... AS` with data builds a transient, fills it and swaps, exactly as a rewriting `ALTER` does, so the storage row named a relation that no longer existed. Measured before the fix: the row named a dropped 16527 while the matview was 16523. `REFRESH MATERIALIZED VIEW` already repaired it, because that node type was in the repair gate; `CreateTableAsStmt` was not.

THE `CREATE TABLE ... AS` ARM IS THE CONTROL THAT NARROWS THE FIX, and it is the reason the remedy is restricted to `objtype == OBJECT_MATVIEW` rather than applied to the parse node. Same node, no defect -- it fills the relation it created instead of swapping a transient in. Without that control the fix would reasonably have been written for `CreateTableAsStmt` as a whole, which is broader than anything measured asked for, and the premise now refuses the narrowing the day it stops being true.

THE `REFRESH` ARM GUARDS THE PATH THAT ALREADY WORKED. It repaired this before the fix, through a different node type, so an arm there is what says the new arm did not displace it.

### Every test

| test | what it holds |
| --- | --- |
| `test_rewrite_storage_oid` | the storage row matches the live regclass before the rewrite, the row count survives, an unrewritten table still matches itself, and the rewritten table's storage row matches the live regclass afterwards |
| `test_a_matview_created_with_data_points_at_itself` | that the matview and the `CREATE TABLE AS` table both hold their rows, that `CREATE TABLE ... AS` points at itself (the control that narrows the fix), that a matview created `WITH DATA` points at itself, and that `REFRESH` still does |
| `test_the_repair_gate_leaves_untouched_partitions_alone` | on a parent with two columnar partitions, that `ALTER TABLE ONLY`, `ATTACH`, `DETACH` and `DETACH CONCURRENTLY` read no block of the partition they did not name, against a control that a real read of it is visible. The gate used to open that partition with `NoLock`, an Assert on assert builds and an unlocked open on release builds; the fix skips any relation the statement holds no lock on. A session the ALTER aborted reports `None`, which the arm refuses. Also that a type rewrite through the parent still repoints both partitions' storage rows, so the skip does not skip what it must repair |

## 83. test_catalog_scan_index.py: twelve metadata scans do not read their catalog whole

Port of `catalog_scan_index.sh` (#1207). Twelve `systable_beginscan` calls in `columnar_metadata.c` passed `InvalidOid` on a key that an existing index starts with, so each read its whole catalog to answer a question about one table. There are three on `free_space` (the compaction reuse and both end-truncation reads), one on `row_group` (`row_group_exists`, reached by EVERY `DELETE`), four on `storage` (three existence checks in `PgColumnarInsertNativeStorageRow` and the sorted-extent update), and four on `projection_declaration` (list, rename a column, drop one, drop all). The fix routes each through `pgcolumnar_scan_index_oid()`.

IT ASSERTS THE PATH, AND SECTION 81 SAYS NOT TO, so the reason has to be stated. A path assertion is wrong where declining a probe can be right, which is on a catalog below `pgcolumnar.index_min_blocks`. Every arm here runs after 350 other projected tables have put all four target catalogs above that threshold, and a premise asserts it against the live setting. The resolver never declines on such a catalog, so a sequential scan of it during the operation can only be one of these sites still reading whole. Buffers and `idx_scan` are printed beside each arm as diagnosis.

TWO COST MEASURES WERE TRIED FIRST, AND A REVERT SURVIVED BOTH. Comparing the default against `index_min_blocks` forced high let a full revert through on four arms of nine. The setting also governs the scans converted earlier (#1213, #1237), so forcing it made those read whole too, and the forced reading came out worse than the unfixed code's. Growth from an empty database to the populated one failed on correct code instead. A probe costs about three buffers where a one-page catalog costs one, and `vacuum_sorted` makes 28 `storage` scans, so the fixed build grew by more than the pages added.

EVERY ARM HAS AN EFFECT PREMISE, AND BOTH SILENT FAILURES IT GUARDS AGAINST HAPPENED. `compact()` cannot retire rows deleted by a transaction that has not committed, so a fixture that deleted and compacted in one transaction left no free range to measure. `pgcolumnar.truncate()` refuses a transaction block, so a multi-statement call raised and read zero. "No sequential scan" is exactly what an operation that never ran reports. Each arm therefore asserts what the operation changed, and that it reached the catalog at all.

THE END-TRUNCATION TARGET HAS NO PROJECTION. A projection is its own storage in the same file, written after the base's groups, so its live data sits behind the base's last group and there is no tail to reclaim. Measured: with the projection, `truncate()` returns 0 and both free ranges stay; without it, 3 blocks are reclaimed and one range is left.

Removal proof, on the shell twin at PG 17: the full revert reddens all eleven path arms, and reverting each of the twelve sites ALONE reddens at least one arm named for the operation that reaches it. This twin aborts at its first failure, so under the revert it reports the first arm (`storage seq_scan=2`).

The two halves share no code and no fixture. The shell suite adds 400 tables with `int` keys and retires groups 0 and 2; this one adds 350 with `bigint` keys and retires groups 1 and 3, and its effect values differ throughout. This half flushes statistics before each reset, for the reason section 81 gives.

### Every test

| test | what it holds |
| --- | --- |
| `test_catalog_scans_do_not_read_their_catalog_whole` | each of the four catalogs at or above `index_min_blocks`; then, for eleven operations (an insert into an existing table, into a table created in the same transaction, a `parallel_copy` load, a one-row delete, `compact_rewrite`, `truncate()`, `vacuum_sorted`, renaming a projected column, `TRUNCATE`, `drop_projection`, `DROP TABLE`), that the target held two free ranges, that the operation took effect, that it reached its catalog, and that it made no sequential scan of it |

## 84. test_native_format.py: the format stamp is a range, and an append raises it

The port of `native_format.sh`, by name, one check per test (#1341). The `nf` module fixture runs the shell's sequence once, in its order, and records every reading; each test asserts one of them.

THE METAPAGE VERSION IS PLANTED THROUGH A C HOOK BOUND AT TEST TIME, `pgcolumnar_debug_set_metapage_version`, as the shell binds it and `test_native_gap.py` binds its own. Re-stamping the current major must read cleanly; a major of 99 must be refused by `PgColumnarReadMetapage`, naming `99.0`.

#1130 stopped writing a validity bitmap for a chunk with no nulls, and an alpha4 binary misreads such a chunk without refusing it. Measured with the real alpha4 `.so`, 40 single-row fetches gave 9 NULL values for rows that hold one. So this build stamps `format_version` 2, and alpha4, which refuses any stamp but 1, refuses the table. The check became a range, `1 <= v <= 2`, so this build still reads what alpha4 wrote.

THE FIRST WRITE RAISES THE STAMP, AND A READ DOES NOT. A table stamped 1 that this build appends to holds chunks alpha4 misreads, so the stamp has to move on that write. The arms set a stamp of 1 by catalog `UPDATE`, which is what an alpha4 table looks like, and check that reading leaves it at 1 and appending raises it to 2.

FOUR ENTRY POINTS, AND THE FOURTH NEEDS THE CUSTOM SCAN OFF. The stamp is checked in the sequential scan, the aggregate scan, the group-aggregate scan and the by-row fetch. With the custom scan on, an index query is served by pgcolumnar's own node and never reaches `pgcolumnar_fetch_row`. That path's check was deleted outright on the shell twin and every arm still passed.

REFUSED BY SQLSTATE, `0A000`, not by the absence of a wrong answer. Stamps 3 and 0 are both refused on all four paths.

`RESET ALL` IS NOT USED after the fetch path's settings, because it also drops the private `search_path` that `pgc_conn` sets. The first draft did this, and every later query failed `42P01` instead of measuring anything.

Removal proof, on this twin: making the append path skip the raise reddens `appending to a table stamped 1 raises its stamp` (got 1, want 2).

THE PART-4 INDEX ARMS NEED THE CUSTOM SCAN OFF TOO (#1341). The shell ran that query with it on, so with the fetch path's stamp check deleted, "an index-scan fetch of a decoded column fails" and the two message arms on the same query stayed green: the scan-open guard refused it instead. Both twins now turn it off, and that mutant reddens all three.

"Backend survives" is read on a new connection, as the shell's `q` reads it.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_native_format_version_is_2`, `test_the_native_vector_length_is_1024` | the stamped identifiers |
| `test_a_diverse_type_round_trip_matches_heap` | eight storage classes read back in order, as the heap returns them |
| `test_the_table_reads_at_the_version_this_build_wrote`, `test_the_metapage_major_this_build_writes_is_still_2` | the metapage version this build writes reads cleanly, re-stamped or not |
| `test_a_read_of_the_bumped_version_fails`, `test_the_failure_is_the_format_version_guard`, `test_the_error_names_the_offending_version`, `test_the_backend_survives_the_rejection` | a metapage major of 99 is refused by its guard, by name, and the server survives |
| `test_the_table_reads_at_the_native_format_version_this_build_writes`, `test_a_future_native_format_version_is_refused`, `test_the_failure_is_the_native_format_version_guard`, `test_the_native_format_error_names_the_offending_version`, `test_the_backend_survives_the_native_format_rejection` | a format_version of 99 is refused on the seq scan, the aggregate and the row fetch, by its guard, and the server survives |
| `test_the_fixture_was_stamped_by_this_build`, `test_a_table_stamped_1_is_read_on_all_four_paths`, `test_a_table_stamped_1_returns_its_rows`, `test_reading_a_table_stamped_1_does_not_raise_its_stamp`, `test_appending_to_a_table_stamped_1_raises_its_stamp`, `test_the_appended_table_still_reads_whole` | the range's lower end, and an append raising the stamp |
| `test_a_stamp_above_this_builds_is_refused`, `test_a_stamp_below_min_readable_is_refused`, `test_the_backend_survives_the_range_rejections` | stamps 3 and 0 are refused with `0A000` on all four paths |

## 85. test_native_agg.py: min/max over timestamptz read no data

A partial port of `native_agg.sh`, covering the arms #1293 added. "What is the latest reading?" is `max(ts)`, and an ungrouped, unfiltered `min`/`max` is answered from the row groups' zone maps. The suite already proved the metadata node is chosen and that the answer matches heap, on `int4` and `text`. Neither shows the data went unread: with every group forced dirty, the node is still chosen, every parity arm stays green, and `max(ts)` reads 414 blocks against a decode's 403 (shell twin).

THE ARM READS THE TABLE'S OWN BLOCKS from `pg_statio`, against a control that must decode the column (`max(at + interval '0 s')`). A tenth rather than zero, because the metapage and its neighbours are read either way: 13 blocks here, and 15 on a 10M-row table measured on #1293. The margin `decode - 10 * reading` is what is compared, so a failure reports by how much it fell short.

THE ROW COUNT IS A PREMISE because the shell twin's first draft overflowed `int` computing its timestamps. The `INSERT` failed without a word, and every arm measured an empty table: the decode "control" read 3 blocks. Parity is then checked again after deleting the newest and oldest rows, because a zone map still records deleted values (#149).

Removal proof, on this twin: forcing every group dirty reddens `max(ts) reads under a tenth of the blocks a decode of ts reads` (got -1487, wanted at least 1).

The rest of the shell suite was ported for #1341, every check by its name: the `int4`, `text` and `sum`/`avg` parity arms with the metadata node asserted first, the node winning against a parallel plan with no Gather (#133), `count(*)` reading no zone maps against an aggregate that must read them, and the filtered, `sum(bigint)` and empty-table fallbacks. Removal proof for those: `count(col)` folded from the row count instead of the value count reddens `count(col,nulls)` in both twins.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_timestamptz_table_holds_every_row` | **premise**: the table holds every row inserted |
| `test_max_ts_plans_the_metadata_agg_node` | `max(ts)` over timestamptz plans the metadata node |
| `test_decoding_ts_reads_the_tables_blocks` | **control**: a decode of `ts` reads at least 10 blocks |
| `test_min_max_ts_read_under_a_tenth_of_a_decode` | `max(ts)` and `min(ts)` each read under a tenth of the decode's blocks (parametrized) |
| `test_timestamptz_parity` | `max` and `min` equal heap's, before and after deleting rows at both ends (parametrized) |
| `test_the_deletes_removed_the_same_rows_at_both_ends` | the deletes removed rows, the same rows from each table |
| `test_count_min_max_with_no_where_are_the_vectorized_aggregate` | **premise**: with no WHERE, the aggregate is the vectorized node |
| `test_a_where_with_no_column_in_it_gives_heaps_answer` | `(SELECT false)`, `now() < ...` and `current_user = ...` each give heap's answer (#1435) (parametrized) |
| `test_heaps_answer_to_a_false_gate_is_no_rows` | **premise**: heap's answer to a false gate is no rows |
| `test_the_aggregate_uses_the_metadata_node` | `count(*)` and sum/min/max plan the metadata node (parametrized) |
| `test_the_aggregate_answers_as_heap` | count, sum, avg, min and max over int4 and text, the multi-aggregate, the filtered aggregate and the `sum(bigint)` fallback each equal heap (parametrized, 13 names) |
| `test_the_metadata_node_beats_a_parallel_plan` | the node wins against a parallel plan, for `count(*)` and the multi-aggregate (#133) (parametrized) |
| `test_the_metadata_plan_has_no_gather` | neither plan has a Gather (parametrized) |
| `test_count_reads_no_zone_maps` | `count(*)` reads no zone maps |
| `test_an_aggregate_that_needs_zone_maps_reads_them` | `count(*), sum(id)` reads at least one: the measured delta, so the zero above cannot pass on a counter that never moves |
| `test_a_filtered_aggregate_is_not_the_metadata_node` | a filtered aggregate falls back |
| `test_an_empty_table_counts_zero` | an empty table counts 0 |
| `test_an_empty_table_aggregates_to_null` | an empty table's `sum` and `min` are NULL (parametrized) |

ONE CHECK PER TEST (#1339). Three module fixtures take every reading in order, and each test asserts one, so a red cannot stop the next check from running. Measured with #133 restored (the metadata path priced at the scan's cost) on PG17: the shell reddens 4 arms, the parallel-plan and no-Gather pairs. The earlier twin, three tests holding 40 checks, reported 1. This one reports the same 4.

## 86. test_format_downgrade.py: an alpha4 binary refuses what this build wrote

Port of `format_downgrade.sh` (#1137). 1.0-alpha5 stops writing a validity bitmap for a chunk with no nulls, and an alpha4 binary misreads such a chunk without refusing it: with the real `v1.0-alpha4` library, 9 of 40 single-row fetches returned NULL for a row that holds a value. The fix is the format stamp. A table this build writes carries `format_version` 2, alpha4 refuses any stamp but 1, and a table this build only reads keeps 1.

THE CLAIM IS ABOUT TWO BINARIES, so this test uses both, and it runs only when it is alone: see the second test below. It builds `v1.0-alpha4` from the tag, which it fetches when the checkout is shallow, as CI's is. It writes two tables with the old build, upgrades to this tree, reads one, appends to the other and creates a third. Then it puts the old library back under the upgraded catalog, which is what a package rollback does. `native_format.sh`'s arms stand in for alpha4 with a catalog `UPDATE`; this one does not.

ON A CLUSTER OF ITS OWN, AND THE PREFIX IS PUT BACK EXACTLY. The session cluster shares the install prefix. So before anything is installed, every `pgcolumnar*` file in the library and extension directories is saved, bytes and mtime, and restored in a `finally` by atomic rename. Anything the old install added is removed. The mtime matters because the harness compares the library's mtime with the postmaster's start. Nothing is written into a library in place, because a copy over a library a running postmaster has mapped corrupts it. Measured: the prefix's library afterwards is byte-identical to the one this tree built.

Refusal is asserted by SQLSTATE `0A000`, on a scan and on an index fetch with the custom scan off. The index fetch is the path that returned the silent NULLs.

Removal proof: with `COLUMNAR_NATIVE_FORMAT_VERSION` set back to 1, the stamps read `1 1 1` and the test fails there. On the shell twin the same mutation reddens five arms, and alpha4 answers `ok` on the appended table's index fetch. That `ok` is the silent read this test exists to refuse.

### Every test

| test | what it holds |
| --- | --- |
| `test_an_alpha4_binary_refuses_what_this_build_wrote` | the old build stores both tables; the libraries differ; the upgrade succeeds and this build reads the old table; the stamps are `1 2 2` for read, appended and new; on the old library again, the table only read is still read and fetched by index, and the appended and new tables are refused with `0A000` on a scan and on an index fetch |
| `test_the_library_swap_declines_beside_other_workers` | the decision that makes the swap decline beside other xdist workers, driven with the environments pytest-xdist sets: a serial run and a one-worker run proceed, a run beside three other workers declines, and a worker with no stated count is treated as not alone. A backend another worker started during the swap would load alpha4 against this tree's catalogs and fail in an unrelated test; atomic renames protect only processes that had already mapped the library (found by @jdatcmd). Under `-n`, the swap test declines as `UNMET_PRECONDITION` with that reason, so a parallel run ends INCOMPLETE naming it rather than risking someone else's test |

## 87. test_latest_reading.py: the latest-reading recipe does what the document says

Port of `latest_reading.sh` (#1293). The recipe's SQL is read out of `docs/how-to.md`'s "Find the latest reading per device" section, by this file's own extraction (Python, not the shell twin's awk). So an edit to the document is what runs: a recipe that stops returning the right rows, or stops being cheap, turns this red. The approach is jdatcmd's, from #1288.

Measured on #1293 with 10 million rows and 1,000 devices: `DISTINCT ON` took 5.6 s, the btree plus `LATERAL` form 45 ms, and the same `LATERAL` without the btree 12.6 minutes. Asserted here: each documented form returns exactly the rows `DISTINCT ON` returns; with the btree the `LATERAL` form reads under a tenth of the table; and without it the table is read about once per device, which is the trap the document warns about.

A ZERO IS NOT A MEASUREMENT. On the shell twin, the red run before the section existed passed "under a tenth of the table" on empty SQL: a query that reads nothing reads under a tenth of anything. Reading at least one block is now its own premise.

THE FIXTURE NEEDS MANY ROWS PER DEVICE. The `LATERAL` form costs about a block per device whatever the table's size, so at 5,000 rows per device it read 45 blocks of a 177-page table and "a tenth" failed. Here it is 36,000 rows per device.

On a private database, built once by a module fixture, because the document names its tables, `readings` and `devices`.

Removal proof: the document's `LATERAL` edited to `ORDER BY ts` (ascending) returns each device's first reading, and `the LATERAL form returns what DISTINCT ON returns` fails. On the shell twin that is the only red, on every major.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_documents_three_recipe_blocks_were_found` | **premise**: the document's three recipe blocks are found |
| `test_the_table_holds_every_row` | **premise**: the table holds every row |
| `test_without_the_btree_the_lateral_form_reads_the_table_once_per_device` | the trap the document warns about: without the btree, the `LATERAL` form reads the table about once per device |
| `test_the_group_by_form_returns_what_distinct_on_returns` | the `GROUP BY` form returns the rows `DISTINCT ON` returns |
| `test_the_group_by_form_reads_the_table_once` | the `GROUP BY` form reads at least one block and no more than the table, as the document says (#1339) |
| `test_the_documents_index_exists` | **premise**: the document's index exists |
| `test_the_lateral_form_returns_what_distinct_on_returns` | with the index, the `LATERAL` form returns the rows `DISTINCT ON` returns |
| `test_the_indexed_lateral_form_read_the_table_at_all` | **premise**: the indexed `LATERAL` form read at least one block |
| `test_with_the_btree_the_lateral_form_reads_under_a_tenth_of_the_table` | with the index, the `LATERAL` form reads under a tenth of the table |

ONE CHECK PER TEST (#1339). A module fixture runs every step in the shell's order, the trap before the document's index exists, and records each reading; each test asserts one. Measured with the document's index statement replaced by `SELECT 1` on PG17: the shell reddens "the document's index exists" and "reads under a tenth of the table". The earlier one-test twin reported only the first. This one reports both.

## 88. test_storage_row_repair.py: the alpha5 release notes' repair procedure works as written

Two alpha4 defects (#1211, #1275) left a table's `pgcolumnar.storage` row naming a relation that no longer exists, and the upgrade does not repair existing rows. `RELEASE_NOTES_1.0-alpha5.md` gives a query that finds them and two repairs: `REFRESH MATERIALIZED VIEW` for a view, and a same-type `ALTER COLUMN ... USING` for a table. Instructions are validated by performing them, so this performs them.

THE BROKEN STATE IS MADE, NOT INHERITED: each storage row is pointed at a table created and dropped for the purpose. That matches what a real alpha4 build left behind, measured on PG17. THE QUERY IS READ FROM THE NOTES, so an edit to the document is what runs.

SCOPED TO ITS OWN STORAGE IDS. `pgc_conn` gives a private schema in a shared database, and the pgcolumnar catalogs are per database. So the count asserted is of the rows the notes' query returns for the ids this test damaged, the original and the post-repair ones both.

Removal proofs on PG17: disabling `PgColumnarRetargetStorageRelation` reddens it at the premise. Editing the notes' query from `NOT EXISTS` to `EXISTS` reddens it too.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_release_notes_repair_procedure_works` | the notes give the query; it finds neither row before the damage and both after; the damaged table still reads every row; each repair repoints its row; the query then finds nothing; the table and the view hold the same rows |

## 89. test_docs_cross_database_query.py: no documented query asks another database for its extensions

`pg_extension` is a per-database catalog. A statement that reads `pg_database` and tests `pg_extension` answers for the current database only, so it lists every connectable database or none. `docs/installation.md` shipped exactly that as the way to find the databases that need the upgrade (#1288). Only fenced SQL is read, split into statements, so prose that explains the rule cannot trip it. It needs no cluster, so it is in `NO_CLUSTER`.

Red with the old query restored (got 1, want 0). Its premise is red when the fence pattern is broken and nothing is scanned (got 0, wanted at least 10). That is the case where the arm alone would pass on nothing.

### Every test

| test | what it holds |
| --- | --- |
| `test_no_documented_query_asks_another_database_for_its_extensions` | at least ten SQL statements were read; none reads `pg_database` and tests `pg_extension` |

## 90. test_partman_columnar.py: pg_partman makes columnar partitions with one setting

Port of `partman_columnar.sh` (#1291). The owner's question was whether pg_partman works with columnar partitions on every major; if it does, we document and test that. Measured with pg_partman 5.4.1 on PostgreSQL 15 to 19. Out of the box every partition it makes is heap, even on 17+ where the parent carries `USING pgcolumnar`. With `default_table_access_method = pgcolumnar` every partition is columnar, including the `DEFAULT` one and those made ahead, and retention leaves no pgcolumnar catalog row behind.

THE TRAP IS PINNED, AS A PREMISE. Which access method core gives a partition, whether pg_partman makes a `DEFAULT` partition, and the `ACCESS EXCLUSIVE` its maintenance holds on the parent are core's and pg_partman's behaviour. No change to this repository can redden them, so by the owner's #1236 rule they are premises (found by @jdatcmd reviewing #1302). They are still asserted, because `docs/how-to.md` states them, and a pg_partman release that changes one must turn the document red with it. The arms are what pgcolumnar decides: what a scan returns and what the drop hook leaves in the catalogs.

"READ BACK AS HEAP DOES" HASHES EVERY ROW IN ORDER. An unfiltered `count`/`sum`/`min`/`max` is answered from zone maps and would never exercise the scan, so a scan mutation could not have reddened it.

ON A PRIVATE DATABASE, because the recipe is a database setting and the retention arms count the whole storage catalog. The lock is read inside one explicit `BEGIN`/`COMMIT`. A first draft set `autocommit = False` on the fixture's connection, which did not open a transaction, and read `none`.

pg_partman missing is `MISSING_DEPENDENCY`, an unrunnable that fails the run, never a pass. CI installs `postgresql-N-partman` from PGDG.

Removal proof, one mutation per arm, every major on the shell twin: the drop hook keeping the `storage` row reddens `retention leaves no pgcolumnar storage row behind` (20); keeping `row_group` rows reddens `and no row-group row behind` (20); a scan that drops its last row group reddens `and read back as heap does` and `and the rows land in columnar partitions`. A first scan mutation dropped a group only when there were two or more. Every partition here holds one group, so it changed nothing, and was replaced.

### Every test

| test | what it holds |
| --- | --- |
| `test_pg_partman_makes_columnar_partitions_with_one_setting` | pg_partman installed; without the setting every partition is heap; with it, a `DEFAULT` partition exists and every partition is columnar; the rows land columnar and read back as heap does; maintenance drops partitions past retention, creates the ones ahead columnar, and leaves no orphaned storage or row-group row; and it holds `AccessExclusiveLock` on the parent |

## 91. test_capability_decline.py: a missing pyarrow declines by name, and nothing declines silently

The pytest half of selftest parts 600 and 610, held by the Python harness's own machinery. It does not source, call or read `lib.sh`, `capability_sweep.sh` or either part: the twins are independent (the first version drove `lib.sh` from Python, and the harness-independence inventory refused it).

The owner's rule of 2026-10-12 (#1215): a missing capability may be bypassed as a named skip, the run stays green, and no test may be silent. On this side that is `expect.declines(capability, *, name)`, a fourth record verdict `DECLINED`, and `expect.require_module(module, *, name)`. Five holes are closed, from OffgridwithJD's review of #1286:

1. where `PGC_REQUIRE` names the capability, as CI does, a decline FAILS;
2. a decline must be the test's first outcome, and no check may follow it;
3. only an ABSENT module declines: a broken install (an `ImportError` from inside) fails;
4. a test file that imports pyarrow outside `require_module` is refused, in each of six import spellings, and each one is proven seen;
5. the run's accounting line carries `+ N declined` when there are any, and each decline is printed by name with its capability.

Written red first: all seven failed before `pgc_vacuity` had the methods. The four failure arms pin their failure's MESSAGE, because a missing method also fails an inner test, so outcomes alone could not tell "refused for the right reason" from "crashed".

### Every test

| test | what it holds |
| --- | --- |
| `test_a_missing_pyarrow_declines_by_name_and_the_run_stays_green` | the inner run passes and exits 0; the accounting line carries `+ 1 declined`; the decline is printed with its node id and capability |
| `test_pgc_require_turns_a_decline_into_a_failure` | with `PGC_REQUIRE=pyarrow` the same test fails, and says pyarrow is required |
| `test_a_decline_after_a_counted_check_is_refused` | a decline after a recorded check is refused as not the first outcome |
| `test_a_check_after_a_decline_is_refused` | a check recorded after a decline is refused |
| `test_only_a_declinable_capability_may_decline` | `btree_gist` cannot decline; it is not in the closed list |
| `test_a_broken_install_fails_rather_than_declines` | a `pyarrow` that raises `ImportError` from inside fails with its own error |
| `test_a_missing_dependency_of_pyarrow_fails_rather_than_declines` | a `pyarrow` that cannot import its own dependency raises `ModuleNotFoundError` naming that dependency, and fails rather than declines; shown red with `require_module`'s `.name` check deleted (OffgridwithJD, review of #1286) |
| `test_every_unguarded_import_spelling_is_refused` | each of six spellings, `__import__` among them, is named and refused; a file using `require_module` is not |
| `test_an_unguarded_import_is_refused_under_xdist_too` | under `-n 2` the controller names the unguarded import once and the run is refused, as the serial run is: workers ship their sites back through `workeroutput` (#1305) |

## 92. test_tid_range_scan.py: a ctid range reads only the row groups that hold it

Port of `tid_range_scan.sh`. Before this, the access method had no `scan_set_tidrange`, so a ctid range was a per-row filter: on #1294, `WHERE ctid >= '(600000,0)'` on a 10-million-row table matched 0 rows and still read all 67 row groups. The rollup that #1294 proposes needs "the newest row groups only", and so does anything else that bounds ctid.

The oracle is the same query with `enable_tidscan = off`, which is that per-row filter. The cost is the table's own blocks from pg_statio, read under `max(body || 'y')`, because an unfiltered max is answered from the zone maps and reads no data. The tail range must read under a fifth of what a decode of the whole table reads, and at least one block, because a zero is not a measurement.

THE BOUNDS THAT MATTER ARE THE ONES NO ROW HAS. A block holds 291 rows, at offsets 1 to 291, but a bound may name offset 0 or anything up to 65535. The executor turns `ctid < '(B,0)'` into an upper bound of `(B-1,65535)`. The first build read those offsets unclamped, so offset 400 named a row in the next block. The shell twin's arms for those bounds went red against it, which is how it was found.

CURSORS. PostgreSQL counts a TID range scan as able to scan backward, so it puts no Material above one under a cursor that may scroll, and a backward fetch reached the access method, which refused it (`0A000`). Found by @jdatcmd reviewing #1306. A planner hook now puts a Material above a cursor's plan that holds a TID range scan of a columnar table. The arms: a SCROLL cursor's `FETCH LAST`, `BACKWARD` and `ABSOLUTE` match the filter's plan, and a plain DECLARE (which the portal makes scrollable) answers a backward fetch correctly. Both were red before the hook.

Also held: rows inserted earlier in the same transaction are in the range; a rescan with new bounds per outer row (the plan premise is taken on the very query compared, because a draft whose lateral output went unreferenced planned no scan at all); deletes inside the range; and compaction, which gives a thinned group's surviving rows new, higher row numbers. On PostgreSQL 19, a parallel TID range scan with two workers is held too. Below 19 that test is `UNSUPPORTED_MAJOR` and listed in `expected_unrunnable.txt`.

On a private database, because `pg_stat_reset()` resets the whole database's counters.

Removal proofs, each red on PostgreSQL 19 on the arm named:
- without the group restriction: the tail-range cost arm;
- with an upper offset past 291 unclamped: `ctid < '(B,0)'`;
- with a lower offset past 291 unclamped: `ctid >= '(B,500)' AND ctid <= '(B+1,700)'`;
- without the boundary-row filter: the tail range and the parallel arm;
- with the reader kept across a rescan: the rescan arm;
- without the shared parallel claim: the parallel arm, which returned every row twice.
- without the planner hook: both cursor arms;
- with the hook for SCROLL cursors only: the plain-cursor arm.

THE MATERIAL'S NODE ID. `set_plan_references` numbers the main tree and then the subplans from one counter, so the Material's id must be above the subplans' ids too; the first version took the main tree's largest and repeated a subplan's id (found by @jdatcmd). An assert build checks that every id in the final plan is unique and raises an error otherwise. So `a SCROLL cursor whose query has a subplan fetches backward as the filter's plan does` was red (`XX000`) before the fix on assert builds. On a release build it checks the fetches alone.

### Every test

| test | what it holds |
| --- | --- |
| `test_a_ctid_range_reads_only_its_row_groups` | the table has its rows and many groups; a ctid range plans a TID range scan; eleven ranges, including offsets 0, 291, 500 and 65535, return what the per-row filter returns; a rescanned scan does too; a SCROLL cursor and a plain cursor fetch backward correctly, and so does a SCROLL cursor whose query has a subplan; rows inserted earlier in the transaction are in the range; the tail range reads at least one block and under a fifth of the table; deleted rows stay deleted; after compaction moves rows, ranges over the old and new addresses return what the filter returns |
| `test_a_parallel_tid_range_scan_returns_what_the_filter_returns` | PostgreSQL 19+: the range runs as a parallel TID range scan with two workers launched, and returns what the filter returns. Below 19, `UNSUPPORTED_MAJOR` |

## 93. test_partition_lifecycle.py: partition_create_ahead keeps a time-partitioned parent ahead of now

Port of `partition_lifecycle.sh` (#1297). The drop half, `partition_drop_plan`, returns its statements rather than running them: `DETACH ... CONCURRENTLY` refuses to run inside any function, so each statement is run at top level in its own autocommit session, as `\gexec` would. `pgcolumnar.partition_create_ahead(parent, width, ahead)` makes columnar partitions, each `width` wide and contiguous with the highest existing upper bound, until they reach now plus `ahead` widths. It uses a standalone CREATE then ATTACH, so the parent is held SHARE UPDATE EXCLUSIVE, never ACCESS EXCLUSIVE. A DEFAULT partition is refused (`55000`).

THE SESSION'S ZONE IS WHERE THE PROTOTYPE BROKE. A `timestamp` key holds wall-clock time. The prototype read its bounds as timestamptz in the session's zone and wrote the next ones converted from UTC. In America/New_York that left a four-hour gap between partitions, so a row at the boundary had nowhere to go; and daily partitions made from Tokyo started at 15:00. The arms continue from hand-made partitions in sessions west and east of UTC, and check contiguity, routing at every boundary, coverage of the session's local now, and alignment to the width in the key's own wall clock. This file uses six-hour widths, and Asia/Kolkata for a half-hour offset.

Four inputs from @jdatcmd's review of #1308, each an arm red against the first version: a stored generated column (LIKE without INCLUDING GENERATED, `42804` at ATTACH); a 63-character name (the date suffix truncated and the second partition collided; the twin uses 31 two-byte characters so truncation must never split one); a call creating more than 1000 partitions (it ran 11 minutes and failed `53200`; now refused with `22023` before any DDL); and a MAXVALUE bound (an overlap; now refused with `55000`).

Removal proofs, each red in both twins: the prototype's zone handling; `CREATE TABLE ... PARTITION OF` (the lock arm); no DEFAULT refusal; starting from now rather than the highest bound (a second call fails); heap partitions; stopping one width short (the coverage arms); a minute's gap between partitions (the contiguity and boundary arms); and one mutant per #1308 fix. The `one_short` mutation also reddened "rows within the covered range go in", so that check is not a premise (#1236).

### Every test

| test | what it holds |
| --- | --- |
| `test_partition_create_ahead` | from nothing it reaches now plus the lead, columnar, contiguous and one width wide; a second call creates nothing, and asking further ahead creates only the difference; rows land in columnar partitions; the parent is never held ACCESS EXCLUSIVE; west of UTC, for both key types, partitions continue a hand-made one contiguously and a row at every boundary routes; east of UTC, a timestamp parent covers the local now and starts on the width's boundary; inputs that broke the first version (a generated column, a 63-character name, more than 1000 partitions, a MAXVALUE bound) are handled or refused before any DDL; a DEFAULT partition is refused and nothing is created |
| `test_partition_drop_plan` | the drop half: `partition_drop_plan` lists `DETACH ... CONCURRENTLY` and `DROP` for the partitions wholly before the cutoff, in range order, from partitions created newest first. A cutoff on an upper bound includes the partition, and one before them all plans nothing. Running the plan, each statement top-level, leaves the rest with their rows and drops the storage rows. A `timestamp` key with a `timestamp` cutoff plans the same in Tokyo and Denver, and a cutoff of the other type is refused (`42804`). MINVALUE is listed once past, and MAXVALUE never. Quoted schema and partition names run as returned. A sub-partitioned partition is planned whole and dropped with its children. A tiered partition is planned with `DROP FOREIGN TABLE`, and its Parquet file stays. A partition whose name holds the drop block's dollar-quote tag drops as planned. A detach cancelled in its second phase is planned as `FINALIZE` and dropped. The DEFAULT and non-timestamp parents are refused (`55000`) |
| `test_partition_create_ahead_runs_on_the_callers_own_privileges` | not SECURITY DEFINER: a role that does not own the parent, holding USAGE and CREATE on the schema, is refused `42501` and creates nothing; a non-superuser owner creates partitions ahead on a parent of its own |

## 94. test_approx_aggregates.py: hll and tdigest over columnar tables, the compatibility statement

Port of `approx_aggregates.sh` (#1296 phase 2). Phase 0 on #1296 decided that approximate distinct counts and percentiles come from the `hll` and `tdigest` extensions for 1.0, so this pair is the tested compatibility statement for them; docs/how-to.md names the versions.

Independent of the shell suite: its own generator (other multipliers, the same 200,000 rows and 50,000 devices, because the check names state them), its own SQL. `compare_to_bash` grades the pair 14 literal matches, 0 missing.

THE HEAP MIRROR IS WRITTEN FROM THE GENERATOR, never copied from the columnar table. The shell suite first built it by `CREATE TABLE ... AS SELECT * FROM ax`, which reads through the scan under test: with a mutation dropping one row in a thousand, every equality stayed green on all five majors. Writing both sides from the generator turned seven checks red under the same mutation.

The bounds against the exact answer are premises, as pg_partman's are: they state the extension's own error, and a change here that broke them would break the equalities first.

hll 2.19 does not build against PostgreSQL 19, so `test_hll_over_columnar` is `UNSUPPORTED_MAJOR` there and listed in `expected_unrunnable.txt`. CI installs both extensions from PGDG for 15 to 18.

### Every test

| test | what it holds |
| --- | --- |
| `test_tdigest_over_columnar` | the columnar table returns every row written; the tdigest aggregate reads through the columnar scan; its p99 over columnar equals heap's exactly; within 0.1% of the exact percentile; a tdigest stored per day in a columnar table still answers within 0.1% |
| `test_hll_over_columnar` | the hll aggregate reads through the columnar scan; the sketch over columnar is byte-identical to heap's; the estimate is within 6.9% of the exact count; a stored hll comes back byte for byte, and the union of daily sketches counts what one sketch counts |

## 95. test_docs_how_to_sections.py: the pg_partman how-to keeps its own heading

An edit that inserted the approximate-aggregates section before the pg_partman how-to replaced its heading instead (3627327, #1315). Every docs check passed, because the steps were still valid Markdown, filed under "Count distinct values and percentiles approximately". The pytest half of the docs_style.sh arm of the same name, written independently: it splits the how-to on its level-two headings and reads the section's first paragraph. It needs no cluster, so it is in `NO_CLUSTER`.

Red on main at d208bce in both twins (got 0, want 1); green with the heading restored.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_how_to_keeps_its_pg_partman_section_under_its_own_heading` | the how-to has a "Manage time partitions with pg_partman" section, and it opens with the pg_partman paragraph its steps belong to |

## 96. test_autovacuum_expire.py: the daemon expires retention tables only when told to

Port of `autovacuum_expire.sh` (#1290). `pgcolumnar.autovacuum_expire` is off by default, the owner's decision on #1290: `expire` deletes rows, so turning the daemon on must not start deleting them by itself. With both switches on, each pass runs `expire` on every table that declares a retention, in its own transaction, before compaction.

Independent of the shell suite: its own schema, connection, SQL and polling, and its fresh-row count differs (500 rows against the suite's 1000). The check names match, so `compare_to_bash` grades the pair one-for-one (19 of 19).

WHERE THE PORT DIFFERS. The shell suite starts its server with the daemon on and a 2 s naptime in `postgresql.conf`. This corpus shares one cluster per worker, so the settings arrive by `ALTER SYSTEM` and a reload, and the switches are read back from a fresh session. Because the cluster is shared and the daemon visits every database, teardown resets every setting the module changed, each reset on its own, so a build without the switch cannot leave the others in force.

THE YIELD ARM NEEDS A TABLE WHERE `expire` IS THE ONLY VERB DUE. `ex_hold` has a retention and no deletes or sort key, so a ShareUpdateExclusiveLock the daemon worker holds on it is `expire`'s. The other retention tables are dropped first: with the hold on, the worker spends 30 s inside `expire` on each of them, and the first version's poll gave up before the worker reached `ex_hold`.

Written red first: on main the fixture raises `unrecognized configuration parameter "pgcolumnar.autovacuum_expire"`, and all five tests error on it.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_switch_exists_and_is_off_by_default` | the launcher runs; the switch reads `off` while the daemon is `on`; a manual `expire` on the same fixture retires its two aged groups |
| `test_with_the_switch_off_the_daemon_expires_nothing` | the daemon compacts a deleted-heavy table in the window, so it is running passes, and the aged table keeps all 2000 aged rows |
| `test_with_the_switch_on_the_daemon_expires_only_what_aged` | the aged rows go; the 500 fresh rows stay; a table with no retention keeps every row and is never named in a skipped line; the log names what was retired; later passes that retire nothing write no LOG line for the table |
| `test_a_failing_expire_does_not_undo_the_compaction` | `expire` on a table whose retention column was dropped fails with `42703`, and the compaction due on that table still lands |
| `test_the_daemon_yields_the_lock_it_holds_inside_expire` | the worker holds ShareUpdateExclusiveLock inside `expire`; a session's ACCESS EXCLUSIVE is granted within 8 s of a 30 s hold, and the log shows the cancel |
| `test_a_table_dropped_mid_pass_is_skipped_and_the_pass_goes_on` | while the worker holds `ex_hold`, `ex_gone` is dropped; after the cancel the same pass logs `ex_gone` as skipped and still expires `ex_after` |
| `test_the_daemon_cuts_a_date_retention_at_its_databases_time_zone` | one row group per day: the first surviving day is the daemon's cutoff in the server's TimeZone, then in a changed one after a reload, and in a database-level TimeZone, which overrides the server's; two zones 25 hours apart, read before and after the passes so a midnight between cannot flake it |

## 97. test_rollup.py: incremental rollups fold appended row groups, recompute changed ones, and equal a full recompute

Port of `rollup.sh` (#1294, phases 1 and 2). `create_rollup` makes a view over a partials table; `refresh_rollup` folds the rows of the row groups appended since the last refresh, in ONE statement, so the group list, the folded rows and the recorded groups share a snapshot. The design, its twelve points and the owner's decisions are on #1294.

THE ORACLE is a full recompute of the same GROUP BY, every time. Exactness was fixed by the design: count, integer sum, min, max and the avg of an integer column exact; the avg of `float8` within a relative 1e-12. This file uses an integer avg; the shell twin a float one.

What the arms found while the code was written:
- A lateral per-group subquery was pulled up into a join, and a TID range scan is built only from a relation's own restrictions, so the refresh read every row group: 114 blocks of a 103-block table for one new group. `OFFSET 0` keeps it a scan: 15 blocks.
- `pgcolumnar.vacuum` gives a table a new storage id and may keep its group numbers, so the source's identity is the storage id.
- Every arm run as a superuser bypassed the row-level security on the rollup catalogs. An owner and a stranger are now ordinary roles; PUBLIC has no USAGE on schema `pgcolumnar` (by design, docs/administration.md), so the test grants it as an administrator would.
- The first draft's source load overflowed `int` and left the table empty, and three arms passed on emptiness; each has a premise that requires a real measure now.

From @jdatcmd's review of #1319: the refresh restated 291 rows a block in SQL, which holds only at the default block size, so the TID range of each group now comes from `pgcolumnar.row_tid` (the access method's own mapping), and an arm requires every group's range to hold exactly its rows. NULL group keys and NULL timestamps have an arm in each twin, and writing it showed both twins' digests dropped any row with a NULL (`||` then `string_agg`); they use `concat_ws` over `coalesce` now. Retention keeps history: a group `expire` retired leaves the snapshot and its buckets keep their rows (#1314's `retired_group`).

A role that cannot SELECT the source cannot create a rollup of it (42501). `create_rollup` has no check of its own for this: it reads the source's storage id through `get_storage_id`, which refuses such a caller, and the arm was red only when that check was removed. `entry_point_privilege.sh` buckets `create_rollup` as relation-taking by its second argument and `row_tid` as taking no relation.

Phase 2, dirty buckets: a delete, an update or a `compact_rewrite` in the source makes the buckets of the changed row groups dirty, and the refresh recomputes them from the source in the same statement, folding new groups into the clean buckets only. A bucket that holds retained history is never recomputed: such a refresh is refused (`0A000`) and writes nothing, including when the history is retired by that same refresh. Arms: a delete (and no partial outside the changed groups' range rewritten), an update moving rows to another bucket, a delete plus an append straddling its bucket, `compact_rewrite`, a deleted row with a NULL ts, the two history cases, a day bucket refreshed in Europe/Berlin across its DST switch, and the record kept tidy (a recounted group, a forgotten one, retired groups dropped). Writing the DST arm showed that the first fixture's bucket began after the switch, because this file's ORIGIN is 06:00 UTC, so the arm was green under the mutation it was for; the rows were moved into the bucket that contains the switch.

Phase 2 mutations, each red on PG17 at the arm the shell twin names: new rows folded into dirty buckets too, every bucket recomputed, no history refusal, blocking on stored history only, a rebase without ranges, the NULL bucket never dirty, the width added in the session's zone, new groups recorded without ranges, no recount, no forget, retired groups kept in the record, and the retired ranges never stored.

Phase 1 removal proofs, each red on its arm: no `OFFSET 0` (the cost arm), no isolation check, no lock (two refreshes double-fold), no identity check, no changed-group check, the folded groups recorded from a fresh read (rows committed mid-refresh lost), min combined as max, no row-level security, a rebase that clears the rollup, `=` in place of `IS NOT DISTINCT FROM` (only the NULL arm), retired groups kept in the snapshot, and `row_tid` with 290 rows a block.

### Every test

| test | what it holds |
| --- | --- |
| `test_rollup_phase_one` | the rollup equals a full recompute after the first refresh, appends with late rows, a bulk append, and a rebase; a refresh with nothing new folds nothing; rows committed during a refresh are folded by the next, once; two concurrent refreshes fold each row once; zones agree; a refresh after a small append reads under a fifth of the source; a rewritten source is refused until `rebase_rollup`; after a delete, an update, a straddling append, `compact_rewrite` or a deleted NULL-ts row the rollup equals a full recompute, rewriting no partial outside the changed groups' range; a recompute that would reach retained history is refused with `0A000` and changes nothing; a day bucket is recomputed whole across a DST switch; a heap rollup behaves the same; REPEATABLE READ is refused; an owner who is not a superuser creates and refreshes a rollup, and another role sees none of it; bad definitions are refused with `22023`; each group's TID range holds exactly its rows |
| `test_a_rollup_and_its_source_are_dropped_together` | the view depends on its source, as a view over it would (#1376): `DROP TABLE` of the source is refused (`2BP01`), `CASCADE` takes the view, its partials table and its catalog rows, and `DROP VIEW` takes the partials and the rows. A role that cannot read a table cannot tie a rollup row to it (`42501`), so it cannot block that table's drop. Nor can it name another role's table as its partials (`42501`, and that table survives its view's drop), or a non-view as its rollup (`42809`); and a written row's view, partials and source cannot be changed (`0A000`) |
| `test_rollup_dump_and_restore` | a dump carries a rollup's definition; after a restore into another database, and into the same one after DROP EXTENSION, the restored source has the recorded storage id, and the owner's refresh is refused with `55000` naming `rebase_rollup`, the rollup untouched; after the rebase a refresh folds nothing, then exactly the rows appended since, and the rollup equals a full recompute (#1377) |
| `test_min_and_max_keep_each_columns_collation` | #1341: min and max over a `"C"` column and an en_US column equal a recompute after a refresh that folds new rows into an existing bucket, where the two collations disagree; the partials columns carry the default collation, so the fold's own comparison is what is tested. No en_US collation: `cannot_run` |
| `test_row_tid_refuses_a_row_number_below_one` | `row_tid(0)` and `row_tid(-1)` are refused with `22003` and `row_tid(1)` is accepted. A test of its own, last in the file: without the floor `row_tid(0)` crashed the backend |

## 98. test_timeseries_recipes.py: the gap-fill, LOCF and interpolation recipes answer what they claim

Port of `timeseries_recipes.sh` (#1296). Phase 0 on #1296 found that core SQL expresses gap filling, last observation carried forward and linear interpolation fast enough on columnar, so the deliverable is three recipes in `docs/how-to.md`, and this file holds each one to a reference.

THE RECIPES ARE READ FROM THE DOCUMENT, as the shell suite reads them: the first `sql` block under each heading, run as written against the table the document names. Everything else is this file's own. The rows are generated in Python, and the reference is computed in Python from those same rows, so it shares nothing with the shell suite's plpgsql reference. `compare_to_bash` grades the pair 10 of 10.

THE FIXTURE holds an empty first hour, a run of empty hours, an hour with some NULL values, an hour of only NULL values and two empty last hours. Values grow as the square of the hour, so a line through the wrong neighbours lands on the wrong value.

THE HALF-HOUR ZONE ARM is why the recipes bucket with `date_bin` and an explicit origin. `date_trunc` truncates in the session's zone, so in Asia/Kolkata its buckets start at :30 UTC. With `date_trunc` in the document, this arm read all 24 buckets as NULL.

Red first: on main the how-to has no recipes, so the premise reads 0 and the comparisons get nothing.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_recipes_are_in_the_how_to` | each of the three headings has a `sql` block naming `readings` |
| `test_each_recipe_matches_the_reference` | the table is columnar; the gap fill, LOCF and interpolation match the Python reference for all 24 hours; the gap fill gives the same buckets in an Asia/Kolkata session; its aggregate runs as the vectorized grouped node when that is on |

## 99. test_suite_exit_trap.py: a suite's own EXIT trap must still stop its cluster

The pytest twin of selftest part 620 (#1321). `pgc_setup` arms `trap pgc_on_exit EXIT`, which stops the server, removes the workdir and gives a suite that died its accounting (#1233). Bash keeps one EXIT trap, so a suite that arms its own after `pgc_setup` must call back into `pgc_on_exit` or `pgc_teardown`. `native_upgrade_converge.sh` did not, and every run left its server listening and its workdir in `/tmp`.

Independent of the part: it reads the suites and `lib.sh` as text but never sources or runs them, and its reader is Python's own. Trap lines are split with `shlex`. Comments are removed by the shell's rule (a `#` that starts a word, outside quotes, with backslash escapes) rather than by cutting everything after `#`. A one-line handler is read as that line, and any other up to its closing brace. The check names match the part's, five of them. It is not graded by `compare_to_bash`, which pairs `test/<stem>.sh` with `test_<stem>.py` and so cannot see a selftest part; `test_capability_decline.py` (parts 600 and 610) is the precedent.

Written red first: on the tree before the fix, the population arm fails naming `native_upgrade_converge.sh` and nothing else. Five mutations of this file's reader each redden the same arms as the matching mutation of the part: it never reports, it keeps comments, it reads past a one-line handler, it accepts only `pgc_on_exit`, and it sees `EXIT` only last on the line.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_detector_flags_a_handler_that_reaches_neither` | two suites the test writes, one trapping `EXIT` and one `EXIT INT TERM`, are both reported |
| `test_the_detector_is_not_satisfied_by_a_comment_or_the_next_function` | the teardown named only in a comment, or only in the function after a one-line handler, does not count |
| `test_the_detector_passes_a_handler_that_chains` | a handler calling `pgc_on_exit`, one calling `pgc_teardown`, a quoted handler, and a suite without `pgc_setup` are not reported |
| `test_no_suites_own_exit_trap_leaves_its_cluster_running` | `pgc_setup` still arms `trap pgc_on_exit EXIT` (premise), and no suite in `test/` is reported |

## 100. test_tier_partition.py: tier_partition moves a cold partition to Parquet, exactly once

Port of `tier_partition.sh` (#1295). The owner's decision on #1295: the move is atomic, with a brief ACCESS EXCLUSIVE on the parent. The export runs under the partition's SHARE lock. Then the function detaches and drops the partition, creates a foreign table with the partition's own name, columns, owner, grants and comment, and attaches it with the partition's own bounds.

EXACTLY ONCE IS TESTED, NOT ARGUED. The developer GUC `pgcolumnar.tier_fault_after` raises an error right after the step it names (`pause` sleeps instead, for the race arm). Each fault arm first requires that the error raised is the injected one: a missing function also fails and leaves the rows where they were.

What the arms found, in order:
- `pg_stat_file(...) IS NOT NULL` is false for an existing file, because `creation` is NULL on Linux, so the first existing-path refusal never fired, and a second move overwrote a file another partition was reading.
- Without the SHARE lock, a write in flight when the move started was dropped with the partition.
- @jdatcmd's review: at REPEATABLE READ or SERIALIZABLE the SHARE lock does not refresh the snapshot. The first version, at those levels, exported without a row that another session committed between the caller's snapshot and the lock, and dropped it: `16992|0` where `16993|1` was owed. Those levels are now refused (`25000`).
- The first version silently moved a partition with a row-level policy, dropping the policy, and left an orphan file behind a UNIQUE index on the parent. Both are refused before anything moves now, as are a view, a trigger of its own and a foreign key of its own.
- A foreign key REFERENCING a columnar table is refused by pgcolumnar itself, so it cannot arise. In the first draft of that arm the setup failed, rolled back the previous case's DROP with it (a multi-statement string is one transaction), and the arm passed on a leftover trigger. Each dependent is now made in statements of its own, with a premise that it exists, and a control arm shows the same partition moves once they are gone.
- The deadlock @jdatcmd predicted is real: an insert through the parent racing the move ends with one side cancelled (`40P01`), and every row is still exactly once.

@jdatcmd's review of #1317 found four inputs that failed after the export, each leaving an orphan file, each an arm now: a schema, owner or grantee whose name needs quoting (`%I` over a reg* cast quoted it twice), a column with its own collation, and a stored generated column (both refused at ATTACH). On PostgreSQL 18 a virtual generated column is carried too; measured, ATTACH accepts a foreign table with either kind, and an identity column on the parent needs nothing.

On a private database, because foreign servers are per database. A local path stands in for object storage. The pytest half uses connections in threads where the shell half uses background sessions.

### Every test

| test | what it holds |
| --- | --- |
| `test_tier_partition_moves_a_partition_exactly_once` | the move returns the rows moved; the parent's answers are the same; the cold range is a foreign partition with the same name and bound, reading the exported file; no pgcolumnar catalog rows are left; pruning keeps the ranges apart; an insert into the tiered range is refused; five refusals move nothing; the parent is held ACCESS EXCLUSIVE until commit, and a rollback leaves the rows local; a write in flight is exported; a fault after each of the five steps leaves the rows exactly once, still local |
| `test_tier_partition_refuses_what_it_cannot_move_and_survives_concurrency` | at each isolation level a row committed between the snapshot and the lock survives, and only READ COMMITTED moves; a UNIQUE parent and four kinds of dependent are refused with no file left, and the partition moves once they are gone; grants and comment carry over; a racing insert leaves every row once; lock_wait times a held parent out with nothing attached; the caller's lock_timeout survives; a retry over a failed attempt's file moves the partition |
| `test_tier_partition_moves_a_virtual_generated_column` | PostgreSQL 18+: a partition whose parent has a virtual generated column moves and reads back the same; below 18, `UNSUPPORTED_MAJOR`, listed in `expected_unrunnable.txt` |
| `test_tier_partition_runs_on_the_callers_own_privileges` | not SECURITY DEFINER: a role that does not own the table, holding the server-file role, USAGE on the server, CREATE on the schema and SELECT on the partition, is refused `42501` before any file is written, and the partition stays local and attached with no foreign table behind it; a non-superuser owner tiers a partition of its own table |

## 101. test_prepared_writes.py: a write prepared and finished in one session leaves the backend alive

Port of `prepared_writes.sh` (#1323). PREPARE frees TopTransactionContext without reaching the COMMIT/ABORT arms of the transaction callback, and the pending write states and delete-vector buffers live in children of it. The session's next transaction, the COMMIT PREPARED itself, walked the freed lists at its PRE_COMMIT and died of SIGSEGV. Committing from another session never touched them, which is the path `parallel_copy` takes, so nothing in the corpus had reached this.

Each arm runs in one connection, a transaction per statement, and asserts what that connection returns. A crash also shows as a "terminated by signal" line that the shared cluster's server log gained during this test; only lines written after the test began are counted, after a premise that the log is there. Independent of the shell suite: its own table, row counts and values. The check names match.

Red on main, the first arm reading `lost`. Each of the two discards is red under its own removal. The index-fetch arm has a premise that the plan is an Index Scan, because with the custom scan on, the fetch never reaches the fetch cache. A discard of that cache at PREPARE changed no arm, so it is not in the fix.

### Every test

| test | what it holds |
| --- | --- |
| `test_a_write_prepared_and_finished_in_one_session` | an insert, a delete and an update prepared and committed in one session, an insert prepared and rolled back, a write after COMMIT PREPARED, and an index fetch on each side of it, all answered by the same session; the server log gains no "terminated by signal"; no prepared transaction is left |

## 102. test_column_extent.py: the per-column extent is a true bound, recomputed whenever groups go

Port of `column_extent.sh` (#1304 option 5, for #1292 phase 2). `pgcolumnar.column_extent` keeps, per column, rows that are inserted and deleted but never updated. Each committing writer adds one row for the groups it flushed. A recompute over every zone map one snapshot sees replaces the rows that snapshot sees with one complete row. Every path that retires a group recomputes before commit. The planner uses the rows only when a complete one is visible, and their fold is then a true bound. `pgcolumnar.column_extent(rel, col)` shows that bound.

THE ORACLE IS THE DATA: `min()` and `max()` of the column. Every equality arm first makes sure no group is left holding deleted rows, so the zone maps and the data agree. Twice, the first draft of an arm deleted rows that left a group partly live and so wider than the data, and the extent was right where the arm was wrong. The one case that may legitimately be wider, a rolled-back savepoint that flushed a group, is asserted as containment.

Independent of the shell suite: its own row counts, values, times and helpers. The check names match, so `compare_to_bash` grades the pair one for one.

WHAT WRITING IT FOUND:

- **Ordinary writers never overlap.** Concurrent writers into one table are serialized by the storage-row lock (class 102) from their first flush to their commit. The concurrency arm therefore uses `parallel_copy`, whose loaders flush into one storage at once, each through PREPARE.
- **COMMIT PREPARED crashes in the preparing session.** On main, `COMMIT PREPARED` in the session that prepared a pgcolumnar write crashes the backend (#1323), so this file commits from another session.
- **A transaction did not see its own groups in the extent.** Its extent row is written at pre-commit, but its groups are visible to it from the moment they flush. A plan made in between bounded the column by the committed rows alone, which is an under-estimate: 61 rows against the zone maps' 4500 in this file's fixture. The bound now adds the transaction's own pending fold.
- **The xact scan counters carry over.** `pg_stat_get_xact_numscans` reports a backend's pending stats, flushed about once a second rather than at transaction end. A measurement in the same connection as the writes counts their zone-map reads too, so the planning arm measures on a fresh connection.

Written red first: the shell twin, against main, with the function absent (42883).

### Every test

| test | what it holds |
| --- | --- |
| `test_the_extent_is_the_columns_own_order` | `timestamptz`, `int` across 256 and negative, negative `numeric`, `date` and `text COLLATE "C"`: each column's extent is the data's min and max, over several row groups |
| `test_the_planner_reads_the_extent_not_the_zone_maps` | planning a bucketed GROUP BY scans `zone_map` 0 times with a complete extent and 1 without, with the same estimate; with no rows the function returns none; the next writer recomputes one over every group |
| `test_writers_concurrent_aborted_and_prepared` | `parallel_copy` with three loaders, an aborted writer, a rolled-back savepoint (containment), PREPARE and COMMIT PREPARED, and a table created in a rolled-back savepoint (no orphan rows) |
| `test_a_transaction_reads_its_own_flushed_groups` | inside a transaction that has flushed groups far outside the committed extent, `column_extent` contains them, and a plan estimates as it does with the extent rows deleted (the zone maps) |
| `test_a_shrink_is_recomputed_never_guessed` | after `expire`, `compact_rewrite` and VACUUM retire or rewrite groups, the extent equals the data left, and after `expire` it moved |
| `test_no_complete_extent_where_the_zone_maps_cannot_give_one` | a column added after older groups, and a `point` column even after a recompute, get no complete extent |
| `test_housekeeping_and_privilege` | fifty committing writers leave at most nine rows; TRUNCATE and DROP remove them; an ordinary role cannot read the catalog, nor the function without SELECT |

## 103. test_collation_change.py: a collation change without a rewrite leaves columnar answering as heap does

Port of `collation_change.sh` (#1327, found by @jdatcmd reviewing #1325). `ALTER COLUMN ... TYPE text COLLATE <other>` keeps the relfilenode, so no zone map is rewritten. The stored minima and maxima were the old collation's, and the reader compared them under the new one. Chunk-group pruning and the min/max fold then skipped rows that the new collation orders differently.

THE ORACLE IS HEAP: the same rows, the same `ALTER` and the same query. A server with no en_US collation cannot show the bug, so the test calls `cannot_run`, which fails the run rather than passing it.

THE FIX FORGETS THE BOUNDS RATHER THAN RECOMPUTING THEM. After an `ALTER` that changes a type, every columnar relation it reached that was not rewritten has its changed columns' zone-map minima and maxima nulled, in the base storage and in every projection storage. The column's extent rows are deleted. Counts and sums stay. A zone map with values and no bounds is already the state that a type with no ordering writes, and every reader treats it as "nothing is known". The ungrouped min/max fold was the exception: it skipped such a group as if it were all NULL, and returned NULL where heap returned a value.

Independent of the shell suite: its own values (`c`, `Q`, `x`, `q` against the shell's `a`, `B`, `m`, `b`), its own geometry (four groups), and two list partitions where the shell has one range partition. The projection lists the column second, so its storage index differs from the shell's. The check names match.

### Every test

| test | what it holds |
| --- | --- |
| `test_a_collation_change_without_a_rewrite` | after the change, columnar answers as heap does for: `min`/`max`, a range, an equality, rows written afterwards, a columnar partition reached through its parent, a `vacuum_sorted` table under `ORDER BY ... LIMIT`, and a projection sorted on the column. The extent is never the old collation's bound. The relfilenode is unchanged (premise) |
| `test_a_type_change_without_a_rewrite` | after int4 to oid, a binary-coercible change that keeps the relfilenode, columnar answers as heap does for `min`/`max`, a range above every int4 value and an equality on a value that was negative; the extent is not int4's bound; a table sorted on the column and a projection sorted on it answer as heap does (#1341 item 19) |

## 104. test_build_needs_pg_config.py: a build with no pg_config refuses, it does not wait on stdin

The pytest half of selftest part 630 (#1344). With no `pg_config` that answers, `$(PGXS)` was empty and the Makefile's CC probe ran `sed` with no file, so it read make's stdin. With stdin left open, as in a background job or a CI step, the build waited forever. With stdin closed, `make clean` exited 0 having cleaned nothing. The Makefile now refuses as soon as `PGXS` is empty, and says what to set.

DRIVEN, NOT READ. `make -n clean` runs on the real Makefile with `PG_CONFIG` naming nothing. `-n` does the parse in full, where the probe and the refusal both live, and builds or removes nothing. stdin is a pipe the test holds open and never writes to, and the process group is killed at 20 seconds, so a hung `sed` does not outlive the test. Its own driver (subprocess and `os.pipe`), where the shell part uses a FIFO and `timeout(1)`. The check names match.

Needs no cluster and no driver.

### Every test

| test | what it holds |
| --- | --- |
| `test_with_stdin_open_it_ends_and_names_pg_config` | with stdin open and silent, make ends within 20 seconds, and its own `*** ... Stop.` error names `PG_CONFIG`. The pipe really is one a reader waits on (premise) |
| `test_with_stdin_closed_it_still_refuses` | with stdin closed, make exits non-zero rather than 0 |

## 105. test_import_export_privilege.py: import and export check the table's privilege, serial and parallel

The pytest twin of `test/import_export_privilege.sh` (#559, #566), with the parallel twins' arms both halves gained in #1341. The attacker role holds every outer layer: USAGE on schema pgcolumnar, EXECUTE, and both server-file roles. It holds no privilege on `victim`, so the table ACL is the only refuser. A refusal is SQLSTATE 42501 with a message naming the table, where the shell twin greps "permission denied for table". Real logins, not `SET ROLE`, and every file is in `server_dir`.

`parallel_copy` and `parallel_export_parquet` always checked INSERT and SELECT, and nothing asked them. Their loaders and workers skip the executor's permission check, so that check is the only one.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_premises_isolate_the_table_acl` | the attacker logs in, holds both server-file roles, schema USAGE and EXECUTE, and nothing on `victim`; `victim` has its rows; the attacker's own source files exist |
| `test_a_table_the_caller_holds_no_grant_on_is_refused` | the seven deny arms, serial and parallel, in the shell's order; then `victim` keeps exactly its rows and no file of it was written |
| `test_the_owner_is_not_refused` | the owner's own exports, storage id and import are not refused |
| `test_the_owner_runs_the_parallel_twins` | the owner's `parallel_copy` loads its 50 rows, and its `parallel_export_parquet` writes some |

## 106. test_every_suite_job_installs_the_extensions.py: every job that runs the suites installs the extension packages

The pytest half of selftest part 640 (#1382). Suites such as `partman_columnar` and `approx_aggregates` need a PGDG extension package (`postgresql-N-partman`, `-hll`, `-tdigest`) and record a FAIL without it. The nightly's coverage job ran those suites without the packages, and the nightly went red. So the list is derived, not trusted: every job that runs the suites must install every extension package any such job installs.

READ, WITH ITS OWN READER. Python reads `ci.yml` and `nightly.yml` line by line, where the part uses awk. A job runs the suites when a line that is not a comment calls `run_all_versions.sh` or `run_coverage.sh`, or runs `pytest -q` in a job whose key names a cluster. The detector names a workflow it cannot read or finds no suite job in, so an empty answer can only mean nothing is missing. The check names match the part's.

Needs no cluster and no driver.

### Every test

| test | what it holds |
| --- | --- |
| `test_both_workflows_are_present` | both workflow files exist (premise) |
| `test_the_detector_names_what_it_finds_and_what_it_cannot_read` | on workflows written to fail, the detector names the job that lacks a package, a workflow it cannot read, and a workflow with no suite job |
| `test_the_suite_jobs_are_the_five_known` | the suite-running jobs are exactly `ci.yml`'s `pytest-cluster` and `suites`, and `nightly.yml`'s `coverage`, `pytest-cluster` and `suites`, so a rename is red |
| `test_every_suite_job_installs_every_extension_package` | no suite job lacks a package another installs |

## 107. test_fdw_plan_privilege.py: planning a foreign table reads no server file for a role the scan would refuse

The pytest twin of `test/fdw_plan_privilege.sh`. A planner estimate is visible through EXPLAIN, so a wrapper that reads its file at plan time for anyone tells a role without `pg_read_server_files` how many rows a server-side table holds. If the path doesn't exist, the planning error tells it that instead. The Iceberg wrapper's planner read `metadata_path` for every role: the unprivileged planner saw the true row count (5), and a missing path failed planning with `58P01`. Its planner now reads only for a role the scan would admit, as the Parquet wrapper's has since #330. That gate had no test either.

### Every test

| test | what it holds |
| --- | --- |
| `test_fdw_planning_reads_no_server_file_for_an_unprivileged_role` | a role without `pg_read_server_files` gets the flat estimate from both wrappers and plans a missing Iceberg path the same as a real one, while the scan still refuses it (42501); a privileged role gets the manifest count and the size estimate (controls) |

## 108. test_arrow_import.py: import_arrow round trips, reads a foreign file, and refuses what it cannot decode

The pytest twin of `test/arrow_import.sh` (ported for #1341), every check by the shell's name. A 16-column table survives export and import exactly (a set hash on both sides), non-finite dates and NaN numerics arrive as null, and a non-columnar target and a column-count mismatch are refused by SQLSTATE. A pyarrow-written file imports with its values; nanosecond values are floored to the microsecond and the loss is reported once, counted in VALUES (two columns, so values and rows differ), never refused; a dictionary batch and a compressed body are refused by SQLSTATE and message; every Arrow date, time and timestamp carrier and unit decodes to the instant it holds or is refused 22008, a file whose declared type does not match its column is refused 42804, and nested units are honoured. An import maintains plain, unique (23505 on a second import) and partial indexes, and a FIFO is refused rather than hung. The four pyarrow tests decline by name without pyarrow, and fail where `PGC_REQUIRE` names it.

### Every test

| test | what it holds |
| --- | --- |
| `test_a_round_trip_and_the_documented_lossy_mapping` | export and import counts, the identical round trip, the non-finite mapping, and the two argument refusals |
| `test_a_file_pyarrow_wrote` | a foreign file's row count and values |
| `test_nanosecond_narrowing_is_reported_never_refused` | every row kept, the loss counted in values (8, then 12 through time64 and a list), no report on the two controls, and the floor |
| `test_the_refusals_and_every_temporal_carrier` | the dictionary and compressed refusals, every carrier and unit, the 22008 guards, the 42804 type matches with their controls, an empty target after a refusal, and nested units |
| `test_an_import_maintains_the_tables_indexes` | the plan really uses the index, index and sequential scans agree, the unique violation and its row count, and the partial index |
| `test_a_fifo_is_refused_not_a_hang` | XX001 within the wall clock, and the backend still up |

## 109. test_entry_point_privilege.py: every relation-taking entry point has a decided privilege bar

The pytest twin of `test/entry_point_privilege.sh` (#569, ported for #1341), every check by the shell's name. The population comes from the catalog, never the script: `prosrc` is the C symbol and `proargtypes[0] = regclass` is "takes a relation", and the install script's symbols are checked against the catalog as a set. Every entry point lands in exactly one bucket (exempt, relation-taking, relation by other means), the exempt list may not hold a relation-taking name, and the relation-taking class splits into catalog-governed (the six from #560) and claimed-guarded with none pinned as ungated. Then behaviourally: a login role holding EXECUTE, so the C check is the only refuser, is refused 42501 by the five guarded names after the owner's identical call succeeds, and the four file siblings refuse it at the table before touching the file.

### Every test

| test | what it holds |
| --- | --- |
| `test_every_relation_taking_entry_point_has_a_decided_bar` | the enumeration premises, the symbol-set agreement, the bucket and class counts, every claimed-guarded entry point named with the pytest file that asserts its bar (#1386), the nine 42501 refusals with their owner calls, and the four file siblings refused at the table |

## 110. test_am_contracts.py: contracts the access method owes core DML and transactions

The pytest twin of `test/am_contracts.sh` (#1341). Each section is a promise from `docs/` or from core's table-AM interface that no test exercised. The first is `INSERT ... ON CONFLICT`: `docs/limitations.md` said `DO NOTHING` works, and on any table with a unique index it raised `0A000`, because the access method refused speculative insertion. Speculative insertion now inserts the row as an ordinary row, and withdraws it with a delete mark when a concurrent inserter won the key.

THE WITHDRAWAL IS REACHED ONLY BY A RACE. When one session holds a key uncommitted, the executor's pre-check sees it and waits, so it never inserts. A row is withdrawn only when another session inserts the key between this one's pre-check and its insert. So the third test races eight sessions on clock-derived keys. Measured: 0 duplicate keys clean; 407 to 507 with the withdrawal disabled.

### Every test

| test | what it holds |
| --- | --- |
| `test_insert_on_conflict` | `DO NOTHING` skips a duplicate and inserts the rest, with and without a conflict target; a key twice in one statement goes in once; `DO UPDATE` is refused (`0A000`) on a conflicting row and changes nothing, and inserts when nothing conflicts |
| `test_insert_on_conflict_withdraws_a_row_a_concurrent_insert_beat` | a `DO NOTHING` racing another session's uncommitted insert of the same key waits, then skips it: one row, the holder's, by scan and by the unique index |
| `test_insert_on_conflict_racing_sessions_leave_each_key_once` | eight sessions racing `DO NOTHING` on the same keys leave each key once, with no error; a premise requires that they contended |
| `test_a_truncate_that_rolls_back_restores_every_row` | a `TRUNCATE` rolled back at transaction level, and one rolled back to a savepoint, restore every row, with the primary key still answering and refusing; rows the parent buffered before the savepoint survive too |
| `test_an_aborted_inner_block_keeps_the_outer_statements_rows` | an outer `INSERT` and an outer `DELETE` whose expression calls a function with an aborted EXCEPTION block keep heap's answer. Before #1341's fix the INSERT kept 1 row of 5000. The same loss through a read inside the block is #1360 |
| `test_a_read_inside_an_aborted_inner_block_keeps_the_outer_statements_rows` | a read of the table inside an EXCEPTION block writes out the enclosing INSERT's or DELETE's buffered rows and marks, and keeps them until the block resolves (#1360). On abort the rows, the read's own count and the index's view all match heap; so do a read in a nested block that commits into an aborted one, and a block that deletes a row by index before its read. A block that commits keeps every outer row once. The cost is linear: at twice the rows, the file is under 2.5 times the size; and one read early in a 5000-row insert leaves at most 3 row groups |
| `test_an_insert_racing_an_uncommitted_delete_of_its_key` | an insert of a key whose delete (or key-changing update) is uncommitted waits, then fails with 23505 if the delete rolls back and succeeds if it commits: never two rows with the key. Before #1341's fix the rollback left two |
| `test_an_index_built_after_a_delete_serves_an_older_snapshot` | a REPEATABLE READ snapshot from before a committed DELETE still finds the row through an index built after it (an Index Scan, asserted), and a new snapshot does not; a UNIQUE index still builds over a key whose other holder is deleted. Before #1341's fix the old snapshot's lookup found 0 |
| `test_reindex_reclaims_the_entries_of_rows_no_snapshot_sees` | after a committed delete of 9 rows in 10, with no older snapshot, `REINDEX` takes the index under half its size, as it does on heap, and the index still finds the 10,000 live rows (#1407) |
| `test_the_documented_refusals_change_nothing` | `CREATE UNLOGGED TABLE` and `SET UNLOGGED` are refused (`0A000`) and leave nothing changed; `SET TABLESPACE` is refused and the table stays where it was; `CREATE INDEX CONCURRENTLY` is refused and leaves its index marked invalid, as core does (#1341 items 44, 51, 77) |

## 111. test_objstore_credentials.py: object-store credentials live on a user mapping, and the environment is a privilege

The pytest twin of `test/objstore_credentials.sh` (#393 M4, ported for #1341), every check by the shell's name. Its fixture server is its own: an http.server in a thread that verifies every request's AWS Signature Version 4 from the spec with the standard library, so a green read proves the C signer and an independent implementation agree, and that records every request. A private cluster, because phase 2 restarts the postmaster with `AWS_*` in its environment; phase 1 starts it with every `AWS_*` of the test process removed. Secrets misplaced on a server, table or (for an endpoint) a mapping are refused `HV00D`; only a superuser may set `credentials_required 'false'`; each role's own mapping decides its read (a wrong secret is `28000` from the server); a mapping with half a credential is refused `28000` by its own message before any request; no secret reaches an error, `EXPLAIN VERBOSE` or `srvoptions`; and with the environment present, only a superuser or a `credentials_required 'false'` mapping may use it.

### Every test

| test | what it holds |
| --- | --- |
| `test_object_store_credentials` | placement, per-user resolution, the half-credential refusals and the request count behind them, no leak, and ambient credentials as a privilege across the restart |

## 112. test_debug_hook_privilege.py: the debug metadata mutators are owner-only

The pytest twin of `test/debug_hook_privilege.sh` (#707, ported for #1341), every check by the shell's name. The two debug hooks that mutate storage metadata are bound as the test suites bind them and probed by a non-owner on its own login: each is refused 42501 with "must be owner", which tells the owner gate from an EXECUTE denial, and the table still reads. The mirrors show the gate is ownership: the same role may mutate its own tables. A private database, because the last mirror leaves a table that cannot be dropped.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_debug_mutators_are_owner_only` | the premises, both refusals by SQLSTATE and message, the gated table intact, and both mirrors |

## 113. test_vacuum_lock_privilege.py: maintenance refuses a non-owner before taking its lock

The pytest twin of `test/vacuum_lock_privilege.sh` (#749, ported for #1341), every check by the shell's name. A superuser connection holds ACCESS EXCLUSIVE on `victim`, and a non-owner calls vacuum, vacuum_sorted, cluster, add_projection, drop_projection, recluster, compact_rewrite and compact with lock_timeout 4s. Refused by ownership first is 42501 at once; an ownership check after the lock request is a wait that ends 55P03. A non-owner asking about a heap relation learns only that it is not the owner, and a refused vacuum is watched in `pg_locks` while it runs: it never queues. The lock is still held after the last probe. The owner's own calls are the controls. The role holds USAGE on the test's schema, so no 42501 is a schema refusal.

### Every test

| test | what it holds |
| --- | --- |
| `test_maintenance_refuses_a_non_owner_before_its_lock` | the premises, the nine refusals under contention (`truncate` among them, #1384), the three heap-relation refusals, no lock request queued while a refused probe runs, the lock still held, and the owner controls, `truncate`'s by its block count |

## 114. test_vm_privilege.py: the visibility-map entry points are owner-only and refuse heap

The pytest twin of `test/vm_privilege.sh` (#748, ported for #1341), every check by the shell's name. `vm_selftest` sets an all-visible bit, which on a block holding dead rows makes an index-only scan return them. A role holding only schema USAGE is stopped by the REVOKE; a role granted EXECUTE reaches the C ownership check and is refused there, on every block. No bit is set, and the index-only scan still agrees with a sequential scan. On a heap relation both functions refuse even the owner with 42809, and a sweep over every heap block leaves its index-only scan correct. A scan of the install script checks every `vm_*` function is one this test probes.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_visibility_map_entry_points` | the premises, the two refusal layers and the damage they prevent, the owner controls, the heap refusals and sweep, and the coverage scan |

## 115. test_alter_table_scans.py: ALTER TABLE forms see the live rows and only those

The pytest twin of `test/alter_table_scans.sh` (#1341), every check by the shell's name. A columnar table keeps a deleted row in its file until compaction, so every ALTER TABLE form that reads the existing rows must see the live ones and only those. `SET NOT NULL`, `ADD PRIMARY KEY` and `VALIDATE CONSTRAINT` run on a columnar table and on heap with the same rows. First a row violates each form, and both tables refuse it with the same SQLSTATE (23502, 23505, 23514). Then that row is deleted, and both accept the form. `ALTER COLUMN TYPE` and `ADD COLUMN` with a volatile default rewrite the table: each keeps the live rows and leaves the deleted ones gone. After both rewrites the primary key still finds id 1 once, through the index.

### Every test

| test | what it holds |
| --- | --- |
| `test_alter_table_forms_see_the_live_rows_and_only_those` | the three refusals and three acceptances against heap, both rewrites, and the index lookup after them |

## 116. test_settings_reference.py: the settings reference matches the server

The pytest twin of `test/settings_reference.sh` (#1341 items 42, 57 and 83), every check by the shell's name. It reads each `pgcolumnar.*` row of `docs/configuration.md` and compares it with `pg_settings` both ways. Every setting the server defines is documented, except the ones the server marks `NOT_IN_SAMPLE` (developer hooks and internal switches). Every documented setting exists. Each documented default is the server's `boot_val`. Each row's condition ("superuser", "only at server start", "Reloadable", or none of these) is the server's context. A role that is not a superuser, on its own login, is refused `enable_end_truncation` with 42501; a superuser sets it. `compression_level` reaches the zstd codec: the same text stores smaller at level 19 than at level 1. A build without zstd cannot run that test.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_reference_and_the_server_name_the_same_settings` | at least thirty documented settings, none undocumented, none documented that the server lacks |
| `test_each_documented_default_and_condition_is_the_servers` | each documented default and condition matches `boot_val` and `context` |
| `test_only_a_superuser_sets_enable_end_truncation` | the 42501 refusal on a real login, and the superuser control |
| `test_compression_level_reaches_the_zstd_codec` | level 19 stores less than level 1 |

## 117. test_large_values.py: megabyte-scale values are stored exactly

The pytest twin of `test/large_values.sh` (#1341), every check by the shell's name but one. A columnar table has no TOAST table: the writer detoasts each value and stores it inline. A 1 MB value, two 16 MB incompressible values and a 200 MB compressible one go into heap and are copied to a columnar table; then an UPDATE takes a value heap has TOASTed. Each must agree with heap by length and md5. The shell's opt-in arm for a chunk over 1 GB (`PGC_RUN_OVER_1GB=1`, refused with 54000, nothing committed, server up) is not ported, and `test_compare_to_bash.py` declares the pair incomplete for it.

### Every test

| test | what it holds |
| --- | --- |
| `test_megabyte_values_are_stored_exactly` | the four values and the TOASTed update against heap, and the rows the update left alone |

## 118. test_recovery_minimal_wal.py: tables that skip WAL survive a crash

The pytest twin of `test/recovery_minimal_wal.sh` (#1341), every check by the shell's name. A private cluster at `wal_level=minimal`, stopped with `-m immediate` for each crash. A columnar table and a heap mirror are created and loaded in one transaction, and later truncated and reloaded in one, so PostgreSQL skips WAL for them and makes them durable at COMMIT. Each arm reads the committed row count before the crash, so a load that failed cannot pass as two empty tables that agree, then checks that the server came back through crash recovery and that the columnar table holds what its heap mirror holds. Both commit-time branches run: `wal_skip_threshold` 0 (fsync) and 64MB (the new pages written to WAL).

### Every test

| test | what it holds |
| --- | --- |
| `test_tables_skipping_wal_survive_a_crash` | the premise, and create-and-load and truncate-and-reload at both thresholds, each across a crash |

## 119. test_statement_self_visibility.py: a statement does not see its own writes

The pytest twin of `test/statement_self_visibility.sh` (#1341), every check by the shell's name. A scan reads its row-group list once, when it starts, so the groups its own statement flushes later are not in it. With row groups of 1000 rows, `INSERT INTO t SELECT FROM t`, an `UPDATE` of every row, both in one transaction after an earlier write, and an `INSERT` in a `WITH` clause each run on a columnar table and on heap, and heap is the answer. An arm checks that the `INSERT` really flushed row groups while its scan was reading. Every statement runs under a `statement_timeout`, because a scan that fed on its own output would never end. Both tables run every statement, so a columnar failure cannot make the comparison vacuous.

### Every test

| test | what it holds |
| --- | --- |
| `test_a_statement_does_not_see_its_own_writes` | the four shapes run to completion and answer as heap does, and the mid-statement flush |

## 120. test_maintenance_rollback.py: a maintenance function that rolls back leaves the table as it was

The pytest twin of `test/maintenance_rollback.sh` (#1341), every check by the shell's name. `compact_rewrite`, `recluster`, `compact`, `vacuum` and `vacuum_sorted` each retire row groups, and most write new ones. Each runs inside a transaction on a table with row groups of 1000 rows, two rows in five deleted, and one group deleted whole. Its effect on the row groups is read inside the transaction, and the transaction rolls back. The table must then hold the same rows by content and the same row groups, and an index scan must agree. The same then runs under a savepoint, later writes commit, and every function runs for real. The rollback is issued as SQL statements, because `pgc_conn` is a watching wrapper and an autocommit flag set on it does not reach the connection.

### Every test

| test | what it holds |
| --- | --- |
| `test_a_maintenance_function_that_rolls_back_leaves_the_table_as_it_was` | each function's effect inside its transaction, the table after each rollback by scan and by index, the savepoint case, and the real runs |

## 121. test_partition_rows.py: rows cross columnar partitions, and keys hold through the parent

The pytest twin of `test/partition_rows.sh` (#1341, #1421), every check by the shell's name. A parent with a PRIMARY KEY and two columnar partitions, and the same with heap partitions; heap is the answer. An `UPDATE` moves 49 rows to the other partition: each partition and the parent then hold what heap's do, and the key finds a moved row at its new key only. A duplicate key through the parent is refused (23505), a key a moved row gave up is free again, and a duplicate of a moved row's new key is refused in its new partition. Three races, ordered by a gate connection rather than by sleeps: a second insert of the same key waits and gets 23505; a second session moving the row the first is moving waits and gets 40001, which columnar used to answer with 23505 (#1421); and an in-place `UPDATE` of a row being moved waits and gets 40001.

### Every test

| test | what it holds |
| --- | --- |
| `test_rows_cross_columnar_partitions_and_keys_hold_through_the_parent` | the row-moving update against heap, the three key arms, and the three races with each contender seen waiting |
| `test_a_keyed_update_keeps_its_memory_bounded` | an UPDATE of 25,000 keyed rows, in place and moving partition, over a row group with deletes, grows the backend's memory by under 32 MB (#1422 review) |

## 122. test_prepared_across_sessions.py: a prepared transaction, seen from other sessions and across a crash

The pytest twin of `test/prepared_across_sessions.sh` (#1341), every check by the shell's name, on a private cluster because the last arm crashes the postmaster. While a transaction is prepared, another session does not see its insert and still sees the rows its DELETE removed. A second insert of a key it holds waits and gets 23505 after COMMIT PREPARED; an UPDATE of a row it updated waits and gets 40001, which needs #1421's fix (it got 23505 before, the table having a primary key). ROLLBACK PREPARED of a DELETE and of an UPDATE leaves every row as it was. A prepared insert survives an immediate stop, is still listed in `pg_prepared_xacts`, and after COMMIT PREPARED its rows equal a heap table's.

### Every test

| test | what it holds |
| --- | --- |
| `test_a_prepared_transaction_across_sessions_and_a_crash` | visibility while prepared, the two waits and their errors, both ROLLBACK PREPAREDs, and the crash |

## 123. test_maintenance_crash.py: a crash part-way through maintenance loses nothing

The pytest twin of `test/maintenance_crash.sh` (#1341), every check by the shell's name. A private cluster, because each arm crashes the postmaster: the shell kills its process group, and this side stops it with `-m immediate`, which leaves the same redo for the next start. `compact_rewrite`, `recluster`, `compact` and `vacuum` each run twice on a fresh table with row groups of 1000 rows, two rows in five deleted, one group deleted whole, and an index. First the function runs in an open transaction, another session checkpoints, and the server crashes: after recovery the table is as it was. Then the function commits and the server crashes before any checkpoint: after recovery the table is as the function left it. Each arm also checks that the server logged a crash recovery. After each recovery an index scan through `mc_id` must read the rows a scan reads: the as-before arms compare two by-index readings with each other, so an index already wrong before the crash agrees with itself there.

### Every test

| test | what it holds |
| --- | --- |
| `test_a_crash_part_way_through_maintenance_loses_nothing` | for each function, the uncommitted crash and the committed crash, each with its recovery logged and the index agreeing with a scan after it |

## 124. test_serializable.py: SERIALIZABLE refuses a write skew on a columnar table

The pytest twin of `test/serializable.sh` (#1341), every check by the shell's name. Two serializable transactions each read what the other is about to change, then each write; SSI must refuse the second with 40001 ("read/write dependencies") and the invariant must hold, on a columnar table as on heap. Each error is tagged with its step: columnar raises at the second write, which waits for the first commit, and heap at the second COMMIT, so the checks hold the refusal, not the step. At REPEATABLE READ the same interleavings must commit both transactions, on both tables, so a 40001 from anything but SSI cannot pass. Four shapes: two doctors taken off call in one row group and in two, and someone put on call in an empty ward after a scan of the whole table and after an index scan. The protection comes from heap SSI on pgColumnar's catalog tables, and an arm pins the SIRead locks a serializable read takes on `row_group` and `delete_vector`. The second write runs in a thread, because on columnar a write to a row group another session is changing waits for that session to commit.

### Every test

| test | what it holds |
| --- | --- |
| `test_serializable_refuses_a_write_skew` | the catalog SIRead locks, the four write skews refused by SSI with 40001 against heap, and the REPEATABLE READ controls |

## 125. test_rls_direct_storage.py: the direct-storage paths refuse a caller a policy restricts

The pytest twin of `test/rls_direct_storage.sh` (#563, ported for #1341), every check by the shell's name. Row-level security is applied by the rewriter, and these functions read and write storage with no query to rewrite, so each refuses a relation whose policies apply to the caller. The caller HOLDS SELECT, INSERT, EXECUTE and the server-file roles, so the RLS check is the only refuser left. Eight entry points are refused and nothing was written or imported; a role with no privilege is told "permission denied" and not that RLS exists; a table with no policy, the owner without FORCE, and the owner's export (read back, row for row) stay allowed; and under FORCE a non-superuser owner is refused too.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_direct_storage_paths_refuse_a_policy_restricted_caller` | the premises, the eight refusals and their absent effects, the no-privilege caller's ACL error, the not-over-refusing controls, and the FORCE pair |

## 126. test_objstore_crlf.py: a CR or LF in an object-store URL is refused before it is sent

The pytest twin of `test/objstore_crlf.sh` (ported for #1341), every check by the shell's name. A URL path is written into the request line, so a CR/LF in it would split the request. The guard is in the single connect path, so read_parquet and the REST catalog client's request are both refused, naming CR or LF; and a listener of the file's own records that nothing carrying the injected line reached it.

### Every test

| test | what it holds |
| --- | --- |
| `test_a_cr_or_lf_in_a_url_is_refused_before_it_is_sent` | read_parquet and a REST `catalog_uri` with a CR/LF in the path are refused naming CR or LF, the listener receives no injected line, and the backend is alive after each |

## 127. test_objstore_allowlist.py: the endpoint allow-list, empty by default

The pytest twin of `test/objstore_allowlist.sh` (#393, ported for #1341), every check by the shell's name. An empty `pgcolumnar.objstore_allowed_endpoints` refuses all remote reads with 42501 and names the setting; a local read is untouched. A list admits exactly what it names, host or host:port, any entry of several. Only a superuser may change it (SUSET), and a superuser session SET is the admin override, asserted with the configured list empty so only the SET can admit the read. A link-local address is refused even when listed, before any connection, naming the range.

The list is changed by ALTER SYSTEM and a reload, and reset in a `finally`. The fixture server is the file's own Range-capable `http.server`, serving one Parquet file from `server_dir`.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_endpoint_allow_list` | the default deny, the list's matching, SUSET and the admin override, and the link-local refusal |

## 128. test_objstore_userinfo.py: userinfo in a URL is refused on every entry point

The pytest twin of `test/objstore_userinfo.sh` (#706, #995, #997, ported for #1341), every check by the shell's name. `user@host` is refused with the parse guard's 22023 on the http read and write paths, in an s3 or gs bucket authority, and in an endpoint a foreign server configures. 22023 comes only from the parse guards and 42501 only from the allow-list, so the SQLSTATE says which layer refused, and the message pins the guard. `test_objstore_endpoint_userinfo.py` covers the endpoint half independently and is not this file's pair.

The controls are what make the arms mean anything. With no endpoint configured every s3 URL is refused 28000, the clean one included, and 28000 has three causes, so each control names the refusal it expects.

### Every test

| test | what it holds |
| --- | --- |
| `test_userinfo_in_a_url_is_refused_by_the_parse_guard` | read_parquet and export_parquet over http, export_arrow, and the s3 and gs bucket authority, read and write: 22023, naming userinfo |
| `test_a_clean_url_reaches_the_refusal_it_should` | a clean s3 URL demands an endpoint (naming `AWS_ENDPOINT_URL`), gs demands a credential, a malformed URL and an `@` in a key are not userinfo, a clean non-allowed endpoint is still 42501, and the backend is alive |
| `test_userinfo_in_a_configured_endpoint_is_refused` | both endpoint shapes, `u:p@` and `user@`, are 22023 naming userinfo and the endpoint; a clean endpoint gets past the parse |

## 129. test_savepoint_flush.py: a savepoint rolled back after a flush

The pytest twin of `test/savepoint_flush.sh` (#1341), every check by the shell's name. A row group flushed inside a savepoint is a catalog row written under the subtransaction, so the abort must hide it; rows still buffered, and buffered delete marks, must be thrown away. With row groups of 1000 rows, each shape runs on a columnar table and on heap, and heap is the answer: a flushed `INSERT` rolled back to a savepoint, at READ COMMITTED and at REPEATABLE READ; the same unique keys inserted again after `ROLLBACK TO`; an `UPDATE` of every row rolled back to a savepoint; and an `INSERT` and an `UPDATE` that fail after flushing, alone and inside a savepoint. Four readers compare each result, inside the transaction and after `COMMIT`: a plain sequential scan, the custom scan, the vectorized count and the primary key's index. Every shape starts from fresh tables, so one that goes wrong cannot fail the ones after it. Premises check that each reader takes its path and that each write really flushed: row groups for the INSERTs, and for both UPDATEs the inserts into `row_group` and `delete_vector` that `pg_stat_xact_all_tables` counts between `BEGIN` and `ROLLBACK TO`, two or more of each. The failing UPDATE reads its own table half-way through, through `sp_probe`, which flushes its delete marks before it fails. Each transaction runs one statement at a time and goes on after an error, as `psql` does with `ON_ERROR_STOP` off.

### Every test

| test | what it holds |
| --- | --- |
| `test_a_savepoint_rolled_back_after_a_flush` | the reader paths, the flushes, and every shape answering as heap does inside the transaction and after it |

## 130. test_rls_query_paths.py: row-level security holds on every read path

The pytest twin of `test/rls_query_paths.sh` (#1341), every check by the shell's name. A columnar table and a heap mirror hold the same 20000 rows, half owned by each of two roles, under a policy that shows a role only its own rows. One role, on its own login, queries both by the custom scan, the vectorized and grouped aggregates, with vectorization and the custom scan off, by a parallel scan, an index-only scan and a covering projection; each answer must be heap's, and plan premises check each path was taken. A cheap function that is not leakproof must see only that role's rows, by the custom scan, with it off, and in parallel; a notice handler counts what it saw. Each vectorized node also gets a leak arm, with a premise that the same query with a leakproof filter is answered by that node, and `column_extent`, which reads the stored extent of every row, must refuse a caller the policy restricts.

### Every test

| test | what it holds |
| --- | --- |
| `test_row_level_security_holds_on_every_read_path` | the read paths against heap (the custom scan, both vectorized aggregates, parallel, index-only, the covering projection), their plan premises, and the leak arms, including one through each vectorized node |

## 131. test_type_matrix.py: types the differential matrix never stored answer as heap

The pytest twin of `test/type_matrix.sh` (#1341), every check by the shell's name. The encoder has fast paths for fixed widths 1, 2, 4 and 8, and every other width and every varlena takes a generic path. This matrix stores what no test had: by-reference fixed widths 6, 8, 12, 16 and 64 (macaddr, macaddr8, timetz, point, name), the 1-byte `"char"`, time, money, inet, cidr, bit, varbit, tsvector, xml, json, an enum, a composite, a domain, and numeric NaN and ±Infinity with timestamp and date ±infinity. 12000 rows in row groups of 1000, each column NULL on its own pattern, are generated once into heap and copied. Every row as text, each column's counts, a range on every ordered column with literal bounds read from heap, json, point and tsvector matches, the special-value aggregates, and the composite's fields must all equal heap's.

### Every test

| test | what it holds |
| --- | --- |
| `test_types_the_matrix_never_stored_answer_as_heap` | the 21 columns against heap: rows, counts, sixteen ranges, the three non-btree matches, and the special values |

## 132. test_iceberg_data_files.py: an Iceberg table's live data files, listed inside its location

The port of `test/iceberg_data_files.sh`, by name, one check per test (#1341). The `ice` module fixture copies the committed warehouse away from its recorded root (`/tmp/pgc_ice_wh`), so rebasing is exercised for real. It stages every tampered and crafted table once and records every reading.

- **The oracle is pyiceberg's own view,** committed as `expected_files.json`, so pyiceberg is not needed at test time.
- **A manifest list outside the table's location is refused with 22023, by four routes:** a foreign absolute path, a `..` traversal, a sibling sharing the byte prefix, and a symlink under the location that targets a file outside it. XX001 would mean the backend opened the file.
- **Deletes are refused, not ignored:** a delete manifest in the list, and a delete or removed entry inside a data manifest (#1341 item 49). Each table is crafted Avro, because no available writer emits these shapes. The added and the EXISTING entry must be listed, so a refusal comes from the entry, and a lister that keeps only ADDED entries fails.
- **The privilege** is read under `SET ROLE` to a role without `pg_read_server_files`.

The shell's two skip names, "iceberg warehouse fixture is missing" and "python3 (json, zlib) is needed", are carried as the same declines.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_warehouse_fixture_is_present`, `test_python_json_and_zlib_are_available` | the shell's skip names; when present, the oracle lists data files and zlib round-trips |
| `test_the_data_files_match_pyicebergs_oracle`, `test_every_data_file_is_parquet` | the live data files, by record count, region and file name, all PARQUET |
| `test_returned_paths_are_rebased_under_the_actual_location`, `test_no_returned_path_leaks_the_recorded_root` | the rebasing |
| `test_a_manifest_list_outside_the_location_is_refused`, `test_the_backend_survives_the_traversal_attempts` | the four escapes, refused with 22023 |
| `test_a_snapshot_with_a_delete_manifest_is_refused`, `test_the_backend_survives_the_delete_refusal` | a delete manifest in the list, refused with 0A000 |
| `test_the_control_entry_is_listed`, `test_an_existing_entry_is_listed`, `test_a_delete_entry_is_refused`, `test_the_backend_survives_the_entry_refusals` | an added and an existing entry are listed; a position-delete, an equality-delete and a DELETED entry are refused with 0A000 |
| `test_a_table_with_no_current_snapshot_yields_no_data_files`, `test_an_unprivileged_role_is_refused` | no current snapshot lists nothing; a caller without `pg_read_server_files` gets 42501 |

## 133. test_native_skip.py: a type binary-coercible to its opclass's type prunes

The pytest twin of `test/native_skip.sh`, every check by the shell's name (#1433, completed for #1341). A comparison on a cidr column resolves to inet's operator, and a varchar column's to text's. The reader called such a key cross-type and asked the opfamily for an ordering proc over the pair, which `network_ops` and `text_ops` do not register, so the key was dropped and the column pruned nothing. inet and text hold the same values and are the controls. Each arm asserts heap's answer, then that chunk groups were removed. A premise checks that heap's answer holds the rows the arm expects. The rest of the suite covers scalar ranges and equality, including a bigint column against an integer literal (#477) and domains (#483); an anchored LIKE as a range under the C collation, with its refusals under the default and a discovered non-C collation (#426); a default-collation column in a `--locale=C` database, which must derive the bounds; and a columnar partition taking the custom scan (#436). The port found the shell's non-C half had never run: its collation probe closed a quoted identifier with a single quote.

### Every test

| test | what it holds |
| --- | --- |
| `test_heaps_range_on_a_coercible_type_holds_121_rows` | **premise**: heap's range on each of inet, cidr, a domain over cidr, text, varchar and a domain over varchar holds 121 rows (parametrized) |
| `test_a_range_on_a_coercible_type_returns_heaps_rows` | the columnar range returns heap's rows, for each of the six types (#1433) (parametrized) |
| `test_a_range_on_a_coercible_type_skips_groups` | and removes chunk groups, for each of the six types (#1433) (parametrized) |
| `test_equality_on_a_coercible_type_returns_heaps_rows` | equality on cidr and on varchar, which also takes the bloom probe, returns heap's row (parametrized) |
| `test_heaps_equality_on_a_coercible_type_finds_the_row` | **premise**: heap's equality finds row 11009 (parametrized) |
| `test_equality_on_a_coercible_type_skips_groups` | and the columnar equality removes chunk groups (parametrized) |
| `test_the_scalar_plan_is_a_columnar_custom_scan` | the plan under test is the columnar custom scan |
| `test_scalar_rows_agree_with_heap` | ranges on int, bigint (with and without casts, #477) and domains over both (#483) return heap's rows (parametrized) |
| `test_scalar_counts_agree_with_heap` | counts under equality, one-sided and non-selective predicates equal heap's (parametrized) |
| `test_scalar_counts_are_known` | the row count, absent values, out-of-range values and a present boundary give known counts (parametrized) |
| `test_a_scalar_predicate_skips_groups` | each selective predicate, cast or not, on a column or a domain, removes chunk groups (#477, #483) (parametrized) |
| `test_a_non_selective_scan_skips_nothing` | a non-selective scan removes none |
| `test_a_like_or_range_prunes` | under COLLATE "C" an anchored LIKE, a trailing `_` and a low-end prefix prune, and the equivalent ranges do on both tables (#426) (parametrized) |
| `test_a_like_does_not_prune` | an unanchored LIKE, and an anchored one under this cluster's non-C default, remove nothing (#426) (parametrized) |
| `test_a_like_returns_heaps_rows` | every LIKE form, escaped and `_` wildcards included, returns heap's rows (#426) (parametrized) |
| `test_a_default_collation_in_a_locale_c_database_derives_the_bounds` | in a database created with LC_COLLATE 'C', a default-collation column derives both bounds (#426) |
| `test_and_it_returns_the_right_rows` | and it returns the right row (#426) |
| `test_a_range_still_prunes_under_a_non_c_collation` | **premise**: under a discovered non-C collation a range still prunes; declares unrunnable when the build has none |
| `test_an_anchored_like_does_not_prune_under_a_non_c_collation` | but an anchored LIKE does not (#426) |
| `test_a_non_c_collation_like_returns_heaps_rows` | and its rows match heap's (#426) |
| `test_both_partitions_are_columnar` | **premise**: both partitions are columnar (#436) |
| `test_a_partitioned_columnar_table_returns_heaps_rows` | the partitioned table returns heap's rows (#436) |
| `test_a_partition_takes_the_columnar_custom_scan` | it plans the columnar custom scan (#436) |
| `test_a_partition_prunes_row_groups` | and it prunes row groups (#436) |

ONE CHECK PER TEST (#1339): module fixtures build each data set once, and each test asserts one reading. Measured with zone-map skipping disabled in the scan on PG17: the shell reddens 21 checks. The earlier twin, 13 tests holding 76 checks, reported 10. This one reports the same 21.

## 134. test_unique_conc.py: concurrent inserters of one unique key serialize

The pytest twin of `test/unique_conc.sh` (#5, ported for #1341), every check by the shell's name. A columnar row's btree entry is written at once while its data waits for the stripe to flush, so a second inserter of the same key could not see the first's row and would insert a duplicate. The table AM takes a transaction-scoped advisory lock per unique key, hashed with the key type's default hash. So the second inserter waits for the first to commit and gets 23505, asserted by SQLSTATE and the index's name. T1 is held mid-INSERT, its key row indexed and unflushed, by a second row that waits in `cq_block` on a lock a holder session has. Every barrier polls `pg_stat_activity` for a lock wait.

The shapes: one key, with the lock off in T2 as the control that shows the bug; keys equal under the opclass with different bytes (numeric 1.0 and 1.00, and citext by case); different keys, which must not wait; a multi-column key, equal and differing in its second column; a partial index, inside and outside its predicate; NULL keys under NULLS DISTINCT and NULLS NOT DISTINCT; a duplicate within one statement; and a first inserter that aborts. A private cluster runs `pgcolumnar.unique_lock_buckets` at 100003, which is fixed at server start, so unrelated keys do not share a bucket.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_bucket_count_is_fixed_at_server_start` | `SET` of the bucket count is refused 55P02, and the cluster runs 100003 |
| `test_one_key_serializes_and_the_lock_is_what_does_it` | with the lock off in T2 both inserts commit; with it on T2 waits, gets 23505, and one row is left |
| `test_keys_equal_under_the_opclass_serialize` | numeric 1.0 and 1.00: the same pair, lock off and on |
| `test_citext_keys_equal_by_case_serialize` | citext 'ABC' and 'abc' serialize and conflict; unrunnable without citext |
| `test_different_keys_do_not_block_each_other` | T2's different key returns at once, and both rows are kept |
| `test_a_multi_column_key` | (1,2) twice serializes and conflicts; (5,6) and (5,7) do not wait |
| `test_a_partial_unique_index` | inside the predicate the keys serialize and conflict; outside it neither waits and both rows are kept |
| `test_null_keys` | two NULLs never wait under NULLS DISTINCT; under NULLS NOT DISTINCT they serialize and conflict |
| `test_a_duplicate_within_one_statement` | one INSERT of the same key twice raises 23505 and inserts nothing |
| `test_an_aborted_first_inserter` | T2 waits behind T1's flushed, uncommitted row; T1 rolls back and T2's insert succeeds |

## 135. test_update_conc.py: two writers of one row serialize

The pytest twin of `test/update_conc.sh` (#5, ported for #1341), every check by the shell's name. A columnar UPDATE marks the old row deleted and appends a new one, so without a lock on the row identity two transactions updating one row each kept their own new version, and the row was duplicated. `columnar_row_lock.c` takes a transaction-scoped advisory lock per row at the delete-vector flush, re-reads the latest committed state, and gives the loser a retryable 40001. A table with a unique index takes the lock during the UPDATE, so the unique check cannot answer 23505 first (#1421).

Each concurrent case runs one interleaving on a heap table, the oracle, and on a columnar one: s1 writes and holds its transaction, s2's statement waits on a lock, which a poll of `pg_stat_activity` confirms, and s1 commits. At READ COMMITTED a heap re-applies s2's update where columnar fails it, so the columnar value is compared with the heap's only at REPEATABLE READ. A private database; each session runs at a 30s `lock_timeout`, as the shell's cluster does.

### Every test

| test | what it holds |
| --- | --- |
| `test_update_vs_update_read_committed` | one row is left and the loser gets 40001; the heap composes both updates into one row |
| `test_update_vs_update_repeatable_read` | count and values match the heap, and both losers get 40001 |
| `test_update_vs_update_serializable` | the count matches the heap and the loser gets 40001, which held before the row lock too |
| `test_update_vs_delete_read_committed` | the heap's DELETE removes the updated row; columnar's loser gets 40001 |
| `test_delete_vs_update_read_committed` | the committed DELETE stands on both, and columnar's loser gets 40001 |
| `test_a_row_updated_twice_by_one_statement` | `UPDATE ... FROM (VALUES (1), (1))` leaves one row on both |
| `test_overlapping_multi_row_updates` | 41 overlapping rows: 200 rows remain, and the loser gets 40001 |
| `test_serial_updates_control` | the same two updates without overlap leave one row with the last writer's value |
| `test_a_covering_unique_index` | with a unique index the loser still gets 40001, not 23505; the heap composes |
| `test_a_savepoint_rolled_back` | an UPDATE rolled back to a savepoint leaves the pre-savepoint value, as on the heap |
| `test_a_statement_failing_inside_a_savepoint` | an UPDATE that fails partway inside a savepoint leaves nothing behind: its buffered mark and row are discarded |

## 136. test_recovery.py: committed writes survive a crash, uncommitted ones leave nothing

The pytest twin of `test/recovery.sh` (ported for #1341), every check by the shell's name. Columnar stripes are written through the buffer manager with a full-page WAL record each, and the metadata lives in WAL-logged heap catalogs. So rows committed after the last checkpoint come back from WAL, a transaction open at the crash leaves nothing visible, and a visibility-map bit cleared after the checkpoint does not come back set from the page on disk. A heap mirror is the oracle.

A private cluster, stopped with `-m immediate` for each crash. Each crash is proven by the server log gaining a "database system was not properly shut down" line, counted before that crash, so one recovered crash cannot vouch for a later one. The open transaction's INSERT is proven to have run by its session holding an xid.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_pair_through_three_crashes` | rows committed after a checkpoint survive; an open transaction's checkpointed INSERT leaves no row; committed deletes survive; each against the heap pair |
| `test_a_cleared_visibility_bit_stays_cleared` | a bit set and checkpointed, then cleared by a DELETE, stays cleared after the crash; a clean block's bit stays set; the rows match heap |
| `test_a_projection_survives_a_crash` | a projection copy committed after the checkpoint replays, and reproduces the base's rows and a scan through it |

## 137. test_native_backend_crash.py: one backend dies and committed data is intact

The pytest twin of `test/native_backend_crash.sh` (#217, ported for #1341), every check by the shell's name. A SIGSEGV in one backend makes the postmaster drop every session and reinitialize, with crash recovery, while it stays up itself. Committed columnar rows must then match a heap mirror, and an INSERT killed before its commit must leave no row and no row group.

The kill lands mid-write, once the INSERT has written pages to the relation and before it reaches its trailing `pg_sleep`, and after every stripe has flushed, while it sleeps; both are asserted. A sentinel session opened before each kill must be dropped by the reinit, which proves the crash happened. A private cluster, since the reinit drops every session on the server.

### Every test

| test | what it holds |
| --- | --- |
| `test_a_kill_mid_write` | a 3,000,000-row INSERT killed after it has grown the relation: the cluster reinitialized, the postmaster kept its pid, and c holds exactly its committed rows and row groups |
| `test_a_kill_after_the_stripes_flushed` | the same for a 100,000-row INSERT killed in its trailing `pg_sleep` |
| `test_a_kill_with_a_second_reader` | the mid-write kill with a second parked reader, which the reinit drops too |

## 138. test_corruption.py: a corrupt catalog row is refused cleanly and no backend dies

The pytest twin of `test/corruption.sh` (ported for #1341), every check by the shell's name. The reader trusts the lengths, counts, offsets and descriptors the catalog stores. Each test corrupts one field on a fresh table and asserts that a read raises XX001, or, for a field the reader tolerates, that it does not crash.

A crash is read from the server log, counted from the test's start, since a client error cannot tell a refusal from a crash and a liveness probe passes once crash recovery has finished. Each corrupting UPDATE must change a catalog row. The bloom test also looks up a value the table holds, since an absent value answers 0 even when a corrupt filter wrongly says "absent". A private cluster, since a crash drops every session.

### Every test

| test | what it holds |
| --- | --- |
| `test_a_corrupt_byte_length` | `row_group.byte_length` inflated: the read raises XX001, and no backend died |
| `test_a_corrupt_row_count` | `row_group.row_count` inflated: XX001, no crash |
| `test_a_corrupt_page_length` | `column_chunk.page_length` inflated: XX001, no crash |
| `test_a_corrupt_value_count_degrades_safely` | `column_chunk.value_count` inflated: the read may answer or refuse, and no backend died |
| `test_a_corrupt_encoding_descriptor` | a descriptor of `\xffffffff`: XX001, no crash |
| `test_a_negative_column_index` | `column_index = -1`: no crash |
| `test_a_corrupt_bloom_header_is_no_filter` | a huge nbits over a short buffer: an absent value is 0 rows, a held value is still found, no crash |

## 139. test_inheritance.py: a partitioned columnar parent is refused, a columnar matview is not

Part of the pytest twin of `test/inheritance.sh` (#1259, ported for #1341): its #1259 arms only, by the shell's names, and the pair is declared INCOMPLETE. From PostgreSQL 17 a partitioned table may carry relam = pgcolumnar and has no storage. `get_storage_id` and `add_projection` on it must be refused with 42809, not any error: with the fix reverted a release build fails with 58P01 opening relfilenode 0. Liveness is read from the server log. PostgreSQL 15 and 16 cannot build the fixture, so that test declines there.

### Every test

| test | what it holds |
| --- | --- |
| `test_a_partitioned_parent_is_refused_and_its_leaf_is_not` | the parent is refused with 42809 by both calls and no backend crashed; its leaf is accepted by both |
| `test_a_columnar_materialized_view_is_still_columnar` | a columnar matview is still recognised and returns its rows |

## 140. test_parquet_import.py: a partitioned columnar parent is refused by import_parquet

Part of the pytest twin of `test/parquet_import.sh` (#1263, ported for #1341): its #1263 arms only, by the shell's names, and the pair is declared INCOMPLETE. Handed a partitioned parent, import_parquet used to SIGSEGV. The file is exported first, so the import reaches the relation; a leaf and a heap target must still import; the parent must be refused with 42809, and no backend may crash, read from the server log. It declines on PostgreSQL 15 and 16.

### Every test

| test | what it holds |
| --- | --- |
| `test_a_partitioned_parent_is_refused` | the file exists, a leaf and a heap target import 50 rows each, and the parent is refused with 42809 and no crash |

## 141. test_phase5.py: set_options and reset_options agree

Part of the pytest twin of `test/phase5.sh` (#1265, ported for #1341): its #1265 arms only, by the shell's names, and the pair is declared INCOMPLETE. reset_options had no guard, so it reported success where set_options refused. The property is parity on a columnar table, a heap table, a partitioned parent and a columnar matview, with an ordinary columnar table as the control both must accept, and the settled verdicts named: both accept the matview and refuse the partitioned parent.

### Every test

| test | what it holds |
| --- | --- |
| `test_set_and_reset_options_agree` | the four fixtures' kinds, the control, parity on each, and the matview and partitioned-parent verdicts |

## 142. test_drop_cleanup.py: a columnar matview's options row goes with its DROP

Part of the pytest twin of `test/drop_cleanup.sh` (#1265, ported for #1341): its #1265 arms only, by the shell's names, and the pair is declared INCOMPLETE. The drop hook skipped anything that was not an ordinary table, so a matview's options row outlived it. The row is planted directly, so the hook is the only subject. REFRESH must keep the row, and an unrelated table's row is the control against a hook that deletes every row.

### Every test

| test | what it holds |
| --- | --- |
| `test_a_columnar_matview_takes_its_options_row_with_it` | a planted options row is gone after DROP MATERIALIZED VIEW |
| `test_refresh_keeps_a_matviews_options_row` | a REFRESH that rewrote the matview leaves its set_options row in place |
| `test_an_unrelated_options_row_survives` | another table's row survives a matview's DROP, and an ordinary columnar table still takes its own |

## 143. test_native_upgrade_converge.py: every release upgrades to a fresh catalog, and the window before it

The pytest twin of `test/native_upgrade_converge.sh` (#1341), every check by the shell's name, so the pair is declared COMPLETE. `ALTER EXTENSION pgcolumnar UPDATE` must bring a database on any released version to a catalog identical to a fresh install of the current `default_version`: every function's definition, ACL and comment; every relation, column, type and foreign-data wrapper; and every column default, index, constraint, row-level-security flag and policy. The released starting points are the install scripts in `test/fixtures`. They are staged into the extension directory once for the module, under a lock that makes a second stager wait, and whatever was there is put back after. A backup already there means an earlier run died, so it holds the original and is kept.

Between installing a new library and running the update, a database runs the new library over its previous release's catalog (#1341 item 66). Reads and writes must work there. `DROP TABLE`, `TRUNCATE` and `pgcolumnar.vacuum()` need a catalog table the old one lacks, so they must be refused with 42P01, change nothing, and carry a hint naming the update. After the update all three succeed on the same rows.

### Every test

| test | what it holds |
| --- | --- |
| `test_a_run_after_one_that_died_keeps_the_original` | with a died run's state in a temporary directory, staging and restoring leave the prefix's own file, not the fixture |
| `test_two_stagings_at_once_keep_the_original` | a second staging that starts inside the first, as another `pytest -n` worker's would, waits for it under the staging lock, and the prefix's own file survives both |
| `test_the_staged_fixtures_are_the_committed_ones` | the control's `default_version`; no backup was already staged; every fixture was staged, and each staged script is the committed fixture |
| `test_each_release_upgrades_to_a_fresh_catalog` | parametrized by the four fixtures: a fresh install of the target has more than 100 objects to compare, the upgrade applies, the extension is at the target, and its catalog equals the fresh install's |
| `test_the_upgrade_window` | at the previous catalog: rows written, deleted and read; the three operations refused with the hint and nothing changed; after the update the rows are intact and all three succeed |
| `test_the_fixture_sweep_found_fixtures` | the fixtures on disk, less the synthetic `1.0-alpha`, are not an empty list: an empty sweep would compare nothing and report clean |
| `test_each_fixture_is_what_its_tag_shipped` | parametrized by those fixtures: each one's blob id equals `v<version>:pgcolumnar--<version>.sql`'s; with no git repository or no release tags it cannot run (UNMET_PRECONDITION) rather than pass. CI fetches the tags for it |

## 144. test_fk_referencing.py: a foreign key referencing a columnar table is refused where it is chosen

The pytest twin of `test/fk_referencing.sh` (ported for #1341), every check by the shell's name. Enforcing a foreign key locks the referenced row, and columnar tables cannot lock rows, so a constraint referencing one would make every insert into the referencing table fail. It is refused at DDL by either route into that state: creating the constraint against a columnar table, or converting a referenced table (or partitioned parent) to columnar. Only the referenced side is refused, and the controls hold everything else steady, including converting the referenced table itself to a row store.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_referenced_side_is_refused_at_ddl` | CREATE TABLE and ALTER TABLE ADD CONSTRAINT referencing a columnar table are refused, saying why, and nothing is left behind |
| `test_the_referencing_side_still_works` | a columnar table referencing a heap table is created, accepts satisfying rows and rejects a violating one |
| `test_constraints_that_need_no_row_lock_are_unaffected` | primary key, unique, check and not-null on a columnar table, and a heap-to-heap foreign key, are unaffected |
| `test_converting_a_referenced_table_is_refused_and_nothing_else_is` | SET ACCESS METHOD pgcolumnar on a referenced table and the helper are refused, the table stays heap and its child writable; an unreferenced table, the referencing side, an unrelated ALTER, a conversion back to heap and the referenced table to a row store all still work |
| `test_a_partitioned_referenced_parent` | a columnar partition and setting the parent's access method are refused, the parent does not become columnar, an ordinary partition still works; an unreferenced partitioned table converts from PG17, and before 17 core refuses it |

## 145. test_truncate_cleanup.py: TRUNCATE leaves no catalog rows behind, and keeps what it must

The pytest twin of `test/truncate_cleanup.sh` (ported for #1341), every check by the shell's name. TRUNCATE installs a new storage id, and the old storage's catalog rows used to stay behind, so each truncate-and-reload cycle leaked one storage's worth and a later DROP removed only the empty current one. Retiring them made a cached write state stale, which is dropped with them. A relation created in the current transaction is truncated in place, on a different path, which must clear each projection's storage too. A private database, since the snapshot counts every pgcolumnar catalog row.

### Every test

| test | what it holds |
| --- | --- |
| `test_truncate_and_drop_leave_the_catalog_as_they_found_it` | one TRUNCATE and three truncate-and-reload cycles return the catalog to the baseline, and a DROP of a reloaded table leaves nothing |
| `test_a_projection_is_not_leaked_by_truncate` | no projection row names a retired storage id, the declaration survives, and a DROP of the reloaded table leaves nothing |
| `test_repeated_truncates_accumulate_nothing` | ten transactional truncates of one table, and its DROP, leave the baseline |
| `test_insert_truncate_insert_in_one_transaction` | the transaction commits and holds exactly the rows written after the TRUNCATE |
| `test_a_rewrite_that_is_not_a_truncate_loses_nothing` | a type-changing rewrite keeps all 5000 rows readable |
| `test_an_in_place_truncate_clears_the_projection_too` | the in-place truncate commits, the later rows are all there, the projection reads them back and its storage holds one group of exactly them |

## 146. test_native_dml.py: DELETE, UPDATE and MERGE on a native table agree with heap

The pytest twin of `test/native_dml.sh` (ported for #1341), every check by the shell's name. Each statement runs on a heap table and on a columnar one with 1000-row stripes, and every result is compared. After deletes an aggregate must not come from zone maps that still describe the deleted rows: the deletes include the smallest and largest ids, so min and max move, and they are taken over `k`, a column the primary key does not order. A deleted row's index lookup finds nothing, and the lookups are asserted to take the index.

### Every test

| test | what it holds |
| --- | --- |
| `test_delete_update_and_merge_match_heap` | after DELETE, UPDATE and MERGE, counts, rows, sum and min/max match heap; the index finds a live row and skips a deleted one; the primary key still rejects a duplicate |

## 147. test_native_dict_underfill.py: an under-filled DICT chunk is refused and no backend dies

The pytest twin of `test/native_dict_underfill.sh` (ported for #1341), every check by the shell's name. A DICT chunk's `value_raw_length` is inflated in its descriptor, so the codes decode to fewer bytes than declared; without the fill check the raw buffer's tail is uninitialized and a varlena length is read out of it. The read must raise XX001, and survival is read from the server log's crash count, not from a fresh connection, which succeeds once crash recovery has finished.

### Every test

| test | what it holds |
| --- | --- |
| `test_an_under_filled_dict_chunk_is_refused` | the column is DICT-encoded, the inflation rewrites its descriptor, the read is refused with XX001, and no backend died |

## 148. test_native_decode_gating.py: an unprunable qual gates payload decode per vector

The pytest twin of `test/native_decode_gating.sh` (#452 phase 2, ported for #1341), every check by the shell's name. A leading-wildcard LIKE is no skip predicate, so phase 2 evaluates the qual per 1024-row vector between the decode passes, and a vector no row passes is never decoded for its payload columns; "Columnar Vector Decodes" makes the saving exact arithmetic. The filter counters stay exact: a rejection is counted once, inside a surviving vector too, and a deleted row is not counted. The width gate is set off for the session.

### Every test

| test | what it holds |
| --- | --- |
| `test_an_unprunable_qual_gates_payload_decode` | the premises (no skip predicate, the late-materialization path); the control decodes all 192 (vector, column) pairs, a zero-match LIKE 32 and the needle 37; every filter counter is exact, including rejections inside the surviving vector; the answers match heap |
| `test_deleted_rows_across_several_groups` | with deletes over several groups the answers match heap, and Rows Removed by Filter counts only live rows |

## 149. test_native_decode_gate_width.py: decode gating runs only on a projection wide enough to pay

The pytest twin of `test/native_decode_gate_width.sh` (#595, ported for #1341), every check by the shell's name. Phase-2 gating runs only when the projection has at least `pgcolumnar.qual_skipvec_min_payload_cols` non-qual columns (default 20). The policy is pinned by moving the threshold across a fixed projection: a 24-payload `SELECT *` gates at the default, stops above 24 and gates again at exactly 24, and a three-column projection gates only once the setting is 0. The gate is output-invariant: the answers match heap with it on or off. The setting is held per session, where the shell sets it per database.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_width_gate` | the premises, including that the two rejected probe settings are undefined (42704); the decode counts at the default, with the gate off, above the width and at it; and correctness against heap with the gate on and off |

## 150. test_pg_dump_roundtrip.py: a columnar table survives pg_dump and restore

The pytest twin of `test/pg_dump_roundtrip.sh` (ported for #1341), every check by the shell's name. A source database holds a columnar table with data, an option, an index and a projection, and a second table whose declared `sort_by` names columns after a dropped one (#288). It is dumped as plain SQL and as a custom archive and restored into fresh databases, where the rows, the access method, the option, the `sort_by` and the projection declaration survive, and `rebuild_projections()` recovers the projection whose storage pg_dump cannot carry (#266). The point lookup must take the restored index, and the restored `sort_by` must leave the rows in its order.

### Every test

| test | what it holds |
| --- | --- |
| `test_dropping_a_projection_forgets_its_declaration` | a dropped projection's declaration goes with it, so a rebuild builds nothing |
| `test_a_plain_dump_round_trips` | pg_dump and psql exit 0, and the restored database passes every survival arm |
| `test_a_custom_archive_round_trips` | the same through `pg_dump -Fc` and `pg_restore --exit-on-error` |

## 151. test_pg_upgrade.py: a columnar table survives pg_upgrade

The pytest twin of `test/pg_upgrade.sh` (ported for #1341), every check by the shell's name. A cluster on an older major gets a columnar table with non-default options, deletes and an index; it is upgraded with pg_upgrade into an empty cluster on the new major, and the row count, the content byte for byte, the options, the index and writability must all survive. The index arm runs with the columnar scan off and must take the index: right after pg_upgrade there are no planner statistics, the planner took the columnar scan for the range, and the arm passed with the index dropped.

Like the shell it needs TWO majors: the new one is `--pg-config`, the old one `PGC_UPGRADE_FROM_PG_CONFIG`, and the test builds and installs the extension for the old one from a copy of the tree. The matrix runs one major at a time, so there it declines, and `expected_unrunnable.txt` lists it for every major.

### Every test

| test | what it holds |
| --- | --- |
| `test_pg_upgrade_preserves_a_columnar_table` | pg_upgrade exits 0, and the upgraded table keeps its rows, content, options and index, and takes writes |

## 152. test_replication.py: physical replication and standby redo

The pytest twin of `test/replication.sh` (ported for #1341), every check by the shell's name. A private primary gets a columnar table and a projection. pg_basebackup makes a streaming standby of it, and a second backup is brought up alone with no upstream, so what it holds came from the backup bytes. Inserts, deletes, updates, vacuum, compact and an SMGR end truncation are replayed and compared with the primary. A standby running without the module must still replay, because every record the extension emits is a core type.

Each replay arm is about a write the primary made. Vacuum must have rewritten the table into a new relfilenode. Compact must have retired a row group; after vacuum it had none to retire until the fixture gave it one. The truncate must have shrunk the primary.

### Every test

| test | what it holds |
| --- | --- |
| `test_a_streaming_standby_and_a_restore_replay_columnar_tables` | the standby and the isolated restore hold the primary's rows and projections, every write path replays identically, and a module-less standby replays |

## 153. test_native_recluster.py: online reclustering under ShareUpdateExclusiveLock

The pytest twin of `test/native_recluster.sh` (ported for #1341, item 70), every check by the shell's name. `pgcolumnar.recluster()` rewrites a table's live rows in Z-order online. It writes them back as fresh row groups, inserts their index entries, clears the visibility map over the row numbers it retires, and retires the old groups. A 2D box skips more groups afterwards, results match a heap mirror, the unique index still enforces by SQLSTATE 23505, and the lock is ShareUpdateExclusiveLock, never AccessExclusiveLock.

The index arm counts through an index-only scan of the unique index, after a VACUUM has set the visibility map, so a recluster that left the old entries' VM bits set counts them twice. The plan is asserted too: with only seqscan and the custom scan off, the count came back right with the index dropped.

### Every test

| test | what it holds |
| --- | --- |
| `test_online_recluster` | recluster tightens 2D skipping, keeps the rows, keeps the index exact through an index-only scan and the unique constraint enforced, and holds only ShareUpdateExclusiveLock |

## 154. test_alter_am_cleanup.py: DROP after SET ACCESS METHOD heap cleans up

The pytest twin of `test/alter_am_cleanup.sh` (ported for #1341, item 70), every check by the shell's name. `pgcolumnar.options` and `projection_declaration` are keyed by relid, so a table converted to heap still has rows there, and the drop hook must clear them although the table is heap by then (#304). The hook is armed in every database, because the library is preloaded. Where the extension was never installed, or has been dropped, it must not open a catalog that isn't there: an ERROR in an object-access hook aborts the statement, and every table rewrite drops a transient relation through it. It gates on `pgcolumnar.options` resolving, not on the schema, and skips only the extension's own members.

Each cleanup arm compares the catalogs with their state just before its own table was made, so one leak can't redden the arms after it. The REFRESH arm has a matview of its own, made WITH NO DATA, so it can't inherit the CREATE arm's failure.

### Every test

| test | what it holds |
| --- | --- |
| `test_drop_after_converting_to_heap_cleans_up` | a converted, a round-tripped and an in-schema table each leave no relid-keyed rows on DROP, and a never-columnar heap is left alone |
| `test_the_hook_is_harmless_where_the_extension_never_was` | DROP, temp tables and every rewrite path succeed in a database without the extension |
| `test_a_relation_older_than_the_extension_is_cleaned_up` | a table that predates CREATE EXTENSION isn't a member, so its options row goes on DROP |
| `test_the_gate_is_on_the_options_table_not_the_schema` | DROP EXTENSION succeeds with populated catalogs, and afterwards, with only the schema left, every path still works |

## 155. test_native_reclaim.py: physical page reclaim and the compaction threshold guards

The pytest twin of `test/native_reclaim.sh` (ported for #1341, item 70), every check by the shell's name. Retiring a row group records its byte range in `pgcolumnar.free_space`, and a later online compaction reserves from a freed range instead of growing the file. So repeated reclusters plateau the file, free_space stays bounded, and the reused blocks read back the heap mirror's rows. The reclusters alternate their key, because recluster self-gates on a table already in that key's order: four more `recluster('n', 'id')` calls each returned 0, and the file "plateaued" with reuse disabled.

`compact_rewrite()` and `maintenance_due()` refuse NaN, negative, above-1 and (for maintenance_due) NULL thresholds with 22023, and accept the boundaries 0.0 and 1.0. Controls pin that other failures carry other SQLSTATEs. `max_groups` caps one `compact_rewrite` call, which returns what it rewrote.

### Every test

| test | what it holds |
| --- | --- |
| `test_repeated_recluster_reuses_space_and_compact_rewrite_is_guarded` | every repeated recluster rewrites, the file plateaus, free_space doesn't grow, reused blocks read back correctly, and compact_rewrite's threshold guard holds in both directions |
| `test_maintenance_due_validates_both_thresholds` | each threshold is refused out of range or NULL by 22023 and accepted on its boundaries, and the verdict follows the deleted fraction |
| `test_max_groups_caps_one_compact_rewrite` | a capped call rewrites three of ten half-deleted groups and an uncapped one the other seven, with every live row kept |

## 156. test_native_repack.py: REPACK on a columnar table

The pytest twin of `test/native_repack.sh` (ported for #1341), every check by the shell's name. PostgreSQL 19 and later only. REPACK replaces CLUSTER and VACUUM FULL in 19 and dispatches through `relation_copy_for_cluster`, which pgColumnar registers as a stub that raises. So every spelling errors, the message names REPACK, and the table keeps its rows, content, options and access method. REPACK (CONCURRENTLY) needs two fixtures: without an identity index core refuses it before the access method is reached, and with one heap succeeds while columnar is refused.

The alternative must reclaim, not only return. With `vacuum()` returning before it rewrote anything, every arm stayed green, so the deleted rows are counted at fixture time and again after it. The twin runs on a private cluster at `wal_level=logical`, which the concurrent form needs. Below 19 it declines with `UNSUPPORTED_MAJOR`, listed in `expected_unrunnable.txt`.

### Every test

| test | what it holds |
| --- | --- |
| `test_repack_on_a_columnar_table` | every REPACK spelling is refused naming REPACK, the concurrent form is refused for a stated reason, the table is unharmed, and `pgcolumnar.vacuum()` reclaims the deleted rows |

## 157. test_native_join_vector_agg.py: the vectorized aggregate over a unique-key inner join

The pytest twin of `test/native_join_vector_agg.sh` (ported for #1341), every check by the shell's name. A star-schema inner Hash Join onto a unique dimension only filters the fact table, so the ungrouped vectorized aggregate can survive it and still answer exactly (#752). A dimension with duplicate keys, a LEFT join, and a Join Filter besides the hash clause must each refuse the fold, and every answer must equal the heap mirror's. Plans are read with parallel workers off and nested-loop and merge joins disabled, as in the shell.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_join_fold` | the fold takes a unique-key inner join and answers as core does and as heap does; duplicate keys, a LEFT join and an extra join filter each refuse it and answer as heap does |

## 158. test_native_writer.py: the native writer by its catalog rows

The pytest twin of `test/native_writer.sh` (ported for #1341), every check by the shell's name. A columnar table writes the native format, judged by the catalog rows it produces. 5000 rows, a quarter of them NULL, under a 1000-row stripe limit give one storage row at format version 2 with 1024-value vectors, five row groups holding 5000 rows, and ten column chunks counting 10000 values. DROP clears the storage, row-group and column-chunk rows for its storage id.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_native_writer_by_its_catalog_rows` | the writer's storage, row-group and column-chunk rows match the fixture, and DROP deletes all three |

## 159. test_native_batch_fold_projection.py: the batch fold's column lists on a wide table

The pytest twin of `test/native_batch_fold_projection.sh` (ported for #1341), every check by the shell's name. The ungrouped batch fold steps over compact key and payload column lists, and a short list would silently return a wrong aggregate (#10). So a 47-column table sums its last column under a selective pushed key, and the folded answer must equal the unfolded one. The fold must really have run, which EXPLAIN ANALYZE shows and a plain EXPLAIN's prediction doesn't. The reference must really be unfolded, or the comparison is of the fold with itself. And the reference must equal the generator's own answer, computed with no table at all.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_batch_fold_gathers_the_right_columns_on_a_wide_table` | the fold runs and sums the right column on a wide table, against an unfolded reference that itself matches the generator |

## 160. test_native_encdesc_golden.py: the encoding descriptor's wire layout

The pytest twin of `test/native_encdesc_golden.sh` (ported for #1341), every check by the shell's name. The encoding descriptor is the writer-to-reader contract for a column chunk's per-vector encoding, and a deterministic 300-row table pins its version 3 bytes at their offsets: version, flags, vectorCount, each entry's valueCount and rawLen, the length, and the trailer. A read that decodes closes it. `count(*)` and `sum(a)` are answered from the catalog and the zone maps, so only every row's text, compared with the generator's, needs the pages. That's what sees a reader that parses rawLen and encLen from each other's offsets.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_descriptor_layout` | the descriptor's bytes sit at their fixed offsets with the expected values, and every row decodes to what the generator wrote |

## 161. test_native_parquet_schema.py: parquet_schema round trip

The pytest twin of `test/native_parquet_schema.sh` (ported for #1341), every check by the shell's name. `pgcolumnar.parquet_schema(path)` reports a Parquet file's leaf columns with the PostgreSQL type each maps to. A columnar table covering every exported type is exported, and each source type must come back, every column nullable. A file that isn't Parquet must be refused by the magic check itself: any error was the old arm, and with the magic check gone the same file is still refused, as a corrupt footer. pyarrow supplies what the exporter can't: a REQUIRED column, and field ids out of order, with absence reported as NULL.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_exported_schema_round_trips` | every exported type and its nullability come back from `parquet_schema`, and bad magic is refused by the magic check |
| `test_required_columns_and_field_ids` | a pyarrow REQUIRED column reports nullable=f, and field ids report as written or as NULL when absent |

## 162. test_native_vecskip.py: per-vector skipping by the zone maps

The pytest twin of `test/native_vecskip.sh` (ported for #1341), every check by the shell's name. Inside a row group the predicate can't prune whole, the 1024-value vectors its zone maps rule out are neither decoded nor emitted. One row group of eight vectors and a range inside vector 0: exactly seven are skipped and no whole group is removed, and the rows equal a heap mirror's. The count alone can't say which mechanism skipped, because without the zone-map rule #452's by-value refinement rules the same seven out. So the decodes (1) and the by-value count (0) are read too. The vectorized aggregate must report the same seven.

### Every test

| test | what it holds |
| --- | --- |
| `test_per_vector_skipping` | the zone maps skip exactly the seven vectors outside the range without decoding them, on the scalar scan and the vectorized aggregate, and every range returns the heap's rows |

## 163. test_native_index_projection.py: CREATE INDEX projects only what it needs

The pytest twin of `test/native_index_projection.sh` (ported for #1341), every check by the shell's name. CREATE INDEX projects only the columns the index needs (#413), serial and parallel: each build's own DEBUG1 line says which branch ran and how many columns its reader decodes. The built indexes must then answer as a sequential scan does: point lookups through the index, and the full ordered result of an index scan against a sequential scan. The index side runs with the columnar scan off, and each oracle asserts the index its own query reads. Before that, three of the oracles ran as the columnar scan on both sides.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_built_indexes_answer_as_a_sequential_scan_does` | each serial index finds its rows, and its full ordered result equals a sequential scan's, read through that index |
| `test_each_build_projects_only_what_it_needs` | every serial and parallel build projects only its key, expression and predicate columns |
| `test_the_parallel_built_index_agrees_and_holds_each_row_once` | the parallel-built index agrees with a sequential scan and holds each row once |
| `test_amcheck_finds_every_index_sound` | `bt_index_check` finds all eleven indexes sound |

## 164. test_native_saop_pushdown.py: IN-lists and = ANY(array) prune row groups

The pytest twin of `test/native_saop_pushdown.sh` (ported for #1341), every check by the shell's name. The scan-key builder keeps an IN-list or `= ANY(array)` of up to 128 non-NULL elements as one set predicate (#704), and a group survives when any element can lie in its zone map and bloom filter. Larger sets keep the bounded [min,max] hull. The fixture is 40000 rows in 20 monotonic groups, plus an overlap column whose every group has the range [10,88] and only even values, so only the bloom filter can prune an odd probe. NOT IN builds no keys. An empty or all-NULL array neither crashes nor mis-counts. A generic plan's array parameter is frozen at Begin, and a correlated one is not. The batch fold never takes the conservative set keys as its exact filter.

### Every test

| test | what it holds |
| --- | --- |
| `test_a_set_prunes_by_element_and_keeps_the_exact_answer` | an IN-list pushes one usable set filter that removes 19 groups, a scattered set prunes by element rather than by its hull, and both sides of the 128-element bound |
| `test_nulls_types_and_collations_prune_exactly` | NULL elements, a by-reference text set, a cross-type array, a C-collated list and a domain-element array each prune and count exactly |
| `test_only_the_bloom_filter_can_prune_the_overlap_column` | with 25 inside every group's range, the bloom filter alone removes all 20 groups for a set, a single value and a single distinct value |
| `test_the_conservative_arms_build_nothing_wrong` | a range-spanning list prunes between its elements, NOT IN builds no range, and empty and all-NULL arrays count zero |
| `test_a_parameter_array_is_frozen_at_begin` | a generic plan's array parameter and a mixed literal-and-parameter list prune |
| `test_the_batch_fold_never_takes_the_set_keys_as_exact` | the batch fold refuses the IN-list but accepts the equivalent range, and the vectorized count stays exact |
| `test_a_correlated_array_is_not_frozen_and_the_backend_lives` | a correlated LATERAL array is not frozen, and the session's backend is still alive; a crash is a named red |
| `test_the_reader_refuses_every_special_flag_but_the_array_one` | the reader's reject expression names every special scan-key flag, and SK_SEARCHARRAY reaches its own arm |

## 165. test_native_groupagg_batch.py: the grouped aggregate folds column-at-a-time

The pytest twin of `test/native_groupagg_batch.sh` (ported for #1341), every check by the shell's name. The grouped vectorized aggregate folds a whole row group at a time (#708), and two things are proved separately. The answers must be unchanged: each shape agrees with a heap mirror, and with the same rows when the path is off. The work must happen: "Columnar Batch Fold: yes" under EXPLAIN ANALYZE is the only check that goes red when the fold stops running. Each shape the fold must decline is read off the grouped node, so a shape the node stops taking is a red, not a skip.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_fold_takes_every_eligible_shape_and_answers_as_the_heap_does` | seven shapes (plain, filtered, two keys, float8, count(*), timestamp and date keys) plan the grouped node, fold, and match the heap |
| `test_the_fold_declines_every_ineligible_shape_and_still_answers` | a text key, an expression key, min/max and a `<>` filter make the fold decline on the grouped node, and each still matches the heap |
| `test_the_fold_honours_the_delete_mask` | after deletes the fold still runs and matches the heap |
| `test_the_fold_equals_the_row_path_over_the_same_rows` | three shapes answer the same with the path off and on, and the off arm really is not the grouped node |
| `test_float8_keys_group_by_the_types_equality_not_by_bits` | a stored -0.0 and 0.0 are one group on the fold, as on the heap |
| `test_the_fold_steps_over_skipped_vectors` | a scan that really skips vectors folds, and its one-column and four-column sums match the heap |
| `test_the_parallel_partial_fold_equals_the_serial_one` | our parallel-aware node launches workers and folds, and equals the serial fold and the heap |
| `test_a_column_added_after_some_row_groups_runs_the_row_path` | plain EXPLAIN predicts the fold, ANALYZE reports the row path that ran, and the answer is right |

## 166. test_native_fetch_projection.py: a fetch by row number decodes only what its caller needs

The pytest twin of `test/native_fetch_projection.sh` (ported for #1341), every check by the shell's name. A row fetched by number decoded every column whatever the caller wanted (#157). Two narrower entry points exist beside it: one decodes a given set of columns, and one answers visibility and decodes nothing. Neither has a SQL surface, so they are exercised through `pgcolumnar.read_projection` and `pgcolumnar.reconstruct_via_projection` on a 41-column table, against the base table as the oracle. The source arms read code with its comments blanked, as the shell's `code_of` does.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_projected_fetch_returns_the_base_tables_rows_and_sees_deletes` | every reconstructed row matches the base table, a deleted row stops being read or reconstructed, and an uncovered column still reads right |
| `test_a_projection_covering_every_column_decodes_nothing_and_is_right` | with the computed base-column set empty, every row is reconstructed with the right values |
| `test_no_caller_decodes_every_column_and_index_deletion_reads_no_row` | the projection path calls no full decode, and index deletion asks no row whether it is live |
| `test_every_needed_set_test_consults_the_all_columns_flag` | every needed-set membership test in the reader consults the explicit all-columns flag |

## 167. test_native_fetch_position.py: a fetch by row number costs the same anywhere in its group

The pytest twin of `test/native_fetch_position.sh` (ported for #1341), every check by the shell's name. A cached row group still walked to the row: it counted the validity bits below the row, then decoded and discarded that many values (#143). A rank prefix over the validity bitmap and an offset table for varying-length columns make each fetch O(1). Every fetch here runs with the columnar custom scan off, and each measurement first asserts that its plan reads the index. With only `enable_seqscan=off`, every query scanned instead and none fetched by row number. The timing ratios read the same ranges three times. An UPDATE would write its rows into a new group, so later passes would fetch elsewhere.

### Every test

| test | what it holds |
| --- | --- |
| `test_every_fetched_value_matches_the_heap_mirror` | the group is one row group, and fixed, varying-length and heavily-null columns fetched through the index match a heap mirror, at every probed position |
| `test_the_cost_does_not_depend_on_the_position` | fetching the last twentieth of the group costs under three times the first |
| `test_a_bigger_group_does_not_cost_more_per_fetch` | the same far-end fetches in a group twice the size cost under 1.8 times as much |
| `test_the_indexes_that_make_it_constant_are_in_the_fetch` | `pgcolumnar_fetch_row` takes the rank from the prefix and a varying-length value from the offset table |

## 168. test_native_fetch_cache.py: the fetch-by-row-number cache

The pytest twin of `test/native_fetch_cache.sh` (ported for #1341), every check by the shell's name. A fetch by row number decoded a whole row group per row (#143), and a statement-scoped cache of the decoded group removes the repeat. The cache is timed as a ratio: the same index fetches from one big row group and from ten small ones. Each reading runs on a freshly built fixture, because an UPDATE moves the rows it reads into a new group (#801). Then come the scoping guards in the source, a wide group's decode scratch (#353), and crossing the cache cap (#359).

### Every test

| test | what it holds |
| --- | --- |
| `test_the_cache_makes_one_big_group_cost_what_ten_small_ones_do` | each reading measured the geometry it meant to, one big group costs under three times ten small ones, and the updated rows are right |
| `test_the_scoping_guards_are_in_the_source` | the cache key includes the storage id, an earlier command's entry is rejected, a hit re-checks four fields, and the cache is discarded at executor end and transaction end |
| `test_a_wide_groups_decode_scratch_does_not_blow_the_cap` | a wide group's point query drives the fetch path, costs under five times a small group's, and answers the same |
| `test_crossing_the_cap_costs_per_column` | four and five projected columns drive the fetch path, five costs under twelve times four, and every over-cap column reads back its value |

## 169. test_native_parquet_pushdown.py: the Parquet FDW skips row groups its statistics rule out

The pytest twin of `test/native_parquet_pushdown.sh` (ported for #1341), every check by the shell's name. The FDW reads each row group's min/max and skips a group that a pushable col-op-const clause proves empty. The executor still rechecks every row, so a sound skip can only save work. The checks run against a heap oracle, and the skips themselves are read from EXPLAIN ANALYZE's "Row Groups Skipped". The soundness cases follow. A NaN row sits outside the statistics, so a float `>`, `>=` or `= 'NaN'` must never skip. An inverted interval must never drive a skip. An INT64-backed DECIMAL is skippable. pyarrow writes every file.

### Every test

| test | what it holds |
| --- | --- |
| `test_skipping_never_changes_the_answer_and_really_skips` | eight predicates agree with the heap, each confining range skips the groups it should, float `>=` is not pushed, and `read_parquet` reads the same rows |
| `test_a_nan_row_is_never_skipped` | seven float predicates over a file with one NaN agree with the heap |
| `test_inverted_statistics_never_drive_a_skip` | a UINT_32 column whose statistics decode inverted agrees with the heap under six predicates and skips nothing |
| `test_an_int64_backed_decimal_is_skippable` | a range inside one of four DECIMAL groups skips the other three without dropping rows |

## 170. test_native_parquet_flba.py: uuid and DECIMAL read from fixed-length byte arrays

The pytest twin of `test/native_parquet_flba.sh` (ported for #1341), every check by the shell's name. The reader handles uuid as a 16-byte FLBA, and DECIMAL as big-endian two's complement at the column's scale, in FLBA, BYTE_ARRAY or a little-endian INT32/INT64. Our own export is read back through `read_parquet`, `import_parquet` and the FDW and compared with the source table. pyarrow-written files serve as the foreign producer. A footer whose DECIMAL scale has been patched out of range must be refused at bind (42804), before any decoding.

### Every test

| test | what it holds |
| --- | --- |
| `test_our_export_reads_back_through_every_surface` | uuid, numeric and whole rows round-trip through `read_parquet`, `import_parquet` and the FDW |
| `test_pyarrow_written_flba_reads_exactly` | both pyarrow uuids read in order, decimal128 values are exact, and a crafted scale is refused at bind with the backend alive |
| `test_a_decimal_held_in_an_integer_reads_as_numeric` | INT32 and INT64 DECIMALs read at their scale with their null, `parquet_schema` advises numeric, and bigint still binds to the unscaled integer |

## 171. test_native_parquet_multifile.py: directories and globs read as one relation

The pytest twin of `test/native_parquet_multifile.sh` (ported for #1341), every check by the shell's name. A directory reads every `*.parquet` file below it as one relation, and a glob expands the same way. The union must equal a single-file oracle through `read_parquet`, `import_parquet` and the FDW. Pushdown and the EXPLAIN counters aggregate across files.

Several inputs must fail: an empty directory, a glob matching nothing, a schema-mismatched file, a dangling symlink, and a tree past the depth bound. A FIFO and a directory named `*.parquet` are skipped. The walk skips names that start with `_` or `.`, and it does not follow a symlinked directory, so a loop terminates. It still follows a symlinked file, and it orders files by full path. Reads that could block are bounded, as the shell's `timeout` bounds them.

### Every test

| test | what it holds |
| --- | --- |
| `test_directories_and_globs_read_as_one_relation` | a directory and a glob equal the oracle on every surface, pushdown and counters aggregate, and every error case fails |
| `test_a_fifo_named_parquet_is_skipped_not_opened` | a FIFO named `*.parquet` is skipped rather than opened, within a bound; declined as "the FIFO case" where mkfifo is unavailable |
| `test_the_recursive_walk` | a nested tree reads as one relation in sorted path order, `_`/`.` names and symlinked directories are not walked, a symlinked file is read, and the depth bound errors |

## 172. test_native_index.py: index lookups and unique enforcement fetch by row number

The pytest twin of `test/native_index.sh` (ported for #1341), every check by the shell's name. An index scan, unique enforcement and primary-key enforcement all fetch rows by number (Phase D6a). Before that, an index scan on a native table returned no rows, and unique was silently unenforced. Lookups are compared with a heap mirror under a forced index path. The columnar custom scan is off as well as seqscan, because with only seqscan off the range aggregate planned as the custom scan. Each lookup first asserts that its plan is an Index Scan through `n_pkey`.

### Every test

| test | what it holds |
| --- | --- |
| `test_index_lookups_match_the_heap` | four lookups plan as Index Scans and match the heap, and a duplicate primary key is refused with the row count unchanged |
| `test_a_standalone_unique_index_enforces` | a standalone unique index refuses a duplicate and admits a new key |

## 173. test_native_fetch_bigcap.py: fetching from a row group above the cache cap

The pytest twin of `test/native_fetch_bigcap.sh` (ported for #1341), every check by the shell's name. Above the 32 MB fetch-cache cap, the entry used to be dropped whole after every fetch, so each fetched row re-read the whole group (#433). The measure is buffers, a count rather than a time. The fixture's premise is its on-disk size, read back from `pgcolumnar.row_group`. Every measured query is forced onto the index path with the custom scan off, and asserts its plan. A payload wider than the cap re-decodes per fetch by design (#359). A narrow projection, and columns that fit, must still be cached.

### Every test

| test | what it holds |
| --- | --- |
| `test_a_group_above_the_cap_is_read_once_for_what_fits` | the fixture has a group above the cap, every measured query is an index fetch, and ten rows of a narrow or a fitting projection cost under three times one row |
| `test_caching_changes_no_row` | the first ten rows equal the same rows read in key order, and every payload is full length |

## 174. test_sorted_projection.py: vacuum_sorted reorders storage and changes no answer

The pytest twin of `test/sorted_projection.sh` (ported for #1341), every check by the shell's name. `pgcolumnar.vacuum_sorted()` rewrites a columnar table physically sorted on a key. It must never change an answer: every query shape agrees with a heap oracle before and after, order-independently, and in order where the query orders. It must also reorder storage for real, so a narrow range on the sort key skips vectors it could not skip before. The rest covers a multi-column key, the reclaim of deleted rows with the index rebuilt, idempotence, and argument validation. Each index lookup is forced with the custom scan off and asserts that it reads the index. Each refusal must be the function's own, identified by its SQLSTATE.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_sorted_rewrite_changes_no_answer_and_really_reorders` | five query shapes match the heap before and after the sort, the ordered oracle is order-sensitive, and the sort makes the range skip vectors |
| `test_a_multi_key_sort_reclaims_and_rebuilds_the_index` | a two-column sort matches, deletes are reclaimed, the rebuilt index finds and hides the right rows, and a second sort changes nothing |
| `test_vacuum_sorted_refuses_what_it_cannot_sort` | a heap table, an unknown column, no sort key and a json key are each refused with their own SQLSTATE |

## 175. test_native_ios.py: an index-only scan skips the fetch on all-visible row groups

The pytest twin of `test/native_ios.sh` (ported for #1341), every check by the shell's name. Once VACUUM has marked a native table's all-visible row groups in the visibility map, an index-only scan over an all-visible range skips the fetch (Heap Fetches: 0) and returns a heap mirror's rows. VACUUM also records the all-visible pages in `pg_class` (#507). They must be in pages, not the VM's synthetic blocks, because core prices an index-only scan by `relallvisible / relpages`. A delete clears the VM bit, so the scan falls back to the fetch there and never returns a deleted row.

### Every test

| test | what it holds |
| --- | --- |
| `test_an_all_visible_range_needs_no_fetch_and_vacuum_records_it` | the index-only scan is chosen, fetches nothing, matches the heap, and VACUUM records a page count no larger than the relation and most of it, as the heap's is |
| `test_a_delete_clears_the_bit_and_is_never_returned` | after a delete the scan matches the heap and fetches where the bit was cleared |

## 176. test_native_truncate.py: end-truncation shrinks the file and loses no row

The pytest twin of `test/native_truncate.sh` (ported for #1341), every check by the shell's name. `pgcolumnar.truncate()` returns trailing reclaimed blocks to the OS. After the trailing half is deleted and compacted, the file shrinks and the rows still match a heap mirror. A second truncate has nothing to do, and the table stays usable: reinsert, index, and an indexed lookup forced through the index and asserted to read it. With `pgcolumnar.enable_end_truncation` off, truncate must return 0 and leave the file alone. The call is a statement of its own on both sides, because sent with the `SET` as one string it ran in an implicit transaction block and was refused, which passed whatever the GUC did. Inside a transaction block truncate is refused and changes nothing, because its shrink is not transactional, and a truncate followed by reinsert and compaction keeps parity.

### Every test

| test | what it holds |
| --- | --- |
| `test_truncate_shrinks_the_file_and_keeps_every_row` | a truncate with nothing freed returns 0; after delete and compact it returns blocks, the file shrinks, the rows match the heap, and a second truncate returns 0 |
| `test_the_table_stays_usable_and_the_guc_turns_it_off` | reinsert and an index lookup through the index after truncation; with the GUC off truncate returns 0 and the file keeps its size, with it on the file shrinks |
| `test_truncate_refuses_a_transaction_block_and_reuse_stays_clean` | refused inside BEGIN with the file and rows unchanged; truncate, reinsert and compaction keep parity |

## 177. test_native_gap.py: a write beyond EOF fills the gap and loses no row

The pytest twin of `test/native_gap.sh` (ported for #1341), every check by the shell's name. The metapage highwater can sit ahead of the physical EOF, which is the state an aborted or crash-interrupted end-truncation leaves. A write whose target block lies beyond EOF must fill the gap with empty pages and succeed. A test-only hook, bound by the test as the shell binds it, pushes the highwater 50 pages past EOF. The next insert must keep every row against a heap mirror and grow the file by at least 40 pages. The table then stays usable: another insert, an index lookup forced through the index and asserted to read it, and a delete.

### Every test

Each check is its own test over a module fixture that runs every step in the shell's order and records each reading, so one red cannot stop the checks after it.

| test | what it holds |
| --- | --- |
| `test_baseline_row_count` | 3000 rows before the gap |
| `test_baseline_parity` | and they match the heap |
| `test_row_count_after_gapped_insert` | 6000 rows after the insert across a 50-page gap |
| `test_parity_after_gapped_insert` | and they match the heap |
| `test_gap_was_physically_materialized` | the file grew by more than 40 pages |
| `test_parity_after_post_gap_insert` | a later insert keeps parity |
| `test_the_indexed_lookup_reads_n_id` | the forced lookup plans an Index Scan on `n_id` |
| `test_indexed_lookup_after_gap` | and finds all 7000 rows |
| `test_parity_after_delete_on_gapped_layout` | a delete keeps parity |

## 178. test_native_roundtrip.py: a native table reads back what the heap holds

The pytest twin of `test/native_roundtrip.sh` (ported for #1341), every check by the shell's name. A native-format table with nulls in a text and a float column is read back through the native reader and compared with a heap mirror holding the same rows. Its 5000 rows are split into five row groups, and a check asserts the count. The row count is answered from metadata, so only the hashed reads see a lost row group. The comparisons cover every column, a filtered scan, a projection, the null counts, an aggregate and a filtered count. Rows are hashed order-independently, so the heap is the oracle.

### Every test

Each check is its own test, parametrized by the shell's name, so one red cannot stop the others.

| test | what it holds |
| --- | --- |
| `test_native_row_count` | 5000 rows, as metadata counts them |
| `test_the_native_table_holds_five_row_groups` | five row groups, so the read crosses them |
| `test_a_read_matches_the_heap[...]` | every column, a filtered scan and a projection match the heap, one test per shell name |
| `test_a_null_count_or_aggregate_matches_the_heap[...]` | the null counts of the text and float columns, `sum(k)` and `count(*) WHERE b` match the heap, one test per shell name |

## 179. test_native_bloom.py: a bloom filter prunes the equality probe min/max cannot

The pytest twin of `test/native_bloom.sh` (ported for #1341), every check by the shell's name. A native table carries a bloom filter per column chunk over each hashable column. An equality probe on an unsorted column can then skip row groups that provably lack the value, even where the min/max zone map cannot. The fixture spreads its values so that every group's range spans the domain. Groups are therefore skipped only with `pgcolumnar.enable_bloom_filter` on, and the probe's answer matches a heap mirror. The skip counter reads -1 when the plan has none, so a plan without the columnar scan can't pass as "skipped nothing".

### Every test

Each check is its own test, so one red cannot stop the next.

| test | what it holds |
| --- | --- |
| `test_row_count` | 20480 rows |
| `test_bloom_rows_written` | one filter per row group for the tag column |
| `test_the_plan_under_test_is_a_columnar_custom_scan` | the probe plans the columnar scan, the only source of the skip counter |
| `test_equality_probe_parity` | the probe for a present value matches the heap |
| `test_bloom_on_skips_groups` | the probe skips groups with the bloom on |
| `test_bloom_off_skips_nothing` | and none with it off |
| `test_absent_value_parity` | a value outside every group's range matches the heap |

## 180. test_native_toasted_write.py: a toasted value is written once and read back whole

The pytest twin of `test/native_toasted_write.sh` (ported for #1341), every check by the shell's name. The writer flattens each varlena once per row and reuses the flat value for the encoder, the bloom hash and the min/max (#445). The source is a heap table whose wide column is stored pglz-compressed (named, since PG19 defaults to lz4), so the columnar writer is handed already-toasted values. The columnar table is six row groups, so a zone map or a bloom can prune a group. The checks cover byte-identity with a heap mirror, two ranges that each select some rows but not all, one bounded below so the max is asked and one bounded above so the min is, and an equality probed with the value itself, so it is a usable skip predicate and the bloom is asked.

### Every test

Each check is its own test.

| test | what it holds |
| --- | --- |
| `test_the_source_values_are_toasted` | the source column reads back pglz |
| `test_toasted_values_are_byte_identical` | the wide column's digest matches the heap |
| `test_each_range_selects_some_rows_and_not_all` | both ranges are selective on the heap |
| `test_the_min_max_zone_map_is_correct` | `big >= 'e'`, pruned by the max, matches the heap |
| `test_the_min_max_zone_map_is_correct_below_a_bound` | `big < '1'`, pruned by the min, matches the heap |
| `test_the_equality_probe_reaches_the_bloom` | the probe is a usable skip predicate |
| `test_equality_on_a_toasted_value_matches_heap` | and its count matches the heap |
| `test_the_non_toasted_column_is_byte_identical` | the short column's digest matches the heap |

## 181. test_native_varlena_bound.py: a corrupt varlena length is refused, not read past its buffer

The pytest twin of `test/native_varlena_bound.sh` (ported for #1341), every check by the shell's name. Every decode site reads a varlena's stored length through `PgColumnarVarSizeAnyUnalignedBounded`, which refuses a length, or an external-TOAST tag, that would read past the value stream. The zone map's min/max is a catalog bytea, so a plain UPDATE reaches it: a 5-byte value whose header claims ~1 GB, then a predicate that consults it. The text column is corrupted, since the int id's min/max is no varlena. A length far past the stream cannot tell a correct bound from an off-by-one, so the boundary is planted too: a length that fits exactly, one that runs one byte past, and a 4-byte header with fewer than 4 bytes left. The minimum is restored before the maximum is corrupted, because the zone map decodes the minimum first and a corrupt minimum left in place would answer for the maximum. A crash is read from the server log, counted from each corruption, since a liveness probe alone passes once crash recovery finishes. It runs on a private cluster, because a crash drops every session on the server.

### Every test

Each check is its own test.

| test | what it holds |
| --- | --- |
| `test_the_txt_column_has_a_whole_chunk_zone_map_to_corrupt` | there is one zone-map row to corrupt |
| `test_a_min_with_an_over_long_varlena_length_is_refused` | a ~1 GB minimum raises XX001 |
| `test_backend_survived_the_corrupt_minimum` | and no backend died |
| `test_with_the_minimum_restored_the_range_read_is_clean` | the restored minimum reads back all 2000 rows |
| `test_a_max_with_an_over_long_varlena_length_is_refused` | a ~1 GB maximum raises XX001 |
| `test_backend_survived_the_corrupt_maximum` | and no backend died |
| `test_an_external_toast_tagged_min_value_is_refused` | an external-TOAST tag raises XX001 |
| `test_backend_survived_the_external_tagged_minimum` | and no backend died |
| `test_a_min_whose_length_exactly_fits_its_buffer_reads_cleanly` | an exact-fit length reads all 2000 rows |
| `test_a_min_whose_length_runs_one_byte_past_its_buffer_is_refused` | one byte past raises XX001 |
| `test_a_4_byte_header_cut_off_by_the_buffers_end_is_refused` | a header with fewer than 4 bytes left raises XX001 |

## 182. test_native_agg_addcolumn.py: a metadata aggregate over an added column reads what the metadata cannot hold

The pytest twin of `test/native_agg_addcolumn.sh` (ported for #1341), every check by the shell's name. An ungrouped, unfiltered aggregate is answered from row-group metadata, but a column added by ALTER TABLE ADD COLUMN has no chunk, and so no zone map, in a row group written before it. The fold must send such a group to be read, where the reader supplies the attribute's missing value. Each aggregate is compared with the same bare query on a second connection that starts with `pgcolumnar.enable_vectorization` off, as the shell's PGOPTIONS does. The aggregates run over a column added with a constant default, one added with none, groups on both sides of the ALTER, and the same after a delete in the newer group (a group with deletes goes whole to the scan, so a delete in the only pre-ALTER group would keep it from the fold under test).

### Every test

Each check is its own test over a module fixture that runs every step in the shell's order and records each reading, so one red cannot stop the checks after it. A failed query is a `query_error()`, refused on either side by name.

| test | what it holds |
| --- | --- |
| `test_the_metadata_aggregate_path_is_the_one_under_test` | `count(d)` plans the metadata node |
| `test_the_oracle_reads_the_rows` | premise: the oracle's connection plans `Custom Scan (PgColumnarScan)`, so the two sides take different paths; an empty or other plan cannot pass |
| `test_the_scan_reads_an_added_columns_default_for_the_rows_written_before_it` | the oracle's own `count(d)` and `sum(d)` are 3000 and 21000: both paths read old groups through the reader's missing value, so agreement alone cannot see it lost |
| `test_the_metadata_path_agrees_with_the_scan` | parametrized by the shell's 16 names: count, sum, min, max and avg over the added columns, count(*), and the same across both sides of the ALTER and after a delete |

## 183. test_native_groupagg_wide_cost.py: the grouped fold prices its own input scan as the scan node does

The pytest twin of `test/native_groupagg_wide_cost.sh` (ported for #1341), every check by the shell's name. The grouped vector aggregate prices the scan it performs from the cheapest serial scan path that survived add_path. When none survived, because a Gather over the partial path dominated it, it prices that scan itself, and must do so as the columnar scan node does, not with the bare seqscan formula (#12). On the suite's 40,000-row wide table a serial scan survives, so the fold and result readings never reach that fallback. Free parallelism (`parallel_setup_cost`, `parallel_tuple_cost` and `min_parallel_table_scan_size` at 0) lets a Gather displace it, and with `enable_hashagg` and `enable_sort` off the parallel finalize cannot win, so the fold node is the plan and its cost can be read. Each reading's settings ride in the connection's options, as the shell's PGOPTIONS do.

### Every test

Each check is its own test over a module fixture that takes every reading in the shell's order, each on a connection of its own. A failed query is a `query_error()`, refused on either side by name.

| test | what it holds |
| --- | --- |
| `test_the_fold_path_is_used_on_the_wide_grouped_aggregate` | the plan carries `Columnar Batch Fold: yes`, not merely a custom scan, which the reference plan's input is too |
| `test_the_fold_result_matches_a_plain_aggregate_on_a_wide_table` | the folded per-group sums and counts equal the plan without vectorized grouping |
| `test_with_parallelism_free_a_gather_displaces_the_serial_scan` | premise: with parallelism free the plan is a Gather, the state in which the fallback prices the scan |
| `test_the_fallback_prices_the_fold_as_a_surviving_serial_scan_does` | the fold node's cost through the fallback equals its cost when a serial scan survives (800.19 on PG17; 503.01 with the bare seqscan formula) |
| `test_backend_alive` | the backend still answers |

## 184. test_hardening.py: corrupted native metadata is refused or ignored, never trusted

The pytest twin of `test/hardening.sh` (ported for #1341), every check by the shell's name. A plain UPDATE corrupts a chunk's encoding descriptor, then a bloom filter, both catalog byteas. The descriptor must be refused with a clean XX001 and a surviving backend; the malformed bloom must be read as "may match", so the probe for a present value still finds it. An empty result is not read as the error, since a crash and a missing table give one too, and a crash is counted from the server log: a liveness probe alone passes once recovery has finished. On a private cluster, because a crash drops every session on the server.

### Every test

Each check is its own test over a module fixture that runs every step in the shell's order, each reading on a connection of its own opened once any recovery is over. A failed query is a `query_error()`, refused on either side by name.

| test | what it holds |
| --- | --- |
| `test_the_corrupt_descriptor_replaces_one_chunks` | premise: the descriptor UPDATE reports `UPDATE 1` |
| `test_an_invalid_descriptor_raises_an_error` | reading the table raises XX001 |
| `test_the_backend_is_alive_after_an_invalid_descriptor` | the server answers and no backend died since the corruption |
| `test_the_malformed_bloom_replaces_ks_filter` | premise: the bloom UPDATE reports `UPDATE 1` |
| `test_a_corrupt_bloom_still_finds_a_present_value` | the equality probe returns the heap's rows |
| `test_a_corrupt_bloom_still_counts_an_absent_value` | the absent probe's count equals the heap's |
| `test_the_backend_is_alive_after_a_corrupt_bloom` | the server answers and no backend died since the corruption |

## 185. test_dependency_estimate.py: a functional-dependency estimate is not collapsed by a truncated extended MCV

The pytest twin of `test/dependency_estimate.sh` (ported for #1341), every check by the shell's name. Core applies a capacity-truncated extended MCV before the functional dependency, which clamps the joint estimate of `a=5 AND b=10` to 1 against an actual 300 (#438); the columnar path recomputes a dependency-consistent estimate and raises the row count to it when core's is lower. Three tables of 300,000 rows: the #438 case, probed at a pair outside its truncated extended MCV (1000 values, `b = a*2`, full extended statistics), a case core already gets right (10 values, each an MCV), and one with no extended statistic.

### Every test

A module fixture builds the three tables on a private database and takes every reading in the shell's order. An estimate is the top plan node's `rows=`, or a `query_error()` when EXPLAIN fails or prints none, so a missing estimate is never read as 0 (the shell's `${EST:-0}` did, which passed "bounded" and "untouched" with every EXPLAIN failing).

| test | what it holds |
| --- | --- |
| `test_the_438_probe_is_outside_the_truncated_mcv` | premise: the probe is the first pair the extended MCV does not hold (the clamp happens only then; a fixed `a=5` sat in the MCV in 1 run of 6, and the arm then passed with the correction off) |
| `test_the_438_case_really_matches_about_300_rows` | premise: the #438 probe matches 300 rows |
| `test_the_dependency_estimate_is_not_collapsed` | the #438 estimate is at least 100, not clamped to the floor |
| `test_and_it_is_not_an_over_correction` | and at most twice the actual count |
| `test_a_case_core_already_estimates_well_is_unchanged` | the 10-value table's estimate is within 20% of its actual count |
| `test_a_table_with_no_dependency_statistic_is_untouched` | with no extended statistic the estimate stays at core's (at most 10) |

## 186. test_batch_fold_explain.py: EXPLAIN ANALYZE reports the fold that ran, not the one predicted

The pytest twin of `test/batch_fold_explain.sh` (ported for #1341), every check by the shell's name. A column added after some row groups exist is predicted fold-eligible, but the first old group lacks it, so the ungrouped vectorized aggregate refuses before claiming a group and the scan runs the row path. EXPLAIN ANALYZE must say `Columnar Batch Fold: no` for that run (#602), plain EXPLAIN keeps the prediction, a control table whose columns exist from the start genuinely folds, and the parallel partial fold, which cannot take the fallback, refuses the relation with an error.

### Every test

A module fixture builds `bfe_old` (50,000 rows before `ADD COLUMN v`, 10,000 after) and `bfe_ctrl` on a private database and takes every reading in the shell's order, each on a connection of its own with the suite's settings in its options. A plan with no `Columnar Batch Fold` line reads `""`, which `expect.text` refuses; a failed query is a `query_error()`.

| test | what it holds |
| --- | --- |
| `test_the_fixture_loaded_every_row` | premise: `bfe_old` holds 60,000 rows |
| `test_the_fallback_table_returns_the_right_sum` | premise: the fallback table's sum equals an oracle over `generate_series` |
| `test_the_control_table_returns_the_right_sum` | premise: so does the control table's |
| `test_analyze_reports_the_fold_that_ran` | ANALYZE on the control reports `yes` |
| `test_plain_explain_still_reports_the_prediction` | plain EXPLAIN on the fallback table reports `yes` |
| `test_analyze_reports_the_row_path_that_ran` | ANALYZE on the fallback table reports `no` |
| `test_the_planner_gives_the_parallel_partial_fold` | premise: with parallelism free the plan is a Gather over the fold (planned on 15-19; the shell used to print a note and drop the next check when it was not) |
| `test_the_parallel_partial_fold_refuses_the_fallback` | the parallel partial fold errors with "cannot fold a relation with a column added" |

## 187. test_zonemap_cost.py: a scan is priced for what the zone maps let it skip, and only for that

The pytest twin of `test/zonemap_cost.sh` (ported for #1341), every check by the shell's name. The cost model used to quote a full columnar scan whatever the zone maps excluded, so an index scan priced at its best case won a plan it ran 11.6 times slower (#434). One 200,000-row table holds three keys over the same values: `seq` ascends with physical order and prunes 16 of 20 row groups; `scat` is scattered and prunes none, the control a blanket discount would fail; `saw` is uncorrelated globally but tight inside each group, so it prunes as hard as `seq` while its correlation says otherwise (#461). Pruning is measured from EXPLAIN ANALYZE's chunk-groups-removed counter before any price is compared (#477). Plans and cost relations only, never wall clock. On a private database, because `pg_stats` is read by table name.

### Every test

Each check is its own test over a module fixture that runs every step in the shell's order, each reading's SETs on the connection that runs it. A failed query is a `query_error()`, refused on either side by name.

| test | what it holds |
| --- | --- |
| `test_every_row_loaded` | premise: 200,000 rows |
| `test_the_ordered_key_really_is_correlated`, `test_the_scattered_key_really_is_not` | premise: \|correlation\| above 0.9 for `seq`, below 0.2 for `scat` |
| `test_the_correlated_arm_actually_removes_chunk_groups` | `seq` removes chunk groups: an arm, since a cross-type scan key the reader drops removes none (#477) |
| `test_the_scattered_arm_removes_none` | premise: `scat` removes none, so the two differ physically |
| `test_the_columnar_cost_is_a_measurement`, `test_the_index_cost_is_a_measurement` | premise: both costs on `seq` were read and are positive |
| `test_a_correlated_restriction_picks_the_columnar_scan` | the plan for `seq` is the columnar scan |
| `test_it_is_priced_below_the_index_scan_it_used_to_lose_to` | and costs less than the index scan |
| `test_the_scattered_columnar_cost_is_a_measurement` | premise: the cost on `scat` was read |
| `test_an_uncorrelated_restriction_gets_no_pruning_discount` | `scat` costs more than twice what `seq` costs: a margin, because a bare comparison passed under a blanket discount |
| `test_the_sawtooth_column_is_uncorrelated`, `test_it_prunes_as_hard_as_the_correlated_column` | premise: `saw` has \|correlation\| below 0.2 and removes at least as many groups as `seq` |
| `test_the_sawtooth_columnar_cost_is_a_measurement` | premise: the cost on `saw` was read |
| `test_a_demonstrably_pruning_restriction_is_discounted` | `saw` costs under 0.8 of `scat` |
| `test_it_is_priced_close_to_the_correlated_arm` | and under 1.5 times `seq` |
| `test_the_planner_picks_the_columnar_scan_for_it` | the plan for `saw` is the columnar scan |
| `test_the_correlated_query_returns_the_same_rows_either_way` | the grouped counts agree with the custom scan on and off |
| `test_the_correlated_query_returns_the_right_count` | 40,000 rows match `seq` |

## 188. test_concurrent_diff.py: concurrent writers to one columnar relation end equal to a heap oracle

The pytest twin of `test/concurrent_diff.sh` (ported for #1341), every check by the shell's name. Six backends run the same INSERT/DELETE/UPDATE workload over disjoint id ranges against a heap table and a columnar one (`chunk_group_row_limit` 500, `stripe_row_limit` 2000), so the final state is deterministic whatever the interleaving and the columnar table must end equal to the heap. It exercises stripe and row-number reservation, the delete vector and the encoding and bloom write paths under concurrent writers.

### Every test

A module fixture runs six threads, each on a connection of its own sending its workload as one simple-protocol query (one implicit transaction, as `psql -c`), then takes every reading. Each comparison hashes both sides on the server as lib.sh's `pgc_set_hash` does; a failed query is a `query_error()`.

| test | what it holds |
| --- | --- |
| `test_all_workers_succeeded` | every worker's transaction committed |
| `test_every_worker_started_before_any_finished` | premise: the workers overlapped (the latest start precedes the earliest end), or this is a serial differential |
| `test_the_whole_row_agrees` | every row equals the heap's |
| `test_count_and_sum_agree` | count, sum(v) and count(distinct cat) agree |
| `test_the_price_aggregates_agree` | min, max and count of the float8 column agree |
| `test_the_bloom_equality_agrees` | the count for `v = 900` agrees |
| `test_the_range_agrees` | the count for `id BETWEEN 4000 AND 12000` agrees |
| `test_the_row_count_is_sane` | the columnar table is not empty |

## 189. test_write_minmax_fastpath.py: zone min/max through the direct comparison path are the true bounds

The pytest twin of `test/write_minmax_fastpath.sh` (ported for #1341), every check by the shell's name. The writer tracks each chunk's minimum and maximum, comparing integer-family values directly (#155) and testing the maximum first. A bound that is wrong skips a group holding matching rows, so the query silently returns fewer. So the checks compare a columnar table with a heap mirror, over predicates on chunk-group bounds. Floats stay off the direct path: btree orders NaN above every value, and a C comparison never would. On a private database.

### Every test

Each check is its own test over a module fixture that runs every step in the shell's order. A failed query is a `query_error()`, and a comparison counts a failure on either side as a mismatch, never as agreement.

| test | what it holds |
| --- | --- |
| `test_every_column_returns_the_same_rows_as_heap` | sixteen range and equality predicates on every column type return heap's counts |
| `test_min_and_max_match_heap_on_every_column` | each column's min and max, answered from the zone maps, equal heap's |
| `test_the_narrow_range_plan_is_a_columnar_custom_scan` | premise: the narrow-range query plans the columnar scan |
| `test_a_narrow_range_still_skips_row_groups` | that query removes chunk groups, so the bounds were not widened |
| `test_the_float_fixture_is_one_chunk_group` | premise: the float table is one group, with NaN and the infinities at its end |
| `test_a_float_reading_matches_heap` | parametrized by the shell's five names: NaN as the float8 and real maximum, NaN by equality, and each infinity by an equality of its own (an OR never prunes a group) |
| `test_float_types_are_not_on_the_direct_comparison_list` | the writer's source names no float direct-comparison kind |

## 190. test_catalog_natts.py: every Natts_ constant equals the width of the catalog it sizes

The pytest twin of `test/catalog_natts.sh` (ported for #1341), every check by the shell's name. The `Natts_*` constants in `src/columnar_metadata.c` size the stack arrays handed to `heap_modify_tuple`, which iterates the live tuple descriptor, so a constant smaller than its catalog is a stack overflow (`Natts_options` stayed at 7 when `options` grew to 9, and a RENAME COLUMN on a sorted table aborted the backend). Each constant is compared with the width the LIVE SERVER reports, not the shipped `.sql`, since an upgrade script changes the server without changing any CREATE TABLE text.

### Every test

The source is read with the shell's own patterns; the widths come from `pg_attribute` on the cluster. A failed query is a `query_error()`.

| test | what it holds |
| --- | --- |
| `test_the_metadata_source_is_where_we_think_it_is` | premise: `src/columnar_metadata.c` exists |
| `test_the_source_defines_natts_constants` | premise: it defines `Natts_*` constants |
| `test_those_constants_size_arrays` | premise: they size arrays (`[Natts_...]`), so a wrong one is a real overflow |
| `test_heap_modify_tuple_is_still_called_here` | premise: `heap_modify_tuple`, the consumer, is still called there |
| `test_each_constant_matches_its_catalogs_width` | parametrized by the constants: each equals its catalog's live width (`Natts_native_storage` addresses `storage`); a catalog absent from the server fails its own premise |
| `test_every_constant_found_was_swept` | premise: every constant found was compared |

## 191. test_write_fsst_compressed.py: FSST is kept only where it wins after the block compressor

The pytest twin of `test/write_fsst_compressed.sh` (ported for #1341), every check by the shell's name. FSST used to be picked per vector against the raw length, but what reaches disk is the stream after the codec, and FSST turns repetitive text into codes that compress worse than the text did. The writer now keeps FSST only when it is smaller after the codec. Three shapes of 300,000 rows: short `'s' || g` and a repeated md5, where FSST loses under zstd, and an e-mail-like control, where it must be kept. The choice is read from the chunk descriptor (type 8 per vector), which does not depend on the zstd build. On a private database.

### Every test

Each check is its own test over a module fixture that runs every step in the shell's order. A failed query is a `query_error()`. A round-trip reading is `mismatches/rows` over a full join against a heap mirror built from the same generator, never copied from the columnar table, and a vector count says `no vectors` for a column with none, so neither an empty table nor a consistently wrong decoder can read as agreement.

| test | what it holds |
| --- | --- |
| `test_every_value_round_trips_where_fsst_is_dropped` | a table with nulls, empty strings and short values matches its heap mirror: `0/300000` |
| `test_the_null_and_empty_values_survive` | the null, empty and row counts equal the heap's, which holds every row |
| `test_every_value_round_trips_where_fsst_is_kept` | the e-mail table matches its heap mirror: `0/300000` |
| `test_fsst_is_not_chosen_for_text_the_codec_compresses_better_alone` | no vector of the short shape under zstd is FSST |
| `test_fsst_is_not_chosen_where_it_cost_more_write_time_for_a_larger_file` | none of the md5 shape is either |
| `test_fsst_is_still_chosen_where_it_genuinely_wins` | every vector of the e-mail shape under zstd is FSST: the control a fix that removed FSST, or a wrongly reused verdict, would fail |
| `test_with_compression_off_fsst_is_still_chosen` | parametrized by the shell's two names: with no codec, the short and md5 shapes keep FSST |
| `test_values_round_trip_with_compression_off` | the short shape uncompressed matches its heap mirror: `0/300000` |

## 192. test_zonemap_estimate_sample.py: the planner's zone-map sample is one-based, complete and per-column

The pytest twin of `test/zonemap_estimate_sample.sh` (ported for #1341), every check by the shell's name. The zone-map survival estimator prices a restricted columnar scan by sampling row groups and asking the reader's own skip predicates how many survive. Its stride ran over groups 0 to ngroups-1, but group numbers start at 1, so the first probe hit a group that cannot exist and the newest group was never probed, and the same predicate was priced by where its groups sat. Each probe also fetched every column's zone-map rows to read one column's. And the estimate followed the planning session's `stripe_row_limit` rather than the limit the table was written at (#817). The instrument is `idx_tup_fetch` on `pgcolumnar.zone_map`, split into planning (EXPLAIN) and execution, and the estimator's own DEBUG1 report. On a private database, because `pg_stat_reset()` resets the whole database's counters.

### Every test

Each check is its own test over a module fixture that runs every step in the shell's order, each reading on a connection of its own. A failed query is a `query_error()`, refused on either side by name.

| test | what it holds |
| --- | --- |
| `test_w_loaded_20000_rows`, `test_n_loaded_20000_rows` | premise: each table holds its 20000 rows |
| `test_w_carries_the_per_table_stripe_row_limit` | premise: `w` carries the 2000-row option, not the session GUC |
| `test_w_has_10_row_groups_numbered_1_to_10` | premise: the zone map has groups 1 to 10, so there is no group 0 |
| `test_the_whole_chunk_row_sorts_first` | premise: the whole-chunk row (vector -1) is the first a per-column probe meets |
| `test_planning_probes_the_zone_map_at_all` | EXPLAIN fetches zone-map tuples; a failed statement is a failed reading, never a count of 0 |
| `test_census_planning_reads_one_column_across_all_10_groups_w`, `test_census_planning_reads_one_column_across_all_10_groups_n` | planning fetches exactly 10 tuples on each table: one column, every group |
| `test_width_the_planners_reads_do_not_scale_with_table_width` | the 30-column and 2-column tables cost planning the same fetches |
| `test_the_executors_reads_stay_width_independent_too` | so do the executor's |
| `test_the_estimator_reports_a_session_of_its_own` | the estimator's DEBUG1 report appears |
| `test_it_probes_once_per_group_across_all_10_groups` | it reports 10 probes |
| `test_it_opens_zone_map_once_for_the_whole_sample` | and one open of `zone_map` |
| `test_a_predicate_matches_its_rows` | parametrized by the shell's four names: each predicate returns its rows |
| `test_a_cost_was_extracted` | premise: all four scan costs were read |
| `test_the_mirror_pair_shares_a_row_estimate`, `test_the_narrow_pair_shares_a_row_estimate` | premise: each pair has one row estimate, so only survival differs |
| `test_mirror_the_six_newest_groups_cost_what_the_six_oldest_cost` | the newest and oldest six groups cost the same |
| `test_narrow_the_two_newest_groups_cost_what_the_two_oldest_cost` | so do the newest and oldest two |
| `test_a_predicate_matching_nothing_is_floored` | a predicate matching no group costs at least 1 |
| `test_matching_only_the_last_group_costs_the_one_group_share` | the last group alone costs the same floor |
| `test_a_pruning_predicate_still_returns_the_right_rows`, `test_a_predicate_matching_nothing_still_returns_no_rows` | the pruned scans return 1000 and 0 rows |
| `test_gsess_carries_no_per_table_option`, `test_gsess_was_written_at_2000` | premise: `gsess` has no option and 10 groups, written under the session GUC |
| `test_a_cost_was_extracted_from_both_arms` | premise: both GUC readings gave a cost |
| `test_the_estimate_does_not_move_with_the_planning_sessions_guc` | planned at the default 150000 and at the written 2000, the cost is the same |
| `test_the_discount_is_real` | two of ten groups are priced below a full scan |
| `test_gsess_reads_back_correctly` | `gsess` returns its 4000 rows |

## 193. test_wal_envelope.py: pgColumnar emits only core's WAL, inside RelationTruncate's envelope

The pytest twin of `test/wal_envelope.sh` (ported for #1341), every check by the shell's name. An extension that registers a resource manager or record type of its own ties the WAL stream to its build, so a standby or recovery without that build cannot replay it. Both halves read the source and run no server: the shell with grep and awk, the twin with Python's `re`. The one direct record, `XLOG_SMGR_TRUNCATE` in `PgColumnarTruncateMainFork`, must keep core's crash-safety order.

### Every test

Each check is its own test. Every "none found" count reads `no .c files under ...` when there was no source to scan, and every reading inside the function reads `PgColumnarTruncateMainFork not found` when the function is missing, so an empty or misplaced `src/` cannot pass as "no custom WAL".

| test | what it holds |
| --- | --- |
| `test_no_custom_resource_manager_is_registered` | no file under `src/` calls `RegisterCustomRmgr` or defines an `RmgrData` |
| `test_no_rmgr_id_other_than_the_smgr_truncate_is_used` | every `XLogInsert(RM_...` names `RM_SMGR_ID` |
| `test_exactly_one_direct_xloginsert_in_the_tree` | one line of `src/*.c` calls `XLogInsert(` |
| `test_every_other_wal_emitter_is_a_core_full_page_image_helper` | no `GenericXLogFinish` or `XLogRegisterBuffer` in `src/*.c` |
| `test_pgcolumnartruncatemainfork_was_found` | the function the envelope checks read is there |
| `test_envelope_step_present` | parametrized by the shell's seven names: each step of the envelope is in the function |
| `test_envelope_steps_are_ordered` | parametrized by the shell's six names: delay set, critical section entered, record, flush, truncate, delay cleared, critical section left, in that order |
| `test_the_truncate_record_covers_the_main_fork_only` | the record's flags are `SMGR_TRUNCATE_HEAP`, once |
| `test_no_fork_mask_other_than_the_heap_fork_is_used_here` | no `SMGR_TRUNCATE_VM`, `_FSM` or `_ALL` in the function |

## 194. test_arrow_nested_import.py: arrays and composites round-trip through Arrow

The pytest twin of `test/arrow_nested_import.sh` (ported for #1341), every check by the shell's name. A table with `int[]`, `text[]` and composite columns is exported with `pgcolumnar.export_arrow` and imported with `pgcolumnar.import_arrow` into a fresh table, which must equal the original: the arrays and composites are rebuilt from the Arrow List and Struct buffers, with NULL arrays, empty arrays, NULL elements and NULL composites all in the fixture. After a DELETE on the source, a fresh export and import must carry the live rows alone.

### Every test

A module fixture on a private database takes every reading in the shell's order; each test asserts one. Each comparison hashes both sides on the server as `pgc_set_hash` does, a failed query is a `query_error()`, the source's side of each agreement reads a sentence unless the source holds the rows it should, and the file lives in `server_dir`, since the backend writes and reads it.

| test | what it holds |
| --- | --- |
| `test_the_export_writes_every_row` | the export reports 5000 rows |
| `test_the_import_reads_every_row` | the import reports 5000 rows |
| `test_the_nested_import_round_trips` | the imported table equals the source, every column as text, read only while the source holds its 5000 rows |
| `test_the_import_count_matches` | both row counts are numbers and equal, with the source at 5000 (two failed counts, or two empty tables, no longer pass) |
| `test_the_delete_left_the_live_rows` | premise: the DELETE left 3999 live rows in the source, so the next check has deleted rows to leave out |
| `test_a_fresh_round_trip_after_a_delete` | a fresh export and import after the DELETE equals the live source, read only while it holds its 3999 rows |

## 195. test_arrow_nested.py: arrays and composites export as Arrow List and Struct

The pytest twin of `test/arrow_nested.sh` (ported for #1341), every check by the shell's name. A columnar table with `int[]`, `text[]` and composite columns, with NULL arrays, empty arrays, NULL elements and NULL composites among them, is exported with `pgcolumnar.export_arrow`. pyarrow reads the file back: the fields must be List, List and Struct, and every one of the 5000 rows must equal the value recomputed from the generator. A table with nested columns and no rows still writes a readable schema-only stream, and a multi-dimensional array is refused rather than flattened.

### Every test

A module fixture on a private database runs the SQL in the shell's order; each test reads the file it needs and asserts one reading. The shell skips whole without pyarrow, so every test declines by the shell's skip name first. The files live in `server_dir`, since the backend writes them, and a file that cannot be read is a failed reading.

| test | what it holds |
| --- | --- |
| `test_the_nested_export_writes_every_row` | the export reports 5000 rows |
| `test_the_fields_are_list_list_struct` | `ia` and `ta` are Lists of int32 and string; `c` is a Struct of `x int32, y string` |
| `test_every_value_matches_the_generator` | the file holds every id 1..5000 once, and each row equals the generator's (a file short of rows no longer passes) |
| `test_the_empty_table_writes_no_rows` | the empty table's export reports 0 rows |
| `test_the_empty_table_writes_a_readable_schema` | its file reads back with 0 rows, a List and a Struct |
| `test_a_multidimensional_array_is_refused` | the export of a 2-D array fails with the multi-dimensional message (another error no longer passes) |

## 196. test_vm_clear_on_renumber.py: the visibility map is cleared wherever live rows are renumbered

The pytest twin of `test/vm_clear_on_renumber.sh` (ported for #1341), every check by the shell's name. `compact_rewrite` and `recluster` give live rows new row numbers, so each must clear the all-visible bits over the old numbers; otherwise an index-only scan answers from the index for a TID whose group no longer exists. Both halves read the bits with `pgcolumnar.vm_is_visible`, which makes the same call the index-only-scan executor makes. On a private database.

### Every test

Each check is its own test over a module fixture that runs the shell's steps in order. Every "cleared" reading is the transition `before->after` and wants `t->f`, so a VACUUM that set no bit cannot pass as a clear. The shell's geometry premises, which exit rather than record, refuse the fixture, so every test errors rather than passes.

| test | what it holds |
| --- | --- |
| `test_vacuum_set_all_visible_on_group_3s_late_block` | premise: VACUUM set group 3's late block all-visible |
| `test_and_on_the_untouched_control_groups_block` | premise: and group 5's block |
| `test_the_delete_cleared_the_block_holding_the_deleted_rows` | deleting the front of group 3 takes its first block `t->f` |
| `test_but_left_group_3s_late_block_all_visible` | premise: the delete left the late block set |
| `test_and_left_the_control_block_all_visible` | premise: and the control block |
| `test_compact_rewrite_rewrote_exactly_the_one_qualifying_group` | `compact_rewrite('cw', 0.2)` reports 1 |
| `test_group_3_is_gone_from_the_catalog` | premise: group 3 has no `row_group` row |
| `test_rewrite_one_group_cleared_the_retired_groups_late_block` | the late block goes `t->f` through the rewrite alone |
| `test_a_group_the_rewrite_did_not_touch_keeps_its_all_visible_bit` | control: group 5's block stays `t`, which fails a clear widened to the relation |
| `test_vacuum_set_all_visible_early_in_rc` | premise: an interior early block of `rc` is set |
| `test_and_late_in_rc` | premise: and an interior late block |
| `test_and_on_the_control_relation` | premise: and the control relation's block |
| `test_recluster_reported_reclustering_groups` | `recluster('rc', 'x', 'y')` reports at least one group |
| `test_recluster_online_cleared_the_old_numbers_early_block` | the early block goes `t->f` |
| `test_recluster_online_cleared_the_old_numbers_late_block` | the late block goes `t->f` |
| `test_a_relation_that_was_not_reclustered_keeps_its_all_visible_bits` | control: `rcctl`'s early block stays `t`, which fails a clear that escapes its relation |
| `test_and_its_late_block_too` | control: and its late block |

## 197. test_arrow_export.py: every exported value equals the heap oracle, and only the documented refusals

The pytest twin of `test/arrow_export.sh` (ported for #1341), every check by the shell's name. A mixed-type columnar table copied from a heap table (NULLs, integer boundaries, NaN and infinities, empty and unicode text, bytea) is exported across more than one record batch, and pyarrow must read every value equal to the heap's. An empty table writes a readable schema-only stream. A heap table and an unsupported column type are refused, each by its own message. Date, time, timestamps, uuid, numeric and json export to the representation each maps to.

### Every test

A module fixture on a private database runs the SQL in the shell's order and reads the heap oracle over the driver with the shell's own expressions; each test asserts one reading. Each file reading requires the oracle to hold the fixture's rows, so two empty sides cannot agree, and the file's row count to equal the oracle's, because rows are compared by id and a row written twice would otherwise collapse into one. The shell skips whole without pyarrow, so every test declines by the shell's skip name first.

| test | what it holds |
| --- | --- |
| `test_export_arrow_reports_every_row` | the export reports 20003 rows |
| `test_arrow_values_match_the_heap_oracle` | the heap holds its 20003 rows, the file as many, and each equals the heap's (NaN equal to NaN) |
| `test_the_arrow_schema_has_the_expected_types` | int32, int16, int32, int64, float, double, bool, string, binary |
| `test_an_empty_table_reports_no_rows` | the empty table's export reports 0 rows |
| `test_an_empty_table_writes_a_readable_stream` | its file reads back with 0 rows and both fields |
| `test_a_heap_table_is_refused` | a heap table fails with `is not a columnar table` (another error no longer passes) |
| `test_an_unsupported_column_type_is_refused` | a `point` column fails with its own message |
| `test_the_extended_export_reports_every_row` | the extended table's export reports 4 rows |
| `test_extended_values_match_the_heap_oracle` | the heap holds its 4 rows, the file as many, and each extended value equals its canonical form; non-finite dates and timestamps and NaN numerics are null |
| `test_the_extended_schema_has_the_expected_types` | date32, time64, timestamp with and without UTC, fixed_size_binary(16), decimal128(20, 4), and strings |

## 198. test_vector_agg_tlist_shape.py: the vectorized aggregate accepts a target list that contains aggregates

The pytest twin of `test/vector_agg_tlist_shape.sh` (ported for #1341), every check by the shell's name. Since #755 the ungrouped vectorized path accepts `count(*)::text`, `avg(a)+avg(b)`, `round(avg(a), 2)` and other expressions over aggregates: the node emits bare aggregates and a projection above it computes the expressions. The risk is a wrong answer, so every shape is asserted twice, once for the plan and once for the answer against a heap table holding the same 20,000 rows. On a private database.

### Every test

A plan reads `vectorized`, `not vectorized`, or the failed EXPLAIN as a `query_error()`, so an EXPLAIN that errors cannot read as a decline. An answer is the repr of the row list, which is order-sensitive. The grouped vectorized path is pinned off for the plan readings, because it prints the same marker.

| test | what it holds |
| --- | --- |
| `test_the_ordered_oracle_is_order_sensitive` | the three oracle checks: an ordered digest tells two orders apart and agrees with itself, and the set digest does not |
| `test_both_tables_hold_the_same_rows` | premise: the columnar and heap tables hold the same count |
| `test_the_sort_column_has_nulls` | premise: column `a` has NULLs for the aggregates to skip |
| `test_the_shape_takes_the_fast_path` | parametrized by the shell's 17 `...: vectorized` names: each shape plans the vectorized node |
| `test_the_answer_matches_heap` | parametrized by the shell's 25 answer names: each shape, and each refused shape, answers as the heap does |
| `test_the_scan_fold_guc_is_off_by_default` | premise: a filtered aggregate declines with the scan-fold GUC at its default |
| `test_a_bare_aggregate_takes_the_scan_fold_path_with_the_guc_on` | premise: and takes it once the GUC is on |
| `test_a_wrapped_aggregate_takes_the_scan_fold_path_too` | a wrapped filtered aggregate takes it too |
| `test_the_shape_still_declines` | parametrized by the shell's 7 `REFUSE:` names: a bare column, an unsupported aggregate, FILTER, DISTINCT, ORDER BY inside an aggregate, and an aggregate over an expression decline |
| `test_empty_then_all_null_relations_answer_as_heap_does` | an empty pair, then the same pair filled with 500 all-NULL rows, answer the wrapped shape as the heap does, with a premise for each state |
| `test_a_bare_aggregate_on_the_scan_fold_path_goes_parallel` | premise: the plan has a Gather, a planned worker and the vectorized node |
| `test_parallel_accepts_a_wrapped_target_list` | parametrized by the shell's three names: wrapped target lists go parallel too |
| `test_the_parallel_answer_matches_heap` | parametrized by the shell's three names: and their parallel answers equal the heap's |

## 199. test_alter_column_type.py: ALTER COLUMN TYPE converts every value, and the column really changes type

The pytest twin of `test/alter_column_type.sh` (ported for #1341), every check by the shell's name. Phase 2 of ATRewriteTable updates `pg_attribute` before phase 3 scans the old relation, so the scan must decode against the slot core builds from `tab->oldDesc`, not the relation's new descriptor (#178). Each conversion runs on a columnar table and on a heap table of the same shape, and the heap is the oracle.

### Every test

A module fixture on a private database runs every conversion in the shell's order, each statement on a connection of its own; each test asserts one reading. A conversion's reading is `mismatches/rows/type`, so an ALTER that failed and rolled back no longer passes; a corrupt chunk or a lost backend reads `failed: …`. The conversion that crashed the backend runs last, as in the shell.

| test | what it holds |
| --- | --- |
| `test_int_to_bigint` | int to bigint: every row matches heap converted the same way, all 20000 rows survive, and the column has the new type |
| `test_bigint_to_int` | bigint to int: every row matches heap converted the same way, all 20000 rows survive, and the column has the new type |
| `test_smallint_to_int` | smallint to int: every row matches heap converted the same way, all 20000 rows survive, and the column has the new type |
| `test_int_to_smallint` | int to smallint: every row matches heap converted the same way, all 20000 rows survive, and the column has the new type |
| `test_float4_to_float8` | float4 to float8: every row matches heap converted the same way, all 20000 rows survive, and the column has the new type |
| `test_float8_to_float4` | float8 to float4: every row matches heap converted the same way, all 20000 rows survive, and the column has the new type |
| `test_date_to_timestamp` | date to timestamp: every row matches heap converted the same way, all 20000 rows survive, and the column has the new type |
| `test_int_to_numeric` | int to numeric: every row matches heap converted the same way, all 20000 rows survive, and the column has the new type |
| `test_int_to_text` | int to text: every row matches heap converted the same way, all 20000 rows survive, and the column has the new type |
| `test_bool_to_int` | bool to int: every row matches heap converted the same way, all 20000 rows survive, and the column has the new type |
| `test_numeric_to_text` | numeric to text: every row matches heap converted the same way, all 20000 rows survive, and the column has the new type |
| `test_text_to_varchar_64` | text to varchar(64): every row matches heap converted the same way, all 20000 rows survive, and the column has the new type |
| `test_text_to_bytea` | text to bytea: every row matches heap converted the same way, all 20000 rows survive, and the column has the new type |
| `test_text_rewritten_by_using` | text rewritten by `USING`: every row matches heap converted the same way, all 20000 rows survive, and the column has the new type |
| `test_the_column_really_is_bigint_now` | the indexed table's column is `bigint` after its ALTER |
| `test_an_index_scan_still_finds_a_row_after_the_rewrite` | a scan through `at_ix_i` finds row 10000 with its converted value (a plan with no index scan no longer passes) |
| `test_an_index_scan_finds_every_row_after_the_rewrite` | a scan through `at_ix_i` over the whole id range finds all 20000 rows (an index missing rows, or no index at all, no longer passes) |
| `test_the_aggregate_over_the_converted_column_is_right` | `sum(v)` equals the generator's, both numbers |
| `test_every_row_survived_the_rewrite` | the indexed table still holds 20000 rows |
| `test_the_nullable_column_really_is_bigint_now` | the nullable column is `bigint` after its ALTER |
| `test_nulls_stay_on_the_same_rows_through_a_conversion` | no row differs from heap, and the columnar table still holds all 20000 rows |
| `test_text_to_int_the_crash` | the varlena-to-fixed conversion that dereferenced an integer as a pointer, run last |

## 200. test_vector_agg_rescan_memory.py: rescanning a columnar aggregate does not grow query memory

The pytest twin of `test/vector_agg_rescan_memory.sh` (ported for #1341), every check by the shell's name. A LATERAL sub-scan re-executes its node once per outer row, and whatever the node leaves in query memory stays there until ExecutorEnd. Three such growths were fixed: an IN-list's scan keys rebuilt per rescan (#717), the vectorized aggregate's own per-rescan arrays (#727), and the plain scan's row-group list (#734). Each claim compares a growth per rescan, a slope across two rescan counts, with a control: the same query without the IN-list, or over a heap table. On a private database.

### Every test

For #717 the twin samples `pg_log_backend_memory_contexts()` from a second connection while the query runs, and takes the largest `ExecutorState` total from the server log. For #727 and #734 it reads the backend's peak RSS (`VmHWM`) before and after each run. A claim reads `no reading` unless both of its slopes were measured, so readers that measured nothing cannot pass as "no growth".

| test | what it holds |
| --- | --- |
| `test_the_fixture_loaded` | premise: `vam` holds 50,000 rows |
| `test_the_in_list_becomes_pushdown_scan_keys` | premise: EXPLAIN ANALYZE shows pushed-down filters for the IN-list |
| `test_each_arm_produced_a_reading` | parametrized by the shell's four names: each #717 arm logged an ExecutorState peak |
| `test_an_in_list_does_not_add_per_rescan_query_memory` | the IN-list adds less than 20,000 bytes per rescan over the control (#717) |
| `test_the_driver_has_enough_rows` | premise: the driver holds 40,000 rows |
| `test_the_two_rescan_counts_differ` | premise: the two driver limits really give 40,000 and 5,000 rescans |
| `test_the_heap_mirror_has_the_same_rows` | premise: `vam_heap` and `vam_rs` hold the same count |
| `test_the_columnar_rescan_arm_is_the_vectorized_aggregate` | premise: the #727 arm plans the vectorized aggregate |
| `test_both_rescan_slopes_were_measured` | premise: both #727 slopes were read |
| `test_the_vectorized_aggregate_does_not_grow_query_memory_per_rescan` | the vectorized aggregate grows less than 100 bytes per rescan above the heap floor (#727) |
| `test_the_rescanned_aggregate_still_returns_the_heaps_answer` | and answers as the heap does |
| `test_with_the_aggregate_off_the_arm_is_the_plain_columnar_scan` | premise: with the aggregate off, the arm is the plain columnar scan |
| `test_and_it_is_not_the_vectorized_aggregate_node` | premise: and not the vectorized aggregate |
| `test_both_plain_scan_slopes_were_measured` | premise: both #734 slopes were read |
| `test_the_plain_columnar_scan_does_not_grow_query_memory_per_rescan` | the plain scan grows less than 100 bytes per rescan above the heap floor (#734) |
| `test_the_rescanned_plain_scan_still_returns_the_heaps_answer` | and answers as the heap does |
| `test_the_pruning_arm_is_on_the_row_path` | premise: the pruning query is on the row path, not the fold |
| `test_and_it_is_the_vectorized_aggregate_node` | premise: and is the vectorized aggregate |
| `test_the_row_paths_scan_keys_still_prune` | the row path's scan keys remove at least one chunk group, as work done |
| `test_the_pruned_query_still_answers_correctly` | and the answer is 10,000 |

## 201. test_analyze_stats.py: ANALYZE collects statistics that agree with heap, and the planner prices what they feed

The pytest twin of `test/analyze_stats.sh` (ported for #1341), every check by the shell's name. The sampler maps each block core chooses to the slice of its row group that the block stands for, and offers that slice's live rows (#154). The statistics are compared with a heap table on identical data; reltuples discriminates against cluster sampling; correlation needs the sampled tuples' item pointers. The fetch-penalty (#355, #362, #376), decode-prefix (#363) and all-visible (#507) checks read plans and costs built from those statistics, and the ANALYZE time cap is checked as a function and on a wide table.

### Every test

A module fixture on a private database builds every table in the shell's order, each statement on a connection of its own; each test asserts one reading. A missing reading never agrees with another: survival also reads the server log's count of killed backends, a statistic ANALYZE did not record is a failed query, and each negative plan check first requires a plan of the table it names.

| test | what it holds |
| --- | --- |
| `test_the_server_survived_analyze` | the server survived ANALYZE -- and the server log's count of killed backends is unchanged, an unreadable log failing it (a backend SIGKILLed mid-ANALYZE no longer passes) |
| `test_analyze_now_collects_statistics_at_all` | ANALYZE now collects statistics at all |
| `test_null_frac_matches_heap` | null_frac matches heap within 0.02 -- both are numbers (two missing statistics no longer agree) |
| `test_n_distinct_on_status_matches_heap` | n_distinct on status matches heap -- a missing statistic is a failed query, so two of them no longer agree |
| `test_n_distinct_on_v_matches_heap` | n_distinct on v matches heap -- a missing statistic is a failed query, so two of them no longer agree |
| `test_n_distinct_on_nullable_matches_heap` | n_distinct on nullable matches heap -- a missing statistic is a failed query, so two of them no longer agree |
| `test_most_common_vals_holds_the_same_set_as_heap` | most_common_vals holds the same set as heap |
| `test_correlation_on_an_ascending_column_is_near_1` | correlation on an ascending column is near 1 |
| `test_the_sampled_tuples_carry_a_valid_item_pointer` | the sampled tuples carry a valid item pointer |
| `test_no_row_is_offered_to_the_sampler_twice` | no row is offered to the sampler twice |
| `test_reltuples_is_within_2_percent_of_the_true_row_count` | reltuples is within 2% of the true row count |
| `test_n_distinct_on_a_clustered_column_is_within_10_percent` | n_distinct on a clustered column is within 10% of the truth |
| `test_the_join_plans_as_a_join_rather_than_degenerating` | the join plans as a join rather than degenerating |
| `test_a_selective_equality_is_estimated_from_the_data` | a selective equality is estimated from the data, not the 0.5% default |
| `test_a_point_lookup_still_uses_the_index_after_analyze` | a point lookup on an indexed column still uses the index after ANALYZE |
| `test_having_an_index_available_saves_the_point_lookup_real_work` | having an index available saves the point lookup real work -- both counts were read from a plan (a failed EXPLAIN ANALYZE, which read as 0 rows discarded with the index, no longer passes; found by jdatcmd) |
| `test_the_cap_is_exposed_as_a_function` | premise: the cap is exposed as a function, not written inline |
| `test_a_fast_machine_still_gets_the_floor` | a fast machine still gets the floor, not a bound that shrinks with it |
| `test_a_slow_machine_gets_a_budget_proportional_to_its_insert` | and a slow one gets a budget proportional to its own insert |
| `test_the_floor_and_the_derived_bound_meet` | the floor and the derived bound meet where they should |
| `test_one_millisecond_past_the_floor_the_derived_bound_takes_over` | and one millisecond past it the derived bound takes over |
| `test_a_zero_insert_time_still_yields_the_floor` | a missing or zero insert time still yields the floor, not zero |
| `test_a_malformed_insert_time_yields_the_floor` | a malformed insert time yields the floor rather than an arithmetic error |
| `test_an_insert_time_past_int64_yields_the_floor` | and an insert time past int64 yields the floor rather than a cap that cannot fail |
| `test_analyze_on_a_wide_table_completes_inside_its_cap` | ANALYZE on a wide table completes well inside its derived cap -- and ANALYZE succeeded (a failing one, fast, no longer passes) |
| `test_without_the_penalty_an_unclustered_order_by_takes_the_index` | without the fetch penalty an unclustered ORDER BY takes the index (#355 premise) |
| `test_the_penalty_makes_an_unclustered_order_by_sort` | the fetch penalty makes an unclustered ORDER BY sort rather than fetch per row (#355) |
| `test_the_penalty_leaves_a_clustered_order_by_on_its_index` | the fetch penalty leaves a clustered ORDER BY on its index (#355 must not over-fire) |
| `test_the_penalty_leaves_a_selective_point_lookup_on_the_index` | the fetch penalty leaves a selective point lookup on the index (#355 vs #171) |
| `test_without_the_penalty_a_scattered_condition_takes_the_index` | without the penalty a selective scattered condition takes the index (#362 premise) |
| `test_the_penalty_is_applied_before_the_columnar_path_is_offered` | the penalty is applied before the columnar path is offered, so it can still win (#362) |
| `test_an_early_columns_short_decode_prefix_stays_on_the_index` | an early column's short decode prefix leaves it on the index (#363) |
| `test_a_late_columns_wide_decode_prefix_costs_it_off_the_index` | a late column's wide decode prefix costs it off the index (#363) -- in a plan that scans o363 (a failed EXPLAIN no longer passes) |
| `test_a_small_limit_still_reaches_the_index` | a small LIMIT still reaches the index through the fetch penalty (#376) |
| `test_the_bound_does_not_disable_the_penalty_for_a_full_read` | the bound does not disable the penalty for a full ordered read (#376) |
| `test_vacuum_recorded_all_visible_pages` | premise: VACUUM recorded all-visible pages to price with (#507) |
| `test_the_selective_range_has_its_rows` | premise: the selective range has its rows (#507) |
| `test_a_vacuumed_columnar_table_can_reach_an_index_only_scan` | a vacuumed columnar table can reach an index-only scan (#507) |
| `test_the_all_visible_fraction_does_not_hand_every_query_to_the_index` | the all-visible fraction does not hand every query to the index (#507) |
| `test_both_all_visible_estimates_were_taken` | premise: both estimates were taken (#507) |
| `test_the_all_visible_pages_buy_the_index_only_scan_a_saving` | the all-visible pages buy the index-only scan a saving (#507) -- the index scan's cost minus the index-only scan's is positive after VACUUM (a fraction hardcoded to zero, which still gets the index-only plan here, no longer passes) |
| `test_growth_without_vacuum_does_not_inflate_the_saving` | growth without vacuum does not inflate the all-visible saving (#507) -- both estimates were taken (a missing grown estimate no longer passes) |

## 202. test_vacuum_stripe_count.py: pgcolumnar.vacuum refuses a stripe_count bound rather than ignoring it

The pytest twin of `test/vacuum_stripe_count.sh` (ported for #1341), every check by the shell's name. `stripe_count` was documented as bounding how many row groups one vacuum combines, but the code never read it. It cannot simply be honoured, because the compaction swaps the relation to a new relfilenode, so a non-default value is refused, with a hint naming `pgcolumnar.compact_rewrite`, which does bound its work. On a private database.

### Every test

Each check is its own test over a module fixture that runs the shell's steps in order. A call reads `noerror`, `connfail` or `SQLSTATE <code>: <message>`, so an unreachable server cannot read as success. A refusal reads `refused` only for pgcolumnar's own message, so any other error reads as itself. The bounded rewrite is read as work: how many groups it rewrote, and how many still hold deleted rows.

| test | what it holds |
| --- | --- |
| `test_err_reports_a_clean_call` | premise: the reader says `noerror` for `SELECT 1` |
| `test_err_reports_a_real_error` | premise: and `SQLSTATE 22012: division by zero` for `SELECT 1/0` |
| `test_err_cannot_mistake_an_unreachable_server_for_success` | premise: and `connfail` for port 1 |
| `test_the_fixture_constants_are_non_zero` | premise: six groups of 1,000 rows |
| `test_the_whole_fixture_fits_in_one_stripe` | premise: so combining them is possible |
| `test_the_fixture_has_several_row_groups` | premise: the table has six row groups |
| `test_the_fixture_has_its_rows` | premise: and 6,000 rows |
| `test_the_one_argument_form_still_vacuums` | `vacuum('vsc')` succeeds |
| `test_it_combined_the_groups` | and leaves one group |
| `test_it_preserved_every_row` | and 6,000 rows |
| `test_the_explicit_default_zero_is_still_accepted` | `vacuum('vsc', 0)` succeeds |
| `test_zero_also_did_the_work` | and leaves one group |
| `test_a_non_default_stripe_count_is_refused` | `vacuum('vsc', 4)` fails with pgcolumnar's own refusal |
| `test_the_refusal_names_compact_rewrite` | one diagnostic field names `compact_rewrite` |
| `test_the_name_is_offered_as_a_hint` | and it is the HINT |
| `test_the_refused_call_did_no_work` | the six groups are untouched |
| `test_no_rows_were_lost_to_it` | and the 6,000 rows |
| `test_vacuum_full_inherits_the_refusal` | `vacuum_full('public', 0.0, 4)` fails with the same refusal |
| `test_the_refused_vacuum_full_did_no_work` | the six groups are untouched |
| `test_vacuum_fulls_default_form_still_works` | `vacuum_full('public', 0.0)` succeeds |
| `test_vacuum_full_combined_the_groups` | and leaves one group |
| `test_compact_rewrites_bounded_form_is_accepted` | premise: `compact_rewrite('vsc', 0.1, 2)` succeeds |
| `test_the_named_alternative_rewrote_exactly_its_bound` | it rewrote 2 of 6 groups, and 4 still hold deleted rows |
## 203. test_bloom_sizing.py: a bloom filter is sized by its distinct count, and still skips

The pytest twin of `test/bloom_sizing.sh` (ported for #1341), every check by the shell's name. Two columns of one row group differ only in cardinality, so a difference in their stored filter sizes can only be cardinality (#467). A smaller filter must still skip: a scattered fixture where zone maps exclude nothing must report groups removed by the probe, and the answer must not change with the filter off. Above the distinct cap a column is refused a filter, while a five-value column of the same row group keeps its own.

### Every test

A module fixture on a private database builds the three tables in the shell's order; each test asserts one reading. A reading is a value or a failed query, never a default: a missing filter row is not a size.

| test | what it holds |
| --- | --- |
| `test_every_row_of_the_sizing_table_loaded` | premise: every row loaded |
| `test_the_sizing_table_is_one_row_group` | premise: the whole table is one row group |
| `test_the_low_cardinality_column_really_is_low` | premise: the low-cardinality column really is low |
| `test_the_high_cardinality_column_really_is_high` | premise: the high-cardinality column really is high |
| `test_the_low_cardinality_filter_size_is_a_measurement` | premise: the low-cardinality filter size is a measurement |
| `test_the_high_cardinality_filter_size_is_a_measurement` | premise: the high-cardinality filter size is a measurement |
| `test_a_five_value_column_does_not_get_a_unique_columns_filter` | a five-value column does not get a unique column's filter (#467) |
| `test_a_value_that_is_absent_is_still_reported_absent` | a value that is absent is still reported absent |
| `test_a_value_that_is_present_is_still_found` | and a value that is present is still found |
| `test_results_are_identical_with_the_bloom_filter_and_without_it` | results are identical with the filter and without it |
| `test_the_skip_fixture_has_many_row_groups` | premise: the skip fixture has many row groups to skip |
| `test_the_skip_counter_is_reported_with_the_probe_on` | premise: the skip counter is reported with the probe on |
| `test_the_skip_counter_is_reported_with_the_probe_off` | premise: the skip counter is reported with the probe off |
| `test_zone_maps_alone_skip_nothing_on_the_bloom_fixture` | premise: zone maps alone skip nothing on this fixture |
| `test_the_bloom_filter_still_skips_groups_it_can_prove_empty` | the filter still skips groups it can prove empty (#467) |
| `test_one_row_group_holds_more_than_the_refusal_threshold` | premise: one row group holds more than the refusal threshold |
| `test_the_values_above_the_threshold_really_are_distinct` | premise: those values really are distinct |
| `test_a_column_above_the_distinct_cap_is_refused_a_filter` | a column above the distinct cap is refused a filter, not given a bad one -- read as refused/kept against a five-value column in the same row group (a build that writes no filter no longer passes) |
| `test_the_large_load_itself_succeeded` | and the load itself succeeded, rather than erroring on the way |

## 204. test_vacuum_sorted_gate.py: pgcolumnar.vacuum_sorted skips its rewrite only when there is nothing to do

The pytest twin of `test/vacuum_sorted_gate.sh` (ported for #1341), every check by the shell's name. vacuum_sorted both orders the live rows and reclaims deleted rows' space, so its gate (#760) may skip only when the relation is already one lexicographic run over exactly this key, with nothing appended and nothing deleted. The check this suite exists for is that deleted rows defeat the gate. On a private database.

### Every test

Each check is its own test over a module fixture that runs the shell's steps in order. A rewrite is read from the storage id, which moves on a rewrite and not on a skip; the no-op checks also require the row-group layout to be unchanged. A call that fails, or a reading that is missing, reads as itself, never as "no-op". The heap mirror for the last check is built from the generator, not copied from the table under test.

| test | what it holds |
| --- | --- |
| `test_the_first_vacuum_sorted_does_real_work` | premise: the first `vacuum_sorted('vg_t','k','j')` rewrites |
| `test_it_recorded_a_lexicographic_run` | premise: the mark is `lexicographic` |
| `test_over_exactly_the_requested_key` | premise: over `{k,j}` |
| `test_the_run_covers_the_whole_relation` | premise: 0 appended groups |
| `test_nothing_is_deleted_yet` | premise: 0 deleted rows |
| `test_a_second_call_on_the_same_key_does_not_rewrite` | the second call succeeds and moves neither storage nor layout |
| `test_a_third_identical_call_is_also_a_no_op` | and so does the third |
| `test_the_relation_is_still_in_order_after_the_skips` | the rows are still in (k,j) order |
| `test_the_delete_left_dead_rows_inside_the_run` | premise: deleting the even ids leaves dead rows and appends nothing |
| `test_an_ordering_only_gate_would_see_nothing_to_do` | premise: the mark still reads `lexicographic/{k,j}` |
| `test_dead_rows_force_a_rewrite` | with dead rows, the call rewrites |
| `test_it_reclaimed_the_space` | and 0 deleted rows remain |
| `test_without_losing_a_live_row` | and the live row count is unchanged |
| `test_the_call_after_the_reclaim_is_a_no_op_again` | the next call is a no-op again |
| `test_the_insert_appended_groups_outside_the_run` | premise: inserting 5,000 rows appends groups |
| `test_an_append_forces_a_rewrite` | after an append, the call rewrites |
| `test_the_tail_is_folded_back_into_the_run` | and 0 appended groups remain |
| `test_a_different_key_rewrites` | `vacuum_sorted` by (j,k) rewrites |
| `test_the_new_key_is_recorded` | and records `{j,k}` |
| `test_a_prefix_of_the_key_rewrites` | `vacuum_sorted` by (k) alone rewrites |
| `test_cluster_recorded_a_zorder_run` | premise: `cluster('vg_z','k','j')` records `zorder/{k,j}` |
| `test_the_zordered_table_is_not_in_k_order` | premise: and the rows are not in k order |
| `test_a_zorder_run_forces_a_rewrite` | `vacuum_sorted` over that run rewrites |
| `test_the_result_is_in_kj_order` | and the rows come out in (k,j) order |
| `test_the_kind_is_now_lexicographic` | and the mark is `lexicographic` |
| `test_a_plain_vacuum_leaves_no_mark` | premise: a plain `vacuum()` clears the mark |
| `test_vacuum_sorted_after_a_plain_vacuum_rewrites` | so the next `vacuum_sorted` rewrites |
| `test_the_tail_is_smaller_than_the_chunk_group_limit` | premise: the 500-row tail is below the 1,000-row group limit |
| `test_an_insert_in_the_same_transaction_defeats_the_gate` | an insert then `vacuum_sorted` in one transaction leaves the rows in order |
| `test_every_row_is_still_there` | and 20,500 rows remain |
| `test_nothing_is_deleted_before_the_transaction` | premise: 0 deleted rows |
| `test_a_delete_in_the_same_transaction_defeats_the_gate` | a delete then `vacuum_sorted` in one transaction leaves 0 deleted rows |
| `test_the_rewrite_really_happened` | and 10,000 rows are stored |
| `test_the_gated_calls_preserved_the_exact_row_set` | `vg_t` holds exactly the rows the generator says it should |

## 205. test_ungrouped_vector_agg.py: the ungrouped vectorized aggregate returns what core Agg returns

The pytest twin of `test/ungrouped_vector_agg.sh` (ported for #1341), every check by the shell's name. `pgcolumnar.enable_ungrouped_vector_agg` routes an ungrouped aggregate that a zone map cannot answer to a single-pass scan-fold node (#289). On a private database, with serial execution pinned in both arms.

### Every test

A module fixture runs the shell's steps in order. An on/off pair reads `same (VALUE)` only when both sides returned a value; an error or empty side reads `no reading`. Every fixture is held to its row count first, since over an empty table every aggregate agrees with itself. A plan counts as "no vectorized node" only when it has an Aggregate at all.

| test | what it holds |
| --- | --- |
| `test_the_fixture_holds_its_rows` | premise: `t` has 60,000 rows |
| `test_the_node_is_used_with_the_setting_on` | premise: the plan has the vectorized node with the setting on |
| `test_core_agg_runs_with_the_setting_off` | premise: with it off, the plan is a core Agg |
| `test_the_filter_is_pushed_into_the_scan` | premise: EXPLAIN shows the pushed-down filter |
| `test_an_eligible_shape_uses_the_batch_fold` | premise: `Batch Fold: yes` for float sum/avg/count |
| `test_min_max_falls_back_off_the_batch_fold` | premise: `Batch Fold: no` for min/max |
| `test_the_fold_matches_core_agg` | 19 on/off pairs over `t`: filtered and unfiltered sum/avg over int, int8, float and numeric, count, min/max, all rows filtered out, a NULL-heavy column, and the metadata path |
| `test_the_all_null_fixture_holds_its_rows` | premise: `t_allnull` has 5,000 rows |
| `test_an_all_null_column_matches_core_agg` | sum and avg over an all-NULL column read `same (NULL)` |
| `test_the_fold_matches_core_agg_after_deletes` | avg and count still match after three deletes |
| `test_the_fold_prunes_groups_on_a_clustered_filter` | EXPLAIN ANALYZE shows groups removed by the fold (#349) |
| `test_the_fold_prunes_the_same_groups_as_the_row_path` | the same number as the row path |
| `test_the_clustered_fold_matches_core_agg` | and the same answer |
| `test_the_server_is_still_up` | `SELECT 1` answers |
| `test_uuid_is_fixed_width_but_not_by_value` | premise: uuid is `16/false` |
| `test_the_varlena_fixture_holds_its_rows` | premise: `vfold` and its heap mirror have 200,000 rows each |
| `test_the_varlena_fixture_is_columnar` | premise: `vfold` uses pgcolumnar |
| `test_a_shape_agrees_with_heap` | 10 shapes over text, uuid and name columns, and the `<>` clause (#715), each equal to the heap mirror |
| `test_the_batch_fold_decision` | a fixed-width filter folds; text, uuid, name and `<>` fall back, and EXPLAIN says so (#423, #715) |
| `test_the_numeric_fixture_holds_its_rows` | premise: `vnum` and `vnumc` have 20,000 rows each |
| `test_the_vectorized_node_count` | premise: float8 takes the node; sum(numeric), avg(numeric) and a mixed query do not (#785) |
| `test_a_refused_numeric_aggregate_matches_heap` | the refused queries still equal the heap mirror |

## 206. test_tablesample.py: TABLESAMPLE on a columnar table raises 0A000

The pytest twin of `test/tablesample.sh` (ported for #1341), every check by the shell's name. The planner hooks used to replace Sample Scan with the custom scan or a vectorized aggregate, which answered every row; the contract is the error. On a private database, with serial planning pinned as the shell's cluster pins it.

### Every test

A statement reads as the SQLSTATE it raised, `00000` when it ran, or `no reading` when the server could not be reached.

| test | what it holds |
| --- | --- |
| `test_the_table_holds_its_rows` | premise: `n` has 10,000 rows |
| `test_a_sampled_shape_raises_0a000` | count, sum, SYSTEM, a projection and a grouped query over `TABLESAMPLE` each raise 0A000 |
| `test_a_control_raises_nothing` | an unsampled count, and the same sample on a heap table, raise nothing |
## 207. test_bloom_setting.py: enable_bloom_filter governs the write side too

The pytest twin of `test/bloom_setting.sh` (ported for #1341), every check by the shell's name. The setting was consulted only by the reader, so turning it off still hashed every value and stored a filter for every chunk. With it off nothing is written, a chunk written with no filter still answers equality in both directions, and a projection, written through its own write state, gets its own filters.

### Every test

A module fixture on a private database builds the tables in the shell's order; each test asserts one reading. A reading carries what it was read over, so a load that failed cannot pass as "no filters" or "no mismatch".

| test | what it holds |
| --- | --- |
| `test_filters_are_written_when_the_setting_is_on` | filters are written when the setting is on |
| `test_no_filters_are_written_when_the_setting_is_off` | no filters are written when the setting is off -- over the 20000 rows the table holds (a load that failed no longer passes) |
| `test_written_off_read_on_finds_a_present_value` | written off, read with the setting on: present value found |
| `test_written_off_read_on_does_not_find_an_absent_value` | written off, read with the setting on: absent value not found |
| `test_written_off_read_off_finds_a_present_value` | written off, read with the setting off: present value found |
| `test_written_off_read_off_does_not_find_an_absent_value` | written off, read with the setting off: absent value not found |
| `test_written_on_read_off_finds_a_present_value` | written on, read with the setting off: present value found |
| `test_a_full_scan_of_the_unfiltered_table_is_complete` | a full scan of the unfiltered table is complete |
| `test_the_filtered_and_unfiltered_tables_hold_identical_data` | the two tables hold identical data -- read as mismatches of rows compared (two empty tables no longer pass) |
| `test_a_projection_still_gets_filters_when_the_setting_is_on` | a projection still gets filters when the setting is on -- counted on the projection's own storage (a projection written without filters no longer passes) |
| `test_the_projected_table_reads_correctly` | the projected table reads correctly |

## 208. test_sorted_mark_rename.py: a table's ordering mark follows a column rename

The pytest twin of `test/sorted_mark_rename.sh` (ported for #1341), every check by the shell's name. pgcolumnar.storage records the key a table is ordered on by column name, and vacuum_sorted's gate (#760) and the planner both trust it. Swapping two columns' names must move the mark, or the gate skips work it must do and ORDER BY returns rows out of order (#778, #779). On a private database, with serial planning as the shell's cluster runs.

### Every test

A module fixture runs the shell's steps in order. A vacuum_sorted call is read by the storage id: `noop`, `rewrote`, `call failed`, or `no reading`. A plan with no scan in it is `no plan`. Every fixture is held to its row count.

| test | what it holds |
| --- | --- |
| `test_vr_ctl_loaded_every_row` | premise: vr_ctl loaded every row (an overflowed INSERT would make an empty table look like a fired gate) |
| `test_before_sorting_a_is_not_in_order` | premise: before sorting, a is not in order |
| `test_vacuum_sorted_really_ordered_it` | premise: vacuum_sorted really ordered it by a |
| `test_a_second_identical_call_is_a_no_op` | CONTROL: a second identical call is a no-op, so the gate does fire |
| `test_vr_t_loaded_every_row` | premise: vr_t loaded every row (an overflowed INSERT would make an empty table look like a fired gate) |
| `test_vr_t_is_ordered_by_a` | premise: vr_t is ordered by a |
| `test_vr_t_is_not_ordered_by_b` | premise: and NOT by b |
| `test_the_mark_records_a` | premise: the mark records key {a} |
| `test_after_the_swap_b_is_ordered` | premise: after the swap the ordered column is the one now named b |
| `test_after_the_swap_a_is_not_ordered` | premise: and the column now named a is NOT ordered |
| `test_the_mark_follows_the_rename` | the mark FOLLOWS the rename, so it still names the ordered column |
| `test_vacuum_sorted_after_the_swap_does_the_work` | THE POINT: vacuum_sorted('a') after the swap does the work |
| `test_the_table_is_ordered_by_a_afterwards` | and the table really is ordered by a afterwards |
| `test_the_gate_still_fires_for_the_right_key` | the gate still fires for the right key after all that |
| `test_vr_o_loaded_every_row` | premise: vr_o loaded every row (an overflowed INSERT would make an empty table look like a fired gate) |
| `test_an_unrelated_rename_leaves_the_mark` | renaming an unrelated column does not disturb the mark |
| `test_an_unrelated_rename_does_not_rewrite_the_storage_row` | and does not rewrite the storage row at all (xmin unchanged) |
| `test_xmin_can_move` | premise: xmin CAN move, so the arm above is not comparing two constants |
| `test_the_gate_still_fires_after_an_unrelated_rename` | and the gate still fires |
| `test_the_partition_holds_every_row` | premise: the partition holds every row |
| `test_the_partition_is_ordered_and_marked` | premise: the partition is ordered by k and its mark says so |
| `test_the_parent_is_not_columnar` | premise: the parent is NOT itself columnar, so gating on it would skip the child |
| `test_after_the_swap_the_partitions_ordered_column_is_j` | premise: after the swap the partition's ordered column is named j |
| `test_after_the_swap_the_partitions_k_is_not_ordered` | premise: and the column now named k is NOT ordered |
| `test_the_partitions_mark_follows_a_rename_on_the_parent` | the PARTITION's mark follows a rename issued on the parent |
| `test_vacuum_sorted_on_the_partition_does_the_work` | so vacuum_sorted on the partition does the work |
| `test_the_partition_is_ordered_by_k_afterwards` | and the partition really is ordered by k afterwards |
| `test_the_inheritance_child_is_ordered` | premise: the inheritance child is ordered by k |
| `test_the_childs_mark_follows_a_rename_on_the_parent` | the CHILD's mark follows a rename issued on the parent |
| `test_vacuum_sorted_on_the_child_does_the_work` | so vacuum_sorted on the child does the work |
| `test_with_the_mark_true_order_by_needs_no_sort` | premise: with the mark true, ORDER BY k needs no Sort |
| `test_after_the_swap_order_by_plans_a_sort` | after the swap, ORDER BY on the now-unordered column plans a Sort |
| `test_the_rows_come_back_ordered` | THE WRONG-ANSWER ARM: and the rows really do come back ordered |
| `test_order_by_on_the_ordered_column_needs_no_sort` | and ORDER BY on the column that IS ordered still needs no Sort |
| `test_sb_loaded_every_row` | premise: sb loaded every row (an overflowed INSERT would make an empty table look like a fired gate) |
| `test_a_bare_vacuum_sorted_used_the_declared_key` | premise: a bare vacuum_sorted() used the declared key |
| `test_the_declared_key_follows_the_rename` | the DECLARED key follows the rename too |
| `test_the_two_catalogs_still_agree` | so the two catalogs still agree after the rename |
| `test_a_bare_vacuum_sorted_is_a_no_op` | and a bare vacuum_sorted() is a no-op, not a rewrite on a different key |
| `test_rr_ctl_loaded_every_row` | premise: rr_ctl loaded every row |
| `test_the_first_recluster_does_real_work` | premise: the first recluster does real work |
| `test_a_repeat_recluster_is_a_no_op` | CONTROL: a repeat recluster on the same key is a no-op, so the gate fires |
| `test_rr_t_loaded_every_row` | premise: rr_t loaded every row |
| `test_the_mark_records_a_zorder_run` | premise: the mark records {a,b} as zorder |
| `test_the_mark_follows_both_renames` | the mark follows both renames |
| `test_recluster_on_the_new_key_is_not_skipped` | recluster on the NEW (a,b) is not skipped as already done |

## 209. test_sort_status_privilege.py: sort_status answers whoever may SELECT the table, and only them

The pytest twin of `test/sort_status_privilege.sh` (ported for #1341), every check by the shell's name. sort_status reads catalogs that carry no GRANT, so it runs with definer rights and gates the real caller itself (#608). Run as three non-superuser roles created for the module: the owner, a role granted SELECT, and a role with nothing. On a private database.

### Every test

A call made as a role reads the first column of its first row, or its failure as `SQLSTATE <code>: <message>`, so a refusal is matched on its message as in the shell.

| test | what it holds |
| --- | --- |
| `test_the_owner_can_open_a_session` | premise: the owner can open a session |
| `test_the_no_privilege_role_can_open_a_session` | premise: the no-privilege role can open a session |
| `test_the_owner_really_owns_the_table` | premise: t_ssowner really owns the table |
| `test_none_of_the_roles_is_a_superuser` | premise: none of these roles is a superuser |
| `test_the_owner_can_read_its_table` | premise: the owner can read its own table by ordinary SQL |
| `test_the_no_privilege_role_cannot_read_the_table` | premise: the no-privilege role cannot read it by ordinary SQL |
| `test_the_catalogs_are_not_readable` | premise: the catalog tables are NOT readable by these roles, so a GRANT is not the fix |
| `test_the_owner_can_read_its_sort_status` | the OWNER of the table can read its sort status |
| `test_a_grantee_can_read_the_sort_status` | a role merely GRANTed select can read it too |
| `test_the_owner_sees_its_sorted_run` | the owner sees its sorted run, not a false empty |
| `test_a_role_with_no_privilege_is_refused_its_sort_status` | a role with no privilege on the table is refused |
| `test_the_refusal_names_the_table` | and the refusal names the TABLE, not a catalog table it never asked about |
| `test_a_superuser_still_reads_the_sort_status` | a superuser still reads sort status |
## 210. test_bloom_lazy.py: a scan reads the bloom filters of the groups and columns it examines

The pytest twin of `test/bloom_lazy.sh` (ported for #1341), every check by the shell's name. Two tables differ only in how many groups a one-group equality scan skips (6 and 30), so the bloom blocks they fetch, read from `pg_stat_get_blocks_fetched` on `pgcolumnar.bloom`, must not grow with the skipped groups (#310); on a wide table where no group is excluded, six equality predicates must fetch more than one (#314). The answers are compared with a heap mirror, with and without bloom filters.

### Every test

A module fixture on a private database builds the tables in the shell's order, with no parallel workers as the shell's cluster has; each test asserts one reading. A missing block count is a failed query rather than 0, and the kept group must have read a block, so "nothing was read" cannot pass as "nothing extra was read".

| test | what it holds |
| --- | --- |
| `test_the_small_table_has_the_groups_it_should` | the small table has the groups it should |
| `test_the_large_table_has_the_groups_it_should` | the large table has the groups it should |
| `test_the_small_scan_keeps_one_group` | the small scan keeps one group |
| `test_the_large_scan_keeps_one_group` | the large scan keeps one group |
| `test_bloom_reads_do_not_follow_the_skipped_groups` | bloom reads do not follow the skipped groups -- the 24 groups the large table adds read fewer than 12 bloom blocks, and the kept group read at least one (the bloom fetched for every skipped group, or never, no longer passes) |
| `test_the_control_query_really_does_keep_every_group` | the control query really does keep every group |
| `test_a_scan_that_keeps_every_group_reads_more` | a scan that keeps every group reads more |
| `test_equality_on_the_segment_key_agrees_with_the_heap` | equality on the segment key agrees with the heap |
| `test_equality_that_no_group_holds_agrees_with_the_heap` | equality that no group holds agrees with the heap |
| `test_equality_on_a_scattered_column_agrees_with_the_heap` | equality on a scattered column agrees with the heap |
| `test_two_equality_predicates_agree_with_the_heap` | two equality predicates agree with the heap |
| `test_a_range_predicate_still_agrees_with_the_heap` | a range predicate still agrees with the heap |
| `test_full_set_parity_with_the_heap_mirror` | full set parity |
| `test_results_agree_with_bloom_filters_disabled` | results agree with bloom filters disabled |
| `test_an_absent_value_agrees_with_bloom_filters_disabled` | an absent value agrees with bloom filters disabled |
| `test_no_wide_group_is_excluded` | no group is excluded, so every predicate is reached |
| `test_bloom_reads_follow_the_columns_a_query_filters_on` | bloom reads follow the columns a query filters on |
| `test_one_predicate_costs_well_less_than_six` | one predicate costs well less than six |
| `test_the_filtered_answers_agree_with_a_heap_mirror` | the filtered answers agree with a heap mirror |
| `test_six_predicates_give_the_same_answer_as_one_here` | six predicates give the same answer as one here |

## 211. test_sort_status.py: sort_status reports how much of an ordered table is still in its run

The pytest twin of `test/sort_status.sh` (ported for #1341), every check by the shell's name. sort_status splits a table's row groups into the sorted run, recorded by the last vacuum_sorted, cluster or recluster, and the groups appended since (#301). Followed through one table's life: unsorted, sorted, with a tail, re-sorted, rewritten unsorted, clustered, reclustered, with groups retired by compact_rewrite, and after a delete. On a private database.

### Every test

Each value is the first column as text, or a failed query, so two failed reads never agree. A check comparing two sort_status readings is numeric.

| test | what it holds |
| --- | --- |
| `test_groups_written` | groups written |
| `test_an_unsorted_table_reports_no_sorted_groups` | unsorted table reports no sorted groups |
| `test_an_unsorted_table_reports_every_group_appended` | unsorted table reports every group appended |
| `test_an_unsorted_table_reports_no_sorted_rows` | unsorted table reports no sorted rows |
| `test_an_unsorted_table_reports_every_row_appended` | unsorted table reports every row appended |
| `test_no_declared_sort_key_yet` | no declared sort key yet |
| `test_the_sort_rewrote_into_a_new_storage` | the sort rewrote into a new storage |
| `test_rows_survived_the_sort` | rows survived the sort |
| `test_the_table_is_physically_ordered_on_k` | the table is physically ordered on k |
| `test_a_fresh_sort_reports_every_group_sorted` | a fresh sort reports every group sorted |
| `test_a_fresh_sort_reports_no_decay` | a fresh sort reports no decay |
| `test_a_fresh_sort_reports_every_row_sorted` | a fresh sort reports every row sorted |
| `test_a_fresh_sort_reports_no_appended_rows` | a fresh sort reports no appended rows |
| `test_the_sorted_extent_did_not_move` | the sorted extent did not move |
| `test_appended_groups_appeared` | appended groups appeared |
| `test_appended_rows_are_counted` | appended rows are counted |
| `test_sorted_rows_are_unchanged` | sorted rows are unchanged |
| `test_the_totals_add_up` | the totals add up |
| `test_the_table_is_no_longer_fully_ordered` | the table is no longer fully ordered |
| `test_re_sorting_clears_the_appended_tail` | re-sorting clears the appended tail |
| `test_re_sorting_counts_every_row_as_sorted` | re-sorting counts every row as sorted |
| `test_re_sorting_reports_no_appended_rows` | re-sorting reports no appended rows |
| `test_an_unsorted_rewrite_reports_no_sorted_groups` | an unsorted rewrite reports no sorted groups |
| `test_an_unsorted_rewrite_reports_no_sorted_rows` | an unsorted rewrite reports no sorted rows |
| `test_an_unsorted_rewrite_reports_every_row_appended` | an unsorted rewrite reports every row appended |
| `test_cluster_reports_no_decay` | cluster reports no decay |
| `test_cluster_counts_every_row_as_sorted` | cluster counts every row as sorted |
| `test_the_unsorted_rewrite_cleared_the_mark_again` | the unsorted rewrite cleared the mark again |
| `test_recluster_records_its_extent` | recluster records its extent |
| `test_recluster_reports_no_decay` | recluster reports no decay |
| `test_recluster_counts_every_row_as_sorted` | recluster counts every row as sorted |
| `test_recluster_reports_no_appended_rows` | recluster reports no appended rows |
| `test_rows_inserted_after_recluster_are_appended` | rows inserted after recluster are appended |
| `test_the_reclustered_rows_are_still_counted_as_sorted` | the reclustered rows are still counted as sorted |
| `test_a_group_was_rewritten_out_of_the_run` | a group was rewritten out of the run |
| `test_the_run_lost_the_retired_groups` | the run lost the retired groups |
| `test_the_replacement_counts_as_appended` | the replacement counts as appended |
| `test_the_totals_still_add_up` | the totals still add up |
| `test_the_declared_sort_key_is_reported` | the declared sort key is reported |
| `test_an_empty_table_reports_no_groups` | an empty table reports no groups |
| `test_an_empty_table_reports_no_sorted_groups` | an empty table reports no sorted groups |
| `test_an_empty_table_reports_no_appended_rows` | an empty table reports no appended rows |
| `test_the_delete_removed_rows` | the delete removed rows |
| `test_a_delete_leaves_the_sorted_extent_alone` | a delete leaves the sorted extent alone |
| `test_a_delete_leaves_no_appended_tail` | a delete leaves no appended tail |
| `test_live_rows_dropped_by_the_deleted_count` | live rows dropped by the deleted count |
| `test_the_mark_covers_the_runs_last_group` | the mark covers the run's last group |
| `test_the_storage_the_sort_abandoned_is_gone` | the storage the sort abandoned is gone |
| `test_dropping_the_table_removes_the_storage_row` | dropping the table removes the storage row |

## 212. test_smoke.py: a columnar table stores rows and gives them back

The pytest twin of `test/smoke.sh` (ported for #1341), every check by the shell's name. The shell starts a throwaway cluster with pgcolumnar preloaded; the twin uses the harness cluster, which preloads it too, on a private database, so the orphan count reads only this module's tables.

### Every test

A failed step stops the fixture, as `set -e` stops the shell, and every reading is a value or a failed query.

| test | what it holds |
| --- | --- |
| `test_count_star` | count(*) |
| `test_count_where_a_lt_50` | count where a<50 |
| `test_order_by_a_limit_3` | order by a limit 3 |
| `test_no_nulls_in_t_b` | no nulls in t.b |
| `test_null_table_rows` | null table rows |
| `test_null_table_nulls` | null table nulls |
| `test_null_table_value` | null table value |
| `test_orphan_stripes` | orphan stripes |
| `test_native_catalog_tables` | native catalog tables |

## 213. test_server_file_privilege.py: every server-file function is gated like COPY

The pytest twin of `test/server_file_privilege.sh` (ported for #1341), every check by the shell's name. Each of 21 entry points is called as a role with neither server-file role, which must be refused by the gate its direction needs, and as a role with both, which must get past it. Then every function in the install script that takes a path or filename must be one of them. On a private database.

### Every test

A call reads `refused` when the reply names the gate role, `passed` when it does not, and `no reading` when `SET ROLE` never took effect.

| test | what it holds |
| --- | --- |
| `test_the_role_with_nothing_is_refused_by_the_gate` | one test per row of `REFUSED` (21 rows), each by the shell's name |
| `test_the_privileged_role_gets_past_the_gate` | one test per row of `PASSED` (21 rows), each by the shell's name |
| `test_a_file_function_is_covered_by_the_boundary_test` | one test per row of `COVERED` (15 rows), each by the shell's name |
| `test_the_install_script_defines_no_unnamed_file_function` | the install script defines no file function this suite does not name |
| `test_the_coverage_check_found_the_file_functions` | coverage check found the file functions (not an empty scan) |
## 214. test_autovacuum.py: the maintenance daemon compacts and reclusters only while it is on

The pytest twin of `test/autovacuum.sh` (ported for #1341), every check by the shell's name. With `pgcolumnar.autovacuum` off a deleted-heavy table is left alone; turned on, the daemon compacts it without changing its survivors and folds a reclustered table's appended groups back into its run; turned off again, a new deleted table is left alone. The settings arrive by ALTER SYSTEM and a reload and are reset at teardown, and "off by default" is the setting's boot value.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_maintenance_launcher_is_running_for_the_daemon` | the maintenance launcher is running |
| `test_the_daemon_is_off_by_default` | the daemon is OFF by default |
| `test_av_off_starts_with_dead_rows_above_the_threshold` | premise: av_off starts with dead rows above the threshold |
| `test_off_the_daemon_did_not_touch_the_table` | OFF: the daemon did not touch the table (dead rows unchanged) -- over dead rows that stayed (a table with none to leave, or a failed read, no longer passes) |
| `test_av_on_starts_with_dead_rows_above_the_threshold` | premise: av_on starts with dead rows above the threshold |
| `test_the_daemon_is_now_on` | the daemon is now ON |
| `test_on_the_daemon_compacted_the_deleted_heavy_table` | ON: the daemon compacted the deleted-heavy table (dead rows now 0) |
| `test_on_the_surviving_rows_are_unchanged` | ON: the surviving rows are unchanged |
| `test_av_rc_has_a_recorded_key_and_appended_decay` | premise: av_rc has a recorded key and appended decay |
| `test_on_the_daemon_reclustered_the_decayed_table` | ON: the daemon reclustered the decayed table (appended groups folded to 0) |
| `test_off_again_a_new_deleted_table_is_left_alone` | OFF-again: a new deleted table is left alone after disabling -- over dead rows that stayed |

## 215. test_autovacuum_yield.py: the maintenance daemon yields its lock to a waiting session

The pytest twin of `test/autovacuum_yield.sh` (ported for #1341), every check by the shell's name. With the daemon holding ShareUpdateExclusiveLock for a 30s maintenance hold, an ACCESS EXCLUSIVE request is granted within seconds, because the worker marks itself autovacuum and core cancels it, and the daemon logs the cancellation.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_launcher_is_running_for_the_yield` | the launcher is running |
| `test_the_daemon_is_on_with_a_30s_maintenance_hold` | premise: the daemon is on with a 30s maintenance hold |
| `test_the_daemon_is_holding_its_lock_on_the_table` | the daemon is holding SUEL on the table (the hold window is open) |
| `test_yield_the_access_exclusive_lock_was_granted` | yield: the AccessExclusive lock was granted (no timeout -> daemon yielded) -- the statement returning, not the absence of an error (a session that never connected no longer passes) |
| `test_yield_and_it_was_granted_fast` | yield: and it was granted FAST (<= 8s, far under the 30s hold) |
| `test_the_daemon_logged_cancelling_its_maintenance_op` | the daemon logged cancelling its maintenance op |

## 216. test_projection_rewrite.py: a declared projection survives a rewrite

The pytest twin of `test/projection_rewrite.sh` (ported for #1341), every check by the shell's name. After any rewrite, read_projection must answer with exactly the base table's rows (P1), no projection row may name a retired storage (P2), and the declaration must survive (P3) (#876, #887). The rewrites: TRUNCATE in four shapes, including through a foreign-key CASCADE and a trigger that re-enters the utility hook; ALTER COLUMN TYPE on a covered and an uncovered column; ADD COLUMN with a volatile default; a type change on a partitioned parent; REFRESH MATERIALIZED VIEW; and the extension's own vacuum, vacuum_sorted and cluster. Also: the repair adds nothing when nothing was rewritten, a stale declaration warns instead of aborting the statement, and rebuild_projections rebuilds an absent projection. Each arm first asserts what the operation did, because a failed or no-op operation leaves the projection answering. On a private database.

### Every test

| test | what it holds |
| --- | --- |
| `test_a_projection_rewrite_premise_holds` | one test per row of `PREMISES` (17 rows), each by the shell's name |
| `test_a_rewrite_keeps_the_projection` | one test per row of `REWRITES` (53 rows), each by the shell's name |
| `test_the_projection_repair_guards_hold` | one test per row of `GUARDS` (14 rows), each by the shell's name |
## 217. test_avro_manifest.py: Iceberg Avro manifests and manifest lists decode, and refuse what they must

The pytest twin of `test/avro_manifest.sh` (ported for #1341), every check by the shell's name. `read_avro_manifest` and `read_manifest_list` decode the pyiceberg-written fixtures under `test/fixtures/iceberg`, checked against the JSON oracles beside them; a hand-built manifest pins an explicit sequence number; a role without `pg_read_server_files` is refused by that gate; a file with no Avro magic is rejected as corrupt; and a deflate zip-bomb is refused by the cumulative cap.

### Every test

A module fixture on a private database runs the shell's sequence; each test asserts one reading. Survival also reads the server log's count of killed backends, and each refusal reads its message as well as its SQLSTATE.

| test | what it holds |
| --- | --- |
| `test_decoded_entry_count_equals_the_oracle` | decoded entry count == oracle |
| `test_decoded_entries_equal_the_oracle` | decoded entries == oracle (record_count|file_size|region) |
| `test_the_partition_renders_the_schemas_field_name` | partition renders the schema's field name |
| `test_every_entrys_file_path_ends_in_parquet` | every entry's file_path ends in .parquet |
| `test_all_entries_are_added_data_files` | all entries are ADDED data files (status=1, content=0) |
| `test_the_committed_entries_inherit_their_sequence_number` | the committed entries have a NULL (inherited) sequence_number |
| `test_an_explicit_entry_sequence_number_is_decoded` | an explicit entry sequence_number is decoded (42) |
| `test_the_manifest_list_entry_count_equals_the_oracle` | manifest-list entry count == oracle (1) |
| `test_the_manifest_list_entry_equals_the_oracle` | manifest-list entry == oracle (all scalar fields) |
| `test_the_manifest_list_path_basename_equals_the_oracle` | manifest-list manifest_path basename == oracle |
| `test_an_unprivileged_role_is_refused_the_manifest` | an unprivileged role is refused (42501) -- by the function's own gate, the role holding USAGE on the schema |
| `test_a_non_avro_file_is_rejected` | a non-Avro file is rejected (bad magic), backend survives -- the message names the bad magic |
| `test_the_backend_is_still_up_after_the_bad_file` | backend still up after the bad file -- and no backend died since (crash recovery no longer passes) |
| `test_file_format_decodes_to_the_iceberg_enum_name` | file_format decodes to the Iceberg enum name |
| `test_a_deflate_zip_bomb_is_refused_by_the_cap` | a deflate zip-bomb is refused by the cumulative cap (54000) |
| `test_the_backend_is_still_up_after_the_zip_bomb` | backend still up after the zip-bomb -- and no backend died since |
| `test_the_manifest_fixtures_are_present` | the committed fixture is present; without it the test declines under the shell's skip name, iceberg manifest fixtures are missing |
| `test_python_can_read_the_oracle` | the oracle lists manifest entries; without json the test declines under the shell's skip name, python3 is needed to read the oracle |
## 218. test_column_projection.py: column projection reads only the columns a query references

The pytest twin of `test/column_projection.sh` (ported for #1341), every check by the shell's name. A fourteen-column columnar table and its heap copy: one column must cost a fraction of reading them all, SELECT * must read what projection-off reads, every query must answer the same with `pgcolumnar.enable_column_projection` on and off and the same as the heap -- through an added column, deletes, updates and a parallel scan -- and the cost model must price a narrow projection below a wide one.

### Every test

A module fixture on a private database replays the shell's steps in order, with no parallel workers outside the parallel checks; each test asserts one reading. A comparison reads "same" only over two values, so two identical errors or two empty results cannot agree.

| test | what it holds |
| --- | --- |
| `test_columnar_and_heap_hold_the_same_rows` | premise: columnar and heap hold the same rows |
| `test_the_unprojected_read_is_large_enough_to_measure` | premise: the unprojected read is large enough to measure |
| `test_one_column_costs_less_than_a_third_of_all` | one column costs less than a third of reading all of them |
| `test_a_qual_only_column_still_produces_the_right_count` | qual-only column still produces the right count |
| `test_select_star_reads_as_much_as_projection_off` | SELECT * reads as much as projection-off does |
| `test_single_column_agg_on_off` | single column agg :: on==off |
| `test_two_columns_on_off` | two columns :: on==off |
| `test_qual_column_not_selected_on_off` | qual column not selected :: on==off |
| `test_qual_and_target_disjoint_on_off` | qual and target disjoint :: on==off |
| `test_varlena_projected_alone_on_off` | varlena projected alone :: on==off |
| `test_all_null_column_on_off` | all-NULL column :: on==off |
| `test_count_on_off` | count(*) :: on==off |
| `test_select_star_hash_on_off` | select star hash :: on==off |
| `test_whole_row_var_on_off` | whole-row var :: on==off |
| `test_ctid_system_column_on_off` | ctid system column :: on==off |
| `test_order_by_unselected_col_on_off` | order by unselected col :: on==off |
| `test_min_max_mixed_types_on_off` | min/max mixed types :: on==off |
| `test_join_on_projected_column_on_off` | join on projected column :: on==off |
| `test_single_column_agg_columnar_heap` | single column agg :: columnar==heap |
| `test_qual_and_target_disjoint_columnar_heap` | qual and target disjoint :: columnar==heap |
| `test_varlena_alone_columnar_heap` | varlena alone :: columnar==heap |
| `test_select_star_hash_columnar_heap` | select star hash :: columnar==heap |
| `test_all_null_column_columnar_heap` | all-NULL column :: columnar==heap |
| `test_min_max_mixed_types_columnar_heap` | min/max mixed types :: columnar==heap |
| `test_added_column_alone_on_off` | added column alone :: on==off |
| `test_added_column_alone_columnar_heap` | added column alone :: columnar==heap |
| `test_added_column_with_others_columnar_heap` | added column with others :: columnar==heap |
| `test_added_column_across_the_boundary_columnar_heap` | added column across the boundary :: columnar==heap |
| `test_added_column_across_the_boundary_on_off` | added column across the boundary :: on==off |
| `test_after_deletes_projected_column_columnar_heap` | after deletes, projected column :: columnar==heap |
| `test_after_deletes_projected_column_on_off` | after deletes, projected column :: on==off |
| `test_after_updates_projected_column_columnar_heap` | after updates, projected column :: columnar==heap |
| `test_after_updates_select_star_hash_columnar_heap` | after updates, select star hash :: columnar==heap |
| `test_a_parallel_scan_of_a_projected_column_matches_the_heap` | parallel scan, projected column |
| `test_a_parallel_scan_with_a_qual_on_an_unselected_column_matches_the_heap` | parallel scan, qual on unselected column |
| `test_the_narrow_projection_reads_far_fewer_buffers` | the narrow projection really does read far fewer buffers -- not a premise: it measures the projection itself |
| `test_both_scan_costs_are_measurements` | premise: both scan costs are measurements |
| `test_the_cost_model_prices_a_narrow_projection_below_a_wide_one` | the cost model prices a narrow projection below a wide one |

## 219. test_local_open_race_free.py: local file opens are race-free: no stat-before-open

The twin of `test/local_open_race_free.sh` (#644/#686). The shape of PgColumnarOpenLocalRegularFile and the parallel-copy partition coordinator: each opens with O_NONBLOCK in the open call and stats nothing before it, and the racy stat-before-open helper is gone from a tree the sweep read. Functionally, read_parquet and read_avro_manifest refuse a FIFO with the opener's own XX001 rather than hang.

### Every test

The shape readings come from the shell's own function-body extraction, in Python, run in the fixture; a body that was not read is a failed reading. The FIFO lives in `server_dir`, and a statement still running after 5s is a HANG, after which the FIFO's write end is opened non-blocking so the backend's open(2) returns.

| test | what it holds |
| --- | --- |
| `test_the_race_free_opener_exists` | the race-free opener exists |
| `test_the_opener_opens_o_nonblock` | the opener opens O_NONBLOCK (a FIFO cannot block the open) |
| `test_the_opener_fstats_the_fd_it_holds` | the opener fstats the fd it holds (checks what it opened) |
| `test_the_opener_never_stats_a_path_before_opening_it` | the opener never stats a path before opening it (no TOCTOU) |
| `test_the_recursive_sweep_read_source_files` | premise: the recursive sweep read source files under src/ |
| `test_the_racy_stat_before_open_helper_is_gone` | the racy stat-before-open helper is gone |
| `test_the_partition_coordinator_opens_o_nonblock` | the partition coordinator opens O_NONBLOCK (a FIFO cannot block its open) |
| `test_the_partition_coordinator_never_stats_a_path_before_opening_it` | the partition coordinator never stats a path before opening it (no TOCTOU) |
| `test_read_parquet_on_a_fifo_is_a_clean_error` | read_parquet on a FIFO is a clean error, not a hang |
| `test_read_avro_manifest_on_a_fifo_is_a_clean_error` | read_avro_manifest on a FIFO is a clean error, not a hang |
| `test_backend_still_up_after_the_fifo_refusals` | backend still up after the FIFO refusals |

## 220. test_encode_effort.py: the per-table encode_effort option

The twin of `test/encode_effort.sh` (#155). encode_effort and compression are validated and stored; on md5 text, where FSST wins, fast stores more bytes than full, and where FSST loses they store the same; rows, a filtered read and an index fetch agree either way; a table with no option stores what an explicit 'full' stores; reset_options clears the option.

### Every test

Every test reads one value from a module fixture that builds a private database in the shell's order, with pgcolumnar.fsst_min_gain_percent held at 0 on that database. A size is a reading only when it is a positive number, and each content reading carries its row count against the fixture's.

| test | what it holds |
| --- | --- |
| `test_an_unknown_encode_effort_is_refused` | an unknown encode_effort is refused |
| `test_the_refused_value_was_not_stored` | and the refused value was not stored |
| `test_a_valid_encode_effort_is_stored` | a valid encode_effort is stored |
| `test_an_unknown_compression_name_is_refused` | an unknown compression name is refused |
| `test_the_compression_set_before_it_is_still_the_one_stored` | and the compression set before it is still the one stored |
| `test_fast_stores_more_than_full_where_fsst_wins` | fast stores more than full on a shape where FSST wins |
| `test_and_is_identical_where_fsst_does_not_win` | and is identical where FSST does not win |
| `test_the_rows_are_identical_either_way` | the rows are identical either way |
| `test_a_filtered_read_agrees` | and a filtered read agrees |
| `test_an_index_fetch_finds_a_fast_written_row` | and an index fetch finds a fast-written row |
| `test_a_table_with_no_option_set_matches_full` | a table with no option set matches full |
| `test_reset_options_clears_it` | reset_options clears it |

## 221. test_doc_parallel_premise.py: the parallel-scan page's narrow query reads every chunk group

The twin of `test/doc_parallel_premise.sh`. The narrow query's filter column is extracted from docs/limitations.md as the shell extracts it; on the page's fixture shape, that query must read every chunk group the catalog holds, filtering the ordered column must prune, the documented column must not be the ordered one, and it must cost more.

### Every test

Every test reads one value from a module fixture that builds a private database in the shell's order. Every plan is read with max_parallel_workers_per_gather = 0. Where the shell stops (no filter column on the page) or skips its cost block (the page names sel), those tests decline unnamed.

| test | what it holds |
| --- | --- |
| `test_the_page_still_names_a_narrow_query` | the page still names a narrow query |
| `test_that_query_has_an_extractable_filter_column` | and that query has an extractable filter column |
| `test_the_fixture_holds_several_row_groups` | the fixture holds several row groups, so pruning can be seen |
| `test_the_documented_columns_plan_is_one_columnar_scan` | the documented column's plan is exactly one columnar scan node |
| `test_the_sel_plan_is_one_columnar_scan` | the sel plan is exactly one columnar scan node |
| `test_the_group_counters_were_read` | the group counters were read, not blank |
| `test_the_documented_narrow_query_reads_every_chunk_group` | the documented narrow query reads every chunk group |
| `test_filtering_the_ordered_column_prunes` | and filtering the ordered column instead prunes some away |
| `test_the_documented_column_is_not_the_ordered_one` | the documented column is not the one stored in order |
| `test_both_costs_were_extracted` | both costs were extracted, not blank |
| `test_the_documented_column_is_costed_above_the_ordered_one` | and the documented column is costed above the ordered one |

## 222. test_maintenance_due.py: pgcolumnar.maintenance_due reports which online verb is worth running

The twin of `test/maintenance_due.sh` (#415). Five fixtures -- clean and ordered, 40% deleted and never ordered, ordered then appended, never ordered in two batches (the sorted-run guard), and both decays at once -- each with its verdicts and recommendation, the thresholds shown to be live parameters; a SELECT-holder gets a verdict and a role without SELECT is refused with 42501.

### Every test

Every test reads one value from a module fixture that builds a private database in the shell's order. The two roles log in on their own, as the shell's do, and are dropped at teardown.

| test | what it holds |
| --- | --- |
| `test_md_clean_has_no_deleted_rows` | md_clean has no deleted rows |
| `test_md_clean_is_fully_in_its_sorted_run` | md_clean is fully in its sorted run (0 appended groups) |
| `test_md_clean_has_a_sorted_run` | md_clean has a sorted run |
| `test_clean_compact_rewrite_not_due` | clean: compact_rewrite not due |
| `test_clean_recluster_not_due` | clean: recluster not due |
| `test_clean_recommendation_is_null` | clean: recommendation is null |
| `test_md_deleted_deleted_fraction_is_about_0_40` | premise: md_deleted deleted fraction ~0.40 |
| `test_delete_maintenance_due_reports_the_same_deleted_fraction` | delete: maintenance_due reports the same deleted_fraction |
| `test_delete_compact_rewrite_due` | delete: compact_rewrite due (0.40 >= 0.2 default) |
| `test_delete_recluster_not_due` | delete: recluster NOT due (table was never ordered) |
| `test_delete_recommendation_names_compact_rewrite_only` | delete: recommendation names compact_rewrite only |
| `test_delete_raising_the_compact_threshold_makes_it_not_due` | delete: raising the compact threshold above 0.40 makes it not due |
| `test_md_appended_has_appended_groups` | md_appended has appended groups |
| `test_md_appended_still_has_a_sorted_run` | md_appended still has a sorted run |
| `test_appended_recluster_due` | appended: recluster due |
| `test_appended_compact_rewrite_not_due` | appended: compact_rewrite not due (no deletes) |
| `test_appended_recommendation_names_recluster_only` | appended: recommendation names recluster only |
| `test_appended_raising_the_recluster_threshold_makes_it_not_due` | appended: raising the recluster threshold above the fraction makes it not due |
| `test_never_ordered_table_has_no_sorted_run` | never-ordered table has NO sorted run |
| `test_yet_sort_status_still_counts_appended_groups` | yet sort_status still counts appended groups (> 0) |
| `test_guard_recluster_not_due_on_a_never_ordered_table` | GUARD: recluster NOT due on a never-ordered table |
| `test_guard_recommendation_is_null_on_a_never_ordered_table` | GUARD: recommendation is null on a never-ordered table |
| `test_both_compact_rewrite_due` | both: compact_rewrite due |
| `test_both_recluster_due` | both: recluster due |
| `test_both_recommendation_names_both_verbs` | both: recommendation names both verbs |
| `test_positive_a_select_holder_gets_a_verdict` | positive: a SELECT-holder gets a verdict, not a false deny |
| `test_deny_a_role_without_select_is_refused` | deny: a role without SELECT on the relation is refused (42501) |

## 223. test_eager_ordering_record.py: eager rewrites record the ordering they applied

The twin of `test/eager_ordering_record.sh` (#758, #761). vacuum_sorted records a lexicographic run, cluster a Z-order run over its two columns, and the two are told apart in the catalog and through sort_status; the recluster self-gate skips exactly an eagerly clustered table on the same key; an unsorted rewrite claims no ordering; and vacuum_sorted with no columns records the declared key.

### Every test

Every test reads one value from a module fixture that builds a private database in the shell's order, reading each layout at the same point the shell does. Distinguishable and moved are read only over two readings that were both taken.

| test | what it holds |
| --- | --- |
| `test_the_lexicographic_rewrite_ordered_the_rows_on_k` | the lexicographic rewrite really ordered the rows on k |
| `test_it_left_no_unsorted_tail` | it left no unsorted tail |
| `test_eager_vacuum_sorted_records_the_kind_it_applied` | eager vacuum_sorted records the kind it applied |
| `test_sort_status_reports_that_kind_to_the_owner` | and sort_status reports that kind to the table's owner (#761) |
| `test_eager_vacuum_sorted_records_the_key_it_applied` | eager vacuum_sorted records the key it applied |
| `test_the_z_ordered_table_is_not_ordered_on_k` | the Z-ordered table is NOT ordered on k |
| `test_it_too_reports_a_full_run_with_no_tail` | it too reports a full run with no tail |
| `test_eager_cluster_records_the_kind_it_applied` | eager cluster records the kind it applied |
| `test_sort_status_reports_that_kind_not_the_lexicographic_one` | and sort_status reports THAT kind, not the lexicographic one (#761) |
| `test_eager_cluster_records_the_key_it_applied` | eager cluster records the key it applied |
| `test_the_two_eager_layouts_are_distinguishable_in_the_catalog` | the two eager layouts are now distinguishable in the catalog |
| `test_sort_status_reports_the_applied_key_on_the_z_ordered_table` | sort_status reports the APPLIED key on the Z-ordered table, not the declared {k} |
| `test_sort_status_still_reports_k_on_the_sorted_table` | sort_status still reports {k} on the lexicographically sorted table |
| `test_the_two_layouts_are_distinguishable_through_sort_status_alone` | the two layouts are distinguishable through sort_status alone (#761) |
| `test_the_gate_fixture_has_more_than_one_group` | the gate fixture has more than one group |
| `test_and_live_rows_in_them` | premise: and live rows in them |
| `test_recluster_on_the_applied_key_does_nothing` | recluster on the key the eager cluster already applied does nothing |
| `test_it_rewrote_no_group` | and it rewrote no group: the physical layout is unchanged |
| `test_recluster_on_a_different_key_still_rewrites_every_group` | control: recluster on a DIFFERENT key still rewrites every group |
| `test_that_rewrite_really_moved_the_layout` | control: and that rewrite really moved the layout |
| `test_the_insert_appended_past_the_recorded_run` | the insert appended past the recorded run |
| `test_recluster_with_an_appended_tail_does_not_skip` | control: recluster with an appended tail does not skip |
| `test_it_moved_the_tailgate_layout` | control: and it moved the tailgate layout |
| `test_recluster_does_not_skip_a_lexicographic_run` | control: recluster does not skip a LEXICOGRAPHIC run of the same lead column |
| `test_it_moved_the_lexgate_layout` | control: and it moved the lexgate layout |
| `test_the_mark_is_set_before_the_unsorted_rewrite` | the mark is set before the unsorted rewrite |
| `test_an_unsorted_rewrite_claims_no_ordering` | an unsorted rewrite claims no ordering |
| `test_sort_status_reports_no_kind_either` | and sort_status reports no kind either, rather than a stale one (#761) |
| `test_and_records_no_key` | and records no key |
| `test_vacuum_sorted_with_no_columns_records_the_declared_key` | vacuum_sorted with no columns records the DECLARED key it applied |
| `test_and_records_it_as_a_lexicographic_run` | and records it as a lexicographic run |

## 224. test_int8_agg_int128.py: sum and avg over bigint accumulate in 128 bits

The twin of `test/int8_agg_int128.sh` (#785, #786). Every value arm runs vectorized, scalar and on heap and requires all three to agree, including sums past int64 in both directions, empty and all-NULL columns, and numeric; two timing arms bound the vectorized path at 1.10x the scalar one as a ratio of interleaved minima; and a LATERAL rescan must match the heap.

### Every test

Every test reads one value from a module fixture that builds a private database in the shell's order. The heap oracle is loaded from the same generator as the columnar table, never copied out of it. A NULL answer reads <NULL>, and a rescan reading must return its four rows.

| test | what it holds |
| --- | --- |
| `test_the_vectorized_node_really_runs_for_sum` | premise: the vectorized node really runs for sum(v) |
| `test_and_for_avg` | premise: and for avg(v) |
| `test_sum_bigint_agrees_with_the_scalar_path_and_with_heap` | sum(bigint) agrees with the scalar path and with heap |
| `test_avg_bigint_agrees_too` | avg(bigint) agrees too |
| `test_the_fixture_really_holds_nulls_and_negatives` | premise: the fixture really holds NULLs and negatives |
| `test_sum_over_a_column_with_nulls_and_negatives_agrees` | sum over a column with NULLs and negatives agrees |
| `test_avg_over_the_same_agrees` | avg over the same agrees |
| `test_the_sum_really_exceeds_what_int64_can_hold` | premise: the sum really exceeds what int64 can hold |
| `test_the_vectorized_node_runs_on_this_table_too` | premise: the vectorized node runs on this table too |
| `test_a_sum_past_int64_agrees` | a sum past int64 agrees with the scalar path and with heap |
| `test_and_its_avg_agrees` | and its avg agrees |
| `test_this_sum_is_past_int64_in_the_negative_direction` | premise: this sum is past int64 in the negative direction |
| `test_a_large_negative_sum_agrees` | a large NEGATIVE sum agrees |
| `test_mixed_signs_summing_back_into_int64_range_agree` | mixed signs summing back into int64 range agree |
| `test_sum_over_an_empty_table_is_null` | sum over an empty table is NULL, as on heap |
| `test_avg_over_an_empty_table_is_null_too` | avg over an empty table is NULL too |
| `test_sum_over_an_all_null_column_is_null` | sum over an all-NULL column is NULL |
| `test_avg_over_an_all_null_column_is_null` | avg over an all-NULL column is NULL |
| `test_sum_numeric_keeps_its_scale_and_still_agrees` | sum(numeric) keeps its scale and still agrees |
| `test_avg_numeric_too` | avg(numeric) too |
| `test_the_timing_fixture_loaded_every_row` | premise: the timing fixture loaded every row |
| `test_the_vectorized_node_really_runs_so_the_ratio_compares_two_paths` | premise: and the vectorized node really runs, so the ratio compares two paths |
| `test_the_vectorized_path_is_not_slower_than_the_scalar_one` | the vectorized path is not slower than the scalar one |
| `test_the_filtered_fixture_loaded_every_row` | premise: the filtered fixture loaded every row |
| `test_its_values_are_not_low_cardinality` | premise: its values are NOT low-cardinality, so the fold is not answered from runs |
| `test_the_vectorized_node_runs_on_the_filtered_query_too` | premise: the vectorized node runs on the FILTERED query too |
| `test_the_filtered_vectorized_path_is_not_slower` | the FILTERED vectorized path is not slower than the scalar one |
| `test_the_rescan_shape_still_takes_the_vectorized_node` | premise: the rescan shape still takes the vectorized node |
| `test_the_node_really_is_re_executed` | premise: the node really is re-executed, so a stale accumulator can show |
| `test_the_rescan_query_returns_a_row_per_driver_row` | premise: the rescan query returns a row per driver row, not an error |
| `test_sum_bigint_across_a_rescan_matches_the_heap` | sum(bigint) across a rescan matches the heap |
| `test_avg_bigint_across_a_rescan_matches_the_heap` | avg(bigint) across a rescan matches the heap |
| `test_sum_bigint_across_a_rescan_on_the_big_fixture_matches_the_heap` | sum(bigint) across a rescan on the >int64 fixture matches the heap |
| `test_count_across_the_same_rescan_matches_the_heap` | control: count(*) across the same rescan matches the heap |

## 225. test_fsst_verdict_cache.py: the FSST keep/drop verdict is cached with an age bound

The twin of `test/fsst_verdict_cache.sh` (#472). Cached HELPS and HURTS verdicts store byte-identical chunks, a bound of one row group re-decides every time, a bounded cache tracks a column that changes character more closely than an unbounded one, values match a heap loaded from the same generator, and caching makes an md5 load at least 10% faster.

### Every test

Every test reads one value from a module fixture that builds a private database in the shell's order. A decline and a value comparison carry their row counts, and a timed load must have loaded.

| test | what it holds |
| --- | --- |
| `test_the_prose_fixture_loaded` | premise: the prose fixture loaded |
| `test_it_spans_several_row_groups` | premise: and it spans several row groups, so caching has something to reuse |
| `test_the_md5_fixture_loaded` | premise: the md5 fixture loaded |
| `test_the_prose_corpus_keeps_fsst` | premise: the prose corpus KEEPS fsst, so the HELPS branch is exercised |
| `test_the_md5_corpus_drops_it` | premise: and the md5 corpus DROPS it, so the two arms are different branches |
| `test_a_reused_helps_verdict_stores_byte_identical_chunks` | a reused HELPS verdict stores byte-identical chunks |
| `test_and_it_still_keeps_fsst` | and it still keeps fsst, rather than silently dropping it |
| `test_a_reused_hurts_verdict_stores_byte_identical_chunks` | a reused HURTS verdict stores byte-identical chunks |
| `test_and_it_still_declines_fsst` | and it still declines fsst |
| `test_the_cached_load_returns_the_same_values_as_heap` | the cached load returns the same values as heap |
| `test_a_reuse_bound_of_one_row_group_is_byte_identical_to_no_caching` | a reuse bound of one row group is byte-identical to no caching |
| `test_the_three_changing_column_loads_all_produced_bytes` | premise: the three changing-column loads all produced bytes to compare |
| `test_an_unbounded_cache_really_does_diverge` | an unbounded cache really does diverge on this fixture |
| `test_a_bounded_cache_tracks_the_change_more_closely` | a bounded cache tracks the change more closely than an unbounded one |
| `test_the_changing_column_still_returns_its_values` | and the changing column still returns its values |
| `test_caching_the_verdict_makes_the_load_measurably_faster` | caching the verdict makes the load measurably faster |

## 226. test_import_exclusion.py: import_arrow enforces an exclusion constraint, and a rewrite does not

The twin of `test/import_exclusion.sh` (follow-up to #153). A clean import into a table with EXCLUDE USING btree (k WITH =) succeeds and reaches the index; a violating import raises and leaves the row count alone, as an ordinary INSERT does; and a rewrite that really moves rows does not check the constraint against the rows it replaces.

### Every test

Every test reads one value from a module fixture that builds a private database in the shell's order. The arrow file lives in `server_dir`, because the backend writes and reads it.

| test | what it holds |
| --- | --- |
| `test_the_constraint_is_an_exclusion_constraint_not_a_unique_index` | the constraint is an exclusion constraint, not a unique index |
| `test_importing_rows_that_violate_nothing_succeeds` | importing rows that violate nothing succeeds |
| `test_and_the_rows_are_there` | and the rows are there |
| `test_an_index_scan_finds_an_imported_row` | an index scan finds an imported row |
| `test_importing_rows_that_violate_the_constraint_raises` | importing rows that violate the constraint raises |
| `test_the_failed_import_left_the_row_count_alone` | and the failed import left the row count alone |
| `test_an_ordinary_insert_of_the_same_violation_also_raises` | an ordinary INSERT of the same violation also raises |
| `test_a_rewrite_of_the_same_table_still_succeeds` | a rewrite of the same table still succeeds |
| `test_and_the_rows_survived_it` | and the rows survived it |

## 227. test_import_deferred.py: imports honour a DEFERRABLE constraint, and the check is deferred, not gone

The twin of `test/import_deferred.sh` (#168). A transient collision commits under a deferred unique constraint through import_arrow and import_parquet as on heap; a duplicate that survives to commit, an IMMEDIATE constraint, SET CONSTRAINTS IMMEDIATE mid-transaction and an exclusion constraint each still fail with their own SQLSTATE; a partial unique index is maintained for exactly the rows it covers; and compact_rewrite over a uniquely indexed table enforces nothing.

### Every test

Every test reads one value from a module fixture that builds a private database in the shell's order; heap is the oracle where the shell uses it. The export files live in `server_dir`.

| test | what it holds |
| --- | --- |
| `test_heap_allows_a_transient_collision` | premise: heap allows a transient collision under a deferred constraint |
| `test_an_ordinary_columnar_insert_still_allows_it` | an ordinary columnar INSERT still allows it |
| `test_import_arrow_allows_it` | import_arrow allows it |
| `test_import_parquet_allows_it` | import_parquet allows it |
| `test_heap_rejects_a_duplicate_still_there_at_commit` | premise: heap rejects a duplicate that is still there at commit |
| `test_import_arrow_rejects_a_duplicate_that_survives_to_commit` | import_arrow rejects a duplicate that survives to commit |
| `test_the_import_rolled_back` | and the import rolled back rather than leaving the row |
| `test_an_immediate_constraint_still_rejects_at_insert_time` | an IMMEDIATE constraint still rejects at insert time |
| `test_set_constraints_immediate_is_honoured_mid_transaction` | SET CONSTRAINTS IMMEDIATE is honoured mid-transaction |
| `test_an_exclusion_constraint_is_still_enforced_on_import` | an exclusion constraint is still enforced on import |
| `test_a_partial_unique_index_accepts_rows_it_does_not_cover` | a partial unique index accepts rows it does not cover |
| `test_rows_outside_the_partial_index_are_all_present` | rows outside the partial index are all present |
| `test_a_rewrite_of_a_uniquely_indexed_table_still_succeeds` | a rewrite of a uniquely-indexed table still succeeds |
| `test_and_the_surviving_rows_are_intact` | and the surviving rows are intact |

## 228. test_index_only.py: index-only scans on a columnar table, served from the visibility map

The twin of `test/index_only.sh` (gap 28). VM bits written for synthetic blocks round-trip; lazy VACUUM marks all-visible groups and a delete clears its block; the GUC gates the index-only plan; all-visible blocks are served with zero heap fetches and a deleted range falls back; and an old REPEATABLE READ snapshot never sees post-snapshot rows through an index-only scan.

### Every test

Every test reads one value from a module fixture that builds a private database in the shell's order, with the planner settings set on that database as the shell sets them. Session A is a second connection holding its snapshot open.

| test | what it holds |
| --- | --- |
| `test_rows_loaded` | rows loaded |
| `test_vm_bit_round_trips_block_0` | vm bit round-trips on columnar rel (block 0) |
| `test_vm_bit_round_trips_block_3` | vm bit round-trips on columnar rel (block 3) |
| `test_vm_bit_round_trips_block_50` | vm bit round-trips on columnar rel (block 50) |
| `test_vm_bit_round_trips_block_171` | vm bit round-trips on columnar rel (block 171) |
| `test_first_set_on_fresh_block_succeeds` | first set on fresh block succeeds |
| `test_second_set_on_same_block_sees_prior_bit` | second set on same block sees prior bit |
| `test_iv_row_count` | iv row count |
| `test_before_vacuum_block_50_not_all_visible` | before vacuum: block 50 not all-visible |
| `test_after_vacuum_block_10_all_visible` | after vacuum: block 10 all-visible |
| `test_after_vacuum_block_50_all_visible` | after vacuum: block 50 all-visible |
| `test_after_vacuum_block_120_all_visible` | after vacuum: block 120 all-visible |
| `test_after_delete_block_50_cleared_by_write` | after delete: block 50 cleared by write |
| `test_after_delete_vacuum_deleted_block_50_not_all_visible` | after delete+vacuum: deleted block 50 not all-visible |
| `test_after_delete_vacuum_clean_block_10_still_all_visible` | after delete+vacuum: clean block 10 still all-visible |
| `test_after_delete_vacuum_clean_block_120_still_all_visible` | after delete+vacuum: clean block 120 still all-visible |
| `test_iv_contents_match_heap_oracle_after_vacuum_delete` | iv contents match heap oracle after vacuum/delete |
| `test_guc_off_no_index_only_scan` | GUC off: no index-only scan |
| `test_guc_on_index_only_scan_chosen` | GUC on: index-only scan chosen |
| `test_all_visible_interior_zero_heap_fetches` | all-visible interior: zero heap fetches |
| `test_index_only_results_match_heap_oracle_all_visible` | index-only results match heap oracle (all-visible) |
| `test_index_only_results_match_heap_oracle_after_delete` | index-only results match heap oracle after delete (fallback) |
| `test_after_delete_some_heap_fetches_occur` | after delete: some heap fetches occur |
| `test_session_a_baseline_sees_batch_1_only` | session A baseline sees batch 1 only |
| `test_old_snapshot_ios_does_not_see_post_snapshot_rows` | old snapshot IOS does not see post-snapshot rows |
| `test_session_a_used_an_index_only_scan` | session A used an index-only scan |
| `test_new_snapshot_sees_both_batches` | new snapshot sees both batches |

## 229. test_isolation.py: deterministic isolation specs for the compaction and truncation races

The twin of `test/isolation.sh` (Phase F3). pg_isolation_regress runs seven exact interleavings -- delete against compact_rewrite, an old snapshot against a delete and compaction, a reader against compaction, recluster against delete, and end-truncation against a reader, a writer and another truncation -- and each spec is its own check.

### Every test

The fixture runs pg_isolation_regress once over all seven specs in a private database, with --use-existing as the shell runs it, and reads each spec's verdict from its line; a spec with no line is "not run". Without the binary every test declines unnamed.

| test | what it holds |
| --- | --- |
| `test_isolation_spec_delete_vs_rewrite` | isolation spec delete_vs_rewrite |
| `test_isolation_spec_old_snapshot_compact` | isolation spec old_snapshot_compact |
| `test_isolation_spec_compact_vs_reader` | isolation spec compact_vs_reader |
| `test_isolation_spec_recluster_vs_delete` | isolation spec recluster_vs_delete |
| `test_isolation_spec_truncate_vs_reader` | isolation spec truncate_vs_reader |
| `test_isolation_spec_truncate_vs_writer` | isolation spec truncate_vs_writer |
| `test_isolation_spec_truncate_vs_truncate` | isolation spec truncate_vs_truncate |

## 230. test_concurrency.py: concurrent deletes into one chunk group serialize and both survive

The twin of `test/concurrency.sh` (#4). Two sessions deleting different rows of one chunk group serialize on the chunk-group lock, and both deletes survive -- with the group's delete_vector row present and absent; deletes into different chunk groups do not wait and finish in a live transaction.

### Every test

One module fixture runs the three scenarios in a private database; session 2's blocking DELETE runs in a thread while the controller polls pg_stat_activity as the shell does, every wait bounded at 60s.

| test | what it holds |
| --- | --- |
| `test_a_same_group_second_deleter_blocks` | A same-group second deleter blocks |
| `test_a_both_concurrent_deletes_survived` | A both concurrent deletes survived (ids 1,2,6 gone) |
| `test_a_row_count_after_concurrent_deletes` | A row count after concurrent deletes |
| `test_b_first_delete_second_deleter_blocks` | B first-delete second deleter blocks |
| `test_b_both_first_deletes_survived` | B both first deletes survived (ids 1,2 gone) |
| `test_c_different_group_deleter_does_not_block` | C different-group deleter does not block |
| `test_c_both_different_group_deletes_survived` | C both different-group deletes survived (ids 1,5 gone) |
| `test_every_bounded_wait_completed` | a bounded wait for a session to reach idle-in-transaction completed -- the shell's four wait_timeout names, recorded only where a wait timed out; declines on a green run, where the shell records nothing |

## 231. test_iceberg_scan.py: iceberg_scan reads by field id, behind the server-file gate

The twin of `test/iceberg_scan.sh` (#388 phase 3c). Over a relocated warehouse the scan reads the current snapshot, projects and reorders by field id, reads a subset and a column the schema renamed, refuses a name the schema lacks, refuses a role without pg_read_server_files, and lets a non-superuser member read every row.

### Every test

One module fixture copies the warehouse into `server_dir`, writes the renamed metadata beside it, and reads every value from a private database; the two roles log in on their own and are dropped at teardown.

| test | what it holds |
| --- | --- |
| `test_the_data_file_carries_field_ids` | premise: the data file carries field ids (id=1,region=2,amount=3) |
| `test_iceberg_scan_reads_the_current_snapshots_rows` | iceberg_scan reads the current snapshot's rows |
| `test_iceberg_scan_projects_and_reorders_by_field_id` | iceberg_scan projects and reorders by field id (amount, id) |
| `test_iceberg_scan_reads_a_subset` | iceberg_scan reads a subset (region only) |
| `test_a_renamed_column_still_reads_by_field_id` | a column renamed in the schema still reads by field id (amt2 = amount) |
| `test_an_output_column_not_in_the_schema_is_refused` | an output column not in the schema is refused (42703) |
| `test_an_unprivileged_role_is_refused_the_scan` | an unprivileged role is refused (42501) |
| `test_a_pg_read_server_files_member_may_scan` | a pg_read_server_files member who is not superuser may scan |

## 232. test_iceberg_catalog.py: the Iceberg catalog reader resolves the named current snapshot

The twin of `test/iceberg_catalog.sh` (#388 phase 3a). Against pyiceberg's own view of a real table, the reader returns the current snapshot, exactly one row with no parent, the snapshot the table names rather than snapshots[0], zero rows with no current snapshot, and refuses a dangling id, non-Iceberg JSON and malformed JSON; a role without pg_read_server_files is refused and a non-superuser member reads the catalog.

### Every test

One module fixture writes every metadata file into `server_dir` and reads every value from a private database; the two roles log in on their own and are dropped at teardown.

| test | what it holds |
| --- | --- |
| `test_current_snapshot_matches_the_oracle` | current snapshot == oracle (id|seq|op|schema|manifest-list) |
| `test_exactly_one_current_snapshot_row` | exactly one current snapshot row |
| `test_the_first_snapshot_has_no_parent` | the first snapshot has no parent (NULL) |
| `test_resolves_the_named_current_snapshot` | resolves the named current snapshot, not snapshots[0] |
| `test_the_decoy_snapshot_is_not_returned` | the decoy snapshot is not returned |
| `test_a_table_with_no_current_snapshot_returns_zero_rows` | a table with no current snapshot returns zero rows |
| `test_a_dangling_current_snapshot_id_is_refused` | a dangling current-snapshot-id is refused (data_corrupted XX001) |
| `test_a_non_iceberg_json_object_is_refused` | a non-Iceberg JSON object is refused (invalid_parameter_value 22023) |
| `test_malformed_json_is_rejected` | malformed JSON is rejected (22P02), backend survives |
| `test_backend_still_up_after_the_bad_metadata` | backend still up after the bad file |
| `test_an_unprivileged_role_is_refused_the_catalog` | an unprivileged role is refused (42501) |
| `test_a_pg_read_server_files_member_may_read_the_catalog` | a pg_read_server_files member who is not superuser may read the catalog |

## 233. test_iceberg_name_mapping_memory.py: the Iceberg name-mapping parse does not allocate per entry

The twin of `test/iceberg_name_mapping_memory.sh` (#731). The peak memory of a scan over a padded name mapping, less core's own parse of the byte-identical mapping, as a slope across two sizes, stays under 30 bytes an entry, and the padded mapping still binds every row with the same values.

### Every test

Each peak is VmHWM from /proc for the backend of the connection that ran the one statement, before and after it; a statement that errored has no peak. The fixtures live in `server_dir`.

| test | what it holds |
| --- | --- |
| `test_the_small_arm_produced_three_readings` | the small arm produced three readings |
| `test_the_large_arm_produced_three_readings` | the large arm produced three readings |
| `test_cores_own_parse_is_the_larger_part` | core's own parse is the larger part of the scan's peak |
| `test_the_two_sizes_give_different_peaks` | the two sizes give different peaks |
| `test_the_name_mapping_parse_does_not_allocate_per_entry` | the name-mapping parse does not allocate per entry (#731) |
| `test_the_padded_mapping_still_binds_every_row` | the padded mapping still binds every row |
| `test_the_mapped_values_are_still_correct` | and the mapped values are still correct |

## 234. test_iceberg_fdw_estimate.py: the Iceberg FDW estimates rows from the manifests

The twin of `test/iceberg_fdw_estimate.sh`. The FDW's row estimate is positive, equals the live record count summed from the manifests rather than the old flat 1000, and still has the scan's selectivity applied.

### Every test

One module fixture copies the warehouse into `server_dir` and reads every value from a private database.

| test | what it holds |
| --- | --- |
| `test_the_fdw_estimates_a_positive_row_count` | the FDW estimates a positive row count from the manifests |
| `test_the_estimate_is_the_real_record_count` | the estimate is the real record count, not the old constant 1000 |
| `test_a_filtered_scan_estimates_fewer_rows` | a filtered scan estimates fewer rows than the whole table (selectivity applied) |
| `test_backend_alive_after_the_estimates` | backend alive |

## 235. test_iceberg_fdw_projection.py: the Iceberg FDW's projection pushdown never changes a result

The twin of `test/iceberg_fdw_projection.sh`. Over a 13-column fixture, every projection shape the FDW decodes -- one column, three, the whole row, none, a filter on an unselected column, one selected and another filtered -- answers what bare iceberg_scan answers.

### Every test

One module fixture copies the warehouse into `server_dir` and reads both sides of each shape from a private database; the two are compared only when both were taken.

| test | what it holds |
| --- | --- |
| `test_one_column` | one column : FDW projection matches iceberg_scan |
| `test_three_columns` | three columns : FDW projection matches iceberg_scan |
| `test_whole_row` | whole row : FDW projection matches iceberg_scan |
| `test_count_only` | count only (no column referenced) : FDW projection matches iceberg_scan |
| `test_filter_references_an_unselected_column` | filter references an unselected column : FDW projection matches iceberg_scan |
| `test_select_one_filter_another` | select one, filter another : FDW projection matches iceberg_scan |
| `test_backend_alive_after_the_projections` | backend alive |

## 236. test_estimate_deleted.py: the planner's row estimate follows deletes

The pytest twin of `test/estimate_deleted.sh` (ported for #1341), every check by the shell's name. After deleting 99% of a table and analysing it, EXPLAIN must estimate the live rows, as heap does, not the physical occupancy; with reltuples left stale on a table of several row groups, the estimate must still follow the deletes, while an undeleted table of the same shape estimates its full size.

### Every test

A module fixture on a private database runs the shell's sequence; each test asserts one reading. An estimate that is not a number is a failed reading, never zero.

| test | what it holds |
| --- | --- |
| `test_columnar_holds_its_rows_before_the_delete` | premise: columnar holds 100000 rows before delete |
| `test_the_estimate_before_the_delete_is_the_full_table` | the estimate before delete is the full table |
| `test_a_thousand_live_rows_remain` | premise: 1000 live rows remain |
| `test_analyze_recorded_the_live_count_in_reltuples` | ANALYZE recorded the live count in reltuples |
| `test_the_estimate_after_the_delete_is_the_live_count` | the planner estimate after delete is the live count, not the physical occupancy |
| `test_heap_is_near_the_live_count_too` | control: heap is also near the live count, so the probe reads EXPLAIN |
| `test_the_fixture_spans_several_row_groups` | premise: the fixture spans several row groups, not one |
| `test_reltuples_is_full_before_the_delete` | reltuples is 100000 before the delete |
| `test_the_estimate_follows_the_deletes_with_a_stale_reltuples` | the estimate follows the deletes with a stale reltuples and several groups |
| `test_the_live_count_really_is_60000` | control: and the live count really is 60000 |
| `test_an_undeleted_table_estimates_its_full_size` | control: an undeleted table of the same shape estimates its full size |

## 237. test_generated_columns.py: a columnar table with a generated column returns what heap does

The twin of `test/generated_columns.sh`. STORED generated columns, on every major, read, filter and aggregate as the heap copy does and get a column chunk. VIRTUAL ones, on PostgreSQL 18+, read and filter as heap does, equal their expression on every row, and store no chunk while the base column still gets one.

### Every test

Every test reads one value from a module fixture that builds a private database in the shell's order. Every columnar reading carries its row count against the constant the fixture loads, and the heap oracle must hold the fixture's aggregate. Before 18 the virtual tests decline; only the row-count test names the shell's skip.

| test | what it holds |
| --- | --- |
| `test_stored_generated_row_count` | stored generated: row count |
| `test_stored_generated_values_match` | stored generated: values match |
| `test_stored_generated_filtered_scan_matches` | stored generated: filtered scan matches |
| `test_stored_generated_aggregate_matches` | stored generated: aggregate matches |
| `test_stored_generated_chunk_present_for_attr_2` | stored generated: chunk present for attr 2 |
| `test_virtual_generated_row_count` | virtual generated: row count -- before 18, declines as `virtual generated columns`, the shell's skip |
| `test_virtual_generated_values_match` | virtual generated: values match |
| `test_virtual_generated_filtered_scan_matches` | virtual generated: filtered scan matches |
| `test_virtual_generated_recomputed_correctly` | virtual generated: recomputed correctly |
| `test_virtual_generated_no_chunk_stored_for_the_virtual_columns` | virtual generated: no chunk stored for the virtual columns |
| `test_virtual_generated_base_column_still_stored` | virtual generated: base column still stored |

## 238. test_export_sink.py: the export byte sink checks every write and never leaves a partial file under the final name

The twin of `test/export_sink.sh` (#394 step 1). No fwrite remains in either export file; parquet and arrow exports of 40,000 rows import back hash-equal to the source with no `*.tmp.*` debris; and with `pgcolumnar.sink_fail_after` injecting the error a full disk raises, each export fails 53100, nothing exists at its final path, a failed re-export leaves the previous good file intact, and no temp file remains.

### Every test

Every test reads one value from a module fixture that builds a private database in the shell's order. The files live in `server_dir`, because the backend writes them. Each round-trip reading carries its row count, a directory that cannot be listed is a failed reading, and the intact-file reading carries the re-export's SQLSTATE, so a re-export that never ran cannot pass.

| test | what it holds |
| --- | --- |
| `test_census_zero_fwrite_remain_in_columnar_parquet_c` | census: zero fwrite remain in columnar_parquet.c |
| `test_census_zero_fwrite_remain_in_columnar_arrow_c` | census: zero fwrite remain in columnar_arrow.c |
| `test_parquet_export_succeeds_through_the_sink` | parquet export succeeds through the sink |
| `test_arrow_export_succeeds_through_the_sink` | arrow export succeeds through the sink |
| `test_no_temp_debris_after_successful_exports` | no temp debris after successful exports |
| `test_parquet_round_trip_hash_matches_the_source` | parquet round-trip hash-matches the source |
| `test_arrow_round_trip_hash_matches_the_source` | arrow round-trip hash-matches the source |
| `test_the_arrow_size_supports_a_mid_batch_injection_point` | measured arrow size supports a mid-batch injection point |
| `test_inject_parquet_export_fails_as_disk_full` | inject: parquet export fails as disk-full (53100) |
| `test_inject_nothing_exists_at_the_parquet_final_path` | inject: nothing exists at the parquet FINAL path |
| `test_inject_a_failed_re_export_leaves_the_previous_parquet_file_intact` | inject: a failed re-export leaves the previous parquet file intact |
| `test_inject_no_parquet_temp_debris` | inject: no parquet temp debris |
| `test_inject_arrow_export_fails_inside_write_record_batch` | inject: arrow export fails inside write_record_batch (53100) |
| `test_inject_nothing_exists_at_the_arrow_final_path` | inject: nothing exists at the arrow FINAL path |
| `test_inject_no_arrow_temp_debris` | inject: no arrow temp debris |
| `test_after_reset_exports_work_again` | after RESET, exports work again |

## 239. test_groupagg_table_sizing.py: the vectorized grouped aggregate sizes its table from the estimate, bounded

The twin of `test/groupagg_table_sizing.sh` (#403 item 6, #810 review). On 2,000,000 rows in 200,000 groups the node rehashes at most a quarter of the doubling ladder in at most four allocations and never allocates more than the real group count needs; an over-estimated expression key never inflates a table that does not grow, and a key that does grow never jumps past eight times its need. Every answer is read through the vectorized node.

### Every test

Every test reads one value from a module fixture that builds a private database in the shell's order. A counter that was not printed is a failed reading, and a real group count must be a positive number.

| test | what it holds |
| --- | --- |
| `test_the_vectorized_grouped_aggregate_is_the_node_that_ran` | the vectorized grouped aggregate is the node that ran |
| `test_it_built_the_group_count_this_suite_is_sized_for` | premise: it built the group count this suite is sized for |
| `test_the_work_done_counter_is_reported` | the work-done counter is reported at all |
| `test_the_declared_n_distinct_reached_the_planner` | premise: the declared n_distinct reached the planner, so the estimate is not a sample |
| `test_a_sized_table_removes_most_of_the_rehashing` | a sized table removes most of the rehashing (#403 item 6) |
| `test_it_gets_there_in_a_few_allocations` | and it gets there in a few allocations, not a ladder (#403 item 6) |
| `test_the_table_is_never_larger_than_the_real_group_count_needs` | the table is never larger than the real group count needs (#810 review) |
| `test_the_planner_really_over_estimates_an_expression_key` | premise: the planner really over-estimates an expression key, or there is nothing to guard |
| `test_a_wrong_estimate_cannot_inflate_a_table_that_never_needed_to_grow` | a wrong estimate cannot inflate a table that never needed to grow (#810 review) |
| `test_the_expression_key_still_answers_correctly` | and the expression key still answers correctly |
| `test_this_key_really_does_force_the_table_to_grow` | premise: this key really does force the table to grow, or the bound is untested |
| `test_a_grow_cannot_jump_to_a_wrong_estimate` | a grow cannot jump to a wrong estimate, only toward it (#810 review) |
| `test_the_minute_key_answers_correctly_too` | and the minute key answers correctly too |
| `test_the_answer_is_the_same_under_a_small_work_mem` | and the answer is the same under a small work_mem |
| `test_the_aggregate_itself_is_still_right` | the aggregate itself is still right |

## 240. test_fsst_margin.py: the FSST keep/drop margin decides, and never changes content

The twin of `test/fsst_margin.sh` (#155, #271). Margin 0 keeps FSST on a marginal corpus and margin 90 drops it; the shipped default drops it; with no codec it is kept even at the maximum margin; and tables loaded at margin 0, at 90 and at the default read back what a heap mirror holds, NULLs and empty strings included, over a corpus whose margin-0 copy is FSST.

### Every test

Every test reads one value from a module fixture that builds a private database in the shell's order. Each drop carries its table's row count, and the content readings are compared only when both were taken.

| test | what it holds |
| --- | --- |
| `test_the_heap_mirror_has_content_to_compare_against` | the heap mirror has content to compare against |
| `test_at_margin_0_this_corpus_keeps_fsst` | at margin 0 this corpus keeps FSST |
| `test_at_margin_90_the_same_corpus_drops_fsst` | at margin 90 the same corpus drops FSST |
| `test_the_margin_is_what_changed_the_decision` | so the margin is what changed the decision, not the corpus |
| `test_the_shipped_default_drops_fsst_on_a_marginal_corpus` | the shipped default drops FSST on a marginal corpus |
| `test_and_0_still_keeps_it` | and 0 still keeps it, so the default is a decision and not a rewrite |
| `test_the_paired_arm_dropped_fsst_at_this_margin_with_a_codec` | the paired arm dropped FSST at this margin with a codec |
| `test_with_no_codec_the_same_corpus_keeps_fsst_at_the_same_margin` | with no codec the same corpus keeps FSST at the same margin |
| `test_and_at_the_maximum_margin_too` | and at the maximum margin too, because the test returns before reading it |
| `test_the_margin_0_content_copy_really_is_stored_as_fsst` | the margin-0 content copy really is stored as FSST, so the invariant reads FSST |
| `test_fm_c0_reads_back_exactly_what_the_heap_holds` | fm_c0 reads back exactly what the heap holds |
| `test_fm_c90_reads_back_exactly_what_the_heap_holds` | fm_c90 reads back exactly what the heap holds |
| `test_fm_cd_reads_back_exactly_what_the_heap_holds` | fm_cd reads back exactly what the heap holds |
| `test_fm_c0_preserves_nulls` | fm_c0 preserves NULLs |
| `test_fm_c0_preserves_empty_strings` | fm_c0 preserves empty strings |
| `test_fm_c90_preserves_nulls` | fm_c90 preserves NULLs |
| `test_fm_c90_preserves_empty_strings` | fm_c90 preserves empty strings |

## 241. test_index_delete_liveness.py: nbtree's deletion passes never destroy an entry for a live row

The twin of `test/index_delete_liveness.sh`. After twelve rounds of churn on a non-indexed column over 400 live rows, rolled back or committed, an index scan must still reach all 400; churn on the indexed column and a heap table under the same hint are the controls, and at shipped defaults count(*) and count(b) must agree.

### Every test

Every test reads one value from a module fixture that builds a private database in the shell's order. Every churn reports the rows it updated, and each arm's reading carries that count.

| test | what it holds |
| --- | --- |
| `test_arm_a_holds_n_rows_before_the_churn` | premise: arm A holds 400 rows before the churn |
| `test_arm_a_really_plans_an_index_scan` | premise: arm A really plans an Index Scan, so the arm measures the index |
| `test_arm_a_a_rollback_leaves_every_live_row_reachable` | arm A: a ROLLBACK leaves every live row reachable through the index |
| `test_control_b_indexed_column_churn_loses_nothing` | control B: indexed-column churn triggers no bottom-up pass and loses nothing |
| `test_control_c_the_heap_loses_nothing` | control C: the heap loses nothing under the same churn |
| `test_arm_d_a_commit_leaves_every_live_row_reachable` | arm D: a COMMIT leaves every live row reachable through the index |
| `test_at_shipped_defaults_count_star_is_right` | at shipped defaults count(*) is right |
| `test_at_shipped_defaults_count_star_and_count_b_agree` | at shipped defaults the table answers the same to count(*) and count(b) |
| `test_oracle_the_heap_answers_count_star_correctly` | oracle: the heap answers count(*) correctly after the same churn |

## 242. test_rewrite_group_scan.py: a rewrite reads each row group once, not once per row

The pytest twin of `test/rewrite_group_scan.sh` (ported for #1341), every check by the shell's name. A group whose decoded form exceeds the fetch cache cap used to be re-decoded once per row during a rewrite (#196). The fixture crosses the cap, and the rewrite must finish within a generous statement_timeout, keep every surviving row, agree with a heap mirror, and keep the rows reachable through an index built before it. On a private database.

### Every test

A rewrite reads `finished` only when compact_rewrite returned a positive count. The fixture is held to its survivors first. The index `rgs_id` is built before the rewrite, so the rewrite has to insert the moved rows' index entries itself.

| test | what it holds |
| --- | --- |
| `test_the_fixture_holds_its_survivors` | premise: the fixture holds the rows that survive the delete |
| `test_a_rewrite_over_the_cache_cap_finishes` | a rewrite of a group over the cache cap finishes |
| `test_the_rewrite_preserved_every_surviving_row` | the rewrite preserved every surviving row |
| `test_the_rewrite_agrees_with_a_heap_mirror` | and it agrees with a heap mirror |
| `test_a_group_under_the_cap_still_rewrites_correctly` | a group under the cap still rewrites correctly |
| `test_the_index_lookup_reads_the_index` | the index lookup's plan reads rgs_id |
| `test_an_index_lookup_finds_a_rewritten_row` | an index lookup finds a rewritten row |

## 243. test_recluster_gate.py: recluster skips its rewrite when the table is already this key's run

The pytest twin of `test/recluster_gate.sh` (ported for #1341), every check by the shell's name. recluster returns the number of row groups it rewrote, and its self-gate (#415) makes a repeat on an unchanged table rewrite nothing; an append or a new key makes it work again. On a private database.

### Every test

The row-set check reads a heap mirror built from the generator, not copied from the table under test. A layout comparison needs both readings.

| test | what it holds |
| --- | --- |
| `test_the_first_gate_recluster_does_real_work` | the first recluster does real work (>0 groups) |
| `test_sort_status_reports_the_clustering_key` | sort_status now reports the clustering key (was NULL before #415) |
| `test_the_recluster_run_covers_the_whole_relation` | the run covers the whole relation (0 appended groups) |
| `test_a_repeat_on_an_unchanged_table_reclusters_nothing` | recluster by the same key on an unchanged table reclusters 0 groups |
| `test_the_layout_is_byte_identical` | and it leaves the physical layout byte-identical (group offsets unmoved) |
| `test_a_third_identical_recluster_is_a_no_op` | a third identical recluster is also a no-op |
| `test_the_insert_appended_unsorted_groups` | premise: the insert appended unsorted groups |
| `test_a_recluster_after_appends_does_work` | recluster after appends does real work again (>0) |
| `test_recluster_folds_the_tail_back_into_the_run` | and it folds the new data back into the run (0 appended after) |
| `test_a_recluster_by_a_new_key_does_work` | recluster by a NEW key is NOT a no-op -- it re-clusters (>0) |
| `test_sort_status_reports_the_new_key` | and sort_status now reports the new key |
| `test_a_repeat_by_the_new_key_is_a_no_op` | a repeat recluster by the new key is a no-op |
| `test_the_no_op_reclusters_preserved_the_row_set` | no-op recluster preserved the exact row set (columnar == heap mirror) |

## 244. test_recluster_extent.py: an online recluster records only the extent it rewrote

The pytest twin of `test/recluster_extent.sh` (ported for #1341), every check by the shell's name. The online recluster runs while other sessions write, and its run must cover the groups it wrote and nothing else: not rows inserted during it (#311), and not a group whose id another writer drew below the rewrite's (#342). The races run in threads, as the shell runs them in background jobs. On a private database.

### Every test

Whether the insert ran during the rewrite is asked of the server before the insert begins. The run must claim at least one row and no more than the base. The projection arm first reads its projection back from the catalog, so a failed `add_projection` cannot turn it into a second arm without one.

| test | what it holds |
| --- | --- |
| `test_the_table_loaded` | the table loaded |
| `test_the_insert_ran_during_the_rewrite` | the insert ran while the rewrite was still running |
| `test_the_rewrite_reported_groups_reclustered` | the rewrite reported groups reclustered |
| `test_the_concurrent_insert_was_not_in_the_work_set` | premise: the concurrent insert was not in the rewrite's work set |
| `test_every_row_is_present_afterwards` | every row is present afterwards |
| `test_the_run_does_not_claim_rows_written_during_the_rewrite` | the run does not claim rows written during the rewrite |
| `test_the_counts_still_cover_every_stored_row` | the counts still cover every stored row |
| `test_the_concurrent_rows_are_reported_as_appended` | the concurrent rows are reported as appended |
| `test_a_group_numbered_below_the_run_is_not_ordered` | a group numbered below the run is not counted as ordered |
| `test_the_rows_below_the_run_are_appended` | the rows written below the run are reported as appended |
| `test_the_id_drawn_below_counts_cover_every_row` | the id-drawn-below counts still cover every stored row |
| `test_noproj_a_full_recluster_claims_every_row` | noproj: a full recluster claims every row |
| `test_noproj_a_full_recluster_leaves_nothing_appended` | noproj: a full recluster leaves nothing appended |
| `test_withproj_add_projection_created_the_projection` | withproj: add_projection created the projection |
| `test_withproj_a_full_recluster_claims_every_row` | withproj: a full recluster claims every row |
| `test_withproj_a_full_recluster_leaves_nothing_appended` | withproj: a full recluster leaves nothing appended |
| `test_an_uncontended_rewrite_claims_the_whole_relation` | an uncontended rewrite claims the whole relation |
| `test_an_uncontended_rewrite_counts_every_row_as_sorted` | an uncontended rewrite counts every row as sorted |

## 245. test_reader_buffer_reuse.py: the reader reuses one group buffer, and the reuse never shows in an answer

The pytest twin of `test/reader_buffer_reuse.sh` (ported for #1341), every check by the shell's name. Reading every group costs well under one group's worth of minor faults per extra group (log_executor_stats), and the full scan, projected scans, whole-group reads, a short group, added and dropped columns and a rescan all match heap mirrors. On a private database, with serial planning as the shell's cluster runs.

### Every test

The heap mirrors are built from the generators, not copied from the tables under test. A differential comparison refuses an empty side.

A read that names its columns fills only their ranges of the reused buffer. A read with no projection reads the whole group into it in one call: a whole-row read, and every read with `pgcolumnar.enable_column_projection = off`. Four tests compare that read with the mirror, before and after the short group.

| test | what it holds |
| --- | --- |
| `test_the_reuse_fixture_has_several_row_groups` | premise: the fixture has several row groups, or reuse cannot matter |
| `test_the_one_group_arm_reads_one_group` | premise: the one-group arm really reads exactly one row group |
| `test_the_all_groups_arm_reads_every_group` | premise: the all-groups arm really reads every row group |
| `test_the_instrument_reports_faults` | premise: the instrument reports a nonzero fault count at all |
| `test_reading_every_group_reuses_the_buffer` | reading every group does not cost per-group faults (ratio well under the group count) |
| `test_the_full_scan_matches_the_mirror` | the full scan matches a heap mirror exactly |
| `test_a_projected_scan_matches_too` | a PROJECTED scan (one column of two) matches too |
| `test_the_full_scan_with_projection_off_matches_the_mirror` | the full scan with column projection off (a whole-group read) matches a heap mirror |
| `test_a_whole_row_read_matches_the_mirror` | a whole-row read (a whole-group read) matches a heap mirror |
| `test_a_smaller_group_was_added` | premise: that really added a group smaller than the ones before it |
| `test_a_short_group_after_a_full_one_reads_correctly` | a short group after a full one still reads correctly |
| `test_a_short_group_reads_correctly_with_projection_off` | a short group after a full one reads correctly with column projection off |
| `test_a_short_group_reads_correctly_in_a_whole_row_read` | a short group after a full one reads correctly in a whole-row read |
| `test_the_differential_fixture_spans_several_groups` | premise: the differential fixture spans several row groups |
| `test_a_projected_subset_matches_the_heap_twin` | one test per row of `PROJECTIONS` (8 rows), each by the shell's name |
| `test_an_added_column_reads_back_null` | a column ADDED after the groups exist reads back NULL, as on heap |
| `test_an_all_null_column_beside_a_wide_one_matches` | an all-NULL column beside a wide one matches |
| `test_reads_after_drop_column_match` | reads after DROP COLUMN match |
| `test_a_rescan_returns_the_same_rows` | a rescan (nested loop) returns the same rows |

## 246. test_nologin_workers.py: a NOLOGIN role's background workers connect and act as that role

The pytest twin of `test/nologin_workers.sh` (#1585), every check by the shell's name. As a NOLOGIN role reached by SET ROLE and holding both server-file roles: `parallel_export_parquet` and `parallel_copy` return every row, a row trigger inside the copy worker records that role and not the authenticated user, the parallel flush encodes every column in a worker, and the server log holds no refused login for the role beyond the one the premise provokes. Before #1585 the workers connected as the calling role, which PostgreSQL 15 and 16 refuse for a role without LOGIN. On a private database.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_nologin_role_cannot_open_a_session` | premise: the NOLOGIN role cannot open a session |
| `test_the_server_log_records_the_refused_login` | premise: the server log records the NOLOGIN role's refused login -- the positive control for the log reading the last test relies on |
| `test_set_role_to_the_nologin_role_takes` | premise: SET ROLE to the NOLOGIN role takes |
| `test_a_nologin_roles_parallel_export_exports_every_row` | a NOLOGIN role's parallel_export_parquet exports every row |
| `test_a_nologin_roles_parallel_copy_loads_every_row` | a NOLOGIN role's parallel_copy loads every row |
| `test_parallel_copys_workers_wrote_as_the_nologin_role` | parallel_copy's workers wrote every row as the NOLOGIN role -- a worker that connected as the authenticated user and never took the caller's id writes `postgres` here |
| `test_the_flush_is_dispatched_to_workers` | the flush is dispatched to workers |
| `test_a_nologin_roles_parallel_flush_encodes_every_column_in_a_worker` | a NOLOGIN role's parallel flush encodes every column in a worker -- read from the flush's own DEBUG1 split, because a refused worker falls back to serial and stores the same rows |
| `test_the_table_holds_every_row_it_flushed` | and the table holds every row it flushed |
| `test_no_worker_was_refused_a_login` | no worker was refused a login as the NOLOGIN role |
| `test_a_role_resetting_copy_trigger_writes_as_the_nologin_role` | a copy trigger that resets the role still writes every row as the NOLOGIN role -- the worker's session user is the caller on every major (#1612 review) |
| `test_the_role_resetting_copy_leaves_nothing_prepared` | and that copy leaves no prepared transaction behind |
| `test_a_role_switching_copy_trigger_is_refused_before_prepare` | a copy trigger that switches to another role is refused before the worker prepares: a transaction prepared as another user is one the caller cannot finish |
| `test_the_refused_copy_leaves_nothing_prepared` | and the refused copy leaves no prepared transaction behind |
| `test_a_definer_copy_keeps_its_restriction_in_the_workers` | a copy run by a SECURITY DEFINER function keeps the definer's restriction in its workers: the worker takes the caller's security context, inside its transaction |

## 247. test_scan_direction.py: a scrollable cursor over a columnar table never invents a backward answer

The pytest twin of `test/scan_direction.sh` (ported for #1341), every check by the shell's name. At shipped defaults the custom scan serves a cursor and PostgreSQL materializes under SCROLL, so a backward fetch matches heap; with the custom scan off, the access method must raise 0A000 for a backward fetch and for FETCH LAST. On a private database, with serial planning as the shell's cluster runs.

### Every test

A cursor's rows are compared with heap's only when both returned rows. A statement reads as its SQLSTATE, `00000` when it ran, or `no reading` when the server could not be reached.

| test | what it holds |
| --- | --- |
| `test_the_pair_holds_the_same_row_count` | premise: the pair holds the same row count |
| `test_at_shipped_defaults_the_cursor_matches_heap` | at shipped defaults the cursor matches heap |
| `test_with_the_custom_scan_off_the_plan_is_a_seq_scan` | with the custom scan off the plan really is a Seq Scan on the AM |
| `test_a_forward_only_fetch_matches_heap_through_the_am` | forward-only fetch still matches heap through the AM |
| `test_a_backward_fetch_through_the_am_raises_0a000` | a backward fetch through the AM raises 0A000 rather than answering |
| `test_fetch_last_through_the_am_raises_0a000` | FETCH LAST through the AM raises 0A000 rather than returning nothing |
| `test_the_same_cursor_on_heap_raises_nothing` | control: the same cursor on a heap table raises nothing |
| `test_a_forward_only_fetch_reports_success` | control: a forward-only fetch reports success, so the probe is not stuck on 0A000 |

## 248. test_qual_order_selectivity.py: skip predicates are probed most-selective-first

The pytest twin of `test/qual_order_selectivity.sh` (ported for #1341), every check by the shell's name. With an unselective predicate written before a selective one, the reader moves the excluding predicate to the front, so a pruned group costs one zone-map probe rather than two (#403 item 4). On a private database, with serial planning.

### Every test

A probe count is a number or `no reading`. The heap mirror is built from the generator.

| test | what it holds |
| --- | --- |
| `test_the_columnar_scan_ran` | premise: the columnar scan ran, so these are its counters |
| `test_both_predicates_are_usable` | both predicates were pushed down and are usable |
| `test_the_fixture_has_its_groups` | premise: the fixture has the groups this suite is sized for |
| `test_the_selective_predicate_still_prunes` | the selective predicate still prunes what it always did |
| `test_the_probe_counter_is_reported` | the probe counter is reported at all |
| `test_a_pruned_group_is_not_probed_through_every_column` | a pruned group is not probed through every predicate's column (#403 item 4) |
| `test_the_order_the_predicates_are_written_in_does_not_matter` | and it does not depend on the order the predicates are written in |
| `test_the_answer_matches_the_heap_oracle` | the answer matches the heap oracle |
| `test_a_range_selective_predicate_matches` | and so does a query where the selective predicate is a range |
| `test_nothing_selective_still_answers_correctly` | and one where NOTHING is selective, which must still answer correctly |
| `test_no_selective_predicate_prunes_nothing` | a query with no selective predicate prunes nothing, as before |

## 249. test_pushdown_report.py: EXPLAIN reports what qual pushdown did, the same way on every columnar node

The pytest twin of `test/pushdown_report.sh` (ported for #1341), every check by the shell's name. Pushed-down filters, usable skip predicates and the chunk groups they remove are reported with pushdown on and off, for a usable qual and one whose operator builds no skip predicate, on the scalar scan and both vectorized aggregates, under plain EXPLAIN and EXPLAIN ANALYZE (#191, #479, #493, #495, #726). On a private database, with serial planning.

### Every test

A comparison of two EXPLAIN fields is numeric, so a field missing from both plans is not agreement.

| test | what it holds |
| --- | --- |
| `test_the_plan_is_a_columnar_custom_scan` | the plan under test is a columnar custom scan |
| `test_it_is_still_one_with_pushdown_off` | and it is still one with pushdown off |
| `test_pushdown_on_reports_a_filter` | pushdown on reports a pushed-down filter |
| `test_pushdown_off_reports_none` | pushdown off reports none |
| `test_pushdown_on_skips_chunk_groups` | pushdown on skips chunk groups |
| `test_pushdown_off_skips_none` | pushdown off skips none |
| `test_the_executor_filters_more_with_pushdown_off` | the executor filters far more rows with pushdown off |
| `test_pushdown_off_examines_every_row` | pushdown off examines every row |
| `test_the_answer_is_unchanged_with_pushdown_on` | the answer is unchanged with pushdown on |
| `test_the_answer_is_unchanged_with_pushdown_off` | the answer is unchanged with pushdown off |
| `test_two_quals_report_two_filters` | two quals report two pushed-down filters |
| `test_integer_ops_has_no_ordering_proc_for_the_pair` | premise: integer_ops has no ordering proc for (bigint, oid) |
| `test_core_has_no_such_pair` | premise: and core has no such pair of its own, which is why this is built |
| `test_the_usable_arm_is_a_custom_scan` | the usable arm is a columnar custom scan |
| `test_the_unusable_arm_is_a_custom_scan` | the unusable arm is a columnar custom scan |
| `test_the_usable_arm_removes_chunk_groups` | the usable arm removes chunk groups |
| `test_the_unusable_arm_removes_none` | the unusable arm removes none |
| `test_the_unusable_arm_returns_the_right_answer` | and the unusable arm still returns the right answer |
| `test_both_arms_report_the_filter_pushed_down` | both arms report the filter as pushed down |
| `test_the_usable_arm_reports_one_usable_predicate` | the usable arm reports one usable skip predicate |
| `test_the_unusable_arm_reports_no_usable_predicate` | the unusable arm reports none |
| `test_pushdown_off_reports_no_usable_predicates` | pushdown off reports no usable skip predicates |
| `test_the_ungrouped_node_ran_usable` | the ungrouped vectorized aggregate node ran (usable arm) |
| `test_the_ungrouped_node_ran_unusable` | the ungrouped vectorized aggregate node ran (unusable arm) |
| `test_ungrouped_the_usable_arm_removes_groups` | ungrouped: the usable arm removes chunk groups |
| `test_ungrouped_the_unusable_arm_removes_none` | ungrouped: the unusable arm removes none |
| `test_the_plain_explain_arm_is_the_ungrouped_node` | the plain EXPLAIN arm is the ungrouped vectorized aggregate |
| `test_the_analyze_arm_reports_a_nonzero_count` | the same query under ANALYZE reports a non-zero count |
| `test_plain_explain_reports_the_ungrouped_filters` | plain EXPLAIN reports the ungrouped node's pushed-down filters (#726) |
| `test_plain_explain_reports_the_ungrouped_vector_predicates` | plain EXPLAIN reports the ungrouped node's vector predicates (#726) |
| `test_the_grouped_node_is_planned_for_the_cross_node_arm` | the grouped node IS planned for the cross-node arm |
| `test_plain_explain_the_grouped_node_agrees` | plain EXPLAIN: the grouped node agrees with the ungrouped one (#493, #726) |
| `test_ungrouped_the_usable_arm_reports_one_of_each` | ungrouped: the usable arm reports one of each, agreeing |
| `test_ungrouped_the_unusable_arm_builds_no_predicate` | ungrouped: and the unusable arm builds no skip predicate |
| `test_the_grouped_node_ran_usable` | the grouped vectorized aggregate node ran (usable arm) |
| `test_the_grouped_node_ran_unusable` | the grouped vectorized aggregate node ran (unusable arm) |
| `test_grouped_the_usable_arm_removes_groups` | grouped: the usable arm removes chunk groups |
| `test_grouped_the_unusable_arm_removes_none` | grouped: the unusable arm removes none |
| `test_grouped_the_usable_arm_reports_one_of_each` | grouped: the usable arm reports one of each, agreeing |
| `test_grouped_the_unusable_arm_builds_no_predicate` | grouped: and the unusable arm builds no skip predicate |
| `test_each_counter_has_one_emitter` | one test per row of `EMITTERS` (6 rows), each by the shell's name |
| `test_the_scalar_arm_is_the_scalar_scan` | the scalar arm really is the scalar scan (#493) |
| `test_scalar_and_ungrouped_agree` | scalar and ungrouped agree on Pushed-Down Filters (#493) |
| `test_scalar_and_grouped_agree` | scalar and grouped agree on Pushed-Down Filters (#493) |
| `test_the_ungrouped_node_reports_vector_predicates_separately` | the ungrouped node reports its vector-predicate count separately (#493) |
| `test_it_is_the_count_that_used_to_be_mislabelled` | and it is the count that used to be mislabelled (#493) |

## 250. test_row_triggers.py: row-level triggers on a columnar table fire once per row with the right row

The pytest twin of `test/row_triggers.sh` (ported for #1341), every check by the shell's name. An AFTER INSERT row trigger re-fetches its row by TID before the stripe is flushed, so the access method answers from its write buffer (#179); deferred, UPDATE and DELETE triggers fetch rows on disk. Each shape runs on a columnar table and its heap twin, and the two trigger logs must agree. On a private database.

### Every test

The two logs agree only when the heap twin's trigger fired at all. A statement succeeds only when it raised nothing.

| test | what it holds |
| --- | --- |
| `test_a_single_row_insert_with_an_after_row_trigger_succeeds` | a single-row insert with an after-row trigger succeeds |
| `test_the_single_row_landed` | and the row itself landed |
| `test_the_trigger_saw_the_same_row_heap_did` | the trigger saw the same row heap did |
| `test_a_multi_row_insert_fires_once_per_row` | a multi-row insert fires once per row with the right values |
| `test_an_insert_crossing_a_stripe_boundary_fires_for_every_row` | an insert crossing a stripe boundary fires for every row |
| `test_1500_rows_with_no_mid_statement_flush_all_fire` | 1500 rows with no mid-statement flush all fire |
| `test_after_update_fires_with_the_new_row` | after-update fires with the new row |
| `test_after_delete_fires_with_the_old_row` | after-delete fires with the old row |
| `test_insert_then_update_in_one_transaction` | insert then update in one transaction |
| `test_a_before_row_trigger_still_fires_once_per_row` | a before-row trigger still fires once per row |
| `test_a_deferred_constraint_trigger_still_fires_at_commit` | a deferred constraint trigger still fires at commit |
| `test_an_import_into_a_table_with_an_after_row_trigger_succeeds` | an import into a table with an after-row trigger succeeds |
| `test_the_imported_rows_landed` | the imported rows landed |
| `test_the_trigger_fired_once_per_imported_row` | and the trigger fired once per imported row |

## 251. test_projection_rename_restore.py: a projection's declaration follows a column rename, so it rebuilds after restore

The pytest twin of `test/projection_rename_restore.sh` (ported for #1341), every check by the shell's name. A projection is declared by column names and restore rebuilds it from the declaration, so a column renamed afterwards, on the table or on a partition through its parent, must be renamed in the declaration too (#888). On two private databases: the source and the restore target.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_live_projection_reads_after_the_rename` | the live projection still reads after the rename |
| `test_the_child_projection_reads_after_a_parent_rename` | the child projection still reads after a parent rename |
| `test_pg_dump_succeeds` | pg_dump succeeds |
| `test_restore_succeeds` | restore succeeds |
| `test_the_renamed_projection_rebuilds_after_restore` | the renamed projection rebuilds after restore |
| `test_the_rebuilt_projection_holds_every_row` | the rebuilt projection contains every restored row |
| `test_the_descendant_declaration_follows_the_parent_rename` | the descendant declaration follows the parent rename |
| `test_the_rebuilt_child_projection_holds_every_row` | the rebuilt child projection contains every restored row |

## 252. test_preimage_rewrite.py: a predicate on date_trunc of a column prunes like the range it stands for

The pytest twin of `test/preimage_rewrite.sh` (ported for #1341), every check by the shell's name. A predicate on `date_trunc(unit, ts)` is rewritten to a range on `ts`, so the zone maps can prune it (#403 item 1). The answers cannot see the rewrite, so the work checks compare the chunk groups each form removes with the explicit range it stands for: equality, and the four ordered operators in both operand orders and with constants that are not truncated. The answer checks catch a rewrite that prunes wrongly, against the range and against heap mirrors, by count and by rows. Near the end of the timestamp range and on the infinities the rewrite must answer rather than raise; the shapes that must decline still answer as heap does; and the keys stay conservative, so the batch fold declines them. The heap mirrors are built from the generators, never copied from the columnar tables, and the server must not have lost a backend since setup. On a private database, with serial planning.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_fixture_spans_many_chunk_groups` | premise: the fixture spans many chunk groups |
| `test_the_explicit_range_prunes` | the explicit range prunes, so there IS something to remove |
| `test_date_trunc_equality_prunes` | date_trunc('day', ts) = const prunes chunk groups (#403) |
| `test_date_trunc_equality_prunes_as_well_as_the_range` | ...and prunes as well as the equivalent explicit range |
| `test_the_rewritten_predicate_returns_the_ranges_answer` | the rewritten predicate returns the range's answer |
| `test_the_rewritten_predicate_returns_the_heaps_answer` | and the heap mirror's answer |
| `test_the_rewritten_predicate_returns_the_heaps_rows` | the actual ROWS match the heap, not just the count |
| `test_the_range_for_an_ordered_arm_prunes` | one test per ordered arm (8): the range for [arm] prunes, so there is something to remove |
| `test_an_ordered_arm_prunes` | one test per ordered arm (8): [arm] prunes chunk groups |
| `test_an_ordered_arm_prunes_as_well_as_the_range` | one test per ordered arm (8): [arm] prunes as well as the equivalent explicit range |
| `test_an_ordered_arm_returns_the_ranges_answer` | one test per ordered arm (8): [arm] returns the range's answer |
| `test_an_ordered_arm_returns_the_heaps_answer` | one test per ordered arm (8): [arm] returns the heap's answer |
| `test_an_ordered_arm_returns_the_heaps_rows` | one test per ordered arm (8): [arm] returns the heap's ROWS, not just its count |
| `test_the_near_maximum_row_is_there` | premise: the near-maximum row is really there |
| `test_an_edge_arm_answers_as_heap_does` | one test per edge predicate (11): [arm] answers instead of raising, and agrees with the heap |
| `test_the_fixture_holds_one_positive_infinity` | premise: the fixture holds one +infinity row |
| `test_the_fixture_holds_one_negative_infinity` | premise: the fixture holds one -infinity row |
| `test_a_non_truncated_constant_returns_no_rows` | a non-truncated constant returns no rows |
| `test_the_heap_agrees_it_is_unsatisfiable` | ...and the heap agrees it is unsatisfiable |
| `test_timestamptz_is_correct_under_utc` | timestamptz: correct under UTC |
| `test_timestamptz_is_correct_under_another_zone` | timestamptz: and still correct under a different zone |
| `test_a_non_constant_unit_answers_correctly` | a non-constant unit still answers correctly |
| `test_the_rewritten_predicate_does_not_engage_the_batch_fold` | the rewritten predicate does not engage the batch fold |
| `test_the_rewritten_predicate_answers_with_the_fold_enabled` | ...and still answers correctly with the fold enabled |
| `test_a_unit_returns_the_heaps_answer` | one test per unit (month, week, quarter, year): unit 'u' returns the heap's answer |
| `test_a_unit_matches_some_rows` | one test per unit: unit 'u': premise: it matches some rows |
| `test_an_unlisted_unit_answers_correctly` | an unlisted unit (millennium) still answers correctly |
| `test_the_backend_is_still_up` | backend still up -- the server answers AND the log shows no backend killed since setup |

## 253. test_pg19_vacuum_options.py: PostgreSQL 19's parallel-autovacuum parameter is accepted on a columnar table and changes nothing

The pytest twin of `test/pg19_vacuum_options.sh` (ported for #1341), every check by the shell's name. `autovacuum_parallel_workers` is accepted on a columnar table and recorded in reloptions as on heap, and it changes nothing: VACUUM, VACUUM (PARALLEL 2) and `pgcolumnar.vacuum` succeed with it set and leave the rows and their content unchanged, and VACUUM VERBOSE reports on heap but not on the columnar table, whose vacuum path is pgcolumnar's (#398). A statement's success is read from the driver, not from output lacking "ERROR". Each test declines before PostgreSQL 19 under the shell's skip name, `parallel autovacuum`, and is listed per older major in `expected_unrunnable.txt`. On a private database.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_server_variable_exists` | autovacuum_max_parallel_workers exists on this server |
| `test_the_parameter_is_accepted_on_columnar` | autovacuum_parallel_workers is accepted on av_c |
| `test_the_parameter_is_recorded_on_columnar` | and it is recorded in reloptions for av_c |
| `test_the_parameter_is_accepted_on_heap` | autovacuum_parallel_workers is accepted on av_h |
| `test_the_parameter_is_recorded_on_heap` | and it is recorded in reloptions for av_h |
| `test_vacuum_with_the_parameter_succeeds` | VACUUM on a columnar table with the parameter set succeeds |
| `test_parallel_vacuum_succeeds` | VACUUM (PARALLEL 2) on a columnar table succeeds |
| `test_pgcolumnar_vacuum_succeeds` | pgcolumnar.vacuum with the parameter set succeeds |
| `test_the_rows_are_unchanged` | the rows are unchanged by any of it |
| `test_the_content_is_unchanged` | and the content is unchanged |
| `test_vacuum_verbose_reports_on_heap` | control: VACUUM VERBOSE reports on the heap table |
| `test_vacuum_verbose_is_silent_on_columnar` | columnar VACUUM VERBOSE reports nothing, because the AM path is ours |

## 254. test_cost_written_geometry.py: the cost model describes the geometry a table was written with

The twin of `test/cost_written_geometry.sh` (#806). Two 400,000-row tables, gb written at the default stripe_row_limit and ga at 20,000: varying only the planning session's stripe_row_limit must not reprice gb's index fetch, ga must cost less than gb by a margin and under a third of it, and a never-written table must still plan.

### Every test

Every test reads one value from a module fixture that builds a private database in the shell's order. Every plan is read with max_parallel_workers_per_gather = 0 in the session that sets the plan-time GUC. A cost that cannot be read is a failed reading, so two missing costs cannot agree, and the row counts are compared against the constant.

| test | what it holds |
| --- | --- |
| `test_both_tables_hold_the_same_number_of_rows` | premise: both tables hold the same number of rows |
| `test_gb_was_written_at_the_default_limit_in_few_groups` | premise: gb was written at the default limit, in few groups |
| `test_ga_was_written_at_20000_in_many_more_groups` | ga was written at 20000, in many more groups |
| `test_the_catalog_recorded_both_limits` | and the catalog recorded both limits |
| `test_gb_is_costed_as_an_index_scan_at_plan_time_limit_150000` | premise: gb is costed as an index scan at plan-time limit 150000 |
| `test_gb_is_costed_as_an_index_scan_at_plan_time_limit_20000` | premise: gb is costed as an index scan at plan-time limit 20000 |
| `test_the_planning_sessions_limit_does_not_reprice_a_written_table` | the planning session's stripe_row_limit does not reprice a written table |
| `test_a_table_written_in_smaller_groups_is_costed_cheaper` | a table written in smaller groups is costed cheaper than one in larger |
| `test_the_index_fetch_penalty_sees_the_written_geometry` | the index-fetch penalty sees the written geometry: ga costs under a third of gb |
| `test_a_never_written_table_still_produces_a_plan` | a never-written table still produces a plan |

## 255. test_encode_invariants.py: encoding invariants for the #155 bulk-load optimisations

The twin of `test/encode_invariants.sh` (#155). The C self-test matches each rewritten primitive against its reference implementation over a real case count; bit packing at every reachable width, full-range 64-bit values, Gorilla float patterns and dictionary text around the distinct cap round-trip against a heap mirror, each with a control that its encoder ran; a high-cardinality corpus selects FSST and nothing at or below the cap does; and the block compression default is zstd at level 3.

### Every test

Every test reads one value from a module fixture that builds a private database in the shell's order, with enable_post_codec_encoding_choice off on that database. A round-trip agrees only over a non-empty columnar hash, and every FSST-premise table must hold its 20,000 rows.

| test | what it holds |
| --- | --- |
| `test_the_encoding_primitives_match_their_reference_implementations` | the encoding primitives match their reference implementations byte for byte |
| `test_the_self_test_compared_a_meaningful_number_of_cases` | and the self-test actually compared a meaningful number of cases |
| `test_every_reachable_bit_pack_width_round_trips` | every reachable bit-pack width round-trips |
| `test_full_range_64_bit_values_round_trip` | full-range 64-bit values round-trip whatever encoding is chosen |
| `test_the_width_fixtures_chose_frame_of_reference_packing` | and the width fixtures actually chose frame-of-reference packing |
| `test_gorilla_float_patterns_round_trip` | gorilla float patterns round-trip, including all-zero XOR |
| `test_at_least_one_pattern_chose_gorilla` | and at least one of them actually chose gorilla |
| `test_dictionary_round_trips_around_the_distinct_cap` | dictionary round-trips at, below and above the distinct cap |
| `test_the_low_cardinality_fixture_chose_dictionary` | and the low-cardinality fixture actually chose dictionary |
| `test_a_high_cardinality_corpus_does_select_fsst` | control: a high-cardinality corpus does select FSST |
| `test_no_vector_selects_fsst_at_or_below_the_distinct_cap` | no vector selects FSST at or below the distinct cap, so skipping its build cannot change output |
| `test_the_block_compression_default_is_zstd` | the block compression default is zstd, pinned to #890 phase 1 |
| `test_its_level_is_3` | and its level is 3 |

## 256. test_read_stream.py: reads through a read stream return what the synchronous path returns

The pytest twin of `test/read_stream.sh` (ported for #1341), every check by the shell's name. Five query shapes over a multi-stripe table are compared with a heap twin, once with `pgcolumnar.enable_read_stream` on and once off. On a private database.

### Every test

Each mode first reads its setting back from a fresh session, since a setting that never took would run both modes down one path. If the setting cannot be applied, the error is the value those two checks compare, so they red by name and the comparisons still run. A comparison is a set hash of each side.

**What the `[off]` arms vouch for.** The read-back checks prove that the setting reached the session. They do not prove which path a read took, and no check here observes the path. With the stream path taken whatever the setting says, all 13 checks pass; with that and the synchronous path copying one byte late, all 13 still pass. So the `[off]` arms vouch for the synchronous path only while the setting routes reads to it.

**On PostgreSQL 15 and 16** the stream path is compiled out, so `[on]` and `[off]` run the same synchronous code. There the `[on]` arms repeat the `[off]` arms.

| test | what it holds |
| --- | --- |
| `test_multiple_stripes_present` | multiple stripes present |
| `test_a_fresh_session_reads_the_setting_on` | [on] a fresh session reads enable_read_stream as on |
| `test_a_fresh_session_reads_the_setting_off` | [off] a fresh session reads enable_read_stream as off |
| `test_a_shape_matches_its_heap_twin` | one test per row of `NAMES` (10 rows), each by the shell's name |

## 257. test_parquet_level_width.py: the Parquet level-width helper is single-sourced, unsigned and bounded

The pytest twin of `test/parquet_level_width.sh` (ported for #1341), every check by the shell's name. A source check, needing no cluster: `pq_bits_for` in `columnar_parquet_format.h` is the one definition of the RLE/bit-pack level width, called from the writer and the reader, with an unsigned shift and a loop bounded below 32 shifts (#710). Read in Python with `re`, independently of the shell's grep and awk.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_shared_helper_exists` | the shared level-width helper exists |
| `test_no_local_copy_survives` | no local copy of the helper survives in the writer or the reader -- read only when both files are, since a count over a missing file is 0 |
| `test_both_callers_reach_the_shared_helper` | and both callers reach the shared one |
| `test_the_shift_is_unsigned` | the shift is unsigned |
| `test_no_signed_shift_remains` | and no signed 1 << remains in it -- read only over a body that was found |
| `test_the_loop_is_bounded` | the loop is bounded below 32 shifts |
| `test_a_non_positive_maximum_returns_first` | a non-positive maximum returns before shifting at all |

## 258. test_parquet_nested.py: Parquet nested export writes lists and groups that read back as the generator's values

The pytest twin of `test/parquet_nested.sh` (ported for #1341), every check by the shell's name. A columnar table with int[], text[] and composite columns, NULL arrays, empty arrays, NULL elements and NULL composites among them, is exported to Parquet; pyarrow reads it back as list/list/struct with every row equal to the generator's. A 70,000-row table spans several row groups and round-trips whole, and a multi-dimensional array is refused. Every test declines by the shell's skip name without pyarrow. On a private database, with the files in `server_dir`.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_nested_parquet_export_writes_every_row` | nested Parquet export rows written |
| `test_the_parquet_fields_are_list_list_struct` | nested Parquet field types |
| `test_every_parquet_value_matches_the_generator` | nested Parquet values match expectation -- every id 1..5000 once, then each row equals the generator's; an empty file used to match |
| `test_the_multi_row_group_export_writes_every_row` | np2 export rows |
| `test_the_multi_row_group_values_match` | np2 values match |
| `test_a_multidimensional_array_is_refused_for_parquet` | reject multi-dim array -- the multi-dimensional refusal itself; any error used to count |

## 259. test_parquet_nested_import.py: nested Parquet import round-trips arrays and composites

The pytest twin of `test/parquet_nested_import.sh` (ported for #1341), every check by the shell's name. A table with int[], text[] and composite columns, NULL arrays, empty arrays, NULL elements and NULL composites among them, is exported to Parquet and imported into a fresh table, which must equal the original as a set, before and after a DELETE; a nested file written by pyarrow must import as the generator's rows. Sets are hashed in SQL as the shell's `pgc_set_hash` does, a failed query reading as a unique `query_error()`. On a private database, with the files in `server_dir`.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_nested_table_exports_every_row` | export rows |
| `test_the_nested_file_imports_every_row` | import rows |
| `test_the_nested_parquet_import_round_trips` | nested import round-trips (arrays + composite) |
| `test_the_parquet_import_count_matches` | import count matches -- compared as numbers, since two failed counts agreed as text |
| `test_a_fresh_round_trip_after_a_delete_matches` | round-trips after delete |
| `test_a_pyarrow_nested_file_imports_every_row` | pyarrow import rows; declines by this name without pyarrow |
| `test_a_pyarrow_nested_file_matches_the_oracle` | pyarrow nested file matches oracle; declines by this name without pyarrow |

## 260. test_parquet_count_bounds.py: file-declared Parquet counts are bounded before use

The pytest twin of `test/parquet_count_bounds.sh` (ported for #1341), every check by the shell's name. With the shell's crafter, which patches the footer counts of an honest pyarrow file, a column chunk's num_values of 2^62 and a row group's num_rows past its values (scalar and LIST) must each be rejected as a malformed row group whose detail names the count, without phantom rows; the honest files import. A survival check passes only when the server answers AND the log's count of killed backends has not moved since setup, since a fresh connection answers again after crash recovery. Every test declines by the shell's skip name without pyarrow. On a private database.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_crafter_produced_the_files` | premise: the crafter produced the seven files |
| `test_the_server_is_up_before_any_malformed_file` | premise: the server is up before any malformed file is read |
| `test_an_honest_file_imports_its_rows` | an honest file imports its rows |
| `test_the_server_is_up_after_the_honest_import` | the server is still up after the honest import |
| `test_a_wrapping_num_values_is_rejected` | num_values 2^62 is rejected, not imported |
| `test_the_backend_survives_the_wrapping_num_values` | and the backend is still up (it did not wrap-allocate and crash) |
| `test_num_rows_past_the_values_is_rejected` | num_rows past the chunk's values is rejected, not emitted as phantom rows |
| `test_num_rows_past_the_values_returns_no_phantom_rows` | and it did not silently return more rows than the file holds -- any count above 8 |
| `test_a_huge_num_rows_is_rejected` | a huge num_rows is rejected rather than walking off the allocation |
| `test_the_backend_survives_the_huge_num_rows` | and the backend survived it |
| `test_the_num_values_rejection_names_num_values` | the num_values rejection names num_values in its detail |
| `test_the_num_rows_rejection_names_num_rows` | the num_rows rejection names num_rows in its detail |
| `test_an_honest_list_imports_one_row` | an honest one-record list imports one row |
| `test_a_valid_three_record_list_imports_three_rows` | a valid three-record list imports three rows (not over-rejected) |
| `test_num_rows_past_a_lists_records_is_rejected` | num_rows past a list's record count is rejected, not emitted as phantom rows |
| `test_num_rows_past_a_lists_records_returns_no_phantom_rows` | and it did not silently return more rows than the file's records -- any count above 1 |
| `test_the_backend_survives_the_list_over_read` | and the backend survived the list over-read |

## 261. test_parallel_degree.py: identical data in heap and columnar is granted the same parallel degree

The pytest twin of `test/parallel_degree.sh` (ported for #1341), every check by the shell's name. The planner sizes a parallel scan from relpages on a log3 ladder; columnar relpages count compressed pages, so the custom scan sizes itself from an uncompressed estimate instead (#451). The same 400,000 rows go into a heap and a columnar table, and both plans must ask for the same number of workers. On a private database, with the planner settings set on it.

### Every test

Every reading must be a measurement: a failed query is a query_error(), and a plan with no Workers Planned line is not a count of 0.

| test | what it holds |
| --- | --- |
| `test_degree_heap_and_columnar_hold_the_same_rows` | both tables hold the same rows |
| `test_degree_columnar_is_smaller_on_disk` | premise: the columnar table really is smaller on disk |
| `test_degree_the_heap_plan_is_parallel` | premise: the heap plan is parallel |
| `test_degree_the_columnar_plan_is_parallel` | the columnar plan is parallel |
| `test_degree_the_worker_cap_decides_neither_arm` | the worker cap is not what decides either arm |
| `test_degree_identical_data_gets_the_same_workers` | identical data is granted the same parallel degree |

## 262. test_parallel_flush_optin.py: parallel_flush stays opt-in, and the dispatch metric cannot separate the shapes

The pytest twin of `test/parallel_flush_optin.sh` (ported for #1341), every check by the shell's name. `pgcolumnar.parallel_flush` stays off by default (#445): a default flush dispatches serial; with it on, a wide-numeric flush dispatches parallel, workers produce its columns, and the stored chunks and rows equal the serial path's; and a random and a constant bigint table give the same dispatch metric, so no gate over that metric can separate them. The DEBUG1 lines are read as notices on the inserting session. On a private database.

### Every test

The ON check reads the split line ("N of M columns by K worker(s)") as well as the dispatch line, so a flush where no worker produced a column is not parallel. The eight wnum columns have eight different shapes, and the fingerprint adds the rows read back.

| test | what it holds |
| --- | --- |
| `test_optin_parallel_flush_is_off_by_default` | parallel_flush is off by default |
| `test_optin_a_default_flush_dispatches_serial` | a default flush dispatches serial |
| `test_optin_the_guc_on_flushes_in_workers` | with the GUC on, a wide-numeric flush dispatches parallel (not a silent serial fall-back) |
| `test_optin_stores_the_serial_paths_chunks` | opting in stores byte-identical chunks to the serial path |
| `test_optin_the_dispatch_metric_cannot_separate_the_shapes` | the dispatch metric is byte-identical for a random and a constant column (the metric cannot separate opposite-cost shapes) |

## 263. test_parquet_export_stats.py: the Parquet files we export carry correct per-row-group statistics

The pytest twin of `test/parquet_export_stats.sh` (ported for #1341), every check by the shell's name: 162 tests, three of the functions parametrized over a literal table of the shell's names. A 196609-row table is exported in four row groups and `test/parquet_stats.py` reads the footer with no Parquet library. The bounds, null counts, float rules (#850), column orders and the FDW's row-group skipping on the exported file are asserted. On a private database; the exported files live in `server_dir`.

### Every test

The bound and null-count oracles read the heap built from the generator, not the exported file, so a fold the export applies to both its data and its statistics is red. Each absence check reads "N of M", where M is how many chunks or bounds it examined. The two pyarrow controls decline under their own names when pyarrow is absent.

| test | what it holds |
| --- | --- |
| `test_export_stats_leaf_indexes_resolve` | premise: leaf indexes resolved for all ten columns |
| `test_export_stats_file_is_one_we_wrote` | premise: the file under test is one we wrote |
| `test_export_stats_footer_declares_four_row_groups` | premise: the footer declares 4 row groups |
| `test_export_stats_footer_declares_ten_leaves` | premise: the footer declares 10 leaf columns |
| `test_export_stats_parser_reaches_column_metadata` | premise: the parser reaches ColumnMetaData (num_values sums to the table) |
| `test_export_stats_oracle_buckets_are_row_groups` | the oracle's buckets are the file's row groups |
| `test_export_stats_buckets_hold_contiguous_ids` | each bucket holds its own contiguous id range |
| `test_export_stats_oracle_holds_every_row` | the oracle holds every exported row |
| `test_export_stats_exported_data_equals_the_heap` | the exported data equals the heap on the columns nothing folds |
| `test_export_stats_every_chunk_carries_a_minimum` | every value-bearing skippable chunk carries a minimum |
| `test_export_stats_every_chunk_carries_a_maximum` | every value-bearing skippable chunk carries a maximum |
| `test_export_stats_no_chunk_is_one_sided` | no chunk carries exactly one bound |
| `test_export_stats_buckets_account_for_every_chunk` | buckets account for every skippable-type chunk |
| `test_export_stats_all_null_column_has_no_bounds` | the all-null column carries no bounds |
| `test_export_stats_bounds_are_physical_width` | every bound is its physical width |
| `test_export_stats_group_bound_equals_the_heap` | group G min(C) / max(C) equals the oracle: 56 tests, one per bound of id, i2, i8, f4, d, ts and nul in each of the four row groups, against the heap |
| `test_export_stats_four_distinct_id_minima` | the four groups report four distinct id minima |
| `test_export_stats_min_le_max` | min <= max in every chunk carrying both |
| `test_export_stats_null_count_always_written` | null_count is written for every chunk, including where it is zero |
| `test_export_stats_group_null_count_equals_the_heap` | group G null_count(C) equals the oracle: 20 tests, one per row group of nul, d, ts, alln and id, against the heap with infinities counted as nulls |
| `test_export_stats_fixture_has_infinite_dates` | premise: the fixture's infinite dates are more than none |
| `test_export_stats_fixture_has_infinite_timestamps` | premise: the fixture's infinite timestamps are more than none |
| `test_export_stats_no_infinity_reaches_a_bound` | no date or timestamp bound is an infinity sentinel |
| `test_export_stats_nan_column_keeps_its_bounds` | a float column holding a NaN still carries both bounds |
| `test_export_stats_nan_column_minimum` | min(f8a) is the smallest non-NaN value |
| `test_export_stats_nan_column_maximum` | max(f8a) is the largest non-NaN value |
| `test_export_stats_all_nan_column_has_no_minimum` | an all-NaN column carries no minimum |
| `test_export_stats_all_nan_column_has_no_maximum` | an all-NaN column carries no maximum |
| `test_export_stats_nan_count_always_written` | nan_count is written for every float chunk |
| `test_export_stats_nan_count_counts` | nan_count counts the NaNs |
| `test_export_stats_no_nan_bound` | no float bound is a NaN bit pattern |
| `test_export_stats_zero_maximum_is_positive` | a zero maximum is written as +0.0 |
| `test_export_stats_zero_minimum_is_negative` | a zero minimum is written as -0.0 |
| `test_export_stats_one_column_order_per_leaf` | the footer carries one ColumnOrder per leaf column |
| `test_export_stats_column_orders_are_type_order` | every ColumnOrder is TYPE_ORDER |
| `test_export_stats_no_deprecated_minimum` | no chunk carries the deprecated minimum (field 2) |
| `test_export_stats_no_deprecated_maximum` | no chunk carries the deprecated maximum (field 1) |
| `test_export_stats_byte_array_has_no_bounds` | the BYTE_ARRAY column carries no bounds |
| `test_export_stats_boolean_has_no_bounds` | the BOOLEAN column carries no bounds |
| `test_export_stats_fdw_reports_four_groups` | premise: the FDW reports 4 row groups |
| `test_export_stats_unrestricted_scan_skips_nothing` | an unrestricted scan skips nothing |
| `test_export_stats_one_group_predicate_skips_three` | a predicate confined to one group skips the other three |
| `test_export_stats_bigint_predicate_skips` | a bigint predicate skips |
| `test_export_stats_float_predicate_skips` | a float predicate skips |
| `test_export_stats_date_predicate_skips` | a date predicate skips |
| `test_export_stats_timestamp_predicate_skips` | a timestamp predicate skips |
| `test_export_stats_matching_constant_type_skips` | control: the same column skips when the constant matches its type |
| `test_export_stats_cross_type_constant_skips_nothing` | a cross-type constant skips nothing, on the identical predicate |
| `test_export_stats_float_less_than_skips` | control: a float < predicate below every bound skips |
| `test_export_stats_float_greater_equal_skips_nothing` | a float >= predicate above every bound still skips nothing |
| `test_export_stats_quoted_bigint_literal_skips` | a quoted literal takes a bigint column's type and skips |
| `test_export_stats_unquoted_bigint_literal_skips_nothing` | the same digits unquoted are an integer, and skip nothing |
| `test_export_stats_wide_digits_skip_a_bigint` | digits too wide for an integer are a bigint, and skip |
| `test_export_stats_decimal_literal_casts_the_column` | a decimal-point literal casts the column, not the constant |
| `test_export_stats_quoted_literal_casts_the_constant` | control: the quoted form casts the constant to the column's type |
| `test_export_stats_quoted_real_literal_skips` | a quoted literal takes a real column's type and skips |
| `test_export_stats_unquoted_real_literal_skips_nothing` | the same literal unquoted is double precision, and skips nothing |
| `test_export_stats_quoted_smallint_literal_skips` | a quoted literal takes a smallint column's type and skips |
| `test_export_stats_unquoted_smallint_literal_skips_nothing` | the same digits unquoted are an integer, and skip nothing (int2) |
| `test_export_stats_adorned_date_skips` | an adorned literal of the column's own type skips |
| `test_export_stats_unadorned_date_skips` | the same date unadorned is unknown, takes the column's type and skips |
| `test_export_stats_timestamp_adorned_date_skips_nothing` | the same date adorned as a timestamp does not skip |
| `test_export_stats_adorned_literal_keeps_its_type` | an adorned literal keeps its own type, not the column's |
| `test_export_stats_unadorned_literal_takes_the_column_type` | control: the unadorned form resolves to the column's type instead |
| `test_export_stats_integer_digits_prune_every_group` | control: a digit string that fits an integer prunes every group |
| `test_export_stats_int4_column_refuses_wide_digits` | digits too wide for an integer are a bigint, so an int4 column refuses |
| `test_export_stats_fdw_result_equals_the_oracle` | the FDW result equals the oracle for [P]: 20 tests, one per predicate the skip checks use |
| `test_export_stats_parser_finds_pyarrow_bounds` | control: the parser finds pyarrow's bounds |
| `test_export_stats_parser_reports_deprecated_fields` | control: the parser reports the deprecated fields where they exist |

## 264. test_catalog_filter_reads.py: stats() and sort_status() read a table's storage id once

The pytest twin of `test/catalog_filter_reads.sh` (#1637), every check by the shell's name. With 20 other columnar tables of 5 row groups each and statistics on the catalog, `pgcolumnar.stats('t')` and `pgcolumnar.sort_status('t')` each read at most 5 blocks of `t`, counted by `pg_stat_get_xact_blocks_fetched` inside the call's own transaction in a fresh session. Before #1637 both filtered a shared catalog on `get_storage_id(rel)`, which a sequential scan calls once per catalog row: 104 and 170 blocks. On a private database.

### Every test

| test | what it holds |
| --- | --- |
| `test_the_catalog_holds_other_tables_row_groups` | premise: other tables put at least 100 row groups in the catalog |
| `test_the_catalogs_have_statistics` | premise: pgcolumnar.row_group and pgcolumnar.storage have statistics -- the plan that scans is the one a database with current statistics gets |
| `test_the_counter_counts_one_call` | premise: one get_storage_id call reads t -- without it, a counter that always read 0 would pass both bounds |
| `test_stats_reads_the_storage_id_once` | stats() reads the storage id once, not once per catalog row group |
| `test_stats_reports_only_the_tables_groups` | control: stats() reports t's 3 row groups and no other table's |
| `test_sort_status_reads_the_storage_id_once` | sort_status() reads the storage id once, not once per catalog row |
| `test_sort_status_counts_only_the_tables_groups` | control: sort_status() counts t's 3 row groups and no other table's |

## 265. test_iceberg_malformed.py: the Iceberg reader refuses crafted metadata cleanly, never crashing or hanging

The twin of `test/iceberg_malformed.sh` (#388 phase 4a, #644). The hand-crafted fixtures in `test/fixtures/iceberg/warehouse_malformed` are refused with the refusal each arm names, not merely its SQLSTATE: an array-valued Avro `fields` element, a NULL data-file path in the lister and the scan, a NULL manifest_path, a FIFO at every Iceberg-reachable open (a HANG after 5s), a null manifest sequence number an added entry must inherit, and a dangling current-schema-id. Null manifest-list sequence numbers decode as NULL, and every refusal has a control that still reads.

### Every test

One module fixture copies the fixtures into `server_dir`, builds the FIFOs there, and records every reading from a private database. A FIFO read runs in a thread with a 5s deadline. Each survival reading compares the server log's crash count with the one taken just before its arm, and waits out a crash recovery. The first two tests mirror the shell's two skip names.

| test | what it holds |
| --- | --- |
| `test_the_malformed_iceberg_fixtures_are_present` | premise: the committed malformed fixtures are not empty |
| `test_python_reads_the_malformed_metadata` | premise: the dangling-schema metadata parses as JSON |
| `test_an_array_valued_fields_element_is_refused` | an array-valued schema fields element is refused, not crashed (XX001) |
| `test_the_backend_survives_the_malformed_schema` | backend still up after the malformed-schema refusal |
| `test_a_well_formed_field_object_still_decodes` | control: a well-formed field object still decodes (0 entries) |
| `test_the_lister_refuses_a_null_data_file_path` | a NULL data-file path is refused in iceberg_data_files (XX001, not a crash) |
| `test_the_backend_survives_the_lister_refusal` | backend still up after the lister refusal |
| `test_the_scan_refuses_a_null_data_file_path` | a NULL data-file path is refused in iceberg_scan (XX001, not a crash) |
| `test_the_backend_survives_the_scan_refusal` | backend still up after the scan refusal |
| `test_a_present_file_path_lists_one_data_file` | control: a present file_path lists one data file |
| `test_a_null_manifest_path_is_refused` | a NULL manifest_path is refused, not a crash (XX001) |
| `test_the_backend_survives_the_null_manifest_path` | backend still up after the null-manifest_path refusal |
| `test_iceberg_current_snapshot_refuses_a_fifo` | iceberg_current_snapshot on a FIFO is refused, not a hang (XX001) |
| `test_iceberg_data_files_refuses_a_fifo` | iceberg_data_files on a FIFO is refused, not a hang (XX001) |
| `test_iceberg_scan_refuses_a_fifo` | iceberg_scan on a FIFO is refused, not a hang (XX001) |
| `test_read_avro_manifest_refuses_a_fifo_as_iceberg_input` | read_avro_manifest on a FIFO is refused, not a hang (XX001) |
| `test_read_manifest_list_refuses_a_fifo` | read_manifest_list on a FIFO is refused, not a hang (XX001) |
| `test_read_parquet_refuses_a_fifo_beside_iceberg` | read_parquet on a FIFO is refused, not a hang (XX001) |
| `test_the_backend_survives_the_fifo_refusals` | backend still up after the FIFO refusals |
| `test_a_manifest_list_that_is_a_fifo_is_refused` | a manifest-list that is a FIFO is refused, not a hang (XX001) |
| `test_the_backend_survives_the_manifest_list_fifo` | backend still up after the manifest-list FIFO refusal |
| `test_a_null_manifest_list_sequence_number_reads_null` | a null manifest-list sequence_number decodes as NULL, not 0 |
| `test_a_present_manifest_list_sequence_number_reads_its_value` | control: a present manifest-list sequence_number decodes to its value |
| `test_a_null_min_sequence_number_reads_null` | a null manifest-list min_sequence_number decodes as NULL, not 0 (#686) |
| `test_a_present_min_sequence_number_reads_its_value` | control: a present min_sequence_number decodes to its value (#686) |
| `test_the_scan_refuses_a_null_sequence_an_added_entry_inherits` | iceberg_scan refuses a null manifest seq an ADDED entry must inherit (XX001, not a silent wrong row) |
| `test_the_backend_survives_the_null_sequence_refusal` | backend still up after the null-sequence refusal |
| `test_a_concrete_manifest_sequence_scans_every_row` | control: a concrete manifest seq scans all 5 rows (seq-3 delete is older than seq-5 data) |
| `test_a_dangling_current_schema_id_is_refused` | a dangling current-schema-id is refused (XX001), not silently stale-read |
| `test_the_backend_survives_the_dangling_schema_refusal` | backend still up after the dangling-schema refusal |
| `test_a_resolvable_current_schema_id_reads_amount` | control: a resolvable current-schema-id reads the correct amount column |
| `test_a_legacy_only_schema_table_still_reads` | control: a legacy-only-schema table still reads (5 rows) |
| `test_the_legacy_table_binds_amount` | control: the legacy table binds amount correctly (10..50) |

## 266. test_iceberg_name_mapping.py: an Iceberg data file with no field ids binds by name mapping

The twin of `test/iceberg_name_mapping.sh` (#388). An id-less data file (an imported dataset) is read by binding its columns by name through `schema.name-mapping.default`: every row against the fixture's own rows, a renamed schema column, an alias, projection, refusals (no mapping, a duplicate name, a field id beyond int32, one name over the cap) by SQLSTATE, null-fill for unmapped and later-added columns, and the cap's opening allocation by a source read with comments blanked out.

### Every test

One check per test, each by the shell's name, every reading a value from a module fixture on a private database. A refusal reads as its SQLSTATE. A survival check also wants the server log's count of killed backends unchanged since its section began, and waits out crash recovery before the next section reads.

| test | what it holds |
| --- | --- |
| `test_name_mapping_data_file_carries_no_field_ids` | premise: the data file carries no field ids |
| `test_name_mapping_reads_an_id_less_file` | an id-less data file reads by name mapping (all 5 rows) |
| `test_name_mapping_values_are_correct` | the mapped values are correct |
| `test_name_mapping_renamed_schema_column` | a renamed schema column still reads via the mapping name |
| `test_name_mapping_alias_resolves` | an alias name in the mapping resolves the file column |
| `test_name_mapping_under_projection` | name mapping applies under column projection (id, amount) |
| `test_name_mapping_backend_up_after_reads` | backend still up after the name-mapping reads |
| `test_name_mapping_no_mapping_is_refused` | an id-less file with no name mapping is refused (22023) |
| `test_name_mapping_refusal_names_the_property` | ...and the refusal names schema.name-mapping.default |
| `test_name_mapping_duplicate_name_is_refused` | a duplicate name in the mapping is refused (XX001) |
| `test_name_mapping_big_field_id_is_refused` | a mapping field-id beyond int32 is refused (XX001) |
| `test_name_mapping_backend_up_after_refusals` | backend still up after the name-mapping refusals |
| `test_name_mapping_unmapped_column_reads_null` | a projected column absent from the mapping reads null, not an error |
| `test_name_mapping_unmapped_null_mapped_real` | the unmapped column is null and the mapped columns are real |
| `test_name_mapping_added_column_reads_null` | a column added after the file was written reads null (schema evolution) |
| `test_name_mapping_evolved_columns_are_real` | ...and its other columns still carry real data |
| `test_name_mapping_backend_up_after_null_fill` | backend still up after the null-fill reads |
| `test_name_mapping_at_the_cap_is_accepted` | a mapping with exactly the cap in names is accepted |
| `test_name_mapping_over_the_cap_is_refused` | one name over the cap is refused |
| `test_name_mapping_cap_refusal_names_the_cap` | ...and the refusal names the cap |
| `test_name_mapping_opening_capacity_is_bounded` | the opening capacity is bounded by the cap, not by the declared count |
| `test_name_mapping_backend_up_after_cap` | backend still up after the cap reads |

## 267. test_iceberg_objstore.py: Iceberg tables read over object storage

The twin of `test/iceberg_objstore.sh` (#388 phase 6). The committed delete and name-mapping warehouses are served over S3 by the shell's own SigV4 fixture server, and read exactly as the filesystem suites read them: position deletes, equality deletes and deletion vectors against the same oracle, and a name-mapped id-less file. A read with no AWS environment is refused 28000; recorded delete paths that leave the table location (outside it, a literal `..`, a percent-encoded `..`, and one behind an encoded NUL) are refused 22023, never fetched; and an endpoint off `pgcolumnar.objstore_allowed_endpoints` is refused 42501 by the allow-list's own message.

### Every test

One check per test, each by the shell's name, every reading a value from a module fixture on a private cluster, restarted with AWS_* in the postmaster environment as the shell does. A refusal reads as its SQLSTATE, the allow-list's with its message. A survival check also wants the server log's count of killed backends unchanged since the reads began.

| test | what it holds |
| --- | --- |
| `test_the_iceberg_objstore_fixtures_are_present` | premise: sigv4 fixture server is up |
| `test_an_s3_iceberg_read_with_no_aws_environment_is_refused` | config: an s3 iceberg read with no AWS environment is refused (28000) |
| `test_position_deletes_read_over_s3` | position deletes read over s3 (surviving == filesystem oracle) |
| `test_the_position_deleted_ids_are_gone_over_s3` | the position-deleted ids are gone over s3 |
| `test_equality_deletes_read_over_s3` | equality deletes read over s3 (survivors == oracle) |
| `test_deletion_vectors_read_over_s3` | deletion vectors read over s3 (survivors == oracle) |
| `test_a_scan_whose_delete_does_not_apply_reads_all_rows_over_s3` | a scan whose delete does not apply reads all rows over s3 |
| `test_name_mapping_reads_an_id_less_file_over_s3` | name mapping reads an id-less file over s3 (5 rows) |
| `test_the_backend_is_still_up_after_the_s3_reads` | backend still up after the s3 reads |
| `test_a_delete_path_outside_the_table_location_is_refused_over_s3` | a delete path outside the table location is refused over s3 (22023) |
| `test_a_dotdot_escaping_delete_path_is_refused_over_s3` | a dotdot-escaping delete path is refused over s3 (22023) |
| `test_a_percent_encoded_dotdot_delete_path_is_refused_over_s3` | a percent-encoded dotdot delete path is refused over s3 (22023) |
| `test_a_percent_encoded_dotdot_behind_an_encoded_nul_is_refused_over_s3` | a percent-encoded dotdot behind an encoded NUL is refused over s3 (22023) |
| `test_the_backend_is_still_up_after_the_s3_refusal` | backend still up after the s3 refusal |
| `test_an_endpoint_not_on_the_allow_list_is_refused` | an endpoint not on objstore_allowed_endpoints is refused |
| `test_the_backend_is_still_up_after_the_endpoint_refusal` | backend still up after the endpoint refusal |
