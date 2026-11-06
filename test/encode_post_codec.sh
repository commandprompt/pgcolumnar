#!/usr/bin/env bash
#
# An encoding is chosen on pre-codec bytes and stored post-codec (#1132).
#
# PgColumnarEncodeChunk picks the smallest candidate against bestLen, which
# starts at rawLen (columnar_encoding.c:2329), and every comparison is
# `len < bestLen` on UNCOMPRESSED bytes. The block codec runs afterwards, once,
# over the whole encoded region (columnar_write_state.c:1479), defaulting to
# zstd level 3. So an encoding that shrinks the bytes can still ENLARGE the
# stored chunk, because bit-packing whitens a stream the codec was exploiting.
#
# FSST is the one encoder that already decides post-codec, through
# PgColumnarFsstHelpsCompressed. This suite is the same question asked of the
# fixed-width encoders, which had no such gate.
#
# THE FIXTURE IS THE ARGUMENT, so it is built from the shape that fails rather
# than from a shape that happens to be convenient. Measured on ClickBench
# hits_0.parquet, the worst column was ClientEventTime: DELTA+FOR stored it
# 2.06x larger than storing it unencoded. Its shape is a HEAVY TAIL --
# 91,735 distinct values over a range of 1,707,676,369, because rare outliers
# reach back to 1971 while the typical value sits in a narrow recent band.
#
# That splits the two cost models exactly:
#   - FOR prices by RANGE, so it must size every value for the outliers;
#   - zstd prices by BYTE REDUNDANCY, and the typical value's high bytes are
#     constant, so it compresses what FOR spent bits on.
#
# Repetition alone does NOT reproduce it: a narrow-range column with 7,000
# scattered repeats was measured at 0.71x, encoding genuinely winning. The tail
# is the load-bearing ingredient, which is why the premise below asserts it.
#
# It asserts:
#   1. the tail fixture really is tail-shaped -- its range is set by outliers
#      and not by its typical value, observed rather than assumed;
#   2. a chunk is never stored larger than it would be unencoded, which is the
#      whole claim;
#   3. a column where encoding genuinely wins still encodes. This is the silent
#      direction: declining every encoding would satisfy 2 and reddens nothing;
#   4. the rows read back byte-identical against a heap mirror at every arm.
#
# Usage:  test/encode_post_codec.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

ROWS="${PGC_POST_CODEC_ROWS:-200000}"

# How many vectors of column 0 chose an encoding other than NONE (type 0).
#
# Same descriptor decode as fsst_margin.sh and write_fsst_compressed.sh: a
# 6-byte header (version, reserved, uint32 vector count) then that many 13-byte
# entries. Reading past the entries would score the chunk's trailing shared
# table as encoding types.
encoded_vectors() {	# table -> count of non-NONE vectors
	q "SELECT coalesce(sum(n), 0) FROM (
		SELECT (SELECT count(*)
				FROM generate_series(0,
					get_byte(c.encoding_descriptor, 2)
					+ get_byte(c.encoding_descriptor, 3) * 256
					+ get_byte(c.encoding_descriptor, 4) * 65536
					+ get_byte(c.encoding_descriptor, 5) * 16777216 - 1) i
				WHERE get_byte(c.encoding_descriptor, 6 + i * 13) <> 0) AS n
		FROM pgcolumnar.column_chunk c
		JOIN pgcolumnar.storage s ON s.storage_id = c.storage_id
		WHERE s.relation_oid = '$1'::regclass
		  AND c.column_index = 0) t;" | tail -1
}

# Stored bytes of the column's value stream: the pages minus the validity
# bitmap, which is one bit per row and is written raw ahead of the codec
# (columnar_write_state.c:1079). Subtracted so the number moves only with the
# encoding decision, which is what this suite is about.
#
# SUBTRACTED ONLY WHERE THERE IS ONE (#1130). A chunk that holds no null stores
# no bitmap and sets bit 0 of the descriptor's flags byte, so the old
# unconditional subtraction now removes bytes that were never written and
# understates the value stream. These fixtures hold no nulls at all, so every
# chunk takes the zero branch today; the condition is here because a fixture
# that gains a null must not silently change what this measures.
value_bytes() {	# table -> bytes
	q "SELECT coalesce(sum(c.page_length) - sum(
			CASE WHEN octet_length(c.encoding_descriptor) >= 6
			      AND (get_byte(c.encoding_descriptor, 1) & 1) = 1
			     THEN 0 ELSE (c.value_count + 7) / 8 END), 0)
		FROM pgcolumnar.column_chunk c
		JOIN pgcolumnar.storage s ON s.storage_id = c.storage_id
		WHERE s.relation_oid = '$1'::regclass
		  AND c.column_index = 0;" | tail -1
}

psql_run "SET pgcolumnar.compression = 'zstd';"

# THE TAIL FIXTURE. 99.9% of values sit in a 5,000-wide band; one in a thousand
# is drawn from a 1.7-billion-wide range. Seeded, so the arms below are reading
# one corpus and not one sample of a family of corpora.
#
# THE SEED AND THE INSERTS ARE ONE CALL, and the heap mirror is written from the same
# seeded generator, not copied from the table (#1339). Each psql_run is its own
# session, so a setseed in one call never reached the INSERT in the next: the
# fixture was unseeded. And a CREATE TABLE ... AS SELECT FROM epc_tail reads
# through the decoder under test, so a decode defect landed on both sides: with the
# reader returning row 5 of every row group as NULL, the read-back arms stayed green.
EPC_TAIL_GEN="SELECT CASE WHEN random() < 0.001
	            THEN 31525449 + floor(random() * 1700000000)::bigint
	            ELSE 1739000000 + floor(random() * 5000)::bigint END
	FROM generate_series(1, $ROWS) g"
psql_run "CREATE TABLE epc_tail (v bigint) USING pgcolumnar;
	CREATE TABLE epc_tail_h (v bigint);
	SELECT setseed(0.11); INSERT INTO epc_tail $EPC_TAIL_GEN;
	SELECT setseed(0.11); INSERT INTO epc_tail_h $EPC_TAIL_GEN;"

# THE CONTROL FIXTURE. A narrow range where every value repeats many times, the
# shape hits.EventTime has. Encoding earns its place here: measured 0.29x, so an
# over-eager decline would cost 3.5x on this column.
psql_run "CREATE TABLE epc_rep (v bigint) USING pgcolumnar;
	CREATE TABLE epc_rep_h (v bigint);
	INSERT INTO epc_rep SELECT 1700000000 + (g % 86400)::bigint FROM generate_series(1, $ROWS) g;
	INSERT INTO epc_rep_h SELECT 1700000000 + (g % 86400)::bigint FROM generate_series(1, $ROWS) g;"

tail_range="$(q "SELECT max(v) - min(v) FROM epc_tail;" | tail -1)"
# 1st to 99th percentile, NOT 0.1st to 99.9th. One row in a thousand is an
# outlier, so a 99.9th percentile lands exactly ON the boundary and reads either
# the band width or the full range depending on whether the draw produced a
# hair more or fewer than 200 outliers. Measured both ways from the same seed:
# 4,994 and 53,786,536. The 99th is far enough inside the body to be a property
# of the fixture rather than of the sample.
tail_body="$(q "SELECT (percentile_disc(0.99) WITHIN GROUP (ORDER BY v)
                       - percentile_disc(0.01) WITHIN GROUP (ORDER BY v)) FROM epc_tail;" | tail -1)"
tail_enc="$(encoded_vectors epc_tail)"
rep_enc="$(encoded_vectors epc_rep)"
tail_bytes="$(value_bytes epc_tail)"
rep_bytes="$(value_bytes epc_rep)"
raw_bytes=$((ROWS * 8))

echo "-- tail: range=$tail_range body-spread=$tail_body encoded_vectors=$tail_enc bytes=$tail_bytes"
echo "-- rep : encoded_vectors=$rep_enc bytes=$rep_bytes   raw=$raw_bytes"

# PREMISE: the fixture is tail-shaped. Without this the arms below could pass on
# a corpus that is merely narrow, where nothing interesting is being decided.
check "premise: the tail fixture's range is set by outliers, not by its typical value" \
	"$(awk -v r="$tail_range" -v p="$tail_body" \
		'BEGIN { print (r > 1000000000 && p < 100000) ? "tail-shaped" : "not-tail-shaped" }')" \
	"tail-shaped"

check "premise: both fixtures hold every row" \
	"$(q "SELECT (SELECT count(*) FROM epc_tail) || '/' || (SELECT count(*) FROM epc_rep);" | tail -1)" \
	"$ROWS/$ROWS"

# THE ARM. Storing the chunk unencoded is always available, so no encoding
# decision may ever land above it. Measured before this was fixed: FOR was
# chosen and stored 681,699 bytes where unencoded zstd stores 440,492, which is
# 42.6% and 27.5% of raw respectively. The 35% gate sits between them.
check "a chunk is not stored larger than it would be with no encoding at all" \
	"$(awk -v b="$tail_bytes" -v r="$raw_bytes" \
		'BEGIN { print (b <= r * 0.35) ? "not-inflated" : "inflated" }')" \
	"not-inflated"

# THE SILENT DIRECTION. Declining every encoding would satisfy the arm above and
# redden nothing, so the control ships beside it rather than after it.
check "while a column where encoding genuinely wins still encodes" \
	"$(awk -v n="$rep_enc" 'BEGIN { print (n + 0 > 0) ? "encoded" : "declined" }')" \
	"encoded"

check "and that column stays far below the no-encoding size, so the win is real" \
	"$(awk -v b="$rep_bytes" -v r="$raw_bytes" \
		'BEGIN { print (b <= r * 0.10) ? "small" : "large" }')" \
	"small"

# THE INVARIANT. 2 and 3 exist to prove this one is not vacuous: a decision that
# changes what comes back out is a data-loss bug, not a size regression.
# Named for the COLUMN SHAPE rather than the table, so the property is the name
# and the pytest twin can assert it over its own fixture (CONTEXT.md's
# independence rule) instead of inheriting this suite's table names.
for t in tail rep; do
	tbl="epc_$t"
	check "the $t column reads back exactly what the heap holds" \
		"$(q "SELECT count(*) FROM (
			SELECT v FROM $tbl EXCEPT ALL SELECT v FROM ${tbl}_h) d;" | tail -1)" \
		"0"
	check "the $t column holds no row the heap does not" \
		"$(q "SELECT count(*) FROM (
			SELECT v FROM ${tbl}_h EXCEPT ALL SELECT v FROM $tbl) d;" | tail -1)" \
		"0"
	check "the $t column preserves its checksum" \
		"$(q "SELECT coalesce(sum(v), 0) FROM $tbl;" | tail -1)" \
		"$(q "SELECT coalesce(sum(v), 0) FROM ${tbl}_h;" | tail -1)"
done

pgc_summary
