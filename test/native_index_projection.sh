#!/usr/bin/env bash
#
# CREATE INDEX projects only the columns the index needs (issue #413).
#
# pgcolumnar_index_build_range_scan opened its reader with no projection, so building a
# one-column index on a wide table decoded every column. It never had to: the callback
# receives IndexInfo, which carries ii_IndexAttrNumbers and the expression and predicate
# trees. The information was in its own arguments and was thrown away.
#
# Measured on 300,000 rows of 20 columns, index on the key alone, non-assert PG18:
#
#     before   columnar 1,403 ms   heap 149 ms   9.4x slower than heap
#     after    columnar    87 ms   heap 144 ms   1.65x faster
#
# What is asserted, in the order the risk sits:
#
#  1. the projection NARROWED, read from the build's own DEBUG1 line. A fix that
#     silently did nothing passes every correctness check below, and a wall-clock check
#     on a quiet machine, so this is the assertion with teeth.
#  2. expression and partial indexes project their EXTRA columns. Getting those wrong
#     evaluates against unset slot values, which is a wrong answer and not a slow one.
#  3. a forced index scan and a forced seq scan agree over the FULL ordered result.
#     Point lookups can be satisfied by an index that is wrong for keys nobody asked for.
#  4. the PARALLEL build projects as well. Its reader arrives through the table-AM scan
#     interface, which carries no projection, and it is the branch core takes by default
#     for any table of consequential size. See the section below for the measurements.
#
# amcheck runs where the build has contrib and skips VISIBLY where it does not. Source
# builds have no contrib, and a silent skip is the defect this file exists to prevent.
#
# Usage:  test/native_index_projection.sh [PG_CONFIG]
# Written fresh for pgColumnar.
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

ROWS=${PGC_IDXPROJ_ROWS:-300000}

# lib.sh's q() emits psql's output for every statement, so "SET ...; SELECT ..." in one
# call returns "SET" as well as the value. Keep the session but read only the result.
qset() {  # $1 = SET clause(s), $2 = query
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -q -c "$1" -c "$2" 2>/dev/null | tail -1
}


# 20 columns: an int key, an int secondary, 18 wide text.
cols="k int, k2 int"
vals="g, g*2"
for i in $(seq 1 18); do cols="$cols, c$i text"; vals="$vals, repeat('x',80)||g"; done
psql_run "CREATE TABLE w ($cols) USING pgcolumnar;"
psql_run "INSERT INTO w SELECT $vals FROM generate_series(1,$ROWS) g;"
check "fixture rows" "$(q 'SELECT count(*) FROM w')" "$ROWS"
check "fixture is wide" \
	"$(q "SELECT count(*) FROM pg_attribute WHERE attrelid='w'::regclass AND attnum>0")" "20"

# The projection each build chose, from its own DEBUG1 line. This is the check a
# do-nothing fix would fail and the correctness checks below would not.
proj() {  # $1 index name, $2 CREATE INDEX statement -> "n of m"
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-c "SET client_min_messages=debug1;" -c "$2" 2>&1 \
		| grep -oE "index build on \"$1\" projecting [0-9]+ of [0-9]+" \
		| grep -oE '[0-9]+ of [0-9]+' | head -1
}

check "plain index projects one column of twenty" \
	"$(proj w_k    'CREATE INDEX w_k ON w (k)')" "1 of 20"
check "two-column index projects two" \
	"$(proj w_k12  'CREATE INDEX w_k12 ON w (k, k2)')" "2 of 20"
check "an index on a late column projects one, not everything before it" \
	"$(proj w_c18  'CREATE INDEX w_c18 ON w (c18)')" "1 of 20"
check "an expression index projects its expression's columns" \
	"$(proj w_expr 'CREATE INDEX w_expr ON w ((k + k2))')" "2 of 20"
check "a partial index projects the predicate's columns too" \
	"$(proj w_part 'CREATE INDEX w_part ON w (k) WHERE k2 > 100')" "2 of 20"
check "an expression over a text column projects that column" \
	"$(proj w_len  'CREATE INDEX w_len ON w ((length(c1)))')" "1 of 20"

# Correctness. An index built from under-projected data indexes unset slot values, so
# each of these must find what a sequential scan finds.
# The custom scan off as well: measured, the expression lookup ran as a Custom Scan.
check "plain index finds the row" \
	"$(qset 'SET enable_seqscan=off; SET pgcolumnar.enable_custom_scan=off' 'SELECT k2 FROM w WHERE k = 12345')" "24690"
check "expression index finds the row" \
	"$(qset 'SET enable_seqscan=off; SET pgcolumnar.enable_custom_scan=off' 'SELECT count(*) FROM w WHERE (k + k2) = 30000')" "1"
check "partial index finds the row inside its predicate" \
	"$(qset 'SET enable_seqscan=off; SET pgcolumnar.enable_custom_scan=off' 'SELECT count(*) FROM w WHERE k = 50000 AND k2 > 100')" "1"

# The oracle, depending on nothing but PostgreSQL. Compare the FULL ordered result of a
# forced index scan against a forced seq scan.
#
# Both sides must be a real md5. A down cluster, an errored query or a predicate that
# matches nothing all yield "", and `check "" ""` compares nothing with nothing and
# prints PASS (#418). This oracle is the strongest assertion in the file, so it is the
# worst one to have silently comparing two empty strings.
#
# The emptiness half is `check_text` in `test/lib.sh` now (#418, #422), so this file no
# longer carries its own version of it. The shape half stays here: an md5 is 32 hex
# characters, and this file knows that where the shared helper only knows "not empty".
# THE INDEX SIDE MUST READ THE INDEX (#1341). enable_seqscan=off does not disable the
# columnar custom scan, and measured, the late-column, expression and text-expression
# oracles ran as Custom Scan on BOTH sides: a sequential scan compared with itself,
# whatever the index held. So the custom scan is off too, and each oracle names the
# index it must read and asserts the plan does.
IXOFF="SET enable_seqscan=off; SET enable_bitmapscan=off; SET pgcolumnar.enable_custom_scan=off"
#
# The premise reads the plan of the oracle query ITSELF, fenced with OFFSET 0. Without
# the fence the aggregate's ORDER BY asked for rows in key order, and the planner took
# a full scan of w_k12 with the late-column predicate as a filter: every row fetched
# through the index, two minutes at 60,000 rows and hours at this size.
agree() {  # $1 label, $2 predicate, $3 selected expression, $4 the index it must read
	local viaix viaseq ixplan oq
	oq="SELECT md5(string_agg(t::text, ',' ORDER BY t)) FROM (SELECT $3 AS t FROM w WHERE $2 OFFSET 0) s"
	ixplan="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-At -q -c "$IXOFF" -c "EXPLAIN (COSTS OFF) $oq" 2>/dev/null)"
	check "premise: $1 -- the index side reads $4" \
		"$(grep -cE "Index (Only )?Scan using $4 on w" <<<"$ixplan")" "1"
	viaix=$(qset "$IXOFF" "$oq")
	viaseq=$(qset "SET enable_indexscan=off; SET enable_bitmapscan=off; SET enable_indexonlyscan=off" "$oq")
	if ! grep -qE '^[0-9a-f]{32}$' <<<"$viaix" || ! grep -qE '^[0-9a-f]{32}$' <<<"$viaseq"; then
		# under the oracle's own name, so a red here is a red of that check
		check "$1" "index=[$viaix] seq=[$viaseq] (both sides must be a real md5)" "two md5 hashes"
		return
	fi
	check_text "$1" "$viaix" "$viaseq"
}
agree "plain index agrees with a sequential scan"   "k BETWEEN 1000 AND 9999"        "k" "w_k(12)?"
agree "late-column index agrees"                    "c18 > repeat('x',80)||'99000'"  "k" "w_c18"
agree "expression index agrees"                     "(k + k2) BETWEEN 300 AND 30000" "k" "w_expr"
agree "partial index agrees on its subset"          "k2 > 100 AND k < 5000"          "k" "w_part"
agree "text expression index agrees"                "length(c1) = 83"                "k" "w_len"

# The PARALLEL build, which is the default path for any table of consequential size and
# was the one left unprojected.
#
# index_build_range_scan gets its reader two ways. A serial build opens its own, and
# every participant of a parallel build (leader included) arrives with the shared
# TableScanDesc, whose reader came through the table-AM scan interface with nowhere to
# carry a projection. Projecting only the serial branch reads every column exactly when
# it costs most.
#
# This is not a tuning corner. Measured on this fixture at 1.5M rows of incompressible
# text, 459 MB on disk, with every parallel GUC at its default, core chose workers and
# the build took the parallel branch. Forcing it here only keeps the assertion cheap.
#
# Serial-branch-only projection scores 568 ms against heap's 563 ms on the parallel arm,
# and 71 ms on the serial arm: the wall clock alone reads as "fixed" if you measure the
# serial arm, which is why the branch is named in the DEBUG1 line and asserted here.
PAR="SET max_parallel_maintenance_workers=4; SET min_parallel_table_scan_size='0';
     SET maintenance_work_mem='256MB';"

branch() {  # $1 index name, $2 SET clauses, $3 CREATE INDEX -> "<branch> <n> of <m>"
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-c "SET client_min_messages=debug1; $2" -c "$3" 2>&1 \
		| grep -oE "(parallel|serial) index build on \"$1\" projecting [0-9]+ of [0-9]+" \
		| sed -E 's/ index build on .* projecting / /' | head -1
}

# Assert the PREMISE first. If core declines to go parallel here, the next check would
# pass by reading the serial branch and prove nothing about the one under test.
check "forcing parallel maintenance workers does reach the parallel branch" \
	"$(branch w_par "$PAR" 'CREATE INDEX w_par ON w (k)' | cut -d' ' -f1)" "parallel"
check "a parallel build projects one column of twenty, not all twenty" \
	"$(branch w_par2 "$PAR" 'CREATE INDEX w_par2 ON w (k2)')" "parallel 1 of 20"
check "a parallel expression build projects its expression's columns" \
	"$(branch w_pare "$PAR" 'CREATE INDEX w_pare ON w ((k + k2))')" "parallel 2 of 20"
check "a parallel partial build projects the predicate's columns too" \
	"$(branch w_parp "$PAR" 'CREATE INDEX w_parp ON w (k) WHERE k2 > 100')" "parallel 2 of 20"
#
# On c17, NOT on k2. A serially built index on k2 would sit beside the parallel-built
# w_par2 covering the same column, and the planner is free to pick either, so the two
# oracles below would silently validate the serial build. This check only has to show
# the serial branch is still reachable, so it goes on a column no oracle reads.
check "the serial branch is still reached when workers are refused" \
	"$(branch w_ser 'SET max_parallel_maintenance_workers=0;' \
		'CREATE INDEX w_ser ON w (c17)' | cut -d' ' -f1)" "serial"

# Every participant reads through one shared reader, so an under-projected or
# double-counted parallel build shows up as wrong or duplicated index entries.
#
# Assert the premise first. "The index scan agrees with the seq scan" proves nothing
# about a parallel build unless the scan actually uses the parallel-built index, and
# nothing in the query text guarantees that: it names a column, not an index.
# qset ends in `tail -1`, which would keep only the last plan line, so read the whole
# EXPLAIN directly.
usesix() {  # $1 = query -> the index name the plan scans
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-At -q -c "SET enable_seqscan=off; SET enable_bitmapscan=off;" \
		-c "EXPLAIN (COSTS OFF) $1" 2>/dev/null \
		| grep -oE 'using [a-z0-9_]+' | head -1 | cut -d' ' -f2
}
check "the oracle below really reads the parallel-built index" \
	"$(usesix 'SELECT k2 FROM w WHERE k2 BETWEEN 2000 AND 19998')" "w_par2"
agree "parallel-built index agrees with a sequential scan" "k2 BETWEEN 2000 AND 19998" "k2" "w_par2"
check "parallel-built index returns each row once" \
	"$(qset 'SET enable_seqscan=off; SET enable_bitmapscan=off' \
		'SELECT count(*) FROM w WHERE k2 BETWEEN 2 AND 2000')" "1000"

# amcheck where available. The skip must be visible: a check that reports nothing is
# indistinguishable from a check that passes, which is what this file is about.
# ONE LIST, BOTH BRANCHES. The names the else branch declines have to be the
# names the then branch checks, or two logs from two boxes differ by ten checks
# and nobody reading them can say why. It also stops the two drifting when an
# index is added to one branch and not the other.
_ixs="w_k w_k12 w_c18 w_expr w_part w_len w_par w_par2 w_pare w_parp w_ser"
if psql_run "CREATE EXTENSION IF NOT EXISTS amcheck;" >/dev/null 2>&1 &&
	[ "$(q "SELECT count(*) FROM pg_proc WHERE proname='bt_index_check'")" != "0" ]; then
	for ix in $_ixs; do
		out=$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
			-d "$PGC_DB" -c "SELECT bt_index_check('$ix'::regclass)" 2>&1)
		check "bt_index_check($ix)" \
			"$(grep -qE 'ERROR' <<<"$out" && echo bad || echo ok)" "ok"
	done
else
	# ELEVEN LINES, NOT ONE. The first version printed the old single summary
	# line and recorded eleven, which selftest 400 refused and was right to: a
	# line reading `SKIP  ...` that no record backs is exactly the shape this
	# branch of the change exists to remove, and a reader cannot tell it from a
	# recorded one. The then branch prints eleven PASS lines; this prints eleven
	# SKIP lines, and the reason travels on each.
	echo "      amcheck is not installed on this build; the seq-scan oracle above still ran"
	for ix in $_ixs; do
		check_skip "bt_index_check($ix)" \
			"SKIP  bt_index_check($ix): amcheck is not installed on this build" \
			"amcheck is not installed on this build"
	done
fi
unset _ixs

pgc_summary
