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

-- the same, on the Hilbert curve
SELECT pgcolumnar.cluster_hilbert('events', 'customer_id', 'ts');
```

`vacuum_sorted` sorts ascending and tightens the first column most. `cluster`
uses a Z-order curve, so filters on more than one of its columns all skip more
groups.

**Which curve.** `cluster_hilbert` lays the rows on the Hilbert curve instead.
That curve has no jumps at a bit boundary, so keys close in the data stay close
in storage. Range filters on the clustered columns then read fewer chunk groups.
Measured on 200,000 rows over two columns, Hilbert read 1.24x to 2.04x fewer
groups than Z-order. The advantage is largest on the most selective queries.
Take Z-order for point lookups and Hilbert for ranges, and measure your own
corpus if the two are close.

**The curve is sticky, on the same key.** The table records which curve it was laid
on, and `pgcolumnar.sort_status` reports it as `sorted_kind`. Once a table is on the
Hilbert curve, plain `cluster` and `recluster` **over the same key** maintain that
curve rather than converting it back.

**A different key reverts to Z-order**, and the column order is part of the key:

```sql
SELECT pgcolumnar.cluster_hilbert('h', 'a', 'b');   -- sorted_kind: hilbert
SELECT pgcolumnar.recluster('h', 'a', 'b');         -- sorted_kind: hilbert
SELECT pgcolumnar.recluster('h', 'b', 'a');         -- sorted_kind: zorder
```

That is what keeps "sticky" from meaning "unescapable". To move a Z-ordered table
onto Hilbert, or to keep Hilbert while changing the key, name the verb:
`pgcolumnar.recluster_hilbert`.

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


## Skip fact-table work under a star-schema join

A serial inner Hash Join can push the dimension keys into the fact-table scan.
The scan drops chunk groups outside the build-side key range.
It also rejects rows whose keys are absent from a Bloom filter of those keys.

```sql
EXPLAIN (ANALYZE) SELECT sum(amount) FROM fact JOIN dim ON fact.k = dim.k;
```

**Tuning.** It is on by default (`pgcolumnar.enable_join_runtime_filter`).
Group skip needs the fact table clustered on the join key.
When the keys sit in few chunk groups, the scan drops the rest.
When every group holds every key, group skip removes none.
The Bloom filter can still reject non-matching rows.
Cluster the fact table on the join key with `pgcolumnar.cluster` or `pgcolumnar.recluster`.
It applies only to a serial inner Hash Join whose outer path is a direct columnar scan.
A LEFT, SEMI, ANTI, or CROSS join is unchanged.
A covering projection is also unchanged.
Read `Runtime Filter Groups Removed` and `Runtime Filter Rows Rejected` to confirm the skip.

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

## Find the latest reading per device

"What is the latest reading for each device?" is the usual time-series question.
`max(ts)` over a whole table is answered from zone maps and reads almost no data.
Per device, choose one of two forms.

To answer it fast for many devices, add a btree on the device and the timestamp:

```sql
CREATE INDEX readings_device_ts ON readings (device, ts);
```

When you need only the latest timestamp, group by device. This reads the table
once:

```sql
SELECT device, max(ts) FROM readings GROUP BY device ORDER BY device;
```

When you need the whole latest row, look each device up through the index. The
`devices` table lists the devices, one row each:

```sql
SELECT d.device, x.ts
FROM devices d
CROSS JOIN LATERAL (
    SELECT ts FROM readings r
    WHERE r.device = d.device
    ORDER BY ts DESC LIMIT 1) x
ORDER BY d.device;
```

**Do not use the second form without the index.** It then reads the whole table
once for each device. On 10 million rows and 1,000 devices, the query read
1,013 blocks in 45 ms with the index. Without it, the same query read 8.7 million
blocks in 12.6 minutes.

`DISTINCT ON (device) ... ORDER BY device, ts DESC` returns the same rows, but it
sorts the whole table to do so. On the same 10 million rows it took 5.6 seconds.

`test/latest_reading.sh` runs the SQL in this section and checks what it states.

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

Before you start: the column must be `date`, `timestamp` or `timestamptz`, and
you must own the table.

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

It takes `ShareUpdateExclusiveLock`, so reads and writes continue. Calling it on
a table with no declared retention raises an error rather than doing nothing.

3. To expire on a schedule instead, let the maintenance daemon run `expire`.
   Both settings are needed; the second is off by default, so turning on the
   daemon alone never deletes rows.

```sql
ALTER SYSTEM SET pgcolumnar.autovacuum = on;
ALTER SYSTEM SET pgcolumnar.autovacuum_expire = on;
SELECT pg_reload_conf();
```

The daemon then runs `expire` on every table that declares a retention, once
per `pgcolumnar.autovacuum_naptime` (default 60 seconds), and logs
`pgcolumnar autovacuum: expire <table> retired <n> row groups` when it retires
any. A pass that retires nothing logs at `DEBUG1` only.

For a `date` or `timestamp` retention column, the cutoff depends on the time
zone. The daemon uses the `TimeZone` in effect for the database it connects to.
That is the database's own setting (`ALTER DATABASE ... SET TimeZone`) when it
has one. Otherwise it is the server's `TimeZone`, including a change made by
reloading the configuration. Either may differ from the time zone your sessions
use. For a
cutoff in your session's time zone, run `pgcolumnar.expire` by hand from that
session.

`pgcolumnar.reset_options` has no argument for the retention. To stop expiring,
set `ttl_interval` to a span no row will reach.

## Fill gaps in a time series

A bucketed query returns no row for a bucket with no data. These three recipes
return a row for every bucket, then fill the empty ones. They are plain SQL, so
they need nothing beyond pgColumnar.

The examples use a table `readings (ts timestamptz, v float8)` and one-hour
buckets over one day. To adapt one, change the table, the column names, the
range and the width. Keep the origin of `date_bin` equal to the start of the
series, so every bucket the aggregate makes is a bucket the series has.

`date_bin` with an explicit origin does not depend on the session's time zone.
`date_trunc('hour', ts)` does: in a zone with a half-hour offset, its buckets
start at half past the hour and match no bucket in the series.

### Fill empty buckets

Join the bucketed aggregate to a series of every bucket. An empty bucket
returns `NULL`.

```sql
SELECT b.bucket, a.avg_v
  FROM generate_series(timestamptz '2026-10-01 00:00+00',
                       timestamptz '2026-10-01 23:00+00',
                       interval '1 hour') AS b(bucket)
  LEFT JOIN (SELECT date_bin(interval '1 hour', ts, timestamptz '2026-10-01 00:00+00') AS bucket,
                    avg(v) AS avg_v
               FROM readings
              GROUP BY 1) AS a USING (bucket)
 ORDER BY b.bucket;
```

With `pgcolumnar.enable_group_vectorization` on, the aggregate under the join
runs as the vectorized grouped node.

### Carry the last value forward

Last observation carried forward (LOCF) fills an empty bucket with the value of
the latest bucket before it that has one. A running count of non-null values
puts each empty bucket in the same group as the last known value, and
`first_value` reads that value.

```sql
WITH filled AS (
SELECT b.bucket, a.avg_v
  FROM generate_series(timestamptz '2026-10-01 00:00+00',
                       timestamptz '2026-10-01 23:00+00',
                       interval '1 hour') AS b(bucket)
  LEFT JOIN (SELECT date_bin(interval '1 hour', ts, timestamptz '2026-10-01 00:00+00') AS bucket,
                    avg(v) AS avg_v
               FROM readings
              GROUP BY 1) AS a USING (bucket)
), grouped AS (
  SELECT bucket, avg_v, count(avg_v) OVER (ORDER BY bucket) AS grp
    FROM filled
)
SELECT bucket, avg_v,
       first_value(avg_v) OVER (PARTITION BY grp ORDER BY bucket) AS avg_v_locf
  FROM grouped
 ORDER BY bucket;
```

A bucket before the first value stays `NULL`, because there is nothing to
carry. A bucket whose rows all have `v` null has an average of `NULL`, so it
counts as empty and is filled.

### Interpolate between known values

Linear interpolation fills an empty bucket on the straight line between the
known buckets on either side. A running count in each direction finds both
neighbours.

```sql
WITH filled AS (
SELECT b.bucket, a.avg_v
  FROM generate_series(timestamptz '2026-10-01 00:00+00',
                       timestamptz '2026-10-01 23:00+00',
                       interval '1 hour') AS b(bucket)
  LEFT JOIN (SELECT date_bin(interval '1 hour', ts, timestamptz '2026-10-01 00:00+00') AS bucket,
                    avg(v) AS avg_v
               FROM readings
              GROUP BY 1) AS a USING (bucket)
), marks AS (
  SELECT bucket, avg_v,
         count(avg_v) OVER (ORDER BY bucket) AS g_prev,
         count(avg_v) OVER (ORDER BY bucket DESC) AS g_next
    FROM filled
), ends AS (
  SELECT bucket, avg_v,
         first_value(bucket) OVER (PARTITION BY g_prev ORDER BY bucket) AS prev_t,
         first_value(avg_v)  OVER (PARTITION BY g_prev ORDER BY bucket) AS prev_v,
         first_value(bucket) OVER (PARTITION BY g_next ORDER BY bucket DESC) AS next_t,
         first_value(avg_v)  OVER (PARTITION BY g_next ORDER BY bucket DESC) AS next_v
    FROM marks
)
SELECT bucket,
       CASE WHEN avg_v IS NOT NULL THEN avg_v
            WHEN prev_v IS NULL OR next_v IS NULL THEN NULL
            ELSE prev_v + (next_v - prev_v)
                 * extract(epoch FROM bucket - prev_t)
                 / extract(epoch FROM next_t - prev_t)
       END AS avg_v_interp
  FROM ends
 ORDER BY bucket;
```

A bucket before the first value or after the last one stays `NULL`, because it
has a neighbour on one side only.

## Create time partitions ahead

`pgcolumnar.partition_create_ahead` keeps a time-partitioned table ready for
the rows that are coming. Call it from a scheduler, such as pg_cron, more often
than the width:

```sql
CREATE TABLE metrics (ts timestamptz NOT NULL, device int, value float8)
    PARTITION BY RANGE (ts);

-- Daily partitions, up to three days ahead of now.
SELECT pgcolumnar.partition_create_ahead('metrics', '1 day', 3);
```

Every partition that it creates is columnar. It continues from the highest
existing upper bound, so the partitions stay contiguous, and a second call
creates nothing.

It takes `SHARE UPDATE EXCLUSIVE` on the parent, not `ACCESS EXCLUSIVE`, so
queries and inserts on the table continue while it runs.

For a `timestamp` key, which has no time zone, "now" is the session's local
time, and partitions start on the width's boundary in that time.

The function refuses a table that has a DEFAULT partition, or a partition
bounded by `MAXVALUE`. It also refuses a call that would create more than 1000
partitions, before it creates any. Use a wider width or a shorter lead.

`test/partition_lifecycle.sh` checks each of these statements, in sessions
west and east of UTC.

## Drop old time partitions

`pgcolumnar.partition_drop_plan` returns the statements that retire every
partition whose whole range is older than a cutoff. It does not run them, because
`DETACH PARTITION ... CONCURRENTLY` cannot run inside a function. Run each one as
its own statement, for example with psql's `\gexec`:

```sql
\set ON_ERROR_STOP on
SELECT * FROM pgcolumnar.partition_drop_plan('metrics', now() - interval '90 days') \gexec
```

For each old partition, in range order, the plan detaches it concurrently and
then drops it. The concurrent detach takes no exclusive lock on the parent, so
queries and inserts on the table continue. The drop locks only the detached
partition, which no query reaches through the parent any more. A partition whose
upper bound equals the cutoff is old. A partition that straddles the cutoff is
kept.

If a concurrent detach was interrupted, the partition stays pending. The next
plan finishes that detach with `DETACH PARTITION ... FINALIZE` instead of starting
it again.

The drop refuses while its partition is still attached (`55000`). So if a detach
fails, for example on a lock timeout, the drop after it does not lock the parent
or remove the partition. A later plan retries it. `ON_ERROR_STOP` stops the run
at the first failure, which also keeps the plan's other statements from running.

The cutoff must have the key's type. A `timestamptz` key takes a `timestamptz`
cutoff, as in the example. A `timestamp` key takes a `timestamp` cutoff, such
as `localtimestamp - interval '90 days'`. A cutoff of the other type is refused
(`42804`). Comparing across the two types depends on the session's time zone,
so the same call could plan differently from two sessions.

A partition that is itself partitioned is planned as one, and the `DROP` takes
its children with it. A partition that `tier_partition` moved to Parquet is
planned with `DROP FOREIGN TABLE`. That removes the foreign table, and the
Parquet file stays where it is: delete it yourself when you no longer need it.

The function refuses the same tables as `partition_create_ahead`, because a
DEFAULT partition rules out a concurrent detach. A run of the plan drops the
partitions' rows: nothing keeps them.

## Count distinct values and percentiles approximately

For large tables, approximate distinct counts and percentiles cost far less than
exact ones. Two PostgreSQL extensions provide them, and both work on columnar
tables without anything from pgColumnar: `hll` (HyperLogLog) for distinct counts
and `tdigest` for percentiles.

Before you start: install the extensions for your PostgreSQL major. PGDG ships
them as `postgresql-N-hll` and `postgresql-N-tdigest`. Tested with hll 2.19 on
PostgreSQL 15 to 18 and tdigest 1.4.3 on PostgreSQL 15 to 19. hll 2.19 does not
build against PostgreSQL 19.

```sql
CREATE EXTENSION hll;
CREATE EXTENSION tdigest;

SELECT hll_cardinality(hll_add_agg(hll_hash_integer(device_id))) FROM readings;
SELECT tdigest_percentile(v, 100, 0.99) FROM readings;
```

Over a columnar table, each gives exactly what it gives over the same rows in a
heap table. Both read through the columnar scan, and read only the column they
aggregate. Their error against the exact answer is the extension's own. For `hll`
at its default precision, one standard error is about 2.3%. For `tdigest` at
compression 100, the 99th percentile is well within 0.1%.

Both sketch types can be stored in a columnar table, for example one sketch per
day. A stored `hll` comes back byte for byte, so `hll_union_agg` over the daily
sketches counts what one sketch over all the rows counts.

## Manage time partitions with pg_partman

pg_partman can create and drop columnar partitions. It needs one setting to do
so, and without it the partitions it makes are not columnar.

Before you start: install pg_partman (PGDG ships it as `postgresql-N-partman`),
and create it in its own schema.

```sql
CREATE SCHEMA partman;
CREATE EXTENSION pg_partman SCHEMA partman;
```

1. Make columnar the default access method where pg_partman runs. A database
   setting is the simplest place.

```sql
ALTER DATABASE mydb SET default_table_access_method = 'pgcolumnar';
```

2. Create the parent and hand it to pg_partman.

```sql
CREATE TABLE metrics (ts timestamptz NOT NULL, v float8)
    PARTITION BY RANGE (ts);
SELECT partman.create_parent('public.metrics', 'ts', '1 day', p_premake := 4);
```

3. Set the retention, and run maintenance on a schedule.

```sql
UPDATE partman.part_config
   SET retention = '90 days', retention_keep_table = false
 WHERE parent_table = 'public.metrics';
SELECT partman.run_maintenance('public.metrics');
```

**Without step 1, every partition is a heap table.** pg_partman does not take the
access method from the parent. That holds even on PostgreSQL 17 and later, where
the parent itself can carry `USING pgcolumnar`. Changing the access method of
pg_partman's template table does not help either. Measured with pg_partman 5.4.1
on PostgreSQL 15 to 19.

**The setting has a cost.** Every table created in that database becomes
columnar unless it names another access method.

**Maintenance locks the parent.** `run_maintenance` holds `ACCESS EXCLUSIVE` on
the parent table while it runs, so queries on the table wait for it. pg_partman
also creates a `DEFAULT` partition.

When a partition is dropped by retention, its pgcolumnar catalog rows go with it.

`test/partman_columnar.sh` checks each of these statements.

## Keep a rollup up to date

A rollup keeps an aggregate per time bucket, such as hourly averages. A refresh
folds in the rows added since the last refresh, and recomputes only the buckets
whose rows were deleted or updated. Queries read the rollup instead of the raw
table.

```sql
SELECT pgcolumnar.create_rollup(
    'metrics_hourly', 'metrics', 'ts', '1 hour', '2026-01-01 00:00:00+00',
    group_by      => '{device}',
    agg_functions => '{count,sum,min,max,avg}',
    agg_columns   => '{*,value,value,value,value}');

SELECT pgcolumnar.refresh_rollup('metrics_hourly');   -- returns rows read
SELECT * FROM metrics_hourly WHERE bucket >= now() - interval '1 day';
```

The rollup is a view with a `bucket` column, the `group_by` columns, and one
column for each aggregate. The aggregate columns are named `count`, or the
function and the column, as in `sum_value` and `avg_value`. The buckets come
from `date_bin(width, ts, origin)`, so every session gets the same buckets,
whatever its time zone. The width cannot include months.

A refresh reads only the row groups added since the last refresh. Rows that
arrive late, with old timestamps, are still folded into their buckets. Rows
that commit while a refresh runs are folded by the next refresh. Two refreshes
that run at the same time fold each row once. A refresh after a small append
reads a small part of the table.

The rollup equals what the same `GROUP BY` over the whole table returns. Count,
sum, min and max are exact, and so is the average of an integer column. The
average of a `float8` column can differ from a full recompute in the last digits.

**Retention keeps the rollup's history.** When `pgcolumnar.expire`, or the
maintenance daemon, drops old rows of the source, the rollup keeps its rows for
those buckets. So, for example, hourly averages can outlive the raw data.

**Deletes and updates are recomputed.** Rows can be deleted or updated, or
`pgcolumnar.compact_rewrite` can rewrite a row group. The next refresh then
recomputes, from the source, each bucket that the changed row groups covered.
It leaves the other buckets as they are.

**A refresh never recomputes history.** A bucket can hold rows that retention
dropped. If such a bucket needs a recompute, `refresh_rollup` refuses (`0A000`)
and changes nothing, because a recompute would lose those rows. A delete that reaches a row group
spanning such a bucket is refused too, even when the deleted rows are recent.
If the rollup is right as it stands, run `pgcolumnar.rebase_rollup` to accept
the source as it is now.

If the source is rewritten, for example by `pgcolumnar.vacuum`, `refresh_rollup`
refuses with `55000`. If the rollup was current before the rewrite, run
`pgcolumnar.rebase_rollup` to record the rewritten table and continue.

**A rollup depends on its source.** `DROP TABLE` of the source is refused while
a rollup reads from it, as it is for a view. `DROP TABLE ... CASCADE` drops the
rollup too. To remove only the rollup, run `DROP VIEW` on it, which also drops
its partials table and its catalog rows.

Run `refresh_rollup` at READ COMMITTED. A role that is not a superuser needs
`USAGE` on schema `pgcolumnar`, which a superuser grants. Each role then sees
and refreshes only the rollups that it owns.

`test/rollup.sh` checks each of these statements.

## Move cold partitions to Parquet

`pgcolumnar.tier_partition` moves one columnar partition into a Parquet file.
It then attaches the file as a foreign partition with the same name, bounds,
grants and comment. Queries on the parent return the same rows before and after
the move.

```sql
CREATE SERVER cold FOREIGN DATA WRAPPER pgcolumnar_parquet;

SELECT pgcolumnar.tier_partition('metrics_2026_01', 'cold',
                                 's3://archive/metrics/2026_01.parquet');
```

The function returns the number of rows that it moved. It runs as one
transaction: if it fails at any step, the rows stay in the columnar partition,
exactly once.

**Run it at READ COMMITTED.** The function refuses `REPEATABLE READ` and
`SERIALIZABLE` (`25000`). An older snapshot does not see rows that other
sessions committed just before the move. Those rows would be dropped with the
partition, not exported.

**It locks the parent briefly.** The export runs under a `SHARE` lock on the
partition, so writes to that partition wait and reads continue. Then the
function holds `ACCESS EXCLUSIVE` on the parent until it commits. That lock
covers the catalog changes only, not the export.

**Every lock waits at most `lock_wait`.** The default is 10 seconds. A long
query on the parent would otherwise make every new query wait behind the move.
When the wait runs out, the move fails with `55P03` and the rows stay where they
were:

```sql
SELECT pgcolumnar.tier_partition('metrics_2026_01', 'cold',
                                 's3://archive/metrics/2026_01.parquet',
                                 lock_wait => '2 seconds');
```

An insert into the partition's range that runs at the same moment can deadlock
with the move. PostgreSQL then cancels one of the two (`40P01`). Either way,
every row is stored exactly once.

**A failed attempt leaves its file behind.** A transaction cannot remove a file
that it wrote. You can retry with the same `path`: the function overwrites a
file that no foreign table in the database reads. It refuses a `path` that a
foreign table in this database reads (`58P02`). The check compares the text of
the path, so the same file spelled another way, for example through a symbolic
link, is not recognised. It also cannot see readers outside the database, so do
not reuse a path that another system reads.

The function refuses these partitions before it moves anything:

- A `DEFAULT` partition, a partition that is not columnar, or a table that is
  not a partition.
- A partition whose parent has a `UNIQUE` index or a primary key. A foreign
  table cannot be attached under one.
- A partition that a view depends on (`2BP01`).
- A partition that has a row-level policy, a trigger of its own, or a foreign
  key of its own (`2BP01`). Dropping the partition would remove these objects.

After the move:

- An insert into the moved range fails, because a Parquet foreign table is
  read-only.
- Partition pruning works as before. A query that touches only the local
  partitions does not read the Parquet file.

`test/tier_partition.sh` checks each of these statements. It uses a local path,
not object storage.

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

`export_arrow` writes the Arrow IPC stream format. `import_arrow` reads the
stream format and the Arrow IPC file format, which includes Feather v2.

```sql
SELECT pgcolumnar.export_arrow('events', '/data/events.arrows');
SELECT pgcolumnar.import_arrow('events', '/data/events.arrows');
SELECT pgcolumnar.import_arrow('events', '/data/events.arrow');  -- IPC file or Feather v2
```

To read an export with pyarrow, use `pyarrow.ipc.open_stream`. The export has no
file footer, so `pyarrow.ipc.open_file` and Feather readers refuse it.

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
over a high-latency network. Its maximum is 512 MiB. Choose `pgcolumnar.objstore_s3_addressing` to match
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
kept in `pg_user_mapping` where it is not world-readable. The role needs `USAGE`
on the server. On a multi-warehouse catalog, add a `warehouse` server option; it
is sent on the config request.

```sql
CREATE SERVER cat FOREIGN DATA WRAPPER pgcolumnar_iceberg_catalog
  OPTIONS (catalog_uri 'https://catalog.example.com', warehouse 'analytics_wh');
GRANT USAGE ON FOREIGN SERVER cat TO analyst;
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
vectors. It also covers `GROUP BY`, on by default, when the planner estimates a
bounded number of groups.

```sql
EXPLAIN (ANALYZE) SELECT region, sum(amount) FROM events GROUP BY region;
```

**Tuning.** The plan shows `Columnar Vectorized Group Keys` when the path is
taken. The planner takes it only when the group count cannot exceed
`pgcolumnar.groupagg_max_groups` (default 1000000). For a plain key the bound is
the table's row count, so a table of more rows than the cap gets the ordinary
plan. For time-bucket keys the bound is the number of buckets. Raise the cap to
keep larger tables on the fast path.

## Count rows quickly

An unfiltered `count(*)` reads the per-chunk-group row counts from metadata and
decodes no column data.

```sql
EXPLAIN (ANALYZE) SELECT count(*) FROM events;
```

**Tuning.** A filter, or a table with deleted rows, forces a fold over the
surviving rows and decodes the filtered columns. Keep the filter on a sorted or
bloomed column so chunk-group skipping removes most groups first.

## Fold an aggregate over a unique-key join

An ungrouped aggregate over a unique-key inner Hash Join can keep the
vectorized fold.

```sql
SET pgcolumnar.enable_ungrouped_vector_agg = on;
EXPLAIN (COSTS OFF)
SELECT count(*), sum(fact.amount)
FROM fact JOIN dim ON fact.k = dim.k;
```

**Tuning.** The dimension needs a unique constraint on the join key.
A duplicate-key dimension keeps the core Agg plan.
Grouped aggregation over a join is not this path.

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
