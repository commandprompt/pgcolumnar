# Changelog

All notable changes to pgColumnar are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). pgColumnar is
pre-release; the version marker is `1.0-alpha3`, recorded in `VERSION`. New tables
are written in the native on-disk format, PGCN v1. For the forward-looking plan see
[design/ROADMAP.md](design/ROADMAP.md); for full history see the git log.

The extension's `default_version` is `1.0-alpha3`, which is in development and not
yet tagged; the latest published pre-release is `v1.0-alpha2`. Upgrade scripts from
every previously shipped version ship with it (`1.0-dev`, which the v1.0-alpha tag
installed, `1.0-alpha`, and `1.0-alpha2`), so a single
`ALTER EXTENSION pgcolumnar UPDATE` reaches `1.0-alpha3` from any of them. Older
notes in this file describe `default_version` as pinned at an earlier version, each
true until the next version shipped.

## [Unreleased]

### Added

- `MERGE` is documented as working, which it has been all along. It needs no
  index on the columnar target and takes every arm, including
  `WHEN MATCHED ... DELETE`, `WHEN NOT MATCHED BY SOURCE`, and
  `RETURNING merge_action()`. A columnar table can be the source as well as the
  target. Verified rather than assumed, on PostgreSQL 17.10, in four shapes.

  Nothing in the code changed. The gap was that a reader had no way to learn
  this: `MERGE` appeared in no user-facing document, and its absence from
  `docs/limitations.md` reads as easily as "unsupported" as "supported".

  `docs/features.md` also records what a `MERGE` costs, because that is the part
  a reader acts on: its updates and deletes mark rows rather than rewriting row
  groups, exactly as a plain `UPDATE` or `DELETE` does, so space returns only
  after `pgcolumnar.vacuum`.

### Fixed

- `UPDATE` now fans the new row version out to every covering projection, so a
  projection scan stops answering as if the updated rows were gone.

  **A projection stored the base row number, and `UPDATE` never gave it the new
  one.** A projection joins back to the base table on the row number and takes
  visibility from the base delete vector, so `DELETE` needs no rewrite: the
  vector hides the old number from the projection too. `UPDATE` is delete-old
  plus insert-new, and only the delete half reached the projection. The
  projection kept the old number, the delete vector hid it, and the new number
  was nowhere. `read_projection` returned no rows, and a covering projection
  scan (projected columns with a sort-key qual) answered as if the updated rows
  had been removed.

  The base table was always correct. The loss was confined to reads the planner
  served from a projection, which makes it the worse shape: the same query
  returns different answers depending on whether the projection is chosen.

  `tuple_update` now calls the same fan-out that insert calls.

  `docs/features.md`, `docs/administration.md` and `docs/how-to.md` said only
  that inserts write projections. All three now say updates do too, and say why
  a delete does not.
- Arrow import reads the temporal unit and carrier width the file declares,
  rather than assuming the ones our own exporter writes (#864, #865).

  **A file could say what it meant and be read as something else.** The importer
  built its decode plan from the target column type alone and never opened the
  Arrow `Field` table, so the unit went unread. A `date64` column holding
  2000-01-01 was decoded as a day count and stored as `4908285-05-04`; a
  `timestamp` in seconds became `1970-01-01 00:15:46.6848`, in milliseconds
  `1970-01-11 22:58:04.8`, and in nanoseconds `31969-04-01`. A `time64` in
  nanoseconds holding noon stored `12000:00:00`, a legal PostgreSQL time. None
  raised an error. `time32` could not be imported at all, failing with "value
  buffer too small for the row count".

  Only microsecond timestamps and times, and `date32` dates, were read
  correctly, and those are exactly what `export_arrow` writes -- so a round trip
  through our own exporter never showed the defect.

  The import now reads `Date.unit`, `Time.unit`, `Time.bitWidth` and
  `Timestamp.unit` from the file and scales to PostgreSQL's units, treating an
  absent field as its FlatBuffers default. That last part matters: a writer omits
  any field equal to its default, and two of these defaults are not zero, so
  pyarrow emits `date64` and `time32[ms]` with no unit field at all. Reading an
  absent field as zero is what produced the `date64` result above.

  Scaling a coarse unit up can leave PostgreSQL's range, so each conversion is
  overflow-checked and refused with `22008` rather than wrapped. Nanoseconds are
  narrowed to microseconds, which PostgreSQL cannot store beyond: narrowing keeps
  the instant, where reading nanoseconds as microseconds is wrong by a factor of
  1000. Every narrowing floors, so an instant before the epoch reports the day and
  the microsecond it falls in rather than the one after.

  The unit is applied to nested fields too, so a `timestamp[]` or a composite
  with a temporal member is read by its own declared unit.

  Reading the file's declared type also closed a third case, found while fixing
  these two and present since Arrow import shipped: a temporal file whose type
  the target column cannot hold was read anyway, taking the low four bytes of an
  eight-byte carrier. A `time64` file imported into a `date` column stored
  `687342-02-27`, and a `timestamp` file into the same column stored
  `2722128-09-17`. Both are now refused with `42804`, naming the Arrow type and
  the column type. A file whose type is not temporal is unaffected, so importing
  a plain `int64` file into a `timestamp` column still works.

- A shebang and the execute bit go together, and every directory that documents
  a command is swept (#856). Two things were left over from #852.

  **The rule flagged both states, so a sourced fragment could not be correct.**
  It reddened a file that declared an interpreter without the bit, and it also
  reddened a file with no interpreter line at all. Its header documented the way
  out as "drop its shebang and say why", and that way out did not exist: dropping
  the shebang moved the file from one red to the other. `bench/cb_guards.sh` is
  the file that proved it -- sourced by `bench/run_clickbench.sh` and
  `test/bench_guards.sh`, its header saying "Sourced, not executed" since it was
  written, and unfixable under the old rule. `CONTEXT.md` had described the rule
  correctly all along, as failing "if one has either without the other"; it was
  the code that was stricter than the documented rule.

  The rule is now that biconditional, and a file with neither a shebang nor the
  bit passes.

  **That removed the exemptions.** `test/selftest/` and `test/fixtures/` were
  pruned by path because the old rule would have reddened them wholesale.
  Measured under the new one before the prune came out: `selftest/` is 31 scripts
  and every one is already correct, `fixtures/` is 14 `.sh`/`.py` of which 13 are
  the host tools this change gives the bit. Nothing is excluded now, so nothing
  is concealed.

  **`bench/` joins the population.** `docs/benchmarks.md` names five `bench/`
  scripts as bare commands. All five are executable today, so all five work:
  correct by habit with nothing checking it, which is what `test/` was before
  #852.

  **A third check, anchored on the documents, closes the hole the biconditional
  opens.** Delete a documented command's shebang and its bit and the file is
  internally consistent and still broken for a reader, so neither of the first
  two rules can see it. The third requires every script a document names to be
  executable.

- No compiled Python artifact is tracked, and the tree ignores the ones the
  interpreter writes (#854). `test/__pycache__/ste_check.cpython-312.pyc` was
  tracked. Its source, `test/ste_check.py`, was renamed to
  `test/plain_language_check.py` in `e9de048`; the `.pyc` outlived it by 146
  commits and was still tracked at `fe1f3a2`, the base of this change. So the
  file was compiled code for a module that no longer existed and that CPython
  would never open: it reads a `__pycache__` entry only when the matching source
  sits beside it.

  A tracked build artifact does not stay still. This one had already re-committed
  itself inside an unrelated logical-replication fix, where the diffstat reads
  `Bin 6028 -> 6028 bytes` and exactly two bytes differ -- the PEP 552
  source-timestamp word. The compiled code was identical either side.

  `.gitignore` gains `__pycache__/` and `*.pyc`, which it had neither of, and
  `test/selftest/` gains a part that fails when a compiled Python artifact is
  tracked or when either rule goes missing. Ignoring is not enough on its own and
  the suite proves it: restoring the tracked file with both rules in place still
  reds two checks, because `.gitignore` has no effect on a file git already
  tracks.

  `test/devloop.sh` stages its build directory with `tar --exclude=.git`, so the
  tree the suites run out of was not a checkout and the new checks reported five
  failures on a clean tree. It now writes a one-line gitfile into the build
  directory instead: 45 bytes rather than the 32 MB of copying the object
  database, and the loop stays as cheap as it was. Every check in the part also
  answers `no-repo` where there is no repository, rather than the answer git
  gives by default -- `ls-files` prints nothing, which reads as a clean tree, and
  `check-ignore` says "not ignored", which reports a present rule as missing.

- Every test script is executable, so the commands this project documents run as
  written (#852). No released version is affected: nothing in the extension
  changed, and this is test tooling only.

  103 of the 262 top-level scripts in `test/` were mode 100644, and 30 documented
  invocations named one of them. `test/temporal.sh /path/to/pg_config` and the 29
  others died with `Permission denied` before running a single statement. The
  fix gives those 103 files the execute bit.

  Every green run in this project's history is honest, and the reason is the
  point. `ci.yml:485` and `nightly.yml:188` both invoke
  `bash test/run_all_versions.sh`, and the runner starts each suite as
  `bash "$builddir/test/${s}.sh"` at lines 712 and 749. An interpreter named on
  the command line does not consult the execute bit, so a suite's own mode never
  reached the matrix. The gap was between the documentation and a reader's shell,
  and no green matrix could stand in it.

  Two situations, not one. Exactly one 100755 to 100644 transition exists in the
  whole history of `test/`: `56ae5f8eb`, a perf commit that added a line to
  `SUITES` and stripped the bit on the way past. The other 102 files were born
  100644, so one is a regression and the rest are a habit nothing contradicted.

  `test/crlf_listener.py` was the only script with no interpreter line, and the
  bit alone would have made it worse: `execve` returns `ENOEXEC`, the shell
  retries the file under `/bin/sh`, and the reader gets a syntax error instead of
  a clean refusal. It now declares `#!/usr/bin/env python3`, as its eleven
  siblings do.

  A new `harness_selftest` part pins both halves for every `.sh` and `.py` under
  `test/`. The rule is anchored on the file's own first line rather than
  on a name list or on what a document happens to mention: a script that opens
  `#!/usr/bin/env bash` has said it is meant to be run, and a mode that forbids
  running it contradicts the file itself. Run against the unfixed tree the check
  reports 102 files without the bit and 1 without a shebang, which is the
  population the issue measured, so the check is neither over nor under matching.
  Stripping the bit from `run_all_versions.sh` alone, which is what `56ae5f8eb`
  did, reddens it naming that one file.

  The sweep exempts exactly two directories. The parts in `test/selftest/` and
  the scripts under `test/fixtures/` are sourced or imported, never executed, so
  the bit would advertise a way to run them that does not work. That is this same
  defect pointing the other way. Every other directory is swept, which is what
  covers `test/pbt/run.sh`: it is a documented command (`docs/testing.md:132`)
  that lives one level down, it was already correct, and a sweep of the top level
  alone would have left the file most like this defect outside the guard.

- `docs/limitations.md` states the constant-typing rule correctly. The previous
  wording said `smallint` and `real` "always need a cast". That is false: an
  unadorned quoted literal is `unknown` and takes the column's type, so
  `smallint_col < '500'` skips. The rule is not special to the temporal types
  either. Their literals are simply always quoted, which is why they skip as
  written.

  Two limits on that rule are stated with it, because the first revision of this
  entry got both wrong. A literal that names its own type is that type rather
  than `unknown`, so `timestamp_col < DATE '2026-01-01'` does not skip, which is
  what the same page's conditions list has always said. And a digit string is an
  `integer` only while the value fits in one: `integer_col < 5000000000` is an
  `integer` column against a `bigint` constant and does not skip either.

  Measured across the nine skippable types in three literal forms, with the
  planner's own constant printed beside each skip count, and every row count
  compared with a heap oracle. Nine checks in `test/parquet_export_stats.sh` now
  pin the rule, and each can fail: three go red when the reader's
  exact-constant-type refusal is deleted, four when the writer stops emitting
  statistics, and the two plan-shape checks go red when the patterns they look
  for are swapped.

- Exported Parquet files carry row-group statistics, so a file pgColumnar wrote
  can be skipped (#850). Reported from outside the project, and the report was
  right: every condition `docs/limitations.md` documents for row-group skipping
  could hold and `Row Groups Skipped` would still be 0.

  `write_column_chunk()` emitted ColumnMetaData fields 1 through 7 and 9, and
  never field 12, so no file we wrote carried a minimum or a maximum for the
  reader to test a predicate against. The read side was never at fault. On the
  reporter's own repro, a 1,000,000-row table exported to 16 row groups: 0 of 32
  column chunks carried statistics, and the foreign scan decoded all 16 groups
  for `id < 5000`. It now carries 32 of 32 and skips 15 of 16, decoding one.
  The native chunk-group skip on the same data always worked, and is unchanged.

  What the exporter now writes, per column chunk: `null_count` always, including
  where it is zero, because `parquet.thrift` says a reader must not read an
  absent count as zero; `min_value` and `max_value` for the columns stored as
  INT32, INT64, FLOAT or DOUBLE, which is exactly the set the reader can skip
  on; and `nan_count` for the floating point columns. A `text`, `bytea`, `uuid`,
  `boolean` or byte-array `numeric` column gets the null count and no bounds:
  UTF8 sorts by unsigned bytes, which is not any PostgreSQL collation, and a
  bound in the wrong order is worse than no bound at all.

  The file footer now also carries `column_orders`, one `TYPE_ORDER` per leaf
  column. It is not decoration. `parquet.thrift` states that without it the
  meaning of `min_value` and `max_value` is undefined, and Arrow acts on that by
  discarding both, so statistics without `column_orders` would have left the file
  exactly as unskippable under pyarrow as it was before. With it, pyarrow reports
  `is_stats_set=True` on our files.

  Three rules from the specification are followed for floats, and each is pinned
  by a test: a NaN never becomes a bound, a column whose non-null values are all
  NaN gets no bounds at all, and a computed zero is written `-0.0` as a minimum
  and `+0.0` as a maximum. A value with no Parquet representation, such as an
  infinite date or timestamp, is folded to null before the accumulator sees it,
  so it counts as a null and cannot reach a bound.

  `test/parquet_export_stats.sh` covers this, reading the footer bytes with
  `test/parquet_stats.py` rather than asking a third-party library whether it
  feels like surfacing them. Eight mutations of the fix were each run against the
  suite and each turned a named check red.

  One of the eight loses rows rather than merely losing the skip: taking a date
  bound from the PostgreSQL epoch instead of the Unix one. That shift leaves a
  well-ordered interval which is simply wrong, so no guard can see it, and the
  single check standing between it and silent row loss is a predicate placed
  past the end of the data, `d > DATE '2060-01-01'`.

  Swapping the two bounds does not lose rows, which is worth knowing rather than
  glossing: it inverts the interval, and the reader already refuses to skip on an
  inverted one. `docs/limitations.md` documents that refusal, and it caught this
  mutation. The swap loses 54 checks and no rows.

  Files exported by 1.0-alpha2 or earlier carry no statistics, and nothing
  rewrites them in place. Export again to make one skippable.


- Index entries for live rows are no longer destroyed (#838). A released version
  is affected: this is present in `v1.0-alpha2`.

  `pgcolumnar_index_delete_tuples` answered nbtree from the calling backend's MVCC
  snapshot. `table_index_delete_tuples` asks a different question, whether an entry
  is dead to everyone, which heapam answers from a global visibility test. nbtree
  acts on the answer in `_bt_delitems_delete_check` by removing the items from the
  leaf page physically, inside a critical section, and nothing restores them. A row
  the current uncommitted transaction had deleted read as not live while remaining
  live to every other session, so entries for live rows were erased.

  Measured on 400 live rows given twelve committed rounds of churn on a column that
  is not indexed: an index scan reached 0 of the 400 after a `ROLLBACK` and 228 of
  400 after a `COMMIT`, while `SELECT count(*)` at shipped defaults answered 228
  through an index only scan and `SELECT count(b)` answered 400 through the columnar
  scan, on the same table in the same session. Two controls place the fault: churning
  the indexed column instead makes `index_unchanged_by_update()` false so no
  bottom up pass runs and nothing is lost, and a heap given the same hint loses
  nothing.

  Native storage carries no per row transaction id to compare against a global
  horizon, because the delete vector is a bitmap whose visibility comes from the
  MVCC catalog rows that hold it. It therefore cannot prove global deadness and now
  vouches for nothing rather than guessing. The cost is that version churn no longer
  reclaims leaf page space, which `docs/limitations.md` already described: stale
  entries are reclaimed by `REINDEX`, not removed opportunistically.

- A scrollable cursor no longer answers a backward fetch with forward rows (#842).
  A released version is affected: this is present in `v1.0-alpha2`.

  `pgcolumnar_scan_getnextslot` accepted a `ScanDirection` and never read it, so
  every call advanced forward. The fetch succeeded, with SQLSTATE `00000`; it simply
  returned rows that were not asked for. Measured on a 20 row fixture with the plan
  asserted in both arms: `FETCH FORWARD 5` then `FETCH BACKWARD 3` gave `6 7 8`
  where the heap gave `4 3 2`, and `FETCH LAST` returned nothing where the heap
  returned row 20.

  Forward is honoured, `NoMovement` returns no row, and backward is refused with
  `feature_not_supported` rather than emulated. The reader walks row groups and
  decodes vectors forwards, so scanning the other way is a feature rather than a
  correction, and inventing an answer is the one thing that must not happen on this
  path. At shipped defaults the planner gives a columnar relation a custom scan,
  which serves these cursors correctly, so the refusal is reached only where that
  path has been turned off.

- An encoded NUL no longer defeats the Iceberg traversal guard (#844). A released
  version is affected: this is present in `v1.0-alpha2`.

  `ice_has_encoded_dotdot` catches a traversal smuggled through percent encoding,
  because an http or https origin may decode a key before serving it. A `%00` in
  front of the traversal defeated it: `ice_percent_decode_once` wrote the decoded
  NUL and kept going, and everything downstream is NUL terminated string work, so
  the next pass measured with `strlen` and the scan walked with `strchr`. Both
  stopped at that byte and never reached the `../` behind it. The literal `..` guard
  beside it saw nothing either, because the segments were encoded.

  Measured over the S3 fixture with the delete path recorded as
  `%00%2e%2e/%2e%2e/%2e%2e/%2e%2e/etc/hostname`: the literal and the plainly encoded
  forms were both refused with `22023`, and the form behind an encoded NUL returned
  `58P01`, meaning it was not refused but fetched, and failed only at the escaped
  location. A NUL has no place in an object key or a path, so the encoded NUL is now
  refused on its own rather than scanned past.

- `pgcolumnar.vacuum_sorted`'s comment no longer calls `cluster()` numeric-only
  (#827 follow-up). A released version is affected: the sentence is in
  `v1.0-alpha2`, in the full script and in the upgrade script, so `\df+` prints it
  to a user today.

  It is wrong in both directions. `cluster_type_supported()` takes `boolean`,
  `smallint`, `integer`, `bigint`, `real`, `double precision`, `date`, `timestamp`
  and `timestamptz`, several of which are not numeric, and it does not take
  `numeric` at all. The extension already contradicted the sentence: a rejected
  column raises an errhint reading "Z-order clustering supports integer,
  date/time, boolean, and floating-point columns". The comment now uses that
  wording, so there is one description of the rule rather than two that disagree.

  Measured with a positive control so the deny arms are not vacuous, on a 20,000
  row table: `vacuum_sorted` accepts `numeric` and `text`; `cluster()` accepts
  `int` and rejects `numeric` with "column n of type numeric cannot be used as a
  clustering key", and rejects `text` likewise.

  The `1.0-alpha2` to `1.0-alpha3` upgrade re-issues the comment, as
  `set_options`, `expire`, `parallel_copy` and `sort_status` already do in that
  script. Without that half, a fresh install and an upgraded one disagree and
  `native_upgrade_converge` fails, which is the suite doing its job: it hashes
  `obj_description(p.oid,'pg_proc')` for every function in the schema. Verified in
  that order: 8 of 8 on unmodified main, failing on both the `1.0-alpha` and
  `1.0-alpha2` paths with only the full script corrected, and 8 of 8 again once
  the upgrade script carried it.

- `pgcolumnar.set_options` no longer writes past three stack arrays during
  `ALTER TABLE ... RENAME COLUMN` (#834). No released version is affected: the
  defect was introduced and fixed inside this cycle.

  `pgcolumnar.options` grew `ttl_column` and `ttl_interval` for retention and the
  `Anum_options_*` constants were extended to 9, but `Natts_options` three lines
  below them was left at 7. It sizes the `values`, `nulls` and `replace` arrays
  handed to `heap_modify_tuple`, which iterates `tupdesc->natts`, so a rename on a
  table with a declared `sort_by` read two slots past the end of all three and
  aborted the backend, taking the cluster into crash recovery.

  On an ordinary build the overrun lands in adjacent stack slots and is silent, so
  the whole matrix passed. `test/catalog_natts.sh` now pins every `Natts_*` constant
  against the width the live server reports, which makes it a check on every major
  rather than a sanitizer only one.

- `date_trunc(unit, ts) = 'infinity'` returns the matching row again (#836). No
  released version is affected: the defect was introduced and fixed inside this
  cycle.

  The equality preimage builds the bounded interval `k >= lo AND k < hi`. Interval
  arithmetic saturates at an infinity instead of raising, so `hi = lo + step`
  returned `lo` again and the interval was empty, excluding the row it was built to
  select. Measured on a 5000 row fixture holding exactly one `+infinity` and one
  `-infinity` row, at default settings: the heap returned 1 and the columnar table
  returned 0 for both, while a bare `ts = 'infinity'` returned 1 from both.

  Equality now declines when the interval degenerates, which costs a scan rather
  than an answer. The ordered operators keep their exemption and are unaffected,
  because each emits one key, so `lo == hi` costs them nothing. That is why the two
  infinity arms already in `test/preimage_rewrite.sh` stayed green while equality was
  wrong: the suite carried infinity arms, and carried equality arms, and never their
  intersection.

- `sum(bigint)` and `avg(bigint)` no longer accumulate across a rescan (#840). No
  released version is affected: the defect was introduced and fixed inside this
  cycle.

  With `pgcolumnar.enable_ungrouped_vector_agg` on, a re-executed aggregate node
  added to the previous execution's running total. There were two accumulator reset
  sites and they had drifted: `pgcolumnar_agg_specs_reset` cleared nine fields
  including `spec->i128sum`, and `PgColumnarReScanAggScan` open coded the same loop
  over eight and never cleared it. The int8 kinds accumulate there, so on a rescan
  that total was never zeroed. Measured over four `LATERAL` iterations with the node
  asserted chosen and asserted rescanned: 800022400060000, then 1600044799119997,
  then 2400067196179988, where every true answer was near the first.

  The second copy is deleted rather than corrected, so the next field added to
  `PgColumnarAggSpec` is safe by construction rather than by remembering two places.

- The documentation is brought back into line with the code, and the version
  check now covers the file that had drifted furthest (#753 follow-up).

  `README.md` said the version marker was `1.0-alpha` while `VERSION` said
  `1.0-alpha3`, two versions behind. It drifted because `test/docs_style.sh`
  compared `VERSION` against `CHANGELOG.md` and `docs/` and did not read
  `README.md`, so the only file that was wrong was the one file the check could
  not see. `README.md` is now in that comparison, and the badge is corrected and
  resized for the longer string.

  Corrected against the code: `docs/how-to.md` said `cluster` takes numeric
  columns only, where the code accepts boolean, integer, floating-point, date
  and timestamp keys and rejects `numeric` and `text`. `docs/installation.md`
  said `DROP EXTENSION` removes columnar tables, where a plain `DROP EXTENSION`
  fails while they exist and only `CASCADE` drops them. `docs/administration.md`
  said there is no cache of decompressed chunk groups, where the fetch cache
  holds four decoded row groups per backend per statement under a 32 MB cap.

  Brought up to date: `docs/roadmap.md` listed five shipped features as planned,
  all five of their issues closed, and repeated that we read local files only.
  `docs/configuration.md` did not document `ttl_column` or `ttl_interval`.
  `docs/features.md` omitted `pgcolumnar.expire` and the `dedup` argument of
  `pgcolumnar.parallel_copy`. `docs/how-to.md` gains a retention recipe, so the
  feature is reachable by task and not only by name.

### Added

- `pgcolumnar.expire` drops row groups whose rows are all older than a declared
  retention, without reading or rewriting them (#403 item 5a). Declare the
  retention with `pgcolumnar.set_options(..., ttl_column => 'ts',
  ttl_interval => '90 days')`; both halves are needed, and either alone means no
  retention.

  This is the tractable half of the paper's "merge-time data transformation".
  The rewrites already retire whole row groups: `pgcolumnar.compact` drops every
  group that is fully deleted. Retention is the same operation with a different
  predicate, and the zone map already holds what decides it, so the decision is a
  catalog read. Nothing is decoded and nothing is rewritten, and it holds only
  `ShareUpdateExclusiveLock`.

  **It is called by name and never runs on its own.** It deletes rows, and an
  operation a user runs for maintenance must not do that silently, so it is not
  wired into `VACUUM`, `compact` or autovacuum. A table with no declared
  retention raises an error rather than reporting that it did nothing.

  A group is kept whole or dropped whole. A group holding rows on both sides of
  the cutoff is kept, so retention is approximate at the group boundary and errs
  toward keeping data. Measured on 5,000 rows in 5 groups with 1,440 rows past a
  three-day retention: one group dropped, 1,000 rows removed, and all 3,560 rows
  still inside the retention kept, including the 440 expired rows sharing the
  straddling group.

  The retention column must be `timestamp` or `timestamptz`.

- `pgcolumnar.parallel_copy` can refuse a load it has already taken, with
  `dedup => true` (#403 item 7). A load that commits, whose acknowledgement the
  client never receives, is retried and the rows go in twice: measured, the same
  file loaded twice gives 100,000 rows and then 200,000.

  With `dedup`, the SHA-256 of the file is recorded in the new
  `pgcolumnar.load_fingerprint` catalog after a successful load. A later load of
  the same contents into the same table stores nothing, returns 0, and raises a
  `NOTICE` rather than failing. A file whose contents changed is a different load
  and is stored, even at the same path.

  It is off by default, because discarding rows a caller asked to store is not
  ordinary `INSERT` behavior.

  The unit is the whole load rather than a "part". `parallel_copy` is atomic
  through 2PC, proved by a malformed row at line 50,001 leaving 0 rows and 0
  prepared transactions, so parts never commit independently and a part hash
  would deduplicate nothing that is not already all-or-nothing.

  The check runs after every worker has prepared and before anything commits,
  which is the only point at which a repeat can be refused without charging every
  load for it. A refused load therefore does its work and discards it. The
  alternative, hashing before dispatch, costs 42% of a 264 MiB load; as
  implemented the fingerprint has no measurable cost, because the coordinator
  computes it while the loaders are already reading the same file.

  Three limits, all stated in `docs/sql-reference.md`: the fingerprint is
  recorded after the data commits, so a crash between them leaves data that a
  retry will store again; two identical loads running at once both store, because
  each checks the record before either writes it; and a refused load still reads
  and parses the file.

### Fixed

- The zone-map survival estimate reads the row-group geometry a table was
  **written** with, so a plan's cost no longer moves with the planning session's
  `pgcolumnar.stripe_row_limit` (#817). This is the half #806 left open; it fixed
  the index-fetch penalty and deliberately declined the same substitution here.

  `pgcolumnar_zonemap_survival` asked `pgcolumnar_effective_stripe_row_limit`,
  which answers "what limit would a write in THIS session use". For a table with
  no per-table option, that is the session GUC. A table written at 2,000 rows per
  group and planned from a session at the 150,000 default was therefore modelled
  as holding `ceil(20000/150000) = 1` group, the one sampled group survived, and
  the discount vanished. Measured on one unchanged table, varying only the
  plan-time GUC:

  | plan-time `stripe_row_limit` | before | after |
  | --- | ---: | ---: |
  | 150000, the default | 306.00 | 61.20 |
  | 2000, the written value | 61.20 | 61.20 |

  A 5x swing on a table that did not change, and at the default the pruning
  predicate was priced identically to a full scan of the same table -- pruning had
  left the cost model entirely rather than merely drifting.

  **Why this is now safe, and was not before.** #806 declined it because the
  substitution then moved `native_zonemap_narrow` from wide=30/narrow=30 to
  wide=570/narrow=66, zone-map reads that scale with table width. That was the
  width-blind whole-group probe in the estimator, which #821 replaced with a
  per-column probe. The same substitution now measures **wide=40/narrow=40**,
  exactly width-independent.

  Planning-time zone-map fetches go from 1 to 10 on that fixture. The 1 was never
  a saving: it was the estimator examining a single group because it believed a
  20,000-row table held one. Ten is the estimator sampling the ten groups that
  exist, bounded by `PGCOLUMNAR_PRUNE_SAMPLE_GROUPS` and charged per predicate
  column rather than per table column.

  `test/doc_parallel_premise.sh` carried `SET pgcolumnar.stripe_row_limit = 20000`
  as a workaround for exactly this. It is **removed**, which makes that suite a
  removal proof: its cost-ordering check now holds only because the estimator
  reads the written geometry.

- `pgcolumnar.row_group.group_number` is documented as **one-based**, which is what
  it has always been (#817). The column comment said "0-based row group ordinal".
  A group number is the stripe id reserved from the metapage when the group began
  buffering, and `PgColumnarInitMetapage` starts `reservedStripeId` at 1, so there
  is no group 0 on any storage. Verified rather than reasoned: on a 27-group table,
  `row_group` and `zone_map` both report `min = 1, max = 27` over 27 distinct
  numbers.

  This is a source comment, not a catalog one -- there is no `COMMENT ON` for the
  table, so nothing reads it at runtime and `native_upgrade_converge`, which
  compares `col_description`, cannot see it. It is corrected because a reader
  believed it: the planner's zone-map sample walked `[0, ngroups)` and paid for it
  in the defect fixed immediately below.

- The planner's zone-map sample is one-based, so it no longer under-prices a
  scan for matching the **newest** rows (#817).

  `pgcolumnar_zonemap_survival` samples row groups and asks the reader's own skip
  predicates how many survive. It walked `g = i * ngroups / nsample` for `i` in
  `[0, nsample)`, so it sampled `[0, ngroups)`. A row group number is the stripe
  id reserved from the metapage, and the metapage starts `reservedStripeId` at 1:
  **group 0 exists on no table.** The first probe was always spent on a number
  that could not exist, and group `ngroups` was never probed at all.

  The wasted probe was harmless -- an absent group narrows the sample rather than
  biasing it. The missing one was not. When the group count fits in the sample
  target the loop is a census, and it was a census that omitted the newest group
  every time, so the same predicate was priced differently according to where in
  the table its groups sat. Measured on ten groups of 2,000 rows, one clause
  each, identical row estimates, the only difference being position:

  | predicate | groups matched | before | after |
  | --- | --- | ---: | ---: |
  | `c1 > 8000` | 5..10, the six newest | 166.89 | 180.24 |
  | `c1 <= 12000` | 1..6, the six oldest | 200.27 | 180.24 |
  | `c1 > 17000` | 9,10, the two newest | 33.38 | 60.08 |
  | `c1 <= 4000` | 1,2, the two oldest | 66.76 | 60.08 |

  Exactly half, in the narrow pair. The under-priced half is the recency
  predicate this engine is aimed at: on batch-loaded time-series, `WHERE ts >
  now() - interval '1 hour'` selects the groups the sample never looked at.

- The planner's zone-map sample reads only the predicate columns, as the executor
  already does (#817).

  It called `PgColumnarReadZoneMapList`, which keys on `(storage_id,
  group_number)` against the four-column `zone_map_pkey`, so it fetched every
  column's and every vector's row from the heap and then used one column's.
  `pgcolumnar_native_group_can_match` has asked the per-column question with a
  three-key probe since #314; the estimator now asks it the same way, through the
  same `PgColumnarReadZoneMapForColumn` and the same session cache. Planning-time
  `zone_map` fetches on a 30-column table go from 540 to 10, and no longer scale
  with table width: 30 columns and 2 columns both read 10.

  The estimator holds a #744 read session of its own, so it resolves `zone_map`
  once for the whole sample rather than once per probe. Its `DEBUG1` report is
  labelled `zone map estimate:` where a scan's stays `zone map read:`, because
  the two interleave in one backend's log and `native_zonemap_session` counts
  scan reports to prove that a scan around an aborted one opens for itself. A
  scan's line is byte-identical to what #744 shipped.

  **The two halves could not ship apart.** Fixing only the one-based sample makes
  the whole-group probe reachable at the default `stripe_row_limit`, where the
  single wasted probe had been hiding it, and `native_zonemap_narrow` correctly
  reddens at wide 90 against narrow 34.

- `PgColumnarReadZoneMapForColumn` no longer discards the index oid it just
  cached (#817). #744 resolves `zone_map_pkey` once per read session and stores
  it; an unconditional second `pgcolumnar_index_oid("zone_map_pkey")` stood
  immediately before `systable_beginscan` and overwrote both that value and the
  no-session branch's own lookup. The session's `opens` counter never noticed,
  because it counts relation opens, which really were saved. Shown by poisoning
  the cached value to `InvalidOid`: with the dead store present the poison is
  inert (40 index fetches, 0 sequential scans, identical to the unpoisoned
  control), and with it removed the poison reaches the scan and forces 20
  sequential scans of `zone_map`. A dead store draws no compiler warning, which
  is how it survived.

- The cost model reads the row-group geometry a table was **written** with,
  not the geometry a write in the planning session would produce (#806).
  `pgcolumnar.storage.row_group_limit` records what the writer used and nothing
  read it back; the index-fetch penalty called
  `pgcolumnar_effective_stripe_row_limit()`, which returns the per-table option
  or else the planning session's `pgcolumnar.stripe_row_limit`.

  The consequence is larger than a mis-costed table. A session that sets that
  GUC -- before a bulk load, say -- repriced every columnar table it then
  planned against, including tables it never touched. Measured on one unchanged
  table of three row groups, varying only the GUC at plan time:

  | plan-time `stripe_row_limit` | before | after |
  | --- | ---: | ---: |
  | 150000 | 775.26 | 775.28 |
  | 20000 | 113.26 | 775.28 |
  | 5000 | 36.26 | 775.28 |

  A 21x swing on a table that did not change. `R` is not a minor term: it sets
  the group count, the pages per group, the per-group decode CPU, and the
  `decodedWidth * R` test against the 32 MB fetch-cache cap, so it can be wrong
  in either direction there.

  **One call site changed**, the index-fetch penalty, which is where the defect
  was measured. Two others deliberately did not:

  - `columnar_write_state.c` still asks `pgcolumnar_effective_stripe_row_limit()`,
    because a writer deciding the geometry it is about to lay down is exactly
    what that function answers;
  - `pgcolumnar_zonemap_survival()` also still asks it. Substituting the written
    geometry there moved `native_zonemap_narrow` from 30/30 zone-map reads on a
    wide and a narrow table to 570/66 -- reads that scale with table width,
    which is the property that suite exists to hold. There is no measured defect
    at that site and there is a measured regression from changing it.

  An explicit per-table `stripe_row_limit` still wins over the written geometry.
  This is about the planning session's GUC, which has nothing to do with the
  table; a per-table option is a durable statement by its owner, and
  `native_index_fetch_stripe_cost` asserts the cost model can see it.

  Two things this leaves, stated rather than hidden. Because the survival
  estimate still reads the session GUC, a plan-time limit far enough below the
  written one can still flip a plan through that path: at 5000 the table above
  costs 87.79 rather than 775.07, because it stops being an index scan. And the
  recorded limit is one number, the last writer's, so a table whose groups were
  written under changing limits is still approximated -- the exact quantity is
  the real group count, which would be a catalog scan proportional to the number
  of groups on every plan.

  New suite `test/cost_written_geometry.sh`. It asserts planner costs rather
  than timings, so `PGC_SKIP_TIMING` does not drop it.

### Changed

- Chunk-group skip predicates are now evaluated most-selective-first, so a group
  that is going to be pruned is no longer probed through every other predicate's
  column on the way there (#403 item 4). The skip loop returns on the first
  predicate that excludes, and each predicate's first use in a group fetches that
  column's zone map from the catalog, so the order decides what a pruned group
  costs.

  The order used was the order the ScanKeys arrived in, which is attribute order.
  Measured on 100 row groups with one predicate excluding 99 groups and one
  excluding none: 200 zone-map probes, and writing the selective predicate first
  in the query did not change that, because query order is not ScanKey order. The
  order was arbitrary with respect to selectivity rather than merely suboptimal.
  After: 102 probes, with the same 99 groups pruned.

  The reordering is a transpose on each exclusion rather than a sort: O(1), no
  statistics the reader does not already have, and it converges in one step for
  one selective predicate behind an unselective one. The paper's caveat, that
  ordering should apply only when a highly selective predicate is present, falls
  out of the mechanism: a predicate that never excludes never moves, so a query
  with nothing selective keeps the order it started with and pays nothing.

  This cannot change an answer. The predicates are a conjunction, and the loop
  returns on the first exclusion whichever one that is. `EXPLAIN (ANALYZE)`
  reports `Columnar Zone Map Probes`, which is the only line that moves.

- The vectorized grouped aggregate now sizes its hash table from the group
  estimate the planner already made, instead of growing it from nothing
  (#403 item 6). The table is open-addressing and doubles at 70% load from
  capacity 0, so a query with 200,000 groups walked 1024, 2048, ... , 524288 and
  rehashed every live entry at each step. Measured on 2,000,000 rows with 200,000
  groups: 10 allocations and 366,280 entries rehashed before, 3 allocations and
  46,591 after, with identical answers.

  The table still starts at 1024 and is sized only once the data has proven it
  must grow, and a grow jumps toward the estimate but never past 64 times what
  the table has proven it holds. Both bounds are expressed in the live count
  rather than in a memory budget, which is the point: an earlier version of this
  change bounded the allocation by `work_mem` and so allocated 131,072 entries
  for a query with 47 real groups, because `estimate_num_groups` cannot see
  through a function and `GROUP BY date_trunc('day', ts)` reaches the node with
  an estimate of every row. A query whose groups fit in the starting 1024 never
  allocates more than it would on unpatched code. Under-estimating costs nothing
  new.

  `EXPLAIN (ANALYZE)` reports `Columnar Group Table Entries Rehashed`, the work
  done, alongside the allocation count and the table's peak capacity, which is the
  memory cost. Entries rehashed
  rather than resizes because the two disagree: the first instrument counted
  resizes and reported that sizing removed 7 of 10 while it removed about a
  quarter of the rehashing, the work being dominated by the last two steps.
### Fixed

- `test/build_all_versions.sh` now reports how many majors it built and refuses
  a run that built none (#809). Its verdict read only the `failed` flag, which
  only a build that RUNS and FAILS sets, so a `pg_config` that is not executable
  was skipped without touching it. A run where every major was skipped reached
  the end with `failed=0` and printed `PASSED` with exit 0, having invoked no
  compiler.

  That is not hypothetical: the default list is `/usr/local/pg15`, `pg16`,
  `pg17`, `pgsql` and `pg19`, and a machine whose builds live elsewhere skips
  all five. It was recorded as a green five-major preflight for a pull request
  and caught by reading the body above the verdict rather than by any check.

  The script now prints `built N of M` before its verdict, in the same shape
  `test/run_all_versions.sh` already uses for `versions run: N of M configured`,
  and fails on zero with the invocation that names the paths. A PARTIAL run is
  deliberately left passing: whether three of five present should fail or warn
  is a judgement about how people run this, and the count makes it visible
  either way. A new `harness_selftest` case pins all three behaviours, since
  nothing covered this script before.

### Added

- `pgcolumnar.sort_status` reports `sorted_kind`, so the reporter can finally
  express the distinction the catalog has recorded since #758 (#761). It holds
  `lexicographic` after `pgcolumnar.vacuum_sorted`, `zorder` after
  `pgcolumnar.cluster` and `pgcolumnar.recluster`, and NULL when the table was
  never ordered or was ordered before the column existed.

  `sort_key` names the columns and says nothing about the arrangement, and a
  Z-order over two or more columns is not a sort on any one of them. The
  documented way to read the kind was to select it from `pgcolumnar.storage`
  directly. That table carries no `GRANT`, so only a superuser could follow that
  advice, while `sort_status` is SECURITY DEFINER and gated on
  `require_caller_select` and is therefore available to a table's own owner.

### Changed

- The extension's `default_version` is now `1.0-alpha3`, and
  `pgcolumnar--1.0-alpha2--1.0-alpha3.sql` ships with it. Adding an OUT parameter
  changes a function's signature, so #761 cannot be a `CREATE OR REPLACE`; it is
  the change that opens this cycle. Every other `[Unreleased]` entry so far is
  shared-library only and needs no catalog change.

  `test/native_upgrade_converge.sh` now exercises both `1.0-alpha` and
  `1.0-alpha2` as starting points, and asserts that each reaches a catalog
  identical to a fresh `1.0-alpha3` install in function definition, ACL and
  comment, relation kind, column type and comment, non-base types, and foreign
  data wrappers. `1.0-alpha` reaches it by the chain through `1.0-alpha2`.

### Fixed

- The index-fetch penalty now charges decode CPU by projected column WIDTH, not by
  column count (#803). This is the same flatness #768 fixed for the sequential
  scan, in the other cost site. `pgcolumnar_index_fetch_penalty`'s CPU term was
  `cpu_operator_cost * R * nproj`, a count, so a 68-byte text column was charged
  exactly what a 4-byte int4 column was.

  Measured on two tables of identical shape whose columns differ only in type, so
  the decoded prefix is four columns on both arms and only the bytes move. Read
  out of an instrumented build, the decode CPU term was **200.00 on both arms**
  across an 8.6x difference in decoded width (16 B against 138 B). The same
  ~39,000-row index fetch took 4,385 ms on the narrow table and 88,352 ms on the
  wide one, a 20.15x difference against a modelled 1.49x. Cost per millisecond
  spanned 13.4x, inside the 13x-22x #768 measured for the scan path.

  The consequence was an inverted ordering rather than a uniform under-charge. A
  wider decoded prefix makes every row-group decode dearer, so a wide table should
  abandon per-row fetches sooner; priced by count it did the opposite. The model
  kept the index up to 120-161 rows on the wide table and only 40-60 on the narrow
  one, while the measured crossovers are 40-120 wide and 120-279 narrow. At 120
  rows the wide table's chosen index plan measured 239.6 ms against an 82.9 ms
  scan.

  `pgcolumnar_scan_decode_shape` now accumulates decode units over the prefix it
  already walks, using the same reference width as the scan's
  `pgcolumnar_projected_decode_units`, and the penalty charges those units.
  Substituting `pgcolumnar_projected_decode_units` itself would have been wrong:
  it sums the columns a query *references*, while the fetch decodes the *prefix*
  up to the highest referenced column (#363), and the two differ on exactly the
  shape #363 exists for.

  An all-int prefix is priced exactly as before, because a 4-byte column is one
  unit: the narrow arm's flip point is unchanged at 40-60 rows. The wide arm moves
  to 7-15. Both arms now stop earlier than the measured crossover, which is the
  direction the model already erred on the narrow arm before this change; the
  #376 bound and the clustered-index and point-lookup guards in
  `test/analyze_stats.sh` are unaffected and still pass.

  `test/index_fetch_penalty_width.sh` pins the ordering from the plan rather than
  the clock, so `PGC_SKIP_TIMING` does not apply to it. On the unfixed build its
  seven premises pass and the ordering check fails with
  `INVERTED (wide 138B fetches to 120 rows, narrow 16B only to 40)`.

- `pgcolumnar_fetch_group_slot`'s header promised a NULL return that neither the
  function nor its callers implement, and following it segfaults (#795). The
  comment read "Returns NULL when nothing should be cached, in which case the
  caller decodes into its own scratch context exactly as before". There is no
  such path. The function has two returns, `return e` on a hit and
  `return victim` on a miss, and `victim` cannot be NULL: the branch that runs
  when every slot is live picks a least-recently-used entry. Both callers
  dereference the result immediately, one through `entry->firstRowNumber` in the
  geometry check and one through `MemoryContextSwitchTo(entry->cx)`, neither
  guarded.

  So the sentence did not describe an unimplemented option, it described a trap.
  Forcing the documented return on an assert build of PostgreSQL 18.4 produced
  `signal 11: Segmentation fault` in a backend. Nothing in `main` reaches it --
  the function never returns NULL today, and the same fixture on a clean build is
  correct -- but the comment invites the change that does.

  The comment now states the guarantee the code makes, that a slot always comes
  back and a miss returns a reset entry for the caller to fill, and records that
  a "do not cache this one" policy needs a scratch-context path in the callers
  before it can be expressed as a NULL. An `Assert(entry != NULL)` at each call
  site holds that to be true rather than leaving it to the comment, which is what
  rotted. With the Assert in place the same injected NULL return reports
  `TRAP: failed Assert("entry != NULL"), File: "src/columnar_reader.c", Line:
  3888` instead of faulting, so the next person to try it gets the line rather
  than a core file.

- The columnar scan's decode cost is now charged by projected column WIDTH, not
  by column count (#768). #503 gave the scan a per-value decode term, which
  fixed "nine columns are priced like one"; it counted columns, so a 324-byte
  text column was charged exactly what a 4-byte int4 column was. Measured on
  4,000,000 rows, four 324-byte text columns were priced at 163,422 while taking
  5875 ms, against 206,524 and 335 ms for eight int columns: priced below, and
  17.5x slower. Cost per millisecond across those projections spanned 22x before
  the change and 1.7x after it, with an int-only projection priced exactly as
  before.

- The shared test cluster no longer sets `pgcolumnar.unique_lock_buckets`
  (#799). `test/lib.sh` wrote `100003` into the cluster nearly every suite runs
  against, where the shipped default is `128`, so the whole tree ran 781x above
  shipped behaviour to serve one suite that already sets the value on the
  cluster it builds itself. The visible symptom was a 20,000-row insert into a
  columnar table with a unique index failing with `out of shared memory` and a
  hint to raise `max_locks_per_transaction`, which reads as a product defect and
  is not one. A new harness selftest keeps the shared cluster config free of
  `pgcolumnar.*` GUCs; `PGC_EXTRA_CONF` remains the per-suite mechanism.

- The `one/many` fetch cost guard in `test/native_fetch_cache.sh` now rebuilds
  its fixture before every reading, and asserts the group geometry at each one
  (#801). The guard could not fail. The `UPDATE` it times does not rewrite the
  row group it reads: it marks the rows deleted and appends them as a new group,
  so `fc_one` goes from one group of 20,000 rows to that group plus one of
  2,000, and from the second reading on the rows it targets sit in the small
  appended group. That is the geometry the `fc_many` control has, so the arms
  stop differing after one reading, and `min3` returned one of the cheap
  post-rewrite readings every run. Measured against a build whose fetch cache
  retains nothing, so every fetch re-decodes: the guard read 0.98x and PASSED,
  where rebuilding reads 5.89x and 6.13x on two runs and fails as it should. The
  `#353` guard in the same file fails correctly on that build, which is what
  shows the ablation was real. A per-fetch decode counter says it in rows rather
  than milliseconds: `fc_one` decodes 40,000,000 rows on the first reading and
  4,000,000 on the next two, and 40,000,000 on all three once the fixture is
  rebuilt.

  The new premise records the group count at each reading. Both faults it
  covers were proved by mutation, each changing one thing: dropping the rebuild
  records `1 2 3`, and an `INSERT` that populates nothing records `0 0 0`. The
  timing check passed in both mutants, at 0.77x and 1.14x, so the premise is
  what separates either fault from a green suite.

- The fetch cache cost guards in `test/native_fetch_cache.sh` now assert that
  they reach the per-row fetch path, and set
  `pgcolumnar.enable_index_fetch_penalty = off` so that they do (#797). They had
  not exercised the cache since the penalty landed: `enable_seqscan = off` and
  `enable_bitmapscan = off` do not disable the columnar custom scan, so the
  planner answered these queries with a group scan and the guards timed a
  different mechanism while staying green. The #353 guard was written 83 minutes
  before the penalty existed. With the path restored the two guards read 2.00x
  and 6.42x, against 0.98x and 1.21x before, and the 6.42x reproduces the 5.8x
  recorded in the suite's own comment when the guard was written.

- `sum()` and `avg()` over `numeric` no longer take the ungrouped vectorized
  path, where they were slower than the ordinary `Agg` (#785). This closes the
  last of the three families `pgcolumnar.enable_ungrouped_vector_agg` names.
  `bigint` was fixed by giving it a cheap accumulator (#786); `numeric` cannot
  have one, because its input already carries a scale.

  Measured on 8,000,000 rows, interleaved, minimum of seven, plan and answers
  asserted on both arms:

  | query | before | after |
  | --- | ---: | ---: |
  | `sum(numeric)` | 1.10x slower | **1.04x faster** |
  | `sum(float8)` | 2.73x faster | 2.74x faster |
  | `sum(numeric), sum(float8)` | 1.00x, no gain | 1.01x, unchanged |

  **The refusal is unconditional, and the mixed case is why.** Classification is
  all or nothing, so refusing `numeric` refuses the whole node, and the obvious
  worry is that a mixed query loses the float win with it. It does not. Measured
  on one cluster with only the installed library changing, a mixed query was
  being actively **harmed**:

  | query | numeric admitted | numeric refused | |
  | --- | ---: | ---: | ---: |
  | `sum(numeric)` | 391.0 ms | 351.0 ms | 1.11x faster |
  | `sum(numeric), sum(float8)` | 412.4 ms | 367.6 ms | 1.12x faster |
  | `sum(float8)` (control) | 78.8 ms | 77.2 ms | flat |

  So the mixed case is an argument for the refusal rather than a cost of it.

  It is also not conditional on the data. The penalty holds whether or not
  chunk-group pruning happens: 1.14x with 25 of 27 groups removed and 1.56x with
  none.

  The refusal is placed where the scan-fold path is chosen rather than in
  `pgcolumnar_classify_aggref`, which is shared with the grouped path. The
  grouped path is a different implementation and has not been measured for these
  kinds, so this narrows only what was measured.

### Changed

- `test/native_fetch_cache.sh`'s three cost guards now assert through
  `check_ratio` instead of computing the comparison in shell and passing
  `yes`/`no` to `check`. They were hand-rolled while they used `check_timing`,
  which takes a scalar; once #792 made them ordinary checks the ratio helper
  became available, and it is strictly better.

  `check_ratio` refuses a zero on **either** side. The hand-rolled form guarded
  only the denominator, so a numerator timing at 0 ms, which means the
  measurement fell below timer resolution, passed silently as a ratio of zero.
  Demonstrated in both directions with the same injected reading:

  ```
  check_ratio:  FAIL  ... a side of the ratio is zero, so nothing was measured: a=[0] b=[26]
  hand-rolled:  PASS  ...
  ```

  It also compares as a float rather than truncating integer division, and
  prints the ratio with both sides, so a verdict now reads
  `(1.39x, bound 3x, from a=39 b=28)` rather than a bare `yes`.

  One boundary moves by a hair and the comment says so: the old form failed at a
  ratio of exactly 3.0 and `check_ratio` fails above it. Nothing measured on
  these shapes is near that.

### Changed

- `test/native_fetch_cache.sh`'s three cost guards now run in CI. They were
  written with `check_timing`, which `PGC_SKIP_TIMING` drops, and that suite is
  not in `is_timing_suite` -- so the suite ran, reported `PASSED`, and skipped all
  three, including the two that guard named regressions (#353, #359). Three cost
  guards in no automated gate (#792).

  They were already the exempt shape by `test/cancel_decode.sh`'s argument: two
  readings taken back to back in the same run, which move together under load.
  But that argument has a second half -- *"the best of three readings, not the
  average ... an average would let one descheduled run widen the ratio on its
  own"* -- and these took a single reading per side. Measured on six busy cores
  of an eight-core box before the change, the `one/many` ratio reached **2.39
  against its bound of 3** on one run. Unguarding them as they stood would have
  traded a check that is skipped everywhere for one that is flaky somewhere.

  So each side is now the minimum of three, and then they are ordinary checks.
  Under the same load afterwards, four runs: `one/many` 0.84 to 0.93, `wide/small`
  0.98 to 1.00, `over/under` 1.18 to 1.23, against bounds of 3, 5 and 12. The
  suite costs about 0.9 s more (4.4 s to 5.3 s, build excluded).

  A repeated measurement must be idempotent, and this one was not: `upd_ms`
  updated with `v = v + 1`, which is correct once and wrong three times. The
  suite's own correctness arms caught it -- they assert `v = id + 1` -- so the
  update now sets `v = id + 1`, which is the same work per row and true after any
  number of runs.

  `check_timing`'s skip message said "wall-clock ratio" on a helper that takes a
  scalar, so it misdescribed every skip it printed. Its one remaining caller,
  `test/native_cancel.sh`, is in `is_timing_suite`, so the driver names that suite
  as skipped rather than reporting a pass over a dropped subject.

- `sum()` and `avg()` over `bigint` now take the batch fold, which closes the
  filtered case that #786 left behind (#755 question 3). Before this, a filtered
  `sum(bigint)` on the vectorized path was **slower** than not using it, while
  `sum(float8)` on the same fixture was more than twice as fast. That asymmetry
  was the whole evidence for the question, and it was the batch fold rather than
  the aggregate: the int8 kinds folded row at a time while the float kinds folded
  column at a time.

  Measured on one cluster with only the installed library changing, 8,000,000
  rows, values chosen to defeat run-length and dictionary encoding so the fold is
  not answered from encoded runs, minimum of seven, answers identical:

  | | Before | After |
  | --- | ---: | ---: |
  | `sum(bigint)` | 133.5 ms | **92.0 ms** |
  | `sum(bigint) WHERE ...` | 228.2 ms | **99.4 ms** |
  | `avg(bigint) WHERE ...` | 226.2 ms | **98.7 ms** |
  | `sum(float8) WHERE ...` (control) | 85.2 ms | 86.6 ms |

  The float control does not move, which is what says the change is specific to
  the int8 kinds.

  It is a two-line change because #786 did the work: the fold reads a column at a
  time and then accumulates per value, so a kind is foldable when its accumulate
  is cheap, and since #786 the int8 kinds accumulate into an `int128` and
  allocate nothing. This does **not** make them parallel-eligible.
  `pgcolumnar_parallel_agg_ok` is a separate predicate with its own callers, and
  the int8 kinds stay out of it: an `int128` running total is not a partial state
  a core `Finalize` can combine.

  `sum`/`avg` over `numeric` are unchanged and still slower on this path. They
  cannot use an integer accumulator, and that residue stays on #785.

### Changed

- `check_ratio_timing` is renamed to `check_ratio_needs_quiet_machine`, and the
  distinction it encodes is now written where it is decided (#787). The helper was
  named for what it measures, a ratio of timings, rather than for what it does:
  both `ci.yml` and `nightly.yml` set `PGC_SKIP_TIMING`, so a check written with it
  runs in no automated gate at all.

  Two kinds of timing assertion need different treatment and only one needs that.
  A ratio against an absolute or cross-run baseline can be distorted by a loaded
  machine. A ratio whose two arms are measured back to back in the same run and
  compared by minimum cannot, because both readings move together under load, and
  `test/cancel_decode.sh` had argued exactly that for its own ratio and run in CI
  on the strength of it. An author holding the safe shape reached for the name
  that matched their units and lost the check silently, which is how #786's guard
  came to be skipped everywhere until it was moved to `check_ratio`.

  No behaviour changed. The two call sites, both in
  `test/planner_choice_quality.sh`, keep the guard they had; that suite is in
  `is_timing_suite`, so the driver already names it as skipped rather than
  reporting a pass over a dropped subject. What changed is that the reasoning now
  sits beside the helper rather than being re-derived in three suite headers --
  `bloom_sizing` for a size, `native_fetch_bigcap` for buffers, `cancel_decode`
  for a same-run ratio -- which is the recurrence #253, #254 and #764 each fixed
  for one suite.

- The documentation style gate is now based on ISO 24495-1:2023, *Plain language
  - Part 1: Governing principles and guidelines*, in place of ASD-STE100. The
  project's writing rules cite two standards and no others: ISO 24495-1:2023 for
  language, and ISO 82079-1 for the structure of instructions.

  **No check changed.** The gate enforces the same four rules over the same
  files, and `docs_style` runs the same nine checks. What changed is the basis
  each rule is attributed to, and the honesty of that attribution:

  - the 25-word sentence limit and the idiom list come from the standard's
    "understandable" principle;
  - the two dash rules are this project's typographic house rules and are now
    labelled as such, because ISO 24495-1 does not ask for them.

  The 25-word figure is named as this project's measurable proxy rather than a
  number quoted from the standard, which does not give one. `test/ste_check.py`
  is renamed to `test/plain_language_check.py` so the file does not assert a
  standard the project no longer cites.

  Conformity is still not claimed, and the reason is now the accurate one. Only
  one of the four governing principles has mechanically checkable content, and
  the standard's own test for another is that a reader acts on the document
  successfully, which no checker performs.

### Fixed

- `sum()` and `avg()` over `bigint` are no longer **slower** on the vectorized
  path than off it (#785). `pgcolumnar.enable_ungrouped_vector_agg` describes
  itself as the fast path for "sum/avg over int8/float/numeric", and for `bigint`
  it was a pessimisation: 2.03x slower for `sum`, 1.91x for `avg`.

  The path converted every value to `numeric` and called `numeric_add` per row,
  which is two allocations and a full numeric addition for each row. A profile of
  it was 13.0% `make_result_opt_error`, 9.3% `add_abs`, 8.1% `init_var_from_num`
  and 8.5% `AllocSet` alloc and free: the numeric machinery, not the aggregate.
  PostgreSQL's own `bigint` accumulators are 128-bit and convert once, and this
  now does the same.

  Measured on 8,000,000 rows, interleaved, minimum of seven, with the plan and
  the answer asserted on both arms:

  | | Before | After |
  | --- | ---: | ---: |
  | `sum(bigint)` | 2.03x slower | **1.65x faster** |
  | `avg(bigint)` | 1.91x slower | **1.74x faster** |

  `float4` and `float8` were already large wins on this path (3.18x and 2.88x
  here), so the setting is now a gain for three of the four families it names.
  `numeric` remains about 1.14x slower and is unchanged by this: its input is
  already `numeric` with a scale, so it cannot use an integer accumulator. That
  residue is recorded on #785 rather than fixed here.

### Changed

- `docs/configuration.md` now documents when `encode_effort` changes anything at
  all, and what it costs on read (#768). The section described the setting as a
  trade between load speed and compression ratio, which reads as though the
  choice has no consequence after the load.

  The writer builds the FSST symbol table, then compares the result against the
  same data without it **after the block codec has run**, and keeps the table
  only when the saving clears `pgcolumnar.fsst_min_gain_percent`, default 5.

  **That decision depends on the data and cannot be predicted.** It is made for
  each column chunk, and two people who both write "text-shaped" test data get
  opposite answers. In four shapes measured for this entry the table was dropped
  every time, so `full` and `fast` produced byte-identical storage and equal read
  times. A second set of measurements found shapes where it survives and `full`
  is 31.8% and 9.6% smaller, at read times of 1.08x and 0.99x, because the extra
  decoding is paid back by reading fewer bytes.

  So the section gives the reader a way to measure their own column rather than a
  verdict. An earlier draft of this entry concluded that at default settings the
  option changes nothing but write time. That was true of the shapes it was
  measured on and false in general, and a reader who acted on it could have paid
  a third more storage.

  The read-cost table is scoped to `compression = 'none'`, which is the only
  setting that keeps the table unconditionally, and is labelled as isolating the
  encoding rather than describing a default installation:

  | Text shape | `full` | `fast` | `fast` is |
  | --- | ---: | ---: | ---: |
  | 128-char hex | 267.6 ms | 143.9 ms | 1.86x faster |
  | URL-shaped text | 131.5 ms | 96.0 ms | 1.37x faster |

  Those replaced 2.69x and 3.84x, which were measured before the reader learned
  to decode a symbol with one machine word. The section names the reader version
  its figures came from.


### Fixed

- `ORDER BY` no longer returns unordered rows after a column rename, and neither
  ordering self-gate skips work it must do (#778). Both follow from one cause:
  nothing maintained the recorded sort key across a rename. `pgcolumnar.storage` records
  the applied sort key as column **names**, and both gates compare that stored
  list against the current `attname`: `pgcolumnar.vacuum_sorted`'s gate and the
  online `pgcolumnar.recluster`'s. Nothing maintained the mark across a rename.

  A three-statement swap made the stored name resolve to a different column:

  ```sql
  ALTER TABLE t RENAME COLUMN a TO tmp;
  ALTER TABLE t RENAME COLUMN b TO a;
  ALTER TABLE t RENAME COLUMN tmp TO b;
  SELECT pgcolumnar.vacuum_sorted('t','a');   -- reported success, did nothing
  ```

  The gate reported "already in this order" about a column that was never
  sorted, and the table was left neither ordered nor reclaimed with no error
  raised. For `vacuum_sorted` that is the failure the gate exists to prevent,
  reached through another door. `recluster` returned 0, which a scheduler reads
  as "nothing to do".

  The mark is renamed rather than cleared. A rename does not move data, so the
  ordering is still true of whichever column now carries the name, and following
  the rename keeps a real optimisation instead of discarding it on every rename.
  It composes through the swap above: `{a}` to `{tmp}` to `{b}`, which is the
  column the rows are ordered by. A rename that touches no sort-key column does
  not rewrite the storage row at all.

  The wrong-answer half is the more serious one. The sorted-pathkey code (#751)
  reads the same mark, so a stale mark made the planner claim an ordering that
  did not hold: after the swap above, `SELECT a FROM t ORDER BY a` planned with
  no `Sort` node and returned 1,990 descents out of 2,000 rows.

  The rename **cascades** to inheritance children and partitions, which are
  separate relations with their own storage and their own marks, so the fix
  walks every columnar descendant. PostgreSQL refuses
  `ALTER TABLE child RENAME COLUMN` with "cannot rename inherited column", so
  for a columnar partition the parent is the only route a user has, and a fix
  that looked only at the relation named in the statement would never have fired
  for it at all.

  The declared key in `pgcolumnar.options` is maintained the same way, and it is
  not optional once the mark is. Before, both were consistently stale; renaming
  only the mark would make the two catalogs name different columns, so a bare
  `pgcolumnar.vacuum_sorted(t)` would silently rewrite on a different physical
  key than it did the day before, with no change to the call and no change to
  the declared intent. `options.sort_by` holds names deliberately, because
  `options` is the catalog carried through `pg_dump` where attnums renumber and
  names do not, so maintaining them across a rename is the correct repair rather
  than a workaround.

  One behaviour improves as a result. The sorted-pathkey claim used to be
  refused after a rename, because the recorded name stopped resolving; it now
  survives, so `ORDER BY` on the renamed column plans no `Sort`. That claim is
  sound for the same reason the mark is: the rows really are still in that
  order.
### Changed

- The harness now refuses a `pgcolumnar.set_options` call whose value the
  function will reject. `set_options` raises on an out-of-range limit, so a
  suite that calls it and discards the output runs on **default** limits while
  the script reads as configured, and every later assertion is about a fixture
  that was never built. Found in review probes that passed
  `stripe_row_limit => 500`, which errors with "must be at least 1000".

  The naive form of that guard would be wrong. Measured on this tree before
  writing it: 182 `set_options` calls and exactly three out-of-range literals,
  all in `test/audit.sh`, all deliberate, all wrapped in `expect_error` because
  rejecting them is what that suite tests. A guard flagging every out-of-range
  value is wrong on three of three. The discriminator is whether the call's
  result is inspected or discarded, so calls passed to `expect_error` are
  skipped and exactly the silent class remains.

  Line continuations are joined before matching, because two of those three
  calls are backslash-continued and a line-based scan sees neither the value nor
  the `expect_error`. That direction fails safe; the same blindness fails open
  on a real multi-line offender. Proved: without the join, those three become
  false positives.

  The guard also asserts that it swept **every** file containing such a call,
  not merely that it swept something. Its globs are `test/*.sh` and `bench/*.sh`,
  which do not match `test/selftest/*.sh` or `test/pbt/*.sh`, so it was complete
  by accident of the tree's current shape rather than by construction. Comparing
  the files that contain a call against the files actually read fails loudly the
  day one appears anywhere new, including a directory nobody predicted, which a
  wider glob cannot do.

### Changed

- `docs/configuration.md` now says why `pgcolumnar.enable_group_vectorization`
  is off by default, which #755 records as not visible from outside the code.
  Measured rather than reasoned: on 4,000,000 rows in 200,000 groups the setting
  is worth 781.0 ms to 403.7 ms with identical answers, so performance is not the
  reason. That is the minimum of seven interleaved pairs on a non-assert build; an
  earlier figure here ran all of one arm and then all of the other, which on a
  contended host attributes drift to whichever arm ran second. An assert build
  measures about 2.2 instead, because `AllocSetCheck` runs per context RESET and
  the ordinary `Agg` resets far more contexts than the vectorized fold, so the
  skew lands one-sidedly on the slower arm. `src/columnar.h` also called the cap
  a plan-time one, which contradicted the code and the new section. The reason is the failure mode. The grouped path builds a hash table
  that does not spill, `pgcolumnar.groupagg_max_groups` bounds it, and the cap is
  checked during execution against the real group count because the plan is fixed
  by then and cannot fall back to an ordinary `Agg`. On a table with 1,500,000
  distinct keys the same query succeeds with the setting off and raises
  `54000 grouped vectorized aggregate exceeded pgcolumnar.groupagg_max_groups`
  with it on. A default would carry that to tables nobody chose it for, where the
  failure arrives when the table grows rather than when anything changes.
- The reader now reuses one row-group buffer instead of allocating and freeing
  one per row group (#768). It materializes the whole group into a single
  buffer, sized `stripe_row_limit` x row width, which at the default
  `stripe_row_limit = 150000` is about 19.8 MB for a 128-character text column.
  Allocated per group that is far past `ALLOC_CHUNK_LIMIT`, so it is its own
  malloc block, glibc returns it to the kernel when the group context is reset,
  and the next group faults every page back in.

  Measured on the same cluster, the same table and the same query, with only the
  installed library changing (1,000,000 rows of 128-character text,
  `stripe_row_limit = 150000`, vectorized paths off so the scan really decodes):

  | | minor faults per query | time |
  | --- | ---: | ---: |
  | before | 57,846 | 143.0 ms |
  | after | 4,838 | 85.8 ms |

  The trade is resident memory: the buffer stays at the largest group's size for
  the life of the read state rather than being returned between groups. Only
  pages a scan actually touched are ever resident either way, since a projected
  read still reads only the columns it wants, so the cost is bounded by what the
  scan already touched.

### Added

- The documented CDC recipe is now tested end to end, and the decision behind it
  is recorded rather than implied (#754). `test/logical_decoding_source.sh`
  already pinned the limitation: a columnar table emits no decodable change.
  Nothing pinned the workaround `docs/user-guide.md` offers in its place, which
  is to capture rows into a heap table with a row trigger and decode that.

  That recipe makes four factual claims, none of which was checked. Row triggers
  fire on a columnar table and see the same rows a heap table would. An `UPDATE`
  arrives as one `UPDATE` rather than as the storage's internal delete and
  insert, which is the claim a CDC consumer would be broken by and the one most
  likely to be false. The capture is transactional. The capture table decodes.
  `test/logical_decoding_cdc_recipe.sh` EXTRACTS the recipe from the guide at run
  time and executes it, so the guide is the thing under test, and asserts all
  four, plus the guide's warning that `FOR ALL TABLES` really does
  pick up the `pgcolumnar` schema, and its cost claim of one heap row per changed
  row.

  `docs/limitations.md` now states plainly that this is a decision and not an
  open item: emitting a decodable change for a columnar write needs a WAL record
  type carrying tuple structure for columnar data, which is a new WAL semantic
  and so out of scope by the same rule that keeps the extension installable on a
  stock server.
- `pgcolumnar.vacuum_sorted()` now self-gates: when the relation is already
  exactly the requested lexicographic run with nothing appended and nothing to
  reclaim, it skips the rewrite instead of materializing every live row through
  a tuplesort again. `pgcolumnar.recluster()` has had the ordering half of this
  since #415 so a scheduler can call it speculatively;
  `design/ISSUE_415_AUTOVACUUM.md` promised the mirror here.

  The mirror is not a copy, and that is the whole of the change.
  `vacuum_sorted` has two jobs: it orders the live rows and it physically
  reclaims deleted-row space. The obvious gate -- "is it already in this
  order", which is complete for `recluster` -- would answer yes on a relation
  that is in order and full of dead rows, and silently stop reclaiming. Ported
  unchanged into a tree carrying it, that gate leaves 10,000 deleted rows
  stored where the un-gated call reclaims all of them, with no error and no
  report.

  So the skip condition is "already in this order AND there is nothing to
  reclaim": a `lexicographic` run (a `zorder` run over the same columns is not
  this order, because Z-order over two or more columns is not a sort on any one
  of them), over exactly these columns in this order, covering every row group,
  with no deleted row and no empty group. Anything else falls through to the
  full rewrite, so re-sorting by a new key, re-sorting a Z-ordered table, and
  reclaiming all still work.

- The columnar scan now tells the planner the order a sorted rewrite left the
  rows in, so `ORDER BY` on that key plans no `Sort`. Every `pathkeys` field in
  the tree was `NIL`, so a table that `pgcolumnar.vacuum_sorted` had physically
  ordered still paid a full sort to be read in that order, and
  `ORDER BY ... LIMIT n` could not stop early.

  Measured on 4,000,000 rows in 27 row groups, `vacuum_sorted('t','k','j')`,
  `sort_status` reporting key `{k,j}`, 27 sorted groups, 0 appended, 0
  inversions on `k` in scan order. Instructions retired by one pinned backend,
  same query, `pgcolumnar.enable_sorted_pathkeys` off against on, and both arms
  return byte-identical answers:

  | query | Sort (off) | no Sort (on) | ratio |
  | --- | ---: | ---: | ---: |
  | `ORDER BY k, j LIMIT 10` | 5,545,800,871 | 14,133,342 | 392x |
  | `ORDER BY k, j OFFSET 3999990` | 8,395,455,193 | 3,392,835,665 | 2.47x |

  The second row is the whole relation consumed, so it is the sort itself:
  2,099 instructions a row against 848, about 1,251 a row that the sort was
  costing. The first is the early stop the sort made impossible. Re-run with the
  arm order reversed the four figures reproduce within 0.8%.

  **A claim the rows do not satisfy is a wrong answer, not a slow plan**, since
  the `Sort` that would have fixed the order is gone. The claim is therefore
  refused unless all of: the last ordering rewrite was lexicographic and
  recorded itself as such (#758), so a Z-order run and an unknown one are
  refused rather than guessed at; every row group lies inside the recorded run,
  so one appended or updated row retracts it; no sort column is collatable; and
  the requested ordering is a prefix of the recorded key, ascending, NULLS LAST,
  which is what the rewrite's own tuplesort applies.

  Collatable sort columns are refused because only the column names are
  recorded. `ALTER TABLE t ALTER COLUMN k TYPE text COLLATE "en_US.utf8"` on a
  column already `text COLLATE "C"` needs no transformation, so PostgreSQL
  updates `pg_attribute` and leaves every row where it is: same storage, mark
  intact, ordering silently a different one. A text sort key therefore gets no
  ordered path. Lifting that needs the collations recorded beside the names.

  The claim lives in a plan, and no catalog object the plan cache watches
  changes when rows are appended, so a group written outside the run now
  invalidates the relation's cached plans. Without it a prepared
  `ORDER BY k NULLS LAST LIMIT 5` answered from the ordered run alone after 600
  rows were appended below it. It fires once per row group and only on a
  relation that has a mark.

  Only the serial scan carries the claim. The parallel partial path and the
  projection path keep `NIL`: workers finish in any order, and a projection is a
  separate layout with its own sort key.

  A query that could not use an ordering does not pay to find one out. Deciding
  whether to claim reads the whole row-group list, and a query with no `ORDER BY`
  and no mergejoinable clause would have had any claim discarded at the end
  anyway, so `has_useful_pathkeys` is asked first. Measured as buffers touched
  during planning, on 1,000,000 rows in 1,000 row groups, a relation marked
  lexicographic, `SELECT count(*) FROM t WHERE j = 3`: 142 with the feature on
  against 121 with it off before, and 121 against 121 after. The query that can
  use the ordering still reads, which is what stops that from being satisfied by
  a function that reads nothing.

  New GUC `pgcolumnar.enable_sorted_pathkeys`, on by default. New suite
  `sorted_pathkeys`, 108 checks. (#751)

- A predicate on `date_trunc(unit, ts)` now drives chunk-group skipping for the
  ORDERED comparisons too, not only equality. #739 inverted `=` and declined
  every other operator, so `date_trunc('day', ts) >= '2024-02-01'` still read
  every chunk group, which is the shape a time-series filter usually takes.
  Monotonicity is what makes the ordered operators invertible, so they come from
  the same unit table for one scan key each rather than equality's two. Measured
  on the existing 500,000-row time-clustered fixture, chunk groups removed by the
  filter, `date_trunc` form against the explicit range it is equivalent to:

  | predicate | before | after | the equivalent range |
  | --- | ---: | ---: | ---: |
  | `date_trunc('day', ts) >= c` | 0 | 30 | 30 |
  | `date_trunc('day', ts) > c` | 0 | 31 | 31 |
  | `date_trunc('day', ts) < c` | 0 | 19 | 19 |
  | `date_trunc('day', ts) <= c` | 0 | 18 | 18 |

  Two cases carry their own risk and their own arms. With the constant on the
  LEFT the strategy has to be commuted, because `c < f(ts)` is `f(ts) > c`; for
  equality that was a no-op, which is why it never came up before. And an
  untruncated constant, which equality treats as unsatisfiable, is satisfiable
  here and moves the bound to the next bucket boundary instead of emptying the
  result. `timestamptz` stays declined for the reason #739 gives. (#403)

- A predicate on `date_trunc(unit, ts) = constant` now drives chunk-group
  skipping. A zone map holds `ts`, so a predicate about a function of `ts` could
  exclude nothing and the scan read every group. It is rewritten to a range on
  `ts`, which prunes with the machinery that already exists. Measured on 500,000
  rows clustered by time in 50 chunk groups: the equivalent explicit range read
  2 groups, the `date_trunc` form read all 50, and it now reads 2. The derived
  keys are conservative, so the executor still applies the original clause. Only
  the `timestamp` form is rewritten: `date_trunc` on `timestamptz` truncates in
  the session time zone, so a key frozen at executor start could be wrong if the
  zone changed. Units outside a known list, and a constant that is not itself
  truncated, are declined rather than approximated. A new suite,
  `preimage_rewrite`, measures the pruning and pins each declined shape. (#403)

### Added

- The vectorized aggregate now accepts a target list that CONTAINS aggregates,
  not only one that IS them. It required every entry to be a bare aggregate, so
  an entry that merely contained one fell off the path entirely, and on an
  unfiltered columnar table that is the difference between answering from the
  zone maps and scanning the whole relation. Measured on 8,000,000 rows:

  | query | before | after |
  | --- | ---: | ---: |
  | `count(*)::text` | 634.1 ms | **0.057 ms** |
  | `avg(a)+avg(b)` | 329.5 ms | **0.504 ms** |
  | `max(a)-min(a)` | 245.4 ms | **0.430 ms** |
  | `round(avg(a)::numeric, 2)` | 230.9 ms | **0.488 ms** |

  Two to four orders of magnitude, and every shape that was losing is ordinary
  SQL: a cast on a count, a difference of two aggregates, a rounded average.
  `count(*)` itself was already 0.030 ms, so wrapping it in a cast was costing a
  full scan of the table.

  The node is unchanged and still emits one bare aggregate per output column. The
  aggregates are pulled out of the target list, the node produces those, and a
  projection above it computes the expressions, which is how core's own
  `Agg` relates to an upper target. A target list that is already exactly the
  aggregates keeps its previous plan with no projection added, so nothing that
  worked before is planned differently.

  The parallel arm needed no change and never did: it uses core's partial
  grouping target, which is bare aggregates however the final target is shaped.
  Refusing the shape in the serial gate was killing the parallel arm with it.

  Unsupported aggregates, aggregates over expressions, and grouped queries
  decline exactly as before. A filter still routes to the scan-fold path behind
  `pgcolumnar.enable_ungrouped_vector_agg`, which is off by default; with it on,
  a wrapped aggregate takes that path too. New suite
  `vector_agg_tlist_shape`. (#755)

- A suite for the claim under `docs/limitations.md`'s "Replication and backup":
  that a columnar table is not a logical decoding source. The whole of #754 rests
  on that sentence and nothing asserted it. The only other logical-replication
  coverage, `logical_subscriber`, tests the opposite direction, a heap publisher
  into a columnar subscriber.

  Measured through a `test_decoding` slot created before any row is written, on a
  heap table and a columnar table of identical shape holding the same 50 rows:

  | | decoded changes |
  | --- | ---: |
  | `public.heap_t` (control) | 50 |
  | `public.col_t` (columnar) | **0** |
  | `pgcolumnar.*` (internal) | 8 |

  The heap control is the load-bearing arm, because zero decoded changes is also
  what a broken slot, a mis-built plugin or a query against the wrong slot looks
  like. The second documented behaviour is asserted too: the slot is not silent,
  it carries pgcolumnar's own catalog writes, and that churn scales with row
  groups and chunks rather than rows. 100 times the rows gives 8 records against
  10, not 800. New suite `logical_decoding_source`. (#754)

### Fixed

- `docs/limitations.md`'s account of parallel scans stated two things that are
  not true, and gave a cause the arithmetic does not support. It said a columnar
  scan "ships fewer, already-decoded rows" than a heap scan, and that "a heap
  scan on the same shape also flipped" at about half the default
  `parallel_tuple_cost`.

  Re-measured on 4,000,000 rows in a 14 column table with a heap twin holding the
  same rows. The two plans ship the **same** number of rows, 999,987, so the
  Gather charge is identical at 99,999; what differs is tuple width, three
  projected columns against fourteen. And the heap does not flip at half the
  default: columnar turns parallel at 0.060 and the heap at 0.030.

  The effect itself is real and larger than recorded. Over the eleven query
  shapes where the planner chose the serial scan, the parallel plan ran 1.8 to
  2.5 times faster on the median, and in every shape the slowest parallel run
  beat the fastest serial run, by 1.45 times at the narrowest margin. "Narrow"
  turns out to mean narrow in **columns**. Two mechanisms produce it, both now
  stated: `parallel_tuple_cost` is charged per row and is blind to width,
  overstating a narrow row by about 1.9 times at a constant volume of data
  shipped; and the columnar scan cost is two fifths to seven tenths of what its
  real time implies, by an amount that is largest on the narrowest projection.
  (#753)

- Both eager ordering rewrites discarded the ordering they had just applied.
  `pgcolumnar.storage.sorted_by` and `sorted_kind` exist to record which
  ordering a rewrite left behind, and the base schema has always specified them
  as `'zorder'` (`recluster`/`cluster`) or `'lexicographic'` (`vacuum_sorted`).
  Only the online `recluster` wrote them: both eager paths reached the catalog
  through one function that passed `NIL` and `NULL`, and the string
  `lexicographic` had never been written to the catalog at all.

  The two eager layouts were therefore identical in the catalog, and
  `pgcolumnar.sort_status` falls back to the declared `options.sort_by` when the
  recorded key is NULL, so both reported the same thing. Measured on 5,000 rows
  with `sort_by => ARRAY['k']` declared on each:

  | rewrite | `sorted_kind` | `sort_status.sort_key` | inversions on `k` |
  | --- | --- | --- | ---: |
  | `vacuum_sorted('t','k')` | was NULL, now `lexicographic` | `{k}` | 0 |
  | `cluster('t','k','j')` | was NULL, now `zorder` | was `{k}`, now `{k,j}` | 930 |

  Z-order over two or more columns is not a sort on any one of them, so the
  second row reported an order the rows were not in. A single-column key cannot
  show this, because single-column Z-order *is* lexicographic order.

  Two consequences go with it. `sort_status` now reports the applied key rather
  than the declared one on an eagerly clustered table, which is a visible output
  change for anyone reading that column. And #415's recluster self-gate requires
  `sorted_kind = 'zorder'`, so after `pgcolumnar.cluster('t','k','j')` it could
  never fire: an immediately following `pgcolumnar.recluster('t','k','j')` on an
  already-clustered relation paid a full rewrite of every group. It now returns
  0 and rewrites nothing, which is the case the gate was written for.

  A storage ordered by an earlier build still has NULL in both columns and still
  falls back to the declared key, until its next ordering rewrite. A reader that
  must not be wrong about the physical order should require
  `sorted_kind = 'lexicographic'` from `pgcolumnar.storage` and treat NULL as
  unknown, rather than read `sort_status`. An unsorted rewrite continues to
  claim no ordering. New suite `eager_ordering_record`. (#758)

- `pgcolumnar.vm_selftest` and `pgcolumnar.vm_is_visible` accepted any relation,
  including a plain heap table. `vm_selftest` does not only inspect the
  visibility map, it writes an all-visible bit into it, and a wrongly set bit is
  how an index-only scan skips the heap visibility check and returns rows it
  should not. Both functions now refuse a relation that does not use the
  `pgcolumnar` access method, with SQLSTATE `42809`, which is the same code the
  other C entry points use for that condition. Both also check ownership before
  opening the relation rather than after, so a caller who does not own the table
  is turned away before it can request a lock. `test/vm_privilege.sh` is rewritten
  around the change: the ownership boundary is asserted on a columnar table, where
  it still applies, and a heap table asserts the stronger new guarantee that no
  caller reaches its visibility map at all. (#748)

- Five entry points requested a relation lock before checking that the caller
  owns the table. `pgcolumnar.add_projection` takes a `ShareLock`, and
  `drop_projection`, `recluster`, `compact_rewrite` and `compact` take a
  `ShareUpdateExclusiveLock`; each opened the relation first and validated
  ownership after. An unprivileged caller could therefore queue in the lock
  manager on a table it has no rights to, blocking readers and writers until it
  was refused. Measured against a held `AccessExclusiveLock`: the call reached a
  four second lock timeout before the ownership error, where it now returns the
  ownership error immediately. Ownership is now checked before the lock, which
  is the ordering `pgcolumnar.vacuum`, `vacuum_sorted` and `cluster` already
  used. `drop_projection` also reported whether an arbitrary relation was
  columnar to a caller who does not own it; it now reports only that they are
  not the owner, matching the four entry points beside it. (#749)

- The columnar scan resolved `pgcolumnar.zone_map` once per chunk group per
  predicate column instead of once per scan. Every group a predicate could
  exclude ran a relation open with a lock and two catalog name lookups, then
  closed again; the reuse cache that the write path uses for the same catalogs
  is gated on a flag only the write path sets, so the read path never reached
  it. The scan now holds the relation and the resolved index for its own
  duration. Measured at 640 chunk groups with one predicate column, the same
  binary with the session bypassed, backend instructions pinned to one PMU and
  normalised by completed queries: 29,694,459 per query before and 26,676,590
  after, a saving of 4,715 per chunk group and 1.113x on the query. That is 15%
  of the cost #744 measured for locating the surviving groups at that size, so
  the systable index probe, which stays inside the loop, remains the larger
  part. Buffer counts are unchanged, which is the expected shape: a catcache or
  relcache lookup reads no buffers once warm, so the buffers a probe costs are
  the index scan. (#744)

- `pgcolumnar.set_options` refused a non-columnar relation with SQLSTATE `P0001`
  rather than `42809`. plpgsql's `RAISE EXCEPTION` defaults to `P0001` unless an
  `ERRCODE` is given, and the guard shipped without one, so the identical message
  `relation "..." is not a columnar table` carried one SQLSTATE from this
  function and another from the C paths, which raise it with
  `ERRCODE_WRONG_OBJECT_TYPE`. A caller keying on SQLSTATE, which is what this
  project's own privilege suites do deliberately, got different answers depending
  on which path refused it. The guard now sets `wrong_object_type` explicitly and
  `audit.sh` asserts the code. (#757)
- A `date_trunc` predicate within one bucket of the end of the `timestamp` range
  raised `ERROR: timestamp out of range` on a columnar table instead of
  answering. The rewrite computes the next bucket boundary as `lo + step`, and
  that addition throws near the maximum, which fails the whole query rather than
  declining to prune. Reachable on `main` before this release through equality
  alone: `date_trunc('year', ts) = timestamp '294276-01-01'` errored while the
  heap returned the row. The rewrite now declines within one step of the end.
  Infinities are unaffected, because interval arithmetic on them saturates rather
  than overflowing. (#403)

- The nightly coverage report now measures something. It had never captured any
  coverage: the job failed every night from the night it was added on
  2026-07-31, always at `lcov capture produced nothing`. `test/run_coverage.sh`
  runs under `sudo` in CI, so the instrumented build is owned by root, while the
  harness runs the server as `postgres` whenever it is root. gcov writes each
  `.gcda` beside its object, as the process that ran the code, so the backend
  could not create one and there was nothing to capture. The counter directories
  are now redirected with `GCOV_PREFIX` to a directory under `/tmp`, which is
  writable and traversable whoever the server runs as, and returned beside their
  objects before the report is built. Making the object directories writable was
  tried first and is not sufficient: creating a file also needs execute on every
  ancestor, and in CI the tree sits under the runner's home. Measured in a
  configuration with an ancestor the server user cannot traverse, which defeats
  the ownership fix: 0 counters before and 33 after, through `lcov` and `genhtml`
  to a report. The runner also refuses a run that captured no
  counters, and says where they should have been, instead of leaving `lcov` to
  report an empty tree as a tool failure. The per-suite logs are uploaded, so a
  suite that fails inside this job no longer has its detail discarded when the
  runner is torn down. (#740)

- Five differential assertions named an `ORDER BY` they could not fail on. The
  oracle in `test/lib.sh` hashes `string_agg(_row::text, chr(10) ORDER BY t)`,
  which sorts the rendered rows before comparing them: two results holding the
  same rows in opposite orders hash identically. That is the right comparison for
  the ~150 call sites that do not name an order, and no comparison at all for the
  five that did: two in `differential.sh`, one in `native_format.sh`, and two in
  `sorted_projection.sh`, whose whole subject is sorted output. Measured on the
  oracle expression itself: five rows forward and the same five reversed both
  hash to `2603e60e802d02d5370794d279cb522a`, while a genuinely different row set
  hashes differently, so it was order-blind rather than broken. A second oracle,
  `pgc_seq_hash`, hashes `ORDER BY row_number() OVER ()` and so keeps the query's
  own output order; `diff_query_ordered` is its comparison helper, and the five
  sites now use it. It keeps the `EMPTY` and unique `QUERY_ERROR.$seq` sentinels,
  so empty-versus-empty and error-versus-error still cannot pass vacuously
  (#418). A new `test/selftest` part enforces the split in both directions: a
  `diff_query` whose query names an `ORDER BY` now fails the harness self-test,
  as does a `diff_query_ordered` whose query names none.

- The extension-upgrade guard now runs in CI, and had never verified an upgrade
  before. `test/extension_upgrade.sh` catches a break that is invisible until a
  user upgrades: a renamed C link name leaves every existing install pointing at
  a symbol the new library no longer exports. No workflow set `PGC_RUN_UPGRADE`,
  so it ran nowhere, and the one runner that did reach it, the coverage report,
  discovered it through `not_a_suite()` and invoked it with no old source, then
  counted the resulting environment shortfall as a failed suite. A nightly job
  now builds the previous release on PostgreSQL 18, loads data into it and
  upgrades it in place, and both upgrade suites are excluded from the coverage
  runner together. Running it also exposed a defect in the suite itself: every
  connection used `psql -h /tmp`, while the socket directory is decided by how
  PostgreSQL was built, `/tmp` for a source build and `/var/run/postgresql` for
  the Debian and PGDG packages, so the suite had never been runnable against a
  packaged server and reported the connection failure as "old install did not
  store rows". It now states the directory it connects to. (#741)

- `pgcolumnar.set_options` accepted a relation that is not columnar, and silently
  recorded options for it. The row was not merely useless, because options are
  read by the columnar writer and a heap table has none. It also leaked: the object
  access hook that clears `pgcolumnar.options` fires only for columnar relations,
  so the row outlived the table. Measured on one cluster: `set_options` on a
  `USING heap` table stored a row, `DROP TABLE` left it behind, and `regclass`
  then rendered as the bare oid, which a later relation reusing that oid would
  inherit; the identical sequence on a columnar table cleaned up. `set_options`
  now raises `relation "..." is not a columnar table`, matching the wording the C
  paths already use, with a hint pointing at `ALTER TABLE ... SET ACCESS METHOD`.
  The test is on the access method **and** on `relkind`, which is what makes it
  match the cleanup rather than merely look strict: the drop hook returns before
  it examines the access method for anything that is not an ordinary table, so
  `'r'` is exactly the set whose options row can ever be cleared. That matters
  from PG17, where a partitioned table may itself carry an access method: an
  access-method test alone accepted a partitioned parent, which has no storage,
  which the writer never writes, and whose row the hook would never clear.
  Measured on 17.6: accepted, one row recorded, still present after `DROP TABLE`
  keyed to the dropped oid, while an ordinary columnar table in the same run
  cleaned up. PG16 and earlier refuse `PARTITION BY ... USING pgcolumnar`
  outright (checked on 16.14). Partitions themselves are ordinary tables and are
  still accepted, which is where the options belong. A materialized view is
  refused for the same reason: `CREATE MATERIALIZED VIEW ... USING pgcolumnar`
  works and its rows read back, but options recorded for one are inert, measured
  against a live fixture that moves the same measurement from 3 to 21 on an
  ordinary table, and its row leaks on `DROP` exactly as the others do.
  That conversion keeps the relation's oid (measured), so the one workflow that
  might have wanted the old order, options first and convert second, is served by
  setting them after the conversion. Nothing in the test corpus called
  `set_options` on a non-columnar relation. The guard is mirrored into the
  `1.0-alpha` → `1.0-alpha2` upgrade script, so an upgraded catalog still matches
  a fresh install. (#432 follow-up)

- The vectorized aggregates now bound their per-execution memory with one
  mechanism instead of two. The scan-key context added for #717 covered the
  scan keys; the scratch context added for #727 covered the whole scan and so
  covered the keys as well, on the ungrouped node. The grouped node now has the
  same scratch wrap, the two Begin-time eligibility checks build in a temporary
  context they delete, and the scan-key context is gone. No behaviour changes
  and no measurement moves; what changes is that one thing owns the lifetime,
  and the regression check for it can name what enforces it again. (#717, #727)

- A columnar scan no longer grows query memory on every rescan. A rescan reuses
  the read state rather than closing and re-opening it, and each restart rebuilt
  the row-group list and its per-group metadata into the read state's own
  context without reclaiming the previous one, so a LATERAL or parameterized
  scan accumulated one list per outer row. The list now has a context the
  rescan resets. Measured against the same query over a heap table with
  identical data, so the figure is what pgcolumnar adds rather than what
  re-executing any node costs: 198 bytes per rescan against a 33 byte heap
  floor before, 33 against 33 after. (#734)

- A plain `EXPLAIN` of the ungrouped vectorized aggregate now reports the
  filters it pushes down. The counts were assigned after the node's
  EXPLAIN-only return, so a plain plan printed zero for both
  `Columnar Pushed-Down Filters` and `Columnar Vector Predicates` however many
  there really were, while `EXPLAIN ANALYZE` of the same query reported them
  correctly. A hard zero reads as pushdown not happening rather than as a number
  that was never filled in, and it left the two vectorized nodes disagreeing
  about one label, since the grouped node has always counted before its own
  return. Both counts are catalog and plan work only. (#726)

- The FSST decoder now checks for interrupts inside its decode loop. Every
  other value decoder already did. The loop is bounded by the encoded length
  of one vector, so this is a latency bound rather than the uncancellable hang
  the Thrift and Avro skip loops had, but a large vector could still hold a
  backend past a cancel or a `statement_timeout`. The check uses an iteration
  counter rather than a byte offset, because the stride is a mask and a code
  that advances the pointer by two can step over the exact multiple and never
  fire. `decode_interrupts` now covers this decoder, and asserts the check sits
  in the decode loop rather than in the bounded symbol-table parse above it.
  (#712)
- The Parquet level-width helper no longer performs a signed left shift that
  is undefined for a maximum level at or above 2^30, and no longer has two
  copies. It computed `1 << b` with `b` reaching 31; real schemas cannot get
  there, because levels accumulate one per nesting level from a bounded
  recursion, but it was undefined behaviour in a decoder that reads files it
  did not write. Making the shift unsigned is not the whole fix on its own: it
  makes the comparison unsigned too, so a negative maximum converts to a huge
  value and the loop runs to a 32-place shift that is undefined in turn, and
  does not terminate under a recovering sanitizer. The helper now returns
  early for a non-positive maximum and bounds the loop. It also moved to
  `columnar_parquet_format.h`, which the writer and the reader already share,
  because a writer and a reader that disagree on a level width produce a file
  that is silently wrong rather than one that fails to parse. Values are
  unchanged on every reachable input. A new suite, `parquet_level_width`,
  pins the parts no query can reach. (#710)

- The Iceberg name-mapping parse reclaims its per-entry scratch instead of
  holding it for the whole document. Walking one entry allocates a `JsonbValue`
  per container lookup, and none of it is needed once that entry's names are
  copied out; left in the caller's context it accumulated across the mapping.
  Measured as memory attributable to pgcolumnar above the cost of core parsing
  the identical string: 100 bytes per entry before, 3 after, which is 78 MB
  down to 4 MB over an 800000-entry mapping. The larger part of such a
  document's cost is core's own `jsonb` parsing and is unchanged; an
  unprivileged session can reach more of it with a plain cast, so this is a
  bounded improvement to one loop and not a limit on what a mapping can cost.
  A new suite, `iceberg_name_mapping_memory`, measures the difference against
  core's own parse rather than a total. (#731)

- The Iceberg name-mapping parse sizes its first allocation by the name cap
  rather than by the element count the file declares. The cap
  (`ICE_MAX_NAME_MAPPING`, 100000) was applied inside the loop, after an
  allocation taken from the document, so a mapping declaring N entries asked
  for 12N bytes up front however few names it actually held. The surplus is
  never written, so it costs address space rather than resident memory, and
  the effect on peak RSS measured 28 kB out of 1196 MB; this is
  defense-in-depth rather than a memory saving, independent of the 64 MB
  metadata bound several layers away that was the only other thing limiting
  it. `iceberg_name_mapping` gains arms at and one over the cap, built as a
  single entry carrying many names, which is the shape where the opening
  capacity and the real ceiling are furthest apart. (#711)

- The object-store write path (export sink, delete, list) now refuses a URL
  with userinfo (`user@host`) with the same error the read path always gave.
  Before, the write parse admitted the URL and failed closed downstream by
  accident: the allow-list cannot match a host with an embedded `@`, and a
  `user:pass@` URL split at the wrong colon into a rejected port. A new
  suite, `objstore_userinfo`, pins all three entry points on the SQLSTATE
  and the guard's own message. (#706)
- The debug metadata mutators (`pgcolumnar_debug_advance_reserved_offset`,
  `pgcolumnar_debug_set_metapage_version`) are now owner-only, checked
  before the table is opened like the other maintenance verbs. They ship
  unbound, but a binding is one `CREATE FUNCTION` away, and one of them can
  overwrite a table's metapage version and brick every later read. A new
  suite, `debug_hook_privilege`, binds them the way the test suites do and
  proves the gate as a non-owner role, by SQLSTATE and the owner message.
  (#707)

- A merge join over index-fetched columnar rows no longer aborts with `pfree is
  not supported by the bump memory allocator` on PostgreSQL 17 and later. A
  columnar index scan returns a deferred slot, and when a sort materialises it
  the current context is PostgreSQL 17's bump context, which forbids `pfree`. The
  deferred decode pfreed there twice: the needed-column `Bitmapset`
  (`bms_add_member`/`bms_free`) and, on a storage's first fetch, the
  format-version check's catalog scan (which frees a btree search stack). Both
  now run in a private pfree-supporting context; the decoded values are
  unaffected because they were already copied into the caller's context, where
  `palloc` is legal. Covered by a new suite, `native_fetch_sort_context` (#720).

### Added

- `IN (...)` and `= ANY(array)` predicates now drive chunk-group skipping.
  The scan-key builder derives a conservative `[min, max]` range over the
  array's non-NULL elements. A single-valued list becomes an equality key,
  which keeps the bloom-filter probe. A parameterized array on a generic
  plan is frozen at executor start, like scalar parameters. That includes a
  mixed list such as `IN (1, $1)`. A correlated array is not frozen and
  stays correct. A new suite, `native_saop_pushdown`, proves the pruning,
  the conservativeness, and both freeze rules. (#704)

- The cross-engine benchmark arms are now reproducible from the repository.
  `bench/build_timescaledb.sh` pins TimescaleDB 2.29.0 with the exact cmake
  options recovered from the original benchmark build, and `bench/build_citus.sh`
  pins Citus v14.1.0, each built against one explicit `pg_config` and refusing an
  assert build. `bench/provision.sh check` continues to report their presence;
  installing them is now a scripted, pinned step instead of a manual one (#702).

### Changed

- The vectorized aggregate no longer grows query memory on every rescan. The
  node re-executes for each outer row of a LATERAL or parameterized sub-scan,
  and what it allocated in the caller's context each time, the per-row value and
  null arrays, the projected set on the metadata path, and what the flushes and
  the reader left behind, lived until the query ended. It now runs in a scratch
  context released when the scan returns. Measured against the same query over a
  heap table with identical data, so the figure is what pgcolumnar adds and not
  what re-executing any node costs: 376 bytes per rescan against a 33 byte heap
  floor before, 31 against 27 after. The ordinary columnar scan beneath it has a
  separate per-rescan cost that this does not change. (#727)

- The vectorized aggregates no longer stack a fresh set of pushdown scan keys
  in query memory on every rescan. The scalar scan builds its keys once at
  Begin; these paths rebuild them at execution, and did so into a context that
  lives until the query ends, so a LATERAL or parameterized aggregate sub-scan
  grew with the rescan count. An `IN (...)` key made it material, because
  building one also detoasts the array constant and deconstructs it. Measured on
  a LATERAL aggregate with a 20000-element list: adding the list cost 179,222
  bytes of query memory per rescan before, and nothing measurable after, against
  a no-list control that is unchanged. The keys now live in a per-node context
  that is reset at each build. A new suite, `vector_agg_rescan_memory`, measures
  the two arms and asserts the difference. (#717)

- The grouped vectorized aggregate (`pgcolumnar.enable_group_vectorization`)
  now folds column at a time instead of row at a time. Where every group key is
  a plain column, every aggregate is one the fold accumulates (`count`, `sum`
  and `avg` over integer and float columns), and the whole `WHERE` is expressed
  exactly by scan keys, the node walks row groups and reads each column's
  packed values directly. The row path paid, for every row scanned,
  `PgColumnarReadNextRow`, an expression context reset, staging the row into a
  virtual slot, `ExecQual`, and one expression evaluation per group key; the
  fold pays none of those, and the group hash and the probe equality are inline
  for integer, `oid`, `date`, `time` and `timestamp` keys. Float keys keep the
  type's own hash and equality functions, because `-0.0` and `0.0` are one
  group with two bit patterns. Measured on 4,000,000 rows with 8 groups, 5
  repetitions, counting instructions retired by the backend: 15,850,896,522
  before and 7,352,675,551 after without a filter (2.16x), and 13,899,374,785
  before and 6,292,504,808 after with one (2.21x). `EXPLAIN` reports
  `Columnar Batch Fold` on the grouped node, as it already did on the ungrouped
  one, so a plan states whether the fold ran. Shapes the fold declines, among
  them a by-reference key such as `text`, an expression key, `min` and `max`,
  and a filter no scan key expresses exactly, run the row path and return the
  same results. A new suite, `native_groupagg_batch`, proves the results
  against a heap mirror and proves the work separately from them. (#708)

- An index or bitmap fetch no longer reads the whole row-group list out of
  the catalog for every row. The list is memoized per command and snapshot,
  with a refresh on any miss, so a group flushed earlier in the same
  statement stays visible. Measured on 3000 index-fetched rows: 6001 catalog
  index scans before, 2 after. The memo resets on subtransaction abort and
  on row-group retirement, and a new suite, `native_fetch_group_memo`,
  proves both by resurrection: through an open cursor after ROLLBACK TO
  SAVEPOINT, and through stale index entries after a same-statement
  compaction. (#709)

### Fixed

- The ungrouped vectorized aggregate (`pgcolumnar.enable_ungrouped_vector_agg`)
  no longer returns wrong results for a filter whose operator has no btree
  strategy, such as `<>`. The batch fold's only per-row filter is the scan-key
  loop, but eligibility only checked that each clause was a strict comparison,
  not that it became a scan key. A `<>` clause is strict yet builds no key, so on
  an all-by-value shape the fold engaged and counted the excluded rows
  (`count(*) WHERE x <> 5` over-counted by one). Eligibility now requires every
  clause to be expressed exactly by scan keys (`PgColumnarQualsExactlyKeyed`);
  conservative prune-only keys (anchored LIKE, and the IN-list ranges added
  above for #704) are marked inexact and also disqualify the fold, which then
  falls back to the correct scalar path. Covered by new cases in
  `ungrouped_vector_agg` (#715).

- A corrupt `column_chunk.page_offset` no longer crashes the backend on a
  whole-group (unprojected) read. The projected read already refused a chunk
  lying outside its row group; the whole-group decode path derived
  `base = nativeBuffer + (page_offset - file_offset)` without the same check, so
  a poisoned offset (or one below the group start, wrapping the subtraction)
  read out of bounds and took SIGSEGV. The containment check now sits on the
  shared decode path, so both reads raise `ERRCODE_DATA_CORRUPTED` and survive.
  The containment test on both the shared and projected paths is written free of
  unsigned overflow, so a `page_offset` of `-1` (storable in the signed `bigint`
  column, read back as `~UINT64_MAX`) is rejected here rather than wrapping past
  the `page_offset + page_length` sum into an out-of-bounds read. New suite
  `native_page_offset_bound` reproduces the crash and the fix, including the
  `-1` wrap on both paths; it closes the `page_offset` gap in `corruption.sh`,
  which only ever corrupted `page_length` through a projected scan.

## [1.0-alpha2] - 2026-08-18

### Added

- The extension packages a `1.0-alpha` to `1.0-alpha2` upgrade script, generated
  from the catalog delta between the two versions (16 new functions, 3 changed,
  two foreign-data wrappers, two `pgcolumnar.storage` columns, and the PUBLIC
  execute revokes on the internal projection and visibility-map functions). A
  convergence test, `native_upgrade_converge`, installs `1.0-alpha`, runs
  `ALTER EXTENSION pgcolumnar UPDATE`, and asserts the result is byte-identical to
  a fresh `1.0-alpha2` across every function definition and ACL, relation, column,
  type, and foreign-data wrapper. The full `1.0-dev` to `1.0-alpha2` path,
  including that an existing columnar table still reads unchanged across the
  C-symbol rename, is covered by a two-library test, `upgrade_from_dev_twolib`.
  This test caught three defects in the generated script before it converged
  (psql command tags captured into the SQL, function definitions concatenated
  without a terminating semicolon, and the ACL revokes omitted because a
  definition-only diff cannot see an ACL-only change).
- The Iceberg REST catalog `FOREIGN SERVER` now accepts a `warehouse` option
  alongside `catalog_uri`. It selects a warehouse on a multi-warehouse catalog
  and is sent as the `?warehouse=` query parameter on the `GET /v1/config`
  request. This completes the per-catalog credential model (issue #656).
  Regression test: iceberg_rest_server.

### Changed

- The native encoding-descriptor wire layout is single-sourced. The descriptor
  is a column chunk's writer-to-reader contract for its per-vector encoding; its
  byte layout was hand-packed in `columnar_write_state.c` and hand-parsed in three
  passes in `columnar_reader.c`, so a field change had to be made in five disjoint
  places or a reader stride would land mid-field. Because the version byte does
  not move on a field change, the version guard would pass and the mismatch would
  surface as `DATA_CORRUPTED` or wrong values, not a clean rejection. A new
  `columnar_encdesc.h` owns the layout (`PgColumnarEncdescPut*` / `ReadEntry`), and
  a `StaticAssert` ties the entry length to the field widths. The on-disk bytes are
  unchanged (verified byte-identical to the hand-packed form). Regression test:
  native_encdesc_golden (pins the layout; catches a field-order change that the
  self-consistent round-trip suites cannot).
- The delete-vector visibility logic is single-sourced. The fold that ORs a row
  group's delete bitmaps into one mask was duplicated in the sequential scan and
  the index-fetch liveness cache, and the per-row bit test was open-coded in five
  places across the sequential scan, the index-only scan, the per-row fetch, and
  the buffered read-your-writes path. A change to one (a new bitmap encoding, a
  bound) could silently diverge the others, invisible until one access path hit
  the changed row. The fold is now `pgcolumnar_merge_delete_vectors` and the bit
  test `dv_row_deleted`; every path routes through them. Behavior is unchanged.
  Regression test: native_delete_visibility_paths (asserts all paths agree on the
  deleted set; a mutation of the shared bit test fails every path).

### Fixed

- Group and per-vector skipping now read only the predicate columns' zone maps.
  `pgcolumnar_native_group_can_match` and the per-vector skip builder read the
  whole group's zone maps (every attribute's min/max) up front and used only the
  columns carrying predicates, so on a wide table a scan that consults one or two
  columns paid min/max buffer traffic proportional to the table width. Both now
  probe per column via `zone_map_pkey`'s `column_index`, exactly as the per-column
  bloom fetch already did; the per-vector spans (identical across columns) come
  from a predicate column, falling back to the whole-group read only when no
  predicate column has per-vector rows. On a 30-column table a one-predicate scan
  went from about 1200 zone-map row fetches to 30, matching the 2-column table.
  Regression test: native_zonemap_narrow.
- Chunk-group skipping now applies to parameterized predicates. A qual like
  `col >= $1` from a prepared statement, PL/pgSQL, or the extended protocol kept a
  `Param` operand, and the scan-key builder accepted only a `Const`, so on a
  generic plan the scan built no key and read every chunk group. Begin now freezes
  execution-stable operands (a `PARAM_EXTERN`, or a subexpression with no `Var`,
  no `PARAM_EXEC`, and no volatile function) into `Const`s before building the
  keys. A correlated `PARAM_EXEC` changes per rescan and is deliberately not
  frozen, so results stay correct; the executor still re-applies the original qual
  to every surviving row. On a 20-group test a `>= $1` generic-plan scan went from
  reading all 20 groups to reading 1. Regression test: native_param_pushdown.
- Reads of the `delete_vector` catalog now use its `delete_vector_pkey` index
  instead of a sequential scan. `PgColumnarReadDeleteVectorList` (called once per
  row group while a scan builds its liveness cache), the reclaim deleted-count
  sum, and `PgColumnarStorageHasDeleteVector` each passed `InvalidOid` to
  `systable_beginscan`, so every call sequentially scanned the whole
  `delete_vector` catalog filtered by scan key. On a table with deletes the cost
  was `O(row_groups * delete_vector_rows)`. The sibling metadata tables already
  index their reads this way; `delete_vector` now matches. Regression test:
  native_delete_vector_index (asserts the reads take the index, seq_scan = 0).
- The Thrift and Avro field-skip loops are now interruptible. A Parquet footer
  whose unknown field is a `list<bool>` with a file-declared count up to
  `0xFFFFFFFF` (or a struct holding many such lists) drove billions of zero-byte
  skips through `PgColumnarThriftSkip`, which checked stack depth but not
  interrupts, so the backend spun uncancellably on a sub-2 KB file reached through
  `parquet_schema()` / `read_parquet()`. `av_skip` had the same gap on an
  `array<null>` manifest block, where its interrupt check ran once per block
  rather than per element. Both now call `CHECK_FOR_INTERRUPTS` on the per-value
  skip path, so `statement_timeout` and cancel apply. This is a denial-of-service
  hardening in the same class as the parallel_copy FIFO fix. Regression test:
  decode_skip_interrupts (functional cancel for Thrift; per-element placement
  shape for both).
- `pgcolumnar.read_manifest_list` now reports a null manifest-list
  `min_sequence_number` as SQL NULL instead of 0, matching `sequence_number`. The
  value is not used by the delete-application rules, so this is a display fix.
  Regression test: iceberg_malformed (#686, #691).
- `pgcolumnar.parallel_copy` no longer hangs the backend when its path names a
  FIFO. `pcopy_open_regular_file` opened the path `O_RDONLY` and then checked for
  a regular file, but a FIFO blocks inside that `open(2)` and the block survives a
  cancel or `statement_timeout`, a denial of service. It now opens with
  `O_NONBLOCK`, rejects a non-regular file, then clears the flag, which also
  closes the stat-before-open race. Regression test: parallel_copy (#686).

- The Iceberg read path now refuses four classes of malformed or hostile table
  metadata that an adversarial re-audit (#644) found it mishandled. A non-regular
  file (for example a FIFO) named as a metadata, manifest, or data path is
  refused before it is opened, instead of blocking the backend in a
  cancel-resistant `open(2)` (an availability denial of service). A manifest-list
  `sequence_number` that is the Avro union's null branch is refused rather than
  decoded as 0, which had understated a data file's sequence number and could
  mis-apply an older delete and drop a live row. A position-delete row with a
  null or negative `pos` is refused rather than silently dropped, which had left
  a row the delete was meant to remove. A `current-schema-id` that names a schema
  absent from the `schemas` array is refused rather than silently resolved to the
  deprecated top-level `schema`, which had bound columns through a stale schema
  and misprojected rows. A v1 table with only a top-level `schema` still reads.
  The same non-regular-file guard also covers `pgcolumnar.import_arrow`, which
  opened its path with the identical unguarded FIFO `open(2)` denial of service.
  Regression tests: iceberg_malformed, iceberg_deletes, arrow_import.
- Concurrent `UPDATE` or `DELETE` of the same columnar row now serializes on the
  row identity, so the losing writer gets a retryable `serialization_failure`
  instead of duplicating the row and losing an update (issue #5, the UPDATE facet).
  Two sessions updating one row without a covering unique index each kept their own
  new version. The fix takes a transaction-scoped advisory lock on the row, then
  re-reads the committed delete vector under a fresh snapshot before writing. New
  GUCs `pgcolumnar.enable_row_update_lock` (default on) and
  `pgcolumnar.row_lock_buckets` (default 1024). Regression test: update_conc.
- The ungrouped batch fold's per-row gather now steps over the columns a query
  references rather than all of a table's columns. On a wide table a scan that
  reads a few columns walked every column per row (twice on a deferred group),
  which is loop overhead proportional to the table width. It now iterates compact
  key/payload lists; a 46-column single-aggregate fold measured about 23% faster
  with no change in results. Regression test: native_batch_fold_projection.
- The grouped vector aggregate's input-scan estimate is now shared with the
  columnar scan node's, via one pgcolumnar_refined_scan_cost helper. Its
  no-serial-survivor fallback used the bare seqscan formula, which omitted the
  projected-width I/O, the per-column decode CPU, and the zone-map survival
  scaling the real scan applies, so it under-priced the node's input on a wide,
  low-pruning scan. Regression test: native_groupagg_wide_cost.
- The Iceberg foreign-data wrapper now pushes projection down: it decodes only
  the columns a query references (from the output list and the recheck quals),
  not every column of every surviving file. A narrow projection over a wide table
  is much cheaper (a 1-of-40-column scan measured about 5x faster). The reader's
  existing needTop mask carries it; the wrapper computes the mask and the results
  are unchanged. Regression test: iceberg_fdw_projection.
- The index-fetch cost penalty now sizes row groups by a relation's effective
  `stripe_row_limit` (the per-table option when set, else the GUC), matching the
  writer and the zone-map survival estimate. It read only the GUC, so it
  mis-priced the row-group decode for a table that set the option, and could steer
  the planner toward or away from an index scan on that table. The effective-limit
  lookup is now one shared helper. Regression test: native_index_fetch_stripe_cost.
- The Iceberg foreign-data wrapper now estimates a scan's row count from the
  manifests (the sum of the live data files' record counts) instead of a constant
  1000. The constant mis-sized every scan and corrupted join planning above a
  large Iceberg table. Regression test: iceberg_fdw_estimate.

### Security

- The native varlena decoder now bounds a value's stored length against its
  buffer. A varying-length value in a column chunk (and a zone map's min/max)
  carries its own length prefix, which `PgColumnarDecodeValue`,
  `pgcolumnar_skip_value`, and `pgcolumnar_build_val_offsets` read and `memcpy`d
  with no check that the value fit; a corrupt chunk or catalog row could then
  declare a value running past the buffer (an out-of-bounds read), and a prefix
  carrying the external-TOAST tag would be detoasted through a bogus pointer. The
  decode sites now pass the value stream's end and read through a bounded reader
  that refuses an over-long length or an external tag with `DATA_CORRUPTED`. This
  is the trusted-storage boundary (bit rot, a hand-written stripe), not a
  file-author-reachable one, but a clean error beats a crash. Regression test:
  native_varlena_bound (a min/max whose header claims ~1 GB now errors instead of
  crashing the backend; a mutation removing the bound reddens it).
- Closed a stat-before-open race in the local file read path. The
  non-regular-file guard that keeps a FIFO from blocking the backend in `open(2)`
  (a cancel-resistant denial of service) screened the path with `stat()` before a
  separate `open()`, so a local principal who can write the directory could swap
  the checked regular file for a FIFO in the window between them and re-introduce
  the block. The five transient-fd local openers (the Iceberg metadata and
  manifest reads, the Avro manifest read, `import_arrow`, and the Parquet source)
  now go through one helper that opens `O_NONBLOCK`, `fstat`s the fd it holds,
  refuses a non-regular file, then clears the flag, so the file checked is the
  file opened. The Parquet source reads positionally with `pg_pread`. The
  parallel-copy partition coordinator (`pcopy_partition_aligned_offsets`), which
  needs a stdio `FILE*` for `getline`, applies the same `O_NONBLOCK` open plus
  `fstat` inline. Regression tests: local_open_race_free, parallel_copy.
- Fixed a backend crash on a hostile Iceberg manifest with a null path. The
  reader decodes a manifest and manifest list against the schema embedded in the
  file, which the table author controls, so a manifest_path (or data or delete
  path) can be declared nullable and encoded null. `ice_rebase` then called
  `strncmp` on the null pointer and segfaulted the backend. A null recorded path
  is now refused as `DATA_CORRUPTED`, matching the read path's other
  malformed-metadata refusals. Regression test: iceberg_malformed (#691).
- Fixed an HTTP request-line injection in the object-store client. A URL path or
  host containing CR or LF was written verbatim into the request line, so a
  crafted path could split the request and smuggle a second line to an
  allow-listed endpoint. The request path and host are now rejected if they carry
  CR or LF, as the caller-supplied header lines already were. Regression test:
  objstore_crlf.

- Fixed an uninitialized-memory read in the native DICT decode path. A chunk
  whose descriptor declared a `value_raw_length` larger than its codes decode to
  left the tail of the raw buffer uninitialized, and a varlena column then read a
  length prefix out of that garbage (silent wrong results, or an out-of-bounds
  read). `decode_dict` now requires the decoded length to equal the declared raw
  length, mirroring the FSST path. Regression test: native_dict_underfill.
- Fixed an out-of-bounds read in the Parquet dictionary decode path. A file whose
  RLE_DICTIONARY data page carried an index with the high bit set (reachable at
  bit_width 32) passed a signed bounds check that sign-extended it to a negative
  int, and the dictionary was then read far out of bounds, crashing the backend
  from a single crafted file. The bounds check is now unsigned and the index is
  rejected. The index decode runs only when the page has coded values, so an
  all-null column with an empty dictionary page still reads. Regression tests:
  native_parquet_dict_oob and native_parquet_streaming.

### Added

- Read-only Apache Iceberg support, filesystem-backed, at a table's current
  snapshot (#388). `pgcolumnar.iceberg_scan(metadata_path)` reads a table given
  a column definition list, resolving each output column to a schema field id so
  a data file written before a column rename still reads. It applies **row-level
  deletes of all three kinds**, each under its own sequence rule: a position delete
  drops the row ordinals it names from a data file whose data sequence number is
  at or below the delete's (same commit or earlier), and an equality delete
  drops every data row matching a delete row on the delete's `equality_ids`
  columns when the data file's sequence number is strictly below the delete's
  (never same-commit data). Format-version 3 **deletion vectors** (Puffin
  files holding a portable roaring bitmap of row ordinals) apply under the
  position-delete rule, scoped to their referenced data file, and supersede
  position delete files for that file per the specification; the blob checksum,
  the manifest/footer offsets, and the recorded cardinality are verified, and
  at most one vector may reference a data file. A null delete value matches only a null data value,
  and columns beyond `equality_ids` do not take part in the match. A
  partition-scoped equality delete is applied within its partition: its stored
  partition values are matched against each data file's, so it removes rows only
  from data files in the same partition. Equality
  deletes with no supported handling are refused rather than ignored, so a table
  using them errors instead of returning rows it should have removed:
  delete columns of
  types outside `int`/`long`/`string`/`boolean`/`date`, delete columns
  dropped from the current schema, and a partition value the reader cannot
  compare exactly. Supporting introspection functions:
  `iceberg_current_snapshot` and
  `iceberg_data_files` (which refuses any delete), and the Avro building blocks
  `read_avro_manifest` and `read_manifest_list`. Only Parquet data files are
  read; recorded paths are rebased onto the table's actual location and refused
  if they resolve outside it. The table may live in object storage: a metadata
  path of `s3://`, `http://`, or `https://` reads the metadata, manifests, data
  files, and delete files from the endpoint through the object-store module,
  gated by the same `objstore_allowed_endpoints` allow-list and ambient
  credentials as the Parquet reader. A data file written outside Iceberg, which carries
  no field ids, is read through the table's `schema.name-mapping.default`
  property, which binds its columns by name; a file with no field ids and no
  such property is refused rather than guessed. `read_parquet` also gained a
  `field_ids` form that projects columns by Parquet field id. See
  [Iceberg](docs/sql-reference.md#pgcolumnariceberg_scanmetadata_path-text-returns-setof-record).

- Read-only Apache Iceberg **REST catalog** support (#388). A table is named by a
  catalog (catalog URI, namespace, table) rather than a metadata path.
  `pgcolumnar.iceberg_rest_scan(catalog_uri, namespace, table_name)` reads it at
  its current snapshot, taking a column definition list exactly like
  `iceberg_scan`: the catalog resolves the table to its metadata location, which
  is then read through the same path, so every projection and delete rule
  applies unchanged. `pgcolumnar.iceberg_rest_table_location` returns that
  resolved metadata location on its own, and
  `pgcolumnar.iceberg_rest_namespaces` and `pgcolumnar.iceberg_rest_tables` list
  a catalog. Requests go over HTTP or HTTPS. The catalog endpoint is subject to
  the same `objstore_allowed_endpoints` allow-list and link-local refusal as
  every other remote access, and is carried by the object-store module, so no
  second TLS stack enters the server process. A bearer token, when the catalog
  requires one, is read from the `PGCOLUMNAR_ICEBERG_REST_TOKEN` server
  environment variable, never a function argument, so it does not appear in the
  statement log. The first argument may instead name a foreign server of the new
  validator-only `pgcolumnar_iceberg_catalog` wrapper (#656). The server holds
  `catalog_uri`, and the current role's user mapping holds the bearer `token` in
  `pg_user_mapping`, which is not world-readable, so one role's token is private
  from another. A role with neither a mapping token nor superuser rights is
  refused, and the validator keeps secrets off the world-readable server options.
  A user mapping may instead carry OAuth2 client credentials (`oauth_client_id`,
  `oauth_client_secret`, and optionally `oauth_scope` and `oauth_token_uri`)
  (#656). The catalog then mints a bearer by the client-credentials grant; the
  secret travels in the request body, never a URL or a log line, and a half
  credential is refused before any request. When the catalog vends short-lived
  storage credentials in its
  `loadTable` reply (the flat `config` keys or the `storage-credentials` array,
  longest prefix selected), `iceberg_rest_scan` reads the table's files with
  those credentials rather than the server environment (#656). Vended
  credentials do not bypass the endpoint allow-list. A table that vends none
  reads with the ambient environment as before. See
  [Iceberg REST catalog](docs/sql-reference.md#pgcolumnariceberg_rest_scancatalog_uri-text-namespace-text-table_name-text-returns-setof-record).

- An Apache Iceberg **foreign-data wrapper**, `pgcolumnar_iceberg` (#388). A
  foreign table over an Iceberg table gets the query's predicate, which
  `iceberg_scan` cannot, and prunes: a predicate on an identity-partitioned
  column removes whole data files before they are opened, reading each file's
  partition value from the manifest. A predicate on an integer or boolean column
  removes whole files whose stored minimum and maximum exclude it, so an
  unpartitioned column prunes too. Pruning is only an optimization, so a
  predicate the wrapper cannot decide never changes the rows returned, and every
  projection and delete rule matches `iceberg_scan`. The table option is
  `metadata_path`; `EXPLAIN (ANALYZE)` reports `Files Pruned`. An equality
  predicate on a `bucket[N]`-partitioned column prunes files whose stored bucket
  differs from the constant's, computed with the Iceberg murmur3 hash. A
  predicate on a `truncate[W]`-partitioned integer column prunes files whose
  truncated value range excludes it, and a predicate on a `day()`-partitioned
  date column prunes by day. The `year()`, `month()`, `day()`, and `hour()`
  transforms prune too, on a `timestamp` or `timestamp with time zone` column
  (and `year()`/`month()` on a `date`): each bucket spans a range, so a file
  whose bucket equals the constant's is read and its rows are rechecked, never
  dropped at the boundary, and a timestamptz value is compared as its UTC
  instant. Partition pruning covers identity, `bucket[N]`, `truncate[W]`
  (integer), and the temporal transforms on date, timestamp, and timestamptz;
  metrics pruning covers integer and boolean columns; other column types read in
  full. See
  [Iceberg FDW](docs/sql-reference.md#the-pgcolumnar_iceberg-foreign-data-wrapper).

- The Parquet read and export functions and the foreign-data wrapper read from
  and write to object storage (#393, #394). A path may be an `s3://`,
  `http://`, or `https://` URL wherever it may be a local path. `s3://` requests
  are signed with AWS Signature Version 4; `https://` verifies the server
  certificate and is available when the object-store module is built with
  OpenSSL. Support lives in a separate module, `pgcolumnar_objstore`, loaded on
  the first remote use. Reads take exact object keys only. `export_parquet` and
  `export_arrow` write to `s3://`, as one request for a small object or a
  multipart upload for a large one, and the object becomes visible only when the
  upload completes. See [Object storage](docs/sql-reference.md#object-storage).

- Credentials for object storage come from the server process environment
  (`AWS_ENDPOINT_URL`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
  `AWS_SESSION_TOKEN`, `AWS_REGION` or `AWS_DEFAULT_REGION`) for the function
  API, and from the catalog
  for the foreign-data wrapper: `endpoint` and `region` on the server, and
  `access_key_id`, `secret_access_key`, `session_token`, and
  `credentials_required` only on a user mapping, so a secret is never in a
  world-readable option. Ambient environment credentials are used only for a
  superuser or a mapping a superuser marked `credentials_required 'false'`.

- `pgcolumnar.objstore_allowed_endpoints` lists the endpoints the object-store
  module may connect to (#393). It is empty by default, which refuses every
  remote endpoint, so a role that can read or write server files cannot reach an
  arbitrary host through the extension. Link-local addresses, including the cloud
  instance-metadata address, are refused whether or not they are listed. The
  setting is superuser-only.

- `pgcolumnar.parquet_schema` reports a `field_id` column (#388), the Parquet
  schema field id each leaf column carries, which formats such as Apache Iceberg
  use to select columns by id. It is NULL when the writer emitted none.

- `pgcolumnar.maintenance_due(rel, compact_due_fraction, recluster_due_fraction)`
  reports whether an online maintenance verb is worth running on a table, from its
  statistics alone (#415). It takes no lock and rewrites nothing. It returns the
  deleted and appended fractions, whether `compact_rewrite` or `recluster` has
  crossed its threshold, and a `recommendation`. This is the policy the
  `pgcolumnar.autovacuum` daemon consults, and a monitoring role can call it
  directly. It is `SECURITY DEFINER` and checks that the caller may `SELECT` the
  table, so the owner can run it without superuser rights.

- `pgcolumnar.autovacuum`, a maintenance daemon for the online upkeep that core
  autovacuum cannot reach (#415). pgColumnar's `compact_rewrite` and `recluster`
  live in extension functions, not table access method callbacks, so core
  autovacuum never runs them. A table's dead rows and clustering decay then
  accumulate until someone runs the verbs by hand. This daemon runs them for you.

  It is off by default. When on, a launcher wakes every
  `pgcolumnar.autovacuum_naptime` seconds (default 60) and starts one worker per
  database. Each worker asks `pgcolumnar.maintenance_due()` which tables crossed a
  threshold, then runs the recommended verb over SPI in its own transaction.

  Two properties make it safe unattended. It calls only the
  `ShareUpdateExclusiveLock` verbs, never `vacuum`, `vacuum_sorted`, or `cluster`,
  so it cannot block a reader or a writer. And it yields the way autovacuum does:
  the worker sets `PROC_IS_AUTOVACUUM`, so the lock manager cancels its
  maintenance the moment a backend queues for a stronger lock. New settings:
  `pgcolumnar.autovacuum`, `autovacuum_naptime`, `autovacuum_compact_threshold`
  (0.2), and `autovacuum_recluster_threshold` (0.05). See the administration
  guide for the operator's view.

- `pgcolumnar.parallel_flush` dispatches a stripe flush across background workers
  (#445). Default off. When on, a flush of two or more columns fans the per-column
  encode and compress work out to a worker pool. Any column a worker does not
  finish is completed serially in the backend, so the stored bytes match the
  serial path either way. It helps one large flush of many numeric columns by up
  to 14 percent. A wide text-heavy flush regresses, and so do frequent small
  flushes, because it copies the buffered bytes through shared memory. So it is a
  per-session opt-in for a wide numeric bulk load, not a default.

- `pgcolumnar.fsst_verdict_reuse` caches a column's FSST keep/drop verdict for a
  bounded number of row groups (#472). Default 16; `0` asks every time, which is
  the behaviour before this setting existed.

  Deciding whether an FSST symbol table pays for itself costs a whole-corpus
  encode plus a compression pass, and the answer cannot be sampled: on a training
  prefix FSST can look 24 percent worse while over the whole column it is 23
  percent better. So it was asked once per column per row group, and for a column
  whose data does not change character that re-derived the same answer for the
  whole load. Measured at 2,000,000 rows in 20 row groups: 2482 ms of a 5319 ms
  `md5` load and 843 ms of a 2081 ms email-shaped load, with the verdict identical
  all 20 times.

  A text load is about 2.5 times faster as a result, measured in-suite at 1623 ms
  against 648 ms. Stored bytes are unchanged for a column whose verdict is stable,
  which is asserted rather than assumed: the suite compares the encoding
  descriptor, block codec and page length of every chunk. A column that changes
  character mid-load is noticed within the bound.

  Reuse is per statement. Nothing is persisted and no on-disk structure changes.

- `EXPLAIN (ANALYZE)` now reports `Columnar Usable Skip Predicates` beside
  `Columnar Pushed-Down Filters` (#479). The existing line counts the filters the
  scan was handed and is unchanged; the new one counts how many of those the
  reader can actually skip chunk groups with. A filter whose types have no
  ordering function for the pair is dropped by the reader and excludes nothing,
  and until now the plan reported it as pushed down with no way to see the
  difference. That is how #477 went unseen for a year, and how
  `test/zonemap_cost.sh` validated a cost discount against a fixture that pruned
  zero groups.

  All three nodes that print the original line report the new one: the scalar
  custom scan and both vectorized aggregate nodes. The new line needs `ANALYZE`,
  since it describes what the scan built at execution.

- `pgcolumnar.analyze()` now collects `most_common_vals` and `most_common_freqs`,
  and excludes those values from `histogram_bounds` (#414). Frequencies are exact
  counts over the total row count rather than sample estimates. PostgreSQL 18 and
  later, which is where `pg_restore_attribute_stats` exists; earlier majors raise
  and should use `ANALYZE`.

  The selection rule is PostgreSQL's own. `analyze_mcv_list()` keeps the entire
  list when the whole table was read instead of applying its significance filter,
  because that filter exists to judge sample frequencies. Reading the column makes
  the values eligible on count alone, matching what core would store given the
  same information.

  Excluding most-common values from the histogram is required rather than
  cosmetic: keeping them counts those values twice in selectivity, once from the
  most-common list and again inside the bucket that holds them.

- `test/analyze_differential.sh`, which compares the statistics `pgcolumnar.analyze()`
  writes against the shape PostgreSQL's own `ANALYZE` produces across five column
  types. `pg_restore_attribute_stats` takes `VARIADIC "any"` and responds to a
  mistyped argument with a warning rather than an error, so a statistic can be
  dropped while the call reports success. Values cannot be the comparison, since
  exact and sampled statistics differ by design, so the suite compares the
  operator, collation and presence of each statistic kind, and verifies every
  stored value against an independent count.

### Fixed

- The Iceberg reader no longer crashes on a malformed manifest (#644). A crafted
  manifest that recorded no data-file path made `iceberg_scan` and
  `iceberg_data_files` dereference a null pointer. A manifest whose Avro record
  schema gave a `fields` element as a JSON array made the schema decoder read past
  an object container. Both now raise a clean error. These reproduce only from
  hand-crafted manifests, since no writer emits them, and they are covered by the
  new `iceberg_malformed` suite.

- A failed `export_parquet` or `export_arrow` no longer leaves a partial file at
  the destination (#394). An export writes to a temporary name and renames to the
  final name only when it is complete, so a reader never sees a half-written file.
  Every write is checked, so a full disk during an export is reported rather than
  detected only at close.

- `EXPLAIN (ANALYZE)` on a vectorized aggregate reports whether the batch fold
  actually ran, not whether it was predicted eligible (#602). A query that fell
  back to the row path, such as an aggregate over a column added after some row
  groups, no longer reads `Columnar Batch Fold: yes`. Plain `EXPLAIN`, which has
  no execution to report, still shows the prediction.

- `pgcolumnar.sort_status` works for a non-superuser who owns the table (#608).
  The function reads pgColumnar's internal catalogs, which carry no `GRANT`. As an
  invoker-rights function it therefore failed for any caller who was not a
  superuser. It is now `SECURITY DEFINER` and checks that the caller may `SELECT`
  the table. The owner can read the sort status of their own table, and no caller
  gains access to a table they could not already read.

- `pgcolumnar.analyze()` counts `null_frac` over live rows (#485). It came from
  the zone maps, which record what was written, so a deleted row kept counting
  toward the denominator until the table was rewritten. `VACUUM` did not correct
  it. On 1,000 rows holding 100 nulls, deleting the 301 rows of one value left
  `null_frac` at 0.100000 against a true 0.143062.

  The size of the error is not the whole of it. `null_frac` came from the zone
  maps while the most-common frequencies came from a live count, so one
  `pg_stats` row carried two statistics normalised against different
  populations: a `null_frac` implying 1,200 rows beside a frequency implying
  900, with 900 actually present. `null_frac + sum(most_common_freqs) + rest =
  1` stopped holding, and `eqsel` subtracts both when pricing everything else.

  The null count now comes from the read the function already performs, so this
  costs no extra pass. It does give up the "null_frac is a metadata read"
  property claimed for #414 slice 1, which cost nothing in practice because the
  function always goes on to read the column for `n_distinct`. A metadata-only
  fast path would need a live-row count, which is that same read. Whether
  `pgcolumnar.zone_map`'s counts should account for the delete vector, which
  would also affect pruning, is a wider question and is not addressed.

- A column declared over a domain now prunes chunk groups (#483). The scan key
  was built and then dropped: a domain column carries the domain's type in
  `pg_attribute` while the constant beside it carries the base type, so the
  comparison looked cross-type, and an operator family has no comparison
  function registered for a domain. Measured on identical values in one table
  over 20 row groups, `int` and `bigint` each removed 19 groups and a domain
  over either removed none, while all three reported the filter as pushed down.

  Answers were never wrong, because the executor re-applies the qual. The cost
  was reading the whole table on ordinary SQL. Both sides of the comparison are
  now resolved to their base types, so a domain compared against a value of a
  different domain over the same base type is also recognised. Ordering and
  hashing are unchanged: the comparison and hash functions were already taken
  from the column type's resolved entry, which is what the writer used to build
  the zone maps and bloom filters.

- A `bigint` column compared against an unadorned integer literal now prunes
  chunk groups (#477). The scan key was dropped because the column type's default
  comparison function cannot take an `int4` argument, so predicates of the form
  `bigint_column > 16000` read every chunk group while `bigint_column >
  16000::bigint` pruned normally. The comparison function is now resolved for
  both types from the column's btree operator family, which supplies exactly this
  for the built-in numeric types. Where a family provides no such function the
  key is still skipped, as before.

  `EXPLAIN` did not show the difference. `Columnar Pushed-Down Filters` counts
  scan keys given to the reader rather than predicates able to exclude a group,
  so it reported the filter as pushed down while nothing was skipped.

  The bloom filter probe remains disabled for cross-type equality. The filter
  stores hashes of column-type values, so hashing a differently typed constant
  would probe a slot that was never written and could skip a group holding
  matching rows.

- `pgcolumnar.analyze()` now honours the per-column statistics target set by
  `ALTER TABLE ... ALTER COLUMN ... SET STATISTICS` (#414). It read the global
  `default_statistics_target` for every column, so a column given its own target
  was sized by the global setting instead. A target of zero means the column is
  not to be analysed at all, and is now respected rather than overridden.

  Requesting only zero-target columns no longer raises. The function reported
  that it had collected statistics for no columns, with a hint about missing row
  groups, which pointed at storage for what was a deliberate setting.

- Renamed the custom scan node from `ColumnarScan` to `PgColumnarScan`, and the
  custom path from `ColumnarAgg` to `PgColumnarAgg` (#428). `ColumnarScan` is
  also registered by **Citus columnar** and by **TimescaleDB 2.29**.
  PostgreSQL's registry is one hash table keyed on that name, so two extensions
  cannot both hold it. Neither failure needed a pgColumnar table; our presence
  in `shared_preload_libraries` was enough.

  With **Citus columnar** the server refused to start at all, in either load
  order:

      FATAL:  extensible node type "ColumnarScan" already exists

  With **TimescaleDB** there was no startup error and serial queries returned
  correct results. TimescaleDB checks the registry first and silently skips
  registering when the name is taken, so a parallel worker then resolved
  TimescaleDB's node through pgColumnar's callbacks, and any parallel query over
  a columnstore hypertable failed with
  `could not read blocks 0..0 in file ...`. That is the more dangerous of the
  two, because nothing announces it.

  **This changes `EXPLAIN` output.** Plans that read `Custom Scan (ColumnarScan)`
  now read `Custom Scan (PgColumnarScan)`. Anything parsing plan text for the old
  name must be updated. The `Columnar ...` property lines, such as
  `Columnar Projected Columns`, are a different namespace and are unchanged.
  `ColumnarAgg` never appeared in `EXPLAIN`: it names a `CustomPathMethods`, and
  the planned node carries the scan's methods (`columnar_vector.c:717`), so that
  half of the rename is hygiene rather than a visible change.

### Changed

- `pgcolumnar.recluster` no longer rewrites a table that is already clustered by
  the requested key (#415). The function records the clustering key and kind it
  establishes. A later call with the same key returns 0 and touches nothing when
  the existing sorted run still covers every row group. Before this, it re-sorted
  on every call, so a scheduled recluster rewrote the whole table each time, which
  is why the maintenance daemon could not have run it safely. `pgcolumnar.sort_status`
  now reports this recorded key as `sort_key`, and falls back to the declared
  `sort_by` when there is no recorded key.

- A columnar scan whose filter cannot be pushed down now skips decoding the
  projected columns of a 1024-row vector that holds no matching row (#452). The
  scan decodes the filter columns first, rules out the vectors with no match, and
  decodes the rest only for the vectors that survive. A `SELECT *` under a
  leading-wildcard `LIKE` that matches few rows then approaches the cost of
  `count(*)`. It no longer decodes every column of every row scanned. A count over
  one column gains nothing, because it has no projected column to skip.

- The writer detoasts each value once per row (#445). It was detoasted once for
  the encoder, once for the bloom filter, and once for each of the two zone-map
  comparisons. For a toasted column each of those was a separate decompression. A
  load of a large compressed text column is about 11 percent faster, and the
  stored bytes are unchanged.

- `pgcolumnar.analyze()` places `histogram_bounds` at PostgreSQL's own positions
  (#414). The bounds were evenly spaced quantiles; core places bound i at
  `values[floor(i * (nvals - 1) / (num_hist - 1))]` among the rows left after
  the most-common values are removed, and `percentile_disc` resolves a fraction
  to a different index whenever the two disagree.

  **This changes the emitted array.** The length and both endpoints are the
  same, so the exactness of the minimum and maximum is unaffected, but an
  interior bound can move by one position. Both forms are valid equi-depth
  histograms; core's is the one the planner's selectivity estimators were tuned
  against. Anyone comparing `pg_stats` across this upgrade should expect
  interior bounds to differ and that is intended, not a regression.

- The unsupported-rewrite error names `REPACK` on PostgreSQL 19 (#399). `REPACK`
  replaces `CLUSTER` and `VACUUM FULL` in 19 and dispatches through the same
  copy-for-cluster path, which pgColumnar does not implement, so a 19 user who
  typed `REPACK` was told that `CLUSTER / VACUUM FULL` was unsupported: two
  commands they had not typed, and on 19 the superseded ones. The message now
  names the command and hints at `pgcolumnar.vacuum()`, which does the work. This
  covers `REPACK (CONCURRENTLY)` too: given a table with an identity index, where
  PostgreSQL will run it, heap succeeds and a columnar table is refused.
- `CREATE TABLE ... USING pgcolumnar AS SELECT` no longer fails when the source
  plan is parallel (#387). The storage-row creation path re-checked for an
  existing row against `GetLatestSnapshot()`, which raises "cannot update
  SecondarySnapshot during a parallel operation" inside parallel mode, and CTAS
  runs its whole executor in parallel mode whenever the source plan is parallel.
  That is the default for any source large enough to be worth loading, so bulk
  creating a columnar table from existing data failed on every supported major.
  The lock and the fresh snapshot are now skipped when the relation was created
  by the current transaction, because no other session can see it and the
  first-writer race they defend against cannot happen. A committed table
  first-written by two sessions at once is unaffected and still serializes.

- The extension's exported C symbols are namespaced under `pgcolumnar` (#382).
  Two extensions that both call themselves `columnar` could define the same
  symbol. `columnar_handler` and `columnar_relation_storageid` collided with
  Citus columnar. Four settings variables such as `columnar_stripe_row_limit`
  also shared names with the same settings there. That case binds one library's
  setting to the other's storage.
- `default_version` moves from `1.0-dev` to `1.0-alpha`, so
  `SELECT extversion FROM pg_extension` now agrees with `VERSION`.
- `CREATE INDEX` decodes only the columns the index needs (#413). The index
  build received an `IndexInfo` carrying the key columns and the expression and
  predicate trees, and discarded it, so a one-column index on a wide table read
  every column. Both readers are now projected: the one a serial build opens for
  itself, and the shared scan a parallel build arrives with, which comes through
  the table-access-method scan interface and has nowhere to carry a projection.
  The parallel branch is not a corner case. With every parallel setting left at
  its default, a 1.5 million row table of incompressible text, 459 MB on disk,
  is built in parallel, so that is the branch a table of consequential size
  takes. On 300,000 rows of 20 columns on PostgreSQL 18, a one-column index
  drops from 568 ms to 73 ms with workers allowed, against 563 ms for the same
  index on a heap table.

- Logical replication into a columnar table says what is wrong and how to
  proceed (#435). Applying an UPDATE or a DELETE takes a row lock, which
  columnar storage does not support, so the apply worker raised "columnar: row
  locking is not supported yet". That names something the user was not doing.
  PostgreSQL takes that lock for every applied UPDATE and DELETE and has no
  lock-free path, and it does not advance the replication origin when a
  transaction fails, so the subscription retries the same transaction for as
  long as it runs and no later change is applied. The error now names logical
  replication, says the retry is unbounded, and points at
  `CREATE PUBLICATION ... WITH (publish = 'insert')`, which does work. See
  [Limitations](docs/limitations.md).

- The grouped vectorized aggregate's parallel arm is no longer declined on a
  truncating time key (#369). `estimate_num_groups` cannot see through a
  function, so for `date_trunc('minute', ts)` it falls back to the timestamp
  column's distinct count, which measured 19,996,000 against 720 actual. That
  number is charged twice on the parallel arm, once by the Gather for tuples it
  believes it must ship and again by the Finalize, and not at all on the serial
  node, which is priced per input row. The serial node therefore won by
  construction on the shapes where the parallel arm is fastest. The estimate is
  now bounded by the number of buckets the scanned time range can span, and only
  when the planner had nothing to estimate from. A plain column, an expression
  index and a user's `CREATE STATISTICS ON (expr)` all count as informed and are
  left alone. Measured on 20 million rows: a one-aggregate windowed query goes
  from 2,017 ms to 497 ms and a ten-aggregate one from 4,687 ms to 1,146 ms,
  while a plain-column key keeps a bit-identical estimate and its existing plan.
  Both settings involved are still off by default.
- The ungrouped vectorized aggregate no longer errors on a varlena filter
  column (#423). `SELECT count(*) FROM t WHERE s LIKE '%x%'` raised
  "unsupported byval length: -1" with
  `pgcolumnar.enable_ungrouped_vector_agg` on. The batch fold gathers each
  projected column with pointer arithmetic on `attlen`, which is -1 for a
  varlena, so the offset and the fetch were both wrong. The eligibility check
  walked the scan keys and asked whether each type was comparable, while the
  gather walks the projected set and needs each type passed by value. A text
  column filtered with `LIKE` is projected and is not a scan key, so it arrived
  unchecked. `uuid` and `name` failed the same way for a different reason: both
  are fixed width, 16 and 64 bytes, but passed by reference, and the gather
  hardcodes by-value. Such a shape now falls back to the row path, which is what
  the ALTER TABLE ADD COLUMN case already did. This was ClickBench q21.

### Upgrading

**Run `ALTER EXTENSION pgcolumnar UPDATE;` in every database that has the
extension, after installing this build.**

The rename moves the C symbol names that each installed function recorded when it
was created. Replace the shared library without this step and those records
point at symbols the new library does not export. The extension then stops
working until the catalog is updated. Reading an existing columnar table fails
with `could not find function "columnar_handler"`.

Nothing happens to your data, and no conversion runs. The upgrade replaces
catalog entries only, and keeps each function's identity, so the access method
binding and every dependency survive. The SQL you write does not change.

See [Upgrade](docs/installation.md#upgrade) for the commands, including how to
list the databases that need it.

## [1.0-alpha] - 2026-08-04

First tagged release. Everything below shipped in it.

### Known limitations

- The grouped vectorized aggregate's parallel arm is declined on shapes with an
  expression grouping key, because the core Finalize is priced off a group estimate
  that can be 25x to 42x wrong (#369). Both settings involved are off by default.
- The by-row-number fetch cache is bounded by `4 x (cap + retained position indexes
  + groupBuffer)` rather than `4 x cap`. On a table of many wide varlena columns one
  entry measured 62 MB against a 32 MB cap (#364). Releasing the position indexes
  with the decoded stream holds the bound but costs 47% in time, so this design
  keeps the speed and records the trade.
- The index-fetch penalty is bounded by a multiple of one full scan rather than
  modelled against the consumer, so a plan that stops early inherits more of it than
  it should (#376). The bound keeps the penalty steering correctly on every shape
  measured; the model is post-alpha work.
- Point lookups remain slower than heap, and the cost of a fetch grows with table
  width, because an index fetch decodes the attribute prefix up to the highest
  column the query reads. See `docs/limitations.md`.

### Added

- `pgcolumnar.parallel_copy(target, path [, workers])` loads a COPY text file into
  a columnar table with several background workers at once, and returns the row
  count (#300). Each worker runs core `COPY` over a byte range of the file, so
  parse and write behavior match `COPY FROM`. The load is atomic through two-phase
  commit: every worker prepares its transaction, and a coordinator commits them
  together only when all succeeded, so any failure rolls the whole load back. The
  target is a single columnar table, where any record-aligned split is correct, or
  a RANGE-partitioned table with columnar partitions, where the file must be sorted
  ascending by the partition key and the key type must be numeric or a date/time
  type. The columnar encode step is CPU bound, so the load scales with worker
  count up to the physical core count. It landed in two parts, partition-parallel
  (#323) and single-table (#324). See docs/user-guide.md and docs/benchmarks.md.

- Column projection reads only the columns a query references, and is **on by
  default** (#339, `pgcolumnar.enable_column_projection`). A columnar scan
  previously decoded every column of every row group it touched regardless of the
  query's target list, which discards the main advantage of the storage format on
  wide tables. Measured on a 100M-row 21-column fixture, a single-column
  aggregate improved 6.9x. The gain is smaller on grouped queries, which also read
  their grouping keys: 1.24x, 1.13x and 3.13x on three TSBS-shaped grouped
  aggregates. Turning the setting off restores the previous behavior.

- Vectorized aggregates, all **off by default** and opt-in while they are proven:

  - `pgcolumnar.enable_ungrouped_vector_agg` folds a plain `SELECT agg(col) FROM t`
    over the decoded column buffer instead of one Datum tuple per row (#337).
  - `pgcolumnar.enable_parallel_vector_agg` makes that fold parallel-aware (#343),
    extended to integer sum and average partials (#346), and to grouped
    aggregates (#366). Each worker claims distinct row groups through a shared
    counter and emits per-worker transition state that a core Finalize combines.
  - `pgcolumnar.enable_group_vectorization` answers `GROUP BY` from a vectorized
    grouped node (#321). `pgcolumnar.groupagg_max_groups` caps its hash table and
    errors with guidance rather than growing without bound.

  These remain off by default because plan selection for them is not settled: a
  grouped query with an expression grouping key such as `date_trunc()` can decline
  the parallel path on a group-count estimate that is 25x to 42x wrong (#369).

- `pgcolumnar.enable_index_fetch_penalty`, **on by default** (#355), prices the
  per-row heap fetch of an index or bitmap path on a columnar table. A columnar
  fetch decodes the row group the row lives in, while core prices it as a page or
  two, so an unclustered ordering column made an index scan look cheap and then
  run for minutes decoding the table many times over. The penalty counts the
  distinct row groups the fetches force, interpolating on the square of the
  leading-key correlation. Turning it off restores the previous planner behavior.

### Changed

- The fetch cache holds the columns that fit rather than dropping a whole entry
  when it exceeds its size cap (#359). An entry one byte over the 32 MB cap was
  not retained at all, so every fetch re-read the row group and re-decoded every
  column it touched. On a 100M-row fixture that was 2,833 ms at four aggregated
  columns and 134,147 ms at five, flat on either side of the step. Each column now
  decodes into its own context and the one that crosses the cap is released after
  its value is read, so exceeding the cap costs the overflow fraction rather than
  everything. An earlier fix moved the decode scratch out of the cached entry,
  shrinking entries about 3x (#353). A group whose raw bytes alone exceed the cap
  is still dropped whole.

- The index-fetch penalty is applied before the columnar path is offered to the
  planner, not after (#362). `add_path` frees a path it judges dominated, so a
  columnar path offered while the index paths still carried un-penalized costs was
  discarded, and raising those costs afterwards changed what `EXPLAIN` printed
  with nothing left to switch to. The planner chose an index scan it priced at
  13,954,742 over a columnar path it priced at 589,348, running 224 seconds where
  the columnar path runs 4.7. Two related defects were fixed with it: the parallel
  columnar path was conditional on a sequential scan surviving `add_path`, so it
  did not exist on exactly the selective queries where it was needed, and the
  projection path read the base path's cost after `add_path` may have freed it.

- The grouped vectorized aggregate path is charged for the folding it does (#349),
  `cpu_operator_cost` per input row per aggregate. It previously priced itself
  just above the scan it performs, which made it unpriceable against: every
  competing plan paid a per-row aggregation cost and this one paid none, so it won
  by construction, including against a parallel plan several times faster. That
  cost about 1.9x on a full-scan `GROUP BY` with few groups.

- The vectorized batch fold pushes scan keys, so it no longer forfeits zone-map
  row-group pruning (#349). The fold opened its reader with no predicates, so no
  group skipping occurred: on a clustered fixture with a selective predicate it
  read 200 of 200 row groups where the ordinary path read 2.

- Server-file functions now gate on the `pg_read_server_files` and
  `pg_write_server_files` roles instead of `superuser()` (#330), matching core
  `COPY ... FROM/TO 'file'` so a DBA can delegate server-file access without
  handing over superuser. This is a deliberate loosening. The read functions
  (`import_parquet`, `read_parquet`, `parquet_schema`, the `pgcolumnar_parquet`
  foreign-table scan, `import_arrow`) parse files this project wrote, so they are
  now reachable from a role short of superuser; give an untrusted Parquet or Arrow
  file the care in `docs/administration.md` while the Arrow parser fuzzing (#214)
  is incomplete. The write functions (`export_parquet`, `export_arrow`,
  `parallel_export_parquet`) gate on `pg_write_server_files`. `file_split_offsets`
  and `parallel_copy` already used the read role. `test/server_file_privilege.sh`
  now covers the full set and fails if a new file function lacks a check.

- The C standard flag for PostgreSQL 19 is probed rather than hardcoded (#294).
  This project sets `-std=gnu23` for PostgreSQL 19, whose headers use C23
  constructs. GCC 13 accepts only the older `gnu2x` spelling of the same
  language and rejects `gnu23` outright, so building against PostgreSQL 19 with
  GCC 13 failed on a flag the user never set. The Makefile now asks the compiler
  which spelling it takes. Every source file compiles under GCC 13 with `gnu2x`
  against PostgreSQL 19 headers.

- `pgcolumnar.recluster` records its ordered extent, so `pgcolumnar.sort_status`
  no longer reports a reclustered table as entirely unsorted (#311). It runs
  under a lock that permits concurrent inserts, and the mark is a boundary, so
  it can only be set where no other session's group is numbered below it. The
  rewrite records the stripe ids it reserves and marks the contiguous run from
  its first; a concurrent reservation leaves a gap in that sequence whenever it
  commits, which the visible catalog cannot show. With no concurrent writer the
  whole relation is recorded. With one, the run stops where it was interrupted
  and the rest is reported as decay, never the reverse.

- A row group's bloom filter is read for the columns a query filters on, not for
  every column (#314). A predicate probes one column, so a group that is
  examined needs the filters of the columns carrying predicates and no others.
  `bloom_pkey` is `(storage_id, group_number, column_index)`, so naming the
  column makes the fetch an exact index lookup rather than a range scan whose
  unwanted rows are discarded. Measured on one group of 200,000 rows over 12
  columns with one equality predicate: 715 buffers to 323, against a floor of
  251 with the bloom read deleted outright. With #310 the same probe query falls
  from 9577 buffers to 1547.

- A row group's bloom filters are read only when a predicate reaches them, not
  before every skip decision (#310). A bloom filter is consulted only for an
  equality predicate whose zone map did not already rule the group out, so a
  group the zone map skips needs none of them. The reader loaded them for every
  candidate group, and the cost scaled with the column count and the group size,
  because a filter holds one bitmap per column sized by the group's distinct
  values.

  The scale of that is easy to understand: on a 100 million row TSBS-cpu table a
  single filter is 256 kB, and the whole bloom catalog is 3.5 GB, larger than the
  data it describes. A selective scan copied it per query through 256 kB
  allocations, and profiling put about 55 percent of the query's CPU in
  anonymous-page faults under the group-skip check.

  On that table a clustered hostname query falls from 4610 ms to 106 ms, a factor
  of 43. On a smaller shape, 20 groups of 200,000 rows over 12 columns, the cost
  is 466 buffers per skipped group out of 504, and the query falls from 9577
  buffers to 1946. Results do not change; the filter was always a pruning step.

### Added

- `pgcolumnar.sort_status(rel)` reports how much of a sorted table is still in
  sorted order (#301). `vacuum_sorted` and `cluster` order a table once; rows
  inserted afterwards append in insertion order, and until now nothing measured
  how large that unsorted tail had become. An ordering rewrite now records the
  row group its run ends at, in a new `pgcolumnar.storage.sorted_through` column,
  and the function reports sorted and appended groups and rows alongside the
  declared `sort_by` key. A boundary rather than a count, so retiring a group
  inside the run does not move the mark onto a later replacement. The mark lives
  on the storage row, so any rewrite resets it with no invalidation step. The
  online `recluster` does not set it and therefore reports more decay than a
  table has (#311).

- Declarative `sort_by` clustering key (#288). `pgcolumnar.set_options(t, sort_by
  => ARRAY['col', ...])` records a physical sort key; `pgcolumnar.vacuum_sorted(t)`
  with no columns re-applies it, like PostgreSQL `CLUSTER` remembering an index.
  The sorted rewrite works on any btree-orderable column, text included (the
  Z-order `cluster()` is numeric-only), so a segment key such as `hostname`
  tightens its zone maps and lets equality/range filters on it skip chunk groups.
  Stored as column names, so it survives `pg_dump`/restore. Not auto-maintained;
  re-run after inserts. Virtual generated columns are rejected as a sort key.

- Column-oriented table access method (`USING pgcolumnar`) with per-column
  compression, chunk-group minimum and maximum skipping, per-chunk bloom filters,
  and a vectorized aggregate path.
- Native on-disk format PGCN v1: row groups, per-column chunks, an adaptive
  per-vector encoding cascade, zone maps for skipping, and per-chunk bloom
  filters. Delete, update, index scan, index-only scan, and projections all work
  on native tables. The earlier 1.0-dev format line has been removed; the
  `v1.0-dev` git tag preserves it.
- Compression codecs `none`, `pglz`, `lz4`, and `zstd`. `lz4` and `zstd` are
  compiled in when their system libraries are present.
- `count(*)` answered from catalog metadata without scanning.
- Parallel scan.
- Read stream prefetch in the scan on PostgreSQL 17 and later
  (`pgcolumnar.enable_read_stream`).
- Full index-only scan through a columnar visibility-map fork, with lazy `VACUUM`
  setting all-visible bits and clear-on-write, on by default
  (`pgcolumnar.enable_index_only_scan`).
- Multiple projections (C-Store model): a `pgcolumnar.projection` catalog, write
  fan-out, planner projection scan, back-fill, and vacuum coordination
  (`pgcolumnar.add_projection`, `pgcolumnar.drop_projection`,
  `pgcolumnar.enable_projection_scan`).
- Sorted storage with `pgcolumnar.vacuum_sorted`.
- Arrow IPC and Parquet export (`pgcolumnar.export_arrow`,
  `pgcolumnar.export_parquet`), self-contained with no libarrow or libparquet
  dependency. Coverage: scalar types (int2/4/8, float4/8, bool, text/varchar,
  bytea, date, time, timestamp, timestamptz, uuid, numeric, json),
  one-dimensional arrays, and composite types, with nulls at every level.
- Arrow IPC and Parquet import (`pgcolumnar.import_arrow`,
  `pgcolumnar.import_parquet`). The Parquet reader parses Thrift metadata,
  decompresses uncompressed, Snappy, GZIP, ZSTD, and LZ4_RAW pages, and decodes
  PLAIN and dictionary encodings from data-page versions 1 and 2. Both readers
  reconstruct one-dimensional arrays and composite types: Arrow from its List and
  Struct buffers, Parquet from the Dremel repetition and definition levels.
- Reading external Parquet in place. `pgcolumnar.read_parquet(path)` returns a
  file's rows without importing, `pgcolumnar.parquet_schema(path)` reports its
  columns and inferred types, and the `pgcolumnar_parquet` foreign-data wrapper
  exposes a file as a foreign table. A `path` may be a single file, a directory
  of `*.parquet` files, or a glob pattern, read as one relation in sorted order.
  The foreign scan skips row groups excluded by the query's predicate (min/max
  statistics) and decodes only the referenced columns; `EXPLAIN ANALYZE` reports
  the row groups and columns read and skipped and the number of files.
- Value encodings are chosen from a strided sample rather than by applying every
  candidate to every vector. Measured on a 6,000,000-row load: 20.9 s to 15.7 s,
  with byte-identical output. `pgcolumnar.encoding_sample_rows` controls the
  sample size and `0` restores the previous exhaustive selection.
- Partition values are percent-decoded, so a directory named `region=a%3Db` reads
  as `a=b`, and `__HIVE_DEFAULT_PARTITION__` reads as NULL rather than as that
  literal string, matching what Hive and Spark write.
- Hive-style partitioning on the `pgcolumnar_parquet` foreign-data wrapper. A
  foreign table declaring `partition_columns` reads `col=value` directory names
  as column values, and a predicate on a partition column drops whole files
  before they are opened, so a pruned file costs no I/O. `EXPLAIN ANALYZE`
  reports `Files Pruned`. The columns are declared rather than inferred, and a
  file missing a declared component raises rather than yielding nulls.
- A directory path now reads `*.parquet` files at any depth below it, where it
  previously read only the files directly inside. Entries whose name begins with
  `_` or `.` are skipped, so a Spark or Hive output directory does not read its
  own `_temporary` staging tree. A directory reached through a
  symbolic link is not descended, since a link to an ancestor would make the walk
  endless; a symbolic link to a file is still followed. Nesting deeper than 32
  levels raises rather than reading part of the tree.
- External Parquet files are read on demand instead of loaded whole. The reader
  holds a file's footer for the scan and pulls one page at a time, so peak memory
  for raw file data is one page rather than one file. A file of 1GB or more could
  not be read at all before this, because the whole-file allocation exceeded
  `MaxAllocSize`; that ceiling is gone. A row group excluded by predicate
  pushdown is now never read from disk, and `pgcolumnar.parquet_schema` reads
  only the footer.
- A Parquet DECIMAL is also read when it is stored as an INT32 or INT64 holding
  the unscaled integer, which is how writers store small precisions;
  `pgcolumnar.parquet_schema` advises `numeric(p,s)` for those columns.
- Parquet read type coverage extended to uuid and numeric (from fixed and
  variable DECIMAL, precision up to 38), fixed-length binary, and millisecond,
  microsecond, and nanosecond time units.
- `pgcolumnar.fsst_min_gain_percent`, a cost margin for the FSST string encoding
  decision. FSST is kept only when it reduces the compressed chunk by at least
  this percentage, default 5. Building FSST codes for every vector is one of the
  larger costs of a text or varlena load, and a sub-margin reduction does not
  repay it.
- The on-disk format version is enforced when data is read, not only stamped when
  it is written. Both the physical metapage version and the native data format
  version are checked, on every path that decodes columnar data, so a file this
  build cannot read is refused rather than misread.
- User and administrator documentation under [docs/](docs/index.md):
  installation, user guide, administration, configuration reference, SQL
  reference, and limitations.
- Benchmark harness (`bench/run_bench.sh`) covering storage size, query latency,
  vectorization, compression, sorted projection, index-only scan, projection
  scan, export, import, nested round-trip, and cross-engine reads of the Parquet
  output with DuckDB and pyarrow.
- Project logo under [logo/](logo/README.md).

### Fixed

- Bounded importer memory. `pgcolumnar.import_arrow` and `pgcolumnar.import_parquet`
  built each row's arrays and composites in one memory context and did not free
  them, using memory proportional to the row count. They now reset a per-row
  scratch context (and, for Parquet, a per-row-group context for decoded leaf
  streams), so peak memory stays bounded on large files.
- Hardened the Parquet reader against crafted files. File-declared page sizes,
  DECIMAL scale, and per-row-group column-chunk counts are range-checked, so a
  malformed footer yields a clean decode error rather than a stack overflow, an
  out-of-bounds read, or a wrong value. Float and double row-group skipping
  accounts for NaN and for inverted min/max intervals, and narrowing a wide
  Parquet value into a smaller PostgreSQL type raises instead of wrapping.
- Concurrent inserts of the same unique-index key now serialize correctly with a
  transaction-scoped advisory lock (`pgcolumnar.enable_unique_insert_lock`).
- Lost delete marks under concurrent same-chunk-group deletes.
- Relation-reference leak in parallel `CREATE INDEX`.

### Removed

- The decompressed-chunk cache, and the `pgcolumnar.enable_column_cache` and
  `pgcolumnar.column_cache_size` settings with it. Its only entry point had lost
  its caller when the earlier on-disk format was removed, so the cache had done
  nothing since. Two settings and four passages of documentation described a
  feature that did not run. A `postgresql.conf` that sets either parameter must
  drop the line. The implementation is in the git history if the performance case
  is made again against the current reader.

### Changed

- FSST string encoding is now kept only when it reduces the compressed chunk by
  at least 5 percent, rather than on any reduction at all. On shapes where FSST
  barely wins, such as high-entropy text, this costs about 2 percent stored size
  and reduces load time by roughly a third. Where FSST wins by more than the
  margin the encoding and the stored bytes are unchanged. Set
  `pgcolumnar.fsst_min_gain_percent` to 0 for the previous behaviour.
- Renamed the per-table option functions to `pgcolumnar.set_options` and
  `pgcolumnar.reset_options`. The previous names were carried over from an
  earlier compatibility goal that no longer applies. No aliases are kept, since
  the project is pre-release.

### Compatibility

- Builds from one source tree on PostgreSQL 15 through 19. Every test suite runs
  on all five majors.
- The Arrow and Parquet import and export functions require superuser and run on
  little-endian hosts.
- Cross-major `pg_upgrade` is covered by an opt-in gate
  (`PGC_RUN_UPGRADE=1 test/run_all_versions.sh`), in both copy and link transfer
  modes.
- All recorded test results come from x86_64. The suites have not been run on
  aarch64 or on a big-endian platform.
