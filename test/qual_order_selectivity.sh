#!/usr/bin/env bash
#
# pgColumnar: chunk-group skip predicates are evaluated most-selective-first, so
# a group that is going to be thrown away is not probed through every other
# predicate's column on the way there (#403 item 4).
#
# The group-skip loop returns "cannot match" on the FIRST predicate that excludes,
# and each predicate's first use in a group fetches that column's zone map from
# the catalog. Which predicate is tried first therefore decides how many catalog
# probes a pruned group costs.
#
# The order the loop used was the order the ScanKeys arrived in, which is
# ATTRIBUTE order. Measured on 100 row groups with two predicates, one excluding
# 99 groups and one excluding none:
#
#     WHERE a >= 0 AND b = 42     200 probes
#     WHERE b = 42 AND a >= 0     200 probes
#
# Both, because writing the selective predicate first in the query does not put
# it first in the ScanKeys. The order was arbitrary with respect to selectivity,
# not merely suboptimal.
#
# WHAT THIS CANNOT BREAK. The predicates are a conjunction, so the result does
# not depend on the order, and the groups-removed count is the same either way.
# That is why this suite pins the PROBES: no correctness check can see this
# change, and the groups-removed line cannot either. The answer arms below are
# still here, because a reordering bug that dropped a predicate would show up
# nowhere else.
#
# Usage:  test/qual_order_selectivity.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
# Pinned in the cluster config rather than by SET: the writing session and the
# planning session must agree about the geometry, which is #806.
PGC_EXTRA_CONF="${PGC_EXTRA_CONF:-}
pgcolumnar.stripe_row_limit=1000"
export PGC_EXTRA_CONF
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

NROWS=100000
NGROUPS=100			# NROWS / stripe_row_limit

# a is uniform, so every group's min/max spans it and "a >= 0" excludes nothing.
# b is monotonic, so each group holds a distinct range and "b = 42" excludes all
# but one. a is attribute 1, so it is tried first without this change whichever
# order the query is written in.
psql_run "CREATE TABLE qos (a int, b int, pad int) USING pgcolumnar;"
psql_run "INSERT INTO qos SELECT i % 10, i, i FROM generate_series(1,$NROWS) i;"
psql_run "ANALYZE qos;"

# the heap oracle, for the answer arms
psql_run "CREATE TABLE qos_heap (LIKE qos);"
# From the generator, not copied from qos: a copy agrees with whatever qos returned.
psql_run "INSERT INTO qos_heap SELECT i % 10, i, i FROM generate_series(1,$NROWS) i;"

plan_of() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq \
		-c "SET max_parallel_workers_per_gather=0;
		    EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) $1" 2>&1
}
# q1 SIDE SQL -> the answer's last line, or "SIDE_ERROR (first line of output)" when
# psql fails or prints nothing. The side is in the error, so a failed read on each
# side is two different values: the error text alone made two failures agree, and
# with the server stopped all three answer arms passed.
q1() {
	local out
	if out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq \
		-v ON_ERROR_STOP=1 -c "$2" 2>&1)" && [ -n "$out" ]; then
		printf '%s\n' "$out" | tail -1
	else
		printf '%s_ERROR (%s)\n' "$1" "$(printf '%s' "$out" | head -1)"
	fi
}
field() {
	sed -n "s/.*$2: \([0-9]*\).*/\1/p" <<<"$1" | head -1
}

Q="SELECT count(*) FROM qos WHERE a >= 0 AND b = 42"
PLAN="$(plan_of "$Q")"
PROBES="$(field "$PLAN" 'Columnar Zone Map Probes')"
TOTAL="$(field "$PLAN" 'Columnar Chunk Groups Total')"
REMOVED="$(field "$PLAN" 'Columnar Chunk Groups Removed by Filter')"
USABLE="$(field "$PLAN" 'Columnar Usable Skip Predicates')"

# ---- premises -------------------------------------------------------------
check "premise: the columnar scan ran, so these are its counters" \
	"$(grep -q 'Custom Scan (PgColumnarScan)' <<<"$PLAN" && echo yes || echo no)" "yes"

check "both predicates were pushed down and are usable" "$USABLE" "2"

check "premise: the fixture has the groups this suite is sized for" "$TOTAL" "$NGROUPS"

# If the selective predicate stopped pruning, probes would fall for a reason that
# has nothing to do with ordering. This is the arm that catches that.
check "the selective predicate still prunes what it always did" \
	"$REMOVED" "$(( NGROUPS - 1 ))"

check "the probe counter is reported at all" \
	"$(grep -qE 'Columnar Zone Map Probes: [0-9]+' <<<"$PLAN" && echo yes || echo no)" "yes"

# ---- the check ------------------------------------------------------------
# A pruned group should cost one probe, not one per predicate. The allowance is
# for the groups seen before the order settles: the first group matches and probes
# both columns, and the first EXCLUDED group probes both before the transpose.
BOUND=$(( NGROUPS + 4 ))
FLAT=$(( NGROUPS * 2 ))
echo "-- $NGROUPS groups, 2 predicates: $PROBES zone map probes (unordered costs $FLAT, bound $BOUND)"

check "a pruned group is not probed through every predicate's column (#403 item 4)" \
	"$([ "${PROBES:-0}" -gt 0 ] && [ "${PROBES:-0}" -le "$BOUND" ] && echo "ordered" \
	   || echo "UNORDERED ($PROBES probes for $NGROUPS groups, flat cost $FLAT)")" "ordered"

# ---- the order the query is written in must not matter ---------------------
REV="$(plan_of "SELECT count(*) FROM qos WHERE b = 42 AND a >= 0")"
REVPROBES="$(field "$REV" 'Columnar Zone Map Probes')"
# A missing count read as 0, inside the bound: with the probe counter reporting
# nothing, this passed. The reversed query must probe at all, and no more.
check "and it does not depend on the order the predicates are written in" \
	"$(if ! [[ "$REVPROBES" =~ ^[0-9]+$ ]] || [ "$REVPROBES" -eq 0 ]; then echo "no reading ($REVPROBES probes)"
	   elif [ "$REVPROBES" -le "$BOUND" ]; then echo "ordered"; else echo "UNORDERED ($REVPROBES)"; fi)" "ordered"

# ---- answers --------------------------------------------------------------
check "the answer matches the heap oracle" \
	"$(q1 COL "SELECT count(*) FROM qos WHERE a >= 0 AND b = 42")" \
	"$(q1 HEAP "SELECT count(*) FROM qos_heap WHERE a >= 0 AND b = 42")"

check "and so does a query where the selective predicate is a range" \
	"$(q1 COL "SELECT count(*), sum(b) FROM qos WHERE a >= 0 AND b BETWEEN 5000 AND 5100")" \
	"$(q1 HEAP "SELECT count(*), sum(b) FROM qos_heap WHERE a >= 0 AND b BETWEEN 5000 AND 5100")"

check "and one where NOTHING is selective, which must still answer correctly" \
	"$(q1 COL "SELECT count(*) FROM qos WHERE a >= 0 AND b >= 0")" \
	"$(q1 HEAP "SELECT count(*) FROM qos_heap WHERE a >= 0 AND b >= 0")"

# A query with no selective predicate must not pay for the mechanism: nothing
# excludes, so nothing is promoted, and every group is examined by both.
UNSEL="$(plan_of "SELECT count(*) FROM qos WHERE a >= 0 AND b >= 0")"
UNSELREM="$(field "$UNSEL" 'Columnar Chunk Groups Removed by Filter')"
check "a query with no selective predicate prunes nothing, as before" "$UNSELREM" "0"

pgc_summary
