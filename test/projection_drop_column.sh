#!/usr/bin/env bash
#
# pgColumnar: DROP COLUMN must not invalidate a materialized projection.
#
# Projections are extension metadata rather than pg_depend objects. PostgreSQL
# therefore accepted DROP COLUMN even when a projection stored that column.
# Dropping its sort key left the projection's attnum pointing at a dropped
# pg_attribute row; the next INSERT failed in lookup_type_cache with
# "type with OID 0 does not exist", making the table unwritable.
#
# Until projections can participate in DROP ... CASCADE, reject the dependent
# DROP at its public DDL boundary and tell the operator to drop the projection.
#
# THE OTHER ROUTES TO A COLUMN DROP CASCADE (#2240). ALTER TYPE ... DROP
# ATTRIBUTE ... CASCADE drops the column from every typed table of the type, and
# DROP DOMAIN ... CASCADE drops every column of the domain. Neither passes
# through ALTER TABLE, so the refusal above never saw them: the projection kept
# the dropped attnum, the next INSERT failed with "type with OID 0 does not
# exist", and the declaration kept the name. Both routes need CASCADE, and the
# projection now goes with the column, as any other dependent object would. A
# refusal there is not possible: core drops a column on its own just before
# dropping its table in DROP SCHEMA ... CASCADE, and the drop hook cannot tell
# that from these.
#
# Usage: test/projection_drop_column.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

sqlstate_as() {
	local role="$1" sql="$2" out code
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" \
		-U "$role" -d "$PGC_DB" -Atq -v VERBOSITY=verbose \
		-c "\\set VERBOSITY verbose" -c "$sql" 2>&1)"
	code="$(printf '%s\n' "$out" |
		sed -n 's/.*ERROR:[[:space:]]*\([0-9A-Z]\{5\}\):.*/\1/p' |
		head -1)"
	if [ -n "$code" ]; then printf '%s\n' "$code"; else printf '00000\n'; fi
}
sqlstate() { sqlstate_as postgres "$1"; }

psql_run "CREATE TABLE pdc (id int, payload text, sort_key int) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.add_projection(
	'pdc', 'by_sort', ARRAY['id','sort_key'], ARRAY['sort_key']);"
psql_run "INSERT INTO pdc
	SELECT g, 'row-' || g, g % 10 FROM generate_series(1,1000) g;"

# The pre-statement dependency check must not tell a stranger which column the
# projection covers, or let that role take the lock retained by the owner path.
psql_run "CREATE ROLE pdc_nobody LOGIN;"
proj_state="$(sqlstate_as pdc_nobody 'ALTER TABLE pdc DROP COLUMN sort_key;')"
plain_state="$(sqlstate_as pdc_nobody 'ALTER TABLE pdc DROP COLUMN payload;')"
# The property is that the two refusals are INDISTINGUISHABLE, so it is compared
# first: with both ownership checks deleted, the stranger gets 2BP01 for the
# projected column and 42501 for the other, and this is the check that names it.
check "and the two refusals are the same error, so neither leaks" \
	"$proj_state" "$plain_state"
check "a non-owner learns nothing from a projected column" "$proj_state" "42501"
check "the same non-owner error is returned for an unprojected column" \
	"$plain_state" "42501"

# THE LOCK HALF OF THE COMMENT ABOVE (#1339). The ownership check runs twice:
# once with no lock, then again after ShareUpdateExclusiveLock. The two checks
# above cannot tell them apart, because both raise 42501. Contention can: while
# the owner holds SHARE on pdc, which conflicts with ShareUpdateExclusiveLock, a
# stranger refused by the unlocked check gets 42501 at once, and one that reaches
# the lock first waits out its lock_timeout (55P03).
holder_log="$PGC_WORKDIR/pdc_holder.log"
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
	-Atq -v ON_ERROR_STOP=0 -c "BEGIN;" -c "LOCK TABLE pdc IN SHARE MODE;" \
	-c "SELECT pg_sleep(3600) /* pdc_holder */;" -c "ROLLBACK;" > "$holder_log" 2>&1 &
holder_pid=$!
held=0
for _ in $(seq 1 40); do
	held="$(q "SELECT count(*) FROM pg_locks WHERE relation = 'pdc'::regclass AND mode = 'ShareLock' AND granted;")"
	[ "${held:-0}" -ge 1 ] && break
	sleep 0.25
done
check "premise: the owner holds SHARE on pdc before the stranger asks" "${held:-0}" "1"
check "a non-owner is refused before it waits for the table lock" \
	"$(sqlstate_as pdc_nobody "SET lock_timeout = '2s'; ALTER TABLE pdc DROP COLUMN sort_key;")" "42501"
psql_run "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE query LIKE '%pdc_holder%' AND pid <> pg_backend_pid();" >/dev/null
wait "$holder_pid" 2>/dev/null || true

# Red before the fix: PostgreSQL accepts this (00000), and the next INSERT
# reaches the projection writer with a type OID of zero.
check "dropping a projected column is refused as a dependency" \
	"$(sqlstate 'ALTER TABLE pdc DROP COLUMN sort_key;')" "2BP01"

psql_run "INSERT INTO pdc VALUES (1001, 'still-writable', 1);"
check "the rejected DDL leaves the table writable" \
	"$(q 'SELECT count(*) FROM pdc')" "1001"
check "and the projection still receives the row" \
	"$(q "SELECT count(*) FROM pgcolumnar.read_projection('pdc','by_sort')")" \
	"1001"

# A column outside every projection remains ordinary DDL.
psql_run "ALTER TABLE pdc DROP COLUMN payload;"
psql_run "INSERT INTO pdc VALUES (1002, 2);"
check "dropping an unrelated column remains allowed" \
	"$(q 'SELECT count(*) FROM pdc')" "1002"

# A partitioned parent has no storage itself, but DROP COLUMN recurses into its
# columnar partitions. The dependency check must walk the same hierarchy.
psql_run "CREATE TABLE pdc_parent (id int, sort_key int)
	PARTITION BY RANGE (id);"
psql_run "CREATE TABLE pdc_child PARTITION OF pdc_parent
	FOR VALUES FROM (0) TO (100) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.add_projection(
	'pdc_child', 'child_sort', ARRAY['id','sort_key'], ARRAY['sort_key']);"
psql_run "INSERT INTO pdc_parent VALUES (1, 1);"
check "a parent DROP sees projections on columnar partitions" \
	"$(sqlstate 'ALTER TABLE pdc_parent DROP COLUMN sort_key;')" "2BP01"
psql_run "INSERT INTO pdc_parent VALUES (2, 2);"
check "the rejected parent DDL leaves its partition writable" \
	"$(q 'SELECT count(*) FROM pdc_parent')" "2"

# ---------------------------------------------------------------------------
# #2240: a column dropped by a cascade takes its projections with it.
# ---------------------------------------------------------------------------
crashes() {	# a number, or nothing when the log can't be read
	local _n
	_n="$(pgc_pg "grep -c 'terminated by signal' '$PGC_LOGFILE'" 2>/dev/null)"
	case "$_n" in '' | *[!0-9]*) ;; *) echo "$_n" ;; esac
}
# Runs one statement and prints only its "drop cascades to projection" notices,
# sorted, one per line, or "none".
cascade_notices() {
	local out
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" \
		-U postgres -d "$PGC_DB" -Atq -c "$1" 2>&1)"
	out="$(printf '%s\n' "$out" |
		sed -n 's/^.*NOTICE:[[:space:]]*\(drop cascades to projection .*\)$/\1/p' |
		LC_ALL=C sort)"
	if [ -n "$out" ]; then printf '%s\n' "$out"; else echo none; fi
}
# The declarations of one table, as name:columns, sorted; "none" when empty.
decls() {
	local out
	out="$(q "SELECT string_agg(name || ':' || columns::text, ' ' ORDER BY name COLLATE \"C\")
		FROM pgcolumnar.projection_declaration WHERE rel = '$1'::regclass;")"
	if [ -n "$out" ]; then echo "$out"; else echo none; fi
}
# The materialized projections of one table, as name:columns, sorted, base excluded.
projs() {
	local out
	out="$(q "SELECT string_agg(name || ':' || columns::text, ' ' ORDER BY name COLLATE \"C\")
		FROM pgcolumnar.projection
		WHERE storage_id = pgcolumnar.get_storage_id('$1') AND projection_id <> 0;")"
	if [ -n "$out" ]; then echo "$out"; else echo none; fi
}
uses() {	# uses TABLE COL -> the projection EXPLAIN names, or "base"
	local out
	out="$(q "EXPLAIN (COSTS OFF) SELECT a, $2 FROM $1 WHERE $2 BETWEEN 2 AND 3;" |
		sed -n 's/^[[:space:]]*Columnar Projection: //p')"
	if [ -n "$out" ]; then echo "$out"; else echo base; fi
}
crash0="$(crashes)"

# --- ALTER TYPE ... DROP ATTRIBUTE ... CASCADE, two typed tables of one type ---
#
# Two columnar tables of the type, because the cascade reaches each one by its
# own column drop and a fix that handled only the first would pass with one.
# The heap table of the same type is the oracle, and loses bb in the same
# statement. "BB" differs from bb only in case, and its projection must stay.
psql_run 'CREATE TYPE pdc_pt AS (a int, bb int, "BB" int);'
for t in pdc_tp1 pdc_tp2; do
	psql_run "CREATE TABLE $t OF pdc_pt USING pgcolumnar;"
	psql_run "SELECT pgcolumnar.set_options('$t', stripe_row_limit => 1000, chunk_group_row_limit => 500);"
	psql_run "SELECT pgcolumnar.add_projection('$t', 'pj', ARRAY['bb','a'], ARRAY['bb']);"
done
psql_run "CREATE TABLE pdc_tph OF pdc_pt;"
psql_run "SELECT pgcolumnar.add_projection('pdc_tp1', 'pj_upper', ARRAY['BB','a'], ARRAY['BB']);"
for t in pdc_tp1 pdc_tp2 pdc_tph; do
	psql_run "INSERT INTO $t SELECT g, g % 100, g % 70 FROM generate_series(1, 20000) g;"
	psql_run "ANALYZE $t;"
done
pj1_sid="$(q "SELECT proj_storage_id FROM pgcolumnar.projection WHERE storage_id = pgcolumnar.get_storage_id('pdc_tp1') AND name = 'pj';")"
pj2_sid="$(q "SELECT proj_storage_id FROM pgcolumnar.projection WHERE storage_id = pgcolumnar.get_storage_id('pdc_tp2') AND name = 'pj';")"
check "premise: the planner reads pj on both typed tables before the drop" \
	"$(uses pdc_tp1 bb) $(uses pdc_tp2 bb)" "pj pj"
check "premise: both pj projections hold row groups before the drop" \
	"$(q "SELECT count(*) > 0 FROM pgcolumnar.row_group WHERE storage_id = ${pj1_sid:-0}") $(q "SELECT count(*) > 0 FROM pgcolumnar.row_group WHERE storage_id = ${pj2_sid:-0}")" \
	"t t"

# Core refuses this without CASCADE, before any column is dropped.
check "premise: without CASCADE, ALTER TYPE ... DROP ATTRIBUTE is refused" \
	"$(sqlstate 'ALTER TYPE pdc_pt DROP ATTRIBUTE bb;')" "2BP01"
check "premise: and the refused ALTER TYPE leaves every declaration" \
	"$(decls pdc_tp1) | $(decls pdc_tp2)" "pj:{bb,a} pj_upper:{BB,a} | pj:{bb,a}"

# Red before the fix: the cascade succeeded and left pj declared and
# materialized on both tables, with no notice.
check "ALTER TYPE ... DROP ATTRIBUTE CASCADE reports each projection it drops" \
	"$(cascade_notices 'ALTER TYPE pdc_pt DROP ATTRIBUTE bb CASCADE;')" \
	"drop cascades to projection \"pj\" on table \"pdc_tp1\"
drop cascades to projection \"pj\" on table \"pdc_tp2\""
check "the cascade drops pj's declaration on the first typed table, and keeps the one on BB" \
	"$(decls pdc_tp1)" "pj_upper:{BB,a}"
check "the cascade drops pj's declaration on the second typed table" \
	"$(decls pdc_tp2)" "none"
check "the cascade drops pj's projection on both typed tables, and keeps the one on BB" \
	"$(projs pdc_tp1) | $(projs pdc_tp2)" "pj_upper:{3,1} | none"
check "the dropped projections' row groups are freed" \
	"$(q "SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id IN (${pj1_sid:-0}, ${pj2_sid:-0})")" "0"
check "the projection on BB still holds every row" \
	"$(q "SELECT count(*) FROM pgcolumnar.read_projection('pdc_tp1', 'pj_upper')")" "20000"

# Red before the fix: "type with OID 0 does not exist".
for t in pdc_tp1 pdc_tp2 pdc_tph; do
	psql_run "INSERT INTO $t VALUES (20001, 5);"
done
check "both typed tables stay writable after the cascade" \
	"$(q 'SELECT count(*) FROM pdc_tp1') $(q 'SELECT count(*) FROM pdc_tp2')" "20001 20001"

# bb comes back under the same name, with a new attnum and new values. The
# dropped projection must not answer for it.
psql_run "ALTER TYPE pdc_pt ADD ATTRIBUTE bb int CASCADE;"
for t in pdc_tp1 pdc_tp2 pdc_tph; do
	psql_run "INSERT INTO $t (a, bb, \"BB\") SELECT g, (g * 7) % 100, g % 70 FROM generate_series(20002, 24000) g;"
	psql_run "ANALYZE $t;"
done
check "after bb is added back, the planner reads the base on both typed tables" \
	"$(uses pdc_tp1 bb) $(uses pdc_tp2 bb)" "base base"
check "after bb is added back, both typed tables answer as heap does" \
	"$(pgc_set_hash "SELECT a, bb FROM pdc_tp1 WHERE bb BETWEEN 2 AND 3") $(pgc_set_hash "SELECT a, bb FROM pdc_tp2 WHERE bb BETWEEN 2 AND 3")" \
	"$(pgc_set_hash "SELECT a, bb FROM pdc_tph WHERE bb BETWEEN 2 AND 3") $(pgc_set_hash "SELECT a, bb FROM pdc_tph WHERE bb BETWEEN 2 AND 3")"
check "add_projection takes the name pj again on the added-back bb" \
	"$(sqlstate "SELECT pgcolumnar.add_projection('pdc_tp1', 'pj', ARRAY['bb','a'], ARRAY['bb']);")" "00000"
psql_run "ANALYZE pdc_tp1;"
check "and the planner reads the new pj, which answers as heap does" \
	"$(uses pdc_tp1 bb) $(pgc_set_hash "SELECT a, bb FROM pdc_tp1 WHERE bb BETWEEN 2 AND 3")" \
	"pj $(pgc_set_hash "SELECT a, bb FROM pdc_tph WHERE bb BETWEEN 2 AND 3")"

# --- DROP DOMAIN ... CASCADE on a plain table --------------------------------
#
# pd and pd2 store c, the domain column, and go. c is pd's first column and
# pd2's last, so a loop that stops early misses one. pd_decl names c too
# but has no materialized projection, as a rewrite that could not re-record
# it leaves; it is made here by deleting its projection row. pd_upper stores
# "C", pa stores neither, and pd on pdc_do is another table's projection of
# the same name on a column of the same name: all three stay.
psql_run "CREATE DOMAIN pdc_d AS int;"
psql_run 'CREATE TABLE pdc_dt (a int, c pdc_d, "C" int) USING pgcolumnar;'
psql_run 'CREATE TABLE pdc_dh (a int, c pdc_d, "C" int);'
psql_run "CREATE TABLE pdc_do (a int, c int) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('pdc_dt', stripe_row_limit => 1000, chunk_group_row_limit => 500);"
psql_run "SELECT pgcolumnar.add_projection('pdc_dt', 'pd', ARRAY['c','a'], ARRAY['c']);"
psql_run "SELECT pgcolumnar.add_projection('pdc_dt', 'pd_upper', ARRAY['C','a'], ARRAY['C']);"
psql_run "SELECT pgcolumnar.add_projection('pdc_dt', 'pa', ARRAY['a']);"
psql_run "SELECT pgcolumnar.add_projection('pdc_dt', 'pd2', ARRAY['a','c']);"
psql_run "SELECT pgcolumnar.add_projection('pdc_dt', 'pd_decl', ARRAY['a','c']);"
psql_run "SELECT pgcolumnar.add_projection('pdc_do', 'pd', ARRAY['c','a'], ARRAY['c']);"
psql_run "DELETE FROM pgcolumnar.projection WHERE storage_id = pgcolumnar.get_storage_id('pdc_dt') AND name = 'pd_decl';"
for t in pdc_dt pdc_dh; do
	psql_run "INSERT INTO $t SELECT g, g % 100, g % 70 FROM generate_series(1, 20000) g;"
	psql_run "ANALYZE $t;"
done
psql_run "INSERT INTO pdc_do SELECT g, g % 100 FROM generate_series(1, 20000) g;"
check "premise: the planner reads pd before the drop" "$(uses pdc_dt c)" "pd"
check "premise: pd_decl is declared with no materialized projection" \
	"$(decls pdc_dt) | $(projs pdc_dt)" \
	"pa:{a} pd:{c,a} pd2:{a,c} pd_decl:{a,c} pd_upper:{C,a} | pa:{1} pd:{2,1} pd2:{1,2} pd_upper:{3,1}"

check "DROP DOMAIN ... CASCADE reports each projection it drops" \
	"$(cascade_notices 'DROP DOMAIN pdc_d CASCADE;')" \
	"drop cascades to projection \"pd\" on table \"pdc_dt\"
drop cascades to projection \"pd2\" on table \"pdc_dt\"
drop cascades to projection \"pd_decl\" on table \"pdc_dt\""
check "DROP DOMAIN ... CASCADE drops the declarations naming c, a materialized one or not" \
	"$(decls pdc_dt)" "pa:{a} pd_upper:{C,a}"
check "DROP DOMAIN ... CASCADE drops every projection storing c and keeps the others" \
	"$(projs pdc_dt)" "pa:{1} pd_upper:{3,1}"
check "another table's projection of the same name on a column of the same name stays" \
	"$(decls pdc_do) | $(projs pdc_do)" "pd:{c,a} | pd:{2,1}"

for t in pdc_dt pdc_dh; do
	psql_run "INSERT INTO $t VALUES (20001, 5);"
done
check "the table stays writable after DROP DOMAIN ... CASCADE" \
	"$(q 'SELECT count(*) FROM pdc_dt')" "20001"

psql_run "ALTER TABLE pdc_dt ADD COLUMN c int;"
psql_run "ALTER TABLE pdc_dh ADD COLUMN c int;"
for t in pdc_dt pdc_dh; do
	psql_run "INSERT INTO $t (a, \"C\", c) SELECT g, g % 70, (g * 7) % 100 FROM generate_series(20002, 24000) g;"
	psql_run "ANALYZE $t;"
done
check "after c is added back, the planner reads the base and answers as heap does" \
	"$(uses pdc_dt c) $(pgc_set_hash "SELECT a, c FROM pdc_dt WHERE c BETWEEN 2 AND 3")" \
	"base $(pgc_set_hash "SELECT a, c FROM pdc_dh WHERE c BETWEEN 2 AND 3")"
check "add_projection takes the name pd again on the added-back c" \
	"$(sqlstate "SELECT pgcolumnar.add_projection('pdc_dt', 'pd', ARRAY['c','a'], ARRAY['c']);")" "00000"
psql_run "ANALYZE pdc_dt;"
check "and the planner reads the new pd, which answers as heap does" \
	"$(uses pdc_dt c) $(pgc_set_hash "SELECT a, c FROM pdc_dt WHERE c BETWEEN 2 AND 3")" \
	"pd $(pgc_set_hash "SELECT a, c FROM pdc_dh WHERE c BETWEEN 2 AND 3")"

# A write, the cascade and another write in one transaction. The second write
# must not reach the dropped projection's storage through a writer cached by
# the first.
psql_run "CREATE DOMAIN pdc_dx AS int;"
psql_run "CREATE TABLE pdc_x (a int, b int, c pdc_dx) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.add_projection('pdc_x', 'pxb', ARRAY['b','a'], ARRAY['b']);"
psql_run "SELECT pgcolumnar.add_projection('pdc_x', 'pxc', ARRAY['c','a'], ARRAY['c']);"
psql_run "INSERT INTO pdc_x SELECT g, g % 10, g % 7 FROM generate_series(1, 100) g;"
pxc_sid="$(q "SELECT proj_storage_id FROM pgcolumnar.projection WHERE storage_id = pgcolumnar.get_storage_id('pdc_x') AND name = 'pxc';")"
psql_run "BEGIN;
	INSERT INTO pdc_x SELECT g, g % 10, g % 7 FROM generate_series(101, 200) g;
	DROP DOMAIN pdc_dx CASCADE;
	INSERT INTO pdc_x SELECT g, g % 10 FROM generate_series(201, 300) g;
	COMMIT;" >/dev/null 2>&1
check "a write, the cascade and a write in one transaction leave the dropped storage empty" \
	"$(q 'SELECT count(*) FROM pdc_x') $(q "SELECT count(*) FROM pgcolumnar.read_projection('pdc_x', 'pxb')") $(q "SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = ${pxc_sid:-0}")" \
	"300 300 0"

# WHY THE CASCADE DROPS RATHER THAN REFUSES. The table predates the domain, so
# DROP SCHEMA ... CASCADE drops its column c on its own, just before the table
# itself. The drop hook sees that column drop as it sees DROP DOMAIN's, and a
# refusal there failed this DROP SCHEMA.
psql_run "CREATE SCHEMA pdc_s;"
psql_run "CREATE TABLE pdc_s.t (a int) USING pgcolumnar;"
psql_run "CREATE DOMAIN pdc_s.d AS int;"
psql_run "ALTER TABLE pdc_s.t ADD COLUMN c pdc_s.d;"
psql_run "INSERT INTO pdc_s.t SELECT g, g % 10 FROM generate_series(1, 1000) g;"
psql_run "SELECT pgcolumnar.add_projection('pdc_s.t', 'ps', ARRAY['c','a'], ARRAY['c']);"
pdc_s_oid="$(q "SELECT 'pdc_s.t'::regclass::oid;")"
check "DROP SCHEMA ... CASCADE drops a table whose projection stores a later domain's column" \
	"$(sqlstate 'DROP SCHEMA pdc_s CASCADE;') $(q "SELECT count(*) FROM pgcolumnar.projection_declaration WHERE rel::oid = ${pdc_s_oid:-0}")" \
	"00000 0"

check "no backend crashed during the cascade arms" "$(crashes)" "${crash0:-unread}"

pgc_summary
