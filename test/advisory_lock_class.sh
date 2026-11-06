#!/usr/bin/env bash
#
# pgColumnar's internal advisory locks must not be reachable from SQL (#430).
#
# locktag_field4 says which advisory lock space a tag belongs to, and PostgreSQL's
# own functions own exactly two values. From lockfuncs.c:
#
#     field4: 1 if using an int8 key, 2 if using 2 int4 keys
#
# We used both. The unique-key lock was SET_LOCKTAG_ADVISORY(db, indexOid, bucket, 2),
# which is bit for bit what pg_advisory_lock(indexOid, bucket) takes. So an
# application holding that tag blocked columnar inserts of that key, and columnar
# blocked the application, with nothing to point at but unexplained waiting.
#
# The lock is DISCOVERED from pg_locks rather than recomputed here. Reimplementing
# the bucket hash in the test would assert that two copies of our arithmetic agree,
# which is not the property. Reading the tag the running system actually took, then
# trying to grab that exact tag through the SQL function, is.
#
# It also avoids a trap the first version of this file walked into: "hold every
# bucket" needs one advisory lock per bucket against a max_locks_per_transaction
# of 64, so the holder failed, the check passed with nobody holding anything, and
# the suite reported the same result with and without the fix. That was found
# when lib.sh globally set unique_lock_buckets=100003; the global is gone (#799)
# and the shipped default is 128, but 128 still exceeds 64, so discovering the
# tag rather than enumerating buckets remains the right shape.
#
# Usage:  test/advisory_lock_class.sh [PG_CONFIG]
# Written fresh for pgColumnar.
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

PSQL_BG() {  # run SQL in a background session that stays open
	# STATEMENTS ARE FED ON STDIN, not through -c. With -c psql sends the whole
	# string as ONE simple-query message, so pg_stat_activity carries the entire
	# text from the first instant and cannot say which statement is running. The
	# set of locks an insert takes is complete only once the insert has RETURNED,
	# and "the session has reached its pg_sleep" is the signal that says so. On
	# stdin each statement is its own round trip, so the view moves with it.
	printf '%s\n' "$1" | env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 \
		-p "$PGC_PORT" -U postgres -d "$PGC_DB" -At >"$2" 2>&1 &
	echo $!
}

wait_for_sleeper() {  # -> yes once a background session has reached its pg_sleep
	for _i in $(seq 1 100); do
		if [ "$(q "SELECT count(*) FROM pg_stat_activity
		            WHERE pid <> pg_backend_pid() AND backend_type = 'client backend'
		              AND state = 'active' AND query LIKE 'SELECT pg_sleep%'")" != "0" ]; then
			echo yes; return
		fi
		sleep 0.2
	done
	echo "no (no session reached pg_sleep within 20s)"
}

psql_run "CREATE TABLE u (k int, v text) USING pgcolumnar;
          CREATE UNIQUE INDEX u_k ON u (k);
          INSERT INTO u SELECT g, 'v'||g FROM generate_series(1,100) g;" >/dev/null

check "premise: the lock is enabled, or nothing below proves anything" \
	"$(q "SHOW pgcolumnar.enable_unique_insert_lock")" "on"

# The lock under test is TAKEN at all. Nothing else here says so: every check below
# reads whatever locks an insert takes, and an insert also takes the storage-row lock
# (102), so with the unique-key lock never taken the premise below still saw a lock,
# the class check still read "unreachable", and the duplicate check still passed --
# the btree's own uniqueness check waits on the winner and rejects the loser without
# it (#1339). Read from the inserting session's own pg_locks, with the setting as the
# control rather than the class number, so a renumbered class still counts: the
# classes taken with the lock on that are not taken with it off. Both readings must
# hold a lock, since the storage-row lock is taken either way: with the off reading
# failing, an empty control made every class "extra" and this read "taken" (found by
# jdatcmd reviewing #1565).
lock_classes() {	# $1 = on|off -> the advisory classes one insert into u takes
	q "SET pgcolumnar.enable_unique_insert_lock = $1;
	   BEGIN;
	   INSERT INTO u VALUES (900003, 'classes');
	   SELECT coalesce(string_agg(DISTINCT objsubid::text, ',' ORDER BY objsubid::text), '')
	     FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid();
	   ROLLBACK;" | grep -vxE 'SET|BEGIN|INSERT 0 1|ROLLBACK'
}
cls_on="$(lock_classes on)"
cls_off="$(lock_classes off)"
cls_extra="$(awk -v on="$cls_on" -v off=",$cls_off," 'BEGIN {
	n = split(on, c, ","); for (i = 1; i <= n; i++) if (c[i] != "" && index(off, "," c[i] ",") == 0) x = x "," c[i]
	print substr(x, 2) }')"
echo "      advisory classes an insert takes: lock on [$cls_on], lock off [$cls_off]"
check "an insert takes the unique-key lock, and only while enable_unique_insert_lock is on" \
	"$(if [ -z "$cls_on" ]; then echo "no reading (no advisory lock with the setting on)"
	   elif [ -z "$cls_off" ]; then echo "no reading (no advisory lock with the setting off)"
	   elif [ -n "$cls_extra" ]; then echo taken
	   else echo "not taken (on [$cls_on], off [$cls_off])"; fi)" \
	"taken"

# ---------------------------------------------------------------------------
# 1. Discover the advisory lock an insert actually takes.
# ---------------------------------------------------------------------------
HOLD1="$PGC_WORKDIR/discover.out"
PID1=$(PSQL_BG "BEGIN; INSERT INTO u VALUES (900001, 'probe'); SELECT pg_sleep(30);" "$HOLD1")

# EVERY advisory lock the session holds, and only once the insert has RETURNED.
# An insert takes more than one -- PGCOLUMNAR_LOCKCLASS_STORAGE_ROW (102) as
# well as PGCOLUMNAR_LOCKCLASS_UNIQUE_KEY (103) -- so `ORDER BY objsubid DESC
# LIMIT 1` names the unique-key lock only while the unique-key lock is the
# highest-numbered one. That stops being true in exactly the case this suite
# exists to catch: put the unique-key class back to 2 and the maximum becomes
# 102, the STORAGE_ROW lock, which is still unreachable -- so the suite reported
# the property holding while the lock under test sat in the SQL space (#1154).
sleeper="$(wait_for_sleeper)"
LOCKS="$(q "SELECT classid || ' ' || objid || ' ' || objsubid
	              FROM pg_locks
	             WHERE locktype = 'advisory' AND granted
	               AND pid <> pg_backend_pid()
	             ORDER BY objsubid DESC")"
NLOCKS="$(printf '%s\n' "$LOCKS" | grep -c '[0-9]')"

check "premise: the insert took an advisory lock we can see" \
	"$([ "$NLOCKS" -ge 1 ] && echo yes || echo "no (pg_locks showed nothing; sleeper=$sleeper)")" "yes"
echo "      the locks it took: $(printf '%s' "$LOCKS" | tr '\n' ';')"

# The assertion, read straight off the tags and over the WHOLE set: NO lock an
# insert takes may be in a SQL-reachable class. 1 and 2 are the only values an
# application can produce (lockfuncs.c), so anything else is unreachable. The
# failing value names the offending tags, so a red says which lock moved.
sql_reachable=""
while read -r _c _o _f; do
	[ -z "${_f:-}" ] && continue
	case "$_f" in 1|2) sql_reachable="$sql_reachable ($_c,$_o,field4=$_f)" ;; esac
done <<<"$LOCKS"
check "the lock an insert takes is not in a SQL-reachable class" \
	"$(if [ "$NLOCKS" -eq 0 ]; then echo unknown
	   elif [ -n "$sql_reachable" ]; then echo "reachable:$sql_reachable"
	   else echo unreachable; fi)" \
	"unreachable"

# Terminate the BACKEND, not just psql. Killing the client leaves the server
# inside pg_sleep() holding its transaction, and every check below then blocks on
# our own lock rather than on the user's, in both arms, which is how the first
# version of this file reported the same result with and without the fix.
q "SELECT pg_terminate_backend(pid) FROM pg_stat_activity
    WHERE backend_type = 'client backend' AND pid <> pg_backend_pid()
      AND state IN ('idle in transaction', 'active')" >/dev/null
kill "$PID1" 2>/dev/null; wait "$PID1" 2>/dev/null
gone=no
for _i in $(seq 1 60); do
	if [ "$(q "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND pid<>pg_backend_pid()")" = "0" ]; then
		gone=yes; break
	fi
	sleep 0.2
done
check "premise: the discovering session is gone and holds nothing" "$gone" "yes"

# ---------------------------------------------------------------------------
# 2. A user taking that exact tag must not block the insert.
# ---------------------------------------------------------------------------
# pg_advisory_xact_lock(int4,int4) produces field4 = 2. Before the fix our lock
# was also field4 = 2, so this took the same tag and the insert waited forever.
#
# EVERY addressable tag is held, not the highest-numbered one, for the reason in
# section 1: the discovery FEEDS this arm, so contending for the wrong tag
# disables this check as well -- two arms moving together with the defect, both
# still looking like evidence (#1154).
ADDR=""
NADDR=0
while read -r _c _o _f; do
	[ -z "${_f:-}" ] && continue
	if [ "$_c" -ge -2147483648 ] && [ "$_c" -le 2147483647 ] &&
	   [ "$_o" -ge -2147483648 ] && [ "$_o" -le 2147483647 ]; then
		ADDR="$ADDR$_c $_o
"
		NADDR=$((NADDR + 1))
	fi
done <<<"$LOCKS"

if [ "$NADDR" -gt 0 ]; then
	grab="BEGIN;"
	cond=""
	while read -r _c _o; do
		[ -z "${_o:-}" ] && continue
		grab="$grab
SELECT pg_advisory_xact_lock($_c::int, $_o::int);"
		cond="$cond OR (classid = $_c AND objid = $_o)"
	done <<<"$ADDR"
	grab="$grab
SELECT pg_sleep(30);"
	cond="${cond# OR }"

	HOLD2="$PGC_WORKDIR/holder.out"
	PID2=$(PSQL_BG "$grab" "$HOLD2")
	sleeper2="$(wait_for_sleeper)"
	held=$(q "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND granted
	            AND objsubid = 2 AND ($cond)")
	check "premise: the other session really holds that exact tag in class 2" \
		"$([ "${held:-0}" -ge "$NADDR" ] && echo yes || echo "no (holds ${held:-0} of $NADDR; sleeper=$sleeper2)")" \
		"yes"

	ins=$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At \
		-c "SET statement_timeout = '10s';" -c "INSERT INTO u VALUES (900001, 'new');" 2>&1)
	case "$ins" in
		*timeout*|*canceling*) verdict="BLOCKED by the user lock" ;;
		*ERROR*)               verdict="ERROR: $(head -1 <<<"$ins")" ;;
		*)                     verdict=ok ;;
	esac
	check "a user advisory lock on that tag does not block a columnar insert" "$verdict" "ok"

	q "SELECT pg_terminate_backend(pid) FROM pg_stat_activity
	    WHERE backend_type = 'client backend' AND pid <> pg_backend_pid()
	      AND state IN ('idle in transaction', 'active')" >/dev/null
	kill "$PID2" 2>/dev/null; wait "$PID2" 2>/dev/null
	for _i in $(seq 1 60); do
		[ "$(q "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND pid<>pg_backend_pid()")" = "0" ] && break
		sleep 0.2
	done
else
	check_skip "the SQL form of the advisory lock" "SKIP  none of $(printf '%s' "$LOCKS" | tr '\n' ';') fits in two int4s, so the SQL form cannot address it" "classid or objid exceeds int4"
fi

# ---------------------------------------------------------------------------
# 3. The internal lock still does its job.
# ---------------------------------------------------------------------------
# This asserts that a duplicate is rejected, NOT that the lock is what rejects it.
# The lock's presence is the "an insert takes the unique-key lock" check above. With
# the lock never taken, a duplicate is still rejected: measured on PG17, even a
# CONCURRENT duplicate waits on the first insert and gets 23505, because the btree's
# uniqueness check waits on the winner (#1339).
#
# IT PLANTS ITS OWN KEY. This arm used to re-insert 900001 and rely on the
# contention arm above having inserted it successfully -- so under a mutation
# that BLOCKS that insert, the key was never there and "a duplicate key is still
# rejected" went red for a reason that has nothing to do with duplicate keys.
# Measured: the #1154 mutation reddened three arms, of which the third was this
# cascade. An arm that depends on an earlier arm having passed reports on the
# earlier arm.
psql_run "INSERT INTO u VALUES (900002, 'first');" >/dev/null 2>&1
dup=$(psql_run "INSERT INTO u VALUES (900002, 'dup');" 2>&1)
check "a duplicate key is still rejected" \
	"$(grep -qiE 'duplicate key|unique constraint' <<<"$dup" && echo rejected || echo "NOT rejected: $(head -1 <<<"$dup")")" \
	"rejected"

# ---- the row-identity lock (class 104) and its two settings (#1341) ----------------
#
# An UPDATE or DELETE serialises on the row it changes through an advisory lock in
# class 104 (PGCOLUMNAR_LOCKCLASS_ROW_IDENTITY). pgcolumnar.enable_row_update_lock
# turns that off for a session; pgcolumnar.row_lock_buckets is part of the lock tag,
# so it is fixed at server start. Neither was set or read by any test.
psql_run "CREATE TABLE rl (id int, v int) USING pgcolumnar;"
psql_run "INSERT INTO rl SELECT g, g FROM generate_series(1, 100) g;"
# row_locks_while_updating SETTING -> "<class-104 locks>|<holder past its UPDATE>", read
# in one query once the holder's UPDATE has FINISHED. WAITED FOR, NOT SLEPT ON (#1368
# review), and waited for on the right event: the holder names itself in
# application_name after its UPDATE returns, and the poll waits for that name. Waiting
# for its RowExclusiveLock on rl was not enough, because the UPDATE takes that at
# executor start, before it reaches row 7 and takes the class-104 lock; a poll landing
# between the two read "none|1" on CI (main f601281, PG 17 and PG 18). The CTE sleep
# ahead of the row keeps that window open on every run, so waiting on the early event
# fails every time rather than now and then.
row_locks_while_updating() {
	local tag="rl_updated_$1"
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq \
		-c "BEGIN; SET LOCAL pgcolumnar.enable_row_update_lock = $1; WITH d AS MATERIALIZED (SELECT pg_sleep(1)) UPDATE rl SET v = v + 1 FROM d WHERE id = 7; SET application_name = '$tag'; SELECT pg_sleep(3); COMMIT;" \
		>/dev/null 2>&1 &
	local holder=$! got="" i
	for i in $(seq 1 100); do
		got="$(q "SELECT (SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objsubid = 104
		                    AND pid <> pg_backend_pid()) || '|' ||
		                 (SELECT count(*) FROM pg_stat_activity WHERE application_name = '$tag')")"
		[ "${got#*|}" = 1 ] && break
		sleep 0.1
	done
	echo "$got"
	wait "$holder"
}
_rl_on="$(row_locks_while_updating on)"
check "an UPDATE holds a row-identity lock (class 104) while enable_row_update_lock is on" \
	"$([ "${_rl_on%|*}" -ge 1 ] 2>/dev/null && echo "held|${_rl_on#*|}" || echo "none|${_rl_on#*|}")" "held|1"
check "and holds none with enable_row_update_lock off" "$(row_locks_while_updating off)" "0|1"
check "row_lock_buckets is fixed at server start, so a session cannot change it (55P02)" \
	"$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At -v VERBOSITY=sqlstate -c "SET pgcolumnar.row_lock_buckets = 7;" 2>&1 | sed -n 's/^.*ERROR:[[:space:]]*\([0-9A-Z]\{5\}\).*$/\1/p')|$(q "SELECT context || '|' || setting FROM pg_settings WHERE name = 'pgcolumnar.row_lock_buckets'")" \
	"55P02|postmaster|1024"

# The documented bound (docs/configuration.md): row_lock_buckets "bounds the row locks
# a transaction holds, so a bulk update cannot exhaust the lock table". One transaction
# updates 200000 rows and then counts its own class-104 locks (one -c string is one
# transaction): one per bucket, not one per row. A row lock per row would not fit the
# default lock table at all.
psql_run "CREATE TABLE rlb (id int, v int) USING pgcolumnar;
          INSERT INTO rlb SELECT g, g FROM generate_series(1, 200000) g;" >/dev/null
check "a bulk UPDATE of 200000 rows holds one row-identity lock per bucket, row_lock_buckets of them" \
	"$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At \
		-c "UPDATE rlb SET v = v + 1;
		    SELECT count(*) || '|' || current_setting('pgcolumnar.row_lock_buckets') FROM pg_locks
		     WHERE pid = pg_backend_pid() AND locktype = 'advisory' AND objsubid = 104;" 2>&1 |
		grep -E '^[0-9]+[|]|ERROR' | tail -1)" "1024|1024"
check "and the UPDATE committed every row" "$(q "SELECT count(*) || '|' || sum(v - id) FROM rlb")" "200000|200000"

# ---- the row-identity lock cannot be reached from SQL (#1341 item 56) ------------
# The INSERT arms above ask this of the unique-key and storage-row locks; nothing
# asked it of class 104. An UPDATE is held open and every advisory lock it took is
# read. None may be in field 1 or 2, the only classes SQL can produce. Then a second
# session takes each of those exact tags with pg_advisory_xact_lock(int4, int4), and
# a columnar UPDATE of the same row must not wait on it. An oid above the int4 range
# is addressed by its negative int4, the same 32 bits lockfuncs.c puts in the tag.
rl_wait_for() {	# rl_wait_for APPLICATION_NAME -> its pid, once it has named itself
	local i pid=""
	for i in $(seq 1 100); do
		pid="$(q "SELECT pid FROM pg_stat_activity WHERE application_name = '$1'")"
		[ -n "$pid" ] && break
		sleep 0.1
	done
	echo "$pid"
}
rl_end() {	# rl_end PID BGPID: end a holder's backend and its psql
	q "SELECT pg_terminate_backend($1)" >/dev/null 2>&1
	kill "$2" 2>/dev/null; wait "$2" 2>/dev/null
	local i
	for i in $(seq 1 60); do
		[ "$(q "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid <> pg_backend_pid()")" = 0 ] && break
		sleep 0.2
	done
}
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq \
	-c "BEGIN; UPDATE rl SET v = v + 1 WHERE id = 7; SET application_name = 'rl_tagger'; SELECT pg_sleep(30); COMMIT;" \
	>/dev/null 2>&1 &
rl_bg=$!
rl_pid="$(rl_wait_for rl_tagger)"
RL_TAGS="$(q "SELECT CASE WHEN classid::bigint > 2147483647 THEN classid::bigint - 4294967296 ELSE classid::bigint END
	              || ' ' || CASE WHEN objid::bigint > 2147483647 THEN objid::bigint - 4294967296 ELSE objid::bigint END
	              || ' ' || objsubid
	         FROM pg_locks WHERE locktype = 'advisory' AND granted AND pid = ${rl_pid:-0}")"
rl_end "${rl_pid:-0}" "$rl_bg"
RL_N="$(printf '%s\n' "$RL_TAGS" | grep -c '[0-9]')"
rl_reach=""
while read -r _c _o _f; do
	case "${_f:-}" in 1|2) rl_reach="$rl_reach ($_c,$_o,field4=$_f)" ;; esac
done <<<"$RL_TAGS"
check "the locks an UPDATE takes are not in a SQL-reachable class" \
	"$(if [ "$RL_N" -eq 0 ]; then echo "unknown (no advisory lock seen; holder pid ${rl_pid:-none})"
	   elif [ -n "$rl_reach" ]; then echo "reachable:$rl_reach"
	   else echo unreachable; fi)" "unreachable"
rl_grab="BEGIN;"
while read -r _c _o _f; do
	[ -z "${_f:-}" ] && continue
	rl_grab="$rl_grab SELECT pg_advisory_xact_lock($_c::int, $_o::int);"
done <<<"$RL_TAGS"
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq \
	-c "$rl_grab SET application_name = 'rl_user'; SELECT pg_sleep(30); COMMIT;" >/dev/null 2>&1 &
rl_bg=$!
rl_pid="$(rl_wait_for rl_user)"
check "premise: the other session holds every one of those tags through the SQL functions" \
	"$(q "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted AND objsubid = 2
	         AND pid = ${rl_pid:-0}")" "$RL_N"
rl_upd="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At \
	-c "SET statement_timeout = '10s';" -c "UPDATE rl SET v = v + 1 WHERE id = 7;" 2>&1)"
case "$rl_upd" in
	*timeout*|*canceling*) rl_verdict="BLOCKED by the user lock" ;;
	*ERROR*)               rl_verdict="ERROR: $(head -1 <<<"$rl_upd")" ;;
	*)                     rl_verdict=ok ;;
esac
check "a user advisory lock on those tags does not block a columnar UPDATE" "$rl_verdict" "ok"
rl_end "${rl_pid:-0}" "$rl_bg"

# ---- bulk_parallel_writer set by hand still serialises a first write (#1341 item 55) --
# The setting lets parallel_copy's loaders skip the storage-row creation lock (class
# 102). The reference says setting it by hand is safe, because the skip fires only
# once the storage row is committed. Two sessions with it on each make the first
# write to a committed, empty table: the second must wait for the first and then
# land its row. A skip that did not wait for the row to be committed lets both
# create it, and the second fails on storage_pkey.
#
# The first writer commits only once the suite has looked for the second one waiting
# and then inserted a committed flag row, not after a fixed sleep: a slow second
# writer would otherwise start after the first had committed, and the premise below
# would fail on timing (OffgridwithJD, review of #1415). The pytest twin does the same.
psql_run "CREATE TABLE bw (id int) USING pgcolumnar; CREATE TABLE bw_go (go int) USING heap;"
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq \
	-c "SET statement_timeout = '60s'; BEGIN; SET LOCAL pgcolumnar.bulk_parallel_writer = on; INSERT INTO bw VALUES (1); SET application_name = 'bw_first';
	    DO \$\$ BEGIN WHILE NOT EXISTS (SELECT 1 FROM bw_go) LOOP PERFORM pg_sleep(0.1); END LOOP; END \$\$; COMMIT;" \
	>/dev/null 2>&1 &
bw_bg=$!
bw_pid="$(rl_wait_for bw_first)"
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At \
	-c "SET pgcolumnar.bulk_parallel_writer = on; SET statement_timeout = '30s';" -c "INSERT INTO bw VALUES (2);" \
	>"$PGC_WORKDIR/bw_second.out" 2>&1 &
bw_bg2=$!
# The second write must be waiting on the first before the first commits, or the arm
# measures two writes in sequence.
bw_waited=no
for _i in $(seq 1 100); do
	[ "$(q "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND query LIKE 'INSERT INTO bw%'")" -ge 1 ] && { bw_waited=yes; break; }
	sleep 0.1
done
psql_run "INSERT INTO bw_go VALUES (1);"	# release the first writer, whatever the poll saw
check "premise: the second first write waited on the first" "$bw_waited" "yes"
wait "$bw_bg2"; wait "$bw_bg"
bw_out="$(cat "$PGC_WORKDIR/bw_second.out")"
case "$bw_out" in
	*ERROR*) bw_verdict="ERROR: $(grep -m1 ERROR <<<"$bw_out")" ;;
	*)       bw_verdict=ok ;;
esac
check "with bulk_parallel_writer set by hand, a second first write waits for the first and both rows land" \
	"$([ -n "$bw_pid" ] && echo held || echo "no holder")|$bw_verdict|$(q "SELECT count(*) FROM bw")" "held|ok|2"

pgc_summary
