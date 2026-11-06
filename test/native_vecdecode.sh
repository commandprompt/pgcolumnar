#!/usr/bin/env bash
#
# pgColumnar: decode honours the skip vector (#452 phase 1b-i).
#
# "Columnar Vectors Skipped" has never meant what its name suggests. Decode runs
# BEFORE the skip vector exists -- pgcolumnar_native_decode_chunk at
# columnar_reader.c:1598, pgcolumnar_native_build_skipvec at :1615 -- so a vector
# the zone maps ruled out is decoded in full and the skip only stops it being
# turned into Datums. The counter says "not emitted", never "not decoded", and no
# existing counter can tell those apart.
#
# So this suite needs its own observable, "Columnar Vectors Decoded", and the
# central check is not that it moves but that it moves by EXACTLY the skipped
# count. "Decodes fewer" would pass on decoding one vector less out of thirty,
# which is the shape of a fix that does not work.
#
# Usage:  test/native_vecdecode.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# One row group of 32 vectors x 1024 rows. The stripe limit is above the row
# count so nothing is pruned at the row-group level and every difference below
# is per-vector. id is monotonic, so each vector's zone map is tight and
# non-overlapping, which is what lets a narrow range rule most of them out.
ROWS=32768
# w is NULL on every seventh row, so a vector carries nulls and the packed stream is
# indexed by presence rather than by row: a reader that ignores the validity bitmap
# reads the wrong value for every row after the first null (#1341 port; with no
# nulls, a fold that ignored the bitmap passed every check).
GEN="SELECT g AS id, (g % 97) AS v, CASE WHEN g % 7 = 0 THEN NULL ELSE g % 89 END AS w FROM generate_series(1, $ROWS) g"

psql_run "CREATE TABLE h (id int, v int, w int);"
psql_run "CREATE TABLE n (id int, v int, w int) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('n', stripe_row_limit => 65536, chunk_group_row_limit => 1024);"
psql_run "INSERT INTO h $GEN;"
psql_run "INSERT INTO n $GEN;"

explain_of() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) $1" 2>/dev/null
}

# A counter's value, or empty when the line is absent. Read from the plan text
# rather than from a second EXPLAIN, so every counter in one check comes from
# one run of one query.
counter_in() {  # counter_in <plan text> <label>
	grep -F "$2" <<<"$1" | grep -oE '[0-9]+' | head -1
}

has_line() { grep -qF "$2" <<<"$1" && echo yes || echo no; }
isnum() { [[ "$1" =~ ^[0-9]+$ ]]; }

# The narrow range lives inside a single vector; the wide one spans them all.
NARROW="SELECT id, v FROM n WHERE id BETWEEN 2000 AND 2050"
WIDE="SELECT id, v FROM n WHERE id BETWEEN 1 AND $ROWS"

PLAN_NARROW="$(explain_of "$NARROW")"
PLAN_WIDE="$(explain_of "$WIDE")"

# ---- the ground --------------------------------------------------------------
#
# Every number below is read out of these two plans. If the planner stops
# choosing the columnar custom scan, or the group splits, the comparisons start
# reporting a decode regression that is really a plan or a layout change. Assert
# the ground first so a failure names its own cause. None of these is a premise:
# each reads pgcolumnar's plan or its writer's row_group catalog, and a one-line
# pgcolumnar change reddens it (the custom scan off by default; a writer that
# reads stripe_row_limit wrong).

check "the narrow plan is a columnar custom scan" \
	"$(has_line "$PLAN_NARROW" 'Columnar Projected Columns')" "yes"
check "the wide plan is a columnar custom scan" \
	"$(has_line "$PLAN_WIDE" 'Columnar Projected Columns')" "yes"
check "one row group, so every difference is per-vector" \
	"$(q "SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = pgcolumnar.get_storage_id('n');")" \
	"1"
check "no whole group is pruned on the narrow query" \
	"$(counter_in "$PLAN_NARROW" 'Columnar Chunk Groups Removed by Filter')" "0"

# The two arms must differ in what the zone maps rule out, or the central check
# compares a number with itself and passes against any implementation.
SKIP_NARROW="$(counter_in "$PLAN_NARROW" 'Columnar Vectors Skipped')"
SKIP_WIDE="$(counter_in "$PLAN_WIDE" 'Columnar Vectors Skipped')"
check "the narrow range skips vectors" \
	"$(isnum "$SKIP_NARROW" && { [ "$SKIP_NARROW" -gt 0 ] && echo yes || echo no; } || echo "no reading [$SKIP_NARROW]")" "yes"
check "the wide range skips none, so the arms really differ" \
	"$SKIP_WIDE" "0"

# ---- the observable -------------------------------------------------------

check "EXPLAIN reports Columnar Vectors Decoded" \
	"$(has_line "$PLAN_WIDE" 'Columnar Vectors Decoded')" "yes"

DEC_WIDE="$(counter_in "$PLAN_WIDE" 'Columnar Vectors Decoded')"
check "and it is not stuck at zero when a full scan decodes everything" \
	"$([ "${DEC_WIDE:-0}" -gt 0 ] && echo yes || echo no)" "yes"

# ---- the fix itself -------------------------------------------------------

DEC_NARROW="$(counter_in "$PLAN_NARROW" 'Columnar Vectors Decoded')"

check "a ruled-out vector is not decoded" \
	"$(isnum "$DEC_NARROW" && isnum "$DEC_WIDE" && { [ "$DEC_NARROW" -lt "$DEC_WIDE" ] && echo yes || echo no; } || echo "no reading [$DEC_NARROW] [$DEC_WIDE]")" "yes"

# The check that has teeth. "Decodes fewer" is satisfied by decoding one vector
# less out of thirty-two, which is what a half-working mask does. Every vector
# the zone maps ruled out must go undecoded, so the two counters must account
# for the whole group between them.
# All three counters must be READ: with the plans failing, 0 + 0 = 0 used to pass.
check "and EVERY ruled-out vector is: decoded plus skipped is the group's vectors" \
	"$(isnum "$DEC_NARROW" && isnum "$SKIP_NARROW" && echo $(( DEC_NARROW + SKIP_NARROW )) || echo "no reading [$DEC_NARROW] [$SKIP_NARROW]")" \
	"$(isnum "$DEC_WIDE" && echo "$DEC_WIDE" || echo "no wide reading [$DEC_WIDE]")"

# ---- correctness, which is the risk this change carries -------------------
#
# Not decoding a vector leaves a hole in the chunk's raw buffer. The row
# producer steps its cursors past it, but the vectorized fold reads that buffer
# directly and re-checks every value, so a hole would be re-checked as
# uninitialized memory -- a wrong aggregate, silently, and only on data whose
# zone maps rule something out. #512 and #523 are that hazard; these are the
# checks that would catch it coming back.

check "narrow range parity with heap" \
	"$(pgc_set_hash "$NARROW")" \
	"$(pgc_set_hash 'SELECT id, v FROM h WHERE id BETWEEN 2000 AND 2050')"
check "cross-vector range parity" \
	"$(pgc_set_hash 'SELECT id, v FROM n WHERE id BETWEEN 3000 AND 9000')" \
	"$(pgc_set_hash 'SELECT id, v FROM h WHERE id BETWEEN 3000 AND 9000')"
check "boundary range parity, a range starting exactly on a vector edge" \
	"$(pgc_set_hash 'SELECT id, v FROM n WHERE id BETWEEN 1025 AND 2048')" \
	"$(pgc_set_hash 'SELECT id, v FROM h WHERE id BETWEEN 1025 AND 2048')"

check "premise: the nullable column has nulls inside the ranges read below" \
	"$(q 'SELECT count(*) - count(w) > 0 FROM h WHERE id BETWEEN 2000 AND 2050;')" "t"
check "cross-vector parity of a nullable column, nulls included" \
	"$(pgc_set_hash 'SELECT id, w FROM n WHERE id BETWEEN 3000 AND 9000')" \
	"$(pgc_set_hash 'SELECT id, w FROM h WHERE id BETWEEN 3000 AND 9000')"

# The aggregate path is the one that reads the raw buffer directly.
check_text "aggregate over a skipping range matches heap" \
	"$(q 'SELECT count(*), sum(v) FROM n WHERE id BETWEEN 2000 AND 2050;')" \
	"$(q 'SELECT count(*), sum(v) FROM h WHERE id BETWEEN 2000 AND 2050;')"
check_text "aggregate over a range spanning many skipped vectors matches heap" \
	"$(q 'SELECT count(*), sum(v), min(v), max(v) FROM n WHERE id BETWEEN 5000 AND 5200;')" \
	"$(q 'SELECT count(*), sum(v), min(v), max(v) FROM h WHERE id BETWEEN 5000 AND 5200;')"
check_num "a range matching no row at all is still correct" \
	"$(q 'SELECT count(*) FROM n WHERE id BETWEEN 100000 AND 200000;')" "0"

# ---- the fold, which is the path that reads the holes ----------------------
#
# The checks above run the SCALAR scan, which is safe by construction: it steps
# its cursors past a skipped vector and never looks at the bytes. The vectorized
# fold reads the packed stream directly, so it is the one that would read an
# undecoded hole, and it is OFF by default -- meaning every aggregate check
# above passed without ever touching the path at risk. That is the whole reason
# this section exists.

fold_run() {  # fold_run <sql>
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At \
		-c "SET max_parallel_workers_per_gather=0; SET pgcolumnar.enable_ungrouped_vector_agg=on;" \
		-c "$1" 2>&1 | tail -1
}
fold_plan() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At \
		-c "SET max_parallel_workers_per_gather=0; SET pgcolumnar.enable_ungrouped_vector_agg=on;" \
		-c "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) $1" 2>&1
}

FQ="SELECT count(*), sum(v) FROM n WHERE id BETWEEN 2000 AND 2050"

# Without this the two checks below are unreachable and would pass forever while
# guarding nothing: the fold declines most shapes, and a declined fold answers
# from the scalar path that was never at risk.
check "the aggregate really reaches the batch fold" \
	"$(fold_plan "$FQ" | grep -oE 'Columnar Batch Fold: [a-z]+' | head -1)" \
	"Columnar Batch Fold: yes"

check_text "the fold answers correctly over a range that skips vectors" \
	"$(fold_run "$FQ")" \
	"$(q 'SELECT count(*), sum(v) FROM h WHERE id BETWEEN 2000 AND 2050;')"

# A range deep in the table, so the fold must advance the present index across
# about nineteen leading skipped vectors before it reaches a row it keeps. That
# is the alignment risk: the packed stream is indexed by presence, so an index
# that does not advance over a skipped vector reads later values shifted by
# nineteen vectors' worth of rows and still returns a plausible number.
#
# The first range above starts in vector 1 and cannot show this: one leading
# skipped vector is too few for a misalignment to be obvious, and its answer
# could be right by luck.
# A nullable column through the fold: the packed stream holds only present values,
# so the fold must advance its present index by the validity bitmap. With the bitmap
# ignored it reads a shifted value for every row after the first null.
FQN="SELECT count(w), sum(w) FROM n WHERE id BETWEEN 2000 AND 2050"
check "a nullable aggregate reaches the fold" \
	"$(fold_plan "$FQN" | grep -oE 'Columnar Batch Fold: [a-z]+' | head -1)" \
	"Columnar Batch Fold: yes"
check_text "the fold answers count and sum of a nullable column over a skipping range" \
	"$(fold_run "$FQN")" \
	"$(q 'SELECT count(w), sum(w) FROM h WHERE id BETWEEN 2000 AND 2050;')"

FQ2="SELECT count(*), sum(v) FROM n WHERE id BETWEEN 20000 AND 20050"
check "the deep range reaches the fold too" \
	"$(fold_plan "$FQ2" | grep -oE 'Columnar Batch Fold: [a-z]+' | head -1)" \
	"Columnar Batch Fold: yes"
check_text "and it agrees with heap after many leading skipped vectors" \
	"$(fold_run "$FQ2")" \
	"$(q 'SELECT count(*), sum(v) FROM h WHERE id BETWEEN 20000 AND 20050;')"

# The check that can actually catch a consumer reading an undecoded hole.
#
# The two above cannot, and that is worth stating plainly: a fold that reads
# every hole PASSES them. Whatever palloc last left in the buffer is rejected by
# the fold's own scan-key recheck, so the wrong code returns the right answer.
# The hazard is real and the symptom is data-dependent, which is precisely the
# combination a test cannot pin down.
#
# So assert builds poison a skipped vector's bytes with 0xA5, which as an int4 is
# -1515870811. This predicate is chosen to ACCEPT that value while still letting
# the zone maps rule most vectors out: every id in the table is between 1 and
# 32768, so the lower bound excludes nothing and the upper bound rules out every
# vector past id 3000. A fold that reads the poison counts rows that are not
# there and the count runs away from heap's 3000.
#
# On a non-assert build there is no poison and this check is merely another
# parity check. The matrix runs assert builds, which is where it has teeth.
POISONQ="SELECT count(*), sum(v) FROM n WHERE id BETWEEN -2000000000 AND 3000"
check "the poison-accepting range reaches the fold" \
	"$(fold_plan "$POISONQ" | grep -oE 'Columnar Batch Fold: [a-z]+' | head -1)" \
	"Columnar Batch Fold: yes"
# Measured on the FOLD plan, not on a bare projection beside it.
#
# This premise used to read "Columnar Vectors Skipped" off
# "SELECT id FROM n WHERE ...", a scalar-arm plan, and infer that the fold arm
# skipped too. That inference is true today because both arms share one decode,
# but it makes the premise for a fold check a measurement of a different plan.
# It is also unfixable in its own terms: Vectors Skipped reads 0 on the fold arm
# whatever happens, because it is incremented in the row-production path the fold
# never enters (#542).
#
# "Columnar Vectors Decoded" does report correctly on the fold arm, so the
# premise is now a direct statement about the plan under test. Ids 1..3000 live
# in the first three vectors of 1024, so three is the whole of what this query
# needs and the other 29 are the saving.
FOLD_DEC="$(counter_in "$(fold_plan "$POISONQ")" 'Columnar Vectors Decoded')"
check_num "the FOLD ARM really decodes only the vectors it needs" "$FOLD_DEC" "3"

# The counter must be capable of reading high on this arm, or the check above is
# satisfied by a number that is simply always small. This is the case that would
# catch a fold which silently stopped skipping and decoded everything: it would
# return the right answer, read no poison, and fail nothing else we assert.
check "control: and the same counter reads every vector when nothing is skipped" \
	"$(counter_in "$(fold_plan 'SELECT count(*), sum(v) FROM n WHERE id >= 0')" 'Columnar Vectors Decoded')" \
	"32"
check_text "the fold does not count rows from a vector it never decoded" \
	"$(fold_run "$POISONQ")" \
	"$(q 'SELECT count(*), sum(v) FROM h WHERE id BETWEEN -2000000000 AND 3000;')"

pgc_summary
