#!/usr/bin/env bash
#
# Retiring a row group must not cost more because the database holds other
# columnar tables.
#
# THE BUG. delete_group_rows() opens its catalog from a `const char *tableName`
# PARAMETER and PgColumnarDeleteGroupMetadata calls it five times, for
# delete_vector, column_chunk, zone_map, bloom and row_group. One
# systable_beginscan in the source was therefore five sequential reads per
# retired group at run time, and two more sit beside it on the compaction path.
# The pgcolumnar metadata catalogs are SHARED by every columnar table in the
# database, so each of those reads was charged for every other table's rows.
#
# THE FIX IS NOT "USE THE INDEX". It is "ask the catalog how big it is, and use
# the index when that is the cheaper read". A probe is a btree descent plus a
# heap fetch plus two catcache lookups, which on a catalog of a few pages is
# MORE work than reading the whole thing; on a large one it is far less. Both
# sizes occur in one installation, because the catalogs are shared. So this
# suite has to show the choice being made well at BOTH ends, which is why it
# measures at three catalog sizes rather than one.
#
# WHAT IT ASSERTS, AND WHAT IT DELIBERATELY DOES NOT. It asserts the WORK --
# buffers served out of the six catalogs, heap and index, from
# pg_statio_all_tables -- and never the access path. An earlier version of this
# file asserted `seq_scan = 0` and `idx_scan >= 1` on each catalog. That is a
# claim about WHICH PATH WAS TAKEN, and it is wrong twice over:
#
#   1. It cannot tell a revert from an improvement. Run against the build that
#      chooses by catalog size -- cheaper than the always-probe version at every
#      size measured -- those arms failed 13 of 21, the same 13 a full revert
#      reddens. A guard that fires on correct code gets switched off, and the
#      rule goes with it.
#   2. A scan count is not a cost. Counting scans is how this change came to
#      report a saving it had measured in the wrong unit (#1213).
#
# NO CONSTANT ANYWHERE. Every buffer count is compared against another buffer
# count taken in the same run, on the same cluster, from the same build.
# `pgcolumnar.index_min_blocks` is the setting that decides the path -- 0 probes
# every catalog, a very large value reads every one whole -- so the same
# compaction is measured three ways and the three are compared to each other.
# This matters because the numbers are not the same on every major: the same
# fixture reads 848 buffers on PG17, 845 on PG15 and 895 on PG19. Any number
# typed into this file from a measurement someone once took would be wrong on
# two majors out of three.
#
# Usage:  test/catalog_delete_index.sh [PG_CONFIG]

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

CATS="'delete_vector','column_chunk','zone_map','bloom','row_group','free_space'"

# HOW FAR APART TWO READINGS MUST BE BEFORE THIS FILE CALLS THE DIFFERENCE A
# RESULT, in parts per thousand of the reading they are compared against.
#
# MEASURED, NOT CHOSEN, AND THE FIRST TWO ATTEMPTS AT IT WERE BOTH WRONG.
#
# Every claim compares two compactions of two different tables run one after
# another, and that is not a controlled comparison: compaction WRITES to
# row_group and free_space, so the next compaction reads more of them. Two
# readings taken from identical code drift apart.
#
#   A floor of ONE BUFFER let a full revert through. Fifteen arms of sixteen
#   passed against code with the fix removed, carried by 2 to 8 buffers of
#   drift.
#
#   A floor of TEN PARTS PER THOUSAND let it through too. Under a full revert
#   the drift reached 14 to 16 parts per thousand -- above the floor -- and two
#   arms of eighteen reddened. Worse, the two arms it was meant to protect were
#   themselves worth only 22 and 26, so they sat inside the drift. They were not
#   measuring the fix; they were measuring the sequence.
#
# The floor is now 100, and each claim is made at a catalog size where it is
# worth several times that. Measured, this build against the three mutations,
# in parts per thousand:
#
#     arm                    real   revert   probe-always   default replaced
#     P1 (6 pages)            284        0             12        284 (passes)
#     A1 (22 pages)           237       -1             -2         -1
#     B1 (76 pages)          1823        0             -1          0
#     growth (A to B)         960        1             22          1
#     vacuum                  714        0              0          0
#
#     measured drift            -      1-7           2-18        1-7
#
# Every claim clears the floor by at least 2.4x; every mutation falls at least
# 4.5x below it. A full revert and a deleted size check each redden all five.
# Replacing the default with one that never probes reddens four: P1 passes
# there, correctly, because at six catalog pages declining every probe IS the
# cheaper read -- that build is wrong at the other end, and four arms say so.
#
# TWO ARMS WERE DELETED RATHER THAN RESCUED. "The default does less work than
# probing every catalog" was worth 22 parts per thousand at phase A and 26 at
# phase B -- inside the drift, so unmeasurable there. It survives as P1, at a
# catalog size where it is worth 284. What a fixture cannot measure, this file
# does not assert.
FLOOR_PERMILLE=100

q "CREATE EXTENSION IF NOT EXISTS pgcolumnar;" >/dev/null

# WHICH BUILD KIND THIS RUN MEASURED, printed rather than asserted.
#
# Two of the eight converted scan sites are in PgColumnarCheckFreeSpaceNoOverlap,
# which is assert-only. On a release build they do not execute, so every arm
# below is a WEAKER claim there: it says nothing about those two sites rather
# than clearing them. A green on `debug_assertions = off` is not the same
# statement as a green on `on`.
#
# That distinction cost real time. The probe run that closed the account for
# #1207 was on a release build and reported the compaction path FULLY CLEAN,
# while the assert-enabled suite still showed one sequential scan on each of two
# catalogs. Nothing in the measurement said which build it was, so the zero read
# as an answer rather than as a partial one. Suggested by @OffgridwithJD.
#
# Printed and NOT made an arm on purpose: it records the condition the run
# happened in, and breaking the code under test cannot change it.
echo "-- debug_assertions=$(q "SHOW debug_assertions;")  (off = the two assert-only sites did not run)"
echo "-- pgcolumnar.index_min_blocks=$(q "SHOW pgcolumnar.index_min_blocks;")  (the shipped default this run measures)"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# make_target TABLE GROUPS -- GROUPS 1000-row groups, every other one emptied.
#
# Each measurement gets its own table. A compaction retires its groups once, so
# a second reading of the same table would measure a compaction that found
# nothing left to do -- which reports a small number for the same reason a fast
# one does.
make_target() {
	q "CREATE TABLE $1 (id int, v int) USING pgcolumnar;
	   SELECT pgcolumnar.set_options('$1', stripe_row_limit => 1000);
	   INSERT INTO $1 SELECT g, g % 100 FROM generate_series(1,$(($2 * 1000))) g;
	   DELETE FROM $1 WHERE ((id - 1) / 1000) % 2 = 0;" >/dev/null
}

groups_of() {
	q "SELECT count(*) FROM pgcolumnar.row_group r
		JOIN pgcolumnar.storage s USING (storage_id)
		WHERE s.relation_oid = '$1'::regclass::oid;"
}

catpages() {
	q "SELECT coalesce(sum(pg_relation_size('pgcolumnar.' || relname) / 8192), 0)
		FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
		WHERE n.nspname = 'pgcolumnar' AND relname IN ($CATS);"
}

# compact_work TABLE [INDEX_MIN_BLOCKS] -- buffers the six catalogs served
# while TABLE was compacted. Heap AND index blocks: counting only the heap
# would make a probe look free, which is the error this file exists to avoid.
#
# The SET and the compaction share one q, so they share a backend. The harness
# runs every other statement in a fresh backend, which flushes its statistics on
# exit, so the reset and the reading need no flush of their own.
compact_work() {
	local set_clause=""
	[ $# -ge 2 ] && set_clause="SET pgcolumnar.index_min_blocks = $2; "
	q "SELECT pg_stat_reset();" >/dev/null
	q "${set_clause}SELECT pgcolumnar.compact('$1');" >/dev/null
	q "SELECT pg_stat_force_next_flush();" >/dev/null
	q "SELECT coalesce(sum(heap_blks_read + heap_blks_hit
				   + coalesce(idx_blks_read,0) + coalesce(idx_blks_hit,0)), 0)
		FROM pg_statio_all_tables
		WHERE schemaname = 'pgcolumnar' AND relname IN ($CATS);"
}

# cat_detail -- the per-catalog breakdown of the reading compact_work just took.
# Valid only until the next reset, so it is called immediately after.
#
# It is diagnosis, not decoration. It is what showed that under a full revert
# five of the six catalogs return identical numbers across every reading and the
# whole drift is row_group -- the one catalog the compaction writes to.
cat_detail() {
	q "SELECT string_agg(relname || '=' ||
			(heap_blks_read + heap_blks_hit
			 + coalesce(idx_blks_read,0) + coalesce(idx_blks_hit,0))::text,
			' ' ORDER BY relname)
		FROM pg_statio_all_tables
		WHERE schemaname = 'pgcolumnar' AND relname IN ($CATS);"
}

# permille PART WHOLE. A WHOLE of zero gives 0, which fails every arm rather
# than dividing by it.
permille() { [ "$2" -gt 0 ] && echo $(( $1 * 1000 / $2 )) || echo 0; }
abs()      { [ "$1" -lt 0 ] && echo $(( -$1 )) || echo "$1"; }

# margin MARGIN FLOOR -- MARGIN when it falls short of FLOOR, else FLOOR. An arm
# comparing this against FLOOR passes exactly when MARGIN >= FLOOR, and a
# failing one reports the margin it measured. A plain 1-or-0 arm would report
# `got [0] want [1]` whether the readings were four parts per thousand the wrong
# way or four hundred, and the size of the gap is most of the diagnosis.
margin() { [ "$1" -lt "$2" ] && echo "$1" || echo "$2"; }

# THE CONTROL IS COMPACTED FIRST, NEXT TO THE DEFAULT, in every phase.
#
# Drift accumulates with distance, so a control three steps from the default
# measures three steps of it and condemns a claim exposed to one. It did: with
# the control last, phase B reported 32 parts per thousand of noise against a
# margin of 31. Adjacent, the same phase reports 2.
#
# NO SEPARATE NOISE TABLE. Each phase already holds three storages, so no arm
# can pass on a catalog that happens to hold only one.

# phase PH PREFIX GROUPS OTHER OTHER_LABEL
#
# Measure one phase and set PH_DEFAULT, PH_OTHER and PH_PAGES for the caller.
#
# Three identical tables: a control and the measured one at the shipped default,
# then one at OTHER. Each measurement needs its own table because a compaction
# retires its groups once.
#
# THE CONTROL IS COMPACTED FIRST, NEXT TO THE DEFAULT. Drift accumulates with
# distance, so a control three steps from the default measures three steps of it
# and condemns a claim exposed to one. It did: with the control last, one phase
# reported 32 parts per thousand of noise against a margin of 31. Adjacent, the
# same phase reports 2.
#
# NO SEPARATE NOISE TABLE. Each phase already holds three storages, so no arm can
# pass on a catalog that happens to hold only one.
phase() {
	local ph="$1" prefix="$2" groups="$3" other="$4" label="$5" carried="${6:-0}"
	local retired=$(( groups - groups / 2 ))
	local surviving=$(( (groups / 2) * 1000 ))
	local t_control="${prefix}_control" t_default="${prefix}_default" t_other="${prefix}_other"
	local control default other_work noise

	for t in "$t_control" "$t_default" "$t_other"; do make_target "$t" "$groups"; done
	PH_PAGES="$(catpages)"

	check_num "premise: the three phase $ph targets are the same fixture" \
		"$(( $(groups_of "$t_control") + $(groups_of "$t_default") \
		     + $(groups_of "$t_other") ))" "$(( groups * 3 ))"

	# THE PREMISE THAT CAUGHT A REAL FIXTURE DEFECT IN THE PORT. Phase 0's claim
	# is about a catalog of a few pages, and a large one reports a SMALLER
	# MARGIN rather than an error -- 65 parts per thousand instead of 223, which
	# reads as a weak result and not as a broken fixture. Counting the columnar
	# relations says which it is.
	#
	# This half gets a cluster of its own so it has always been true here. The
	# pytest half gets a private SCHEMA and the pgcolumnar catalogs are per
	# DATABASE, so run after the other fifty-one cluster files its phase 0 saw
	# thirty-nine catalog pages instead of six and P1 fell under the floor. CI
	# found it; a local run of one file could not.
	check_num "premise: the phase $ph catalogs hold only this file's tables" \
		"$(( $(q "SELECT count(*) FROM pg_class c JOIN pg_am a ON a.oid = c.relam
			WHERE a.amname = 'pgcolumnar';") - carried ))" "3"

	control="$(compact_work "$t_control")";      echo "--   control:      $(cat_detail)"
	default="$(compact_work "$t_default")";      echo "--   default:      $(cat_detail)"
	other_work="$(compact_work "$t_other" "$other")"; echo "--   $label: $(cat_detail)"
	noise="$(permille "$(abs $((control - default)))" "$default")"

	echo "-- phase $ph  catalog pages=$PH_PAGES  work: control=$control default=$default $label=$other_work"
	echo "-- phase $ph  permille vs the default: $label=$(permille "$((other_work - default))" "$default") noise=$noise"

	# DERIVED FROM `groups`, NOT ASSUMED EVEN. make_target empties every other
	# group, so an odd count retires the larger half.
	check_num "premise: the phase $ph compaction retired the emptied groups" \
		"$(( groups - $(groups_of "$t_default") ))" "$retired"
	check_num "premise: the phase $ph compaction kept every surviving row" \
		"$(q "SELECT count(*) FROM $t_default;")" "$surviving"

	# A catalog missing from the reading would read as a catalog nothing touched,
	# which is the answer these arms are looking for.
	check_num "premise: the phase $ph reading covers every catalog the arms name" \
		"$(q "SELECT count(*) FROM pg_statio_all_tables
			WHERE schemaname = 'pgcolumnar' AND relname IN ($CATS);")" "6"
	check_num "premise: the phase $ph reading measured something" \
		"$(margin "$default" 1)" "1"

	# THE ARMS ARE ONLY AS GOOD AS THIS ONE. A third identical table is compacted
	# at the SAME setting as the measured one, so the two readings differ only by
	# where they sit in the sequence. If that difference ever approaches the floor
	# the claims are asserted against, the claims stop meaning anything -- and this
	# says so instead of letting them pass on it.
	check_num "premise: two phase $ph compactions at the same setting agree well inside the floor" \
		"$(margin "$((FLOOR_PERMILLE - noise))" 1)" "1"

	PH_DEFAULT="$default"
	PH_OTHER="$other_work"
}

# ---------------------------------------------------------------------------
# Phase 0: a few catalog pages, where reading one whole is the cheaper path
#
# Ten-group tables, and small on purpose. The claim here -- that probing every
# catalog costs more than choosing -- is worth 284 parts per thousand at six
# catalog pages, 151 at nine and 31 at eighteen, against a drift of 3 to 10
# throughout. It is a real effect that a bigger fixture hides.
# ---------------------------------------------------------------------------

phase 0 del_0 10 0 "probe-always"
w0_default="$PH_DEFAULT"; w0_probe="$PH_OTHER"

check_num "P1 with a few catalog pages the default does less work than probing every one" \
	"$(margin "$(permille "$((w0_probe - w0_default))" "$w0_default")" $FLOOR_PERMILLE)" \
	"$FLOOR_PERMILLE"

# ---------------------------------------------------------------------------
# Phase A: more catalog pages, where probing has started to pay
# ---------------------------------------------------------------------------

phase A del_a 40 2147483647 "read-whole" 3
wa_default="$PH_DEFAULT"; wa_scan="$PH_OTHER"; pages_a="$PH_PAGES"

check_num "A1 with more catalog pages the default does less work than reading every one whole" \
	"$(margin "$(permille "$((wa_scan - wa_default))" "$wa_default")" $FLOOR_PERMILLE)" \
	"$FLOOR_PERMILLE"

# ---------------------------------------------------------------------------
# Phase B: large catalogs
#
# ONE DEEP TABLE, NOT MANY SHALLOW ONES. What makes a sequential read expensive
# is catalog PAGES, not how many tables share the catalogs. 200,000 rows at 1024
# to a group put the six catalogs near eighty pages; reaching the same size with
# one-group tables took a thousand of them and most of a suite's runtime.
# ---------------------------------------------------------------------------

q "CREATE TABLE del_deep (id int, a int, b int, c text) USING pgcolumnar;
   SELECT pgcolumnar.set_options('del_deep', stripe_row_limit => 1024);
   INSERT INTO del_deep SELECT g, g%7, g%13, 'x'||g FROM generate_series(1,200000) g;" >/dev/null

pages_after_deep="$(catpages)"

# THE PREMISE THIS EXPERIMENT NEEDS MOST. The growth arm compares two readings
# taken over catalogs that are supposed to differ in size. If the deep table
# never landed, both phases measure the same fixture and the arm passes while
# proving nothing -- and that is not hypothetical: the sweep that chose the
# shipped default first produced a clean table across seven database sizes in
# which the noise had been eaten by shell quoting. Every row was secretly the
# same database, and the only thing that said so was this quantity, flat at 8
# pages where it should have reached 65.
phase B del_b 40 2147483647 "read-whole" 7
wb_default="$PH_DEFAULT"; wb_scan="$PH_OTHER"; pages_b="$PH_PAGES"

# THE PREMISE THIS EXPERIMENT NEEDS MOST. The growth arm compares two readings
# taken over catalogs that are supposed to differ in size. If the deep table
# never landed, both phases measure the same fixture and the arm passes while
# proving nothing -- and that is not hypothetical: the sweep that chose the
# shipped default first produced a clean table across seven database sizes in
# which the noise had been eaten by shell quoting. Every row was secretly the
# same database, and the only thing that said so was this quantity, flat at 8
# pages where it should have reached 65.
# READ BEFORE PHASE B BUILDS ITS TARGETS, on purpose: this asks whether the
# DEEP TABLE grew the catalogs, and measuring after phase B's own three tables
# exist would fold their growth into the answer and make it true either way.
check_num "premise: the deep table grew the catalogs it is there to grow" \
	"$(margin "$((pages_after_deep - pages_a))" 1)" "1"

check_num "B1 with large catalogs the default does far less work than reading every one whole" \
	"$(margin "$(permille "$((wb_scan - wb_default))" "$wb_default")" $FLOOR_PERMILLE)" \
	"$FLOOR_PERMILLE"

# THE INVARIANT THE ISSUE IS ABOUT, written down as its own arm rather than left
# for a reader to compose out of A1 and B1. It is the sentence the bug report
# would use: retiring a group must not cost more because other tables exist.
growth_default=$((wb_default - wa_default))
growth_scan=$((wb_scan - wa_scan))
echo "-- growth from phase A to phase B: default=$growth_default read-whole=$growth_scan permille=$(permille "$((growth_scan - growth_default))" "$growth_scan")"

check_num "the default's cost grows far less with the database than reading whole does" \
	"$(margin "$(permille "$((growth_scan - growth_default))" "$growth_scan")" $FLOOR_PERMILLE)" \
	"$FLOOR_PERMILLE"

# ---------------------------------------------------------------------------
# The VACUUM path, which reaches a different row_group read.
#
# PgColumnarVMSetVisibleForRelation calls PgColumnarComputeAllVisibleGroups, and
# NOTHING ABOVE REACHES IT. An earlier draft converted that scan and proved
# nothing about it: probing every site during a run of the sections above showed
# PgColumnarComputeAllVisibleGroups never fired, so the change to it rode along
# on arms that could not fail if it were reverted.
#
# THIS ARM HAS NO POSITIONAL CONFOUND, unlike every arm above it, because a
# VACUUM is repeatable where a compaction is not: retiring a group happens once,
# so each compaction needs its own table, but the same table can be vacuumed
# three times. All three readings here come from ONE table, and the only thing
# that differs between them is the setting.
#
# AND IT IS VACUUMED SMALL, ON PURPOSE. The size check is worth the difference
# between reading row_group whole and fetching the vacuumed table's own rows
# from it, so the gap widens as the catalog grows and narrows as the VACUUMED
# table grows. An earlier draft vacuumed a forty-group table and the arm swung
# between 200 and 750 parts per thousand from run to run on a base of ten
# buffers. Two groups against a row_group of fourteen pages is the same claim
# with a base that can carry it.
# ---------------------------------------------------------------------------

# ALTER DATABASE rather than SET, because VACUUM refuses to run inside a
# transaction block and `psql -c "SET ...; VACUUM ..."` is exactly that: one
# simple query, wrapped implicitly. Measured -- the first draft reported 0
# buffers, which reads like a vacuum that touched nothing and was a vacuum that
# never ran:
#     ERROR:  VACUUM cannot run inside a transaction block
# The harness runs every statement in a fresh backend, so a database-level
# setting is how a GUC gets in front of one of them.
#
# Both catalogs come back from ONE reading, so the premise below cannot be
# describing a different moment from the arm it guards.
vac_work() {
	if [ $# -ge 2 ]; then
		q "ALTER DATABASE \"$PGC_DB\" SET pgcolumnar.index_min_blocks = $2;" >/dev/null
	fi
	q "SELECT pg_stat_reset();" >/dev/null
	q "VACUUM $1;" >/dev/null
	q "SELECT pg_stat_force_next_flush();" >/dev/null
	q "SELECT coalesce(max(w) FILTER (WHERE relname = 'row_group'), 0)::text || ' ' ||
		   coalesce(max(w) FILTER (WHERE relname = 'delete_vector'), 0)::text
		FROM (SELECT relname,
			     heap_blks_read + heap_blks_hit
			     + coalesce(idx_blks_read,0) + coalesce(idx_blks_hit,0) AS w
			FROM pg_statio_all_tables
			WHERE schemaname = 'pgcolumnar') t;"
	if [ $# -ge 2 ]; then
		q "ALTER DATABASE \"$PGC_DB\" RESET pgcolumnar.index_min_blocks;" >/dev/null
	fi
}

q "CREATE TABLE del_vac (id int) USING pgcolumnar;
   SELECT pgcolumnar.set_options('del_vac', stripe_row_limit => 1000);
   INSERT INTO del_vac SELECT g FROM generate_series(1,2000) g;
   DELETE FROM del_vac WHERE id % 3 = 0;" >/dev/null

rg_pages="$(q "SELECT pg_relation_size('pgcolumnar.row_group') / 8192;")"
min_blocks="$(q "SHOW pgcolumnar.index_min_blocks;")"
echo "-- row_group pages=$rg_pages, threshold=$min_blocks"

# THE PREMISE THE ARM BELOW CANNOT DO WITHOUT, and it is derived from the
# setting rather than typed. Below the threshold the default declines the probe
# and reads row_group whole -- which is what the other reading does too, so both
# come back equal and the arm reports 0. That reads as "the fix is gone" and
# means "the fixture is too small". The port failed exactly that way the moment
# its fixture was corrected to a private database.
check_num "premise: row_group is larger than the threshold, so the two paths differ" \
	"$(margin "$((rg_pages - min_blocks))" 1)" "1"

v_d="$(vac_work del_vac)"
v_c="$(vac_work del_vac)"
v_s="$(vac_work del_vac 2147483647)"
v_default="${v_d%% *}"; v_dv="${v_d##* }"
v_control="${v_c%% *}"
v_scan="${v_s%% *}"
noise_v="$(permille "$(abs $((v_control - v_default)))" "$v_default")"
echo "-- VACUUM row_group work: default=$v_default control=$v_control read-whole=$v_scan permille=$(permille "$((v_scan - v_default))" "$v_default") noise=$noise_v  (delete_vector at the default=$v_dv)"

# THE PREMISE IS NOT THE CLAIM. It reads delete_vector -- a DIFFERENT catalog
# from the one the arm is about -- so it cannot be satisfied by whatever makes
# that arm pass.
#
# TWO MORE OBVIOUS PREMISES WERE MEASURED AND ARE BOTH THE WRONG QUANTITY:
#
#   relallvisible stays 0 on this fixture however many times the table is
#   vacuumed (measured at six consecutive vacuums), and 0 again on a table with
#   no deletes at all. An arm resting on it would have refused a vacuum that HAD
#   reached the visibility-map path.
#
#   vacuum_count and last_vacuum in pg_stat_all_tables stay 0 and NULL for a
#   columnar table, because this table access method's vacuum does not report
#   through them. "The vacuum did not run" and "the counter cannot see this
#   vacuum" are the same reading.
check_num "premise: the vacuum walked this table's groups" \
	"$(margin "$v_dv" 1)" "1"
check_num "premise: two vacuums of the same table at the same setting agree well inside the floor" \
	"$(margin "$((FLOOR_PERMILLE - noise_v))" 1)" "1"

check_num "the vacuum's default does less row_group work than reading it whole" \
	"$(margin "$(permille "$((v_scan - v_default))" "$v_default")" $FLOOR_PERMILLE)" \
	"$FLOOR_PERMILLE"

# ---- the DROP path: delete_rows_by_storage_id, seven catalogs (#1207) -------
#
# THIS FILE DID NOT COVER THE CONVERSION IT LOOKS LIKE IT COVERS. Everything
# above drives `delete_group_rows` (columnar_metadata.c:745), the retire path.
# The seven-catalog sweep is `delete_rows_by_storage_id` (:1919), reached only
# from PgColumnarDeleteMetadata on DROP and TRUNCATE -- and this suite contained
# no DROP TABLE at all. Across the whole corpus no suite paired a catalog-work
# assertion with a DROP, so seq_scan 2 -> 0 lived only in a PR body, which
# nothing executes. Reported by @jdatcmd.
#
# WORK, NOT THE ACCESS PATH, for the reason at the top of this file: an arm
# asserting seq_scan=0 fails against a build that makes the drop cheaper some
# other way. And an arm asserting only that the rows were deleted passes with
# InvalidOid, since a sequential scan deletes them just as correctly -- that is
# the vacuous version this one exists instead of.
#
# drop_work TABLE [INDEX_MIN_BLOCKS] -- buffers the seven catalogs served while
# TABLE was dropped. A DROP cannot be repeated, so each reading gets its own
# identically built table.
DCATS="$CATS,'storage'"
drop_work() {
	local set_clause=""
	[ $# -ge 2 ] && set_clause="SET pgcolumnar.index_min_blocks = $2; "
	q "SELECT pg_stat_reset();" >/dev/null
	q "${set_clause}DROP TABLE $1;" >/dev/null
	q "SELECT pg_stat_force_next_flush();" >/dev/null
	q "SELECT coalesce(sum(heap_blks_read + heap_blks_hit
			   + coalesce(idx_blks_read,0) + coalesce(idx_blks_hit,0)), 0)
		FROM pg_statio_all_tables
		WHERE schemaname = 'pgcolumnar' AND relname IN ($DCATS);"
}

# Enough neighbours that the catalogs are worth an index. The conversion is
# size-aware, so with a few pages the default DECLINES the probe and the two
# readings converge -- which is the second arm, not a failure of the first.
for i in $(seq 1 24); do make_target "drp_fill_$i" 6; done
for t in drp_default drp_whole; do make_target "$t" 6; done

# `margin`, not a 1-or-0: this arm reports the page count it saw. Part 540
# refused the first version by name, and its rule is why -- a `-ge` against
# anything but 0 or 1 discards a real number, where `-gt 0` below is an honest
# presence check and is excused. `got [0] want [1]` would read the same at two
# pages and at zero, and those are a thin fixture and a broken one.
check_num "premise: the drop fixture grew the catalogs it is there to grow" \
	"$(margin "$(catpages)" 3)" "3"
check_num "premise: the table about to be dropped owns catalog rows" \
	"$([ "$(groups_of drp_default)" -gt 0 ] && echo 1 || echo 0)" "1"

d_default="$(drop_work drp_default)";               echo "--   drop default:    $d_default"
d_whole="$(drop_work drp_whole 2147483647)";        echo "--   drop read-whole: $d_whole"
echo "-- drop  catalog pages=$(catpages)  default=$d_default read-whole=$d_whole"

check_num "the drop's default does less catalog work than reading them whole" \
	"$(margin "$(permille "$((d_whole - d_default))" "$d_default")" $FLOOR_PERMILLE)" \
	"$FLOOR_PERMILLE"

# THE OTHER SIDE, and the arm that says the size check still protects a small
# database: with the catalogs below the threshold the default declines the probe,
# so forcing a probe cannot beat it.
q "DROP TABLE IF EXISTS drp_small_a, drp_small_b;" >/dev/null 2>&1
for t in drp_small_a drp_small_b; do
	q "CREATE TABLE $t (id int) USING pgcolumnar;
	   INSERT INTO $t SELECT g FROM generate_series(1,50) g;" >/dev/null
done
s_default="$(drop_work drp_small_a)"
s_probe="$(drop_work drp_small_b 0)"
echo "-- drop small  default=$s_default probe-always=$s_probe"
# STRICT, NOT `-le`, AND THAT IS THE WHOLE ARM. The first version asked only
# that the default do no MORE work than a forced probe. A build with the size
# check REMOVED -- probing unconditionally -- makes the two readings equal, and
# `-le` passes on it: the arm could not tell "the check is present and
# declining" from "there is no check". That is the same vacuity as an arm
# asserting only that the rows were deleted, which is what this section exists
# instead of. Reported by @jdatcmd.
#
# Requiring a MARGIN reddens under both mutations, so this arm witnesses the
# conversion rather than merely guarding a future one:
#
#     conversion present, declining      24 vs 27     125 permille   PASS
#     size check removed, always probe   27 vs 27       0            FAIL
#     conversion reverted, sequential   101 vs 101      0            FAIL
check_num "with few catalog pages the drop's default does less work than probing every one" \
	"$(margin "$(permille "$((s_probe - s_default))" "$s_default")" $FLOOR_PERMILLE)" \
	"$FLOOR_PERMILLE"

pgc_summary
