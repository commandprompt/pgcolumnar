#!/usr/bin/env bash
#
# pgColumnar Parquet compression-codec read coverage (Phase G).
#
# The shared decoder handled only UNCOMPRESSED and SNAPPY. This suite covers the
# other codecs a foreign Parquet writer commonly uses -- GZIP, ZSTD, and LZ4_RAW --
# reading each back and comparing it to the rows the generator wrote, so a codec
# that decompresses to the wrong bytes is caught. It also checks that a crafted
# page header fails cleanly rather than crashing.
#
# THE ORACLE IS THE GENERATOR, NOT THE UNCOMPRESSED FILE (#1341). Every codec used to
# be compared with c_none.parquet read back through read_parquet, the reader under
# test. A defect after decompression -- in the page decode every codec shares --
# corrupts both sides alike: with one decoded byte flipped in every page over 4 KB,
# all ten checks passed on main. Each file is now compared with the same rows built
# in SQL from the generator the crafter wrote them from.
#
# EACH FILE'S CODEC IS READ, NOT ASSUMED. The crafter used to print which codec each
# file got to stderr, and nothing read it: a file pyarrow wrote uncompressed would
# have passed every codec arm. The premises now read the column chunk's compression
# from the file's own metadata.
#
# Usage:  test/native_parquet_codecs.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

if ! python3 -c 'import pyarrow.parquet' 2>/dev/null; then
	pgc_skip pyarrow "pyarrow not available; codec suite needs it to write compressed files"
fi

W="$PGC_WORKDIR"
psql_run "CREATE SERVER pq FOREIGN DATA WRAPPER pgcolumnar_parquet;"

# Enough rows and enough repetition that compression actually engages and pages are
# non-trivial (dictionary + multiple data pages), across a few types.
python3 "$(dirname "${BASH_SOURCE[0]}")/craft_parquet_codecs.py" "$W"
codec_of() { awk -v c="$1" '$1 == c { sub(/^[^ ]* /, ""); print }' "$W/codecs.txt" 2>/dev/null; }

# Backends killed by a signal, from the server log: a number, or nothing when the log
# can't be read. A fresh connection answers again after crash recovery, so "SELECT 1"
# alone passes with a backend killed before it; survival also requires this count not
# to have moved.
crashes() {
	local _n
	_n="$(pgc_pg "grep -c 'terminated by signal' '$PGC_LOGFILE'" 2>/dev/null)"
	case "$_n" in '' | *[!0-9]*) ;; *) echo "$_n" ;; esac
}
survived() {	# survived BEFORE -> "1" when the server answers and no backend died since
	local _up _now
	_up="$(q 'SELECT 1')"; _now="$(crashes)"
	if ! [[ "$1" =~ ^[0-9]+$ && "$_now" =~ ^[0-9]+$ ]]; then echo "no (crash count unreadable)"
	elif [ "$_up" = 1 ] && [ "$_now" = "$1" ]; then echo 1
	else echo "no ([$_up], $(( _now - $1 )) backend crashes)"; fi
}

# pyarrow names the LZ4_RAW codec "LZ4" in its metadata; the reader decodes only LZ4_RAW.
check "premise: the uncompressed file is UNCOMPRESSED" "$(codec_of none)" "UNCOMPRESSED"
check "premise: the gzip file is GZIP-compressed" "$(codec_of gzip)" "GZIP"
check "premise: the zstd file is ZSTD-compressed" "$(codec_of zstd)" "ZSTD"
check "premise: the lz4 file is LZ4-compressed" "$(codec_of lz4)" "LZ4"

cols="id int, grp int, val float8, s text"
gen="$(pgc_set_hash "SELECT g::int, (g % 50)::int, (g * 1.5)::float8, 'row-' || (g % 100)
                     FROM generate_series(0, 19999) g")"
rp() { pgc_set_hash "SELECT * FROM pgcolumnar.read_parquet('$W/c_$1.parquet') AS t($cols)"; }

check "uncompressed decodes to the generator's rows" "$(rp none)" "$gen"
check "gzip decodes to the generator's rows" "$(rp gzip)" "$gen"
check "zstd decodes to the generator's rows" "$(rp zstd)" "$gen"
check "lz4 decodes to the generator's rows" "$(rp lz4)" "$gen"

# a count per codec via the FDW surface too
for codec in gzip zstd lz4; do
	psql_run "CREATE FOREIGN TABLE fc_$codec ($cols) SERVER pq OPTIONS (path '$W/c_${codec}.parquet');"
done
check_num "gzip FDW count" "$(q "SELECT count(*) FROM fc_gzip;")" "20000"
check_num "zstd FDW count" "$(q "SELECT count(*) FROM fc_zstd;")" "20000"
check_num "lz4 FDW count" "$(q "SELECT count(*) FROM fc_lz4;")" "20000"

# an aggregate that forces every value through the decoder, not just row counts:
# sum(0 .. 19999) = 199990000
check_num "gzip sum matches the generator" \
	"$(q "SELECT sum(id) FROM pgcolumnar.read_parquet('$W/c_gzip.parquet') AS t($cols);")" "199990000"

# ---- an all-NULL column under GZIP, ZSTD and LZ4_RAW (#1786) ----------------
# pyarrow writes a column that is NULL in every row with a dictionary page that holds
# no values, so the page declares an uncompressed size of 0. The decoder refused every
# size-0 page for these three codecs, so the column could not be read at all.
check "premise: the all-NULL gzip file is GZIP with a dictionary page" "$(codec_of allnull_gzip)" "GZIP dict"
check "premise: the all-NULL zstd file is ZSTD with a dictionary page" "$(codec_of allnull_zstd)" "ZSTD dict"
check "premise: the all-NULL lz4 file is LZ4 with a dictionary page" "$(codec_of allnull_lz4)" "LZ4 dict"
allnull() { q "SELECT count(*) || '|' || count(s) FROM pgcolumnar.read_parquet('$W/allnull_$1.parquet') AS t(id bigint, s text);"; }
check "all-NULL gzip column reads as 3 rows, none non-NULL" "$(allnull gzip)" "3|0"
check "all-NULL zstd column reads as 3 rows, none non-NULL" "$(allnull zstd)" "3|0"
check "all-NULL lz4 column reads as 3 rows, none non-NULL" "$(allnull lz4)" "3|0"

# ---- crafted page-header size must yield a clean decode error --------------
# uncompressed_page_size comes from the page header unvalidated; the codec consumes it.
# The crafter patched it to -1 (zigzag), so it arrives as an enormous size_t. The guard
# must reject with the reader's own "could not decode" error, not a generic "invalid
# string enlargement" from enlargeStringInfo -- and not a crash.
errtext() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "$1" 2>&1 | grep -m1 '^ERROR:'
}
CRASHES_BEFORE="$(crashes)"
hdr_err="$(errtext "SELECT * FROM pgcolumnar.read_parquet('$W/badhdr.parquet') AS t(a int)")"
case "$hdr_err" in
	*"could not decode"*) hdr_verdict="CLEAN DECODE ERROR" ;;
	*) hdr_verdict="${hdr_err:-no error}" ;;
esac
check "crafted uncompressed size gives a clean decode error" "$hdr_verdict" "CLEAN DECODE ERROR"
check "backend survived the crafted header" "$(survived "$CRASHES_BEFORE")" "1"

pgc_summary
