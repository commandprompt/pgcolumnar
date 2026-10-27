#!/usr/bin/env bash
#
# pgColumnar: approx_count_distinct answered from the stored sketches (#1309 phase 3).
#
# An ungrouped, unfiltered approx_count_distinct over a column named in
# sketch_columns is answered from metadata: each clean row group's stored sketch is
# folded by a register-wise max, and only a group the sketches cannot describe is
# read and hashed -- a group with deletes (its sketch counts deleted rows), or a
# group with no sketch row (written before the option was set). A register union is
# exactly the registers of the union, so the fold must equal the plain aggregate's
# answer EXACTLY, not within an error: every answer here is compared with the same
# query under pgcolumnar.enable_custom_scan = off, which is core's Agg over the plain
# transition function.
#
# EXPLAIN ANALYZE reports how many groups were folded from sketches and how many were
# scanned, so an arm can tell a fold from a scan that happens to agree.
#
# Usage:  test/native_sketch_fold.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

plain() {	# plain SQL -> the answer with the custom scan off (core's Agg)
	q "SET pgcolumnar.enable_custom_scan = off; $1" | tail -1
}
fold() {	# fold SQL -> the answer as planned
	q "$1" | tail -1
}
same() {	# same A B -> "same" when both are the same number
	case "$1" in ''|*[!0-9]*) echo "not a number: [$1] vs [$2]"; return ;; esac
	[ "$1" = "$2" ] && echo same || echo "[$1] vs [$2]"
}
groups_line() {	# groups_line SQL -> "folded N scanned M" from EXPLAIN ANALYZE, or "no fold"
	local out f s
	out="$(q "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) $1")"
	f="$(sed -n 's/^ *Columnar Sketch Groups Folded: //p' <<<"$out")"
	s="$(sed -n 's/^ *Columnar Sketch Groups Scanned: //p' <<<"$out")"
	if [ -z "$f$s" ]; then echo "no fold"; else echo "folded ${f:-?} scanned ${s:-?}"; fi
}

# Ten row groups of 1000 rows, about 3000 distinct values in a and t, and NULLs. The
# first 50 rows hold values no other row has, so deleting them must lower the count:
# with only repeated values deleted, a fold that ignored the deletes agreed.
psql_run "CREATE TABLE f (a bigint, t text, b int) USING pgcolumnar;
	SELECT pgcolumnar.set_options('f', stripe_row_limit => 1000, sketch_columns => ARRAY['a', 't']);" >/dev/null 2>&1
psql_run "INSERT INTO f SELECT CASE WHEN g <= 50 THEN 100000 + g WHEN g % 97 = 0 THEN NULL ELSE g % 3000 END,
	CASE WHEN g % 89 = 0 THEN NULL ELSE 'k' || (g % 2500) END, g FROM generate_series(1, 10000) g;" >/dev/null 2>&1

A="SELECT pgcolumnar.approx_count_distinct(a) FROM f"
T="SELECT pgcolumnar.approx_count_distinct(t) FROM f"

# --- 1. a clean table -----------------------------------------------------------

check "premise: ten row groups, each with a sketch for a and for t" \
	"$(q "SELECT count(*) || '/' || (SELECT count(*) FROM pgcolumnar.sketch
		WHERE storage_id = pgcolumnar.get_storage_id('f'))
		FROM pgcolumnar.row_group WHERE storage_id = pgcolumnar.get_storage_id('f');")" "10/20"
check "on a clean table every row group is folded from its sketch" "$(groups_line "$A")" "folded 10 scanned 0"
check "and the fold answers what the plain aggregate answers" "$(same "$(fold "$A")" "$(plain "$A")")" "same"
check "a text column folds to the plain answer too" "$(same "$(fold "$T")" "$(plain "$T")")" "same"
check "count(*) beside it folds and counts every row" \
	"$(fold "SELECT count(*) || '|' || pgcolumnar.approx_count_distinct(a) FROM f")" \
	"10000|$(plain "$A")"

# --- 2. a group the sketches cannot describe is scanned --------------------------

psql_run "DELETE FROM f WHERE b BETWEEN 1 AND 50;" >/dev/null 2>&1
check "a row group with deletes is scanned, the others folded" "$(groups_line "$A")" "folded 9 scanned 1"
check "and the answer is the plain aggregate's over the live rows" "$(same "$(fold "$A")" "$(plain "$A")")" "same"

psql_run "CREATE TABLE fb (a bigint) USING pgcolumnar;
	SELECT pgcolumnar.set_options('fb', stripe_row_limit => 1000);" >/dev/null 2>&1
psql_run "INSERT INTO fb SELECT g % 700 FROM generate_series(1, 2000) g;" >/dev/null 2>&1
psql_run "SELECT pgcolumnar.set_options('fb', sketch_columns => ARRAY['a']);" >/dev/null 2>&1
psql_run "INSERT INTO fb SELECT 500 + g % 700 FROM generate_series(1, 2000) g;" >/dev/null 2>&1
B="SELECT pgcolumnar.approx_count_distinct(a) FROM fb"
check "a row group with no sketch row is scanned, not counted as empty" "$(groups_line "$B")" "folded 2 scanned 2"
check "and the answer is the plain aggregate's" "$(same "$(fold "$B")" "$(plain "$B")")" "same"

# --- 3. what the fold declines ----------------------------------------------------

W="SELECT pgcolumnar.approx_count_distinct(a) FROM f WHERE b > 5000"
check "a WHERE declines the fold" "$(groups_line "$W")" "no fold"
check "and still answers what the plain aggregate answers" "$(same "$(fold "$W")" "$(plain "$W")")" "same"
G="SELECT b % 3, pgcolumnar.approx_count_distinct(a) FROM f GROUP BY 1 ORDER BY 1"
check "a GROUP BY declines the fold" "$(groups_line "$G")" "no fold"
check "and answers what the plain aggregate answers" \
	"$(q "$G" | md5sum | cut -c1-32)" "$(q "SET pgcolumnar.enable_custom_scan = off; $G" | grep -v "^SET$" | md5sum | cut -c1-32)"
M="SELECT pgcolumnar.approx_count_distinct(a), sum(b::numeric) FROM f"
check "an aggregate the metadata cannot answer beside it declines the fold" "$(groups_line "$M")" "no fold"
C="SELECT pgcolumnar.approx_count_distinct(t COLLATE \"C\") FROM f"
check "a COLLATE override declines the fold" "$(groups_line "$C")" "no fold"
check "and the override answers what the plain aggregate answers" "$(same "$(fold "$C")" "$(plain "$C")")" "same"

pgc_summary
