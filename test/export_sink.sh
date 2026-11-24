#!/usr/bin/env bash
#
# pgColumnar #394 step 1: the export byte sink — every write checked, and
# nothing ever visible at the final name before the export commits.
#
# The two defects this pins (found mapping the write path, PR #604): all 17
# fwrite sites ignored their return values, so a disk-full mid-export was
# detected only if the final flush happened to fail; and serial exports wrote
# directly to the final path, leaving a partial file there on error.
#
# Arms:
#   census    zero fwrite calls remain in either export file. The 13-vs-17
#             lesson made a property of the tree, so the count can never be
#             hand-maintained (and never wrong) again. RED on the old shape.
#   round     exports still round-trip: parquet and arrow files import back
#             into tables that hash-match the source, and no *.tmp.* debris
#             remains after success.
#   inject    pgcolumnar.sink_fail_after drives the SAME error path a full
#             disk takes: the export errors SQLSTATE 53100, the FINAL path
#             does not exist, and no temp file remains. The arrow arm's
#             fail_after is derived from the measured full size so the
#             failure lands inside write_record_batch — the helper the
#             13-site census missed, which the design doc requires the proof
#             to reach.
#   fatal     an export that ends with FATAL leaves no temp file (#2075). A
#             FATAL exit (pg_terminate_backend, SIGTERM at shutdown) skips the
#             writers' PG_CATCH, so only an exit callback can remove the file.
#             exit_on_error promotes the injected ERROR to FATAL, which takes
#             that same exit path at a known byte, with no timing race. The
#             directory is read only after the backend has left
#             pg_stat_activity, because the client sees the FATAL message
#             before the backend runs its exit callbacks. Each target holds a
#             good file first, which the FATAL re-export must not delete. Two
#             arms end a transaction with FATAL after a finished export and
#             after a failed one rolled back to a savepoint, so the exit
#             callback walks a list that once held other sinks. The server log
#             must show no backend killed by a signal during these arms.
#
# Usage:  test/export_sink.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

SRC="$(dirname "${BASH_SOURCE[0]}")/../src"

sqlstate_of() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -qtA 2>&1 <<SQLEOF | sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\).*/\1/p' | head -1
\\set VERBOSITY sqlstate
$1;
SQLEOF
}
# A find that cannot read the directory is a failed reading, never "no debris".
no_debris() {
	local found
	found="$(find "$PGC_WORKDIR" -name '*.tmp.*' 2>&1)" || { echo "find failed: $found"; return; }
	printf '%s' "$found" | grep -c .
}

# ---- census -----------------------------------------------------------------
check_num "census: zero fwrite remain in columnar_parquet.c" \
	"$(grep -c 'fwrite(' "$SRC/columnar_parquet.c")" "0"
check_num "census: zero fwrite remain in columnar_arrow.c" \
	"$(grep -c 'fwrite(' "$SRC/columnar_arrow.c")" "0"

# ---- fixture ----------------------------------------------------------------
ROWS=40000
psql_run "CREATE TABLE es_src (id int, v int8, t text) USING pgcolumnar;"
psql_run "INSERT INTO es_src SELECT g, g*17, 'row-'||g FROM generate_series(1,$ROWS) g;"

# ---- round-trip and no-debris ----------------------------------------------
PQF="$PGC_WORKDIR/es.parquet"
ARF="$PGC_WORKDIR/es.arrow"
check_num "parquet export succeeds through the sink" \
	"$(q "SELECT pgcolumnar.export_parquet('es_src', '$PQF')")" "$ROWS"
check_num "arrow export succeeds through the sink" \
	"$(q "SELECT pgcolumnar.export_arrow('es_src', '$ARF')")" "$ROWS"
check_num "no temp debris after successful exports" "$(no_debris)" "0"

psql_run "CREATE TABLE es_back_pq (id int, v int8, t text) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.import_parquet('es_back_pq', '$PQF');"
# Each side carries its row count, so two empty tables cannot hash-match.
check_text "parquet round-trip hash-matches the source" \
	"$(q "SELECT count(*) FROM es_back_pq"):$(pgc_set_hash "SELECT * FROM es_back_pq")" \
	"$ROWS:$(pgc_set_hash "SELECT * FROM es_src")"
psql_run "CREATE TABLE es_back_ar (id int, v int8, t text) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.import_arrow('es_back_ar', '$ARF');"
check_text "arrow round-trip hash-matches the source" \
	"$(q "SELECT count(*) FROM es_back_ar"):$(pgc_set_hash "SELECT * FROM es_back_ar")" \
	"$ROWS:$(pgc_set_hash "SELECT * FROM es_src")"

ARROW_SIZE="$(stat -c %s "$ARF" 2>/dev/null || echo 0)"
# The good parquet file's bytes, read before a failed export over the same name.
PQ_BEFORE="$(md5sum < "$PQF" 2>/dev/null | cut -d' ' -f1)"
[ -n "$PQ_BEFORE" ] || PQ_BEFORE="unread before the failed export"
check "measured arrow size supports a mid-batch injection point" \
	"$([ "${ARROW_SIZE:-0}" -gt 4200 ] 2>/dev/null && echo yes || echo "no (arrow size=${ARROW_SIZE:-unset})")" "yes"

# ---- injection: parquet, mid page body -------------------------------------
FAILP="$PGC_WORKDIR/es_fail.parquet"
check "inject: parquet export fails as disk-full (53100)" \
	"$(sqlstate_of "SET pgcolumnar.sink_fail_after = 8192; SELECT pgcolumnar.export_parquet('es_src', '$FAILP')")" "53100"
check "inject: nothing exists at the parquet FINAL path" \
	"$([ ! -e "$FAILP" ] && echo clean)" "clean"
# A failed export over an existing good file must leave that file's bytes:
# a sink that opened the final name would truncate it, and its abort would
# then remove the only good copy. The sqlstate rides along, so a re-export
# that never ran cannot pass.
check_text "inject: a failed re-export leaves the previous parquet file intact" \
	"$(sqlstate_of "SET pgcolumnar.sink_fail_after = 8192; SELECT pgcolumnar.export_parquet('es_src', '$PQF')"):$(md5sum < "$PQF" 2>/dev/null | cut -d' ' -f1)" \
	"53100:$PQ_BEFORE"
check_num "inject: no parquet temp debris" "$(no_debris)" "0"

# ---- injection: arrow, inside write_record_batch ----------------------------
# fail_after = fullsize - 100: the last 100 bytes belong to the final record
# batch (only the 8-byte EOS marker follows it), so the failing write is
# inside the helper by arithmetic, not by luck. The schema message is bounded
# well under the premise-checked 4200.
FAILA="$PGC_WORKDIR/es_fail.arrow"
check "inject: arrow export fails inside write_record_batch (53100)" \
	"$(sqlstate_of "SET pgcolumnar.sink_fail_after = $((ARROW_SIZE - 100)); SELECT pgcolumnar.export_arrow('es_src', '$FAILA')")" "53100"
check "inject: nothing exists at the arrow FINAL path" \
	"$([ ! -e "$FAILA" ] && echo clean)" "clean"
check_num "inject: no arrow temp debris" "$(no_debris)" "0"

# ---- the injection is session state, not lingering damage -------------------
check_num "after RESET, exports work again" \
	"$(q "SELECT pgcolumnar.export_parquet('es_src', '$PGC_WORKDIR/es2.parquet')")" "$ROWS"

# ---- FATAL exit: no temp file left behind (#2075) ---------------------------
# fatal_session BODY runs BODY in one psql session that first prints its pid,
# then waits until that backend has left pg_stat_activity. It sets FS_OUT to
# psql's output (VERBOSITY verbose, so each error line carries its SQLSTATE)
# and FS to "<fatal|no fatal> <gone|still listed (n)>". "fatal" means the
# session ended with the injected fault's FATAL, SQLSTATE 53100. It sets
# globals, so call it directly, never inside $(...).
fatal_session() {
	local pid n i
	FS_OUT="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -qtA 2>&1 <<SQLEOF
\\set VERBOSITY verbose
SELECT 'pid=' || pg_backend_pid();
$1
SQLEOF
)"
	pid="$(printf '%s\n' "$FS_OUT" | sed -n 's/^pid=\([0-9]*\)$/\1/p' | head -1)"
	[ -n "$pid" ] || { FS="no pid: $FS_OUT"; return; }
	n=1
	for i in $(seq 1 100); do
		n="$(q "SELECT count(*) FROM pg_stat_activity WHERE pid = $pid")"
		[ "$n" = "0" ] && break
		sleep 0.1
	done
	# A `case`, not `printf "$out" | grep -q`: under pipefail an early-exit
	# reader can report a present match as absent (selftest 080, #486).
	local fatal='no fatal'
	case "$FS_OUT" in
	*'FATAL:  53100: columnar: export write failed (injected fault)'*) fatal=fatal ;;
	esac
	FS="$fatal $([ "$n" = "0" ] && echo gone || echo "still listed ($n)")"
}
# lines_of TEXT LINE -> how many lines of TEXT are exactly LINE
lines_of() {
	local c=0 l
	while IFS= read -r l; do [ "$l" = "$2" ] && c=$((c + 1)); done <<<"$1"
	echo "$c"
}
# debris_of NAME -> the count of NAME.tmp.* files, so each arm reads only its own
debris_of() {
	local found
	found="$(find "$PGC_WORKDIR" -name "$1.tmp.*" 2>&1)" || { echo "find failed: $found"; return; }
	printf '%s' "$found" | grep -c .
}
# The client sees the FATAL line before the backend runs its exit callbacks,
# so a callback that crashes after unlinking the temp file passes every other
# check here. Only the server log shows it, and lib.sh reads the log only
# after a check has failed. So count "terminated by signal" from here on.
log_lines() {
	local n
	n="$(pgc_pg "wc -l < '$PGC_LOGFILE'" 2>/dev/null)" && [ -n "$n" ] && echo "$n" || echo "unreadable"
}
LOG_AT="$(log_lines)"
FATAL_PRE='SET exit_on_error = on; SET pgcolumnar.sink_fail_after = 8192;'
# Each target is exported once first. A callback that unlinked the final name
# as well as the temp file would delete that good file, so its bytes must
# survive the FATAL re-export.
for kind in parquet arrow; do
	FATF="$PGC_WORKDIR/es_fatal.$kind"
	PRE="$(q "SELECT pgcolumnar.export_$kind('es_src', '$FATF')")"
	BEFORE="$(md5sum < "$FATF" 2>/dev/null | cut -d' ' -f1)"
	[ -n "$BEFORE" ] || BEFORE="unread before the FATAL re-export"
	fatal_session "$FATAL_PRE SELECT pgcolumnar.export_$kind('es_src', '$FATF');"
	check "fatal: the $kind export ends its backend with FATAL" "$FS" "fatal gone"
	check_text "fatal: a FATAL re-export leaves the previous $kind file intact" \
		"$PRE:$(md5sum < "$FATF" 2>/dev/null | cut -d' ' -f1)" "$ROWS:$BEFORE"
	check_num "fatal: no $kind temp file after a FATAL exit" "$(debris_of "es_fatal.$kind")" "0"
done
# A finished export leaves the list while its memory lives only as long as its
# statement. One that stayed listed would be a freed node for the exit callback
# to walk when a later export in the same transaction ends with FATAL.
fatal_session "BEGIN;
SELECT pgcolumnar.export_parquet('es_src', '$PGC_WORKDIR/es_tx1.parquet');
SELECT pgcolumnar.export_arrow('es_src', '$PGC_WORKDIR/es_tx2.arrow');
$FATAL_PRE
SELECT pgcolumnar.export_parquet('es_src', '$PGC_WORKDIR/es_tx3.parquet');"
check "fatal: a FATAL export after two finished exports in one transaction ends with FATAL" \
	"$(lines_of "$FS_OUT" "$ROWS"):$FS" "2:fatal gone"
check_num "fatal: no temp file after a FATAL export that followed finished exports" "$(no_debris)" "0"
# The same for a failed export: its abort must take it off the list before
# ROLLBACK TO releases its memory. exit_on_error is set only after the
# rollback, so the first failure is an ERROR.
fatal_session "BEGIN;
SAVEPOINT s;
SET pgcolumnar.sink_fail_after = 8192;
SELECT pgcolumnar.export_parquet('es_src', '$PGC_WORKDIR/es_sp1.parquet');
ROLLBACK TO SAVEPOINT s;
$FATAL_PRE
SELECT pgcolumnar.export_parquet('es_src', '$PGC_WORKDIR/es_sp2.parquet');"
check "fatal: a FATAL export after a failed export rolled back to a savepoint ends with FATAL" \
	"$(lines_of "$FS_OUT" "ERROR:  53100: columnar: export write failed (injected fault)"):$FS" "1:fatal gone"
check_num "fatal: no temp file after a FATAL export that followed a rolled-back export" "$(no_debris)" "0"
# A crash restarts the cluster. Wait for it to answer before reading the log.
for i in $(seq 1 100); do [ "$(q "SELECT 1")" = "1" ] && break; sleep 0.1; done
SIGS="unread"
if [ "$LOG_AT" != "unreadable" ]; then
	SIGS="$(pgc_pg "tail -n +$((LOG_AT + 1)) '$PGC_LOGFILE'" 2>/dev/null)" &&
		SIGS="$(lines_of "$(printf '%s\n' "$SIGS" | sed -n 's/.*\(terminated by signal\).*/\1/p')" "terminated by signal")" ||
		SIGS="log unreadable"
fi
check_num "fatal: no backend died by a signal during the FATAL arms" "$SIGS" "0"

pgc_summary
