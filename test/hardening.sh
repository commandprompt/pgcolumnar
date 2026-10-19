#!/usr/bin/env bash
#
# pgColumnar hardening suite: corrupted-input robustness for the native encoding
# and bloom-filter metadata. An invalid encoding descriptor must raise a clean
# error, not crash the backend (a following query still works). A malformed bloom
# filter must be ignored (conservatively "may match"), so results stay correct.
#
# Usage:  test/hardening.sh [PG_CONFIG]
#
# Written fresh for pgColumnar; it does not reuse any upstream test file.

set -uo pipefail

. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

errcode() { # SQL -> the 5-char SQLSTATE it raises, or empty: a crash raises none
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-qtA -v VERBOSITY=sqlstate -c "$1" 2>&1 \
		| sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\).*/\1/p' | head -1
}
# A liveness probe alone passes once crash recovery finishes, so survival also
# requires that no backend has died since the scenario started, read from the
# server log rather than from timing.
crashes() {
	# grep -c prints 0 itself when nothing matches, and then exits 1
	pgc_pg "grep -c 'terminated by signal' '$PGC_LOGFILE'" 2>/dev/null || true
}
alive() {	# since the scenario's corruption, which notes the count
	# a crash puts the server through recovery: wait it out, so the next scenario
	# starts on a server that answers and one crash cannot fail the checks after it
	local _i
	for _i in $(seq 60); do [ "$(q 'SELECT 1;' 2>/dev/null)" = "1" ] && break; sleep 1; done
	[ "$(q 'SELECT 1;')" = "1" ] && [ "$(crashes)" = "$CRASHES_BEFORE" ] && echo yes \
		|| echo "no ($(( $(crashes) - CRASHES_BEFORE )) backend crashes)"
}

# An invalid encoding descriptor must raise a clean error, not crash the backend.
# "Clean" is read as its SQLSTATE: an empty result, which this arm used to take
# for an error, is also what a crash and a missing table give.
echo "-- invalid encoding descriptor"
make_pair "id int, v bigint"
load_pair "SELECT g, g*2 FROM generate_series(1,3000) g"
check "premise: the corrupt descriptor replaces one chunk's" \
	"$(q "UPDATE pgcolumnar.column_chunk SET encoding_descriptor = '\\\\xffffffff'::bytea
	      WHERE storage_id = pgcolumnar.get_storage_id('t_col') AND column_index = 1;")" \
	"UPDATE 1"
CRASHES_BEFORE="$(crashes)"
check "invalid descriptor raises error" "$(errcode "SELECT * FROM t_col;")" "XX001"
check "backend alive after invalid descriptor" "$(alive)" "yes"

# A malformed bloom filter must be ignored, keeping results correct.
echo "-- malformed bloom filter"
make_pair "id int, k bigint"
q "SELECT pgcolumnar.set_options('t_col', chunk_group_row_limit => 1000, stripe_row_limit => 20000);" >/dev/null
load_pair "SELECT g, ((g*2654435761)%100000)::bigint FROM generate_series(1,8000) g"
check "premise: the malformed bloom replaces k's filter" \
	"$(q "UPDATE pgcolumnar.bloom SET filter = '\\\\x00'::bytea
	      WHERE storage_id = pgcolumnar.get_storage_id('t_col') AND column_index = 1;")" \
	"UPDATE 1"
CRASHES_BEFORE="$(crashes)"
diff_query "corrupt bloom present" "SELECT id FROM %T WHERE k = ((7*2654435761)%100000)::bigint"
diff_query "corrupt bloom absent"  "SELECT count(*) FROM %T WHERE k = 424242424"
check "backend alive after corrupt bloom" "$(alive)" "yes"

pgc_summary
