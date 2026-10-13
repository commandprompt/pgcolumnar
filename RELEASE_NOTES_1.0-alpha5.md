# pgColumnar 1.0-alpha5 release notes

Release date: 2026-10-13
Previous release: 1.0-alpha4 (2026-09-17)

pgColumnar is a columnar table access method for PostgreSQL. This is the fifth
alpha. It has two themes. The first is time series: retention, partitions, cold
storage, rollups and bucketed queries. The second is encoding, as measured. Tables that this release writes are
smaller: on the ClickBench sample, the same data takes 20.6% fewer bytes than
before this release's two encoding changes. Range columns can now skip chunk
groups on overlap and containment queries. Catalog reads cost less in a database
with many columnar tables.

This release changes how column chunks are stored. Once it writes to a table,
an alpha4 build refuses to read that table. Read "On-disk format" before you
upgrade.

This release requires one upgrade command. See "Upgrading" at the end.

## Highlights

- **Time series.** Seven features for time-series tables. The maintenance daemon
  can apply retention. pg_partman and two new functions manage time partitions.
  A cold partition can move to a Parquet file. Incremental rollups keep
  per-bucket aggregates. See "Time series".
- **`GROUP BY` is vectorized by default** (#1292). This changes a default:
  `pgcolumnar.enable_group_vectorization` is now `on`. See "Settings".
- **An older build refuses a table it cannot read** (#1137). A table this
  release has written to carries format version 2. An alpha4 build refuses it on
  every read path, instead of returning a wrong answer. See "On-disk format".
- **Smaller tables.** On ClickBench `hits_0.parquet` (1,000,000 rows, 105
  columns), stored chunk bytes fell from 81,869,112 to 64,984,810. Two changes
  produce this. An encoding is kept only when it is smaller after compression.
  A chunk that holds no null stores no validity bitmap.
- **Range columns skip on `&&` and `@>`.** A zone map now records the greatest
  upper bound of a range column. On a table clustered on the range, an overlap
  query read 1 chunk group of 20 instead of all 20.
- **Less catalog work in a large database.** The pgcolumnar catalogs are shared
  by every columnar table in a database. Many lookups used to read a whole
  catalog. They now use its index once the catalog is large enough. A new
  setting, `pgcolumnar.index_min_blocks`, sets that size.
- **Two crashes fixed.** Both involve a partitioned table that names
  `pgcolumnar` as its access method. See "Fixed".
- **Columnar materialized views work in more places.** A columnar materialized
  view can now hold per-table options. Its catalog entry is correct from the
  moment it is created.

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

## Time series

Each feature below has a section in [docs/how-to.md](docs/how-to.md), and each
statement is checked by a test suite.

### The maintenance daemon can apply retention (#1290)

A table declares a retention with `pgcolumnar.set_options(..., ttl_column,
ttl_interval)`. With `pgcolumnar.autovacuum_expire` on, as well as
`pgcolumnar.autovacuum`, each daemon pass runs `pgcolumnar.expire` on every such
table. The setting is off by default, because `expire` deletes rows.

`expire` drops whole row groups whose rows are all older than the cutoff. It
now records each group it retires in a new catalog, `pgcolumnar.retired_group`.
When the column's extent shows that nothing can qualify, it answers in one
index lookup instead of reading every row group's zone map (#1310).

### Time partitions (#1291, #1297)

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

### Cold partitions move to Parquet (#1295)

`pgcolumnar.tier_partition(partition, server, path, lock_wait)` writes a cold
columnar partition to a Parquet file. It attaches the file in the partition's
place, as a foreign partition with the same name, bounds, grants and comment.
It runs in one transaction. The parent is locked `ACCESS EXCLUSIVE` from the
detach until commit. Every lock it takes waits at most `lock_wait`.

### Incremental rollups (#1294)

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

### Bucketed queries (#1292, #1293)

`GROUP BY` is vectorized by default. The planner takes the grouped path only
when the group count cannot exceed `pgcolumnar.groupagg_max_groups`. The
table's row count bounds the groups, and so do the time buckets for a
`date_bin` or `date_trunc` key. The planner estimates the number of buckets
from the time column's range, read in one index lookup (#1304).

`max(ts)` and `min(ts)` on a `timestamptz` column are answered from each row
group's stored bounds, without reading column data, for row groups with no
deleted rows. The how-to has a recipe for the latest reading per device.

### Gap filling and approximate aggregates (#1296)

The how-to has recipes to fill gaps, carry the last value forward and
interpolate. The `hll` and `tdigest` extensions work over columnar tables and
give exactly what they give over heap. Tested: hll 2.19 on PostgreSQL 15 to 18,
and tdigest 1.4.3 on 15 to 19. hll 2.19 does not build against PostgreSQL 19.

## Smaller tables

### An encoding is kept only when it is smaller after compression (#1132)

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

### A chunk with no nulls stores no validity bitmap (#1130)

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

## Range pruning (#1144)

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

## Catalog work in a large database

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

## Covering projections and the planner

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

## Retention

`pgcolumnar.expire` now accepts a `date` retention column. Before, it accepted
only `timestamp` and `timestamptz`.

For a `date` column, the cutoff is rounded down to the start of its day. As a
result, `expire` can keep a row up to one day older than the retention window.
It never removes a row that is inside the window.

## Settings

Three settings are added and one default changes. None is removed.

| setting | type | default | purpose |
| --- | --- | --- | --- |
| `pgcolumnar.enable_group_vectorization` | boolean | `on` (was `off`) | Vectorize `GROUP BY`. The planner takes the path only when the group count cannot exceed `pgcolumnar.groupagg_max_groups`. `off` restores alpha4's plans. |
| `pgcolumnar.autovacuum_expire` | boolean | `off` | Let the maintenance daemon run `pgcolumnar.expire` on tables that declare a retention. |
| `pgcolumnar.enable_post_codec_encoding_choice` | boolean | `on` | Keep an encoding only when it is smaller after block compression. `off` restores the earlier choice. |
| `pgcolumnar.index_min_blocks` | integer | `3` | Page count at or above which a catalog lookup uses the catalog's index. `0` always uses the index. A very large value never does. |

`pgcolumnar.autovacuum_expire` is set in the server configuration and takes
effect on reload. The others can be set per session. Two developer settings,
`pgcolumnar.rollup_pause` and `pgcolumnar.tier_fault_after`, exist for the
test suites; only a superuser can set them. See
[docs/configuration.md](docs/configuration.md).

## Fixed

- **Changing a column's collation gave wrong answers** (#1327).
  `ALTER COLUMN ... TYPE text COLLATE <other>` keeps the table's files, so the
  zone maps kept bounds ordered under the old collation. Range and equality
  queries and `min()`/`max()` then skipped rows. Such a change now forgets the
  column's stored bounds.
- **`COMMIT PREPARED` in the session that prepared a columnar write crashed the
  backend** (#1323). The same holds for `ROLLBACK PREPARED`.
- **An `ALTER TABLE` on a partitioned columnar table could open partitions
  without a lock.** This happened for `DETACH PARTITION`, `ATTACH PARTITION` and
  `ALTER TABLE ONLY` when the parent had two or more columnar partitions. An
  assert-enabled build aborted.
- **A partitioned table that names `pgcolumnar` could abort the backend**
  (#1259). From PostgreSQL 17, a partitioned table can carry
  `USING pgcolumnar` as the default for its partitions. It has no storage of its
  own. Functions such as `pgcolumnar.stats` and `pgcolumnar.sort_status` treated
  it as a columnar table. An assert-enabled build aborted. A production build
  returned a wrong answer instead. These functions now treat the partitioned
  parent as not columnar.
- **`pgcolumnar.import_parquet` crashed the server on a partitioned parent**
  (#1263). It now refuses a target without storage. The crash was a
  segmentation fault on production builds too. Importing into a heap table still
  works.
- **`CREATE MATERIALIZED VIEW ... USING pgcolumnar ... WITH DATA` left a wrong
  catalog entry** (#1275). The view's storage row named a relation that no
  longer existed until the first `REFRESH`. It now names the view.
- **`ALTER TABLE ... ALTER COLUMN TYPE` left a wrong catalog entry** (#1211).
  After the rewrite, the table's storage row named the dropped temporary
  relation. The rows stayed readable. The storage row now names the table.
- **`pgcolumnar.analyze()` could skip columns without saying so** (#1276). On a
  table with a covering projection, it could resolve the projection's storage
  instead of the table's. Measured on a five-column table with a one-column
  projection: statistics for 2 columns instead of 5, and a successful return.
- **A columnar materialized view can hold per-table options** (#1265).
  `pgcolumnar.set_options` and `pgcolumnar.reset_options` now accept one, and
  `DROP MATERIALIZED VIEW` removes its options.
- **`pgcolumnar.reset_options` reported success on a non-columnar relation**
  (#1265). It accepted a heap table, a partitioned table and a view, while
  `set_options` refused them. It now refuses the same relations, with SQLSTATE
  `42809`.

## Known issues

- **A grouped query can still hit `pgcolumnar.groupagg_max_groups`.** The
  planner checks the cap when it plans. A prepared statement planned before the
  cap was lowered, or before the table grew past it, raises an error at
  execution. Raise the cap, or set `pgcolumnar.enable_group_vectorization` to
  `off`. Spilling instead of erroring is #1300.
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
- It adds four catalogs: `pgcolumnar.retired_group`, `pgcolumnar.column_extent`,
  `pgcolumnar.rollup` and `pgcolumnar.rollup_group`. The two rollup catalogs carry
  row-level security, so each role sees only the rollups it owns.
- It adds the functions `partition_create_ahead`, `partition_drop_plan`,
  `tier_partition`, `create_rollup`, `refresh_rollup`, `rebase_rollup`,
  `column_extent` and `row_tid`. It also adds three internal helpers: two for
  rollups and one for partitions.
- `pgcolumnar.column_extent` starts empty for existing tables. Until a table's
  next `expire`, `compact_rewrite`, `VACUUM` or `recluster`, the planner reads
  its zone maps instead. The answer is the same; only planning costs more.
- It replaces `pgcolumnar.set_options`. The new version also accepts a columnar
  materialized view.
- It replaces `pgcolumnar.reset_options`. The new version refuses any relation
  that `set_options` refuses. A script that called it on a heap table, a
  partitioned table or a view used to succeed. It now raises an error.
- It replaces `pgcolumnar."analyze"`. The new version finds a table's storage
  through the table's metapage.
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
- A GIN or BRIN index builds on a columnar table, but the planner can never use
  it. Both need a bitmap scan, which the columnar access method does not
  provide. See [docs/limitations.md](docs/limitations.md).
- On PGXN this release is `1.0.0-alpha.5`, while `CREATE EXTENSION` reports
  `1.0-alpha5`. PGXN requires a semantic version, which needs three integer
  components. The two names refer to the same release.

The complete, itemized list of changes is in `CHANGELOG.md`.
