#!/usr/bin/env bash
#
# TABLESAMPLE must raise, not return the whole table.
#
# docs/limitations.md: "TABLESAMPLE is unsupported and says so: it raises an
# error rather than returning no rows." The AM sample callbacks already raise
# 0A000. The planner hooks did not look at rte->tablesample, so a custom scan
# or vectorized aggregate replaced Sample Scan and answered every row.
#
# Measured on 10,000 rows at shipped defaults:
#
#     SELECT count(*) FROM n TABLESAMPLE BERNOULLI(10) REPEATABLE (7)
#     heap:    ~1000
#     columnar: 10000, plan: Custom Scan (Columnar Vector Agg)
#
# and on a wide table the same silent full scan, plan: Custom Scan (Columnar Scan).
# A shape that still reached Sample Scan did raise. The contract is the error,
# not a sample, so this pins 0A000 on the shapes that used to succeed.
#
# The arms assert SQLSTATE rather than message text. A grep for "not supported"
# is also satisfied by a dozen unrelated errors, and by a connection failure.
#
# Usage:  test/tablesample.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

sqlstate() {
	local out code
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -Atq -v VERBOSITY=verbose \
		-c "\\set VERBOSITY verbose" \
		-c "$1" 2>&1)"
	code="$(printf '%s\n' "$out" | sed -n 's/.*ERROR:[[:space:]]*\([0-9A-Z]\{5\}\):.*/\1/p' | head -1)"
	if [ -n "$code" ]; then printf '%s\n' "$code"; else printf '00000\n'; fi
}

psql_run "CREATE TABLE n (id int, v text) USING pgcolumnar;"
psql_run "INSERT INTO n SELECT g, 'x' || g FROM generate_series(1, 10000) g;"
check_num "premise: the table holds 10000 rows" \
	"$(q 'SELECT count(*) FROM n')" "10000"

# The defect was that this succeeded with 10000. After the fix it must refuse.
st_count="$(sqlstate 'SELECT count(*) FROM n TABLESAMPLE BERNOULLI(10) REPEATABLE (7);')"
echo "-- count(*) TABLESAMPLE SQLSTATE: ${st_count:-<none>}"
check "count(*) TABLESAMPLE raises 0A000 rather than answering every row" \
	"$st_count" "0A000"

st_sum="$(sqlstate 'SELECT sum(id) FROM n TABLESAMPLE BERNOULLI(10) REPEATABLE (7);')"
check "sum() TABLESAMPLE raises 0A000 rather than folding the whole table" \
	"$st_sum" "0A000"

st_sys="$(sqlstate 'SELECT count(*) FROM n TABLESAMPLE SYSTEM(10) REPEATABLE (7);')"
check "SYSTEM TABLESAMPLE raises 0A000 as well" "$st_sys" "0A000"

st_proj="$(sqlstate 'SELECT id FROM n TABLESAMPLE BERNOULLI(10) REPEATABLE (7);')"
check "a projected TABLESAMPLE scan raises 0A000 rather than returning every id" \
	"$st_proj" "0A000"

st_group="$(sqlstate 'SET pgcolumnar.enable_group_vectorization=on; SELECT id % 10, count(*) FROM n TABLESAMPLE BERNOULLI(10) REPEATABLE (7) GROUP BY 1;')"
check "grouped TABLESAMPLE raises 0A000 rather than grouping every row" \
	"$st_group" "0A000"

# control: the probe distinguishes success from 0A000, and heap still samples
st_ok="$(sqlstate 'SELECT count(*) FROM n;')"
check "control: an unsampled count reports success, so the probe is not stuck on 0A000" \
	"$st_ok" "00000"

psql_run "CREATE TABLE h (id int, v text);"
psql_run "INSERT INTO h SELECT g, 'x' || g FROM generate_series(1, 10000) g;"
st_heap="$(sqlstate 'SELECT count(*) FROM h TABLESAMPLE BERNOULLI(10) REPEATABLE (7);')"
check "control: the same TABLESAMPLE on a heap table raises nothing" \
	"$st_heap" "00000"

pgc_summary
