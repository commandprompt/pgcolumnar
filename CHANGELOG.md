# Changelog

All notable changes to pgColumnar are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). pgColumnar is
pre-release; the version marker is `1.0-alpha`, recorded in `VERSION`. New tables
are written in the native on-disk format, PGCN v1. For the forward-looking plan see
[design/ROADMAP.md](design/ROADMAP.md); for full history see the git log.

The extension's `default_version` is `1.0-alpha`, and an upgrade script ships with
it. Older notes in this file describe `default_version` as pinned at `1.0-dev`,
which was true until that script existed.

## [Unreleased]

### Changed

- Building an index on a columnar table reads only the columns the index needs
  (#413). The table-AM scan interface has nowhere to carry a projection, so the
  index-build callback opened a reader that decoded every column: on a 20-column
  table, creating an index on one `int` column took 517 ms against heap's 442,
  slower than heap at the shape columnar storage should win. The callback is
  handed an `IndexInfo`, which names the key and `INCLUDE` columns and carries
  the expression and predicate trees, so it now says which columns it needs. The
  same build takes 72 ms, and build cost no longer scales with columns the index
  does not reference. Expression and partial indexes project their expression and
  predicate columns too, since a predicate evaluated against an unread column
  would test an unset value.

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
