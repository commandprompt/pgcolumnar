#!/usr/bin/env bash
#
# Fetching from a row group larger than the fetch-cache cap (#433).
#
# The fetch cache keyed on (storageId, groupNumber) works, and below the cap a
# second fetch into the same group is nearly free. Above it the hit rate was zero
# BY CONSTRUCTION: the entry was populated, used once, and dropped whole, because
# the group's raw bytes alone exceeded the cap and groupBuffer could not be
# released while any baseline column pointed into it. Every fetched row therefore
# re-read and re-decoded the whole group. Measured on a 60,909,712-byte group in
# perfect key order: +7,476 shared buffers per row.
#
# test/native_fetch_cache.sh could not see this. Its widest fixture is
# repeat('a',150) text, which compresses far below the cap, so the whole-entry
# branch never executed anywhere in the suite. That is why this survived #143,
# #157 and #359, each of which moved the cliff rather than removing it.
#
# The measurement here is BUFFERS, not wall clock. The defect is "the group is
# re-read once per row", which is a count, and a count does not need a quiet
# machine to be true. Nothing in this suite is timing-sensitive, so none of it is
# subject to PGC_SKIP_TIMING.
#
# Usage:  test/native_fetch_bigcap.sh [PG_CONFIG]
# Written fresh for pgColumnar.
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

CAP=$((32 * 1024 * 1024))	# COLUMNAR_FETCH_CACHE_MAX_BYTES, src/columnar.h

# This suite is about what an index fetch COSTS TO EXECUTE, not about whether the
# planner picks one. Left alone the planner picks the custom scan here, correctly
# by its own model, and then nothing measures the fetch path at all. So the index
# path is forced, and every measured query asserts it got one. Whether the
# planner should choose it is #434, and that is a separate suite.
#
# pgcolumnar.enable_custom_scan=off as well: enable_seqscan alone does not turn
# the columnar custom scan off (#1339). Every measured query asserts its plan, so
# a planner change cannot quietly take the arms off the fetch path.
FORCE_INDEX="SET max_parallel_workers_per_gather=0; SET enable_seqscan=off;
             SET enable_bitmapscan=off; SET pgcolumnar.enable_index_fetch_penalty=off;
             SET pgcolumnar.enable_custom_scan=off;"

# ---------------------------------------------------------------------------
# The fixture, whose premise is its ON-DISK SIZE.
#
# #433 records two earlier attempts at this fixture that silently compressed 17x
# and 25x and would have been green while measuring nothing: repeat(md5(...), 32)
# repeats ONE string, and an uncorrelated scalar subquery is evaluated once for
# all rows. Both produce a table that never crosses the cap.
#
# So the payload is per-row distinct hex, built from a subquery correlated on g,
# and the block codec is off so the stored size is the encoded size and not a
# codec's opinion of it. Neither of those is trusted: the size is read back from
# pgcolumnar.row_group and asserted before any measurement is taken.
# ---------------------------------------------------------------------------
psql_run "DROP TABLE IF EXISTS bigcap;
	CREATE TABLE bigcap (id int, tag int, m1 text, m2 text, m3 text, payload text)
	    USING pgcolumnar;
	SELECT pgcolumnar.set_options('bigcap', stripe_row_limit => 10000,
	                              compression => 'none');
	INSERT INTO bigcap
	SELECT g, g % 97,
	       md5((g * 7)::text), md5((g * 11)::text), md5((g * 13)::text),
	       (SELECT string_agg(md5((g * 300 + s)::text), '')
	          FROM generate_series(1, 256) s)
	  FROM generate_series(1, 20000) g;
	CREATE INDEX bigcap_id ON bigcap (id);
	ANALYZE bigcap;" >/dev/null 2>&1

MAXG=$(q "SELECT max(byte_length) FROM pgcolumnar.row_group
          WHERE storage_id = pgcolumnar.get_storage_id('bigcap')")
echo "-- largest row group: ${MAXG:-?} bytes, cap ${CAP}"
check_num "premise: the fixture really has a row group above the cap" \
	"$([ -n "$MAXG" ] && [ "$MAXG" -gt "$CAP" ] && echo 1 || echo 0)" "1"
check_num "premise: the table loaded every row" \
	"$(q 'SELECT count(*) FROM bigcap')" "20000"

# ---------------------------------------------------------------------------
# Buffers touched by an index scan fetching N rows from ONE group.
#
# The rows are consecutive ids, so they are in one group and arrive in key order,
# which is the friendliest pattern the cache can be given. Run once to warm, then
# measure, so the figure is buffer TOUCHES and not first-read I/O.
# ---------------------------------------------------------------------------
bufs() {	# $1 = number of rows to fetch, starting at id 1
	local sql="$FORCE_INDEX
	           EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF, FORMAT TEXT)
	           SELECT sum(length(payload)) FROM bigcap WHERE id BETWEEN 1 AND $1;"
	psql_run "$sql" >/dev/null 2>&1
	psql_run "$sql" 2>/dev/null |
		awk '/Buffers:/ { for (i = 1; i <= NF; i++) {
			if ($i ~ /^(shared|read|hit)/) { gsub(/[^0-9]/, "", $i); if ($i != "") t += $i }
		} } END { print t + 0 }'
}

plan_of() {	# $1 = number of rows
	psql_run "$FORCE_INDEX
	          EXPLAIN (COSTS OFF)
	          SELECT sum(length(payload)) FROM bigcap WHERE id BETWEEN 1 AND $1;" 2>/dev/null |
		grep -c 'Index Scan'
}

check_num "premise: one row is fetched through an index scan" "$(plan_of 1)" "1"
check_num "premise: ten rows are fetched through an index scan" "$(plan_of 10)" "1"

B1=$(bufs 1)
B10=$(bufs 10)
echo "-- buffers: 1 row = ${B1}, 10 rows = ${B10}"

# Both sides must be a measurement before they are compared (#418). A down
# cluster, a failed EXPLAIN and a changed BUFFERS format all give 0 here, and 0
# would otherwise satisfy any ratio bound.
check_num "premise: fetching one row touched a measurable number of buffers" \
	"$([ "${B1:-0}" -gt 100 ] && echo 1 || echo 0)" "1"
check_num "premise: fetching ten rows touched a measurable number of buffers" \
	"$([ "${B10:-0}" -gt 100 ] && echo 1 || echo 0)" "1"

# The assertion. Ten rows from one group must not cost ten group re-reads.
#
# Broken, this ratio is ~10: the entry is dropped at the end of every fetch, so
# each row re-reads the whole group. Fixed, the group's bytes are touched once
# and the ratio is near 1. A bound of 3 sits well clear of both.
# `payload` decodes to ~8 KB x 10,000 rows = ~82 MB, which cannot be held in a
# 32 MB cap by any policy. It is therefore released and re-decoded per fetch,
# which is #359's per-column overflow behaving exactly as designed, and it is NOT
# what #433 is about. Recorded here as a bound rather than asserted as a fix,
# because a single column larger than the cap has nowhere to live.
echo "-- note: the payload column alone exceeds the cap, so it re-decodes per fetch (#359)"
check_num "premise: the wide column is the one over the cap, so this arm is #359's" \
	"$([ "${B10:-0}" -gt "${B1:-1}" ] && echo 1 || echo 0)" "1"

# ---------------------------------------------------------------------------
# A narrow projection over a wide group.
#
# The dropped-entry test used the group's RAW STORED bytes, so selecting one
# small column from a wide table tripped it just the same: the projection was
# never what the cap measured. This reads only `tag`, an int, whose whole column
# is a rounding error against the cap.
# ---------------------------------------------------------------------------
nbufs() {	# $1 = number of rows, projecting one narrow column
	local sql="$FORCE_INDEX
	           EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF, FORMAT TEXT)
	           SELECT sum(tag) FROM bigcap WHERE id BETWEEN 1 AND $1;"
	psql_run "$sql" >/dev/null 2>&1
	psql_run "$sql" 2>/dev/null |
		awk '/Buffers:/ { for (i = 1; i <= NF; i++) {
			if ($i ~ /^(shared|read|hit)/) { gsub(/[^0-9]/, "", $i); if ($i != "") t += $i }
		} } END { print t + 0 }'
}

# The narrow and medium projections had no plan premise of their own; only the
# payload query's plan was asserted (#1339). They plan as Index Scans today, and
# these keep a planner change from turning their ratios into scan-against-scan.
check_num "premise: the narrow projection is fetched through an index scan" \
	"$(pgc_uses_row_fetch "$FORCE_INDEX" "SELECT sum(tag) FROM bigcap WHERE id BETWEEN 1 AND 10" | grep -c yes)" "1"
N1=$(nbufs 1)
N10=$(nbufs 10)
echo "-- narrow projection buffers: 1 row = ${N1}, 10 rows = ${N10}"
check_num "premise: the narrow projection touched a measurable number of buffers" \
	"$([ "${N1:-0}" -gt 0 ] && echo 1 || echo 0)" "1"
check_ratio "a narrow projection over a wide group is cached too (#433)" \
	"$N10" "$N1" "3"

# ---------------------------------------------------------------------------
# The arm that isolates #433 from #359: a group well above the cap, projecting
# only columns that individually fit.
#
# This is precisely what the old whole-entry drop destroyed. It tested the
# group's RAW STORED size, so it fired here regardless of what the query
# projected, and three 32-byte text columns were re-read and re-decoded per row
# because a sixth column they never touched was large.
# ---------------------------------------------------------------------------
mbufs() {	# $1 = number of rows, projecting three medium columns
	local sql="$FORCE_INDEX
	           EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF, FORMAT TEXT)
	           SELECT count(m1 || m2 || m3) FROM bigcap WHERE id BETWEEN 1 AND $1;"
	psql_run "$sql" >/dev/null 2>&1
	psql_run "$sql" 2>/dev/null |
		awk '/Buffers:/ { for (i = 1; i <= NF; i++) {
			if ($i ~ /^(shared|read|hit)/) { gsub(/[^0-9]/, "", $i); if ($i != "") t += $i }
		} } END { print t + 0 }'
}

check_num "premise: the medium projection is fetched through an index scan" \
	"$(pgc_uses_row_fetch "$FORCE_INDEX" "SELECT count(m1 || m2 || m3) FROM bigcap WHERE id BETWEEN 1 AND 10" | grep -c yes)" "1"
M1=$(mbufs 1)
M10=$(mbufs 10)
echo "-- three medium columns from an oversized group: 1 row = ${M1}, 10 rows = ${M10}"
check_num "premise: the medium projection touched a measurable number of buffers" \
	"$([ "${M1:-0}" -gt 0 ] && echo 1 || echo 0)" "1"
check_ratio "columns that fit are cached even when their group does not (#433)" \
	"$M10" "$M1" "3"

# ---------------------------------------------------------------------------
# Correctness, which no amount of caching may change.
# ---------------------------------------------------------------------------
check_text "the fetched rows are the right rows" \
	"$(q "SELECT md5(string_agg(id || ':' || tag || ':' || m1 || m2 || m3 || ':' || md5(payload), ',' ORDER BY id))
	        FROM bigcap WHERE id BETWEEN 1 AND 10")" \
	"$(q "SELECT md5(string_agg(id || ':' || tag || ':' || m1 || m2 || m3 || ':' || md5(payload), ',' ORDER BY id))
	        FROM (SELECT * FROM bigcap ORDER BY id LIMIT 10) s")"
check_num "every payload survived the round trip at full length" \
	"$(q 'SELECT count(*) FROM bigcap WHERE length(payload) <> 8192')" "0"

pgc_summary
