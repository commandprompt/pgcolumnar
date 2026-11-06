#!/usr/bin/env bash
#
# On-disk format stability (#240, Phase 0).
#
# Two layers carry a version. The native data format stamps a major version into
# pgcolumnar.storage.format_version (PGCN v1); the physical metapage stamps
# versionMajor/versionMinor into block 0. Only the metapage version is checked on
# read -- PgColumnarReadMetapage rejects a version it does not understand -- so that
# guard is the thing standing between a future, incompatible layout and a silent
# misread of old bytes. This suite pins both stamps and proves the guard fires.
#
# It asserts three things:
#   1. the stamped format identifiers are the values this build writes, so an
#      accidental bump goes red here rather than shipping unnoticed;
#   2. a metapage version this build does not understand is REJECTED with a clean
#      error and a surviving backend -- not misread as valid data. A test-only
#      hook (pgcolumnar_debug_set_metapage_version, bound here rather than shipped,
#      like the gap suite's advance hook) plants the bad version;
#   3. within a version, a diverse-typed table round-trips byte-for-byte against a
#      heap mirror, so "same version" genuinely means "same data back".
#
# Usage:  test/native_format.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# Bind the internal test hook (deliberately not in the shipped catalog): it
# overwrites the metapage version so we can confirm the read-side guard.
psql_run "CREATE FUNCTION pgcolumnar.debug_set_metapage_version(regclass, int, int)
  RETURNS void AS 'pgcolumnar', 'pgcolumnar_debug_set_metapage_version'
  LANGUAGE C;"

# Error text (stderr) of a statement that fails with an ERROR; empty otherwise. Used
# to assert the guard rejects rather than misreads. stdout is discarded so only the
# diagnostic is captured. Only an ERROR counts (#1341): a guard that fell to a
# WARNING, or a backend that died with a FATAL, also writes to stderr, and taking
# that as the refusal kept these arms green under both (#1531 review).
err_of() {
	local out
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -v ON_ERROR_STOP=1 -Atq -c "$1" 2>&1 >/dev/null)"
	case "$out" in *"ERROR:"*) printf '%s\n' "$out" ;; esac
}
contains() { case "$1" in *"$2"*) echo yes;; *) echo no;; esac; }

# --- 1 & 3: stamped identifiers + diverse-type round-trip -------------------
# A pair with one column of every storage class that matters to the format:
# fixed-width, varlena text, numeric, float, bool, timestamp, and raw bytea.
make_pair "id bigint, a int, b text, c numeric(12,4), d float8, e bool, f timestamptz, g bytea"
load_pair "SELECT s,
                  (s * 7) % 1000,
                  md5(s::text),
                  (s * 1.25)::numeric(12,4),
                  s::float8 / 3,
                  (s % 2 = 0),
                  '2020-01-01'::timestamptz + (s || ' minutes')::interval,
                  decode(md5(s::text), 'hex')
           FROM generate_series(1, 5000) s"

sid="$(storage_id_of t_col)"
check "native format_version is 2" \
	"$(q "SELECT format_version FROM pgcolumnar.storage WHERE storage_id = $sid;")" "2"
check "native vector_length is 1024" \
	"$(q "SELECT vector_length FROM pgcolumnar.storage WHERE storage_id = $sid;")" "1024"

# Within a version, columnar must return exactly what heap returns for every type.
# The premise behind the ordered comparison below (test/lib.sh).
pgc_check_ordered_oracle
diff_query_ordered "diverse-type round-trip matches heap" "SELECT * FROM %T ORDER BY id"

# --- 2: unsupported metapage version is rejected, not misread ----------------
psql_run "CREATE TABLE bad (id int, v text) USING pgcolumnar;"
psql_run "INSERT INTO bad SELECT s, md5(s::text) FROM generate_series(1, 2000) s;"
check "table reads at the version this build wrote" "$(q 'SELECT count(*) FROM bad;')" "2000"

# Pin the metapage major -- the version that is actually enforced on read, and
# the one this suite exists to guard (format_version above is stamped but not
# checked). PgColumnarReadMetapage rejects on versionMajor != COLUMNAR_VERSION_MAJOR,
# so re-stamping the current major must be a no-op the read accepts. Bump
# COLUMNAR_VERSION_MAJOR by accident and this goes red; on a deliberate bump,
# change the 2 below in the same commit.
psql_run "SELECT pgcolumnar.debug_set_metapage_version('bad', 2, 2);"
check "the metapage major this build writes is still 2" \
	"$(q 'SELECT count(*) FROM bad;')" "2000"

# Plant a major version this build does not understand.
psql_run "SELECT pgcolumnar.debug_set_metapage_version('bad', 99, 0);"

emsg="$(err_of 'SELECT count(*) FROM bad;')"
check "a read of the bumped version fails (not silently misread)" \
	"$([ -n "$emsg" ] && echo yes || echo no)" "yes"
check "the failure is the format-version guard" \
	"$(contains "$emsg" 'unsupported columnar format version')" "yes"
check "the error names the offending version" \
	"$(contains "$emsg" '99.0')" "yes"
# The whole point of a clean rejection: the backend is still there afterwards.
check "backend survives the rejection" "$(q 'SELECT 1;')" "1"

# --- 4: native format_version is now enforced on read too (#240 decision) -----
# The metapage version above guards the physical layout; format_version is the
# independent data-format stamp. It used to be written and never read; it is now a
# read-side guard (PgColumnarCheckNativeFormatVersion at scan open), so a future
# PGCN version that keeps the metapage layout but changes the encoding is rejected
# rather than misread. A catalog UPDATE stands in for that future version -- the
# value is read from pgcolumnar.storage, so no on-disk bytes need forging.
psql_run "CREATE TABLE fv (id int, v text) USING pgcolumnar;"
psql_run "INSERT INTO fv SELECT s, md5(s::text) FROM generate_series(1, 2000) s;"
psql_run "CREATE INDEX fv_id ON fv (id);"
check "table reads at the native format_version this build writes" "$(q 'SELECT count(*) FROM fv;')" "2000"
fvsid="$(storage_id_of fv)"
psql_run "UPDATE pgcolumnar.storage SET format_version = 99 WHERE storage_id = $fvsid;"
# Three decode shapes must all reject. A seq scan opens a read state
# (PgColumnarBeginReadWithStorage); the zone-map-only aggregate answers from metadata
# without one (PgColumnarBeginAggScan); and an index-scan fetch of a non-key column
# decodes through the by-row-number fetch path, which reaches neither scan-open
# guard -- it is the case the first cut missed. All are keyed to the same catalog
# UPDATE standing in for a future format version.
fverr_scan="$(err_of 'SELECT v FROM fv LIMIT 1;')"
fverr_agg="$(err_of 'SELECT count(*) FROM fv;')"
# THE CUSTOM SCAN IS OFF HERE TOO (#1341), as in part 5 below: with it on, pgcolumnar's
# own node serves the index query, and with the fetch path's stamp check deleted the
# three arms on this query stayed green, refused by the scan-open guard instead.
fverr_idx="$(err_of 'SET pgcolumnar.enable_custom_scan=off; SET enable_seqscan=off; SET enable_bitmapscan=off; SELECT v FROM fv WHERE id = 42;')"
check "a seq-scan read of a future native format_version fails (not silently misread)" \
	"$([ -n "$fverr_scan" ] && echo yes || echo no)" "yes"
check "an aggregate over a future native format_version fails (not silently misread)" \
	"$([ -n "$fverr_agg" ] && echo yes || echo no)" "yes"
check "an index-scan fetch of a decoded column fails (not silently misread)" \
	"$([ -n "$fverr_idx" ] && echo yes || echo no)" "yes"
check "the failure is the native-format-version guard" \
	"$(contains "$fverr_idx" 'unsupported columnar native format version')" "yes"
check "the native-format error names the offending version" \
	"$(contains "$fverr_idx" '99')" "yes"
check "backend survives the native-format rejection" "$(q 'SELECT 1;')" "1"

# --- 5: the stamp is a RANGE, and an append raises it (#1137) ---------------
# 1.0-alpha5 writes chunks with no validity bitmap (#1130), which an alpha4
# binary misreads: some rows that hold a value come back NULL. So this build
# stamps 2, and alpha4 -- which refuses any stamp but 1 -- refuses the table at
# every decode entry point. This build still reads 1 (MIN_READABLE). Each arm
# below runs one query per entry point that checks the stamp:
#
#     seq scan        PgColumnarBeginReadWithStorage   SELECT v ... LIMIT
#     aggregate scan  PgColumnarBeginAggScan           count(*)
#     group agg scan  PgColumnarBeginGroupAggScan      GROUP BY
#     row fetch       pgcolumnar_fetch_row             index scan of a non-key column,
#                                                      custom scan OFF
#
# THE GROUP BY QUERY DOES NOT PROVE THE GROUP AGG SCAN'S OWN CHECK. It plans the
# group agg node, but with that node's stamp check removed no arm here goes red
# (#1531 review): an earlier check refuses the query first.
#
# THE CUSTOM SCAN HAS TO BE OFF FOR THE FOURTH, and that was measured rather
# than assumed. With it on, the index query is served by pgcolumnar's own scan
# node and never reaches pgcolumnar_fetch_row's decode: with that path's stamp
# check deleted outright, every arm here still passed.
#
# A catalog UPDATE stands in for the other build's stamp; the value is read from
# pgcolumnar.storage, so no bytes need forging.
sqlstate_of() {	# the SQLSTATE a statement fails with, or "ok"
	local out
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -v ON_ERROR_STOP=1 -v VERBOSITY=sqlstate -Atq -c "$1" 2>&1 >/dev/null)"
	if [ -z "$out" ]; then echo ok; else echo "$out" | sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\).*/\1/p' | head -1; fi
}
four_paths() {	# TABLE -> "scan agg group fetch", each a SQLSTATE or ok
	echo "$(sqlstate_of "SELECT v FROM $1 LIMIT 1;") \
$(sqlstate_of "SELECT count(*) FROM $1;") \
$(sqlstate_of "SELECT id % 3, count(*) FROM $1 GROUP BY 1;") \
$(sqlstate_of "SET pgcolumnar.enable_custom_scan=off; SET enable_seqscan=off; SET enable_bitmapscan=off; SELECT v FROM $1 WHERE id = 42;")"
}
stamp_of() { q "SELECT format_version FROM pgcolumnar.storage WHERE storage_id = $(storage_id_of "$1");"; }

psql_run "CREATE TABLE fr (id int, v text) USING pgcolumnar;"
psql_run "INSERT INTO fr SELECT s, md5(s::text) FROM generate_series(1, 2000) s;"
psql_run "CREATE INDEX fr_id ON fr (id);"
frsid="$(storage_id_of fr)"
check "premise: the fixture was stamped by this build" "$(stamp_of fr)" "2"

# What an alpha4 table looks like to this build.
psql_run "UPDATE pgcolumnar.storage SET format_version = 1 WHERE storage_id = $frsid;"
check "a table stamped 1 is read on all four paths" "$(four_paths fr)" "ok ok ok ok"
check "and returns its rows" "$(q 'SELECT count(*), sum(id) FROM fr;')" "2000|2001000"
check "reading a table stamped 1 does not raise its stamp" "$(stamp_of fr)" "1"

# Appending is what puts bitmap-less chunks in it, so appending moves the stamp.
psql_run "INSERT INTO fr SELECT s, md5(s::text) FROM generate_series(2001, 2100) s;"
check "appending to a table stamped 1 raises its stamp" "$(stamp_of fr)" "2"
check "and the table still reads whole" "$(q 'SELECT count(*), sum(id) FROM fr;')" "2100|2206050"

# Either side of the range is refused, everywhere, by SQLSTATE rather than by
# the absence of a wrong answer. 0A000 is feature_not_supported.
psql_run "UPDATE pgcolumnar.storage SET format_version = 3 WHERE storage_id = $frsid;"
check "a stamp above this build's is refused on all four paths" "$(four_paths fr)" "0A000 0A000 0A000 0A000"
psql_run "UPDATE pgcolumnar.storage SET format_version = 0 WHERE storage_id = $frsid;"
check "a stamp below MIN_READABLE is refused on all four paths" "$(four_paths fr)" "0A000 0A000 0A000 0A000"
check "backend survives the range rejections" "$(q 'SELECT 1;')" "1"

pgc_summary
