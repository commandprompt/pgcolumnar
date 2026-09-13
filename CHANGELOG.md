# Changelog

All notable changes to pgColumnar are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). pgColumnar is
pre-release; the version marker is `1.0-alpha4`, recorded in `VERSION`. New tables
are written in the native on-disk format, PGCN v1. For the forward-looking plan see
[design/ROADMAP.md](design/ROADMAP.md); for full history see the git log.

The extension's `default_version` is `1.0-alpha4`, which is in development and not
yet tagged; `v1.0-alpha3` is the latest published pre-release. Upgrade scripts from
every previously shipped version ship with it (`1.0-dev`, which the v1.0-alpha tag
installed, `1.0-alpha`, `1.0-alpha2`, and `1.0-alpha3`), so a single
`ALTER EXTENSION pgcolumnar UPDATE` reaches `1.0-alpha4` from any of them. Older
notes in this file describe `default_version` as pinned at an earlier version, each
true until the next version shipped.

## [Unreleased]

### Added

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
