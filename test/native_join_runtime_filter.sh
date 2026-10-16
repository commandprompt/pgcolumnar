#!/usr/bin/env bash
# Serial join runtime filter public-seam regression (#752).
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/lib/postgresql/18/bin/pg_config}"
q "$(cat <<'SQL'
CREATE EXTENSION IF NOT EXISTS pgcolumnar;
CREATE TABLE d(k int);
INSERT INTO d SELECT g FROM generate_series(8001,8200) g;
INSERT INTO d VALUES(8100),(NULL);
CREATE TABLE f(k int,p text) USING pgcolumnar;
SELECT pgcolumnar.set_options($t$f$t$, stripe_row_limit => 1000);
INSERT INTO f SELECT g, repeat(md5(g::text), 8) FROM generate_series(1,20000) g;
CREATE TABLE h AS SELECT * FROM f;
ANALYZE d;
ANALYZE f;
SQL
)" >/dev/null
pc(){ env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At -v ON_ERROR_STOP=1 -c "$1" 2>&1; }
SQL="SELECT count(*),sum(f.k),sum(length(f.p)) FROM f JOIN d ON f.k=d.k"
rf_on=""
rf_off=""
if [[ "$(pc "SELECT current_setting(\$g\$pgcolumnar.enable_join_runtime_filter\$g\$, true) IS NOT NULL" | tail -1)" == t ]]; then
  rf_on="SET pgcolumnar.enable_join_runtime_filter=on;"
  rf_off="SET pgcolumnar.enable_join_runtime_filter=off;"
fi
check "join runtime filter defaults on" \
	"$(pc "SHOW pgcolumnar.enable_join_runtime_filter" | tail -1)" "on"
default_plan="$(pc "SET max_parallel_workers_per_gather=0;SET enable_nestloop=off;SET enable_mergejoin=off;EXPLAIN(ANALYZE,TIMING off,SUMMARY off)$SQL")"
check "default plan has runtime coordinator" \
	"$(grep -c 'Columnar Runtime Filter Coordinator' <<<"$default_plan")" 1
base="$(pc "${rf_off}SET max_parallel_workers_per_gather=0;SET enable_nestloop=off;SET enable_mergejoin=off;EXPLAIN(ANALYZE,TIMING off,SUMMARY off)$SQL")"
on="$(pc "${rf_on}SET max_parallel_workers_per_gather=0;SET enable_nestloop=off;SET enable_mergejoin=off;EXPLAIN(ANALYZE,TIMING off,SUMMARY off)$SQL")"
val(){ sed -n "s/.*$1: \([0-9]*\).*/\1/p" <<<"$2" | head -1; }
check "baseline core Hash Join" "$(grep -c 'Hash Join' <<<"$base")" 1
check "baseline reads all groups" "$(val 'Columnar Chunk Groups Read' "$base")" 20
check "plan has runtime coordinator" "$(grep -c 'Columnar Runtime Filter Coordinator' <<<"$on")" 1
check "plan has build tap" "$(grep -c 'Columnar Runtime Filter Build Tap' <<<"$on")" 1
check "plan retains core Hash Join" "$(grep -c 'Hash Join' <<<"$on")" 1
check "build rows omit NULL" "$(val 'Runtime Filter Build Rows' "$on")" 201
check "filter ready before scan" "$(grep -c 'Runtime Filter Ready: true' <<<"$on")" 1
check "clustered groups removed" "$(val 'Runtime Filter Groups Removed' "$on")" 19
check "clustered reads fewer groups" "$(val 'Columnar Chunk Groups Read' "$on")" 1
check "runtime answer equals off" "$(pc "${rf_on}$SQL"|tail -1)" "$(pc "${rf_off}$SQL"|tail -1)"
check "runtime answer equals heap" "$(pc "${rf_on}$SQL"|tail -1)" "$(q "SELECT count(*),sum(h.k),sum(length(h.p)) FROM h JOIN d ON h.k=d.k")"
for shape in \
 "LEFT|SELECT count(*) FROM f LEFT JOIN d ON f.k=d.k" \
 "SEMI|SELECT count(*) FROM f WHERE EXISTS(SELECT 1 FROM d WHERE d.k=f.k)" \
 "ANTI|SELECT count(*) FROM f WHERE NOT EXISTS(SELECT 1 FROM d WHERE d.k=f.k)" \
 "CROSS|SELECT count(*) FROM f CROSS JOIN d"; do
 n=${shape%%|*}; s=${shape#*|}; p="$(pc "${rf_on}EXPLAIN $s")"; check "$n refusal" "$(grep -c 'Columnar Runtime Filter Coordinator'<<<"$p")" 0
done

# Scattered keys: the interval hull spans every 1000-row group, so group
# pruning cannot be the thing that avoids payload work. Bloom must reject
# non-matches on the public EXPLAIN counter. 200 keys, each present once in
# 20000 fact rows, leaves 19800 non-matches; 15000 is a conservative bound
# below that after allowing Bloom false positives.
q "$(cat <<'SQL'
CREATE TABLE dim_bloom(k int);
INSERT INTO dim_bloom SELECT 25 + 100 * g FROM generate_series(0,199) g;
CREATE TABLE fact_bloom(k int, payload text) USING pgcolumnar;
SELECT pgcolumnar.set_options($t$fact_bloom$t$, stripe_row_limit => 1000);
INSERT INTO fact_bloom SELECT g, repeat(md5(g::text), 8) FROM generate_series(1,20000) g;
CREATE TABLE heap_bloom AS SELECT * FROM fact_bloom;
ANALYZE dim_bloom;
ANALYZE fact_bloom;
SQL
)" >/dev/null
SQLB="SELECT count(*),sum(fact_bloom.k),sum(length(fact_bloom.payload)) FROM fact_bloom JOIN dim_bloom ON fact_bloom.k=dim_bloom.k"
onb="$(pc "${rf_on}SET max_parallel_workers_per_gather=0;SET enable_nestloop=off;SET enable_mergejoin=off;EXPLAIN(ANALYZE,TIMING off,SUMMARY off)$SQLB")"
check "scattered plan has coordinator" "$(grep -c 'Columnar Runtime Filter Coordinator' <<<"$onb")" 1
check_num "scattered interval cannot drop groups" "$(val 'Runtime Filter Groups Removed' "$onb")" 0
check_num "scattered still reads every group" "$(val 'Columnar Chunk Groups Read' "$onb")" 20
rejected="$(val 'Runtime Filter Rows Rejected' "$onb")"
bloom_hit="$(awk -v r="${rejected:-0}" 'BEGIN { print (r+0 >= 15000) ? 1 : 0 }')"
check_num "scattered bloom rejects most non-matches" "$bloom_hit" 1
check "scattered answer equals heap" "$(pc "${rf_on}$SQLB"|tail -1)" "$(q "SELECT count(*),sum(heap_bloom.k),sum(length(heap_bloom.payload)) FROM heap_bloom JOIN dim_bloom ON heap_bloom.k=dim_bloom.k")"

# Cross-type int4 fact vs int8 dimension: hash both sides, but do not make an
# interval claim with mixed representations. 200 matching keys, 20000 fact rows.
q "$(cat <<'SQL'
CREATE TABLE dim_i8(k bigint);
INSERT INTO dim_i8 SELECT g FROM generate_series(8001,8200) g;
CREATE TABLE fact_i4(k int, payload text) USING pgcolumnar;
SELECT pgcolumnar.set_options($t$fact_i4$t$, stripe_row_limit => 1000);
INSERT INTO fact_i4 SELECT g, repeat(md5(g::text), 8) FROM generate_series(1,20000) g;
CREATE TABLE heap_i4 AS SELECT * FROM fact_i4;
ANALYZE dim_i8;
ANALYZE fact_i4;
SQL
)" >/dev/null
SQLX="SELECT count(*),sum(fact_i4.k),sum(length(fact_i4.payload)) FROM fact_i4 JOIN dim_i8 ON fact_i4.k=dim_i8.k"
onx="$(pc "${rf_on}SET max_parallel_workers_per_gather=0;SET enable_nestloop=off;SET enable_mergejoin=off;EXPLAIN(ANALYZE,TIMING off,SUMMARY off)$SQLX")"
check "int4/int8 plan has coordinator" "$(grep -c 'Columnar Runtime Filter Coordinator' <<<"$onx")" 1
xgrp="$(val 'Runtime Filter Groups Removed' "$onx")"
check_num "int4/int8 interval stays off" "${xgrp:-0}" 0
xrej="$(val 'Runtime Filter Rows Rejected' "$onx")"
xhit="$(awk -v r="${xrej:-0}" 'BEGIN { print (r+0 >= 15000) ? 1 : 0 }')"
check_num "int4/int8 bloom rejects most non-matches" "$xhit" 1
check "int4/int8 answer equals heap" "$(pc "${rf_on}$SQLX"|tail -1)" "$(q "SELECT count(*),sum(heap_i4.k),sum(length(heap_i4.payload)) FROM heap_i4 JOIN dim_i8 ON heap_i4.k=dim_i8.k")"

# Collation mismatch: operator collation is not the fact attribute's. Interval
# must not claim an ordering; Bloom may still reject. 200 text keys.
q "$(cat <<'SQL'
CREATE TABLE dim_txt(k text COLLATE "C");
INSERT INTO dim_txt SELECT (15000 + g)::text FROM generate_series(0,199) g;
CREATE TABLE fact_txt(k text COLLATE "C", payload text) USING pgcolumnar;
SELECT pgcolumnar.set_options($t$fact_txt$t$, stripe_row_limit => 1000);
INSERT INTO fact_txt SELECT g::text, repeat(md5(g::text), 8) FROM generate_series(1,20000) g;
CREATE TABLE heap_txt(k text COLLATE "C", payload text);
INSERT INTO heap_txt SELECT g::text, repeat(md5(g::text), 8) FROM generate_series(1,20000) g;
ANALYZE dim_txt;
ANALYZE fact_txt;
SQL
)" >/dev/null
SQLC=$(cat <<'SQL'
SELECT count(*),sum(length(fact_txt.k)),sum(length(fact_txt.payload))
FROM fact_txt JOIN dim_txt ON fact_txt.k = dim_txt.k COLLATE "POSIX"
SQL
)
# THE FIXTURE MUST BE ONE THE INTERVAL COULD PRUNE (#1339). The dimension keys used
# to span C-order "10060".."9960", which overlaps every fact group, so a wrongly
# attached interval removed nothing either and the "stays off" arm passed with the
# collation gate removed. The 200 keys "15000".."15199" leave every group whose keys
# start with 2-9 outside the hull, and the control below proves an interval prunes
# them when the collations DO match. Two hundred, not ten: under ten build rows no
# Bloom filter is built (measured: "Runtime Filter Bloom: false"), and the Bloom arm
# below would have nothing to measure. The heap mirror is written from the generator.
SQLM=$(cat <<'SQL'
SELECT count(*),sum(length(fact_txt.k)),sum(length(fact_txt.payload))
FROM fact_txt JOIN dim_txt ON fact_txt.k = dim_txt.k
SQL
)
onm="$(pc "${rf_on}SET max_parallel_workers_per_gather=0;SET enable_nestloop=off;SET enable_mergejoin=off;EXPLAIN(ANALYZE,TIMING off,SUMMARY off)$SQLM")"
mgrp="$(val 'Runtime Filter Groups Removed' "$onm")"
check "with matching collations the same fixture's interval removes row groups" \
	"$([ "${mgrp:-0}" -ge 1 ] && echo "removed ($mgrp)" || echo "REMOVED NONE")" "removed ($mgrp)"
onc="$(pc "${rf_on}SET max_parallel_workers_per_gather=0;SET enable_nestloop=off;SET enable_mergejoin=off;EXPLAIN(ANALYZE,TIMING off,SUMMARY off)$SQLC")"
check "collation-mismatch plan has coordinator" "$(grep -c 'Columnar Runtime Filter Coordinator' <<<"$onc")" 1
cgrp="$(val 'Runtime Filter Groups Removed' "$onc")"
check_num "collation-mismatch interval stays off" "${cgrp:-0}" 0
crej="$(val 'Runtime Filter Rows Rejected' "$onc")"
chit="$(awk -v r="${crej:-0}" 'BEGIN { print (r+0 >= 15000) ? 1 : 0 }')"
check_num "collation-mismatch bloom rejects most non-matches" "$chit" 1
check "collation-mismatch answer equals heap" "$(pc "${rf_on}$SQLC"|tail -1)" "$(q "$(cat <<'SQL'
SELECT count(*),sum(length(heap_txt.k)),sum(length(heap_txt.payload))
FROM heap_txt JOIN dim_txt ON heap_txt.k = dim_txt.k COLLATE "POSIX"
SQL
)")"

# Correlated LATERAL must rebuild the filter per outer parameter. 0 keeps all
# 200 keys; 8000 keeps g>=80, which is 120 keys in this fixture.
SQLR=$(cat <<'SQL'
SELECT v.x, s.c
FROM (VALUES (0),(8000)) v(x)
CROSS JOIN LATERAL (
  SELECT count(*) c
  FROM fact_bloom JOIN dim_bloom ON fact_bloom.k = dim_bloom.k
  WHERE dim_bloom.k > v.x
) s
ORDER BY 1
SQL
)
SQLRH=$(cat <<'SQL'
SELECT v.x, s.c
FROM (VALUES (0),(8000)) v(x)
CROSS JOIN LATERAL (
  SELECT count(*) c
  FROM heap_bloom JOIN dim_bloom ON heap_bloom.k = dim_bloom.k
  WHERE dim_bloom.k > v.x
) s
ORDER BY 1
SQL
)
check "rescan answers equal heap" "$(pc "${rf_on}SET max_parallel_workers_per_gather=0;$SQLR" | grep -v '^SET$')" "$(q "$SQLRH")"
# An empty build on one rescan must not carry into the next (#1447). An empty
# build now ends the join at once, so a state left over from the first outer row
# (99999 keeps no key) would answer the second (0 keeps all 200) with no rows.
SQLRE="${SQLR//(VALUES (0),(8000))/(VALUES (99999),(0))}"
SQLRHE="${SQLRH//(VALUES (0),(8000))/(VALUES (99999),(0))}"
check "premise: the empty-then-full rescan runs under the coordinator" \
	"$(pc "${rf_on}SET max_parallel_workers_per_gather=0;EXPLAIN (COSTS OFF) $SQLRE" | grep -c 'Columnar Runtime Filter Coordinator')" 1
check "premise: heap's empty-then-full rescan answer holds the 200 keys" "$(q "$SQLRHE" | head -1)" "0|200"
check "an empty build on one rescan does not empty the next" \
	"$(pc "${rf_on}SET max_parallel_workers_per_gather=0;$SQLRE" | grep -v '^SET$')" "$(q "$SQLRHE")"

# 220000 distinct build keys exceed d*10 vs 2^21, so Bloom must refuse rather
# than emit a saturated filter. Fact is larger so it remains the hash outer.
q "$(cat <<'SQL'
CREATE TABLE dim_sat(k int);
INSERT INTO dim_sat SELECT g FROM generate_series(1,220000) g;
CREATE TABLE fact_sat(k int) USING pgcolumnar;
SELECT pgcolumnar.set_options($t$fact_sat$t$, stripe_row_limit => 1000);
INSERT INTO fact_sat SELECT g FROM generate_series(1,300000) g;
CREATE TABLE heap_sat AS SELECT * FROM fact_sat;
ANALYZE dim_sat;
ANALYZE fact_sat;
SQL
)" >/dev/null
SQLS="SELECT count(*),sum(fact_sat.k) FROM fact_sat JOIN dim_sat ON fact_sat.k=dim_sat.k"
ons="$(pc "${rf_on}SET max_parallel_workers_per_gather=0;SET enable_nestloop=off;SET enable_mergejoin=off;EXPLAIN(ANALYZE,TIMING off,SUMMARY off)$SQLS")"
check "saturated plan has coordinator" "$(grep -c 'Columnar Runtime Filter Coordinator' <<<"$ons")" 1
check "saturated bloom is disabled" "$(grep -c 'Runtime Filter Bloom: false' <<<"$ons")" 1
check "saturated answer equals heap" "$(pc "${rf_on}$SQLS"|tail -1)" "$(q "SELECT count(*),sum(heap_sat.k) FROM heap_sat JOIN dim_sat ON heap_sat.k=dim_sat.k")"

# Three-table join: wrapping the columnar-outer hash join must not reorder the
# two heap dimensions relative to filter-off.
q "$(cat <<'SQL'
CREATE TABLE ja(k int);
INSERT INTO ja SELECT g FROM generate_series(1,200) g;
CREATE TABLE jb(k int, extra int);
INSERT INTO jb SELECT g, g+1 FROM generate_series(1,200) g;
CREATE TABLE jf(k int, payload text) USING pgcolumnar;
SELECT pgcolumnar.set_options($t$jf$t$, stripe_row_limit => 1000);
INSERT INTO jf SELECT g, repeat(md5(g::text), 4) FROM generate_series(1,5000) g;
ANALYZE ja;
ANALYZE jb;
ANALYZE jf;
SQL
)" >/dev/null
SQL3="SELECT count(*),sum(jf.k),sum(jb.extra) FROM jf JOIN ja ON jf.k=ja.k JOIN jb ON ja.k=jb.k"
rel_order() {
  python3 -c '
import json,sys
plan=json.loads(sys.stdin.read())
def walk(n):
    if isinstance(n, list):
        for x in n:
            yield from walk(x)
        return
    if not isinstance(n, dict):
        return
    name=n.get("Relation Name")
    if name:
        yield name
    for child in n.get("Plans") or []:
        yield from walk(child)
    child=n.get("Plan")
    if isinstance(child, dict):
        yield from walk(child)
print(" ".join(walk(plan)))
'
}
on3="$(pc "${rf_on}SET max_parallel_workers_per_gather=0;SET enable_nestloop=off;SET enable_mergejoin=off;EXPLAIN(FORMAT JSON, COSTS OFF)$SQL3" | grep -v '^SET$')"
off3="$(pc "${rf_off}SET max_parallel_workers_per_gather=0;SET enable_nestloop=off;SET enable_mergejoin=off;EXPLAIN(FORMAT JSON, COSTS OFF)$SQL3" | grep -v '^SET$')"
check "3-table plan has coordinator" "$(printf '%s\n' "$on3" | grep -c 'Columnar Runtime Filter Coordinator')" 1
check "3-table join order matches filter-off" "$(printf '%s\n' "$on3" | rel_order)" "$(printf '%s\n' "$off3" | rel_order)"
check "3-table answer equals filter-off" "$(pc "${rf_on}SET max_parallel_workers_per_gather=0;SET enable_nestloop=off;SET enable_mergejoin=off;$SQL3"|tail -1)" "$(pc "${rf_off}SET max_parallel_workers_per_gather=0;SET enable_nestloop=off;SET enable_mergejoin=off;$SQL3"|tail -1)"

# Empty build: Hash Join still exists, the coordinator still wraps it, and the
# answer is zero. With a dimension this small the hash join never runs its outer
# side at all, so this answer cannot fail on what the coordinator does with an
# empty build (#1339); the large-dimension arms below can.
q "$(cat <<'SQL'
CREATE TABLE dim_none(k int);
CREATE TABLE fact_none(k int, payload text) USING pgcolumnar;
SELECT pgcolumnar.set_options($t$fact_none$t$, stripe_row_limit => 1000);
INSERT INTO fact_none SELECT g, repeat(md5(g::text), 4) FROM generate_series(1,5000) g;
CREATE TABLE heap_none AS SELECT * FROM fact_none;
ANALYZE dim_none;
ANALYZE fact_none;
SQL
)" >/dev/null
SQLN="SELECT count(*),sum(fact_none.k) FROM fact_none JOIN dim_none ON fact_none.k=dim_none.k"
onn="$(pc "${rf_on}SET max_parallel_workers_per_gather=0;SET enable_nestloop=off;SET enable_mergejoin=off;EXPLAIN(ANALYZE,TIMING off,SUMMARY off)$SQLN")"
check "empty-build plan has coordinator" "$(grep -c 'Columnar Runtime Filter Coordinator' <<<"$onn")" 1
check "empty-build answer equals heap" "$(pc "${rf_on}$SQLN"|tail -1)" "$(q "SELECT count(*),sum(heap_none.k) FROM heap_none JOIN dim_none ON heap_none.k=dim_none.k")"
# An empty build from a LARGE dimension filtered to nothing (#1447). Here the hash
# join fetches its first outer row before it builds, so the fact scan runs. The
# coordinator found the build empty, and an inner join with an empty build has no
# rows, so the fact table must not be read. It was read whole: every fact row was
# rejected one at a time (5 of 5 groups read, 5000 rows rejected).
q "$(cat <<'SQL'
CREATE TABLE dim_gone(k int, v int);
INSERT INTO dim_gone SELECT g, g FROM generate_series(1,200000) g;
ANALYZE dim_gone;
SQL
)" >/dev/null
SQLE="SELECT count(*),sum(fact_none.k) FROM fact_none JOIN dim_gone ON fact_none.k=dim_gone.k WHERE dim_gone.v < 0"
one="$(pc "${rf_on}SET max_parallel_workers_per_gather=0;SET enable_nestloop=off;SET enable_mergejoin=off;EXPLAIN(ANALYZE,TIMING off,SUMMARY off)$SQLE")"
check "premise: the large-dimension empty build runs under the coordinator" "$(grep -c 'Columnar Runtime Filter Coordinator' <<<"$one")" 1
check "premise: and its build is empty" "$(val 'Runtime Filter Build Rows' "$one")" 0
check "an empty build reads no fact chunk group" "$(val 'Columnar Chunk Groups Read' "$one")" 0
check "an empty build from a large dimension answers as heap" "$(pc "${rf_on}$SQLE"|tail -1)" "$(q "SELECT count(*),sum(heap_none.k) FROM heap_none JOIN dim_gone ON heap_none.k=dim_gone.k WHERE dim_gone.v < 0")"

# Projection-backed outer scans are excluded: a covering projection with a
# sort-key restriction is cheaper than the base scan, so Hash Join would wrap
# it if the coordinator did not refuse custom_private != NIL.
q "$(cat <<'SQL'
CREATE TABLE dim_pj(k int);
INSERT INTO dim_pj SELECT g FROM generate_series(1,200) g;
CREATE TABLE fact_pj(k int, payload text) USING pgcolumnar;
SELECT pgcolumnar.set_options($t$fact_pj$t$, stripe_row_limit => 1000);
INSERT INTO fact_pj SELECT g, repeat(md5(g::text), 4) FROM generate_series(1,4000) g ORDER BY md5(g::text);
SELECT pgcolumnar.add_projection($t$fact_pj$t$, $n$byk$n$, ARRAY['k','payload'], ARRAY['k']);
CREATE TABLE heap_pj AS SELECT * FROM fact_pj;
ANALYZE dim_pj;
ANALYZE fact_pj;
SQL
)" >/dev/null
SQLP="SELECT count(*),sum(fact_pj.k) FROM fact_pj JOIN dim_pj ON fact_pj.k=dim_pj.k WHERE fact_pj.k BETWEEN 1 AND 200"
onp="$(pc "${rf_on}SET max_parallel_workers_per_gather=0;SET enable_nestloop=off;SET enable_mergejoin=off;EXPLAIN(ANALYZE,TIMING off,SUMMARY off)$SQLP")"
check "projection scan is chosen" "$(grep -c 'Columnar Projection:' <<<"$onp")" 1
check "projection outer is not wrapped" "$(grep -c 'Columnar Runtime Filter Coordinator' <<<"$onp")" 0
check "projection join equals heap" "$(pc "${rf_on}$SQLP"|tail -1)" "$(q "SELECT count(*),sum(h.k) FROM heap_pj h JOIN dim_pj ON h.k=dim_pj.k WHERE h.k BETWEEN 1 AND 200")"

# Early LIMIT on a compact key range. The coordinator used to SIGSEGV while
# draining the tap through ExecProcNode on this shape; a heap control with the
# same ORDER BY is the public answer.
q "$(cat <<'SQL'
CREATE TABLE dim_lim(k int);
INSERT INTO dim_lim SELECT g FROM generate_series(300,420) g;
CREATE TABLE fact_lim(k int, payload text) USING pgcolumnar;
SELECT pgcolumnar.set_options($t$fact_lim$t$, stripe_row_limit => 1000);
INSERT INTO fact_lim SELECT g, repeat(md5(g::text), 4) FROM generate_series(1,2500) g;
CREATE TABLE heap_lim AS SELECT * FROM fact_lim;
ANALYZE dim_lim;
ANALYZE fact_lim;
SQL
)" >/dev/null
SQLL="SELECT fact_lim.k FROM fact_lim JOIN dim_lim ON fact_lim.k=dim_lim.k ORDER BY fact_lim.k LIMIT 3"
onl="$(pc "${rf_on}SET max_parallel_workers_per_gather=0;SET enable_nestloop=off;SET enable_mergejoin=off;EXPLAIN(ANALYZE,TIMING off,SUMMARY off)$SQLL")"
check "early limit has coordinator" "$(grep -c 'Columnar Runtime Filter Coordinator' <<<"$onl")" 1
check "early limit equals heap" "$(pc "${rf_on}SET max_parallel_workers_per_gather=0;SET enable_nestloop=off;SET enable_mergejoin=off;$SQLL" | grep -v '^SET$')" "$(q "SELECT heap_lim.k FROM heap_lim JOIN dim_lim ON heap_lim.k=dim_lim.k ORDER BY heap_lim.k LIMIT 3")"
# Fact-table local qual must see every column it names. The coordinator
# used to mark only the join key, so a conjunction that also named a
# non-key column dropped every surviving row. Independent of the pytest
# twin: different names, key range, and row count.
q "$(cat <<'SQL'
CREATE TABLE dim_fq(k int);
INSERT INTO dim_fq SELECT g FROM generate_series(5500, 5699) g;
CREATE TABLE fact_fq(k int, extra int, payload text) USING pgcolumnar;
SELECT pgcolumnar.set_options($t$fact_fq$t$, stripe_row_limit => 1000);
INSERT INTO fact_fq SELECT g, g, repeat(md5(g::text), 4)
FROM generate_series(1, 12000) g;
CREATE TABLE heap_fq AS SELECT * FROM fact_fq;
ANALYZE dim_fq;
ANALYZE fact_fq;
SQL
)" >/dev/null
SQLFQ="SELECT count(*),sum(fact_fq.k),sum(fact_fq.extra) FROM fact_fq JOIN dim_fq ON fact_fq.k=dim_fq.k WHERE fact_fq.k > 100 AND fact_fq.extra % 4 <> 0"
onfq="$(pc "${rf_on}SET max_parallel_workers_per_gather=0;SET enable_nestloop=off;SET enable_mergejoin=off;EXPLAIN(ANALYZE,TIMING off,SUMMARY off)$SQLFQ")"
check "fact-qual plan has coordinator" "$(grep -c 'Columnar Runtime Filter Coordinator' <<<"$onfq")" 1
# The conjunction must REMOVE some matching rows (#1339). "extra > 40" held for
# every one of the 200 matches, so an answer that ignored the qual equalled heap's.
check "premise: the fact qual removes matching rows on heap" \
	"$(q "SELECT count(*) FROM heap_fq JOIN dim_fq ON heap_fq.k=dim_fq.k WHERE heap_fq.k > 100 AND heap_fq.extra % 4 <> 0")|$(q "SELECT count(*) FROM heap_fq JOIN dim_fq ON heap_fq.k=dim_fq.k")" "150|200"
check "fact-qual conjunction equals heap" "$(pc "${rf_on}$SQLFQ"|tail -1)" "$(q "SELECT count(*),sum(heap_fq.k),sum(heap_fq.extra) FROM heap_fq JOIN dim_fq ON heap_fq.k=dim_fq.k WHERE heap_fq.k > 100 AND heap_fq.extra % 4 <> 0")"
# A gating qual, a WHERE that names no column (#1437). The planner puts a
# Result with a One-Time Filter over the join, so the coordinator's child was a
# Result rather than the Hash Join it expects, and every such query failed with
# "expected a core Hash Join", EXPLAIN included. A true gate must give heap's
# rows and a false one none, over a heap and a columnar dimension. The premises
# show the same join with no gate takes the coordinator, so the gate is what the
# arms vary. Independent of the pytest twin: different names, key range, and
# row count.
q "$(cat <<'SQL'
CREATE TABLE dim_gh(k int);
INSERT INTO dim_gh SELECT g FROM generate_series(3100, 3299) g;
CREATE TABLE dim_gc(k int) USING pgcolumnar;
INSERT INTO dim_gc SELECT g FROM generate_series(3100, 3299) g;
CREATE TABLE fact_g(k int, payload text) USING pgcolumnar;
SELECT pgcolumnar.set_options($t$fact_g$t$, stripe_row_limit => 1000);
INSERT INTO fact_g SELECT g, repeat(md5(g::text), 4) FROM generate_series(1, 15000) g;
CREATE TABLE heap_g AS SELECT * FROM fact_g;
ANALYZE dim_gh;
ANALYZE dim_gc;
ANALYZE fact_g;
SQL
)" >/dev/null
for dim in dim_gh dim_gc; do
	kind=heap; [ "$dim" = dim_gc ] && kind=columnar
	SQLG="SELECT count(*),sum(fact_g.k),sum(length(fact_g.payload)) FROM fact_g JOIN $dim ON fact_g.k=$dim.k"
	ong="$(pc "${rf_on}SET max_parallel_workers_per_gather=0;SET enable_nestloop=off;SET enable_mergejoin=off;EXPLAIN(ANALYZE,TIMING off,SUMMARY off)$SQLG")"
	check "premise: with no gate, the $kind-dimension join has the coordinator" \
		"$(grep -c 'Columnar Runtime Filter Coordinator' <<<"$ong")" 1
	# the reference must hold rows, or a server that answered nothing on both
	# sides would pass the comparisons below as equal
	check "premise: heap's answer under a true gate over a $kind dimension holds 200 rows" \
		"$(q "SELECT count(*) FROM heap_g JOIN $dim ON heap_g.k=$dim.k WHERE (SELECT true)")" 200
	for gate in false true; do
		check "a (SELECT $gate) gate over a $kind dimension equals heap" \
			"$(pc "${rf_on}SET max_parallel_workers_per_gather=0;SET enable_nestloop=off;SET enable_mergejoin=off;$SQLG WHERE (SELECT $gate)" | tail -1)" \
			"$(q "SELECT count(*),sum(heap_g.k),sum(length(heap_g.payload)) FROM heap_g JOIN $dim ON heap_g.k=$dim.k WHERE (SELECT $gate)")"
	done
done
pgc_summary
