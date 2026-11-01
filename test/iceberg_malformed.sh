#!/usr/bin/env bash
#
# pgColumnar Iceberg read-path robustness (#388 phase 4a, #644). The Iceberg
# reader takes an untrusted metadata.json / manifest / manifest list written by
# whoever authored the table, and is called by an honest pg_read_server_files
# role that named only the trusted metadata pointer. A crafted manifest must be
# REFUSED cleanly, never crash the backend. Each arm has a control so a vacuous
# pass is visible, each refusal names its own message (the SQLSTATE alone is
# shared with later parse errors), and a "backend still up" probe after every
# refusal reads the server log's crash count, so a segfault followed by crash
# recovery does not pass as a refusal. Fixtures are hand-crafted
# (test/fixtures/iceberg/warehouse_malformed) because no writer emits these
# shapes; see gen_malformed_fixture.py.
#
# Usage:  test/iceberg_malformed.sh [PG_CONFIG]

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# refusal SQL REGEX LABEL -> "<SQLSTATE> (LABEL)" when SQL raised an error whose message
# matches REGEX, "<SQLSTATE>: <message>" for any other error, or "no error, read: ROWS". Every
# arm here refuses with XX001, and so do later parse steps: a FIFO read as an empty
# file failed a parse with the same XX001, so the SQLSTATE alone passed with the
# guard removed. The arm now names the refusal it wants. When SQL raised no error,
# the reading carries the rows it returned, so a failure shows the value read.
refusal_of() {	# stdin: psql output -> the refusal reading
	local re="$1" label="$2" line state msg
	local out; out="$(cat)"
	line="$(sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\): /\1|/p' <<<"$out" | head -1)"
	if [ -z "$line" ]; then
		if grep -qE 'server closed the connection|connection to server' <<<"$out"; then
			echo "no refusal: the connection was lost"; return
		fi
		# Only the rows go in the reading. psql's stderr is merged here, so drop the
		# server's NOTICE, WARNING and their detail lines: the twin never sees them.
		out="$(grep -vE '^(DEBUG|LOG|INFO|NOTICE|WARNING|DETAIL|HINT|CONTEXT|LOCATION|QUERY): ' <<<"$out")"
		echo "no error${out:+, read: $(tr '\n' ' ' <<<"$out" | sed 's/ *$//')}"
		return
	fi
	state="${line%%|*}"; msg="${line#*|}"
	if grep -qE -- "$re" <<<"$msg"; then echo "$state ($label)"; else echo "$state: $msg"; fi
}
refusal() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -qtA 2>&1 <<SQLEOF | refusal_of "$2" "$3"
\\set VERBOSITY verbose
$1;
SQLEOF
}

# Run one statement under a HARD wall-clock cap. A cancel-resistant open (a FIFO)
# cannot be ended by statement_timeout, so the external `timeout` is the only
# reliable detector. Prints the refusal reading as refusal() does, or the literal
# HANG when psql was KILLed by the cap (exit 124/137). No SQL-level
# statement_timeout is set, so a merely-slow legit path is never a false HANG.
FIFO_CAP=5
FIFO_RE='is not a regular file'
refusal_or_hang() {
	local out rc
	out="$(timeout -s KILL "$FIFO_CAP" env PATH="$PGC_BINDIR:$PATH" psql \
		-h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -qtA 2>&1 <<SQLEOF
\\set VERBOSITY verbose
$1;
SQLEOF
)"
	rc=$?
	if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then echo HANG; return; fi
	printf '%s\n' "$out" | refusal_of "$FIFO_RE" "not a regular file"
}

# Backends killed by a signal, from the server log: a number, or nothing when the
# log can't be read. A fresh connection answers again after crash recovery, so
# "SELECT 1" alone passed after a backend segfaulted on a NULL path, once the probe
# came 3s later. Every survival check also requires that this count has not moved
# since just before its own arm ran; when it has, the check waits out the crash
# recovery, so the arms after it are not collateral.
crashes() {
	local _n
	_n="$(pgc_pg "grep -c 'terminated by signal' '$PGC_LOGFILE'" 2>/dev/null)"
	case "$_n" in '' | *[!0-9]*) ;; *) echo "$_n" ;; esac
}
survived() {	# survived BEFORE -> "1" when the server answers and no backend died since BEFORE
	local _up _now _i
	_up="$(q 'SELECT 1')"; _now="$(crashes)"
	if ! [[ "$1" =~ ^[0-9]+$ && "$_now" =~ ^[0-9]+$ ]]; then echo "no (crash count unreadable)"; return; fi
	if [ "$_up" = 1 ] && [ "$_now" = "$1" ]; then echo 1; return; fi
	for _i in $(seq 1 60); do [ "$(q 'SELECT 1' 2>/dev/null)" = 1 ] && break; sleep 0.5; done
	echo "no ([$_up], $(( _now - $1 )) backend crashes)"
}

# Release a backend that is blocked in open(2) on a FIFO (main only): opening the
# write end lets its open return; it then hits EOF/short-read and errors out, so
# cluster teardown is clean. No-op after the fix (nothing is ever blocked).
fifo_release() { exec 9<>"$1" 2>/dev/null; exec 9>&- 2>/dev/null; }

FX="$(dirname "${BASH_SOURCE[0]}")/fixtures/iceberg"
WHM="$FX/warehouse_malformed"
[ -f "$WHM/nullpath/db/t/metadata/t.metadata.json" ] \
	&& [ -f "$WHM/nullseq_ml/ml.avro" ] \
	&& [ -f "$WHM/nullminseq_ml/ml.avro" ] \
	&& [ -f "$WHM/nullmanifestpath/db/t/metadata/t.metadata.json" ] \
	&& [ -f "$WHM/danglingschema/db/t/metadata/t.metadata.json" ] \
	&& [ -f "$WHM/enum_ctl/db/t/metadata/t.metadata.json" ] \
	|| pgc_skip fixture "malformed iceberg fixtures are missing"
python3 -c 'import json' 2>/dev/null || pgc_skip python "python3 is needed"

# relocate so the recorded root (/tmp/pgc_ice_malformed) != actual root, which
# exercises the same path rebasing the honest read path uses
DEST="$PGC_WORKDIR/malformed"
rm -rf "$DEST"; mkdir -p "$DEST"
cp -r "$WHM"/* "$DEST/"
chmod -R u+rwX "$DEST"

# =========================================================================
# #7  an Avro record schema whose "fields" element is a JSON array crashed the
#     backend (an object-container Assert / OOB heap read). It must now be a
#     clean malformed-schema error, and the control must still decode.
# =========================================================================
B="$(crashes)"
check "an array-valued schema fields element is refused, not crashed (XX001)" \
	"$(refusal "SELECT * FROM pgcolumnar.read_avro_manifest('$DEST/arrayfield/evil.avro')" \
		'unsupported or malformed Avro schema in manifest' "malformed schema")" \
	"XX001 (malformed schema)"
check "backend still up after the malformed-schema refusal" "$(survived "$B")" "1"
check "control: a well-formed field object still decodes (0 entries)" \
	"$(q "SELECT count(*) FROM pgcolumnar.read_avro_manifest('$DEST/arrayfield/good.avro')")" \
	"0"

# =========================================================================
# #2  a data_file with a NULL file_path (nullable union, null branch) crashed
#     both entry points via pstrdup(NULL) / ice_strip_scheme(NULL). It must now
#     be a clean DATA_CORRUPTED refusal; the control (file_path present) reads.
# =========================================================================
NP="$DEST/nullpath/db/t/metadata/t.metadata.json"
B="$(crashes)"
check "a NULL data-file path is refused in iceberg_data_files (XX001, not a crash)" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_data_files('$NP')" \
		'has no data file path' "no data file path")" "XX001 (no data file path)"
check "backend still up after the lister refusal" "$(survived "$B")" "1"
B="$(crashes)"
check "a NULL data-file path is refused in iceberg_scan (XX001, not a crash)" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_scan('$NP') AS t(id bigint)" \
		'has no data file path' "no data file path")" "XX001 (no data file path)"
check "backend still up after the scan refusal" "$(survived "$B")" "1"
check "control: a present file_path lists one data file" \
	"$(q "SELECT count(*) FROM pgcolumnar.iceberg_data_files('$DEST/nullpath_ctl/db/t/metadata/t.metadata.json')")" \
	"1"

# =========================================================================
# #691  a hostile manifest-list whose manifest_file.manifest_path is the Avro
#     union NULL branch reached ice_rebase -> ice_strip_scheme(NULL) and crashed
#     the backend (strncmp of NULL). The embedded manifest schema is
#     author-controlled, so this is reachable. Refuse it as DATA_CORRUPTED.
# =========================================================================
NMP="$DEST/nullmanifestpath/db/t/metadata/t.metadata.json"
B="$(crashes)"
check "a NULL manifest_path is refused, not a crash (XX001)" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_data_files('$NMP')" \
		'a manifest path in .* is null' "null manifest path")" "XX001 (null manifest path)"
check "backend still up after the null-manifest_path refusal" "$(survived "$B")" "1"

# =========================================================================
# #1  a non-regular file (FIFO) named on any Iceberg-reachable open must be
#     refused (XX001), never block the backend cancel-resistant. RED on main is
#     HANG (psql KILLed by the cap); GREEN is XX001, well under the cap.
# =========================================================================
FIFODIR="$PGC_WORKDIR/fifo"; mkdir -p "$FIFODIR"
FIFO="$FIFODIR/x"; mkfifo "$FIFO"

B="$(crashes)"
# Site A -- ice_slurp_text, FIFO as the metadata argument (three entry points)
check "iceberg_current_snapshot on a FIFO is refused, not a hang (XX001)" \
	"$(refusal_or_hang "SELECT * FROM pgcolumnar.iceberg_current_snapshot('$FIFO')")" \
	"XX001 (not a regular file)"; fifo_release "$FIFO"
check "iceberg_data_files on a FIFO is refused, not a hang (XX001)" \
	"$(refusal_or_hang "SELECT * FROM pgcolumnar.iceberg_data_files('$FIFO')")" \
	"XX001 (not a regular file)"; fifo_release "$FIFO"
check "iceberg_scan on a FIFO is refused, not a hang (XX001)" \
	"$(refusal_or_hang "SELECT * FROM pgcolumnar.iceberg_scan('$FIFO') AS t(id bigint)")" \
	"XX001 (not a regular file)"; fifo_release "$FIFO"

# Site C -- av_slurp_file, FIFO as the argument (both entry points)
check "read_avro_manifest on a FIFO is refused, not a hang (XX001)" \
	"$(refusal_or_hang "SELECT * FROM pgcolumnar.read_avro_manifest('$FIFO')")" \
	"XX001 (not a regular file)"; fifo_release "$FIFO"
check "read_manifest_list on a FIFO is refused, not a hang (XX001)" \
	"$(refusal_or_hang "SELECT * FROM pgcolumnar.read_manifest_list('$FIFO')")" \
	"XX001 (not a regular file)"; fifo_release "$FIFO"

# Site D -- pq_source_open_cfg via the single-file parquet API (sibling)
check "read_parquet on a FIFO is refused, not a hang (XX001)" \
	"$(refusal_or_hang "SELECT * FROM pgcolumnar.read_parquet('$FIFO') AS t(id bigint)")" \
	"XX001 (not a regular file)"; fifo_release "$FIFO"

check "backend still up after the FIFO refusals" "$(survived "$B")" "1"

# Site B -- ice_slurp_bin, FIFO named as the manifest-list INSIDE metadata.json.
# realpath() on the FIFO succeeds and passes ice_open_path's containment, so only
# the S_ISREG guard inside ice_slurp_bin stops it.
MLROOT="$FIFODIR/tbl"; mkdir -p "$MLROOT/db/t/metadata"
MLFIFO="$MLROOT/db/t/metadata/ml.avro"; mkfifo "$MLFIFO"
MDJSON="$MLROOT/db/t/metadata/t.metadata.json"
cat > "$MDJSON" <<JSON
{"format-version":2,"location":"file://$MLROOT/db/t",
 "current-snapshot-id":4242,"current-schema-id":0,
 "schemas":[{"schema-id":0,"type":"struct","fields":[]}],
 "snapshots":[{"snapshot-id":4242,"timestamp-ms":0,"sequence-number":1,
   "schema-id":0,"manifest-list":"file://$MLROOT/db/t/metadata/ml.avro",
   "summary":{"operation":"append"}}]}
JSON
B="$(crashes)"
check "a manifest-list that is a FIFO is refused, not a hang (XX001)" \
	"$(refusal_or_hang "SELECT * FROM pgcolumnar.iceberg_data_files('$MDJSON')")" \
	"XX001 (not a regular file)"; fifo_release "$MLFIFO"
check "backend still up after the manifest-list FIFO refusal" "$(survived "$B")" "1"

# =========================================================================
# #4  a manifest_file.sequence_number that is the union's NULL branch decoded
#     silently as 0. A v2/v3 list always carries a concrete number, so a null is
#     corrupt: it understates a data file's seq and mis-applies an older delete
#     -> a dropped row. Refuse it; keep v1 (absent column) -> 0.
# =========================================================================
NSML="$DEST/nullseq_ml"
check "a null manifest-list sequence_number decodes as NULL, not 0" \
	"$(q "SELECT sequence_number IS NULL FROM pgcolumnar.read_manifest_list('$NSML/ml.avro')")" \
	"t"
check "control: a present manifest-list sequence_number decodes to its value" \
	"$(q "SELECT sequence_number FROM pgcolumnar.read_manifest_list('$NSML/ml_ctl.avro')")" \
	"5"
# #686/#691: a null min_sequence_number should read as SQL NULL, not 0 (cosmetic).
NMML="$DEST/nullminseq_ml"
check "a null manifest-list min_sequence_number decodes as NULL, not 0 (#686)" \
	"$(q "SELECT min_sequence_number IS NULL FROM pgcolumnar.read_manifest_list('$NMML/ml.avro')")" \
	"t"
check "control: a present min_sequence_number decodes to its value (#686)" \
	"$(q "SELECT min_sequence_number FROM pgcolumnar.read_manifest_list('$NMML/ml_ctl.avro')")" \
	"7"

NS="$DEST/nullseq/db/t/metadata/t.metadata.json"
NSC="$DEST/nullseq_ctl/db/t/metadata/t.metadata.json"
B="$(crashes)"
check "iceberg_scan refuses a null manifest seq an ADDED entry must inherit (XX001, not a silent wrong row)" \
	"$(refusal "SELECT count(*) FROM pgcolumnar.iceberg_scan('$NS') AS t(id bigint, region text, amount int)" \
		'has a null sequence number that an added entry' "null sequence to inherit")" \
	"XX001 (null sequence to inherit)"
check "backend still up after the null-sequence refusal" "$(survived "$B")" "1"
check "control: a concrete manifest seq scans all 5 rows (seq-3 delete is older than seq-5 data)" \
	"$(q "SELECT count(*) FROM pgcolumnar.iceberg_scan('$NSC') AS t(id bigint, region text, amount int)")" \
	"5"

# =========================================================================
# #8  a dangling current-schema-id (names a schema absent from "schemas")
#     must be REFUSED as DATA_CORRUPTED, not silently resolved to the
#     deprecated top-level "schema" (which binds columns through a stale
#     schema and misprojects every row). Asymmetric with ice_current_snapshot.
# =========================================================================
DS="$DEST/danglingschema/db/t/metadata/t.metadata.json"
B="$(crashes)"
check "a dangling current-schema-id is refused (XX001), not silently stale-read" \
	"$(refusal "SELECT amount FROM pgcolumnar.iceberg_scan('$DS') AS t(amount int)" \
		'is not present in the schemas array' "dangling schema id")" \
	"XX001 (dangling schema id)"
check "backend still up after the dangling-schema refusal" "$(survived "$B")" "1"
# control: the SAME data with a RESOLVABLE current-schema-id reads the correct
# column (amount = field id 3 = 10..50). Proves only the dangling id flips it.
DSC="$DEST/danglingschema_ctl/db/t/metadata/t.metadata.json"
check "control: a resolvable current-schema-id reads the correct amount column" \
	"$(q "SELECT string_agg(amount::text, ',' ORDER BY amount)
	      FROM pgcolumnar.iceberg_scan('$DSC') AS t(amount int)")" \
	"10,20,30,40,50"
# green control: a legit legacy-only table (top-level "schema", no "schemas"
# array, no current-schema-id) must STILL read -- the fix must not reject it.
LS="$DEST/legacyschema/db/t/metadata/t.metadata.json"
check "control: a legacy-only-schema table still reads (5 rows)" \
	"$(q "SELECT count(*) FROM pgcolumnar.iceberg_scan('$LS')
	      AS t(id bigint, region text, amount int)")" "5"
check "control: the legacy table binds amount correctly (10..50)" \
	"$(q "SELECT string_agg(amount::text, ',' ORDER BY amount)
	      FROM pgcolumnar.iceberg_scan('$LS') AS t(id bigint, region text, amount int)")" \
	"10,20,30,40,50"


# =========================================================================
# #1824  malformed numbers in metadata.json were read, not refused. A field id
#     above INT32 truncated to another field's id (4294967297 -> 1), a fraction
#     rounded (1.4 -> 1), so "amount" returned the "id" column. format-version 99
#     read as v2, and a string current-snapshot-id read as an empty table. The
#     variants are edited copies of the db/events fixture (id 1, region 2,
#     amount 3), written next to the original so they read its data files.
# =========================================================================
LAX="$PGC_WORKDIR/lax"
rm -rf "$LAX"; mkdir -p "$LAX"
cp -r "$FX/warehouse/db" "$LAX/"; chmod -R u+rwX "$LAX"
LAXMD="$LAX/db/events/metadata"
LAXSRC="$LAXMD/00001-6808fe8c-acd3-4fbf-9fbe-1ac447a16b3c.metadata.json"
python3 - "$LAXSRC" <<'PYEOF'
import json, os, sys
src = sys.argv[1]; d = os.path.dirname(src)
def variant(name, edit):
    m = json.load(open(src)); edit(m)
    json.dump(m, open(os.path.join(d, name + ".metadata.json"), "w"))
def amount_id(v):
    def edit(m):
        for s in m["schemas"]:
            for f in s["fields"]:
                if f["name"] == "amount": f["id"] = v
    return edit
variant("bigid", amount_id(4294967297))   # 2^32 + 1 truncates to 1, the id field
variant("fracid", amount_id(1.4))          # rounds to 1
variant("fv99", lambda m: m.__setitem__("format-version", 99))
variant("csidx", lambda m: m.__setitem__("current-snapshot-id", "x"))
variant("csidneg", lambda m: m.__setitem__("current-snapshot-id", -1))
variant("fv4", lambda m: m.__setitem__("format-version", 4))   # the bounds of 1..3
variant("fv0", lambda m: m.__setitem__("format-version", 0))
variant("csidnull", lambda m: m.__setitem__("current-snapshot-id", None))
PYEOF
FIDRANGE='schema field "amount" has a field id out of range: 4294967297'
NONINT='holds the non-integer number 1.4 where an integer is required'
check "control: the unedited events metadata reads amount 10..50" \
	"$(q "SELECT string_agg(amount::text, ',' ORDER BY id) FROM pgcolumnar.iceberg_scan('$LAXSRC') AS t(id bigint, amount int)")" \
	"10,20,30,40,50"
B="$(crashes)"
check "a field id above INT32 is refused (XX001), not aliased to another field" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_scan('$LAXMD/bigid.metadata.json') AS t(amount bigint)" \
		"$FIDRANGE" "field id out of range")" "XX001 (field id out of range)"
check "a field id above INT32 is named even when its alias is also queried" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_scan('$LAXMD/bigid.metadata.json') AS t(id bigint, amount bigint)" \
		"$FIDRANGE" "field id out of range")" "XX001 (field id out of range)"
check "a fractional field id is refused (XX001), not rounded" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_scan('$LAXMD/fracid.metadata.json') AS t(amount bigint)" \
		"$NONINT" "non-integer number")" "XX001 (non-integer number)"
check "format-version 99 is refused (0A000), not read as v2" \
	"$(refusal "SELECT count(*) FROM pgcolumnar.iceberg_scan('$LAXMD/fv99.metadata.json') AS t(id bigint)" \
		'has an unsupported format-version 99' "unsupported format-version")" "0A000 (unsupported format-version)"
check "format-version 4 is refused (0A000), one above the newest known" \
	"$(refusal "SELECT count(*) FROM pgcolumnar.iceberg_scan('$LAXMD/fv4.metadata.json') AS t(id bigint)" \
		'has an unsupported format-version 4' "unsupported format-version")" "0A000 (unsupported format-version)"
check "format-version 0 is refused (0A000), one below the oldest known" \
	"$(refusal "SELECT count(*) FROM pgcolumnar.iceberg_scan('$LAXMD/fv0.metadata.json') AS t(id bigint)" \
		'has an unsupported format-version 0' "unsupported format-version")" "0A000 (unsupported format-version)"
check "a string current-snapshot-id is refused (XX001), not read as an empty table" \
	"$(refusal "SELECT count(*) FROM pgcolumnar.iceberg_scan('$LAXMD/csidx.metadata.json') AS t(id bigint)" \
		'has a current-snapshot-id that is not an integer' "non-integer current-snapshot-id")" \
	"XX001 (non-integer current-snapshot-id)"
check "backend still up after the malformed-number refusals" "$(survived "$B")" "1"
check "control: current-snapshot-id -1 still reads as a table with no snapshot (0 rows)" \
	"$(q "SELECT count(*) FROM pgcolumnar.iceberg_scan('$LAXMD/csidneg.metadata.json') AS t(id bigint)")" "0"
check "control: a null current-snapshot-id still reads as a table with no snapshot (0 rows)" \
	"$(q "SELECT count(*) FROM pgcolumnar.iceberg_scan('$LAXMD/csidnull.metadata.json') AS t(id bigint)")" "0"

# =========================================================================
# #1823  manifest enum fields outside the spec. data_file.content may be only
#     0 (data), 1 (position deletes) or 2 (equality deletes); any other value was
#     sorted as an equality delete. On the OLDEST data file that delete was then
#     skipped as never applicable, so the file's five rows vanished with no error;
#     on the newest the scan failed with a misleading "has no equality_ids". The
#     lister called it a delete file (0A000). manifest_entry.status (0..2) and
#     manifest_file.content (0..1) were not range-checked either. Each variant is
#     enum_ctl with ONE value changed, and each refusal must name the value.
# =========================================================================
md_of() { echo "$DEST/$1/db/t/metadata/t.metadata.json"; }
EN="AS t(id bigint, region text, amount int)"
check "control: two valid data entries scan as ten rows (ids 1..10)" \
	"$(q "SELECT count(*) || '|' || min(id) || '|' || max(id) FROM pgcolumnar.iceberg_scan('$(md_of enum_ctl)') $EN")" \
	"10|1|10"
check "control: two valid data entries list two data files" \
	"$(q "SELECT count(*) FROM pgcolumnar.iceberg_data_files('$(md_of enum_ctl)')")" "2"
B="$(crashes)"
check "data_file.content 3 on the oldest file is refused by iceberg_scan (XX001), not read as 5 rows" \
	"$(refusal "SELECT count(*) FROM pgcolumnar.iceberg_scan('$(md_of content3_old)') $EN" \
		'manifest entry ".*/old\.parquet" has invalid content 3$' "invalid content 3")" \
	"XX001 (invalid content 3)"
check "data_file.content -1 on the oldest file is refused by iceberg_scan (XX001), not read as 5 rows" \
	"$(refusal "SELECT count(*) FROM pgcolumnar.iceberg_scan('$(md_of contentm1_old)') $EN" \
		'manifest entry ".*/old\.parquet" has invalid content -1$' "invalid content -1")" \
	"XX001 (invalid content -1)"
check "data_file.content 3 on the newest file is refused naming the value, not as an equality delete with no ids" \
	"$(refusal "SELECT count(*) FROM pgcolumnar.iceberg_scan('$(md_of content3_new)') $EN" \
		'manifest entry ".*/new\.parquet" has invalid content 3$' "invalid content 3")" \
	"XX001 (invalid content 3)"
check "iceberg_data_files refuses data_file.content 3 as corrupt (XX001), not as a delete file (0A000)" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_data_files('$(md_of content3_old)')" \
		'manifest entry ".*/old\.parquet" has invalid content 3$' "invalid content 3")" \
	"XX001 (invalid content 3)"
check "manifest_entry.status 3 is refused by iceberg_scan (XX001), not read as a live entry" \
	"$(refusal "SELECT count(*) FROM pgcolumnar.iceberg_scan('$(md_of status3_old)') $EN" \
		'manifest entry ".*/old\.parquet" has invalid status 3$' "invalid status 3")" \
	"XX001 (invalid status 3)"
check "iceberg_data_files refuses manifest_entry.status 3 (XX001), not listed" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_data_files('$(md_of status3_old)')" \
		'manifest entry ".*/old\.parquet" has invalid status 3$' "invalid status 3")" \
	"XX001 (invalid status 3)"
check "manifest_file.content 5 is refused by iceberg_scan (XX001), not read as a data manifest" \
	"$(refusal "SELECT count(*) FROM pgcolumnar.iceberg_scan('$(md_of mfcontent5)') $EN" \
		'manifest ".*/dm\.avro" has invalid content 5$' "invalid manifest content 5")" \
	"XX001 (invalid manifest content 5)"
check "iceberg_data_files refuses manifest_file.content 5 as corrupt (XX001), not as a delete manifest (0A000)" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_data_files('$(md_of mfcontent5)')" \
		'manifest ".*/dm\.avro" has invalid content 5$' "invalid manifest content 5")" \
	"XX001 (invalid manifest content 5)"
# A value outside int32 was cast before that range check, so 2^32 wrapped to 0
# and 2^32+1 to 1: valid values, read as live data with no error. The decoder
# refuses it before the cast; no path is decoded yet for an entry's enum.
check "data_file.content 4294967296 on the oldest file is refused by iceberg_scan (XX001), not wrapped to 0" \
	"$(refusal "SELECT count(*) FROM pgcolumnar.iceberg_scan('$(md_of content4g_old)') $EN" \
		'manifest entry has invalid content 4294967296$' "invalid content 4294967296")" \
	"XX001 (invalid content 4294967296)"
check "manifest_entry.status 4294967297 is refused by iceberg_scan (XX001), not wrapped to 1" \
	"$(refusal "SELECT count(*) FROM pgcolumnar.iceberg_scan('$(md_of status4g_old)') $EN" \
		'manifest entry has invalid status 4294967297$' "invalid status 4294967297")" \
	"XX001 (invalid status 4294967297)"
check "manifest_file.content 4294967296 is refused by iceberg_scan (XX001), not wrapped to 0" \
	"$(refusal "SELECT count(*) FROM pgcolumnar.iceberg_scan('$(md_of mfcontent4g)') $EN" \
		'manifest ".*/dm\.avro" has invalid content 4294967296$' "invalid manifest content 4294967296")" \
	"XX001 (invalid manifest content 4294967296)"
# #1878: manifest_file.partition_spec_id had the same plain cast, so 2^32 read as
# spec 0 and the manifest was pruned with spec 0's quals. Its path is decoded
# first, so the refusal names the manifest as well as the value.
SPEC4G='manifest ".*/dm\.avro" has invalid partition_spec_id 4294967296$'
check "manifest_file.partition_spec_id 4294967296 is refused by iceberg_scan (XX001), not read as spec 0" \
	"$(refusal "SELECT count(*) FROM pgcolumnar.iceberg_scan('$(md_of specid4g)') $EN" \
		"$SPEC4G" "invalid partition_spec_id 4294967296")" \
	"XX001 (invalid partition_spec_id 4294967296)"
check "read_manifest_list refuses partition_spec_id 4294967296 (XX001), not shown as 0" \
	"$(refusal "SELECT * FROM pgcolumnar.read_manifest_list('$DEST/specid4g/db/t/metadata/ml.avro')" \
		"$SPEC4G" "invalid partition_spec_id 4294967296")" \
	"XX001 (invalid partition_spec_id 4294967296)"
check "backend still up after the enum refusals" "$(survived "$B")" "1"

# =========================================================================
# #1853  metadata.json ids that must fit int32. A snapshot's schema-id and the
#     table's default-spec-id were read as 64-bit numbers and cast to int32 with
#     no range check, so 4294967296 became 0 and -2147483649 became 2147483647.
#     iceberg_current_snapshot reported the wrong schema_id; the foreign table
#     scan took the wrong current spec id. Each variant is enum_ctl's metadata
#     with ONE id changed, and each refusal must name the value.
# =========================================================================
IDS="$DEST/enum_ctl/db/t/metadata"
ids_md() {	# ids_md TAG KEY VALUE -> path of enum_ctl's metadata with KEY set to VALUE
	# (VALUE "absent" deletes KEY)
	python3 - "$IDS/t.metadata.json" "$IDS/ids_$1.metadata.json" "$2" "$3" <<'PY'
import json, sys
src, dst, key, val = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
m = json.load(open(src))
m["partition-specs"] = [{"spec-id": 0, "fields": []}]
m["default-spec-id"] = 0
if val == "absent":
    del m[key]
elif key == "schema-id":
    m["snapshots"][0]["schema-id"] = int(val)
else:
    m[key] = int(val)
json.dump(m, open(dst, "w"))
PY
	echo "$IDS/ids_$1.metadata.json"
}
SID_MAX="$(ids_md sidmax schema-id 2147483647)"
SID_4G="$(ids_md sid4g schema-id 4294967296)"
SID_NEG="$(ids_md sidneg schema-id -2147483649)"
SID_MIN="$(ids_md sidmin schema-id -2147483648)"
DS_0="$(ids_md ds0 default-spec-id 0)"
DS_4G="$(ids_md ds4g default-spec-id 4294967296)"
DS_NEG="$(ids_md dsneg default-spec-id -2147483649)"
DS_MAX="$(ids_md dsmax default-spec-id 2147483647)"
DS_NONE="$(ids_md dsnone default-spec-id absent)"
q "CREATE SERVER ice_ids FOREIGN DATA WRAPPER pgcolumnar_iceberg" >/dev/null
for t in ds0 ds4g dsneg dsmax dsnone; do
	q "CREATE FOREIGN TABLE ids_$t (id bigint, region text, amount int)
	   SERVER ice_ids OPTIONS (metadata_path '$IDS/ids_$t.metadata.json')" >/dev/null
done
check "control: a snapshot schema-id of 2147483647 reads as 2147483647" \
	"$(q "SELECT schema_id FROM pgcolumnar.iceberg_current_snapshot('$SID_MAX')")" \
	"2147483647"
B="$(crashes)"
check "a snapshot schema-id of 4294967296 is refused by iceberg_current_snapshot (XX001), not truncated to 0" \
	"$(refusal "SELECT schema_id FROM pgcolumnar.iceberg_current_snapshot('$SID_4G')" \
		'snapshot 4242 in ".*" has a schema-id out of range: 4294967296$' "schema-id 4294967296")" \
	"XX001 (schema-id 4294967296)"
check "a snapshot schema-id of -2147483649 is refused by iceberg_current_snapshot (XX001), not truncated to 2147483647" \
	"$(refusal "SELECT schema_id FROM pgcolumnar.iceberg_current_snapshot('$SID_NEG')" \
		'snapshot 4242 in ".*" has a schema-id out of range: -2147483649$' "schema-id -2147483649")" \
	"XX001 (schema-id -2147483649)"
check "control: a foreign table with default-spec-id 0 scans ten rows" \
	"$(q "SELECT count(*) FROM ids_ds0")" "10"
check "a default-spec-id of 4294967296 is refused by the Iceberg foreign table scan (XX001), not truncated to 0" \
	"$(refusal "SELECT count(*) FROM ids_ds4g" \
		'".*" has a default-spec-id out of range: 4294967296$' "default-spec-id 4294967296")" \
	"XX001 (default-spec-id 4294967296)"
check "a default-spec-id of -2147483649 is refused by the Iceberg foreign table scan (XX001), not truncated to 2147483647" \
	"$(refusal "SELECT count(*) FROM ids_dsneg" \
		'".*" has a default-spec-id out of range: -2147483649$' "default-spec-id -2147483649")" \
	"XX001 (default-spec-id -2147483649)"
# The controls at the other edges: INT32_MIN and INT32_MAX are valid ids, and a
# table with no default-spec-id (a v1 table from an older writer) reads as spec 0.
check "control: a snapshot schema-id of -2147483648 reads as -2147483648" \
	"$(q "SELECT schema_id FROM pgcolumnar.iceberg_current_snapshot('$SID_MIN')")" \
	"-2147483648"
check "control: a foreign table with default-spec-id 2147483647 scans ten rows" \
	"$(q "SELECT count(*) FROM ids_dsmax")" "10"
check "control: a foreign table with no default-spec-id scans ten rows" \
	"$(q "SELECT count(*) FROM ids_dsnone")" "10"
check "backend still up after the int32 id refusals" "$(survived "$B")" "1"

pgc_summary
