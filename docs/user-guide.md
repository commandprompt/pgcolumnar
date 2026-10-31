# User guide

This guide tells you how to create columnar tables, how to load data, and how to
query the tables. It assumes that you installed the extension and that the server
loads it. Refer to [Installation](installation.md).

## Create a columnar table

Add `USING pgcolumnar` to `CREATE TABLE`:

```sql
CREATE TABLE events (
    id          bigint,
    customer_id int,
    amount      numeric,
    kind        text,
    ts          timestamptz
) USING pgcolumnar;
```

The table behaves like an ordinary PostgreSQL table for most SQL. It supports
transactions, constraints, indexes, `COPY`, and `pg_dump`. See
[Limitations](limitations.md) for the exceptions.

## Convert an existing table

On PostgreSQL 15 and later:

```sql
ALTER TABLE events SET ACCESS METHOD pgcolumnar;
```

On any supported major, and on 13 and 14 which build but are outside the tested
matrix, the extension provides a helper:

```sql
SELECT pgcolumnar.alter_table_set_access_method('events', 'pgcolumnar');
```

On PostgreSQL 13 and 14 the helper rebuilds the table and does not preserve its
OID or dependent objects such as views and foreign keys. See the
[SQL reference](sql-reference.md#pgcolumnaralter_table_set_access_methodt-text-method-text).

To convert back to the default heap, use `heap` as the method.

## Load data

Columnar storage is for data that you mostly append. The writer holds rows in a
buffer, then writes them in row groups. A row group contains a maximum of
`pgcolumnar.stripe_row_limit` rows. Each transaction writes its own row groups.
Thus the shape of a load changes the result:

- Prefer `COPY` or multi-row `INSERT` over many single-row `INSERT` statements. A
  `COPY` of N rows writes about N divided by `stripe_row_limit` row groups.
- Many small transactions make many small row groups. If a load used that method,
  run [`pgcolumnar.vacuum`](sql-reference.md#pgcolumnarvacuumtablename-regclass-stripe_count-int-default-0)
  to combine the row groups.

```sql
COPY events FROM '/data/events.csv' WITH (FORMAT csv, HEADER);

INSERT INTO events
SELECT g, g % 1000, (random() * 100)::numeric(10,2), 'sale',
       now() - (g || ' seconds')::interval
FROM generate_series(1, 1000000) g;
```

### Parallel bulk load

A single `COPY` uses one core, and the columnar encode step is the largest part
of a load. To use more cores, load a text file with
[`pgcolumnar.parallel_copy`](sql-reference.md#pgcolumnarparallel_copytarget-regclass-filename-text-workers-int-default-null-returns-bigint).
It fans the file across several background workers and returns the row count. The
load is atomic, so a failure in any part rolls the whole load back. It commits on
its own, so a `ROLLBACK` in the caller does not undo it.

```sql
CREATE TABLE events (id bigint, ts timestamptz, val double precision) USING pgcolumnar;
SELECT pgcolumnar.parallel_copy('events', '/data/events.txt', 8);
```

The target is either a single columnar table or a RANGE-partitioned table with
columnar partitions. A single table needs no row order. A partitioned target
needs the file sorted ascending by the partition key, and the key type must be
numeric or a date/time type. Set `max_prepared_transactions` above the worker
count first, because the load prepares one transaction per worker. On a machine
with many cores this loads several times faster than one `COPY`. See
[Benchmarks](benchmarks.md#parallel-bulk-ingest) for measured numbers.

### Parallel Parquet export

To write a large columnar table out fast, use
[`pgcolumnar.parallel_export_parquet`](sql-reference.md#pgcolumnarparallel_export_parquettarget-regclass-path-text-workers-int-default-null-returns-bigint).
It fans the export across several read-only background workers, each writing one
file into a directory, and returns the row count. Read the directory back with
`pgcolumnar.read_parquet`.

```sql
SELECT pgcolumnar.parallel_export_parquet('events', '/data/events_out', 8);
SELECT count(*) FROM pgcolumnar.read_parquet('/data/events_out')
  AS t(id bigint, ts timestamptz, val double precision);
```

The output directory must be empty, and is created if it does not exist. The caller needs the
`pg_write_server_files` role. See [Benchmarks](benchmarks.md#import-and-export)
for measured numbers.

## Query

Queries need no special syntax. The planner adds columnar scan and aggregate
paths for columnar tables. Reads that touch a subset of columns and scan many
rows benefit most.

```sql
-- reads only amount and ts, skips chunk groups outside the time range
SELECT date_trunc('day', ts) AS day, sum(amount)
FROM events
WHERE ts >= '2026-01-01' AND ts < '2026-02-01'
GROUP BY 1
ORDER BY 1;
```

### How the scan is chosen

Read the plan with `EXPLAIN`:

```sql
EXPLAIN (ANALYZE, VERBOSE) SELECT sum(amount) FROM events WHERE customer_id = 42;
```

A columnar scan shows as a custom scan node. The scan uses the following, each
controlled by a setting in the [Configuration reference](configuration.md):

- Chunk-group skipping: per-chunk minimum and maximum values drop groups of rows
  that cannot satisfy a filter.
- Bloom filters: per-chunk filters drop groups for equality filters.
- Vectorized aggregate. The zone-map metadata answers an ungrouped count, sum,
  avg, min, or max on a supported type.
- `count(*)` answered from catalog metadata when there is no filter.
- Join runtime filter: a serial inner Hash Join can skip fact-table groups and reject non-matching rows using the build-side keys.
  Cluster the fact table on the join key. Group skip is a no-op when every group holds every key.
- Vectorized aggregate over a unique-key inner Hash Join when the ungrouped fold is on.

#### Reading the filter counters

`EXPLAIN (ANALYZE)` reports two counters for filters. They answer different
questions and are read together:

- `Columnar Pushed-Down Filters` is how many filters the scan was given. It
  follows the `pgcolumnar.enable_qual_pushdown` setting.
- `Columnar Usable Skip Predicates` is how many of those the scan can skip chunk
  groups with. A filter is not usable when the two types being compared have no
  ordering function for that pair. The scan then applies the filter to each row
  and returns the same result, after it reads every group.

```
Columnar Pushed-Down Filters: 1
Columnar Usable Skip Predicates: 0
Columnar Chunk Groups Removed by Filter: 0
```

That plan read the whole table. The `1` and the `0` mean that the filter reached
the scan and the scan could not use it. That is a different situation from a
usable filter that matches most rows.

When the two numbers are equal and no groups are removed, the filter is usable.
The values are then spread across every group, so the scan can skip none of them.
Cluster the table on that column with `pgcolumnar.cluster()` to change that
result. See the [SQL reference](sql-reference.md).

`Columnar Usable Skip Predicates` requires `ANALYZE`, because it reports what the
scan built at execution. A plain `EXPLAIN` shows only the first counter.

### Point lookups and indexes

Create indexes on columnar tables as usual:

```sql
CREATE INDEX ON events (id);
SELECT * FROM events WHERE id = 12345;
```

An index supports point lookups and range scans. An index-only scan reads the
index and not the table. pgColumnar can use one when two conditions are true.
First, the index contains all the columns of the query. Second, the
visibility-map fork marks the rows of the table as all-visible. `VACUUM` sets the
visibility bits. Refer to
[Administration](administration.md#index-only-scans).

## Arrays and composite types

Columnar tables store one-dimensional arrays and composite types. These are also
covered by the Arrow and Parquet import and export functions.

```sql
CREATE TYPE addr AS (city text, zip text);

CREATE TABLE people (
    id    int,
    tags  text[],
    home  addr
) USING pgcolumnar;

INSERT INTO people VALUES (1, ARRAY['a','b'], ROW('Portland','97201')::addr);
```

## Read Parquet files in place

You can query a Parquet file without importing it, either as a set-returning
function or as a foreign table. Reading a local server file requires the
`pg_read_server_files` role, which superusers hold.

```sql
-- inspect the file's columns and their inferred types
SELECT * FROM pgcolumnar.parquet_schema('/data/events.parquet');

-- read the rows directly
SELECT count(*), max(ts)
FROM pgcolumnar.read_parquet('/data/events.parquet')
  AS t(id int, ts timestamp, amount numeric(12,2));
```

For repeated queries, a foreign table is more convenient, and its scans skip row
groups and columns the query does not need:

```sql
CREATE SERVER pq FOREIGN DATA WRAPPER pgcolumnar_parquet;
CREATE FOREIGN TABLE events_parquet (id int, ts timestamp, amount numeric(12,2))
  SERVER pq OPTIONS (path '/data/events/');   -- a directory of *.parquet files

SELECT sum(amount) FROM events_parquet WHERE ts >= '2026-01-01';
```

A `path` may name one file, a directory (all `*.parquet` files below it, at any
depth), or a
glob pattern. `EXPLAIN (ANALYZE)` shows how much the scan skipped:

```sql
EXPLAIN (ANALYZE, COSTS OFF) SELECT id FROM events_parquet WHERE ts >= '2026-01-01';
--   Foreign Scan on events_parquet
--     Row Groups: 12
--     Row Groups Skipped: 9
--     Columns Read: 2
--     Columns Total: 3
--     Files: 4
```

**Timestamps and time zones.** A Parquet timestamp column holds either instants
(UTC-adjusted) or wall times with no zone. Read into the matching type
(`timestamptz` for instants, `timestamp` for wall times), a value comes back
unchanged. Read into the other type, it is converted as PostgreSQL's own cast
converts it, in the session's `TimeZone`:

- a wall time read into `timestamptz` is taken as local time in that zone, like
  `'2024-06-01 12:00'::timestamp::timestamptz`;
- an instant read into `timestamp` becomes the local wall time, like
  `'2024-06-01 12:00+00'::timestamptz::timestamp`.

A foreign-table scan does not skip row groups of a column read this way, because
the conversion does not keep order across a daylight-saving change. For the same
reason the Iceberg foreign table does not prune files on a `year()`, `month()`,
`day()` or `hour()` partition of such a column. A column marked only with the
older `TIMESTAMP_MILLIS` or `TIMESTAMP_MICROS` type, with no `isAdjustedToUTC`
flag, holds instants: the Parquet format defines it so. `parquet_schema` advises
`timestamptz` for every column that holds instants, and `timestamp` for wall
times.

The `path` may also be an object-storage URL. Every read and export function, and
both foreign-data wrappers, accept an `s3://`, `http://`, or `https://` URL where
they accept a local path.

```sql
SELECT count(*) FROM pgcolumnar.read_parquet('s3://bucket/events.parquet')
  AS t(id int, ts timestamp);
```

Credentials come from the server process environment (`AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_REGION`, `AWS_ENDPOINT_URL`).
An administrator must first list the endpoint in the superuser-only
`pgcolumnar.objstore_allowed_endpoints`, which is empty by default and refuses
every host until then. See [Administration](administration.md#object-storage).

## Read an Apache Iceberg table

Read an Iceberg table at its current snapshot. Give a metadata path and a column
definition list. The reader applies the table's delete files and resolves each
column by schema field id.

```sql
SELECT region, sum(amount) FROM pgcolumnar.iceberg_scan(
  '/warehouse/db/events/metadata/00042.metadata.json')
  AS t(id bigint, region text, amount int)
  GROUP BY region;
```

Name a table by a REST catalog instead of a metadata path. The catalog URI form
takes its bearer token from the `PGCOLUMNAR_ICEBERG_REST_TOKEN` server
environment variable.

```sql
SELECT * FROM pgcolumnar.iceberg_rest_scan(
  'https://catalog.example.com', 'analytics', 'events')
  AS t(id bigint, region text, amount int);
```

For per-role credentials, pass a foreign server name as the first argument. The
server holds the catalog URI, and the current role's user mapping supplies the
token.

```sql
CREATE SERVER cat FOREIGN DATA WRAPPER pgcolumnar_iceberg_catalog
  OPTIONS (catalog_uri 'https://catalog.example.com');
CREATE USER MAPPING FOR analyst SERVER cat OPTIONS (token 's3cr3t');

SELECT * FROM pgcolumnar.iceberg_rest_scan('cat', 'analytics', 'events')
  AS t(id bigint, region text, amount int);
```

For file pruning, use the `pgcolumnar_iceberg` foreign-data wrapper. It receives
the query predicate, which `iceberg_scan` does not, so it removes whole data
files before it opens them.

```sql
CREATE SERVER ice FOREIGN DATA WRAPPER pgcolumnar_iceberg;
CREATE FOREIGN TABLE events_iceberg (id bigint, region text, amount int)
  SERVER ice OPTIONS (metadata_path '/warehouse/db/events/metadata/v3.metadata.json');

-- EXPLAIN (ANALYZE) reports "Files Pruned"
SELECT sum(amount) FROM events_iceberg WHERE region = 'eu';
```

The wrapper prunes by partition (identity, `bucket[N]`, `truncate[W]`, and the
year, month, day, and hour transforms) and by integer and boolean metrics. See
the [SQL reference](sql-reference.md) and the [how-to guides](how-to.md#query-an-apache-iceberg-table).

## Capture changes for replication or CDC

Logical decoding does not carry changes to a columnar table. Columnar data
reaches WAL as full-page images, which carry no tuple structure for a decoder to
read, so a replication slot never sees the rows. See
[limitations](limitations.md#replication-and-backup).

A slot is not quiet about the table, though. The metadata catalog is made of
ordinary heap tables, so a slot subscribed to everything delivers the internal
bookkeeping and none of the data:

```
table pgcolumnar.row_group:    INSERT: storage_id[bigint]:10000000000 group_number[bigint]:1 ...
table pgcolumnar.column_chunk: INSERT: ... encoding_descriptor[bytea]:'\x030100...'
table pgcolumnar.zone_map:     INSERT: ... minimum[bytea]:'\x0100000000000000' ...
table pgcolumnar.delete_vector: UPDATE: ... bitmap[bytea]:'\x03' deleted_count[integer]:2
```

Exclude the `pgcolumnar` schema from any publication.

To capture the changes themselves, write them to a heap table with a row trigger
and decode that table. Row triggers fire on columnar tables and see the same
rows a heap table would.

```sql
CREATE TABLE events (id bigint primary key, kind text, amount int)
    USING pgcolumnar;

CREATE TABLE events_cdc (
    seq    bigserial primary key,
    op     text,
    id     bigint,
    kind   text,
    amount int
);

CREATE FUNCTION events_capture() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        INSERT INTO events_cdc (op, id, kind, amount)
            VALUES ('DELETE', OLD.id, OLD.kind, OLD.amount);
    ELSE
        INSERT INTO events_cdc (op, id, kind, amount)
            VALUES (TG_OP, NEW.id, NEW.kind, NEW.amount);
    END IF;
    RETURN NULL;
END
$$;

CREATE TRIGGER events_cdc_t AFTER INSERT OR UPDATE OR DELETE ON events
    FOR EACH ROW EXECUTE FUNCTION events_capture();
```

The capture table is a heap table, so a slot carries it:

```
table public.events_cdc: INSERT: seq[bigint]:1 op[text]:'INSERT' id[bigint]:1 kind[text]:'sale'   amount[integer]:100
table public.events_cdc: INSERT: seq[bigint]:3 op[text]:'UPDATE' id[bigint]:1 kind[text]:'sale'   amount[integer]:150
table public.events_cdc: INSERT: seq[bigint]:4 op[text]:'DELETE' id[bigint]:2 kind[text]:'refund' amount[integer]:50
```

Four properties of this arrangement are worth knowing.

An `UPDATE` arrives as one `UPDATE`. Internally, a columnar update deletes the
old row and inserts a new row. The trigger operates one time for each row event.
Thus the capture table records the operation of the statement. It does not record
the operation of the storage.

The capture is transactional. The trigger operates in the transaction of the
statement. Thus a transaction that rolls back captures no rows. The captured rows
commit together with the data.

The capture writes one heap row for each changed row. This has a cost in write
time and in space. Examine bulk loads before you enable the capture. A load of
ten million rows also writes ten million heap rows.

The capture table needs pruning. Nothing deletes from it. Delete rows once the
consumer has confirmed them, and remember that the deletes are themselves
decoded unless the consumer filters them.

## Next steps

- Operate tables in production: [Administration](administration.md).
- Improve range scans on a scattered key or serve a query from a column subset:
  [projections](administration.md#projections) and
  [`pgcolumnar.vacuum_sorted`](sql-reference.md#pgcolumnarvacuum_sortedtablename-regclass-variadic-sort_columns-name).
- Move data in and out of the Arrow and Parquet ecosystem:
  [import and export](sql-reference.md#import-and-export).
- Reclaim space and re-sort a live table without an exclusive lock:
  [`compact`, `compact_rewrite`, and `recluster`](administration.md#online-maintenance-and-disk-reclaim),
  or schedule them with the [`pgcolumnar.autovacuum` daemon](administration.md#the-maintenance-daemon-pgcolumnarautovacuum).
- Follow a task-focused recipe for any feature, each with a tuning note:
  [how-to guides](how-to.md).
