#!/usr/bin/env bash
#
# Overlap and containment prune chunk groups and vectors (#1144).
#
# A zone map records a range column's minimum and maximum under the range type's
# own btree ordering, which sorts by lower bound and then upper bound. That
# ordering does not bound overlap: the lexicographically largest range is not the
# one reaching furthest right, so a chunk holding [1,2) and [3,100) has the same
# maximum as one holding [1,2) and [3,4). Before this, `span && ...` and
# `span @> ...` resolved through the btree family, found no strategy, and fell
# through to a post-decode filter -- measured on 200,000 rows: 0 pushed-down
# filters, 0 zone map probes, 199,881 rows removed AFTER decoding them.
#
# What this suite pins is BOTH halves, because a pruning change can be wrong in
# two directions and only one of them is visible in a counter:
#
#   CORRECTNESS   a heap mirror answers the same queries. A prune that removes a
#                 unit it should have kept LOSES ROWS, and only the oracle sees it.
#   EFFECT        the counters move. A prune that removes nothing is correct and
#                 worthless, and only the counters see that.
#
# THE SCATTERED CORPUS IS NOT A CONTROL FOR CORRECTNESS, it is the documented
# zero: this is worth nothing on an unclustered column, exactly as a min/max zone
# map is, and docs/limitations.md says so. It is asserted here so the claim in the
# documentation has something behind it.
#
# Usage:  test/range_pruning.sh [PG_CONFIG]
# Written fresh for pgColumnar.
set -uo pipefail
# Pinned in the cluster config rather than by SET, so the writing session and any
# later session agree about the geometry (#806), and so a unit is one hour of
# spans rather than whatever the default makes it.
# THE GEOMETRY IS THE MEASUREMENT. At stripe_row_limit=10000 a row group holds
# exactly ONE vector, because chunk_group_row_limit is 10000 too -- so pruning
# happens entirely at the group level and "vectors skipped" is 0 however well it
# works. The first version of this suite asserted the vector counter under that
# geometry and failed for a reason that had nothing to do with the code. One
# group of five vectors puts both levels in one fixture.
PGC_EXTRA_CONF="${PGC_EXTRA_CONF:-}
pgcolumnar.stripe_row_limit=50000"
export PGC_EXTRA_CONF
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

N=50000

# THE SUMMARY IS A CATALOG COLUMN, AND AN OLD CATALOG DOES NOT HAVE IT. Without
# it the writer records nothing and every arm below would pass for the wrong
# reason -- a green suite proving only that pruning nothing returns the right
# rows. Refused by name rather than skipped silently.
HAS_COL="$(q "SELECT count(*) FROM pg_attribute
               WHERE attrelid = 'pgcolumnar.zone_map'::regclass
                 AND attname = 'max_upper' AND NOT attisdropped")"
if [ "${HAS_COL:-0}" != "1" ]; then
	pgc_skip zone_map_max_upper \
		"pgcolumnar.zone_map has no max_upper column; ALTER EXTENSION pgcolumnar UPDATE"
fi

mk() {  # mk NAME AM clustered|scattered
	local off
	[ "$3" = clustered ] && off="g" || off="(g * 7919 % $N)"
	psql_run "DROP TABLE IF EXISTS $1;
	          CREATE TABLE $1 (id bigint, span tstzrange, payload int)$2;
	          INSERT INTO $1 SELECT g,
	            tstzrange('2020-01-01'::timestamptz + ($off||' minutes')::interval,
	                      '2020-01-01'::timestamptz + ($off||' minutes')::interval
	                        + interval '1 hour'),
	            g % 97 FROM generate_series(1,$N) g;
	          ANALYZE $1;" >/dev/null
}
mk r_col " USING pgcolumnar" clustered
mk r_heap "" clustered
mk s_col " USING pgcolumnar" scattered
mk s_heap "" scattered

# The oracle compares a count AND two sums: a count alone agrees whenever two
# wrong answers are the same size.
fingerprint() {  # fingerprint TABLE PREDICATE
	q "SELECT count(*) || '/' || coalesce(sum(payload),0) || '/' || coalesce(sum(id),0)
	     FROM $1 WHERE $2"
}
counter() {  # counter TABLE PREDICATE NAME
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -Atq \
		-c "EXPLAIN (ANALYZE, TIMING OFF, COSTS OFF) SELECT count(*) FROM $1 WHERE $2" 2>&1 |
		grep -oE "$3: [0-9]+" | head -1 | grep -oE '[0-9]+$'
}

WINDOW="tstzrange('2020-01-02 00:00+00','2020-01-02 01:00+00')"
WEEK="tstzrange('2020-01-05 00:00+00','2020-01-12 00:00+00')"
POINT="'2020-01-02 00:30+00'::timestamptz"
FUTURE="tstzrange('2099-01-01 00:00+00','2099-01-02 00:00+00')"
# NEAR THE END (#1339). A window at minute 45,360 falls in the last of the group's five
# vectors. The four before it start before the window ends, so their LOWER bounds
# cannot rule them out; only the upper bound each one records can. The one-hour
# WINDOW above sits in the first vector, so the vectors after it are skipped on lower
# bounds alone, and its skip arm passed with max_upper ignored entirely.
LATE="tstzrange('2020-02-01 12:00+00','2020-02-01 13:00+00')"

# ---- premises -------------------------------------------------------------
check "premise: the clustered fixture holds the rows the numbers below count" \
	"$(q "SELECT count(*) FROM r_col")" "$N"
check "premise: the heap mirror holds the same rows" \
	"$(q "SELECT count(*) FROM r_heap")" "$N"
UNITS="$(q "SELECT count(*) FROM pgcolumnar.zone_map z
            JOIN pgcolumnar.storage s USING (storage_id)
           WHERE s.relation_oid = 'r_col'::regclass AND z.column_index = 1
             AND z.vector_index >= 0")"
check "premise: the range column is summarised in several units, or nothing can be skipped" \
	"$([ "${UNITS:-0}" -ge 4 ] && echo "many ($UNITS)" || echo "TOO FEW ($UNITS)")" \
	"many ($UNITS)"
WITHBOUND="$(q "SELECT count(*) FROM pgcolumnar.zone_map z
                JOIN pgcolumnar.storage s USING (storage_id)
               WHERE s.relation_oid = 'r_col'::regclass AND z.column_index = 1
                 AND z.max_upper IS NOT NULL")"
check "premise: every one of those units carries an upper bound" \
	"$WITHBOUND" "$(q "SELECT count(*) FROM pgcolumnar.zone_map z
	                   JOIN pgcolumnar.storage s USING (storage_id)
	                  WHERE s.relation_oid = 'r_col'::regclass AND z.column_index = 1")"
check "premise: a non-range column carries none" \
	"$(q "SELECT count(*) FROM pgcolumnar.zone_map z
	      JOIN pgcolumnar.storage s USING (storage_id)
	     WHERE s.relation_oid = 'r_col'::regclass AND z.column_index <> 1
	       AND z.max_upper IS NOT NULL")" "0"

# ---- correctness: the heap answers the same questions ---------------------
check_text "overlap with a one-hour window returns the heap's rows" \
	"$(fingerprint r_col "span && $WINDOW")" "$(fingerprint r_heap "span && $WINDOW")"
check_text "overlap with a week-long window returns the heap's rows" \
	"$(fingerprint r_col "span && $WEEK")" "$(fingerprint r_heap "span && $WEEK")"
check_text "containment of a point returns the heap's rows" \
	"$(fingerprint r_col "span @> $POINT")" "$(fingerprint r_heap "span @> $POINT")"
check_text "a window no row can meet returns nothing, from both" \
	"$(fingerprint r_col "span && $FUTURE")" "$(fingerprint r_heap "span && $FUTURE")"
check "premise: the first three of those are not vacuously empty" \
	"$([ "$(q "SELECT count(*) FROM r_heap WHERE span && $WINDOW")" -gt 0 ] &&
	   [ "$(q "SELECT count(*) FROM r_heap WHERE span && $WEEK")" -gt 0 ] &&
	   [ "$(q "SELECT count(*) FROM r_heap WHERE span @> $POINT")" -gt 0 ] &&
	   echo "all three match rows" || echo "AT LEAST ONE IS EMPTY")" \
	"all three match rows"

# ---- effect: the counters move --------------------------------------------
check "the overlap qual is pushed down, where it used to be dropped" \
	"$(counter r_col "span && $WINDOW" 'Pushed-Down Filters')" "1"
PROBES="$(counter r_col "span && $WINDOW" 'Zone Map Probes')"
check "and the zone map is probed for it" \
	"$([ "${PROBES:-0}" -ge 1 ] && echo "probed ($PROBES)" || echo "NOT PROBED")" \
	"probed ($PROBES)"
SKIPPED="$(counter r_col "span && $WINDOW" 'Vectors Skipped')"
check "a clustered overlap skips vectors instead of decoding them" \
	"$([ "${SKIPPED:-0}" -gt 0 ] && echo "skipped ($SKIPPED)" || echo "SKIPPED NOTHING")" \
	"skipped ($SKIPPED)"
SKIPPED_LATE="$(counter r_col "span && $LATE" 'Vectors Skipped')"
check "a clustered overlap near the end skips the four vectors only their upper bounds rule out" \
	"$SKIPPED_LATE" "4"
check_text "and that late overlap returns the heap's rows" \
	"$(fingerprint r_col "span && $LATE")" "$(fingerprint r_heap "span && $LATE")"
SKIPPED_PT="$(counter r_col "span @> $POINT" 'Vectors Skipped')"
check "so does a containment probe" \
	"$([ "${SKIPPED_PT:-0}" -gt 0 ] && echo "skipped ($SKIPPED_PT)" || echo "SKIPPED NOTHING")" \
	"skipped ($SKIPPED_PT)"
check "a window no row can meet reads no chunk group at all" \
	"$(counter r_col "span && $FUTURE" 'Chunk Groups Read')" "0"

# ---- the documented zero ---------------------------------------------------
check "a scattered column skips nothing, which is what the documentation says" \
	"$(counter s_col "span && $WINDOW" 'Vectors Skipped')" "0"
check_text "and still returns the right rows" \
	"$(fingerprint s_col "span && $WINDOW")" "$(fingerprint s_heap "span && $WINDOW")"

# ---- the two values upper() cannot tell apart ------------------------------
#
# THE PAYLOAD COLUMN IS WHAT fingerprint SUMS. These tables lacked it until #1341,
# so both sides of the two arms below errored, q turned each error into "", and
# `check` compared "" with "" and passed: measured on PG17, u_col=[] u_heap=[].
# check_text refuses an empty side, so a failed query is now a red.
#
# AND NO EMPTY RANGE IN THIS UNIT. An empty range sorts first, so it becomes the
# unit's minimum, and a unit whose minimum is empty is never pruned at all
# (native_range_excludes returns false before reading max_upper). With the empty
# row here, a writer that ignored unbounded ranges left both arms green; e_col
# below holds the empty case on its own.
#
# AND ONE STATEMENT, SO ONE UNIT. A separate INSERT for the unbounded row wrote it
# to a row group of its own, which records no finite bound to prune on, so the
# arms passed with the unbounded state ignored by the writer.
psql_run "DROP TABLE IF EXISTS u_col; DROP TABLE IF EXISTS u_heap;
          CREATE TABLE u_col (id bigint, span tstzrange, payload int) USING pgcolumnar;
          CREATE TABLE u_heap (id bigint, span tstzrange, payload int);
          INSERT INTO u_col SELECT g,
            tstzrange('2020-01-01'::timestamptz + (g||' minutes')::interval,
                      '2020-01-01'::timestamptz + (g||' minutes')::interval + interval '1 hour'),
            g % 97 FROM generate_series(1,20000) g
          UNION ALL SELECT 20001, tstzrange('2020-01-03'::timestamptz, NULL), 5;
          INSERT INTO u_heap SELECT g,
            tstzrange('2020-01-01'::timestamptz + (g||' minutes')::interval,
                      '2020-01-01'::timestamptz + (g||' minutes')::interval + interval '1 hour'),
            g % 97 FROM generate_series(1,20000) g
          UNION ALL SELECT 20001, tstzrange('2020-01-03'::timestamptz, NULL), 5;
          ANALYZE u_col;" >/dev/null
check_text "a unit holding an unbounded range is never pruned from above" \
	"$(fingerprint u_col "span && $FUTURE")" "$(fingerprint u_heap "span && $FUTURE")"
check_text "the same for a containment probe far to its right" \
	"$(fingerprint u_col "span @> '2099-06-06 00:00+00'::timestamptz")" \
	"$(fingerprint u_heap "span @> '2099-06-06 00:00+00'::timestamptz")"
check "premise: that unbounded row really is matched, so the arm is not vacuous" \
	"$(q "SELECT count(*) FROM u_heap WHERE span && $FUTURE")" "1"
# AN EMPTY RANGE NEEDS ITS OWN TABLE. In u_col it shares a unit with the
# unbounded row, and unbounded is absorbing, so that unit says nothing about
# empty. `upper()` is NULL for both and they need opposite treatment: an empty
# range must not raise the bound, an unbounded one must stop the unit being
# pruned. This is the arm that tells them apart.
psql_run "DROP TABLE IF EXISTS e_col;
          CREATE TABLE e_col (id bigint, span tstzrange) USING pgcolumnar;
          INSERT INTO e_col SELECT g, 'empty'::tstzrange FROM generate_series(1,2000) g;
          ANALYZE e_col;" >/dev/null
E_TOTAL="$(q "SELECT count(*) FROM pgcolumnar.zone_map z
              JOIN pgcolumnar.storage s USING (storage_id)
             WHERE s.relation_oid = 'e_col'::regclass AND z.column_index = 1")"
check "premise: the all-empty table is summarised at all" \
	"$([ "${E_TOTAL:-0}" -ge 1 ] && echo "summarised ($E_TOTAL)" || echo "NO ZONE MAP")" \
	"summarised ($E_TOTAL)"
check "an empty range contributes no bound, so its units record NULL" \
	"$(q "SELECT count(*) FROM pgcolumnar.zone_map z
	      JOIN pgcolumnar.storage s USING (storage_id)
	     WHERE s.relation_oid = 'e_col'::regclass AND z.column_index = 1
	       AND z.max_upper IS NULL")" "$E_TOTAL"
check "and an unbounded range records a bound that is present but empty" \
	"$(U_EMPTY="$(q "SELECT count(*) FROM pgcolumnar.zone_map z
	                 JOIN pgcolumnar.storage s USING (storage_id)
	                WHERE s.relation_oid = 'u_col'::regclass AND z.column_index = 1
	                  AND z.max_upper IS NOT NULL AND octet_length(z.max_upper) = 0")";
	   [ "${U_EMPTY:-0}" -ge 1 ] && echo "present and empty" || echo "NOT RECORDED ($U_EMPTY)")" \
	"present and empty"

# ---- a range compares under ITS OWN collation, not its element type's ---------
#
# A range type is DECLARED with a collation, which the type cache carries as
# rng_collation. The element type's typcollation is a different value: for
# `CREATE TYPE tr AS RANGE (SUBTYPE = text, COLLATION = "en_US.utf8")` the range
# collates en_US.utf8 while text's typcollation is `default`. Summarising under
# one ordering and pruning under another makes the scan MISS ROWS -- a wrong
# answer, not an error. Found by @jdatcmd reviewing #1144.
#
# NOT REACHABLE WITH A BUILT-IN RANGE TYPE. tstzrange, daterange, int4range,
# int8range and numrange are all over non-collatable subtypes, so both
# expressions are 0 and agree however this is computed. It needs a user-defined
# range over a collatable subtype with an explicit non-default COLLATION, which
# is why every arm above passes with the defect present.
#
# WHAT THIS ARM NEEDS FROM THE DATABASE, stated because a quiet box looks like a
# clean one. It needs a collation whose ordering DIFFERS from the database default.
# On a cluster whose default is C.UTF-8 that is any locale collation; on a cluster
# whose default is already a locale collation it is "C". Declaring COLLATION "C"
# against a C default changes no ordering, so a fixture written that way is green
# on a correct tree AND on a broken one -- which is how the defect this arm exists
# for survived every other arm in this suite.
#
# THE COLLATION IS DISCOVERED, NOT ASSUMED. A box with no locale whose ordering
# differs from the database default cannot pose the question, and an arm that
# cannot discriminate must say so rather than pass.
_rc_coll=""
for _c in '"en_US.utf8"' '"en_GB.utf8"' '"unicode"' '"ucs_basic"' '"C"'; do
	if [ "$(q "SELECT ('B' < 'a' COLLATE $_c) <> ('B' < 'a')" 2>/dev/null)" = "t" ]; then
		_rc_coll="$_c"; break
	fi
done

if [ -z "$_rc_coll" ]; then
	check_skip "a range type's declared collation is the one it prunes under" \
		"SKIP  no available collation orders differently from this database's default, so the arm cannot discriminate" \
		"no discriminating collation on this box"
else
	psql_run "CREATE TYPE tr_coll AS RANGE (SUBTYPE = text, COLLATION = $_rc_coll);"
	psql_run "CREATE TABLE rc_heap (id int, span tr_coll);"
	psql_run "CREATE TABLE rc_col  (id int, span tr_coll) USING pgcolumnar;"
	# 1000 is the floor set_options enforces; a smaller value is REFUSED and
	# psql_run does not abort the suite, so the fixture would quietly keep the
	# default geometry while this line claimed otherwise.
	psql_run "SELECT pgcolumnar.set_options('rc_col', stripe_row_limit => 1000, chunk_group_row_limit => 1000);"
	RC_GEN="SELECT g, tr_coll(v, v || 'zz') FROM (SELECT g, (ARRAY['A','a','B','b','C','c','M','m','Y','y'])[1 + (g / 1000) % 10] AS v FROM generate_series(1, 10000) g) s"
	psql_run "INSERT INTO rc_heap $RC_GEN;"
	psql_run "INSERT INTO rc_col  $RC_GEN;"
	psql_run "ANALYZE rc_heap;"
	psql_run "ANALYZE rc_col;"

	check "premise: the range type collates differently from its element type" \
		"$(q "SELECT (SELECT rngcollation FROM pg_range WHERE rngtypid = 'tr_coll'::regtype)
		             <> (SELECT typcollation FROM pg_type WHERE oid = 'text'::regtype)")" \
		"t"
	check "premise: both tables hold the same rows before any predicate" \
		"$(q "SELECT count(*) FROM rc_heap") $(q "SELECT count(*) FROM rc_col")" \
		"10000 10000"
	check "premise: the collated range column is summarised, so pruning can engage" \
		"$(RC_Z="$(q "SELECT count(*) FROM pgcolumnar.zone_map z
		              JOIN pgcolumnar.storage s USING (storage_id)
		             WHERE s.relation_oid = 'rc_col'::regclass AND z.max_upper IS NOT NULL")";
		   [ "${RC_Z:-0}" -ge 1 ] && echo "summarised ($RC_Z)" || echo "NOT SUMMARISED ($RC_Z)")" \
		"summarised ($(q "SELECT count(*) FROM pgcolumnar.zone_map z JOIN pgcolumnar.storage s USING (storage_id) WHERE s.relation_oid = 'rc_col'::regclass AND z.max_upper IS NOT NULL"))"

	# THE ORACLE. Every probe is answered by the heap as well, and the two must
	# agree; a pruning bug shows up as the columnar side returning FEWER rows.
	for _q in '["A","C"]' '["a","b"]' '["B","M"]' '["m","z"]'; do
		check "overlap $_q returns the heap's rows under a declared collation" \
			"$(q "SELECT count(*) FROM rc_col WHERE span && '$_q'::tr_coll")" \
			"$(q "SELECT count(*) FROM rc_heap WHERE span && '$_q'::tr_coll")"
	done
	check "and the rows themselves match, not only the count" \
		"$(pgc_set_hash "SELECT id FROM rc_col WHERE span && '[\"A\",\"C\"]'::tr_coll")" \
		"$(pgc_set_hash "SELECT id FROM rc_heap WHERE span && '[\"A\",\"C\"]'::tr_coll")"
fi

pgc_summary
