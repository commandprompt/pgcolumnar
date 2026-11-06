#!/usr/bin/env bash
#
# The foreign-data wrappers' PLANNING path must not read server files for a role the
# scan itself would refuse (#330 for Parquet; the Iceberg wrapper had no such gate).
# A planner estimate is visible through EXPLAIN, so a wrapper that reads the file at
# plan time for anyone tells a role without pg_read_server_files how many rows a
# server-side table holds, and whether a path exists at all.
#
# Usage:  test/fdw_plan_privilege.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

FX="$(dirname "${BASH_SOURCE[0]}")/fixtures/iceberg"
[ -f "$(ls "$FX"/warehouse/db/events/data/*/*.parquet 2>/dev/null | head -1)" ] \
	|| pgc_skip fixture "iceberg warehouse data files are missing"
DEST="$PGC_WORKDIR/wh"; rm -rf "$DEST"; mkdir -p "$DEST"
cp -r "$FX/warehouse/db" "$DEST/db"; chmod -R a+rX "$DEST"
MD="$(ls "$DEST"/db/events/metadata/*.metadata.json | sort | tail -1)"
PQ="$PGC_WORKDIR/fp.parquet"

# est ROLE SQL -> the top node's row estimate, or the SQLSTATE if EXPLAIN failed
est() {
	local out
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U "$1" -d "$PGC_DB" \
		-At -v VERBOSITY=sqlstate -v ON_ERROR_STOP=1 -c "EXPLAIN $2" 2>&1)" \
		|| { sed -n 's/^.*ERROR:[[:space:]]*\([0-9A-Z]\{5\}\).*$/\1/p' <<<"$out" | head -1; return; }
	grep -oE 'rows=[0-9]+' <<<"$out" | head -1 | cut -d= -f2
}
state_as() {
	local out
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U "$1" -d "$PGC_DB" \
		-At -v VERBOSITY=sqlstate -v ON_ERROR_STOP=1 -c "$2" 2>&1)" && { echo ok; return; }
	sed -n 's/^.*ERROR:[[:space:]]*\([0-9A-Z]\{5\}\).*$/\1/p' <<<"$out" | head -1
}

psql_run "CREATE ROLE fp_reader NOSUPERUSER LOGIN;"
psql_run "CREATE SERVER fp_ice FOREIGN DATA WRAPPER pgcolumnar_iceberg;"
psql_run "CREATE FOREIGN TABLE fp_ev (id bigint, region text, amount int) SERVER fp_ice OPTIONS (metadata_path '$MD');"
psql_run "CREATE FOREIGN TABLE fp_gone (id bigint, region text, amount int) SERVER fp_ice OPTIONS (metadata_path '$DEST/no/such/v1.metadata.json');"
psql_run "CREATE TABLE fp_src (id int, v text) USING pgcolumnar;"
psql_run "INSERT INTO fp_src SELECT g, md5(g::text) || md5((g * 7)::text) FROM generate_series(1, 20000) g;"
psql_run "SELECT pgcolumnar.export_parquet('fp_src', '$PQ');"
psql_run "CREATE SERVER fp_pq FOREIGN DATA WRAPPER pgcolumnar_parquet;"
psql_run "CREATE FOREIGN TABLE fp_pqt (id int, v text) SERVER fp_pq OPTIONS (path '$PQ');"
psql_run "GRANT SELECT ON fp_ev, fp_gone, fp_pqt TO fp_reader;"

check "premise: the Parquet file was written" \
	"$(q "SELECT ((pg_stat_file('$PQ')).size > 0)::text")" "true"
check "premise: the reader is not a member of pg_read_server_files" \
	"$(q "SELECT pg_has_role('fp_reader', 'pg_read_server_files', 'USAGE')::text")" "false"
_ice_rows="$(q 'SELECT count(*) FROM fp_ev')"

# ---- Iceberg ---------------------------------------------------------------------
check "a privileged planner estimates the Iceberg table from its manifests" \
	"$(est postgres "SELECT * FROM fp_ev")" "$_ice_rows"
check "an unprivileged planner gets the flat estimate, not the Iceberg table's row count" \
	"$(est fp_reader "SELECT * FROM fp_ev")" "1000"
check "and a metadata_path that does not exist plans the same, so planning probes nothing" \
	"$(est fp_reader "SELECT * FROM fp_gone")" "1000"
check "the unprivileged Iceberg scan is still refused (42501)" \
	"$(state_as fp_reader "SELECT count(*) FROM fp_ev;")" "42501"

# ---- Parquet (#330) ---------------------------------------------------------------
_pq_priv="$(est postgres "SELECT * FROM fp_pqt")"
check "a privileged planner estimates the Parquet table from the file's size" \
	"$([ -n "$_pq_priv" ] && [ "$_pq_priv" != 1000 ] && echo sized || echo "flat ($_pq_priv)")" "sized"
check "an unprivileged planner gets the flat Parquet estimate" \
	"$(est fp_reader "SELECT * FROM fp_pqt")" "1000"

pgc_summary
