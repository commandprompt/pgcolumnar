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
#      without the version (#1888 review). Two reads of one object in one
#      statement read one version (#1894); one s3 key on two endpoints is two
#      objects; the pin lasts one statement start time, so statements sent one
#      at a time in a transaction get their own pins and the statements of one
#      query string share one; a write drops the pin; with a weak ETag only the
#      length is compared (#1907 review). Many guards share 08006 and 08P01, so
#      each refusal must also carry its own guard's message.
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
SRV_PID_B=""

objstore_http_teardown() {
	[ -n "$SRV_PID" ] && kill "$SRV_PID" 2>/dev/null
	[ -n "$SRV_PID_B" ] && kill "$SRV_PID_B" 2>/dev/null
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
# hswap.parquet and vhswap.parquet are the pairs for /hswap/ and /vhswap/, which
# swap between two reads of one statement instead of mid-read (#1894).
# hswaptx.parquet and hswapmsg.parquet are more pairs for /hswap/, read by two
# statements of one transaction and of one query string, and whswap.parquet the pair for /whswap/, which sends a weak
# ETag. xpair.parquet is split over two servers: server A serves the old file
# and server B the new one, under one s3 key. rw.parquet is the object a
# statement overwrites and then reads again (#1907 review). vhswapmsg.parquet
# is read by two statements of one query string from a versioned store,
# hswapdet.parquet by the DETAIL check, and hswapfn.parquet by two reads inside
# one plpgsql call, hswapdo.parquet by two reads in one DO block, and
# hswapdo2.parquet by a DO block and the statement after it (#1907 second
# review).
python3 - "$PGC_WORKDIR/swap.parquet" "$PGC_WORKDIR/swap79.parquet" \
	"$PGC_WORKDIR/swapoff.parquet" "$PGC_WORKDIR/vswap.parquet" \
	"$PGC_WORKDIR/hswap.parquet" "$PGC_WORKDIR/vhswap.parquet" \
	"$PGC_WORKDIR/hswaptx.parquet" "$PGC_WORKDIR/whswap.parquet" \
	"$PGC_WORKDIR/xpair.parquet" "$PGC_WORKDIR/rw.parquet" \
	"$PGC_WORKDIR/hswapmsg.parquet" "$PGC_WORKDIR/vhswapmsg.parquet" \
	"$PGC_WORKDIR/hswapdet.parquet" "$PGC_WORKDIR/hswapfn.parquet" \
	"$PGC_WORKDIR/hswapdo.parquet" "$PGC_WORKDIR/hswapdo2.parquet" \
	"$PGC_WORKDIR/vswapq.parquet" <<PYEOF
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
# vhlen.parquet is served by /vhswap/ and whlen.parquet by /whswap/: the
# overwrite between two reads of one statement changes the length (#1907
# second review).
python3 - "$PGC_WORKDIR/lenswap.parquet" "$PGC_WORKDIR/lenswapoff.parquet" \
	"$PGC_WORKDIR/vhlen.parquet" "$PGC_WORKDIR/whlen.parquet" <<PYEOF
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

# Server B (#1907 review): a second store at another endpoint. It holds the
# same s3 key as server A, bkt/x.parquet, with the same length and other bytes.
mkdir -p "$PGC_WORKDIR/bkt" "$PGC_WORKDIR/b/bkt"
cp "$PGC_WORKDIR/xpair.parquet" "$PGC_WORKDIR/bkt/x.parquet"
cp "$PGC_WORKDIR/xpair.parquet.next" "$PGC_WORKDIR/b/bkt/x.parquet"
HTTP_PORT_B="$(pgc_pick_free_port "$PGC_AUX_PORT_LO" "$PGC_AUX_PORT_HI" $((HTTP_PORT + 13)))"
HTTP_LOG_B="$PGC_WORKDIR/http_b.log"
python3 "$(dirname "${BASH_SOURCE[0]}")/objstore_http_server.py" \
	--dir "$PGC_WORKDIR/b" --port "$HTTP_PORT_B" --log "$HTTP_LOG_B" \
	> "$PGC_WORKDIR/http_server_b.out" 2>&1 &
SRV_PID_B=$!
for _ in $(seq 1 50); do
	grep -q READY "$PGC_WORKDIR/http_server_b.out" 2>/dev/null && break
	sleep 0.1
done
check "premise: fixture server B is up" \
	"$(grep -c READY "$PGC_WORKDIR/http_server_b.out" 2>/dev/null)" "1"

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
psql_run "CREATE FOREIGN TABLE fhswap (c0 int8) SERVER osrv OPTIONS (path '$BASE/hswap/hswap.parquet');"
psql_run "CREATE FOREIGN TABLE fvhswap (c0 int8) SERVER osrv OPTIONS (path '$BASE/vhswap/vhswap.parquet');"
psql_run "CREATE FOREIGN TABLE fhswaptx (c0 int8) SERVER osrv OPTIONS (path '$BASE/hswap/hswaptx.parquet');"
psql_run "CREATE FOREIGN TABLE fhswapmsg (c0 int8) SERVER osrv OPTIONS (path '$BASE/hswap/hswapmsg.parquet');"
psql_run "CREATE FOREIGN TABLE fwhswap (c0 int8) SERVER osrv OPTIONS (path '$BASE/whswap/whswap.parquet');"
psql_run "CREATE FOREIGN TABLE fvhswapmsg (c0 int8) SERVER osrv OPTIONS (path '$BASE/vhswap/vhswapmsg.parquet');"
psql_run "CREATE FOREIGN TABLE fhswapdet (c0 int8) SERVER osrv OPTIONS (path '$BASE/hswap/hswapdet.parquet');"
psql_run "CREATE FOREIGN TABLE fhswapfn (c0 int8) SERVER osrv OPTIONS (path '$BASE/hswap/hswapfn.parquet');"
psql_run "CREATE FOREIGN TABLE fhswapdo (c0 int8) SERVER osrv OPTIONS (path '$BASE/hswap/hswapdo.parquet');"
psql_run "CREATE FOREIGN TABLE fhswapdo2 (c0 int8) SERVER osrv OPTIONS (path '$BASE/hswap/hswapdo2.parquet');"
psql_run "CREATE FOREIGN TABLE fvhlen (c0 int8) SERVER osrv OPTIONS (path '$BASE/vhswap/vhlen.parquet');"
psql_run "CREATE FOREIGN TABLE fwhlen (c0 int8) SERVER osrv OPTIONS (path '$BASE/whswap/whlen.parquet');"
psql_run "CREATE FOREIGN TABLE frw (c0 int8) SERVER osrv OPTIONS (path '$BASE/rw.parquet');"
# One s3 key on two endpoints (#1907 review): servers sa and sb name the two
# fixture servers. The fixture servers do not check signatures, so any key works.
for _s in a b; do
	_p="$HTTP_PORT"; [ "$_s" = b ] && _p="$HTTP_PORT_B"
	psql_run "CREATE SERVER s$_s FOREIGN DATA WRAPPER pgcolumnar_parquet OPTIONS (endpoint 'http://127.0.0.1:$_p', region 'us-east-1');"
	psql_run "CREATE USER MAPPING FOR CURRENT_USER SERVER s$_s OPTIONS (access_key_id 'pgc', secret_access_key 'pgc');"
	psql_run "CREATE FOREIGN TABLE fx$_s (c0 int8) SERVER s$_s OPTIONS (path 's3://bkt/x.parquet');"
	psql_run "CREATE FOREIGN TABLE fvx$_s (c0 int8) SERVER s$_s OPTIONS (path 's3://vswap/bkt/x.parquet');"
done

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

# Two reads of one object in one statement each send a HEAD (#1894). /hswap/
# overwrites the object at the 2nd HEAD, so between the two reads. Unpinned
# across reads, the first read returns the old rows and the second the new
# ones, and the statement mixes both without an error. Each reading is the
# statement's result, then how many HEADs of the object it sent.
heads_of() { awk -v m="$1" '$1 == "HEAD" && index($2, m)' "$HTTP_LOG" | wc -l | tr -d ' '; }
TWO_READS="SELECT 'old=' || count(*) FILTER (WHERE c0 >= 0) || ' new=' || count(*) FILTER (WHERE c0 < 0) FROM (SELECT c0 FROM %s UNION ALL SELECT c0 FROM %s) _u"
await_server >/dev/null
: > "$HTTP_LOG"
R="$(refusal_of "$(printf "$TWO_READS" fhswap fhswap)" "changed while it was being read")"
check "C: an object overwritten between two reads of one statement is refused" \
	"$R; HEADs: $(heads_of /hswap/hswap)" \
	"55000: changed while it was being read; HEADs: 2"
# The pin lasts one statement: the next statement reads the new object.
await_server >/dev/null
check "C: the statement after that reads the new object" \
	"$(refusal_of "$(printf "$TWO_READS" fhswap fhswap)" "changed while it was being read")" \
	"no error: old=0 new=$((2 * G * ROWS_PER_GROUP))"
# A versioned store names the version, so the second read asks for the
# version the first read opened, and both read the old rows.
await_server >/dev/null
: > "$HTTP_LOG"
R="$(refusal_of "$(printf "$TWO_READS" fvhswap fvhswap)" "changed while it was being read")"
check "C: two reads of one versioned object in one statement, overwritten between them, read the version the first opened" \
	"$R; HEADs: $(heads_of /vhswap/vhswap)" \
	"no error: old=$((2 * G * ROWS_PER_GROUP)) new=0; HEADs: 2"

# The pin belongs to one object in one store (#1907 review). Servers A and B
# hold the same s3 key with other bytes, so one statement reads A's old rows
# and B's new rows. A pin keyed on the URL alone refused this as an overwrite,
# and in a versioned bucket it sent A's version id to B, which answered 404.
await_server >/dev/null
check "C: one s3 key on two endpoints, read in one statement, reads each endpoint's own object" \
	"$(refusal_of "$(printf "$TWO_READS" fxa fxb)" "changed while it was being read")" \
	"no error: old=$((G * ROWS_PER_GROUP)) new=$((G * ROWS_PER_GROUP))"
await_server >/dev/null
check "C: one versioned s3 key on two endpoints, read in one statement, reads each endpoint's own version" \
	"$(refusal_of "$(printf "$TWO_READS" fvxa fvxb)" "changed while it was being read")" \
	"no error: old=$((G * ROWS_PER_GROUP)) new=$((G * ROWS_PER_GROUP))"

# The pin lasts while statement_timestamp() stays the same (#1907 review).
# Statements sent one at a time in a transaction each get their own pin:
# /hswap/ overwrites hswaptx.parquet at its 2nd HEAD, between the two
# statements, so the second reads only the new rows. The reading is each
# statement's result in turn.
ONE_READ="SELECT 'old=' || count(*) FILTER (WHERE c0 >= 0) || ' new=' || count(*) FILTER (WHERE c0 < 0) FROM %s"
await_server >/dev/null
check "C: in one transaction, the next statement reads the new object" \
	"$(refusal_of "BEGIN;
$(printf "$ONE_READ" fhswaptx);
$(printf "$ONE_READ" fhswaptx);
COMMIT" "changed while it was being read")" \
	"no error: old=$((G * ROWS_PER_GROUP)) new=0 old=0 new=$((G * ROWS_PER_GROUP))"
# Statements a client sends in one query string, as psql -c does, share one
# statement start time, but each is its own statement: the pin ends with the
# top-level statement (#1907 second review). /hswap/ overwrites the object at
# its 2nd HEAD, between the two statements, so the second reads the new rows,
# from an unversioned and from a versioned store alike.
# refusal_of_message SQL PHRASE: refusal_of, with SQL sent as one message (-c).
refusal_of_message() {
	local out code err
	await_server >/dev/null
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -qtA -v VERBOSITY=verbose -c "$1" 2>&1)"
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
check "C: two statements sent in one query string each read the current object" \
	"$(refusal_of_message "$(printf "$ONE_READ" fhswapmsg); $(printf "$ONE_READ" fhswapmsg)" \
		"changed while it was being read")" \
	"no error: old=$((G * ROWS_PER_GROUP)) new=0 old=0 new=$((G * ROWS_PER_GROUP))"
check "C: two statements sent in one query string to a versioned store each read the current version" \
	"$(refusal_of_message "$(printf "$ONE_READ" fvhswapmsg); $(printf "$ONE_READ" fvhswapmsg)" \
		"changed while it was being read")" \
	"no error: old=$((G * ROWS_PER_GROUP)) new=0 old=0 new=$((G * ROWS_PER_GROUP))"

# The pin lasts the top-level statement, not each statement a function runs
# inside it (#1907 second review). pgc_two() reads fhswapfn twice in one call;
# /hswap/ overwrites the object between the two reads, so the second is refused.
psql_run "CREATE FUNCTION pgc_two() RETURNS text LANGUAGE plpgsql AS \$f\$
DECLARE a text; b text;
BEGIN
	SELECT 'old=' || count(*) FILTER (WHERE c0 >= 0) || ' new=' || count(*) FILTER (WHERE c0 < 0) INTO a FROM fhswapfn;
	SELECT 'old=' || count(*) FILTER (WHERE c0 >= 0) || ' new=' || count(*) FILTER (WHERE c0 < 0) INTO b FROM fhswapfn;
	RETURN a || ' | ' || b;
END \$f\$;"
check "C: two reads of one object in one function call, overwritten between them, are refused" \
	"$(refusal_of "SELECT pgc_two()" "changed while it was being read")" \
	"55000: changed while it was being read"
# A DO block is one top-level utility statement: the queries it runs share
# its pin, and the pin ends when it returns, even with more statements to
# come in the same query string.
check "C: two reads of one object in one DO block, overwritten between them, are refused" \
	"$(refusal_of "DO \$\$ BEGIN PERFORM count(*) FROM fhswapdo; PERFORM count(*) FROM fhswapdo; END \$\$" \
		"changed while it was being read")" \
	"55000: changed while it was being read"
check "C: the statement after a DO block in one query string reads the new object" \
	"$(refusal_of_message "DO \$\$ BEGIN PERFORM count(*) FROM fhswapdo2; END \$\$; $(printf "$ONE_READ" fhswapdo2)" \
		"changed while it was being read")" \
	"no error: old=0 new=$((G * ROWS_PER_GROUP))"

# The refusal's DETAIL names the earlier read and both ETags and lengths
# (#1907 second review). The reading masks the two ETags, which change per run.
# detail_of SQL: the first DETAIL line SQL's error gives, or what came instead.
detail_of() {
	local out
	await_server >/dev/null
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -qtA -v VERBOSITY=verbose -c "$1" 2>&1)"
	out="$(printf '%s\n' "$out" | awk '/^DETAIL:/ && !f { sub(/^DETAIL:  /, ""); print; f = 1 }')"
	printf '%s\n' "${out:-no DETAIL}" | sed 's/"[^"]*"/"E"/g'
}
HSWAPDET_LEN="$(stat -c %s "$PGC_WORKDIR/hswapdet.parquet" 2>&1)"
check "C: the refusal of an overwrite between two reads names the earlier read of the statement in its DETAIL" \
	"$(detail_of "$(printf "$TWO_READS" fhswapdet fhswapdet)")" \
	"An earlier read of the object in this statement saw ETag \"E\" and $HSWAPDET_LEN bytes; this read sees ETag \"E\" and $HSWAPDET_LEN bytes."

# A versioned overwrite that changes the length (#1907 second review): the
# second read asks for the first read's version, so it must also take that
# version's length, not the length of the new object its HEAD saw.
await_server >/dev/null
check "C: two reads of one versioned object in one statement, overwritten between them by a longer object, read the version the first opened" \
	"$(refusal_of "$(printf "$TWO_READS" fvhlen fvhlen)" "changed while it was being read")" \
	"no error: old=$((2 * G * ROWS_PER_GROUP)) new=0"
# With a weak ETag the length is the only thing the second read can compare
# (#1907 second review): an overwrite that changes it is refused.
await_server >/dev/null
check "C: with a weak ETag, an overwrite that changes the length between two reads of one statement is refused" \
	"$(refusal_of "$(printf "$TWO_READS" fwhlen fwhlen)" "changed while it was being read")" \
	"55000: changed while it was being read"

# A statement that writes an object drops its pin, so a later read in the same
# statement reads what it wrote. pgc_rw() reads frw, exports rwneg (c0 < 0 in
# every row) over the same URL, and reads frw again, all in one statement.
psql_run "CREATE TABLE rwneg USING pgcolumnar AS SELECT (-1 - g)::int8 AS c0 FROM generate_series(0, $((G * ROWS_PER_GROUP - 1))) g;"
psql_run "CREATE FUNCTION pgc_rw() RETURNS text LANGUAGE plpgsql AS \$f\$
DECLARE a text; b text;
BEGIN
	SELECT 'old=' || count(*) FILTER (WHERE c0 >= 0) || ' new=' || count(*) FILTER (WHERE c0 < 0) INTO a FROM frw;
	PERFORM pgcolumnar.export_parquet('rwneg', '$BASE/rw.parquet');
	SELECT 'old=' || count(*) FILTER (WHERE c0 >= 0) || ' new=' || count(*) FILTER (WHERE c0 < 0) INTO b FROM frw;
	RETURN a || ' | ' || b;
END \$f\$;"
await_server >/dev/null
check "C: a statement that overwrites an object and reads it again reads the new object" \
	"$(refusal_of "SELECT pgc_rw()" "changed while it was being read")" \
	"no error: old=$((G * ROWS_PER_GROUP)) new=0 | old=0 new=$((G * ROWS_PER_GROUP))"

# With no strong ETag the second read compares only the length (#1907 review).
# /whswap/ sends a weak ETag and overwrites whswap.parquet at its 2nd HEAD with
# a file of the same length, so the statement mixes both versions without an
# error. docs/limitations.md states this limit; this check measures it.
await_server >/dev/null
check "C: with a weak ETag, a same-length overwrite between two reads of one statement is not caught" \
	"$(refusal_of "$(printf "$TWO_READS" fwhswap fwhswap)" "changed while it was being read")" \
	"no error: old=$((G * ROWS_PER_GROUP)) new=$((G * ROWS_PER_GROUP))"

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
