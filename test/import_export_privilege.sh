#!/usr/bin/env bash
#
# pgColumnar: import/export functions check table privilege, not only the
# server-file role (#559), and get_storage_id checks it too (#566).
#
# The four serial import/export functions gated on pg_read_server_files /
# pg_write_server_files and then checked NO privilege on the relation they read
# or wrote. So pg_read_server_files conferred INSERT on every columnar table and
# pg_write_server_files conferred SELECT on every one, exfiltrated to a file. The
# parallel twins (parallel_copy, parallel_export_parquet) already checked it; the
# serial four did not. get_storage_id returned an internal storage id of any
# relation to a role with only schema USAGE.
#
# LAYER ISOLATION is the whole point of the role setup. These functions have
# three refusers BEFORE the new table check: the pgcolumnar schema USAGE, the
# EXECUTE grant on the function, and the server-file role. A test role missing
# any of those is refused for a reason that is not the table ACL, and a total
# removal of the new check would still read as "refused". So the attacker role
# here holds ALL THREE outer layers and NO privilege on the target table -- the
# only configuration in which the new pg_class_aclcheck is the sole thing that
# can refuse. Verified by removal: deleting the five aclcheck blocks makes every
# makes every deny arm below report "allowed" (the leak), not a different error.
#
# The refusal is matched on "permission denied for table", which only
# aclcheck_error emits, and allow arms prove the owner is not broken by the fix.
#
# Usage:  test/import_export_privilege.sh [PG_CONFIG]
# Written fresh for pgColumnar. Sibling of test/server_file_privilege.sh.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

export PGC_EXTRA_CONF=$'max_prepared_transactions=4\nmax_worker_processes=8'
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

nope="/tmp/pgc_iep_does_not_exist.parquet"
arrow_out="$PGC_WORKDIR/iep_out.arrow"
parq_out="$PGC_WORKDIR/iep_out.parquet"

# A columnar table owned by the superuser. The attacker never gets a grant on it.
psql_run "CREATE TABLE victim (id int, secret text) USING pgcolumnar;"
psql_run "INSERT INTO victim SELECT g, 'ssn-'||g FROM generate_series(1,200) g;"
psql_run "REVOKE ALL ON victim FROM PUBLIC;"

# A source file the attacker legitimately produced from her OWN table, so the
# import arms fail on the target ACL and not on a missing or unreadable file.
psql_run "CREATE TABLE mine (id int, secret text) USING pgcolumnar;"
psql_run "DROP ROLE IF EXISTS t_files;"
psql_run "CREATE ROLE t_files NOSUPERUSER LOGIN;"
psql_run "GRANT USAGE ON SCHEMA pgcolumnar TO t_files;"
psql_run "GRANT pg_read_server_files, pg_write_server_files TO t_files;"
psql_run "ALTER TABLE mine OWNER TO t_files;"
mine_arrow="$PGC_WORKDIR/mine.arrow"
mine_parq="$PGC_WORKDIR/mine.parquet"
psql_run "INSERT INTO mine SELECT g, 'x'||g FROM generate_series(1,50) g;"
psql_run "SELECT pgcolumnar.export_arrow('mine'::regclass, '$mine_arrow');"
psql_run "SELECT pgcolumnar.export_parquet('mine'::regclass, '$mine_parq');"
# parallel_copy reads COPY text, so the attacker's own rows go out in that form too.
mine_text="$PGC_WORKDIR/mine.txt"
pex_out="$PGC_WORKDIR/iep_pex_out.parquet"
psql_run "COPY (SELECT id, secret FROM mine) TO '$mine_text' WITH (FORMAT text);"

# A DIRECT connection as the role, matching test/native_ownership.sh:22 and the
# trust auth pgc_setup uses (test/lib.sh:214, initdb -A trust). SET ROLE over a
# superuser connection was the wrong tool: a failed SET ROLE silently leaves the
# session as the superuser, so a deny arm reads as noerror for the wrong reason.
run_as() {	# role sql -> output as that role
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U "$1" \
		-d "$PGC_DB" -Atq -v ON_ERROR_STOP=0 -c "$2" 2>&1
}
# Whether a statement was refused by a table-privilege check, matching the repo
# idiom (test/native_ownership.sh, test/server_file_privilege.sh grep error text).
# "permission denied for table/relation" is emitted only by aclcheck_error, so it
# names the table ACL specifically and not the schema, the grant, or a file error.
# The role setup isolates this to a single refuser, so text is unambiguous here.
denied_as() {	# role sql -> denied | allowed
	local out
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U "$1" \
		-d "$PGC_DB" -Atq -v ON_ERROR_STOP=0 -c "$2" 2>&1)"
	grep -qiE 'permission denied for (table|relation)' <<<"$out" && echo denied || echo allowed
}

# ---- premises, each run BY the attacker so a login failure cannot pass a deny -
check_num "premise: the attacker can open a session" \
	"$(run_as t_files 'SELECT 1;')" 1
check "premise: the attacker holds pg_read_server_files" \
	"$(q "SELECT pg_has_role('t_files','pg_read_server_files','USAGE');")" "t"
check "premise: the attacker holds pg_write_server_files" \
	"$(q "SELECT pg_has_role('t_files','pg_write_server_files','USAGE');")" "t"
check "premise: the attacker reaches the schema (so a refusal is not schema USAGE)" \
	"$(q "SELECT has_schema_privilege('t_files','pgcolumnar','USAGE');")" "t"
check "premise: the attacker has NO privilege on victim" \
	"$(q "SELECT has_table_privilege('t_files','victim','SELECT') OR has_table_privilege('t_files','victim','INSERT');")" "f"
check "premise: the attacker can execute the functions (so a refusal is the table ACL)" \
	"$(q "SELECT has_function_privilege('t_files','pgcolumnar.import_parquet(regclass,text)','EXECUTE');")" "t"
check_num "premise: victim has its rows before any import is attempted" \
	"$(q "SELECT count(*) FROM victim;")" 200
check "premise: the attacker's own source files exist" \
	"$([ -s "$mine_arrow" ] && [ -s "$mine_parq" ] && echo yes || echo no)" "yes"

# ---- the deny arms: the new table ACL is the only thing that can refuse -------
# Each is "allowed" (leak) on main and "denied" after the ACL fix lands.
check "import_parquet refuses INSERT into a table the caller cannot write" \
	"$(denied_as t_files "SELECT pgcolumnar.import_parquet('victim'::regclass, '$mine_parq');")" "denied"
check "import_arrow refuses INSERT into a table the caller cannot write" \
	"$(denied_as t_files "SELECT pgcolumnar.import_arrow('victim'::regclass, '$mine_arrow');")" "denied"
check "export_parquet refuses SELECT of a table the caller cannot read" \
	"$(denied_as t_files "SELECT pgcolumnar.export_parquet('victim'::regclass, '$parq_out');")" "denied"
check "export_arrow refuses SELECT of a table the caller cannot read" \
	"$(denied_as t_files "SELECT pgcolumnar.export_arrow('victim'::regclass, '$arrow_out');")" "denied"
check "get_storage_id refuses a caller who cannot SELECT the relation" \
	"$(denied_as t_files "SELECT pgcolumnar.get_storage_id('victim'::regclass);")" "denied"
# The parallel twins (#1341): they always checked, and nothing asked them. Their
# loaders and workers skip the executor's permission check, so the check before
# anything is spawned is the only one.
check "parallel_copy refuses INSERT into a table the caller cannot write" \
	"$(denied_as t_files "SELECT pgcolumnar.parallel_copy('victim'::regclass, '$mine_text', 2);")" "denied"
check "parallel_export_parquet refuses SELECT of a table the caller cannot read" \
	"$(denied_as t_files "SELECT pgcolumnar.parallel_export_parquet('victim'::regclass, '$pex_out', 2);")" "denied"

# ---- the consequence: no rows leaked in or out -------------------------------
check_num "victim still holds exactly its own rows (no import landed)" \
	"$(q "SELECT count(*) FROM victim;")" 200
check "no victim file was written by the export attempts" \
	"$([ -e "$parq_out" ] || [ -e "$arrow_out" ] && echo written || echo none)" "none"
check "nor by the parallel export" \
	"$([ -e "$pex_out" ] && echo written || echo none)" "none"

# ---- allow arms: the owner is not broken by the fix --------------------------
# t_files owns 'mine' and may read and write it, so the same calls must succeed.
check "the owner can still export_arrow its own table" \
	"$(denied_as t_files "SELECT pgcolumnar.export_arrow('mine'::regclass, '$PGC_WORKDIR/mine2.arrow');")" "allowed"
check "the owner can still export_parquet its own table" \
	"$(denied_as t_files "SELECT pgcolumnar.export_parquet('mine'::regclass, '$PGC_WORKDIR/mine2.parquet');")" "allowed"
check "the owner can still read its own storage id" \
	"$(denied_as t_files "SELECT pgcolumnar.get_storage_id('mine'::regclass);")" "allowed"
check "the owner can still import into its own table" \
	"$(denied_as t_files "SELECT pgcolumnar.import_arrow('mine'::regclass, '$mine_arrow');")" "allowed"
check "the owner can still parallel_copy into its own table" \
	"$(run_as t_files "SELECT pgcolumnar.parallel_copy('mine'::regclass, '$mine_text', 2);")" "50"
check "the owner can still parallel_export_parquet its own table" \
	"$(run_as t_files "SELECT pgcolumnar.parallel_export_parquet('mine'::regclass, '$PGC_WORKDIR/mine_pex.parquet', 2) > 0;")" "t"


pgc_summary
