#!/usr/bin/env bash
#
# A maintenance function that rolls back leaves the table as it was (#1341).
# compact_rewrite, recluster, compact, vacuum and vacuum_sorted each retire row
# groups and most write new ones. Retirement is a catalog delete, so a rollback
# must bring every retired group back, with the bytes it points at intact: the
# space a retired group frees may be reused only once no snapshot can need it,
# and never by the transaction that freed it, which can still roll back.
#
# The table has row groups of 1000 rows, two in five rows deleted, and one group
# deleted whole, so every function has work. Each function runs inside a
# transaction, its effect on the row groups is read there, and the transaction
# rolls back. The table must then hold the same rows, by content, and the same
# row groups, and an index scan must agree. The last arm does the same under a
# savepoint, then commits further writes, and runs every function for real.
#
# EACH FUNCTION GETS A FRESH TABLE. On one shared table, a rollback that corrupts it
# fails every function after it for that reason, not its own (OffgridwithJD, review
# of #1419).
#
# Usage:  test/maintenance_rollback.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

build_mr() {	# the table, made again: 10 row groups of 1000, 5400 rows left
	psql_run "SET pgcolumnar.stripe_row_limit = 1000;
	          DROP TABLE IF EXISTS mr;
	          CREATE TABLE mr (id int, v int, w text) USING pgcolumnar;
	          INSERT INTO mr SELECT g, (g * 7919) % 10007, md5(g::text) FROM generate_series(1, 10000) g;
	          CREATE INDEX mr_id ON mr (id);
	          DELETE FROM mr WHERE id % 5 IN (1, 2) OR id BETWEEN 3001 AND 4000;"
}
build_mr

rows() {	# the rows, by content
	q "SELECT count(*) || '/' || md5(string_agg(id || ':' || v || ':' || w, ',' ORDER BY id)) FROM mr" | tail -1
}
RG_SQL="SELECT count(*) || '/' || coalesce(sum(row_count), 0) FROM pgcolumnar.row_group
         WHERE storage_id = pgcolumnar.get_storage_id('mr')"
BY_INDEX_SETS="SET enable_seqscan = off; SET enable_bitmapscan = off; SET pgcolumnar.enable_custom_scan = off;"
BY_INDEX_SQL="SELECT count(*) || '/' || md5(string_agg(id || ':' || v || ':' || w, ',' ORDER BY id))
	     FROM mr WHERE id BETWEEN 1 AND 10000"
by_index() {	# the rows an index scan returns, by content
	# pgcolumnar.enable_custom_scan = off as well, and a premise that this plan is an
	# Index Scan: enable_seqscan alone leaves the columnar custom scan on, and this
	# whole-table range planned as Custom Scan (PgColumnarScan). Measured (#1339):
	# with the index fetch reading the neighbouring row, every "by index" arm stayed
	# green, because none of them read the index.
	q "$BY_INDEX_SETS $BY_INDEX_SQL" | grep -E '^[0-9]+/' | tail -1
}
ROWS0="$(rows)"
RG0="$(q "$RG_SQL")"
check "premise: the table holds the rows it was given" "${ROWS0%%/*}|$RG0" "5400|10/10000"
check "premise: the by-index reading is an Index Scan through mr_id" \
	"$(pgc_uses_row_fetch "$BY_INDEX_SETS" "$BY_INDEX_SQL")" yes

# rolled_back FUNCTION -> "<groups inside the transaction>|<rows after>|<groups after>|<by index after>"
rolled_back() {
	local inside
	inside="$(q "BEGIN; SELECT $1; $RG_SQL; ROLLBACK;" | grep -E '^[0-9]+/[0-9]+$' | tail -1)"
	echo "$inside|$(rows)|$(q "$RG_SQL")|$(by_index)"
}
for f in "pgcolumnar.compact_rewrite('mr', 0.0)" "pgcolumnar.recluster('mr', 'v')" "pgcolumnar.compact('mr')" \
         "pgcolumnar.vacuum('mr')" "pgcolumnar.vacuum_sorted('mr', 'v')"; do
	build_mr
	got="$(rolled_back "$f")"
	name="${f#pgcolumnar.}"; name="${name%%(*}"
	check "$name changed the row groups inside its transaction" \
		"$([ "${got%%|*}" != "$RG0" ] && [ -n "${got%%|*}" ] && echo changed || echo "unchanged (${got%%|*})")" "changed"
	check "after $name rolls back, the table holds the same rows and row groups, by scan and by index" \
		"${got#*|}" "$ROWS0|$RG0|$ROWS0"
done

# ---- under a savepoint, then committed work, then every function for real ----------
build_mr
psql_run "BEGIN;
          SAVEPOINT s; SELECT pgcolumnar.compact_rewrite('mr', 0.0); SELECT pgcolumnar.recluster('mr', 'v');
          ROLLBACK TO s;
          SET LOCAL pgcolumnar.stripe_row_limit = 1000;
          INSERT INTO mr SELECT g, g, 'new' FROM generate_series(20001, 21000) g;
          COMMIT;" >/dev/null
ROWS1="$(rows)"
check "after a savepoint rolls back two rewrites, the rows written later in the transaction commit beside the old ones" \
	"$(q "SELECT count(*) FILTER (WHERE w = 'new') || '/' || count(*) FROM mr" | tail -1)|$(by_index | cut -d/ -f1)" "1000/6400|5400"
for f in "pgcolumnar.compact_rewrite('mr', 0.0)" "pgcolumnar.recluster('mr', 'v')" "pgcolumnar.compact('mr')" \
         "pgcolumnar.vacuum('mr')"; do
	psql_run "SELECT $f;" >/dev/null
done
check "and every function then runs for real and keeps the same rows" "$(rows)" "$ROWS1"

pgc_summary
