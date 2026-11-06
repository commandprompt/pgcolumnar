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

pgc_summary
