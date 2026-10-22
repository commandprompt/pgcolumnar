#!/usr/bin/env bash
#
# pgColumnar #289: fast decode of attbyval fixed-width columns.
#
# The read path inlines the by-value decode in pgcolumnar_native_next_row: for an
# attbyval column it does the same fetch_att + advance PgColumnarDecodeValue does,
# but without the out-of-line call and its own attbyval branch (the per-row
# decode dispatch #289 profiled as hot). By-reference (uuid) and varlena (text,
# numeric) columns keep the PgColumnarDecodeValue path and serve as controls. This
# test proves the inlined values are identical to the call path across every
# byval fixed type, every NULL pattern, every encoding, per-vector skipping,
# deletes and ADD COLUMN, plus adversarial bit patterns.
#
# The heap mirror is the oracle: a full projection compare is exact per value,
# so a single wrong decode anywhere fails it. Aggregates are chosen to be exact
# (count, integer sums, min/max including floats) to avoid float summation-order
# noise, which would be about the executor, not the decode this changes.
#
# The inline path is the same fetch_att the call path uses, so correctness is by
# construction; run this on an assert server too, so any bad read also trips a
# backend assertion under the oracle data.
#
# Usage:  test/native_fastdecode.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# ---- 1. heap oracle: every attbyval fixed type + controls, many groups ------

make_pair "id int,
	b  bool,
	i2 smallint, i4 int, i8 bigint,
	f4 real, f8 double precision,
	d  date, ts timestamp, tz timestamptz, tm time,
	txt text, uid uuid, num numeric"
# small groups so the once-per-group widen runs many times over the scan
psql_run "SELECT pgcolumnar.set_options('t_col', stripe_row_limit => 1000);"

# Interleaved NULLs with different phases per column, including leading (f8),
# trailing (ts) and dense (i8) runs, to stress the dense present-stream index
# against the validity bitmap.
load_pair "SELECT g,
	CASE WHEN g % 13 = 0 THEN NULL ELSE (g % 2 = 0) END,
	CASE WHEN g % 11 = 0 THEN NULL ELSE (g % 97 - 48)::smallint END,
	CASE WHEN g %  7 = 0 THEN NULL ELSE (g * 7 - 3) END,
	CASE WHEN g %  5 = 0 THEN NULL ELSE (g::bigint * 1000003 - 5) END,
	CASE WHEN g %  6 = 0 THEN NULL ELSE (g * 1.5)::real END,
	CASE WHEN g <= 50    THEN NULL ELSE (g * 3.14159)::float8 END,
	CASE WHEN g %  8 = 0 THEN NULL ELSE DATE '2020-01-01' + g END,
	CASE WHEN g > 5950   THEN NULL ELSE TIMESTAMP '2020-01-01' + (g || ' min')::interval END,
	CASE WHEN g %  9 = 0 THEN NULL ELSE TIMESTAMPTZ '2020-01-01' + (g || ' min')::interval END,
	CASE WHEN g % 10 = 0 THEN NULL ELSE TIME '00:00:00' + (g || ' sec')::interval END,
	CASE WHEN g %  4 = 0 THEN NULL ELSE 't' || g END,
	CASE WHEN g % 12 = 0 THEN NULL ELSE ('00000000-0000-0000-0000-' || lpad(g::text, 12, '0'))::uuid END,
	CASE WHEN g %  3 = 0 THEN NULL ELSE (g * 0.01)::numeric END
	FROM generate_series(1, 6000) g"

check "row count matches" "$(q 'SELECT count(*) FROM t_col;')" "6000"
check "scan is the columnar custom scan" "$(pgc_is_columnar_scan 'SELECT * FROM t_col')" "yes"
check "many row groups written (widen runs per group)" \
	"$([ "$(stripe_count)" -gt 1 ] && echo yes || echo no)" "yes"
# the byval fixed columns must carry real descriptors, or the fast path is never
# reached and this whole test would pass while exercising nothing.
check "byval fixed columns carry non-baseline descriptors" \
	"$(q "SELECT CASE WHEN count(*) = 0 THEN 'no chunks counted'
	             ELSE (count(*) FILTER (WHERE octet_length(encoding_descriptor) < 6))::text END
	      FROM pgcolumnar.column_chunk
	      WHERE storage_id = pgcolumnar.get_storage_id('t_col')
	        AND column_index BETWEEN 1 AND 10;")" "0"

# The core oracle: every value, exact, order-independent.
diff_query "full projection is byte-identical to heap" "SELECT * FROM %T"

# Exact aggregates (no float summation): count, integer sums, min/max of all.
diff_query "exact aggregates match heap" \
	"SELECT count(*), count(b), count(i4), count(f8), count(ts),
		sum(i2::bigint), sum(i4::bigint), sum(i8),
		bool_and(b), bool_or(b),
		min(i2), max(i2), min(i4), max(i4), min(i8), max(i8),
		min(f4), max(f4), min(f8), max(f8),
		min(d), max(d), min(ts), max(ts), min(tz), max(tz), min(tm), max(tm)
	 FROM %T"

# q4-shaped grouped aggregate (avg-per-bucket), kept to exact aggregates.
diff_query "grouped aggregate matches heap" \
	"SELECT (i4 % 24) AS bucket, count(*), sum(i8), min(f8), max(f8), max(d)
	 FROM %T WHERE i4 IS NOT NULL GROUP BY 1"

# Filtered scans on byval columns (predicate + decode together).
diff_query "filtered by int matches heap"  "SELECT * FROM %T WHERE i4 BETWEEN 1000 AND 2000"
diff_query "filtered by bigint matches heap" "SELECT id, i8 FROM %T WHERE i8 < 0 OR i8 > 3000000000"
diff_query "filtered by float matches heap" "SELECT id, f8 FROM %T WHERE f8 > 10000"
diff_query "filtered by date matches heap"  "SELECT id, d FROM %T WHERE d > DATE '2035-01-01'"

# ---- 2. adversarial boundary bit patterns -----------------------------------
# Values a naive (Datum)(uint64) cast or a sloppy sign/reinterpret would corrupt:
# sign bits, INT_MIN/MAX, -0.0, NaN, +/-Inf, subnormals.

make_pair "id int, i4 int, i8 bigint, f4 real, f8 double precision"
psql_run "SELECT pgcolumnar.set_options('t_col', stripe_row_limit => 1000);"
psql_run "INSERT INTO t_heap VALUES
	(1, -2147483648, -9223372036854775808, 'NaN',        'NaN'),
	(2,  2147483647,  9223372036854775807, 'Infinity',   'Infinity'),
	(3,  -1,          -1,                   '-Infinity',  '-Infinity'),
	(4,   0,           0,                   0.0,           0.0),
	(5,   1,           1,                  -0.0,          -0.0),
	(6,  -2147483647,  9223372036854775806, 1.1754944e-38, 5e-324),
	(7,   2147483646, -9223372036854775807, 3.4028235e38,  1.7976931348623157e308),
	(8,  -123456789,   1234567890123456789, -3.4028235e38, -1.7976931348623157e308);"
psql_run "INSERT INTO t_col SELECT * FROM t_heap;"
diff_query "boundary int/float bit patterns round-trip exactly" "SELECT * FROM %T"
# -0.0 and NaN specifically: text form must survive the widen verbatim.
check_text "negative zero preserved" \
	"$(q "SELECT f8::text FROM t_col WHERE id = 5;")" \
	"$(q "SELECT f8::text FROM t_heap WHERE id = 5;")"
check "NaN preserved" "$(q "SELECT f4::text FROM t_col WHERE id = 1;")" "NaN"

# ---- 3. every fixed-width encoding, same logical data ------------------------
# All fixed-width encodings converge to attlen-wide present values at rawBuf, so
# one fast path covers them. Force varied shapes and prove each matches heap.

make_pair "id int, seq bigint, dod bigint, cst int, gor float8, alp float8"
psql_run "SELECT pgcolumnar.set_options('t_col', stripe_row_limit => 2048, compression => 'none');"
load_pair "SELECT g,
	g::bigint * 4,                                   -- FOR / DELTA
	(g * (g + 1) / 2)::bigint,                       -- DELTA-of-DELTA friendly
	42,                                              -- RLE (constant)
	(100.0 + sin(g::float8 / 25.0) * 5.0)::float8,   -- GORILLA (smooth float)
	(g * 0.01)::float8                               -- ALP (decimal)
	FROM generate_series(1, 6000) g"
check "encoded columns carry non-baseline descriptors" \
	"$(q "SELECT CASE WHEN count(*) = 0 THEN 'no chunks counted'
	             ELSE (count(*) FILTER (WHERE octet_length(encoding_descriptor) < 6))::text END
	      FROM pgcolumnar.column_chunk
	      WHERE storage_id = pgcolumnar.get_storage_id('t_col')
	        AND column_index BETWEEN 1 AND 5;")" "0"
diff_query "all encodings decode identically to heap" "SELECT * FROM %T"
diff_query "aggregates over encoded columns match" \
	"SELECT count(*), sum(seq), sum(dod), sum(cst::bigint), min(gor), max(gor), min(alp), max(alp) FROM %T"

# ---- 4. per-vector skipping active (the #1 desync risk) ----------------------
# Large groups (many 1024-vectors each) over a monotonic key, then a narrow
# predicate so whole vectors are zone-map ruled out. If the typed index lost
# lockstep with the byte cursor after a vector jump, values would shift and the
# compare would fail.

make_pair "id int, k bigint, v float8"
# chunk_group_row_limit too: with only stripe_row_limit set, the default 10000-row
# chunk group made each 8192-row group ONE vector, so nothing inside a group was ever
# skipped -- the section skipped whole groups and never tested the jump it exists for
# (head read "Columnar Vectors Skipped: 0"). 1024 gives eight vectors a group.
psql_run "SELECT pgcolumnar.set_options('t_col', stripe_row_limit => 8192, chunk_group_row_limit => 1024);"
load_pair "SELECT g, g::bigint, (g * 2.5)::float8 FROM generate_series(1, 40000) g"
# Per GROUP: the table total was 5 here even with one vector per group, and with one
# vector a group has nothing to skip inside it.
check "vecskip: a row group holds multiple vectors" \
	"$(q "SELECT CASE WHEN max(n) > 1 THEN 'yes' ELSE 'no (' || coalesce(max(n)::text, 'none') || ')' END
	      FROM (SELECT count(*) n FROM pgcolumnar.zone_map
	            WHERE storage_id = pgcolumnar.get_storage_id('t_col')
	              AND vector_index >= 0 AND column_index = 0 GROUP BY group_number) s;")" "yes"
diff_query "narrow range with vector skipping matches heap" \
	"SELECT * FROM %T WHERE k BETWEEN 20000 AND 20100"
# The compares above prove nothing about skipping if no vector was skipped: with the
# skip mask dropped every vector decodes, the values are still right, and main passed
# 25/25. The scan's own counter says whether a jump happened.
VSKIP="$(q "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF)
            SELECT * FROM t_col WHERE k BETWEEN 20000 AND 20100;" |
	sed -n 's/.*Columnar Vectors Skipped: \([0-9][0-9]*\).*/\1/p' | head -1)"
check "the narrow range skips whole vectors" \
	"$( [ -n "$VSKIP" ] && [ "$VSKIP" -gt 0 ] && echo yes || echo "no (skipped [$VSKIP])")" "yes"
diff_query "two narrow ranges (non-adjacent vectors) match heap" \
	"SELECT id, v FROM %T WHERE k < 50 OR k BETWEEN 30000 AND 30050"
diff_query "aggregate under skipping matches heap" \
	"SELECT count(*), sum(k), min(v), max(v) FROM %T WHERE k BETWEEN 10000 AND 25000"

# ---- 5. deleted rows (cursor advances for deleted rows) ----------------------

make_pair "id int, i4 int, i8 bigint, f8 float8"
psql_run "SELECT pgcolumnar.set_options('t_col', stripe_row_limit => 1000);"
load_pair "SELECT g, g * 3, g::bigint * 7, (g / 3.0)::float8 FROM generate_series(1, 5000) g"
psql_run "DELETE FROM t_heap WHERE id % 4 = 0 OR id BETWEEN 1000 AND 1500;"
psql_run "DELETE FROM t_col  WHERE id % 4 = 0 OR id BETWEEN 1000 AND 1500;"
diff_query "scan after scattered deletes matches heap" "SELECT * FROM %T"
diff_query "aggregate after deletes matches heap" \
	"SELECT count(*), sum(i4::bigint), sum(i8), min(f8), max(f8) FROM %T"

# ---- 6. ADD COLUMN so early groups lack the column (nativeTyped[c] == NULL) --

make_pair "id int, a int"
psql_run "SELECT pgcolumnar.set_options('t_col', stripe_row_limit => 1000);"
load_pair "SELECT g, g * 2 FROM generate_series(1, 3000) g"
psql_run "ALTER TABLE t_heap ADD COLUMN b bigint DEFAULT 77;"
psql_run "ALTER TABLE t_col  ADD COLUMN b bigint DEFAULT 77;"
psql_run "INSERT INTO t_heap SELECT g, g * 2, g::bigint * 5 FROM generate_series(3001, 6000) g;"
psql_run "INSERT INTO t_col  SELECT g, g * 2, g::bigint * 5 FROM generate_series(3001, 6000) g;"
diff_query "scan across an ADD COLUMN boundary matches heap" "SELECT * FROM %T"
diff_query "aggregate across ADD COLUMN matches heap" \
	"SELECT count(*), sum(a::bigint), sum(b), min(b), max(b) FROM %T"

pgc_summary
