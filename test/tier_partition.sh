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
check "premise: the first attempt failed after export and left its file" \
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
# every other privilege the call needs (the server-file role for the export, USAGE
# on the server and CREATE on the schema for the foreign table, and SELECT on the
# partition), so ownership is the only thing between them.
for r in tier_own tier_peer; do
	psql_run "DROP ROLE IF EXISTS $r; CREATE ROLE $r LOGIN NOSUPERUSER;
	          GRANT pg_write_server_files TO $r; GRANT USAGE ON FOREIGN SERVER tier_pq TO $r;
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
	check "premise: the call fails with the fault injected after $step" \
		"$(grep -o "injected fault after $step" <<<"$err" || echo "$err")" "injected fault after $step"
	check "a failure after $step leaves the rows exactly once, still local" \
		"$(digest f)|$(jan_shape f)" "$fb|r:pgcolumnar:$JAN_BOUND"
	check_num "and no foreign table behind it after $step" \
		"$(q "SELECT count(*) FROM pg_foreign_table t JOIN pg_class c ON c.oid = t.ftrelid
		      WHERE c.relname LIKE 'f\\_%';")" "0"
done

pgc_summary
