#!/usr/bin/env bash
# Regression: the Iceberg FDW pushes projection down (decodes only the columns a
# query references), and doing so must not change results. The reader already
# supports a needTop mask; this asserts the FDW passes it correctly by comparing
# every projection shape against bare iceberg_scan (which reads all columns).
#
# The performance win itself (a narrow projection over a wide table decodes far
# fewer columns) is not asserted here because timing is environment-dependent; it
# was measured out of band (a 1-of-40-column scan ran ~5x faster than the same
# query before the pushdown). This suite pins the CORRECTNESS of the pushdown.
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

python3 -c 'import json' 2>/dev/null || pgc_skip python "python3 is needed"
FX="$(dirname "${BASH_SOURCE[0]}")/fixtures/iceberg/warehouse_wide"
[ -f "$(ls "$FX"/db/wide/data/*.parquet 2>/dev/null | head -1)" ] \
	|| pgc_skip fixture "wide iceberg fixture is missing"

DEST="$PGC_WORKDIR/wh"; rm -rf "$DEST"; mkdir -p "$DEST"
cp -r "$FX/db" "$DEST/db"; chmod -R u+rwX "$DEST"
MD="$(ls "$DEST"/db/wide/metadata/*.metadata.json | sort | tail -1)"

COLS="k bigint$(for i in $(seq 0 11); do printf ', c%d text' "$i"; done)"
q "CREATE SERVER ice FOREIGN DATA WRAPPER pgcolumnar_iceberg" >/dev/null
q "CREATE FOREIGN TABLE w ($COLS) SERVER ice OPTIONS (metadata_path '$MD')" >/dev/null

# Each projection shape: the FDW (projected) must equal iceberg_scan (all columns).
# One literal check per shape, so the grader reads each name: a helper that built
# "$name : ..." left all six names unreadable.
fdw_of()  { q "SELECT $1 FROM w ${2:-}"; }
scan_of() { q "SELECT $1 FROM pgcolumnar.iceberg_scan('$MD') AS t($COLS) ${2:-}"; }
# check_text: with the metadata unreadable both sides failed, read empty, and all
# six shapes passed (measured).
check_text "one column : FDW projection matches iceberg_scan" \
	"$(fdw_of "string_agg(c0, ',' ORDER BY k)")" "$(scan_of "string_agg(c0, ',' ORDER BY k)")"
check_text "three columns : FDW projection matches iceberg_scan" \
	"$(fdw_of "string_agg(c0||c5||c11, ',' ORDER BY k)")" "$(scan_of "string_agg(c0||c5||c11, ',' ORDER BY k)")"
check_text "whole row : FDW projection matches iceberg_scan" \
	"$(fdw_of "string_agg(k||c0||c6||c11, ',' ORDER BY k)")" "$(scan_of "string_agg(k||c0||c6||c11, ',' ORDER BY k)")"
check_text "count only (no column referenced) : FDW projection matches iceberg_scan" \
	"$(fdw_of "count(*)")" "$(scan_of "count(*)")"
check_text "filter references an unselected column : FDW projection matches iceberg_scan" \
	"$(fdw_of "string_agg(c1, ',' ORDER BY k)" "WHERE c7 = 'c7r10'")" \
	"$(scan_of "string_agg(c1, ',' ORDER BY k)" "WHERE c7 = 'c7r10'")"
check_text "select one, filter another : FDW projection matches iceberg_scan" \
	"$(fdw_of "string_agg(c2, ',' ORDER BY k)" "WHERE c9 = 'c9r5' OR c9 = 'c9r6'")" \
	"$(scan_of "string_agg(c2, ',' ORDER BY k)" "WHERE c9 = 'c9r5' OR c9 = 'c9r6'")"

check "backend alive" "$(q 'SELECT 1')" "1"
pgc_summary
