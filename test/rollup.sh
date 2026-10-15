#!/usr/bin/env bash
#
# Incremental rollups, phase 1: append-only (#1294).
#
# WRITTEN BEFORE THE CODE, against the API proposed on #1294:
#
#     pgcolumnar.create_rollup(rollup, source, ts_column, width, origin, group_by name[],
#                              agg_functions text[], agg_columns name[], access_method)
#     pgcolumnar.refresh_rollup(rollup regclass) -> rows folded
#     pgcolumnar.rebase_rollup(rollup regclass) -> row groups recorded
#
# The rollup is a table, one row per (bucket, group_by...) key, with columns
# `bucket`, the group_by columns, then one per aggregate named `count`, or
# <function>_<column> (sum_v, min_v, max_v, avg_f).
#
# THE ORACLE is a full recompute of the same GROUP BY over the source, every
# time. Exactness is fixed by the design on #1294 so it cannot be loosened here:
# count, integer sum, min and max exact; avg of float8 within a relative 1e-12.
#
# A refresh is one statement and so one snapshot: rows committed while it runs
# are neither folded nor recorded, and the next refresh takes them, exactly once
# (pgcolumnar.rollup_pause, a developer setting, holds it open for that arm).
# Phase 1 refuses any change but appends, until phase 2's dirty buckets.
#
# Usage:  test/rollup.sh [PG_CONFIG]

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

at_least() { [ -z "$1" ] && return; [ "$1" -lt "$2" ] && echo "$1" || echo "$2"; }
sqlstate() {	# sqlstate SQL -> the SQLSTATE it fails with, or ok
	local out
	out=$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-X -At -v ON_ERROR_STOP=1 -v VERBOSITY=sqlstate -c "$1" 2>&1 >/dev/null)
	[ -z "$out" ] && { echo ok; return; }
	echo "$out" | sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\).*/\1/p' | head -1
}

sqlstate_or() {	# sqlstate_or SQL -> the single value it returns, or the SQLSTATE it fails with
	local out
	out=$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-X -At -v ON_ERROR_STOP=1 -v VERBOSITY=sqlstate -c "$1" 2>&1)
	case "$out" in ERROR:*) sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\).*/\1/p' <<<"$out" | head -1;; *) echo "$out";; esac
}
q "CREATE EXTENSION IF NOT EXISTS pgcolumnar;" >/dev/null
check_num "premise: the rollup functions exist" \
	"$(q "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
	      WHERE n.nspname = 'pgcolumnar' AND p.proname IN ('create_rollup', 'refresh_rollup', 'rebase_rollup');")" "3"

ORIGIN="2026-01-01 00:00:00+00"
q "CREATE TABLE src (ts timestamptz, dev int, v bigint, f float8) USING pgcolumnar;
   SELECT pgcolumnar.set_options('src', stripe_row_limit => 4000);" >/dev/null
load() {	# load FROM TO START-TS: rows g in [FROM, TO], 37 s apart from START-TS
	# bigint: g * 104729 overflows int past g = 20,505, the INSERT then fails, and
	# every arm below would measure an empty table (the first red run did).
	q "INSERT INTO src SELECT '$3'::timestamptz + (g - $1) * interval '37 s', g % 7, (g * 7919) % 100003,
	   ((g * 104729) % 9973) / 13.0 FROM generate_series($1::bigint, $2) g;" >/dev/null
}
load 1 30000 "2026-03-01 00:00:00+00"
check_num "premise: the source holds every row" "$(q "SELECT count(*) FROM src;")" "30000"

# The oracle, and the rollup read the same way: exact columns hashed, avg kept apart.
RECOMPUTE="SELECT date_bin('1 hour', ts, '$ORIGIN') AS bucket, dev, count(*) AS count,
           sum(v)::numeric AS sum_v, min(v) AS min_v, max(v) AS max_v, avg(f) AS avg_f
           FROM src GROUP BY 1, 2"
# concat_ws over coalesce: a plain || makes a row with any NULL into NULL, and
# string_agg then drops it, so NULL keys would be compared by not being there.
exact() { q "SELECT count(*) || '|' || md5(string_agg(concat_ws(',', coalesce(bucket::text, '~'),
             coalesce(dev::text, '~'), count, coalesce(sum_v::text, '~'), coalesce(min_v::text, '~'),
             coalesce(max_v::text, '~')), ';' ORDER BY bucket NULLS FIRST, dev NULLS FIRST)) FROM ($1) x;"; }
# the worst relative difference of avg_f between the rollup and the recompute
avg_gap() {
	q "SELECT coalesce(max(abs(r.avg_f - o.avg_f) / greatest(abs(o.avg_f), 1e-300)), 0) < 1e-12
	   AND count(*) = (SELECT count(*) FROM ($RECOMPUTE) z)
	   FROM r_hourly r JOIN ($RECOMPUTE) o USING (bucket, dev);"
}
matches() { echo "$(exact "SELECT * FROM r_hourly")|$(avg_gap)"; }
oracle() { echo "$(exact "$RECOMPUTE")|t"; }

AGGS="'{count,sum,min,max,avg}', '{*,v,v,v,f}'"
q "SELECT pgcolumnar.create_rollup('r_hourly', 'src', 'ts', '1 hour', '$ORIGIN', '{dev}', $AGGS);" >/dev/null
check "the rollup table exists with the documented columns" \
	"$(q "SELECT string_agg(attname, ',' ORDER BY attnum) FROM pg_attribute
	      WHERE attrelid = to_regclass('r_hourly') AND attnum > 0 AND NOT attisdropped;")" \
	"bucket,dev,count,sum_v,min_v,max_v,avg_f"

check_num "the first refresh folds every row" "$(q "SELECT pgcolumnar.refresh_rollup('r_hourly');")" "30000"
check "and after the first refresh the rollup equals a full recompute" "$(matches)" "$(oracle)"
check_num "a refresh with nothing new folds nothing" "$(q "SELECT pgcolumnar.refresh_rollup('r_hourly');")" "0"
check "and leaves the rollup as it was" "$(matches)" "$(oracle)"

# Appends, including late data: rows whose timestamps land in buckets the rollup
# already has. A time watermark would never see them.
load 30001 33000 "2026-03-13 08:00:00+00"
load 33001 34500 "2026-03-02 05:00:00+00"
check_num "a refresh after appends folds exactly the appended rows, late ones included" \
	"$(q "SELECT pgcolumnar.refresh_rollup('r_hourly');")" "4500"
check "and the rollup still equals a full recompute" "$(matches)" "$(oracle)"

# Rows committed while a refresh runs: not in its snapshot, so the NEXT refresh
# folds them, once.
load 34501 35000 "2026-03-14 00:00:00+00"
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At \
	-c "SET pgcolumnar.rollup_pause = on" -c "SELECT 'folded=' || pgcolumnar.refresh_rollup('r_hourly')" \
	> "$PGC_WORKDIR/paused.out" 2>&1 &
refresher=$!
sleep 1
load 35001 35300 "2026-03-14 06:00:00+00"
wait "$refresher"
check "the paused refresh folded what was there before it began" \
	"$(sed -n 's/^folded=//p' "$PGC_WORKDIR/paused.out")" "500"
check_num "the next refresh folds the rows committed during the last one, once" \
	"$(q "SELECT pgcolumnar.refresh_rollup('r_hourly');")" "300"
check "and after the refresh that ran beside an insert the rollup equals a full recompute" "$(matches)" "$(oracle)"

# Two refreshes at once: the second waits for the first, then has nothing to fold.
load 35301 37300 "2026-03-15 00:00:00+00"
for i in 1 2; do
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At \
		-c "SELECT pgcolumnar.refresh_rollup('r_hourly')" > "$PGC_WORKDIR/both_$i.out" 2>&1 &
done
wait
check "two refreshes started together fold the new rows once between them" \
	"$(cat "$PGC_WORKDIR/both_1.out" "$PGC_WORKDIR/both_2.out" | sort -n | tr '\n' ' ')" "0 2000 "
check "and after two refreshes started together the rollup equals a full recompute" "$(matches)" "$(oracle)"

# The bucket is a pure function of ts: refreshes from different zones agree.
for z in UTC Asia/Kolkata; do
	r="r_$(tr '/' '_' <<<"$z" | tr 'A-Z' 'a-z')"
	q "SET TimeZone = '$z'; SELECT pgcolumnar.create_rollup('$r', 'src', 'ts', '1 hour', '$ORIGIN', '{dev}', $AGGS);
	   SELECT pgcolumnar.refresh_rollup('$r');" >/dev/null
done
check "refreshes from sessions in different time zones build the same rollup" \
	"$(exact "SELECT * FROM r_utc")" "$(exact "SELECT * FROM r_asia_kolkata")"
# Empty equals empty: in the first red run both were, and the arm above passed.
check_num "the zone rollups hold a row per key of the recompute" \
	"$(q "SELECT count(*) FROM r_utc;")" "$(q "SELECT count(*) FROM ($RECOMPUTE) z;")"
check_num "and that is not zero" "$(at_least "$(q "SELECT count(*) FROM r_utc;")" 1)" "1"

# Cost: after a small append to the whole table, a refresh reads the new groups.
# The source is grown first: on a 26-page table the refresh's fixed cost (the
# group list, the storage id) is itself more than a fifth (measured: 15 blocks).
load 100001 300000 "2026-04-01 00:00:00+00"
check_num "a refresh folds a bulk append" "$(q "SELECT pgcolumnar.refresh_rollup('r_hourly');")" "200000"
check "and after the bulk append the rollup equals a full recompute" "$(matches)" "$(oracle)"
load 37301 38300 "2026-03-16 00:00:00+00"
q "SELECT pg_stat_reset();" >/dev/null
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At \
	-c "SELECT pgcolumnar.refresh_rollup('r_hourly')" -c "SELECT pg_stat_force_next_flush()" >/dev/null 2>&1
read_blocks="$(q "SELECT coalesce(heap_blks_read, 0) + coalesce(heap_blks_hit, 0) FROM pg_statio_user_tables WHERE relname = 'src';")"
pages="$(q "SELECT pg_relation_size('src') / 8192;")"
echo "-- refresh after a 1000-row append read $read_blocks of $pages blocks"
check_num "the refresh after a small append read the source at all" "$(at_least "$read_blocks" 1)" "1"
# Reading nothing also reads under a fifth: the arm requires a read.
check_num "a refresh after a small append reads under a fifth of the source" \
	"$( [ -n "$read_blocks" ] && [ "$read_blocks" -ge 1 ] && at_least $((pages - 5 * read_blocks)) 1 || echo "read ${read_blocks:-nothing}")" "1"
check "and after the small append the rollup equals a full recompute" "$(matches)" "$(oracle)"

# A rewrite renumbers every row group (#1294 point 9): the snapshot no longer names
# the source's groups, so a refresh is refused, and rebase_rollup records the
# current groups without touching the rollup.
# The identity is the storage id and the group numbers: measured, pgcolumnar.vacuum
# gives a table a new storage id and may keep its group numbers.
groups_before="$(q "SELECT pgcolumnar.get_storage_id('src') || ':' || string_agg(group_number::text, ',' ORDER BY group_number)
	FROM pgcolumnar.row_group WHERE storage_id = pgcolumnar.get_storage_id('src');")"
q "SELECT pgcolumnar.vacuum('src');" >/dev/null
groups_after="$(q "SELECT pgcolumnar.get_storage_id('src') || ':' || string_agg(group_number::text, ',' ORDER BY group_number)
	FROM pgcolumnar.row_group WHERE storage_id = pgcolumnar.get_storage_id('src');")"
check "pgcolumnar.vacuum gave the source's row groups new identities" \
	"$([ -n "$groups_after" ] && [ "$groups_after" != "$groups_before" ] && echo renumbered || echo "same: $groups_after")" "renumbered"
vb="$(exact "SELECT * FROM r_hourly")"
check_num "the rollup holds rows before the rewrite" "$(at_least "$(q "SELECT count(*) FROM r_hourly;")" 1)" "1"
check "a refresh after the source was rewritten is refused, naming rebase_rollup" \
	"$(sqlstate "SELECT pgcolumnar.refresh_rollup('r_hourly');")|$(exact "SELECT * FROM r_hourly")" "55000|$vb"
check_num "rebase_rollup records the source's current row groups" \
	"$(at_least "$(q "SELECT pgcolumnar.rebase_rollup('r_hourly');")" 1)" "1"
check "and leaves the rollup's rows alone" "$(exact "SELECT * FROM r_hourly")" "$vb"
load 38301 39100 "2026-03-17 00:00:00+00"
check_num "after a rebase the next refresh folds exactly the appended rows" \
	"$(q "SELECT pgcolumnar.refresh_rollup('r_hourly');")" "800"
check "and after the rebase the rollup equals a full recompute" "$(matches)" "$(oracle)"

# Phase 2 (#1294): a delete, an update or a rewrite in the source makes the
# buckets of the row groups it changed DIRTY. A refresh recomputes those from the
# source, folds new groups into the rest, and leaves every other partial alone.
# The groups were last recorded by rebase_rollup above, so these arms also need
# rebase to have recorded each group's time range.
# where TEXT -> the partials matching it, by physical position: a bucket that is
# recomputed or updated gets a new row, so a new ctid.
partials_at() { q "SELECT count(*) || '|' || md5(string_agg(ctid::text, ',' ORDER BY ctid)) FROM r_hourly__partials WHERE $1;"; }
FAR="bucket >= '2026-03-05 00:00:00+00'"	# beyond the time range of every group the delete below reaches
far_before="$(partials_at "$FAR")"
check_num "the rollup holds partials the delete below cannot reach" \
	"$(at_least "$(q "SELECT count(*) FROM r_hourly__partials WHERE $FAR;")" 1)" "1"
q "DELETE FROM src WHERE dev = 3 AND ts < '2026-03-01 06:00:00+00';" >/dev/null
check "a refresh after a delete is not refused" \
	"$(sqlstate_or "SELECT pgcolumnar.refresh_rollup('r_hourly');" | sed 's/^[0-9]*$/folded/')" "folded"
check "and after a delete the rollup equals a full recompute" "$(matches)" "$(oracle)"
check "and the refresh rewrote no partial outside the changed groups' time range" "$(partials_at "$FAR")" "$far_before"
check_num "and the next refresh, with nothing changed since, recomputes nothing" \
	"$(q "SELECT pgcolumnar.refresh_rollup('r_hourly');")" "0"

# An update moves rows to a bucket five days on: the old bucket loses them (its
# group is dirty), the new one gains them (an appended group).
q "UPDATE src SET ts = ts + interval '5 days' WHERE ts >= '2026-03-03 10:00:00+00' AND ts < '2026-03-03 10:20:00+00';" >/dev/null
q "SELECT pgcolumnar.refresh_rollup('r_hourly');" >/dev/null
check "after an update that moves rows to another bucket the rollup equals a full recompute" "$(matches)" "$(oracle)"

# A dirty bucket and a new group in the same refresh: the new group's rows in the
# dirty bucket are counted by the recompute, so the fold must skip them.
q "DELETE FROM src WHERE dev = 5 AND ts >= '2026-03-04 02:00:00+00' AND ts < '2026-03-04 03:00:00+00';" >/dev/null
# 200 rows every 30 minutes from 02:30: 100 hours, past any 4000-row group's 41.
q "INSERT INTO src SELECT '2026-03-04 02:30:00+00'::timestamptz + g * interval '30 min', 5, g, g / 3.0
   FROM generate_series(0, 199) g;" >/dev/null
q "SELECT pgcolumnar.refresh_rollup('r_hourly');" >/dev/null
check "after a delete and an append that straddles its bucket, a refresh counts the new rows once" "$(matches)" "$(oracle)"

# compact_rewrite drops a group's dead rows by rewriting the group.
q "DELETE FROM src WHERE ts >= '2026-03-06 00:00:00+00' AND ts < '2026-03-06 12:00:00+00';" >/dev/null
check_num "compact_rewrite rewrote a row group" \
	"$(at_least "$(q "SELECT pgcolumnar.compact_rewrite('src', 0.01);")" 1)" "1"
q "SELECT pgcolumnar.refresh_rollup('r_hourly');" >/dev/null
check "after compact_rewrite the rollup equals a full recompute" "$(matches)" "$(oracle)"
check_num "and the refresh after the rewrite recomputes nothing" \
	"$(q "SELECT pgcolumnar.refresh_rollup('r_hourly');")" "0"

# Refused at creation.
# Each new group is read through a TID range built from its row numbers. The
# mapping is the access method's (row_tid, in C), not restated in SQL: 291 rows
# per block holds only at the default block size (#1319 review). Every group's
# range must hold exactly its rows.
check "each row group's TID range holds exactly that group's rows" \
	"$(q "SELECT count(*) FILTER (WHERE got <> want) || '|' || count(*) FROM (
	        SELECT g.row_count - g.deleted_count AS want,
	               (SELECT count(*) FROM src WHERE ctid >= pgcolumnar.row_tid(g.first_row_number)
	                  AND ctid <= pgcolumnar.row_tid(g.first_row_number + g.row_count - 1)) AS got
	          FROM pgcolumnar.rollup_source_groups('src') g) x;")" \
	"0|$(q "SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = pgcolumnar.get_storage_id('src');")"

# NULL group keys and NULL timestamps: the fold matches keys with IS NOT DISTINCT
# FROM, and a NULL ts is a NULL bucket. Measured right by @jdatcmd on #1319; an
# = in place of IS NOT DISTINCT FROM would duplicate the NULL partials.
q "CREATE TABLE nk (ts timestamptz, dev int, v bigint) USING pgcolumnar;
   SELECT pgcolumnar.set_options('nk', stripe_row_limit => 1000);
   INSERT INTO nk SELECT CASE WHEN g % 11 = 0 THEN NULL ELSE '2026-06-01'::timestamptz + g * interval '61 s' END,
          CASE WHEN g % 7 = 0 THEN NULL ELSE g % 5 END, g FROM generate_series(1, 3000) g;" >/dev/null
q "SELECT pgcolumnar.create_rollup('r_nk', 'nk', 'ts', '1 hour', '$ORIGIN', '{dev}', '{count,sum,min,max}', '{*,v,v,v}');
   SELECT pgcolumnar.refresh_rollup('r_nk');" >/dev/null
q "INSERT INTO nk SELECT CASE WHEN g % 11 = 0 THEN NULL ELSE '2026-06-01'::timestamptz + g * interval '61 s' END,
          CASE WHEN g % 7 = 0 THEN NULL ELSE g % 5 END, g FROM generate_series(3001, 4500) g;" >/dev/null
check_num "the second refresh of the NULL-key rollup folded the append" "$(q "SELECT pgcolumnar.refresh_rollup('r_nk');")" "1500"
NKR="SELECT date_bin('1 hour', ts, '$ORIGIN') AS bucket, dev, count(*) AS count, sum(v)::numeric AS sum_v,
     min(v) AS min_v, max(v) AS max_v FROM nk GROUP BY 1, 2"
check "a rollup with NULL keys and NULL timestamps equals a full recompute" \
	"$(exact "SELECT * FROM r_nk")|$(q "SELECT count(*) FROM r_nk WHERE dev IS NULL;") $(q "SELECT count(*) FROM r_nk WHERE bucket IS NULL;")" \
	"$(exact "$NKR")|$(q "SELECT count(*) FROM ($NKR) z WHERE dev IS NULL;") $(q "SELECT count(*) FROM ($NKR) z WHERE bucket IS NULL;")"
check_num "premise: and the fixture has NULL-key and NULL-bucket groups" \
	"$(at_least "$(q "SELECT count(*) FROM ($NKR) z WHERE dev IS NULL OR bucket IS NULL;")" 2)" "2"
# A deleted row whose ts is NULL dirties the NULL bucket, which has no time range.
q "DELETE FROM nk WHERE ts IS NULL AND dev = 2;" >/dev/null
q "SELECT pgcolumnar.refresh_rollup('r_nk');" >/dev/null
check "after a delete of rows whose ts is NULL the NULL-bucket rollup equals a full recompute" \
	"$(exact "SELECT * FROM r_nk")" "$(exact "$NKR")"

# Retention keeps history (owner's decision on #1294 point 2): groups that expire
# retires are recorded in pgcolumnar.retired_group (#1314), and a refresh drops
# them from its snapshot without touching their buckets. Any other missing group
# is still refused (the delete arm above).
q "CREATE TABLE hist (ts timestamptz, v int) USING pgcolumnar;
   SELECT pgcolumnar.set_options('hist', stripe_row_limit => 1000);
   INSERT INTO hist SELECT now() - interval '200 days' + g * interval '1 min', g FROM generate_series(1, 3000) g;
   INSERT INTO hist SELECT now() - interval '1 day' + g * interval '10 s', g FROM generate_series(1, 2000) g;" >/dev/null
q "SELECT pgcolumnar.create_rollup('r_hist', 'hist', 'ts', '1 day', '$ORIGIN', '{}', '{count,sum}', '{*,v}');" >/dev/null
check_num "the history rollup folded every row" "$(q "SELECT pgcolumnar.refresh_rollup('r_hist');")" "5000"
hb="$(q "SELECT md5(string_agg(bucket || ':' || count || ':' || sum_v, ',' ORDER BY bucket)) FROM r_hist;")"
ob="$(q "SELECT count(*) FROM r_hist WHERE bucket < now() - interval '100 days';")"
check_num "the history rollup holds old buckets" "$(at_least "${ob:-0}" 1)" "1"
q "SELECT pgcolumnar.set_options('hist', ttl_column => 'ts', ttl_interval => '30 days');" >/dev/null
retired="$(q "SELECT pgcolumnar.expire('hist');")"
check_num "expire retired the old row groups" "$(at_least "${retired:-0}" 1)" "1"
check "a refresh after retention folds nothing, refuses nothing, and keeps every bucket" \
	"$(sqlstate_or "SELECT pgcolumnar.refresh_rollup('r_hist');")|$(q "SELECT md5(string_agg(bucket || ':' || count || ':' || sum_v, ',' ORDER BY bucket)) FROM r_hist;")" \
	"0|$hb"
check_num "and the rollup no longer records the groups retention retired" \
	"$(q "SELECT count(*) FROM pgcolumnar.rollup_group rg JOIN pgcolumnar.retired_group t
	      ON t.group_number = rg.group_number AND t.storage_id = pgcolumnar.get_storage_id('hist')
	      WHERE rg.rollup = 'r_hist'::regclass;")" "0"
q "INSERT INTO hist SELECT now() - interval '2 hours' + g * interval '1 s', g FROM generate_series(1, 500) g;" >/dev/null
check_num "and after retention an append still folds" "$(q "SELECT pgcolumnar.refresh_rollup('r_hist');")" "500"
check "and the recent buckets equal a recompute, while the expired ones keep their history" \
	"$(q "SELECT md5(string_agg(bucket || ':' || count || ':' || sum_v, ',' ORDER BY bucket)) FROM r_hist WHERE bucket >= now() - interval '30 days';")|$(q "SELECT count(*) FROM r_hist WHERE bucket < now() - interval '100 days';")" \
	"$(q "SELECT md5(string_agg(b || ':' || n || ':' || s, ',' ORDER BY b)) FROM (SELECT date_bin('1 day', ts, '$ORIGIN') b, count(*) n, sum(v)::numeric s FROM hist WHERE ts >= now() - interval '30 days' GROUP BY 1) x WHERE b >= now() - interval '30 days';")|$ob"

# History and dirty buckets. A bucket that also holds a retired group's rows
# cannot be recomputed: the source no longer has them. Such a refresh is refused
# and changes nothing; a delete whose buckets hold no history folds. Three groups,
# one per INSERT: OLD retires; KEPT shares OLD's bucket D and reaches past the
# cutoff, so expire keeps it; LATE starts days after D.
D="date_bin('1 day', now() - interval '40 days', '$ORIGIN')"
layered() {	# layered TABLE: the three groups, a daily rollup r_TABLE refreshed once, then expire
	q "CREATE TABLE $1 (ts timestamptz, v int) USING pgcolumnar;
	   SELECT pgcolumnar.set_options('$1', stripe_row_limit => 2000);
	   INSERT INTO $1 SELECT $D + interval '2 hours' + g * interval '1 min', g FROM generate_series(1, 500) g;
	   INSERT INTO $1 SELECT $D + interval '12 hours' + g * interval '15 min', g FROM generate_series(1, 1200) g;
	   INSERT INTO $1 SELECT $D + interval '20 days' + g * interval '5 min', g FROM generate_series(1, 600) g;" >/dev/null
	q "SELECT pgcolumnar.create_rollup('r_$1', '$1', 'ts', '1 day', '$ORIGIN', '{}', '{count,sum}', '{*,v}');
	   SELECT pgcolumnar.refresh_rollup('r_$1');
	   SELECT pgcolumnar.set_options('$1', ttl_column => 'ts', ttl_interval => '30 days');" >/dev/null
	q "SELECT pgcolumnar.expire('$1');" >/dev/null
}
layered hd
check "expire retired the group in bucket D alone and kept the one that shares it" \
	"$(q "SELECT count(*) FILTER (WHERE ts < $D + interval '12 hours') || '|' || count(*) FILTER (WHERE ts >= $D + interval '12 hours' AND ts < $D + interval '1 day') FROM hd;")" "0|47"
HDQ="SELECT md5(string_agg(bucket || ':' || count || ':' || sum_v, ',' ORDER BY bucket)) FROM r_hd"
HDR="SELECT md5(string_agg(b || ':' || n || ':' || s, ',' ORDER BY b)) FROM (SELECT date_bin('1 day', ts, '$ORIGIN') b, count(*) n, sum(v)::numeric s FROM hd GROUP BY 1) x"
q "DELETE FROM hd WHERE ts >= $D + interval '20 days 12 hours' AND v % 2 = 0;" >/dev/null
check "a delete in buckets with no history, next to retired ones, folds: those buckets equal a recompute and D keeps its history" \
	"$(sqlstate_or "SELECT pgcolumnar.refresh_rollup('r_hd');" | sed 's/^[0-9]*$/folded/')|$(q "$HDQ WHERE bucket > $D;")|$(q "SELECT count || ':' || sum_v FROM r_hd WHERE bucket = $D;")" \
	"folded|$(q "$HDR WHERE b > $D;")|547:$((500 * 501 / 2 + 47 * 48 / 2))"
hd_before="$(q "$HDQ;")"
q "DELETE FROM hd WHERE ts >= $D + interval '12 hours' AND ts < $D + interval '13 hours';" >/dev/null
check "a delete in a bucket that holds retired history is refused, and the rollup is unchanged" \
	"$(sqlstate "SELECT pgcolumnar.refresh_rollup('r_hd');")|$(q "$HDQ;")" "0A000|$hd_before"
# The same, with no refresh between expire and the delete: the history that
# blocks the recompute is retired by the very refresh that would recompute it.
layered hd2
hd2_before="$(q "SELECT md5(string_agg(bucket || ':' || count || ':' || sum_v, ',' ORDER BY bucket)) FROM r_hd2;")"
q "DELETE FROM hd2 WHERE ts >= $D + interval '12 hours' AND ts < $D + interval '13 hours';" >/dev/null
check "a delete in a bucket whose history the same refresh retires is refused, and the rollup is unchanged" \
	"$(sqlstate "SELECT pgcolumnar.refresh_rollup('r_hd2');")|$(q "SELECT md5(string_agg(bucket || ':' || count || ':' || sum_v, ',' ORDER BY bucket)) FROM r_hd2;")" \
	"0A000|$hd2_before"

# Rows deleted from a group that expire then retires, before the next refresh: the
# rows are gone, so their buckets cannot be recomputed, and folding the group's
# range into history would keep counting them. Found by @OffgridwithJD on #1328
# (1000 counted where the source had 600). The refresh is refused instead.
q "CREATE TABLE dx (ts timestamptz, v int) USING pgcolumnar;
   SELECT pgcolumnar.set_options('dx', stripe_row_limit => 1000);
   INSERT INTO dx SELECT now() - interval '60 days' + g * interval '1 s', g FROM generate_series(1, 1000) g;
   INSERT INTO dx SELECT now() - interval '1 day' + g * interval '1 s', g FROM generate_series(1, 1000) g;
   SELECT pgcolumnar.create_rollup('r_dx', 'dx', 'ts', '1 day', '$ORIGIN', '{}', '{count}', '{*}');
   SELECT pgcolumnar.refresh_rollup('r_dx');
   DELETE FROM dx WHERE ts < now() - interval '30 days' AND v <= 400;
   SELECT pgcolumnar.set_options('dx', ttl_column => 'ts', ttl_interval => '30 days');" >/dev/null
check_num "expire retired the group the delete thinned" "$(q "SELECT pgcolumnar.expire('dx');")" "1"
dx_before="$(q "SELECT string_agg(bucket || ':' || count, ',' ORDER BY bucket) FROM r_dx;")"
check "a delete in a group that expire then retires is refused, and the rollup is unchanged" \
	"$(sqlstate "SELECT pgcolumnar.refresh_rollup('r_dx');")|$(q "SELECT string_agg(bucket || ':' || count, ',' ORDER BY bucket) FROM r_dx;")" \
	"0A000|$dx_before"

# A day bucket is 24 hours (date_bin's day), whatever the session's zone. In
# America/New_York, 2026-03-08 is 23 hours long, and the dirty bucket of that UTC
# day must still be recomputed up to its end: rows run to 23:30 UTC.
q "CREATE TABLE dst (ts timestamptz, v int) USING pgcolumnar;
   SELECT pgcolumnar.set_options('dst', stripe_row_limit => 5000);
   INSERT INTO dst SELECT '2026-03-07 00:00:00+00'::timestamptz + g * interval '5 min', g FROM generate_series(1, 570) g;
   SELECT pgcolumnar.create_rollup('r_dst', 'dst', 'ts', '1 day', '$ORIGIN', '{}', '{count,sum}', '{*,v}');
   SELECT pgcolumnar.refresh_rollup('r_dst');
   DELETE FROM dst WHERE v = 1;" >/dev/null
q "SET TimeZone = 'America/New_York'; SELECT pgcolumnar.refresh_rollup('r_dst');" >/dev/null
check "a refresh in a zone with a DST change recomputes a whole day bucket" \
	"$(q "SELECT string_agg(bucket || ':' || count || ':' || sum_v, ',' ORDER BY bucket) FROM r_dst;" | tail -1)" \
	"$(q "SELECT string_agg(b || ':' || n || ':' || s, ',' ORDER BY b) FROM (SELECT date_bin('1 day', ts, '$ORIGIN') b, count(*) n, sum(v)::numeric s FROM dst GROUP BY 1) x;")"

# Not only for superusers: a superuser bypasses row-level security, so every arm
# above ran past the policies on pgcolumnar.rollup and rollup_group. A table's
# owner can roll it up; another role sees nothing of it and cannot refresh it.
# PUBLIC has no USAGE on schema pgcolumnar (measured: 42501 for any call), so the
# roles get it as an administrator would grant it; what is then tested is the
# row-level security on the rollup catalogs.
q "CREATE ROLE ru_a; CREATE ROLE ru_b; GRANT USAGE, CREATE ON SCHEMA public TO ru_a, ru_b;
   GRANT USAGE ON SCHEMA pgcolumnar TO ru_a, ru_b;" >/dev/null
as_role() { env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At \
	-v VERBOSITY=sqlstate -c "SET ROLE $1" -c "$2" 2>&1 | grep -v '^SET$' | tail -1; }
as_role ru_a "CREATE TABLE own_src (ts timestamptz, v int) USING pgcolumnar" >/dev/null
as_role ru_a "INSERT INTO own_src SELECT '2026-05-01'::timestamptz + g * interval '1 min', g FROM generate_series(1, 5000) g" >/dev/null
check "a table's owner, not a superuser, creates a rollup of it" \
	"$(as_role ru_a "SELECT pgcolumnar.create_rollup('own_r', 'own_src', 'ts', '1 day', '$ORIGIN', '{}', '{count,sum}', '{*,v}')")" "own_r"
check_num "and refreshes it" "$(as_role ru_a "SELECT pgcolumnar.refresh_rollup('own_r')")" "5000"
check "and reads what a recompute gives" \
	"$(as_role ru_a "SELECT string_agg(bucket || ':' || count || ':' || sum_v, ' ' ORDER BY bucket) FROM own_r")" \
	"$(q "SELECT string_agg(b || ':' || n || ':' || s, ' ' ORDER BY b) FROM (SELECT date_bin('1 day', ts, '$ORIGIN') b, count(*) n, sum(v)::numeric s FROM own_src GROUP BY 1) x;")"
# THE WRITE SIDE AND THE DEFINER HELPERS (#1341). The policies have USING only, so
# they also check an INSERT's new row; and an UPDATE or DELETE with no WHERE reads
# nothing, so only the policy's USING keeps it off another owner's rows. Asked
# before the read-side arms below, so a write that got through is what fails.
_st() { sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\).*/\1/p'; }
_rg_own="$(q "SELECT count(*) FROM pgcolumnar.rollup_group WHERE rollup = 'own_r'::regclass;")"
check "premise: the owner's rollup has group rows another role could reach" \
	"$([ "${_rg_own:-0}" -gt 0 ] 2>/dev/null && echo some || echo "none ($_rg_own)")" "some"
check "another role cannot add a group row to the owner's rollup (42501)" \
	"$(as_role ru_b "INSERT INTO pgcolumnar.rollup_group VALUES ('public.own_r'::regclass, 999999, 1, 0, NULL, NULL, false)" | _st)" "42501"
as_role ru_b "UPDATE pgcolumnar.rollup SET history_has_null = true" >/dev/null
as_role ru_b "DELETE FROM pgcolumnar.rollup_group" >/dev/null
check "and its unqualified UPDATE and DELETE reach none of the owner's catalog rows" \
	"$(q "SELECT history_has_null FROM pgcolumnar.rollup WHERE rollup = 'own_r'::regclass;")|$(q "SELECT count(*) FROM pgcolumnar.rollup_group WHERE rollup = 'own_r'::regclass;")" \
	"f|$_rg_own"
# rollup_source_groups and rollup_source_retired run as their definer, so the
# catalogs they read refuse no one; the caller's SELECT on the source is checked
# by require_caller_select against the OUTER user.
check "the owner reads its source's groups through the definer helpers" \
	"$(as_role ru_a "SELECT (count(*) > 0)::text FROM pgcolumnar.rollup_source_groups('own_src')")|$(as_role ru_a "SELECT count(*) >= 0 FROM pgcolumnar.rollup_source_retired('own_src')")" "true|t"
check "another role cannot read the source's groups through rollup_source_groups (42501)" \
	"$(as_role ru_b "SELECT count(*) FROM pgcolumnar.rollup_source_groups('public.own_src')" | _st)" "42501"
check "nor its retired groups through rollup_source_retired (42501)" \
	"$(as_role ru_b "SELECT count(*) FROM pgcolumnar.rollup_source_retired('public.own_src')" | _st)" "42501"
check "another role sees none of its rollup catalog rows" \
	"$(as_role ru_b "SELECT count(*) FROM pgcolumnar.rollup") $(as_role ru_b "SELECT count(*) FROM pgcolumnar.rollup_group")" "0 0"
# Row-level security hides the catalog row, so to that role it is no rollup at
# all (42704), which also says nothing about whether one exists.
check "and cannot refresh it" "$(as_role ru_b "SELECT pgcolumnar.refresh_rollup('public.own_r')" | sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\).*/\1/p')" "42704"
# create_rollup takes the source as a relation, and it reads the source's
# storage id through pgcolumnar.get_storage_id, which refuses a caller that
# cannot SELECT the table (42501; one of entry_point_privilege.sh's guarded
# entry points). So the refusal comes at creation, not at the first refresh.
check "a role that cannot read the source cannot create a rollup of it" \
	"$(as_role ru_b "SELECT pgcolumnar.create_rollup('b_r', 'public.own_src', 'ts', '1 day', '$ORIGIN', '{}', '{count}', '{*}')" | sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\).*/\1/p')" "42501"

# access_method: the partials need not be columnar (#1294 point 12 measures which
# serves a hot rollup better). A heap rollup folds and matches the same way.
q "SELECT pgcolumnar.create_rollup('r_heap', 'src', 'ts', '1 hour', '$ORIGIN', '{dev}', $AGGS, 'heap');" >/dev/null
check "a rollup kept in a heap table refreshes and equals a full recompute" \
	"$(q "SELECT a.amname FROM pg_class c JOIN pg_am a ON a.oid = c.relam WHERE c.oid = 'r_heap__partials'::regclass;")|$(q "SELECT pgcolumnar.refresh_rollup('r_heap') > 0;")|$(exact "SELECT * FROM r_heap")" \
	"heap|t|$(exact "$RECOMPUTE")"

# REPEATABLE READ: the caller's snapshot would predate the refresh's lock, and a
# refresh that waited for another would fold the same groups again.
check "a refresh at REPEATABLE READ is refused" \
	"$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At \
	   -v VERBOSITY=sqlstate -c "BEGIN ISOLATION LEVEL REPEATABLE READ" \
	   -c "SELECT pgcolumnar.refresh_rollup('r_utc')" -c "ROLLBACK" 2>&1 |
	   sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\).*/\1/p' | head -1)" "25000"

# A DUMP AND RESTORE (#1377). The definition is dumped, so the rollup still exists
# after a restore; its folded row groups are not, because they name storage that
# a restore replaces. Neither database OID nor storage id tells a restored source
# from the one recorded: storageid_seq restarts with the extension, so the first
# columnar table of a new database gets the same storage id, and refresh would fold
# every restored row into the restored partials a second time. The source's OID does
# tell them apart: the dump writes the regclass as a name and the recorded oid as a
# number, so after a restore they disagree, and refresh refuses, naming rebase_rollup.
# Its own databases, so nothing above is touched. The partials are heap and the
# source is its database's only columnar table, so the restored source gets the
# storage id recorded for the old one: the case a storage id alone does not catch.
RD="${PGC_DB}_rd"; RD2="${PGC_DB}_rd2"; RDUMP="$PGC_WORKDIR/rollup-1377.sql"
rd_run() { env PATH="$PGC_BINDIR:$PATH" "$@"; }
rd_q() { rd_run psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$1" -X -At -c "$2" 2>/dev/null || true; }
rd_state() {	# rd_state DB SQL -> the SQLSTATE it fails with, ok, or noconnect
	local out rc
	out=$(rd_run psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$1" -X -At -v ON_ERROR_STOP=1 \
		-v VERBOSITY=sqlstate -c "SET ROLE rd_owner" -c "$2" 2>&1 >/dev/null); rc=$?
	[ $rc -eq 0 ] && { echo ok; return; }
	[ $rc -eq 2 ] && { echo noconnect; return; }
	echo "$out" | sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\).*/\1/p' | head -1
}
rd_as() { rd_run psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$1" -X -At \
	-c "SET ROLE rd_owner" -c "$2" 2>/dev/null | grep -v '^SET$' | tail -1; }
rd_hint() {	# rd_hint DB SQL -> the HINT of the error it fails with
	rd_run psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$1" -X -At -v VERBOSITY=verbose \
		-c "SET ROLE rd_owner" -c "$2" 2>&1 | sed -n 's/^HINT: *//p' | head -1
}
# count|md5, so a mismatch prints two short values rather than every bucket.
RD_RECOMPUTE="SELECT count(*) || '|' || md5(string_agg(b || ':' || d || ':' || n || ':' || s, ' ' ORDER BY b, d)) FROM
	(SELECT date_bin('1 hour', ts, '$ORIGIN') b, dev d, count(*) n, sum(v)::numeric s FROM rd.src GROUP BY 1, 2) x"
RD_ROLLUP="SELECT count(*) || '|' || md5(string_agg(bucket || ':' || dev || ':' || count || ':' || sum_v, ' ' ORDER BY bucket, dev)) FROM rd.r"
q "CREATE ROLE rd_owner;" >/dev/null
for db in "$RD" "$RD2"; do q "DROP DATABASE IF EXISTS $db;" >/dev/null; done
q "CREATE DATABASE $RD;" >/dev/null
rd_q "$RD" "CREATE EXTENSION pgcolumnar; GRANT USAGE ON SCHEMA pgcolumnar TO rd_owner;
	CREATE SCHEMA rd AUTHORIZATION rd_owner;" >/dev/null
rd_as "$RD" "CREATE TABLE rd.src (ts timestamptz, dev int, v bigint) USING pgcolumnar" >/dev/null
rd_as "$RD" "INSERT INTO rd.src SELECT '2026-06-01'::timestamptz + g * interval '1 min', g % 3, g FROM generate_series(1, 3000) g" >/dev/null
rd_as "$RD" "SELECT pgcolumnar.create_rollup('r', 'rd.src', 'ts', '1 hour', '$ORIGIN', '{dev}', '{count,sum}', '{*,v}', 'heap')" >/dev/null
check_num "the rollup to be dumped folds its source" "$(rd_as "$RD" "SELECT pgcolumnar.refresh_rollup('rd.r')")" "3000"
rd_sid="$(rd_q "$RD" "SELECT pgcolumnar.get_storage_id('rd.src')")"
rd_run pg_dump -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$RD" -f "$RDUMP"; rd_rc=$?
check "the database holding a rollup dumps" "$rd_rc" "0"

# Restored into another database.
q "CREATE DATABASE $RD2;" >/dev/null
rd_restore() {	# rd_restore DB -> 0, or psql's status and its first error
	local out
	out=$(rd_run psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$1" -X -q -v ON_ERROR_STOP=1 -f "$RDUMP" 2>&1 >/dev/null) \
		&& { echo 0; return; }
	echo "$? $(grep -m1 'ERROR' <<<"$out")"
}
check "the dump restores into another database" "$(rd_restore "$RD2")" "0"
check "the restored source holds the dumped rows under the storage id recorded for the old one" \
	"$(rd_q "$RD2" "SELECT count(*) FROM rd.src")|$(rd_q "$RD2" "SELECT pgcolumnar.get_storage_id('rd.src')")" \
	"3000|$rd_sid"
check "a restore keeps the rollup's definition" \
	"$(rd_q "$RD2" "SELECT source::text || ' ' || ts_column || ' ' || width || ' ' || group_by::text || ' ' || agg_functions::text
	   FROM pgcolumnar.rollup WHERE rollup = 'rd.r'::regclass")" "rd.src ts 01:00:00 {dev} {count,sum}"
rd_restored="$(rd_q "$RD2" "$RD_ROLLUP")"
check "the restored rollup reads as a recompute of the restored source" "$rd_restored" "$(rd_q "$RD2" "$RD_RECOMPUTE")"
check "after a restore into another database its owner's refresh is refused (55000), the rollup untouched" \
	"$(rd_state "$RD2" "SELECT pgcolumnar.refresh_rollup('rd.r')")|$(rd_q "$RD2" "$RD_ROLLUP")" "55000|$rd_restored"
check "and the refusal names rebase_rollup" \
	"$(rd_hint "$RD2" "SELECT pgcolumnar.refresh_rollup('rd.r')" | grep -o 'rebase_rollup' | head -1)" "rebase_rollup"
check_num "after rebase_rollup a refresh folds nothing" \
	"$(rd_as "$RD2" "SELECT pgcolumnar.rebase_rollup('rd.r')" >/dev/null; rd_as "$RD2" "SELECT pgcolumnar.refresh_rollup('rd.r')")" "0"
rd_as "$RD2" "INSERT INTO rd.src SELECT '2026-06-01'::timestamptz + g * interval '1 min', g % 3, g FROM generate_series(3001, 3500) g" >/dev/null
check_num "and then folds exactly the rows appended after the restore" "$(rd_as "$RD2" "SELECT pgcolumnar.refresh_rollup('rd.r')")" "500"
check "and the restored rollup equals a full recompute" "$(rd_q "$RD2" "$RD_ROLLUP")" "$(rd_q "$RD2" "$RD_RECOMPUTE")"
# The restore wrote the catalog row back, and with it the view's dependency on the
# restored source (#1376): the source cannot be dropped from under its rollup.
check "after a restore the rollup's source cannot be dropped while the rollup exists (2BP01)" \
	"$(rd_state "$RD2" "DROP TABLE rd.src")|$(rd_q "$RD2" "SELECT count(*) FROM rd.src")" "2BP01|3500"

# Restored into the same database after DROP EXTENSION: same cluster, same database
# OID, and the sequence restarted, so again the recorded storage id.
# The pgcolumnar schema outlives DROP EXTENSION (not a member: alter_am_cleanup.sh),
# and the dump creates it, so it goes too, as it would before such a restore.
rd_q "$RD" "DROP EXTENSION pgcolumnar CASCADE; DROP SCHEMA pgcolumnar CASCADE; DROP SCHEMA rd CASCADE;" >/dev/null
check "the dump restores into its own database after DROP EXTENSION, again under the recorded storage id" \
	"$(rd_restore "$RD")|$(rd_q "$RD" "SELECT count(*) FROM rd.src")|$(rd_q "$RD" "SELECT pgcolumnar.get_storage_id('rd.src')")" \
	"0|3000|$rd_sid"
rd_restored="$(rd_q "$RD" "$RD_ROLLUP")"
check "after a restore into the same database its refresh is refused (55000), the rollup untouched" \
	"$(rd_state "$RD" "SELECT pgcolumnar.refresh_rollup('rd.r')")|$(rd_q "$RD" "$RD_ROLLUP")" "55000|$rd_restored"
for db in "$RD" "$RD2"; do q "DROP DATABASE IF EXISTS $db;" >/dev/null; done

# Refused at creation, each with 22023: the aggregates are structured input
# checked against the five that combine, never SQL text (#1294 point 11).
refused_create() { sqlstate "SELECT pgcolumnar.create_rollup('$1', 'src', 'ts', '$2', '$ORIGIN', '{}', '$3', '$4');"; }
check "a width with months is refused" "$(refused_create r_m '1 month' '{count}' '{*}')" "22023"
check "an aggregate that does not combine is refused" "$(refused_create r_p '1 hour' '{median}' '{v}')" "22023"
# More columns than functions: a shorter columns array would be caught by the
# column check instead, and the arm would not see this check go.
check "aggregate functions and columns of different lengths are refused" \
	"$(refused_create r_l '1 hour' '{count}' '{*,v}')" "22023"
# Under min: under sum the "cannot be folded" check would catch it first.
check "an aggregate column the source does not have is refused" \
	"$(refused_create r_c '1 hour' '{min}' '{nosuch}')" "22023"
check "* with anything but count is refused" "$(refused_create r_s '1 hour' '{sum}' '{*}')" "22023"
# The rest of create_rollup's refusals, and rebase_rollup's (#1341). A timestamp
# (no zone) source column would be bucketed by date_bin in whatever zone the
# refresh runs in, so it is refused rather than converted.
q "CREATE TABLE src_lt (ts timestamp, v int) USING pgcolumnar;" >/dev/null
check "a ts_column that is not timestamptz is refused" \
	"$(sqlstate "SELECT pgcolumnar.create_rollup('r_t', 'src_lt', 'ts', '1 hour', '$ORIGIN', '{}', '{count}', '{*}');")" "22023"
check "a group_by column the source does not have is refused" \
	"$(sqlstate "SELECT pgcolumnar.create_rollup('r_g', 'src', 'ts', '1 hour', '$ORIGIN', '{nosuch}', '{count}', '{*}');")" "22023"
check "a sum over a column that cannot be folded is refused" "$(refused_create r_f '1 hour' '{sum}' '{ts}')" "22023"
check "rebase_rollup of a table that is not a rollup is refused (42704)" \
	"$(sqlstate "SELECT pgcolumnar.rebase_rollup('src');")" "42704"
# An arm, not a premise: with a refusal removed, the create succeeds and leaves a
# table (the months-check mutation did), so a repository change reddens it.
check_num "and no refused rollup left a table behind" \
	"$(q "SELECT count(*) FROM pg_class WHERE relname IN ('r_m', 'r_p', 'r_l', 'r_c', 'r_s', 'r_t', 'r_g', 'r_f');")" "0"

# ---- a rollup and its source are dropped together (#1376) ----------------------
#
# The catalog held the source and the view as bare OIDs. DROP TABLE of the source
# succeeded and left the rollup unrefreshable for good (42P01 on the dead OID, or a
# reused OID folded silently); DROP VIEW left the partials table and the catalog rows
# behind. Now the view depends on the source as a view over it would, so the drop is
# refused and CASCADE takes the rollup; and dropping the view takes the rest of it.
q "CREATE TABLE lsrc (ts timestamptz, v int) USING pgcolumnar;
   INSERT INTO lsrc SELECT '$ORIGIN'::timestamptz + g * interval '7 minutes', g FROM generate_series(1, 500) g;" >/dev/null
for lr in lr_a lr_b; do
	psql_run "SELECT pgcolumnar.create_rollup('$lr', 'lsrc', 'ts', '1 hour', '$ORIGIN', '{}', '{count}', '{*}');" >/dev/null
	psql_run "SELECT pgcolumnar.refresh_rollup('$lr');" >/dev/null
done
lr_a_oid="$(q "SELECT 'lr_a'::regclass::oid")"
lr_b_oid="$(q "SELECT 'lr_b'::regclass::oid")"
lr_left() {	# lr_left OID NAME -> "<view and partials left>|<rollup rows>|<rollup_group rows>"
	q "SELECT (SELECT count(*) FROM pg_class WHERE relname IN ('$2', '$2__partials')) || '|' ||
	          (SELECT count(*) FROM pgcolumnar.rollup WHERE rollup::oid = $1) || '|' ||
	          (SELECT count(*) FROM pgcolumnar.rollup_group WHERE rollup::oid = $1)"
}
check "both rollups over the source refreshed, so each has a view, partials and catalog rows" \
	"$(q "SELECT (SELECT sum(count) FROM lr_a) || '|' || (SELECT sum(count) FROM lr_b)")|$(lr_left "$lr_a_oid" lr_a | cut -d'|' -f1,2)" "500|500|2|1"
check "DROP VIEW of a rollup takes its partials and its catalog rows with it (#1376)" \
	"$(sqlstate "DROP VIEW lr_b")|$(lr_left "$lr_b_oid" lr_b)" "ok|0|0|0"
check "DROP TABLE of a rollup's source is refused while the rollup exists (2BP01, #1376)" \
	"$(sqlstate "DROP TABLE lsrc")|$(q "SELECT count(*) FROM pg_class WHERE relname = 'lsrc'")" "2BP01|1"
check "and DROP TABLE ... CASCADE takes the rollup's view, partials and catalog rows with it (#1376)" \
	"$(sqlstate "SET client_min_messages = warning; DROP TABLE lsrc CASCADE")|$(lr_left "$lr_a_oid" lr_a)" "ok|0|0|0"
# The trigger that records those dependencies writes to pg_depend for whoever writes
# a rollup row, and the row-level security on the catalog only asks that they own
# the view. So it asks what a view over the source would: SELECT on the source.
# Without that, a role could tie a table it cannot read to a view of its own and
# block the table owner's DROP TABLE.
psql_run "DROP ROLE IF EXISTS lr_stranger; CREATE ROLE lr_stranger LOGIN;
          GRANT USAGE, CREATE ON SCHEMA public TO lr_stranger; GRANT USAGE ON SCHEMA pgcolumnar TO lr_stranger;
          CREATE TABLE lr_victim (ts timestamptz, v int) USING pgcolumnar; REVOKE ALL ON lr_victim FROM PUBLIC;"
lr_tie="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U lr_stranger -d "$PGC_DB" \
	-X -At -v ON_ERROR_STOP=1 -v VERBOSITY=sqlstate -c "CREATE TABLE lr_mine (bucket timestamptz) USING pgcolumnar;
	CREATE VIEW lr_my_view AS SELECT bucket FROM lr_mine;
	INSERT INTO pgcolumnar.rollup (rollup, partials, source, source_storage, source_oid, ts_column, width, origin,
	  group_by, agg_functions, agg_columns)
	VALUES ('lr_my_view', 'lr_mine', 'lr_victim', 0, 0, 'ts', '1 hour', '$ORIGIN', '{}', '{count}', '{*}');" 2>&1 >/dev/null)"
check "a role cannot tie a rollup row to a table it cannot read (42501, #1376)" \
	"$(st="$(sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\).*/\1/p' <<<"$lr_tie" | head -1)"; echo "${st:-${lr_tie:+other}}")|$(sqlstate "DROP TABLE lr_victim")" "42501|ok"
# The partials table goes with the view by an AUTO dependency, and a drop deletes AUTO
# dependents with no ownership check. So the trigger requires the writer to own the
# partials too, and the rollup to be a view and the partials a table (#1406 review):
# otherwise a role names another role's table as its partials and drops its own view.
as_stranger() {	# as_stranger SQL -> ok, the SQLSTATE it fails with, or other
	local out st
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U lr_stranger -d "$PGC_DB" \
		-X -At -v ON_ERROR_STOP=1 -v VERBOSITY=sqlstate -c "$1" 2>&1 >/dev/null)"
	[ -z "$out" ] && { echo ok; return; }
	st="$(sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\).*/\1/p' <<<"$out" | head -1)"
	echo "${st:-other}"
}
psql_run "CREATE TABLE lr_others (bucket timestamptz) USING pgcolumnar; GRANT SELECT ON lr_others TO lr_stranger;
          CREATE TABLE lr_src2 (ts timestamptz, v int) USING pgcolumnar; GRANT SELECT ON lr_src2 TO lr_stranger;"
lr_row() {	# lr_row VIEW PARTIALS -> a rollup row over lr_src2
	echo "INSERT INTO pgcolumnar.rollup (rollup, partials, source, source_storage, source_oid, ts_column, width, origin,
	  group_by, agg_functions, agg_columns) VALUES ('$1', '$2', 'lr_src2', 0, 0, 'ts', '1 hour', '$ORIGIN', '{}', '{count}', '{*}');"
}
check "a role cannot name another role's table as its rollup's partials (42501), and that table survives its view's drop (#1376)" \
	"$(as_stranger "CREATE TABLE lr_p2 (bucket timestamptz) USING pgcolumnar; CREATE VIEW lr_v2 AS SELECT bucket FROM lr_p2; $(lr_row lr_v2 lr_others)")|$(as_stranger "SET client_min_messages = warning; DROP VIEW IF EXISTS lr_v2")|$(q "SELECT count(*) FROM pg_class WHERE relname = 'lr_others'")" \
	"42501|ok|1"
check "a rollup row whose rollup is not a view is refused (42809, #1376)" \
	"$(as_stranger "CREATE TABLE lr_notview (bucket timestamptz) USING pgcolumnar; CREATE TABLE lr_p4 (bucket timestamptz) USING pgcolumnar; $(lr_row lr_notview lr_p4)")" "42809"
psql_run "CREATE TABLE lr_p5 (bucket timestamptz) USING pgcolumnar; CREATE VIEW lr_v5 AS SELECT bucket FROM lr_p5;
          CREATE TABLE lr_p6 (bucket timestamptz) USING pgcolumnar; $(lr_row lr_v5 lr_p5)"
check "a rollup row's view, partials and source cannot be changed once written (0A000, #1376)" \
	"$(sqlstate "UPDATE pgcolumnar.rollup SET partials = 'lr_p6' WHERE rollup = 'lr_v5'::regclass")" "0A000"

# Row numbers start at 1 (#1341). LAST IN THE SUITE ON PURPOSE: without the floor,
# row_tid(0) crashed the backend (measured on PG17), and a crash mid-suite takes the
# checks after it down with it, premises included.
check "row_tid refuses a row number below 1 (22003), and row 1 is accepted" \
	"$(sqlstate "SELECT pgcolumnar.row_tid(0)")|$(sqlstate "SELECT pgcolumnar.row_tid(-1)")|$(sqlstate "SELECT pgcolumnar.row_tid(1)")" \
	"22003|22003|ok"

pgc_summary
