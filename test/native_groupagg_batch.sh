#!/usr/bin/env bash
#
# pgColumnar #708: the grouped vectorized aggregate folds column-at-a-time.
#
# Before this, the grouped path (GROUP BY over one columnar relation) was fully
# row-at-a-time: PgColumnarReadNextRow per row, then slot staging,
# ExecStoreVirtualTuple, ExecQual, one ExecEvalExpr per group key, one
# FunctionCall1Coll hash per key and one FunctionCall2Coll equality per probe --
# six to eight fmgr calls a row, measured at ~920 instructions per row. The
# ungrouped path already had pgcolumnar_native_batch_fold; grouped had no
# equivalent.
#
# The grouped batch fold walks row groups, gathers each needed column's packed
# values directly, applies the delete mask, the skipped-vector map and the scan
# keys inline, and hash-probes with an inline hash and equality for key types
# whose equality IS bitwise Datum equality.
#
# TWO THINGS MUST BE PROVED SEPARATELY, and this suite is built around the
# distinction (the #545 rule, `assert-work-done-not-only-correctness`):
#
#   * The ANSWERS are unchanged. An optimisation that stops optimising still
#     returns correct answers, so the oracle arms below cannot see the fold at
#     all. They exist to catch a fold that folds WRONGLY.
#
#   * The WORK happened. "Columnar Batch Fold: yes" on the grouped node under
#     EXPLAIN ANALYZE is the only assertion that goes red when the fold stops
#     running. Delete the pgcolumnar_groupagg_batch_fold call in
#     pgcolumnar_groupagg_build and every oracle arm here stays green while the
#     work-done arms go red. That is the removal proof.
#
# Eligibility is asserted in BOTH directions. A gate that never refuses is not a
# gate, so every shape the fold must decline has its own arm AND its own oracle:
# a by-reference key, an expression key, a non-batchable aggregate, and a `<>`
# filter that no scan key expresses exactly (#715 -- if that gate did not carry
# over to the grouped path, the fold's scan-key loop would count the excluded
# rows, which is a wrong answer and not a slow one).
#
# Usage:  test/native_groupagg_batch.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# The grouped path is opt-in. Set it on the database so every connection the
# helpers open inherits it.
psql_run "ALTER DATABASE $PGC_DB SET pgcolumnar.enable_group_vectorization = on;"

q() {	# q QUERY -> rows, one per line, tab separated
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -F$'\t' -c "$1" 2>/dev/null
}

# One scalar.
q1() { q "$1" | tail -1; }

# Is this planned as the grouped vectorized node? "Columnar Vectorized Group
# Keys" is emitted by no other node, so a positive grep proves the node rather
# than an absence test a fallback would also satisfy.
is_groupvec() {	# query -> yes|no
	# grep -c on a captured value, not a pipe into grep -q; see lib.sh's
	# pgc_is_columnar_scan for the mechanism and the measurement.
	local _plan
	_plan="$(q "EXPLAIN (COSTS OFF) $1")"
	[ "$(grep -c 'Columnar Vectorized Group Keys' <<<"$_plan" || true)" -ne 0 ] \
		&& echo yes || echo no
}

# The grouped node's "Columnar Batch Fold" value, or empty when the plan has no
# such line. check_text refuses empty, so a fixture that lost the custom node
# fails by name instead of passing vacuously.
fold_of() {	# fold_of <explain-prefix> <query> -> yes|no
	q "$1 $2" | grep 'Columnar Batch Fold' | grep -oE 'yes|no' | head -1
}

ANALYZE_PFX="EXPLAIN (ANALYZE, TIMING OFF, COSTS OFF, SUMMARY OFF)"
PLAIN_PFX="EXPLAIN (COSTS OFF)"

# agree LABEL QUERY_TEMPLATE: run the same query against the columnar table and
# its heap mirror and require byte-identical ordered output. TBL is substituted.
# Both sides must be non-empty, so a query that errors on one arm fails here
# rather than comparing nothing with nothing (#418).
# The table pair is a parameter, not a constant. It was hardcoded to gbb/gbb_h
# at first, so the later gbs fixture substituted the WRONG table into %T, the
# query errored on a column that table does not have, and the arm reported "the
# columnar arm returned no rows" -- which reads exactly like over-aggressive
# vector skipping returning an empty result, and was not.
agree_in() {	# agree_in TABLE LABEL "SELECT ... FROM %T ..."
	local tbl="$1" label="$2" tmpl="$3" col heap
	col="$(q "${tmpl//%T/$tbl}" | sort | md5sum | cut -d' ' -f1)"
	heap="$(q "${tmpl//%T/${tbl}_h}" | sort | md5sum | cut -d' ' -f1)"
	# md5 of empty input is a fixed string, so require the columnar arm produced
	# rows at all before trusting the comparison.
	if [ -z "$(q "${tmpl//%T/$tbl}" | head -1)" ]; then
		pgc_fail "$label" "the columnar arm returned no rows, so nothing was compared"
		return 1
	fi
	check_text "$label" "$col" "$heap"
}
agree() { agree_in gbb "$1" "$2"; }

# ---- fixture ---------------------------------------------------------------
# 200k rows over several row groups. k is the ordinary integer group key (8
# groups); k8 a bigint second key; f a float8 key (by value, so eligible, but
# its equality is NOT bitwise -- -0.0 equals 0.0 -- so it must group through the
# type's own equality function); t a text key (by reference, ineligible); v and
# w the aggregate payloads. Every 997th row has a NULL k, so the null-key group
# is exercised on the fold path rather than assumed.
#
# ts and d are here because timestamp and date are in the fold's
# bitwise-equality key list and nothing else in this tree groups a folded query
# by either. They are also different widths (8 bytes and 4), so they exercise
# two different gather strides rather than one.
DDL="k int, k8 bigint, f float8, t text, ts timestamp, d date, v int, w int"
GEN="SELECT CASE WHEN g % 997 = 0 THEN NULL ELSE g % 8 END,
            (g % 5)::bigint,
            (g % 4)::float8,
            't' || (g % 6),
            timestamp '2024-01-01 00:00:00' + ((g % 7) * interval '1 day'),
            date '2024-03-01' + (g % 9),
            g,
            g % 100
     FROM generate_series(1,200000) g"

psql_run "CREATE TABLE gbb($DDL) USING pgcolumnar;"
psql_run "INSERT INTO gbb $GEN;"
psql_run "CREATE TABLE gbb_h($DDL);"
psql_run "INSERT INTO gbb_h $GEN;"

check_num "premise: fixture loaded every row (columnar)" "$(q1 'SELECT count(*) FROM gbb;')" 200000
check_num "premise: fixture loaded every row (heap)"     "$(q1 'SELECT count(*) FROM gbb_h;')" 200000
check_num "premise: the fixture really has null keys" \
	"$(q1 'SELECT count(*) FROM gbb WHERE k IS NULL;')" \
	"$(q1 'SELECT count(*) FROM gbb_h WHERE k IS NULL;')"
check_num "premise: the fixture spans more than one row group" \
	"$(q1 "SELECT (count(*) > 1)::int FROM pgcolumnar.row_group
	       WHERE storage_id = pgcolumnar.get_storage_id('gbb'::regclass);")" 1

# ---- the shapes the fold must take -----------------------------------------
Q_PLAIN="SELECT k, count(*), sum(v) FROM %T GROUP BY k"
Q_FILT="SELECT k, count(*), sum(v) FROM %T WHERE v < 50000 GROUP BY k"
Q_TWOKEY="SELECT k, k8, count(*), sum(w) FROM %T GROUP BY k, k8"
Q_FLOAT="SELECT f, count(*), sum(v) FROM %T GROUP BY f"
Q_COUNTSTAR="SELECT k, count(*) FROM %T GROUP BY k"
Q_TS="SELECT ts, count(*), sum(v) FROM %T GROUP BY ts"
Q_DATE="SELECT d, count(*), sum(w) FROM %T GROUP BY d"

for pair in "plain:$Q_PLAIN" "filtered:$Q_FILT" "two keys:$Q_TWOKEY" \
            "float8 key:$Q_FLOAT" "count(*):$Q_COUNTSTAR" \
            "timestamp key:$Q_TS" "date key:$Q_DATE"; do
	label="${pair%%:*}"; tmpl="${pair#*:}"
	qcol="${tmpl//%T/gbb}"
	check_text "$label: the grouped node is planned" "$(is_groupvec "$qcol")" yes
	check_text "$label: the fold ran (work done)" \
		"$(fold_of "$ANALYZE_PFX" "$qcol")" yes
	agree "$label: answers match the heap mirror" "$tmpl"
done

# ---- the shapes the fold must DECLINE, each with its own oracle -------------
# A gate that never refuses is not a gate. Each of these must still be the
# grouped node (so the "no" is the fold declining, not the node being absent)
# and must still return the right answer on the row path.
Q_TEXTKEY="SELECT t, count(*), sum(v) FROM %T GROUP BY t"
Q_EXPRKEY="SELECT k + 1, count(*) FROM %T GROUP BY k + 1"
Q_MINMAX="SELECT k, min(v), max(v) FROM %T GROUP BY k"
Q_NEQ="SELECT k, count(*), sum(v) FROM %T WHERE v <> 5 GROUP BY k"

for pair in "text key:$Q_TEXTKEY" "expression key:$Q_EXPRKEY" \
            "min/max:$Q_MINMAX" "<> filter:$Q_NEQ"; do
	label="${pair%%:*}"; tmpl="${pair#*:}"
	qcol="${tmpl//%T/gbb}"
	# The decline must be the FOLD's, read off the grouped node. This branch was
	# a check_skip, and it never fired on 15-19: with the node gone the gate
	# under test is unreachable, so a skip there was a silent pass. A shape the
	# node stops taking is now a red under the arm's own name.
	if [ "$(is_groupvec "$qcol")" = yes ]; then
		check_text "$label: the fold declined" \
			"$(fold_of "$ANALYZE_PFX" "$qcol")" no
	else
		check_text "$label: the fold declined" \
			"the grouped node is not planned" no
	fi
	agree "$label: answers match the heap mirror" "$tmpl"
done

# The <> arm is the #715 gate carried onto the grouped path, and it is the one
# whose failure is a WRONG ANSWER rather than a slow one: the fold's only
# per-row filter is the scan-key loop, and `<>` produces no scan key at all.
# Pin the count independently of the md5 so the failure names the number.
check_num "<> filter: the excluded rows really are excluded" \
	"$(q1 'SELECT count(*) FROM gbb WHERE v <> 5;')" \
	"$(q1 'SELECT count(*) FROM gbb_h WHERE v <> 5;')"
check_num "premise: the <> filter excludes something at all" \
	"$(q1 'SELECT (count(*) < 200000)::int FROM gbb_h WHERE v <> 5;')" 1

# ---- deletes: the fold must honour the delete mask --------------------------
psql_run "DELETE FROM gbb   WHERE v % 7 = 0;"
psql_run "DELETE FROM gbb_h WHERE v % 7 = 0;"
check_num "premise: the delete removed rows" \
	"$(q1 'SELECT (count(*) < 200000)::int FROM gbb;')" 1
check_text "deletes: the fold still ran" \
	"$(fold_of "$ANALYZE_PFX" "${Q_PLAIN//%T/gbb}")" yes
agree "deletes: answers match the heap mirror" "$Q_PLAIN"

# ---- toggle-differential: byte-exact against the same rows, path off --------
# The fold changes accumulation ORDER for nothing: it folds in row order exactly
# as the row path does. Exact aggregates must be byte-identical with the grouped
# path off (a scalar Agg over the columnar scan), which is a stronger statement
# than agreeing with a heap whose physical order differs.
#
# The "off" arm sets the GUC through PGOPTIONS rather than a leading SET in the
# same -c string. psql prints a "SET" command tag into -At output, so the two
# arms differed by one line and the comparison failed on the tag rather than on
# any answer -- a difference that would have read as a real defect.
tog() {	# tog LABEL QUERY(on gbb)
	local label="$1" query="$2" off on
	off="$(PGOPTIONS='-c pgcolumnar.enable_group_vectorization=off' \
		q "$query" | sort | md5sum)"
	check_text "$label [premise: node fires with the path on]" "$(is_groupvec "$query")" yes
	on="$(q "$query" | sort | md5sum)"
	# A premise for the OFF arm too: with the path off the grouped node must NOT
	# be planned, or both arms ran the same plan and the differential is vacuous.
	check_text "$label [premise: the off arm is not the grouped node]" \
		"$(PGOPTIONS='-c pgcolumnar.enable_group_vectorization=off' \
		   is_groupvec "$query")" no
	check_text "$label" "$off" "$on"
}
tog "toggle-differential: plain"    "${Q_PLAIN//%T/gbb}"
tog "toggle-differential: filtered" "${Q_FILT//%T/gbb}"
tog "toggle-differential: two keys" "${Q_TWOKEY//%T/gbb}"

# ---- float8 keys group by the type's equality, not by bits ------------------
# -0.0 and 0.0 are ONE group in PostgreSQL and have different bit patterns, so a
# fold that hashed or compared raw Datum bits for float8 would split them. This
# arm is why float8 keys go through the type's own hash and equality functions
# even though the column is passed by value and the fold can read it.
# The negative zero has to be written as '-0'::float8, and the premise below
# asserts the sign bit really is set in storage. Spelled `-0.0` in a VALUES list
# it is an untyped literal, so it goes through NUMERIC -- which has no signed
# zero -- and reaches the column as +0.0. The first version of this fixture did
# exactly that: it stored three identical +0.0 bit patterns, so a bitwise probe
# would have grouped them correctly and the arm passed while proving nothing.
psql_run "CREATE TABLE gbz(f float8, v int) USING pgcolumnar;"
psql_run "INSERT INTO gbz VALUES ('0'::float8, 1), ('-0'::float8, 2), ('0'::float8, 4), ('NaN'::float8, 8), ('NaN'::float8, 16);"
psql_run "CREATE TABLE gbz_h(f float8, v int);"
psql_run "INSERT INTO gbz_h SELECT * FROM gbz;"
check_text "float8 zero: the fixture really stores a NEGATIVE zero" \
	"$(q1 "SELECT encode(float8send(f),'hex') FROM gbz WHERE v = 2;")" \
	"8000000000000000"
check_text "float8 zero: and a positive one beside it" \
	"$(q1 "SELECT encode(float8send(f),'hex') FROM gbz WHERE v = 1;")" \
	"0000000000000000"
check_text "float8 zero: the grouped node is planned" \
	"$(is_groupvec 'SELECT f, sum(v) FROM gbz GROUP BY f')" yes
# Without this the arm below is satisfied by a fold that never ran, which is the
# only way the bitwise-probe hazard it guards could reach the data at all.
check_text "float8 zero: and the fold actually ran on it" \
	"$(fold_of "$ANALYZE_PFX" 'SELECT f, sum(v) FROM gbz GROUP BY f')" yes
# Count the groups by counting the ROWS the folding query returns, not by
# wrapping it in an outer count(*): the wrapper changes the plan, the grouped
# node is not chosen underneath it, and the arm then reports on a query the fold
# never touched. It sat green through the float8 mutation for exactly that
# reason while the sums arm beside it went red.
check_num "float8 zero: -0.0 and 0.0 are one group, exactly as the heap says" \
	"$(q 'SELECT f, sum(v) FROM gbz GROUP BY f' | wc -l)" \
	"$(q 'SELECT f, sum(v) FROM gbz_h GROUP BY f' | wc -l)"
check_text "float8 zero: the grouped sums match the heap" \
	"$(q 'SELECT f, sum(v) FROM gbz GROUP BY f ORDER BY 1' | md5sum)" \
	"$(q 'SELECT f, sum(v) FROM gbz_h GROUP BY f ORDER BY 1' | md5sum)"

# ---- the skipped-vector cursor, actually exercised --------------------------
# The fold's per-row loop must advance each column's present index across a
# SKIPPED vector without reading it: the stream is packed by presence, so a
# skipped row's slot is consumed whether or not its value is read, and getting
# that wrong misaligns every later vector rather than losing one row. It is
# silent, and it only shows on data whose zone maps rule something out.
#
# Every arm above runs with `Columnar Vectors Skipped: 0`, so none of them reach
# that code at all. The loop was present and unexecuted, and a check that cannot
# reach the code it names is not evidence. This fixture makes the skip happen
# and asserts that it did before comparing anything: a small vector against a
# large row group, and a selector that matches in only a few vectors, so group
# level pruning cannot remove the whole row group and vector level skipping is
# what is left to do the work.
psql_run "CREATE TABLE gbs(k int, sel int, p1 int, p2 int, p3 int, p4 int, p5 int) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('gbs', stripe_row_limit => 65536, chunk_group_row_limit => 1024);"
SGEN="SELECT g % 8, CASE WHEN (g / 1024) % 40 = 0 THEN 1 ELSE 0 END,
             g, g % 3, g % 5, g % 7, g % 11
      FROM generate_series(1,200000) g"
psql_run "INSERT INTO gbs $SGEN;"
psql_run "CREATE TABLE gbs_h(k int, sel int, p1 int, p2 int, p3 int, p4 int, p5 int);"
psql_run "INSERT INTO gbs_h $SGEN;"

Q_SKIP="SELECT k, count(*), sum(p1) FROM %T WHERE sel = 1 GROUP BY k"
skip_plan="$(q "$ANALYZE_PFX ${Q_SKIP//%T/gbs}")"
check_text "skipped vectors: the fold ran" \
	"$(grep 'Columnar Batch Fold' <<<"$skip_plan" | grep -oE 'yes|no' | head -1)" yes
# THE PREMISE, and the whole point of this fixture. Without it the arm below is
# satisfied by a scan that skipped nothing, which is what every other arm here
# already is.
skipped="$(grep 'Columnar Vectors Skipped' <<<"$skip_plan" | grep -oE '[0-9]+' | head -1)"
check_num "skipped vectors: premise: the scan really skipped vectors" \
	"$([ -n "$skipped" ] && [ "$skipped" -gt 0 ] && echo 1 || echo 0)" 1
echo "      (Columnar Vectors Skipped = ${skipped:-unset})"
# And the row group was NOT pruned whole: if it had been there would be no
# surviving vectors for the cursor to step over, and the skip count above would
# be describing a scan that read nothing.
groups_read="$(grep 'Columnar Chunk Groups Read' <<<"$skip_plan" | grep -oE '[0-9]+' | head -1)"
check_num "skipped vectors: premise: a row group was still read" \
	"$([ -n "$groups_read" ] && [ "$groups_read" -gt 0 ] && echo 1 || echo 0)" 1
agree_in gbs "skipped vectors: answers match the heap mirror" "$Q_SKIP"
# Aggregates over four payload columns, so a cursor that misaligns on any one of
# them shows up as a wrong sum instead of being masked by a column that happens
# to stay in step. Four and not five: at five payload aggregates the cost model
# prefers core's GroupAggregate and this node is not chosen at all, which would
# make the arm test nothing. The node assertion below is what would say so.
Q_SKIP4="SELECT k, count(*), sum(p1), sum(p2), sum(p3), sum(p4) FROM %T WHERE sel = 1 GROUP BY k"
check_text "skipped vectors: premise: the wide arm is still the grouped node" \
	"$(is_groupvec "${Q_SKIP4//%T/gbs}")" yes
check_text "skipped vectors: the fold ran on the four-payload arm" \
	"$(fold_of "$ANALYZE_PFX" "${Q_SKIP4//%T/gbs}")" yes
agree_in gbs "skipped vectors: and their sums match the heap mirror" "$Q_SKIP4"

# ---- the parallel partial grouped fold --------------------------------------
# The fold has its own isPartial branch: each worker claims row groups through
# the shared atomic and emits per-worker transition states for a core Finalize to
# combine (#349). Nothing else in this tree exercises it, because
# parallel_vector_agg.sh's grouped arms all use a text key or an IS NOT NULL
# filter and so decline the fold. Untested new code is a claim like any other.
#
# The premises here are what stop this being satisfied by core's own parallel
# grouped plan, which also has a Gather and also launches workers: assert OUR
# parallel-aware node, assert workers were launched, and assert the fold ran.
PAR_GUC="SET max_parallel_workers_per_gather=4;
         SET parallel_setup_cost=0; SET parallel_tuple_cost=0;
         SET min_parallel_table_scan_size=0; SET min_parallel_index_scan_size=0;
         SET pgcolumnar.enable_parallel_vector_agg=on;"
Q_PAR="SELECT k, count(*), sum(v) FROM gbb GROUP BY k"
PEA="$(q "$PAR_GUC $ANALYZE_PFX $Q_PAR")"
check_text "parallel: our parallel-aware grouped node is under the Gather" \
	"$(grep -qi 'Parallel Custom Scan (PgColumnarScan)' <<<"$PEA" &&
	   grep -qi 'Columnar Vectorized Group Keys' <<<"$PEA" && echo yes || echo no)" yes
check_text "parallel: workers actually launched for that node" \
	"$(grep -qiE 'Workers Launched: [1-9]' <<<"$PEA" &&
	   grep -qi 'Columnar Vectorized Group Keys' <<<"$PEA" && echo yes || echo no)" yes
check_text "parallel: the fold ran in the partial node (work done)" \
	"$(grep 'Columnar Batch Fold' <<<"$PEA" | grep -oE 'yes|no' | head -1)" yes
# Values: the parallel fold must agree with the serial one, byte for byte. Each
# worker folds a distinct set of row groups, so a claim bug shows up here as a
# double count rather than an error.
#
# The settings go through PGOPTIONS, not a leading SET, for the same reason the
# toggle-differential above does: psql prints a "SET" command tag per statement
# into -At output, so an arm with six SETs and an arm with one differ by five
# lines before any row is compared. Written the obvious way this arm was red
# with byte-identical data underneath, which reads exactly like a parallel
# double-count and is not one.
PAR_OPTS='-c max_parallel_workers_per_gather=4 -c parallel_setup_cost=0 -c parallel_tuple_cost=0 -c min_parallel_table_scan_size=0 -c min_parallel_index_scan_size=0 -c pgcolumnar.enable_parallel_vector_agg=on'
check_text "parallel: the answers equal the serial fold, byte for byte" \
	"$(PGOPTIONS="$PAR_OPTS" q "$Q_PAR ORDER BY k" | md5sum)" \
	"$(PGOPTIONS='-c max_parallel_workers_per_gather=0' q "$Q_PAR ORDER BY k" | md5sum)"
check_text "parallel: and they equal the heap mirror" \
	"$(PGOPTIONS="$PAR_OPTS" q "$Q_PAR ORDER BY k" | md5sum)" \
	"$(q "SELECT k, count(*), sum(v) FROM gbb_h GROUP BY k ORDER BY k" | md5sum)"
# And the premise those two need: the parallel arm really did run in parallel.
# Without it both arms are the same serial plan and the comparison is vacuous.
gbb_par_plan="$(PGOPTIONS="$PAR_OPTS" q "$ANALYZE_PFX $Q_PAR")"
check_text "parallel: premise: the value arm's own plan launches workers" \
	"$(grep -qiE 'Workers Launched: [1-9]' <<<"$gbb_par_plan" && echo yes || echo no)" yes

# ---- a column added after some row groups: predicted yes, ran no ------------
# Same shape as #602 on the ungrouped node. The old row groups have no chunk for
# v, so the fold refuses the relation at execution and the whole scan runs the
# row path. Plain EXPLAIN has no execution to report and keeps the prediction.
psql_run "CREATE TABLE gba(k int) USING pgcolumnar;"
psql_run "INSERT INTO gba SELECT g % 8 FROM generate_series(1,50000) g;"
psql_run "ALTER TABLE gba ADD COLUMN v int;"
psql_run "INSERT INTO gba SELECT g % 8, g FROM generate_series(1,10000) g;"
Q_ADD="SELECT k, count(*), sum(v) FROM gba GROUP BY k"
check_text "add column: plain EXPLAIN reports the prediction" \
	"$(fold_of "$PLAIN_PFX" "$Q_ADD")" yes
check_text "add column: ANALYZE reports the row-path run" \
	"$(fold_of "$ANALYZE_PFX" "$Q_ADD")" no
check_num "add column: the answer is still right" \
	"$(q1 "SELECT sum(s) FROM (SELECT sum(v) AS s FROM gba GROUP BY k) x;")" \
	"$(q1 'SELECT sum(g) FROM generate_series(1,10000) g;')"

pgc_summary
