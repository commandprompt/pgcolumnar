#!/usr/bin/env bash
#
# Rolling back to a savepoint, or failing a statement, after rows have already been
# flushed into row groups (#1341). A flushed group is a catalog row written under
# the subtransaction, so the abort must hide it; rows still buffered, and buffered
# delete marks, must be thrown away; and the group-list memo built while the
# savepoint was live must not keep serving the aborted groups.
#
# Each shape runs on a columnar table and on heap, and heap is the answer. The
# columnar table writes row groups of 1000 rows, so every write below crosses
# several flushes. Each transaction prints labelled values (label=value), and the
# two tables are compared label by label, inside the transaction and after it.
#
# EVERY SHAPE STARTS FROM FRESH TABLES. Otherwise a shape that goes wrong poisons
# the ones after it, and they fail for its rows rather than their own (OffgridwithJD,
# review of #1430).
#
# Usage:  test/savepoint_flush.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

SET="SET pgcolumnar.stripe_row_limit = 1000; SET statement_timeout = '60s';"
AMS="pgcolumnar heap"
groups() {	# groups -> how many row groups the columnar table has, as this session sees
	echo "SELECT 'groups=' || count(*) FROM pgcolumnar.row_group
	      WHERE storage_id = pgcolumnar.get_storage_id('sp_pgcolumnar');"
}
# What every reader sees: a plain sequential scan through the table AM, the custom
# scan without vectorization, the vectorized count, and the primary key's index.
# Each is a pair of the settings that choose its path and its query.
R_SEQ=("SET pgcolumnar.enable_custom_scan = off;"
       "SELECT 'seq=' || count(*) || '|' || coalesce(sum(id), 0) || '|' || coalesce(sum(v), 0) FROM %t;")
R_SCAN=("SET pgcolumnar.enable_vectorization = off;"
        "SELECT 'scan=' || count(*) || '|' || coalesce(sum(id), 0) || '|' || coalesce(sum(v), 0) FROM %t;")
R_VEC=("SET pgcolumnar.enable_vectorization = on;"
       "SELECT 'vec=' || count(*) || '|' || coalesce(sum(id), 0) FROM %t;")
R_IDX=("SET pgcolumnar.enable_custom_scan = off; SET enable_seqscan = off; SET enable_bitmapscan = off;"
       "SELECT 'idx=' || count(*) || '|' || coalesce(sum(v), 0) FROM %t WHERE id > 1000;")
RESET="RESET pgcolumnar.enable_custom_scan; RESET pgcolumnar.enable_vectorization;
       RESET enable_seqscan; RESET enable_bitmapscan;"
READS=""
for r in R_SEQ R_SCAN R_VEC R_IDX; do
	declare -n rr="$r"; READS="$READS ${rr[0]} ${rr[1]} $RESET"
done
unset -n rr
READS_IN="${READS//\'seq=/\'in_seq=}"; READS_IN="${READS_IN//\'scan=/\'in_scan=}"
READS_IN="${READS_IN//\'vec=/\'in_vec=}"; READS_IN="${READS_IN//\'idx=/\'in_idx=}"
declare -A OUT
xact() {	# xact SQL: run SQL on both tables; %t is the table, %r READS, %i READS_IN
	# Both tables, always: stopping at a columnar error would leave heap unchanged
	# too, and the "as heap does" arms after it would compare two untouched tables.
	local am sql
	for am in $AMS; do
		sql="${1//%r/$READS}"; sql="${sql//%i/$READS_IN}"
		OUT[$am]="$(psql_run_at "$SET ${sql//%t/sp_$am}" 2>&1)"
	done
}
psql_run_at() {	# psql_run_at SQL: a script, one statement at a time, each value a line
	# Not -c: psql sends a -c string as one query, and an error in it skips the
	# rest, so the ROLLBACK TO after a failing statement would never run.
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -v ON_ERROR_STOP=0 -qAt <<<"$1"
}
pick() {	# pick AM LABEL -> the value the last xact printed under LABEL for AM
	grep -m1 "^$2=" <<<"${OUT[$1]}" | cut -d= -f2-
}
same() {	# same LABEL... -> "same" when both tables printed alike under every LABEL
	local l c h diff=""
	for l in "$@"; do
		c="$(pick pgcolumnar "$l")"; h="$(pick heap "$l")"
		[ -n "$h" ] || { diff="$diff $l: heap printed nothing (${OUT[heap]:0:200})"; continue; }
		[ "$c" = "$h" ] || diff="$diff $l: columnar=[$c] heap=[$h]"
	done
	echo "${diff:-same}"
}
errors() {	# errors [MSG] -> "as expected" when each table's last xact raised one
	# error containing MSG (no MSG: none at all), else what each table raised
	local am n e want=0 bad=""
	[ -n "${1:-}" ] && want=1
	for am in $AMS; do
		n="$(grep -c ERROR <<<"${OUT[$am]}")"; e="$(grep -m1 ERROR <<<"${OUT[$am]}")"
		{ [ "$n" = "$want" ] && { [ "$want" = 0 ] || [[ "$e" == *"$1"* ]]; }; } ||
			bad="$bad $am: $n errors, first [$e]"
	done
	echo "${bad:-as expected}"
}
grew() {	# grew BEFORE AFTER -> yes when the columnar table gained two groups or more
	[ -n "$1" ] && [ -n "$2" ] && [ "$(($2 - $1))" -ge 2 ] && echo yes || echo "no ($1 groups before, $2 after)"
}

fresh() {	# fresh [N]: both tables dropped and made again, holding rows 1..N (2000)
	local am
	for am in $AMS; do
		psql_run "$SET DROP TABLE IF EXISTS sp_$am; CREATE TABLE sp_$am (id int PRIMARY KEY, v int) USING $am;
		          INSERT INTO sp_$am SELECT g, 0 FROM generate_series(1, ${1:-2000}) g;"
	done
}
# catalog_ins TAG -> this transaction's inserts so far into row_group and delete_vector,
# as rg_TAG= and dv_TAG=. pg_stat_xact_all_tables still counts a subtransaction's
# inserts after it aborts, so it shows what a rolled-back write flushed. Read once
# after BEGIN ("b") and once after ROLLBACK TO ("a"); the premise is the difference,
# so the reading cannot include earlier work in the same session, as the twin's one
# connection does (OffgridwithJD, review of #1430).
catalog_ins() {
	echo "SELECT 'rg_$1=' || n_tup_ins FROM pg_stat_xact_all_tables WHERE relid = 'pgcolumnar.row_group'::regclass;
	      SELECT 'dv_$1=' || n_tup_ins FROM pg_stat_xact_all_tables WHERE relid = 'pgcolumnar.delete_vector'::regclass;"
}
flushed() {	# flushed -> yes when both catalogs gained two rows or more between b and a
	local rg dv k
	for k in rg_b dv_b rg_a dv_a; do
		local "$k=$(grep -m1 "^$k=" <<<"${OUT[pgcolumnar]}" | cut -d= -f2)"
	done
	rg=$(( ${rg_a:-0} - ${rg_b:-0} )); dv=$(( ${dv_a:-0} - ${dv_b:-0} ))
	[ -n "$rg_a" ] && [ -n "$rg_b" ] && [ "$rg" -ge 2 ] && [ "$dv" -ge 2 ] && echo yes ||
		echo "no (row_group inserts [$rg], delete_vector inserts [$dv])"
}
# sp_probe(table, id): reads the table when it reaches row 2500 -- a scan starting
# flushes the delete marks buffered so far -- and divides by zero at row 5000.
psql_run "CREATE FUNCTION sp_probe(t regclass, id int) RETURNS int LANGUAGE plpgsql AS \$\$
          BEGIN
              IF id = 2500 THEN EXECUTE format('SELECT count(*) FROM %s', t); END IF;
              RETURN 1 / (5000 - id) * 0;
          END \$\$;"
fresh

# ---- the readers take the paths they are named for --------------------------------
plan() {	# plan READER -> the columnar table's plan for that reader's query
	declare -n rr="$1"
	q "$SET ${rr[0]} EXPLAIN (COSTS OFF) ${rr[1]//%t/sp_pgcolumnar}"
}
check "the seq reader is a plain sequential scan, not the custom scan" \
	"$(plan R_SEQ | grep -c 'Seq Scan')|$(plan R_SEQ | grep -c 'Custom Scan')" "1|0"
check "the scan reader is the custom scan with no vectorized aggregate" \
	"$(plan R_SCAN | grep -c 'Custom Scan')|$(plan R_SCAN | grep -c 'Columnar Vectorized Aggregates')" "1|0"
check "the vec reader is a vectorized aggregate" \
	"$(plan R_VEC | grep -c 'Columnar Vectorized Aggregates')" "1"
check "the idx reader reads the primary key's index" \
	"$(plan R_IDX | grep -c 'Index Scan\|Index Only Scan')|$(plan R_IDX | grep -c 'Seq Scan\|Custom Scan')" "1|0"

# ---- a flushed INSERT rolled back to a savepoint ---------------------------------
fresh
xact "BEGIN; $(groups) SAVEPOINT s;
      INSERT INTO %t SELECT g, 1 FROM generate_series(10001, 15000) g;
      $(groups) SELECT 'inside=' || count(*) FROM %t;
      ROLLBACK TO s; %r COMMIT;"
check "a savepoint's INSERT runs, and ROLLBACK TO it, on both tables" "$(errors)" "as expected"
check "the savepoint's INSERT flushed row groups before ROLLBACK TO" \
	"$(grew "$(grep -m1 '^groups=' <<<"${OUT[pgcolumnar]}" | cut -d= -f2)" \
	        "$(grep '^groups=' <<<"${OUT[pgcolumnar]}" | sed -n 2p | cut -d= -f2)")" "yes"
check "inside the savepoint, every reader saw the INSERT, as heap does" "$(same inside)" "same"
check "after ROLLBACK TO, in the same transaction, no reader sees the savepoint's rows, as heap does" \
	"$(same seq scan vec idx)" "same"
xact "%r"
check "and after COMMIT, no reader sees them, as heap does" "$(same seq scan vec idx)" "same"

# ---- the same, under one snapshot -----------------------------------------------
# At REPEATABLE READ the reads after ROLLBACK TO use the snapshot the reads inside
# the savepoint used, so only the abort itself can hide the savepoint's groups.
# (A scan held open across the abort, through a cursor, is native_fetch_group_memo's.)
fresh
xact "BEGIN ISOLATION LEVEL REPEATABLE READ; SELECT 1 FROM %t LIMIT 1; SAVEPOINT s;
      INSERT INTO %t SELECT g, 1 FROM generate_series(10001, 15000) g;
      %i ROLLBACK TO s; %r COMMIT;"
check "at REPEATABLE READ, a savepoint's INSERT runs, and ROLLBACK TO it, on both tables" "$(errors)" "as expected"
check "at REPEATABLE READ, inside the savepoint, every reader saw the INSERT, as heap does" \
	"$(same in_seq in_scan in_vec in_idx)" "same"
check "at REPEATABLE READ, after ROLLBACK TO, no reader sees the savepoint's rows, as heap does" \
	"$(same seq scan vec idx)" "same"

# ---- the same unique keys again after ROLLBACK TO --------------------------------
fresh
xact "BEGIN; SAVEPOINT s;
      INSERT INTO %t SELECT g, 2 FROM generate_series(10001, 15000) g;
      ROLLBACK TO s;
      INSERT INTO %t SELECT g, 3 FROM generate_series(10001, 15000) g;
      %r COMMIT;"
check "after ROLLBACK TO, the same unique keys insert again in the same transaction, as on heap" "$(errors)" "as expected"
check "and every reader sees each key once, with the second insert's values, as heap does" \
	"$(same seq scan vec idx)" "same"
xact "%r"
check "and after COMMIT, each key is there once, as heap does" "$(same seq scan vec idx)" "same"

# ---- a large UPDATE rolled back to a savepoint -----------------------------------
fresh 5000
xact "BEGIN; $(catalog_ins b) SAVEPOINT s;
      UPDATE %t SET v = v + 10;
      SELECT 'inside=' || count(*) || '|' || sum(v) FROM %t;
      ROLLBACK TO s; $(catalog_ins a) %r COMMIT;"
check "an UPDATE of every row in a savepoint runs, and ROLLBACK TO it, on both tables" "$(errors)" "as expected"
check "the savepoint's UPDATE flushed new versions and delete marks before ROLLBACK TO" "$(flushed)" "yes"
check "inside the savepoint, the UPDATE changed each row once, as heap does" "$(same inside)" "same"
check "after ROLLBACK TO, in the same transaction, every reader sees the rows as they were, as heap does" \
	"$(same seq scan vec idx)" "same"
xact "%r"
check "and after COMMIT, the rows are as they were, as heap does" "$(same seq scan vec idx)" "same"

# ---- a statement that fails after it has flushed ---------------------------------
# Rows are generated in order, so the division by zero comes at the 3500th row,
# after three flushes. The same statement without the failing row shows the flushes.
FAIL="INSERT INTO %t SELECT g, 1 / (g - 23500) FROM generate_series(20001, 24000) g;"
fresh
xact "BEGIN; $(groups) INSERT INTO %t SELECT g, 1 / (g - 99999) FROM generate_series(20001, 24000) g;
      $(groups) ROLLBACK;"
check "the failing INSERT's shape flushes row groups before its 3500th row" \
	"$(grew "$(grep -m1 '^groups=' <<<"${OUT[pgcolumnar]}" | cut -d= -f2)" \
	        "$(grep '^groups=' <<<"${OUT[pgcolumnar]}" | sed -n 2p | cut -d= -f2)")" "yes"

fresh
xact "$FAIL"
check "an INSERT that fails after flushing is refused on both tables" \
	"$(errors 'division by zero')" "as expected"
xact "%r"
check "and no reader sees any of its rows, as heap does" "$(same seq scan vec idx)" "same"

fresh
xact "BEGIN; SAVEPOINT s; $FAIL
      ROLLBACK TO s; INSERT INTO %t VALUES (30001, 5), (30002, 5);
      %r COMMIT;"
check "in a savepoint, an INSERT that fails after flushing raises its error and nothing else, on both tables" \
	"$(errors 'division by zero')" "as expected"
check "and in the same transaction every reader sees only the rows written after it, as heap does" \
	"$(same seq scan vec idx)" "same"
xact "%r"
check "and after COMMIT, only the rows written after the failed INSERT are there, as heap does" "$(same seq scan vec idx)" "same"

# An UPDATE of every row that fails at the last row it reaches (5000, the last in
# insert order). By then it has flushed new versions, and sp_probe's read at row 2500
# flushed the delete marks of the rows before it. The premise reads both catalogs'
# inserts right after ROLLBACK TO, before anything else in the transaction writes.
fresh 5000
xact "BEGIN; $(catalog_ins b) SAVEPOINT s;
      UPDATE %t SET v = v + sp_probe('%t', id);
      ROLLBACK TO s; $(catalog_ins a) UPDATE %t SET v = v + 100 WHERE id = 1;
      %r COMMIT;"
check "in a savepoint, an UPDATE of every row that fails at the last raises its error and nothing else, on both tables" \
	"$(errors 'division by zero')" "as expected"
check "the failing UPDATE flushed new versions and delete marks before it failed" "$(flushed)" "yes"
check "and in the same transaction every reader sees the rows as they were, plus the later UPDATE, as heap does" \
	"$(same seq scan vec idx)" "same"
xact "%r"
check "and after COMMIT, the rows are as they were plus the later UPDATE, as heap does" "$(same seq scan vec idx)" "same"

pgc_summary
