#!/usr/bin/env bash
#
# pgColumnar: direct-storage paths refuse a relation with RLS in force (#563).
#
# Row-level security is applied by the REWRITER to a query's range table entry.
# These functions never build a query over the relation: they open its storage
# and read or write it, so there is nothing for the rewriter to act on and every
# policy is silently bypassed.
#
# THE FIXTURE THAT MATTERS IS A ROLE THAT HOLDS SELECT AND IS POLICY-RESTRICTED.
# A role with no privilege proves nothing here, because #559's and #562's
# pg_class_aclcheck already refuses it: delete the RLS guard entirely and a
# no-privilege arm stays green. Only a granted-but-restricted caller reaches the
# RLS check with every other refuser cleared, which is the same single-refuser
# isolation as the import/export suite, one layer further in.
#
# The bar is check_enable_rls(relid, InvalidOid, true) == RLS_ENABLED:
#   RLS_NONE      no policy on the table            -> allowed
#   RLS_NONE_ENV  superuser, BYPASSRLS, unforced owner -> allowed, they already
#                 read everything, and refusing them breaks admin tooling
#   RLS_ENABLED   a policy applies to this caller   -> refused
# noError=true so the verdict does not swing with the row_security GUC.
#
# Usage:  test/rls_direct_storage.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# parallel_copy prepares one transaction per worker and parallel_export spawns
# workers. max_prepared_transactions is PGC_POSTMASTER, so it has to be set
# before the cluster starts or those two arms fail on capacity rather than on
# the thing under test.
export PGC_EXTRA_CONF=$'max_prepared_transactions=8\nmax_worker_processes=16'
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

ROWS=200
OUT="$(mktemp -d)"; chmod 777 "$OUT"

psql_run "CREATE TABLE rls_t (id int, owner_name text, ssn text) USING pgcolumnar;"
psql_run "INSERT INTO rls_t SELECT g, CASE WHEN g = 1 THEN 't_rls' ELSE 'someone' END, 'ssn-'||g FROM generate_series(1,$ROWS) g;"
psql_run "CREATE TABLE plain_t (id int, v text) USING pgcolumnar;"
psql_run "INSERT INTO plain_t SELECT g, 'v'||g FROM generate_series(1,$ROWS) g;"
psql_run "SELECT pgcolumnar.add_projection('rls_t','p1',ARRAY['id'],ARRAY['id']);"
psql_run "SELECT pgcolumnar.add_projection('plain_t','p1',ARRAY['id'],ARRAY['id']);"
psql_run "SELECT pgcolumnar.rebuild_projections('rls_t');"
psql_run "SELECT pgcolumnar.rebuild_projections('plain_t');"

psql_run "DROP ROLE IF EXISTS t_rls;"
psql_run "CREATE ROLE t_rls NOSUPERUSER LOGIN;"
psql_run "GRANT USAGE ON SCHEMA pgcolumnar TO t_rls;"
psql_run "GRANT pg_read_server_files, pg_write_server_files TO t_rls;"
# SELECT and INSERT are granted DELIBERATELY. This role must clear every bar
# except the RLS one.
psql_run "GRANT SELECT, INSERT ON rls_t TO t_rls;"
psql_run "GRANT SELECT, INSERT ON plain_t TO t_rls;"
# #567 revoked EXECUTE on the projection readers from PUBLIC. Without these
# grants the projection arms are refused by the SQL grant layer and never reach
# the RLS check, so they would report acl and prove nothing about this fix.
psql_run "GRANT EXECUTE ON FUNCTION pgcolumnar.read_projection(regclass,text) TO t_rls;"
psql_run "GRANT EXECUTE ON FUNCTION pgcolumnar.reconstruct_via_projection(regclass,text) TO t_rls;"
# A source file the role may import, produced by the owner from the open table,
# BEFORE row-level security is enabled, so no version of the guard under test can
# stop the fixture from being made.
# Exported from rls_t BY THE OWNER, so the source shape matches the import
# target. A source taken from a differently shaped table makes the import arms
# fail on column counts, which looks like a refusal and is not one.
psql_run "SELECT pgcolumnar.export_parquet('rls_t','$OUT/src.parquet');"
psql_run "SELECT pgcolumnar.export_arrow('rls_t','$OUT/src.arrow');"
psql_run "COPY rls_t TO '$OUT/src.txt';"
psql_run "ALTER TABLE rls_t ENABLE ROW LEVEL SECURITY;"
psql_run "CREATE POLICY p_own ON rls_t FOR SELECT TO t_rls USING (owner_name = current_user);"
psql_run "CREATE POLICY p_ins ON rls_t FOR INSERT TO t_rls WITH CHECK (owner_name = current_user);"

as_rls() {  # as_rls <sql> -> the ERROR line, connfail, or noerror
	local out
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U t_rls \
		-d "$PGC_DB" -At -v ON_ERROR_STOP=0 -c "$1" 2>&1)"
	case "$out" in
		*"psql: error"*|*"could not connect"*|*"FATAL:"*) echo connfail ;;
		*ERROR:*) printf '%s\n' "$out" | grep -m1 'ERROR:' ;;
		*)        echo noerror ;;
	esac
}
verdict() {  # verdict <sql> -> rls | acl | allowed | other
	local e; e="$(as_rls "$1")"
	case "$e" in
		noerror)                       echo allowed ;;
		*"row-level security"*)        echo rls ;;
		*"permission denied"*)         echo acl ;;
		*)                             echo "other: $e" ;;
	esac
}

# ---- premises, every one run BY the restricted role -------------------------
#
# Two of these read the table through the columnar scan, so a product change can
# redden them, and they are checks, not premises (#1236; #1397 review): the policy's
# row count, which the custom scan dropping its quals turned into every row, and the
# owner's count. The source files are a third: the owner's export writes them, so an
# export that writes nothing reddens that check (#1383 review).
check "premise: the role can open a session" \
	"$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U t_rls -d "$PGC_DB" -At -c 'SELECT 1;' 2>&1 | head -1)" "1"
check "premise: the role HOLDS select, so the ACL check cannot be what refuses" \
	"$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U t_rls -d "$PGC_DB" -At \
		-c "SELECT has_table_privilege('rls_t','SELECT');" 2>&1 | head -1)" "t"
check_num "the policy is in force for ordinary SQL" \
	"$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U t_rls -d "$PGC_DB" -At \
		-c 'SELECT count(*) FROM rls_t;' 2>&1 | head -1)" "1"
check "premise: the role holds EXECUTE, so the grant layer is not what refuses" \
	"$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U t_rls -d "$PGC_DB" -At \
		-c "SELECT has_function_privilege('pgcolumnar.read_projection(regclass,text)','EXECUTE');" 2>&1 | head -1)" "t"
check_num "the owner sees every row, so the policy hides something real" \
	"$(q 'SELECT count(*) FROM rls_t;')" "$ROWS"
check "the source files the import arms need exist" \
	"$([ -s "$OUT/src.parquet" ] && [ -s "$OUT/src.arrow" ] && [ -s "$OUT/src.txt" ] && echo yes || echo no)" "yes"

# ---- the refusals -----------------------------------------------------------
#
# Each of these reads or writes rls_t's storage directly for a caller a policy
# restricts. On unfixed code they all return "allowed", which is the leak.
check "read_projection refuses a policy-restricted caller" \
	"$(verdict "SELECT count(*) FROM pgcolumnar.read_projection('rls_t','p1');")" "rls"
check "reconstruct_via_projection refuses one too" \
	"$(verdict "SELECT count(*) FROM pgcolumnar.reconstruct_via_projection('rls_t','p1');")" "rls"
check "export_parquet refuses one" \
	"$(verdict "SELECT pgcolumnar.export_parquet('rls_t','$OUT/leak1.parquet');")" "rls"
check "export_arrow refuses one" \
	"$(verdict "SELECT pgcolumnar.export_arrow('rls_t','$OUT/leak2.arrow');")" "rls"
check "parallel_export_parquet refuses one" \
	"$(verdict "SELECT pgcolumnar.parallel_export_parquet('rls_t','$OUT/leak3',2);")" "rls"
check "import_parquet refuses a policy-restricted writer" \
	"$(verdict "SELECT pgcolumnar.import_parquet('rls_t','$OUT/src.parquet');")" "rls"
check "import_arrow refuses one" \
	"$(verdict "SELECT pgcolumnar.import_arrow('rls_t','$OUT/src.arrow');")" "rls"
check "parallel_copy refuses one" \
	"$(verdict "SELECT pgcolumnar.parallel_copy('rls_t','$OUT/src.txt',2);")" "rls"

# The refusal must not have done the work anyway. A message is not a behaviour.
check "no export file was written for the refused caller" \
	"$([ -e "$OUT/leak1.parquet" ] || [ -e "$OUT/leak2.arrow" ] || [ -e "$OUT/leak3" ] && echo written || echo none)" "none"
check_num "and no imported row landed in the restricted table" "$(q 'SELECT count(*) FROM rls_t;')" "$ROWS"

# ---- a caller WITHOUT privilege sees the ACL error, not the RLS one ---------
#
# The guard sits below the ACL check so these functions agree with core, which
# applies the ACL first. Above it, a caller with no privilege on an RLS table was
# told "row-level security is in force", which discloses RLS state that ordinary
# SQL does not. Found in review; this arm is what would notice the order being
# swapped back.
psql_run "DROP ROLE IF EXISTS t_rlsnone;"
psql_run "CREATE ROLE t_rlsnone NOSUPERUSER LOGIN;"
psql_run "GRANT USAGE ON SCHEMA pgcolumnar TO t_rlsnone;"
psql_run "GRANT EXECUTE ON FUNCTION pgcolumnar.read_projection(regclass,text) TO t_rlsnone;"
as_none() { env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U t_rlsnone \
	-d "$PGC_DB" -At -v ON_ERROR_STOP=0 -c "$1" 2>&1 | head -1; }
check "premise: the no-privilege role is told permission denied by ordinary SQL" \
	"$(as_none 'SELECT count(*) FROM rls_t;' | grep -c 'permission denied')" "1"
check "and read_projection tells it the same thing, not that RLS exists" \
	"$(as_none "SELECT count(*) FROM pgcolumnar.read_projection('rls_t','p1');" | grep -c 'permission denied')" "1"
check "so no caller without privilege learns RLS state from these functions" \
	"$(as_none "SELECT count(*) FROM pgcolumnar.read_projection('rls_t','p1');" | grep -c 'row-level security')" "0"

# ---- not over-refusing ------------------------------------------------------
#
# Without these the fix could pass by refusing everyone, which is a broken
# function with a tidy message rather than a fix.
check "a table with NO policy is still readable by the same role" \
	"$(verdict "SELECT count(*) FROM pgcolumnar.read_projection('plain_t','p1');")" "allowed"
check "and still exportable by it" \
	"$(verdict "SELECT pgcolumnar.export_parquet('plain_t','$OUT/ok1.parquet');")" "allowed"
# Compared against the table's LIVE count, not the starting constant. On unfixed
# code the import arms above leak rows into rls_t, so a constant here fails for
# the previous arm's reason rather than this one and reports the wrong defect.
owner_rows="$(q 'SELECT count(*) FROM rls_t;')"
check "the OWNER can still read the RLS table's storage (RLS_NONE_ENV)" \
	"$(q "SELECT count(*) FROM pgcolumnar.read_projection('rls_t','p1');" 2>&1 | head -1)" "$owner_rows"
# Measured as the rows the export holds, read back: both branches of the old test
# here echoed "ran", so a refused owner export passed it.
q "SELECT pgcolumnar.export_parquet('rls_t','$OUT/owner.parquet');" >/dev/null 2>&1
check_num "and the owner can still export it" \
	"$([ -s "$OUT/owner.parquet" ] && q "SELECT count(*) FROM pgcolumnar.read_parquet('$OUT/owner.parquet') AS (id int, owner_name text, ssn text);" 2>&1 | head -1 || echo 0)" "$owner_rows"

# ---- FORCE ROW LEVEL SECURITY reaches the owner too -------------------------
#
# This is why "declare the surface owner-only" is not a substitute for the fix:
# under FORCE, check_enable_rls returns RLS_ENABLED for the owner as well, so an
# owner-only rule would still leak.
# The owner must be a NON-SUPERUSER for this to mean anything. A superuser
# bypasses RLS unconditionally and check_enable_rls returns RLS_NONE_ENV for one
# even under FORCE, so running this as postgres asserts nothing. That is a real
# distinction and not pedantry: FORCE reaches a table's owner, never a superuser.
psql_run "DROP TABLE IF EXISTS force_t;"
psql_run "DROP ROLE IF EXISTS t_owner;"
psql_run "CREATE ROLE t_owner NOSUPERUSER LOGIN;"
psql_run "GRANT USAGE ON SCHEMA pgcolumnar TO t_owner;"
psql_run "CREATE TABLE force_t (id int, owner_name text) USING pgcolumnar;"
psql_run "INSERT INTO force_t SELECT g, 'nobody' FROM generate_series(1,50) g;"
psql_run "SELECT pgcolumnar.add_projection('force_t','p1',ARRAY['id'],ARRAY['id']);"
psql_run "SELECT pgcolumnar.rebuild_projections('force_t');"
psql_run "ALTER TABLE force_t OWNER TO t_owner;"
psql_run "GRANT EXECUTE ON FUNCTION pgcolumnar.read_projection(regclass,text) TO t_owner;"
psql_run "ALTER TABLE force_t ENABLE ROW LEVEL SECURITY;"
psql_run "CREATE POLICY p_none ON force_t FOR SELECT TO t_owner USING (owner_name = current_user);"

as_owner() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U t_owner \
		-d "$PGC_DB" -At -v ON_ERROR_STOP=0 -c "$1" 2>&1 | head -1
}
check "premise: t_owner owns force_t" \
	"$(q "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relname='force_t';")" "t_owner"
# WITHOUT force, an owner is RLS_NONE_ENV and must still be allowed. This is the
# arm that stops the fix from degenerating into owner-hostile over-refusal.
check "without FORCE the owner is allowed, because RLS_NONE_ENV is not RLS_ENABLED" \
	"$(as_owner "SELECT count(*) FROM pgcolumnar.read_projection('force_t','p1');")" "50"
psql_run "ALTER TABLE force_t FORCE ROW LEVEL SECURITY;"
check "under FORCE the non-superuser owner IS refused, so owner-only is no substitute" \
	"$(as_owner "SELECT count(*) FROM pgcolumnar.read_projection('force_t','p1');" | grep -c 'row-level security')" "1"

rm -rf "$OUT"
pgc_summary
