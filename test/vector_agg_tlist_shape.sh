#!/usr/bin/env bash
#
# The vectorized aggregate accepts a target list that CONTAINS aggregates, not
# only one that IS them (#755).
#
# The ungrouped vectorized path used to require every target-list entry to be a
# bare Aggref. An entry that merely contained one -- `count(*)::text`,
# `avg(a)+avg(b)`, `round(avg(a),2)`, `max(a)-min(a)` -- fell off the path
# entirely, and on an unfiltered columnar table that is the difference between
# answering from the zone maps and scanning the whole relation. Measured on
# 8,000,000 rows before the change: count(*) 0.030 ms against count(*)::text
# 634.1 ms.
#
# THE RISK IS A WRONG ANSWER, NOT A SLOW ONE. The node emits one bare aggregate
# per output column and a projection above it computes the expressions, so a
# mis-built projection returns the wrong column, the wrong aggregate, or the
# right value in the wrong place. Every arm below therefore asserts the ANSWER
# against a heap table holding identical rows, and asserts the plan separately.
# A plan check alone cannot see a wrong result; an answer check alone cannot see
# that the fast path was never taken.
#
# Usage:  test/vector_agg_tlist_shape.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# NULLs and duplicate values on purpose: an aggregate's null handling is part of
# what the projection must not disturb, and ties are where a wrong column shows.
psql_run "CREATE TABLE h (id int, a int, b int, v float8, t text) USING heap;"
psql_run "INSERT INTO h SELECT g,
                CASE WHEN g % 101 = 0 THEN NULL ELSE (g * 7919) % 1000 END,
                (g * 104729) % 97,
                (g % 13)::float8,
                'v' || (g % 7)
           FROM generate_series(1, 20000) g;"
psql_run "CREATE TABLE c (LIKE h) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('c', stripe_row_limit => 2000, chunk_group_row_limit => 500);"
psql_run "INSERT INTO c SELECT * FROM h;"
psql_run "ANALYZE h; ANALYZE c;"

# ansq below compares through pgc_seq_hash, which is order-sensitive. Assert the
# oracle can actually fail on order before relying on it (selftest 260).
pgc_check_ordered_oracle

check "premise: both tables hold the same rows" \
	"$(q 'SELECT count(*) FROM c;')" "$(q 'SELECT count(*) FROM h;')"
check "premise: the sort column has NULLs, which the aggregates must skip" \
	"$([ "$(q 'SELECT count(*) FROM c WHERE a IS NULL;')" -gt 0 ] && echo yes || echo no)" "yes"

# Vectorized-aggregate node count in the plan. The whole question is which path
# ran, so this is asserted per arm rather than assumed.
# The grouped path is pinned off: this suite is about the UNGROUPED fold, and since
# #1292 phase 2 the grouped path is on by default and prints the same marker.
#
# THREE ANSWERS, NOT A COUNT: "vectorized", "not vectorized", or "EXPLAIN failed: ...".
# This counted marker lines with psql's errors discarded, so a failed EXPLAIN counted 0
# and every REFUSE arm, which wants 0, passed on the error (measured: with table c
# never created, all seven REFUSE arms and the GUC-off premise passed).
plan_of() {  # plan_of SQL [SET-statement] -> one of the three readings
	local out
	if ! out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -v ON_ERROR_STOP=1 -c "${2:-SELECT 1}" -c "EXPLAIN (COSTS OFF) $1" 2>&1)"; then
		echo "EXPLAIN failed: $(printf '%s' "$out" | grep -m1 ERROR)"
	elif grep -qE 'Columnar Vectorized Aggregates: [1-9]' <<<"$out"; then
		echo "vectorized"
	else
		echo "not vectorized"
	fi
}
vec() { plan_of "$1" "SET pgcolumnar.enable_group_vectorization = off"; }

# ansq LABEL QUERY-with-%T : identical answer from heap and columnar.
ansq() {
	local label="$1" tmpl="$2"
	check_text "$label" "$(pgc_seq_hash "${tmpl//%T/c}")" "$(pgc_seq_hash "${tmpl//%T/h}")"
}
# fast NAME QUERY-with-%T : the plan over c took the fast path. Each arm is a `fast`
# line and an `ansq` line, each naming its check in full: a name composed inside a
# helper ("$label: vectorized") is one template to compare_to_bash, so a port could
# omit every arm but one and still grade complete.
fast() {
	check "$1" "$(vec "${2//%T/c}")" "vectorized"
}

# ---------------------------------------------------------- already worked

fast "a bare aggregate: vectorized" 'SELECT avg(a) FROM %T'
ansq "a bare aggregate: answer matches heap" 'SELECT avg(a) FROM %T'
fast "two bare aggregates: vectorized" 'SELECT avg(a), avg(b) FROM %T'
ansq "two bare aggregates: answer matches heap" 'SELECT avg(a), avg(b) FROM %T'
fast "count(*): vectorized" 'SELECT count(*) FROM %T'
ansq "count(*): answer matches heap" 'SELECT count(*) FROM %T'
fast "min and max: vectorized" 'SELECT min(a), max(a) FROM %T'
ansq "min and max: answer matches heap" 'SELECT min(a), max(a) FROM %T'

# ------------------------------------------------- newly accepted shapes

fast "a cast on an aggregate: vectorized" 'SELECT count(*)::text FROM %T'
ansq "a cast on an aggregate: answer matches heap" 'SELECT count(*)::text FROM %T'
fast "a sum of two aggregates: vectorized" 'SELECT avg(a)+avg(b) FROM %T'
ansq "a sum of two aggregates: answer matches heap" 'SELECT avg(a)+avg(b) FROM %T'
fast "a difference of aggregates: vectorized" 'SELECT max(a)-min(a) FROM %T'
ansq "a difference of aggregates: answer matches heap" 'SELECT max(a)-min(a) FROM %T'
fast "an aggregate in a function: vectorized" 'SELECT round(avg(a)::numeric, 2) FROM %T'
ansq "an aggregate in a function: answer matches heap" 'SELECT round(avg(a)::numeric, 2) FROM %T'
fast "an aggregate times a const: vectorized" 'SELECT avg(a)*2 FROM %T'
ansq "an aggregate times a const: answer matches heap" 'SELECT avg(a)*2 FROM %T'
fast "mixed bare and wrapped: vectorized" 'SELECT count(*), count(*)::text, avg(a) FROM %T'
ansq "mixed bare and wrapped: answer matches heap" 'SELECT count(*), count(*)::text, avg(a) FROM %T'
fast "a constant beside aggregates: vectorized" 'SELECT 1, count(*), avg(a) FROM %T'
ansq "a constant beside aggregates: answer matches heap" 'SELECT 1, count(*), avg(a) FROM %T'
fast "the same aggregate twice: vectorized" 'SELECT avg(a)+avg(a) FROM %T'
ansq "the same aggregate twice: answer matches heap" 'SELECT avg(a)+avg(a) FROM %T'
fast "aggregates in both orders: vectorized" 'SELECT max(a)-min(a), min(a)-max(a) FROM %T'
ansq "aggregates in both orders: answer matches heap" 'SELECT max(a)-min(a), min(a)-max(a) FROM %T'
fast "a CASE over aggregates: vectorized" 'SELECT CASE WHEN count(*) > 0 THEN avg(a) ELSE NULL END FROM %T'
ansq "a CASE over aggregates: answer matches heap" 'SELECT CASE WHEN count(*) > 0 THEN avg(a) ELSE NULL END FROM %T'

# A WHERE routes to the SCAN-FOLD path, which is behind
# pgcolumnar.enable_ungrouped_vector_agg and that GUC is OFF by default. So a
# filtered aggregate declines with or without this change, and an arm that did
# not set it would be measuring the opt-in rather than the target-list shape.
# With it on, the relaxation applies there too.
vec_sf() { plan_of "$1" "SET pgcolumnar.enable_ungrouped_vector_agg = on"; }
check "premise: the scan-fold GUC is off by default, so a filter declines" \
	"$(vec 'SELECT avg(a) FROM c WHERE b < 40')" "not vectorized"
check "premise: and a BARE aggregate takes it once the GUC is on" \
	"$(vec_sf 'SELECT avg(a) FROM c WHERE b < 40')" "vectorized"
check "a wrapped aggregate takes the scan-fold path too" \
	"$(vec_sf 'SELECT round(avg(a)::numeric,2) FROM c WHERE b < 40')" "vectorized"
ansq "and the filtered wrapped answer matches heap" \
	'SELECT round(avg(a)::numeric,2) FROM %T WHERE b < 40'
fast "count of a nullable column wrapped: vectorized" 'SELECT count(a)::text, count(*)::text FROM %T'
ansq "count of a nullable column wrapped: answer matches heap" 'SELECT count(a)::text, count(*)::text FROM %T'

# Column ORDER is what a mis-built projection gets wrong while every individual
# value is still right, so an arm whose columns are deliberately not in
# aggregate order.
fast "output order differs from aggregate order: vectorized" 'SELECT max(a), min(a), count(*), avg(a) FROM %T'
ansq "output order differs from aggregate order: answer matches heap" 'SELECT max(a), min(a), count(*), avg(a) FROM %T'
fast "an aggregate used in two different expressions: vectorized" 'SELECT avg(a)+1, avg(a)*2, avg(a) FROM %T'
ansq "an aggregate used in two different expressions: answer matches heap" 'SELECT avg(a)+1, avg(a)*2, avg(a) FROM %T'

# --------------------------------------------------------- refusal arms
#
# Shapes that must still decline. These pass trivially if the feature were
# removed, so the over-accepting mutation in the PR is what proves them.

check "REFUSE: a bare column is not an ungrouped aggregate shape" \
	"$(vec 'SELECT a, count(*) FROM c GROUP BY a')" "not vectorized"
ansq "and GROUP BY still answers correctly" \
	'SELECT a, count(*) FROM %T GROUP BY a ORDER BY a NULLS LAST'

check "REFUSE: an unsupported aggregate declines the whole target list" \
	"$(vec "SELECT count(*)::text, string_agg(t, ',') FROM c")" "not vectorized"
ansq "and the unsupported aggregate still answers correctly" \
	"SELECT count(*)::text, length(string_agg(t, ',')) FROM %T"

# --- the surface this change OPENS ------------------------------------------
#
# Before the relaxation these never reached pgcolumnar_classify_aggref at all:
# the tlist-shape gate refused them first, because each is an aggregate wrapped
# in something. Now the shape gate passes and classify_aggref is the ONLY thing
# between a modifier the node cannot represent and a wrong answer. It rejects
# aggorder, aggdistinct, aggfilter and aggvariadic in its first condition; these
# arms are what keep that true.
#
# THE NODE DOES NOT FAIL ON A MODIFIER IT CANNOT REPRESENT. IT IGNORES IT AND
# RETURNS THE UNMODIFIED AGGREGATE. Measured by removing aggdistinct and
# aggfilter from that condition, on 20,000 rows:
#
#                                   correct   with the refusal removed
#   count(*) FILTER (WHERE a > 5)     8,000            20,000
#   count(DISTINCT b)                     7            20,000
#   count(*)                         20,000            20,000
#
# Both collapse to the unmodified aggregate. A FILTER that removes 60% of the
# rows returns the count of all of them, and a DISTINCT over 7 values returns
# 20,000. No error, no plan tell, and a number that looks entirely plausible --
# which is the worst failure direction available and the reason the refusal has
# to live in classify_aggref rather than be inferred from a shape gate that used
# to hide these before they got there.

check "REFUSE: FILTER on a wrapped aggregate" \
	"$(vec 'SELECT count(*) FILTER (WHERE a > 5)::text FROM c')" "not vectorized"
ansq "and FILTER still answers correctly" \
	'SELECT count(*) FILTER (WHERE a > 5)::text FROM %T'

check "REFUSE: DISTINCT on a wrapped aggregate" \
	"$(vec 'SELECT count(DISTINCT b)::text FROM c')" "not vectorized"
ansq "and DISTINCT still answers correctly" 'SELECT count(DISTINCT b)::text FROM %T'

check "REFUSE: FILTER inside an expression over aggregates" \
	"$(vec 'SELECT avg(a) FILTER (WHERE b < 50) + 1 FROM c')" "not vectorized"
ansq "and FILTER inside an expression still answers correctly" 'SELECT avg(a) FILTER (WHERE b < 50) + 1 FROM %T'

check "REFUSE: ORDER BY inside an aggregate, wrapped" \
	"$(vec "SELECT length(string_agg(t, ',' ORDER BY a)) FROM c")" "not vectorized"
ansq "and a wrapped ORDER BY still answers correctly" \
	"SELECT length(string_agg(t, ',' ORDER BY a NULLS LAST, id)) FROM %T"

# --- empty and all-NULL, which a projection can get wrong quietly -----------

psql_run "CREATE TABLE eh (LIKE h) USING heap;"
psql_run "CREATE TABLE ec (LIKE h) USING pgcolumnar;"
check "premise: the empty pair really is empty" "$(q 'SELECT count(*) FROM ec;')" "0"
check_text "an empty relation answers the wrapped shape as heap does" \
	"$(pgc_seq_hash 'SELECT count(*)::text, avg(a), avg(a)+avg(b) FROM ec')" \
	"$(pgc_seq_hash 'SELECT count(*)::text, avg(a), avg(a)+avg(b) FROM eh')"

psql_run "INSERT INTO eh SELECT g, NULL, NULL, NULL, NULL FROM generate_series(1,500) g;"
psql_run "INSERT INTO ec SELECT * FROM eh;"
check "premise: the all-NULL pair has rows but no values" \
	"$([ "$(q 'SELECT count(*) FROM ec;')" -gt 0 ] && [ "$(q 'SELECT count(a) FROM ec;')" -eq 0 ] && echo yes || echo no)" "yes"
check_text "an all-NULL relation answers the wrapped shape as heap does" \
	"$(pgc_seq_hash 'SELECT count(*)::text, avg(a), avg(a)+avg(b) FROM ec')" \
	"$(pgc_seq_hash 'SELECT count(*)::text, avg(a), avg(a)+avg(b) FROM eh')"

check "REFUSE: an aggregate over an expression of two columns" \
	"$(vec 'SELECT sum(a + b) FROM c')" "not vectorized"
ansq "and an aggregate over an expression still answers correctly" 'SELECT sum(a + b) FROM %T'

# --- the parallel arm, which this change also unblocked ---------------------
#
# The serial gate returned before the parallel arm was reached, so refusing a
# wrapped target list killed both. The parallel arm itself needed no change: it
# uses core's partial grouping target, which is bare aggregates however the final
# target is shaped.
#
# A METADATA-ANSWERABLE SHAPE CANNOT TEST THIS. count(*) and avg over int are
# answered from the zone maps in microseconds, so no parallel plan can win and
# the arm would measure nothing while looking green. The float8 column forces the
# scan-fold path, and the Gather is asserted BEFORE any answer is believed.

par() {  # par QUERY -> "<gather count> <workers planned> <vec count>"
	env PATH="$PGC_BINDIR:$PATH" \
		PGOPTIONS="-c max_parallel_workers_per_gather=4 -c parallel_setup_cost=0 -c min_parallel_table_scan_size=0 -c pgcolumnar.enable_ungrouped_vector_agg=on -c pgcolumnar.enable_parallel_vector_agg=on" \
		psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At \
		-c "EXPLAIN (COSTS OFF) $1" 2>/dev/null | awk '
			/Gather/ {g++} /Workers Planned: [0-9]+/ {match($0,/[0-9]+/); w=substr($0,RSTART,RLENGTH)}
			/Columnar Vectorized Aggregates: [1-9]/ {v++}
			END {printf "%d %d %d", g+0, w+0, v+0}'
}
par_ans() {  # the parallel answer, as text
	env PATH="$PGC_BINDIR:$PATH" \
		PGOPTIONS="-c max_parallel_workers_per_gather=4 -c parallel_setup_cost=0 -c min_parallel_table_scan_size=0 -c pgcolumnar.enable_ungrouped_vector_agg=on -c pgcolumnar.enable_parallel_vector_agg=on" \
		psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At -c "$1" 2>&1
}

read -r PG PW PV <<<"$(par 'SELECT avg(v) FROM c')"
check "premise: a BARE aggregate on the scan-fold path really goes parallel" \
	"$([ "$PG" -ge 1 ] && [ "$PW" -ge 1 ] && [ "$PV" -ge 1 ] && echo yes \
	   || echo "no (gather=$PG workers=$PW vec=$PV)")" "yes"

# par_arm NAME QUERY: the wrapped target list went parallel; par_match NAME QUERY: and its
# parallel answer equals the heap's. Unrolled with literal names, for the reason `fast` is.
par_arm() {
	local G W V
	read -r G W V <<<"$(par "$2")"
	check "$1" \
		"$([ "$G" -ge 1 ] && [ "$W" -ge 1 ] && [ "$V" -ge 1 ] && echo yes \
		   || echo "no (gather=$G workers=$W vec=$V)")" "yes"
}
par_match() {
	check_text "$1" "$(par_ans "$2")" "$(q "${2//FROM c/FROM h}")"
}
par_arm "parallel accepts a wrapped target list: avg(v)+avg(v) FROM c" 'SELECT avg(v)+avg(v) FROM c'
par_match "and its answer matches the heap twin: avg(v)+avg(v) FROM c" 'SELECT avg(v)+avg(v) FROM c'
par_arm "parallel accepts a wrapped target list: round(avg(v)::numeric,3) FROM c" 'SELECT round(avg(v)::numeric,3) FROM c'
par_match "and its answer matches the heap twin: round(avg(v)::numeric,3) FROM c" 'SELECT round(avg(v)::numeric,3) FROM c'
par_arm "parallel accepts a wrapped target list: avg(v)::text FROM c" 'SELECT avg(v)::text FROM c'
par_match "and its answer matches the heap twin: avg(v)::text FROM c" 'SELECT avg(v)::text FROM c'

pgc_summary
