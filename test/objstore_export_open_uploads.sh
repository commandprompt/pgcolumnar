#!/usr/bin/env bash
#
# pgColumnar #1743: a parallel_export_parquet to s3:// that is cancelled, or
# whose workers are terminated, must leave none of its multipart uploads open,
# even an upload whose CreateMultipartUpload (POST ?uploads) had not answered.
#
# A worker learns its upload id only from the answer to POST ?uploads. The store
# creates the upload when the request arrives, so a worker that ends while it
# waits for the answer leaves an upload open that nothing on the worker's side
# can abort. The dispatcher's cleanup therefore lists the uploads under the
# export's prefix (ListMultipartUploads) and aborts each one whose key is one of
# this export's own part names. An upload under the same prefix with any other
# key belongs to someone else and must stay open.
#
# The parts arm is the plain case (#1722): every worker holds its upload id and
# is sending parts (the fixture's __part_delay__ holds each part's answer) when
# the export is cancelled; the workers end FATAL and never abort.
#
# In the other arms the fixture server's __uploads_delay__ hook holds every
# POST ?uploads after the upload exists, so the arm ends the export inside that
# window. Its
# __open_uploads__ file lists the uploads still open. Two foreign uploads are
# seeded under each prefix: part-0004.parquet, one past a 4-worker export's last
# part, and notes.parquet.
#
# The double-cancel arm cancels in that window and cancels again while the
# first cancel's cleanup DELETEs a part key (__delete_delay__ holds each DELETE).
# The second cancel lands inside the module's best-effort DELETE, whose catch
# empties the error stack (#1741). The upload cleanup must still run after it,
# and the export must still report the first cancel.
#
# The silent-listing arm holds the listing itself (__list_uploads_delay__), so
# the cleanup's bounded wait gives up and the export still reports its own
# cancel, promptly.
#
# Usage:  test/objstore_export_open_uploads.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# The part size goes in the configuration file, not a SET: the workers are
# background workers and do not inherit the dispatcher's session settings.
export PGC_EXTRA_CONF=$'pgcolumnar.objstore_allowed_endpoints=\'127.0.0.1\'\nmax_worker_processes=16\npgcolumnar.objstore_part_size=262144'

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

S3_PORT="$(pgc_pick_free_port "$PGC_AUX_PORT_LO" "$PGC_AUX_PORT_HI")"
S3_LOG="$PGC_WORKDIR/ou.log"
OPEN="$PGC_WORKDIR/__open_uploads__"
SRV_PID=""
AKID="PGCTESTKEYID"
SECRET="pgctest-secret-shhh"
REGION="pgc-test-1"
BUCKET="pgc-bucket"

open_uploads_teardown() {
	[ -n "$SRV_PID" ] && kill "$SRV_PID" 2>/dev/null
	pgc_teardown
}
trap open_uploads_teardown EXIT INT TERM

pg_restart_env() {
	pgc_pg "pg_ctl -D '$PGC_PGDATA' stop -m fast -w" >/dev/null 2>&1
	pgc_pg "$* pg_ctl -D '$PGC_PGDATA' -l '$PGC_LOGFILE' start -w" >/dev/null 2>&1
	for _ in $(seq 1 30); do [ -n "$(q 'SELECT 1')" ] && return 0; sleep 0.5; done
	echo "FATAL: cluster did not come back"; exit 1
}
# Lines of the server log matching $1: a number, or nothing when it can't be read.
logcount() {
	local _n
	_n="$(pgc_pg "grep -c '$1' '$PGC_LOGFILE'" 2>/dev/null)"
	case "$_n" in '' | *[!0-9]*) ;; *) echo "$_n" ;; esac
}
survived() {	# survived BEFORE -> "1" when the server answers and no backend died since
	local _up _now
	_up="$(q 'SELECT 1')"; _now="$(logcount 'terminated by signal')"
	if ! [[ "$1" =~ ^[0-9]+$ && "$_now" =~ ^[0-9]+$ ]]; then echo "no (crash count unreadable)"
	elif [ "$_up" = 1 ] && [ "$_now" = "$1" ]; then echo 1
	else echo "no ([$_up], $(( _now - $1 )) backend crashes)"; fi
}
# The export's own open uploads under prefix $1 (part-0000..0003): a number, or
# nothing when the server's record is missing.
own_open() {
	[ -f "$OPEN" ] || return 0
	grep -cE "^$BUCKET/$1/part-000[0-3]\.parquet\$" "$OPEN" | tr -d ' '
}
# Every other open upload under prefix $1, sorted, space-separated.
foreign_open() {
	[ -f "$OPEN" ] || { echo "no record"; return 0; }
	grep -E "^$BUCKET/$1/" "$OPEN" | grep -vE "^$BUCKET/$1/part-000[0-3]\.parquet\$" |
		sort | tr '\n' ' ' | sed 's/ $//'
}
part_puts() { grep -cE "^PUT /$BUCKET/$1/part-[0-9]+\.parquet .*partNumber=" "$S3_LOG" | tr -d ' '; }
# Wait until all 4 of the export's uploads under $1 are open; then report what
# the store holds: "4 open, 0 parts" means every worker waits for its upload id.
wait_unanswered() {
	for _ in $(seq 1 300); do
		[ "$(own_open "$1")" = 4 ] && break
		sleep 0.1
	done
	echo "$(own_open "$1") open, $(part_puts "$1") parts"
}
# Wait until all 4 of the export's uploads under $1 are open and 4 parts were
# sent; "4 open, sending parts" means every worker holds its upload id.
wait_sending() {
	for _ in $(seq 1 300); do
		[ "$(own_open "$1")" = 4 ] && [ "$(part_puts "$1")" -ge 4 ] && break
		sleep 0.1
	done
	if [ "$(part_puts "$1")" -ge 4 ]; then
		echo "$(own_open "$1") open, sending parts"
	else
		echo "$(own_open "$1") open, $(part_puts "$1") parts"
	fi
}
cancel_export() {
	q "SELECT pg_cancel_backend(pid) FROM pg_stat_activity
	   WHERE query LIKE '%parallel_export_parquet%' AND backend_type = 'client backend'
	     AND pid <> pg_backend_pid()" >/dev/null
}
terminate_workers() {
	q "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity
	   WHERE backend_type = 'pgcolumnar parallel_export worker'" >/dev/null
}
# start_export PREFIX OUTFILE: a 4-worker export to s3://bucket/PREFIX in the
# background; its pid is left in $bgpid.
start_export() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq \
		-v VERBOSITY=verbose \
		-c "SELECT pgcolumnar.parallel_export_parquet('ou_big'::regclass, 's3://$BUCKET/$1', 4)" \
		>"$2" 2>&1 &
	bgpid=$!
}
completed_parts() { ls "$PGC_WORKDIR/$BUCKET/$1/" 2>/dev/null | grep -c 'part-.*\.parquet$'; }

mkdir -p "$PGC_WORKDIR/$BUCKET"
python3 "$(dirname "${BASH_SOURCE[0]}")/objstore_http_server.py" \
	--dir "$PGC_WORKDIR" --port "$S3_PORT" --log "$S3_LOG" \
	--sigv4-key "$AKID" --sigv4-secret "$SECRET" --sigv4-region "$REGION" \
	--list-page-size 2 \
	--seed-upload "$BUCKET/op/part-0004.parquet" --seed-upload "$BUCKET/op/notes.parquet" \
	--seed-upload "$BUCKET/oc/part-0004.parquet" --seed-upload "$BUCKET/oc/notes.parquet" \
	--seed-upload "$BUCKET/ot/part-0004.parquet" --seed-upload "$BUCKET/ot/notes.parquet" \
	--seed-upload "$BUCKET/od/part-0004.parquet" --seed-upload "$BUCKET/od/notes.parquet" \
	> "$PGC_WORKDIR/ou_server.out" 2>&1 &
SRV_PID=$!
for _ in $(seq 1 50); do
	grep -q READY "$PGC_WORKDIR/ou_server.out" 2>/dev/null && break
	sleep 0.1
done
check "premise: sigv4 fixture server is up" \
	"$(grep -c READY "$PGC_WORKDIR/ou_server.out" 2>/dev/null)" "1"
check "premise: the foreign uploads were open before the exports" \
	"$(foreign_open op) $(foreign_open oc) $(foreign_open ot)" \
	"$BUCKET/op/notes.parquet $BUCKET/op/part-0004.parquet $BUCKET/oc/notes.parquet $BUCKET/oc/part-0004.parquet $BUCKET/ot/notes.parquet $BUCKET/ot/part-0004.parquet"

pg_restart_env "AWS_ENDPOINT_URL='http://127.0.0.1:$S3_PORT'" \
	"AWS_ACCESS_KEY_ID='$AKID'" "AWS_SECRET_ACCESS_KEY='$SECRET'" \
	"AWS_REGION='$REGION'"

# Enough rows that each of 4 workers writes more than one 256 KiB part, so each
# one starts a multipart upload.
psql_run "CREATE TABLE ou_big (id int8, pad text) USING pgcolumnar;
          SELECT pgcolumnar.set_options('ou_big', stripe_row_limit => 4000);
          INSERT INTO ou_big SELECT g, md5(g::text)||md5((g+1)::text)
                             FROM generate_series(1,400000) g;" >/dev/null
crashes0="$(logcount 'terminated by signal')"

# ---- parts: the dispatcher is cancelled while every worker sends parts ----
: > "$S3_LOG"
echo 20 > "$PGC_WORKDIR/__part_delay__"
start_export op "$PGC_WORKDIR/op.out"
check "premise: parts: every worker was sending parts when the cancel landed" \
	"$(wait_sending op)" "4 open, sending parts"
cancel_export
wait "$bgpid" 2>/dev/null || true
rm -f "$PGC_WORKDIR/__part_delay__"
check "parts: the export reports its own cancel (57014)" \
	"$(grep -m1 '^ERROR:' "$PGC_WORKDIR/op.out")" \
	"ERROR:  57014: canceling statement due to user request"
check_num "parts: no upload of this export is left open" "$(own_open op)" "0"
check_text "parts: the foreign uploads under the prefix are left open" \
	"$(foreign_open op)" "$BUCKET/op/notes.parquet $BUCKET/op/part-0004.parquet"
check "parts: the server survived" "$(survived "$crashes0")" "1"

# ---- cancel: the dispatcher is cancelled while every POST ?uploads is held ----
: > "$S3_LOG"
echo 20 > "$PGC_WORKDIR/__uploads_delay__"
start_export oc "$PGC_WORKDIR/oc.out"
check "premise: cancel: every worker's CreateMultipartUpload was unanswered when the cancel landed" \
	"$(wait_unanswered oc)" "4 open, 0 parts"
cancel_export
wait "$bgpid" 2>/dev/null || true
rm -f "$PGC_WORKDIR/__uploads_delay__"
check "cancel: the export reports its own cancel (57014)" \
	"$(grep -m1 '^ERROR:' "$PGC_WORKDIR/oc.out")" \
	"ERROR:  57014: canceling statement due to user request"
check_num "cancel: no upload of this export is left open" "$(own_open oc)" "0"
check_text "cancel: the foreign uploads under the prefix are left open" \
	"$(foreign_open oc)" "$BUCKET/oc/notes.parquet $BUCKET/oc/part-0004.parquet"
check_num "cancel: no completed object remains at the prefix" "$(completed_parts oc)" "0"
check "cancel: the server survived" "$(survived "$crashes0")" "1"

# ---- terminate: the workers are terminated while every POST ?uploads is held ----
: > "$S3_LOG"
echo 20 > "$PGC_WORKDIR/__uploads_delay__"
start_export ot "$PGC_WORKDIR/ot.out"
check "premise: terminate: every worker's CreateMultipartUpload was unanswered when the workers were terminated" \
	"$(wait_unanswered ot)" "4 open, 0 parts"
terminate_workers
wait "$bgpid" 2>/dev/null || true
rm -f "$PGC_WORKDIR/__uploads_delay__"
check "terminate: the export reports that a worker failed" \
	"$(grep -m1 '^ERROR:' "$PGC_WORKDIR/ot.out" | sed -E 's/worker [0-9]+ failed/worker N failed/')" \
	"ERROR:  XX000: pgcolumnar.parallel_export_parquet worker N failed: worker exited without reporting a result"
check_num "terminate: no upload of this export is left open" "$(own_open ot)" "0"
check_text "terminate: the foreign uploads under the prefix are left open" \
	"$(foreign_open ot)" "$BUCKET/ot/notes.parquet $BUCKET/ot/part-0004.parquet"
check "terminate: the server survived" "$(survived "$crashes0")" "1"

# ---- double cancel: a second cancel lands in the first cancel's part DELETE ----
: > "$S3_LOG"
echo 20 > "$PGC_WORKDIR/__uploads_delay__"
echo 3 > "$PGC_WORKDIR/__delete_delay__"
errstart0="$(logcount 'errstart was not called')"
start_export od "$PGC_WORKDIR/od.out"
check "premise: double cancel: every worker's CreateMultipartUpload was unanswered when the first cancel landed" \
	"$(wait_unanswered od)" "4 open, 0 parts"
cancel_export
# The cleanup's first part DELETE (no query: not an upload abort) is logged
# on arrival and answered 3 s later.
deleting=no
for _ in $(seq 1 100); do
	grep -qE "^DELETE /$BUCKET/od/part-[0-9]+\.parquet - " "$S3_LOG" && { deleting=yes; break; }
	sleep 0.1
done
check "premise: double cancel: the first cancel's cleanup was deleting a part when the second cancel landed" \
	"$deleting" "yes"
cancel_export
# Later DELETEs, the upload aborts among them, are answered at once.
rm -f "$PGC_WORKDIR/__delete_delay__"
wait "$bgpid" 2>/dev/null || true
rm -f "$PGC_WORKDIR/__uploads_delay__"
check "double cancel: the export reports its own cancel (57014)" \
	"$(grep -m1 '^ERROR:' "$PGC_WORKDIR/od.out")" \
	"ERROR:  57014: canceling statement due to user request"
errstart1="$(logcount 'errstart was not called')"
check "double cancel: the server log has no 'errstart was not called'" \
	"$( [[ "$errstart0" =~ ^[0-9]+$ && "$errstart1" =~ ^[0-9]+$ ]] && echo $(( errstart1 - errstart0 )) || echo "unreadable")" "0"
check_num "double cancel: no upload of this export is left open" "$(own_open od)" "0"
check_text "double cancel: the foreign uploads under the prefix are left open" \
	"$(foreign_open od)" "$BUCKET/od/notes.parquet $BUCKET/od/part-0004.parquet"
check "double cancel: the server survived" "$(survived "$crashes0")" "1"

# ---- silent listing: the store does not answer ListMultipartUploads ----
: > "$S3_LOG"
echo 20 > "$PGC_WORKDIR/__uploads_delay__"
echo 120 > "$PGC_WORKDIR/__list_uploads_delay__"
start_export os "$PGC_WORKDIR/os.out"
check "premise: silent listing: every worker's CreateMultipartUpload was unanswered when the cancel landed" \
	"$(wait_unanswered os)" "4 open, 0 parts"
t0="$(date +%s)"
cancel_export
wait "$bgpid" 2>/dev/null || true
t1="$(date +%s)"
rm -f "$PGC_WORKDIR/__uploads_delay__" "$PGC_WORKDIR/__list_uploads_delay__"
check "silent listing: the cleanup asked the store for its open uploads" \
	"$(grep -cE "^GET /$BUCKET .*uploads=" "$S3_LOG" | tr -d ' ')" "1"
check "silent listing: the export reports its own cancel (57014)" \
	"$(grep -m1 '^ERROR:' "$PGC_WORKDIR/os.out")" \
	"ERROR:  57014: canceling statement due to user request"
check "silent listing: the export returned within 30 s of the cancel" \
	"$( (( t1 - t0 <= 30 )) && echo yes || echo "no, after $(( t1 - t0 )) s")" "yes"
check "silent listing: the server survived" "$(survived "$crashes0")" "1"

pgc_summary
