#!/usr/bin/env bash
#
# pgColumnar: pgcolumnar.approx_count_distinct, the aggregate (#1309 phase 2).
#
# A HyperLogLog over the type's extended hash (seed 0), 2048 one-byte registers,
# the same register rule the writer's sketches use (native_sketch.sh). This phase
# is the aggregate alone: a plain transition function that hashes every non-NULL
# value, so it answers on any table, columnar or heap. Nothing reads the stored
# sketches yet; that is phase 3, and it must return exactly what this returns.
#
# The estimator, fixed so a pinned value means something:
#   raw = alpha_m * m^2 / sum(2^-M[j]),  alpha_m = 0.7213 / (1 + 1.079 / m)
#   when raw <= 5m/2 and some register is zero: m * ln(m / zeros)   (linear counting)
# The standard error is 1.04/sqrt(2048), about 2.3%. ONE dataset is held within 4 of
# them (9.2%) and to a pinned value: 3 would fail a correct estimator once in ~370
# datasets, and this suite's own text set lands at +6.99%, 3.03 standard errors,
# which an independent recomputation in SQL from hashtextextended reproduces exactly.
# The 3-sigma claim is tested where it means something, over 50 disjoint sets: the
# RMS relative error must sit between 0.5 and 1.5 standard errors, and the mean bias
# within 3 standard errors of the mean. A pin alone holds the bytes still; the
# distribution holds the estimator to its theory.
#
# Usage:  test/native_sketch_agg.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

sqlstate() {	# sqlstate SQL -> a five-character SQLSTATE, noerror, or PROBE_UNREACHABLE
	local out st
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -v VERBOSITY=sqlstate -v ON_ERROR_STOP=0 \
		-c "$1" -c "SELECT 'PGC_PROBE_OK';" 2>&1)"
	case "$out" in
		*PGC_PROBE_OK*) ;;
		*) echo PROBE_UNREACHABLE; return ;;
	esac
	st="$(printf '%s\n' "$out" | sed -n 's/^.*ERROR:[[:space:]]*\([0-9A-Z]\{5\}\).*$/\1/p' | head -1)"
	if [ -n "$st" ]; then printf '%s\n' "$st"; else echo noerror; fi
}

# "within" when the estimate is within 4 standard errors of the exact count, else the numbers
# "same" when both readings are the same NUMBER: two failed reads are both empty
same() {	# same A B
	case "$1" in ''|*[!0-9]*) echo "not a number: [$1] vs [$2]"; return ;; esac
	[ "$1" = "$2" ] && echo same || echo "[$1] vs [$2]"
}
within() {	# within TABLE COLUMN
	q "SELECT CASE WHEN abs(e - x) <= 0.092 * x THEN 'within' ELSE e || ' for ' || x END
		FROM (SELECT pgcolumnar.approx_count_distinct($2) e, count(DISTINCT $2) x FROM $1) s;"
}
est() {	# est TABLE COLUMN
	q "SELECT pgcolumnar.approx_count_distinct($2) FROM $1;"
}

psql_run "CREATE TABLE ak (a bigint, t text) USING pgcolumnar;
	INSERT INTO ak SELECT g, 'key-' || g FROM generate_series(1, 100000) g;
	CREATE TABLE akh (a bigint, t text);
	INSERT INTO akh SELECT * FROM ak;" >/dev/null 2>&1

# --- 1. the answer ------------------------------------------------------------

check "premise: the table holds 100000 distinct values" "$(q "SELECT count(DISTINCT a) FROM ak;")" "100000"
check "approx_count_distinct of 100000 distinct bigints is within 4 standard errors of the exact count" \
	"$(within ak a)" "within"
check "and equals its pinned value" "$(est ak a)" "101372"
check "a text column is within 4 standard errors too" "$(within ak t)" "within"
check "and equals its pinned value, the recomputation's" "$(est ak t)" "106986"
check "a heap table gives the columnar table's answer" "$(same "$(est akh a)" "$(est ak a)")" "same"

# --- 1b. the error distribution, over 50 disjoint sets of 20000 values ------------

DIST="SELECT sqrt(avg(e * e)) AS rms, avg(e) AS bias FROM (
	SELECT (pgcolumnar.approx_count_distinct(g) - 20000) / 20000.0 AS e
	  FROM generate_series(1, 50) k, LATERAL generate_series(k * 1000000::bigint, k * 1000000::bigint + 19999) g
	 GROUP BY k) s"
check "over 50 sets the RMS relative error is between 0.5 and 1.5 standard errors" \
	"$(q "SELECT CASE WHEN rms BETWEEN 0.0115 AND 0.0345 THEN 'yes' ELSE 'no: rms ' || round(rms::numeric, 4) END FROM ($DIST) d;")" "yes"
check "and the mean bias is within 3 standard errors of the mean" \
	"$(q "SELECT CASE WHEN abs(bias) <= 3 * 0.023 / sqrt(50) THEN 'yes' ELSE 'no: bias ' || round(bias::numeric, 4) END FROM ($DIST) d;")" "yes"

# --- 2. small counts, on either side of the switch to linear counting (5m/2 = 5120)

psql_run "CREATE TABLE ak10 AS SELECT g::bigint AS a FROM generate_series(1, 10) g;
	CREATE TABLE ak1k AS SELECT g::bigint AS a FROM generate_series(1, 1000) g;
	CREATE TABLE ak9k AS SELECT g::bigint AS a FROM generate_series(1, 9000) g;" >/dev/null 2>&1
check "ten distinct values estimate ten" "$(est ak10 a)" "10"
check "1000 distinct values are within 4 standard errors (linear counting)" "$(within ak1k a)" "within"
check "9000 distinct values are within 4 standard errors (the raw estimate)" "$(within ak9k a)" "within"
check "and the small counts equal their pinned values" "$(est ak1k a)|$(est ak9k a)" "1010|9026"

# --- 3. NULLs and empty input -------------------------------------------------

# TEN values and 500 NULLs, where linear counting is exact: a NULL counted as any one
# value reads 11. Against 1000 values one extra distinct value is lost in the noise,
# and a NULL-as-zero mutant passed that version of this arm.
psql_run "CREATE TABLE akn AS SELECT g::bigint AS a FROM generate_series(1, 10) g
		UNION ALL SELECT NULL FROM generate_series(1, 500);" >/dev/null 2>&1
check "NULLs are not counted" "$(same "$(est akn a)" "$(est ak10 a)")" "same"
check "an empty input estimates 0" "$(q "SELECT pgcolumnar.approx_count_distinct(a) FROM ak WHERE false;")" "0"
check "an all-NULL input estimates 0" \
	"$(q "SELECT pgcolumnar.approx_count_distinct(NULL::bigint) FROM generate_series(1, 100);")" "0"

# --- 4. a parallel plan ---------------------------------------------------------

# Forced workers over the heap copy, so the partial states travel serialized and the
# combine (register-wise max) merges them. The register max does not depend on which
# worker saw which value, so the answer must be the serial one exactly.
#
# NO debug_parallel_query: PostgreSQL 15 calls it force_parallel_mode, so on 15 the
# SET failed and the arm read psql's echoed SET as its value (@OffgridwithJD on
# #1770). The costs at zero choose the Gather on every major, and the premise below
# proves the plan ran in workers rather than assuming it.
PAR="SET max_parallel_workers_per_gather = 4; SET parallel_setup_cost = 0;
	SET parallel_tuple_cost = 0; SET min_parallel_table_scan_size = 0;"
check "premise: the parallel plan runs a partial aggregate in workers" \
	"$(q "$PAR EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF)
		SELECT pgcolumnar.approx_count_distinct(a) FROM akh;" | grep -cE 'Partial Aggregate|Workers Launched: [1-9]' )" "2"
check "a parallel plan returns the serial answer" \
	"$(same "$(q "$PAR SELECT pgcolumnar.approx_count_distinct(a) FROM akh;" | tail -1)" "$(est akh a)")" "same"

# --- 5. what cannot be counted --------------------------------------------------

check "a type with no extended hash is refused" \
	"$(sqlstate "SELECT pgcolumnar.approx_count_distinct(point(g, g)) FROM generate_series(1, 3) g")" "0A000"
psql_run "CREATE COLLATION IF NOT EXISTS ak_ci (provider = icu, locale = 'und-u-ks-level2', deterministic = false);" >/dev/null 2>&1
if [ "$(q "SELECT count(*) FROM pg_collation WHERE collname = 'ak_ci';")" = 1 ]; then
	check "a nondeterministic collation is refused" \
		"$(sqlstate "SELECT pgcolumnar.approx_count_distinct(t COLLATE ak_ci) FROM ak")" "0A000"
else
	check_unrunnable "a nondeterministic collation is refused" MISSING_DEPENDENCY \
		"this server has no ICU, so no nondeterministic collation can be created"
fi

pgc_summary
