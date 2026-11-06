#!/usr/bin/env bash
#
# Encoding invariants for the #155 bulk-load optimisations.
#
# Four changes rewrote hot paths in the encoder: the FSST build skip, the
# dictionary hash, word-at-a-time bit packing, and the batched Gorilla bit
# writer. Three of them are rewrites that must emit exactly the bytes the old
# code emitted; the fourth changes a decision and claims the stored bytes are
# unchanged as a consequence. None of that was pinned by a test.
#
# What a suite can and cannot do here matters. It cannot compare against a build
# that no longer exists, so "byte-identical to the previous implementation" is not
# directly assertable. What it can do is pin the properties that make the claim
# true, and exercise the edges the rewrites introduced:
#
#   1. bit packing at every width from 1 to 64, including the width where the
#      mask special case lives, because a wrong mask or a shift-by-64 shows up as
#      wrong values rather than as a crash;
#   2. Gorilla over float patterns chosen to drive the leading and trailing zero
#      counts across their range, through the batched bit writer;
#   3. dictionary encoding on both sides of DICT_MAX_DISTINCT, since the hash
#      rewrite has to preserve first-seen assignment order to keep codes stable;
#   4. the premise the FSST skip rests on, which is the one thing in the stack
#      that is an argument rather than a mechanism: that no vector selects FSST
#      while the distinct count is at or below the dictionary cap. If that holds,
#      not building the table cannot change output. If it does not, the skip
#      changes stored bytes.
#
# Every check compares against a heap mirror rather than a literal, so a wrong
# expectation cannot be written into the test.
#
# Usage:  test/encode_invariants.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# #1132: the writer now keeps an encoding only when it is smaller AFTER block
# compression, and this suite's subject is the ENCODERS rather than that choice.
# Its fixtures are chosen to exercise a particular encoder, which is not the same
# as being fixtures where that encoder beats zstd -- two of its controls assert
# the encoder actually ran, and those went red when the choice landed. Pinning the
# pre-#1132 behaviour here keeps this suite testing what it is named for; the
# selection policy has its own suite, encode_post_codec.sh.
psql_run "ALTER DATABASE $PGC_DB SET pgcolumnar.enable_post_codec_encoding_choice = off;" >/dev/null 2>&1

ROWS="${PGC_ENCINV_ROWS:-4096}"

# The C-level half. Bound here rather than shipped, like the other debug hooks.
#
# Two of the rewrites have edges no SQL fixture can reach, because the encoder
# never selects the path: the (width == 64) mask in the bit packer, and the zero
# guard in ctz_in. Both were confirmed unreachable by removal proofs that failed
# to fail against the SQL checks below. This function calls the primitives
# directly and compares each against a reference implementation of the algorithm
# it replaced, which is the promise the rewrites actually made: the same bytes.
psql_run "CREATE FUNCTION pgcolumnar.debug_encoding_selftest()
  RETURNS SETOF text AS 'pgcolumnar', 'pgcolumnar_debug_encoding_selftest'
  LANGUAGE C;" >/dev/null 2>&1

selftest="$(q "SELECT * FROM pgcolumnar.debug_encoding_selftest();")"
fails="$(printf '%s\n' "$selftest" | grep -c '^FAIL' || true)"
ran="$(printf '%s\n' "$selftest" | sed -n 's/^cases=//p')"

# A self-test that returned nothing has no FAIL lines either: "none" is read only
# over a run that reported its case count (measured: with the function never
# created, this passed on its own).
check "the encoding primitives match their reference implementations byte for byte" \
	"$(if ! pgc_is_number "$ran"; then echo "the self-test returned no case count [${ran:-none}]";
	   elif [ "$fails" = 0 ]; then echo none;
	   else printf '%s' "$(printf '%s\n' "$selftest" | grep '^FAIL' | head -3)"; fi)" \
	"none"
# Without this a self-test that compared nothing would report no failures. It also
# catches the self-test dying mid-run: pg_rightmost_one_pos64 asserts on zero, so
# removing the ctz_in guard takes the backend down before any row is returned and
# this reports ran=none rather than a clean pass. Proved by removal, both ways.
check "and the self-test actually compared a meaningful number of cases" \
	"$([ -n "$ran" ] && [ "$ran" -gt 500 ] && echo yes || echo "no (ran=${ran:-none})")" "yes"

# Count vectors of the first non-id column that chose a given encoding type.
# Same descriptor decode as write_fsst_compressed.sh: a 6-byte header whose last
# four bytes are the vector count, then that many 13-byte entries.
enc_vectors() {  # table type -> count
	q "SELECT coalesce(sum(n), 0) FROM (
		SELECT (SELECT count(*)
				FROM generate_series(0,
					get_byte(c.encoding_descriptor, 2)
					+ get_byte(c.encoding_descriptor, 3) * 256
					+ get_byte(c.encoding_descriptor, 4) * 65536
					+ get_byte(c.encoding_descriptor, 5) * 16777216 - 1) i
				WHERE get_byte(c.encoding_descriptor, 6 + i * 13) = $2) AS n
		FROM pgcolumnar.column_chunk c
		JOIN pgcolumnar.storage s ON s.storage_id = c.storage_id
		WHERE s.relation_oid = '$1'::regclass
		  AND c.column_index = 1) t;" | tail -1
}

# Load the same expression into a columnar table and a heap mirror, then compare.
pair() {  # name coltype expr
	psql_run "DROP TABLE IF EXISTS ${1}_c; DROP TABLE IF EXISTS ${1}_h;
		CREATE TABLE ${1}_c (id int, v $2) USING pgcolumnar;
		CREATE TABLE ${1}_h (id int, v $2);
		INSERT INTO ${1}_c SELECT g, $3 FROM generate_series(1, $ROWS) g;
		INSERT INTO ${1}_h SELECT g, $3 FROM generate_series(1, $ROWS) g;" >/dev/null 2>&1
}
agrees() {  # name -> yes/no
	local a b
	a="$(q "SELECT md5(string_agg(t::text, '' ORDER BY t::text)) FROM ${1}_c t;")"
	b="$(q "SELECT md5(string_agg(t::text, '' ORDER BY t::text)) FROM ${1}_h t;")"
	[ -n "$a" ] && [ "$a" = "$b" ] && echo yes || echo "no"
}

# --- 1. bit packing at every width (#285) ------------------------------------
#
# The packed width follows the largest value in the vector, so a column whose
# maximum is 2^k - 1 exercises width k. Widths 1 to 63 are driven with bigint;
# 64 needs the full range, which is where the (width == 64) mask special case
# lives and where a naive (1 << width) - 1 is undefined.
echo "-- bit-pack widths"
# One row carries the maximum so the packed width is exactly k; the rest vary so
# the vector has enough distinct values for the packing to be worth choosing.
#
# The widths stop at 32 because that is where the encoder stops choosing this
# path, not because higher widths are uninteresting. Measured: with few distinct
# values the dictionary wins above ~32 bits, and with many distinct values across
# a wide range nothing wins, because packing near the full width saves nothing
# over raw. So widths above ~32, the nine-byte field span, and the (width == 64)
# mask are defensive code that no SQL fixture can reach. They are worth keeping
# and cannot be covered here; covering them needs a C-level test rather than a
# suite. Claiming otherwise is how a check ends up passing while the thing it
# names is never executed, which an earlier version of this file did.
wfail=""
for k in 1 2 3 4 7 8 9 15 16 17 24 25 31 32; do
	maxv="((1::bigint << $k) - 1)"
	pair "bp$k" bigint "CASE WHEN g = 1 THEN $maxv ELSE (g % 997)::bigint END"
	[ "$(agrees "bp$k")" = yes ] || wfail="$wfail $k"
done
check "every reachable bit-pack width round-trips" \
	"$([ -z "$wfail" ] && echo none || echo "failed:$wfail")" "none"

# Full-range 64-bit values still have to survive a write and a read, whatever
# encoding is chosen for them. This is a round-trip check and deliberately not
# labelled as a test of the width-64 mask: as noted above, the encoder does not
# select packing for this data, so the mask is not what runs here.
pair bp64 bigint "(('x' || md5(g::text))::bit(64)::bigint)"
check "full-range 64-bit values round-trip whatever encoding is chosen" \
	"$(agrees bp64)" "yes"

# A control: the widths above are only meaningful if something is bit-packing.
# Without this the widths above prove only that some encoding round-trips, not
# that the rewritten packer ran at all.
check "and the width fixtures actually chose frame-of-reference packing" \
	"$([ "$(enc_vectors bp31_c 2)" -gt 0 ] && echo yes || echo no)" "yes"

# --- 2. Gorilla bit-scan edges (#286) ----------------------------------------
#
# clz_in and ctz_in now use a hardware bit scan that requires a non-zero
# argument. These patterns drive the XOR to zero (identical neighbours), to a
# single high bit, to a single low bit, and to a full-width value.
echo "-- gorilla patterns"
gfail=""
gpat_constant="1.5::float8"
gpat_zero="0::float8"
gpat_tiny="(1.0 + g * 1e-15)::float8"
gpat_alt="CASE WHEN g % 2 = 0 THEN 1.0::float8 ELSE -1.0::float8 END"
gpat_extreme="CASE g % 4 WHEN 0 THEN 0::float8 WHEN 1 THEN 1e308::float8
                          WHEN 2 THEN -1e308::float8 ELSE 5e-324::float8 END"
gpat_seq="(g::float8)"
for p in constant zero tiny alt extreme seq; do
	eval "e=\$gpat_$p"
	pair "g_$p" float8 "$e"
	[ "$(agrees "g_$p")" = yes ] || gfail="$gfail $p"
done
# Proved by removal that this section covers the batched writer but NOT the
# ctz_in zero guard: with `if (x == 0) return 0;` deleted, every check here still
# passes. Gorilla encodes identical neighbours with a same-as-previous bit rather
# than XORing and counting, so ctz_in is never reached with zero from SQL. The
# guard is correct to keep and is defensive, like the width-64 mask above.
check "gorilla float patterns round-trip, including all-zero XOR" \
	"$([ -z "$gfail" ] && echo none || echo "failed:$gfail")" "none"
check "and at least one of them actually chose gorilla" \
	"$([ "$(enc_vectors g_tiny_c 4)" -gt 0 ] && echo yes || echo no)" "yes"

# --- 3. dictionary around the distinct cap (#284) ----------------------------
#
# The hash rewrite must preserve first-seen assignment order, or the codes and
# the dictionary differ. Round-tripping at, just below, and just above the cap is
# what would catch an order change or an off-by-one at the boundary.
echo "-- dictionary cardinality"
dfail=""
for d in 1 2 1023 1024 1025; do
	pair "d$d" text "'v' || (g % $d)"
	[ "$(agrees "d$d")" = yes ] || dfail="$dfail $d"
done
check "dictionary round-trips at, below and above the distinct cap" \
	"$([ -z "$dfail" ] && echo none || echo "failed:$dfail")" "none"
check "and the low-cardinality fixture actually chose dictionary" \
	"$([ "$(enc_vectors d2_c 6)" -gt 0 ] && echo yes || echo no)" "yes"

# --- 4. the premise under the FSST build skip (#283) -------------------------
#
# The skip is safe if and only if no vector selects FSST while the distinct count
# is at or below the dictionary cap: that is what makes "the table would have been
# built and never used" true. Assert it directly.
#
# The control comes first and is the important half. A corpus that does select
# FSST proves this suite can see FSST at all; without it, "no FSST below the cap"
# passes on a suite that could never have observed FSST anywhere.
echo "-- fsst skip premise"
psql_run "DROP TABLE IF EXISTS fs_hi;
	SET pgcolumnar.fsst_min_gain_percent = 0;
	CREATE TABLE fs_hi (id int, v text) USING pgcolumnar;
	INSERT INTO fs_hi SELECT g, md5(g::text) || md5((g * 7)::text)
		FROM generate_series(1, 20000) g;" >/dev/null 2>&1
hi_fsst="$(enc_vectors fs_hi 8)"
check "control: a high-cardinality corpus does select FSST" \
	"$([ "$hi_fsst" -gt 0 ] && echo yes || echo no)" "yes"

# Now the premise, across shapes that stay at or under the cap. Long values are
# included on purpose: that is the case where the dictionary is viable but may
# not save enough for the encoder to stop looking, which is the gap the skip's
# reasoning has to cover.
lofail=""
for d in 16 256 1000 1024; do
	for shape in short long; do
		if [ "$shape" = short ]; then e="'v' || (g % $d)"
		else e="'https://example.com/catalog/section/item-' || (g % $d) || '-standard-edition-variant'"; fi
		t="fs_${d}_${shape}"
		psql_run "DROP TABLE IF EXISTS $t;
			SET pgcolumnar.fsst_min_gain_percent = 0;
			CREATE TABLE $t (id int, v text) USING pgcolumnar;
			INSERT INTO $t SELECT g, $e FROM generate_series(1, 20000) g;" >/dev/null 2>&1
		# The table must hold its 20000 rows: an empty one has no vector to choose
		# FSST and read 0 (measured: with every load empty, this passed).
		rows="$(q "SELECT count(*) FROM $t;" | tail -1)"
		n="$(enc_vectors "$t" 8)"
		[ "$rows" = 20000 ] && [ "$n" = 0 ] || lofail="$lofail ${d}/${shape}=$n(rows=$rows)"
	done
done
check "no vector selects FSST at or below the distinct cap, so skipping its build cannot change output" \
	"$([ -z "$lofail" ] && echo none || echo "failed:$lofail")" "none"

# ---- the block compression default is pinned to its measurement (#890) -------
#
# The roadmap proposed making this default depend on a storage tier, on the premise
# that block compression trades CPU for I/O. Phase 1 refuted the premise ON THIS
# ENGINE: the cascade runs lightweight encodings first, so zstd works on
# already-reduced bytes and its remaining cost is small against the memory traffic
# it removes. PG 17.10, 400k rows, minimum of 12, arms round-robined, and the
# none/zstd ranges do not overlap on any shape:
#
#     shape   marginal bytes saved   scan time vs none
#     rep                   +76.6%   0.92  (faster)
#     rand                   -1.7%   1.14  (slower -- incompressible)
#     mix                   +12.0%   0.92  (faster)
#
#
# WARM CACHE, which understates the saving ON THE SHAPES THAT COMPRESS: no physical
# read happens on either arm, so a cold run adds an I/O term proportional to bytes
# read and rep and mix can only widen.
#
# NOT SO ON rand. There zstd writes 148,076 MORE bytes than none, so cold makes it
# worse on both terms at once -- more bytes to read AND the same decompression CPU.
# The incompressible shape degrading cold is a SECOND, independent reason for the
# per-table override rather than a global change.
# A pin, not a discovery: changing this default should be a deliberate
# act with a new measurement attached. Incompressible data is a real cost and the
# answer there is the per-table override, not a different global default.
check "the block compression default is zstd, pinned to #890 phase 1" \
	"$(q "SHOW pgcolumnar.compression")" "zstd"
check "and its level is 3" \
	"$(q "SHOW pgcolumnar.compression_level")" "3"

pgc_summary
