#!/usr/bin/env bash
#
# Columnar as a logical replication APPLY TARGET (#435).
#
# test/replication.sh covers physical replication and columnar as a publisher. This is the
# other direction, and it is the shape most likely to bring users: a heap OLTP table on the
# publisher, a columnar mirror on the subscriber for analytics.
#
# Two facts are pinned here, and the second is why this file exists:
#
#  1. An INSERT-only publication works end to end. Initial sync and streamed INSERTs use
#     COPY and table_tuple_insert, neither of which takes a tuple lock. Append-only
#     mirroring is a real capability and nothing said so before.
#
#  2. A publication that carries UPDATE raises OUR error, naming the cause and the way out.
#     It used to raise "columnar: row locking is not supported yet", which describes
#     something the user was not doing. Core takes a tuple lock for every applied UPDATE
#     and DELETE and has no lock-free path, and the apply worker deliberately does not
#     advance the origin on failure, so the subscription retries the same transaction
#     forever. The message is the only diagnostic the user gets.
#
# Usage:  test/logical_subscriber.sh [PG_CONFIG]
# Written fresh for pgColumnar.
set -uo pipefail
# The publisher needs logical decoding. Real newlines, not a literal \n: lib.sh appends
# PGC_EXTRA_CONF with printf '%s\n', so an escaped one lands as a single broken line.
PGC_EXTRA_CONF="${PGC_EXTRA_CONF:-}wal_level=logical
max_wal_senders=10
max_replication_slots=10
max_locks_per_transaction=256"
export PGC_EXTRA_CONF
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

check "premise: the publisher has logical decoding on" "$(q 'SHOW wal_level')" "logical"

# The subscriber needs a cluster and a port of its own. Same reasoning as
# replication.sh's picker: probe, then bind, and treat "no free port" as a visible SKIP
# rather than a silent pass.
SUB_DIR="$PGC_WORKDIR/subscriber"
SUB_LOG="$PGC_WORKDIR/subscriber.log"
pick_port() {
	local c
	for _ in $(seq 1 40); do
		c=$(( PGC_PORT_LO + RANDOM % (PGC_PORT_HI - PGC_PORT_LO) ))
		[ "$c" = "$PGC_PORT" ] && continue
		(exec 3<>"/dev/tcp/127.0.0.1/$c") 2>/dev/null || { echo "$c"; return; }
	done
	echo 0
}
SUB_PORT="$(pick_port)"
if [ "$SUB_PORT" = 0 ]; then
	check_skip "the logical subscriber round trip" "SKIP  no free port for the subscriber cluster" "no free port for the subscriber cluster"
	pgc_summary; exit 0
fi

pgc_pg "initdb -D '$SUB_DIR' -A trust" >/dev/null 2>&1
{ echo "port=$SUB_PORT"; echo "listen_addresses='127.0.0.1'"; echo "wal_level=logical";
  echo "max_locks_per_transaction=256";
  echo "shared_preload_libraries='pgcolumnar'"; } >> "$SUB_DIR/postgresql.conf" 2>/dev/null ||
  pgc_pg "printf 'port=%s\nlisten_addresses=%s\nwal_level=logical\nmax_locks_per_transaction=256\nshared_preload_libraries=%s\n' \
	'$SUB_PORT' \"'127.0.0.1'\" \"'pgcolumnar'\" >> '$SUB_DIR/postgresql.conf'"
pgc_pg "pg_ctl -D '$SUB_DIR' -l '$SUB_LOG' start -w" >/dev/null 2>&1
# Ends in pgc_teardown, which is not optional. pgc_setup installs
# `trap pgc_teardown EXIT`, and a second `trap ... EXIT` REPLACES it rather than
# adding to it, so this used to stop the subscriber and leave the publisher
# cluster running: measured at one orphaned postmaster per run, from a box with
# none. Orphans hold ports, the band is finite, and a suite that cannot get one
# fails after 8 start attempts with "could not create any TCP/IP sockets" -- a red
# that is indistinguishable from a real one and lands on whichever suite drew that
# port. replication.sh's sb_teardown has always chained this way (#470).
sub_cleanup() {
	pgc_pg "pg_ctl -D '$SUB_DIR' -m immediate stop" >/dev/null 2>&1 || true
	pgc_teardown
}
trap sub_cleanup EXIT

SUB() { env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$SUB_PORT" -U postgres \
	-d postgres -At -q -c "$1" 2>&1; }

check "premise: the subscriber cluster answers" \
	"$(SUB 'SELECT 1')" "1"
check "premise: the subscriber loaded pgcolumnar" \
	"$(SUB "CREATE EXTENSION IF NOT EXISTS pgcolumnar; SELECT count(*) FROM pg_extension WHERE extname='pgcolumnar'" | tail -1)" "1"

# ---- 1. the working pattern: publish inserts only ---------------------------
psql_run "CREATE TABLE src (id int PRIMARY KEY, v text);
	INSERT INTO src SELECT g, 'v'||g FROM generate_series(1,10) g;
	CREATE PUBLICATION p_ins FOR TABLE src WITH (publish = 'insert');" >/dev/null 2>&1
SUB "CREATE TABLE src (id int PRIMARY KEY, v text) USING pgcolumnar;" >/dev/null 2>&1
SUB "CREATE SUBSCRIPTION s_ins CONNECTION 'host=127.0.0.1 port=$PGC_PORT user=postgres dbname=$PGC_DB' PUBLICATION p_ins;" >/dev/null 2>&1
for _ in $(seq 1 30); do [ "$(SUB 'SELECT count(*) FROM src')" = "10" ] && break; sleep 1; done
check "insert-only: initial sync copied every row" "$(SUB 'SELECT count(*) FROM src')" "10"

psql_run "INSERT INTO src SELECT g, 'v'||g FROM generate_series(11,15) g;" >/dev/null 2>&1
for _ in $(seq 1 30); do [ "$(SUB 'SELECT count(*) FROM src')" = "15" ] && break; sleep 1; done
check "insert-only: streamed INSERTs applied" "$(SUB 'SELECT count(*) FROM src')" "15"

# An INSERT committed after the UPDATE: once it has arrived, the UPDATE before it in the
# stream has been processed. A fixed sleep proved nothing. With the subscription disabled
# before the UPDATE, or with streamed INSERTs failing, both checks below used to pass:
# nothing had been applied, so nothing had been applied wrongly.
psql_run "UPDATE src SET v = 'changed' WHERE id = 1;" >/dev/null 2>&1
psql_run "INSERT INTO src VALUES (16, 'after the update');" >/dev/null 2>&1
for _ in $(seq 1 30); do [ "$(SUB 'SELECT count(*) FROM src WHERE id = 16')" = "1" ] && break; sleep 1; done
SENTINEL="$(SUB 'SELECT count(*) FROM src WHERE id = 16')"
after_sentinel() {	# reading -> the reading, or "no reading" when the sentinel never arrived
	if [ "$SENTINEL" = "1" ]; then echo "$1"
	else echo "no reading (the INSERT after the UPDATE never arrived: [$SENTINEL]; reading [$1])"; fi
}
check "insert-only: the UPDATE is skipped by design, not applied" \
	"$(after_sentinel "$(SUB "SELECT v FROM src WHERE id = 1")")" "v1"
check "insert-only: no apply errors" \
	"$(after_sentinel "$(SUB "SELECT coalesce(sum(apply_error_count),0)::text FROM pg_stat_subscription_stats" | tail -1)")" "0"

# ---- 2. the wedge: a publication carrying UPDATE ----------------------------
# Asserted from the subscriber's log, because the failure happens in the apply worker and
# never reaches a client. Without this the check would be asserting nothing.
psql_run "CREATE TABLE upd (id int PRIMARY KEY, v text);
	INSERT INTO upd SELECT g, 'v'||g FROM generate_series(1,5) g;
	CREATE PUBLICATION p_all FOR TABLE upd;" >/dev/null 2>&1
SUB "CREATE TABLE upd (id int PRIMARY KEY, v text) USING pgcolumnar;" >/dev/null 2>&1
SUB "CREATE SUBSCRIPTION s_all CONNECTION 'host=127.0.0.1 port=$PGC_PORT user=postgres dbname=$PGC_DB' PUBLICATION p_all;" >/dev/null 2>&1
for _ in $(seq 1 30); do [ "$(SUB 'SELECT count(*) FROM upd')" = "5" ] && break; sleep 1; done
check "the second subscription synced before the UPDATE" \
	"$(SUB 'SELECT count(*) FROM upd')" "5"

psql_run "UPDATE upd SET v = 'changed' WHERE id = 1;" >/dev/null 2>&1
for _ in $(seq 1 25); do
	grep -q "logical replication cannot apply UPDATE" "$SUB_LOG" 2>/dev/null && break
	pgc_pg "grep -q 'logical replication cannot apply UPDATE' '$SUB_LOG'" >/dev/null 2>&1 && break
	sleep 1
done
log_has() { pgc_pg "grep -c '$1' '$SUB_LOG' 2>/dev/null || echo 0" | tail -1; }

check "the error names logical replication, not row locking" \
	"$([ "$(log_has 'logical replication cannot apply UPDATE or DELETE')" -ge 1 ] && echo yes || echo no)" "yes"
# Absence counts only once the replication error is there: with no UPDATE sent, an
# unreadable log, or a wedge that never happened, this used to pass.
check "the error does NOT say only 'row locking is not supported'" \
	"$(if [ "$(log_has 'logical replication cannot apply UPDATE or DELETE')" -lt 1 ]; then
		echo "no reading (the subscriber log holds no replication error)"
	   elif [ "$(log_has 'row locking is not supported')" = 0 ]; then echo yes; else echo no; fi)" "yes"
check "the hint names the insert-only publication" \
	"$([ "$(log_has 'publish = ')" -ge 1 ] && echo yes || echo no)" "yes"
# Read as "publisher -> subscriber": with no UPDATE sent, the subscriber's v1 used to pass.
check "the wedge is real: the row is still unchanged on the subscriber" \
	"$(q "SELECT v FROM upd WHERE id = 1") -> $(SUB "SELECT v FROM upd WHERE id = 1")" "changed -> v1"
# Unbounded is the whole point, so assert the SECOND attempt rather than a counter that
# may not have flushed. Core re-sends the same transaction about every 5 seconds.
for _ in $(seq 1 20); do
	[ "$(log_has 'logical replication cannot apply UPDATE or DELETE')" -ge 2 ] && break
	sleep 2
done
check "it retries rather than giving up, so the subscription is wedged" \
	"$([ "$(log_has 'logical replication cannot apply UPDATE or DELETE')" -ge 2 ] && echo yes || echo no)" "yes"

# ---- 3. the gate must not widen -------------------------------------------
# The replication message is selected by IsLogicalWorker(). Everything else that takes a
# row lock must keep the generic message, or a user running SELECT ... FOR UPDATE gets
# told about a subscription they do not have. This is the check that fails if someone
# later drops the gate.
# On a COLUMNAR table, which src on the publisher is not: it is the heap source. Asserting
# a columnar-specific message against a heap table is how the first version of this check
# passed nothing and failed for the wrong reason.
psql_run "CREATE TABLE lk (id int primary key, v text) USING pgcolumnar;
	INSERT INTO lk VALUES (1, 'a');" >/dev/null 2>&1
check "premise: the lock fixture really is columnar" \
	"$(q "SELECT amname FROM pg_class c JOIN pg_am a ON a.oid=c.relam WHERE c.relname='lk'")" "pgcolumnar"
# lk_said MODE -> the error an ordinary SELECT ... MODE raised. lk_replication MODE -> how
# often it mentions replication, but only when it raised the generic message: a statement
# that failed some other way (a missing table, a dead server) used to pass "is not told
# about replication" because nothing was said at all. Unrolled so each check's name is a
# literal the twin grader can read.
lk_said() { psql_run "SELECT * FROM lk WHERE id = 1 $1" 2>&1; }
lk_generic() { grep -c 'row locking is not supported' <<<"$(lk_said "$1")"; }
lk_replication() {
	local out; out="$(lk_said "$1")"
	if grep -q 'row locking is not supported' <<<"$out"; then grep -c 'logical replication cannot apply' <<<"$out"
	else echo "no reading (no generic error: $(head -1 <<<"$out"))"; fi
}
check "an ordinary SELECT ... FOR UPDATE keeps the generic message" "$(lk_generic 'FOR UPDATE')" "1"
check "an ordinary SELECT ... FOR UPDATE is not told about replication" "$(lk_replication 'FOR UPDATE')" "0"
check "an ordinary SELECT ... FOR SHARE keeps the generic message" "$(lk_generic 'FOR SHARE')" "1"
check "an ordinary SELECT ... FOR SHARE is not told about replication" "$(lk_replication 'FOR SHARE')" "0"
check "an ordinary SELECT ... FOR NO KEY UPDATE keeps the generic message" "$(lk_generic 'FOR NO KEY UPDATE')" "1"
check "an ordinary SELECT ... FOR NO KEY UPDATE is not told about replication" "$(lk_replication 'FOR NO KEY UPDATE')" "0"

SUB "DROP SUBSCRIPTION s_ins;" >/dev/null 2>&1
SUB "DROP SUBSCRIPTION s_all;" >/dev/null 2>&1
pgc_summary
