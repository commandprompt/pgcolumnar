#!/usr/bin/env bash
#
# pgColumnar online rewrite of partially-deleted groups (Phase F3b).
# pgcolumnar.compact_rewrite() rewrites groups whose deleted fraction exceeds a
# threshold into fresh groups (dropping the dead rows) with fresh row numbers,
# ONLINE under ShareUpdateExclusiveLock. This suite proves: results match a heap
# mirror; the dead rows' space is reclaimed (deletedrows drops); INDEX scans
# return each live row exactly once despite the transient old/new row-number
# overlap (unique constraint still holds, point lookups correct); and the lock is
# ShareUpdateExclusiveLock, never AccessExclusiveLock. Concurrency is covered by
# native_rewrite_conc.sh.
#
# Usage:  test/native_rewrite.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

GEN="SELECT g, (g % 1000) AS v, 'p' || (g % 100) AS payload
  FROM generate_series(1, 8192) g"

psql_run "CREATE TABLE h (id int, v int, payload text);"
psql_run "CREATE TABLE n (id int, v int, payload text) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('n', stripe_row_limit => 1024, chunk_group_row_limit => 1024);"
psql_run "INSERT INTO h $GEN;"
psql_run "INSERT INTO n $GEN;"
psql_run "CREATE UNIQUE INDEX n_id_uq ON n (id);"
psql_run "CREATE INDEX n_v ON n (v);"

deletedrows() { q "SELECT COALESCE(sum(deletedrows), 0) FROM pgcolumnar.stats('n'::regclass);"; }

# Partially delete a third of the rows across every group (id % 3 = 0), commit.
psql_run "DELETE FROM h WHERE id % 3 = 0;"
psql_run "DELETE FROM n WHERE id % 3 = 0;"

check_num "row count after delete" "$(q 'SELECT count(*) FROM n;')" "$(q 'SELECT count(*) FROM h;')"
check "there are deleted rows to reclaim" "$([ "$(deletedrows)" -gt 0 ] && echo yes || echo no)" "yes"
check "parity after delete" \
	"$(pgc_set_hash 'SELECT id, v, payload FROM n ORDER BY id')" \
	"$(pgc_set_hash 'SELECT id, v, payload FROM h ORDER BY id')"

# Online rewrite of the partially-deleted groups.
rew="$(q "SELECT pgcolumnar.compact_rewrite('n', 0.2);")"
echo "  (compact_rewrite rewrote $rew groups; deletedrows now $(deletedrows))"
check "rewrote some groups" "$([ "$rew" -gt 0 ] && echo yes || echo no)" "yes"
check "dead-row space reclaimed" "$(deletedrows)" "0"

# Results unchanged.
check "parity after rewrite" \
	"$(pgc_set_hash 'SELECT id, v, payload FROM n ORDER BY id')" \
	"$(pgc_set_hash 'SELECT id, v, payload FROM h ORDER BY id')"
check_num "row count after rewrite" \
	"$(q 'SELECT count(*) FROM n;')" \
	"$(q 'SELECT count(*) FROM h;')"

# The threshold, measured from both sides. A rewrite with nothing deleted must leave
# every group alone, and a group deleted below min_deleted_fraction is not a
# candidate. With every undeleted group rewritten, or the threshold ignored, all 17
# checks above and below passed on main.
check_num "a second rewrite with nothing deleted rewrites no group" \
	"$(q "SELECT pgcolumnar.compact_rewrite('n', 0.2);")" "0"
psql_run "DELETE FROM h WHERE id = 2;"
psql_run "DELETE FROM n WHERE id = 2;"
check_num "a group deleted below the threshold is not rewritten" \
	"$(q "SELECT pgcolumnar.compact_rewrite('n', 0.2);")" "0"

# Index-scan correctness: force an index scan and confirm each live row is
# returned exactly once (no duplicate from lingering old-number index entries).
idxcount() {	# [UPPER] -> live rows with id in 1..UPPER read through the index, or nothing
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At 2>/dev/null -c \
		"SET enable_seqscan=off; SET pgcolumnar.enable_custom_scan=off;
		 SELECT count(*) FROM n WHERE id BETWEEN 1 AND ${1:-8192};" | tail -1
}
check_num "index scan returns each live row once" "$(idxcount)" "$(q 'SELECT count(*) FROM h;')"
# Point lookups via the unique index.
check "live id present via index" "$(q 'SELECT count(*) FROM n WHERE id = 4097;')" "1"
check "deleted id absent via index" "$(q 'SELECT count(*) FROM n WHERE id = 4098;')" "0"

# Uniqueness still enforced after the rewrite (no stale live duplicate).
# dupfails -> "yes" only when the insert is refused by n_id_uq itself (23505 and its
# message). Any other failure is not enforcement: with the server stopped, the old
# "psql failed, so yes" passed.
dupfails() {
	local out
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -v ON_ERROR_STOP=1 -v VERBOSITY=verbose \
		-c "INSERT INTO n VALUES (4097, 0, 'x');" 2>&1)" && { echo "no (the duplicate was inserted)"; return; }
	case "$out" in
	*'23505'*'duplicate key value violates unique constraint "n_id_uq"'*) echo yes ;;
	*) echo "no ($(head -1 <<<"$out"))" ;;
	esac
}
check "unique constraint still enforced" "$(dupfails)" "yes"

# Lock level: ShareUpdateExclusiveLock, never AccessExclusiveLock.
locks="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
	-d "$PGC_DB" -At 2>/dev/null <<'SQL' | grep -E '^[0-9]+\|[0-9]+$'
BEGIN;
SELECT pgcolumnar.compact_rewrite('n', 0.0);
SELECT count(*) FILTER (WHERE mode='ShareUpdateExclusiveLock')::text || '|' ||
       count(*) FILTER (WHERE mode='AccessExclusiveLock')::text
  FROM pg_locks WHERE relation='n'::regclass AND locktype='relation';
ROLLBACK;
SQL
)"
check "compact_rewrite holds ShareUpdateExclusiveLock" "${locks%%|*}" "1"
check "compact_rewrite holds no AccessExclusiveLock" "${locks##*|}" "0"

# ---------------------------------------------------------------------------
# What makes the statement-scoped fetch cache safe (#143, #148).
#
# PgColumnarReadRowByNumber caches a decoded row group keyed by
# (storage_id, group_number). That is only sound because such a pair is never
# re-served with different bytes, which rests on two allocator properties rather
# than on any locking:
#
#   group numbers come from the monotonic metapage counter reserved in
#   PgColumnarReserveRowNumbers (meta->reservedStripeId += 1), never from the
#   catalog maximum, so retiring a group does not free its number for reuse;
#
#   a rewrite writes into freshly allocated storage rather than over the old.
#
# Both are asserted here rather than in the fetch path, because this is where a
# change would be made. If either stops holding, the cache can hand back a decode
# of bytes that no longer exist and these checks are the warning.
# ---------------------------------------------------------------------------

before_max="$(q "SELECT coalesce(max(group_number), -1) FROM pgcolumnar.row_group
                  WHERE storage_id = pgcolumnar.get_storage_id('n');")"
psql_run "SELECT pgcolumnar.compact_rewrite('n', 0.1);" >/dev/null
# Fresh ids: n carries a unique index, so re-inserting the original range would
# fail and leave no new groups to look at.
EXTRA="SELECT g + 1000000, g % 1000, 'p' || (g % 100) FROM generate_series(1, 2000) g"
psql_run "INSERT INTO n $EXTRA;"
psql_run "INSERT INTO h $EXTRA;"
after_min_new="$(q "SELECT coalesce(min(group_number), -1) FROM pgcolumnar.row_group
                     WHERE storage_id = pgcolumnar.get_storage_id('n')
                       AND group_number > $before_max;")"

check "a rewrite plus later writes reuse no group number" \
	"$(q "SELECT count(*) = count(DISTINCT group_number) FROM pgcolumnar.row_group
	       WHERE storage_id = pgcolumnar.get_storage_id('n');")" "t"
check "group numbers issued after a rewrite are past every earlier one" \
	"$([ "$after_min_new" -gt "$before_max" ] && echo yes || echo no)" "yes"

# A full rewrite moves the table to new storage, so a cached decode keyed by the
# old storage id can never match afterwards.
sid_before="$(q "SELECT pgcolumnar.get_storage_id('n');")"
psql_run "SELECT pgcolumnar.vacuum('n');" >/dev/null
sid_after="$(q "SELECT pgcolumnar.get_storage_id('n');")"
check "vacuum rewrites into a fresh storage id" \
	"$([ "$sid_after" != "$sid_before" ] && echo yes || echo no)" "yes"

# And the rows survive all of it, which is what the invariants are for.
check_num "every live row still reads back after rewrite and vacuum" \
	"$(q 'SELECT count(*) FROM n;')" "$(q 'SELECT count(*) FROM h;')"
# vacuum assigned every row a fresh number, so the indexes had to be rebuilt to point
# at them. A stale entry still fetches A row, just the wrong one, and over the whole
# table the stale entries land on every live row once: with the rebuild skipped, a
# count and a hash of everything read through the index both still matched, and every
# check above passed on main. A narrow range is what tells: its entries fetch rows
# from elsewhere in the table (id 4097 read back as 6146).
idxids() {	# LO HI -> the ids an index range read of n returns, or nothing
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At 2>/dev/null -c \
		"SET enable_seqscan=off; SET enable_bitmapscan=off; SET pgcolumnar.enable_custom_scan=off;
		 SELECT string_agg(id::text, ',' ORDER BY id) FROM n WHERE id BETWEEN $1 AND $2;" | tail -1
}
check_text "an index range read after vacuum returns the rows asked for" \
	"$(idxids 4090 4100)" \
	"$(q "SELECT string_agg(id::text, ',' ORDER BY id) FROM h WHERE id BETWEEN 4090 AND 4100;")"

pgc_summary
