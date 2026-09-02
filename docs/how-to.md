# How-to guides

Task-focused recipes for each pgColumnar feature. Each recipe ends with a short
tuning note, and a permissions note where one applies. For the advice behind these
tuning notes, collected by decision rather than by task, see the
[Best practices](best-practices.md) guide. For full function signatures see the
[SQL reference](sql-reference.md). For every server setting see the
[Configuration reference](configuration.md).

Load `pgcolumnar` in `shared_preload_libraries` before you begin. It installs the
planner and executor hooks that every backend needs.

## Create a columnar table

Create a new table with the access method, or convert an existing heap table.

```sql
-- new table
CREATE TABLE events (id bigint, ts timestamptz, region text, amount numeric)
  USING pgcolumnar;

-- convert an existing table (heap to columnar, or back)
SELECT pgcolumnar.alter_table_set_access_method('events', 'pgcolumnar');
```

On PostgreSQL 15 and later the conversion runs `ALTER TABLE ... SET ACCESS
METHOD` in place and keeps the table identity and dependents. On 13 and 14 it
copies through a new table and swaps the names, so the original OID and its
dependent objects are not preserved.

**Tuning.** Columnar storage rewards wide scans over a few columns. Keep small,
highly selective point-lookup tables as heap. Put analytic and append-heavy
tables on `pgcolumnar`.

## Load data efficiently

`COPY` and multi-row `INSERT` both work. A bulk load writes best in large
transactions, because each transaction seals its own row groups.

```sql
COPY events FROM '/data/events.csv' WITH (FORMAT csv, HEADER true);
```

Load a local file in parallel with a pool of background writers.

```sql
SELECT pgcolumnar.parallel_copy('events', '/data/events.csv');   -- workers auto
SELECT pgcolumnar.parallel_copy('events', '/data/events.csv', 8);
```

**Tuning.** Many small commits produce many small row groups, which read less
efficiently. Run `pgcolumnar.vacuum` after a trickle load to combine them.

**Permissions.** The caller of `parallel_copy` needs the `pg_read_server_files`
role.

## Choose compression and encoding

Each column is encoded automatically (run-length, frame-of-reference,
bit-packing, delta, delta-of-delta, Gorilla XOR, dictionary, and FSST for text).
A block codec then compresses the encoded chunk. The codec is `zstd` by default.

```sql
-- per table
SELECT pgcolumnar.set_options('events', compression => 'zstd', compression_level => 6);
SELECT pgcolumnar.set_options('events', encode_effort => 'fast');

-- session default for new chunks
SET pgcolumnar.compression = 'lz4';
```

The codec is one of `none`, `pglz`, `lz4`, or `zstd`. `compression_level`
applies to `zstd` and ranges from 1 to 22, default 3. `encode_effort` is `full`
by default, or `fast` to skip the costly FSST search on a text-heavy load.

**Tuning.** Use `zstd` for the best ratio and `lz4` for the fastest
decompression. Raise `compression_level` toward its maximum of 22 for cold data
that is written once and read rarely. Set `encode_effort` to `fast` when a text
load is CPU-bound and the ratio matters less. Re-run `pgcolumnar.vacuum` to
re-encode existing data under new options.

## Sort a table to skip more chunk groups

pgColumnar records a per-chunk minimum and maximum in a zone map. A scan skips a
chunk group when its filter cannot match that range. Sorting the table on the
columns you filter tightens those ranges, so more groups are skipped.

```sql
-- one-shot ascending sort on a key
SELECT pgcolumnar.vacuum_sorted('events', 'region', 'ts');

-- declare a key, then re-apply it later with no arguments
SELECT pgcolumnar.set_options('events', sort_by => ARRAY['region','ts']);
SELECT pgcolumnar.vacuum_sorted('events');

-- Z-order (Morton) clustering over several columns at once
SELECT pgcolumnar.cluster('events', 'customer_id', 'amount');
```

`vacuum_sorted` sorts ascending and tightens the first column most. `cluster`
uses a Z-order curve, so filters on more than one of its columns all skip more
groups.

`cluster` and `recluster` take at most eight key columns, and each must be a
boolean, an integer, a floating-point, a `date` or a timestamp. They do not take
`numeric` or `text`. Sort on a `numeric` or `text` column with
`pgcolumnar.vacuum_sorted` instead.

**Tuning.** Sort on the column your selective queries filter on. A sort is a
trade-off: it groups one dimension tightly while spreading the others. For a live
table use `pgcolumnar.recluster`, which re-establishes the same Z-order under a
weaker lock so reads and writes continue. Read `pgcolumnar.sort_status` to see how
much order remains, and re-sort when it decays.

```sql
SELECT pgcolumnar.recluster('events', 'customer_id', 'amount');   -- online
SELECT * FROM pgcolumnar.sort_status('events');
```

## Skip more chunk groups on equality filters

A bloom filter skips a chunk group when an equality filter names a value the group
does not hold. It complements the minimum and maximum, which skip range filters
only. Bloom filtering is on by default (`pgcolumnar.enable_bloom_filter`).

```sql
EXPLAIN (ANALYZE) SELECT * FROM events WHERE customer_id = 42;
```

**Tuning.** It works best on a clustered or sorted column, because equal values
then sit in few chunk groups. Read `Columnar Chunk Groups Removed by Filter` to
confirm the skip. A scattered high-cardinality column gains little. Turn the
feature off with `SET pgcolumnar.enable_bloom_filter = off` to compare.

## Add a projection for a second sort order

A table has one physical sort order. A projection stores a column subset a second
time under a different sort key, so a second access pattern also prunes well. The
planner reads the projection instead of the base table when the projection covers
every column the query needs and gives a lower cost.

```sql
SELECT pgcolumnar.add_projection(
    'events', 'events_by_customer',
    columns  => ARRAY['customer_id', 'amount', 'ts'],
    sort_key => ARRAY['customer_id']);

EXPLAIN SELECT sum(amount) FROM events WHERE customer_id = 42;
```

Adding a projection fills it from the existing rows. Later inserts and updates write to
the base table and to every projection, so each projection adds write cost. Drop one
with `pgcolumnar.drop_projection('events', 'events_by_customer')`.

**Tuning.** Add a projection only for a hot access pattern the base sort order
serves poorly. Confirm the plan shows `Columnar Projection`. Keep the column list
minimal, because a projection that misses one queried column is never used. See
Projections in the administration guide for detail.

## Add indexes and use index-only scans

A columnar table supports B-tree and other index types. An index helps a highly
selective point lookup that no chunk-group skip can serve. An index-only scan
answers a query from the index alone when the chunk groups it reads are all
visible.

```sql
CREATE INDEX ON events (order_id);
VACUUM events;
EXPLAIN (ANALYZE) SELECT order_id FROM events WHERE order_id = 9001;
```

**Tuning.** Keep `VACUUM` current, because any write clears the all-visible mark
and forces a fetch from the row data. `pgcolumnar.enable_index_only_scan` and
`pgcolumnar.enable_index_fetch_penalty` shape when the planner prefers an index
over a scan. Prefer a sort key or a projection for a range query. Reserve an index
for a selective point lookup.

## Reclaim space after deletes and updates

A delete or update marks rows dead. Space returns when you compact.

```sql
SELECT pgcolumnar.compact('events');                    -- retire fully-dead groups
SELECT pgcolumnar.compact_rewrite('events', 0.3);       -- rewrite groups >= 30% dead
SELECT pgcolumnar.vacuum('events');                     -- combine small groups + reclaim
SELECT pgcolumnar.truncate('events');                   -- return end blocks to the OS
```

`compact` and `compact_rewrite` hold only `ShareUpdateExclusiveLock`, so they run
against a live table. `compact_rewrite` rewrites groups whose dead fraction is at
least `min_deleted_fraction` (default 0.2), and `max_groups` caps how many one
call rewrites (0 means no cap). `vacuum` rewrites the whole relation.

**Tuning.** Prefer `compact` and `compact_rewrite` for online reclaim. Reserve
`vacuum` for a full reorganization window. Cap `compact_rewrite` with `max_groups`
to bound each call and keep it incremental.

## Drop data past its retention

Declare how long rows are kept, then drop what has passed it.

Before you start: the column must be `timestamp` or `timestamptz`, and you must
own the table.

1. Declare the retention. Both halves are required.

```sql
SELECT pgcolumnar.set_options('events',
                              ttl_column => 'ts',
                              ttl_interval => '90 days');
```

2. Drop what has expired.

```sql
SELECT pgcolumnar.expire('events');
```

The function returns the number of row groups it dropped.

`expire` works on whole row groups. It drops a group only when every row in it
has passed the retention, so a group holding one live row is kept whole. Some
rows older than the interval therefore survive until the rest of their group
ages out. Nothing inside the retention is ever dropped.

It takes `ShareUpdateExclusiveLock`, so reads and writes continue. It never runs
on its own: call it by name, or schedule it. Calling it on a table with no
declared retention raises an error rather than doing nothing.

`pgcolumnar.reset_options` has no argument for the retention. To stop expiring,
set `ttl_interval` to a span no row will reach.

## Keep tables optimized automatically

A background daemon can compact and recluster tables on a schedule. The threshold
values below are examples, not the defaults.

```sql
-- postgresql.conf
pgcolumnar.autovacuum = on
pgcolumnar.autovacuum_naptime = '60s'
pgcolumnar.autovacuum_compact_threshold = 0.2
pgcolumnar.autovacuum_recluster_threshold = 0.1
```

**Tuning.** Lower the thresholds to keep tables tighter at the cost of more
background work. The daemon calls the online operations only, so it does not take
an exclusive lock. A table whose clustering is already intact reclusters as a
fast no-op, so the daemon does not churn storage.

## Read and write Parquet

Read a Parquet file directly, inspect its schema, or load it into a table.

```sql
SELECT * FROM pgcolumnar.read_parquet('/data/events.parquet')
  AS t(id bigint, ts timestamptz, region text);
SELECT * FROM pgcolumnar.parquet_schema('/data/events.parquet');
SELECT pgcolumnar.import_parquet('events', '/data/events.parquet');
```

Export a table to one file, or in parallel to a directory of files.

```sql
SELECT pgcolumnar.export_parquet('events', '/data/events.parquet');
SELECT pgcolumnar.parallel_export_parquet('events', '/data/events_out', 8);
```

Expose a Parquet file, directory, or Hive layout as a foreign table.

```sql
CREATE SERVER pq FOREIGN DATA WRAPPER pgcolumnar_parquet;
CREATE FOREIGN TABLE events_parquet (id bigint, ts timestamptz, region text)
  SERVER pq OPTIONS (path '/data/events', partition_columns 'region');
```

`parallel_export_parquet` writes one `part-NNNN.parquet` file per worker into the
directory, and `read_parquet` or the foreign table reads the directory back as one
relation.

**Tuning.** Project only the columns you need, because the reader reads only those
columns. A predicate on a `partition_columns` column removes whole files before
they open. `EXPLAIN (ANALYZE)` reports `Files Pruned` and the row groups read and
skipped.

**Permissions.** Reading needs the `pg_read_server_files` role, and
`parallel_export_parquet` needs `pg_write_server_files`.

## Import and export Arrow

The Arrow IPC file format works the same way as Parquet.

```sql
SELECT pgcolumnar.import_arrow('events', '/data/events.arrow');
SELECT pgcolumnar.export_arrow('events', '/data/events.arrow');
```

**Tuning.** Use Arrow to exchange data with an in-process analytic engine without
a Parquet encode step. Use Parquet for durable, compressed files.

## Use object storage

Every path that accepts a local path also accepts an `s3://`, `http://`, or
`https://` URL. Credentials come from the server process environment.

```sql
SELECT * FROM pgcolumnar.read_parquet('s3://bucket/events.parquet')
  AS t(id bigint, region text);
SELECT pgcolumnar.export_parquet('events', 's3://bucket/events.parquet');
```

Set the endpoint allow-list first, because it gates every remote scheme.

```sql
-- postgresql.conf (a superuser setting)
pgcolumnar.objstore_allowed_endpoints = 's3.amazonaws.com, minio.internal'
```

The environment supplies `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`AWS_SESSION_TOKEN`, `AWS_REGION`, and `AWS_ENDPOINT_URL`. A host that resolves to
a link-local or instance-metadata address is refused even when it is listed.

**Tuning.** Raise `pgcolumnar.objstore_part_size` to use larger multipart parts
over a high-latency network. Choose `pgcolumnar.objstore_s3_addressing` to match
your provider's path or virtual-host style. Keep the allow-list as small as your
buckets require.

## Query an Apache Iceberg table

Read an Iceberg table at its current snapshot from a metadata path.

```sql
SELECT region, sum(amount) FROM pgcolumnar.iceberg_scan(
  '/warehouse/db/events/metadata/00042.metadata.json')
  AS t(id bigint, region text, amount int)
  GROUP BY region;

SELECT * FROM pgcolumnar.iceberg_current_snapshot('.../00042.metadata.json');
SELECT * FROM pgcolumnar.iceberg_data_files('.../00042.metadata.json');
```

The reader resolves each output column to a schema field id, so a file written
before a column rename still reads. It applies position deletes, equality
deletes, and format-version 3 deletion vectors. The metadata path may be a local
path or an `s3://`, `http://`, or `https://` URL.

**Tuning.** Select only the columns you need. Read from object storage under the
same allow-list as every other remote access.

**Permissions.** The function requires the `pg_read_server_files` role.

## Query an Iceberg REST catalog

Name a table by catalog, namespace, and table instead of a metadata path.

```sql
SELECT * FROM pgcolumnar.iceberg_rest_scan(
  'https://catalog.example.com', 'analytics', 'events')
  AS t(id bigint, region text, amount int);

SELECT * FROM pgcolumnar.iceberg_rest_namespaces('https://catalog.example.com');
SELECT * FROM pgcolumnar.iceberg_rest_tables('https://catalog.example.com', 'analytics');
```

For per-role credentials, name a foreign server instead of a URI. The server
holds the catalog URI. The current role's user mapping holds the bearer token,
kept in `pg_user_mapping` where it is not world-readable. On a multi-warehouse
catalog, add a `warehouse` server option; it is sent on the config request.

```sql
CREATE SERVER cat FOREIGN DATA WRAPPER pgcolumnar_iceberg_catalog
  OPTIONS (catalog_uri 'https://catalog.example.com', warehouse 'analytics_wh');
CREATE USER MAPPING FOR analyst SERVER cat OPTIONS (token 's3cr3t');

SELECT * FROM pgcolumnar.iceberg_rest_scan('cat', 'analytics', 'events')
  AS t(id bigint, region text, amount int);
```

A user mapping may carry OAuth2 client credentials rather than a static token.
The catalog then mints a short-lived bearer.

```sql
CREATE USER MAPPING FOR analyst SERVER cat OPTIONS (
  oauth_client_id 'app', oauth_client_secret 's3cr3t', oauth_scope 'catalog');
```

**Tuning.** A catalog can vend short-lived storage credentials in its reply.
`iceberg_rest_scan` then reads the table files with those credentials, not the
server environment. The client secret travels in the request body, never a URL or
a log line.

## Prune Iceberg data files with the foreign-data wrapper

`iceberg_scan` receives no query predicate, so it cannot skip files. The
foreign-data wrapper gives Iceberg a predicate-bearing scan that prunes whole
files.

```sql
CREATE SERVER ice FOREIGN DATA WRAPPER pgcolumnar_iceberg;
CREATE FOREIGN TABLE events (id bigint, region text, amount int)
  SERVER ice OPTIONS (metadata_path '/warehouse/db/events/metadata/v3.metadata.json');

-- reads only the matching files; EXPLAIN ANALYZE shows "Files Pruned"
SELECT sum(amount) FROM events WHERE region = 'eu';
```

Partition pruning covers identity, `bucket[N]`, `truncate[W]`, and the temporal
transforms. It prunes `year`, `month`, `day`, and `hour` on a timestamp or
timestamptz column, and `year`, `month`, and `day` on a date column. Metrics
pruning covers integer and boolean columns by their stored minimum and maximum.
Pruning never changes the rows returned. A predicate the wrapper cannot decide
reads the file and returns the same rows.

**Tuning.** Filter on a partition column to remove whole files. Filter on an
integer or boolean column to prune by metrics. Confirm the effect with
`EXPLAIN (ANALYZE)`, which reports `Files Pruned`. A predicate on an unsupported
type still returns correct rows, only without the pruning.

## Tune concurrent writes

pgColumnar serializes two conflicting writes so neither is lost. A concurrent
insert of the same unique key, and a concurrent `UPDATE` or `DELETE` of the same
row, each take a transaction-scoped advisory lock. The losing writer gets a
retryable error, which the application repeats.

```conf
# postgresql.conf (both bucket counts are read at server start only)
pgcolumnar.row_lock_buckets = 4096
pgcolumnar.unique_lock_buckets = 512
```

**Tuning.** `pgcolumnar.enable_row_update_lock` and
`pgcolumnar.enable_unique_insert_lock` are on by default and should stay on for
correctness. Raise `row_lock_buckets` or `unique_lock_buckets` for a workload with
many concurrent single-row writes, so fewer unrelated rows share a bucket. Retry a
`serialization_failure` (SQLSTATE 40001) in the application. See Concurrency in the
limitations guide.

## Vectorize a GROUP BY aggregate

The vectorized aggregate path computes an ungrouped aggregate over decoded
vectors. An opt-in setting extends it to `GROUP BY`.

```sql
SET pgcolumnar.enable_group_vectorization = on;
EXPLAIN (ANALYZE) SELECT region, sum(amount) FROM events GROUP BY region;
```

**Tuning.** Turn it on for a `GROUP BY` with a bounded number of distinct groups.
The plan then shows `Columnar Vectorized Group Keys`. A query that exceeds
`pgcolumnar.groupagg_max_groups` (default 1000000) raises an error, so leave the
setting off for high-cardinality grouping.

## Count rows quickly

An unfiltered `count(*)` reads the per-chunk-group row counts from metadata and
decodes no column data.

```sql
EXPLAIN (ANALYZE) SELECT count(*) FROM events;
```

**Tuning.** A filter, or a table with deleted rows, forces a fold over the
surviving rows and decodes the filtered columns. Keep the filter on a sorted or
bloomed column so chunk-group skipping removes most groups first.

## Measure and introspect

Inspect physical layout, sort quality, and query plans.

```sql
SELECT * FROM pgcolumnar.stats('events');          -- per-row-group rows, dead rows, size
SELECT * FROM pgcolumnar.sort_status('events');    -- sorted vs appended groups
SELECT pgcolumnar.analyze('events');               -- refresh planner statistics
EXPLAIN (ANALYZE) SELECT sum(amount) FROM events WHERE region = 'eu';
```

`EXPLAIN (ANALYZE)` on a columnar scan reports `Columnar Pushed-Down Filters`,
`Columnar Usable Skip Predicates`, and `Columnar Chunk Groups Removed by Filter`.

**Tuning.** Read `Columnar Chunk Groups Removed by Filter` to confirm a filter
skips data. A low removal count on a selective filter means the sort key does not
match the query. Re-sort on the filtered column, then check the count again.

## Benchmark your own workload

Reference numbers live in the benchmarks guide. Measure your own tables the same
way.

```sql
\timing on
EXPLAIN (ANALYZE, BUFFERS) SELECT sum(amount) FROM events WHERE region = 'eu';
```

**Tuning.** Warm the cache with one run and time the next. Read the `Columnar`
counters in the plan to confirm the query took the path you measure, on both the
columnar table and any row-store baseline. Compare a columnar table against a heap
table of the same rows, not against a different query. Load enough rows to fill the
row groups, because a small table hides the skipping and decode effects.
