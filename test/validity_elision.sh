#!/usr/bin/env bash
#
# A column with no nulls must not store a validity bitmap (#1130).
#
# flush_one_column builds the page as [validity][finalData]
# (columnar_write_state.c:1079). The bitmap is one bit per row, allocated
# unconditionally (:1107) and appended raw (:1156) BEFORE the block codec runs
# (:1479), which therefore never sees it. A NOT NULL column, or one that simply
# holds no nulls, still writes ceil(rows/8) bytes of 0xFF.
#
# Measured on ClickBench hits_0.parquet -- 1,000,000 rows, 105 columns, no nulls
# in any of them -- the bitmap was 16.03% of the stored table, and 16.80% after
# #1132 shrank the values around it. On a column that encodes well it dominates:
# on a sorted bigint it was 99.5% of the page.
#
# THE MEASUREMENT IS EXACT, not a ratio, which is why this suite can assert a
# number rather than a threshold. With no block codec the page is exactly
# [validity][encoded], so
#
#     page_length - sum(encLen over the chunk's vectors)
#
# IS the validity size, with nothing else in it. Verified on eight chunks across
# four corpora before this change: residual 0 on every one. So the arm reads 0
# for an elided bitmap and ceil(rows/8) for a present one, and a wrong answer
# cannot hide inside a tolerance.
#
# It asserts:
#   1. the two fixtures are what they claim -- one holds no nulls, the other
#      holds some -- because the arms below are about that difference;
#   2. a column with no nulls stores no bitmap at all;
#   3. a column WITH nulls still stores one. This is the silent direction:
#      deleting the bitmap unconditionally satisfies 2 and loses every null;
#   4. both columns read back exactly what the heap holds, NULLs included;
#   5. and they read back the same through the FETCH path, which is a second
#      reader with its own copy of the layout. The elided chunk is the one most
#      likely to land there: the coalesced read skips a chunk smaller than the
#      group's bitmap, and eliding the bitmap is what takes it below that.
#
# Usage:  test/validity_elision.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

ROWS="${PGC_VALIDITY_ROWS:-100000}"

# page_length minus the descriptor's own accounting of the encoded bytes.
#
# Same descriptor decode as fsst_margin.sh and encode_post_codec.sh: a 6-byte
# header -- version, a flags byte, then the vector count as uint32 -- followed by
# that many 13-byte entries of [type][valueCount][rawLen][encLen]. The count
# bounds the scan, so the trailing shared-table region is not read as entries.
# THE COLUMN IS AN ARGUMENT, because the property is per CHUNK and the sharpest
# statement of that is two columns of ONE row group answering differently.
validity_residual() {	# table, column -> page_length - sum(encLen) over its chunks
	q "SELECT coalesce(sum(c.page_length - enc), 0) FROM (
		SELECT c.page_length,
		       (SELECT coalesce(sum(
				  get_byte(c.encoding_descriptor, 6 + i * 13 + 9)
				+ get_byte(c.encoding_descriptor, 6 + i * 13 + 10) * 256
				+ get_byte(c.encoding_descriptor, 6 + i * 13 + 11) * 65536
				+ get_byte(c.encoding_descriptor, 6 + i * 13 + 12) * 16777216), 0)
		         FROM generate_series(0,
				  get_byte(c.encoding_descriptor, 2)
				+ get_byte(c.encoding_descriptor, 3) * 256
				+ get_byte(c.encoding_descriptor, 4) * 65536
				+ get_byte(c.encoding_descriptor, 5) * 16777216 - 1) i) AS enc
		  FROM pgcolumnar.column_chunk c
		  JOIN pgcolumnar.storage s ON s.storage_id = c.storage_id
		 WHERE s.relation_oid = '$1'::regclass
		   AND c.column_index = $2) c;" | tail -1
}

# HOW MANY CHUNKS THE LINE ABOVE ACTUALLY SUMMED. `coalesce(sum(...), 0)` returns
# 0 over an empty set, which is the same 0 the headline arm wants, so a residual
# of 0 is evidence only beside this. A misspelt table, a column index off the end
# or a fixture that never flushed all read as "no bitmap here".
chunks_measured() {	# table, column -> row count of the chunks behind the residual
	q "SELECT count(*) FROM pgcolumnar.column_chunk c
		JOIN pgcolumnar.storage s ON s.storage_id = c.storage_id
	       WHERE s.relation_oid = '$1'::regclass
	         AND c.column_index = $2;" | tail -1
}

row_groups() {	# table -> how many row groups it holds
	q "SELECT count(*) FROM pgcolumnar.row_group r
		JOIN pgcolumnar.storage s ON s.storage_id = r.storage_id
	       WHERE s.relation_oid = '$1'::regclass;" | tail -1
}

# NO CODEC, deliberately. With one, page_length is the COMPRESSED encoded region
# and the subtraction above stops being the bitmap size -- the arm would then be
# reading a compression ratio and calling it a bitmap.
#
# ALTER DATABASE, NOT SET. Each psql_run is its own session, so a plain SET is
# gone by the next statement and the INSERTs below would run under the default
# zstd. That is not a hypothetical: the first version of this suite used SET and
# measured residuals of 12382 and -10091 where the bitmap is 12500. The negative
# one is what gave it away -- a compressed page can be smaller than the bytes the
# descriptor says it encoded, and no bitmap size can be below zero.
psql_run "ALTER DATABASE $PGC_DB SET pgcolumnar.compression = 'none';"

# TWO COLUMNS, and the measured one is the SECOND. The key column k exists so the
# fetch arms at the end reach the table: with a single column, an index on it
# answers the query out of the index and the planner takes an Index Only Scan,
# which never calls the table AM's fetch at all. The first version of this suite
# did exactly that -- its fetch arms passed against a build whose fetch path was
# provably broken, and the premise arm is what caught them.
psql_run "CREATE TABLE ve_full (k bigint, v bigint) USING pgcolumnar;"
psql_run "INSERT INTO ve_full SELECT g, g FROM generate_series(1, $ROWS) g;"
# THE HEAP MIRRORS ARE WRITTEN FROM THE GENERATOR, NOT COPIED FROM THE TABLE (#1339).
# A CREATE TABLE ... AS SELECT FROM ve_full reads through the reader under test, so
# a read that turned a present value into NULL was copied into the oracle too and
# every comparison with it stayed equal: measured, with row 5 of every group read
# as NULL, the EXCEPT ALL arms stayed green.
psql_run "CREATE TABLE ve_full_h (k bigint, v bigint);"
psql_run "INSERT INTO ve_full_h SELECT g, g FROM generate_series(1, $ROWS) g;"

# Every tenth row NULL: enough that the bitmap is genuinely needed, and not so
# many that the column stops holding values to compare.
psql_run "CREATE TABLE ve_nulls (k bigint, v bigint) USING pgcolumnar;"
psql_run "INSERT INTO ve_nulls
	SELECT g, CASE WHEN g % 10 = 0 THEN NULL ELSE g END
	FROM generate_series(1, $ROWS) g;"
psql_run "CREATE TABLE ve_nulls_h (k bigint, v bigint);"
psql_run "INSERT INTO ve_nulls_h
	SELECT g, CASE WHEN g % 10 = 0 THEN NULL ELSE g END
	FROM generate_series(1, $ROWS) g;"

full_nulls="$(q "SELECT count(*) FROM ve_full WHERE v IS NULL;" | tail -1)"
some_nulls="$(q "SELECT count(*) FROM ve_nulls WHERE v IS NULL;" | tail -1)"
full_res="$(validity_residual ve_full 1)"
nulls_res="$(validity_residual ve_nulls 1)"
# ONE VARIABLE, USED TWICE. The premise below and the arm it guards must speak
# about the SAME column, and repeating the literal in two calls does not make
# them: @OffgridwithJD measured the third cell -- residual moved to a column that
# does not exist, premise left pointing at column 0 -- and the suite was green
# again over nothing. The twin cannot express that cell at all, because one call
# there returns both the residual and its count; this is the shell saying the
# same thing.
VE_KEY_COL=0
nulls_key_res="$(validity_residual ve_nulls $VE_KEY_COL)"
nulls_key_chunks="$(chunks_measured ve_nulls $VE_KEY_COL)"
chunks="$(chunks_measured ve_full 1)"
groups="$(row_groups ve_full)$(row_groups ve_nulls)"
codec="$(q "SHOW pgcolumnar.compression;" | tail -1)"
want_bitmap=$(( (ROWS + 7) / 8 ))

echo "-- ve_full: nulls=$full_nulls residual=$full_res   ve_nulls: nulls=$some_nulls residual=$nulls_res key_residual=$nulls_key_res   ceil(rows/8)=$want_bitmap codec=$codec groups=$groups"

# THE PREMISE THE EXACT NUMBER RESTS ON. With a block codec, page_length is the
# COMPRESSED encoded region and the subtraction stops being the bitmap size: the
# arms would read a compression ratio and call it a bitmap. ALTER DATABASE above
# is a statement that has to have taken effect, so it is read back rather than
# assumed -- a misspelt GUC name raises, but a session that did not pick the
# setting up would not.
check "premise: the block codec is off, so the residual is the bitmap and nothing else" \
	"$codec" "none"

# AND THAT THE INSTRUMENT READ SOMETHING. `coalesce(sum(...), 0)` answers 0 over
# an empty set, which is exactly what the headline arm wants to see, so a
# residual of 0 means "no bitmap" only once this says the sum had chunks in it.
check "premise: the residual was summed over chunks that exist" \
	"$(awk -v n="$chunks" 'BEGIN { print (n + 0 > 0) ? "measured" : "measured-nothing" }')" \
	"measured"

# AND THAT ceil(ROWS / 8) IS THE WHOLE EXPECTED BITMAP. It is the size of ONE
# group's bitmap; a fixture split across two groups stores two of them, summing
# to the same total only by accident of rounding. One group each, asserted.
check "premise: each fixture is a single row group, which is what the expected size assumes" \
	"$groups" "11"

# AN ARM, NOT A PREMISE (#1339, #1236). It counts the nulls THROUGH THE READER, so a
# read that turns a present value into NULL reddens it: measured, with row 5 of every
# group read as NULL, it read 1. A check a repository change can redden is an arm.
check "the full column reads back no nulls at all" "$full_nulls" "0"

check "the second fixture holds nulls, so its bitmap is load-bearing" \
	"$(awk -v n="$some_nulls" 'BEGIN { print (n + 0 > 0) ? "has-nulls" : "none" }')" \
	"has-nulls"

# THE ARM. Exact, not a threshold: with no codec the page is [validity][encoded]
# and the residual IS the bitmap.
check "a column with no nulls stores no validity bitmap" "$full_res" "0"

# THE SILENT DIRECTION. Dropping the bitmap unconditionally satisfies the arm
# above and loses every null in the table, so this ships beside it.
check "while a column with nulls still stores one, sized one bit per row" \
	"$nulls_res" "$want_bitmap"

# THE PROPERTY IS PER CHUNK, AND THIS IS THE ONLY ARM THAT SAYS SO. Both arms
# above are satisfied by a writer that decided elision once per ROW GROUP: the
# first fixture's group holds no null anywhere, the second's holds some, and the
# two answers would be the same. ve_nulls has a null-free key column IN THE SAME
# ROW GROUP as its null-bearing one, so only a per-chunk decision can elide one
# and keep the other.
# ITS OWN PREMISE, BESIDE IT RATHER THAN WITH THE OTHERS, so the two move
# together. The arm below expects 0 and 0 is also what the residual returns when
# it summed nothing: a column index off the end of the table answers exactly the
# same as a correctly elided bitmap. Measured by @OffgridwithJD against the first
# version of this suite, which had the premise only on the OTHER residual --
# pointing this one at column_index 99 left all 24 checks green in both
# harnesses. The residual that protects itself is the one for a column that
# SHOULD store a bitmap, because a bug there reads as a large number; this one
# needs saying.
check "premise: the per-chunk arm's residual was summed over chunks that exist" \
	"$(awk -v n="$nulls_key_chunks" 'BEGIN { print (n + 0 > 0) ? "measured" : "measured-nothing" }')" \
	"measured"

check "a null-free column elides its bitmap beside a null-bearing one in the same row group" \
	"$nulls_key_res" "0"

# THE INVARIANT the two arms above exist to protect. A layout change that loses
# or shifts a null is a data-loss bug, not a size regression.
#
# Named for the COLUMN SHAPE rather than the table, so the property is the name
# and the pytest twin can assert it over its own fixture (CONTEXT.md's
# independence rule) instead of inheriting this suite's table names.
for t in full nulls; do
	tbl="ve_$t"
	check "the $t column reads back exactly what the heap holds" \
		"$(q "SELECT count(*) FROM (
			SELECT k, v FROM $tbl EXCEPT ALL SELECT k, v FROM ${tbl}_h) d;" | tail -1)" \
		"0"
	check "the $t column holds no row the heap does not" \
		"$(q "SELECT count(*) FROM (
			SELECT k, v FROM ${tbl}_h EXCEPT ALL SELECT k, v FROM $tbl) d;" | tail -1)" \
		"0"
	check "the $t column preserves its null count" \
		"$(q "SELECT count(*) FROM $tbl WHERE v IS NULL;" | tail -1)" \
		"$(q "SELECT count(*) FROM ${tbl}_h WHERE v IS NULL;" | tail -1)"
done

# THE FETCH PATH IS A SECOND READER, and it is not the one the arms above
# exercise. A sequential scan goes through pgcolumnar_native_load_group; an
# index scan reaches a row through pgcolumnar_fetch_get_row, which reads the
# chunk's bytes itself. Both have to know that the bitmap may be absent, and
# nothing above can tell whether the second one does.
#
# ELISION MAKES THIS PATH MORE LIKELY, not less: the coalesced read skips any
# chunk whose page_length is below the group's bitmap size, and eliding the
# bitmap is exactly what takes a well-encoded chunk below it. So the column this
# change helps most is the one that lands on the per-column path.
#
# THIS IS NOT HYPOTHETICAL. Measured on this tree with the scan path fixed and
# the fetch path not: 33 suites red on the PG17 matrix, and native_index's point
# lookup returned NO ROW for a row that is there -- the chunk's first encoded
# bytes were read as a validity bitmap, so the rows they covered read as absent.
psql_run "CREATE INDEX ve_full_k ON ve_full (k); CREATE INDEX ve_nulls_k ON ve_nulls (k);"

# enable_custom_scan = off IS THE ONE THAT MATTERS. `enable_seqscan` does not
# govern the columnar custom scan, so without this the planner answers from
# `Custom Scan (PgColumnarScan)` -- a scan, not a fetch -- and every arm below
# becomes a second copy of the scan arms above. Measured: with the three plain
# SETs alone the plan was `Custom Scan (PgColumnarScan) on ve_full`.
FORCE="SET pgcolumnar.enable_custom_scan = off;
	SET enable_seqscan = off; SET enable_bitmapscan = off;
	SET max_parallel_workers_per_gather = 0;"

# THE PREMISE THAT MAKES THE THREE ARMS BELOW MEAN ANYTHING, and it is not
# decoration: an INDEX ONLY scan answers out of the index and never calls the
# table AM's fetch, so arms over a single-column table pass against a build
# whose fetch path is broken. "Index Only Scan" does not match this anchored
# pattern, which is the point of anchoring it.
check "the arms below reach the row through an index scan" \
	"$(q "$FORCE EXPLAIN (COSTS OFF) SELECT v FROM ve_full WHERE k = 12345;" |
		grep -cE '^[[:space:]]*Index Scan using')" \
	"1"

# AND THE SAME FOR THE SHAPE THE NEXT TWO ARMS ACTUALLY RUN. The premise above
# plans a point query; the arms below plan a join, which the planner is free to
# serve differently. Asserting one and running the other is how an arm ends up
# licensed by a plan nobody produced.
check "premise: the join arms below reach the columnar side by index too" \
	"$(q "$FORCE EXPLAIN (COSTS OFF) SELECT count(*) FROM ve_full c
		JOIN ve_full_h h ON h.k = c.k WHERE c.v IS DISTINCT FROM h.v;" |
		grep -cE '^[[:space:]]*(->[[:space:]]*)?Index Scan using ve_full_k')" \
	"1"

for t in full nulls; do
	tbl="ve_$t"
	check "the $t column fetched by index matches the heap, row for row" \
		"$(q "$FORCE SELECT count(*) FROM $tbl c JOIN ${tbl}_h h ON h.k = c.k
			WHERE c.v IS DISTINCT FROM h.v;" | tail -1)" \
		"0"
done

# ONE ROW AT A TIME, at positions where a bitmap read out of the wrong bytes
# lands on a neighbour or reports a present row as NULL. The join above is
# satisfied by a fetch that returns the row it was asked for; these are not.
bad=""
for pos in 1 2 3 8 9 4097 $((ROWS / 2)) $((ROWS - 1)) "$ROWS"; do
	got="$(q "$FORCE SELECT coalesce(v::text, 'N') FROM ve_full WHERE k = $pos;" | tail -1)"
	[ "$got" = "$pos" ] || bad="$bad k=$pos(got=${got:-<none>})"
done
check "single-row fetches through the elided path return the row asked for" \
	"${bad:-same}" "same"

# THE NULL-BEARING COLUMN NEEDS ITS OWN ONE-ROW PROBE, and a null row in it.
# ve_nulls keeps its bitmap, so these rows exercise the OTHER branch of the same
# per-chunk decision -- and the row that is genuinely NULL is the one a fetch
# that lost the bitmap would answer wrongly in the safe-looking direction.
bad_n=""
for pos in 9 10 11 4096 4097 $((ROWS / 2)) $((ROWS - 1)) "$ROWS"; do
	want="N"
	[ $(( pos % 10 )) -eq 0 ] || want="$pos"
	got="$(q "$FORCE SELECT coalesce(v::text, 'N') FROM ve_nulls WHERE k = $pos;" | tail -1)"
	[ "$got" = "$want" ] || bad_n="$bad_n k=$pos(got=${got:-<none>} want=$want)"
done
check "single-row fetches of the null-bearing column return its nulls as nulls" \
	"${bad_n:-same}" "same"

# ---------------------------------------------------------------------------
# THE TWO GUARDS THE ELISION ADDED, EACH WITH AN ARM OF ITS OWN.
#
# Both live in pgcolumnar_native_load_group and neither is reachable from a
# table this suite writes correctly, so they are exercised by CORRUPTING the
# catalog -- which is what a bit flip or a crafted file does, and what
# corruption.sh does for the fields that predate this change.
#
# ASSERT THE SQLSTATE, NOT ONLY THE TEXT. `42501`, `22023`, a login FATAL and a
# missing function all satisfy a grep for "ERROR"; XX001 is ERRCODE_DATA_CORRUPTED
# and comes only from a guard that ran.
#
# AND WHICH GUARD (#1339). XX001 comes from sixteen guards in the reader: with the
# no-nulls guard disabled, both arms below still saw XX001 from a later one and
# stayed green. So each reads the message too, and a red names what fired.
# SQL -> `SQLSTATE|the no-nulls guard`, `SQLSTATE|another guard: <message>` or NOERR.
guard_of() {
	local out state msg
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" \
		-U postgres -d "$PGC_DB" -qtA 2>&1 <<SQLEOF
\\set VERBOSITY verbose
$1;
SQLEOF
)"
	state="$(printf '%s\n' "$out" | sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\): .*/\1/p' | head -1)"
	msg="$(printf '%s\n' "$out" | sed -n 's/^ERROR:  [0-9A-Z]\{5\}: //p' | head -1)"
	if [ -z "$state" ]; then
		echo NOERR
	elif grep -qF "claims no nulls but does not account for every row" <<<"$msg"; then
		echo "$state|the no-nulls guard"
	else
		echo "$state|another guard: $msg"
	fi
}

# GUARD 1: a chunk that CLAIMS it stored no bitmap while its own descriptor
# accounts for fewer values than the group has rows. Without the guard the
# reader synthesizes an all-ones bitmap, believes in rows the chunk does not
# hold, and reads past the value stream. ve_nulls' v column is the fixture that
# makes the claim false: it holds 90% of the rows, so setting the flag on it is
# exactly the lie the guard exists for.
psql_run "UPDATE pgcolumnar.column_chunk c
	SET encoding_descriptor = set_byte(c.encoding_descriptor, 1, 1)
	FROM pgcolumnar.storage s
	WHERE s.storage_id = c.storage_id
	  AND s.relation_oid = 've_nulls'::regclass
	  AND c.column_index = 1;"

check "a chunk claiming no bitmap while it holds fewer values than rows is refused" \
	"$(guard_of "SELECT sum(v) FROM ve_nulls")" \
	"XX001|the no-nulls guard"

check "and the backend survives that refusal" "$(q "SELECT 1;" | tail -1)" "1"

# GUARD 2: a row count the chunk cannot possibly hold. The elided chunk's
# descriptor still accounts for its own 100,000 values, so a row_count of nine
# billion makes the same claim false in the other direction, and the reader
# refuses BEFORE it synthesizes anything -- which is the ordering that matters.
#
# NINE BILLION, NOT FOUR, SO THE ORDER IS VISIBLE (#1339 review). The bitmap it
# would need is 1.125 GB, over MaxAllocSize, so the "implausible row count"
# backstop sits between the no-nulls guard and the allocation: a no-nulls guard
# moved below the allocation is met by the backstop first and names it. At four
# billion the bitmap was 500 MB, under the backstop, and that move stayed green.
# It still cannot see a guard placed between the backstop and the allocation.
#
# A reworded guard message reddens both arms here and in the pytest twin, which
# must then be updated together: the red prints the real message.
#
# BOUNDING THE SYNTHESIZED BITMAP BY THE ROW GROUP'S BYTE LENGTH DOES NOT WORK,
# and this suite is where that was measured: ve_full's group is 360 bytes on
# disk and would need 12,500 bytes of bits, which is the saving, not a defect.
psql_run "UPDATE pgcolumnar.row_group r
	SET row_count = 9000000000
	FROM pgcolumnar.storage s
	WHERE s.storage_id = r.storage_id
	  AND s.relation_oid = 've_full'::regclass;"

check "an implausible row count on an elided chunk is refused before anything is allocated" \
	"$(guard_of "SELECT sum(v) FROM ve_full")" \
	"XX001|the no-nulls guard"

check "and the backend survives that refusal too" "$(q "SELECT 1;" | tail -1)" "1"

pgc_summary
