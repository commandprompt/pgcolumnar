#!/usr/bin/env bash
#
# pgColumnar native zone-map-only aggregates (Phase D5b): an ungrouped, unfiltered
# aggregate over a native-format (PGCN v1) table is answered from the whole-chunk
# zone maps without reading any data pages (native spec 7.1): count(*) from row
# counts, count(col) from value_count, sum/avg(int2,int4) from the zone sum, and
# min/max from the zone min/max. This suite proves the answers equal a heap oracle
# across the supported aggregates, nulls and an empty table; that the metadata path
# is actually taken (EXPLAIN shows the PgColumnarAgg node with no data scan); and
# that a filtered or unsupported aggregate falls back and is still correct.
#
# Usage:  test/native_agg.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# id int4 monotonic no nulls; s int4 with nulls; k bigint; label text. Several row
# groups so the aggregate folds across many whole-chunk zone maps.
GEN="SELECT g,
       CASE WHEN g % 5 = 0 THEN NULL ELSE g % 1000 END,
       (g * 7)::bigint,
       'lbl-' || (g % 40)
  FROM generate_series(1, 10000) g"

psql_run "CREATE TABLE h (id int, s int, k bigint, label text);"
psql_run "CREATE TABLE n (id int, s int, k bigint, label text) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('n', stripe_row_limit => 2048, chunk_group_row_limit => 1024);"
psql_run "INSERT INTO h $GEN;"
psql_run "INSERT INTO n $GEN;"

# EXPLAIN plan text for a query.
plan() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "EXPLAIN (COSTS OFF) $1" 2>/dev/null
}
# The metadata aggregate path reports "Columnar Vectorized Aggregates: N" and has
# no separate Aggregate node above a scan; a fallback plan has an Aggregate node
# and no such line.
has_aggnode() { grep -q 'Columnar Vectorized Aggregates' <<<"$1" && echo yes || echo no; }
# at_least GOT FLOOR -- GOT when it falls short of FLOOR, else FLOOR, so a failing
# arm reports the number it measured (selftest part 540). An empty GOT stays
# empty, which check_num refuses as "not a measurement" rather than reading as 0.
at_least() { [ -z "$1" ] && return; [ "$1" -lt "$2" ] && echo "$1" || echo "$2"; }
has_gather() { grep -q 'Gather' <<<"$1" && echo yes || echo no; }

# Same, with a parallel plan available: workers allowed, no setup charge, and no
# minimum table size, since the suite runs with max_parallel_workers_per_gather=0
# and a fixture of a few pages, so without this a plan-choice check would never
# see the alternative it is about.
#
# parallel_tuple_cost keeps its default on purpose. Zeroing it makes a Gather of
# raw rows look free, which makes that Gather the cheapest scan path, and the
# cost this check exists to catch was inherited from the cheapest scan path. With
# the charge zeroed the wrong costing still picks the right plan and the check
# passes for a reason that has nothing to do with the fix.
plan_par() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "SET max_parallel_workers_per_gather = 4;
		SET parallel_setup_cost = 0;
		SET min_parallel_table_scan_size = 0;
		EXPLAIN (COSTS OFF) $1" 2>/dev/null
}

# The node, before any answer read from it.
#
# Everything below compares a columnar answer to a heap oracle, and a fallback
# plan returns exactly the same answers -- that is what makes it a correct
# fallback. So the parity checks cannot tell the metadata path from the path it
# replaces, and eleven of them passing says nothing about which one ran. Assert
# the node first, so that a silent fall back fails here and names itself rather
# than passing quietly eleven times over.
check "count(*) uses the metadata agg node" \
	"$(has_aggnode "$(plan 'SELECT count(*) FROM n')")" "yes"
check "sum/min/max uses the metadata agg node" \
	"$(has_aggnode "$(plan 'SELECT sum(id), min(id), max(id) FROM n')")" "yes"

# A WHERE that references no column is a gating qual: planned as a one-time
# filter, not part of the per-row quals. The node answered from the zone maps alone
# and ignored it, so a false gate returned every row's aggregates (#1435).
GATED="SELECT count(*) || '/' || coalesce(min(id)::text, '-') || '/' || coalesce(max(id)::text, '-') FROM"
check "premise: count, min and max with no WHERE are the vectorized aggregate" \
	"$(has_aggnode "$(plan 'SELECT count(*), min(id), max(id) FROM n')")" "yes"
check "a WHERE (SELECT false) with no column in it gives heap's answer" \
	"$(q "$GATED n WHERE (SELECT false)")" "$(q "$GATED h WHERE (SELECT false)")"
check "a WHERE on now() with no column in it gives heap's answer" \
	"$(q "$GATED n WHERE now() < '2000-01-01'")" "$(q "$GATED h WHERE now() < '2000-01-01'")"
check "a WHERE on current_user with no column in it gives heap's answer" \
	"$(q "$GATED n WHERE current_user = 'pgc_nobody'")" "$(q "$GATED h WHERE current_user = 'pgc_nobody'")"
check "premise: heap's answer to a false gate is no rows" "$(q "$GATED h WHERE (SELECT false)")" "0/-/-"

# Supported aggregates answered from zone maps must equal the heap oracle.
check "count(*)"          "$(q 'SELECT count(*) FROM n;')"        "$(q 'SELECT count(*) FROM h;')"
check "count(col,nulls)"  "$(q 'SELECT count(s) FROM n;')"       "$(q 'SELECT count(s) FROM h;')"
check "sum(int4)"         "$(q 'SELECT sum(id) FROM n;')"        "$(q 'SELECT sum(id) FROM h;')"
check "sum(int4,nulls)"   "$(q 'SELECT sum(s) FROM n;')"         "$(q 'SELECT sum(s) FROM h;')"
check "avg(int4,nulls)"   "$(q 'SELECT avg(s) FROM n;')"         "$(q 'SELECT avg(s) FROM h;')"
check "min(int4)"         "$(q 'SELECT min(id) FROM n;')"        "$(q 'SELECT min(id) FROM h;')"
check "max(int4)"         "$(q 'SELECT max(id) FROM n;')"        "$(q 'SELECT max(id) FROM h;')"
check "min(int4,nulls)"   "$(q 'SELECT min(s) FROM n;')"         "$(q 'SELECT min(s) FROM h;')"
check "min(text)"         "$(q 'SELECT min(label) FROM n;')"     "$(q 'SELECT min(label) FROM h;')"
check "max(text)"         "$(q 'SELECT max(label) FROM n;')"     "$(q 'SELECT max(label) FROM h;')"
check "multi-agg"         "$(q 'SELECT count(*), sum(id), min(id), max(id) FROM n;')" \
                          "$(q 'SELECT count(*), sum(id), min(id), max(id) FROM h;')"

# The path has to win on cost, not only exist. It reads one metadata entry per
# row group; Gather over Partial Aggregate reads the whole table. Costing this
# path at the scan's cost let the parallel plan take work the catalog already
# has (issue #133), so pin the plan the planner picks once a parallel one is on
# the table.
par_count="$(plan_par 'SELECT count(*) FROM n')"
check "count(*) beats a parallel plan"  "$(has_aggnode "$par_count")" "yes"
check "count(*) plan has no Gather"     "$(has_gather "$par_count")"  "no"
par_multi="$(plan_par 'SELECT count(*), sum(id), min(id), max(id) FROM n')"
check "multi-agg beats a parallel plan" "$(has_aggnode "$par_multi")" "yes"
check "multi-agg plan has no Gather"    "$(has_gather "$par_multi")"  "no"

# count(*) is answered from each row group's stored row count, so it has no
# reason to read zone maps at all. Reading them costs one catalog lookup per row
# group returning an entry for every column, and on a 6,000,000-row table that
# was the entire cost of the query (issue #133). Count the scans of
# pgcolumnar.zone_map across the statement: each q is its own backend, and a
# backend flushes its statistics when it exits, so the counter is settled by the
# time the next one reads it.
zm_scans() {
	q "SELECT coalesce(idx_scan, 0) + coalesce(seq_scan, 0)
	     FROM pg_stat_all_tables WHERE relid = 'pgcolumnar.zone_map'::regclass;"
}
zm_before="$(zm_scans)"; q 'SELECT count(*) FROM n;' >/dev/null; zm_after="$(zm_scans)"
check "count(*) reads no zone maps" "$((zm_after - zm_before))" "0"

# The same measurement the other way round, so the check above cannot pass just
# because the counter never moves.
zm_before="$(zm_scans)"; q 'SELECT count(*), sum(id) FROM n;' >/dev/null; zm_after="$(zm_scans)"
check "an aggregate that needs zone maps reads them" \
	"$([ "$((zm_after - zm_before))" -gt 0 ] && echo yes || echo no)" "yes"

# A filtered aggregate falls back (no zone-map answer) but is still correct.
check "filtered aggregate parity" \
	"$(q 'SELECT count(*), sum(id) FROM n WHERE id > 5000;')" \
	"$(q 'SELECT count(*), sum(id) FROM h WHERE id > 5000;')"
check "filtered aggregate not metadata node" \
	"$(has_aggnode "$(plan 'SELECT count(*), sum(id) FROM n WHERE id > 5000')")" "no"

# An unsupported aggregate (sum on bigint) falls back and is still correct.
check "sum(bigint) fallback parity" "$(q 'SELECT sum(k) FROM n;')" "$(q 'SELECT sum(k) FROM h;')"

# Empty native table: count 0, sum/min/max NULL, exactly as SQL.
psql_run "CREATE TABLE e (id int, label text) USING pgcolumnar;"
check "empty count(*)"  "$(q 'SELECT count(*) FROM e;')"                 "0"
check "empty sum NULL"  "$(q 'SELECT sum(id) IS NULL FROM e;')"          "t"
check "empty min NULL"  "$(q 'SELECT min(label) IS NULL FROM e;')"       "t"

# ---- min/max over timestamptz read no data, and stay right after deletes (#1293)
#
# "What is the latest reading?" -- max(ts) -- is the time-series question, and
# the arms above cover min/max only on int4 and text. They also prove the
# metadata node is CHOSEN and that the answer matches heap; neither shows the
# data was not read. A build that kept the node and decoded every group anyway
# would return the same answers and pass them all.
#
# So these arms read the TABLE's own blocks, from pg_statio, flushed by the
# querying session, against a control that must decode the same column.
psql_run "CREATE TABLE tz (dev int, ts timestamptz) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('tz', stripe_row_limit => 1000);"
# THE HEAP IS WRITTEN FROM THE GENERATOR, NOT COPIED (#1339): a CTAS from tz put a
# write path's consistent error on both sides of every parity arm.
_tzgen="SELECT g % 50, '2026-03-01'::timestamptz + ((g::bigint * 7919) % 400000) * interval '1 min'
          FROM generate_series(1, 400000) g"
psql_run "INSERT INTO tz $_tzgen;"
psql_run "CREATE TABLE tzh (dev int, ts timestamptz) USING heap;"
psql_run "INSERT INTO tzh $_tzgen;"
# THE ROW COUNT FIRST. The first draft computed g * 7919 in int, which overflows
# past g = 271,000; the INSERT failed, psql_run reported nothing, and every arm
# below measured an empty table -- the decode "control" read 3 blocks.
check_num "premise: the timestamptz table holds every row inserted" \
	"$(q 'SELECT count(*) FROM tz;')" "400000"

tz_blocks() {	# tz_blocks QUERY -> blocks of tz read while QUERY ran, in its own session
	q "SELECT pg_stat_reset();" >/dev/null
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "$1" -c "SELECT pg_stat_force_next_flush()" >/dev/null 2>&1
	q "SELECT coalesce(heap_blks_read, 0) + coalesce(heap_blks_hit, 0)
	   FROM pg_statio_user_tables WHERE relname = 'tz';"
}
tz_decode="$(tz_blocks "SELECT max(ts + interval '0 s') FROM tz")"
tz_max="$(tz_blocks "SELECT max(ts) FROM tz")"
tz_min="$(tz_blocks "SELECT min(ts) FROM tz")"
echo "-- tz blocks read: decode=$tz_decode max=$tz_max min=$tz_min"

check "max(ts) over timestamptz plans the metadata agg node" \
	"$(has_aggnode "$(plan 'SELECT max(ts) FROM tz')")" "yes"
# THE CONTROL. A decode of ts has to show up, or every comparison below is
# against an instrument that cannot see a read.
check_num "control: decoding ts reads the table's blocks" \
	"$(at_least "$tz_decode" 10)" "10"
# A tenth, not zero: the metapage and its neighbours are read either way (15
# blocks against 6,603 on a 10M-row table, measured on #1293). The margin is
# reported, not collapsed to a yes or no.
# The margin is decode - 10 * reading: positive passes, and a failure reports by
# how much it fell short.
check_num "max(ts) reads under a tenth of the blocks a decode of ts reads" \
	"$( [ -n "$tz_max" ] && at_least $((tz_decode - 10 * tz_max)) 1 )" "1"
check_num "min(ts) reads under a tenth of the blocks a decode of ts reads" \
	"$( [ -n "$tz_min" ] && at_least $((tz_decode - 10 * tz_min)) 1 )" "1"

check "max(timestamptz) parity" "$(q 'SELECT max(ts) FROM tz;')" "$(q 'SELECT max(ts) FROM tzh;')"
check "min(timestamptz) parity" "$(q 'SELECT min(ts) FROM tz;')" "$(q 'SELECT min(ts) FROM tzh;')"

# The newest and oldest readings deleted: a zone map still records them, so a
# group with deletes must not be folded from it (#149).
psql_run "DELETE FROM tz  WHERE ts > (SELECT max(ts) FROM tzh) - interval '30 min' OR ts < (SELECT min(ts) FROM tzh) + interval '30 min';"
psql_run "DELETE FROM tzh WHERE ts > (SELECT max(ts) FROM tzh) - interval '30 min' OR ts < (SELECT min(ts) FROM tzh) + interval '30 min';"
check "the deletes removed rows at both ends, the same rows from each" \
	"$(q 'SELECT count(*) < 400000 FROM tz;')|$(q 'SELECT count(*) FROM tz;')" \
	"t|$(q 'SELECT count(*) FROM tzh;')"
check "max(timestamptz) parity after deleting the newest rows" "$(q 'SELECT max(ts) FROM tz;')" "$(q 'SELECT max(ts) FROM tzh;')"
check "min(timestamptz) parity after deleting the oldest rows" "$(q 'SELECT min(ts) FROM tz;')" "$(q 'SELECT min(ts) FROM tzh;')"

pgc_summary
