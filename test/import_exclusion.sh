#!/usr/bin/env bash
#
# pgColumnar import against an exclusion constraint (follow-up to #153).
#
# #158 made import_arrow and import_parquet maintain indexes, which fixed index
# scans returning nothing and unique constraints accepting duplicates. It did not
# cover exclusion constraints, and they are not unique indexes: index_insert does
# not enforce one. The executor inserts the entry and then scans for a conflicting
# one, through check_exclusion_constraint. Without that call an import could leave
# a table in a state an ordinary INSERT would have refused -- entries present, the
# constraint violated, nothing raised.
#
# That is the same shape as #153 itself, which is why it belongs with it rather
# than in a general-purpose suite.
#
# The interesting risk in the fix is the opposite one: a check that fires when it
# should not would break every import into a table that merely has an exclusion
# constraint, which is why the first thing asserted is that a clean import still
# works.
#
# Usage:  test/import_exclusion.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

ROWS=${PGC_EXCL_ROWS:-500}
ARROW="${PGC_TMPDIR:-/tmp}/pgc_excl_$$.arrow"

# EXCLUDE USING btree (k WITH =) needs no extension and is still an exclusion
# constraint rather than a unique index -- indisunique false, indisexclusion true
# -- which is the distinction that matters here, because index_insert enforces
# the one and not the other.
#
# The first version of this file required btree_gist and skipped without it,
# reporting PASSED having run zero checks. On the machine the gate runs on there
# was then no evidence behind the change at all, which is the same failure as a
# suite no gate runs. Nothing here is conditional now.

psql_run "DROP TABLE IF EXISTS ex_src;
	CREATE TABLE ex_src (k int, v text) USING pgcolumnar;
	INSERT INTO ex_src SELECT g, 'r' || g FROM generate_series(1, $ROWS) g;" >/dev/null
psql_run "SELECT pgcolumnar.export_arrow('ex_src', '$ARROW');" >/dev/null

psql_run "DROP TABLE IF EXISTS ex_t;
	CREATE TABLE ex_t (k int, v text) USING pgcolumnar;
	ALTER TABLE ex_t ADD CONSTRAINT ex_t_k EXCLUDE USING btree (k WITH =);" >/dev/null

# If this were a unique index the suite would pass through index_insert's own
# enforcement and prove nothing about the path it is named for.
check "the constraint is an exclusion constraint, not a unique index" \
	"$(q "SELECT indisunique || '/' || indisexclusion FROM pg_index i
		JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = 'ex_t_k';")" \
	"false/true"

# --- 1. a clean import still works --------------------------------------------

# First, because a conflict check that fires when it should not would make every
# import into a table with an exclusion constraint fail, which is worse than the
# defect being fixed.
check "importing rows that violate nothing succeeds" \
	"$(q "SELECT pgcolumnar.import_arrow('ex_t', '$ARROW');")" "$ROWS"

check "and the rows are there" \
	"$(q "SELECT count(*) FROM ex_t;")" "$ROWS"

# the entries reached the index too, which is what #153 was about
check "an index scan finds an imported row" \
	"$(q "SET pgcolumnar.enable_custom_scan = off; SET enable_seqscan = off;
		SELECT count(*) FROM ex_t WHERE k = $((ROWS / 2));" | tail -1)" "1"

# --- 2. a violating import raises ---------------------------------------------

# Importing the same keys again conflicts with every row already there.
err="$(psql_run "SELECT pgcolumnar.import_arrow('ex_t', '$ARROW');" 2>&1 || true)"
check "importing rows that violate the constraint raises" \
	"$(case "$err" in *"exclusion constraint"*) echo yes ;; *) echo "no ($err)" ;; esac)" \
	"yes"

check "and the failed import left the row count alone" \
	"$(q "SELECT count(*) FROM ex_t;")" "$ROWS"

# --- 3. the same violation through ordinary DML behaves the same --------------

# The point of the fix is that an import cannot reach a state DML refuses, so the
# two paths must agree rather than merely both being non-silent.
derr="$(psql_run "INSERT INTO ex_t VALUES ($((ROWS / 2)), 'dup');" 2>&1 || true)"
check "an ordinary INSERT of the same violation also raises" \
	"$(case "$derr" in *"exclusion constraint"*) echo yes ;; *) echo "no ($derr)" ;; esac)" \
	"yes"

# --- 4. a rewrite must not check, because the row it moves is still visible ----

# compact_rewrite moves rows that already satisfied the constraint while the row
# being replaced is still visible, so a conflict check there would find the row
# against itself. This is the case that fails if the check is not conditioned on
# the caller enforcing constraints.
#
# THE REWRITE MUST HAPPEN. With nothing deleted, compact_rewrite has nothing to
# compact and rewrites 0 groups, so this arm passed without moving a row: a
# rewrite that enforced the constraint, finding each moved row against itself,
# passed it (measured). One key in ten is deleted first, so a group really is
# rewritten, and the reading is the number of groups it rewrote.
psql_run "DELETE FROM ex_t WHERE k % 10 = 0;" >/dev/null
rewrote="$(q "SELECT pgcolumnar.compact_rewrite('ex_t', 0.0);" 2>&1 | tail -1)"
check "a rewrite of the same table still succeeds" \
	"$(pgc_is_number "$rewrote" && [ "$rewrote" -ge 1 ] && echo yes || echo "no ($rewrote)")" "yes"

check "and the rows survived it" "$(q "SELECT count(*) FROM ex_t;")" "$((ROWS - ROWS / 10))"

rm -f "$ARROW"
pgc_summary
