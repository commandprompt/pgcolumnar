#!/usr/bin/env bash
#
# pgColumnar: DROP TABLE after SET ACCESS METHOD heap must still take the
# relid-keyed catalogs with it.
#
# Storage-id catalogs (row_group, zone_map, ...) are already gone after the
# rewrite onto heap storage. pgcolumnar.options and projection_declaration are
# keyed by relid. The drop hook used to look at the access method and return
# before touching them, so converting away from columnar and then dropping left
# those rows behind. config_dump emits a bare oid for a relid that no longer
# resolves, and rebuild_projections() aborts on an orphan declaration -- the
# same blast radius #304 closed for a plain DROP of a still-columnar table.
#
# Usage:  test/alter_am_cleanup.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# catalogs keyed by storage id, then the two keyed by relid
snapshot() {
	q "SELECT (SELECT count(*) FROM pgcolumnar.storage) || '/' ||
		(SELECT count(*) FROM pgcolumnar.projection) || '/' ||
		(SELECT count(*) FROM pgcolumnar.row_group) || '/' ||
		(SELECT count(*) FROM pgcolumnar.options) || '/' ||
		(SELECT count(*) FROM pgcolumnar.projection_declaration);" | tail -1
}

base="$(snapshot)"

psql_run "CREATE TABLE aac_t (id int, a int, b text) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('aac_t', stripe_row_limit => 5000,
                                        sort_by => ARRAY['id']);"
psql_run "SELECT pgcolumnar.add_projection('aac_t','p1',ARRAY['a','b'],ARRAY['a']);"
psql_run "INSERT INTO aac_t SELECT g, g, 'x' FROM generate_series(1,200) g;"

grew="$(snapshot)"
check "premise: options and a projection declaration were recorded" \
	"$(awk -v a="$base" -v b="$grew" 'BEGIN { print (a == b) ? "no" : "yes" }')" "yes"

psql_run "ALTER TABLE aac_t SET ACCESS METHOD heap;"
check "the table is heap after the conversion" \
	"$(q "SELECT am.amname FROM pg_class c JOIN pg_am am ON am.oid = c.relam
	      WHERE c.relname = 'aac_t'")" "heap"

# Storage-id catalogs are already gone; the relid-keyed rows are what DROP
# used to miss. Do not require them to vanish at convert time: a round-trip
# back to columnar should still see the declared options.
check "converting to heap does not drop the declared options" \
	"$(q "SELECT stripe_row_limit FROM pgcolumnar.options
	      WHERE regclass = 'aac_t'::regclass")" "5000"

psql_run "DROP TABLE aac_t;"
check "DROP after SET ACCESS METHOD heap leaves no options or declarations" \
	"$(snapshot)" "$base"

# Round-trip: convert away and back, then DROP a still-columnar table.
psql_run "CREATE TABLE aac_rt (id int) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('aac_rt', stripe_row_limit => 8000);"
psql_run "ALTER TABLE aac_rt SET ACCESS METHOD heap;"
psql_run "ALTER TABLE aac_rt SET ACCESS METHOD pgcolumnar;"
check "options survive a heap round-trip" \
	"$(q "SELECT stripe_row_limit FROM pgcolumnar.options
	      WHERE regclass = 'aac_rt'::regclass")" "8000"
psql_run "DROP TABLE aac_rt;"
check "a round-tripped table still cleans up on DROP" "$(snapshot)" "$base"

# Control: a never-columnar heap must not be charged for this either.
psql_run "CREATE TABLE aac_h (id int);"
psql_run "DROP TABLE aac_h;"
# kept alive for the VACUUM FULL control further down
psql_run "CREATE TABLE aac_h_ctl (id int);"
check "dropping a heap table that was never columnar leaves the catalogs alone" \
	"$(snapshot)" "$base"

# ---- the hook is armed in EVERY database, not only where the extension is ----
#
# pgcolumnar_object_access reaches pgcolumnar.options to delete the relid-keyed
# rows. The library is preloaded, so the hook runs in a database where CREATE
# EXTENSION never ran and in one where the extension has been dropped. Reaching
# the catalogs there ERRORs, and an ERROR raised inside an object-access hook
# aborts the statement that fired it.
#
# The blast radius is not DROP TABLE. Every table REWRITE builds a transient
# pg_temp_<oid> relation and performDeletion()s it through this same hook, so
# VACUUM FULL, CLUSTER, ALTER COLUMN TYPE and CREATE MATERIALIZED VIEW take the
# path too, and `vacuumdb --full --all` visits every database in the cluster.
#
# Every statement below is its own arm. An unasserted setup statement that fails
# makes the NEXT arm report a failure it did not cause: a CREATE MATERIALIZED
# VIEW left as setup turns the REFRESH arm into "relation does not exist", which
# reddens for the wrong reason and hides which statement the hook actually broke.

# rc=0 on success, otherwise rc=N plus the first ERROR/FATAL line, so a red says
# which statement failed and how rather than only that something did.
aac_dbrun() {
	local db="$1" sql="$2" out rc
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$db" -v ON_ERROR_STOP=1 -At -c "$sql" 2>&1)"
	rc=$?
	[ "$rc" = 0 ] && { printf 'rc=0\n'; return; }
	printf 'rc=%s %s\n' "$rc" "$(printf '%s\n' "$out" | grep -m1 -E 'ERROR|FATAL' | head -c 120)"
}
aac_dbq() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$1" -At -c "$2" 2>/dev/null || true
}

psql_admin "CREATE DATABASE aac_nocx;" >/dev/null 2>&1

check "premise: the extension is absent in the second database" \
	"$(aac_dbq aac_nocx "SELECT count(*) FROM pg_extension WHERE extname='pgcolumnar'")" "0"
check "premise: and installed in this suite's own database" \
	"$(aac_dbq "$PGC_DB" "SELECT count(*) FROM pg_extension WHERE extname='pgcolumnar'")" "1"

check "a plain table is created in a database without the extension" \
	"$(aac_dbrun aac_nocx 'CREATE TABLE nx (i int primary key, t text);')" "rc=0"
check "and dropped there" \
	"$(aac_dbrun aac_nocx 'DROP TABLE nx;')" "rc=0"
check "an explicit DROP of a temp table succeeds there" \
	"$(aac_dbrun aac_nocx 'CREATE TEMP TABLE nxt (i int); DROP TABLE nxt;')" "rc=0"
check "and ON COMMIT DROP commits there" \
	"$(aac_dbrun aac_nocx 'BEGIN; CREATE TEMP TABLE nxo (i int) ON COMMIT DROP; COMMIT;')" "rc=0"

# The rewrite paths. Each drops a transient relation through the hook.
psql_admin "SELECT 1;" >/dev/null 2>&1
check "premise: a table to rewrite exists in that database" \
	"$(aac_dbrun aac_nocx 'CREATE TABLE rw (i int primary key, t text);
	                       INSERT INTO rw SELECT g, g::text FROM generate_series(1,100) g;')" "rc=0"
check "VACUUM FULL succeeds without the extension" \
	"$(aac_dbrun aac_nocx 'VACUUM FULL rw;')" "rc=0"
check "CLUSTER succeeds without the extension" \
	"$(aac_dbrun aac_nocx 'CLUSTER rw USING rw_pkey;')" "rc=0"
check "ALTER COLUMN TYPE succeeds without the extension" \
	"$(aac_dbrun aac_nocx 'ALTER TABLE rw ALTER COLUMN t TYPE varchar(64);')" "rc=0"
check "CREATE MATERIALIZED VIEW succeeds without the extension" \
	"$(aac_dbrun aac_nocx 'CREATE MATERIALIZED VIEW rwm AS SELECT * FROM rw;')" "rc=0"
check "and REFRESH MATERIALIZED VIEW does too" \
	"$(aac_dbrun aac_nocx 'REFRESH MATERIALIZED VIEW rwm;')" "rc=0"
check "TRUNCATE succeeds without the extension" \
	"$(aac_dbrun aac_nocx 'TRUNCATE rw;')" "rc=0"

# Controls. Without these the arms above are satisfied by a hook that never runs
# at all, which would also break the cleanup this suite exists to test.
check "control: VACUUM FULL still succeeds where the extension IS installed" \
	"$(aac_dbrun "$PGC_DB" 'VACUUM FULL aac_h_ctl;')" "rc=0"

# ---- a table in the extension's schema is ours to clean, not ours to skip ----
#
# Skipping the whole pgcolumnar schema leaks a row per user table created there.
# The thing that must be skipped is the extension's OWN MEMBERS, which is a
# different set: DROP EXTENSION drops those as ordinary relations, and opening
# options while options is itself being dropped would fail.

psql_run "CREATE TABLE pgcolumnar.aac_inschema (id int) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('pgcolumnar.aac_inschema', stripe_row_limit => 7000);"
check "premise: a table in the extension's schema recorded its options" \
	"$(q "SELECT count(*) FROM pgcolumnar.options
	      WHERE regclass = 'pgcolumnar.aac_inschema'::regclass")" "1"
psql_run "DROP TABLE pgcolumnar.aac_inschema;"
check "dropping a table that lives in the extension's schema still cleans up" \
	"$(snapshot)" "$base"

# ---- a relation that predates CREATE EXTENSION is not an extension member ----
#
# getExtensionOfObject returns InvalidOid for it, exactly as it does for any
# other non-member, so it is cleaned up rather than skipped. Round-tripped
# through columnar so it has a row to leak if the member test were inverted.

psql_admin "CREATE DATABASE aac_pre;" >/dev/null 2>&1
check "premise: a table exists before the extension does" \
	"$(aac_dbrun aac_pre 'CREATE TABLE early (id int);')" "rc=0"
check "premise: and the extension is installed after it" \
	"$(aac_dbrun aac_pre 'CREATE EXTENSION pgcolumnar;')" "rc=0"
check "premise: the pre-existing table converts to columnar and records options" \
	"$(aac_dbrun aac_pre "ALTER TABLE early SET ACCESS METHOD pgcolumnar;
	                      SELECT pgcolumnar.set_options('early', stripe_row_limit => 6000);")" "rc=0"
check "premise: its options row is really there" \
	"$(aac_dbq aac_pre "SELECT count(*) FROM pgcolumnar.options WHERE regclass = 'early'::regclass")" "1"
check "and dropping it after SET ACCESS METHOD heap leaves no orphan" \
	"$(aac_dbrun aac_pre 'ALTER TABLE early SET ACCESS METHOD heap; DROP TABLE early;')" "rc=0"
check "the pre-extension table's options row is gone" \
	"$(aac_dbq aac_pre "SELECT count(*) FROM pgcolumnar.options")" "0"

pgc_summary
