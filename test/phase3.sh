#!/usr/bin/env bash
#
# pgColumnar phase 3 test: update and delete via the delete vector, snapshot
# visibility and same-transaction read-your-writes, transaction and savepoint
# rollback, and temporary columnar tables. Also re-runs the phase 1 and phase 2
# regression checks.
#
# Builds and installs the extension, spins up a throwaway PostgreSQL cluster as
# the postgres OS user, and exercises the phase 3 feature set. Written fresh for
# pgColumnar; it does not reuse any upstream test file or expected-output file.
#
# Usage:
#   test/phase3.sh [PG_CONFIG]
#
# PG_CONFIG defaults to /usr/local/pg17/bin/pg_config. Run as a user that may
# "runuser -u postgres" (e.g. root) when the current user is not postgres.

set -euo pipefail

# lib.sh for the check vocabulary (#965). It sources portlib.sh itself
# (lib.sh:110), so this is a superset of what was here, and its top level is
# assignments and function definitions only, so sourcing it starts nothing.
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

PG_CONFIG="${1:-/usr/local/pg17/bin/pg_config}"

# WHICH MAJOR THIS SUITE RAN ON (#1121, the wider half of #1109). `pgc_record`
# writes `${PGC_MAJOR:-unknown}`, and PGC_MAJOR is set inside `pgc_setup` -- which
# this suite does not call, deliberately. Without this line every record it emits
# says `unknown`, and a ledger row claiming `unknown` matches no run, so none of
# these checks could ever be seeded or matched again.
#
# The runner passes the pg_config as $1 to EVERY suite, including those that need
# no cluster, so it is available here. `pgc_major_of` returns empty on a path it
# cannot run, which degrades to exactly today's `unknown` rather than to a WRONG
# major -- a guessed major would seed a row claiming a major the check was never
# observed on, which is worse than saying nothing.
PGC_MAJOR="$(pgc_major_of "$PG_CONFIG")"
BINDIR="$("$PG_CONFIG" --bindir)"
PORT="${PGC_PORT:-$(pgc_pick_port)}"
SRCDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

WORKDIR="$(mktemp -d /tmp/pgcolumnar-phase3.XXXXXX)"
PGDATA="$WORKDIR/data"
LOGFILE="$WORKDIR/server.log"

echo "== pgColumnar phase 3 test =="
echo "PG_CONFIG=$PG_CONFIG"
echo "workdir=$WORKDIR"

# The matrix runner installs the extension once per version and sets
# PGC_SKIP_BUILD; skip the redundant per-suite build+install then, which also
# avoids racing a concurrent suite's install into the same lib dir.
if [ -z "${PGC_SKIP_BUILD:-}" ]; then
	# THE HARNESS BUILDER, NOT A HAND-ROLLED make (#1220). Three guarantees come
	# with it and none of them were here: it asks the OBJECTS which major built
	# them (#1219), it keeps the build stamp (#536), and it refuses to run when
	# the build or the install failed instead of reporting checks against the
	# previously installed .so -- which the two discarded exit statuses below it
	# used to do silently.
	pgc_build_and_install "$SRCDIR" "$PG_CONFIG" "$PGC_MAJOR" || exit 1
fi

if [ "$(id -u)" = "0" ]; then
	RUNPG=(runuser -u postgres --)
	chown -R postgres "$WORKDIR"
else
	RUNPG=(env)
fi

run_pg() { "${RUNPG[@]}" env PATH="$BINDIR:$PATH" bash -lc "$1"; }

cleanup() {
	run_pg "pg_ctl -D '$PGDATA' stop -m immediate -w" >/dev/null 2>&1 || true
	rm -rf "$WORKDIR"
}
trap cleanup EXIT

echo "-- initdb"
run_pg "initdb -D '$PGDATA' -A trust" >/dev/null 2>&1
run_pg "echo \"port=$PORT\" >> '$PGDATA/postgresql.conf'"
run_pg "echo \"shared_preload_libraries='pgcolumnar'\" >> '$PGDATA/postgresql.conf'"
echo "-- start"
run_pg "pg_ctl -D '$PGDATA' -l '$LOGFILE' start -w" >/dev/null
run_pg "createdb -p $PORT p3"

# -qAt: quiet (no command tags), unaligned, tuples-only, so a value is exactly
# the query output. The SQL is fed on psql's stdin rather than through -c: a
# multi-statement -c string prints only the last command's result before
# PostgreSQL 14, whereas reading from stdin prints every statement's result on
# all supported majors. Statements joined with ';' still run in one session
# (and, with explicit BEGIN/COMMIT, one transaction), which is what the
# read-your-writes and savepoint checks below rely on.
PSQL="psql -p $PORT -d p3 -qAt -v ON_ERROR_STOP=1"
q() { run_pg "$PSQL" <<<"$1"; }
qq() { run_pg "$PSQL" <<<"$1" | tr '\n' ',' ; }

fail=0
# RECORDS RATHER THAN ONLY PRINTING (#965). This suite emitted human PASS lines
# and no RESULT records, so every mechanism built on the record vocabulary -- the
# ledger, the census, checks_never_observed_red, the red-observation record,
# duplicate-name detection -- was blind to all of them. The ledger did not merely
# return nothing on this log, it REFUSED it: "no RESULT records, so there is
# nothing to reconcile".
#
# lib.sh's own `check` (#1341), as smoke.sh did. This suite used to define its own
# `check` around pgc_record, to keep a `: $got` suffix on PASS lines. A PASS
# line's value is the wanted value, which the call already states, and a FAIL line
# carries both either way. A suite-local recorder is also what the pytest twin's
# grading cannot read (test_compare_to_bash's seed arm), so the shared one is used
# and `fail` is taken from lib.sh's own PGC_FAIL before the verdict. The verdict
# line stays: under `set -euo pipefail` a failing command aborts the suite, and
# the verdict is what distinguishes finished from stopped.
#
# The `checks run:` line is READ BY NOTHING YET. The matrix gates reconciliation on
# the ACCOUNTING line via `pgc_log_shows_accounting`, and this suite emits none
# because it emits no accounting line. The line is still correct and wanted --
# it is the total the records reconcile against -- so the remaining step is a gate
# flip rather than new work (@jdatcmd, #969 review).

q "CREATE EXTENSION pgcolumnar;" >/dev/null

# ---------------------------------------------------------------------------
# Phase 1/2 regression: count and filter still work.
# ---------------------------------------------------------------------------
echo "-- phase 1/2 regression"
q "CREATE TABLE reg (a int, b text) USING pgcolumnar;" >/dev/null
q "INSERT INTO reg SELECT g, g::text FROM generate_series(1,50000) g;" >/dev/null
check "reg count"        "$(q 'SELECT count(*) FROM reg;')"              "50000"
check "reg filter a<100" "$(q 'SELECT count(*) FROM reg WHERE a < 100;')" "99"
q "DROP TABLE reg;" >/dev/null

# ---------------------------------------------------------------------------
# Same-transaction read-your-writes: rows inserted earlier in a transaction are
# visible to a later scan in the same transaction (the phase 1 gap, now closed).
# ---------------------------------------------------------------------------
echo "-- read-your-writes"
check "ryw count" \
	"$(q "BEGIN; CREATE TABLE ryw (a int) USING pgcolumnar; INSERT INTO ryw SELECT g FROM generate_series(1,1000) g; SELECT count(*) FROM ryw; COMMIT;")" \
	"1000"
check "ryw sum" \
	"$(q 'BEGIN; INSERT INTO ryw VALUES (100000); SELECT sum(a) FROM ryw; COMMIT;')" \
	"600500"
check "ryw persisted" "$(q 'SELECT count(*) FROM ryw;')" "1001"
q "DROP TABLE ryw;" >/dev/null

# ---------------------------------------------------------------------------
# DELETE marks rows in the delete vector; scans return exactly the survivors.
# 25000 rows span three chunk groups (10000/10000/5000), so the deleted range
# below straddles a chunk-group boundary.
# ---------------------------------------------------------------------------
echo "-- delete"
q "CREATE TABLE d (id int, v text) USING pgcolumnar;" >/dev/null
q "INSERT INTO d SELECT g, 'r'||g FROM generate_series(1,25000) g;" >/dev/null
# AND IT DOES STRADDLE ONE (#1341). Nothing read the boundaries, so with a
# 16384-row vector every check below passed on a delete that straddled nothing.
# Each vector's maximum id, decoded from the zone map's little-endian int4.
check "the deleted range straddles vectors 0 and 1" "$(q "SELECT string_agg(((get_byte(maximum,3)::bigint<<24)|(get_byte(maximum,2)<<16)|(get_byte(maximum,1)<<8)|get_byte(maximum,0))::text, chr(44) ORDER BY vector_index) FROM pgcolumnar.zone_map WHERE storage_id = pgcolumnar.get_storage_id('d') AND column_index = 0 AND vector_index >= 0;")" "10000,20000,25000"
q "DELETE FROM d WHERE id BETWEEN 9995 AND 10005;" >/dev/null
check "delete count"       "$(q 'SELECT count(*) FROM d;')"                       "24989"
check "delete gone"        "$(q 'SELECT count(*) FROM d WHERE id BETWEEN 9995 AND 10005;')" "0"
check "delete neighbors"   "$(qq 'SELECT id FROM d WHERE id IN (9994,10006) ORDER BY id;')" "9994,10006,"
check "delete boundaries"  "$(q 'SELECT min(id)||chr(47)||max(id) FROM d;')"      "1/25000"
# a second, whole-chunk-group delete
q "DELETE FROM d WHERE id > 20000;" >/dev/null
check "delete tail count"  "$(q 'SELECT count(*) FROM d;')"                       "19989"
check "delete tail max"    "$(q 'SELECT max(id) FROM d;')"                        "20000"
# delete_vector catalog reflects the deletions
check "delete_vector rows>0"    "$(q 'SELECT (count(*) > 0)::int FROM pgcolumnar.delete_vector;')" "1"
check "delete_vector deleted"   "$(q 'SELECT sum(deleted_count) FROM pgcolumnar.delete_vector;')"   "5011"
q "DROP TABLE d;" >/dev/null

# ---------------------------------------------------------------------------
# UPDATE is delete-plus-insert: the new value shows, the old row disappears,
# and the row count is unchanged. Updating the key column works too.
# ---------------------------------------------------------------------------
echo "-- update"
q "CREATE TABLE u (id int, v text) USING pgcolumnar;" >/dev/null
q "INSERT INTO u SELECT g, 'r'||g FROM generate_series(1,5000) g;" >/dev/null
q "UPDATE u SET v='changed' WHERE id = 2500;" >/dev/null
check "update value"     "$(q 'SELECT v FROM u WHERE id = 2500;')"   "changed"
check "update one row"   "$(q "SELECT count(*) FROM u WHERE v = 'changed';")" "1"
check "update count"     "$(q 'SELECT count(*) FROM u;')"            "5000"
q "UPDATE u SET id = id + 100000 WHERE id <= 10;" >/dev/null
check "update key gone"  "$(q 'SELECT count(*) FROM u WHERE id <= 10;')"      "0"
check "update key new"   "$(q 'SELECT count(*) FROM u WHERE id > 100000;')"   "10"
check "update key total" "$(q 'SELECT count(*) FROM u;')"            "5000"
# update every row (bulk), verify no old survivors and the suffix is present
q "UPDATE u SET v = v || 'ZZ';" >/dev/null
check "bulk update rows"  "$(q 'SELECT count(*) FROM u;')"                  "5000"
check "bulk update sfx"   "$(q "SELECT count(*) FROM u WHERE right(v,2) = 'ZZ';")" "5000"
q "DROP TABLE u;" >/dev/null

# ---------------------------------------------------------------------------
# ROLLBACK of an inserting and deleting transaction leaves the table unchanged.
# ---------------------------------------------------------------------------
echo "-- rollback"
q "CREATE TABLE rb (a int) USING pgcolumnar;" >/dev/null
q "INSERT INTO rb SELECT g FROM generate_series(1,100) g;" >/dev/null
check "rb before" "$(q 'SELECT count(*) FROM rb;')" "100"
q "BEGIN; INSERT INTO rb SELECT g FROM generate_series(101,200) g; DELETE FROM rb WHERE a <= 50; ROLLBACK;" >/dev/null
check "rb after count" "$(q 'SELECT count(*) FROM rb;')"                "100"
check "rb after range" "$(q 'SELECT min(a)||chr(47)||max(a) FROM rb;')" "1/100"
q "DROP TABLE rb;" >/dev/null

# ---------------------------------------------------------------------------
# SAVEPOINT then ROLLBACK TO discards only the subtransaction's changes.
# Rows written before the savepoint (and deletes made before it) survive; work
# done after the savepoint is undone.
# ---------------------------------------------------------------------------
echo "-- savepoint"
q "CREATE TABLE sp (a int) USING pgcolumnar;" >/dev/null
q "INSERT INTO sp SELECT g FROM generate_series(1,10) g;" >/dev/null
SPRES="$(q "BEGIN;
	INSERT INTO sp VALUES (11);
	DELETE FROM sp WHERE a = 1;
	SAVEPOINT s1;
	INSERT INTO sp VALUES (12);
	DELETE FROM sp WHERE a = 2;
	ROLLBACK TO s1;
	SELECT string_agg(a::text, ',' ORDER BY a) FROM sp;
	COMMIT;")"
# before savepoint: added 11, removed 1 -> {2..11}. after-savepoint work undone.
check "savepoint in-txn"  "$SPRES" "2,3,4,5,6,7,8,9,10,11"
check "savepoint final"   "$(qq 'SELECT a FROM sp ORDER BY a;')" "2,3,4,5,6,7,8,9,10,11,"
# AND A STATEMENT THAT FAILS PARTWAY INSIDE A SAVEPOINT (#1341). The two checks
# above cannot reach the savepoint's own discard: executor end flushes each
# COMPLETED statement's buffers under its subtransaction, so the abort's ordinary
# xmin verdict hides them (#1441). Measured: with the subxact abort discarding
# neither pending writes nor pending deletes, both stayed green and so did every
# other check here. An INSERT whose 2001st row divides by zero leaves 2000 rows
# pending, and a DELETE that fails at a = 9 leaves 2..8 pending; ROLLBACK TO must
# discard both, in the transaction and after it. ON_ERROR_STOP is lifted for the
# one statement meant to fail, and each must fail with division by zero and
# nothing else: one that failed before writing anything would leave both discard
# checks nothing to discard. If either failed otherwise, every reading is blanked,
# as the twin's 22012-only allowance ends its session there. A later ERROR (the
# COMMIT, say) already stops psql, and the readings before it stand.
SPERR="$WORKDIR/spfail.err"
SPFAIL="$(q 'BEGIN;
	SAVEPOINT s2;
	\set ON_ERROR_STOP 0
	INSERT INTO sp SELECT CASE WHEN g < 2100 THEN g ELSE 1 / (g - g) END FROM generate_series(100, 3000) g;
	\set ON_ERROR_STOP 1
	ROLLBACK TO s2;
	SELECT count(*) FROM sp WHERE a >= 100;
	SAVEPOINT s3;
	\set ON_ERROR_STOP 0
	DELETE FROM sp WHERE a < 11 AND 1 / (a - 9) IS NOT NULL;
	\set ON_ERROR_STOP 1
	ROLLBACK TO s3;
	SELECT string_agg(a::text, chr(44) ORDER BY a) FROM sp;
	COMMIT;' 2>"$SPERR" || true)"
sed 's/^/spfail stderr: /' "$SPERR"
[ "$(grep ERROR "$SPERR" | head -2 | grep -c 'division by zero')" = 2 ] || SPFAIL=""
check "savepoint failed insert discarded" "$(sed -n 1p <<<"$SPFAIL")" "0"
check "savepoint failed delete discarded" "$(sed -n 2p <<<"$SPFAIL")" "2,3,4,5,6,7,8,9,10,11"
check "savepoint failed final" "$(qq 'SELECT a FROM sp ORDER BY a;')" "2,3,4,5,6,7,8,9,10,11,"
q "DROP TABLE sp;" >/dev/null
# AND A READ IN A BLOCK THAT COMMITS INTO ONE THAT ABORTS (#1341). A DELETE's
# RETURNING calls spn_rd once per deleted row. spn_rd reads the table in an inner
# EXCEPTION block that commits into an outer one, which raises. The read writes the
# outer DELETE's marks so far under the inner block. Its commit hands them to the
# outer block, whose rollback takes them away. The next read must write them again.
# With the inner block's commit leaving them attributed to itself, no later read
# writes them, so each later read counts the deleted rows as live. The statement's
# end still writes every mark, so only the reads show it; their sum is pinned here,
# with the table's shape after. Heap runs the same statement on the same rows.
q "CREATE FUNCTION spn_rd(t text) RETURNS int LANGUAGE plpgsql AS \$\$
DECLARE n int;
BEGIN
	BEGIN
		BEGIN
			EXECUTE format('SELECT count(*) FROM %s', t) INTO n;
		EXCEPTION WHEN no_data_found THEN NULL;
		END;
		PERFORM 1 / 0;
	EXCEPTION WHEN division_by_zero THEN NULL;
	END;
	RETURN n;
END \$\$;" >/dev/null
spn() {
	q "CREATE TABLE spn_$1 (id int) USING $1;
	   INSERT INTO spn_$1 SELECT g FROM generate_series(1, 200) g;
	   WITH d AS (DELETE FROM spn_$1 WHERE id % 2 = 0 RETURNING spn_rd('spn_$1') AS n)
	   SELECT sum(n) FROM d;
	   SELECT count(*) || chr(124) || sum(id) FROM spn_$1;" | paste -sd'|'
}
check "savepoint nested read in a delete sees its deleted rows, as on heap" "$(spn pgcolumnar)" "$(spn heap | grep . || echo "heap gave no reading")"
q "DROP TABLE spn_pgcolumnar, spn_heap; DROP FUNCTION spn_rd(text);" >/dev/null

# ---------------------------------------------------------------------------
# Temporary columnar table: create, insert, scan, delete, update all work.
# ---------------------------------------------------------------------------
echo "-- temporary table"
TEMPRES="$(q "CREATE TEMP TABLE t_tmp (a int, b text) USING pgcolumnar;
	INSERT INTO t_tmp SELECT g, g::text FROM generate_series(1,2000) g;
	DELETE FROM t_tmp WHERE a % 5 = 0;
	UPDATE t_tmp SET b = 'u' WHERE a = 3;
	SELECT count(*) FROM t_tmp;")"
check "temp survivors" "$TEMPRES" "1600"
check "temp deleted"   "$(q 'CREATE TEMP TABLE t_tmp2 (a int) USING pgcolumnar; INSERT INTO t_tmp2 SELECT g FROM generate_series(1,2000) g; DELETE FROM t_tmp2 WHERE a % 5 = 0; SELECT count(*) FROM t_tmp2 WHERE a % 5 = 0;')" "0"

# ---------------------------------------------------------------------------
# Deletes survive across transactions, and drop cleans up delete_vector rows.
# ---------------------------------------------------------------------------
echo "-- durability and cleanup"
q "CREATE TABLE dur (a int) USING pgcolumnar;" >/dev/null
q "INSERT INTO dur SELECT g FROM generate_series(1,1000) g;" >/dev/null
q "DELETE FROM dur WHERE a % 2 = 0;" >/dev/null
check "durable survivors" "$(q 'SELECT count(*) FROM dur;')"                 "500"
check "durable odd only"  "$(q 'SELECT count(*) FROM dur WHERE a % 2 = 0;')" "0"
q "DROP TABLE dur;" >/dev/null
check "delete_vector cleaned"  "$(q 'SELECT count(*) FROM pgcolumnar.delete_vector;')" "0"
check "row group cleaned" "$(q 'SELECT count(*) FROM pgcolumnar.row_group;')" "0"

fail="$PGC_FAIL"

echo
if [ "$fail" = "0" ]; then
	echo "checks run: $PGC_CHECKS"
	echo "PHASE 3 TEST PASSED"
else
	echo "checks run: $PGC_CHECKS"
	echo "PHASE 3 TEST FAILED"
	echo "---- server log tail ----"
	run_pg "tail -40 '$LOGFILE'" || true
fi
exit $fail
