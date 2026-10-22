#!/usr/bin/env bash
#
# pgColumnar is not a logical decoding source (#754).
#
# docs/limitations.md states it: logical decoding reads the WAL records of heap
# tuples, columnar data reaches WAL as full-page images which carry no tuple
# structure, so no change to a columnar table is emitted. That sentence is the
# premise under a whole class of user expectations -- CDC, blue-green upgrades,
# selective logical replication -- and until this suite it was asserted nowhere.
#
# The only other logical-replication coverage in the tree, logical_subscriber.sh,
# tests the OPPOSITE direction: a heap publisher into a columnar subscriber. That
# direction works and is the shape most likely to bring users. Nothing tested
# the source direction, which is the one the limitation is about.
#
# WHY THE HEAP CONTROL IS THE LOAD-BEARING ARM. "Zero changes decoded" is what a
# working exclusion looks like and it is also what a broken slot, a mis-built
# plugin, or a query against the wrong slot looks like. The columnar arm cannot
# mean anything on its own. So a heap table of identical shape is written in the
# same transaction, through the same slot, and asserted to decode -- proving the
# instrument sees changes before the absence of one is believed.
#
# Usage:  test/logical_decoding_source.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# Logical decoding needs the WAL to carry enough to decode from, and the level
# cannot be raised without a restart.
export PGC_EXTRA_CONF="wal_level=logical
max_replication_slots=8
max_wal_senders=8"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# test_decoding is stock contrib, but it is not built by default in a source
# install. A missing dependency is an environment defect rather than a pass.
if [ ! -f "$("$PGC_PG_CONFIG" --pkglibdir)/test_decoding.so" ]; then
	# The message is the check's name, so it is a literal: the twin records the same one.
	pgc_skip "test_decoding" "test_decoding.so is not installed; build contrib/test_decoding"
fi

check "premise: the server is running at wal_level=logical" \
	"$(q "SHOW wal_level;")" "logical"

# Identical shape, identical rows, one heap and one columnar.
psql_run "CREATE TABLE heap_t (id int PRIMARY KEY, v text) USING heap;"
psql_run "CREATE TABLE col_t (id int PRIMARY KEY, v text) USING pgcolumnar;"

# THE SLOT IS CREATED BEFORE ANY ROW IS WRITTEN. A slot only carries changes
# made after it exists, so rows written first would be absent from BOTH arms and
# the heap control would fail for the wrong reason.
#
# Created with pg_create_logical_replication_slot rather than CREATE
# SUBSCRIPTION: a subscription pointed at its own cluster self-deadlocks, since
# it runs in a transaction and logical slot creation waits for running
# transactions including that one. It hangs with the walsender on
# Lock/transactionid rather than failing.
psql_run "SELECT pg_create_logical_replication_slot('pgc_754', 'test_decoding');"
check "premise: the slot exists and is logical" \
	"$(q "SELECT slot_type FROM pg_replication_slots WHERE slot_name = 'pgc_754';")" "logical"

psql_run "INSERT INTO heap_t SELECT g, 'h' || g FROM generate_series(1, 50) g;"
psql_run "INSERT INTO col_t  SELECT g, 'c' || g FROM generate_series(1, 50) g;"

check_num "premise: both tables hold the same 50 rows" \
	"$(q 'SELECT count(*) FROM heap_t;')" "$(q 'SELECT count(*) FROM col_t;')"

# Drain the slot once into a temp table; every count below reads that snapshot,
# so the arms cannot disagree about which changes they saw.
psql_run "CREATE TABLE decoded AS
          SELECT data FROM pg_logical_slot_get_changes('pgc_754', NULL, NULL);"

d() { q "SELECT count(*) FROM decoded WHERE data LIKE '%$1%';"; }

check "premise: the slot decoded something at all" \
	"$([ "$(q 'SELECT count(*) FROM decoded;')" -gt 0 ] && echo yes || echo no)" "yes"

# ------------------------------------------------------- the control, first

check_num "CONTROL: the heap table's 50 inserts ARE decoded" \
	"$(d 'table public.heap_t: INSERT')" "50"

# ------------------------------------------------------- the claim itself

check_num "a columnar table emits no decodable change" \
	"$(d 'table public.col_t:')" "0"

# And not because the rows are missing: they are in the table, and the heap
# control above proves the slot was live across the same window.
check_num "although the rows are there" "$(q 'SELECT count(*) FROM col_t;')" "50"

# ---------------------------------------- the slot is not silent, it is noisy

# The second half of the documented behaviour: a slot on a database holding
# columnar tables is not empty, it carries pgcolumnar's own catalog writes. A
# consumer sees internal churn it cannot interpret rather than nothing at all.
check "the slot carries pgcolumnar's internal catalog instead" \
	"$([ "$(d 'table pgcolumnar.')" -gt 0 ] && echo yes || echo no)" "yes"

# That churn scales with GROUPS and CHUNKS, not with rows, which is what makes
# it useless as a change stream: it does not carry the data and it does not even
# carry a row count. Asserted by writing 100x the rows into a second columnar
# table and showing the record count does not follow.
#
# THE SECOND SLOT GETS ITS OWN HEAP CONTROL. Without one, a slot that decoded
# nothing passed both checks below: "no decodable change" reads 0 and "not 100x
# the records" reads 0 < anything. Measured: with pgc_754b created after the
# insert, the slot carried 0 records and every check passed.
psql_run "CREATE TABLE col_big (id int PRIMARY KEY, v text) USING pgcolumnar;"
psql_run "CREATE TABLE heap_big (id int PRIMARY KEY, v text) USING heap;"
psql_run "SELECT pg_create_logical_replication_slot('pgc_754b', 'test_decoding');"
psql_run "INSERT INTO col_big SELECT g, 'c' || g FROM generate_series(1, 5000) g;"
psql_run "INSERT INTO heap_big SELECT g, 'h' || g FROM generate_series(1, 5000) g;"
psql_run "CREATE TABLE decoded_big AS
          SELECT data FROM pg_logical_slot_get_changes('pgc_754b', NULL, NULL);"
SMALL="$(d 'table pgcolumnar.')"
BIG="$(q "SELECT count(*) FROM decoded_big WHERE data LIKE '%table pgcolumnar.%';")"
check "premise: the 100x table really holds 100x the rows" \
	"$(q 'SELECT count(*) FROM col_big;')" "5000"
check_num "CONTROL: the second slot decodes the heap table's 5000 inserts" \
	"$(q "SELECT count(*) FROM decoded_big WHERE data LIKE '%table public.heap_big: INSERT%';")" "5000"
check_num "and it too emits no decodable change of its own" \
	"$(q "SELECT count(*) FROM decoded_big WHERE data LIKE '%table public.col_big:%';")" "0"
echo "-- internal pgcolumnar records: $SMALL for 50 rows, $BIG for 5000"
check "100x the rows does not give 100x the internal records" \
	"$([ "${BIG:-0}" -gt 0 ] 2>/dev/null && [ "$BIG" -lt $(( SMALL * 10 )) ] 2>/dev/null && echo yes ||
	   echo "no ($SMALL then $BIG; the second slot must carry some)")" "yes"

psql_run "SELECT pg_drop_replication_slot('pgc_754');"
psql_run "SELECT pg_drop_replication_slot('pgc_754b');"

pgc_summary
