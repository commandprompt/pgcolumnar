# Roadmap

What is done, what is planned, and where the detail lives.

This page is the entry point. The working record is
[design/ROADMAP.md](https://github.com/commandprompt/pgcolumnar/blob/main/design/ROADMAP.md),
which carries the full list with per-item history. The release schedule and the test that
gates beta 1 are in
[design/RELEASE_PLAN_1.0.md](https://github.com/commandprompt/pgcolumnar/blob/main/design/RELEASE_PLAN_1.0.md).
Issues are the authority on anything being worked now.

## Status

pgColumnar is [pre-release](limitations.md#release-status). The version marker is
`1.0-alpha4`, recorded in `VERSION`. The
latest published pre-release is `v1.0-alpha4`. A table `USING pgcolumnar` is stored in the
native on-disk format, PGCN v1.

## Releases to 1.0

**An alpha may add features. Beta 1 and every release after it may not.**

That rule decides the schedule. Anything a user can see must land in an alpha.
After beta 1 the only changes are fixes, performance work that adds no surface,
documentation, and tests. A feature deferred past beta 1 waits for 2.0.

| release | target | theme |
| --- | --- | --- |
| `1.0-alpha3` | 2026-09-02 | feature complete: retention, load deduplication, sort reporting, more skipping |
| `1.0-alpha4` | 2026-09-17 | layout, skipping, and join acceleration |
| `1.0-alpha5` | 2026-09-29 | encoding, as measured |
| `1.0-alpha6` | 2026-10-13 | Parquet partition inference |
| `1.0-beta1` | 2026-10-27 | feature freeze |

Alpha4 carries join acceleration because that work finished inside its cycle
rather than waiting for the alpha5 it was planned for. The serial join runtime
filter ships on by default, and per-element pruning of a set predicate ships with
it.

Alpha5 was re-themed on 2026-09-19. It was to carry adaptive cascade selection,
moved up from alpha6. Measuring the candidate chains found two defects worth more
than the chains. An encoding was chosen on pre-codec bytes while the chunk is
stored post-codec. And a column with no nulls still stored a validity bitmap.
Fixing both took a 1,000,000-row ClickBench table from 81,869,112 bytes to
64,984,810. Cascading itself has not met the measured win its own plan demands,
so it leaves the alpha and is tracked on
[issue #1139](https://github.com/commandprompt/pgcolumnar/issues/1139). No date
moves.

Dates are a cadence, not a commitment. They follow the 14 days observed between
`v1.0-alpha` and `v1.0-alpha2`, and each is the date a tag is cut.

Beta 1 is a test rather than a date. It needs a frozen on-disk format and an
upgrade path gated from every shipped version. It needs a green matrix on the tag
and documentation current against the code. And it needs every open performance
issue measured and answered. The plan linked above carries the full test.

## Done

The large pieces that have shipped:

- **Storage and scan.** Native PGCN v1 format, zone maps and bloom filters for skipping,
  column projection, vectorized execution, delete vectors.
- **Interoperability.** Arrow and Parquet, import and export, flat and nested, with no
  libarrow or libparquet dependency. External Parquet read in place, with an FDW surface,
  projection and predicate pushdown, multi-file reads and partition pruning.
- **Maintenance.** Vacuum, compaction, clustering and reclustering, projections. Retention
  through `pgcolumnar.expire`, and an optional background daemon for the online verbs.
- **Object storage.** Reads and writes over S3-compatible endpoints for the Parquet
  functions and both foreign-data wrappers, behind an endpoint allow-list that is empty by
  default.
- **Apache Iceberg.** Read a table at its current snapshot, applying row-level deletes of
  every kind, with an FDW surface and a REST catalog client.
- **Parallel bulk work.** `pgcolumnar.parallel_copy` loads one file with several background
  workers as one atomic operation, and `pgcolumnar.parallel_export_parquet` exports in
  parallel.
- **PostgreSQL integration.** Read stream and asynchronous IO, virtual generated columns,
  temporal constraints, statistics collection for the planner.

## Planned

Where an item falls in the series is in the table above; nothing here is a further
commitment. Each links to the issue that owns it.

| area | item | issue |
| --- | --- | --- |
| Planner | Join and aggregate acceleration, including runtime filters pushed into the scan | [#752](https://github.com/commandprompt/pgcolumnar/issues/752) |

Everything this table listed before has shipped: object storage reads and writes, Apache
Iceberg, the grouped parallel aggregate arm, and the join-heavy benchmark. See Done above.

## Under investigation

Recorded so the work is visible, without implying it will be built:

- Techniques from published columnar systems, ranked against what we already implement.
  See [#403](https://github.com/commandprompt/pgcolumnar/issues/403). Its companion
  reading, [#405](https://github.com/commandprompt/pgcolumnar/issues/405), is closed, and
  so is the PostgreSQL 19 and 20 survey,
  [#390](https://github.com/commandprompt/pgcolumnar/issues/390).

## What this page is not

It is not a commitment. The dates above are the cadence this project has kept, not
a promise. An item here means the work is recorded and reasoned about. It does not
mean anyone is working on it.
