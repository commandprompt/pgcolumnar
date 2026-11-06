#!/usr/bin/env bash
#
# column_chunk.page_offset corruption on the whole-group (unprojected) read.
#
# corruption.sh corrupts page_LENGTH and proves it errors cleanly, but only via
# SELECT sum(r): a projection, which reaches pgcolumnar_native_read_projected,
# whose containment check (columnar_reader.c: "chunk ... lies outside row group")
# catches a bad offset while it computes the ranges to read. It never corrupts
# page_OFFSET and never exercises the whole-group read.
#
# The whole-group read (rs->allColumnsWanted -> PgColumnarReadLogicalData) reads
# the group as one span and reaches the shared decode loop, where
#     base = rs->nativeBuffer + (cc->pageOffset - rg->fileOffset)
# indexes into that span. Without a containment check on this path a corrupt
# page_offset (above the group, or below fileOffset so the subtraction wraps)
# points base outside the buffer and the decode reads out of bounds -- an assert
# build takes SIGSEGV. The whole-group path is reached whenever column projection
# is off (pgcolumnar.enable_column_projection = off, a USERSET GUC) or a query
# genuinely wants every column.
#
# This suite pins that path shut. The projected arm is the control (already
# guarded); the enable_column_projection=off arms are the ones that segfault an
# unfixed reader. A sound reader raises ERRCODE_DATA_CORRUPTED on all of them and
# the backend survives.
#
# Removal proof: revert the whole-group guard in columnar_reader.c and the
# "SELECT * (proj off)" arms go from a clean error + alive to a lost connection.
#
# Usage:  test/native_page_offset_bound.sh [PG_CONFIG]
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

# A REFUSAL IS THE CONTAINMENT GUARD'S OWN. Every arm wants XX001 with the guard's
# message ("... lies outside row group"), read as "XX001 (containment guard)". It used to
# accept any psql failure, so a backend the corrupt offset crashed passed as refused.
# A BACKEND THAT DIED DOES NOT PASS AS ONE THAT SURVIVED: SELECT 1 answers again once
# crash recovery has finished, so survival also reads the server log's count of
# backends killed by a signal since the arm began. AND AN INTACT TABLE STILL READS: a
# guard over-applied to refuse every chunk, or a legal chunk ending exactly at its
# group's end, passed every arm below, which all want a refusal.
GUARD_RE='lies outside row group'
refusal_of() {	# stdin: psql output -> "XX001 (containment guard)", "<SQLSTATE>: <msg>", or what happened
	local line state msg out; out="$(cat)"
	line="$(sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\): /\1|/p' <<<"$out" | head -1)"
	if [ -z "$line" ]; then
		grep -qE 'server closed the connection|connection to server' <<<"$out" \
			&& echo "no refusal: the connection was lost" || echo "no error"
		return
	fi
	state="${line%%|*}"; msg="${line#*|}"
	if grep -qE -- "$GUARD_RE" <<<"$msg"; then echo "$state (containment guard)"; else echo "$state: $msg"; fi
}
refusal() {	# refusal SQL [PROJECTION_OFF] -> the refusal reading
	local pre=""; [ "${2:-}" = off ] && pre="SET pgcolumnar.enable_column_projection = off;"
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -qtA 2>&1 <<SQLEOF | refusal_of
\\set VERBOSITY verbose
$pre
$1;
SQLEOF
}
crashes() {
	local _n
	_n="$(pgc_pg "grep -c 'terminated by signal' '$PGC_LOGFILE'" 2>/dev/null)"
	case "$_n" in '' | *[!0-9]*) ;; *) echo "$_n" ;; esac
}
survived() {	# survived BEFORE -> "1" when the server answers and no backend died since BEFORE
	local _up _now _i
	_up="$(q 'SELECT 1')"; _now="$(crashes)"
	if ! [[ "$1" =~ ^[0-9]+$ && "$_now" =~ ^[0-9]+$ ]]; then echo "no (crash count unreadable)"; return; fi
	if [ "$_up" = 1 ] && [ "$_now" = "$1" ]; then echo 1; return; fi
	for _i in $(seq 1 60); do [ "$(q 'SELECT 1' 2>/dev/null)" = 1 ] && break; sleep 0.5; done
	echo "no ([$_up], $(( _now - $1 )) backend crashes)"
}
sid()    { q "SELECT pgcolumnar.get_storage_id('c');"; }

mkc() {
	psql_run "DROP TABLE IF EXISTS c;" >/dev/null 2>&1
	psql_run "CREATE TABLE c (id int, r bigint, t text) USING pgcolumnar;"
	psql_run "INSERT INTO c SELECT g, (random()*9e18)::bigint, 'v'||(g%5)
	          FROM generate_series(1,20000) g;"
}
WANT="XX001 (containment guard)"

# --- controls: an intact table reads every row, on both paths --------------------
echo "-- control: an intact table reads through the whole-group and the projected read"
mkc
# Rows, not an aggregate: count() and sum() over an intact table can be answered
# without decoding a chunk, and then neither read path is exercised. The OFFSET 0
# fence keeps the filter a filter, so every chunk of every group is decoded.
EVERY_1000="$(seq -s, 1000 1000 20000)"
check_text "an intact table reads every row and value with projection off" \
	"$(PGOPTIONS="-c pgcolumnar.enable_column_projection=off" q "SELECT string_agg(id::text, ',' ORDER BY id) FROM
	        (SELECT * FROM c WHERE id % 1000 = 0 AND r IS NOT NULL AND t LIKE 'v%' OFFSET 0) s;")" \
	"$EVERY_1000"
check_text "an intact table's projected read returns every id" \
	"$(q "SELECT string_agg(id::text, ',' ORDER BY id) FROM
	        (SELECT id FROM c WHERE id % 1000 = 0 OFFSET 0) s;")" "$EVERY_1000"

# --- control: the projected read already refuses an out-of-group offset ----------
echo "-- control: page_offset overrun on a PROJECTED scan (SELECT sum(r)) is refused"
mkc
psql_run "UPDATE pgcolumnar.column_chunk SET page_offset = page_offset + 999999999
          WHERE storage_id = $(sid) AND column_index = 1;"
B="$(crashes)"
check_text "projected page_offset overrun is refused by the containment guard" \
	"$(refusal 'SELECT sum(r) FROM c')" "$WANT"
check "alive after projected overrun" "$(survived "$B")" "1"

# --- the gap: whole-group read (projection off) with the offset above the group --
echo "-- page_offset overrun on the WHOLE-GROUP read (projection off) must be refused, not crash"
mkc
psql_run "UPDATE pgcolumnar.column_chunk SET page_offset = page_offset + 999999999
          WHERE storage_id = $(sid) AND column_index = 1;"
B="$(crashes)"
check_text "whole-group page_offset overrun is refused by the containment guard" \
	"$(refusal 'SELECT * FROM c' off)" "$WANT"
check "alive after whole-group overrun" "$(survived "$B")" "1"

# --- the harsher case: offset below fileOffset -> uint64 wrap on the same path ---
echo "-- page_offset driven below the group start (subtraction wraps), projection off"
mkc
psql_run "UPDATE pgcolumnar.column_chunk SET page_offset = 0
          WHERE storage_id = $(sid) AND column_index = 2;"
B="$(crashes)"
check_text "whole-group page_offset=0 is refused by the containment guard" \
	"$(refusal 'SELECT * FROM c' off)" "$WANT"
check "alive after whole-group wrap" "$(survived "$B")" "1"

# --- the overflow case: page_offset = -1 arrives as ~UINT64_MAX, so a naive
# --- `page_offset + page_length > group_end` test wraps and passes. The guard is
# --- written overflow-safe, and it must be the CONTAINMENT guard that fires, not a
# --- downstream decode error that only incidentally catches the wrapped read.
echo "-- page_offset = -1 (unsigned wrap) must be caught by the containment guard, both paths"
mkc
psql_run "UPDATE pgcolumnar.column_chunk SET page_offset = -1
          WHERE storage_id = $(sid) AND column_index = 1;"
B="$(crashes)"
check_text "whole-group page_offset=-1 hits the containment guard" \
	"$(refusal 'SELECT * FROM c' off)" "$WANT"
check "alive after whole-group page_offset=-1" "$(survived "$B")" "1"
B="$(crashes)"
check_text "projected page_offset=-1 hits the containment guard" \
	"$(refusal 'SELECT sum(r) FROM c')" "$WANT"
check "alive after projected page_offset=-1" "$(survived "$B")" "1"

pgc_summary
