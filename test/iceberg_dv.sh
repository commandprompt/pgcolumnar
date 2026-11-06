#!/usr/bin/env bash
#
# pgColumnar Iceberg v3 deletion vectors (#388 phase 4c). iceberg_scan reads a
# table whose current snapshot carries Puffin deletion vectors and drops the
# rows they name. Fixtures are hand-crafted (test/fixtures/iceberg/
# warehouse_del, generator gen_delete_fixture.py): pyroaring portable bitmaps
# wrapped in hand-built Puffin files, hand-encoded manifests, exact sequence
# numbers. The hand-derived oracle is cross-checked against two independent
# engines (pyiceberg and DuckDB) by crosscheck_dv.py; the two arms where those
# engines deviate from the spec are documented there, and this suite asserts
# the spec behavior.
#
# Usage:  test/iceberg_dv.sh [PG_CONFIG]

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# refusal SQL PHRASE -> the SQLSTATE when SQL is refused with PHRASE in its message;
# "SQLSTATE (another refusal: MESSAGE)" when a different guard refused it; "no
# error" when it ran; nothing when the server could not be asked. An arm used to
# read the SQLSTATE alone, and several guards here raise XX001: with the
# manifest/footer offset match removed, a later XX001 refused the same file and
# that arm still passed.
refusal() {
	local out st msg
	await_up
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -qtA 2>&1 <<SQLEOF
\\set VERBOSITY verbose
$1;
SQLEOF
)"
	st="$(sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\): .*/\1/p' <<<"$out" | head -1)"
	msg="$(sed -n 's/^ERROR:  [0-9A-Z]\{5\}: //p' <<<"$out" | head -1)"
	if [ -n "$st" ]; then
		case "$msg" in *"$2"*) echo "$st" ;; *) echo "$st (another refusal: $msg)" ;; esac
	elif grep -q 'could not connect\|server closed the connection\|terminating connection' <<<"$out"; then
		:
	else
		echo "no error"
	fi
}

# the first ERROR message line, for telling apart same-SQLSTATE refusals
errmsg_of() {
	await_up
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -qtA 2>&1 <<SQLEOF | sed -n 's/^ERROR:  //p' | head -1
$1;
SQLEOF
}

# await_up -> wait, at most 60 s, until the server answers. A refusal arm that kills a
# backend sends the server through crash recovery; without the wait, the next arms'
# readings fail on recovery and red as collateral, more or fewer of them by timing.
# The crash itself is still caught: survived() counts it.
await_up() {
	local i
	for i in $(seq 1 120); do
		[ "$(q 'SELECT 1')" = 1 ] && return 0
		sleep 0.5
	done
}

# Backends killed by a signal, from the server log: a number, or nothing when the
# log can't be read. A fresh connection answers again after crash recovery, so
# "SELECT 1" alone passed with a backend killed by a refusal arm (no
# referenced_data_file check: a NULL path reached strcmp); the survival checks
# below also require that this count has not moved.
crashes() {
	local _n
	_n="$(pgc_pg "grep -c 'terminated by signal' '$PGC_LOGFILE'" 2>/dev/null)"
	case "$_n" in '' | *[!0-9]*) ;; *) echo "$_n" ;; esac
}
survived() {	# survived BEFORE -> "1" when the server answers and no backend died since
	local _up _now
	_up="$(q 'SELECT 1')"; _now="$(crashes)"
	if ! [[ "$1" =~ ^[0-9]+$ && "$_now" =~ ^[0-9]+$ ]]; then echo "no (crash count unreadable)"
	elif [ "$_up" = 1 ] && [ "$_now" = "$1" ]; then echo 1
	else echo "no ([$_up], $(( _now - $1 )) backend crashes)"; fi
}

FX="$(dirname "${BASH_SOURCE[0]}")/fixtures/iceberg"
WHD="$FX/warehouse_del"
[ -f "$WHD/db/t/metadata/dvapply.metadata.json" ] || pgc_skip fixture "deletion-vector fixtures are missing"
python3 -c 'import json' 2>/dev/null || pgc_skip python "python3 is needed"

# relocate so recorded root (/tmp/pgc_ice_del) != actual root (rebasing)
DEST="$PGC_WORKDIR/dv"
rm -rf "$DEST"; mkdir -p "$DEST"
cp -r "$WHD/db" "$DEST/db"
chmod -R u+rwX "$DEST"
MDIR="$DEST/db/t/metadata"

dv_oracle() {
	python3 - "$FX/warehouse_del/expected_deletes.json" "$1" <<'PY'
import json, sys
o = json.load(open(sys.argv[1]))
print(",".join(str(i) for i in o["dv_surviving"][sys.argv[2]]))
PY
}
dv_ids_of() {
	q "SELECT string_agg(id::text, ',' ORDER BY id)
	   FROM pgcolumnar.iceberg_scan('$MDIR/$1.metadata.json')
	     AS t(id bigint, region text, amount int)"
}

# ---- deletion vectors are applied -------------------------------------------
# THE <= boundary: a DV at the data file's own sequence number applies (same
# rule as 4a position-delete files; a strict > would wrongly exclude it)
CRASHES_BEFORE="$(crashes)"
check_text "a deletion vector at an equal sequence number applies (survivors == oracle)" \
	"$(dv_ids_of dvapply)" "$(dv_oracle dvapply)"
# the other side: a DV strictly older than the data applies to nothing
check_text "a deletion vector older than the data does not apply" \
	"$(dv_ids_of dvnoapply)" "$(dv_oracle dvnoapply)"
# the spec's supersede rule: a position-delete FILE applies only when no DV
# must be applied to the data file. The fixture's DV drops ordinal 1 and its
# (also applicable) Parquet posdel names ordinals 1 and 3; a UNION would drop
# both, supersede drops only ordinal 1.
check_text "an applicable DV supersedes position-delete files for its data file" \
	"$(dv_ids_of dvsupersede)" "$(dv_oracle dvsupersede)"
# supersede is per data file: a posdel targeting a DIFFERENT data file still
# applies alongside the DV
check_text "supersede is per data file, not global" \
	"$(dv_ids_of dvother)" "$(dv_oracle dvother)"
# roaring coverage: run-form containers plus a second 64-bit bucket
check_text "a DV with run containers and a high-bucket position decodes" \
	"$(dv_ids_of dvwide)" "$(dv_oracle dvwide)"
# cookie 12347 (run containers marked in the run bitset)
check_text "a run-optimized DV (cookie 12347) decodes" \
	"$(dv_ids_of dvrun)" "$(dv_oracle dvrun)"
# a bitset container (cardinality above 4096)
check_text "a bitset-container DV decodes" \
	"$(dv_ids_of dvbitset)" "$(dv_oracle dvbitset)"
# one Puffin file carrying two DV blobs for two data files
check_text "two DV blobs in one Puffin file each apply to their own data file" \
	"$(dv_ids_of dvtwo)" "$(dv_oracle dvtwo)"
check "backend still up after the DV applications" "$(survived "$CRASHES_BEFORE")" "1"

# ---- refusal and corruption arms --------------------------------------------
CRASHES_BEFORE="$(crashes)"
# two DV entries referencing one data file: the spec says scan results are
# undefined and readers may raise; we raise rather than guess
check "two DV entries for one data file are refused (XX001)" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/dvdup.metadata.json')
	                AS t(id bigint, region text, amount int)" "two deletion vectors reference data file")" "XX001"
check "a corrupted DV CRC is refused (XX001)" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/dvbadcrc.metadata.json')
	                AS t(id bigint, region text, amount int)" "fails its CRC-32 check")" "XX001"
check "a wrong DV blob magic is refused (XX001)" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/dvbadmagic.metadata.json')
	                AS t(id bigint, region text, amount int)" "has a bad magic sequence")" "XX001"
# the manifest's content_offset must exactly match the footer's blob offset
check "a manifest/footer offset mismatch is refused (XX001)" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/dvoffmismatch.metadata.json')
	                AS t(id bigint, region text, amount int)" "has no blob matching the manifest's offset")" "XX001"
# referenced_data_file is required for DVs
check "a DV entry without referenced_data_file is refused (XX001)" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/dvnoref.metadata.json')
	                AS t(id bigint, region text, amount int)" "has no referenced_data_file")" "XX001"
# record_count is defined as the DV's cardinality
check "a record_count that disagrees with the DV cardinality is refused (XX001)" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/dvbadcount.metadata.json')
	                AS t(id bigint, region text, amount int)" "positions but its manifest entry records")" "XX001"
# deletion-vector-v1 blobs must not declare a compression codec
check "a DV blob declaring a compression codec is refused (XX001)" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/dvcompressed.metadata.json')
	                AS t(id bigint, region text, amount int)" "declares a compression codec")" "XX001"
# a compressed Puffin footer is refused loudly, never misparsed
check "a compressed Puffin footer is refused (0A000)" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/dvflags.metadata.json')
	                AS t(id bigint, region text, amount int)" "has a compressed Puffin footer")" "0A000"
check "...and the refusal names the compressed footer" \
	"$(errmsg_of "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/dvflags.metadata.json')
	              AS t(id bigint, region text, amount int)" | grep -c "compressed")" "1"
# deletion vectors are v3-only
check "a deletion vector in a format-version 2 table is refused (0A000)" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/dvv2.metadata.json')
	                AS t(id bigint, region text, amount int)" "is a deletion vector, which format-version")" "0A000"
check "...and the refusal names the format version" \
	"$(errmsg_of "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/dvv2.metadata.json')
	              AS t(id bigint, region text, amount int)" | grep -c "format-version")" "1"
# a Puffin path outside the table root is stopped by the path boundary
check "a Puffin path outside the table location is refused (22023)" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/dvescape.metadata.json')
	                AS t(id bigint, region text, amount int)" "is not under the table location")" "22023"
check "backend still up after the DV refusals" "$(survived "$CRASHES_BEFORE")" "1"

# ---- audit arms (multi-agent adversarial audit findings) --------------------
CRASHES_BEFORE="$(crashes)"
# content_offset + content_size chosen to overflow int64: a bounds check that
# adds the operands would wrap negative and read a wild pointer; testing each
# operand against the file size refuses it cleanly
check "a DV whose offset+size overflows int64 is refused, not read (XX001)" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/dvbigoff.metadata.json')
	                AS t(id bigint, region text, amount int)" "lies outside the file's blob region")" "XX001"
# a run container whose start+length exceeds the 16-bit container range would
# fabricate positions in a neighboring container; refuse rather than corrupt
check "a run container overflowing its 16-bit range is refused (XX001)" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/dvrunoverflow.metadata.json')
	                AS t(id bigint, region text, amount int)" "overflows its 16-bit range")" "XX001"
# two DV entries for one data file recorded with and without the file:// scheme:
# the one-DV-per-file check strips the scheme, so the aliased pair is caught
check "an aliased duplicate DV (scheme vs no scheme) is refused (XX001)" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_scan('$MDIR/dvdupscheme.metadata.json')
	                AS t(id bigint, region text, amount int)" "two deletion vectors reference data file")" "XX001"
check "backend still up after the audit arms" "$(survived "$CRASHES_BEFORE")" "1"

pgc_summary
