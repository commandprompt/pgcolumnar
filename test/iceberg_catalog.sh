#!/usr/bin/env bash
#
# pgColumnar Iceberg catalog reader (#388 phase 3a). Reads a real Apache Iceberg
# table metadata.json -- one produced by pyiceberg, an independent writer,
# committed under test/fixtures/iceberg/ with an oracle in expected_meta.json
# extracted from pyiceberg's own current_snapshot() view -- and asserts the
# resolved current snapshot against that oracle. Same shape as avro_manifest.sh:
# an independent writer is the oracle, so a green check proves our reader and a
# real Iceberg writer agree, not that our reader agrees with our own writer.
#
# Usage:  test/iceberg_catalog.sh [PG_CONFIG]

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

FX="$(dirname "${BASH_SOURCE[0]}")/fixtures/iceberg"
[ -f "$FX/table.metadata.json" ] || pgc_skip fixture "iceberg metadata fixture is missing"
python3 -c 'import json' 2>/dev/null || pgc_skip python "python3 is needed to read the oracle"

MD="$PGC_WORKDIR/table.metadata.json"
cp "$FX/table.metadata.json" "$MD"
chmod 644 "$MD"

# ---- resolve: the current snapshot matches the oracle ----------------------
# oracle rendered "snapshot_id|sequence_number|operation|schema_id|<ml basename>"
ORACLE="$(python3 - "$FX/expected_meta.json" <<'PY'
import json, sys
o = json.load(open(sys.argv[1]))
print("%d|%d|%s|%d|%s" % (o["snapshot_id"], o["sequence_number"],
      o["operation"], o["schema_id"], o["manifest_list_basename"]))
PY
)"
DECODED="$(q "SELECT snapshot_id || '|' || sequence_number || '|' || operation || '|' ||
                     schema_id || '|' || (regexp_match(manifest_list, '[^/]+\$'))[1]
              FROM pgcolumnar.iceberg_current_snapshot('$MD')")"
# check_text: with both the reader and the oracle failing, two empty readings
# would agree.
check_text "current snapshot == oracle (id|seq|op|schema|manifest-list)" \
	"$DECODED" "$ORACLE"

# exactly one current snapshot, and no parent (first append)
check "exactly one current snapshot row" \
	"$(q "SELECT count(*) FROM pgcolumnar.iceberg_current_snapshot('$MD')")" "1"
check "the first snapshot has no parent (NULL)" \
	"$(q "SELECT parent_snapshot_id IS NULL FROM pgcolumnar.iceberg_current_snapshot('$MD')")" "t"

# ---- resolution proof: picks the NAMED current snapshot, not the first -----
# Craft a two-snapshot metadata by prepending a decoy snapshot (different id and
# manifest-list) while current-snapshot-id still names the real one. A reader
# that returned snapshots[0] would return the decoy; the real reader must not.
TWO="$PGC_WORKDIR/two_snap.metadata.json"
python3 - "$MD" "$TWO" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
real = m["snapshots"][-1]
decoy = dict(real)
decoy["snapshot-id"] = real["snapshot-id"] + 1
decoy["manifest-list"] = "file:///warehouse/db/events/metadata/DECOY.avro"
decoy["sequence-number"] = (real.get("sequence-number", 0) or 0) + 99
m["snapshots"] = [decoy, real]            # decoy FIRST; current-snapshot-id unchanged
json.dump(m, open(sys.argv[2], "w"))
PY
chmod 644 "$TWO"
check_text "resolves the named current snapshot, not snapshots[0]" \
	"$(q "SELECT (regexp_match(manifest_list, '[^/]+\$'))[1]
	      FROM pgcolumnar.iceberg_current_snapshot('$TWO')")" \
	"$(python3 -c "import json;print(json.load(open('$FX/expected_meta.json'))['manifest_list_basename'])")"
# With the total beside it: a reader that returned no row at all also returns no
# decoy.
check "the decoy snapshot is not returned" \
	"$(q "SELECT count(*) || '/' || count(*) FILTER (WHERE manifest_list LIKE '%DECOY%')
	      FROM pgcolumnar.iceberg_current_snapshot('$TWO')")" "1/0"

# ---- no current snapshot: zero rows, not an error --------------------------
NOSNAP="$PGC_WORKDIR/no_current.metadata.json"
python3 - "$MD" "$NOSNAP" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
m.pop("current-snapshot-id", None)         # a table with no current snapshot is legal
json.dump(m, open(sys.argv[2], "w"))
PY
chmod 644 "$NOSNAP"
check "a table with no current snapshot returns zero rows" \
	"$(q "SELECT count(*) FROM pgcolumnar.iceberg_current_snapshot('$NOSNAP')")" "0"

# ---- corrupt: current-snapshot-id names a snapshot that is not there --------
BADID="$PGC_WORKDIR/bad_id.metadata.json"
python3 - "$MD" "$BADID" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
m["current-snapshot-id"] = 999999999999999   # points at nothing in snapshots[]
json.dump(m, open(sys.argv[2], "w"))
PY
chmod 644 "$BADID"
check "a dangling current-snapshot-id is refused (data_corrupted XX001)" \
	"$(sqlstate_of "SELECT * FROM pgcolumnar.iceberg_current_snapshot('$BADID')")" "XX001"

# ---- not Iceberg metadata: valid JSON, but no format-version ---------------
printf '{"hello":"world"}' > "$PGC_WORKDIR/notice.json"
chmod 644 "$PGC_WORKDIR/notice.json"
check "a non-Iceberg JSON object is refused (invalid_parameter_value 22023)" \
	"$(sqlstate_of "SELECT * FROM pgcolumnar.iceberg_current_snapshot('$PGC_WORKDIR/notice.json')")" "22023"

# ---- malformed JSON is rejected cleanly, backend survives ------------------
printf 'this is { not json at all' > "$PGC_WORKDIR/bad.json"
chmod 644 "$PGC_WORKDIR/bad.json"
check "malformed JSON is rejected (22P02), backend survives" \
	"$(sqlstate_of "SELECT * FROM pgcolumnar.iceberg_current_snapshot('$PGC_WORKDIR/bad.json')")" "22P02"
check "backend still up after the bad file" "$(q 'SELECT 1')" "1"

# ---- privilege: needs pg_read_server_files ---------------------------------
# USAGE on the schema, so the refusal is the FILE gate's and not "permission
# denied for schema pgcolumnar", which is also 42501 (the shape iceberg_scan.sh had,
# measured there).
psql_run "DROP ROLE IF EXISTS ice_unpriv; CREATE ROLE ice_unpriv LOGIN;
	GRANT USAGE ON SCHEMA pgcolumnar TO ice_unpriv;"
check "an unprivileged role is refused (42501)" \
	"$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U ice_unpriv -d "$PGC_DB" \
		-qtA 2>&1 <<SQL | sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\).*/\1/p' | head -1
\\set VERBOSITY sqlstate
SELECT count(*) FROM pgcolumnar.iceberg_current_snapshot('$MD');
SQL
)" "42501"

# The positive control a deny arm needs: a gate admitting only superusers would
# lock out every pg_read_server_files member and still pass the arm above.
psql_run "DROP ROLE IF EXISTS ice_reader; CREATE ROLE ice_reader LOGIN;
	GRANT pg_read_server_files TO ice_reader; GRANT USAGE ON SCHEMA pgcolumnar TO ice_reader;"
check "a pg_read_server_files member who is not superuser may read the catalog" \
	"$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U ice_reader -d "$PGC_DB" \
		-qtA -c "SELECT count(*) FROM pgcolumnar.iceberg_current_snapshot('$MD');" 2>&1 | tail -1)" "1"

pgc_summary
