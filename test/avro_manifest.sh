#!/usr/bin/env bash
#
# pgColumnar Avro manifest decoder (#388 step 1). Decodes a real Apache Iceberg
# manifest -- one produced by pyiceberg, an independent writer, committed under
# test/fixtures/iceberg/ with an oracle in expected.json -- and asserts the
# decoded data-file entries against that oracle. This is the same shape as the
# pyarrow cross-check in the Parquet suites: an independent writer is the oracle,
# so a green check proves the C decoder and a real Iceberg writer agree, not that
# the decoder agrees with our own encoder.
#
# Usage:  test/avro_manifest.sh [PG_CONFIG]

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

sqlstate_of() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -qtA 2>&1 <<SQLEOF | sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\).*/\1/p' | head -1
\\set VERBOSITY sqlstate
$1;
SQLEOF
}

# Backends killed by a signal, from the server log: a number, or nothing when the
# log can't be read. A fresh connection answers again after crash recovery, so
# "SELECT 1" alone passed after a backend died on a bad file; the survival checks
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
[ -f "$FX/manifest-0.avro" ] || pgc_skip fixture "iceberg manifest fixtures are missing"
python3 -c 'import json' 2>/dev/null || pgc_skip python "python3 is needed to read the oracle"

# copy the fixture where the postgres backend can read it
M0="$PGC_WORKDIR/manifest-0.avro"
cp "$FX/manifest-0.avro" "$M0"
chmod 644 "$M0"

# ---- decode: entry count matches the oracle --------------------------------
EXP_N="$(python3 -c "import json;print(len(json.load(open('$FX/expected.json'))['entries']))")"
check "decoded entry count == oracle" \
	"$(q "SELECT count(*) FROM pgcolumnar.read_avro_manifest('$M0')")" "$EXP_N"

# ---- per-entry: record_count, file_size, partition, file basename ----------
# The oracle rows, rendered as "record_count|file_size|region", sorted.
ORACLE="$(python3 - "$FX/expected.json" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
rows = []
for e in r["entries"]:
    rows.append("%d|%d|%s" % (e["record_count"], e["file_size_in_bytes"], e["partition"]["region"]))
print("\n".join(sorted(rows)))
PY
)"
DECODED="$(q "SELECT record_count || '|' || file_size_in_bytes || '|' ||
                     split_part(partition, '=', 2)
              FROM pgcolumnar.read_avro_manifest('$M0') ORDER BY 1")"
check_text "decoded entries == oracle (record_count|file_size|region)" \
	"$DECODED" "$ORACLE"

# the partition is rendered name=value; confirm the field name came from the
# embedded schema, not a hardcoded label
check "partition renders the schema's field name" \
	"$(q "SELECT bool_and(partition LIKE 'region=%') FROM pgcolumnar.read_avro_manifest('$M0')")" "t"

# every entry names a real parquet data file (file_path decoded from the string)
check "every entry's file_path ends in .parquet" \
	"$(q "SELECT bool_and(file_path LIKE '%.parquet') FROM pgcolumnar.read_avro_manifest('$M0')")" "t"
check "all entries are ADDED data files (status=1, content=0)" \
	"$(q "SELECT bool_and(status = 1 AND content = 0) FROM pgcolumnar.read_avro_manifest('$M0')")" "t"

# ---- the entry's data sequence number (#388 phase 4) ------------------------
# The committed manifest was a first append: its entries carry a NULL
# sequence_number (inherited from the manifest's, per the v2 spec).
# Against the oracle's count: the expected side was read from the same call, so a
# failed read compared empty with empty and an empty one 0/0 with 0/0.
check "the committed entries have a NULL (inherited) sequence_number" \
	"$(q "SELECT count(*) FILTER (WHERE sequence_number IS NULL) || '/' || count(*)
	      FROM pgcolumnar.read_avro_manifest('$M0')")" \
	"$EXP_N/$EXP_N"
# A crafted manifest_entry with an EXPLICIT sequence_number = 42, so the decode of
# a present (non-null) value is proven, not just the null case. Reduced schema
# (the decoder projects by field name and skips the rest).
python3 - "$PGC_WORKDIR/seq.avro" <<'PY'
import sys
def zz(n):
    u = (n << 1) ^ (n >> 63) if n < 0 else (n << 1)
    out = bytearray()
    while True:
        b = u & 0x7f; u >>= 7
        out.append(b | 0x80 if u else b)
        if not u: break
    return bytes(out)
def s(bs): return zz(len(bs)) + bs
schema = (b'{"type":"record","name":"manifest_entry","fields":['
          b'{"name":"status","type":"int"},'
          b'{"name":"sequence_number","type":["null","long"]},'
          b'{"name":"data_file","type":{"type":"record","name":"df","fields":['
          b'{"name":"content","type":"int"},'
          b'{"name":"file_path","type":"string"},'
          b'{"name":"file_format","type":"string"},'
          b'{"name":"record_count","type":"long"},'
          b'{"name":"file_size_in_bytes","type":"long"}]}}]}')
# one entry: status=1, sequence_number=union(1)->42, data_file{0,"d.parquet","PARQUET",5,100}
rec  = zz(1) + zz(1) + zz(42)
rec += zz(0) + s(b"d.parquet") + s(b"PARQUET") + zz(5) + zz(100)
sync = b"\x00" * 16
meta = zz(2) + s(b"avro.schema") + s(schema) + s(b"avro.codec") + s(b"null") + zz(0)
ocf  = b"Obj\x01" + meta + sync + zz(1) + zz(len(rec)) + rec + sync
open(sys.argv[1], "wb").write(ocf)
PY
chmod 644 "$PGC_WORKDIR/seq.avro"
check "an explicit entry sequence_number is decoded (42)" \
	"$(q "SELECT sequence_number FROM pgcolumnar.read_avro_manifest('$PGC_WORKDIR/seq.avro')")" "42"

# ---- the manifest LIST (a snapshot's manifest_file entries) -----------------
# The same object-container machinery, a different embedded record. Decode the
# committed real manifest-list and assert its manifest_file entry against the
# writer's oracle (expected_list.json).
ML="$PGC_WORKDIR/manifest-list.avro"
cp "$FX/manifest-list.avro" "$ML"; chmod 644 "$ML"
# every scalar the manifest_file record carries, so the arm is not vacuous on the
# columns the decoder fills to zero for this fixture (min_sequence_number,
# existing/deleted rows). Field order matches the SELECT below.
LORACLE="$(python3 - "$FX/expected_list.json" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))["manifest_files"]
r = m[0]
print("%d|%d|%d|%d|%d|%d|%d|%d|%d|%d|%d|%s" % (
      r["manifest_length"], r["content"], r["partition_spec_id"],
      r["added_files_count"], r["existing_files_count"], r["deleted_files_count"],
      r["added_rows_count"], r["existing_rows_count"], r["deleted_rows_count"],
      r["sequence_number"], r["min_sequence_number"], r["added_snapshot_id"]))
PY
)"
# the manifest_path the snapshot points at, matched by basename against the oracle
# (the fixture stores an absolute file:// path the writer chose; only the basename
# is stable across warehouses)
LPATH="$(python3 -c "import json;print(json.load(open('$FX/expected_list.json'))['manifest_files'][0]['manifest_path_basename'])")"
check "manifest-list entry count == oracle (1)" \
	"$(q "SELECT count(*) FROM pgcolumnar.read_manifest_list('$ML')")" "1"
check_text "manifest-list entry == oracle (all scalar fields)" \
	"$(q "SELECT manifest_length || '|' || content || '|' || partition_spec_id || '|' ||
	             added_files_count || '|' || existing_files_count || '|' ||
	             deleted_files_count || '|' || added_rows_count || '|' ||
	             existing_rows_count || '|' || deleted_rows_count || '|' ||
	             sequence_number || '|' || min_sequence_number || '|' || added_snapshot_id
	      FROM pgcolumnar.read_manifest_list('$ML')")" "$LORACLE"
check_text "manifest-list manifest_path basename == oracle" \
	"$(q "SELECT (regexp_match(manifest_path, '[^/]+\$'))[1]
	      FROM pgcolumnar.read_manifest_list('$ML')")" "$LPATH"

# ---- privilege: needs pg_read_server_files ---------------------------------
# USAGE on the schema, so the refusal is the function's own: without it the role
# was refused 42501 for the schema, before the pg_read_server_files gate, and with
# that gate removed this still passed. The gate's message is read too.
psql_run "DROP ROLE IF EXISTS avro_unpriv; CREATE ROLE avro_unpriv LOGIN; GRANT USAGE ON SCHEMA pgcolumnar TO avro_unpriv;"
unpriv_out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U avro_unpriv -d "$PGC_DB" \
		-qtA -c "\\set VERBOSITY verbose" -c "SELECT count(*) FROM pgcolumnar.read_avro_manifest('$M0');" 2>&1)"
check "an unprivileged role is refused (42501)" \
	"$(sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\).*/\1/p' <<<"$unpriv_out" | head -1)$(grep -q 'pg_read_server_files' <<<"$unpriv_out" && echo ' by the pg_read_server_files gate')" \
	"42501 by the pg_read_server_files gate"

# ---- a non-Avro file is refused cleanly, not crashed -----------------------
CRASHES_BEFORE="$(crashes)"
printf 'this is not avro at all, not even close, just some bytes' > "$PGC_WORKDIR/notavro.bin"
chmod 644 "$PGC_WORKDIR/notavro.bin"
# The magic is read, not inferred from the SQLSTATE: the file is garbage throughout,
# so with the magic check removed a later parse still raised XX001 and this passed.
badmagic_out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-qtA -c "\\set VERBOSITY verbose" -c "SELECT * FROM pgcolumnar.read_avro_manifest('$PGC_WORKDIR/notavro.bin');" 2>&1)"
check "a non-Avro file is rejected (bad magic), backend survives" \
	"$(sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\).*/\1/p' <<<"$badmagic_out" | head -1)$(grep -q 'bad magic' <<<"$badmagic_out" && echo ' (bad magic)')" \
	"XX001 (bad magic)"
check "backend still up after the bad file" "$(survived "$CRASHES_BEFORE")" "1"

# the file_format decodes to the bare Iceberg enum name (matches the normalized
# oracle, not pyiceberg's Python repr)
check "file_format decodes to the Iceberg enum name" \
	"$(q "SELECT bool_and(file_format = 'PARQUET') FROM pgcolumnar.read_avro_manifest('$M0')")" "t"

# ---- deflate zip-bomb: a crafted manifest must not OOM the backend ----------
# #633 review: count=0 blocks decode no entries yet still inflate, so they bypass
# the per-entry cap; without the per-block free + cumulative decompressed cap a
# small file inflates to hundreds of GB. Craft a minimal valid OCF whose deflate
# blocks inflate past the 1 GB cumulative cap, and assert it is refused
# (program_limit_exceeded, 54000), not OOMed. Each block's buffer is freed as it
# goes, so peak memory stays one block.
python3 - "$PGC_WORKDIR/bomb.avro" <<'PY'
import sys, zlib
def varint(u):
    out = bytearray()
    while True:
        b = u & 0x7f; u >>= 7
        out.append(b | 0x80 if u else b)
        if not u: break
    return bytes(out)
def zz(n): return varint(((n << 1) ^ (n >> 63)) & 0xFFFFFFFFFFFFFFFF)
sync = b'\x00' * 16
schema = b'{"type":"record","name":"x","fields":[]}'
meta = zz(2)
for k, v in ((b'avro.schema', schema), (b'avro.codec', b'deflate')):
    meta += zz(len(k)) + k + zz(len(v)) + v
meta += zz(0)
out = b'Obj\x01' + meta + sync
co = zlib.compressobj(9, zlib.DEFLATED, -15)          # raw deflate
raw = co.compress(b'\x00' * (128 * 1024 * 1024)) + co.flush()
for _ in range(9):                                    # 9 x 128 MiB > 1 GiB cap
    out += zz(0) + zz(len(raw)) + raw + sync
open(sys.argv[1], 'wb').write(out)
PY
chmod 644 "$PGC_WORKDIR/bomb.avro"
check "a deflate zip-bomb is refused by the cumulative cap (54000)" \
	"$(sqlstate_of "SET statement_timeout='60s'; SELECT count(*) FROM pgcolumnar.read_avro_manifest('$PGC_WORKDIR/bomb.avro')")" "54000"
check "backend still up after the zip-bomb" "$(survived "$CRASHES_BEFORE")" "1"

# ---- #1913: a remote URL is refused up front, not opened as a local file -----
# read_avro_manifest and read_manifest_list read local files only. Before #1913
# an s3:// or http:// URL went to open(2) and failed with 58P01 ("could not open
# file "s3://..." for reading"), which says the object does not exist. They now
# refuse every scheme PgColumnarPathIsRemote recognises with 0A000, naming the
# function and the URL, with a hint that it reads local files only -- the
# refusal #1886 gave import_arrow. Each refusal is read whole: SQLSTATE, message
# and hint. The refusal comes after the pg_read_server_files gate, so a role
# without it gets 42501 for a URL as for a local path. The controls hold the
# other side: a LOCAL path that carries "s3://" past its first byte is still read.
echo "-- remote URL refusal"
remote_refusal() {	# role SQL -> "SQLSTATE message | hint" of its error, or "no error (...)"
	local out msg hint
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U "$1" -d "$PGC_DB" \
		-X -At -v ON_ERROR_STOP=1 -v VERBOSITY=verbose -c "$2" 2>&1)"
	case "$out" in
		*ERROR:*)
			msg="$(sed -n '/^ERROR:/{s/^ERROR: *\([0-9A-Z]\{5\}\): \(.*\)$/\1 \2/p;q;}' <<<"$out")"
			hint="$(sed -n '/^HINT:/{s/^HINT: *//p;q;}' <<<"$out")"
			printf '%s | %s\n' "$msg" "${hint:-no hint}";;
		*) printf 'no error (%s)\n' "$out";;
	esac
}
want_refusal() {	# function URL -> the refusal #1913 promises
	printf '0A000 columnar: %s does not read "%s" | %s reads local files only. Use a local filesystem path.\n' "$1" "$2" "$1"
}
REMOTE_S3="s3://pgc-test/f1913/m.avro"
REMOTE_HTTP="http://127.0.0.1:9/f1913/m.avro"
REMOTE_OTHER="https://127.0.0.1:9/f1913/m.avro gs://pgc-test/f1913/m.avro az://pgc-test/f1913/m.avro S3://pgc-test/f1913/m.avro"
GATE_REFUSAL="42501 must be superuser or a member of the pg_read_server_files role to read a server file | no hint"
for fn in read_avro_manifest read_manifest_list; do
	other_refusals="" other_wants=""
	for u in $REMOTE_OTHER; do
		other_refusals="$other_refusals${other_refusals:+ ; }$(remote_refusal postgres "SELECT count(*) FROM pgcolumnar.$fn('$u')")"
		other_wants="$other_wants${other_wants:+ ; }$(want_refusal "$fn" "$u")"
	done
	printf -v "OTHER_GOT_$fn" '%s' "$other_refusals"
	printf -v "OTHER_WANT_$fn" '%s' "$other_wants"
done
check "read_avro_manifest refuses an s3:// URL with 0A000, naming itself and the URL (#1913)" \
	"$(remote_refusal postgres "SELECT count(*) FROM pgcolumnar.read_avro_manifest('$REMOTE_S3')")" \
	"$(want_refusal read_avro_manifest "$REMOTE_S3")"
check "read_avro_manifest refuses an http:// URL with 0A000, naming itself and the URL (#1913)" \
	"$(remote_refusal postgres "SELECT count(*) FROM pgcolumnar.read_avro_manifest('$REMOTE_HTTP')")" \
	"$(want_refusal read_avro_manifest "$REMOTE_HTTP")"
check "read_avro_manifest refuses https://, gs://, az:// and S3:// URLs with 0A000 too (#1913)" \
	"$OTHER_GOT_read_avro_manifest" "$OTHER_WANT_read_avro_manifest"
check "read_manifest_list refuses an s3:// URL with 0A000, naming itself and the URL (#1913)" \
	"$(remote_refusal postgres "SELECT count(*) FROM pgcolumnar.read_manifest_list('$REMOTE_S3')")" \
	"$(want_refusal read_manifest_list "$REMOTE_S3")"
check "read_manifest_list refuses an http:// URL with 0A000, naming itself and the URL (#1913)" \
	"$(remote_refusal postgres "SELECT count(*) FROM pgcolumnar.read_manifest_list('$REMOTE_HTTP')")" \
	"$(want_refusal read_manifest_list "$REMOTE_HTTP")"
check "read_manifest_list refuses https://, gs://, az:// and S3:// URLs with 0A000 too (#1913)" \
	"$OTHER_GOT_read_manifest_list" "$OTHER_WANT_read_manifest_list"

# The gate first: avro_unpriv (above) holds USAGE on the schema and no
# pg_read_server_files, so with the refusal moved ahead of the gate it read 0A000.
check "read_avro_manifest checks pg_read_server_files before it refuses a remote URL (#1913)" \
	"$(remote_refusal avro_unpriv "SELECT count(*) FROM pgcolumnar.read_avro_manifest('$REMOTE_S3')")" \
	"$GATE_REFUSAL"
check "read_manifest_list checks pg_read_server_files before it refuses a remote URL (#1913)" \
	"$(remote_refusal avro_unpriv "SELECT count(*) FROM pgcolumnar.read_manifest_list('$REMOTE_S3')")" \
	"$GATE_REFUSAL"

# "$PGC_WORKDIR/s3:" is a directory, so "$PGC_WORKDIR/s3://m0.avro" is a local
# file whose path holds "s3://" past its start.
mkdir -p "$PGC_WORKDIR/s3:"; chmod 777 "$PGC_WORKDIR/s3:"
cp "$M0" "$PGC_WORKDIR/s3://m0.avro"; cp "$ML" "$PGC_WORKDIR/s3://ml.avro"
chmod 644 "$PGC_WORKDIR/s3://m0.avro" "$PGC_WORKDIR/s3://ml.avro"
if [ "$(id -u)" = "0" ]; then chown -R postgres "$PGC_WORKDIR/s3:"; fi
check "control: read_avro_manifest still reads a local path that holds s3:// past its start (#1913)" \
	"$(q "SELECT count(*) FROM pgcolumnar.read_avro_manifest('$PGC_WORKDIR/s3://m0.avro')")" "$EXP_N"
check "control: read_manifest_list still reads a local path that holds s3:// past its start (#1913)" \
	"$(q "SELECT count(*) FROM pgcolumnar.read_manifest_list('$PGC_WORKDIR/s3://ml.avro')")" "1"

pgc_summary
