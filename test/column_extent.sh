#!/usr/bin/env bash
#
# The per-column extent (#1304 option 5, for #1292 phase 2).
#
# WRITTEN BEFORE THE CODE, against the design on #1304:
#
#     pgcolumnar.column_extent  catalog: (storage_id, column_index, complete,
#                               minimum, maximum), no GRANT
#     pgcolumnar.column_extent(rel regclass, col name)
#         -> (complete bool, minimum text, maximum text), zero rows when the
#            planner would fall back to the zone maps
#
# Each committing writer inserts one row of its own (never an in-place update), a
# complete row comes only from a recompute over every visible zone map, and every
# path that retires or rewrites groups recomputes in the same transaction. The
# planner trusts the extent only when a complete row is visible.
#
# THE ORACLE is the data: min() and max() of the column, printed by the same output
# function. A zone map can be wider than the live rows only by rows deleted but not
# yet retired, so every arm that asserts equality first retires or rewrites those;
# the one arm whose extent may legitimately be wider (a rolled-back savepoint that
# flushed a group) asserts containment, not equality.
#
# Usage:  test/column_extent.sh [PG_CONFIG]

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
export PGC_EXTRA_CONF=$'max_prepared_transactions=8\nmax_worker_processes=16'
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

q "CREATE EXTENSION IF NOT EXISTS pgcolumnar;" >/dev/null
check_num "premise: the extent function exists" \
	"$(q "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
	      WHERE n.nspname = 'pgcolumnar' AND p.proname = 'column_extent';")" "1"

ext() {		# ext TABLE COLUMN -> "complete|min|max", "none" when no complete extent, or the SQLSTATE
	# Its own psql, not q: "none" must mean the function answered with no row, and a
	# missing function or a refused call must not read as that.
	local r
	r="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At \
		-v ON_ERROR_STOP=1 -v VERBOSITY=sqlstate \
		-c "SELECT complete || '|' || coalesce(minimum, '-') || '|' || coalesce(maximum, '-') FROM pgcolumnar.column_extent('$1', '$2');" 2>&1)"
	case "$r" in ERROR:*) echo "error $(sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\).*/\1/p' <<<"$r" | head -1)";; "") echo none;; *) echo "$r";; esac
}
truth() {	# truth TABLE COLUMN -> "true|min|max" from the data itself
	q "SELECT 'true|' || min($2)::text || '|' || max($2)::text FROM $1;"
}
sid() { q "SELECT pgcolumnar.get_storage_id('$1');"; }
GB="SET pgcolumnar.enable_group_vectorization = on;"

# ---- the fold is the column type's own ordering ------------------------------
#
# Byte order is not value order for any of these: ints are stored little-endian
# (256 sorts before 1 as bytes), negatives and numerics have sign and scale in
# their bytes, and text under "C" puts 'Z' before 'a'. Several row groups, so the
# fold crosses groups as well as values.
q "CREATE TABLE ty (ts timestamptz, i int, n numeric, d date, s text COLLATE \"C\") USING pgcolumnar;
   SELECT pgcolumnar.set_options('ty', stripe_row_limit => 1000, chunk_group_row_limit => 1000);
   INSERT INTO ty SELECT '2026-02-01'::timestamptz + g * interval '1 min', (g * 37) % 3001 - 1500,
          ((g * 7919) % 20001 - 10000) / 7.0, date '2026-01-01' + (g % 400),
          chr(65 + (g % 26)) || chr(97 + (g % 26)) FROM generate_series(1, 6000) g;" >/dev/null
check "premise: the typed fixture spans several row groups" \
	"$([ "$(q "SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = $(sid ty);")" -ge 6 ] && echo yes || echo no)" "yes"
for c in ts i n d s; do
	check "the extent of ty.$c is the data's min and max, in the type's order" "$(ext ty $c)" "$(truth ty $c)"
done

# ---- the planner reads the extent, not the zone maps --------------------------
#
# The #1287 instrument native_groupagg uses: the planning transaction's own scans of
# zone_map and its index, which no other session can move. 50 groups, so a fallback
# would show as a scan and the extent as none.
q "CREATE TABLE pl (ts timestamptz, v float8) USING pgcolumnar;
   SELECT pgcolumnar.set_options('pl', stripe_row_limit => 1000, chunk_group_row_limit => 1000);
   INSERT INTO pl SELECT '2026-01-01'::timestamptz + g * interval '1 second', g FROM generate_series(1, 50000) g;
   ANALYZE pl;" >/dev/null
plan_scans() {	# zone_map scans and the estimate, planning a bucketed GROUP BY on pl
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At -q \
		-c "$GB" -c "BEGIN" -c "EXPLAIN SELECT date_trunc('minute', ts) b, avg(v) FROM pl GROUP BY b" \
		-c "SELECT 'scans=' || (pg_stat_get_xact_numscans('pgcolumnar.zone_map'::regclass)
		          + pg_stat_get_xact_numscans('pgcolumnar.zone_map_pkey'::regclass))" -c "ROLLBACK" 2>&1
}
pl_with="$(plan_scans)"
check "planning a bucketed GROUP BY scans zone_map not at all when the extent is complete" \
	"$(sed -n 's/^scans=//p' <<<"$pl_with")" "0"
# The same estimate from the fallback: remove the extent rows (superuser, as a stale
# catalog would look) and plan again.
pl_est_with="$(grep -oE 'rows=[0-9]+' <<<"$pl_with" | head -1)"
q "DELETE FROM pgcolumnar.column_extent WHERE storage_id = $(sid pl);" >/dev/null
pl_without="$(plan_scans)"
check "without the extent rows the planner falls back to one zone_map scan" \
	"$(sed -n 's/^scans=//p' <<<"$pl_without")" "1"
check "and the estimate is the one the zone maps give" "$pl_est_with" "$(grep -oE 'rows=[0-9]+' <<<"$pl_without" | head -1)"
check "and with no extent rows the function reports no complete extent" "$(ext pl ts)" "none"
# The writer-side recompute: the next committing writer finds no complete row and
# makes one, from every visible zone map, so the old groups are in it.
q "INSERT INTO pl VALUES ('2025-12-31 00:00:00+00', 0);" >/dev/null
check "a writer that finds no complete extent recomputes one over every group" "$(ext pl ts)" "$(truth pl ts)"

# ---- concurrent writers never share a row --------------------------------------
#
# Ordinary writers into one table are serialized by the storage-row lock (class
# 102) from their first flush to their commit, so they never overlap. The writers
# that do are parallel_copy's loaders: separate sessions flushing into one storage
# at once, each preparing its transaction. An in-place update from two of them
# would fail ("tuple concurrently updated") or lose one's widening; and each
# writes its row at PRE_PREPARE, so one missing would leave the extent too narrow.
# The file's earliest rows and its latest are far apart in it, so different loaders
# hold them.
q "CREATE TABLE cw (ts timestamptz, v int) USING pgcolumnar;
   SELECT pgcolumnar.set_options('cw', stripe_row_limit => 1000, chunk_group_row_limit => 1000);
   INSERT INTO cw SELECT '2026-06-01'::timestamptz + g * interval '1 s', g FROM generate_series(1, 2000) g;" >/dev/null
q "COPY (SELECT '2020-01-01'::timestamptz + g * interval '1 day', g FROM generate_series(1, 4000) g)
   TO '$PGC_WORKDIR/cw.txt' WITH (FORMAT text);" >/dev/null
check "parallel_copy's loaders, flushing into one table at once, all commit" \
	"$(q "SELECT pgcolumnar.parallel_copy('cw', '$PGC_WORKDIR/cw.txt', 4);")" "4000"
check "and the extent covers what they loaded" "$(ext cw ts)" "$(truth cw ts)"

# ---- a writer that aborts leaves nothing; a rolled-back savepoint only widens --
q "BEGIN; INSERT INTO cw SELECT '1990-01-01'::timestamptz + g * interval '1 s', g FROM generate_series(1, 3000) g; ROLLBACK;" >/dev/null
check "a writer that rolls back leaves the extent as the data has it" "$(ext cw ts)" "$(truth cw ts)"
q "BEGIN; INSERT INTO cw VALUES ('2026-06-02', 1); SAVEPOINT s;
   INSERT INTO cw SELECT '1980-01-01'::timestamptz + g * interval '1 s', g FROM generate_series(1, 3000) g;
   ROLLBACK TO s; COMMIT;" >/dev/null
contains() {	# contains TABLE COLUMN TYPE -> yes when the extent contains the data's range
	q "SELECT CASE WHEN e.complete AND e.minimum::$3 <= (SELECT min($2) FROM $1)
	                AND e.maximum::$3 >= (SELECT max($2) FROM $1) THEN 'yes' ELSE 'no' END
	   FROM pgcolumnar.column_extent('$1', '$2') e;"
}
check "a savepoint rolled back after flushing may widen the extent, never narrow it" "$(contains cw ts timestamptz)" "yes"

# A prepared transaction writes its extent row before PREPARE, where a catalog write
# is still allowed; its groups commit with COMMIT PREPARED and must be inside it.
# Its own table: cw's extent is already wider than its data, by the savepoint above.
q "CREATE TABLE tp (ts timestamptz, v int) USING pgcolumnar;
   SELECT pgcolumnar.set_options('tp', stripe_row_limit => 1000, chunk_group_row_limit => 1000);
   INSERT INTO tp SELECT '2026-06-01'::timestamptz + g * interval '1 s', g FROM generate_series(1, 2000) g;" >/dev/null
q "BEGIN; INSERT INTO tp SELECT '2040-01-01'::timestamptz + g * interval '1 s', g FROM generate_series(1, 3000) g;
   PREPARE TRANSACTION 'ce_2pc';" >/dev/null
q "COMMIT PREPARED 'ce_2pc';" >/dev/null
check "a prepared transaction's groups are inside the extent after COMMIT PREPARED" "$(ext tp ts)" "$(truth tp ts)"

# A table created and written inside a savepoint that rolls back leaves no extent
# row behind: its storage never existed for anyone, and an orphan row would be kept
# by nothing.
q "BEGIN; SAVEPOINT s; CREATE TABLE gone (ts timestamptz) USING pgcolumnar;
   INSERT INTO gone SELECT now() FROM generate_series(1, 2000);
   ROLLBACK TO s; COMMIT;" >/dev/null
check "a table created in a rolled-back savepoint leaves no extent rows" \
	"$(q "SELECT count(*) FROM pgcolumnar.column_extent e
	      WHERE NOT EXISTS (SELECT 1 FROM pgcolumnar.storage s WHERE s.storage_id = e.storage_id);")" "0"

# ---- a transaction reads its own flushed groups in the extent --------------------
#
# Its extent row is written at pre-commit, but its groups are visible to it from the
# moment they flush. A plan made in between must not bound the column by the
# committed rows alone: that is an UNDER-estimate, the direction the gate on this
# bound must never take. The oracle is the same transaction with the extent rows
# deleted, so the planner falls back to the zone maps, which do include them.
q "CREATE TABLE ot (ts timestamptz, v int) USING pgcolumnar;
   SELECT pgcolumnar.set_options('ot', stripe_row_limit => 1000, chunk_group_row_limit => 1000);
   INSERT INTO ot SELECT '2026-06-01'::timestamptz + g * interval '1 s', g FROM generate_series(1, 2000) g;
   ANALYZE ot;" >/dev/null
OWN_INS="INSERT INTO ot SELECT '2050-01-01'::timestamptz + g * interval '1 min', g FROM generate_series(1, 3000) g"
check "a transaction's own flushed groups are inside the extent it reads before commit" \
	"$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At -q \
		-c "BEGIN" -c "$OWN_INS" \
		-c "SELECT CASE WHEN e.complete AND e.minimum::timestamptz <= (SELECT min(ts) FROM ot)
		                AND e.maximum::timestamptz >= (SELECT max(ts) FROM ot) THEN 'yes' ELSE 'no' END
		    FROM pgcolumnar.column_extent('ot', 'ts') e" -c "ROLLBACK" 2>&1)" "yes"
own_plans="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At -q \
	-c "$GB" -c "BEGIN" -c "$OWN_INS" \
	-c "SELECT 'plan'" -c "EXPLAIN SELECT date_trunc('minute', ts) b, avg(v) FROM ot GROUP BY b" \
	-c "DELETE FROM pgcolumnar.column_extent WHERE storage_id = $(sid ot)" \
	-c "SELECT 'plan'" -c "EXPLAIN SELECT date_trunc('minute', ts) b, avg(v) FROM ot GROUP BY b" -c "ROLLBACK" 2>&1 |
	awk '/^plan$/ { want = 1; next } want && match($0, /rows=[0-9]+/) { print substr($0, RSTART, RLENGTH); want = 0 }')"
check "and a plan made in that transaction estimates as the zone maps do" \
	"$(sed -n 1p <<<"$own_plans")" "$(sed -n 2p <<<"$own_plans")"
check "premise: the transaction's plans gave two estimates" "$(wc -l <<<"$own_plans")" "2"

# ---- a shrink is recomputed, never guessed (jdatcmd, #1304) ---------------------
#
# Each path that retires or rewrites groups recomputes the extent in the same
# transaction, so it equals the data afterwards: not narrower (an under-estimate),
# and not left at the old width.
q "CREATE TABLE ex (ts timestamptz, v int) USING pgcolumnar;
   SELECT pgcolumnar.set_options('ex', stripe_row_limit => 1000, chunk_group_row_limit => 1000,
                                 ttl_column => 'ts', ttl_interval => '30 days');
   INSERT INTO ex SELECT now() - interval '400 days' + g * interval '1 min', g FROM generate_series(1, 3000) g;
   INSERT INTO ex SELECT now() - interval '1 day' + g * interval '1 s', g FROM generate_series(1, 3000) g;" >/dev/null
ex_before="$(ext ex ts)"
# The extent was there before expire (#1339): the arms below say it MOVED, which
# means nothing if it was never written.
check "before expire the extent covers the table" "$ex_before" "$(truth ex ts)"
retired="$(q "SELECT pgcolumnar.expire('ex');")"
check "expire retires the old row groups with the extent in place" "$([ "${retired:-0}" -ge 1 ] && echo yes || echo no)" "yes"
check "after expire the extent is recomputed to the data that is left" "$(ext ex ts)" "$(truth ex ts)"
check "and it did move: the retired groups held the old minimum" \
	"$([ "$ex_before" != "$(ext ex ts)" ] && echo yes || echo no)" "yes"

q "CREATE TABLE cr (ts timestamptz, v int) USING pgcolumnar;
   SELECT pgcolumnar.set_options('cr', stripe_row_limit => 1000, chunk_group_row_limit => 1000);
   INSERT INTO cr SELECT '2026-01-01'::timestamptz + g * interval '1 min', g FROM generate_series(1, 4000) g;
   DELETE FROM cr WHERE v > 3500 OR v <= 400;" >/dev/null
rewritten="$(q "SELECT pgcolumnar.compact_rewrite('cr', 0.1);")"
check "compact_rewrite rewrites the groups the deletes thinned" "$([ "${rewritten:-0}" -ge 1 ] && echo yes || echo no)" "yes"
check "after compact_rewrite the extent is the live rows'" "$(ext cr ts)" "$(truth cr ts)"

q "CREATE TABLE vr (ts timestamptz, v int) USING pgcolumnar;
   SELECT pgcolumnar.set_options('vr', stripe_row_limit => 1000, chunk_group_row_limit => 1000);
   INSERT INTO vr SELECT '2026-01-01'::timestamptz + g * interval '1 min', g FROM generate_series(1, 3000) g;
   DELETE FROM vr WHERE v > 2000;" >/dev/null
q "VACUUM vr;" >/dev/null
check "after VACUUM retires a fully deleted group the extent is the live rows'" "$(ext vr ts)" "$(truth vr ts)"

# recluster rewrites every group too, and the changelog names it with the three
# above (#1341 item 75): the rows a DELETE removed are not written back, so its
# extent must be the live rows', not wider by rows no longer stored. The deleted
# rows hold the minimum, so a stale extent keeps it.
q "CREATE TABLE rc (ts timestamptz, v int) USING pgcolumnar;
   SELECT pgcolumnar.set_options('rc', stripe_row_limit => 1000, chunk_group_row_limit => 1000);
   INSERT INTO rc SELECT '2026-01-01'::timestamptz + g * interval '1 min', (g * 7919) % 3001 FROM generate_series(1, 3000) g;
   DELETE FROM rc WHERE ts < '2026-01-01'::timestamptz + interval '400 min';" >/dev/null
rc_before="$(ext rc ts)"
reclustered="$(q "SELECT pgcolumnar.recluster('rc', 'ts', 'v');")"
check "recluster rewrites the table's row groups" "$([ "${reclustered:-0}" -ge 1 ] && echo yes || echo "no ($reclustered)")" "yes"
check "after recluster the extent is the live rows'" "$(ext rc ts)" "$(truth rc ts)"
check "and it did move: the deleted rows held the old minimum" \
	"$([ -n "$rc_before" ] && [ "$rc_before" != "$(ext rc ts)" ] && echo yes || echo "no ($rc_before)")" "yes"

# ---- no complete extent where the zone maps cannot give one ----------------------
q "CREATE TABLE ac (ts timestamptz) USING pgcolumnar;
   SELECT pgcolumnar.set_options('ac', stripe_row_limit => 1000, chunk_group_row_limit => 1000);
   INSERT INTO ac SELECT '2026-01-01'::timestamptz + g * interval '1 s' FROM generate_series(1, 2000) g;
   ALTER TABLE ac ADD COLUMN v int DEFAULT 5;
   INSERT INTO ac SELECT '2026-02-01'::timestamptz + g * interval '1 s', g FROM generate_series(1, 2000) g;" >/dev/null
check "a column added after older groups has no complete extent (those groups have no zone map for it)" \
	"$(ext ac v)" "none"
check "while the column that was there from the start still has one" "$(ext ac ts)" "$(truth ac ts)"
# Even after a recompute: a type with no ordering has values its zone maps cannot
# bound, and a recompute must refuse rather than claim the column holds none. A
# fully deleted group, then VACUUM, makes one run.
q "CREATE TABLE no (p point, k int) USING pgcolumnar;
   SELECT pgcolumnar.set_options('no', stripe_row_limit => 1000, chunk_group_row_limit => 1000);
   INSERT INTO no SELECT point(g, g), g FROM generate_series(1, 3000) g;
   DELETE FROM no WHERE k > 2000;" >/dev/null
q "VACUUM no;" >/dev/null
check "a type with no ordering gets no extent" "$(ext no p)" "none"

# ---- housekeeping -----------------------------------------------------------------
q "CREATE TABLE hk (ts timestamptz, v int) USING pgcolumnar;" >/dev/null
for k in $(seq 1 50); do q "INSERT INTO hk VALUES ('2026-01-01'::timestamptz + interval '$k min', $k);" >/dev/null; done
hk_rows="$(q "SELECT count(*) FROM pgcolumnar.column_extent WHERE storage_id = $(sid hk) AND column_index = 0;")"
check "fifty committing writers leave a bounded number of extent rows" \
	"$([ "${hk_rows:-0}" -ge 1 ] && [ "${hk_rows:-99}" -le 9 ] && echo yes || echo "no (${hk_rows:-none})")" "yes"
check "and the extent still covers all fifty" "$(ext hk ts)" "$(truth hk ts)"
hk_old="$(sid hk)"
q "TRUNCATE hk;" >/dev/null
check "TRUNCATE removes the old storage's extent rows" \
	"$(q "SELECT count(*) FROM pgcolumnar.column_extent WHERE storage_id = $hk_old;")" "0"
cw_old="$(sid cw)"
# Rows to remove (#1339): a count of zero after the DROP proves nothing without it.
check "before DROP the table has extent rows to remove" \
	"$(q "SELECT (count(*) > 0)::text FROM pgcolumnar.column_extent WHERE storage_id = $cw_old;")" "true"
q "DROP TABLE cw;" >/dev/null
check "DROP TABLE removes them" "$(q "SELECT count(*) FROM pgcolumnar.column_extent WHERE storage_id = $cw_old;")" "0"

q "CREATE ROLE ce_r; GRANT USAGE ON SCHEMA pgcolumnar TO ce_r;" >/dev/null
as_role() { env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At \
	-v VERBOSITY=sqlstate -c "SET ROLE $1" -c "$2" 2>&1 | grep -v '^SET$' | tail -1; }
check "the extent catalog is not readable by an ordinary role (it holds column values)" \
	"$(as_role ce_r "SELECT count(*) FROM pgcolumnar.column_extent" | sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\).*/\1/p')" "42501"
check "nor is a table's extent through the function, without SELECT on the table" \
	"$(as_role ce_r "SELECT * FROM pgcolumnar.column_extent('public.ty', 'ts')" | sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\).*/\1/p')" "42501"
# The control (#1339): the same role, given SELECT, reads it. Without this, a function
# refused to everyone -- by EXECUTE, or by the schema -- passed the arm above.
q "GRANT SELECT ON ty TO ce_r;" >/dev/null
check "and with SELECT on the table the same role reads its extent" \
	"$(as_role ce_r "SELECT count(*) FROM pgcolumnar.column_extent('public.ty', 'ts')")" "1"

pgc_summary
