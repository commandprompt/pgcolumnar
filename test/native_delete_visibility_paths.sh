#!/usr/bin/env bash
#
# pgColumnar delete visibility agrees across every access path.
#
# The delete-vector fold (OR the group's bitmaps into one mask) and the per-row
# bit test are consulted by four independent paths: the sequential scan, the
# index fetch, the index-only scan, and the buffered (read-your-writes) path. The
# fold is single-sourced in pgcolumnar_merge_delete_vectors and the bit test in
# dv_row_deleted, so a deleted row is invisible through all of them identically.
#
# This is the characterization test for that seam: it deletes a known set and
# asserts every path returns the same live set. It does not go red when the seam
# is un-refactored (the paths behaved the same before), which is the point -- it
# guards the INVARIANT the shared helpers protect, so a future change that breaks
# one path's fold or bit test fails here rather than only under one plan shape.
#
# Usage:  test/native_delete_visibility_paths.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

q "CREATE EXTENSION IF NOT EXISTS pgcolumnar;" >/dev/null
# Small stripes so the 5000 rows span five groups (each builds its own mask).
q "SET pgcolumnar.stripe_row_limit=1000;
   CREATE TABLE t (id int, v int) USING pgcolumnar;
   INSERT INTO t SELECT g, g FROM generate_series(1,5000) g;
   CREATE UNIQUE INDEX t_id ON t (id);" >/dev/null
check "premise: the table holds all 5000 rows" "$(q 'SELECT count(*) FROM t;')" "5000"
q "DELETE FROM t WHERE id % 7 = 0;" >/dev/null
# Each group builds its own mask, so one group would test the fold of one bitmap.
check "premise: the deletes span more than one row group" \
	"$(q "SELECT (count(*) >= 2)::int FROM pgcolumnar.storage s
	      JOIN pgcolumnar.row_group rg USING (storage_id)
	      WHERE s.relation_oid = 't'::regclass;")" "1"

# Live count = 5000 - floor(5000/7) = 5000 - 714 = 4286. Five row groups.
LIVE=4286

# The last line of each probe is the count; SET/BEGIN precede it under a
# multi-statement send.
last() { q "$1" | tail -1; }

# EACH ARM FORCES THE PATH IT IS NAMED FOR, AND ASSERTS THE NODE FIRST (#1339).
# The arms set enable_seqscan/enable_indexonlyscan and trusted the names, but none
# of the enable_* scan GUCs governs Custom Scan (PgColumnarScan): measured, the
# index / bitmap and index-only arms planned as the columnar custom scan, the same
# path as the sequential arm, so three of four named paths were one path counted
# three times. pgcolumnar.enable_custom_scan=off is what moves the plan; the
# pytest twin has forced each path this way since it was written.
NO_COLUMNAR="SET pgcolumnar.enable_custom_scan = off;"
path_plan() {	# path_plan SETS QUERY NODE -> yes, or the plan's node lines
	local plan
	plan="$(q "$1 EXPLAIN (COSTS OFF) $2")"
	case "$plan" in
		*"$3"*) echo yes ;;
		*) echo "no: $(grep -E 'Scan|Aggregate' <<<"$plan" | sed 's/^ *//' | tr '\n' '/')" ;;
	esac
}

SEQ="$NO_COLUMNAR SET enable_indexscan=off; SET enable_bitmapscan=off; SET enable_indexonlyscan=off;"
check "premise: the sequential arm really plans a Seq Scan" \
	"$(path_plan "$SEQ" "SELECT count(*) FROM t WHERE v >= 0" "Seq Scan")" yes
check "sequential scan sees the live set (row-emitting path)" \
	"$(last "$SEQ SELECT count(*) FROM t WHERE v >= 0;")" "$LIVE"

IDX="$NO_COLUMNAR SET enable_seqscan=off; SET enable_indexonlyscan=off;"
check "premise: the index arm really plans an Index Scan" \
	"$(path_plan "$IDX" "SELECT count(*) FROM t WHERE id >= 0" "Index Scan")" yes
check "index / bitmap scan sees the live set" \
	"$(last "$IDX SELECT count(*) FROM t WHERE id >= 0;")" "$LIVE"

IOS="$NO_COLUMNAR SET enable_seqscan=off; SET enable_indexonlyscan=on;"
check "premise: the index-only arm really plans an Index Only Scan" \
	"$(path_plan "$IOS" "SELECT count(id) FROM t WHERE id >= 0" "Index Only Scan")" yes
check "index-only scan sees the live set" \
	"$(last "$IOS SELECT count(id) FROM t WHERE id >= 0;")" "$LIVE"

# The columnar path, left to the planner: a different consumer of the same fold.
check "premise: the whole-table aggregate stays on the columnar path" \
	"$(path_plan "" "SELECT count(*) FROM t" "Custom Scan")" yes
check "aggregate over the whole table sees the live set" \
	"$(q 'SELECT count(*) FROM t;')" "$LIVE"

# A specific deleted row is invisible, and its live neighbour visible, via the
# index (which reaches the same bit test through a different path).
check "a deleted row is invisible via the index" \
	"$(last "$NO_COLUMNAR SET enable_seqscan=off; SELECT count(*) FROM t WHERE id = 700;")" "0"
check "a live row is visible via the index" \
	"$(last "$NO_COLUMNAR SET enable_seqscan=off; SELECT count(*) FROM t WHERE id = 701;")" "1"
# The buffered (same-transaction) path: delete more inside a txn, then read it back.
check "a row deleted in-transaction is invisible in the same transaction" \
	"$(q 'BEGIN; DELETE FROM t WHERE id = 8; SELECT count(*) FROM t WHERE id = 8; ROLLBACK;' | grep -E '^[0-9]+$' | head -1)" "0"
# and the rollback really rolled back, so the arm above is not a delete that persisted
check "the rolled-back delete left the row in place" "$(q 'SELECT count(*) FROM t WHERE id = 8;')" "1"

# A DELETE whose qual deletes another row of the same table (#1394). The inner DELETE's
# scan start writes the outer statement's buffered marks, and its statement end writes
# its own mark to the same row group, both in one command. The second write updated a
# delete_vector row the first had written in that command, and failed with "attempted
# to update invisible tuple". Run on heap too; the columnar answer must be heap's.
psql_run "CREATE FUNCTION del_next(i int, t text) RETURNS int LANGUAGE plpgsql AS \$\$
BEGIN EXECUTE format('DELETE FROM %s WHERE id = %s', t, i + 1); RETURN 1; END \$\$;"
for am in pgcolumnar heap; do
	psql_run "CREATE TABLE dn_$am (id int, v int) USING $am;
	          INSERT INTO dn_$am SELECT g, 0 FROM generate_series(1, 20) g;"
done
dn() {	# dn AM -> ok, or the SQLSTATE the DELETE fails with
	local out
	out=$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-X -At -v ON_ERROR_STOP=1 -v VERBOSITY=sqlstate \
		-c "DELETE FROM dn_$1 WHERE id % 2 = 0 AND del_next(id, 'dn_$1') IS NOT NULL" 2>&1 >/dev/null)
	[ -z "$out" ] && { echo ok; return; }
	echo "$out" | sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\).*/\1/p' | head -1
}
dn_after() { q "SELECT count(*) || '|' || coalesce(sum(id), 0) FROM dn_$1"; }
check "a DELETE whose qual deletes another row of the table succeeds and leaves heap's rows (#1394)" \
	"$(dn pgcolumnar)|$(dn_after pgcolumnar)" "$(dn heap)|$(dn_after heap)"

pgc_summary
