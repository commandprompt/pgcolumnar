# pgColumnar documentation

pgColumnar is a column-oriented storage extension for PostgreSQL, implemented as
a table access method. A table created `USING pgcolumnar` stores its data by
column, with per-column compression, chunk-group skipping, and a vectorized
aggregate path. It targets analytic workloads: large scans, aggregates, and
column projections over append-mostly data. It also reads external Parquet and
Apache Iceberg tables, from a local path or from object storage.

pgColumnar builds from one source tree on PostgreSQL 15 through 18, with 19
validated against 19beta4. It is
licensed under the MIT License.

## Where to start

| If you want to | Read |
| --- | --- |
| See what pgColumnar provides | [Features](features.md) |
| Install the extension and load it into a server | [Installation](installation.md) |
| Create columnar tables, load data, and query them | [User guide](user-guide.md) |
| Follow a recipe for one feature, with a tuning note | [How-to guides](how-to.md) |
| Design, compress, lay out, and maintain a table well | [Best practices](best-practices.md) |
| Read Parquet or Apache Iceberg data in place | [User guide](user-guide.md) and [SQL reference](sql-reference.md) |
| Operate columnar tables in production | [Administration](administration.md) |
| Look up a setting and its default | [Configuration reference](configuration.md) |
| Look up a `pgcolumnar.*` function | [SQL reference](sql-reference.md) |
| Check release status, type coverage, and known constraints | [Limitations and compatibility](limitations.md) |
| See size and latency numbers | [Benchmarks](benchmarks.md) |
| Run the test suite | [Testing](testing.md) |

## When to use columnar storage

A columnar table stores each column separately and compresses it. A read that
uses a subset of the columns and scans many rows gets a benefit from this. The
scan reads and decompresses only the columns that the query asks for. The minimum
and maximum of each chunk also let the scan skip the groups of rows that cannot
match a filter.

Use pgColumnar for:

- Fact tables and event logs that are appended to and read with aggregates or
  wide scans.
- Queries that select a few columns from a table with many columns.
- Data that compresses well and is queried more often than it is updated.

Row storage (the default heap) remains the better choice for high-rate single
row updates and deletes, and for point lookups that return whole rows.
pgColumnar supports updates, deletes, and indexes, but its storage layout is
built for append-mostly data. See [Limitations and compatibility](limitations.md).

### When not to use pgColumnar

The clearest case against columnar storage is a table whose bytes are mostly one
large value per row. An independent validation measured this on 2026-08-05, on a
2,000,000 row ledger table. Each row held 1 KiB of incompressible data. The
figures below are theirs.

| operation | heap | pgColumnar | comparison |
| --- | ---: | ---: | --- |
| point lookup by indexed uuid | 1.0 ms | 203 ms | 200x slower |
| ordered 100,000 row full-row segment | 42 ms | 998 ms | 24x slower |
| full-table full-row export scan | 630 ms | 1747 ms | 2.8x slower |
| bulk `INSERT ... SELECT` of 2M rows | 19.9 s | 42.6 s | 2.1x slower |
| table size | 2604 MB | 2051 MB | 1.27x smaller |
| narrow `GROUP BY` aggregate | 266 ms | 115 ms | **2.3x faster** |

Read the last two rows with the others. Columnar storage still favors the narrow
aggregate, its intended case. It gains little on size, because only about a
quarter of the bytes are the kind that compress. Every row-wise operation is slower.

Use heap when most of your bytes are one large value per row. Use heap when point
lookups are the main access pattern. Use pgColumnar when queries read a few
columns out of many.

## How it fits together

A columnar table is an ordinary PostgreSQL relation. It works with transactions,
WAL, replication, indexes, `COPY`, and `pg_dump`. The extension adds:

- A table access method named `pgcolumnar`. New tables are written in the native
  on-disk format, PGCN v1.
- A set of catalog tables and functions in the `pgcolumnar` schema.
- Planner and executor paths for columnar scans, aggregates, index-only scans,
  and projections, controlled by settings under the `pgcolumnar.` prefix.
- Readers and foreign-data wrappers for external Parquet and Apache Iceberg
  tables. They read a local path or object storage (`s3://`, `http://`,
  `https://`), gated by `pgcolumnar.objstore_allowed_endpoints`.
- Online maintenance that runs against a live table (`compact`, `compact_rewrite`,
  `recluster`), and an optional `pgcolumnar.autovacuum` daemon that schedules
  `compact_rewrite` and `recluster`.

## Design and internals

The documents above are for users and administrators. The design and format
specifications are separate:

- [../design/NATIVE_FORMAT_AND_INTERFACE_SPEC.md](https://github.com/commandprompt/pgcolumnar/blob/main/design/NATIVE_FORMAT_AND_INTERFACE_SPEC.md):
  on-disk format and SQL interface specification.
- [ARCHITECTURE.md](ARCHITECTURE.md): source layout and how the pieces connect.
- [../design/ROADMAP.md](https://github.com/commandprompt/pgcolumnar/blob/main/design/ROADMAP.md): completed work and remaining items.
- [../PROVENANCE.md](https://github.com/commandprompt/pgcolumnar/blob/main/PROVENANCE.md): clean-room implementation method.
