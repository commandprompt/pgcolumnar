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

check "server still up after the exports" "$(survived "$CRASHES_AT_START")" 1

pgc_summary
