#!/usr/bin/env bash
#
# The planner's zone-map sample is one-based, complete, and per-column.
#
# pgcolumnar_zonemap_survival prices a restricted columnar scan by sampling row
# groups and asking the reader's own skip predicates how many survive. Two things
# were wrong with how it sampled.
#
# ONE-BASED. The stride was g = i * ngroups / nsample for i in [0, nsample), so it
# ran over [0, ngroups). A row group number is the stripe id reserved from the
# metapage (columnar_write_state.c), and the metapage starts reservedStripeId at 1,
# so group 0 exists on no table: the first probe was always spent on a number that
# could not exist, and group ngroups was never probed at all. When ngroups fits in
# PGCOLUMNAR_PRUNE_SAMPLE_GROUPS the loop is a census, and it was a census that
# omitted the newest group every time. The same predicate was therefore priced
# differently according to WHERE in the table its groups sat -- and the half that
# came out cheap was the recency predicate this engine is aimed at.
#
# PER-COLUMN. It called PgColumnarReadZoneMapList, which keys on (storage_id,
# group_number) against a four-column index, so it fetched every column's and every
# vector's row and then used one column's. pgcolumnar_native_group_can_match asks
# the same question with a three-key per-column probe (#314); the estimator now
# asks it the same way, so its reads no longer scale with table width.
#
# The instrument is pg_stat_all_tables.idx_tup_fetch for pgcolumnar.zone_map, split
# into PLANNING (EXPLAIN, which runs the estimator and no executor) and EXECUTION,
# and made readable with pg_stat_force_next_flush. The per-table stripe_row_limit
# option is used rather than the session GUC, so the suite does not depend on the
# separate question of which limit the survival estimate reads (#817).
#
# Removal proof, both halves measured:
#   - restore the zero-based stride and the two CENSUS checks read 9 of 10 groups
#     while the mirror pair splits 166.89 against 200.27 and the narrow pair 33.38
#     against 66.76, exactly half;
#   - restore the whole-group probe and WIDTH reads 600 against 40.
#
# Usage:  test/zonemap_estimate_sample.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

q "CREATE EXTENSION IF NOT EXISTS pgcolumnar;" >/dev/null

wcols=""; wvals=""
for i in $(seq 1 30); do
	wcols="$wcols${wcols:+, }c$i int"
	wvals="$wvals${wvals:+, }g"
done
q "CREATE TABLE w ($wcols) USING pgcolumnar;
   CREATE TABLE n (c1 int, c2 int) USING pgcolumnar;
   SELECT pgcolumnar.set_options('w'::regclass, stripe_row_limit => 2000);
   SELECT pgcolumnar.set_options('n'::regclass, stripe_row_limit => 2000);" >/dev/null
q "INSERT INTO w SELECT $wvals FROM generate_series(1,20000) g;" >/dev/null
q "INSERT INTO n SELECT g, g FROM generate_series(1,20000) g;" >/dev/null

sid() { q "SELECT pgcolumnar.get_storage_id('$1'::regclass);"; }

# ---- premises: the fixture is the shape every number below assumes ----------

check "w loaded 20000 rows" "$(q 'SELECT count(*) FROM w;')" "20000"
check "n loaded 20000 rows" "$(q 'SELECT count(*) FROM n;')" "20000"
check "w carries the per-table stripe_row_limit, not the session GUC" \
	"$(q "SELECT stripe_row_limit FROM pgcolumnar.options WHERE regclass='w'::regclass;")" "2000"
check "w has 10 row groups numbered 1..10, so there is no group 0" \
	"$(q "SELECT count(DISTINCT group_number)||'/'||min(group_number)||'/'||max(group_number)
	        FROM pgcolumnar.zone_map WHERE storage_id = $(sid w);")" "10/1/10"
# The per-column probe stops at the whole-chunk row and zone_map_pkey orders
# vector_index ascending, so -1 is the first tuple it meets: one fetch per group.
check "the whole-chunk row sorts first, so one fetch ends a per-column probe" \
	"$(q "SELECT min(vector_index) FROM pgcolumnar.zone_map
	       WHERE storage_id = $(sid w) AND group_number = 1 AND column_index = 0;")" "-1"

echo "-- zone_map rows per group: whole group w=$(q "SELECT count(*) FROM pgcolumnar.zone_map WHERE storage_id = $(sid w) AND group_number = 1;") n=$(q "SELECT count(*) FROM pgcolumnar.zone_map WHERE storage_id = $(sid n) AND group_number = 1;")"

# ---- the sample: complete, and one column wide ------------------------------

# zone_map index tuples fetched by ONE statement, or why there is no reading. q
# swallows a failure, and a failed statement fetches nothing, so it used to read as 0
# and pass WIDTH and the executor check on 0 = 0. The marker runs only if the
# statement before it succeeded.
zm() {
	local _zm_out
	q "SELECT pg_stat_reset();" >/dev/null
	_zm_out="$(q "$1; SELECT 'zm-ok';")"
	case "${_zm_out##*$'\n'}" in
		zm-ok) ;;
		*) echo "no reading (failed: $1)"; return ;;
	esac
	q "SELECT pg_stat_force_next_flush();" >/dev/null
	q "SELECT coalesce(idx_tup_fetch,0) FROM pg_stat_all_tables WHERE relname='zone_map' AND schemaname='pgcolumnar';"
}
# full minus planning, when both are readings
zm_exec() {
	if [[ "$1" =~ ^[0-9]+$ && "$2" =~ ^[0-9]+$ ]]; then echo $(($1 - $2)); else echo "no reading ($1 | $2)"; fi
}

WP="$(zm "EXPLAIN SELECT count(*) FROM w WHERE c1 >= 0;")"
WF="$(zm "SELECT count(*) FROM w WHERE c1 >= 0;")"
NP="$(zm "EXPLAIN SELECT count(*) FROM n WHERE c1 >= 0;")"
NF="$(zm "SELECT count(*) FROM n WHERE c1 >= 0;")"
echo "-- zone_map idx_tup_fetch, w (30 col): planning=$WP full=$WF execution=$(zm_exec "$WF" "$WP")"
echo "-- zone_map idx_tup_fetch, n ( 2 col): planning=$NP full=$NF execution=$(zm_exec "$NF" "$NP")"

check "planning probes the zone map at all" \
	"$([[ "$WP" =~ ^[0-9]+$ ]] && [ "$WP" -gt 0 ] && echo yes || echo "no ($WP)")" "yes"
check "CENSUS: planning reads one column across all 10 groups (w)" "$WP" "10"
check "CENSUS: planning reads one column across all 10 groups (n)" "$NP" "10"
check "WIDTH: the planner's zone-map reads do not scale with table width" "$WP" "$NP"
check "the executor's reads stay width-independent too" "$(zm_exec "$WF" "$WP")" "$(zm_exec "$NF" "$NP")"

# ---- the estimator's own session -------------------------------------------

# #744 resolves zone_map once per read session instead of once per probe. The
# estimator now holds one too, and its DEBUG1 report is labelled "estimate" so it
# can be told from a scan's "read" -- they interleave in one backend's log, and
# native_zonemap_session counts scan reports to prove a scan around an aborted one
# opens for itself.
est="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
	-d "$PGC_DB" -At -c "SET client_min_messages=debug1;
	    EXPLAIN SELECT count(*) FROM w WHERE c1 >= 0;" 2>&1 |
	grep -oE 'zone map estimate: probes=[0-9]+ opens=[0-9]+' | tail -1)"
echo "-- the planner's zone-map session: ${est:-<none>}"
check "the estimator reports a session of its own, distinct from a scan's" \
	"$([ -n "$est" ] && echo yes || echo no)" "yes"
check "it probes once per group across all 10 groups" \
	"$(printf '%s' "$est" | grep -oE 'probes=[0-9]+' | grep -oE '[0-9]+')" "10"
check "and opens zone_map once for the whole sample, not once per probe" \
	"$(printf '%s' "$est" | grep -oE 'opens=[0-9]+' | grep -oE '[0-9]+')" "1"

# ---- the consequence: position must not change the price --------------------

check "c1 > 17000 matches 3000 rows"   "$(q 'SELECT count(*) FROM w WHERE c1 > 17000;')" "3000"
check "c1 <= 4000 matches 4000 rows"   "$(q 'SELECT count(*) FROM w WHERE c1 <= 4000;')" "4000"
check "c1 > 8000 matches 12000 rows"   "$(q 'SELECT count(*) FROM w WHERE c1 > 8000;')" "12000"
check "c1 <= 12000 matches 12000 rows" "$(q 'SELECT count(*) FROM w WHERE c1 <= 12000;')" "12000"

scancost() {
	q "EXPLAIN SELECT count(*) FROM w WHERE $1;" |
		sed -n 's/.*Custom Scan (PgColumnarScan) on w  (cost=[0-9.]*\.\.\([0-9.]*\) .*/\1/p'
}
scanrows() {
	q "EXPLAIN SELECT count(*) FROM w WHERE $1;" | sed -n 's/.*PgColumnarScan.*rows=\([0-9]*\).*/\1/p'
}

# The MIRROR PAIR isolates position and nothing else: one clause each, the same
# operator family, the same default selectivity (no ANALYZE has run, so every
# range clause is estimated alike), and the same SIX matching groups -- 5..10 for
# one, 1..6 for the other. `run` is therefore identical and any cost difference is
# survival alone. The NARROW PAIR repeats it at two groups, where the omission is
# half the sample rather than a sixth.
HI="$(scancost 'c1 > 8000')"        # groups 5..10, the six NEWEST
LO="$(scancost 'c1 <= 12000')"      # groups 1..6,  the six OLDEST
TOP="$(scancost 'c1 > 17000')"      # groups 9,10,  the two NEWEST
BOT="$(scancost 'c1 <= 4000')"      # groups 1,2,   the two OLDEST
echo "-- Custom Scan total cost, six of ten groups: newest=$HI oldest=$LO"
echo "-- Custom Scan total cost, two of ten groups: newest=$TOP oldest=$BOT"

check "a cost was extracted, so the checks below are not comparing two blanks" \
	"$([ -n "$HI" ] && [ -n "$LO" ] && [ -n "$TOP" ] && [ -n "$BOT" ] && echo yes || echo no)" "yes"
check "the mirror pair shares a row estimate, so only survival differs" \
	"$(scanrows 'c1 > 8000')" "$(scanrows 'c1 <= 12000')"
check "the narrow pair shares a row estimate, so only survival differs" \
	"$(scanrows 'c1 > 17000')" "$(scanrows 'c1 <= 4000')"
check "MIRROR: the six newest groups cost what the six oldest cost" "$HI" "$LO"
check "NARROW: the two newest groups cost what the two oldest cost"  "$TOP" "$BOT"

# The floor survives the change. A sample that excludes every group reports a
# survival of zero, and a scan matching anything at all must still read the group
# its match is in, so the discount is floored at one group's share (#171). A
# predicate matching NO group and one matching exactly the LAST group must both
# come out at that floor -- and the last group is one this sample previously never
# looked at.
NONE="$(scancost 'c1 > 99999')"
LAST="$(scancost 'c1 > 19000')"
echo "-- Custom Scan total cost at the floor: matches-nothing=$NONE last-group-only=$LAST"
check "a predicate matching nothing is floored, not priced at zero" \
	"$([ -n "$NONE" ] && [ "${NONE%%.*}" -gt 0 ] && echo yes || echo no)" "yes"
check "matching only the last group costs the same one-group share" "$LAST" "$NONE"
check "a pruning predicate still returns the right rows" \
	"$(q 'SELECT count(*) FROM w WHERE c1 > 19000;')" "1000"
check "a predicate matching nothing still returns no rows" \
	"$(q 'SELECT count(*) FROM w WHERE c1 > 99999;')" "0"

# ---- the estimate must not move with the planning session's GUC (#817) ------
#
# The two tables above carry an explicit per-table stripe_row_limit, so
# pgcolumnar_effective_stripe_row_limit would return the option and the session
# GUC could never have reached them. This one deliberately sets NO option and is
# written under a session GUC instead, which is the only shape where the two
# readings differ -- and it is the shape a user produces by setting the GUC before
# a bulk load.
#
# Before #817's fix the survival site asked what limit a write in the PLANNING
# session would use. At the 150,000 default that made this 10-group table a
# ceil(20000/150000) = 1 group one, the single sampled group survived, and the
# discount vanished: the same unchanged table was priced differently by two
# sessions that had done nothing but SET a GUC.
q "CREATE TABLE gsess (c1 int, c2 int) USING pgcolumnar;" >/dev/null
q "SET pgcolumnar.stripe_row_limit = 2000;
   INSERT INTO gsess SELECT g, g FROM generate_series(1,20000) g;" >/dev/null

check "PREMISE: gsess carries NO per-table option, so the GUC is the only other input" \
	"$(q "SELECT count(*) FROM pgcolumnar.options WHERE regclass='gsess'::regclass AND stripe_row_limit IS NOT NULL;")" "0"
check "PREMISE: and it really was written at 2000, so it holds 10 groups" \
	"$(q "SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = $(sid gsess);")" "10"

# The SET and the EXPLAIN must share a session, so they go in one call.
gcost() {
	q "SET max_parallel_workers_per_gather = 0;
	   SET pgcolumnar.stripe_row_limit = $1;
	   EXPLAIN SELECT count(*) FROM gsess WHERE c1 <= 4000;" |
	  sed -n 's/.*Custom Scan (PgColumnarScan) on gsess  (cost=[0-9.]*\.\.\([0-9.]*\) .*/\1/p'
}
DEF="$(gcost 150000)"   # the shipped default
WRT="$(gcost 2000)"     # the value the table was written at
echo "-- gsess cost, planned at the default GUC=$DEF ; at the written 2000=$WRT"
check "a cost was extracted from both arms" \
	"$([ -n "$DEF" ] && [ -n "$WRT" ] && echo yes || echo "def=$DEF wrt=$WRT")" "yes"
check "the estimate does not move with the planning session's GUC (#817)" "$DEF" "$WRT"
check "and the discount is real, not just stable (2 of 10 groups priced below a full scan)" \
	"$(q "SET max_parallel_workers_per_gather = 0;
	      EXPLAIN SELECT count(*) FROM gsess WHERE c1 <= 4000;" |
	   sed -n 's/.*Custom Scan (PgColumnarScan) on gsess  (cost=[0-9.]*\.\.\([0-9.]*\) .*/\1/p' |
	   awk -v f="$(q "SET max_parallel_workers_per_gather = 0;
	                  EXPLAIN SELECT count(*) FROM gsess WHERE c1 >= 0;" |
	               sed -n 's/.*Custom Scan (PgColumnarScan) on gsess  (cost=[0-9.]*\.\.\([0-9.]*\) .*/\1/p')" \
	       '{ print ($1 < f) ? "yes" : "no (" $1 " vs full " f ")" }')" "yes"
check "gsess reads back correctly" "$(q 'SELECT count(*) FROM gsess WHERE c1 <= 4000;')" "4000"

pgc_summary
