# Best practices

This guide collects the operational advice that the [how-to guides](how-to.md)
apply one task at a time. It is organized by decision. Each section covers one
choice: table design and loading, compression, layout for skipping, maintenance,
query speed, and safe object storage and Iceberg use. Every default and lock level
here is the shipped behavior. The [configuration reference](configuration.md) lists
the settings and their bounds.

## When to choose a columnar table

pgColumnar suits wide, append-mostly, analytical tables. These tables are scanned
by ranges and aggregates far more often than they are updated row by row. It is not
a replacement for a narrow, write-hot, point-lookup table. Use this test: if a query
reads a few columns out of many and touches a range of rows, columnar wins. If a
query fetches whole rows by primary key at high frequency, keep the heap. The
[limitations](limitations.md) page lists the features a columnar table does not
support. Read it before you migrate a table you cannot easily migrate back.

## Design and load a table

**Load in large batches.** The writer packs rows into a row group, the unit of the
write. It divides each row group into chunk groups of up to
`pgcolumnar.chunk_group_row_limit` rows (default 10000). A chunk group is the unit a
scan skips. A column within a chunk group is a chunk, compressed and encoded on its
own. Small transactions produce small, poorly compressed row groups, and many of
them to scan later. Prefer `COPY` or a multi-row `INSERT ... SELECT` over
row-at-a-time inserts. Load in batches that fill a row group
(`pgcolumnar.stripe_row_limit`, default 150000 rows), so each row group compresses
well. Keep `pgcolumnar.stripe_row_limit` at 1024 or above if you lower it. A vector
is a fixed 1024 values. A row group below one vector gets no FSST on its text
columns. Those columns cost 106.4% of their raw bytes at the accepted minimum of
1000, against 54.7% at 1200 (#1017).

**Use `parallel_copy` for a large file.** `pgcolumnar.parallel_copy` splits a
server-side file across workers and scales the load into one table. It runs a
coordinator plus the loader workers, so it needs one worker slot more than the
loader count, in `max_worker_processes` and `max_parallel_workers`. Without the
extra slot, the load moves no rows. The caller needs the `pg_read_server_files`
role.

**Do not over-partition.** Native partitioning multiplies catalog and planning cost.
Use it when a partition key genuinely bounds most queries, not by default. A single
well-clustered columnar table often skips as much as a partitioned heap.

## Choose compression and encoding

**The codec trades ratio against speed.** The block codec is one of `none`, `pglz`,
`lz4`, or `zstd`, and defaults to `zstd`. `zstd` gives the best ratio. `lz4`
decompresses fastest for a scan-bound workload where storage is cheap. `none` and
`pglz` exist for special cases. `pgcolumnar.compression_level` applies to `zstd` and
ranges from 1 to 22, with a default of 3. Raise it for cold, archival data, where a
slower write buys lasting space. Leave it low for tables you rewrite often.

**Let the encoder choose the encoding.** Before the codec runs, each chunk takes the
encoding that makes it smallest. The encodings include dictionary, run-length,
delta, frame-of-reference, delta-of-delta, and FSST for strings. Specialized float
encodings are available too. You do not select these encodings. You feed the encoder
data it can exploit. Sorted or low-cardinality columns encode far smaller, which is
another reason to cluster.

**Spend encode effort where it pays.** `encode_effort` is `full` or `fast`. Leave it
at `full` for the best ratio. Switch a column to `fast` when a text-heavy load is
CPU-bound on encoding. You then accept a slightly larger file for a faster write.
`pgcolumnar.fsst_verdict_reuse` (default 16) reuses the FSST decision for a column
whose character is stable between row groups.

```sql
-- archival table: maximize ratio
SELECT pgcolumnar.set_options('events_cold', compression => 'zstd', compression_level => 19);
-- hot ingest table: favor write speed
SELECT pgcolumnar.set_options('events_hot', compression => 'lz4', encode_effort => 'fast');
```

## Lay the table out for skipping

A scan skips a chunk group when the group's per-column minimum and maximum exclude
the predicate. That test is only as sharp as the data's order. A column whose values
are scattered across every chunk group cannot be pruned. A column whose values are
clustered into a few groups prunes the rest. So the most effective layout choice is
to cluster on the column you filter by ranges. That column is most often a timestamp.
A star-schema join skips on the join key, not on that timestamp.
Cluster the fact table on the join key so matching keys sit in few groups.
A fact table clustered only on a timestamp still holds every join key in every group.
A runtime filter then cannot drop groups.

- `pgcolumnar.recluster(table)` re-establishes the sort order incrementally and
  online. It runs under `ShareUpdateExclusiveLock`, so reads and writes continue. It
  is the daemon-friendly verb, and a no-op on an already-sorted table.
- `pgcolumnar.cluster(table)` does the eager, one-shot reorg under
  `AccessExclusiveLock`. Use it for a first sort or a full rewrite in a maintenance
  window, not on a live hot table.
- `pgcolumnar.sort_status(table)` reports how much of the table is in order. Measure
  before and after a sort rather than guess.

Read `Columnar Chunk Groups Removed by Filter` in `EXPLAIN (ANALYZE)` to confirm
pruning happens. Correlation decides it, not intention. A column you believe is ordered, but that
arrives interleaved, does not prune until you cluster it.

## Keep tables maintained

Deletes and updates leave dead rows in chunk groups. Without maintenance, every scan
still reads and filters those rows. Match the verb to the damage:

| Verb | What it does | Lock |
| --- | --- | --- |
| `pgcolumnar.compact(table)` | retires row groups that are fully deleted | `ShareUpdateExclusiveLock` |
| `pgcolumnar.compact_rewrite(table, min_deleted_fraction, max_groups)` | rewrites partially deleted groups to drop dead rows | `ShareUpdateExclusiveLock` |
| `pgcolumnar.recluster(table)` | restores sort order online | `ShareUpdateExclusiveLock` |
| `pgcolumnar.truncate(table)` | returns reclaimed end blocks to the OS | `ShareUpdateExclusiveLock` plus a brief conditional `AccessExclusiveLock` |
| `pgcolumnar.cluster(table)` / `vacuum_sorted(table)` | eager full reorg | `AccessExclusiveLock` |

**Let the daemon carry the routine load.** Set `pgcolumnar.autovacuum = on`; it is
off by default. A background worker then calls only the two online verbs,
`compact_rewrite` and `recluster`. It never calls the `AccessExclusiveLock` reorgs,
so it does not block a session. It wakes every `pgcolumnar.autovacuum_naptime`
(default 60s). It acts when a table crosses `pgcolumnar.autovacuum_compact_threshold`
(default 0.2, the deleted fraction). It also acts on
`pgcolumnar.autovacuum_recluster_threshold` (default 0.05, the appended fraction).
The worker registers as an autovacuum-kind process, so it yields when a session asks
for a stronger lock.

**Drive a manual cadence from the catalog.** `pgcolumnar.maintenance_due(table)`
reports whether compaction and reclustering are due under the same fractions. A
scheduled job can then skip tables that do not need work.

**Run `truncate` to give disk back.** The online verbs reclaim space inside the
relation. Only `truncate` returns end blocks to the operating system. It holds its
`AccessExclusiveLock` briefly and conditionally. It takes the strong lock only for
the physical shrink, and yields rather than wait. It cannot run inside a transaction
block.

## Make queries fast

- **Project only the columns you need.** Column pruning decodes only the columns a
  narrow `SELECT` names. `SELECT *` decodes everything. On a wide table this is
  the single biggest reduction in decode cost.
- **Late materialization is on by default** (`pgcolumnar.enable_late_materialization`).
  A row the filter rejects does not have its other columns built. Decode cost then
  scales with rows emitted. Leave it on.
- **Per-vector qual gating** skips a no-match vector's payload decode. It only pays
  off once a query projects enough non-filter columns. The width gate
  `pgcolumnar.qual_skipvec_min_payload_cols` (default 20) turns it on for wide
  projections. It turns off for narrow aggregates, and the default suits most
  workloads.
- **Index-only scans are on by default** (`pgcolumnar.enable_index_only_scan`). A
  chunk group answers from the index alone once `VACUUM` marks it all-visible. That
  happens when the group has no deletes and predates the oldest snapshot. Keep
  `VACUUM` current to keep index-only scans effective. Any write clears the bit. The
  executor always re-checks the snapshot, so an index-only answer never returns a row
  you should not see.
- **Read the plan.** `EXPLAIN (ANALYZE)` reports `Files Pruned` for the Iceberg
  wrapper and `Columnar Chunk Groups Removed by Filter` for a local scan. If a range
  query is not pruning,
  the table is not clustered on that column.

## Run object storage safely

Remote access to `s3://`, `http://`, and `https://` endpoints is default-deny.
`pgcolumnar.objstore_allowed_endpoints` is an allow-list, and an empty list allows
nothing. It is a superuser-only setting, so a role cannot widen its own reach. Keep
it as narrow as the deployment allows.

- **Link-local and instance-metadata addresses are refused unconditionally, even
  when listed.** The ranges `169.254.0.0/16` and `fe80::/10` are the
  cloud-credential-theft surface. They have no legitimate object-storage use. You
  cannot rely on the allow-list alone to block them, and you do not need to.
- **Prefer per-role credentials over ambient environment keys.** The object-storage
  foreign-data wrapper takes `access_key_id`, `secret_access_key`, `session_token`,
  and `credentials_required` from a foreign server and user mapping. Each role then
  signs with its own credentials. The secret lives in `pg_user_mapping`, which is not
  world-readable, rather than in a process-wide `AWS_*` environment variable.
- **Leave buffered reads on** (`pgcolumnar.objstore_buffered`). They coalesce a
  column chunk into one request instead of many ranged reads. For a fast link and a
  large export, raise `pgcolumnar.objstore_part_size` above its 8 MiB default.
- **Grant the server-file roles sparingly.** `parallel_copy` and Parquet reads
  require `pg_read_server_files`. `parallel_export_parquet` requires
  `pg_write_server_files`. Both roles also reach remote endpoints through the
  allow-list, so treat them as privileged grants.

## Run Iceberg safely

- **The reader is read-only and delete-correct.** `pgcolumnar.iceberg_scan` reads a
  table at its current snapshot. It applies all three delete kinds: position,
  equality, and format-version 3 deletion vectors. It never returns a deleted row,
  and it
  never writes the table.
- **Use the foreign-data wrapper when you filter.** A bare `iceberg_scan` reads every
  data file. The `pgcolumnar_iceberg` wrapper receives the query predicate. It prunes
  whole files by partition and by metrics before it opens them. The partition
  transforms are identity, `bucket[N]`, `truncate[W]`, and the temporal transforms.
  Pruning is only ever an optimization, so the wrapper returns exactly the rows the
  bare scan would. Prefer it whenever you filter.
- **Keep the REST catalog token off the wire and out of the log.** A per-role bearer
  token belongs on a `pgcolumnar_iceberg_catalog` user mapping. It then lives in
  `pg_user_mapping` and is never a function argument. So it does not appear in the
  statement log or `pg_stat_activity`. For a rotating credential, use OAuth2 client
  credentials on the mapping instead of a static token. The client secret travels
  only in the token-request body. The same allow-list and link-local refusal govern
  every catalog, storage, and OAuth request.

## Verify, do not assume

Two settings exist to check your work rather than trust it. Set
`pgcolumnar.enable_late_materialization` or `pgcolumnar.enable_index_only_scan` to
`off` to compare a query against the optimization. Read `sort_status` and
`maintenance_due` to measure a table's real state before you schedule work. The
optimizations preserve correctness, so any difference you see is performance, not
results.
