#!/usr/bin/env bash
#
# A statement does not see the rows it writes, even after it has flushed some of
# them into row groups part-way through (#1341). A scan reads its row-group list
# once, when it starts; groups the same statement flushes later are not in it. A
# reader that picked them up would make INSERT INTO t SELECT FROM t feed on its own
# output and UPDATE t reach a row twice -- the Halloween problem.
#
# Each shape runs on a columnar table and on heap, and heap is the answer. The
# columnar table writes row groups of 1000 rows, so every statement below crosses
# several flushes. Every statement runs under a statement_timeout: a scan that fed
# on its own output would otherwise never end.
#
# Usage:  test/statement_self_visibility.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

SET="SET pgcolumnar.stripe_row_limit = 1000; SET statement_timeout = '60s';"
groups() {	# groups TABLE -> how many row groups the table has
	q "SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = pgcolumnar.get_storage_id('$1')"
}
run() {	# run SQL: on both tables, with %t standing for the table; -> ok or the errors
	# Both tables, always: stopping at a columnar error would leave heap unchanged
	# too, and the "as heap does" arms after it would compare two untouched tables.
	local am out t errs=""
	for am in pgcolumnar heap; do
		t="sv_$am"
		out="$(psql_run "$SET ${1//%t/$t}" 2>&1)" || errs="$errs$am: $(grep -m1 ERROR <<<"$out") "
	done
	echo "${errs:-ok}"
}
answer() {	# answer SQL -> the last line psql prints, or its ERROR line if the query failed
	local out
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-v ON_ERROR_STOP=1 -qAt -c "$1" 2>&1)" || { grep -m1 ERROR <<<"$out"; return; }
	tail -1 <<<"$out"
}
same() {	# same SQL -> "same" when both tables answer alike, else both answers
	local c h
	c="$(answer "$SET ${1//%t/sv_pgcolumnar}")"; h="$(answer "$SET ${1//%t/sv_heap}")"
	[ "$c" = "$h" ] && echo same || echo "columnar=$c heap=$h"
}
fresh() {	# both tables made again, holding rows 1..5000. Every shape starts here,
	# so a shape that goes wrong cannot fail the ones after it (OffgridwithJD,
	# review of #1416).
	local am
	for am in pgcolumnar heap; do
		psql_run "$SET DROP TABLE IF EXISTS sv_$am; CREATE TABLE sv_$am (id int, v int) USING $am;
		          INSERT INTO sv_$am SELECT g, 0 FROM generate_series(1, 5000) g;"
	done
}

fresh
before="$(groups sv_pgcolumnar)"

# ---- INSERT INTO t SELECT FROM t ------------------------------------------------
check "INSERT INTO t SELECT FROM t runs to completion" "$(run "INSERT INTO %t SELECT id + 5000, v FROM %t;")" "ok"
after="$(groups sv_pgcolumnar)"
check "the INSERT flushed row groups while its scan was still reading" \
	"$([ "$((after - before))" -ge 2 ] && echo yes || echo "no ($before groups before, $after after)")" "yes"
check "and it read only the rows that were there when it started, as heap does" \
	"$(same "SELECT count(*) || '|' || count(DISTINCT id) || '|' || max(id) FROM %t")" "same"

# ---- UPDATE of every row --------------------------------------------------------
fresh
check "UPDATE of every row runs to completion" "$(run "UPDATE %t SET v = v + 1;")" "ok"
check "and it changed each row exactly once, as heap does" \
	"$(same "SELECT count(*) || '|' || min(v) || '|' || max(v) FROM %t")" "same"

# ---- both again, in one transaction after an earlier write ----------------------
fresh
check "in one transaction, an INSERT, an UPDATE of every row and an INSERT SELECT run to completion" \
	"$(run "BEGIN; INSERT INTO %t SELECT g, 0 FROM generate_series(20001, 23000) g;
	        UPDATE %t SET v = v + 1; INSERT INTO %t SELECT id + 100000, v FROM %t; COMMIT;")" "ok"
check "and each statement saw the earlier ones but not itself, as heap does" \
	"$(same "SELECT count(*) || '|' || count(DISTINCT id) || '|' || min(v) || '|' || max(v) || '|' || sum(v) FROM %t")" "same"

# ---- a data-modifying CTE --------------------------------------------------------
# No WHERE on the scan: every row the INSERT writes would pass it, so a scan that fed
# on its own output would meet those rows (OffgridwithJD, review of #1416).
fresh
check "an INSERT in a WITH clause reading its own table returns the rows it inserted, as heap does" \
	"$(same "WITH w AS (INSERT INTO %t SELECT id + 1000000, v FROM %t RETURNING 1)
	         SELECT count(*) FROM w")" "same"
check "and the table holds them once, as heap does" "$(same "SELECT count(*) FROM %t")" "same"

# ---- a statement reading through an index the rows it wrote (#2247) ------------
# A self-join UPDATE, DELETE or INSERT SELECT reads its own table on the inner side
# of a join, or in a subquery, while it writes. Heap's inner scan sees the table as
# it was when the statement started. A columnar index fetch used to see the
# statement's own new row versions, from the write buffer or from row groups the
# statement flushed, and to lose the old versions it had marked deleted. Heap is
# the answer. Each shape ends in ROLLBACK, so the tables stay as fresh_pk made them.
# The tables hold 40000 rows with a primary key and a covering index; the suite's
# SET writes row groups of 1000 rows, so a statement that writes more than that
# flushes part-way through.
fresh_pk() {	# both tables, (id PRIMARY KEY, pad) holding pad = id for 1..40000
	local am
	for am in pgcolumnar heap; do
		psql_run "$SET DROP TABLE IF EXISTS sv_$am CASCADE; DROP FUNCTION IF EXISTS seen_sv_$am();
		          CREATE TABLE sv_$am (id int PRIMARY KEY, pad bigint) USING $am;
		          INSERT INTO sv_$am SELECT g, g FROM generate_series(1, 40000) g;
		          CREATE UNIQUE INDEX sv_${am}_cover ON sv_$am (id) INCLUDE (pad);
		          CREATE FUNCTION seen_sv_$am() RETURNS bigint VOLATILE LANGUAGE sql
		            AS 'SELECT count(*) FROM sv_$am WHERE id > 100000';
		          DROP FUNCTION IF EXISTS sib_sv_$am();
		          CREATE FUNCTION sib_sv_$am() RETURNS trigger LANGUAGE plpgsql AS \$\$
		            BEGIN
		              INSERT INTO sv_$am VALUES (-NEW.id,
		                (SELECT count(*) FROM sv_$am WHERE id > 100000) * 1000
		                + (SELECT count(*) FROM sv_$am WHERE id < 0));
		              RETURN NEW;
		            END \$\$;"
		psql_run "VACUUM ANALYZE sv_$am;"
	done
	psql_run "CREATE OR REPLACE FUNCTION sv_try(q text) RETURNS text LANGUAGE plpgsql AS \$\$
	          BEGIN EXECUTE q; RETURN 'ok';
	          EXCEPTION WHEN unique_violation THEN RETURN 'unique_violation'; END \$\$;"
}
plan_has() {	# plan_has SQL PATTERN -> yes when the columnar plan of SQL matches PATTERN
	local out
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-qAt -c "$SET ${1//%t/sv_pgcolumnar}" 2>&1)"
	grep -qE "$2" <<<"$out" && echo yes || echo "no: $(tr '\n' ' ' <<<"$out" | cut -c1-300)"
}
NL="SET LOCAL enable_hashjoin = off; SET LOCAL enable_mergejoin = off;"
IDX="SET LOCAL enable_seqscan = off; SET LOCAL enable_bitmapscan = off;"
CSOFF="SET LOCAL pgcolumnar.enable_custom_scan = off;"
UPD="UPDATE %t SET pad = t2.pad + 1000000 FROM %t t2 WHERE t2.id = %t.id - 1 AND %t.id <= 10;"
SHOW="SELECT string_agg(id || ':' || pad, ' ' ORDER BY id) FROM %t WHERE id <= 11;"
BIG="UPDATE %t SET pad = t2.pad + 1000000 FROM %t t2 WHERE t2.id = %t.id - 1 AND %t.id <= 3000;
     SELECT count(*) FILTER (WHERE pad <> id + 999999) || '|' || min(pad) || '|' || max(pad) FROM %t WHERE id BETWEEN 2 AND 3000;"
SUBQ="UPDATE %t SET pad = (SELECT t2.pad FROM %t t2 WHERE t2.id = %t.id - 1) + 1000000 WHERE %t.id BETWEEN 2 AND 10;"
fresh_pk
for cs in off on; do
	check "premise: the self-join UPDATE runs as a nested loop over index scans (custom scan $cs)" \
		"$(plan_has "BEGIN; SET LOCAL pgcolumnar.enable_custom_scan = $cs; $NL $IDX EXPLAIN (COSTS OFF) $UPD ROLLBACK;" \
			'Nested Loop')" "yes"
	check "a self-join UPDATE through index scans reads each row's old value, as heap does (custom scan $cs)" \
		"$(same "BEGIN; SET LOCAL pgcolumnar.enable_custom_scan = $cs; $NL $IDX $UPD $SHOW ROLLBACK;")" "same"
done
check "premise: the self-join UPDATE runs as a merge join" \
	"$(plan_has "BEGIN; $CSOFF SET LOCAL enable_hashjoin = off; SET LOCAL enable_nestloop = off; EXPLAIN (COSTS OFF) $UPD ROLLBACK;" \
		'Merge Join')" "yes"
check "a self-join UPDATE through a merge join reads each row's old value, as heap does" \
	"$(same "BEGIN; $CSOFF SET LOCAL enable_hashjoin = off; SET LOCAL enable_nestloop = off; $UPD $SHOW ROLLBACK;")" "same"
check "a self-join DELETE through index scans deletes the rows heap deletes" \
	"$(same "BEGIN; $CSOFF $NL $IDX DELETE FROM %t USING %t t2 WHERE t2.id = %t.id - 1 AND %t.id <= 10;
	         SELECT count(*) || '|' || string_agg(id::text, ',' ORDER BY id) FROM %t WHERE id <= 11; ROLLBACK;")" "same"
# Bounded by id < 200000, so a scan that fed on its own output stops after one more
# pass instead of running until the statement_timeout.
for limit in 150000 1000; do
	check "an INSERT SELECT from its own table through an index range scan inserts each row once, as heap does (stripe_row_limit $limit)" \
		"$(same "BEGIN; SET LOCAL statement_timeout = '20s'; SET LOCAL pgcolumnar.stripe_row_limit = $limit; $CSOFF $IDX
		         INSERT INTO %t SELECT id + 100000, pad FROM %t WHERE id > 30000 AND id < 200000;
		         SELECT count(*) || '|' || max(id) FROM %t; ROLLBACK;")" "same"
done
check "an UPDATE with a correlated subquery through an index scan reads the old value, as heap does" \
	"$(same "BEGIN; $CSOFF $IDX SET LOCAL enable_indexonlyscan = off; $SUBQ $SHOW ROLLBACK;")" "same"
check "premise: the correlated subquery runs as an index-only scan" \
	"$(plan_has "BEGIN; $CSOFF $IDX EXPLAIN (COSTS OFF) $SUBQ ROLLBACK;" 'Index Only Scan')" "yes"
check "an UPDATE with a correlated subquery through an index-only scan reads the old value, as heap does" \
	"$(same "BEGIN; $CSOFF $IDX $SUBQ $SHOW ROLLBACK;")" "same"
check "a self-join UPDATE chained across row groups reads each row's old value, as heap does" \
	"$(same "BEGIN; $CSOFF $NL $IDX UPDATE %t SET pad = t2.pad + 1000000 FROM %t t2 WHERE t2.id = %t.id - 10000 AND %t.id % 10000 = 5;
	         SELECT string_agg(id || ':' || pad, ' ' ORDER BY id) FROM %t WHERE id % 10000 = 5; ROLLBACK;")" "same"
check "a 3000-row self-join UPDATE through index scans reads each row's old value, with row groups flushed part-way, as heap does" \
	"$(same "BEGIN; $CSOFF $NL $IDX $BIG ROLLBACK;")" "same"
check "a 3000-row self-join UPDATE through index scans reads each row's old value, with every new row still buffered, as heap does" \
	"$(same "BEGIN; SET LOCAL pgcolumnar.stripe_row_limit = 150000; $CSOFF $NL $IDX $BIG ROLLBACK;")" "same"
check "after an earlier UPDATE in the same transaction, a self-join UPDATE sees that UPDATE but not its own rows, as heap does" \
	"$(same "BEGIN; $CSOFF $NL $IDX UPDATE %t SET pad = pad + 7 WHERE id <= 10; $UPD $SHOW ROLLBACK;")" "same"
check "an UPDATE that joins the same target row twice through an index changes it once, as heap does" \
	"$(same "BEGIN; $CSOFF $NL $IDX UPDATE %t SET pad = pad + 1 FROM (VALUES (1), (1), (2)) v(x) WHERE %t.id = v.x;
	         SELECT string_agg(id || ':' || pad, ' ' ORDER BY id) FROM %t WHERE id <= 3; ROLLBACK;")" "same"
check "a cursor fetched after a later UPDATE reads the old values through an index scan, as heap does" \
	"$(same "BEGIN; $CSOFF $IDX DECLARE c CURSOR FOR SELECT sum(pad) FROM %t WHERE id <= 10;
	         UPDATE %t SET pad = 0 WHERE id <= 10; FETCH ALL FROM c; ROLLBACK;")" "same"

# A BEFORE ROW trigger that inserts into its own table writes under a new command
# for each outer row, so the write buffer holds runs of rows from different
# commands. Each trigger call must see the outer rows and its own earlier rows,
# but not the row it is writing.
check "a BEFORE ROW trigger inserting into its own table sees the statement's earlier rows and its own earlier rows, as heap does" \
	"$(same "BEGIN; $CSOFF $IDX CREATE TRIGGER sib BEFORE INSERT ON %t FOR EACH ROW WHEN (NEW.id > 0) EXECUTE FUNCTION sib_%t();
	         INSERT INTO %t SELECT 100000 + g, 0 FROM generate_series(1, 5) g;
	         SELECT string_agg(pad::text, ',' ORDER BY id DESC) FROM %t WHERE id < 0; ROLLBACK;")" "same"

# ---- shapes that must keep working (#2247 guards) ------------------------------
# A plan that reads the inner side once, a nested read of earlier rows, and a
# parallel scan after this transaction's flushed writes. All match heap before the
# fix; the fix must not break them.
check "a VOLATILE function that reads the table inside an INSERT on it sees the statement's earlier rows, as heap does" \
	"$(same "BEGIN; $CSOFF INSERT INTO %t SELECT g, seen_%t() FROM generate_series(100001, 100005) g;
	         SELECT string_agg(pad::text, ',' ORDER BY id) FROM %t WHERE id > 100000; ROLLBACK;")" "same"
check "a self-join UPDATE through a hash join reads each row's old value, as heap does" \
	"$(same "BEGIN; $CSOFF SET LOCAL enable_mergejoin = off; SET LOCAL enable_nestloop = off; $UPD $SHOW ROLLBACK;")" "same"
check "a self-join UPDATE through a materialized sequential inner scan reads each row's old value, as heap does" \
	"$(same "BEGIN; $CSOFF $NL SET LOCAL enable_indexscan = off; SET LOCAL enable_indexonlyscan = off; SET LOCAL enable_bitmapscan = off;
	         $UPD $SHOW ROLLBACK;")" "same"
check "an INSERT SELECT from its own table through a sequential scan inserts each row once, as heap does" \
	"$(same "BEGIN; $CSOFF INSERT INTO %t SELECT id + 100000, pad FROM %t; SELECT count(*) || '|' || max(id) FROM %t; ROLLBACK;")" "same"
# Snapshots that are not MVCC see every write: the unique check's dirty snapshot
# (#212) and the AFTER trigger's re-fetch under SnapshotAny (#179).
check "a duplicate key inserted twice in one statement is still refused, as heap does" \
	"$(same "BEGIN; $CSOFF SELECT sv_try('INSERT INTO %t VALUES (900001, 1), (900001, 2)'); ROLLBACK;")" "same"
check "an AFTER ROW trigger of a self-join UPDATE still reads OLD and NEW, as heap does" \
	"$(same "BEGIN; $CSOFF $NL $IDX CREATE TEMP TABLE trig_log (o bigint, n bigint);
	         CREATE FUNCTION pg_temp.trig_note() RETURNS trigger LANGUAGE plpgsql AS \$\$
	           BEGIN INSERT INTO trig_log VALUES (OLD.pad, NEW.pad); RETURN NULL; END \$\$;
	         CREATE TRIGGER au AFTER UPDATE ON %t FOR EACH ROW EXECUTE FUNCTION pg_temp.trig_note();
	         $UPD SELECT count(*) || '|' || sum(o) || '|' || sum(n) FROM trig_log; ROLLBACK;")" "same"
PAR="SET LOCAL max_parallel_workers_per_gather = 2; SET LOCAL parallel_setup_cost = 0; SET LOCAL parallel_tuple_cost = 0;
     SET LOCAL min_parallel_table_scan_size = 0; SET LOCAL enable_indexscan = off; SET LOCAL enable_indexonlyscan = off;
     SET LOCAL enable_bitmapscan = off;"
PARW="UPDATE %t SET pad = pad + 1 WHERE id % 3 = 0; INSERT INTO %t SELECT g, g FROM generate_series(40001, 50000) g;"
for cs in off on; do
	check "premise: the count after this transaction's writes runs in parallel workers (custom scan $cs)" \
		"$(plan_has "BEGIN; SET LOCAL pgcolumnar.enable_custom_scan = $cs; $PARW $PAR
		             EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) SELECT count(*), sum(pad) FROM %t; ROLLBACK;" \
			'Workers Launched: [1-9]')" "yes"
	check "a parallel worker refuses a leader whose own writes it could not hide (custom scan $cs)" \
		"$(answer "$SET BEGIN; SET LOCAL pgcolumnar.enable_custom_scan = $cs; SET LOCAL pgcolumnar.debug_registry_cid_skew = 1;
		           $PAR SELECT count(*) || '|' || sum(pad) FROM sv_pgcolumnar; ROLLBACK;")" \
		"ERROR:  columnar: a parallel worker cannot hide this transaction's own writes"
	check "a parallel scan after this transaction's flushed writes sees them, as heap does (custom scan $cs)" \
		"$(same "BEGIN; SET LOCAL pgcolumnar.enable_custom_scan = $cs; $PARW $PAR
		         SELECT count(*) || '|' || sum(pad) FROM %t; ROLLBACK;")" "same"
done

pgc_summary
