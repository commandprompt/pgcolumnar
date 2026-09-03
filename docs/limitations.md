# Limitations and compatibility

## Release status

pgColumnar is pre-release. The version marker is `1.0-alpha3`, recorded in `VERSION`,
and it is tagged `v1.0-alpha3`.

On PGXN the same release is `1.0.0-alpha.3`. The two differ because PGXN requires a
semantic version. A semantic version has three integer components, and `1.0-alpha3`
has two. The pre-release identifier is dot-separated so that a tenth alpha sorts
after a third. `CREATE EXTENSION` reports the control file's version.

An alpha is still an alpha: treat a columnar table as reloadable and keep the source
the data was loaded from. The extension is appropriate today for evaluation, for analytical
tables that can be rebuilt from an external source, and for development, testing,
and measurement. It is not yet recommended for a production system of record or
for data that cannot be reloaded.

The hardening that was tracked before a first alpha is complete. The
Parquet and Arrow parsers, which this project wrote, are fuzzed, both by byte mutation and by structural
mutation of the container format
([issue #214](https://github.com/commandprompt/pgcolumnar/issues/214)). The boundary for
untrusted input files is stated and tested
([issue #216](https://github.com/commandprompt/pgcolumnar/issues/216)). The blast radius
of a single backend crash is asserted and documented
([issue #217](https://github.com/commandprompt/pgcolumnar/issues/217)). The build checks the on-disk format version
on read. It does not only stamp the version on write
([issue #240](https://github.com/commandprompt/pgcolumnar/issues/240)). A gate covers
cross-major `pg_upgrade`. Before, only a suite that nobody ran covered it
([issue #257](https://github.com/commandprompt/pgcolumnar/issues/257)).

What that testing does not cover is worth stating alongside it. The suites run
and pass on aarch64. An independent validation on 2026-08-05 ran the full gate on
a Graviton3 host. Every suite passed, and the per-suite result was the same as the
x86_64 run. The suites have still not been run on a big-endian platform
([issue #242](https://github.com/commandprompt/pgcolumnar/issues/242)).

Unaligned reads are one class that a reader could expect to differ by
architecture. The tests cover that class on each architecture. The sanitizer gate
builds with the clang address checks and undefined-behaviour checks. These report
a misaligned load on any host. That gate exists because the project found and
fixed such a read one time.

What stays hard to cover is what a sanitizer on x86_64 cannot see. Memory
ordering is the main item. x86_64 puts stores in a more strict order than aarch64.
Thus a missing barrier in concurrent code can be invisible on one architecture and
a defect on the other. The aarch64 gate runs the concurrency and isolation suites
on real hardware. That is evidence. It is not a proof.

PostgreSQL 19 coverage is against 19beta2, not a final release.

The sections below are the standing functional limitations, separate from that
work.

## On-disk format stability

The on-disk format is versioned. Each columnar relation records two versions. The first is a native data format
version, which is PGCN v1 at this time. The second is a physical metapage
version. The build checks both on read.

The metapage version guards the physical block layout. The native format version
guards the data encoding. Thus the build also catches a future version that
changes the encoding but keeps the metapage layout.

The build refuses a version that it does not know. It reports `unsupported
columnar format version` for the metapage, or `unsupported columnar native format
version` for the data format. The read then fails. It does not read bytes that a
different layout wrote. The check runs on each decode path. These paths include
the sequential scan, the vectorized aggregate, and the index-scan fetch. Thus the
build refuses a version that it cannot read, whichever path the query uses. Both guards are pinned by
`test/native_format.sh`.

A projection stores its own copy of the data and carries its own format version.
The build checks it against the storage of the projection and not against the
storage of the base table. Each stored object therefore describes itself. A build
that writes a new version stamps each object that it writes. This includes the
base table and the projections together.

In one version, the format keeps the data exactly. Data that one build writes and
reads back is identical byte for byte. `test/native_format.sh` proves this on
each supported PostgreSQL major. `test/pg_upgrade.sh` asserts the same property
across a PostgreSQL major upgrade. It runs as an opt-in gate
(`PGC_RUN_UPGRADE=1 test/run_all_versions.sh`). It covers each adjacent pair of
majors, in the copy transfer mode and in the link transfer mode.

There is no in-place upgrade across an incompatible format version. The format has
not changed during the pre-release, so no migration has been needed. The project has not
committed to an upgrade path or to a compatibility guarantee. Until it does, a
change to an incompatible version needs a reload. This is the same position as
the release-status note above. Keep the source that you loaded a columnar table
from. You can then build the table again if a future version changes the layout.
A physical copy keeps the exact bytes, and a build of the same version can read
it. `pg_basebackup`, a file-system snapshot and replication all make such a copy.
A physical copy does not replace the source across a version change.

The same posture covers the extension's own catalog, not only the on-disk data
format. The install script of a build defines the `pgcolumnar` catalog tables for a fresh
`CREATE EXTENSION`. An `ALTER EXTENSION UPDATE` script ships when a build needs one.
Three such scripts ship today: 1.0-dev to 1.0-alpha, 1.0-alpha to 1.0-alpha2, and
1.0-alpha2 to 1.0-alpha3. A single `ALTER EXTENSION pgcolumnar UPDATE` walks the
chain from any of them.

**Replacing the shared library is not sufficient on its own.** After installing a new
build, run `ALTER EXTENSION pgcolumnar UPDATE;` in every database that has the extension.
The library and the catalog have to agree, and only that command updates the catalog.

Skipping it can leave the catalog describing the previous build. Each installed function
records the name of a C symbol. 1.0-alpha moved those names, so an un-updated catalog
names symbols the new library does not export. Reading an existing columnar table then
fails with `could not find function "columnar_handler"`. The data is untouched, and the
command above fixes it. See [Upgrade](installation.md#upgrade).

A catalog can also lack a column that a newer build needs, where no upgrade script covers
the gap. For example, `sort_by` was added to `pgcolumnar.options`. A function that
uses that column fails against an `options` table that an older build created.

Across an incompatible build, meaning one where no upgrade script covers the change,
recreate the extension with `DROP EXTENSION` and `CREATE EXTENSION`, and load the data
again. This is not the remedy for the un-updated catalog described above, where
`ALTER EXTENSION pgcolumnar UPDATE` is enough. `DROP EXTENSION` removes your columnar
tables with it. Do not replace the files in place. A
dump that exists still restores into a newer build. `pg_dump` writes an explicit
column list for the configuration tables of the extension, and a new column takes
its default of NULL.

## Row-level security

The functions that read or write a table's storage directly refuse a relation
whose row-level security policies apply to the caller. They raise
`row-level security is in force on "<table>"`.

These functions do not run a query against the table. They open its storage and
read or write it, and row-level security is applied by the query rewriter, so no
policy would be consulted. Returning rows in that state would hand a restricted
caller every row, so the call is refused instead.

The functions affected are `read_projection`, `reconstruct_via_projection`,
`export_arrow`, `export_parquet`, `parallel_export_parquet`, `import_arrow`,
`import_parquet` and `parallel_copy`.

A superuser, a role with `BYPASSRLS`, and a table's owner without `FORCE ROW
LEVEL SECURITY` are not refused. Each already reads every row by other means.
Under `FORCE ROW LEVEL SECURITY` the owner is refused as well.

Use ordinary SQL against the table when policies must apply.

## PostgreSQL versions

pgColumnar builds from one source tree on PostgreSQL 15, 16, 17, 18, and
19. Every test suite runs on all five majors. The project validates 19 against 19beta2 and not against a final release. It will
validate 19 again after the release. It validates 15 through 18 against released
versions. PostgreSQL 13 and 14 still
build but are out of the tested matrix.

Three behaviors depend on the major:

- `ALTER TABLE ... SET ACCESS METHOD` exists on PostgreSQL 15 and later. On 13 and
  14, `pgcolumnar.alter_table_set_access_method` builds a new table, copies rows,
  and exchanges the names. This keeps the columns, the defaults, the constraints
  and the indexes. It does not keep the OID of the original relation. It also
  does not keep the objects that depend on it, such as views and foreign keys.
- The read stream prefetch path (`pgcolumnar.enable_read_stream`) is effective on
  PostgreSQL 17 and later. On earlier majors the setting has no effect.
- To set the access method of a *partitioned* table, you need PostgreSQL 17. On
  15 and 16, core refuses `ALTER TABLE ... SET ACCESS METHOD` on each partitioned
  table. It gives the message `cannot change access method of a partitioned
  table`. You therefore cannot make a partitioned parent columnar on those two
  majors, and its later partitions cannot take the choice from it. Set the access
  method of each partition one at a time instead. This is a restriction of core
  PostgreSQL and not of this extension. It applies with a foreign key and without
  one.

## Host architecture

The suites run and pass on x86_64 and on aarch64. The Arrow and Parquet import
and export functions run on little-endian hosts only.

The code runs on any architecture PostgreSQL supports. A data directory does not
move between architectures of different byte order. The native format stores each
multi-byte value in host byte order. The format specification states this. PostgreSQL's own heap format follows the same rule, so a columnar table
is no more restricted than the rest of the cluster.

A move to a big-endian host is not supported and is not tested. The format does
not record the byte order of the host that wrote it. A read therefore has no
check that could refuse such a move. See
[Backup and restore](administration.md#backup-and-restore).

## Workload and access patterns

- Columnar storage is for data that you mostly append. Updates and deletes
  operate, but they mark the rows and do not rewrite the data. Only
  `pgcolumnar.vacuum` makes the space available again.
- Point lookups are slower than heap, but much less slow than before. A fetch by
  item pointer finds the group of the row, and it keeps the decoded group for the
  rest of the statement. The cost no longer increases with the position of the row
  in its group. Heap is still faster for a single-row fetch. Bloom filters make an
  equality scan faster, because they skip row groups. They do not help a fetch by
  item pointer.
- **The cost of a fetch does increase with the width of the table.** There are two
  causes and both are easy to meet. An index fetch decodes the columns from the
  first one up to the highest-numbered column that the query reads. It does not
  decode only the columns that it reads. A query that reads one late column
  therefore decodes every column before it. The second cause is the size limit on
  the decoded columns that the statement keeps. The columns that do not fit are
  decoded again on each fetch. A table of many wide text columns meets both
  conditions. One measurement shows the effect. On a table of ten text columns,
  with the same rows and the same plan, a query on the first column took 975 ms.
  A query on the tenth column took 194,798 ms.

  The planner accounts for this. It charges the decode by the width of the columns
  it must decode, not by their number. A wide table therefore moves to a full scan
  at a lower row count than a narrow one. That is the order the measured times
  follow.
- A bulk `UPDATE` or `DELETE` through an index no longer costs the number of rows
  multiplied by the row group size. It still costs several times more than heap.
  The reason is that each changed row is marked and written again, and not
  changed in place.

## Bulk load and import throughput

For most data shapes, a load into a columnar table is slower than a load into a
heap. The factor depends on the data. There is no single number. The measurement
used 6,000,000 rows on PostgreSQL 17.10, non-assert, against a heap insert of the
same rows:

| shape | `encode_effort = full` | `encode_effort = fast` |
| --- | --- | --- |
| one `bigint` column | 0.67x | |
| one low-cardinality text column | 3.83x | 3.01x |
| one high-entropy text column | 6.48x | 1.94x |
| five columns, low-cardinality text | 5.59x | 5.55x |
| five columns, high-entropy text | 8.83x | 4.13x |

A single narrow column can be faster than heap; five columns of long high-entropy
text at full effort are several times slower. A single multiplier would be wrong
in one direction or the other depending on the schema.

`encode_effort` is the control. The default is `full`. It spends the most on the
encoding and gives the best storage ratio. `fast` accepts a lower ratio and gives
a higher load speed. On high-entropy text it recovers most of the cost, because
the encoding search is the work that it omits. Set it for one table with
`pgcolumnar.set_options(rel, encode_effort => 'fast')`. For low-cardinality text,
dictionary encoding is the largest cost and not that search. `fast` therefore
helps less.

`import_arrow` and `import_parquet` are not slower than an ordinary `INSERT` of
the same rows. There is no import-specific overhead; the cost is the columnar
write path either way.

Work on load throughput is tracked in
[issue #155](https://github.com/commandprompt/pgcolumnar/issues/155). One realized lever
is [`pgcolumnar.parallel_copy`](#parallel-bulk-ingest). It loads a text file
across several cores at once.

## Parallel bulk ingest

`pgcolumnar.parallel_copy` loads a text file with several workers at once. It has
these constraints.

- The file must use COPY text format. The function does not accept CSV or binary
  format.
- The target is a single columnar table or a RANGE-partitioned table with
  columnar partitions. The function rejects any other target, such as a heap
  table.
- For a partitioned target the file must be sorted ascending by the partition
  key. The key type must be numeric or a date/time type. The function reports an
  error for a text key and for an unsorted file.
- The caller needs membership in the `pg_read_server_files` role and INSERT on
  the target. The file is read on the server host.
- Set `max_prepared_transactions` above the worker count. The load prepares one
  transaction per worker, and the function errors up front when the setting is
  too low.
- The speedup is bounded by the physical core count. The columnar encode step is
  CPU bound, so workers past the physical cores add little.
- The load commits on its own. It runs in background workers, so it is not part
  of the calling transaction. A `ROLLBACK` in the caller does not undo the loaded
  rows. The atomicity is across the workers, not with the caller.
- The load is atomic through two-phase commit. A coordinator crash during the
  final commit step can leave some ranges committed and some prepared. This is
  the ordinary two-phase-commit in-doubt case. A DBA resolves it from
  `pg_prepared_xacts`.

See the [SQL reference](sql-reference.md#pgcolumnarparallel_copytarget-regclass-filename-text-workers-int-default-null-returns-bigint)
and [Benchmarks](benchmarks.md#parallel-bulk-ingest).

## Planner statistics

`ANALYZE` collects column statistics for a columnar table. These are the null
fraction, the distinct counts, the most-common values, the histograms and the
correlation. This is the same set that it collects for a heap table. The planner
then estimates the predicates from the data.

Correlation has a special importance. It is the statistic that shows
`pgcolumnar.vacuum_sorted` and Z-order clustering to the planner. A table that is
stored in sorted order on a key reports a correlation near 1 for that column. The
planner can then give a correct cost to a range scan on it.

The row count the planner uses does not come from `ANALYZE` at all. It comes from the row-group
metadata. It is therefore correct whether or not `ANALYZE` has run.
`pg_class.reltuples` is a few percent low after `ANALYZE`. Some blocks hold no
row-group data, such as the metapage and the space that is reserved but not
written. These blocks count as visited, but they give no rows. The planner does
not use that figure for columnar tables.

`ANALYZE` costs more on a columnar table than on a heap of the same size. The
sampler offers every row of every block that it visits, so the cost follows the
rows offered and not the rows kept. It reads those rows with a reader that is
restricted to one row group, and not through the fetch-by-row-number path.

`TABLESAMPLE` is unsupported and says so: it raises an error rather than
returning no rows.

## Vacuum and compaction

- `autovacuum_parallel_workers`, the per-table storage parameter PostgreSQL 19 adds
  for parallel autovacuum, is accepted on a columnar table and has no effect.
  pgColumnar implements its own vacuum, which marks the visibility map and retires
  fully-deleted row groups, and does no parallel work. The parameter cannot be
  refused: storage-parameter validation belongs to PostgreSQL and is driven by the
  relation kind rather than by the access method. Setting it is harmless and
  pointless.
- `REPACK`, `VACUUM FULL` and `CLUSTER` are not supported on a columnar table; the
  copy-for-cluster path raises an error. `REPACK` arrives in PostgreSQL 19 and
  replaces the other two, and it dispatches through the same path, so it is
  refused for the same reason. Use `pgcolumnar.vacuum` or
  `pgcolumnar.vacuum_full` instead.
- `pgcolumnar.vacuum` always rewrites the whole relation into full row groups. It
  accepts a `stripe_count` argument for compatibility with the interface, but it
  does the full rewrite. It gives the rows new numbers, so it also builds the
  indexes of the table again.
- The writer reserves row numbers one whole row group at a time. A row group that
  it flushes with fewer than `stripe_row_limit` rows therefore leaves a gap in
  the row-number space. A row number must be unique and stable, and nothing more.
  The gap does no damage.
- A declared `sort_by` key (#288) and `pgcolumnar.vacuum_sorted` are **one-shot**.
  Rows that an insert adds after a sort go in at the end, in insertion order. No
  operation sorts them again. Skip quality therefore decreases until the next
  `vacuum_sorted`. `pgcolumnar.sort_status` reports how far that has gone, so the
  decision to re-sort rests on a measurement. The online `pgcolumnar.recluster`
  records only the part of its output it can prove is one contiguous ordered
  run. A table reclustered while another session was inserting therefore reports
  more decay than it has. PostgreSQL `CLUSTER` behaves the same way and is also not
  maintained on insert. An online re-sort for text keys that does not block, and
  automatic maintenance, are not implemented yet. A sort key is also a trade. It
  puts the values of one dimension together, and it spreads the other dimensions
  across each chunk group. A query that filters a *different* dimension can
  therefore skip fewer groups than the natural insertion order permits. Sort by
  the key that your selective queries filter on.

## Parallel scans

A columnar scan can run in parallel, and the planner builds a parallel path for
it. On a wide projection it chooses that path when
`max_parallel_workers_per_gather` is 4. The decode cost is priced by column
width, and a parallel plan divides it across workers. At the default of 2 the
planner still chooses the serial plan for the wide query below.

The wide query was measured on one machine, on a build without assertions. The
parallel plan runs in about 275 ms and the serial plan in about 639 ms. Both are
the fastest of 7 interleaved readings at 4 workers. Two earlier readings of the
same query gave 306 and 604, then 300 and 610.

The fastest reading is quoted rather than the middle one. This machine is shared,
so scheduling noise only ever adds time, and the fastest reading is the one least
polluted by it. The middle reading moved 31% between runs where the fastest moved
2.5%.

On a narrow projection the planner still prefers the serial plan where the
parallel one is faster.

This was measured on 4,000,000 rows in a table of 14 columns. Eleven query shapes
were tested, all of them shapes where the planner chose the serial scan. The
parallel plan ran 1.8 to 2.5 times faster on the median. In every shape the
slowest parallel run was still faster than the fastest serial run, by 1.45 times
at the narrowest margin.

Those readings were taken on a build with assertions enabled. The narrow query
below was re-measured on a build without them. Taking the fastest reading of each
arm at 4 workers, it ran 2.5 to 2.7 times faster. On the middle reading it ran
2.4 to 2.5 times faster. The slowest parallel run beat the fastest serial run by 1.60
times. Both arm orders were run and both agree.

Which column the narrow query filters on decides all of this. The section below
on the predicate column gives the measurements and says why.

"Narrow" here means few columns, not few rows. A scan that reads three columns of
fourteen decodes little. The serial plan is therefore cheap, and a fixed per row
charge above it then decides the comparison.

Two things produce this and both apply.

First, the planner charges each row passed from a worker to the leader at
`parallel_tuple_cost`. That is a fixed amount per row. It does not depend on how
wide the row is.

This was measured at a constant volume of data shipped. Tripling the row count
and thirding the width raised the real cost of the transfer by 1.56 times. The
charge rose by 3 times. The charge therefore overstates a narrow row by about 1.9
times. This applies to any narrow projection, on a heap table as much as a
columnar one.

Second, the columnar scan cost is low against the time it takes. The same query
was measured on a heap table holding the same rows, over a ladder of one to eight
integer columns. The columnar scan is priced at two fifths to seven tenths of what
its real time implies. The error is largest on the narrowest projection, which is
where this problem lives. That is recorded separately, because it affects every
plan comparison and not only this one.

`parallel_tuple_cost` is a global setting applied through the core Gather cost.
An access method cannot set its own value without a core change. Lowering the
global setting is not a general fix, because it changes the rate for every table
on the system.

The workaround is to force the parallel plan when it helps, with `SET
max_parallel_workers_per_gather` and a lower `SET parallel_tuple_cost` for the
session.

The value at which a plan turns parallel depends on the table and on the worker
count. Measure it on your own table rather than copying a number. One measured example follows. It is given as
the SQL that builds it. A prose description does not pin a columnar table's
size, and the size is what sets the threshold.

```sql
CREATE TABLE c753 (sel int4, a int4, b int4, c int4, d int4, e int4, f int4,
    g int4, t1 text, t2 text, t3 text, t4 text, n1 numeric, n2 float8)
    USING pgcolumnar;

INSERT INTO c753 SELECT i,
    hashint4(i), hashint4(i+1), hashint4(i+2), hashint4(i+3),
    hashint4(i)%1000, hashint4(i)%100, hashint4(i)%10,
    md5(hashint4(i)::text), md5(hashint4(i+1)::text),
    md5(hashint4(i+2)::text), md5(hashint4(i+3)::text),
    (hashint4(i)%100000)::numeric, (hashint4(i)%100000)::float8
    FROM generate_series(1,4000000) i;

CREATE TABLE h753 (LIKE c753);
INSERT INTO h753 SELECT * FROM c753;
ANALYZE c753;
ANALYZE h753;
```

That gives 383 MB for `c753` against 823 MB for `h753`. Three of the `int4`
columns hold values in a small range, and they compress well. Change them and
the columnar size changes, and so does every number below.

The narrow query is `SELECT sel, a, b FROM c753 WHERE a <= -1073741824`, which
returns 1,000,158 rows. The wide query is
`SELECT * FROM c753 WHERE sel <= 2000000`, which returns 2,000,000 rows.

### The predicate column must be unordered, or the zone map answers first

The column a narrow query filters on decides how much work the serial plan does.
`sel` is the `generate_series` counter, so it is stored in order. The zone
map then excludes 20 of the table's 27 chunk groups before any row is read.

Filtering instead on `a`, which is `hashint4` derived and unordered, reads all 27
groups for the same number of rows returned. The two differ by more than the
clock:

| narrow query filters on | groups read | serial cost | speedup, fastest | speedup, middle | columnar turns parallel at | heap turns parallel at |
| --- | --- | --- | --- | --- | --- | --- |
| `sel`, stored in order | 7 of 27 | 22,428 | 1.54 to 1.67 | 1.24 to 1.31 | 0.015 | 0.035 |
| `a`, unordered | 27 of 27 | 83,305 | 2.54 to 2.67 | 2.43 to 2.51 | 0.060 | 0.035 |

The last two columns are at 4 workers and are the causal evidence.

On the ordered column the effect this section describes nearly disappears. The
slowest parallel run is then slower than the fastest serial run, so the
non-overlap stated above does not hold there. The numbers in this section are
measured on the unordered column, because that is the case where the planner's
choice costs the most.

Read the last two columns together. The columnar threshold moves from 0.015 to
0.060 between the two predicates. The heap threshold does not move at all. A heap
has no zone map, so that is what identifies pruning as the cause, rather than
anything about the two columns themselves.

The values below come from sweeping `parallel_tuple_cost` down from the default
0.100 in steps of 0.001. The threshold is the first value at which `EXPLAIN`
shows a `Gather` node.

| `max_parallel_workers_per_gather` | columnar turns parallel at | heap turns parallel at |
| --- | --- | --- |
| 2, the default | 0.039 | 0.026 |
| 3 | 0.053 | 0.031 |
| 4 | 0.060 | 0.035 |

Each value is one sweep on one build of the table. `ANALYZE` samples, so the row
estimate moves between builds, and the heap threshold moves with it. The columnar
threshold barely moves. An earlier edition reported these as ranges over repeated
builds, and those ranges covered PostgreSQL 17 and PostgreSQL 18.

A different table gives different values, and so does a different worker count.
An earlier edition of this page quoted 0.060 for the columnar plan and named
neither the table nor the worker count. That value is correct. It is this table
at 4 workers, with the narrow query filtering on an unordered column. A later
edition replaced it with 0.010, which is the same table at 2 workers with the
query filtering on `sel`. Both are right for the case they measure, and neither
edition recorded which case that was.

## Index-only scans

An index-only scan uses the columnar visibility-map fork, which lazy `VACUUM`
populates. A row group gets the all-visible mark only when two conditions are true. Its
inserting transaction must come before the oldest snapshot horizon, and the group
must have no deletes. Any later write clears the bit. A fetch that checks the
snapshot serves data that a load wrote recently. This continues until autovacuum
or an explicit `VACUUM` marks the group. Turn the feature off with
`pgcolumnar.enable_index_only_scan = off`.

## Projections

A projection is an additional sorted copy. Each projection therefore adds write
cost and storage cost. `pgcolumnar.vacuum` builds the projections again.

The planner uses a projection only when two conditions are true. The projection
must contain each column that the query refers to, with no system columns and no
whole-row references. The query must also restrict the leading sort column of the
projection. Other queries read the base table.

When you add a projection to a table that holds rows, the build fills it under
`ShareLock`. That lock stops concurrent writes until the build completes, in the
same way as a `CREATE INDEX` that is not concurrent. Turn projection scans off with
`pgcolumnar.enable_projection_scan = off`.

## Concurrency

- Concurrent deletes or updates to rows in the same row group go in sequence, on
  the row-mask entry of that group. A second writer waits for the first writer to
  commit. It then reads the committed mask again and merges its own bits. Thus
  both sets of delete marks survive. Writes to different row groups continue at
  the same time.
- A concurrent `UPDATE` or `DELETE` of the same row serializes on the row
  identity. The second writer waits for the first to commit. It then gets a
  retryable `serialization_failure`, so the row is never duplicated and no update
  is lost. This holds at every isolation level and needs no unique index. Retry
  the transaction on this error, as you retry one at `REPEATABLE READ`. The
  behavior is stricter than a heap at `READ COMMITTED`. A heap re-applies both
  updates transparently there; pgColumnar makes the loser retry. The GUC
  `pgcolumnar.enable_row_update_lock` (default on) controls this serialization.
  The GUC `pgcolumnar.row_lock_buckets` (default 1024) bounds the held locks per
  storage, so a bulk update cannot exhaust the lock table. Unrelated rows may
  share a bucket, which only makes them wait when they need not. For the same
  reason, two multi-statement transactions can rarely deadlock, even on disjoint
  rows. This happens when their bucketed locks collide in opposite statement order.
  Retry resolves it, as with any serialization failure.
- Concurrent inserts of the same unique key go in sequence. The server therefore
  always finds the conflict. Before a new row reaches the uniqueness check, the
  access method takes an advisory lock with the scope of the transaction. The key
  of that lock is the unique key of the row. Equal keys hash to the same lock,
  which agrees with the equality of the index. Thus `numeric` `1.0` and `1.00`,
  `citext` values that differ only in case, and text values that a collation
  makes equal, all go in sequence correctly. Each index has a limited number of buckets, which
  `pgcolumnar.unique_lock_buckets` sets and which defaults to 128. Keys hash into
  those buckets. Two keys that are not related can share a bucket. Such keys go
  in sequence when they do not need to, but they never fail to go in sequence
  when they must. This applies to unique, immediate and valid
  indexes. It includes multi-column indexes, partial indexes and expression
  indexes. Some indexes use a single lock for the whole index
  instead. The first is an index whose operator class does not match
  the default equality of its key type. The second is an index whose key type has
  no hash support. A
  true conflict on the same key can appear as a deadlock abort and not as a
  `unique_violation`. Both results refuse the duplicate. Turn the serialization off with
  `pgcolumnar.enable_unique_insert_lock = off`.

## Row locking

`SELECT ... FOR UPDATE`, `FOR SHARE` and `FOR KEY SHARE` are not implemented on a
columnar table and raise `columnar: row locking is not supported yet`.

Two consequences are worth stating here, because the error surfaces somewhere
other than where the feature is used:

- **A foreign key cannot refer to a columnar table. The server refuses such a
  key when you create it.** The check for referential integrity reads the parent
  row with `FOR KEY SHARE`. A columnar table cannot do that. `CREATE TABLE` and
  `ALTER TABLE ADD CONSTRAINT` therefore refuse the constraint. They do not
  accept a constraint that nothing could satisfy. A columnar table on the child side of a foreign
  key is unaffected and works normally.
- **The server refuses a conversion of a table that a foreign key already refers
  to. The reason is the same.** `ALTER TABLE ... SET ACCESS METHOD pgcolumnar` and
  `pgcolumnar.alter_table_set_access_method` give the same configuration from the
  other side. They are therefore refused while a foreign key refers to the table.
  A partitioned table is also refused. It has no storage of its own. But it sets the access
  method that each later partition takes, and core would then refuse each of those
  partitions. Drop the constraint first if the table must be
  columnar. A conversion of the referencing side is permitted. A conversion of a
  table that no foreign key refers to is also permitted.
- `INSERT ... ON CONFLICT DO UPDATE` takes a row lock and raises the same error.
  `ON CONFLICT DO NOTHING` does not, and works.

`CREATE TABLE` refuses an unlogged columnar table for the same reason. Both cases
follow one rule. This access method refuses a configuration that it cannot
support at the point where you choose it. It does not refuse it at each later
use.

## Indexes

- Stale index entries left by deletes and updates are filtered on fetch and
  reclaimed by `REINDEX`, not removed opportunistically.
- `CREATE INDEX CONCURRENTLY` (the concurrent validate path) and partial
  block-range index builds are not supported.

## Constraints on the import path

`pgcolumnar.import_arrow` and `pgcolumnar.import_parquet` maintain each index on
the target. They also apply the unique constraints and the exclusion constraints.
An import therefore cannot reach a state that an ordinary `INSERT` would refuse.
The import path defers a deferrable constraint to the commit, as ordinary DML
does. An import can therefore break uniqueness for a short time in the middle and
still be correct at the end. Such an import commits, and the server does not
refuse it.

## Vectorized aggregate coverage

The vectorized aggregate path covers one shape only. That shape is
`SELECT agg(col) FROM t [WHERE ...]`, on one relation and with no grouping.

The target list may contain expressions over those aggregates. `count(*)::text`,
`avg(a)+avg(b)`, `round(avg(a), 2)` and `max(a)-min(a)` all take the path. What
matters is that every aggregate in the list is a supported one, not that the list
holds nothing else. The
path also needs each aggregate, each column type and each filter clause to be
supported. The supported set is:

- `count`, which includes `count(*)` and `count(col)`.
- `sum` and `avg` on `smallint` and `integer` columns.
- `min` and `max` on any type that has a default ordering.
- `WHERE` clauses that are conjunctions of simple `column op const` comparisons.

Each other query uses the scalar plan and stays correct. These include `sum` or
`avg` on `bigint`, `numeric` or floating point. They also include these:

- ordered-set aggregates and string aggregates
- aggregates with `DISTINCT`
- `GROUP BY` (unless the opt-in grouped path below is enabled) and `HAVING`
- filters that are not simple
- joins
- a reference to a whole row or to a system column

A separate opt-in path vectorizes `GROUP BY`. It is off by default. Set
`pgcolumnar.enable_group_vectorization` to `on` to enable it. It covers
`SELECT <keys>, agg(col) ... [WHERE ...] GROUP BY <keys>` on one columnar
relation. Each key must be non-volatile, hashable, and computable from that
relation. A collatable key must use a deterministic collation. Each output must
be a supported aggregate or a bare key reference. This path also accepts `sum`
and `avg` on `bigint`, `numeric`, and floating-point columns, which the ungrouped
path rejects. `pgcolumnar.groupagg_max_groups` caps the group count, default
1,000,000. The cap is checked at execution against the real count, so a query
that exceeds it errors rather than switching plans. Raise the cap or turn the
path off.

## Skipping and collation

A pushed-down filter drives chunk-group skipping only when one condition is true.
The collation of the comparison must match the collation of the column. That is
the collation that put the stored minimum and maximum in order. A comparison with
a different collation still operates as a filter, but it does not drive skipping.
The results therefore never depend on the pushdown.

## Replication and backup

- Physical replication and physical backups (`pg_basebackup`, snapshots) include
  columnar tables, which are WAL-logged relations.
- `pg_dump` and `pg_restore` handle columnar tables, and the target server must
  have the extension installed and preloaded. The table data, the indexes and the
  per-table options (`pgcolumnar.set_options`) survive the round trip. The
  projection **storage** does not. Its key is an internal storage id, and a
  restore makes a new one. The projection **declaration** does survive, in
  `pgcolumnar.projection_declaration`, which is keyed by relation and stores
  column names. After a restore, run `pgcolumnar.rebuild_projections()` to build
  the projections again from those declarations. A physical backup (`pg_basebackup`) copies the
  cluster bytewise and preserves them.
- Logical decoding reads the WAL records of heap tuples. Columnar data reaches
  WAL as full-page images, and those carry no tuple structure. Logical decoding
  therefore does not emit a change to a columnar table, and logical replication
  does not carry it. Use physical replication for columnar tables.

  A decoding slot is not silent about a columnar table, which is the part worth
  knowing before relying on one. The metadata catalog uses ordinary heap tables.
  A slot therefore delivers the inserts and the updates to `pgcolumnar.storage`,
  `pgcolumnar.row_group`, `pgcolumnar.column_chunk`, `pgcolumnar.zone_map` and
  `pgcolumnar.delete_vector` while the writer writes the table. A consumer with a
  subscription to all tables therefore gets a stream of internal records. This
  includes the encoded chunk descriptors, as bytea. It includes none of the rows
  of the table. Filter
  the `pgcolumnar` schema out of any publication.

  A columnar table can be the **target** of logical replication, with one
  restriction. An INSERT-only publication works end to end. The initial table
  sync and streamed inserts both use paths that take no row lock. A heap table
  on the publisher can therefore be mirrored into a columnar table on the
  subscriber. This suits append-only fact tables and event logs.

  A publication that carries UPDATE or DELETE does not work, and the failure is
  permanent rather than temporary. Applying either one requires a row lock,
  which columnar storage does not support. PostgreSQL does not advance the
  replication origin when a transaction fails to apply. The subscription
  therefore retries the same transaction indefinitely, roughly every five
  seconds. No later change is applied and the mirror stops at that point.

  Create the publication with `publish = 'insert'` when the target is columnar.

  ```sql
  CREATE PUBLICATION p FOR TABLE t WITH (publish = 'insert');
  ```

  **This is a decision and not an open item.** pgColumnar uses only the WAL
  mechanisms and record types that PostgreSQL already defines. A decodable
  change for a columnar write needs a record type that carries tuple structure
  for columnar data. That is a new WAL semantic. It is therefore out of scope,
  by the same rule that keeps the extension installable on a stock server.

  The supported way to feed a change consumer is a heap capture table written
  by a row trigger. It is documented in
  [capture changes for replication or CDC](user-guide.md#capture-changes-for-replication-or-cdc).
  Tests pin both halves. `test/logical_decoding_source.sh` asserts that a
  columnar table emits nothing. `test/logical_decoding_cdc_recipe.sh` asserts
  that the capture recipe works and decodes.

  Updates and deletes are then skipped by design and the subscription keeps
  running. If a subscription is already wedged, the error in the subscriber log
  names this restriction and the option above.

  To capture changes from a columnar table today, use a row trigger that writes
  to a heap table and decode that. The recipe is in
  [user-guide.md](user-guide.md#capture-changes-for-replication-or-cdc).

## Import and export type coverage

The import and export functions require the `pg_read_server_files` or
`pg_write_server_files` role, which superusers hold, and run on little-endian
hosts.
They support one-dimensional arrays and composite types built from the scalar
types below, with nulls at every level. Multi-dimensional arrays and types not
listed are rejected.

| Type | Arrow export | Parquet export | Arrow import | Parquet import |
| --- | --- | --- | --- | --- |
| `int2`, `int4`, `int8` | yes | yes | yes | yes |
| `float4`, `float8` | yes | yes | yes | yes |
| `bool` | yes | yes | yes | yes |
| `text`, `varchar` | yes | yes | yes | yes |
| `bytea` | yes | yes | yes | yes |
| `date`, `time`, `timestamp`, `timestamptz` | yes | yes | yes | yes |
| `uuid` | yes | yes | yes | yes |
| `numeric` | yes | yes (`numeric(p,s)`, `p` <= 38) | yes | yes (DECIMAL only) |
| `json`, `jsonb` | yes | yes | yes | no |
| one-dimensional array of the above | yes | yes | yes | yes |
| composite of the above | yes | yes | yes | yes |

The reader imports `uuid` from a fixed-length binary column of 16 bytes. It
imports `numeric` from a DECIMAL column with a precision up to 38. That column
holds big-endian bytes, of fixed or variable length. The reader also imports
`numeric` from an INT32 or an INT64 that holds the unscaled integer. Writers use
that form for a small precision.

`numeric` needs a declared precision for a Parquet round trip. The exporter writes DECIMAL only for a column that you
declare as `numeric(p,s)`, with `p` up to 38. It writes a `numeric` column with
no precision as text, and it does the same for a column with `p` above 38. The
reader cannot import a text column back into `numeric`. Declare
`numeric(p,s)` with `p` up to 38 when the file has to read back into a `numeric`
column. Arrow export and import carry `numeric` in either form.
The exporter can write `json` and `jsonb` to Parquet, and other tools can read
them. pgColumnar cannot import them from Parquet at this time. Arrow supports
both types in each direction.

## Compression codecs

For the native table format, `lz4` and `zstd` are available only when the
extension was built with the corresponding system libraries. When the build does not
include a codec, a request for it uses a codec that the build does include.
`pglz` and `none` are always available.

When reading external Parquet files, the reader decodes uncompressed, Snappy,
GZIP, ZSTD, and LZ4_RAW pages. GZIP needs a build with zlib. ZSTD and LZ4_RAW
need the same libraries as the native codecs. A page whose codec the build did
not include fails with an explicit decode error. LZO, BROTLI, and the deprecated
Hadoop-framed LZ4 (codec 5, as distinct from LZ4_RAW) are not read.

## Reading external Parquet

The read-in-place surface (`read_parquet`, `parquet_schema`, and the
`pgcolumnar_parquet` foreign-data wrapper) has these limits:

- Reads require the `pg_read_server_files` role, which superusers hold, and run on
  little-endian hosts, as import and export do, since they read a server-side path. A file from a different source is input
  without trust, and the parser for it is code that this project wrote. Refer to
  Security in the administration guide for the trust boundary and the risk that
  stays. Issue #214 tracks the fuzzing.
- A `path` that is a directory reads the `*.parquet` files at any depth below
  it, descending into subdirectories. The reader skips each entry whose name starts
  with `_` or `.`, and it does this for directories and for files. Spark and Hive
  write names of that form, such as `_SUCCESS` beside the data and the output of
  a task in progress under `_temporary`. The reader still reads a path that you
  name directly, whatever its name is. The reader does not go into a directory
  that it reaches through a symbolic link. A link to a parent would make the walk
  continue without end. It does follow a symbolic link to a file. Nesting deeper than 32 levels raises rather than reading part of the
  tree.
- Hive-style partitioning is available on the foreign-data wrapper only, through
  the `partition_columns` table option. The columns are declared, not inferred
  from the tree, and `read_parquet` has no equivalent. The reader takes a value from the directory name,
  after it decodes the percent escapes. A value written as `a%3Db` therefore
  reads as `a=b`. Hive and Spark write `__HIVE_DEFAULT_PARTITION__` as the marker
  for a null partition value, and the reader gives NULL for it, not that string.
  A file must carry a directory component for each declared column. If it does
  not, the reader raises an error. It does not give rows with nulls in the
  partition columns.
- The reader takes the partition values only from the directory components
  between the declared path and the file. A component above the path does not set
  a column. A file whose own name has the form `col=value` does not set one
  either.
- A predicate that reads only partition columns prunes files, unless it contains
  a volatile function. Pruning decides a clause one time for each file. That
  matches the decision that the executor would make for each row, but only when
  the clause depends on the partition values alone. A volatile call does not meet
  that condition. The reader therefore leaves such a clause to the
  executor and prunes nothing. Stable and immutable functions still prune.
- `parquet_schema` describes the first file of a directory or glob, assuming the
  set is uniform. The read paths still bind every file against the declared
  columns, so a mismatched file raises rather than returning wrong rows.
- A `TIMESTAMP` column with nanosecond precision is advised as `bigint`, which is
  exact; declaring it `timestamp` reads it with the sub-microsecond digits
  truncated.
- The reader reads a file in parts and does not load the whole file. It holds the
  footer for the length of the scan. It reads the pages one at a time. Peak memory for the
  raw file data is one page, not one file, so file size is not a limit. What does
  increase with the data is the decoded form of one row group, for the columns
  that the query reads. The reader decodes a row group before it produces the rows
  of that group. A file
  written with larger row groups therefore costs more memory than the same data
  written with smaller ones.
- The column definition list, or a foreign table's column list, must cover every
  leaf column in the file. A shorter list is an error rather than a projection.

### Row-group skipping

Row-group skipping is narrower than the general statement that a group is skipped
when its statistics exclude the predicate. A scan that skips nothing still returns
correct rows; these are the conditions under which it can skip at all:

- The clause must be `column op constant` with a btree comparison operator.
  A parameterized qual, such as one inside a PL/pgSQL function or a generic plan
  from `PREPARE`, does not drive skipping. This is deliberate: the skip set is
  computed once when the scan starts and reused across rescans.
- The column must be stored as a Parquet INT32, INT64, FLOAT, or DOUBLE. Text,
  bytea, uuid, and boolean columns are filtered but never skipped, whatever their
  statistics. A `numeric` column follows its storage: one written as an INT32 or
  INT64 DECIMAL does skip, one written as a byte-array DECIMAL does not.
- The constant's type must match the column's type exactly. A cross-type
  comparison such as `ts >= DATE '2026-01-01'` against a `timestamp` column, or
  `bigint_col > 5::int`, does not skip.
- The row group's statistics must carry both a minimum and a maximum, and the
  interval must not be inverted. Two cases decode to an interval that the reader
  does not trust for skipping. The first is an unsigned Parquet column that
  crosses the sign boundary. The second is a column that the reader makes
  narrower, into a smaller PostgreSQL type.

The `Row Groups Skipped` counter in `EXPLAIN ANALYZE` reports what was actually
skipped.

From 1.0-alpha3, `pgcolumnar.export_parquet` writes per-row-group statistics, so
a file pgColumnar wrote can be skipped. Three limits apply to what those files
carry:

- Bounds are written for the columns stored as INT32, INT64, FLOAT or DOUBLE.
  That is the set the reader can skip on: `smallint`, `integer`, `bigint`,
  `real`, `double precision`, `date`, `time`, `timestamp` and
  `timestamp with time zone`. A `text`, `bytea`, `uuid`, `boolean` or
  byte-array `numeric` column carries a null count and no bounds. A predicate on
  one of those filters, but it never skips.
- The constant must still match the column's type exactly, per the condition
  above. How the literal is written decides its type, in three ways.

    - **Quoted, with no type named.** The literal is `unknown` and takes the
      column's type, so all nine types skip. `smallint_col < '500'` skips.
      `smallint_col < 500` does not. A temporal literal is usually written this
      way, which is why it skips as written.
    - **Named type, quoted or cast.** The literal is that type, not `unknown`.
      The exact-type condition then applies to it like any other constant.
      `DATE '2026-01-01'` is a `date`, so `date_col < DATE '2026-01-01'` skips
      and `timestamp_col < DATE '2026-01-01'` does not.
    - **Unquoted.** The literal is typed by its own text, so it matches the
      column only sometimes. A digit string is an `integer` while the value fits
      in one, and a `bigint` when it does not. It matches whichever of those two
      widths the column is, and never the other: `bigint_col < 5000000000`
      skips, while `bigint_col < 5` and `integer_col < 5000000000` do not. It
      always matches `double precision`, whose operand is widened. A literal
      with a decimal point is a `numeric`, and it matches `double precision`
      alone. Against an integer column PostgreSQL casts the column rather than
      the constant, so `bigint_col < 5.0` skips nothing. `smallint` and `real`
      match no unquoted literal at all.
- A file exported by 1.0-alpha2 or earlier carries no statistics. Nothing
  rewrites it in place. Export it again to make it skippable.

This exporter writes every `numeric` as a byte-array DECIMAL, so a `numeric`
column in a file we wrote never skips. Another writer may store the same column
as an INT32 or INT64 DECIMAL, which does skip.

## Reading Apache Iceberg

The Iceberg read surface (`iceberg_scan`, `iceberg_data_files`, the
`pgcolumnar_iceberg` foreign-data wrapper, and the REST catalog client) reads a
table at its current snapshot. It has these limits:

- The metadata, manifests, and manifest lists are written by whoever authored the
  table. They are input without trust, parsed by code this project wrote. An
  honest caller names only the trusted `metadata.json`. Refer to Security in the
  administration guide for the trust boundary.
- The reader refuses malformed or hostile metadata rather than crashing or
  returning wrong rows. It rejects a non-regular file, such as a FIFO, named as a
  path. It also rejects a null manifest sequence number, a null or negative
  position-delete position, and a `current-schema-id` that names no schema.
- Reads apply position deletes, equality deletes, and format-version-3 deletion
  vectors. The data files must be Parquet.
- The reader does not write or commit to an Iceberg table, and it reads only the
  current snapshot. Time travel to an older snapshot is not supported.
