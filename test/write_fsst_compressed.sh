#!/usr/bin/env bash
#
# pgColumnar FSST selection against the block compressor.
#
# FSST is picked per vector by comparing its code stream to the raw length, but
# the bytes that reach disk are that stream after the configured codec has run.
# Those are different objectives and for some shapes they disagree: FSST turns
# highly repetitive text into high-entropy codes, which are smaller than the
# text and far less compressible than it was. Scored uncompressed FSST wins;
# stored compressed it loses. The guard that gates the attempt -- try FSST only
# when no other encoding shrank the vector -- selects for exactly that case,
# because a vector nothing could encode is usually one zstd handles well.
#
# Measured on main before the fix, 300,000 rows at the default zstd:3:
#
#     'sN'              352 kB written,  611 ms   against 184 kB, 370 ms without
#     md5 repeated 8x  6864 kB written, 4629 ms   against 6528 kB, 804 ms
#
# so 1.9x the space on one shape, and 5.8x the write time to produce a file 5%
# larger on the other. The fix runs both candidates through the codec over a
# bounded run of the chunk and keeps FSST only when it is genuinely smaller.
#
# Three things are asserted.
#
# 1. Values round-trip. Dropping the table changes which encoding every vector
#    of that chunk uses, so this is where the change can go wrong quietly, and
#    it is checked differentially against a heap mirror rather than against
#    expected constants.
#
# 2. The choice itself, read out of the chunk descriptor. This is the mechanism,
#    and it is what makes the file portable: sizes in kB depend on the zstd
#    build, but "which encoding did it pick" does not.
#
# 3. Nothing changes when there is no codec to compete with. With compression
#    off the encoded length IS the stored length, the original per-vector test
#    is already measuring the right thing, and the fix must not disturb it.
#
# Usage:  test/write_fsst_compressed.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

ROWS=${PGC_FSST_ROWS:-300000}

# The shapes that separate the two objectives. "short" is the one FSST enlarged
# most; "email" is the control -- FSST genuinely wins there after compression,
# so a fix that simply disabled FSST would fail on it and the check below is
# what stops this file passing against that.
short_expr="'s' || g"
email_expr="'user' || g || '@' || (g % 997) || '.example.com'"
hient_expr="repeat(md5(g::text), 8)"

# Everything here runs at the stock stripe and row-group sizes, because those
# are the sizes the behaviour was measured at and the decision moves with how
# much data it sees.
build() {  # table, expression, compression
	psql_run "DROP TABLE IF EXISTS $1;
		SET pgcolumnar.compression = $3;
		CREATE TABLE $1 (id int, v text) USING pgcolumnar;
		INSERT INTO $1 SELECT g, $2 FROM generate_series(1, $ROWS) g;" >/dev/null 2>&1
}

# How many vectors of the text column chose FSST.
#
# The descriptor is a 6-byte header -- version, a flags byte (#1130; a reserved
# zero before it), then the vector
# count as uint32 -- followed by that many 13-byte entries, followed by the
# chunk-shared symbol table. So entry i's type byte is at 6 + i*13, and the
# count comes from the header rather than from the length: the trailing table's
# size is not recoverable from the length alone, and reading past the entries
# would score the table's own bytes as encoding types. An earlier version of
# this file assumed a 13-byte stride from offset 0 and reported Gorilla and
# delta-of-delta on a text column, which is how the header was found.
#
# "no vectors" when the column has none: a table whose load failed has no chunks, and
# "no FSST vector" read from it as 0 passed both "not chosen" checks.
fsst_vectors() {  # table -> count, or "no vectors"
	q "SELECT CASE WHEN coalesce(sum(total), 0) > 0 THEN sum(n)::text ELSE 'no vectors' END FROM (
		SELECT get_byte(c.encoding_descriptor, 2)
					+ get_byte(c.encoding_descriptor, 3) * 256
					+ get_byte(c.encoding_descriptor, 4) * 65536
					+ get_byte(c.encoding_descriptor, 5) * 16777216 AS total,
			(SELECT count(*)
				FROM generate_series(0,
					get_byte(c.encoding_descriptor, 2)
					+ get_byte(c.encoding_descriptor, 3) * 256
					+ get_byte(c.encoding_descriptor, 4) * 65536
					+ get_byte(c.encoding_descriptor, 5) * 16777216 - 1) i
				WHERE get_byte(c.encoding_descriptor, 6 + i * 13) = 8) AS n
		FROM pgcolumnar.column_chunk c
		JOIN pgcolumnar.storage s ON s.storage_id = c.storage_id
		WHERE s.relation_oid = '$1'::regclass
		  AND c.column_index = 1) t;" | tail -1
}

# mismatches/rows of two tables joined on id, missing rows on either side counted as
# mismatches. Counting only the mismatches of an inner join read an empty table as
# "0 differ", which passed every round-trip check with nothing loaded.
roundtrip() {  # table, table -> "mismatches/rows"
	q "SELECT count(*) FILTER (WHERE a.id IS NULL OR b.id IS NULL OR a.v IS DISTINCT FROM b.v)
		|| '/' || count(*) FROM $1 a FULL JOIN $2 b USING (id);" | tail -1
}

# --- 1. values round-trip through both outcomes --------------------------------

# nulls, empty strings and values below the FSST minimum are mixed in, because
# the fix changes the encoding of every vector in a chunk at once and those are
# the values whose handling differs between encodings.
#
# EVERY HEAP MIRROR IS BUILT FROM ITS GENERATOR, never copied from the columnar table:
# a decoder that is wrong consistently would write its wrong values into a copy, and
# the round-trip would read 0 mismatches (measured by OffgridwithJD reviewing #1548:
# 57230 of 300000 e-mail values wrong, both suites green).
mixed_expr="CASE g % 11 WHEN 0 THEN NULL WHEN 1 THEN '' WHEN 2 THEN 'x' ELSE 's' || g END"
psql_run "DROP TABLE IF EXISTS fc_c; DROP TABLE IF EXISTS fc_h;
	CREATE TABLE fc_c (id int, v text) USING pgcolumnar;
	INSERT INTO fc_c SELECT g, $mixed_expr FROM generate_series(1, $ROWS) g;
	CREATE TABLE fc_h (id int, v text);
	INSERT INTO fc_h SELECT g, $mixed_expr FROM generate_series(1, $ROWS) g;" >/dev/null 2>&1

check "every value round-trips on a shape where FSST is dropped" \
	"$(roundtrip fc_c fc_h)" "0/$ROWS"

# nulls/empties/rows, and the heap side must hold every row, so two empty tables
# cannot agree on 0/0/0
fc_heap_ne="$(q "SELECT count(*) FILTER (WHERE v IS NULL) || '/' || count(*) FILTER (WHERE v = '')
		|| '/' || count(*) FROM fc_h;" | tail -1)"
case "$fc_heap_ne" in */"$ROWS") ;; *) fc_heap_ne="heap holds [$fc_heap_ne], not $ROWS rows" ;; esac
check "the null and empty values survive" \
	"$(q "SELECT count(*) FILTER (WHERE v IS NULL) || '/' || count(*) FILTER (WHERE v = '')
		|| '/' || count(*) FROM fc_c;" | tail -1)" "$fc_heap_ne"

build fc_e "$email_expr" zstd
psql_run "DROP TABLE IF EXISTS fc_eh; CREATE TABLE fc_eh (id int, v text);
	INSERT INTO fc_eh SELECT g, $email_expr FROM generate_series(1, $ROWS) g;" >/dev/null 2>&1

check "every value round-trips on a shape where FSST is kept" \
	"$(roundtrip fc_e fc_eh)" "0/$ROWS"

# --- 2. the choice goes the right way on each shape ----------------------------

# This is the check that fails against main. Both directions are asserted,
# because a fix that switched FSST off wholesale would pass the first half.
build fc_s "$short_expr" zstd
check "FSST is not chosen for text the codec compresses better alone" \
	"$(fsst_vectors fc_s)" "0"

build fc_hi "$hient_expr" zstd
check "FSST is not chosen where it cost 5.8x the write time for a larger file" \
	"$(fsst_vectors fc_hi)" "0"

# The control. FSST beats zstd-alone on this shape by 23% and must be kept, so
# this is what separates the fix from simply removing FSST.
# chosen N -> yes when N is a count above 0. Not awk: awk compares "no vectors" > 0 as
# strings, which is true, and an empty table would pass as chosen.
chosen() { [[ "$1" =~ ^[0-9]+$ ]] && [ "$1" -gt 0 ] && echo yes || echo "no ($1)"; }
# EVERY vector of the control, not some: a verdict reused wrongly dropped FSST in one
# stripe of two and still left "some" (found by OffgridwithJD reviewing #1548).
vectors() {  # table -> how many vectors the text column has
	q "SELECT coalesce(sum(get_byte(c.encoding_descriptor, 2)
			+ get_byte(c.encoding_descriptor, 3) * 256
			+ get_byte(c.encoding_descriptor, 4) * 65536
			+ get_byte(c.encoding_descriptor, 5) * 16777216), 0)
		FROM pgcolumnar.column_chunk c JOIN pgcolumnar.storage s ON s.storage_id = c.storage_id
		WHERE s.relation_oid = '$1'::regclass AND c.column_index = 1;" | tail -1
}
fc_e_fsst="$(fsst_vectors fc_e)"; fc_e_all="$(vectors fc_e)"
check "FSST is still chosen where it genuinely wins after compression" \
	"$([ "$(chosen "$fc_e_fsst")" = yes ] && [ "$fc_e_fsst" = "$fc_e_all" ] && echo yes \
	   || echo "no ($fc_e_fsst of $fc_e_all vectors)")" "yes"

# --- 3. no codec, no change ----------------------------------------------------

# With compression off the per-vector test already compares the stored length,
# so the decision must be left exactly as it was: FSST wins all three shapes
# uncompressed, including the two it loses under zstd.
for shape in short hient; do
	eval "expr=\$${shape}_expr"
	build "fc_n_$shape" "$expr" none
	check "with compression off FSST is still chosen for $shape" \
		"$(chosen "$(fsst_vectors "fc_n_$shape")")" "yes"
done

psql_run "DROP TABLE IF EXISTS fc_sh; CREATE TABLE fc_sh (id int, v text);
	INSERT INTO fc_sh SELECT g, $short_expr FROM generate_series(1, $ROWS) g;" >/dev/null 2>&1
check "values round-trip with compression off" \
	"$(roundtrip fc_n_short fc_sh)" "0/$ROWS"

pgc_summary
