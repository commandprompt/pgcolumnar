#!/usr/bin/env bash
#
# pgColumnar deferred column decode on the index fetch path (issue #157).
#
# An index fetch used to reconstruct every column of the row before returning it,
# because a virtual slot holds values and nothing else. On a wide table that is
# not merely wasteful: the decoded row group exceeds the fetch cache's size cap,
# the entry is dropped after every fetch, and each row re-reads and re-decodes the
# whole group. 2,000 fetches of one column from a 41-column table took minutes.
#
# The executor never says which columns it will read, but it does ask, through
# slot_getsomeattrs, for the smallest prefix it needs. So the slot now carries the
# row's address and decodes when asked.
#
# The risk is entirely correctness, and it is the quiet kind: a slot that decodes
# too few columns hands back a stale or empty value with nothing raised. So the
# timing check is last and everything before it is differential against a heap
# table holding the same rows.
#
# Four things are asserted.
#
# 1. Values are right through an index scan, for every column position and for
#    the shapes that decide how much gets decoded: the first column, the last,
#    one in the middle, all of them, and none of them (count(*)).
#
# 2. Values are right when the row has never been flushed, which takes the
#    buffered path instead and is stored eagerly.
#
# 3. The paths that need a whole row still get one: materialising, sorting,
#    copying into another table, and forming a heap tuple.
#
# 4. The cliff is gone, as a ratio between column counts on the same machine.
#
# Usage:  test/native_lazy_slot.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

ROWS=${PGC_LAZY_ROWS:-20000}
NCOLS=${PGC_LAZY_COLS:-40}

cols=""; sel=""
for i in $(seq 1 "$NCOLS"); do
	cols="$cols, c$i bigint"
	sel="$sel, g * $i"
done

psql_run "DROP TABLE IF EXISTS lz_c; DROP TABLE IF EXISTS lz_h;
	CREATE TABLE lz_c (id int$cols, txt text) USING pgcolumnar;
	INSERT INTO lz_c SELECT g$sel, 'v' || g FROM generate_series(1, $ROWS) g;
	CREATE INDEX lz_c_id ON lz_c (id);
	CREATE TABLE lz_h (LIKE lz_c);
	INSERT INTO lz_h SELECT g$sel, 'v' || g FROM generate_series(1, $ROWS) g;
	CREATE INDEX lz_h_id ON lz_h (id);" >/dev/null

# THE HEAP MIRROR IS WRITTEN FROM THE GENERATOR, NOT COPIED FROM lz_c. A copy reads
# lz_c through the reader under test, so a value the writer stored wrong was wrong on
# both sides: with the writer storing c40 off by one, every check passed (#1341).

# qv SIDE SQL -> the statement's last output line, or "SIDE_ERROR (first line)" when
# psql fails or prints nothing. The side is in the failure, so two failed reads are two
# different values: with the server stopped after setup, five comparisons and the
# ratio passed on two empty strings.
qv() {
	local out
	if out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -v ON_ERROR_STOP=1 -c "$2" 2>&1)" && [ -n "$out" ]; then
		printf '%s\n' "$out" | tail -1
	else
		printf '%s_ERROR (%s)\n' "$1" "$(printf '%s' "$out" | head -1)"
	fi
}

# force the index path, and keep the custom scan out of the way so the fetch
# really is index_fetch_tuple
FORCE="SET enable_seqscan = off; SET enable_bitmapscan = off;
	SET pgcolumnar.enable_custom_scan = off; SET max_parallel_workers_per_gather = 0;"

check "the fetch really is an index scan" \
	"$(q "$FORCE EXPLAIN (COSTS off) SELECT c1 FROM lz_c WHERE id = 5;" \
		| grep -c 'Index Scan')" "1"

# --- 1. every column position, through the index ------------------------------

# Each of these decodes a different prefix: c1 is the shortest, txt the longest,
# and count(*) asks for nothing at all.
bad=""
for expr in "c1" "c2" "c20" "c${NCOLS}" "txt" "c1 + c${NCOLS}" "id"; do
	cv="$(qv COL "$FORCE SELECT $expr FROM lz_c WHERE id = 7777;")"
	hv="$(qv HEAP "$FORCE SELECT $expr FROM lz_h WHERE id = 7777;")"
	[ "$cv" = "$hv" ] || bad="$bad [$expr: $cv vs $hv]"
done
check "single-row index fetches match heap for every column position" \
	"${bad:-same}" "same"

# a whole-row fetch, which needs the full decode
check_text "SELECT * through the index matches heap" \
	"$(qv COL "$FORCE SELECT md5(lz_c::text) FROM lz_c WHERE id = 999;")" \
	"$(qv HEAP "$FORCE SELECT md5(lz_h::text) FROM lz_h WHERE id = 999;")"

# many rows, so the cache is reused across fetches within one statement
check_text "a range of rows sums the same as heap" \
	"$(qv COL "$FORCE SELECT sum(c1) + sum(c${NCOLS}) FROM lz_c WHERE id <= 2000;")" \
	"$(qv HEAP "$FORCE SELECT sum(c1) + sum(c${NCOLS}) FROM lz_h WHERE id <= 2000;")"

# count(*) reads no column at all
check_text "count(*) through the index matches heap" \
	"$(qv COL "$FORCE SELECT count(*) FROM lz_c WHERE id <= 2000;")" \
	"$(qv HEAP "$FORCE SELECT count(*) FROM lz_h WHERE id <= 2000;")"

# --- 2. rows that are still buffered ------------------------------------------

# A row written in this transaction and not yet flushed is not in a row group, so
# the fetch takes the buffered reader and stores eagerly. Same statement, so the
# write is still pending.
check "a row fetched before it is flushed reads correctly" \
	"$(q "$FORCE BEGIN;
		INSERT INTO lz_c (id, c1, txt) VALUES (999999, 4242, 'buffered');
		SELECT c1 || '|' || txt FROM lz_c WHERE id = 999999;
		ROLLBACK;" | grep '|' | tail -1)" \
	"4242|buffered"

# --- 3. the paths that need the whole row -------------------------------------

# Each of these goes through a different callback: materialize, copy_heap_tuple
# via a tuplestore, copyslot, and copy_minimal_tuple via a sort.
check_text "an ORDER BY over index-fetched rows matches heap" \
	"$(qv COL "$FORCE SELECT string_agg(c1::text, ',' ORDER BY c1)
		FROM (SELECT c1 FROM lz_c WHERE id <= 20) s;")" \
	"$(qv HEAP "$FORCE SELECT string_agg(c1::text, ',' ORDER BY c1)
		FROM (SELECT c1 FROM lz_h WHERE id <= 20) s;")"

psql_run "DROP TABLE IF EXISTS lz_copy;" >/dev/null
check "copying index-fetched rows into a heap table keeps every column" \
	"$(q "$FORCE CREATE TABLE lz_copy AS
		SELECT * FROM lz_c WHERE id <= 500;
		SELECT count(*) FROM lz_copy c JOIN lz_h h USING (id)
		WHERE c.c1 IS DISTINCT FROM h.c1
		   OR c.c${NCOLS} IS DISTINCT FROM h.c${NCOLS}
		   OR c.txt IS DISTINCT FROM h.txt;" | tail -1)" \
	"0"

# a unique check fetches the existing row through the index while holding a
# buffer lock, which is the most constrained caller of this path
psql_run "DROP TABLE IF EXISTS lz_u;
	CREATE TABLE lz_u (id int, v bigint) USING pgcolumnar;
	INSERT INTO lz_u SELECT g, g FROM generate_series(1, 1000) g;
	CREATE UNIQUE INDEX lz_u_id ON lz_u (id);" >/dev/null
dup="$(psql_run "INSERT INTO lz_u VALUES (500, 500);" 2>&1 || true)"
check "a unique violation is still detected through the fetch path" \
	"$(case "$dup" in *"duplicate key"*) echo yes ;; *) echo "no ($dup)" ;; esac)" "yes"

# --- 4. the cliff ------------------------------------------------------------

# Ratio between a narrow and a wide table for the same fetches on the same
# machine.
#
# The fixture has to be big enough for the defect to exist. The cliff is the
# decoded row group exceeding COLUMNAR_FETCH_CACHE_MAX_BYTES (32 MB), and a
# 41-column bigint table of 20,000 rows decodes to about 6 MB -- comfortably
# under, no cliff, and a timing check over it passes with the old eager fetch
# too. Forty text columns of forty characters cross the cap at a row count the
# suite can afford, which is why the wide fixture is text and the narrow one
# matches it column for column.
CLIFF_ROWS=${PGC_LAZY_CLIFF_ROWS:-30000}

build_w() {  # table, ncols
	local c="" s2="" i
	for i in $(seq 1 $2); do
		c="$c, c$i text"
		s2="$s2, repeat('x', 40)"
	done
	psql_run "DROP TABLE IF EXISTS $1;
		CREATE TABLE $1 (id int$c) USING pgcolumnar;
		INSERT INTO $1 SELECT g$s2 FROM generate_series(1, $CLIFF_ROWS) g;
		CREATE INDEX ${1}_id ON $1 (id);" >/dev/null
}
ms_of() {  # table -> milliseconds, or "no reading" when the statement failed: a
	# statement that errors at once is fast, and two of them used to pass the ratio
	local t0 t1
	t0=$(date +%s%N)
	if ! env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-At -v ON_ERROR_STOP=1 -c "$FORCE SELECT count(c1) FROM $1 WHERE id <= 2000;" >/dev/null 2>&1; then
		echo "no reading"; return
	fi
	t1=$(date +%s%N)
	echo $(( (t1 - t0) / 1000000 ))
}
build_w lz_n 2
build_w lz_w "$NCOLS"
narrow="$(ms_of lz_n)"
wide="$(ms_of lz_w)"
echo "-- 2000 index fetches of one column: 3 cols ${narrow} ms, $((NCOLS + 1)) cols ${wide} ms"

check "a wide table is not in a different class from a narrow one" \
	"$(awk -v n="$narrow" -v w="$wide" \
		'BEGIN { if (n !~ /^[0-9]+$/ || w !~ /^[0-9]+$/) print "no reading (" n " / " w ")";
		         else print (n > 0 && w / n < 25) ? "yes" : "no (" n "ms vs " w "ms)" }')" \
	"yes"

pgc_summary
