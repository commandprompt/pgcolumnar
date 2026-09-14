#!/usr/bin/env bash
#
# A column chunk's page_length is uint64 in the catalog and on the metadata
# struct, but both decode entry points cast the value stream to uint32. Adding
# 2^32 to page_length leaves the low 32 bits unchanged, so an index fetch
# silently reads the original stream and returns the row. A sequential scan
# already refuses (the chunk no longer fits its row group). The fetch path
# never had that check.
#
# Public seam: poison pgcolumnar.column_chunk.page_length, then SELECT through
# a btree. The property is the SQLSTATE, not a cost number.
#
# Independent of test/pytest/test_native_chunk_length_bound.py: same public
# seam, own fixture, own observations.
#
# Usage:  test/native_chunk_length_bound.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

q "CREATE EXTENSION IF NOT EXISTS pgcolumnar;" >/dev/null
q "CREATE TABLE clb (id int, t text) USING pgcolumnar;
   INSERT INTO clb SELECT g, 'v'||g FROM generate_series(1,5000) g;
   CREATE INDEX clb_id ON clb(id);
   ANALYZE clb;" >/dev/null
SID="$(q "SELECT pgcolumnar.get_storage_id('clb');")"

errcode() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-qtA -v VERBOSITY=sqlstate -c "$1" 2>&1 \
		| sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\).*/\1/p' | head -1
}
alive() { [ "$(q 'SELECT 1;')" = "1" ] && echo yes || echo no; }
plan_scan() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-Atq -c "SET enable_seqscan=off; SET enable_bitmapscan=off;
		         SET pgcolumnar.enable_custom_scan=off;
		         EXPLAIN (COSTS OFF) $1" 2>&1 \
		| grep -m1 -oE 'Index Scan|Index Only Scan|Bitmap Heap Scan|Custom Scan|Seq Scan'
}

FETCH="SET enable_seqscan=off; SET enable_bitmapscan=off; SET pgcolumnar.enable_custom_scan=off;"

check "premise: a point lookup uses the index, not a sequential columnar scan" \
	"$(plan_scan 'SELECT t FROM clb WHERE id = 1')" "Index Scan"
fetch_val() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-Atq -c "$1" 2>&1 | grep -v '^SET$' | tail -1
}
check "premise: that fetch returns the row" \
	"$(fetch_val "${FETCH} SELECT t FROM clb WHERE id = 1;")" "v1"

q "UPDATE pgcolumnar.column_chunk SET page_length = page_length + 4294967296
   WHERE storage_id = $SID AND column_index = 1;" >/dev/null

check "an index fetch of a chunk whose page_length is 2^32 too large is refused (XX001)" \
	"$(errcode "${FETCH} SELECT t FROM clb WHERE id = 1;")" "XX001"
check "backend survived the truncated-length fetch" "$(alive)" "yes"

# The sequential path already refuses a chunk that does not fit its row group.
# Pin that so a "fix" that only silences the fetch is not enough, and so this
# suite still means something if the fetch path starts using the same guard.
check "a sequential scan of the same poisoned chunk is refused (XX001)" \
	"$(errcode "SET pgcolumnar.enable_custom_scan=on; SET enable_indexscan=off;
	            SELECT t FROM clb WHERE id = 1;")" "XX001"
check "backend survived the sequential refusal" "$(alive)" "yes"

pgc_summary
