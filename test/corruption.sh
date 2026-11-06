#!/usr/bin/env bash
#
# pgColumnar catalog-corruption robustness suite.
#
# The value-stream decoders and the reader trust lengths, counts, offsets, and
# codes stored in the pgcolumnar.* catalog. This suite corrupts those exact fields
# and asserts the extension raises a clean error (or degrades safely) and the
# backend survives, rather than reading or writing out of bounds. It complements
# test/hardening.sh (which mutates on-disk bytes) by mutating the catalog
# directly, which a superuser can do and which stands in for bit rot or a crafted
# format-2.0 file. Run on an assert-enabled build so an out-of-bounds access
# would trip an assertion or crash and fail the survival check.
#
# Usage:  test/corruption.sh [PG_CONFIG]
#
# Written fresh for pgColumnar; it does not reuse any upstream test file.

set -uo pipefail

. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# A BACKEND CRASH PASSED THESE CHECKS (#1341). `errors` said yes to any failure of
# psql, and a crash is one. `alive` then connected after the postmaster's crash
# recovery, so it passed whenever recovery finished first: with the bad-descriptor
# refusal replaced by abort(), the suite was 12/12 once `alive` waited 3s. So a
# refusal must now be a clean XX001, which a crash never prints, and `alive` also
# requires that no backend has died since the scenario started, read from the
# server log rather than from timing.
crashes() {
	# a number, or nothing when the log can't be read: grep -c prints 0 itself when
	# nothing matches (and exits 1), but nothing at all for a file it cannot open,
	# and a blind count must not read as "no crashes"
	local _n
	_n="$(pgc_pg "grep -c 'terminated by signal' '$PGC_LOGFILE'" 2>/dev/null)"
	case "$_n" in '' | *[!0-9]*) ;; *) echo "$_n" ;; esac
}
alive() {	# since this scenario's mkc, which notes the count
	local _up _now
	_up="$(q 'SELECT 1;')"; _now="$(crashes)"
	if ! [[ "$CRASHES_BEFORE" =~ ^[0-9]+$ && "$_now" =~ ^[0-9]+$ ]]; then
		echo "no (crash count unreadable)"
	elif [ "$_up" = "1" ] && [ "$_now" = "$CRASHES_BEFORE" ]; then
		echo yes
	else
		echo "no ($(( _now - CRASHES_BEFORE )) backend crashes)"
	fi
}
refused() {	# refused SQL -> the SQLSTATE of the ERROR it raised, or what it printed instead
	local out
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-qAtX -v VERBOSITY=verbose -c "$1" 2>&1)"
	sed -nE 's/^ERROR:  ([0-9A-Z]{5}): .*/\1/p' <<<"$out" | head -1 | grep . \
		|| echo "no error ($(head -1 <<<"$out"))"
}
sid() { q "SELECT pgcolumnar.get_storage_id('c');"; }
# corrupt FIELD "UPDATE ..." : run the corrupting UPDATE and assert it changed a
# catalog row. An UPDATE that matched nothing would leave the "must not crash"
# scenarios passing on a table nobody corrupted.
corrupt() {
	check "premise: the $1 corruption changed a catalog row" \
		"$(q "WITH u AS ($2 RETURNING 1) SELECT count(*) > 0 FROM u;")" "t"
}

mkc() {
	CRASHES_BEFORE="$(crashes)"
	psql_run "DROP TABLE IF EXISTS c;" >/dev/null 2>&1
	psql_run "CREATE TABLE c (id int, r bigint, t text) USING pgcolumnar;"
	# r random -> poorly compressible; id monotonic; t low-cardinality -> dictionary
	psql_run "INSERT INTO c SELECT g, (random()*9e18)::bigint, 'v'||(g%5)
	          FROM generate_series(1,20000) g;"
}

# The reader trusts the row_group and column_chunk lengths/counts and the encoding
# descriptor. Corrupt each and require a clean error (or safe degradation) with the
# backend surviving.
echo "-- row_group.byte_length corruption must error, not crash"
mkc
corrupt "byte_length" "UPDATE pgcolumnar.row_group SET byte_length = byte_length + 123456
          WHERE storage_id = $(sid)"
check "byte_length corruption errors"   "$(refused 'SELECT count(*), sum(r) FROM c;')" "XX001"
check "alive after byte_length"         "$(alive)" "yes"

echo "-- row_group.row_count corruption must error, not crash"
mkc
corrupt "row_count" "UPDATE pgcolumnar.row_group SET row_count = row_count + 100000
          WHERE storage_id = $(sid)"
check "row_count corruption errors"     "$(refused 'SELECT sum(r) FROM c;')" "XX001"
check "alive after row_count"           "$(alive)" "yes"

echo "-- column_chunk.page_length corruption must error, not crash"
mkc
corrupt "page_length" "UPDATE pgcolumnar.column_chunk SET page_length = page_length + 123456
          WHERE storage_id = $(sid)"
check "page_length corruption errors"   "$(refused 'SELECT sum(r) FROM c;')" "XX001"
check "alive after page_length"         "$(alive)" "yes"

echo "-- column_chunk.value_count corruption must not crash (degrades safely)"
mkc
corrupt "value_count" "UPDATE pgcolumnar.column_chunk SET value_count = value_count + 100000
          WHERE storage_id = $(sid)"
q "SELECT sum(r) FROM c;" >/dev/null 2>&1
check "alive after value_count"         "$(alive)" "yes"

echo "-- corrupt encoding descriptor must error, not crash"
mkc
corrupt "encoding_descriptor" "UPDATE pgcolumnar.column_chunk SET encoding_descriptor = '\\xffffffff'::bytea
          WHERE storage_id = $(sid) AND column_index = 1"
check "bad descriptor errors"           "$(refused 'SELECT * FROM c;')" "XX001"
check "alive after bad descriptor"      "$(alive)" "yes"

echo "-- column_index = -1 (negative index guard) must not crash"
mkc
corrupt "column_index" "UPDATE pgcolumnar.column_chunk SET column_index = -1
          WHERE storage_id = $(sid) AND column_index = 1"
q "SELECT count(*) FROM c;" >/dev/null 2>&1
check "alive after column_index=-1"     "$(alive)" "yes"

echo "-- corrupt bloom header (huge nbits, short buffer) is treated as no filter"
mkc
BLOOM_HIT="$(q 'SELECT r FROM c WHERE id = 7777;')"
corrupt "bloom filter" "UPDATE pgcolumnar.bloom SET filter = '\\x0000004006'::bytea
          WHERE storage_id = $(sid)"
check "corrupt bloom equality still correct" "$(q 'SELECT count(*) FROM c WHERE r = -999;')" "0"
# An absent value is 0 rows whether the corrupt filter is ignored or wrongly says
# "absent", so the arm above cannot see a false negative, which is the bloom's one
# way to lose rows (#1341). A value the table holds must still be found.
check "corrupt bloom: a value the table holds is still found" \
	"$(q "SELECT count(*) FROM c WHERE r = $BLOOM_HIT;")" "1"
check "alive after bloom corruption"         "$(alive)" "yes"

pgc_summary
