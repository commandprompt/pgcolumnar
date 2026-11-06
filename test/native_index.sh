#!/usr/bin/env bash
#
# pgColumnar native fetch-by-row-number (Phase D6a): index scan, bitmap scan and
# unique/primary-key enforcement on a native-format (PGCN v1) table. All route
# through PgColumnarReadRowByNumber, which before D6a had only a 2.2 path (index
# scans returned 0 rows, unique was silently unenforced). This suite proves parity
# with a heap mirror under a forced index path. Index-only scan (visibility map)
# is D6c; native projection storage is D6d; delete/update is D6b.
#
# Usage:  test/native_index.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

psql_run "CREATE TABLE h (id int PRIMARY KEY, k bigint, v text);"
psql_run "CREATE TABLE n (id int PRIMARY KEY, k bigint, v text) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('n', stripe_row_limit => 2048);"
GEN="SELECT g, (g * 10)::bigint, 'row-' || g FROM generate_series(1, 8000) g"
psql_run "INSERT INTO h $GEN;"
psql_run "INSERT INTO n $GEN;"

check "row count" "$(q 'SELECT count(*) FROM n;')" "8000"

# Force the index path in a single session (two -c, quiet so the SET tag is not
# printed), and confirm scalar results match the heap oracle. Each lookup fetches
# its rows by number through the index.
#
# enable_seqscan=off was all this set, and it leaves the columnar custom scan on.
# Measured (#1341): the range aggregate planned as Custom Scan (PgColumnarScan),
# so it fetched nothing by row number, and with the index fetch reading the
# neighbouring row it stayed green. The custom scan is off now, and index-only
# and bitmap scans with it, so every lookup is an Index Scan that fetches; each
# asserts that plan first.
FORCE_INDEX="SET enable_seqscan=off; SET enable_bitmapscan=off;
	SET enable_indexonlyscan=off; SET pgcolumnar.enable_custom_scan=off;"
iscan() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -q -At -c "$FORCE_INDEX" -c "$1" 2>/dev/null
}

for _ni in "point lookup:SELECT v FROM n WHERE id = 4242" \
		"range aggregate:SELECT count(*), sum(k), min(v), max(v) FROM n WHERE id BETWEEN 100 AND 400" \
		"IN lookup:SELECT count(*), sum(k) FROM n WHERE id IN (1,2000,4000,6000,8000)" \
		"lookup miss:SELECT count(*) FROM n WHERE id = 999999"; do
	check "premise: the index ${_ni%%:*} fetches through n_pkey" \
		"$(pgc_uses_row_fetch "$FORCE_INDEX" "${_ni#*:}")" yes
done

check "index point lookup" \
	"$(iscan 'SELECT v FROM n WHERE id = 4242;')" \
	"$(q     'SELECT v FROM h WHERE id = 4242;')"
check "index range aggregate" \
	"$(iscan 'SELECT count(*), sum(k), min(v), max(v) FROM n WHERE id BETWEEN 100 AND 400;')" \
	"$(q     'SELECT count(*), sum(k), min(v), max(v) FROM h WHERE id BETWEEN 100 AND 400;')"
check "index IN lookup" \
	"$(iscan 'SELECT count(*), sum(k) FROM n WHERE id IN (1,2000,4000,6000,8000);')" \
	"$(q     'SELECT count(*), sum(k) FROM h WHERE id IN (1,2000,4000,6000,8000);')"
check "index lookup miss returns nothing" "$(iscan 'SELECT count(*) FROM n WHERE id = 999999;')" "0"

# Primary-key / unique enforcement: a duplicate must be rejected. Before D6a the
# native index fetch found nothing, so duplicates were silently admitted.
check "duplicate id rejected" \
	"$(psql_run 'INSERT INTO n VALUES (4242, 1, '"'"'dup'"'"');' 2>&1 | grep -c -i 'duplicate key\|unique')" \
	"1"
check "row count unchanged after rejected dup" "$(q 'SELECT count(*) FROM n;')" "8000"

# A standalone UNIQUE index enforces too.
psql_run "CREATE TABLE nu (id int, k bigint) USING pgcolumnar;"
psql_run "CREATE UNIQUE INDEX nu_k ON nu (k);"
psql_run "INSERT INTO nu SELECT g, g FROM generate_series(1,1000) g;"
check "unique index rejects duplicate" \
	"$(psql_run 'INSERT INTO nu VALUES (99999, 500);' 2>&1 | grep -c -i 'duplicate key\|unique')" \
	"1"
check "unique index admits a new key" \
	"$(psql_run 'INSERT INTO nu VALUES (99999, 100000);' 2>&1 | grep -c -iE 'error|duplicate')" \
	"0"

pgc_summary
