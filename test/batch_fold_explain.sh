#!/usr/bin/env bash
#
# pgColumnar #602: EXPLAIN ANALYZE must not report "Columnar Batch Fold: yes"
# for an execution that fell back to the row path.
#
# The ungrouped vectorized aggregate decides fold eligibility from the query
# shape at Begin (so plain EXPLAIN can report what will be attempted), and
# records during execution whether the fold actually ran. The two can disagree:
# a column added after some row groups exist is predicted-eligible, but the
# first old group is missing the column, pgcolumnar_native_batch_fold returns
# false before claiming any group, and the whole scan runs the row path.
# Printing the prediction under ANALYZE reported "yes" for that row-path run,
# which is exactly the line a fold-path measurement pins its premise on.
#
# Plain EXPLAIN keeps the prediction: with no execution there is nothing else
# to report, and "will this shape fold" is the useful answer there.
#
# Usage:  test/batch_fold_explain.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

GUC="SET pgcolumnar.enable_ungrouped_vector_agg = on;"

# Scalar under this suite's GUCs. tail -1 drops the SET tags psql prints
# ahead of the result row.
sq() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "$GUC $1" 2>/dev/null | tail -1
}

# The "Columnar Batch Fold" value out of an EXPLAIN, or empty if the plan has
# no such line (wrong node planned): check_text refuses empty, so a fixture
# that misses the custom aggregate node fails by name instead of passing.
fold_line() {	# fold_line <explain-prefix> <query>
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "$GUC $1 $2" 2>/dev/null \
		| grep "Columnar Batch Fold" | grep -oE "yes|no" | head -1
}

# ---- fixtures ---------------------------------------------------------------
# bfe_old: 50k rows written BEFORE v exists, 10k after. The old groups have no
# chunk for v, so the fold must refuse the relation at execution.
# bfe_ctrl: the same 10k post-ADD rows, columns present from the start, so the
# same query shape genuinely folds. v is NULL on every pre-ADD row, so both
# tables aggregate the same 10k generate_series values.
psql_run "CREATE TABLE bfe_old(q int) USING pgcolumnar;"
psql_run "INSERT INTO bfe_old SELECT 1 + g % 100 FROM generate_series(1,50000) g;"
psql_run "ALTER TABLE bfe_old ADD COLUMN v int;"
psql_run "INSERT INTO bfe_old SELECT 1 + g % 100, g FROM generate_series(1,10000) g;"
psql_run "CREATE TABLE bfe_ctrl(q int, v int) USING pgcolumnar;"
psql_run "INSERT INTO bfe_ctrl SELECT 1 + g % 100, g FROM generate_series(1,10000) g;"

Q_OLD="SELECT sum(v) FROM bfe_old WHERE q <= 50"
Q_CTRL="SELECT sum(v) FROM bfe_ctrl WHERE q <= 50"
ANALYZE_PFX="EXPLAIN (ANALYZE, TIMING OFF, COSTS OFF, SUMMARY OFF)"
PLAIN_PFX="EXPLAIN (COSTS OFF)"

# ---- premises ---------------------------------------------------------------
check_num "premise: fixture loaded every row" \
	"$(sq "SELECT count(*) FROM bfe_old;")" "60000"

# The answers, against an oracle that touches neither table.
want_sum="$(sq "SELECT sum(g) FROM generate_series(1,10000) g WHERE 1 + g % 100 <= 50;")"
check_num "premise: fallback table returns the right sum" "$(sq "$Q_OLD;")" "$want_sum"
check_num "premise: control table returns the right sum" "$(sq "$Q_CTRL;")" "$want_sum"

# The control genuinely folds, and ANALYZE says so. Guards the fix against
# breaking the true positive.
check_text "control: ANALYZE reports the fold that ran" \
	"$(fold_line "$ANALYZE_PFX" "$Q_CTRL")" "yes"

# Plain EXPLAIN keeps reporting the prediction: the shape IS eligible.
check_text "fallback table: plain EXPLAIN still reports the prediction" \
	"$(fold_line "$PLAIN_PFX" "$Q_OLD")" "yes"

# ---- the check (#602) -------------------------------------------------------
# Execution on bfe_old cannot fold (old groups lack v), so ANALYZE must say no.
# On the pre-fix code this read "yes": the prediction, printed as if it ran.
check_text "fallback table: ANALYZE reports the row path that actually ran" \
	"$(fold_line "$ANALYZE_PFX" "$Q_OLD")" "no"

# ---- fallback-exists pin ----------------------------------------------------
# The parallel partial fold cannot take this fallback (its group counter has
# already advanced) and refuses the relation with a hard error. When the
# planner gives us the partial fold, that error is independent proof the
# fixture still forces the fallback. If the fold ever learns to read a column
# added after some row groups, this and the "no" check above go red together,
# which is the loud revisit this suite wants.
PGUC="$GUC SET pgcolumnar.enable_parallel_vector_agg = on;
SET max_parallel_workers_per_gather = 2; SET parallel_setup_cost = 0;
SET parallel_tuple_cost = 0; SET min_parallel_table_scan_size = 0;"
pplan="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
	-d "$PGC_DB" -At -c "$PGUC $PLAIN_PFX $Q_OLD;" 2>/dev/null)"
# case patterns, not a pipe into an early-exit grep: piping a captured string
# into a quiet-mode reader loses the match to EPIPE under pipefail (selftest
# rule #486 — whose scanner reads these comment lines too).
par_planned=no
case "$pplan" in
	*Gather*)
		case "$pplan" in
			*"Columnar Batch Fold"*) par_planned=yes ;;
		esac ;;
esac
# A PREMISE, NOT A NOTE. The partial fold is planned on every major (15-19,
# measured), and when it was not, this printed a note and the check below simply
# did not run: the suite reported 6/6 and rc=0 with its parallel arm gone.
check "premise: with parallelism free, the planner gives the parallel partial fold" \
	"$par_planned" "yes"
if [ "$par_planned" = yes ]; then
	perr_out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "$PGUC $Q_OLD;" 2>&1)"
	perr_hits="$(printf '%s\n' "$perr_out" | grep -c "cannot fold a relation with a column added")"
	check_num "parallel partial fold refuses the fallback relation" "$perr_hits" "1"
else
	# recorded either way, so the arm cannot leave the count by not running
	check_num "parallel partial fold refuses the fallback relation" "not planned" "1"
fi

pgc_summary
