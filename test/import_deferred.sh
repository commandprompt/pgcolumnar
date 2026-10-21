#!/usr/bin/env bash
#
# pgColumnar import against a DEFERRABLE constraint (issue #168).
#
# A deferrable constraint is checked at commit, not at insert. The import path
# maintained indexes itself and checked immediately, so an import that collided
# with a row the same transaction went on to remove failed where an ordinary
# INSERT succeeded. The importers now use the executor's index maintenance --
# ExecInsertIndexTuples selects UNIQUE_CHECK_PARTIAL for a non-immediate index
# by itself and ExecARInsertTriggers queues the recheck -- inside an
# after-trigger query level of their own.
#
# The collision has to exist WHEN THE ROW IS INSERTED and be gone by commit;
# that is what "deferred" means. Deleting the conflicting row first produces a
# transaction that commits on any build, passing against the defect. The first
# version of this file did exactly that and proved nothing.
#
# What separates a deferred check from an absent one is the second group below.
# "The import committed" is true both when deferral works and when the check
# quietly stopped happening, so a file asserting only that would pass against a
# build with constraint enforcement removed. These must still fail, and fail
# where an ordinary INSERT fails:
#
#   a duplicate that is still there at commit
#   an IMMEDIATE constraint
#   SET CONSTRAINTS ... IMMEDIATE part-way through
#   an exclusion constraint
#
# heap is the oracle throughout: every case is run against a heap table of the
# same shape, and the columnar answer has to match it rather than match a
# constant written here.
#
# Usage:  test/import_deferred.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

ARROW="${PGC_TMPDIR:-/tmp}/pgc_defer_$$.arrow"
PARQ="${PGC_TMPDIR:-/tmp}/pgc_defer_$$.parquet"

# one row holding id = 1, to be imported into a table that already has one
psql_run "DROP TABLE IF EXISTS df_src;
	CREATE TABLE df_src (id int) USING pgcolumnar;
	INSERT INTO df_src VALUES (1);" >/dev/null
psql_run "SELECT pgcolumnar.export_arrow('df_src', '$ARROW');" >/dev/null
psql_run "SELECT pgcolumnar.export_parquet('df_src', '$PARQ');" >/dev/null

# build a table of the given access method holding id = 1, with the given
# constraint clause on a unique constraint over id
mk() {  # table, access method, constraint clause
	psql_run "DROP TABLE IF EXISTS $1;
		CREATE TABLE $1 (id int) USING $2;
		INSERT INTO $1 VALUES (1);
		ALTER TABLE $1 ADD CONSTRAINT ${1}_uq UNIQUE (id) $3;" >/dev/null 2>&1
}

# Run a transaction and report whether it committed, and if not, the SQLSTATE
# it failed with. The error TEXT differs between an insert-time and a
# commit-time failure in ways that are not the property under test; the CODE
# does not. Any error used to count: with every import reading a file that does
# not exist, all five "rejects" arms passed (measured).
outcome() {  # sql
	local out code
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -v ON_ERROR_STOP=1 -v VERBOSITY=sqlstate -q -c "$1" 2>&1)"
	code="$(printf '%s\n' "$out" | sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\).*/\1/p' | head -1)"
	if [ -n "$code" ]; then echo "error $code"
	elif [ "$(printf '%s\n' "$out" | grep -ci 'error')" -gt 0 ]; then echo "error (no SQLSTATE): $out"
	else echo "committed"; fi
}

# --- 1. a transient collision is allowed to commit -----------------------------

# The row imported collides with the row already there; that row is deleted
# before commit, so at commit the constraint holds. heap says this is fine.
mk df_h heap "DEFERRABLE INITIALLY DEFERRED"
heap_transient="$(outcome "BEGIN;
	INSERT INTO df_h VALUES (1);
	DELETE FROM df_h WHERE ctid = (SELECT min(ctid) FROM df_h);
	COMMIT;")"
check "premise: heap allows a transient collision under a deferred constraint" \
	"$heap_transient" "committed"

# the same shape through pgcolumnar's ordinary insert path, which already
# deferred correctly -- here so a regression in it is not blamed on the import
mk df_i pgcolumnar "DEFERRABLE INITIALLY DEFERRED"
check "an ordinary columnar INSERT still allows it" \
	"$(outcome "BEGIN;
		INSERT INTO df_i VALUES (1);
		DELETE FROM df_i WHERE ctid = (SELECT min(ctid) FROM df_i);
		COMMIT;")" \
	"$heap_transient"

mk df_a pgcolumnar "DEFERRABLE INITIALLY DEFERRED"
check "import_arrow allows it" \
	"$(outcome "BEGIN;
		SELECT pgcolumnar.import_arrow('df_a', '$ARROW');
		DELETE FROM df_a WHERE ctid = (SELECT min(ctid) FROM df_a);
		COMMIT;")" \
	"$heap_transient"

mk df_p pgcolumnar "DEFERRABLE INITIALLY DEFERRED"
check "import_parquet allows it" \
	"$(outcome "BEGIN;
		SELECT pgcolumnar.import_parquet('df_p', '$PARQ');
		DELETE FROM df_p WHERE ctid = (SELECT min(ctid) FROM df_p);
		COMMIT;")" \
	"$heap_transient"

# --- 2. the check is deferred, not gone ----------------------------------------

# This group is what makes the file discriminating. Each case must still fail.

mk df_h2 heap "DEFERRABLE INITIALLY DEFERRED"
heap_permanent="$(outcome "BEGIN;
	INSERT INTO df_h2 VALUES (1);
	COMMIT;")"
check "premise: heap rejects a duplicate that is still there at commit" \
	"$heap_permanent" "error 23505"

mk df_a2 pgcolumnar "DEFERRABLE INITIALLY DEFERRED"
check "import_arrow rejects a duplicate that survives to commit" \
	"$(outcome "BEGIN;
		SELECT pgcolumnar.import_arrow('df_a2', '$ARROW');
		COMMIT;")" \
	"$heap_permanent"
check "and the import rolled back rather than leaving the row" \
	"$(q "SELECT count(*) FROM df_a2;")" "1"

mk df_a3 pgcolumnar ""
check "an IMMEDIATE constraint still rejects at insert time" \
	"$(outcome "BEGIN;
		SELECT pgcolumnar.import_arrow('df_a3', '$ARROW');
		DELETE FROM df_a3 WHERE ctid = (SELECT min(ctid) FROM df_a3);
		COMMIT;")" \
	"error 23505"

# The deletion that would have rescued it happens after SET CONSTRAINTS, so a
# build that honours the setting fails and one that ignores it commits.
mk df_a4 pgcolumnar "DEFERRABLE INITIALLY DEFERRED"
check "SET CONSTRAINTS IMMEDIATE is honoured mid-transaction" \
	"$(outcome "BEGIN;
		SELECT pgcolumnar.import_arrow('df_a4', '$ARROW');
		SET CONSTRAINTS df_a4_uq IMMEDIATE;
		DELETE FROM df_a4 WHERE ctid = (SELECT min(ctid) FROM df_a4);
		COMMIT;")" \
	"error 23505"

# --- 3. what the executor route must not have dropped --------------------------

psql_run "DROP TABLE IF EXISTS df_x;
	CREATE TABLE df_x (id int) USING pgcolumnar;
	INSERT INTO df_x VALUES (1);
	ALTER TABLE df_x ADD CONSTRAINT df_x_ex EXCLUDE ((id) WITH =);" >/dev/null 2>&1
check "an exclusion constraint is still enforced on import" \
	"$(outcome "BEGIN;
		SELECT pgcolumnar.import_arrow('df_x', '$ARROW');
		COMMIT;")" \
	"error 23P01"

# A partial index must still be maintained, and only for rows it covers. The
# executor decides this now; before, this file's own predicate check did.
psql_run "DROP TABLE IF EXISTS df_pi; DROP TABLE IF EXISTS df_pisrc;
	CREATE TABLE df_pisrc (id int, keep bool) USING pgcolumnar;
	INSERT INTO df_pisrc SELECT g, (g % 2 = 0) FROM generate_series(1, 200) g;" >/dev/null
psql_run "SELECT pgcolumnar.export_arrow('df_pisrc', '${ARROW}.pi');" >/dev/null
psql_run "CREATE TABLE df_pi (id int, keep bool) USING pgcolumnar;
	CREATE UNIQUE INDEX df_pi_partial ON df_pi (id) WHERE keep;" >/dev/null
psql_run "SELECT pgcolumnar.import_arrow('df_pi', '${ARROW}.pi');" >/dev/null

check "a partial unique index accepts rows it does not cover" \
	"$(outcome "SELECT pgcolumnar.import_arrow('df_pi', '${ARROW}.pi');")" \
	"error 23505"
check "rows outside the partial index are all present" \
	"$(q "SELECT count(*) FROM df_pi WHERE NOT keep;")" "100"

# The rewrite path enforces nothing on purpose: it moves rows that already
# satisfied the constraint while the row being replaced is still visible, so a
# check would find the row against itself. It keeps the hand-rolled loop, and
# this is what catches it being routed through the enforcing one by mistake.
psql_run "DROP TABLE IF EXISTS df_rw;
	CREATE TABLE df_rw (id int) USING pgcolumnar;
	INSERT INTO df_rw SELECT g FROM generate_series(1, 500) g;
	CREATE UNIQUE INDEX df_rw_uq ON df_rw (id);
	DELETE FROM df_rw WHERE id % 3 = 0;" >/dev/null
#
# THROUGH compact_rewrite, NOT vacuum(). vacuum() rewrites the whole relation and
# then reindexes it, so it never reaches the per-row loop this arm is about, and
# routing that loop through the enforcing path passed it (measured).
# compact_rewrite over a table with deleted rows does reach it.
check "a rewrite of a uniquely-indexed table still succeeds" \
	"$(outcome "SELECT pgcolumnar.compact_rewrite('df_rw', 0.0);")" "committed"
check "and the surviving rows are intact" \
	"$(q "SELECT count(*) FROM df_rw;")" \
	"$(q "SELECT count(*) FROM generate_series(1,500) g WHERE g % 3 <> 0;")"

rm -f "$ARROW" "${ARROW}.pi" "$PARQ"
pgc_summary
