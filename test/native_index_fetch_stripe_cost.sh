#!/usr/bin/env bash
# Regression: the index-fetch cost penalty must size row groups by the table's
# effective stripe_row_limit (the per-table option when set), not the GUC. The
# penalty exists to price the row-group decode a columnar index fetch forces, and
# that decode's size is the effective limit. Reading only the GUC mis-prices a
# table that set the option. Here, changing the per-table option must change the
# estimated index-scan cost; on the unfixed build it did not.
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

q "CREATE TABLE ct (id int, v int) USING pgcolumnar" >/dev/null
q "INSERT INTO ct SELECT g, g FROM generate_series(1, 200000) g" >/dev/null
q "CREATE INDEX ct_id ON ct (id)" >/dev/null
q "ANALYZE ct" >/dev/null
check "premise: the table holds all 200000 rows" "$(q 'SELECT count(*) FROM ct')" "200000"

# 1,000 ROWS, NOT 100,000 (#1339). At 100,000 rows the plan flips to the custom
# scan at stripe_row_limit 150,000, so the two costs this compared were the prices
# of two DIFFERENT plans and differed on a plan flip alone. Below 20,000 rows both
# arms stay on the index (measured in the pytest twin's docstring), so a difference
# is the price of one plan shape -- and the shape is asserted before the prices.
topplan() {  # $1 = on|off for the fetch penalty: the top plan line of the index-forced scan
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -qtA \
		-c "SET enable_seqscan=off" \
		-c "SET pgcolumnar.enable_index_fetch_penalty = $1" \
		-c "EXPLAIN (COSTS on, FORMAT text) SELECT * FROM ct WHERE id BETWEEN 1 AND 1000" 2>/dev/null \
		| head -1
}
cost_of() { grep -oiE 'cost=[0-9.]+\.\.[0-9.]+' <<<"$1" | head -1 | sed 's/.*\.\.//'; }
node_of() { sed -E 's/^ *(->)? *//; s/ \(.*//; s/ (using|on) .*//' <<<"$1"; }

q "SELECT pgcolumnar.set_options('ct', stripe_row_limit => 2000)" >/dev/null
p1="$(topplan on)"; c1="$(cost_of "$p1")"
q "SELECT pgcolumnar.set_options('ct', stripe_row_limit => 150000)" >/dev/null
p2="$(topplan on)"; c2="$(cost_of "$p2")"
echo "-- index-scan total cost: stripe_row_limit=2000 -> $c1 ; =150000 -> $c2"
echo "-- top node: $(node_of "$p1") then $(node_of "$p2")"

# NAMED FOR WHAT IT MEASURES (#1339). It was called "the index-fetch cost responds
# to the per-table stripe_row_limit", but it reads one cost, so it cannot see a
# response; the arm below that compares two costs does. And it asks for a cost of at
# least 1, as the pytest twin does, not only for digits. A mutation that set the
# penalised path's total cost to 0 printed "0.00": digits, so this arm passed while
# the twin went red.
check "the index-fetch cost is a number of at least 1" \
	"$(case "$c1" in ''|*[!0-9.]*) echo "not a number: [$c1]" ;; \
		*) awk -v c="$c1" 'BEGIN{ print (c >= 1) ? "at least 1" : "below 1: " c }' ;; esac)" "at least 1"
check "both arms priced the same plan shape" "$(node_of "$p2")" "$(node_of "$p1")"
check "the priced plan is the per-row index fetch the penalty prices" "$(node_of "$p1")" "Index Scan"
check "changing the per-table stripe_row_limit changes the estimated cost" \
	"$([ -n "$c1" ] && [ -n "$c2" ] && [ "$c1" != "$c2" ] && echo differ)" "differ"

# THE ATTRIBUTION CELL: with the penalty off the same two arms collapse onto one
# number, which names the penalty as the term that carries the option.
q "SELECT pgcolumnar.set_options('ct', stripe_row_limit => 2000)" >/dev/null
o1="$(cost_of "$(topplan off)")"
q "SELECT pgcolumnar.set_options('ct', stripe_row_limit => 150000)" >/dev/null
o2="$(cost_of "$(topplan off)")"
echo "-- penalty off: 2000 -> $o1 ; 150000 -> $o2"
check "with the fetch penalty off the option reaches no other cost term" \
	"$([ -n "$o1" ] && echo "$o1" || echo none)" "$o2"
check "backend alive" "$(q 'SELECT 1')" "1"
pgc_summary
