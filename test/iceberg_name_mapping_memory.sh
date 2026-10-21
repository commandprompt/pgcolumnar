#!/usr/bin/env bash
#
# pgColumnar #731: the Iceberg name-mapping parse must not allocate per element.
#
# READ THE DISPOSITION FIRST, because the issue's headline is wrong and this
# suite deliberately does not assert it.
#
# #731 was filed as a memory-amplification DoS: ~190 bytes of resident memory per
# byte of input. Measuring it dissolved that twice over.
#
#   1. It needs `pg_read_server_files`. Every entry point is gated.
#   2. 87.5% of the amplification is core PostgreSQL's `jsonb_in`, not ours.
#      Ablated: with the loop body never entered, a 1.6M-entry mapping still
#      peaked at 1,046,456 kB. The byte-identical string cast by core alone
#      peaked at 1,050,280 kB -- run by a role with NO grants at all, so an
#      unprivileged user can trigger a strictly larger amplification through
#      `SELECT ('[' || repeat('{},', N) || '{}]')::jsonb` with pgcolumnar
#      nowhere in the picture.
#   3. It does not accumulate: the same scan repeated 1, 2 and 4 times in one
#      query peaked at 300,788 / 305,712 / 305,812 kB. Flat.
#
# So there is no DoS here and no retention bug. What IS ours, and what this
# suite measures, is the per-element scratch the entry loop allocates and holds
# until the call ends: one `JsonbValue` from `getIthJsonbValueFromContainer` and
# one from `ice_field`, per entry, measured at ~100 bytes an element.
#
# THE ASSERTION IS THE EXCESS OVER CORE'S OWN COST, not a total. A total would
# be measuring `jsonb_in`, which this code does not control and cannot fix, and
# it would move with every PostgreSQL release. The core arm parses the
# BYTE-IDENTICAL mapping string, so subtracting it leaves only what pgcolumnar
# adds, and a slope across two sizes cancels the fixed cost of opening the file
# and reading the data.
#
# Usage:  test/iceberg_name_mapping_memory.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

FX="$(dirname "${BASH_SOURCE[0]}")/fixtures/iceberg"
WHN="$FX/warehouse_nm"
[ -f "$WHN/db/t/metadata/nmapply.metadata.json" ] || pgc_skip fixture "name-mapping fixtures are missing"
python3 -c 'import json' 2>/dev/null || pgc_skip python "python3 is needed"

q() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "$1" 2>&1
}
q1() { q "$1" | tail -1; }

DEST="$PGC_WORKDIR/nmmem"; rm -rf "$DEST"; mkdir -p "$DEST"
cp -r "$WHN/db" "$DEST/db"; chmod -R u+rwX "$DEST"
MDIR="$DEST/db/t/metadata"

SMALL=200000
LARGE=800000

# peak RSS of ONE statement, in a backend of its own. VmHWM is monotone, so this
# needs no sampling and cannot miss the peak the way an interval sampler can.
peak() {	# peak QUERY -> kB above the idle baseline, or empty
	local query="$1" fifo pid="" n=0 base pk
	fifo="$PGC_WORKDIR/nmm.$RANDOM.$RANDOM"; rm -f "$fifo"; mkfifo "$fifo"
	env PATH="$PGC_BINDIR:$PATH" PGAPPNAME=pgc731 \
		psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At \
		< "$fifo" > "$fifo.out" 2>&1 &
	exec 9> "$fifo"
	echo "SELECT 1;" >&9
	while [ -z "$pid" ] && [ $n -lt 200 ]; do
		pid="$(q1 "SELECT pid FROM pg_stat_activity WHERE application_name = 'pgc731' LIMIT 1;")"
		n=$((n+1)); [ -z "$pid" ] && sleep 0.1
	done
	if [ -z "$pid" ]; then exec 9>&-; echo ""; return 1; fi
	base=$(awk '/VmHWM/{print $2}' "/proc/$pid/status" 2>/dev/null)
	echo "$query;" >&9
	n=0
	while [ "$(q1 "SELECT count(*) FROM pg_stat_activity WHERE pid = $pid AND state = 'idle';")" != 1 ] && [ $n -lt 4000 ]; do
		n=$((n+1)); sleep 0.1
	done
	pk=$(awk '/VmHWM/{print $2}' "/proc/$pid/status" 2>/dev/null)
	exec 9>&-; wait 2>/dev/null
	# A statement that ERRORED has no peak worth the name: with the scan pointed
	# at a missing file it still read ~2 MB, the overhead went negative, and the
	# #731 claim passed on a scan that never ran (measured).
	local errored
	errored="$(grep -c '^ERROR' "$fifo.out" 2>/dev/null)"
	rm -f "$fifo" "$fifo.out"
	[ "${errored:-1}" = 0 ] && [ -n "$base" ] && [ -n "$pk" ] && echo $(( pk - base )) || echo ""
}

# build_fixture N -> a padded metadata file AND the identical mapping string
# alone, so core's arm parses exactly the same bytes ours does. The padding
# entries carry no field-id, so they contribute ZERO names: everything they cost
# is scratch, which is the quantity under test.
build_fixture() {
	python3 - "$MDIR" "$1" <<'PY_FIX'
import json, sys
d, N = sys.argv[1], int(sys.argv[2])
md = json.load(open(d + "/nmapply.metadata.json"))
real = json.loads(md["properties"]["schema.name-mapping.default"])
nm = json.dumps(real + [{} for _ in range(N)])
md["properties"]["schema.name-mapping.default"] = nm
open(d + "/pad.metadata.json", "w").write(json.dumps(md))
open(d + "/mapping.json", "w").write(nm)
PY_FIX
}

measure() {	# measure N -> "core scan overhead", in kB
	local n="$1" core scan
	build_fixture "$n"
	core="$(peak "SELECT length(pg_read_file('$MDIR/mapping.json')::jsonb::text)")"
	scan="$(peak "SELECT count(*) FROM pgcolumnar.iceberg_scan('$MDIR/pad.metadata.json') AS t(id bigint, region text, amount int)")"
	[ -z "$core" ] || [ -z "$scan" ] && { echo ""; return 1; }
	echo "$core $scan $(( scan - core ))"
}

small="$(measure "$SMALL")"
large="$(measure "$LARGE")"
echo "      ${SMALL} entries: core_floor=$(awk '{print $1}' <<<"$small")kB scan=$(awk '{print $2}' <<<"$small")kB overhead=$(awk '{print $3}' <<<"$small")kB"
echo "      ${LARGE} entries: core_floor=$(awk '{print $1}' <<<"$large")kB scan=$(awk '{print $2}' <<<"$large")kB overhead=$(awk '{print $3}' <<<"$large")kB"

# Every reading must be real before any of them is compared.
# Unrolled, so the grader reads both names.
check_text "the small arm produced three readings" \
	"$([ "$(wc -w <<<"$small")" -eq 3 ] && echo 1 || echo "0 (readings [$small])")" 1
check_text "the large arm produced three readings" \
	"$([ "$(wc -w <<<"$large")" -eq 3 ] && echo 1 || echo "0 (readings [$large])")" 1
# And the core floor must actually dominate, or the subtraction is measuring
# noise rather than a floor -- which is the whole basis for using a difference.
cf="$(awk '{print $1}' <<<"$large")"; ov="$(awk '{print $3}' <<<"$large")"
check_text "core's own parse is the larger part of the scan's peak" \
	"$(pgc_is_number "$cf" && pgc_is_number "$ov" && [ "$cf" -gt "$ov" ] && echo 1 \
	   || echo "0 (core floor [$cf] kB, overhead [$ov] kB)")" 1
# The fixture has to be big enough that the two sizes differ measurably at all.
sp="$(awk '{print $2}' <<<"$small")"; lp="$(awk '{print $2}' <<<"$large")"
check_text "the two sizes give different peaks" \
	"$(pgc_is_number "$sp" && pgc_is_number "$lp" && [ "$lp" -gt $(( sp * 2 )) ] && echo 1 \
	   || echo "0 (scan peaks: small [$sp] kB, large [$lp] kB)")" 1

# THE CLAIM. Bytes of pgcolumnar-attributable memory per mapping entry, as a
# slope so the fixed cost of opening the file and reading the data cancels.
#
# Measured before the fix: 20,392 kB at 200k entries and 78,772 kB at 800k, a
# slope of ~100 bytes an entry, which agrees with an independent ablation
# (~150 MB over 1.6M entries) taken a different way. After it, the per-entry
# scratch is reclaimed each iteration and the slope goes to about zero. The
# 30-byte gate sits three times under the defect and well over the noise.
per_entry="$(awk -v lo="$(awk '{print $3}' <<<"$small")" -v hi="$(awk '{print $3}' <<<"$large")" \
	-v dn="$(( LARGE - SMALL ))" 'BEGIN { printf "%d", ((hi - lo) * 1024) / dn }')"
echo "      pgcolumnar-attributable memory per mapping entry: ${per_entry} bytes"
# Only over two real readings: awk reads a missing overhead as 0, so with either
# measurement failed the slope was 0 and this passed (measured).
check_text "the name-mapping parse does not allocate per entry (#731)" \
	"$([ "$(wc -w <<<"$small")" -eq 3 ] && [ "$(wc -w <<<"$large")" -eq 3 ] && [ "$per_entry" -lt 30 ] && echo 1 \
	   || echo "0 ($per_entry bytes an entry, from readings [$small] [$large])")" 1

# The answers must be unchanged throughout: a fix that reclaimed a name still in
# use would show up here and not in the number above.
check_num "the padded mapping still binds every row" \
	"$(q1 "SELECT count(*) FROM pgcolumnar.iceberg_scan('$MDIR/pad.metadata.json') AS t(id bigint, region text, amount int);")" 5
# Against the fixture's own rows (data.parquet, read with pyarrow), not a second
# read through the same mapping code: with the mapping no longer binding region,
# both scans read NULL for every region and their two md5s agreed (measured). A
# scan that errored is a failed reading that names its file, and a NULL region
# makes string_agg NULL, an empty side.
values_of() {	# values_of METADATA -> the scan's rows as id:region, or why not
	local out
	out="$(q "SELECT string_agg(id || ':' || region, ',' ORDER BY id) FROM pgcolumnar.iceberg_scan('$MDIR/$1') AS t(id bigint, region text, amount int)")"
	if [ "$(printf '%s\n' "$out" | grep -c '^ERROR')" -gt 0 ]; then echo "$1 did not scan: $(printf '%s' "$out" | head -1)"
	else printf '%s' "$out"; fi
}
check_text "and the mapped values are still correct" \
	"$(values_of pad.metadata.json)" "1:eu,2:eu,3:us,4:us,5:us"

pgc_summary
