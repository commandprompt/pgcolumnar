#!/usr/bin/env bash
#
# Row-level security on every columnar read path (#1341). The columnar custom scan
# builds its quals from the restriction list, the vectorized and grouped aggregates
# and the covering projection scan answer from their own paths, and an index-only
# scan and a parallel scan read the table differently again. A policy must hold on
# each, and a function that is not leakproof must never see a row the policy hides.
#
# A table and a heap mirror hold the same 20000 rows, half owned by each of two
# roles, with a policy that shows a role only its own rows. Each arm logs in as one of
# the roles (a real login, not SET ROLE) and must answer as heap does, on the path the
# arm names: the plan premises check that path was taken.
#
# Usage:  test/rls_query_paths.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

psql_run "DROP ROLE IF EXISTS rq_alice; DROP ROLE IF EXISTS rq_bob;
          CREATE ROLE rq_alice LOGIN NOSUPERUSER; CREATE ROLE rq_bob LOGIN NOSUPERUSER;
          GRANT USAGE ON SCHEMA pgcolumnar TO rq_alice;
          CREATE FUNCTION rq_leak(text) RETURNS bool LANGUAGE plpgsql COST 0.0001 PARALLEL SAFE
            AS \$\$ BEGIN RAISE NOTICE 'seen:%', \$1; RETURN true; END \$\$;"
for t in rq rh; do
	am=pgcolumnar; [ $t = rh ] && am=heap
	psql_run "CREATE TABLE $t (id int, owner name, grp int, v int, secret text) USING $am;
	          INSERT INTO $t SELECT g, CASE WHEN g % 2 = 0 THEN 'rq_alice' ELSE 'rq_bob' END, g % 10, g,
	                                'S-' || CASE WHEN g % 2 = 0 THEN 'rq_alice' ELSE 'rq_bob' END || '-' || g
	                           FROM generate_series(1, 20000) g;
	          CREATE INDEX ${t}_io ON $t (id, owner);
	          ALTER TABLE $t ENABLE ROW LEVEL SECURITY;
	          CREATE POLICY ${t}_own ON $t USING (owner = current_user);
	          GRANT SELECT ON $t TO rq_alice, rq_bob;"
	psql_run "VACUUM ANALYZE $t;"	# not in a transaction block
done
psql_run "SELECT pgcolumnar.add_projection('rq', 'rq_p', ARRAY['id', 'owner', 'v'], ARRAY['id']);" >/dev/null

as_alice() {	# as_alice SQL -> its output, as rq_alice on her own login
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U rq_alice -d "$PGC_DB" -XAtq \
		-c "SET search_path TO public, pgcolumnar" -c "$1" 2>&1
}
same() {	# same SETUP QUERY -> "same", or both answers; %t is the table
	local c h
	c="$(as_alice "$1 ${2//%t/rq}" | tail -1)"; h="$(as_alice "$1 ${2//%t/rh}" | tail -1)"
	# an error on both sides is not agreement
	case "$c" in *ERROR*) echo "columnar=[$c] heap=[$h]"; return ;; esac
	[ "$c" = "$h" ] && [ -n "$c" ] && echo same || echo "columnar=[$c] heap=[$h]"
}
plan_has() {	# plan_has SETUP QUERY PATTERN -> how many plan lines match, on the columnar table
	as_alice "$1 EXPLAIN (COSTS OFF) ${2//%t/rq}" | grep -cE "$3"
}

AGG="SELECT count(*) || '/' || sum(v) || '/' || min(v) || '/' || max(v) || '/' || count(*) FILTER (WHERE owner <> current_user) FROM %t"
GRP="SELECT string_agg(grp || ':' || n || ':' || s, ',' ORDER BY grp) FROM (SELECT grp, count(*) n, sum(v) s FROM %t GROUP BY grp) x"

check "a superuser sees all 20000 rows, and rq_alice her 10000 on both tables" \
	"$(q "SELECT count(*) FROM rq")|$(as_alice "SELECT count(*) FROM rq")|$(as_alice "SELECT count(*) FROM rh")" "20000|10000|10000"

# ---- the custom scan and the vectorized aggregates -----------------------------------
check "premise: the aggregate is answered by the columnar custom scan" "$(plan_has "" "$AGG" 'Custom Scan \(PgColumnarScan\)')" "1"
check "count, sum, min and max under the policy, by the custom scan, are heap's" "$(same "" "$AGG")" "same"
check "premise: the GROUP BY arm's plan is the grouped vectorized node" \
	"$(plan_has "" "$GRP" 'Columnar Vectorized Group Keys')" "1"
check "a GROUP BY under the policy, with vectorization on, is heap's" "$(same "" "$GRP")" "same"
# The ungrouped vectorized aggregate builds its own quals from baserestrictinfo too,
# and any user can turn it on (PGC_USERSET). With no qual left, count(*) alone is
# answered from metadata, so a sum makes it scan (OffgridwithJD, review of #1429).
UVA="SET pgcolumnar.enable_ungrouped_vector_agg = on;"
UAGG="SELECT count(*) || '/' || sum(v) FROM %t"
check "premise: the ungrouped vectorized arm's plan folds the aggregates in the scan" \
	"$(plan_has "$UVA" "$UAGG" 'Columnar Vectorized Aggregates')" "1"
check "count and sum under the policy, by the ungrouped vectorized aggregate, are heap's" "$(same "$UVA" "$UAGG")" "same"
OFF="SET pgcolumnar.enable_vectorization = off; SET pgcolumnar.enable_group_vectorization = off; SET pgcolumnar.enable_custom_scan = off;"
check "and with vectorization and the custom scan off" "$(same "$OFF" "$GRP")|$(same "$OFF" "$AGG")" "same|same"

# ---- a parallel scan -----------------------------------------------------------------
PAR="SET parallel_setup_cost = 0; SET parallel_tuple_cost = 0; SET min_parallel_table_scan_size = 0; SET max_parallel_workers_per_gather = 2;"
check "premise: the parallel arm's plan gathers from workers" "$(plan_has "$PAR" "$AGG" 'Gather')" "1"
check "count, sum, min and max under the policy, by a parallel scan, are heap's" "$(same "$PAR" "$AGG")|$(same "$PAR" "$GRP")" "same|same"

# ---- an index-only scan --------------------------------------------------------------
IOS="SET enable_seqscan = off; SET enable_bitmapscan = off; SET pgcolumnar.enable_custom_scan = off;"
IQ="SELECT count(*) || '/' || sum(id) FROM %t WHERE id BETWEEN 100 AND 15000"
check "premise: the index-only arm's plan is an index-only scan" "$(plan_has "$IOS" "$IQ" 'Index Only Scan')" "1"
check "an index-only scan under the policy is heap's" "$(same "$IOS" "$IQ")" "same"

# ---- a covering projection -----------------------------------------------------------
PQ="SELECT count(*) || '/' || sum(v) FROM %t WHERE id BETWEEN 100 AND 15000"
check "premise: the projection arm's plan reads the covering projection" "$(plan_has "" "$PQ" 'Columnar Projection: rq_p')" "1"
check "a covering projection scan under the policy is heap's" "$(same "" "$PQ")" "same"

# ---- a function that is not leakproof sees only visible rows --------------------------
leaks() {	# leaks TABLE SETUP [QUERY] -> "<notices>|<notices naming another owner's row>"
	local out sql="${3:-SELECT count(*) FROM %t WHERE rq_leak(secret)}"
	out="$(as_alice "$2 ${sql//%t/$1}")"
	echo "$(grep -c 'seen:' <<<"$out")|$(grep -c 'seen:S-rq_bob' <<<"$out")"
}
check "a cheap function that is not leakproof sees only rq_alice's rows on columnar, as on heap" \
	"$(leaks rq "")|$(leaks rh "")" "10000|0|10000|0"
check "and the same with vectorization and the custom scan off" "$(leaks rq "$OFF")" "10000|0"
# PARALLEL SAFE, or the planner keeps the function out of the workers and this arm
# repeats the first one (OffgridwithJD, review of #1429).
check "premise: the parallel leak arm's plan gathers from workers" \
	"$(plan_has "$PAR" "SELECT count(*) FROM %t WHERE rq_leak(secret)" 'Gather')" "1"
check "and through a parallel scan" "$(leaks rq "$PAR")" "10000|0"
# Both vectorized nodes build their own quals (above), so each must also keep a
# function that is not leakproof from rows the policy hides. Each premise shows the
# arm's query shape, with a leakproof filter in place of the function, is answered by
# that node (OffgridwithJD, review of #1429).
check "premise: a GROUP BY with a leakproof filter under the policy is the grouped vectorized node" \
	"$(plan_has "" "SELECT grp, count(*) FROM %t WHERE secret IS NOT NULL GROUP BY grp" 'Columnar Vectorized Group Keys')" "1"
check "a non-leakproof function in a GROUP BY under the policy sees only rq_alice's rows, as on heap" \
	"$(leaks rq "" "SELECT grp, count(*) FROM %t WHERE rq_leak(secret) GROUP BY grp")|$(leaks rh "" "SELECT grp, count(*) FROM %t WHERE rq_leak(secret) GROUP BY grp")" "10000|0|10000|0"
check "premise: an ungrouped count and sum with a leakproof filter, with the setting on, is the ungrouped vectorized aggregate" \
	"$(plan_has "$UVA" "SELECT count(*), sum(v) FROM %t WHERE secret IS NOT NULL" 'Columnar Vectorized Aggregates')" "1"
check "and in the ungrouped vectorized aggregate" "$(leaks rq "$UVA" "SELECT count(*), sum(v) FROM %t WHERE rq_leak(secret)")" "10000|0"
# pgcolumnar.column_extent reads the stored extent, which covers every row, so under a
# policy it would show values from rows the caller cannot see. It refuses, as the
# other direct-storage entry points do (#563), and the owner still reads it
# (OffgridwithJD, review of #1429).
ce="$(as_alice "SELECT count(*) FROM pgcolumnar.column_extent('rq', 'v')")"
check "column_extent refuses a caller a policy restricts" \
	"$(grep -q 'row-level security is in force' <<<"$ce" && echo refused || echo "not refused (${ce//$'\n'/ })")" "refused"
check "and the owner still reads the extent" "$(q "SELECT count(*) FROM pgcolumnar.column_extent('rq', 'v')")" "1"

pgc_summary
