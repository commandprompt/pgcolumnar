#!/usr/bin/env bash
# Regression for the shared full-scan cost helper (#12): the grouped vector
# aggregate's no-serial-survivor fallback must price its input scan the same way
# the columnar scan node does (projected width, per-column decode CPU, zone-map
# survival), not with the bare seqscan formula that under-priced it on a wide
# table. This asserts the fold path is chosen, that it returns the same aggregate
# a plain (non-fold) plan does on a WIDE table, and that the fallback prices the
# fold node exactly as a surviving serial scan does.
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# wide table: many columns so the per-column decode CPU term the fallback was
# missing is material.
DEF="g int"; for i in $(seq 0 24); do DEF="$DEF, c$i bigint"; done
q "CREATE TABLE wa ($DEF) USING pgcolumnar" >/dev/null
VALS="g%16$(for i in $(seq 0 24); do printf ', g*%d' $((i + 1)); done)"
q "INSERT INTO wa SELECT $VALS FROM generate_series(1, 40000) g" >/dev/null
q "ANALYZE wa" >/dev/null

# Each reading's settings ride in PGOPTIONS rather than a SET, so a failed query
# prints nothing and check_text refuses it. The earlier form kept the last line of
# "SET ...; SELECT ...", which for a failed SELECT was the SET's own tag: both
# sides read "SET" and agreed.
qn() {	# SQL [PGOPTIONS]
	env PATH="$PGC_BINDIR:$PATH" PGOPTIONS="${2:-}" psql -h 127.0.0.1 -p "$PGC_PORT" \
		-U postgres -d "$PGC_DB" -At -c "$1" 2>/dev/null || true
}
# the fold node's total cost, or empty when the plan is not the fold node
fold_cost() {	# PGOPTIONS
	qn "EXPLAIN SELECT g, sum(c0) FROM wa GROUP BY g;" "$1" \
		| awk 'NR == 1 && /^Custom Scan \(PgColumnarScan\)/ { f = 1
			if (match($0, /cost=[0-9.]+\.\.[0-9.]+/)) {
				c = substr($0, RSTART + 5, RLENGTH - 5); sub(/^[0-9.]+\.\./, "", c) } }
			f && /Columnar Batch Fold: yes/ { print c; exit }'
}

# The whole grouped result reduced to one comparable string, computed in SQL so
# there is no shell ordering or row-count artifact.
Q="SELECT string_agg(g||':'||s0||':'||s24||':'||c, '|' ORDER BY g)
   FROM (SELECT g, sum(c0) s0, sum(c24) s24, count(*) c FROM wa GROUP BY g) r"
EXPLAINQ="SELECT g, sum(c0) FROM wa GROUP BY g"

# reference: no vectorized grouping
ref="$(qn "$Q" "-c pgcolumnar.enable_group_vectorization=off -c pgcolumnar.enable_ungrouped_vector_agg=off")"

FOLD="-c pgcolumnar.enable_ungrouped_vector_agg=on -c pgcolumnar.enable_parallel_vector_agg=on -c max_parallel_workers_per_gather=4"
got="$(qn "$Q" "$FOLD")"

# the fold node by its own line, not any custom scan: the reference plan's input
# is a Custom Scan too
plan="$(qn "EXPLAIN (COSTS OFF, VERBOSE) $EXPLAINQ" "$FOLD")"
foldcnt="$(printf '%s' "$plan" | grep -c 'Columnar Batch Fold: yes')"
echo "-- fold path markers: $foldcnt"

check "the fold path is used on the wide grouped aggregate" \
	"$([ "$foldcnt" -gt 0 ] && echo yes)" "yes"
check_text "the fold result matches a plain aggregate on a wide table" "$got" "$ref"

# --- the fallback this suite is named for ---------------------------------------
#
# With 40,000 rows a serial scan survives add_path, so the readings above price the
# fold from it and the fallback never runs (traced: a bare-seqscan fallback leaves
# every reading above unchanged). Free parallelism lets a Gather displace the
# serial scan, which is when the fallback prices the scan itself; hash and sort off
# keep the parallel finalize from winning, so the fold node is the plan and its
# cost can be read. Measured on PG17: 800.19 both ways, and 503.01 through the
# fallback with the bare seqscan formula.
FREE="-c parallel_setup_cost=0 -c parallel_tuple_cost=0 -c min_parallel_table_scan_size=0 -c max_parallel_workers_per_gather=4"
FREEPLAN="$(qn "EXPLAIN (COSTS OFF) $EXPLAINQ" "$FREE")"
case "$FREEPLAN" in *Gather*) FREEGATHER=yes ;; *) FREEGATHER="no Gather in: $FREEPLAN" ;; esac
check "premise: with parallelism free, a Gather displaces the serial scan" "$FREEGATHER" "yes"
check_text "the fallback prices the fold as a surviving serial scan does" \
	"$(fold_cost "$FREE -c enable_hashagg=off -c enable_sort=off")" "$(fold_cost "")"

check "backend alive" "$(q 'SELECT 1')" "1"
pgc_summary
