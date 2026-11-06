#!/usr/bin/env bash
#
# pgColumnar: pgcolumnar.enable_bloom_filter must govern the write side too.
#
# The setting was consulted in exactly one place, columnar_reader.c, on the read
# side. The write path gated on the column being hashable and never looked at
# it, so turning the feature off still hashed every value and still stored a
# filter for every chunk -- the user paid the cost and lost only the benefit.
#
# Measured on a 1,000,000-row table of five int columns: 8,320 kB of filters
# written into pgcolumnar.bloom with the setting off. Write time moved 10% on
# that shape and 2-3% on text, so storage is the larger part of it.
#
# The interesting risk in the fix is the other direction: a chunk written with
# no filter must still answer equality correctly, because a reader that treats
# "no filter" as "no match" would skip real rows and return fewer of them --
# silently. So the first thing asserted is that data written with the setting
# off still reads correctly with it on.
#
# Usage:  test/bloom_setting.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

ROWS=${PGC_BLOOM_ROWS:-20000}

# how many bloom filters exist for a relation
filters() {  # table
	q "SELECT count(*) FROM pgcolumnar.bloom f
		JOIN pgcolumnar.storage s ON s.storage_id = f.storage_id
		WHERE s.relation_oid = '$1'::regclass;" | tail -1
}

build() {  # table, setting
	psql_run "DROP TABLE IF EXISTS $1;
		SET pgcolumnar.enable_bloom_filter = $2;
		CREATE TABLE $1 (id int, v text) USING pgcolumnar;" >/dev/null 2>&1
	psql_run "SET pgcolumnar.enable_bloom_filter = $2;
		INSERT INTO $1 SELECT g, 'k' || g FROM generate_series(1, $ROWS) g;" >/dev/null 2>&1
}

# --- 1. the setting governs whether filters are written ------------------------

build bs_on on
check "filters are written when the setting is on" \
	"$(awk -v n="$(filters bs_on)" 'BEGIN { print (n > 0) ? "some" : "none" }')" "some"

build bs_off off
# Read with the rows the table holds: a build that failed, whose stderr is discarded
# above, writes no filters either, and read 0 here.
check "no filters are written when the setting is off" \
	"$(filters bs_off) filters over $(q "SELECT count(*) FROM bs_off;" | tail -1) rows" "0 filters over $ROWS rows"

# --- 2. a chunk with no filter still answers equality --------------------------

# This is the direction that would be a silent wrong answer: skipping a chunk
# because it has no filter returns fewer rows and raises nothing.
# One check per reading, each named literally: a name built from a loop variable is a
# template to compare_to_bash.
probe() {	# $1 = setting, $2 = value -> rows of bs_off holding it
	q "SET pgcolumnar.enable_bloom_filter = $1;
		SELECT count(*) FROM bs_off WHERE v = '$2';" | tail -1
}
check "written off, read with the setting on: present value found" "$(probe on "k$((ROWS / 2))")" "1"
check "written off, read with the setting on: absent value not found" "$(probe on nosuchvalue)" "0"
check "written off, read with the setting off: present value found" "$(probe off "k$((ROWS / 2))")" "1"
check "written off, read with the setting off: absent value not found" "$(probe off nosuchvalue)" "0"

# and the mirror: written with filters, read with skipping off
check "written on, read with the setting off: present value found" \
	"$(q "SET pgcolumnar.enable_bloom_filter = off;
		SELECT count(*) FROM bs_on WHERE v = 'k$((ROWS / 2))';" | tail -1)" "1"

# every row still reachable, not just the probed one
check "a full scan of the unfiltered table is complete" \
	"$(q "SELECT count(*) FROM bs_off;" | tail -1)" "$ROWS"
# with the rows compared: two empty tables have no mismatch either
check "the two tables hold identical data" \
	"$(q "SELECT count(*) FILTER (WHERE a.v IS DISTINCT FROM b.v) || ' of ' || count(*)
		FROM bs_on a FULL JOIN bs_off b USING (id);" | tail -1)" "0 of $ROWS"

# --- 3. projections follow the same decision -----------------------------------

# A projection is written through its own write state. That state is configured
# separately, so it can miss a setting the base relation honours -- which is
# exactly what the first version of this fix did, silently dropping filters from
# projections while the setting was on.
psql_run "DROP TABLE IF EXISTS bs_p;
	SET pgcolumnar.enable_bloom_filter = on;
	CREATE TABLE bs_p (id int, v text) USING pgcolumnar;
	SELECT pgcolumnar.add_projection('bs_p', 'bs_p_proj', ARRAY['v'], ARRAY['v']);" >/dev/null 2>&1
psql_run "SET pgcolumnar.enable_bloom_filter = on;
	INSERT INTO bs_p SELECT g, 'p' || g FROM generate_series(1, $ROWS) g;" >/dev/null 2>&1

# The projection has its own storage id, and the filters counted are that storage's.
# This counted every filter in the database, so bs_on's alone made it read "some":
# with the projection's write state dropping its filters -- the defect described
# above -- main passed it.
check "a projection still gets filters when the setting is on" \
	"$(awk -v n="$(q "SELECT count(*) FROM pgcolumnar.bloom
		WHERE storage_id IN (SELECT proj_storage_id FROM pgcolumnar.projection
		                      WHERE name = 'bs_p_proj');" | tail -1)" \
		'BEGIN { print (n > 0) ? "some" : "none (" n ")" }')" "some"
check "the projected table reads correctly" \
	"$(q "SELECT count(*) FROM bs_p WHERE v = 'p$((ROWS / 2))';" | tail -1)" "1"

pgc_summary
