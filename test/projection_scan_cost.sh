#!/usr/bin/env bash
#
# pgColumnar: a covering projection scan must not be priced at half the base.
#
# PgColumnarSetRelPathlist offers a covering-projection path by taking the
# base custom-scan run cost and multiplying by 0.5. That constant does not
# depend on the restriction, so a 5% range on the sort key is quoted the
# same as a 50% range. The projection is stored sorted on that key; the
# planner number has to move with selectivity, the way zone-map survival
# already does for the base scan.
#
# This suite pins the PLANNER number, not a runtime. Independent of
# test/pytest/test_projection_scan_cost.py: same public seam (EXPLAIN of a
# columnar scan with and without pgcolumnar.enable_projection_scan), own
# fixture, own observations.
#
# Usage:  test/projection_scan_cost.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/lib/postgresql/18/bin/pg_config}"

N=20000
TIGHT_HI=1000
LOOSE_HI=10000
psql_run "CREATE TABLE prsc (k int, payload text) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('prsc', stripe_row_limit => 1000, chunk_group_row_limit => 500);"
# Physical order is scrambled so the BASE scan cannot prune on k. The
# covering projection is stored sorted on k, which is the only reason it
# should be cheaper than the base for a range on k.
psql_run "INSERT INTO prsc SELECT k, repeat('x', 64) FROM generate_series(1, $N) k ORDER BY md5(k::text);"
psql_run "SELECT pgcolumnar.add_projection('prsc', 'byk', ARRAY['k'], ARRAY['k']);"
psql_run "ANALYZE prsc;"

explain_scan() {
	# $1 = on|off for pgcolumnar.enable_projection_scan
	# $2 = SQL
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -Atq \
		-c "SET max_parallel_workers_per_gather = 0;" \
		-c "SET pgcolumnar.enable_ungrouped_vector_agg = off;" \
		-c "SET pgcolumnar.enable_group_vectorization = off;" \
		-c "SET jit = off;" \
		-c "SET pgcolumnar.enable_projection_scan = $1;" \
		-c "EXPLAIN (COSTS ON) $2" \
		| grep -v '^SET$'
}

scan_cost_pair() {
	echo "$1" | grep -F "Custom Scan (PgColumnarScan)" | head -1 \
		| grep -oE "cost=[0-9.]+\.\.[0-9.]+" | head -1 \
		| sed -E "s/cost=([0-9.]+)\\.\\.([0-9.]+)/\\1 \\2/"
}

run_of() {
	local pair start total
	pair="$(scan_cost_pair "$1")"
	start="${pair%% *}"
	total="${pair##* }"
	awk -v t="$total" -v s="$start" "BEGIN{ print t-s }"
}

SQL_TIGHT="SELECT k FROM prsc WHERE k BETWEEN 1 AND $TIGHT_HI"
SQL_LOOSE="SELECT k FROM prsc WHERE k BETWEEN 1 AND $LOOSE_HI"

tight_proj="$(explain_scan on "$SQL_TIGHT")"
loose_proj="$(explain_scan on "$SQL_LOOSE")"
tight_base="$(explain_scan off "$SQL_TIGHT")"
loose_base="$(explain_scan off "$SQL_LOOSE")"

t_proj_run="$(run_of "$tight_proj")"
l_proj_run="$(run_of "$loose_proj")"
t_base_run="$(run_of "$tight_base")"
l_base_run="$(run_of "$loose_base")"

t_ratio="$(awk -v p="$t_proj_run" -v b="$t_base_run" "BEGIN{ if (b<=0) print 0; else printf \"%.3f\", p/b }")"
l_ratio="$(awk -v p="$l_proj_run" -v b="$l_base_run" "BEGIN{ if (b<=0) print 0; else printf \"%.3f\", p/b }")"

echo "-- tight proj_run=$t_proj_run base_run=$t_base_run ratio=$t_ratio"
echo "-- loose proj_run=$l_proj_run base_run=$l_base_run ratio=$l_ratio"

check "premise: the table holds every inserted row" \
	"$(q "SELECT count(*) FROM prsc")" "$N"

check "premise: a covering projection exists" \
	"$(q "SELECT count(*) FROM pgcolumnar.projection_declaration WHERE rel = 'prsc'::regclass AND name = 'byk'")" "1"

check "the tight plan uses the covering projection" \
	"$(echo "$tight_proj" | grep -c 'Columnar Projection: byk')" "1"

check "the loose plan uses the covering projection" \
	"$(echo "$loose_proj" | grep -c 'Columnar Projection: byk')" "1"

check "premise: without the projection scan, the tight plan is a base columnar scan" \
	"$(echo "$tight_base" | grep -c 'Columnar Projection')" "0"

check "premise: without the projection scan, the loose plan is a base columnar scan" \
	"$(echo "$loose_base" | grep -c 'Columnar Projection')" "0"

# NAMES WHICH SCAN. Collapsed to yes/no this said one of four was not positive and
# nothing about which one, and the reader had to re-derive all four (#1164). Single
# quotes round the awk program so the strings need no escaping.
check "premise: every compared scan has a positive run cost" \
	"$(awk -v a="$t_proj_run" -v b="$t_base_run" -v c="$l_proj_run" -v d="$l_base_run" \
		'BEGIN{ z = ""
			if (a <= 0) z = z " tight projection"
			if (b <= 0) z = z " tight base"
			if (c <= 0) z = z " loose projection"
			if (d <= 0) z = z " loose base"
			print (z == "") ? "yes" : "no run cost:" z }')" "yes"

# The unfixed path multiplies the whole run by 0.5, so both ratios are 0.500.
# A constant other than 0.5 can dodge the "both halved" pin; it cannot make
# a 5% range cheaper relative to the base than a 50% range.
check "a tight covering projection is cheaper relative to the base than a loose one" \
	"$(awk -v t="$t_ratio" -v l="$l_ratio" "BEGIN{ print (t < l) ? \"tighter\" : \"not\" }")" \
	"tighter"

check "tight and loose covering scans are not both priced at half the base" \
	"$(awk -v t="$t_ratio" -v l="$l_ratio" "BEGIN{
		both = (t>0.45 && t<0.55 && l>0.45 && l<0.55);
		print both ? \"both-halved\" : \"scaled\"
	}")" "scaled"


# The projection prunes only on its sort key. A query whose selectivity comes
# from a different column must not be priced as if the sort order produced
# that selectivity. Own table, own N, own column names; not derived from the
# pytest twin.
N_ATTR=20000
psql_run "CREATE TABLE prsk (sk int, kind text) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('prsk', stripe_row_limit => 1000, chunk_group_row_limit => 500);"
psql_run "INSERT INTO prsk SELECT sk, CASE WHEN sk % 1000 = 0 THEN 'odd' ELSE 'usual' END FROM generate_series(1, $N_ATTR) sk ORDER BY md5(sk::text);"
psql_run "SELECT pgcolumnar.add_projection('prsk', 'onsk', ARRAY['sk','kind'], ARRAY['sk']);"
psql_run "ANALYZE prsk;"

SQL_MIS="SELECT sk FROM prsk WHERE sk BETWEEN 1 AND $N_ATTR AND kind = 'odd'"
mis_proj="$(explain_scan on "$SQL_MIS")"
mis_base="$(explain_scan off "$SQL_MIS")"
m_proj_run="$(run_of "$mis_proj")"
m_base_run="$(run_of "$mis_base")"
m_ratio="$(awk -v p="$m_proj_run" -v b="$m_base_run" "BEGIN{ if (b<=0) print 0; else printf \"%.3f\", p/b }")"
echo "-- misattr proj_run=$m_proj_run base_run=$m_base_run ratio=$m_ratio"

check "premise: the misattributed query has a covering projection" \
	"$(q "SELECT count(*) FROM pgcolumnar.projection_declaration WHERE rel = 'prsk'::regclass AND name = 'onsk'")" "1"

check "premise: every misattributed scan has a positive run cost" \
	"$(awk -v a="$m_proj_run" -v b="$m_base_run" \
		'BEGIN{ z = ""
			if (a <= 0) z = z " projection"
			if (b <= 0) z = z " base"
			print (z == "") ? "yes" : "no run cost:" z }')" "yes"

# rel->rows after every restriction makes this cheap (one-stripe floor over
# heap survival on kind). The sort key is the whole table, so the ratio
# has to sit with the base.
check "a non-sort-key restriction does not cheapen a covering projection" \
	"$(awk -v r="$m_ratio" "BEGIN{ print (r+0 >= 0.8) ? \"not-cheap\" : \"cheap\" }")" \
	"not-cheap"

# ---- a clause that MENTIONS the sort key but cannot prune on it (#1126) -------
#
# Eligibility and pricing both ask "does this clause reference sortKey[0]".
# Mentioning is not prunability. A single RestrictInfo that ORs a sort-key range
# with a predicate on another column mentions the key, is counted whole, and
# contributes a selectivity the sort order cannot deliver.
#
# THE SAME prsk FIXTURE, because the question is about the clause and not the
# data: `kind = 'odd'` holds where sk % 1000 = 0, so its rows are spread across
# the whole sk domain and no sort order on sk brings them together.
#
# THE RANGE MUST CLEAR THE ONE-STRIPE FLOOR or the arm is vacuous. At
# stripe_row_limit => 1000 over 20000 rows there are 20 stripes, so the floor is
# 0.05; a range of 100 rows prices at the floor whether the arithmetic is right
# or wrong, and a broken guard and a working one are indistinguishable. 2000 rows
# is 10%, well clear of it. The first version of this arm used 100 and passed
# against the defect.
OR_HI=2000
SQL_ORQ="SELECT sk FROM prsk WHERE sk BETWEEN 1 AND $OR_HI OR kind = 'odd'"
SQL_PRUNE="SELECT sk FROM prsk WHERE sk BETWEEN 1 AND $OR_HI"
SQL_SAOP="SELECT sk FROM prsk WHERE sk = ANY (ARRAY[1,2,3,4,5,6,7,8,9,10])"

or_run="$(run_of "$(explain_scan on "$SQL_ORQ")")"
or_base="$(run_of "$(explain_scan off "$SQL_ORQ")")"
pr_run="$(run_of "$(explain_scan on "$SQL_PRUNE")")"
pr_base="$(run_of "$(explain_scan off "$SQL_PRUNE")")"
sa_run="$(run_of "$(explain_scan on "$SQL_SAOP")")"
sa_base="$(run_of "$(explain_scan off "$SQL_SAOP")")"
or_ratio="$(awk -v p="$or_run" -v b="$or_base" "BEGIN{ if (b<=0) print 0; else printf \"%.3f\", p/b }")"
pr_ratio="$(awk -v p="$pr_run" -v b="$pr_base" "BEGIN{ if (b<=0) print 0; else printf \"%.3f\", p/b }")"
sa_ratio="$(awk -v p="$sa_run" -v b="$sa_base" "BEGIN{ if (b<=0) print 0; else printf \"%.3f\", p/b }")"
echo "-- unprunable OR ratio=$or_ratio   prunable range ratio=$pr_ratio   IN-list ratio=$sa_ratio"

check "premise: every unprunable-clause scan has a positive run cost" \
	"$(awk -v a="$or_run" -v b="$or_base" \
		'BEGIN{ z = ""
			if (a <= 0) z = z " OR projection"
			if (b <= 0) z = z " OR base"
			print (z == "") ? "yes" : "no run cost:" z }')" "yes"

# PREMISE THAT THE FIXTURE CLEARS THE FLOOR. Without it a pass says nothing:
# at the floor every arm below reads 0.05 and agrees for the wrong reason.
check "premise: the prunable range is priced above the one-stripe floor, so the arms differ" \
	"$(awk -v r="$pr_ratio" "BEGIN{ print (r+0 > 0.051) ? \"above\" : \"at-floor\" }")" "above"

# THE ARM. The OR rules out nothing by sort order, so it must not be discounted.
check "a clause that mentions the sort key but cannot prune on it does not cheapen a covering projection" \
	"$(awk -v r="$or_ratio" "BEGIN{ print (r+0 >= 0.8) ? \"not-cheap\" : \"cheap\" }")" \
	"not-cheap"

# THE OTHER DIRECTION, which is the silent one. Declining a projection that would
# have won costs a query plan and reddens nothing, so both controls ship with the
# arm rather than after it.
check "while a plain range on the sort key still earns its discount" \
	"$(awk -v r="$pr_ratio" "BEGIN{ print (r+0 < 0.8) ? \"cheap\" : \"not-cheap\" }")" \
	"cheap"

# AN IN-LIST PRUNES HONESTLY and must keep its discount. `exact` is false for a
# ScalarArrayOpExpr range key, so a fix that gated on exactness rather than on
# producing a key at all would decline this one.
check "and an IN-list on the sort key keeps its discount, which gating on exactness would lose" \
	"$(awk -v r="$sa_ratio" "BEGIN{ print (r+0 < 0.8) ? \"cheap\" : \"not-cheap\" }")" \
	"cheap"

pgc_summary
