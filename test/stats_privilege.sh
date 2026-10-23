#!/usr/bin/env bash
#
# pgColumnar: pgcolumnar.stats is usable by a table's owner, and only by a
# caller who may read the table (#560).
#
# stats() is documented as the function "most users" read, and it failed for the
# owner of the table being inspected: it is LANGUAGE sql and selects straight
# from pgcolumnar.row_group, delete_vector and zone_map, and the install script
# contains no GRANT, so those tables are owner-only and the owner of a COLUMNAR
# table is not their owner.
#
# The obvious fix is worse than the defect. GRANTing SELECT on the catalog to
# PUBLIC would publish pgcolumnar.zone_map, which stores per-column minimum,
# maximum and sum for every columnar table: actual column values. That is a
# larger disclosure than the usability bug it fixes.
#
# So the function runs SECURITY DEFINER and does the privilege check itself.
#
# THE TRAP THIS SUITE EXISTS TO CATCH. Inside a SECURITY DEFINER function the
# effective user is the function OWNER, so pg_class_aclcheck(relid, GetUserId(),
# ...) checks the superuser who installed the extension and returns ACLCHECK_OK
# for every relation in the database. It looks exactly like a correct check and
# refuses nobody. Measured on this build:
#
#   plain call    as t_id: cur=t_id     outer=t_id  sess=t_id
#   definer call  as t_id: cur=postgres outer=t_id  sess=t_id
#
# GetOuterUserId() is the caller. The "a role with no privilege is refused" arm
# below is what fails if that is ever changed back to GetUserId().
#
# Usage:  test/stats_privilege.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

ROWS=500

psql_run "DROP ROLE IF EXISTS t_stowner;"
psql_run "DROP ROLE IF EXISTS t_stnone;"
psql_run "DROP ROLE IF EXISTS t_stsel;"
psql_run "CREATE ROLE t_stowner NOSUPERUSER LOGIN;"
psql_run "CREATE ROLE t_stnone NOSUPERUSER LOGIN;"
psql_run "CREATE ROLE t_stsel NOSUPERUSER LOGIN;"
for r in t_stowner t_stnone t_stsel; do
	psql_run "GRANT USAGE ON SCHEMA pgcolumnar TO $r;"
done
psql_run "CREATE TABLE st_t (id int, v text) USING pgcolumnar;"
psql_run "INSERT INTO st_t SELECT g, 'v'||g FROM generate_series(1,$ROWS) g;"
psql_run "ALTER TABLE st_t OWNER TO t_stowner;"
psql_run "REVOKE ALL ON st_t FROM PUBLIC;"
psql_run "GRANT SELECT ON st_t TO t_stsel;"
psql_run "CREATE TABLE st_h (id int);"   # a heap table, readable by no test role
psql_run "REVOKE ALL ON st_h FROM PUBLIC;"

as() { env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U "$1" \
	-d "$PGC_DB" -At -v ON_ERROR_STOP=0 -c "$2" 2>&1 | head -1; }

# ---- premises, each run by the role it is about ----------------------------
check "premise: the owner can open a session" "$(as t_stowner 'SELECT 1;')" "1"
check "premise: the no-privilege role can open a session" "$(as t_stnone 'SELECT 1;')" "1"
check "premise: the granted role can open a session" "$(as t_stsel 'SELECT 1;')" "1"
check "premise: t_stowner really owns the table" \
	"$(q "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relname='st_t';")" "t_stowner"
check "premise: none of these roles is a superuser" \
	"$(q "SELECT bool_and(NOT rolsuper) FROM pg_roles WHERE rolname LIKE 't_st%';")" "t"
check "premise: the owner can read its own table by ordinary SQL" \
	"$(as t_stowner 'SELECT count(*) FROM st_t;')" "$ROWS"
check "premise: the no-privilege role cannot read it by ordinary SQL" \
	"$(as t_stnone 'SELECT count(*) FROM st_t;' | grep -c 'permission denied')" "1"
# Every role against every catalog stats() reads (#1339). One role against row_group
# stayed green with zone_map -- per-column minimum, maximum and sum -- granted to
# PUBLIC, which is the disclosure this suite exists to rule out.
cat_open=""; cat_refused=0
for r in t_stowner t_stnone t_stsel; do
	for c in row_group delete_vector zone_map; do
		# Captured first. psql exits 1 on the refused query, pipefail carries that out
		# of `as`, and so `as ... | grep -q` failed on a line grep had matched:
		# measured, PIPESTATUS (1 0) for all nine.
		cat_got="$(as "$r" "SELECT count(*) FROM pgcolumnar.$c;")"
		if grep -q "permission denied for table $c\$" <<<"$cat_got"; then
			cat_refused=$((cat_refused + 1))
		else
			cat_open+=" $r/$c"
		fi
	done
done
check "premise: the catalog tables are NOT readable by these roles, so a GRANT is not the fix" \
	"refused $cat_refused of 9${cat_open:+ (not refused:$cat_open)}" "refused 9 of 9"

# ---- the defect ------------------------------------------------------------
check_num "the OWNER of the table can read its stats" "$(as t_stowner "SELECT count(*) FROM pgcolumnar.stats('st_t');")" "1"
check_num "a role merely GRANTed select can read them too" "$(as t_stsel "SELECT count(*) FROM pgcolumnar.stats('st_t');")" "1"

# ---- the bar ---------------------------------------------------------------
#
# This is the arm that catches a check reading the DEFINER instead of the caller.
# With GetUserId() the check tests the superuser who owns the function, passes,
# and this role reads the stats of a table it cannot read.
check "a role with no privilege on the table is refused" \
	"$(as t_stnone "SELECT count(*) FROM pgcolumnar.stats('st_t');" | grep -c 'permission denied for table')" "1"
check "and the refusal names the TABLE, not a catalog table it never asked about" \
	"$(as t_stnone "SELECT count(*) FROM pgcolumnar.stats('st_t');" | grep -c 'st_t')" "1"

# ---- the order: privilege before anything else ------------------------------
#
# get_storage_id checks SELECT with GetUserId(), which inside this SECURITY DEFINER
# function is the definer, so it passes for anyone and then says "not a columnar
# table" for a heap table. Only the explicit caller check refuses, and only while
# it runs FIRST: with the two lines swapped (#1657 review, "sidfirst") every check
# above stays green, because on st_t the caller check still refuses afterwards.
# A heap table the caller may not read must be refused for privilege; the control
# shows the same call does say "not a columnar table" to a caller who may read it.
check "a role with no privilege on a heap table is refused for privilege, not told it is not columnar" \
	"$(as t_stnone "SELECT count(*) FROM pgcolumnar.stats('st_h');" | grep -c 'permission denied for table st_h')" "1"
check "a superuser asking about a heap table is told it is not columnar" \
	"$(as postgres "SELECT count(*) FROM pgcolumnar.stats('st_h');" | grep -c 'relation "st_h" is not a columnar table')" "1"

# ---- the superuser path still works ----------------------------------------
check_num "a superuser still reads stats" "$(q "SELECT count(*) FROM pgcolumnar.stats('st_t');")" "1"

pgc_summary
