#!/usr/bin/env bash
#
# pgColumnar native per-vector skipping (Phase D5b): within a row group that a
# predicate does not prune wholesale, individual 1024-value vectors whose per-
# vector zone map rules them out are neither decoded nor emitted (native spec
# 7.1). This suite forces a single large row group (so nothing is pruned at the
# row-group level) with many vectors, and shows a selective range prunes vectors:
# "Columnar Vectors Skipped" > 0 while "Chunk Groups Removed by Filter"
# stays 0. Results always match a heap mirror, since skipping is a pure
# optimization the executor's re-check makes correctness-neutral.
#
# Usage:  test/native_vecskip.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# One row group (stripe limit above the row count) of 8 vectors x 1024 rows.
# id is monotonic, so each vector's min/max is tight and non-overlapping.
GEN="SELECT g AS id, (g % 97) AS v FROM generate_series(1, 8192) g"

psql_run "CREATE TABLE h (id int, v int);"
psql_run "CREATE TABLE n (id int, v int) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('n', stripe_row_limit => 16384, chunk_group_row_limit => 1024);"
psql_run "INSERT INTO h $GEN;"
psql_run "INSERT INTO n $GEN;"

# EXPLAIN ANALYZE counters for a query.
counter() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) $2" 2>/dev/null \
		| grep "$1" | grep -oE '[0-9]+' | head -1
}

# The plan text for a query, and whether it is the scalar columnar custom scan.
# "Columnar Projected Columns" is that node's own marker: the vectorized
# aggregate node does not report it and no other node reports it at all.
explain_of() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) $1" 2>/dev/null
}
is_scalar_scan() {
	# grep -c, not grep -q: see lib.sh's pgc_is_columnar_scan for why and for the
	# measurement. This file holds the worst instance of the shape -- the arm at
	# "premise: and it is not the scalar scan" WANTS "no", so a spurious EPIPE
	# answer makes that premise pass for the wrong reason. Vacuity, not a red.
	[ "$(explain_of "$1" | grep -c 'Columnar Projected Columns' || true)" != 0 ] \
		&& echo yes || echo no
}

# The node, before any counter is read out of it.
#
# Every "Columnar Vectors Skipped" and "Chunk Groups Removed by Filter" below is
# read from this query's plan. If the planner stops choosing the columnar custom
# scan, counter() returns empty and the comparisons start reporting a skipping
# regression that is really a plan change. Assert the node first so the failure
# names the actual cause.
check "the plan under test is a columnar custom scan" \
	"$(is_scalar_scan 'SELECT id FROM n WHERE id BETWEEN 100 AND 200')" "yes"

check "row count" "$(q 'SELECT count(*) FROM n;')" "8192"
check "single row group" \
	"$(q "SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = pgcolumnar.get_storage_id('n');")" \
	"1"

# A range confined to one vector: correct, prunes vectors, prunes no whole group.
Q="SELECT id, v FROM n WHERE id BETWEEN 100 AND 200"
check "range parity" \
	"$(pgc_set_hash "$Q")" \
	"$(pgc_set_hash 'SELECT id, v FROM h WHERE id BETWEEN 100 AND 200')"
# EXACTLY seven, not "> 0" (#1341): the range sits inside vector 0 of eight with
# tight, disjoint id ranges, so the other seven are ruled out. Measured, with the
# zone-map loop never reaching the last vector the count was 6, and "> 0" held.
check "the seven vectors outside the range are skipped" \
	"$(counter 'Columnar Vectors Skipped' "SELECT id FROM n WHERE id BETWEEN 100 AND 200")" "7"
check "no whole group removed" "$(counter 'Columnar Chunk Groups Removed by Filter' "SELECT id FROM n WHERE id BETWEEN 100 AND 200")" "0"
# BY THE ZONE MAPS, without decoding (#1341). The skip count alone cannot say which
# mechanism skipped: with the zone-map rule removed, #452's by-value refinement
# decodes the id column of all eight vectors and rules the same seven out, and the
# count is still 7. Measured: decodes|ruled-by-value is 1|0 here, 8|7 with the
# zone-map rule gone, and 2|1 with the loop stopping short of the last vector.
check "the zone maps ruled them out without decoding them: one vector decoded, none ruled out by value" \
	"$(counter 'Columnar Vector Decodes' "SELECT id FROM n WHERE id BETWEEN 100 AND 200")|$(counter 'Columnar Vectors Ruled Out by Value' "SELECT id FROM n WHERE id BETWEEN 100 AND 200")" "1|0"

# A predicate matching every vector prunes none.
check "non-selective removes no vectors" \
	"$(counter 'Columnar Vectors Skipped' 'SELECT id FROM n WHERE id > 0')" "0"

# ---- the vectorized aggregate must report the counter too --------------------
#
# "Columnar Vectors Skipped" is emitted by the scalar node only. The two
# vectorized aggregate nodes print Chunk Groups Total/Read/Removed, Usable Skip
# Predicates and Vector Predicates -- but not this one -- so a plan cannot say
# whether per-vector skipping happened on the aggregate path. That is the path
# where it matters most: #512 is precisely about the fold and the skip vector
# disagreeing, and the number that would show it is the one not printed.
#
# Same table, same predicate as the scalar checks above, so the two arms differ
# only in the node.
# pgcolumnar.enable_ungrouped_vector_agg defaults to OFF, so the aggregate node
# has to be asked for. Without the SET this falls back to the scalar scan -- which
# is what the first version of this check did, and the premise below caught it
# passing for that reason.
AGGQ="SELECT count(*), sum(v) FROM n WHERE id BETWEEN 100 AND 200"
explain_agg() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At \
		-c "SET pgcolumnar.enable_ungrouped_vector_agg = on;
		    EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) $1" 2>/dev/null
}

# Premise, and it has teeth: if the planner declines the vectorized aggregate,
# this query falls back to the scalar scan -- which DOES print the line, so the
# check below would pass while testing nothing at all.
check "premise: the aggregate arm really is a vectorized aggregate" \
	"$([ "$(explain_agg "$AGGQ" | grep -c 'Columnar Vectorized Aggregates' || true)" != 0 ] \
		&& echo yes || echo no)" \
	"yes"
check "premise: and it is not the scalar scan" \
	"$([ "$(explain_agg "$AGGQ" | grep -c 'Columnar Projected Columns' || true)" != 0 ] \
		&& echo yes || echo no)" "no"

check "the vectorized aggregate reports Columnar Vectors Skipped" \
	"$([ "$(explain_agg "$AGGQ" | grep -c 'Columnar Vectors Skipped' || true)" != 0 ] \
		&& echo yes || echo no)" \
	"yes"
# And the number it reports is the scalar node's, on the same predicate: a line
# that printed 0, or anything else, would answer #512's question wrongly.
check "and the aggregate skips the same seven vectors" \
	"$(explain_agg "$AGGQ" | grep 'Columnar Vectors Skipped' | grep -oE '[0-9]+' | head -1)" "7"

# Boundary and cross-vector ranges still return exactly the heap rows.
check "cross-vector range parity" \
	"$(pgc_set_hash 'SELECT id, v FROM n WHERE id BETWEEN 2000 AND 5000')" \
	"$(pgc_set_hash 'SELECT id, v FROM h WHERE id BETWEEN 2000 AND 5000')"
check "last-vector range parity" \
	"$(pgc_set_hash 'SELECT id, v FROM n WHERE id BETWEEN 8000 AND 9000')" \
	"$(pgc_set_hash 'SELECT id, v FROM h WHERE id BETWEEN 8000 AND 9000')"
check "empty range parity" \
	"$(q 'SELECT count(*) FROM n WHERE id BETWEEN 100000 AND 200000;')" \
	"0"

pgc_summary
