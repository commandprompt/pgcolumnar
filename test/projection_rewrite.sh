#!/usr/bin/env bash
#
# pgColumnar: a declared projection must survive a rewrite (#876, #887).
#
# #876 reports the symptom: pgcolumnar.read_projection raises 42704 after a
# rewrite, for a projection that is still declared over an intact base table.
# Three properties must hold after ANY rewrite, and before this suite nothing in
# the tree asserted the first of them:
#
#   P1  read_projection answers, and holds exactly the rows the base table holds.
#   P2  no pgcolumnar.projection row names a storage id the table no longer has.
#   P3  the declaration survives, so a rebuild is always possible.
#
# Two traps this suite is built around.
#
# An arm that asserts only "read_projection did not raise" passes on a tree where
# the projection was re-recorded EMPTY, so every arm compares pgc_set_hash
# against the base table rather than counting rows or checking for an error.
#
# And an operation that FAILED or that no-opped leaves the storage id unchanged
# and read_projection answering -- indistinguishable from an operation that
# handled projections correctly. Three rows of #887's table were vacuous that
# way. So every arm asserts what the operation DID (REWROTE / NOOP / FAILED)
# before its outcome is allowed to mean anything.
#
# Usage:  test/projection_rewrite.sh [PG_CONFIG]
#
# Written fresh for pgColumnar; it does not reuse any upstream test file.

set -uo pipefail

. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

N=5000

# ---------------------------------------------------------------------------
# Premises. Every verdict below is vacuous if the fixture is not what it says,
# so these run first and are checks in their own right.
# ---------------------------------------------------------------------------
psql_run "CREATE TABLE prem (id int, a int, b text) USING pgcolumnar;"
psql_run "INSERT INTO prem SELECT g, g%50, 'b'||g FROM generate_series(1,$N) g;"
psql_run "SELECT pgcolumnar.add_projection('prem','pp',ARRAY['a','b'],ARRAY['a']);"
check "PREMISE base holds its rows" "$(q 'SELECT count(*) FROM prem;')" "$N"
check "PREMISE projection answers before any rewrite" \
	"$(q "SELECT count(*) FROM pgcolumnar.read_projection('prem','pp');")" "$N"
check_text "PREMISE projection agrees with base before any rewrite" \
	"$(pgc_set_hash "SELECT pgcolumnar.read_projection('prem','pp')")" \
	"$(pgc_set_hash "SELECT a::text||'|'||b FROM prem")"
check "PREMISE two catalog rows under the current storage" \
	"$(q "SELECT count(*) FROM pgcolumnar.projection WHERE storage_id = pgcolumnar.get_storage_id('prem');")" "2"

# P2's oracle: a projection row whose storage id is not the CURRENT storage id of
# any live columnar relation. It must read 0 at every point in this suite.
#
# NOT "has no pgcolumnar.storage row", which is what this asserted first and is
# wrong in a way that made it pass for the wrong reason. A storage row is written
# on the first WRITE, not when the storage is created, so straight after a
# TRUNCATE the table's own current storage has no row -- and a correctly
# re-recorded projection under it read as retired. The version below asks the
# question the property is about, and it is deliberately GLOBAL: a row orphaned
# under any table is a leak, whichever arm caused it.
#
# This arm is a guard, not the detector for #876. It reads 0 on unmodified main
# too, because pgcolumnar_delete_storage_tree deletes the rows rather than
# stranding them (#867). It is here to redden if a fix strands them instead --
# which re-recording in the wrong place does.
retired_rows() {
	# relkind IN ('r','m'): a MATERIALIZED VIEW can be columnar and carry a
	# projection, and 'r' alone counted a live matview's rows as retired. That
	# poisoned four later arms, because this helper is global rather than
	# per-relation: every P2 check after the matview arm read the same 2.
	q "SELECT count(*) FROM pgcolumnar.projection p
	    WHERE NOT EXISTS (
	        SELECT 1 FROM pg_class c JOIN pg_am am ON am.oid = c.relam
	         WHERE am.amname = 'pgcolumnar' AND c.relkind IN ('r','m')
	           AND pgcolumnar.get_storage_id(c.oid) = p.storage_id);"
}
check "PREMISE no retired projection rows to begin with" "$(retired_rows)" "0"

# ---------------------------------------------------------------------------
# One arm: build a fixture, run one operation, assert all three properties.
#
#   $1 table    $2 operation SQL    $3 what the operation must DO
#   $4 the base-table projection of the covered columns, AFTER the operation
# ---------------------------------------------------------------------------
# arm TAG OP -> runs OP on a fresh copy of the fixture, and sets ARM_DID to NOOP,
# REWROTE or "FAILED: <error>". The checks are written at each call site, by literal
# name (#1341): built here from $tag, no grader could read them, and the operation's
# own check was recorded under one name when it passed and another when it failed.
ARM_DID=""
arm() {
	local tag="$1" op="$2" sid0 err
	psql_run "CREATE TABLE $tag (id int, a int, b text) USING pgcolumnar;" >/dev/null
	psql_run "INSERT INTO $tag SELECT g, g%50, 'b'||g FROM generate_series(1,$N) g;" >/dev/null
	psql_run "SELECT pgcolumnar.add_projection('$tag','pp',ARRAY['a','b'],ARRAY['a']);" >/dev/null
	sid0="$(q "SELECT pgcolumnar.get_storage_id('$tag');")"
	if err="$(psql_run "$op" 2>&1)"; then
		[ "$sid0" = "$(q "SELECT pgcolumnar.get_storage_id('$tag');")" ] && ARM_DID=NOOP || ARM_DID=REWROTE
	else
		ARM_DID="FAILED: $(sed -nE 's/.*(ERROR:.*)$/\1/p' <<<"$err" | head -1)"
	fi
}
proj_hash() { pgc_set_hash "SELECT pgcolumnar.read_projection('$1','pp')"; }
decl_count() {
	q "SELECT count(*) FROM pgcolumnar.projection_declaration WHERE rel = '$1'::regclass AND name = 'pp';"
}

echo "-- the rewrites that lose the projection today (#876)"

# TRUNCATE then re-INSERT: the projection must answer with the NEW rows.
arm t_trunc_reinsert "TRUNCATE t_trunc_reinsert;
	 INSERT INTO t_trunc_reinsert SELECT g, g%7, 'z'||g FROM generate_series(1,100) g;"
check "t_trunc_reinsert: operation REWROTE" "$ARM_DID" "REWROTE"
if [ "$ARM_DID" = REWROTE ]; then
	check_text "t_trunc_reinsert P1 projection agrees with base" "$(proj_hash t_trunc_reinsert)" "$(pgc_set_hash "SELECT a::text||'|'||b FROM t_trunc_reinsert")"
	check "t_trunc_reinsert P2 no retired projection rows" "$(retired_rows)" "0"
	check "t_trunc_reinsert P3 declaration survives" "$(decl_count t_trunc_reinsert)" "1"
else
	check_unrunnable "t_trunc_reinsert P1 projection agrees with base" UNMET_PRECONDITION "operation did not REWROTE: $ARM_DID"
	check_unrunnable "t_trunc_reinsert P2 no retired projection rows" UNMET_PRECONDITION "operation did not REWROTE: $ARM_DID"
	check_unrunnable "t_trunc_reinsert P3 declaration survives" UNMET_PRECONDITION "operation did not REWROTE: $ARM_DID"
fi

# TRUNCATE alone. The correct end state is a projection that answers and is
# EMPTY, which is why the oracle is the base table rather than a row count: an
# arm demanding rows would be wrong here and right for every other arm.
arm t_trunc_empty "TRUNCATE t_trunc_empty;"
check "t_trunc_empty: operation REWROTE" "$ARM_DID" "REWROTE"
if [ "$ARM_DID" = REWROTE ]; then
	check_text "t_trunc_empty P1 projection agrees with base" "$(proj_hash t_trunc_empty)" "$(pgc_set_hash "SELECT a::text||'|'||b FROM t_trunc_empty")"
	check "t_trunc_empty P2 no retired projection rows" "$(retired_rows)" "0"
	check "t_trunc_empty P3 declaration survives" "$(decl_count t_trunc_empty)" "1"
else
	check_unrunnable "t_trunc_empty P1 projection agrees with base" UNMET_PRECONDITION "operation did not REWROTE: $ARM_DID"
	check_unrunnable "t_trunc_empty P2 no retired projection rows" UNMET_PRECONDITION "operation did not REWROTE: $ARM_DID"
	check_unrunnable "t_trunc_empty P3 declaration survives" UNMET_PRECONDITION "operation did not REWROTE: $ARM_DID"
fi

# A type change on a COVERED column. The stored value must come back under the
# new type, so the oracle is read after the ALTER, not before.
arm t_altertype "ALTER TABLE t_altertype ALTER COLUMN a TYPE bigint;"
check "t_altertype: operation REWROTE" "$ARM_DID" "REWROTE"
if [ "$ARM_DID" = REWROTE ]; then
	check_text "t_altertype P1 projection agrees with base" "$(proj_hash t_altertype)" "$(pgc_set_hash "SELECT a::text||'|'||b FROM t_altertype")"
	check "t_altertype P2 no retired projection rows" "$(retired_rows)" "0"
	check "t_altertype P3 declaration survives" "$(decl_count t_altertype)" "1"
else
	check_unrunnable "t_altertype P1 projection agrees with base" UNMET_PRECONDITION "operation did not REWROTE: $ARM_DID"
	check_unrunnable "t_altertype P2 no retired projection rows" UNMET_PRECONDITION "operation did not REWROTE: $ARM_DID"
	check_unrunnable "t_altertype P3 declaration survives" UNMET_PRECONDITION "operation did not REWROTE: $ARM_DID"
fi

# A type change on a column the projection does NOT cover still rewrites the
# whole table, so it loses the projection just the same.
arm t_altertype_unc "ALTER TABLE t_altertype_unc ALTER COLUMN id TYPE bigint;"
check "t_altertype_unc: operation REWROTE" "$ARM_DID" "REWROTE"
if [ "$ARM_DID" = REWROTE ]; then
	check_text "t_altertype_unc P1 projection agrees with base" "$(proj_hash t_altertype_unc)" "$(pgc_set_hash "SELECT a::text||'|'||b FROM t_altertype_unc")"
	check "t_altertype_unc P2 no retired projection rows" "$(retired_rows)" "0"
	check "t_altertype_unc P3 declaration survives" "$(decl_count t_altertype_unc)" "1"
else
	check_unrunnable "t_altertype_unc P1 projection agrees with base" UNMET_PRECONDITION "operation did not REWROTE: $ARM_DID"
	check_unrunnable "t_altertype_unc P2 no retired projection rows" UNMET_PRECONDITION "operation did not REWROTE: $ARM_DID"
	check_unrunnable "t_altertype_unc P3 declaration survives" UNMET_PRECONDITION "operation did not REWROTE: $ARM_DID"
fi

# ADD COLUMN with a volatile default rewrites AND changes the live column set.
# The base projection (projection_id 0) records all live columns, so a fix that
# copies the old row verbatim leaves it naming a stale column set -- which no
# arm above would catch, because pp does not cover the new column.
arm t_addcol_vol "ALTER TABLE t_addcol_vol ADD COLUMN zz double precision DEFAULT random();"
check "t_addcol_vol: operation REWROTE" "$ARM_DID" "REWROTE"
if [ "$ARM_DID" = REWROTE ]; then
	check_text "t_addcol_vol P1 projection agrees with base" "$(proj_hash t_addcol_vol)" "$(pgc_set_hash "SELECT a::text||'|'||b FROM t_addcol_vol")"
	check "t_addcol_vol P2 no retired projection rows" "$(retired_rows)" "0"
	check "t_addcol_vol P3 declaration survives" "$(decl_count t_addcol_vol)" "1"
else
	check_unrunnable "t_addcol_vol P1 projection agrees with base" UNMET_PRECONDITION "operation did not REWROTE: $ARM_DID"
	check_unrunnable "t_addcol_vol P2 no retired projection rows" UNMET_PRECONDITION "operation did not REWROTE: $ARM_DID"
	check_unrunnable "t_addcol_vol P3 declaration survives" UNMET_PRECONDITION "operation did not REWROTE: $ARM_DID"
fi

# TRUNCATE naming SEVERAL tables rewrites every one of them, so a fix that
# repairs only the first relation in the statement passes every arm above.
psql_run "CREATE TABLE t_trunc_two (id int, a int, b text) USING pgcolumnar;" >/dev/null
psql_run "INSERT INTO t_trunc_two SELECT g, g%50, 'b'||g FROM generate_series(1,$N) g;" >/dev/null
psql_run "SELECT pgcolumnar.add_projection('t_trunc_two','pp',ARRAY['a','b'],ARRAY['a']);" >/dev/null
arm t_trunc_multi "TRUNCATE t_trunc_multi, t_trunc_two;"
check "t_trunc_multi: operation REWROTE" "$ARM_DID" "REWROTE"
if [ "$ARM_DID" = REWROTE ]; then
	check_text "t_trunc_multi P1 projection agrees with base" "$(proj_hash t_trunc_multi)" "$(pgc_set_hash "SELECT a::text||'|'||b FROM t_trunc_multi")"
	check "t_trunc_multi P2 no retired projection rows" "$(retired_rows)" "0"
	check "t_trunc_multi P3 declaration survives" "$(decl_count t_trunc_multi)" "1"
else
	check_unrunnable "t_trunc_multi P1 projection agrees with base" UNMET_PRECONDITION "operation did not REWROTE: $ARM_DID"
	check_unrunnable "t_trunc_multi P2 no retired projection rows" UNMET_PRECONDITION "operation did not REWROTE: $ARM_DID"
	check_unrunnable "t_trunc_multi P3 declaration survives" UNMET_PRECONDITION "operation did not REWROTE: $ARM_DID"
fi
check_text "t_trunc_multi: the SECOND table in the statement also survives" \
	"$(pgc_set_hash "SELECT pgcolumnar.read_projection('t_trunc_two','pp')")" \
	"$(pgc_set_hash "SELECT a::text||'|'||b FROM t_trunc_two")"

# TRUNCATE ... CASCADE reaches a table through a FOREIGN KEY. That table is not
# named in the statement and is not an inheritance descendant of anything named, so
# a repair that walks the statement's relation list plus find_all_inheritors never
# visits it (@linuxhikerpm, #892 review). The repair therefore records what the
# table-AM callback actually rewrote instead of re-deriving the statement's reach.
#
# The parent here is a HEAP table with no projections of its own, so the only thing
# that can make this arm pass is the child being repaired.
echo "-- a columnar table truncated through a foreign-key CASCADE"
psql_run "CREATE TABLE cas_parent (id int PRIMARY KEY);"
psql_run "CREATE TABLE cas_child (id int REFERENCES cas_parent(id), v int) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.add_projection('cas_child','pv',ARRAY['id','v'],ARRAY['v']);"
psql_run "INSERT INTO cas_parent SELECT g FROM generate_series(1,$N) g;"
psql_run "INSERT INTO cas_child SELECT g, g%7 FROM generate_series(1,$N) g;"
check "PREMISE the cascade fixture reads before the truncate" \
	"$(q "SELECT count(*) FROM pgcolumnar.read_projection('cas_child','pv');")" "$N"
CAS_SID0="$(q "SELECT pgcolumnar.get_storage_id('cas_child');")"
psql_run "TRUNCATE cas_parent CASCADE;"
if [ "$CAS_SID0" = "$(q "SELECT pgcolumnar.get_storage_id('cas_child');")" ]; then
	# If the cascade did not rewrite the child there is nothing to repair, and the
	# arm below would pass for the wrong reason.
	check_unrunnable "cas_child P1 the cascaded child keeps its projection" \
		UNMET_PRECONDITION "the CASCADE did not rewrite the child"
else
	pgc_pass "cas_child: the CASCADE rewrote the child"
	check_text "cas_child P1 the cascaded child keeps its projection" \
		"$(pgc_set_hash "SELECT pgcolumnar.read_projection('cas_child','pv')")" \
		"$(pgc_set_hash "SELECT id::text||'|'||v::text FROM cas_child")"
	check "cas_child P2 no retired projection rows" "$(retired_rows)" "0"
	check "cas_child P3 declaration survives" \
		"$(q "SELECT count(*) FROM pgcolumnar.projection_declaration
		       WHERE rel = 'cas_child'::regclass AND name = 'pv';")" "1"
fi

# ProcessUtility is RE-ENTRANT, and the repair records what it must fix in a
# process-global list. An AFTER TRUNCATE trigger whose function runs any utility
# statement calls the hook again from inside the outer statement, after the callback
# has already recorded the truncated relation (@jdatcmd, #892 review).
#
# Measured with the callback logging its own order, before this was fixed:
#     entry: clearing, had 0        the outer TRUNCATE starts
#     record: relid=16573           the cascaded child is recorded
#     entry: clearing, had 1        the trigger's nested utility clears it
#     drain: recorded=0             the outer drain has nothing left
#
# A DIRECTLY NAMED table survives that, because the statement's own relation list is
# a second source, which is why this arm composes the nested utility with a CASCADE.
# For a cascaded table the recorded list is the only route, so the two together are
# what leave the projection absent. Either alone passes.
echo "-- a cascade whose trigger runs a nested utility statement"
psql_run "CREATE TABLE reent_parent (id int PRIMARY KEY);"
psql_run "CREATE TABLE reent_child (id int REFERENCES reent_parent(id), v int) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.add_projection('reent_child','pv',ARRAY['id','v'],ARRAY['v']);"
psql_run "INSERT INTO reent_parent SELECT g FROM generate_series(1,$N) g;"
psql_run "INSERT INTO reent_child SELECT g, g%7 FROM generate_series(1,$N) g;"
psql_run "CREATE FUNCTION reent_nested_utility() RETURNS trigger LANGUAGE plpgsql AS \$\$
BEGIN
    -- A utility statement, so it re-enters the hook. Permanent, so the premise
    -- below can see from another session that it really ran; a TEMP table would
    -- vanish with the trigger's session and the premise would silently read 0.
    CREATE TABLE IF NOT EXISTS reent_marker (x int);
    RETURN NULL;
END\$\$;"
psql_run "CREATE TRIGGER reent_child_trunc AFTER TRUNCATE ON reent_child
          FOR EACH STATEMENT EXECUTE FUNCTION reent_nested_utility();"
check "PREMISE the trigger is installed on the cascaded child" \
	"$(q "SELECT count(*) FROM pg_trigger WHERE tgrelid='reent_child'::regclass AND NOT tgisinternal;")" "1"
check "PREMISE the projection reads before the truncate" \
	"$(q "SELECT count(*) FROM pgcolumnar.read_projection('reent_child','pv');")" "$N"
REENT_SID0="$(q "SELECT pgcolumnar.get_storage_id('reent_child');")"
psql_run "TRUNCATE reent_parent CASCADE;"
check "PREMISE the nested utility statement really ran" \
	"$(q "SELECT count(*) FROM pg_class WHERE relname='reent_marker';")" "1"
if [ "$REENT_SID0" = "$(q "SELECT pgcolumnar.get_storage_id('reent_child');")" ]; then
	check_unrunnable "reent_child P1 the projection survives a nested utility" \
		UNMET_PRECONDITION "the CASCADE did not rewrite the child"
else
	pgc_pass "reent_child: the CASCADE rewrote the child"
	check_text "reent_child P1 the projection survives a nested utility" \
		"$(pgc_set_hash "SELECT pgcolumnar.read_projection('reent_child','pv')")" \
		"$(pgc_set_hash "SELECT id::text||'|'||v::text FROM reent_child")"
	check "reent_child P2 no retired projection rows" "$(retired_rows)" "0"
fi

# A partitioned CHILD, rewritten by a type change on the PARENT. The statement
# names pt, the rewrite lands on pt1, and pt is not itself a columnar relation --
# so a fix that looks only at the relation named in the statement never fires
# here. This is the same reason the #778 rename block walks find_all_inheritors.
echo "-- a partitioned child rewritten through its parent"
psql_run "CREATE TABLE ptr (id int, a int, b text) PARTITION BY RANGE (id);"
psql_run "CREATE TABLE ptr1 PARTITION OF ptr FOR VALUES FROM (1) TO (100000) USING pgcolumnar;"
psql_run "INSERT INTO ptr SELECT g, g%50, 'b'||g FROM generate_series(1,$N) g;"
psql_run "SELECT pgcolumnar.add_projection('ptr1','pp',ARRAY['a','b'],ARRAY['a']);"
PTR_SID0="$(q "SELECT pgcolumnar.get_storage_id('ptr1');")"
psql_run "ALTER TABLE ptr ALTER COLUMN a TYPE bigint;"
if [ "$PTR_SID0" = "$(q "SELECT pgcolumnar.get_storage_id('ptr1');")" ]; then
	check_unrunnable "ptr1 P1 child projection agrees with base" \
		UNMET_PRECONDITION "the parent-level ALTER did not rewrite the child"
else
	pgc_pass "ptr1: parent-level ALTER rewrote the child"
	check_text "ptr1 P1 child projection agrees with base" \
		"$(pgc_set_hash "SELECT pgcolumnar.read_projection('ptr1','pp')")" \
		"$(pgc_set_hash "SELECT a::text||'|'||b FROM ptr1")"
fi

echo "-- the repair must be a no-op when the projection is already present"
# The already-present guard had no arm (@jdatcmd, #892 review), and it runs far more
# often than the repair does: EVERY AlterTableStmt on a relation with a declared
# projection reaches the repair, including the ones that rewrite nothing. That is
# also why the decision is now made under AccessShareLock -- these three statements
# take ShareUpdateExclusiveLock, and the repair must not escalate past them.
psql_run "CREATE TABLE noop (id int, a int, b text) USING pgcolumnar;"
psql_run "INSERT INTO noop SELECT g, g%50, 'b'||g FROM generate_series(1,$N) g;"
psql_run "SELECT pgcolumnar.add_projection('noop','pp',ARRAY['a','b'],ARRAY['a']);"
NOOP_SID="$(q "SELECT pgcolumnar.get_storage_id('noop');")"
check "PREMISE the projection is present before the metadata-only statements" \
	"$(q "SELECT count(*) FROM pgcolumnar.projection
	       WHERE storage_id = pgcolumnar.get_storage_id('noop');")" "2"
psql_run "ALTER TABLE noop ALTER COLUMN a SET STATISTICS 50;"
psql_run "ALTER TABLE noop SET (autovacuum_enabled = false);"
check "noop: the metadata-only statements rewrote nothing" \
	"$(q "SELECT pgcolumnar.get_storage_id('noop');")" "$NOOP_SID"
check "noop: and the repair added no projection row" \
	"$(q "SELECT count(*) FROM pgcolumnar.projection
	       WHERE storage_id = pgcolumnar.get_storage_id('noop');")" "2"
check_text "noop: and the projection still agrees with the base table" \
	"$(pgc_set_hash "SELECT pgcolumnar.read_projection('noop','pp')")" \
	"$(pgc_set_hash "SELECT a::text||'|'||b FROM noop")"

echo "-- REFRESH MATERIALIZED VIEW: a rewrite that is neither ALTER nor TRUNCATE"
# The third rewriting utility shape, and the one a gate naming only AlterTableStmt
# and TruncateStmt lets through (@jdatcmd, #892 review). Proved load-bearing by
# removal: with RefreshMatViewStmt taken out of the gate, this arm reports
# ERROR: projection "pp" does not exist on "mv1" -- and the new HINT then tells the
# reader that re-recording is automatic, which is the opposite of what happened.
#
# REFRESH ... CONCURRENTLY has no arm because it is not a rewrite: measured, the
# storage id is unchanged across it, so there is nothing to repair and an arm would
# be permanently unrunnable rather than merely green.
psql_run "CREATE TABLE mv_base (id int, a int, b text);"
psql_run "INSERT INTO mv_base SELECT g, g%50, 'b'||g FROM generate_series(1,$N) g;"
psql_run "CREATE MATERIALIZED VIEW mv1 USING pgcolumnar AS SELECT id, a, b FROM mv_base;"
psql_run "SELECT pgcolumnar.add_projection('mv1','pp',ARRAY['a','b'],ARRAY['a']);"
check "PREMISE the matview projection reads before the refresh" \
	"$(q "SELECT count(*) FROM pgcolumnar.read_projection('mv1','pp');")" "$N"
MV_SID0="$(q "SELECT pgcolumnar.get_storage_id('mv1');")"
psql_run "UPDATE mv_base SET b = 'z'||id WHERE id <= 10;"
psql_run "REFRESH MATERIALIZED VIEW mv1;"
if [ "$MV_SID0" = "$(q "SELECT pgcolumnar.get_storage_id('mv1');")" ]; then
	check_unrunnable "mv1 P1 the refreshed matview keeps its projection" \
		UNMET_PRECONDITION "REFRESH did not rewrite the matview"
else
	pgc_pass "mv1: REFRESH rewrote the matview"
	check_text "mv1 P1 the refreshed matview keeps its projection" \
		"$(pgc_set_hash "SELECT pgcolumnar.read_projection('mv1','pp')")" \
		"$(pgc_set_hash "SELECT a::text||'|'||b FROM mv1")"
fi
check "mv1 P2 no projection row names a storage the matview no longer has" \
	"$(retired_rows)" "0"
check "mv1 P3 the declaration survives the refresh" \
	"$(q "SELECT count(*) FROM pgcolumnar.projection_declaration
	       WHERE rel = 'mv1'::regclass AND name = 'pp';")" "1"

echo "-- and the base projection must still name every live column"
check "t_addcol_vol base projection covers the added column" \
	"$(q "SELECT columns FROM pgcolumnar.projection
	       WHERE storage_id = pgcolumnar.get_storage_id('t_addcol_vol')
	         AND projection_id = 0;")" \
	"{1,2,3,4}"

echo "-- the rewrites that already handle projections: regression arms"
# These are GREEN on main. pgcolumnar_compact_relation and its siblings re-record
# after RelationSetNewRelfilenumber, which dispatches through the same table-AM
# callback the arms above go through. So a fix placed IN that callback double-
# records for these three and violates projection_pkey (storage_id,
# projection_id). They are here to redden if that happens.
arm r_vacuum "SELECT pgcolumnar.vacuum('r_vacuum');"
check "r_vacuum: operation REWROTE" "$ARM_DID" "REWROTE"
if [ "$ARM_DID" = REWROTE ]; then
	check_text "r_vacuum P1 projection agrees with base" "$(proj_hash r_vacuum)" "$(pgc_set_hash "SELECT a::text||'|'||b FROM r_vacuum")"
	check "r_vacuum P2 no retired projection rows" "$(retired_rows)" "0"
	check "r_vacuum P3 declaration survives" "$(decl_count r_vacuum)" "1"
else
	check_unrunnable "r_vacuum P1 projection agrees with base" UNMET_PRECONDITION "operation did not REWROTE: $ARM_DID"
	check_unrunnable "r_vacuum P2 no retired projection rows" UNMET_PRECONDITION "operation did not REWROTE: $ARM_DID"
	check_unrunnable "r_vacuum P3 declaration survives" UNMET_PRECONDITION "operation did not REWROTE: $ARM_DID"
fi
arm r_vacsorted "SELECT pgcolumnar.vacuum_sorted('r_vacsorted','a');"
check "r_vacsorted: operation REWROTE" "$ARM_DID" "REWROTE"
if [ "$ARM_DID" = REWROTE ]; then
	check_text "r_vacsorted P1 projection agrees with base" "$(proj_hash r_vacsorted)" "$(pgc_set_hash "SELECT a::text||'|'||b FROM r_vacsorted")"
	check "r_vacsorted P2 no retired projection rows" "$(retired_rows)" "0"
	check "r_vacsorted P3 declaration survives" "$(decl_count r_vacsorted)" "1"
else
	check_unrunnable "r_vacsorted P1 projection agrees with base" UNMET_PRECONDITION "operation did not REWROTE: $ARM_DID"
	check_unrunnable "r_vacsorted P2 no retired projection rows" UNMET_PRECONDITION "operation did not REWROTE: $ARM_DID"
	check_unrunnable "r_vacsorted P3 declaration survives" UNMET_PRECONDITION "operation did not REWROTE: $ARM_DID"
fi
arm r_cluster "SELECT pgcolumnar.cluster('r_cluster','a');"
check "r_cluster: operation REWROTE" "$ARM_DID" "REWROTE"
if [ "$ARM_DID" = REWROTE ]; then
	check_text "r_cluster P1 projection agrees with base" "$(proj_hash r_cluster)" "$(pgc_set_hash "SELECT a::text||'|'||b FROM r_cluster")"
	check "r_cluster P2 no retired projection rows" "$(retired_rows)" "0"
	check "r_cluster P3 declaration survives" "$(decl_count r_cluster)" "1"
else
	check_unrunnable "r_cluster P1 projection agrees with base" UNMET_PRECONDITION "operation did not REWROTE: $ARM_DID"
	check_unrunnable "r_cluster P2 no retired projection rows" UNMET_PRECONDITION "operation did not REWROTE: $ARM_DID"
	check_unrunnable "r_cluster P3 declaration survives" UNMET_PRECONDITION "operation did not REWROTE: $ARM_DID"
fi

# ---------------------------------------------------------------------------
# The re-record must never abort the statement that triggered it.
#
# A declaration can name a column the relation no longer has. Measured before this
# arm existed: the re-record raised `column "a" does not exist` inside an unrelated
# ALTER TABLE ... ALTER COLUMN id TYPE bigint, exit 1, and the type change was
# rolled back -- turning a silently lost projection into a blocked schema change. A
# repair that cannot run must degrade to a WARNING and leave the projection for
# rebuild_projections, which is the documented recovery.
#
# HOW THE STALE DECLARATION IS PRODUCED, and why it is not a rename.
# ALTER TABLE ... RENAME COLUMN used to leave the declaration behind, and #888 fixed
# that. So a rename can no longer produce this state, and an earlier version of this
# arm correctly reported UNMET_PRECONDITION once #888 landed rather than passing
# vacuously. The state is still reachable: any database created before #888 carries
# it, and nothing guarantees some future path cannot reintroduce it. So the arm
# writes the stale name directly into pgcolumnar.projection_declaration, which is
# exactly what such a database looks like, and keeps testing the property that
# matters -- that the repair cannot take a user's statement down with it.
echo "-- a stale declaration must not abort the statement"
psql_run "CREATE TABLE stale (id int, a int, b text) USING pgcolumnar;"
psql_run "INSERT INTO stale SELECT g, g%50, 'b'||g FROM generate_series(1,$N) g;"
psql_run "SELECT pgcolumnar.add_projection('stale','pp',ARRAY['a','b'],ARRAY['a']);"
# Name a column the table does not have, as a pre-#888 database would after a rename.
psql_run "UPDATE pgcolumnar.projection_declaration
             SET columns = ARRAY['gone','b'], sort_key = ARRAY['gone']
           WHERE rel = 'stale'::regclass AND name = 'pp';"
STALE_DECL="$(q "SELECT columns::text FROM pgcolumnar.projection_declaration WHERE rel='stale'::regclass;")"
if [ "$STALE_DECL" = "{gone,b}" ]; then
	pgc_pass "PREMISE the declaration names a column the table lacks"
else
	check_unrunnable "stale: the statement survives a stale declaration" \
		UNMET_PRECONDITION "declaration is $STALE_DECL, so it is not stale"
	check_unrunnable "stale: the base table keeps its rows" \
		UNMET_PRECONDITION "declaration is $STALE_DECL, so it is not stale"
	check_unrunnable "stale: the unrelated type change took effect" \
		UNMET_PRECONDITION "declaration is $STALE_DECL, so it is not stale"
fi
if [ "$STALE_DECL" = "{gone,b}" ]; then
	if psql_run "ALTER TABLE stale ALTER COLUMN id TYPE bigint;" >/dev/null 2>&1; then
		pgc_pass "stale: the statement survives a stale declaration"
	else
		pgc_fail "stale: the statement survives a stale declaration" \
			"the re-record aborted an unrelated ALTER TABLE"
	fi
	check "stale: the base table keeps its rows" "$(q 'SELECT count(*) FROM stale;')" "$N"
	check "stale: the unrelated type change took effect" \
		"$(q "SELECT format_type(atttypid,atttypmod) FROM pg_attribute
		       WHERE attrelid='stale'::regclass AND attname='id';")" "bigint"

	# A skip the user is never told about is a silent projection loss, which is the
	# whole complaint in #876. Assert the WARNING from its own output, and assert the
	# negative control in the same breath: a table whose declaration is intact must
	# not produce one, or the arm passes on a warning that fires unconditionally.
	psql_run "CREATE TABLE stale2 (id int, a int, b text) USING pgcolumnar;" >/dev/null
	psql_run "INSERT INTO stale2 SELECT g, g%50, 'b'||g FROM generate_series(1,$N) g;" >/dev/null
	psql_run "SELECT pgcolumnar.add_projection('stale2','pp',ARRAY['a','b'],ARRAY['a']);" >/dev/null
	psql_run "UPDATE pgcolumnar.projection_declaration SET columns = ARRAY['gone','b'],
	             sort_key = ARRAY['gone'] WHERE rel = 'stale2'::regclass;" >/dev/null
	# Assert the SQLSTATE, not the prose. The message text is prose and will be
	# reworded -- it already was, in this very PR -- while 42703 is the contract.
	# The earlier version counted lines matching the text OR the string
	# "rebuild_projections", and rewording the HINT to name the recovery that
	# actually works would have silently halved that count (@jdatcmd, #892 review).
	STALE_OUT="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" \
		-U postgres -d "$PGC_DB" -v VERBOSITY=verbose \
		-c "ALTER TABLE stale2 ALTER COLUMN id TYPE bigint;" 2>&1)"
	check "stale: the skip warns with SQLSTATE 42703" \
		"$(printf '%s\n' "$STALE_OUT" | grep -c '^WARNING:  42703: ')" "1"
	check "stale: and the warning names the projection it skipped" \
		"$(printf '%s\n' "$STALE_OUT" | grep -c 'could not restore projection "pp"')" "1"
	check "stale: and it names a recovery the extension actually offers" \
		"$(printf '%s\n' "$STALE_OUT" | grep -c 'add_projection')" "1"

	psql_run "CREATE TABLE fresh2 (id int, a int, b text) USING pgcolumnar;" >/dev/null
	psql_run "INSERT INTO fresh2 SELECT g, g%50, 'b'||g FROM generate_series(1,$N) g;" >/dev/null
	psql_run "SELECT pgcolumnar.add_projection('fresh2','pp',ARRAY['a','b'],ARRAY['a']);" >/dev/null
	# The negative control needs its own premise. "no line matched WARNING" is
	# satisfied by NO OUTPUT AT ALL, so a statement that never ran would pass it
	# (@jdatcmd, #892 review). Assert that the statement ran and rewrote first.
	FRESH_SID0="$(q "SELECT pgcolumnar.get_storage_id('fresh2');")"
	FRESH_OUT="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" \
		-U postgres -d "$PGC_DB" \
		-c "ALTER TABLE fresh2 ALTER COLUMN id TYPE bigint;" 2>&1)"
	check "PREMISE the control statement completed, so its output is not empty" \
		"$(printf '%s\n' "$FRESH_OUT" | grep -c '^ALTER TABLE$')" "1"
	if [ "$FRESH_SID0" = "$(q "SELECT pgcolumnar.get_storage_id('fresh2');")" ]; then
		check_unrunnable "stale: an intact declaration produces NO warning" \
			UNMET_PRECONDITION "the control ALTER did not rewrite fresh2"
	else
		pgc_pass "PREMISE the control statement rewrote fresh2, so a warning was possible"
		check "stale: an intact declaration produces NO warning" \
			"$(printf '%s\n' "$FRESH_OUT" | grep -ci warning)" "0"
	fi
fi

# And the property #888 now guarantees, asserted here too because this suite is the
# one that breaks if it regresses: a rename carries into the declaration, so the
# repair after a later rewrite still resolves.
echo "-- a renamed column no longer strands the declaration (#888)"
psql_run "CREATE TABLE renamed (id int, a int, b text) USING pgcolumnar;"
psql_run "INSERT INTO renamed SELECT g, g%50, 'b'||g FROM generate_series(1,$N) g;"
psql_run "SELECT pgcolumnar.add_projection('renamed','pp',ARRAY['a','b'],ARRAY['a']);"
psql_run "ALTER TABLE renamed RENAME COLUMN a TO a2;"
check "renamed: the declaration followed the rename" \
	"$(q "SELECT columns::text FROM pgcolumnar.projection_declaration WHERE rel='renamed'::regclass;")" \
	"{a2,b}"
psql_run "ALTER TABLE renamed ALTER COLUMN id TYPE bigint;"
check_text "renamed: and the projection survives a later rewrite" \
	"$(pgc_set_hash "SELECT pgcolumnar.read_projection('renamed','pp')")" \
	"$(pgc_set_hash "SELECT a2::text||'|'||b FROM renamed")"

# rebuild_projections stays the documented manual recovery (#876), and this arm has
# to make the projection GENUINELY ABSENT to test it.
#
# The earlier version truncated and then called rebuild_projections. Once the repair
# in this PR landed, the TRUNCATE was already repaired automatically, so the
# comparison passed whether or not rebuild_projections did anything: it passed with
# the function gutted (@jdatcmd, #892 review). A test named after a function it does
# not exercise is worse than no test, because the name is what a reader trusts.
#
# So the projection rows are deleted directly, which is what a logical restore
# leaves: pg_dump carries pgcolumnar.projection_declaration and cannot carry the
# storage, and that is the case the function was written for. Its RETURN VALUE is
# asserted, not merely the end state, because the count is the direct evidence it
# did the work.
echo "-- rebuild_projections rebuilds a projection whose storage is absent (#876)"
psql_run "CREATE TABLE rec (id int, a int, b text) USING pgcolumnar;"
psql_run "INSERT INTO rec SELECT g, g%50, 'b'||g FROM generate_series(1,$N) g;"
psql_run "SELECT pgcolumnar.add_projection('rec','pp',ARRAY['a','b'],ARRAY['a']);"
check "PREMISE the projection reads before it is removed" \
	"$(q "SELECT count(*) FROM pgcolumnar.read_projection('rec','pp');")" "$N"
# Simulate the restored state: the declaration survives, the storage does not.
psql_run "DELETE FROM pgcolumnar.projection
           WHERE storage_id = pgcolumnar.get_storage_id('rec') AND projection_id > 0;"
check "PREMISE the projection is now genuinely absent" \
	"$(q "SELECT count(*) FROM pgcolumnar.projection
	       WHERE storage_id = pgcolumnar.get_storage_id('rec') AND projection_id > 0;")" "0"
check "PREMISE and the declaration survived, so a rebuild is possible" \
	"$(q "SELECT count(*) FROM pgcolumnar.projection_declaration
	       WHERE rel = 'rec'::regclass AND name = 'pp';")" "1"
check "rebuild_projections reports rebuilding exactly one projection" \
	"$(q "SELECT pgcolumnar.rebuild_projections('rec');")" "1"
check_text "and the rebuilt projection matches the base table" \
	"$(pgc_set_hash "SELECT pgcolumnar.read_projection('rec','pp')")" \
	"$(pgc_set_hash "SELECT a::text||'|'||b FROM rec")"
check "and it leaves no retired projection rows" "$(retired_rows)" "0"
check "a second call rebuilds nothing, so it is safe to run at any time" \
	"$(q "SELECT pgcolumnar.rebuild_projections('rec');")" "0"

pgc_summary
