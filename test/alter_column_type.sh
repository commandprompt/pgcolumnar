#!/usr/bin/env bash
#
# pgColumnar ALTER TABLE ... ALTER COLUMN TYPE (issue #178).
#
# Phase 2 of ATRewriteTable updates pg_attribute before phase 3 scans the old
# relation, so RelationGetDescr() describes the new types while the bytes on disk
# are still the old ones. Core builds the scan's slot from tab->oldDesc for
# exactly that reason. The scan decoded against the relation instead, so every
# conversion that moved a column across a width boundary read the stored bytes as
# the wrong shape:
#
#   int -> bigint      ERROR: corrupt encoded chunk (raw length does not match value count)
#   int -> text        ERROR: corrupt encoded chunk (fixed-width encoding on a non-fixed-width column)
#   bool -> int        ERROR: corrupt encoded chunk (bit width out of range)
#   text -> int        SIGSEGV -- an integer read out of the stream and then
#                      dereferenced as a text pointer, in the USING expression
#
# Varlena-to-varlena conversions were unaffected, which is why this looked like a
# type-specific problem rather than a descriptor problem.
#
# heap is the oracle for the values. Comparing against a heap table converted the
# same way is stronger than comparing to constants written here: it is the
# property that has to hold, whatever the data.
#
# The crash case is worth keeping even though it is now just another conversion:
# it is the one where nothing between the misread and the dereference could
# notice, so it is the one that regresses silently.
#
# Usage:  test/alter_column_type.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

ROWS=${PGC_ALTERTYPE_ROWS:-20000}

# Convert one column in a columnar table and in a heap table of the same shape,
# then compare every row. The two tables are built and altered through separate
# psql_run calls: sharing one would let a failure on the columnar side stop the
# script before the heap side ran, and the comparison would then find nothing
# against nothing and pass.
conv() {  # label, column type, value expression, alter clause
	local c="at_c" h="at_h"

	psql_run "DROP TABLE IF EXISTS $c;
		CREATE TABLE $c (id int, v $2) USING pgcolumnar;
		INSERT INTO $c SELECT g, $3 FROM generate_series(1, $ROWS) g;" >/dev/null 2>&1
	psql_run "DROP TABLE IF EXISTS $h;
		CREATE TABLE $h (id int, v $2);
		INSERT INTO $h SELECT g, $3 FROM generate_series(1, $ROWS) g;" >/dev/null 2>&1

	local err
	err="$(psql_run "ALTER TABLE $c ALTER COLUMN v TYPE $4;" 2>&1 || true)"
	psql_run "ALTER TABLE $h ALTER COLUMN v TYPE $4;" >/dev/null 2>&1

	if grep -qiE "corrupt encoded chunk|server closed|terminated" <<<"$err"; then
		check "$1" "failed: $(echo "$err" | grep -oiE 'corrupt encoded chunk[^\"]*|server closed' | head -1)" "ok"
		return
	fi

	# every row must match heap, and the row count must be right: a conversion
	# that silently dropped rows would otherwise pass a value comparison over
	# whatever survived
	local mismatch count ctype want
	mismatch="$(q "SELECT count(*) FROM $c a JOIN $h b USING (id)
		WHERE a.v IS DISTINCT FROM b.v;" | tail -1)"
	count="$(q "SELECT count(*) FROM $c;" | tail -1)"
	# and the column must have the new type: an ALTER that failed with any other
	# error rolled back, and a widening such as int -> bigint compares equal to
	# heap's converted values unconverted, so nine of these passed with no ALTER
	ctype="$(q "SELECT atttypid::regtype::text FROM pg_attribute
		WHERE attrelid = '$c'::regclass AND attname = 'v';" | tail -1)"
	want="$(q "SELECT '${4%% USING*}'::regtype::text;" | tail -1)"
	check "$1" "${mismatch:-x}/${count:-x}/${ctype:-x}" "0/$ROWS/${want:-the target type}"
}

# --- fixed width to fixed width, the width-boundary cases --------------------

conv "int to bigint"            int      "g"                          "bigint"
conv "bigint to int"            bigint   "g"                          "int"
conv "smallint to int"          smallint "(g % 1000)::smallint"       "int"
conv "int to smallint"          int      "(g % 1000)"                 "smallint"
conv "float4 to float8"         float4   "(g * 1.5)::float4"          "float8"
conv "float8 to float4"         float8   "(g % 512)::float8"          "float4"
conv "date to timestamp"        date     "date '2024-01-01' + g"      "timestamp"
conv "int to numeric"           int      "g"                          "numeric"

# --- across the fixed/varlena boundary, both directions ----------------------

conv "int to text"              int      "g"                          "text"
conv "bool to int"              bool     "(g % 2 = 0)"                "int USING v::int"

conv "numeric to text"          numeric  "g"                          "text"

# --- varlena to varlena, which always worked and must keep working -----------

conv "text to varchar(64)"      text     "'s' || g"                   "varchar(64)"
conv "text to bytea"            text     "'s' || g"                   "bytea USING v::bytea"
conv "text rewritten by USING"  text     "'s' || g"                   "text USING upper(v)"

# --- the shape must survive too ---------------------------------------------

# A conversion is a rewrite, so the row groups are rebuilt. Check the table is
# still readable through an index and still answers an aggregate, not just that
# a sequential comparison matched.
# The ALTER is its own call. psql runs a multi-statement -c in one implicit
# transaction, so putting it with the CREATE means a failed ALTER rolls the table
# away too, and the checks below then fail because nothing exists rather than
# because the rewrite was wrong.
psql_run "DROP TABLE IF EXISTS at_ix;
	CREATE TABLE at_ix (id int, v int) USING pgcolumnar;
	INSERT INTO at_ix SELECT g, g * 2 FROM generate_series(1, $ROWS) g;
	CREATE INDEX at_ix_i ON at_ix (id);" >/dev/null 2>&1
psql_run "ALTER TABLE at_ix ALTER COLUMN v TYPE bigint;" >/dev/null 2>&1

# Assert the conversion actually happened. Every value check below compares
# numbers that are equal whether or not the column changed type, so without this
# they are all satisfied by an ALTER that failed and rolled back.
check "the column really is bigint now" \
	"$(q "SELECT atttypid::regtype::text FROM pg_attribute
		WHERE attrelid = 'at_ix'::regclass AND attname = 'v';" | tail -1)" \
	"bigint"

# via_index SQL -> "<answer> via at_ix_i", or "<answer> via no index". enable_seqscan
# = off only prices a seq scan up: with no index at all the planner still takes one,
# and both checks below read the table's own rows as "found through the index".
via_index() {
	local set="SET enable_seqscan = off; SET pgcolumnar.enable_custom_scan = off;" plan
	plan="$(q "$set EXPLAIN (COSTS OFF) $1")"
	case "$plan" in
	*"Index Scan using at_ix_i"*|*"Index Only Scan using at_ix_i"*|*"Bitmap Index Scan on at_ix_i"*)
		echo "$(q "$set $1" | tail -1) via at_ix_i" ;;
	*) echo "$(q "$set $1" | tail -1) via no index" ;;
	esac
}

check "an index scan still finds a row after the rewrite" \
	"$(via_index "SELECT v FROM at_ix WHERE id = $((ROWS / 2));")" \
	"$((ROWS)) via at_ix_i"

# every row through the index, not one: an index rebuilt with every other row left
# out still found row ROWS/2, which is one it kept
check "an index scan finds every row after the rewrite" \
	"$(via_index "SELECT count(*) FROM at_ix WHERE id BETWEEN 1 AND $ROWS;")" \
	"$ROWS via at_ix_i"

check_num "the aggregate over the converted column is right" \
	"$(q "SELECT sum(v) FROM at_ix;" | tail -1)" \
	"$(q "SELECT sum(g * 2)::bigint FROM generate_series(1, $ROWS) g;" | tail -1)"

check "every row survived the rewrite" \
	"$(q "SELECT count(*) FROM at_ix;" | tail -1)" "$ROWS"

# A column with nulls: the validity bitmap is rebuilt by the rewrite, and an
# off-by-one there moves values onto the wrong rows rather than losing them.
psql_run "DROP TABLE IF EXISTS at_n; DROP TABLE IF EXISTS at_nh;
	CREATE TABLE at_n (id int, v int) USING pgcolumnar;
	INSERT INTO at_n SELECT g, CASE WHEN g % 3 = 0 THEN NULL ELSE g END
		FROM generate_series(1, $ROWS) g;" >/dev/null 2>&1
psql_run "CREATE TABLE at_nh (id int, v int);
	INSERT INTO at_nh SELECT g, CASE WHEN g % 3 = 0 THEN NULL ELSE g END
		FROM generate_series(1, $ROWS) g;" >/dev/null 2>&1
psql_run "ALTER TABLE at_n ALTER COLUMN v TYPE bigint;" >/dev/null 2>&1
psql_run "ALTER TABLE at_nh ALTER COLUMN v TYPE bigint;" >/dev/null 2>&1

check "the nullable column really is bigint now" \
	"$(q "SELECT atttypid::regtype::text FROM pg_attribute
		WHERE attrelid = 'at_n'::regclass AND attname = 'v';" | tail -1)" \
	"bigint"

# the mismatches over the join, and at_n's own rows: rows lost by the rewrite join
# nothing, so with every null row gone the mismatch count alone still read 0
check "nulls stay on the same rows through a conversion" \
	"$(q "SELECT count(*) FROM at_n a JOIN at_nh b USING (id)
		WHERE a.v IS DISTINCT FROM b.v;" | tail -1)/$(q "SELECT count(*) FROM at_n;" | tail -1)" "0/$ROWS"

# --- the crash, deliberately last ---------------------------------------------
#
# The stored bytes are varlena, the new type is fixed width, and against the
# unfixed build the USING expression dereferences an integer read out of the
# value stream as a text pointer. It runs last because it takes the whole
# cluster down with it: every check after it in the file reports empty results
# and fails for a reason that has nothing to do with what it tests, which makes
# a run against a broken build unreadable. Ordering it here keeps the controls
# above meaningful.
conv "text to int (the crash)"  text     "g::text"                    "int USING v::int"

pgc_summary
