#!/usr/bin/env bash
#
# pgColumnar: a stripe flush opens each metadata table ONCE, not once per row (#445).
#
# The four metadata inserters (row_group, column_chunk, zone_map, bloom) each
# used to open their catalog, insert one row, and close. A flush inserts a chunk
# row per column and about one zone row per vector per column, so it opened a
# metadata relation on the order of natts * vectors times, and a profile of the
# numeric write path put that open and close cycle, not the encoding, at the top.
# A per-flush session now caches each metadata relation and reuses it for the
# whole flush.
#
# The witness is the DEBUG1 line the flush emits once per stripe:
#   "pgcolumnar metadata flush: opens=N tables=M"
# where N is the number of REAL relation opens during the flush.
#
# This suite pins the property that is the whole point: the open count is a small
# constant, and it does NOT grow with the column count. That equality IS the
# removal proof. Revert the reuse (make open_columnar_table always table_open, or
# drop the session bracket in the flush) and the wide flush opens far more
# relations than the narrow one, so the "same for narrow and wide" check goes RED
# on its own, with no mutation needed. The absolute-bound and heap-mirror checks
# fence it further.
#
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

q() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -Atqc "$1" 2>/dev/null
}

# One full stripe of exactly stripe_row_limit rows flushes once. Small limits
# keep it fast: 2000 rows / 500 per vector = 4 vectors, so 5 zone rows per column.
LIMITS="SET pgcolumnar.stripe_row_limit=2000; SET pgcolumnar.chunk_group_row_limit=500;"

# opens <colcount-fixture-insert> -> the N from the flush's DEBUG1 witness.
# The SET and the INSERT share one session, so the limits reach the flush.
opens() { # opens <insertSql>
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -c "SET client_min_messages=debug1;" -c "$LIMITS" -c "$1" 2>&1 |
		grep -oE 'metadata flush: opens=[0-9]+' | tail -1 | grep -oE '[0-9]+'
}

# ---- premise: the witness fires at all --------------------------------------

q "CREATE TABLE tprobe (a int, b int, c int) USING pgcolumnar;" >/dev/null
probe="$(opens "INSERT INTO tprobe SELECT g, g*2, g%7 FROM generate_series(1,2000) g;")"
# Not a premise: with the flush session never begun (a one-line pgcolumnar change)
# no witness line is emitted, so this is an ordinary check (#1339).
check_text "the flush emits the metadata-open witness" \
	"$([ -n "$probe" ] && echo yes || echo no)" "yes"

# ---- the batched open count is a small constant -----------------------------

# A 3-column and a 20-column table, each flushed once. Batched, both open the
# same small set of metadata tables (row_group, column_chunk, zone_map, and bloom
# if built), so the counts are EQUAL and small. Un-batched, the wide table opens
# ~natts more chunk rows and ~natts*vectors more zone rows, so its count is far
# larger than the narrow one and this equality FAILS.
q "CREATE TABLE tnarrow (a int, b int, c int) USING pgcolumnar;" >/dev/null
q "CREATE TABLE twide (c00 int, c01 int, c02 int, c03 int, c04 int, c05 int,
	c06 int, c07 int, c08 int, c09 int, c10 int, c11 int, c12 int, c13 int,
	c14 int, c15 int, c16 int, c17 int, c18 int, c19 int) USING pgcolumnar;" >/dev/null

n_narrow="$(opens "INSERT INTO tnarrow SELECT g, g*2, g%7 FROM generate_series(1,2000) g;")"
n_wide="$(opens "INSERT INTO twide SELECT g,g,g,g,g,g,g,g,g,g,g,g,g,g,g,g,g,g,g,g FROM generate_series(1,2000) g;")"

check_num "narrow (3 col) and wide (20 col) flush open the same number of metadata relations" \
	"$n_narrow" "$n_wide"

# Absolute bound: batched is the count of distinct metadata tables, a small
# constant near four. Un-batched a 20-column flush opens ~120, so this bound also
# goes RED without the batching.
check_text "a 20-column flush opens a small constant number of metadata relations, not one per row" \
	"$([ -n "$n_wide" ] && [ "$n_wide" -le 6 ] && echo small || echo "big:[$n_wide]")" \
	"small"

# A floor as well as a ceiling. Every flush writes a row_group row, column_chunk
# rows and zone_map rows, so it opens at least those three tables. Without the
# floor a witness that counted nothing (opens=0 for every flush) passed both checks
# above: 0 equals 0, and 0 is small.
check_text "and the 20-column flush opens at least the three tables every flush writes" \
	"$([ -n "$n_wide" ] && [ "$n_wide" -ge 3 ] && echo yes || echo "no:[$n_wide]")" \
	"yes"

# ---- the writes are correct, batched or not ---------------------------------

# The batching must not change what is written. Mirror the wide table in heap and
# compare, so a corrupted or dropped metadata row would show as a count or sum
# mismatch.
q "CREATE TABLE twide_h (LIKE twide);" >/dev/null
# THE MIRROR IS WRITTEN FROM THE GENERATOR, NOT COPIED FROM twide. Copied, it read
# twide through the reader under test, so a writer that stored a wrong value put the
# same wrong value in both and the sum below still agreed.
q "INSERT INTO twide_h SELECT g,g,g,g,g,g,g,g,g,g,g,g,g,g,g,g,g,g,g,g FROM generate_series(1,2000) g;" >/dev/null

check_num "the wide table's row count survives the batched flush" \
	"$(q "SELECT count(*) FROM twide")" "2000"
check_num "every column reads back correct (sum over all 20 columns matches the heap mirror)" \
	"$(q "SELECT sum(c00+c01+c02+c03+c04+c05+c06+c07+c08+c09+c10+c11+c12+c13+c14+c15+c16+c17+c18+c19) FROM twide")" \
	"$(q "SELECT sum(c00+c01+c02+c03+c04+c05+c06+c07+c08+c09+c10+c11+c12+c13+c14+c15+c16+c17+c18+c19) FROM twide_h")"

# A min/max range read exercises the zone-map rows the flush wrote in a batch.
check_num "a zone-map-pruned range read matches heap (the batched zone rows are correct)" \
	"$(q "SELECT count(*) FROM twide WHERE c00 BETWEEN 500 AND 1500")" \
	"$(q "SELECT count(*) FROM twide_h WHERE c00 BETWEEN 500 AND 1500")"

# And the zone rows are read: the range excludes the last vector of the stripe
# (1501..2000 lies above 1500), and the zone row for that vector is what rules it out.
# Vectors Skipped alone cannot show that, because the reader also rules out vectors
# by value after decoding the qual column, with or without a zone map. So this reads
# the zone-map part: Skipped minus Ruled Out by Value. Without it, a flush that wrote
# no usable min/max still answered every range read correctly.
plan="$(q "SET max_parallel_workers_per_gather=0;
	EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) SELECT count(*) FROM twide WHERE c00 BETWEEN 500 AND 1500;")"
skipped="$(grep -oE 'Columnar Vectors Skipped: [0-9]+' <<<"$plan" | head -1 | grep -oE '[0-9]+')"
byvalue="$(grep -oE 'Columnar Vectors Ruled Out by Value: [0-9]+' <<<"$plan" | head -1 | grep -oE '[0-9]+')"
check_text "and that range read rules out a vector by its zone row alone" \
	"$([ -n "$skipped" ] && [ -n "$byvalue" ] && [ $((skipped - byvalue)) -ge 1 ] && echo yes || echo "no:[skipped $skipped, by value $byvalue]")" "yes"

pgc_summary
