#!/usr/bin/env bash
#
# pgColumnar: min and max under a COLLATE override answer under that collation (#1771).
#
# The grouped node and the ungrouped vectorized aggregate compared min/max values
# under the COLUMN's collation and ignored the Aggref's input collation, which a
# COLLATE clause sets. On an ICU column, min(t COLLATE "C") per key returned the ICU
# answer: key 0 gave a1078 where heap gives B1029, and the ungrouped pair gave
# a10|B999 for B1|a998. Filters were unaffected.
#
# The data mixes 'a...' and 'B...' in every group (k = g % 49, an odd modulus), the
# two orderings disagree on them, and a heap mirror is the oracle. A control holds
# that min/max under the column's own collation still take the vectorized path, so
# a fix that refused every min/max would not pass.
#
# Usage:  test/native_agg_collation.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

srt() {	# srt SQL -> md5 of the rows, sorted
	q "$1" | LC_ALL=C sort | md5sum | cut -c1-32
}

if [ "$(q "SELECT count(*) FROM pg_collation WHERE collname = 'en-x-icu';")" != 1 ]; then
	for n in "premise: the groups mix values the two collations order differently" \
		"control: min and max under the column's own collation take the grouped node" \
		"and answer what heap answers" \
		"grouped min and max under a C-collation override answer what heap answers" \
		"ungrouped min and max under a C-collation override answer what heap answers"; do
		check_unrunnable "$n" MISSING_DEPENDENCY "this server has no ICU collation en-x-icu"
	done
	pgc_summary
	exit
fi

psql_run "CREATE TABLE cx (k int, t text COLLATE \"en-x-icu\") USING pgcolumnar;
	INSERT INTO cx SELECT g % 49, CASE WHEN g % 2 = 0 THEN 'a' || g ELSE 'B' || g END
	  FROM generate_series(1, 5000) g;
	CREATE TABLE cxh AS SELECT * FROM cx;
	ANALYZE cx;" >/dev/null 2>&1

check "premise: the groups mix values the two collations order differently" \
	"$(q "SELECT count(*) FROM (SELECT k FROM cxh GROUP BY k
		HAVING min(t) <> min(t COLLATE \"C\") AND max(t) <> max(t COLLATE \"C\")) s;")" "49"

OWN="SELECT k, min(t), max(t) FROM %T GROUP BY k"
check "control: min and max under the column's own collation take the grouped node" \
	"$(q "EXPLAIN (COSTS OFF) ${OWN//%T/cx}" | grep -c 'Columnar Vectorized Group Keys')" "1"
check "and answer what heap answers" "$(srt "${OWN//%T/cx}")" "$(srt "${OWN//%T/cxh}")"

OVR="SELECT k, min(t COLLATE \"C\"), max(t COLLATE \"C\") FROM %T GROUP BY k"
check "grouped min and max under a C-collation override answer what heap answers" \
	"$(srt "${OVR//%T/cx}")" "$(srt "${OVR//%T/cxh}")"

UNG="SELECT min(t COLLATE \"C\"), max(t COLLATE \"C\") FROM %T"
check "ungrouped min and max under a C-collation override answer what heap answers" \
	"$(q "${UNG//%T/cx}")" "$(q "${UNG//%T/cxh}")"

pgc_summary
