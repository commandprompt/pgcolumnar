#!/usr/bin/env bash
#
# pgColumnar: the FSST keep/drop verdict is cached with an age bound (#472).
#
# pgcolumnar_flush_row_group decides FSST keep/drop once per column per row
# group, and the decision cannot use a sample: on a training prefix FSST can
# look 24% worse while over the whole column it is 23% better, an inversion no
# margin would make safe. So PgColumnarFsstHelpsCompressed FSST-encodes the
# whole corpus and compresses it, only to answer yes or no. For a column whose
# data does not change character, that recomputes the same answer for every row
# group of the load.
#
# Measured on main before this change, 2,000,000 rows in 20 row groups: 2482 ms
# of a 5319 ms md5 load and 843 ms of a 2081 ms email load went to deciding, and
# the verdict was the same all 20 times. 41 to 47 percent of a text load
# re-deriving a constant.
#
# THE RISK IS NOT SPEED, IT IS SILENCE. A stale verdict does not corrupt
# anything; it compresses worse, correctly, and nothing would notice. So the
# headline checks here are byte equality of the stored chunks, not load time.
#
# Usage:  test/fsst_verdict_cache.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

ROWS=${PGC_FSST_ROWS:-600000}
NGROUPS=6                     # ROWS / stripe_row_limit below.
                              # NOT `GROUPS`: that is a bash special
                              # variable holding the caller's group ids,
                              # so assigning it silently does nothing and
                              # $((ROWS / NGROUPS)) divides by zero.

# Two corpora, chosen by measurement rather than by assumption. Six candidates
# were tried and five return HURTS (md5, urls, emails, JSON, log paths); prose
# is the only one that returns HELPS. A suite that tested only the common case
# would exercise one branch, and a wrongly cached HELPS verdict would sail
# through it.
PROSE="'the quick brown fox jumps over the lazy dog number ' || g || ' in the morning'"
MD5="md5(g::text)"

# Load <table> with <expr> at a given pgcolumnar.fsst_verdict_reuse, and leave
# the elapsed milliseconds in LOAD_MS.
LOAD_MS=0
load() {  # load <table> <expr> <reuse>
	local tbl="$1" expr="$2" reuse="$3" t0 t1
	psql_run "DROP TABLE IF EXISTS $tbl;
		CREATE TABLE $tbl (t text) USING pgcolumnar;
		SELECT pgcolumnar.set_options('$tbl', stripe_row_limit => $((ROWS / NGROUPS)));" >/dev/null
	t0=$(date +%s%N)
	psql_run "SET pgcolumnar.fsst_verdict_reuse = $reuse;
		INSERT INTO $tbl SELECT $expr FROM generate_series(1,$ROWS) g;" >/dev/null
	t1=$(date +%s%N)
	LOAD_MS=$(( (t1 - t0) / 1000000 ))
}

# The stored bytes, as a fingerprint that does not move with the LSN.
#
# A checksum of the relation file would be useless here: columnar pages live in
# the main fork and carry page headers, so two identical loads differ in their
# LSNs and every comparison below would fail for a reason that is not the one
# under test. The catalog records exactly what the encoder chose and how long
# the result was, which is the property this change must not alter.
fingerprint() {  # fingerprint <table>
	q "SELECT md5(string_agg(
			c.group_number || ':' || c.column_index || ':' || c.value_count || ':' ||
			encode(c.encoding_descriptor, 'hex') || ':' || c.block_codec || ':' ||
			c.page_length, '|' ORDER BY c.group_number, c.column_index))
		FROM pgcolumnar.column_chunk c
		JOIN pgcolumnar.storage s ON s.storage_id = c.storage_id
		WHERE s.relation_oid = '$1'::regclass;"
}

# How many vectors chose FSST. Lifted from test/write_fsst_compressed.sh, whose
# comment carries the trap: the descriptor is a 6-byte header (version, a
# flags byte -- a reserved zero before #1130 -- then the vector count as uint32)
# followed by that many 13-byte
# entries and then the chunk-shared symbol table, so entry i's type byte is at
# 6 + i*13 and the count must come from the header rather than from the length.
# Reading past the entries scores the symbol table's own bytes as encoding types.
fsst_vectors() {  # fsst_vectors <table>
	q "SELECT coalesce(sum(n), 0) FROM (
		SELECT (SELECT count(*)
				FROM generate_series(0,
					get_byte(c.encoding_descriptor, 2)
					+ get_byte(c.encoding_descriptor, 3) * 256
					+ get_byte(c.encoding_descriptor, 4) * 65536
					+ get_byte(c.encoding_descriptor, 5) * 16777216 - 1) i
				WHERE get_byte(c.encoding_descriptor, 6 + i * 13) = 8) AS n
		FROM pgcolumnar.column_chunk c
		JOIN pgcolumnar.storage s ON s.storage_id = c.storage_id
		WHERE s.relation_oid = '$1'::regclass) t;" | tail -1
}

# An empty count is not a zero count: `[ "" -gt 0 ]` errors instead of being
# false, and the check would then report on a number that was never read.
fsst_or_none() { local v; v="$(fsst_vectors "$1")"; pgc_is_number "$v" && echo "$v" || echo none; }

rows_of()  { q "SELECT count(*) FROM $1;"; }
# A decline is read with the table's row count: an empty table has no vector to
# choose FSST, so its 0 is not a decline (measured, with the cached load failed).
rows_fsst() { echo "$(rows_of "$1")/$(fsst_or_none "$1")"; }
# Values, carrying the row count, so two empty tables cannot hash-match.
counted_hash() { echo "$(q "SELECT count(*) FROM ($1) _c;"):$(pgc_set_hash "$1")"; }
groups_of() { q "SELECT count(DISTINCT group_number) FROM pgcolumnar.column_chunk c
		JOIN pgcolumnar.storage s ON s.storage_id = c.storage_id
		WHERE s.relation_oid = '$1'::regclass;"; }

# --- 0. the premises, before any comparison is believed -------------------------

load fv_prose_off "$PROSE" 0
check_num "premise: the prose fixture loaded" "$(rows_of fv_prose_off)" "$ROWS"
check "premise: and it spans several row groups, so caching has something to reuse" \
	"$([ "$(groups_of fv_prose_off)" -ge 3 ] && echo yes || echo "no ($(groups_of fv_prose_off))")" "yes"

load fv_md5_off "$MD5" 0
check_num "premise: the md5 fixture loaded" "$(rows_of fv_md5_off)" "$ROWS"

# The premise that stops this suite testing one branch twice. Without it, both
# fixtures could be taking the same path and every equality below would still
# hold, which is the shape of a green suite that measures nothing.
check "premise: the prose corpus KEEPS fsst, so the HELPS branch is exercised" \
	"$(v=$(fsst_or_none fv_prose_off); [ "$v" != none ] && [ "$v" -gt 0 ] && echo yes || echo "no ($v)")" "yes"
check "premise: and the md5 corpus DROPS it, so the two arms are different branches" \
	"$(rows_fsst fv_md5_off)" "$ROWS/0"

# --- 1. a cached verdict must not change one stored byte ------------------------

load fv_prose_on "$PROSE" 16
check_text "a reused HELPS verdict stores byte-identical chunks" \
	"$(fingerprint fv_prose_on)" "$(fingerprint fv_prose_off)"
check "and it still keeps fsst, rather than silently dropping it" \
	"$(v=$(fsst_or_none fv_prose_on); [ "$v" != none ] && [ "$v" -gt 0 ] && echo yes || echo "no ($v)")" "yes"

load fv_md5_on "$MD5" 16
check_text "a reused HURTS verdict stores byte-identical chunks" \
	"$(fingerprint fv_md5_on)" "$(fingerprint fv_md5_off)"
check "and it still declines fsst" "$(rows_fsst fv_md5_on)" "$ROWS/0"

# --- 2. the data, which is a different question from the bytes ------------------
#
# A compression regression is a cost; a decode failure is a defect. They must
# not share a check, so the values are compared independently of the chunks.
psql_run "DROP TABLE IF EXISTS fv_heap;
	CREATE TABLE fv_heap (t text);
	INSERT INTO fv_heap SELECT $PROSE FROM generate_series(1,$ROWS) g;" >/dev/null
check_text "the cached load returns the same values as heap" \
	"$(counted_hash 'SELECT t FROM fv_prose_on')" \
	"$ROWS:$(pgc_set_hash 'SELECT t FROM fv_heap')"

# --- 3. the age bound, tested rather than assumed -------------------------------
#
# A bound of one row group must re-decide every time, so it has to reproduce the
# uncached bytes exactly. This is what pins the mechanism: if the age were
# ignored, or off by one, this is the check that moves.
load fv_prose_one "$PROSE" 1
check_text "a reuse bound of one row group is byte-identical to no caching" \
	"$(fingerprint fv_prose_one)" "$(fingerprint fv_prose_off)"

# And a column that changes character mid-load. Caching CANNOT be byte-identical
# here, because within the window the stale verdict is used deliberately, so the
# assertion is the one that matters: a bounded cache notices the change and an
# unbounded one does not.
CHANGING="CASE WHEN g <= $((ROWS / 2)) THEN $PROSE ELSE $MD5 END"
load fv_chg_off "$CHANGING" 0
load fv_chg_bounded "$CHANGING" 2
load fv_chg_unbounded "$CHANGING" 1000000

chg_off="$(q "SELECT sum(page_length) FROM pgcolumnar.column_chunk c
	JOIN pgcolumnar.storage s ON s.storage_id = c.storage_id
	WHERE s.relation_oid = 'fv_chg_off'::regclass;")"
chg_bounded="$(q "SELECT sum(page_length) FROM pgcolumnar.column_chunk c
	JOIN pgcolumnar.storage s ON s.storage_id = c.storage_id
	WHERE s.relation_oid = 'fv_chg_bounded'::regclass;")"
chg_unbounded="$(q "SELECT sum(page_length) FROM pgcolumnar.column_chunk c
	JOIN pgcolumnar.storage s ON s.storage_id = c.storage_id
	WHERE s.relation_oid = 'fv_chg_unbounded'::regclass;")"
echo "-- changing column stored bytes: uncached=$chg_off bounded=$chg_bounded unbounded=$chg_unbounded"

check "premise: the three changing-column loads all produced bytes to compare" \
	"$(if pgc_is_number "$chg_off" && pgc_is_number "$chg_bounded" \
		&& pgc_is_number "$chg_unbounded"; then echo yes; else echo no; fi)" "yes"

# WHICH DIRECTION IS "BETTER" IS NOT THE OBVIOUS ONE, and assuming it is what
# this check got wrong first. A stale HELPS verdict can store FEWER bytes than
# the correct decision: PgColumnarFsstHelpsCompressed keeps FSST only when the
# compressed win clears pgcolumnar.fsst_min_gain_percent, so a marginal win is
# declined deliberately, and forcing FSST through a stale verdict takes that
# margin back. Measured here: uncached 5778575, unbounded 5628054. Smaller, and
# still the wrong call, because the margin exists to pay for decode.
#
# So the assertion is about tracking the uncached DECISION, not about size.
_d_bounded=$(( chg_bounded > chg_off ? chg_bounded - chg_off : chg_off - chg_bounded ))
_d_unbounded=$(( chg_unbounded > chg_off ? chg_unbounded - chg_off : chg_off - chg_unbounded ))
echo "-- distance from the uncached decision: bounded=$_d_bounded unbounded=$_d_unbounded"

# Without this the comparison below could hold with both distances zero, which
# is what a fixture that does not actually change character would produce.
check "an unbounded cache really does diverge on this fixture" \
	"$([ "$_d_unbounded" -gt 0 ] && echo yes || echo "no (the fixture does not change character)")" "yes"

check "a bounded cache tracks the change more closely than an unbounded one" \
	"$([ "$_d_bounded" -lt "$_d_unbounded" ] && echo yes || echo "no ($_d_bounded vs $_d_unbounded)")" "yes"

# Its VALUES, against a heap loaded from the same generator. This read only the
# row count, and an FSST decoder that wrote the wrong symbol passed it (measured).
psql_run "DROP TABLE IF EXISTS fv_chg_heap;
	CREATE TABLE fv_chg_heap (t text);
	INSERT INTO fv_chg_heap SELECT $CHANGING FROM generate_series(1,$ROWS) g;" >/dev/null
check_text "and the changing column still returns its values" \
	"$(counted_hash 'SELECT t FROM fv_chg_bounded')" \
	"$ROWS:$(pgc_set_hash 'SELECT t FROM fv_chg_heap')"

# --- 4. the win, in this suite rather than from a standalone probe --------------
#
# Deliberately loose. The measured saving on this shape is 40 percent and more,
# so a 10 percent bound fails only if the caching stopped working, not because
# the box is busy. A tighter bound would buy nothing and would flake.
load fv_time_off "$MD5" 0
t_off=$LOAD_MS
load fv_time_on "$MD5" 16
t_on=$LOAD_MS
echo "-- md5 load: uncached=${t_off} ms cached=${t_on} ms"
# Both loads must have loaded: a load that errored at once is fast too, and with
# the cached load failed this passed (measured).
check "caching the verdict makes the load measurably faster" \
	"$(if [ "$(rows_of fv_time_on)" != "$ROWS" ] || [ "$(rows_of fv_time_off)" != "$ROWS" ]; then
		echo "a timed load did not load ($(rows_of fv_time_on) / $(rows_of fv_time_off) rows)"
	   elif [ "$t_on" -lt "$(( t_off * 90 / 100 ))" ]; then echo yes
	   else echo "no ($t_on vs $t_off ms)"; fi)" \
	"yes"

pgc_summary
