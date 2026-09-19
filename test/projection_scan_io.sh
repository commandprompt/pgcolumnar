#!/usr/bin/env bash
#
# pgColumnar: a covering projection must be priced from its own pages.
#
# PgColumnarSetRelPathlist offers a covering-projection path by scaling the
# BASE scan's run cost. That run's I/O term is seq_page_cost * rel->pages,
# the whole relation file (base plus every projection). A covering projection
# is stored as its own row groups; charging the base page count prices that
# scan as a full-table read of a file that also holds the base copy.
#
# This suite pins the PLANNER number, not a runtime. Independent of
# test/pytest/test_projection_scan_io.py: same public seam (EXPLAIN cost of a
# covering projection vs the relation's pages), own fixture, own observations.
#
# Usage:  test/projection_scan_io.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/lib/postgresql/18/bin/pg_config}"

N=24000
psql_run "CREATE TABLE psio (ik int, bulky text) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('psio', stripe_row_limit => 1200, chunk_group_row_limit => 400);"
# Compressible payload so ANALYZE width is large while stored bytes stay
# modest. The covering projection is a second copy of the same columns,
# so rel->pages counts base plus projection.
psql_run "INSERT INTO psio SELECT ik, repeat('b', 900) FROM generate_series(1, $N) ik ORDER BY md5(ik::text);"
psql_run "SELECT pgcolumnar.add_projection('psio', 'byik', ARRAY['ik','bulky'], ARRAY['ik']);"
psql_run "ANALYZE psio;"

explain_scan() {
	# $1 = on|off for pgcolumnar.enable_projection_scan
	# $2 = SQL
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -Atq \
		-c "SET max_parallel_workers_per_gather = 0;" \
		-c "SET pgcolumnar.enable_ungrouped_vector_agg = off;" \
		-c "SET pgcolumnar.enable_group_vectorization = off;" \
		-c "SET jit = off;" \
		-c "SET seq_page_cost = 1000;" \
		-c "SET cpu_tuple_cost = 0;" \
		-c "SET cpu_operator_cost = 0;" \
		-c "SET cpu_index_tuple_cost = 0;" \
		-c "SET pgcolumnar.enable_projection_scan = $1;" \
		-c "EXPLAIN (COSTS ON) $2" \
		| grep -v '^SET$'
}

scan_cost_pair() {
	echo "$1" | grep -F "Custom Scan (PgColumnarScan)" | head -1 \
		| grep -oE "cost=[0-9.]+\.\.[0-9.]+" | head -1 \
		| sed -E "s/cost=([0-9.]+)\\.\\.([0-9.]+)/\\1 \\2/"
}

run_of() {
	local pair start total
	pair="$(scan_cost_pair "$1")"
	start="${pair%% *}"
	total="${pair##* }"
	awk -v t="$total" -v s="$start" "BEGIN{ print t-s }"
}

SQL="SELECT ik, bulky FROM psio WHERE ik BETWEEN 1 AND $N"
cover_plan="$(explain_scan on "$SQL")"
c_run="$(run_of "$cover_plan")"

rel_pages="$(q "SELECT pg_relation_size('psio') / 8192.0")"
page_cost=1000
base_io="$(awk -v p="$rel_pages" -v c="$page_cost" "BEGIN{ print p*c }")"
ratio="$(awk -v r="$c_run" -v i="$base_io" "BEGIN{ if (i<=0) print 0; else printf \"%.3f\", r/i }")"

proj_bytes="$(q "SELECT coalesce(sum(rg.byte_length),0) FROM pgcolumnar.row_group rg JOIN pgcolumnar.projection p ON p.proj_storage_id = rg.storage_id JOIN pgcolumnar.storage s ON s.storage_id = p.storage_id WHERE s.relation_oid = 'psio'::regclass AND p.name = 'byik'")"
rel_bytes="$(q "SELECT pg_relation_size('psio')")"

echo "-- cover_run=$c_run rel_pages=$rel_pages base_io=$base_io ratio=$ratio"
echo "-- proj_bytes=$proj_bytes rel_bytes=$rel_bytes"

check "premise: the table holds every inserted row" \
	"$(q "SELECT count(*) FROM psio")" "$N"

check "premise: a covering projection exists" \
	"$(q "SELECT count(*) FROM pgcolumnar.projection_declaration WHERE rel = 'psio'::regclass AND name = 'byik'")" "1"

check "premise: the plan uses the covering projection" \
	"$(echo "$cover_plan" | grep -c 'Columnar Projection: byik')" "1"

check "premise: the covering scan has a positive run cost" \
	"$(awk -v c="$c_run" "BEGIN{ print (c>0) ? \"yes\" : \"no\" }")" "yes"

# Without this, a pass could mean the projection filled the file and both
# formulae agree. Charging rel->pages and charging projection pages must
# not look the same.
check "premise: the covering projection occupies a minority of the relation" \
	"$(awk -v p="$proj_bytes" -v r="$rel_bytes" "BEGIN{ print (r>0 && p < r*0.7) ? \"minority\" : \"majority\" }")" \
	"minority"

# Unfixed: covering I/O is seq_page_cost * rel->pages * sel ~= 1.0 of base_io.
# Fixed: the same path charges the projection's own pages, a minority.
check "a covering projection is not priced from the base table's pages" \
	"$(awk -v r="$ratio" "BEGIN{ print (r+0 > 0.8) ? \"base-pages\" : \"proj-pages\" }")" \
	"proj-pages"

pgc_summary
