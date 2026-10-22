#!/usr/bin/env bash
#
# pgColumnar metadata aggregates against a table with deletes (issue #149).
#
# An ungrouped, unfiltered aggregate over a native table is answered from row
# group metadata. A zone map describes every row written into its group, deleted
# ones included, so a group with deletes cannot be folded from its zone map. That
# used to be decided for the whole storage: one deleted row anywhere sent the
# query to a full scan, and a table lost a 0.04 ms count(*) to a 95 ms scan
# because one row of two million was gone.
#
# Deletion is a property of a row group, so the decision belongs there. Three
# things are asserted, needing three kinds of evidence.
#
# 1. The answers are right. Differential against a heap mirror over four delete
#    patterns, ending with one that dirties every group, because the interesting
#    failure is a group folded twice or not at all -- once from its zone map and
#    once from the scan that covers it.
#
# 2. count(*) no longer cares. Timed as a ratio against the same query on the
#    same table scanned in full, because a millisecond threshold is not portable.
#    count(*) over a group is its row count minus its deleted count, which is
#    exact, so a delete should not send the query back to reading the table.
#    Before the change it did exactly that.
#
# 3. Only the dirty groups are read. min/max cannot be folded from a zone map
#    once a row is gone, so those groups are scanned -- but only those. Timed
#    against the same query with vectorization off, which reads everything: one
#    dirty group out of many must come in far under a full scan. Before the
#    change the two were the same query.
#
# Usage:  test/native_agg_deletes.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

ROWS=${PGC_AGGDEL_ROWS:-400000}

psql_run "DROP TABLE IF EXISTS ad_c; DROP TABLE IF EXISTS ad_h;
	CREATE TABLE ad_c (id int, v int, w bigint, s text) USING pgcolumnar;
	INSERT INTO ad_c SELECT g, g % 1000, g * 7, 'r' || g
		FROM generate_series(1, $ROWS) g;
	CREATE TABLE ad_h (id int, v int, w bigint, s text);
	INSERT INTO ad_h SELECT g, g % 1000, g * 7, 'r' || g
		FROM generate_series(1, $ROWS) g;" >/dev/null 2>&1

groups="$(q "SELECT count(*) FROM pgcolumnar.row_group r
	JOIN pgcolumnar.storage s ON s.storage_id = r.storage_id
	WHERE s.relation_oid = 'ad_c'::regclass;")"
echo "-- $ROWS rows in ${groups} row groups"

# --- 1. the answers are right --------------------------------------------------

AGGS="count(*) count(v) count(s) sum(v) avg(v) min(v) max(v) min(w) max(w) min(s) max(s)"

# differential -> "same", or each aggregate that differs or could not be read. The
# caller names the check: a name built from an argument is one the parity grader
# cannot read, so every call site spells its own.
differential() {
	local bad="" agg cv hv

	for agg in $AGGS; do
		cv="$(q "SELECT $agg FROM ad_c;")"
		hv="$(q "SELECT $agg FROM ad_h;")"
		# An empty reading is a failed query, not a value: two of them used to agree,
		# so with the server gone after setup all four differentials passed.
		if [ -z "$cv" ] || [ -z "$hv" ]; then
			bad="$bad $agg(no reading: columnar=[$cv] heap=[$hv])"
		elif [ "$cv" != "$hv" ]; then
			bad="$bad $agg(columnar=$cv heap=$hv)"
		fi
	done
	echo "${bad:-same}"
}

# survivors CONDITION -> how many generated rows satisfy it: the oracle's oracle.
# Each delete below goes to /dev/null with its table, so without this a delete
# that never ran left every "after ..." arm comparing two untouched tables.
survivors() {
	q "SELECT count(*) FROM generate_series(1, $ROWS) g WHERE $1;"
}

check_num "premise: the heap mirror holds every generated row" \
	"$(q 'SELECT count(*) FROM ad_h;')" "$(survivors true)"
check "aggregates match heap with nothing deleted" "$(differential)" "same"

psql_run "DELETE FROM ad_c WHERE id = $((ROWS / 2));
	DELETE FROM ad_h WHERE id = $((ROWS / 2));" >/dev/null 2>&1
check_num "premise: the heap mirror lost exactly the one deleted row" \
	"$(q 'SELECT count(*) FROM ad_h;')" "$(survivors "g <> $((ROWS / 2))")"
check "aggregates match heap after one row is deleted" "$(differential)" "same"

psql_run "DELETE FROM ad_c WHERE id BETWEEN 1 AND $((ROWS / 10));
	DELETE FROM ad_h WHERE id BETWEEN 1 AND $((ROWS / 10));" >/dev/null 2>&1
check_num "premise: the heap mirror lost exactly the contiguous tenth" \
	"$(q 'SELECT count(*) FROM ad_h;')" \
	"$(survivors "g <> $((ROWS / 2)) AND g > $((ROWS / 10))")"
check "aggregates match heap after a contiguous tenth is deleted" "$(differential)" "same"

# every group now has deletes, so every group takes the scan path
psql_run "DELETE FROM ad_c WHERE id % 7 = 0;
	DELETE FROM ad_h WHERE id % 7 = 0;" >/dev/null 2>&1
check_num "premise: the heap mirror lost exactly every seventh row as well" \
	"$(q 'SELECT count(*) FROM ad_h;')" \
	"$(survivors "g <> $((ROWS / 2)) AND g > $((ROWS / 10)) AND g % 7 <> 0")"
check "aggregates match heap after every group is dirtied" "$(differential)" "same"

# --- 2. count(*) no longer cares about a delete --------------------------------

# a fresh pair: the table above is now heavily deleted, and this measures the
# cost of *a* delete, not of many
# Ten row groups, set per table rather than per session. The margin below
# depends on this: one dirty group out of three is a third of the table before
# any fixed overhead, measured against a half bound, and that is too little room.
#
# set_options rather than SET, because psql_run is one psql -c per call and so
# one session per call. A SET here reaches only this statement, and a later
# refactor that splits the call would silently drop it -- which is exactly what
# the first version of this fix did: it set the GUC in the psql_run that builds
# ad_c, forty lines above, and ad_t was still built with three groups.
psql_run "DROP TABLE IF EXISTS ad_t;
	CREATE TABLE ad_t (id int, v int) USING pgcolumnar;
	SELECT pgcolumnar.set_options('ad_t', stripe_row_limit => 40000);
	INSERT INTO ad_t SELECT g, g % 1000 FROM generate_series(1, $ROWS) g;" >/dev/null 2>&1

# The fixture's premise, asserted rather than printed. Every margin argument
# below depends on this being ten, so a change that quietly returns it to three
# fails here, naming the fixture, instead of resurfacing later as a timing check
# that fails for an apparently unrelated reason.
t_groups="$(q "SELECT count(*) FROM pgcolumnar.row_group r
	JOIN pgcolumnar.storage s ON s.storage_id = r.storage_id
	WHERE s.relation_oid = 'ad_t'::regclass;")"

check "the timing fixture has the row groups its margin assumes" "$t_groups" "10"

# Timed inside the server, best of three. psql's connect floor is about 15 ms and
# would swamp a query that should take a fraction of one, so a client-side
# stopwatch cannot see the difference this change makes.
psql_run "CREATE OR REPLACE FUNCTION ad_ms(qry text, n int) RETURNS numeric AS \$\$
	DECLARE t0 timestamptz; best numeric; cur numeric; i int;
	BEGIN
		EXECUTE qry;                       -- warm-up, discarded
		FOR i IN 1..n LOOP
			t0 := clock_timestamp();
			EXECUTE qry;
			cur := extract(epoch FROM clock_timestamp() - t0) * 1000;
			IF best IS NULL OR cur < best THEN best := cur; END IF;
		END LOOP;
		RETURN round(best, 4);
	END \$\$ LANGUAGE plpgsql;" >/dev/null 2>&1

# A leading SET prints its own "SET" line ahead of the result, so take the last
# line rather than the first.
ms() {  # sql [session settings] -> milliseconds, best of 3
	q "${2:-} SELECT ad_ms(\$q\$$1\$q\$, 3);" | tail -1
}

clean_ms="$(ms "SELECT count(*) FROM ad_t")"
psql_run "DELETE FROM ad_t WHERE id = $((ROWS / 2));" >/dev/null 2>&1
dirty_ms="$(ms "SELECT count(*) FROM ad_t")"
check_num "the timing table counts the row its one delete removed" \
	"$(q 'SELECT count(*) FROM ad_t;')" "$(survivors "g <> $((ROWS / 2))")"

# The reference is a full scan of the same table, not the undeleted timing.
#
# Comparing the deleted timing against the clean one looked like the obvious
# ratio and is the wrong shape: the clean figure is a metadata read of about
# 0.03 ms, so any jitter at all moves the ratio by tens. On an assert-enabled
# build -- which is what the matrix runs -- that failed about one run in three,
# at 1.1960/0.0320 against a threshold of 20, while the behaviour under test was
# perfectly correct.
#
# What the issue is actually about is that one deleted row must not send count(*)
# back to reading the table. So the thing to measure against is reading the
# table, which is milliseconds rather than microseconds and does not swing.
scan_ms="$(ms "SELECT count(*) FROM ad_t" \
	"SET pgcolumnar.enable_vectorization = off;")"

echo "-- count(*): ${clean_ms} ms clean, ${dirty_ms} ms with one deleted, ${scan_ms} ms scanning"

check "one deleted row does not send count(*) back to a full scan" \
	"$(awk -v d="$dirty_ms" -v s="$scan_ms" \
		'BEGIN { if (d !~ /^[0-9]+(\.[0-9]+)?$/ || s !~ /^[0-9]+(\.[0-9]+)?$/) print "no reading (" d " against a " s " scan)"; else print (s > 0 && d < s / 4) ? "yes" : "no (" d " against a " s " scan)" }')" \
	"yes"

# --- 3. only the groups with deletes are read ----------------------------------

# min/max cannot come from a zone map once a row in that group is gone, so the
# group is scanned. One dirty group out of many must still cost far less than
# reading the table, which is what the vectorization-off path does.
mm_ms="$(ms "SELECT min(v), max(v) FROM ad_t")"
full_ms="$(ms "SELECT min(v), max(v) FROM ad_t" \
	"SET pgcolumnar.enable_vectorization = off;")"

echo "-- min/max: ${mm_ms} ms with one group dirty, ${full_ms} ms reading everything"

check "a dirty group is scanned without scanning the clean ones" \
	"$(awk -v m="$mm_ms" -v f="$full_ms" \
		'BEGIN { if (m !~ /^[0-9]+(\.[0-9]+)?$/ || f !~ /^[0-9]+(\.[0-9]+)?$/) print "no reading (" m " vs " f ")"; else print (f > 0 && m < f / 2) ? "yes" : "no (" m " vs " f ")" }')" \
	"yes"

# and the path is still the one being measured
plan="$(q "EXPLAIN (COSTS off) SELECT count(*) FROM ad_t;" | head -1)"
check "the metadata aggregate path is still chosen with a row deleted" \
	"$(case "$plan" in *"Custom Scan (PgColumnarScan)"*) echo yes ;; *) echo "no ($plan)" ;; esac)" \
	"yes"

pgc_summary
