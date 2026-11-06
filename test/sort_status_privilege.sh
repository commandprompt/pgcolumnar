#!/usr/bin/env bash
#
# pgColumnar: pgcolumnar.sort_status is usable by a table's owner, and only by a
# caller who may read the table (#608).
#
# Same class as stats() (#560): sort_status was LANGUAGE sql and selected
# straight from pgcolumnar.storage, row_group and options, which carry no GRANT,
# so those tables are owner-only and a COLUMNAR table's own owner is not their
# owner. The function therefore false-denied the owner of the table it reports on
# with 42501 on an internal catalog. GRANTing the catalog to PUBLIC is the wrong
# fix -- it would publish zone_map's per-column min/max/sum -- so the function
# runs SECURITY DEFINER and does the privilege check itself.
#
# THE ARMS THAT MATTER (the #607 review lesson): a deny arm alone is vacuous --
# an invoker-rights function denies EVERYONE and still passes it. So the owner
# and a SELECT-holder each getting a row are the arms that RED on the invoker
# shape, and the deny arm asserts "permission denied for table" AND that the
# refusal names the TABLE, not the internal catalog it never asked about (which
# is exactly what the invoker-rights bug refused with).
#
# Usage:  test/sort_status_privilege.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

ROWS=500

psql_run "DROP ROLE IF EXISTS t_ssowner;"
psql_run "DROP ROLE IF EXISTS t_ssnone;"
psql_run "DROP ROLE IF EXISTS t_sssel;"
psql_run "CREATE ROLE t_ssowner NOSUPERUSER LOGIN;"
psql_run "CREATE ROLE t_ssnone NOSUPERUSER LOGIN;"
psql_run "CREATE ROLE t_sssel NOSUPERUSER LOGIN;"
for r in t_ssowner t_ssnone t_sssel; do
	psql_run "GRANT USAGE ON SCHEMA pgcolumnar TO $r;"
done
psql_run "CREATE TABLE ss_t (id int, ts int) USING pgcolumnar;"
psql_run "INSERT INTO ss_t SELECT g, g FROM generate_series(1,$ROWS) g;"
psql_run "SELECT pgcolumnar.vacuum_sorted('ss_t','ts');"   -- give it a sorted run
psql_run "ALTER TABLE ss_t OWNER TO t_ssowner;"
psql_run "REVOKE ALL ON ss_t FROM PUBLIC;"
psql_run "GRANT SELECT ON ss_t TO t_sssel;"
psql_run "CREATE TABLE ss_h (id int);"   # a heap table, readable by no test role
psql_run "REVOKE ALL ON ss_h FROM PUBLIC;"

as() { env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U "$1" \
	-d "$PGC_DB" -At -v ON_ERROR_STOP=0 -c "$2" 2>&1 | head -1; }

# ---- premises, each run by the role it is about ----------------------------
check "premise: the owner can open a session" "$(as t_ssowner 'SELECT 1;')" "1"
check "premise: the no-privilege role can open a session" "$(as t_ssnone 'SELECT 1;')" "1"
check "premise: t_ssowner really owns the table" \
	"$(q "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relname='ss_t';")" "t_ssowner"
check "premise: none of these roles is a superuser" \
	"$(q "SELECT bool_and(NOT rolsuper) FROM pg_roles WHERE rolname LIKE 't_ss%';")" "t"
check "premise: the owner can read its own table by ordinary SQL" \
	"$(as t_ssowner 'SELECT count(*) FROM ss_t;')" "$ROWS"
check "premise: the no-privilege role cannot read it by ordinary SQL" \
	"$(as t_ssnone 'SELECT count(*) FROM ss_t;' | grep -c 'permission denied')" "1"
check "premise: the catalog tables are NOT readable by these roles, so a GRANT is not the fix" \
	"$(as t_sssel 'SELECT count(*) FROM pgcolumnar.row_group;' | grep -c 'permission denied')" "1"

# ---- the defect (positive controls: these RED on the invoker-rights shape) --
check_num "the OWNER of the table can read its sort status" \
	"$(as t_ssowner "SELECT count(*) FROM pgcolumnar.sort_status('ss_t');")" "1"
check_num "a role merely GRANTed select can read it too" \
	"$(as t_sssel "SELECT count(*) FROM pgcolumnar.sort_status('ss_t');")" "1"
# and the value is real, not an empty row
check "the owner sees its sorted run, not a false empty" \
	"$(as t_ssowner "SELECT sorted_groups > 0 FROM pgcolumnar.sort_status('ss_t');")" "t"

# ---- the bar (deny, and it must name the TABLE not the catalog) -------------
check "a role with no privilege on the table is refused" \
	"$(as t_ssnone "SELECT count(*) FROM pgcolumnar.sort_status('ss_t');" | grep -c 'permission denied for table')" "1"
check "and the refusal names the TABLE, not a catalog table it never asked about" \
	"$(as t_ssnone "SELECT count(*) FROM pgcolumnar.sort_status('ss_t');" | grep -c 'ss_t')" "1"

# ---- the order: privilege before anything else ------------------------------
#
# As in stats_privilege.sh: get_storage_id checks the DEFINER, so only the explicit
# caller check refuses, and only while it runs first ("sidfirst" swaps them and
# every check above stays green). A heap table must be refused for privilege.
check "a role with no privilege on a heap table is refused for privilege, not told it is not columnar" \
	"$(as t_ssnone "SELECT count(*) FROM pgcolumnar.sort_status('ss_h');" | grep -c 'permission denied for table ss_h')" "1"
check "a superuser asking about a heap table is told it is not columnar" \
	"$(as postgres "SELECT count(*) FROM pgcolumnar.sort_status('ss_h');" | grep -c 'relation "ss_h" is not a columnar table')" "1"

# ---- the superuser path still works ----------------------------------------
check_num "a superuser still reads sort status" \
	"$(q "SELECT count(*) FROM pgcolumnar.sort_status('ss_t');")" "1"

pgc_summary
