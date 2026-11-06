#!/usr/bin/env bash
#
# The parallel-scan numbers in docs/limitations.md assume the narrow query reads
# EVERY chunk group. Nothing checked that the query the page publishes does.
#
# It did not. The page published `WHERE sel <= 1000000`, and `sel` is the
# `generate_series` counter, so it is stored in order and the zone map excluded
# 20 of 27 chunk groups before a row was read. The speedup numbers beside it were
# taken on a run that asserts 27 of 27 groups read. Measured on the published
# fixture, the published query gives 1.24 to 1.31 times and no non-overlap, where
# the page claims 1.8 to 2.5 times and non-overlap at 1.45 times.
#
# So the page described one table and quoted another table's numbers. This suite
# holds the premise those numbers rest on.
#
# WHY IT READS THE DOCUMENT. A suite carrying its own copy of the query cannot
# see the document drift away from it, which is the whole defect. The filter
# column is extracted from docs/limitations.md and the checks run against that.
# If the page stops naming a narrow query, the extraction fails and this suite
# goes red rather than passing on nothing.
#
# WHAT IS NOT CLAIMED. This suite does not check the published RATIOS. Those are
# wall clock on one machine and no suite should pin them. It checks the
# structural premise underneath them, which is clock-free and exact: the
# documented query must read every chunk group, and the ordered column must
# prune. PGC_SKIP_TIMING does not apply, because nothing here reads a clock.
#
# Usage:  test/doc_parallel_premise.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

DOC="$PGC_SRCDIR/docs/limitations.md"
ROWS=${PGC_DPP_ROWS:-200000}

# --- extract the narrow query's filter column FROM THE DOCUMENT -------------
doc_line=$(grep -m1 'The narrow query is' "$DOC")
doc_query=$(printf '%s' "$doc_line" | sed -n 's/.*`\(SELECT[^`]*\)`.*/\1/p')
doc_col=$(printf '%s' "$doc_query" | sed -n 's/.*WHERE[[:space:]]*\([a-zA-Z_][a-zA-Z0-9_]*\).*/\1/p')

check "the page still names a narrow query" \
	"$([ -n "$doc_query" ] && echo yes || echo no)" "yes"
check "and that query has an extractable filter column" \
	"$([ -n "$doc_col" ] && echo yes || echo no)" "yes"
[ -n "$doc_col" ] || { echo "could not extract a filter column from: $doc_line"; pgc_summary; }
echo "-- the page's narrow query filters on: $doc_col"

# --- the documented fixture shape, at a size a suite can afford -------------
# Same columns as the page's SQL. sel is the counter and is stored in order; a
# is hashint4-derived and is not. The row-group limit is lowered so a small
# table still holds many groups, which is what makes pruning observable.
psql_run "DROP TABLE IF EXISTS c753;
	SET pgcolumnar.stripe_row_limit = 20000;
	SET pgcolumnar.chunk_group_row_limit = 10000;
	CREATE TABLE c753 (sel int4, a int4, b int4) USING pgcolumnar;
	INSERT INTO c753 SELECT g, hashint4(g), hashint4(g+1)
	    FROM generate_series(1, $ROWS) g;"
psql_run "ANALYZE c753;"

groups_total=$(q "SELECT count(*) FROM pgcolumnar.row_group r
	JOIN pgcolumnar.storage s USING (storage_id)
	WHERE s.relation_oid = 'c753'::regclass")
check "the fixture holds several row groups, so pruning can be seen" \
	"$([ "${groups_total:-0}" -ge 5 ] && echo many || echo "$groups_total")" "many"

# A constant taken from the data, so the selectivity is the same whichever
# column is filtered and the check does not depend on the table's size.
p25() { q "SELECT percentile_disc(0.25) WITHIN GROUP (ORDER BY $1) FROM c753"; }

explain_of() {  # column -> the EXPLAIN ANALYZE text for a 25% scan on it
	q "SET max_parallel_workers_per_gather = 0;
	   EXPLAIN (ANALYZE, TIMING OFF)
	   SELECT sel, a, b FROM c753 WHERE $1 <= $(p25 "$1")"
}
read_groups() { explain_of "$1" | sed -n 's/.*Chunk Groups Read: \([0-9]*\).*/\1/p' | head -1; }
total_groups() { explain_of "$1" | sed -n 's/.*Chunk Groups Total: \([0-9]*\).*/\1/p' | head -1; }
# No plan-time stripe_row_limit here, and that is now load-bearing.
#
# This carried `SET pgcolumnar.stripe_row_limit = 20000` as a workaround for the
# half of #817 that was still open: pgcolumnar_zonemap_survival sized the group
# count from the planning SESSION's GUC, so at the 150,000 default it modelled
# this 10-group table as a 2-group one and zone-map pruning left the cost
# entirely -- both columns priced 4212.00. The survival site now reads the
# WRITTEN geometry, so the session GUC no longer reaches this estimate and the
# workaround is redundant.
#
# Leaving it out is deliberate: it makes this suite a removal proof for that fix.
# The check below asserts the documented column is costed ABOVE the ordered one,
# and without the SET that only holds because the estimator reads the geometry
# the table was written with. Put the SET back and the check passes for the wrong
# reason; revert the fix and it fails here.
#
# Anchored on the scan node rather than on the first cost= in the plan.
# `grep -m1` returns whatever node comes first, so the moment the plan gains a
# node above the scan it reports that node's cost instead, silently and with the
# right shape. That is the same class of defect this suite exists to catch, so
# the suite should not contain one.
plan_of() {
	q "SET max_parallel_workers_per_gather = 0;
	   EXPLAIN SELECT sel, a, b FROM c753 WHERE $1 <= $(p25 "$1")"
}
scan_lines() { plan_of "$1" | grep -c 'PgColumnarScan'; }
serial_cost() {
	plan_of "$1" | grep 'PgColumnarScan' \
	  | grep -oE 'cost=[0-9.]+\.\.[0-9.]+' | sed 's/.*\.\.//' | head -1
}

# The scan-node checks come FIRST, because every counter below is read out of
# a columnar scan's EXPLAIN output and does not exist without one.
# Unrolled, and the documented column named by its role: its name comes from the
# page, so a name built from it is one the grader cannot read.
check "the documented column's plan is exactly one columnar scan node" \
	"$(scan_lines "$doc_col")" "1"
check "the sel plan is exactly one columnar scan node" \
	"$(scan_lines sel)" "1"

doc_read=$(read_groups "$doc_col"); doc_tot=$(total_groups "$doc_col")
sel_read=$(read_groups sel);        sel_tot=$(total_groups sel)

# A counter that was not printed is an EMPTY string, and `check "" ""` PASSES.
# That is not hypothetical: with pgcolumnar.enable_custom_scan=off the plan is a
# seq scan, every counter below is blank, and check 1 passed comparing one blank
# with another. A check that passes on nothing is worse than no check.
check "the group counters were read, not blank" \
	"$([ -n "$doc_read" ] && [ -n "$doc_tot" ] && [ -n "$sel_read" ] && [ -n "$sel_tot" ] \
		&& echo yes || echo "doc=$doc_read/$doc_tot sel=$sel_read/$sel_tot")" "yes"
echo "-- $doc_col (the documented column): $doc_read of $doc_tot groups read"
echo "-- sel (stored in order):            $sel_read of $sel_tot groups read"

# --- 1. the defect this suite exists for ------------------------------------
# The published speedup numbers were taken with every group read. If the page's
# query prunes, the numbers beside it describe a different amount of work.
# Defaults that CANNOT collide, so blank-versus-blank fails on its own rather
# than relying on the check above to catch it. `check "" ""` passes, so a check
# fed by two extracted values must never be able to compare one blank with
# another.
#
# Against the CATALOG's group count, not only the scan's own total: a reader that
# never counted a group it read printed 0 of 0, and 0 == 0 passed (measured).
check "the documented narrow query reads every chunk group" \
	"${doc_read:-<no-read-counter>}/${doc_tot:-<no-total-counter>}" \
	"$(pgc_is_number "$groups_total" && echo "$groups_total/$groups_total" || echo "<no catalog count [$groups_total]>")"

# --- 2. the contrast the page now documents is real -------------------------
check "and filtering the ordered column instead prunes some away" \
	"$(pgc_is_number "$sel_read" && pgc_is_number "$sel_tot" && [ "$sel_read" -lt "$sel_tot" ] \
	   && echo prunes || echo "$sel_read of $sel_tot")" \
	"prunes"

# --- 3. stated directly, so the comparison below cannot be a self-comparison -
# Without this, a page that names the ordered column makes check 4 compare a
# value with itself, which is a check that cannot pass rather than one that
# measured something.
check "the documented column is not the one stored in order" \
	"$([ "$doc_col" != "sel" ] && echo yes || echo "no, the page names sel")" "yes"

# --- 4. the same effect in the cost, which is what moves the plan -----------
# Exact rather than bounded: reading more groups must cost more. A bound here
# would need calibrating and the direction does not.
if [ "$doc_col" != "sel" ]; then
	dc=$(serial_cost "$doc_col"); sc=$(serial_cost sel)
	check "both costs were extracted, not blank" \
		"$([ -n "$dc" ] && [ -n "$sc" ] && echo yes || echo "dc=$dc sc=$sc")" "yes"
	echo "-- serial cost: $doc_col $dc, sel $sc"
	check "and the documented column is costed above the ordered one" \
		"$(pgc_is_number "$dc" && pgc_is_number "$sc" \
		   && awk -v a="$dc" -v b="$sc" 'BEGIN { print (a + 0 > b + 0) ? "yes" : "no (" a " vs " b ")" }' \
		   || echo "unread [$dc|$sc]")" "yes"
fi

pgc_summary
