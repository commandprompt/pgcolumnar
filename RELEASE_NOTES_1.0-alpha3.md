# pgColumnar 1.0-alpha3 release notes

Release date: 2026-09-02
Previous release: 1.0-alpha2 (2026-08-18)

pgColumnar is a columnar table access method for PostgreSQL. This is the third
alpha. Its theme is retention and skipping. Rows can now expire on a declared
interval. A bulk load can refuse work it has already done. Three more predicate
shapes prune whole chunk groups. The on-disk native format, PGCN v1, is
unchanged. Existing tables are read and written as before.

This release requires one upgrade command. See "Upgrading" at the end.

## Highlights

- **Retention.** `pgcolumnar.expire` drops row groups whose rows are all older
  than an interval you declare on the table. It works on whole row groups, so it
  reclaims space without rewriting live data.
- **Bulk loads can refuse a repeat.** `pgcolumnar.parallel_copy` records a
  fingerprint of each load. A load it has already taken is refused rather than
  duplicated.
- **Three more predicate shapes prune chunk groups.**
  `date_trunc(unit, ts)` now drives skipping in both its range and its equality
  form, and so do `IN (...)` and `= ANY(array)`.
- **The scan tells the planner what order it is in.** A sorted rewrite leaves an
  ordering behind. The scan now reports it, so the planner can skip a sort.
- **The vectorized aggregate takes a wider range of queries**, including a
  target list that itself contains aggregates.

## Retention

`pgcolumnar.set_options` takes `ttl_column` and `ttl_interval`.
`pgcolumnar.expire` then retires every row group whose maximum value in that
column is older than the cutoff. A group is dropped whole. Rows inside the
retention window are never touched.

The interval must be positive. A negative interval would put the cutoff in the
future, which would retire groups whose rows are still current.

## Bulk ingest

`pgcolumnar.parallel_copy` records a fingerprint for each completed load in
`pgcolumnar.load_fingerprint`. Re-running the same load is refused. This makes a
retried ingest safe to repeat after a failure, without a manual check for
partial work.

## Skipping and the planner

A predicate on `date_trunc(unit, ts)` now prunes chunk groups. Both the range
form and the equality form work. `IN (...)` and `= ANY(array)` prune too.

Skip predicates are evaluated most-selective-first, so a group that can be ruled
out cheaply is ruled out first.

The cost model and the zone-map sample now read the row-group geometry a table
was **written** with, rather than the current setting. Changing a setting no
longer reprices existing data.

## Maintenance and reporting

`pgcolumnar.vacuum_sorted()` self-gates. When the relation is already sorted on
the requested key, it does nothing rather than rewriting the table.

`pgcolumnar.sort_status` reports `sorted_kind`, so a reader can tell which kind
of ordering a table carries.

## Correctness fixes

Two defects in this list were silent: the operation looked correct and the data
was wrong. Both were found in the review before this release and both were
reproduced before being changed.

- **A projection created mid-transaction missed every write that followed it**
  (#875). A write before `pgcolumnar.add_projection()` in the same transaction
  left the new projection empty of everything written after it. Measured: 116
  rows in the base table, 105 in the projection, with no error raised. A
  covering projection scan then answered as though those rows did not exist. The
  same defect in `pgcolumnar.drop_projection()` left rows in a projection
  storage whose catalog rows were already deleted.
- **An Arrow import ignored the width, sign and scale the file declared**
  (#881). The importer decoded with the target column's parameters instead of
  the file's. A `uint64` value above 2^63 was stored as a negative number. An
  `int64` file read into an `int` column returned `1,0,2,0` for `1,2,3,4`. A
  `decimal(10,2)` value of 1.25 was stored as 0.0125. A
  `fixed_size_binary(32)` was read as its first 16 bytes. All four imported
  without an error and the wrong values were persisted. They are refused now.

  The `1,0,2,0` is worth recognising if you imported integers. The reader took
  its stride from the target column, so four-byte reads walked an
  eight-byte-per-value buffer. Every second read landed on the high half of a
  small positive number, which is zero. The signature is a real value alternating
  with a zero, not a column of ascending garbage.
- Index entries for live rows are no longer destroyed (#838).
- A scrollable cursor no longer answers a backward fetch with forward rows
  (#842).
- `sum(bigint)` and `avg(bigint)` no longer accumulate across a rescan (#840).
- `date_trunc(unit, ts) = 'infinity'` returns the matching row again (#836).
- An encoded NUL no longer defeats the Iceberg traversal guard (#844).
- `pgcolumnar.set_options` no longer writes past three stack arrays.
- Deleted rows no longer count toward the planner's row estimate.
- A custom scan no longer hides the children of an `INHERITS` parent.
- `TRUNCATE` retires the old storage's catalog rows, and a transaction that
  writes, truncates and writes again keeps the rows it should.
- `ALTER TABLE ... SET ACCESS METHOD heap` drops the catalogs keyed to the
  relation.
- A parallel export refuses a destination too long to hold the names it
  generates.
- `pgcolumnar.compact_rewrite` and `pgcolumnar.maintenance_due` reject `NaN`
  thresholds.

## Known issues

- **A rewrite makes a projection read as absent** (#876). `TRUNCATE`, vacuum and
  recluster mint a new storage id, and the projection rows keep the old one. The
  projection is still declared and its data is intact.
  `pgcolumnar.rebuild_projections()` re-records them. The error message names
  that function.
- **Two visibility-map clears have tests but no verdict** (#877). The clears on
  the recluster and partial-rewrite paths gained coverage in this release.
  Whether a defect sits behind them is not known, and there is no reported
  symptom.

  The rule they implement is not speculative. Its third application, on
  `pgcolumnar.expire`, was a real defect: an index-only scan answered from the
  index for a row group that had been retired. That one is fixed. These two are
  the same rule on two other paths, with no demonstrated symptom on either.

  Of the two issues in this section, **#876 can affect you today** and has a
  recovery command. #877 has no known user-visible effect and is listed so the
  state is on the record, not because there is something to act on.

## Upgrading

Install this build, then run the following in every database that has the
extension:

```sql
ALTER EXTENSION pgcolumnar UPDATE;
```

This is required. The upgrade adds two columns to `pgcolumnar.options` for
retention and creates the `pgcolumnar.load_fingerprint` table. It creates
`pgcolumnar.expire(regclass)`, which is the entry point for the retention
feature above. It replaces four more function definitions:
`pgcolumnar.parallel_copy` and `pgcolumnar.set_options` are dropped and
recreated at a new signature, and `pgcolumnar.maintenance_due` and
`pgcolumnar.sort_status` change in place. No table data is converted and no SQL
you write changes.

See `docs/installation.md` for the commands, including how to list the databases
that need the update.

## Scope and limitations

- This is an alpha. Interfaces may change before 1.0.
- Retention drops whole row groups. A group is retired only when every row in it
  is outside the retention window.
- On PGXN this release is `1.0.0-alpha.3`, while `CREATE EXTENSION` reports
  `1.0-alpha3`. PGXN requires a semantic version, which needs three integer
  components. The extension's own version has two. The two names refer to the
  same release.

The complete, itemized list of changes is in `CHANGELOG.md`.
