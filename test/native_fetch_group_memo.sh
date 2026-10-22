#!/usr/bin/env bash
#
# The by-row-number fetch resolves its row group from a memo, not a per-fetch
# catalog scan (#709).
#
# pgcolumnar_fetch_row runs once per fetched TID -- twice, in fact: a liveness
# settle and a deferred decode (#157) -- and each call read the ENTIRE row-group
# list out of the catalog (an index scan of pgcolumnar.row_group per call).
# K fetched rows cost ~2K catalog index scans; measured 6001 scans for 3000
# rows on the unfixed build. The memo keeps the list per (storage, command,
# snapshot content) and refreshes on a miss, which is what preserves the old
# per-fetch read's contract that a group flushed earlier in the same statement
# is visible.
#
# The instrument counts work done, not intent: pg_stat_get_xact_numscans on
# row_group_pkey, read as a BASELINE/AFTER DELTA inside the one session that
# runs the measured statement (the counter is a pending-stats entry; a fresh
# connection reads 0). The row_group TABLE's scan delta must stay 0 in the
# same window, or a broken index resolution would fake the bound green by
# degrading to heap scans. The plan shape is pinned from the same session,
# BEFORE the baseline, because planning itself reads the list once
# (relation_estimate_size).
#
# The savepoint arm is the removal proof for the subxact-abort reset: a
# group flushed under a savepoint must vanish from the memo at ROLLBACK TO
# SAVEPOINT, where nothing else changes -- not the cid, not the snapshot
# content. The same-statement rewrite arm is the removal proof for the
# retirement reset, and it fires on PG18: PG17's rewrite path happens to
# cross a CommandCounterIncrement that rebuilds the memo anyway, PG18's does
# not, and the matrix runs both. Poisoning manifests as all-NULL rows -- the
# group comes from the memo while the fresh column_chunk read finds
# nothing -- so both arms count ROWS, never values.
#
# Usage:  test/native_fetch_group_memo.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

q "CREATE EXTENSION IF NOT EXISTS pgcolumnar;" >/dev/null
# 100000 rows / 2000 = 50 groups, id unique and indexed, v reconstructible.
q "SET pgcolumnar.stripe_row_limit=2000;
   CREATE TABLE f (id int, v text) USING pgcolumnar;
   INSERT INTO f SELECT g, 'v'||g FROM generate_series(1,100000) g;
   CREATE INDEX f_id ON f (id);" >/dev/null

psql_c() { env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At -c "$1" 2>&1; }

# Survival reads the server log's count of backends killed by a signal as well as
# SELECT 1: SELECT 1 answers again once crash recovery has finished, so with a
# client backend SIGKILLed before it, "backend alive" passed on main (#1341).
crashes() {
	local _n
	_n="$(pgc_pg "grep -c 'terminated by signal' '$PGC_LOGFILE'" 2>/dev/null)"
	case "$_n" in '' | *[!0-9]*) ;; *) echo "$_n" ;; esac
}
survived() {	# survived BEFORE -> "1" when the server answers and no backend died since
	local _up _now
	_up="$(q 'SELECT 1')"; _now="$(crashes)"
	if ! [[ "$1" =~ ^[0-9]+$ && "$_now" =~ ^[0-9]+$ ]]; then echo "no (crash count unreadable)"
	elif [ "$_up" = 1 ] && [ "$_now" = "$1" ]; then echo 1
	else echo "no ([$_up], $(( _now - $1 )) backend crashes)"; fi
}
CRASHES_BEFORE="$(crashes)"

# Join-method forcing matters as much as scan forcing: with merge join
# available the planner answers the LATERAL with a Sort over a FULL index
# scan (every row fetched -- the measured delta once read 199840), which the
# naive "contains Index Scan" assertion happily matched. Nested loop is the
# per-probe fetch shape the arm claims to measure, so it is pinned below.
# (That merge-join shape also trips pre-existing #720 on PG17.)
# Every GUC here is load-bearing: seqscan/bitmapscan/custom_scan/penalty
# steer the plan-shape arm to a nested loop of index probes; mergejoin and
# hashjoin off keep it from the full-scan merge shape; parallel off keeps
# worker stats out of the xact-local counters. No enable_indexonlyscan: v is
# in no index, so an IOS is never a legal plan for these queries.
FORCE="SET enable_seqscan=off; SET enable_bitmapscan=off;
SET pgcolumnar.enable_custom_scan=off; SET pgcolumnar.enable_index_fetch_penalty=off;
SET max_parallel_workers_per_gather=0; SET enable_mergejoin=off; SET enable_hashjoin=off;"

FETCHQ="SELECT count(x.v) FROM generate_series(1,3000) g, LATERAL (SELECT v FROM f WHERE f.id = (g*97)%100000+1) x"

# One session, one implicit transaction: plan shape, baseline, the measured
# statement, after. The deltas are computed from the data, never retyped.
measured="$(psql_c "$FORCE
EXPLAIN (COSTS off) $FETCHQ;
SELECT 'BASE', pg_stat_get_xact_numscans('pgcolumnar.row_group_pkey'::regclass),
	   pg_stat_get_xact_numscans('pgcolumnar.row_group'::regclass);
$FETCHQ;
SELECT 'AFTER', pg_stat_get_xact_numscans('pgcolumnar.row_group_pkey'::regclass),
	   pg_stat_get_xact_numscans('pgcolumnar.row_group'::regclass);")"

check "the measured plan is a nested loop of index probes (fetch path engaged)" \
	"$(grep -c 'Nested Loop' <<<"$measured")/$(grep -c 'Index Scan using f_id' <<<"$measured")" \
	"1/1"

base_idx="$(awk -F'|' '$1=="BASE"{print $2}' <<<"$measured")"
after_idx="$(awk -F'|' '$1=="AFTER"{print $2}' <<<"$measured")"
base_seq="$(awk -F'|' '$1=="BASE"{print $3}' <<<"$measured")"
after_seq="$(awk -F'|' '$1=="AFTER"{print $3}' <<<"$measured")"

check "the fetch count is exact (3000 rows found)" \
	"$(grep -cx '3000' <<<"$measured")" "1"

# Post-fix contributors inside the window: the statement's own planning scan
# plus one memo build. 20 is auditable slack; the unfixed build reads ~6000.
check "3000 index fetches cost a bounded number of row_group reads (delta <= 20)" \
	"$( [ -n "$base_idx" ] && [ -n "$after_idx" ] && [ $((after_idx - base_idx)) -le 20 ] && echo bounded || echo "unbounded($base_idx -> $after_idx)")" \
	"bounded"

# Both counters must be READ: an empty pair used to subtract to 0 and pass, so with
# the measured session failing (the server stopped before it) this was the one
# check that stayed green.
check "row_group is never heap-scanned in the window (index resolution intact)" \
	"$( [[ "$base_seq" =~ ^[0-9]+$ && "$after_seq" =~ ^[0-9]+$ ]] && echo $((after_seq - base_seq)) || echo "no reading ($base_seq -> $after_seq)")" "0"

# --- correctness: scattered fetches return the right values -------------------
# The value comparison sits INSIDE the lateral so it filters on the fetched
# row directly (the outer-WHERE form flips to the merge-join shape of #720).
check "scattered index-fetched values are exact" \
	"$(psql_c "$FORCE SELECT count(*) FROM generate_series(1,2000) g,
		LATERAL (SELECT v FROM f WHERE f.id = g*50 AND f.v = 'v'||(g*50)) x;" | tail -1)" \
	"2000"

# --- correctness: a group flushed in an earlier statement of the same
# --- transaction is fetchable (new cid rebuilds the memo) ---------------------
check "same-transaction flush then fetch sees the new rows" \
	"$(psql_c "BEGIN;
		$FORCE
		SELECT count(x.v) FROM generate_series(1,50) g, LATERAL (SELECT v FROM f WHERE f.id = g) x;
		INSERT INTO f SELECT 200000+g, 'n'||g FROM generate_series(1,5000) g;
		SELECT count(x.v) FROM generate_series(1,50) g, LATERAL (SELECT v FROM f WHERE f.id = 200000+g) x;
		ROLLBACK;" | tail -2 | head -1)" \
	"50"

# --- hazard arm 1: savepoint flush, then ROLLBACK TO SAVEPOINT ----------------
# The cursor's index scan hands out TIDs of the aborted insert (their btree
# entries remain); the fetch must judge the aborted group invisible. Nothing
# but the subxact abort separates the poisoned memo from this drain: the cid
# and the portal snapshot are unchanged.
# MOVE 5 twice leaves 99990 of the 100000 real rows. The instrument is MOVE
# ALL's command tag -- a ROW count -- because the resurrected rows decode as
# all-NULL (the group metadata comes from the poisoned memo while the per-
# fetch column_chunk read correctly judges the aborted chunk rows invisible),
# so any value-based count is blind to them. That blindness cost this arm its
# first removal proof. The stripe limit is set IN THIS SESSION: the fixture's
# SET was session-local, and without it the savepoint's insert never flushes
# a group and the arm tests nothing.
sub_move="$(psql_c "SET pgcolumnar.stripe_row_limit=2000;
$FORCE
BEGIN;
DECLARE cur CURSOR FOR SELECT v FROM f WHERE id BETWEEN 1 AND 300000 ORDER BY id;
MOVE 5 FROM cur;
SAVEPOINT s;
INSERT INTO f SELECT 100000+g, 'a'||g FROM generate_series(1,3000) g;
MOVE 5 FROM cur;
ROLLBACK TO SAVEPOINT s;
MOVE ALL FROM cur;
COMMIT;" | grep '^MOVE' | tail -1)"

check "rows flushed under an aborted savepoint do not resurrect through the cursor" \
	"$sub_move" "MOVE 99990"

# --- hazard arm 2: compact_rewrite retires a group INSIDE one statement -------
# The removal proof for PgColumnarRetireGroup's memo reset -- on PG18.
# reclaim_coalesce=off removes the coalesce path's CommandCounterIncrement;
# on PG17 the flush's storage-row update happens to CCI anyway, which forces
# a key-mismatch rebuild and masks a missing reset, but on PG18 it does not,
# and without the reset the FIRST post-rewrite probe hits the stale memo
# through the old index entry (one doubled row, 501) before refresh-on-miss
# heals the slot. count(*), deliberately not count(v): the resurrected row
# decodes all-NULL (stale group metadata, fresh column_chunk read sees
# nothing), and a value count is blind to it.
q "SET pgcolumnar.stripe_row_limit=2000;
   CREATE TABLE r (id int, v text) USING pgcolumnar;
   INSERT INTO r SELECT g, 'r'||g FROM generate_series(1,10000) g;
   CREATE INDEX r_id ON r (id);
   DELETE FROM r WHERE id <= 1000;" >/dev/null

# The middle field asserts the rewrite REALLY rewrote one group (= 1, not the
# >= 0 tautology this arm first shipped with): a refused rewrite would leave
# nothing retired and the arm green with the hazard untested.
retire_out="$(psql_c "SET pgcolumnar.reclaim_coalesce=off;
$FORCE
SELECT (SELECT count(*) FROM generate_series(1001,1500) g, LATERAL (SELECT v FROM r WHERE r.id = g) x),
       (SELECT pgcolumnar.compact_rewrite('r'::regclass, 0.1, 0) = 1),
       (SELECT count(*) FROM generate_series(1001,1500) g, LATERAL (SELECT v FROM r WHERE r.id = g) x);" | tail -1)"

check "a same-statement retire neither loses nor doubles rows (500|t|500)" \
	"$retire_out" "500|t|500"

# --- hazard arm 3: pgcolumnar.compact() retires a FULLY deleted group ---------
# compact() is an ordinary SQL function, callable mid-statement, and reaches
# PgColumnarRetireGroup with NO flush at all -- so no incidental CCI on any
# major; only the memo reset stands between the post fetch and the retired
# group, whose delete vectors die with it (every one of its 2000 rows would
# resurrect as an all-NULL row). On assert builds an assert-only CCI after
# compact() masks a severed reset, so the removal proof for this arm fires on
# the matrix's non-assert majors.
q "SET pgcolumnar.stripe_row_limit=2000;
   CREATE TABLE fd (id int, v text) USING pgcolumnar;
   INSERT INTO fd SELECT g, 'f'||g FROM generate_series(1,10000) g;
   CREATE INDEX fd_id ON fd (id);
   DELETE FROM fd WHERE id <= 2000;" >/dev/null

compact_out="$(psql_c "SET pgcolumnar.reclaim_coalesce=off;
$FORCE
SELECT (SELECT count(*) FROM generate_series(2001,2010) g, LATERAL (SELECT v FROM fd WHERE fd.id = g) x),
       (SELECT pgcolumnar.compact('fd'::regclass) >= 1),
       (SELECT count(*) FROM generate_series(1,2000) g, LATERAL (SELECT v FROM fd WHERE fd.id = g) x);" | tail -1)"

check "a same-statement compact() of a fully deleted group resurrects nothing (10|t|0)" \
	"$compact_out" "10|t|0"

check "the table is exact after the in-statement rewrite" \
	"$(q 'SELECT count(*) FROM r;')" "9000"

# --- unchanged paths ----------------------------------------------------------
# SQLSTATE, not error text (house rule): 23505 comes only from the unique
# check; VERBOSITY=verbose makes psql print it.
check "a buffered + flushed duplicate still violates a unique index (23505)" \
	"$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At -v VERBOSITY=verbose -c "
		CREATE TABLE u (id int, v text) USING pgcolumnar;
		CREATE UNIQUE INDEX u_id ON u (id);
		INSERT INTO u SELECT g, 'u' FROM generate_series(1,5000) g;
		INSERT INTO u VALUES (4999, 'dup');" 2>&1 | grep -c 'ERROR:  23505')" \
	"1"

check "an AFTER UPDATE trigger still sees the OLD row (SnapshotAny re-fetch)" \
	"$(psql_c "CREATE TABLE trg (id int, v text) USING pgcolumnar;
		INSERT INTO trg SELECT g, 'o'||g FROM generate_series(1,3000) g;
		CREATE FUNCTION trg_f() RETURNS trigger LANGUAGE plpgsql AS
		  \$\$ BEGIN RAISE NOTICE 'old=%', OLD.v; RETURN NEW; END \$\$;
		CREATE TRIGGER t_au AFTER UPDATE ON trg FOR EACH ROW EXECUTE FUNCTION trg_f();
		UPDATE trg SET v = 'n' WHERE id = 2500;" | grep -c 'old=o2500')" \
	"1"

check "a DELETE is visible to the very next fetch (delete vectors uncached)" \
	"$(psql_c "$FORCE
		DELETE FROM f WHERE id = 77;
		SELECT count(x.v) FROM (VALUES (77)) t(p), LATERAL (SELECT v FROM f WHERE f.id = t.p) x;" | tail -1)" \
	"0"

# --- characterization: same-transaction TRUNCATE then fetch -------------------
# The third loss path (PgColumnarDeleteMetadata resets the memo; see the
# ledger). No removal proof is constructible on current majors -- TRUNCATE
# advances the command id, which rebuilds the memo anyway -- so this arm pins
# the behavior the reset guarantees independently of that cid accident: after
# a same-transaction TRUNCATE + reinsert, fetches see exactly the new rows.
check "same-transaction TRUNCATE then fetch sees only the new rows" \
	"$(psql_c "SET pgcolumnar.stripe_row_limit=2000;
	$FORCE
	BEGIN;
	CREATE TABLE tw (id int, v text) USING pgcolumnar;
	INSERT INTO tw SELECT g, 'o'||g FROM generate_series(1,5000) g;
	CREATE INDEX tw_id ON tw (id);
	SELECT count(x.v) FROM generate_series(1,50) g, LATERAL (SELECT v FROM tw WHERE tw.id = g) x;
	TRUNCATE tw;
	INSERT INTO tw SELECT g, 'w'||g FROM generate_series(1,100) g;
	SELECT count(*) FROM generate_series(1,5000) g, LATERAL (SELECT v FROM tw WHERE tw.id = g) x;
	SELECT count(x.v) FROM generate_series(1,5000) g, LATERAL (SELECT v FROM tw WHERE tw.id = g) x;
	COMMIT;" | grep -E '^[0-9]+$' | tr '\n' '/')" \
	"50/100/100/"

check "backend alive" "$(survived "$CRASHES_BEFORE")" "1"

pgc_summary
