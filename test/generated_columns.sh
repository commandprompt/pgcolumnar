#!/usr/bin/env bash
#
# pgColumnar generated-column coverage.
#
# A columnar table with a generated column must return the same values a heap
# table does. STORED generated columns are materialized and exist on all
# supported majors; VIRTUAL generated columns compute at read time and are the
# default in PostgreSQL 18+. This suite loads a heap/columnar pair for each and
# asserts, via the differential oracle, that the generated values match. For a
# VIRTUAL column it additionally asserts pgColumnar stores no chunk for that
# attribute (nothing is written for a read-time-computed column).
#
# Usage:  test/generated_columns.sh [PG_CONFIG]
#
# Written fresh for pgColumnar; it does not reuse any upstream test file.

set -uo pipefail

. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# The columnar side's row count rides with its hash, against the count the
# fixture loads, so two empty tables cannot hash-match as EMPTY == EMPTY.
counted_hash() { echo "$(q "SELECT count(*) FROM ($1) _c"):$(pgc_set_hash "$1")"; }

# --- STORED generated column (all supported majors) ------------------------
echo "-- stored generated column"
psql_run "CREATE TABLE gs_heap (a int, b bigint GENERATED ALWAYS AS (a * 2) STORED,
                                c text GENERATED ALWAYS AS ('r' || a) STORED) USING heap;"
psql_run "CREATE TABLE gs_col  (a int, b bigint GENERATED ALWAYS AS (a * 2) STORED,
                                c text GENERATED ALWAYS AS ('r' || a) STORED) USING pgcolumnar;"
psql_run "INSERT INTO gs_heap (a) SELECT g FROM generate_series(1, 5000) g;"
psql_run "INSERT INTO gs_col  (a) SELECT g FROM generate_series(1, 5000) g;"

# sanity: both tables loaded (guards against an empty/unreadable cluster making
# the differential checks below trivially "match" on two empty results)
check "stored generated: row count" "$(q "SELECT count(*) FROM gs_col;")" "5000"

hs_h="$(pgc_set_hash "SELECT a, b, c FROM gs_heap")"
hs_c="$(counted_hash "SELECT a, b, c FROM gs_col")"
check_text "stored generated: values match" "$hs_c" "5000:$hs_h"

# a filter and an aggregate over the generated column exercise the scan path
fs_h="$(pgc_set_hash "SELECT a, b FROM gs_heap WHERE b > 8000 AND b < 9000")"
fs_c="$(counted_hash "SELECT a, b FROM gs_col  WHERE b > 8000 AND b < 9000")"
check_text "stored generated: filtered scan matches" "$fs_c" "499:$fs_h"

as_h="$(q "SELECT sum(b), count(c) FROM gs_heap")"
as_c="$(q "SELECT sum(b), count(c) FROM gs_col")"
# check_text: two failed readings are both empty, and "" == "" would pass. And the
# oracle must hold the fixture's answer, or two empty tables agree as "|0".
[ "$as_h" = "25005000|5000" ] || as_h="the heap oracle does not hold the fixture: [$as_h]"
check_text "stored generated: aggregate matches" "$as_c" "$as_h"

# a STORED column is materialized, so a column chunk is written for it: the
# 0-based column_index 1 (attribute 2).
sc="$(q "SELECT count(*) FROM pgcolumnar.column_chunk
         WHERE storage_id = pgcolumnar.get_storage_id('gs_col') AND column_index = 1;")"
check "stored generated: chunk present for attr 2" "$([ "$sc" -gt 0 ] && echo yes)" "yes"

# --- VIRTUAL generated column (PostgreSQL 18+) -----------------------------
# The major is asserted first, so an unreadable version is not mistaken for an
# old one and the arm skipped as unsupported. Before PG18 the arm is recorded
# as a SKIP, not printed and left out of the accounting.
if ! pgc_is_number "${PGC_MAJOR:-}"; then
	pgc_fail "could not read the server major, so the virtual arm cannot be gated" \
		"got [${PGC_MAJOR:-<none>}]"
elif [ "$PGC_MAJOR" -ge 18 ]; then
	echo "-- virtual generated column"
	psql_run "CREATE TABLE gv_heap (a int, b bigint GENERATED ALWAYS AS (a * 2) VIRTUAL,
									c text GENERATED ALWAYS AS ('r' || a) VIRTUAL) USING heap;"
	psql_run "CREATE TABLE gv_col  (a int, b bigint GENERATED ALWAYS AS (a * 2) VIRTUAL,
									c text GENERATED ALWAYS AS ('r' || a) VIRTUAL) USING pgcolumnar;"
	psql_run "INSERT INTO gv_heap (a) SELECT g FROM generate_series(1, 5000) g;"
	psql_run "INSERT INTO gv_col  (a) SELECT g FROM generate_series(1, 5000) g;"

	check "virtual generated: row count" "$(q "SELECT count(*) FROM gv_col;")" "5000"

	hv_h="$(pgc_set_hash "SELECT a, b, c FROM gv_heap")"
	hv_c="$(counted_hash "SELECT a, b, c FROM gv_col")"
	check_text "virtual generated: values match" "$hv_c" "5000:$hv_h"

	fv_h="$(pgc_set_hash "SELECT a, b FROM gv_heap WHERE b > 8000 AND b < 9000")"
	fv_c="$(counted_hash "SELECT a, b FROM gv_col  WHERE b > 8000 AND b < 9000")"
	check_text "virtual generated: filtered scan matches" "$fv_c" "499:$fv_h"

	# Correctness holds because the executor recomputes the virtual value on read,
	# independent of anything pgColumnar stores. Verify the read values equal the
	# generation expression applied to the base column.
	# IS DISTINCT FROM, so a NULL is a mismatch rather than no answer; and the
	# total rides along, so an empty table cannot read as "no mismatch".
	mism="$(q "SELECT count(*) FILTER (WHERE b IS DISTINCT FROM a * 2 OR c IS DISTINCT FROM 'r' || a)
	           || '/' || count(*) FROM gv_col;")"
	check_text "virtual generated: recomputed correctly" "$mism" "0/5000"

	# Write-path optimization (POSTGRESQL_VERSION_ADOPTION.md item 2): a virtual generated
	# column is computed on read and never stored, so pgColumnar writes NO column
	# chunk for it (b, c = column_index 1, 2), while the base column a (index 0) is
	# stored. The reader returns the column's missing value (NULL) for the absent
	# chunk and the executor expands the generation expression, so reads stay
	# correct (asserted above) with no wasted storage.
	# With the base column's chunks counted beside them, so a table that stored
	# nothing at all cannot read as "no virtual chunk".
	vchunks="$(q "SELECT count(*) FILTER (WHERE column_index IN (1, 2)) || '/' ||
	                     (count(*) FILTER (WHERE column_index = 0) > 0)
	              FROM pgcolumnar.column_chunk
	              WHERE storage_id = pgcolumnar.get_storage_id('gv_col');")"
	check_text "virtual generated: no chunk stored for the virtual columns" "$vchunks" "0/true"
	achunks="$(q "SELECT count(*) FROM pgcolumnar.column_chunk
	              WHERE storage_id = pgcolumnar.get_storage_id('gv_col')
	                AND column_index = 0;")"
	check "virtual generated: base column still stored" \
		"$([ "$achunks" -gt 0 ] && echo yes || echo no)" "yes"
else
	check_skip "virtual generated columns" \
		"SKIP  VIRTUAL generated columns arrived in PostgreSQL 18; this server is $PGC_MAJOR" \
		"needs VIRTUAL generated columns, PG18+"
fi

pgc_summary
