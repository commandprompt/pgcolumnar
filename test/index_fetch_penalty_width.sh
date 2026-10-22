#!/usr/bin/env bash
#
# pgColumnar: the index-fetch penalty charges decode CPU by column WIDTH, not by a
# bare column count (#803).
#
# pgcolumnar_index_fetch_penalty prices the row-group decode a per-row fetch forces.
# Its CPU term was cpu_operator_cost * R * nproj, where nproj is a count of the
# decoded prefix's columns, so a 68-byte text column was charged exactly what a
# 4-byte int4 column was -- the same flatness #768 fixed for the sequential scan.
#
# Measured before the fix, on the two tables this suite builds: the decode CPU term
# was 200.00 on both arms across an 8.6x difference in decoded bytes, while the same
# ~39k-row fetch took 4,385 ms narrow against 88,352 ms wide. The consequence was not
# merely under-pricing but an inverted ordering -- the model let the WIDE table fetch
# about 3x more rows than the narrow one before switching to a scan, when in reality
# the wide table can afford about 3x FEWER. At 120 rows the wide table's chosen index
# plan measured 239.6 ms against an 82.9 ms scan.
#
# This suite pins the ordering, which is the part that changes a plan, and does it
# from the PLAN rather than the clock so it is not a timing check and PGC_SKIP_TIMING
# never applies to it. The absolute row counts are deliberately not asserted: they
# move with the cost constants, and the defect is the direction.
#
# nproj is the decoded PREFIX length (pgcolumnar_scan_decode_shape sets *nprefix =
# maxatt, per #363), so width cannot be varied at a fixed prefix inside one table.
# Hence two tables of identical shape whose columns differ only in TYPE at each
# position: the prefix is four columns on both arms by construction.
#
# Both prefixes are kept far below COLUMNAR_FETCH_CACHE_MAX_BYTES so the width-aware
# cache-overflow blend is inactive on both arms and cannot be what the check measures;
# that is asserted, not assumed. That the penalty must still not over-fire on a
# clustered index or a point lookup is pinned by test/analyze_stats.sh (#355, #376).
#
# Usage:  test/index_fetch_penalty_width.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
# The row-group limit is pinned in the cluster config rather than by SET so the
# writing and planning sessions cannot disagree about it (see #806).
PGC_EXTRA_CONF="${PGC_EXTRA_CONF:-}
pgcolumnar.stripe_row_limit=20000"
export PGC_EXTRA_CONF
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

N=400000
R=20000
CAP=$((32 * 1024 * 1024))

psql_run "CREATE TABLE ifw_n (id int, scat int, c1 int,  c2 int)  USING pgcolumnar;"
psql_run "CREATE TABLE ifw_w (id int, scat int, c1 text, c2 text) USING pgcolumnar;"
psql_run "INSERT INTO ifw_n SELECT g, (g * 2654435761::bigint % 1000000)::int, g, g + 1
          FROM generate_series(1,$N) g;"
psql_run "INSERT INTO ifw_w SELECT g, (g * 2654435761::bigint % 1000000)::int,
            repeat(md5(g::text),2), repeat(md5((g+1)::text),2)
          FROM generate_series(1,$N) g;"
psql_run "CREATE INDEX ifw_n_scat ON ifw_n(scat);"
psql_run "CREATE INDEX ifw_w_scat ON ifw_w(scat);"
psql_run "ANALYZE ifw_n;"
psql_run "ANALYZE ifw_w;"

q1() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq \
		-c "$1" 2>&1 | tail -1
}
SETS="SET enable_seqscan=off; SET max_parallel_workers_per_gather=0; SET random_page_cost=1.1;"
plan_of() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq \
		-c "$SETS $1" 2>&1
}

# ---- premises -------------------------------------------------------------
for t in ifw_n ifw_w; do
	check "premise: $t holds all $N rows" "$(q1 "SELECT count(*) FROM $t")" "$N"
done

GN="$(q1 "SELECT count(*) FROM pgcolumnar.storage s JOIN pgcolumnar.row_group rg USING (storage_id) WHERE s.relation_oid = 'ifw_n'::regclass")"
GW="$(q1 "SELECT count(*) FROM pgcolumnar.storage s JOIN pgcolumnar.row_group rg USING (storage_id) WHERE s.relation_oid = 'ifw_w'::regclass")"
check "premise: both tables have the same row-group count, and more than the fetch cache holds" \
	"$([ "$GN" = "$GW" ] && [ "${GN:-0}" -gt 4 ] && echo "yes ($GN)" || echo "no (n=$GN w=$GW)")" "yes ($GN)"

WN="$(q1 "SELECT sum(avg_width) FROM pg_stats WHERE tablename = 'ifw_n'")"
WW="$(q1 "SELECT sum(avg_width) FROM pg_stats WHERE tablename = 'ifw_w'")"
check "premise: the wide table's prefix really is wider, by a large factor" \
	"$(awk -v a="${WN:-0}" -v b="${WW:-0}" 'BEGIN { print (a > 0 && b >= 4 * a) ? "wider" : "NOT WIDER (n=" a " w=" b ")" }')" "wider"
check "premise: both prefixes decode well under the fetch cache cap, so the overflow blend is inactive on both arms" \
	"$([ $(( ${WN:-0} * R )) -lt $CAP ] && [ $(( ${WW:-0} * R )) -lt $CAP ] && echo "under" \
	   || echo "OVER (n=$(( ${WN:-0} * R )) w=$(( ${WW:-0} * R )) cap=$CAP)")" "under"

# The check below reads a plan flip. If the penalty never moves a plan there is
# nothing to order and the whole suite would pass vacuously. NOT A PREMISE (#1339):
# it reads pgcolumnar's own costing, so the penalty switched off in the source
# reddens it.
PEN_ON="$(plan_of "SET pgcolumnar.enable_index_fetch_penalty=on;  EXPLAIN (COSTS OFF) SELECT c1,c2 FROM ifw_w WHERE scat BETWEEN 1 AND 400")"
PEN_OFF="$(plan_of "SET pgcolumnar.enable_index_fetch_penalty=off; EXPLAIN (COSTS OFF) SELECT c1,c2 FROM ifw_w WHERE scat BETWEEN 1 AND 400")"
check "the penalty is what moves this plan (index without it, not with it)" \
	"$(grep -q 'Index Scan' <<<"$PEN_OFF" && ! grep -q 'Index Scan' <<<"$PEN_ON" && echo yes \
	   || echo "no (off=$(grep -m1 -oE 'Index Scan|Custom Scan' <<<"$PEN_OFF") on=$(grep -m1 -oE 'Index Scan|Custom Scan' <<<"$PEN_ON"))")" "yes"

# ---- the check ------------------------------------------------------------
# The largest fetched-row count at which the table still plans a per-row index
# fetch. Ladder rather than a bisection so a failure prints where it turned over.
flip_rows() {
	local t="$1" k last=0
	# FOUR RUNGS BELOW 20, as the twin's (#1339): the wide table flips between 5 and 7
	# rows, so a ladder starting at 20 read FW = 0 and the ordering arm passed for
	# the wrong reason. Measured, penalty on: ifw_w fetches by index at 1, 2, 5, 10.
	for k in 1 2 5 10 20 40 60 80 100 150 200 300 400 600 900; do
		if grep -q 'Index Scan' <<<"$(plan_of "SET pgcolumnar.enable_index_fetch_penalty=on;
			EXPLAIN (COSTS OFF) SELECT c1,c2 FROM $t WHERE scat BETWEEN 1 AND $k")"; then
			last="$(q1 "SELECT count(*) FROM $t WHERE scat BETWEEN 1 AND $k")"
		else
			break
		fi
	done
	echo "$last"
}
FN="$(flip_rows ifw_n)"
FW="$(flip_rows ifw_w)"
echo "-- last row count still fetched by index:  narrow prefix ${WN}B = $FN rows,  wide prefix ${WW}B = $FW rows"

# NOT PREMISES (#1339): both read pgcolumnar's costing. And BOTH SIDES, as the twin
# has them: "wide <= narrow" holds for a wide count of 0, which an over-charged width
# weight produces as surely as the #803 under-charge it guards against.
check "the narrow table does fetch by index somewhere on the ladder" \
	"$([ "${FN:-0}" -gt 0 ] && echo yes || echo "no -- nothing to order")" "yes"
check "and the wide table fetches by index somewhere on it too" \
	"$([ "${FW:-0}" -gt 0 ] && echo yes || echo "no -- the wide table never fetches by index")" "yes"

# A wider decoded prefix makes every row-group decode dearer, so the wide table must
# abandon per-row fetches no later than the narrow one. Priced by column COUNT the two
# arms charge the same decode CPU and the wide table's larger page term pushes its
# flip point HIGHER, which is the inversion #803 reports.
check "a wider decoded prefix gives up on per-row fetches no later than a narrow one (#803)" \
	"$([ "${FW:-0}" -le "${FN:-0}" ] && echo "ordered" \
	   || echo "INVERTED (wide ${WW}B fetches to $FW rows, narrow ${WN}B only to $FN)")" "ordered"

pgc_summary
