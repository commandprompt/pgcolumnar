#!/usr/bin/env bash
#
# SQL TRUNCATE must not leave the old storage's catalog rows behind.
#
# DROP deletes metadata for the relation's current storage id. TRUNCATE
# installs a new relfilenode and a new storage id, and used to leave the old
# catalog rows keyed by the retired id. DROP after that only cleaned the new
# id, so each truncate-and-reload cycle leaked one storage's worth of
# row_group, column_chunk, zone_map and bloom rows.
#
# Measured before the fix, one insert-then-TRUNCATE left the catalog at the
# post-insert counts instead of the empty baseline; a second insert doubled
# them. Counting rather than sampling is deliberate, as in drop_cleanup.sh:
# a leak of one row per truncate is exactly the size of this defect.
#
# Usage:  test/truncate_cleanup.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

snapshot() {
	q "SELECT (SELECT count(*) FROM pgcolumnar.storage) || '/' ||
		(SELECT count(*) FROM pgcolumnar.projection) || '/' ||
		(SELECT count(*) FROM pgcolumnar.row_group) || '/' ||
		(SELECT count(*) FROM pgcolumnar.column_chunk) || '/' ||
		(SELECT count(*) FROM pgcolumnar.zone_map) || '/' ||
		(SELECT count(*) FROM pgcolumnar.bloom) || '/' ||
		(SELECT count(*) FROM pgcolumnar.delete_vector);" | tail -1
}

base="$(snapshot)"

psql_run "CREATE TABLE tcl_plain (id int, v text) USING pgcolumnar;
	INSERT INTO tcl_plain SELECT g, 'x' || g FROM generate_series(1, 5000) g;" >/dev/null
grew="$(snapshot)"
check "insert did add metadata" \
	"$(awk -v a="$base" -v b="$grew" 'BEGIN { print (a == b) ? "no" : "yes" }')" "yes"

psql_run "TRUNCATE tcl_plain;" >/dev/null
check "TRUNCATE returns the catalog to the empty baseline" "$(snapshot)" "$base"
check_num "TRUNCATE leaves no rows" "$(q 'SELECT count(*) FROM tcl_plain')" "0"

psql_run "INSERT INTO tcl_plain SELECT g, 'x' || g FROM generate_series(1, 5000) g;" >/dev/null
psql_run "TRUNCATE tcl_plain;" >/dev/null
psql_run "INSERT INTO tcl_plain SELECT g, 'x' || g FROM generate_series(1, 5000) g;" >/dev/null
psql_run "TRUNCATE tcl_plain;" >/dev/null
check "three truncate-and-reload cycles still sit at the baseline" "$(snapshot)" "$base"

psql_run "DROP TABLE tcl_plain;" >/dev/null
check "DROP after TRUNCATE leaves nothing" "$(snapshot)" "$base"

# a projection has its own storage id; TRUNCATE used to leak that too
base="$(snapshot)"
psql_run "CREATE TABLE tcl_proj (id int, a int, b text) USING pgcolumnar;" >/dev/null
psql_run "SELECT pgcolumnar.add_projection('tcl_proj','tcl_p',ARRAY['a','b'],ARRAY['a']);" >/dev/null
psql_run "INSERT INTO tcl_proj SELECT g, g % 50, 'b' || g FROM generate_series(1, 5000) g;" >/dev/null
grew="$(snapshot)"
check "the projection did add metadata" \
	"$(awk -v a="$base" -v b="$grew" 'BEGIN { print (a == b) ? "no" : "yes" }')" "yes"

psql_run "TRUNCATE tcl_proj;" >/dev/null
check "TRUNCATE takes the projection's retired storage with it" "$(snapshot)" "$base"

psql_run "DROP TABLE tcl_proj;" >/dev/null
check "DROP of the truncated projected table leaves nothing" "$(snapshot)" "$base"

# repetition, which is where an unbounded leak shows
base="$(snapshot)"
for i in $(seq 1 10); do
	psql_run "CREATE TABLE tcl_rep (id int, v text) USING pgcolumnar;
		INSERT INTO tcl_rep SELECT g, 'x' || g FROM generate_series(1, 2000) g;
		TRUNCATE tcl_rep;
		DROP TABLE tcl_rep;" >/dev/null
done
check "ten create-insert-truncate-drop cycles leave nothing" "$(snapshot)" "$base"

# ---- a transaction that writes, truncates and writes again --------------------
#
# Retiring the old storage's catalog rows removed the only thing that made a
# stale cached write state safe. The state holds the storage id the rows were
# just deleted from; the second INSERT reuses it and flushes into a storage the
# relation no longer reads. Before the rows were deleted that flush collided
# with them on the primary key and the transaction ERRORed, so the hazard was
# loud. Deleting them made it silent: the transaction COMMITTED and left the
# table EMPTY.
#
# Measured on three trees, PG 17.10, each statement on its own -c so TRUNCATE
# does not take core's in-place same-transaction path:
#
#   main 53224e4              rc=1  1..10  n=10   loud failure, rolls back
#   this PR before the fix    rc=0         n=0    COMMITTED, table empty
#   this PR with the fix      rc=0  21..30 n=10   commits, keeps the new rows
#
# main is not the standard to restore here. This transaction is ordinary SQL and
# it never worked; the arm asserts what it should do, which is commit and hold
# exactly the rows written after the TRUNCATE.

psql_run "CREATE TABLE tc_txn (id int) USING pgcolumnar;"
psql_run "INSERT INTO tc_txn SELECT g FROM generate_series(1,10) g;"
check "premise: the table holds its first ten rows" \
	"$(q 'SELECT count(*) FROM tc_txn')" "10"

tc_txn_rc=0
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
	-v ON_ERROR_STOP=1 -At \
	-c "BEGIN;" \
	-c "INSERT INTO tc_txn SELECT g FROM generate_series(11,20) g;" \
	-c "TRUNCATE tc_txn;" \
	-c "INSERT INTO tc_txn SELECT g FROM generate_series(21,30) g;" \
	-c "COMMIT;" >/dev/null 2>&1 || tc_txn_rc=$?

# One arm over the exit status AND the surviving rows together. Split apart, a
# commit that leaves nothing passes the status half, and 1..10 (the TRUNCATE
# lost) passes a bare count of 10.
check "INSERT, TRUNCATE, INSERT in one transaction commits and keeps the new rows" \
	"rc=$tc_txn_rc $(q "SELECT coalesce(min(id),-1) || '..' || coalesce(max(id),-1) ||
	                    ' n=' || count(*) FROM tc_txn")" \
	"rc=0 21..30 n=10"

psql_run "DROP TABLE tc_txn;"

# ---- a rewrite that is not a truncate must not lose anything -----------------
#
# The retire runs from relation_set_new_filelocator, which every rewrite calls,
# not only TRUNCATE. If the guard ever misfires on a plain rewrite it takes the
# rows with it, and drop_cleanup rewrites nothing, so nothing else would notice.

psql_run "CREATE TABLE tc_rw (id int, v text) USING pgcolumnar;"
psql_run "INSERT INTO tc_rw SELECT g, 'v' || g FROM generate_series(1,5000) g;"
check "premise: five thousand rows before the rewrite" \
	"$(q 'SELECT count(*) FROM tc_rw')" "5000"
psql_run "ALTER TABLE tc_rw ALTER COLUMN v TYPE varchar(64);"
check "a full rewrite keeps every row, and they are still readable" \
	"$(q "SELECT count(*) || '/' || count(v) || '/' || max(id) FROM tc_rw")" \
	"5000/5000/5000"
psql_run "DROP TABLE tc_rw;"

pgc_summary
