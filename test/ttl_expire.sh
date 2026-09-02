#!/usr/bin/env bash
#
# pgColumnar: pgcolumnar.expire drops row groups whose rows are all older than a
# declared retention, without reading or rewriting them (#403 item 5a).
#
# WHAT IT IS. The tractable half of #403 item 5, "merge-time data transformation".
# Our rewrites already retire whole row groups: pgcolumnar.compact drops every
# group that is fully deleted, through PgColumnarRetireGroup, under
# ShareUpdateExclusiveLock. Retention is the same operation with a different
# predicate, and the zone map already holds what decides it -- a group whose
# MAXIMUM value in the retention column is older than the cutoff contains no row
# that is still within it. So the decision is a catalog read: no decode, no
# rewrite, no scan of the data.
#
# WHY AN EXPLICIT FUNCTION rather than a hook inside vacuum. This deletes rows.
# ClickHouse folds TTL into its merges; an operation a PostgreSQL user runs for
# maintenance must not silently drop their data. It is asked for by name, it
# returns how many groups it dropped, and a table with no declared retention is
# an error rather than a no-op that reads as success.
#
# THE CHECK THAT MATTERS IS THE STRADDLING GROUP. Dropping a group whose rows are
# all expired is the feature; dropping one that still holds live rows is data
# loss. The fixture builds a group that spans the cutoff deliberately and pins
# that it survives with every one of its rows. A test that only proved expired
# data disappears would pass just as well on an implementation that dropped
# everything.
#
# Two more ways a "the maximum is expired" reading is not "every row is
# expired":
#   * a NULL in the retention column. The zone map's max covers only non-NULL
#     timestamps, so a group of old timestamps plus NULLs looks fully expired
#     and would drop the NULLs.
#   * an index-only scan after VACUUM. expire retires live groups without
#     going through the delete vector, so the VM bits VACUUM set stay on and
#     the scan answers from the index without fetching the (now missing) group.
#
# Usage:  test/ttl_expire.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
# Pinned in the cluster config rather than by SET, so the writing session and any
# later session agree about the geometry (#806).
PGC_EXTRA_CONF="${PGC_EXTRA_CONF:-}
pgcolumnar.stripe_row_limit=1000"
export PGC_EXTRA_CONF
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

q() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq \
		-c "$1" 2>&1 | tail -1
}

# 5,000 rows at 1,000 a group = 5 groups, one day apart per 1,000 rows, so each
# group covers a distinct day. Retention of 3 days puts the cutoff INSIDE the
# third group, which is the straddling case.
psql_run "CREATE TABLE ttl_t (id int, ts timestamptz, v text) USING pgcolumnar;"
psql_run "INSERT INTO ttl_t
          SELECT g,
                 now() - make_interval(days => 5) + make_interval(mins => g * 2),
                 'v'||g
          FROM generate_series(1,5000) g;"
psql_run "ANALYZE ttl_t;"

GROUPS_BEFORE="$(q "SELECT count(*) FROM pgcolumnar.storage s
                    JOIN pgcolumnar.row_group rg USING (storage_id)
                    WHERE s.relation_oid = 'ttl_t'::regclass")"
ROWS_BEFORE="$(q "SELECT count(*) FROM ttl_t")"

CUTOFF="SELECT now() - make_interval(days => 3)"
EXPIRED_ROWS="$(q "SELECT count(*) FROM ttl_t WHERE ts < ($CUTOFF)")"
LIVE_ROWS="$(q "SELECT count(*) FROM ttl_t WHERE ts >= ($CUTOFF)")"

# ---- premises -------------------------------------------------------------
check "premise: the fixture is laid out in several row groups" \
	"$([ "${GROUPS_BEFORE:-0}" -ge 4 ] && echo "many ($GROUPS_BEFORE)" || echo "TOO FEW ($GROUPS_BEFORE)")" \
	"many ($GROUPS_BEFORE)"

check "premise: some rows are past the retention and some are not" \
	"$([ "${EXPIRED_ROWS:-0}" -gt 0 ] && [ "${LIVE_ROWS:-0}" -gt 0 ] && echo "both" \
	   || echo "ONE-SIDED (expired=$EXPIRED_ROWS live=$LIVE_ROWS)")" "both"

# The whole safety argument rests on this: a group that spans the cutoff exists,
# so "drop every group with an expired row in it" and "drop every group whose
# rows are all expired" give different answers on this fixture.
STRADDLE_ROWS="$(q "SELECT count(*) FROM ttl_t
                    WHERE ts >= ($CUTOFF) - make_interval(days => 1)
                      AND ts <  ($CUTOFF) + make_interval(days => 1)")"
check "premise: rows exist on both sides of the cutoff within one day of it" \
	"$([ "${STRADDLE_ROWS:-0}" -gt 0 ] && echo "straddled" || echo "NO STRADDLE")" "straddled"

# ---- declaring the retention ----------------------------------------------
psql_run "SELECT pgcolumnar.set_options('ttl_t', ttl_column => 'ts',
                                        ttl_interval => '3 days');"
check "the retention is recorded where the other options live" \
	"$(q "SELECT ttl_column || ' / ' || ttl_interval FROM pgcolumnar.options
	      WHERE regclass = 'ttl_t'::regclass")" "ts / 3 days"

# ---- expiring --------------------------------------------------------------
RETIRED="$(q "SELECT pgcolumnar.expire('ttl_t')")"
ROWS_AFTER="$(q "SELECT count(*) FROM ttl_t")"
GROUPS_AFTER="$(q "SELECT count(*) FROM pgcolumnar.storage s
                   JOIN pgcolumnar.row_group rg USING (storage_id)
                   WHERE s.relation_oid = 'ttl_t'::regclass")"
LIVE_AFTER="$(q "SELECT count(*) FROM ttl_t WHERE ts >= ($CUTOFF)")"
echo "-- before: $ROWS_BEFORE rows in $GROUPS_BEFORE groups ($EXPIRED_ROWS past retention)"
echo "-- expire retired $RETIRED group(s): $ROWS_AFTER rows in $GROUPS_AFTER groups"

check "expire retires at least one group (#403 item 5a)" \
	"$([ "${RETIRED:-0}" -gt 0 ] && echo "retired" || echo "RETIRED NOTHING ($RETIRED)")" "retired"

check "and the table lost exactly the groups it retired" \
	"$(( GROUPS_BEFORE - RETIRED ))" "$GROUPS_AFTER"

# THE SAFETY CHECK. Every row still inside the retention must still be there. An
# implementation that dropped a straddling group would fail here and nowhere else.
check "NO row still inside the retention was dropped (#403 item 5a)" "$LIVE_AFTER" "$LIVE_ROWS"

# Something was actually dropped, so the check above is not passing because the
# function did nothing at all.
check "and the table really is smaller than it was" \
	"$([ "${ROWS_AFTER:-0}" -lt "${ROWS_BEFORE:-0}" ] && echo "smaller" \
	   || echo "UNCHANGED ($ROWS_AFTER of $ROWS_BEFORE)")" "smaller"

check "every remaining row reads back its own value" \
	"$(q "SELECT count(*) FROM ttl_t WHERE v <> 'v'||id")" "0"

# ---- a second run has nothing left to do ----------------------------------
AGAIN="$(q "SELECT pgcolumnar.expire('ttl_t')")"
check "running it again retires nothing, because nothing new expired" "$AGAIN" "0"

# ---- a table with no declared retention ------------------------------------
psql_run "CREATE TABLE ttl_none (id int, ts timestamptz) USING pgcolumnar;"
psql_run "INSERT INTO ttl_none SELECT g, now() FROM generate_series(1,10) g;"
ERR="$(q "SELECT pgcolumnar.expire('ttl_none')")"
check "a table with no declared retention is an error, not a silent success" \
	"$(grep -qiE 'ERROR|no retention|ttl' <<<"$ERR" && echo "refused" || echo "ACCEPTED ($ERR)")" "refused"

# ---- NULL in the retention column is a straddle, not an expiry ------------
psql_run "CREATE TABLE ttl_null (id int, ts timestamptz, v text) USING pgcolumnar;"
psql_run "INSERT INTO ttl_null
          SELECT g,
                 CASE WHEN g <= 10 THEN NULL
                      ELSE now() - interval '10 days' END,
                 'v'||g
          FROM generate_series(1,1000) g;"
psql_run "SELECT pgcolumnar.set_options('ttl_null', ttl_column => 'ts',
                                        ttl_interval => '3 days');"
NULL_BEFORE="$(q "SELECT count(*) FROM ttl_null WHERE ts IS NULL")"
check "premise: the table holds NULL retention rows at all" "$NULL_BEFORE" "10"
# The arm below is about a GROUP that mixes expired rows with NULL ones. A
# table-level count cannot see a row group, and passes just as readily on a
# fixture where the NULL rows sit in a group of their own -- which is the
# arrangement the premise exists to exclude. Count the groups that hold both.
check "premise: and the SAME row group holds expired rows and NULL ones" \
	"$(q "SELECT count(*) FROM (
	        SELECT z.group_number
	        FROM pgcolumnar.zone_map z
	        JOIN pgcolumnar.storage s ON s.storage_id = z.storage_id
	        WHERE s.relation_oid = 'ttl_null'::regclass
	          AND z.null_count > 0
	        GROUP BY z.group_number) g")" "1"
NULL_RETIRED="$(q "SELECT pgcolumnar.expire('ttl_null')")"
NULL_AFTER="$(q "SELECT count(*) FROM ttl_null WHERE ts IS NULL")"
NULL_ROWS="$(q "SELECT count(*) FROM ttl_null")"
check "expire does not retire a group that still holds NULL retention rows" \
	"$NULL_RETIRED" "0"
check "and those NULL rows are still there" "$NULL_AFTER" "10"
check "and the expired timestamps sharing the group were kept with them" \
	"$NULL_ROWS" "1000"

# ---- index-only scan must not return rows expire already retired ----------
psql_run "CREATE TABLE ttl_ios (id int, ts timestamptz) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('ttl_ios', stripe_row_limit => 16384);"
psql_run "INSERT INTO ttl_ios SELECT g, now() - interval '10 days'
          FROM generate_series(1,8000) g;"
psql_run "CREATE INDEX ttl_ios_id ON ttl_ios (id);"
psql_run "ALTER DATABASE $PGC_DB SET pgcolumnar.enable_index_only_scan = on;"
psql_run "ALTER DATABASE $PGC_DB SET pgcolumnar.enable_custom_scan = off;"
psql_run "ALTER DATABASE $PGC_DB SET enable_seqscan = off;"
psql_run "ALTER DATABASE $PGC_DB SET enable_bitmapscan = off;"
psql_run "VACUUM ttl_ios;"
psql_run "SELECT pgcolumnar.set_options('ttl_ios', ttl_column => 'ts',
                                        ttl_interval => '3 days');"
ios_plan_before="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" \
	-U postgres -d "$PGC_DB" -Atq -c \
	"EXPLAIN (COSTS OFF) SELECT id FROM ttl_ios WHERE id BETWEEN 1 AND 8000;")"
check "premise: an index-only scan is chosen on the all-visible table" \
	"$(printf '%s' "$ios_plan_before" | grep -c 'Index Only Scan')" "1"
# The plan shape is decided by pg_class.relallvisible and by enable_seqscan and
# enable_bitmapscan being off -- NOT by the visibility-map bit the fix clears.
# Stop PgColumnarVMSetVisibleForRelation writing bits and the plan is unchanged,
# so the arm above cannot fail for the thing under test. Assert the bit itself.
check "premise: and VACUUM really did set visibility-map bits to clear" \
	"$(q "SELECT CASE WHEN relallvisible > 0 THEN 'set' ELSE 'none' END
	      FROM pg_class WHERE oid = 'ttl_ios'::regclass")" "set"
IOS_VM_BEFORE="$(q "SELECT relallvisible FROM pg_class WHERE oid = 'ttl_ios'::regclass")"
IOS_RETIRED="$(q "SELECT pgcolumnar.expire('ttl_ios')")"
check "expire retires the all-visible expired group" \
	"$([ "${IOS_RETIRED:-0}" -gt 0 ] && echo retired || echo "RETIRED NOTHING ($IOS_RETIRED)")" \
	"retired"
# Seqscan is the catalog truth: the group is gone.
SEQ_AFTER="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" \
	-U postgres -d "$PGC_DB" -Atq -c \
	"SET enable_seqscan = on; SET pgcolumnar.enable_custom_scan = on;
	 SELECT count(*) FROM ttl_ios;")"
check "seqscan agrees the expired rows are gone" "$(printf '%s' "$SEQ_AFTER" | tail -1)" "0"
ios_plan_after="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" \
	-U postgres -d "$PGC_DB" -Atq -c \
	"EXPLAIN (COSTS OFF) SELECT id FROM ttl_ios WHERE id BETWEEN 1 AND 8000;")"
check "the index-only scan is still the plan after expire" \
	"$(printf '%s' "$ios_plan_after" | grep -c 'Index Only Scan')" "1"
IOS_AFTER="$(q "SELECT count(*) FROM ttl_ios WHERE id BETWEEN 1 AND 8000")"
check "index-only scan does not return rows expire already retired" "$IOS_AFTER" "0"

# NOT asserted here: that the visibility-map bits were CLEARED, as opposed to
# the consequence above. I tried and the arm was wrong -- pg_class.relallvisible
# is a statistic that VACUUM refreshes, and clearing a VM bit does not touch it,
# so the arm read "still 2 of 2" on a tree where the clear demonstrably works.
# Reading the fork itself needs pg_visibility, which is not built in this
# environment (no pg_visibility.control under the prefix's extension directory).
#
# So the clear is asserted through its consequence, with the premise above
# pinning the thing that was previously assumed: that VACUUM really did set bits
# for this table. That was the reviewer's ask, and it is what makes the
# index-only arm able to fail for the reason it names.

# ---- a negative retention must be refused, not applied ----------------------
#
# A negative ttl_interval puts the cutoff in the FUTURE, so expire finds
# `maximum < cutoff` true for groups entirely inside their retention and retires
# them: live rows dropped, which is the failure this suite is named for.
# SQLSTATE, not message text -- 22023 comes from the range check, while a
# missing function would be 42883 and a non-owner 42501.
ttl_state() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -v ON_ERROR_STOP=1 -Atq -v VERBOSITY=sqlstate -c "$1" 2>&1 \
		| sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\).*/\1/p' | head -1
}
psql_run "CREATE TABLE ttl_neg (id int, ts timestamptz) USING pgcolumnar;"
psql_run "INSERT INTO ttl_neg SELECT g, now() FROM generate_series(1,100) g;"
check "a negative ttl_interval is refused with 22023" \
	"$(ttl_state "SELECT pgcolumnar.set_options('ttl_neg', ttl_column => 'ts',
	                                            ttl_interval => '-3 days');")" "22023"
check "and a zero ttl_interval is refused too" \
	"$(ttl_state "SELECT pgcolumnar.set_options('ttl_neg', ttl_column => 'ts',
	                                            ttl_interval => '0 seconds');")" "22023"
# Control: without this pair, a guard that refused EVERY interval would look
# identical to one that refuses only the dangerous ones.
check "control: and a positive one is still accepted" \
	"$(ttl_state "SELECT pgcolumnar.set_options('ttl_neg', ttl_column => 'ts',
	                                            ttl_interval => '3 days');")" ""
check "control: and the rows are all still there" \
	"$(q 'SELECT count(*) FROM ttl_neg')" "100"

pgc_summary
