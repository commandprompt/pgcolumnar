#!/usr/bin/env bash
#
# pgColumnar reclaim reconciliation (Phase F). The free_space catalog is
# transactional, so normal retirement is atomic. The one seam is physical
# end-truncation: a crash in its narrow window can leave a free_space row that
# overlaps a live group (the highwater was lowered but the row's delete rolled
# back, and a later insert placed a live group there). PgColumnarReconcileFreeList
# runs at the start of every reuse op (compact_rewrite, recluster) and drops any
# free_space row overlapping a live row-group footprint, so it cannot be handed
# out on top of a live group.
#
# This suite synthesizes that post-crash state directly: it INSERTs a stale
# free_space row over a live group's byte range, then runs compact_rewrite and
# asserts the reconcile purged it and data is intact. (Without the reconcile the
# assert-build no-overlap validator fires on the overlapping row.)
#
# Usage:  test/native_reclaim_reconcile.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

hash_n() { pgc_set_hash 'SELECT id, v FROM n'; }
hash_h() { pgc_set_hash 'SELECT id, v FROM h'; }

# enable end-truncation for the whole db (a single-statement truncate below must
# not be wrapped in a transaction block, which truncate refuses).
psql_run "ALTER DATABASE $PGC_DB SET pgcolumnar.enable_end_truncation = on;"

psql_run "CREATE TABLE h (id int, v text);"
psql_run "CREATE TABLE n (id int, v text) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('n', stripe_row_limit => 1000, chunk_group_row_limit => 1000);"
psql_run "INSERT INTO h SELECT g, md5(g::text) FROM generate_series(1, 8000) g;"
psql_run "INSERT INTO n SELECT g, md5(g::text) FROM generate_series(1, 8000) g;"
# partial deletes so every group is a compact_rewrite candidate
psql_run "DELETE FROM h WHERE id % 3 = 0;"
psql_run "DELETE FROM n WHERE id % 3 = 0;"
check "parity before" "$(hash_n)" "$(hash_h)"

# a live group's byte range, and the storage id
sid="$(q "SELECT pgcolumnar.get_storage_id('n');")"
off="$(q "SELECT fileoffset FROM pgcolumnar.stats('n') ORDER BY fileoffset LIMIT 1;")"
len="$(q "SELECT datalength FROM pgcolumnar.stats('n') ORDER BY fileoffset LIMIT 1;")"

# synthesize the post-crash inverted state: a reusable (ancient freed_xid) free
# range sitting on top of a live group. len is the raw datalength, not the
# page-rounded footprint real free_space rows store; it still overlaps the group,
# which is all the reconcile keys on.
inject() {  # inject a stale free row over the first live group; echoes nothing
	local o l
	o="$(q "SELECT fileoffset FROM pgcolumnar.stats('n') ORDER BY fileoffset LIMIT 1;")"
	l="$(q "SELECT datalength FROM pgcolumnar.stats('n') ORDER BY fileoffset LIMIT 1;")"
	psql_run "INSERT INTO pgcolumnar.free_space VALUES ($sid, $o, $l, 1);"
}
stalerows() { q "SELECT count(*) FROM pgcolumnar.free_space WHERE freed_xid=1;"; }

inject
check "stale overlapping free row injected" "$(stalerows)" "1"

# compact_rewrite reconciles first, so it neither reuses the stale row nor trips
# the no-overlap validator; data stays correct.
psql_run "SELECT pgcolumnar.compact_rewrite('n', 0.1);"
check "compact_rewrite reconciled the stale row" "$(stalerows)" "0"
check "data intact after reconcile + rewrite" "$(hash_n)" "$(hash_h)"
# check_num: two failed reads are two empty strings, which plain check would call equal.
check_num "row count intact" "$(q 'SELECT count(*) FROM n;')" "$(q 'SELECT count(*) FROM h;')"

# a PLAIN compact must also self-heal it: it asserts the no-overlap invariant but
# does not reuse, so it must reconcile too (fails without that call).
#
# And it must drop ONLY the overlapping row. The checks above count the stale row
# alone, so a reconcile that dropped EVERY free row (leaking all reusable space)
# passed all of them: measured, 11/11. Plain compact does not reuse, so the
# legitimate free ranges the earlier rewrite left must come through it unchanged.
legit() { q "SELECT count(*) || '|' || coalesce(sum(byte_length), 0) FROM pgcolumnar.free_space
            WHERE storage_id = $sid AND freed_xid <> 1;"; }
inject
legit_before="$(legit)"
# Not a premise: the ranges exist only because compact_rewrite recorded them, and a
# record_free_space that drops its insert (one line) leaks them all and reddens this.
check "compact_rewrite recorded the free ranges it left" \
	"$(case "$legit_before" in ''|0\|*) echo "no ($legit_before)" ;; *) echo yes ;; esac)" "yes"
psql_run "SELECT pgcolumnar.compact('n');"
check "plain compact reconciled the stale row" "$(stalerows)" "0"
check_text "plain compact kept the legitimate free ranges, which overlap no live group" \
	"$(legit)" "$legit_before"
check "data intact after compact reconcile" "$(hash_n)" "$(hash_h)"

# a truncate must likewise self-heal it (its start purge only removes rows at or
# above the highwater; the stale row sits below it, over a live group).
inject
psql_run "SELECT pgcolumnar.truncate('n');"
check "truncate reconciled the stale row" "$(stalerows)" "0"
check "data intact after truncate reconcile" "$(hash_n)" "$(hash_h)"

# reuse keeps working correctly afterward.
# Unrolled, so each check name is a literal the parity grader can read.
psql_run "DELETE FROM h WHERE id % 5 = 1;"
psql_run "DELETE FROM n WHERE id % 5 = 1;"
psql_run "SELECT pgcolumnar.compact_rewrite('n', 0.0);"
check "parity after reuse cycle 1" "$(hash_n)" "$(hash_h)"
psql_run "DELETE FROM h WHERE id % 5 = 2;"
psql_run "DELETE FROM n WHERE id % 5 = 2;"
psql_run "SELECT pgcolumnar.compact_rewrite('n', 0.0);"
check "parity after reuse cycle 2" "$(hash_n)" "$(hash_h)"

pgc_summary
