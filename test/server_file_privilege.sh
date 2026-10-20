#!/usr/bin/env bash
#
# pgColumnar server-side-file privilege boundary (#216, #330).
#
# Every entry point that reads or writes a server-side file gates in C on the
# matching core role -- pg_read_server_files for a read, pg_write_server_files for
# a write -- and on nothing else. There is no SQL-level REVOKE and no shared
# wrapper, so the boundary is one role check per call site. A new entry point that
# forgets the check would be reachable by any role the schema is exposed to, and
# nothing would notice. This suite is that noticing.
#
# It enumerates the entry points as data and asserts two things per point:
#   1. a role WITHOUT the matching server-file role is refused, and the error
#      names the role -- so the check reached the gate, not the schema-usage or
#      parse-time errors, which do not contain the role name;
#   2. a role WITH the matching role gets PAST the gate (it then fails on the
#      missing file, or the write succeeds) -- so the gate is the role and not
#      something incidental.
# And a coverage check: every SQL function that takes a file path must appear in
# the list, so a new server-file function fails the gate instead of slipping past.
#
# The schema does not grant USAGE to PUBLIC, so both test roles are granted USAGE
# on purpose; that lets the call reach the C gate rather than stopping at the
# schema.
#
# #330: this replaced the superuser() gate. See docs/administration.md. The read
# functions parse files this project wrote, so widening them to a role widens who
# reaches those parsers; the mitigation is the parser fuzzing tracked in #214.
#
# Usage:  test/server_file_privilege.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# parallel_copy prepares one xact per worker; parallel_export spawns workers.
export PGC_EXTRA_CONF=$'max_prepared_transactions=4\nmax_worker_processes=8'
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# Derived from the control file, not hardcoded. This broke once when the install script
# was renamed for a version bump, and a hardcoded name would break again at the next one.
_root="$(dirname "${BASH_SOURCE[0]}")/.."
_ver=$(grep -oE "default_version = '[^']+'" "$_root/pgcolumnar.control" | sed "s/.*'\(.*\)'/\1/")
SQLFILE="$_root/pgcolumnar--$_ver.sql"
[ -f "$SQLFILE" ] || { echo "FAIL  install script $SQLFILE not found"; exit 1; }
nope="/tmp/pgc_sfp_does_not_exist.parquet"
out="$PGC_WORKDIR/sfp_out"
outdir="$PGC_WORKDIR/sfp_dir"

psql_run "CREATE TABLE imp_target (id int, v text) USING pgcolumnar;"

# t_none: reaches the functions (schema usage) but holds no server-file role.
psql_run "DROP ROLE IF EXISTS t_none;"
psql_run "CREATE ROLE t_none NOSUPERUSER;"
psql_run "GRANT USAGE ON SCHEMA pgcolumnar TO t_none;"

# t_priv: holds both roles plus the table grants, so it clears the gate and then
# meets the file. Not a superuser -- that is the point, the role is the gate.
psql_run "DROP ROLE IF EXISTS t_priv;"
psql_run "CREATE ROLE t_priv NOSUPERUSER;"
psql_run "GRANT USAGE ON SCHEMA pgcolumnar TO t_priv;"
psql_run "GRANT pg_read_server_files, pg_write_server_files TO t_priv;"
psql_run "GRANT SELECT, INSERT ON imp_target TO t_priv;"

# foreign table over the parquet FDW; the gate is in BeginForeignScan.
psql_run "CREATE SERVER pqsrv FOREIGN DATA WRAPPER pgcolumnar_parquet;"
psql_run "CREATE FOREIGN TABLE ft (id int, v text) SERVER pqsrv OPTIONS (path '$nope');"
psql_run "GRANT SELECT ON ft TO t_none, t_priv;"
psql_run "CREATE SERVER icesrv FOREIGN DATA WRAPPER pgcolumnar_iceberg;"
psql_run "CREATE FOREIGN TABLE ift (id bigint) SERVER icesrv OPTIONS (metadata_path '$nope');"
psql_run "GRANT SELECT ON ift TO t_none, t_priv;"

# tier_partition (#1295) writes its file through export_parquet, so its gate is
# export_parquet's. To reach it a role needs SELECT and, for the SHARE lock the
# function takes first, UPDATE on the partition; both roles get them, so the
# role gate is the only thing between them. Past the gate, the privileged role
# writes the file and is then stopped by DETACH, which needs the parent's owner.
psql_run "CREATE TABLE sfp_parent (k int) PARTITION BY RANGE (k);"
psql_run "CREATE TABLE sfp_p1 PARTITION OF sfp_parent FOR VALUES FROM (0) TO (10) USING pgcolumnar;"
psql_run "INSERT INTO sfp_parent VALUES (1);"
psql_run "GRANT SELECT, UPDATE ON sfp_p1 TO t_none, t_priv;"

run_as() {	# role sql -> combined output; SET ROLE drops to the test role in C.
	# The first line proves the role took: "as:<role>". Without it a failed SET ROLE
	# printed no gate message, which the privileged side reads as "got past".
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -X -v ON_ERROR_STOP=0 -c "SET ROLE $1;" -c "SELECT 'as:' || current_user;" -c "$2" 2>&1
}

# gate ROLE GATE SQL -> "refused" when ROLE's call names the GATE role, "passed" when
# it does not, or "no reading" when ROLE never took effect. With SET ROLE failing for
# the privileged role, all 21 "gets past the gate" checks passed and the suite exited 0.
gate() {
	local reply
	rm -rf "$out".parquet "$out".arrow "$outdir" 2>/dev/null || true
	reply="$(run_as "$1" "$3")"
	if ! grep -q "as:$1\b" <<<"$reply"; then
		echo "no reading (SET ROLE $1 did not take: $(head -1 <<<"$reply"))"
	elif grep -qi "$2" <<<"$reply"; then echo refused
	else echo passed; fi
}

# Each entry point twice, by literal name (#1341): t_none must be refused by the
# server-file role its direction needs, and t_priv, which holds both roles, must not.
SQL_import_parquet="SELECT pgcolumnar.import_parquet('imp_target'::regclass, '$nope')"
check "no role is refused by the pg_read_server_files gate: import_parquet" "$(gate t_none pg_read_server_files "$SQL_import_parquet")" "refused"
check "the pg_read_server_files role gets past the gate: import_parquet" "$(gate t_priv pg_read_server_files "$SQL_import_parquet")" "passed"
SQL_read_parquet="SELECT * FROM pgcolumnar.read_parquet('$nope') AS t(id int)"
check "no role is refused by the pg_read_server_files gate: read_parquet" "$(gate t_none pg_read_server_files "$SQL_read_parquet")" "refused"
check "the pg_read_server_files role gets past the gate: read_parquet" "$(gate t_priv pg_read_server_files "$SQL_read_parquet")" "passed"
SQL_parquet_schema="SELECT * FROM pgcolumnar.parquet_schema('$nope')"
check "no role is refused by the pg_read_server_files gate: parquet_schema" "$(gate t_none pg_read_server_files "$SQL_parquet_schema")" "refused"
check "the pg_read_server_files role gets past the gate: parquet_schema" "$(gate t_priv pg_read_server_files "$SQL_parquet_schema")" "passed"
SQL_import_arrow="SELECT pgcolumnar.import_arrow('imp_target'::regclass, '$nope')"
check "no role is refused by the pg_read_server_files gate: import_arrow" "$(gate t_none pg_read_server_files "$SQL_import_arrow")" "refused"
check "the pg_read_server_files role gets past the gate: import_arrow" "$(gate t_priv pg_read_server_files "$SQL_import_arrow")" "passed"
SQL_file_split_offsets="SELECT pgcolumnar.file_split_offsets('$nope', 2)"
check "no role is refused by the pg_read_server_files gate: file_split_offsets" "$(gate t_none pg_read_server_files "$SQL_file_split_offsets")" "refused"
check "the pg_read_server_files role gets past the gate: file_split_offsets" "$(gate t_priv pg_read_server_files "$SQL_file_split_offsets")" "passed"
SQL_parallel_copy="SELECT pgcolumnar.parallel_copy('imp_target'::regclass, '$nope', 2)"
check "no role is refused by the pg_read_server_files gate: parallel_copy" "$(gate t_none pg_read_server_files "$SQL_parallel_copy")" "refused"
check "the pg_read_server_files role gets past the gate: parallel_copy" "$(gate t_priv pg_read_server_files "$SQL_parallel_copy")" "passed"
SQL_parquet_fdw_scan="SELECT * FROM ft"
check "no role is refused by the pg_read_server_files gate: parquet_fdw_scan" "$(gate t_none pg_read_server_files "$SQL_parquet_fdw_scan")" "refused"
check "the pg_read_server_files role gets past the gate: parquet_fdw_scan" "$(gate t_priv pg_read_server_files "$SQL_parquet_fdw_scan")" "passed"
SQL_read_avro_manifest="SELECT * FROM pgcolumnar.read_avro_manifest('$nope')"
check "no role is refused by the pg_read_server_files gate: read_avro_manifest" "$(gate t_none pg_read_server_files "$SQL_read_avro_manifest")" "refused"
check "the pg_read_server_files role gets past the gate: read_avro_manifest" "$(gate t_priv pg_read_server_files "$SQL_read_avro_manifest")" "passed"
SQL_read_manifest_list="SELECT * FROM pgcolumnar.read_manifest_list('$nope')"
check "no role is refused by the pg_read_server_files gate: read_manifest_list" "$(gate t_none pg_read_server_files "$SQL_read_manifest_list")" "refused"
check "the pg_read_server_files role gets past the gate: read_manifest_list" "$(gate t_priv pg_read_server_files "$SQL_read_manifest_list")" "passed"
SQL_iceberg_current_snapshot="SELECT * FROM pgcolumnar.iceberg_current_snapshot('$nope')"
check "no role is refused by the pg_read_server_files gate: iceberg_current_snapshot" "$(gate t_none pg_read_server_files "$SQL_iceberg_current_snapshot")" "refused"
check "the pg_read_server_files role gets past the gate: iceberg_current_snapshot" "$(gate t_priv pg_read_server_files "$SQL_iceberg_current_snapshot")" "passed"
SQL_iceberg_data_files="SELECT * FROM pgcolumnar.iceberg_data_files('$nope')"
check "no role is refused by the pg_read_server_files gate: iceberg_data_files" "$(gate t_none pg_read_server_files "$SQL_iceberg_data_files")" "refused"
check "the pg_read_server_files role gets past the gate: iceberg_data_files" "$(gate t_priv pg_read_server_files "$SQL_iceberg_data_files")" "passed"
SQL_iceberg_scan="SELECT * FROM pgcolumnar.iceberg_scan('$nope') AS t(id bigint)"
check "no role is refused by the pg_read_server_files gate: iceberg_scan" "$(gate t_none pg_read_server_files "$SQL_iceberg_scan")" "refused"
check "the pg_read_server_files role gets past the gate: iceberg_scan" "$(gate t_priv pg_read_server_files "$SQL_iceberg_scan")" "passed"
SQL_export_parquet="SELECT pgcolumnar.export_parquet('imp_target'::regclass, '$out.parquet')"
check "no role is refused by the pg_write_server_files gate: export_parquet" "$(gate t_none pg_write_server_files "$SQL_export_parquet")" "refused"
check "the pg_write_server_files role gets past the gate: export_parquet" "$(gate t_priv pg_write_server_files "$SQL_export_parquet")" "passed"
SQL_export_arrow="SELECT pgcolumnar.export_arrow('imp_target'::regclass, '$out.arrow')"
check "no role is refused by the pg_write_server_files gate: export_arrow" "$(gate t_none pg_write_server_files "$SQL_export_arrow")" "refused"
check "the pg_write_server_files role gets past the gate: export_arrow" "$(gate t_priv pg_write_server_files "$SQL_export_arrow")" "passed"
SQL_parallel_export_parquet="SELECT pgcolumnar.parallel_export_parquet('imp_target'::regclass, '$outdir', 2)"
check "no role is refused by the pg_write_server_files gate: parallel_export_parquet" "$(gate t_none pg_write_server_files "$SQL_parallel_export_parquet")" "refused"
check "the pg_write_server_files role gets past the gate: parallel_export_parquet" "$(gate t_priv pg_write_server_files "$SQL_parallel_export_parquet")" "passed"
SQL_tier_partition="SELECT pgcolumnar.tier_partition('sfp_p1'::regclass, 'pqsrv', '$out.parquet')"
check "no role is refused by the pg_write_server_files gate: tier_partition" "$(gate t_none pg_write_server_files "$SQL_tier_partition")" "refused"
check "the pg_write_server_files role gets past the gate: tier_partition" "$(gate t_priv pg_write_server_files "$SQL_tier_partition")" "passed"
SQL_iceberg_rest_table_location="SELECT pgcolumnar.iceberg_rest_table_location('http://127.0.0.1:1', 'db', 't')"
check "no role is refused by the pg_read_server_files gate: iceberg_rest_table_location" "$(gate t_none pg_read_server_files "$SQL_iceberg_rest_table_location")" "refused"
check "the pg_read_server_files role gets past the gate: iceberg_rest_table_location" "$(gate t_priv pg_read_server_files "$SQL_iceberg_rest_table_location")" "passed"
SQL_iceberg_rest_scan="SELECT * FROM pgcolumnar.iceberg_rest_scan('http://127.0.0.1:1', 'db', 't') AS t(id bigint)"
check "no role is refused by the pg_read_server_files gate: iceberg_rest_scan" "$(gate t_none pg_read_server_files "$SQL_iceberg_rest_scan")" "refused"
check "the pg_read_server_files role gets past the gate: iceberg_rest_scan" "$(gate t_priv pg_read_server_files "$SQL_iceberg_rest_scan")" "passed"
SQL_iceberg_rest_namespaces="SELECT * FROM pgcolumnar.iceberg_rest_namespaces('http://127.0.0.1:1')"
check "no role is refused by the pg_read_server_files gate: iceberg_rest_namespaces" "$(gate t_none pg_read_server_files "$SQL_iceberg_rest_namespaces")" "refused"
check "the pg_read_server_files role gets past the gate: iceberg_rest_namespaces" "$(gate t_priv pg_read_server_files "$SQL_iceberg_rest_namespaces")" "passed"
SQL_iceberg_rest_tables="SELECT * FROM pgcolumnar.iceberg_rest_tables('http://127.0.0.1:1', 'db')"
check "no role is refused by the pg_read_server_files gate: iceberg_rest_tables" "$(gate t_none pg_read_server_files "$SQL_iceberg_rest_tables")" "refused"
check "the pg_read_server_files role gets past the gate: iceberg_rest_tables" "$(gate t_priv pg_read_server_files "$SQL_iceberg_rest_tables")" "passed"
SQL_iceberg_fdw_scan="SELECT * FROM ift"
check "no role is refused by the pg_read_server_files gate: iceberg_fdw_scan" "$(gate t_none pg_read_server_files "$SQL_iceberg_fdw_scan")" "refused"
check "the pg_read_server_files role gets past the gate: iceberg_fdw_scan" "$(gate t_priv pg_read_server_files "$SQL_iceberg_fdw_scan")" "passed"

# Every function in the install script that takes a path or filename must be one of
# the entry points above. The list is literal so each name is a check of its own; the
# check after it fails when the script grows a file function this list does not name.
discovered="$(awk '
	/CREATE FUNCTION pgcolumnar\./ { cap=1; buf="" }
	cap { buf = buf " " $0
	      if (buf ~ /\)/) {
	          if (buf ~ /path text/ || buf ~ /filename text/) {
	              match(buf, /pgcolumnar\.[a-z_]+/)
	              print substr(buf, RSTART + 11, RLENGTH - 11)
	          }
	          cap = 0
	      } }' "$SQLFILE" | sort -u)"
covered() {	# fn -> yes when the install script defines it with a path AND it is an entry point
	grep -qx "$1" <<<"$discovered" || { echo "no (not a file function in $SQLFILE)"; return; }
	local v="SQL_$1"
	[ -n "${!v:-}" ] && echo yes || echo "no (no entry point)"
}
check "server-file function is covered by the boundary test: export_arrow" "$(covered export_arrow)" "yes"
check "server-file function is covered by the boundary test: export_parquet" "$(covered export_parquet)" "yes"
check "server-file function is covered by the boundary test: file_split_offsets" "$(covered file_split_offsets)" "yes"
check "server-file function is covered by the boundary test: iceberg_current_snapshot" "$(covered iceberg_current_snapshot)" "yes"
check "server-file function is covered by the boundary test: iceberg_data_files" "$(covered iceberg_data_files)" "yes"
check "server-file function is covered by the boundary test: iceberg_scan" "$(covered iceberg_scan)" "yes"
check "server-file function is covered by the boundary test: import_arrow" "$(covered import_arrow)" "yes"
check "server-file function is covered by the boundary test: import_parquet" "$(covered import_parquet)" "yes"
check "server-file function is covered by the boundary test: parallel_copy" "$(covered parallel_copy)" "yes"
check "server-file function is covered by the boundary test: parallel_export_parquet" "$(covered parallel_export_parquet)" "yes"
check "server-file function is covered by the boundary test: parquet_schema" "$(covered parquet_schema)" "yes"
check "server-file function is covered by the boundary test: read_avro_manifest" "$(covered read_avro_manifest)" "yes"
check "server-file function is covered by the boundary test: read_manifest_list" "$(covered read_manifest_list)" "yes"
check "server-file function is covered by the boundary test: read_parquet" "$(covered read_parquet)" "yes"
check "server-file function is covered by the boundary test: tier_partition" "$(covered tier_partition)" "yes"
KNOWN_FILE_FUNCTIONS="export_arrow export_parquet file_split_offsets iceberg_current_snapshot iceberg_data_files iceberg_scan import_arrow import_parquet parallel_copy parallel_export_parquet parquet_schema read_avro_manifest read_manifest_list read_parquet tier_partition"
check "the install script defines no file function this suite does not name" \
	"$(x=""; for fn in $discovered; do case " $KNOWN_FILE_FUNCTIONS " in *" $fn "*) ;; *) x="$x $fn" ;; esac; done; echo "${x:-none}")" "none"
n_found="$(printf '%s\n' "$discovered" | grep -c .)"
check "coverage check found the file functions (not an empty scan)" \
	"$([ "$n_found" -ge 7 ] && echo yes || echo "no ($n_found found)")" "yes"

pgc_summary
