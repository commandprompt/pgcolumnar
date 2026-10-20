#!/usr/bin/env bash
# Column projection (#338): the reader must read and decode only the columns a
# query references.
#
# Two things have to hold, and they pull against each other, so both are checked
# rather than one being inferred from the other:
#
#   1. It is faithful. Skipping a column must not change a single result. The
#      oracle is the same query with pgcolumnar.enable_column_projection off,
#      which restores the old read-everything behavior, plus a heap table
#      holding identical data.
#   2. It actually skips. Correctness alone is satisfied by doing nothing, so
#      the win is asserted directly as a buffer count. Buffers, not timings:
#      exact, reproducible on a shared runner, and they fail loudly if the
#      projection ever silently stops applying.
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

GUC=pgcolumnar.enable_column_projection
NOPAR="SET max_parallel_workers_per_gather=0"

# scalar value of a query at a given setting of the projection GUC
val() {  # val <on|off> <sql>
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -Atq -c "$NOPAR" -c "SET $GUC=$1" -c "$2" 2>&1
}

# Buffers touched by the scan, at a given setting.
bufs() {  # bufs <on|off> <sql>
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -Atq -c "$NOPAR" -c "SET $GUC=$1" \
		-c "EXPLAIN (ANALYZE, BUFFERS) $2" 2>&1 |
		grep -m1 -oE 'Buffers: shared[^)]*' |
		grep -oE '(hit|read)=[0-9]+' | cut -d= -f2 |
		awk '{ n += $1 } END { print n + 0 }'
}

# The core oracle: projection on must equal projection off, byte for byte.
# agree A B -> "same" when both readings are values and equal, else what was wrong.
# val returns psql's error text, so two runs failing the same way compared equal, and
# so did two empty results (#1341). The names are written at each call site now,
# because a name built inside a helper is a template to compare_to_bash.
agree() {
	if [ -z "$1" ] || [ -z "$2" ] || grep -q 'ERROR' <<<"$1$2"; then echo "no reading: [$1] and [$2]"
	elif [ "$1" = "$2" ]; then echo same
	else echo "differ: [$1] and [$2]"; fi
}
ab() { agree "$(val on "$1")" "$(val off "$1")"; }	# ab <sql>: projection on against off
oracle() { agree "$(val on "$(printf "$1" t)")" "$(val on "$(printf "$1" h)")"; }	# oracle <sql-with-%s>: columnar against heap

# ---------------------------------------------------------------------------
# Fixture: wide enough that skipping is measurable, with every shape that
# interacts with an unmaterialised column.
#   - k        filter column, never selected  (qual-only projection)
#   - v,w      by-value aggregate inputs
#   - txt      varlena (decoded via the by-reference path, not fetch_att)
#   - alln     entirely NULL
#   - pad1..8  the bulk that projection should avoid reading. Random, so it is
#              incompressible: an arithmetic sequence encodes down to almost
#              nothing, which would leave the test asserting a saving that the
#              encoding had already made and projection had not.
# ---------------------------------------------------------------------------
# The heap is built from the generator and the columnar table from the heap, never
# the other way round: copied out of t, h held whatever the columnar reader returned,
# and every columnar==heap check compared the reader with itself -- with a row of t
# corrupted before the copy, all twelve agreed (found by jdatcmd reviewing #1581).
# The pad columns are random(), so the generator cannot simply be run twice.
psql_run "DROP TABLE IF EXISTS t; DROP TABLE IF EXISTS h;
          CREATE TABLE h (id int, k int, v float8, w bigint, txt text, alln int,
                          pad1 float8, pad2 float8, pad3 float8, pad4 float8,
                          pad5 float8, pad6 float8, pad7 float8, pad8 float8);
          INSERT INTO h
          SELECT g, g % 1000,
                 CASE WHEN g % 50 = 0 THEN NULL ELSE (g % 13)::float8 * 1.5 END,
                 g::bigint * 7,
                 CASE WHEN g % 31 = 0 THEN NULL ELSE 'row' || g END,
                 NULL,
                 random(), random(), random(), random(),
                 random(), random(), random(), random()
          FROM generate_series(1, 300000) g;
          CREATE TABLE t (LIKE h) USING pgcolumnar;
          SELECT pgcolumnar.set_options('t'::regclass, stripe_row_limit => 20000);
          INSERT INTO t SELECT * FROM h;" >/dev/null

check_num "premise: columnar and heap hold the same rows" \
	"$(val on "SELECT count(*) FROM t")" "$(val on "SELECT count(*) FROM h")"

# ---- 1. it actually skips ---------------------------------------------------
# The comparison is against the same query with projection off, not against
# pg_total_relation_size: a buffer count includes the catalog reads for row
# group, chunk, and zone map metadata, which projection does not remove and
# which would make a "fraction of the relation" threshold measure the wrong
# thing.
B_ON="$(bufs on  "SELECT sum(v) FROM t")"
B_OFF="$(bufs off "SELECT sum(v) FROM t")"
# This premise exists because the ratio below means nothing on a tiny read. It
# now also catches a missing measurement: bufs() sums with awk and yields 0
# rather than the empty string bc left behind when it was not installed (#418).
# The value is in the reading, not the name: a name carrying the measured buffers
# never matched from one run to the next.
check "premise: the unprojected read is large enough to measure" \
	"$([ "${B_OFF:-0}" -gt 1000 ] 2>/dev/null && echo yes || echo "no (off: [$B_OFF])")" yes
# One float8 column out of fourteen. A third is far above the ideal ~1/14 and
# far below the 1.0 a broken projection would give, so it fails on a regression
# without tracking encoding-ratio drift.
check_ratio "one column costs less than a third of reading all of them" \
	"$B_ON" "$B_OFF" 0.333

# A qual-only column must be read (it is filtered on) even though it is never
# emitted -- if it were skipped the filter would silently match nothing.
check_num "qual-only column still produces the right count" \
	"$(val on "SELECT count(*) FROM t WHERE k = 7")" \
	"$(val on "SELECT count(*) FROM h WHERE k = 7")"

# Selecting everything must not get cheaper: there is nothing to skip. The
# OFFSET 0 stops the planner from collapsing the subquery and reaching the
# count(*) metadata path, which reads no columns at all and would make this
# check pass for the wrong reason.
B_ALL="$(bufs on "SELECT count(*) FROM (SELECT t.* FROM t OFFSET 0) s")"
# Inverted, so the same helper carries it: projection-off over SELECT * must not
# exceed 1.25, which is "SELECT * costs at least 80 percent of projection-off".
check_ratio "SELECT * reads as much as projection-off does" \
	"$B_OFF" "$B_ALL" 1.25

# ---- 2. it is faithful ------------------------------------------------------
check "single column agg :: on==off" "$(ab "SELECT sum(v) FROM t")" "same"
check "two columns :: on==off" "$(ab "SELECT sum(v), sum(w) FROM t")" "same"
check "qual column not selected :: on==off" "$(ab "SELECT count(*) FROM t WHERE k < 500")" "same"
check "qual and target disjoint :: on==off" "$(ab "SELECT sum(v) FROM t WHERE k < 500")" "same"
check "varlena projected alone :: on==off" "$(ab "SELECT md5(string_agg(txt, '|' ORDER BY id)) FROM t WHERE id <= 5000")" "same"
check "all-NULL column :: on==off" "$(ab "SELECT count(alln), count(*) FROM t")" "same"
check "count(*) :: on==off" "$(ab "SELECT count(*) FROM t")" "same"
check "select star hash :: on==off" "$(ab "SELECT md5(string_agg(x.*::text, '|' ORDER BY x.id)) FROM (SELECT * FROM t WHERE id <= 3000) x")" "same"
check "whole-row var :: on==off" "$(ab "SELECT md5(string_agg(t::text, '|' ORDER BY id)) FROM t WHERE id <= 3000")" "same"
check "ctid system column :: on==off" "$(ab "SELECT count(DISTINCT ctid) FROM t WHERE id <= 3000")" "same"
check "order by unselected col :: on==off" "$(ab "SELECT string_agg(id::text, ',') FROM (SELECT id FROM t WHERE id <= 200 ORDER BY k, id) s")" "same"
check "min/max mixed types :: on==off" "$(ab "SELECT min(v), max(w), min(txt), max(id) FROM t")" "same"
check "join on projected column :: on==off" "$(ab "SELECT count(*) FROM t a JOIN t b USING (id) WHERE a.id <= 2000")" "same"
check "single column agg :: columnar==heap" "$(oracle "SELECT sum(v) FROM %s")" "same"
check "qual and target disjoint :: columnar==heap" "$(oracle "SELECT sum(v) FROM %s WHERE k < 500")" "same"
check "varlena alone :: columnar==heap" "$(oracle "SELECT md5(string_agg(txt, '|' ORDER BY id)) FROM %s WHERE id <= 5000")" "same"
check "select star hash :: columnar==heap" "$(oracle "SELECT md5(string_agg(x.*::text, '|' ORDER BY x.id)) FROM (SELECT * FROM %s WHERE id <= 3000) x")" "same"
check "all-NULL column :: columnar==heap" "$(oracle "SELECT count(alln), count(*) FROM %s")" "same"
check "min/max mixed types :: columnar==heap" "$(oracle "SELECT min(v), max(w), min(txt), max(id) FROM %s")" "same"
# ---- 3. the ADD COLUMN interaction -----------------------------------------
# The dangerous case. An unmaterialised column and a column that predates an
# ADD COLUMN both leave the validity pointer NULL, but they mean different
# things: the latter must yield the column's default, the former must never be
# read at all. If the two were conflated, selecting the added column alone would
# hand back its default for rows that have a real stored value.
psql_run "ALTER TABLE t ADD COLUMN added int DEFAULT 42;
          ALTER TABLE h ADD COLUMN added int DEFAULT 42;" >/dev/null
check "added column alone :: on==off" "$(ab "SELECT sum(added), count(added) FROM t")" "same"
check "added column alone :: columnar==heap" "$(oracle "SELECT sum(added), count(added) FROM %s")" "same"
check "added column with others :: columnar==heap" "$(oracle "SELECT sum(added), sum(v) FROM %s")" "same"
# Rows written after the ADD COLUMN carry a real chunk for it; rows written
# before do not. Both must be right when the column is projected by itself.
psql_run "INSERT INTO t SELECT g, g % 1000, 1.0, g, 'new'||g, NULL,
                 1,2,3,4,5,6,7,8, 99
          FROM generate_series(200001, 210000) g;
          INSERT INTO h SELECT g, g % 1000, 1.0, g, 'new'||g, NULL,
                 1,2,3,4,5,6,7,8, 99
          FROM generate_series(200001, 210000) g;" >/dev/null
check "added column across the boundary :: columnar==heap" "$(oracle "SELECT sum(added), count(added), min(added), max(added) FROM %s")" "same"
check "added column across the boundary :: on==off" "$(ab "SELECT sum(added), count(added), min(added), max(added) FROM t")" "same"
# ---- 4. deletes and updates ------------------------------------------------
psql_run "DELETE FROM t WHERE id % 97 = 0; DELETE FROM h WHERE id % 97 = 0;" >/dev/null
check "after deletes, projected column :: columnar==heap" "$(oracle "SELECT sum(v), count(*) FROM %s")" "same"
check "after deletes, projected column :: on==off" "$(ab "SELECT sum(v), count(*) FROM t")" "same"
psql_run "UPDATE t SET v = v + 1 WHERE id % 500 = 0;
          UPDATE h SET v = v + 1 WHERE id % 500 = 0;" >/dev/null
check "after updates, projected column :: columnar==heap" "$(oracle "SELECT sum(v), count(*) FROM %s")" "same"
check "after updates, select star hash :: columnar==heap" "$(oracle "SELECT md5(string_agg(x.*::text, '|' ORDER BY x.id)) FROM (SELECT * FROM %s WHERE id <= 3000) x")" "same"
# ---- 5. parallel scan ------------------------------------------------------
# Projection state is per-worker; a worker reading the wrong ranges would show
# up as a wrong total rather than a crash.
par() {  # par <sql>
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -Atq \
		-c "SET max_parallel_workers_per_gather=4" \
		-c "SET parallel_setup_cost=0" -c "SET parallel_tuple_cost=0" \
		-c "SET min_parallel_table_scan_size=0" \
		-c "SET $GUC=on" -c "$1" 2>&1
}
check_text "parallel scan, projected column" \
	"$(par "SELECT count(*), sum(w) FROM t")" \
	"$(val on "SELECT count(*), sum(w) FROM h")"
check_text "parallel scan, qual on unselected column" \
	"$(par "SELECT sum(v) FROM t WHERE k < 500")" \
	"$(val on "SELECT sum(v) FROM h WHERE k < 500")"

# ---- and the COST MODEL has to know it ---------------------------------------
#
# Everything above proves the scan skips the columns it does not need. None of it
# asks whether the PLANNER knows that, and it did not: the columnar path inherits
# the sequential scan's cost, which prices every page of the relation, so reading
# one int column out of fourteen was quoted the same as reading all fourteen.
#
# That is not a rounding error on a columnar engine. Column projection is the
# reason to store data this way at all, and a planner that cannot see it declines
# the scan precisely where it wins biggest. Measured on a 200,000-row table with
# a 2 KB payload: an index-only scan priced 15,521 and ran 373 ms, against a
# columnar scan priced 27,682 that ran 8 ms -- the plan 44x faster was declined
# because it was quoted as reading a relation it never touches.
#
# It stayed hidden because a second error cancelled it. The zone-map discount
# read pg_stats.correlation and credited a correlated key with skipping half the
# relation, on a table whose two chunk groups are both read (measured: "Chunk
# Groups Removed by Filter: 0"). A fictional pruning discount of ~0.5 stood in
# for the real width discount that was missing, and test/planner_choice_quality.sh
# passed on the product of the two. #461 removes the fiction, which is why this
# has to be priced properly first.
#
# The oracle is BUFFERS, measured above, not a re-derivation of the cost formula.
# A cost model checked against its own arithmetic asserts nothing.

# Cost of the columnar scan node itself, not the plan total: the aggregate above
# it costs the same either way and would dilute the ratio.
scan_cost() {	# $1 = sql
	psql_run "SET max_parallel_workers_per_gather=0; EXPLAIN $1;" 2>/dev/null |
		grep -E 'PgColumnar' | grep -oE 'cost=[0-9.]+\.\.[0-9.]+' |
		head -1 | sed 's/.*\.\.//'
}

NARROW_SQL="SELECT count(id) FROM t"
WIDE_SQL="SELECT count(id), count(txt), count(pad1), count(pad2), count(pad3),
                 count(pad4), count(pad5), count(pad6), count(pad7), count(pad8) FROM t"

NARROW_BUF=$(bufs on "$NARROW_SQL")
WIDE_BUF=$(bufs on "$WIDE_SQL")
NARROW_COST=$(scan_cost "$NARROW_SQL")
WIDE_COST=$(scan_cost "$WIDE_SQL")
echo "-- one column of fourteen: buffers $NARROW_BUF vs $WIDE_BUF, cost $NARROW_COST vs $WIDE_COST"

# The physical premise. If the narrow read did not actually touch fewer buffers
# there would be nothing for the cost model to reflect, and the check below would
# be demanding a discount that is not owed.
# Not a premise (#1339): the projection is pgcolumnar's, and with every column read
# whatever it says this went red.
check_num "the narrow projection really does read far fewer buffers" \
	"$([ -n "$NARROW_BUF" ] && [ -n "$WIDE_BUF" ] &&
	   awk "BEGIN{exit !($WIDE_BUF > 0 && $NARROW_BUF < $WIDE_BUF * 0.5)}" && echo 1 || echo 0)" "1"

check_num "premise: both scan costs are measurements" \
	"$([ -n "$NARROW_COST" ] && [ -n "$WIDE_COST" ] &&
	   awk "BEGIN{exit !($NARROW_COST > 0 && $WIDE_COST > 0)}" && echo 1 || echo 0)" "1"

# A margin, not a bare "<". Two undiscounted prices differ by rounding, and a
# bare comparison would report that as the model working.
check_num "the cost model prices a narrow projection below a wide one" \
	"$(awk "BEGIN{print ($NARROW_COST < $WIDE_COST * 0.8) ? 1 : 0}")" "1"

pgc_summary
