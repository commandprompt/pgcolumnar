# Benchmarks

These harnesses ship with the source, not with the distribution. A PGXN
distribution does not carry `bench/`. The harnesses build and install other
database engines to compare against. Clone
<https://github.com/commandprompt/pgcolumnar> to run them.

`bench/` holds three harnesses. Each builds and installs the extension into a
throwaway cluster, loads a dataset, and reports timings:

```sh
BENCH_DUCKDB=1 bench/run_bench.sh /path/to/pg17_nc/bin/pg_config   # main suite
bench/run_bench_fsst.sh          /path/to/pg17_nc/bin/pg_config    # string ingestion
bench/run_bench_readstream.sh    /path/to/pg18_uring/bin/pg_config # read stream and AIO
```

Run them one at a time, on an idle machine, against a **non-assert** PostgreSQL
build. Assertions distort timing, and a concurrent build or test run makes every
figure meaningless.

These environment variables control the harnesses:

- `BENCH_SCALE`, the number of rows. The default is 6000000.
- `BENCH_REPS`, the number of timed repetitions. The harness reports the median.
  The default is 5.
- `BENCH_PORT`.
- `BENCH_DUCKDB`. Set it to 1 to add a DuckDB comparison, if `duckdb` is on
  `PATH`.

The numbers below come from `bench/run_bench.sh`. The storage, query, and mutation
tables were re-run on 2026-08-17 at commit `7c873d5`. They are materially unchanged
from the 2026-08-14 run at `122fd5c`. The main write-path change since then is issue
#5's row-identity lock. It serializes a concurrent `UPDATE` or `DELETE` of the same
row and is on by default. It adds no measurable cost here. The single-row `UPDATE`
is 1.91 ms, unchanged, and that path takes the lock on the one row. An isolated
on-and-off control of that operation, on a narrower table, measured 0.59 ms and
0.63 ms, so the lock adds about 0.04 ms. Spot checks match: heap 707 MB against
columnar-zstd
5.95 MB, and `count(*)` at 0.04 ms. The
conditions were PostgreSQL 18.4 non-assert, 6,000,000 rows, an 8-column table, the
median of 5 repetitions, 16 cores and 62 GB of memory.

The previous record of this section was 2026-08-04 at commit `aeb7882`, on
PostgreSQL 18.4 and an 8-core machine with 12 GB. The harness itself did not change
between the two runs. The machine did, so read the ratios and not the absolute
values.

The read stream section is not re-measured. It needs a PostgreSQL 18 build with
`--with-liburing`, and no such build exists on this machine. Its numbers are from
the earlier run and say so.
The Cross-engine comparison and the parallel sections are a separate, larger run.
It ran on the bench host, at up to 100,000,000 rows, dated 2026-08-04, at commit
`aeb7882`. Each of those sections states its own method.
They show the shape of the trade and not a precise score. The dataset is
synthetic. It mixes column shapes that suit different encodings, and it does this
deliberately. A table of fully random values will therefore look worse, and a
repetitive table will look better.

**Compare the ratios between runs. Do not compare absolute milliseconds.** This
run is on a larger machine than the previous record. It has 16 cores and 62 GB
against 8 and 12. The absolute figures here are therefore larger and different,
and that is expected. Refer to [What changed](#what-changed-since-the-previous-run) for what
moved in the code. Trust the ratios, not the milliseconds.

## Storage

Total relation size, including indexes:

| table | size |
| --- | --- |
| heap | 707 MB |
| columnar (zstd) | 135 MB |
| columnar (none) | 40 MB |

Table-only size, excluding indexes:

| table | size | note |
| --- | --- | --- |
| heap | 579 MB | |
| columnar (none) | 40 MB | encodings, no block codec |
| columnar (zstd) | 5.95 MB | encodings plus zstd |

The `columnar (none)` line has no block compression, so 40 MB against heap's
579 MB is the encoding layer alone: 14.5x. zstd on the already-encoded stream
brings the table to 5.95 MB, 97x smaller than heap. Most of the win is the
encodings; the codec compounds it. Including indexes the gap is narrower, because
the benchmark builds the same btree on both and that index dominates the columnar
total.

## Query latency

Heap versus columnar (zstd), median milliseconds:

| query | heap | columnar | heap / columnar |
| --- | --- | --- | --- |
| count(*) full table | 517.98 | 0.04 | 12950 |
| sum/avg over one int column | 571.25 | 0.96 | 595 |
| filtered agg, min/max-skippable range | 443.74 | 19.75 | 22.5 |
| projection: 3 of 8 cols, 1% filter | 444.16 | 17.90 | 24.8 |
| point lookup by indexed id | 0.03 | 18.17 | 0.00 |

`count(*)` and the ungrouped aggregates are answered from row-group metadata
without decoding column data, which is why they are microseconds rather than
milliseconds.

The point lookup was a regression when the previous version of this page was
written, at 1251.88 ms, and it was reported as
[issue #171](https://github.com/commandprompt/pgcolumnar/issues/171). That issue is
closed. The planner chose a full columnar scan for a point lookup once statistics
existed. It now keeps the index, and the same query takes 18.17 ms.

The filtered aggregate and the projection query also changed by about 8 times.
Column projection is the cause. A columnar scan reads only the columns that a
query references (issue #339).

One point stays true in each case. A single-row fetch must find and decode the
row inside its row group. Columnar storage therefore suits scans and aggregates.
Heap storage suits point lookups and write-heavy OLTP.

## Aggregates fall back once anything is deleted

The metadata answers above hold only while the storage has no delete vector. A zone map covers each row in its group, and this includes the deleted rows. After
a delete, the metadata answer would therefore be incorrect, and the executor uses
a scan instead:

| state | count(*) | sum/avg | min/max |
| --- | --- | --- | --- |
| no deletes | 0.02 ms | 0.32 ms | 0.32 ms |
| after deleting 1 row of 6,000,000 | 0.18 ms | 6.87 ms | 8.44 ms |
| after `pgcolumnar.vacuum` | 0.02 ms | 0.31 ms | 0.31 ms |

The change applies to one row group at a time. A delete therefore costs only the
groups that it touches. The 39 clean groups still fold from their zone maps, and
the executor reads only the group with the delete. This is why `sum/avg` costs
the scan of one group and not of forty. `count(*)` changes very little. The live
count of a group is its row count less its deleted count, and neither figure
needs the data.

This behaviour used to apply to the whole storage. One deleted row then put
`count(*)` at 222 ms and `min/max` at 317 ms, instead of the figures above
([issue #149](https://github.com/commandprompt/pgcolumnar/issues/149), fixed). Vacuuming
still helps, since it returns the dirty group to the clean path.

## Mutation

Rows reached by index, on a 1,000,000-row copy, median of 5 for the updates and a
single run for the delete:

| operation | heap | columnar | columnar / heap |
| --- | --- | --- | --- |
| UPDATE single row by id | 0.03 ms | 1.91 ms | 64 |
| UPDATE 1000 rows, ids in row order | 4.73 ms | 23.21 ms | 4.9 |
| UPDATE 1000 rows, ids scattered | 83.51 ms | 79.38 ms | 0.95 |
| DELETE 1000 rows by id range | 0.8 ms | 4.5 ms | 5.6 |

The columnar cost rises far more slowly than the row count. A single update is
1.91 ms and 1000 scattered updates 79.38 ms, about 40 times the cost for 1000
times the rows. A write to a columnar table marks the old row and appends a new
one, and the row group is the unit of that work. The count of rows that are
reached therefore matters much less than it does for heap. This is why the ratio
against heap falls as the number of rows rises. It goes from 64 times on a single
row to below heap on the scattered batch. Heap pays per row, and columnar pays
per row group.

Read the ratio for the scattered case and not the ratio for one row. A single-row
update is the shape that columnar storage is worst at, and the table says so.

**A note on the previous record of this table.** It reported 0.22 ms for the
single-row update. That figure could not be reproduced on the current machine
with the current build or with the build it was taken at. The two builds were compared directly, on
one machine and one major version, with an equivalent single-row update. Commit
`7a9c9f7` gives 162 ms. The current one gives 19 ms. The mutation path is faster than it was, and the earlier 0.22 ms is not a
baseline that this run failed to meet.

The delete figure was the weakest number in this document, at 1509 ms. At that
time, to reach a row, the code went through each earlier row in the group. Both
halves of [issue #143](https://github.com/commandprompt/pgcolumnar/issues/143) are now
complete. The code decodes the group one time and keeps it in a cache. It then
reaches the value of a row by rank, and not by a walk. Deleting 1000 rows costs 4.5 ms rather than 1509.

## Online maintenance

pgColumnar's online maintenance verbs run under `ShareUpdateExclusiveLock`, so
they do not block readers or writers. Two changes today make them cheap to run on
a schedule. Both were measured on the same non-assert PostgreSQL 18.4 host.

### recluster is a no-op when the clustering is intact (#614)

`recluster` re-establishes a table's Z-order clustering online. Before #614 it
rewrote the whole table on every call, even when the table was already clustered
by that key and nothing had changed. That is the cost a scheduled recluster, or
the maintenance daemon, would pay every sweep. #614 records the clustering key and
makes a redundant call a no-op.

The table below is 10,000,000 rows in 67 chunk groups, a 186 MB relation. It is
clustered once, then `recluster` is called again on the unchanged table:

| build | groups rewritten | time |
| --- | --- | --- |
| before #614 (`bd983d9`) | 67 (all) | 14,750 ms |
| after #614 (self-gate) | 0 | 0.96 ms |

Each figure is the median of three repeat calls. Before #614 every repeat rewrote
all 67 groups and the physical layout digest changed each time. After #614 every
repeat returned 0 and the layout digest was byte-identical. Establishing the order
the first time costs about 21,000 ms either way. The win is that the second and
later calls no longer pay it. This is what lets the maintenance daemon call
`recluster` on a schedule without churning storage.

### The maintenance daemon does not steal foreground throughput (#624)

The `pgcolumnar.autovacuum` daemon runs `compact_rewrite` and `recluster` in the
background, and is off by default. To measure its cost when on, a representative
columnar scan was timed while the daemon actively `compact_rewrite`d a separate
6,000,000-row deleted-heavy table:

| foreground scan (8,000,000-row table) | median |
| --- | --- |
| daemon off | 174.3 ms |
| daemon on, actively maintaining | 173.9 ms |

The scan is unaffected. The daemon completed its maintenance during the window, so
the table was genuinely being rewritten. The daemon also yields to any statement
that needs a stronger lock. `test/autovacuum_yield.sh` proves this with a hard
bound. An `AccessExclusive` request is granted within a fraction of a held
maintenance window, rather than waiting it out.

## Feature toggles

Vectorization on versus off (columnar zstd, median ms):

| query | on | off | speedup |
| --- | --- | --- | --- |
| sum/avg over int | 0.84 | 522.30 | 622 |
| filtered agg (range) | 18.15 | 18.11 | 1.00 |

The "off" column is much faster than in the record before these two runs, at 522 ms
against 1392 ms. Column projection (issue #339) is the reason. The path that does not
vectorize now also reads fewer columns.

Index-only scan on versus off (covering range count, median ms):

| query | on | off | speedup |
| --- | --- | --- | --- |
| covering count, id range (~2%) | 9.25 | 1069.93 | 116 |

The "off" column is the fetch-by-row path with no other work. It therefore
isolates the cost of that path and the effect of #143. This shape was 200.9 s
before the decoded-group cache. It was 31.8 s after the cache. It is 1.07 s now
that the walk to the row is also gone.

Projection scan on versus off (covering scan on a scattered sort key, median ms):

| query | on | off | speedup |
| --- | --- | --- | --- |
| sortk, val where sortk in ~0.1% range | 49.16 | 330.32 | 6.72 |

Sorted storage (`pgcolumnar.vacuum_sorted`), narrow range scan on a key not
correlated with insert order, median ms:

| state | ms |
| --- | --- |
| before vacuum_sorted | 318.70 |
| after vacuum_sorted | 1.75 |

Compression `none` against `zstd`, for the columnar table only: 40 MB against
5.95 MB. The scan latency does not change, at 0.85 ms against 0.87 ms. The
encoded stream is already small, and the aggregates do not read it.

## Parallel bulk ingest

`pgcolumnar.parallel_copy` loads a text file with several background workers at
once. The columnar encode step is CPU bound, so the load speeds up with the worker
count, up to the physical core count. The bench host has 8 physical cores and 16
hardware threads.

Method: PostgreSQL 18.4, non-assert, on the bench with 16 vCPU and 62 GB. The
source file is a 20,000,000-row TSBS cpu slice of 21 columns, sorted by time. Each
figure is the median of three interleaved rounds, with the file warm in the page
cache. The baseline is one server-side `COPY`.

Single columnar table, 20,000,000 rows:

| workers | seconds | speedup |
| --- | --- | --- |
| 1 (COPY) | 129.8 | 1.00x |
| 2 | 67.4 | 1.93x |
| 4 | 36.1 | 3.60x |
| 8 | 20.6 | 6.29x |
| 16 | 18.9 | 6.87x |

One worker matches a plain `COPY` at 130.4 s, so the coordinator and the two-phase
commit add little. The result is the same data every time. All runs load
20,000,000 rows with an identical `sum(usage_user)`. On-disk size varies by 0.03%
across worker counts, because the byte split moves a few stripe boundaries.

A 100,000,000-row load shows the same effect at scale. One `COPY` takes 644.1 s;
`parallel_copy` with 16 workers takes 92.8 s, a 6.94x speedup. Both produce 2.67 GB
on disk, within 0.004%. The row counts match. The float `sum` matches to nine
figures and differs in the last, because parallel summation adds in a different
order.

RANGE-partitioned table, 20,000,000 rows, 24 hourly partitions:

| workers | seconds | speedup |
| --- | --- | --- |
| 1 (COPY) | 134.0 | 1.00x |
| 8 | 29.1 | 4.61x |
| 16 | 25.4 | 5.27x |

The partitioned path routes each row to its partition and gives each worker a
distinct partition set. That routing costs a little more than the single-table
split, so the speedup is lower. It still cuts a two-minute load to under 30
seconds.

## Import and export

Export, 6,000,000 rows, 5 columns:

| format | ms | file size | M rows/s |
| --- | --- | --- | --- |
| arrow | 1226.4 | 186 MB | 4.9 |
| parquet | 1254.3 | 186 MB | 4.8 |

Import, 6,000,000 rows, 5 columns:

| format | ms | M rows/s |
| --- | --- | --- |
| arrow | 4190.4 | 1.4 |
| parquet | 4410.8 | 1.4 |

Import is about 18x slower than export, and the reason is not the import code.
A separate measurement shows this. `import_arrow` costs 12,150 ms. An
`INSERT INTO ... SELECT` of the same rows, with no file, costs 12,990 ms. There
is therefore no overhead that belongs to the import. Reading the whole Parquet file through `read_parquet`
costs 1,415 ms, 11% of the import. The other 89% is the write path, which is also
4.9x slower than a heap insert of the same rows.

So the target is bulk load in general rather than the interop path. Tracked as
[issue #155](https://github.com/commandprompt/pgcolumnar/issues/155) with a plan in
`design/IMPORT_THROUGHPUT_PLAN.md`.

The plan gives the cost to the transposition between rows and columns. Both
readers decode a column-oriented file into per-row values, and the writer then
copies those values back into per-column buffers. The measurements do not support
that as the main term. A single integer column already writes faster than heap. On a text column, the
FSST substring search is the largest part of the write path. To omit that search
makes a load of 1,000,000 rows 1.2x to 5.7x faster. On five of the seven text
shapes measured, it also produced storage that is identical byte for byte. `encode_effort = fast`
(see [Configuration reference](configuration.md#encode-effort)) exposes that
trade per table.

Index maintenance is not in this path at all, which is a correctness bug rather
than a cost: see [issue #153](https://github.com/commandprompt/pgcolumnar/issues/153).

Nested round-trip, 1,000,000 rows, one `int[3]` array column and one composite
column:

| format | export ms | import ms | file size |
| --- | --- | --- | --- |
| arrow | 509.7 | 2889.7 | 38 MB |
| parquet | 455.3 | 3025.6 | 35 MB |

Both reconstructed tables matched the source exactly (zero differing rows).

### Parallel export

`pgcolumnar.parallel_export_parquet(table, dir, workers)` writes a columnar table
to a directory of Parquet files with several background workers at once. Each
worker takes a disjoint set of row groups and writes its own file. There is no
coordinator and no shared write state, so export scales close to linearly with
the worker count.

Method: the bench host, 16 vCPU and 62 GB, PostgreSQL 18.4 non-assert, a
50,000,000-row columnar table of 21 columns, source warm, median of three.

| workers | seconds | speedup |
| --- | --- | --- |
| serial `export_parquet` | 72.1 | 1.00x |
| 1 | 71.9 | 1.00x |
| 2 | 36.6 | 1.97x |
| 4 | 19.4 | 3.72x |
| 8 | 10.2 | 7.07x |

One worker matches the serial writer, so the dispatcher adds nothing. Eight
workers cut a 72-second export to about 10 seconds. Every worker imports the one
snapshot the dispatcher exported, so the files together are a single consistent
image of the table at call time.

## String ingestion (FSST)

3,000,000 rows of URL-like strings:

| | |
| --- | --- |
| heap INSERT | 2.89 s |
| columnar INSERT | 10.31 s |
| heap size | 419 MB |
| columnar size | 101 MB |
| vectors using FSST | 20 of 20 |
| round trip | exact match |

FSST is chosen for every vector of the URL column and gives 4.2x on size, paid for
with 3.6x on ingestion time. That ratio is the reason to optimise the selection of the encoding. It is also
the reason to choose the candidates from a sample, and not to apply each of them.
`pgcolumnar.encoding_sample_rows` controls this. It measured loads that are 1.33x
faster, with output that is identical byte for byte.

## Read stream and asynchronous IO

Cold-scan latency on the PostgreSQL 18 io_uring build, 60,000,000 rows, median of
3, caches dropped between runs:

| io_method | read stream on | off | gain |
| --- | --- | --- | --- |
| `sync` | 40.25 s | 40.24 s | 1.00x |
| `worker` | 39.44 s | 40.13 s | 1.02x |
| `io_uring` | 40.10 s | 39.85 s | 0.99x |

Across methods with the read stream on: `worker` 1.02x against `sync`, `io_uring`
1.00x.

These effects are small, and the correct reading does not change from the
previous run. This workload is not I/O-bound in the way that gives prefetching
the most benefit. The columnar layout already reads a small number of large,
sequential regions. The feature costs nothing and
helps slightly. It is not a headline.

## Cross-engine comparison

The sections above measure pgColumnar against heap on the 6,000,000-row synthetic
suite. This section is a separate, larger run. It compares pgColumnar with heap,
TimescaleDB, and Citus on the TSBS `cpu` workload, at 100,000,000 rows across 21
columns. The data is loaded byte for byte the same way into each engine.

The bench host has 16 vCPU and 62 GB. The comparison engines are built from
source against the same non-assert PostgreSQL. The exact versions and build
flags are pinned by `bench/build_timescaledb.sh` and `bench/build_citus.sh`.
Each engine uses the configuration its own users would choose:

- pgColumnar: columnar scan, storage in load order. One btree on
  `(hostname, time DESC)`. The load order is not globally sorted on either key. It is
  locally ordered on `time`, which is what matters for skipping. See
  [what prunes and what does not](#what-prunes-and-what-does-not).
- TimescaleDB: compressed columnstore, segmented by `hostname`, ordered by `time`
  descending. One btree on `(hostname, time DESC)`, and the `(time DESC)` index that
  `create_hypertable` makes, which gives 53 chunk indexes below them.
- heap: sequential scan with the secondary indexes a heap user would build. One btree
  on `(hostname, time DESC)`.
- Citus: single node, columnar storage. One btree on `(hostname, time DESC)`.

All four therefore carry the same `(hostname, time DESC)` index, and TimescaleDB
carries one more. The index set is given per engine because it decides some of these
rows. q7 on pgColumnar is 767 ms because a skip scan reads that index. The
same query on the un-indexed table in the parallel-scan section below full-scans and
sorts.

The full method follows the storage table.

### Cross-engine storage

Total relation size, including indexes, for the 100,000,000 rows:

| engine | size | smaller than heap |
| --- | --- | --- |
| heap | 22 GB | 1.0x |
| pgColumnar (zstd) | 6,590 MB | 3.4x |
| TimescaleDB columnstore | 7,975 MB | 2.8x |
| Citus columnar | 8,147 MB | 2.8x |

pgColumnar is the smallest of the four.

### Cross-engine query latency

Measured on 2026-08-04 against `main` at commit `aeb7882`, on the 100,000,000-row
fixture. The numbers are the median of five warm runs.

Serial, `max_parallel_workers_per_gather = 0`:

| query | shape | pgColumnar | TimescaleDB | heap | Citus |
| --- | --- | ---: | ---: | ---: | ---: |
| q1 | one host, 1 hour | 403 | 4 | 4 | 272 |
| q2 | one host, 12 hours | 2,211 | 5 | 11 | 1,865 |
| q3 | one host, 12 hours, 5 aggregates | 4,071 | 6 | 13 | 3,365 |
| q4 | all hosts, 12 hours, group by host | 8,210 | 5,119 | 9,713 | 7,364 |
| q5 | all hosts, 12 hours, 10 aggregates | 16,706 | 10,341 | 25,410 | 15,836 |
| q6 | full scan, one value filter | 11,220 | 1,737 | 14,525 | 8,124 |
| q7 | last point per host | 767 | 289 | 46 | 124,537 |
| q8 | top 20 by max | 15,757 | 7,554 | 20,507 | 14,578 |

Parallel, `max_parallel_workers_per_gather = 4`:

| query | pgColumnar | TimescaleDB | heap | Citus |
| --- | ---: | ---: | ---: | ---: |
| q1 | 73 | 4 | 4 | 277 |
| q2 | 500 | 5 | 12 | 1,897 |
| q3 | 875 | 6 | 14 | 3,366 |
| q4 | 8,256 | fails | 9,798 | 7,543 |
| q5 | **11,968** | fails | 28,043 | 16,658 |
| q6 | **2,294** | fails | 3,261 | 8,242 |
| q7 | 766 | 295 | 47 | 125,423 |
| q8 | 15,685 | fails | 20,020 | 14,928 |

Every cell is the median of five warm runs. The widest spread between the fastest and
the slowest of those five, anywhere in either table, is **1.04 times**.

**Spill.** Most cells use no temporary disk at `work_mem = 256MB`. Three do:

| cell | temp blocks read / written | node |
| --- | ---: | --- |
| q7, Citus, serial | 1,022,514 / 1,022,568 | Sort |
| q7, Citus, parallel | 1,022,514 / 1,022,568 | Sort |
| q5, pgColumnar, parallel | 721,569 / 721,584 | GroupAggregate |

The earlier record of this page had five, and two of them were q7 on pgColumnar. Those
are gone because the plan changed. A sort of the whole table became a skip scan over
the index, and a skip scan sorts nothing.

A plan that spills can be unstable between runs, which is why each cell is the median
of five. On this host the spilling cells are not the unstable ones. No cell in either
table has a spread wider than 1.04 times between its fastest and slowest run.

Do not read that as "spill does not matter". It matters at a small `work_mem`, where
the same shape has been measured to swing 1.43 times with every setting held constant.
It says that at this setting, on this host, the sort had enough memory for the spill to
be sequential and cheap.

**Parallel workers are what pgColumnar gains most from.** The scan divides cleanly
across workers. q1, q2 and q3 improve by about five times. q5 and q6 move from behind
heap to ahead of it. The serial table is a measure of the storage format. The parallel
table is closer to what an installation gets.

**TimescaleDB is faster than pgColumnar on every query it completes.** That is the
first thing to take from these tables. In serial it leads by 1.6 times on q4 and q5, and by
2.1 times on q8. It leads by 2.7 times on q7 and 6.5 times on q6. On q1, q2 and q3 it
leads by 101, 442 and 679 times. Against heap, pgColumnar wins q4, q5, q6 and q8 by 20
to 50 percent. It loses q1, q2, q3 and q7.

**pgColumnar is first on q5 and q6 in the parallel table for one reason: TimescaleDB
fails there.** Its parallel arm cannot run on this host. The method above records it.
Where TimescaleDB does run, in serial, it is ahead on both of those queries. Read
those two cells as "faster than heap and Citus", and not as a win over TimescaleDB.

**Why the one-host queries are so far apart.** pgColumnar skips nothing. Measured on
q2, with `EXPLAIN (ANALYZE)`:

```
Columnar Chunk Groups Total: 667
Columnar Chunk Groups Read: 118
Columnar Chunk Groups Removed by Filter: 549
Columnar Vectors Skipped: 40
Rows Removed by Filter: 17295680
```

The two predicates behave differently, and only one of them is the problem. Running each
alone on the same table:

| predicate | groups read | groups removed | time |
| --- | ---: | ---: | ---: |
| `hostname` only | 667 of 667 | 0 | 10,791 ms |
| `time` only | 118 of 667 | 549 | 2,415 ms |
| both, as q2 | 118 of 667 | 549 | 2,238 ms |

**Time pruning already removes 82 percent of the row groups. Hostname removes none.**
The zone maps are working. The gap is a `hostname` clustering failure, not a zone-map
failure.

The catalog says why. Every one of the 667 groups records the same minimum and the same
maximum for `hostname`. Every group holds all 4,000 hosts, so no group can be ruled out.

For `time` the picture is the opposite. A group spans about 6 minutes on average, which
is **0.30 percent** of the table's 2 day 21 hour range.

TimescaleDB answers the same query in 5 milliseconds. It excludes all but one
chunk on time, then reads one `hostname` segment through an index. It is the second half
that we lack, not the first.

### What prunes and what does not

An earlier version of this page argued from `pg_stats.correlation`, which reads 0.0133
for `time` and -0.0036 for `hostname`, and concluded that neither key was ordered. The
conclusion was wrong for `time`, and the instrument was the reason.

`correlation` measures how a column's values track physical row order **across the whole
relation**. Zone-map skipping does not depend on that. It depends on whether each
**group's** minimum and maximum are narrow against the predicate, which is a local
property.

This table is the case that separates them. Its groups are individually tight on `time`,
about 6 minutes each. But the sequence of groups is rotated rather than ascending, with
group 1 beginning at 14:00:10 and group 663 at 13:31:00. A whole-relation correlation is
therefore near zero. Skipping still removes 549 groups of 667.

**So do not infer pruning from `correlation`.** The instrument that answers the question
is `Columnar Chunk Groups Removed by Filter` in `EXPLAIN (ANALYZE)`. Run one predicate at
a time, because a conjunction hides which half is doing the work.

**So this table measures pgColumnar in the layout that suits it least.** A user with
this shape would cluster the table on `hostname`. That is what
TimescaleDB's `segmentby` does for it. That configuration is not measured here. Do not
read the gap on q1, q2 and q3 as a property of columnar storage until it is. The table
does show the layout-independent part. pgColumnar reads fewer columns than heap and
wins the wide scan-bound aggregates. It also stores the same rows in 6,590 MB against
heap's 22 GB.

**q7 was a planner defect and is now fixed.** The earlier record of this page reported
133,759 ms for q7 in serial. The cost model charged an index path for the rows the
path returns, and not for the rows the query reads. A `DISTINCT ON` reads one row per
host. The model therefore priced the index path far above every
alternative. No consumer could recover it, not even one that reads 3,998 rows of
100,000,000. That is
[issue #376](https://github.com/commandprompt/pgcolumnar/issues/376), found by this
benchmark pass and fixed in
[#378](https://github.com/commandprompt/pgcolumnar/pull/378), which bounds the penalty at a
multiple of one scan. The query now takes 767 ms.

Citus is slow on this shape for its own reasons, at 124,537 ms.

#### Queries

```sql
-- q1, q2: one host, 1 hour and 12 hours
SELECT date_trunc('minute',time) m, max(usage_user) FROM cpu
WHERE hostname='host_1' AND time >= '2024-01-01' AND time < '2024-01-01' + interval '1 hour'
GROUP BY 1 ORDER BY 1;

-- q3: as q2 with five aggregates
SELECT date_trunc('minute',time) m, max(usage_user), max(usage_system),
       max(usage_idle), max(usage_nice), max(usage_iowait) FROM cpu
WHERE hostname='host_1' AND time >= '2024-01-01' AND time < '2024-01-01' + interval '12 hours'
GROUP BY 1 ORDER BY 1;

-- q4, q5: all hosts over 12 hours, one metric and ten
SELECT date_trunc('hour',time) h, hostname, avg(usage_user) FROM cpu
WHERE time >= '2024-01-01' AND time < '2024-01-01' + interval '12 hours'
GROUP BY 1,2;

-- q6: full scan, one value filter
SELECT count(*), avg(usage_system) FROM cpu WHERE usage_user > 90.0;

-- q7: last point per host
SELECT DISTINCT ON (hostname) hostname, time, usage_user FROM cpu
ORDER BY hostname, time DESC;

-- q8: top 20 by max
SELECT hostname, max(usage_user) mx FROM cpu GROUP BY 1 ORDER BY mx DESC LIMIT 20;
```

### Parallel scan

The serial table above holds one axis fixed. pgColumnar's columnar scan
parallelizes across workers. The same query shapes, on a 50,000,000-row columnar
table with no index, serial against four workers, warm median milliseconds:

| query | serial | 4 workers | speedup |
| --- | --- | --- | --- |
| q1 | 987 | 255 | 3.9x |
| q2 | 9580 | 1977 | 4.8x |
| q3 | 9570 | 1965 | 4.9x |
| q4 | 15825 | 3387 | 4.7x |
| q5 | 19155 | 4352 | 4.4x |
| q6 | 40724 | 8290 | 4.9x |
| q7 | 91112 | 26301 | 3.5x |
| q8 | 15482 | 3332 | 4.6x |

Four workers give close to four times on every shape. This table has no index. So
q7 full-scans and sorts, and its serial figure is far above the indexed q7 in the
table above. The point here is the speedup within a column, not a comparison with
that run.

### Reading Parquet from other engines

The Parquet that pgColumnar writes is read by other engines without conversion.
Over a 6,000,000-row file, count and sum:

| reader | time |
| --- | --- |
| DuckDB `read_parquet`, stats-accelerated | 12 ms |
| pyarrow `read_table`, full materialization | 149 ms |

DuckDB over the same rows in its own store answers `count(*)` in 1 ms and
`sum/avg` in 4 ms. pgColumnar answers both from catalog metadata, in 0.02 ms and
0.53 ms, because it does not read the column data for those two shapes. On the
shapes that scan, DuckDB leads. Treat this as an order-of-magnitude check, not a
competitive claim.

## What changed since the previous run

The previous record of this page was 2026-08-04 at `aeb7882`. These are the code
deltas between it and this run's `2fe6596` (Merge #592):

- **Late-materialization decode gating shipped (#452, phase 2).** This is the
  trade described in the ClickBench section above. It is a large win on a wide
  `SELECT *` under a highly selective filter (q24 from 6098 ms to 3395 ms with
  gating on). It was a regression on unselective narrow quals, so it moved the
  ClickBench win count from 33 to 25. #598 then gated the gating on projected
  payload width, which recovered those queries and returned the win count to 34.
  Phase 1 also landed, so #452 is no longer open on either half.
- **Detoast once (#587).** A toasted text value is now detoasted a single time per
  scan rather than per reference. This is about 11 percent on queries over toasted
  text.
- **FSST verdict cache (#472).** The FSST encode decision is cached rather than
  recomputed per vector.
- **Bloom filters sized by distinct count (#467).** Filter width now follows the
  column's distinct count instead of a fixed provisioning.
- **`pgcolumnar.parallel_flush` (#445 slice 4).** A measured opt-in, off by
  default. It helps a wide bulk load of many numeric columns by up to about 14
  percent. It regresses text-heavy or small, frequent flushes, because it copies
  buffered data through shared memory. It is not a default.
- **Anchored `LIKE 'prefix%'` now prunes (#510).** Infix `LIKE '%...%'` is handled
  by phase-2 decode gating (#426/#586), not a substring filter. This addresses
  #426 for both the anchored and the infix case.

Re-run on 2026-08-14 at `122fd5c`. The storage, query, and mutation numbers above
are materially unchanged. The code deltas since `2fe6596` are on the maintenance
path and are measured in [Online maintenance](#online-maintenance):

- **`recluster` self-gates (#614).** A redundant `recluster` on an already
  clustered table is a no-op rather than a full rewrite. Measured at 14,750 ms down
  to 0.96 ms on a 10,000,000-row table.
- **The `pgcolumnar.autovacuum` maintenance daemon (#624).** Off by default. It
  runs the online verbs on a schedule and yields to any stronger lock. It does not
  measurably affect foreground scan latency while maintaining.
- **`pgcolumnar.maintenance_due()` (#607).** Reports when a verb is worth running,
  and takes no lock. Not a throughput change; it is the daemon's policy input.

Re-run on 2026-08-17 at `7c873d5`. The storage, query, and mutation numbers above
are materially unchanged. The code deltas since `122fd5c` that touch a measured
path:

- **Issue #5's row-identity lock (#684).** A concurrent `UPDATE` or `DELETE` of the
  same row now serializes on the row. The losing writer gets a retryable
  `serialization_failure` instead of duplicating the row. It is on by default and
  costs nothing measurable on the write path. The single-row `UPDATE` is 1.91 ms,
  unchanged, and that path takes the lock for the one row. An isolated on-and-off
  control of it, on a narrower table, measured 0.59 ms and 0.63 ms with
  `pgcolumnar.enable_row_update_lock` off and on. The lock adds about 0.04 ms. The
  scattered
  1000-row `UPDATE` re-measured at about 70 ms, the same band as before. It is the
  noisiest number here.
- **Iceberg read-path hardening (#685) and cost-model refinements (#679, #681,
  #682).** The Iceberg work is on the Iceberg read path. It does not touch the scan,
  storage, or write paths these tables measure. The cost-model changes adjust
  planner estimates, not runtime scan speed, and did not move these numbers.

## Joins

Every other query on this page reads one table. Star schemas and dimension joins
are a large part of what columnar storage is bought for, so `bench/run_bench_join.sh`
measures them.

```sh
BENCH_SCALE=20000000 bench/run_bench_join.sh /path/to/pg18n/bin/pg_config
```

The numbers below are one run on 2026-08-12. The conditions were PostgreSQL 18.4
non-assert, 16 cores, 20,000,000 fact rows, serial execution and the median of
three repetitions. The fact table has eight metric columns. The dimensions are
heap tables in both arms, which is the realistic deployment.

The data shape is an arm rather than an assumption. `quantised` rounds each metric
to two decimals, which is what an instrument reports. `random` stores raw
`random()`, which has maximum entropy and cannot be compressed. An earlier
measurement of these shapes used `random()` without noticing, and reported a gap
that was much larger than the realistic one.

### Realistic data

| shape | heap | columnar | columnar over heap |
| --- | ---: | ---: | ---: |
| no join, the control | 1,584.7 ms | 601.5 ms | **0.38x** |
| selective dimension join | 1,935.7 ms | 2,538.7 ms | 1.31x |
| unselective join | 3,125.6 ms | 3,621.5 ms | 1.16x |
| multi-dimension star | 6,538.6 ms | 6,774.1 ms | 1.04x |
| wide projection under a join | 2,265.4 ms | 5,015.0 ms | 2.21x |
| total relation size | 2,185,166,848 B | 307,118,080 B | 0.141x |

### Incompressible data

| shape | heap | columnar | columnar over heap |
| --- | ---: | ---: | ---: |
| no join, the control | 1,600.5 ms | 1,972.3 ms | 1.23x |
| selective dimension join | 1,970.3 ms | 4,053.4 ms | 2.06x |
| unselective join | 3,177.2 ms | 5,147.9 ms | 1.62x |
| multi-dimension star | 6,576.2 ms | 8,283.9 ms | 1.26x |
| wide projection under a join | 2,274.9 ms | 16,442.5 ms | 7.23x |
| total relation size | 2,185,166,848 B | 1,306,484,736 B | 0.598x |

### What the control says

Read the first two rows of the first table together. **Without a join we are 2.63
times faster. Add a join and we are 1.31 times slower.** The rows are the same,
the bytes are the same, and the encoding is the same.

The reason is structural. Our vectorized aggregate only sits directly above our
scan. Put a join between the scan and the aggregate and it cannot apply. We
then compete row at a time, against a format built for exactly that. The harness asserts
this rather than describing it. It fails the run if the control is not vectorized,
and it fails the run if the join arm is.

So a join does not cost us through the join itself. It costs us by disabling the
thing we are fast at.

### What the two data shapes say

Compressibility is a second and separate effect. On incompressible data we lose
even with no join at all, and the wide projection shape goes from 2.21x to 7.23x.
Storage goes from 7.1 times smaller to 1.7 times smaller.

Neither effect explains the other. Both are real.

## ClickBench

[ClickBench](https://github.com/ClickHouse/ClickBench/) is a published analytics
benchmark. It is one table of 105 columns and 43 queries. Most of the queries are
a filter, a `GROUP BY` and an `ORDER BY LIMIT`. It is the shape this engine is
built for, which is why it is worth measuring rather than assuming.

Run it with `bench/run_clickbench.sh`. It is optional and it is not in the test
matrix. Nothing downloads until you ask for it.

```sh
PGC_CB_ROWS=10000000 bench/run_clickbench.sh /path/to/pg18n/bin/pg_config
```

The schema and the queries are not stored in this repository. ClickBench is
licensed CC BY-NC-SA 4.0, and NonCommercial and ShareAlike are restrictions this
project's MIT license does not carry. The harness fetches the definition from
upstream at run time and copies nothing. Our comparison oracle is the heap arm of
the same run.

The definition is re-fetched on every run, so the benchmark tracks the current
upstream rather than a copy taken once. That means upstream can change what is
measured between two runs, and a result is only comparable to another result
taken against the same definition. Each run therefore prints the SHA-256 of both
fetched files:

```
   definition: create.sql 42d28575fd59fb4a  queries.sql a7d6673357348ee9
```

Those are the real digests of upstream `main` as of 2026-08-06, 43 queries.

Cite those beside any number taken from a run.

`PGC_CB_OFFLINE=1` runs against the copy already on the machine, for a host with
no network. It says so in the output. A run against a stale definition is not
comparable to one against the current definition.

The table below predates the digest being recorded, so it cannot cite one. The
run was on 2026-08-05, and upstream carried the digests above a day later. That
is an inference and not a measurement. The next run is the first that will state
it.

**These numbers are documentation**, and the NonCommercial term governs their
reuse. Publishing our own test results here is permitted. We measure to compare,
and to catch our own regressions.

Reuse in promotional material, a corporate brochure, a sales deck or a paid
advertisement is not permitted. Producing the numbers ourselves does not change
that. The benchmark definition they came from is the licensed material. The full
list of prohibited uses is in `PROVENANCE.md`, beside the owner's determination
of 2026-08-06.

Two runs are recorded below. The 2026-08-12 run is the current one. It adds a
Citus arm, the parallel loader, a Citus bulk arm, a DuckDB arm, and an accelerated
columnar arm. The 2026-08-05 run is kept because it is a second independent run of
the same benchmark. Both used PostgreSQL 18.4 non-assert, 16 cores, 62 GB of memory,
and 11,110,833 rows. That row count is every ninth row of the real 100 million
row table. The reported time is the best of two hot runs, which is what
ClickBench reports.

### Why the sample is every ninth row and not the first eleven million

`hits.tsv` is ordered. Measured on the real file:

| sample | distinct EventDate | distinct CounterID |
| --- | ---: | ---: |
| first 1,000,000 rows | 1 | 7 |
| every 100th row | 17 | 4,220 |

A prefix does not scale this benchmark down. It replaces it. Every `GROUP BY`
collapses to a handful of groups, every date range hits one day, and the storage
clusters perfectly on the filtered columns. That flatters columnar storage
heavily. The harness therefore samples with a stride, and it fails the run if the
loaded sample is degenerate.

### The 2026-08-12 run: the bulk load is slower than Citus, not faster

This run adds a Citus columnar arm, the parallel loader, and a Citus bulk arm to
compare the parallel loader against. It also adds a DuckDB arm and an accelerated
columnar arm. It is also a run that can cite the definition digest recorded above.
Upstream has not changed since:
`create.sql 42d28575fd59fb4a`, `queries.sql a7d6673357348ee9`.

Conditions: PostgreSQL 18.4 non-assert, 16 cores, 62 GB, 11,110,833 rows, three
tries per query, arms interleaved per query. Citus columnar was co-loaded in the
same cluster, which became possible when the custom scan names stopped colliding.
Every arm loaded all 11,110,833 rows.

| arm | connections | load | total relation size |
| --- | ---: | ---: | ---: |
| heap | 1 | 141.5 s | 7,818,592,256 bytes |
| columnar, serial `COPY` | 1 | 443.4 s | 1,479,745,536 bytes |
| citus columnar, serial `COPY` | 1 | 186.8 s | 1,662,558,208 bytes |
| columnar, `pgcolumnar.parallel_copy` | 16 | 88.8 s | 1,478,057,984 bytes |
| citus columnar, parallel `COPY` | 16 | 49.9 s | 1,663,025,152 bytes |
| duckdb (persistent file) | - | 36.2 s | 3,237,752,832 bytes |

Read the rows in pairs, by connection count. On a single connection columnar is
2.37 times slower to load than Citus (443.4 against 186.8). At sixteen workers
columnar is 1.78 times slower than Citus (88.8 against 49.9), and 1.59 times
faster than heap. Both figures are a property of the loader, not the format.
Comparing the two sixteen worker arms, the stored table is 11.1 percent smaller
than Citus and 5.3 times smaller than heap. On the serial pair the size
difference against Citus is 11.0 percent, so the storage result does not depend
on which loader wrote it. Columnar is the smallest of these on disk: 1.48 GB
against Citus 1.66 GB and DuckDB 3.24 GB.

Our own serial to bulk speedup is 4.99 times against their 3.74 times. That is a
statement about how our loader scales, and not a win over Citus on load time.

An earlier version of this page and of issue #445 reported the parallel loader as
2.01 times faster than Citus on the bulk path. That was wrong, and wrong in sign.
It compared our sixteen worker path against a single Citus `COPY` connection.
Citus accepts concurrent writers into one columnar table and scales well, so the
comparison rested on a premise that had never been checked. Both bulk arms now
split the same file at the same boundaries, using our own
`pgcolumnar.file_split_offsets`, with the same worker count. They differ in
engine and in nothing else.

Query latency, hot times, same run:

| arm | total across 43 queries | geometric mean against heap |
| --- | ---: | ---: |
| heap | 157.6 s | |
| columnar | 129.4 s | 0.49 |
| citus columnar | 260.6 s | 1.86 |

Every figure in the last column is that row's own arm measured against heap, so
the column can be read down. Columnar against Citus is 0.26, which belongs in
this sentence rather than in the Citus row.

Against heap, columnar wins 34 of the 43 queries, loses 7, and ties on 2. A tie
is a query whose two arms are closer to each other than the run to run scatter
of their own repeated tries. This run cannot separate them in either direction.
The two are q34 and q35. Against Citus, columnar wins 39 of 43
with no tie, losing q16, q17, q19 and q33.

> The late-materialization decode gating (#452 phase 2) is a trade applied to unprunable-qual scans.
> It evaluates the qual per 1024-row vector to decide which vectors' payload decode it can skip.
> On the query it is built for, a wide `SELECT *` under a highly selective filter, it is a large win.
> An A/B on this data, gating on versus off, takes q24 from 6098 ms to 3395 ms.
> But on a narrow projection there is no payload to skip, so the per-vector evaluation only adds cost.
> An earlier run of this benchmark, with gating always on, lost eight queries to it.
> They were q11, q12, q14, q15, q25, q27, q31 and q32, each about 1.2 to 2x.
> #598 gates the gating on projected payload width, a plan-time property the unreliable `LIKE '%...%'` estimate cannot corrupt.
> It runs only when the scan materializes at least twenty non-qual columns, which only q24 does here.
> So the eight narrow queries recover their wins and q24 keeps its gain.
> This run measures the gate on, with the win count back at 34: issue #595.

The wide-text losses read wide text under a predicate no min and max can bound.
The cost columnar adds rises with the number of columns the query
materialises:

| query | columns touched | heap | columnar | columnar adds | ratio |
| --- | ---: | ---: | ---: | ---: | ---: |
| q29 | 1 | 8271.0 ms | 8526.2 ms | 255.2 ms | 1.03 |
| q21 | 1 | 771.9 ms | 1386.8 ms | 614.9 ms | 1.80 |
| q22 | 2 | 914.2 ms | 1433.1 ms | 518.9 ms | 1.57 |
| q28 | 2 | 738.9 ms | 1491.5 ms | 752.6 ms | 2.02 |
| q23 | 4 | 913.9 ms | 2014.9 ms | 1101.0 ms | 2.20 |
| q24 | 105 | 777.9 ms | 3376.1 ms | 2598.2 ms | 4.34 |

q24 improved from 6.19x to 4.34x col/heap, which is #452 phase 2 at work. Read
the added milliseconds, not the ratio. The ratio does not order these the same
way, and the reason is the baseline rather than anything about columnar. Queries
q29 and q21 both touch one column, and both add a few hundred milliseconds of
work, 255 ms and 615 ms. But q29 sits on an 8,271 ms baseline and q21 on a 772 ms
one. The same kind of overhead therefore reads as 1.03 on one line and 1.80 on
the next. The added
cost rises with columns touched, with the one and two column groups overlapping.
The ratio column rises only at the extremes.

None of these can be pruned by a minimum and maximum statistic, but not for one
reason. In q21 through q24 the predicate is a `LIKE` with a leading wildcard,
which no min and max can bound. The anchored `LIKE 'prefix%'` case does prune now
(#510), but these are infix. In q28 and q29 the predicate is `URL <> ''` and
`Referer <> ''`. Pruning those is possible in principle, but only for a row group
whose minimum and maximum are both the empty string. That is a group in which
every value is empty. An earlier version of this page said every one of these
predicates had a leading wildcard. Two of them do not. These are the
late-materialisation shapes of issue #452, whose phase 1 and phase 2 both shipped
since the previous run. Infix `LIKE '%...%'` is now handled by the phase-2 decode
gating (#426/#586) rather than a substring pushdown. These are the losses that
gating cannot remove. The q24 win comes from skipping payload. The others, q21
through q23, q28 and q29, have too little payload to skip and must decode the wide
text column itself. The selectivity trade that once cost eight narrow queries is gone,
gated out by #598. None of these is addressed by pushing text predicates down.

Two limits on this table. Every arm was vacuumed and analyzed after loading, so
these are vacuumed state numbers. For a columnar table that is not the normal
state, because autovacuum cannot reach it. The columnar arm also runs with the
analytical accelerators off, which is what a user gets by default.

The accelerated arm (`columnar_tuned`, the analytical accelerators on) sharpens
several results against heap. It brings q2 to 0.13, q16 to 0.39, q17 to 0.39,
q38 to 0.11, and q19 from a loss of 1.05 to 0.84. Turning the accelerators on is
not a clear win everywhere, though, and the 2026-08-05 run below records where it
regresses.

### The 2026-08-05 run: storage and load

| arm | load | total relation size |
| --- | ---: | ---: |
| heap | 141.7 s | 7,818,592,256 bytes |
| columnar | 544.0 s | 1,478,000,640 bytes |

Columnar storage is 5.3 times smaller. It loads 3.8 times slower. Both figures
are part of a published ClickBench result, so both are here.

### Query latency

Columnar is faster on 32 of the 43 queries and slower on 11.

The shapes below are read off the benchmark definition as recorded on
2026-08-06, `queries.sql a7d6673357348ee9`. This run predates that digest being
recorded, so the labels are matched against that definition rather than proved
against the file this particular run fetched. An earlier revision of this page
described six of these ten queries as something other than what they are, which
is #533.

The largest wins, as heap time divided by columnar time:

| query | shape | faster by |
| --- | --- | ---: |
| q1 | `COUNT(*)` over the whole table | 358x |
| q3 | `SUM`, `COUNT` and `AVG`, no `GROUP BY` | 27x |
| q41, q42 | `GROUP BY` two narrow columns, behind a five predicate filter | 25x |
| q7 | `MIN` and `MAX` of a date | 24x |
| q20 | a point lookup, `WHERE UserID = <constant>` | 16x |

The largest losses, as columnar time divided by heap time:

| query | shape | slower by |
| --- | --- | ---: |
| q24 | `SELECT *` of all 105 columns, `URL LIKE '%google%'`, `ORDER BY EventTime LIMIT 10` | 11.6x |
| q23 | `GROUP BY SearchPhrase` with `Title LIKE '%Google%'` and `COUNT(DISTINCT UserID)` | 3.2x |
| q21 | `COUNT(*) WHERE URL LIKE '%google%'` | 2.2x |
| q28 | `GROUP BY CounterID` with `AVG(length(URL))` and a `HAVING` | 2.2x |
| q22 | `GROUP BY SearchPhrase` with `URL LIKE '%google%'` and `MIN(URL)` | 1.8x |

Every loss reads wide text. `q24` selects all 105 columns, so there is no
projection to make. It also sorts a large intermediate to return ten rows. A row
store reads one row and stops, while a column store decodes the chunk groups
that hold the candidates. The other four sit behind a predicate on a wide text
column that no minimum and maximum statistic can prune. That column is therefore
read in full, whatever else the query returns. The 2026-08-12 run above measures
the same effect in more detail.

### The accelerations are off by default, and turning them on is not a clear win

`pgcolumnar.enable_group_vectorization` and
`pgcolumnar.enable_ungrouped_vector_agg` both default to off. About 35 of the 43
queries are `GROUP BY`. So a default run measures this engine with its main
analytical accelerator disabled.

The harness runs a third arm with the analytical accelerators on. That arm sets
four settings, not only the two above. It also sets
`pgcolumnar.enable_parallel_vector_agg` on and raises
`pgcolumnar.groupagg_max_groups` to 200000000.

The result does not support turning these settings on by default:

| query | default | accelerated |
| --- | ---: | ---: |
| q2 | 0.55x | **0.19x** |
| q17 | 0.56x | **0.41x** |
| q19 | 0.98x | **0.84x** |
| q18 | 1.01x | **2.17x** |
| q31 | 0.96x | **1.48x** |
| q15 | 0.84x | **0.92x** |

Three queries improve and three get worse, and the losses are an effect of the
settings rather than noise. For `q18` and `q31` the plan is not identical with
the settings on and off. Measured by `EXPLAIN` on this four-setting arm, each
query runs a seven-worker parallel plan by default. With the settings on, each
runs a serial `Custom Scan` with `Columnar Vectorized Group Keys` instead. The
grouped node is chosen and forecloses the `Gather`, so the query loses its
parallelism. Both queries have a `GROUP BY`, so that node is responsible. The
group counts of 5,727, 18,344, 49,511 and 4,906,030 were measured on other query
shapes, so they do not license excluding the node here. See issue #369.

One query, `q21`, fails outright with the accelerations on. It is
`COUNT(*) WHERE URL LIKE '%google%'`, and it raises
`ERROR: unsupported byval length: -1`. That is issue #423. The harness reports a
failed query and does not drop it.

**Resolved since:** in the 2026-08-12 run above, `q21` no longer errors. It
returns 1399 ms with the accelerations on (`columnar_tuned`) and 1414 ms at
defaults. So #423 has been fixed in the interval. This 2026-08-05
subsection is kept only as the historical record of when it failed.

## What this page does not measure

**Every query on this page reads one table.** The TSBS workload is time-series
shaped. All eight of its queries are a scan, a filter and an aggregate over a
single relation. None of them joins.

The join-heavy analytical shapes are measured separately, in the Joins section
above, on a fixture built for them. That section is the evidence about star
schemas and dimension joins. This section is about the TSBS numbers only.

Joins themselves work. A columnar table joins a heap table in either direction,
and joins another columnar table. Hash, merge and nested-loop strategies all return
the same rows as the all-heap equivalent. Column projection survives the join, so
the columnar side reads only the columns the join and the target list need. What is
absent is a measurement of how fast that is, not evidence that it works.

Read the conclusions below as being about single-table scan and aggregate work,
because that is the evidence behind them.

## Reading the results

Columnar wins on the analytic shapes measured here. Those are aggregates answered
from metadata, filtered aggregates that minimum, maximum and bloom skipping can
prune, wide-table projections, and index-only covering scans. The size reduction comes mostly from
the encoding layer before zstd. Vectorization adds a large further speedup on
aggregates, and storing a table sorted on its range key improves skipping.

Heap is better for single-row fetches and for deletes, and by a large margin in
both. On a table with deletes, the aggregate advantage is not present until a
vacuum runs.
Columnar is the wrong choice for write-heavy OLTP. It is the right choice for
scan-heavy and aggregate-heavy analytics over wide, append-mostly tables, on the
single-table shapes measured here. Whether that holds once a join is involved is
an open question and not a claim this page supports.
