#!/usr/bin/env bash
#
# pgColumnar Parquet column-projection pushdown (Phase G).
#
# The FDW scan now decodes only the column chunks the query references; an
# unreferenced column's chunk is never decompressed or decoded. This suite checks
# that projected results are correct against a heap oracle, that the EXPLAIN
# "Columns Read" counter reflects the projection (including 0 for count(*) and all
# for SELECT *), and -- the load-bearing proof -- that a column which would error
# on decode is not read when it is projected out.
#
# THE ORACLE IS THE GENERATOR, NOT THE READER (#1341). h used to be filled with
# read_parquet of the same file, and read_parquet shares the scan's decoders: with a
# text decoder dropping a byte, or an INT64 decoder off by one, both sides read the
# same wrong values and every projection agreed (main passed 15/15 under each). h is
# now built in SQL from the generator the crafter wrote the file from.
#
# A DROPPED COLUMN SHIFTS ATTRIBUTE NUMBERS. Projection matches each target column's
# attribute number against the planner's needed set. With no dropped column the
# attribute number equals the target's index, so a reader matching by index passed
# 15/15. ftd drops a column ahead of the others, and its projection must still read
# exactly the column asked for.
#
# Usage:  test/native_parquet_projection.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

if ! python3 -c 'import pyarrow.parquet' 2>/dev/null; then
	pgc_skip pyarrow "pyarrow not available; projection suite needs it"
fi

W="$PGC_WORKDIR"
psql_run "CREATE SERVER pq FOREIGN DATA WRAPPER pgcolumnar_parquet;"

python3 "$(dirname "${BASH_SOURCE[0]}")/craft_parquet_projection.py" "$W"

cols="a int, b int8, c text, d float8"
psql_run "CREATE FOREIGN TABLE ft ($cols) SERVER pq OPTIONS (path '$W/proj.parquet');"
psql_run "CREATE TABLE h ($cols);"
# the generator the crafter wrote proj.parquet from, in SQL: never read back through
# the reader under test
psql_run "INSERT INTO h SELECT g, g * 2, 's' || (g % 20), g::float8 / 3.0 FROM generate_series(0, 4999) g;"
# the same file behind a table whose first declared column is dropped: every later
# column's attribute number is one more than its position among the live columns
psql_run "CREATE FOREIGN TABLE ftd (z int, $cols) SERVER pq OPTIONS (path '$W/proj.parquet');"
psql_run "ALTER FOREIGN TABLE ftd DROP COLUMN z;"

# ---- correctness: projected columns match the oracle -----------------------
check "projection [a] == oracle" "$(pgc_set_hash "SELECT a FROM ft")" "$(pgc_set_hash "SELECT a FROM h")"
check "projection [c] == oracle" "$(pgc_set_hash "SELECT c FROM ft")" "$(pgc_set_hash "SELECT c FROM h")"
check "projection [a, d] == oracle" "$(pgc_set_hash "SELECT a, d FROM ft")" "$(pgc_set_hash "SELECT a, d FROM h")"
check "projection [b, c, d] == oracle" "$(pgc_set_hash "SELECT b, c, d FROM ft")" "$(pgc_set_hash "SELECT b, c, d FROM h")"
check "projection [*] == oracle" "$(pgc_set_hash "SELECT * FROM ft")" "$(pgc_set_hash "SELECT * FROM h")"
# a qual column that is not selected must still be read for the recheck
check "qual on unselected column == oracle" \
	"$(pgc_set_hash "SELECT a FROM ft WHERE c = 's7'")" \
	"$(pgc_set_hash "SELECT a FROM h  WHERE c = 's7'")"

# ---- the EXPLAIN counter reflects the projection ---------------------------
cols_read() {  # cols_read QUERY
	q "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) $1" \
		| grep 'Columns Read:' | grep -oE '[0-9]+' | head -1
}
check "one column reads 1" "$(cols_read "SELECT a FROM ft")" "1"
check "two columns read 2" "$(cols_read "SELECT a, d FROM ft")" "2"
check "select star reads 4" "$(cols_read "SELECT * FROM ft")" "4"
check "count(*) reads 0"    "$(cols_read "SELECT count(*) FROM ft")" "0"

# ---- a dropped column ahead of the others ------------------------------------
check "after a dropped column, projection [d] == oracle" \
	"$(pgc_set_hash "SELECT d FROM ftd")" "$(pgc_set_hash "SELECT d FROM h")"
check "after a dropped column, one column reads 1" "$(cols_read "SELECT d FROM ftd")" "1"
# a qual column counts as read even when not selected
check "selected 1 + qual 1 reads 2" "$(cols_read "SELECT a FROM ft WHERE c = 's7'")" "2"

# ---- proof that a projected-out column is not decoded -----------------------
# The counter reflects the decision; this proves the decode actually skips. A
# two-column file whose SECOND column's data page is corrupted: reading only the
# first column must succeed (the second is never decoded), while reading the
# second must fail. If projection did not skip decode, selecting only the first
# would also hit the corruption.
# corrupt.parquet was written by the crafter above, its second column's page smashed.

psql_run "CREATE FOREIGN TABLE ftc (g int, bad int) SERVER pq OPTIONS (path '$W/corrupt.parquet');"
psql_run "CREATE FUNCTION pgc_try_proj(q text) RETURNS text LANGUAGE plpgsql AS \$\$
          BEGIN EXECUTE q; RETURN 'OK';
          EXCEPTION WHEN OTHERS THEN RETURN 'ERROR'; END \$\$;"
tryq() { q "SELECT pgc_try_proj(\$q\$$1\$q\$);"; }

check "selecting the good column skips the corrupt one" \
	"$(tryq "SELECT count(g) FROM ftc")" "OK"
check "count(*) decodes nothing, survives corruption" \
	"$(tryq "SELECT count(*) FROM ftc")" "OK"
check "selecting the corrupt column does error" \
	"$(tryq "SELECT count(bad) FROM ftc")" "ERROR"
check "good-column values are still correct" \
	"$(q 'SELECT sum(g)::text FROM ftc;')" "499500"

pgc_summary
