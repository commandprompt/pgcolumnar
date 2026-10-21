#!/usr/bin/env bash
#
# The cost model must describe the geometry a table WAS WRITTEN WITH, not the
# geometry a write in the planning session would produce (#806).
#
# pgcolumnar.storage.row_group_limit records what the writer used. Nothing read
# it: the index-fetch penalty called pgcolumnar_effective_stripe_row_limit(),
# which returns the per-table option or else the PLANNING SESSION's GUC. So a
# session that set pgcolumnar.stripe_row_limit -- before a bulk load, say --
# repriced every columnar table it then planned against, including tables it had
# never touched.
#
# Measured on the same unchanged table, three row groups, written at the
# default, varying only the GUC at plan time:
#
#     plan-time stripe_row_limit    before      after
#              150000               775.26     775.28
#               20000               113.26     775.28
#                5000                36.26     775.28
#
# A 21x swing on a table that did not change. That is the shape of this suite's
# first check, and it needs no bound: the costs must be EQUAL.
#
# WHY THE SWEEP STOPS AT 20000 AND NOT LOWER. The penalty is one of two places
# the session GUC reached. The other is pgcolumnar_zonemap_survival, which uses
# it to guess how many groups exist and is NOT changed here -- substituting the
# written geometry there moved native_zonemap_narrow from wide=30/narrow=30 zone
# map reads to wide=570/narrow=66, reads that scale with table width, which is
# the property that suite exists to hold. So this change fixes the penalty and
# leaves the survival estimate, and a plan-time limit far enough below the
# written one still flips the plan through that second path: at 5000 the same
# table costs 87.79 rather than 775.07, because it stops being an index scan.
#
# That residual is real and is filed rather than hidden. This suite asserts what
# this change actually fixes; it would be dishonest to sweep a range the change
# does not cover and dishonest to pretend the range it does cover is the whole
# defect.
#
# These are planner costs, not timings, so this suite is not a timing suite and
# PGC_SKIP_TIMING does not apply to it (#787).
#
# Usage:  test/cost_written_geometry.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

ROWS=${PGC_CWG_ROWS:-400000}

# gb: written at the default limit.  ga: written at a much smaller one, in the
# session that does the writing, which is where the limit legitimately applies.
psql_run "DROP TABLE IF EXISTS gb;
	CREATE TABLE gb (id int, v int, t text) USING pgcolumnar;
	INSERT INTO gb SELECT g, g, 'r' || g FROM generate_series(1, $ROWS) g;
	CREATE INDEX gb_id ON gb (id);"
psql_run "DROP TABLE IF EXISTS ga;
	SET pgcolumnar.stripe_row_limit = 20000;
	SET pgcolumnar.chunk_group_row_limit = 20000;
	CREATE TABLE ga (id int, v int, t text) USING pgcolumnar;
	INSERT INTO ga SELECT g, g, 'r' || g FROM generate_series(1, $ROWS) g;
	CREATE INDEX ga_id ON ga (id);"
psql_run "ANALYZE ga; ANALYZE gb;"

groups() {  # table -> row groups actually written
	q "SELECT count(*) FROM pgcolumnar.row_group r
	   JOIN pgcolumnar.storage s USING (storage_id)
	   WHERE s.relation_oid = '$1'::regclass"
}
written_limit() {
	q "SELECT row_group_limit FROM pgcolumnar.storage WHERE relation_oid = '$1'::regclass"
}
# The costed plan for an index-driven fetch, under a stated plan-time GUC.
plan_line() {  # table, plan-time stripe_row_limit
	q "SET pgcolumnar.stripe_row_limit = $2; SET enable_seqscan = off;
	   SET max_parallel_workers_per_gather = 0;
	   EXPLAIN SELECT id, v FROM $1 WHERE id <= 100" | grep -m1 'cost='
}
cost_at() { plan_line "$1" "$2" | grep -oE 'cost=[0-9.]+\.\.[0-9.]+' | sed 's/.*\.\.//'; }

# --- premises: the fixture is the shape the checks assume -------------------
# Against the constant: two failed counts read empty and would agree.
check_text "premise: both tables hold the same number of rows" \
	"$(q "SELECT count(*) FROM ga")|$(q "SELECT count(*) FROM gb")" "$ROWS|$ROWS"
check "gb was written at the default limit, in few groups" \
	"$([ "$(groups gb)" -le 5 ] && echo few || echo "$(groups gb)")" "few"
check "ga was written at 20000, in many more groups" \
	"$([ "$(groups ga)" -ge 15 ] && echo many || echo "$(groups ga)")" "many"
check "and the catalog recorded both limits" \
	"$(written_limit ga)|$(written_limit gb)" \
	"20000|$(_d="$(q "SHOW pgcolumnar.stripe_row_limit" | tr -d ' ')"; pgc_is_number "$_d" && echo "$_d" || echo "unread [$_d]")"

# The comparison is only meaningful against one plan shape.
check "premise: gb is costed as an index scan at plan-time limit 150000" \
	"$(plan_line gb 150000 | grep -c 'Index Scan')" "1"
check "premise: gb is costed as an index scan at plan-time limit 20000" \
	"$(plan_line gb 20000 | grep -c 'Index Scan')" "1"

# --- 1. the planning session must not reprice an existing table -------------
# The table is not touched between these three readings. Only a session GUC
# moves, and it describes what a WRITE would do, so it cannot legitimately
# change what an existing table costs to read.
c1="$(cost_at gb 150000)"
c2="$(cost_at gb 20000)"
echo "-- gb costed at plan-time limits 150000 / 20000: $c1 / $c2"
# Both readings must be costs: two missing ones agree as "|" == "|".
check_text "the planning session's stripe_row_limit does not reprice a written table" \
	"$(pgc_is_number "$c1" && pgc_is_number "$c2" && echo "$c1|$c2" || echo "unread [$c1|$c2]")" "$c1|$c1"

# --- 2. and the model can still see real geometry ---------------------------
# The converse failure would be a model that ignores the session GUC by ignoring
# geometry altogether. ga really is laid out in many small groups, so its
# per-group decode is genuinely cheaper, and the cost must reflect that.
ca="$(cost_at ga 150000)"
cb="$(cost_at gb 150000)"
echo "-- ga (many small groups) $ca vs gb (few large) $cb, same rows, same plan-time GUC"
# By a margin: a model blind to geometry costs the two within the ANALYZE
# sample's noise (771.10 against 771.06, 771.17 against 771.40, measured), and
# a bare "<" turned that into a coin flip. One percent is ~25x the noise and
# far below the real gaps (2.2x through the other terms alone, 6.8x in all).
check "a table written in smaller groups is costed cheaper than one in larger" \
	"$(pgc_is_number "$ca" && pgc_is_number "$cb" \
	   && awk -v a="$ca" -v b="$cb" 'BEGIN{print (a + 0 < 0.99 * b) ? "cheaper" : "not cheaper: ga " a " gb " b}' \
	   || echo "unread [$ca|$cb]")" "cheaper"

# Cheaper alone does not hold the index-fetch penalty to the written geometry:
# with the penalty's limit pinned at 150000 for every table, ga still costs
# less through the other geometry terms (350.72 against 771.02 on PG17), and
# the check above passes. The penalty's own share is the difference: measured
# 113.98 against 771.36, a 0.15x ratio, where the pinned penalty gives 0.45x.
# A third separates the two with room on both sides.
check_ratio "the index-fetch penalty sees the written geometry: ga costs under a third of gb" \
	"$ca" "$cb" 0.33

# --- 3. a table that was never written still plans ---------------------------
# There is no storage row before the first write, so the reader falls back to the
# session answer. That path has to work rather than error or return zero.
psql_run "DROP TABLE IF EXISTS gempty;
	CREATE TABLE gempty (id int, v int) USING pgcolumnar;
	CREATE INDEX gempty_id ON gempty (id);"
check "a never-written table still produces a plan" \
	"$(q "SET enable_seqscan=off; EXPLAIN SELECT id, v FROM gempty WHERE id <= 100" \
	   | grep -c 'cost=' | awk '{print ($1 >= 1) ? "yes" : "no"}')" "yes"

pgc_summary
