#!/usr/bin/env bash
#
# pgColumnar zone min/max via the direct comparison path (issue #155).
#
# Tracking a chunk's min and max costs two comparisons per value per column, and
# routing both through fmgr is most of what they cost. Integer-family columns now
# compare directly, and the maximum is tested first so an ascending load -- what a
# bulk load of a serial or timestamp column produces -- needs one comparison per
# value rather than two.
#
# The risk this carries is not a crash. A zone map drives row-group skipping, so a
# minimum that is too high or a maximum that is too low makes the reader skip a
# group that does hold matching rows and the query silently returns fewer. That is
# the failure this file is built to catch, so the checks are differential against
# a heap mirror over predicates that sit exactly on the chunk bounds.
#
# The float case has its own check and is the reason floats are deliberately NOT
# on the fast path. btree float ordering puts NaN above every other value; a C
# comparison gets that wrong, because every comparison against NaN is false, so
# NaN would read as equal and never become a chunk's maximum. A later change that
# adds float4/float8 to the fast list fails here rather than silently losing rows.
#
# Usage:  test/write_minmax_fastpath.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

ROWS=${PGC_MINMAX_ROWS:-200000}

# small chunk groups so there are many zone maps to get wrong, and so a predicate
# can fall inside one group and outside its neighbours
psql_run "DROP TABLE IF EXISTS mm_c; DROP TABLE IF EXISTS mm_h;
	SET pgcolumnar.stripe_row_limit = 20000;
	SET pgcolumnar.chunk_group_row_limit = 5000;
	CREATE TABLE mm_c (
		i2 smallint, i4 int, i8 bigint,
		d date, ts timestamp, tz timestamptz,
		asc_i int, desc_i int, rand_i int, neg_i int,
		nullable_i int, t text
	) USING pgcolumnar;
	INSERT INTO mm_c SELECT
		(g % 30000)::smallint,
		g,
		g::bigint * 1000000,
		'2000-01-01'::date + (g % 10000),
		'2000-01-01'::timestamp + (g || ' sec')::interval,
		'2000-01-01'::timestamptz + (g || ' sec')::interval,
		g,
		$ROWS - g,
		(g * 7919) % 100000,
		-g,
		CASE WHEN g % 5 = 0 THEN NULL ELSE g END,
		't' || g
	FROM generate_series(1, $ROWS) g;
	CREATE TABLE mm_h (LIKE mm_c);
	INSERT INTO mm_h SELECT * FROM mm_c;" >/dev/null 2>&1

echo "-- $ROWS rows, $(q "SELECT count(*) FROM pgcolumnar.row_group r
	JOIN pgcolumnar.storage s ON s.storage_id = r.storage_id
	WHERE s.relation_oid = 'mm_c'::regclass;") row groups"

# --- 1. every column agrees with heap under range predicates -------------------

# Predicates chosen to land on and around chunk-group boundaries, which is where a
# botched bound shows up: a group is skipped or kept wrongly only at its edges.
# A count is a number or it is not a reading: q swallows a failure, and two empty
# readings compared equal, so with both tables gone this check and the next passed.
diff_count() {  # label, predicate
	local c h
	c="$(q "SELECT count(*) FROM mm_c WHERE $2;")"
	h="$(q "SELECT count(*) FROM mm_h WHERE $2;")"
	[[ "$c" =~ ^[0-9]+$ && "$c" = "$h" ]] || echo "$1(columnar=$c heap=$h)"
}

bad=""
bad="$bad $(diff_count i4_eq       "i4 = 5000")"
bad="$bad $(diff_count i4_bound    "i4 BETWEEN 4999 AND 5001")"
bad="$bad $(diff_count i4_range    "i4 > $((ROWS / 2)) AND i4 < $((ROWS / 2 + 17))")"
bad="$bad $(diff_count i2_eq       "i2 = 12345")"
bad="$bad $(diff_count i2_range    "i2 BETWEEN 100 AND 200")"
bad="$bad $(diff_count i8_range    "i8 > 4999000000 AND i8 < 5001000000")"
bad="$bad $(diff_count date_range  "d BETWEEN '2000-06-01' AND '2000-06-03'")"
bad="$bad $(diff_count ts_range    "ts > '2000-01-01 01:00:00' AND ts < '2000-01-01 01:00:10'")"
bad="$bad $(diff_count tz_range    "tz > '2000-01-01 01:00:00+00' AND tz < '2000-01-01 01:00:10+00'")"
bad="$bad $(diff_count asc_range   "asc_i BETWEEN 19999 AND 20001")"
bad="$bad $(diff_count desc_range  "desc_i BETWEEN 19999 AND 20001")"
bad="$bad $(diff_count rand_range  "rand_i BETWEEN 500 AND 600")"
bad="$bad $(diff_count neg_range   "neg_i BETWEEN -20001 AND -19999")"
bad="$bad $(diff_count null_range  "nullable_i BETWEEN 4999 AND 5001")"
bad="$bad $(diff_count null_isnull "nullable_i IS NULL")"
bad="$bad $(diff_count text_eq     "t = 't5000'")"

check "every column returns the same rows as heap under range predicates" \
	"$(echo $bad | tr -s ' ')" ""

# --- 2. the stored bounds are the true bounds ----------------------------------

# min/max read straight back out, which is what the zone maps hold. An aggregate
# over a column with no deletes is answered from those zone maps, so this reads
# the stored bounds rather than the data.
bad=""
for col in i2 i4 i8 d ts tz asc_i desc_i rand_i neg_i nullable_i t; do
	c="$(q "SELECT min($col)::text || '|' || max($col)::text FROM mm_c;")"
	h="$(q "SELECT min($col)::text || '|' || max($col)::text FROM mm_h;")"
	[ -n "$c" ] && [ "$c" = "$h" ] || bad="$bad $col(columnar=$c heap=$h)"
done
check "min and max match heap on every column" "${bad:-same}" "same"

# --- 3. skipping still happens -------------------------------------------------

# A fast path that quietly widened every bound would pass checks 1 and 2 while
# giving up the skipping the zone maps exist for, so assert groups are still
# ruled out.
# The node, before the counter read out of it.
check "the plan under test is a columnar custom scan" \
	"$(pgc_is_columnar_scan "SELECT count(*) FROM mm_c WHERE asc_i BETWEEN 19999 AND 20001")" "yes"

skipped="$(q "EXPLAIN (ANALYZE, COSTS off, TIMING off, SUMMARY off)
	SELECT count(*) FROM mm_c WHERE asc_i BETWEEN 19999 AND 20001;" \
	| grep -oE 'Columnar Chunk Groups Removed by Filter: [0-9]+' \
	| grep -oE '[0-9]+$' | head -1)"
echo "-- chunk groups skipped for a narrow range: ${skipped:-none reported}"

check "a narrow range still skips row groups" \
	"$( [ -n "${skipped:-}" ] && [ "$skipped" -gt 0 ] && echo yes || echo "no (${skipped:-unreported})")" \
	"yes"

# --- 4. floats keep their btree ordering, NaN included -------------------------

psql_run "DROP TABLE IF EXISTS mm_f; DROP TABLE IF EXISTS mm_fh;
	SET pgcolumnar.stripe_row_limit = 20000;
	SET pgcolumnar.chunk_group_row_limit = 20000;
	CREATE TABLE mm_f (f4 real, f8 double precision) USING pgcolumnar;
	INSERT INTO mm_f SELECT
		CASE WHEN g = 5001 THEN 'NaN'::real
		     WHEN g = 5002 THEN 'Infinity'::real
		     WHEN g = 5003 THEN '-Infinity'::real
		     ELSE g::real END,
		CASE WHEN g = 5001 THEN 'NaN'::float8
		     WHEN g = 5002 THEN 'Infinity'::float8
		     WHEN g = 5003 THEN '-Infinity'::float8
		     ELSE g::float8 END
	FROM generate_series(1, 5003) g;
	CREATE TABLE mm_fh (LIKE mm_f);
	INSERT INTO mm_fh SELECT * FROM mm_f;" >/dev/null 2>&1

# The special values sit at the END of a single chunk group, on purpose. NaN
# arriving first would become that chunk's min and max before any comparison ran,
# and every check below would pass whatever the comparison did -- which is what an
# earlier version of this fixture did, and it let a deliberately broken build
# through.
check "the float fixture is one chunk group" \
	"$(q "SELECT count(*) FROM pgcolumnar.row_group r
		JOIN pgcolumnar.storage s ON s.storage_id = r.storage_id
		WHERE s.relation_oid = 'mm_f'::regclass;")" "1"

# btree orders NaN above everything, so it is the maximum of both columns. A C
# comparison would never make it one, and then a query for it can be skipped.
# check_text, not check: two failed readings are both empty and compared equal.
check_text "max of a float column with NaN matches heap" \
	"$(q "SELECT max(f8)::text FROM mm_f;")" "$(q "SELECT max(f8)::text FROM mm_fh;")"

check_text "max of a real column with NaN matches heap" \
	"$(q "SELECT max(f4)::text FROM mm_f;")" "$(q "SELECT max(f4)::text FROM mm_fh;")"

check_text "a row holding NaN is still found by an equality predicate" \
	"$(q "SELECT count(*) FROM mm_f WHERE f8 = 'NaN';")" \
	"$(q "SELECT count(*) FROM mm_fh WHERE f8 = 'NaN';")"

# One equality per infinity, not an OR: an OR never prunes a chunk group, so with
# either bound wrong it still counted both while each infinity, asked for alone,
# was skipped (found by OffgridwithJD reviewing #1547).
check_text "Infinity is found by an equality predicate" \
	"$(q "SELECT count(*) FROM mm_f WHERE f8 = 'Infinity';")" \
	"$(q "SELECT count(*) FROM mm_fh WHERE f8 = 'Infinity';")"
check_text "-Infinity is found by an equality predicate" \
	"$(q "SELECT count(*) FROM mm_f WHERE f8 = '-Infinity';")" \
	"$(q "SELECT count(*) FROM mm_fh WHERE f8 = '-Infinity';")"

# --- 5. the fast path is restricted to the types that can take it --------------

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/src"

check "float types are not on the direct comparison list" \
	"$(grep -cE 'COLUMNAR_FASTCMP_F(32|64)' "$SRC/columnar_write_state.c")" "0"

pgc_summary
