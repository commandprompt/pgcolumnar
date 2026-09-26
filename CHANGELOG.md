# Changelog

All notable changes to pgColumnar are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). pgColumnar is
pre-release; the version marker is `1.0-alpha5`, recorded in `VERSION`. New tables
are written in the native on-disk format, PGCN v1. For the forward-looking plan see
[design/ROADMAP.md](design/ROADMAP.md); for full history see the git log.

The extension's `default_version` is `1.0-alpha5`.
`v1.0-alpha4` is the latest published pre-release. Upgrade scripts from
every previously shipped version ship with it (`1.0-dev`, which the v1.0-alpha tag
installed, `1.0-alpha`, `1.0-alpha2`, `1.0-alpha3`, and `1.0-alpha4`), so a single
`ALTER EXTENSION pgcolumnar UPDATE` reaches `1.0-alpha5` from any of them. Older
notes in this file describe `default_version` as pinned at an earlier version, each
true until the next version shipped.

## [Unreleased]

### Changed

- `test/pytest/README.md` now names all four registries a new pytest test has to
  be entered in, and which of them a local run can catch. Adding one test reddens
  CI from `expected_tests.txt`, `TESTS.md`, the ledger plus its budget, and
  `expected_unrunnable.txt` separately, and CI reports them one at a time.
  Measured over one day of two people adding tests: four round trips, each
  failure in a different registry from the one just fixed. The collected count
  collided three times; on the third the arithmetic was wrong, because two
  branches cut from one base both moved the key to 487 and the rebased tree
  collects 488.

  The caveat that needed measuring rather than assuming is on
  `expected_unrunnable.txt`. Running the major where a test declines is not
  sufficient to check it: a prefix built without ICU makes a collation test
  decline where the runner's does not, so the same non-zero exit appears on a
  tree where nothing is wrong. CI named one entry under exit 67 and a local PG17
  run named a different one under the same exit, so the section says to read the
  names in the `unrunnable and not expected to be` list rather than the exit code.

  The same section's example invocation is corrected while it is being added to.
  `--pgc-expect-tests` takes the number of **collected tests**, and the example
  passed 24, which is the number of files `NO_CLUSTER` lists. It now takes `N`
  with a sentence saying which quantity it is, because a literal there names the
  wrong thing and goes stale. Every `python` and `pytest` in the file now carries
  the interpreter path the Prerequisites section builds; the first draft of the
  new block used bare `python`, which is not on PATH in the container and would
  have failed for the reader the section is written for.
  Reported by @OffgridwithJD.

### Fixed

- Planning a query over a columnar relation no longer sequentially scans
  `pgcolumnar.storage` (#1210). `pgcolumnar_written_stripe_row_limit` looked its
  row up by `relation_oid`, which has no index, so the scan walked the catalog
  until it matched. A sequential scan stops at the first match, so the cost was
  the row's **position** rather than the catalog's size: the oldest columnar
  relation never paid for what came after it and the newest paid for all of it,
  unbounded, and the relations a working database is actively querying are the
  recent ones.

  Blocks of `pgcolumnar.storage` per planned query, 2513 rows over 37 pages,
  `EXPLAIN (BUFFERS)` with no execution:

  ```
                   newest relation    oldest relation
    before               37                  5
    after                 4                  4
  ```

  Resolved through the relation's own metapage, which carries its storage id,
  and probed against `storage_pkey` -- a UNIQUE btree over exactly that column.
  Nothing is built and no upgrade script changes.

  **An index on `relation_oid` was the issue's own suggestion and would not have
  fixed it.** The column is not unique: `storage_pkey` is the only unique index
  on the table, and a covering projection gets its own storage row carrying the
  **base** table's `relation_oid`. Measured -- a table written at
  `stripe_row_limit` 150000 with a projection added at 7000 resolved to 150000
  with the base row physically first and 7000 with it second, no value altered
  in between. A non-unique index returns the same ambiguous pair in index order
  instead of heap order, so it would only have made the wrong answer arrive
  faster.

  The fix costs one extra block, the metapage, and cold that is a real read:
  `r=1 h=3` against the old route's `r=0 h=3`.

  It is guarded by `PgColumnarIsColumnarRelation`, two syscache probes.
  `PgColumnarReadMetapage` raises `ERROR` on a version mismatch and block 0 of a
  heap relation is a heap page, so a non-columnar relid would turn a cost
  estimate into a query failure where the old scan simply returned the GUC. No
  non-columnar relid was observed arriving -- 20 arrivals over a corpus holding
  a heap table, a heap partition beside a columnar one, a partitioned parent, a
  view and a matview -- which is not the same as safe.

  **What the ambiguity costs a user.** All three call sites turn the limit into
  a group count that feeds a cost term, so resolving the projection's row
  misprices the plan. Same query, same data, only the base row's physical
  position changed:

  ```
    SELECT a,b FROM t WHERE a BETWEEN 1 AND 30000    2078.84  ->  1134.84
    SELECT b   FROM t WHERE b = 'x7'                 2300.00  ->    91.23
  ```

  Four of nine shapes moved. The ones that did not never reach a
  group-sensitive term, which is why an early reading of two such shapes was
  mistaken for the limit being invisible in the plan.

  `pgcolumnar.analyze()` reads the same ambiguous column in SQL and is **not**
  fixed by this change; see #1276.
- `pgcolumnar.analyze()` no longer skips columns when a covering projection's
  storage row comes first in heap order (#1276). It resolved its storage id with
  `WHERE s.relation_oid = rel`, and `relation_oid` is **not unique** in
  `pgcolumnar.storage` -- `storage_pkey`, on `storage_id`, is the only unique
  index -- because a covering projection gets its own row carrying the **base**
  table's `relation_oid`. `SELECT ... INTO` took whichever row heap order handed
  back first without complaining about the second.

  **The harm was missing statistics, not wrong ones.** The values come from
  reading the column; the storage id is only a gate. A projection narrower than
  its base failed that gate for every column index it did not carry, and
  `CONTINUE` skipped those columns in silence with a successful return:

  ```
    five-column table, one-column covering projection
      base storage covers column_index    0,1,2,3,4
      projection storage covers           0,1

      base row first        columns with statistics: 5
      projection row first  columns with statistics: 2
  ```

  Resolved through the relation's metapage instead, matching the idiom three
  siblings in the same file already hold.

  Two behaviours are now asserted rather than left to be discovered. The
  refusal branch keeps a real case: a never-written columnar table has a
  readable metapage and no storage row, so it is still refused with `P0001`.
  And a materialized view created `WITH DATA` starts succeeding, because its
  `relation_oid` is orphaned until the first `REFRESH`.

  **That second one is not #1275 being fixed.** The orphan is still in the
  catalog and every other reader of `relation_oid` still meets it; only this
  caller stops hitting it.

  Same root cause as #1210, which fixed the C lookup and does not touch this
  one.

  The whole corrected body also goes into
  `pgcolumnar--1.0-alpha4--1.0-alpha5.sql`. `pgcolumnar."analyze"` was last
  defined in `pgcolumnar--1.0-alpha--1.0-alpha2.sql`, a shipped artifact that
  must not change, so an alpha4 install carries that body until an upgrade
  script replaces it. `native_upgrade_converge.sh` refused the base-script-only
  change on all four upgrade paths.

- `run_all_versions.sh` now reports `PASS PG19` on a clean tree (#1270). Three
  `projection_scan_io` rows claimed `15;16;17;18` while their checks run on 19,
  so the ledger gate refused a check it had never seen and reddened the major
  with every suite passing.

  This is the second of that issue's two causes; the entry below is the first.
  Both were live at once, which is why the first diagnosis found only one.

  Regenerated with the command the tool itself prints, one log per gated major
  in a single invocation:

  ```
    5 majors, projection_scan_io, rc=0 and 11 RESULT records each
    majors: uniform, all 1787 rows carry 15;16;17;18;19
  ```

  A row covers only the majors it was merged from, so merging the PG19 log
  alone would have stripped `15;16;17;18` off those three rows.

  **No history lost and no row added.** The one row of the three that carried a
  red observation keeps it, and across the whole ledger nothing changed but the
  majors column:

  ```
    before   majors=[15;16;17;18]     date=[2026-09-21]  mutation=[return 1]
    after    majors=[15;16;17;18;19]  date=[2026-09-21]  mutation=[return 1]

    rows whose (date, mutation) changed: 0
    rows=1787  never=1610  budget=1610   unchanged
  ```

- The ledger orphan scan no longer counts one row in two buckets (#1270). Its
  own integrity assertion was refusing a scan that had found nothing wrong, and
  that refusal reddened PG19 in `run_all_versions.sh`.

  `matched` was `set(rows) & now`, drawn from every ledger row, while the other
  three buckets partition `checkable`. A row the run still emits, whose ledger
  majors do not name the running major, therefore landed in `matched` and in
  `not checked` at once. It is now `checkable & now`, from the same side of that
  line.

  Measured on `pg19-projection_scan_io.log`, the one log that reddened the
  matrix:

  ```
    before   matched 11 + orphans 0 + unprunable 0 + not checked 1766 != 1774   rc=2
    after    parts in the run=1, rows in those parts=8, orphans=0, not checked=1766   rc=0
  ```

  `projection_scan_io` holds eleven rows, three of which claim `15;16;17;18`
  while their checks still run on 19, so a PG19 run computed `11 + 1766 = 1777`
  against 1774 rows. It is the only suite in the ledger with that shape, which is
  why one part on one major reddened the whole matrix.

  **CI could not have caught this.** `ci.yml` runs the suites on 17 and 18, the
  nightly on 15/16/17/18 plus aarch64 18; PG19 suites are local-only by design,
  as `ci.yml` says itself. So the documented local gate, full suites on 18 and
  19, failed on a clean tree and nothing upstream of it could see that.

  The integrity assertion is not the defect. It caught a real classification
  error rather than reporting a clean scan, and the numbers it printed are what
  made this diagnosable in one pass.

  **This does not make PG19 green, and the gate after the fix says so.** A clean
  tree still reports `FAIL PG19`, now for a second and unrelated reason: three
  `projection_scan_io` checks run on 19 while their ledger rows claim only
  `15;16;17;18`, so the ledger gate refuses a check it has never seen. That
  refusal is correct and actionable, it names the three checks and prints the
  `merge` command that fixes it, and it needs one log per gated major. It is
  present on pristine `main` before this change, so it is a separate piece of
  maintenance rather than part of this fix.

### Changed

- A columnar `MATERIALIZED VIEW` may now hold per-table options, and `DROP`
  clears them (#1265). `pgcolumnar.set_options` and `pgcolumnar.reset_options`
  accept one; the drop hook deletes its options row.

  **The three sites move together, and that is the whole point.** Before this
  change `set_options` refused a matview, `reset_options` refused it, and the
  drop hook returned early for anything that was not an ordinary table. Widening
  only the two SQL guards would have let a row be recorded that nothing could
  ever clear. Measured on all five majors before the fix, with an options row
  planted directly on a columnar matview:

  ```
    PG15 PG16 PG17 PG18 PG19    matview rows after DROP   1  (orphan)
    PG15 PG16 PG17 PG18 PG19    control: table after DROP 0  (cleaned)
  ```

  The control is what identifies the hook rather than the guard: an ordinary
  columnar table in the same run cleaned up correctly.

  **The hook tests two relkinds, not `RELKIND_HAS_STORAGE`.** That macro also
  admits indexes, sequences and toast relations, none of which can carry
  pgcolumnar options. A partitioned table stays refused by all three sites: it
  holds no rows of its own, so an option set on it could never be read.

  **`CREATE MATERIALIZED VIEW ... USING pgcolumnar` needs no version floor.** It
  was measured working on 15.18, 16.14, 17.6, 18.4 and 19beta2, with `relkind`
  `m` on the `pgcolumnar` access method in every case. This is unlike a
  partitioned table, which cannot carry an access method before PG17.

  **The upgrade path needed its own fix, and a test caught it rather than a
  reviewer.** `pgcolumnar--1.0-alpha4--1.0-alpha5.sql` did not redefine
  `set_options`, correctly, because the function had not changed since
  `alpha2--alpha3`. Widening only `pgcolumnar--1.0-alpha5.sql` left a fresh
  install accepting a matview while every upgraded install still refused one.
  `native_upgrade_converge` failed on all four upgrade paths with one diverging
  line:

  ```
    116c116
    < FN|pgcolumnar.set_options(...)|2113853fea4ad7a654560b4e1784045e|...
    > FN|pgcolumnar.set_options(...)|44abe8992bafb0acd1e809cdb1b0c3d9|...
  ```

  The upgrade script now carries the function, byte-identical below the `CREATE`
  line, and the four paths converge again.

  **`pgcolumnar.vacuum_full` was deliberately left alone.** It sweeps
  `relkind = 'r'`, so it still skips columnar matviews. Whether it should sweep
  them is a separate question about maintenance, not about options, and
  answering it inside this change would have widened a second behaviour without
  measuring it.

- Eighteen arms across `validity_elision`, selftest part 190 and
  `native_join_vector_agg` now carry a mutation that reddens them (#1236). No
  test changed and no code changed: the arms were attacked and the ledger
  records what happened.

  Six mutations over three parts, each prediction written before it ran:

  ```
    veC    the elision predicate forced true              5 arms
    b190A  pgc_build_needs_clean never asks for a clean    5 arms
    b190B  the stamp writes an escaped literal (#898)      2 arms
    b190C  pgc_build_needs_clean always demands a clean    2 arms
    b190D  unknown provenance reported as a major, PG?     2 arms
    jvaB   the dim-side uniqueness refusal disabled        2 arms
  ```

  **b190A and b190C are opposites and redden disjoint pairs.** A guard that
  never cleans loses the five arms about when a clean is required; a guard that
  always cleans loses exactly two -- "building the same major again needs no
  clean" and "but a tree with no objects at all needs nothing, stamp or not".
  Only those two can tell a correct guard from one that is merely fail-closed,
  and a guard that cleans unconditionally would pass every other arm in the part.

  **Fifteen of the eighteen are newly dated.** The other three are
  `validity_elision` arms already dated under the inverse mutation -- the
  predicate forced *false* rather than *true* -- so they now carry both strings.
  An arm reddened by a predicate in either direction is stronger evidence than
  one reddened in a single direction, and the ledger keeps both rather than
  overwriting.

  Re-derived on the merged tree rather than carried forward. All six had been
  run once before #1240 and #1241 landed, and all six were re-run against
  `ac80762a`, reproducing their earlier results exactly -- same names, not merely
  the same counts, with a control showing the comparison can separate unrelated
  sets. "The check names still exist" and "the mutation still reddens them" are
  different claims, and only re-running tests the second.

  The two C mutations ran with a forced `make clean`: PGXS here has no
  `--enable-depend`, so a stale object after a header edit would leave `veC`
  reddening nothing and reading exactly like a fixture fact.

- Nine `index_am_support` arms now carry a mutation that reddens them (#1236).
  No test changed and no code changed.

  Its only dated rows were two premises about `docs/features.md`, reddened by a
  **docs** mutation, which cannot reach one arm about what an index does. Eight
  behaviour arms sat `never` beside them -- unattacked rather than vacuous, the
  same shape as the first batch.

  Two mutations with disjoint predicted sets, both confirmed exactly:

  ```
    M-1  the build drops two rows in three, so the index misses entries
         predicted the four "answers its operator" arms, NOT the build arms
         observed  exactly those four; CREATE INDEX still succeeds

    M-2  the build errors, so CREATE INDEX fails
         predicted the four "builds an index" arms
         observed  those four, plus the GiST overlap premise
  ```

  **M-2 also shows a weakness in the four answer arms.** With no index in
  existence they all stayed green: the arm compares a count taken with
  `enable_seqscan = off` against one taken with the custom scan off, and
  `enable_seqscan` is a preference rather than a prohibition -- so with no index
  both sides fall back to the same scan and agree. An arm named *answers its
  operator with the same rows the scan returns* can be satisfied without an index
  scan happening. Reported on #1236 rather than repaired here, because this
  change records evidence and alters no test.
- Three suite comments now say what they counted and why they deviate (#1215).

  `arrow_import.sh` explained why it does not use `pgc_skip` and said "THE OTHER
  29 pyarrow SUITES USE" it. **29 was honestly counted and attached to the wrong
  noun.** At `f738dd4d`, the commit that wrote the sentence, 29 suites other than
  that one ran `import pyarrow` -- but the sentence is about the suites that USE
  `pgc_skip`, and 25 did. Measured in a worktree at that commit, excluding
  `arrow_import.sh`, `lib.sh` and the three drivers:

  ```
                                       at f738dd4d   today
      names pyarrow anywhere                34          35
      names pyarrow in CODE                 29          30
      runs `import pyarrow` in code         29          30
      imports it AND calls pgc_skip         25          25
  ```

  **The predicate separating 34 from 29 is comment stripping**, and the five in
  the gap name pyarrow only to say they do not use it -- two hand-craft their
  fixtures, three gate on other capabilities. Same house-style trap as searching
  `arrow_import.sh` for `pgc_skip` and matching the sentence that says why not to
  use it.

  So 29 was honestly counted, of the suites whose *code* names pyarrow. 25 has not
  moved. The count that drifted is the one the sentence did not mean --
  `capability_sweep.sh` joined the population when #1235 landed. All four rows go
  in the comment, with the predicate named on each, because three rows invite the
  reader to guess which "names pyarrow" means -- and guessing differently is how
  two reviewers got different totals for the same tree before reconciling them.

  The recount is also a worked example of the defect it fixes: the first attempt
  returned 26, because searching `arrow_import.sh` for `pgc_skip` matches the
  sentence explaining why it does not call `pgc_skip`. With comments stripped
  that file scores 0.

  `native_parquet_schema.sh` and `parquet_nested_import.sh` decline arm by arm
  instead of being terminal, like `arrow_import.sh`, but unlike it said nothing
  about why -- leaving a reader unable to tell a deliberate deviation from an
  oversight. Both now carry the measurement that justifies it:

  ```
    native_parquet_schema    with pyarrow  35 passed +  0 skipped = 35
                             without       29 passed +  6 skipped = 35
    parquet_nested_import    with pyarrow   7 passed +  0 skipped =  7
                             without        5 passed +  2 skipped =  7
  ```

  Identical name sets either way -- 35 and 7 records in both runs -- and both
  still exit 0. Terminal is right for a suite with nothing else to do, and
  neither is that suite.

  Comments only. No test changed, no check added, no behaviour changed.

- Fifteen `native_join_runtime_filter` arms now carry a mutation that reddens
  them (#1236). No test changed and no code changed: the arms were attacked and
  the ledger records what happened.

  #1236's reading list is the checks that are `never`, non-premise, and sit in a
  part where a sibling IS dated -- arms that survived an attack aimed at their
  neighbours. **Forty-four of the eighty-seven were in this one part**, and the
  reason was visible as soon as it was asked: the only mutation ever aimed there
  flipped the GUC's boot value, which can redden "defaults on" and "has a
  coordinator" and cannot touch a single arm about what the filter does.

  Three mutations, each with its prediction written before it was run:

  ```
    M-A  the bloom match forced true, so the filter never rejects
         predicted the pruning arms and not the answer arms
         observed exactly the three "bloom rejects most non-matches" arms

    M-B  the bloom probe's verdict inverted, so it rejects what matches
         predicted the answer arms
         observed nine answer arms, plus the same three pruning arms

    M-C  the join-type gate widened past JOIN_INNER
         predicted the refusal arms
         observed ANTI, LEFT and SEMI -- and not CROSS, correctly, since the
         gate was not widened to it and a cross join carries no clause to use
  ```

  M-A leaving every answer arm green is the half worth keeping: it says the
  suite can tell "stopped pruning" from "started lying", which is the property
  a filter's tests exist to have.

  **The rows come from `pgc_ledger.py merge` rather than from a script.** The
  first version of them applied a minimal-attribution policy -- each arm recorded
  against the smallest mutation that kills it -- with a script that edited
  `check_ledger.tsv` directly. There is no `minimal` in the tool, because there
  was no `minimal` anywhere except in one head and a note in another file, and a
  policy applied by hand is a human judgement written into the one file whose
  value is that its rows come from runs. Redone with the tool, one merge per
  mutation, `--target` for every arm that reddened. The three pruning arms
  therefore carry BOTH M-A and M-B, which is what the column is for: the
  catalogue of which checks share a cause.

  Twenty-nine arms in that part remain `never`. They are not shown to be vacuous;
  they are unattacked, and they want mutations aimed at plan shape, NULL handling
  and the baseline join rather than at the filter's verdict.

- The parallel covering clamp's comment records what was measured rather than
  "unproven" (#1209). It is reachable, the threshold was predicted before it was
  observed, and the clamp itself decides nothing.

  Probed at the clamp site. Both sides are linear in `seq_page_cost`, because the
  CPU term is not, so three constants fitted from six points predict the crossing:

  ```
    pre = ioRun * projScale = spc * A          A = 3.1
    projRun                 = C + spc * B      B = 3.0   C = 262.5
    binding needs spc > C/(A-B) = 2625
  ```

  Then measured: 2048 does not bind, missing by 0.9%, and 4096 does. When it binds
  the plan changes from `Gather -> Parallel Custom Scan` to a serial
  `Custom Scan (PgColumnarScan)`, which is the consequence #1127 wrote down.

  At the default cost settings it cannot bind, and the reason is a constant rather
  than a property of the fixture: per row, binding needs the projection to save
  more than `5.12 * W + 82` bytes, where `W` is the analyzed width of the columns
  read, because `cpu_operator_cost * W/4` is 5.12 times `seq_page_cost * W/8192`.
  Measured storage runs at 0.13x and 0.04x of `W`.

  A second fixture built to move that margin did not move it. With a blob constant
  across runs of 500 sort-key values, inserted scrambled so only the clustered
  projection sees the runs, `basePagesRead - projPages` came out at 2 pages in both
  fixtures and the threshold landed at 2625 twice. The base compresses the same
  data almost as well as the projection does, so the margin is structural.

  **The clamp changes no plan**, which is worth stating because it reads as though
  it should. Removing it leaves every plan in `projection_parallel.sh` identical
  and the suite green: at the binding point the unclamped total is `startup + pre +
  (projRun - pre)/divisor`, which is LARGER than `startup + projRun`, so the serial
  covering path wins either way. The clamp keeps `cpuRunProj` from going negative,
  an internal quantity no plan exposes.

  The threshold is **not** scale-invariant, which an earlier draft of this entry
  claimed. That claim rested on two fixtures that agreed and were both 20,000
  rows, so their agreement said the margin is insensitive to content and said
  nothing about the row count. Measured on three fixtures at
  `seq_page_cost = 4096`: 20,000 rows binds, 32,000 and 50,000 do not. It grows
  with N, because `cpuRun` scales while `basePagesRead - projPages` stayed at 2
  pages. The arms are safe by margin -- `1000000` is roughly 150x the largest
  threshold observed -- rather than by invariance, and no rung near a crossover is
  asserted.

  Two arms in `projection_parallel.sh` and its pytest twin pin the page-cost
  ladder. They are named for
  I/O amortisation rather than for the clamp, because the clamp-removal mutation
  does not redden them and an arm named for it would have been vacuous. What does
  redden the second is `(ioRunProj + cpuRunProj) / divisor`.

- `build_all_versions.sh` reads the matrix's major list from `run_all_versions.sh`
  instead of carrying its own copy (#1219). The two copies were identical, so the
  rot was latent rather than live -- but a major moving in one and not the other
  means the build check compiles against a set the gate does not use, and
  reporting "builds on every major" about the wrong majors is the failure that
  script exists to prevent, one level up.

  Not hypothetical as a shape: the runner's PG18 is `/usr/local/pgsql`, which is
  not the path a reader guesses, and picking `pg18_nc` by hand instead of reading
  the array cost @jdatcmd four probes on a phantom regression. A person choosing
  majors by hand makes the same mistake a stale copy does.

  A list that could not be READ is now distinguished from a list of zero majors,
  and a malformed array from an absent one. Without the first, a renamed array
  gives `built 0 of 0`, which reads exactly like a host with none of the majors
  installed and sends the reader to their PATH rather than to the array.

  THE CAPTURED BLOCK IS VALIDATED BEFORE IT IS EVALLED (@jdatcmd, review).
  `/^)/` does not match an indented terminator, and the runner's own `SUITES=(`
  closes with a tab, so matching that style is a natural edit rather than a
  hypothetical. With a tabbed `)` the range ran to end of file and `eval`
  executed about 1500 lines of the runner, clobbering this script's variables
  and stopping only when `set -u` hit an unbound name: bounded by accident
  rather than by design. The reader now accepts either terminator and refuses
  any block whose lines are not the opener, a path, or the closer.

  Driven, four ways:

      paren at column 0             built 1 of 1, PASSED
      terminator indented           7 lines captured, built 1 of 1, PASSED
      an assignment smuggled in     refused, 1 line was something else
      array deleted                 refused, no DEFAULT_CONFIGS array found
      array present but empty       refused, no default majors could be read

  and on a host lacking the default paths, `built 0 of 5` with exit 1.

  THE DEFAULT LIST IS ONLY READ WHEN IT IS GOING TO BE USED (@jdatcmd, review).
  Each refusal tells the reader that naming `pg_config` paths as arguments
  bypasses it. That promise was false while the block was read before `$#` was
  examined: a reader with a malformed array who did exactly what the message
  said got the same refusal. A message that sends the reader where the code
  will not take them is this change's own subject, one level in. The read now
  happens only in the branch that uses it, and all three refusals were driven
  again with an explicit `pg_config` argument: smuggled assignment, deleted
  array and empty array each build and report PASSED.

- The arm asserting the build path calls `pgc_build_needs_clean` now strips comments
  before counting, so a comment naming the call cannot satisfy it (#1222). The arm
  has always been named "rather than merely naming it" and relied on a trailing
  quote to tell code from prose. That is not enough in a tree whose house style
  quotes code verbatim in comments. Driven: replacing the call with

      if false; then  # call to pgc_build_needs_clean "$had" "$want" removed

  left the old arm counting 1 -- from the comment -- and reporting yes on a build
  path that consulted nothing. With the fix the same mutation gives
  `1094 passed + 1 failed`, on that arm alone, and the tree restores to 1095.

  A second arm drives the difference directly: counting a file whose only mention
  is a comment must give `raw=1 stripped=0`. Anchoring at `^` is the cheaper fix
  where it fits -- a comment line begins with `#`, so `grep -cE
  '^PGC_EXIT_SKIPPED=66$'` is safe by construction -- but it does not fit a call
  indented inside an `if`.

  No census movement: the existing row leaves `never` with a measured date and the
  new row enters as `never`, so `checks_never_observed_red` stays 1509.

- `projection_scan_io` now covers the third return of `pgcolumnar_projection_pages`,
  which neither harness reached (#1208). The function has three returns and the
  existing arms reach two: the miss (`proj_storage_id = 0`) and the covering arm.
  The third is `if (pages < 1) pages = 1`.

  **It is not the "small projection" case.** `COLUMNAR_PAGE_ROUND_UP` rounds every
  group to a whole page, so one row already gives `pages = 1` through the covering
  return. Probed at all three returns:

      empty table + projection   bytes=0       pages=0   this return
      one row                    bytes=8168    pages=1   covering return
      32000 rows                 bytes=261376  pages=32  covering return

  The state under test is a projection whose storage holds zero row groups. The
  oracle is the plan rather than a cost: priced at one page the covering path is
  chosen, and mutating the line to `pages = 997` drops it from the plan. Proven by
  removal in both harnesses -- 10 passed + 1 failed, the new arm alone, restored to
  11 and 11. Green on PG 15, 16, 17, 18 and 19.
- Three suites now record a skip under each arm's own name when pyarrow is absent,
  so the set of recorded check names no longer depends on the environment (#1185).
  Measured by running every pyarrow-dependent suite twice, with the capability
  present and absent, and comparing the recorded `RESULT` names:

      parquet_export_stats.sh    162 -> 160 passed + 2 skipped = 162
      native_parquet_schema.sh    35 ->  29 passed + 6 skipped =  35
      parquet_nested_import.sh     7 ->   5 passed + 2 skipped =   7

  `parquet_export_stats.sh` recorded nothing at all -- a bare `echo` in the `else`
  -- so it reported `160 passed + 0 failed + 0 unrunnable + 0 skipped` and PASSED
  while both of its `control:` arms had vanished. The other two recorded a single
  summary skip while six and two named arms disappeared, which reconciles the
  accounting and hides the shortfall. Check counts with pyarrow present are
  unchanged at 162, 35 and 7, so no ledger or census movement.

- The sorted-pathkeys planning-buffer arm now takes each reading in a fresh backend,
  so a reading cannot carry catalog-cache state accumulated by earlier tests (#1203).
  `fx` is module-scoped, so the arm was reading two measurements out of a connection
  that had absorbed the DDL of twenty-nine other tests, and the difference between
  them had no upper bound and no fixed sign -- five CI failures, on two arms, in
  opposite directions, including the same commit red then green. **Not reproduced
  locally**: fourteen runs on a quiet box, five of the full cluster corpus, six of
  the module and three with an injected catalog invalidation, all gave a difference
  of exactly 0. This bounds what a reading can carry rather than turning a red green.
  The arm still discriminates: with the `has_useful_pathkeys` guard removed it reads
  `differs by 19 (on=235 off=216)` and fails.

- `docs/limitations.md` now says GIN and BRIN can never be chosen, rather than that
  nothing has been seen to choose them (#1143).

  The old wording left GIN as an open question and suggested the fixture might have
  been too small. It is not a cost problem. Both are bitmap-only access methods, and
  the columnar table access method implements no bitmap-scan callback, so the planner
  generates no path either index could serve.

  Measured on 200,000 rows, same data and same two indexes on both storages, with
  `enable_seqscan`, `enable_indexscan` and `enable_indexonlyscan` off. The only
  difference between the rows is the table access method:

  | storage | GIN plan | BRIN plan |
  | --- | --- | --- |
  | heap | Bitmap Heap Scan | Bitmap Heap Scan |
  | columnar | Seq Scan | Seq Scan |

  The page now also states the maintenance cost, because "never used" understates
  it. A columnar insert touches very few buffers, so any index maintenance is a large
  multiple of it: on 100,000 rows an `INSERT` takes 202 shared hits with no index and
  37,484 with a BRIN index, against 101,468 and 118,923 on heap. The index is essentially
  the same size on both storages -- equal on this fixture, and about 0.5% apart on a
  second run on a different fixture. The work is real and buys an index that cannot
  be chosen.
  Measured by @jdatcmd and reproduced here, whose columnar delta agreed within 0.8%.

  A BRIN index on a columnar table also never summarizes. `brin_summarize_new_values`
  returns 0 where heap returns a count, and `brin_summarize_range` on a range that has
  work raises "columnar: partial-range index build is not supported". The two readings
  look contradictory and are not: a 0 means BRIN found no range and never called into
  the access method, so it is silence rather than success. @jdatcmd found the guard in
  `index_build_range_scan`; the condition that reaches it is reproduced here by naming
  a range that has work.

  **The refusal is consumable, which is worse than the refusal.** The same range
  returns 0 on the next call and the index never grows, so a reader who checks twice
  is told the maintenance function worked. Sweeping ranges 0 to 6 three times gives
  `0 0 E 0 0 0 0`, then all zeros, then all zeros, with the index at 24,576 bytes
  throughout. That is also why neither session could reproduce the other's result by
  re-running the same call: each of us was at a different point in the same
  consumption sequence.

  On PostgreSQL 18 the columnar plan carries `Disabled: true`, which is the planner
  reporting that it used a node it had been told not to use because no alternative
  path existed. A row count cannot change that. BRIN is settled by the same
  measurement, which #1143 records as never having been probed past the build.

  `src/columnar_tableam.c` gains a comment at the access-method routine, because the
  callback set is not the same on every major and a half-implementation fails badly:
  15 to 17 declare `scan_bitmap_next_block` and `scan_bitmap_next_tuple`, 18 removed
  the former, and `table_scan_bitmap_*` calls through the pointer after guarding only
  against logical decoding, so a NULL member is a null function-pointer call rather
  than an error. Reported by @jdatcmd.

### Added

- A nightly sweep that refuses a check vanishing when an optional capability is
  absent (#1185). `test/capability_sweep.sh`, twelve checks.

  A suite that runs fewer checks because a capability is missing reads exactly
  like a suite that has fewer checks. The accounting reconciles what ran against
  what was recorded, so a block that records nothing when it does not run is
  invisible by construction. Measured on `arrow_import.sh` before #1159 fixed it:

  ```
    with pyarrow     72 passed + 0 failed + 0 unrunnable + 0 skipped   PASSED
    without          16 passed + 0 failed + 0 unrunnable + 0 skipped   PASSED
  ```

  **The invariant is not that the name sets must match.** Measured over all 30
  pyarrow suites, 25 decline as a whole with one failure record and nothing else
  runs. That is loud, and it is not this defect. A guard demanding equality would
  refuse 25 correct suites, and a guard that refuses correct code gets switched
  off and takes its rule with it. So: either the absent run records every name the
  present run does, or it records a refusal.

  ```
    A  names preserved, a skip under each arm's own name     5
    B  loud refusal: one FAIL record, nothing else ran      25
    C  SILENT LOSS                                           0
  ```

  Both directions are driven. Putting one suite back into category C gives
  `got [1] want [0]` and names it in the log, while the 25 category-B suites stay
  classified as refusals in the same run -- so the guard is known not to fire on
  correct code as well as known to fire on the defect.

  **Emptying the mask reddens three arms**, which is the one that matters: a
  failed mask makes both runs the present run, every name set matches trivially,
  and the sweep passes having compared nothing. Its pass and its vacuity are
  otherwise indistinguishable.

  It runs as its own nightly job rather than as a registered suite. Two runs of 30
  suites is 224 seconds, a nightly cost rather than one every pull request pays,
  and it would otherwise nest inside a runner that already runs six suites at
  once. It is not behind `PGC_SKIP_TIMING`: `ci.yml` and `nightly.yml` both set
  that, so a guard behind it never runs anywhere.

  Two things it had to learn about itself. It excludes itself from the population
  it sweeps -- it names pyarrow in its own shim, so the first run swept itself, one
  recursive call per suite. And it derives the population with `grep -c` rather
  than `grep -q`, which under `pipefail` closes the pipe on `sed` and drops a
  different file each run: measured at 30, 29, 29, 29, 30 over one unchanging tree.

### Fixed

- A partitioned table carrying `relam = pgcolumnar` no longer aborts the backend
  (#1259).

  `PgColumnarIsColumnarRelation` decided "is this ours?" from `relam` alone.
  From PostgreSQL 17 a partitioned table may carry `relam = pgcolumnar` as the
  default for its future partitions, and it has no storage: `relfilenode` is 0.
  Every caller that took the true branch and then read storage handed
  `smgropen` a zero relfilenumber, which on an assert build aborts the backend
  and restarts the cluster:

  ```
    TRAP: failed Assert("RelFileNumberIsValid(rlocator.relNumber)"), smgr.c:204
    LOG:  server process was terminated by signal 6: Aborted
  ```

  On a production build it does not crash, it answers wrong: `get_storage_id`
  returns NULL and `stats` and `sort_status` build on that.

  The predicate now also requires storage. **`RELKIND_HAS_STORAGE` rather than
  `== RELKIND_RELATION`, and the difference is load-bearing:** a materialized
  view can be columnar -- measured, `relkind` `m`, `relam` `pgcolumnar`, rows
  readable -- and narrowing to `RELKIND_RELATION` would have stopped
  recognising one at all 31 call sites, silently. A test arm drives exactly that
  wrong fix.

  All 31 call sites were classified before the change: none legitimately wants
  the partitioned parent. `set_options` refuses it and `add_projection` aborted
  on it, so the two catalog renames in `pgcolumnar_process_utility` have nothing
  to maintain there.

- The ANALYZE cap's input guard defends a class, and the comment named one
  member of it (#1252).

  The first version said a partially numeric token was "the one input it
  actually defends". The guard refuses everything bash will not read as an
  int64, and the other member is the dangerous one:

  ```
    pgc_analyze_cap_ms 12abc
      guarded 5000   unguarded <arithmetic error>
    pgc_analyze_cap_ms 99999999999999999999
      guarded 5000   unguarded 7751640039368425452
  ```

  `$(( ))` wraps silently and returns a positive number past the floor, so the
  unguarded function yields a cap that can never fail. An arithmetic error is
  loud; a bound that always passes looks like a green test. An arm now covers
  the overflow member, and the sentence that would have justified deleting the
  guard is gone.

  The dead `c > 0` conjunct in the same arm is removed by the same rule: it
  cannot fire, because the cap is never below its floor, and a condition that
  cannot be covered gets removed.

  Both named by @jdatcmd on #1258.

- `analyze_stats`' wide-table bound is derived from the run and floored, instead
  of being a ratio over a 20 ms scan that was mostly process startup (#1252).

  The arm read `an_ms < scan_ms * 20`. It went red on a CI runner against a
  branch of five test and docs files that cannot reach the ANALYZE path, and
  passed when the same jobs were re-run at the same commit.

  Measured on `pgcolumnar-audit`, pg17a, five interleaved rounds, with the same
  `psql_run` wrapper around `SELECT 1` as the control:

  ```
    empty  7.8 ms    insert 316.6 ms    scan 20.0 ms    analyze 115.2 ms
  ```

  The scan carried about 12 ms of work above an 8 ms floor -- 1.5x its own noise
  -- so the ratio measured the floor as much as the scan. The two terms did not
  move together either: on the failing CI run ANALYZE was 3.1x this box while
  the scan was 0.7x it.

  The bound is now `max(5000, insert_ms * 20)`. The insert reads and writes the
  same rows the ANALYZE samples, so it tracks the machine and the data volume;
  the floor stops a future faster write path squeezing the bound into a flake,
  which is how the old one arrived here -- its 20x was justified against "341 ms
  against a 155 ms scan" and no machine has measured a 155 ms scan since.

  The bug being defended is a hang, not a slowdown, so the bound does not need
  to be tight. Measured by removal against `f82bdcd`'s parent, same fixture:
  the unfixed build did not complete within 180 s, and an unbounded run was
  killed at 10m52s of 99.9% CPU still running, against 206 ms fixed.

  The decision is a function driven with literals, so the branch a healthy
  machine never takes is judged rather than left to a box that happens to be
  slow. `analyze_stats` also enters the mutation ledger for the first time:
  `suites_not_covered` falls from 249 to 248.

- The hand-rebuild diagnosis can no longer be silent, which is how the first
  version of it still told the 2026-09-24 nightly nothing (#1248).

  #1249 added `_hr_diagnose` to print `rebuild.log`'s tail when the rebuild
  fails. The nightly of the same day failed on aarch64 again, with the gate
  working -- the three dependent arms skipped with a reason instead of cascading
  -- and **the diagnosis printing nothing at all**, because `rebuild.log` was
  empty and the function opened with

  ```sh
    [ -s "${1:-}" ] || return 0
  ```

  So the one path the diagnosis exists for was the one path it stayed quiet on.

  `_hr_diagnose` now always says something, and distinguishes the two causes,
  because they are different failures and merging them costs the next reader the
  night:

  ```
    ---- rebuild.log, last 20 lines ----
    ---- rebuild.log is EMPTY (0 bytes): rebuild.sh exited
         before its first echo ----
    ---- rebuild.log is MISSING: the redirect never created it ----
  ```

  Two arms of selftest part 580 asserted the old silence and are replaced rather
  than adjusted. They were provably wrong: `an empty log produces nothing rather
  than an empty banner` and `and a log that is not there produces nothing rather
  than an error` required exactly the behaviour that defeated the diagnosis on
  aarch64. Four arms now require it to speak, and to name which of the two cases
  it found.

  **What the old diagnosis could not tell anyone, and this one can.** `[ -s ]`
  is false for an empty file AND for one that is not there, so a silent
  `_hr_diagnose` never distinguished them -- and neither did the first draft of
  this entry, which asserted the log was empty when the only evidence was that
  `-s` failed. The two have different causes and only one of them fits:

  ```
    EMPTY    rebuild.sh ran and wrote nothing before dying
    MISSING  the redirect could not open its target, so bash returned 1 and
             never ran the command at all
  ```

  Every exit in `rebuild.sh` before its first unconditional `echo` writes to
  stderr first, and stderr is inside the same redirect, so EMPTY is not
  something the script's control flow produces. MISSING is: measured, a failing
  redirect returns 1, creates no file, and reports on the caller's stderr
  rather than into the log. That matches `got [1]` and a silent diagnosis
  exactly. Caught by @jdatcmd.

  This does not explain why the aarch64 rebuild fails. It makes the next nightly
  say which of the two it is.
- Selftest part 540 could not see the shell `if/then/else` verdict, so five live
  arms discarded their operands untracked (#1255).

  540 refuses an arm whose failure cannot say what it measured. Both of its
  matchers required the `&&` spelling, so this passed through:

  ```sh
    "$(if [ "$sj_idx" -ge 2 ]; then echo 1; else echo 0; fi)"
  ```

  Both branches are constants, `sj_idx` is discarded, and the arm renders
  `got [0] want [1]` -- the symptom #1164 is named for. 12 raw matches,
  7 excused
  by 540's own `determinate()` carve-outs, five in scope and none tracked.
  The
  gaps 540 already records are all awk and all say "zero are lossy today".

  Three of the five are repaired here; `catalog_plan_index.sh`'s two were
  repaired in #1254. `lossy_arms.tsv` therefore does not move: 101 before, 101
  after.

  Two further matcher defects, found together and each inert alone:

  - The `[ ... ] && echo` rule had no same-line recorder conjunct, though the
    awk rule beside it does and the comment above that one explains why. It used
    `name`, the last name seen, so a helper was charged to whichever arm
    preceded it.
  - Whole-line comments were read as code, so a comment **quoting** a recorder
    call was swept as an instance of it, under a name that exists nowhere.

  Comment skipping is whole-line only, and deliberately not `sub(/#.*/, "")`:
  214 check names in this corpus contain a `#`, so stripping from the first one
  truncates the name the sweep reports and trades a false positive for a corpus
  of mangled rows.

  The lifted sweep was validated against the shipped list before being used for
  any of this -- 101 found, 101 tracked, both `comm` directions empty.

- A failed hand rebuild now says why, and stops the arms beneath it reporting on
  a rebuild that did not happen (#1248).

  The 2026-09-24 nightly failed on aarch64 with the whole diagnosis being

  ```
    FAIL  premise: the hand rebuild itself succeeded: got [1] want [0]
  ```

  and nothing else. `rebuild.sh`'s output goes to a file the part deletes on the
  way out, so the cause left with the workdir. **The three arms below that
  premise then ran anyway and reported PASS.**

  They were not vacuous that night, and only by luck: `rebuild.sh` writes both
  stamps at step 3b and the unresolved-symbol check that rejected the build comes
  after, so the stamps the three arms read had in fact been written. Had the
  *build* failed, the same three arms would have read an absent stamp. That is
  what "the premise does not gate" costs.

  Both decisions are now functions the part drives directly, the way part 190
  drives `pgc_build_needs_clean`, rather than inline branches that only a broken
  machine exercises:

  ```
    _hr_dependents RC   ->  run | skip
    _hr_diagnose  LOG   ->  the last 20 lines, indented, or nothing
  ```

  Driven with `rebuild.sh` forced to exit 1 *after* its stamp, reproducing the
  aarch64 shape exactly:

  ```
    FAIL   premise: the hand rebuild itself succeeded
    SKIP   and the freshness gate then reads the tree as built from this source
    SKIP   and it reads a different source as stale rather than fresh
    SKIP   and the installed library of the running major was never touched
    the diagnosis is printed, and carries the failing line rather than a status
  ```

  **This does not fix the aarch64 failure and does not guess at it.** The
  hypothesis is GCC's outline-atomic helpers (`__aarch64_*`) resolving from
  libgcc and falling outside the `ldd` reference set the symbol check builds, and
  there is no aarch64 box here to test it on. What this change does is make the
  next nightly report the symbol names instead of a bare exit status, so the fix
  after it is measured rather than reasoned about.
- The cross-major preflight leaves no object tree behind, on a box with no
  `pg_config` on `PATH` (#1219).

  `test/build_all_versions.sh` ends with a clean, and its comment says why:
  "Leave no object tree behind from whichever major happened to be last: the
  next build against a different major would link objects compiled for this
  one." It was written as

  ```sh
  make -C "$SRCDIR" clean >/dev/null 2>&1 || true
  ```

  with no `PG_CONFIG` and its failure swallowed. PGXS resolves `pg_config` from
  `PATH`, so on a box without a packaged one that `make` fails, the `|| true`
  eats it, and the tree keeps the last major's objects while the script prints
  PASSED. Measured, same tree, pg18a then pg19a:

  ```
    normal PATH              built 2 of 2  PASSED   objects left:  0
    PATH with no pg_config   built 2 of 2  PASSED   objects left: 36
  ```

  **Passing a `pg_config` is not the fix.** `make clean` needs PGXS loaded to do
  anything, objects live in `src/` *and* `objstore/`, and a clean whose failure
  is swallowed cannot be told from one that worked. The sweep now removes by
  `find` -- needing no `pg_config` -- and then verifies, through two functions the
  selftest drives directly:

  ```
    pgc_bav_tree_has_objects DIR          -> yes | no
    pgc_bav_clean_tree SRCDIR [PG_CONFIG] -> clean | dirty
  ```

  End to end, the real script under `env -i PATH=<no pg_config>` now leaves
  **0 objects** where it left 36.

  The consequence was bounded -- #1221's DWARF provenance catches foreign objects
  on the next suite and cleans them -- so this cost a rebuild rather than a wrong
  install. What it did not cost is nothing, and the comment promised something it
  did not always do.
- `ALTER TABLE ... ALTER COLUMN TYPE` left `pgcolumnar.storage.relation_oid` pointing at the dropped rewrite relation (#1211).

  The flush records the transient relation `make_new_heap` builds. The swap keeps the user's OID and drops the transient, so the one lookup keyed on `relation_oid` finds nothing. The rows stay readable. Measured on PG18 before the change: the storage row for `pgcolumnar.get_storage_id` compared equal to the live regclass was `0` after `ALTER COLUMN id TYPE bigint`, and `1` before it. A table that was not rewritten stayed `1`.

  After the statement, the storage row for the id now on the metapage is retargeted at the surviving OID when it names something else. The same comparison is `1`. Removing that call returns `0`. `test/projection_rewrite.sh` stayed 84 passed on the fixed build.

- `pgc_reconcile_records` no longer reports an impossible mismatch on a log it
  cannot measure (#1242). Handed a log that states `checks run: N` and carries
  no `RESULT` records at all, it ran the arithmetic anyway and concluded that
  every counted check had vanished:

  ```
    records=0 but the log states checks run: 126
      126 check(s) were counted without emitting a record.
      Likely cause: something bumped PGC_CHECKS without going through pgc_record.
  ```

  The function already guards two input faults and its comments name both -- an
  absent log, and a log with no `checks run:` line at all. The third is a
  well-formed log of some other producer, which emits no `RESULT` records
  because records are not a thing it emits.

  **The verdict is unchanged; only the words are.** It still returns non-zero,
  because being handed something unreconcilable is worth surfacing, and going
  quiet would let a genuine zero-record suite pass as clean. A positive count
  with no records is reachable inside this harness too -- an assignment to
  `PGC_CHECKS` that never went through `pgc_record` -- so the guard does not
  claim to know which of the two it is looking at. It says it cannot tell.

  Verified from the output rather than from the diff, all four inputs:

  ```
    states 126, no records      "cannot measure ... rather than a suite that lost every outcome"   rc=1
    1 record, states 3          "2 check(s) were counted without emitting a record"                rc=1
    3 records, states 1         "2 check(s) reported an outcome the count never saw"               rc=1
    1 record, states 1          (silent)                                                           rc=0
  ```

- The cause under a records/count mismatch is now marked `Likely cause:` rather
  than stated flatly (#1242). The two counts above it are measured; the sentence
  below it is the fault the message was written for, and is a guess about which
  one produced them. Stated as fact it sent a reader to subshells during the
  counter-clobber defect, where the cause was an assignment of their own.

  How it surfaced is the part worth keeping: not by looking for it. A bare
  `bash test/<suite>.sh` never reaches this function -- it lives in
  `run_all_versions.sh` -- so every green called from a development run is
  called without it. Sweeping 64 accumulated logs through it found one genuine
  mismatch in zero and one impossible one.

  Seven arms cover the two changes and six of them carry a mutation that reddens
  them; `test/check_ledger_budget.txt` records all three. The one worth naming
  here is the over-fire -- a guard that fired on any shortfall rather than only
  on no records would swallow the real counted-without-a-record fault while
  every arm above it stayed green. It reddened four arms, and two of them were
  already in the suite, so the corpus caught that failure without help. The
  prediction said two; recording the four is the point of writing it down.

  Driving that mutation also showed one of the new arms was a byte-for-byte
  duplicate of an existing one -- same fixture, same grep, same expectation.
  Removed rather than shipped, which is why the part went from 1146 checks
  to 1145.
- 252 suites declared accounting and then exited without it on any of
  `pgc_setup`'s eight failure paths (#1233).

  Every suite that calls `pgc_setup` also calls `pgc_summary`, so every one of
  them DECLARES accounting. `pgc_setup` has eight `exit 1` sites and calls
  `pgc_summary` on none of them.

  **NOT A FALSE PASS.** `rc` is 1 and the FATAL is correct on all eight. What
  was lost is the machine-readable line, and six readers consume it:
  `run_all_versions.sh`'s reconciliation, `pgc_vacuity.py`,
  `test_check_records.py`, `test_residual_is_counted.py`,
  `test_suite_accounting.py` and `smoke.sh`. To every one of them a declared
  suite with no accounting is indistinguishable from a suite that died mid-run.
  `run_all_versions.sh` already names the case in the reconciliation's own
  comment, so the comment is older than the defect it describes.

  Reported against the build failure alone; it is every failure exit, measured
  by driving a NON-build one:

  ```
    control   rc=0  accounting lines=1  TERMINATED=0  tail: PASSED
    mutant    rc=1  accounting lines=1  TERMINATED=1  tail: TERMINATED before its summary
  ```

  EMITTED FROM THE TRAP, WHICH IS ONE SITE RATHER THAN EIGHT. `pgc_teardown`
  already runs on every one of them, so `trap pgc_on_exit EXIT` covers the ninth
  exit by construction. A per-exit patch cannot promise that. The wrapper
  captures `$?` on its first line, so a FATAL is rc=1 and stays rc=1.

  The line it emits is the one `pgc_log_shows_accounting` anchors on, and the
  marker beside it keeps three states distinguishable to a reader as well as to
  a parser: ran to completion, terminated before its summary, died mid-run.

  THE READER LEARNED A THIRD STATE, because emitting the line was not enough
  (@OffgridwithJD, review). `pgc_log_shows_accounting`'s own comment states its
  contract: *"its presence says 'this suite reached its summary'"*. Emitting on
  every exit silently ended that, and a `set -e` abort RUNS the EXIT trap, so
  only `SIGKILL` leaves no accounting at all and the mid-run death became
  invisible too. `TERMINATED` was emitted and nothing parsed it.

  ```
    yes          reached its summary           unchanged meaning
    terminated   stopped before it             named in the run's report
    no           no accounting at all          unchanged meaning
  ```

  Folding `terminated` into `yes` loses the mid-run death; folding it into `no`
  restores the false *declared but never accounted* the trap was added to
  remove. Both call sites reconcile a terminated log, because it carries a count
  and the records behind it, and the reconciliation names it rather than
  counting it as a fault: the suite's own `rc` has already failed the run.

  `test/selftest/590-a-declared-suite-must-account.sh`, nineteen checks, of
  which eight are reddened by reverting the trap and one by folding the reader
  back to two values. Five majors on the composed tree: rc=0, `checks run: 1164`,
  PASSED. The last three drive a completed log, a terminated log and
  an accounting-less log THROUGH the reader and assert the verdict: **the arms
  that existed asserted the line was emitted and that the pattern matched it,
  and none asserted what a reader concluded from it.** That was the gap the
  first version came through.

  THE FALSE-POSITIVE SURFACE IS MEASURED, not assumed empty. Four `exit 0` sites
  exist in declared suites and every one reaches its summary first, three on the
  previous line and `logical_subscriber` on the same line. An arm asserts it,
  because a guard that mislabels correct behaviour gets switched off.

- Planning a columnar query sequentially scanned `pgcolumnar.storage` on its own
  primary key (#1237).

  Two readers key on `storage_id` and both passed `InvalidOid`, so both swept the
  catalog sequentially on the one column `storage_pkey` is a UNIQUE btree over:

  | reader | phase | reached from |
  | --- | --- | --- |
  | `PgColumnarGetSortedInfo` | planning | `pgcolumnar_sorted_pathkeys` |
  | `PgColumnarCheckNativeFormatVersion` | execution, once per relation scanned | `columnar_reader.c:644` |

  Unlike #1210 nothing had to be built and no decision about #1211 was involved:
  the index exists and the key is exact.

  ```
    unfixed   count(*) idx=0 seq=1    join idx=0 seq=5
    fixed     count(*) idx=1 seq=0    join idx=4 seq=1
  ```

  The four converted scans are `GetSortedInfo` twice at planning and
  `CheckNativeFormatVersion` twice at execution. **THE RESIDUE IS
  `pgcolumnar_written_stripe_row_limit`, ONCE PER COLUMNAR RELATION THAT REACHES
  IT** (#1210, #1211), which keys on `relation_oid` and has no index to name.
  One here because this query's qual is on one side of the join only;
  @OffgridwithJD measures two where both sides reach it. `seq` becoming 1 is a
  property of the query, not of the change.

  WHICH IS WHY THE JOIN ARM ASSERTS `idx_scan >= 2` RATHER THAN `seq_scan == 0`.
  Asserting zero would fail for a defect this change does not fix, and asserting
  one would pin a number #1210 is expected to move.

  The two shapes are separate arms because they reach different code. A no-qual
  count never reaches the row-group-limit lookup, so its only storage access is
  the format-version one and `seq_scan == 0` reads cleanly for that site alone.

  HOW IT WAS FOUND: two instruments disagreeing, and both being right. An `elog`
  inside `pgcolumnar_written_stripe_row_limit` counted 2 scans for a join;
  `pg_stat_all_tables.seq_scan` on the catalog counted 4. An elog at one
  function counts arrivals at that function; a counter on the relation counts
  scans from any caller. The gap was the second reader.

- Eleven suites built with their own `make`, so neither the build stamp (#536)
  nor the cross-major object check (#1219) protected them (#1220). They now route
  through `pgc_build_and_install`.

  One major's objects were installed into another major's prefix, silently. The
  demonstration is the issue's own fixture and it is the same command twice, PG19
  objects in the tree and a build stamp made to AGREE with the major being run:

  ```
    before   smoke.sh <pg17a>  rc=1   pg_ctl: could not start server
                               the pg17a prefix then held a library with an
                               undefined PG19-only symbol, build_simple_rel_hook

    after    smoke.sh <pg17a>  rc=0   -- the objects in the tree were not built
                               against .../pg17a/include/postgresql/server,
                               whatever the build stamp says; cleaning first
                               SMOKE TEST PASSED, 9 checks, no foreign symbol
  ```

  `pg17a` and `pg19a` are one box's names for a PG17 and a PG19 installation; the
  point is that they are different majors, not which prefix holds them.

  A stamp that DISAGREES was always caught. A stamp that agrees while the objects
  are foreign is the dangerous direction, and it is the one any hand-run `make`
  for another major creates.

  **These eleven also write the source stamp now**, which they never did, so each
  of them becomes a freshness controller for whatever runs after it with the build
  skipped. That is the other side of #1230: a run that installs without recording
  leaves the next suite refusing a binary that is in fact correct.

  **A further change is a behaviour change rather than a guarantee restored.** These suites discarded `make`'s exit status, so a build
  that failed left them reporting a full set of checks against the PREVIOUSLY
  installed library. The builder refuses instead: "the build failed, so there is
  nothing new to test". A suite that cannot build now says so and stops.

  `objstore_stash_recovery.sh` keeps its own vocabulary, reporting through
  `pgc_fail` and `pgc_summary` rather than exiting, because its accounting line is
  what tells the reader nothing measured the guard.

  **`pg_upgrade.sh` and `extension_upgrade.sh` are not part of this and are not a
  remaining tail of it.** `pg_upgrade.sh` never builds the tree: it tars a copy
  and builds that against both majors with `make clean` before each, which is the
  matrix's own safety argument, and a single-major builder cannot express two
  majors at once. `extension_upgrade.sh` cleans the tree before rebuilding it, and
  installs an OLD REF into the prefix on purpose -- that is its subject, and no
  builder whose job is to install what this tree built can express it. The two
  look like the other eleven from a grep and are a different thing.
- `rebuild.sh` installed a correct library and recorded nothing, so the next suite
  run with `PGC_SKIP_BUILD=1` refused it (#1230).

  The refusal was false in both halves:

  ```
    A. harness-built, build skipped
       -- source: 9cea7e4a926b matches the binary under test        rc=0
    B. append a comment to a .c, then test/rebuild.sh <pg18a>       rc=0
       the installed .so is built from the edited source
    C. the same suite, build skipped, the way the matrix runs it    rc=1
       FATAL: the binary under test was not built from this source
              (refusing to report checks about code that is not installed)
  ```

  The binary was built from that source one step earlier, and the code is
  installed. The reader is sent to the compiler and to a list of 66 files when the
  answer is that a stamp did not move. It cost three runs the last time it
  happened, two of them spent deleting object files.

  After the fix, C reads `-- source: 84a75ca2a3bf matches the binary under test`
  and reports its 11 checks. Editing again WITHOUT rebuilding still refuses, which
  is the control that matters: the guard is recorded, not weakened.

  **The tool keeps its own build.** Routing it through `pgc_build_and_install`
  would have traded away the parallel `-j`, the compiler-warning gate that mirrors
  the matrix's zero-warning rule, and the error extraction from the build log --
  none of which the harness builder has. Only the record was missing, so only the
  record moved: `lib.sh` gains `pgc_record_source_stamp`, one place that knows what
  a record of an install is, and both the builder and the tool call it.

  `test/selftest/580-a-hand-rebuild-must-record.sh`, nine checks, serially on all
  five majors. Seven of the nine carry an observed red. The driven arms run
  `rebuild.sh` for real against a `pg_config` shim that redirects `--pkglibdir` and
  `--sharedir` into a temporary prefix, so nothing reaches a real installation,
  and cost 2.1 seconds.

  `pgc_record_source_stamp` refuses when it is not told what to record
  (@jdatcmd, review). With no arguments it did not fail: it wrote
  `./.pgc_source_stamp.0.nolibd41` into the current directory, keyed by the md5 of
  an empty pkglibdir -- `d41` is the front of `d41d8cd98f00`, the md5 of nothing --
  holding a fingerprint of the current directory rather than of any source. Both
  callers pass real arguments, so it was latent; a believable record in the wrong
  place is worse than none, because the freshness gate reads it.

  One of those arms watches the live prefix while the run happens, and it reads the
  **mtime** rather than the digest: the tree it builds is a copy of the tree under
  test and the build is byte-reproducible, so an install that did land on the real
  prefix would write identical bytes and a digest arm could never fail.
- The `.git` sweep shipped with #1224 caught half the spellings it claimed to,
  and refused three that are correct (#1227).

  `-d[[:space:]]+"?\$?\{?[A-Za-z_]*\}?/?\.git` allowed the optional quote only
  BEFORE the variable, so `[ -d "$SRCDIR"/.git ]` did not match, and it had no
  path component, so `"$WORK/$name/.git"` did not either. Found by
  @OffgridwithJD, who moved the guard's own refusals into one place, counted
  three of them, and noticed only two had ever been driven.

  Measured over ten spellings, every one either of us has written or found:

      shipped pattern     5 of 10 caught
      this pattern       10 of 10 caught

  A guard that under-reports is worse than no guard, because it answers the
  question with a number that reads as coverage.

  IT WAS ALSO WRONG IN THE OTHER DIRECTION. `\.git` with nothing after it is a
  prefix of `.gitignore`, `.gitattributes` and `.github`, so three correct
  spellings were refused. The pattern now spans from the test operator to
  `.git` and requires a non-word character after it:

      five legitimate spellings, this pattern             0 flagged
      the same five, without the trailing boundary        3 flagged

  The second line is a check, not a remark: naming the change that would make
  the first arm fail is not the same as making it. The first draft of that arm
  spelled `.gitattributes` with `-f`, so the form was excluded for having the
  wrong operator and the boundary it exists to test was never reached.

  LATENT, NOT LIVE, and the number is recorded because the issue named the
  check and did not run it. The false-positive count over every `test/*.sh` and
  `test/selftest/*.sh`, comments stripped, is ZERO for the shipped pattern and
  zero for this one: no suite reads those three names with `-d` today. It is
  fixed anyway, because a guard that refuses correct code gets switched off and
  the rule goes with it, and the cost arrives as a red build nobody can explain.

  The ten forms are ASSEMBLED FROM A FRAGMENT rather than written out. The
  sweep reads this file, so spelling them literally would make the guard count
  its own test data, which is the defect the part exists to catch and which its
  first version committed.

  THE CONTROL IS DERIVED FROM THE PATTERN, NOT COPIED (@OffgridwithJD, review).
  A second hand-written copy of the pattern is a drift site, and it drifts
  GREEN: edit the boundary in one and the control measures a pattern nobody
  ships. Stripping the boundary off the real pattern cannot drift, and it fails
  safe. Measured with the boundary changed to `([^A-Za-z0-9]|$)`:

      derived control, boundary intact     flags 3 of 5, arm passes
      derived control, boundary drifted    flags 0 of 5, arm goes RED

  because the strip silently becomes a no-op and the control keeps the boundary
  it was meant to lack. A premise beside it names that cause, so the failure
  reads as "the boundary strip changed nothing" rather than as an unexplained
  0 against 3.

  THE SWEEP NOW READS COMMANDS, NOT LINES (@OffgridwithJD, review). bash
  continues a command after a trailing backslash, so a test spelled over two
  lines was invisible to both halves of a line-at-a-time sweep. Latent again,
  with zero live sites, and closed for the same reason as the false positives.

      continuations in test/*.sh and test/selftest/*.sh    6533
      lines, raw                                          70623
      lines, folded                                       64090
      matches, folded                                         0
      matches, unfolded                                       0

  Comments are stripped BEFORE the fold: the other order joins a comment to the
  code line below it and strips both, which loses real code rather than gaining
  it. Parts 260 and 460 already fold with the same sed.

  A BACKSLASH FOLD CLOSES THE CLASS HERE, measured rather than assumed. bash
  also continues after a trailing `&&`, `||` or `|` with no backslash, and every
  sweep in this tree folds on `\` only. Those operators cannot hide this
  pattern, because they join COMMANDS and not a command's words: splitting
  `[`'s operands with a bare newline is not a continuation but `[: missing ']'`
  at runtime. All four constructs parsed and run.

  Fourteen mutations back the nineteen arms, one log each, every ledger row
  merged from a run rather than typed. Two of them are PAIRS in which neither
  half is evidence alone: the same planted line in a real suite is caught by
  this sweep and invisible to the shipped one, with nothing different between
  the runs but the pattern, and the same again for the fold. One of the four is
  deliberately GREEN and writes no ledger row, because a mutation whose value is
  that nothing reddens cannot leave a last-red.

- `extension_upgrade.sh` accepted an ENCLOSING repository as the tree's own,
  and turned a skip with instructions into a fatal error (#1228).

  `git rev-parse` searches upward. A copy with no git presence of its own that
  happens to sit inside any repository answers yes to `--absolute-git-dir`, so
  the #1224 gate let it through. `git clone --shared` then takes that path
  literally rather than walking up. Reported by @OffgridwithJD and driven here
  on two fixtures, because what the reader is told depends on what the
  ENCLOSING repository happens to contain:

  ```
    enclosing repo has no v1.0-alpha tag
      #1224   FAIL  v1.0-alpha is not present. Fetch tags, or pass a ref
      now     SKIP  ... sits inside the repository at /tmp/bx/nest

    enclosing repo HAS a v1.0-alpha tag
      #1224   fatal: repository '/tmp/bx/nest/copy' does not exist
              FATAL: clone failed
      now     SKIP  ... sits inside the repository at /tmp/bx/nest
  ```

  The first is the worse of the two. Both of the #1224 gates pass on a
  directory that is not a repository at all, and the suite then tells the
  reader to fetch tags: the ref is being looked for in somebody else's
  repository, so fetching tags into this tree cannot help, and the instruction
  is not merely unhelpful but wrong. A skip carrying a usable instruction had
  become a failure carrying an unusable one.

  `--show-toplevel` separates the three cases, which `--absolute-git-dir`
  cannot:

  | tree | `--show-toplevel` | verdict |
  | --- | --- | --- |
  | linked worktree | itself | its own |
  | gitfile-staged copy | itself | its own |
  | nested in a repository | the enclosing root | not its own |

  THE MESSAGES WERE REWRITTEN, not just the test. *"git cannot answer for
  $SRCDIR"* became false in exactly the way this thread is about once the check
  changed: git answers, and it answers about a different tree. Both sites now
  name WHICH of the two situations the reader is in, rather than offering a
  disjunction to resolve, because they have different repairs:

  ```
    nested in a repository   It sits inside the repository at <root>, which is
                             a different tree and does not carry this one's
                             history.
    no git anywhere          It has no git presence of its own and none above it.
  ```

  Both driven, as the principal that runs the suite. The first version of this
  probe ran its premises as `root` and the suite as `postgres`, and git's
  dubious-ownership refusal made a nested tree look like a tree with no git:
  the before and after arms printed the same thing, and the measurement said
  nothing at all.

- `extension_upgrade.sh` skipped in a linked worktree, because it stat'd `.git`
  instead of asking git (#1224).

  It decided whether it could build its old ref with `[ -d "$SRCDIR/.git" ]`. In
  a linked worktree `.git` is a regular **file** holding `gitdir: <path>`, and so
  is the one-line gitfile `devloop.sh` writes into its build dir. git answers
  perfectly in both; the directory test does not.

  | | |
  | --- | --- |
  | linked worktree `.git` | regular file |
  | `[ -d .git ]` | false |
  | `git rev-parse HEAD` | `5b20ae8f` |

  So the suite skipped its default path in *"the audit container, worktrees of a
  clone"*, one of the five environments `selftest/310` surveys as supported.

  Both sites now use `git -C "$SRCDIR" rev-parse --absolute-git-dir`, which is
  what `devloop.sh` already used, with the reason recorded beside it: *"SRC may
  itself be a linked worktree, where `.git` is a FILE and that path is not a git
  directory."* `docs_style.sh` and `native_upgrade_converge.sh` ask git too, so the
  defect was one file against three, not a missing idiom.

  **The suite now runs rather than skipping, which is a stronger claim than the
  one the issue made.** `git clone --shared` from a linked worktree was measured
  to work and old refs are reachable through it, so correcting the test was the
  whole fix rather than a step of it:

  ```
    before, gitfile tree   SKIP  "not a git checkout"
    after,  gitfile tree   == extension_upgrade: PASS
                           (upgraded catalog byte-identical to a fresh
                            1.0-alpha5 install, 162 objects)
  ```

  `test/selftest/570-a-checkout-test-must-ask-git.sh`, five checks. It is a class
  guard rather than a list: it asks whether **any** suite decides the question by
  stat-ing `.git`, so a fourth file adopting the wrong form is caught with nobody
  maintaining a roster.

  The arm states the gitfile's nature with `stat -c %F` rather than probing it
  with the discouraged idiom. The first version did probe, and the sweep then
  counted its own demonstration and stayed red after both real sites were
  fixed. That is a guard satisfying its own pattern, which is #1222 one level up.

- A hand-run `make` for another major installed its objects into this one's
  prefix, and the build stamp could not see it (#1219).

  `pgc_build_and_install` decided whether to clean by comparing a STAMP the
  harness writes against the major being built. Neither side is a property of
  the objects in the tree, so a `make` run by hand for another major left
  foreign objects and never touched the stamp: the two agreed, the clean was
  skipped, `make` found everything up to date, and one major's objects were
  installed into another's prefix. Found by @OffgridwithJD while measuring a
  cross-major preflight -- a PG 18 run died on `undefined symbol:
  build_simple_rel_hook`, which is the PG 19 name.

  **The stamp fails closed in one direction and open in the other, and only the
  open direction is dangerous.** A stale or absent stamp forces a clean nobody
  needed. A stamp that matches while the objects are foreign is silent.

  The objects can answer for themselves. The build passes `-g`, so each `.o`
  carries a DWARF directory table naming the server headers it was compiled
  against; `pgc_objects_built_for` compares that against
  `pg_config --includedir-server`. Not by parsing a major out of the path -- the
  layouts differ (`/usr/local/pgNN/include/postgresql/server` against
  `/usr/include/postgresql/NN/server`) and a pattern written for one returns
  empty for the other, which reads exactly like "not derivable".

  Proved end to end on the case the stamp cannot construct. PG 18 objects, a
  stamp written to say `17`, run under PG 17:

  | | result |
  | --- | --- |
  | with the check | cleans, rebuilds, `4 passed + 0 failed` |
  | without it | `undefined symbol: pqsignal_be`, server never starts |

  `had=17` and `major=17` agree in both runs, which is the defect; the objects
  are what refuse.

  **A build without `-g` reports `unknown`, not foreign.** `/usr/local/pg18_nc`
  carries no `-g` in its `cflags`, so its objects have zero `.debug_` sections
  and there is nothing to compare. Reporting those as foreign is correct in the
  fail-closed sense and useless in practice -- every suite would clean and
  rebuild on every run, and a guard that makes the tree slow gets switched off.
  `unknown` hands the decision back to the stamp, which is what those builds do
  today.

  So the three answers are not two, and what each one costs differs:

  | reading | returns | caller |
  | --- | --- | --- |
  | DWARF present and disagrees | `no` | cleans, whatever the stamp says |
  | `pg_config` cannot be asked | `no` | cleans |
  | no objects | `unknown` | stamp decides |
  | no `-g`, or no `readelf` | `unknown` | stamp decides |

  **This check needs `-g` and `readelf` to do anything at all**, and where it
  has neither it adds no protection rather than adding a refusal. That is
  deliberate, and it is the reason the stamp check stays rather than being
  replaced.

  The pytest harness drives the same `pgc_build_and_install` out of `lib.sh`
  rather than carrying a second implementation, so both harnesses gain this from
  one change; `test_build_refusal.py` holds that wiring, 107 checks.

  `test/selftest/560-the-objects-decide-which-major.sh`, thirteen checks, the
  same thirteen on 15, 16, 17, 18 and 19, all passing with nothing declining.

  **Nothing branches on what the host happens to have.** The part builds both
  object kinds itself -- `cc -c` and `cc -g -c` on a two-line source -- and
  asserts both debug-section counts as premises, so "cc obeyed both" is measured
  rather than assumed. An earlier revision branched on whether *this* build
  carried debug info and declined the arm that did not apply; that is #1185's
  rule applied correctly, and it produced a worse bug. `check_unrunnable` makes
  a run `EXIT_INCOMPLETE` by design, so an arm that is structurally inapplicable
  on most hosts made the whole suite incomplete nearly everywhere:

  | | |
  | --- | --- |
  | main | `rc=0` 1094 passed + 0 unrunnable, PASSED |
  | that revision | `rc=67` 1105 passed + 1 unrunnable, INCOMPLETE |
  | now | `rc=0` 1107 passed + 0 unrunnable, PASSED |

  A declined arm is right when applicability is a property of the **run**. It is
  wrong when applicability is a property of the **host** and the thing under
  test is a pure function of a file -- then build the file.

  Three arms read `pgc_build_and_install`'s own text and require it to reach
  both decisions, because a removal proof of the function alone passes while the
  lines that call it do nothing. **They strip comments before counting**: the
  first version passed on prose, green on a caller that consulted nothing, and
  one arm now builds that exact text and requires zero. `selftest/190` has the
  same shape on `pgc_build_needs_clean`, tracked in #1222.
- Planning paid to probe two catalogs that are empty in the default
  configuration, and the probe could not win there at any database size (#1217).

  `pgcolumnar.options` gets a row only when `set_options` is called, and
  `pgcolumnar.projection` only when a projection is added. **An installation
  doing neither has both empty, and that is the default.** At zero rows the heap
  the probe replaces is zero pages, so the sequential read it replaces is
  literally free and the index cannot win however large the database grows.
  There is no crossover to be above. Measured on `6c3a9510`, 50 plans:

  | columnar tables | `options` | `projection` | per plan |
  | ---: | --- | --- | ---: |
  | 10 | heap 0, index 100 | heap 0, index 200 | 6 |
  | 200 | heap 0, index 100 | heap 0, index 200 | 6 |
  | 1000 | heap 0, index 100 | heap 0, index 200 | 6 |

  Flat, because there is nothing to scan more of.

  #1198's seven planner-path sites now go through the size check that #1213
  shipped, and the cost returns to **nothing** -- 300 buffers over 50 plans down
  to 0, which is the pre-#1198 number exactly. The probe is still taken where it
  pays: with `projection` at seven pages of other tables' rows, planning reads
  451 buffers against 1050 for reading it whole.

  **The threshold did not need a second value, and that was not assumed.** 3 was
  derived entirely on the compaction path, and #1217's own data put the planner
  crossover near 1200 tables, so a separate constant looked likely. Swept across
  five database sizes on the planner path, 3 is optimal or tied at every one.
  The first sweep said otherwise and its fixture was wrong: it planned the
  FIRST-created table, whose row sits at the head of the heap, so the sequential
  scan it replaces stops almost immediately. That made a higher threshold look
  51 buffers better at 1200 tables; measured on the last-created table it is 199
  worse.

  `test/catalog_plan_index.sh` (14 checks) and
  `test/pytest/test_catalog_plan_index.py` (15). Four arms were **removed**
  rather than repaired: they asserted `idx_scan >= 1` and `seq_scan == 0` on each
  catalog, and they fail against a build that made planning strictly cheaper.
  That is a guard firing on correct code, the same defect #1213 removed one
  level over, and it was predicted before this change was written -- the
  prediction named those four arms, the direction each would move, and the
  `storage` arms as the ones that must not move, and all three held.

  The port runs on a private database (`pgc_own_db`, moved to `conftest.py` now
  that a second file needs it), because these catalogs are per database and
  inside the corpus other files have already populated them.

- Retiring a row group read five catalogs whole, once each per group, and the
  cost grew with every unrelated columnar table in the database (#1207).

  `delete_group_rows()` opens its catalog from a `const char *tableName`
  **parameter**, and `PgColumnarDeleteGroupMetadata` calls it five times, for
  `delete_vector`, `column_chunk`, `zone_map`, `bloom` and `row_group`. So one
  `systable_beginscan` in the source was five sequential reads per retired group
  at run time, and two more sit beside it on the compaction path. The
  `pgcolumnar` metadata catalogs are shared by every columnar table in the
  database, so each of those reads was charged for every other table's rows.

  Two audits of these scans missed it. Both attributed a scan to a catalog by
  the `open_columnar_table("<name>")` that produced its relation handle, and a
  relation that arrives as an argument has no name at the call site. What found
  it was a reconciliation rather than a better reading: probing all 44
  `systable_beginscan` sites and requiring the number that ran without an index
  to equal the sum of `seq_scan` over every catalog failed at 41 counted against
  22 probed.

  Eight sites now choose between an index probe and a sequential read **by
  asking the catalog how many pages it has**. Every key was already a prefix of
  an index that exists, so nothing here needs a catalog migration.

  **Choosing by size, rather than always probing, is the whole of the fix.** An
  index probe is not unconditionally cheaper: it is a btree descent plus a heap
  fetch plus two catcache lookups, which on a one-page catalog is more work than
  reading the whole thing. The same catalog is one page in a database with one
  columnar table and hundreds in a database with a thousand, so a path that
  commits to either method is wrong at one end of that range. Measured in
  buffers, for `pgcolumnar.compact()` over 40 row groups with 20 retired:

  | catalogs | always read whole | always probe | **by size** |
  | ---: | ---: | ---: | ---: |
  | 8 pages | 848 | 1000 | **848** |
  | 18 pages | 1112 | 1080 | **1038** |
  | 46 pages | 1672 | 1086 | **1041** |
  | 390 pages | 8993 | 1122 | **1080** |

  Always probing is a regression of up to 18 per cent on a small database;
  always reading whole costs 8.3 times as much on a large one. Choosing by size
  is within 4 buffers of the best of the two at every size measured.

  `pgcolumnar.index_min_blocks` is the page count at which the choice flips.
  Its default of **3** is derived rather than chosen: every threshold from
  always-probe to never-probe, on PG 15, 17 and 19, over two families of fixture
  (many small columnar tables; one deep one), 24 size points. Scored against the
  cheapest threshold at each point, 3 costs 19 buffers in total where its
  nearest rival costs 95, and never more than 4 at any single point. The
  optimum is not the same for every catalog -- `row_group` pays for a probe at
  three pages, `zone_map` not until five, because the hot catalogs are read more
  often per retired group -- so one value for all six is a priced compromise
  rather than a truth. Setting it very large restores the behaviour of every
  earlier release exactly; setting it to 0 always probes. Both are slower at
  some database size.

  Two of the eight sites are in `PgColumnarCheckFreeSpaceNoOverlap`, which is
  assert-only. It was the last one found, and it is worth saying why: a
  measurement taken on a release build reports those two clean while every
  assert-enabled CI leg pays for them on each maintenance operation. Both
  suites print `debug_assertions` for that reason.

  `test/catalog_delete_index.sh` (30 checks) and
  `test/pytest/test_catalog_delete_index.py` (31). They assert the work --
  buffers served out of the six catalogs -- and never the access path. An
  earlier version of both asserted `seq_scan = 0` and `idx_scan >= 1` per
  catalog, and that is a claim about which path was taken: run against the
  size-aware build, which is cheaper at every size, it failed 13 of 21 arms, the
  same 13 a full revert reddens. A guard that fires on correct code gets
  switched off.

  The port runs on a private DATABASE rather than the private schema every other
  test here gets, because the `pgcolumnar` catalogs are per database and shared
  by the whole session, and every claim in the file is about how big they are.
  Run alone the file saw six catalog pages at its smallest phase; run after the
  other fifty-one cluster files it saw thirty-nine, and the claim there fell from
  223 parts per thousand to 65. A premise counts the columnar relations so that
  arrives as a named failure rather than a weak number.

- A subset pytest run failed on PG 15-17, and the message told you to break the
  check (#1204).

  Running one file on a major with rows in `expected_unrunnable.txt` exited 67
  while reporting its own file passed, naming six tests it never collected as
  proof that a tracked list was stale:

  ```
  $ pytest test_native_reclaim_cycles.py --pg-config .../pg17
    1 passed, 15 checks, 0 fail        exit 67
  ```

  **The check had an unstated premise.** `allowed - actual` reads as "listed as
  declining, did not decline", which is only answerable for a test the run
  COLLECTED. A subset collects none of the others, so the question was not stale,
  it was unasked.

  **And the instruction was worse than the noise.** Running exactly one of the six
  listed decliners failed too, because the other five were still uncollected; and
  doing what the message said, deleting the rows, moved the red to the opposite
  direction, where a full run would have fired against rows that were correct. A
  guard that refuses correct work gets switched off and takes its rule with it.

  The premise is now stated per ROW rather than per run: the expected-but-ran
  direction is intersected with what the session collected. A full run is
  unchanged, because every listed row is collected there. A listed decliner that
  WAS collected and did not decline still reddens a one-file run, and an
  unexpected decline still reddens one, so both halves of #1192 keep their teeth
  on a subset.

  Reported by @OffgridwithJD, who also tried to break the fix and could not:
  applied to main with a deliberately stale row planted, it refuses under both a
  serial run and `-n2`, where the concern was that the controller collects nothing
  under xdist.

- The session that deleted rows still scanned `delete_vector` sequentially, once per row
  group (#1146).

  `delete_vector` gained a `_pkey` and its readers were switched to it, but
  `PgColumnarUpsertDeleteVector` kept passing `InvalidOid`. It runs once per row group, so
  a DELETE touching twenty groups took twenty sequential scans of the catalog -- the cost
  the index switch existed to remove, still paid by the writer.

  Attributed with an elog probe at every `delete_vector` scan site rather than inferred
  from the totals:

  | site | calls during the DELETE |
  | --- | ---: |
  | `PgColumnarUpsertDeleteVector` | 20 |
  | `PgColumnarReadDeleteVectorList` | 40 |
  | `PgColumnarReadDeleteVectorsForStorage` | 1 |

  against `seq_scan=20 idx_scan=41` for the catalog, so 40 + 1 are the indexed reads and
  the twenty are that one site. Afterwards:

  | | seq_scan | idx_scan |
  | --- | ---: | ---: |
  | before | 20 | 41 |
  | after | 0 | 61 |

  41 + 20 = 61, so every scan that was sequential is now indexed and none was lost.

  **The regression arm has to be in the pytest harness.** Every `q` in the shell harness
  is its own `psql`, so the scan it measures is always made by a session that did no
  writing, and the writing-session path is unobservable there whatever the arm asserts.
  The new test holds one connection across the DELETE and reads the counters as a delta
  over it.

### Fixed

- `docs/limitations.md` told a user that a range column has a collation, and it does
  not (#1144 review).

  The "Skipping and collation" section says a pushed-down filter drives skipping only
  when the comparison collation matches the COLUMN's collation, and that this is the
  collation that ordered the stored minimum and maximum. Range pruning landed in
  #1196 and made that wrong for a range column, in a section that change did not
  touch.

  Measured rather than reasoned:

  | | value |
  | --- | ---: |
  | `attcollation` of a text column declared `COLLATE "en_US.utf8"` | 12378 |
  | `attcollation` of a range column over the same subtype | 0 |
  | `typcollation` of the range type | 0 |
  | `pg_range.rngcollation` | `"en_US.utf8"` |

  So a range column has no collation for a comparison to match. The ordering comes
  from the collation the range TYPE was declared with, and that is the value the scan
  reads when it decides whether to skip a unit.

  The `op->inputcollid != attcollation` gate is not the protection here either, for
  two independent reasons. The range path in `PgColumnarSetRelPathlist` sets
  `PGC_SK_RANGE` and returns before reaching it. And both sides are 0 for a range
  predicate, so the comparison would pass the clause through even if it ran.

  Reported by @jdatcmd.

### Added

- `ttl_expire` ships in both harnesses (#1188).

  `pgcolumnar.expire` is the one function in the tree that DELETES ROWS, so a wrong
  answer there loses data rather than reporting a wrong number. It had a shell suite
  and no pytest twin, which is the worst place in the corpus for single-harness
  coverage.

  `test/pytest/test_ttl_expire.py` asserts all 51 of the shell suite's properties:
  the straddling group that constrains the feature, a live NULL the zone map's
  maximum cannot speak for, a deleted NULL that must stop pinning its group forever,
  the index-only scan over a group `expire` removed without touching the delete
  vector, the `date` rounding, and the session-zone cutoff.

  **Two substitutions, both asserted rather than assumed.** The shell suite bakes
  `pgcolumnar.stripe_row_limit=1000` into the cluster config before the postmaster
  starts; this corpus has one connection, so it uses per-table
  `set_options(..., stripe_row_limit => 1000)` and pins the resulting group count on
  every fixture. `ALTER DATABASE ... SET` becomes a session `SET`, and the plan it is
  meant to produce is asserted. A procedural substitute for a structural guarantee
  can fail silently where the original cannot.

  **Measured, not argued.** Changing the retire decision to read a group's `minimum`
  instead of its `maximum` is the data-loss implementation, and it reddens the
  straddle arm in both halves: `NO row still inside the retention was dropped: got
  [3000] want [3560]`. Removing the live-NULL guard reddens two tests; neutering the
  deleted-NULL arm reddens one. Each mutation was asserted to match its anchor
  exactly once and to compile, the `.so` moved and returned, and the source was
  restored byte-identical.

  The issue asked for 43 checks. The suite runs 51: a `^check "` sweep anchored at
  column 0 misses four call sites indented inside the timezone loop, and those four
  interpolate the zone into the check name, so 47 call sites yield 51 runtime names.
  The suite's own `checks run:` line is what settles it.

- Range columns prune on overlap and containment (#1144).

  A zone map recorded `minimum` and `maximum` only. Those are the range type's own
  btree ordering, which sorts by lower bound and then upper bound, so the largest
  range is not the one reaching furthest right. A chunk holding `[1,2)` and
  `[3,100)` has the same `maximum` as one holding `[1,2)` and `[3,4)`. Neither
  `&&` nor `@>` could be answered from that, so both read every chunk group.

  `zone_map` gains `max_upper`, the greatest upper bound in the unit. It is a
  `bytea` with three states, because two cannot express what a reader needs:

  | value | meaning | pruning |
  | --- | --- | --- |
  | NULL | no summary was recorded | do not prune |
  | zero length | the unit holds a range unbounded above | do not prune |
  | any other length | the bound, in the element type's encoding | may prune |

  Every row written before this column existed reads as NULL, so an upgraded
  install prunes nothing until its data is rewritten, and never prunes wrongly.

  Measured on 200,000 rows in 20 chunk groups, `tstzrange` over three months:

  | fixture | groups read, before | groups read, after |
  | --- | ---: | ---: |
  | clustered on the range | 20 | 1 |
  | scattered | 20 | 20 |

  The scattered result is the documented one, not a shortfall. Every group holds
  a bound near the maximum, so no summary can exclude any of them.
  `docs/limitations.md` states which predicates prune and which do not.

  The column is added in `pgcolumnar--1.0-alpha4--1.0-alpha5.sql` and in
  `pgcolumnar--1.0-alpha5.sql`, so a fresh install and an upgraded one converge.
  `test/native_upgrade_converge.sh` is what asserts that.

- `pgcolumnar.expire` accepts a `date` retention column (#1135).

  `ttl_column` took `timestamp` or `timestamptz` only. `date` is not an unsupported
  type in that file: the same source handles `DATEOID` in its zone-map key and
  sortability paths. Only the retention cutoff refused it, and a date
  partition key is an ordinary shape for the retention case the feature exists to
  serve.

  The cutoff is computed in the column's own type, as the two existing arms are. For
  `date` that means truncating. No date-minus-interval yields a date, because
  PostgreSQL's `date_mi_interval` returns a timestamp. **Truncation is load-bearing
  even for a whole-day interval**, because the cutoff is measured from the current
  instant rather than from midnight:

  | | value |
  | --- | --- |
  | `now() - interval '3 days'` | `2026-09-19 00:47:25` |
  | truncated to a date | `2026-09-19` |
  | `CURRENT_DATE - interval '3 days'` | `2026-09-19 00:00:00` |

  A row dated `2026-09-19` is midnight, so it falls before the untruncated cutoff and
  survives the truncated one. Truncation therefore moves the cutoff earlier by the
  current time of day. It keeps a row up to a day older than the retention window
  rather than dropping one younger. That is the direction to err in for a function
  whose failure mode is deleting data.

  `test/ttl_expire.sh` pins the rounding with an arm that reddens when the cutoff
  moves a day. Its fixture puts 1,000 rows exactly on the cutoff.

  **The cutoff is the calling session's date, not the server's.** `expire` converts
  the current instant through the session's `TimeZone`. Two sessions can therefore
  retire different groups. Take a three-day retention with the server at
  `2026-09-22 03:21+00`. A `UTC` session cuts at `2026-09-19` and retires three
  groups of the six. A `Pacific/Midway` session cuts at `2026-09-18` and retires
  two. The
  behaviour is inherited rather than introduced: `timestamptz` is zone-independent,
  and the `timestamp` arm already converts through the session zone. What changes
  is the granularity, from hours to a whole day of rows. Found by @OffgridwithJD
  reviewing this change, documented in `docs/sql-reference.md`, and pinned by an
  arm that runs `expire` under two zones.
### Changed

- The pytest corpus runs on more than one major (#1163).

  The two harnesses assert the same properties, and the pytest half ran on PG 18
  alone while the shell half ran the matrix. A five-major gate on a port branch then
  reddened one arm on PG 15, an arm that had never run on 15 anywhere: not in CI, not
  nightly, not in `run_all_versions.sh`. Attributing it meant building a baseline from
  scratch.

  | | before | after |
  | --- | --- | --- |
  | per-PR CI | PG 18 | PG 17 and 18 |
  | nightly | not run | PG 15, 16, 17 and 18 |

  The nightly majors match the shell suites beside them. PG 19 is in neither half of
  that workflow, only as a from-source build in CI, so matching the list keeps the two
  halves aligned rather than inventing a gap the other way.

  **The matrix could not land on its own.** The vacuity layer exited
  `EXIT_INCOMPLETE` whenever anything in a run declared itself unrunnable. Six tests
  decline on 15, 16 and 17 for a correct reason: `pg_restore_attribute_stats` and
  `WITHOUT OVERLAPS` both arrived in PostgreSQL 18. So a healthy corpus exited
  non-zero on three majors, and CI proved it by failing this change's own PG 17 leg.

  The rule is sharpened rather than masked. `test/pytest/expected_unrunnable.txt`
  names what may decline on which major, and the layer refuses a run whose unrunnable
  set differs from it **in either direction**. An unexpected decline is a test that
  stopped running with nobody saying so. An expected one that ran means the feature
  arrived while the file still claims it did not. Proposed by @OffgridwithJD.

  The shell twin's clean nights do not transfer, which is why this needed the majors
  rather than an argument. The two halves differ structurally in the relevant
  place. The shell suite runs each measurement in a fresh `psql` backend. The port
  runs both on one persistent connection.
- The `1.0-alpha5` cycle is open (#1144, #1139).

  `pgcolumnar.zone_map` has to gain a nullable column for the summary that overlap
  and containment pruning needs, and `1.0-alpha4` is published. Adding the column to a shipped
  script would leave an existing alpha4 install without it. A fresh install would
  then have a different catalog from an upgraded one. So the column needs an
  upgrade script, and the upgrade script needs a version.

  This change opens the version and nothing else. `pgcolumnar--1.0-alpha4--1.0-alpha5.sql`
  is **empty on purpose**. The work already pointed at alpha5 appends to a file that
  exists rather than racing to create it. The version bump is then reviewable on its
  own rather than inside a feature diff.

  The install script is renamed rather than duplicated, which is what opening
  `1.0-alpha4` did in `4b66555`. One full install script covers the current version,
  and the older versions are reached through the upgrade chain.

### Fixed

- Three documents named an upgrade chain the tree does not ship (#1197).

  A sentence in each names the installed versions one `ALTER EXTENSION pgcolumnar
  UPDATE` can start from, and the version it arrives at. Opening the `1.0-alpha5`
  cycle left all three wrong, and every version check in `docs_style.sh` stayed
  green:

  | document | starting versions it named | destination it named |
  | --- | --- | --- |
  | `CHANGELOG.md` | 4 of 5, `1.0-alpha4` missing | `1.0-alpha4` |
  | `docs/installation.md` | all 5 | none a machine can find: "reaches **it** from" |
  | `docs/limitations.md` | 3 of 5, under a hand-typed "Three such scripts" | `1.0-alpha3` |

  Both facts are knowable from the tree. `pgcolumnar.control` gives the destination,
  and the `pgcolumnar--A--B.sql` filenames give the starting versions, so the three
  documents are now compared against them rather than edited by hand.

  **The filenames are walked, not counted.** "a single update reaches `1.0-alpha5`
  from any of them" says the scripts form an unbroken chain, which the set of `A`
  sides cannot see: renaming `pgcolumnar--1.0-alpha2--1.0-alpha3.sql` to
  `--1.0-alphaX.sql` leaves `1.0-alpha2` starting a script while three of the five
  named versions can no longer arrive, and every arm passed on that tree. Reported
  by @OffgridwithJD. Each version is now followed `A--B` to the script starting at
  `B` and kept only if the walk ends at `default_version`, and a second premise
  refuses any version that starts two scripts rather than picking one silently.

  **The two errors in `CHANGELOG.md` cancel if you count.** It named `1.0-alpha4`
  once too few as a starting version and once too many as the destination, so the set
  of versions in the sentence was exactly right. A rule comparing that set against
  the tree would have passed a sentence in which both halves were wrong. The two
  claims are therefore read separately, each against its own source on disk.

  The sentence splitter masks markdown list markers per line, before the lines are
  joined, because a list marker begins a line and nothing else does. Masking any
  number followed by period-space also protects a sentence ENDING in a number and
  merged 9, 2 and 71 sentence boundaries in `docs/limitations.md`,
  `docs/installation.md` and `CHANGELOG.md`, measured at `acc4116d`. The counts
  drift as those files grow, so the revision is quoted with them; the three named
  lines below do not drift and are what decided the rule. No verdict moved, which is a property
  of today's prose rather than of the rule.

  The mask is anchored at column 0. Allowing an indented marker read 3939
  sentences in `CHANGELOG.md` against @OffgridwithJD's 3942, and all three lines
  that differ are wrapped prose rather than list items, which is the same
  over-match one indent to the right. Every ordered-list marker in these documents
  is at column 0.

  Eight arms in `test/docs_style.sh` and four tests in
  `test/pytest/test_docs_upgrade_chain.py`. The shell half folds the file with `tr`
  and cuts sentences with `sed`; the pytest half splits on a lookbehind and collects
  with `re`, so a parsing mistake in one is not a parsing mistake in the other.

- Planning a columnar query sequentially scanned `pgcolumnar.options` and `pgcolumnar.projection` (#1198).

  `options_pkey` is `(regclass)` and `projection_pkey` leads with `storage_id`. The planner looks those catalogs up by those columns, and the scans passed `InvalidOid`. One filtered scan of one table then walked every columnar table's options row and every projection row. Measured on PG18 before the change, after `pg_stat_reset()`:

  | catalog | idx_scan | seq_scan |
  | --- | ---: | ---: |
  | options | 0 | 2 |
  | projection | 0 | 2 |

  Both readers now pass their primary key, as `row_group` and `delete_vector` already did. The same lookup on `pgcolumnar.storage` stays a sequential scan: `storage_pkey` is on `storage_id` and that reader looks up by `relation_oid`.

  After the change the same scan reports `idx_scan=2` and `seq_scan=0` on both catalogs, and the filtered count is still every row. Putting `InvalidOid` back on the two planner scans returns `idx_scan=0` and `seq_scan=2`.

- `native_reclaim_cycles` could not reach the defect it guards (#1138).

  It is the declared regression guard for #84. Deleting the #84 fix left it reporting
  `12 passed + 0 failed`, arm for arm, including the arm named after the defect.

  `pgcolumnar.reclaim_coalesce` defaults on, and it does two things: it merges
  adjacent freed ranges, and it carries its own `CommandCounterIncrement` on the free
  path. That second one does the visibility work the #84 fix would otherwise do, so
  with coalescing on the defect is masked.

  Isolated by freeing alternate whole groups, which fragments by non-adjacency and
  leaves the option at its default. On the same mutated build:

  | fixture | coalesce | free list | rewrote | result |
  | --- | --- | ---: | ---: | --- |
  | alternate groups | on | 15 | 15 | clean |
  | contiguous block | off | 18 | 12 | `tuple already updated by self` |

  Fifteen fragments with coalescing on does not reach it. The option is necessary and
  sufficient, and the free-list count is a property of the fixture. The suite runs with coalescing
  off and **asserts that, read back from the server**. The value is set in the cluster
  config. A conf line that stops taking effect returns the suite to the state this
  issue is about. With the #84 fix removed it reddens five
  arms with `ERROR: tuple already updated by self`.

  The pytest twin says the same thing under the same name. It asserts the option read
  back from the server, rather than assumed from the `SET` that asked for it. The parity
  grader is what caught the divergence. It graded the pair `1` while every other pair
  was `0`, on a difference of meaning rather than of counts.

  Found by @OffgridwithJD, whose pytest twin already had the fixture and who then
  separated the two factors.

- The coalescing increment is documented as load-bearing for #84 (#1194).

  `test/native_reclaim_cycles.sh` reaches #84 only because it sets
  `pgcolumnar.reclaim_coalesce` off, which is a configuration no production cluster
  uses. Under the shipped default the #84 fix can be deleted and nothing notices.
  `reclaim_coalesce` carries its own `CommandCounterIncrement` on the free path, and
  that increment provides the same visibility.

  Nothing marked that line as load-bearing, so it reads as an optimisation detail
  beside the merge it belongs to. Measured on PG17:

  | build | configuration | result |
  | --- | --- | --- |
  | #84 fix removed, this increment kept | default | 12 passed + 0 failed |
  | both removed | default | 8 passed + 4 failed, `tuple already updated by self` |

  So deleting it is safe only while the #84 fix is present, and nothing in the tree
  would catch the two being deleted together. The comment says that, with the
  measurement, where the next person editing the line will read it.

- A pytest run killed with `SIGTERM` leaked its throwaway cluster (#1170).

  Python does not run `finally` blocks when the default `SIGTERM` disposition
  terminates the process, so the `pgc_cluster` fixture's teardown was unreachable for
  every interrupted run. Interrupting a run is normal and correct. The postmaster then
  survived holding its port, its shared memory and its datadir. Measured across the two
  development containers: 21 orphaned postmasters and about 2.5 GB of datadirs, the
  oldest 33 hours. One container lost background tasks to low memory as a result. A leaked cluster holding a port is also a false-red source. A later run then
  fails for a reason that has nothing to do with the code.

Measured by the identity of the postmaster each
  run started, killing at a delay counted from the moment it first appears:

  | kill delay | before | after |
  | --- | --- | --- |
  | `SIGTERM` at 0s | survived, datadir present | gone, datadir removed |
  | `SIGTERM` at 0.5s | survived, datadir present | gone, datadir removed |
  | `SIGTERM` at 2s | survived, datadir present | gone, datadir removed |
  | `SIGKILL` | survived, datadir present | survived, datadir present |

  The fixture installs a `SIGTERM` handler that raises `SystemExit(143)`, before
  anything is built or started, so the teardown that already existed becomes
  reachable. `SIGKILL` is uncatchable and still leaks, which is recorded rather than
  fixed.

  The 0s case needed a second change. `Cluster.stop()` returns at once when the
  interruption arrives during `start()`. An intermediate version therefore removed the
  tree under a live server, which is a leak with no name tag on the filesystem.
  `make_cluster` now refuses to remove a directory a postmaster still holds.

  The shell harness already handles this and is unchanged. `lib.sh`'s
  `trap pgc_teardown EXIT` runs on TERM, INT and HUP already. Naming the signals
  explicitly would make the handler fire twice.
- The dependency files #1158 generates were not ignored, so every built tree reported
  dirt.

  `-MMD -MP` writes one `.d` beside each object. Measured on a pristine clone at
  `7397731`, with nothing touched but a build:

  | | `git status --porcelain` |
  | --- | ---: |
  | before the build | 0 entries |
  | after the build | 36 entries, all `.d` |

  `git status` being empty is a premise both harness gates assert before they run, so
  this made it unusable. The files also sat directly beside the sources, in the
  position `git add -A` sweeps. `.gitignore`'s own last entry records nine git bundles that rode
  into a commit exactly that way.

  `*.d` is safe because nothing tracked matches it: `git ls-files '*.d'` is empty.
  `harness_selftest` part 550 gains the sibling of the arm it already had. The old arm
  asserts the dependency files exist; the new one asserts they are ignored, which is
  the half that was missing. Both rest on a premise that `git` can answer at all.
  Outside a repository `git status` prints nothing, and "nothing untracked" is what
  silence looks like. Reported by @OffgridwithJD.

- A header edit did not rebuild, so every header mutation proof ran against a stale
  object and reported a clean pass (#1158).

  PGXS generates header dependencies only when the server was configured with
  `--enable-depend`. `autodepend` is empty in the `Makefile.global` of every server
  this project builds against. `make` therefore saw `src/*.o` as newer than
  `src/*.c` and rebuilt nothing, however the headers changed. Measured on PG16, in
  one build directory so the two fingerprints are comparable:

  | | `pgcolumnar.so` after `make`, no `make clean` |
  | --- | --- |
  | before, header edited | `2b8bfc2a6d75`, byte-identical to the clean build |
  | after, header edited | `7cfcb28e9fe3`, moved from `74840637f16c` |

  A vacuous mutation reads as "the code is not load-bearing", which is the most
  expensive wrong answer a removal proof can give. CI was never affected, because
  every job builds from a fresh checkout. Local verification is where removal proofs
  are run.

  The top-level `Makefile` now passes `-MMD -MP` and includes the generated `.d`
  files, and `make clean` removes them. `objstore/Makefile` does the same for its own
  object, which includes a header from `src/`. `harness_selftest` part 550 asserts the
  outcome rather than the Makefile text. The tree it just built has one dependency
  file per object, 35 of 35, and 25 of them name `columnar.h`.

- `advisory_lock_class` discovered a maximum, so it went green exactly when the
  unique-key class regressed (#1154).

  The suite guards #430 -- no advisory lock pgColumnar takes may sit in a
  SQL-reachable class -- and found the lock it would assert about with
  `ORDER BY objsubid DESC LIMIT 1`. An insert takes more than one:
  `PGCOLUMNAR_LOCKCLASS_STORAGE_ROW` (102) as well as
  `PGCOLUMNAR_LOCKCLASS_UNIQUE_KEY` (103). **The maximum is the unique-key lock
  only while the unique-key lock is the highest-numbered one**, which stops being
  true in exactly the case the suite exists to catch.

  MEASURED, four cells on PG 18, the mutation being the #430 defect itself
  (`PGCOLUMNAR_LOCKCLASS_UNIQUE_KEY` back to `2`):

      suite      build      result
      current    clean      7 passed + 0 failed          <- control
      current    MUTATED    7 passed + 0 failed  PASSED  <- the defect, reported green
      repaired   MUTATED    5 passed + 2 FAILED  FAILED
      repaired   clean      7 passed + 0 failed

  Under the mutation the old suite discovered `classid=2 objid=1410065408
  field4=102` -- the storage-row lock, still unreachable -- and reported the
  property holding while the unique-key lock sat in the two-int4 space any
  application can address. The repaired suite names the tag instead:

      got [reachable: (16572,77,field4=2)] want [unreachable]

  ONE WRONG PREMISE DISABLED TWO ARMS. The discovery fed the contention arm, which
  contended for the tag it had discovered -- by then the wrong tag -- so nothing
  blocked and that arm passed too. Both come back together when the premise is
  fixed, which is why the fix is to assert over the SET: no lock an insert takes
  may be SQL-reachable, and every addressable tag is contended for rather than one.

  THREE SMALLER REPAIRS CAME OUT OF THE PROOF, each of which was hiding behind the
  first:

  - **The duplicate-key arm depended on an earlier arm having passed.** It
    re-inserted the key the contention arm had inserted, so the mutation reddened
    it as well -- three reds, of which the third was a cascade and had nothing to
    do with duplicate keys. It plants its own key now, which is what the pytest
    twin already did.
  - **The discovery raced the insert.** It polled `pg_locks` until a row appeared,
    which can be observed part-way through an insert that takes two locks. The
    probe now waits for the session to reach its `pg_sleep`, which is only true
    once the insert has returned.
  - **`psql -c` could not express that wait.** A multi-statement `-c` is ONE
    simple-query message, so `pg_stat_activity` carries the whole string from the
    first instant. The background sessions take their statements on stdin, one
    round trip each, so the view moves with them.

  NO CHECK NAME CHANGED, so assertion parity with `test/pytest/test_advisory_lock_class.py`
  holds and no ledger row moves. That port already asserted over the set (#1157);
  the shell suite is what was left.

  Gated: 7 passed on 15, 16, 17, 18 and 19; `shellcheck -S error` clean over the
  whole harness; the selftest corpus and the pytest guard leg green.
- `index_fetch_penalty_crossover` asserted a chosen plan node on a sampled estimate,
  and flaked twice in two days in the gate, once on `main` itself (#1168).

  The suite asserts that a 50,000-row correlated range uses the custom scan rather than
  a fetching index. That is the right property, and 50,000 is the worst place to ask
  it: the crossover sits just below. `ANALYZE` samples 300 x the statistics target
  rows. At the default 100 that is 30,000 of the fixture's 1,000,000, so the estimate
  feeding both prices moves every run. Bisecting K*, the smallest range at
  which the custom scan wins, over 30 `ANALYZE` cycles on PG17:

  | statistics target | n | K* mean | K* sd | K* min..max | estimate at 50,000 |
  | --- | ---: | ---: | ---: | --- | --- |
  | 100 (default) | 30 | 46,847 | 1,067 | 44,706..49,276 | 47,859..52,486 |
  | 1000 | 30 | 47,163 | 336 | 46,406..47,812 | 49,303..50,707 |
  | 3500 | 30 | 47,109 | 0 | 47,109..47,109 | 50,000 in 30 of 30 |
  | 3000 (control) | 30 | 47,072 | 64 | 46,933..47,167 | 49,906..50,189 |

  The fixture now sets `STATISTICS 3500` on the key column. 300 x 3500 exceeds the
  table, so `ANALYZE` reads all of it and the statistics stop being a draw. That is a
  structural fix, not a wider margin. K is unchanged, so the arm keeps every bit of the
  sensitivity it had. The premise is asserted rather than assumed, in both
  harnesses, by a new arm that fails with the estimate it saw. 3000 is the control. 300 x 3000
  is just under the table, and the wobble returns at sd 64, with the estimate exact in
  0 of 30 draws. The determinism comes from covering the table, not from a large
  target.

- `native_batch_fold_projection`'s "actually ran" arm read the plan's prediction
  (#1149).

  The arm is named `the ungrouped batch fold actually ran (Batch Fold: yes)` and
  asked a plain `EXPLAIN`. `columnar_vector.c` prints
  `haveStats ? batchFolded : batchEligible`, and `haveStats` is true only under
  `EXPLAIN (ANALYZE)` -- so the arm read the Begin-time shape prediction, and a fold
  that was predicted eligible and fell back at execution reported PASS.

  MEASURED, four cells on PG 18. The mutation returns early from
  `pgcolumnar_native_batch_fold` after the shape check, so the prediction is
  untouched and the fold does not run:

      suite                    build     the arm      .so
      plain EXPLAIN (before)   clean     PASS         19baa7b64232
      plain EXPLAIN (before)   MUTATED   PASS         efcf4b8bc011   <- the defect
      EXPLAIN ANALYZE (after)  MUTATED   FAIL         efcf4b8bc011
      EXPLAIN ANALYZE (after)  clean     PASS         19baa7b64232

      FAIL  the ungrouped batch fold actually ran (Batch Fold: yes):
            got [no (1 Batch Fold lines:  Columnar Batch Fold: no;)] want [yes]

  **Every other arm stayed green in both mutated cells**, including the one comparing
  the folded aggregate against the unfolded one: the fallback returns the right
  answer, which is exactly why nothing else could notice.

  The arm now carries its measurement when it fails -- the count of `Batch Fold`
  lines and the line itself, or the error -- rather than the empty string the old
  `[ ... ] && echo yes` produced. `TIMING OFF` keeps it a plan-shape assertion rather
  than a measurement, and stderr is kept so an erroring `ANALYZE` has something to
  show.

  No check name changed and the suite carries no ledger rows.

  Gated: 4 passed on 15, 16, 17, 18 and 19; `shellcheck -S error` clean; the selftest
  corpus and the pytest guard leg green.

- `native_groupagg`'s #349 cost arm passed against the defect it names (#1162).

  The arm asserts that the grouped path prices the folding it does. It compared
  one aggregate against ten, and the ten read ten DIFFERENT columns, so the two
  plans do not project the same data. A scan projecting ten columns costs more
  than one projecting one column whatever the folding charge is, and that
  difference alone kept the arm green.

  MEASURED on PG 18, 20,000 rows, mutation = the folding charge stops scaling with
  the aggregate count, which is the exact defect #349 fixed:

      shape                        control   mutant
      distinct columns, 1 agg       350.50   350.50
      distinct columns, 10 aggs    1253.01   803.01    <- 803.01 > 350.50: PASS
      one column,       1 agg       350.50   350.50
      one column,       10 aggs     800.50   350.50    <- the arm now reddens

  The 452.51 that survives the mutation is the scan's projection cost. The 450.00
  that does not is `cpu_operator_cost` x 20,000 rows x 9 extra aggregates, which is
  the term under test.

      before   PASS  ten aggregates cost more than one (#349)
      after    FAIL  ten aggregates cost more than one (#349):
                     got [no [1=35050 10=35050]] want [yes]

  THE PATH IS FORCED, and the reason is the same defect one level up. With the
  folding charge applied, ten `avg(a)` over one column is priced OUT of the grouped
  path and the planner picks core's aggregate node; removing the charge makes the
  grouped path win it. Without `enable_hashagg = off` and `enable_sort = off` the
  comparison silently changes which node it reads.

  So the arm gains the premises it never had: the fixture holds the rows the 450.00
  is computed from, and BOTH plans are the grouped node, asserted by the presence of
  `Columnar Vectorized Group Keys`, which no other node emits. The three names match
  `test/pytest/test_native_groupagg.py`, which already held the projection fixed and
  already carried those premises; the shell suite is what was left.

  Gated: 75 passed + 1 skipped on 18; `docs_style` PASSED; the selftest corpus and
  the pytest guard leg green.
- `arrow_import.sh` ran 16 of its 72 checks without pyarrow and reported `PASSED`
  (#1159).

  The three blocks that need a file a foreign producer wrote recorded nothing when
  they did not run. The accounting reconciles what ran against what was
  recorded, so it was satisfied by construction. A suite that runs fewer checks read
  exactly like a suite that has fewer. Two blocks printed an unrecorded line; the third, which
  carries 44 of the 56 lost checks, printed nothing at all.

  | | checks run | accounting | verdict |
  | --- | ---: | --- | --- |
  | before, with pyarrow | 72 | 72 passed, 0 skipped | PASSED |
  | before, without | 16 | 16 passed, 0 skipped | PASSED |
  | after, with pyarrow | 72 | 72 passed, 0 skipped | PASSED |
  | after, without | 72 | 16 passed, 56 skipped | PASSED |

  Each lost arm now records a skip under its own name, which is the pattern #994
  settled on in `native_parquet_flba.sh`. The run no longer shrinks: both
  configurations record the same 72 names, 0 in either direction of the difference.
  The three lists are measured rather than read off the source. The suite was run both ways and the lost
  records compared in execution order. They fall into exactly three contiguous runs
  of 2, 10 and 44, which is the three blocks. 51 guarded call sites produce 56 records, because some sit
  in loops. A list copied from the `check` lines would have been short by five.

- `iceberg_fdw.sh` refused to run without python3 and never used it (#1150).

  The gate was the only occurrence of `python3` in the file:

      iceberg_fdw.sh:22   python3 -c 'import json' 2>/dev/null \
                              || pgc_skip python "python3 is needed"

  Everything after it reads the committed warehouse under `test/fixtures/iceberg/`
  with `cp`, `psql` and the FDW. The Iceberg metadata is parsed by the extension,
  not by a helper script, and nothing in the suite parses JSON in the shell.

  IT COST SOMETHING REAL. `iceberg_fdw` was the tree's only declared-INCOMPLETE
  pytest pair, and this gate was the whole of the remaining gap: a refusal named
  after a dependency the port does not have would assert a precondition that is
  always met. With the gate gone the pair reaches zero and is declared COMPLETE,
  which leaves no declared-incomplete pair in the tree:

      before   literal 75 | template 0 | missing: 1   PORT IS INCOMPLETE
      after    literal 75 | template 0 | missing: 0   every bash property is covered

  THE SUITE HAS NO PYTHON3 DEPENDENCY; THE HARNESS STILL DOES. `lib.sh` shells to
  python3 for the source fingerprint and reports rather than swallows a miss. This
  removes one suite's vestigial gate, not the tree's use of python3.

  No ledger row moves: `grep -c '^iceberg_fdw' test/check_ledger.tsv` is 0, and the
  gate only ever recorded when python3 was ABSENT, so no clean run has emitted it.

- `parallel_scan_cost`'s two ratio arms rendered `halved` and threw the ratio away
  (#1164, found by #1174's widening).

  Both arms are `(r < 1.35) ? "io-kept" : "halved"`. The verdict named the bound
  being crossed and nothing about by how much, or from which two costs. They are
  two of the twenty-six arms #1174 uncovered when awk-rendered verdicts came into
  the sweep's scope. The failing branch now carries what it measured, in the same
  words the pytest twin already used:

      before   got [halved] want [io-kept]
      after    got [ratio 2.000 at or above 1.35 (serial 10625, parallel 5312.5)]
               want [io-kept]

  THE LIST SHRANK, WHICH IS THE ONLY DIRECTION IT MOVES: `test/lossy_arms.tsv`
  goes 103 rows to 101. The guard refuses both directions, so repairing an arm and
  leaving its row is a red by name -- measured here before the rows came out:

      stale: parallel_scan_cost: an I/O-dominated parallel scan is not priced
             at serial/workers (and the leader-participating arm)

  AND THE PUBLISHED PARTITION MOVED WITH IT. 540's header states the population in
  typed prose, which no check reads:

      carries        33 -> 35
      both constant  46 -> 44
      LOSSY          26 -> 24
      total         136 -> 136   (a repaired arm is still an awk-valued arm)

  Re-derived with an independent classifier rather than adjusted: it reproduces the
  committed 57/33/46 (20 excused, 26 lossy) on the unrepaired tree, and 57/35/44 (20
  excused, 24 lossy) on this one, with `inputs == sum(buckets)` true on both. The
  header now says why that block has to be re-derived by hand in the commit that
  repairs an arm.

  ONE DEFINITION OF THE BOUND, which the forced-failure run is what exposed. The
  first repair wrote `1.35` in the condition and again in the message, so a later
  edit could move the test and leave the message quoting the old threshold. That is
  the same defect one level along: a number the reader is told and nothing checks.
  `IO_BOUND` now feeds both.
- A base columnar scan was priced from the whole relation file, so adding a
  covering projection made the base scan look more expensive even though it
  still reads only the base storage.

  `pgcolumnar_relation_estimate_size` reported `smgrnblocks` of the main
  fork. That file holds the base plus every projection. The planner's
  `rel->pages` now subtracts the page-rounded footprints of sibling
  projections. Tables with no extra projection keep the same page count.

  Measured on PG18 with `seq_page_cost = 1000` and CPU terms zeroed, 20000
  rows: the base-scan run stayed 22000 after a covering projection grew the
  file from 180224 to 344064 bytes (ratio 1.000). Unfixed, the same scan
  jumped to 42000 (ratio 1.909).

- Five premise arms carried a verdict about a number they never printed (#1164).

  #1164's rule excuses a nothing-versus-something comparison -- `x > 0`, `n >= 1` --
  because exactly one value fails, so the boolean is a lossless encoding. **That is a
  property of a non-negative integer, not of the operator.** Over a signed operand the
  failing set has more than one member and the arm hides which one was reached, which
  is the defect the rule exists to refuse.

  THE POPULATION WAS COUNTED, not asserted. Twenty arms in the pytest corpus are
  excused by that carve-out. The operand of all twenty was read: eighteen are counts
  (`count(*)`, `cur.rowcount`, log-line tallies, buffer counts). Two are not, and both
  appear on the shell side as well.

  `reltuples` IS SIGNED. Core writes `-1` for "no statistics" (PG 14+), so the failing
  set is `{-1, 0}` and the branch named the member it had not read:

      before   got [ZERO] want [nonzero]              -- reltuples may be -1
      after    got [reltuples 10000] want [nonzero]

  Those are different defects. `0` is the #414 shift, where ANALYZE ran and mapped no
  block to its row group. `-1` is an ANALYZE that never reached the table at all. A
  reader following `ZERO` would look for the first and not find it.

  A DIFFERENCE OF TWO COSTS IS SIGNED BY CONSTRUCTION. `Total Cost - Startup Cost`
  collapsed to `yes`/`no` covered a degenerate plan (`total == startup`) and a
  cost-model inversion (`total < startup`) with one word:

      before   got [no] want [yes]
      after    got [run cost 9793.75 (total 9793.75, startup 0.00)] want [yes]
      after    got [serial 10625, parallel 9912.5] want [yes]

  **The corpus already disagreed with itself about this one.** In
  `test_parallel_scan_cost.py` the same premise appears twice, twelve lines apart;
  #1165 repaired the leader-off arm to carry both costs and walked past the leader-on
  arm below it. The carve-out is what made them look alike. Both shell arms were
  unrepaired.

  FIXED IN BOTH HARNESSES, each in its own. `test/analyze_reltuples.sh`,
  `test/parallel_scan_cost.sh` (two arms), `test/pytest/test_analyze_reltuples.py` and
  `test/pytest/test_parallel_scan_cost.py`. No check NAME changed, so assertion parity
  and the ledger keys are untouched.

  Every arm proved by mutating its threshold until it failed, asserting the mutation
  matched once, reading the message, and restoring byte-identical. Green either side:

      test/analyze_reltuples.sh       11 passed + 0 failed
      test/parallel_scan_cost.sh      13 passed + 0 failed
      the two pytest files            33 pass + 0 fail + 0 unrun

  Measured consequence, through #1166's sweep: carve-out beneficiaries 20 -> 18, live
  offenders none either way. The remaining eighteen are counts, and the gap is recorded
  rather than closed -- no AST sweep can tell a count from a signed value.
- Three TESTS.md sections documented other files' tests, and nothing could see it.

  `test_every_file_and_test_is_named_in_the_document` matches with `t not in text`
  over the WHOLE document, so a row naming a real test satisfies it wherever that row
  sits. The reverse arm passes for the same reason: a misplaced row names a test that
  exists, somewhere. **Between them they accept any permutation of every table in the
  file.** Presence is not placement.

  NINE OCCURRENCES OVER 237 COMMITS, and the mechanism is the MERGE. The rule was run
  over every commit that touches this file; six of the ten that introduced an
  occurrence are two-parent merges. The document is append-heavy, so two branches each
  add a section, git merges the text with no conflict, and a table lands under the
  wrong heading. Section 15 has been wrong since 2026-09-10 and 179 commits.

  WHAT THE EXISTING ARMS CATCH, measured by running them at each commit rather than by
  reasoning about them. A permutation that SPLITS a section duplicates a number, and
  `test_the_contents_list_is_numbered_in_order` and the link arm reject it at once --
  they did, on two commits of #1164. A permutation with the numbering intact is
  invisible: at both merges that produced the 44-48 rotation the arms were fully green.
  **And the obvious hand repair of the first produces the second** -- renumbering the
  split section and moving it to the end satisfied both arms and carried the previous
  section's table along, so red became green and the transposition survived. A green
  numbering arm after a conflict repair is not evidence the document is right.

  WHAT WAS ACTUALLY WRONG, measured before the guard was written:

      ## 15  eight rows the classifier file defines, left behind when ## 16 was split
             out of it -- interleaved among section 15's own rows, not appended, so
             no reader spots them
      ## 44  no table; its row sat in ## 47
      ## 46  no table; its two rows AND its `phs_nallocated` paragraph sat in ## 48
      ## 47  its row sat in ## 48
      ## 48  its row and its `serial_run / parallel_run` paragraph sat in ## 47

  44 through 48 are one four-section shift: each section's table and its load-bearing
  paragraph had come to rest under the NEXT heading. **Nothing here is a judgement
  about anyone's prose** -- each paragraph names its own subject, so its home is
  determined. Section 15's eight are removed rather than merged, because section 16
  already carries all eight with the descriptions its author wrote for that file.

  THE RULE IS KEYED ON THE ROW, NEVER ON THE HEADING, and that was learned the
  expensive way. A first draft matched `### Every test` and reported "4 tables, 0
  failing on main". This document spells that heading at least four ways -- `### Every
  test` (4), `### Every arm` (24), `### The arms`, and ninety-odd sections headed with
  a backticked test name -- so the draft measured 4 of 58 sections and called it the
  population. Row-keyed, main was never at zero:

      heading-keyed draft      4 sections     0 failing     (wrong population)
      row-keyed                58             3 failing

  Corrected in public before it was built on. A count is a claim about its glob.

  A `##` HEADING THAT IS NOT A FILE SECTION ENDS THE SECTION TOO. Without that, a row
  under ordinary prose is charged to the file section above it -- a false positive on
  correct writing, and one that names a section the row is not even in. Not live (five
  non-file `##` headings sit after a file section, no table rows under any of them) and
  reachable: two of the five are "What this corpus does NOT yet refuse" and "Traps this
  corpus records", exactly where somebody writes a table of test names.

  ONE DIRECTION ONLY. A row may not name a foreign test. A test having no row is a
  different property and a larger change; `test_harness_deps_classifier.py` is short
  one row today (`test_a_file_that_only_PARSES_a_driver_import_is_job_runnable`) and
  that is recorded rather than smuggled in.

  Proved by removal against the repaired document, not only on a fixture: one row
  moved into the previous section, restored byte-identical (`7f720954f8c6` both ways).

      got 'test_index_fetch_penalty_crossover.py: test_parallel_scan_cost belongs to
           test_parallel_scan_cost.py'   want 'none'

  The fixture arms carry the control beside the offender and the case that decided the
  rule's shape -- a table carried by no heading at all, which is how a quarter of this
  document is written and what a heading-keyed sweep would score clean.

      guard leg   393 passed, want 393 (391 on main after #1166, +2), DERIVED BY
                  COLLECTION on the merged tree rather than by adding this branch's
                  +2 to #1166's total -- both branches moved this line from 382
      cluster leg 462, re-derived in the same run and unmoved, as expected

- `analyze_function` ported to pytest (#414, #432).

  56 names, `missing: 0`, no extras. Core ANALYZE samples 30,000 rows and those rows
  are spread across every row group, so every group is decoded for every column.
  `pgcolumnar.analyze()` reads ONE column, and the statistics that come out of a full
  read are EXACT where core's are estimates. Exactness is the observable a sampled
  implementation cannot fake, which is what gets asserted.

  THE DISCRIMINATION DOES NOT DEPEND ON LUCK. `k` is one row in ten NULL, and 0.1 is
  a number a sampler reaches whenever it is lucky. `k7` is one row in SEVEN, so the
  truth is 0.142856 and core's estimate is always a whole number of sampled rows over
  30,000 -- there is no whole k with k/30000 = 0.142856. Core cannot report that
  fraction whatever it draws. An earlier version of the shell suite gated on core
  being WRONG and failed a correct suite about one run in 130 (#487); this file
  prints those figures and asserts only against independently counted truth.

  ONE NAMED REFUSAL, THREE UNNAMED. Four tests, split by fixture so only the first
  pays the 500,000-row build. The shell suite prints ONE `check_skip` and exits, so
  exactly one test carries `name="pgcolumnar.analyze()"` and the other three decline
  unnamed -- naming all four would publish three checks the original does not have.

  VERIFIED AT RUNTIME, not only in source. `compare_to_bash` grades the NAME and
  proves the string is PRESENT, never that anything emits it. Spying on
  `Expect._record`: PG18 emits 55 of the 56 (the refusal does not fire above 17), and
  PG17 emits `pgcolumnar.analyze()` with verdict UNRUN rather than laundered into a
  pass.

  Proved by removal against `pgcolumnar--1.0-alpha4.sql`; control 55 checks passed,
  each mutation asserted to have matched its anchor once and restored byte-identical:

      per-column target -> global default   the histogram honours the column's target
                                            (101 want 11), and the positional stride
      SET STATISTICS 0 skip disabled        ran for the zero-target column without
                                            raising (22012: division by zero)
      null_frac / non-null count            reports null_frac exactly (0.11111111 want
                                            0.1), and the DELETE arm (0.153846 want
                                            0.133333)

  THE `.so` HASH IS NOT THE INSTRUMENT FOR A SQL MUTATION and is deliberately not
  quoted: the C is unchanged, so the library hashes identically in all four cells and
  four identical hashes would look like evidence and be none. What establishes the
  mutant reached the server is the `.sql` md5 moving per cell, and `.sql` being in
  `pgc_fingerprint`'s ROOT_SUFFIXES so the build fingerprint moves and `build_once`
  reinstalls -- measured: 542af67fbc25 unmutated, 1cd8f666717b mutated, restored.

  `cluster_tests` 458 -> 462, derived by collection. The branch stated 446 before the
  rebase, correctly, against a main that #1167 has since moved.

- `native_groupagg` ported to pytest, with one arm stronger than the original
  because the original's cannot fail against the defect it names (#289, #432, #1162).

  33 names plus one template, `missing: 0`. The grouped vectorized aggregate is held
  to two oracles that are not substitutes for each other: a heap mirror, which cannot
  judge float summation order because the two storage types do not scan in the same
  order, and a toggle differential over the SAME columnar rows, which can. Every
  comparison carries a node premise -- a query the node quietly rejects runs the
  scalar Agg in BOTH arms, which is how `sum(real)` returning 0 once passed.

  THE #349 COST ARM. `native_groupagg.sh` asks ten aggregates over ten DIFFERENT
  columns to cost more than one over one, and a scan projecting ten columns costs
  more whatever the folding charge is. Measured with the charge stripped of its
  aggregate-count factor -- the exact defect #349 fixed:

      10 aggregates over 10 columns   control 1253.01   mutant 803.01   still green
      10 aggregates over 1 column     control  800.50   mutant 350.50   reddens

  The port holds the projection fixed and forces the path, so the difference is
  exactly the term under test: 450.00 = cpu_operator_cost x 20,000 rows x 9 extra
  aggregates. Forcing matters -- with the charge applied the planner prices that
  shape out of the grouped path, so an unforced comparison changes which node it
  reads. Two premises assert both costed plans ARE the grouped node. #1162 tracks
  the shell suite, which still has the hole.

  Proved by removal against `src/columnar_vector.c` on the Debian PostgreSQL 18 the
  CI cluster job uses. Control 85 checks passed; each mutation asserted to have
  matched its anchor exactly once and been restored byte-identical; `.so` hash per
  cell, because a mutation that never reached the binary is the failure a red arm
  cannot show:

      A control                          37d535c9dd69   85 pass
      B deterministic-collation gate     77a1d36c89a0   plan: non-deterministic collation key falls back
      C folding charge's naggs factor    f9dc2a9d5faa   ten aggregates cost more than one (#349)
      D group-estimate bound             7d25ab46e1d9   the bound is accurate ... (299940 want 12), +1
      E pseudoconstant qual rejection    f0ef8381416d   regress B2: gating WHERE falls back

  The ICU-gated collation pair is its own test, because `cannot_run` declares a whole
  TEST unrunnable while `check_skip` declines one check. Measured rather than assumed:
  the five local source builds are `--without-icu` and it declines there (run exits
  67); the `pg_config` the cluster job is given is `--with-icu`, so CI runs those
  arms. `pg_config --configure | grep -c icu` returns 1 for `--without-icu`, which is
  how the first reading of that was wrong.

  `cluster_tests` 442 -> 458, derived by collection. The branch stated 457 before the
  rebase, correctly, against a main that has since moved.

- A tolerance arm that collapses to a boolean cannot say which assertion failed,
  or by how much. 26 sites now carry their measurement into the failure message
  (#1164).

  `expect.num(1 if abs(on - off) <= 5 else 0, 1, name)` discards four measured
  buffer counts BEFORE asserting, so the failure can only ever read `got 0 want 1`.
  Two arms in `test_sorted_pathkeys.py` were the motivating pair: one is a 6-buffer
  spurious gap and the other a 14-buffer collapse, they are different findings with
  different owners, and a red said neither. They now read:

      got 'differs by 9 (on=227 off=218 over 1000 groups)'  want 'within 5'
      got 'margin 3 (on=45 off=42)'                         want 'reads more'

  THE PORT LOST A DIAGNOSTIC THE ORIGINAL HAD, which is the sharper half of this.
  `sorted_pathkeys.sh` already printed `on=`, `off=` and the group count on BOTH
  arms, and `parallel_am_scan.sh` already printed `no (${_pam_nreq:-none})`. Their
  ports dropped it. `compare_to_bash` grades the NAME, so two arms grade
  `missing: 0` while one prints the measurement and the other prints `got 0 want 1`
  -- name parity does not preserve diagnostics, and nothing in the harness can see
  the difference. `native_fetch_coalesce.sh` and `projection_scan_cost.sh` were
  defective on both sides and are fixed on both.

  THE POPULATION WAS MEASURED, NOT ESTIMATED, and the first two answers were wrong
  for reasons worth keeping. Over `test/pytest/*.py` by `ast` rather than by regex,
  because the shape appears inside docstrings in this tree and a text sweep flags
  its own documentation:

      190  expect.*(<conditional>, ...)
      148  ...both branches constant        -- 'Gather'/'none' fails informatively
       26  ...AND the failing set has more than one member
           AND neither branch carries the value

  Both halves of that last rule are load-bearing. `x > 0` and `n >= 1` over a count
  fail at exactly one value, so the boolean is a LOSSLESS encoding and `got 'no'
  want 'yes'` already determines the state: 9 such sites are deliberately unchanged.
  And a branch that merely NAMES the outcome is not carrying it -- `got 'halved'
  want 'io-kept'` tells the reader the verdict they already had and not the ratio
  that produced it. Excluding those cost 14 sites on the first pass.

  Each rewritten arm was proved to carry its measurement by mutating the threshold
  until it fails, restoring by inverse edit and re-hashing byte-identical:

      got 'wide 14 over the bound 25 (narrow 14 + 11)'      want 'coalesced'
      got 'no run cost: tight base'                         want 'all positive'
      got 'ratio 1.008 at or above 1.35 (serial 208206.25, parallel 206603.12)'
      got '259338 bytes over the 336000 ceiling (raw 960000)'
      got 'requested 1, needs 2'                            want 'requested'

  `test_differential.py` also put `got`, `want` and the relative difference into the
  check NAME. That does reach the reader, and it makes the name a different string
  on every run -- the name is what the parity grader matches, what the accounting
  counts and what a ledger row is keyed on. Moved to the compared value; the name
  now states the property and is stable.

  Left alone deliberately, with the reason recorded at the site: `check_num "$X"
  "$X"` in `sorted_pathkeys.sh` compares a value against itself, which the pytest
  layer refuses outright as a vacuity mode. `check_num` validates that BOTH sides
  are numbers before comparing, so through that helper a self-comparison IS a
  numeric-ness test -- an idiom, not a defect, and the reason the port had to
  express that premise a different way.

  No test is added or removed, so `cluster_tests` and `guard_tests` do not move;
  both re-derived by collection rather than assumed. All nine affected pairs still
  grade `missing: 0`.

- `analyze_differential` ported to pytest (#432).

  20 names, `missing: 0`, and no extras. `pgcolumnar.analyze()` writes through
  `pg_restore_attribute_stats`, which takes `VARIADIC "any"` and validates each
  argument's type at run time. A mistyped argument is not an error: the function
  warns, sets the argument to NULL and returns cleanly having stored nothing. A
  value-level suite cannot see that, because the function leaves kinds it was not
  given in place -- so a statistic we failed to write is still there wearing core's
  shape. Core ANALYZE is therefore the oracle and the comparison is over SHAPE.

  The refusal carries the shell suite's name, which #1131 made possible: on 15, 16
  and 17 `analyze_differential.sh` declines through `check_skip`, which RECORDS the
  refusal as "the differential analyze path". The port declines under the same name
  and the run exits 67.

  ONE ARM STRONGER THAN THE ORIGINAL. The shell suite names "could not read the
  server major, so the gate below cannot be trusted" inside a `pgc_fail` that fires
  only when the version is unreadable. The port asserts it on every run, so the
  version gate is known to have been decided on a real number.

  Proved by removal against `pgcolumnar--1.0-alpha4.sql`, each mutation asserted to
  have matched its anchor once and to have been restored byte-identical; control 27
  checks passed:

      mcvfreqs::real[] -> ::float8[]      and without a WARNING ...   got 10 want 0
      frequency / non-null count          every most-common value of i ...  got 2 want 0
      histogram_bounds -> typed NULL      and a histogram for exactly ...  got 0 want 4

  `cluster_tests` 441 -> 442, derived by collection.

- `advisory_lock_class` ported to pytest, asserting a stronger property than the
  original because the original's cannot fail when it matters (#432, #1154).

  8 names, `missing: 0`, and no extras: a one-to-one port. The shell suite discovers
  "the lock the insert took" as `ORDER BY objsubid DESC LIMIT 1`, but an insert takes
  more than one advisory lock, and the maximum is the unique-key lock only while the
  unique-key lock is the highest-numbered one. Regress `PGCOLUMNAR_LOCKCLASS_UNIQUE_KEY`
  to 2 -- the #430 defect -- and the maximum becomes 102, STORAGE_ROW, still
  unreachable: the suite reports the property holding while the lock under test sits in
  the SQL-addressable space, and the contention arm takes the wrong tag and passes too.

  The port collects EVERY advisory lock the insert holds, asserts none is in a
  SQL-reachable class, and contends for every addressable tag. Under the same mutation
  it reddens naming the offender:

      the lock an insert takes is not in a SQL-reachable class:
          got 'reachable [(16572, 77, 2)]' want 'unreachable'

  NO SLEEPS AND NO POLLING. The shell suite backgrounds a psql running pg_sleep(30),
  polls pg_locks up to sixty times each way, and must pg_terminate_backend rather than
  kill the client -- its own comment records that killing the client left the server
  holding the transaction and reported the same result with and without the fix. With
  real connections there is no window to wait on.

  `cluster_tests` 437 -> 440: three tests rather than one, because `cannot_run`
  declares a whole test unrunnable while `check_skip` skips a single check.

- `native_parquet_dict_oob` ported to pytest (#432).

  5 names, `missing: 0`. A Parquet dictionary index with the high bit set must not
  read out of bounds: the RLE_DICTIONARY path bounds-checked a file-controlled index
  with a SIGNED comparison, so an index of 0x80000000 sign-extends to a negative int,
  slips past the check, and reads about 16 GB past the dictionary.

  The gate carries the shell suite's name, which #1131 made possible. The fixtures are
  COPIED rather than read in place: the server runs as the postgres OS user while the
  checkout does not belong to it, and `read_parquet` opens the file as the server -- a
  permission failure there would look exactly like the rejection the suite exists to
  prove.

  Removal proof, restoring the signed comparison: the crafted file answers "server
  closed the connection unexpectedly" and the rejection arm reddens. On a real crash
  the arms after it never run, because the connection is gone; they cover the quieter
  world where the out-of-bounds read lands on mapped memory, returns garbage, and
  neither raises nor dies.

  `cluster_tests` 436 -> 437, re-derived by collection.

- `temporal` ported to pytest, the first pair #1131 unblocked (#432).

  5 names, `missing: 0`. PostgreSQL 18's `WITHOUT OVERLAPS` primary keys and 19's
  `UPDATE ... FOR PORTION OF` run through the constraint machinery pgColumnar
  integrates with, so a columnar table must behave exactly as a heap one does.

  `temporal.sh` gates on `btree_gist` and records that refusal under a NAME, which a
  port could not emit until #1131. Two refusals here and only one is a property: the
  version refusal is deliberately UNNAMED, because on 15/16/17 the shell suite exits
  without recording anything and there is no name to match.

  ONE ARM THE ORIGINAL DOES NOT HAVE. Both PK arms are satisfied by a table that
  rejected everything, and two empty tables compare equal -- so the hash comparison
  after them has the same hole. The port asserts the accepted rows are present first.

  `cluster_tests` 435 -> 436, re-derived by collection.
- A covering projection scan was priced from the base table's pages, so a
  column subset stored as its own row groups was quoted as a full-file read.

  `pgcolumnar_scan_io_run_cost` and the covering path in
  `PgColumnarSetRelPathlist` used `seq_page_cost * rel->pages`. The relation
  file holds the base plus every projection. The covering path now charges
  I/O from that projection's own page-rounded row groups, times the sort-key
  selectivity (already one-stripe floored). CPU still scales from the base
  run: decode work follows the rows, not the file. A lookup that cannot
  find that projection's storage falls back to rel->pages rather than
  one page, so a failed lookup cannot make the covering path look
  cheaper than the base scan.

  Measured on PG18 with `seq_page_cost = 1000` and CPU terms zeroed, 24000
  rows, a covering projection of `(ik, bulky)` beside the base: covering run
  41991.6 against base-page I/O 42000 (ratio 1.000) before, 19996 against
  42000 (ratio 0.476) after. The projection occupied 56366 of 344064 relation
  bytes.

- A named skip could not be ported: `cannot_run` recorded under its reason code, so
  66 suites and 1,281 names could not reach `missing: 0` (#1131, #1150).

  `check_skip`, `pgc_skip` and `pgc_fail` record under the NAME they are given.
  `expect.cannot_run` recorded under its REASON CODE, from a closed list, so a port
  could not emit the string its bash suite states and `compare_to_bash` reported it
  MISSING however faithfully the suite was ported. jd chose resolution 1 on #1131:
  the name rides alongside the code.

      cannot_run(reason, detail="", *, name=None)

  `name` IS KEYWORD-ONLY. `cannot_run(reason, detail)` is called positionally across
  the corpus, and a `name` parameter in second position would silently reinterpret
  every existing detail as a check name with nothing red to say so. The reason list
  is unchanged and still validated, the code still rides in `self.unrunnable` so the
  UNRUN line is untouched, and the outcome is still UNRUN rather than PASS -- a name
  that arrived by turning the declaration into a pass would satisfy the grader while
  asserting the missing dependency is present.

  `_NAME_ARG["cannot_run"]` becomes `None`, so an unnamed declaration states no
  property rather than publishing its reason code as a check name -- the rule
  `_names_in` already applies when it drops a bare `{}` template.

  ONE TEST RETIRED, NOT WEAKENED. `test_cannot_run_names_its_reason_not_its_detail`
  pinned "the reason CODE is the name", the least-bad answer while there was no name
  to harvest. `test_cannot_run_without_a_name_states_no_property` replaces it and the
  reasoning is in the file.

  THE LIMIT OF THE DECISION, found by hitting it: this unblocks a bash gate name
  whose PRECONDITION EXISTS on the pytest side. A gate on a dependency the port does
  not have stays unportable, and that is correct. `iceberg_fdw` goes from
  `missing: 2` to `missing: 1` -- `iceberg warehouse data files are missing` is now
  carried and matched; `python3 is needed` is not, because the port reads the
  committed warehouses and needs no python3. Nor does the shell suite: that gate is
  its only mention of python3 and nothing after it parses JSON, so it is filed as
  vestigial (#1150). Remove it and the pair reaches zero.

  `guard_tests` 380 -> 382, re-derived by collection.

- The validity bitmap was stored uncompressed and never elided, so a column with
  no nulls paid `ceil(rows / 8)` bytes for ever (#1130).

  A column chunk's page was always `[validity bitmap][encoded values]`. The
  bitmap is one bit per row and is written RAW, ahead of the block codec, which
  therefore never saw it. A `NOT NULL` column, or one that simply holds no nulls,
  still stored `ceil(rows / 8)` bytes of `0xFF`.

  The writer now omits the bitmap for a chunk that holds no null, and says so in
  the encoding descriptor's new flags byte. Measured on ClickBench
  `hits_0.parquet` (1,000,000 rows, 105 columns, no null in any of them),
  imported through `pgcolumnar.import_parquet` on PG17, both arms loaded into the
  same cluster on the same day:

      stored chunk bytes   78,109,810 -> 64,984,810   -16.80%
      validity bytes       13,125,000 ->          0
      relation size        78,381,056 -> 65,216,512
      chunks without a bitmap    0 of 735 -> 735 of 735

  The saving is exactly the bitmap: -13,125,000 bytes, which is
  105 columns x 125,000 bytes. Nothing else moved.

  **DECIDED FROM THE ROWS WRITTEN, not from the column's `NOT NULL` flag.** A
  nullable column whose rows happen to be complete gets the saving too, and a
  constraint added later cannot make an already-written chunk lie.

  **THE FORMAT MOVES: encoding descriptor version 2 -> 3.** Version 3 spends the
  byte version 2 wrote as a zero reserved byte, so every field keeps its offset
  and a version-2 descriptor is a version-3 one whose flags are clear. Readers
  accept 2 and 3; writers emit 3. Tables written by an older build keep reading
  with no conversion.

  **DOWNGRADING A BINARY BELOW THE ONE THAT WROTE THE TABLE IS NOT SUPPORTED**,
  and the shape of that failure is measured rather than asserted. An alpha4
  binary reading a table this build wrote: a sequential scan always raises
  `unrecognized native encoding descriptor`; an index fetch of a chunk narrower
  than the bitmap raises `validity bitmap longer than the chunk`; and an index
  fetch of a WIDER chunk returned NULL for a row that holds a value in 16 of 40
  single-row fetches, because the old reader tests a bit in bytes that are not a
  bitmap before it reaches any version check. #1137 tracks the versioning gap
  that makes the silent case possible.

  **THE BITMAP'S SIZE IS NOW A PROPERTY OF THE CHUNK, NOT THE ROW GROUP**, and
  three readers had to learn that, not one. The first implementation taught only
  the sequential scan, and the PG17 matrix went red in 33 suites --
  `native_index`'s point lookup returned NO ROW for a row that is there. The two
  are correlated rather than independent: `pgcolumnar_fetch_coalesce_read` skips
  any chunk whose `page_length` is below the group's bitmap size, and eliding the
  bitmap is exactly what takes a well-encoded chunk below it, so the chunks this
  change helps most are the ones that fall to the per-column fetch path.

  **A HARDENING GAP THE CHANGE'S OWN COMMENTS FORCED INTO THE OPEN.** The scan's
  fast path for fixed-width by-value types read a value without checking it
  against the end of the stream, trusting the validity bitmap to stop first.
  A synthesized all-ones bitmap is a new way to reach past it, so the fast path
  now carries the bound the general path has always had. Two more guards refuse
  a descriptor that claims no bitmap while accounting for fewer values than the
  group has rows, and a row count whose bitmap would not fit in memory. Both are
  asserted by SQLSTATE `XX001` against a poisoned catalog.

  THE BOUND ON THE FAST PATH COSTS NOTHING MEASURABLE. Backend instructions for
  `sum` over a 1,000,000-row bigint column, `cpu_core/instructions/` pinned to
  the P-cores, three repetitions per backend and two backends per arm:

      with the bound      6,819,185,307   6,811,086,223
      without it          7,408,953,743   7,394,084,968

  The bounded build is 7.9% LOWER, reproduced in a second build directory with
  the arms interleaved (6,818,778,149 / 6,801,164,841 against the same .so).
  WHAT THIS SUPPORTS IS THAT THE CHECK IS NOT A COST. It is not claimed as a
  saving, and the direction is unexplained: the compiled function is LARGER with
  the bound (97 instructions against 88, plus 69 bytes of cold block) and nothing
  else in the object moved, so a local codegen effect cannot produce a gap of
  197 instructions per row.

  THE PLAN AND THE WORK ARE THE SAME ON BOTH ARMS, which was the first thing to
  suspect and is now excluded. Captured per arm: the same plan node
  (`Custom Scan (PgColumnarScan)`), the same `actual rows=1000000`, the same
  7 chunk groups and 100 vectors decoded, and the same checksum over the column.
  So neither the plan nor the fixture explains it.

  WHAT DOES NARROW IT IS THE BRANCH COUNT. Measured beside the instructions:

      branches         1,679,418,138   against   1,675,963,100      +0.2%
      instructions     6,817,514,459   against   7,409,086,680      +8.7%

  Equal control flow, 590 million more instructions retired. So the difference
  is the instruction MIX in straight-line code and not more iterations of
  anything. The mechanism is still unidentified, and the number is recorded as
  what it is: the check is not a cost, and nothing further is claimed.

  `test/validity_elision.sh` and `test/pytest/test_validity_elision.py`, 25
  checks each, green on PG15 through PG19. Three mutations were run against
  them: forcing the writer's elision decision false reddens one arm, making the
  reader ignore the flag reddens ten, and using the group-wide size in the fetch
  path reddens exactly the three fetch arms. Ledger rows seeded from five real
  runs merged in one call, so each carries 15;16;17;18;19.
- Two delete-visibility suites ported to pytest, and a suite that names four access
  paths and takes two (#432, #1146).

  `native_delete_vector_index` and `native_delete_visibility_paths`, 11 names, each
  graded `missing: 0`.

  THE SHELL SUITE NAMES FOUR PATHS AND TAKES TWO. None of the `enable_*` scan GUCs
  governs `Custom Scan (PgColumnarScan)`, so its sequential, index/bitmap and
  index-only arms all plan the same node. The property is true -- each returns 4286 --
  but three arms are one piece of evidence counted three times.
  `pgcolumnar.enable_custom_scan = off` moves it, and each named path is then
  reachable and still correct: Seq Scan, Index Scan and Index Only Scan all return
  4286. The port forces the path each arm is named for and asserts the node before
  reading the count.

  AND THE INDEXED DELETE-VECTOR READ IS ONLY MEASURED ON A FRESH SESSION (#1146). The
  shell harness sends every statement through its own psql, so the scan it measures is
  always made by a session that did no writing. Measured on the same fixture:

      the scan runs in...                  idx_scan   seq_scan
      the session that wrote and deleted         62         20
      a fresh session                            21          0

  Twenty sequential catalog scans, one per row group, survive in the writing session.
  The port asserts the property the suite states, on a second connection, and asserts
  nothing about the writing session; the observation is filed rather than pinned.

  The delete_vector count is scoped by storage_id, because that catalog is
  database-wide and a pytest worker shares one cluster across the corpus -- unscoped,
  the arm would pass or fail on test order.

  No bash suite changes, so no ledger row moves and the census does not.
  `cluster_tests` 433 -> 435, re-derived by collection.

- Three reclaim and maintenance suites ported to pytest, and a guard that could not
  see its own defect (#432, #1138).

  `native_reclaim_cycles`, `native_reclaim_frag` and `native_vacuum_race`, 12 names,
  each graded `missing: 0`. Ports take 25 to 28 of the corpus.

  THE FIRST PORT CATCHES A DEFECT ITS ORIGINAL CANNOT. `native_reclaim_cycles.sh` is
  the declared regression guard for #84, and deleting the #84 fix leaves it reporting
  12 passed / 0 failed. `pgcolumnar.reclaim_coalesce` defaults ON, so compaction
  merges adjacent freed ranges and the free list holds one or two rows however much
  is freed -- and #84 needs one command to allocate from that list twice. Measured on
  the shell fixture, free_space rows per cycle: 0, 1, 2, 2, 2.

  The port runs its cycles with coalescing off, which keeps 18 separate ranges, and
  asserts that fragmentation as a premise so the suite cannot go quietly vacuous
  again. Two cells, each built from its own source:

      fix present (.so e95880e45673)   clean cycles, free list steady at 18
      fix removed (.so 42bb17933a55)   compact_rewrite cycle 1 returns a count
                                       (no self-conflict): got 'bad:tuple already
                                       updated by self'

  The shell suite is left alone; the fixture change is filed as #1138 rather than
  made in a pull request whose subject is the port.

  THE THIRD PORT REPLACES A CLOCK WITH AN ORDERING. `native_vacuum_race.sh`
  backgrounds a psql running pg_sleep(5), sleeps 2 in the shell, and inserts during
  the gap -- then redirects to /dev/null the one observation that would show the race
  happened. The port uses two connections and no sleeps, and asserts the
  interleaving: session B sees 50 rows before A commits and still sees 50 after, so
  A's rows are provably outside B's snapshot. Removal proof: take the caller's
  snapshot instead of a fresh post-lock one and vacuum() destroys 100 of 150 rows.

  The second port asserts the row count per coalesce mode before comparing them,
  because the original wraps its whole build in `>/dev/null 2>&1` and would report
  parity between two empty tables.

  No bash suite changes, so no ledger row moves and the census does not.
  `cluster_tests` 430 -> 433, re-derived by collection after the reseat.

- Three cost-model suites ported to pytest (#432).

  `native_index_fetch_stripe_cost`, `scan_decode_cost` and
  `index_fetch_penalty_width`, 18 names, each graded `missing: 0` by
  `compare_to_bash.py`. Ports take 22 to 25 of the corpus. Every cost quoted here and
  in the files came from ONE run, PG18 assert build, 2026-09-18. One subject: what the
  planner charges for the decode a scan or a fetch really performs.

  EACH PORT ASSERTS SOMETHING ITS BASH ORIGINAL DOES NOT.

      native_index_fetch_stripe_cost   the original moves the per-table
                                       stripe_row_limit and asserts two costs
                                       differ. Measured, the two arms are not the
                                       same plan: at its 100,000 rows the second
                                       is a Custom Scan, so the check compares the
                                       prices of two different nodes and cannot
                                       separate a re-priced fetch from a plan
                                       flip. The port matches at 1,000 rows, where
                                       both arms stay on the index, and asserts
                                       the shape before comparing the prices. It
                                       also adds the attribution cell: with
                                       enable_index_fetch_penalty off the same two
                                       arms price identically, 35.20 and 35.20
                                       against 51.70 and 808.98.

      scan_decode_cost                 the fixtures are smaller and the ratios
                                       were measured before they were shrunk. The
                                       column-count ratio is flat at 4.209 from
                                       100,000 rows to 2,000,000, so the original's
                                       2,000,000 buys the assertion nothing;
                                       200,000 is kept because it still spans more
                                       than one row group. The point-lookup arm is
                                       the one size decides, and its boundary was
                                       measured: a Custom Scan at 50,000 rows, the
                                       index from 100,000.

      index_fetch_penalty_width        plan shape is read from FORMAT JSON by
                                       exact Node Type equality rather than by
                                       grepping plan text, and the row-group
                                       geometry is read back from
                                       pgcolumnar.row_group (20 groups per arm)
                                       rather than assumed from the option that
                                       set it.

  THE ROW-GROUP LIMIT IS A PER-TABLE OPTION IN THE PORT, NOT A CLUSTER SETTING.
  The bash suite pins `pgcolumnar.stripe_row_limit=20000` in the cluster config so
  the writing and planning sessions cannot disagree (#806). A pytest cluster is
  shared by every test in the session, so that is not available -- and is not
  needed: `set_options` before the write is durable and is what both the writer
  and the planner read.

  REVIEW FIXES (@jdatcmd). The ordering arm's `FW <= FN` was satisfied by `FW = 0`,
  which is also what an over-charged width weight produces; the ladder now reaches
  below the shell suite's first rung, where the wide table does still fetch by index,
  so `FW` is a measured 5 and `FW > 0` is asserted beside it. And the `#171`
  point-lookup arm is labelled for what it actually guards: inflating the scan decode
  charge a millionfold leaves it passing (`Index Scan`, #503 ratio 4.209 -> 16.999),
  while inflating the index-fetch penalty reddens it (`got 'Custom Scan'`). It bounds
  a neighbouring subsystem, not this change, and nothing in either harness bounds
  this change from that side today.

  Every load-bearing arm carries a removal proof against the C, each mutation
  asserted to have applied and restored byte-identical:

      decode charge deleted (#503)        column-count ratio 4.209 -> 1.014
      width weight dropped (#768)         width ratio 7.303 -> 0.966, and the
                                          column-count arm stays green at 2.610
      prefix charged per column (#803)    the ordering inverts: the wide table
                                          fetches to 120 rows against the narrow
                                          table's 40
      per-table option branch removed     both stripe_row_limit arms collapse
                                          onto 810.46

  No bash suite changes, so no ledger row moves and the census does not.
  `cluster_tests` 427 -> 430, re-derived by collection on the reseated tree.
- A covering projection scan could not run in parallel.

  `PgColumnarSetRelPathlist` offered the covering projection as a serial
  CustomPath (`parallel_aware = false`, `parallel_safe = false`) and the
  parallel base scan as a partial path with no projection name. Those cannot
  both be true of one plan: either Gather wins and the projection is dropped,
  or the serial projection wins and the workers are dropped. Measured on a
  32,000-row scrambled table under parallel settings: the covering query
  planned as a serial `Columnar Projection` (`projection-only`), while the
  same query with the projection-scan GUC off planned Gather over a parallel
  base scan.

  The executor already partitions whatever storage `BeginCustomScan` opened
  (the DSM stripe counter is attached to `readState`). A partial covering
  path now carries the projection name, divides CPU the same way the parallel
  base path does, and keeps I/O undivided. After the change the same fixture
  plans `Gather` plus `Columnar Projection: byik` and still returns each
  covering row once. I/O is still the base relation's pages; pricing from the
  projection's own storage pages is a separate defect.

- Three suites ported to pytest, and the queue re-derived (#432).

  `analyze_reltuples`, `projection_update` and `projection_drop_column`, 21 names,
  each graded `missing: 0` by `compare_to_bash.py`. Ports take 17 to 20 of the
  corpus, and the batch is one pull request rather than three because the
  per-PR cost -- the census, the counts, TESTS.md and its anchor -- is paid once
  per review and not once per suite.

  EACH PORT ASSERTS SOMETHING ITS BASH ORIGINAL DOES NOT, which is the point of
  porting rather than translating:

      analyze_reltuples        the helper builds a columnar and a heap table from
                               two separate inserts and compares their row
                               estimates without checking they hold the same rows.
                               Both counts are asserted now. And the arm named
                               `20 stripes: within 5% of actual` never read the
                               geometry -- a single-group table passes that 5%
                               check too -- so the port reads pgcolumnar.row_group.

      projection_update        an UPDATE whose WHERE matched nothing leaves both
                               sides identical and every arm below it green,
                               having exercised no fan-out at all. The affected
                               row counts are pinned.

      projection_drop_column   the two non-owner arms exist to show a stranger
                               cannot tell a projected column from an unprojected
                               one. The bash suite asserts each against the
                               literal 42501 and leaves the reader to notice they
                               match; the port compares them to each other.

  THE SECOND ONE CAUGHT MY OWN PREMISE. I first asserted the non-owner could read
  the table, and the run returned 42501: USAGE on the schema resolves the NAME,
  while SELECT is a separate grant. The property the arms below it need is only
  that the name is not invisible, because 42P01 would mean they were asserting
  ownership against a table the role cannot see. Corrected to test for that.

  Row sets are compared as sorted tuples in Python rather than through
  `pgc_set_hash`, so the two harnesses stay independent by construction and a
  failure prints the rows that differ instead of two unequal hashes.

  No bash suite changes, so no ledger row moves and the census does not.
  `cluster_tests` 418 -> 421, re-derived by collection.
- An encoding was chosen on pre-codec bytes but the chunk is stored post-codec,
  so an encoding that shrank the bytes could enlarge the stored chunk (#1132).

  `PgColumnarEncodeChunk` picks the smallest candidate against `bestLen`, which
  starts at `rawLen`, and every comparison is on UNCOMPRESSED bytes. The block
  codec runs afterwards, once, over the whole encoded region, defaulting to zstd
  level 3. Bit-packing whitens a stream the codec was exploiting, so the two
  disagree -- and only the codec's answer is what gets written.

  FSST already decided this way, through `PgColumnarFsstHelpsCompressed`. The
  writer now asks the same question for the rest: it compresses the encoded
  region and the raw one and keeps whichever is smaller, per column chunk, which
  is the granularity the codec actually runs at.

  Measured on ClickBench `hits_0.parquet`, 1,000,000 rows and 105 columns,
  imported with `pgcolumnar.import_parquet` on PG17:

      stored total    81,869,112  ->  78,109,810      -4.59%
      NONE vectors           101  ->       1,621
      RLE / DICT / FOR      5372 / 3782 / 934  ->  4558 / 3390 / 621
      FSST                   310  ->         310      unchanged

  FSST is unchanged because it already had this gate; the encoders that did not
  are exactly the ones that moved. The worst single column, `ClientEventTime`,
  was stored 49.7% smaller. Its shape is why: rare outliers stretch the range
  frame-of-reference must size every value for, while the typical value's high
  bytes stay constant for the codec to compress. Row counts, two column sums and
  an md5 over `URL` are identical across the two loads.

  `pgcolumnar.enable_post_codec_encoding_choice` (default `on`) restores the old
  behaviour. It exists because the suites that test the ENCODERS need them to
  actually run: a fixture chosen to exercise frame-of-reference packing is not
  necessarily one where packing beats the codec.

- A covering projection was priced by clauses that merely mention its sort key,
  rather than by clauses it can prune on (#1126, the remainder of #1107).

  #1107 replaced a constant `0.5` with the selectivity of the clauses referencing
  `sortKey[0]`, which fixed the case where the selectivity came from a different
  column entirely. It left a narrower one: **a single `RestrictInfo` that ORs a
  sort-key range with a predicate on another column references the sort key**, so
  the membership test counted it whole and credited the projection with a
  selectivity its sort order cannot deliver.

      WHERE sk BETWEEN 1 AND 2000                  priced 0.100   earns it
      WHERE sk BETWEEN 1 AND 2000 OR kind = 'odd'  priced 0.101   earns nothing

  Measured on 20,000 rows, `stripe_row_limit => 1000`, scrambled physical order,
  projection sorted on `sk`, with `kind = 'odd'` where `sk % 1000 = 0` so its rows
  are spread across the whole `sk` domain. The second query prunes NOTHING and is
  slower than the base scan it undercuts tenfold:

      Columnar Usable Skip Predicates   0 with the projection, 0 without
      Columnar Vectors Skipped          0 with the projection, 0 without
      Columnar Chunk Groups Read       20 of 20, both ways
      Columnar Vector Decodes         120 with the projection, 80 without
      Execution Time                2.653 ms with, 1.996 ms without

  ASK THE FUNCTION THAT DECIDES SKIPPING. `pgcolumnar_clause_to_scankey` already
  answers "can this clause prune, and on which column" -- it returns 0 for a
  `BoolExpr`, because a BoolExpr is not an `OpExpr` and never becomes a scan key --
  and it records `sk_attno` per key. Pricing now keeps a clause only when it yields
  at least one key and every key it yields is on the sort key. One definition of
  "can skip", shared by the price and the executor, rather than a second one
  restated in the cost path.

  NOT GATED ON `exact`. The batch fold needs exactness because scan keys are its
  whole row filter (#715); pruning does not. An anchored `LIKE` (#426) and an
  IN-list range (#704) are conservative keys that prune honestly, and gating on
  exactness would decline a projection that genuinely wins. That is the silent
  direction -- a plan not taken reddens nothing -- so the arm for it ships beside
  the arm for the defect, and mutation-testing the gate onto `exact` reddens the
  IN-list arm exactly as intended.

  THE FIXTURE HAD TO CLEAR THE ONE-STRIPE FLOOR. The first version of this arm used
  a 100-row range; at 20 stripes the floor is 0.05 and both the fabricated discount
  and the honest one price there, so a broken guard and a working one were
  indistinguishable and the arm passed against the defect. The range is 10% now,
  and a premise check asserts it is above the floor so the arm cannot quietly
  return to being vacuous.

  NOT A REGRESSION FROM #1107: the old constant `0.5` also beat the base for this
  query and the planner also chose the projection. What changed is how confidently.
- Two guards read the prose describing their subject as if it were the subject
  (#1123). One of them reddened on a comment.

  `selftest/320` counted `MAJOR_FAIL=` over `run_all_versions.sh`, which carries 874
  comment lines, and expected zero. MEASURED: adding one comment saying the old
  spelling was `MAJOR_FAIL=` before #967 renamed it, and changing no code, took
  `harness_selftest` to `1080 passed + 1 failed`. The arm exists to keep that name
  retired, so the sentence that records the retirement is exactly what breaks it.

  `selftest/080` counted the bare word `grep` over `harness_selftest.sh`, a file of
  61 comment lines about readers. It passes today only because that file happens to
  contain the word zero times.

  Both now strip comments into a variable and read the variable, with a herestring
  rather than a pipe, which is the form part 080 itself requires (#486).

  PROVED IN FOUR ARMS, because a guard that stops flagging prose looks identical to
  one that stopped working:

      clean tree                              1081 passed + 0 failed
      the comment that reddened 320           1081 passed + 0 failed   (was 1080 + 1)
      a comment mentioning `grep`             1081 passed + 0 failed
      a REAL reader put back in the subject   1080 passed + 1 failed   <- still caught

  THE COUNT I COULD NOT PRODUCE WHEN I FILED #1123, produced. The first attempt
  extracted the surrounding `"$(grep ...` rather than the grep's own pattern and
  reported 54 "exposed" including obviously immune cases, so nothing was published.
  Parsing each `$(...)` body with `shlex` instead of regexing the line fixes it:

      126 sweeps over shell source
       34 anchored at ^, so a comment line cannot match
       92 unanchored
        2 unanchored AND over a commented file AND the pattern is a bare identifier

  Both of those two were real and both are fixed here. The wider 92 is a
  MEASUREMENT AND NOT A BUDGET: `/pbt/run\.sh$` is unanchored and cannot appear in
  prose, and deciding which patterns plausibly can is a judgment a guard should not
  pretend to make. `CONTEXT.md` carries the rule and the numbers; no gate is added
  on the 92.

  The justification in 080 also carried a stale count -- "60 lines, zero" for a file
  that is now 90 lines. Corrected, with the growth noted, because the exposure
  surface grew with it.

  No check names change, so no ledger row moves and the census stays at 1391.
- A covering projection scan could not run in parallel.

  `PgColumnarSetRelPathlist` offered the covering projection as a serial
  CustomPath (`parallel_aware = false`, `parallel_safe = false`) and the
  parallel base scan as a partial path with no projection name. Those cannot
  both be true of one plan: either Gather wins and the projection is dropped,
  or the serial projection wins and the workers are dropped. Measured on a
  32,000-row scrambled table under parallel settings: the covering query
  planned as a serial `Columnar Projection` (`projection-only`), while the
  same query with the projection-scan GUC off planned Gather over a parallel
  base scan.

  The executor already partitions whatever storage `BeginCustomScan` opened
  (the DSM stripe counter is attached to `readState`). A partial covering
  path now carries the projection name, divides CPU the same way the parallel
  base path does, and keeps I/O undivided. After the change the same fixture
  plans `Gather` plus `Columnar Projection: byik` and still returns each
  covering row once. I/O is still the base relation's pages; pricing from the
  projection's own storage pages is a separate defect.

- The union-merge page did not say why rebasing works where merging does not
  (#1116 follow-up).

  `CONTEXT.md` told a contributor to rebase rather than click *Update branch*, and
  gave the commands, but not the reason. The reason is the part that makes the advice
  transferable: **git reads `.gitattributes` from the tree it is merging INTO**, so a
  branch opened before the driver landed cannot use it.

  Measured on #1107, whose head predates #1108:

      merge main INTO the branch   conflicts: CHANGELOG.md  <derived files>
      rebase the branch ONTO main  conflicts:               <derived files>

  `grep -c 'CHANGELOG.md.*merge=union'` gives 0 on that head and 1 on main. Merging
  brings main's commits into a tree whose attributes have no driver; rebasing replays
  the branch onto main, where the driver is already in force. So for any branch older
  than the driver, *Update branch* cannot work even in principle.

  THE ARM FOR THIS WAS RED ON THE UNMUTATED TREE FIRST. Its grep spanned the prose's
  line break -- "the tree it is / merging **into**" -- so a line-based pattern found
  0 on a correct document. The mutation then reddened it as well, which looks like a
  working removal proof and is two failures agreeing. Re-anchored on a phrase that
  fits one line; the control is green and the mutation still reddens.
- A covering projection scan was priced at half the base scan for every
  restriction, then from every restriction's selectivity.

  `PgColumnarSetRelPathlist` offers a covering-projection path when a
  projection stores every referenced column and its leading sort key appears
  in a restriction. That path took the base custom-scan run cost and
  multiplied by 0.5. The constant does not depend on selectivity, so a 5
  percent range on the sort key was quoted the same as a 50 percent range.
  Measured on a 20,000-row table with scrambled insert order: both plans
  reported run-cost ratio 0.500 against the base scan.

  Replacing the constant with `rel->rows / rel->tuples` moved with
  selectivity, but `rel->rows` is the estimate after every restriction.
  The projection prunes only on `sortKey[0]`. A full-range sort key plus a
  rare non-sort-key column was then quoted at 0.077 of the base for work
  the sort order cannot reduce. The run cost now follows
  `clauselist_selectivity` over the clauses that reference that sort key,
  floored at one written stripe, and is not discounted twice when the heap
  layout already prunes as tightly. The same misattributed query reports
  1.000; a 5 percent range on the sort key still reports 0.050 against a
  50 percent range at 0.500.
- Eleven suites recorded every check against a major that is not a major (#1121).

  `pgc_record` writes `${PGC_MAJOR:-unknown}` and PGC_MAJOR is set inside
  `pgc_setup`. A suite that records but never calls `pgc_setup` wrote every check
  against the literal string `unknown`.

  THE GATE MATCHES A LEDGER ROW ONLY WHERE ITS MAJORS INTERSECT THE RUN'S, and no
  run ever observes `unknown`. So none of those checks could be seeded, and a row
  for one could never be matched again. Measured on PG17 before the fix:

      smoke 9   audit 31   objstore_stash_recovery 17   phase2 42   phase3 32
      phase4 38   phase5 36   phase6 43   decode_interrupts 29
      hilbert_curve 184   wal_envelope 20
      ---- 481 of 481 records named no major ----

  Same defect and same one-line fix as #1109, which reached `concurrency`,
  `unique_conc` and `update_conc`. After the fix all eleven record their real major
  and every record count is unchanged.

  THE THREE THAT TAKE NO PG_CONFIG read it from `$1`. The runner passes the
  pg_config to every suite, including those needing no cluster, and `pgc_major_of`
  yields empty on a path it cannot run -- so a bad path degrades to today's
  `unknown` rather than to a WRONG major. A guessed major would seed a row claiming
  a major the check was never observed on, which is worse than saying nothing.

  TWO GUARDS, BECAUSE NEITHER SUBSUMES THE OTHER. `run_all_versions.sh` refuses a
  major whose logs carry such a record, naming the suites: that reads what was
  actually written, but is silent about a suite nothing dispatched. Selftest 400
  sweeps the registered suites statically: that is decidable without running
  anything, but models how a suite gets its major rather than observing it.

  THE POPULATION WAS WRONG TWICE AND BOTH ERRORS WERE STATIC ONES. A sweep keyed on
  "defines no `check()` of its own" misses `audit`, whose own `check()` body calls
  `pgc_record`. One keyed on "the file contains `pgc_record`" misses
  `objstore_stash_recovery`, which uses lib.sh's `check()`. And excluding files that
  match `pgc_setup` dropped `decode_interrupts`, `hilbert_curve` and `wal_envelope`
  -- the three whose comments say pgc_setup is SKIPPED deliberately, matching the
  same grep a call would. The text explaining the behaviour was read as the
  behaviour. Selftest 400 carries both wrong regex spellings as fixtures.

  `pg_upgrade` meets several of those static tests and is NOT affected: it carries
  its own `check()` that never calls `pgc_record`, so it emits no records at all.
  Checked by running it.

- A `CONFLICTING` badge on a changelog entry is GitHub, not git (#1116).

  #996 gave `CHANGELOG.md` a union merge driver, and it does what it was argued to
  do: four branches have rebased onto it with zero conflicts, one `## [Unreleased]`,
  every entry present, `docs_style.sh` green.

  GITHUB DOES NOT HONOUR IT. Its mergeability calculation and its merge button do not
  read `.gitattributes`, so a pull request shows the red *This branch has conflicts*
  badge the moment another changelog entry lands -- while the same merge is clean on
  the contributor's machine. #1108's body asked this question explicitly and said it
  was untested; @jdatcmd tested it.

  Documented rather than worked around, which is what the measurement supports: the
  badge is wrong rather than harmful, and the cost #996 measured was nine local
  merges and rebases a day, which is gone. `CONTEXT.md` now says what the badge means
  and gives the three commands; `.gitattributes` records it beside the driver.

  Four arms in `docs_style.sh` hold the driver and the instruction TOGETHER, because
  the driver without the instruction is worse than either alone -- it makes the badge
  lie and gives nobody the reason. Removing the section reddens three of them.

- `PGC_SEED` seeded nothing: two fuzzer runs at one seed shared no fixture at all
  (#1011).

  `fuzz.sh` printed `reproduce a failure with PGC_SEED=<n>` on every run. Following
  that instruction did not reproduce anything.

  `RANDOM=$SEED` does seed the sequence in the shell that sets it. Every generator
  was read through a COMMAND SUBSTITUTION -- `N=$(( 1 + $(rnd 5000) ))` -- and
  `$( )` is a subshell. Bash re-seeds `RANDOM` in a subshell, deliberately, since
  5.1, so that two subshells do not yield the same value. Each call therefore drew
  from a fresh sequence and the seeded one was never read.

  Measured on this box, bash 5.3.9, five draws from one seed:

      subshell    run A: 739 227 141 262 904     run B: 798 969 493 460 795
      arithmetic  run A: 202 552 638 271 721     run B: 202 552 638 271 721

  `$(( ))` is arithmetic expansion and runs in the current shell, so the generators
  now ASSIGN rather than print, and no call site enters a subshell.

  End to end, `PGC_SEED=12345`, two runs each:

      main        first fixture differs; shared fixtures 0 of 5
      this change first fixture identical; shared fixtures 5 of 5
      seed 99999  different fixtures again, so it is seeded and not constant

  THE GUARD EXERCISES THE MECHANISM rather than grepping for the spelling: one arm
  shows a generator read through a subshell losing the seed on this bash, another
  shows the assigning form keeping it. Three static arms pin `fuzz.sh`'s own form,
  and putting the printing generator back reddens two of them.
- The collation guard could not see a process-substituted sort (#1112).

  `comm` requires both inputs sorted in ITS collation and does not check. Fed a
  mismatch it writes `input is not in sorted order` to stderr and prints a result
  anyway, so where stderr lands in a log nobody reads, a wrong set arrives looking
  like an answer. The inputs are not collation-insensitive: on real suite names
  `pgc_setup`/`pg_dump_roundtrip` and `projections`/`projection_update` both swap
  between `C` and `en_US.UTF-8`.

  `test/selftest/070` required every `sort` feeding a `comm` to carry `LC_ALL=C`,
  and matched only the PIPED form. A process-substituted sort is not a pipeline, so
  `run_all_versions.sh` used `comm` three times through substitutions, matched the
  pattern zero times, and read as compliant. One of the three had been there since
  #928. Found by OffgridwithJD reviewing #1110.

  PINNING THE COMM IS NOT ENOUGH, and that is the half that changes the fix rather
  than its description. `LC_ALL=C comm <(sort a) <(sort b)` pins only comm's own
  comparison: the substitutions run in subshells of the PARENT and inherit its
  locale. A guard accepting `LC_ALL=C` anywhere on the line would bless exactly the
  form a reader writes after reading the guard's name. The two halves are now
  separate checks.

  AND IT READS CODE ONLY. The guard scanned every line, prose included, so a comment
  explaining the rule violated it: a note reading "reads only the piped form"
  contained the literal string it grepped for and flagged its own file. A rule that
  cannot be written down is a rule people stop writing down.

  Both corpus arms report zero on this tree, measured before the change, so the
  detector is proved by PLANTING rather than by the corpus: the substituted form,
  the piped form, a half-pinned line, a pinned comm over unpinned sorts, and the two
  forms that must not be flagged.

  Mutation testing deleted a line from the first draft and NOTHING changed, so it
  was dead and is gone: both patterns require `sort` immediately after the `|` or
  the `<(`, which makes a mixed line fail without the substitution step the draft
  performed.
- A stale log and a deleted check were indistinguishable, so `orphan-scan` could
  report one as the other (#1073).

  `orphan-scan` refuses a ledger row that no record in its own part matches. A row
  whose check was ADDED after the log was written produces exactly that signal, and
  nothing in a RESULT record dates it against a tree.

  Measured when it was filed: replaying a log from one tree against the ledger one
  commit later reported two orphans, and both were checks that tree had just GAINED
  -- one step from a defect report against a tool merged an hour earlier.

  Nothing needed inventing. `test/lib.sh` already writes `-- source: <fingerprint>`
  into every log, from the one implementation in `test/pgc_fingerprint.py`. It needed
  READING. `--expect-source FINGERPRINT` refuses a log that does not carry the one
  the caller names, on `orphan-scan` and on `merge` -- and on merge it matters more,
  because a misread orphan is recoverable by looking again and a stale log stamped
  into the ledger persists.

  Reproduced end to end, with the control that makes the refusal mean something:

      without the flag          orphan: demo part1 one added since   rc=1
                                (reported DELETED; it was ADDED)
      --expect-source <new>     refused: "came from a different tree: it names
                                source aaaa1111bbbb, and you expected c9e65b1b35ba"
      a log FROM that tree      read, and the orphan STILL REPORTED  rc=1
                                (tree confirmed, so the orphan is a real finding)
      a log naming none         refused, because "does not disagree" is how an
                                opt-in check reports success having asked nothing

  OPT-IN, deliberately. A hand caller may not know the build its log came from, and a
  flag that refused every hand invocation is a flag nobody passes. Today's runner is
  safe by CONSTRUCTION -- it passes the logs from the run it has just finished -- and
  that is an argument for stating the guarantee, not for assuming the next caller
  inherits it. The runner now passes `--expect-source` from the same stamp it wrote,
  so the flag is exercised in production rather than only in its own arms: `lib.sh`
  and `pgc_fingerprint.py` both give `c9e65b1b35ba` on this tree.

  AN EMPTY EXPECTATION EXPECTS NOTHING, and the runner could pass one.
  `pgc_source_fingerprint` returns EMPTY with status 0 on both its failure paths --
  no `python3`, or the module erroring -- so on a box where the freshness machinery
  is broken the runner would pass `--expect-source ""`, and the tool's own opt-in
  rule would then skip the check entirely. That is "does not disagree" satisfying a
  guard, moved from the log to the expectation: precisely what this flag exists to
  refuse, one level out. Reported by jdatcmd.

      pgc_source_fingerprint /nonexistent/tree   -> value=[] rc=0
      PATH=/nonexistent pgc_source_fingerprint   -> value=[] rc=0

  The runner now refuses rather than scanning: an empty fingerprint sets the broken
  flag and the scan does not run at all, so it cannot report clean. The two halves
  do degrade together -- `lib.sh` cannot stamp the log either, so a log written then
  would be refused if the check ran -- but "it happens to be covered elsewhere" is
  how a guard stops being one.

  THE RUNNER STAMPS, NOT THE SUITES, and the first version had that wrong. The
  `-- source:` line is written by `pgc_setup`, and TWENTY-EIGHT files in `test/`
  never call it: they carry their own harness, deliberately, which is the population
  #1109 exists for. Fourteen REGISTERED suites among them produced logs that could
  not satisfy the flag, and CI refused every major:

      audit  concurrency  decode_interrupts  hilbert_curve  objstore_stash_recovery
      phase2  phase3  phase4  phase5  phase6  smoke  unique_conc  update_conc
      wal_envelope

  "Safe by construction" was true of PROVENANCE and not of STAMPING, and those are
  different properties. Reported by jdatcmd off CI.

  The runner owns every log, so it stamps every log: one helper, three sites,
  nothing asked of any suite. The stamp is written FIRST and the suite APPENDS --
  `_log_source_fingerprint` returns the first match, so a suite that also stamps
  gets one answer rather than two, and a `>` where a `>>` belongs would erase the
  line just written.

  COUNTED ON CALLS, NOT MENTIONS. A plain `grep -l pgc_setup` says twenty, because
  six of those fourteen name it only in a comment saying they skip it deliberately.
  Counting the mention would have put six suites on the wrong side of the claim.

  Verified end to end on a full one-major matrix, run alone:

      suite FAILs                               0
      command not found                         0
      names no source fingerprint               0
      came from a different tree                0
      source fingerprint could not be computed  0
      suites that ran: 256 of 258

  The third line is the claim: `orphan-scan` ran with a real expectation against
  every log in the batch and refused none. The fifth says the expectation was not
  empty, so the flag did work rather than being skipped.

  AND A DEFINITION-ORDER DEFECT FOUND BY RUNNING IT. The helper was first defined
  below its call sites; bash reads top to bottom, so the matrix printed
  `pgc_stamp_log: command not found` once per suite AND THE SUITE LOOP RAN ANYWAY.
  Every log would have arrived unstamped while the run looked normal -- the failure
  this change exists to prevent, reintroduced by ordering. No behavioural arm over
  the helper can see it, since the helper is correct and the file is not, so an arm
  compares the definition's line number against the first call's.

  `selftest/390` was re-anchored rather than changed in substance: it pinned the
  skip branch on the literal `>"$builddir/${s}.log"`, and that redirection had to
  become `>>`. The arm's subject -- that the branch which declines to dispatch is
  the thing that records it -- is unchanged.

  Both harnesses independently: twenty-three arms in `selftest/410`, nine in
  `test_mutation_ledger.py`, own fixtures and own names.

  A fixture error on the way, worth recording because it looked like a code defect:
  the first end-to-end run used `0ld7reefaaaa` as a fingerprint, which is not
  hexadecimal, so the tool reported "names no source fingerprint" where a mismatch
  was expected. The format check was right and the fixture was wrong.

- Every PR with a changelog entry conflicted with every other one (#996).

  Entries insert as the first child of a single heading, so two PRs that share no
  file but `CHANGELOG.md` still collide. Nine of 27 merges in one day touched it,
  and three merge commits that day exist only to resolve it.

  `.gitattributes` now gives `CHANGELOG.md` a union merge driver. Measured on the
  real pair, #1098 and #1106, which both open a new `## [Unreleased]`:

      default 3-way   rc=1, 2 conflict markers
      merge=union     rc=0, 0 markers, ONE [Unreleased], both entries intact

  The headings are not doubled because union emits identical lines once:
  7640 + 57 + 52 = 7749 against an actual 7744, the 5 being the shared
  `## [Unreleased]` / blank / `### Fixed` / blank prefix.

  UNION'S HAZARD IS REAL FOR THIS FILE, so the driver does not ship alone. Union
  keeps both sides of a divergent hunk with no marker, and a release cut EDITS the
  line a pending PR appends beneath. Reproduced: a PR merged into a release cut
  files its entry INSIDE the section that just shipped, rc=0, no marker. Today that
  case conflicts and a human sees it, so union alone would trade a loud daily cost
  for a silent one at every release.

  `test/docs_style.sh` therefore gains the check that catches it: each dated
  section must hold the entries its own tag shipped, and nothing else. THE KEY IS
  THE ENTRY, not a count -- counting would let one post-tag entry be swapped for
  another with the arm still green.

  IT FOUND ONE ALREADY ON MAIN. `## [1.0-alpha3]` carries an entry that
  `v1.0-alpha3` never shipped:

      - A pytest harness beside the bash suites, with a layer that refuses tests which

  added by `d978e7f` (#432) on 2026-09-09, seven days after the 2026-09-02 tag.
  Found by @jdatcmd in review.

  That entry cannot be corrected without making a second section wrong: the work
  shipped in the 1.0-alpha4 cycle, and the `v1.0-alpha4` tag does not carry it
  either. So a released section CAN diverge from its tag, but only by being
  recorded in `test/changelog_post_tag.txt` with a reason a reviewer sees in the
  diff. Two further arms guard that file: every row needs a reason, and no row may
  be stale.

  Seven mutations, each restored byte-exact: an entry added to closed alpha4, an
  entry removed from alpha2, the allowance row deleted, the allowance naming a
  different entry, the reason blanked, a stale allowance row, and the driver line
  removed. Each reddens its own arm; the clean tree is 42 checks, rc=0.

  THE FIRST VERSION OF THIS CHECK WAS MEASURED AGAINST A STALE TAG and reported the
  opposite. This tree's local `v1.0-alpha3` was `d9df031d` against the server's
  `cec9e9b5`, and `git fetch` never moves a tag that already exists. That produced
  a claim that `v1.0-alpha3` shipped with its section still named `## [Unreleased]`,
  a skip for it, and a "false-positive budget of 1 of 4" -- all three false, and
  the arm green where the tree was actually in violation. @jdatcmd caught it by
  checking their own refs against `git ls-remote` before contradicting the result.
  The suite now says how to check a tag before believing a red arm.

  Reaching for a skip there was also the wrong instrument, and two guards said so.
  This suite keeps its OWN tally and emits none of the machine RESULT vocabulary,
  so `lib.sh`'s `check_skip` is `command not found` inside it -- printing nothing,
  counting nothing, failing nothing, while the suite reports PASSED. A local one
  then tripped `selftest 400`, which refuses `echo "SKIP` in any file that calls
  `check`, because a SKIP is an outcome a count and a record must see. A property
  this suite cannot compare is now a `note()`: printed, counted as nothing,
  claiming no outcome.

  KNOWN LIMIT, stated because it changes what a green CI check means here: at depth
  1 with no tags every section arm is skipped, so CI cannot run any of this. It
  runs locally and in the five-major release gate, which is where a release is cut.
- Eleven timeout paths printed a FAIL that nothing recorded, and the accounting
  balanced at the wrong number (#965).

  `concurrency.sh`, `unique_conc.sh` and `update_conc.sh` bound every wait. On a
  timeout each printed `FAIL  timeout waiting for ...`, set the suite-local `fail`
  and returned -- touching neither `PGC_CHECKS` nor the record stream. The suite
  still exited 1, so this was never a false green. What it was is worse than a
  missing number:

      main, with a timeout induced      rc=1, CONCURRENCY TEST FAILED
        human FAIL lines          1
        RESULT records with FAIL  0
        RESULT records total      7
        checks run:               7

  Records and total agree, so nothing refuses the log -- on a run that printed a
  FAIL and exited 1. A missing figure can be noticed; one that reconciles cannot.

  Each path now records through `pgc_record`. The same induced timeout:

      this change                       rc=1, CONCURRENCY TEST FAILED
        RESULT records with FAIL  2
        RESULT records total      9
        checks run:               9

  THE NAME IS FIXED PER WAIT KIND, with the session and sentinel in the REASON
  field. The ledger is keyed on (suite, part, name), so interpolating
  `"$name/$label"` would mint rows nobody can enumerate and therefore nobody can
  seed. Four kinds: a command's sentinel, a standalone sentinel, a session
  blocking, a session reaching idle-in-transaction.

  It cannot be proved by running the suite green -- not one of these lines
  executes on a green run, so every count is identical whether the conversion is
  right, wrong or absent. Proved by lowering all four wait bounds from 1200 to 1
  in a scratch tree, with the rewritten count asserted before the run, and with
  unmodified `main` as the control. Green runs are unchanged: 7 records and
  `checks run: 7` before and after.

- Every record these three suites emitted named `major=unknown` (#965).

  `pgc_record` reads `${PGC_MAJOR:-unknown}`, and `PGC_MAJOR` is set by
  `pgc_setup`, which these three do not call -- they carry their own harness.
  Measured on a green run before the fix: 7 of 7 records said `unknown`. A ledger
  row claiming to hold on `unknown` matches no run, so none of these checks could
  ever have been seeded, which is part of why `suites_not_covered` has a floor
  here. One line each, from the `PG_CONFIG` they already resolve. All records now
  carry the real major: `concurrency` 7, `unique_conc` 31, `update_conc` 25, every
  one of them `18` on PG18, each reconciling with its own `checks run:`.
- The matrix summary printed a negative count of suites (#999, #1006).

  Every PG 17 matrix report on `main` printed a number that cannot exist:

      suites that ran: 243 of 252 (skipped: 9, incomplete: 0)
      of those, 248 accounted for their checks and -5 did not

  PG 18 printed `-2` the same day, from 246 ran. Both sessions filed it
  independently off the same runs.

  THE TWO TERMS COUNTED DIFFERENT POPULATIONS. `suites_ran` excludes a skipped
  suite. `_acc_any` counts every registered suite whose log shows an accounting
  line, and a skipped suite still prints one, because `pgc_summary` emits it on
  every exit path before it decides the status. Subtracting one from the other
  goes negative as soon as any suite skips and accounts.

  NOTHING CAUGHT IT BECAUSE THE RESIDUAL WAS DERIVED. `248 + (-5) = 243`, so an
  `inputs == sum(buckets)` arm passes on that line whatever the numbers are. It is
  the error `pgc_summary` warns about eight lines below its own counter, committed
  one level up.

  The residual is now a SET DIFFERENCE over the names the run recorded, and the
  other bucket is the intersection, so neither is the other's leftover and a
  negative is unrepresentable rather than merely detected. `pgc_tally_suite`
  records the name of every suite beside the counter it increments. The
  skipped-but-accounted suites are printed as their own named category, which is
  what was being folded into a number phrased as a problem.

  Guarded by `test/selftest/510-a-residual-must-be-counted.sh` and
  `test/pytest/test_residual_is_counted.py`, which assert the same properties
  through their own harnesses and name each other nowhere. Both drive the readers
  AND the summary block extracted from `run_all_versions.sh`, rather than grepping
  its text: proving the reader correct says nothing about what the summary prints
  with it. Removal proof, four mutations, each reddening named checks and each
  mutant asserted to still parse -- reversing the difference, dropping the sort,
  restoring the subtraction, and dropping one recorded name.
- `pgc_ledger merge` wrote a row covering one major and never said so (#1071).

  A contributor adds checks, runs the suite on ONE major, merges that log. The row
  lands with `majors = 18`. The gate considers a row only where its majors intersect
  the run's, so `suites (PG 18)` matches it and is green while `suites (PG 17)` reads
  it as a check the ledger has never seen and reddens -- naming the contributor's own
  checks `(on major 17)`, which reads as though their suite is broken on 17 when it
  passes there.

  FIVE AUTHORS IN A ROW hit it, including the person who wrote the tool, on a PR that
  was itself about ledger hygiene: #1039, #1063, #1065, #1068 and #1070 each wrote 6
  to 10 rows at `18` against a ledger where every other row carried `15;16;17;18;19`.
  When everyone makes the same mistake it is the tool's shape rather than five lapses.

  THE TOOL ALREADY KNEW. The distribution it prints for its summary line is computed
  from the same rows, so `merge` could see the new row was a strict subset of what the
  rest of the ledger carries, and said nothing.

  `merge` now warns, naming the rows, the set they carry, the set the rest of the
  ledger carries, and the majors the gate will redden on. The predicate is STRICT
  SUBSET rather than inequality, so a row naming a major the ledger has never carried
  -- how a new major legitimately enters -- is not warned about. Only rows the merge
  touched are candidates, or a partly-seeded ledger would reprint its own history on
  every merge.

  REPORTING, NOT A REFUSAL, deliberately. Seeding one major at a time is how a
  contributor without five installed majors makes progress, so refusing would block
  the honest case to catch the careless one. The gate still refuses later; this makes
  that refusal predictable at the moment it is caused.

  The gate's printed recipe said `<log>`, singular, so following it exactly produced
  the broken row. It now names one log per gated major and says why.
- The hand-rolled-inequality scan covered two operators of ten, so its scanned
  class was empty while 27 live sites sat outside it (#1030).

  `test_layer.py`'s `_hand_rolled_inequalities` refuses `int(<Compare>)` passed to
  an `expect` call -- the idiom that throws both values away. Its docstring said
  exactly that. The code required an `Eq` or a `NotEq`:

      and any(isinstance(op, (ast.NotEq, ast.Eq)) for op in arg.args[0].ops)

  So `int(a != b)` was refused and `int("x" in got)` was not. A name that outran
  its content, and the corpus reported clean because the shapes it actually used
  were the ones the scan could not see.

  Widened to any comparison, and to ANY boolean combination, which is the worse
  case: `int(a and b)` cannot say which half was false. The operator list is NAMED
  and pinned against `ast` itself, so a future operator reddens an arm rather than
  silently narrowing the rule.

  THE FIRST ATTEMPT AT THE BOOLEAN HALF INHERITED THE BUG IT WAS FIXING. It
  required a comparison inside the BoolOp, which left
  `int(p.exists() and q.exists())` live -- a PREMISE arm about a pair, whose whole
  job is to say which half is missing, reporting `got 0 want 1`. The operator
  widening fixed the comparison half completely and the boolean half kept the
  original narrowing. Reported by @jdatcmd, who probed the boundary rather than
  reading the branch. A single truthiness is still honest: `int(p.exists())` has
  one value and nothing to disambiguate; it is the COMBINING that loses the answer.

  THE POPULATION WAS 28, NOT THE 7 THE ISSUE MEASURED -- main moved between the
  measurement and the fix. Twenty-one `in`, five `> 0`, two boolean pairs, across
  eight files.

  `Expect.contains(got, want, name, absent=False)` is new, because 21 sites of one
  shape is a missing word in the vocabulary rather than 21 local mistakes. It
  reports what was actually there:

      collapsed : how-to names clustering: got 0 want 1
      contains  : how-to names clustering: 'cluster' is absent from 'this document
                  talks about join keys and nothing else at all'

  Its parameters are `got` and `want` deliberately: `test_failed_query_sentinel.py`
  partitions the layer's assertions by their first two parameter names, so any
  other spelling would have put it in neither bucket and opened the silent hole
  that file exists to refuse. It is registered in that file's shape table, so the
  failed-query sentinel sweep covers it like every other comparison.

  The five `int(len(x) > 0)` sites became `at_least`, which reports the number. The
  boolean pair became two `at_least` arms, each naming its own half.

  Removal proof, both directions:

      control                                        44 passed
      one collapsed `in` site put back               the sweep FAILS
      the same site, with the OLD Eq/NotEq scanner   the sweep PASSES

  The third line is the finding: the old scan reports a clean corpus with the
  collapsed site still in it.

  Eleven false-positive arms, five of them real `int()` calls from this corpus --
  a parsed regex group, a driver flag, a path premise, a value `num()` would refuse
  as a string, and a single call, which has one value and nothing to disambiguate.

  Guard half 347 passed, 913 checks; the two cluster-side files touched, 22 passed,
  79 checks. `guard_tests` re-derived by collection, 346 -> 347.
- A mutation with two genuine targets could not be recorded at all (#1014).

  `merge --mutation NAME` refused any run in which more than one check failed. The
  guard is right about the hazard -- attributing a mutation to a bystander records
  collateral damage as evidence -- and wrong about the remedy. A mutation with TWO
  GENUINE targets is ordinary, and for it the only permitted merge was
  `--reds-are-real`, which writes `-` in the mutation column.

  So the catalogue that column exists to become could never hold the entry it most
  exists for: the one that says WHICH CHECKS SHARE A CAUSE.

  `--target CHECK`, repeatable, makes the caller assert the attribution, exactly as
  `--reds-are-real` makes them assert that a red is real. The guard keeps its teeth
  in both directions:

      a red not named as a target   refused, and the refusal names it
      a target that did not fail    refused, because the claim is wrong
      --target without --mutation   refused, because it attributes nothing
      one red and no --target       merges, as every existing caller does

  THE TWO ROWS ARE BACK-FILLED, and the mutation was re-run rather than recalled.
  `native_join_runtime_filter`'s two ever-red rows now name
  `pgcolumnar.enable_join_runtime_filter boot value true -> false` instead of `-`.

  Getting there cost two wrong attempts, and the reason is a PostgreSQL fact worth
  writing down because it is not the obvious one.

  Mutating only the C initializer is INERT: 46 checks, 0 failed.
  `DefineCustomBoolVariable` assigns the boot value to the variable at registration,
  so for a bool GUC the initializer decides nothing at run time.

  Mutating only the `DefineCustomBoolVariable` boot value will not start at all:

      LOG:  GUC (PGC_BOOL) pgcolumnar.enable_join_runtime_filter, boot_val=0, C-var=1
      TRAP: failed Assert("check_GUC_init(variable)"), File: "guc.c", Line: 4944

  `check_GUC_init` does NOT require the two to agree. Read at the source rather
  than inferred from the trap, `src/backend/utils/misc/guc.c`:

      case PGC_BOOL:
          if (*conf->variable && !conf->boot_val)   /* traps: C-var true, boot false */

  It is ASYMMETRIC: a C variable left `true` against a `false` boot value traps, and
  a `false` initializer against a `true` boot value is accepted silently. `PGC_INT`
  and `PGC_REAL` apply the same asymmetry against zero; `PGC_ENUM` requires equality
  unconditionally.

  So this mutation is two lines, but not because the two must match -- because one
  line alone does nothing and the other alone will not boot. The third attempt
  changed both, fingerprinted the `.so` before and after to prove the build took
  (`53c3f626889c` -> `7f67e077f26c`), reddened exactly the two checks, and restored
  byte-exact with the `.so` back to `53c3f626889c`.

  The census does not move: both rows were already ever-red, so
  `checks_never_observed_red` stays at 1277 and the ledger stays 1285 rows, all
  carrying `15;16;17;18;19`.

  Both harnesses, independently: nine arms in `selftest/410` and six in
  `test_mutation_ledger.py`, each with its own fixture and its own names. Every
  refusal arm greps its MESSAGE rather than only its status -- `--target` did not
  exist before this change, so argparse exited 2 for an unknown flag, and three arms
  written against the status alone passed against the absent feature. Measured
  before the implementation, which is why they are written the other way.

- Four secret-leak claims over the PG server log could pass having read nothing
  (#1032).

  `iceberg_rest.sh`, `iceberg_rest_server.sh` (twice) and `iceberg_rest_vended.sh`
  each assert that a token or secret never reaches `$PGC_LOGFILE`, with
  `grep -c "$SECRET" "$PGC_LOGFILE"` compared against `0`. Nothing in the tree made
  any positive claim about that file: no suite asserted it exists, is readable, or
  holds a single line.

  THE HOLE IS EXACTLY ONE STATE, and the other three are already safe. Measured:

      a real server log   got=[0]  passes
      an EMPTY log        got=[0]  PASSES, having read nothing
      a missing log       got=[]   fails
      a log that LEAKS    got=[1]  fails

  `grep -c` prints nothing for a file it cannot open, so a missing path, an unset
  variable and an unreadable file all fail closed. A file that EXISTS AND IS EMPTY
  prints `0`, which is the value the claim wants. Anything leaving the log present
  and empty -- a rotation, a `log_destination` change, a truncating reuse path --
  turns all four green and says nothing.

  Each claim now carries its own premise immediately above it, so the two cannot
  drift apart:

      _ir_loglines="$(grep -c . "$PGC_LOGFILE" 2>/dev/null)" || _ir_loglines=0
      check "premise: the PG server log holds lines to search" \
      	"$([ "${_ir_loglines:-0}" -gt 0 ] && echo yes || echo no)" "yes"

  NOT the `grep -c . ... || echo 0` form: `grep -c` prints `0` AND exits 1 on an
  empty file, so that yields two lines and turns the comparison into a shell error
  rather than a comparison. The assignment form carries one value. Measured.

  Removal proof, at suite level: pointing both reads at a present-and-empty file
  gives `FAIL premise: the PG server log holds lines to search` while
  `PASS the token never appears in the server (PG) log` still stands -- the premise
  catching precisely what the claim cannot see.

  A first attempt at that proof truncated `$PGC_LOGFILE` in place and did NOT
  reproduce: the running postmaster wrote a line back within the same instant
  (`lines immediately after truncation: 1`), so the mutation never created the
  state it claimed to. Recorded because the invalid version looked like a passing
  control.

  The three suites already applied this discipline to their own request logs --
  every absence claim over `$REST_LOG` and `$OLOG` sits beside a positive grep
  returning `1`. The PG log was the one file they skipped.

- `native_upgrade_converge` staged its fixtures only when nothing was already
  installed, so a leftover install script won over the committed fixture (#1090).

  The suite creates an old-version extension to upgrade from, which needs the old
  base install script present in the extension directory. It staged each fixture
  `if [ -f "$src" ] && [ ! -f "$dst" ]`. This repository shipped
  `pgcolumnar--1.0-alpha2.sql` and `pgcolumnar--1.0-alpha3.sql` from the tree
  until the cycle-open rename moved them under `test/fixtures`, so every prefix
  installed before that still carries them and nothing prunes them. And because
  the leftover was not staged, the EXIT trap did not remove it either: it
  persisted and won again on every later run.

  IT FAILED IN BOTH DIRECTIONS, and the quiet one is the one that matters.
  Measured on one machine, same commit, two prefixes:

      leftover DIFFERS    PG15 holds the v1.0-alpha2 TAG content (fead351f84ca)
                          against the fixture's cbb4f36e4308. The suite reported
                          11 passed + 0 failed -- CONVERGED, having tested a file
                          nobody committed, with nothing in the output naming
                          which file it read.
      leftover BROKEN     a one-line invalid file gave 8 passed + 3 failed: a red
                          for a defect that is not in the tree at all.

  That the suite reads the leftover rather than the fixture was established with
  a discriminator that cannot be ambiguous -- a syntactically invalid leftover
  must fail if it is read and pass if it is not.

  Staging is now unconditional, and a pre-existing file is preserved and restored
  rather than deleted: the suite did not create it and must not change the state of
  a prefix it does not own. Its CONTENT is restored, not its every attribute --
  `cp -p` cannot give back an owner the suite does not have, and the leftovers on a
  developer's box are root-owned while the suite runs as `postgres`.

  The backup is taken only when there is not one already. `cp -p "$dst"
  "$dst.pgcbak"` run unconditionally destroys the original across a crashed run:
  the first run leaves the fixture installed and the original in `.pgcbak`, and the
  second overwrites the backup with the fixture. The pre-existing file is then gone
  for good, silently, by the route the preserve-rather-than-delete design exists to
  avoid. Those leftovers are the evidence for #901, so this is not hypothetical.
  A pre-existing `.pgcbak` also now fails a named check rather than passing
  unremarked, because it means an earlier run died mid-staging. Reported by
  jdatcmd.

  Two arms say so out loud, so a future change that reintroduces a conditional
  cannot pass silently: one pins that all three fixtures were staged, the other
  that each installed script IS the committed fixture. The count is pinned
  separately because without it a fixture that vanished would leave the content
  arm comparing nothing and reporting clean.

  Removal proof: restoring the conditional while keeping the arms gives
  `every staged install script is the committed fixture, not a leftover:
  got [1.0-alpha2] want []`, naming the script that was wrong.

## [1.0-alpha4] - 2026-09-17

### Fixed

- `docs/features.md` promised an encoding choice the writer does not make (#1074).

  The section said "Each chunk takes the encoding that makes it smallest". It does
  not. FSST is kept only when it beats the alternative by `fsst_min_gain_percent`.
  Both sides are measured after the block codec has run. Storing the FSST codes
  uncompressed is never one of the options compared. A codec strong enough to shrink
  the plain text past that margin drops FSST and can write more than `none` does.

  Reproduced on 200,000 rows of random hex text. Three arms write from one
  materialised corpus, so every arm sees identical input. The codec is read back
  from `pgcolumnar.column_chunk.block_codec` rather than assumed from the GUC:

      arm    page bytes    vs none    block_codec
      lz4    10,663,464    99.997%    none x4, lz4 x4
      none   10,663,789    100%       none x8
      zstd   10,853,334    101.777%   zstd x8

  The descriptor length is what names the mechanism. On the two text columns it is
  1870 and 1897 bytes under `none`. That is the FSST symbol table. Under `zstd` it
  is 280, which is FSST gone. `lz4` is unaffected because it declined on half the
  chunks and FSST stayed.

  Only the documentation changes here. The encoding cascade is untouched, and the
  fix #1074 proposes belongs with its own margin tests rather than beside a release.

- Two more suite headers told a reader to undo what had already been done (#1088).

  `fcfd3e6` fixed this class in `hilbert_cluster.sh`. Two files still carried it,
  and every claim in them is false against the tree:

      hilbert_locality.sh   "DELIBERATELY NOT REGISTERED ... makes that arm red
                            until then"          registered; 070's arm PASSES;
                                                 harness_selftest 967 + 0
      hilbert_curve.sh      "NOT REGISTERED"     registered
                            "src/columnar_curve.c does not exist"
                                                 127 lines, links into the .so
                            "070 reports it UNREGISTERED, which is the accurate
                             state"              070 reports the opposite

  `hilbert_curve.sh` also said registering the suite "belongs in the commit that
  adds the encoder". That commit landed.

  `hilbert_locality.sh` IS THE FILE THE PUBLISHED ADVICE CITES. `features.md`,
  `how-to.md` and `best-practices.md` quote 1.24x to 2.04x against Z-order, and
  that range comes from this suite's pins -- 2.0424, 1.6794, 1.4627 at
  ROWS=200000. A reader arriving from the docs to check the number was told the
  file is not in the matrix and is expected to redden the selftest.

  THE SWEEP THAT FOUND THEM NEEDED ITS CONTINUATIONS JOINED, which is why the
  class survived the first pass. `hilbert_locality.sh` wraps the phrase as
  `makes that arm` / `# red until then` across two comment lines, invisible to a
  line-oriented grep, and the search reported nothing. Reported by
  @OffgridwithJD, who checked before concluding the file was clean.

  So the rule for guarding this class is to match on REGISTRATION STATUS, which
  is structural, and never on the prose, which wraps. The sweep now used joins
  comment continuations first and compares each claim against the `SUITES` array.

  Comment only. No check name moves: `3cfc50038951` and `bf6154cc7070` before and
  after. Both suites green on PG17 non-assert, 65 and 184 checks, and
  `harness_selftest` unchanged.

- Hilbert clustering shipped in alpha4 and the pages a user reads never mentioned
  it (#1043 for the suite header).

  `pgcolumnar.cluster_hilbert` and `pgcolumnar.recluster_hilbert` were documented
  in `docs/sql-reference.md` and NOWHERE ELSE. `features.md`, `how-to.md` and
  `best-practices.md` all describe clustering as Z-order and name only `cluster`
  and `recluster`:

      sql-reference.md   cluster_hilbert 5 hits, recluster_hilbert 2
      features.md        0
      how-to.md          0
      best-practices.md  0
      user-guide.md      0

  So a reader following the discovery path picked Z-order and never learned the
  other option existed. It is the headline item of the release whose theme is
  skipping and layout, with a measured 1.24x to 2.04x advantage on range filters.

  All three pages now carry the verbs, the measured advantage, and the rule for
  choosing: Z-order for point lookups, Hilbert for ranges, measure when they look
  close. `how-to.md` also states that the curve is sticky ON THE SAME KEY, because
  that is the part which surprises people.

  THE QUALIFIER IS THE WHOLE CLAIM AND THE FIRST DRAFT DROPPED IT. `sql-reference.md`
  already said "plain `cluster` and `recluster` ON THE SAME KEY maintain that curve";
  the paraphrase written for `how-to.md` said it unconditionally.
  `cluster_inherited_curve` requires `sort_key_matches`, which compares the recorded
  key position by position, so the column ORDER is part of it:

      cluster_hilbert('h','a','b')   sorted_kind hilbert
      recluster('h','a','b')         sorted_kind hilbert
      recluster('h','b','a')         sorted_kind zorder

  The page now carries that sequence, because a reader copying the paragraph is
  exactly who needs it. Caught in review by @OffgridwithJD; the function's own
  header calls the condition the thing that keeps "sticky" from meaning
  "unescapable".

  AND THE SUITE HEADER TOLD A READER TO UNDO THE FEATURE (#1043).
  `test/hilbert_cluster.sh` said the suite is red on purpose, that neither verb
  exists, and that registering it in the matrix is future work. All three were
  true when written; `4b66555` carried out every instruction in them and left the
  paragraphs in place. Verified against the tree: both verbs are defined in
  `pgcolumnar--1.0-alpha4.sql`, the suite is registered, and it is green on all
  five majors.

  A stale instruction is worse than a stale fact, because it tells the next person
  to undo what was done. The one sentence that is still true is kept, and it is the
  trap the paragraphs existed to name: a shim renaming the Z-order verbs passes 162
  of 181 arms, and S5 is green on it BY CONSTRUCTION because over one column both
  curves are the identity.

  No check name moves: the sorted name list hashes `72eb1c4e4f42` before and after.
- `META.json` named an install script the distribution does not contain, and
  nothing had ever read it.

  It is the PGXN distribution metadata. It hardcodes the version twice and names
  the base install script by filename, and no suite, no Makefile rule and no CI
  step consumed it. It went stale for the whole alpha4 cycle:

      version                    1.0.0-alpha.3     while VERSION said 1.0-alpha4
      provides.pgcolumnar.file   pgcolumnar--1.0-alpha3.sql

  THE FILENAME IS THE HALF THAT MATTERS. `pgcolumnar--1.0-alpha3.sql` does not
  exist: this repository opens each cycle by RENAMING the base script to the new
  version. So the published metadata pointed at a file PGXN would not receive, and
  the two version strings were only the visible symptom. A reader comparing those
  against `VERSION` would have called it a cosmetic lag.

  Fixed, and now gated in both harnesses. `docs_style.sh` already had a section for
  exactly this class -- documents that hardcode a version beside a citation of the
  file holding it -- and `META.json` is the same shape with a filename added.
  `test/pytest/test_pgxn_metadata.py` asserts the same properties through its own
  parse and its own `git archive`.

  `git archive` rather than a filesystem test throughout, because the question is
  what the DISTRIBUTION contains. An `export-ignore` can drop a file that is plainly
  present on disk, which this repository has been bitten by before.

  A fourth arm covers what the third cannot: `provides.file` is ONE filename, so an
  `export-ignore` dropping a different install script would leave it green while
  `ALTER EXTENSION ... UPDATE` broke for anyone who installed from PGXN. It is on
  BOTH sides rather than in python alone, because the pytest guards run in CI while
  the five-major shell matrix is the release gate, and an arm protecting the upgrade
  path for published installs belongs in the gate that runs before a tag.

  ITS FIRST SHELL VERSION TRIPPED `selftest/080`, which is the rule working on
  arrival: the arm was fine in the pytest twin and only became subject to the
  no-pipe sweep when it entered the shell harness. The line was

      printf '%s\n' "$_meta_ship" | grep -qx "$_ms"

  a builtin writing a captured string into a reader that exits on its first match,
  which is #486 exactly. `$_meta_ship` is the whole `git archive | tar -t` listing,
  4999 bytes on this tree, right at the pipe-buffer boundary where the shape works
  almost every time. Replaced with a `case` over newline-sentinelled text, which
  gives the anchored match `grep -x` provided and starts no process. The failure
  direction was a false RED rather than a false green, so noisy rather than
  dangerous, but it was red in the gate the arm had just been moved into.

  The PGXN form is DERIVED from `VERSION` rather than hardcoded -- `1.0-alpha4` to
  `1.0.0-alpha.4`, and a future `1.0-beta1` to `1.0.0-beta.1` -- so the arms do not
  need editing at the next release. The transform has its own arm, because a
  derivation is a claim too: were it wrong, every other arm would compare against a
  wrong expectation.

  Removal proof, both harnesses: restore `META.json` as it shipped and every
  substantive arm reddens while every premise stays green. The premises hold because
  the file still parses and still names *a* script; it names the wrong one, which is
  the distinction the arms draw.

      docs_style.sh   3 arms red, premises green
      pytest          2 tests red, 9 pass + 2 fail + 0 unrun = 11

  THE TWO COUNTS DIFFER AND THAT IS NOT DRIFT. `docs_style.sh` splits the version
  property across two arms with its `_mk` loop, one for `version` and one for
  `provides.pgcolumnar.version`, while the pytest twin asserts both inside a single
  test. Three and two are the same three assertions counted by their harness's own
  unit. An earlier revision of this entry said "two on each side", which the table
  beside it already contradicted (@OffgridwithJD).

  `guard_tests` re-derived by collection 342 -> 346; `cluster_tests` unchanged at
  410, checked in the same run rather than assumed.

- One geometry arm accepted a comparison the property does not describe (#1081).

  #1081 replaced a count of `entry->fileOffset != rg->fileOffset` with a per-field
  membership loop, so that dropping any one of the four compared fields reddens by
  name. The loop matched `entry->$_f != rg->$_f` OR `entry->$_f != natts` for every
  field, because `natts` is the one field compared against the scan's own column
  count rather than against the row group.

  Harmless today -- `entry->firstRowNumber != natts` appears nowhere -- and still
  wrong as a claim: each arm would accept a comparison its own name denies. Three
  fields now match only the `rg` form, and `natts` has its own arm and its own name.

  Reported by @jdatcmd on #1081's review.

  All four verified by removal on the real suite, each reddening only its own arm:

      firstRowNumber removed   FAIL  a hit re-checks the group's firstRowNumber ...
      rowCount removed         FAIL  a hit re-checks the group's rowCount ...
      fileOffset removed       FAIL  a hit re-checks the group's fileOffset ...
      natts removed            FAIL  a hit re-checks the group's natts against the
                                     scan's column count
      restored                 26 passed, source byte-identical

  `firstRowNumber` is the one #1081 shipped unproven. Its mutation did not apply --
  that line starts `(entry->` rather than `entry->`, so the pattern missed -- and the
  harness's applied-assertion reported it rather than counting a clean run as a pass.

### Added

- Hilbert clustering: `pgcolumnar.cluster_hilbert` and
  `pgcolumnar.recluster_hilbert` (#889).

  **Two verbs rather than a parameter on the existing two.** PostgreSQL refuses
  to extend `cluster(regclass, VARIADIC name[])` in either direction: a defaulted
  parameter cannot precede a `VARIADIC` one, and an array-plus-kind overload
  makes the documented `cluster('t','a','b')` call ambiguous. Both were measured
  on 18.4. The new verbs match their siblings element for element in argument
  types, variadic element type, return type and volatility, so a caller switches
  between them by name alone.

  **What the curve buys.** Z-order jumps a long way in key space at a bit
  boundary; a Hilbert curve does not. Keys that are close in the data therefore
  stay closer in storage, the min/max zone maps over the clustered columns are
  tighter, and a range filter reads fewer chunk groups. The key is the same width
  and sorts through the same `bytea` comparator, so nothing downstream of the
  sort knows which curve produced it.

  **The curve is sticky.** `sorted_kind` is the table's declared intent, not a
  property of each call:

  - plain `recluster` on a Hilbert table over the same key is a no-op returning
    0, not a silent conversion back to Z-order;
  - `recluster_hilbert` on a Z-ordered table over the same columns rewrites it;
  - `vacuum_sorted` leaves a Hilbert table alone rather than sorting it
    lexicographically and relabelling it;
  - the maintenance daemon dispatches on the recorded kind, so a Hilbert table
    is re-clustered with Hilbert instead of being converted on a timer;
  - naming the other verb, or reclustering on a different key, is how a table
    changes curve.

  Held by `test/hilbert_cluster.sh` (181 arms) over the SQL surface, the recorded
  kind, both self-gates and the daemon, and by `test/hilbert_curve.sh` (184 arms)
  over the encoder itself.

- Six more guards counted their callers instead of pinning their property (#1078's
  class). Two were blind to the defect they name.

  #1078 repaired two arms in `native_fetch_projection.sh` that compared a whole-file
  `grep -c` against a literal. A sweep of `test/` found **twelve** sites of that shape;
  these are six of the remaining ten, in `native_fetch_cache.sh` and
  `native_fetch_position.sh`.

  THE REPAIR DIFFERS PER ARM, BECAUSE THE FAILURE DIRECTION DOES. A count over a call
  site is blind; a count over a guarded FORM is blind and noisy; and a count is CORRECT
  where the count is the property. Two arms in the sweep were left alone for that reason
  -- `decode_interrupts.sh`'s `^#define COLUMNAR_DECODE_INTERRUPT(i)` would be a
  redefinition if it appeared twice, and `native_saop_pushdown.sh`'s premise is
  load-bearing for an `awk` range that would silently concatenate two expressions.

      the entry key    both directions   -> self-referential, keyed N of N
      the cid reject   noise only        -> scoped to the function that must contain it
      the geometry     blind to 3 of 4   -> membership over all four compared fields
      the discard      proxy for "where" -> the two functions named
      rank, valOffset  noise only        -> scoped to pgcolumnar_fetch_row

  Measured, every mutation compiling so the suite rebuilds and runs end to end:

      case                        OLD arms            NEW arms
      unkeyed group lookup        key=1     PASS      RED  keyed 1 of 2
      second keyed lookup         key=2     RED       24 passed
      rowCount dropped            geom=1    PASS      RED  rowCount
      executor-end discard gone   discard=1 RED       RED  names the function
      third discard call          discard=3 RED       24 passed
      rank replaced by a walk     rank=0    RED       RED  rank prefix

  **Both blindness rows are the case for this change**: an unkeyed lookup and a dropped
  geometry field both leave the old arms green. The two noise rows are what fired on
  #1077 and cost a correct PR a red.

  No ledger change: `native_fetch_cache` and `native_fetch_position` have zero rows, so
  they are two of the 249 uncovered suites and no check name here is a ledger key.
  Verified rather than inherited.

- The piped-loop sweep reported a clean tree without reading one (#1033).

  `selftest/400` proves its detector FIRES -- a fixture with a check inside a piped
  loop gives 1 -- and nothing proved it had EXAMINED anything. Without `nullglob` a
  wrong `$PGC_TESTDIR` leaves both globs LITERAL, `awk` opens no file, `grep -c .`
  over no input prints `0`, and the arm compares that `0` against `0` and passes:

      PGC_TESTDIR=<a real dir with one offender>   hits=1   detector fires
      PGC_TESTDIR=/nonexistent                     hits=0   ARM PASSES, nothing read

  The same file already gets this right 260 lines above, where a different sweep
  carries `[ -e "$_sk_f" ] || continue` and a `premise: the sweep classified a corpus
  of check-calling files` arm. One sweep was premised and the other was not.

  The population is now counted by what `awk` actually OPENED -- `FNR == 1` fires once
  per file it reads -- rather than by `ls`, so a file that exists and cannot be read is
  a miss rather than an invisible one, and the premise uses the same mechanism as the
  detector so the two cannot drift apart. It is RECONCILED against what the globs
  offered rather than floored at a constant: a literal glob offers 2 words and reads 0.

  Removal proof: pointed at a nonexistent tree, `no suite calls a check inside a piped
  loop` still PASSES -- which is the defect -- while both premises redden.

  Closes the third of #1033's three gaps; `ae008c2` closed the other two.
- `test/pytest/test_projections.py`: the multiple-projections DDL, catalog and read
  path, ported from `test/projections.sh` (#432). All 75 of its check names, one for
  one.

  A projection is a second copy of some columns, and every property is about the copy
  staying honest: it holds the rows the base holds, it loses the rows the base loses, it
  survives a vacuum that renumbers every row underneath it, and the planner reads it only
  when it can answer the whole query from it. A wrong projection is a WRONG ANSWER rather
  than a slow one, because nothing downstream re-checks.

  THE PORT IS STRICTLY STRONGER IN ONE PLACE, and it is worth saying which way. The
  original's `expect_fail` helper runs the statement and passes when it errors AT ALL, so
  a misspelt table name satisfies every one of its eight refusal arms. The port asserts
  the SQLSTATE, and every code was measured against this build rather than guessed --
  42710, 42703, 22023, 42701, 22023, 42809, 22023, 42704, 42704. The names are the bash
  suite's; the assertions are not.

  Three other mechanism changes assert the same property by a stronger means: the
  `EXPLAIN` grep becomes a typed JSON field and reads the projection NAME rather than its
  presence; `pgc_set_hash` becomes `expect.row_set`, order-blind by declaration rather
  than by construction; and the second MVCC session becomes a second connection rather
  than a background `psql` on a fifo polled for a token, which removes the wait rather
  than shortening it.

  Mutation proof, each asserting it applied before its result was believed: removing the
  #875 projection-writer reset reddens both directions of the latch (`got 105 want 116`
  on the mid-transaction add, and the orphan storage the mid-transaction drop leaves);
  forcing the planner to refuse every projection reddens the covering query and the
  post-vacuum planner arm with `got None want 'pc'`, which is what proves the port reads
  the name rather than the presence. Restored, byte-identical: 75 checks, 0 fail.

- `test/pytest/test_sorted_pathkeys.py`: the ordered-scan surface, ported from
  `test/sorted_pathkeys.sh` (#432). All 110 of its check names, one for one.

  The bash suite pins one decision: when a columnar scan may hand the planner
  PATHKEYS, the promise that its rows already arrive in a stated order. The planner
  then drops the Sort above the scan, and nothing downstream re-checks. So a wrong
  promise is not a slow plan, it is WRONG ROWS.

  The port keeps the original's three shapes rather than reorganising by feature: a
  CLAIM arm (the Sort goes), a REFUSAL arm (something made the claim untrue and the
  Sort comes back), and an ANSWER arm (the rows themselves, against a heap table
  built from the same data). The ANSWER arm is not a duplicate of the CLAIM arm --
  dropping the Sort is only correct if the rows arrive sorted anyway, and a plan
  check alone cannot say whether they did.

  THE ONE ARM THAT NEEDED MORE THAN A PORT is `pgcolumnar.parallel_copy`, which
  prepares one transaction per worker. `max_prepared_transactions` cannot be raised
  without restarting the postmaster, and the default is 0, so asking for fewer
  workers does not help. `pgc_cluster` now sets it where it writes
  `postgresql.conf`, at the value `lib.sh` gives this suite through
  `PGC_EXTRA_CONF`. Refusing the arm instead was measured and rejected: it loses
  three names outright (`cannot_run` records under the REASON CODE, #1040 phase 0b)
  AND turns the `pytest (cluster tests)` job red, because an unrunnable check exits
  67 and the job runs pytest under `set -euo pipefail`. That half exits 0 today with
  zero unrun, so this file would have been the first to break it.

- `test_docs_cover_the_corpus.py` now refuses a NUMBERED SECTION WITH NO BODY.

  The arm above it asks whether each test file is NAMED by a numbered heading. A
  heading with no body is still a heading, so a section inserted into the gap between
  another heading and its body leaves both files named and one of them documented
  under the wrong title -- every existing arm green. `## 37. test_iceberg_fdw.py`
  reached `main` sitting directly above `## 38.`, with the Iceberg body attached to
  the userinfo heading. Fixed here, and the section order now matches the bodies.

### Changed

- `design/ROADMAP.md`'s block-compression entry and alpha4's plan item record the
  measurement instead of the claim (#890).

  The entry read as an outstanding task sourced to Zeng et al., VLDB 2024. It was
  measured on this engine and did not reproduce, and left as-is it would have
  regenerated the same issue against a different corpus in a year.

  The roadmap carries the five-shape table, the narrow reading of it -- the shape
  the pre-registered rule names as the DECIDER comes out UNDECIDED rather than
  NET WIN, so "the premise is refuted" is true only of the two most compressible
  shapes -- and the two reasons it retired: no storage tier exists to key a
  default off, and no shape fires NET COST.

  THE FIRST VERSION OF THIS ENTRY MIXED TWO RUNS IN ONE COLUMN, found in review
  by @OffgridwithJD. The summary table it was copied from labelled two of its
  read costs `0.92 (rep)` and `0.92 (mix)` -- shape names from an EARLIER
  three-shape run, with a different row count and wall-clock rather than
  instruction counts. Transcribing it dropped the parentheses, which were the
  only marker that those two values came from somewhere else. Reconciled against
  the five-shape run row by row, three of the five read costs were wrong:

      shape            C as written     C in the five-shape run
      repetitive           0.92                 1.001
      text_heavy            --                  0.911
      realistic            0.92                 1.001
      random_int           1.00                 1.000     matched
      incompressible       1.01                 1.010     matched

  Every `S` value was correct, and no verdict changes: `repetitive` and
  `text_heavy` are NET WIN on `S` alone, and the three UNDECIDED rows stay
  UNDECIDED because their `C` was already below the 1.10 threshold. So the
  conclusion never depended on the wrong numbers -- which is exactly why nothing
  would have caught them.

  `text_heavy`'s blank was the costly one. Its real `C` of 0.911 is the most
  favourable read cost in the set, so omitting it UNDERSTATED the case the table
  makes.

  The entry now also carries the conditions -- rows, build, counter, and that
  the codec was asserted from `column_chunk.block_codec` rather than assumed --
  plus the limit that an instruction count cannot see an I/O saving at ANY
  working-set size. #890 learned that when a table went out from an assert build
  without naming it; the roadmap is where someone goes to reopen this, so the
  header belongs there more than on the issue.

  Two things kept that would otherwise be lost with it. Dictionary encoding on
  float columns was a SEPARATE clause of the same roadmap entry, nothing above
  measures it, and it stays open. And the alpha4 item is marked as having cost
  nothing in written bytes, so it need not have been alpha work at all -- it was
  scheduled as alpha because the plan assumed it would change the writer, and
  that assumption was the part that needed checking first.

  Three defects found by the investigation are tracked separately and do not
  close with it: #1074, #1075, #1076.


### Fixed

- `fsst_margin.sh`'s low-margin arm looks redundant and is not: it falsifies the
  descriptor reader.

  The three arms across sections 1 and 2b bracket the FSST decision, and the
  bracket also falsifies `fsst_vectors` itself. Nothing said so, and the
  low-margin arm reads as a weaker duplicate of the margin-90 one, so it is the
  obvious thing to delete in a tidy-up.

  Measured by evaluating the arms against a stubbed reader rather than argued:

      reader always returns 0    low-margin RED   margin-90 PASS   codec RED
      reader always returns >0   low-margin PASS  margin-90 RED    codec PASS
      the real tree              PASS             PASS             PASS

  `fsst_vectors` parses the encoding descriptor by byte offset,
  `get_byte(descriptor, 6 + i * 13)`, which is the kind of reader that goes
  silently wrong on a format change and returns a plausible number. With only the
  margin-90 arm, a reader stuck at 0 reads as "FSST was dropped" and every arm is
  green.

  AND THE TWO MARGINS ARE THE GUC'S OWN ENDPOINTS. `columnar_tableam.c:3269`
  declares it `5, 0, 99`, so the bracket uses the extremes the setting admits
  rather than an arbitrary low and high. That is what makes each half the
  strongest form: at margin 0 the keep test is "any compressed win at all keeps
  FSST", in the GUC's own help text, so `kept == 0` there would mean FSST never
  helps on this corpus rather than merely not helping enough. Raising it to 5 to
  simplify the arm would lose that with nothing to show it went.

  Comment only. No check name moves: the sorted name list hashes `62d1b8a49926`
  before and after.

  Found by @OffgridwithJD reviewing #1082, which had already merged, and the
  endpoint reading is theirs too. The table is measured rather than argued, and
  the range was read from the declaration rather than taken on trust.

- The arm named "the visibility-only caller decodes nothing" passed on a tree
  where it decoded (#1077 sweep).

  Two arms in `native_fetch_projection.sh` counted a CALL SITE, with its argument
  text, pinned at a literal `1`:

      grep -c 'PgColumnarRowIsLive(rel, snap, baseRow)'            "1"
      grep -c 'PgColumnarReadRowByNumberCols(rel, snap, baseRow'   "1"

  That asserts a particular call is still written the way it was written, which is
  not the property either name claims. Plant the regression they exist to catch --
  a full `PgColumnarReadRowByNumber` beside the liveness check in the visibility
  path -- and both stay green, because the call they count is still there and the
  decode added next to it is invisible to them:

      state                          live cols full | OLD1  OLD2 | NEW
      clean                             1    1    0 | PASS  PASS | PASS
      REGRESSED, full decode added      1    1    1 | PASS  PASS | RED
      honest 2nd narrow caller          2    1    0 | PASS  PASS | PASS
      empty file                        0    0    0 | RED   RED  | premises RED

  Unlike the `allColumns` pair repaired in `8e88f42`, these fail in ONE direction
  only. A realistic second caller uses different variable names, so the exact-text
  count stays at 1 and the old arm is BLIND rather than falsely alarmed. Row 3 is a
  pass for the old arms by accident, not by correctness.

  THE PROPERTY IS A ZERO. Three entry points exist and only one decodes every
  column, so the projection path is asserted never to call it. More correct callers
  of the two narrow entry points move nothing; any full decode moves it off zero --
  the opposite failure direction from a count pinned at 1.

  Three premises keep the zero from being vacuous: each narrow entry point is
  called at all, and that `PgColumnarReadRowByNumberCols(` does not match
  `PgColumnarReadRowByNumber(`. That third one DOCUMENTS the assumption rather than
  providing the guarantee, which the first version of its comment got wrong: a
  broken prefix would make `full >= cols`, and `cols >= 1` with `full == 0` is a
  contradiction, so the main arm reddens rather than hiding. Corrected in review by
  @OffgridwithJD.

  Removal proof, against the real suite: md5 `c8324ce4f486` -> `a2c26702a9cc`, full
  decodes 0 -> 1, `16 passed + 1 failed + 0 unrunnable + 0 skipped = 17`, one red
  naming `got [1 full decode(s)] want [0 full decode(s)]`, all three premises green,
  restored with an empty diff.

  The fourth and fifth arm in this file repaired for counting a string across a
  whole file; `8e88f42` did the second and third and the `deltuples` comment
  records the first.
- The docs said `pgcolumnar.compression` picks a codec. It also picks encodings
  (#1076), and two things they told users about `none` are measurably wrong
  (#1074).

  `columnar_encoding.c` returns "FSST helps" UNCONDITIONALLY when the codec is
  `none`, before it looks at the corpus or the margin:

      if (compressionType == COLUMNAR_COMPRESSION_NONE)
          return true;

  So `none` is a DIFFERENT cascade, not the same cascade with the codec removed.
  Nothing in `docs/` said so. `configuration.md` called the setting "default codec
  for new chunks", which is exactly the narrow reading that makes the coupling
  invisible.

  Two user-facing claims fell out of it. `administration.md` described `none` as
  "Lowest write cost, largest size". Measured on 200,000 rows each, backend
  instruction counts, `zstd` against `none`:

      high-entropy hex text     0.92     zstd CHEAPER to write
      repetitive text           0.99     zstd CHEAPER to write

  Neither shape made `none` the cheaper one, and on the hex shape `zstd` also
  produced a table 1.4 percent LARGER. `best-practices.md` said each chunk "takes
  the encoding that makes it smallest", which is wrong twice: the target is the
  smallest STORED result, so it depends on the codec, and a FSST win below
  `fsst_min_gain_percent` is given up on purpose.

  All three pages corrected, with a new `Compression and the encoding cascade`
  section carrying the mechanism and both measurements.

  ARMS IN BOTH HARNESSES, and each is a DIFFERENTIAL rather than a bare assertion.
  "FSST is kept under `none`" alone is satisfied by any corpus FSST always wins on,
  so every arm is paired with the same corpus at the same margin WITH a codec,
  where it is dropped. `fsst_margin.sh` gains three checks; the independent
  `test/pytest/test_compression_reaches_the_cascade.py` builds its own corpus and
  parses the descriptor itself.

  Removal proof: deleting the early return and rebuilding moves the `.so`
  `c8e5c790dbac` -> `6455728725e5`, and reddens exactly the codec arms on both
  sides while every content invariant stays green:

      fsst_margin.sh    14 passed + 2 failed + 0 unrunnable + 0 skipped = 16
      pytest            8 pass + 2 fail + 0 unrun = 10

  The mutant still writes correct rows, which is why the content arms hold: this
  is a decision changing, not data corruption.
- The block codec's buffer was never freed, on either path (#1075).

  `flush_one_column` compresses the whole encoded region of a column chunk and
  then leaves the codec's buffer allocated:

      PgColumnarCompressValueStream(encoded->data, encoded->len, ..., &compData, ...);
      if (usedType != COLUMNAR_COMPRESSION_NONE)
      {
          finalData = compData; ...
      }
      if (finalLen > 0)
          appendBinaryStringInfo(chunk, finalData, finalLen);

  BOTH paths leave it dead. When the codec declines,
  `PgColumnarCompressValueStream` returns a palloc'd COPY of the raw bytes rather
  than NULL, by its documented contract, and this caller never reads it --
  `finalData` still points at `encoded->data`. When the codec succeeds,
  `appendBinaryStringInfo` has already copied the bytes into `chunk`.

  It is bounded rather than a leak: `flushContext` is deleted per row group. What
  it costs is peak allocation, because it roughly doubles what the flush holds for
  the encoded region while every other column is still flushing.

  Freed after the append rather than inside either branch, so the success path is
  covered too. A free placed only in the declined arm is the version that reads as
  complete and is not.

  MEASURED ON TWO FIXTURES, AND THE FIRST ONE FOUND NOTHING. Peak RSS of the
  loading backend (`VmHWM`), lz4, byte-identical input every run:

      200,000 rows, default stripe    baseline 117,178 kB   patched 118,002 kB
      600,000 rows, ONE stripe        baseline 326,584 kB   patched 304,979 kB

  The first is +0.70%, the WRONG DIRECTION, and it is recorded because it is the
  honest half: at that scale the encoded region is a few MB against a 117 MB
  process and the effect is swamped. The second saves 21.1 MiB, 6.6% of peak,
  against repetition spreads of 0.14% and 0.27%. The saving is proportional to the
  row group's encoded size, and on a default stripe of narrow data it is not
  observable at all.

  Stored bytes do not move, which is the requirement: 32,055,856 bytes and
  fingerprint `a0959193` identical across every run of both builds.

  NO NEW TEST ARM, deliberately. What changed is peak allocation, and there is no
  stable way to assert that in CI here -- a probe of
  `pg_log_backend_memory_contexts` would have to land mid-flush. The correctness
  requirement is that output does not move, which the existing content suites
  cover and which was verified by measurement. An arm grepping the source for
  `pfree(codecBuf)` would be the exact shape repaired in `8e88f42` and `f115d0b`.

- The projection guard fired on a correct caller and stayed green on a wrong one
  (#1077).

  `native_fetch_projection.sh` protected one convention -- "every column" is an
  explicit flag, never an absent set -- with two arms that counted a string across
  `columnar_reader.c` and compared it against a literal `1`. That asserts HOW MANY
  honest callers exist, which is a fact about today's tree, not the property.

  It errs in BOTH directions, and the second one is why this is a fix rather than
  a widening. Four source states, the same two arms:

      state        tests guarded flags | OLD arm1  OLD arm2 | NEW
      main             1       1     1 | PASS      PASS     | PASS
      honest caller    2       2     2 | RED       RED      | PASS
      unguarded fn     2       1     2 | RED       PASS     | RED
      unguarded inline 2       1     1 | PASS      PASS     | RED

  Row 2 is #1077's coalescing read: it takes the flag and tests it exactly as the
  convention demands, and was failed for existing. Row 4 is the defect the arms
  exist to catch, added inside the existing worker so no new flag is declared --
  and BOTH arms pass it. `arm2` counts the GUARDED form, which is still 1, so a
  bare `bms_is_member(c, needed)` beside it is invisible to the arm named "the
  column test consults that flag rather than a null set".

  Row 3 shows the one red the old arms do produce is not evidence either: `arm1`
  reddens there for the same reason it reddens on row 2, the count moved. It
  cannot distinguish a correct caller from a broken one.

  The arms now pin the property against itself -- every needed-set membership test
  consults the flag, `guarded N of N` -- so honest callers move both counts
  together and an unguarded one moves only the total. Both numbers are printed in
  the compared strings, so the arm's message is the reconciliation.

  Two premises were added beside it, because `guarded 0 of 0` also satisfies
  equality: that a membership test exists to guard, and that the flag is declared.

  This is the third arm in this file to be repaired for counting a string across a
  whole file. The `deltuples` comment 15 lines above records the first, fixed by
  scoping; these two were left as whole-file counts and did the same thing again.
- A parallel custom scan divided its whole run cost by the worker count.

  Core seqscan divides CPU across workers and leaves disk I/O whole. The
  partial columnar path divided `(total - startup)` by `workers`, so an
  I/O-dominated scan was quoted at half its serial cost with two workers.
  Measured: serial run 10825, parallel Custom Scan 5412.5 (ratio 2.000).
  Leaving I/O undivided, the same fixture is 10112.5 (ratio 1.070).

  `get_parallel_divisor` is static in core; the leader-participation heuristic
  is reproduced so CPU uses the same divisor a parallel seqscan does.

- `compare_to_bash.py`'s corpus arm called a WRAPPED name fabricated. A name too long
  for one line is written as adjacent literals, and Python joins them at parse time,
  so the joined name is text the file contains but not text `in src` can find. The
  arm exists to catch a reader that CONSTRUCTS a name, so it now collapses the file's
  own concatenation and keeps exactly that guarantee: an f-string name still yields a
  `{}` template, which the collapse does not rescue. Both directions are asserted.

- `orphan-scan` is armed in the matrix runner, so a ledger row naming a deleted
  check is refused rather than reported (#983, #1015).

  The gate answers "has this run a check the ledger has never seen". Nothing answered
  the other half, "does the ledger name a check that no longer exists" -- and two rows
  naming deleted checks sat in the committed ledger from #917 until #983 found them
  while doing something else. The census counted both. `orphan-scan` was written for
  exactly that and had NO CALLER IN THE TREE: tested, and unable to fire on anybody's
  change.

  **One log per call, not all of them.** `_by_run` returns one entry per LOG, so
  `len(runs) > 1` is true whenever more than one file is passed even when they came
  from one matrix run. A call shaped like the gate's `$_led_logs` is refused by the
  COUNT, before the before-log/after-log union the message names. So the runner loops.

  **`--orphans-only`, because a skip is not a deletion.** By default `rc=1` also covers
  a part that skipped, and that is right: `not checked` is silence by construction and
  out of scope, while `unprunable` is a gap in a part the run claimed and in it. But a
  skip is box-dependent -- part 340 skips only where there is no non-root user to read
  as -- and failing a matrix for that is a gate somebody turns off. The flag changes the
  QUESTION rather than the severity of an answer, so `rc=1` never acquires a second
  meaning, and the skipped part is still printed.

  Measured before wiring: only part 340 of the 46 has a real `check_skip` call; 320,
  400, 410 and 480 match on comments, a grep-based sweep and `printf` fixtures. Five
  majors on one box gave `orphans=0, unprunable=0` -- but with `108 PASS and 0 SKIP`,
  so the skip path never fired and that run tested nothing about skips. The skipped-part
  case was forced synthetically instead, and the tool classifies the siblings
  `unprunable` rather than orphans.

- Userinfo in an object-store ENDPOINT was accepted, and the diagnostic told the
  operator to allow-list it (#995).

  #997 closed `s3://u:p@bucket/key` -- userinfo in the URL the caller writes. The
  endpoint the OPERATOR configures was still unguarded, and the bucket guard cannot see
  it because it is not in the URL at all.

  TWO SHAPES, AND ONLY ONE WAS EVER CAUGHT. Measured on the authority parse:

      http://u:p@host:30829   -> host "u",          port 0       refused, wrong reason
      http://user@host:30829  -> host "user@host",  port 30829   port VALID

  The first has a colon inside the userinfo, so the split lands there and the port
  becomes `atoi("p@host:30829")` = 0. The second has no such colon: the real port
  survives, the `@` rides along in the host, and the invalid-port refusal never fires.
  #995 measured the first and concluded "refused as invalid host or port", which is
  true of that shape rather than of the code.

  The second shape is why this is a guard and not a message change. Its refusal came
  from the allow-list, naming the host it could not match, and the hint then said:

      ALTER SYSTEM SET pgcolumnar.objstore_allowed_endpoints = 'user@host'

  A diagnostic that invites widening a security boundary to accommodate a parse bug is
  worse than a wrong error code. Following it moves the failure from the allow-list to
  a DNS miss.

  The guard sits BEFORE the scheme and region demands rather than at the authority
  parse eighty lines later. Placed there it is unreachable whenever no region is
  configured: measured, every endpoint arm reported `requires a region option` until it
  moved. That is the same reasoning the bucket guard carries, and the trap #995 named.

  Arms in `test/objstore_userinfo.sh` beside the existing ones, with a clean-endpoint
  control in the same run, and in `test/pytest/test_objstore_endpoint_userinfo.py`
  independently.
- A test file documented as an unnumbered `###` section was invisible to every arm
  that checks `TESTS.md` (#1024).

  Not in the numbering, so `1..N with no gap` never saw it. Not in the contents, so the
  link arms never saw it. Still NAMED in the document, so the coverage arm was
  satisfied. `test_iceberg_fdw.py` shipped that way in #1057 and sat undetected.

  #1024's own report -- two PRs each taking the next section number -- is no longer
  open: `test_the_contents_list_is_numbered_in_order` landed after #1023 and catches a
  duplicate and an inversion in one rule. Planted, it reddens. This change keeps that
  guard and closes the remaining shape, which is cheaper to hit: the collision needs two
  PRs in flight, a heading at the wrong level needs one person.

- A bash suite that unrolls a family as literals graded MISSING against the port that
  parametrises it (#1045 class 3).

      bash   diff_query "c_int range" ... "c_text range"        11 literals
      port   @pytest.mark.parametrize("col", sorted(RANGES))
             expect.row_set(c, h, f"{col} range")               1 template

  The port asserts every one of the 17 properties -- `RANGES` holds the same 11
  columns bash unrolls and `EQUALITIES` the same 6 -- but a literal never meets a
  template, so `differential` was declared INCOMPLETE for a spelling.

  The grader now resolves the container and emits one CONCRETE name per member,
  matched literally on both sides. The alternative, widening `_template` so a literal
  matches a template, resolves the same 17 and gives up the ability to ever detect
  them going wrong: `c_bytea range` would match `{}` range whether or not the port
  covers `c_bytea`. Measured -- drop `c_bytea` from `RANGES` and the expansion reports
  it by name, where a widened template cannot.

  THE EXPANSION IS ADDITIVE, and that is a constraint rather than a convenience. Where
  BOTH sides are templated the template IS the match: 11 of `hilbert_locality`'s 30
  bash names and 10 of `hilbert_cluster`'s match that way, so replacing the port's
  template orphans them. Measured, replacing breaks three green pairs and takes
  `differential` to 13 rather than 0.

  IT IS ALSO NOT AN ASSERTION. One parametrised arm is ONE assertion and ELEVEN
  spellings; counting the spellings made `differential` report 274 named assertions
  where the port has 100. The expansion feeds the MISSING calculation and nothing
  else -- it is not counted and it is not listed as `extra`.

  Refused, each costing a false MISSING at worst: a module constant, because
  `FLOAT_RTOL = 1e-6` renders as `1e-06` and bash carries neither spelling; two or
  more distinct parametrised columns, because stacked parametrize is a cartesian
  product and expanding one while holding the other invents names that exist nowhere;
  and any container that is not literal. Read through `_name_argument` and nothing
  else -- expanding every f-string instead emits `SELECT id, c_int FROM %T` as a check
  name, 96 such in `differential` alone.

  `differential` reaches `missing: 0` and leaves `INCOMPLETE`. Eight of the nine
  graded pairs are now one-for-one; the ninth is blocked on #1040 phase 0b.

- The grader could not read a port's own name when a `for` loop supplied it
  (#1045 class 2).

      for label, sql in (("allnull column scan",  "SELECT * FROM %T"),
                         ("allnull column count", "SELECT count(a) FROM %T")):
          c, h = p.both(sql)
          expect.row_set(c, h, label)

  `_as_names` reads a variable as nothing, deliberately: reporting a guessed string is
  worse than reporting none. So the arm RAN AND PASSED while the grader reported its
  bash counterpart MISSING. 46 names across 15 sites, 37 of them in `differential`,
  whose port is behaviourally complete.

  This is `_parametrized_names` one level down and gets the same answer: the table is
  literal, the column is a name, read the column. Read CELL BY CELL rather than row by
  row, because `ast.literal_eval` on a whole row raises when any other column holds an
  f-string -- which is `differential`'s mismatched-collation table, five literal labels
  beside one interpolated query.

  Only a column actually used as a NAME ARGUMENT is read. A loop variable merely
  mentioned in the body is not a name, and harvesting it invents properties out of SQL
  strings; measured, that doubles the names this returns. A table that is not literal
  -- a module constant, a comprehension, a computed label -- contributes nothing
  rather than a guess.

  `differential` goes from 54 MISSING to 17, and the 17 are a spelling rather than a
  gap: bash unrolls `c_int range` through `c_text range` and `c_int eq` through
  `c_arr eq` as literals where the port parametrises them over `RANGES` and
  `EQUALITIES`, which hold the same 11 and the same 6 columns. That is #1045 class 3,
  a judgement the tool cannot make, and it stays declared rather than decided by
  widening `_template`.

- `pgc_ledger.py merge` reported the UNION of majors, which cannot show the defect
  that has occurred twice (#1048).

  The summary printed a union over rows:

      majors = sorted(set().union(*(v[0] for v in rows.values())) if rows else set())

  A union cannot represent a MINORITY set. Merge rows carrying `{18}` into a ledger whose
  rows carry `{15,16,17,18,19}` and the union does not move, so the line was
  byte-identical on a correct merge and an incorrect one -- the one statistic that could
  not see the only defect this summary has ever had, and the only one it emitted.

  It happened twice in three hours, to the same person, with a written note in between:
  #1041 wrote 12 rows at `18` against 934 uniform ones, caught only by CI's `suites
  (PG 17)` leg; #1042 wrote 8 against 1209, caught by a manual `uniq -c`. Both times the
  merge printed `majors ... 15, 16, 17, 18, 19`. The operator was not ignoring the
  output; the output agreed with them.

  It now prints the distribution, names it `NOT UNIFORM` when there is more than one set,
  and prints `rows N = sum of buckets N` beside it:

      majors: NOT UNIFORM -- 2 distinct sets over 5 rows
             3 rows  15;16;17;18;19
             2 rows  18
        rows 5 = sum of buckets printed 5

  The reconciliation counts what was PRINTED, and a SOURCE-TEXT pin holds that,
  because no behavioural arm can: wherever the display prints every bucket the two
  sources are equal by construction, so a wording-preserving swap back to
  `sum(dist.values())` passed the whole file. The guarantee rested on a comment
  until review pointed out that comments rot where arms do not. `sum(dist.values())` would equal
  `len(rows)` by construction, so it could never catch a bucket lost in the DISPLAY --
  and truncating the loop drops the MINORITY bucket, the one the summary exists to
  show, while the line still balances. Found by @OffgridwithJD in review.

  Reporting only. Whether merge should REFUSE a non-uniform result is a live design
  question and is deliberately not settled by this change.

  `test/pytest/TESTS.md` also described the ledger as FIVE tab-separated columns and
  omitted `majors` from the list, from the day that column landed (#1010) until now.
- `compare_to_bash.py` could not read a name a suite passed through its OWN wrapper,
  and published a bare `{}` in its place (#1053).

  #1051 taught the extractor the recorders `lib.sh` shares. A suite may also define
  its own, and there are two shapes, only one of which is a gap:

      COMPOSE   check "non-owner refused: ${1%%(*}"     the definition states a
                                                        TEMPLATE naming the property
      FORWARD   check_text "$label" "$got" "$want"      the definition states nothing

  A composing wrapper is already read correctly -- `non-owner refused: {}` covers all
  nine of `native_ownership`'s call sites, which is why that pair grades one-for-one.
  A forwarding wrapper's definition yields the bare template `{}`, and 17 of those
  were being published: a "property" with no content, sitting in MISSING where no port
  can ever assert it, and MATCHING a port name that is entirely one interpolation.

  The grader now derives each suite's own recorders by the rule that already works for
  `lib.sh` -- a function forwarding a bare positional into a known recorder's name
  slot, transitively, seeded from `pgc_record` -- reads the call sites of the
  forwarding ones, drops the bare `{}`, and leaves composers alone. 145 names across
  14 suites become readable. `sorted_pathkeys` alone gains 18, and they are not a
  random 18: that suite pairs every "plans no Sort" with an "and still answers
  correctly", so the grader could see every claim about the PLAN and none about the
  ANSWER.

  AND IT REFUSES what it cannot read. `hilbert_curve.sh` defines two helpers taking a
  newline-separated LIST of names in one argument, so no rule about argument positions
  can read them; the grader now exits 2 naming both rather than grading the rest. One
  suite of 264, a true positive, with no pytest twin.

  MEASURED BEFORE BUILDING, and it changed the design: refusing on "the name position
  is not a bare positional" also refuses every COMPOSING wrapper -- 32 suites,
  including `hilbert_cluster`, `hilbert_locality` and `native_ownership`, three pairs
  that are COMPLETE today -- to fix nothing. Every graded pair is unchanged by what
  shipped.

- `iceberg_fdw.sh` is ported to pytest: the FDW's partition and metrics pruning
  (#388, #432).

  74 of the suite's 76 check names, one for one under `compare_to_bash.py`, across
  identity partitions, file metrics, `bucket[8]`, `truncate[100]`, and
  `day`/`year`/`month`/`hour` on date, timestamp and timestamptz columns.

  EVERY PRUNING ARM IS PAIRED WITH A CORRECTNESS ARM, which is the design of the bash
  suite and the reason it matters here: pruning is an optimisation, so a bug in it
  returns FEWER ROWS rather than a slower plan, and a row count cannot tell the two
  apart because the right answer to most of these predicates is also small. The
  oracle is `iceberg_scan` of the same table under the same predicate -- it receives
  no predicate and opens every file, so it cannot over-prune.

  Three things the port asserts that a count alone would not:

  - a coarse transform must READ the boundary bucket. `year(ts)` puts every 2020
    timestamp in one bucket, so `ts > 2021-01-01` has to keep the 2021 file even
    though the constant is in it. An exact `[V,V]` rule prunes that file and loses
    the row.
  - `bucket[8](id)` and metrics are distinguished rather than conflated. `id = 5`
    keeps one file under bucket pruning and two under metrics alone, because
    bucket-3's range `[3,7]` contains 5. The expected count is 4 for that reason.
  - a date partition the FDW cannot convert must be read in full (#660). The wrong
    behaviour is to NULL-fill and prune, and its symptom is an empty answer.

  `_pruned` returns a sentinel rather than 0 when EXPLAIN reports no `Files Pruned`
  marker. "Pruned nothing" and "did not say" are the same number and opposite facts,
  and returning 0 for the second would make every pruning arm pass vacuously the day
  the FDW stopped reporting.

  The two names not carried are `pgc_skip`'s refusal names. That is structural:
  `pgc_skip` records under the NAME it is given and `expect.cannot_run` records under
  the REASON CODE, so a port cannot emit those strings as check names at all
  (#1040 phase 0b). Declared in `INCOMPLETE` with that reason rather than worked
  around by naming a passing premise after a missing dependency.

- `compare_to_bash.py` read 6 of `differential.sh`'s 86 check names and reported the
  port one-for-one (#1045).

  The extractor's helper list was every recorder `lib.sh` names `check*`. `diff_query`
  is a `lib.sh` wrapper that forwards its `$1` into `check`, so the NAME is in the
  suite and only the RECORDER is in `lib.sh`:

      test/differential.sh:116   diff_query "c_uuid range" "SELECT id FROM %T WHERE ..."

  and the grader read none of them. Corpus-wide, five recorders were unread:

      diff_query          name is $1   150 names   11 suites
      diff_query_ordered  name is $1     5          3
      pgc_skip            name is $2    70         47
      pgc_pass            name is $1     9          2
      pgc_fail            name is $1    15         11
                                       249 names   70 of 264 suites

  `pgc_skip` is why the table records a POSITION rather than a membership. Its name is
  `$2` and `$1` is a capability, written bare at 68 of its 70 call sites and quoted at
  the other two, so a pattern keyed to the first quoted argument reads the capability
  at those two. A wrong name is worse than an absent one: no port can assert
  `test_decoding`, so it would be reported MISSING for ever.

  THE DRIFT GUARD #1040 ADDED WAS GREEN THROUGHOUT, AND WAS NOT BROKEN. It derived its
  population by SPELLING -- `^(check(?:_[a-z_]+)?)\(\)` -- so `diff_query` was never in
  the set it ranged over. A guard is worth exactly its population. It now derives the
  population from what a function DOES, taking the closure from `pgc_record`, and
  checks each name's argument position as well as its membership. Reverting either
  half reddens it: the spelling population finds 8 recorders where the closure finds
  13, and a membership-only guard passes while `pgc_skip` is read at `$1`.

  `differential` moves to `INCOMPLETE` with `missing: 54` in the same change, because
  the extractor cannot be widened and the pair kept green at once. The port is not
  missing 54 properties. Two blindnesses were cancelling: the suite records through
  `diff_query`, which this change reads, and the port binds most of its own names to a
  `for` loop variable, which the grader still cannot read (#1045 class 2). The pair
  graded `missing: 0` on 6 of 86 bash names against 62 of 132 port names -- two blind
  halves cannot disagree. 17 of the 54 are a spelling rather than a gap: bash unrolls
  `c_int range` through `c_text range` and `c_int eq` through `c_arr eq` as literals
  where the port parametrises them over `RANGES` and `EQUALITIES`, which hold the same
  11 and the same 6 columns.

  Reading a name from the argument that HOLDS it means one pattern per position, and
  an empty position is not a no-op: `(?:)` matches everywhere, so the pattern
  degenerates to "any word, then any quoted string". On `zonemap_boundaries.sh`,
  which has no position-2 helper, it invented six names including `$PGC_DB` and
  `$(dirname `, each of which would be reported as a bash property the port is
  missing for ever, because no port can assert them. The positions are therefore
  derived from the table's own values, so an empty group cannot be constructed, and
  building one is refused rather than silently returning junk. Found because a
  reviewer's stale `.pyc` left the table mid-mutation: python validates a cached
  `.pyc` on source mtime in whole seconds and size, so a same-size edit applied and
  restored within one second keeps running while the file's md5 reports clean.

- `skip-loop-arms.py` read a one-line function body as everything below it, so a psql
  wrapper counted as a check recorder (#1042).

  The tool decides which functions record a check by reading each one's body, and it took
  that body as everything up to the next brace at column zero:

      test/audit.sh:122   q() { run_pg "$PSQL -c \"$1\""; }

  `q`'s brace is not at column zero, so its body ran on into the NEXT function's and
  swallowed every `check` call in between. `q` -- a psql wrapper that records nothing --
  then classified as a RECORDER, and its first argument, SQL text, entered a set of valid
  check names. The corpus has 199 one-line definitions, so this is the common form and not
  an edge case. The body now ends at its own closing brace, counted per line.

  IT CHANGED NO VERDICT ON THE TREE AS IT STOOD, which is why it needed an arm and not
  only a fix. A/B between the two extractors on `db74d9e9c`: `loops 8 / compared 6 /
  interpolated 1 / armless 1` from both, byte-identical. Two extractors that agree on
  today's corpus are indistinguishable from the tool's own output, so a regression would
  have been invisible to every existing check. That is a property of today's corpus and
  not of the tool.

  THE TOOL NOW SAYS WHEN A BODY NEVER CLOSES. Counting braces is defeated by an unbalanced
  one inside a quoted string, and the corpus has exactly one -- `_us_unbound` in
  `test/selftest/400-a-check-result-must-be-machine.sh`, whose `grep -oE '\$\{?...'` and
  `tr -d '${'` leave the walk unterminated, so its body is 229 lines and runs to
  end-of-file. That is NOT fixed here: one pathological definition in 909 does not buy a
  shell lexer, and a lexer is a much larger thing to be wrong about. What is fixed is the
  silence. `_us_unbound` is misclassified as a recorder today by the shipped tool AND by
  this one -- the symmetric difference of the two emitter sets is empty -- so it predates
  this change and survives it, and it is reported separately rather than folded in.

  AND HEREDOC BODIES ARE BLANKED BEFORE SCANNING FOR DEFINITIONS, which this file already
  did for its structure walk and did not do here. The guard for this very defect writes
  `q() { ... }` into a fixture as heredoc content, so without it the tool reported the
  test's own fixture as a finding in the real tree: `unclosed 2`, the second being the
  guard's own `swallower`.

  The new selftest part brackets the change in both directions, because no single mutation
  can redden all three arms:

      revert the body walk         the one-line wrapper arm reddens        1 FAIL
      delete the unclosed report   the count premise and the NAMED arm     2 FAIL
      report unconditionally       "a clean file is not named" reddens     1 FAIL
                                   and the other two stay green

  Each mutation was asserted to still PARSE before its red was believed, and each restore
  was md5-asserted.

- `test/hilbert_cluster.sh` has a pytest twin: the Hilbert clustering SQL surface, graded
  one-for-one (#432).

  45 collected tests, 184 checks, across the bash suite's eight arms: the surface and its
  refusals by SQLSTATE, "it only reorders", the recorded kind, the self-gate in every
  direction, the single-column identity, the vacuum_sorted ruling, the daemon, and the
  enumerations. `compare_to_bash.py` reports **0 MISSING** both as the tool ships today
  (124 bash checks) and under the widened extractor in #1044 (133). The difference is that
  suite's nine `check_unrunnable` sites, all of which are twinned, so this is the first
  exercise of the widening on a suite that uses those helpers correctly rather than on the
  one where they were broken.

  TWO PLACES THE PORT ASSERTS SOMETHING THE ORIGINAL GETS FOR FREE, and they are one class:
  wherever a port replaces a structural guarantee with a procedural one, it owes an arm the
  original does not need.

  The bash suite gives the daemon's naptime and thresholds to the server through
  `PGC_EXTRA_CONF`, so they sit in `postgresql.conf` before the postmaster starts and the
  suite cannot run without them. `pgc_cluster.py` has no such hook, so the port sets them
  with `ALTER SYSTEM` and a reload, which is available because all three are `PGC_SIGHUP`
  and which can silently fail to take effect. It would fail silently in the worst way: at
  the default naptime the daemon still acts, the poll still sees the tail fold, and every
  arm still passes while the values the fixture claims to have set were never in force. So
  the three are read back from the server. The same for
  `max_parallel_workers_per_gather = 0`, which the fixture set and nothing read until the
  parity tool reported the bash suite's own premise as missing.

  A RELOAD IS NOT A READ, and this one is inherited by any later port that changes
  postmaster-level state. `ALTER SYSTEM SET pgcolumnar.autovacuum = on`, then
  `pg_reload_conf()`, then `SHOW` on the same connection returned `off`: a reload signals
  the postmaster and a backend already open absorbs it at its next command boundary, and
  this module runs every statement through one connection by design. The bash suite never
  meets it because every `q` is a fresh `psql`, so a new session here is the port of what
  the original gets for free rather than a workaround.

  Every conditional `check_unrunnable` in the bash suite is its own test here.
  `expect.cannot_run()` records under the reason code, so two refusals in one test would
  collapse onto a single `UNMET_PRECONDITION` record and neither could be told from the
  other.

  THE DAEMON ARM RECORDS HOW LONG IT WAITED, not only that it succeeded. A fixture one
  poll from its window and one fourteen from it produce identical greens, so the count is
  the only thing that distinguishes them and drift toward the bound is otherwise
  invisible. Measured on all five assert builds, each run on its own:

      PG15  PG16  PG17  PG18  PG19
         1     1     1     1     1     poll(s) of 15, at a 2s naptime

  READ THAT FOR WHAT IT DOES NOT SAY. One poll on every major means the loop NEVER
  WAITED: the condition was true on the first check each time, so nothing in those runs
  exercised the waiting at all, and a loop that always succeeds on poll one is
  indistinguishable from no loop. The distribution is a SINGLE POINT, taken on an idle
  container, and the case the bound will actually meet is a shared and loaded CI runner --
  this repository has that divergence recorded elsewhere as 0 in 400 idle runs against 6
  in 400 under load. So: **one poll on each of five majors on an idle container; the bound
  of 5 is four above the only value ever observed; no loaded measurement exists.** A reader
  who meets a red at six is the first person to see the loop do its job, rather than
  someone looking at a regression.

  The count is printed by the fixture, and pytest captures fixture stdout on a PASSING
  run, so `-s` is what surfaces it; on a failing run the arm's own message carries the
  number, which is where it is needed.

  This is the one part of the change with a single source of evidence: @jdatcmd reviewed
  the rest but has no PostgreSQL on their host, said so rather than offering a reading of
  the loop, and the five-major measurement above is the only one that exists.

  What the port does NOT buy is stated in the file: over one column the Hilbert index and
  the Morton index are both the identity, so the single-column arm is green on a relabelled
  Z-order implementation by construction. Four arms refuse one, and no others.

- An unrunnable record names the check it stands in for, so a refused check keeps
  one ledger key instead of two (#1040).

  `check_unrunnable NAME REASON DETAIL` gives one check the honesty `pgc_skip` gives a
  whole suite: the check did not run, and the reader is told which. That only works if
  the record carries the name the check uses when it DOES run. Two of the four sites in
  one loop in `hilbert_locality.sh` carried a shortened name:

      :574  check_unrunnable "box $box: groups read, Z-order"
      :597  check_num        "box $box: groups read over $PLACEMENTS placements, Z-order"

  while the other two matched their runnable twins exactly. So the property had two
  ledger keys and which one appeared depended on whether that box's two partitions came
  out different that day -- the key was a function of the data. `hilbert_locality` is not
  a ledger-covered suite, so this was a wrong key waiting to be seeded rather than a
  wrong number in `check_ledger.tsv`.

  The convention is already near-universal, and that is what made the lapse invisible:
  23 of the 25 `check_unrunnable` call sites in `test/*.sh` carry the runnable name
  (`hilbert_cluster` 9 of 9, `projection_rewrite` 11 of 11), so a shorter name in a new
  refusal branch reads as ordinary. Both halves of a two-branch site are rarely read
  together.

  A new selftest part asserts the property over the whole corpus, driving
  `.github/scripts/unrunnable-arm-names.py`. The tool DERIVES three things a list of it
  got wrong first: which functions record, from the `pgc_record` call in their body;
  which argument is the name, because `pgc_skip` records `"$2"` and reading argument one
  takes the capability where the check is called `arrow support is present`; and what
  counts as a refusal, from the verdict rather than the helper's spelling. `check_skip`
  is deliberately not swept -- a skipped arm has no runnable counterpart by construction,
  so 21 of its 23 call sites have no twin and always will.

  The part carries a positive control because the guard's steady state is zero and a
  broken sweep reports zero too: it drives the real tool over a fixture whose refusal
  branch names something the file never records, and over a control where the names
  agree.

  This also closes the only pair `compare_to_bash.py` fails once its extractor is
  widened to all eight `lib.sh` helpers (#1040): with the two names fixed, all seven
  ported suites grade one-for-one under the widened extractor, with no change to the
  port and none to `pgc_vacuity.py`.

  The sweep's exit code is the verdict. It was a flat zero in the first version, so the
  tool printed two `MISMATCH` lines and reported success. The part gates on the parsed
  output and was never fooled, which is exactly why the exit code needed its own arms
  rather than an observation: the next caller is the one that trusts `$?`, and a gate that
  cannot fail is a trap whether or not today's only caller steps in it. Three arms, because
  pinning the non-zero side alone passes on a tool that always exits 1, and the clean side
  alone passes on a corpus with nothing to find -- so the clean run uses a second fixture
  directory that HAS a refusal site, with a premise asserting it. Reported by @jdatcmd.

- The census re-derivation printed in `check_ledger_budget.txt` reads the wrong field
  and returns zero (#1040).

      awk -F'\t' '$4=="never"' test/check_ledger.tsv | wc -l     -> 0
      awk -F'\t' '$5=="never"' test/check_ledger.tsv | wc -l     -> 1189 on db74d9e

  #1010 inserted the majors a row claims as field 4, moving the last-red to field 5, and
  the recipe stayed on field 4. The entry for #1010 in this file states that
  `check_ledger_budget.txt` carries the corrected form; it did not. The gate is
  unaffected -- it computes the census itself and never runs this command -- so the harm
  is a reviewer re-deriving the number by the printed method, getting 0, and correcting a
  budget that was right. Fixed here rather than filed because this change moves that very
  number, and a wrong recipe beside a number nobody can check is worse than no recipe.

- `allow_empty` documented a rule the code did not enforce (#1031).

  `Expect.rows` documents the argument as taking *"a REASON, not a flag"*. The sentence even
  gives the rationale: the hatch should cost more to type than the honest assertion. One line
  below it sits a truthiness test:

      if _empty(got) and _empty(want) and not allow_empty:

  So `allow_empty=True` satisfied it and carried nothing, and the hatch cost LESS than the
  assertion rather than more. Measured before the refusal: `allow_empty=True` and
  `allow_empty=1` both passed, 3 passed. `row_set` forwards the argument, so it inherited the
  hole and now has its own arm asserting it does not route around the refusal.

  THE CHECK FIRES WHENEVER THE ARGUMENT IS GIVEN, not only when both sides turn out to be
  empty. Otherwise a flag form in a test whose sides happen to be non-empty passes today and
  refuses on the day the data changes. That is the worst moment to learn it.

  TWO LIVE SITES USED THE FLAG FORM AND BOTH WERE SUBSTANTIVELY CORRECT. Each had its
  population premise on the line above. `test_check_records.py` even stated the argument in a
  comment: *"an empty offender list is the answer to both, and only one of them is good news."*

  So this is not a defect hiding behind the hatch. The cost fell on the next reader. The hatch
  exists so every empty-on-both-sides comparison carries its justification where an audit of
  `allow_empty=` can read it, and half of them carried none. Both now do.

  Pinned in `test_guards_pinned.py`, which exists so a refusal is asserted by its message rather
  than by "something failed". That file was built after a census found 12 of 17 guards deletable
  with the suite still green.

- The gate refuses two checks that share one ledger key, instead of printing a note
  about them (#982).

  AND THE SAME CLASS ONE LEVEL DOWN, found while building the arm for the first. `read_ledger`
  did `rows[(f[0], f[1], f[2])] = [...]`, so a duplicated key in the TRACKED file collapsed
  silently and the last line won. Measured on a two-line fixture, both orders:

      never first, then 2026-09-01   survivor last_red='2026-09-01'
      2026-09-01 first, then never   survivor last_red='never'      the red is GONE

  Line order decided whether a recorded red observation survived. A merge that keeps both
  sides of a changed row turns `ever red` back into `never`. That is what #918 and #925 exist
  to prevent, arriving from the opposite direction.

  NOTHING ELSE COULD CATCH IT, and bounding the census cannot. `check_ledger_budget.txt` says
  `checks_never_observed_red` is a CENSUS and must not become a ceiling, because every new
  check enters as `never` and bounding it deadlocks. The gate compares the budget's number
  with the ledger's, and both come from the same dict, so they agree either way. Measured:
  with the budget regenerated alongside, an erased red passes the gate at rc=0.

  It is refused as an INTEGRITY FAILURE (rc=2) rather than a gate verdict, beside the other
  inputs that do not parse. A ledger that cannot be trusted is not a gate result.

  So the two halves are the same shape at two levels. A SET hid two checks in one run; a DICT
  hid two rows in one file. The question that found both is what the input canonicalises
  before the guard sees it.

  AND A THIRD, IN THE ARM THAT POLICES THE OTHER TRACKED KEY-VALUE FILE.
  `test_harness_deps.py` read `expected_tests.txt` with `nums[f[0]] = int(f[1])`, so a
  duplicated key collapsed and the last line won, exactly as `read_ledger` did. Measured on
  one fixture read both ways:

      the line form   names guard_tests as duplicated
      the dict form   sees two keys and keeps 280, the LAST line

  THIS IS NOT HYPOTHETICAL. Three PRs were open at once, each moving `guard_tests`, and
  resolving all three keep-both produced three of those lines. `ci.yml` reads the value with
  `awk '$1=="guard_tests"{print $2}'`, which prints one line per match. So `WANT` becomes
  multi-line, `test -n "$WANT"` still passes, and the flag refuses it at exit 4:

      pytest: error: argument --pgc-expect-tests: invalid int value: '284\n280\n283'

  It FAILS CLOSED, so this is legibility rather than a hole. What exit 4 does not say is that a
  line is duplicated. Two arms now say it. The removal proof on the real file reddens the new
  arm and leaves the pre-existing one green, which is the point.

  Keep-both is right for a changelog and wrong for a key-value file. Nothing in the tree said
  so.

  `cluster_tests` 205 -> 207, not `guard_tests`: `test_harness_deps.py` DEFINES `NO_CLUSTER`
  and is not in it. I got that wrong first and the mechanism caught it, which is the argument
  for the mechanism.

  A ledger row is keyed on `(suite, part, name)`, so two checks with the same name in one
  part share a row. Nothing is mis-recorded while both pass. The hazard is exact: when one
  goes red the row records `ever red`, and its namesake inherits a red observation nothing
  attacked. `checks_never_observed_red` then falls by one for a check nobody attacked, and
  that census is what #918 and #925 exist to make trustworthy.

  `merge` has detected this since #982 was filed, and returns 0. That is how three of them
  sat in one part of `selftest/400` for a day. The instance was fixed by `c3b13aed`; this is
  the mechanism, which that issue called the more valuable half.

  THE GATE COULD NOT SEE IT AT ALL, and the reason is worth recording. `cmd_gate` builds its
  records as `sorted({(s, p, n, m) for ...})`. A set collapses the duplicate before any arm
  can count it. So the same canonicalisation that makes the rest of the gate correct made
  this one class unreachable. The count now comes from the raw records through `_by_run`.

  PER LOG, because one check observed in two logs is two RUNS of it. That is the normal case
  and the way the ledger accumulates evidence at all. Only a repeat inside one log is a
  collision. An arm pins the distinction, because written over the logs together the refusal
  would reject every multi-day merge.

  EVERY SUITE, DELIBERATELY UNLIKE THE NEW-CHECK REFUSAL. That one is restricted to suites
  the ledger covers because it cannot know which of an uncovered suite's checks are new.
  This one needs no history: two records, one key, one log is decidable from the log alone.
  The ledger covers four suites of 253. Copying the restriction would close the class in four
  places only. The next collision would then sit in one of the other 249 until that suite was
  seeded.

  MEASURED BEFORE WIDENING IT, because a gate that reddens 250 unmeasured suites is a gate
  somebody turns off. A full PG 18 matrix run with the refusal armed for every suite:

      suites that ran                       247   (6 skipped, 0 incomplete)
      matrix verdict                        ALL VERSIONS PASSED, RC=0
      shared ledger keys found                0

  Check names are static, so one major's matrix measures this class completely rather than
  sampling it. A real `harness_selftest` log says the same end to end. Run against the
  COMMITTED ledger and budget, the gate returns 0 with no shared-key line, over 934 records
  and 934 distinct keys.

- `projection_privilege.sh` has a pytest twin, and both halves now attribute a refusal
  by SQLSTATE instead of by error text (#432, #562, #563).

  The bash suite decided four things by matching the message, and two of them were
  load-bearing rather than decorative. Its own comment says why: both ACL layers raise
  42501, so a bare "refused" stays true if the SQL `REVOKE` is deleted and the C check
  catches it instead. Measured there: with the `REVOKE` removed the suite still passed
  14 of 14.

  The fixture is what separates them, not the wording. Called with a projection name
  that does not exist, on a table the role may read, a caller stopped by the SQL grant
  never runs the body and gets 42501; one that gets past the grant reaches the lookup
  and gets 42704. The refusal is attributed by what the code REACHED. RLS is a third
  code again, `0A000` from `ERRCODE_FEATURE_NOT_SUPPORTED`, which is a different
  SQLSTATE class from either ACL refusal.

  Two orderings that `src/columnar_projection.c` and `src/columnar_vacuum.c` assert had
  no test in either harness: the base ACL is checked before the projection is looked
  up, so a caller with no SELECT cannot learn whether a projection exists on a table it
  may not read; and the ACL is checked before RLS, so a caller with no privilege is not
  told the table has row-level security enabled. The second is a correction the source
  records being made in review.

  Five mutations, each asserted to apply at both call sites. Moving the ACL check below
  the projection lookup but above its raise reddens nothing, correctly -- 42501 still
  wins. Moving it below the raise reddens both ordering arms, and the shell half prints
  the disclosure: `got [42704] want [42501]`.

- `compare_to_bash.py` reads the assertion's NAME (#432, #897).

  The parity tool decides whether a port is one-for-one with its bash suite, which is
  #432's definition of done, and it was reading the wrong argument. The python side was
  matched with `expect\.\w+\([^)]*?"([^"]+)"`, whose lazy `[^)]*?` stops at the FIRST
  quoted argument. For `expect.num(got, 1, NAME)` that is the name, so the tool looked
  correct. For `expect.sqlstate(err, "42501", NAME)` it is `42501`.

  Every SQLSTATE assertion was therefore read as the literal `42501`, reported as an
  "extra" name the bash suite does not have, while the real property was reported
  MISSING. #432's ports are exactly the ones replacing a grep on a message with a
  SQLSTATE assertion, so the tool went blind in proportion to the work being done well.

  It parses with `ast` now and takes the last string argument, resolving f-strings to
  templates, both arms of a conditional, and the `name` column of a
  `@pytest.mark.parametrize`. Bash interpolations reduce to the same template, including
  `$1`, which is the commonest one in a check name and which the first version of the
  reducer missed because its pattern required a letter after the dollar.

  Measured over every pair in the tree: **61 bash properties reported missing, now 0.**
  34 were never missing. The rest were real and are closed here: `stats_privilege` had
  invented a name for a property the bash suite already named, and `zonemap_boundaries`
  was missing its `backend alive` premise outright. Neither was visible while the tool
  was reporting the wrong string.

- A UNIQUE-constraint check passed on any psql failure, and a recursive sweep passed on a
  tree it never read (#1033).

  `native_recluster.sh` decided "unique still enforced" from psql's exit code:

      psql ... -c "INSERT INTO n VALUES (1, 0, 0, 'x');" >/dev/null 2>&1 && echo no || echo yes

  `|| echo yes` cannot tell a unique violation from any other failure, and
  `>/dev/null 2>&1` discards the message so nothing else can either. Measured on the
  identical expression: a missing table, a wrong port, psql absent from PATH and a syntax
  error all produced `yes`.

  THE CONTRAST, RUN END TO END on PG 18 rather than argued. The same mutation -- point the
  probe at a table that does not exist -- against both versions of the arm:

      main's arm    PASS  "unique still enforced"       suite PASSED, rc=0, 12 passed
      this arm      FAIL  got [42P01] want [23505]      suite FAILED, rc=1

  And the arm is observable in the direction that matters. Replace the unique index with a
  plain one and the duplicate INSERT succeeds. The arm goes red there too, so it tests the
  constraint rather than merely the SQLSTATE plumbing.

  The arm now reads the SQLSTATE. 23505 is `ERRCODE_UNIQUE_VIOLATION` and comes from the
  index; 42P01, 42601 and a connection failure do not. `arrow_import.sh` and `audit.sh`
  already read SQLSTATE this way, so this is the convention rather than a new one.

  SECOND, A SWEEP WITH NO POPULATION. `local_open_race_free.sh` asserted that a removed
  helper leaves no trace:

      "$(grep -rc 'PgColumnarRejectNonRegularFile' "$SRC" | awk -F: '{s+=$2} END{print s+0}')" "0"

  `grep -rc` prints one `file:count` line per file and prints NOTHING for a path it cannot
  open, and `END{print s+0}` then manufactures the `0` the check wants. The three arms above
  it read `$OBJ`, not `$SRC`, so nothing established that `$SRC` was a source tree.
  Measured:

      SRC=src            files=56  premise=yes  arm=0   both agree
      SRC=/no/such/tree  files=0   premise=no   arm=0   the premise catches it, the arm alone passes

  A premise now counts the lines the recursive grep emitted, which is exactly what the
  arm's `awk` sums over.

  NEITHER SUITE NEEDS A LEDGER ROW, which is why these two and not a third.
  `check_ledger.tsv` holds zero rows for `native_recluster` and `local_open_race_free`, so
  the gate cannot refuse a new check there.

  A third site has the same shape and is left alone: `selftest/400`'s tree-wide piped-loop
  sweep. It lives in `harness_selftest`, which the ledger covers with 934 rows. Its detector
  IS premised, since `:549` proves it fires on a planted offence, and only its glob
  population is not. So it belongs to a change that carries the five-major ledger merge.

  Verified on PG 18 in the container: `local_open_race_free.sh` PASSED, 11 checks;
  `native_recluster.sh` PASSED, 12 checks; both mutations red; main's arm green under the
  same mutation.
- The vacuity inventory named one entry twice, and no arm could fail on it (#432).

  `VACUITY_MODES.md` section 3.4 carried the same six-id bullet twice, verbatim, on two
  lines each. It is deleted, and three arms in `test_docs_cover_the_corpus.py` now refuse
  a duplicated entry.

  WHY NOTHING CAUGHT IT, measured rather than guessed. Every count this document states
  is checked. Every one of those checks is blind to this by construction:
  `_named_modes_in` builds `set(MODE_ID.findall(chunk))` per section, so each total is
  over distinct ids. Measured with the second copy present and then deleted:

      with the duplicate      (28, 44, 72)
      without the duplicate   (28, 44, 72)

  So `test_the_mode_inventory_states_its_own_totals_correctly`, the prose-totals arm and
  the sum arm were all green with a duplicated entry in the file. Deduping ids is right.
  A total must not move because a line was pasted twice. The cost falls on the reader
  instead: one group of open modes reads as two. So the new arm is about entries, and the
  counting rule is unchanged.

  MY OWN FIRST SWEEP MISSED IT, which decided the unit the rule uses. An adjacent
  duplicate-LINE sweep over every document in the directory reported nothing. The
  duplicate is a two-line bullet, so line 1 of the first copy and line 1 of the second
  are not adjacent. Re-keyed on the bullet ENTRY, the same sweep found it, and found
  exactly one tree-wide. Four sweeps here have now failed by keying on how something is
  written rather than on what it contains. So the unit is named in the code, and both
  fixture arms use a two-line bullet rather than a one-line one.

  The rule carries its false-positive budget as an arm. The inventory legitimately repeats
  short bullets, so entries under a 40-character floor are not compared. The budget is
  measured at the boundary: the same bullet passes below the floor and is refused above
  it.

- The section that checks for stale documents carried a stale count (#432).

  `TESTS.md` section 6 said "the five fixture arms". Five was correct at `3d6e1216`
  (2026-09-09) and counted the arms taking `tmp_path`; there are eight of those now and
  thirteen fixture arms in total. Nothing read the number, so it went stale in the
  document whose whole subject is documents going stale.

  It is removed rather than corrected. The paragraph above it already decided that for the
  same reason: `test_the_document_states_no_totals_for_a_merge_to_get_wrong` exists to keep
  a totals line OUT. A count in prose that no arm reads is a claim waiting to go wrong. The
  arms themselves are listed in the table above, where a reader can count them.
- `TESTS.md`'s contents list was out of numeric order and no arm could see it (#1026).

  After #1023 merged, the document read:

      TOC       ... 29, 31, 30      and 29, 31, 30, 32 once the next section arrived
      sections  ... 29, 30, 31      contiguous and correct

  #1023's contents entry for section 30 landed after 31. Resolving this PR's conflict in
  that same region meant choosing an order, so the fix lands here: TOC and sections are
  both 1..32 with no gap and no inversion.

  WHY NOTHING CAUGHT IT. `test_docs_cover_the_corpus.py` already sweeps every
  contents-list link and asserts it reaches a heading. Both orders resolve, so that arm is
  green either way. Measured by restoring the broken order under the new arm:

      the new arm      FAIL  got '[(29, 31), (31, 30), (30, 32)]' want 'none'
      the link arms    1 passed

  So the link sweep is not a weaker version of this rule; it answers a different question,
  and a shuffled contents list was outside both.

  Two arms. The first reads TESTS.md. It requires the contents numbers and the section
  numbers each to count 1..N with no gap, and one contents entry per section.

  The second is the removal proof on a fixture. It uses the `29, 31, 30` shape that actually
  shipped rather than a single swap. It also names an omitted entry apart from an inversion:
  a missing entry gives `[(1, 3)]` and a shuffle gives `[(1, 3), (3, 2)]`.

  THE GAP RULE CATCHES THE COLLISION TOO. Three open PRs each claimed a section number
  another had taken, which is the cause rather than a coincidence. A number used twice
  leaves a gap in the section sequence, so `1..N with no gap` reddens on a duplicate and on
  an omission with one rule.

  `guard_tests` 282 -> 284, re-derived by collection.

- `native_ownership.sh` has a pytest twin, and it asserts the SQLSTATE (#432).

  Nine maintenance and DDL functions, each refused to a non-owner with 42501 rather
  than with a grep for `must be owner`. `CLAUDE.md` already states the rule: 42501
  comes only from `aclcheck_error`, and the refusal is
  `aclcheck_error(ACLCHECK_NOT_OWNER, ...)` at `columnar_vacuum.c:189` and `:205`. A
  text grep passes whatever code the server attached.

  It also stops conflating refusal with login: the bash suite runs each call as a role
  that must be able to connect, so a role that could not log in fails the arm for a
  reason unrelated to ownership. `SET ROLE` changes the effective user without
  authenticating.

  A third arm states the ordering the bash comment asserts in prose: the check fires
  before the work, so a non-owner is refused for a projection that does not exist.

  THE PREMISE ARM CORRECTED ITS OWN DOCSTRING. Every refusal carries `premise: alice
  reaches the table`, because each test runs in a private schema. Measured by removing
  the grant, alice gets `42P01 relation does not exist` rather than the 42501 I had
  claimed: an unqualified name resolves through `search_path` and an unusable schema
  is skipped, so the arms fail rather than falsely pass. The false-pass case needs a
  QUALIFIED reference, which raises 42501 for the schema.

  AND THE PARITY TOOL CANNOT GRADE THIS PAIR. Both sides build names at runtime, so
  `compare_to_bash.py` reports `PORT IS INCOMPLETE` for a complete port. Measured: 81
  of 253 suites carry at least one interpolated check name. That bounds how much of
  #432's parity the tool can certify, and it is a false red rather than a false green.
- `stats_privilege.sh` has a pytest twin, and it asserts the SQLSTATE (#432).

  The bash suite decides the refusal with a grep on the message. `CLAUDE.md` names
  the rule: 42501 comes only from `aclcheck_error`, while a grep for "permission
  denied" is also satisfied by other refusals.

  WHICH ARMS ARE LOOSE, MEASURED RATHER THAN ASSERTED, because two rounds of review
  narrowed this twice. The defect arm at `:82` uses `permission denied for table`,
  which the schema message does NOT match -- so that arm is not confusable, and an
  earlier version of this entry claiming otherwise was wrong. The two BARE greps are
  at `:68` and `:70`, and both are premises:

      :68  premise: the no-privilege role cannot read it by ordinary SQL
      :70  premise: the catalog tables are NOT readable by these roles

  So the risk is a premise satisfied for the wrong reason, which weakens what the
  suite rests on, rather than a defect slipping through. The port asserts 42501 on
  the premises and on the refusal, and that the refusal names the table.

  Real logins rather than `SET ROLE`, because session-opening is a property this
  suite tests and `SET ROLE` would assert it away.

  SCOPE, MEASURED AND THEN CORRECTED. Across the corpus, 50 suites already assert a
  refusal by SQLSTATE and 3 assert both. FIVE assert by text with no SQLSTATE
  anywhere: `native_ownership`, `stats_privilege`, `projection_privilege`,
  `rls_direct_storage` and `import_export_privilege`. Two are now ported; the class
  closes at five, not at the whole corpus.

  My first sweep said four. It missed `import_export_privilege.sh:76` because the
  flag class `grep -[qic]*i?` does not cover the `E` in `grep -qiE`, so the line
  never matched and a fifth member stayed invisible while the output looked complete.
  Found by @OffgridwithJD. Four sweeps in one day across two sessions have now failed
  this way, each keyed on how something was NAMED or SPELLED rather than on content:
  key on content, and when a sweep returns a tidy number, grep for one known-present
  member and check the sweep found it.

  A HELPER TURNED A DRIVER DETAIL INTO A PRODUCT CLAIM. psycopg3 returns the FIRST
  statement's result for a multi-statement execute, so `SET search_path ...; SELECT`
  hands back the SET's empty result. The first version collapsed that into a 0 and
  the arm reported "the OWNER cannot read its stats". Every call site now asserts the
  error is None rather than folding it into a value.
- The docs gate checks that a markdown table is still a table (#1026).

  `docs_style.sh` enforced seven rules over every user-facing page. Sentence length, the
  idiom list, em and en dashes, prose double-hyphens, conflict markers, the nav entry, and
  every `VERSION` citation. ALL SEVEN ARE ABOUT PROSE. So a table that had stopped being a
  table passed the gate whose purpose is keeping those pages readable.

  The measured case. A note and a second table were spliced into the middle of
  `configuration.md`'s `set_options` argument table. That left six of the nine arguments as a
  headerless block, and `docs_style.sh` passed with 14 checks.

  It was the second splice in one day. The first gave the GUC table no blank lines, which
  made an `awk RS=''` guard read two GUC rows as one record and pass on `main`. Both are the
  same fact: a markdown table is a contiguous run of `|` lines, and a blank line is
  structural.

  Fences are tracked BY LINE rather than stripped with a regex. The regex form already in
  that file is fine for counting, but it loses line numbers and it breaks on an unclosed
  fence. State-tracking under-reports there instead, which is the safe direction.

  MEASURED BEFORE LANDING, which is what a static guard here owes. 0 across the gate's own
  scope, and 5 elsewhere in the tree. All five are REAL rather than false positives: 3 in
  `test/pytest/TESTS.md` and 2 in a design document, none of which the gate covers.

- The mutation ledger records WHICH MAJORS each check exists on (#1010).

      suite<TAB>part<TAB>name<TAB>majors<TAB>last-red<TAB>mutations

      rows            1179 -> 1197   (carried 1177, new 20, dropped 2)
      checks_never_observed_red   1171 -> 1189
      ever red           8 ->    8   (all carried, with their dates and one mutation)
      major sets      1197 x "15;16;17;18;19"
      covered         differential, harness_selftest, native_join_runtime_filter,
                      native_join_vector_agg
      suites_not_covered          249 (unchanged)

  Every row claims all five majors, because all four covered suites are major-invariant:
  934, 204, 46 and 13 records, identical on 15/16/17/18/19 in a five-major matrix. So the
  census moves only by the twenty arms this change adds, and the diff is one field per
  line.

  The 20 new rows are part 410's arms about the majors field; the 2 dropped are the rows
  whose checks this change RENAMED (`every committed row has five fields` became `six`).
  The migration refuses to drop a row carrying history, for the reason `orphan-scan`
  refuses it -- the catalogue of what has been seen red is the thing no run can recreate --
  so a drop is only available for a row with nothing to lose, and the name has to be typed.

  A check's existence depends on the major, so a ledger that cannot say where a check
  exists cannot tell a deleted check from one that never ran here.
  `test/analyze_differential.sh:61` emits ONE record on PG15-17 and a suite's worth on
  PG18+; `test/fk_referencing.sh:287` emits DIFFERENT CHECK NAMES in its two branches.

  THE MAJOR IS A FIELD, NOT PART OF THE KEY, and that is the design rather than an
  implementation detail. Measured on a full matrix at `4d7c75ae`, 252 suites on PG15 and
  PG18: 6367 of 6472 checks are identical on both majors and 105 exist on exactly one. A
  `(major, check)` key would hold 6472 x 5 = 32,360 rows to express those 105 -- about 247
  duplicate rows for every row that differs, each a second copy of one observation.
  Keeping the key at `(suite, part, name)` also keeps `checks_never_observed_red` counting
  CHECKS: under a pair key it would count pairs, and "5800 checks" in a tree holding 1150
  of them is a number that lies by its own name.

  The field is a sorted `;`-separated SET -- `15;18` -- and it ACCUMULATES. A plain
  assignment was measured doing the wrong thing to last-red on #918: merging a PG15 log
  after a PG18 log must not make the check stop existing on 18, because the order somebody
  merges logs in is not a fact about the code.

  NO WILDCARD. "Every major observed" would change meaning the day a major joins the
  matrix, inheriting a claim nothing measured.

  `unknown` is a token in the set like any other, and the common one:
  `harness_selftest` never references `PGC_MAJOR`, so every record it emits carries it.

  WHAT THIS FIXES is `orphan-scan`, not `gate`. The gate refuses a check in the LOG the
  ledger has not seen, and a PG18-only check does not appear in a PG15 log, so it stayed
  correct by never being asked. `orphan-scan` asks the opposite question, and a PG18-only
  row is exactly what a deleted check looks like on PG15. It was saved only by the SKIP
  rule -- `analyze_differential` emits a `check_skip` so its part was unprunable, while
  `fk_referencing` emits `check` and has no SKIP at all, so once that suite is seeded a
  PG15 run would have called its two PG17+ checks deleted. The fourth category already
  said the true thing, so the scope gains an intersection and nothing else: no new
  category and no grandfather rule.

  The gate also cannot refuse a check on a major it holds no rows for, for the same reason
  it cannot in a suite it has never seen -- otherwise adding PG20 would redden every check
  at once, which is a gate somebody turns off. It tightens the moment one run on that
  major is merged, and it says out loud when it is not enforcing.
- The docs name 1024 as the floor for `stripe_row_limit` (#1017).

  A vector is a fixed 1024 values, so a row group smaller than one never fills it and
  the chunk-shared FSST symbol table is not built. Measured on 200,000 rows of a text
  column, `compression = none`, against 12,800,000 raw bytes, two identical passes:

      stripe_row_limit 1000   0 FSST tables    13,625,000   106.4% of raw
      stripe_row_limit 1200   166 of 167        6,998,031    54.7% of raw
      stripe_row_limit 2000   100 of 100        6,990,641    54.6% of raw

  At 1000 the column costs more than storing the bytes uncompressed. THE ACCEPTED
  MINIMUM IS 1000, enforced in `set_options`, so the most aggressive legal setting is
  the one that pays this -- and `docs/administration.md` tells a reader to LOWER this
  setting for point-lookup-heavy tables, which is the path in. The warning now sits in
  that block rather than in a reference table.

  Documentation only. The minimum is unchanged: whether to raise it, make FSST work
  below a vector, or warn at `set_options` is still open on #1017.

  The chunk-group limit does not affect this, and that is now measured rather than
  assumed: `chunk_group_row_limit` at its floor of 100 stores 7,086,080 bytes, the same
  byte count as 1024 and 10000. Measured by @OffgridwithJD.

  THE GUARD WAS BORN GREEN TWICE BEFORE IT WORKED, and the measurement is why it does
  now. A blank-line block reader passed on main, because `configuration.md`'s GUC table
  has no blank lines and `stripe_row_limit`'s row shares a block with
  `chunk_group_row_limit`'s "fixed 1024-value vectors". A three-line proximity window
  passed for the same reason. One line naming both is 0 on all three pages on main, and
  it is also a claim about the prose: the floor has to be stated in a sentence.

  The two harnesses disagreed while that was being found -- the awk arm used paragraph
  mode and passed on main for two pages, the python twin split on blank lines and did
  not -- which is the argument for keeping both halves, paid back the day it was written.

  AND ONE LINE SAYS NOTHING ABOUT WHERE. Moving the line out of the advice block to the
  end of administration.md, 402 lines away, left the arm passing while its name claimed
  the floor was stated beside the advice. Reported by @OffgridwithJD. The arm now
  asserts the SECTION: the floor and the lowering advice must sit under one `## `
  heading. A heading is a declared boundary, which is what the paragraph reader lacked.

- A check record names the PostgreSQL major it was observed under (#1010).

      RESULT<TAB>suite<TAB>part<TAB>name<TAB>verdict<TAB>major<TAB>reason

  A CHECK'S EXISTENCE DEPENDS ON THE MAJOR, so a record that does not name one
  identifies a check only partly. `test/analyze_differential.sh:61` emits ONE record on
  PG15-17 and N on PG18+; `test/fk_referencing.sh:287` emits DIFFERENT CHECK NAMES in
  its two branches, so the two majors' key sets are disjoint. Measured on a full matrix
  at `4d7c75ae`: `analyze_differential`, `analyze_function`, `native_repack` and
  `pg19_vacuum_options` each emit exactly 1 record on PG15 against a suite's worth on
  PG18.

  A grep for `PGC_MAJOR` does not find all of them, which is why this is a field rather
  than a convention: `test/native_repack.sh:48`, `test/pg19_vacuum_options.sh:34` and
  `test/native_dml.sh:61` gate on `server_version_num` and never mention it.

  Before this the tool could only learn the major from whoever invoked it, which is the
  `--date not-a-date` failure one field over: a PG15 log merged as PG18 is misattributed
  and nothing in the log can contradict it. The major is VALIDATED rather than stored --
  `eighteen`, `18.2`, `pg18` and an empty field are each refused -- because a major
  decides which checks can exist.

  `unknown` is the one non-numeric value, and it is lib.sh's own word for a field the
  harness did not set (it already uses it for an unset suite and part). It is a real case
  rather than a courtesy: `PGC_MAJOR` is set in `pgc_setup`, and 14 suites need no cluster
  so never call it. Measured on a full pg18 matrix, 544 of 6753 records carry it --
  `audit`, `concurrency`, `decode_interrupts`, `hilbert_curve`, `objstore_stash_recovery`,
  `phase2`-`phase6`, `smoke`, `unique_conc`, `update_conc`, `wal_envelope`.

  It is ORDER-DEPENDENT in a suite that sources parts into one shell: a record emitted
  before the first `pgc_setup` says `unknown` and one after it names the major.
  `harness_selftest` is that shape, 10 of its 46 parts call `pgc_setup`, and on pg18 all
  916 of its records named the major -- so the first setup precedes the first record
  today, and a part added ahead of it would change that.

  The ledger itself is unchanged in SHAPE: it still keys on `(suite, part, name)` and
  discards the major. Keying on it is #1010's second step, and it needs a migration this
  change does not.

      checks_never_observed_red   1155 -> 1164
      suites_not_covered          249 (unchanged; no suite was seeded)

  The nine rows are part 400's new arms: four emitter arms driving `pgc_record` with
  `PGC_MAJOR` set to two different values and unset, one that the reason still follows
  the major, and five reconciler arms -- four invalid majors and the `unknown` control
  that keeps them from passing because the reconciler started refusing everything.
  RE-DERIVED from the committed file by the derivation the budget file states, not
  computed from 1155:

      awk -F'\t' '$4=="never"' test/check_ledger.tsv | wc -l
- Ungrouped vectorized aggregate over a unique-key inner Hash Join (#752).

  The fold used to require a single base relation, so a star-schema join dropped it.
  A unique dimension is a filter of the fact table, so the fold can keep running.
  Duplicate-key dimensions, LEFT joins, extra Join Filters, and grouped aggregation over a join still use core Agg.
  A Join Filter besides the hash clause is not a membership test, so the fold refuses it.
  `pgcolumnar.enable_ungrouped_vector_agg` stays off by default.

      checks_never_observed_red   1155 -> 1162
      covered                     native_join_vector_agg, 13 checks, last-red 2026-09-12
      awk -F'\t' '$5=="never"' test/check_ledger.tsv | wc -l

  Field 5 because the entry above inserted the majors as field 4. At the moment this
  change landed on its own it was field 4; both ship in the same release, so the form
  here is the one that works on the shipped tree.
- A pytest cluster that will not start now says why (#1016).

  `pg_ctl` prints "Examine the log output." and nothing examined it, so a cluster that
  failed to start produced fifty identical errors naming the COMMAND and not one naming
  the cause -- measured on a GitHub runner, fifty `pg_ctl: could not start server` and the
  reason sitting in a file nobody read. `lib.sh` has had `pgc_start_log_report` since #537
  for exactly this; the pytest harness had no equivalent, and the two are meant to be
  parallel in functionality. It reports the FATAL lines with their line numbers, then a
  tail, and says so explicitly when it found neither -- silence reads as "nothing to say",
  which was #537's whole complaint. Written on this side rather than called across the
  boundary, because the harnesses stay independent.

- The pytest tests that need a cluster now run in CI, and both pytest jobs assert how many
  tests they collected (#1016).

  **166 collected tests in 9 files**, counted on this tree: 93 when the gap was filed, plus
  #1012's `test_join_vector_agg.py` and #1020's `test_differential.py`, which landed into the
  ungated half while this change was in review. That is the argument for the change rather
  than a detail about it -- the half was growing faster than it was being gated.

  `ci.yml` had one pytest job, `pytest-guards`, and it installs psycopg deliberately NOT
  -- that absence is what proves those files need no database. `nightly.yml` mentions
  pytest zero times. `run_all_versions.sh` mentions it zero times and must, because the
  two harnesses stay independent and the shell runner invoking pytest is the cross-harness
  call the project forbids. So 8 files and 93 test functions, 26% of the corpus, ran
  nowhere: green when somebody ran them by hand, silent when they stopped.

  They were never broken. Measured on `pg18a` with the driver present, at the time the gap
  was filed: 99 collected, 289 checks, 289 pass, 40 seconds. Nothing ran them.

  `--pgc-expect-tests` is now passed by BOTH jobs, from `test/pytest/expected_tests.txt`.
  The flag existed and nothing used it. What it closes is narrower than "pytest passed
  with no tests" and worse: a file list that resolves to real files and collects FEWER
  tests than it should. Measured, dropping one file from the guard list:

      unarmed   rc=0   "255 passed"          17 tests gone, nothing said
      armed     rc=4   "collected 255 test(s) but expected 272"

  A nonexistent path already fails on its own, so that was not the hole. A valid-but-short
  list was.

  THE NUMBERS ARE IN A TRACKED FILE, not in the workflow and not in an environment
  variable, for the reason `check_ledger_budget.txt` gives about its own: a change to one
  is then a diff a reviewer sees, sitting next to the test that moved it. `PGC_SKIP_TIMING`
  is the precedent for the other choice -- set in two workflow files, suppressing whole
  suites for months, with no diff ever showing it.

  The SPLIT is derived from `NO_CLUSTER` in `test_harness_deps.py`, in both jobs, rather
  than written out again: two copies of which file needs a database is a thing that goes
  stale silently. `test -n` guards every derived value, because an empty read would omit
  the flag and fail OPEN.

  `test/pytest/README.md` recorded the old reason and it had gone stale twice over: it
  said CI would have to install from `requirements-test.txt` first, which `pytest-guards`
  already does, and it proposed registering the run in `SUITES`, which is the
  cross-harness invocation the independence rule forbids. A second CI job was always the
  mechanism.

- The mutation ledger covers a third suite: `differential`, 204 checks (#752).

      suites_not_covered          250 -> 249
      checks_never_observed_red   951 -> 1155
      covered                     harness_selftest, native_join_runtime_filter, differential

  BOTH NUMBERS ARE DERIVED FROM THE FILES, never computed from the old ones, and this
  change is its own argument for that rule. Written against an earlier base the same
  seed produced `913 -> 1117`; #983 then landed forty rows and pruned two, and the
  census became 1155. Carrying 1117 forward would have been arithmetic that was true
  when it was written and false when it shipped.

  CORRECTION, and the wrong version is left visible because the sentence was an
  INSTRUCTION. This entry first said the census is `grep -c` over the ledger. It is
  not. The gate compares the budget against the count of rows whose last-red is
  `never`:

      awk -F'\t' '$4=="never"' test/check_ledger.tsv | wc -l

  SECOND CORRECTION, by the same rule that kept the first one visible: #1010 inserted
  the majors a row claims as field 4, so on the shipped tree the last-red is field 5 and
  the command above reads a major where it expects a date. The form that works is
  `$5=="never"`, and `check_ledger_budget.txt` carries it. What the number COUNTS is
  unchanged -- the key is still `(suite, part, name)`, so a row is still one check --
  which is the reason the major is a set in a field rather than part of the key.

  A plain row count agrees with that only while nothing has ever been seen red, which
  is true of this tree today (1155 rows, 1155 never, 0 ever red) and stops being true
  the first time a check is attacked -- the event the ledger exists to record. So the
  number here was right by luck and the method was wrong, in an entry whose whole
  subject is derive rather than carry. Reported by @OffgridwithJD. The correct
  derivation now sits in `check_ledger_budget.txt` beside the number it governs, so
  the next person does not have to find it in a changelog.

  The ceiling is the registered list minus the ledger's own suites.

  WHY THIS SUITE, measured rather than chosen by taste. It is the heap-versus-columnar
  differential correctness suite, so a check that cannot fail there is a wrong answer
  nobody sees. 16 of the last 300 commits touch it, so the gate will fire. It runs in
  19 seconds, and no open change touches it.

  THE RISK THAT DECIDED IT WAS STABILITY ACROSS RUNS, because a suite whose checks move
  between runs churns the ledger and fires the gate on nothing. Two consecutive runs on
  PG17: 204 records, 204 distinct names, zero duplicate keys, and the two sets identical
  in NAME AND IN VERDICT -- the second half matters because a flipped verdict churns the
  `last observed red` column while the keys stay still.

  Proof that seeding changed behaviour, re-run against this base:

      after seeding, a log with one unseen differential check    rc=1, REFUSED
      before seeding, the same log against main's ledger         rc=0, not refused
      after seeding, the real log                                rc=0, no false red

  The middle row is the point: the gate refuses an unseen check only in a suite it
  covers, so before this those 204 checks were invisible to it. The third stops the
  first from being bought with a gate that refuses everything.

  The tax is the gate working: a change adding a check to `differential` now needs the
  ledger regenerated in the same commit, which is a reviewable diff.

- `test/selftest/470` now has the pytest half it shipped without (#994).

  #998 added the shell part and no pytest twin, against the owner's rule that a test
  living in one harness is not finished. `test_skip_loop_arms.py` is that half, and it
  is not a port: the shell part runs the sweep over the real corpus and asserts no
  mismatch survives, which measures the TREE; this one drives the same tool over
  planted trees whose right answer is known, which measures the INSTRUMENT.

  The distinction is the reason for writing it rather than a justification after the
  fact. A classifier that filed every site as `armless` would report zero mismatches,
  and the shell part's population premises would still pass on whatever loops remained.
  Six tests, seventeen checks: the clean site is compared rather than quietly skipped,
  a renamed sibling is caught, an armless branch and an interpolated one are each
  counted as themselves, and `compared + armless + interpolated == loops` so nothing
  falls out of the report.

  Three removal proofs, each mutation asserted to apply before the run. Stopping the
  mismatch report reddens the rename arms; comparing the interpolated site literally
  reddens the other two, because removing the tool's refusal to compare manufactures a
  FALSE mismatch on a correct site.

  The third decided the control's shape. Its first version compared two trees, one clean
  and one drifted, and a classifier that reports a mismatch naming the WRONG LINE passes
  that: `0/1` either way, measured on the mutant. So the control plants both loops in one
  file and pins the drifted loop's own line, which is the only form that can tell "it
  found my bug" from "it found something". Raised in review by @OffgridwithJD.

  A fourth proof closed a blindness the first three shared. Returning 2 from the tool's
  `main()` while still printing correct counters left all six tests green: every one of
  them reads stdout and none noticed the tool had become unusable. The shell half caught
  it through its `TOOL FAILED` fallback, and there it was a PREMISE that failed while the
  headline arm stayed green. So the exit code is now asserted once in the shared helper,
  and under the same mutation all six fail. Reported by @OffgridwithJD, whose own first
  probe of it was invalid and said so: `sys.exit(2)` appended after the `__main__` guard
  applied cleanly and moved nothing. Asserting a mutation APPLIED is not asserting the
  behaviour MOVED.

  And the partition arm, `compared + armless + interpolated == loops`, is the strongest
  line in the file and the cheapest to satisfy wrongly: a classifier filing everything as
  `armless` satisfies it perfectly. It is load-bearing only because the per-bucket arms
  sit beside it, and that is now written where a reader will find it rather than left to
  be worked out.

- The pytest harness reports its own check totals, and the record stream is
  reconciled against what arrived (#937, third phase).

  A run now ends with `checks run: N` and an `accounting:` line counted from the
  records, so the harness states what it did rather than leaving it to be
  inferred from pytest's test count. Five assertions across two tests is five.

  The reconciliation compares two quantities that arrive by different routes:
  what the recorder held, read in the process that ran the test, and what
  arrived, read back off the report after it was built. Under `-n` the second
  route crosses a process boundary.

  Reconciling the count against the records would have been vacuous, because
  the second phase made the count `len(records)` on purpose. Partitioning the
  records into verdict buckets and summing them is the same trap. Both compare
  a value with its own definition.

  A record that does not arrive, or that carries a verdict outside the closed
  set, refuses the run and names the test. Both refusals are proven by injecting
  the failure from a conftest, because no code in the tree drops a record and an
  arm that waits for a real defect is not evidence the check can fail.

  It is a transport check and not a completeness check. A record created after
  the report was built is invisible to it, because both quantities come from one
  read of the recorder at one instant. That is a limit rather than an oversight:
  the totals are built from what arrived, and under `-n` the controller has no
  recorder to consult. An arm asserts the limit so it cannot be claimed away.

- Every counted assertion in the pytest harness produces a record, and the
  count is derived from them (#937, first phase).

  The shell harness makes counting and recording the same call, so no path can
  do either alone, and reconciles the totals afterwards. The pytest half reaches
  the same property through Python rather than through the shell's format.

  It reaches it more strongly, because Python can remove the possibility instead
  of policing it. The count is not a second variable kept in step with the
  records; it is `len(self._records)`, a property with no setter. A count that
  cannot be written cannot drift from the stream it counts.

  Measured before this: `_counted()` at 15 call sites, the counter incremented
  by one line and read by one, and zero per-assertion records.

  The record is an object on the recorder, not a formatted line. The shell's
  record is tab separated, so `pgc_record` has to strip tabs and newlines out of
  a check name. There is no separator here to smuggle, and a name carrying both
  is asserted to round-trip byte-identical, so the class of defect cannot return
  silently if these ever become a line.

  `cannot_run()` records `UNRUN` rather than a pass. An assertion that declined
  to run is an outcome like any other.

  A refused assertion leaves no record: a `VacuityError` means the assertion
  never ran, so the stream is outcomes rather than attempts.

  Still to come in #937: the verdict resolved from the outcome, and a session
  reconciliation that can fail.

- A serial inner Hash Join can push the build-side keys into a direct
  columnar scan (#752).

  The coordinator wraps core Hash Join. It does not keep a HashPath from
  `joinrel->pathlist`. It builds a private path, drains the build side into a
  tuplestore, then lets Hash replay that spool. The scan skips chunk groups
  outside the conservative key interval when types and collations match. It
  also rejects non-matching rows with a Bloom filter of those keys, using the
  same saturation cap as on-disk bloom filters.

  The path is serial and INNER only. LEFT, SEMI, ANTI, CROSS, parallel, and
  projection-backed outers are refused. `pgcolumnar.enable_join_runtime_filter`
  is off by default until the skip is measured on the join fixture.

  `EXPLAIN (ANALYZE)` reports `Runtime Filter Groups Removed` and
  `Runtime Filter Rows Rejected`. Those counters are dedicated. They are not
  `InstrCountFiltered1`.

- Every check result is machine-readable, and counting a check is the same
  operation as recording it (#917).

  `check`, `check_num` and `check_text` printed `PASS` or `FAIL` and nothing
  else, so proving that a mutation reddened one NAMED check meant grepping prose
  and retyping the result. That is also how a reverted guard once reported plain
  green while the check count fell from 190 to 186: the suite passed, and the only
  evidence anything had changed was a number nobody compared.

  `lib.sh` had eleven places that bumped `PGC_CHECKS`, each with its own outcome
  line beside it, which is eleven chances to add a twelfth and forget the line.
  `projections.sh` did exactly that with an `expect_fail` at ten call sites, for
  as long as it existed. There is now one, `pgc_record`, so a helper cannot report
  an outcome without being counted and cannot be counted without reporting one.
  Every human line is byte-identical; 3,762 call sites is past what a careful
  refactor can be trusted on, so both harnesses pin the exact strings.

  Each record names the suite, the part, the check, the verdict and the reason.
  The part matters because `harness_selftest` sources 40-odd parts into one shell
  and phrases its premises to be copied, so a key of suite and name is a key of
  check NAMES rather than of checks: 583 records give 579 distinct pairs against
  582 distinct triples. It is derived from `BASH_SOURCE`, not from a convention.

  A skipped wall-clock check is a fourth counted outcome. Under
  `PGC_SKIP_TIMING`, `check_timing` and `check_ratio_needs_quiet_machine` printed
  a `SKIP` line a reader sees while leaving the count at zero and emitting no
  record, in branches no arm reached. `checks run:` now reports the checks a suite
  encountered rather than the ones it evaluated, and the summary reconciles four
  counters against it. A suite that skipped every check reports `SKIPPED` rather
  than `PASSED`, which the old zero-check condition caught only by accident.

  The matrix reconciles each suite's records against the count its log states, and
  names the cause rather than the arithmetic: more records than counted is a check
  that ran in a subshell, fewer is a counter bumped outside `pgc_record`.
- A ledger of which checks have ever been seen red, and under what (#918).

  Nothing recorded it. That is the gap that let 39 checks across 35 suites ship
  unable to fail, three of them inside the suite whose whole purpose is to stop
  exactly that: the gate answered "did anything print FAIL" and had never
  answered "could anything print FAIL".

  It records that a named check WAS OBSERVED RED in a recorded run. It does not
  claim the check is proven able to fail, which needs a named mutation applied
  deliberately; conflating the two would put a claim in the ledger that nothing
  measured. It is fed by every real failure, not only by deliberate mutation
  runs.

  `run_all_versions.sh` runs the gate before it removes the build directory,
  which is the only place a matrix run can reach every suite's log. What the gate
  refuses is a check the committed ledger has never seen, in a suite the ledger
  covers. Regenerating the ledger is the intended fix and a reviewable diff.

  The two tracked numbers are different kinds of thing, and the first design
  treated both as ceilings and deadlocked. `checks_never_observed_red` is a
  CENSUS: every new check enters as `never`, so bounding it means every added
  check breaks the gate and the only way to land one is to raise a number the
  design says may only fall. It shipped that way once, at 614 rows, 614 never,
  ceiling 614. `suites_not_covered` IS a ceiling, because adding a check to a
  covered suite does not move it, and the gate refuses to see it raised above its
  previously committed value rather than leaving that to review.

  The row is keyed on suite, part and check name. The part matters because
  `harness_selftest` sources 40-odd parts into one shell and phrases its premises
  to be copied, so a name-only key is a key of check NAMES: 583 records give 579
  distinct pairs against 582 distinct triples. A rename is detected and named
  rather than silently resetting a check's history to `never`, and the mutation
  column accumulates a set rather than keeping only the most recent attack.

  Bad input fails closed. An unreadable log, an empty one, and a record missing
  its verdict each returned success before, which is worse than no gate because
  it certifies.

- A write that wrote no rows no longer passes as a fixture that built something
  (#432).

  `INSERT ... SELECT ... WHERE false` writes nothing and raises nothing. psycopg
  reports `INSERT 0 0` with a `rowcount` of 0, and nobody in the pytest corpus read
  either field -- so the fixture a test meant to build did not exist, and every
  assertion below it compared two empty things. That is `insert-wrote-no-rows` in
  `test/pytest/VACUITY_MODES.md`, which now lists it as refused rather than as a gap.

  THE COMMAND TAG DECIDES, NOT THE ROW COUNT. `SELECT 0` and `INSERT 0 0` both carry
  `rowcount == 0`, so a guard keyed on the count alone would refuse every test whose
  last statement was a SELECT over an empty result. `statusmessage` is the server's
  own tag, so this guard never parses SQL. Measured on PG 18 against a pgcolumnar
  table: DDL reports `CREATE TABLE`, `SET` or `TRUNCATE TABLE` with `rowcount` -1, an
  `INSERT ... WHERE false` reports `INSERT 0 0` with 0, and `UPDATE 0` and `DELETE 0`
  likewise.

  A deliberate zero stays writable. A DELETE that must match nothing is a real
  negative control, and `expect.wrote(cur, 0, name)` says so: it compares the count
  and marks the write as named. An unnamed zero fails the test, and naming a count
  does not excuse a wrong one. The refusal runs in the CALL phase rather than a
  teardown fixture, because #931 measured that a teardown guard reports the test it
  guards as PASSED and fails separately.

  TWO PROPERTIES, TWO FILES, PROVEN SEPARATELY. The classifier and the refusal live
  in `test/pytest/test_writes_wrote_rows.py`, which needs no database: a stub cursor
  carrying the two measured fields exercises them exactly. Whether the connection the
  tests actually use is watched is a different claim that no driver-free arm can make,
  and `test_the_connection_the_tests_use_is_watched` makes it through a real
  `INSERT ... WHERE false`, on both `conn.execute` and a cursor the connection handed
  out -- 24 sites and 42 sites in the corpus respectively, so a proxy watching only
  the connection would leave most of it unwatched.

  The split is load-bearing, measured rather than asserted. Unwiring the connection
  proxy in `conftest.py` and changing nothing else leaves the driver-free file at 10
  passed and reds the wiring arm alone. That is #917's defect in miniature -- its
  pytest twin tested a function's body and left the runner's CALL to it uncovered, so
  removing the call kept that half green -- and it is why these arms are in two files.

  Prove-by-removal, five mutations, each applied by exact string match and the file
  asserted to still parse: no refusal -> 2 arms red; the command tag ignored -> 2 red
  and the wiring arm red; every statement treated as a write -> 4 red; the connection
  not wrapped -> 0 driver-free red and the wiring arm red; the acknowledgement not
  recorded -> 1 red. Control: 10 passed driver-free, 9 passed with a cluster.

  Full corpus with a cluster: 246 passed, and none of the 12 write sites already in
  the corpus reddened -- the false-positive budget this guard needed before it could
  ship.

- A broad `pytest.raises` must name a SQLSTATE, and the block must hold one
  statement (#432).

  `pytest.raises(psycopg.Error)` claims that one of 254 SQLSTATEs arrived, across 42
  SQLSTATE classes, counted against psycopg 3.3.5. It does not claim even that much.
  Measured on this tree before the guard landed, this reported `1 passed`, exit 0:

      with pytest.raises(psycopg.Error):
          conn = psycopg.connect("host=/nonexistent-socket-dir dbname=pgc")
          conn.execute("SELECT pgc_definitely_no_such_function()")
      expect.num(1, 1, "the server rejected the call")

  What satisfied it was `OperationalError` with `sqlstate` None: the connect failed,
  nothing reached a server, and the statement under test never executed.

  `pgc_vacuity.py` now refuses two shapes at collection time, found by walking the
  `ast` rather than matching lines, so an offending file does not collect at all. A
  `raises` over `Error`, `DatabaseError`, `Exception` or `BaseException` must bind the
  exception and pin its SQLSTATE, and any `raises` block must hold exactly one
  top-level statement. `expect.sqlstate(exc.value, "42883", name)` is the honest form
  the refusal points at; it compares a typed field, refuses a two-character SQLSTATE
  class as the prefix claim it is, and refuses an empty set of codes so the tuple
  escape hatch cannot become the hole.

  The family list is bound inside the scan rather than at module level. Every
  `conftest.py` under `test/pytest/` is imported before collection, so a module-level
  tuple is writable from the corpus the rule polices -- `import pgc_vacuity` then
  `pgc_vacuity.<the tuple> = ()` -- after which the scan reports zero offences for
  ever and the suite is green with the guard off and nothing saying so. An arm writes
  three spellings of the name onto the module and requires the refusal to still
  arrive.

  This CLOSES `raises-too-broad`, which moves to `VACUITY_MODES.md` section 2, and
  only NARROWS `raises-catches-setup`, which stays in section 3.4. The statement rule
  counts TOP-LEVEL statements, so two shapes still walk past it, each being one
  statement that performs the setup inside the block: a call to a helper, and a
  compound statement such as a `for` holding the setup and the statement under test.
  Both are measured at `1 passed`, exit 0, zero offences, and both have an arm
  asserting the scan reports nothing on them, so the residual is a measurement rather
  than a sentence. A recursive statement count would catch them and would also refuse
  a legitimate single-statement loop; what would close the mode is a claim about which
  statement raised.

  Parsed rather than grepped, because the suite writes the forbidden shape inside a
  `pytester.makepyfile` string in every arm. Over `test/pytest/*.py` the `ast` finds
  5 `pytest.raises` call sites and reports 0 offences, while a `pytest.raises(` line
  regex matches 35 lines, 30 of them inside a string literal or a comment. Swapping
  `ast.parse` for that regex makes the layer refuse its own test suite with 22
  invented offences and exit 4, which is mutation 11 of 11 in the removal proof.

  THE ARMS LIVE IN ONE HARNESS. `test/pytest/test_raises_sqlstate.py` carries all of
  them, through `pytester` and through the scan directly. An earlier version of this
  change also shipped a shell mirror, `test/selftest/440-a-raises-must-name-a-sqlstate.sh`,
  which checked the scan by GREPPING ITS SOURCE: 44 of its 55 checks were `grep -c`
  against the function's text and it invoked `python3` zero times. Reviewing it,
  @linuxhikerpm measured three faithful neuterings -- `False and` prefixed, nothing
  renamed, every pinned substring left in place -- and all three left that part at 55
  passed while the scan went blind. A text pin catches a rewrite or a deletion; it
  cannot catch `False and`, which is how a guard actually dies.

  The mirror is gone, and the second reason is the one that settles it: the shell
  harness and the pytest corpus are parallel in functionality and do not drive each
  other. A shell part whose whole subject is another harness's source text is a
  dependency rather than a parallel guard -- it asserts against an implementation
  instead of against the product. So the neutering proof is now two arms that copy the
  layer, disable one condition faithfully, and require the copy to go blind while still
  containing the text a grep arm would have pinned.

  THREE DEFECTS @linuxhikerpm FOUND IN THE GUARD ITSELF, each reproduced before it was
  fixed. A bare `exc.value.sqlstate`, and a `code = exc.value.sqlstate` never read, both
  satisfied the pin while asserting nothing -- the rule counted any attribute named
  `sqlstate` anywhere in the body. It now requires the read to reach a CALL, following
  one hop of assignment so the honest `code = ...` / `expect.text(code, ...)` form is not
  refused. And `pytest.raises(expected_exception=...)` escaped BOTH rules, because the
  class was read from `call.args[0]` and the item was skipped before it was recorded,
  which made the statement rule silently conditional on the class being positional while
  the documentation stated it unconditionally.

- The harness guards run in the gate, without a database, and which files that is
  gets DECIDED rather than listed (#432).

  `conftest.py` imported psycopg at module scope, and conftest is imported before
  every run, so a database driver was a hard requirement of COLLECTING the whole
  pytest corpus -- including every test that never opens a connection. Deferring
  that one import into the two fixtures that connect lets the guard-testing half
  run where the gate runs: `.github/workflows/ci.yml` gains a `pytest-guards` job
  with no database, no build, an interpreter and two pinned packages. The job
  derives its file list from `NO_CLUSTER` in `test/pytest/test_harness_deps.py`
  and its pins from `requirements-test.txt`, so neither is a second copy, and it
  asserts psycopg is ABSENT before running -- otherwise the tests would pass for
  the ordinary reason and prove nothing about the coupling.

  `NO_CLUSTER` is now decided, not declaimed. The only arm over it asked whether
  the files it named EXIST, which is one direction of a membership claim, and the
  missing direction is the one that loses coverage: a database-free test file that
  nobody adds to the list is simply absent from the job, every arm stays green and
  nothing says so. It had already happened twice -- `test_build_refusal.py` and
  `test_layer.py` both need no database and neither was listed. The property is
  now computed from the corpus by an ast walk and required to equal the list in
  both directions, so the job runs every database-free file rather than the four
  somebody remembered.

  An ast walk rather than a line regex, because three shapes here defeat a grep: a
  file may name the driver in a docstring, discuss a cluster fixture in prose, or
  build another test as a string for `pytester`. And needing a database is not
  importing the driver -- a test reaches a cluster through a FIXTURE and may import
  nothing -- so a file is cluster-bound if it imports the driver at module scope,
  if any test or fixture in it requests a fixture that reaches a cluster, or if it
  drives a cluster-bound file as a subprocess. The connecting fixtures are read off
  `conftest.py` rather than named in the classifier.

  Every rule was proved by removal: eight mutations, each asserted to have applied
  and restored byte-exact, each reddening a named arm. Two of them survived the
  first pass and found real gaps -- a closure over each file's own fixture graph
  that changed no classification, removed as dead, and a prose filter with no
  killing arm, which now has one. The classification of all twelve corpus files was
  exercised against reality: each of the six called database-free passes with
  `import psycopg` shimmed to raise and no usable `pg_config`, and each of the six
  called cluster-bound fails, for the driver or for `pg_config` and nothing else.

  Because the corpus is not in `SUITES` and the job runs only the database-free
  files, none of those arms runs in the gate, so
  `test/selftest/350-the-pytest-corpus-must-be.sh` runs the membership decision
  through the module's command line, with its own fixture corpus to fail against.
  A guard that does not run is a comment.

- Exact zone-map boundary coverage now lives in matching shell and pytest tests
  (#831).

  The `<=` and `>=` arms put the constant exactly at a row-group minimum or
  maximum and compare returned rows with a heap twin. The `<`, `>`, and `=`
  mirrors assert groups removed with bloom disabled, so a conservative pruning
  regression cannot hide behind a correct answer. Each of the five one-token
  strategy mutations was proved to fail its corresponding assertion.

- The source fingerprint has one implementation, in Python, and both harnesses
  call it.

  `test/lib.sh` and `test/pytest/pgc_cluster.py` each carried their own answer to
  "what was this binary built from". In one day the pair produced four defects,
  two in each copy, and not one was found by whoever wrote that copy: the Python
  side never walked `objstore/*.c`, then mixed in each file's bare name so
  `src/module.c` and `objstore/module.c` were interchangeable, then omitted each
  build directory's `Makefile`; the shell side hashed `xargs -0 cat | md5sum`, a
  stream with no per-file boundaries, so moving bytes between two files left the
  hash unchanged while the source no longer compiled. The Python docstring
  asserted parity with the shell throughout all four. It was false when written
  and stayed false through two rounds of fixing.

  `test/pgc_fingerprint.py` is now the only implementation. It is Python rather
  than shell because the portable half should be the one that survives: bash is
  largely a GNU thing, while Python is present on FreeBSD and Windows where bash
  is not. It uses the standard library only and runs on the system interpreter,
  never the pytest virtualenv, so a freshness gate cannot depend on the test
  dependencies of a harness it gates.

  It is also faster. The shell forked `md5sum` once per file; the module starts
  one interpreter:

      shell, forking md5sum per file      239 ms per call
      the module                           26 ms per call
      across 261 suites, twice each        124 s  ->  13 s

  Unifying them closed a fifth defect that neither implementation had been
  suspected of. `sort -z` orders by LOCALE COLLATION and nothing in the harness
  pinned a locale, so one tree fingerprinted two ways depending on the machine:

      LC_ALL=C             6d122a7158d5
      LC_ALL=en_US.UTF-8   0b59bd75fa4f

  `en_US.UTF-8` is a common desktop default, so a developer could stamp a tree
  and have CI read it back and call the binary stale. The module sorts bytes,
  which is what `LC_ALL=C` produced and what every stamp already on disk was
  written with, so no existing stamp is invalidated.

- The test harness refuses to measure a binary that was not built from the source
  under test.

  `test/run_all_versions.sh` builds and installs once per major and then runs every
  suite with `PGC_SKIP_BUILD=1`, so no suite could tell whether the binary it
  measured came from this tree. The controller now records a fingerprint of the
  build inputs after a successful install, and `pgc_setup` checks it in every
  suite, whether that suite built or skipped. A mismatch is fatal and names both
  fingerprints.

  A second check covers the other half. `make install` does not reload anything:
  `shared_preload_libraries` maps the library at postmaster start, so a reinstall
  under a running server leaves the backends executing older code than the file on
  disk. The `.so` mtime is compared against `pg_postmaster_start_time()`, and the
  message says to restart rather than only that something is wrong.

  Neither check fails when it cannot answer. Someone who ran `make install` by hand
  has no stamp, so that case prints `freshness UNVERIFIED` and says which question
  went unanswered, rather than printing nothing and letting a reader assume it
  passed.

  `pgc_freshness_verdict` and `pgc_running_binary_verdict` are pure functions of
  strings, so `test/selftest/340-the-binary-must-be-built-from.sh` drives them
  directly: 27 arms including both empty inputs, a non-numeric epoch, equal
  timestamps on the boundary, sensitivity to each fingerprint input class, and
  the three the review added -- the derived build-directory set, the running
  binary check driven against a live cluster, and devloop writing the stamp.
  `harness_selftest` goes from 261 checks to 288, both measured.

- `IN (...)` and `= ANY(array)` now test each listed value against row-group
  zone maps and bloom filters instead of reducing the list to its `[min,max]`
  hull (#752).

  A scattered list whose hull spans a table could previously read every row
  group. The set is now one internal predicate whose element tests are a
  disjunction, while the outer predicate list remains a conjunction. The
  executor still rechecks exact membership, so pruning remains conservative.

  Per-element evaluation is capped at 128 non-NULL entries. Larger lists retain
  the bounded two-key hull because exact vector refinement otherwise costs
  elements times rows.
- `test/projection_rewrite.sh`, 84 checks. Nothing in the tree asserted that a
  projection answers after a rewrite, which is why this was silent.

  Every arm compares a `pgc_set_hash` of `read_projection` against the base table
  rather than checking that the call did not raise, so a projection re-recorded
  EMPTY fails -- which matters because the correct end state after a bare
  `TRUNCATE` is an empty projection that answers. Every arm also asserts what its
  operation DID (`REWROTE`, `NOOP` or `FAILED`) and reports its properties as
  `UNMET_PRECONDITION` rather than as passes when it did not: an operation that
  failed or no-opped leaves the storage id unchanged and `read_projection`
  answering, which is indistinguishable from a path that handles projections
  correctly. Three arms carry `pgcolumnar.vacuum`, `vacuum_sorted` and `cluster`,
  which already re-record for themselves, so a future fix moved into the table-AM
  callback reddens here instead of double-recording.

### Changed

- Serial join runtime filter is on by default (#752).

  Three measured cases, which is the argument. The docs being ready is why the
  guidance exists, not why the default is right:

      clustered on the join key   19 of 20 chunk groups removed, 1 read
      scattered                   0 groups removed, but Bloom rejects more
                                  than 15,000 of 19,800 non-matches
      build side too large        the Bloom disables itself

  The third is why this is safe everywhere. A filter that is on for every user
  needs an answer for the case where it helps nothing, and turning itself off is
  that answer. Group skip still needs the fact table clustered on the join key,
  and a scattered fact table still cannot drop groups. SET the GUC off to compare.

  NOT MEASURED: the overhead of a filter that is on, not saturated, and rejecting
  almost nothing, which is a join where nearly every row matches. Saturation bounds
  the pathological end and the scattered case bounds the middle, so this is reasoned
  rather than measured. It is the gap to close if the default is ever doubted.

### Fixed


- The standing parity arm graded a hand-written list, and nothing enforced it
  (#432, #1046).

  `test_the_ported_suites_in_this_tree_are_graded_one_for_one` grades the pairs it is
  GIVEN. A pair that existed and was not given to it was not graded, and nothing said
  so: the arm passed, grading the ones it knew about, and reported a clean verdict for
  a tree it had not fully looked at. **Absent-from-the-list and no-gap-found produced
  the same green.**

  Latent throughout -- the declared set happened to equal the tree, so nothing was ever
  silently ungraded. Found by @OffgridwithJD, whose eighth port would not have been
  graded by it, and it would have gone live the moment a ninth landed undeclared.

  `COMPLETE` and `INCOMPLETE` are declared at module scope and asserted BOTH ways, the
  shape `SHELL_REFERENCES` already uses: a pair in the tree that is not declared reddens
  with the stem named, and a declared stem whose pair has been deleted reddens too. A
  pair is derived as `test_<stem>.py` beside `test/<stem>.sh`, so the 24 pytest files
  with no suite stay out without a second exemption list.

  **It forbids one thing, and that was the decision rather than an oversight.** An
  incomplete pair could previously land declaring nothing; now it must carry a stem and
  a reason. The escape hatch is attached rather than the case forbidden, and the cost
  today is zero -- 8 pairs exist, 8 are declared, `INCOMPLETE` starts empty. Named by
  @OffgridwithJD, who pointed out that "forbids nothing that was allowed before" was the
  comfortable phrasing.

  Five mutations, each asserted to apply by md5 and the tree restored after:

      drop a stem                            every pair in the tree is declared
      declare a stem with no files           every declared stem is a pair that exists
      move a stem to INCOMPLETE, thin reason every incomplete pair says why
      a stem in both lists                   no stem is both complete and incomplete
      move a stem to INCOMPLETE, full reason ALL GREEN -- the hatch works

  The last is the one that matters: it demonstrates the escape hatch is usable, rather
  than only that the guard fires.

  **AND IT DID NOT, FIRST TIME.** The cardinality premise below was added AFTER that
  mutation was run and the mutation was never re-run, so "the hatch works" was true of a
  file that no longer existed. The premise counted `len(COMPLETE)` against a hard-coded
  8, so moving one stem to INCOMPLETE took it to 7 and reddened the suite: a pair could
  not be declared incomplete without going red, which is the one thing this change exists
  to allow. Caught by @OffgridwithJD reviewing, not by the author re-running.

  The premise now counts the DECLARED TOTAL against the pairs that exist on disk, both
  derived. That keeps the guard -- an emptied `COMPLETE` still reddens, because 0 does
  not equal 8 -- permits the hatch, and removes a hard-coded number that would have
  needed an edit the first time a ninth pair landed, which is the budget shape #982
  argues against.

  All five mutations were then re-run against the file that ships. Three of them redden
  the accounting premise as well as their named arm, which is correct rather than noise:
  a dropped stem, a phantom stem and a double-declared stem each genuinely break the
  accounting, and the named arm fires beside it to say which.

  **Hoisting the list made an existing guard fire**, which is the sweep working. The
  standing arm's loop now iterates a module-level name rather than a literal, so
  `test_loop_coverage_premise.py` demanded a cardinality premise -- an empty `COMPLETE`
  would leave every arm unrun and the verdict comparison trivially equal.


- `compare_to_bash.py` read five of the eight check helpers `lib.sh` defines
  (#432, #1040).

  The bash side of the parity tool matched
  `check(?:_num|_ratio|_text|_timing)?`, so `check_unrunnable`, `check_skip` and
  `check_ratio_needs_quiet_machine` were invisible. A property asserted through one of
  the three was never reported MISSING and could not move `rc`, which means **a pair
  could grade one-for-one on the strength of the grader's blind spot.**
  `hilbert_locality` was exactly that, and #1041 closed the two properties it was
  hiding.

  It never drifted out of date: `0cbf574` introduced that pattern, and
  `check_unrunnable` already had 21 call sites that day. Same defect shape as the
  python side in #1036 and #1038 -- a rule true of most of a class taken for a
  property of the class -- sitting on the other side of the same tool for five days.

  The helper list is hand-written, so it is pinned the way `_NAME_ARG` is: an arm
  re-derives it from `lib.sh`'s DEFINITIONS and fails with the helper named. Two more
  arms state what the tool does not cover -- the four suite-local helpers
  (`check_structure`, `check_reconstruct`, `check_split_happened`, `check_float`),
  none of whose suites has a pytest twin, and the pattern shape that used to make a
  prefix unreadable.

  **The longest-first ordering is NOT what makes that work, and the code said
  otherwise until it was measured.** Python's `re` backtracks across alternatives, so
  a pure reorder reads both names identically. What the old pattern could not do was
  read `check_ratio_needs_quiet_machine` at all: it matches `check_ratio`, wants
  whitespace, finds `_needs`, backtracks to the empty option, wants whitespace after
  `check`, and fails. Measured on a fixture holding both, the old form reads
  `['short']` and this one reads `['short', 'long']`. The arm pins the pattern shape;
  the ordering is readability.

  The population is `check` or `check_<something>`, not `check[a-z_]*`: the loose form
  also matches `checks_in` in `decode_interrupts.sh`, a counting utility that returns a
  number and records nothing.

- `compare_to_bash.py` read the wrong argument for the four helpers whose name is not
  last (#432, #1036).

  The previous fix replaced "the first quoted argument" with "the last argument". That
  is true of 14 of `Expect`'s 18 helpers, but it is a property of most of them rather
  than of the class, and the last argument is a real string in each of the other four --
  so a wrong name looked exactly like a right one:

  | call | last argument | the name it records |
  | --- | --- | --- |
  | `refusal(result, name, *patterns)` | a message PATTERN | `name`, argument 1 |
  | `cannot_run(reason, detail="")` | the DETAIL of one run | `reason`, argument 0 |
  | `plan_marker(plan, key, name=None)` | a plan KEY | the `name=` keyword only |
  | `plan_node(plan, ..., name=None)` | a field of the NODE | the `name=` keyword only |

  `refusal` is the worst: the real name goes MISSING and a fragment of an error message
  arrives as an EXTRA, which is two false entries from one call.

  Measured over every pair in the tree at `73e8e3d`, with the table as the only variable:
  **68 extras, now 67.** Two were false -- a `plan_marker` key and a `cannot_run` detail,
  both on `hilbert_locality` -- and `UNMET_PRECONDITION` appeared in their place, the
  reason code `cannot_run` really records. No pair's verdict moved, because `rc` is driven
  by MISSING and extras never moved it. That is why nothing caught this: the tool reported
  a plausible list, and only the list was evidence either way.

  `UNMET_PRECONDITION` is reported as an extra only because the tool cannot see the bash
  side of it. `hilbert_locality.sh:574` and three lines after it check that property with
  `check_unrunnable`, which the bash extractor's `check(_num|_ratio|_text|_timing)?` does
  not match. Widening it by that one alternative and nothing else takes that pair from
  `rc=0 missing=0` to `rc=1 missing=2`, every other pair unchanged. Filed as #1040: it is a
  port's worth of work, not a tool fix, and this change is only what made it visible.

  The positions live in a `_NAME_ARG` table, because the tool is deliberately standalone
  (`ast`, `re`, `sys`) and importing `Expect` to ask would pull in pytest. A
  hand-written derived value goes stale, so it is pinned: a drift guard reads the real
  signatures out of `pgc_vacuity.py`, recomputes every entry, and fails with the helper
  named. That guard first passed over a missing `refusal` entry -- `name` IS its last
  DECLARED parameter, since `*patterns` is not -- and now accounts for the vararg.

  A second coincidence sat inside the clause that fixed the first, found by
  @OffgridwithJD in review. `-1` is a claim about the CALL SITE while the guard reads the
  SIGNATURE, and they agree only while no optional parameter sits after the name:
  `expect.rows(got, want, "THE NAME", "the reason")` read `the reason`, and
  `expect.plan_marker(plan, "key", "THE NAME")` read nothing at all, dropping a name
  silently. Latent rather than live -- no call site passes a trailing optional
  positionally -- but #1037 makes `allow_empty` a reason string, which is exactly that
  argument. Closed in the signatures rather than in the reader: `rows`, `row_set`,
  `plan_marker` and `plan_node` take everything after the name as keyword-only, so the
  wrong call is now a `TypeError`. No call site changed; all four already used keywords.

- A BOGUS-verdict ledger record is refused by naming the verdict, not by field count (#1013).

- The star-schema join how-to names clustering on the join key (#752).

  Group skip was already measured: 19 of 20 groups when the keys are local,
  0 of 20 when they cycle. The page named the GUC and not that discriminator.
  A fact table that is not clustered on the join key still holds every key in
  every group, so the filter cannot skip. The fold over a join is a later slice.

- A skipped arm records under the name it would have used, so a skipped arm and
  a deleted one are no longer indistinguishable (#994).

  Four `check_skip` calls stood in for 19 named arms under a name none of those
  arms has. When the condition failed, those 19 produced no record at all: a
  reader could not tell which arms did not run, and the ledger could not tell a
  skipped arm from a deleted one, because a skipped arm's row has no matching
  record exactly as a removed check's would.

  The convention was already in the tree, 30 lines below one of the offenders:
  skip under each arm's own name, in a loop.

  The issue counted 17. It is 19. Two arms in `sorted_pathkeys.sh` go through
  `ansp`, which records under its first argument, and a sweep that looked for
  `check` did not see them.

  One arm's name interpolated the very variable whose emptiness causes its own
  skip, so the skip would have recorded a key no real run emits. That name is now
  stable and the collation it names moves into the display, which is not the key.

  Six sites, not four, and 19 arms. Two arms go through `ansp`, which records
  under its first argument. Two sites hold their arms in the `then` branch with
  the skip in the `else`, which a classifier looking only forward reads as having
  no arms at all.

  `340` also skipped five arms behind a branch whose comment said they had
  already been skipped above. Above had skipped the three premises, not these
  five, so on a box with no non-root user five arms produced no record.

  A new selftest part asserts every skip loop names exactly the arms its sibling
  branch would emit. The loop duplicates those names, so a rename desynchronises
  them silently and the skip records under a name nothing emits, which is the
  failure this change exists to remove. That is not hypothetical: writing this,
  a name from another open PR's rename went into the loop, and the comparison is
  what caught it.

  The part reports what it did not compare. One loop's sibling arm is generated
  by a loop of its own, so a literal comparison would be wrong in both
  directions. That loop is the one this change repairs, and it is not covered.
  A total of zero mismatches would otherwise read as a corpus in agreement.

  This is the precondition for arming the orphan guard in #983. Until a skipped
  arm records under its own name, absence cannot mean removal.


- A conftest can no longer switch a vacuity rule off by rebinding a name the
  layer reads (#924).

  pytest imports `conftest.py` from the directory it is policing, into the
  policing interpreter, before collection. Every module-level name in
  `pgc_vacuity.py` is therefore writable by the code it judges.

  #958 closed the datum one exploit used. The three scans that read such data
  are module-level names one frame further out, and each was a two-line conftest
  away from being a no-op. Measured with the pinned runner:

  ```
  GUARD               no conftest    with the scan rebound to a no-op
  order collapse      REFUSED rc=4   PASSED rc=0
  broad except        REFUSED rc=4   PASSED rc=0
  raises not pinned   REFUSED rc=4   PASSED rc=0
  ```

  Plugging a fourth name would reopen this again: the transitive closure from
  the eight hooks is 31 of the module's 47 names, `ast` among them. So the layer
  snapshots its own bindings at import, holds the snapshot in a closure, and
  refuses a run in which any of them changed. Names added later are covered
  without being listed anywhere.

  No allowlist is needed: the module contains no `global` statement, so every
  module-level binding is constant after import.

  The bindings are restored before the refusal is raised. `pytester` runs its
  inner session in-process on the same module object, so without that an inner
  conftest's rebind stays made for every test that follows.

  This is a cost guard, not a lock. Reaching into the hook's `__defaults__` still
  reaches the closure. The criterion is that silencing a rule must cost more than
  stating a reason, which `expect.cannot_run(REASON, detail)` does.

  The shell harness needs no equivalent, and not because bash is simpler: its
  policing runs in a different process from the code it polices. `selftest/260`
  reads `lib.sh` with grep and awk and never sources the file it judges.

- The sentinel sweep no longer excludes an assertion by accident of naming (#938).

  `_comparisons()` selected on the first two parameter names, so `wrote(cur, want,
  name)` sat outside because its first parameter is not called `got`. That happens
  to be the right answer for a cursor, and it would also have been the answer for a
  future comparison whose first parameter was `left`. Exclusion is now a positive
  match on the kind of the left operand, and `inputs == selected + excluded` fails
  when a method matches neither rule. A list of method names is not the fix.

- A loop that never ran asserted nothing, and half of that was already refused by a
  mechanism nobody had recorded covered it (#432).

  `assert-inside-a-loop-over-zero-rows` in VACUITY_MODES.md 3.5 is two shapes.

  **Already refused.** When a loop's body holds a test's ONLY counted assertions, a
  zero-trip loop leaves the count at 0 and `pytest_runtest_call` raises `VacuityError`.
  Measured on a planted test rather than read off the hook: the zero-trip case fails
  with "made no counted assertion" and the same loop with one row passes.

  **Was open.** When the test ALSO asserts outside the loop, the count is non-zero, the
  test passes, and the loop's assertions simply never ran. That is the shape a query
  returning no rows produces, and the shape a glob matching nothing produces.

  `test_loop_coverage_premise.py` requires a cardinality premise for exactly that shape.
  THE POPULATION, measured over the whole corpus before the arm was written: 20 loops
  non-empty by construction and so unable to be zero-trip, 0 in the already-refused
  shape, and **2 at risk** -- both of which already carried a premise. The arm is green
  on arrival, which is the point rather than a weakness: the property was true and
  nothing was holding it there, so what this catches is the third one.

  **IT NARROWS RATHER THAN CLOSES, and 3.5 names the residual.** The honest requirement
  is a premise bounding the cardinality of THIS iterable; what is enforced is a counted
  assertion outside the loop taking `len(...)` of something. `test_harness_deps.py`'s
  loop iterates `sorted(found)` while its premise bounds `len(files)`, because `found` is
  built from `files` in a preceding loop -- so a rule demanding the names match would
  reject correct code, which is how a guard gets switched off. The residual is a loop
  whose premise bounds the wrong collection: a reviewer catches it, a sweep does not.

  Section 5 gains entry 6, struck and anchored to its mode id, which is the first use of
  the rule the previous commit added.

- Section 5 of VACUITY_MODES.md, the "what to add next" list, is checked (#432).

  1a, 2 and 3 are all compared against the mode ids on disk. Section 5 was prose, and
  it was **wrong**: entry 1 still said "the constant exists and nothing writes it" long
  after `query_error()` existed and `test_failed_query_sentinel.py` carried ten arms
  over it, including the producer's uniqueness and the constant's non-uniqueness that
  is the reason the producer exists.

  **That is the most expensive place in the document for a stale sentence**, because
  its only reader is someone about to build something. The near-miss one document over
  is what it costs: a bad enumeration of `test/selftest/340` made an existing block
  look like a coverage gap, and the duplicate was written and proven to discriminate
  before the duplication was noticed.

  **What is checkable is the anchor, not the work.** Entry 1's work landed under four
  test names, none of them the one the entry proposed, so asking whether the NAMED test
  exists would have passed and said nothing. Two arms hold the list instead:

  - every entry names at least one mode id, so it is tied to the inventory at all.
    Entry 1 named none, which is exactly how it stayed wrong.
  - no UN-STRUCK entry names an id section 2 already claims as refused. An entry whose
    id has reached section 2 is done by the document's own accounting, whatever the
    test ended up being called.

  Entry 1 is struck and anchored to `error-swallowed-to-empty`, and it records what the
  entry got wrong rather than replacing it: both halves of "the constant exists and
  nothing writes it" were false -- two sites already minted sentinels by hand, and the
  missing thing was the REFUSAL in four of the five comparisons.

  **Fixing it made the second arm vacuous on this document**, because with every entry
  struck there is nothing left to refuse. The planted fixture beside it is therefore
  the whole of its evidence, and the document says so rather than leaving it implied: a
  two-entry fixture where moving the open entry's id into section 2 must be named, with
  the clean control beside it.

  The totals do not move -- 28 refused, 44 not refused, 72 named -- because the mode was
  already counted as refused. Only the list that tells the next person what to do was
  wrong.

  **And 3.6 now records measured populations, so the next entry is chosen on evidence.**
  Section 5's new rule is that an entry must name a mode id, which makes WHICH id worth
  measuring. Four of 3.6's were counted by AST scan over the whole corpus:
  `truthy-cursor-from-execute` has 7 sites and **all are benign** -- every one is
  `x = cur.execute(...)`, idiomatic in psycopg3, and **zero** branch on it, which is the
  dangerous form; `empty-query-string-succeeds` has 0; the multistatement mode has 1, a
  setup that fetches nothing with a `count(*)` premise right after it; and the
  server-cursor rowcount mode has 2, both legitimate. So all four are PROSPECTIVE, and a
  guard for any of them would be insurance rather than a closure. Recorded because a
  refusal with no population has not refused anything, and 1a counts section 2 as
  "refused today".

  One instrument defect of mine, caught by that fixture: the parser matched the
  SECTION HEADING as an entry. `re.split(r"^## ")` leaves a chunk beginning "5. What to
  add next", which the numbered-item pattern also matches -- inventing an entry 5 that
  is the title and colliding with the real entry 5. The fixture reported 3 entries in a
  two-entry document, which is how it was found.

- The pytest corpus no longer drives the shell harness for properties the shell
  harness already holds, and the inventory of what remains is down to four calls,
  none of them debt (#432).

  CONTEXT.md's rule: the two harnesses are parallel in FUNCTIONALITY and independent
  in CALL. `test_build_refusal.py` drove `test/lib.sh` for seven calls over
  `pgc_write_source_stamp`, `pgc_source_stamp_path`, `pgc_freshness_report` and
  `pgc_freshness_verdict` -- four PURE SHELL functions, so the python arms were a
  second measurement of someone else's subject, agreeing with it by construction.

  **Five arms removed and not one needed porting.** `test/selftest/340` already held
  every property they asserted, and more of it in each case:

  | the python arm | what 340 already had |
  | --- | --- |
  | the stamp writer reports failure | the same, plus a premise that the stamp really was not written |
  | two installations of one major do not share a stamp | the same, plus pkglibdir keying and two unreadable pg_configs |
  | the report names each file and states how many | the same two arms |
  | an empty manifest says so rather than printing nothing | the same arm |
  | a failed digest is unknown, never a false stale | the same, over TWO unreadable files, plus the premise below |

  340's version of the last one carries a premise the pytest twin did not state: that
  the unprivileged read AGREES with the privileged one while nothing is denied.
  Without it the arms measure the user switch rather than the permission denial.

  **The first measurement of 340 was wrong and nearly cost a duplicate.** Enumerating
  its checks with `grep -cE '^check "'` gave 81. The real number is **89**: 340 has
  indented `check` calls inside an `if` and a `for`, and the eight the sweep missed
  are exactly the unreadable-source block. On that bad count one python arm looked
  like a genuine gap, and a duplicate of it was written -- and proven to discriminate
  against a mutation -- before the duplication was noticed.

  **The tell was a duplicate check name.** `pgc_ledger.py` reported "duplicate check
  name in one run, so one ledger row covers 2", and the first response was to rename
  the new check. The right response to a name that already exists is to ask WHY it
  exists. Renaming it hid the only evidence that the work was unnecessary. A check
  sweep has to be anchored at `^[[:space:]]*`, not at column 0.

  Two mutations were run against the duplicate before it was discarded, and the first
  was a no-op for a reason worth keeping: `pgc_source_fingerprint` returns empty
  because the MODULE prints nothing and exits 0, not because the wrapper's `rc != 0`
  branch fires. Mutating that branch changes nothing on this path. The property lives
  in `test/pgc_fingerprint.py`, and the wrapper's contribution is only that it does
  not substitute a value for the module's empty answer.

  What remains in `test_build_refusal.py` is four calls in two arms, both named:
  `test_the_two_fingerprint_implementations_cover_the_same_inputs`, which is the one
  permitted cross-reference because the property IS the relationship between the two
  implementations, and a historical-parity arm whose fixture is its own. `_sh_fp_as`
  is deleted with its last caller.

  TESTS.md loses the rows naming the deleted arms, and three prose passages that named
  them are rewritten to say where the property lives now rather than left pointing at
  arms that do not exist. A backticked name is a claim that it exists.

- A count `grep` never produced no longer reads as "present" (#929).

  #922 replaced roughly 28 `producer | grep -q PAT` tests with
  `[ "$(grep -c PAT ... || true)" != 0 ]`, which fixed a real EPIPE race (#486). The
  replacement answered **present** where the original answered **absent** whenever grep
  produced no stdout, and a pattern that does not compile is the way to get there:

      grep -cE '[' file   ->  stdout is []   (empty, not "0")
      [ "" != 0 ]         ->  TRUE           (a STRING comparison: "" is not "0")

  So the test reported the pattern present for a question it never managed to ask.

  Every pattern in the tree is valid today, so no site was wrong. The hazard is the
  DIRECTION of the next edit: a premise arm phrased to want `present` -- and most are,
  because a premise asserts the fixture really is in the state the test needs -- turns
  GREEN when its pattern stops compiling. It passes BECAUSE the instrument broke, which
  is the failure this harness spends most of its effort refusing. The old form failed
  red.

  **23 sites** compared a count as a string; they now compare numerically, which is
  behaviour-preserving in every case that is not broken. Measured:

      input                      [ "$n" != 0 ]   [ "$n" -ne 0 ]   stderr
      a real count: 0            false           false            no
      a real count: 3            true            true             no
      EMPTY (grep usage error)    TRUE            false           YES

  Both spellings failed the same way. `= 0` is an ABSENCE claim, and on an empty value
  it is false -- which does not assert absence, and is the safe direction once it is
  loud. All 23 sites pass exactly one input to grep, so the value is always a bare
  number and a numeric comparison cannot be confused by `file:count` output.

  THE POPULATION RECONCILES, and the first version of this entry did not. It said
  "58 sites, of which 20 string-compared and 32 numeric" -- and 20 + 32 is 52. The 58
  came from a broad grep and the 20 from the sweep's own narrower one, so two
  instruments were reported as one measurement. With the sweep's pattern corrected:

      on main (cfe1fde9)   58 inputs = 23 string-compared + 35 numeric
      after this change    58 inputs =  0 string-compared + 58 numeric

  `test/selftest/440-a-count-grep-never-produced.sh` holds the arms and a heredoc-aware
  sweep requiring zero string comparisons on a `grep -c`, so the class is closed rather
  than the 23 instances.

  THE SWEEP'S FIRST PATTERN COULD NOT SEE THREE OF THEM. `[^)]*` stopped at the first
  `)`, which is inside the GREP PATTERN rather than at the end of the substitution, so
  any pattern containing a parenthesis hid its own site: `sorted_mark_rename.sh:183` and
  `sorted_pathkeys.sh:53` and `:456`, each spelling
  `grep -cE '^ *(->)? *(Incremental )?Sort'`. The guard and its population came out of
  the same regex, so the guard agreed with the count by construction -- which is why one
  plant now carries a parenthesis and differs from the plain one in nothing else.
  Reported by @jdatcmd. The sweep skips comments as well as heredocs: a flat grep
  counts the paragraph that documents the idiom, which is how a guard comes to flag its
  own explanation.

- `test/harness_selftest.sh` can no longer exit 0 having evaluated nothing (#934).

  Handed a `pg_config` the box does not have, it printed four lines, never reached its
  summary, and **exited 0**. Measured on main:

      /usr/local/pg18a/bin/pg_config    rc=0  1246 lines  checks run: 610
      /usr/local/pgNOPE/bin/pg_config   rc=0     4 lines  no summary at all

  A caller cannot tell the second from the first. It is not hypothetical: it cost a
  whole mutation round, because the control and the mutated arm both reported rc=0 with
  zero FAIL lines -- which reads exactly like "the mutation changed nothing", the
  conclusion the run existed to test. The log being 4 lines instead of 1246 was the only
  thing that gave it away. The default argument is `/usr/local/pg17/bin/pg_config`, which
  the audit container does not have, so the wrong invocation is the easy one to make.

  TWO CAUSES, and the second is the one that generalises. `_bindir` was assigned from a
  command that had failed, so every later PATH was wrong; and part 010, which is
  SOURCED, then took its own skip path and called `exit 0` -- which exits the DRIVER
  rather than the part.

  The driver now refuses the argument before sourcing anything, on two predicates
  because one is not enough: a `pg_config` can exist and be executable and still answer
  nothing, which is the shape that produced the empty `_bindir`. Both of part 010's skip
  paths now exit 66, the status `lib.sh` calls `PGC_EXIT_SKIPPED`, paired with the
  `SKIPPED (ran no checks)` line the runners already require beside it.

  `test/selftest/430-the-self-test-must-not-report.sh` holds the arms, including a
  heredoc-aware sweep requiring that **no** part exits 0 -- closing the class rather
  than the two instances. The sweep has to be heredoc-aware because the parts generate
  fixture scripts that legitimately end in `exit 0`: it sees 2 sites before this change
  and 0 after, where a flat `grep -c 'exit 0'` sees 13 before and 24 after -- the flat
  count moves because the new part's own fixtures add ten, for a reason that has nothing
  to do with the defect. It sees `exit 0` and a bare `exit`; it cannot decide `exit $?`
  or `exit "$rc"`, and each of those is zero in the tree today.

  After: `rc=2` and a named refusal for a missing `pg_config`, for a path that is not a
  directory, and for one that answers nothing; `604 passed + 0 failed + 0 unrunnable`
  with a real one.

  The part's own comment at line 3 already said why this mattered -- "a quiet skip means
  the guard stops being tested that run without anyone noticing" -- four lines above the
  first `exit 0`.

- The pytest corpus reaches into the shell harness in three files instead of four, and
  the inventory that records it is now a mechanism rather than prose (#432).

  CONTEXT.md's rule: the two harnesses are parallel in functionality and independent in
  implementation. A pytest test that drives `test/lib.sh` is the first measurement
  wearing a Python wrapper -- it agrees with the shell by construction and can never
  report it wrong -- so the coupling turns a twin into a mirror.

  `test_build_refusal.py` was the largest item on that inventory at 36 cross-harness
  calls. **22 of them are gone.** Their subject was `test/pgc_fingerprint.py`, which has
  been the one implementation since #907, and `pgc_source_fingerprint` and
  `pgc_source_manifest` in `lib.sh` are thin wrappers that shell out to exactly that
  module -- so the path was python -> bash -> lib.sh -> python3 -> the module, and taking
  the middle two out changed no subject. Measured before converting a single arm: the
  fingerprint is byte-identical through both paths, the manifest identical line for line,
  and both agree across `LC_ALL=C`, `C.UTF-8` and `en_US.UTF-8`.

  Two of those arms need a separate process rather than an in-process call -- one reads
  as an unprivileged user, because root ignores `chmod 000`, and one varies the locale --
  and both now use the module's own CLI, which is the entry point `lib.sh` itself uses
  with `lib.sh` taken out of the path.

  13 calls remain and only 7 are debt: they drive `pgc_write_source_stamp`,
  `pgc_source_stamp_path`, `pgc_freshness_report` and `pgc_freshness_verdict`, which are
  PURE SHELL rather than wrappers over shared code, so those properties belong to the
  shell harness. Two are the one permitted cross-reference, named as the rule asks, and
  two are a historical-parity arm whose fixture is its own.

  **`SHELL_REFERENCES` in `test_harness_deps.py` makes the inventory falsifiable.** It
  declares which files reach across and by what mechanism, and asserts set equality in
  both directions: a new file that reaches in reddens, and a file that stops reaching and
  is left in the list reddens too -- which is what stops a record of debt becoming a
  permanent exemption.

  A file-level guard, which is what the rule asks for and the most it can honestly be:
  within a flagged file it cannot tell a path joined onto the real tree from the same
  name joined onto a `tmp_path`, because both are the string `lib.sh` and only the
  dataflow says which.

  **The other direction: `test/selftest/370` is deleted.** It pinned the POSITION of
  `plan_marker`'s empty-plan refusal by reading the Python source as text, which is all a
  shell part can see. It cannot run the function, so it cannot tell a refusal that still
  fires from one stranded behind an early return -- the only thing the position is for.
  The property moved into `test_guards_pinned.py` as
  `test_the_empty_plan_refusal_precedes_the_arms_it_protects`, which takes the class off
  the `expect` fixture so the file still imports nothing. 370's other three properties
  were already covered; confirmed by running those arms, not by reading them.

  That arm discriminates -- move the refusal to the end of `plan_marker` and it fails --
  and it is NOT load-bearing today: with the refusal at the end,
  `plan_marker([], absent=True)` still refuses, because `plan_marker` has no early
  return. So 370's stated reason, that the absent arm returns a pass first, describes a
  shape the function does not have. It is labelled prospective insurance against a
  refactor that adds one, rather than sold as a live hole.

  **CONTEXT.md said seven shell files reach across and the number is three.** `lib.sh`,
  `selftest/030` and `selftest/040` matched only `pgc_cluster_datadir` and
  `pgc_cluster_is_ours`, which are shell functions defined in `lib.sh` -- rule 3 of that
  entry, "a word that merely looks like a filename", caught for the second time in the
  entry that states it. `selftest/350`, `360` and `380` are the real three.

  **`test/selftest/360` is rewritten rather than deleted, and it now checks more.**
  Eleven of its seventeen arms were text pins on `pgc_vacuity.py`: that the
  unrunnable field is written, that something reads it, that the read reaches
  `session.exitstatus`, that the override is conditional. Those are gone. What is
  left is the kind CONTEXT.md permits -- a property that IS the relationship between
  the two harnesses, so it cannot be stated from one side -- and there turned out to
  be THREE such properties where the part checked one.

  The part checked the INCOMPLETE exit code and left two duplications beside it
  unchecked: the closed list of unrunnable reasons, and the one-line shape an
  unrunnable check prints. Both are now parsed out of both files and compared, with
  a drifted fixture for each so the comparison can fail. The reason list is the
  worse omission of the two, because being closed on both sides is its whole purpose.

  Both deletions were measured first. With `session.exitstatus = EXIT_INCOMPLETE`
  made unreachable in `pgc_vacuity.py` -- the defect exactly as it shipped -- the
  four behavioural arms in `test_layer.py` go from `4 passed` to `2 failed, 2
  passed`. Two is correct rather than partial: the other two assert exit 1 for a run
  with a real failure and exit 0 for a run with nothing unrunnable, and neither
  outcome moves. The file was restored and compared byte-for-byte afterwards.

  **And the reason the part gave for keeping those pins was true when written and is
  not now.** It said the behavioural arms "need pytest, psycopg and a virtualenv; CI
  installs none of them". CI has a `pytest-guards` job that installs pytest pinned
  from `requirements-test.txt`, asserts psycopg is absent, and runs the
  database-free file list -- which contains `test_layer.py`. A stale justification
  for keeping coverage in the wrong place is harder to find than a missing check,
  because nothing reddens.

  Two instrument defects of my own, both caught by the new arms' own premises. The
  shape parse took the FIRST line matching the print marker, which in
  `pgc_vacuity.py` is the DOCSTRING that spells the shape out for a reader;
  requiring a quote before the marker selects the code in both languages. And the
  drifted-shape fixture wrote one space where the real shape has two, so the parse
  found nothing and the comparison was empty-against-real -- which "differs", for
  the wrong reason. Its premise arm said so.

  **`test/selftest/380` keeps finding 1 and hands finding 2 to the pytest corpus.**
  The part covers two of @linuxhikerpm's #897 findings, and they have different
  subjects. Finding 1 is `test/pgc_fingerprint.py`, which `lib.sh` runs with the
  system interpreter and which is not part of the pytest harness, so checking it is
  this part's own business and it stays. Finding 2 is `test/pytest/pgc_cluster.py`,
  and the nine arms that read it as text are gone.

  Seven of its twenty-one checks had a cross-harness subject, not twenty-one: the
  other fourteen read `pgc_fingerprint.py`, `lib.sh`, or fixtures the part writes
  itself. Counting the whole file would have deleted coverage that was never debt.

  `make_cluster`'s cleanup SHAPE moved to `test_build_refusal.py` as
  `test_the_cleanup_guard_has_the_shape_the_leak_needs`, beside the behavioural arm
  that provokes a real failed setup. It has to be a source check, and it says so:
  `make_cluster` fails exactly one way in the behavioural arm -- a missing
  `pg_config` -- while three more properties decide whether the guard works, and two
  of those cannot be provoked at all. You cannot deliver SIGINT into `initdb`
  reliably, and a cluster that started is one the arm would then have to stop.

  Five mutations say the moved arms discriminate, each asserted to have applied,
  each leaving the module well-formed, restored byte-for-byte afterwards:
  `BaseException` narrowed to `Exception`, `cluster.stop()` removed, the bare
  re-raise turned into `pass`, a private `hashlib.md5` added, and
  `shutil.rmtree(root, …)` removed. The two whose effect reaches the filesystem
  redden the behavioural arm as well; the three that redden only the source arm are
  exactly the properties the behavioural arm cannot see.

  One of those five did not apply on the first attempt -- the `cluster.stop()`
  pattern assumed twelve spaces of indentation and the call sits at sixteen, inside
  a nested `try`. The harness refused to report a result for it rather than printing
  a green, which is the only safe behaviour for a mutation that did not land.

  **And one thing went wrong that is worth more than the change itself.** I ran the
  selftest before naming the two new pytest arms in `TESTS.md`, so the run was red on
  the doc-coverage check -- and then merged that log into `check_ledger.tsv`, whose
  entire subject is which checks have ever been red. It recorded a red for
  `350`'s "every test file and every test in the corpus is named in TESTS.md",
  a check that failed only because my own change was half-finished. Reverted with
  `git checkout HEAD --`; there is no second copy to repair from.

  The merge step now refuses a log that is not definitely green: a numeric
  `checks run:` line, a floor on it so an aborted run cannot pass, zero `FAIL`
  lines, and rc=0. The absence of a `FAIL` line is not enough, because an aborted
  run has none either -- the same shape as a pending-count that cannot see a job

  **`test/selftest/350` goes from 50 checks to 5, and two of its rules moved rather
  than being deleted.** Forty-six of the fifty had a subject on the other side of the
  boundary: they globbed `test/pytest/*.py` and parsed Python out of it, or swept the
  markdown in that directory. The pytest corpus asserts all three properties natively
  in `test_docs_cover_the_corpus.py`, where the subject is, so the shell copy could
  only ever agree with it.

  Two of the three rules were implemented on BOTH sides and self-tested only on the
  shell side -- the side that cannot run the corpus it counts. Deleting those
  fixtures would have left the Python implementation with no fixtures at all, so they
  went with them:

  - the mode-counting rule's edges: an id of fewer than three words, an id named
    twice, stopping at the next heading, section 3's back-references, and the row
    reader taking the value cell rather than a digit inside its label
  - the contents-list anchor rule: GitHub's derivation, the broken link that shipped,
    and a control beside it

  `_named_modes_in(text)` and `_stated_row(text, label)` are new seams, so a fixture
  can reach the rules at all. The row reader's fixture makes the label digit and the
  value DIFFER -- label "section 2", value 9 -- because `350`'s own fixture had both
  as 2 and could not see the bug it was written for.

  **What stays in 350 is its arms over `ci.yml`**, whose subject is neither harness.
  A shell part may read the workflow for the same reason it may read the Makefile, and
  no pytest arm can assert that the gate runs pytest without assuming the thing in
  question. One arm is added while the part's subject is being settled: the job must
  ASSERT `psycopg` is absent rather than assume it.

  ### The stale justification, in triplicate

  TESTS.md section 6 said "Nothing runs pytest. Not `run_all_versions.sh`, not any
  workflow under `.github/`", and concluded that the `.sh` half was therefore the
  enforcement. CI has a `pytest-guards` job: it installs pytest pinned from
  `requirements-test.txt`, asserts `psycopg` is absent, derives the file list from
  `NO_CLUSTER`, and runs it.

  That claim was load-bearing in THREE places at once -- TESTS.md section 6,
  `selftest/360` and `selftest/380` -- each asserting the behavioural half could not
  run in CI. Nothing was wrong in any of them; the reason was, three times, and **no
  arm reddens on a stale justification.** All three are corrected.

  ### Where the two harnesses now stand

  Shell parts holding a reference to anything under `test/pytest/`: **one**, and it is
  deliberate. `selftest/360` reads `pgc_vacuity.py` to compare the three values the
  two harnesses both write down -- the INCOMPLETE exit code, the closed list of
  unrunnable reasons, and the line an unrunnable check prints. That is CONTEXT.md's
  permitted cross-reference: a property that IS the relationship, so it cannot be
  stated from one side. The inventory said seven; the measured answer was three plus a
  deleted fourth; the end state is one.
  which never started.

- One matrix report no longer gives two answers to "how many suites accounted for their
  checks" (#928).

  A full PG 17 report printed both, three lines apart:

      population reconciliation: registered=251 | accounted=237, ... | sum=251
      of those, 235 accounted for their checks and 7 did not

  237 and 235, both describing suites that accounted for their checks, differing by
  exactly 2. The population line counted with the WIDE reader,
  `pgc_log_shows_any_accounting`; the breakdown line derived from the NARROW one,
  `pgc_log_shows_accounting`. The figure a reader acts on is the second, because it is the
  one phrased as a problem, and it overstated the debt by 2.

  That matters more than a mismatch: the breakdown line exists to stop an overcount, and
  deriving it from the narrower reader reintroduced a smaller version of the same
  overcount in the line added to close it.

  THE GAP IS A DIFFERENT MECHANISM, NOT A DEBT. The wide reader also accepts a suite that
  prints its own `checks run:` line. Measured on this tree: of the twelve registered
  suites that never call `pgc_summary`, exactly two -- `bench_guards` and `docs_style` --
  emit a tally of their own, and the other ten keep none. Those two are the 2.

  The headline now comes from the same file the population line counts, and the two
  mechanisms are broken out beneath it with the own-mechanism suites NAMED rather than
  counted, so a third adopting its own tally appears without anyone editing a number.

  The comment above the line said "Ten registered suites exit 0 having never called
  pgc_summary", which conflated the two populations: twelve never call it, and ten of
  those keep no tally. Both halves were true of something; neither was true of what it
  said.
- A test helper no longer takes a tree it ignores (#933).

  `_sh(srcdir, expr)` in `test/pytest/test_build_refusal.py` read as "evaluate one
  `lib.sh` expression against a tree": the docstring said so, nine call sites passed a
  fixture tree, and the body sourced the module-global `SRCDIR` -- the real source tree --
  instead. Whatever those arms measured, it was not parameterised by the tree they were
  handed.

  WHICH READING WAS INTENDED IS A MEASUREMENT. Every caller passes a tree built by
  `_tree_with_module` or `_tree_with_source`, and none contains `test/lib.sh`:

      fixture tree holds: ['Makefile', 'objstore', 'pgcolumnar.control']
      honouring srcdir:   rc=1, "No such file or directory" -- the source fails
      sourcing SRCDIR:    rc=0, the function under test runs

  So the parameter could never have worked -- honouring it would have made every one of
  those arms measure a failed `source` rather than the function under test. The arms mean
  the real tree, so the parameter was noise that made nine call sites read as something
  they were not. It is gone, and behaviour is unchanged: the expressions that need the
  fixture interpolate it themselves.

  A corpus-wide AST scan now requires that no helper takes a parameter it never reads, so
  the class is closed rather than the instance. Two exclusions, both real: a TEST
  function's parameters are pytest fixtures, and requesting one has an effect whether or
  not the body reads it; and a HOOK's signature is pytest's API, where arguments arrive by
  name, so declaring one you do not read is how a hook says which it wants. Three of the
  four the scan found were hooks -- `pytest_collection_modifyitems(config)`,
  `pytest_xdist_node_collection_finished(node)`, `pytest_sessionfinish(exitstatus)` -- so
  only `_sh` was a defect and the budget was 1, now 0.

- The vacuity guard's PLACEMENT is now a checked property, because a guard in a
  teardown cannot fail the test it guards (#432).

  Measured, the same `AssertionError` raised from two places: from a
  `pytest_runtest_call` wrapper the run reports `1 failed`; from a fixture teardown it
  reports `1 passed, 1 error`. pytest has already recorded the call phase as passed, so
  a teardown refusal arrives as a separate error on the same node-id and the test's own
  outcome stays `passed`. Anything counting passes -- `--pgc-expect-tests`, a CI
  summary, a human reading "N passed" -- sees a pass.

  This layer's guard was already in the call-phase wrapper, so nothing was broken. What
  was missing is that nothing said so: moving it into the `expect` fixture's teardown
  was a plausible-looking refactor that would have turned every vacuous test from
  `failed` into `passed` with an error beside it. Two arms in `test_runshape.py` pin the
  placement, and the second is the control -- without it the first passes whatever phase
  the guard is in, because "a vacuous test fails" is equally true of a correct guard and
  of no guard at all next to an unrelated failure. Proved by moving the guard into the
  teardown: the first arm reddens.

  `guard-as-teardown-fixture-still-reports-passed` is NARROWED rather than closed, and
  `VACUITY_MODES.md` 3.7 says which half. The half closed is this layer's own guard
  placement. The half still open is the general shape: a guard anyone adds later in a
  teardown still cannot fail its test, and nothing refuses that.

- A failed query is no longer comparable with another failed query (#432).

  `error-swallowed-to-empty`: two queries raise, a helper turns each into the same
  value, and they compare equal. The test is green and has asserted nothing about
  either query. `test/lib.sh` closed this by PRODUCING the sentinel with a sequence
  number per failure -- `res="QUERY_ERROR.$seq"` -- so two failures can never compare
  equal.

  The pytest port had the constant `QUERY_ERROR = "QUERY_ERROR"`, a comment claiming it
  was "unique per occurrence" -- which is false of a constant -- and a refusal in
  exactly one assertion. Measured before the fix, with a sentinel on both sides:
  `expect.hash` refused; `expect.text`, `expect.rows`, `expect.row_set` and
  `expect.ordered_rows` all PASSED. Four of the five comparisons accepted two failed
  queries as agreement. `expect.num`, `expect.at_least` and `expect.rowcount` refused
  already, by their type guards rather than by anything about sentinels.

  There is now one refusal, called by every comparison, so an assertion added later
  inherits it instead of being the next hole -- which is how this survived: `hash` had
  a refusal and the four written after it did not. It matches the prefix at any depth,
  because a sentinel arrives as a CELL inside a row as often as it arrives as a whole
  side. `row_set` refuses BEFORE it maps its rows through `repr`, since
  `repr(("QUERY_ERROR.1",))` does not start with the prefix: delegating an assertion
  does not delegate its refusals when the delegation transforms the data.

  `query_error()` produces a value unique per occurrence, for the paths that compare
  without the layer -- a helper comparing by hand, which is what the two existing
  hand-rolled sentinels in `test_hilbert_locality.py` do. The refusal is the mechanism;
  the producer is the second line. One of those two sentinels is produced by
  `coalesce(...)` inside SQL and cannot use the Python producer, which is why the
  refusal has to be the mechanism rather than the other way round.

  Each refusal is proved load-bearing: removing it from one assertion at a time makes
  the arm name that assertion and no other, five times out of five. The mutations are
  only distinguishable with `__pycache__` cleared between runs -- the five deleted
  lines are byte-identical, so three of the five leave the file the same size and
  Python reuses the stale bytecode, which made two of the refusals look like they did
  not bite.

  THREE DEFECTS @jdatcmd FOUND IN THE FIRST VERSION, each reproduced before it was fixed.

  The HATCH WAS OPEN and the arm that said otherwise was tautological: it rewrote
  `pgc_vacuity.QUERY_ERROR` and THEN minted its sentinels with `query_error()`, which
  read that same global -- producer and matcher moved together, so the refusal matched
  whatever the prefix had just been set to. The faithful hatch mints while armed and
  rewrites afterwards, which is what a corpus file does, and against the first version
  that COMPARED two sentinels instead of refusing them. End to end it reported
  `1 passed` over two failed queries. The prefix is now bound in a DEFAULT ARGUMENT,
  evaluated once when the function is defined and never read from the module namespace
  again, so rewriting the global changes neither what is minted nor what is refused.
  `ZZZ_NOT_A_PREFIX` is in the arm's spellings deliberately: `'Q'` cannot disarm a
  prefix-reading matcher, because `'QUERY_ERROR.1'.startswith('Q')` is true.

  `ordering_observable` WAS MADE WEAKER BY THE PRODUCER, and this is the one place the
  change regressed the layer. It takes `(forward, reverse)` rather than `(got, want)`,
  so it sat outside the refusal and outside the derivation that finds comparisons. With
  the old shared constant two failed readings were IDENTICAL and it went red -- loudly.
  With unique sentinels they differ, so it passed and greenlit every ordered assertion
  resting on the premise. The refusal is now the first thing it does, the derivation
  recognises `(forward, reverse)`, and an arm pins both.

  And `TESTS.md` stated the split twice in one sentence while only the first half was
  gated: 26 refused and "the other 47" sums to 73 against the 72 the inventory names.
  `selftest/350`'s regex matches the half a change naturally updates. Both halves are
  now read by an arm in the corpus's own docs guard, against the inventory's count of
  the ids it names rather than a number typed twice.

  `VACUITY_MODES.md` said "the port has the sentinel constant but nothing produces it",
  and that was wrong in both halves: two sites did produce sentinels by hand, and the
  thing actually missing was the refusal in four of the five comparisons. The
  correction is recorded in the document rather than quietly replacing the sentence,
  because a map that names the wrong gap is worse than one that admits it does not
  know.

- The sweep that forbids piping a captured string into `grep -q` now joins a
  pipeline split across two lines, and the six suites that had split one are
  fixed (#486).

  The sweep read one physical line at a time. `producer |` on one line with
  `grep -q PATTERN` on the next was therefore invisible to it: the producer's line
  holds no reader, and the reader's line holds no producer. Six live sites were
  written that way -- three in `test/vector_agg_rescan_memory.sh`, one each in
  `test/unique_conc.sh`, `test/native_groupagg_batch.sh` and
  `bench/run_clickbench.sh`. Every one of them answers a premise that decides
  whether a whole arm measures what it claims to, and the failure direction is the
  expensive one: the pipeline reports the thing it was looking for as ABSENT, so a
  plan that contains the vectorized aggregate reads as a planner regression.

  `test/unique_conc.sh` is the one to read. The comment directly above it explains
  this exact trap, and captures the output into a variable for that reason. The
  next line pipes that variable into `grep -q` anyway.

  The sweep now builds logical lines before it matches, and applies one pattern to
  both the physical and the joined stream. Three behaviours of bash were measured
  rather than assumed: a pending `|` skips blank and comment lines, a `\` joins the
  next physical line with no skipping, and a comment never continues at all. Two
  premises the joiner rests on are asserted instead of coded around -- no line
  opens a heredoc and also continues, and no `\` continuation is followed by a
  blank or a comment -- so if either stops being true the gate says so rather than
  reading past it.

  One false positive is accepted, and a fixture pins it: a double-quoted string
  continued across a line break, whose first line ends in a bare `|`, reads as a
  pipeline once the two lines are joined. It fails loud, where the blindness it
  replaces failed silent. A second latent defect went with it -- the physical
  stream now passes `-H`, because `grep -n` omits the filename when it reads a
  single file and the heredoc exemption keys on `file:line`.

  A COMMENT NAMING A HEREDOC USED TO EXEMPT THE REST OF THE FILE. The exemption
  scanner matched the opener anywhere on a line and left heredoc mode only on a line
  equal to the tag, so a COMMENT that merely named the idiom switched the rule off for
  everything after it. @linuxhikerpm measured it: with a genuine two-line violation
  restored this part went red, and adding one comment line 24 lines above it -- changing
  nothing else -- took it back to 37 passed while the violation was still there byte for
  byte. This change is where that becomes load-bearing, because it deletes the filename
  exclusion and rests the argument on the exemption being DERIVED rather than listed.

  Two conditions now, each measured. An opener is recognised only on a NON-COMMENT line,
  and only when a later line EQUALS its tag -- one with no terminator exempts nothing.
  The second condition is what stops a TRAILING comment doing the same thing, and it
  retires the old per-file reset: an unterminated candidate can no longer leak into the
  next file. Proved by restoring the violation, adding the comment, and staying red; and
  by writing `cat <<'X' |` plainly in this file's own prose, which used to take its
  exempt-line count from 6 to 187 and now changes nothing.

  AND BOTH NEW PREMISE ARMS WERE NUMERATOR-ONLY. They reported zero whether or not their
  detector worked: replacing the heredoc-opener pattern with one that cannot match left
  the arm green, and so did making the continuation detector never arm. The denominators
  are printed and asserted now -- 179 openers and 5,533 continuations -- which is the
  inputs == sum(buckets) rule the rest of this directory applies. An earlier draft of
  this entry claimed "if either stops being true the gate says so"; that was the half
  which was not true.

  Planting any one of the six sites back in its old form takes the rule red and
  names the file and the line.

- TESTS.md's contents list no longer carries a link that goes nowhere, and the
  corpus gate now checks every one of them.

  The entry added for `test_harness_deps.py` stripped the underscores out of the
  file name -- `#14-testharnessdepspy-...` against a heading GitHub renders as
  `#14-test_harness_depspy-...` -- so the link silently resolved to nothing. The
  eleven entries above it keep the underscores, so the document already stated the
  convention. Neither existing arm could see it: both sweep for NAMES, and a broken
  anchor is still a string containing the name it points at. Selftest 350 now
  derives each heading's anchor by GitHub's rule and requires every in-document
  link to reach one. Measured over the three documents in that directory it reports
  nothing, and over the document as it shipped it reported exactly the one entry.

- No sentence in the pytest harness states how many tests the corpus holds (#908).

  Eight places said "61 of 142" or "152 tests": `test_harness_deps.py` twice,
  `conftest.py`, `TESTS.md` three times, the `pytest-guards` job's comment in
  `ci.yml`, and selftest 350's own comment. The corpus held 154 on the day they
  were written, so the job's comment was already wrong, and a concurrent branch
  adds a thirteenth test file, which would have made every one of them wrong
  again. These are the hand-maintained derived values #908 spent a day removing
  from TESTS.md's totals line, reintroduced as prose, where that line's guard
  cannot see them: it matches only the bold fixed-form line. The numbers are gone.
  The job prints how many files it ran and pytest prints how many tests passed, the
  membership arm prints the partition, and an arm over the job and its comment
  block refuses a written count there.

- `ALTER TABLE ... RENAME COLUMN` now carries the new name into
  `pgcolumnar.projection_declaration`, for the named relation and for every
  inheritance descendant, including a `PARTITION OF` child (#888).

  The materialized projection stores attnums, so it already followed a rename
  without any catalog change. The declaration deliberately stores NAMES, because
  a restore assigns new attnums -- so leaving the old name behind broke
  `pgcolumnar.rebuild_projections()` after a dump and restore, even though the
  live projection had kept working right up to the backup. The failure was
  therefore invisible until the moment it mattered.

  The descendant half is held by an arm that was proved able to fail: changing
  the walk to use the named relation instead of each descendant takes
  `test/projection_rename_restore.sh` from 8 passed to 6 passed and 2 failed.

- An in-place `TRUNCATE` now clears each projection's storage as well as the base
  (#896).

  PostgreSQL truncates a relation created in the current transaction in place,
  because a rollback discards the relation anyway. Measured on 18.4: the
  relfilenode and the storage id are both unchanged across such a `TRUNCATE`,
  where a table from an earlier transaction gets fresh ones. So
  `relation_set_new_filelocator` never runs, and the teardown that retires a
  projection's storage never happens.

  `pgcolumnar_relation_nontransactional_truncate` cleared only the base storage.
  The projection's row groups survived, and the next write to the projection
  collided with them:

      ERROR:  duplicate key value violates unique constraint "row_group_pkey"
      DETAIL:  Key (storage_id, group_number)=(10000000001, 2) already exists.

  The whole transaction rolled back, so `TRUNCATE` and reload in one transaction
  was unavailable on any table carrying a projection. It needed four things
  together: the table created in the same transaction, a projection, the
  `TRUNCATE`, and a write after it. Each was isolated by a control.

  **The `pgcolumnar.projection` rows are kept, deliberately.** This is not
  `pgcolumnar_delete_storage_tree`, which also deletes them. That is right for a
  rewrite, where the base storage id changes and the rows are re-recorded under
  the new one. Here the metapage keeps its storage id, so the rows still describe
  this relation correctly; only their content is being truncated away.

- `ALTER TABLE ... DROP COLUMN` is now refused when a projection depends on the
  column, instead of leaving the table unreadable (#891).

  Dropping a column any projection stores left the table broken, in one of two
  ways. Dropping the **sort-key** column produced
  `ERROR: type with OID 0 does not exist` on the next `INSERT`. Dropping any
  other stored column let writes continue while `pgcolumnar.read_projection`
  raised `cache lookup failed for type 0` and `pgcolumnar.rebuild_projections()`
  reported repairing nothing -- the quieter and worse half, because it tells an
  operator there was nothing to do.

  The refusal raises `2BP01` (`dependent_objects_still_exist`) and names the
  projection, so the remedy is to drop the projection first. One loop covers
  sort keys too, because `add_projection()` requires every sort-key column to
  appear in the stored columns. `DROP COLUMN IF EXISTS` of a column that is not
  there is unaffected.

- A rewrite no longer loses a declared projection (#876, #887).

  `TRUNCATE`, `ALTER TABLE ... ALTER COLUMN ... TYPE` and `ALTER TABLE ... ADD
  COLUMN` with a volatile default each mint a new base storage id.
  `pgcolumnar.projection` is keyed by that id, so afterwards
  `pgcolumnar.read_projection` raised `42704` for a projection that was still
  declared over an intact table. 1.0-alpha3 shipped only a `HINT` naming
  `pgcolumnar.rebuild_projections()`; the projections are now re-recorded
  automatically and the manual call is no longer part of the routine path.

  **Five shapes lost the projection, not the two the issue named.** Swept on
  18.4 rather than reasoned about: `TRUNCATE` including its multi-table form and
  its `CASCADE` form, a type change on a covered or an uncovered column, `ADD
  COLUMN` with a volatile default, **a partitioned child rewritten by a type
  change on its parent** -- where the statement names the parent, which is not
  itself a columnar relation -- and `REFRESH MATERIALIZED VIEW`, which is neither
  an `AlterTableStmt` nor a `TruncateStmt` and so escaped a gate naming only those
  two. `REFRESH ... CONCURRENTLY` is not a rewrite: the storage id is unchanged
  across it, measured.
  `ADD COLUMN` with a constant default, `DROP COLUMN`, `VACUUM`, `SET ACCESS
  METHOD` to the same method, `SET TABLESPACE` and a no-op type change do not
  rewrite and were never affected. Core `VACUUM FULL` and `CLUSTER` are refused
  on a columnar table, which bounds the class.

  **The repair runs after the statement, in `ProcessUtility`, not in the table-AM
  callback.** `pgcolumnar_relation_set_new_filelocator` cannot do this job, which
  is measurable rather than arguable: with the callback logging its own relid,
  `TRUNCATE` reaches it as the user's relation with the old fork attached and both
  projection rows in scope, but a rewriting `ALTER TABLE` reaches it as the
  transient relation `make_new_heap` builds -- `pg_temp_<oid>`, no columnar fork --
  so the rewrite branch is not taken and neither the old storage id nor the
  projection list is ever in scope. A re-record placed there also records under the
  id the rewrite just retired, because `PgColumnarStorageId(rel)` still returns the
  old id after the new metapage is written.

  **The projections are re-derived from the declaration, not copied forward.**
  `pgcolumnar.projection_declaration` records column NAMES and survives a rewrite,
  and resolving those names against the relation as it is now is what makes `ADD
  COLUMN` correct: the base projection records every live column, so a copy of the
  old row would leave it naming a stale column set. `materialize_projection` is
  extracted from `pgcolumnar.add_projection` so both paths drive one
  implementation.

  **A repair that cannot run degrades to a WARNING and never fails the statement
  that triggered it.** A declaration can name a column the table no longer has;
  `ALTER TABLE ... RENAME COLUMN` used to leave one behind and no longer does, so
  the reachable case is a database created before that fix. Before this was
  handled, the repair raised `column "a" does not exist` inside an unrelated
  `ALTER TABLE ... ALTER COLUMN id TYPE bigint` and rolled that type change back --
  turning a silently lost projection into a blocked schema change. It now reports

      WARNING:  42703: could not restore projection "p" on "public.t" after rewrite
      DETAIL:   Its declaration names a column the table no longer has.
      HINT:     Call pgcolumnar.add_projection('public.t', 'p', ...) again,
                naming columns the table has. That replaces the declaration.

  **The HINT names `add_projection`, not `rebuild_projections`, because the other
  two candidates were measured to fail.** `rebuild_projections()` re-runs the same
  stale declaration and raises the same missing-column error;
  `drop_projection()` refuses with `42704`, because the projection row is exactly
  what is absent. `add_projection()` with the same name replaces the declaration
  and materialises it. The relation name in both messages is schema-qualified:
  unqualified, a HINT for a table outside the reader's `search_path` told them to
  run a statement that fails with `relation "t" does not exist`.

  **Every other failure is contained too.** The resolves-check covers one cause,
  and `materialize_projection` can raise for others, so it runs in an internal
  subtransaction. A failure is rolled back and reported as a WARNING carrying the
  original SQLSTATE, and the user's statement continues.

  **The repair decides under `AccessShareLock` and escalates only when there is
  work.** It is reached for every `AlterTableStmt` on a relation with a declared
  projection, not only for one that rewrote it, so an unconditional `ShareLock`
  blocked concurrent writers on statements that rewrite nothing: `ALTER COLUMN SET
  STATISTICS` and `SET (autovacuum_enabled)` take only
  `ShareUpdateExclusiveLock`, and each opened the relation with `ShareLock` with
  nothing to repair.

  **The rewritten relations are recorded, not re-derived from the statement.** A
  `TRUNCATE ... CASCADE` rewrites tables it never names, so the table-AM callback
  records each relation whose storage it retires and `ProcessUtility` drains that
  list. The list survives a nested utility statement -- a cascade whose trigger
  runs one -- because it is cleared only for the outermost statement, and the
  drain removes only the relids it repaired rather than emptying it.

- The `42704` hint no longer names a rewrite as the likely cause, since a rewrite
  now re-records. It names the two cases that remain: a declaration that no longer
  resolves, and the implicit base projection, which is not readable by name at all.

- A `conftest.py` can no longer switch off the order-collapse guard by rebinding
  the module-level name it used to read (#924).

  The scan looked up `_ORDER_KILLERS` on each call. A conftest is imported before
  collection, so `pgc_vacuity._ORDER_KILLERS = ()` turned the refusal off for every
  test in that directory, with no reason recorded. Two lines, less to type than
  the honest form. Measured: the same collapse test was uncollectable with no extra
  file, and reported `1 passed` with only that rebind.

  The killer names are bound at definition time, in a default argument, the same
  way `query_error` already binds its prefix. There is no module-level name left
  to rebind. `_RECORDERS` is a registry the layer writes, not rule data, and is
  unchanged.
- The pytest harness no longer reports results against a library another process
  installed (#956).

  `build_once()` skipped the build when its marker matched, and the marker recorded
  the pg_config, the major and the source fingerprint. That answers "did this layer
  last build this source", and it was read as "does the prefix hold that build". The
  two differ whenever anything else writes the shared prefix: the bash harness, a
  timing run, a manual install, another worktree. Measured twice in one day, a
  measurement run installed an older library and the corpus then reported ten
  failures in one file on one machine and nineteen on another, with the code under
  test entirely innocent.

  The installed library is now part of the marker, so a prefix someone else wrote is
  rebuilt rather than certified. A library that is absent counts as changed. Where
  the prefix cannot be observed at all, no marker is written, so the next call builds
  rather than matching another unobservable run. The arms that exercise the marker
  supply a `pg_config` that answers, so whether the skip happens no longer depends on
  whether the machine running the tests has one.

  The digest cannot be predicted from the source, because the build path is compiled
  in: one commit built in two directories produces two different libraries. So what
  is recorded is the digest installed at the moment the marker was written, which is
  a statement about that prefix over time.

  Blast radius worth knowing, since it is what made this hard to spot: a stale
  library fails exactly the tests of the feature it lacks, so it presents as one
  whole file failing while the rest of the suite passes. Scattered failures are
  usually the code; a clean file boundary is usually the environment.
- `pgc_ledger.py gate` no longer certifies a census that contradicts its own
  ledger (#952).

  The gate printed `ledger census: rows=N` and never compared that number to the
  `checks_never_observed_red` the budget states, so it returned 0 on a twenty-row
  ledger claiming five. Reporting is not enforcing. The comparison existed one layer
  out, in a selftest arm, which runs on a pull request and therefore reports the
  disagreement after the merge that creates it rather than before.

  It creates it because the census is a measurement of the tree, so every merge
  invalidates it: two pull requests each re-derive it from the same base, the merged
  ledger takes both sets of rows, and the budget keeps whichever side won the
  conflict. Three in flight at once set 769, 762 and 800 against a base of 756, and
  no two composed. The new refusal is decidable from the two inputs alone, needing no
  prior and no `--against`, which is what lets it speak about a merge commit.

  It refuses a contradiction in either direction and does not make the census a
  ceiling. A ceiling refuses a rise, and bounding this number deadlocks: every added
  check enters as `never`, so landing one would mean raising a number the design says
  may only fall. A budget that states no census at all is reported rather than
  refused, since absence is not a contradiction; what holds the committed budget to
  naming both numbers is a separate arm in each harness.

- The harness no longer reports that the installed library matches when it has not
  looked at the library (#959).

  `lib.sh` compared a recorded source fingerprint against the current one and then
  printed "source <hash> matches the binary under test". That is a claim about the
  binary drawn from evidence about the source, and it is false whenever another
  process has written the shared prefix: a second worktree, a timing run, a manual
  install. The stamp could not see it, because the stamp is keyed per source tree,
  so two trees installing into one prefix keep two stamps and each records only what
  its own tree built.

  Measured on two trees whose sources differ by five files. One built and installed
  through the harness, the other installed its own library into the same prefix, and
  the first then ran a suite with PGC_SKIP_BUILD=1: the run printed the library's
  fingerprint, asserted that the source matched the binary, and failed nine checks
  of a feature the installed library did not contain.

  The stamp now records the installed library's digest beside the source
  fingerprint, and the claim requires both to match what is on disk. A library that
  changed under the stamp is refused the way a changed source already was, naming
  both digests and the prefix another build wrote. A stamp written before this change
  records no digest, so it reports the source claim it earned and says the library is
  unverified rather than implying it was checked.

  The digest cannot be predicted from the source, because the build path is compiled
  in: one commit built in two directories produces two different libraries. So what
  is recorded is the digest installed at the moment the stamp was written.

  All three places that write a stamp record it: the build function, the matrix
  runner and the development loop. Without that the matrix, which builds once per
  major and then sets PGC_SKIP_BUILD, would have reported every suite as unverified.

  The reader for the source field now reads its first line only. Stripping hex from
  the whole file was right while a stamp was one line, and wrong as soon as there were
  two: a source that cannot be fingerprinted writes an empty first line, and the
  whole-file read returned the library digest as the source, turning a documented
  unverified into a refusal that named a library digest as a source fingerprint. That
  was introduced by the second line rather than found lying in wait, so it is fixed
  here. Reported by Joshua D. Drake, who swept every hex extraction in the harness to
  establish it was the only one.

  The refusal on a changed source had never been exercised by anyone before this
  change, only read. It is now driven end to end, along with the three other states.

- The ten suites that recorded nothing now record their checks (#965).

  Ten registered suites print their own `PASS <name>: <value>` lines and their own
  verdict and emit no machine-readable records at all. Counted across the ten, 293
  checks were invisible to every mechanism built on that vocabulary: the ledger, the
  census, the count of checks never observed red, the red-observation record, and the
  duplicate-name detection. They pass, and nothing that reads records can see them.

  This converts the first of the ten. Its nine checks now emit a record each and a
  total, measured on PG18: nine records where there were none, and `checks run: 9`
  where there was no total.

  The human output is unchanged, byte for byte. `pgc_record` takes the display whole,
  so each line still reads `PASS  count(*): 100000` with the measured value rather
  than a label. That value is the comparison, not a message, which is why the local
  helper records through `pgc_record` rather than delegating to `lib.sh`'s own
  `check` -- the latter composes its own display and would drop the value.

  The suite's own verdict line stays, and so does its exit logic. That line is
  load-bearing until a reconciliation replaces it: `smoke.sh` runs under
  `set -euo pipefail`, so a failing command aborts it, and the verdict is what
  distinguishes a suite that finished from one that stopped. Removing it in the same
  change would make the suite report less than it did before.

  This converts nine more of the ten, the same way, after the shape was reviewed on
  the first. Measured on PG18: 227 records where there were none, and every suite's
  human output byte-for-byte unchanged.

  It also fixes a call that was failing silently. `unique_conc.sh` calls `check_skip`
  for the case where the citext extension is absent, and that function lives in
  `lib.sh`, which the suite did not source -- so the baseline log carries
  `line 392: check_skip: command not found`, the suite continued under
  `set -uo pipefail`, and the case was reported nowhere at all. It now emits a SKIP
  record with its reason.

  Fifty-eight checks across four of the suites are still not recorded -- fifty-eight
  on PG18 and fifty-seven on PG16, because one of them sits behind a version gate --
  since those suites have further check-like helpers of their own, `eq_on_off` in
  `phase6` alone accounting for thirty-nine, each printing its own display. Those need a
  second pass rather than the same substitution, and the count in #965 should be read
  as the number of checks rather than the number of helpers.

  It does not add the suite to the ledger. Making a suite coverable and covering it
  are separate decisions, and the second one is blocked on a measurement: check NAMES
  differ between majors in at least one suite, the ledger has no major dimension, and
  a gate seeded from one major would refuse runs on another.

- The last fifty-eight checks in those ten suites record too, so all ten are now
  complete (#965).

  The previous change converted each suite's own `check` helper and recorded 236 of
  the 293. The rest went through four further helpers with four different displays,
  which is why they needed a second pass rather than the same substitution:

  | suite | helper | checks |
  | --- | --- | --- |
  | `phase6` | `eq_on_off` | 39 |
  | `phase4` | `expect_fail` 5, `assert_plan` 2, `assert_plan_seq` 1, one written inline | 9 |
  | `audit` | `expect_error` | 5 |
  | `phase5` | `assert_plan` | 5 |

  Measured on PG18, each suite's records now equal both its own human check lines and
  its `checks run:` total, and every human line is byte-for-byte what it was:

      suite    records before -> after   checks run:   human lines
      audit                26 ->  31             31            31
      phase4               29 ->  38             38            38
      phase5               31 ->  36             36            36
      phase6                4 ->  43             43            43

  `phase6`'s `eq_on_off` has three outcomes and two of them `return` early. Each one
  records, because a `return` that skips the record leaves the check counted nowhere,
  which is the state this conversion exists to end.

  Two displays span more than one line -- `assert_plan` in both `phase4` and `phase5`
  prints the whole plan under a header when it fails. The dump is passed as part of
  the display rather than echoed after the record, so a failing run's output is also
  byte-identical instead of having a record line wedged between the header and the
  plan.

  It also makes a version-gated arm visible to the ledger. `audit.sh` gates its
  partitioned-parent arm on `server_version_num >= 170000`, because PG16 and earlier
  refuse `PARTITION BY ... USING pgcolumnar`, and the gated branch printed a bare
  note and recorded nothing. So on PG16 the ledger received three fewer rows for
  `audit` with nothing saying why. It now records a SKIP with its reason, the way
  `unique_conc.sh` already does for its own version gate, and PG16's human output
  gains that SKIP line in place of the note.

  One SKIP for the block, not one per gated check. Naming each of the four checks in
  a branch that never runs them would make the count the same on every major, and
  would also put four check names somewhere nothing exercises them, where they would
  drift. Comparing counts across majors needs a major dimension in the ledger, which
  belongs to #432.

  Every count here is a PG18 number. On PG16 the same suites give 28 records for
  `audit` rather than 31, because that gated arm holds three `check` calls and the
  `expect_error` above, and one of those records is now the SKIP standing in for all
  four. The per-major table is in #965, which is where the remainder should be read
  from rather than from either run alone.

- The unprivileged reader in the fingerprint arms needs a harness it can reach,
  and a failed source must not be reported as a refused fingerprint (#907).

  The nightly coverage job went red for two nights and nothing said so: every suite
  job was green, the PR gate was green, and the only failure was one suite inside the
  job nobody reads. `harness_selftest` reported 249 passed, 1 failed.

  Three checks in `340-the-binary-must-be-built-from` failed, with six
  `test/lib.sh: Permission denied` lines beside them. The arms there read a fixture
  tree as a second user, because `chmod 000` is invisible to root, and they sourced
  `lib.sh` from the checkout. In GitHub Actions the checkout lives under
  `/home/runner/work`, which `postgres` and `nobody` cannot traverse, so the
  unprivileged shell could not load the harness at all.

  **Two of the arms passed anyway, and that is the defect worth naming.** The reader
  did `. "$PGC_TESTDIR/lib.sh" || exit 1`, so a denied source produced empty output --
  which is exactly what an arm asserting "an unreadable file yields no fingerprint"
  wants to see. Measured under a tree the reader cannot traverse:

      everything readable, lib.sh UNREACHABLE   -> []              the arms PASS on this
      b.c unreadable,      lib.sh reachable     -> []              what they mean to test
      everything readable, lib.sh reachable     -> cde49bff94a7

  So the premises were the only thing standing between the suite and a clean report
  on two arms that proved nothing.

  Three changes, each with its own reason. A readable copy of `lib.sh`, `portlib.sh`
  and `pgc_fingerprint.py` is staged beside the fixture, which the reader can always
  reach -- the same three files, read from a different directory, which is an
  environment property rather than anything the part asserts. A failed source now
  answers `harness-unreadable`, a value that cannot be mistaken for a hash or for
  empty, so no arm can pass that way again. And the arms run only when the premises
  they rest on were met, skipping loudly otherwise, because a guard that cannot run
  is not a guard that held.

  Measured, three conditions, PG18:

      tree readable by the reader            unfixed 803 checks 0 FAIL   fixed 804 checks 0 FAIL
      tree under a mode-750 parent           unfixed 3 FAIL, 6 denied,   fixed 0 FAIL, 0 denied,
                                                     2 arms PASS vacuously      arms run
      reader cannot fingerprint at all       --                          2 FAIL, 5 honest SKIP

  The last row is a probe rather than a condition anyone meets today: `python3` was
  removed from the reader's `PATH` to prove the skip branch can be reached, since an
  `else` that cannot run proves nothing either.

- The per-file coverage table is computed from the tracefile, so it no longer ranks
  the best-covered files as the worst (#974).

  The nightly's "least covered first" table printed

      columnar_parquet_codec.c  | 2.0%    100|3200%     2|    -      0

  for a file whose records say `LF:100 LH:100 FNF:2 FNH:2 BRF:64 BRH:47`. A file at
  100% presented as 2.0% and sorted to the top of a list headed least covered, with a
  function rate above 100% on its face and an empty branch column where the tracefile
  carries 13,900 branches. Four of the files it named as least covered were between
  94% and 100%. A coverage table is read to decide where to spend effort, and this one
  inverted the ranking.

  The table came from `lcov --list`. The run's own `lcov --summary`, four lines
  earlier and on the same tracefile, was correct, and so was `lcov --list` run here on
  the run's own uploaded tracefile. The two builds differ only in distro patch level
  -- the runner installs `2.0-4ubuntu2`, this was checked on `2.0-1` -- and which
  patch does it has not been bisected.

  So the table is computed instead. A rate is a division of two integers the tracefile
  states outright, and `LF:` cannot be got wrong by a patch to `--list`. Cross-checked
  against `lcov --list` on all 39 files in that tracefile and independently against the
  raw counters: no disagreement in either comparison. `lcov --summary` keeps its job.

  The format now carries hit/found rather than only the total, because `93.7% 22765`
  cannot be checked by a reader and `93.7% 21320/22765` can -- and a reader who can
  check the number is the only one who will notice when it is wrong again.

  An absent counter prints `-`, not `0.0%`. A header with no branches at 0.0% would
  sort to the top and read as the least covered file in the tree, which is how a table
  misleads while every individual number in it is defensible.

  Twelve arms in `250-the-coverage-runner-must-refuse` drive the generator over a
  synthetic tracefile rather than grepping it, because a static check that the runner
  calls the right script cannot tell whether the script is correct, and "the table is
  wrong" was the defect. Three of them failed when first written: two matched the
  comments in the runner that explain the replaced call, and the third asserted a cell
  by the wrong field number. They are counted over code with comments stripped now,
  under a premise that stripping comments did not strip the code.

  A fourth failed on a substring: `0\.0%` matches inside `50.0%`, which is the line
  rate of the fixture that has no branches. It is anchored, and a control with a
  genuinely zero-covered file proves the anchor did not defeat the assertion.

- The matrix controller is asserted to record the installed library, because the
  failure if it stops is a green matrix-wide downgrade (#961).

  `run_all_versions.sh` builds once per major and runs every child suite with
  `PGC_SKIP_BUILD=1`, so each child checks the controller's stamp to learn whether
  the binary it measures came from this tree. If the controller's stamp write loses
  its third argument, every child reaches `source-only` and **passes** -- because
  `source-only` is also the state of every stamp written before #959, so it cannot be
  a failure. The whole matrix degrades to UNVERIFIED with a green rollup on both
  majors, and nothing says so.

  `@jdatcmd` asked for this arm while approving #960, on the ground that an arm
  catching a green failure is worth more than most arms.

  **The defect is a dropped argument at a call site**, so an arm that calls
  `pgc_write_source_stamp` itself would prove nothing: the function would be correct
  and the caller wrong. The new part extracts the controller's stamp block and runs
  it, so what executes is the real call site's own text.

  It costs no build. The block reads the installed library and fingerprints a tree;
  it does not compile. So it runs against a copy of the tree with `builddir` and
  `pgc` set the way the controller sets them -- 24 MB at 31 ms a copy, against
  minutes for a per-major build.

  Measured by mutating `run_all_versions.sh` itself, dropping the third argument in a
  way that still parses:

      the controller's stamp carries BOTH fields          got [1] want [2]
      a child reaches verified, not source-only           got [source-only] want [verified]
      every caller records the installed library's digest got [2] want [3]

  Three arms, on an edit a careless hand would make. The part also carries its own
  control: it removes the argument from the extracted block and asserts the same
  driver reaches `source-only`, so the two arms above cannot pass for a reason that
  is about the driver rather than the controller.

  The static sweep covers the other two call sites, `pgc_setup` and `devloop.sh`,
  where driving either would cost a build. It joins line continuations first, because
  all three calls are written across four lines and a per-line grep finds the
  function name on a line carrying no arguments at all.

  What it cannot see, which is worth stating: that the controller REACHES that line.
  The `make install` guard above it could start failing closed and this part would
  not notice. It asserts what the line does, not that control flow arrives there.

  One of the arms reported `got []` when first written, because it read `$PG_CONFIG`
  and no selftest part sets that -- the harness passes `PGC_SELFTEST_PG_CONFIG`.
  Under `set -u` that aborted the command substitution the driver runs in. The input
  is now asserted before use and the driver answers `driver-could-not-run` rather
  than nothing, because an empty result reports the same emptiness for "the
  controller is broken" and "this part misspelled a variable".

- A continuation check carries the discriminator its headline already names, so two
  checks stop sharing one ledger row (#982, first of eight).

  The ledger keys on `(suite, part, name)`. Twenty-four checks across eight suites share
  a key with another check, so one going red would mark its namesake observed-red for a
  claim nothing attacked -- the same failure `pgc_record`'s `BASH_SOURCE` part-derivation
  fixed across parts, one frame further in.

  The cause is a convention rather than carelessness. Suites name a follow-up check as a
  short continuation of a distinct headline, which reads well:

      PASS  a skipped timing check emits exactly one record
      PASS  and its verdict is SKIP

  Two parallel blocks in `400-a-check-result-must-be-machine` test `check_timing` and
  `check_ratio_needs_quiet_machine`. Their headlines say which; only the continuations
  were short enough to collide.

  **The rule this proposes, for the remaining seven files: a continuation check carries
  the same discriminator its headline already interpolates.** No new convention, no
  readability lost, and at loop sites the variable is already in scope:

      before   check_num "and does NOT report it as a missing file"
      after    check_num "and the s3 URL is NOT reported as a missing file"

  Six renames here, measured on the resulting log rather than asserted, against a control
  run on clean `main`:

      clean main bf325b34   857 records   854 distinct keys   3 colliding   3 lost
      this branch           857 records   857 distinct keys   0 colliding   0 lost

  The record count is unchanged, so this adds and removes no checks -- it only renames.

  A rename creates orphan ledger rows, and this removes the three it creates. Each carried
  verdict `never` and no mutation, which is the criterion `f80ca7d05` established for
  dropping a row without losing history; a row with a date or a mutation would have had to
  travel with the rename instead.

  **And a collapsed key cannot be un-collapsed with its history intact.** `rename-scan`
  pairs each old name with one of the two new ones arbitrarily, because one row cannot
  become two. It does not matter here -- all three carried nothing -- but if any of those
  six had ever gone red, the ledger would hold that red against a key covering both, and
  splitting it would leave nobody able to say which check earned it. That is an argument
  for fixing collisions before one of them reddens rather than after.

  The orphan check written for this also found two rows whose checks no longer exist
  anywhere in the tree, removed by #917 with their rows left behind. Those are not created
  by this change and are filed separately rather than tidied away here.

- The controller-stamp extraction anchors on the write rather than on file position
  (#961 follow-up).

  Selftest 460 took the **first** one-tab `if (` in `run_all_versions.sh`. There is
  exactly one today, so it was unambiguous, and the premises would have caught it if
  that stopped being true -- the extracted block would hold zero or two stamp writes.
  It was the premises doing the work rather than the anchor.

  The anchor now finds the stamp write and walks back to the `if (` enclosing it, then
  forward to the first terminator at or after it. A subshell added elsewhere at the same
  indent cannot move the range, because the range is defined by the line it is about.

  **A subshell that NESTS around the write can still widen it, and that is a premise
  rather than a fix.** The anchor matches `if (` at one tab, so a write inside a deeper
  subshell leaves the opener pointing at the outer block -- which holds exactly one
  one-tab `if (` and exactly one stamp write, so every other premise passes on a block
  wider than the call site. Measured on a fixture with the write two tabs in: eight lines
  out, all premises green. A premise counting `if (` at ANY indent distinguishes them --
  one in the real block, two in the nested shape -- which is cheaper than teaching the
  anchor to track depth and fails closed, refusing a shape it does not understand rather
  than driving it.

  Proven in both directions, because either half alone says nothing. Identical on
  today's input -- the extracted block hashes `47b4f1a9193c` before and after -- and
  different on the input that motivated the change:

      a second one-tab subshell injected ABOVE the stamp block
        OLD anchor   4 lines, 0 stamp writes, so it extracted the WRONG block and the
                     `exactly one stamp write` premise reads 0: loudly wrong
        NEW anchor   7 lines, 1 stamp write, unchanged

  md5-only would prove the change does nothing that matters; injection-only would prove
  it does something without showing what else moved.

  **And it closes a boundary the new design could open rather than one the old one
  had.** A backward walk has to decide what to do when it runs off the top of the file,
  and one of the three possible behaviours satisfies every guard in the part: emitting
  the write alone gives exactly one stamp write, so both premises pass on a block that
  is not the call site. This emits nothing instead, because `open` is never assigned and
  the guard exits before the print loop -- a property that arrived from the guard's
  shape rather than from foresight, now written into the code as load-bearing so the
  next reader does not default `start` to 1 as a tidy-up.

  One premise added, `the extraction produced a block at all`, because an awk whose
  condition never fires prints nothing and an empty block would otherwise read as a
  block with no stamp write in it -- two different failures arriving at the same number.

  Two stamp writes in **separate** subshells is the case the count premise cannot see:
  the extracted block holds one and the premise passes. The static caller sweep catches
  it -- injected, both the premise and the arm report `got [4] want [3]`.

- The hilbert premises name the table they digest, so five checks stop sharing three
  ledger keys (#982, third of eight).

  `hilbert_cluster` held the second-largest loss: a premise repeated before four separate
  arms, and two pairs of `(d)` checks repeated across two fixtures. Measured against a
  control run on clean `main`:

      clean main    181 records   176 distinct keys   3 colliding   5 lost
      this branch   181 records   181 distinct keys   0 colliding   0 lost

  The record count is unchanged, so this renames and nothing else.

  **A third form of the rule, and the files keep supplying them.** #984 said a continuation
  carries the discriminator its headline interpolates; #989 added that where the headline
  names none either, it gains one. Here *nothing* interpolates anything -- all eight sites
  are hand-written -- so the discriminator comes from the check's own **value expression**:

      premise: the plan being digested here is the columnar custom scan too
        "$(pgc_is_columnar_scan 'SELECT * FROM s3hi')"     -> ... digested for s3hi ...
        "$(pgc_is_columnar_scan 'SELECT * FROM s5hi')"     -> ... digested for s5hi ...
        "$(pgc_is_columnar_scan 'SELECT * FROM s6t')"      -> ... digested for s6t ...
        "$(pgc_is_columnar_scan 'SELECT * FROM av_hi')"    -> ... digested for av_hi ...

  That keeps the name and the assertion in agreement, which is worth more than brevity: a
  reader can check one against the other without leaving the line. The word doing the
  colliding was `here`, which named the site to someone reading top to bottom and named
  nothing at all to a key.

  The two `(d)` pairs take the table their own `count(*)` and `physlayout` name --
  `moved s4d1's layout`, `no row was lost from s4d2` -- for the same reason.

  No ledger change: `hilbert_cluster` is not one of the two suites the ledger covers.
- The object-storage loops name the case each iteration tests, so six checks stop
  sharing three ledger keys (#982, second of eight).

  `objstore_module` lost the most records of any suite to key collapsing: two loops of
  three iterations each, where the check names did not carry the thing the iteration
  varies. Measured against a control run on clean `main`:

      clean main    30 records   24 distinct keys   3 colliding   6 lost
      this branch   30 records   30 distinct keys   0 colliding   0 lost

  The record count is unchanged, so this adds and removes no checks.

  **The two loops show both sub-shapes of the same defect.** In the first, the headline
  already interpolated the scheme and only the continuation was short:

      check     "a s3 URL reports an object-storage error, not a missing file"
      check_num "and does NOT report it as a missing file"      <- identical three times

  In the second, *neither* name carried the metacharacter, so three iterations produced
  one key for each of **two** checks -- the headline collided as well.

  Both are fixed by the rule #984 proposed: the continuation carries the same
  discriminator its headline names, and where the headline does not name one either, it
  gains it. The discriminators come from the loop variable by parameter expansion
  (`${url%%:*}` and stripping the fixed prefix and suffix off the pattern), so they are
  already in scope:

      a remote glob (*) is handled remotely (an object-storage error, not a local one)
      and the * glob is NOT reported as a local filesystem miss
      and the https URL is NOT reported as a missing file

  The first loop's headline now reads its scheme from a variable rather than a `cut`
  subshell, because both names need it. The resulting check name is byte-identical, so
  no ledger row moves on account of it.

  No ledger change at all: `objstore_module` is not one of the two suites the ledger
  covers, so its check names have no rows. That is also why the twenty-four collisions
  matter for #432 rather than for the census today -- twenty-one of them are in suites
  that become covered only when the 240 are seeded.
- The last five suites' continuation checks name the case they continue, closing the
  rename half of #982 (ten records, five files).

  Ten checks across five suites shared five ledger keys with another check. Measured by
  running every one of the five on clean `main` and on this branch, on the same box:

      suite                     control (clean main)        this branch
      sorted_pathkeys           113 records 110 keys  3 lost   113  113  0
      vector_agg_tlist_shape     68 records  65 keys  3 lost    68   68  0
      alter_am_cleanup           45 records  43 keys  2 lost    45   45  0
      eager_ordering_record      31 records  30 keys  1 lost    31   31  0
      objstore_userinfo           7 records   6 keys  1 lost     7    7  0

  Every record count is unchanged, so this renames and nothing else. All five suites:
  `rc=0`, `FAIL=0`, on both trees.

  **This is not a new convention. It is each file's own convention, applied where it
  lapsed.** Every one of the five already names the case at a neighbouring site --
  `and DESC still answers correctly`, `and NULLS FIRST still answers correctly`,
  `and FILTER still answers correctly`, `and DISTINCT still answers correctly` -- and then
  falls back to a bare `and it still answers correctly` for the next four. The fix is to
  finish the pattern the author started:

      REFUSE: a non-prefix of the key is not an order the rows are in
        and it still answers correctly   ->  and a non-prefix still answers correctly
      REFUSE: a column that is not in the key at all
        and it still answers correctly   ->  and a non-key column still answers correctly
      REFUSE: FILTER inside an expression over aggregates
        and it still answers correctly   ->  and FILTER inside an expression still answers correctly

  Two sites took the discriminator from the value expression instead, because their
  headline names no table: `control: and it moved the layout` becomes
  `... moved the tailgate layout` and `... moved the lexgate layout`, matching the
  `layout tailgate` and `layout lexgate` the checks actually read.

  No ledger change: none of the five is one of the two suites `test/check_ledger.tsv`
  covers, and both ledger files are byte-identical to `main`.

  One coupling, found by review rather than by either PR's own checks: #998 added a skip
  loop to `sorted_pathkeys.sh` that lists its arms by name, and one of those names is the
  arm this change renames. The two merge cleanly, so nothing would have presented a
  conflict -- #998's own guard would simply have gone red in `main`. The loop is updated
  here, and its guard reports no mismatch. Counting the old name is how you MISS this: an
  unanchored `grep -F` finds 3 occurrences because both renames EXTEND the name rather
  than replace it, so each renamed line still matches its own old form. Anchoring on the
  closing quote gives 1, which is the one that matters.
- `pgc_ledger.py orphan-scan` reports a ledger row that no record in its own part
  matches, and `--prune` removes it only when no history would be lost (#983).

  The comparison already existed and already printed the answer. `rename-scan` pairs an
  appearance with a disappearance, so an UNPAIRED disappearance -- a check deleted, or
  renamed in a run where nothing appeared -- printed `vanished=2` and returned 0. Two rows
  in the committed ledger named checks that no longer existed; the census counted both, and
  every run for days said so in a line nobody acted on. **A guard that compels one list and
  ignores the second manufactures the confidence that the thing is handled.**

  Driven on the real ledger, which is the only instance that matters:

      orphan: harness_selftest 330-... premise: all three runner functions were extracted
      orphan: harness_selftest 330-... premise: and all three are callable
      not checked: 44 row(s) in 1 part(s) this run does not contain
      orphan scan: parts in the run=43, rows in those parts=861, orphans=2
                   (0 carrying history), not checked=44
      orphan prune: removed 2 row(s), the ledger now holds 925

  **A row carrying history is never pruned**, and one such row refuses the WHOLE prune.
  No run can recreate the catalogue of what has been seen red, and removing the safe rows
  while naming the unsafe ones would leave a partial job for whoever reads the output.

  **Rows in parts the run does not contain are counted out loud as `not checked`**, never
  as present. Otherwise a single-suite log would certify the whole ledger, which is the
  same defect one level up.

  **`--prune` refuses a part that skipped, which was this change's own worst bug.**
  `not checked` protects a part the run does not contain. A part *contained but skipped
  wholesale* fell in the gap: one SKIP record put the part in the run's `parts`, every other
  row of that suite became an orphan, and `--prune` deleted the suite while reporting
  `not checked=0` and `rc=0` -- the most confident output the tool can produce. Measured on
  a three-row fixture for `analyze_differential`, whose PG17 run is a single SKIP; found by
  @pgcolumnar-9b in review. Nine suites skip wholesale on PG17 and `suites_not_covered` is
  250, so seeding any one of them would have armed it.

  The rule is broader than that case deliberately: a SKIP **anywhere** in the part means
  some arm did not run, so the run cannot tell "this row's check was deleted" from "this
  row's check was skipped under a name that does not match it" -- #994's defect at suite
  granularity rather than branch granularity. One skipped timing check therefore blocks
  pruning that whole part, and that is the direction a deleting command should err in. The
  control holds the other half: the same rows are still pruned when the part's record is a
  PASS, so this is not a tool that refuses to prune anything.

  The four categories -- matched, orphan, unprunable, not checked -- are asserted to account
  for every ledger row, because a classification that silently loses one is the failure this
  tool exists to report.

  **It reports and is NOT wired into the gate, for a measured reason.** Scanned against a
  real run, `340-the-binary-must-be-built-from.sh` records ONE skip under a name neither
  of its two arms has (`the unreadable-source refusal`) whenever the box has no non-root
  user to read as. On such a box two live ledger rows have no matching record, so an absent
  record does not yet mean a removed check and a gate refusing on absence would redden a
  correct run. Arming it needs those branches to record a SKIP under the names they stand
  in for -- the conversion #965 made for the eleven timeout paths -- and an arm now pins
  that shape so the day it changes, the arm says so.

- Nine git bundles are out of the tree, `*.bundle` is ignored, and
  `310-a-compiled-artifact-must-not-be.sh` now covers transfer artifacts as well as
  compiled ones.

  **I put them there.** 76,194 bytes across nine files went into `c697c8cd` -- a merged
  commit whose subject is a check name in `sorted_pathkeys.sh`. I create bundles in my clone
  to move a branch into the audit container, and I staged with `git add -A`. That is the
  same mechanism the existing comment in part 310 calls out for a stray `.pyc`: *a tracked
  build artifact joins whichever commit is next.*

  **Nothing caught it.** Five suites and the whole selftest ran green either side, because
  no suite has an opinion about files it does not read. What found it was a later rebase
  printing the filenames in a list I happened to read.

  Part 310's scope note said Python only, and deferred the wider question:

  > Whether every derived file in the tree deserves one rule is a larger judgement and is
  > deliberately not decided here.

  This decides it for one more class, and only that class. **A bundle is a transfer
  artifact**, which is why it belongs beside the `.pyc` rather than beside the Parquet
  fixtures: it is derived from commits already in the history, it is named after whatever
  branch was in flight, nothing in the tree opens one, and the next person to make one will
  choose a different name -- so it can never become a fixture anything depends on.

  Same two-part rule, same `no-repo` discipline as the Python arms, and a control: a source
  file merely *named* like a bundle (`test/bundle_notes.sh`) must not be ignored, or the
  suffix rule is broader than it claims.

  The blobs stay reachable in the repository's history -- removing a file from the tree does
  not unwrite it, and rewriting `main` is not something a stray artifact justifies. What this
  stops is the tree carrying them, and the next `git add -A` re-adding them.
- `s3://` and `gs://` now refuse URL userinfo, as `http(s)://` has since #706. They were
  absorbing it into the bucket name and reporting a missing object (#995).

      http://u:p@127.0.0.1:1/x.parquet   22023  userinfo in "..." is not supported
      s3://u:p@mybucket/x.parquet        58P01  "..." does not exist (HTTP 404)   <- before
      s3://u:p@mybucket/x.parquet        22023  userinfo in "..." is not supported <- after

  The old message was true and useless: the object did not exist *because* `u:p@mybucket`
  had become a bucket name. `s3://user:key@bucket/obj` is a form other tools accept, so it
  is a mistake a user makes, and the answer sent them looking for a missing object.

  **Measured on main with an endpoint configured**, which is the only way to reach the
  authority parse at all:

      path style     HEAD /u%3Ap%40pgc-bucket/vh.parquet, Host untouched -> HTTP 404
      virtual host   the bucket becomes the leftmost Host label -> could not resolve
                     "u:p@pgc-bucket.s3.local"
      write          export_parquet raised NO ERROR and PUT to
                     /u%3Ap%40pgc-bucket/ui.parquet -- a bucket nobody named

  **Not an SSRF, and that was checked rather than assumed.** Nothing splits the authority
  at `@`, so the userinfo never becomes basic auth and never moves the host. The write row
  is why this is refused rather than documented: acceptance there sends bytes to a bucket
  the caller did not ask for.

  The guard sits **before the endpoint is resolved**. Placed after it, the refusal would be
  unreachable whenever no endpoint is configured, because the s3 branch demands one first
  -- and the arms could then say nothing without an object-store fixture.

  **Twelve arms** added to `test/objstore_userinfo.sh` -- five refusals and **seven
  controls**, taking the file from 7 checks to 19 -- because
  the first probe of this gap proved nothing: with no credentials every s3 URL returned
  `28000`, the clean one included, so a userinfo refusal was indistinguishable from an
  unreachable object store.

  The controls hold that a clean URL still reaches the endpoint demand **and that the
  message names `AWS_ENDPOINT_URL`**, that a malformed URL still gets the bucket/key
  refusal, and that an `@` in the KEY is left alone.

  That positive matcher matters more than it looks: `28000` is raised by **three** different
  demands in `os_resolve_s3` -- a missing endpoint, a missing credential, and the
  authorization refusal -- so a control that sees `28000` and no `userinfo` has pinned
  nothing about which one fired. The `gs://` control makes the point concretely: its `28000`
  is the **credential** demand, because `gs` defaults its endpoint to the interop host and
  never reaches the endpoint demand at all. Same code, different cause, and only a matcher
  that names the variable can tell them apart.

- The matrix runner's accounting breakdown says which LINE it means, because the old
  wording produced a wrong planning number twice in one night.

  It printed:

      N via lib.sh's accounting; by their own mechanism: audit bench_guards concurrency ...

  and "by their own mechanism" was read -- by two different readers, hours apart -- as *emits
  no RESULT records*, which would make those twelve suites impossible to seed into the
  mutation ledger. A bound on how far `suites_not_covered` can fall was derived from that
  reading.

  **It is false. Measured by running all twelve and counting:**

      audit 31   smoke 9   phase2 42   phase3 32   phase4 38   phase5 36
      phase6 43  concurrency 7   unique_conc 31   update_conc 25
      bench_guards 0   docs_style 0

  **Ten of the twelve emit records.** The set is the suites whose log lacks lib.sh's
  `accounting:` line and carries their own `checks run:` instead -- a statement about the
  accounting LINE and nothing else. Only `bench_guards` and `docs_style` emit no records, for
  the reason `pgc_log_shows_any_accounting`'s comment already gives: those two never source
  `lib.sh` at all. Defining a private `check` is orthogonal -- `audit` does it and records 31.

  So the bound is **two suites, not twelve**, with about 294 records sitting in the other ten.

  The label now names the line and disclaims the records in the same breath. The selftest arm
  that pins it follows the reword; widening that arm to pin the disclaimer separately needs
  either a new ledger row or a rename that would orphan an existing one, and `main` has no
  tool to remove an orphan until #983 lands -- so the sequencing is written into the arm's
  comment rather than quietly skipped.
- A `conftest.py` can no longer stub an `Expect` method on the class so a false
  claim reports as a pass (#967).

  #964 snapshots the module's bindings. `Expect.num = a stub that still counts`
  is not a rebind of `Expect` -- the name still points at the same class -- so a
  test asserting `1 == 2` printed `1 passed` and exited 0. That is strictly worse
  than switching off a meta-rule: the comparison never happens, the count still
  rises, and every guard downstream is satisfied by a test that concluded nothing.

  Public methods of `Expect` are now snapshotted by identity, the same way the
  module bindings are. The refusal names `Expect.num` and restores the method
  before raising, so an in-process `pytester` inner session cannot poison the
  rest of the file. Names that start with `_` are excluded: stubbing `_record`
  still leaves the count at 0 and is refused by `pytest_runtest_call`, which is a
  different mechanism and the control this issue asked to keep.

  What this does not close: `expect.num = stub` on the instance, or a subclass
  yielded by an overridden `expect` fixture. Both still keep the count and drop
  the comparison. A snapshot of `Expect.__dict__` cannot see either. #967 stays
  open for those two routes.

- A collection-time vacuity refusal keeps its reason under pytest-xdist (#963).

  `pytest_collection_modifyitems` raises `UsageError`. Serial, that is rc 4 and
  the sentence on stderr. Under `-n` pytest still runs `pytest_collection_finish`
  in a `finally`, so the worker tells the controller it collected the tests and
  then exits. xdist's `worker_workerfinished` asserts a worker that collected
  tests must not finish with them pending: a 35-line INTERNALERROR, rc 1, and
  the sentence is gone. Measured on the pinned runner (pytest 9.1.1,
  pytest-xdist 3.8.0).

  A worker now records the sentence on `workeroutput` and clears the items so
  no ids cross. The controller re-raises `UsageError` from `pytest_testnodedown`,
  which is the process serial already used. The in-test control (a body that
  concludes nothing) is unchanged in both modes.

- A collection-time vacuity refusal is no longer also reported as a silent loss (#991).

  The layer refused a run and then contradicted itself:

      ERROR: the pgColumnar vacuity layer refuses this run: a bare skip is refused ...
      VACUITY: 1 collected test(s) never reported an outcome, so the run lost them
               silently: tmp/test_offender.py::test_one_offending_arm

  **The run did not lose it silently. It refused it loudly, one line above.** So a reader who
  typed one bare `@pytest.mark.skip` got the correct diagnosis and then a second finding
  telling them a test vanished without saying so -- and the natural response is to go looking
  for a lost test that was never lost.

  `collected - reported` is the right set difference and the wrong **meaning**: a silent loss
  is when nobody said anything, and here the layer itself is what stopped the run. The guard
  whose whole subject is a silent loss was firing on the one event that is its opposite.

  **One assignment covers all five refusal sites**, because #963 gave the layer a single
  chokepoint: `_collection_usage_error` records the refusal, and the reconciliation in
  `_RunShape.pytest_sessionfinish` skips the missing-outcome problem when it is set. Only that
  problem. The setup-skip problem still prints -- a fixture removing every test that depends on
  it is not something a refusal accounts for, and the two are independent findings.

  **Serial was the only path left.** #963 clears `items[:]` in the worker, so the controller's
  `collected` set is already empty under `-n` and the contradiction cannot arise there:

      main   serial   refusal + the silent-loss line
      main   xdist    no refusal at all            (that was #963)
      #963   serial   refusal + the silent-loss line   <- what this closes
      #963   xdist    refusal, no line

  The second arm is the control and the first is worth nothing without it: an item collected
  and never reported, with **no** refusal anywhere, is still named and still reddens the run.
  Dropping a problem from a reconciliation is one edit away from dropping the guard. The
  fixture removes an item after the layer's hook recorded it and without deselecting it, which
  is the shape of a crashed xdist worker -- `pytest_deselected` is what tells a deliberate
  subset from a loss, and nothing calls it there.

  No shell check moved, so no ledger change: `harness_selftest` is 907 checks on both trees.

## [1.0-alpha3] - 2026-09-02

### Added
- A pytest harness beside the bash suites, with a layer that refuses tests which
  assert nothing (#432).

  `test/pytest/` connects through `psycopg` rather than parsing `psql` output, so a
  test can assert the TYPE as well as the value: `psql -At` returns text, and an
  `int4` `1` and a `text` `'1'` are the same string to a bash oracle.

  This ports ONE bash suite. `test/` carries 4,429 anchored assertions across 256
  suites, so this is 0.18% of them and is not coverage. The layer is the point:
  71 tests in 6 files, of which 56 test the harness rather than the product.

  A pytest run fails open in several ways this project has already been bitten by:
  a test that asserts nothing passes, a filter that selects nothing exits 0, and a
  fixture that skips greens every test under it. `pgc_vacuity.py` is loaded for
  every run and refuses those shapes, along with an empty result compared with an
  empty result, a value compared against itself, a plan matched by substring rather
  than by typed key, an absence claim over an empty plan, `cursor.rowcount` of
  `-1`, and a broad `except` found by walking the AST rather than by line regex.
  `xfail_strict` is on, and `--pgc-expect-tests N` asserts the run's own shape and
  refuses `N = 0`.

  Every refusal has a red test that runs pytest inside pytest and asserts on the
  INNER run's outcome, which is what proves a guard refuses rather than assuming
  it. Each records the bare-pytest behaviour it exists to stop; every one of those
  measurements exited 0. Positive controls sit beside the guards, so a guard that
  starts rejecting good tests reddens there first.

  **Asserting that an inner run failed is not asserting that a named guard fired.**
  A census that neutered each guard alone found most of them deletable with
  `test_layer.py` still green, from two causes: several were never driven at all,
  and several are subsumed by a neighbouring guard, so the inner run fails either
  way and an outcome-only assertion cannot tell which fired.
  `test_guards_pinned.py` pins each refusal to its own MESSAGE through
  `expect.refusal`, which refuses to be called with no pattern -- the same move as
  asserting on a SQLSTATE rather than on prose.

  **`plan_marker`'s two arms are pinned, and the hole under them is closed.**
  Both could be deleted independently with the suite green -- and it is the worst
  place in the layer for that, because `plan_marker` is the port of
  `pgc_is_columnar_scan` and is used as the PREMISE that the vectorized aggregate
  engaged. A premise that cannot fail turns its test into one about an ordinary
  plan. Underneath both sat a third hole: an absence claim is satisfied by
  nothing being there at all, so `plan_marker([], key, absent=True)` passed
  against a plan that never arrived. That is now refused, for the present arm
  too. Each of the three neutered alone reddens exactly one test, and it is that
  test's own -- which is what proves they are distinguishable rather than
  subsumed.

  **The corpus builds and installs before it measures anything.** It did not at
  first: with `#error` appended to a source file and nothing rebuilt, the run
  reported 25 passed and exit 0 while the bash suite reported `FATAL: the build
  failed` and exit 1. The refusal now comes from `pgc_build_and_install` in
  `test/lib.sh`, driven from Python so there is one implementation rather than
  two that can drift, and it runs BEFORE the cluster starts --
  `shared_preload_libraries` maps the library at postmaster start, so a cluster
  started before the install keeps the old one mapped for its whole life.

  **The third state is a state, not a comment.** `expect.cannot_run(REASON,
  detail)` wrote a field nothing read, so a test declaring itself unrunnable
  reported `1 passed` and exit 0 -- a write-only flag, the shape selftest 320
  already polices in the runner. It made the layer's own escape hatch its largest
  hole, because a bare `@pytest.mark.skip` fails the run while the honest-looking
  alternative greened silently. A run holding one now exits 67, the same number as
  `PGC_EXIT_INCOMPLETE` in `lib.sh`, and prints the reason and detail in the same
  shape; a run holding a real failure as well still exits 1, because failure
  dominates. Verified serial and under `-n 2`, the declaration travelling to the
  xdist controller on the test report.

  **`test/pytest/TESTS.md` is checked rather than trusted.** It documents every
  test in the corpus, and it went stale inside a single rework: 29 of 54 tests
  were named nowhere in it while its header still claimed 25. A guard now requires
  every file and every test to be named there, and requires the totals it states
  to be the totals on disk -- a document can name every test and still miscount
  them. It is written twice, as `test/selftest/350-the-pytest-corpus-must-be.sh`
  and `test/pytest/test_docs_cover_the_corpus.py`; only the first runs in the
  gate, and it reddened on its twin's arrival before this entry was written.

  Not registered in `test/run_all_versions.sh`. That would add a `psycopg` build
  dependency to every CI leg for 0.18% of the assertions; `test/pytest/README.md`
  records what registering would cost and what has to be true first.


- A nanosecond Arrow import says how many values lost precision, and still
  imports every row.

  **`pgcolumnar.import_arrow` narrows nanoseconds to microseconds and always
  did.** PostgreSQL timestamps and times are int64 microseconds; Arrow
  parameterises the unit in the type. Of Arrow's four units, second,
  millisecond and microsecond all widen or match exactly, so only nanosecond can
  lose anything, and only for a value that is not already on a microsecond
  boundary.

  The narrowing was silent, and that is what changed. An import now ends with

      NOTICE:  columnar.import_arrow: 12 values lost sub-microsecond precision
      DETAIL:  PostgreSQL timestamps and times hold microseconds; this Arrow file
               declares nanoseconds.
      HINT:    Import the raw nanoseconds into a bigint column if the extra digits
               are significant.

  **Counted per value, not per type.** A pandas `datetime64[ns]` column built
  from second- or millisecond-resolution data is nanosecond-TYPED and entirely
  lossless to convert; it reports nothing. Only values that actually carried
  sub-microsecond digits are counted. The remainder is not extra work:
  `arrow_floordiv` already computes it to decide the rounding direction.

  The counter is reached from the time arm as well as the timestamp arm, and is
  threaded through the list and struct recursion, so a nested `timestamp[]` and a
  `time64[ns]` column are counted the same way.

  **Nothing is refused.** `NOTICE`, not `WARNING` or `ERROR`: a bulk load must
  not fail on the last row of a large file for a conversion the caller may have
  intended. Out-of-range values are still refused with `22008`, unchanged.

  Truncation does more than reduce precision -- it can make rows that were
  distinct in the file compare EQUAL here, so a `UNIQUE` violation or a lost
  `ORDER BY` tie-break now has its cause stated at the point it was introduced
  rather than surfacing later as a mystery.

- The two visibility-map clears that no test held are now held (#877).

  **Three paths retire a row group and give its live rows new row numbers**, and
  each must clear the all-visible bits over the OLD numbers, or an index-only
  scan answers from the index for a TID whose group no longer exists. Only
  `pgcolumnar.expire`'s clear was covered, because only its absence had been
  reported as data loss. Deleting the other two -- `rewrite_one_group`
  (`src/columnar_vacuum.c:346`, reached through `pgcolumnar.compact_rewrite`) and
  `pgcolumnar_recluster_online` (`:765`, through `pgcolumnar.recluster`) -- left
  319 checks across 14 suites green.

  `test/vm_clear_on_renumber.sh` is the two missing arms. Neuter the clear at
  `:346` and one arm reddens; neuter the one at `:765` and two redden; neither
  mutation reddens the other's arms.

  **The instrument is why this was not covered earlier.**
  `pg_class.relallvisible` is a statistic `VACUUM` refreshes, and clearing a bit
  does not touch it, so an arm reading it reports the same number with the clear
  and without. `pgcolumnar.vm_is_visible(rel, blk)` reads the fork through
  `visibilitymap_get_status`, which is the call the index-only-scan executor
  makes.

  **And a `DELETE` clears the bits of the blocks holding the deleted rows**, so a
  group made compactable is already not-all-visible there and an arm placed on it
  cannot fail. These arms `VACUUM` first, delete only the front of the target
  group, and assert over the group's later blocks, which are all-visible on the
  way in and reachable only by the rewrite.

  **The two controls differ in strength and the suite says so.** The rewrite's
  control is a group in the same table, and widening that clear to the whole
  relation reddens it. Recluster renumbers every group, so that half has no
  in-table control; its control is a second relation, which catches a clear
  reaching beyond the relation it was called on but NOT an over-broad range --
  the same widening leaves it green. That limit is measured and recorded in the
  file rather than left for a reader to infer.

- `MERGE` is documented as working, which it has been all along. It needs no
  index on the columnar target and takes every arm, including
  `WHEN MATCHED ... DELETE`, `WHEN NOT MATCHED BY SOURCE`, and
  `RETURNING merge_action()`. A columnar table can be the source as well as the
  target. Verified rather than assumed, on PostgreSQL 17.10, in four shapes.

  Nothing in the code changed. The gap was that a reader had no way to learn
  this: `MERGE` appeared in no user-facing document, and its absence from
  `docs/limitations.md` reads as easily as "unsupported" as "supported".

  `docs/features.md` also records what a `MERGE` costs, because that is the part
  a reader acts on: its updates and deletes mark rows rather than rewriting row
  groups, exactly as a plain `UPDATE` or `DELETE` does, so space returns only
  after `pgcolumnar.vacuum`.

### Fixed

- A projection created mid-transaction receives the writes that follow it
  (#875).

  **A write before `pgcolumnar.add_projection()` in the same transaction made
  every later write in that transaction skip the new projection.** The rows
  landed in the base table and never reached the projection, with no error, and
  a covering projection scan then answered as though they had not been inserted.

      BEGIN;
        INSERT INTO t ...;                       -- any write will do
        SELECT pgcolumnar.add_projection('t', ...);
        INSERT INTO t ...;                       -- these rows were lost to it
      COMMIT;

  Measured: 116 rows in the base table, 105 in the projection.

  `PgColumnarProjectionFanoutRow` builds the write state's projection-writer
  list on first use and latches it, **including when the list comes back
  empty** -- which is what it is before the projection exists. `add_projection`
  now drops that cache, after the back-fill, so the writes that follow rebuild
  it from the catalog.

  **`drop_projection` had the same defect in the other direction.** A writer
  cached before the drop kept taking rows, which landed in a projection storage
  whose catalog rows were already deleted and committed as an orphan: one orphan
  storage id when the drop happened mid-transaction, none when it had the
  transaction to itself. It drops the cache too.

- An Arrow import reads the width, sign and scale the FILE declares (#881).

  **`imp_apply_field` inspected only `Date`, `Time` and `Timestamp`.** For every
  other tag the stride and the interpretation came from the TARGET column, so a
  file that declared something else was decoded as though it had not:

      uint64 2^63+5        into bigint          ->  -9223372036854775803
      int64  1,2,3,4       into int             ->  0,0,1,2
      decimal(10,2) 1.25   into numeric(20,4)   ->  0.0125
      fixed_size_binary(32) into uuid           ->  the first 16 bytes

  All four imported without an error. They are refused now with `42804`.

  The buffer-length check already caught the cases where the file's carrier is
  NARROWER than the target; these are the ones where it is the same width or
  wider, so the buffer is long enough and nothing complained.

  **Scope is within a family.** A tag that does not match the column's family at
  all -- an `int64` read into a `timestamp` as raw microseconds -- is
  long-standing accepted behaviour with its own test, and is unchanged.

- `read_projection` explains itself after a rewrite (#876).

  A rewrite mints a new storage id while `pgcolumnar.projection` keeps the old
  one, so a projection that is still declared reads as absent. The error said
  only `projection "p" does not exist on "t"`, which is not true -- the
  declaration is intact. It now carries a hint naming
  `pgcolumnar.rebuild_projections()`, which recovers it. The underlying
  re-recording is still open as #876.

- `DROP` after `ALTER TABLE ... SET ACCESS METHOD heap` now takes the
  relid-keyed catalog rows with it, and the hook that does it stays out of the
  way in databases that have no extension.

  **Two catalogs are keyed by relid rather than by storage id.**
  `pgcolumnar.options` and `pgcolumnar.projection_declaration` survive the
  rewrite onto heap storage, which drops only the storage-id catalogs. The drop
  hook then looked at the access method, saw heap, and returned before touching
  them. `config_dump` emits a bare oid for a relid that no longer resolves, and
  `rebuild_projections()` aborts on an orphan declaration, taking every other
  table in the database with it. That is the blast radius #304 closed for a
  plain `DROP` of a still-columnar table, reached instead through access-method
  conversion.

  **The hook runs in every database of the cluster, because the library is
  preloaded rather than loaded by `CREATE EXTENSION`.** So it must reach the
  columnar catalogs only where they exist. It now gates on
  `pgcolumnar.options` resolving, not on the schema: `DROP EXTENSION` leaves the
  schema behind and takes its tables, so a schema test passes in exactly the
  case where the catalogs have gone.

  It also skips the extension's own member relations rather than everything in
  the extension's schema. A user table that merely lives in `pgcolumnar` is not
  a member, and it gets cleaned up like any other.

  This matters beyond `DROP TABLE`. Every table rewrite drops a transient
  relation through the same hook, so `VACUUM FULL`, `CLUSTER`,
  `ALTER TABLE ... ALTER COLUMN TYPE` and `CREATE MATERIALIZED VIEW` take the
  path too, and `vacuumdb --full --all` visits every database in the cluster.

- `pgcolumnar.expire()` no longer drops live rows, and every path that renumbers
  live rows now clears the visibility map (#403).

  **Three separate ways to lose data or read a row that is gone.**

  A group holding a row with a `NULL` retention value could be retired, taking
  live rows with it. `expire` now keeps any group whose retention column has a
  LIVE `NULL`, because a `NULL` has no age and the group cannot be known to be
  wholly expired. Deciding that reads the retention column of a candidate
  group, on a table that has any deletes. A table with no deletes still decides
  from row-group metadata alone. Nothing is rewritten either way.

  Live is the operative word. The zone map's null count is recorded when the
  group is written and never revised, so it still counts rows a later `DELETE`
  marked. Reading it as the live count keeps a group whose every live row is
  past retention and whose `NULL` rows have all been deleted, and keeps it
  permanently, because nothing rewrites a zone map on delete. That trades data
  loss for silent over-retention. `expire` now checks the live rows, and only
  for a group that has both recorded `NULL`s and deletes: with no delete vector
  the recorded count is still exact, so the metadata-only path is unchanged and
  `expire` still reads nothing.

  A negative `ttl_interval` put the cutoff in the future, so `expire` retired
  groups that were entirely inside their retention. `set_options` range-checked
  every other option it accepts and not this one. Zero and negative intervals
  now raise `22023`.

  Retiring a group leaves its old row numbers in the visibility map, so an
  index-only scan answers from the index for a row group that is gone. **None of
  the three paths cleared them.** `expire`'s absence is the one that was
  reported; `pgcolumnar.recluster()` and the partial-group rewrite behind
  `pgcolumnar.compact_rewrite()` renumber live rows through the same retire and
  had the same gap, with no report against either.

  All three clear now. As of #878 all three are held by tests; before it, only
  expire's clear was. The rule is that visibility-map bits go wherever row
  numbers are reassigned, not only where rows expire.

  `docs/sql-reference.md` gains the accepted range for `ttl_interval` and the
  `NULL` rule, which is stronger than the straddling behaviour the page already
  described: a straddling group is released once its newest row ages past the
  cutoff, and a group holding a `NULL` never is.

- The matrix runner counts incomplete suites per major, not per run, and the
  INCOMPLETE dispatch is now testable end to end (#858).

  **`suites_incomplete=${suites_incomplete:-0}` is a `set -u` guard, not an
  initialiser.** It keeps whatever the previous major left, while `suites_ran`
  and `suites_skipped` on the lines above it are zeroed unconditionally, and
  `verfail` is reset per major too. On the five-major matrix the count
  accumulated: PG16 would report PG15's incomplete suites in its own summary
  line and still print `PASS`, because the one number in a per-major report
  belonged to the whole run.

  Latent today. `check_unrunnable` has no production call site, so no real suite
  can reach the INCOMPLETE state in a matrix run yet.

  **The tally is now a function, `pgc_tally_suite`, rather than four branches in
  the middle of the per-major loop.** The regression #859 shipped was not in the
  classifier: it returned INCOMPLETE correctly and the caller threw the answer
  away into a write-only flag. Nothing could reach the caller, because a loop
  that needs a suite list and a populated build directory is not something a
  selftest can drive. Extracted, the whole chain is drivable, and
  `test/selftest/330-the-incomplete-path-must-run-whole.sh` drives it: a real
  suite exits 67, the runner's own classifier reads the files that suite wrote,
  the runner's own tally consumes the classifier's verdict, the runner's own
  collect loop runs over both fixtures, and the runner's own major-verdict
  branch decides PASS or FAIL.

  The arm that earns the file reads the loop's own text. Every behavioural arm
  stays green if the runner defines the tally and never calls it, which is the
  defect class the file exists to prevent.

- The planner estimate counts live rows, and reads the delete count in one
  catalog scan rather than one per row group.

  **`row_group.row_count` is physical occupancy.** It still counts rows that a
  later `DELETE` marked in the delete vector, and the planner uses this callback
  instead of `pg_class.reltuples`, so every scan of a heavily deleted table was
  priced as if the deletes had not happened.

  The count now comes from one indexed scan summing
  `delete_vector.deleted_count` over the storage. The earlier shape walked each
  group's bitmap a bit at a time, once per row group, on every plan of a
  columnar relation.

  Summing is exact rather than an approximation. `delete_vector` carries a
  unique index on `(storage_id, group_number)`, so there is one row per group
  and no two summands can count the same row. The comment that justified the
  per-group fold said the opposite, and the schema forbids what it described.
- `UPDATE` now fans the new row version out to every covering projection, so a
  projection scan stops answering as if the updated rows were gone.

  **A projection stored the base row number, and `UPDATE` never gave it the new
  one.** A projection joins back to the base table on the row number and takes
  visibility from the base delete vector, so `DELETE` needs no rewrite: the
  vector hides the old number from the projection too. `UPDATE` is delete-old
  plus insert-new, and only the delete half reached the projection. The
  projection kept the old number, the delete vector hid it, and the new number
  was nowhere. `read_projection` returned no rows, and a covering projection
  scan (projected columns with a sort-key qual) answered as if the updated rows
  had been removed.

  The base table was always correct. The loss was confined to reads the planner
  served from a projection, which makes it the worse shape: the same query
  returns different answers depending on whether the projection is chosen.

  `tuple_update` now calls the same fan-out that insert calls.

  `docs/features.md`, `docs/administration.md` and `docs/how-to.md` said only
  that inserts write projections. All three now say updates do too, and say why
  a delete does not.
- Arrow import reads the temporal unit and carrier width the file declares,
  rather than assuming the ones our own exporter writes (#864, #865).

  **A file could say what it meant and be read as something else.** The importer
  built its decode plan from the target column type alone and never opened the
  Arrow `Field` table, so the unit went unread. A `date64` column holding
  2000-01-01 was decoded as a day count and stored as `4908285-05-04`; a
  `timestamp` in seconds became `1970-01-01 00:15:46.6848`, in milliseconds
  `1970-01-11 22:58:04.8`, and in nanoseconds `31969-04-01`. A `time64` in
  nanoseconds holding noon stored `12000:00:00`, a legal PostgreSQL time. None
  raised an error. `time32` could not be imported at all, failing with "value
  buffer too small for the row count".

  Only microsecond timestamps and times, and `date32` dates, were read
  correctly, and those are exactly what `export_arrow` writes -- so a round trip
  through our own exporter never showed the defect.

  The import now reads `Date.unit`, `Time.unit`, `Time.bitWidth` and
  `Timestamp.unit` from the file and scales to PostgreSQL's units, treating an
  absent field as its FlatBuffers default. That last part matters: a writer omits
  any field equal to its default, and two of these defaults are not zero, so
  pyarrow emits `date64` and `time32[ms]` with no unit field at all. Reading an
  absent field as zero is what produced the `date64` result above.

  Scaling a coarse unit up can leave PostgreSQL's range, so each conversion is
  overflow-checked and refused with `22008` rather than wrapped. Nanoseconds are
  narrowed to microseconds, which PostgreSQL cannot store beyond: narrowing keeps
  the instant, where reading nanoseconds as microseconds is wrong by a factor of
  1000. Every narrowing floors, so an instant before the epoch reports the day and
  the microsecond it falls in rather than the one after.

  The unit is applied to nested fields too, so a `timestamp[]` or a composite
  with a temporal member is read by its own declared unit.

  Reading the file's declared type also closed a third case, found while fixing
  these two and present since Arrow import shipped: a temporal file whose type
  the target column cannot hold was read anyway, taking the low four bytes of an
  eight-byte carrier. A `time64` file imported into a `date` column stored
  `687342-02-27`, and a `timestamp` file into the same column stored
  `2722128-09-17`. Both are now refused with `42804`, naming the Arrow type and
  the column type. A file whose type is not temporal is unaffected, so importing
  a plain `int64` file into a `timestamp` column still works.

- `TABLESAMPLE` on a columnar table is refused rather than silently ignored
  (#866).

  The AM's sample callbacks have always raised `0A000`, and
  `docs/limitations.md` has always said so. The planner hooks never let a query
  reach them: `set_rel_pathlist_hook` added a custom scan in place of the Sample
  Scan, and the vectorized aggregate paths did the same for an aggregate. The
  sample was then not applied at all. Measured on 10,000 rows with a heap mirror
  as the oracle: `TABLESAMPLE BERNOULLI (10)` returned 1030 rows from the heap
  table and **all 10,000** from the columnar one, and `sum(id)` came back as the
  whole table's sum rather than a sample's. `SYSTEM` behaved the same way.

  A wrong answer is worse than a refusal, and the refusal is the documented
  behaviour. All three hooks that read the range-table entry now decline when
  `rte->tablesample` is set, so the query reaches the callbacks and raises. An
  unsampled query on the same table still takes the custom scan.

- A custom scan no longer hides the children of an `INHERITS` hierarchy (#871).

  A legacy inheritance parent is an ordinary `RELKIND_RELATION` carrying
  `rte->inh`. `set_rel_pathlist_hook` still runs on that appendrel after core
  has built the `Append` over its children, and a custom scan added there reads
  only the parent's own storage. Every child's rows became invisible, with no
  error and no warning. Measured with a heap mirror as the oracle, parent 1 row
  and child 5000: `SELECT count(*) FROM parent` returned **1 where 5001 is
  right**, from a plan containing no `Append`. The ungrouped vector aggregate
  path had the same hole; the grouped path already refused this shape.

  Both hooks now decline when `rte->inh` is set, leaving the hierarchy to the
  ordinary `Append` plan. The children keep the custom scan, because a child's
  own range-table entry has `inh` false: the resulting plan is an `Append` of
  two `PgColumnarScan` nodes with pushdown intact, not a fallback to `Seq Scan`.

- Both compaction thresholds are validated, in both entry points (#860).
  `pgcolumnar.compact_rewrite` accepted `NaN` for `min_deleted_fraction`, because
  `NaN < 0.0` and `NaN > 1.0` are each false. The call then matched no row group
  and reclaimed nothing while reporting success.

  `pgcolumnar.maintenance_due` is the gate the `pgcolumnar.autovacuum` daemon
  consults before it ever calls `compact_rewrite`, and it validated nothing at
  all. Measured on a table with 10000 of 20000 rows deleted: `NaN` and `2.0` both
  reported `compact_rewrite_due = f`, suppressing the work for good, and `-1.0`
  reported `t`, which makes the daemon believe compaction is always due and
  rewrite every columnar table on every sweep. `NULL` behaved like `NaN`, because
  the daemon reads a NULL verdict as "not due". Both thresholds now raise
  `invalid_parameter_value` (SQLSTATE `22023`) for all four, matching
  `compact_rewrite`.

  0 and 1 remain legal on every threshold, and `test/native_reclaim.sh` now pins
  both endpoints as accepted. That is the direction a bounds fix usually breaks,
  and nothing could see it: the suite's only call at 0 was a bare `psql_run`
  whose exit status nothing read, so a rejected 0 reached nothing but the server
  log. Measured with `minFrac < 0.0` changed to `minFrac <= 0.0`: exactly one arm
  fails, the new one, and the other 32 pass. The same mutation applied to
  `compact_due_fraction` reddens the matching `maintenance_due` arm and nothing
  else.

  Every deny arm asserts the SQLSTATE, not "it failed". Four calls that satisfy
  "it failed" without touching the guard are pinned as controls: a missing
  function and a wrong-arity call (`42883`), a null table name (`22004`) and
  `1/0` (`22012`).

- A parallel export refuses a destination too long to hold the names it generates
  (#863).

  **`pgcolumnar.parallel_export_parquet` checked the destination's length not at
  all.** Worker paths cross shared memory in `MAXPGPATH` buffers and have a
  generated name appended, and `snprintf` truncates rather than failing, so a long
  destination produced short names and the export completed as though nothing had
  happened.

  Two distinct failures, both silent. A destination of 1000 bytes or more truncated
  the part names themselves: the run wrote `part-0000.parqu` instead of
  `part-0000.parquet`, stamped `_SUCCESS` beside it, and reported success for an
  export `pgcolumnar.read_parquet` does not recognise. A destination of 994 to 999
  bytes wrote correct part names but truncated the path that
  `pexport_remove_outputs` composes for its cleanup scan -- `"%s/%s"` from the same
  directory and the sink's in-flight `part-NNNN.parquet.tmp.<pid>`, which is 30
  bytes past the directory with a seven-digit pid -- so the scan unlinked a
  truncated path and left the temporary file behind.

  The destination is now probed against the longest name the file constructs,
  `"/part-%04d.parquet.tmp.%d"` at `INT_MAX` for both, which is 39 bytes past the
  directory against the final part name's 24. Anything that does not fit is refused
  with `54000` before the destination directory is created, because a truncated run
  publishes a differently named object and still stamps `_SUCCESS` over it.

  The sink itself was never at risk and is unchanged: `columnar_sink.c` builds its
  temporary name with `psprintf`, which allocates.
- A shebang and the execute bit go together, and every directory that documents
  a command is swept (#856). Two things were left over from #852.

  **The rule flagged both states, so a sourced fragment could not be correct.**
  It reddened a file that declared an interpreter without the bit, and it also
  reddened a file with no interpreter line at all. Its header documented the way
  out as "drop its shebang and say why", and that way out did not exist: dropping
  the shebang moved the file from one red to the other. `bench/cb_guards.sh` is
  the file that proved it -- sourced by `bench/run_clickbench.sh` and
  `test/bench_guards.sh`, its header saying "Sourced, not executed" since it was
  written, and unfixable under the old rule. `CONTEXT.md` had described the rule
  correctly all along, as failing "if one has either without the other"; it was
  the code that was stricter than the documented rule.

  The rule is now that biconditional, and a file with neither a shebang nor the
  bit passes.

  **That removed the exemptions.** `test/selftest/` and `test/fixtures/` were
  pruned by path because the old rule would have reddened them wholesale.
  Measured under the new one before the prune came out: `selftest/` is 31 scripts
  and every one is already correct, `fixtures/` is 14 `.sh`/`.py` of which 13 are
  the host tools this change gives the bit. Nothing is excluded now, so nothing
  is concealed.

  **`bench/` joins the population.** `docs/benchmarks.md` names five `bench/`
  scripts as bare commands. All five are executable today, so all five work:
  correct by habit with nothing checking it, which is what `test/` was before
  #852.

  **A third check, anchored on the documents, closes the hole the biconditional
  opens.** Delete a documented command's shebang and its bit and the file is
  internally consistent and still broken for a reader, so neither of the first
  two rules can see it. The third requires every script a document names to be
  executable.

- No compiled Python artifact is tracked, and the tree ignores the ones the
  interpreter writes (#854). `test/__pycache__/ste_check.cpython-312.pyc` was
  tracked. Its source, `test/ste_check.py`, was renamed to
  `test/plain_language_check.py` in `e9de048`; the `.pyc` outlived it by 146
  commits and was still tracked at `fe1f3a2`, the base of this change. So the
  file was compiled code for a module that no longer existed and that CPython
  would never open: it reads a `__pycache__` entry only when the matching source
  sits beside it.

  A tracked build artifact does not stay still. This one had already re-committed
  itself inside an unrelated logical-replication fix, where the diffstat reads
  `Bin 6028 -> 6028 bytes` and exactly two bytes differ -- the PEP 552
  source-timestamp word. The compiled code was identical either side.

  `.gitignore` gains `__pycache__/` and `*.pyc`, which it had neither of, and
  `test/selftest/` gains a part that fails when a compiled Python artifact is
  tracked or when either rule goes missing. Ignoring is not enough on its own and
  the suite proves it: restoring the tracked file with both rules in place still
  reds two checks, because `.gitignore` has no effect on a file git already
  tracks.

  `test/devloop.sh` stages its build directory with `tar --exclude=.git`, so the
  tree the suites run out of was not a checkout and the new checks reported five
  failures on a clean tree. It now writes a one-line gitfile into the build
  directory instead: 45 bytes rather than the 32 MB of copying the object
  database, and the loop stays as cheap as it was. Every check in the part also
  answers `no-repo` where there is no repository, rather than the answer git
  gives by default -- `ls-files` prints nothing, which reads as a clean tree, and
  `check-ignore` says "not ignored", which reports a present rule as missing.

- Every test script is executable, so the commands this project documents run as
  written (#852). No released version is affected: nothing in the extension
  changed, and this is test tooling only.

  103 of the 262 top-level scripts in `test/` were mode 100644, and 30 documented
  invocations named one of them. `test/temporal.sh /path/to/pg_config` and the 29
  others died with `Permission denied` before running a single statement. The
  fix gives those 103 files the execute bit.

  Every green run in this project's history is honest, and the reason is the
  point. `ci.yml:485` and `nightly.yml:188` both invoke
  `bash test/run_all_versions.sh`, and the runner starts each suite as
  `bash "$builddir/test/${s}.sh"` at lines 712 and 749. An interpreter named on
  the command line does not consult the execute bit, so a suite's own mode never
  reached the matrix. The gap was between the documentation and a reader's shell,
  and no green matrix could stand in it.

  Two situations, not one. Exactly one 100755 to 100644 transition exists in the
  whole history of `test/`: `56ae5f8eb`, a perf commit that added a line to
  `SUITES` and stripped the bit on the way past. The other 102 files were born
  100644, so one is a regression and the rest are a habit nothing contradicted.

  `test/crlf_listener.py` was the only script with no interpreter line, and the
  bit alone would have made it worse: `execve` returns `ENOEXEC`, the shell
  retries the file under `/bin/sh`, and the reader gets a syntax error instead of
  a clean refusal. It now declares `#!/usr/bin/env python3`, as its eleven
  siblings do.

  A new `harness_selftest` part pins both halves for every `.sh` and `.py` under
  `test/`. The rule is anchored on the file's own first line rather than
  on a name list or on what a document happens to mention: a script that opens
  `#!/usr/bin/env bash` has said it is meant to be run, and a mode that forbids
  running it contradicts the file itself. Run against the unfixed tree the check
  reports 102 files without the bit and 1 without a shebang, which is the
  population the issue measured, so the check is neither over nor under matching.
  Stripping the bit from `run_all_versions.sh` alone, which is what `56ae5f8eb`
  did, reddens it naming that one file.

  The sweep exempts exactly two directories. The parts in `test/selftest/` and
  the scripts under `test/fixtures/` are sourced or imported, never executed, so
  the bit would advertise a way to run them that does not work. That is this same
  defect pointing the other way. Every other directory is swept, which is what
  covers `test/pbt/run.sh`: it is a documented command (`docs/testing.md:132`)
  that lives one level down, it was already correct, and a sweep of the top level
  alone would have left the file most like this defect outside the guard.

- `docs/limitations.md` states the constant-typing rule correctly. The previous
  wording said `smallint` and `real` "always need a cast". That is false: an
  unadorned quoted literal is `unknown` and takes the column's type, so
  `smallint_col < '500'` skips. The rule is not special to the temporal types
  either. Their literals are simply always quoted, which is why they skip as
  written.

  Two limits on that rule are stated with it, because the first revision of this
  entry got both wrong. A literal that names its own type is that type rather
  than `unknown`, so `timestamp_col < DATE '2026-01-01'` does not skip, which is
  what the same page's conditions list has always said. And a digit string is an
  `integer` only while the value fits in one: `integer_col < 5000000000` is an
  `integer` column against a `bigint` constant and does not skip either.

  Measured across the nine skippable types in three literal forms, with the
  planner's own constant printed beside each skip count, and every row count
  compared with a heap oracle. Nine checks in `test/parquet_export_stats.sh` now
  pin the rule, and each can fail: three go red when the reader's
  exact-constant-type refusal is deleted, four when the writer stops emitting
  statistics, and the two plan-shape checks go red when the patterns they look
  for are swapped.

- Exported Parquet files carry row-group statistics, so a file pgColumnar wrote
  can be skipped (#850). Reported from outside the project, and the report was
  right: every condition `docs/limitations.md` documents for row-group skipping
  could hold and `Row Groups Skipped` would still be 0.

  `write_column_chunk()` emitted ColumnMetaData fields 1 through 7 and 9, and
  never field 12, so no file we wrote carried a minimum or a maximum for the
  reader to test a predicate against. The read side was never at fault. On the
  reporter's own repro, a 1,000,000-row table exported to 16 row groups: 0 of 32
  column chunks carried statistics, and the foreign scan decoded all 16 groups
  for `id < 5000`. It now carries 32 of 32 and skips 15 of 16, decoding one.
  The native chunk-group skip on the same data always worked, and is unchanged.

  What the exporter now writes, per column chunk: `null_count` always, including
  where it is zero, because `parquet.thrift` says a reader must not read an
  absent count as zero; `min_value` and `max_value` for the columns stored as
  INT32, INT64, FLOAT or DOUBLE, which is exactly the set the reader can skip
  on; and `nan_count` for the floating point columns. A `text`, `bytea`, `uuid`,
  `boolean` or byte-array `numeric` column gets the null count and no bounds:
  UTF8 sorts by unsigned bytes, which is not any PostgreSQL collation, and a
  bound in the wrong order is worse than no bound at all.

  The file footer now also carries `column_orders`, one `TYPE_ORDER` per leaf
  column. It is not decoration. `parquet.thrift` states that without it the
  meaning of `min_value` and `max_value` is undefined, and Arrow acts on that by
  discarding both, so statistics without `column_orders` would have left the file
  exactly as unskippable under pyarrow as it was before. With it, pyarrow reports
  `is_stats_set=True` on our files.

  Three rules from the specification are followed for floats, and each is pinned
  by a test: a NaN never becomes a bound, a column whose non-null values are all
  NaN gets no bounds at all, and a computed zero is written `-0.0` as a minimum
  and `+0.0` as a maximum. A value with no Parquet representation, such as an
  infinite date or timestamp, is folded to null before the accumulator sees it,
  so it counts as a null and cannot reach a bound.

  `test/parquet_export_stats.sh` covers this, reading the footer bytes with
  `test/parquet_stats.py` rather than asking a third-party library whether it
  feels like surfacing them. Eight mutations of the fix were each run against the
  suite and each turned a named check red.

  One of the eight loses rows rather than merely losing the skip: taking a date
  bound from the PostgreSQL epoch instead of the Unix one. That shift leaves a
  well-ordered interval which is simply wrong, so no guard can see it, and the
  single check standing between it and silent row loss is a predicate placed
  past the end of the data, `d > DATE '2060-01-01'`.

  Swapping the two bounds does not lose rows, which is worth knowing rather than
  glossing: it inverts the interval, and the reader already refuses to skip on an
  inverted one. `docs/limitations.md` documents that refusal, and it caught this
  mutation. The swap loses 54 checks and no rows.

  Files exported by 1.0-alpha2 or earlier carry no statistics, and nothing
  rewrites them in place. Export again to make one skippable.


- Index entries for live rows are no longer destroyed (#838). A released version
  is affected: this is present in `v1.0-alpha2`.

  `pgcolumnar_index_delete_tuples` answered nbtree from the calling backend's MVCC
  snapshot. `table_index_delete_tuples` asks a different question, whether an entry
  is dead to everyone, which heapam answers from a global visibility test. nbtree
  acts on the answer in `_bt_delitems_delete_check` by removing the items from the
  leaf page physically, inside a critical section, and nothing restores them. A row
  the current uncommitted transaction had deleted read as not live while remaining
  live to every other session, so entries for live rows were erased.

  Measured on 400 live rows given twelve committed rounds of churn on a column that
  is not indexed: an index scan reached 0 of the 400 after a `ROLLBACK` and 228 of
  400 after a `COMMIT`, while `SELECT count(*)` at shipped defaults answered 228
  through an index only scan and `SELECT count(b)` answered 400 through the columnar
  scan, on the same table in the same session. Two controls place the fault: churning
  the indexed column instead makes `index_unchanged_by_update()` false so no
  bottom up pass runs and nothing is lost, and a heap given the same hint loses
  nothing.

  Native storage carries no per row transaction id to compare against a global
  horizon, because the delete vector is a bitmap whose visibility comes from the
  MVCC catalog rows that hold it. It therefore cannot prove global deadness and now
  vouches for nothing rather than guessing. The cost is that version churn no longer
  reclaims leaf page space, which `docs/limitations.md` already described: stale
  entries are reclaimed by `REINDEX`, not removed opportunistically.

- A scrollable cursor no longer answers a backward fetch with forward rows (#842).
  A released version is affected: this is present in `v1.0-alpha2`.

  `pgcolumnar_scan_getnextslot` accepted a `ScanDirection` and never read it, so
  every call advanced forward. The fetch succeeded, with SQLSTATE `00000`; it simply
  returned rows that were not asked for. Measured on a 20 row fixture with the plan
  asserted in both arms: `FETCH FORWARD 5` then `FETCH BACKWARD 3` gave `6 7 8`
  where the heap gave `4 3 2`, and `FETCH LAST` returned nothing where the heap
  returned row 20.

  Forward is honoured, `NoMovement` returns no row, and backward is refused with
  `feature_not_supported` rather than emulated. The reader walks row groups and
  decodes vectors forwards, so scanning the other way is a feature rather than a
  correction, and inventing an answer is the one thing that must not happen on this
  path. At shipped defaults the planner gives a columnar relation a custom scan,
  which serves these cursors correctly, so the refusal is reached only where that
  path has been turned off.

- An encoded NUL no longer defeats the Iceberg traversal guard (#844). A released
  version is affected: this is present in `v1.0-alpha2`.

  `ice_has_encoded_dotdot` catches a traversal smuggled through percent encoding,
  because an http or https origin may decode a key before serving it. A `%00` in
  front of the traversal defeated it: `ice_percent_decode_once` wrote the decoded
  NUL and kept going, and everything downstream is NUL terminated string work, so
  the next pass measured with `strlen` and the scan walked with `strchr`. Both
  stopped at that byte and never reached the `../` behind it. The literal `..` guard
  beside it saw nothing either, because the segments were encoded.

  Measured over the S3 fixture with the delete path recorded as
  `%00%2e%2e/%2e%2e/%2e%2e/%2e%2e/etc/hostname`: the literal and the plainly encoded
  forms were both refused with `22023`, and the form behind an encoded NUL returned
  `58P01`, meaning it was not refused but fetched, and failed only at the escaped
  location. A NUL has no place in an object key or a path, so the encoded NUL is now
  refused on its own rather than scanned past.

- `pgcolumnar.vacuum_sorted`'s comment no longer calls `cluster()` numeric-only
  (#827 follow-up). A released version is affected: the sentence is in
  `v1.0-alpha2`, in the full script and in the upgrade script, so `\df+` prints it
  to a user today.

  It is wrong in both directions. `cluster_type_supported()` takes `boolean`,
  `smallint`, `integer`, `bigint`, `real`, `double precision`, `date`, `timestamp`
  and `timestamptz`, several of which are not numeric, and it does not take
  `numeric` at all. The extension already contradicted the sentence: a rejected
  column raises an errhint reading "Z-order clustering supports integer,
  date/time, boolean, and floating-point columns". The comment now uses that
  wording, so there is one description of the rule rather than two that disagree.

  Measured with a positive control so the deny arms are not vacuous, on a 20,000
  row table: `vacuum_sorted` accepts `numeric` and `text`; `cluster()` accepts
  `int` and rejects `numeric` with "column n of type numeric cannot be used as a
  clustering key", and rejects `text` likewise.

  The `1.0-alpha2` to `1.0-alpha3` upgrade re-issues the comment, as
  `set_options`, `expire`, `parallel_copy` and `sort_status` already do in that
  script. Without that half, a fresh install and an upgraded one disagree and
  `native_upgrade_converge` fails, which is the suite doing its job: it hashes
  `obj_description(p.oid,'pg_proc')` for every function in the schema. Verified in
  that order: 8 of 8 on unmodified main, failing on both the `1.0-alpha` and
  `1.0-alpha2` paths with only the full script corrected, and 8 of 8 again once
  the upgrade script carried it.

- `pgcolumnar.set_options` no longer writes past three stack arrays during
  `ALTER TABLE ... RENAME COLUMN` (#834). No released version is affected: the
  defect was introduced and fixed inside this cycle.

  `pgcolumnar.options` grew `ttl_column` and `ttl_interval` for retention and the
  `Anum_options_*` constants were extended to 9, but `Natts_options` three lines
  below them was left at 7. It sizes the `values`, `nulls` and `replace` arrays
  handed to `heap_modify_tuple`, which iterates `tupdesc->natts`, so a rename on a
  table with a declared `sort_by` read two slots past the end of all three and
  aborted the backend, taking the cluster into crash recovery.

  On an ordinary build the overrun lands in adjacent stack slots and is silent, so
  the whole matrix passed. `test/catalog_natts.sh` now pins every `Natts_*` constant
  against the width the live server reports, which makes it a check on every major
  rather than a sanitizer only one.

- `date_trunc(unit, ts) = 'infinity'` returns the matching row again (#836). No
  released version is affected: the defect was introduced and fixed inside this
  cycle.

  The equality preimage builds the bounded interval `k >= lo AND k < hi`. Interval
  arithmetic saturates at an infinity instead of raising, so `hi = lo + step`
  returned `lo` again and the interval was empty, excluding the row it was built to
  select. Measured on a 5000 row fixture holding exactly one `+infinity` and one
  `-infinity` row, at default settings: the heap returned 1 and the columnar table
  returned 0 for both, while a bare `ts = 'infinity'` returned 1 from both.

  Equality now declines when the interval degenerates, which costs a scan rather
  than an answer. The ordered operators keep their exemption and are unaffected,
  because each emits one key, so `lo == hi` costs them nothing. That is why the two
  infinity arms already in `test/preimage_rewrite.sh` stayed green while equality was
  wrong: the suite carried infinity arms, and carried equality arms, and never their
  intersection.

- `sum(bigint)` and `avg(bigint)` no longer accumulate across a rescan (#840). No
  released version is affected: the defect was introduced and fixed inside this
  cycle.

  With `pgcolumnar.enable_ungrouped_vector_agg` on, a re-executed aggregate node
  added to the previous execution's running total. There were two accumulator reset
  sites and they had drifted: `pgcolumnar_agg_specs_reset` cleared nine fields
  including `spec->i128sum`, and `PgColumnarReScanAggScan` open coded the same loop
  over eight and never cleared it. The int8 kinds accumulate there, so on a rescan
  that total was never zeroed. Measured over four `LATERAL` iterations with the node
  asserted chosen and asserted rescanned: 800022400060000, then 1600044799119997,
  then 2400067196179988, where every true answer was near the first.

  The second copy is deleted rather than corrected, so the next field added to
  `PgColumnarAggSpec` is safe by construction rather than by remembering two places.

- The documentation is brought back into line with the code, and the version
  check now covers the file that had drifted furthest (#753 follow-up).

  `README.md` said the version marker was `1.0-alpha` while `VERSION` said
  `1.0-alpha3`, two versions behind. It drifted because `test/docs_style.sh`
  compared `VERSION` against `CHANGELOG.md` and `docs/` and did not read
  `README.md`, so the only file that was wrong was the one file the check could
  not see. `README.md` is now in that comparison, and the badge is corrected and
  resized for the longer string.

  Corrected against the code: `docs/how-to.md` said `cluster` takes numeric
  columns only, where the code accepts boolean, integer, floating-point, date
  and timestamp keys and rejects `numeric` and `text`. `docs/installation.md`
  said `DROP EXTENSION` removes columnar tables, where a plain `DROP EXTENSION`
  fails while they exist and only `CASCADE` drops them. `docs/administration.md`
  said there is no cache of decompressed chunk groups, where the fetch cache
  holds four decoded row groups per backend per statement under a 32 MB cap.

  Brought up to date: `docs/roadmap.md` listed five shipped features as planned,
  all five of their issues closed, and repeated that we read local files only.
  `docs/configuration.md` did not document `ttl_column` or `ttl_interval`.
  `docs/features.md` omitted `pgcolumnar.expire` and the `dedup` argument of
  `pgcolumnar.parallel_copy`. `docs/how-to.md` gains a retention recipe, so the
  feature is reachable by task and not only by name.

### Added

- `pgcolumnar.expire` drops row groups whose rows are all older than a declared
  retention, without reading or rewriting them (#403 item 5a). Declare the
  retention with `pgcolumnar.set_options(..., ttl_column => 'ts',
  ttl_interval => '90 days')`; both halves are needed, and either alone means no
  retention.

  This is the tractable half of the paper's "merge-time data transformation".
  The rewrites already retire whole row groups: `pgcolumnar.compact` drops every
  group that is fully deleted. Retention is the same operation with a different
  predicate, and the zone map already holds what decides it, so the decision is a
  catalog read. Nothing is decoded and nothing is rewritten, and it holds only
  `ShareUpdateExclusiveLock`.

  **It is called by name and never runs on its own.** It deletes rows, and an
  operation a user runs for maintenance must not do that silently, so it is not
  wired into `VACUUM`, `compact` or autovacuum. A table with no declared
  retention raises an error rather than reporting that it did nothing.

  A group is kept whole or dropped whole. A group holding rows on both sides of
  the cutoff is kept, so retention is approximate at the group boundary and errs
  toward keeping data. Measured on 5,000 rows in 5 groups with 1,440 rows past a
  three-day retention: one group dropped, 1,000 rows removed, and all 3,560 rows
  still inside the retention kept, including the 440 expired rows sharing the
  straddling group.

  The retention column must be `timestamp` or `timestamptz`.

- `pgcolumnar.parallel_copy` can refuse a load it has already taken, with
  `dedup => true` (#403 item 7). A load that commits, whose acknowledgement the
  client never receives, is retried and the rows go in twice: measured, the same
  file loaded twice gives 100,000 rows and then 200,000.

  With `dedup`, the SHA-256 of the file is recorded in the new
  `pgcolumnar.load_fingerprint` catalog after a successful load. A later load of
  the same contents into the same table stores nothing, returns 0, and raises a
  `NOTICE` rather than failing. A file whose contents changed is a different load
  and is stored, even at the same path.

  It is off by default, because discarding rows a caller asked to store is not
  ordinary `INSERT` behavior.

  The unit is the whole load rather than a "part". `parallel_copy` is atomic
  through 2PC, proved by a malformed row at line 50,001 leaving 0 rows and 0
  prepared transactions, so parts never commit independently and a part hash
  would deduplicate nothing that is not already all-or-nothing.

  The check runs after every worker has prepared and before anything commits,
  which is the only point at which a repeat can be refused without charging every
  load for it. A refused load therefore does its work and discards it. The
  alternative, hashing before dispatch, costs 42% of a 264 MiB load; as
  implemented the fingerprint has no measurable cost, because the coordinator
  computes it while the loaders are already reading the same file.

  Three limits, all stated in `docs/sql-reference.md`: the fingerprint is
  recorded after the data commits, so a crash between them leaves data that a
  retry will store again; two identical loads running at once both store, because
  each checks the record before either writes it; and a refused load still reads
  and parses the file.

### Fixed

- The zone-map survival estimate reads the row-group geometry a table was
  **written** with, so a plan's cost no longer moves with the planning session's
  `pgcolumnar.stripe_row_limit` (#817). This is the half #806 left open; it fixed
  the index-fetch penalty and deliberately declined the same substitution here.

  `pgcolumnar_zonemap_survival` asked `pgcolumnar_effective_stripe_row_limit`,
  which answers "what limit would a write in THIS session use". For a table with
  no per-table option, that is the session GUC. A table written at 2,000 rows per
  group and planned from a session at the 150,000 default was therefore modelled
  as holding `ceil(20000/150000) = 1` group, the one sampled group survived, and
  the discount vanished. Measured on one unchanged table, varying only the
  plan-time GUC:

  | plan-time `stripe_row_limit` | before | after |
  | --- | ---: | ---: |
  | 150000, the default | 306.00 | 61.20 |
  | 2000, the written value | 61.20 | 61.20 |

  A 5x swing on a table that did not change, and at the default the pruning
  predicate was priced identically to a full scan of the same table -- pruning had
  left the cost model entirely rather than merely drifting.

  **Why this is now safe, and was not before.** #806 declined it because the
  substitution then moved `native_zonemap_narrow` from wide=30/narrow=30 to
  wide=570/narrow=66, zone-map reads that scale with table width. That was the
  width-blind whole-group probe in the estimator, which #821 replaced with a
  per-column probe. The same substitution now measures **wide=40/narrow=40**,
  exactly width-independent.

  Planning-time zone-map fetches go from 1 to 10 on that fixture. The 1 was never
  a saving: it was the estimator examining a single group because it believed a
  20,000-row table held one. Ten is the estimator sampling the ten groups that
  exist, bounded by `PGCOLUMNAR_PRUNE_SAMPLE_GROUPS` and charged per predicate
  column rather than per table column.

  `test/doc_parallel_premise.sh` carried `SET pgcolumnar.stripe_row_limit = 20000`
  as a workaround for exactly this. It is **removed**, which makes that suite a
  removal proof: its cost-ordering check now holds only because the estimator
  reads the written geometry.

- `pgcolumnar.row_group.group_number` is documented as **one-based**, which is what
  it has always been (#817). The column comment said "0-based row group ordinal".
  A group number is the stripe id reserved from the metapage when the group began
  buffering, and `PgColumnarInitMetapage` starts `reservedStripeId` at 1, so there
  is no group 0 on any storage. Verified rather than reasoned: on a 27-group table,
  `row_group` and `zone_map` both report `min = 1, max = 27` over 27 distinct
  numbers.

  This is a source comment, not a catalog one -- there is no `COMMENT ON` for the
  table, so nothing reads it at runtime and `native_upgrade_converge`, which
  compares `col_description`, cannot see it. It is corrected because a reader
  believed it: the planner's zone-map sample walked `[0, ngroups)` and paid for it
  in the defect fixed immediately below.

- The planner's zone-map sample is one-based, so it no longer under-prices a
  scan for matching the **newest** rows (#817).

  `pgcolumnar_zonemap_survival` samples row groups and asks the reader's own skip
  predicates how many survive. It walked `g = i * ngroups / nsample` for `i` in
  `[0, nsample)`, so it sampled `[0, ngroups)`. A row group number is the stripe
  id reserved from the metapage, and the metapage starts `reservedStripeId` at 1:
  **group 0 exists on no table.** The first probe was always spent on a number
  that could not exist, and group `ngroups` was never probed at all.

  The wasted probe was harmless -- an absent group narrows the sample rather than
  biasing it. The missing one was not. When the group count fits in the sample
  target the loop is a census, and it was a census that omitted the newest group
  every time, so the same predicate was priced differently according to where in
  the table its groups sat. Measured on ten groups of 2,000 rows, one clause
  each, identical row estimates, the only difference being position:

  | predicate | groups matched | before | after |
  | --- | --- | ---: | ---: |
  | `c1 > 8000` | 5..10, the six newest | 166.89 | 180.24 |
  | `c1 <= 12000` | 1..6, the six oldest | 200.27 | 180.24 |
  | `c1 > 17000` | 9,10, the two newest | 33.38 | 60.08 |
  | `c1 <= 4000` | 1,2, the two oldest | 66.76 | 60.08 |

  Exactly half, in the narrow pair. The under-priced half is the recency
  predicate this engine is aimed at: on batch-loaded time-series, `WHERE ts >
  now() - interval '1 hour'` selects the groups the sample never looked at.

- The planner's zone-map sample reads only the predicate columns, as the executor
  already does (#817).

  It called `PgColumnarReadZoneMapList`, which keys on `(storage_id,
  group_number)` against the four-column `zone_map_pkey`, so it fetched every
  column's and every vector's row from the heap and then used one column's.
  `pgcolumnar_native_group_can_match` has asked the per-column question with a
  three-key probe since #314; the estimator now asks it the same way, through the
  same `PgColumnarReadZoneMapForColumn` and the same session cache. Planning-time
  `zone_map` fetches on a 30-column table go from 540 to 10, and no longer scale
  with table width: 30 columns and 2 columns both read 10.

  The estimator holds a #744 read session of its own, so it resolves `zone_map`
  once for the whole sample rather than once per probe. Its `DEBUG1` report is
  labelled `zone map estimate:` where a scan's stays `zone map read:`, because
  the two interleave in one backend's log and `native_zonemap_session` counts
  scan reports to prove that a scan around an aborted one opens for itself. A
  scan's line is byte-identical to what #744 shipped.

  **The two halves could not ship apart.** Fixing only the one-based sample makes
  the whole-group probe reachable at the default `stripe_row_limit`, where the
  single wasted probe had been hiding it, and `native_zonemap_narrow` correctly
  reddens at wide 90 against narrow 34.

- `PgColumnarReadZoneMapForColumn` no longer discards the index oid it just
  cached (#817). #744 resolves `zone_map_pkey` once per read session and stores
  it; an unconditional second `pgcolumnar_index_oid("zone_map_pkey")` stood
  immediately before `systable_beginscan` and overwrote both that value and the
  no-session branch's own lookup. The session's `opens` counter never noticed,
  because it counts relation opens, which really were saved. Shown by poisoning
  the cached value to `InvalidOid`: with the dead store present the poison is
  inert (40 index fetches, 0 sequential scans, identical to the unpoisoned
  control), and with it removed the poison reaches the scan and forces 20
  sequential scans of `zone_map`. A dead store draws no compiler warning, which
  is how it survived.

- The cost model reads the row-group geometry a table was **written** with,
  not the geometry a write in the planning session would produce (#806).
  `pgcolumnar.storage.row_group_limit` records what the writer used and nothing
  read it back; the index-fetch penalty called
  `pgcolumnar_effective_stripe_row_limit()`, which returns the per-table option
  or else the planning session's `pgcolumnar.stripe_row_limit`.

  The consequence is larger than a mis-costed table. A session that sets that
  GUC -- before a bulk load, say -- repriced every columnar table it then
  planned against, including tables it never touched. Measured on one unchanged
  table of three row groups, varying only the GUC at plan time:

  | plan-time `stripe_row_limit` | before | after |
  | --- | ---: | ---: |
  | 150000 | 775.26 | 775.28 |
  | 20000 | 113.26 | 775.28 |
  | 5000 | 36.26 | 775.28 |

  A 21x swing on a table that did not change. `R` is not a minor term: it sets
  the group count, the pages per group, the per-group decode CPU, and the
  `decodedWidth * R` test against the 32 MB fetch-cache cap, so it can be wrong
  in either direction there.

  **One call site changed**, the index-fetch penalty, which is where the defect
  was measured. Two others deliberately did not:

  - `columnar_write_state.c` still asks `pgcolumnar_effective_stripe_row_limit()`,
    because a writer deciding the geometry it is about to lay down is exactly
    what that function answers;
  - `pgcolumnar_zonemap_survival()` also still asks it. Substituting the written
    geometry there moved `native_zonemap_narrow` from 30/30 zone-map reads on a
    wide and a narrow table to 570/66 -- reads that scale with table width,
    which is the property that suite exists to hold. There is no measured defect
    at that site and there is a measured regression from changing it.

  An explicit per-table `stripe_row_limit` still wins over the written geometry.
  This is about the planning session's GUC, which has nothing to do with the
  table; a per-table option is a durable statement by its owner, and
  `native_index_fetch_stripe_cost` asserts the cost model can see it.

  Two things this leaves, stated rather than hidden. Because the survival
  estimate still reads the session GUC, a plan-time limit far enough below the
  written one can still flip a plan through that path: at 5000 the table above
  costs 87.79 rather than 775.07, because it stops being an index scan. And the
  recorded limit is one number, the last writer's, so a table whose groups were
  written under changing limits is still approximated -- the exact quantity is
  the real group count, which would be a catalog scan proportional to the number
  of groups on every plan.

  New suite `test/cost_written_geometry.sh`. It asserts planner costs rather
  than timings, so `PGC_SKIP_TIMING` does not drop it.

### Changed

- Chunk-group skip predicates are now evaluated most-selective-first, so a group
  that is going to be pruned is no longer probed through every other predicate's
  column on the way there (#403 item 4). The skip loop returns on the first
  predicate that excludes, and each predicate's first use in a group fetches that
  column's zone map from the catalog, so the order decides what a pruned group
  costs.

  The order used was the order the ScanKeys arrived in, which is attribute order.
  Measured on 100 row groups with one predicate excluding 99 groups and one
  excluding none: 200 zone-map probes, and writing the selective predicate first
  in the query did not change that, because query order is not ScanKey order. The
  order was arbitrary with respect to selectivity rather than merely suboptimal.
  After: 102 probes, with the same 99 groups pruned.

  The reordering is a transpose on each exclusion rather than a sort: O(1), no
  statistics the reader does not already have, and it converges in one step for
  one selective predicate behind an unselective one. The paper's caveat, that
  ordering should apply only when a highly selective predicate is present, falls
  out of the mechanism: a predicate that never excludes never moves, so a query
  with nothing selective keeps the order it started with and pays nothing.

  This cannot change an answer. The predicates are a conjunction, and the loop
  returns on the first exclusion whichever one that is. `EXPLAIN (ANALYZE)`
  reports `Columnar Zone Map Probes`, which is the only line that moves.

- The vectorized grouped aggregate now sizes its hash table from the group
  estimate the planner already made, instead of growing it from nothing
  (#403 item 6). The table is open-addressing and doubles at 70% load from
  capacity 0, so a query with 200,000 groups walked 1024, 2048, ... , 524288 and
  rehashed every live entry at each step. Measured on 2,000,000 rows with 200,000
  groups: 10 allocations and 366,280 entries rehashed before, 3 allocations and
  46,591 after, with identical answers.

  The table still starts at 1024 and is sized only once the data has proven it
  must grow, and a grow jumps toward the estimate but never past 64 times what
  the table has proven it holds. Both bounds are expressed in the live count
  rather than in a memory budget, which is the point: an earlier version of this
  change bounded the allocation by `work_mem` and so allocated 131,072 entries
  for a query with 47 real groups, because `estimate_num_groups` cannot see
  through a function and `GROUP BY date_trunc('day', ts)` reaches the node with
  an estimate of every row. A query whose groups fit in the starting 1024 never
  allocates more than it would on unpatched code. Under-estimating costs nothing
  new.

  `EXPLAIN (ANALYZE)` reports `Columnar Group Table Entries Rehashed`, the work
  done, alongside the allocation count and the table's peak capacity, which is the
  memory cost. Entries rehashed
  rather than resizes because the two disagree: the first instrument counted
  resizes and reported that sizing removed 7 of 10 while it removed about a
  quarter of the rehashing, the work being dominated by the last two steps.
### Fixed

- `test/build_all_versions.sh` now reports how many majors it built and refuses
  a run that built none (#809). Its verdict read only the `failed` flag, which
  only a build that RUNS and FAILS sets, so a `pg_config` that is not executable
  was skipped without touching it. A run where every major was skipped reached
  the end with `failed=0` and printed `PASSED` with exit 0, having invoked no
  compiler.

  That is not hypothetical: the default list is `/usr/local/pg15`, `pg16`,
  `pg17`, `pgsql` and `pg19`, and a machine whose builds live elsewhere skips
  all five. It was recorded as a green five-major preflight for a pull request
  and caught by reading the body above the verdict rather than by any check.

  The script now prints `built N of M` before its verdict, in the same shape
  `test/run_all_versions.sh` already uses for `versions run: N of M configured`,
  and fails on zero with the invocation that names the paths. A PARTIAL run is
  deliberately left passing: whether three of five present should fail or warn
  is a judgement about how people run this, and the count makes it visible
  either way. A new `harness_selftest` case pins all three behaviours, since
  nothing covered this script before.

### Added

- `pgcolumnar.sort_status` reports `sorted_kind`, so the reporter can finally
  express the distinction the catalog has recorded since #758 (#761). It holds
  `lexicographic` after `pgcolumnar.vacuum_sorted`, `zorder` after
  `pgcolumnar.cluster` and `pgcolumnar.recluster`, and NULL when the table was
  never ordered or was ordered before the column existed.

  `sort_key` names the columns and says nothing about the arrangement, and a
  Z-order over two or more columns is not a sort on any one of them. The
  documented way to read the kind was to select it from `pgcolumnar.storage`
  directly. That table carries no `GRANT`, so only a superuser could follow that
  advice, while `sort_status` is SECURITY DEFINER and gated on
  `require_caller_select` and is therefore available to a table's own owner.

### Changed

- The extension's `default_version` is now `1.0-alpha3`, and
  `pgcolumnar--1.0-alpha2--1.0-alpha3.sql` ships with it. Adding an OUT parameter
  changes a function's signature, so #761 cannot be a `CREATE OR REPLACE`; it is
  the change that opens this cycle. Every other `[Unreleased]` entry so far is
  shared-library only and needs no catalog change.

  `test/native_upgrade_converge.sh` now exercises both `1.0-alpha` and
  `1.0-alpha2` as starting points, and asserts that each reaches a catalog
  identical to a fresh `1.0-alpha3` install in function definition, ACL and
  comment, relation kind, column type and comment, non-base types, and foreign
  data wrappers. `1.0-alpha` reaches it by the chain through `1.0-alpha2`.

### Fixed

- The index-fetch penalty now charges decode CPU by projected column WIDTH, not by
  column count (#803). This is the same flatness #768 fixed for the sequential
  scan, in the other cost site. `pgcolumnar_index_fetch_penalty`'s CPU term was
  `cpu_operator_cost * R * nproj`, a count, so a 68-byte text column was charged
  exactly what a 4-byte int4 column was.

  Measured on two tables of identical shape whose columns differ only in type, so
  the decoded prefix is four columns on both arms and only the bytes move. Read
  out of an instrumented build, the decode CPU term was **200.00 on both arms**
  across an 8.6x difference in decoded width (16 B against 138 B). The same
  ~39,000-row index fetch took 4,385 ms on the narrow table and 88,352 ms on the
  wide one, a 20.15x difference against a modelled 1.49x. Cost per millisecond
  spanned 13.4x, inside the 13x-22x #768 measured for the scan path.

  The consequence was an inverted ordering rather than a uniform under-charge. A
  wider decoded prefix makes every row-group decode dearer, so a wide table should
  abandon per-row fetches sooner; priced by count it did the opposite. The model
  kept the index up to 120-161 rows on the wide table and only 40-60 on the narrow
  one, while the measured crossovers are 40-120 wide and 120-279 narrow. At 120
  rows the wide table's chosen index plan measured 239.6 ms against an 82.9 ms
  scan.

  `pgcolumnar_scan_decode_shape` now accumulates decode units over the prefix it
  already walks, using the same reference width as the scan's
  `pgcolumnar_projected_decode_units`, and the penalty charges those units.
  Substituting `pgcolumnar_projected_decode_units` itself would have been wrong:
  it sums the columns a query *references*, while the fetch decodes the *prefix*
  up to the highest referenced column (#363), and the two differ on exactly the
  shape #363 exists for.

  An all-int prefix is priced exactly as before, because a 4-byte column is one
  unit: the narrow arm's flip point is unchanged at 40-60 rows. The wide arm moves
  to 7-15. Both arms now stop earlier than the measured crossover, which is the
  direction the model already erred on the narrow arm before this change; the
  #376 bound and the clustered-index and point-lookup guards in
  `test/analyze_stats.sh` are unaffected and still pass.

  `test/index_fetch_penalty_width.sh` pins the ordering from the plan rather than
  the clock, so `PGC_SKIP_TIMING` does not apply to it. On the unfixed build its
  seven premises pass and the ordering check fails with
  `INVERTED (wide 138B fetches to 120 rows, narrow 16B only to 40)`.

- `pgcolumnar_fetch_group_slot`'s header promised a NULL return that neither the
  function nor its callers implement, and following it segfaults (#795). The
  comment read "Returns NULL when nothing should be cached, in which case the
  caller decodes into its own scratch context exactly as before". There is no
  such path. The function has two returns, `return e` on a hit and
  `return victim` on a miss, and `victim` cannot be NULL: the branch that runs
  when every slot is live picks a least-recently-used entry. Both callers
  dereference the result immediately, one through `entry->firstRowNumber` in the
  geometry check and one through `MemoryContextSwitchTo(entry->cx)`, neither
  guarded.

  So the sentence did not describe an unimplemented option, it described a trap.
  Forcing the documented return on an assert build of PostgreSQL 18.4 produced
  `signal 11: Segmentation fault` in a backend. Nothing in `main` reaches it --
  the function never returns NULL today, and the same fixture on a clean build is
  correct -- but the comment invites the change that does.

  The comment now states the guarantee the code makes, that a slot always comes
  back and a miss returns a reset entry for the caller to fill, and records that
  a "do not cache this one" policy needs a scratch-context path in the callers
  before it can be expressed as a NULL. An `Assert(entry != NULL)` at each call
  site holds that to be true rather than leaving it to the comment, which is what
  rotted. With the Assert in place the same injected NULL return reports
  `TRAP: failed Assert("entry != NULL"), File: "src/columnar_reader.c", Line:
  3888` instead of faulting, so the next person to try it gets the line rather
  than a core file.

- The columnar scan's decode cost is now charged by projected column WIDTH, not
  by column count (#768). #503 gave the scan a per-value decode term, which
  fixed "nine columns are priced like one"; it counted columns, so a 324-byte
  text column was charged exactly what a 4-byte int4 column was. Measured on
  4,000,000 rows, four 324-byte text columns were priced at 163,422 while taking
  5875 ms, against 206,524 and 335 ms for eight int columns: priced below, and
  17.5x slower. Cost per millisecond across those projections spanned 22x before
  the change and 1.7x after it, with an int-only projection priced exactly as
  before.

- The shared test cluster no longer sets `pgcolumnar.unique_lock_buckets`
  (#799). `test/lib.sh` wrote `100003` into the cluster nearly every suite runs
  against, where the shipped default is `128`, so the whole tree ran 781x above
  shipped behaviour to serve one suite that already sets the value on the
  cluster it builds itself. The visible symptom was a 20,000-row insert into a
  columnar table with a unique index failing with `out of shared memory` and a
  hint to raise `max_locks_per_transaction`, which reads as a product defect and
  is not one. A new harness selftest keeps the shared cluster config free of
  `pgcolumnar.*` GUCs; `PGC_EXTRA_CONF` remains the per-suite mechanism.

- The `one/many` fetch cost guard in `test/native_fetch_cache.sh` now rebuilds
  its fixture before every reading, and asserts the group geometry at each one
  (#801). The guard could not fail. The `UPDATE` it times does not rewrite the
  row group it reads: it marks the rows deleted and appends them as a new group,
  so `fc_one` goes from one group of 20,000 rows to that group plus one of
  2,000, and from the second reading on the rows it targets sit in the small
  appended group. That is the geometry the `fc_many` control has, so the arms
  stop differing after one reading, and `min3` returned one of the cheap
  post-rewrite readings every run. Measured against a build whose fetch cache
  retains nothing, so every fetch re-decodes: the guard read 0.98x and PASSED,
  where rebuilding reads 5.89x and 6.13x on two runs and fails as it should. The
  `#353` guard in the same file fails correctly on that build, which is what
  shows the ablation was real. A per-fetch decode counter says it in rows rather
  than milliseconds: `fc_one` decodes 40,000,000 rows on the first reading and
  4,000,000 on the next two, and 40,000,000 on all three once the fixture is
  rebuilt.

  The new premise records the group count at each reading. Both faults it
  covers were proved by mutation, each changing one thing: dropping the rebuild
  records `1 2 3`, and an `INSERT` that populates nothing records `0 0 0`. The
  timing check passed in both mutants, at 0.77x and 1.14x, so the premise is
  what separates either fault from a green suite.

- The fetch cache cost guards in `test/native_fetch_cache.sh` now assert that
  they reach the per-row fetch path, and set
  `pgcolumnar.enable_index_fetch_penalty = off` so that they do (#797). They had
  not exercised the cache since the penalty landed: `enable_seqscan = off` and
  `enable_bitmapscan = off` do not disable the columnar custom scan, so the
  planner answered these queries with a group scan and the guards timed a
  different mechanism while staying green. The #353 guard was written 83 minutes
  before the penalty existed. With the path restored the two guards read 2.00x
  and 6.42x, against 0.98x and 1.21x before, and the 6.42x reproduces the 5.8x
  recorded in the suite's own comment when the guard was written.

- `sum()` and `avg()` over `numeric` no longer take the ungrouped vectorized
  path, where they were slower than the ordinary `Agg` (#785). This closes the
  last of the three families `pgcolumnar.enable_ungrouped_vector_agg` names.
  `bigint` was fixed by giving it a cheap accumulator (#786); `numeric` cannot
  have one, because its input already carries a scale.

  Measured on 8,000,000 rows, interleaved, minimum of seven, plan and answers
  asserted on both arms:

  | query | before | after |
  | --- | ---: | ---: |
  | `sum(numeric)` | 1.10x slower | **1.04x faster** |
  | `sum(float8)` | 2.73x faster | 2.74x faster |
  | `sum(numeric), sum(float8)` | 1.00x, no gain | 1.01x, unchanged |

  **The refusal is unconditional, and the mixed case is why.** Classification is
  all or nothing, so refusing `numeric` refuses the whole node, and the obvious
  worry is that a mixed query loses the float win with it. It does not. Measured
  on one cluster with only the installed library changing, a mixed query was
  being actively **harmed**:

  | query | numeric admitted | numeric refused | |
  | --- | ---: | ---: | ---: |
  | `sum(numeric)` | 391.0 ms | 351.0 ms | 1.11x faster |
  | `sum(numeric), sum(float8)` | 412.4 ms | 367.6 ms | 1.12x faster |
  | `sum(float8)` (control) | 78.8 ms | 77.2 ms | flat |

  So the mixed case is an argument for the refusal rather than a cost of it.

  It is also not conditional on the data. The penalty holds whether or not
  chunk-group pruning happens: 1.14x with 25 of 27 groups removed and 1.56x with
  none.

  The refusal is placed where the scan-fold path is chosen rather than in
  `pgcolumnar_classify_aggref`, which is shared with the grouped path. The
  grouped path is a different implementation and has not been measured for these
  kinds, so this narrows only what was measured.

### Changed

- `test/native_fetch_cache.sh`'s three cost guards now assert through
  `check_ratio` instead of computing the comparison in shell and passing
  `yes`/`no` to `check`. They were hand-rolled while they used `check_timing`,
  which takes a scalar; once #792 made them ordinary checks the ratio helper
  became available, and it is strictly better.

  `check_ratio` refuses a zero on **either** side. The hand-rolled form guarded
  only the denominator, so a numerator timing at 0 ms, which means the
  measurement fell below timer resolution, passed silently as a ratio of zero.
  Demonstrated in both directions with the same injected reading:

  ```
  check_ratio:  FAIL  ... a side of the ratio is zero, so nothing was measured: a=[0] b=[26]
  hand-rolled:  PASS  ...
  ```

  It also compares as a float rather than truncating integer division, and
  prints the ratio with both sides, so a verdict now reads
  `(1.39x, bound 3x, from a=39 b=28)` rather than a bare `yes`.

  One boundary moves by a hair and the comment says so: the old form failed at a
  ratio of exactly 3.0 and `check_ratio` fails above it. Nothing measured on
  these shapes is near that.

### Changed

- `test/native_fetch_cache.sh`'s three cost guards now run in CI. They were
  written with `check_timing`, which `PGC_SKIP_TIMING` drops, and that suite is
  not in `is_timing_suite` -- so the suite ran, reported `PASSED`, and skipped all
  three, including the two that guard named regressions (#353, #359). Three cost
  guards in no automated gate (#792).

  They were already the exempt shape by `test/cancel_decode.sh`'s argument: two
  readings taken back to back in the same run, which move together under load.
  But that argument has a second half -- *"the best of three readings, not the
  average ... an average would let one descheduled run widen the ratio on its
  own"* -- and these took a single reading per side. Measured on six busy cores
  of an eight-core box before the change, the `one/many` ratio reached **2.39
  against its bound of 3** on one run. Unguarding them as they stood would have
  traded a check that is skipped everywhere for one that is flaky somewhere.

  So each side is now the minimum of three, and then they are ordinary checks.
  Under the same load afterwards, four runs: `one/many` 0.84 to 0.93, `wide/small`
  0.98 to 1.00, `over/under` 1.18 to 1.23, against bounds of 3, 5 and 12. The
  suite costs about 0.9 s more (4.4 s to 5.3 s, build excluded).

  A repeated measurement must be idempotent, and this one was not: `upd_ms`
  updated with `v = v + 1`, which is correct once and wrong three times. The
  suite's own correctness arms caught it -- they assert `v = id + 1` -- so the
  update now sets `v = id + 1`, which is the same work per row and true after any
  number of runs.

  `check_timing`'s skip message said "wall-clock ratio" on a helper that takes a
  scalar, so it misdescribed every skip it printed. Its one remaining caller,
  `test/native_cancel.sh`, is in `is_timing_suite`, so the driver names that suite
  as skipped rather than reporting a pass over a dropped subject.

- `sum()` and `avg()` over `bigint` now take the batch fold, which closes the
  filtered case that #786 left behind (#755 question 3). Before this, a filtered
  `sum(bigint)` on the vectorized path was **slower** than not using it, while
  `sum(float8)` on the same fixture was more than twice as fast. That asymmetry
  was the whole evidence for the question, and it was the batch fold rather than
  the aggregate: the int8 kinds folded row at a time while the float kinds folded
  column at a time.

  Measured on one cluster with only the installed library changing, 8,000,000
  rows, values chosen to defeat run-length and dictionary encoding so the fold is
  not answered from encoded runs, minimum of seven, answers identical:

  | | Before | After |
  | --- | ---: | ---: |
  | `sum(bigint)` | 133.5 ms | **92.0 ms** |
  | `sum(bigint) WHERE ...` | 228.2 ms | **99.4 ms** |
  | `avg(bigint) WHERE ...` | 226.2 ms | **98.7 ms** |
  | `sum(float8) WHERE ...` (control) | 85.2 ms | 86.6 ms |

  The float control does not move, which is what says the change is specific to
  the int8 kinds.

  It is a two-line change because #786 did the work: the fold reads a column at a
  time and then accumulates per value, so a kind is foldable when its accumulate
  is cheap, and since #786 the int8 kinds accumulate into an `int128` and
  allocate nothing. This does **not** make them parallel-eligible.
  `pgcolumnar_parallel_agg_ok` is a separate predicate with its own callers, and
  the int8 kinds stay out of it: an `int128` running total is not a partial state
  a core `Finalize` can combine.

  `sum`/`avg` over `numeric` are unchanged and still slower on this path. They
  cannot use an integer accumulator, and that residue stays on #785.

### Changed

- `check_ratio_timing` is renamed to `check_ratio_needs_quiet_machine`, and the
  distinction it encodes is now written where it is decided (#787). The helper was
  named for what it measures, a ratio of timings, rather than for what it does:
  both `ci.yml` and `nightly.yml` set `PGC_SKIP_TIMING`, so a check written with it
  runs in no automated gate at all.

  Two kinds of timing assertion need different treatment and only one needs that.
  A ratio against an absolute or cross-run baseline can be distorted by a loaded
  machine. A ratio whose two arms are measured back to back in the same run and
  compared by minimum cannot, because both readings move together under load, and
  `test/cancel_decode.sh` had argued exactly that for its own ratio and run in CI
  on the strength of it. An author holding the safe shape reached for the name
  that matched their units and lost the check silently, which is how #786's guard
  came to be skipped everywhere until it was moved to `check_ratio`.

  No behaviour changed. The two call sites, both in
  `test/planner_choice_quality.sh`, keep the guard they had; that suite is in
  `is_timing_suite`, so the driver already names it as skipped rather than
  reporting a pass over a dropped subject. What changed is that the reasoning now
  sits beside the helper rather than being re-derived in three suite headers --
  `bloom_sizing` for a size, `native_fetch_bigcap` for buffers, `cancel_decode`
  for a same-run ratio -- which is the recurrence #253, #254 and #764 each fixed
  for one suite.

- The documentation style gate is now based on ISO 24495-1:2023, *Plain language
  - Part 1: Governing principles and guidelines*, in place of ASD-STE100. The
  project's writing rules cite two standards and no others: ISO 24495-1:2023 for
  language, and ISO 82079-1 for the structure of instructions.

  **No check changed.** The gate enforces the same four rules over the same
  files, and `docs_style` runs the same nine checks. What changed is the basis
  each rule is attributed to, and the honesty of that attribution:

  - the 25-word sentence limit and the idiom list come from the standard's
    "understandable" principle;
  - the two dash rules are this project's typographic house rules and are now
    labelled as such, because ISO 24495-1 does not ask for them.

  The 25-word figure is named as this project's measurable proxy rather than a
  number quoted from the standard, which does not give one. `test/ste_check.py`
  is renamed to `test/plain_language_check.py` so the file does not assert a
  standard the project no longer cites.

  Conformity is still not claimed, and the reason is now the accurate one. Only
  one of the four governing principles has mechanically checkable content, and
  the standard's own test for another is that a reader acts on the document
  successfully, which no checker performs.

### Fixed

- `sum()` and `avg()` over `bigint` are no longer **slower** on the vectorized
  path than off it (#785). `pgcolumnar.enable_ungrouped_vector_agg` describes
  itself as the fast path for "sum/avg over int8/float/numeric", and for `bigint`
  it was a pessimisation: 2.03x slower for `sum`, 1.91x for `avg`.

  The path converted every value to `numeric` and called `numeric_add` per row,
  which is two allocations and a full numeric addition for each row. A profile of
  it was 13.0% `make_result_opt_error`, 9.3% `add_abs`, 8.1% `init_var_from_num`
  and 8.5% `AllocSet` alloc and free: the numeric machinery, not the aggregate.
  PostgreSQL's own `bigint` accumulators are 128-bit and convert once, and this
  now does the same.

  Measured on 8,000,000 rows, interleaved, minimum of seven, with the plan and
  the answer asserted on both arms:

  | | Before | After |
  | --- | ---: | ---: |
  | `sum(bigint)` | 2.03x slower | **1.65x faster** |
  | `avg(bigint)` | 1.91x slower | **1.74x faster** |

  `float4` and `float8` were already large wins on this path (3.18x and 2.88x
  here), so the setting is now a gain for three of the four families it names.
  `numeric` remains about 1.14x slower and is unchanged by this: its input is
  already `numeric` with a scale, so it cannot use an integer accumulator. That
  residue is recorded on #785 rather than fixed here.

### Changed

- `docs/configuration.md` now documents when `encode_effort` changes anything at
  all, and what it costs on read (#768). The section described the setting as a
  trade between load speed and compression ratio, which reads as though the
  choice has no consequence after the load.

  The writer builds the FSST symbol table, then compares the result against the
  same data without it **after the block codec has run**, and keeps the table
  only when the saving clears `pgcolumnar.fsst_min_gain_percent`, default 5.

  **That decision depends on the data and cannot be predicted.** It is made for
  each column chunk, and two people who both write "text-shaped" test data get
  opposite answers. In four shapes measured for this entry the table was dropped
  every time, so `full` and `fast` produced byte-identical storage and equal read
  times. A second set of measurements found shapes where it survives and `full`
  is 31.8% and 9.6% smaller, at read times of 1.08x and 0.99x, because the extra
  decoding is paid back by reading fewer bytes.

  So the section gives the reader a way to measure their own column rather than a
  verdict. An earlier draft of this entry concluded that at default settings the
  option changes nothing but write time. That was true of the shapes it was
  measured on and false in general, and a reader who acted on it could have paid
  a third more storage.

  The read-cost table is scoped to `compression = 'none'`, which is the only
  setting that keeps the table unconditionally, and is labelled as isolating the
  encoding rather than describing a default installation:

  | Text shape | `full` | `fast` | `fast` is |
  | --- | ---: | ---: | ---: |
  | 128-char hex | 267.6 ms | 143.9 ms | 1.86x faster |
  | URL-shaped text | 131.5 ms | 96.0 ms | 1.37x faster |

  Those replaced 2.69x and 3.84x, which were measured before the reader learned
  to decode a symbol with one machine word. The section names the reader version
  its figures came from.


### Fixed

- `ORDER BY` no longer returns unordered rows after a column rename, and neither
  ordering self-gate skips work it must do (#778). Both follow from one cause:
  nothing maintained the recorded sort key across a rename. `pgcolumnar.storage` records
  the applied sort key as column **names**, and both gates compare that stored
  list against the current `attname`: `pgcolumnar.vacuum_sorted`'s gate and the
  online `pgcolumnar.recluster`'s. Nothing maintained the mark across a rename.

  A three-statement swap made the stored name resolve to a different column:

  ```sql
  ALTER TABLE t RENAME COLUMN a TO tmp;
  ALTER TABLE t RENAME COLUMN b TO a;
  ALTER TABLE t RENAME COLUMN tmp TO b;
  SELECT pgcolumnar.vacuum_sorted('t','a');   -- reported success, did nothing
  ```

  The gate reported "already in this order" about a column that was never
  sorted, and the table was left neither ordered nor reclaimed with no error
  raised. For `vacuum_sorted` that is the failure the gate exists to prevent,
  reached through another door. `recluster` returned 0, which a scheduler reads
  as "nothing to do".

  The mark is renamed rather than cleared. A rename does not move data, so the
  ordering is still true of whichever column now carries the name, and following
  the rename keeps a real optimisation instead of discarding it on every rename.
  It composes through the swap above: `{a}` to `{tmp}` to `{b}`, which is the
  column the rows are ordered by. A rename that touches no sort-key column does
  not rewrite the storage row at all.

  The wrong-answer half is the more serious one. The sorted-pathkey code (#751)
  reads the same mark, so a stale mark made the planner claim an ordering that
  did not hold: after the swap above, `SELECT a FROM t ORDER BY a` planned with
  no `Sort` node and returned 1,990 descents out of 2,000 rows.

  The rename **cascades** to inheritance children and partitions, which are
  separate relations with their own storage and their own marks, so the fix
  walks every columnar descendant. PostgreSQL refuses
  `ALTER TABLE child RENAME COLUMN` with "cannot rename inherited column", so
  for a columnar partition the parent is the only route a user has, and a fix
  that looked only at the relation named in the statement would never have fired
  for it at all.

  The declared key in `pgcolumnar.options` is maintained the same way, and it is
  not optional once the mark is. Before, both were consistently stale; renaming
  only the mark would make the two catalogs name different columns, so a bare
  `pgcolumnar.vacuum_sorted(t)` would silently rewrite on a different physical
  key than it did the day before, with no change to the call and no change to
  the declared intent. `options.sort_by` holds names deliberately, because
  `options` is the catalog carried through `pg_dump` where attnums renumber and
  names do not, so maintaining them across a rename is the correct repair rather
  than a workaround.

  One behaviour improves as a result. The sorted-pathkey claim used to be
  refused after a rename, because the recorded name stopped resolving; it now
  survives, so `ORDER BY` on the renamed column plans no `Sort`. That claim is
  sound for the same reason the mark is: the rows really are still in that
  order.
### Changed

- The harness now refuses a `pgcolumnar.set_options` call whose value the
  function will reject. `set_options` raises on an out-of-range limit, so a
  suite that calls it and discards the output runs on **default** limits while
  the script reads as configured, and every later assertion is about a fixture
  that was never built. Found in review probes that passed
  `stripe_row_limit => 500`, which errors with "must be at least 1000".

  The naive form of that guard would be wrong. Measured on this tree before
  writing it: 182 `set_options` calls and exactly three out-of-range literals,
  all in `test/audit.sh`, all deliberate, all wrapped in `expect_error` because
  rejecting them is what that suite tests. A guard flagging every out-of-range
  value is wrong on three of three. The discriminator is whether the call's
  result is inspected or discarded, so calls passed to `expect_error` are
  skipped and exactly the silent class remains.

  Line continuations are joined before matching, because two of those three
  calls are backslash-continued and a line-based scan sees neither the value nor
  the `expect_error`. That direction fails safe; the same blindness fails open
  on a real multi-line offender. Proved: without the join, those three become
  false positives.

  The guard also asserts that it swept **every** file containing such a call,
  not merely that it swept something. Its globs are `test/*.sh` and `bench/*.sh`,
  which do not match `test/selftest/*.sh` or `test/pbt/*.sh`, so it was complete
  by accident of the tree's current shape rather than by construction. Comparing
  the files that contain a call against the files actually read fails loudly the
  day one appears anywhere new, including a directory nobody predicted, which a
  wider glob cannot do.

### Changed

- `docs/configuration.md` now says why `pgcolumnar.enable_group_vectorization`
  is off by default, which #755 records as not visible from outside the code.
  Measured rather than reasoned: on 4,000,000 rows in 200,000 groups the setting
  is worth 781.0 ms to 403.7 ms with identical answers, so performance is not the
  reason. That is the minimum of seven interleaved pairs on a non-assert build; an
  earlier figure here ran all of one arm and then all of the other, which on a
  contended host attributes drift to whichever arm ran second. An assert build
  measures about 2.2 instead, because `AllocSetCheck` runs per context RESET and
  the ordinary `Agg` resets far more contexts than the vectorized fold, so the
  skew lands one-sidedly on the slower arm. `src/columnar.h` also called the cap
  a plan-time one, which contradicted the code and the new section. The reason is the failure mode. The grouped path builds a hash table
  that does not spill, `pgcolumnar.groupagg_max_groups` bounds it, and the cap is
  checked during execution against the real group count because the plan is fixed
  by then and cannot fall back to an ordinary `Agg`. On a table with 1,500,000
  distinct keys the same query succeeds with the setting off and raises
  `54000 grouped vectorized aggregate exceeded pgcolumnar.groupagg_max_groups`
  with it on. A default would carry that to tables nobody chose it for, where the
  failure arrives when the table grows rather than when anything changes.
- The reader now reuses one row-group buffer instead of allocating and freeing
  one per row group (#768). It materializes the whole group into a single
  buffer, sized `stripe_row_limit` x row width, which at the default
  `stripe_row_limit = 150000` is about 19.8 MB for a 128-character text column.
  Allocated per group that is far past `ALLOC_CHUNK_LIMIT`, so it is its own
  malloc block, glibc returns it to the kernel when the group context is reset,
  and the next group faults every page back in.

  Measured on the same cluster, the same table and the same query, with only the
  installed library changing (1,000,000 rows of 128-character text,
  `stripe_row_limit = 150000`, vectorized paths off so the scan really decodes):

  | | minor faults per query | time |
  | --- | ---: | ---: |
  | before | 57,846 | 143.0 ms |
  | after | 4,838 | 85.8 ms |

  The trade is resident memory: the buffer stays at the largest group's size for
  the life of the read state rather than being returned between groups. Only
  pages a scan actually touched are ever resident either way, since a projected
  read still reads only the columns it wants, so the cost is bounded by what the
  scan already touched.

### Added

- The documented CDC recipe is now tested end to end, and the decision behind it
  is recorded rather than implied (#754). `test/logical_decoding_source.sh`
  already pinned the limitation: a columnar table emits no decodable change.
  Nothing pinned the workaround `docs/user-guide.md` offers in its place, which
  is to capture rows into a heap table with a row trigger and decode that.

  That recipe makes four factual claims, none of which was checked. Row triggers
  fire on a columnar table and see the same rows a heap table would. An `UPDATE`
  arrives as one `UPDATE` rather than as the storage's internal delete and
  insert, which is the claim a CDC consumer would be broken by and the one most
  likely to be false. The capture is transactional. The capture table decodes.
  `test/logical_decoding_cdc_recipe.sh` EXTRACTS the recipe from the guide at run
  time and executes it, so the guide is the thing under test, and asserts all
  four, plus the guide's warning that `FOR ALL TABLES` really does
  pick up the `pgcolumnar` schema, and its cost claim of one heap row per changed
  row.

  `docs/limitations.md` now states plainly that this is a decision and not an
  open item: emitting a decodable change for a columnar write needs a WAL record
  type carrying tuple structure for columnar data, which is a new WAL semantic
  and so out of scope by the same rule that keeps the extension installable on a
  stock server.
- `pgcolumnar.vacuum_sorted()` now self-gates: when the relation is already
  exactly the requested lexicographic run with nothing appended and nothing to
  reclaim, it skips the rewrite instead of materializing every live row through
  a tuplesort again. `pgcolumnar.recluster()` has had the ordering half of this
  since #415 so a scheduler can call it speculatively;
  `design/ISSUE_415_AUTOVACUUM.md` promised the mirror here.

  The mirror is not a copy, and that is the whole of the change.
  `vacuum_sorted` has two jobs: it orders the live rows and it physically
  reclaims deleted-row space. The obvious gate -- "is it already in this
  order", which is complete for `recluster` -- would answer yes on a relation
  that is in order and full of dead rows, and silently stop reclaiming. Ported
  unchanged into a tree carrying it, that gate leaves 10,000 deleted rows
  stored where the un-gated call reclaims all of them, with no error and no
  report.

  So the skip condition is "already in this order AND there is nothing to
  reclaim": a `lexicographic` run (a `zorder` run over the same columns is not
  this order, because Z-order over two or more columns is not a sort on any one
  of them), over exactly these columns in this order, covering every row group,
  with no deleted row and no empty group. Anything else falls through to the
  full rewrite, so re-sorting by a new key, re-sorting a Z-ordered table, and
  reclaiming all still work.

- The columnar scan now tells the planner the order a sorted rewrite left the
  rows in, so `ORDER BY` on that key plans no `Sort`. Every `pathkeys` field in
  the tree was `NIL`, so a table that `pgcolumnar.vacuum_sorted` had physically
  ordered still paid a full sort to be read in that order, and
  `ORDER BY ... LIMIT n` could not stop early.

  Measured on 4,000,000 rows in 27 row groups, `vacuum_sorted('t','k','j')`,
  `sort_status` reporting key `{k,j}`, 27 sorted groups, 0 appended, 0
  inversions on `k` in scan order. Instructions retired by one pinned backend,
  same query, `pgcolumnar.enable_sorted_pathkeys` off against on, and both arms
  return byte-identical answers:

  | query | Sort (off) | no Sort (on) | ratio |
  | --- | ---: | ---: | ---: |
  | `ORDER BY k, j LIMIT 10` | 5,545,800,871 | 14,133,342 | 392x |
  | `ORDER BY k, j OFFSET 3999990` | 8,395,455,193 | 3,392,835,665 | 2.47x |

  The second row is the whole relation consumed, so it is the sort itself:
  2,099 instructions a row against 848, about 1,251 a row that the sort was
  costing. The first is the early stop the sort made impossible. Re-run with the
  arm order reversed the four figures reproduce within 0.8%.

  **A claim the rows do not satisfy is a wrong answer, not a slow plan**, since
  the `Sort` that would have fixed the order is gone. The claim is therefore
  refused unless all of: the last ordering rewrite was lexicographic and
  recorded itself as such (#758), so a Z-order run and an unknown one are
  refused rather than guessed at; every row group lies inside the recorded run,
  so one appended or updated row retracts it; no sort column is collatable; and
  the requested ordering is a prefix of the recorded key, ascending, NULLS LAST,
  which is what the rewrite's own tuplesort applies.

  Collatable sort columns are refused because only the column names are
  recorded. `ALTER TABLE t ALTER COLUMN k TYPE text COLLATE "en_US.utf8"` on a
  column already `text COLLATE "C"` needs no transformation, so PostgreSQL
  updates `pg_attribute` and leaves every row where it is: same storage, mark
  intact, ordering silently a different one. A text sort key therefore gets no
  ordered path. Lifting that needs the collations recorded beside the names.

  The claim lives in a plan, and no catalog object the plan cache watches
  changes when rows are appended, so a group written outside the run now
  invalidates the relation's cached plans. Without it a prepared
  `ORDER BY k NULLS LAST LIMIT 5` answered from the ordered run alone after 600
  rows were appended below it. It fires once per row group and only on a
  relation that has a mark.

  Only the serial scan carries the claim. The parallel partial path and the
  projection path keep `NIL`: workers finish in any order, and a projection is a
  separate layout with its own sort key.

  A query that could not use an ordering does not pay to find one out. Deciding
  whether to claim reads the whole row-group list, and a query with no `ORDER BY`
  and no mergejoinable clause would have had any claim discarded at the end
  anyway, so `has_useful_pathkeys` is asked first. Measured as buffers touched
  during planning, on 1,000,000 rows in 1,000 row groups, a relation marked
  lexicographic, `SELECT count(*) FROM t WHERE j = 3`: 142 with the feature on
  against 121 with it off before, and 121 against 121 after. The query that can
  use the ordering still reads, which is what stops that from being satisfied by
  a function that reads nothing.

  New GUC `pgcolumnar.enable_sorted_pathkeys`, on by default. New suite
  `sorted_pathkeys`, 108 checks. (#751)

- A predicate on `date_trunc(unit, ts)` now drives chunk-group skipping for the
  ORDERED comparisons too, not only equality. #739 inverted `=` and declined
  every other operator, so `date_trunc('day', ts) >= '2024-02-01'` still read
  every chunk group, which is the shape a time-series filter usually takes.
  Monotonicity is what makes the ordered operators invertible, so they come from
  the same unit table for one scan key each rather than equality's two. Measured
  on the existing 500,000-row time-clustered fixture, chunk groups removed by the
  filter, `date_trunc` form against the explicit range it is equivalent to:

  | predicate | before | after | the equivalent range |
  | --- | ---: | ---: | ---: |
  | `date_trunc('day', ts) >= c` | 0 | 30 | 30 |
  | `date_trunc('day', ts) > c` | 0 | 31 | 31 |
  | `date_trunc('day', ts) < c` | 0 | 19 | 19 |
  | `date_trunc('day', ts) <= c` | 0 | 18 | 18 |

  Two cases carry their own risk and their own arms. With the constant on the
  LEFT the strategy has to be commuted, because `c < f(ts)` is `f(ts) > c`; for
  equality that was a no-op, which is why it never came up before. And an
  untruncated constant, which equality treats as unsatisfiable, is satisfiable
  here and moves the bound to the next bucket boundary instead of emptying the
  result. `timestamptz` stays declined for the reason #739 gives. (#403)

- A predicate on `date_trunc(unit, ts) = constant` now drives chunk-group
  skipping. A zone map holds `ts`, so a predicate about a function of `ts` could
  exclude nothing and the scan read every group. It is rewritten to a range on
  `ts`, which prunes with the machinery that already exists. Measured on 500,000
  rows clustered by time in 50 chunk groups: the equivalent explicit range read
  2 groups, the `date_trunc` form read all 50, and it now reads 2. The derived
  keys are conservative, so the executor still applies the original clause. Only
  the `timestamp` form is rewritten: `date_trunc` on `timestamptz` truncates in
  the session time zone, so a key frozen at executor start could be wrong if the
  zone changed. Units outside a known list, and a constant that is not itself
  truncated, are declined rather than approximated. A new suite,
  `preimage_rewrite`, measures the pruning and pins each declined shape. (#403)

### Added

- The vectorized aggregate now accepts a target list that CONTAINS aggregates,
  not only one that IS them. It required every entry to be a bare aggregate, so
  an entry that merely contained one fell off the path entirely, and on an
  unfiltered columnar table that is the difference between answering from the
  zone maps and scanning the whole relation. Measured on 8,000,000 rows:

  | query | before | after |
  | --- | ---: | ---: |
  | `count(*)::text` | 634.1 ms | **0.057 ms** |
  | `avg(a)+avg(b)` | 329.5 ms | **0.504 ms** |
  | `max(a)-min(a)` | 245.4 ms | **0.430 ms** |
  | `round(avg(a)::numeric, 2)` | 230.9 ms | **0.488 ms** |

  Two to four orders of magnitude, and every shape that was losing is ordinary
  SQL: a cast on a count, a difference of two aggregates, a rounded average.
  `count(*)` itself was already 0.030 ms, so wrapping it in a cast was costing a
  full scan of the table.

  The node is unchanged and still emits one bare aggregate per output column. The
  aggregates are pulled out of the target list, the node produces those, and a
  projection above it computes the expressions, which is how core's own
  `Agg` relates to an upper target. A target list that is already exactly the
  aggregates keeps its previous plan with no projection added, so nothing that
  worked before is planned differently.

  The parallel arm needed no change and never did: it uses core's partial
  grouping target, which is bare aggregates however the final target is shaped.
  Refusing the shape in the serial gate was killing the parallel arm with it.

  Unsupported aggregates, aggregates over expressions, and grouped queries
  decline exactly as before. A filter still routes to the scan-fold path behind
  `pgcolumnar.enable_ungrouped_vector_agg`, which is off by default; with it on,
  a wrapped aggregate takes that path too. New suite
  `vector_agg_tlist_shape`. (#755)

- A suite for the claim under `docs/limitations.md`'s "Replication and backup":
  that a columnar table is not a logical decoding source. The whole of #754 rests
  on that sentence and nothing asserted it. The only other logical-replication
  coverage, `logical_subscriber`, tests the opposite direction, a heap publisher
  into a columnar subscriber.

  Measured through a `test_decoding` slot created before any row is written, on a
  heap table and a columnar table of identical shape holding the same 50 rows:

  | | decoded changes |
  | --- | ---: |
  | `public.heap_t` (control) | 50 |
  | `public.col_t` (columnar) | **0** |
  | `pgcolumnar.*` (internal) | 8 |

  The heap control is the load-bearing arm, because zero decoded changes is also
  what a broken slot, a mis-built plugin or a query against the wrong slot looks
  like. The second documented behaviour is asserted too: the slot is not silent,
  it carries pgcolumnar's own catalog writes, and that churn scales with row
  groups and chunks rather than rows. 100 times the rows gives 8 records against
  10, not 800. New suite `logical_decoding_source`. (#754)

### Fixed

- `docs/limitations.md`'s account of parallel scans stated two things that are
  not true, and gave a cause the arithmetic does not support. It said a columnar
  scan "ships fewer, already-decoded rows" than a heap scan, and that "a heap
  scan on the same shape also flipped" at about half the default
  `parallel_tuple_cost`.

  Re-measured on 4,000,000 rows in a 14 column table with a heap twin holding the
  same rows. The two plans ship the **same** number of rows, 999,987, so the
  Gather charge is identical at 99,999; what differs is tuple width, three
  projected columns against fourteen. And the heap does not flip at half the
  default: columnar turns parallel at 0.060 and the heap at 0.030.

  The effect itself is real and larger than recorded. Over the eleven query
  shapes where the planner chose the serial scan, the parallel plan ran 1.8 to
  2.5 times faster on the median, and in every shape the slowest parallel run
  beat the fastest serial run, by 1.45 times at the narrowest margin. "Narrow"
  turns out to mean narrow in **columns**. Two mechanisms produce it, both now
  stated: `parallel_tuple_cost` is charged per row and is blind to width,
  overstating a narrow row by about 1.9 times at a constant volume of data
  shipped; and the columnar scan cost is two fifths to seven tenths of what its
  real time implies, by an amount that is largest on the narrowest projection.
  (#753)

- Both eager ordering rewrites discarded the ordering they had just applied.
  `pgcolumnar.storage.sorted_by` and `sorted_kind` exist to record which
  ordering a rewrite left behind, and the base schema has always specified them
  as `'zorder'` (`recluster`/`cluster`) or `'lexicographic'` (`vacuum_sorted`).
  Only the online `recluster` wrote them: both eager paths reached the catalog
  through one function that passed `NIL` and `NULL`, and the string
  `lexicographic` had never been written to the catalog at all.

  The two eager layouts were therefore identical in the catalog, and
  `pgcolumnar.sort_status` falls back to the declared `options.sort_by` when the
  recorded key is NULL, so both reported the same thing. Measured on 5,000 rows
  with `sort_by => ARRAY['k']` declared on each:

  | rewrite | `sorted_kind` | `sort_status.sort_key` | inversions on `k` |
  | --- | --- | --- | ---: |
  | `vacuum_sorted('t','k')` | was NULL, now `lexicographic` | `{k}` | 0 |
  | `cluster('t','k','j')` | was NULL, now `zorder` | was `{k}`, now `{k,j}` | 930 |

  Z-order over two or more columns is not a sort on any one of them, so the
  second row reported an order the rows were not in. A single-column key cannot
  show this, because single-column Z-order *is* lexicographic order.

  Two consequences go with it. `sort_status` now reports the applied key rather
  than the declared one on an eagerly clustered table, which is a visible output
  change for anyone reading that column. And #415's recluster self-gate requires
  `sorted_kind = 'zorder'`, so after `pgcolumnar.cluster('t','k','j')` it could
  never fire: an immediately following `pgcolumnar.recluster('t','k','j')` on an
  already-clustered relation paid a full rewrite of every group. It now returns
  0 and rewrites nothing, which is the case the gate was written for.

  A storage ordered by an earlier build still has NULL in both columns and still
  falls back to the declared key, until its next ordering rewrite. A reader that
  must not be wrong about the physical order should require
  `sorted_kind = 'lexicographic'` from `pgcolumnar.storage` and treat NULL as
  unknown, rather than read `sort_status`. An unsorted rewrite continues to
  claim no ordering. New suite `eager_ordering_record`. (#758)

- `pgcolumnar.vm_selftest` and `pgcolumnar.vm_is_visible` accepted any relation,
  including a plain heap table. `vm_selftest` does not only inspect the
  visibility map, it writes an all-visible bit into it, and a wrongly set bit is
  how an index-only scan skips the heap visibility check and returns rows it
  should not. Both functions now refuse a relation that does not use the
  `pgcolumnar` access method, with SQLSTATE `42809`, which is the same code the
  other C entry points use for that condition. Both also check ownership before
  opening the relation rather than after, so a caller who does not own the table
  is turned away before it can request a lock. `test/vm_privilege.sh` is rewritten
  around the change: the ownership boundary is asserted on a columnar table, where
  it still applies, and a heap table asserts the stronger new guarantee that no
  caller reaches its visibility map at all. (#748)

- Five entry points requested a relation lock before checking that the caller
  owns the table. `pgcolumnar.add_projection` takes a `ShareLock`, and
  `drop_projection`, `recluster`, `compact_rewrite` and `compact` take a
  `ShareUpdateExclusiveLock`; each opened the relation first and validated
  ownership after. An unprivileged caller could therefore queue in the lock
  manager on a table it has no rights to, blocking readers and writers until it
  was refused. Measured against a held `AccessExclusiveLock`: the call reached a
  four second lock timeout before the ownership error, where it now returns the
  ownership error immediately. Ownership is now checked before the lock, which
  is the ordering `pgcolumnar.vacuum`, `vacuum_sorted` and `cluster` already
  used. `drop_projection` also reported whether an arbitrary relation was
  columnar to a caller who does not own it; it now reports only that they are
  not the owner, matching the four entry points beside it. (#749)

- The columnar scan resolved `pgcolumnar.zone_map` once per chunk group per
  predicate column instead of once per scan. Every group a predicate could
  exclude ran a relation open with a lock and two catalog name lookups, then
  closed again; the reuse cache that the write path uses for the same catalogs
  is gated on a flag only the write path sets, so the read path never reached
  it. The scan now holds the relation and the resolved index for its own
  duration. Measured at 640 chunk groups with one predicate column, the same
  binary with the session bypassed, backend instructions pinned to one PMU and
  normalised by completed queries: 29,694,459 per query before and 26,676,590
  after, a saving of 4,715 per chunk group and 1.113x on the query. That is 15%
  of the cost #744 measured for locating the surviving groups at that size, so
  the systable index probe, which stays inside the loop, remains the larger
  part. Buffer counts are unchanged, which is the expected shape: a catcache or
  relcache lookup reads no buffers once warm, so the buffers a probe costs are
  the index scan. (#744)

- `pgcolumnar.set_options` refused a non-columnar relation with SQLSTATE `P0001`
  rather than `42809`. plpgsql's `RAISE EXCEPTION` defaults to `P0001` unless an
  `ERRCODE` is given, and the guard shipped without one, so the identical message
  `relation "..." is not a columnar table` carried one SQLSTATE from this
  function and another from the C paths, which raise it with
  `ERRCODE_WRONG_OBJECT_TYPE`. A caller keying on SQLSTATE, which is what this
  project's own privilege suites do deliberately, got different answers depending
  on which path refused it. The guard now sets `wrong_object_type` explicitly and
  `audit.sh` asserts the code. (#757)
- A `date_trunc` predicate within one bucket of the end of the `timestamp` range
  raised `ERROR: timestamp out of range` on a columnar table instead of
  answering. The rewrite computes the next bucket boundary as `lo + step`, and
  that addition throws near the maximum, which fails the whole query rather than
  declining to prune. Reachable on `main` before this release through equality
  alone: `date_trunc('year', ts) = timestamp '294276-01-01'` errored while the
  heap returned the row. The rewrite now declines within one step of the end.
  Infinities are unaffected, because interval arithmetic on them saturates rather
  than overflowing. (#403)

- The nightly coverage report now measures something. It had never captured any
  coverage: the job failed every night from the night it was added on
  2026-07-31, always at `lcov capture produced nothing`. `test/run_coverage.sh`
  runs under `sudo` in CI, so the instrumented build is owned by root, while the
  harness runs the server as `postgres` whenever it is root. gcov writes each
  `.gcda` beside its object, as the process that ran the code, so the backend
  could not create one and there was nothing to capture. The counter directories
  are now redirected with `GCOV_PREFIX` to a directory under `/tmp`, which is
  writable and traversable whoever the server runs as, and returned beside their
  objects before the report is built. Making the object directories writable was
  tried first and is not sufficient: creating a file also needs execute on every
  ancestor, and in CI the tree sits under the runner's home. Measured in a
  configuration with an ancestor the server user cannot traverse, which defeats
  the ownership fix: 0 counters before and 33 after, through `lcov` and `genhtml`
  to a report. The runner also refuses a run that captured no
  counters, and says where they should have been, instead of leaving `lcov` to
  report an empty tree as a tool failure. The per-suite logs are uploaded, so a
  suite that fails inside this job no longer has its detail discarded when the
  runner is torn down. (#740)

- Five differential assertions named an `ORDER BY` they could not fail on. The
  oracle in `test/lib.sh` hashes `string_agg(_row::text, chr(10) ORDER BY t)`,
  which sorts the rendered rows before comparing them: two results holding the
  same rows in opposite orders hash identically. That is the right comparison for
  the ~150 call sites that do not name an order, and no comparison at all for the
  five that did: two in `differential.sh`, one in `native_format.sh`, and two in
  `sorted_projection.sh`, whose whole subject is sorted output. Measured on the
  oracle expression itself: five rows forward and the same five reversed both
  hash to `2603e60e802d02d5370794d279cb522a`, while a genuinely different row set
  hashes differently, so it was order-blind rather than broken. A second oracle,
  `pgc_seq_hash`, hashes `ORDER BY row_number() OVER ()` and so keeps the query's
  own output order; `diff_query_ordered` is its comparison helper, and the five
  sites now use it. It keeps the `EMPTY` and unique `QUERY_ERROR.$seq` sentinels,
  so empty-versus-empty and error-versus-error still cannot pass vacuously
  (#418). A new `test/selftest` part enforces the split in both directions: a
  `diff_query` whose query names an `ORDER BY` now fails the harness self-test,
  as does a `diff_query_ordered` whose query names none.

- The extension-upgrade guard now runs in CI, and had never verified an upgrade
  before. `test/extension_upgrade.sh` catches a break that is invisible until a
  user upgrades: a renamed C link name leaves every existing install pointing at
  a symbol the new library no longer exports. No workflow set `PGC_RUN_UPGRADE`,
  so it ran nowhere, and the one runner that did reach it, the coverage report,
  discovered it through `not_a_suite()` and invoked it with no old source, then
  counted the resulting environment shortfall as a failed suite. A nightly job
  now builds the previous release on PostgreSQL 18, loads data into it and
  upgrades it in place, and both upgrade suites are excluded from the coverage
  runner together. Running it also exposed a defect in the suite itself: every
  connection used `psql -h /tmp`, while the socket directory is decided by how
  PostgreSQL was built, `/tmp` for a source build and `/var/run/postgresql` for
  the Debian and PGDG packages, so the suite had never been runnable against a
  packaged server and reported the connection failure as "old install did not
  store rows". It now states the directory it connects to. (#741)

- `pgcolumnar.set_options` accepted a relation that is not columnar, and silently
  recorded options for it. The row was not merely useless, because options are
  read by the columnar writer and a heap table has none. It also leaked: the object
  access hook that clears `pgcolumnar.options` fires only for columnar relations,
  so the row outlived the table. Measured on one cluster: `set_options` on a
  `USING heap` table stored a row, `DROP TABLE` left it behind, and `regclass`
  then rendered as the bare oid, which a later relation reusing that oid would
  inherit; the identical sequence on a columnar table cleaned up. `set_options`
  now raises `relation "..." is not a columnar table`, matching the wording the C
  paths already use, with a hint pointing at `ALTER TABLE ... SET ACCESS METHOD`.
  The test is on the access method **and** on `relkind`, which is what makes it
  match the cleanup rather than merely look strict: the drop hook returns before
  it examines the access method for anything that is not an ordinary table, so
  `'r'` is exactly the set whose options row can ever be cleared. That matters
  from PG17, where a partitioned table may itself carry an access method: an
  access-method test alone accepted a partitioned parent, which has no storage,
  which the writer never writes, and whose row the hook would never clear.
  Measured on 17.6: accepted, one row recorded, still present after `DROP TABLE`
  keyed to the dropped oid, while an ordinary columnar table in the same run
  cleaned up. PG16 and earlier refuse `PARTITION BY ... USING pgcolumnar`
  outright (checked on 16.14). Partitions themselves are ordinary tables and are
  still accepted, which is where the options belong. A materialized view is
  refused for the same reason: `CREATE MATERIALIZED VIEW ... USING pgcolumnar`
  works and its rows read back, but options recorded for one are inert, measured
  against a live fixture that moves the same measurement from 3 to 21 on an
  ordinary table, and its row leaks on `DROP` exactly as the others do.
  That conversion keeps the relation's oid (measured), so the one workflow that
  might have wanted the old order, options first and convert second, is served by
  setting them after the conversion. Nothing in the test corpus called
  `set_options` on a non-columnar relation. The guard is mirrored into the
  `1.0-alpha` → `1.0-alpha2` upgrade script, so an upgraded catalog still matches
  a fresh install. (#432 follow-up)

- The vectorized aggregates now bound their per-execution memory with one
  mechanism instead of two. The scan-key context added for #717 covered the
  scan keys; the scratch context added for #727 covered the whole scan and so
  covered the keys as well, on the ungrouped node. The grouped node now has the
  same scratch wrap, the two Begin-time eligibility checks build in a temporary
  context they delete, and the scan-key context is gone. No behaviour changes
  and no measurement moves; what changes is that one thing owns the lifetime,
  and the regression check for it can name what enforces it again. (#717, #727)

- A columnar scan no longer grows query memory on every rescan. A rescan reuses
  the read state rather than closing and re-opening it, and each restart rebuilt
  the row-group list and its per-group metadata into the read state's own
  context without reclaiming the previous one, so a LATERAL or parameterized
  scan accumulated one list per outer row. The list now has a context the
  rescan resets. Measured against the same query over a heap table with
  identical data, so the figure is what pgcolumnar adds rather than what
  re-executing any node costs: 198 bytes per rescan against a 33 byte heap
  floor before, 33 against 33 after. (#734)

- A plain `EXPLAIN` of the ungrouped vectorized aggregate now reports the
  filters it pushes down. The counts were assigned after the node's
  EXPLAIN-only return, so a plain plan printed zero for both
  `Columnar Pushed-Down Filters` and `Columnar Vector Predicates` however many
  there really were, while `EXPLAIN ANALYZE` of the same query reported them
  correctly. A hard zero reads as pushdown not happening rather than as a number
  that was never filled in, and it left the two vectorized nodes disagreeing
  about one label, since the grouped node has always counted before its own
  return. Both counts are catalog and plan work only. (#726)

- The FSST decoder now checks for interrupts inside its decode loop. Every
  other value decoder already did. The loop is bounded by the encoded length
  of one vector, so this is a latency bound rather than the uncancellable hang
  the Thrift and Avro skip loops had, but a large vector could still hold a
  backend past a cancel or a `statement_timeout`. The check uses an iteration
  counter rather than a byte offset, because the stride is a mask and a code
  that advances the pointer by two can step over the exact multiple and never
  fire. `decode_interrupts` now covers this decoder, and asserts the check sits
  in the decode loop rather than in the bounded symbol-table parse above it.
  (#712)
- The Parquet level-width helper no longer performs a signed left shift that
  is undefined for a maximum level at or above 2^30, and no longer has two
  copies. It computed `1 << b` with `b` reaching 31; real schemas cannot get
  there, because levels accumulate one per nesting level from a bounded
  recursion, but it was undefined behaviour in a decoder that reads files it
  did not write. Making the shift unsigned is not the whole fix on its own: it
  makes the comparison unsigned too, so a negative maximum converts to a huge
  value and the loop runs to a 32-place shift that is undefined in turn, and
  does not terminate under a recovering sanitizer. The helper now returns
  early for a non-positive maximum and bounds the loop. It also moved to
  `columnar_parquet_format.h`, which the writer and the reader already share,
  because a writer and a reader that disagree on a level width produce a file
  that is silently wrong rather than one that fails to parse. Values are
  unchanged on every reachable input. A new suite, `parquet_level_width`,
  pins the parts no query can reach. (#710)

- The Iceberg name-mapping parse reclaims its per-entry scratch instead of
  holding it for the whole document. Walking one entry allocates a `JsonbValue`
  per container lookup, and none of it is needed once that entry's names are
  copied out; left in the caller's context it accumulated across the mapping.
  Measured as memory attributable to pgcolumnar above the cost of core parsing
  the identical string: 100 bytes per entry before, 3 after, which is 78 MB
  down to 4 MB over an 800000-entry mapping. The larger part of such a
  document's cost is core's own `jsonb` parsing and is unchanged; an
  unprivileged session can reach more of it with a plain cast, so this is a
  bounded improvement to one loop and not a limit on what a mapping can cost.
  A new suite, `iceberg_name_mapping_memory`, measures the difference against
  core's own parse rather than a total. (#731)

- The Iceberg name-mapping parse sizes its first allocation by the name cap
  rather than by the element count the file declares. The cap
  (`ICE_MAX_NAME_MAPPING`, 100000) was applied inside the loop, after an
  allocation taken from the document, so a mapping declaring N entries asked
  for 12N bytes up front however few names it actually held. The surplus is
  never written, so it costs address space rather than resident memory, and
  the effect on peak RSS measured 28 kB out of 1196 MB; this is
  defense-in-depth rather than a memory saving, independent of the 64 MB
  metadata bound several layers away that was the only other thing limiting
  it. `iceberg_name_mapping` gains arms at and one over the cap, built as a
  single entry carrying many names, which is the shape where the opening
  capacity and the real ceiling are furthest apart. (#711)

- The object-store write path (export sink, delete, list) now refuses a URL
  with userinfo (`user@host`) with the same error the read path always gave.
  Before, the write parse admitted the URL and failed closed downstream by
  accident: the allow-list cannot match a host with an embedded `@`, and a
  `user:pass@` URL split at the wrong colon into a rejected port. A new
  suite, `objstore_userinfo`, pins all three entry points on the SQLSTATE
  and the guard's own message. (#706)
- The debug metadata mutators (`pgcolumnar_debug_advance_reserved_offset`,
  `pgcolumnar_debug_set_metapage_version`) are now owner-only, checked
  before the table is opened like the other maintenance verbs. They ship
  unbound, but a binding is one `CREATE FUNCTION` away, and one of them can
  overwrite a table's metapage version and brick every later read. A new
  suite, `debug_hook_privilege`, binds them the way the test suites do and
  proves the gate as a non-owner role, by SQLSTATE and the owner message.
  (#707)

- A merge join over index-fetched columnar rows no longer aborts with `pfree is
  not supported by the bump memory allocator` on PostgreSQL 17 and later. A
  columnar index scan returns a deferred slot, and when a sort materialises it
  the current context is PostgreSQL 17's bump context, which forbids `pfree`. The
  deferred decode pfreed there twice: the needed-column `Bitmapset`
  (`bms_add_member`/`bms_free`) and, on a storage's first fetch, the
  format-version check's catalog scan (which frees a btree search stack). Both
  now run in a private pfree-supporting context; the decoded values are
  unaffected because they were already copied into the caller's context, where
  `palloc` is legal. Covered by a new suite, `native_fetch_sort_context` (#720).

### Added

- `IN (...)` and `= ANY(array)` predicates now drive chunk-group skipping.
  The scan-key builder derives a conservative `[min, max]` range over the
  array's non-NULL elements. A single-valued list becomes an equality key,
  which keeps the bloom-filter probe. A parameterized array on a generic
  plan is frozen at executor start, like scalar parameters. That includes a
  mixed list such as `IN (1, $1)`. A correlated array is not frozen and
  stays correct. A new suite, `native_saop_pushdown`, proves the pruning,
  the conservativeness, and both freeze rules. (#704)

- The cross-engine benchmark arms are now reproducible from the repository.
  `bench/build_timescaledb.sh` pins TimescaleDB 2.29.0 with the exact cmake
  options recovered from the original benchmark build, and `bench/build_citus.sh`
  pins Citus v14.1.0, each built against one explicit `pg_config` and refusing an
  assert build. `bench/provision.sh check` continues to report their presence;
  installing them is now a scripted, pinned step instead of a manual one (#702).

### Changed

- The vectorized aggregate no longer grows query memory on every rescan. The
  node re-executes for each outer row of a LATERAL or parameterized sub-scan,
  and what it allocated in the caller's context each time, the per-row value and
  null arrays, the projected set on the metadata path, and what the flushes and
  the reader left behind, lived until the query ended. It now runs in a scratch
  context released when the scan returns. Measured against the same query over a
  heap table with identical data, so the figure is what pgcolumnar adds and not
  what re-executing any node costs: 376 bytes per rescan against a 33 byte heap
  floor before, 31 against 27 after. The ordinary columnar scan beneath it has a
  separate per-rescan cost that this does not change. (#727)

- The vectorized aggregates no longer stack a fresh set of pushdown scan keys
  in query memory on every rescan. The scalar scan builds its keys once at
  Begin; these paths rebuild them at execution, and did so into a context that
  lives until the query ends, so a LATERAL or parameterized aggregate sub-scan
  grew with the rescan count. An `IN (...)` key made it material, because
  building one also detoasts the array constant and deconstructs it. Measured on
  a LATERAL aggregate with a 20000-element list: adding the list cost 179,222
  bytes of query memory per rescan before, and nothing measurable after, against
  a no-list control that is unchanged. The keys now live in a per-node context
  that is reset at each build. A new suite, `vector_agg_rescan_memory`, measures
  the two arms and asserts the difference. (#717)

- The grouped vectorized aggregate (`pgcolumnar.enable_group_vectorization`)
  now folds column at a time instead of row at a time. Where every group key is
  a plain column, every aggregate is one the fold accumulates (`count`, `sum`
  and `avg` over integer and float columns), and the whole `WHERE` is expressed
  exactly by scan keys, the node walks row groups and reads each column's
  packed values directly. The row path paid, for every row scanned,
  `PgColumnarReadNextRow`, an expression context reset, staging the row into a
  virtual slot, `ExecQual`, and one expression evaluation per group key; the
  fold pays none of those, and the group hash and the probe equality are inline
  for integer, `oid`, `date`, `time` and `timestamp` keys. Float keys keep the
  type's own hash and equality functions, because `-0.0` and `0.0` are one
  group with two bit patterns. Measured on 4,000,000 rows with 8 groups, 5
  repetitions, counting instructions retired by the backend: 15,850,896,522
  before and 7,352,675,551 after without a filter (2.16x), and 13,899,374,785
  before and 6,292,504,808 after with one (2.21x). `EXPLAIN` reports
  `Columnar Batch Fold` on the grouped node, as it already did on the ungrouped
  one, so a plan states whether the fold ran. Shapes the fold declines, among
  them a by-reference key such as `text`, an expression key, `min` and `max`,
  and a filter no scan key expresses exactly, run the row path and return the
  same results. A new suite, `native_groupagg_batch`, proves the results
  against a heap mirror and proves the work separately from them. (#708)

- An index or bitmap fetch no longer reads the whole row-group list out of
  the catalog for every row. The list is memoized per command and snapshot,
  with a refresh on any miss, so a group flushed earlier in the same
  statement stays visible. Measured on 3000 index-fetched rows: 6001 catalog
  index scans before, 2 after. The memo resets on subtransaction abort and
  on row-group retirement, and a new suite, `native_fetch_group_memo`,
  proves both by resurrection: through an open cursor after ROLLBACK TO
  SAVEPOINT, and through stale index entries after a same-statement
  compaction. (#709)

### Fixed

- The ungrouped vectorized aggregate (`pgcolumnar.enable_ungrouped_vector_agg`)
  no longer returns wrong results for a filter whose operator has no btree
  strategy, such as `<>`. The batch fold's only per-row filter is the scan-key
  loop, but eligibility only checked that each clause was a strict comparison,
  not that it became a scan key. A `<>` clause is strict yet builds no key, so on
  an all-by-value shape the fold engaged and counted the excluded rows
  (`count(*) WHERE x <> 5` over-counted by one). Eligibility now requires every
  clause to be expressed exactly by scan keys (`PgColumnarQualsExactlyKeyed`);
  conservative prune-only keys (anchored LIKE, and the IN-list ranges added
  above for #704) are marked inexact and also disqualify the fold, which then
  falls back to the correct scalar path. Covered by new cases in
  `ungrouped_vector_agg` (#715).

- A corrupt `column_chunk.page_offset` no longer crashes the backend on a
  whole-group (unprojected) read. The projected read already refused a chunk
  lying outside its row group; the whole-group decode path derived
  `base = nativeBuffer + (page_offset - file_offset)` without the same check, so
  a poisoned offset (or one below the group start, wrapping the subtraction)
  read out of bounds and took SIGSEGV. The containment check now sits on the
  shared decode path, so both reads raise `ERRCODE_DATA_CORRUPTED` and survive.
  The containment test on both the shared and projected paths is written free of
  unsigned overflow, so a `page_offset` of `-1` (storable in the signed `bigint`
  column, read back as `~UINT64_MAX`) is rejected here rather than wrapping past
  the `page_offset + page_length` sum into an out-of-bounds read. New suite
  `native_page_offset_bound` reproduces the crash and the fix, including the
  `-1` wrap on both paths; it closes the `page_offset` gap in `corruption.sh`,
  which only ever corrupted `page_length` through a projected scan.

## [1.0-alpha2] - 2026-08-18

### Added

- The extension packages a `1.0-alpha` to `1.0-alpha2` upgrade script, generated
  from the catalog delta between the two versions (16 new functions, 3 changed,
  two foreign-data wrappers, two `pgcolumnar.storage` columns, and the PUBLIC
  execute revokes on the internal projection and visibility-map functions). A
  convergence test, `native_upgrade_converge`, installs `1.0-alpha`, runs
  `ALTER EXTENSION pgcolumnar UPDATE`, and asserts the result is byte-identical to
  a fresh `1.0-alpha2` across every function definition and ACL, relation, column,
  type, and foreign-data wrapper. The full `1.0-dev` to `1.0-alpha2` path,
  including that an existing columnar table still reads unchanged across the
  C-symbol rename, is covered by a two-library test, `upgrade_from_dev_twolib`.
  This test caught three defects in the generated script before it converged
  (psql command tags captured into the SQL, function definitions concatenated
  without a terminating semicolon, and the ACL revokes omitted because a
  definition-only diff cannot see an ACL-only change).
- The Iceberg REST catalog `FOREIGN SERVER` now accepts a `warehouse` option
  alongside `catalog_uri`. It selects a warehouse on a multi-warehouse catalog
  and is sent as the `?warehouse=` query parameter on the `GET /v1/config`
  request. This completes the per-catalog credential model (issue #656).
  Regression test: iceberg_rest_server.

### Changed

- The native encoding-descriptor wire layout is single-sourced. The descriptor
  is a column chunk's writer-to-reader contract for its per-vector encoding; its
  byte layout was hand-packed in `columnar_write_state.c` and hand-parsed in three
  passes in `columnar_reader.c`, so a field change had to be made in five disjoint
  places or a reader stride would land mid-field. Because the version byte does
  not move on a field change, the version guard would pass and the mismatch would
  surface as `DATA_CORRUPTED` or wrong values, not a clean rejection. A new
  `columnar_encdesc.h` owns the layout (`PgColumnarEncdescPut*` / `ReadEntry`), and
  a `StaticAssert` ties the entry length to the field widths. The on-disk bytes are
  unchanged (verified byte-identical to the hand-packed form). Regression test:
  native_encdesc_golden (pins the layout; catches a field-order change that the
  self-consistent round-trip suites cannot).
- The delete-vector visibility logic is single-sourced. The fold that ORs a row
  group's delete bitmaps into one mask was duplicated in the sequential scan and
  the index-fetch liveness cache, and the per-row bit test was open-coded in five
  places across the sequential scan, the index-only scan, the per-row fetch, and
  the buffered read-your-writes path. A change to one (a new bitmap encoding, a
  bound) could silently diverge the others, invisible until one access path hit
  the changed row. The fold is now `pgcolumnar_merge_delete_vectors` and the bit
  test `dv_row_deleted`; every path routes through them. Behavior is unchanged.
  Regression test: native_delete_visibility_paths (asserts all paths agree on the
  deleted set; a mutation of the shared bit test fails every path).

### Fixed

- Group and per-vector skipping now read only the predicate columns' zone maps.
  `pgcolumnar_native_group_can_match` and the per-vector skip builder read the
  whole group's zone maps (every attribute's min/max) up front and used only the
  columns carrying predicates, so on a wide table a scan that consults one or two
  columns paid min/max buffer traffic proportional to the table width. Both now
  probe per column via `zone_map_pkey`'s `column_index`, exactly as the per-column
  bloom fetch already did; the per-vector spans (identical across columns) come
  from a predicate column, falling back to the whole-group read only when no
  predicate column has per-vector rows. On a 30-column table a one-predicate scan
  went from about 1200 zone-map row fetches to 30, matching the 2-column table.
  Regression test: native_zonemap_narrow.
- Chunk-group skipping now applies to parameterized predicates. A qual like
  `col >= $1` from a prepared statement, PL/pgSQL, or the extended protocol kept a
  `Param` operand, and the scan-key builder accepted only a `Const`, so on a
  generic plan the scan built no key and read every chunk group. Begin now freezes
  execution-stable operands (a `PARAM_EXTERN`, or a subexpression with no `Var`,
  no `PARAM_EXEC`, and no volatile function) into `Const`s before building the
  keys. A correlated `PARAM_EXEC` changes per rescan and is deliberately not
  frozen, so results stay correct; the executor still re-applies the original qual
  to every surviving row. On a 20-group test a `>= $1` generic-plan scan went from
  reading all 20 groups to reading 1. Regression test: native_param_pushdown.
- Reads of the `delete_vector` catalog now use its `delete_vector_pkey` index
  instead of a sequential scan. `PgColumnarReadDeleteVectorList` (called once per
  row group while a scan builds its liveness cache), the reclaim deleted-count
  sum, and `PgColumnarStorageHasDeleteVector` each passed `InvalidOid` to
  `systable_beginscan`, so every call sequentially scanned the whole
  `delete_vector` catalog filtered by scan key. On a table with deletes the cost
  was `O(row_groups * delete_vector_rows)`. The sibling metadata tables already
  index their reads this way; `delete_vector` now matches. Regression test:
  native_delete_vector_index (asserts the reads take the index, seq_scan = 0).
- The Thrift and Avro field-skip loops are now interruptible. A Parquet footer
  whose unknown field is a `list<bool>` with a file-declared count up to
  `0xFFFFFFFF` (or a struct holding many such lists) drove billions of zero-byte
  skips through `PgColumnarThriftSkip`, which checked stack depth but not
  interrupts, so the backend spun uncancellably on a sub-2 KB file reached through
  `parquet_schema()` / `read_parquet()`. `av_skip` had the same gap on an
  `array<null>` manifest block, where its interrupt check ran once per block
  rather than per element. Both now call `CHECK_FOR_INTERRUPTS` on the per-value
  skip path, so `statement_timeout` and cancel apply. This is a denial-of-service
  hardening in the same class as the parallel_copy FIFO fix. Regression test:
  decode_skip_interrupts (functional cancel for Thrift; per-element placement
  shape for both).
- `pgcolumnar.read_manifest_list` now reports a null manifest-list
  `min_sequence_number` as SQL NULL instead of 0, matching `sequence_number`. The
  value is not used by the delete-application rules, so this is a display fix.
  Regression test: iceberg_malformed (#686, #691).
- `pgcolumnar.parallel_copy` no longer hangs the backend when its path names a
  FIFO. `pcopy_open_regular_file` opened the path `O_RDONLY` and then checked for
  a regular file, but a FIFO blocks inside that `open(2)` and the block survives a
  cancel or `statement_timeout`, a denial of service. It now opens with
  `O_NONBLOCK`, rejects a non-regular file, then clears the flag, which also
  closes the stat-before-open race. Regression test: parallel_copy (#686).

- The Iceberg read path now refuses four classes of malformed or hostile table
  metadata that an adversarial re-audit (#644) found it mishandled. A non-regular
  file (for example a FIFO) named as a metadata, manifest, or data path is
  refused before it is opened, instead of blocking the backend in a
  cancel-resistant `open(2)` (an availability denial of service). A manifest-list
  `sequence_number` that is the Avro union's null branch is refused rather than
  decoded as 0, which had understated a data file's sequence number and could
  mis-apply an older delete and drop a live row. A position-delete row with a
  null or negative `pos` is refused rather than silently dropped, which had left
  a row the delete was meant to remove. A `current-schema-id` that names a schema
  absent from the `schemas` array is refused rather than silently resolved to the
  deprecated top-level `schema`, which had bound columns through a stale schema
  and misprojected rows. A v1 table with only a top-level `schema` still reads.
  The same non-regular-file guard also covers `pgcolumnar.import_arrow`, which
  opened its path with the identical unguarded FIFO `open(2)` denial of service.
  Regression tests: iceberg_malformed, iceberg_deletes, arrow_import.
- Concurrent `UPDATE` or `DELETE` of the same columnar row now serializes on the
  row identity, so the losing writer gets a retryable `serialization_failure`
  instead of duplicating the row and losing an update (issue #5, the UPDATE facet).
  Two sessions updating one row without a covering unique index each kept their own
  new version. The fix takes a transaction-scoped advisory lock on the row, then
  re-reads the committed delete vector under a fresh snapshot before writing. New
  GUCs `pgcolumnar.enable_row_update_lock` (default on) and
  `pgcolumnar.row_lock_buckets` (default 1024). Regression test: update_conc.
- The ungrouped batch fold's per-row gather now steps over the columns a query
  references rather than all of a table's columns. On a wide table a scan that
  reads a few columns walked every column per row (twice on a deferred group),
  which is loop overhead proportional to the table width. It now iterates compact
  key/payload lists; a 46-column single-aggregate fold measured about 23% faster
  with no change in results. Regression test: native_batch_fold_projection.
- The grouped vector aggregate's input-scan estimate is now shared with the
  columnar scan node's, via one pgcolumnar_refined_scan_cost helper. Its
  no-serial-survivor fallback used the bare seqscan formula, which omitted the
  projected-width I/O, the per-column decode CPU, and the zone-map survival
  scaling the real scan applies, so it under-priced the node's input on a wide,
  low-pruning scan. Regression test: native_groupagg_wide_cost.
- The Iceberg foreign-data wrapper now pushes projection down: it decodes only
  the columns a query references (from the output list and the recheck quals),
  not every column of every surviving file. A narrow projection over a wide table
  is much cheaper (a 1-of-40-column scan measured about 5x faster). The reader's
  existing needTop mask carries it; the wrapper computes the mask and the results
  are unchanged. Regression test: iceberg_fdw_projection.
- The index-fetch cost penalty now sizes row groups by a relation's effective
  `stripe_row_limit` (the per-table option when set, else the GUC), matching the
  writer and the zone-map survival estimate. It read only the GUC, so it
  mis-priced the row-group decode for a table that set the option, and could steer
  the planner toward or away from an index scan on that table. The effective-limit
  lookup is now one shared helper. Regression test: native_index_fetch_stripe_cost.
- The Iceberg foreign-data wrapper now estimates a scan's row count from the
  manifests (the sum of the live data files' record counts) instead of a constant
  1000. The constant mis-sized every scan and corrupted join planning above a
  large Iceberg table. Regression test: iceberg_fdw_estimate.

### Security

- The native varlena decoder now bounds a value's stored length against its
  buffer. A varying-length value in a column chunk (and a zone map's min/max)
  carries its own length prefix, which `PgColumnarDecodeValue`,
  `pgcolumnar_skip_value`, and `pgcolumnar_build_val_offsets` read and `memcpy`d
  with no check that the value fit; a corrupt chunk or catalog row could then
  declare a value running past the buffer (an out-of-bounds read), and a prefix
  carrying the external-TOAST tag would be detoasted through a bogus pointer. The
  decode sites now pass the value stream's end and read through a bounded reader
  that refuses an over-long length or an external tag with `DATA_CORRUPTED`. This
  is the trusted-storage boundary (bit rot, a hand-written stripe), not a
  file-author-reachable one, but a clean error beats a crash. Regression test:
  native_varlena_bound (a min/max whose header claims ~1 GB now errors instead of
  crashing the backend; a mutation removing the bound reddens it).
- Closed a stat-before-open race in the local file read path. The
  non-regular-file guard that keeps a FIFO from blocking the backend in `open(2)`
  (a cancel-resistant denial of service) screened the path with `stat()` before a
  separate `open()`, so a local principal who can write the directory could swap
  the checked regular file for a FIFO in the window between them and re-introduce
  the block. The five transient-fd local openers (the Iceberg metadata and
  manifest reads, the Avro manifest read, `import_arrow`, and the Parquet source)
  now go through one helper that opens `O_NONBLOCK`, `fstat`s the fd it holds,
  refuses a non-regular file, then clears the flag, so the file checked is the
  file opened. The Parquet source reads positionally with `pg_pread`. The
  parallel-copy partition coordinator (`pcopy_partition_aligned_offsets`), which
  needs a stdio `FILE*` for `getline`, applies the same `O_NONBLOCK` open plus
  `fstat` inline. Regression tests: local_open_race_free, parallel_copy.
- Fixed a backend crash on a hostile Iceberg manifest with a null path. The
  reader decodes a manifest and manifest list against the schema embedded in the
  file, which the table author controls, so a manifest_path (or data or delete
  path) can be declared nullable and encoded null. `ice_rebase` then called
  `strncmp` on the null pointer and segfaulted the backend. A null recorded path
  is now refused as `DATA_CORRUPTED`, matching the read path's other
  malformed-metadata refusals. Regression test: iceberg_malformed (#691).
- Fixed an HTTP request-line injection in the object-store client. A URL path or
  host containing CR or LF was written verbatim into the request line, so a
  crafted path could split the request and smuggle a second line to an
  allow-listed endpoint. The request path and host are now rejected if they carry
  CR or LF, as the caller-supplied header lines already were. Regression test:
  objstore_crlf.

- Fixed an uninitialized-memory read in the native DICT decode path. A chunk
  whose descriptor declared a `value_raw_length` larger than its codes decode to
  left the tail of the raw buffer uninitialized, and a varlena column then read a
  length prefix out of that garbage (silent wrong results, or an out-of-bounds
  read). `decode_dict` now requires the decoded length to equal the declared raw
  length, mirroring the FSST path. Regression test: native_dict_underfill.
- Fixed an out-of-bounds read in the Parquet dictionary decode path. A file whose
  RLE_DICTIONARY data page carried an index with the high bit set (reachable at
  bit_width 32) passed a signed bounds check that sign-extended it to a negative
  int, and the dictionary was then read far out of bounds, crashing the backend
  from a single crafted file. The bounds check is now unsigned and the index is
  rejected. The index decode runs only when the page has coded values, so an
  all-null column with an empty dictionary page still reads. Regression tests:
  native_parquet_dict_oob and native_parquet_streaming.

### Added

- Read-only Apache Iceberg support, filesystem-backed, at a table's current
  snapshot (#388). `pgcolumnar.iceberg_scan(metadata_path)` reads a table given
  a column definition list, resolving each output column to a schema field id so
  a data file written before a column rename still reads. It applies **row-level
  deletes of all three kinds**, each under its own sequence rule: a position delete
  drops the row ordinals it names from a data file whose data sequence number is
  at or below the delete's (same commit or earlier), and an equality delete
  drops every data row matching a delete row on the delete's `equality_ids`
  columns when the data file's sequence number is strictly below the delete's
  (never same-commit data). Format-version 3 **deletion vectors** (Puffin
  files holding a portable roaring bitmap of row ordinals) apply under the
  position-delete rule, scoped to their referenced data file, and supersede
  position delete files for that file per the specification; the blob checksum,
  the manifest/footer offsets, and the recorded cardinality are verified, and
  at most one vector may reference a data file. A null delete value matches only a null data value,
  and columns beyond `equality_ids` do not take part in the match. A
  partition-scoped equality delete is applied within its partition: its stored
  partition values are matched against each data file's, so it removes rows only
  from data files in the same partition. Equality
  deletes with no supported handling are refused rather than ignored, so a table
  using them errors instead of returning rows it should have removed:
  delete columns of
  types outside `int`/`long`/`string`/`boolean`/`date`, delete columns
  dropped from the current schema, and a partition value the reader cannot
  compare exactly. Supporting introspection functions:
  `iceberg_current_snapshot` and
  `iceberg_data_files` (which refuses any delete), and the Avro building blocks
  `read_avro_manifest` and `read_manifest_list`. Only Parquet data files are
  read; recorded paths are rebased onto the table's actual location and refused
  if they resolve outside it. The table may live in object storage: a metadata
  path of `s3://`, `http://`, or `https://` reads the metadata, manifests, data
  files, and delete files from the endpoint through the object-store module,
  gated by the same `objstore_allowed_endpoints` allow-list and ambient
  credentials as the Parquet reader. A data file written outside Iceberg, which carries
  no field ids, is read through the table's `schema.name-mapping.default`
  property, which binds its columns by name; a file with no field ids and no
  such property is refused rather than guessed. `read_parquet` also gained a
  `field_ids` form that projects columns by Parquet field id. See
  [Iceberg](docs/sql-reference.md#pgcolumnariceberg_scanmetadata_path-text-returns-setof-record).

- Read-only Apache Iceberg **REST catalog** support (#388). A table is named by a
  catalog (catalog URI, namespace, table) rather than a metadata path.
  `pgcolumnar.iceberg_rest_scan(catalog_uri, namespace, table_name)` reads it at
  its current snapshot, taking a column definition list exactly like
  `iceberg_scan`: the catalog resolves the table to its metadata location, which
  is then read through the same path, so every projection and delete rule
  applies unchanged. `pgcolumnar.iceberg_rest_table_location` returns that
  resolved metadata location on its own, and
  `pgcolumnar.iceberg_rest_namespaces` and `pgcolumnar.iceberg_rest_tables` list
  a catalog. Requests go over HTTP or HTTPS. The catalog endpoint is subject to
  the same `objstore_allowed_endpoints` allow-list and link-local refusal as
  every other remote access, and is carried by the object-store module, so no
  second TLS stack enters the server process. A bearer token, when the catalog
  requires one, is read from the `PGCOLUMNAR_ICEBERG_REST_TOKEN` server
  environment variable, never a function argument, so it does not appear in the
  statement log. The first argument may instead name a foreign server of the new
  validator-only `pgcolumnar_iceberg_catalog` wrapper (#656). The server holds
  `catalog_uri`, and the current role's user mapping holds the bearer `token` in
  `pg_user_mapping`, which is not world-readable, so one role's token is private
  from another. A role with neither a mapping token nor superuser rights is
  refused, and the validator keeps secrets off the world-readable server options.
  A user mapping may instead carry OAuth2 client credentials (`oauth_client_id`,
  `oauth_client_secret`, and optionally `oauth_scope` and `oauth_token_uri`)
  (#656). The catalog then mints a bearer by the client-credentials grant; the
  secret travels in the request body, never a URL or a log line, and a half
  credential is refused before any request. When the catalog vends short-lived
  storage credentials in its
  `loadTable` reply (the flat `config` keys or the `storage-credentials` array,
  longest prefix selected), `iceberg_rest_scan` reads the table's files with
  those credentials rather than the server environment (#656). Vended
  credentials do not bypass the endpoint allow-list. A table that vends none
  reads with the ambient environment as before. See
  [Iceberg REST catalog](docs/sql-reference.md#pgcolumnariceberg_rest_scancatalog_uri-text-namespace-text-table_name-text-returns-setof-record).

- An Apache Iceberg **foreign-data wrapper**, `pgcolumnar_iceberg` (#388). A
  foreign table over an Iceberg table gets the query's predicate, which
  `iceberg_scan` cannot, and prunes: a predicate on an identity-partitioned
  column removes whole data files before they are opened, reading each file's
  partition value from the manifest. A predicate on an integer or boolean column
  removes whole files whose stored minimum and maximum exclude it, so an
  unpartitioned column prunes too. Pruning is only an optimization, so a
  predicate the wrapper cannot decide never changes the rows returned, and every
  projection and delete rule matches `iceberg_scan`. The table option is
  `metadata_path`; `EXPLAIN (ANALYZE)` reports `Files Pruned`. An equality
  predicate on a `bucket[N]`-partitioned column prunes files whose stored bucket
  differs from the constant's, computed with the Iceberg murmur3 hash. A
  predicate on a `truncate[W]`-partitioned integer column prunes files whose
  truncated value range excludes it, and a predicate on a `day()`-partitioned
  date column prunes by day. The `year()`, `month()`, `day()`, and `hour()`
  transforms prune too, on a `timestamp` or `timestamp with time zone` column
  (and `year()`/`month()` on a `date`): each bucket spans a range, so a file
  whose bucket equals the constant's is read and its rows are rechecked, never
  dropped at the boundary, and a timestamptz value is compared as its UTC
  instant. Partition pruning covers identity, `bucket[N]`, `truncate[W]`
  (integer), and the temporal transforms on date, timestamp, and timestamptz;
  metrics pruning covers integer and boolean columns; other column types read in
  full. See
  [Iceberg FDW](docs/sql-reference.md#the-pgcolumnar_iceberg-foreign-data-wrapper).

- The Parquet read and export functions and the foreign-data wrapper read from
  and write to object storage (#393, #394). A path may be an `s3://`,
  `http://`, or `https://` URL wherever it may be a local path. `s3://` requests
  are signed with AWS Signature Version 4; `https://` verifies the server
  certificate and is available when the object-store module is built with
  OpenSSL. Support lives in a separate module, `pgcolumnar_objstore`, loaded on
  the first remote use. Reads take exact object keys only. `export_parquet` and
  `export_arrow` write to `s3://`, as one request for a small object or a
  multipart upload for a large one, and the object becomes visible only when the
  upload completes. See [Object storage](docs/sql-reference.md#object-storage).

- Credentials for object storage come from the server process environment
  (`AWS_ENDPOINT_URL`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
  `AWS_SESSION_TOKEN`, `AWS_REGION` or `AWS_DEFAULT_REGION`) for the function
  API, and from the catalog
  for the foreign-data wrapper: `endpoint` and `region` on the server, and
  `access_key_id`, `secret_access_key`, `session_token`, and
  `credentials_required` only on a user mapping, so a secret is never in a
  world-readable option. Ambient environment credentials are used only for a
  superuser or a mapping a superuser marked `credentials_required 'false'`.

- `pgcolumnar.objstore_allowed_endpoints` lists the endpoints the object-store
  module may connect to (#393). It is empty by default, which refuses every
  remote endpoint, so a role that can read or write server files cannot reach an
  arbitrary host through the extension. Link-local addresses, including the cloud
  instance-metadata address, are refused whether or not they are listed. The
  setting is superuser-only.

- `pgcolumnar.parquet_schema` reports a `field_id` column (#388), the Parquet
  schema field id each leaf column carries, which formats such as Apache Iceberg
  use to select columns by id. It is NULL when the writer emitted none.

- `pgcolumnar.maintenance_due(rel, compact_due_fraction, recluster_due_fraction)`
  reports whether an online maintenance verb is worth running on a table, from its
  statistics alone (#415). It takes no lock and rewrites nothing. It returns the
  deleted and appended fractions, whether `compact_rewrite` or `recluster` has
  crossed its threshold, and a `recommendation`. This is the policy the
  `pgcolumnar.autovacuum` daemon consults, and a monitoring role can call it
  directly. It is `SECURITY DEFINER` and checks that the caller may `SELECT` the
  table, so the owner can run it without superuser rights.

- `pgcolumnar.autovacuum`, a maintenance daemon for the online upkeep that core
  autovacuum cannot reach (#415). pgColumnar's `compact_rewrite` and `recluster`
  live in extension functions, not table access method callbacks, so core
  autovacuum never runs them. A table's dead rows and clustering decay then
  accumulate until someone runs the verbs by hand. This daemon runs them for you.

  It is off by default. When on, a launcher wakes every
  `pgcolumnar.autovacuum_naptime` seconds (default 60) and starts one worker per
  database. Each worker asks `pgcolumnar.maintenance_due()` which tables crossed a
  threshold, then runs the recommended verb over SPI in its own transaction.

  Two properties make it safe unattended. It calls only the
  `ShareUpdateExclusiveLock` verbs, never `vacuum`, `vacuum_sorted`, or `cluster`,
  so it cannot block a reader or a writer. And it yields the way autovacuum does:
  the worker sets `PROC_IS_AUTOVACUUM`, so the lock manager cancels its
  maintenance the moment a backend queues for a stronger lock. New settings:
  `pgcolumnar.autovacuum`, `autovacuum_naptime`, `autovacuum_compact_threshold`
  (0.2), and `autovacuum_recluster_threshold` (0.05). See the administration
  guide for the operator's view.

- `pgcolumnar.parallel_flush` dispatches a stripe flush across background workers
  (#445). Default off. When on, a flush of two or more columns fans the per-column
  encode and compress work out to a worker pool. Any column a worker does not
  finish is completed serially in the backend, so the stored bytes match the
  serial path either way. It helps one large flush of many numeric columns by up
  to 14 percent. A wide text-heavy flush regresses, and so do frequent small
  flushes, because it copies the buffered bytes through shared memory. So it is a
  per-session opt-in for a wide numeric bulk load, not a default.

- `pgcolumnar.fsst_verdict_reuse` caches a column's FSST keep/drop verdict for a
  bounded number of row groups (#472). Default 16; `0` asks every time, which is
  the behaviour before this setting existed.

  Deciding whether an FSST symbol table pays for itself costs a whole-corpus
  encode plus a compression pass, and the answer cannot be sampled: on a training
  prefix FSST can look 24 percent worse while over the whole column it is 23
  percent better. So it was asked once per column per row group, and for a column
  whose data does not change character that re-derived the same answer for the
  whole load. Measured at 2,000,000 rows in 20 row groups: 2482 ms of a 5319 ms
  `md5` load and 843 ms of a 2081 ms email-shaped load, with the verdict identical
  all 20 times.

  A text load is about 2.5 times faster as a result, measured in-suite at 1623 ms
  against 648 ms. Stored bytes are unchanged for a column whose verdict is stable,
  which is asserted rather than assumed: the suite compares the encoding
  descriptor, block codec and page length of every chunk. A column that changes
  character mid-load is noticed within the bound.

  Reuse is per statement. Nothing is persisted and no on-disk structure changes.

- `EXPLAIN (ANALYZE)` now reports `Columnar Usable Skip Predicates` beside
  `Columnar Pushed-Down Filters` (#479). The existing line counts the filters the
  scan was handed and is unchanged; the new one counts how many of those the
  reader can actually skip chunk groups with. A filter whose types have no
  ordering function for the pair is dropped by the reader and excludes nothing,
  and until now the plan reported it as pushed down with no way to see the
  difference. That is how #477 went unseen for a year, and how
  `test/zonemap_cost.sh` validated a cost discount against a fixture that pruned
  zero groups.

  All three nodes that print the original line report the new one: the scalar
  custom scan and both vectorized aggregate nodes. The new line needs `ANALYZE`,
  since it describes what the scan built at execution.

- `pgcolumnar.analyze()` now collects `most_common_vals` and `most_common_freqs`,
  and excludes those values from `histogram_bounds` (#414). Frequencies are exact
  counts over the total row count rather than sample estimates. PostgreSQL 18 and
  later, which is where `pg_restore_attribute_stats` exists; earlier majors raise
  and should use `ANALYZE`.

  The selection rule is PostgreSQL's own. `analyze_mcv_list()` keeps the entire
  list when the whole table was read instead of applying its significance filter,
  because that filter exists to judge sample frequencies. Reading the column makes
  the values eligible on count alone, matching what core would store given the
  same information.

  Excluding most-common values from the histogram is required rather than
  cosmetic: keeping them counts those values twice in selectivity, once from the
  most-common list and again inside the bucket that holds them.

- `test/analyze_differential.sh`, which compares the statistics `pgcolumnar.analyze()`
  writes against the shape PostgreSQL's own `ANALYZE` produces across five column
  types. `pg_restore_attribute_stats` takes `VARIADIC "any"` and responds to a
  mistyped argument with a warning rather than an error, so a statistic can be
  dropped while the call reports success. Values cannot be the comparison, since
  exact and sampled statistics differ by design, so the suite compares the
  operator, collation and presence of each statistic kind, and verifies every
  stored value against an independent count.

### Fixed

- The Iceberg reader no longer crashes on a malformed manifest (#644). A crafted
  manifest that recorded no data-file path made `iceberg_scan` and
  `iceberg_data_files` dereference a null pointer. A manifest whose Avro record
  schema gave a `fields` element as a JSON array made the schema decoder read past
  an object container. Both now raise a clean error. These reproduce only from
  hand-crafted manifests, since no writer emits them, and they are covered by the
  new `iceberg_malformed` suite.

- A failed `export_parquet` or `export_arrow` no longer leaves a partial file at
  the destination (#394). An export writes to a temporary name and renames to the
  final name only when it is complete, so a reader never sees a half-written file.
  Every write is checked, so a full disk during an export is reported rather than
  detected only at close.

- `EXPLAIN (ANALYZE)` on a vectorized aggregate reports whether the batch fold
  actually ran, not whether it was predicted eligible (#602). A query that fell
  back to the row path, such as an aggregate over a column added after some row
  groups, no longer reads `Columnar Batch Fold: yes`. Plain `EXPLAIN`, which has
  no execution to report, still shows the prediction.

- `pgcolumnar.sort_status` works for a non-superuser who owns the table (#608).
  The function reads pgColumnar's internal catalogs, which carry no `GRANT`. As an
  invoker-rights function it therefore failed for any caller who was not a
  superuser. It is now `SECURITY DEFINER` and checks that the caller may `SELECT`
  the table. The owner can read the sort status of their own table, and no caller
  gains access to a table they could not already read.

- `pgcolumnar.analyze()` counts `null_frac` over live rows (#485). It came from
  the zone maps, which record what was written, so a deleted row kept counting
  toward the denominator until the table was rewritten. `VACUUM` did not correct
  it. On 1,000 rows holding 100 nulls, deleting the 301 rows of one value left
  `null_frac` at 0.100000 against a true 0.143062.

  The size of the error is not the whole of it. `null_frac` came from the zone
  maps while the most-common frequencies came from a live count, so one
  `pg_stats` row carried two statistics normalised against different
  populations: a `null_frac` implying 1,200 rows beside a frequency implying
  900, with 900 actually present. `null_frac + sum(most_common_freqs) + rest =
  1` stopped holding, and `eqsel` subtracts both when pricing everything else.

  The null count now comes from the read the function already performs, so this
  costs no extra pass. It does give up the "null_frac is a metadata read"
  property claimed for #414 slice 1, which cost nothing in practice because the
  function always goes on to read the column for `n_distinct`. A metadata-only
  fast path would need a live-row count, which is that same read. Whether
  `pgcolumnar.zone_map`'s counts should account for the delete vector, which
  would also affect pruning, is a wider question and is not addressed.

- A column declared over a domain now prunes chunk groups (#483). The scan key
  was built and then dropped: a domain column carries the domain's type in
  `pg_attribute` while the constant beside it carries the base type, so the
  comparison looked cross-type, and an operator family has no comparison
  function registered for a domain. Measured on identical values in one table
  over 20 row groups, `int` and `bigint` each removed 19 groups and a domain
  over either removed none, while all three reported the filter as pushed down.

  Answers were never wrong, because the executor re-applies the qual. The cost
  was reading the whole table on ordinary SQL. Both sides of the comparison are
  now resolved to their base types, so a domain compared against a value of a
  different domain over the same base type is also recognised. Ordering and
  hashing are unchanged: the comparison and hash functions were already taken
  from the column type's resolved entry, which is what the writer used to build
  the zone maps and bloom filters.

- A `bigint` column compared against an unadorned integer literal now prunes
  chunk groups (#477). The scan key was dropped because the column type's default
  comparison function cannot take an `int4` argument, so predicates of the form
  `bigint_column > 16000` read every chunk group while `bigint_column >
  16000::bigint` pruned normally. The comparison function is now resolved for
  both types from the column's btree operator family, which supplies exactly this
  for the built-in numeric types. Where a family provides no such function the
  key is still skipped, as before.

  `EXPLAIN` did not show the difference. `Columnar Pushed-Down Filters` counts
  scan keys given to the reader rather than predicates able to exclude a group,
  so it reported the filter as pushed down while nothing was skipped.

  The bloom filter probe remains disabled for cross-type equality. The filter
  stores hashes of column-type values, so hashing a differently typed constant
  would probe a slot that was never written and could skip a group holding
  matching rows.

- `pgcolumnar.analyze()` now honours the per-column statistics target set by
  `ALTER TABLE ... ALTER COLUMN ... SET STATISTICS` (#414). It read the global
  `default_statistics_target` for every column, so a column given its own target
  was sized by the global setting instead. A target of zero means the column is
  not to be analysed at all, and is now respected rather than overridden.

  Requesting only zero-target columns no longer raises. The function reported
  that it had collected statistics for no columns, with a hint about missing row
  groups, which pointed at storage for what was a deliberate setting.

- Renamed the custom scan node from `ColumnarScan` to `PgColumnarScan`, and the
  custom path from `ColumnarAgg` to `PgColumnarAgg` (#428). `ColumnarScan` is
  also registered by **Citus columnar** and by **TimescaleDB 2.29**.
  PostgreSQL's registry is one hash table keyed on that name, so two extensions
  cannot both hold it. Neither failure needed a pgColumnar table; our presence
  in `shared_preload_libraries` was enough.

  With **Citus columnar** the server refused to start at all, in either load
  order:

      FATAL:  extensible node type "ColumnarScan" already exists

  With **TimescaleDB** there was no startup error and serial queries returned
  correct results. TimescaleDB checks the registry first and silently skips
  registering when the name is taken, so a parallel worker then resolved
  TimescaleDB's node through pgColumnar's callbacks, and any parallel query over
  a columnstore hypertable failed with
  `could not read blocks 0..0 in file ...`. That is the more dangerous of the
  two, because nothing announces it.

  **This changes `EXPLAIN` output.** Plans that read `Custom Scan (ColumnarScan)`
  now read `Custom Scan (PgColumnarScan)`. Anything parsing plan text for the old
  name must be updated. The `Columnar ...` property lines, such as
  `Columnar Projected Columns`, are a different namespace and are unchanged.
  `ColumnarAgg` never appeared in `EXPLAIN`: it names a `CustomPathMethods`, and
  the planned node carries the scan's methods (`columnar_vector.c:717`), so that
  half of the rename is hygiene rather than a visible change.

### Changed

- `pgcolumnar.recluster` no longer rewrites a table that is already clustered by
  the requested key (#415). The function records the clustering key and kind it
  establishes. A later call with the same key returns 0 and touches nothing when
  the existing sorted run still covers every row group. Before this, it re-sorted
  on every call, so a scheduled recluster rewrote the whole table each time, which
  is why the maintenance daemon could not have run it safely. `pgcolumnar.sort_status`
  now reports this recorded key as `sort_key`, and falls back to the declared
  `sort_by` when there is no recorded key.

- A columnar scan whose filter cannot be pushed down now skips decoding the
  projected columns of a 1024-row vector that holds no matching row (#452). The
  scan decodes the filter columns first, rules out the vectors with no match, and
  decodes the rest only for the vectors that survive. A `SELECT *` under a
  leading-wildcard `LIKE` that matches few rows then approaches the cost of
  `count(*)`. It no longer decodes every column of every row scanned. A count over
  one column gains nothing, because it has no projected column to skip.

- The writer detoasts each value once per row (#445). It was detoasted once for
  the encoder, once for the bloom filter, and once for each of the two zone-map
  comparisons. For a toasted column each of those was a separate decompression. A
  load of a large compressed text column is about 11 percent faster, and the
  stored bytes are unchanged.

- `pgcolumnar.analyze()` places `histogram_bounds` at PostgreSQL's own positions
  (#414). The bounds were evenly spaced quantiles; core places bound i at
  `values[floor(i * (nvals - 1) / (num_hist - 1))]` among the rows left after
  the most-common values are removed, and `percentile_disc` resolves a fraction
  to a different index whenever the two disagree.

  **This changes the emitted array.** The length and both endpoints are the
  same, so the exactness of the minimum and maximum is unaffected, but an
  interior bound can move by one position. Both forms are valid equi-depth
  histograms; core's is the one the planner's selectivity estimators were tuned
  against. Anyone comparing `pg_stats` across this upgrade should expect
  interior bounds to differ and that is intended, not a regression.

- The unsupported-rewrite error names `REPACK` on PostgreSQL 19 (#399). `REPACK`
  replaces `CLUSTER` and `VACUUM FULL` in 19 and dispatches through the same
  copy-for-cluster path, which pgColumnar does not implement, so a 19 user who
  typed `REPACK` was told that `CLUSTER / VACUUM FULL` was unsupported: two
  commands they had not typed, and on 19 the superseded ones. The message now
  names the command and hints at `pgcolumnar.vacuum()`, which does the work. This
  covers `REPACK (CONCURRENTLY)` too: given a table with an identity index, where
  PostgreSQL will run it, heap succeeds and a columnar table is refused.
- `CREATE TABLE ... USING pgcolumnar AS SELECT` no longer fails when the source
  plan is parallel (#387). The storage-row creation path re-checked for an
  existing row against `GetLatestSnapshot()`, which raises "cannot update
  SecondarySnapshot during a parallel operation" inside parallel mode, and CTAS
  runs its whole executor in parallel mode whenever the source plan is parallel.
  That is the default for any source large enough to be worth loading, so bulk
  creating a columnar table from existing data failed on every supported major.
  The lock and the fresh snapshot are now skipped when the relation was created
  by the current transaction, because no other session can see it and the
  first-writer race they defend against cannot happen. A committed table
  first-written by two sessions at once is unaffected and still serializes.

- The extension's exported C symbols are namespaced under `pgcolumnar` (#382).
  Two extensions that both call themselves `columnar` could define the same
  symbol. `columnar_handler` and `columnar_relation_storageid` collided with
  Citus columnar. Four settings variables such as `columnar_stripe_row_limit`
  also shared names with the same settings there. That case binds one library's
  setting to the other's storage.
- `default_version` moves from `1.0-dev` to `1.0-alpha`, so
  `SELECT extversion FROM pg_extension` now agrees with `VERSION`.
- `CREATE INDEX` decodes only the columns the index needs (#413). The index
  build received an `IndexInfo` carrying the key columns and the expression and
  predicate trees, and discarded it, so a one-column index on a wide table read
  every column. Both readers are now projected: the one a serial build opens for
  itself, and the shared scan a parallel build arrives with, which comes through
  the table-access-method scan interface and has nowhere to carry a projection.
  The parallel branch is not a corner case. With every parallel setting left at
  its default, a 1.5 million row table of incompressible text, 459 MB on disk,
  is built in parallel, so that is the branch a table of consequential size
  takes. On 300,000 rows of 20 columns on PostgreSQL 18, a one-column index
  drops from 568 ms to 73 ms with workers allowed, against 563 ms for the same
  index on a heap table.

- Logical replication into a columnar table says what is wrong and how to
  proceed (#435). Applying an UPDATE or a DELETE takes a row lock, which
  columnar storage does not support, so the apply worker raised "columnar: row
  locking is not supported yet". That names something the user was not doing.
  PostgreSQL takes that lock for every applied UPDATE and DELETE and has no
  lock-free path, and it does not advance the replication origin when a
  transaction fails, so the subscription retries the same transaction for as
  long as it runs and no later change is applied. The error now names logical
  replication, says the retry is unbounded, and points at
  `CREATE PUBLICATION ... WITH (publish = 'insert')`, which does work. See
  [Limitations](docs/limitations.md).

- The grouped vectorized aggregate's parallel arm is no longer declined on a
  truncating time key (#369). `estimate_num_groups` cannot see through a
  function, so for `date_trunc('minute', ts)` it falls back to the timestamp
  column's distinct count, which measured 19,996,000 against 720 actual. That
  number is charged twice on the parallel arm, once by the Gather for tuples it
  believes it must ship and again by the Finalize, and not at all on the serial
  node, which is priced per input row. The serial node therefore won by
  construction on the shapes where the parallel arm is fastest. The estimate is
  now bounded by the number of buckets the scanned time range can span, and only
  when the planner had nothing to estimate from. A plain column, an expression
  index and a user's `CREATE STATISTICS ON (expr)` all count as informed and are
  left alone. Measured on 20 million rows: a one-aggregate windowed query goes
  from 2,017 ms to 497 ms and a ten-aggregate one from 4,687 ms to 1,146 ms,
  while a plain-column key keeps a bit-identical estimate and its existing plan.
  Both settings involved are still off by default.
- The ungrouped vectorized aggregate no longer errors on a varlena filter
  column (#423). `SELECT count(*) FROM t WHERE s LIKE '%x%'` raised
  "unsupported byval length: -1" with
  `pgcolumnar.enable_ungrouped_vector_agg` on. The batch fold gathers each
  projected column with pointer arithmetic on `attlen`, which is -1 for a
  varlena, so the offset and the fetch were both wrong. The eligibility check
  walked the scan keys and asked whether each type was comparable, while the
  gather walks the projected set and needs each type passed by value. A text
  column filtered with `LIKE` is projected and is not a scan key, so it arrived
  unchecked. `uuid` and `name` failed the same way for a different reason: both
  are fixed width, 16 and 64 bytes, but passed by reference, and the gather
  hardcodes by-value. Such a shape now falls back to the row path, which is what
  the ALTER TABLE ADD COLUMN case already did. This was ClickBench q21.

### Upgrading

**Run `ALTER EXTENSION pgcolumnar UPDATE;` in every database that has the
extension, after installing this build.**

The rename moves the C symbol names that each installed function recorded when it
was created. Replace the shared library without this step and those records
point at symbols the new library does not export. The extension then stops
working until the catalog is updated. Reading an existing columnar table fails
with `could not find function "columnar_handler"`.

Nothing happens to your data, and no conversion runs. The upgrade replaces
catalog entries only, and keeps each function's identity, so the access method
binding and every dependency survive. The SQL you write does not change.

See [Upgrade](docs/installation.md#upgrade) for the commands, including how to
list the databases that need it.

## [1.0-alpha] - 2026-08-04

First tagged release. Everything below shipped in it.

### Known limitations

- The grouped vectorized aggregate's parallel arm is declined on shapes with an
  expression grouping key, because the core Finalize is priced off a group estimate
  that can be 25x to 42x wrong (#369). Both settings involved are off by default.
- The by-row-number fetch cache is bounded by `4 x (cap + retained position indexes
  + groupBuffer)` rather than `4 x cap`. On a table of many wide varlena columns one
  entry measured 62 MB against a 32 MB cap (#364). Releasing the position indexes
  with the decoded stream holds the bound but costs 47% in time, so this design
  keeps the speed and records the trade.
- The index-fetch penalty is bounded by a multiple of one full scan rather than
  modelled against the consumer, so a plan that stops early inherits more of it than
  it should (#376). The bound keeps the penalty steering correctly on every shape
  measured; the model is post-alpha work.
- Point lookups remain slower than heap, and the cost of a fetch grows with table
  width, because an index fetch decodes the attribute prefix up to the highest
  column the query reads. See `docs/limitations.md`.

### Added

- `pgcolumnar.parallel_copy(target, path [, workers])` loads a COPY text file into
  a columnar table with several background workers at once, and returns the row
  count (#300). Each worker runs core `COPY` over a byte range of the file, so
  parse and write behavior match `COPY FROM`. The load is atomic through two-phase
  commit: every worker prepares its transaction, and a coordinator commits them
  together only when all succeeded, so any failure rolls the whole load back. The
  target is a single columnar table, where any record-aligned split is correct, or
  a RANGE-partitioned table with columnar partitions, where the file must be sorted
  ascending by the partition key and the key type must be numeric or a date/time
  type. The columnar encode step is CPU bound, so the load scales with worker
  count up to the physical core count. It landed in two parts, partition-parallel
  (#323) and single-table (#324). See docs/user-guide.md and docs/benchmarks.md.

- Column projection reads only the columns a query references, and is **on by
  default** (#339, `pgcolumnar.enable_column_projection`). A columnar scan
  previously decoded every column of every row group it touched regardless of the
  query's target list, which discards the main advantage of the storage format on
  wide tables. Measured on a 100M-row 21-column fixture, a single-column
  aggregate improved 6.9x. The gain is smaller on grouped queries, which also read
  their grouping keys: 1.24x, 1.13x and 3.13x on three TSBS-shaped grouped
  aggregates. Turning the setting off restores the previous behavior.

- Vectorized aggregates, all **off by default** and opt-in while they are proven:

  - `pgcolumnar.enable_ungrouped_vector_agg` folds a plain `SELECT agg(col) FROM t`
    over the decoded column buffer instead of one Datum tuple per row (#337).
  - `pgcolumnar.enable_parallel_vector_agg` makes that fold parallel-aware (#343),
    extended to integer sum and average partials (#346), and to grouped
    aggregates (#366). Each worker claims distinct row groups through a shared
    counter and emits per-worker transition state that a core Finalize combines.
  - `pgcolumnar.enable_group_vectorization` answers `GROUP BY` from a vectorized
    grouped node (#321). `pgcolumnar.groupagg_max_groups` caps its hash table and
    errors with guidance rather than growing without bound.

  These remain off by default because plan selection for them is not settled: a
  grouped query with an expression grouping key such as `date_trunc()` can decline
  the parallel path on a group-count estimate that is 25x to 42x wrong (#369).

- `pgcolumnar.enable_index_fetch_penalty`, **on by default** (#355), prices the
  per-row heap fetch of an index or bitmap path on a columnar table. A columnar
  fetch decodes the row group the row lives in, while core prices it as a page or
  two, so an unclustered ordering column made an index scan look cheap and then
  run for minutes decoding the table many times over. The penalty counts the
  distinct row groups the fetches force, interpolating on the square of the
  leading-key correlation. Turning it off restores the previous planner behavior.

### Changed

- The fetch cache holds the columns that fit rather than dropping a whole entry
  when it exceeds its size cap (#359). An entry one byte over the 32 MB cap was
  not retained at all, so every fetch re-read the row group and re-decoded every
  column it touched. On a 100M-row fixture that was 2,833 ms at four aggregated
  columns and 134,147 ms at five, flat on either side of the step. Each column now
  decodes into its own context and the one that crosses the cap is released after
  its value is read, so exceeding the cap costs the overflow fraction rather than
  everything. An earlier fix moved the decode scratch out of the cached entry,
  shrinking entries about 3x (#353). A group whose raw bytes alone exceed the cap
  is still dropped whole.

- The index-fetch penalty is applied before the columnar path is offered to the
  planner, not after (#362). `add_path` frees a path it judges dominated, so a
  columnar path offered while the index paths still carried un-penalized costs was
  discarded, and raising those costs afterwards changed what `EXPLAIN` printed
  with nothing left to switch to. The planner chose an index scan it priced at
  13,954,742 over a columnar path it priced at 589,348, running 224 seconds where
  the columnar path runs 4.7. Two related defects were fixed with it: the parallel
  columnar path was conditional on a sequential scan surviving `add_path`, so it
  did not exist on exactly the selective queries where it was needed, and the
  projection path read the base path's cost after `add_path` may have freed it.

- The grouped vectorized aggregate path is charged for the folding it does (#349),
  `cpu_operator_cost` per input row per aggregate. It previously priced itself
  just above the scan it performs, which made it unpriceable against: every
  competing plan paid a per-row aggregation cost and this one paid none, so it won
  by construction, including against a parallel plan several times faster. That
  cost about 1.9x on a full-scan `GROUP BY` with few groups.

- The vectorized batch fold pushes scan keys, so it no longer forfeits zone-map
  row-group pruning (#349). The fold opened its reader with no predicates, so no
  group skipping occurred: on a clustered fixture with a selective predicate it
  read 200 of 200 row groups where the ordinary path read 2.

- Server-file functions now gate on the `pg_read_server_files` and
  `pg_write_server_files` roles instead of `superuser()` (#330), matching core
  `COPY ... FROM/TO 'file'` so a DBA can delegate server-file access without
  handing over superuser. This is a deliberate loosening. The read functions
  (`import_parquet`, `read_parquet`, `parquet_schema`, the `pgcolumnar_parquet`
  foreign-table scan, `import_arrow`) parse files this project wrote, so they are
  now reachable from a role short of superuser; give an untrusted Parquet or Arrow
  file the care in `docs/administration.md` while the Arrow parser fuzzing (#214)
  is incomplete. The write functions (`export_parquet`, `export_arrow`,
  `parallel_export_parquet`) gate on `pg_write_server_files`. `file_split_offsets`
  and `parallel_copy` already used the read role. `test/server_file_privilege.sh`
  now covers the full set and fails if a new file function lacks a check.

- The C standard flag for PostgreSQL 19 is probed rather than hardcoded (#294).
  This project sets `-std=gnu23` for PostgreSQL 19, whose headers use C23
  constructs. GCC 13 accepts only the older `gnu2x` spelling of the same
  language and rejects `gnu23` outright, so building against PostgreSQL 19 with
  GCC 13 failed on a flag the user never set. The Makefile now asks the compiler
  which spelling it takes. Every source file compiles under GCC 13 with `gnu2x`
  against PostgreSQL 19 headers.

- `pgcolumnar.recluster` records its ordered extent, so `pgcolumnar.sort_status`
  no longer reports a reclustered table as entirely unsorted (#311). It runs
  under a lock that permits concurrent inserts, and the mark is a boundary, so
  it can only be set where no other session's group is numbered below it. The
  rewrite records the stripe ids it reserves and marks the contiguous run from
  its first; a concurrent reservation leaves a gap in that sequence whenever it
  commits, which the visible catalog cannot show. With no concurrent writer the
  whole relation is recorded. With one, the run stops where it was interrupted
  and the rest is reported as decay, never the reverse.

- A row group's bloom filter is read for the columns a query filters on, not for
  every column (#314). A predicate probes one column, so a group that is
  examined needs the filters of the columns carrying predicates and no others.
  `bloom_pkey` is `(storage_id, group_number, column_index)`, so naming the
  column makes the fetch an exact index lookup rather than a range scan whose
  unwanted rows are discarded. Measured on one group of 200,000 rows over 12
  columns with one equality predicate: 715 buffers to 323, against a floor of
  251 with the bloom read deleted outright. With #310 the same probe query falls
  from 9577 buffers to 1547.

- A row group's bloom filters are read only when a predicate reaches them, not
  before every skip decision (#310). A bloom filter is consulted only for an
  equality predicate whose zone map did not already rule the group out, so a
  group the zone map skips needs none of them. The reader loaded them for every
  candidate group, and the cost scaled with the column count and the group size,
  because a filter holds one bitmap per column sized by the group's distinct
  values.

  The scale of that is easy to understand: on a 100 million row TSBS-cpu table a
  single filter is 256 kB, and the whole bloom catalog is 3.5 GB, larger than the
  data it describes. A selective scan copied it per query through 256 kB
  allocations, and profiling put about 55 percent of the query's CPU in
  anonymous-page faults under the group-skip check.

  On that table a clustered hostname query falls from 4610 ms to 106 ms, a factor
  of 43. On a smaller shape, 20 groups of 200,000 rows over 12 columns, the cost
  is 466 buffers per skipped group out of 504, and the query falls from 9577
  buffers to 1946. Results do not change; the filter was always a pruning step.

### Added

- `pgcolumnar.sort_status(rel)` reports how much of a sorted table is still in
  sorted order (#301). `vacuum_sorted` and `cluster` order a table once; rows
  inserted afterwards append in insertion order, and until now nothing measured
  how large that unsorted tail had become. An ordering rewrite now records the
  row group its run ends at, in a new `pgcolumnar.storage.sorted_through` column,
  and the function reports sorted and appended groups and rows alongside the
  declared `sort_by` key. A boundary rather than a count, so retiring a group
  inside the run does not move the mark onto a later replacement. The mark lives
  on the storage row, so any rewrite resets it with no invalidation step. The
  online `recluster` does not set it and therefore reports more decay than a
  table has (#311).

- Declarative `sort_by` clustering key (#288). `pgcolumnar.set_options(t, sort_by
  => ARRAY['col', ...])` records a physical sort key; `pgcolumnar.vacuum_sorted(t)`
  with no columns re-applies it, like PostgreSQL `CLUSTER` remembering an index.
  The sorted rewrite works on any btree-orderable column, text included (the
  Z-order `cluster()` is numeric-only), so a segment key such as `hostname`
  tightens its zone maps and lets equality/range filters on it skip chunk groups.
  Stored as column names, so it survives `pg_dump`/restore. Not auto-maintained;
  re-run after inserts. Virtual generated columns are rejected as a sort key.

- Column-oriented table access method (`USING pgcolumnar`) with per-column
  compression, chunk-group minimum and maximum skipping, per-chunk bloom filters,
  and a vectorized aggregate path.
- Native on-disk format PGCN v1: row groups, per-column chunks, an adaptive
  per-vector encoding cascade, zone maps for skipping, and per-chunk bloom
  filters. Delete, update, index scan, index-only scan, and projections all work
  on native tables. The earlier 1.0-dev format line has been removed; the
  `v1.0-dev` git tag preserves it.
- Compression codecs `none`, `pglz`, `lz4`, and `zstd`. `lz4` and `zstd` are
  compiled in when their system libraries are present.
- `count(*)` answered from catalog metadata without scanning.
- Parallel scan.
- Read stream prefetch in the scan on PostgreSQL 17 and later
  (`pgcolumnar.enable_read_stream`).
- Full index-only scan through a columnar visibility-map fork, with lazy `VACUUM`
  setting all-visible bits and clear-on-write, on by default
  (`pgcolumnar.enable_index_only_scan`).
- Multiple projections (C-Store model): a `pgcolumnar.projection` catalog, write
  fan-out, planner projection scan, back-fill, and vacuum coordination
  (`pgcolumnar.add_projection`, `pgcolumnar.drop_projection`,
  `pgcolumnar.enable_projection_scan`).
- Sorted storage with `pgcolumnar.vacuum_sorted`.
- Arrow IPC and Parquet export (`pgcolumnar.export_arrow`,
  `pgcolumnar.export_parquet`), self-contained with no libarrow or libparquet
  dependency. Coverage: scalar types (int2/4/8, float4/8, bool, text/varchar,
  bytea, date, time, timestamp, timestamptz, uuid, numeric, json),
  one-dimensional arrays, and composite types, with nulls at every level.
- Arrow IPC and Parquet import (`pgcolumnar.import_arrow`,
  `pgcolumnar.import_parquet`). The Parquet reader parses Thrift metadata,
  decompresses uncompressed, Snappy, GZIP, ZSTD, and LZ4_RAW pages, and decodes
  PLAIN and dictionary encodings from data-page versions 1 and 2. Both readers
  reconstruct one-dimensional arrays and composite types: Arrow from its List and
  Struct buffers, Parquet from the Dremel repetition and definition levels.
- Reading external Parquet in place. `pgcolumnar.read_parquet(path)` returns a
  file's rows without importing, `pgcolumnar.parquet_schema(path)` reports its
  columns and inferred types, and the `pgcolumnar_parquet` foreign-data wrapper
  exposes a file as a foreign table. A `path` may be a single file, a directory
  of `*.parquet` files, or a glob pattern, read as one relation in sorted order.
  The foreign scan skips row groups excluded by the query's predicate (min/max
  statistics) and decodes only the referenced columns; `EXPLAIN ANALYZE` reports
  the row groups and columns read and skipped and the number of files.
- Value encodings are chosen from a strided sample rather than by applying every
  candidate to every vector. Measured on a 6,000,000-row load: 20.9 s to 15.7 s,
  with byte-identical output. `pgcolumnar.encoding_sample_rows` controls the
  sample size and `0` restores the previous exhaustive selection.
- Partition values are percent-decoded, so a directory named `region=a%3Db` reads
  as `a=b`, and `__HIVE_DEFAULT_PARTITION__` reads as NULL rather than as that
  literal string, matching what Hive and Spark write.
- Hive-style partitioning on the `pgcolumnar_parquet` foreign-data wrapper. A
  foreign table declaring `partition_columns` reads `col=value` directory names
  as column values, and a predicate on a partition column drops whole files
  before they are opened, so a pruned file costs no I/O. `EXPLAIN ANALYZE`
  reports `Files Pruned`. The columns are declared rather than inferred, and a
  file missing a declared component raises rather than yielding nulls.
- A directory path now reads `*.parquet` files at any depth below it, where it
  previously read only the files directly inside. Entries whose name begins with
  `_` or `.` are skipped, so a Spark or Hive output directory does not read its
  own `_temporary` staging tree. A directory reached through a
  symbolic link is not descended, since a link to an ancestor would make the walk
  endless; a symbolic link to a file is still followed. Nesting deeper than 32
  levels raises rather than reading part of the tree.
- External Parquet files are read on demand instead of loaded whole. The reader
  holds a file's footer for the scan and pulls one page at a time, so peak memory
  for raw file data is one page rather than one file. A file of 1GB or more could
  not be read at all before this, because the whole-file allocation exceeded
  `MaxAllocSize`; that ceiling is gone. A row group excluded by predicate
  pushdown is now never read from disk, and `pgcolumnar.parquet_schema` reads
  only the footer.
- A Parquet DECIMAL is also read when it is stored as an INT32 or INT64 holding
  the unscaled integer, which is how writers store small precisions;
  `pgcolumnar.parquet_schema` advises `numeric(p,s)` for those columns.
- Parquet read type coverage extended to uuid and numeric (from fixed and
  variable DECIMAL, precision up to 38), fixed-length binary, and millisecond,
  microsecond, and nanosecond time units.
- `pgcolumnar.fsst_min_gain_percent`, a cost margin for the FSST string encoding
  decision. FSST is kept only when it reduces the compressed chunk by at least
  this percentage, default 5. Building FSST codes for every vector is one of the
  larger costs of a text or varlena load, and a sub-margin reduction does not
  repay it.
- The on-disk format version is enforced when data is read, not only stamped when
  it is written. Both the physical metapage version and the native data format
  version are checked, on every path that decodes columnar data, so a file this
  build cannot read is refused rather than misread.
- User and administrator documentation under [docs/](docs/index.md):
  installation, user guide, administration, configuration reference, SQL
  reference, and limitations.
- Benchmark harness (`bench/run_bench.sh`) covering storage size, query latency,
  vectorization, compression, sorted projection, index-only scan, projection
  scan, export, import, nested round-trip, and cross-engine reads of the Parquet
  output with DuckDB and pyarrow.
- Project logo under [logo/](logo/README.md).

### Fixed

- Bounded importer memory. `pgcolumnar.import_arrow` and `pgcolumnar.import_parquet`
  built each row's arrays and composites in one memory context and did not free
  them, using memory proportional to the row count. They now reset a per-row
  scratch context (and, for Parquet, a per-row-group context for decoded leaf
  streams), so peak memory stays bounded on large files.
- Hardened the Parquet reader against crafted files. File-declared page sizes,
  DECIMAL scale, and per-row-group column-chunk counts are range-checked, so a
  malformed footer yields a clean decode error rather than a stack overflow, an
  out-of-bounds read, or a wrong value. Float and double row-group skipping
  accounts for NaN and for inverted min/max intervals, and narrowing a wide
  Parquet value into a smaller PostgreSQL type raises instead of wrapping.
- Concurrent inserts of the same unique-index key now serialize correctly with a
  transaction-scoped advisory lock (`pgcolumnar.enable_unique_insert_lock`).
- Lost delete marks under concurrent same-chunk-group deletes.
- Relation-reference leak in parallel `CREATE INDEX`.

### Removed

- The decompressed-chunk cache, and the `pgcolumnar.enable_column_cache` and
  `pgcolumnar.column_cache_size` settings with it. Its only entry point had lost
  its caller when the earlier on-disk format was removed, so the cache had done
  nothing since. Two settings and four passages of documentation described a
  feature that did not run. A `postgresql.conf` that sets either parameter must
  drop the line. The implementation is in the git history if the performance case
  is made again against the current reader.

### Changed

- FSST string encoding is now kept only when it reduces the compressed chunk by
  at least 5 percent, rather than on any reduction at all. On shapes where FSST
  barely wins, such as high-entropy text, this costs about 2 percent stored size
  and reduces load time by roughly a third. Where FSST wins by more than the
  margin the encoding and the stored bytes are unchanged. Set
  `pgcolumnar.fsst_min_gain_percent` to 0 for the previous behaviour.
- Renamed the per-table option functions to `pgcolumnar.set_options` and
  `pgcolumnar.reset_options`. The previous names were carried over from an
  earlier compatibility goal that no longer applies. No aliases are kept, since
  the project is pre-release.

### Compatibility

- Builds from one source tree on PostgreSQL 15 through 19. Every test suite runs
  on all five majors.
- The Arrow and Parquet import and export functions require superuser and run on
  little-endian hosts.
- Cross-major `pg_upgrade` is covered by an opt-in gate
  (`PGC_RUN_UPGRADE=1 test/run_all_versions.sh`), in both copy and link transfer
  modes.
- All recorded test results come from x86_64. The suites have not been run on
  aarch64 or on a big-endian platform.
