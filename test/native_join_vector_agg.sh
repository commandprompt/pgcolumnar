#!/usr/bin/env bash
# Ungrouped vectorized aggregate over a unique-key inner join (#752).
#
# The fold is worth 4.1x on a bare scan. create_upper_paths_hook drops it the
# moment the input is a joinrel (RELOPT_BASEREL). A star-schema inner join onto
# a UNIQUE dimension is a filter of the fact table, so the fold can survive.
# A duplicate-key dimension is not that filter: the path must refuse and core
# Agg over the join remains correct.
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/lib/postgresql/18/bin/pg_config}"

GUC=pgcolumnar.enable_ungrouped_vector_agg
NOPAR="SET max_parallel_workers_per_gather=0"
FORCEH="SET enable_nestloop=off; SET enable_mergejoin=off"

pc() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -v ON_ERROR_STOP=1 -c "$NOPAR" -c "$FORCEH" -c "$1" 2>&1
}

q "$(cat <<'SQL'
CREATE TABLE dim_u(k int PRIMARY KEY);
INSERT INTO dim_u SELECT g FROM generate_series(1,25) g;
CREATE TABLE fact_u(k int, m float8) USING pgcolumnar;
SELECT pgcolumnar.set_options($t$fact_u$t$, stripe_row_limit => 1000);
INSERT INTO fact_u SELECT 1 + (g % 50), (g % 17)::float8
  FROM generate_series(1,5000) g;
CREATE TABLE heap_u (k int, m float8) USING heap;
INSERT INTO heap_u SELECT * FROM fact_u;
ANALYZE dim_u;
ANALYZE fact_u;
SQL
)" >/dev/null

SQLU="SELECT count(*), coalesce(sum(fact_u.m)::text,'z') FROM fact_u JOIN dim_u ON fact_u.k = dim_u.k"

plan_on="$(pc "SET $GUC=on; EXPLAIN (COSTS OFF) $SQLU")"
plan_off="$(pc "SET $GUC=off; EXPLAIN (COSTS OFF) $SQLU")"
check "unique join uses vectorized agg when GUC on" \
	"$(grep -c 'Columnar Vectorized Aggregates' <<<"$plan_on")" 1
check "unique join uses core Agg when GUC off" \
	"$(grep -c 'Columnar Vectorized Aggregates' <<<"$plan_off")" 0
check "unique join fold answer equals GUC off" \
	"$(pc "SET $GUC=on; $SQLU" | tail -1)" \
	"$(pc "SET $GUC=off; $SQLU" | tail -1)"
check "unique join fold answer equals heap" \
	"$(pc "SET $GUC=on; $SQLU" | tail -1)" \
	"$(q "SELECT count(*), coalesce(sum(heap_u.m)::text,'z') FROM heap_u JOIN dim_u ON heap_u.k = dim_u.k")"

q "$(cat <<'SQL'
CREATE TABLE dim_d(k int);
INSERT INTO dim_d SELECT g FROM generate_series(1,50) g;
INSERT INTO dim_d SELECT g FROM generate_series(1,50) g;
CREATE TABLE fact_d(k int, m float8) USING pgcolumnar;
INSERT INTO fact_d SELECT 1 + (g % 50), 1::float8 FROM generate_series(1,100) g;
CREATE TABLE heap_d (k int, m float8) USING heap;
INSERT INTO heap_d SELECT * FROM fact_d;
ANALYZE dim_d;
ANALYZE fact_d;
SQL
)" >/dev/null

SQLD="SELECT count(*), coalesce(sum(fact_d.m)::text,'z') FROM fact_d JOIN dim_d ON fact_d.k = dim_d.k"
plan_dup="$(pc "SET $GUC=on; EXPLAIN (COSTS OFF) $SQLD")"
check "duplicate dim keys refuse the join fold" \
	"$(grep -c 'Columnar Vectorized Aggregates' <<<"$plan_dup")" 0
check "duplicate dim keys answer equals heap" \
	"$(pc "SET $GUC=on; $SQLD" | tail -1)" \
	"$(q "SELECT count(*), coalesce(sum(heap_d.m)::text,'z') FROM heap_d JOIN dim_d ON heap_d.k = dim_d.k")"

q "$(cat <<'SQL'
CREATE TABLE dim_l(k int PRIMARY KEY);
INSERT INTO dim_l VALUES (1);
CREATE TABLE fact_l(k int, m float8) USING pgcolumnar;
INSERT INTO fact_l VALUES (1, 1.0), (2, 2.0);
CREATE TABLE heap_l (k int, m float8) USING heap;
INSERT INTO heap_l VALUES (1, 1.0), (2, 2.0);
ANALYZE dim_l;
ANALYZE fact_l;
SQL
)" >/dev/null

SQLL="SELECT count(*), coalesce(sum(dim_l.k)::text,'z') FROM fact_l LEFT JOIN dim_l ON fact_l.k = dim_l.k"
plan_left="$(pc "SET $GUC=on; EXPLAIN (COSTS OFF) $SQLL")"
check "LEFT join refuses the join fold" \
	"$(grep -c 'Columnar Vectorized Aggregates' <<<"$plan_left")" 0
check "LEFT join answer equals heap" \
	"$(pc "SET $GUC=on; $SQLL" | tail -1)" \
	"$(q "SELECT count(*), coalesce(sum(dim_l.k)::text,'z') FROM heap_l LEFT JOIN dim_l ON heap_l.k = dim_l.k")"

# A Join Filter besides the hash clause is not a membership test. The fold that
# only drains dim keys would ignore f.v > d.t and over-count.
q "$(cat <<'SQL'
CREATE TABLE dim_xf(k int PRIMARY KEY, t int);
INSERT INTO dim_xf SELECT g, 20 FROM generate_series(1,40) g;
CREATE TABLE fact_xf(k int, v int) USING pgcolumnar;
INSERT INTO fact_xf SELECT 1 + (g % 40), g % 40 FROM generate_series(1,2000) g;
CREATE TABLE heap_xf (k int, v int) USING heap;
INSERT INTO heap_xf SELECT * FROM fact_xf;
ANALYZE dim_xf;
ANALYZE fact_xf;
SQL
)" >/dev/null

SQLXF="SELECT coalesce(sum(fact_xf.v)::text,'z') FROM fact_xf JOIN dim_xf ON fact_xf.k = dim_xf.k AND fact_xf.v > dim_xf.t"
plan_xf="$(pc "SET $GUC=on; EXPLAIN (COSTS OFF) $SQLXF")"
check "extra join filter refuses the join fold" \
	"$(grep -c 'Columnar Vectorized Aggregates' <<<"$plan_xf")" 0
check "extra join filter answer equals GUC off" \
	"$(pc "SET $GUC=on; $SQLXF" | tail -1)" \
	"$(pc "SET $GUC=off; $SQLXF" | tail -1)"
check "extra join filter answer equals heap" \
	"$(pc "SET $GUC=on; $SQLXF" | tail -1)" \
	"$(q "SELECT coalesce(sum(heap_xf.v)::text,'z') FROM heap_xf JOIN dim_xf ON heap_xf.k = dim_xf.k AND heap_xf.v > dim_xf.t")"

q "$(cat <<'SQL'
CREATE TABLE dim_xn(k int PRIMARY KEY, t int);
INSERT INTO dim_xn SELECT g, 7 FROM generate_series(1,12) g;
CREATE TABLE fact_xn(k int, v int) USING pgcolumnar;
INSERT INTO fact_xn SELECT 1 + (g % 12), g % 9 FROM generate_series(1,360) g;
CREATE TABLE heap_xn (k int, v int) USING heap;
INSERT INTO heap_xn SELECT * FROM fact_xn;
ANALYZE dim_xn;
ANALYZE fact_xn;
SQL
)" >/dev/null

SQLXN="SELECT coalesce(sum(fact_xn.v)::text,'z') FROM fact_xn JOIN dim_xn ON fact_xn.k = dim_xn.k AND fact_xn.v <> dim_xn.t"
plan_xn="$(pc "SET $GUC=on; EXPLAIN (COSTS OFF) $SQLXN")"
check "inequality join filter refuses the join fold" \
	"$(grep -c 'Columnar Vectorized Aggregates' <<<"$plan_xn")" 0
check "inequality join filter answer equals heap" \
	"$(pc "SET $GUC=on; $SQLXN" | tail -1)" \
	"$(q "SELECT coalesce(sum(heap_xn.v)::text,'z') FROM heap_xn JOIN dim_xn ON heap_xn.k = dim_xn.k AND heap_xn.v <> dim_xn.t")"

# A rescan of the fold re-drains the dimension for the new outer value (#2128,
# the sibling of #2062). A correlated subquery or LATERAL join reruns the fold.
# The fold read the dimension inside its own key context and then reset that
# context on rescan, which freed the dimension scan's state: the second rescan
# crashed the backend. The DISTINCT arm puts a HashAggregate under the fold. The
# fold holds it in custom_ps, which ExecReScan does not tell about a changed
# parameter, so it would return its first rows again. Each arm runs with
# default join settings and with hash joins forced; the outer values grow, so a
# stale or empty key table cannot match heap.
q "$(cat <<'SQL'
CREATE TABLE dim_rf(k int PRIMARY KEY);
INSERT INTO dim_rf SELECT g FROM generate_series(1,40) g;
CREATE TABLE dimh_rf(k int);
INSERT INTO dimh_rf SELECT g FROM generate_series(1,40) g;
CREATE TABLE fact_rf(k int, m float8) USING pgcolumnar;
INSERT INTO fact_rf SELECT 1 + (g % 40), 1::float8 FROM generate_series(1,4000) g;
CREATE TABLE heap_rf (k int, m float8) USING heap;
INSERT INTO heap_rf SELECT * FROM fact_rf;
ANALYZE dim_rf;
ANALYZE dimh_rf;
ANALYZE fact_rf;
SQL
)" >/dev/null

DEFJ="RESET enable_nestloop; RESET enable_mergejoin;"
# A crash (the defect above) restarts the server. Wait until it accepts
# queries again, so the next check reports its own result and not the restart.
rs_ready() {
	local i
	for i in $(seq 1 100); do
		[ "$(q "SELECT 1")" = 1 ] && return 0
		sleep 0.1
	done
}
for arm in \
	"scalar|SELECT v.lim||':'||(SELECT count(*) FROM @T JOIN dim_rf ON @T.k = dim_rf.k WHERE dim_rf.k < v.lim) FROM (VALUES (3), (12), (30)) v(lim) ORDER BY v.lim" \
	"LATERAL|SELECT v.lim||':'||x.c FROM (VALUES (3), (12), (30)) v(lim), LATERAL (SELECT count(*) c FROM @T JOIN dim_rf ON @T.k = dim_rf.k WHERE dim_rf.k < v.lim) x ORDER BY v.lim" \
	"distinct|SELECT v.lim||':'||(SELECT count(*) FROM @T JOIN (SELECT DISTINCT k FROM dimh_rf WHERE k < v.lim) d ON @T.k = d.k) FROM (VALUES (3), (12), (30)) v(lim) ORDER BY v.lim"; do
	n=${arm%%|*}; s=${arm#*|}
	heap_rs="$(q "${s//@T/heap_rf}" | tr '\n' ' ')"
	check "premise: heap's $n rescan answer grows with each outer value" \
		"$heap_rs" "3:200 12:1100 30:2900 "
	for setting in "default|$DEFJ" "hash-only|"; do
		sn=${setting%%|*}; ss=${setting#*|}
		check "premise: the $sn $n rescan uses the join fold" \
			"$(pc "SET $GUC=on; ${ss}EXPLAIN (COSTS OFF) ${s//@T/fact_rf}" | grep -c 'Columnar Join Fold: yes')" 1
		check "each $sn $n fold rescan drains its own dimension keys" \
			"$(pc "SET $GUC=on; ${ss}${s//@T/fact_rf}" | grep -v '^SET$\|^RESET$' | tr '\n' ' ')" \
			"$heap_rs"
		rs_ready
	done
done

pgc_summary
