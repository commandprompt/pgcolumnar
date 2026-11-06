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
# Each side counted on its own (#1339): one count over the two-day window was met by
# rows on one side alone.
STRADDLE_ROWS="$(q "SELECT count(*) FILTER (WHERE ts >= ($CUTOFF) - make_interval(days => 1) AND ts < ($CUTOFF))
                    || '|' || count(*) FILTER (WHERE ts >= ($CUTOFF) AND ts < ($CUTOFF) + make_interval(days => 1))
                    FROM ttl_t")"
check "premise: rows exist on both sides of the cutoff within one day of it" \
	"$(case "$STRADDLE_ROWS" in 0\|*|*\|0|'') echo "NO STRADDLE ($STRADDLE_ROWS)" ;; *) echo straddled ;; esac)" "straddled"

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

# SQLSTATE, not message text: the refusals below each have their own state, while a
# missing function would be 42883 and a non-owner 42501. Empty when the call succeeds.
ttl_state() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -v ON_ERROR_STOP=1 -Atq -v VERBOSITY=sqlstate -c "$1" 2>&1 \
		| sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\).*/\1/p' | head -1
}

# ---- a table with no declared retention ------------------------------------
psql_run "CREATE TABLE ttl_none (id int, ts timestamptz) USING pgcolumnar;"
psql_run "INSERT INTO ttl_none SELECT g, now() FROM generate_series(1,10) g;"
# BY SQLSTATE, AS THE PYTEST TWIN ASSERTS (#1339). The message grep accepted any
# ERROR at all -- a missing function, a non-owner, an internal error -- so a refusal
# raised as the wrong kind of failure passed. 55000 is the prerequisite-state check.
_none_state="$(ttl_state "SELECT pgcolumnar.expire('ttl_none')")"
check "a table with no declared retention is an error, not a silent success" \
	"${_none_state:-ACCEPTED}" "55000"

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
# And THAT group holds an expired row (#1339): counting groups with NULLs alone was
# met by a group of nothing but NULLs. Rows are mapped to their group through
# row_tid() over the group's row numbers.
check "premise: and the SAME row group holds expired rows and NULL ones" \
	"$(q "WITH ng AS (SELECT DISTINCT z.storage_id, z.group_number
	                  FROM pgcolumnar.zone_map z JOIN pgcolumnar.storage s ON s.storage_id = z.storage_id
	                  WHERE s.relation_oid = 'ttl_null'::regclass AND z.null_count > 0)
	      SELECT (SELECT count(*) FROM ng) || '|' ||
	             (SELECT count(*) > 0 FROM ttl_null t WHERE t.ts < now() - interval '3 days'
	                AND t.ctid IN (SELECT pgcolumnar.row_tid(n) FROM pgcolumnar.row_group rg JOIN ng USING (storage_id, group_number),
	                                      generate_series(rg.first_row_number, rg.first_row_number + rg.row_count - 1) n))")" "1|true"
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
check "and VACUUM really did set visibility-map bits to clear" \
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

# ---- what expire refuses, with rows it could have dropped (#1341) ----------
#
# Each refused table holds 3,000 rows ten days old, every one past a one-day
# retention, so a missing refusal is not a quiet no-op: it is retired groups. The
# row count beside each SQLSTATE is what says nothing was dropped. ttl_ctl is the
# same fixture as ttl_own on its own table, so the owner's control cannot be
# reached by whatever a missing refusal did to ttl_own.
psql_run "CREATE ROLE ttl_nobody;
          GRANT USAGE ON SCHEMA pgcolumnar TO ttl_nobody;"
for _t in ttl_own ttl_ctl ttl_int ttl_gone; do
	_ty=timestamptz; [ "$_t" = ttl_int ] && _ty=int
	psql_run "CREATE TABLE $_t (id int, ts $_ty, ts2 timestamptz) USING pgcolumnar;
	          SELECT pgcolumnar.set_options('$_t', stripe_row_limit => 1000);"
done
psql_run "INSERT INTO ttl_own SELECT g, now() - interval '10 days', now() FROM generate_series(1,3000) g;
          INSERT INTO ttl_ctl SELECT g, now() - interval '10 days', now() FROM generate_series(1,3000) g;
          INSERT INTO ttl_int SELECT g, g - 1500, now() FROM generate_series(1,3000) g;
          INSERT INTO ttl_gone SELECT g, now() - interval '10 days', now() - interval '10 days' FROM generate_series(1,3000) g;"
_lay=""
for _t in ttl_ctl ttl_gone ttl_int ttl_own; do
	_lay="$_lay$(q "SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = pgcolumnar.get_storage_id('$_t');"):$(q "SELECT count(*) FROM $_t;") "
done
check "premise: each table holds 3000 rows in three row groups" "$_lay" "3:3000 3:3000 3:3000 3:3000 "

# Ownership: DELETE on the table is not enough, expire drops whole row groups.
psql_run "SELECT pgcolumnar.set_options('ttl_own', ttl_column => 'ts', ttl_interval => '1 day');
          SELECT pgcolumnar.set_options('ttl_ctl', ttl_column => 'ts', ttl_interval => '1 day');
          GRANT SELECT, DELETE ON ttl_own TO ttl_nobody;"
check "a role that does not own the table cannot expire it, even holding DELETE (42501)" \
	"$(ttl_state "SET ROLE ttl_nobody; SELECT pgcolumnar.expire('ttl_own');")|$(q 'SELECT count(*) FROM ttl_own')" "42501|3000"
check "control: and its owner's expire retires every aged group" \
	"$(q "SELECT pgcolumnar.expire('ttl_ctl');")|$(q 'SELECT count(*) FROM ttl_ctl')" "3|0"

# Type: set_options takes a column of any type, and expire is what refuses one
# that is not date, timestamp or timestamptz.
check "set_options accepts an int retention column, so the refusal is expire's" \
	"$(ttl_state "SELECT pgcolumnar.set_options('ttl_int', ttl_column => 'ts', ttl_interval => '1 day');")" ""
check "expire refuses a retention column that is not a time type (42804)" \
	"$(ttl_state "SELECT pgcolumnar.expire('ttl_int');")|$(q 'SELECT count(*) FROM ttl_int')" "42804|3000"

# A dropped retention column: the declaration outlives it, and expire refuses
# rather than reading some other column.
psql_run "SELECT pgcolumnar.set_options('ttl_gone', ttl_column => 'ts', ttl_interval => '1 day');
          ALTER TABLE ttl_gone DROP COLUMN ts;"
check "premise: the declaration still names the dropped column" \
	"$(q "SELECT ttl_column FROM pgcolumnar.options WHERE regclass = 'ttl_gone'::regclass;")" "ts"
check "expire refuses a retention column that no longer exists (42703)" \
	"$(ttl_state "SELECT pgcolumnar.expire('ttl_gone');")|$(q 'SELECT count(*) FROM ttl_gone')" "42703|3000"

# ---- half a declaration is no retention (#1341) -----------------------------
#
# A column with no interval names nothing to compare against, and an interval
# with no column has nothing to compare: either alone reads as "no retention", so
# a half declaration cannot drop rows. Every row here is ten days old, so a reader
# that filled in the missing half with a default would retire them all.
for _t in ttl_hcol ttl_hiv; do
	psql_run "CREATE TABLE $_t (id int, ts timestamptz) USING pgcolumnar;
	          SELECT pgcolumnar.set_options('$_t', stripe_row_limit => 1000);
	          INSERT INTO $_t SELECT g, now() - interval '10 days' FROM generate_series(1, 3000) g;"
done
# Checks, not premises (#1236; #1370 review): set_options refusing a half declaration
# is a change to this repository's SQL, and it turns both red while the expire arms
# below stay green for an unrelated reason, so they are what notices it.
check "set_options takes a retention column without an interval" \
	"$(ttl_state "SELECT pgcolumnar.set_options('ttl_hcol', ttl_column => 'ts');")|$(q "SELECT coalesce(ttl_column::text, '-') || '|' || coalesce(ttl_interval::text, '-') FROM pgcolumnar.options WHERE regclass = 'ttl_hcol'::regclass;")" "|ts|-"
check "and an interval without a column" \
	"$(ttl_state "SELECT pgcolumnar.set_options('ttl_hiv', ttl_interval => '1 day');")|$(q "SELECT coalesce(ttl_column::text, '-') || '|' || coalesce(ttl_interval::text, '-') FROM pgcolumnar.options WHERE regclass = 'ttl_hiv'::regclass;")" "|-|1 day"
check "a retention column without an interval is no retention: refused (55000), every row kept" \
	"$(ttl_state "SELECT pgcolumnar.expire('ttl_hcol');")|$(q 'SELECT count(*) FROM ttl_hcol')" "55000|3000"

# ---- a NULL that has been DELETED must stop pinning its group ---------------
#
# z->nullCount is recorded at WRITE time and never revised, so it still counts
# rows a later DELETE marked. Reading it as the live count keeps a group whose
# every live row is past retention and whose NULL rows have all been deleted --
# and keeps it FOREVER, because nothing rewrites a zone map on delete. That
# trades data loss for permanent over-retention, which is quieter and not
# better.
#
# Measured on both trees before the guard read a live-row property:
#
#   main 53224e4        expire 1, rows left 0     (right, but by dropping the
#                                                  live NULL rows in arm L too)
#   the null-count guard  expire 0, rows left 810 (REFUSED, and would never
#                                                  retire this group again)
#
# ONE INSERT statement, not two. Two statements flush two row groups, the NULLs
# never share a group with the rows under test, and every arm below answers a
# question nobody asked. The group count is asserted rather than assumed for
# exactly that reason.

psql_run "CREATE TABLE ttl_dn (id int, ts timestamptz, v text) USING pgcolumnar;"
psql_run "INSERT INTO ttl_dn
          SELECT g,
                 CASE WHEN g % 10 = 0 THEN NULL
                      ELSE now() - interval '400 days' END,
                 'v' || g
          FROM generate_series(1,900) g;"
psql_run "SELECT pgcolumnar.set_options('ttl_dn', ttl_column => 'ts',
                                        ttl_interval => '90 days');"

check "premise: the fixture is ONE row group, so the NULLs share it" \
	"$(q "SELECT count(*) FROM pgcolumnar.row_group rg
	      JOIN pgcolumnar.storage s ON s.storage_id = rg.storage_id
	      WHERE s.relation_oid = 'ttl_dn'::regclass")" "1"

# Delete every NULL row. The zone map is not rewritten, so nullCount still
# counts them -- which is the whole point.
psql_run "DELETE FROM ttl_dn WHERE ts IS NULL;"

check "premise: no LIVE row holds a NULL retention value any more" \
	"$(q 'SELECT count(*) FROM ttl_dn WHERE ts IS NULL')" "0"
check "but the zone map still records the deleted NULLs" \
	"$(q "SELECT CASE WHEN sum(z.null_count) > 0 THEN 'still recorded' ELSE 'gone' END
	      FROM pgcolumnar.zone_map z
	      JOIN pgcolumnar.storage s ON s.storage_id = z.storage_id
	      WHERE s.relation_oid = 'ttl_dn'::regclass")" "still recorded"
check "premise: and every live row is past the retention" \
	"$(q "SELECT count(*) FROM ttl_dn WHERE ts >= now() - interval '90 days'")" "0"

DN_RETIRED="$(q "SELECT pgcolumnar.expire('ttl_dn')")"
check "a group whose only NULLs have been deleted is retired" "$DN_RETIRED" "1"
check "and its rows are gone" "$(q 'SELECT count(*) FROM ttl_dn')" "0"

# Control: the live-NULL case must still be refused, or the fix above is just
# "retire everything" wearing a delete.
psql_run "CREATE TABLE ttl_dl (id int, ts timestamptz, v text) USING pgcolumnar;"
psql_run "INSERT INTO ttl_dl
          SELECT g,
                 CASE WHEN g % 10 = 0 THEN NULL
                      ELSE now() - interval '400 days' END,
                 'v' || g
          FROM generate_series(1,900) g;"
psql_run "SELECT pgcolumnar.set_options('ttl_dl', ttl_column => 'ts',
                                        ttl_interval => '90 days');"
# One row deleted, so the table HAS a delete vector and takes the same path as
# ttl_dn above -- but the NULLs are still live.
psql_run "DELETE FROM ttl_dl WHERE id = 1;"
DL_RETIRED="$(q "SELECT pgcolumnar.expire('ttl_dl')")"
check "control: a group whose NULLs are still live is NOT retired" "$DL_RETIRED" "0"
check "control: and those NULL rows survive" \
	"$(q 'SELECT count(*) FROM ttl_dl WHERE ts IS NULL')" "90"

# ---- a date retention column (#1135) ---------------------------------------
#
# A date partition key is an ordinary shape for the retention case this feature
# exists to serve, and `date` is not an unsupported type in this file: the same
# source handles DATEOID in its zone-map key and sortability paths. Only the
# retention cutoff refused it.
#
# THE CUTOFF MUST BE A DATE, and `date - interval` yields a TIMESTAMP in
# PostgreSQL, so it has to be truncated. Truncation KEEPS a row that is slightly
# older than the retention window rather than dropping one that is slightly
# younger, and for a function whose failure mode is deleting data that is the
# direction to err in. It is asserted below rather than left to the reader.
D_DAYS=5
psql_run "CREATE TABLE ttl_date (id int, d date, v text) USING pgcolumnar;"
psql_run "INSERT INTO ttl_date
          SELECT g,
                 (CURRENT_DATE - $D_DAYS) + ((g - 1) / 1000),
                 'v'||g
          FROM generate_series(1,5000) g;"
psql_run "ANALYZE ttl_date;"
psql_run "SELECT pgcolumnar.set_options('ttl_date', ttl_column => 'd',
                                        ttl_interval => '3 days');"

D_GROUPS_BEFORE="$(q "SELECT count(*) FROM pgcolumnar.storage s
                      JOIN pgcolumnar.row_group rg USING (storage_id)
                      WHERE s.relation_oid = 'ttl_date'::regclass")"
D_ROWS_BEFORE="$(q "SELECT count(*) FROM ttl_date")"
D_CUTOFF="SELECT (CURRENT_DATE - 3)"
D_EXPIRED="$(q "SELECT count(*) FROM ttl_date WHERE d < ($D_CUTOFF)")"
D_LIVE="$(q "SELECT count(*) FROM ttl_date WHERE d >= ($D_CUTOFF)")"
D_ONCUTOFF="$(q "SELECT count(*) FROM ttl_date WHERE d = ($D_CUTOFF)")"

check "premise: the date fixture is laid out in several row groups" \
	"$([ "${D_GROUPS_BEFORE:-0}" -ge 4 ] && echo "many ($D_GROUPS_BEFORE)" \
	   || echo "TOO FEW ($D_GROUPS_BEFORE)")" \
	"many ($D_GROUPS_BEFORE)"
check "premise: the date fixture has rows on both sides of the cutoff" \
	"$([ "${D_EXPIRED:-0}" -gt 0 ] && [ "${D_LIVE:-0}" -gt 0 ] && echo "both" \
	   || echo "expired $D_EXPIRED, live $D_LIVE")" \
	"both"
check "premise: and rows dated exactly ON the cutoff, which decide the rounding" \
	"$([ "${D_ONCUTOFF:-0}" -gt 0 ] && echo "some ($D_ONCUTOFF)" || echo "NONE")" \
	"some ($D_ONCUTOFF)"

D_RETIRED="$(q "SELECT pgcolumnar.expire('ttl_date')")"
echo "-- date: $D_ROWS_BEFORE rows in $D_GROUPS_BEFORE groups, $D_EXPIRED past retention, $D_ONCUTOFF on the cutoff"
echo "-- date: expire returned [$D_RETIRED]"

check "a date retention column is accepted, not refused (#1135)" \
	"$(case "$D_RETIRED" in ''|*ERROR*|*error*) echo "refused ($D_RETIRED)" ;; \
	                        *) echo accepted ;; esac)" \
	"accepted"

D_ROWS_AFTER="$(q "SELECT count(*) FROM ttl_date")"
D_LIVE_AFTER="$(q "SELECT count(*) FROM ttl_date WHERE d >= ($D_CUTOFF)")"

check "expire on a date column retires at least one group" \
	"$([ "${D_RETIRED:-0}" -gt 0 ] 2>/dev/null && echo retired \
	   || echo "RETIRED NOTHING ($D_RETIRED)")" \
	"retired"
check "NO row still inside the retention was dropped, on a date column" \
	"$D_LIVE_AFTER" "$D_LIVE"
check "and the date table really is smaller than it was" \
	"$([ "${D_ROWS_AFTER:-0}" -lt "${D_ROWS_BEFORE:-0}" ] && echo smaller \
	   || echo "UNCHANGED ($D_ROWS_AFTER of $D_ROWS_BEFORE)")" \
	"smaller"

# THE ROUNDING, PINNED. A row dated exactly on the cutoff day is inside the
# window by the `<` rule the timestamp arms use, and truncation must not move it
# outside. If the cutoff were rounded the other way this arm is the only one that
# would notice: every other arm above passes either way.
check "a row dated exactly on the cutoff is kept, so truncation errs toward keeping" \
	"$(q "SELECT count(*) FROM ttl_date WHERE d = ($D_CUTOFF)")" "$D_ONCUTOFF"

check "every remaining date row reads back its own value" \
	"$(q "SELECT count(*) FROM ttl_date WHERE v <> 'v'||id")" "0"

# ---- the cutoff is the SESSION's date, not the server's (#1135 review) -------
#
# `timestamptz_timestamp` converts through the session's TimeZone, so the cutoff
# a date column is compared against is the calling session's own date. Two
# sessions can therefore expire different sets. Measured, server now() at
# 2026-09-22 03:21+00:
#
#     session zone         local now              cutoff at '3 days'
#     UTC                  2026-09-22 03:21       2026-09-19
#     Pacific/Midway       2026-09-21 16:21       2026-09-18   <- a day earlier
#     Pacific/Kiritimati   2026-09-22 17:21       2026-09-19
#
# IT IS INHERITED, NOT INTRODUCED. timestamptz is zone-independent; the
# timestamp arm already converts through the session zone, because a zone-naive
# column forces a choice; date follows its sibling. What changes is the
# GRANULARITY -- hours for a timestamp, a whole day of rows for a date. Reported
# by @OffgridwithJD reviewing #1135.
#
# THE ARM PINS THE RULE, NOT A DIFFERENCE. Asserting that two zones disagree
# would be a coin toss on the time of day: at 23:00 UTC, Midway is 12:00 the SAME
# date and there is nothing to see. So each zone asserts the identity that holds
# in every zone at every instant -- the rows that survive are exactly the rows at
# or after THAT SESSION's own cutoff -- and the two cutoffs are printed so a
# reader can see they are free to differ.
qz() {  # qz ZONE SQL
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq \
		-c "SET TimeZone = '$1'; $2" 2>&1 | tail -1
}

# THE SECOND ZONE IS CHOSEN SO ITS DATE IS NOT UTC'S (#1339). Midway alone had
# UTC's date from 11:00 UTC on, and an expire that ignored the session zone passed
# there for half of every day. Midway is a day behind before 11:00 UTC, Kiritimati
# a day ahead from 10:00 UTC, so one of them always differs. The arms name the zone
# by what it is, not which it is, so their ledger keys do not move with the clock.
_other="$(q "SELECT CASE WHEN extract(hour FROM now() AT TIME ZONE 'UTC') < 11
                         THEN 'Pacific/Midway' ELSE 'Pacific/Kiritimati' END;")"
check "premise: the second zone's date is not UTC's right now" \
	"$(q "SELECT ((now() AT TIME ZONE '$_other')::date <> (now() AT TIME ZONE 'UTC')::date)::text;")" "true"
for _tz in UTC "$_other"; do
	_lbl="UTC"; [ "$_tz" = UTC ] || _lbl="a zone on another date"
	_t="ttl_tz_$(printf '%s' "$_lbl" | tr -c 'A-Za-z0-9' '_')"
	psql_run "DROP TABLE IF EXISTS $_t;
	          CREATE TABLE $_t (id int, d date, v text) USING pgcolumnar;"
	psql_run "INSERT INTO $_t
	          SELECT g, (CURRENT_DATE - 6) + ((g - 1) / 1000), 'v'||g
	          FROM generate_series(1,6000) g;"
	psql_run "SELECT pgcolumnar.set_options('$_t', ttl_column => 'd',
	                                        ttl_interval => '3 days');"

	# The cutoff THIS session computes, read in the same zone the expire runs in.
	_cut="$(qz "$_tz" "SELECT ((now() AT TIME ZONE '$_tz') - interval '3 days')::date")"
	_want="$(qz "$_tz" "SELECT count(*) FROM $_t WHERE d >= DATE '$_cut'")"
	_before="$(qz "$_tz" "SELECT count(*) FROM $_t")"
	_ret="$(qz "$_tz" "SELECT pgcolumnar.expire('$_t')")"
	_after="$(qz "$_tz" "SELECT count(*) FROM $_t")"
	_below="$(qz "$_tz" "SELECT count(*) FROM $_t WHERE d < DATE '$_cut'")"
	echo "-- tz $_tz: cutoff $_cut, $_before rows -> $_after, retired $_ret group(s)"

	check "premise: in $_lbl the fixture straddles that session's cutoff" \
		"$([ "${_want:-0}" -gt 0 ] && [ "${_want:-0}" -lt "${_before:-0}" ] && echo straddles \
		   || echo "want $_want of $_before")" \
		"straddles"

	# AND NO GROUP STRADDLES IT, which is what makes the identity below legal.
	# `expire` keeps a straddling group WHOLE -- the timestamp block above pins
	# exactly that -- so "survivors == rows at or after the cutoff" is STRONGER
	# than the contract and holds only on a geometry where every group sits
	# entirely on one side. That geometry comes from the stripe_row_limit=1000
	# baked into the cluster config at the top of this file, not from anything
	# here, so a change to that line must fail as a FIXTURE problem rather than
	# as a date defect. Asserted from counts, because the zone map stores its
	# bounds encoded: 6,000 rows inserted in date order, one date per 1,000, land
	# one date to a group exactly when the group count equals the date count and
	# every date holds the same 1,000 rows. Raised by @OffgridwithJD, who
	# measured a straddling group at the default 150,000 and a red arm with it.
	_gr="$(q "SELECT count(*) FROM pgcolumnar.storage s
	          JOIN pgcolumnar.row_group rg USING (storage_id)
	          WHERE s.relation_oid = '$_t'::regclass")"
	_days="$(q "SELECT count(DISTINCT d) FROM $_t")"
	_perday="$(q "SELECT DISTINCT count(*) FROM $_t GROUP BY d")"
	# NAMED FOR THE ZONE, like the two arms below it. Without the zone this arm
	# records two checks under one ledger key, one per loop iteration, and the
	# gate refuses that for exactly the reason the arm exists: a red in one zone
	# would be recorded against the other. The gate caught it on the first push,
	# on both majors, while all 256 suites reported PASS.
	check "premise: one date per row group in $_lbl, so no group straddles the cutoff" \
		"$([ "${_gr:-0}" = "${_days:-x}" ] && [ "${_perday:-0}" = "1000" ] && echo "one per group" \
		   || echo "$_gr groups, $_days dates, rows per date [$_perday]")" \
		"one per group"
	check "expire in $_lbl keeps exactly the rows at or after that session's cutoff" \
		"$_after" "$_want"
	check "and no row older than $_lbl's own cutoff survives" "$_below" "0"
done

# ---- a timestamp retention column: the cutoff is the session's LOCAL time (#1339) --
# A `timestamp` column has no zone, so expire computes its cutoff from the session's
# local now. Nothing tested that type at all. In Kiritimati, UTC+14, the local cutoff
# and a UTC one are fourteen hours apart. 6,000 rows 36 s apart are six groups of ten
# hours, starting 40 hours before the local cutoff: four groups lie wholly before it
# and go, the fifth holds it and stays. A cutoff taken in UTC would sit 26 hours in
# and retire two.
psql_run "CREATE TABLE ttl_lt (id int, ts timestamp, v text) USING pgcolumnar;"
qz Pacific/Kiritimati "INSERT INTO ttl_lt SELECT g, localtimestamp - interval '2 days' - interval '40 hours'
                         + (g - 1) * interval '36 s', 'v' || g FROM generate_series(1, 6000) g;" >/dev/null
psql_run "SELECT pgcolumnar.set_options('ttl_lt', ttl_column => 'ts', ttl_interval => '2 days');"
check "premise: the timestamp fixture is six groups of 1,000 rows" \
	"$(q "SELECT count(*) FROM pgcolumnar.row_group rg JOIN pgcolumnar.storage s USING (storage_id)
	      WHERE s.relation_oid = 'ttl_lt'::regclass;")" "6"
qz Pacific/Kiritimati "SELECT pgcolumnar.expire('ttl_lt')" >/dev/null
check "expire on a timestamp column retires the groups wholly before the session's local cutoff" \
	"$(q "SELECT count(*) || '|' || min(id) FROM ttl_lt;")" "2000|4001"

# ---- expire records what it retires, for rollups that keep history (#1294) ----
# An incremental rollup keeps its buckets when retention drops their raw rows (the
# owner's decision on #1294), so its refresh must tell a group RETIRED BY RETENTION
# from one deleted or rewritten. expire writes each group it retires to
# pgcolumnar.retired_group, in its own transaction; nothing else writes there.
psql_run "CREATE TABLE ttl_rec (ts timestamptz, v int) USING pgcolumnar;
	SELECT pgcolumnar.set_options('ttl_rec', ttl_column => 'ts', ttl_interval => '1 day');
	INSERT INTO ttl_rec SELECT now() - interval '30 days' + g * interval '1 second', g FROM generate_series(1, 3000) g;
	INSERT INTO ttl_rec SELECT now() + g * interval '1 second', g FROM generate_series(1, 1000) g;"
_sid="$(q "SELECT pgcolumnar.get_storage_id('ttl_rec');")"
_rgs() { q "SELECT string_agg(group_number::text, ',' ORDER BY group_number) FROM pgcolumnar.row_group WHERE storage_id = $_sid;"; }
_rec() { q "SELECT COALESCE(string_agg(group_number::text, ',' ORDER BY group_number), '') FROM pgcolumnar.retired_group WHERE storage_id = $_sid;"; }
_before="$(_rgs)"
check "premise: the record fixture has three aged-out row groups ahead of the current ones" "$_before" "1,2,3,4"

# Rolled back first: the record belongs to expire's transaction, so a retire that
# never happened leaves no trace a refresh could mistake for retention.
psql_run "BEGIN; SELECT pgcolumnar.expire('ttl_rec'); ROLLBACK;"
check "a rolled-back expire records nothing" "$(_rec)|$(_rgs)" "|1,2,3,4"

_n="$(q "SELECT pgcolumnar.expire('ttl_rec');")"
check_num "expire retired the three aged groups" "${_n:-0}" "3"
check "expire records exactly the groups it retired" "$(_rec)" "1,2,3"
check "and they are the groups gone from the row-group catalog" "$(_rgs)" "4"

# A compaction retires groups too, but that is a change to the data, which a rollup
# must recompute; recording it as retention would keep a wrong bucket forever.
psql_run "INSERT INTO ttl_rec SELECT now() + g * interval '1 second', g FROM generate_series(1001, 3000) g;
	DELETE FROM ttl_rec WHERE v % 10 < 6 AND ts > now();
	SELECT pgcolumnar.compact_rewrite('ttl_rec', 0.1);"
check "compact_rewrite retired a group" \
	"$([ "$(_rgs)" != "4,5,6" ] && echo retired || echo "nothing retired [$(_rgs)]")" "retired"
check "a compaction's retired groups are not recorded as retention" "$(_rec)" "1,2,3"

psql_run "DROP TABLE ttl_rec;"
check_num "dropping the table drops its retention record" \
	"$(q "SELECT count(*) FROM pgcolumnar.retired_group WHERE storage_id = $_sid;")" "0"

# ---- a no-op expire reads the extent, not every zone map (#1310) -------------
# Most of the daemon's passes retire nothing. When the retention column's extent
# (#1304 option 5) puts its minimum past the cutoff, no group can qualify, and
# expire answers 0 from one index lookup instead of one zone-map probe per group.
# Fifty groups, so a per-group read shows as fifty scans. The instrument is the
# one native_groupagg uses: the transaction's own scans of zone_map and its index,
# in a fresh connection, because the counters carry a backend's earlier reads.
psql_run "CREATE TABLE ttl_x (ts timestamptz, v int) USING pgcolumnar;
	SELECT pgcolumnar.set_options('ttl_x', ttl_column => 'ts', ttl_interval => '3650 days');
	INSERT INTO ttl_x SELECT now() - interval '1 day' + g * interval '1 second', g FROM generate_series(1, 50000) g;"
_xsid="$(q "SELECT pgcolumnar.get_storage_id('ttl_x');")"
check_num "premise: the no-op fixture has many row groups" \
	"$(q "SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = $_xsid;")" "50"
_expire_scans() {	# _expire_scans TABLE [SQL run first in the transaction] -> "n=<retired> scans=<zone-map scans>"
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -X -At -q \
		-c "BEGIN" ${2:+-c "$2"} \
		-c "SELECT 'n=' || pgcolumnar.expire('$1') || ' scans=' ||
		           (pg_stat_get_xact_numscans('pgcolumnar.zone_map'::regclass)
		            + pg_stat_get_xact_numscans('pgcolumnar.zone_map_pkey'::regclass))" \
		-c "ROLLBACK" 2>&1
}
check "a no-op expire reads no zone map when the extent's minimum is past the cutoff" \
	"$(_expire_scans ttl_x)" "n=0 scans=0"
check "without the extent the same expire probes every group's zone map" \
	"$(_expire_scans ttl_x "DELETE FROM pgcolumnar.column_extent WHERE storage_id = $_xsid")" "n=0 scans=50"

# The extent may say a group can qualify, and then expire decides group by group as
# before: the same groups with or without it.
psql_run "CREATE TABLE ttl_y (ts timestamptz, v int) USING pgcolumnar;
	SELECT pgcolumnar.set_options('ttl_y', ttl_column => 'ts', ttl_interval => '2 days');
	INSERT INTO ttl_y SELECT now() - interval '9 days' + g * interval '1 second', g FROM generate_series(1, 3000) g;
	INSERT INTO ttl_y SELECT now() + g * interval '1 second', g FROM generate_series(1, 2000) g;"
_ysid="$(q "SELECT pgcolumnar.get_storage_id('ttl_y');")"
_y_without="$(_expire_scans ttl_y "DELETE FROM pgcolumnar.column_extent WHERE storage_id = $_ysid" | sed 's/ scans=.*//')"
check "without the extent, expire retires the aged groups" "$_y_without" "n=3"
check "with the extent, expire retires the same groups" "$(_expire_scans ttl_y | sed 's/ scans=.*//')" "$_y_without"

# Old rows written earlier in expire's own transaction: their groups are visible to
# it before its extent row is written, so the early exit must count them.
psql_run "CREATE TABLE ttl_z (ts timestamptz, v int) USING pgcolumnar;
	SELECT pgcolumnar.set_options('ttl_z', ttl_column => 'ts', ttl_interval => '2 days');
	INSERT INTO ttl_z SELECT now() + g * interval '1 second', g FROM generate_series(1, 2000) g;"
check "expire in the transaction that wrote aged rows retires their groups" \
	"$(_expire_scans ttl_z "INSERT INTO ttl_z SELECT now() - interval '9 days' + g * interval '1 second', g FROM generate_series(1, 3000) g" | sed 's/ scans=.*//')" "n=3"

# A column left with no value at all: expire retires every group that held one,
# and the recompute it commits leaves a complete extent with no minimum. The NULL
# rows' groups are kept, and the next pass has nothing to read.
psql_run "CREATE TABLE ttl_n (ts timestamptz, v int) USING pgcolumnar;
	SELECT pgcolumnar.set_options('ttl_n', ttl_column => 'ts', ttl_interval => '2 days');
	INSERT INTO ttl_n SELECT now() - interval '9 days' + g * interval '1 second', g FROM generate_series(1, 2000) g;
	INSERT INTO ttl_n SELECT NULL, g FROM generate_series(1, 4000) g;"
check_num "expire retires the aged groups and keeps those of NULLs" \
	"$(q "SELECT pgcolumnar.expire('ttl_n');")" "2"
check "a later expire on a column of only NULLs reads no zone map" "$(_expire_scans ttl_n)" "n=0 scans=0"

# The interval-only half (#1341), LAST IN THE SUITE ON PURPOSE: with the reader's
# column test removed, expire on ttl_hiv crashed the backend (PG17), and a crash
# mid-suite takes the checks after it down with it, premises included.
check "and an interval without a column is no retention either" \
	"$(ttl_state "SELECT pgcolumnar.expire('ttl_hiv');")|$(q 'SELECT count(*) FROM ttl_hiv')" "55000|3000"

pgc_summary
