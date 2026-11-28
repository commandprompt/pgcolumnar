#!/usr/bin/env bash
#
# pgColumnar: parallel_flush stays OPT-IN, and the metric a default-on gate would
# need cannot separate the shapes (#445).
#
# #445 measured that pgcolumnar.parallel_flush helps one narrow shape -- a large
# flush of many cheap numeric columns -- and regresses the common cases (frequent
# small flushes, wide text, cheap fixed-width). A gate to turn it on by default
# was designed and REFUTED: the metric computable at dispatch time, over the
# buffered .len fields (natts, buffered bytes, bytes per value), does not carry
# per-column encode CPU, which is the variable that decides the outcome. Two
# shapes with byte-identical metrics have opposite best, so no threshold over the
# metric can gate them.
#
# This suite pins the decision, not a gate. It asserts three things:
#  1. the default is OFF, and a default flush dispatches serial;
#  2. opting in is byte-identical to the serial path (and actually dispatches
#     parallel, so the identity is not a silent fall-back);
#  3. the refuted metric COLLIDES: a random and a constant column, opposite in
#     encode cost, produce a byte-identical dispatch metric -- so the metric
#     cannot tell apart shapes whose real best differs.
#
# It is GREEN on current source. Its value is the removal proofs, marked below:
# flip the boot default, or mutate the parallel assembly, and the named arm goes
# RED. The collision arm can only go RED if someone adds a distinguishing term to
# the metric -- which is exactly the signal that a real gate has become possible.
#
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# The ON arms need worker slots; a bulk flush registers min(natts, <=8) workers.
# mmap puts every dynamic shared memory segment in this cluster's own pg_dynshmem,
# where arm 4 can count them from SQL (#1745); posix would put them in /dev/shm,
# shared with every other cluster on the machine.
psql_run "ALTER SYSTEM SET max_worker_processes = 16;"
psql_run "ALTER SYSTEM SET dynamic_shared_memory_type = 'mmap';"
# Through pgc_pg, which drops to postgres when this runs as root (CI's suites job
# does): a bare pg_ctl refuses root, and the refusal used to be swallowed, so the
# server kept posix segments and arm 4 counted 0 whatever the code did (#1746
# review). A restart that fails is said out loud; arm 4's premise then fails on it.
pgc_pg "pg_ctl -D '$PGC_PGDATA' -l '$PGC_LOGFILE' restart -w -o '-p $PGC_PORT'" >/dev/null 2>&1 \
	|| echo "-- RESTART FAILED: dynamic_shared_memory_type = mmap is not in effect"
sleep 1

q() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -Atqc "$1" 2>/dev/null
}

# The dispatch line the flush emits once per stripe at DEBUG1. gucSql runs in the
# same session before the insert, so a SET reaches the flush. Returns the tail of
# the line: "rows=.. natts=.. bufbytes=.. valbytes=.. valcount=.. -> serial".
#
# The whole session's output is kept in $FLUSHLOG for split() below. dispatch runs
# in a command substitution, so a variable set here would not survive it.
FLUSHLOG="$PGC_WORKDIR/flush.log"
dispatch() { # dispatch <gucSql> <insertSql>
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -c "SET client_min_messages=debug1;" -c "$1" -c "$2" >"$FLUSHLOG" 2>&1
	grep -oE 'parallel_flush dispatch: .*-> (parallel|serial)' "$FLUSHLOG" | tail -1 |
		sed 's/^.*dispatch: //'
}

# How the last dispatch()'s parallel flushes were really split. The dispatch line
# prints "-> parallel" BEFORE any worker is registered, so with no worker ever
# registered every column fell back to serial and the dispatch line still said
# parallel: main's shell passed that. The split line counts the columns workers
# produced. -> "workers" when every split line has at least one worker column,
# else what was read.
split() {
	local lines
	lines="$(grep -oE 'parallel_flush: [0-9]+ of [0-9]+ columns by [0-9]+ worker' "$FLUSHLOG")"
	if [ -z "$lines" ]; then
		echo "no split line"
	elif grep -qE 'parallel_flush: 0 of ' <<<"$lines"; then
		echo "serial fall-back:[$(tr '\n' ';' <<<"$lines")]"
	else
		echo workers
	fi
}

# LSN-independent storage fingerprint over the stored chunk metadata, and the
# rows read back. The rows catch bytes that change without changing a length.
fp() { # fp <table>
	q "SELECT (SELECT md5(string_agg(group_number || ':' || column_index || ':' || value_count || ':' || encode(encoding_descriptor, 'hex') || ':' || block_codec || ':' || page_length, '|' ORDER BY group_number, column_index)) FROM pgcolumnar.column_chunk WHERE storage_id = pgcolumnar.get_storage_id('$1'))
	   || '/' || (SELECT md5(string_agg(t::text, '|' ORDER BY t.a)) FROM $1 t)"
}

# Eight columns of eight different shapes, so each column's chunk differs from its
# neighbour's. With eight copies of g, a parallel assembly that stored each worker's
# column under the next index wrote the same bytes, and main's shell passed it.
WNUM="SELECT g, g % 7, ((g::bigint * 7919) % 65537)::int, g / 10, -g, g % 2, 0, (g * 31) % 1000 FROM generate_series(1,300000) g"

# ---- 1. the default is OFF, and stays that way -----------------------------

# Not a premise (#1339, dated 2026-10-21): the boot default is pgcolumnar's own,
# and setting it to true reddens this.
check_text "parallel_flush is off by default" \
	"$(q "SHOW pgcolumnar.parallel_flush")" "off"

q "CREATE TABLE wnum (a int, b int, c int, d int, e int, f int, g int, h int) USING pgcolumnar;" >/dev/null

# DEFAULT-OFF PIN. A fresh flush with no SET must dispatch SERIAL.
# REMOVAL PROOF: set the boot default to true in columnar_tableam.c (the
# DefineCustomBoolVariable for pgcolumnar.parallel_flush) -> this line logs
# "-> parallel" and the check goes RED. That red proves the default, not
# incidental structure, keeps the machine off.
d_default="$(dispatch "SELECT 1;" "INSERT INTO wnum $WNUM;")"
check_text "a default flush dispatches serial" \
	"$([ "${d_default##*-> }" = "serial" ] && echo serial || echo "parallel:[$d_default]")" \
	"serial"

# ---- 2. opting in is byte-identical to serial ------------------------------

q "TRUNCATE wnum;" >/dev/null
d_on="$(dispatch "SET pgcolumnar.parallel_flush=on;" "INSERT INTO wnum $WNUM;")"
# The identity below is vacuous if ON silently fell back to serial, so assert the
# wide-numeric flush really took the worker path.
# Both lines: the dispatch decision, and workers producing columns.
s_on="$(split)"
check_text "with the GUC on, a wide-numeric flush dispatches parallel (not a silent serial fall-back)" \
	"$(if [ "${d_on##*-> }" != "parallel" ]; then echo "serial:[$d_on]"
	   elif [ "$s_on" != workers ]; then echo "$s_on"; else echo parallel; fi)" \
	"parallel"
fp_on="$(fp wnum)"

q "TRUNCATE wnum;" >/dev/null
q "SET pgcolumnar.parallel_flush=off; INSERT INTO wnum $WNUM;" >/dev/null
fp_off="$(fp wnum)"

# REMOVAL PROOF: mutate flush_columns_parallel's assembly to reorder or alter a
# column's bytes -> fp_on diverges from fp_off and this goes RED.
check_text "opting in stores byte-identical chunks to the serial path" "$fp_on" "$fp_off"

# ---- 3. the refuted metric collides ----------------------------------------
#
# i5_rand and i5_const have the identical schema and row count, so the dispatch
# metric (rows, natts, bufbytes, valbytes, valcount) is identical -- yet their
# encode cost is opposite: random bigints are incompressible and carry the heavy
# per-column work parallelism spreads, constants are trivial. #445 measured
# opposite best for exactly this kind of pair. A gate over this metric cannot
# tell them apart, which is why default-on is refused.
q "CREATE TABLE i5_rand  (a bigint,b bigint,c bigint,d bigint,e bigint) USING pgcolumnar;" >/dev/null
q "CREATE TABLE i5_const (a bigint,b bigint,c bigint,d bigint,e bigint) USING pgcolumnar;" >/dev/null
m_rand="$(dispatch "SET pgcolumnar.parallel_flush=on;" "INSERT INTO i5_rand  SELECT (random()*9e18)::bigint,(random()*9e18)::bigint,(random()*9e18)::bigint,(random()*9e18)::bigint,(random()*9e18)::bigint FROM generate_series(1,400000) g;")"
m_const="$(dispatch "SET pgcolumnar.parallel_flush=on;" "INSERT INTO i5_const SELECT 7::bigint,7::bigint,7::bigint,7::bigint,7::bigint FROM generate_series(1,400000) g;")"
# strip the -> decision; compare only the metric the gate would read.
check_text "the dispatch metric is byte-identical for a random and a constant column (the metric cannot separate opposite-cost shapes)" \
	"${m_rand% -> *}" "${m_const% -> *}"

# REMOVAL PROOF / future signal: this can only go RED if a distinguishing term
# (e.g. per-column encode cost) is added to the metric -- which is the signal a
# real gate has become possible. A comment at the dispatch says so.

# ---- 4. a second cancel during the error cleanup frees what the workers pinned (#1745)
#
# A worker publishes its columns in an OUTPUT segment it pins, so the segment
# outlives it. On an error during the dispatcher's wait, pflush_error_cleanup
# terminates the workers, waits for them, then unpins every published segment.
# The wait checked for interrupts, so a second cancel landing in it raised out of
# the cleanup before the unpin: the segments stayed pinned until postmaster
# restart, and the second cancel was reported in place of the first.
#
# The fixture makes the window deterministic. Seven cheap int columns are encoded
# and published within the first second; one text column at zstd level 19 keeps a
# worker encoding for many seconds, and a terminated worker exits only at its next
# interrupt check, after that column. The first cancel is a 2s statement_timeout,
# so it lands in the dispatcher's wait with the cheap outputs published; the second
# is pg_cancel_backend at 3.5s, while the cleanup waits for the encoding worker.
dsm() { q "SELECT string_agg(f, ' ' ORDER BY f) FROM pg_ls_dir('pg_dynshmem') f"; }
pf_workers() { q "SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'pgcolumnar parallel_flush worker'"; }
q "CREATE TABLE dc (a int, b int, c int, d int, e int, f int, g int, x text) USING pgcolumnar;" >/dev/null
DSM0="$(dsm)"
DCOUT="$PGC_WORKDIR/double_cancel.out"
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
	-v VERBOSITY=verbose -c "SET application_name = 'pf_double_cancel';" \
	-c "SET pgcolumnar.parallel_flush = on;" -c "SET pgcolumnar.compression = zstd;" \
	-c "SET pgcolumnar.compression_level = 19;" -c "SET pgcolumnar.stripe_row_limit = 1000000;" \
	-c "SET statement_timeout = '2s';" \
	-c "INSERT INTO dc SELECT g, g, g, g, g, g, g, md5(g::text) || md5((g * 7)::text) || md5((g * 13)::text) FROM generate_series(1, 150000) g;" \
	>"$DCOUT" 2>&1 &
DCPID=$!
sleep 3.5
W_AT="$(pf_workers)"
NEW_AT="$(LC_ALL=C comm -13 <(tr ' ' '\n' <<<"$DSM0" | LC_ALL=C sort) <(dsm | tr ' ' '\n' | LC_ALL=C sort) | grep -c .)"
q "SELECT pg_cancel_backend(pid) FROM pg_stat_activity WHERE application_name = 'pf_double_cancel';" >/dev/null
wait "$DCPID"
for _ in $(seq 1 120); do [ "$(pf_workers)" = 0 ] && break; sleep 1; done
LEFT="$(LC_ALL=C comm -13 <(tr ' ' '\n' <<<"$DSM0" | LC_ALL=C sort) <(dsm | tr ' ' '\n' | LC_ALL=C sort) | grep -c .)"

check_text "premise: a flush worker is still encoding when the second cancel lands" \
	"$([ "${W_AT:-0}" -ge 1 ] 2>/dev/null && echo yes || echo "no (workers=[$W_AT])")" "yes"
check_text "premise: the cheap workers published their segments before the second cancel" \
	"$([ "${NEW_AT:-0}" -ge 2 ] 2>/dev/null && echo yes || echo "no (new segments=[$NEW_AT])")" "yes"
check_text "a twice-cancelled parallel flush reports the first cancel" \
	"$(grep -oE 'ERROR: +[0-9A-Z]{5}: .*' "$DCOUT" | head -1 | sed -E 's/ERROR: +//')" \
	"57014: canceling statement due to statement timeout"
check_text "a twice-cancelled parallel flush leaves no worker segment pinned" \
	"$LEFT new segments" "0 new segments"

# ---- 5. the collect loop forgets a handle as soon as it unpins it (#1753) -----
#
# The collect loop attaches a DONE worker's OUTPUT segment, unpins it, reads its
# columns, then detaches. While the handle stayed set after the unpin, an error in
# between (a palloc in the deserialize) sent pflush_error_cleanup to attach that
# segment again: "can't attach the same segment more than once" replaced the
# original error and cut the cleanup short, leaving the other segments pinned
# (measured on main with an ERROR injected after the first unpin: 3 left). The
# statement right after the unpin must forget the handle; the source is read with
# comments blanked.
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/src"
after_unpin="$(perl -0777 -ne '
	if (/\nflush_columns_parallel\(.*?\n\{(.*?)\n\}\n/s) {
		my $f = $1; $f =~ s{/\*.*?\*/}{}gs; $f =~ s{//[^\n]*}{}g;
		if ($f =~ /dsm_unpin_segment\(slots\[i\]\.outHandle\);\s*([^;]*;)/s) {
			my $b = $1; $b =~ s/\s+/ /g; $b =~ s/^ | $//g; print $b;
		} else { print "the collect loop does not unpin slots[i].outHandle" }
	} else { print "flush_columns_parallel not found" }' "$SRC/columnar_write_state.c")"
check_text "the collect loop forgets a worker's handle in the statement after it unpins the segment" \
	"$after_unpin" "slots[i].outHandle = DSM_HANDLE_INVALID;"

# ---- 6. nothing raises between a worker's pin and its DONE (#1747) ----------
#
# A worker pins its OUTPUT segment so it outlives the worker. On the NORMAL path the
# dispatcher's collect loop unpins only a DONE slot; a worker that raised after the
# pin reported FAILED, and its pinned segment stayed until postmaster restart
# (measured on main with an injected ERROR after the pin: 8 segments left by a
# two-stripe flush). The worker now commits first and pins last, so nothing after
# dsm_pin_segment can raise. The stretch read runs to the end of the PG_TRY block, not
# just to the DONE write: an ERROR after DONE also lands in PG_CATCH, which overwrites
# DONE with FAILED, so a commit moved after the DONE write leaks the segment too
# (#1748 review). Comments blanked, it must be the detach and the DONE write alone.
after_pin="$(perl -0777 -ne '
	if (/\npgcolumnar_parallel_flush_worker\(Datum main_arg\)\n\{(.*?)\n\}\n/s) {
		my $f = $1;
		if ($f =~ /dsm_pin_segment\(outseg\);(.*?)\}\s*PG_CATCH\(\);/s) {
			my $b = $1; $b =~ s{/\*.*?\*/}{}gs; $b =~ s{//[^\n]*}{}g; $b =~ s/\s+/ /g; $b =~ s/^ | $//g;
			print $b eq "" ? "(nothing)" : $b;
		} else { print "the pin and the PG_CATCH are not both in the worker" }
	} else { print "pgcolumnar_parallel_flush_worker not found" }' "$SRC/columnar_write_state.c")"
check_text "a flush worker runs nothing that can raise after pinning its output, to the end of its PG_TRY block" \
	"$after_pin" "dsm_detach(outseg); pg_atomic_write_u32(&me->state, PFLUSH_DONE);"

# ---- 7. a flush worker takes no lock that can wait on the table (#2072) ------
#
# Each flush worker opened the table with ACCESS SHARE. The workers are not in the
# inserting backend's lock group, and the backend waits for them on a latch, so the
# deadlock detector cannot see the wait. Two cases hung until statement_timeout:
#  - the inserting transaction itself holds ACCESS EXCLUSIVE (TRUNCATE, LOCK TABLE,
#    ALTER TABLE before the INSERT);
#  - another session queues ALTER TABLE behind a plain INSERT, and each new
#    stripe's workers queue behind the ALTER.
# Each case must finish well inside its statement_timeout, with worker columns, so
# a fix that silently fell back to serial does not pass the split read.
#
# allsplit <file>: how every parallel flush in <file> was split, read strictly.
# -> "every column in workers" when each split line reads "N of N columns" and no
# worker failed. A worker that fails hands its columns back to the backend, which
# encodes them serially with the right attributes, so the rows still match and
# split()'s "workers" reading still holds: only this reading sees the failure (#2140
# review). Else -> what was read.
allsplit() {
	local lines failed short
	lines="$(grep -oE 'parallel_flush: [0-9]+ of [0-9]+ columns by [0-9]+ worker' "$1")"
	failed="$(grep -m1 -oE 'worker [0-9]+ failed, completing its columns serially: .*' "$1")"
	if [ -z "$lines" ]; then
		echo "no split line"
	elif [ -n "$failed" ]; then
		echo "a worker failed:[$failed]"
	else
		# "parallel_flush: N of M columns by K worker": field 2 is N, field 4 is M.
		short="$(awk '$2 != $4' <<<"$lines" | LC_ALL=C sort -u | tr '\n' ';')"
		if [ -n "$short" ]; then
			echo "not every column in workers:[$short]"
		else
			echo "every column in workers"
		fi
	fi
}

# lockflush <table> <setupSql>: in one session at DEBUG1, with parallel_flush on and
# a 15s statement_timeout, run BEGIN; <setupSql>; an INSERT into <table>; COMMIT.
# -> the first ERROR line, or allsplit()'s reading of the flush, then the rows read
# back.
q "CREATE TABLE lk (a int, b int, c int, d int, e int, f int, g int, h int) USING pgcolumnar;" >/dev/null
LKROWS="SELECT g, g % 7, ((g::bigint * 7919) % 65537)::int, g / 10, -g, g % 2, 0, (g * 31) % 1000 FROM generate_series(1,20000) g"
lockflush() {
	env PATH="$PGC_BINDIR:$PATH" psql -X -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-c "SET client_min_messages = debug1;" -c "SET pgcolumnar.parallel_flush = on;" \
		-c "SET statement_timeout = '15s';" \
		-c "BEGIN; $2; INSERT INTO $1 (a, b, c, d, e, f, g, h) $LKROWS; COMMIT;" >"$FLUSHLOG" 2>&1
	local err
	err="$(grep -m1 -oE 'ERROR: .*' "$FLUSHLOG")"
	if [ -n "$err" ]; then
		echo "$err"
	else
		echo "$(allsplit "$FLUSHLOG"), $(q "SELECT count(*) || ' rows, sum(a) ' || sum(a) FROM $1")"
	fi
}
LKWANT="every column in workers, 20000 rows, sum(a) 200010000"
check_text "a parallel flush after TRUNCATE in the same transaction finishes, in workers (#2072)" \
	"$(lockflush lk "TRUNCATE lk")" "$LKWANT"
check_text "a parallel flush after LOCK TABLE IN ACCESS EXCLUSIVE MODE in the same transaction finishes, in workers (#2072)" \
	"$(lockflush lk "TRUNCATE lk; LOCK TABLE lk IN ACCESS EXCLUSIVE MODE")" "$LKWANT"
# The added column is written too: the flush encodes the columns the inserting
# transaction sees, not the ones committed before it.
check_text "a parallel flush after ALTER TABLE ADD COLUMN in the same transaction finishes, in workers (#2072)" \
	"$(lockflush lk "TRUNCATE lk; ALTER TABLE lk ADD COLUMN x int DEFAULT 5")" "$LKWANT"
check_text "a parallel flush writes the column ALTER TABLE added in the same transaction (#2072)" \
	"$(q "SELECT count(x) || ' values, sum ' || sum(x) FROM lk")" "20000 values, sum 100000"

# The workers must encode with the attributes the inserting transaction sees. An
# added int column has the same bytes under any int4 attribute, so it cannot show
# whose attributes a worker used. A retype in the same transaction can: a worker
# that read the committed descriptor encodes b as int4 and h as int4 while the
# backend hands it int8 and text values. lr is retyped and loaded in parallel; lr_s
# gets the same statements serially; their chunks and rows must match (#2140 review).
q "CREATE TABLE lr (a int, b int, c int, d int, e int, f int, g int, h int) USING pgcolumnar;" >/dev/null
q "CREATE TABLE lr_s (a int, b int, c int, d int, e int, f int, g int, h int) USING pgcolumnar;" >/dev/null
LRALTER="ALTER COLUMN b TYPE bigint, ALTER COLUMN h TYPE text"
check_text "a parallel flush after ALTER COLUMN TYPE in the same transaction finishes, every column in workers (#2140)" \
	"$(lockflush lr "TRUNCATE lr; ALTER TABLE lr $LRALTER")" "$LKWANT"
q "SET pgcolumnar.parallel_flush = off; BEGIN; TRUNCATE lr_s; ALTER TABLE lr_s $LRALTER; INSERT INTO lr_s (a, b, c, d, e, f, g, h) $LKROWS; COMMIT;" >/dev/null
check_text "a parallel flush after ALTER COLUMN TYPE in the same transaction stores the serial path's chunks and rows (#2140)" \
	"$(fp lr)" "$(fp lr_s)"

# The second case. Many small stripes, so every stripe starts its own workers while
# the ALTER waits. The INSERT takes about 8s alone and gets 60s; ALTER, queued
# 1s in, waits for it.
q "CREATE TABLE lq (a int, b int8, c float8) USING pgcolumnar;" >/dev/null
q "SELECT pgcolumnar.set_options('lq', stripe_row_limit => 1000);" >/dev/null
LQOUT="$PGC_WORKDIR/lock_queue_insert.out"
LAOUT="$PGC_WORKDIR/lock_queue_alter.out"
env PATH="$PGC_BINDIR:$PATH" psql -X -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
	-c "SET application_name = 'pf_lock_queue';" -c "SET pgcolumnar.parallel_flush = on;" \
	-c "SET client_min_messages = debug1;" -c "SET statement_timeout = '60s';" \
	-c "INSERT INTO lq SELECT g, g, g FROM generate_series(1, 600000) g;" >"$LQOUT" 2>&1 &
LQPID=$!
sleep 1
env PATH="$PGC_BINDIR:$PATH" psql -X -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
	-c "SET application_name = 'pf_lock_alter';" -c "SET lock_timeout = '120s';" \
	-c "ALTER TABLE lq ADD COLUMN d int;" >"$LAOUT" 2>&1 &
LAPID=$!
# Read both sessions until the ALTER waits on the table lock, for up to 5s, so a
# slow start does not read as a failed premise.
LA_SQL="SELECT 'alter ' || coalesce((SELECT state || ' ' || coalesce(wait_event_type || '/' || wait_event, 'not waiting') FROM pg_stat_activity WHERE application_name = 'pf_lock_alter' AND query LIKE 'ALTER%'), 'not started') || ', insert ' || coalesce((SELECT state FROM pg_stat_activity WHERE application_name = 'pf_lock_queue'), 'gone')"
LA_WANT="alter active Lock/relation, insert active"
for _ in $(seq 1 25); do
	sleep 0.2
	LA_WAIT="$(q "$LA_SQL")"
	[ "$LA_WAIT" = "$LA_WANT" ] && break
done
wait "$LQPID"
wait "$LAPID"
check_text "premise: ALTER TABLE was waiting for the table lock while the parallel INSERT ran" \
	"$LA_WAIT" "$LA_WANT"
check_text "a parallel INSERT finishes while another session's ALTER TABLE waits for its lock (#2072)" \
	"$(grep -m1 -oE '^INSERT 0 [0-9]+|ERROR: .*' "$LQOUT")" "INSERT 0 600000"
# The INSERT must also have flushed in workers, or a fix that went serial whenever a
# lock waits would pass the line above (#2140 review).
check_text "a parallel INSERT that another session's ALTER TABLE waits behind flushes every column in workers (#2140)" \
	"$(allsplit "$LQOUT")" "every column in workers"
# A liveness premise: main's code passes it too.
check_text "premise: the waiting ALTER TABLE completes once the parallel INSERT commits (#2072)" \
	"$(grep -m1 -oE '^ALTER TABLE|ERROR: .*' "$LAOUT")" "ALTER TABLE"


# ---- 8. a column type a worker cannot resolve keeps the flush serial (#2182) -----
#
# A flush worker reads the type cache for each column it encodes. For a composite,
# the type cache opens the composite's table with ACCESS SHARE, and so do record_cmp
# and hash_record for a composite field. The worker is not in the inserting
# backend's lock group, so after TRUNCATE or LOCK TABLE on the composite's table in
# the inserting transaction the INSERT waited until statement_timeout: #2072's wait
# on another table. A worker also runs on its own snapshot, so a type created in
# the inserting transaction was invisible to it: it failed with a WARNING and its
# columns were redone serially. The backend now keeps such a flush serial. Every
# way a worker reaches a composite is tried: directly, as an array element, under a
# domain, as a composite's field, as a range's subtype and inside a multirange.
# tyc and ty_n each carry a dropped column, which the walk must step over.
#
# typeflush <table> <setupSql> <insertSql> <sumExpr>: in one session at DEBUG1, with
# parallel_flush on and a 10s statement_timeout, run BEGIN; <setupSql>; <insertSql>;
# COMMIT. -> the first ERROR line, or the distinct dispatch decisions, allsplit()'s
# reading, and the rows read back.
typeflush() {
	env PATH="$PGC_BINDIR:$PATH" psql -X -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-c "SET client_min_messages = debug1;" -c "SET pgcolumnar.parallel_flush = on;" \
		-c "SET statement_timeout = '10s';" \
		-c "BEGIN; $2; $3; COMMIT;" >"$FLUSHLOG" 2>&1
	local err dec
	err="$(grep -m1 -oE 'ERROR: .*' "$FLUSHLOG")"
	if [ -n "$err" ]; then
		echo "$err"
		return
	fi
	dec="$(grep -oE 'parallel_flush dispatch: .*-> (parallel|serial)' "$FLUSHLOG" |
		sed 's/^.*-> //' | LC_ALL=C sort -u | tr '\n' '/' | sed 's#/$##')"
	echo "${dec:-no dispatch line}, $(allsplit "$FLUSHLOG"), $(q "SELECT count(*) || ' rows, sum ' || sum($4) FROM $1")"
}
q "CREATE TABLE ty_c (x int, y int);
   CREATE TABLE ty_n (x int, gone int, z ty_c);
   ALTER TABLE ty_n DROP COLUMN gone;
   CREATE DOMAIN ty_d AS ty_c;
   CREATE TYPE ty_r AS RANGE (subtype = ty_c);
   CREATE TABLE tyc (a int, b int, gone int, r ty_c) USING pgcolumnar;
   ALTER TABLE tyc DROP COLUMN gone;
   CREATE TABLE tya (a int, b int, r ty_c[]) USING pgcolumnar;
   CREATE TABLE tyd (a int, b int, r ty_d) USING pgcolumnar;
   CREATE TABLE tyn (a int, b int, r ty_n) USING pgcolumnar;
   CREATE TABLE tyr (a int, b int, r ty_r) USING pgcolumnar;
   CREATE TABLE tym (a int, b int, r ty_r_multirange) USING pgcolumnar;
   CREATE TABLE tye (a int, b int) USING pgcolumnar;
   CREATE TABLE tyk (a int, b int) USING pgcolumnar;
   CREATE TABLE tysr (a int, b int) USING pgcolumnar;
   CREATE TABLE tyso (a int, b int) USING pgcolumnar;
   CREATE TABLE tycr (a int, b int) USING pgcolumnar;
   CREATE TABLE tycp (a int, b int) USING pgcolumnar;
   CREATE TABLE tyl (a int, b int) USING pgcolumnar;
   CREATE COLLATION ty_co (provider = icu, locale = 'und');
   CREATE TABLE tyo (a int, b int, s text COLLATE ty_co) USING pgcolumnar;" >/dev/null
TYG="FROM generate_series(1,20000) g"
TYSERIAL="serial, no split line, 20000 rows, sum 200010000"
TYWORKERS="parallel, every column in workers, 20000 rows, sum 200010000"
TYLOCK="LOCK TABLE ty_c IN ACCESS EXCLUSIVE MODE"
check_text "a parallel flush after TRUNCATE of a composite column's table in the same transaction finishes, serially (#2182)" \
	"$(typeflush tyc "TRUNCATE ty_c, tyc" "INSERT INTO tyc SELECT g, g, row(g, g)::ty_c $TYG" "(r).x")" "$TYSERIAL"
check_text "a parallel flush of an array-of-composite column whose table the transaction locked finishes, serially (#2182)" \
	"$(typeflush tya "$TYLOCK" "INSERT INTO tya SELECT g, g, array[row(g, g)::ty_c] $TYG" "(r[1]).x")" "$TYSERIAL"
check_text "a parallel flush of a domain-over-composite column whose table the transaction locked finishes, serially (#2182)" \
	"$(typeflush tyd "$TYLOCK" "INSERT INTO tyd SELECT g, g, row(g, g)::ty_c $TYG" "(r).x")" "$TYSERIAL"
check_text "a parallel flush of a composite whose field's table the transaction locked finishes, serially (#2182)" \
	"$(typeflush tyn "$TYLOCK" "INSERT INTO tyn SELECT g, g, row(g, row(g, g)::ty_c)::ty_n $TYG" "((r).z).x")" "$TYSERIAL"
check_text "a parallel flush of a range-over-composite column whose table the transaction locked finishes, serially (#2182)" \
	"$(typeflush tyr "$TYLOCK" "INSERT INTO tyr SELECT g, g, ty_r(row(g, g)::ty_c, row(g + 1, g)::ty_c) $TYG" "(lower(r)).x")" "$TYSERIAL"
check_text "a parallel flush of a multirange-over-composite column whose table the transaction locked finishes, serially (#2182)" \
	"$(typeflush tym "$TYLOCK" "INSERT INTO tym SELECT g, g, ty_r_multirange(ty_r(row(g, g)::ty_c, row(g + 1, g)::ty_c)) $TYG" "(lower(r)).x")" "$TYSERIAL"
# The type created in the inserting transaction. The reading names a worker that
# failed on it, so a flush that hands its columns back still reads red.
check_text "a parallel flush of a column whose type the same transaction created runs serially, with no worker failure (#2182)" \
	"$(typeflush tye "CREATE TYPE ty_e AS ENUM ('x', 'y'); ALTER TABLE tye ADD COLUMN e ty_e" "INSERT INTO tye SELECT g, g, 'x' $TYG" "a")" "$TYSERIAL"
# A collation created in the inserting transaction is just as invisible to a worker,
# which compares and hashes a text column under it: it failed with "cache lookup
# failed for collation" and its columns were redone serially (#2250 review). Both
# providers. The control: a collation created in an earlier transaction still flushes
# in workers.
check_text "a parallel flush of a column whose ICU collation the same transaction created runs serially, with no worker failure (#2182)" \
	"$(typeflush tyk "CREATE COLLATION ty_ck (provider = icu, locale = 'und'); ALTER TABLE tyk ADD COLUMN s text COLLATE ty_ck" "INSERT INTO tyk SELECT g, g, 'v' || g $TYG" "a")" "$TYSERIAL"
check_text "a parallel flush of a column whose libc collation the same transaction created runs serially, with no worker failure (#2182)" \
	"$(typeflush tyl "CREATE COLLATION ty_cl (provider = libc, locale = 'C'); ALTER TABLE tyl ADD COLUMN s text COLLATE ty_cl" "INSERT INTO tyl SELECT g, g, 'v' || g $TYG" "a")" "$TYSERIAL"
# The same, created inside a savepoint, released or still open. The row's xmin is
# then the subtransaction's, not the top-level transaction's, so a test against the
# top-level transaction ID alone misses it (#2250 review).
check_text "a parallel flush of a column whose type a released savepoint of the same transaction created runs serially, with no worker failure (#2182)" \
	"$(typeflush tysr "SAVEPOINT s; CREATE TYPE ty_esr AS ENUM ('x', 'y'); RELEASE SAVEPOINT s; ALTER TABLE tysr ADD COLUMN e ty_esr" "INSERT INTO tysr SELECT g, g, 'x' $TYG" "a")" "$TYSERIAL"
check_text "a parallel flush of a column whose type a still-open savepoint created runs serially, with no worker failure (#2182)" \
	"$(typeflush tyso "SAVEPOINT s; CREATE TYPE ty_eso AS ENUM ('x', 'y'); ALTER TABLE tyso ADD COLUMN e ty_eso" "INSERT INTO tyso SELECT g, g, 'x' $TYG" "a")" "$TYSERIAL"
check_text "a parallel flush of a column whose collation a released savepoint of the same transaction created runs serially, with no worker failure (#2182)" \
	"$(typeflush tycr "SAVEPOINT s; CREATE COLLATION ty_ccr (provider = icu, locale = 'und'); RELEASE SAVEPOINT s; ALTER TABLE tycr ADD COLUMN s text COLLATE ty_ccr" "INSERT INTO tycr SELECT g, g, 'v' || g $TYG" "a")" "$TYSERIAL"
check_text "a parallel flush of a column whose collation a still-open savepoint created runs serially, with no worker failure (#2182)" \
	"$(typeflush tycp "SAVEPOINT s; CREATE COLLATION ty_ccp (provider = icu, locale = 'und'); ALTER TABLE tycp ADD COLUMN s text COLLATE ty_ccp" "INSERT INTO tycp SELECT g, g, 'v' || g $TYG" "a")" "$TYSERIAL"
check_text "a parallel flush of a column whose non-default collation an earlier transaction created flushes every column in workers (#2182)" \
	"$(typeflush tyo "TRUNCATE tyo" "INSERT INTO tyo SELECT g, g, 'v' || g $TYG" "a")" "$TYWORKERS"
# The controls: a composite whose table this transaction does not lock, or holds
# only in a mode ACCESS SHARE does not conflict with, still flushes in workers. The
# TRUNCATE of the columnar table itself holds ACCESS EXCLUSIVE on it, which #2072
# already handles, so a fix that checked that table instead also reads red here.
check_text "a parallel flush of a composite column whose table the transaction does not lock flushes every column in workers (#2182)" \
	"$(typeflush tyc "TRUNCATE tyc" "INSERT INTO tyc SELECT g, g, row(g, g)::ty_c $TYG" "(r).x")" "$TYWORKERS"
check_text "a parallel flush of a composite column whose table the transaction holds in SHARE mode flushes every column in workers (#2182)" \
	"$(typeflush tyc "TRUNCATE tyc; LOCK TABLE ty_c IN SHARE MODE" "INSERT INTO tyc SELECT g, g, row(g, g)::ty_c $TYG" "(r).x")" "$TYWORKERS"


# ---- 9. the backend waits for its flush workers in the lock manager (#2256) ----
#
# A worker reads the type cache in its own transaction, so for a composite column it
# takes ACCESS SHARE on the composite's table. The backend waited for its workers on a
# latch, which the deadlock detector does not follow. So when another session B held
# ACCESS EXCLUSIVE on that table and then asked for SHARE on the columnar table, the
# worker waited for B, B waited for the backend, and the backend waited for the worker
# unseen: the INSERT waited until statement_timeout. #2182 covers a lock the
# inserting transaction holds itself; this is one another session holds. Each worker
# now holds an advisory lock of class 110 from the moment it connects, and the backend
# waits on those, so the detector sees the cycle and ends it with 40P01.
#
# The order is jdatcmd's: A loads cy_c's type cache entry first, so A itself needs no
# lock on cy_c; B then takes ACCESS EXCLUSIVE on cy_c; A runs a parallel INSERT with
# one worker; B waits until that worker waits on cy_c, then asks for SHARE on the
# columnar table. Who ends the cycle depends on whose deadlock_timeout runs out first.
# The worker runs at the server's 1 s, so A and B pick theirs per case:
#  - A and B at 30 s: the worker is the victim. A logs its 40P01 as a WARNING with
#    the cycle in its detail, encodes the worker's columns itself, and commits;
#  - A at 500 ms: A is the victim, and its INSERT fails with 40P01;
#  - B at 100 ms: B is the victim, and A's INSERT finishes in its worker;
#  - all at 1 s, the issue's case: any of the three, well before the timeout.
# A gets a 10 s statement_timeout and B 20 s; on main every case is A's 57014 at 10 s.
cy_sess() {	# APPNAME PSQL-ARGS... -> psql's output, errors and warnings with their SQLSTATE
	local app="$1"; shift
	env PGAPPNAME="$app" PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-X -qAt -v VERBOSITY=verbose "$@" 2>&1
}
# pg_stat_activity is read once per transaction unless the snapshot is cleared.
cy_until() {	# CONDITION -> a DO block that polls CONDITION every 20 ms, for up to 8 s
	echo "DO \$\$ BEGIN FOR i IN 1..400 LOOP PERFORM pg_stat_clear_snapshot(); EXIT WHEN $1; PERFORM pg_sleep(0.02); END LOOP; END \$\$"
}
cy_crashes() { pgc_pg "grep -c 'terminated by signal' '$PGC_LOGFILE'" 2>/dev/null; }
q "CREATE TABLE cy_c (x int, y int);
   CREATE TABLE cy_w (a int, r cy_c) USING pgcolumnar;
   CREATE TABLE cy_wa (a int, r cy_c) USING pgcolumnar;
   CREATE TABLE cy_wb (a int, r cy_c) USING pgcolumnar;
   CREATE TABLE cy_wd (a int, r cy_c) USING pgcolumnar;
   CREATE TABLE cy_w2 (r cy_c, s cy_c) USING pgcolumnar;
   CREATE TABLE cy_lt (a int, b int, c int, d int, e int, f int, g int, h int) USING pgcolumnar;" >/dev/null
CY_CRASHES_AT_START="$(cy_crashes)"
# cycle TABLE A-DEADLOCK-TIMEOUT B-DEADLOCK-TIMEOUT [MAX-PARALLEL-WORKERS [SELECT-LIST]]
# -> "A: <what A's INSERT did> | B: <what B did> | <rows in TABLE> | <does the deadlock
# report name a run lock> | <time>". max_parallel_workers 2 gives the flush one worker.
cycle() {
	local t0 ms ap ae aw be
	t0=$(date +%s%N)
	cy_sess pf_cy_a -c "SET deadlock_timeout = '$2'" -c "SET max_parallel_workers = ${4:-2}" \
		-c "SELECT (row(1, 1)::cy_c).x" \
		-c "$(cy_until "EXISTS (SELECT 1 FROM pg_locks WHERE relation = 'cy_c'::regclass AND mode = 'AccessExclusiveLock' AND granted)")" \
		-c "SET pgcolumnar.parallel_flush = on" -c "SET client_min_messages = debug1" -c "SET statement_timeout = '10s'" \
		-c "INSERT INTO $1 SELECT ${5:-g, row(g, g)::cy_c} FROM generate_series(1, 20000) g" >"$PGC_WORKDIR/cy_a.out" &
	ap=$!
	cy_sess pf_cy_b -c "SET deadlock_timeout = '$3'" -c "SET statement_timeout = '20s'" \
		-c "$(cy_until "EXISTS (SELECT 1 FROM pg_stat_activity WHERE application_name = 'pf_cy_a' AND query LIKE 'DO%')")" \
		-c "BEGIN" -c "LOCK TABLE cy_c IN ACCESS EXCLUSIVE MODE" \
		-c "$(cy_until "EXISTS (SELECT 1 FROM pg_locks WHERE relation = 'cy_c'::regclass AND NOT granted)")" \
		-c "LOCK TABLE $1 IN SHARE MODE" -c "SELECT 'got both'" -c "COMMIT" >"$PGC_WORKDIR/cy_b.out"
	wait "$ap"
	ms=$(( ($(date +%s%N) - t0) / 1000000 ))
	ae="$(sed -n '/^ERROR:/{s/^ERROR: *\([0-9A-Z]\{5\}\): \(.*\)$/\1 \2/p;q;}' "$PGC_WORKDIR/cy_a.out")"
	aw="$(sed -n '/^WARNING:/{s/^WARNING: *\([0-9A-Z]\{5\}\): \(.*\)$/\1 \2/p;q;}' "$PGC_WORKDIR/cy_a.out")"
	be="$(sed -n '/^ERROR:/{s/^ERROR: *\([0-9A-Z]\{5\}\): \(.*\)$/\1 \2/p;q;}' "$PGC_WORKDIR/cy_b.out")"
	printf 'A: %s | B: %s | %s rows | %s | %s\n' \
		"$(if [ -n "$ae" ]; then echo "$ae"; elif [ -n "$aw" ]; then echo "committed after WARNING $aw"; else echo "committed, $(allsplit "$PGC_WORKDIR/cy_a.out")"; fi)" \
		"$(if [ -n "$be" ]; then echo "$be"; else grep -qx 'got both' "$PGC_WORKDIR/cy_b.out" && echo "got both" || echo "no result"; fi)" \
		"$(q "SELECT count(*) FROM $1")" \
		"$(if grep -q 'waits for ShareLock on advisory lock \[[0-9]*,[0-9]*,[0-9]*,110\]' "$PGC_WORKDIR/cy_a.out" "$PGC_WORKDIR/cy_b.out"; then
			echo "the deadlock names the run lock"; else echo "no run lock named"; fi)" \
		"$(if [ "$ms" -lt 6000 ]; then echo "under 6 s"; else echo "took $ms ms"; fi)"
}
check_text "a parallel flush worker chosen as the deadlock victim reaches the backend as a 40P01 WARNING naming the run lock, and the INSERT commits its rows serially within 6 s (#2256)" \
	"$(cycle cy_w 30s 30s)" \
	"A: committed after WARNING 40P01 pgcolumnar parallel_flush worker 0 failed, completing its columns serially: deadlock detected | B: got both | 20000 rows | the deadlock names the run lock | under 6 s"
check_text "a backend waiting for its parallel flush worker, chosen as the deadlock victim, fails with 40P01 naming the run lock within 6 s (#2256)" \
	"$(cycle cy_wa 500ms 30s)" \
	"A: 40P01 deadlock detected | B: got both | 0 rows | the deadlock names the run lock | under 6 s"
check_text "the other session chosen as the deadlock victim fails with 40P01 naming the run lock, and the parallel INSERT finishes in its worker within 6 s (#2256)" \
	"$(cycle cy_wb 30s 100ms)" \
	"A: committed, every column in workers | B: 40P01 deadlock detected | 20000 rows | the deadlock names the run lock | under 6 s"
# The issue's case, every deadlock_timeout at 1 s: one side gives way, whichever.
cy_default="$(cycle cy_wd 1s 1s)"
case "$cy_default" in
	"A: committed after WARNING 40P01 pgcolumnar parallel_flush worker 0 failed, completing its columns serially: deadlock detected | B: got both | 20000 rows | "*|\
	"A: 40P01 deadlock detected | B: got both | 0 rows | "*|\
	"A: committed, every column in workers | B: 40P01 deadlock detected | 20000 rows | "*)
		cy_gave="${cy_default##* | }" ;;
	*) cy_gave="neither gave way:[$cy_default]" ;;
esac
check_text "with every deadlock_timeout at 1 s, the ordered two-session cycle through a parallel flush worker ends with one side giving way within 6 s, not at statement_timeout (#2256)" \
	"$cy_gave" "under 6 s"
# Two workers, each encoding a composite column, so both wait on cy_c. The backend
# waits for worker 0 first; at the server's 1 s worker 0 sees the cycle and is the
# victim. The backend then waits for worker 1, and that wait closes the cycle again:
# worker 1, if its own check comes after, or the backend at its 2 s, ends it. Either
# way some report names worker 1's run lock. A backend that waited only for worker 0
# waited for worker 1 on a latch, unseen, until statement_timeout.
cy_two="$(cycle cy_w2 2s 30s 4 "row(g, g)::cy_c, row(g, g)::cy_c")"
case "$cy_two" in
	"A: 40P01 deadlock detected | B: got both | 0 rows | the deadlock names the run lock | under 6 s"|\
	"A: committed after WARNING 40P01 pgcolumnar parallel_flush worker "[01]" failed, completing its columns serially: deadlock detected | B: got both | 20000 rows | the deadlock names the run lock | under 6 s")
		cy_two="$(if grep -q 'waits for ShareLock on advisory lock \[[0-9]*,[0-9]*,1,110\]' "$PGC_WORKDIR/cy_a.out"; then
			echo "worker 1's run lock named"; else echo "worker 1's run lock not named"; fi), under 6 s" ;;
	*) cy_two="neither gave way:[$cy_two]" ;;
esac
check_text "with two parallel flush workers waiting on the cycle, the backend waits for the second worker's run lock too, and one side gives way within 6 s (#2256)" \
	"$cy_two" "worker 1's run lock named, under 6 s"

# The waits on the workers are waits for the flush, not lock contention: a 1 ms
# lock_timeout does not end an uncontended parallel flush, and with deadlock_timeout
# 1 ms and log_lock_waits on, none is reported. client_min_messages = debug1 sends
# the LOG lines to the session too.
cy_lt="$(PGOPTIONS='-c lock_timeout=1ms -c deadlock_timeout=1ms -c log_lock_waits=on' cy_sess pf_cy_lt \
	-c "SET pgcolumnar.parallel_flush = on" -c "SET client_min_messages = debug1" -c "SET statement_timeout = '60s'" \
	-c "INSERT INTO cy_lt $WNUM" >"$PGC_WORKDIR/cy_lt.out"
	e="$(sed -n '/^ERROR:/{s/^ERROR: *\([0-9A-Z]\{5\}\): \(.*\)$/\1 \2/p;q;}' "$PGC_WORKDIR/cy_lt.out")"
	echo "${e:-$(q "SELECT count(*) FROM cy_lt") rows}, $(allsplit "$PGC_WORKDIR/cy_lt.out"), $(grep -c 'still waiting for ShareLock on advisory lock' "$PGC_WORKDIR/cy_lt.out") waits for a worker logged")"
check_text "a parallel flush under lock_timeout 1 ms, deadlock_timeout 1 ms and log_lock_waits on still flushes every column in workers, and logs no wait for its workers (#2256)" \
	"$cy_lt" "300000 rows, every column in workers, 0 waits for a worker logged"
# and the session's settings are its own again once the flush returns
cy_sess pf_cy_g -c "BEGIN" -c "SET LOCAL lock_timeout = '2s'" -c "SET LOCAL log_lock_waits = on" \
	-c "SET LOCAL pgcolumnar.parallel_flush = on" -c "SET LOCAL client_min_messages = debug1" \
	-c "INSERT INTO cy_lt (a, b, c, d, e, f, g, h) $LKROWS" \
	-c "SHOW lock_timeout" -c "SHOW log_lock_waits" -c "COMMIT" >"$PGC_WORKDIR/cy_g.out"
check_text "lock_timeout and log_lock_waits read back unchanged after a parallel flush in the same transaction (#2256)" \
	"$(e="$(sed -n '/^ERROR:/{s/^ERROR: *\([0-9A-Z]\{5\}\): \(.*\)$/\1 \2/p;q;}' "$PGC_WORKDIR/cy_g.out")"
	   echo "${e:-$(allsplit "$PGC_WORKDIR/cy_g.out"), $(grep -vE '^(DEBUG|LOCATION|LOG):' "$PGC_WORKDIR/cy_g.out" | paste -sd'|')}")" \
	"every column in workers, 2s|on"
check_text "no backend died during the lock-cycle cases (#2256)" \
	"$(( $(cy_crashes) - CY_CRASHES_AT_START )) terminated by signal" "0 terminated by signal"

pgc_summary
