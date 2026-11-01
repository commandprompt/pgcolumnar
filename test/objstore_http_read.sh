#!/usr/bin/env bash
#
# pgColumnar #393 M1: the HTTP ranged byte source, with the request count
# ASSERTED rather than reported (the M1 test spec agreed on the issue).
#
# Five arms, A to D per design/ISSUE_393_M1_HTTP_SOURCE.md:
#   A  differential oracle: a foreign table over http://127.0.0.1 returns the
#      same rows as the byte-identical local file. Premise for everything else.
#   B  request count, both arms in one run: buffered requests == K where K is
#      arithmetic from the fixture's structure (open = 1 HEAD + 3 ranged GETs,
#      data = one GET per needed chunk); unbuffered (dev GUC off) >= 10*K.
#      Removal proof: force the GUC off and the buffered check must go red.
#   C  failure taxonomy on SQLSTATE AND message: unreachable port and 404 name
#      the URL and produce no rows; a Range-ignoring server is an error, never a
#      silent whole-object read; a short 206 body and EOF mid-body are refused;
#      a keep-alive connection the server dropped is retried once, and a fresh
#      connection the server hangs up on is not; an object overwritten
#      mid-read is refused, a weak ETag or one longer than 79 characters is not
#      used as a pin, and a server whose ETag changes per request is refused
#      (#1805). With pgcolumnar.objstore_pin_reads off, that server is read
#      whole, and an overwrite is read unpinned (#1866). A versioned object is
#      read from the version the read opened, and a length change shown by the
#      Content-Range total, or by a 416 inside the old length, is refused
#      (#1867). The pin off sends no version; a version id of null or one over
#      1024 bytes is not sent; a versioned GET answered 403 is sent again
#      without the version (#1888 review). Many guards share 08006 and 08P01, so each refusal must also
#      carry its own guard's message.
#   D  cancel: statement_timeout fires within bound while the server stalls
#      mid-body. Removal proof: with the module's WaitLatchOrSocket wait arm
#      deleted, this check hangs or reds.
#   E  only an s3:// key is a pattern (#1785): a query string on an http URL
#      reaches the server, and a bracketed IPv6 host is refused by name.
#   Survival: the server still answers and no backend died across the arms (the
#      server log's "terminated by signal" count, read after waiting for it).
#
# The fixture server (test/objstore_http_server.py) logs one line per request;
# that log is seam B. The suite tears the server down through a chained trap
# (the bare-trap EXIT gotcha is pinned by harness_selftest).
#
# Usage:  test/objstore_http_read.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# The endpoint allow-list (#393) defaults to deny-all; this suite's fixture is
# the documented configuration example for a trusted local endpoint.
export PGC_EXTRA_CONF="pgcolumnar.objstore_allowed_endpoints='127.0.0.1'"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

python3 -c 'import pyarrow' 2>/dev/null || pgc_skip pyarrow "pyarrow is required to write the fixture"

HTTP_PORT="$(pgc_pick_free_port "$PGC_AUX_PORT_LO" "$PGC_AUX_PORT_HI")"
HTTP_LOG="$PGC_WORKDIR/http.log"
SRV_PID=""

objstore_http_teardown() {
	[ -n "$SRV_PID" ] && kill "$SRV_PID" 2>/dev/null
	pgc_teardown
}
trap objstore_http_teardown EXIT INT TERM

# ---- fixture ---------------------------------------------------------------
# G row groups x NCOLS int64 columns, tiny data pages so the unbuffered arm's
# per-page request count dwarfs the buffered per-chunk count. The arithmetic
# below depends only on G and the projection width, never on the page count.
G=3
NCOLS=8
ROWS_PER_GROUP=8000

python3 - "$PGC_WORKDIR/fixture.parquet" <<PYEOF
import sys
import pyarrow as pa, pyarrow.parquet as pq
G, NCOLS, RPG = $G, $NCOLS, $ROWS_PER_GROUP
n = G * RPG
cols = {("c%d" % i): pa.array([(i + 1) * v for v in range(n)], pa.int64())
        for i in range(NCOLS)}
t = pa.table(cols)
pq.write_table(t, sys.argv[1], row_group_size=RPG, data_page_size=1024,
               compression="NONE", use_dictionary=False)
md = pq.ParquetFile(sys.argv[1]).metadata
assert md.num_row_groups == G, md.num_row_groups
print("fixture: %d groups, %d cols, %d rows" % (md.num_row_groups, md.num_columns, md.num_rows))
PYEOF
check "premise: fixture written" "$([ -s "$PGC_WORKDIR/fixture.parquet" ] && echo yes)" "yes"

# The overwrite pair (#1805): swap.parquet and swap.parquet.next have the same
# layout and byte length, so every offset in one footer is valid in the other.
# The first has c0 >= 0 in every row, the second c0 < 0 in every row, so a read
# that mixes them shows both signs. /swap/ moves .next over the first mid-read.
# swap79.parquet is a second, identical pair for /swap79/, which swaps it once too.
# Each server mode swaps its file once, so every read that needs a swap gets its
# own pair: swapoff.parquet for the unpinned read, vswap.parquet for /vswap/,
# and vswapq.parquet for the versioned read through a query string (#1904).
python3 - "$PGC_WORKDIR/swap.parquet" "$PGC_WORKDIR/swap79.parquet" \
	"$PGC_WORKDIR/swapoff.parquet" "$PGC_WORKDIR/vswap.parquet" "$PGC_WORKDIR/vswapq.parquet" <<PYEOF
import os, sys
import pyarrow as pa, pyarrow.parquet as pq
G, RPG = $G, $ROWS_PER_GROUP
for first in sys.argv[1:]:
    for path, sign in ((first, 1), (first + ".next", -1)):
        c0 = pa.array([sign * v - (sign < 0) for v in range(G * RPG)], pa.int64())
        pq.write_table(pa.table({"c0": c0}), path, row_group_size=RPG,
                       compression="NONE", use_dictionary=False, write_statistics=False)
    assert os.path.getsize(first) == os.path.getsize(first + ".next")
PYEOF
check "premise: the overwrite pair has one byte length" \
	"$(stat -c %s "$PGC_WORKDIR/swap.parquet.next" 2>&1)" "$(stat -c %s "$PGC_WORKDIR/swap.parquet" 2>&1)"

# The length-change pairs (#1867): the .next file has one more row group, so it
# is longer. Its first G groups sit at the same offsets as the first file's, so a
# read that keeps the old footer gets well-formed rows from the new file. Only
# the total in each 206 Content-Range shows the change. /lenswap/ sends no ETag.
python3 - "$PGC_WORKDIR/lenswap.parquet" "$PGC_WORKDIR/lenswapoff.parquet" <<PYEOF
import os, sys
import pyarrow as pa, pyarrow.parquet as pq
G, RPG = $G, $ROWS_PER_GROUP
for first in sys.argv[1:]:
    for path, sign, groups in ((first, 1, G), (first + ".next", -1, G + 1)):
        c0 = pa.array([sign * v - (sign < 0) for v in range(groups * RPG)], pa.int64())
        pq.write_table(pa.table({"c0": c0}), path, row_group_size=RPG,
                       compression="NONE", use_dictionary=False, write_statistics=False)
PYEOF
check "premise: the length-change pair differs in length" \
	"$(( $(stat -c %s "$PGC_WORKDIR/lenswap.parquet.next" 2>/dev/null || echo 0) \
		- $(stat -c %s "$PGC_WORKDIR/lenswap.parquet" 2>/dev/null || echo 0) > 0 ))" "1"

# The shrink pair (#1888 review): lenshrink.parquet.next holds one row, so it is
# shorter than the offset of every chunk after the swap. The server then answers
# 416 to those GETs, not a 206 with the new total.
python3 - "$PGC_WORKDIR/lenshrink.parquet" <<PYEOF
import sys
import pyarrow as pa, pyarrow.parquet as pq
G, RPG = $G, $ROWS_PER_GROUP
first = sys.argv[1]
for path, rows in ((first, list(range(G * RPG))), (first + ".next", [-1])):
    pq.write_table(pa.table({"c0": pa.array(rows, pa.int64())}), path, row_group_size=RPG,
                   compression="NONE", use_dictionary=False, write_statistics=False)
PYEOF
check "premise: the shrink pair's new file is shorter than its old one (bytes old-new)" \
	"$(( $(stat -c %s "$PGC_WORKDIR/lenshrink.parquet" 2>/dev/null || echo 0) \
		- $(stat -c %s "$PGC_WORKDIR/lenshrink.parquet.next" 2>/dev/null || echo 0) > 60000 ))" "1"

# ---- fixture server --------------------------------------------------------
python3 "$(dirname "${BASH_SOURCE[0]}")/objstore_http_server.py" \
	--dir "$PGC_WORKDIR" --port "$HTTP_PORT" --log "$HTTP_LOG" \
	> "$PGC_WORKDIR/http_server.out" 2>&1 &
SRV_PID=$!
for _ in $(seq 1 50); do
	grep -q READY "$PGC_WORKDIR/http_server.out" 2>/dev/null && break
	sleep 0.1
done
check "premise: fixture server is up" \
	"$(grep -c READY "$PGC_WORKDIR/http_server.out" 2>/dev/null)" "1"

BASE="http://127.0.0.1:$HTTP_PORT"
COLDEFS="$(python3 -c "print(', '.join('c%d int8' % i for i in range($NCOLS)))")"

psql_run "CREATE SERVER osrv FOREIGN DATA WRAPPER pgcolumnar_parquet;"
psql_run "CREATE FOREIGN TABLE flocal ($COLDEFS) SERVER osrv OPTIONS (path '$PGC_WORKDIR/fixture.parquet');"
psql_run "CREATE FOREIGN TABLE fhttp  ($COLDEFS) SERVER osrv OPTIONS (path '$BASE/fixture.parquet');"

# Wait up to 30 s for the server to answer; -> what SELECT 1 last returned. A
# crash restarts the server, so every reading after one waits for it: otherwise
# the next reading fails on recovery instead of measuring its own guard.
await_server() {
	local up=""
	for _ in $(seq 1 60); do
		up="$(q 'SELECT 1')"
		[ "$up" = 1 ] && break
		sleep 0.5
	done
	echo "$up"
}

# refusal_of SQL PHRASE -> "SQLSTATE: PHRASE" when SQL failed with PHRASE in its
# primary message, else the SQLSTATE and the message it did give, or what came
# back instead of an error.
refusal_of() {
	local out code err
	await_server >/dev/null
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -qtA 2>&1 <<SQLEOF
\\set VERBOSITY verbose
$1;
SQLEOF
)"
	# readers that consume all their input: an early exit SIGPIPEs the printf
	err="$(printf '%s\n' "$out" | awk '/^ERROR:/ && !f { print; f = 1 }')"
	code="$(printf '%s\n' "$err" | sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\): .*/\1/p')"
	if [ -z "$code" ]; then
		out="${out//$'\n'/ }"
		echo "no error: ${out:0:200}"
	elif [[ "$err" == *"$2"* ]]; then
		echo "$code: $2"
	else
		echo "$code: ${err#ERROR:  $code: }"
	fi
}

# Backends the server log says died; blank when the log can't be read, so a
# blind count never reads as "no crashes".
crashes() {
	local _n
	_n="$(pgc_pg "grep -c 'terminated by signal' '$PGC_LOGFILE'" 2>/dev/null)"
	case "$_n" in '' | *[!0-9]*) ;; *) echo "$_n" ;; esac
}
CRASHES0="$(crashes)"

# Requests logged since the last truncation.
reqs() { wc -l < "$HTTP_LOG" | tr -d ' '; }

# ---- arm A: differential oracle -------------------------------------------
check "A: full scan http == local" \
	"$(pgc_set_hash "SELECT * FROM fhttp")" "$(pgc_set_hash "SELECT * FROM flocal")"
check "A: projection http == local" \
	"$(pgc_set_hash "SELECT c1, c6 FROM fhttp")" "$(pgc_set_hash "SELECT c1, c6 FROM flocal")"
check_num "A: count(*) over http" "$(q "SELECT count(*) FROM fhttp")" "$((G * ROWS_PER_GROUP))"

# ---- arm B: the asserted request count -------------------------------------
# K is arithmetic from the fixture, stated here, not a remembered literal:
# open = 1 HEAD + 3 ranged GETs (head magic, tail, footer); data = one GET per
# needed chunk = G row groups x the projection's column width. The projections
# are sums so every named column is genuinely pulled (count(*) pulls none and
# would make the width premise silently wrong).
SUM_ALL="$(python3 -c "print(' + '.join('sum(c%d)' % i for i in range($NCOLS)))")"
K_FULL=$((4 + G * NCOLS))
K_PROJ=$((4 + G * 2))

: > "$HTTP_LOG"
q "SELECT $SUM_ALL FROM fhttp" >/dev/null
FULL_BUF="$(reqs)"
: > "$HTTP_LOG"
q "SELECT sum(c1) + sum(c6) FROM fhttp" >/dev/null
PROJ_BUF="$(reqs)"

: > "$HTTP_LOG"
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -qtA \
	-c "SET pgcolumnar.objstore_buffered = off" \
	-c "SELECT $SUM_ALL FROM fhttp" >/dev/null 2>&1
FULL_UNBUF="$(reqs)"

check "premise: buffered run logged requests" "$([ "${FULL_BUF:-0}" -gt 0 ] 2>/dev/null && echo yes)" "yes"
check "premise: unbuffered run logged requests" "$([ "${FULL_UNBUF:-0}" -gt 0 ] 2>/dev/null && echo yes)" "yes"
# Buffered counts are deterministic (keep-alive, no retries in this phase), so
# they are asserted as equalities: stricter than the spec's <= bound, and it
# catches over-coalescing as well as under.
check_num "B: buffered full-width requests (K = 4 + G*NCOLS)" "$FULL_BUF" "$K_FULL"
check_num "B: buffered 2-col requests (K = 4 + G*2)" "$PROJ_BUF" "$K_PROJ"
check_num "B: unbuffered >= 10x K" \
	"$([ "${FULL_UNBUF:-0}" -ge $((10 * K_FULL)) ] 2>/dev/null && echo 1 || echo 0)" "1"
check_ratio "B: buffered/unbuffered request ratio" "$FULL_BUF" "$FULL_UNBUF" "0.1"

# Every data-phase request carried a Range header (the HEAD is the one allowed
# exception per open). The GET-count premise keeps this from passing vacuously
# on an empty log, which is exactly what the RED run produced.
: > "$HTTP_LOG"
q "SELECT sum(c0) FROM fhttp" >/dev/null
# log columns since #394: METHOD path query range ($4 is the range; a GET's
# query is always "-", so the range moved from $3 to $4).
NGETS="$(awk '$1 == "GET"' "$HTTP_LOG" | wc -l | tr -d ' ')"
NORANGE="$(awk '$1 == "GET" && $4 == "-"' "$HTTP_LOG" | wc -l | tr -d ' ')"
check "premise: the range-audit phase logged GETs" \
	"$([ "${NGETS:-0}" -gt 0 ] 2>/dev/null && echo yes)" "yes"
check_num "B: every GET carried a Range header" "$NORANGE" "0"

# ---- arm C: failure taxonomy ----------------------------------------------
DEADPORT="$(pgc_pick_free_port "$PGC_AUX_PORT_LO" "$PGC_AUX_PORT_HI" $((HTTP_PORT + 7)))"
psql_run "CREATE FOREIGN TABLE fdead ($COLDEFS) SERVER osrv OPTIONS (path 'http://127.0.0.1:$DEADPORT/fixture.parquet');"
psql_run "CREATE FOREIGN TABLE f404  ($COLDEFS) SERVER osrv OPTIONS (path '$BASE/no_such.parquet');"
psql_run "CREATE FOREIGN TABLE fnorange ($COLDEFS) SERVER osrv OPTIONS (path '$BASE/norange/fixture.parquet');"

psql_run "CREATE FOREIGN TABLE fshort ($COLDEFS) SERVER osrv OPTIONS (path '$BASE/short/fixture.parquet');"
psql_run "CREATE FOREIGN TABLE ftrunc ($COLDEFS) SERVER osrv OPTIONS (path '$BASE/trunc/fixture.parquet');"
psql_run "CREATE FOREIGN TABLE fdropka ($COLDEFS) SERVER osrv OPTIONS (path '$BASE/dropka/fixture.parquet');"
psql_run "CREATE FOREIGN TABLE fhangup ($COLDEFS) SERVER osrv OPTIONS (path '$BASE/hangup/fixture.parquet');"
psql_run "CREATE FOREIGN TABLE fswap (c0 int8) SERVER osrv OPTIONS (path '$BASE/swap/swap.parquet');"
psql_run "CREATE FOREIGN TABLE fweak ($COLDEFS) SERVER osrv OPTIONS (path '$BASE/weak/fixture.parquet');"
psql_run "CREATE FOREIGN TABLE fswap79 (c0 int8) SERVER osrv OPTIONS (path '$BASE/swap79/swap79.parquet');"
psql_run "CREATE FOREIGN TABLE fetag79 ($COLDEFS) SERVER osrv OPTIONS (path '$BASE/etag79/fixture.parquet');"
psql_run "CREATE FOREIGN TABLE flongetag ($COLDEFS) SERVER osrv OPTIONS (path '$BASE/longetag/fixture.parquet');"
psql_run "CREATE FOREIGN TABLE ffickle ($COLDEFS) SERVER osrv OPTIONS (path '$BASE/fickle/fixture.parquet');"
psql_run "CREATE FOREIGN TABLE fswapoff (c0 int8) SERVER osrv OPTIONS (path '$BASE/swap/swapoff.parquet');"
psql_run "CREATE FOREIGN TABLE fvswap (c0 int8) SERVER osrv OPTIONS (path '$BASE/vswap/vswap.parquet');"
psql_run "CREATE FOREIGN TABLE flenswap (c0 int8) SERVER osrv OPTIONS (path '$BASE/lenswap/lenswap.parquet');"
psql_run "CREATE FOREIGN TABLE flenswapoff (c0 int8) SERVER osrv OPTIONS (path '$BASE/lenswap/lenswapoff.parquet');"
psql_run "CREATE FOREIGN TABLE flenshrink (c0 int8) SERVER osrv OPTIONS (path '$BASE/lenswap/lenshrink.parquet');"
psql_run "CREATE FOREIGN TABLE fvswapoff ($COLDEFS) SERVER osrv OPTIONS (path '$BASE/vswap/fixture.parquet');"
psql_run "CREATE FOREIGN TABLE fvnull ($COLDEFS) SERVER osrv OPTIONS (path '$BASE/vnull/fixture.parquet');"
psql_run "CREATE FOREIGN TABLE fvlong ($COLDEFS) SERVER osrv OPTIONS (path '$BASE/vlong/fixture.parquet');"
psql_run "CREATE FOREIGN TABLE fvdeny ($COLDEFS) SERVER osrv OPTIONS (path '$BASE/vdeny/fixture.parquet');"

check "C: unreachable endpoint SQLSTATE" \
	"$(refusal_of "SELECT count(*) FROM fdead" "could not connect to")" "08006: could not connect to"
check "C: http 404 SQLSTATE" \
	"$(refusal_of "SELECT count(*) FROM f404" "does not exist (HTTP 404)")" "58P01: does not exist (HTTP 404)"
check "C: range-ignoring server SQLSTATE" \
	"$(refusal_of "SELECT count(*) FROM fnorange" "ignored a Range request")" "08P01: ignored a Range request"
check "C: a short 206 body is refused" \
	"$(refusal_of "SELECT count(*) FROM fshort" "requested bytes")" "08P01: requested bytes"
check "C: EOF mid-body is refused" \
	"$(refusal_of "SELECT count(*) FROM ftrunc" "closed mid-body")" "08006: closed mid-body"
# The server closes after every response without saying so, so every request
# after the first on a connection meets the keep-alive race and must be retried.
await_server >/dev/null
check "C: a dropped keep-alive connection is retried (http == local)" \
	"$(pgc_set_hash "SELECT c2, c5 FROM fdropka")" "$(pgc_set_hash "SELECT c2, c5 FROM flocal")"
: > "$HTTP_LOG"
check "C: a server that hangs up on a fresh connection is refused" \
	"$(refusal_of "SELECT count(*) FROM fhangup" "failed before a response")" "08006: failed before a response"
check_num "C: and that fresh connection is not retried (one request logged)" \
	"$(wc -l < "$HTTP_LOG" | tr -d ' ')" "1"

# An object overwritten between two ranged GETs of one read (#1805). Without a
# pin the footer comes from the old object and later chunks from the new one,
# and the read returns rows of both; the measurement then shows both signs.
await_server >/dev/null
check "C: an object overwritten mid-read is refused" \
	"$(refusal_of "SELECT 'old=' || count(*) FILTER (WHERE c0 >= 0) || ' new=' || count(*) FILTER (WHERE c0 < 0) FROM fswap" \
		"changed while it was being read")" "55000: changed while it was being read"
# A weak ETag cannot pin a ranged read (If-Match compares strongly), so the
# client must not send it: the read still equals the local file.
await_server >/dev/null
check "C: a weak ETag is not sent as If-Match (http == local)" \
	"$(pgc_set_hash "SELECT c3, c7 FROM fweak")" "$(pgc_set_hash "SELECT c3, c7 FROM flocal")"
# The client keeps an ETag of up to 79 characters. At 79 the read is pinned, so
# the overwrite is refused; at 80 the client keeps none, because a cut-down ETag
# matches nothing and every GET would fail. Then the read is unpinned, and rows
# come back.
check "C: a 79-character ETag still pins the read" \
	"$(refusal_of "SELECT 'old=' || count(*) FILTER (WHERE c0 >= 0) || ' new=' || count(*) FILTER (WHERE c0 < 0) FROM fswap79" \
		"changed while it was being read")" "55000: changed while it was being read"
# The same 79-character ETag on an object nobody overwrites: the read is pinned
# and every GET must match, so a pin kept shorter than the ETag fails each GET.
await_server >/dev/null
check "C: a 79-character ETag reads an unchanged object whole (http == local)" \
	"$(pgc_set_hash "SELECT c3, c7 FROM fetag79")" "$(pgc_set_hash "SELECT c3, c7 FROM flocal")"
check "C: an 80-character ETag is not sent cut short as If-Match" \
	"$(refusal_of "SELECT 'rows=' || count(*) FROM flongetag" "changed while it was being read")" \
	"no error: rows=$((G * ROWS_PER_GROUP))"
# A server that sends a new ETag on every request for the same bytes looks like
# an overwrite, so the read is refused: the client cannot tell the two apart.
check "C: a server whose ETag changes on every request is refused" \
	"$(refusal_of "SELECT 'rows=' || count(*) FROM ffickle" "changed while it was being read")" \
	"55000: changed while it was being read"

# The pin can be turned off (#1866), for servers like /fickle/ whose ETag
# changes per request. Off, the read sends no If-Match, so it reads the fickle
# object whole. The SET runs in the same session as the read.
NOPIN="SET pgcolumnar.objstore_pin_reads = off"
check "C: with pgcolumnar.objstore_pin_reads off, a server whose ETag changes on every request is read whole (http == local)" \
	"$(refusal_of "$NOPIN; SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), 'EMPTY') FROM (SELECT _r::text AS t FROM (SELECT c3, c7 FROM ffickle) _r) _s" \
		"changed while it was being read")" \
	"no error: $(pgc_set_hash "SELECT c3, c7 FROM flocal")"
# That is the cost of turning it off: an object overwritten mid-read is read
# unpinned, the footer and first chunk from the old object and the rest from
# the new one. The swap comes after the 4th GET, the first of the 3 chunks.
check "C: with pgcolumnar.objstore_pin_reads off, an object overwritten mid-read is read unpinned" \
	"$(refusal_of "$NOPIN; SELECT 'old=' || count(*) FILTER (WHERE c0 >= 0) || ' new=' || count(*) FILTER (WHERE c0 < 0) FROM fswapoff" \
		"changed while it was being read")" \
	"no error: old=$ROWS_PER_GROUP new=$(( (G - 1) * ROWS_PER_GROUP ))"

# A versioned store names the version (#1867). The read then asks for that
# version on every ranged GET, so an overwrite mid-read does not stop it: it
# finishes on the version it opened, every row from the old object.
await_server >/dev/null
: > "$HTTP_LOG"
check "C: a versioned object overwritten mid-read is read whole from the version the read opened" \
	"$(refusal_of "SELECT 'old=' || count(*) FILTER (WHERE c0 >= 0) || ' new=' || count(*) FILTER (WHERE c0 < 0) FROM fvswap" \
		"changed while it was being read")" \
	"no error: old=$((G * ROWS_PER_GROUP)) new=0"
# log columns: METHOD path query range. The id has '+' and '/', so it must go
# out percent-encoded: the server reads an unencoded '+' as a space.
VGETS="$(awk '$1 == "GET" && $2 ~ /vswap/' "$HTTP_LOG" | wc -l | tr -d ' ')"
VNOID="$(awk '$1 == "GET" && $2 ~ /vswap/ && $3 !~ /^versionId=3HL4%2Bkq%2F/' "$HTTP_LOG" | wc -l | tr -d ' ')"
check "premise: the versioned read logged GETs" \
	"$([ "${VGETS:-0}" -gt 0 ] 2>/dev/null && echo yes || echo "no ($VGETS)")" "yes"
check_num "C: every ranged GET of a versioned read names the version" "$VNOID" "0"

# An overwrite that changes the length, on a server that sends no ETag: the
# read has no pin, and the total in a 206 Content-Range is what shows the
# change (#1867). The check stays on when the pin is off.
await_server >/dev/null
check "C: an object whose length changes mid-read is refused (Content-Range total)" \
	"$(refusal_of "SELECT 'old=' || count(*) FILTER (WHERE c0 >= 0) || ' new=' || count(*) FILTER (WHERE c0 < 0) FROM flenswap" \
		"changed while it was being read")" \
	"55000: changed while it was being read"
await_server >/dev/null
check "C: with pgcolumnar.objstore_pin_reads off, an object whose length changes mid-read is still refused" \
	"$(refusal_of "$NOPIN; SELECT 'old=' || count(*) FILTER (WHERE c0 >= 0) || ' new=' || count(*) FILTER (WHERE c0 < 0) FROM flenswapoff" \
		"changed while it was being read")" \
	"55000: changed while it was being read"
# An object that shrinks below the offset of a later GET gets 416, not 206
# (#1888 review). The read asked only for bytes inside the length the HEAD
# gave, so a 416 means the object changed.
await_server >/dev/null
check "C: an object that shrinks mid-read below a later GET's offset is refused (HTTP 416)" \
	"$(refusal_of "SELECT 'sum=' || sum(c0) FROM flenshrink" "changed while it was being read")" \
	"55000: changed while it was being read"

# vgets_named FILE-PART -> how many logged GETs of FILE-PART named a version.
vgets_named() {
	awk -v m="$1" '$1 == "GET" && index($2, m) && $3 ~ /(^|&)versionId=/' "$HTTP_LOG" | wc -l | tr -d ' '
}
LOCAL_C3C7="$(pgc_set_hash "SELECT c3, c7 FROM flocal")"
HASH_SQL="SELECT coalesce(md5(string_agg(t, chr(10) ORDER BY t)), 'EMPTY') FROM (SELECT _r::text AS t FROM (SELECT c3, c7 FROM %s) _r) _s"

# Off, the read sends no versionId either (#1866): the docs say so. Each
# reading below is the read's result, then how many of its GETs named a version.
await_server >/dev/null
: > "$HTTP_LOG"
R="$(refusal_of "$NOPIN; $(printf "$HASH_SQL" fvswapoff)" "changed while it was being read")"
check "C: with pgcolumnar.objstore_pin_reads off, no ranged GET of a versioned object names the version" \
	"$R; GETs naming a version: $(vgets_named /vswap/fixture)" \
	"no error: $LOCAL_C3C7; GETs naming a version: 0"

# S3 gives the id "null" to an object written while versioning was off. That
# version is overwritten in place, so the read does not ask for it.
await_server >/dev/null
: > "$HTTP_LOG"
R="$(refusal_of "$(printf "$HASH_SQL" fvnull)" "changed while it was being read")"
check "C: a version id of null is not sent as a pin" \
	"$R; GETs naming a version: $(vgets_named /vnull/fixture)" \
	"no error: $LOCAL_C3C7; GETs naming a version: 0"

# An id longer than the 1024 bytes S3 allows is kept as none: a cut id would
# name no version, so every GET would fail.
await_server >/dev/null
: > "$HTTP_LOG"
R="$(refusal_of "$(printf "$HASH_SQL" fvlong)" "changed while it was being read")"
check "C: a version id longer than 1024 bytes is not sent cut short" \
	"$R; GETs naming a version: $(vgets_named /vlong/fixture)" \
	"no error: $LOCAL_C3C7; GETs naming a version: 0"

# A GET that names a version needs s3:GetObjectVersion; a policy that grants
# only s3:GetObject answers it 403 (#1888 review). The read then drops the
# version and keeps If-Match, so it reads as it did before #1867: one GET
# names the version, and the rest do not.
await_server >/dev/null
: > "$HTTP_LOG"
R="$(refusal_of "$(printf "$HASH_SQL" fvdeny)" "AccessDenied")"
check "C: a versioned GET answered 403 is sent again without the version, and the read finishes" \
	"$R; GETs naming a version: $(vgets_named /vdeny/fixture)" \
	"no error: $LOCAL_C3C7; GETs naming a version: 1"

# ---- arm D: cancel while the server stalls mid-body ------------------------
psql_run "CREATE FOREIGN TABLE fstall ($COLDEFS) SERVER osrv OPTIONS (path '$BASE/stall/fixture.parquet');"
await_server >/dev/null
D_START=$(date +%s)
D_STATE="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
	-d "$PGC_DB" -qtA 2>&1 <<SQLEOF | sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\).*/\1/p' | head -1
\\set VERBOSITY sqlstate
SET statement_timeout = '2s';
SELECT count(*) FROM fstall;
SQLEOF
)"
D_ELAPSED=$(( $(date +%s) - D_START ))
check "D: stalled transfer cancelled with query_canceled" "$D_STATE" "57014"
check_num "D: cancel latency bounded (<=10s)" \
	"$([ "$D_ELAPSED" -le 10 ] 2>/dev/null && echo 1 || echo 0)" "1"

# ---- arm E: only an s3:// key is a pattern (#1785) -------------------------
# Listing exists only for s3://, so a `?` in an http(s) URL is a query string,
# never a glob, and a bracketed host is refused as an IPv6 literal before any
# path resolution. Both used to fail as "listing a prefix is only supported for
# s3:// URLs".
: > "$HTTP_LOG"
E_ROWS="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -qtA \
	-c "SELECT count(*) FROM pgcolumnar.read_parquet('$BASE/fixture.parquet?raw=true') AS t($COLDEFS)" 2>&1)"
E_ROWS="$(awk '/^ERROR:/ { print; f = 1; exit } { last = $0 } END { if (!f) print last }' <<<"$E_ROWS")"
check "E: an http URL with a query string (?raw=true) reads the object, not a listing" \
	"$E_ROWS" "$((G * ROWS_PER_GROUP))"
E_HEADQ="$(awk '$1 == "HEAD" && $2 == "/fixture.parquet" && $3 == "raw=true"' "$HTTP_LOG" | wc -l | tr -d ' ')"
check "E: and its HEAD carried the query string to the server" \
	"$([ "${E_HEADQ:-0}" -ge 1 ] 2>/dev/null && echo carried || echo "not carried ($E_HEADQ HEADs of /fixture.parquet?raw=true in $(reqs) requests)")" "carried"
# The version pin (#1867) joins a query the URL already has with '&' (#1904).
# A second '?' hides the versionId inside raw's value, so the server reads the
# current object, and the overwrite mid-read is refused by If-Match.
await_server >/dev/null
: > "$HTTP_LOG"
R="$(refusal_of "SELECT 'old=' || count(*) FILTER (WHERE c0 >= 0) || ' new=' || count(*) FILTER (WHERE c0 < 0) FROM pgcolumnar.read_parquet('$BASE/vswap/vswapq.parquet?raw=true') AS t(c0 int8)" \
	"changed while it was being read")"
EQ_GETS="$(awk '$1 == "GET" && $2 == "/vswap/vswapq.parquet"' "$HTTP_LOG" | wc -l | tr -d ' ')"
EQ_NOT="$(awk '$1 == "GET" && $2 == "/vswap/vswapq.parquet" && $3 !~ /^raw=true&versionId=3HL4%2Bkq%2F/ { print $3 }' "$HTTP_LOG")"
EQ_BAD="$(printf '%s' "$EQ_NOT" | grep -c .)"
[ "$EQ_BAD" -gt 0 ] && EQ_BAD="$EQ_BAD (first: $(head -1 <<<"$EQ_NOT"))"
check "E: a versioned read of an http URL with a query string adds the version with '&' and reads the version it opened" \
	"$R; GETs without raw=true&versionId=: $EQ_BAD of $EQ_GETS" \
	"no error: old=$((G * ROWS_PER_GROUP)) new=0; GETs without raw=true&versionId=: 0 of $EQ_GETS"
check "E: an IPv6 literal http host is refused as IPv6, not as a listing" \
	"$(refusal_of "SELECT count(*) FROM pgcolumnar.read_parquet('http://[::1]:$HTTP_PORT/fixture.parquet') AS t($COLDEFS)" \
		"IPv6 literal hosts are not supported")" "0A000: IPv6 literal hosts are not supported"

# ---- survival ----------------------------------------------------------------
# A crash is restarted from, so a bare SELECT 1 alone passes once recovery ends:
# wait for the server, then require the crash count unmoved since the start.
UP="$(await_server)"
CRASHES1="$(crashes)"
check "survival: the server answers and no backend crashed" \
	"$(if ! [[ "$CRASHES0" =~ ^[0-9]+$ && "$CRASHES1" =~ ^[0-9]+$ ]]; then echo "no (crash count unreadable)"
	elif [ "$UP" = 1 ] && [ "$CRASHES1" = "$CRASHES0" ]; then echo 1
	else echo "no ([$UP], $(( CRASHES1 - CRASHES0 )) backend crashes)"; fi)" "1"

pgc_summary
