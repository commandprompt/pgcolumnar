#!/usr/bin/env bash
#
# pgColumnar native writer: a columnar table writes the native format (PGCN v1).
# This validates the writer by its catalog output: the pgcolumnar.storage /
# row_group / column_chunk rows it produces, and that dropping the table clears
# them. It does not SELECT data from the table (see native_roundtrip.sh).
#
# Usage:  test/native_writer.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# A columnar table with a small row-group limit so several row groups are written.
psql_run "CREATE TABLE nw (id int, v text) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('nw', stripe_row_limit => 1000);"
psql_run "INSERT INTO nw SELECT g, CASE WHEN g % 4 = 0 THEN NULL ELSE 'v'||g END FROM generate_series(1, 5000) g;"

SID="$(q "SELECT pgcolumnar.get_storage_id('nw');")"
check "native storage row written" \
	"$(q "SELECT count(*) FROM pgcolumnar.storage WHERE storage_id = $SID;")" "1"
check "native format version recorded" \
	"$(q "SELECT format_version FROM pgcolumnar.storage WHERE storage_id = $SID;")" "2"
check "native vector length recorded" \
	"$(q "SELECT vector_length FROM pgcolumnar.storage WHERE storage_id = $SID;")" "1024"
check "native row groups (5000 rows / 1000 limit)" \
	"$(q "SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = $SID;")" "5"
check "native row-group row total" \
	"$(q "SELECT sum(row_count) FROM pgcolumnar.row_group WHERE storage_id = $SID;")" "5000"
check "native column chunks (5 groups x 2 cols)" \
	"$(q "SELECT count(*) FROM pgcolumnar.column_chunk WHERE storage_id = $SID;")" "10"
check "native column-chunk value total" \
	"$(q "SELECT sum(value_count) FROM pgcolumnar.column_chunk WHERE storage_id = $SID;")" "10000"

# Drop cleanup removes the native catalog rows for the storage.
psql_run "DROP TABLE nw;"
check "drop clears native storage row" \
	"$(q "SELECT count(*) FROM pgcolumnar.storage WHERE storage_id = $SID;")" "0"
check "drop clears native row groups" \
	"$(q "SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = $SID;")" "0"
check "drop clears native column chunks" \
	"$(q "SELECT count(*) FROM pgcolumnar.column_chunk WHERE storage_id = $SID;")" "0"


# Row numbers run out (#2073). A row number maps to an item pointer, and a block
# number holds 32 bits, so there is a largest row number. Each transaction
# reserves stripe_row_limit row numbers, so at the largest stripe_row_limit
# (2147483647) the 583rd single-row transaction would need row numbers past
# that largest one. The writer must refuse it with 54000 (program limit
# exceeded) and keep the 582 rows before it. Before the fix it wrapped the
# block number: the row got an item pointer below the rows before it, and an
# index scan no longer found it.
psql_run "CREATE TABLE nw_rn (id int, v text) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('nw_rn', stripe_row_limit => 2147483647);"
psql_run "CREATE INDEX nw_rn_id ON nw_rn (id);"
RN_STATE="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
	-d "$PGC_DB" -At -v VERBOSITY=sqlstate -c \
	"DO \$\$ BEGIN FOR i IN 1..600 LOOP INSERT INTO nw_rn VALUES (i, 'v' || i); COMMIT; END LOOP; END \$\$;" 2>&1 \
	| sed -n 's/^.*ERROR:[[:space:]]*\([0-9A-Z]\{5\}\).*$/\1/p' | head -1)"
check "a reservation past the largest row number is refused (54000)" "$RN_STATE" "54000"
check "the rows committed before the refusal are kept" \
	"$(q "SELECT count(*) FROM nw_rn;")" "582"
check "no committed row's item pointer wraps below the row before it" \
	"$(q "SELECT count(*) FROM (SELECT ctid, lag(ctid) OVER (ORDER BY id) AS p FROM nw_rn) s WHERE ctid <= p;")" "0"
check "an index scan finds every committed row" \
	"$(q "SET pgcolumnar.enable_custom_scan = off; SET enable_seqscan = off; SET enable_bitmapscan = off;
	      SET pgcolumnar.enable_index_fetch_penalty = off; SELECT count(*) FROM nw_rn WHERE id > 0;" | tail -1)" "582"
psql_run "SELECT pgcolumnar.vacuum('nw_rn');"
psql_run "INSERT INTO nw_rn VALUES (601, 'after');"
check "pgcolumnar.vacuum renumbers the rows, so inserts work again" \
	"$(q "SELECT count(*) || '|' || count(*) FILTER (WHERE id = 601) FROM nw_rn;")" "583|1"
psql_run "DROP TABLE nw_rn;"

pgc_summary
