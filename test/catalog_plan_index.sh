#!/usr/bin/env bash
#
# Planning a columnar query must probe pgcolumnar.options and
# pgcolumnar.projection through their primary keys.
#
# options_pkey is (regclass) and projection_pkey is (storage_id,
# projection_id). The planner looks options up by regclass and projections
# up by storage_id, and both scans passed InvalidOid, so every plan
# sequentially scanned those catalogs. A database with many columnar
# tables pays that on a query that touches one of them.
#
# After one filtered scan of this suite's own table, pg_stat_all_tables
# must show idx_scan > 0 and seq_scan = 0 for both catalogs. The filtered
# scan is its own psql, so the session that wrote the rows is not the
# session being measured.
#
# Usage:  test/catalog_plan_index.sh [PG_CONFIG]

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

q "CREATE EXTENSION IF NOT EXISTS pgcolumnar;" >/dev/null

# margin MARGIN FLOOR -- MARGIN when it falls short of FLOOR, else FLOOR.
#
# AN ARM'S FAILURE MUST SAY WHAT IT MEASURED (#1164, selftest part 540). An arm
# that reduces two numbers to a bare one-or-nought before the comparison reports
# `got [0] want [1]`, which is the word FAILED spelled twice: a reader cannot
# tell whether the two readings were one buffer apart the wrong way or a
# thousand. Comparing the MARGIN against its floor reports the margin itself
# when it falls short.
#
# THE ANTI-PATTERN IS DESCRIBED HERE IN WORDS AND NOT QUOTED, DELIBERATELY.
# Part 540's sweep does not strip comments, and its bracket rule carries no
# "the recorder is on this line" conjunct -- the awk rule beside it does. So a
# comment QUOTING the shape is matched as though it were an arm, and then named
# after whichever recorder call precedes it. Reproduced: a file whose only
# offending text is such a comment yields one finding, named after an innocent
# arm; delete the comment and it goes. That is how an untouched, byte-identical
# storage arm in this file came to be reported as a new offender.
#
# The same helper appears in catalog_delete_index.sh. It belongs in lib.sh the
# moment a third suite wants it; two copies is not yet a population.
margin() { [ "$1" -lt "$2" ] && echo "$1" || echo "$2"; }

# DEFINED HERE, ABOVE THE FIRST RECORDER CALL, AND THE PLACEMENT IS LOAD-BEARING.
# Selftest part 540 folds continuations, remembers the NAME from the last
# recorder call it saw, and attributes any later lossy verdict to it. A helper
# defined mid-file therefore gets blamed on whichever arm happens to precede it:
# with this function further down, the part reported
#
#     a new arm that cannot say what it measured is refused by name:
#       got [catalog_plan_index: planning a join probed pgcolumnar.storage ...]
#
# naming an arm that is byte-identical to main's and was never touched here.
# Above every recorder call there is no name to inherit. catalog_delete_index.sh
# puts its copy in the same place for the same reason.


# Other columnar tables sit in the same catalogs. A sequential scan of
# options or projection walks their rows too; an index probe does not.
q "CREATE TABLE noise_a (id int) USING pgcolumnar;
   CREATE TABLE noise_b (id int) USING pgcolumnar;
   INSERT INTO noise_a SELECT g FROM generate_series(1,40) g;
   INSERT INTO noise_b SELECT g FROM generate_series(1,60) g;
   CREATE TABLE plan_cat (id int) USING pgcolumnar;
   INSERT INTO plan_cat SELECT g FROM generate_series(1,800) g;" >/dev/null

check_num "premise: the measured table holds its rows" \
	"$(q "SELECT count(*) FROM plan_cat;")" "800"

q "SELECT pg_stat_reset();" >/dev/null
q "SELECT count(*) FROM plan_cat WHERE id > 0;" >/dev/null
q "SELECT pg_stat_force_next_flush();" >/dev/null

check_num "premise: the filtered scan returned every row" \
	"$(q "SELECT count(*) FROM plan_cat WHERE id > 0;")" "800"

# THE FOUR ARMS THAT STOOD HERE ASSERTED THE ACCESS PATH, AND THIS CHANGE FAILS
# THEM (#1217). They read
#
#     planning probed pgcolumnar.options through options_pkey      idx_scan >= 1
#     planning did not sequentially scan pgcolumnar.options        seq_scan == 0
#     ... and the same two for projection
#
# On this fixture both catalogs are EMPTY, so the size check declines both probes
# and those readings become idx_scan=0 seq_scan=2 and idx_scan=0 seq_scan=4. They
# fail against a build that made planning strictly cheaper -- 300 buffers over 50
# plans down to nothing -- which is a guard firing on correct code, and a guard
# that fires on correct code gets switched off.
#
# The same defect #1213 removed from catalog_delete_index.sh, one level over. It
# was PREDICTED before this change was written rather than found by running it:
# the prediction named these four arms, the direction each would move, and the
# storage arms below as the ones that must NOT move. All three held.
#
# What replaces them is the work at the default against the work of each extreme
# setting, measured where each claim is measurable: above, on the empty catalogs
# this change is about, and below, on a populated one.

# ---- and pgcolumnar.storage, through storage_pkey (#1237) -------------------
#
# Two readers key on storage_id and both passed InvalidOid, so both scanned the
# catalog sequentially on the one column storage_pkey is a UNIQUE btree over:
#
#     PgColumnarGetSortedInfo           PLANNING, via pgcolumnar_sorted_pathkeys
#     PgColumnarCheckNativeFormatVersion EXECUTION, once per relation scanned
#
# THE TWO SHAPES ARE DIFFERENT ARMS BECAUSE THEY REACH DIFFERENT CODE, and a
# single shape cannot tell them apart. Measured on the unfixed tree, scans of
# pgcolumnar.storage per planned query by shape:
#
#     count(*), no qual                  0 at planning, 1 at execution
#     qual on a plain column             1
#     qual on a column with a projection 4
#     two columnar relations, one qual   4   <- 2 limit lookups + 2 sorted lookups
#
# A `count(*)` never reaches the row-group-limit lookup, so the ONLY storage
# access it makes is the format-version one. That makes seq_scan == 0 a clean
# reading for that site and nothing else.
#
# THE JOIN IS MEASURED ON idx_scan RATHER THAN seq_scan, deliberately. Its two
# remaining sequential scans come from pgcolumnar_written_stripe_row_limit,
# which keys on relation_oid and has NO index to name -- that is #1210 and
# #1211 and is not this change. Asserting seq_scan == 0 there would fail for a
# defect this change does not claim to fix, and asserting seq_scan == 2 would
# pin a number that #1210 is expected to move.
q "CREATE TABLE plan_cat_j (id int) USING pgcolumnar;
   INSERT INTO plan_cat_j SELECT g FROM generate_series(1,800) g;" >/dev/null

storage_stat() {	# -> "idx_scan seq_scan"
	q "SELECT coalesce(idx_scan,0)::text || ' ' || coalesce(seq_scan,0)::text
		FROM pg_stat_all_tables
		WHERE schemaname = 'pgcolumnar' AND relname = 'storage';"
}

q "SELECT pg_stat_reset();" >/dev/null
q "SELECT count(*) FROM plan_cat;" >/dev/null
q "SELECT pg_stat_force_next_flush();" >/dev/null
st="$(storage_stat)"
st_idx="${st%% *}"
st_seq="${st##* }"
echo "-- storage after count(*)  idx_scan=$st_idx seq_scan=$st_seq"

check_num "premise: a no-qual count over a columnar table touched storage at all" \
	"$(if [ "$((st_idx + st_seq))" -ge 1 ]; then echo 1; else echo 0; fi)" "1"
check_num "a no-qual count did not sequentially scan pgcolumnar.storage" \
	"$st_seq" "0"

q "SELECT pg_stat_reset();" >/dev/null
q "SELECT count(*) FROM plan_cat a JOIN plan_cat_j b ON a.id = b.id WHERE a.id > 0;" >/dev/null
q "SELECT pg_stat_force_next_flush();" >/dev/null
sj="$(storage_stat)"
sj_idx="${sj%% *}"
sj_seq="${sj##* }"
echo "-- storage after a two-relation join  idx_scan=$sj_idx seq_scan=$sj_seq"

# THESE TWO WERE LOSSY AND PART 540 COULD NOT SEE IT (#1255). Both branches were
# constants and `sj_idx` was discarded, so a failure printed `got [0] want [1]`
# and the count went with it -- the #1164 symptom exactly. 540 misses them
# because both of its matchers require the `&&` spelling and these used the
# shell `if/then/else` one, so the corpus tracked them as clean rather than as
# debt. Found by @OffgridwithJD while checking an arm I had called innocent
# because it was byte-identical to main's; it was, and that was not the reason
# it went untracked.
#
# The `count(*)` arm above is left alone on purpose: `-ge 1` is inside 540's own
# determinate() carve-out, because exactly one value fails and `got [0]` does
# say which state was reached.
check_num "premise: the join reached storage more than once" \
	"$(margin "$((sj_idx + sj_seq))" 2)" "2"
check_num "planning a join probed pgcolumnar.storage through storage_pkey" \
	"$(margin "$sj_idx" 2)" "2"

# ---------------------------------------------------------------------------
# THE DEFAULT CONFIGURATION, where the probe cannot win at any size (#1217)
#
# `pgcolumnar.options` gets a row only when set_options is called, and
# `pgcolumnar.projection` only when a projection is added. An installation doing
# neither has BOTH EMPTY -- and that is the default. The fixture above is such an
# installation: it creates three columnar tables and calls neither.
#
# At zero rows the heap being scanned is zero pages, so the scan the probe
# replaces is LITERALLY FREE and the probe cannot win however large the database
# grows. Measured on main 6c3a9510, 50 plans, at 10, 200 and 1000 columnar
# tables alike:
#
#     options     heap=0  idx=100      projection  heap=0  idx=200
#
# Six index buffers per plan, no heap work at all, and flat in the table count
# because there is nothing to scan more of. There is no crossover to be above.
#
# THESE ARMS ASSERT THE WORK, not the access path, for the reason the four arms
# above do not: a claim about WHICH path was taken cannot tell a revert from an
# improvement. See #1213, where arms of that shape failed 13 of 21 against a
# build that was cheaper at every size.
# ---------------------------------------------------------------------------

PLAN_CATS="'options','projection'"

# plan_work N [INDEX_MIN_BLOCKS] -- buffers options and projection serve while
# the same query is planned N times, heap and index both.
#
# N plans rather than one, because the effect is per-plan and six buffers is too
# small a base to divide into. Fifty makes it three hundred against nothing.
plan_work() {
	local n="$1" set_clause="" i body=""
	[ $# -ge 2 ] && set_clause="SET pgcolumnar.index_min_blocks = $2; "
	for ((i = 0; i < n; i++)); do
		body="${body}EXPLAIN (COSTS OFF) SELECT count(*) FROM plan_cat WHERE id > 0;"
	done
	q "SELECT pg_stat_reset();" >/dev/null
	q "${set_clause}${body}" >/dev/null
	q "SELECT pg_stat_force_next_flush();" >/dev/null
	q "SELECT coalesce(sum(heap_blks_read + heap_blks_hit
			       + coalesce(idx_blks_read,0) + coalesce(idx_blks_hit,0)), 0)
		FROM pg_statio_all_tables
		WHERE schemaname = 'pgcolumnar' AND relname IN ($PLAN_CATS);"
}

# THE PREMISE THE WHOLE SECTION RESTS ON. If either catalog had rows, the heap
# would not be free and the arms below would be about a different claim.
check_num "premise: this fixture is the default configuration, both catalogs empty" 	"$(q "SELECT (SELECT count(*) FROM pgcolumnar.options)
		   + (SELECT count(*) FROM pgcolumnar.projection)
		   + (pg_relation_size('pgcolumnar.options') / 8192)
		   + (pg_relation_size('pgcolumnar.projection') / 8192);")" "0"

plan_default="$(plan_work 50)"
plan_probe="$(plan_work 50 0)"
echo "-- 50 plans: default=$plan_default  probe-always=$plan_probe"

# AN ARM EXPECTING ZERO IS OWED A PREMISE THAT ANYTHING WAS MEASURED. A run that
# planned nothing reports 0 exactly as loudly as one that planned fifty times
# for free, so the forced-probe reading is what says the instrument was working.
check_num "premise: forcing the probe costs something, so the instrument measured" 	"$(margin "$plan_probe" 50)" "50"

check_num "planning costs less than probing both catalogs would" 	"$(margin "$((plan_probe - plan_default))" 1)" "1"

# AND THE STRONG FORM, which is what "returns to its exact pre-#1198 number"
# means: a zero-page heap costs nothing to read, so planning should touch these
# two catalogs not at all.
check_num "and touches the empty catalogs not at all" 	"$plan_default" "0"

# ---------------------------------------------------------------------------
# AND THE PROBE IS STILL TAKEN WHERE IT PAYS (#1217)
#
# Declining on an empty catalog is only half the claim: a size check that
# declined everything would pass every arm above. So populate one catalog past
# the threshold and require the default to beat reading it whole.
#
# THREE HUNDRED PROJECTIONS ON A NOISE TABLE, AND ONE ON THE MEASURED TABLE.
# `pgcolumnar.projection` takes a row per projection, so one table carries the
# catalog to seven pages in a loop; `pgcolumnar.options` takes one row per
# columnar TABLE and would need about four hundred of them to pass three pages.
# That asymmetry is why this phase drives `projection`.
#
# THE BULK GOES ON THE NOISE TABLE, AND THE FIRST DRAFT PUT IT ON THE MEASURED
# ONE. That fixture read default=1501 against read-whole=1050 -- the probe
# LOSING at seven pages -- and the fixture was what was wrong. A probe's cost
# scales with the number of rows MATCHING ITS KEY, not with the size of the
# catalog; three hundred projections on the measured table means every row
# matches and the probe must return all of them. Scanning seven pages then wins,
# and would have been recorded as "the threshold is wrong for the planner path".
#
# The shape this change is about is the opposite one: a catalog made large by
# OTHER tables' rows, where the probe returns one row and the scan walks
# everything. That is what the noise table builds.
#
# NAMING THE GAP RATHER THAN IMPLYING COVERAGE. The five `options_pkey` sites are
# covered by the arms above, which show the check DECLINING, and not by an arm
# showing it take the probe. They run the same helper as the two
# `projection_pkey` sites, which are covered both ways.
# ---------------------------------------------------------------------------

q "DO \$do\$ BEGIN FOR i IN 1..300 LOOP
     PERFORM pgcolumnar.add_projection('noise_a', 'pp' || i, ARRAY['id'], ARRAY['id']);
   END LOOP; END \$do\$;
   SELECT pgcolumnar.add_projection('plan_cat', 'own', ARRAY['id'], ARRAY['id']);" >/dev/null

prj_pages="$(q "SELECT pg_relation_size('pgcolumnar.projection') / 8192;")"
min_blocks="$(q "SHOW pgcolumnar.index_min_blocks;")"
echo "-- projection now $(q "SELECT count(*) FROM pgcolumnar.projection;") rows, ${prj_pages} pages; threshold ${min_blocks}"

# THE PREMISE THIS PHASE CANNOT DO WITHOUT, and it is derived from the setting
# rather than typed. Below the threshold the default declines the probe and
# reads the heap -- which is what the other reading does too, so both come back
# equal and the arm reports no difference. That reads as "the check is gone" and
# means "the fixture is too small".
check_num "premise: projection is larger than the threshold, so the two paths differ" \
	"$(margin "$((prj_pages - min_blocks))" 1)" "1"

# AND THE MEASURED TABLE'S OWN SHARE OF IT MUST BE SMALL, or the probe returns
# most of the catalog and the comparison is about something else. This is the
# premise the first draft of this phase did not have, and it is the one that
# would have caught its fixture.
own_rows="$(q "SELECT count(*) FROM pgcolumnar.projection p
	JOIN pgcolumnar.storage s USING (storage_id)
	WHERE s.relation_oid = 'plan_cat'::regclass::oid;")"
all_rows="$(q "SELECT count(*) FROM pgcolumnar.projection;")"
echo "-- the measured table owns $own_rows of $all_rows projection rows"

# A SHARE, NOT A COUNT. The first version of this asserted exactly 1 and read 2,
# because add_projection writes more than one row per projection -- which is a
# fact about the function, not about the claim. What the arm needs is that the
# probe returns a SMALL PART of the catalog; a tenth is far looser than the
# fixture and still refuses the shape that broke the first draft, where the
# measured table owned all of it.
check_num "premise: the measured table owns a small share of that catalog" \
	"$(margin "$((all_rows - own_rows * 10))" 1)" "1"

pop_default="$(plan_work 50)"
pop_whole="$(plan_work 50 2147483647)"
echo "-- 50 plans, projection populated: default=$pop_default  read-whole=$pop_whole"

check_num "premise: reading the populated catalog whole costs something" \
	"$(margin "$pop_whole" 50)" "50"

check_num "with the catalog populated, planning costs less than reading it whole" \
	"$(margin "$((pop_whole - pop_default))" 1)" "1"

# ---------------------------------------------------------------------------
# AND pgcolumnar.storage THROUGH ITS OWN METAPAGE (#1210)
#
# pgcolumnar_written_stripe_row_limit looked the storage row up by
# relation_oid, which has NO index, so every planned query over a columnar
# relation sequentially scanned the catalog. The scan stops at the first match,
# so the cost is the row's POSITION: the oldest columnar relation never pays for
# the catalog behind it and the newest pays for all of it.
#
# relation_oid is also NOT UNIQUE. storage_pkey, on storage_id, is the only
# unique index on the table, and a covering projection gets its OWN storage row
# carrying the BASE table's relation_oid -- so the scan finds two rows and takes
# whichever heap order hands it first. Measured on the unfixed tree: a table
# written at stripe_row_limit 150000 with a projection added at 7000 resolved to
# 150000 with the base row first and 7000 with it second, no value altered in
# between. An index on relation_oid cannot fix that, because a non-unique index
# returns the same ambiguous pair in index order instead of heap order.
#
# The fix resolves through the relation's metapage and probes storage_pkey,
# which is exact.
#
# NOT MEASURED THROUGH PLAN COST, and that is a finding rather than a choice.
# On the unfixed tree the resolved limit flips 150000 -> 3000 while
# EXPLAIN's total cost stays byte-identical at 1203.00 on a sequential shape and
# 12.49 on an indexed one. The wrong answer is INVISIBLE in the plan, so an arm
# comparing costs would pass on both trees.
# EIGHT HUNDRED FILL TABLES, AND THE NUMBER IS NOT THE POINT -- the page count
# is. The arms below need the newest relation's storage row far enough into the
# catalog that a sequential scan to it costs more than the four blocks an index
# probe costs, and `new_page` is asserted against that rather than assumed.
#
# PINNING A PAGE COUNT INSTEAD WOULD BE CHEAPER AND WRONG. Rows per page is a
# function of the row width, so a future column on pgcolumnar.storage would
# silently leave a pinned count describing a smaller catalog than it names, and
# the premises would keep passing against a fixture that no longer separates the
# two routes. Driving the catalog past the threshold and reading back where the
# row landed is the property; the fill count is only how it is reached.
# Raised by @pgcolumnar-review-3d, who declined to look for a cheaper fixture
# for this reason.
fill=800
q "DO \$\$
	DECLARE i int;
	BEGIN
		FOR i IN 1..$fill LOOP
			EXECUTE format('CREATE TABLE stor_f%s (a int, b text) USING pgcolumnar', i);
			EXECUTE format('INSERT INTO stor_f%s VALUES (1, ''z'')', i);
		END LOOP;
	END \$\$;" >/dev/null
q "CREATE TABLE stor_new (a int, b text) USING pgcolumnar;
   INSERT INTO stor_new SELECT g, 'x'||(g%50) FROM generate_series(1,2000) g;
   ANALYZE stor_new, stor_f1;" >/dev/null
q "VACUUM pgcolumnar.storage;" >/dev/null

stor_pages="$(q "SELECT relpages FROM pg_class WHERE oid = 'pgcolumnar.storage'::regclass;")"
new_page="$(q "SELECT (ctid::text::point)[0]::int FROM pgcolumnar.storage
	WHERE relation_oid = 'stor_new'::regclass::oid;")"
old_page="$(q "SELECT (ctid::text::point)[0]::int FROM pgcolumnar.storage
	WHERE relation_oid = 'stor_f1'::regclass::oid;")"
echo "-- pgcolumnar.storage $(q "SELECT count(*) FROM pgcolumnar.storage;") rows, ${stor_pages} pages; stor_new on page ${new_page}, stor_f1 on page ${old_page}"

# THE PREMISE THE POSITION ARM CANNOT DO WITHOUT. If the newest relation's row
# sits near the front of the catalog a sequential scan stops there, both routes
# cost about the same, and the arm reports no difference -- which reads as "the
# scan is gone" and means "the catalog is too small to tell". The probe route
# costs about four blocks, so the scan has to be worth more than that before the
# two can be told apart. Derived from the reading, not typed.
check_num "premise: the newest relation's storage row is far enough in to tell the routes apart" \
	"$(margin "$new_page" 5)" "5"

storage_blks() {	# storage_blks RELNAME -> blocks of pgcolumnar.storage per plan
	q "SELECT pg_stat_reset();" >/dev/null
	q "EXPLAIN (BUFFERS, COSTS OFF) SELECT a, b FROM $1 WHERE a BETWEEN 3 AND 900;" >/dev/null
	q "SELECT pg_stat_force_next_flush();" >/dev/null
	q "SELECT coalesce(heap_blks_read,0) + coalesce(heap_blks_hit,0)
		+ coalesce(idx_blks_read,0) + coalesce(idx_blks_hit,0)
		FROM pg_statio_all_tables
		WHERE schemaname = 'pgcolumnar' AND relname = 'storage';"
}

q "SELECT pg_stat_reset();" >/dev/null
q "EXPLAIN (BUFFERS, COSTS OFF) SELECT a, b FROM stor_new WHERE a BETWEEN 3 AND 900;" >/dev/null
q "SELECT pg_stat_force_next_flush();" >/dev/null
sn="$(storage_stat)"
sn_idx="${sn%% *}"
sn_seq="${sn##* }"
echo "-- planning over the NEWEST columnar table  idx_scan=$sn_idx seq_scan=$sn_seq"

# A ZERO IS ONLY EVIDENCE IF SOMETHING WAS REACHED. A plan that never looks the
# limit up scans nothing, which is the same reading as a plan that probes.
check_num "premise: planning the newest table reached pgcolumnar.storage" \
	"$(margin "$((sn_idx + sn_seq))" 1)" "1"

check_num "planning over the newest columnar table did not sequentially scan pgcolumnar.storage" \
	"$sn_seq" "0"

blk_new="$(storage_blks stor_new)"
blk_old="$(storage_blks stor_f1)"
echo "-- blocks of pgcolumnar.storage per plan  newest=$blk_new  oldest=$blk_old"

# BOTH READINGS NEED A FLOOR, not just the one the claim is about. The first
# draft of this arm read oldest=0 -- the fill tables had no column b, so the
# EXPLAIN errored and the plan never happened. Zero blocks then looks like a
# free lookup and makes the comparison arithmetic on nothing.
check_num "premise: planning the oldest table reached pgcolumnar.storage too" \
	"$(margin "$blk_old" 1)" "1"

# THE TWO READINGS MUST BE FAR ENOUGH APART TO MEAN SOMETHING. If the newest
# and the oldest sit on nearby pages the scan costs the same either way and the
# arm passes on the unfixed tree.
check_num "premise: the two relations are far enough apart in the catalog" \
	"$(margin "$((new_page - old_page - 3))" 1)" "1"

# POSITION, NOT SIZE. This is the claim the issue was filed on and the one an
# index probe answers: the newest relation must not cost more than the oldest
# just for having been created later. Measured on the unfixed tree at 706 rows
# over 6 pages: 6 blocks for the newest against 4 for the oldest, and the gap
# grows with the catalog because only the newest reading does.
check_num "the newest columnar table costs no more catalog work than the oldest" \
	"$(margin "$((blk_old + 2 - blk_new))" 1)" "1"

# ---------------------------------------------------------------------------
# AND THE ANSWER MUST NOT DEPEND ON HEAP ORDER (#1210)
#
# The two arms above measure the WORK. This one measures the ANSWER, which is
# what the change is actually for.
#
# relation_oid is not unique: a covering projection gets its OWN storage row
# carrying the BASE table's relation_oid, so a sequential scan keyed on that
# column returns whichever row the heap hands back first. Both rows carry a
# row_group_limit and they need not agree -- a table written under one
# stripe_row_limit and a projection added under another is ordinary use, not a
# contrivance.
#
# THE MUTATION ALTERS NO VALUE. `SET row_group_limit = row_group_limit` writes
# the base row back unchanged, which moves it later in the heap exactly as any
# real write to it would. Nothing about the data changes; only which row the
# scan meets first.
#
# THE COST IS THE OBSERVABLE, and that took a correction to arrive at. A first
# attempt read a stable cost on a 30,000-row table with no useful index and
# concluded the wrong limit was invisible in the plan. It was the fixture: those
# shapes never reach a group-sensitive term. All three call sites turn the limit
# into a GROUP COUNT -- 150000 against 3000 over 20,000 rows is 1 group against
# 7 -- and on a shape that reaches one, the plan is priced 301.29 against 43.86.
# Reported by @pgcolumnar-review-3d, who refused the claim from the mechanism
# rather than from the numbers.
q "SET pgcolumnar.stripe_row_limit = 150000;
   CREATE TABLE stor_proj (a int, b text) USING pgcolumnar;
   INSERT INTO stor_proj SELECT g, 'x'||(g%50) FROM generate_series(1,20000) g;" >/dev/null
q "SET pgcolumnar.stripe_row_limit = 3000;
   SELECT pgcolumnar.add_projection('stor_proj', 'cov_b', '{b}', '{b}');" >/dev/null
q "ANALYZE stor_proj;" >/dev/null

proj_cost() {	# -> the plan's total cost, as EXPLAIN prints it
	q "EXPLAIN (COSTS ON) SELECT b FROM stor_proj WHERE b = 'x7';" \
		| sed -n '1s/.*\.\.\([0-9.]*\) rows.*/\1/p'
}
base_sid="$(q "SELECT pgcolumnar.get_storage_id('stor_proj'::regclass);")"
limits="$(q "SELECT count(DISTINCT row_group_limit) FROM pgcolumnar.storage
	WHERE relation_oid = 'stor_proj'::regclass::oid;")"
rows_for="$(q "SELECT count(*) FROM pgcolumnar.storage
	WHERE relation_oid = 'stor_proj'::regclass::oid;")"
echo "-- stor_proj owns $rows_for storage rows carrying $limits distinct row_group_limit values"

# WITHOUT TWO ROWS THERE IS NO AMBIGUITY, and without two DIFFERENT limits the
# ambiguity cannot be seen. Both are premises, and both are read from the
# catalog rather than assumed from the fixture's DDL.
check_num "premise: a covering projection gave the table a second storage row" \
	"$rows_for" "2"
check_num "premise: the two storage rows disagree about row_group_limit" \
	"$limits" "2"

# THE PREMISE THAT STOPS THIS ARM GOING QUIET, and it is the one a future
# change is most likely to break silently. The arm below asserts two costs are
# EQUAL, so it also passes if this query shape stops reaching a group-sensitive
# term at all -- which is exactly the fixture failure that hid this defect from
# the first attempt. Establish, on this tree, that the cost really does depend
# on the limit: set the base row's limit to the projection's value and require
# the plan to be priced differently. Measured here as 301.29 against 43.86.
#
# RUN FIRST AND RESTORED, because it writes the row the arm below measures.
cost_hi="$(proj_cost)"
q "UPDATE pgcolumnar.storage SET row_group_limit = 3000 WHERE storage_id = $base_sid;" >/dev/null
cost_lo="$(proj_cost)"
q "UPDATE pgcolumnar.storage SET row_group_limit = 150000 WHERE storage_id = $base_sid;" >/dev/null
echo "-- the shape is group-sensitive: limit 150000 -> $cost_hi, limit 3000 -> $cost_lo"

check_num "premise: this query shape is priced differently under the two limits" \
	"$(if [ "$cost_hi" = "$cost_lo" ]; then echo 0; else echo 1; fi)" "1"

# PUT THE BASE ROW FIRST, by writing the OTHER row so it moves to the end. Which
# row the premises above left in front is not something to assume.
proj_sid="$(q "SELECT storage_id FROM pgcolumnar.storage
	WHERE relation_oid = 'stor_proj'::regclass::oid AND storage_id <> $base_sid;")"
q "UPDATE pgcolumnar.storage SET row_group_limit = row_group_limit
	WHERE storage_id = $proj_sid;" >/dev/null

first_before="$(q "SELECT CASE WHEN storage_id = $base_sid THEN 'base' ELSE 'proj' END
	FROM pgcolumnar.storage WHERE relation_oid = 'stor_proj'::regclass::oid
	ORDER BY ctid LIMIT 1;")"
ctid_before="$(q "SELECT ctid FROM pgcolumnar.storage WHERE storage_id = $base_sid;")"
cost_before="$(proj_cost)"

q "UPDATE pgcolumnar.storage SET row_group_limit = row_group_limit
	WHERE storage_id = $base_sid;" >/dev/null

ctid_after="$(q "SELECT ctid FROM pgcolumnar.storage WHERE storage_id = $base_sid;")"
first_after="$(q "SELECT CASE WHEN storage_id = $base_sid THEN 'base' ELSE 'proj' END
	FROM pgcolumnar.storage WHERE relation_oid = 'stor_proj'::regclass::oid
	ORDER BY ctid LIMIT 1;")"
cost_after="$(proj_cost)"
echo "-- base row $ctid_before -> $ctid_after;  first in the heap $first_before -> $first_after;  cost $cost_before -> $cost_after"

# THE MUTATION MUST HAVE DONE SOMETHING, and each of these is a separate way for
# it to have done nothing. A HOT update that rewrites in place, a fixture that
# lost its second row, or a base row that was already second all leave the two
# readings coming from one heap order, and the arm then passes without having
# asked anything.
check_text "premise: the no-op update moved the base storage row" \
	"$(if [ "$ctid_before" = "$ctid_after" ]; then echo same; else echo moved; fi)" "moved"
check_text "premise: the base row was first before the update" "$first_before" "base"
check_text "premise: the update put the projection row in front" "$first_after" "proj"

check_text "the planned cost does not depend on which storage row the heap returns first" \
	"$cost_after" "$cost_before"

pgc_summary
