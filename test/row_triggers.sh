#!/usr/bin/env bash
#
# pgColumnar AFTER ... FOR EACH ROW triggers (issue #179).
#
# An after-row trigger records the row's TID when the event is queued and
# re-fetches by that TID when the event fires. A columnar row is given its
# stripe reservation -- and so its row number and item pointer -- when it is
# buffered, not when the stripe is flushed, and after-row events fire in
# AfterTriggerEndQuery, which runs before ExecutorEnd and so before
# finish_bulk_insert flushes. The row was addressable and unreadable in between:
#
#     INSERT INTO t VALUES (1);
#     ERROR:  failed to fetch tuple1 for AFTER trigger
#
# Every after-insert row trigger on a columnar table failed, and took its own
# INSERT down with it -- the row did not land either. The fix has the fetch fall
# back to this transaction's write buffer, which is what the index fetch has
# always done; the mechanism existed and one caller did not use it.
#
# The oracle is heap. Each case runs against a heap table of identical shape and
# the columnar answer has to match it, rather than match a count written here --
# a trigger that fires the wrong number of times, or sees the wrong NEW row, is
# the failure this can regress into, and both are invisible to "did it error".
#
# Usage:  test/row_triggers.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

ROWS=${PGC_TRIG_ROWS:-500}

psql_run "DROP TABLE IF EXISTS tr_log;
	CREATE TABLE tr_log (src text, ev text, id int, val int);
	CREATE OR REPLACE FUNCTION tr_f() RETURNS trigger LANGUAGE plpgsql AS \$\$
	BEGIN
		IF TG_OP = 'DELETE' THEN
			INSERT INTO tr_log VALUES (TG_TABLE_NAME, TG_OP, OLD.id, OLD.val);
		ELSE
			INSERT INTO tr_log VALUES (TG_TABLE_NAME, TG_OP, NEW.id, NEW.val);
		END IF;
		RETURN NULL;
	END \$\$;" >/dev/null

# build a pair of tables, one heap one columnar, with the same trigger
pair() {  # suffix, trigger clause
	local c="tr_c$1" h="tr_h$1"
	psql_run "DROP TABLE IF EXISTS $c; DROP TABLE IF EXISTS $h;
		CREATE TABLE $c (id int, val int) USING pgcolumnar;
		CREATE TABLE $h (id int, val int);
		CREATE TRIGGER ${c}_t $2 ON $c FOR EACH ROW EXECUTE FUNCTION tr_f();
		CREATE TRIGGER ${h}_t $2 ON $h FOR EACH ROW EXECUTE FUNCTION tr_f();" \
		>/dev/null 2>&1
}

# what the trigger recorded for one table, as an order-independent digest
fired() {  # table
	q "SELECT count(*) || '/' || coalesce(sum(id), 0) || '/' || coalesce(sum(val), 0)
		FROM tr_log WHERE src = '$1';" | tail -1
}

# same_as_heap SUFFIX -> "same", the two summaries, or "no reading" when the heap
# twin's trigger fired nothing: a pair whose tables or triggers were never created
# fired nothing on either side, and "0/0/0" agreed with "0/0/0" -- with every trigger
# created against a missing function, all six of these comparisons passed.
same_as_heap() {
	local c h
	c="$(fired "tr_c$1")"; h="$(fired "tr_h$1")"
	if [ -z "$c" ] || [ -z "$h" ] || [ "${h%%/*}" = "0" ]; then echo "no reading: columnar [$c], heap [$h]"
	elif [ "$c" = "$h" ]; then echo same
	else echo "differ: columnar [$c], heap [$h]"; fi
}

# succeeded OUTPUT -> "succeeded", or the error the statement raised. It used to count
# one message, "failed to fetch", so any other error -- a missing table's included --
# read as success.
succeeded() { grep -m1 'ERROR' <<<"$1" || echo succeeded; }

# --- 1. after insert, one row and many --------------------------------------

pair 1 "AFTER INSERT"
err="$(psql_run "INSERT INTO tr_c1 VALUES (1, 10);" 2>&1 || true)"
check "a single-row insert with an after-row trigger succeeds" "$(succeeded "$err")" "succeeded"
psql_run "INSERT INTO tr_h1 VALUES (1, 10);" >/dev/null
check "and the row itself landed" "$(q "SELECT count(*) FROM tr_c1;")" "1"
check "the trigger saw the same row heap did" "$(same_as_heap 1)" "same"

pair 2 "AFTER INSERT"
psql_run "INSERT INTO tr_c2 SELECT g, g * 2 FROM generate_series(1, $ROWS) g;" >/dev/null
psql_run "INSERT INTO tr_h2 SELECT g, g * 2 FROM generate_series(1, $ROWS) g;" >/dev/null
check "a multi-row insert fires once per row with the right values" "$(same_as_heap 2)" "same"

# A mid-statement stripe flush rescues the whole statement, and that bounds the
# defect, so it gets a case of its own.
#
# This file first claimed to cover "rows on both sides of a stripe boundary".
# It did not, and it cannot: measured against the unfixed build, the failure
# depends only on whether a flush happened during the statement, and the
# threshold is exactly stripe_row_limit.
#
#      999 rows at stripe_row_limit=1000    -> fails
#     1000 rows at stripe_row_limit=1000    -> all 1000 fire
#     1500 rows at stripe_row_limit=100000  -> fails
#
# An insert that crosses the limit therefore cannot be made to fail by making it
# bigger -- crossing is what rescues it, and the mechanism is worth writing down
# because it is not obvious:
#
#   1. the stripe fills mid-statement and flushes, so the earliest rows are on
#      disk before any trigger runs
#   2. the first trigger's fetch therefore succeeds, and its body runs
#   3. the body is itself a statement, and pgColumnar's ExecutorEnd hook calls
#      PgColumnarFlushAllPendingWrites when it ends -- flushing the outer INSERT's
#      remaining buffered rows
#   4. every later fetch then finds its row on disk
#
# So one successful fetch rescues the whole statement, through the trigger body.
# Confirmed by removing the SQL from the body: with a trigger that only does
# RETURN NULL, no ExecutorEnd fires between rows, and 1200 rows at a limit of
# 1000 give 1000 successful fetches and then a failure on row 1001 -- exactly
# the first buffered row.
#
# The case is kept and now says what it is: a control that passed before the fix
# and has to keep passing after it.
psql_run "DROP TABLE IF EXISTS tr_c3; DROP TABLE IF EXISTS tr_h3;
	SET pgcolumnar.stripe_row_limit = 1000;
	CREATE TABLE tr_c3 (id int, val int) USING pgcolumnar;
	CREATE TRIGGER tr_c3_t AFTER INSERT ON tr_c3 FOR EACH ROW EXECUTE FUNCTION tr_f();" >/dev/null
psql_run "CREATE TABLE tr_h3 (id int, val int);
	CREATE TRIGGER tr_h3_t AFTER INSERT ON tr_h3 FOR EACH ROW EXECUTE FUNCTION tr_f();" >/dev/null
psql_run "SET pgcolumnar.stripe_row_limit = 1000;
	INSERT INTO tr_c3 SELECT g, g FROM generate_series(1, 4500) g;" >/dev/null 2>&1 || true
psql_run "INSERT INTO tr_h3 SELECT g, g FROM generate_series(1, 4500) g;" >/dev/null
check "an insert crossing a stripe boundary fires for every row" "$(same_as_heap 3)" "same"

# The discriminating multi-row case: more than one row and fewer than the stripe
# limit, so nothing flushes mid-statement and every trigger has to reach a
# buffered row. This is the shape an ordinary insert has, the default
# stripe_row_limit being far larger than a typical statement.
psql_run "DROP TABLE IF EXISTS tr_c7; DROP TABLE IF EXISTS tr_h7;
	SET pgcolumnar.stripe_row_limit = 100000;
	CREATE TABLE tr_c7 (id int, val int) USING pgcolumnar;
	CREATE TRIGGER tr_c7_t AFTER INSERT ON tr_c7 FOR EACH ROW EXECUTE FUNCTION tr_f();" >/dev/null
psql_run "CREATE TABLE tr_h7 (id int, val int);
	CREATE TRIGGER tr_h7_t AFTER INSERT ON tr_h7 FOR EACH ROW EXECUTE FUNCTION tr_f();" >/dev/null
psql_run "SET pgcolumnar.stripe_row_limit = 100000;
	INSERT INTO tr_c7 SELECT g, g FROM generate_series(1, 1500) g;" >/dev/null 2>&1 || true
psql_run "INSERT INTO tr_h7 SELECT g, g FROM generate_series(1, 1500) g;" >/dev/null
check "1500 rows with no mid-statement flush all fire" "$(same_as_heap 7)" "same"

# --- 2. the other row events ------------------------------------------------

pair 4 "AFTER UPDATE"
psql_run "INSERT INTO tr_c4 SELECT g, g FROM generate_series(1, 50) g;" >/dev/null 2>&1 || true
psql_run "INSERT INTO tr_h4 SELECT g, g FROM generate_series(1, 50) g;" >/dev/null
psql_run "UPDATE tr_c4 SET val = val + 1 WHERE id % 3 = 0;" >/dev/null 2>&1 || true
psql_run "UPDATE tr_h4 SET val = val + 1 WHERE id % 3 = 0;" >/dev/null
check "after-update fires with the new row" "$(same_as_heap 4)" "same"

pair 5 "AFTER DELETE"
psql_run "INSERT INTO tr_c5 SELECT g, g FROM generate_series(1, 50) g;" >/dev/null 2>&1 || true
psql_run "INSERT INTO tr_h5 SELECT g, g FROM generate_series(1, 50) g;" >/dev/null
psql_run "DELETE FROM tr_c5 WHERE id % 4 = 0;" >/dev/null 2>&1 || true
psql_run "DELETE FROM tr_h5 WHERE id % 4 = 0;" >/dev/null
check "after-delete fires with the old row" "$(same_as_heap 5)" "same"

# insert and update in one transaction, so the update re-fetches a row that is
# still buffered from the insert
pair 6 "AFTER INSERT OR UPDATE"
psql_run "BEGIN;
	INSERT INTO tr_c6 SELECT g, g FROM generate_series(1, 30) g;
	UPDATE tr_c6 SET val = 0 WHERE id <= 10;
	COMMIT;" >/dev/null
psql_run "BEGIN;
	INSERT INTO tr_h6 SELECT g, g FROM generate_series(1, 30) g;
	UPDATE tr_h6 SET val = 0 WHERE id <= 10;
	COMMIT;" >/dev/null
check "insert then update in one transaction" "$(same_as_heap 6)" "same"

# --- 3. what must not have changed ------------------------------------------

# A BEFORE trigger never re-fetches, so it worked before the fix and must still.
psql_run "DROP TABLE IF EXISTS tr_b;
	CREATE TABLE tr_b (id int, val int) USING pgcolumnar;
	CREATE TRIGGER tr_b_t BEFORE INSERT ON tr_b FOR EACH ROW EXECUTE FUNCTION tr_f();
	INSERT INTO tr_b SELECT g, g FROM generate_series(1, 20) g;" >/dev/null
check "a before-row trigger still fires once per row" \
	"$(q "SELECT count(*) FROM tr_log WHERE src = 'tr_b';")" "20"

# A deferred constraint trigger fires at commit, after the flush, so it worked
# before the fix too. It is here to catch a fix that breaks the case that worked.
psql_run "DROP TABLE IF EXISTS tr_d;
	CREATE TABLE tr_d (id int, val int) USING pgcolumnar;
	CREATE CONSTRAINT TRIGGER tr_d_t AFTER INSERT ON tr_d
		DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION tr_f();
	BEGIN;
	INSERT INTO tr_d SELECT g, g FROM generate_series(1, 20) g;
	COMMIT;" >/dev/null
check "a deferred constraint trigger still fires at commit" \
	"$(q "SELECT count(*) FROM tr_log WHERE src = 'tr_d';")" "20"

# --- 4. the import path, which #180 made fire these ---------------------------

# #180 routed imports through the executor's index maintenance, which is also
# what queues after-row trigger events. Before this fix that turned every import
# into such a table into the same failure; the two changes only make sense
# together, so the pairing is asserted here.
ARROW="${PGC_TMPDIR:-/tmp}/pgc_trig_$$.arrow"
psql_run "DROP TABLE IF EXISTS tr_src;
	CREATE TABLE tr_src (id int, val int) USING pgcolumnar;
	INSERT INTO tr_src SELECT g, g FROM generate_series(1, 40) g;" >/dev/null
psql_run "SELECT pgcolumnar.export_arrow('tr_src', '$ARROW');" >/dev/null

psql_run "DROP TABLE IF EXISTS tr_imp;
	CREATE TABLE tr_imp (id int, val int) USING pgcolumnar;
	CREATE INDEX tr_imp_i ON tr_imp (id);
	CREATE TRIGGER tr_imp_t AFTER INSERT ON tr_imp
		FOR EACH ROW EXECUTE FUNCTION tr_f();" >/dev/null
imperr="$(psql_run "SELECT pgcolumnar.import_arrow('tr_imp', '$ARROW');" 2>&1 || true)"
check "an import into a table with an after-row trigger succeeds" "$(succeeded "$imperr")" "succeeded"
check "the imported rows landed" "$(q "SELECT count(*) FROM tr_imp;")" "40"
check "and the trigger fired once per imported row" \
	"$(q "SELECT count(*) FROM tr_log WHERE src = 'tr_imp';")" "40"

rm -f "$ARROW"
pgc_summary
