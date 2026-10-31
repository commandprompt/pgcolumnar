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
AKID="PGCTESTKEYID"
SECRET="pgctest-secret-shhh"
REGION="pgc-test-1"
BUCKET="pgc-bucket"

objstore_sink_teardown() {
	[ -n "$SRV_PID" ] && kill "$SRV_PID" 2>/dev/null
	pgc_teardown
}
trap objstore_sink_teardown EXIT INT TERM

pg_restart_env() {
	pgc_pg "pg_ctl -D '$PGC_PGDATA' stop -m fast -w" >/dev/null 2>&1
	pgc_pg "$* pg_ctl -D '$PGC_PGDATA' -l '$PGC_LOGFILE' start -w" >/dev/null 2>&1
	for _ in $(seq 1 30); do [ -n "$(q 'SELECT 1')" ] && return 0; sleep 0.5; done
	echo "FATAL: cluster did not come back"; exit 1
}
# wait_up: wait up to 15 s for the server, so a backend that crashed in one reading
# fails that reading, not every reading after it during recovery.
wait_up() { for _ in $(seq 1 30); do [ -n "$(q 'SELECT 1')" ] && return 0; sleep 0.5; done; }

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
	"AWS_REGION='$REGION'"

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

# ---- optional: a real S3 implementation (Garage) ----------------------------
if [ -n "${PGC_S3_INTEGRATION_ENDPOINT:-}" ]; then
	pg_restart_env "AWS_ENDPOINT_URL='$PGC_S3_INTEGRATION_ENDPOINT'" \
		"AWS_ACCESS_KEY_ID='$PGC_S3_INTEGRATION_KEY'" \
		"AWS_SECRET_ACCESS_KEY='$PGC_S3_INTEGRATION_SECRET'" \
		"AWS_REGION='${PGC_S3_INTEGRATION_REGION:-garage}'"
	pgc_pg "echo \"pgcolumnar.objstore_allowed_endpoints='$(echo "${PGC_S3_INTEGRATION_ENDPOINT#http://}" | cut -d/ -f1 | cut -d: -f1)'\" >> '$PGC_PGDATA/postgresql.conf'"
	psql_run "SELECT pg_reload_conf();"
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
