#!/usr/bin/env bash
#
# pgColumnar #394 steps 3-4: the remote sink - exports to s3://, multipart
# above the part size, complete-or-abort, parallel per-worker objects.
#
# The fixture server emulates multipart faithfully (create/parts/complete/
# abort, parts invisible until complete) under the same stdlib SigV4 verifier
# as the read side - now including PAYLOAD HASHES and canonical QUERY strings,
# so every write arm is a cross-implementation signature check of the parts
# M2's read-only signing never exercised. design/ISSUE_394_REMOTE_SINK.md.
#
# The invariant under test is step 1's, verbatim: nothing is ever visible at
# the final name before finish() returns. S3 gives it via multipart; the arms
# that matter are the failure ones - an aborted upload leaves NOTHING, keyed
# or partial.
#
# Usage:  test/objstore_sink_write.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

export PGC_EXTRA_CONF=$'pgcolumnar.objstore_allowed_endpoints=\'127.0.0.1\'\nmax_worker_processes=16'

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

S3_PORT="$(pgc_pick_free_port "$PGC_AUX_PORT_LO" "$PGC_AUX_PORT_HI")"
S3_LOG="$PGC_WORKDIR/sink.log"
SRV_PID=""
PLAIN_PID=""
AKID="PGCTESTKEYID"
SECRET="pgctest-secret-shhh"
REGION="pgc-test-1"
BUCKET="pgc-bucket"
DENIED_BUCKET="pgc-denied"   # the fixture checks this bucket's signatures against another secret

objstore_sink_teardown() {
	[ -n "$SRV_PID" ] && kill "$SRV_PID" 2>/dev/null
	[ -n "$PLAIN_PID" ] && kill "$PLAIN_PID" 2>/dev/null
	pgc_teardown
}
trap objstore_sink_teardown EXIT INT TERM

# wait_up: wait up to 15 s for the server, so a backend that crashed in one reading
# fails that reading, not every reading after it during recovery.
wait_up() { for _ in $(seq 1 30); do [ -n "$(q 'SELECT 1')" ] && return 0; sleep 0.5; done; }
# pg_restart_env ENV... -> 0 once the cluster answers again with ENV in its
# environment, 1 when it does not; it never exits the suite (#1850). It waits out
# a crash recovery first: with the last arm's backend just killed, a fast stop
# sent while the startup process replayed WAL did not finish in pg_ctl's 60 s,
# the start found the old postmaster, and the suite used to exit here before its
# summary. The caller records the failure by name.
pg_restart_env() {
	wait_up
	pgc_pg "pg_ctl -D '$PGC_PGDATA' stop -m fast -w" >/dev/null 2>&1
	pgc_pg "$* pg_ctl -D '$PGC_PGDATA' -l '$PGC_LOGFILE' start -w" >/dev/null 2>&1
	for _ in $(seq 1 30); do [ -n "$(q 'SELECT 1')" ] && return 0; sleep 0.5; done
	return 1
}

# refusal_of SQL PHRASE LABEL -> "<SQLSTATE><LABEL>" when SQL raised with PHRASE in its
# message, the bare SQLSTATE when the message lacks it, or what happened instead. The
# part and complete failures share 58030, so each refusal names its own guard.
refusal_of() {
	local out state
	wait_up
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -qtA 2>&1 <<SQLEOF
\\set VERBOSITY verbose
$1;
SQLEOF
)"
	state="$(sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\): .*/\1/p' <<<"$out" | head -1)"
	if [ -z "$state" ]; then echo "no error ($(head -1 <<<"$out"))"
	elif grep -qF "$2" <<<"$out"; then echo "$state$3"
	else echo "$state"; fi
}
logn() { grep -cE "$1" "$S3_LOG" | tr -d ' '; }
# open_uploads KEY-REGEX -> how many uploads the server still holds open (created,
# neither completed nor aborted) for keys matching it: the billable orphans. The
# DELETE count alone passed an abort that named the wrong upload (404).
open_uploads() {
	if [ -f "$PGC_WORKDIR/__open_uploads__" ]; then grep -cE "$1" "$PGC_WORKDIR/__open_uploads__" | tr -d ' '
	else echo 0; fi
}

mkdir -p "$PGC_WORKDIR/$BUCKET"
python3 "$(dirname "${BASH_SOURCE[0]}")/objstore_http_server.py" \
	--dir "$PGC_WORKDIR" --port "$S3_PORT" --log "$S3_LOG" \
	--sigv4-key "$AKID" --sigv4-secret "$SECRET" --sigv4-region "$REGION" \
	--tamper-bucket "$DENIED_BUCKET" \
	> "$PGC_WORKDIR/sink_server.out" 2>&1 &
SRV_PID=$!
for _ in $(seq 1 50); do
	grep -q READY "$PGC_WORKDIR/sink_server.out" 2>/dev/null && break
	sleep 0.1
done
check "premise: sigv4 fixture server is up" \
	"$(grep -c READY "$PGC_WORKDIR/sink_server.out" 2>/dev/null)" "1"

pg_restart_env "AWS_ENDPOINT_URL='http://127.0.0.1:$S3_PORT'" \
	"AWS_ACCESS_KEY_ID='$AKID'" "AWS_SECRET_ACCESS_KEY='$SECRET'" \
	"AWS_REGION='$REGION'" || { echo "FATAL: cluster did not come back"; exit 1; }

# ---- fixtures: small (single PUT) and large (multipart) ---------------------
psql_run "CREATE TABLE es_small (id int, t text) USING pgcolumnar;"
psql_run "INSERT INTO es_small SELECT g, 'row-'||g FROM generate_series(1,1000) g;"
psql_run "CREATE TABLE es_big (id int8, pad text) USING pgcolumnar;"
psql_run "INSERT INTO es_big SELECT g, md5(g::text)||md5((g+1)::text) FROM generate_series(1,400000) g;"
psql_run "CREATE SERVER wsrv FOREIGN DATA WRAPPER pgcolumnar_parquet;"

# Force the multipart path on this modest fixture: a 256 KB part means the big
# export crosses several parts without generating tens of MiB. Set inline in
# the same session as the big export (below), the form the injection GUC uses.
PART="SET pgcolumnar.objstore_part_size = 262144;"
qset() {  # qset <sql>  -- scalar under the small-part GUC
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "$PART $1" 2>/dev/null | tail -1
}

# ---- step 3: single-object round trips through the module BOTH ways ---------
: > "$S3_LOG"
check_num "small export to s3:// succeeds" \
	"$(q "SELECT pgcolumnar.export_parquet('es_small', 's3://$BUCKET/small.parquet')")" "1000"
check_num "small object went as ONE plain PUT (no multipart)" \
	"$(logn '^PUT /pgc-bucket/small.parquet -')" "1"
psql_run "CREATE FOREIGN TABLE f_small (id int, t text) SERVER wsrv OPTIONS (path 's3://$BUCKET/small.parquet');"
# The oracles are built from the generators, not read back through the columnar scan
# the export itself reads: a scan that lost or bent rows would agree with itself.
GEN_SMALL="SELECT g, 'row-'||g FROM generate_series(1,1000) g"
GEN_BIG="SELECT g::int8, md5(g::text)||md5((g+1)::text) FROM generate_series(1,400000) g"
GEN_PAR="SELECT g::int8, (g % 100)::int FROM generate_series(1,80000) g"
check "small round trip: s3 read-back == source" \
	"$(pgc_set_hash "SELECT * FROM f_small")" "$(pgc_set_hash "$GEN_SMALL")"

: > "$S3_LOG"
BIGROWS=400000
check_num "large export to s3:// succeeds" \
	"$(qset "SELECT pgcolumnar.export_parquet('es_big', 's3://$BUCKET/big.parquet')")" "$BIGROWS"
check "premise: the large export took the multipart protocol" \
	"$([ "$(logn '^POST /pgc-bucket/big.parquet uploads=')" -ge 1 ] && [ "$(logn '^PUT /pgc-bucket/big.parquet partNumber=')" -ge 2 ] && echo yes || echo no)" "yes"
check_num "and completed it exactly once (POST with uploadId, no partNumber)" \
	"$(logn '^POST /pgc-bucket/big.parquet uploadId=')" "1"
psql_run "CREATE FOREIGN TABLE f_big (id int8, pad text) SERVER wsrv OPTIONS (path 's3://$BUCKET/big.parquet');"
check "large round trip: s3 read-back == source" \
	"$(pgc_set_hash "SELECT * FROM f_big")" "$(pgc_set_hash "$GEN_BIG")"

# #1904 review: an s3:// key can hold a literal '?'. The request path encodes it
# as %3F, so each request starts its own query with '?'. A test of the URL, which
# still holds the '?', would join with '&' and put the query inside the path.
# The read goes through /vswap/, the fixture's versioned bucket, so it is pinned
# by version id. A '?' in an s3:// key makes the read list the bucket first, and
# the listing of bucket vswap looks under vswap/ on disk, so the copy there lets
# the listing find the key. The GETs still read the exported object.
: > "$S3_LOG"
sq_rows="$(qset "SELECT pgcolumnar.export_parquet('es_big', 's3://$BUCKET/q?.parquet')")"
mkdir -p "$PGC_WORKDIR/vswap/$BUCKET"
cp "$PGC_WORKDIR/$BUCKET/q?.parquet" "$PGC_WORKDIR/vswap/$BUCKET/q?.parquet" 2>/dev/null
sq_back="$(pgc_set_hash "SELECT * FROM pgcolumnar.read_parquet('s3://vswap/$BUCKET/q?.parquet') AS t(id int8, pad text)")"
sq_all="$(awk '$2 ~ /^(\/vswap)?\/pgc-bucket\/q/ { print $1, $2, $3 }' "$S3_LOG")"
sq_n="$(printf '%s' "$sq_all" | grep -c .)"
sq_bad="$(printf '%s' "$sq_all" | awk '$2 != "/pgc-bucket/q%3F.parquet" && $2 != "/vswap/pgc-bucket/q%3F.parquet"')"
sq_badn="$(printf '%s' "$sq_bad" | grep -c .)"
[ "$sq_badn" -gt 0 ] && sq_badn="$sq_badn (first: $(head -1 <<<"$sq_bad"))"
sq_gets="$(printf '%s\n' "$sq_all" | awk '$1 == "GET"' | grep -c .)"
sq_unpinned="$(printf '%s\n' "$sq_all" | awk '$1 == "GET" && $3 !~ /^versionId=/' | grep -c .)"
sq_wgets="$sq_gets"; [ "$sq_gets" -ge 1 ] 2>/dev/null || sq_wgets="at least 1"
check "s3:// export and pinned read of a key with a literal '?' keep each request's query out of the path" \
	"rows: ${sq_rows:-none}; requests with more than the key in the path: $sq_badn of $sq_n; GETs without versionId: $sq_unpinned of $sq_gets; read-back: $sq_back" \
	"rows: $BIGROWS; requests with more than the key in the path: 0 of $sq_n; GETs without versionId: 0 of $sq_wgets; read-back: $(pgc_set_hash "$GEN_BIG")"

# ---- the part size has a ceiling the sink can hold (#1812) -------------------
# The sink stages one part in one allocation, and PostgreSQL caps one allocation at
# 1 GiB. The setting took any value up to INT_MAX, so a part size near or above 1 GiB
# failed every export over 1 GiB with a raw string-buffer error. Its maximum is now
# 512 MiB, and SET refuses more. 512 MiB itself is accepted.
part_set() {	# part_set VALUE -> "accepted", or the ERROR text of the refusal
	local out e
	out="$(psql_run "SET pgcolumnar.objstore_part_size = $1" 2>&1)"
	e="$(sed -n 's/^ERROR: *//p' <<<"$out" | head -1)"
	echo "${e:-accepted}"
}
check "objstore_part_size refuses a value above its 512 MiB maximum" \
	"$(part_set 536870913)" \
	'536870913 is outside the valid range for parameter "pgcolumnar.objstore_part_size" (0 .. 536870912)'
check "objstore_part_size accepts its 512 MiB maximum" "$(part_set 536870912)" "accepted"

# A chunk bigger than the room left in the part (#1812). The sink appended the whole
# chunk before it cut a part, so it staged up to one part plus one chunk. A writer's
# chunk is a whole Arrow record batch. Here 2000 rows of 250000 bytes, about 500 MB in
# two batches, are staged under the 512 MiB part when the writer hands over the batch
# of one row with two 400 MB values: 1.3 GB, past what one allocation holds. The sink
# now fills the part, uploads it, and goes on, so it never stages more than one part.
# Opt-in, as large_values.sh's arm is: the export is about 1.3 GB.
OVERPART="an 800 MB chunk over 500 MB already staged uploads whole at the 512 MiB part size, in 3 parts"
if [ "${PGC_RUN_OVER_1GB:-0}" = 1 ]; then
	psql_run "CREATE TABLE es_staged (id int, a text, b text) USING pgcolumnar;
	          SELECT pgcolumnar.set_options('es_staged', stripe_row_limit => 1000);
	          INSERT INTO es_staged SELECT g, repeat('y', 250000), '' FROM generate_series(1, 2000) g;
	          INSERT INTO es_staged VALUES (2001, repeat('z', 400000000), repeat('w', 400000000));" >/dev/null
	: > "$S3_LOG"
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -At \
		-c "SET statement_timeout = '900s'" -c "SET pgcolumnar.objstore_part_size = 536870912" \
		-c "SELECT pgcolumnar.export_arrow('es_staged', 's3://$BUCKET/staged.arrow')" 2>&1)"
	got="$(grep -m1 '^ERROR' <<<"$out" || tail -1 <<<"$out")"
	q "SELECT pgcolumnar.export_arrow('es_staged', '$PGC_WORKDIR/staged-local.arrow')" >/dev/null
	same="$(cmp -s "$PGC_WORKDIR/$BUCKET/staged.arrow" "$PGC_WORKDIR/staged-local.arrow" && echo identical \
		|| echo "differs ($(stat -c %s "$PGC_WORKDIR/$BUCKET/staged.arrow" 2>/dev/null || echo missing) vs $(stat -c %s "$PGC_WORKDIR/staged-local.arrow" 2>/dev/null || echo missing) bytes)")"
	check "$OVERPART" "$got|parts=$(logn '^PUT /pgc-bucket/staged.arrow partNumber=')|$same" "2001|parts=3|identical"
	rm -f "$PGC_WORKDIR/$BUCKET/staged.arrow" "$PGC_WORKDIR/staged-local.arrow"
	psql_run "DROP TABLE es_staged;"
else
	check_skip "$OVERPART" "SKIP  $OVERPART (set PGC_RUN_OVER_1GB=1; it needs several GB of memory and disk)" "opt-in heavy arm"
fi

# ---- injection: abort mid-part leaves NOTHING -------------------------------
: > "$S3_LOG"
# fail_after well above one part so the fault lands mid-multipart, after at
# least one part and the CreateMultipartUpload.
check "inject: mid-upload failure surfaces as disk-full (53100)" \
	"$(refusal_of "$PART SET pgcolumnar.sink_fail_after = 900000; SELECT pgcolumnar.export_parquet('es_big', 's3://$BUCKET/fail.parquet')" \
		"export write failed (injected fault)" " by the injected fault")" "53100 by the injected fault"
check "inject: the key does not exist" \
	"$([ ! -e "$PGC_WORKDIR/$BUCKET/fail.parquet" ] && echo clean)" "clean"
# the money check: every multipart upload STARTED for this key was ABORTED, so
# nothing dangles to bill (robust to exactly where the injected fault landed -
# if it landed before multipart began, both counts are 0).
STARTED="$(logn '^POST /pgc-bucket/fail.parquet uploads=')"
ABORTED="$(logn '^DELETE /pgc-bucket/fail.parquet uploadId=')"
check "premise: the fault landed mid-multipart (an upload was started)" \
	"$([ "${STARTED:-0}" -ge 1 ] && echo yes)" "yes"
check_num "inject: every upload started was aborted (no billable orphan)" \
	"$ABORTED" "$STARTED"
check_num "inject: the server holds no open upload for the key" \
	"$(open_uploads '^pgc-bucket/fail\.parquet$')" "0"

# ---- a part the server refuses fails the export; nothing completes ----------
# Parts 2 and up are answered 500 and not stored. Read as stored, the export would
# complete a short object (or reach the complete with parts missing, 58030 too),
# so the refusal must be the part check's own.
: > "$S3_LOG"
echo "500 2" > "$PGC_WORKDIR/__part_status__"
check "part failure: a refused part fails the export (58030)" \
	"$(refusal_of "$PART SELECT pgcolumnar.export_parquet('es_big', 's3://$BUCKET/partfail.parquet')" \
		"uploading a part of" " by the part check")" "58030 by the part check"
rm -f "$PGC_WORKDIR/__part_status__"
check "part failure: the key does not exist" \
	"$([ ! -e "$PGC_WORKDIR/$BUCKET/partfail.parquet" ] && echo clean)" "clean"
check "premise: the part failure landed mid-multipart (an upload was started)" \
	"$([ "$(logn '^POST /pgc-bucket/partfail.parquet uploads=')" -ge 1 ] && echo yes)" "yes"
check_num "part failure: the server holds no open upload for the key" \
	"$(open_uploads '^pgc-bucket/partfail\.parquet$')" "0"

# ---- #1830 review: an abort answered 204 with no Content-Length -------------
# Garage answers a DELETE with 204, a Date header and nothing else, and keeps
# the connection open. A 204 has no body whatever its headers say (RFC 9112
# section 6.3). A client that reads it as a body ending at connection close
# waits until the store closes the idle connection: 59 s against Garage. The
# fixture's __delete_unframed__ answers the same way and closes after 8 s, so
# the export must return well inside that.
: > "$S3_LOG"
echo 8 > "$PGC_WORKDIR/__delete_unframed__"
echo "500 2" > "$PGC_WORKDIR/__part_status__"
t0="$(date +%s%N)"
AB204="$(refusal_of "$PART SELECT pgcolumnar.export_parquet('es_big', 's3://$BUCKET/abort204.parquet')" \
	"uploading a part of" " by the part check")"
t1="$(date +%s%N)"
rm -f "$PGC_WORKDIR/__part_status__" "$PGC_WORKDIR/__delete_unframed__"
AB204_MS=$(( (t1 - t0) / 1000000 ))
check "unframed 204: a refused part fails the export (58030)" "$AB204" "58030 by the part check"
check "premise: unframed 204: the export sent one abort DELETE" \
	"$(logn '^DELETE /pgc-bucket/abort204.parquet uploadId=')" "1"
check "unframed 204: the export returned within 4 s when its abort was answered 204 with no Content-Length" \
	"$( (( AB204_MS <= 4000 )) && echo yes || echo "no, after $AB204_MS ms")" "yes"
check_num "unframed 204: the server holds no open upload for the key" \
	"$(open_uploads '^pgc-bucket/abort204\.parquet$')" "0"

# ---- taxonomy: the write side wears the same gates as the read side ---------
check "allow-list: an unlisted endpoint refuses 42501 before any write" \
	"$(refusal_of "SELECT pgcolumnar.export_parquet('es_small', 'http://127.0.0.2:1/x.parquet')" \
		"is not in pgcolumnar.objstore_allowed_endpoints" " by the allow-list")" "42501 by the allow-list"
# the object is read back: imported from the bucket into a fresh table and compared
# with the generator (a count > 0 passed with the object's last byte missing)
psql_run "CREATE TABLE es_arrow_back (id int, t text) USING pgcolumnar;"
q "SELECT pgcolumnar.export_arrow('es_small', 's3://$BUCKET/small.arrow')" >/dev/null
q "SELECT pgcolumnar.import_arrow('es_arrow_back', '$PGC_WORKDIR/$BUCKET/small.arrow')" >/dev/null
check "arrow export to s3:// also round-trips" \
	"$(pgc_set_hash "SELECT * FROM es_arrow_back")" "$(pgc_set_hash "$GEN_SMALL")"

# ---- #1830: a write reply with no length the client can count on -----------
# HTTP/1.1 lets a reply end its body by closing the connection, with neither a
# Content-Length nor chunked framing. The write path read a reply body only when
# one of those two was present, so the UploadId reply of CreateMultipartUpload
# was dropped and the export failed. An HTTP/1.0 reply without keep-alive does
# not keep its connection either, and the write path sent its next request on it.
# The fixture's __uploads_unframed__ hook answers CreateMultipartUpload each way.
# first_line SQL -> the first line psql prints for SQL, its result or its error.
first_line() {
	wait_up
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -qtA -c "$1" 2>&1 | head -1
}
: > "$S3_LOG"
echo close > "$PGC_WORKDIR/__uploads_unframed__"
check "unframed reply: a multipart export succeeds when the UploadId reply ends at connection close" \
	"$(first_line "$PART SELECT pgcolumnar.export_parquet('es_big', 's3://$BUCKET/uc.parquet')")" \
	"$BIGROWS"
rm -f "$PGC_WORKDIR/__uploads_unframed__"
check "premise: unframed reply: the export sent CreateMultipartUpload once" \
	"$(logn '^POST /pgc-bucket/uc.parquet uploads=')" "1"
# The fixture ends that body by shutting down only its sending side, so it can
# still see a request the export sends on the same connection (#1830 review).
check "unframed reply: no request went on the connection the close-delimited reply ended" \
	"$(logn '^REUSED ') reused ($(grep -m1 '^REUSED ' "$S3_LOG" || echo none))" "0 reused (none)"
check "unframed reply: the object reads back as the generator" \
	"$(pgc_set_hash "SELECT * FROM pgcolumnar.read_parquet('s3://$BUCKET/uc.parquet') AS t(id int8, pad text)")" \
	"$(pgc_set_hash "$GEN_BIG")"
: > "$S3_LOG"
echo http10 > "$PGC_WORKDIR/__uploads_unframed__"
check "HTTP/1.0 reply: a multipart export succeeds" \
	"$(first_line "$PART SELECT pgcolumnar.export_parquet('es_big', 's3://$BUCKET/u10.parquet')")" \
	"$BIGROWS"
rm -f "$PGC_WORKDIR/__uploads_unframed__"
check "premise: HTTP/1.0 reply: the export sent CreateMultipartUpload once" \
	"$(logn '^POST /pgc-bucket/u10.parquet uploads=')" "1"
check "HTTP/1.0 reply: no request went on the connection the HTTP/1.0 reply ended" \
	"$(logn '^REUSED ') reused ($(grep -m1 '^REUSED ' "$S3_LOG" || echo none))" "0 reused (none)"

# ---- #1857: S3 numbers parts 1 to 10000 --------------------------------------
# An upload holds at most 10000 parts; the fixture refuses part 10001 as S3 does.
# The export must stop before it sends part 10001, say why, and abort the upload.
# 1 KiB parts make the export need more than 10000 of them.
: > "$S3_LOG"
check "part limit: an export that needs more than 10000 parts is refused by the part limit (54000)" \
	"$(refusal_of "SET pgcolumnar.objstore_part_size = 1024; SELECT pgcolumnar.export_parquet('es_big', 's3://$BUCKET/many.parquet')" \
		"more than 10000 parts" " by the part limit")" "54000 by the part limit"
check "part limit: the export uploaded parts 1 to 10000" \
	"$(awk '$1 == "PUT" && $2 == "/pgc-bucket/many.parquet" && match($3, /partNumber=[0-9]+/) { n = substr($3, RSTART + 11, RLENGTH - 11) + 0; if (n <= 10000) lo++ } END { print lo + 0 }' "$S3_LOG")" "10000"
check_num "part limit: no part numbered above 10000 was sent" \
	"$(awk '$1 == "PUT" && $2 == "/pgc-bucket/many.parquet" && match($3, /partNumber=[0-9]+/) { n = substr($3, RSTART + 11, RLENGTH - 11) + 0; if (n > 10000) hi++ } END { print hi + 0 }' "$S3_LOG")" "0"
check_num "part limit: the server holds no open upload for the key" \
	"$(open_uploads '^pgc-bucket/many\.parquet$')" "0"
check "part limit: the key does not exist" \
	"$([ -e "$PGC_WORKDIR/$BUCKET/many.parquet" ] && echo "present ($(stat -c %s "$PGC_WORKDIR/$BUCKET/many.parquet") bytes)" || echo absent)" "absent"

# ---- step 4 (#623): parallel_export_parquet to an s3:// prefix --------------
# The workers already write per-worker objects remotely through the sink; this
# arm proves the dispatcher's remote-awareness: it does not choke on the prefix
# in pexport_prepare_dir, and on cancel it removes the completed keys it wrote.
psql_run "CREATE TABLE es_par (id int8, v int) USING pgcolumnar;"
psql_run "SELECT pgcolumnar.set_options('es_par', stripe_row_limit => 5000);"
psql_run "INSERT INTO es_par SELECT g, g % 100 FROM generate_series(1,80000) g;"
PARROWS=80000

: > "$S3_LOG"
check_num "parallel export to an s3:// prefix succeeds" \
	"$(q "SELECT pgcolumnar.parallel_export_parquet('es_par'::regclass, 's3://$BUCKET/par', 4)")" "$PARROWS"
NOBJ="$(ls "$PGC_WORKDIR/$BUCKET/par/" 2>/dev/null | grep -c 'part-.*\.parquet$')"
check "parallel: one object per worker was written (4)" "$NOBJ" "4"
check "parallel: every worker PUT went to the prefix" \
	"$([ "$(logn '^PUT /pgc-bucket/par/part-')" -ge 4 ] && echo yes)" "yes"
# #394: a completed remote export writes a _SUCCESS marker object at the prefix,
# put through the same remote sink (a single PUT), so a consumer listing the
# prefix sees the run completed. It is not a part object (NOBJ==4 above).
check "parallel remote: a _SUCCESS marker object exists at the prefix" \
	"$([ -f "$PGC_WORKDIR/$BUCKET/par/_SUCCESS" ] && echo yes || echo no)" "yes"
check "parallel remote: the marker went to the prefix as a PUT" \
	"$([ "$(logn '^PUT /pgc-bucket/par/_SUCCESS')" -ge 1 ] && echo yes)" "yes"
# read each key back and union; the union equals the source (prefix-union read
# is #619, so this reads the four exact keys, not the prefix).
for i in 0 1 2 3; do
	psql_run "CREATE FOREIGN TABLE fpar_$i (id int8, v int) SERVER wsrv OPTIONS (path 's3://$BUCKET/par/part-000$i.parquet');"
done
check "parallel: the union of the per-worker objects == source" \
	"$(pgc_set_hash "SELECT * FROM fpar_0 UNION ALL SELECT * FROM fpar_1 UNION ALL SELECT * FROM fpar_2 UNION ALL SELECT * FROM fpar_3")" \
	"$(pgc_set_hash "$GEN_PAR")"

# a non-empty prefix is a documented caveat, not enforced remotely: a re-run
# into the same prefix overwrites the same keys and still round-trips.
: > "$S3_LOG"
check_num "parallel: a re-run into the same prefix succeeds (overwrite)" \
	"$(q "SELECT pgcolumnar.parallel_export_parquet('es_par'::regclass, 's3://$BUCKET/par', 4)")" "$PARROWS"

# #1781: a failure after every worker finished, in the stale-part listing or in the
# marker PUT, is a failed run, so the dispatcher removes this run's parts as it does
# for a failed worker (docs: a failed run leaves none). lf first holds a complete
# 2-worker run with its marker; the re-run's listing is then refused 403. The parts
# the re-run wrote must go, and so must the earlier marker, or it would certify them.
q "SELECT pgcolumnar.parallel_export_parquet('es_par'::regclass, 's3://$BUCKET/lf', 2)" >/dev/null
check "premise: lister refused: an earlier complete run left its marker at lf/" \
	"$([ -f "$PGC_WORKDIR/$BUCKET/lf/_SUCCESS" ] && echo yes || echo no)" "yes"
: > "$S3_LOG"
echo 403 > "$PGC_WORKDIR/__listing_status__"
check "lister refused: the export fails with the listing's 28000" \
	"$(refusal_of "SELECT pgcolumnar.parallel_export_parquet('es_par'::regclass, 's3://$BUCKET/lf', 2)" \
		"was denied" " by the listing")" "28000 by the listing"
rm -f "$PGC_WORKDIR/__listing_status__"
check "premise: lister refused: both workers' parts were uploaded before the listing" \
	"$(n=$(logn '^PUT /pgc-bucket/lf/part-'); [ "$n" -ge 2 ] && echo yes || echo "no ($n part PUTs)")" "yes"
check_num "lister refused: no part object remains at the prefix" \
	"$(ls "$PGC_WORKDIR/$BUCKET/lf/" 2>/dev/null | grep -c 'part-.*\.parquet$')" "0"
check "lister refused: no _SUCCESS marker certifies the failed run's prefix" \
	"$([ -e "$PGC_WORKDIR/$BUCKET/lf/_SUCCESS" ] && echo present || echo absent)" "absent"

# The same for a refused marker PUT: the listing succeeds, the _SUCCESS PUT is 403.
: > "$S3_LOG"
echo "403 $BUCKET/mf/_SUCCESS" > "$PGC_WORKDIR/__put_status__"
check "marker refused: the export fails with the marker PUT's 28000" \
	"$(refusal_of "SELECT pgcolumnar.parallel_export_parquet('es_par'::regclass, 's3://$BUCKET/mf', 2)" \
		"mf/_SUCCESS" " by the marker PUT")" "28000 by the marker PUT"
rm -f "$PGC_WORKDIR/__put_status__"
check "premise: marker refused: both workers' parts were uploaded before the marker" \
	"$(n=$(logn '^PUT /pgc-bucket/mf/part-'); [ "$n" -ge 2 ] && echo yes || echo "no ($n part PUTs)")" "yes"
check_num "marker refused: no part object remains at the prefix" \
	"$(ls "$PGC_WORKDIR/$BUCKET/mf/" 2>/dev/null | grep -c 'part-.*\.parquet$')" "0"

# #1781 review: what docs/sql-reference.md says a refused listing leaves. Without
# the listing the export cannot find a LARGER earlier run's higher-numbered parts:
# a 4-worker run, then a 2-worker re-run whose listing is refused, removes the
# re-run's part-0000/0001 and the marker but leaves the earlier part-0002/0003. A
# reader must check for _SUCCESS first. Pinned so a change to either side is seen.
# The same refusal's message must name the prefix it listed (lt, or lt/ once the
# reconcile lists with a slash), not the dummy key "s3://pgc-bucket/_" the
# listing's handle was resolved from.
q "SELECT pgcolumnar.parallel_export_parquet('es_par'::regclass, 's3://$BUCKET/lt', 4)" >/dev/null
check "premise: listing refused after a larger run: the 4-worker run left 4 parts and its marker at lt/" \
	"$(echo "$(ls "$PGC_WORKDIR/$BUCKET/lt/" 2>/dev/null | grep -c 'part-.*\.parquet$') parts, marker $([ -f "$PGC_WORKDIR/$BUCKET/lt/_SUCCESS" ] && echo present || echo absent)")" \
	"4 parts, marker present"
echo 403 > "$PGC_WORKDIR/__listing_status__"
LT_ERR="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -qtA \
	-c "SELECT pgcolumnar.parallel_export_parquet('es_par'::regclass, 's3://$BUCKET/lt', 2)" 2>&1)"
rm -f "$PGC_WORKDIR/__listing_status__"
LT_URL="$(sed -n 's/^ERROR:  columnar: access to "\([^"]*\)" was denied.*/\1/p' <<<"$LT_ERR" | head -1)"
echo "LT error: $(head -1 <<<"$LT_ERR")"
check "listing error: the refused listing names the prefix it listed, not a dummy key" \
	"${LT_URL:-no access-denied error ($(head -1 <<<"$LT_ERR"))}" "s3://$BUCKET/lt$( [ "${LT_URL: -1}" = / ] && echo /)"
LT_LEFT="$(ls "$PGC_WORKDIR/$BUCKET/lt/" 2>/dev/null | tr '\n' ' ' | sed 's/ $//')"
echo "LT contents: ${LT_LEFT:-(empty)}"
check "listing refused after a larger run: the earlier run's part-0002 and part-0003 remain, and nothing else" \
	"${LT_LEFT:-(empty)}" "part-0002.parquet part-0003.parquet"
check "listing refused after a larger run: no _SUCCESS marker remains at the prefix" \
	"$([ -e "$PGC_WORKDIR/$BUCKET/lt/_SUCCESS" ] && echo present || echo absent)" "absent"

# #632 review: a SMALLER re-run into a used prefix must not leave the prior
# larger run's higher-numbered parts under a freshly-stamped _SUCCESS. First 4
# workers, then 2: the reconcile drops the stale part-0002/0003, so the marker
# certifies exactly the 2 parts this run wrote.
q "SELECT pgcolumnar.parallel_export_parquet('es_par'::regclass, 's3://$BUCKET/sr', 4)" >/dev/null
check "shrink: the 4-worker run wrote 4 parts + marker" \
	"$([ "$(ls "$PGC_WORKDIR/$BUCKET/sr/" 2>/dev/null | grep -c 'part-.*parquet$')" = 4 ] && [ -f "$PGC_WORKDIR/$BUCKET/sr/_SUCCESS" ] && echo ok)" "ok"
q "SELECT pgcolumnar.parallel_export_parquet('es_par'::regclass, 's3://$BUCKET/sr', 2)" >/dev/null
check_num "shrink: a smaller re-run leaves exactly its own 2 parts (stale tail dropped)" \
	"$(ls "$PGC_WORKDIR/$BUCKET/sr/" 2>/dev/null | grep -c 'part-.*parquet$')" "2"
check "shrink: parts 0002/0003 from the larger run are gone" \
	"$([ ! -e "$PGC_WORKDIR/$BUCKET/sr/part-0002.parquet" ] && [ ! -e "$PGC_WORKDIR/$BUCKET/sr/part-0003.parquet" ] && echo gone)" "gone"
check "shrink: _SUCCESS certifies the reconciled prefix" \
	"$([ -f "$PGC_WORKDIR/$BUCKET/sr/_SUCCESS" ] && echo yes)" "yes"

# #1782: the documented prefix form ends in a slash. The keys must not gain an
# empty segment ("sl//part-0000.parquet"): S3 stores that as a different key,
# and the documented glob then matches nothing. This fixture stores objects as
# files, which collapse "//", so only the request log shows the defect.
#
# The stale-part reconcile lists the prefix, and an S3 listing matches leading
# characters, not folders: a listing of "sl" also returns "slx/..." and
# "sl2/...". So each export below runs beside part objects under two sibling
# prefixes, slx/ and sl2/, with indexes above the run's worker count, and every
# form of the prefix must leave them where they are.
plant_siblings() {
	mkdir -p "$PGC_WORKDIR/$BUCKET/slx" "$PGC_WORKDIR/$BUCKET/sl2"
	printf 'x' > "$PGC_WORKDIR/$BUCKET/slx/part-0007.parquet"
	printf 'x' > "$PGC_WORKDIR/$BUCKET/sl2/part-0005.parquet"
}
# siblings_left -> which planted sibling objects are still there, or "none"
siblings_left() {
	local left
	left="$(cd "$PGC_WORKDIR/$BUCKET" && ls slx/part-0007.parquet sl2/part-0005.parquet 2>/dev/null | sort | tr '\n' ' ')"
	left="${left% }"
	echo "${left:-none}"
}
SIBLINGS="sl2/part-0005.parquet slx/part-0007.parquet"
# sl_put_keys -> how many distinct sl/part-NNNN.parquet keys (one slash) took a PUT
sl_put_keys() { grep -oE '^PUT /pgc-bucket/sl/part-[0-9]+\.parquet' "$S3_LOG" | sort -u | wc -l | tr -d ' '; }
sl_parts() { ls "$PGC_WORKDIR/$BUCKET/sl/" 2>/dev/null | grep -c 'part-.*\.parquet$'; }

plant_siblings
: > "$S3_LOG"
check_num "trailing slash: parallel export to an s3:// prefix ending in / succeeds" \
	"$(q "SELECT pgcolumnar.parallel_export_parquet('es_par'::regclass, 's3://$BUCKET/sl/', 4)")" "$PARROWS"
check_num "trailing slash: every worker PUT went to sl/part- (one slash)" "$(sl_put_keys)" "4"
check "trailing slash: the marker went to sl/_SUCCESS (one slash)" \
	"$([ "$(logn '^PUT /pgc-bucket/sl/_SUCCESS')" -ge 1 ] && echo yes)" "yes"
check_num "trailing slash: no request named a key with an empty segment (//)" \
	"$(logn '^[A-Z]+ /pgc-bucket/[^ ]*//')" "0"
check "trailing slash: the documented glob read-back equals the source" \
	"$(pgc_set_hash "SELECT * FROM pgcolumnar.read_parquet('s3://$BUCKET/sl/*.parquet') AS t(id int8, v int)")" \
	"$(pgc_set_hash "$GEN_PAR")"
check "trailing slash: an export to sl/ leaves the parts under slx/ and sl2/" \
	"$(siblings_left)" "$SIBLINGS"

# the same prefix without the slash, and fewer workers: the reconcile must drop
# sl/part-0002 and 0003 and nothing outside sl/. A listing with no delimiter
# also returns keys below sl/, so a part under sl/sub/ (another export's
# prefix) is planted too, with an index above this run's 2 workers.
plant_siblings
mkdir -p "$PGC_WORKDIR/$BUCKET/sl/sub"
printf 'x' > "$PGC_WORKDIR/$BUCKET/sl/sub/part-0009.parquet"
check_num "no slash: a 2-worker re-run to s3://bucket/sl succeeds" \
	"$(q "SELECT pgcolumnar.parallel_export_parquet('es_par'::regclass, 's3://$BUCKET/sl', 2)")" "$PARROWS"
check_num "no slash: the re-run leaves exactly its own 2 parts under sl/" "$(sl_parts)" "2"
check "no slash: a re-run to sl leaves the parts under slx/ and sl2/" \
	"$(siblings_left)" "$SIBLINGS"
check "no slash: a re-run to sl leaves the nested sl/sub/part-0009.parquet" \
	"$(cd "$PGC_WORKDIR/$BUCKET" && ls sl/sub/part-0009.parquet 2>/dev/null || echo none)" \
	"sl/sub/part-0009.parquet"

# several trailing slashes strip to the one prefix
plant_siblings
: > "$S3_LOG"
check_num "several slashes: a 3-worker export to s3://bucket/sl// succeeds" \
	"$(q "SELECT pgcolumnar.parallel_export_parquet('es_par'::regclass, 's3://$BUCKET/sl//', 3)")" "$PARROWS"
check_num "several slashes: every worker PUT went to sl/part- (one slash)" "$(sl_put_keys)" "3"
check_num "several slashes: no request named a key with an empty segment (//)" \
	"$(logn '^[A-Z]+ /pgc-bucket/[^ ]*//')" "0"
check_num "several slashes: sl/ holds exactly this run's 3 parts" "$(sl_parts)" "3"
check "several slashes: an export to sl// leaves the parts under slx/ and sl2/" \
	"$(siblings_left)" "$SIBLINGS"

# The scheme is matched in any case, so "S3://bucket/up/" names the same
# prefix as "s3://bucket/up". The listing returns keys spelled "s3://...", so
# a reconcile that compared them with the prefix as given dropped nothing for
# "S3://", and the marker then certified a mixed set of parts. First 4 workers
# with "s3://", then 2 with "S3://...up/": parts 0002 and 0003 must go.
q "SELECT pgcolumnar.parallel_export_parquet('es_par'::regclass, 's3://$BUCKET/up', 4)" >/dev/null
: > "$S3_LOG"
check_num "uppercase scheme: a 2-worker re-run to S3://bucket/up/ succeeds" \
	"$(q "SELECT pgcolumnar.parallel_export_parquet('es_par'::regclass, 'S3://$BUCKET/up/', 2)")" "$PARROWS"
check_num "uppercase scheme: a 2-worker re-run to S3://bucket/up/ leaves exactly its own 2 parts" \
	"$(ls "$PGC_WORKDIR/$BUCKET/up/" 2>/dev/null | grep -c 'part-.*\.parquet$')" "2"
check "uppercase scheme: the re-run sent a DELETE for up/part-0002 and up/part-0003" \
	"$(grep -oE '^DELETE /pgc-bucket/up/part-000[23]\.parquet' "$S3_LOG" | sort -u | sed 's|^DELETE /pgc-bucket/||' | tr '\n' ' ' | sed 's/ $//')" \
	"up/part-0002.parquet up/part-0003.parquet"

# A bare bucket names no prefix. The export refuses it before any worker
# starts, so nothing is written at the bucket root.
: > "$S3_LOG"
check "bare bucket: an export to s3://bucket/ is refused as naming no prefix" \
	"$(refusal_of "SELECT pgcolumnar.parallel_export_parquet('es_par'::regclass, 's3://$BUCKET/', 2)" \
		"names no prefix under the bucket" " (no prefix)")" "22023 (no prefix)"
check "bare bucket: an export to s3://bucket is refused as naming no prefix" \
	"$(refusal_of "SELECT pgcolumnar.parallel_export_parquet('es_par'::regclass, 's3://$BUCKET', 2)" \
		"names no prefix under the bucket" " (no prefix)")" "22023 (no prefix)"
check_num "bare bucket: the refused exports sent no PUT" "$(logn '^PUT ')" "0"
check_num "bare bucket: no part object at the bucket root" \
	"$(ls -p "$PGC_WORKDIR/$BUCKET/" 2>/dev/null | grep -c '^part-.*\.parquet$')" "0"

# #1832: after the workers finish, the export lists its prefix to delete stale
# parts, and only an s3:// prefix can be listed. A gs:// export used to upload
# every part and then fail at that listing. It is now refused before any worker
# starts, so nothing is uploaded.
: > "$S3_LOG"
check "gs:// prefix: a parallel export is refused before any worker starts, with no request sent" \
	"$(refusal_of "SELECT pgcolumnar.parallel_export_parquet('es_par'::regclass, 'gs://$BUCKET/gx', 2)" \
		"parallel_export_parquet cannot write to" " by the scheme check"), $(logn 'gx/part-') requests" \
	"0A000 by the scheme check, 0 requests"

# #1849: the upload cleanup lists "<prefix>/". The dispatcher refuses a prefix
# too long for the longest name it builds under it before any worker starts, so
# a prefix whose "/" would not fit a MAXPGPATH buffer never reaches the cleanup.
: > "$S3_LOG"
LONGP="s3://$BUCKET/$(head -c $((1023 - ${#BUCKET} - 6)) /dev/zero | tr '\0' L)"
check "long prefix: a 1023-byte s3:// prefix is refused as too long, with no request sent" \
	"$(refusal_of "SELECT pgcolumnar.parallel_export_parquet('es_par'::regclass, '$LONGP', 2)" \
		"parallel export destination is too long" " (too long)"), ${#LONGP} bytes, $(logn '^[A-Z]+ /pgc-bucket/LLLL') requests" \
	"54000 (too long), 1023 bytes, 0 requests"

# #1780: the workers obey the calling session's settings, not only the system's. A
# background worker starts from postgresql.conf, so before the fix a session that SET
# the allow-list to '' still had its workers upload part-0000, and the error came only
# afterwards, from the dispatcher, with the part left behind.
: > "$S3_LOG"
check "premise: a session SET to '' makes parallel_export_parquet fail 42501, with or without the dispatcher's check" \
	"$(refusal_of "SET pgcolumnar.objstore_allowed_endpoints = ''; SELECT pgcolumnar.parallel_export_parquet('es_par'::regclass, 's3://$BUCKET/sa', 2)" \
		"is not in pgcolumnar.objstore_allowed_endpoints" " by the allow-list")" "42501 by the allow-list"
# who refused: the dispatcher itself, or a worker, whose error the dispatcher re-raises
# as "worker N failed". With the settings handed to the workers but no allow-list check
# in the dispatcher, worker 0 refuses. (With neither, the dispatcher refuses only after
# the workers uploaded; the next check catches that.)
refused_by() {  # refused_by SQL -> "refused by the dispatcher" | "refused by worker N" | what happened
	local out
	wait_up
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -qtA -c "$1" 2>&1)"
	if grep -qE 'worker [0-9]+ failed' <<<"$out"; then
		echo "refused by $(grep -oE 'worker [0-9]+' <<<"$out" | head -1)"
	elif grep -qF "is not in pgcolumnar.objstore_allowed_endpoints" <<<"$out"; then
		echo "refused by the dispatcher"
	else echo "not refused ($(head -1 <<<"$out"))"; fi
}
check "session allow-list: the dispatcher raises the refusal, not a worker" \
	"$(refused_by "SET pgcolumnar.objstore_allowed_endpoints = ''; SELECT pgcolumnar.parallel_export_parquet('es_par'::regclass, 's3://$BUCKET/sa', 2)")" \
	"refused by the dispatcher"
check "session allow-list: nothing reached the refused prefix (no PUT, no POST)" \
	"$(logn '^(PUT|POST) /pgc-bucket/sa/') requests, $(ls "$PGC_WORKDIR/$BUCKET/sa/" 2>/dev/null | wc -l | tr -d ' ') objects" \
	"0 requests, 0 objects"
# the reverse: the system list is empty and the session SETs the endpoint
sys_list_is() {  # sys_list_is VALUE: wait up to 10 s for a new session to see VALUE;
	# prints what the last session saw, in brackets, so a reload that never lands shows
	local seen
	for _ in $(seq 1 50); do
		seen="$(q "SELECT current_setting('pgcolumnar.objstore_allowed_endpoints')")"
		[ "$seen" = "$1" ] && break
		sleep 0.2
	done
	echo "[$seen]"
}
psql_run "ALTER SYSTEM SET pgcolumnar.objstore_allowed_endpoints = ''"
psql_run "SELECT pg_reload_conf()" >/dev/null
# the widen arm proves nothing unless the system list really is empty: record what a
# new session sees, so a reload that never lands fails here rather than passing below
check "session allow-list: a new session sees an empty system list before the widen arm" \
	"$(sys_list_is "")" "[]"
check "session allow-list: a session SET widens an empty system list for the workers" \
	"$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq \
		-c "SET pgcolumnar.objstore_allowed_endpoints = '127.0.0.1'" \
		-c "SELECT pgcolumnar.parallel_export_parquet('es_par'::regclass, 's3://$BUCKET/sw', 2)" 2>&1 | tail -1)" "80000"
psql_run "ALTER SYSTEM RESET pgcolumnar.objstore_allowed_endpoints"
psql_run "SELECT pg_reload_conf()" >/dev/null
sys_list_is "127.0.0.1" >/dev/null
# a part far below each worker's object forces multipart only if the workers read it
: > "$S3_LOG"
q "SET pgcolumnar.objstore_part_size = 16384; SELECT pgcolumnar.parallel_export_parquet('es_par'::regclass, 's3://$BUCKET/pz', 2)" >/dev/null
check "parallel: a session SET of objstore_part_size reaches the workers (multipart)" \
	"$([ "$(logn '^POST /pgc-bucket/pz/part-0000.parquet uploads=')" -ge 1 ] && echo yes || echo no)" "yes"

# cancel mid-run: the dispatcher removes the completed keys it wrote. Big table
# + small parts so a worker is mid-multipart when the cancel lands.
# The prefix ends in a slash (#1782), so the cancel-time DELETEs and the
# multipart cleanup listing run on the stripped prefix as well. The premise
# waits for "cx/+part-", so a build that keeps the "//" is still cancelled and
# fails the DELETE and empty-segment checks rather than the premise.
psql_run "DROP TABLE IF EXISTS es_parbig;
          CREATE TABLE es_parbig (id int8, pad text) USING pgcolumnar;
          SELECT pgcolumnar.set_options('es_parbig', stripe_row_limit => 4000);
          INSERT INTO es_parbig SELECT g, md5(g::text)||md5((g+1)::text)
                                FROM generate_series(1,3000000) g;"
: > "$S3_LOG"
rm -rf "$PGC_WORKDIR/$BUCKET/cx"
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq \
	-c "SET pgcolumnar.objstore_part_size = 262144" \
	-c "SELECT pgcolumnar.parallel_export_parquet('es_parbig'::regclass, 's3://$BUCKET/cx/', 4)" \
	>"$PGC_WORKDIR/cx_bg.out" 2>&1 &
bgpid=$!
wrote=no
for _ in $(seq 1 300); do
	[ "$(logn '^PUT /pgc-bucket/cx/+part-')" -ge 1 ] && { wrote=yes; break; }
	sleep 0.1
done
check "premise: a worker was uploading before the cancel" "$wrote" "yes"
# Cancel only once all four workers' uploads are registered on the server. A cancel
# landing while a worker's POST ?uploads is still in flight leaves an upload whose id
# the worker never received, so nothing can abort it: that window is #1743's, not what
# the open-upload check below pins (it flaked on it in CI, #1723's head). Not a
# premise: a writer that never opens a multipart upload reddens it.
cx_open=0
for _ in $(seq 1 300); do
	cx_open="$(open_uploads '^pgc-bucket/cx/')"
	[ "$cx_open" -ge 4 ] && break
	sleep 0.1
done
check "all four workers' uploads were open before the cancel" \
	"$([ "$cx_open" -ge 4 ] && echo yes || echo "no ($cx_open open)")" "yes"
q "SELECT pg_cancel_backend(pid) FROM pg_stat_activity
   WHERE query LIKE '%parallel_export_parquet%' AND state = 'active'
     AND pid <> pg_backend_pid()" >/dev/null
wait "$bgpid" 2>/dev/null || true
# let requests still in flight from the cancelled workers land (the log stops growing
# for a second, at most 10 s), so a late write to a final key is read, not raced
for _ in $(seq 1 10); do
	_sz="$(wc -c < "$S3_LOG")"; sleep 1; [ "$(wc -c < "$S3_LOG")" = "$_sz" ] && break
done
check "premise: the export was cancelled, not completed" \
	"$(grep -qiE 'canceling statement|canceled on user request' "$PGC_WORKDIR/cx_bg.out" && echo cancelled || echo completed)" "cancelled"
check "cancel: the dispatcher issued DELETE over the known keys" \
	"$([ "$(logn '^DELETE /pgc-bucket/cx/part-')" -ge 1 ] && echo yes)" "yes"
check_num "cancel: no request named a key with an empty segment (//)" \
	"$(logn '^[A-Z]+ /pgc-bucket/[^ ]*//')" "0"
check_num "cancel: no completed object remains at the prefix" \
	"$(ls "$PGC_WORKDIR/$BUCKET/cx/" 2>/dev/null | grep -c 'part-.*\.parquet$')" "0"
# #394: a cancelled remote run writes no completion marker (it is written last,
# only on full success), and the dispatcher's cleanup also drops any stale one.
check "cancel: no _SUCCESS marker at the prefix" \
	"$([ -e "$PGC_WORKDIR/$BUCKET/cx/_SUCCESS" ] && echo present || echo absent)" "absent"
# #1722: a worker the cancel terminates aborts its multipart upload on the way out,
# so no billable orphan is left under the prefix
check "cancel: the server holds no open upload under the prefix" \
	"$(open_uploads '^pgc-bucket/cx/')" "0"

# #1864: a cancel must not leave a part a worker was committing. The fixture
# holds every single-request PUT for 3 s after its body arrives, and only then
# writes the object (__put_hold__), as a store that applies a commit late. A
# terminated worker waits for that answer (#1813). The dispatcher used to delete
# the part keys at once, before the held PUTs were applied, so the parts appeared
# afterwards and stayed. It now waits for the workers to exit first.
: > "$S3_LOG"
rm -rf "$PGC_WORKDIR/$BUCKET/ch"
echo 3 > "$PGC_WORKDIR/__put_hold__"
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq \
	-c "SELECT pgcolumnar.parallel_export_parquet('es_par'::regclass, 's3://$BUCKET/ch', 4)" \
	>"$PGC_WORKDIR/ch_bg.out" 2>&1 &
chpid=$!
ch_puts=0
for _ in $(seq 1 300); do
	ch_puts="$(logn '^PUT /pgc-bucket/ch/part-')"
	[ "$ch_puts" -ge 4 ] && break
	sleep 0.1
done
q "SELECT pg_cancel_backend(pid) FROM pg_stat_activity
   WHERE query LIKE '%parallel_export_parquet%' AND state = 'active'
     AND pid <> pg_backend_pid()" >/dev/null
wait "$chpid" 2>/dev/null || true
# a held PUT lands 3 s after it arrived; read the prefix once every hold is over
sleep 4
rm -f "$PGC_WORKDIR/__put_hold__"
check "premise: commit hold: all four part PUTs reached the store before the cancel" \
	"$([ "$ch_puts" -ge 4 ] && echo yes || echo "no ($ch_puts part PUTs)")" "yes"
check "premise: commit hold: the export was cancelled, not completed" \
	"$(grep -qiE 'canceling statement|canceled on user request' "$PGC_WORKDIR/ch_bg.out" && echo cancelled || echo "completed ($(head -1 "$PGC_WORKDIR/ch_bg.out"))")" "cancelled"
check_num "commit hold: a cancel leaves no part that a worker was committing" \
	"$(ls "$PGC_WORKDIR/$BUCKET/ch/" 2>/dev/null | grep -c 'part-.*\.parquet$')" "0"

# #1864 review: the cancel waited for the workers twice, up to 5 s each: once
# before it deleted the parts, and again before it aborted the open uploads. A
# store that holds every commit for 12 s keeps each terminated worker alive past
# both waits, so the cancel took about 10 s. One wait is enough: the cancel must
# return within 8 s.
: > "$S3_LOG"
rm -rf "$PGC_WORKDIR/$BUCKET/cl"
echo 12 > "$PGC_WORKDIR/__put_hold__"
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq \
	-c "SELECT pgcolumnar.parallel_export_parquet('es_par'::regclass, 's3://$BUCKET/cl', 4)" \
	>"$PGC_WORKDIR/cl_bg.out" 2>&1 &
clpid=$!
cl_puts=0
for _ in $(seq 1 300); do
	cl_puts="$(logn '^PUT /pgc-bucket/cl/part-')"
	[ "$cl_puts" -ge 4 ] && break
	sleep 0.1
done
cl_t0=$(date +%s%N)
q "SELECT pg_cancel_backend(pid) FROM pg_stat_activity
   WHERE query LIKE '%parallel_export_parquet%' AND state = 'active'
     AND pid <> pg_backend_pid()" >/dev/null
wait "$clpid" 2>/dev/null || true
cl_ms=$(( ($(date +%s%N) - cl_t0) / 1000000 ))
# a held PUT lands 12 s after it arrived; let every hold end before the next arm
sleep 12
rm -f "$PGC_WORKDIR/__put_hold__"
check "premise: long hold: the export was cancelled, not completed" \
	"$(grep -qiE 'canceling statement|canceled on user request' "$PGC_WORKDIR/cl_bg.out" && echo cancelled || echo "completed ($(head -1 "$PGC_WORKDIR/cl_bg.out"))")" "cancelled"
check "long hold: a cancel waits for the workers once, not twice" \
	"$([ "$cl_ms" -le 8000 ] && echo "at most 8000 ms" || echo "$cl_ms ms")" "at most 8000 ms"

# #1727 review: a worker that SIGTERM ends aborts its upload in an exit callback, and a
# store that stops answering must not hold it there. Each wait slice times out, so the
# 60 s quiet limit ends the abort; without WL_TIMEOUT the wait never woke and the review
# saw all four workers alive at +92 s. Stop the fixture mid-upload, terminate the
# workers, and require them gone within 75 s. One check: no upload seen is a red too.
WORKERS="backend_type = 'pgcolumnar parallel_export worker'"
: > "$S3_LOG"
rm -rf "$PGC_WORKDIR/$BUCKET/hs"
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq \
	-c "SET pgcolumnar.objstore_part_size = 262144" \
	-c "SELECT pgcolumnar.parallel_export_parquet('es_parbig'::regclass, 's3://$BUCKET/hs', 4)" \
	>"$PGC_WORKDIR/hs_bg.out" 2>&1 &
hspid=$!
hwrote=no
for _ in $(seq 1 300); do
	[ "$(logn '^PUT /pgc-bucket/hs/part-')" -ge 1 ] && { hwrote=yes; break; }
	sleep 0.1
done
kill -STOP "$SRV_PID"
hs_n="$(q "SELECT count(*) FROM pg_stat_activity WHERE $WORKERS;")"
q "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE $WORKERS;" >/dev/null
hs_t0=$(date +%s)
hs_left="$hs_n"
for _ in $(seq 1 90); do
	hs_left="$(q "SELECT count(*) FROM pg_stat_activity WHERE $WORKERS;")"
	[ "$hs_left" = 0 ] && break
	sleep 1
done
hs_dt=$(( $(date +%s) - hs_t0 ))
kill -CONT "$SRV_PID"
q "SELECT pg_cancel_backend(pid) FROM pg_stat_activity
   WHERE query LIKE '%parallel_export_parquet%' AND state = 'active'
     AND pid <> pg_backend_pid()" >/dev/null
wait "$hspid" 2>/dev/null || true
echo "store silent: ${hs_n:-?} workers terminated, ${hs_left:-?} left after ${hs_dt}s"
check "terminate: export workers exit within 75 s while the store is silent" \
	"$(if [ "$hwrote" != yes ]; then echo "no upload seen"
	   elif [ "${hs_n:-0}" -ge 1 ] && [ "$hs_left" = 0 ] && [ "$hs_dt" -le 75 ]; then echo bounded
	   else echo "slow (${hs_n:-?} workers, ${hs_left:-?} left after ${hs_dt}s)"; fi)" "bounded"

# ---- #1779: an allow-listed http:// target is written unsigned --------------
# The docs accept an object-storage URL, http:// included, wherever a local path
# is accepted. os_write_request signed every write, and an http:// handle has no
# region or credentials (only os_resolve_s3, for s3:// and gs://, sets them), so
# the signer read a NULL region and the backend died with signal 11. Reads sign
# only when the handle is an S3 one; writes now do the same. A second fixture
# server WITHOUT the --sigv4-* options accepts the unsigned requests, and every
# write arm is covered: one PUT, the multipart protocol, and the abort.
PLAIN_DIR="$PGC_WORKDIR/plain"
PLAIN_LOG="$PGC_WORKDIR/plain.log"
mkdir -p "$PLAIN_DIR/pb"
PLAIN_PORT="$(pgc_pick_free_port "$PGC_AUX_PORT_LO" "$PGC_AUX_PORT_HI")"
python3 "$(dirname "${BASH_SOURCE[0]}")/objstore_http_server.py" \
	--dir "$PLAIN_DIR" --port "$PLAIN_PORT" --log "$PLAIN_LOG" \
	> "$PGC_WORKDIR/plain_server.out" 2>&1 &
PLAIN_PID=$!
for _ in $(seq 1 50); do
	grep -q READY "$PGC_WORKDIR/plain_server.out" 2>/dev/null && break
	sleep 0.1
done
check "premise: unsigned http fixture server is up" \
	"$(grep -c READY "$PGC_WORKDIR/plain_server.out" 2>/dev/null)" "1"
PLAIN="http://127.0.0.1:$PLAIN_PORT/pb"
plogn() { grep -cE "$1" "$PLAIN_LOG" | tr -d ' '; }

: > "$PLAIN_LOG"
check_num "http:// small export succeeds unsigned (no crash)" \
	"$(q "SELECT pgcolumnar.export_parquet('es_small', '$PLAIN/small.parquet')")" "1000"
check_num "http:// small object went as ONE plain PUT" \
	"$(plogn '^PUT /pb/small.parquet -')" "1"
psql_run "CREATE FOREIGN TABLE fh_small (id int, t text) SERVER wsrv OPTIONS (path '$PLAIN/small.parquet');"
check "http:// small round trip: read-back == source" \
	"$(pgc_set_hash "SELECT * FROM fh_small")" "$(pgc_set_hash "$GEN_SMALL")"
# export_arrow shares the sink, and #1779 names it too: the object goes to the
# http:// target and is imported back from the fixture server's directory.
psql_run "CREATE TABLE eh_arrow_back (id int, t text) USING pgcolumnar;"
q "SELECT pgcolumnar.export_arrow('es_small', '$PLAIN/small.arrow')" >/dev/null
wait_up
q "SELECT pgcolumnar.import_arrow('eh_arrow_back', '$PLAIN_DIR/pb/small.arrow')" >/dev/null
check "http:// arrow export round-trips unsigned (no crash)" \
	"$(pgc_set_hash "SELECT * FROM eh_arrow_back")" "$(pgc_set_hash "$GEN_SMALL")"

: > "$PLAIN_LOG"
wait_up   # a crash above leaves the cluster in recovery; read this arm on its own
check_num "http:// large export succeeds unsigned (no crash)" \
	"$(qset "SELECT pgcolumnar.export_parquet('es_big', '$PLAIN/big.parquet')")" "$BIGROWS"
pb_s="$(plogn '^POST /pb/big.parquet uploads=')"; pb_p="$(plogn '^PUT /pb/big.parquet partNumber=')"; pb_c="$(plogn '^POST /pb/big.parquet uploadId=')"
check "the http:// large export took the multipart protocol" \
	"$(if [ "${pb_s:-0}" -ge 1 ] && [ "${pb_p:-0}" -ge 2 ] && [ "${pb_c:-0}" -eq 1 ]; then echo yes
	   else echo "no (starts=${pb_s:-?} parts=${pb_p:-?} completes=${pb_c:-?})"; fi)" "yes"
psql_run "CREATE FOREIGN TABLE fh_big (id int8, pad text) SERVER wsrv OPTIONS (path '$PLAIN/big.parquet');"
check "http:// large round trip: read-back == source" \
	"$(pgc_set_hash "SELECT * FROM fh_big")" "$(pgc_set_hash "$GEN_BIG")"

# #1904: an http:// URL keeps its query string, so each multipart request joins
# its own parameters to it with '&'. A second '?' made the start request read as
# raw=true?uploads=, and the server refused it.
: > "$PLAIN_LOG"
wait_up
pq_rows="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -qAt \
	-c "$PART SELECT pgcolumnar.export_parquet('es_big', '$PLAIN/bigq.parquet?raw=true')" 2>&1 | tr '\n' ' ' | sed 's/ *$//')"
pq_all="$(awk '$2 == "/pb/bigq.parquet" { print $1, $3 }' "$PLAIN_LOG")"
pq_n="$(printf '%s' "$pq_all" | grep -c .)"
pq_bad="$(printf '%s' "$pq_all" | grep -vc ' raw=true&\(uploads=\|partNumber=\|uploadId=\)')"
[ "$pq_bad" -gt 0 ] && pq_bad="$pq_bad (first: $(printf '%s\n' "$pq_all" | grep -v ' raw=true&\(uploads=\|partNumber=\|uploadId=\)' | head -1))"
check "http:// multipart export to a URL with a query string joins each request's parameters with '&'" \
	"rows: ${pq_rows:-none}; requests not of the form raw=true&...: $pq_bad of $pq_n" \
	"rows: $BIGROWS; requests not of the form raw=true&...: 0 of $pq_n"

: > "$PLAIN_LOG"
check "http:// inject: mid-upload failure surfaces as disk-full (53100)" \
	"$(refusal_of "$PART SET pgcolumnar.sink_fail_after = 900000; SELECT pgcolumnar.export_parquet('es_big', '$PLAIN/fail.parquet')" \
		"export write failed (injected fault)" " by the injected fault")" "53100 by the injected fault"
check "the http:// fault landed mid-multipart (an upload was started)" \
	"$(pf_s="$(plogn '^POST /pb/fail.parquet uploads=')"; if [ "${pf_s:-0}" -ge 1 ]; then echo yes; else echo "no (starts=${pf_s:-?})"; fi)" "yes"
check_num "http:// inject: the server holds no open upload for the key" \
	"$(if [ -f "$PLAIN_DIR/__open_uploads__" ]; then grep -cE '^pb/fail\.parquet$' "$PLAIN_DIR/__open_uploads__" | tr -d ' '; else echo 0; fi)" "0"
# #1904 review: the abort of a failed multipart export to a URL with a query
# string joins uploadId to it with '&' too, so the server ends the upload.
: > "$PLAIN_LOG"
fq_err="$(refusal_of "$PART SET pgcolumnar.sink_fail_after = 900000; SELECT pgcolumnar.export_parquet('es_big', '$PLAIN/failq.parquet?raw=true')" \
	"export write failed (injected fault)" " by the injected fault")"
fq_del="$(awk '$1 == "DELETE" && $2 == "/pb/failq.parquet" { print $3 }' "$PLAIN_LOG")"
fq_deln="$(printf '%s' "$fq_del" | grep -c .)"
fq_bad="$(printf '%s' "$fq_del" | grep -vc '^raw=true&uploadId=')"
[ "$fq_bad" -gt 0 ] && fq_bad="$fq_bad (first: $(grep -v '^raw=true&uploadId=' <<<"$fq_del" | head -1))"
fq_open="$(if [ -f "$PLAIN_DIR/__open_uploads__" ]; then grep -cE '^pb/failq\.parquet$' "$PLAIN_DIR/__open_uploads__" | tr -d ' '; else echo 0; fi)"
fq_wdel="$fq_deln"; [ "$fq_deln" -ge 1 ] 2>/dev/null || fq_wdel="at least 1"
check "http:// inject to a URL with a query string: the abort joins uploadId with '&' and no upload stays open" \
	"$fq_err; DELETEs not of the form raw=true&uploadId=: $fq_bad of $fq_deln; open uploads: $fq_open" \
	"53100 by the injected fault; DELETEs not of the form raw=true&uploadId=: 0 of $fq_wdel; open uploads: 0"
# #1832: an http:// prefix cannot be listed either, so it is refused before
# any worker starts too.
: > "$PLAIN_LOG"
check "http:// prefix: a parallel export is refused before any worker starts, with no request sent" \
	"$(refusal_of "SELECT pgcolumnar.parallel_export_parquet('es_par'::regclass, '$PLAIN/px', 2)" \
		"parallel_export_parquet cannot write to" " by the scheme check"), $(plogn '/pb/px/') requests" \
	"0A000 by the scheme check, 0 requests"
kill "$PLAIN_PID" 2>/dev/null
PLAIN_PID=""

# A 403 from an http(s):// target cannot be about AWS_* credentials: those
# requests are never signed. The SigV4 fixture server refuses a request with no
# Authorization header, so an http:// export to it draws the 403 an unsigned
# target gives. The arm echoes the HINT itself, so a failure shows what it said.
hint_of() {
	local out
	wait_up
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -qtA -c "$1" 2>&1)"
	if grep -q '^HINT:  ' <<<"$out"; then sed -n 's/^HINT:  //p' <<<"$out" | head -1
	else echo "no hint ($(head -1 <<<"$out"))"; fi
}
check "http:// 403: the hint says the request went unsigned, not to check AWS_* credentials" \
	"$(hint_of "SELECT pgcolumnar.export_parquet('es_small', 'http://127.0.0.1:$S3_PORT/$BUCKET/denied.parquet')")" \
	"Requests to http:// and https:// URLs are never signed. Use an s3:// or gs:// URL for a store that needs credentials."
# The other side of that branch: an s3:// request IS signed, so its 403 keeps the
# AWS_* hint. The fixture verifies the denied bucket against a different secret,
# so this correctly signed export is refused as a wrong secret would be.
check "s3:// 403: a signed request that is refused still hints to check AWS_* credentials" \
	"$(hint_of "SELECT pgcolumnar.export_parquet('es_small', 's3://$DENIED_BUCKET/denied.parquet')")" \
	"Check the AWS_* credential environment of the server process."
# #1831: the refusal of a scheme the export cannot use said a remote prefix
# cannot be listed. The export lists an s3:// prefix (#619), so the hint now
# says that only an s3:// prefix can be listed.
check "unsupported scheme: the parallel export's hint says only an s3:// prefix can be listed" \
	"$(hint_of "SELECT pgcolumnar.parallel_export_parquet('es_par'::regclass, 'az://container/out', 2)")" \
	"The export lists its prefix to delete stale parts that an earlier, larger export left, and only an s3:// prefix can be listed. Use an s3:// prefix or a local directory."

# ---- #1857: AWS S3 and GCS refuse a part under 5 MiB, except the last -------
# The store finds a small part only at CompleteMultipartUpload, after every part
# has gone up. The export refuses the part size before it starts the upload when
# the endpoint is AWS S3 or GCS. The endpoints below are never reached: the
# allow-list (127.0.0.1 only) refuses any connection to them, so a request the
# export does send shows as the allow-list's 42501.
pg_restart_env "AWS_ENDPOINT_URL='http://s3.us-east-1.amazonaws.com'" \
	"AWS_ACCESS_KEY_ID='$AKID'" "AWS_SECRET_ACCESS_KEY='$SECRET'" \
	"AWS_REGION='us-east-1'"
check "part minimum: an export to AWS S3 with 256 KiB parts is refused before the upload starts (22023)" \
	"$(refusal_of "$PART SELECT pgcolumnar.export_parquet('es_big', 's3://$BUCKET/min.parquet')" \
		"below the 5 MiB minimum" " by the part minimum")" "22023 by the part minimum"
check "part minimum: a one-part export to AWS S3 is not refused by the part minimum" \
	"$(refusal_of "$PART SELECT pgcolumnar.export_parquet('es_small', 's3://$BUCKET/min1.parquet')" \
		"is not in pgcolumnar.objstore_allowed_endpoints" " by the allow-list")" "42501 by the allow-list"
check "part minimum: an export to AWS S3 with 5 MiB parts is not refused by the part minimum" \
	"$(refusal_of "SET pgcolumnar.objstore_part_size = 5242880; SELECT pgcolumnar.export_parquet('es_big', 's3://$BUCKET/min5.parquet')" \
		"is not in pgcolumnar.objstore_allowed_endpoints" " by the allow-list")" "42501 by the allow-list"
pg_restart_env "AWS_ENDPOINT_URL='http://s3.cn-north-1.amazonaws.com.cn'" \
	"AWS_ACCESS_KEY_ID='$AKID'" "AWS_SECRET_ACCESS_KEY='$SECRET'" \
	"AWS_REGION='cn-north-1'"
check "part minimum: an export to AWS S3 in China (amazonaws.com.cn) with 256 KiB parts is refused (22023)" \
	"$(refusal_of "$PART SELECT pgcolumnar.export_parquet('es_big', 's3://$BUCKET/min.parquet')" \
		"below the 5 MiB minimum" " by the part minimum")" "22023 by the part minimum"
# Cloudflare R2 documents the same 5 MiB minimum for every part but the last
# (#1830 review).
pg_restart_env "AWS_ENDPOINT_URL='http://0123456789abcdef.r2.cloudflarestorage.com'" \
	"AWS_ACCESS_KEY_ID='$AKID'" "AWS_SECRET_ACCESS_KEY='$SECRET'" \
	"AWS_REGION='auto'"
check "part minimum: an export to Cloudflare R2 (r2.cloudflarestorage.com) with 256 KiB parts is refused (22023)" \
	"$(refusal_of "$PART SELECT pgcolumnar.export_parquet('es_big', 's3://$BUCKET/min.parquet')" \
		"below the 5 MiB minimum" " by the part minimum")" "22023 by the part minimum"
# A host that ends in the letters "amazonaws.com" without the dot is another
# domain, so the part minimum does not apply to it.
pg_restart_env "AWS_ENDPOINT_URL='http://notamazonaws.com'" \
	"AWS_ACCESS_KEY_ID='$AKID'" "AWS_SECRET_ACCESS_KEY='$SECRET'" \
	"AWS_REGION='us-east-1'"
check "part minimum: an export to notamazonaws.com with 256 KiB parts is not refused by the part minimum" \
	"$(refusal_of "$PART SELECT pgcolumnar.export_parquet('es_big', 's3://$BUCKET/min.parquet')" \
		"is not in pgcolumnar.objstore_allowed_endpoints" " by the allow-list")" "42501 by the allow-list"
pg_restart_env "AWS_ACCESS_KEY_ID='$AKID'" "AWS_SECRET_ACCESS_KEY='$SECRET'"
check "part minimum: a gs:// export at the default GCS endpoint with 256 KiB parts is refused (22023)" \
	"$(refusal_of "$PART SELECT pgcolumnar.export_parquet('es_big', 'gs://$BUCKET/min.parquet')" \
		"below the 5 MiB minimum" " by the part minimum")" "22023 by the part minimum"

# ---- optional: a real S3 implementation (Garage) ----------------------------
if [ -n "${PGC_S3_INTEGRATION_ENDPOINT:-}" ]; then
	# A failed restart is a named red, and the arms below still run and fail by
	# name, so the reds of the arms above reach the summary (#1850).
	if pg_restart_env "AWS_ENDPOINT_URL='$PGC_S3_INTEGRATION_ENDPOINT'" \
		"AWS_ACCESS_KEY_ID='$PGC_S3_INTEGRATION_KEY'" \
		"AWS_SECRET_ACCESS_KEY='$PGC_S3_INTEGRATION_SECRET'" \
		"AWS_REGION='${PGC_S3_INTEGRATION_REGION:-garage}'"
	then int_up=yes; else int_up="no (the cluster did not answer SELECT 1 within 15 s of the restart)"; fi
	check "integration: the cluster restarts with the real S3 credentials" "$int_up" "yes"
	pgc_pg "echo \"pgcolumnar.objstore_allowed_endpoints='$(echo "${PGC_S3_INTEGRATION_ENDPOINT#http://}" | cut -d/ -f1 | cut -d: -f1)'\" >> '$PGC_PGDATA/postgresql.conf'"
	q "SELECT pg_reload_conf()" >/dev/null
	IB="${PGC_S3_INTEGRATION_BUCKET:-pgcolumnar-test}"
	check_num "integration: multipart export to real S3 succeeds" \
		"$(q "SELECT pgcolumnar.export_parquet('es_big', 's3://$IB/m2sink-$$.parquet')")" "$BIGROWS"
	psql_run "CREATE FOREIGN TABLE f_real (id int8, pad text) SERVER wsrv OPTIONS (path 's3://$IB/m2sink-$$.parquet');"
	check "integration: real S3 read-back == source" \
		"$(pgc_set_hash "SELECT * FROM f_real")" "$(pgc_set_hash "$GEN_BIG")"
else
	echo "note: PGC_S3_INTEGRATION_ENDPOINT unset; the stdlib-verifier arms carry the proof alone"
fi

pgc_summary
