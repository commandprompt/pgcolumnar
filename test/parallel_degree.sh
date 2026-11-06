#!/usr/bin/env bash
#
# pgColumnar parallel degree (#451).
#
# How MUCH parallelism the planner grants a columnar scan, as distinct from
# whether parallel execution is correct -- that is parallel.sh's subject.
#
# THE DEFECT
#
# compute_parallel_worker() sizes a parallel scan from pg_class.relpages, on the
# ladder 1 + floor(log3(relpages / min_parallel_table_scan_size)). We hand it our
# COMPRESSED page count, so the same data gets fewer workers the better we
# compress it. Measured on ClickBench: relpages 180,418 against heap's 937,344,
# giving 5 workers where heap got 7, with no cap binding.
#
# The input is wrong for us in a way it is not wrong for heap. A heap page is a
# fixed amount of deform. One of our pages holds compressed bytes that must be
# decompressed before anything can use them, so it is MORE work than a heap page,
# and we report fewer of them. The pathological consequence is that improving our
# compression makes our scans slower by moving us down a rung.
#
# WHAT THIS ASSERTS
#
# Identical data in a heap table and a columnar table must be granted the same
# number of workers. Equality rather than a floor: the same rows doing the same
# work should get the same parallelism, and a bound of ">= heap" would also pass
# a version that over-asked.
#
# NOT a fixed worker count. That would snapshot one machine's core count and mean
# nothing anywhere else.
#
# WHY min_parallel_table_scan_size IS SET AND NOT ZEROED
#
# parallel.sh sets it to 0 to force parallelism regardless of size. Doing that
# here would delete the subject: the ladder reading relpages IS the defect. It is
# set small so a modest fixture still lands on the ladder rather than under it.
#
# Usage:  test/parallel_degree.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail

. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

MAXW=8

setg() { q "ALTER DATABASE $PGC_DB SET $1 = $2;" >/dev/null; }

# The fixture has to compress REALISTICALLY: enough that heap and columnar land on
# different rungs, not so much that columnar drops off the ladder entirely.
#
# The first version used 'category_' || (g%25) and (g%997), which compressed 101x
# to 29 pages -- below min_parallel_table_scan_size, so the columnar arm got no
# parallel plan at all and the premise below caught it. A fixture that cannot be
# parallel cannot demonstrate being under-parallelised.
#
# So: one low-cardinality column that compresses, and unique text and bigint that
# do not, giving a few-fold ratio rather than two orders of magnitude.
make_pair "id int, k int, v bigint, t text"
load_pair "SELECT g, g%50, g::bigint * 7919, 'row_' || g || '_' || md5(g::text) FROM generate_series(1,400000) g"

setg max_parallel_workers_per_gather "$MAXW"
setg parallel_setup_cost 0
setg parallel_tuple_cost 0
setg min_parallel_table_scan_size "'1MB'"

q "ANALYZE t_heap; ANALYZE t_col;" >/dev/null

pages() { q "SELECT relpages FROM pg_class WHERE relname = '$1'"; }
# The count EXPLAIN reports, which is what the planner decided rather than what
# the executor managed to start.
workers() {
	q "EXPLAIN (COSTS OFF) SELECT count(*) FROM $1 WHERE k = 5" |
		grep -oE 'Workers Planned: [0-9]+' | head -1 | grep -oE '[0-9]+'
}

PH="$(pages t_heap)"; PC="$(pages t_col)"
WH="$(workers t_heap)"; WC="$(workers t_col)"

echo "      heap: relpages=$PH workers=${WH:-none}   columnar: relpages=$PC workers=${WC:-none}"

# ---- premises, or this passes while proving nothing -------------------------
#
# Every reading below is required to be a number before it is compared. With the
# server gone after ANALYZE, every q came back empty and three of these six passed:
# the row counts ("" = ""), the cap ("" read as 0, under any cap) and the degree
# ("none" = "none").

# Not a premise (#1339, dated 2026-10-21): the columnar arm holding the rows is
# pgcolumnar's own work, and an insert that drops one row reddens it.
check_num "both tables hold the same rows" \
	"$(q 'SELECT count(*) FROM t_heap')" "$(q 'SELECT count(*) FROM t_col')"

# No size difference, no penalty to detect.
check "premise: the columnar table really is smaller on disk" \
	"$([ "${PC:-0}" -gt 0 ] && [ "${PH:-0}" -gt "${PC:-0}" ] && echo yes || echo "no (heap=$PH col=$PC)")" "yes"

# A serial plan has no workers to compare, so a missing count is not a zero.
check "premise: the heap plan is parallel" \
	"$([ -n "$WH" ] && echo yes || echo "no (no Workers Planned line)")" "yes"
# Not a premise (#1339, dated 2026-10-21): the columnar partial path is pgcolumnar's
# own, and skipping it reddens this.
check "the columnar plan is parallel" \
	"$([ -n "$WC" ] && echo yes || echo "no (no Workers Planned line)")" "yes"

# The one that would silently neuter this. If max_parallel_workers_per_gather
# clamps both arms they report the same number and the suite goes green on a box
# where the defect is fully present. A missing count is "no reading", not 0.
# Not a premise (#1339, dated 2026-10-21): a columnar degree that over-asks into
# the cap reddens it.
check "the worker cap is not what decides either arm" \
	"$(if ! pgc_is_number "$WH" || ! pgc_is_number "$WC"; then echo "no reading (heap=[$WH] col=[$WC])"
	   elif [ "$WH" -lt "$MAXW" ] && [ "$WC" -lt "$MAXW" ]; then echo yes
	   else echo "no (cap=$MAXW heap=$WH col=$WC)"; fi)" "yes"

# ---- the assertion ----------------------------------------------------------

# check_num: two plans with no Workers Planned line both read "none", and agreed.
check_num "identical data is granted the same parallel degree" "$WC" "$WH"

pgc_summary
