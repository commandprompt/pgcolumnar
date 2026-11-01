#!/usr/bin/env bash
#
# pgcolumnar.tier_partition moves a cold columnar partition to Parquet, in one
# transaction, and a failure at any step leaves the rows exactly once (#1295).
#
# WRITTEN BEFORE THE CODE, against the API proposed on #1295:
#
#     pgcolumnar.tier_partition(partition regclass, server name, path text) -> rows
#
# The owner's decision (#1295, 2026-10-12): atomic, with a brief ACCESS EXCLUSIVE
# on the parent. Export under the partition's SHARE lock, then detach, drop,
# create the foreign table under the partition's own name, and attach it with the
# partition's own bounds.
#
# "EXACTLY ONCE" IS TESTED, NOT ARGUED. pgcolumnar.tier_fault_after raises an
# ERROR right after the step it names. Each fault arm first requires that the
# error raised IS the injected one: a missing function also fails, and leaves
# the rows where they were, so "the rows are still local" alone proves nothing.
#
# A local path stands in for object storage; `s3://` is the same writer with
# another path and is not exercised here.
#
# Usage:  test/tier_partition.sh [PG_CONFIG]

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# The endpoint allow-list (#393) defaults to deny-all. The object-store section
# at the end (#1795) writes to two local SigV4 fixture servers.
export PGC_EXTRA_CONF="pgcolumnar.objstore_allowed_endpoints='127.0.0.1'"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

W="$PGC_WORKDIR/tier"
mkdir -p "$W" && chmod 777 "$W"

# sqlstate SQL... -> the SQLSTATE the last statement fails with, or ok
sqlstate() {
	local out
	out=$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-X -At -v ON_ERROR_STOP=1 -v VERBOSITY=sqlstate "${@/#/-c}" 2>&1 >/dev/null)
	[ -z "$out" ] && { echo ok; return; }
	echo "$out" | sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\).*/\1/p' | head -1
}
# errtext SQL... -> the first ERROR line's message, or ok
errtext() {
	local out
	out=$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-X -At -v ON_ERROR_STOP=1 "${@/#/-c}" 2>&1 >/dev/null)
	[ -z "$out" ] && { echo ok; return; }
	echo "$out" | sed -n 's/^ERROR: *//p' | head -1
}

q "CREATE EXTENSION IF NOT EXISTS pgcolumnar;" >/dev/null
psql_run "CREATE SERVER tier_pq FOREIGN DATA WRAPPER pgcolumnar_parquet;"

# build NAME: a parent with a January and a February columnar partition
build() {
	psql_run "DROP TABLE IF EXISTS $1 CASCADE;"
	psql_run "CREATE TABLE $1 (ts timestamptz NOT NULL, dev int, v bigint, note text)
	          PARTITION BY RANGE (ts);"
	psql_run "CREATE TABLE ${1}_jan PARTITION OF $1
	          FOR VALUES FROM ('2026-01-01') TO ('2026-02-01') USING pgcolumnar;"
	psql_run "CREATE TABLE ${1}_feb PARTITION OF $1
	          FOR VALUES FROM ('2026-02-01') TO ('2026-03-01') USING pgcolumnar;"
	psql_run "INSERT INTO $1 SELECT '2026-01-01'::timestamptz + g * interval '5 min', g % 13,
	          (hashint4(g)::bigint * 7) % 100003, md5(g::text)
	          FROM generate_series(0, 16991) g;"
}
# every row, decoded: an unfiltered count/sum is answered from zone maps
digest() { q "SELECT count(*) || '|' || md5(string_agg(ts::text || dev || v || note, ',' ORDER BY ts)) FROM $1;"; }
# where the January range lives: relkind, access method, and its bound
jan_shape() {
	q "SELECT c.relkind::text || ':' || coalesce(a.amname, '-') || ':' || pg_get_expr(c.relpartbound, c.oid)
	   FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid LEFT JOIN pg_am a ON a.oid = c.relam
	   WHERE i.inhparent = '$1'::regclass AND c.relname = '${1}_jan';"
}
JAN_BOUND="FOR VALUES FROM ('2026-01-01 00:00:00+00') TO ('2026-02-01 00:00:00+00')"

psql_run "SET timezone = 'UTC'; ALTER DATABASE \"$PGC_DB\" SET timezone = 'UTC';"
build m
check_num "premise: the parent holds every row" "$(q "SELECT count(*) FROM m;")" "16992"
check_num "premise: the cold partition holds its share" "$(q "SELECT count(*) FROM m_jan;")" "8928"
before="$(digest m)"
check "premise: the cold partition is local and columnar, with its bound" "$(jan_shape m)" "r:pgcolumnar:$JAN_BOUND"
jan_storage="$(q "SELECT pgcolumnar.get_storage_id('m_jan');")"

# ---- the move ---------------------------------------------------------------
check "tier_partition moves the cold partition and returns the rows it moved" \
	"$(q "SELECT pgcolumnar.tier_partition('m_jan', 'tier_pq', '$W/m_jan.parquet');")" "8928"
# The relkind rides along: with nothing moved the digest is trivially unchanged.
check "the answers over the parent are the same after the move" \
	"$(digest m)|$(q "SELECT relkind FROM pg_class WHERE oid = 'm_jan'::regclass;")" "$before|f"
check "the cold partition is now a foreign partition with its own name and bound" "$(jan_shape m)" "f:-:$JAN_BOUND"
check "the foreign partition reads the exported file" \
	"$(q "SELECT srvname || ':' || array_to_string(ftoptions, ',') FROM pg_foreign_table t
	      JOIN pg_foreign_server s ON s.oid = t.ftserver WHERE t.ftrelid = 'm_jan'::regclass;")" \
	"tier_pq:path=$W/m_jan.parquet"
check_num "the local partition leaves no pgcolumnar storage row behind" \
	"$(q "SELECT count(*) FROM pgcolumnar.storage WHERE storage_id = ${jan_storage:-0};")" "0"
check_num "premise: the storage id was read before the move" "$([ -n "$jan_storage" ] && echo 1 || echo 0)" "1"
check_num "nor any row-group row" \
	"$(q "SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = ${jan_storage:-0};")" "0"

# ---- pruning over the mixed parent ------------------------------------------
plan_jan="$(q "EXPLAIN SELECT max(v) FROM m WHERE ts < '2026-01-20';")"
plan_feb="$(q "EXPLAIN SELECT max(v) FROM m WHERE ts >= '2026-02-10';")"
check "a query on the tiered range scans only the foreign partition" \
	"$(grep -c 'Foreign Scan' <<<"$plan_jan")|$(grep -c 'm_feb' <<<"$plan_jan")" "1|0"
check "a query on the other range scans only the local partition, beside a foreign one" \
	"$(grep -c 'Foreign Scan' <<<"$plan_feb")|$(grep -c 'm_feb' <<<"$plan_feb")|$(grep -c 'Foreign Scan' <<<"$plan_jan")" "0|1|1"
check "an insert routed to the tiered range is refused" \
	"$(sqlstate "INSERT INTO m VALUES ('2026-01-15', 1, 1, 'x');")" "0A000"

# ---- refusals, before anything moves ----------------------------------------
# Measured with export_parquet on main: an existing path is overwritten without a
# word, which is why tier_partition must refuse one.
rb="$(digest m)"
psql_run "CREATE TABLE lone (ts timestamptz, v int) USING pgcolumnar;"
check "a table that is not a partition is refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('lone', 'tier_pq', '$W/lone.parquet');")" "42809"
psql_run "CREATE TABLE m_mar PARTITION OF m FOR VALUES FROM ('2026-03-01') TO ('2026-04-01') USING heap;"
# Refused by export_parquet, the first step, so nothing has moved: the arm holds
# the property, not which line of tier_partition raises it.
check "a partition that is not columnar is refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_mar', 'tier_pq', '$W/m_mar.parquet');")" "42809"
psql_run "CREATE TABLE m_def PARTITION OF m DEFAULT USING pgcolumnar;"
check "a DEFAULT partition is refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_def', 'tier_pq', '$W/m_def.parquet');")" "0A000"
psql_run "CREATE SERVER tier_ice FOREIGN DATA WRAPPER pgcolumnar_iceberg;"
# Before anything moves (#1339): a refusal that came only from a later step, after
# the export, would leave the file behind with the same SQLSTATE.
check "a server of another wrapper is refused" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_ice', '$W/m_feb.parquet');")|$([ -e "$W/m_feb.parquet" ] && echo file || echo nofile)" \
	"42809|nofile"
check "an existing path is refused, so no file a table reads is overwritten" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', '$W/m_jan.parquet');")" "58P02"
# The readers read s3a:// and s3n:// as s3://, and match the scheme in any case
# (#1930), so these spellings name the object a tier to s3:// would overwrite
# (#1936). The refusal comes before the export, so no store is contacted.
psql_run "CREATE FOREIGN TABLE dup_s3a (x int) SERVER tier_pq OPTIONS (path 's3a://tierb/m_feb_a.parquet');"
psql_run "CREATE FOREIGN TABLE dup_upper (x int) SERVER tier_pq OPTIONS (path 'S3://tierb/m_feb_u.parquet');"
check "a path a foreign table reads as s3a:// is refused like the identical s3:// spelling" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/m_feb_a.parquet');")" "58P02"
check "a path a foreign table reads as S3:// is refused like the identical s3:// spelling" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('m_feb', 'tier_pq', 's3://tierb/m_feb_u.parquet');")" "58P02"
psql_run "DROP FOREIGN TABLE dup_s3a, dup_upper;"
check "and the refusals moved nothing" "$(digest m)" "$rb"
check "the other partition is still local after the refusals" \
	"$(q "SELECT relkind FROM pg_class WHERE oid = 'm_feb'::regclass;")" "r"

# ---- the parent's lock ------------------------------------------------------
build k
kb="$(digest k)"
lock="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At \
	-c "BEGIN" -c "SELECT pgcolumnar.tier_partition('k_jan', 'tier_pq', '$W/k_jan.parquet')" \
	-c "SELECT coalesce(string_agg(mode, ',' ORDER BY mode), 'none') FROM pg_locks
	    WHERE relation = 'k'::regclass AND pid = pg_backend_pid() AND granted" \
	-c "ROLLBACK" 2>&1)"
# Line 2 is the call's result: a rollback of a call that never ran proves nothing.
check "inside the transaction the call moved the cold partition" "$(sed -n 2p <<<"$lock")" "8928"
check "the call holds ACCESS EXCLUSIVE on the parent until commit, as the docs say" \
	"$(grep -o 'AccessExclusiveLock' <<<"$lock")" "AccessExclusiveLock"
check "a rollback after the whole call leaves the rows local, exactly once" \
	"$(digest k)|$(jan_shape k)" "$kb|r:pgcolumnar:$JAN_BOUND"

# ---- a write in flight when the move starts ---------------------------------
# Without the SHARE lock the export does not see an uncommitted insert, DETACH
# then waits for it to commit, and DROP takes the row with the partition: lost.
# With it, the move waits for the insert and exports the row.
build w
wb="$(q "SELECT count(*) FROM w;")"; wj="$(q "SELECT count(*) FROM w_jan;")"
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At \
	-c "BEGIN" -c "INSERT INTO w_jan VALUES ('2026-01-31 23:59:00+00', 99, 4242, 'in flight')" \
	-c "SELECT pg_sleep(3)" -c "COMMIT" >/dev/null 2>&1 &
writer=$!
sleep 1
t0=$(date +%s%N)
moved="$(q "SELECT pgcolumnar.tier_partition('w_jan', 'tier_pq', '$W/w_jan.parquet');")"
waited=$(( ($(date +%s%N) - t0) / 1000000 ))
wait "$writer"
echo "-- the move waited ${waited} ms for the writer"
check_num "premise: the move started while the insert was in flight, and waited for it" \
	"$([ "$waited" -ge 1000 ] && echo 1 || echo "waited only $waited ms")" "1"
check "a write in flight when the move starts is in the exported rows" \
	"$moved|$(q "SELECT count(*) FROM w;")|$(q "SELECT count(*) FROM w WHERE note = 'in flight';")" \
	"$((wj + 1))|$((wb + 1))|1"

# ---- isolation: a writer that commits between the caller's snapshot and the lock
# Measured by @jdatcmd on #1295: the SHARE lock does not refresh a REPEATABLE READ
# snapshot, so the export missed rows that DROP then took with the partition.
iso_sqlstate() { sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\).*/\1/p' "$1" | head -1; }
n=0
for lvl in "read committed" "repeatable read" "serializable"; do
	n=$((n + 1)); build i
	ib="$(q "SELECT count(*) FROM i;")"; ij="$(q "SELECT count(*) FROM i_jan;")"
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At \
		-v VERBOSITY=sqlstate -c "BEGIN ISOLATION LEVEL $lvl" -c "SELECT 'seen=' || count(*) FROM i_jan" \
		-c "SELECT pg_sleep(2)" \
		-c "SELECT 'moved=' || pgcolumnar.tier_partition('i_jan', 'tier_pq', '$W/i_$n.parquet')" \
		-c "COMMIT" > "$W/iso_$n.out" 2>&1 &
	mover=$!
	sleep 1
	q "INSERT INTO i_jan VALUES ('2026-01-20 12:00:00+00', 7, 7, 'committed between');" >/dev/null
	wait "$mover"
	check_num "premise: at $lvl the caller's snapshot was taken before the writer committed" \
		"$(sed -n 's/^seen=//p' "$W/iso_$n.out")" "$ij"
	check "at $lvl a row committed between the caller's snapshot and the lock survives" \
		"$(q "SELECT count(*) FROM i;")|$(q "SELECT count(*) FROM i WHERE note = 'committed between';")" "$((ib + 1))|1"
	if [ "$lvl" = "read committed" ]; then
		check "at read committed the move goes ahead and exports that row" \
			"$(sed -n 's/^moved=//p' "$W/iso_$n.out")|$(q "SELECT relkind FROM pg_class WHERE oid = 'i_jan'::regclass;")" "$((ij + 1))|f"
	else
		check "at $lvl the move is refused, and the rows stay local" \
			"$(iso_sqlstate "$W/iso_$n.out")|$(q "SELECT relkind FROM pg_class WHERE oid = 'i_jan'::regclass;")" "25000|r"
	fi
done

# ---- refused up front: what ATTACH or DROP would trip over --------------------
# Each refusal leaves the rows local and writes no file: before this, a UNIQUE
# index on the parent surfaced at ATTACH, after the export, as an orphan file.
refused_cleanly() {	# refused_cleanly TABLE PATH -> "rows|relkind|file"
	echo "$(digest "$1")|$(q "SELECT relkind FROM pg_class WHERE oid = '${1}_jan'::regclass;")|$([ -e "$2" ] && echo file || echo nofile)"
}
build u
psql_run "CREATE UNIQUE INDEX u_key ON u (ts);"
ub="$(digest u)"
check "a parent with a UNIQUE index is refused before anything moves" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('u_jan', 'tier_pq', '$W/u.parquet');")|$(refused_cleanly u "$W/u.parquet")" \
	"0A000|$ub|r|nofile"

# A server that does not exist (#1341). Without its own branch the NULL wrapper name
# compares as NULL, nothing is refused, and core's own "server does not exist"
# arrives only at CREATE FOREIGN TABLE, after the export has written the file.
build ns
nsb="$(digest ns)"
check "a server that does not exist is refused before anything moves" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('ns_jan', 'tier_no_such', '$W/ns.parquet');")|$(refused_cleanly ns "$W/ns.parquet")" \
	"42704|$nsb|r|nofile"

# The fault hook's own guard (#1341): a step that is not one of tier_partition's
# must be refused, so a typo cannot leave a test believing it injected a fault.
check "a fault step that is not one of tier_partition's is refused" \
	"$(sqlstate "SET pgcolumnar.tier_fault_after = 'exprot';")" "22023"

build d
db="$(digest d)"
# Each dependent made and removed in statements of its own, with a premise that it
# exists: a multi-statement -c is one transaction, and in the first draft a failed
# CREATE rolled back the DROP of the case before, so the foreign-key arm passed
# on a leftover trigger and tested nothing.
dep_case() {	# dep_case PATH PREMISE_SQL -> "premise|sqlstate|rows|relkind|file"
	echo "$(q "$2")|$(sqlstate "SELECT pgcolumnar.tier_partition('d_jan', 'tier_pq', '$1');")|$(refused_cleanly d "$1")"
}
psql_run "CREATE VIEW d_view AS SELECT * FROM d_jan;"
check "a partition a view depends on is refused before anything moves" \
	"$(dep_case "$W/d1.parquet" "SELECT count(*) FROM pg_views WHERE viewname = 'd_view';")" "1|2BP01|$db|r|nofile"
psql_run "DROP VIEW d_view;"
psql_run "ALTER TABLE d_jan ENABLE ROW LEVEL SECURITY;"
psql_run "CREATE POLICY d_pol ON d_jan USING (dev > 0);"
check "a partition with a row-level policy is refused before anything moves" \
	"$(dep_case "$W/d2.parquet" "SELECT count(*) FROM pg_policy WHERE polname = 'd_pol';")" "1|2BP01|$db|r|nofile"
psql_run "DROP POLICY d_pol ON d_jan;"
psql_run "ALTER TABLE d_jan DISABLE ROW LEVEL SECURITY;"
psql_run "CREATE FUNCTION d_trg() RETURNS trigger LANGUAGE plpgsql AS \$\$BEGIN RETURN NEW; END\$\$;"
psql_run "CREATE TRIGGER d_local BEFORE UPDATE ON d_jan FOR EACH ROW EXECUTE FUNCTION d_trg();"
check "a partition with a trigger of its own is refused before anything moves" \
	"$(dep_case "$W/d3.parquet" "SELECT count(*) FROM pg_trigger WHERE tgname = 'd_local';")" "1|2BP01|$db|r|nofile"
psql_run "DROP TRIGGER d_local ON d_jan;"
# A foreign key REFERENCING a columnar table is refused by pgcolumnar itself (row
# locking), so the case that can arise is one the partition holds: a foreign
# table cannot carry it, and DROP would take it.
psql_run "CREATE TABLE d_dev (k int PRIMARY KEY) USING heap;"
psql_run "INSERT INTO d_dev SELECT g FROM generate_series(0, 12) g;"
psql_run "ALTER TABLE d_jan ADD CONSTRAINT d_fk FOREIGN KEY (dev) REFERENCES d_dev (k);"
check "a partition with a foreign key of its own is refused before anything moves" \
	"$(dep_case "$W/d4.parquet" "SELECT count(*) FROM pg_constraint WHERE conname = 'd_fk';")" "1|2BP01|$db|r|nofile"
psql_run "ALTER TABLE d_jan DROP CONSTRAINT d_fk;"
check_num "with the dependents gone, the same partition moves" \
	"$(q "SELECT pgcolumnar.tier_partition('d_jan', 'tier_pq', '$W/d5.parquet');")" "8928"

# ---- what the foreign table carries over --------------------------------------
build a
psql_run "CREATE ROLE tier_reader; GRANT SELECT ON a_jan TO tier_reader;
          COMMENT ON TABLE a_jan IS 'January, cold';"
check_num "the cold partition was moved with its grant and comment set" \
	"$(q "SELECT pgcolumnar.tier_partition('a_jan', 'tier_pq', '$W/a.parquet');")" "8928"
check "the foreign partition keeps the partition's grants and comment" \
	"$(q "SELECT relkind::text || '|' || has_table_privilege('tier_reader', 'a_jan', 'SELECT') || '|' ||
	         coalesce(obj_description('a_jan'::regclass, 'pg_class'), '-') FROM pg_class WHERE oid = 'a_jan'::regclass;")" \
	"f|true|January, cold"

# ---- an insert routed to the cold partition, racing the swap ------------------
# The mover holds SHARE on the partition and then asks for ACCESS EXCLUSIVE on the
# parent; an insert through the parent holds ROW EXCLUSIVE on the parent and waits
# for the partition. Whichever side the deadlock detector aborts, every row must
# be exactly once.
build r
rb="$(q "SELECT count(*) FROM r;")"
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At -v VERBOSITY=sqlstate \
	-c "SET pgcolumnar.tier_fault_after = 'pause'" \
	-c "SELECT 'moved=' || pgcolumnar.tier_partition('r_jan', 'tier_pq', '$W/r.parquet')" > "$W/race_mover.out" 2>&1 &
mover=$!
sleep 1
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At -v VERBOSITY=sqlstate \
	-c "INSERT INTO r VALUES ('2026-01-26 08:00:00+00', 3, 3, 'raced')" > "$W/race_writer.out" 2>&1
wait "$mover"
echo "-- race: mover [$(tr '\n' ' ' < "$W/race_mover.out")] writer [$(tr '\n' ' ' < "$W/race_writer.out")]"
raced="$(q "SELECT count(*) FROM r WHERE note = 'raced';")"
check_num "premise: one side of the race gave way or both went through, and nothing hung" \
	"$(grep -c 'moved=\|ERROR' "$W/race_mover.out")" "1"
# THE WANT COMES FROM THE WRITER, NOT FROM THE TABLE (#1339). Reading `raced` back
# and putting it on both sides held for 0 and 1 alike, so a row the writer was told
# had committed and the move then dropped passed: with tier_partition's LOCK ... IN
# SHARE MODE removed, the insert commits into the partition during the pause and
# the DROP takes it. A committed insert must be there exactly once, a refused one
# not at all.
w_ok="$(grep -c '^INSERT 0 1$' "$W/race_writer.out")"
check "an insert racing the move leaves every row exactly once, whichever side gives way" \
	"$(q "SELECT count(*) FROM r;")|$raced" "$((rb + w_ok))|$w_ok"

# ---- a bounded wait for the parent's lock -------------------------------------
build l
lb="$(digest l)"
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At \
	-c "BEGIN" -c "SELECT count(*) FROM l" -c "SELECT pg_sleep(6)" -c "COMMIT" >/dev/null 2>&1 &
reader=$!
sleep 1
t0=$(date +%s%N)
lk="$(sqlstate "SELECT pgcolumnar.tier_partition('l_jan', 'tier_pq', '$W/l.parquet', lock_wait => '1 second');")"
lms=$(( ($(date +%s%N) - t0) / 1000000 ))
wait "$reader"
echo "-- the move gave up on the parent's lock after ${lms} ms"
check "a parent held by a long reader times the move out, as lock_wait says" \
	"$lk|$([ "$lms" -lt 4000 ] && echo prompt || echo "took $lms ms")" "55P03|prompt"
check "and the timed-out move leaves the rows local, with nothing attached" \
	"$(digest l)|$(q "SELECT relkind FROM pg_class WHERE oid = 'l_jan'::regclass;")" "$lb|r"

# The bound is set for the move's own locks only: set_config(..., true) would
# otherwise last to the end of the caller's transaction.
build t
lt="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At \
	-c "BEGIN" -c "SET LOCAL lock_timeout = '7s'" \
	-c "SELECT 'moved=' || pgcolumnar.tier_partition('t_jan', 'tier_pq', '$W/t.parquet', lock_wait => '3 seconds')" \
	-c "SHOW lock_timeout" -c "ROLLBACK" 2>&1)"
check "the move ran inside the caller's transaction" "$(sed -n 's/^moved=//p' <<<"$lt")" "8928"
check "after the move the caller's lock_timeout is the caller's again" "$(sed -n '/^moved=/{n;p}' <<<"$lt")" "7s"

# ---- a retry after a failure, over the file the failure left ------------------
build o
ob="$(digest o)"
o_err="$(errtext "SET pgcolumnar.tier_fault_after = 'export'" \
	"SELECT pgcolumnar.tier_partition('o_jan', 'tier_pq', '$W/o.parquet')")"
check "the first attempt failed after export and left its file" \
	"$(grep -o 'injected fault after export' <<<"$o_err")|$([ -e "$W/o.parquet" ] && echo file || echo nofile)" \
	"injected fault after export|file"
check "a retry over the failed attempt's file moves the partition, exactly once" \
	"$(q "SELECT pgcolumnar.tier_partition('o_jan', 'tier_pq', '$W/o.parquet');")|$(digest o)|$(q "SELECT relkind FROM pg_class WHERE oid = 'o_jan'::regclass;")" \
	"8928|$ob|f"

# ---- names and columns the foreign table must reproduce (#1317 review) ---------
# Each failed after the export in the first version: a quoted schema, owner or
# grantee was quoted twice (%I over a reg* cast that quotes itself), and ATTACH
# refused a column whose collation or generation the foreign table lacked.
# Each moves a small partition, which must then read back as before.
mini() {	# mini SCHEMA.TABLE [COLUMNS]: a 100-row parent with one January partition
	psql_run "CREATE TABLE $1 (ts timestamptz NOT NULL, v int${2:-}) PARTITION BY RANGE (ts);"
	psql_run "CREATE TABLE ${1}_jan PARTITION OF $1 FOR VALUES FROM ('2026-01-01') TO ('2026-02-01') USING pgcolumnar;"
	psql_run "INSERT INTO $1 (ts, v) SELECT '2026-01-01'::timestamptz + g * interval '7 h', g FROM generate_series(0, 99) g;"
}
moved_reads() {	# moved_reads SCHEMA.TABLE PATH COLUMNS-TO-READ -> rows moved|relkind|digest-equal
	local before after
	before="$(q "SELECT md5(string_agg($3, ',' ORDER BY ts)) FROM $1;")"
	out="$(sqlstate_or "SELECT pgcolumnar.tier_partition('${1}_jan', 'tier_pq', '$2');")"
	after="$(q "SELECT md5(string_agg($3, ',' ORDER BY ts)) FROM $1;")"
	echo "$out|$(q "SELECT relkind FROM pg_class WHERE oid = '${1}_jan'::regclass;")|$([ -n "$before" ] && [ "$before" = "$after" ] && echo same || echo "differs")"
}
sqlstate_or() {
	local o
	o=$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At \
		-v ON_ERROR_STOP=1 -v VERBOSITY=sqlstate -c "$1" 2>&1)
	case "$o" in ERROR:*) sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\).*/\1/p' <<<"$o" | head -1;; *) echo "$o";; esac
}
psql_run 'CREATE SCHEMA "Tier Q";'
mini '"Tier Q".qs'
check "a partition in a schema whose name needs quoting moves and reads back the same" \
	"$(moved_reads '"Tier Q".qs' "$W/qs.parquet" "ts::text || v")" "100|f|same"
psql_run 'CREATE ROLE "Owner X";'
mini qo
psql_run 'ALTER TABLE qo_jan OWNER TO "Owner X";'
check "a partition owned by a role whose name needs quoting moves, and keeps that owner" \
	"$(moved_reads qo "$W/qo.parquet" "ts::text || v")|$(q "SELECT relowner::regrole FROM pg_class WHERE oid = 'qo_jan'::regclass;")" \
	'100|f|same|"Owner X"'
psql_run 'CREATE ROLE "Reader Y";'
mini qg
psql_run 'GRANT SELECT ON qg_jan TO "Reader Y";'
check "a grant to a role whose name needs quoting is carried to the foreign partition" \
	"$(moved_reads qg "$W/qg.parquet" "ts::text || v")|$(q "SELECT has_table_privilege('Reader Y', 'qg_jan', 'SELECT');")" \
	"100|f|same|t"
mini qc ', t text COLLATE "C"'
psql_run "UPDATE qc SET t = 'r' || v;"
check "a column with its own collation moves and reads back the same" \
	"$(moved_reads qc "$W/qc.parquet" "ts::text || v || t")" "100|f|same"
mini qgs ', w int GENERATED ALWAYS AS (v * 3) STORED'
check "a stored generated column moves and reads back the same" \
	"$(moved_reads qgs "$W/qgs.parquet" "ts::text || v || w")" "100|f|same"
if [ "$PGC_MAJOR" -ge 18 ]; then
	mini qgv ', w int GENERATED ALWAYS AS (v * 5) VIRTUAL'
	check "a virtual generated column moves and reads back the same" \
		"$(moved_reads qgv "$W/qgv.parquet" "ts::text || v || w")" "100|f|same"
else
	check_skip "a virtual generated column moves and reads back the same" \
		"SKIP  a virtual generated column moves and reads back the same (PostgreSQL $PGC_MAJOR has none; 18 added them)" \
		"PostgreSQL $PGC_MAJOR has no virtual generated columns"
fi

# ---- the caller's own privileges (#1341) -------------------------------------
#
# tier_partition is not SECURITY DEFINER: every step runs as the caller, so the
# owner-only steps (its LOCK ... IN SHARE MODE first) stop a role that does not own
# the table.
# Made SECURITY DEFINER, it would run as the extension's owner and let any caller
# detach, drop and replace a partition it does not own. Two roles, each holding
# every other privilege the call needs (both server-file roles, for the export and
# for the read-back of #1795, USAGE on the server and CREATE on the schema for the
# foreign table, and SELECT on the partition), so ownership is the only thing
# between them.
for r in tier_own tier_peer; do
	psql_run "DROP ROLE IF EXISTS $r; CREATE ROLE $r LOGIN NOSUPERUSER;
	          GRANT pg_write_server_files, pg_read_server_files TO $r; GRANT USAGE ON FOREIGN SERVER tier_pq TO $r;
	          GRANT USAGE, CREATE ON SCHEMA public TO $r; GRANT USAGE ON SCHEMA pgcolumnar TO $r;"
done
sqlstate_as() {	# sqlstate_as ROLE SQL -> the SQLSTATE SQL fails with as ROLE, or ok
	local out
	out=$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U "$1" -d "$PGC_DB" \
		-X -At -v ON_ERROR_STOP=1 -v VERBOSITY=sqlstate -c "$2" 2>&1 >/dev/null)
	[ -z "$out" ] && { echo ok; return; }
	echo "$out" | sed -n 's/^\(ERROR\|FATAL\): *\([0-9A-Z]\{5\}\).*/\2/p' | head -1
}
# The owner's control has a table of its own (pw), so a refusal that let the peer
# through cannot make the control fail for the peer's reason.
for t in po pw; do
	build $t
	psql_run "ALTER TABLE $t OWNER TO tier_own; ALTER TABLE ${t}_jan OWNER TO tier_own;
	          ALTER TABLE ${t}_feb OWNER TO tier_own;"
done
psql_run "GRANT SELECT ON po, po_jan TO tier_peer;"
pb="$(digest po)"
check "premise: tier_own owns the parent and its partitions, and neither role is a superuser" \
	"$(q "SELECT string_agg(DISTINCT pg_get_userbyid(relowner), ',') FROM pg_class WHERE relname IN ('po', 'po_jan', 'po_feb', 'pw', 'pw_jan', 'pw_feb');")|$(q "SELECT count(*) FROM pg_roles WHERE rolname IN ('tier_own', 'tier_peer') AND rolsuper;")" \
	"tier_own|0"
check "premise: tier_peer can open a session and read the partition" \
	"$(sqlstate_as tier_peer "SELECT count(*) FROM po_jan")" "ok"
check "a role that does not own the table is refused by its own privileges (42501)" \
	"$(sqlstate_as tier_peer "SELECT pgcolumnar.tier_partition('po_jan', 'tier_pq', '$W/po_peer.parquet')")" "42501"
check "and the refused call leaves the partition local and attached, every row once" \
	"$(digest po)|$(jan_shape po)" "$pb|r:pgcolumnar:$JAN_BOUND"
check_num "with no foreign table behind it" \
	"$(q "SELECT count(*) FROM pg_foreign_table t JOIN pg_class c ON c.oid = t.ftrelid WHERE c.relname LIKE 'po\\_%';")" "0"
# The first statement to need more than SELECT is the LOCK ... IN SHARE MODE ahead
# of the export, so the refusal comes before any file is written (measured on 15-19).
check "and the refused call wrote no export file" \
	"$([ -e "$W/po_peer.parquet" ] && echo written || echo absent)" "absent"
check "a non-superuser owner tiers its own partition" \
	"$(sqlstate_as tier_own "SELECT pgcolumnar.tier_partition('pw_jan', 'tier_pq', '$W/pw_own.parquet')")|$(jan_shape pw)" \
	"ok|f:-:$JAN_BOUND"

# ---- a failure after each step ----------------------------------------------
for step in export detach drop create attach; do
	build f
	fb="$(digest f)"
	err="$(errtext "SET pgcolumnar.tier_fault_after = '$step'" \
		"SELECT pgcolumnar.tier_partition('f_jan', 'tier_pq', '$W/f_$step.parquet')")"
	check "the call fails with the fault injected after $step" \
		"$(grep -o "injected fault after $step" <<<"$err" || echo "$err")" "injected fault after $step"
	check "a failure after $step leaves the rows exactly once, still local" \
		"$(digest f)|$(jan_shape f)" "$fb|r:pgcolumnar:$JAN_BOUND"
	check_num "and no foreign table behind it after $step" \
		"$(q "SELECT count(*) FROM pg_foreign_table t JOIN pg_class c ON c.oid = t.ftrelid
		      WHERE c.relname LIKE 'f\\_%';")" "0"
done


# ---- the write uses the named server and the caller's mapping (#1795) --------
#
# The foreign partition reads with the server's endpoint and region and the
# caller's user mapping. The write must use the same, or the move commits a
# partition that cannot read its own file. Two SigV4-verifying fixture servers,
# each with its own directory and secret: A is the one the postmaster environment
# names, B is reachable only through a server and a mapping. Where an object
# lands shows which identity wrote it.
S3_REGION="pgc-test-1"; S3_KEY="PGCTESTKEYID"
S3A_SECRET="pgctest-env-secret"; S3B_SECRET="pgctest-mapping-secret"
S3_PIDS=""
tier_s3_on_exit() {
	local rc=$?
	[ -n "$S3_PIDS" ] && kill $S3_PIDS 2>/dev/null
	(exit $rc)
	pgc_on_exit
}
trap tier_s3_on_exit EXIT
# s3_start NAME SECRET VAR: start a fixture whose bucket is $PGC_WORKDIR/NAME/tierb and
# set VAR to its port. Not run in $(...): the PID must reach S3_PIDS in this shell,
# or the trap cannot stop the server, and a server left running keeps the
# caller's lock file descriptor open.
s3_start() {
	local port out="$PGC_WORKDIR/$1.out"
	mkdir -p "$PGC_WORKDIR/$1/tierb" && chmod -R 777 "$PGC_WORKDIR/$1"
	port="$(pgc_pick_free_port "$PGC_AUX_PORT_LO" "$PGC_AUX_PORT_HI")"
	python3 "$(dirname "${BASH_SOURCE[0]}")/objstore_http_server.py" \
		--dir "$PGC_WORKDIR/$1" --port "$port" --log "$PGC_WORKDIR/$1.log" \
		--sigv4-key "$S3_KEY" --sigv4-secret "$2" --sigv4-region "$S3_REGION" >"$out" 2>&1 &
	S3_PIDS="$S3_PIDS $!"
	for _ in $(seq 1 50); do grep -q READY "$out" 2>/dev/null && break; sleep 0.1; done
	printf -v "$3" '%s' "$port"
}
s3_start s3a "$S3A_SECRET" S3A_PORT
s3_start s3b "$S3B_SECRET" S3B_PORT
check "premise: both SigV4 fixture servers are up" \
	"$(cat "$PGC_WORKDIR/s3a.out" "$PGC_WORKDIR/s3b.out" 2>/dev/null | grep -c READY)" "2"
pgc_pg "pg_ctl -D '$PGC_PGDATA' stop -m fast -w" >/dev/null 2>&1
pgc_pg "AWS_ENDPOINT_URL=http://127.0.0.1:$S3A_PORT AWS_ACCESS_KEY_ID=$S3_KEY \
	AWS_SECRET_ACCESS_KEY=$S3A_SECRET AWS_REGION=$S3_REGION \
	pg_ctl -D '$PGC_PGDATA' -l '$PGC_LOGFILE' start -w" >/dev/null 2>&1
for _ in $(seq 1 30); do [ -n "$(q 'SELECT 1')" ] && break; sleep 0.5; done
# mdigest TABLE -> count|md5 of a mini table's rows, or the error that stopped the read
mdigest() {
	local out
	out=$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At \
		-v ON_ERROR_STOP=1 -c "SELECT count(*) || '|' || md5(string_agg(ts::text || v, ',' ORDER BY ts)) FROM $1;" 2>&1)
	echo "$out" | sed -n 's/^ERROR: *//p;/^[0-9]*|/p' | head -1
}
objects() { (cd "$PGC_WORKDIR/$1/tierb" 2>/dev/null && ls | sort | tr '\n' ' ' | sed 's/ $//'); }
mini sp
check "premise: the postmaster environment writes to fixture A" \
	"$(q "SELECT pgcolumnar.export_parquet('sp_jan', 's3://tierb/premise.parquet');")|$(objects s3a)" \
	"100|premise.parquet"

# The repro from #1795: the server names fixture A, the environment holds A's
# valid keys, and the caller's mapping holds a wrong secret.
psql_run "CREATE SERVER tier_s3a FOREIGN DATA WRAPPER pgcolumnar_parquet
          OPTIONS (endpoint 'http://127.0.0.1:$S3A_PORT', region '$S3_REGION');
          CREATE USER MAPPING FOR postgres SERVER tier_s3a
          OPTIONS (access_key_id '$S3_KEY', secret_access_key 'pgctest-bogus');"
mini sa
sab="$(mdigest sa)"
check "a mapping the object store rejects refuses the move (28000), not the environment's keys" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('sa_jan', 'tier_s3a', 's3://tierb/sa_jan.parquet')")" "28000"
check "and the refused move leaves the rows local and attached, every row once" \
	"$(mdigest sa)|$(jan_shape sa)" "$sab|r:pgcolumnar:$JAN_BOUND"
check "and the environment's keys wrote no object" "$(objects s3a)" "premise.parquet"

# The server names fixture B, which only the mapping's secret opens.
psql_run "CREATE SERVER tier_s3b FOREIGN DATA WRAPPER pgcolumnar_parquet
          OPTIONS (endpoint 'http://127.0.0.1:$S3B_PORT', region '$S3_REGION');
          CREATE USER MAPPING FOR postgres SERVER tier_s3b
          OPTIONS (access_key_id '$S3_KEY', secret_access_key '$S3B_SECRET');"
mini sb
sbb="$(mdigest sb)"
check "a move through a server writes with its endpoint and the caller's mapping" \
	"$(errtext "SELECT pgcolumnar.tier_partition('sb_jan', 'tier_s3b', 's3://tierb/sb_jan.parquet')")|A: $(objects s3a)|B: $(objects s3b)" \
	"ok|A: premise.parquet|B: sb_jan.parquet"
check "and the parent reads every row back through the foreign partition" \
	"$(mdigest sb)|$(jan_shape sb)" "$sbb|f:-:$JAN_BOUND"

# ---- export_parquet(rel, path, server) refuses before it writes (#1795) --------
#
# The three-argument form is public. Each refusal below must come before the
# file is opened, so neither fixture may hold an object afterwards.
#
# tier_s3pub names fixture B and has a PUBLIC mapping with B's valid secret. A
# role without USAGE on it would write to B with that mapping if the USAGE gate
# were gone, so the refusal guards credentials the role was never given.
# tier_s3half has a mapping for postgres with an access key and no secret.
psql_run "CREATE FOREIGN DATA WRAPPER tier_dummy; CREATE SERVER tier_other FOREIGN DATA WRAPPER tier_dummy;
          CREATE SERVER tier_s3pub FOREIGN DATA WRAPPER pgcolumnar_parquet
          OPTIONS (endpoint 'http://127.0.0.1:$S3B_PORT', region '$S3_REGION');
          CREATE USER MAPPING FOR PUBLIC SERVER tier_s3pub
          OPTIONS (access_key_id '$S3_KEY', secret_access_key '$S3B_SECRET');
          CREATE SERVER tier_s3half FOREIGN DATA WRAPPER pgcolumnar_parquet
          OPTIONS (endpoint 'http://127.0.0.1:$S3B_PORT', region '$S3_REGION');
          CREATE USER MAPPING FOR postgres SERVER tier_s3half OPTIONS (access_key_id '$S3_KEY');
          DROP ROLE IF EXISTS tier_s3nouse; CREATE ROLE tier_s3nouse LOGIN NOSUPERUSER;
          GRANT pg_write_server_files TO tier_s3nouse; GRANT SELECT ON sp_jan TO tier_s3nouse;
          GRANT USAGE ON SCHEMA pgcolumnar TO tier_s3nouse;"
check "premise: the PUBLIC mapping on tier_s3pub writes to fixture B" \
	"$(errtext "SELECT pgcolumnar.export_parquet('sp_jan', 's3://tierb/pub_premise.parquet', 'tier_s3pub')")|$(objects s3b | tr ' ' '\n' | grep -c '^pub_premise')" \
	"ok|1"
# Backends killed by a signal, from the server log: a number, or nothing when the
# log cannot be read, so that a blind count never reads as "no crashes".
crashes() {
	local n
	n="$(pgc_pg "grep -c 'terminated by signal' '$PGC_LOGFILE'" 2>/dev/null)"
	case "$n" in '' | *[!0-9]*) ;; *) echo "$n" ;; esac
}
gA="$(objects s3a)"; gB="$(objects s3b)"; gcrash="$(crashes)"
check "export_parquet through a server of another wrapper is refused (42809)" \
	"$(sqlstate "SELECT pgcolumnar.export_parquet('sp_jan', 's3://tierb/g_other.parquet', 'tier_other')")" "42809"
check "export_parquet through a server that does not exist is refused (42704)" \
	"$(sqlstate "SELECT pgcolumnar.export_parquet('sp_jan', 's3://tierb/g_none.parquet', 'tier_nosuch')")" "42704"
check "export_parquet by a role without USAGE on the server is refused (42501)" \
	"$(sqlstate_as tier_s3nouse "SELECT pgcolumnar.export_parquet('sp_jan', 's3://tierb/g_usage.parquet', 'tier_s3pub')")" "42501"
check "export_parquet to a local path through a server whose mapping holds half a credential is refused (28000)" \
	"$(sqlstate "SELECT pgcolumnar.export_parquet('sp_jan', '$W/g_half.parquet', 'tier_s3half')")|$([ -e "$W/g_half.parquet" ] && echo file || echo nofile)" \
	"28000|nofile"
# The null arm runs last, so that a backend it crashes cannot fail the arms above
# for want of a server in recovery.
check "export_parquet through a null server is refused (22004)" \
	"$(sqlstate "SELECT pgcolumnar.export_parquet('sp_jan', 's3://tierb/g_null.parquet', NULL)")" "22004"
check_num "and no refused export crashed a backend" "$(crashes)" "$gcrash"
check "and no refused export wrote an object to A or B" "A: $(objects s3a)|B: $(objects s3b)" "A: $gA|B: $gB"
# After a crash the server takes a moment to accept connections again.
for _ in $(seq 1 60); do [ "$(q "SELECT 1;" 2>/dev/null)" = 1 ] && break; sleep 0.5; done

# ---- a non-superuser gets no environment keys (#1795) -------------------------
#
# The write takes the read's rule: the environment is a privilege of a superuser
# (or of a mapping a superuser marked credentials_required 'false'). Before #1795
# a member of pg_write_server_files with no mapping wrote with the environment's
# keys. Now it needs a mapping of its own, as it does to read.
count_as() {	# count_as ROLE TABLE -> count(*) of TABLE as ROLE, or the error that stopped it
	local out
	out=$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U "$1" -d "$PGC_DB" -X -At \
		-v ON_ERROR_STOP=1 -c "SELECT count(*) FROM $2;" 2>&1)
	echo "$out" | sed -n 's/^ERROR: *//p;/^[0-9][0-9]*$/p' | head -1
}
for r in tier_s3own tier_s3wo; do
	psql_run "DROP ROLE IF EXISTS $r; CREATE ROLE $r LOGIN NOSUPERUSER;
	          GRANT pg_write_server_files TO $r; GRANT USAGE ON FOREIGN SERVER tier_s3b TO $r;
	          GRANT USAGE, CREATE ON SCHEMA public TO $r; GRANT USAGE ON SCHEMA pgcolumnar TO $r;"
done
# tier_s3own can read server files too; tier_s3wo can only write them.
psql_run "GRANT pg_read_server_files TO tier_s3own;"
for t in so swo; do
	mini $t
done
psql_run "ALTER TABLE so OWNER TO tier_s3own; ALTER TABLE so_jan OWNER TO tier_s3own;
          ALTER TABLE swo OWNER TO tier_s3wo; ALTER TABLE swo_jan OWNER TO tier_s3wo;"
check "premise: neither owner is a superuser, and neither has a mapping on the server" \
	"$(q "SELECT count(*) FROM pg_roles WHERE rolname IN ('tier_s3own', 'tier_s3wo') AND rolsuper;")|$(q "SELECT count(*) FROM pg_user_mappings WHERE srvname = 'tier_s3b' AND usename IN ('tier_s3own', 'tier_s3wo', 'public');")" \
	"0|0"
gB="$(objects s3b)"
check "a non-superuser with no mapping is refused the export (28000), not given the environment's keys" \
	"$(sqlstate_as tier_s3own "SELECT pgcolumnar.export_parquet('so_jan', 's3://tierb/so_env.parquet', 'tier_s3b')")|A: $(objects s3a)" \
	"28000|A: $gA"
sob="$(mdigest so)"
check "a non-superuser owner with no mapping is refused the move (28000)" \
	"$(sqlstate_as tier_s3own "SELECT pgcolumnar.tier_partition('so_jan', 'tier_s3b', 's3://tierb/so_jan.parquet')")" "28000"
check "and its refused move leaves the rows local and attached, every row once, with no object written" \
	"$(mdigest so)|$(jan_shape so)|A: $(objects s3a)|B: $(objects s3b)" "$sob|r:pgcolumnar:$JAN_BOUND|A: $gA|B: $gB"
psql_run "CREATE USER MAPPING FOR tier_s3own SERVER tier_s3b
          OPTIONS (access_key_id '$S3_KEY', secret_access_key '$S3B_SECRET');"
check "a non-superuser owner moves its partition through its own mapping" \
	"$(sqlstate_as tier_s3own "SELECT pgcolumnar.tier_partition('so_jan', 'tier_s3b', 's3://tierb/so_jan.parquet')")|$(jan_shape so)|B: $(objects s3b)" \
	"ok|f:-:$JAN_BOUND|B: $(echo $gB so_jan.parquet | tr ' ' '\n' | sort | tr '\n' ' ' | sed 's/ $//')"
check "and the owner reads every row back through the foreign partition" \
	"$(count_as tier_s3own so)|$(mdigest so)" "100|$sob"

# ---- the move reads its file back before it commits (#1795, Fix point 2) ------
#
# The write proves the key can write. A key that can write but not read (an IAM
# policy with s3:PutObject and no s3:GetObject) would commit a partition its
# caller cannot read. The move must count the rows through the new foreign
# partition before it returns.
psql_run "CREATE USER MAPPING FOR tier_s3wo SERVER tier_s3b
          OPTIONS (access_key_id '$S3_KEY', secret_access_key '$S3B_SECRET');"
swb="$(mdigest swo)"
check "an owner without pg_read_server_files is refused the move before anything is written (42501)" \
	"$(sqlstate_as tier_s3wo "SELECT pgcolumnar.tier_partition('swo_jan', 'tier_s3b', 's3://tierb/swo_jan.parquet')")|B: $(objects s3b | tr ' ' '\n' | grep -c '^swo_jan')" \
	"42501|B: 0"
check "and the move refused for want of pg_read_server_files leaves the rows local and attached" \
	"$(mdigest swo)|$(jan_shape swo)" "$swb|r:pgcolumnar:$JAN_BOUND"
mini sr
srb="$(mdigest sr)"
: >"$PGC_WORKDIR/s3b/__deny_reads__"
check "a key that can write but not read refuses the move (28000)" \
	"$(sqlstate "SELECT pgcolumnar.tier_partition('sr_jan', 'tier_s3b', 's3://tierb/sr_jan.parquet')")" "28000"
rm -f "$PGC_WORKDIR/s3b/__deny_reads__"
check "and the move refused at the read-back leaves the rows local and attached, every row once" \
	"$(mdigest sr)|$(jan_shape sr)" "$srb|r:pgcolumnar:$JAN_BOUND"

pgc_summary
