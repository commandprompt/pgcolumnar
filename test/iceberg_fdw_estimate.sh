#!/usr/bin/env bash
# Regression: the Iceberg FDW must estimate baserel->rows from the manifests, not
# hand the planner a constant. The old code set rows = 1000 for every table, which
# mis-sizes a scan and corrupts join planning above it. Post-fix the estimate
# equals the live record count summed from the manifests.
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

python3 -c 'import json' 2>/dev/null || pgc_skip python "python3 is needed"
FX="$(dirname "${BASH_SOURCE[0]}")/fixtures/iceberg"
WH="$FX/warehouse"
[ -f "$(ls "$WH"/db/events/data/*/*.parquet 2>/dev/null | head -1)" ] \
	|| pgc_skip fixture "iceberg warehouse data files are missing"

DEST="$PGC_WORKDIR/wh"; rm -rf "$DEST"; mkdir -p "$DEST"
cp -r "$WH/db" "$DEST/db"; chmod -R u+rwX "$DEST"
MD="$(ls "$DEST"/db/events/metadata/*.metadata.json | sort | tail -1)"

q "CREATE SERVER ice FOREIGN DATA WRAPPER pgcolumnar_iceberg" >/dev/null
q "CREATE FOREIGN TABLE ev (id bigint, region text, amount int)
   SERVER ice OPTIONS (metadata_path '$MD')" >/dev/null

actual="$(q "SELECT count(*) FROM ev")"
est="$(q "EXPLAIN (FORMAT text) SELECT * FROM ev" | grep -oiE 'rows=[0-9]+' | head -1 | cut -d= -f2)"
echo "-- planner estimate=$est  actual=$actual"
check "the FDW estimates a positive row count from the manifests" \
	"$([ -n "$est" ] && [ "$est" -gt 0 ] && echo yes)" "yes"
# check_num: with the metadata unreadable both readings were empty and agreed
# as "" == "" (measured).
check_num "the estimate is the real record count, not the old constant 1000" "$est" "$actual"

# The sum is the base cardinality, so selectivity still applies: a filtered scan
# must estimate fewer rows than the whole table (the old code set rows directly
# and ignored the WHERE, estimating the full count under a filter too).
filt="$(q "EXPLAIN (FORMAT text) SELECT * FROM ev WHERE region = 'eu'" | grep -oiE 'rows=[0-9]+' | head -1 | cut -d= -f2)"
echo "-- filtered (region='eu') estimate=$filt of $actual"
check "a filtered scan estimates fewer rows than the whole table (selectivity applied)" \
	"$([ -n "$filt" ] && [ "$filt" -lt "$actual" ] && echo yes)" "yes"

check "backend alive" "$(q 'SELECT 1')" "1"
pgc_summary
