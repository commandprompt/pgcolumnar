#!/usr/bin/env bash
#
# pgColumnar #1296 phase 2: approximate distinct counts and percentiles come from
# the hll and tdigest extensions, and this suite is the compatibility statement
# for them. Phase 0 on #1296 measured both reading through pgColumnar's own scan
# and decided against building sketches here for 1.0 (#1309 is the post-1.0 idea).
#
# WHAT IS ASSERTED, AND WHY EACH BOUND IS WHAT IT IS:
#   - over a columnar table each extension gives EXACTLY what it gives over the
#     same rows in heap: the hll sketch byte for byte, the tdigest percentile to
#     the last bit. Both read the rows in the same order here (no parallelism), and
#     an hll is order-independent anyway, so any difference is ours.
#   - against the exact answer, only the extension's own documented error. These
#     are PREMISES, as partman_columnar's are: they state what the extension does,
#     and a change here that broke them would break the equalities above first. hll at
#     its default precision (log2m 11) has a standard error of 1.04/sqrt(2048), so
#     three of them, 6.9%; tdigest at compression 100 within 0.1% at p99, about
#     four times what phase 0 measured (0.025%).
#   - each type stored in a columnar table comes back byte for byte, and its union
#     still answers. For tdigest that union's bound is a premise only because the
#     byte-for-byte arm comes first: a storage change that altered a digest reddens
#     the arm before it can move the percentile.
#   - the aggregate reads through the columnar scan, so the arms exercise our reader.
#     That is an arm, not a premise: turning the custom scan off by default reddens it
#     (#1236: a check a repository change can redden is not a premise).
#
# hll 2.19 does not build against PostgreSQL 19 (FuncnameGetCandidates gained an
# argument), so on 19 the hll arms are named skips that say so. Everywhere else a
# missing extension FAILS (lib.sh's rule); CI installs both from PGDG.
#
# Usage:  test/approx_aggregates.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

q "CREATE EXTENSION IF NOT EXISTS pgcolumnar;" >/dev/null
P0="SET max_parallel_workers_per_gather = 0;"
# The value only: psql prints SET ahead of it when P0 leads the command.
qv() { q "$1" | tail -1; }
# 200,000 rows over 7 days and 50,000 devices; v is spread over 0..999.99.
# THE HEAP MIRROR IS WRITTEN FROM THE GENERATOR, NOT COPIED FROM ax. A CTAS from ax
# reads ax through the scan under test, so a scan that lost rows lost them from both
# sides and every equality below held: measured, with a scan dropping one row in a
# thousand, all of them stayed green on 15-19.
GEN="SELECT g % 7, ((g::bigint * 7919) % 50000)::int, ((g::bigint * 104729) % 100000) / 100.0
	  FROM generate_series(1, 200000) g"
psql_run "CREATE TABLE ax (day int, dev int, v float8) USING pgcolumnar;
	INSERT INTO ax $GEN;
	CREATE TABLE ax_heap (day int, dev int, v float8) USING heap;
	INSERT INTO ax_heap $GEN;"
check_num "the columnar table returns the 200,000 rows written, over 50,000 devices" \
	"$(q "SELECT count(*) + count(DISTINCT dev) FROM ax;")" "250000"
within() {	# within GOT WANT RELATIVE -> ok, or the numbers
	awk -v g="$1" -v w="$2" -v r="$3" 'BEGIN { d = (g - w) / w; if (d < 0) d = -d;
		if (g != "" && w != "" && d <= r) print "ok"; else printf "got %s want %s (off %.4f, bound %s)\n", g, w, d, r }'
}

# ---- tdigest ----------------------------------------------------------------
if [ "$(q "SELECT count(*) FROM pg_available_extensions WHERE name = 'tdigest';")" != 1 ]; then
	pgc_skip tdigest "tdigest is not installed for this major"
fi
q "CREATE EXTENSION tdigest;" >/dev/null
check "premise: tdigest is installed in this database" \
	"$(q "SELECT count(*) FROM pg_extension WHERE extname = 'tdigest';")" "1"
check "the tdigest aggregate reads through the columnar scan" \
	"$(q "$P0 EXPLAIN (COSTS OFF) SELECT tdigest_percentile(v, 100, 0.99) FROM ax;" | grep -c 'Custom Scan (PgColumnarScan) on ax')" "1"
TD_C="$(qv "$P0 SELECT tdigest_percentile(v, 100, 0.99) FROM ax;")"
TD_H="$(qv "$P0 SELECT tdigest_percentile(v, 100, 0.99) FROM ax_heap;")"
EXACT99="$(q "SELECT percentile_cont(0.99) WITHIN GROUP (ORDER BY v) FROM ax_heap;")"
check "tdigest over a columnar table gives exactly what it gives over heap" "$TD_C" "$TD_H"
check "premise: and its p99 is within 0.1% of the exact percentile" "$(within "$TD_C" "$EXACT99" 0.001)" "ok"
# The digests are built ONCE, into a heap table, and that table's values are stored.
# A tdigest depends on the order its inputs arrive in, so digests rebuilt from heap
# could differ from these for reasons that are not storage. Copied from one source,
# any difference is the columnar round trip's (#1339).
psql_run "CREATE TABLE ax_td_src USING heap AS SELECT day, tdigest(v, 100) AS t FROM ax GROUP BY day;
	CREATE TABLE ax_td (day int, t tdigest) USING pgcolumnar;
	INSERT INTO ax_td SELECT day, t FROM ax_td_src;"
check "a tdigest stored in a columnar table comes back byte for byte" \
	"$(q "SELECT (SELECT string_agg(t::text, ',' ORDER BY day) FROM ax_td)
	        = (SELECT string_agg(t::text, ',' ORDER BY day) FROM ax_td_src);")" "t"
check "premise: a tdigest stored in a columnar table still answers: the union of daily digests is within 0.1% at p99" \
	"$(within "$(q "SELECT tdigest_percentile(t, 0.99) FROM ax_td;")" "$EXACT99" 0.001)" "ok"

# ---- hll --------------------------------------------------------------------
# Listed for the PG19 skips below; each check also spells its name out, so the
# twins can be graded by name.
HLL_ARMS=("premise: hll is installed in this database"
	"the hll aggregate reads through the columnar scan"
	"hll over a columnar table builds the same sketch, byte for byte, as over heap"
	"premise: and its estimate is within three standard errors (6.9%) of the exact distinct count"
	"an hll stored in a columnar table comes back byte for byte"
	"and the union of the stored sketches counts what one sketch over all rows counts")
if [ "$(q "SELECT count(*) FROM pg_available_extensions WHERE name = 'hll';")" != 1 ]; then
	if [ "$PGC_MAJOR" = 19 ]; then
		for a in "${HLL_ARMS[@]}"; do
			check_skip "$a" "SKIP  $a (hll 2.19 does not build against PostgreSQL 19)" \
				"hll 2.19 does not build against PostgreSQL 19"
		done
		pgc_summary
	fi
	pgc_skip hll "hll is not installed for this major"
fi
q "CREATE EXTENSION hll;" >/dev/null
check "premise: hll is installed in this database" "$(q "SELECT count(*) FROM pg_extension WHERE extname = 'hll';")" "1"
check "the hll aggregate reads through the columnar scan" \
	"$(q "$P0 EXPLAIN (COSTS OFF) SELECT hll_add_agg(hll_hash_integer(dev)) FROM ax;" | grep -c 'Custom Scan (PgColumnarScan) on ax')" "1"
check "hll over a columnar table builds the same sketch, byte for byte, as over heap" \
	"$(qv "$P0 SELECT (SELECT hll_add_agg(hll_hash_integer(dev)) FROM ax)::text = (SELECT hll_add_agg(hll_hash_integer(dev)) FROM ax_heap)::text;")" "t"
check "premise: and its estimate is within three standard errors (6.9%) of the exact distinct count" \
	"$(within "$(qv "$P0 SELECT hll_cardinality(hll_add_agg(hll_hash_integer(dev))) FROM ax;")" 50000 0.069)" "ok"
psql_run "CREATE TABLE ax_hll (day int, s hll) USING pgcolumnar;
	INSERT INTO ax_hll SELECT day, hll_add_agg(hll_hash_integer(dev)) FROM ax GROUP BY day;"
check "an hll stored in a columnar table comes back byte for byte" \
	"$(q "SELECT (SELECT string_agg(s::text, ',' ORDER BY day) FROM ax_hll)
	        = (SELECT string_agg(s::text, ',' ORDER BY day) FROM (SELECT day, hll_add_agg(hll_hash_integer(dev)) s FROM ax_heap GROUP BY day) x);")" "t"
check "and the union of the stored sketches counts what one sketch over all rows counts" \
	"$(q "SELECT (SELECT hll_cardinality(hll_union_agg(s)) FROM ax_hll) = (SELECT hll_cardinality(hll_add_agg(hll_hash_integer(dev))) FROM ax_heap);")" "t"

pgc_summary
