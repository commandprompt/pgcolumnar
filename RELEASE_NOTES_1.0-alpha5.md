# pgColumnar 1.0-alpha5 release notes

Release date: 2026-11-06
Previous release: 1.0-alpha4 (2026-09-17)

pgColumnar is a columnar table access method for PostgreSQL. This is the fifth
alpha. These notes are for anyone who runs alpha4 and is deciding whether to
upgrade.

This release has four themes:

- **Time series.** Retention, partitions, cold storage, rollups and bucketed
  queries.
- **Encoding, as measured.** Tables that this release writes are smaller. On the
  ClickBench sample, the same data takes 20.6% fewer bytes.
- **Correct answers under concurrency.** Unique indexes, `EXCEPTION` blocks,
  `UPDATE` races and `recluster` no longer lose or duplicate rows.
- **Safer external data.** The Parquet, Arrow and Iceberg readers refuse a
  damaged file instead of returning wrong values. Object storage reads and
  exports fail cleanly, and `tier_partition` no longer overwrites a file that a
  foreign table reads.

Two parts of this release need your attention before you upgrade:

- This release changes how column chunks are stored. Once it writes to a table,
  an alpha4 build refuses to read that table. Read "On-disk format".
- Some calls that worked in alpha4 now return a different answer or an error.
  Read "Before you upgrade".

The upgrade takes one command per database. See "Upgrading" at the end.

## Contents

- Highlights
- Before you upgrade: changes that may need action
- On-disk format
- New functions and behaviour
- Smaller tables and faster queries
- Settings
- Fixed defects
- Security hardening
- Test and CI changes
- Known issues
- Upgrading
- Scope and limitations

## Highlights

- **Time series.** Seven features for time-series tables. The maintenance daemon
  can apply retention. pg_partman and two new functions manage time partitions.
  A cold partition can move to a Parquet file. Incremental rollups keep
  per-bucket aggregates. See "Time series".
- **Distinct counts from stored sketches** (#1309). A new aggregate,
  `pgcolumnar.approx_count_distinct`, estimates distinct values. On a column
  that keeps sketches, it answers from them: 0.8 ms on 8.75 million rows,
  against 254 ms reading the rows.
- **`GROUP BY` is vectorized by default** (#1292). This changes a default:
  `pgcolumnar.enable_group_vectorization` is now `on`. A grouped query that
  passes `pgcolumnar.groupagg_max_groups` now finishes instead of raising an
  error (#1300).
- **`INSERT ... ON CONFLICT DO NOTHING` works** (#1341). In alpha4 it raised an
  error on every table with a unique index.
- **An older build refuses a table it cannot read** (#1137). A table this
  release has written to carries format version 2. An alpha4 build refuses it on
  every read path, instead of returning a wrong answer. See "On-disk format".
- **Smaller tables.** On ClickBench `hits_0.parquet` (1,000,000 rows, 105
  columns), stored chunk bytes fell from 81,869,112 to 64,984,810.
- **Range columns skip on `&&` and `@>`** (#1144). On a table clustered on the
  range, an overlap query read 1 chunk group of 20 instead of all 20.
- **Less catalog work in a large database.** Catalog lookups use the catalog's
  index once the catalog is large enough.
- **Many defects fixed.** They include rows lost or duplicated, wrong answers,
  and backend crashes. See "Fixed defects".
- **Security hardening.** Row-level security now holds in two places where it
  leaked, and object storage refuses cloud metadata addresses. See "Security
  hardening".

## Before you upgrade: changes that may need action

Each change below can make a call that worked in alpha4 behave differently.
Check whether your application relies on the old behaviour.

**Storage and transactions**

- **An alpha4 build cannot read a table this release has written to** (#1137).
  Plan any rollback before you upgrade. See "On-disk format".
- **A concurrent `UPDATE` of one row fails with `40001`, not `23505`** (#1421).
  This holds on a table with a primary key or unique index. An application
  that retries on `serialization_failure` now retries. Two multi-row `UPDATE`s
  that reach shared rows in opposite orders can now deadlock (`40P01`), as on a
  heap table. On such tables an `UPDATE` took about 25% to 50% longer, measured
  on PostgreSQL 17. A table without a unique index is unchanged.
- **`pgcolumnar.reset_options` refuses a relation that is not columnar**
  (#1265). It used to report success on a heap table, a partitioned table or a
  view. It now raises SQLSTATE `42809`.
- **`pgcolumnar.column_extent` refuses a table whose row-level security policies
  apply to the caller.** It raises `row-level security is in force`.

**Reading Parquet and Iceberg data**

- **A Parquet timestamp read into the other PostgreSQL timestamp type is
  converted in the session's time zone** (#1706). Before, a wall-time column
  read into `timestamptz` was taken as UTC. Same-type reads are unchanged, and so
  is any read in a UTC session.
- **`export_parquet` writes a `timestamp` column as a wall time** (#1706). Other
  readers, such as pyarrow, DuckDB and Spark, took it as a UTC instant before.
  `timestamptz` is still written as an instant.
- **`parquet_schema` advises different types** (#1807, #1893, #1814). It
  advises `timestamp with time zone` for a column that holds instants. It
  reports a nested column under its top-level name. It advises `bigint` for an
  unsigned 32-bit column and `numeric(20,0)` for an unsigned 64-bit column.
- **A declared type modifier now applies to every value read** (#1790).
  `numeric(p,s)` rounds to scale `s` and refuses an overflow with `22003`.
  `varchar(n)` refuses a longer value with `22001`.
- **A string cell that is not valid in the server encoding is refused** with
  `22021` (#1821). Before, invalid UTF-8 came back as text, and an
  `INSERT ... SELECT` could store it.
- **A list of lists can no longer be read into an array column** (#1917).
  Before, it was flattened without an error. `read_parquet`, `import_parquet`
  and the `pgcolumnar_parquet` foreign table now refuse such a file with
  `42804`, even for a query that does not use that column.
- **A remote pattern no longer skips names that begin with `_` below the
  literal prefix** (#1784). `s3://bucket/out/*/*.parquet` now reads
  `out/_temporary/x.parquet`, as a local glob does. To skip Spark staging
  output, read the prefix `s3://bucket/out/`, or use a pattern such as
  `s3://bucket/out/[!_]*/*.parquet`.
- **A remote read fails when another client overwrites the object during the
  read** (#1805, #1867, #1894). The error is `changed while it was being read`
  (SQLSTATE `55000`). Before, the read could return rows from both versions.
  A server whose ETag changes on every request now fails every read. For such a
  server, set `pgcolumnar.objstore_pin_reads` to `off` (#1866).

**Iceberg REST catalogs**

- **A missing namespace is SQLSTATE `3F000`, not `42P01`** (#1820). A missing
  table is still `42P01`.
- **The server form of the `iceberg_rest_*` functions needs `USAGE` on the
  server** (#1819). Grant `USAGE` on the `pgcolumnar_iceberg_catalog` server to
  each role that uses it. Without it, the call fails with `42501`.
- **Some names are refused with `22023` before any request.** These are an
  empty table name (#1931) and an empty namespace (#1922). They also include a
  name or level of `.` or `..` (#1948). So is a level that holds the catalog's
  namespace separator (#1865).
- **A user mapping with a static `token` and only one half of an OAuth2 client
  pair is refused** with `28000` (#1794).

**Exports and object storage**

- **`pgcolumnar.objstore_part_size` refuses a value above 512 MiB** (#1812). If
  `postgresql.conf` sets a larger value, PostgreSQL logs a warning and uses the
  8 MiB default.
- **A part size under 5 MiB fails early on AWS S3, Google Cloud Storage and
  Cloudflare R2** (#1857). The export fails with `22023` when it starts a
  multipart upload. Those stores used to refuse it only after every part had
  gone up.
- **`parallel_export_parquet` refuses some targets before any worker
  starts.** It refuses a `gs://`, `http://` or `https://` prefix with `0A000`
  (#1832). It refuses a bare bucket such as `s3://bucket/` with `22023`
  (#1782).
- **The exports write `s3://` only.** An `s3a://` or `s3n://` target is refused
  with `0A000` (#1930). The readers accept both aliases as `s3://`.
- **`tier_partition` refuses more calls** (#1795). It now writes with your user
  mapping on the server, not with the server process environment. It needs
  both `pg_write_server_files` and `pg_read_server_files`. A role that is not a
  superuser and has no mapping on the server is refused with `28000`. It also
  refuses a target that any foreign table reads; see "Security hardening".

## On-disk format (#1137)

Each columnar table records a format version, `pgcolumnar.storage.format_version`.
This release changes it as follows:

- **This release writes version 2.** Every table it creates starts at 2.
- **It reads versions 1 and 2.** It reads every table alpha4 wrote. It refuses
  any other version, with SQLSTATE `0A000` (`unsupported columnar native format
  version`), before it reads any data.
- **A write raises a table from 1 to 2. A read never does.** An alpha4 table
  stays at 1 until this release first writes a row group into it. Until then,
  alpha4 can still read it. That covers `INSERT`, `COPY`, the new row versions of
  `UPDATE`, `compact_rewrite`, `recluster` and `parallel_copy`. `SELECT` never
  moves it, and neither does `DELETE`. A rewrite builds new storage, which
  starts at 2.
- **alpha4 refuses a version 2 table on every read path.** It does not return a
  wrong answer. Measured with an alpha4 build on PostgreSQL 17: a sequential scan
  and 40 single-row fetches were all refused. That held for a table this release
  created and for an alpha4 table it then appended to. A query on a table with a
  covering projection was refused too.

Why the version moved: each column chunk carries an encoding descriptor (a small
header that records how the chunk's values are encoded). Its version moves from 2
to 3, because a chunk with no nulls no longer stores a validity bitmap (#1130).
An alpha4 build that read such a chunk returned NULL for some rows that hold a
value. Measured before this change: 16 of 40 single-row fetches returned NULL,
and the other 24 raised an error. The format version is what turns that wrong
answer into a refusal.

The native data format stays PGCN v1. The metapage version is unchanged.

## New functions and behaviour

### Time series

Each feature below has a section in [docs/how-to.md](docs/how-to.md), and each
statement is checked by a test suite.

#### The maintenance daemon can apply retention (#1290)

A table declares a retention with `pgcolumnar.set_options(..., ttl_column,
ttl_interval)`. With `pgcolumnar.autovacuum_expire` on, as well as
`pgcolumnar.autovacuum`, each daemon pass runs `pgcolumnar.expire` on every such
table. The setting is off by default, because `expire` deletes rows.

`expire` drops whole row groups whose rows are all older than the cutoff. It
now records each group it retires in a new catalog, `pgcolumnar.retired_group`.
When the column's extent shows that nothing can qualify, it answers in one
index lookup instead of reading every row group's zone map (#1310).

#### Time partitions (#1291, #1297)

pg_partman 5.4.1 manages columnar partitions on PostgreSQL 15 to 19. Set
`default_table_access_method` to `pgcolumnar` for the partitions it creates;
without it, every partition is heap. The how-to shows the setup.

Without pg_partman, `pgcolumnar.partition_create_ahead(parent, width, ahead)`
creates columnar partitions ahead of the incoming rows. It never takes an
`ACCESS EXCLUSIVE` lock on the parent.

`pgcolumnar.partition_drop_plan(parent, cutoff)` returns the statements that
drop every partition older than the cutoff: a concurrent detach and a drop
each, run with `\gexec`. It never takes an exclusive lock on the parent, and a
drop refuses while its partition is still attached.

#### Cold partitions move to Parquet (#1295)

`pgcolumnar.tier_partition(partition, server, path, lock_wait)` writes a cold
columnar partition to a Parquet file. It attaches the file in the partition's
place, as a foreign partition with the same name, bounds, grants and comment.
It runs in one transaction. The parent is locked `ACCESS EXCLUSIVE` from the
detach until commit. Every lock it takes waits at most `lock_wait`.

#### Incremental rollups (#1294)

`pgcolumnar.create_rollup` defines aggregates per `date_bin` bucket and
`group_by` key: `count`, `sum`, `min`, `max` and `avg`.
`pgcolumnar.refresh_rollup` brings the rollup up to date in one statement. It
folds in the row groups added since the last refresh. It recomputes the buckets
of row groups that were deleted from or rewritten. The rollup equals a full
recompute of the same query, except that a `float8` average can differ in the
last digits.

Retention keeps the rollup's history: buckets keep their rows after `expire`
drops the raw data. A refresh refuses (`0A000`) and changes nothing when it
would have to recompute such a bucket. After a rewrite of the source, for
example by `pgcolumnar.vacuum`, a refresh refuses with `55000`.
`pgcolumnar.rebase_rollup` accepts the source as it is now.

#### Bucketed queries (#1292, #1293)

`GROUP BY` is vectorized by default. The planner takes the grouped path only
when the group count cannot exceed `pgcolumnar.groupagg_max_groups`. The
table's row count bounds the groups, and so do the time buckets for a
`date_bin` or `date_trunc` key. The planner estimates the number of buckets
from the time column's range, read in one index lookup (#1304).

`max(ts)` and `min(ts)` on a `timestamptz` column are answered from each row
group's stored bounds, without reading column data. This holds for row groups
with no deleted rows. The how-to has a recipe for the latest reading per device.

#### Gap filling and approximate aggregates (#1296)

The how-to has recipes to fill gaps, carry the last value forward and
interpolate. The `hll` and `tdigest` extensions work over columnar tables and
give exactly what they give over heap. Tested: hll 2.19 on PostgreSQL 15 to 18,
and tdigest 1.4.3 on 15 to 19. hll 2.19 does not build against PostgreSQL 19.

#### Retention accepts a `date` column

`pgcolumnar.expire` now accepts a `date` retention column. Before, it accepted
only `timestamp` and `timestamptz`.

For a `date` column, the cutoff is rounded down to the start of its day. As a
result, `expire` can keep a row up to one day older than the retention window.
It never removes a row that is inside the window.

### Distinct-count sketches (#1309)

`pgcolumnar.approx_count_distinct(value)` is a new aggregate. It estimates the
number of distinct non-NULL values with a HyperLogLog sketch of 2048 registers.
Its standard error is about 2.3%. It works on any table and in parallel plans.

A columnar table can also keep a sketch per row group for chosen columns:

```sql
SELECT pgcolumnar.set_options('t', sketch_columns => ARRAY['user_id']);
```

Each row group written from then on stores about 2 KB per named column, in the
new catalog `pgcolumnar.sketch`. Keeping a sketch added about 3% to insert time
for a `bigint` column and 4% for a 96-byte text column. A table that names no
column pays nothing.

With no `WHERE` and no `GROUP BY`, `approx_count_distinct` on such a column
merges the stored sketches. It reads only the row groups that have deleted rows
or no sketch. The answer equals reading every row. On 8.75 million rows it took
0.8 ms, against 254 ms reading the rows and 310 ms for `hll_add_agg`.
`EXPLAIN ANALYZE` reports `Columnar Sketch Groups Folded` and `Columnar Sketch
Groups Scanned`.

Both the aggregate and the option refuse a type with no extended hash function,
and a nondeterministic collation, with `0A000`.

### Grouped queries finish past the group cap (#1300)

In alpha4, a vectorized `GROUP BY` that grew past
`pgcolumnar.groupagg_max_groups` raised an error. That could happen to a
prepared statement after the cap was lowered or the table grew. Now the query
finishes with the answer core PostgreSQL gives.

- For `count`, `min`, `max`, and `sum` and `avg` over integer and float columns,
  the node hands its groups to a core `Finalize HashAggregate` at the cap.
  `EXPLAIN ANALYZE` reports `Columnar Group Table Flushes` and `Columnar Group
  Table Peak Memory`.
- For `sum` and `avg` over `bigint` and `numeric`, the node writes its groups to
  a sort at the cap. The sort spills within `work_mem`. The node merges the
  groups at the end.

See [docs/configuration.md](docs/configuration.md) for how to size the cap.

### `INSERT ... ON CONFLICT DO NOTHING` (#1341)

`INSERT ... ON CONFLICT DO NOTHING` now works on a columnar table with a unique
index. In alpha4 it raised `0A000` on every such table. Eight sessions racing on
the same keys left 0 duplicate keys.

`ON CONFLICT DO UPDATE` still raises an error when a row conflicts, because
updating that row needs a row lock. With no conflict it inserts.

### Reading Parquet, Arrow and Iceberg data

- **Arrow IPC files** (#1806). `pgcolumnar.import_arrow` reads the Arrow IPC
  file format, including Feather v2.
- **Iceberg metadata compressed with gzip** (#1815). Every Iceberg function
  reads `*.gz.metadata.json` and `*.metadata.json.gz`.
- **`s3a://` and `s3n://` paths** (#1825, #1930). The Iceberg reader and the
  Parquet readers read them as `s3://`.
- **Iceberg type promotion** (#1789). A column promoted from `int` to `long`, or
  from `float` to `double`, reads across data files from both sides.
- **More Parquet column types.** A legacy `INT96` timestamp reads as
  `timestamptz` (#1870). A fixed-length binary column reads into `bytea`
  (#1792). An unsigned integer reads as the value it stores (#1814).
- **Iceberg REST catalogs.** The functions follow the catalog's `GET /v1/config`
  (#1793). Nested namespaces work, and the list functions follow paging. A
  namespace level that contains a dot can be named as a `text[]`, such as
  `ARRAY['a.b']` (#1865). A new function, `iceberg_rest_namespace_levels`,
  lists namespaces in that form. `iceberg_rest_scan` now picks a vended
  credential for each file it reads (#1820).
- **`iceberg_data_files` after a copy-on-write delete** (#1791, #1829). It lists
  such a table instead of refusing it with "has delete files".
- **Clearer errors.** A read over a prefix or glob names the file that did not
  match the declared columns (#1902). A type refusal names the function you
  called (#1871). Catalog errors carry the catalog's own message (#1820).

### Object storage and exports

- **Two new settings.** `pgcolumnar.objstore_pin_reads` turns off the version
  pin of a remote read (#1866). `pgcolumnar.export_group_bytes` caps the bytes
  an export buffers for one row group (#1811). See "Settings".
- **A remote read sees one version of each object** (#1805, #1867, #1894). The
  reader pins the version it opened, for the whole statement. A versioned
  object is read at that version, even if it is overwritten during the read.
- **`export_parquet(rel, path, server)`** (#1795). This new form writes with the
  named server and your user mapping on it. `tier_partition` uses it.
- **Large columns export** (#1811). `export_parquet` and `export_arrow` export a
  table with more than 1 GiB of one column in a row group.
- **A prefix or pattern can name a directory that begins with `_` or `.`**, such
  as `s3://bucket/_landing/` (#1784).
- **`pgcolumnar.path_glob_matches(pattern, path)`** (#1957) is a new function. It
  answers whether the Parquet reader's glob matches a path.
  `tier_partition` uses it.

## Smaller tables and faster queries

### Smaller tables

#### An encoding is kept only when it is smaller after compression (#1132)

The writer chooses an encoding, such as bit-packing, by comparing uncompressed
sizes. The block codec (`zstd` or `lz4`) then compresses the result. An
encoding can shrink the bytes and still make the compressed chunk larger,
because it hides patterns the codec would have found.

The writer now compresses both the encoded chunk and the raw chunk and keeps the
smaller. Measured on the ClickBench sample:

| measure | before | after |
| --- | ---: | ---: |
| stored bytes | 81,869,112 | 78,109,810 (-4.59%) |
| `ClientEventTime` column | 3,530,536 | 1,777,051 (-49.7%) |

The price is write CPU: 4.40% more backend instructions on the same load. Query
results are unchanged.

`pgcolumnar.enable_post_codec_encoding_choice` controls this. It is on by
default. Set it to `off` to restore the earlier choice.

#### A chunk with no nulls stores no validity bitmap (#1130)

A validity bitmap is one bit per row that records which rows are NULL. The
writer stored it for every chunk, uncompressed, even when no row was NULL. It
now leaves the bitmap out of any chunk that holds no null. The decision depends
on the rows written, not on a `NOT NULL` constraint. A nullable column with no
nulls in a chunk also saves the space.

Measured on the ClickBench sample, which has no nulls:

| measure | before | after |
| --- | ---: | ---: |
| stored bytes | 78,109,810 | 64,984,810 (-16.80%) |
| validity bitmap bytes | 13,125,000 | 0 |
| chunks without a bitmap | 0 of 735 | 735 of 735 |

Both changes apply to data this release writes. Data written before the upgrade
keeps its old layout until it is rewritten. This was measured on PostgreSQL
17. A row group that alpha4 wrote kept descriptor version 2 after the upgrade.
The next insert wrote version 3. The table read back every row correctly.

### Range pruning (#1144)

A query with `&&` (overlap) or `@>` (contains) on a range column now skips
chunk groups that cannot match. Before this release, both operators read every
chunk group.

The zone map (the per-group summary the scan uses to skip data) gains the
greatest upper bound of the range column. The minimum and maximum it already
held cannot answer overlap, because a range type sorts by lower bound first.

Measured on 200,000 rows of `tstzrange` in 20 chunk groups:

| table | groups read, before | groups read, after |
| --- | ---: | ---: |
| clustered on the range column | 20 | 1 |
| rows in random order | 20 | 20 |

Clustering is a precondition, not an option. On a table whose ranges are
scattered, every group holds a bound near the maximum, so nothing is skipped.

Three further points:

- A range with no upper bound is never skipped from above.
- The scan compares under the collation the range type was declared with.
- Data written before the upgrade has no summary. It is read in full, and never
  skipped wrongly, until it is rewritten.

See [docs/limitations.md](docs/limitations.md), section "Which predicates
prune".

### Catalog work in a large database

pgColumnar keeps its metadata in catalogs that every columnar table in a
database shares. Many lookups read a whole catalog to find one table's rows. In
a database with many columnar tables, each lookup paid for every other table.
This release routes those lookups through each catalog's existing index. No
index is added and no catalog changes.

An index probe costs more than reading a catalog of one or two pages. So a
lookup uses the index only when the catalog has at least
`pgcolumnar.index_min_blocks` pages. The default is 3. It was chosen from
measurements at 24 catalog sizes on PostgreSQL 15, 17 and 19.

Measured on PostgreSQL 17 with 400 other columnar tables that have projections,
in catalog buffers read:

| operation | catalog | before | after |
| --- | --- | ---: | ---: |
| one-row `DELETE` | `row_group` | 45 | 19 |
| `INSERT` | `storage` | 19 | 13 |
| `pgcolumnar.vacuum_sorted` | `storage` | 128 | 93 |
| `pgcolumnar.truncate` | `free_space` | 26 | 17 |

Other paths that changed:

- **Planning.** Planning a query read `pgcolumnar.storage` in full for the
  newest table. It now reads the table's own row through its metapage. Measured
  on a 37-page catalog: 37 blocks per planned query before, 4 after.
- **Retiring a row group.** Compaction made 7 sequential catalog scans per
  retired row group, plus 3. It now uses the catalogs' indexes instead, once
  they reach `pgcolumnar.index_min_blocks` pages.
- **`DELETE`.** The session that ran a `DELETE` scanned `delete_vector` in full
  once per row group it touched. It now uses the index.
- **Empty catalogs.** `pgcolumnar.options` and `pgcolumnar.projection` are empty
  until you set an option or add a projection. Planning no longer probes their
  indexes while they are empty or small.

### Covering projections and the planner

A covering projection is a sorted copy of some of a table's columns, which a
query can scan instead of the table. These changes affect which plan the planner
chooses. They do not change query results.

- **Parallel scan of a covering projection.** The planner can now scan a covering
  projection in parallel. Before, a plan had to give up either the projection or
  the parallel workers.
- **A projection scan is priced from its own pages.** The planner used the size
  of the whole relation file. That file holds the base table and every
  projection.
- **A base scan is no longer priced from its projections' pages**, for the same
  reason.
- **A projection is priced from what its sort order can skip.** The cost used to
  assume half the table for every query. It now follows the selectivity of
  conditions on the sort key. A condition that mentions the sort key but cannot
  use it to skip, such as `sk BETWEEN 1 AND 2000 OR kind = 'odd'`, earns no
  discount.

### Other speed-ups

- **Iceberg deletes.** `iceberg_scan` applies equality deletes through a hash
  table (#1817). Before, 50,000 rows against 32,000 delete keys took 6 s. It
  finds each data file's position deletes through an index (#1868). On a table
  of 32 data files and 106 delete rows, the scan visits 104 rows instead of
  3392.
- **`pgcolumnar.stats()` and `pgcolumnar.sort_status()`** no longer read more as
  the database holds more columnar tables (#1637). With 20 other tables,
  `stats()` read 104 blocks of the table and `sort_status()` read 170. Each now
  reads 1. `refresh_rollup` had the same defect and is fixed too (#1614).
- **A join whose filtered dimension is empty** no longer reads the fact table
  (#1447).
- **A `cidr` or `varchar` column, or a domain over either, skips chunk groups**
  (#1433). Before, such a column was read in full.
- **Parquet row-group skipping uses a constant of another integer or float
  type** (#1712). `WHERE id > 150000` on a `bigint` column now skips row groups.

## Settings

Five settings are added, one default changes, and one range narrows. None is
removed.

| setting | type | default | purpose |
| --- | --- | --- | --- |
| `pgcolumnar.enable_group_vectorization` | boolean | `on` (was `off`) | Vectorize `GROUP BY`. The planner takes the path only when the group count cannot exceed `pgcolumnar.groupagg_max_groups`. `off` restores alpha4's plans. |
| `pgcolumnar.autovacuum_expire` | boolean | `off` | Let the maintenance daemon run `pgcolumnar.expire` on tables that declare a retention. |
| `pgcolumnar.enable_post_codec_encoding_choice` | boolean | `on` | Keep an encoding only when it is smaller after block compression. `off` restores the earlier choice. |
| `pgcolumnar.index_min_blocks` | integer | `3` | Page count at or above which a catalog lookup uses the catalog's index. `0` always uses the index. A very large value never does. |
| `pgcolumnar.objstore_pin_reads` | boolean | `on` | Pin each remote read to the version of the object it opened. Turn it off only for a server whose ETag changes on every request for the same bytes. Off, a read of an object overwritten during the read can return rows from both versions. |
| `pgcolumnar.export_group_bytes` | integer | `268435456` (256 MiB) | The most bytes an export buffers for one Parquet row group or Arrow record batch. Range 1024 to 256 MiB. Lower it to bound the memory an export holds. |
| `pgcolumnar.objstore_part_size` | integer | `0` (unchanged) | The range is now 0 to 512 MiB. It was 0 to `INT_MAX`. |

`pgcolumnar.autovacuum_expire` is set in the server configuration and takes
effect on reload. The others can be set per session. Two developer settings,
`pgcolumnar.rollup_pause` and `pgcolumnar.tier_fault_after`, exist for the
test suites; only a superuser can set them. See
[docs/configuration.md](docs/configuration.md).

## Fixed defects

### Rows lost, duplicated or wrong

- **A plpgsql `EXCEPTION` block that rolled back lost the enclosing statement's
  rows** (#1341, #1360). An `INSERT ... SELECT` that called such a function on
  each row kept 1 row of 5000. This happened when the block wrote to the table,
  and also when it only read it.
- **A unique index could hold two rows with one key** (#1341). An insert that
  raced an uncommitted delete of the same key did not wait for it. When the
  delete rolled back, the key had two rows.
- **An index built after a `DELETE` missed rows** for a transaction whose
  snapshot began before the build (#1341).
- **`pgcolumnar.recluster` duplicated rows** inserted while it waited for its
  row groups (#1633).
- **Changing a column's collation gave wrong answers** (#1327).
  `ALTER COLUMN ... TYPE text COLLATE <other>` kept zone maps ordered under the
  old collation. Such a change now forgets the column's stored bounds.
- **`min` and `max` with a `COLLATE` override ignored it** (#1771).
- **A `timestamp` column compared with a `timestamptz` constant lost rows across
  a spring-forward gap** (#1738).
- **An ungrouped aggregate ignored a `WHERE` that names no column** (#1435).
  `SELECT count(*) FROM t WHERE (SELECT false)` returned every row's count.
- **A rewriting `ALTER TABLE` or `REFRESH MATERIALIZED VIEW` dropped the
  table's own options** for the rows it rewrote (#1309). The rows were written
  with the default options.
- **`pgcolumnar.analyze()` could skip columns without saying so** (#1276), on a
  table with a covering projection.

### Crashes and errors that should not happen

- **`COMMIT PREPARED` or `ROLLBACK PREPARED` in the session that prepared a
  columnar write crashed the backend** (#1323).
- **A partitioned table that names `pgcolumnar` could abort the backend**
  (#1259). Functions such as `pgcolumnar.stats` now treat the partitioned parent
  as not columnar.
- **`pgcolumnar.import_parquet` crashed the server on a partitioned parent**
  (#1263). It now refuses a target without storage.
- **An `ALTER TABLE` on a partitioned columnar table could open partitions
  without a lock.** An assert-enabled build aborted.
- **A join with a `WHERE` that names no column failed** (#1437), and an
  assert-enabled build aborted.
- **A deep glob pattern crashed the Parquet reader** (#1968, #1972). Such a
  pattern is now refused with `54000`.
- **An export to an `http://` or `https://` URL crashed the backend** (#1779).
- **A `DELETE` failed when its condition called a function that deleted
  another row of the same table** (#1394).
- **A NOLOGIN role could not run `parallel_export_parquet`, `parallel_copy` or
  the parallel flush** on PostgreSQL 15 and 16.
- **A failed or twice-cancelled parallel flush leaked shared memory** until the
  server restarted (#1745, #1747).

### Catalog and maintenance

- **`CREATE MATERIALIZED VIEW ... USING pgcolumnar ... WITH DATA` and
  `ALTER TABLE ... ALTER COLUMN TYPE` left a wrong catalog entry** (#1275,
  #1211). See "After the upgrade: repair entries alpha4 left wrong".
- **A columnar materialized view can hold per-table options** (#1265).
- **`REINDEX` reclaims the entries of deleted rows** that no snapshot can see
  (#1407). Before, it reclaimed none.
- **Rollups.** Dropping a rollup's source no longer leaves the rollup broken, and
  dropping a rollup removes its partials and catalog rows (#1376). `pg_dump`
  now carries a rollup's definition (#1377).
- **In the upgrade window, an error names the fix.** Before you run
  `ALTER EXTENSION pgcolumnar UPDATE`, `DROP TABLE`, `TRUNCATE` and
  `pgcolumnar.vacuum()` fail and change nothing. The error now has a hint that
  names `ALTER EXTENSION pgcolumnar UPDATE`.
- **A build with a `PG_CONFIG` that gives no PGXS now stops with an error**
  (#1344). Before, it could wait forever.

### Parquet, Arrow and Iceberg data

Each fix below replaced a wrong answer, or an error that named the wrong cause.

- **Parquet values.** The readers apply declared type modifiers (#1790). They
  read unsigned integers (#1814) and a column that is NULL in every row under
  GZIP, ZSTD or LZ4_RAW (#1786).
- **Iceberg tables.** `iceberg_scan` reads `file_format` spelled `parquet`
  (#1825). The `pgcolumnar_iceberg` wrapper no longer prunes `bucket[N]` files
  under a nondeterministic collation (#1818). A struct, list or map column
  declared with a scalar type is refused instead of read as NULL (#1788).
  `iceberg_data_files` shows every partition value exactly (#1816).
- **Iceberg REST catalogs.** A reply that ends when the server closes the
  connection is read (#1787). Before, a table with about 41 or more snapshots
  could not be read through the Apache Iceberg REST catalog.
- **Arrow.** `import_arrow` refuses a nested type the column cannot hold with
  `42804` (#1932). Before, a large list imported as wrong values.

### Exports and object storage

- **`parallel_export_parquet` to `s3://`.** A prefix that ends in `/` no longer
  writes `prefix//part-NNNN.parquet` (#1782). The export no longer deletes
  parts under a sibling prefix (#1782). A run that fails removes its parts
  (#1781). The workers obey the session's object-store settings (#1780).
- **Cancelled exports.** A cancelled export no longer leaves multipart uploads
  open on `s3://` (#1810, #1864) or `gs://` (#1854). An export cancelled after
  the store committed the object now says that it wrote it (#1813). A second
  cancel no longer reports `errstart was not called` (#1741).
- **Store replies and limits.** An export reads a reply that ends when the store
  closes the connection (#1830). It checks the S3 multipart limits before it
  sends a part (#1857). A store that refuses an upload before it reads the
  body is reported with its `403` (#1902).
- **Reads.** Prefix and pattern reads work under
  `pgcolumnar.objstore_s3_addressing = 'virtual'` (#1783). An exact `s3://` key
  that holds `*`, `?` or `[` reads that object alone (#1785). An `http(s)://`
  URL with a query string gets well-formed requests (#1904).
- **Errors.** An object-store error names the URL you gave, not
  `s3://<bucket>/_` (#1869). Three refusals give advice that fits them (#1796).
- **Functions that read local files only now say so.** `import_arrow`,
  `file_split_offsets`, `parallel_copy`, `read_avro_manifest` and
  `read_manifest_list` refuse an object-storage URL with `0A000` (#1886, #1913,
  #1918). Before, they failed with "could not open file".

## Security hardening

- **Row-level security.** A vectorized aggregate no longer runs a
  non-leakproof function on rows a policy hides. `pgcolumnar.column_extent`
  refuses a table whose policies apply to the caller.
- **Iceberg REST server privileges** (#1819). The server form of the
  `iceberg_rest_*` functions checks `USAGE` on the server before it reads a user
  mapping.
- **Planning an Iceberg foreign table** no longer reads server files for a role
  the scan would refuse. Before, `EXPLAIN` showed such a role the row count, and
  revealed whether a server path exists.
- **`pgcolumnar.truncate()` checks ownership first** (#1384). Before, a role
  that does not own the table could queue for its lock and hold up other
  sessions. It could also learn what kind of relation it named.
- **Cloud metadata addresses** (#1808, #1848). The object-storage module refuses
  the IPv6 metadata addresses of AWS and Google Compute Engine, and Alibaba
  Cloud's `100.100.100.200`. It also refuses them behind the standard NAT64
  prefixes. This holds even when the host is listed in
  `pgcolumnar.objstore_allowed_endpoints`.
- **`tier_partition` no longer overwrites a file that a foreign table
  reads.** It refuses a target read through a directory or a glob (#1941). It
  refuses one spelled another way (#1936, #1965), one under an Iceberg location
  (#1949), and one reached through a relative path (#2001, #2004). It matches
  each glob with the reader's own code (#1957). A symbolic link is still not
  resolved; [docs/how-to.md](docs/how-to.md) states this limit.
- **`tier_partition` writes with your credentials** (#1795). Before, it wrote
  with the server process environment's keys.
- **Bounded glob expansion** (#1973, #1984). A glob that would walk more than
  1,000,000 names is refused with `54000`, and a cancel stops it. Before, a
  short pattern could run for minutes, grow the backend to 2.2 GB and ignore
  `statement_timeout`.
- **Damaged files are refused, not misread.** The Parquet reader refuses a
  footer, schema or page header that it cannot parse exactly. See #1822, #1852,
  #1855, #1910, #1921 and #1935. The Iceberg reader refuses malformed numbers and
  out-of-range values in metadata and manifests (#1823, #1824, #1853, #1878).
  Each could give a wrong result with no error.
- **Request paths.** The Iceberg REST functions refuse names that would make an
  empty, `.` or `..` path segment (#1922, #1931, #1948). A proxy could have sent
  such a request to another resource.

## Test and CI changes

These change no product behaviour.

- The suite `native_scale` now runs in CI (#2018).
- Twelve suites no longer fail when another session holds an old snapshot.
  Shared helpers wait for the snapshot horizon, and report what holds it when
  the wait runs out (#1975, #1979, #1981, #1987, #1993, #2010).
- The harness self-test finds more checks that print only `no` when they fail.
  The checks it found now print their measurements (#1980, #1986, #1997, #2002,
  #2023, #2027).
- The test harness refuses a server that started before its library was
  installed, and fails a run when the library changes during it (#1915,
  #1956).
- The Parquet fuzz test checks the answer of every damaged file that reads
  without an error (#1852).
- The listing suites can run against a real S3 service, as an opt-in
  (#1798).
- The check ledger records its census in a commit trailer, so two pull requests
  no longer conflict over it (#1431).

## Known issues

- **A rollup refresh refuses some changes** (#1294). A refresh refuses when it
  would recompute a bucket that holds retained history. A delete from a row
  group that also reaches such a bucket is refused too, even if the deleted rows
  are recent. A refresh also refuses when rows were deleted from a row group
  that `expire` retired before the refresh. Run `pgcolumnar.rebase_rollup` if
  the rollup is right as it is.
- **Setting a codec can make a table larger, on high-entropy text** (#1074).
  This is unchanged from alpha4. Measured on this release, 200,000 rows of
  random 64-character hex text:

  | `pgcolumnar.compression` | relation bytes | against `none` |
  | --- | ---: | ---: |
  | `none` | 7,036,928 | 100% |
  | `lz4` | 7,036,928 | 100% |
  | `zstd` | 7,135,232 | 101.40% |

  If a table stores long high-entropy text, measure both settings. See
  [docs/features.md](docs/features.md).

## Upgrading

### Before you start

- You need:
  - the alpha5 build
  - the same `PG_CONFIG` you installed alpha4 with
  - permission to restart the server
  - the right to run `ALTER EXTENSION` in each database that has the extension
- List the databases you can connect to. You will visit each one:

  ```sql
  SELECT datname FROM pg_database WHERE datallowconn;
  ```

### Steps

1. Run `make install` with the same `PG_CONFIG`.

   This also installs the object-storage module, `pgcolumnar_objstore`. The
   alpha5 library ignores an alpha4 module, with a warning, so install both
   from the same build.

   **WARNING:** Plan any rollback before you write with alpha5. Once alpha5
   writes to a table, that table carries format version 2, and alpha4 refuses to
   read it. A table alpha5 has only read stays readable by alpha4. To roll back
   after alpha5 has written, restore from a backup taken before the upgrade.

2. Restart the server, so that it loads the new library.
3. Connect to one database from the list.
4. Check whether the extension is installed there:

   ```sql
   SELECT extversion FROM pg_extension WHERE extname = 'pgcolumnar';
   ```

   If the query returns no row, skip to step 7.

5. Run:

   ```sql
   ALTER EXTENSION pgcolumnar UPDATE;
   ```

6. Run the query from step 4 again.

   Expected result: `1.0-alpha5`.

7. Repeat steps 3 to 6 for every other database in the list.

### What the upgrade changes

- It adds the column `max_upper` (type `bytea`) to `pgcolumnar.zone_map`.
  Existing rows get NULL, which means "no summary".
- It adds the column `sketch_columns` (type `name[]`) to `pgcolumnar.options`.
- It adds five catalogs: `pgcolumnar.retired_group`, `pgcolumnar.column_extent`,
  `pgcolumnar.rollup`, `pgcolumnar.rollup_group` and `pgcolumnar.sketch`. The
  two rollup catalogs carry row-level security, so each role sees only the
  rollups it owns. `pg_dump` carries the rows of `pgcolumnar.rollup`.
- It adds the functions `partition_create_ahead`, `partition_drop_plan`,
  `tier_partition`, `create_rollup`, `refresh_rollup`, `rebase_rollup`,
  `column_extent`, `row_tid`, `path_glob_matches` and
  `iceberg_rest_namespace_levels`, and the aggregate `approx_count_distinct`.
- It adds new forms of existing functions: `export_parquet(rel, path, server)`,
  and `iceberg_rest_table_location`, `iceberg_rest_scan` and
  `iceberg_rest_tables` with the namespace as a `text[]`.
- It also adds internal helpers: three for rollups, one for partitions, one for
  sketches, and the five support functions of `approx_count_distinct`.
- `pgcolumnar.column_extent` starts empty for existing tables. Until a table's
  next `expire`, `compact_rewrite`, `VACUUM` or `recluster`, the planner reads
  its zone maps instead. The answer is the same; only planning costs more.
- It replaces `pgcolumnar.set_options`. The new version also accepts a columnar
  materialized view and the `sketch_columns` option.
- It replaces `pgcolumnar.reset_options`. The new version refuses any relation
  that `set_options` refuses. A script that called it on a heap table, a
  partitioned table or a view used to succeed. It now raises an error.
- It replaces `pgcolumnar."analyze"`. The new version finds a table's storage
  through the table's metapage.
- It replaces `pgcolumnar.stats` and `pgcolumnar.sort_status`. Each now reads
  the table's storage id once.
- No table data is converted, and no table's format version changes. A table
  moves from version 1 to 2 the first time alpha5 writes to it.
- It does not repair a catalog entry that alpha4 left wrong. See "After the
  upgrade: repair entries alpha4 left wrong".

### After the upgrade: repair entries alpha4 left wrong

Two alpha4 defects, fixed in this release (#1211, #1275), left a table's storage
row naming a relation that no longer exists. The upgrade does not repair rows
that already exist. The table still reads and writes correctly, and `DROP TABLE`
still removes the row. Repair them anyway, so the catalog describes your tables.

This was measured on PostgreSQL 17, on tables created under alpha4. After the
upgrade, the rows stayed wrong through reads, inserts, `pgcolumnar.analyze()`
and `VACUUM`. Each repair below made them correct.

1. In each database, find the rows that name no relation:

   ```sql
   SELECT s.storage_id, s.relation_oid
   FROM pgcolumnar.storage s
   WHERE NOT EXISTS (SELECT 1 FROM pg_class c WHERE c.oid = s.relation_oid);
   ```

   If the query returns no row, this database needs nothing.

2. For each columnar materialized view, run:

   ```sql
   REFRESH MATERIALIZED VIEW view_name;
   ```

   **CAUTION:** the next step takes an `ACCESS EXCLUSIVE` lock and rewrites the
   whole table. Run it when the table can be locked for that long.

3. For each columnar table that ran `ALTER TABLE ... ALTER COLUMN ... TYPE`
   under alpha4, rewrite it once. Converting a column to its own type with
   `USING` forces the rewrite and changes no data:

   ```sql
   ALTER TABLE table_name ALTER COLUMN column_name TYPE column_type USING column_name;
   ```

4. Run the query from step 1 again.

   Expected result: no row.

See [docs/installation.md](docs/installation.md) for the full upgrade procedure.

## Scope and limitations

- This is an alpha. Interfaces may change before 1.0.
- Cascading encodings (one encoding applied on top of another) are not in this
  release. Issue #1139 tracks them until a measurement shows a size win.
- `pgcolumnar.vacuum_full` still skips columnar materialized views.
- `INSERT ... ON CONFLICT DO UPDATE` raises an error when a row conflicts. See
  [docs/limitations.md](docs/limitations.md).
- A GIN or BRIN index builds on a columnar table, but the planner can never use
  it. Both need a bitmap scan, which the columnar access method does not
  provide. See [docs/limitations.md](docs/limitations.md).
- On PGXN this release is `1.0.0-alpha.5`, while `CREATE EXTENSION` reports
  `1.0-alpha5`. PGXN requires a semantic version, which needs three integer
  components. The two names refer to the same release.

The complete, itemized list of changes is in `CHANGELOG.md`.
