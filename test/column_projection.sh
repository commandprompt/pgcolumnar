#!/usr/bin/env bash
# Column projection (#338): the reader must read and decode only the columns a
# query references.
#
# Two things have to hold, and they pull against each other, so both are checked
# rather than one being inferred from the other:
#
#   1. It is faithful. Skipping a column must not change a single result. The
#      oracle is the same query with pgcolumnar.enable_column_projection off,
#      which restores the old read-everything behavior, plus a heap table
#      holding identical data.
#   2. It actually skips. Correctness alone is satisfied by doing nothing, so
#      the win is asserted directly as a buffer count. Buffers, not timings:
#      exact, reproducible on a shared runner, and they fail loudly if the
#      projection ever silently stops applying.
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

GUC=pgcolumnar.enable_column_projection
NOPAR="SET max_parallel_workers_per_gather=0"

# scalar value of a query at a given setting of the projection GUC
val() {  # val <on|off> <sql>
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -Atq -c "$NOPAR" -c "SET $GUC=$1" -c "$2" 2>&1
}

# Buffers touched by the scan, at a given setting.
bufs() {  # bufs <on|off> <sql>
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -Atq -c "$NOPAR" -c "SET $GUC=$1" \
		-c "EXPLAIN (ANALYZE, BUFFERS) $2" 2>&1 |
		grep -m1 -oE 'Buffers: shared[^)]*' |
		grep -oE '(hit|read)=[0-9]+' | cut -d= -f2 | paste -sd+ | bc
}

# The core oracle: projection on must equal projection off, byte for byte.
ab() {  # ab <label> <sql>
	local on off
	on="$(val on "$2")"
	off="$(val off "$2")"
	check "$1 :: on==off" "$on" "$off"
}

# And equal the heap, so "both arms wrong the same way" cannot pass. The query
# is given with a %s where the table name goes.
oracle() {  # oracle <label> <sql-with-%s>
	local c h
	c="$(val on "$(printf "$2" t)")"
	h="$(val on "$(printf "$2" h)")"
	check "$1 :: columnar==heap" "$c" "$h"
}

# ---------------------------------------------------------------------------
# Fixture: wide enough that skipping is measurable, with every shape that
# interacts with an unmaterialised column.
#   - k        filter column, never selected  (qual-only projection)
#   - v,w      by-value aggregate inputs
#   - txt      varlena (decoded via the by-reference path, not fetch_att)
#   - alln     entirely NULL
#   - pad1..8  the bulk that projection should avoid reading. Random, so it is
#              incompressible: an arithmetic sequence encodes down to almost
#              nothing, which would leave the test asserting a saving that the
#              encoding had already made and projection had not.
# ---------------------------------------------------------------------------
psql_run "DROP TABLE IF EXISTS t; DROP TABLE IF EXISTS h;
          CREATE TABLE t (id int, k int, v float8, w bigint, txt text, alln int,
                          pad1 float8, pad2 float8, pad3 float8, pad4 float8,
                          pad5 float8, pad6 float8, pad7 float8, pad8 float8)
              USING pgcolumnar;
          SELECT pgcolumnar.set_options('t'::regclass, stripe_row_limit => 20000);
          INSERT INTO t
          SELECT g, g % 1000,
                 CASE WHEN g % 50 = 0 THEN NULL ELSE (g % 13)::float8 * 1.5 END,
                 g::bigint * 7,
                 CASE WHEN g % 31 = 0 THEN NULL ELSE 'row' || g END,
                 NULL,
                 random(), random(), random(), random(),
                 random(), random(), random(), random()
          FROM generate_series(1, 300000) g;
          CREATE TABLE h (LIKE t);
          INSERT INTO h SELECT * FROM t;" >/dev/null

check "premise: columnar and heap hold the same rows" \
	"$(val on "SELECT count(*) FROM t")" "$(val on "SELECT count(*) FROM h")"

# ---- 1. it actually skips ---------------------------------------------------
# The comparison is against the same query with projection off, not against
# pg_total_relation_size: a buffer count includes the catalog reads for row
# group, chunk, and zone map metadata, which projection does not remove and
# which would make a "fraction of the relation" threshold measure the wrong
# thing.
B_ON="$(bufs on  "SELECT sum(v) FROM t")"
B_OFF="$(bufs off "SELECT sum(v) FROM t")"
check "premise: the unprojected read is large enough to measure (off: $B_OFF)" \
	"$([ "$B_OFF" -gt 1000 ] && echo yes || echo no)" yes
# One float8 column out of fourteen. A third is far above the ideal ~1/14 and
# far below the 1.0 a broken projection would give, so it fails on a regression
# without tracking encoding-ratio drift.
check "one column costs less than a third of reading all of them (on: $B_ON, off: $B_OFF)" \
	"$([ "$B_ON" -lt $((B_OFF / 3)) ] && echo yes || echo no)" yes

# A qual-only column must be read (it is filtered on) even though it is never
# emitted -- if it were skipped the filter would silently match nothing.
check "qual-only column still produces the right count" \
	"$(val on "SELECT count(*) FROM t WHERE k = 7")" \
	"$(val on "SELECT count(*) FROM h WHERE k = 7")"

# Selecting everything must not get cheaper: there is nothing to skip. The
# OFFSET 0 stops the planner from collapsing the subquery and reaching the
# count(*) metadata path, which reads no columns at all and would make this
# check pass for the wrong reason.
B_ALL="$(bufs on "SELECT count(*) FROM (SELECT t.* FROM t OFFSET 0) s")"
check "SELECT * reads as much as projection-off does ($B_ALL vs $B_OFF)" \
	"$([ "$B_ALL" -gt $((B_OFF * 8 / 10)) ] && echo yes || echo no)" yes

# ---- 2. it is faithful ------------------------------------------------------
ab "single column agg"         "SELECT sum(v) FROM t"
ab "two columns"               "SELECT sum(v), sum(w) FROM t"
ab "qual column not selected"  "SELECT count(*) FROM t WHERE k < 500"
ab "qual and target disjoint"  "SELECT sum(v) FROM t WHERE k < 500"
ab "varlena projected alone"   "SELECT md5(string_agg(txt, '|' ORDER BY id)) FROM t WHERE id <= 5000"
ab "all-NULL column"           "SELECT count(alln), count(*) FROM t"
ab "count(*)"                  "SELECT count(*) FROM t"
ab "select star hash"          "SELECT md5(string_agg(x.*::text, '|' ORDER BY x.id)) FROM (SELECT * FROM t WHERE id <= 3000) x"
ab "whole-row var"             "SELECT md5(string_agg(t::text, '|' ORDER BY id)) FROM t WHERE id <= 3000"
ab "ctid system column"        "SELECT count(DISTINCT ctid) FROM t WHERE id <= 3000"
ab "order by unselected col"   "SELECT string_agg(id::text, ',') FROM (SELECT id FROM t WHERE id <= 200 ORDER BY k, id) s"
ab "min/max mixed types"       "SELECT min(v), max(w), min(txt), max(id) FROM t"
ab "join on projected column"  "SELECT count(*) FROM t a JOIN t b USING (id) WHERE a.id <= 2000"

oracle "single column agg"     "SELECT sum(v) FROM %s"
oracle "qual and target disjoint" "SELECT sum(v) FROM %s WHERE k < 500"
oracle "varlena alone"         "SELECT md5(string_agg(txt, '|' ORDER BY id)) FROM %s WHERE id <= 5000"
oracle "select star hash"      "SELECT md5(string_agg(x.*::text, '|' ORDER BY x.id)) FROM (SELECT * FROM %s WHERE id <= 3000) x"
oracle "all-NULL column"       "SELECT count(alln), count(*) FROM %s"
oracle "min/max mixed types"   "SELECT min(v), max(w), min(txt), max(id) FROM %s"

# ---- 3. the ADD COLUMN interaction -----------------------------------------
# The dangerous case. An unmaterialised column and a column that predates an
# ADD COLUMN both leave the validity pointer NULL, but they mean different
# things: the latter must yield the column's default, the former must never be
# read at all. If the two were conflated, selecting the added column alone would
# hand back its default for rows that have a real stored value.
psql_run "ALTER TABLE t ADD COLUMN added int DEFAULT 42;
          ALTER TABLE h ADD COLUMN added int DEFAULT 42;" >/dev/null
ab     "added column alone"        "SELECT sum(added), count(added) FROM t"
oracle "added column alone"        "SELECT sum(added), count(added) FROM %s"
oracle "added column with others"  "SELECT sum(added), sum(v) FROM %s"

# Rows written after the ADD COLUMN carry a real chunk for it; rows written
# before do not. Both must be right when the column is projected by itself.
psql_run "INSERT INTO t SELECT g, g % 1000, 1.0, g, 'new'||g, NULL,
                 1,2,3,4,5,6,7,8, 99
          FROM generate_series(200001, 210000) g;
          INSERT INTO h SELECT g, g % 1000, 1.0, g, 'new'||g, NULL,
                 1,2,3,4,5,6,7,8, 99
          FROM generate_series(200001, 210000) g;" >/dev/null
oracle "added column across the boundary" "SELECT sum(added), count(added), min(added), max(added) FROM %s"
ab     "added column across the boundary" "SELECT sum(added), count(added), min(added), max(added) FROM t"

# ---- 4. deletes and updates ------------------------------------------------
psql_run "DELETE FROM t WHERE id % 97 = 0; DELETE FROM h WHERE id % 97 = 0;" >/dev/null
oracle "after deletes, projected column"  "SELECT sum(v), count(*) FROM %s"
ab     "after deletes, projected column"  "SELECT sum(v), count(*) FROM t"

psql_run "UPDATE t SET v = v + 1 WHERE id % 500 = 0;
          UPDATE h SET v = v + 1 WHERE id % 500 = 0;" >/dev/null
oracle "after updates, projected column"  "SELECT sum(v), count(*) FROM %s"
oracle "after updates, select star hash"  "SELECT md5(string_agg(x.*::text, '|' ORDER BY x.id)) FROM (SELECT * FROM %s WHERE id <= 3000) x"

# ---- 5. parallel scan ------------------------------------------------------
# Projection state is per-worker; a worker reading the wrong ranges would show
# up as a wrong total rather than a crash.
par() {  # par <sql>
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -Atq \
		-c "SET max_parallel_workers_per_gather=4" \
		-c "SET parallel_setup_cost=0" -c "SET parallel_tuple_cost=0" \
		-c "SET min_parallel_table_scan_size=0" \
		-c "SET $GUC=on" -c "$1" 2>&1
}
check "parallel scan, projected column" \
	"$(par "SELECT count(*), sum(w) FROM t")" \
	"$(val on "SELECT count(*), sum(w) FROM h")"
check "parallel scan, qual on unselected column" \
	"$(par "SELECT sum(v) FROM t WHERE k < 500")" \
	"$(val on "SELECT sum(v) FROM h WHERE k < 500")"

# ---- index build: the projection reaches a path the custom scan cannot (#413) ----
#
# Everything above tests the scan path, where the custom scan node computes the
# projection from the plan. CREATE INDEX does not go through it: the table-AM
# callback opens its own reader, and the reader had no projection, so building an
# index on one column of a wide table decoded all of them. The callback is told
# which columns it needs (IndexInfo), so it can say so.
#
# The metric here is a RATIO rather than the buffer counts used above, and the
# reason is worth stating: EXPLAIN does not cover CREATE INDEX, and
# pg_statio_user_tables counts the heap fork, which is nearly empty for a columnar
# table, so neither exact source is available for this operation.
#
# What is asserted instead is the property itself, which a timing threshold would
# not be: building the SAME single-column index on a narrow and a wide table with
# identical row counts must cost about the same. Two operations, same machine,
# same run, so the ratio does not depend on how fast the box is.
# Measured: 1.0x and 1.2x with projection on, 4.4x and 5.5x with it off.
IDXROWS=${PGC_PROJ_IDX_ROWS:-200000}
wcols=$(for i in $(seq 1 19); do printf ", a%d text" $i; done)
wvals=$(for i in $(seq 1 19); do printf ", repeat(chr(48+%d),80)" $i; done)
psql_run "DROP TABLE IF EXISTS pnarrow; DROP TABLE IF EXISTS pwide;
	CREATE TABLE pnarrow (k int, a1 text) USING pgcolumnar;
	INSERT INTO pnarrow SELECT g, repeat('1',80) FROM generate_series(1,$IDXROWS) g;
	CREATE TABLE pwide (k int$wcols) USING pgcolumnar;
	INSERT INTO pwide SELECT g$wvals FROM generate_series(1,$IDXROWS) g;
	CREATE TABLE pwide_h (k int$wcols);
	INSERT INTO pwide_h SELECT * FROM pwide;" >/dev/null

idx_ms() {  # projection-setting, table, index-name
	local s e
	psql_run "DROP INDEX IF EXISTS $3;" >/dev/null 2>&1
	s=$(date +%s%N)
	psql_run "SET $GUC=$1; CREATE INDEX $3 ON $2 (k);" >/dev/null 2>&1
	e=$(date +%s%N); echo $(( (e - s) / 1000000 ))
}
on_n=$(idx_ms on pnarrow pn_k);  on_w=$(idx_ms on pwide pw_k)
off_n=$(idx_ms off pnarrow pn_k); off_w=$(idx_ms off pwide pw_k)
echo "-- #413 index build: projection on ${on_n}/${on_w} ms, off ${off_n}/${off_w} ms (narrow/wide)"

check_timing "an index build does not scale with columns it does not reference (#413)" \
	"$(awk -v a="$on_w" -v b="$on_n" 'BEGIN { print (b > 0 && a / b < 2.5) ? "yes" : "no" }')" \
	"yes"
# and the control: with projection off it DOES scale, so the check above is not
# passing because the fixture is too small to tell the two apart
check_timing "and with projection off it does scale, so that check discriminates (#413)" \
	"$(awk -v a="$off_w" -v b="$off_n" 'BEGIN { print (b > 0 && a / b > 2.5) ? "yes" : "no" }')" \
	"yes"

# ---- and the index must still be right, which matters more than the speed ------
psql_run "CREATE INDEX pw_e ON pwide ((a1 || a2));
	CREATE INDEX pwh_e ON pwide_h ((a1 || a2));
	CREATE INDEX pw_p ON pwide (k) WHERE a19 > CHR(48);
	CREATE INDEX pwh_p ON pwide_h (k) WHERE a19 > CHR(48);
	CREATE INDEX pwh_k ON pwide_h (k);
	ANALYZE pwide; ANALYZE pwide_h;" >/dev/null
check "plain index over a projected build matches heap (#413)" \
	"$(val on "SET enable_seqscan=off; SET enable_bitmapscan=off; SELECT count(*) FROM pwide WHERE k BETWEEN 100 AND 5000")" \
	"$(val on "SET enable_seqscan=off; SET enable_bitmapscan=off; SELECT count(*) FROM pwide_h WHERE k BETWEEN 100 AND 5000")"
check "expression index over a projected build matches heap (#413)" \
	"$(val on "SET enable_seqscan=off; SET enable_bitmapscan=off; SELECT count(*) FROM pwide WHERE (a1 || a2) = repeat('1',80)||repeat('2',80)")" \
	"$(val on "SET enable_seqscan=off; SET enable_bitmapscan=off; SELECT count(*) FROM pwide_h WHERE (a1 || a2) = repeat('1',80)||repeat('2',80)")"
check "partial index over a projected build matches heap (#413)" \
	"$(val on "SET enable_seqscan=off; SET enable_bitmapscan=off; SELECT count(*) FROM pwide WHERE k < 900 AND a19 > CHR(48)")" \
	"$(val on "SET enable_seqscan=off; SET enable_bitmapscan=off; SELECT count(*) FROM pwide_h WHERE k < 900 AND a19 > CHR(48)")"

pgc_summary
