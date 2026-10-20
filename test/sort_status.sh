#!/usr/bin/env bash
#
# pgColumnar sort decay visibility (#301): pgcolumnar.vacuum_sorted and
# pgcolumnar.cluster order a relation once and do not keep it ordered, so rows
# inserted afterwards append behind the ordered run. Before this change nothing
# reported how large that unsorted tail had grown, so a DBA had no way to decide
# when a re-sort was worth its cost.
#
# pgcolumnar.storage.sorted_through records the row group number the last
# ordering rewrite ended at, and pgcolumnar.sort_status reports the split. This
# suite validates that the mark means what it says: it is unset before any sort,
# it covers every group after one, it leaves an appended tail as rows are
# inserted, an unsorted rewrite clears it, and retiring a group inside the run
# does not slide the boundary onto the tail.
#
# Usage:  test/sort_status.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# A 1000-row group limit over 5000 rows gives 5 groups, so a count is readable
# and an appended tail is countable rather than inferred from one group.
psql_run "CREATE TABLE t (id int, k int, v text) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('t', stripe_row_limit => 1000, chunk_group_row_limit => 500);"
psql_run "INSERT INTO t SELECT g, (g * 7919) % 5000, 'v' || g FROM generate_series(1, 5000) g;"

ss() { q "SELECT $1 FROM pgcolumnar.sort_status('t');"; }
sid() { q "SELECT pgcolumnar.get_storage_id('t');"; }

# ---------------------------------------------------------------- before a sort

check "groups written" "$(ss total_groups)" "5"
check "unsorted table reports no sorted groups" "$(ss sorted_groups)" "0"
check "unsorted table reports every group appended" "$(ss appended_groups)" "5"
check "unsorted table reports no sorted rows" "$(ss sorted_rows)" "0"
check "unsorted table reports every row appended" "$(ss appended_rows)" "5000"
check "no declared sort key yet" "$(ss "sort_key IS NULL")" "t"

# ------------------------------------------------------------------ after a sort

SID_BEFORE="$(sid)"
psql_run "SELECT pgcolumnar.vacuum_sorted('t', 'k');"
SID_AFTER="$(sid)"

# A missing storage id after the sort used to differ from the one before, and read
# as a rewrite.
check "the sort rewrote into a new storage" \
	"$(if [ -z "$SID_BEFORE" ] || [ -z "$SID_AFTER" ]; then echo "no reading: storage [$SID_BEFORE] -> [$SID_AFTER]"
	   elif [ "$SID_BEFORE" != "$SID_AFTER" ]; then echo yes; else echo no; fi)" "yes"
check "rows survived the sort" "$(q 'SELECT count(*) FROM t;')" "5000"
check "the table is physically ordered on k" \
	"$(q "SELECT count(*) FROM (SELECT k, lag(k) OVER () AS p FROM t) s WHERE p > k;")" "0"

TOTAL_SORTED="$(ss total_groups)"
# check_num: sort_status raising read "" here and in TOTAL_SORTED, and two empty
# readings agreed. The same holds for the two extent checks below.
check_num "a fresh sort reports every group sorted" "$(ss sorted_groups)" "$TOTAL_SORTED"
check "a fresh sort reports no decay" "$(ss appended_groups)" "0"
check "a fresh sort reports every row sorted" "$(ss sorted_rows)" "5000"
check "a fresh sort reports no appended rows" "$(ss appended_rows)" "0"

# ------------------------------------------------------------- decay by insert

# 2500 more rows at a 1000-row group limit add 3 groups: two full and one
# partial. They are appended in insert order behind the sorted run.
psql_run "INSERT INTO t SELECT g, (g * 7919) % 5000, 'v' || g FROM generate_series(5001, 7500) g;"

check_num "the sorted extent did not move" "$(ss sorted_groups)" "$TOTAL_SORTED"
check "appended groups appeared" "$(ss appended_groups)" "3"
check "appended rows are counted" "$(ss appended_rows)" "2500"
check "sorted rows are unchanged" "$(ss sorted_rows)" "5000"
check "the totals add up" \
	"$(q "SELECT (sorted_groups + appended_groups = total_groups)::text FROM pgcolumnar.sort_status('t');")" \
	"true"
check "the table is no longer fully ordered" \
	"$(q "SELECT (count(*) > 0)::text FROM (SELECT k, lag(k) OVER () AS p FROM t) s WHERE p > k;")" \
	"true"

# ------------------------------------------------------- a re-sort clears decay

psql_run "SELECT pgcolumnar.vacuum_sorted('t', 'k');"
check "re-sorting clears the appended tail" "$(ss appended_groups)" "0"
check "re-sorting counts every row as sorted" "$(ss sorted_rows)" "7500"
check "re-sorting reports no appended rows" "$(ss appended_rows)" "0"

# --------------------------------------------- an unsorted rewrite clears it

# This is the property the storage row placement buys. pgcolumnar.vacuum does
# not order anything, and it rewrites into a new storage id, so the mark comes
# back unset with no invalidation step of its own. A mark keyed by relation
# instead of by storage would still claim the table was ordered here.
psql_run "SELECT pgcolumnar.vacuum('t');"
check "an unsorted rewrite reports no sorted groups" "$(ss sorted_groups)" "0"
check "an unsorted rewrite reports no sorted rows" "$(ss sorted_rows)" "0"
check "an unsorted rewrite reports every row appended" "$(ss appended_rows)" "7500"

# ------------------------------------------------------------------- z-order

# pgcolumnar.cluster orders by a Z-order code over the given columns. It is a
# separate rewrite path from the plain sort above, and it is an order, so it
# must set the mark the same way.
psql_run "SELECT pgcolumnar.cluster('t', 'id', 'k');"
check "cluster reports no decay" "$(ss appended_groups)" "0"
check "cluster counts every row as sorted" "$(ss sorted_rows)" "7500"

# The online path records its extent too (#311). It reorders under a lock that
# permits concurrent inserts, so it cannot trust the group numbering: it reports
# the stripe ids it reserved, and the run stops at the first live group it did
# not write.
psql_run "SELECT pgcolumnar.vacuum('t');"
check "the unsorted rewrite cleared the mark again" "$(ss sorted_groups)" "0"
psql_run "SELECT pgcolumnar.recluster('t', 'id', 'k');"
check "recluster records its extent" \
	"$( [ "$(ss sorted_groups)" -gt 0 ] && echo yes || echo no )" "yes"
check "recluster reports no decay" "$(ss appended_groups)" "0"
check "recluster counts every row as sorted" "$(ss sorted_rows)" "7500"
check "recluster reports no appended rows" "$(ss appended_rows)" "0"

# Rows inserted after it are outside the run, same as for the eager paths.
psql_run "INSERT INTO t SELECT g, (g * 7919) % 5000, 'v' || g FROM generate_series(7501, 8500) g;"
check "rows inserted after recluster are appended" "$(ss appended_rows)" "1000"
check "the reclustered rows are still counted as sorted" "$(ss sorted_rows)" "7500"

# --------------------------------------- retiring a group inside the run

# This is why the mark is a boundary and not a count. pgcolumnar.compact_rewrite
# rewrites a partially deleted group's survivors into a fresh group with a higher
# number and retires the original. The survivors are no longer in the run's
# order, so they must be counted as appended. A count of sorted groups would
# slide down onto the replacement and report the run as intact.
psql_run "SELECT pgcolumnar.vacuum_sorted('t', 'k');"
RUN_GROUPS="$(ss sorted_groups)"
# Delete by the sort key, not by id. After a sort on k the rows are ordered by
# k, so a low-k range falls inside the first groups and takes their deleted
# fraction over the threshold. Deleting an id range would spread the same number
# of rows evenly and rewrite nothing.
psql_run "DELETE FROM t WHERE k < 400;"
REWRITTEN="$(q "SELECT pgcolumnar.compact_rewrite('t', 0.1, 0);")"
check "a group was rewritten out of the run" \
	"$( [ "$REWRITTEN" -gt 0 ] && echo yes || echo no )" "yes"
RUN_AFTER="$(ss sorted_groups)"
check "the run lost the retired groups" \
	"$( [ "$RUN_AFTER" -lt "$RUN_GROUPS" ] 2>/dev/null && echo yes || echo "no ($RUN_AFTER sorted groups, from $RUN_GROUPS)")" "yes"
APPENDED_AFTER="$(ss appended_groups)"
check "the replacement counts as appended" \
	"$( [ "$APPENDED_AFTER" -ge "$REWRITTEN" ] 2>/dev/null && echo yes || echo "no ($APPENDED_AFTER appended, $REWRITTEN rewritten)")" "yes"
check "the totals still add up" \
	"$(q "SELECT (sorted_groups + appended_groups = total_groups)::text FROM pgcolumnar.sort_status('t');")" \
	"true"

# ---------------------------------------------------------- the declared key

# A declared sort key is reported alongside the counts, so one call answers both
# "on what" and "how much of it is still true".
psql_run "SELECT pgcolumnar.set_options('t', sort_by => ARRAY['k']);"
check "the declared sort key is reported" "$(ss "array_to_string(sort_key, ',')")" "k"

# ------------------------------------------------------------- an empty table

psql_run "CREATE TABLE e (id int) USING pgcolumnar;"
check "an empty table reports no groups" \
	"$(q 'SELECT total_groups FROM pgcolumnar.sort_status('"'"'e'"'"');')" "0"
check "an empty table reports no sorted groups" \
	"$(q 'SELECT sorted_groups FROM pgcolumnar.sort_status('"'"'e'"'"');')" "0"
check "an empty table reports no appended rows" \
	"$(q 'SELECT appended_rows FROM pgcolumnar.sort_status('"'"'e'"'"');')" "0"

# ------------------------------------------------- deletes do not move the mark

# A delete marks rows dead in place. It removes no row group, so the sorted
# extent is unchanged; the stored row counts include the dead rows, which is
# what the function documents.
psql_run "SELECT pgcolumnar.vacuum_sorted('t', 'k');"
BEFORE_DEL="$(ss sorted_groups)"
BEFORE_ROWS="$(q 'SELECT count(*) FROM t;')"
DOOMED="$(q 'SELECT count(*) FROM t WHERE id % 10 = 0;')"
psql_run "DELETE FROM t WHERE id % 10 = 0;"
check "the delete removed rows" \
	"$( [ "$DOOMED" -gt 0 ] && echo yes || echo no )" "yes"
check_num "a delete leaves the sorted extent alone" "$(ss sorted_groups)" "$BEFORE_DEL"
check "a delete leaves no appended tail" "$(ss appended_groups)" "0"
check "live rows dropped by the deleted count" "$(q 'SELECT count(*) FROM t;')" \
	"$((BEFORE_ROWS - DOOMED))"

# ---------------------------------------------------------------- the catalog

# The mark is on pgcolumnar.storage, keyed by storage id, so it belongs to the
# layout rather than to the relation name.
check "the mark covers the run's last group" \
	"$(q "SELECT (st.sorted_through = (SELECT max(rg.group_number)
										FROM pgcolumnar.row_group rg
										WHERE rg.storage_id = st.storage_id))::text
		  FROM pgcolumnar.storage st WHERE st.storage_id = $(sid);")" \
	"true"
check "the storage the sort abandoned is gone" \
	"$(q "SELECT count(*) FROM pgcolumnar.storage WHERE storage_id = $SID_BEFORE;")" "0"
check "dropping the table removes the storage row" \
	"$(SID_NOW="$(sid)"; psql_run "DROP TABLE t;" >/dev/null; q "SELECT count(*) FROM pgcolumnar.storage WHERE storage_id = $SID_NOW;")" \
	"0"

pgc_summary
