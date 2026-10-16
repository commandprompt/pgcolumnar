#!/usr/bin/env bash
#
# ALTER TABLE ... ALTER COLUMN TYPE must leave pgcolumnar.storage.relation_oid
# pointing at the table that survived the rewrite.
#
# A type change rewrites through a transient relation. The storage row is
# written with that transient's OID. The swap keeps the user's OID and drops
# the transient, so a lookup by the live table finds nothing. The rows are
# still readable. The written-geometry lookup is the one reader of this
# column, and it fails closed by returning the session default.
#
# Usage:  test/rewrite_storage_oid.sh [PG_CONFIG]

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/lib/postgresql/18/bin/pg_config}"

q "CREATE EXTENSION IF NOT EXISTS pgcolumnar;" >/dev/null

# at_least GOT FLOOR -- GOT when it falls short of FLOOR, else FLOOR, so a failing
# arm reports the number it measured rather than a bare 0 (selftest part 540).
at_least() { [ "$1" -lt "$2" ] && echo "$1" || echo "$2"; }


q "CREATE TABLE rew_keep (id int) USING pgcolumnar;
   INSERT INTO rew_keep SELECT g FROM generate_series(1,40) g;
   CREATE TABLE rew_oid (id int, note text) USING pgcolumnar;
   INSERT INTO rew_oid SELECT g, 'n' || g FROM generate_series(1,900) g;" >/dev/null

live_rows() {
	q "SELECT (relation_oid = 'rew_oid'::regclass)::int
	   FROM pgcolumnar.storage
	   WHERE storage_id = pgcolumnar.get_storage_id('rew_oid');"
}

check_num "premise: the storage row points at the live table before the rewrite" \
	"$(live_rows)" "1"

check_num "premise: the table holds every inserted row" \
	"$(q "SELECT count(*) FROM rew_oid;")" "900"

q "ALTER TABLE rew_oid ALTER COLUMN id TYPE bigint;" >/dev/null

# Five checks here read product state and are arms, not premises (#1339,
# #1236): the row counts after a rewrite, a matview, CREATE TABLE AS and REFRESH,
# and the untouched table's storage row. A bulk load that drops its rows, or
# a repoint of the wrong storage row, reddens them.
check_num "the rewritten table still holds every row" \
	"$(q "SELECT count(*) FROM rew_oid;")" "900"

check_num "a table that was not rewritten still points at itself" \
	"$(q "SELECT (relation_oid = 'rew_keep'::regclass)::int
	      FROM pgcolumnar.storage
	      WHERE storage_id = pgcolumnar.get_storage_id('rew_keep');")" "1"

echo "-- live=$(live_rows)"
check_num "a type rewrite leaves the storage row pointing at the live table" \
	"$(live_rows)" "1"

# --- and CREATE MATERIALIZED VIEW ... WITH DATA (#1275) ----------------------
#
# The same defect on a different statement. `CREATE MATERIALIZED VIEW ... AS`
# with data builds a transient, fills it, and swaps -- exactly like the type
# change above -- so the storage row is written with the transient's OID and the
# swap leaves it naming a relation that no longer exists.
#
# `REFRESH MATERIALIZED VIEW` already repairs it, because RefreshMatViewStmt is
# one of the node types the repair runs for. A CreateTableAsStmt was not.
#
# THE `CREATE TABLE ... AS` ARM IS THE CONTROL THAT NARROWS THE CLAIM. It is the
# same parse node and it does NOT have the defect -- measured before the fix,
# rows_by_relation_oid = 1 -- because it fills the relation it created rather
# than swapping a transient in. Without it the fix would reasonably have been
# written for CreateTableAsStmt as a whole, which is broader than anything
# measured asked for.
q "CREATE MATERIALIZED VIEW rew_mv USING pgcolumnar AS
     SELECT id, note FROM rew_oid;" >/dev/null
q "CREATE TABLE rew_cta USING pgcolumnar AS
     SELECT id, note FROM rew_oid;" >/dev/null

points_at() {	# points_at RELNAME -> 1 when its storage row names it
	q "SELECT (relation_oid = '$1'::regclass)::int
	   FROM pgcolumnar.storage
	   WHERE storage_id = pgcolumnar.get_storage_id('$1');"
}

# A ROW COUNT FIRST, so the arms below are about a relation that was actually
# populated. A matview created WITH NO DATA writes no storage row at all, and
# every arm here would then be asking about nothing.
check_num "the matview holds the rows it was created with" \
	"$(q "SELECT count(*) FROM rew_mv;")" "900"
check_num "the CREATE TABLE AS table holds them too" \
	"$(q "SELECT count(*) FROM rew_cta;")" "900"

echo "-- mv=$(points_at rew_mv) cta=$(points_at rew_cta)"

# THE CONTROL, AND IT MUST PASS BEFORE THE FIX AS WELL AS AFTER. If this ever
# reads 0 the claim below is no longer about matviews specifically, and the
# remedy is a different one.
check_num "premise: CREATE TABLE ... AS leaves its storage row pointing at itself" \
	"$(points_at rew_cta)" "1"

check_num "a matview created WITH DATA leaves its storage row pointing at itself" \
	"$(points_at rew_mv)" "1"

# AND REFRESH MUST STILL WORK. It repaired this before the fix, through a
# different node type, so an arm here is what says the fix did not displace the
# path that already worked.
q "REFRESH MATERIALIZED VIEW rew_mv;" >/dev/null
check_num "the refreshed matview still holds every row" \
	"$(q "SELECT count(*) FROM rew_mv;")" "900"
check_num "and REFRESH still leaves it pointing at itself" \
	"$(points_at rew_mv)" "1"

# ---- the repair gate must leave alone the partitions an ALTER did not touch -
#
# The gate above runs for every inheritor of an ALTER's target. An ALTER that
# does not recurse -- DETACH, DETACH CONCURRENTLY, ATTACH, ALTER TABLE ONLY --
# locks only the parent and the partition it names, and the gate opened every
# OTHER partition with NoLock to read its storage id. On an assert-enabled build
# relation_open aborted the backend:
#
#     TRAP: failed Assert("lockmode != NoLock || ... CheckRelationLockedByMe ...")
#     <- PgColumnarRetargetStorageRelation <- pgcolumnar_process_utility
#
# measured for all four forms on a parent with two columnar partitions, PG 15
# and 17. One partition hides it: no other partition is left to open.
#
# THE FIX SKIPS A RELATION THE STATEMENT HOLDS NO LOCK ON, rather than locking it,
# since a relation the statement rewrote is locked by it until commit. So the
# arms read WHAT THE GATE READ: blocks of the untouched partition, from
# pg_statio, flushed by the ALTER's own session. The unfixed gate reads that
# partition's metapage; the fixed one reads nothing of it. That holds on every
# build, release builds included, where the Assert does not exist and an "it
# did not crash" arm would be green on the unfixed code.
#
# The partitions are columnar explicitly, so the fixture is the same on every
# major (a parent cannot carry USING before PostgreSQL 17).
setup_parts() {
	q "DROP TABLE IF EXISTS lp, lp_1, lp_2, lp_3 CASCADE;
	   CREATE TABLE lp (ts timestamptz, v int) PARTITION BY RANGE (ts);
	   CREATE TABLE lp_1 PARTITION OF lp FOR VALUES FROM ('2026-01-01') TO ('2026-02-01') USING pgcolumnar;
	   CREATE TABLE lp_2 PARTITION OF lp FOR VALUES FROM ('2026-02-01') TO ('2026-03-01') USING pgcolumnar;
	   CREATE TABLE lp_3 (ts timestamptz, v int) USING pgcolumnar;
	   INSERT INTO lp SELECT '2026-01-01'::timestamptz + g * interval '1 h', g
	     FROM generate_series(1, 1000) g;" >/dev/null
}
# up -- wait for the server to accept connections again. On the unfixed code on
# an assert build each ALTER below aborts a backend and the postmaster restarts
# every session; the next arm must measure the code, not the restart.
up() { local i; for i in $(seq 1 60); do [ "$(q "SELECT 1;")" = 1 ] && return 0; sleep 1; done; return 1; }
# lp1_blocks_during STATEMENT -> lp_1 blocks read by STATEMENT, or nothing when
# the session did not survive to flush. Its own session, one transaction per
# -c, so DETACH CONCURRENTLY runs outside a transaction block as it must.
lp1_blocks_during() {
	q "SELECT pg_stat_reset();" >/dev/null
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "$1" -c "SELECT pg_stat_force_next_flush()" >/dev/null 2>&1 ||
		{ up; return; }
	q "SELECT coalesce(heap_blks_read, 0) + coalesce(heap_blks_hit, 0)
	   FROM pg_statio_user_tables WHERE relname = 'lp_1';"
}

setup_parts
check "premise: both columnar partitions hold their rows" \
	"$(q "SELECT count(*) FROM lp_1;")|$(q "SELECT count(*) FROM lp_2;")" "743|257"

# THE CONTROL: the instrument sees a read of lp_1 when there is one. Without it
# every zero below could be an instrument that cannot see.
ctl="$(lp1_blocks_during "SELECT count(*) FROM lp_1")"
check_num "control: reading lp_1 shows up as blocks read from it" \
	"$( [ -n "$ctl" ] && at_least "$ctl" 1 )" "1"

for alter in \
	"ALTER TABLE ONLY lp ALTER COLUMN v SET DEFAULT 0" \
	"ALTER TABLE lp ATTACH PARTITION lp_3 FOR VALUES FROM ('2026-03-01') TO ('2026-04-01')" \
	"ALTER TABLE lp DETACH PARTITION lp_2" \
	"ALTER TABLE lp DETACH PARTITION lp_2 CONCURRENTLY"; do
	setup_parts
	n="$(lp1_blocks_during "$alter")"
	echo "-- lp_1 blocks read during: $alter -> ${n:-session aborted}"
	check_num "the repair gate does not read a partition the ALTER did not touch: ${alter#ALTER TABLE }" \
		"${n:-aborted}" "0"
done

# The rewrite repair still reaches a partition the statement DID rewrite: a type
# change on the parent recurses, locks every partition, and rewrites both.
setup_parts
q "ALTER TABLE lp ALTER COLUMN v TYPE bigint;" >/dev/null
check "and a rewrite through the parent still repoints every partition's storage row" \
	"$(q "SELECT string_agg((s.relation_oid = c.oid)::int::text, '' ORDER BY c.relname)
	      FROM pg_class c JOIN pgcolumnar.storage s
	        ON s.storage_id = pgcolumnar.get_storage_id(c.oid::regclass)
	      WHERE c.relname IN ('lp_1', 'lp_2');")" "11"

pgc_summary
