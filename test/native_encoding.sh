#!/usr/bin/env bash
#
# pgColumnar native cascade encoding (Phase D4): a native-format (PGCN v1) table
# now encodes each 1024-value vector with the lightweight adaptive selector and,
# when a block codec is set, block-compresses the encoded region. The reader
# reconstructs the exact raw values, so an encoded native table must still match a
# heap mirror. This suite proves round-trip parity across the block codecs
# (including compression=none, the encoded-but-not-block-compressed path that
# native_roundtrip does not exercise) and asserts that the encoding actually
# engages: a constant column's chunk shrinks far below its raw size, and every
# native chunk now carries a real (non-baseline) encoding descriptor.
#
# Usage:  test/native_encoding.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# Data with a distinct compressibility per column so the selector has something to
# choose: a monotonic id (delta/for), a constant bigint (rle/for/dict), a
# low-cardinality label (dict), a genuinely random-ish text (likely none per
# vector, still block-compressible), some nulls throughout. Small row-group limit
# so the write crosses several row groups and each column chunk spans several
# 1024-value vectors.
GEN="SELECT g,
       42::bigint,
       (ARRAY['cat','dog','bird'])[1 + g % 3],
       md5(g::text),
       CASE WHEN g % 7 = 0 THEN NULL ELSE g * 3 END
  FROM generate_series(1, 6000) g"

psql_run "CREATE TABLE h (id int, k bigint, label text, rnd text, v int);"
psql_run "INSERT INTO h $GEN;"

# enc_case CODEC -> builds n_CODEC from the generator and sets the R_* readings below.
# The checks after each call spell their names out: the grader reads only literal
# names, so the codec cannot be a loop variable (#1341).
enc_case() {
	local c="$1" t="n_$1" cc="FROM pgcolumnar.column_chunk WHERE storage_id = pgcolumnar.get_storage_id('n_$1')"
	psql_run "CREATE TABLE $t (id int, k bigint, label text, rnd text, v int) USING pgcolumnar;"
	psql_run "SELECT pgcolumnar.set_options('$t', stripe_row_limit => 1500, compression => '$c');"
	psql_run "INSERT INTO $t $GEN;"
	R_ROWS="$(q "SELECT count(*) FROM $t;")"
	R_HASH="$(pgc_set_hash "SELECT id, k, label, rnd, v FROM $t")"
	R_NULLS="$(q "SELECT count(*) FROM $t WHERE v IS NULL;")"
	R_FILT="$(pgc_set_hash "SELECT id, label FROM $t WHERE k = 42 AND id BETWEEN 2000 AND 4000")"
	R_SUM="$(q "SELECT sum(v) FROM $t;")"
	# Every count below is "bad/total": a total of 0 (no chunks read at all) can no
	# longer pass as "none bad".
	R_BASE="$(q "SELECT count(*) FILTER (WHERE octet_length(encoding_descriptor) < 6) || '/' || count(*) $cc;")"
	R_SMALL="$(q "SELECT count(*) FILTER (WHERE page_length > 4000) || '/' || count(*) $cc AND column_index = 1;")"
	# The first vector's encoding type is the byte after the 6-byte descriptor header.
	# A block codec shrinks a constant column on its own, so "encoded small" passed
	# with every vector stored raw (NONE = 0) for pglz, lz4 and zstd; this reads the
	# encoding itself.
	R_RAW="$(q "SELECT count(*) FILTER (WHERE get_byte(encoding_descriptor, 6) = 0) || '/' || count(*) $cc AND column_index = 1;")"
	R_CODEC="$(q "SELECT count(*) FILTER (WHERE block_codec <> 0) || '/' || count(*) $cc;")"
}
H_HASH="$(pgc_set_hash 'SELECT id, k, label, rnd, v FROM h')"
H_NULLS="$(q 'SELECT count(*) FROM h WHERE v IS NULL;')"
H_FILT="$(pgc_set_hash 'SELECT id, label FROM h WHERE k = 42 AND id BETWEEN 2000 AND 4000')"
H_SUM="$(q 'SELECT sum(v) FROM h;')"

enc_case none
check_num "[none] row count" "$R_ROWS" "6000"
check "[none] all columns round-trip" "$R_HASH" "$H_HASH"
check_num "[none] nulls round-trip" "$R_NULLS" "$H_NULLS"
check "[none] filtered scan" "$R_FILT" "$H_FILT"
check_num "[none] aggregate over encoded column" "$R_SUM" "$H_SUM"
# Every native column chunk carries a real encoding descriptor (>= 6-byte header),
# never the single-byte D2b baseline.
check "[none] no baseline descriptors remain" "$R_BASE" "0/20"
# The constant bigint column (column_index 1) must shrink far below its raw size.
check "[none] constant column encoded small" "$R_SMALL" "0/4"
check "[none] the constant column's vectors are encoded, not stored raw" "$R_RAW" "0/4"
# compression=none leaves block_codec unset on every chunk; the encoding is
# independent of the block codec.
check "[none] no block codec used" "$R_CODEC" "0/20"

enc_case pglz
check_num "[pglz] row count" "$R_ROWS" "6000"
check "[pglz] all columns round-trip" "$R_HASH" "$H_HASH"
check_num "[pglz] nulls round-trip" "$R_NULLS" "$H_NULLS"
check "[pglz] filtered scan" "$R_FILT" "$H_FILT"
check_num "[pglz] aggregate over encoded column" "$R_SUM" "$H_SUM"
# Every native column chunk carries a real encoding descriptor (>= 6-byte header),
# never the single-byte D2b baseline.
check "[pglz] no baseline descriptors remain" "$R_BASE" "0/20"
# The constant bigint column (column_index 1) must shrink far below its raw size.
check "[pglz] constant column encoded small" "$R_SMALL" "0/4"
check "[pglz] the constant column's vectors are encoded, not stored raw" "$R_RAW" "0/4"

enc_case lz4
check_num "[lz4] row count" "$R_ROWS" "6000"
check "[lz4] all columns round-trip" "$R_HASH" "$H_HASH"
check_num "[lz4] nulls round-trip" "$R_NULLS" "$H_NULLS"
check "[lz4] filtered scan" "$R_FILT" "$H_FILT"
check_num "[lz4] aggregate over encoded column" "$R_SUM" "$H_SUM"
# Every native column chunk carries a real encoding descriptor (>= 6-byte header),
# never the single-byte D2b baseline.
check "[lz4] no baseline descriptors remain" "$R_BASE" "0/20"
# The constant bigint column (column_index 1) must shrink far below its raw size.
check "[lz4] constant column encoded small" "$R_SMALL" "0/4"
check "[lz4] the constant column's vectors are encoded, not stored raw" "$R_RAW" "0/4"

enc_case zstd
check_num "[zstd] row count" "$R_ROWS" "6000"
check "[zstd] all columns round-trip" "$R_HASH" "$H_HASH"
check_num "[zstd] nulls round-trip" "$R_NULLS" "$H_NULLS"
check "[zstd] filtered scan" "$R_FILT" "$H_FILT"
check_num "[zstd] aggregate over encoded column" "$R_SUM" "$H_SUM"
# Every native column chunk carries a real encoding descriptor (>= 6-byte header),
# never the single-byte D2b baseline.
check "[zstd] no baseline descriptors remain" "$R_BASE" "0/20"
# The constant bigint column (column_index 1) must shrink far below its raw size.
check "[zstd] constant column encoded small" "$R_SMALL" "0/4"
check "[zstd] the constant column's vectors are encoded, not stored raw" "$R_RAW" "0/4"

# ---------------------------------------------------------------------------
# ALP (Phase E1): a float8 column of two-decimal values (a price) is a decimal in
# disguise, so the adaptive selector encodes it with ALP (encoding type 7). The
# per-vector encoding type is the first byte of each descriptor entry, after the
# 6-byte descriptor header; get_byte reads the first vector's type. Round-trip is
# proven against the heap mirror, including negative-zero and NaN exceptions.
# ---------------------------------------------------------------------------
psql_run "CREATE TABLE hp (id int, price float8, real8 float8);"
psql_run "INSERT INTO hp SELECT g,
             ((g * 37) % 100000) * 0.01,
             sqrt(g::float8) * (CASE WHEN g % 2 = 0 THEN -1 ELSE 1 END)
          FROM generate_series(1, 6000) g;"
# a few exception-forcing values (negative zero, NaN, infinity)
psql_run "INSERT INTO hp VALUES (6001, '-0'::float8, 'NaN'::float8),
                                (6002, 'Infinity'::float8, '-Infinity'::float8),
                                (6003, 12.34, 0.0);"

psql_run "CREATE TABLE np (id int, price float8, real8 float8) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('np', stripe_row_limit => 2048);"
psql_run "INSERT INTO np SELECT id, price, real8 FROM hp;"

check "alp column round-trips exactly" \
	"$(pgc_set_hash "SELECT id, price, real8 FROM np")" \
	"$(pgc_set_hash 'SELECT id, price, real8 FROM hp')"
check_num "alp special values round-trip" \
	"$(q "SELECT count(*) FROM np WHERE price = '-0'::float8 OR real8 = 'NaN'::float8 OR price = 'Infinity'::float8;")" \
	"$(q "SELECT count(*) FROM hp WHERE price = '-0'::float8 OR real8 = 'NaN'::float8 OR price = 'Infinity'::float8;")"
# the decimal price column (column_index 1) picks ALP for its first vector in at
# least one chunk (get_byte reads the first vector's encoding type, ALP = 7)
check "alp chosen for the decimal column" \
	"$([ "$(q "SELECT count(*) FROM pgcolumnar.column_chunk
	           WHERE storage_id = pgcolumnar.get_storage_id('np')
	             AND column_index = 1
	             AND get_byte(encoding_descriptor, 6) = 7;")" -ge 1 ] && echo yes || echo no)" \
	"yes"

# ---------------------------------------------------------------------------
# FSST (Phase E2): high-cardinality strings that share frequent substrings --
# URL / log-line shaped text -- are the case dictionary cannot help (every value
# is distinct, so the distinct count blows past the dictionary cap) but FSST
# compresses well by replacing shared substrings with one-byte codes. The
# selector must pick FSST (encoding type 8) for such a column, and the exact
# bytes (including any bytea) must round-trip against the heap mirror.
# ---------------------------------------------------------------------------
FGEN="SELECT g,
        'https://www.example.com/user/' || g || '/profile?id=' || md5(g::text) || '&ref=columnar',
        ('\\\\x' || md5(g::text) || md5((g+1)::text))::bytea
   FROM generate_series(1, 6000) g"

psql_run "CREATE TABLE hf (id int, url text, blob bytea);"
psql_run "INSERT INTO hf $FGEN;"

psql_run "CREATE TABLE nf (id int, url text, blob bytea) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('nf', stripe_row_limit => 2048, compression => 'none');"
psql_run "INSERT INTO nf $FGEN;"

check "fsst column round-trips exactly" \
	"$(pgc_set_hash "SELECT id, url, blob FROM nf")" \
	"$(pgc_set_hash 'SELECT id, url, blob FROM hf')"
check "fsst filtered scan round-trips" \
	"$(pgc_set_hash "SELECT id, url FROM nf WHERE id BETWEEN 3000 AND 3500")" \
	"$(pgc_set_hash 'SELECT id, url FROM hf WHERE id BETWEEN 3000 AND 3500')"
# the url column (column_index 1) picks FSST for its first vector in at least one
# chunk (get_byte reads the first vector's encoding type, FSST = 8)
check "fsst chosen for the shared-substring column" \
	"$([ "$(q "SELECT count(*) FROM pgcolumnar.column_chunk
	           WHERE storage_id = pgcolumnar.get_storage_id('nf')
	             AND column_index = 1
	             AND get_byte(encoding_descriptor, 6) = 8;")" -ge 1 ] && echo yes || echo no)" \
	"yes"
# FSST with compression=none must shrink the url column well below its raw size
# (each value ~90 bytes * ~2000 rows/chunk ~ 180000 raw; FSST-coded pages stay
# far under that even without a block codec)
check "fsst shrinks the url column" \
	"$([ "$(q "SELECT max(page_length) FROM pgcolumnar.column_chunk
	           WHERE storage_id = pgcolumnar.get_storage_id('nf')
	             AND column_index = 1;")" -lt \
	     "$(q "SELECT max(octet_length(url)) * 2048 FROM hf;")" ] && echo yes || echo no)" \
	"yes"

# E3b: the symbol table is stored once per chunk as a trailing descriptor region,
# not once per vector. The descriptor is [6-byte header][vectorCount entries of 13
# bytes][uint32 sharedTableLen][table]; assert the url column's descriptor carries
# a non-empty shared table, i.e. octet_length exceeds header + entries + the length
# word. vectorCount is the little-endian uint32 at descriptor offset 2. The chunk
# must also have chosen FSST: the caller builds and stores the table whatever the
# selector picks, so with every vector stored raw this passed on main.
check "fsst stores one shared table per chunk (E3b)" \
	"$([ "$(q "SELECT count(*) FROM pgcolumnar.column_chunk
	           WHERE storage_id = pgcolumnar.get_storage_id('nf')
	             AND column_index = 1
	             AND get_byte(encoding_descriptor, 6) = 8
	             AND octet_length(encoding_descriptor) >
	                 6 + (get_byte(encoding_descriptor, 2)
	                      + get_byte(encoding_descriptor, 3) * 256
	                      + get_byte(encoding_descriptor, 4) * 65536
	                      + get_byte(encoding_descriptor, 5) * 16777216) * 13 + 4;")" \
	     -ge 1 ] && echo yes || echo no)" \
	"yes"

# ---- sample-based candidate selection --------------------------------------
#
# pgcolumnar.encoding_sample_rows estimates each candidate on a strided sample and
# applies only the best two to the whole chunk, instead of applying all of them.
# 0 restores the exhaustive behaviour. What must hold is that the choice is an
# optimisation: the rows that come back are identical either way, whatever
# encoding each path picks.

psql_run "CREATE TABLE se_h (id bigint, k int, f float8, txt text);"
psql_run "INSERT INTO se_h SELECT g, (g % 977)::int, (g % 313)::float8 / 7.0,
          'v-' || (g % 4096) FROM generate_series(1, 60000) g;"

psql_run "CREATE TABLE se_off (LIKE se_h) USING pgcolumnar;"
psql_run "SET pgcolumnar.encoding_sample_rows = 0;
          INSERT INTO se_off SELECT * FROM se_h;"
psql_run "CREATE TABLE se_on (LIKE se_h) USING pgcolumnar;"
psql_run "SET pgcolumnar.encoding_sample_rows = 2048;
          INSERT INTO se_on SELECT * FROM se_h;"

check "sampling on == heap oracle" \
	"$(pgc_set_hash 'SELECT id, k, f, txt FROM se_on')" \
	"$(pgc_set_hash 'SELECT id, k, f, txt FROM se_h')"
check "sampling off == heap oracle" \
	"$(pgc_set_hash 'SELECT id, k, f, txt FROM se_off')" \
	"$(pgc_set_hash 'SELECT id, k, f, txt FROM se_h')"
check "sampling changes nothing a query can see" \
	"$(pgc_set_hash 'SELECT id, k, f, txt FROM se_on')" \
	"$(pgc_set_hash 'SELECT id, k, f, txt FROM se_off')"

# The sample is strided rather than a prefix, which matters for a column whose
# head does not describe its tail. Here the first 20000 values are constant, so a
# prefix sample would choose run-length encoding and then apply it to a chunk that
# is mostly random. Assert the sampled size stays close to what exhaustive
# selection achieves; a prefix-sampled build would be far larger.
psql_run "CREATE TABLE se_head_h (id bigint, v bigint);"
psql_run "INSERT INTO se_head_h
          SELECT g, CASE WHEN g <= 20000 THEN 7
                         ELSE (g * 2654435761) % 1000000000 END
          FROM generate_series(1, 60000) g;"
psql_run "CREATE TABLE se_head_off (LIKE se_head_h) USING pgcolumnar;"
psql_run "SET pgcolumnar.encoding_sample_rows = 0;
          INSERT INTO se_head_off SELECT * FROM se_head_h;"
psql_run "CREATE TABLE se_head_on (LIKE se_head_h) USING pgcolumnar;"
psql_run "SET pgcolumnar.encoding_sample_rows = 2048;
          INSERT INTO se_head_on SELECT * FROM se_head_h;"

check "a misleading column head does not cost much ratio" \
	"$([ "$(q "SELECT sum(page_length) FROM pgcolumnar.column_chunk
	           WHERE storage_id = pgcolumnar.get_storage_id('se_head_on')
	             AND column_index = 1;")" -le \
	     "$(q "SELECT ((sum(page_length) * 12) / 10)::bigint FROM pgcolumnar.column_chunk
	           WHERE storage_id = pgcolumnar.get_storage_id('se_head_off')
	             AND column_index = 1;")" ] && echo yes || echo no)" \
	"yes"
check "misleading-head column still reads back exactly" \
	"$(pgc_set_hash 'SELECT id, v FROM se_head_on')" \
	"$(pgc_set_hash 'SELECT id, v FROM se_head_h')"

# Run-length reads a local property, so the sample has to preserve runs. A pure
# stride hides any run shorter than the stride, and the stride grows with
# chunk_group_row_limit, which users are invited to raise. At 100000 rows per
# vector the stride for a 2048-value sample is 48, so these runs of 30 disappear
# entirely from a strided sample while remaining the best encoding for the column
# by a wide margin. Windows of consecutive values keep them visible.
#
# Verified to discriminate: with the windowed sample replaced by a pure stride,
# this check fails.
psql_run "CREATE TABLE se_run_h (id bigint, v bigint);"
psql_run "INSERT INTO se_run_h SELECT g, (g / 30) % 1000 FROM generate_series(1, 200000) g;"
psql_run "CREATE TABLE se_run_off (LIKE se_run_h) USING pgcolumnar;"
psql_run "SET pgcolumnar.chunk_group_row_limit = 100000;
          SET pgcolumnar.encoding_sample_rows = 0;
          INSERT INTO se_run_off SELECT * FROM se_run_h;"
psql_run "CREATE TABLE se_run_on (LIKE se_run_h) USING pgcolumnar;"
psql_run "SET pgcolumnar.chunk_group_row_limit = 100000;
          SET pgcolumnar.encoding_sample_rows = 2048;
          INSERT INTO se_run_on SELECT * FROM se_run_h;"
check "a run-length column is not misjudged by the sample" \
	"$([ "$(q "SELECT sum(page_length) FROM pgcolumnar.column_chunk
	           WHERE storage_id = pgcolumnar.get_storage_id('se_run_on')
	             AND column_index = 1;")" -le \
	     "$(q "SELECT ((sum(page_length) * 12) / 10)::bigint FROM pgcolumnar.column_chunk
	           WHERE storage_id = pgcolumnar.get_storage_id('se_run_off')
	             AND column_index = 1;")" ] && echo yes || echo no)" \
	"yes"
check "run-length column reads back exactly" \
	"$(pgc_set_hash 'SELECT id, v FROM se_run_on')" \
	"$(pgc_set_hash 'SELECT id, v FROM se_run_h')"

# A sample setting too small to rank anything means the exhaustive path, not a
# tiny sample: the alternative silently stores every large vector raw.
psql_run "CREATE TABLE se_tiny (LIKE se_run_h) USING pgcolumnar;"
psql_run "SET pgcolumnar.encoding_sample_rows = 8;
          INSERT INTO se_tiny SELECT * FROM se_run_h;"
check "a sample setting below the floor still encodes" \
	"$([ "$(q "SELECT sum(page_length) FROM pgcolumnar.column_chunk
	           WHERE storage_id = pgcolumnar.get_storage_id('se_tiny')
	             AND column_index = 1;")" -le \
	     "$(q "SELECT ((sum(page_length) * 12) / 10)::bigint FROM pgcolumnar.column_chunk
	           WHERE storage_id = pgcolumnar.get_storage_id('se_run_off')
	             AND column_index = 1;")" ] && echo yes || echo no)" \
	"yes"

# A chunk smaller than the sample window has no stride to take, so it falls back
# to applying every candidate. That path has to keep working.
psql_run "CREATE TABLE se_small (id bigint, v bigint) USING pgcolumnar;"
psql_run "SET pgcolumnar.encoding_sample_rows = 2048;
          INSERT INTO se_small SELECT g, g * 3 FROM generate_series(1, 500) g;"
# "Still encodes" is read from the stored encoding, not from the values: with a
# chunk too small to sample stored raw (NONE), the values still came back right,
# and this check passed on main.
check "a chunk smaller than the sample window still encodes" \
	"$(q "SELECT count(*) FILTER (WHERE get_byte(encoding_descriptor, 6) = 0) || '/' || count(*)
	      FROM pgcolumnar.column_chunk
	      WHERE storage_id = pgcolumnar.get_storage_id('se_small') AND column_index = 1;")" \
	"0/1"
check "a chunk smaller than the sample window reads back exactly" \
	"$(q 'SELECT count(*), sum(v) FROM se_small;' | tr '|' ' ')" \
	"500 375750"

pgc_summary
