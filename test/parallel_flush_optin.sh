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
# two-stripe flush). The worker now commits first and pins last, so nothing between
# dsm_pin_segment and the DONE write can raise: the source is read, comments blanked,
# and the stretch must be the detach alone.
after_pin="$(perl -0777 -ne '
	if (/\npgcolumnar_parallel_flush_worker\(Datum main_arg\)\n\{(.*?)\n\}\n/s) {
		my $f = $1;
		if ($f =~ /dsm_pin_segment\(outseg\);(.*?)pg_atomic_write_u32\(&me->state, PFLUSH_DONE\);/s) {
			my $b = $1; $b =~ s{/\*.*?\*/}{}gs; $b =~ s{//[^\n]*}{}g; $b =~ s/\s+/ /g; $b =~ s/^ | $//g;
			print $b eq "" ? "(nothing)" : $b;
		} else { print "the pin and the DONE write are not both in the worker" }
	} else { print "pgcolumnar_parallel_flush_worker not found" }' "$SRC/columnar_write_state.c")"
check_text "a flush worker runs nothing that can raise between pinning its output and reporting DONE" \
	"$after_pin" "dsm_detach(outseg);"

pgc_summary
