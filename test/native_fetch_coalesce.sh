#!/usr/bin/env bash
#
# Index-fetch I/O is one ReadLogicalData per column (validity, then the value
# stream). The scan path already coalesces adjacent chunk ranges into one read.
# Adjacent columns are laid out back to back, so a wide fetch of a small group
# is many pins of the same pages rather than one walk.
#
# Public seam: EXPLAIN (ANALYZE, BUFFERS) pin count (shared hit+read) after a
# warmup, with the index path forced. The property is the COUNT, not wall
# clock, so it is not subject to PGC_SKIP_TIMING.
#
# Independent of test/pytest/test_native_fetch_coalesce.py: same public seam,
# own fixture, own observations.
#
# Usage:  test/native_fetch_coalesce.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/lib/postgresql/18/bin/pg_config}"

NCOLS=16
ROWS=2000
STRIPE=1000

cols=""
ins=""
sel_wide=""
i=0
while [ "$i" -lt "$NCOLS" ]; do
	cols="${cols}, c$(printf '%02d' "$i") int"
	ins="${ins}, g * ($i + 1)"
	if [ -n "$sel_wide" ]; then
		sel_wide="${sel_wide}, c$(printf '%02d' "$i")"
	else
		sel_wide="c$(printf '%02d' "$i")"
	fi
	i=$((i + 1))
done

q "CREATE EXTENSION IF NOT EXISTS pgcolumnar;" >/dev/null
q "CREATE TABLE nfc (id int${cols}) USING pgcolumnar;" >/dev/null
q "SELECT pgcolumnar.set_options('nfc', stripe_row_limit => ${STRIPE},
                                 chunk_group_row_limit => 1000,
                                 compression => 'none');" >/dev/null
q "INSERT INTO nfc SELECT g${ins} FROM generate_series(1, ${ROWS}) g;
   CREATE INDEX nfc_id ON nfc (id);
   ANALYZE nfc;" >/dev/null

FORCE="SET max_parallel_workers_per_gather=0; SET enable_seqscan=off;
       SET enable_bitmapscan=off; SET pgcolumnar.enable_custom_scan=off;
       SET pgcolumnar.enable_index_fetch_penalty=off;"

plan_scan() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-Atq -c "${FORCE}
		         EXPLAIN (COSTS OFF) $1" 2>&1 \
		| grep -m1 -oE 'Index Scan|Index Only Scan|Bitmap Heap Scan|Custom Scan|Seq Scan'
}

bufs() {
	local sql="$1"
	psql_run "${FORCE} EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF, FORMAT TEXT) ${sql}" \
		>/dev/null 2>&1
	psql_run "${FORCE} EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF, FORMAT TEXT) ${sql}" \
		2>/dev/null |
		awk '
			/Planning:/ { p=1 }
			!p && /Buffers:/ {
				for (i = 1; i <= NF; i++) {
					if ($i ~ /^(shared|read|hit)/) {
						gsub(/[^0-9]/, "", $i)
						if ($i != "") t += $i
					}
				}
			}
			END { print t + 0 }'
}

fetch_val() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-Atq -c "$1" 2>&1 | grep -v '^SET$' | tail -1
}

check "premise: a point lookup uses the index" \
	"$(plan_scan "SELECT ${sel_wide} FROM nfc WHERE id = 1")" "Index Scan"

NARROW="$(bufs "SELECT c00 FROM nfc WHERE id = 1;")"
WIDE="$(bufs "SELECT ${sel_wide} FROM nfc WHERE id = 1;")"
echo "-- exec buffers: one column = ${NARROW}, ${NCOLS} columns = ${WIDE}"

check_num "premise: fetching one projected column touched a measurable number of buffers" \
	"$([ "${NARROW:-0}" -gt 0 ] && echo 1 || echo 0)" "1"
check_num "premise: fetching every projected column touched a measurable number of buffers" \
	"$([ "${WIDE:-0}" -gt 0 ] && echo 1 || echo 0)" "1"

# Planning buffers are ignored: they grow with the target list and are not
# fetch I/O. The Index Scan line is.
#
# THE BOUND IS A FEW PINS, NOT ONE PER EXTRA COLUMN (#1339). It used to be one pin
# per extra column, which is exactly the once-per-column cost the name forbids, so
# a fetch that read every column on its own passed: measured on PG17 with
# coalescing disabled, one column = 71 and 16 columns = 86, the bound to the pin.
# Coalesced, the wide fetch walks the same pages as the narrow one: measured 70
# and 70. Three pins of slack absorbs a page boundary; fifteen extra does not fit.
# (The old comment's "two pins each, validity then values" stopped being true with
# #1130: a column with no nulls stores no bitmap.)
SLACK=3
# `check`, not `check_num`, because the value now carries the measurement rather
# than a 1 or a 0. Collapsed to a number this failed as `got [0] want [1]` and said
# nothing about how far over the bound the wide fetch went (#1164).
check "a wide index fetch does not pin once per column" \
	"$([ "${WIDE:-0}" -le $((NARROW + SLACK)) ] && echo coalesced \
		|| echo "wide ${WIDE:-0} over the bound $((NARROW + SLACK)) (narrow $NARROW + $SLACK)")" \
	"coalesced"

expect_wide=""
i=0
while [ "$i" -lt "$NCOLS" ]; do
	v=$((1 * (i + 1)))
	if [ -n "$expect_wide" ]; then
		expect_wide="${expect_wide}|${v}"
	else
		expect_wide="${v}"
	fi
	i=$((i + 1))
done
check "the wide fetch returns the projected values" \
	"$(fetch_val "${FORCE} SELECT ${sel_wide} FROM nfc WHERE id = 1;")" "$expect_wide"

# ---- the validity copy must be bounded by the chunk, not by the row count -----
#
# THIS IS AN ORDERING PIN AND IT IS DELIBERATELY NOT BEHAVIOURAL. The defect it
# guards was a heap overread: the coalesced path copies the validity bytes out of a
# span buffer that is only guaranteed to hold page_length bytes for this chunk,
# and the test reconciling the two used to run three lines AFTER the copy.
#
# Reproduced before the fix, on a build with -fsanitize=address, by poisoning
# pgcolumnar.column_chunk.page_length below the validity bitmap on the last
# chunk by page_offset and issuing a plain index-scan SELECT:
#
#     AddressSanitizer: heap-buffer-overflow
#     READ of size 625, 0 bytes after a 2640-byte region
#       pgcolumnar_fetch_coalesce_read  columnar_reader.c   (the memcpy)
#       pgcolumnar_fetch_row
#       printtup
#
# A behavioural arm here would be VACUOUS on this build. Reading ~117 bytes past
# a palloc'd span reads adjacent heap and returns quietly without a sanitizer, so
# the suite would report PASS on the broken code. The sanitizer run is the
# behavioural proof and it belongs to the nightly ASAN job; what this suite can
# assert deterministically is the property that was wrong -- the order.
#
# Line numbers are read from the source rather than counted, because a count
# would pass on a file where the two statements had swapped.
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/src"

# awk reads the file directly: piping a captured string into a reader that exits
# early is what selftest/080 refuses (#486).
# $2 is an OPTIONAL anchor: the search starts only after a line matching it.
# THE FUNCTION HAS TWO GUARDS WITH THE SAME TEXT. The range-building loop defers
# a chunk the checked decode path would refuse, and the distribution loop bounds
# the validity copy; both read `pageLength < (uint64) cvb`. Without an anchor
# this finds the FIRST, which is in the wrong loop -- the ordering check would
# then still pass with the distribution guard deleted, which is the guard it
# exists to pin. Anchoring on the containment test, which only the distribution
# loop has, pins the right one. Reported by @jdatcmd.
#
# THE TEXT MOVED WITH #1130 and the pattern moved with it: both guards compared
# against `validityBytes`, the row group's ceil(rowCount / 8), until the bitmap
# became a property of the CHUNK. `cvb` is that chunk's own size, 0 where the
# bitmap was elided. The property this suite pins -- the bound precedes the copy
# -- is unchanged; only the name it is spelled with moved, which is the standing
# cost of pinning source text and the reason the pattern is stated once here.
_nfc_line() {
	awk -v pat="$1" -v after="$2" '
		/^pgcolumnar_fetch_coalesce_read[(]/       { f = 1 }
		f && after != "" && $0 ~ after             { g = 1; next }
		f && (after == "" || g) && $0 ~ pat        { print NR; exit }
		f && /^}/                                  { exit }
	' "$SRC/columnar_reader.c"
}

# BRACKETS, NOT BACKSLASHES, and this is not style. `awk -v` processes escape
# sequences in the VALUE, and `\(` is not a defined escape, so the result is
# implementation-defined: mawk keeps the backslash and the pattern matches, gawk
# strips it with a warning and the pattern becomes a regex GROUP that never
# matches the literal text. `memcpy\(entry->vbits` is worse under gawk -- it
# becomes an unmatched `(` and awk exits fatally. Either way both variables come
# back empty and both checks below report "no", so the arm fails on a tree where
# the property holds.
#
# Measured on one machine, same file, same commit:
#     mawk   guard=4150 copy=4161
#     gawk   warning, then nothing
# CI runners carry gawk; this container carried mawk, which is why it passed
# here and failed there. A bracket expression cannot be mangled by -v escape
# processing and means a literal paren in both. Verified identical under mawk
# and gawk. Reported by @jdatcmd.
_nfc_guard="$(_nfc_line 'pageLength < [(]uint64[)] cvb' 'cc->pageOffset < start')"
_nfc_copy="$(_nfc_line 'memcpy[(]entry->vbits' '')"

check "premise: the coalescing helper holds both the bound and the validity copy" \
	"$([ -n "$_nfc_guard" ] && [ -n "$_nfc_copy" ] && echo yes || echo no)" "yes"

check "the validity copy is bounded by the chunk length before it runs" \
	"$([ -n "$_nfc_guard" ] && [ -n "$_nfc_copy" ] && \
	   [ "$_nfc_guard" -lt "$_nfc_copy" ] && echo yes || echo no)" "yes"

pgc_summary
