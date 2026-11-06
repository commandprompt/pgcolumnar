#!/usr/bin/env bash
#
# SERIALIZABLE refuses a write skew on a columnar table, as on heap (#1341). Two
# transactions each read what the other is about to change, then each writes
# something the other's read depended on. Run one after the other, the second would
# have read differently, so at SERIALIZABLE one of them must fail with
# serialization_failure (40001).
#
# Four shapes, each on a columnar table and on heap, and heap is the answer:
#   - the on-call doctors: both read who is on call, each takes a different doctor
#     off call. Once with the two rows in one row group, once in two;
#   - the empty ward: both see no one on call in a ward, each inserts someone.
#     Once read by a scan of the whole table (columnar's custom scan), once through
#     an index.
# In each, the first transaction commits, the second is refused by SSI with 40001
# ("could not serialize access due to read/write dependencies"), and the invariant
# holds afterwards. WHICH STATEMENT raises differs: on heap the second COMMIT; on
# columnar the second write, which waits for the first session to commit and then
# raises, after which its COMMIT returns cleanly (measured on PG17 and PG18 by
# OffgridwithJD, review of #1424). The checks hold the guarantee, not the step: each
# error is tagged with its step (r = BEGIN and read, w = write, c = COMMIT), and a
# failure prints them. At REPEATABLE READ the same interleavings must commit both
# transactions and break the invariant, on both tables -- the control that makes a
# 40001 from anything but SSI fail the checks above.
#
# Usage:  test/serializable.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

W="$PGC_WORKDIR"
session() {	# a psql reading statements from a fifo, reporting SQLSTATEs
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq -v VERBOSITY=verbose
}
# skew READ WRITE1 WRITE2 [LEVEL] -> "<s1>|<s2>", running at LEVEL (SERIALIZABLE)
#   s1: BEGIN LEVEL; READ               s2: BEGIN LEVEL; READ
#   s1: WRITE1                         s2: WRITE2
#   s1: COMMIT                         s2: COMMIT
# Each statement is sent once the session has answered the one before, which a
# marker line echoed after it proves.
skew() {
	local i n
	rm -f "$W/sk1" "$W/sk2"
	mkfifo "$W/sk1" "$W/sk2"
	session <"$W/sk1" >"$W/so1" 2>&1 & local p1=$!
	session <"$W/sk2" >"$W/so2" 2>&1 & local p2=$!
	exec 7>"$W/sk1" 8>"$W/sk2"
	step() {	# step FD OUT MARK SQL [NAME]: send SQL, wait until OUT shows MARK, or
		# until the session named NAME is seen waiting on a lock. On columnar a write
		# to a row group another session is changing waits for that session to
		# commit, as limitations.md says, so the next step must not wait for it.
		echo "$4" >&"$1"; echo "\\echo $3" >&"$1"
		for i in $(seq 1 300); do
			grep -qx "$3" "$2" 2>/dev/null && return 0
			[ -n "${5:-}" ] && [ "$(q "SELECT count(*) FROM pg_stat_activity WHERE application_name = '$5' AND wait_event_type = 'Lock'")" = 1 ] && return 0
			sleep 0.1
		done
		echo "timeout at $3" >>"$2"
	}
	local lvl="${4:-SERIALIZABLE}"
	step 7 "$W/so1" m1 "SET application_name = 'sk_s1'; BEGIN ISOLATION LEVEL $lvl; $1"
	step 8 "$W/so2" m2 "SET application_name = 'sk_s2'; BEGIN ISOLATION LEVEL $lvl; $1"
	step 7 "$W/so1" m3 "$2"
	step 8 "$W/so2" m4 "$3" sk_s2
	step 7 "$W/so1" m5 "COMMIT;"
	step 8 "$W/so2" m6 "COMMIT;"
	exec 7>&- 8>&-
	wait "$p1" "$p2"
	# tagged FILE N WMARK CMARK -> "<step><N>:<sqlstate>:<ssi|message>" per error
	tagged() {
		awk -v n="$2" -v wm="$3" -v cm="$4" 'BEGIN { st = "r" }
			$0 == wm { st = "w" } $0 == cm { st = "c" }
			/^timeout at / { printf "%s ", $0 }
			/^ERROR: +[0-9A-Z][0-9A-Z][0-9A-Z][0-9A-Z][0-9A-Z]:/ {
				code = $2; sub(/:$/, "", code); msg = $0; sub(/^ERROR: +[0-9A-Z]+: /, "", msg)
				printf "%s%s:%s:%s ", st, n, code, (msg ~ /read\/write dependencies/ ? "ssi" : msg) }' "$1" | sed 's/ $//'
	}
	local e1 e2; e1="$(tagged "$W/so1" 1 m1 m3)"; e2="$(tagged "$W/so2" 2 m2 m4)"
	# s2 refused once by SSI, at its write or its COMMIT, reads as 40001/ssi
	[[ "$e2" =~ ^[wc]2:40001:ssi$ ]] && e2="40001/ssi"
	echo "$e1|$e2"
}

for am in pgcolumnar heap; do
	psql_run "CREATE TABLE doc1_$am (id int, on_call bool) USING $am;
	          INSERT INTO doc1_$am VALUES (1, true), (2, true);
	          CREATE TABLE doc2_$am (id int, on_call bool) USING $am;
	          INSERT INTO doc2_$am VALUES (1, true);
	          INSERT INTO doc2_$am VALUES (2, true);
	          CREATE TABLE ward_$am (id int, ward int, on_call bool) USING $am;
	          INSERT INTO ward_$am SELECT g, g % 50, false FROM generate_series(1, 5000) g;
	          CREATE TABLE wardi_$am (id int, ward int, on_call bool) USING $am;
	          INSERT INTO wardi_$am SELECT g, g % 50, false FROM generate_series(1, 5000) g;
	          CREATE INDEX wardi_${am}_w ON wardi_$am (ward);
	          ANALYZE wardi_$am;"
done
check "premise: the second doctors table holds its two rows in two row groups, the first in one" \
	"$(q "SELECT (SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = pgcolumnar.get_storage_id('doc1_pgcolumnar'))
	          || '/' || (SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = pgcolumnar.get_storage_id('doc2_pgcolumnar'))")" "1/2"

# WHERE THE PROTECTION COMES FROM. The access method takes no predicate locks of its
# own. A serializable read of a columnar table reads columnar's catalog -- row_group,
# delete_vector, column_chunk, which are heap tables -- and heap SSI takes SIRead
# locks there; a write later updates those catalog rows, and heap finds the conflict.
# So a change that stopped reading the catalog per statement (a cache) or read it
# with a non-MVCC snapshot would drop the protection silently. This arm pins it.
check "a serializable read of a columnar table holds SIRead locks on columnar's row_group and delete_vector catalog" \
	"$(q "BEGIN ISOLATION LEVEL SERIALIZABLE; SELECT count(*) FROM doc1_pgcolumnar WHERE on_call;
	      SELECT count(*) FILTER (WHERE c.relname IN ('row_group', 'row_group_pkey')) || '/' ||
	             count(*) FILTER (WHERE c.relname IN ('delete_vector', 'delete_vector_pkey'))
	        FROM pg_locks l JOIN pg_class c ON c.oid = l.relation
	       WHERE l.mode = 'SIReadLock' AND l.pid = pg_backend_pid(); COMMIT;" | grep -E '^[0-9]+/[0-9]+$' | sed 's#^\([1-9][0-9]*\)/\([1-9][0-9]*\)$#held#')" "held"

both() {	# both TABLESTEM READ WRITE1 WRITE2 INVARIANT [LEVEL] -> "<columnar>  <heap>"
	local am out=""
	for am in pgcolumnar heap; do
		t="$1_$am"
		out="$out$(skew "${2//%t/$t}" "${3//%t/$t}" "${4//%t/$t}" "${6:-}")|$(q "${5//%t/$t}" | tail -1)  "
	done
	echo "${out%  }"
}
reset_tables() {	# the doctors back on call, the wards empty again
	local am
	for am in pgcolumnar heap; do
		psql_run "UPDATE doc1_$am SET on_call = true; UPDATE doc2_$am SET on_call = true;
		          DELETE FROM ward_$am WHERE on_call; DELETE FROM wardi_$am WHERE on_call;"
	done
}
check "two doctors in one row group each taken off call: SSI refuses one of the two with 40001, and one stays on call, as on heap" \
	"$(both doc1 "SELECT count(*) FROM %t WHERE on_call;" "UPDATE %t SET on_call = false WHERE id = 1;" \
	        "UPDATE %t SET on_call = false WHERE id = 2;" "SELECT count(*) FROM %t WHERE on_call")" \
	"|40001/ssi|1  |40001/ssi|1"
check "the same with the two doctors in two row groups" \
	"$(both doc2 "SELECT count(*) FROM %t WHERE on_call;" "UPDATE %t SET on_call = false WHERE id = 1;" \
	        "UPDATE %t SET on_call = false WHERE id = 2;" "SELECT count(*) FROM %t WHERE on_call")" \
	"|40001/ssi|1  |40001/ssi|1"
check "two sessions that each see an empty ward by a scan of the whole table and each put someone on call: SSI refuses one of the two with 40001, as on heap" \
	"$(both ward "SET LOCAL enable_indexscan = off; SELECT count(*) FROM %t WHERE ward = 7 AND on_call;" \
	        "INSERT INTO %t VALUES (101, 7, true);" "INSERT INTO %t VALUES (102, 7, true);" \
	        "SELECT count(*) FROM %t WHERE ward = 7 AND on_call")" \
	"|40001/ssi|1  |40001/ssi|1"
# The columnar custom scan wins over an index scan even with seqscan off, so it is
# turned off for the index arm. The sequential arm above reads columnar through it.
IDX="SET LOCAL enable_seqscan = off; SET LOCAL enable_bitmapscan = off; SET LOCAL pgcolumnar.enable_custom_scan = off;"
check "premise: the ward lookup is an index scan on both tables" \
	"$(for t in wardi_pgcolumnar wardi_heap; do q "SET enable_seqscan = off; SET enable_bitmapscan = off; SET pgcolumnar.enable_custom_scan = off; EXPLAIN (COSTS OFF) SELECT count(*) FROM $t WHERE ward = 7 AND on_call" | grep -c "Index.* Scan using ${t}_w"; done | paste -sd'|')" "1|1"
check "the same when both read the ward through an index" \
	"$(both wardi "$IDX SELECT count(*) FROM %t WHERE ward = 7 AND on_call;" \
	        "INSERT INTO %t VALUES (101, 7, true);" "INSERT INTO %t VALUES (102, 7, true);" \
	        "SELECT count(*) FROM %t WHERE ward = 7 AND on_call")" \
	"|40001/ssi|1  |40001/ssi|1"

# ---- the control: REPEATABLE READ lets both commit --------------------------------
# Snapshot isolation without SSI allows the write skew, on heap and on columnar, so
# both transactions commit and the invariant breaks. A 40001 above that came from
# anything but SSI's read/write tracking would show up here as a failure too.
reset_tables
DOFF=("SELECT count(*) FROM %t WHERE on_call;" "UPDATE %t SET on_call = false WHERE id = 1;"
      "UPDATE %t SET on_call = false WHERE id = 2;" "SELECT count(*) FROM %t WHERE on_call")
WON=("INSERT INTO %t VALUES (101, 7, true);" "INSERT INTO %t VALUES (102, 7, true);"
     "SELECT count(*) FROM %t WHERE ward = 7 AND on_call")
r1="$(both doc1 "${DOFF[@]}" "REPEATABLE READ")"; r2="$(both doc2 "${DOFF[@]}" "REPEATABLE READ")"
check "at REPEATABLE READ both doctors' transactions commit and no one stays on call, in one row group and in two, as on heap" \
	"$r1 / $r2" "||0  ||0 / ||0  ||0"
r1="$(both ward "SET LOCAL enable_indexscan = off; SELECT count(*) FROM %t WHERE ward = 7 AND on_call;" "${WON[@]}" "REPEATABLE READ")"
r2="$(both wardi "$IDX SELECT count(*) FROM %t WHERE ward = 7 AND on_call;" "${WON[@]}" "REPEATABLE READ")"
check "at REPEATABLE READ both ward transactions commit and two are on call, by a scan and by an index, as on heap" \
	"$r1 / $r2" "||2  ||2 / ||2  ||2"

pgc_summary
