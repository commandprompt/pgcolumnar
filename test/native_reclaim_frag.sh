#!/usr/bin/env bash
#
# pgColumnar physical reclaim under fragmentation (Phase F split/coalesce).
#
# Runs an identical retire+recluster workload twice -- once with
# pgcolumnar.reclaim_coalesce on (split oversized freed ranges on reuse, coalesce
# adjacent same-transaction frees) and once off (whole-range reuse) -- and
# asserts:
#   * the live set matches a heap mirror in BOTH modes (correctness);
#   * coalescing actually merges: fully deleting a large CONTIGUOUS block of
#     groups and compacting frees many small adjacent byte ranges, and with the
#     GUC on they collapse to FEWER free_space rows than with it off (direct,
#     deterministic evidence the coalesce path runs); and
#   * coalesce is never worse than whole-range reuse for the final file size; and
#   * with the GUC on, reuse splits an oversized freed range and records its tail as
#     free, so no byte between the live groups and the free ranges is lost (#1339).
# The assert-build no-overlap validator (PgColumnarCheckFreeSpaceNoOverlap) runs at
# the end of every compaction here, so this also exercises the tiling invariant
# on variable-size ranges.
#
# Usage:  test/native_reclaim_frag.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# group k (= id/1000) gets width 10..250 bytes, so byte sizes vary widely.
GEN="SELECT g AS id, repeat('ab', 5 + ((g / 1000) % 16) * 8) AS payload
  FROM generate_series(1, 30000) g"

freerows() { q "SELECT count(*) FROM pgcolumnar.free_space WHERE storage_id = pgcolumnar.get_storage_id('n');"; }

# THE SPLIT, MEASURED AS A GAP (#1339). Every live group (rounded up to whole pages,
# as the allocator reserves it) and every free_space range, in offset order: each
# should end where the next one starts. Reuse of an oversized freed range without
# the split records no remnant, so its tail belongs to nothing and shows here as a
# gap. The file size cannot show it: with the split switched off the final sizes
# were the same (262144 on, 286720 off), and "never larger" stayed green.
gaps() {
	q "WITH p AS (SELECT current_setting('block_size')::bigint - 24 AS pg),
	   r AS (SELECT file_offset AS off, ((byte_length + p.pg - 1) / p.pg) * p.pg AS len
	           FROM pgcolumnar.row_group, p
	          WHERE storage_id = pgcolumnar.get_storage_id('n') AND byte_length > 0
	         UNION ALL
	         SELECT file_offset, byte_length FROM pgcolumnar.free_space
	          WHERE storage_id = pgcolumnar.get_storage_id('n')),
	   s AS (SELECT off, len, lead(off) OVER (ORDER BY off) AS nxt FROM r)
	   SELECT count(*) FILTER (WHERE nxt > off + len) || ' gaps of '
	          || coalesce(sum(nxt - off - len) FILTER (WHERE nxt > off + len), 0) || ' bytes'
	     FROM s;"
}

# ids 6001-24000 fill groups 7 to 24 of 1000 rows each: 18 groups to retire.
DEAD_GROUPS=18

run() {			# $1 = on|off ; echoes "<free_rows_after_compact>|<final_size>|<ok|MISMATCH>|<gaps>|<retired>/<tries>"
	{
		psql_run "DROP TABLE IF EXISTS n;"
		psql_run "DROP TABLE IF EXISTS h;"
		# NO AUTOVACUUM ON THE FIXTURE (#1975). An autovacuum of n would retire the
		# dead groups itself, in the server's coalesce mode rather than $1. An
		# autoanalyze holds a snapshot, and while it does, compact retires nothing.
		psql_run "CREATE TABLE h (id int, payload text) WITH (autovacuum_enabled = false);"
		psql_run "CREATE TABLE n (id int, payload text) USING pgcolumnar WITH (autovacuum_enabled = false);"
		# 30 groups of 1000 rows so a deleted block frees many adjacent ranges.
		psql_run "SELECT pgcolumnar.set_options('n', stripe_row_limit => 1000, chunk_group_row_limit => 1000);"
		psql_run "SET pgcolumnar.reclaim_coalesce = $1; INSERT INTO h $GEN;"
		psql_run "SET pgcolumnar.reclaim_coalesce = $1; INSERT INTO n $GEN;"
		# fully delete a large CONTIGUOUS block of groups, then compact: many small
		# adjacent byte ranges are freed in one transaction.
		psql_run "SET pgcolumnar.reclaim_coalesce = $1; DELETE FROM h WHERE id BETWEEN 6001 AND 24000;"
		psql_run "SET pgcolumnar.reclaim_coalesce = $1; DELETE FROM n WHERE id BETWEEN 6001 AND 24000;"
	} >/dev/null 2>&1

	# WAIT FOR THE HORIZON (#1975). compact retires a group only once no
	# transaction in this database can still see its rows. A transaction whose
	# snapshot or transaction ID is older than the DELETE holds that back: an
	# autoanalyze of some other table, a prepared transaction, a replication
	# slot. compact then retires 0 groups and frees nothing. So
	# pgc_retry_past_horizon calls it again, for up to 30 s, until all the dead
	# groups are retired. An error from compact stops the wait at once and is
	# printed, so it does not read as a held snapshot. When the wait runs out, it
	# prints what holds the horizon back (#1981).
	local fr size ph hh gp ret
	ret="$(pgc_retry_past_horizon "$DEAD_GROUPS" \
		"SET pgcolumnar.reclaim_coalesce = $1; SELECT pgcolumnar.compact('n');")"

	# free_space rows right after the compaction, before anything consumes them.
	fr="$(freerows)"

	{
		# force large output groups and recluster: reuses the freed space.
		psql_run "SET pgcolumnar.reclaim_coalesce = $1; SELECT pgcolumnar.set_options('n', stripe_row_limit => 20000, chunk_group_row_limit => 20000);"
		psql_run "SET pgcolumnar.reclaim_coalesce = $1; SELECT pgcolumnar.recluster('n', 'id');"
	} >/dev/null 2>&1

	size="$(q 'SELECT pg_relation_size('"'"'n'"'"');')"
	gp="$(gaps)"
	ph="$(pgc_set_hash 'SELECT id, payload FROM n')"
	hh="$(pgc_set_hash 'SELECT id, payload FROM h')"
	echo "${fr}|${size}|$([ "$ph" = "$hh" ] && echo ok || echo MISMATCH)|${gp}|${ret}"
}

on="$(run on)";  off="$(run off)"
IFS='|' read -r on_fr on_size on_par on_gap on_ret <<<"$on"
IFS='|' read -r off_fr off_size off_par off_gap off_ret <<<"$off"

check "coalesce on: parity with heap"  "$on_par"  "ok"
check "coalesce off: parity with heap" "$off_par" "ok"
echo "  (groups compact retired/compact calls: coalesce-on=$on_ret coalesce-off=$off_ret, of $DEAD_GROUPS)"
echo "  (free_space rows after compact: coalesce-on=$on_fr coalesce-off=$off_fr)"
echo "  (final file size: coalesce-on=$on_size coalesce-off=$off_size)"
echo "  (unowned bytes between ranges after recluster: coalesce-on=$on_gap coalesce-off=$off_gap)"
# EACH MODE MUST HAVE FREED THE SAME 18 GROUPS (#1975). If compact retired
# nothing in one mode, that mode freed nothing, and every arm below compares
# against an empty free list. With the on run empty, "fewer rows with coalescing
# on" reads 0 < 18 as a pass. So both counts are checked, as numbers.
check "coalesce on: compact retired every dead group"  "${on_ret%/*}"  "$DEAD_GROUPS"
check "coalesce off: compact retired every dead group" "${off_ret%/*}" "$DEAD_GROUPS"
# THE COMPARISON NEEDS SOMETHING TO MERGE (#1975). With coalescing off, compact
# must leave many separate free ranges. CI once counted 0 here, and "fewer rows
# with coalescing on" then failed as a verdict on coalescing it could not give.
# An arm, not a premise: a snapshot held past the wait above reddens it.
check "coalesce off: compact leaves more than one free_space row" \
	"$([ "$off_fr" -gt 1 ] 2>/dev/null && echo yes || echo "no (off=$off_fr)")" "yes"
check "coalesce merges adjacent frees (fewer free_space rows)" \
	"$([ "$on_fr" -lt "$off_fr" ] 2>/dev/null && echo yes || echo "no (on=$on_fr off=$off_fr)")" "yes"
check "coalesce never larger than whole-range reuse" \
	"$([ "$on_size" -le "$off_size" ] && echo yes || echo no)" "yes"
check "coalesce on: reuse splits an oversized freed range, so no byte between ranges is lost" \
	"$on_gap" "0 gaps of 0 bytes"

# A RED NAMES WHAT HOLDS THE HORIZON (#1981). When compact still retires too few
# groups after the wait, pgc_retry_past_horizon prints pgc_horizon_holders. CI
# once went red here with no way to tell which session held the horizon. So a
# second session holds a snapshot, and a wait that cannot reach its count
# ("SELECT 0", 2 calls) must print a report that names its pid.
env PGAPPNAME=pgc_horizon_holder PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" \
	-U postgres -d "$PGC_DB" -qAt -c "SELECT pg_sleep(20);" >/dev/null 2>&1 &
holder_job=$!
holder_pid=""
for _ in $(seq 1 50); do
	holder_pid="$(q "SELECT pid FROM pg_stat_activity
	                 WHERE application_name = 'pgc_horizon_holder' AND backend_xmin IS NOT NULL;")"
	[ -n "$holder_pid" ] && break
	sleep 0.1
done
holders="$(pgc_retry_past_horizon 1 "SELECT 0;" 2 2>&1 >/dev/null)"
echo "  (what a short wait printed while pid ${holder_pid:-none} holds a snapshot:)"
echo "${holders:-<empty>}" | sed 's/^/    /'
check "the horizon report names a session that holds a snapshot" \
	"$(grep -q "session pid=${holder_pid:-none} " <<<"$holders" && echo named || echo "not named (pid=${holder_pid:-none})")" "named"
[ -n "$holder_pid" ] && q "SELECT pg_terminate_backend($holder_pid);" >/dev/null
wait "$holder_job" 2>/dev/null

pgc_summary
