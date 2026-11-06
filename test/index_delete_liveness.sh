#!/usr/bin/env bash
#
# nbtree's deletion passes must never destroy an index entry for a LIVE row.
#
# table_index_delete_tuples asks "is this entry dead to EVERYONE". nbtree acts on
# the answer in _bt_delitems_delete: the items are removed from the leaf page
# physically, inside a critical section, WAL-logged. Nothing puts them back, and
# a transaction abort does not.
#
# heapam answers from a GLOBAL visibility test (InitNonVacuumableSnapshot over
# GlobalVisTestFor). Answering from the calling backend's MVCC snapshot instead
# is a different question: a row this very transaction deleted but has not
# committed reads as "not live to me" while still being live to everyone else.
#
# The observable end of that is silent and severe. With version churn on a
# non-indexed column, so that index_unchanged_by_update() sets nbtree's
# indexUnchanged hint and triggers the bottom-up pass:
#
#     SELECT count(*) FROM cc WHERE a = 1;   -- 228, via Index Only Scan
#     SELECT count(b) FROM cc WHERE a = 1;   -- 400, via the columnar scan
#
# at SHIPPED DEFAULTS, on a table whose 400 rows are all live, when the planner
# picked the index-only scan on its own. It does not always: on current builds
# count(*) at defaults plans a Custom Scan (PgColumnarScan), so the two
# shipped-defaults checks below need not reach the index. The arms force the
# index, and assert that the plan names it.
#
# The controls carry the argument. Without them a failing arm would only say that
# index churn loses rows in general, which is not the claim:
#
#   * churn on the INDEXED column makes index_unchanged_by_update() false, so no
#     bottom-up pass runs. Must stay complete.
#   * a heap table with a second index, so HOT is defeated and its index takes
#     the same indexUnchanged hint. Must stay complete.
#
# Usage:  test/index_delete_liveness.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

N=400
ROUNDS=12

# CHURNED is left holding the rows the churn updated, which the arms carry in
# their readings. The churn used to throw away all its output, so a churn that
# errored updated nothing, the index stayed complete, and all nine checks passed
# (measured, with the UPDATE naming a column that does not exist).
CHURNED=""
churn() {	# churn <table> <set-clause> <COMMIT|ROLLBACK>
	local out
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -Atq -c "
		BEGIN;
		DO \$\$ DECLARE n bigint := 0; c bigint;
		BEGIN FOR i IN 1..$ROUNDS LOOP UPDATE $1 SET $2; GET DIAGNOSTICS c = ROW_COUNT; n := n + c; END LOOP;
		RAISE NOTICE 'churned %', n; END \$\$;
		$3;" 2>&1)"
	CHURNED="$(printf '%s\n' "$out" | sed -n 's/.*NOTICE:  churned \([0-9]*\).*/\1/p' | tail -1)"
	[ -n "$CHURNED" ] || CHURNED="none"
}
CHURN_WANT=$((N * ROUNDS))
# num_or <side> <reading> -- the reading when it is a number, else the side's name
# and what it read. truth and viaindex print psql's last line, so a failed read
# is its error text: carried bare, two reads that fail the same way agreed and
# arm D passed with neither taken (measured, with every row group past the
# churn's unreadable). Tagged by side, two failures can never agree.
num_or() { pgc_is_number "$2" && echo "$2" || echo "$1 unread [$2]"; }
# The truth: a plan that cannot use the index at all.
truth() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -Atq \
		-c "SET enable_indexscan=off; SET enable_bitmapscan=off; SET enable_indexonlyscan=off;" \
		-c "SELECT count(*) FROM $1 WHERE a = 1;" 2>&1 | tail -1
}
# The same question through the index.
viaindex() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -Atq \
		-c "SET enable_seqscan=off; SET enable_indexonlyscan=off; SET pgcolumnar.enable_custom_scan=off;" \
		-c "SELECT count(b) FROM $1 WHERE a = 1;" 2>&1 | tail -1
}
viaindexplan() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -Atq \
		-c "SET enable_seqscan=off; SET enable_indexonlyscan=off; SET pgcolumnar.enable_custom_scan=off;" \
		-c "EXPLAIN (COSTS OFF) SELECT count(b) FROM $1 WHERE a = 1;" 2>&1 | tr '\n' ' '
}

mk() {	# mk <table> <USING clause> [extra index]
	psql_run "CREATE TABLE $1(a int, b text) $2;"
	psql_run "CREATE INDEX ${1}i ON $1(a);"
	[ -n "${3:-}" ] && psql_run "CREATE INDEX ${1}i2 ON $1(b);"
	psql_run "INSERT INTO $1 SELECT 1, repeat('x',20) FROM generate_series(1,$N);"
}

# ---- arm A: columnar, churn on the NON-indexed column, ROLLBACK -------------
mk ca "USING pgcolumnar"
check_num "premise: arm A holds 400 rows before the churn" "$(truth ca)" "$N"
churn ca "b = b || 'y'" ROLLBACK; a_c="$CHURNED"
check "premise: arm A really plans an Index Scan, so the arm measures the index" \
	"$(case "$(viaindexplan ca)" in *"Index Scan"*) echo yes ;; *) echo "no: $(viaindexplan ca)" ;; esac)" "yes"
a_t="$(truth ca)"; a_i="$(viaindex ca)"
echo "-- arm A columnar, non-indexed churn, ROLLBACK: rows=$a_t via-index=$a_i"
check_text "arm A: a ROLLBACK leaves every live row reachable through the index" "churned $a_c: $(num_or index "$a_i")" "churned $CHURN_WANT: $(num_or truth "$a_t")"

# ---- control B: columnar, churn ON the indexed column -----------------------
mk cb "USING pgcolumnar"
churn cb "a = a" ROLLBACK; b_c="$CHURNED"
b_t="$(truth cb)"; b_i="$(viaindex cb)"
echo "-- control B columnar, INDEXED-column churn: rows=$b_t via-index=$b_i"
check_text "control B: indexed-column churn triggers no bottom-up pass and loses nothing" "churned $b_c: $(num_or index "$b_i")" "churned $CHURN_WANT: $(num_or truth "$b_t")"

# ---- control C: heap under the same hint ------------------------------------
mk hb "" second
churn hb "b = b || 'y'" ROLLBACK; c_c="$CHURNED"
c_t="$(truth hb)"; c_i="$(viaindex hb)"
echo "-- control C heap, same indexUnchanged hint: rows=$c_t via-index=$c_i"
check_text "control C: the heap loses nothing under the same churn" "churned $c_c: $(num_or index "$c_i")" "churned $CHURN_WANT: $(num_or truth "$c_t")"

# ---- arm D: committed churn, and the answer at SHIPPED DEFAULTS -------------
# An abort is not required, and no setting is required to get a wrong answer.
mk cc "USING pgcolumnar"
churn cc "b = b || 'y'" COMMIT; d_c="$CHURNED"
d_t="$(truth cc)"; d_i="$(viaindex cc)"
echo "-- arm D columnar, non-indexed churn, COMMIT: rows=$d_t via-index=$d_i"
check_text "arm D: a COMMIT leaves every live row reachable through the index" "churned $d_c: $(num_or index "$d_i")" "churned $CHURN_WANT: $(num_or truth "$d_t")"

def_star="$(q "SELECT count(*) FROM cc WHERE a = 1;")"
def_b="$(q "SELECT count(b) FROM cc WHERE a = 1;")"
echo "-- at shipped defaults: count(*)=$def_star  count(b)=$def_b  (truth $d_t)"
echo "-- the plan count(*) gets at defaults: $(q "EXPLAIN (COSTS OFF) SELECT count(*) FROM cc WHERE a = 1;" | tr '\n' ' ')"
check_num "at shipped defaults count(*) is right" "$def_star" "$d_t"
check_num "at shipped defaults the table answers the same to count(*) and count(b)" \
	"$def_star" "$def_b"

# ---- heap oracle on the same shape ------------------------------------------
mk hc "" second
churn hc "b = b || 'y'" COMMIT; h_c="$CHURNED"
check_text "oracle: the heap answers count(*) correctly after the same churn" \
	"churned $h_c: $(q "SELECT count(*) FROM hc WHERE a = 1;")" "churned $CHURN_WANT: $N"

pgc_summary
