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
# Same per-row-group page rounding the cost model uses (COLUMNAR_PAGE_ROUND_UP).
proj_pages="$(q "SELECT coalesce(sum(ceil(rg.byte_length::numeric / (8192 - 24))),0) FROM pgcolumnar.row_group rg JOIN pgcolumnar.projection p ON p.proj_storage_id = rg.storage_id JOIN pgcolumnar.storage s ON s.storage_id = p.storage_id WHERE s.relation_oid = 'psio'::regclass AND p.name = 'byik'")"
rel_bytes="$(q "SELECT pg_relation_size('psio')")"
want_run="$(awk -v p="$proj_pages" -v c="$page_cost" "BEGIN{ print p*c }")"

# Base scan with projection off, printed here for a reader. It is NOT the oracle
# the miss arm divides by; that one is read below, in the same catalog state as
# the arm.
base_plan="$(explain_scan off "$SQL")"
base_run="$(run_of "$base_plan")"

echo "-- cover_run=$c_run rel_pages=$rel_pages base_io=$base_io ratio=$ratio"
echo "-- proj_bytes=$proj_bytes rel_bytes=$rel_bytes proj_pages=$proj_pages want_run=$want_run"
echo "-- base_run=$base_run"

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
	"$(awk -v p="$proj_bytes" -v r="$rel_bytes" "BEGIN{ print (r>0 && p < r*0.7) ? \"minority\" : \"majority p=\" p \" r=\" r }")" \
	"minority"

# Band around seq_page_cost * proj_pages (sel=1), not a ceiling against
# base_io. A half-priced projection still clears a 0.8 ceiling.
check "a covering projection is not priced from the base table's pages" \
	"$(awk -v g="$c_run" -v w="$want_run" "BEGIN{ if (w<=0) print \"no-want\"; else { d=(g>w?g-w:w-g)/w; print (d>0.05) ? \"off-band got=\" g \" want=\" w : \"proj-pages\" } }")" \
	"proj-pages"

# Lookup failure must not make the covering path cheaper than the base file.
# Returning 1 page is essentially free; rel->pages cannot undercut the base
# scan. The path is still offered: choose_projection keys on the name.
psql_run "UPDATE pgcolumnar.projection p SET proj_storage_id = 0 FROM pgcolumnar.storage s WHERE p.storage_id = s.storage_id AND s.relation_oid = 'psio'::regclass AND p.name = 'byik' AND p.projection_id > 0;"

check "premise: the projection row still exists after its storage id is cleared" \
	"$(q "SELECT count(*) FROM pgcolumnar.projection p JOIN pgcolumnar.storage s ON s.storage_id = p.storage_id WHERE s.relation_oid = 'psio'::regclass AND p.name = 'byik' AND p.proj_storage_id = 0")" \
	"1"

# THE ORACLE AND THE ARM MUST BE READ IN THE SAME CATALOG STATE (#1155 review).
# The UPDATE above clears proj_storage_id, which is the SAME KEY #1180's sibling
# walk reads. So a base_run captured before it is priced with the projection's
# pages subtracted and a miss_run captured after it is priced from the whole
# file, and the ratio spans two different trees rather than measuring one.
#
# Composed with main carrying #1180, the old order read
#
#     miss_run=41991.6 base_run=22000 miss_ratio=1.909     FAIL
#
# and re-reading the oracle here gives base_run=42000 and miss_ratio=1.000. The
# comment above used to claim this oracle was "independent of how rel->pages is
# computed", which is true of the covering arm -- whose want_run comes from the
# catalog and never mentions rel->pages -- and false of this one, whose oracle is
# a measured plan cost and moves with rel->pages like everything else.
#
# Before #1180 both states gave rel->pages = 42 and the ratio was exactly 1.000,
# so nothing here could have noticed.
miss_base_run="$(run_of "$(explain_scan off "$SQL")")"
miss_plan="$(explain_scan on "$SQL")"
miss_run="$(run_of "$miss_plan")"
miss_ratio="$(awk -v m="$miss_run" -v b="$miss_base_run" "BEGIN{ if (b<=0) print 0; else printf \"%.3f\", m/b }")"
echo "-- miss_run=$miss_run base_run=$miss_base_run miss_ratio=$miss_ratio"

check "premise: a covering path is still offered when projection storage cannot be found" \
	"$(echo "$miss_plan" | grep -c 'Columnar Projection: byik')" "1"

# Fallback to rel->pages must price like the base scan itself, not like one
# page. Compare to the measured base run (projection off), not to base_io
# from pg_relation_size, so #1180 cannot silently shift the denominator.
check "a covering projection whose storage cannot be found is not priced as one page" \
	"$(awk -v r="$miss_ratio" "BEGIN{ print (r+0 < 0.8 || r+0 > 1.25) ? \"moved miss_ratio=\" r : \"not-one-page\" }")" \
	"not-one-page"

# ---- the third return: a projection that IS found and holds no row groups ----
#
# pgcolumnar_projection_pages has THREE returns and the arms above reach two:
# the miss (proj_storage_id = 0 -> fallbackPages) and the covering arm. The
# third is `if (pages < 1) pages = 1`, and it is not the "small projection"
# case. COLUMNAR_PAGE_ROUND_UP rounds every group to a WHOLE page, so one row is
# already a full page and returns through the covering arm. Probed at all three
# returns on 8ea98fc:
#
#     empty table + projection   bytes=0       pages=0   this return
#     one row                    bytes=8168    pages=1   covering return
#     32000 rows                 bytes=261376  pages=32  covering return
#
# So the state under test is a projection whose storage holds ZERO row groups --
# a projection on a relation nothing has been written to -- and NOT a small one.
# An arm built on "a tiny projection" measures the covering return and reports
# green having never reached this line (#1208).
#
# THE ORACLE IS THE PLAN, NOT A COST. Priced at one page the covering path wins;
# mutated to `pages = 997` it loses and the projection disappears from the plan.
# A pinned cost would move with every unrelated change to the cost model, and
# explain_scan already sets seq_page_cost = 1000, so one page against 997 is not
# a marginal difference.
psql_run "CREATE TABLE psio_e (ik int, pad text) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('psio_e', stripe_row_limit => 1000, chunk_group_row_limit => 100);"
psql_run "SELECT pgcolumnar.add_projection('psio_e', 'byik', ARRAY['ik'], ARRAY['ik']);"
psql_run "ANALYZE psio_e;"

# BOTH HALVES IN ONE READING: the projection must be FOUND (so this is not the
# miss arm again) and its storage must hold nothing (so pages comes out 0).
#
# KEYED ON get_storage_id, NOT ON pgcolumnar.storage. A relation that has never
# been written to has NO storage catalog row at all -- measured here, 0 rows for
# psio_e -- while get_storage_id still returns its id, because that comes from
# the relation's metapage. Joining through pgcolumnar.storage returns the empty
# string and the premise fails for a reason that has nothing to do with the
# projection.
check "premise: the empty projection is found and its storage holds no row groups" \
	"$(q "SELECT (p.proj_storage_id <> 0)::text || '/' || count(rg.*)::text
	      FROM pgcolumnar.projection p
	      LEFT JOIN pgcolumnar.row_group rg ON rg.storage_id = p.proj_storage_id
	      WHERE p.storage_id = pgcolumnar.get_storage_id('psio_e')
	        AND p.name = 'byik' AND p.projection_id > 0
	      GROUP BY p.proj_storage_id;")" \
	"true/0"

psio_e_plan="$(explain_scan on "SELECT ik FROM psio_e WHERE ik BETWEEN 1 AND 50")"
check "an empty covering projection is still priced as one page, so it is chosen" \
	"$(printf '%s\n' "$psio_e_plan" | grep -c 'Columnar Projection: byik')" \
	"1"

# THE OTHER DIRECTION (#1339). The arm above catches over-pricing only: priced
# at 0 pages instead of 1, the projection is cheaper still and stays chosen.
# With every CPU cost set to 0 by explain_scan, the projection scan's whole cost
# is its page cost, so a total of zero means it was priced at zero pages.
# Measured on PG17: 5.00 with the clamp, 0.00 with `if (pages < 1) pages = 1;`
# deleted, and the projection chosen both times.
psio_e_cost="$(printf '%s\n' "$psio_e_plan" | grep -m1 'Custom Scan' |
	sed -n 's/.*cost=[0-9.]*\.\.\([0-9.]*\) .*/\1/p')"
check "and its scan is not priced at zero pages" \
	"$(awk -v c="${psio_e_cost:-none}" 'BEGIN { if (c ~ /^[0-9]+(\.[0-9]+)?$/) print (c + 0 > 0 ? "priced" : "free at " c); else print "unread: " c }')" \
	"priced"

pgc_summary
