#!/usr/bin/env bash
#
# pgColumnar streaming external-Parquet reads (Phase G).
#
# The reader used to palloc() the whole file and fread() it in one go, so a file
# of MaxAllocSize (1GB - 1) or more could not be read at all: the palloc raised
# "invalid memory alloc request size" before any Parquet logic ran. This suite
# pins the streaming model that replaced it, one surface at a time as they are
# ported.
#
# The oversized file here is sparse: a leading PAR1, a hole, then a real footer.
# It costs a few KB on disk but is over the old ceiling by logical size, which is
# what the allocation was sized from. Nothing in the footer path reads the hole.
# The page offsets in that footer do not describe real pages, so a value read of
# this file must fail cleanly rather than crash, which is also checked.
#
# Usage:  test/native_parquet_streaming.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

if ! python3 -c 'import pyarrow.parquet' 2>/dev/null; then
	pgc_skip pyarrow "pyarrow not available; streaming suite needs it"
fi

W="$PGC_WORKDIR"

# Every file is written by test/craft_parquet_streaming.py, shared with the twin; what
# each crafted file breaks, and why it is built that way, is described beside it there.
python3 "$(dirname "${BASH_SOURCE[0]}")/craft_parquet_streaming.py" "$W"
craft_rc=$?
case "$craft_rc" in
	0) ;;
	3 | 4)
		# An environment that cannot build the oversized sparse file is a box that
		# cannot gate this suite, not a major without the feature.
		pgc_skip sparse_file "could not build the oversized sparse file" ;;
	*)
		# A crafted file that was never written would make every refusal below pass
		# for the wrong reason ("could not open file" is still an error).
		check "crafted files were built" "build failed (exit $craft_rc)" "built"
		pgc_summary ;;
esac

# wait_up: a crash in one arm must fail that arm, not every reading made while the
# server recovers. survived reads the server log's count of killed backends as well
# as SELECT 1, since SELECT 1 answers again once crash recovery has finished: with a
# crash in place of the dictionary refusal, the bare SELECT 1 check passed.
wait_up() { local _i; for _i in $(seq 1 60); do [ "$(q 'SELECT 1' 2>/dev/null)" = 1 ] && return; sleep 0.5; done; }
crashes() {
	local _n
	_n="$(pgc_pg "grep -c 'terminated by signal' '$PGC_LOGFILE'" 2>/dev/null)"
	case "$_n" in '' | *[!0-9]*) ;; *) echo "$_n" ;; esac
}
survived() {	# survived BEFORE -> "1" when the server answers and no backend died since BEFORE
	local _up _now
	wait_up
	_up="$(q 'SELECT 1')"; _now="$(crashes)"
	if ! [[ "$1" =~ ^[0-9]+$ && "$_now" =~ ^[0-9]+$ ]]; then echo "no (crash count unreadable)"; return; fi
	if [ "$_up" = 1 ] && [ "$_now" = "$1" ]; then echo 1; return; fi
	echo "no ([$_up], $(( _now - $1 )) backend crashes)"
}
qw() { wait_up; q "$1"; }

# errtext QUERY -> the raised ERROR line, or "NO ERROR"
errtext() {
	local out
	wait_up
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" \
		-U postgres -d "$PGC_DB" -At -c "$1" 2>&1 | grep -m1 '^ERROR:')"
	[ -z "$out" ] && out="NO ERROR"
	echo "$out"
}

# decodes QUERY -> "OK" when QUERY raised the page loop's refusal ("could not decode
# Parquet column"), else the ERROR line it did raise, or "NO ERROR". Any ERROR used to
# count: a refusal from some other layer passed as the page loop's.
decodes() {
	local out
	out="$(errtext "$1")"
	case "$out" in
		"ERROR:  could not decode Parquet column "*) echo OK ;;
		*) echo "$out" ;;
	esac
}

CRASHES_BEFORE="$(crashes)"

# The headline: the footer path never allocates the file, so a file over the old
# ceiling describes itself. This FAILS on the pre-streaming code with
# "invalid memory alloc request size".
check "parquet_schema reads a file over the 1GB palloc ceiling" \
	"$(qw "SELECT count(*) FROM pgcolumnar.parquet_schema('$W/huge_sparse.parquet');")" "2"

check "parquet_schema reports the columns of the oversized file" \
	"$(qw "SELECT string_agg(column_name, ',' ORDER BY column_name) FROM pgcolumnar.parquet_schema('$W/huge_sparse.parquet');")" \
	"id,v"

# The same file's footer describes pages that are not there. A value read must
# report that, not crash the backend or return rows.
check "value read of the sparse file fails cleanly" \
	"$(decodes "SELECT * FROM pgcolumnar.read_parquet('$W/huge_sparse.parquet') AS t(id int, v text)")" "OK"
check "backend survived the failed read" "$(survived "$CRASHES_BEFORE")" "1"
CRASHES_MID="$(crashes)"

# The ordinary path is unchanged.
check "parquet_schema still reads a normal file" \
	"$(qw "SELECT count(*) FROM pgcolumnar.parquet_schema('$W/small.parquet');")" "2"
check "read_parquet still reads a normal file" \
	"$(qw "SELECT count(*) FROM pgcolumnar.read_parquet('$W/small.parquet') AS t(id int, v text);")" "3"

# ---- crafted files: the guards the page loop added -------------------------
# Each keeps a valid footer, so the file binds and the read starts, and breaks one
# thing the page loop must not trust (see test/craft_parquet_streaming.py).

# The timeout is the assertion for the first one: without the guard the page loop
# runs once per byte of a 1600MB zero region, so a regression hangs rather than
# errors, and the timeout is what turns that into a failure.
wait_up
zero_out="$(timeout 60 env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" \
	-U postgres -d "$PGC_DB" -At \
	-c "SELECT count(*) FROM pgcolumnar.read_parquet('$W/zero_pages.parquet') AS t(id int)" 2>&1)"
zero_rc=$?
case "$zero_rc:$zero_out" in
	124:*) zero_verdict="TIMED OUT (page loop made no progress)" ;;
	*"ERROR:  could not decode Parquet column "*) zero_verdict="CLEAN REJECT" ;;
	*ERROR*) zero_verdict="$(grep -m1 '^ERROR:' <<<"$zero_out")" ;;
	*) zero_verdict="NO ERROR: $zero_out" ;;
esac
check "zero-filled page area is rejected, not walked byte by byte" "$zero_verdict" "CLEAN REJECT"

# These two assert the error TEXT, not merely that something failed. Without
# their guards the file is still rejected, just for the wrong reason and after
# the damage: a declared size of 500MB is palloc'd and read past end of file, and
# a page offset of 2^40 is seek'd to. Both then fail in the I/O layer with
# "could not read", and on an assert-enabled build the offset case trips the
# assertion in pq_source_read first. Checking only that an error occurred would
# pass with the guards deleted, which is no test at all.
check "page size past end of file is rejected by the size bound" \
	"$(errtext "SELECT * FROM pgcolumnar.read_parquet('$W/bad_size.parquet') AS t(id int)")" \
	"ERROR:  could not decode Parquet column 0 in row group 0"
check "negative page offset is rejected by the offset check" \
	"$(errtext "SELECT * FROM pgcolumnar.read_parquet('$W/bad_offset.parquet') AS t(id int, v text)")" \
	"ERROR:  could not decode Parquet column 1 in row group 0"
check "unparseable page header is rejected" \
	"$(decodes "SELECT * FROM pgcolumnar.read_parquet('$W/noise_pages.parquet') AS t(id int)")" "OK"
check "a second dictionary page in a chunk is rejected" \
	"$(errtext "SELECT * FROM pgcolumnar.read_parquet('$W/two_dicts.parquet') AS t(id int)")" \
	"ERROR:  could not decode Parquet column 0 in row group 0"
check "v2 level lengths past the page are rejected" \
	"$(errtext "SELECT * FROM pgcolumnar.read_parquet('$W/v2_levels.parquet') AS t(id int)")" \
	"ERROR:  could not decode Parquet column 0 in row group 0"

# An all-null column is written with a dictionary page carrying zero entries,
# so a progress guard that rejects any page with no values rejects a file
# pyarrow produces routinely. Nothing else in the matrix covers an all-null
# Parquet read, which is why that regression was invisible.
check "all-null column (empty dictionary page) still reads" \
	"$(qw "SELECT count(*), count(c) FROM pgcolumnar.read_parquet('$W/allnull.parquet') AS t(c int);" | tr '|' ' ')" \
	"100 0"
check "backend survived the crafted page areas" "$(survived "$CRASHES_MID")" "1"

# the legitimate file the crafted ones were built from must still read
check "the uncompressed source file still reads" \
	"$(qw "SELECT count(*), sum(id) FROM pgcolumnar.read_parquet('$W/plain.parquet') AS t(id int);" | tr '|' ' ')" \
	"200 19900"

# ---- #620: the FDW streams row groups; a LIMIT decodes fewer ----------------
# The FDW no longer materializes the whole file into a tuplestore. It decodes one
# row group at a time, so a LIMIT that the executor satisfies early leaves the
# rest of the file undecoded. "Row Groups Decoded" is the work-done counter added
# for this: a full scan decodes every group; a LIMIT inside the first group
# decodes one; a LIMIT that spills into the second decodes two. On the eager
# model this counter did not exist and every group was decoded up front, so this
# is the streaming proof. A four-row-group file (1000 rows each).
STREAMP="$W/stream_groups.parquet"
psql_run "CREATE SERVER pqs FOREIGN DATA WRAPPER pgcolumnar_parquet;"
psql_run "CREATE FOREIGN TABLE fts (id int4, v int8) SERVER pqs OPTIONS (path '$STREAMP');"

decoded_for() {  # decoded_for SQL -> the Row Groups Decoded count for that plan
	q "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) $1" \
		| grep 'Row Groups Decoded' | grep -oE '[0-9]+' | head -1
}

check "streaming: full scan content is correct" \
	"$(qw 'SELECT count(*), sum(id), sum(v) FROM fts;' | tr '|' ' ')" \
	"4000 7998000 15996000"
check "streaming: a full scan decodes all four row groups" \
	"$(decoded_for 'SELECT * FROM fts')" "4"
check "streaming: LIMIT 10 (inside group 0) decodes one row group" \
	"$(decoded_for 'SELECT * FROM fts LIMIT 10')" "1"
check "streaming: LIMIT 1010 (into group 1) decodes two row groups" \
	"$(decoded_for 'SELECT * FROM fts LIMIT 1010')" "2"
# a rescan re-streams from the top rather than rewinding a buffer. Force a
# nested loop so fts is re-scanned once per outer row, and check the result is
# correct across rescans. outer2 = {1,2}; fts.id = o.k matches id 1 then id 2.
psql_run "CREATE TABLE outer2 (k int); INSERT INTO outer2 VALUES (1),(2);"
check "streaming: rescan under a nested loop is correct" \
	"$(q "SET enable_hashjoin=off; SET enable_mergejoin=off;
	      SELECT count(*) FROM outer2 o JOIN fts ON fts.id = o.k;" | tail -1)" "2"

rm -f "$W/huge_sparse.parquet" "$W/zero_pages.parquet" "$W/bad_size.parquet" \
	"$W/noise_pages.parquet" "$W/bad_offset.parquet" "$W/two_dicts.parquet" \
	"$W/v2_levels.parquet" "$W/allnull.parquet" "$W/stream_groups.parquet"
pgc_summary
