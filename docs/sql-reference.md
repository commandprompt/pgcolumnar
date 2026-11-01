# SQL reference

Every function is in the `pgcolumnar` schema. Types are shown as in the function
signature. For server settings, see [Configuration reference](configuration.md).

## Table management

### pgcolumnar.alter_table_set_access_method(t text, method text)

Converts a table to another access method, for example from the default heap to
`pgcolumnar` or back.

On PostgreSQL 15 and later this runs `ALTER TABLE ... SET ACCESS METHOD`, which
rewrites the table in place and preserves its identity and dependents. PostgreSQL 13 and 14 do not have that command. On those two versions, the
function makes a second table with `LIKE ... INCLUDING ALL`. It then copies each
row through the target method and exchanges the names. On those two majors the conversion does not preserve the original table's
OID or objects that depend on it, such as views and foreign keys.

```sql
SELECT pgcolumnar.alter_table_set_access_method('events', 'pgcolumnar');
```

### pgcolumnar.set_options(...) and pgcolumnar.reset_options(...)

Set or reset per-table storage options (row group and vector row limits,
compression codec and level, encode effort, and the declared `sort_by` key). See
[Configuration reference](configuration.md#per-table-storage-options).

`sort_by name[]` declares a physical sort key, applied by
[`pgcolumnar.vacuum_sorted`](#pgcolumnarvacuum_sortedtablename-regclass-variadic-sort_columns-name)
with no explicit columns. It is not auto-maintained; rows inserted after a sort
append in insertion order, so re-run `vacuum_sorted` to re-establish it, like
PostgreSQL `CLUSTER`. Column names must exist and cannot be virtual generated
columns.

`ttl_column name` and `ttl_interval interval` declare a retention. They are read
only by [`pgcolumnar.expire`](#pgcolumnarexpiretablename-regclass-returns-bigint).
Declaring a retention does not delete anything on its own. `expire` runs when
you call it, or when the maintenance daemon calls it with
`pgcolumnar.autovacuum_expire` on. Both are needed: either one alone means no
retention.

`ttl_interval` must be a positive interval. Zero and negative intervals raise
`22023`. A negative interval would put the cutoff in the future, so `expire`
would drop rows that are still inside their retention.

```sql
SELECT pgcolumnar.set_options('events', sort_by => ARRAY['customer_id','ts']);
SELECT pgcolumnar.reset_options('events', sort_by => true);   -- clear it

-- declare a retention; nothing is deleted until expire runs
SELECT pgcolumnar.set_options('events', ttl_column => 'ts',
                              ttl_interval => '90 days');
```

### pgcolumnar.approx_count_distinct(value anyelement) returns bigint

An aggregate: the approximate number of distinct non-NULL values, from a
HyperLogLog of 2048 registers. The standard error is about 2.3%, so most answers
fall within 5% of the exact `count(DISTINCT value)`. It runs on any table, heap
or columnar, and in parallel plans, where it returns the serial answer exactly.

```sql
SELECT pgcolumnar.approx_count_distinct(device_id) FROM readings;
```

The value's type needs an extended hash function, and a text value a
deterministic collation; anything else is refused with SQLSTATE 0A000. Its hash
and registers are those of the sketches a column named in `set_options(t,
sketch_columns => ...)` stores per row group. A query with no WHERE and no
GROUP BY is answered from those sketches, unless another aggregate in it needs
the rows. Each row group's sketch is merged, and only a group with deleted rows,
or one written before the column was named, is read. The answer is the same as
reading every row. EXPLAIN ANALYZE shows `Columnar Sketch Groups Folded` and
`Columnar Sketch Groups Scanned`.

### pgcolumnar.get_storage_id(rel regclass) returns bigint

Returns the internal storage identifier of a columnar table. Used to join the
`pgcolumnar` catalog tables. Most users read [`pgcolumnar.stats`](#pgcolumnarstatsrel-regclass)
instead.

## Maintenance

### pgcolumnar.vacuum(tablename regclass, stripe_count int DEFAULT 0)

Compacts a columnar table by combining small row groups and reclaiming space held
by rows that were deleted or updated. Use it after bulk deletes or updates, or
after many small load transactions have produced many small row groups.

`stripe_count` is not supported and a non-zero value raises an error. The
argument is retained so existing calls that pass the default keep working. It was
documented as bounding how many row groups are combined in one call. It was never
read, and every call rewrote the whole relation regardless of the value.

To bound the work, use
[`pgcolumnar.compact_rewrite`](#pgcolumnarcompact_rewriterel-regclass-min_deleted_fraction-float8-max_groups-int),
whose `max_groups` does limit how many row groups are rewritten.

```sql
SELECT pgcolumnar.vacuum('events');
```

### pgcolumnar.vacuum_sorted(tablename regclass, VARIADIC sort_columns name[])

Compacts a columnar table and stores its rows sorted ascending (NULLS LAST) on
the given columns. Sorted storage makes the per-chunk minimum and
maximum values tight on the sort columns, and it stops them overlapping.
Equality filters and range filters on those columns therefore skip more chunk
groups. Any column with a btree ordering works, and this includes **text**. The
Z-order `cluster()` takes numeric columns only. Use it for a segment
key (e.g. `customer_id`, `hostname`) whose values are scattered in insertion
order but are often filtered on.

With **no columns**, it applies the `sort_by` key that `set_options` declared for
the table. A bare `CLUSTER` re-applies a remembered index in the same way. It
raises an error if the table declares no key:

```sql
SELECT pgcolumnar.vacuum_sorted('events', 'customer_id');          -- explicit
SELECT pgcolumnar.set_options('events', sort_by => ARRAY['customer_id']);
SELECT pgcolumnar.vacuum_sorted('events');                         -- declared key
```

Re-running `vacuum_sorted` with the same key on a table that is still in that
order is a fast no-op. It skips the rewrite on four conditions. The recorded
key matches. The recorded kind is a sort and not a Z-order. The sorted run
already covers every row group. No row group holds a deleted row.

That last condition is part of the gate because `vacuum_sorted` both orders the
rows and reclaims deleted-row space. A table that is in order but holds deleted
rows is rewritten, so the space is still reclaimed. Deletes, inserts, a
different key, or a table arranged by `cluster()` all fall through to the full
rewrite.

A sort key is a **trade** and not a free gain. A sort by a segment key puts the
rows of each segment together, so a filter on that key skips most groups. It also
spreads each *other* dimension across every group.

For time-series data, a sort by `(segment_key, time)` keeps time in order *inside*
each segment. A query that filters on time alone, across all segments, then
prunes less than the natural time order permits.

Sort by the key that your selective queries filter on. This is a one-shot
reorder, and no operation maintains it. Read
[`pgcolumnar.sort_status`](#pgcolumnarsort_statusrel-regclass) to measure how
much of the order remains.

### pgcolumnar.cluster(tablename regclass, VARIADIC columns name[])

Rewrites a columnar table with its rows ordered by a Z-order (Morton)
space-filling curve on the columns that you give. `vacuum_sorted` sorts in
ascending order. Thus it makes the minimum and maximum of its first column
tighter. Z-order makes each clustered column tighter at the same time. Range
filters and point filters on more than one column then skip more vectors and more
chunk groups. The results do not change. Only the physical order changes.

**Holds `AccessExclusiveLock` for the duration**, like PostgreSQL's own `CLUSTER`
and `VACUUM FULL`, because it rewrites the relation and swaps its file. Reads and
writes on the table block until it completes. Use it for an initial bulk
reorganisation; use [`pgcolumnar.recluster`](administration.md#online-maintenance-and-disk-reclaim)
to reorder a live table without an exclusive lock.

```sql
SELECT pgcolumnar.cluster('events', 'customer_id', 'ts');
```

### pgcolumnar.cluster_hilbert(tablename regclass, VARIADIC columns name[])

The same reorganisation on the Hilbert curve instead of the Z-order one. Take it
when the clustered columns carry range filters. The Hilbert index has no jumps at
a bit boundary. Keys that are close in the data therefore stay close in storage,
and a range filter reads fewer chunk groups. Everything else matches `cluster`:
the same arguments, the same refusals, the same `AccessExclusiveLock`.

```sql
SELECT pgcolumnar.cluster_hilbert('events', 'customer_id', 'ts');
```

**The curve is sticky.** The table records which curve it was laid on, and
`pgcolumnar.sort_status` reports it as `sorted_kind`. Once a table is on the
Hilbert curve, plain `cluster` and `recluster` on the same key maintain that
curve rather than converting it back. `vacuum_sorted` leaves the table alone,
and the maintenance daemon re-clusters it with Hilbert. To switch curves, name
the other verb, or recluster on a different key.

### pgcolumnar.recluster(tablename regclass, VARIADIC columns name[]) returns bigint

The online counterpart to `cluster`. Re-establishes the same Z-order clustering
over the given columns, but under `ShareUpdateExclusiveLock`, so concurrent reads
and writes continue instead of blocking. Returns the number of row groups
reclustered.

```sql
SELECT pgcolumnar.recluster('events', 'customer_id', 'ts');
```

Re-running `recluster` with the same key on a table whose clustering is intact is
a fast no-op. The function records the key it last established. It returns 0
without rewriting anything when the recorded key matches, the kind is Z-order,
and the existing sorted run already covers every row group. This is what lets the
maintenance daemon call it on a schedule without churning storage.

On a table laid on the Hilbert curve over the key you name, `recluster`
maintains that curve. It does not convert the table to Z-order.

### pgcolumnar.recluster_hilbert(tablename regclass, VARIADIC columns name[]) returns bigint

The online counterpart to `cluster_hilbert`, and the way to move a Z-ordered
table onto the Hilbert curve. Same arguments, same lock and same return value as
`recluster`; it re-establishes Hilbert clustering rather than Z-order, and
records the curve it applied.

```sql
SELECT pgcolumnar.recluster_hilbert('events', 'customer_id', 'ts');
```

### pgcolumnar.compact(tablename regclass) returns bigint

Retires row groups that are fully deleted, dropping their metadata so scans skip
them. Holds only `ShareUpdateExclusiveLock`, so it runs against a live table.
Returns the number of groups retired.

```sql
SELECT pgcolumnar.compact('events');
```

### pgcolumnar.expire(tablename regclass) returns bigint

Drops row groups whose rows are all older than the retention declared by
`set_options`. Returns the number of groups dropped. Holds only
`ShareUpdateExclusiveLock`, so it runs against a live table.

**This deletes rows.** It runs when you call it, or when the maintenance daemon
calls it with both `pgcolumnar.autovacuum` and `pgcolumnar.autovacuum_expire`
on. The second is off by default. No other operation applies a retention, and
`VACUUM` never does.

Each group it drops is recorded in `pgcolumnar.retired_group`, in the same
transaction. An incremental rollup reads that record to keep the summary of rows
that retention dropped (#1294). No other operation writes there.

It reads no data. A row group records the maximum value of each of its columns.
A group whose maximum is older than the cutoff holds no row still inside the
retention. Its metadata is dropped without decoding anything.

A group is kept whole or dropped whole. A group holding rows on both sides of the
cutoff is kept, and its expired rows stay until every row in that group has
expired. Retention is therefore approximate at the group boundary, and it errs
toward keeping data. A smaller `stripe_row_limit` narrows the boundary.

The retention column must be `date`, `timestamp` or `timestamptz`. The table
must have both `ttl_column` and `ttl_interval` declared, or the function raises
an error rather than reporting that it did nothing.

On a `date` column the cutoff is the day containing `now() - ttl_interval`. A row
dated exactly on that day is kept, so a `date` retention keeps a row up to a day
longer than the interval asks. That is the same direction the group boundary errs
in, and for the same reason. It applies to a whole-day interval too, because the
cutoff is measured from the current instant rather than from midnight.

**The cutoff is the calling session's date, not the server's.** `expire` converts
the current instant through the session's `TimeZone`. Two sessions can therefore
retire different groups. Take a three-day retention, with the server at
`2026-09-22 03:21+00`. A session in `UTC` cuts at `2026-09-19`. One in
`Pacific/Midway` cuts at `2026-09-18`, a day earlier. A `timestamptz` column does not have this behaviour,
because it carries its own zone. Set `TimeZone` explicitly in the session that
calls `expire` if you need the boundary to be the same every time.

`expire` works on whole row groups and never rewrites them, so it drops a group
only when every row in it is past the retention. A group that straddles the
cutoff stays whole, and rows older than the retention survive in it.

It decides from row-group metadata alone on a table with no deletes. On a table
that has any deletes it reads the retention column of a candidate group. The
recorded `NULL` count is written once, so a `NULL` that has since been deleted
must not go on pinning the group. Nothing is rewritten either way.

One live `NULL` in the retention column pins its entire row group. A `NULL` has
no age, so the group cannot be known to be wholly expired. Deleting the `NULL`
rows releases the group, and the next `expire` can drop it. This is stronger
than the straddling rule above. A straddling group is released once its newest
row ages past the cutoff. A group holding a `NULL` never is. Keep the retention
column `NOT NULL` if you want `expire` to reclaim the space.

```sql
SELECT pgcolumnar.set_options('events', ttl_column => 'ts',
                              ttl_interval => '90 days');
SELECT pgcolumnar.expire('events');    -- returns groups dropped
```

### pgcolumnar.compact_rewrite(tablename regclass, min_deleted_fraction float8 DEFAULT 0.2, max_groups int DEFAULT 0) returns bigint

Rewrites partially-deleted row groups, those whose deleted fraction is at least
`min_deleted_fraction`, to drop their dead rows and reclaim the space, under
`ShareUpdateExclusiveLock`. `max_groups` caps how many groups a single call
rewrites; 0 means no cap. Returns the number of groups rewritten.

`min_deleted_fraction` must be a number from 0 to 1, both ends included. The
function rejects `NaN`, a negative value and a value above 1. Each raises SQLSTATE
`22023`, `invalid_parameter_value`. `NaN` needs a test of its own because it
compares false against every bound. An accepted `NaN` would match no row group, so
the call would reclaim nothing and still report success.

```sql
SELECT pgcolumnar.compact_rewrite('events', 0.3);
```

### pgcolumnar.truncate(tablename regclass) returns bigint

Gives the reclaimed blocks at the end of the file back to the operating system.
The function does what it can. It takes `AccessExclusiveLock` for the short
physical step, but only if the lock is available. If the table is busy, the
function returns 0 and does not wait. It removes only the space that became free
before the oldest-xmin horizon. Gated by `pgcolumnar.enable_end_truncation`, which is off by
default. Returns the number of blocks truncated.

```sql
SELECT pgcolumnar.truncate('events');
```

### pgcolumnar.vacuum_full(schema name DEFAULT 'public', sleep_time real DEFAULT 0.0, stripe_count int DEFAULT 0)

Runs `pgcolumnar.vacuum` on every columnar table in a schema. `sleep_time` is a
pause in seconds between tables.

`stripe_count` is passed through to `pgcolumnar.vacuum` and is subject to the same
restriction: a non-zero value raises an error.

```sql
SELECT pgcolumnar.vacuum_full('public');
```

### pgcolumnar.analyze(rel regclass, columns text[] DEFAULT NULL)

Collects planner statistics for named columns of a columnar table. It reads each
column once instead of sampling whole rows. **PostgreSQL 18 or later.** On 17 and
below it raises an error. It writes through `pg_restore_attribute_stats`, which does
not exist before 18.

```sql
SELECT pgcolumnar.analyze('events', ARRAY['customer_id']);   -- one column
SELECT pgcolumnar.analyze('events');                         -- every column
```

**This is not a faster `ANALYZE`.** That difference decides whether you want it. It
reads every value of the columns you name. Core samples 30,000 rows, whatever the
size of the table. Measured on 3,000,000 rows across 20 columns:

| | time |
| --- | ---: |
| core `ANALYZE`, all 20 columns | 7,169 ms |
| `pgcolumnar.analyze(rel, ARRAY['k'])`, 1 column | 1,092 ms |
| `pgcolumnar.analyze(rel)`, all 20 columns | 90,611 ms |

It wins when you want a **subset** of the columns of a wide table. Core cannot do
that at all. `ANALYZE t (k)` still materialises whole rows, so naming one column
saves about 6 percent. It loses badly for every column. On this shape the crossover
is two to three columns. Prefer core `ANALYZE` unless you analyse a few columns of a
wide table.

The extra cost buys **exactness**. A sample can miss a value held by one row in
500,000. It also collapses range estimates above the largest value it saw. This
function reads the column, so the frequencies and the bounds are counted.

Statistics written, for each named column:

| statistic | |
| --- | --- |
| `null_frac` | exact, from the same read as the rest |
| `n_distinct` | exact |
| `most_common_vals` | exact, and with no significance filter |
| `most_common_freqs` | exact |
| `histogram_bounds` | over the rows the most-common list does not hold |

Core applies a significance filter to its most-common list because that list is
sampled. This one is counted, so the filter does not apply.

Not written: `correlation`, and nothing at the level of the relation.
`pg_class.reltuples` stays at `-1` after this function. That looks alarming and is
not. The row estimate of a columnar table comes from the access method, not from the
catalog, so plans still get the right row count. Run core `ANALYZE` if you want the
catalog populated.

**Nothing schedules this.** Autovacuum does not call it. The same is true of
[`pgcolumnar.vacuum`](#pgcolumnarvacuumtablename-regclass-stripe_count-int-default-0),
but the consequence differs. A stale vacuum wastes space. Stale statistics produce
bad plans, and they do it silently. Re-run this function when the data changes, or
keep core `ANALYZE` scheduled and use this only to sharpen particular columns.

### pgcolumnar.stats(rel regclass)

Requires `SELECT` on the table. The function runs with the privileges of the
extension owner, because it reads pgcolumnar's own catalog tables. It checks the
calling role's privilege on the table before it returns anything.

Returns one row per row group, with these columns:

| Column | Type | Meaning |
| --- | --- | --- |
| `stripeid` | bigint | Row group number within the table. |
| `fileoffset` | bigint | Byte offset of the row group in the relation file. |
| `rowcount` | bigint | Rows written into the row group. |
| `deletedrows` | bigint | Rows in the row group marked deleted. |
| `chunkcount` | integer | Vectors in the row group. |
| `datalength` | bigint | On-disk length of the row group in bytes. |

```sql
-- total live rows, deleted rows, and size
SELECT sum(rowcount) AS rows,
       sum(deletedrows) AS deleted,
       pg_size_pretty(sum(datalength)) AS size
FROM pgcolumnar.stats('events');
```

### pgcolumnar.sort_status(rel regclass)

Reports how much of a table's sorted order is still in place. Returns one row:

| Column | Type | Meaning |
| --- | --- | --- |
| `sort_key` | name[] | The clustering key in effect. It is the key the last ordering rewrite recorded, or the `sort_by` declared by `set_options`, or NULL. |
| `sorted_kind` | text | How that key is applied: `lexicographic`, `zorder`, or NULL. NULL means the table was never ordered, or was ordered before pgColumnar recorded this. |
| `total_groups` | bigint | Row groups in the table. |
| `sorted_groups` | bigint | Row groups written by the last ordering rewrite. |
| `appended_groups` | bigint | Row groups written after it. |
| `sorted_rows` | bigint | Rows stored in the sorted groups. |
| `appended_rows` | bigint | Rows stored in the appended groups. |

`pgcolumnar.vacuum_sorted` and `pgcolumnar.cluster` order a table once. Rows
inserted afterwards go in at the end, in insertion order. The sorted part
therefore shrinks in proportion as the table grows. This function measures that
proportion, so you can decide when another sort is worth its cost.

```sql
-- what fraction of the table is still in sorted order, and by what arrangement
SELECT sort_key,
       sorted_kind,
       sorted_rows,
       appended_rows,
       round(100.0 * sorted_rows / nullif(sorted_rows + appended_rows, 0), 1)
         AS percent_sorted
FROM pgcolumnar.sort_status('events');
```

A table that was never sorted reports zero sorted groups. An unsorted
`pgcolumnar.vacuum` returns it to that state, because it rewrites the table
without ordering it.

`sort_key` names the columns, not the arrangement. `pgcolumnar.vacuum_sorted`
sorts on those columns in order. `pgcolumnar.cluster` and
`pgcolumnar.recluster` arrange the same columns on a Z-order curve, which is not
a sort on any one of them.

Read `sorted_kind` to tell the two apart. Before 1.0-alpha3 this function could
not report it, and the documented way to read it was `pgcolumnar.storage`. That
table carries no `GRANT`, so only a superuser could follow that advice.

The row counts are stored rows. Deleted rows stay stored until a maintenance
operation reclaims them, so they are still counted here. Use
[`pgcolumnar.stats`](#pgcolumnarstatsrel-regclass) to read the deleted count per
group.

Three limits apply. The online `pgcolumnar.recluster` records only the part of
its output it can prove is one contiguous ordered run. With no concurrent writer
that is the whole relation. If another session inserts while it runs, it records
less, sometimes much less, and reports the rest as decay. It never reports less
decay than there is. The counts describe where rows are stored, not whether their
values are still in order. An `UPDATE` stores the new row version at the end,
which counts as appended. The record is internal storage metadata and `pg_dump`
does not carry it, so a restored table reports no sorted groups until you sort it
again.

### pgcolumnar.column_extent(rel regclass, col name)

Shows the bound the planner uses for a column's smallest and largest value, when
it estimates how many time buckets a `GROUP BY date_trunc(...)` or `date_bin(...)`
produces. Returns one row, or none:

| Column | Type | Meaning |
| --- | --- | --- |
| `complete` | bool | Always true when a row is returned. |
| `minimum` | text | The smallest value, printed by the column's type. NULL if the column holds no non-NULL value. |
| `maximum` | text | The largest value, printed the same way. |

The bound is kept in `pgcolumnar.column_extent`, and the planner reads it in one
index lookup however many row groups the table has. Writes widen it when they
commit. Every operation that drops or rewrites row groups recomputes it before
committing: `expire`, `compact_rewrite`, `VACUUM`, `recluster`. So it can be
wider than the data only by rows that are deleted but still stored.

It needs `SELECT` on the table, and it refuses a table whose row-level security
policies apply to the caller (see [limitations](limitations.md#row-level-security)).

No row means the planner reads every row group's zone map instead, which gives
the same answer at a cost that grows with the table. That happens for:

- a column added after some row groups were written;
- a type with no ordering;
- a table written by an earlier version, until its next recompute.

The values are column data, so the caller needs `SELECT` on the table.

```sql
SELECT minimum, maximum FROM pgcolumnar.column_extent('events', 'ts');
```

### pgcolumnar.maintenance_due(rel regclass, compact_due_fraction float8 DEFAULT 0.2, recluster_due_fraction float8 DEFAULT 0.05)

Reports whether an online maintenance verb is worth running on a table, from its
statistics alone. It takes no lock and rewrites nothing. This is the policy the
`pgcolumnar.autovacuum` daemon consults on each sweep. You can also call it from a
monitoring query. Returns one row:

| Column | Type | Meaning |
| --- | --- | --- |
| `total_rows` | bigint | Stored rows, including deleted rows not yet reclaimed. |
| `deleted_rows` | bigint | Rows deleted but still stored. |
| `deleted_fraction` | float8 | `deleted_rows` over `total_rows`. |
| `sort_key` | name[] | The recorded clustering key, or NULL. |
| `appended_groups` | bigint | Row groups written after the last ordering. |
| `appended_rows` | bigint | Rows in those appended groups. |
| `appended_fraction` | float8 | Appended rows over the sorted plus appended rows. |
| `compact_rewrite_due` | boolean | True when `deleted_fraction` reaches `compact_due_fraction`. |
| `recluster_due` | boolean | True when a sorted run exists and `appended_fraction` reaches `recluster_due_fraction`. |
| `recommendation` | text | The verbs to run, comma-separated, or NULL when nothing is due. |

The two thresholds default to the values the daemon uses. Each must be a number
from 0 to 1, both ends included. The function rejects `NaN`, `NULL`, a negative
value and a value above 1. Each raises SQLSTATE `22023`, the same code and the
same bounds as `pgcolumnar.compact_rewrite`. An unchecked threshold fails silently
rather than loudly. `NaN`, `NULL` or a value above 1 reports nothing as due, which
suppresses maintenance for good. A negative value reports every table as due on
every sweep.

The function is `SECURITY DEFINER` and checks that the caller may `SELECT` the
table. A monitoring role that owns the table can therefore call it without
superuser rights.

```sql
SELECT recommendation FROM pgcolumnar.maintenance_due('events');
```

## Projections

A projection is a named subset of a table's columns stored a second time,
optionally sorted on a key. When a projection covers a query and serves it better
than the base table, the planner scans the projection instead. See
[Administration](administration.md#projections).

### pgcolumnar.add_projection(rel regclass, name text, columns text[], sort_key text[] DEFAULT '{}')

Declares a projection on `rel` named `name`, storing `columns`, sorted on
`sort_key`. When you add the projection, pgColumnar fills it with the rows that exist.

```sql
SELECT pgcolumnar.add_projection(
    'events', 'events_by_customer',
    columns  => ARRAY['customer_id', 'amount', 'ts'],
    sort_key => ARRAY['customer_id']);
```

### pgcolumnar.drop_projection(rel regclass, name text)

Drops a projection and frees its storage. It also removes the declaration, so a
later rebuild does not create the projection again.

```sql
SELECT pgcolumnar.drop_projection('events', 'events_by_customer');
```

### pgcolumnar.rebuild_projections(rel regclass DEFAULT NULL)

Builds each declared projection that has no storage, and returns the number that
it built. Give a relation to limit it to one table. Give no argument to cover
each table in the database.

`pg_dump` carries the projection declarations, in
`pgcolumnar.projection_declaration`, but it cannot carry the projection storage.
Run this after a logical restore. A second run builds nothing, so it is safe to
run at any time.

You no longer need it after a rewrite. `TRUNCATE`, a rewriting `ALTER TABLE`,
`REFRESH MATERIALIZED VIEW` and the maintenance rewrites re-record their
projections themselves.

Two cases still need this function. The first is a logical restore. The second is
a rewrite that does not pass through `ProcessUtility`, so nothing observes it. A
`TRUNCATE` replicated to a subscriber is one: the logical replication worker calls
`ExecuteTruncateGuts` directly. Run this on the subscriber after one.

A third case looks like this function's job and is not. A declaration that names a
column the table no longer has is reported by a rewrite as

    WARNING:  42703: could not restore projection "p" on "public.t" after rewrite
    DETAIL:   Its declaration names a column the table no longer has.

Do not run this function for that. It re-runs the same declaration and raises the
same missing-column error. Call `pgcolumnar.add_projection()` again instead, with
the same projection name and columns the table has, which is what the `HINT` on
that WARNING tells you.

```sql
SELECT pgcolumnar.rebuild_projections();
```

### pgcolumnar.read_projection(rel regclass, name text) and pgcolumnar.reconstruct_via_projection(rel regclass, name text)

Return a projection's stored rows as text, for verification. They are for
inspection and testing, not for query use.

## Import and export

These functions read and write Arrow IPC files and Parquet files. `export_arrow`
writes the IPC stream format; `import_arrow` reads the stream format and the IPC
file format. They read
and write files on the server host, so a reader needs the `pg_read_server_files`
role and a writer needs the `pg_write_server_files` role. Superusers hold both.
The Parquet functions also read from and write to object storage. See
[Object storage](#object-storage).
They run on little-endian hosts only. They support scalar column types, one-dimensional
arrays, and composite types, with nulls at every level. The functions refuse multi-dimensional arrays
and types that they do not support. See
[Limitations and compatibility](limitations.md).

### pgcolumnar.export_arrow(rel regclass, path text) returns bigint

Writes the live rows of `rel` to an Arrow IPC stream file at `path`. Returns the
number of rows written. The file has no `ARROW1` header or footer, so pyarrow
reads it with `pyarrow.ipc.open_stream`, not `open_file`. `path` can be a local file or an `s3://` URL. See
[Object storage](#object-storage).

### pgcolumnar.export_parquet(rel regclass, path text) returns bigint

Writes the live rows of `rel` to a Parquet file at `path`. Returns the number of
rows written. `path` can be a local file or an `s3://` URL. See
[Object storage](#object-storage).

### pgcolumnar.partition_create_ahead(parent regclass, width interval, ahead int) returns int

Creates columnar partitions of `parent`, each `width` wide, until they reach now
plus `ahead` widths. Returns the number of partitions created. A second call
creates none. The first new partition starts at the highest existing upper
bound. With no partitions, it starts at the width's boundary that holds now.
`parent` must be partitioned by RANGE on one `timestamp` or `timestamptz`
column. It must have no DEFAULT partition and no partition bounded by
`MAXVALUE` (`55000`). A call that would create more than 1000 partitions is
refused before it creates any (`22023`). The function takes
`SHARE UPDATE EXCLUSIVE` on the parent, never `ACCESS EXCLUSIVE`. See
[Create time partitions ahead](how-to.md#create-time-partitions-ahead).

### pgcolumnar.partition_drop_plan(parent regclass, cutoff timestamptz | timestamp) returns setof text

Returns the statements that retire every partition of `parent` whose upper bound
is at or before `cutoff`, in range order. Each partition gets
`ALTER TABLE ... DETACH PARTITION ... CONCURRENTLY`, then a `DO` block that runs
`DROP TABLE`, or `DROP FOREIGN TABLE` for a partition `tier_partition` moved to
Parquet (the file stays). The block refuses (`55000`) while the partition is
still attached, so a failed detach is never followed by a drop that locks the
parent. A partition that an interrupted concurrent detach
left pending gets `DETACH PARTITION ... FINALIZE` instead. A partition that is
itself partitioned is listed whole. The names are schema-qualified. The
function runs nothing: run each statement at top level, for example with
`\gexec`, because a concurrent detach cannot run inside a function. `cutoff`
must have the key's type; the other type is refused (`42804`), and an untyped
literal is a `timestamptz`. It refuses a parent with the same codes as
`partition_create_ahead` (`55000`), and a NULL cutoff (`22023`).
`pgcolumnar.partition_drop_candidates(parent, cutoff_type)` holds the checks
the two overloads share. See [Drop old time partitions](how-to.md#drop-old-time-partitions).

### pgcolumnar.create_rollup(rollup name, source regclass, ts_column name, width interval, origin timestamptz, group_by name[], agg_functions text[], agg_columns name[], access_method name DEFAULT 'pgcolumnar') returns regclass

Creates a rollup of `source` in the source's schema. A rollup is a view of
`date_bin(width, ts_column, origin)` buckets per `group_by` key, with one column
per aggregate. Each aggregate is a function from `agg_functions` (`count`,
`sum`, `min`, `max` or `avg`) applied to the column at the same position in
`agg_columns`. `*` goes only with `count`. `ts_column` must be `timestamptz`.
The data is kept in a table named `<rollup>__partials`, with access method
`access_method`. The function refuses a width with months, an unknown function,
arrays of different lengths, and a column the source does not have (`22023`).
See [Keep a rollup up to date](how-to.md#keep-a-rollup-up-to-date).

### pgcolumnar.refresh_rollup(rollup regclass) returns bigint

Brings the rollup up to date in one statement, and returns the number of
source rows it read. It folds the rows of the row groups added to the source
since the last refresh. For each row group deleted from or rewritten since, it
recomputes from the source the buckets that the group's rows fall in, and
nothing else. Runs at READ COMMITTED only (`25000`).

Row groups that `pgcolumnar.expire` retired are dropped from the refresh's
record, and the rollup keeps their buckets as history. Refuses, changing
nothing, when a bucket to recompute holds history (`0A000`), and after a rewrite
of the source (`55000`).

### pgcolumnar.row_tid(row_number bigint) returns tid

Returns the item pointer that a columnar table gives the row with this row
number. Row numbers start at 1 (`22003` otherwise).

### pgcolumnar.rebase_rollup(rollup regclass) returns bigint

Records the source's current row groups as folded, without changing the rollup,
and returns the number of row groups recorded. It reads every row group to
record the time range of its rows, and keeps the rollup's history. Use it after
a rewrite of the source, or after a refusal to recompute history, when the
rollup is right as it stands.

### pgcolumnar.tier_partition(partition regclass, server name, path text, lock_wait interval DEFAULT '10 seconds') returns bigint

Moves the columnar partition `partition` to a Parquet file at `path`. Then
attaches the file as a foreign table of `server`, which must use the
`pgcolumnar_parquet` wrapper. The foreign table takes the partition's name,
columns (with their collations and generated expressions), owner, bounds,
grants and comment. Returns the number of rows moved.

The function runs as one transaction, at READ COMMITTED only (`25000`
otherwise). It holds `ACCESS EXCLUSIVE` on the parent from the detach until
commit. Every lock waits at most `lock_wait` (`55P03`).

The function refuses these:

- a `path` that a foreign table in this database reads (`58P02`);
- a `DEFAULT` partition (`0A000`);
- a parent with a `UNIQUE` index or primary key (`0A000`);
- a partition that other objects depend on (`2BP01`);
- a relation that is not a columnar partition (`42809`).

See
[Move cold partitions to Parquet](how-to.md#move-cold-partitions-to-parquet).

### pgcolumnar.import_arrow(rel regclass, path text) returns bigint

Inserts the rows of an Arrow IPC file at `path` into the existing table
`rel`. The column types of the table define the types that the function accepts. Returns the number of
rows inserted.

The file can be in the IPC stream format, which `export_arrow` writes, or in the
IPC file format, which starts and ends with `ARROW1`. Feather v2 is the IPC file
format. The function reads the stream inside an IPC file and ignores its footer.
A message or body length larger than the rest of the file is refused with
`XX001` before any memory is allocated for it.

Temporal columns are read in the unit the file declares, not the unit
`export_arrow` writes. A `date` column accepts `date32` and `date64`. A `time`
column accepts `time32` in seconds or milliseconds, and `time64` in microseconds
or nanoseconds. A `timestamp` column accepts any of the four `Timestamp` units.
Values are converted to PostgreSQL's own units, and a value that cannot be
represented is refused with `22008` rather than stored wrong.

Nanoseconds are narrowed to microseconds, the finest resolution PostgreSQL
stores. Narrowing floors, so an instant before 1970 reports the microsecond it
falls in. Sub-microsecond precision is lost; nothing else is.

A temporal Arrow type the target column cannot hold is refused with `42804`
rather than read. A `time64` file does not import into a `date` column. A file
whose type is not temporal is unaffected.

### pgcolumnar.import_parquet(rel regclass, path text) returns bigint

Inserts the rows of a Parquet file at `path` into the existing table `rel`. The
reader handles uncompressed, Snappy, GZIP, ZSTD, and LZ4_RAW pages, PLAIN and
dictionary encodings, and data-page versions 1 and 2. `path` may name a single
file, a directory (every `*.parquet` file below it is imported, at any depth),
  or a glob
pattern. Returns the number of rows inserted.

```sql
-- round-trip a table through Parquet
SELECT pgcolumnar.export_parquet('events', '/tmp/events.parquet');   -- returns row count
CREATE TABLE events_copy (LIKE events) USING pgcolumnar;
SELECT pgcolumnar.import_parquet('events_copy', '/tmp/events.parquet');

-- import an entire directory of Parquet files
SELECT pgcolumnar.import_parquet('events_copy', '/data/events/');
```

### pgcolumnar.parallel_copy(target regclass, filename text, workers int DEFAULT NULL, dedup boolean DEFAULT false) returns bigint

Loads a text file into a columnar table with several background workers at once,
as one atomic operation. Returns the number of rows loaded. The caller needs
membership in the `pg_read_server_files` role, which superusers hold, and INSERT
on the target. The file uses COPY text format. Each worker runs core `COPY` over
a byte range of the file, so parse and write behavior match `COPY FROM` exactly.

The target may be one of two kinds:

- A single columnar table. The workers write the one table together. Any
  record-aligned split of the file is correct, so the file needs no ordering.
- A RANGE-partitioned table whose partitions are columnar. Each worker loads a
  distinct set of partitions. The file must be sorted ascending by the partition
  key. The key column may sit anywhere in the row, and its type must be numeric
  or a date/time type. The function reports an error when the file is not sorted.

The load is atomic. Each worker prepares its transaction rather than committing,
and a coordinator commits them together only if every worker succeeded. A bad
row, a full disk, or a constraint failure in any range rolls the whole load back.
The target keeps its earlier contents. The load runs in background workers, so it
commits on its own. It is not part of the calling transaction, and a caller
`ROLLBACK` does not undo it. Set `max_prepared_transactions` above the worker
count, because the load prepares one transaction per worker.

When `workers` is omitted the function derives a value from the target. For a
partitioned target it lowers `workers` to the partition count when the count is
smaller.

#### Refusing a load this table has already taken

A load that commits, whose acknowledgement the client never receives, is retried,
and the rows go in twice. Pass `dedup => true` to refuse the repeat.

With `dedup`, the function records the SHA-256 of the file after a successful
load, in `pgcolumnar.load_fingerprint`. A later load of a file with the same
contents into the same table stores nothing, returns 0, and raises a `NOTICE`
saying why. It does not fail. A file whose contents changed is a different load
and is stored, even at the same path.

`dedup` is off by default. Discarding rows a caller asked to store is not
ordinary `INSERT` behavior, so it happens only when asked for, and only on this
function.

Three limits apply.

The fingerprint is recorded after the data commits. A crash between the two
leaves the data stored and unrecorded, so a later retry stores it again. That is
the behavior without `dedup` and is the safe direction.

Two identical loads running at the same time both store their rows. Each checks
the record before either writes it. `dedup` refuses a load that follows a
completed one; it does not serialize concurrent loads.

A refused load still does the work. The rows are read, parsed and written, and
then discarded without being made visible, because the check happens after every
worker has prepared. The alternative costs every load a serial pass over the file
before any worker starts. Measured on a 264 MiB file, that pass takes 42% of the
load, while the fingerprint as implemented has no measurable cost.

```sql
-- refuse a repeat of a load this table has already taken
SELECT pgcolumnar.parallel_copy('events', '/data/events.txt', 8, true);
```

```sql
-- single columnar table, any row order
CREATE TABLE events (id bigint, ts timestamptz, val double precision) USING pgcolumnar;
SELECT pgcolumnar.parallel_copy('events', '/data/events.txt', 8);   -- returns row count

-- RANGE-partitioned target, file sorted ascending by the partition key
SELECT pgcolumnar.parallel_copy('events_by_day', '/data/events_sorted.txt', 8);
```

### pgcolumnar.parallel_export_parquet(target regclass, path text, workers int DEFAULT NULL) returns bigint

Writes a columnar table to a directory of Parquet files with several read-only
background workers at once. Returns the number of rows written. The caller needs
membership in the `pg_write_server_files` role, which superusers hold, and SELECT
on the target. The output directory must be empty. It is created if it does not exist. Each worker writes
its own `part-NNNN.parquet` file, and `pgcolumnar.read_parquet` reads the whole
directory back as one relation.

The `path` may also be an `s3://` prefix. Each worker writes one
`part-NNNN.parquet` object under it, exactly as for a local directory. See
[Object storage](#object-storage) for the endpoint and credential rules, which
are the same as for `export_parquet`. One difference applies to a remote prefix.
The local path is created and checked for emptiness before the export. A remote
prefix is not checked, so a re-run may write into a used prefix. Before it
writes the `_SUCCESS` marker, the export lists the prefix to remove stale parts
a larger prior run left. Its credentials therefore need list permission
(`s3:ListBucket` on AWS) as well as write and delete. If the listing fails,
those stale parts can remain, as described below under "A failed listing can
leave a larger earlier run's parts".

The target may be one of two kinds:

- A single columnar table. The workers split it by row-group ranges, so each
  worker writes a distinct part of the table.
- A partitioned table whose partitions are columnar. Each worker takes a distinct
  set of partitions and writes one file per partition.

The export is read-only and consistent. The dispatcher exports one snapshot and
every worker imports it. The files together are the committed image of the table
at call time. This holds even when the call runs inside a transaction with
uncommitted rows. There is no coordinator and no shared write state. If any worker
fails, or the statement is cancelled, the dispatcher removes the files it wrote.
It also removes them when every worker has finished but a later step fails:
removing stale parts or writing the `_SUCCESS` marker. Over an `s3://` prefix
the dispatcher deletes the same objects by key. A worker terminated mid-upload
can leave one incomplete multipart upload that the dispatcher cannot address.
Set a bucket lifecycle rule that expires incomplete multipart uploads to reclaim
it, as [Exporting to object storage](#exporting-to-object-storage)
recommends.

**A failed listing can leave a larger earlier run's parts.** The cleanup
above removes a failed run's own parts. So a partial directory is never left for
`read_parquet` to union, with one exception: a failed listing of a remote prefix.
A key that may write but not list fails the run, and the dispatcher removes the
parts this run wrote. Without the listing, though, the export cannot find the
higher-numbered parts that a larger earlier run left in the prefix, so they
remain. A run of 4 workers followed by a failed run of 2 leaves `part-0002` and
`part-0003`, with no `_SUCCESS` marker. A later directory or prefix read would
read those stale objects back as if they were a whole table. So check for
`_SUCCESS` before you read a prefix, and read only a prefix that carries it.

On success the function writes an empty `_SUCCESS` marker at the destination, the
Hadoop and Spark convention. Its presence means a complete run's output is there;
a failed or cancelled run, whose part files the dispatcher removes, leaves none.
The marker is written last, after every worker has finished, so a directory or
prefix that carries it is a whole export. A remote prefix allows a re-run to
overwrite a used prefix. A smaller re-run there first removes the stale
higher-numbered parts a larger prior run left, so the marker certifies exactly
the parts this run wrote. `read_parquet` and the foreign-data wrapper skip it
when they read the directory or prefix. They skip any name below it that begins
with an underscore.

When `workers` is omitted the function derives a value from the target.

```sql
-- single columnar table, split across 8 workers
SELECT pgcolumnar.parallel_export_parquet('events', '/data/events_out', 8);
-- read the whole directory back as one relation
SELECT count(*) FROM pgcolumnar.read_parquet('/data/events_out')
  AS t(id bigint, ts timestamptz, val double precision);
```

## Reading external Parquet

These read a server-side Parquet file in place, without importing it. They
require the `pg_read_server_files` role, which superusers hold, and operate on
little-endian hosts. In each function, `path`
can be one of three things. It can be a single file. It can be a directory, and
then the function reads all the `*.parquet` files below it at any depth as one
relation, in sorted order. It can also be a glob pattern. A local `path` can be
any of these. An object-storage URL can be an exact key, and an `s3://` URL can
also be a prefix or a pattern. See [Object storage](#object-storage).

### pgcolumnar.read_parquet(path text) returns setof record

Returns the rows of a Parquet file. You must supply a column definition list. It
gives the names of the output columns and their types. The reader connects the
list to the leaf columns of the file by position. It uses the same rules for type
compatibility as the import functions.

The list must contain each leaf column in the file. A list with fewer columns is
an error and not a projection. The read stops and gives a message. The message
contains the number of leaf columns in the file and the number that the target
expands to. The same rule applies to a foreign
table's column definitions. Projection pushdown selects the declared columns
that the reader decodes. This is a separate question from the number of columns
that you must declare. Use
`parquet_schema` to generate the full list.

A string cell read into a `text` or `varchar` column must be valid in the
server encoding (#1821). A cell with an invalid byte sequence or an embedded
NUL byte stops the read with SQLSTATE `22021`, as COPY does. The message names
the Parquet column and the first bad bytes. This applies to the foreign table,
the import functions and `iceberg_scan` too, because they share the reader.

```sql
SELECT * FROM pgcolumnar.read_parquet('/data/events.parquet')
  AS t(id int, ts timestamp, amount numeric(12,2));

-- read a whole directory
SELECT count(*) FROM pgcolumnar.read_parquet('/data/events/')
  AS t(id int, ts timestamp, amount numeric(12,2));
```

### pgcolumnar.read_parquet(path text, field_ids integer[]) returns setof record

Returns the rows of a Parquet file, binding output columns to file columns by
Parquet field id rather than by position. Output column `i` is bound to the file
column whose field id equals `field_ids[i]`. The reader decodes only those
columns, in the order given, whatever their order in the file. The array length
must equal the column definition list length. This is the projection form Apache
Iceberg uses (#388). A data file written before a column rename still carries the
old name, and columns are selected by id.

The file must carry field ids. A file written without them is an error that
directs you to the positional form above. Each requested id must match exactly
one scalar column of a compatible type. A compatible type includes Iceberg's two
exact widenings: an integer `INT32` column reads as `bigint`, and a `FLOAT`
column as `double precision`. The positional form above does not widen. An
absent id is an error. So is an id that matches more than one column, or an
array or composite output column. An id that belongs to a struct, list or map is
refused as nested, whatever the output column's type (#1788). None is a silent
wrong column. Read the ids a file carries with `parquet_schema`.

```sql
-- the file has columns alpha (id 7), beta (id 3), gamma (id 12);
-- read gamma then alpha, by id, in that order
SELECT * FROM pgcolumnar.read_parquet('/data/events.parquet', ARRAY[12, 7])
  AS t(g int, a int);
```

### pgcolumnar.parquet_schema(path text) returns table(column_name text, data_type text, nullable bool, field_id int)

Reports the leaf columns of a Parquet file and the PostgreSQL type each maps to,
without reading the data. Useful for writing the column definition list for
`read_parquet` or a foreign table. For a directory or glob it describes the first
file. The type it reports holds every value the column can store. So an unsigned
`UINT_32` column reports `bigint`, and a `UINT_64` column reports `numeric(20,0)`.

A millisecond or microsecond timestamp column that holds instants
(`isAdjustedToUTC=true`, or the older `TIMESTAMP_MILLIS` or `TIMESTAMP_MICROS`
type) is reported as `timestamp with time zone`. A column of wall times
(`isAdjustedToUTC=false`) is reported as `timestamp without time zone`. Each
advised type reads the value unchanged. See
[Timestamps and time zones](user-guide.md#read-parquet-files-in-place).

`field_id` is the Parquet schema field id the column carries. Formats such as
Apache Iceberg use it to select columns by id rather than by name. It is NULL when
the writer emitted no field id. A field id of 0 is a real value, so a NULL and a 0
mean different things.

```sql
SELECT * FROM pgcolumnar.parquet_schema('/data/events.parquet');
```

### pgcolumnar.read_avro_manifest(path text) returns table(status int, content int, file_path text, file_format text, record_count bigint, file_size_in_bytes bigint, partition text, sequence_number bigint)

Decodes an Apache Iceberg Avro manifest file and reports its data-file entries.
Each row is one entry: the file path, format, row count, byte size, the
partition rendered as `name=value`, and the entry's data sequence number. The
sequence number is NULL when the entry inherits it from the manifest. That is
the usual case for a freshly written manifest. It is the ordering key for
reading tables with deletes. A position delete applies to data files with a
lower or equal sequence number, the same commit or earlier. An equality delete
applies to a strictly lower one. The caller needs the `pg_read_server_files`
role, which superusers hold. This is the first step of Iceberg support (#388).
It is a standalone Avro object-container reader, decoded against the schema
embedded in the file, so a v3 manifest reads structurally. It reads a local
file. It does not resolve a table's snapshot or apply delete files, which are
later steps.

```sql
SELECT file_path, record_count, partition
  FROM pgcolumnar.read_avro_manifest('/data/warehouse/db/events/metadata/abc.avro');
```

### pgcolumnar.read_manifest_list(path text) returns table(manifest_path text, manifest_length bigint, content int, partition_spec_id int, added_files_count int, existing_files_count int, deleted_files_count int, added_rows_count bigint, existing_rows_count bigint, deleted_rows_count bigint, sequence_number bigint, min_sequence_number bigint, added_snapshot_id bigint)

Decodes an Apache Iceberg snapshot manifest-list Avro file and reports its
`manifest_file` entries. Each row names one manifest the snapshot points at. It
carries the manifest's length, content type (0 data, 1 deletes), and partition
spec. It also carries the added, existing, and deleted file and row counts the
writer recorded. The caller needs the `pg_read_server_files` role, which
superusers hold. This is step two of Iceberg support (#388). It reads the same
Avro object-container format as `read_avro_manifest`, decoded against the schema
embedded in the file. A v3 manifest list therefore reads structurally. It reads
a local file. It does not resolve a table's current snapshot or open the
manifests it names, which are later steps.

```sql
SELECT manifest_path, added_files_count, added_rows_count
  FROM pgcolumnar.read_manifest_list('/data/warehouse/db/events/metadata/snap-123.avro');
```

### pgcolumnar.iceberg_current_snapshot(metadata_path text) returns table(snapshot_id bigint, parent_snapshot_id bigint, sequence_number bigint, timestamp_ms bigint, operation text, manifest_list text, schema_id int)

Reads an Apache Iceberg table `metadata.json` and reports the current snapshot
the table declares. The current snapshot is the one whose id equals the file's
`current-snapshot-id`. The row carries its sequence number, commit timestamp,
and operation (`append`, `overwrite`, `delete`, `replace`). It also carries the
manifest-list file the snapshot points at and its schema id. It returns one row,
or no rows when the table has no current snapshot. The caller needs the
`pg_read_server_files` role, which superusers hold. This is the start of Iceberg
catalog support (#388 phase 3): it resolves the metadata pointer from the
filesystem without a network. It reports the `manifest_list` path as the file
records it. It does not yet open that file or resolve the table's data files.
Those are later steps. The manifest-list it names can then be decoded with
`read_manifest_list`.

```sql
SELECT snapshot_id, operation, manifest_list
  FROM pgcolumnar.iceberg_current_snapshot('/data/warehouse/db/events/metadata/v3.metadata.json');
```

### pgcolumnar.iceberg_data_files(metadata_path text) returns table(file_path text, file_format text, record_count bigint, partition text)

Lists the live data files of an Apache Iceberg table at its current snapshot.
It resolves the current snapshot from `metadata.json`, reads that snapshot's
manifest list, then each manifest. It returns one row per data-file entry: the
file path, format, row count, and partition rendered as `name=value`. The
caller needs the `pg_read_server_files` role, which superusers hold. It returns
no rows when the table has no current snapshot.

The absolute paths recorded in the table are rebased onto the table's actual
location. That location is taken from where `metadata.json` sits. A table copied
to a new directory therefore still reads. A recorded path that points outside
the table location is refused, not read.

A data file that the current snapshot removed is not live, so it is skipped. A
copy-on-write delete or a filtered overwrite records the file it replaced this
way, as a manifest entry with status 2 (DELETED).

Delete files are refused, not ignored. A snapshot that carries any delete
manifest or delete file raises an error here. It does not return rows the table
says are gone. To read a table that uses deletes, use `iceberg_scan`, which
applies position, equality, and deletion-vector deletes.

```sql
SELECT file_path, record_count, partition
  FROM pgcolumnar.iceberg_data_files('/data/warehouse/db/events/metadata/v3.metadata.json');
```

### pgcolumnar.iceberg_scan(metadata_path text) returns setof record

Reads an Apache Iceberg table at its current snapshot. You supply a column
definition list. Each output column name is resolved to a field id through the
table's current schema. Every live data file is then read, projected by those
ids. Iceberg selects columns by field id. A data file written before a column
was renamed still reads. The name in the file need not match, only the id. The
caller needs the `pg_read_server_files` role, which superusers hold.

The output column names must be fields of the table's current schema; a name
that is not is an error. Matching is case sensitive against the schema, so quote
a mixed-case name in the column definition list to preserve its case. Only
Parquet data files are read.

The table may live in object storage. A metadata path of `s3://`, `http://`, or
`https://` reads the metadata, manifests, data files, and delete files from the
endpoint. The endpoint must be listed in `pgcolumnar.objstore_allowed_endpoints`,
and credentials come from the process environment, the same way `read_parquet`
reads a remote file. The results are identical to reading the same table from a
local path.

A data file written outside Iceberg carries no field ids. Such a file is read
through the table's `schema.name-mapping.default` property, which maps each
field id to the column names an id-less file may use. Each id-less column is
bound to the field id whose mapping lists its name. A file that carries field
ids ignores the mapping. An id-less file with no such property is refused,
because the specification defines no positional fallback, and the error names
the property. The mapping is read for top-level scalar columns.

Only top-level primitive columns can be read. An output column that names a
struct, list or map field is an error (SQLSTATE `0A000`). This holds whatever
type the column definition list gives it. The error names the column and its
kind (#1788). The same holds for `iceberg_rest_scan` and the
`pgcolumnar_iceberg` foreign-data wrapper.

A projected column that the file does not carry reads as null. This covers a
column added to the schema after the file was written. It also covers a column
an id-less file has but the mapping does not bind. The file's other columns
return their real values.

A column whose type was promoted reads as its current type. Iceberg allows three
promotions: `int` to `long`, `float` to `double`, and a `decimal` to a larger
precision at the same scale. A data file written before the promotion still
stores the old type, and its values are widened as they are read. Declare such a
column with the current schema's type: `bigint`, `double precision` or `numeric`.
Files from before and after the promotion read together. An equality delete on a
promoted column applies to both, whichever type its own file stores. A declared
type narrower than a file's stored type is refused. So a column declared with its
pre-promotion type errors once a file written after the promotion is live.

Row-level deletes are applied, each kind under its own Iceberg sequence rule.
A position delete drops the row ordinals it lists from the data file it names.
It applies when that data file's data sequence number is at or below the
delete's. A position delete can affect data written in the same commit, so the
two may share a sequence number. An equality delete drops every data row that
equals a delete row on all of the delete file's `equality_ids` columns. It
applies only when the data file's sequence number is strictly below the
delete's. An equality delete never affects data from its own commit. A null
delete value matches a null data value, and only a null. Columns in the delete
file beyond `equality_ids` do not take part in the match. A delete that is not
strictly newer than any data file has no effect and is skipped.

Format version 3 encodes position deletes as deletion vectors. A deletion
vector is a compressed bitmap of row ordinals, stored as a blob in a Puffin
file and scoped to one data file. It applies under the same sequence rule as a
position delete file. When a deletion vector applies to a data file, position
delete files for that file are ignored. The Iceberg specification requires the
writer to fold their deletes into the vector. A snapshot may carry at most one
deletion vector per data file; a second one is refused. The blob's checksum,
its offsets against the Puffin footer, and its recorded cardinality are all
verified, and a mismatch is refused. Deletion vectors in a table below format
version 3 are refused, as is a compressed Puffin footer.

An equality delete written under a partitioned spec is applied within its
partition. Its stored partition values are compared against each data file's,
and it removes rows only from data files in the same partition. A delete for a
partition that holds no data removes nothing. Some equality-delete forms are
refused rather than ignored. A table using them errors instead of returning
rows it should have removed. A delete column is refused when its type has no
supported mapping; the supported types are `int`, `long`, `string`, `boolean`,
and `date`. A delete column that is no longer in the table's current schema is
refused. A delete column missing from an older data file is also an error. A
partition value the reader cannot compare exactly, such as a floating-point
value, is refused. The recorded file paths are rebased
onto the table's actual location and resolved against a path boundary. A
relocated table reads, and a path pointing outside the table is refused.
Applying equality deletes reads each affected data file's delete columns
twice. A probe pass computes the row ordinals to drop.

A malformed manifest is refused, not read as far as it parses. A manifest entry
that records no data-file path is an error, as is a manifest whose embedded Avro
schema is not well formed. An enum value outside the Iceberg spec is refused with
`XX001` and the value in the message. A data file `content` or an entry
`status` must be 0, 1 or 2. A manifest list `content` must be 0 or 1.

```sql
SELECT id, region, sum(amount)
  FROM pgcolumnar.iceberg_scan('/data/warehouse/db/events/metadata/v3.metadata.json')
    AS t(id bigint, region text, amount int)
  GROUP BY id, region;
```

### pgcolumnar.iceberg_rest_table_location(catalog_uri text, namespace text, table_name text) returns text

Resolves a table named by an Iceberg REST catalog to the URI of its current
metadata file. The function calls the catalog over HTTP or HTTPS. It reads the
catalog configuration, loads the table, and returns the reported metadata
location. That location is an ordinary metadata path, so it is read with
`iceberg_scan` and the other functions above.

The catalog endpoint is subject to `pgcolumnar.objstore_allowed_endpoints`, the
allow-list that governs every other remote access. A host that resolves to a
link-local or instance-metadata address is refused whether or not it is listed.
The request is carried by the `pgcolumnar_objstore` module, so no additional TLS
library is loaded into the server process. HTTPS requires the module to be built
with OpenSSL, as for object storage.

The first argument is either a catalog URI or the name of a foreign server. A
value beginning with `http://` or `https://` is a URI. Any other value names a
server. A server name can never look like a URI, so the two forms never collide.

When the first argument is a URI, the token comes from the environment. It is
read from the `PGCOLUMNAR_ICEBERG_REST_TOKEN` variable in the server process. It
is never a function argument, so it does not appear in the statement log or in
`pg_stat_activity`. A catalog that needs no token is queried without one.

When the first argument names a server, the catalog URI comes from the server
and the token comes from the current role's user mapping. The role needs `USAGE`
on the server, as `dblink` requires for a named server. Without it the call is
refused with SQLSTATE 42501 before any mapping is read or any request is made,
even through a `PUBLIC` mapping. So `REVOKE USAGE ON FOREIGN SERVER` takes the
server away from a role. The token lives in
`pg_user_mapping`, which is not world-readable, so one role's token is not
visible to another. A role with no mapping and no token is refused. A superuser,
or a mapping that sets `credentials_required 'false'`, uses the environment token
instead.

A user mapping may carry OAuth2 client credentials rather than a static token.
When it sets `oauth_client_id` and `oauth_client_secret`, the catalog is asked to
mint a bearer by the client-credentials grant. The request goes to
`oauth_token_uri` when set, otherwise to `{catalog_uri}/v1/oauth/tokens`, with an
optional `oauth_scope`. The client secret travels in the request body, never a
URL or a log line. A mapping that sets only one of the pair is refused before any
request is made, even when it also sets a static `token`. A mapping that sets a
`token` and the complete pair uses the token and mints no bearer.

Multi-level namespaces are given dot-separated, and the function requires the
`pg_read_server_files` role, like the other Iceberg functions. The server form
also requires `USAGE` on the server.

Every REST function first reads the catalog configuration (`GET /v1/config`) and
follows it:

- The `prefix` goes after `/v1/` in every later request. A prefix may contain
  `/`, such as `catalogs/main`; each part is sent as its own path segment.
- The `namespace-separator` joins the levels of a multi-level namespace in a
  request. The default is the unit separator, sent as `%1F`. The Apache Iceberg
  REST catalog advertises `%2E`.
- A key can be set in more than one place. The value in `overrides` wins.
  Next comes a top-level key of the same name, and last the value in `defaults`.

```sql
-- URI form: the token, if any, comes from the server environment, not the query
SELECT pgcolumnar.iceberg_rest_table_location(
         'https://catalog.example.com', 'analytics', 'events');
--> s3://warehouse/analytics/events/metadata/00042-....metadata.json

-- server form: the token is per-role and stays in pg_user_mapping
CREATE SERVER cat FOREIGN DATA WRAPPER pgcolumnar_iceberg_catalog
  OPTIONS (catalog_uri 'https://catalog.example.com');
GRANT USAGE ON FOREIGN SERVER cat TO analyst;
CREATE USER MAPPING FOR analyst SERVER cat OPTIONS (token 's3cr3t');

SELECT count(*) FROM pgcolumnar.iceberg_scan(
  pgcolumnar.iceberg_rest_table_location('cat', 'analytics', 'events'))
  AS t(id bigint, region text, amount int);
```

The `pgcolumnar_iceberg_catalog` wrapper has a validator but no handler, so its
servers cannot be selected from as tables. It accepts `catalog_uri` and an
optional `warehouse` on a server. The `warehouse` selects a warehouse on a
multi-warehouse catalog and is sent as the `?warehouse=` parameter on the
`GET /v1/config` request. On a user mapping it accepts `token`, the OAuth2 options
(`oauth_client_id`, `oauth_client_secret`, `oauth_scope`, `oauth_token_uri`), and
`credentials_required`. A secret on a server, where options are world-readable,
is rejected. Setting `credentials_required 'false'` is restricted to a superuser.

### pgcolumnar.iceberg_rest_scan(catalog_uri text, namespace text, table_name text) returns setof record

Reads a table named by an Iceberg REST catalog at its current snapshot. It takes
a column definition list exactly like `iceberg_scan`. The catalog resolves the
table to its metadata location. That location is read through the same path, so
field-id projection and every delete rule apply unchanged. The catalog and
authentication rules are those of `iceberg_rest_table_location` above, including
the allow-list, the link-local refusal, and the environment bearer token.

A catalog can vend storage credentials in its `loadTable` reply. The data,
metadata, and delete files are then read with those credentials, not the server
environment. Both the flat `config` keys and the `storage-credentials` array are
read, and the longest-prefix match is used. Vended credentials do not bypass the
allow-list. The endpoint is still checked. A table that vends no credentials is
read with the ambient environment, as before.

```sql
SELECT region, sum(amount)
  FROM pgcolumnar.iceberg_rest_scan('https://catalog.example.com',
                                    'analytics', 'events')
    AS t(id bigint, region text, amount int)
  GROUP BY region;
```

### pgcolumnar.iceberg_rest_namespaces(catalog_uri text) returns setof text

Lists the namespaces of a catalog, one per row, at every level. A multi-level
namespace is returned dot-joined, for example `analytics.daily`. Same catalog and
authentication rules as above.

The catalog lists only the direct children of a namespace. So the function asks
for the top level, then asks for the children of each namespace it finds
(`?parent=`). That is one request for the top level, plus one for each
namespace. A namespace can be up to 64 levels deep. If the catalog has a
namespace 65 levels deep, the function raises an error.

The function needs every one of these requests to succeed. If the catalog
answers an error to any of them, the function raises that error and returns no
rows. This includes a catalog that does not accept `?parent=`. On such a
catalog, list the tables of a namespace you know with `iceberg_rest_tables`.

Some catalogs also list nested namespaces at the top level. The function
returns each namespace once.

### pgcolumnar.iceberg_rest_tables(catalog_uri text, namespace text) returns setof text

Lists the table names in a namespace, one per row.

Both list functions follow the catalog's `next-page-token`, so a catalog that
splits a list into pages is read to the end.

```sql
SELECT * FROM pgcolumnar.iceberg_rest_namespaces('https://catalog.example.com');
SELECT * FROM pgcolumnar.iceberg_rest_tables('https://catalog.example.com', 'analytics');
```

### The pgcolumnar_parquet foreign-data wrapper

Exposes a Parquet file, directory, or glob as a foreign table. The scan streams
one row group at a time. It holds a single row group rather than the whole file.
A `LIMIT` the plan satisfies early leaves the rest of the file undecoded. It
pushes work down: row groups whose min/max statistics exclude the
query's predicate are skipped, and only referenced columns are decoded. Skipping
requires a `column op constant` clause over an integer or floating-point column
with a constant of the same type; [limitations.md](limitations.md) lists the
conditions. A scan that skips nothing still returns correct rows.

The `path` option can name a local file, directory, or glob, or an
object-storage URL. A remote URL is an exact object, a prefix ending in a slash,
or a pattern, expanded the same way as a local path. For a remote server the
endpoint and credentials come from the server and user-mapping options described
in [Object storage](#object-storage).

Table options: `path`, and `partition_columns` for a Hive-style layout. The
latter names the columns whose values come from `col=value` directory components
rather than from the files. You declare these columns. pgColumnar does not infer
them, because an incorrect value would change the rows that a query returns, with
no message. A predicate on a partition column removes complete files before the
reader opens them. The plan shows this as
`Files Pruned`.

```sql
CREATE SERVER pq FOREIGN DATA WRAPPER pgcolumnar_parquet;
CREATE FOREIGN TABLE events (id int, ts timestamp, amount numeric(12,2))
  SERVER pq OPTIONS (path '/data/events/');

-- events/dt=2026-01-01/region=eu/part-0.parquet
CREATE FOREIGN TABLE events_p (id int, amount numeric(12,2), dt date, region text)
  SERVER pq OPTIONS (path '/data/events', partition_columns 'dt,region');

SELECT sum(amount) FROM events WHERE ts >= '2026-01-01';

-- EXPLAIN ANALYZE reports Row Groups, Row Groups Skipped, Row Groups Decoded,
-- Columns Read, Columns Total, and Files.
EXPLAIN (ANALYZE, COSTS OFF) SELECT id FROM events WHERE ts >= '2026-01-01';
```

### The pgcolumnar_iceberg foreign-data wrapper

Exposes an Apache Iceberg table as a foreign table. Unlike `iceberg_scan`, which
is a set-returning function that receives no predicate, the foreign table gets
the query's quals and prunes data files two ways. A predicate on an
identity-partitioned column removes whole files by their partition value. An
equality predicate on a `bucket[N]`-partitioned column removes files whose stored
bucket differs from the constant's. A predicate on an integer or boolean column
removes whole files whose stored minimum and maximum exclude it. An unpartitioned
column can prune this way. All read from the manifest, so a file is skipped
without a read. Pruning is only an optimization. A file that is not pruned is read
normally, so a predicate the wrapper cannot decide never changes the rows
returned. Field-id projection and every delete rule are those of `iceberg_scan`.
A foreign table may declare a nested Iceberg column (a struct, list or map) as a
scalar. Such a table refuses every query, even one that does not read that
column (#1788).

The one table option is `metadata_path`, the table's current `metadata.json`
(a local path or an object-storage URL). The wrapper requires the
`pg_read_server_files` role. `EXPLAIN (ANALYZE)` reports `Files Pruned`.

Partition pruning covers the identity, `bucket[N]`, `truncate[W]`, and temporal
transforms. Identity partitioning prunes on any predicate. `bucket[N]` prunes on
an equality predicate on the source column under a deterministic collation.
Iceberg buckets a string by its UTF-8 bytes, and under a nondeterministic
collation two equal strings can differ in bytes. So such an equality reads every
file. `truncate[W]` prunes on a predicate
on an integer source column. `day()` on a date source column prunes on a
predicate on that column.

The `year()`, `month()`, `day()`, and `hour()` transforms also prune on a range
or equality predicate. They apply to a `timestamp` or `timestamp with time zone`
source column, and `year()` and `month()` apply to a `date` column as well.
`day()` on a date keeps the exact path above. Each of these buckets spans a range
of source values. So a file whose bucket equals the predicate constant's bucket
is read, not skipped, and the row filter runs on it. A `timestamp with time zone`
value is compared as its UTC instant, matching the catalog. Metrics pruning
covers integer and boolean columns; other column types are read in full.

```sql
CREATE SERVER ice FOREIGN DATA WRAPPER pgcolumnar_iceberg;
CREATE FOREIGN TABLE events (id bigint, region text, amount int)
  SERVER ice OPTIONS (metadata_path '/data/warehouse/db/events/metadata/v3.metadata.json');

-- reads only the region=eu data file; EXPLAIN ANALYZE shows "Files Pruned: 1"
SELECT sum(amount) FROM events WHERE region = 'eu';
```

## Object storage

The Parquet read and export functions, and the foreign-data wrapper, accept an
object-storage URL wherever they accept a local path. These URL schemes are
recognized:

| Scheme | Meaning |
| --- | --- |
| `s3://bucket/key` | An S3 or S3-compatible object. The request is signed with AWS Signature Version 4. |
| `gs://bucket/key` | A Google Cloud Storage object, read and written through the interoperable XML API. It signs with the same Signature Version 4 as `s3://`, so a GCS HMAC key is given as `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`. The endpoint defaults to `https://storage.googleapis.com` and the region to `auto`; set `AWS_ENDPOINT_URL` to override. |
| `http://host[:port]/path` | A plain-HTTP object. The request carries no credential, but the data crosses the network in clear, so use it on a trusted network only. |
| `https://host[:port]/path` | An HTTPS object. Available when the object-store module was built with OpenSSL. The server certificate is verified. |

Requests to an `http://` or `https://` URL are never signed and carry no
credential, for reads and for exports alike. An export sends one `PUT`, or, for a
large object, the multipart `POST` and `PUT` requests and the `DELETE` that aborts a
failed upload. None of them has an `Authorization` header. Use these schemes for a
server that accepts unsigned requests, and `s3://` or `gs://` for a store that needs
credentials. If such a server refuses a request with HTTP 403, the error hint says
the request was unsigned.

Credentials go over plain HTTP only when an `s3://` or `gs://` URL has an `http://`
`AWS_ENDPOINT_URL`. The request is then signed, and the signature, the access key ID
and any `AWS_SESSION_TOKEN` cross the network in clear. The secret access key is
never sent.

S3 requests use path-style addressing (`endpoint/bucket/key`) by default. Set
`pgcolumnar.objstore_s3_addressing` to `virtual` for virtual-host addressing
(`bucket.endpoint/key`), which is what AWS now prefers; the endpoint allow-list
still authorizes the endpoint, not the per-bucket hostname.

Object-storage support lives in a separate module, `pgcolumnar_objstore`, which
loads on the first use of a remote URL and never before. A build or an install
without it reads and writes local files as before, and a remote URL reports that
the module is required.

A remote path is read as an exact key, a prefix, or a pattern. An `s3://` URL
that names an object reads that one object with a single GET, no listing. A
key that holds `*`, `?` or `[` is the exception described below. An
`s3://bucket/prefix/` URL that ends in a slash lists every object under the
prefix, at any depth, like a local directory. An `s3://bucket/prefix/*.parquet`
URL lists the literal prefix and matches the pattern segment by segment, like a
local glob. Listing is a paged ListObjectsV2 call. Below a prefix, `_SUCCESS`,
`_temporary`, and other names that begin with `_` or `.` are skipped, as they are
below a local directory. The prefix you name may itself begin with `_` or `.`,
such as `s3://bucket/_landing/`. A pattern matches names as a local glob does.
It matches a name that begins with `_`. It matches a name that begins with `.`
only when the pattern spells the dot, such as `.*.parquet`. The scheme is
compared without case, so `S3://` reads as `s3://` does. Hive
`partition_columns` work over a remote prefix. A pattern or a prefix requires the
object-store module, since only it can issue the listing.

Only the key of an `s3://` URL, the part after the bucket, can be a pattern. The
scheme and the bucket never are. An `http://` or `https://` URL is always one
object, so a `?` in it starts a query string, which is sent to the server. A key
may itself contain `*`, `?` or `[`, because S3 allows them. When an `s3://` URL
holds one of these characters, the reader lists the prefix and first looks for
an object with exactly that key. If one exists, the reader reads that object
alone, whatever its suffix. Only when none exists is the key matched as a
pattern. A backslash before a character makes the pattern match that character
literally, so `s3://bucket/a/q\?.parquet` matches only `a/q?.parquet`. A read
from a host written as an IPv6 literal in brackets, such as
`http://[::1]:9000/`, is refused with SQLSTATE 0A000.

On write, `parallel_export_parquet` treats a remote URL as a prefix. It writes one
`part-NNNN.parquet` object under the prefix per worker, and an empty `_SUCCESS`
marker beside them on completion. Trailing slashes on the prefix make no
difference: `s3://bucket/out/`, `s3://bucket/out//` and `s3://bucket/out` all
write `out/part-0000.parquet` and `out/_SUCCESS`. The scheme may be written in
any case, so `S3://bucket/out` writes the same keys. Before it writes the marker,
the export deletes any `part-NNNN.parquet` directly under `out/` that a larger
earlier run left behind. It looks only directly under `out/`, so objects under
`outer/`, `out2/` or a deeper prefix such as `out/sub/` are never touched. The prefix must name something under the bucket:
`s3://bucket/` and `s3://bucket` are refused before any worker starts, with
SQLSTATE 22023. The workers use the calling session's values of
`pgcolumnar.objstore_allowed_endpoints`, `pgcolumnar.objstore_s3_addressing` and
`pgcolumnar.objstore_part_size`, including any set with `SET`. The endpoint is
checked against the session's allow-list before any worker starts, so a refused
endpoint fails before anything is uploaded. Every other write function writes a
single object at the exact key.

Remote paths carry the same privilege as local ones. A read needs
`pg_read_server_files` and a write needs `pg_write_server_files`, over and above
any table privilege.

### Endpoints and credentials

The endpoint and the credentials come from the server process environment, from
the catalog, or from both.

The function API (`read_parquet`, `export_parquet`, `export_arrow`,
`import_parquet`, `parquet_schema`) has no server object. Its endpoint and
credentials come from the environment of the server process:

| Variable | Meaning |
| --- | --- |
| `AWS_ENDPOINT_URL` | The object-storage endpoint, `http://...` or `https://...`. Required for an `s3://` URL; optional for `gs://`, which defaults to `https://storage.googleapis.com`. |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | The access key pair. |
| `AWS_SESSION_TOKEN` | A session token, when the credentials are temporary. Optional. |
| `AWS_REGION` or `AWS_DEFAULT_REGION` | The signing region. |

For the foreign-data wrapper the endpoint and credentials come from the catalog,
which keeps them out of a world-readable place. The non-secret settings go on the
server and the secret ones go on a user mapping:

```sql
CREATE SERVER s3 FOREIGN DATA WRAPPER pgcolumnar_parquet
  OPTIONS (endpoint 'https://s3.example.com', region 'us-east-1');

CREATE USER MAPPING FOR analyst SERVER s3
  OPTIONS (access_key_id '...', secret_access_key '...');

CREATE FOREIGN TABLE events (id int, ts timestamp, amount numeric(12,2))
  SERVER s3 OPTIONS (path 's3://reports/events.parquet');
```

The wrapper accepts `endpoint` and `region` only on the server, and
`access_key_id`, `secret_access_key`, `session_token`, and `credentials_required`
only on a user mapping. Any other placement is an error at `CREATE` or `ALTER`
time. `pg_user_mapping` is not world-readable, so a secret placed there is not
exposed the way a server option is.

A read resolves the scanning user's mapping first, then a `PUBLIC` mapping. When a
mapping supplies no credentials, the server process environment is used only in
two cases. The caller is a superuser. Or a superuser has marked the mapping
`credentials_required 'false'`. Ambient credentials are a privilege, not a
default. An ordinary role with no mapping is refused rather than given the server
process identity.

### The endpoint allow-list

`pgcolumnar.objstore_allowed_endpoints` lists the endpoints the module may
connect to. It is empty by default, which refuses every remote endpoint. A role
that can read server files therefore cannot use the extension to reach an
arbitrary host. A superuser sets it to the endpoints the deployment uses:

```sql
ALTER SYSTEM SET pgcolumnar.objstore_allowed_endpoints = 's3.example.com';
SELECT pg_reload_conf();
```

Each entry is a host or a `host:port`. A request whose endpoint matches no entry
is refused before any connection is made. Link-local addresses, including the
cloud instance-metadata address `169.254.169.254`, are refused unconditionally,
whether or not they appear in the list. The setting is superuser-only, so a role
cannot widen its own reach.

### Exporting to object storage

`export_parquet` and `export_arrow` write to an `s3://` URL as well as a local
path. A small object goes in one request. A large one is written as a multipart
upload. It becomes visible at its final name only when the upload completes, so a
reader never sees a partial object. An export that fails before its last request
removes what it wrote, including an incomplete multipart upload. The last request
is different, as the next paragraphs explain.

This cleanup holds when you cancel or terminate the export while it waits for the
store to start the upload. The export then has no id for that upload. It lists
the open uploads for its own key and aborts each one, within 10 seconds. Uploads
at other keys stay open. An upload that another writer started at the same key is
aborted too. This listing works for `s3://` only. A `gs://` export cancelled at
that moment leaves the upload open (#1854).

The last request commits the object. It is the single request of a small object,
or the request that completes a multipart upload. Once the store has that whole
request, a cancel or terminate waits for its answer. The wait ends when the answer
arrives, or when the store sends nothing for 60 seconds. A store that keeps
sending slowly can make it wait longer. If the store accepted the request, the
export issues a warning that it wrote the object. The statement can still end with
the cancel error. The new object is then at the key and has replaced any earlier
object there. A store cannot take a committed object back.

If the connection closes during that wait, the export does not send the request
again. It fails with an error that says the store may have written the object.
Check the key to see whether it holds the new object.

A backend killed with `SIGKILL`, or a lost connection to the store, can still
leave an incomplete multipart upload. To reclaim one, set a bucket lifecycle rule
that expires incomplete multipart uploads after a few days.

Export to object storage is not transactional. An export whose transaction later
rolls back has already written the object. This is true of a local export as
well, and is more visible when the artifact is remote and shared.

## Visibility map inspection

These report the state of the columnar visibility-map fork that serves
index-only scans. They are for diagnostics.

### pgcolumnar.vm_is_visible(rel regclass, blk int)

Tells you if the block (chunk group) has the all-visible mark.

### pgcolumnar.vm_selftest(rel regclass, blk int)

Runs a set and clear self-test against the visibility map for one block.
