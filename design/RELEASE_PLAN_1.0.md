# Release plan to 1.0

Written 2026-08-29. A proposal, not a decision. It exists so the alpha series has
a stated end and beta 1 has an entry test that can be passed or failed.

## The rule this plan is built on

**An alpha may add features. Beta 1 and everything after it may not.**

That single rule decides the whole schedule. Anything we want in 1.0 that a user
can see must land in an alpha. After beta 1 the only permitted changes are fixes,
performance work that adds no surface, documentation, and tests.

Two consequences worth stating before the schedule.

**A feature we defer past beta 1 is deferred to 2.0, not to beta 2.** There is no
later chance inside this release.

**Work that changes what the writer emits is a feature**, even when no function
signature changes. A new default compression tier and a new encoding selector
both change the bytes on disk, so both belong in an alpha.

## Where we are

| version | tagged | contains |
| --- | --- | --- |
| `v1.0-dev` | yes | the first published marker |
| `v1.0-alpha` | 2026-08-04 | native format, scan, maintenance, Arrow and Parquet |
| `v1.0-alpha2` | 2026-08-18 | object storage, Apache Iceberg, parallel bulk ingest and export |
| `1.0-alpha3` | 2026-09-02 | retention, `parallel_copy` deduplication, `sort_status` reporting `sorted_kind` |

The observed cadence is 14 days, from one tag to the next. The dates below keep
it. They are a cadence, not a commitment, and each is the date the tag is cut
rather than the date work starts.

## What is already done

Read this before proposing anything for an alpha, because the list is longer than
the tracker suggests. Exactly one open issue is a feature.

Storage and scan, maintenance, and projections. Arrow and Parquet in both
directions, and external Parquet with pushdown. Object storage and Apache
Iceberg. Parallel bulk ingest and export. The maintenance daemon and retention.
The PostgreSQL integration points we have taken so far.

`MERGE` also works today and is not documented as working. That is a
documentation gap rather than a feature gap. It is verified rather than assumed:
a `MERGE` with both a matched and an unmatched arm updates and inserts correctly
on a columnar target.

## The alpha series

Each alpha carries one theme so that a slipped item moves a whole release rather
than silently widening one. Effort figures come from `design/ROADMAP.md`, which
sources each to a published paper; they are that document's estimates, not new
ones made here.

### 1.0-alpha3, target 2026-09-01

**Already feature-complete**, and it carries more than an earlier draft of this
document said. That draft named three items. The changelog's `[Unreleased]`
section holds **twelve** entries under Added, because everything since the
`v1.0-alpha2` tag of 2026-08-18 is alpha3 content.

The features:

- retention through `pgcolumnar.expire`;
- `pgcolumnar.parallel_copy` refusing a load it has already taken;
- `pgcolumnar.sort_status` reporting `sorted_kind`;
- chunk-group skipping from a predicate on `date_trunc(unit, ts)`, both the
  range and the equality form, which is #403's preimage item;
- chunk-group skipping from `IN (...)` and `= ANY(array)`;
- the scan reporting to the planner the order a sorted rewrite left behind;
- `pgcolumnar.vacuum_sorted()` self-gating when the relation is already sorted;
- the vectorized aggregate accepting a target list that contains aggregates.

Three further Added entries are test and benchmark work rather than user-visible
features. They are the CDC recipe tested end to end, a suite for the replication
and backup claim, and the cross-engine benchmark arms made reproducible.

The arithmetic is shown, because this section exists to correct a miscount.
Twelve Added entries: nine features, three test work. The eight bullets above
cover the nine. The two `date_trunc` entries, the range form and the equality
form, are one capability in one bullet.

Readiness was checked rather than assumed, on 2026-08-29. `VERSION` and
`pgcolumnar.control` agree at `1.0-alpha3`. The upgrade script from `1.0-alpha2`
ships alongside those from `1.0-dev` and `1.0-alpha`. A `COPT=-Werror` build is
clean with zero warnings. `native_upgrade_converge` is 8 of 8, `docs_style` 9,
and `harness_selftest` 168.

Nothing further should be added. It is 11 days into a 14-day cycle.

### 1.0-alpha4, target 2026-09-15. Theme: skipping and layout

- **Hilbert curve clustering.** Confirmed for 1.0 by the owner on 2026-08-29.
  Z-order ships; Hilbert is the open half and gives better locality on the same
  machinery. It is a new key kind for `cluster` and `recluster`, so it is
  user-visible surface. Under the freeze rule it is this release or 2.0.
- **Per-tier block compression defaults.** On fast local storage, block
  compression can cost more CPU than it saves in I/O. The finding reverses for
  object storage. Make the default depend on the tier. Low effort, and it changes
  written bytes, so it is alpha work by the rule above.

### 1.0-alpha5, target 2026-09-29. Theme: join acceleration

- **Per-element evaluation of a set predicate** (#752). Already specified, with a
  measured ceiling. On a clustered fact table it reaches 9 to 12 chunk groups of
  27, against 25 today. On an unclustered one the ceiling is provably zero. The
  design, the cost bound, the buffer constraint and the negative control are all
  recorded on the issue.
- **Runtime filters from a join's build side**, with clustering on the join key as
  a stated precondition rather than an assumption. The ceiling is zero without it,
  which is measured, so the documentation half of this item is as important as the
  code.

### 1.0-alpha6, target 2026-10-13. Theme: encoding and interoperability

- **Adaptive cascade encoding selection.** The primitives exist; the missing piece
  is a sampling selector that chooses per block. High value at low to medium
  effort, and it changes what the writer emits.
- **Parquet partition inference**, the one remaining item inside Parquet.

Two items rather than three, deliberately. The Arrow C Data Interface export was
cut from this alpha on 2026-08-29. alpha6 is now the last one, and the series
should not lose an item it needs to a slip in October.

## What compressing the series costs

The owner chose on 2026-08-29 to compress and reach beta sooner. That removes the
reserve alpha, and beta 1 moves from 2026-11-10 to 2026-10-27.

The saving is real and so is the price. State the price plainly.

**There is no longer a last chance.** With a reserve, an item that slipped moved
one cycle. Without one, an item that slips past alpha6 is deferred to 2.0,
because beta 1 will not take it. The deferral rule was always there; removing the
reserve is what makes it bite.

**The risk concentrates on the final alpha.** alpha6 carries three items, and it
is now the last one. If the series is going to lose something, it loses it there.

**That concentration was acted on.** The owner cut the Arrow C Data Interface
export from alpha6 on 2026-08-29, moving it to the list below.

It was the least load-bearing of the three. The cascade encoding selector changes
written bytes, so it cannot be added later. Parquet partition inference completes
a format we already ship. Zero-copy Arrow export is a new surface that nothing
else depends on. Cutting it deliberately beat losing it to a slip in October.

The consequence is the freeze rule, stated once more because it now binds a real
item: Arrow C Data Interface export is 2.0 work. It does not return in beta 2.

## Not before beta 1

Named so that proposing one later is a decision rather than a discovery. Each is
either a research direction with no specification, or larger than the remaining
alpha series.

| item | why not |
| --- | --- |
| Morsel-driven parallelism | PostgreSQL's parallel workers are process-based and fixed at plan time; large effort against the grain of the host |
| Data-centric JIT with adaptive execution | large effort, and it competes with core's own JIT |
| FastLanes on-disk format generation | a new format generation, so 2.0 by definition |
| ORC, Delta Lake, Hudi | each is a project the size of the Iceberg work |
| Asynchronous write with background compaction | large, and gated on a measurement that has not been taken |
| Arrow C Data Interface zero-copy export | cut from alpha6 on 2026-08-29 to protect the final cycle; a new surface nothing else depends on |

## Beta 1 entry test

Beta 1 is not a date. It is the first build that passes all of the following, and
the date below is only where the cadence puts it if nothing slips.

**Target 2026-10-27.**

1. **The feature set is declared closed.** Every item in the alpha series above is
   either shipped or explicitly deferred to 2.0, in writing.
2. **The full matrix is green**, PostgreSQL 15 through 19. That includes the
   nightly deep gate and the sanitizer gate. It must be green on the tag itself,
   not on a branch.
3. **The upgrade path is gated from every shipped version.** From `1.0-dev`,
   `1.0-alpha`, `1.0-alpha2`, and every alpha tagged after this document.
   `native_upgrade_converge` must reach the beta catalog from each of them.
4. **The on-disk format is frozen for 1.0.** PGCN v1 and the metapage version are
   final. Any format change after this point is 2.0.
5. **Every open performance issue is dispositioned**, which means measured and
   answered, not necessarily fixed. An issue may close as "measured, and the cost
   is inherent"; it may not remain open and unexplained.
6. **The documentation is current against the code.** The currency audit of
   2026-08-29 is the standard. Every function, GUC and default is cross-checked
   against the source. No figure is published without the conditions that
   reproduce it.

## After beta 1

Fixes, performance work that adds no user-visible surface, documentation, and
tests. Beta releases follow the same 14-day cadence until the entry test for 1.0
is met. That test is the beta test above, plus a period with no new defect of the
kind that would change behaviour.

## What this plan does not claim

It does not claim the dates will hold. It claims three things. The cadence is
observed rather than invented. Every item named is traceable to an issue or to
`design/ROADMAP.md`. And the beta 1 test can be failed, which matters, because a
plan whose entry criteria cannot be failed is a wish.
