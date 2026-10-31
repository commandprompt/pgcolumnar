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
# matches REGEX, "<SQLSTATE>: <message>" for any other error, or "no error". Every
# arm here refuses with XX001, and so do later parse steps: a FIFO read as an empty
# file failed a parse with the same XX001, so the SQLSTATE alone passed with the
# guard removed. The arm now names the refusal it wants.
refusal_of() {	# stdin: psql output -> the refusal reading
	local re="$1" label="$2" line state msg
	local out; out="$(cat)"
	line="$(sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\): /\1|/p' <<<"$out" | head -1)"
	if [ -z "$line" ]; then
		grep -qE 'server closed the connection|connection to server' <<<"$out" \
			&& echo "no refusal: the connection was lost" || echo "no error"
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

pgc_summary
