#!/usr/bin/env bash
#
# pgColumnar parallel Parquet export suite (#300).
#
# pgcolumnar.parallel_export_parquet(target, dir, workers) fans a read-only
# export across N background workers, each writing part-NNNN.parquet into one
# directory that pgcolumnar.read_parquet reads back as a single relation. This
# suite proves parallel export == serial export == the source, for a single
# columnar table (split by row-group ranges) and a partitioned columnar table
# (one file per partition), across 1/2/4 workers; that each worker wrote its own
# file and the files partition the source; empty input; and the error cases.
#
# Oracle = pgc_set_hash over the generator that fills each table, never over the
# table being exported. It is order-independent, so file and row ordering do not
# matter. The read-back uses pgcolumnar.read_parquet, so no pyarrow.
#
# Usage:  test/parallel_export_parquet.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# parallel_export spawns N read-only workers (no coordinator), so the cluster
# needs at least that many worker slots.
export PGC_EXTRA_CONF=$'max_worker_processes=16'

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

COLS="id int, k int, v float8, txt text"
RB="id int, k int, v float8, txt text"		# read_parquet column list

nfiles() { ls "$1"/*.parquet 2>/dev/null | wc -l | tr -d ' '; }
# every file including in-flight temps: the #394 sink writes part-N.parquet.tmp.PID
# and renames at completion, so *.parquet appears only when a part is DONE.
anyfiles() { find "$1" -type f 2>/dev/null | wc -l | tr -d ' '; }

# THE BACKEND MUST NOT HAVE DIED, not merely answer now. `SELECT 1` answers again
# after crash recovery, so "still up" also requires the server log's count of
# backends killed by a signal not to have moved since setup. Measured: with one
# backend killed by SIGSEGV before the last check, the old `SELECT 1` check passed.
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
CRASHES_AT_START="$(crashes)"

# refusal_of SQL PATTERN -> "refused" when the statement failed with an ERROR whose
# message matches PATTERN, and what happened instead otherwise. It reads psql's exit
# status: grepping the output for "error" also matched a dead server's
# "psql: error: connection ... failed", so both refusals passed with the server gone,
# and any error at all stood for the refusal named.
refusal_of() {
	local out rc
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -v ON_ERROR_STOP=1 -Atc "$1" 2>&1)"
	rc=$?
	if [ "$rc" -eq 0 ]; then echo "accepted"
	elif [ "$rc" -ne 1 ]; then echo "no reading (psql exit $rc: $(head -1 <<<"$out"))"
	elif grep -qE "^ERROR: .*$2" <<<"$out"; then echo "refused"
	else echo "another error: $(grep -m1 '^ERROR:' <<<"$out")"; fi
}

# ---- the oracles: the generator, not the table under test -------------------
# Every source hash and count below is computed from the generator that fills the
# table, never read back from the columnar table being exported. Measured: with
# tuple_insert dropping the row whose id is 1, the table, its serial export and
# every parallel export agreed with each other, and all 54 checks passed.
GEN_COL="SELECT g, g%1000, g::float8/7, 'r'||g FROM generate_series(1,80000) g"
GEN_PART="SELECT g, g%600, g::float8/7, 'r'||g FROM generate_series(1,80000) g"
GEN_TX_COMMITTED="SELECT g, g, g::float8/7, 'c'||g FROM generate_series(1,20000) g"

# ---- single columnar table: split by row-group ranges -----------------------
# A small stripe_row_limit gives many row groups (80000/5000 = 16), so W up to 4
# actually splits the table and writes W distinct files.
psql_run "DROP TABLE IF EXISTS t_col;
          CREATE TABLE t_col ($COLS) USING pgcolumnar;
          SELECT pgcolumnar.set_options('t_col'::regclass, stripe_row_limit => 5000);
          INSERT INTO t_col $GEN_COL;" >/dev/null
SRC="$(pgc_set_hash "$GEN_COL")"
NSRC=80000

# serial export is a second oracle
SER="$PGC_WORKDIR/serial.parquet"
q "SELECT pgcolumnar.export_parquet('t_col'::regclass, '$SER')" >/dev/null
SER_HASH="$(pgc_set_hash "SELECT * FROM pgcolumnar.read_parquet('$SER') AS t($RB)")"
check "serial export read-back == source" "$SER_HASH" "$SRC"

# export_single W: export t_col with W workers and take every reading. The checks
# are written out per W below, so every check name is literal.
export_single() {
	local d="$PGC_WORKDIR/st_$1"
	ES_N="$(q "SELECT pgcolumnar.parallel_export_parquet('t_col'::regclass, '$d', $1)")"
	ES_RB="$(pgc_set_hash "SELECT * FROM pgcolumnar.read_parquet('$d') AS t($RB)")"
	ES_FILES="$(nfiles "$d")"
	# #394: a completed export writes a _SUCCESS marker. The file count already
	# proves it is not counted as a part, and read-back==source proves read_parquet
	# ignores it; here assert it is present and empty.
	ES_SUCCESS="$([ -f "$d/_SUCCESS" ] && echo yes || echo no)"
	ES_SUCCESS_EMPTY="$([ -f "$d/_SUCCESS" ] && [ ! -s "$d/_SUCCESS" ] && echo empty || echo no)"
}

export_single 1
check_num "single(1): rows returned == source count" "$ES_N" "$NSRC"
check "single(1): read-back == source" "$ES_RB" "$SRC"
check "single(1): read-back == serial export" "$ES_RB" "$SER_HASH"
check_num "single(1): wrote exactly 1 files (distinct per worker)" "$ES_FILES" 1
check "single(1): a _SUCCESS completion marker is written" "$ES_SUCCESS" yes
check "single(1): the _SUCCESS marker is empty" "$ES_SUCCESS_EMPTY" empty

export_single 2
check_num "single(2): rows returned == source count" "$ES_N" "$NSRC"
check "single(2): read-back == source" "$ES_RB" "$SRC"
check "single(2): read-back == serial export" "$ES_RB" "$SER_HASH"
check_num "single(2): wrote exactly 2 files (distinct per worker)" "$ES_FILES" 2
check "single(2): a _SUCCESS completion marker is written" "$ES_SUCCESS" yes
check "single(2): the _SUCCESS marker is empty" "$ES_SUCCESS_EMPTY" empty

export_single 4
check_num "single(4): rows returned == source count" "$ES_N" "$NSRC"
check "single(4): read-back == source" "$ES_RB" "$SRC"
check "single(4): read-back == serial export" "$ES_RB" "$SER_HASH"
check_num "single(4): wrote exactly 4 files (distinct per worker)" "$ES_FILES" 4
check "single(4): a _SUCCESS completion marker is written" "$ES_SUCCESS" yes
check "single(4): the _SUCCESS marker is empty" "$ES_SUCCESS_EMPTY" empty

# ---- partitioned columnar table: one file per partition ---------------------
# k = g%600 -> t_p3 (750..) gets no rows, so one partition is empty.
psql_run "DROP TABLE IF EXISTS t_part CASCADE;
          CREATE TABLE t_part ($COLS) PARTITION BY RANGE (k);
          CREATE TABLE t_p0 PARTITION OF t_part FOR VALUES FROM (0) TO (250) USING pgcolumnar;
          CREATE TABLE t_p1 PARTITION OF t_part FOR VALUES FROM (250) TO (500) USING pgcolumnar;
          CREATE TABLE t_p2 PARTITION OF t_part FOR VALUES FROM (500) TO (750) USING pgcolumnar;
          CREATE TABLE t_p3 PARTITION OF t_part FOR VALUES FROM (750) TO (2000) USING pgcolumnar;
          INSERT INTO t_part $GEN_PART;" >/dev/null
PSRC="$(pgc_set_hash "$GEN_PART")"
NPART=80000

export_part() {	# export_part W: as export_single, for the partitioned table
	local d="$PGC_WORKDIR/pt_$1"
	EP_N="$(q "SELECT pgcolumnar.parallel_export_parquet('t_part'::regclass, '$d', $1)")"
	EP_RB="$(pgc_set_hash "SELECT * FROM pgcolumnar.read_parquet('$d') AS t($RB)")"
	EP_FILES="$(nfiles "$d")"
}

export_part 1
check_num "part(1): rows returned == source count" "$EP_N" "$NPART"
check "part(1): read-back == source" "$EP_RB" "$PSRC"
check_num "part(1): one file per partition (4, incl the empty one)" "$EP_FILES" 4

export_part 2
check_num "part(2): rows returned == source count" "$EP_N" "$NPART"
check "part(2): read-back == source" "$EP_RB" "$PSRC"
check_num "part(2): one file per partition (4, incl the empty one)" "$EP_FILES" 4

export_part 4
check_num "part(4): rows returned == source count" "$EP_N" "$NPART"
check "part(4): read-back == source" "$EP_RB" "$PSRC"
check_num "part(4): one file per partition (4, incl the empty one)" "$EP_FILES" 4

# ---- empty input: a valid zero-row file, read-back is the empty set ---------
# pgc_set_hash returns EMPTY for a genuinely empty result, and a unique
# QUERY_ERROR.<n> for a failed one.
psql_run "DROP TABLE IF EXISTS t_empty; CREATE TABLE t_empty ($COLS) USING pgcolumnar;" >/dev/null
DE="$PGC_WORKDIR/empty"
ne="$(q "SELECT pgcolumnar.parallel_export_parquet('t_empty'::regclass, '$DE', 2)")"
check_num "empty: rows returned == 0" "$ne" 0
check "empty: read-back is the empty set" \
	"$(pgc_set_hash "SELECT * FROM pgcolumnar.read_parquet('$DE') AS t($RB)")" EMPTY

# ---- consistency: export INSIDE a transaction with uncommitted rows ---------
# All workers import ONE exported snapshot, so the export is the committed image
# at call time: no duplication, and rows the caller wrote but did not commit are
# absent. This is jdatcmd's #329 repro; a broken snapshot handoff duplicated the
# table and dropped the caller's rows, and no autocommit fixture could see it.
psql_run "DROP TABLE IF EXISTS t_tx;
          CREATE TABLE t_tx ($COLS) USING pgcolumnar;
          SELECT pgcolumnar.set_options('t_tx'::regclass, stripe_row_limit => 3000);
          INSERT INTO t_tx $GEN_TX_COMMITTED;" >/dev/null
NC=20000		# committed rows, from the generator
DTX="$PGC_WORKDIR/tx"
# one psql session, one transaction: uncommitted INSERT, then export, then commit
psql_run "BEGIN;
          INSERT INTO t_tx SELECT g, g, g::float8/7, 'u'||g FROM generate_series(100001,100500) g;
          SELECT pgcolumnar.parallel_export_parquet('t_tx'::regclass, '$DTX', 4);
          COMMIT;" >/dev/null
check_num "in-txn export: read-back count == committed count (no duplication)" \
	"$(q "SELECT count(*) FROM pgcolumnar.read_parquet('$DTX') AS t($RB)")" "$NC"
check_num "in-txn export: no duplicate ids in the read-back" \
	"$(q "SELECT count(DISTINCT id) FROM pgcolumnar.read_parquet('$DTX') AS t($RB)")" "$NC"
check "in-txn export: read-back == committed rows (uncommitted rows absent)" \
	"$(pgc_set_hash "SELECT * FROM pgcolumnar.read_parquet('$DTX') AS t($RB)")" \
	"$(pgc_set_hash "$GEN_TX_COMMITTED")"

# ---- item 2: a cancelled/failed export leaves a clean directory --------------
# On failure the dispatcher removes the *.parquet it wrote, so read_parquet cannot
# union a partial set as if complete and a retry is not blocked by require-empty.
# Deterministic, not a timer race: run a big export in the background, WAIT until a
# part file is actually on disk (so cleanup has something to remove and we know the
# run reached execution), then cancel the dispatcher from this session. The table
# is large enough that the export is still running when the first file appears.
# control: an export writes files, so "0 after cancel" means cleanup, not "never wrote".
q "SELECT pgcolumnar.parallel_export_parquet('t_col'::regclass, '$PGC_WORKDIR/cx_ok', 2)" >/dev/null
check "cancel-control: a completed export writes files" \
	"$([ "$(nfiles "$PGC_WORKDIR/cx_ok")" -ge 1 ] && echo yes || echo no)" yes
psql_run "DROP TABLE IF EXISTS t_big;
          CREATE TABLE t_big ($COLS) USING pgcolumnar;
          SELECT pgcolumnar.set_options('t_big'::regclass, stripe_row_limit => 4000);
          INSERT INTO t_big SELECT g, g%1000, g::float8/7, 'r'
                            FROM generate_series(1,20000000) g;" >/dev/null
DCX="$PGC_WORKDIR/cx"
BGLOG="$PGC_WORKDIR/cx_bg.log"
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq \
	-c "SELECT pgcolumnar.parallel_export_parquet('t_big'::regclass, '$DCX', 4)" >"$BGLOG" 2>&1 &
bgpid=$!
wrote=no
for i in $(seq 1 200); do
	[ "$(anyfiles "$DCX")" -ge 1 ] && { wrote=yes; break; }
	sleep 0.1
done
# anyfiles, not nfiles: since the #394 sink, a part's FINAL name appears only at
# completion, so waiting for *.parquet meant waiting for a whole part to finish
# and the cancel raced the end of the run. The temp appears at worker open,
# which is the earliest "the run reached execution" signal there is.
check "a file was on disk before the cancel (cleanup has work to do)" "$wrote" yes
q "SELECT pg_cancel_backend(pid) FROM pg_stat_activity
   WHERE query LIKE '%parallel_export_parquet%' AND state = 'active'
     AND pid <> pg_backend_pid()" >/dev/null
wait "$bgpid" 2>/dev/null || true
# assert the premise: the run was cancelled mid-flight, not completed before we
# could cancel. If a very fast runner ever finishes 20M rows before the first poll
# tick + cancel land, THIS line fails loudly (pointing at the fixture) instead of
# the file-count check failing as if cleanup were broken. Grow t_big if it does.
check "the export was cancelled mid-flight, not completed first (fixture premise)" \
	"$(grep -qiE 'canceling statement|canceled on user request' "$BGLOG" && echo cancelled || echo completed)" cancelled
# anyfiles: completed parts are removed by the dispatcher, and in-flight temps
# by each worker's own sink abort (#394) - a cancelled export leaves NOTHING.
check "a cancelled export leaves no partial files (item 2)" \
	"$(anyfiles "$DCX")" 0
# #394: the completion marker is written LAST, only after every worker finishes,
# so a cancelled run leaves none -- the absence of _SUCCESS is what tells a
# consumer the run did not complete.
check "a cancelled export writes no _SUCCESS marker" \
	"$([ -e "$DCX/_SUCCESS" ] && echo present || echo absent)" absent
# and the cleaned directory is reusable (require-empty does not block a retry)
q "SELECT pgcolumnar.parallel_export_parquet('t_col'::regclass, '$DCX', 2)" >/dev/null 2>&1
check "retry into the cleaned directory succeeds" \
	"$([ "$(nfiles "$DCX")" -ge 1 ] && echo yes || echo no)" yes
check "the retry writes a fresh _SUCCESS marker" \
	"$([ -f "$DCX/_SUCCESS" ] && echo yes || echo no)" yes

# A local cancel must wait for the workers to exit before it deletes the parts,
# as a remote one does (#1864 review). A terminated worker stops only at its next
# interrupt check, and the sink renames part-N.parquet.tmp.PID to its final name
# with no check between fsync and rename. The dispatcher's cleanup lists the
# directory and then unlinks, so a rename between the two left a final part. That
# window is too short to hit on demand, so this check holds one worker stopped
# (SIGSTOP) across the cancel and resumes it 2 s later. The cancel must not return
# while that worker is still running.
DLW="$PGC_WORKDIR/cxw"
LWLOG="$PGC_WORKDIR/cxw_bg.log"
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq \
	-c "SELECT pgcolumnar.parallel_export_parquet('t_big'::regclass, '$DLW', 4)" >"$LWLOG" 2>&1 &
lwpid=$!
for i in $(seq 1 200); do
	[ "$(anyfiles "$DLW")" -ge 1 ] && break
	sleep 0.1
done
lw_worker="$(q "SELECT pid FROM pg_stat_activity
                WHERE backend_type = 'pgcolumnar parallel_export worker' LIMIT 1")"
lw_stopped=no
if [[ "$lw_worker" =~ ^[0-9]+$ ]] && kill -STOP "$lw_worker" 2>/dev/null; then
	lw_stopped=yes
	( sleep 2; kill -CONT "$lw_worker" 2>/dev/null ) &
	lw_cont=$!
fi
q "SELECT pg_cancel_backend(pid) FROM pg_stat_activity
   WHERE query LIKE '%parallel_export_parquet%' AND state = 'active'
     AND pid <> pg_backend_pid()" >/dev/null
wait "$lwpid" 2>/dev/null || true
lw_alive=0
[ "$lw_stopped" = yes ] && kill -0 "$lw_worker" 2>/dev/null && lw_alive=1
[ -n "${lw_cont:-}" ] && wait "$lw_cont" 2>/dev/null
check "premise: local stop: a worker was stopped before the cancel" \
	"$([ "$lw_stopped" = yes ] && echo yes || echo "no (worker pid [$lw_worker])")" yes
check "premise: local stop: the export was cancelled, not completed" \
	"$(grep -qiE 'canceling statement|canceled on user request' "$LWLOG" && echo cancelled || echo "completed ($(head -1 "$LWLOG"))")" cancelled
check_num "local stop: no worker is still running when the cancel returns" "$lw_alive" 0

# ---- destination-length guard (#863) ----------------------------------------
# A destination that fits by itself but not with a generated suffix used to be
# silently truncated: the export returned success and stamped _SUCCESS beside a
# clipped part name, which read_parquet ignores.
#
# The guard has to probe the LONGEST path this file builds into a fixed
# MAXPGPATH buffer, and that is NOT the final part name. pexport_remove_outputs()
# composes "%s/%s" from the directory and a directory entry, and the entries it
# unlinks include the sink's in-flight form part-NNNN.parquet.tmp.<pid>:
#
#     final part name   "/part-2147483647.parquet"           dir + 24
#     cleanup scan      "/part-0000.parquet.tmp.1234567"     dir + 30
#
# 7-digit pids are reachable here (/proc/sys/kernel/pid_max is 4194304), so a
# guard that probes only the part name accepts dir lengths 994..999 while the
# cleanup scan's buffer truncates a path it then unlinks. The probe must be the
# longest constructed form, "/part-<int>.parquet.tmp.<int>", which is 39 bytes
# at INT_MAX for both numbers -- so the longest acceptable destination is
# MAXPGPATH - 1 - 39.
#
# Both boundaries are asserted: one byte under must still export (this class of
# fix breaks by becoming over-broad and rejecting legal paths) and one byte over
# must be refused with ERRCODE_PROGRAM_LIMIT_EXCEEDED, by SQLSTATE -- "it
# failed" also passes for a typo, a missing table or a dead server.

PEXPORT_TOO_LONG_SQLSTATE=54000		# ERRCODE_PROGRAM_LIMIT_EXCEEDED

# premise 1: the arithmetic is written for MAXPGPATH == 1024.
PGC_MAXPGPATH="$(sed -n 's/^#define[[:space:]]\{1,\}MAXPGPATH[[:space:]]\{1,\}\([0-9]\{1,\}\).*/\1/p' \
	"$("$PGC_PG_CONFIG" --includedir-server)/pg_config_manual.h" | head -1)"
if [ "$PGC_MAXPGPATH" != "1024" ]; then
	echo "PREMISE FAILED: MAXPGPATH is [$PGC_MAXPGPATH]; these arms are written for 1024" >&2
	exit 1
fi

# premise 2: the cleanup scan still builds dir + "/" + entry into a fixed
# buffer, and the sink still appends ".tmp.<pid>" to the part name. If either
# moves, the +30 above is stale and these lengths measure nothing.
pexport_anchor="$(grep -c 'snprintf(fp, sizeof(fp), "%s/%s", dir, de->d_name);' \
	"$PGC_SRCDIR/src/columnar_parallel_export.c")"
sink_anchor="$(grep -c 'psprintf("%s.tmp.%d", path, MyProcPid)' \
	"$PGC_SRCDIR/src/columnar_sink.c")"
if [ "$pexport_anchor" != "1" ] || [ "$sink_anchor" != "1" ]; then
	echo "PREMISE FAILED: cleanup-scan anchor [$pexport_anchor] sink .tmp anchor [$sink_anchor], wanted 1 and 1" >&2
	exit 1
fi

# Longest destination that still holds the longest generated path, and the
# shortest destination at which the cleanup scan's "%s/%s" truncates with a
# 7-digit pid (len("/part-0000.parquet.tmp.1234567") == 30).
PEXPORT_LEN_OK=$(( PGC_MAXPGPATH - 1 - 39 ))		# 984
PEXPORT_LEN_OVER=$(( PEXPORT_LEN_OK + 1 ))		# 985
PEXPORT_LEN_WINDOW=$(( PGC_MAXPGPATH - 30 ))		# 994

# Build a directory path of an EXACT length and create its parents 777, so the
# server (running as postgres) can create the final component itself. Sets
# LP_PATH; it does not echo, because an exit inside a command substitution would
# leave the suite running with an ungated premise.
LP_PATH=""
path_of_len() {
	local want="$1" p="$PGC_WORKDIR/lp" rem
	while [ $(( want - ${#p} - 1 )) -gt 255 ]; do
		p="$p/$(printf 'a%.0s' $(seq 1 200))"
	done
	rem=$(( want - ${#p} - 1 ))
	if [ "$rem" -lt 1 ]; then
		echo "PREMISE FAILED: cannot build a ${want}-byte path under $PGC_WORKDIR" >&2
		exit 1
	fi
	p="$p/$(printf 'a%.0s' $(seq 1 "$rem"))"
	if [ "${#p}" -ne "$want" ]; then
		echo "PREMISE FAILED: built a ${#p}-byte path, wanted $want" >&2
		exit 1
	fi
	mkdir -p "$(dirname "$p")" || { echo "PREMISE FAILED: mkdir -p for $want failed" >&2; exit 1; }
	chmod -R 777 "$PGC_WORKDIR/lp" || { echo "PREMISE FAILED: chmod for $want failed" >&2; exit 1; }
	if [ ! -d "$(dirname "$p")" ] || [ -e "$p" ]; then
		echo "PREMISE FAILED: parent missing or target already present for $want" >&2
		exit 1
	fi
	LP_PATH="$p"
}

# Run one statement ONCE and record its 5-char SQLSTATE, "none" when it
# succeeded, or HANG on the wall-clock cap. VERBOSITY verbose puts the SQLSTATE
# and the message on the same ERROR line, so one run yields both, and a caller
# can assert the message without executing the statement a second time -- a
# second run of an accepted export hits the require-empty check and returns a
# DIFFERENT sqlstate (55000), which is exactly how "it failed" lies.
#
# Sets globals rather than echoing: a global assigned inside $( ) is assigned in
# a subshell and never reaches the caller, so SQLSTATE_LAST_OUT would arrive
# empty and its message check would silently compare nothing.
SQLSTATE_LAST=""
SQLSTATE_LAST_OUT=""
run_sqlstate() {
	local rc
	SQLSTATE_LAST=""
	SQLSTATE_LAST_OUT="$(timeout -s KILL 180 env PATH="$PGC_BINDIR:$PATH" psql \
		-h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -qtA 2>&1 <<SQLEOF
\\set VERBOSITY verbose
$1;
SQLEOF
)"
	rc=$?
	if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then SQLSTATE_LAST=HANG; return; fi
	# "none" needs psql to have run the statement: a dead server prints
	# "psql: error: ..." and exits 2, which used to read as "none", so the
	# accepted-boundary check passed with the server gone.
	if grep -q '^ERROR:' <<<"$SQLSTATE_LAST_OUT"; then
		SQLSTATE_LAST="$(printf '%s\n' "$SQLSTATE_LAST_OUT" |
			sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\):.*/\1/p' | head -1)"
	elif [ "$rc" -eq 0 ]; then
		SQLSTATE_LAST=none
	else
		SQLSTATE_LAST="no reading (psql exit $rc: $(head -1 <<<"$SQLSTATE_LAST_OUT"))"
	fi
}

# not_created DIR -> "absent" when DIR does not exist AND the statement just run by
# run_sqlstate reached the server; with the server gone nothing could create DIR,
# and "absent" was a pass that measured nothing.
not_created() {
	if [ -e "$1" ]; then echo created
	elif [ "$SQLSTATE_LAST" = none ] || [[ "$SQLSTATE_LAST" =~ ^[0-9A-Z]{5}$ ]]; then echo absent
	else echo "no reading (the statement did not run: $SQLSTATE_LAST)"; fi
}

# -- accepted boundary: one byte under the limit must still export ------------
path_of_len "$PEXPORT_LEN_OK"
PEXPORT_DIR_OK="$LP_PATH"
check_num "accepted boundary: the destination is exactly MAXPGPATH-40 bytes" \
	"${#PEXPORT_DIR_OK}" "$PEXPORT_LEN_OK"
run_sqlstate "SELECT pgcolumnar.parallel_export_parquet('t_col'::regclass, '$PEXPORT_DIR_OK', 2)"
check "accepted boundary: a 984-byte destination still exports" "$SQLSTATE_LAST" none
check_num "accepted boundary: it wrote both part files" "$(nfiles "$PEXPORT_DIR_OK")" 2
check "accepted boundary: it wrote a _SUCCESS marker" \
	"$([ -f "$PEXPORT_DIR_OK/_SUCCESS" ] && echo yes || echo no)" yes

# -- rejected boundary: one byte over the limit --------------------------------
path_of_len "$PEXPORT_LEN_OVER"
PEXPORT_DIR_OVER="$LP_PATH"
check_num "rejected boundary: the destination is exactly MAXPGPATH-39 bytes" \
	"${#PEXPORT_DIR_OVER}" "$PEXPORT_LEN_OVER"
run_sqlstate "SELECT pgcolumnar.parallel_export_parquet('t_col'::regclass, '$PEXPORT_DIR_OVER', 2)"
check "rejected boundary: a 985-byte destination raises 54000" \
	"$SQLSTATE_LAST" "$PEXPORT_TOO_LONG_SQLSTATE"
check "rejected boundary: the destination was not created" \
	"$(not_created "$PEXPORT_DIR_OVER")" absent

# -- the window the part-name-only probe left open ----------------------------
# dir + 30 overruns the cleanup scan's MAXPGPATH buffer here, while
# dir + 24 (the final part name) still fits, so the old probe accepted this.
path_of_len "$PEXPORT_LEN_WINDOW"
PEXPORT_DIR_WINDOW="$LP_PATH"
check_num "cleanup-scan window: the destination is exactly MAXPGPATH-30 bytes" \
	"${#PEXPORT_DIR_WINDOW}" "$PEXPORT_LEN_WINDOW"
run_sqlstate "SELECT pgcolumnar.parallel_export_parquet('t_col'::regclass, '$PEXPORT_DIR_WINDOW', 2)"
check "cleanup-scan window: a 994-byte destination raises 54000" \
	"$SQLSTATE_LAST" "$PEXPORT_TOO_LONG_SQLSTATE"
check "cleanup-scan window: and it is OUR message, not another 54000" \
	"$(grep -qi 'destination is too long' <<<"$SQLSTATE_LAST_OUT" && echo ours || echo other)" ours
check "cleanup-scan window: the destination was not created" \
	"$(not_created "$PEXPORT_DIR_WINDOW")" absent

# ---- error cases ------------------------------------------------------------
# Each refusal is matched by its own message. With the non-columnar branch removed,
# a heap target still failed, on another error, and the old check passed.
# st_1 was written above, so it is non-empty
check "reject a non-empty output directory" \
	"$(refusal_of "SELECT pgcolumnar.parallel_export_parquet('t_col'::regclass, '$PGC_WORKDIR/st_1', 2)" \
		'output directory ".*" is not empty')" refused
psql_run "DROP TABLE IF EXISTS t_heap2; CREATE TABLE t_heap2 ($COLS);" >/dev/null
check "reject a non-columnar target" \
	"$(refusal_of "SELECT pgcolumnar.parallel_export_parquet('t_heap2'::regclass, '$PGC_WORKDIR/heaptgt', 2)" \
		'is not a columnar table or a partitioned table with columnar partitions')" refused

# ---- the workers use the caller's export_group_bytes (#1843 review) ----------
# The workers are background workers, which start with the server's settings, so a
# session's SET of the byte cap did not reach them. 4000 rows of 3000 bytes, 1000 rows
# a stripe, two workers of 2000 rows each. Each row buffers 3010 bytes (id: 4 + 1 level
# byte; s: 4 + 3000 + 1), so under a 65536-byte cap a group holds at most 21 rows.
# The reading is "rows returned|rows read back|the most rows in any row group, over all
# part files".
psql_run "DROP TABLE IF EXISTS t_cap; CREATE TABLE t_cap (id int, s text) USING pgcolumnar;
          SELECT pgcolumnar.set_options('t_cap'::regclass, stripe_row_limit => 1000);
          INSERT INTO t_cap SELECT g, repeat('x', 3000) FROM generate_series(1, 4000) g;" >/dev/null
cap_n="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At \
	-c "SET pgcolumnar.export_group_bytes = 65536" \
	-c "SELECT pgcolumnar.parallel_export_parquet('t_cap'::regclass, '$PGC_WORKDIR/capdir', 2)" 2>&1 | tail -1)"
cap_groups="$(python3 - "$PGC_WORKDIR/capdir" <<'PY'
import glob, sys
try:
    import pyarrow.parquet as pq
    rows, most = 0, 0
    for f in sorted(glob.glob(sys.argv[1] + "/part-*.parquet")):
        md = pq.ParquetFile(f).metadata
        rows += md.num_rows
        most = max([most] + [md.row_group(i).num_rows for i in range(md.num_row_groups)])
    print("%d|%d" % (rows, most))
except Exception as e:
    print("unreadable: %s" % e)
PY
)"
check "the workers cut row groups at the caller's export_group_bytes: 4000 rows of 3000 bytes under a 65536-byte cap, at most 21 rows a group" \
	"$cap_n|$cap_groups" "4000|4000|21"

# ---- a lock the caller holds is refused, not waited on (#2183) ---------------
# The workers open the table, or each leaf, with ACCESS SHARE in their own
# transactions. They are not in the caller's lock group, and the caller waits for
# them on a latch, so the deadlock detector cannot see the wait: after LOCK TABLE
# ... IN ACCESS EXCLUSIVE MODE or TRUNCATE in the caller's transaction, the export
# waited until statement_timeout. It is now refused up front with 55000, naming the
# lock and the relation. A lock the workers' ACCESS SHARE does not conflict with
# (SHARE on the table, ACCESS EXCLUSIVE on another table) must still export, and a
# lock on one leaf of a partitioned table is refused like one on the table.
# Every run gets a 5s statement_timeout, so the old wait reads 57014, not a hang.
#
# self_lock_export SETUP TABLE DIR -> "SQLSTATE|message" of the first ERROR, or
# "none|<rows returned>" when BEGIN; SETUP; the export of TABLE; COMMIT ran.
psql_run "DROP TABLE IF EXISTS t_lk, t_lk_other;
          CREATE TABLE t_lk ($COLS) USING pgcolumnar;
          INSERT INTO t_lk $GEN_TX_COMMITTED;
          CREATE TABLE t_lk_other (a int);" >/dev/null
self_lock_export() {
	local out rc
	out="$(timeout -s KILL 60 env PATH="$PGC_BINDIR:$PATH" psql -X -h 127.0.0.1 -p "$PGC_PORT" \
		-U postgres -d "$PGC_DB" -qtA -v ON_ERROR_STOP=1 \
		-c "SET statement_timeout = '5s'" -c '\set VERBOSITY verbose' \
		-c "BEGIN; $1; SELECT pgcolumnar.parallel_export_parquet('$2'::regclass, '$3', 2); COMMIT;" 2>&1)"
	rc=$?
	if grep -q '^ERROR:' <<<"$out"; then
		sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\): \(.*\)$/\1|\2/p' <<<"$out" | head -1
	elif [ "$rc" -eq 0 ]; then
		echo "none|$(tail -1 <<<"$out")"
	else
		echo "no reading (psql exit $rc: $(head -1 <<<"$out"))"
	fi
}
LK_REFUSED='55000|pgcolumnar.parallel_export_parquet cannot run while this transaction holds AccessExclusiveLock on "t_lk"'
check "parallel_export_parquet after LOCK TABLE IN ACCESS EXCLUSIVE MODE in the same transaction is refused with 55000, not waited on (#2183)" \
	"$(self_lock_export "LOCK TABLE t_lk IN ACCESS EXCLUSIVE MODE" t_lk "$PGC_WORKDIR/lk_ael")" "$LK_REFUSED"
check "parallel_export_parquet after TRUNCATE in the same transaction is refused with 55000, not waited on (#2183)" \
	"$(self_lock_export "TRUNCATE t_lk" t_lk "$PGC_WORKDIR/lk_trunc")" "$LK_REFUSED"
check "parallel_export_parquet after LOCK TABLE IN SHARE MODE in the same transaction still exports (#2183)" \
	"$(self_lock_export "LOCK TABLE t_lk IN SHARE MODE" t_lk "$PGC_WORKDIR/lk_share")" "none|$NC"
check "parallel_export_parquet while the transaction holds ACCESS EXCLUSIVE on another table still exports (#2183)" \
	"$(self_lock_export "LOCK TABLE t_lk_other IN ACCESS EXCLUSIVE MODE" t_lk "$PGC_WORKDIR/lk_other")" "none|$NC"
check "parallel_export_parquet of a partitioned table after LOCK TABLE on one leaf IN ACCESS EXCLUSIVE MODE is refused with 55000 (#2183)" \
	"$(self_lock_export "LOCK TABLE t_p1 IN ACCESS EXCLUSIVE MODE" t_part "$PGC_WORKDIR/lk_leaf")" \
	'55000|pgcolumnar.parallel_export_parquet cannot run while this transaction holds AccessExclusiveLock on "t_p1"'
# An export can never run in a subtransaction: ExportSnapshot refuses it with
# 25001 before any worker starts. The lock refusal must not hide that error behind
# a 55000 whose hint cannot help (#2212 review).
check "parallel_export_parquet in a savepoint after LOCK TABLE IN ACCESS EXCLUSIVE MODE keeps the subtransaction refusal 25001 (#2183)" \
	"$(self_lock_export "SAVEPOINT s; LOCK TABLE t_lk IN ACCESS EXCLUSIVE MODE" t_lk "$PGC_WORKDIR/lk_subx")" \
	'25001|cannot export a snapshot from a subtransaction'

# ---- #2245: the caller waits for its workers in the lock manager -------------
# The dispatcher holds ACCESS SHARE on the target, and each worker asks for it
# again in its own transaction. An ACCESS EXCLUSIVE request (LOCK TABLE,
# TRUNCATE, ALTER TABLE, ...) that arrives in between waits for the caller, and
# the lock manager queues each worker behind it. The caller waited for its
# workers on a latch, which the deadlock detector does not follow, so the export
# waited until statement_timeout. Each worker now holds an advisory lock of class
# 108 from the moment it connects, and the caller waits on those, so the detector
# sees the cycle and moves the workers ahead of the queued request. Here the
# caller already holds ACCESS SHARE from a SELECT earlier in its transaction,
# which makes the window deterministic. Every wait is bounded: 10 s for the
# caller, 20 s for the other session.
qx_sess() {	# APPNAME PSQL-ARGS... -> psql's output, errors with their SQLSTATE
	local app="$1"; shift
	env PGAPPNAME="$app" PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" \
		-X -qAt -v VERBOSITY=verbose "$@" 2>&1
}
qx_err() {	# FILE -> "SQLSTATE message" of the first ERROR in a qx_sess output, or nothing
	sed -n '/^ERROR:/{s/^ERROR: *\([0-9A-Z]\{5\}\): \(.*\)$/\1 \2/p;q;}' "$1"
}
qx_result() {	# FILE -> the first ERROR, or "no error (<the last number printed>)"
	local e
	e="$(qx_err "$1")"
	if [ -n "$e" ]; then echo "$e"; else echo "no error ($(grep -x '[0-9]*' "$1" | tail -1))"; fi
}
qx_wait_lock() {	# APPNAME TABLE MODE -> returns once that session holds MODE on TABLE (up to 5 s)
	for _ in $(seq 100); do
		[ "$(q "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a USING (pid)
		        WHERE a.application_name = '$1' AND l.relation = '$2'::regclass AND l.mode = '$3' AND l.granted")" = 1 ] && return
		sleep 0.05
	done
}
qx_ms() { echo $(( ($(date +%s%N) - $1) / 1000000 )); }
qx_under() {	# MS LIMIT-S -> "under LIMIT s" or "took MS ms"
	if [ "$1" -lt $(( $2 * 1000 )) ]; then echo "under $2 s"; else echo "took $1 ms"; fi
}
qx_queue() {	# TARGET LOCKED DIR -> "export | time | other session | rows read back". The
	# caller selects from LOCKED, waits until an ACCESS EXCLUSIVE request on LOCKED
	# queues behind it, then exports TARGET with 2 workers.
	local t0 ms bp
	qx_sess pexport_qx_b -c "BEGIN" -c "SELECT count(*) FROM $2" \
		-c "DO \$\$ BEGIN FOR i IN 1..400 LOOP EXIT WHEN EXISTS (SELECT 1 FROM pg_locks WHERE relation = '$2'::regclass AND mode = 'AccessExclusiveLock' AND NOT granted); PERFORM pg_sleep(0.02); END LOOP; END \$\$" \
		-c "SET statement_timeout = '10s'" -c "SELECT pgcolumnar.parallel_export_parquet('$1'::regclass, '$3', 2)" \
		-c "COMMIT" >"$PGC_WORKDIR/qx_b.out" &
	bp=$!
	qx_wait_lock pexport_qx_b "$2" AccessShareLock
	t0=$(date +%s%N)
	qx_sess pexport_qx_a -c "SET statement_timeout = '20s'" -c "BEGIN" -c "LOCK TABLE $2 IN ACCESS EXCLUSIVE MODE" \
		-c "COMMIT" >"$PGC_WORKDIR/qx_a.out"
	wait "$bp"
	ms=$(qx_ms "$t0")
	printf '%s | %s | %s | %s rows read back\n' "$(qx_result "$PGC_WORKDIR/qx_b.out")" "$(qx_under "$ms" 6)" \
		"$(e="$(qx_err "$PGC_WORKDIR/qx_a.out")"; echo "other session ${e:-locked}")" \
		"$(q "SELECT count(*) FROM pgcolumnar.read_parquet('$3') AS t($RB)")"
}
check "parallel_export_parquet with ACCESS EXCLUSIVE on the table queued behind the caller exports within 6 s, not a wait until statement_timeout, and the other session then gets its lock (#2245)" \
	"$(qx_queue t_lk t_lk "$PGC_WORKDIR/qx_single")" "no error ($NC) | under 6 s | other session locked | $NC rows read back"
check "parallel_export_parquet of a partitioned table with ACCESS EXCLUSIVE on one leaf queued behind the caller exports within 6 s, not a wait until statement_timeout, and the other session then gets its lock (#2245)" \
	"$(qx_queue t_part t_p1 "$PGC_WORKDIR/qx_leaf")" "no error ($NPART) | under 6 s | other session locked | $NPART rows read back"
# control: a session that holds ACCESS EXCLUSIVE on the table and then waits for
# a lock the caller holds already made a cycle the detector saw, since the
# dispatcher waits for the table in the lock manager before any worker starts.
qx_cycle() {	# -> "outcome | time"
	local t0 ms ap ea eb
	qx_sess pexport_qx_c -c "SET statement_timeout = '20s'" -c "BEGIN" -c "LOCK TABLE t_lk IN ACCESS EXCLUSIVE MODE" \
		-c "DO \$\$ BEGIN FOR i IN 1..400 LOOP EXIT WHEN EXISTS (SELECT 1 FROM pg_locks WHERE relation = 't_lk'::regclass AND NOT granted); PERFORM pg_sleep(0.02); END LOOP; END \$\$" \
		-c "LOCK TABLE t_lk_other IN SHARE MODE" -c "COMMIT" >"$PGC_WORKDIR/qx_c.out" &
	ap=$!
	qx_wait_lock pexport_qx_c t_lk AccessExclusiveLock
	t0=$(date +%s%N)
	qx_sess pexport_qx_d -c "BEGIN" -c "INSERT INTO t_lk_other VALUES (1)" -c "SET statement_timeout = '10s'" \
		-c "SELECT pgcolumnar.parallel_export_parquet('t_lk'::regclass, '$PGC_WORKDIR/qx_cycle', 2)" \
		-c "ROLLBACK" >"$PGC_WORKDIR/qx_d.out"
	wait "$ap"
	ms=$(qx_ms "$t0")
	ea="$(qx_err "$PGC_WORKDIR/qx_c.out")"; eb="$(qx_err "$PGC_WORKDIR/qx_d.out")"
	printf '%s | %s\n' \
		"$(if { [ "${ea%% *}" = 40P01 ] && [ -z "$eb" ]; } || { [ "${eb%% *}" = 40P01 ] && [ -z "$ea" ]; }; then
			echo "one side 40P01, the other went on"; else echo "A ${ea:-committed}, B ${eb:-exported}"; fi)" \
		"$(qx_under "$ms" 5)"
}
check "control: a session holding ACCESS EXCLUSIVE on the table that then waits for a lock the parallel_export_parquet caller holds gets 40P01 on one side within 5 s (#2245)" \
	"$(qx_cycle)" "one side 40P01, the other went on | under 5 s"
# The caller's lock_timeout still bounds its own wait for the table.
qx_lock_timeout() {	# -> "error | time"
	local t0 ms
	qx_sess pexport_qx_h -c "BEGIN" -c "LOCK TABLE t_lk IN ACCESS EXCLUSIVE MODE" -c "SELECT pg_sleep(6)" -c "COMMIT" >/dev/null &
	qx_wait_lock pexport_qx_h t_lk AccessExclusiveLock
	t0=$(date +%s%N)
	PGOPTIONS='-c statement_timeout=10s -c lock_timeout=1s' qx_sess pexport_qx_t \
		-c "SELECT pgcolumnar.parallel_export_parquet('t_lk'::regclass, '$PGC_WORKDIR/qx_lt', 2)" >"$PGC_WORKDIR/qx_t.out"
	ms=$(qx_ms "$t0")
	wait
	printf '%s | %s\n' "$(qx_result "$PGC_WORKDIR/qx_t.out")" "$(qx_under "$ms" 4)"
}
check "the caller's lock_timeout of 1 s ends a parallel_export_parquet that waits for another session's ACCESS EXCLUSIVE on the table with 55P03 within 4 s (#2245)" \
	"$(qx_lock_timeout)" "55P03 canceling statement due to lock timeout | under 4 s"
# The waits on the workers are waits for the export, not lock contention: a
# 1 ms lock_timeout does not end an uncontended export, and with
# deadlock_timeout 1 ms and log_lock_waits on, they are not logged.
qx_lock_waits_logged() { pgc_pg "grep -c 'still waiting for ShareLock on advisory lock' '$PGC_LOGFILE'" 2>/dev/null; }
qx_lw_before="$(qx_lock_waits_logged)"
qx_lt_got="$(PGOPTIONS='-c statement_timeout=30s -c lock_timeout=1ms -c deadlock_timeout=1ms -c log_lock_waits=on' \
	qx_sess pexport_qx_n -c "SELECT pgcolumnar.parallel_export_parquet('t_col'::regclass, '$PGC_WORKDIR/qx_1ms', 2)" \
	>"$PGC_WORKDIR/qx_n.out"; qx_result "$PGC_WORKDIR/qx_n.out")"
check "parallel_export_parquet under lock_timeout 1 ms, deadlock_timeout 1 ms and log_lock_waits on still exports, and logs no wait for its workers (#2245)" \
	"$qx_lt_got | $(( $(qx_lock_waits_logged) - qx_lw_before )) lock waits logged" "no error ($NSRC) | 0 lock waits logged"
# and the caller's settings are its own again once the export returns
qx_sess pexport_qx_g -c "BEGIN" -c "SET LOCAL lock_timeout = '2s'" -c "SET LOCAL log_lock_waits = on" \
	-c "SELECT pgcolumnar.parallel_export_parquet('t_lk'::regclass, '$PGC_WORKDIR/qx_guc', 2)" \
	-c "SHOW lock_timeout" -c "SHOW log_lock_waits" -c "COMMIT" >"$PGC_WORKDIR/qx_g.out"
check "the caller's lock_timeout and log_lock_waits read back unchanged after parallel_export_parquet in the same transaction (#2245)" \
	"$(e="$(qx_err "$PGC_WORKDIR/qx_g.out")"; [ -n "$e" ] && echo "$e" || paste -sd'|' "$PGC_WORKDIR/qx_g.out")" "$NC|2s|on"

# When the deadlock detector picks a worker as the victim, the caller re-raises
# the worker's 40P01 with its detail, not "exited without reporting a result".
# A cycle of hard edges: the caller waits for the worker's run lock, the worker
# waits for another session's ACCESS EXCLUSIVE on pgcolumnar.row_group, and that
# session waits for SHARE on t_wv_w, which the caller wrote. To get there, the
# one worker is first held at leaf t_wv1 by a third session's ACCESS EXCLUSIVE
# request queued behind the caller (the case above), and the other session takes
# row_group while the worker waits. The caller and that session run with
# deadlock_timeout 10 s, so the worker, at the server's 1 s, checks first.
psql_run "DROP TABLE IF EXISTS t_wv, t_wv_w CASCADE;
          CREATE TABLE t_wv ($COLS) PARTITION BY RANGE (id);
          CREATE TABLE t_wv1 PARTITION OF t_wv FOR VALUES FROM (MINVALUE) TO (10001) USING pgcolumnar;
          CREATE TABLE t_wv2 PARTITION OF t_wv FOR VALUES FROM (10001) TO (MAXVALUE) USING pgcolumnar;
          INSERT INTO t_wv $GEN_TX_COMMITTED; CREATE TABLE t_wv_w (a int);" >/dev/null
qx_victim() {	# -> "caller's error | what its detail names | time"
	local t0 ms bp
	qx_sess pexport_qx_vb -c "SET deadlock_timeout = '10s'" -c "BEGIN" -c "INSERT INTO t_wv_w VALUES (1)" \
		-c "LOCK TABLE t_wv1 IN ACCESS SHARE MODE" \
		-c "DO \$\$ BEGIN FOR i IN 1..400 LOOP EXIT WHEN EXISTS (SELECT 1 FROM pg_locks WHERE relation = 't_wv1'::regclass AND mode = 'AccessExclusiveLock' AND NOT granted); PERFORM pg_sleep(0.02); END LOOP; END \$\$" \
		-c "SET statement_timeout = '10s'" -c "SELECT pgcolumnar.parallel_export_parquet('t_wv'::regclass, '$PGC_WORKDIR/qx_victim', 1)" \
		-c "ROLLBACK" >"$PGC_WORKDIR/qx_vb.out" &
	bp=$!
	qx_wait_lock pexport_qx_vb t_wv1 AccessShareLock
	t0=$(date +%s%N)
	qx_sess pexport_qx_vc -c "SET statement_timeout = '20s'" -c "BEGIN" -c "LOCK TABLE t_wv1 IN ACCESS EXCLUSIVE MODE" \
		-c "COMMIT" >/dev/null &
	qx_sess pexport_qx_va -c "SET statement_timeout = '20s'" -c "SET deadlock_timeout = '10s'" -c "BEGIN" \
		-c "DO \$\$ BEGIN FOR i IN 1..400 LOOP EXIT WHEN EXISTS (SELECT 1 FROM pg_locks WHERE relation = 't_wv1'::regclass AND mode = 'AccessShareLock' AND NOT granted); PERFORM pg_sleep(0.02); END LOOP; END \$\$" \
		-c "LOCK TABLE pgcolumnar.row_group IN ACCESS EXCLUSIVE MODE" -c "LOCK TABLE t_wv_w IN SHARE MODE" \
		-c "COMMIT" >/dev/null
	wait "$bp"
	ms=$(qx_ms "$t0")
	wait
	printf '%s | %s | %s\n' "$(qx_result "$PGC_WORKDIR/qx_vb.out")" \
		"$(if grep -q 'waits for ShareLock on advisory lock \[[0-9]*,[0-9]*,0,108\]' "$PGC_WORKDIR/qx_vb.out"; then
			echo "detail names the run lock"; else echo "detail [$(grep -m1 '^DETAIL:' "$PGC_WORKDIR/qx_vb.out")]"; fi)" \
		"$(qx_under "$ms" 8)"
}
check "a parallel_export_parquet worker chosen as the deadlock victim reaches the caller as 40P01 with the cycle in its detail, within 8 s (#2245)" \
	"$(qx_victim)" "40P01 pgcolumnar.parallel_export_parquet worker 0 failed: deadlock detected | detail names the run lock | under 8 s"

check "server still up after the exports" "$(survived "$CRASHES_AT_START")" 1

pgc_summary
