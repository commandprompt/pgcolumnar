#!/usr/bin/env bash
#
# pgColumnar #1741: a second cancel of a parallel_export_parquet to s3://, landing
# while the first cancel's cleanup DELETEs are still in flight, must leave the
# export reporting its own cancel.
#
# The dispatcher's error cleanup deletes the keys it wrote through the object
# store module. That runs while the first cancel's error is still being handled,
# so the error is on the error stack. A second cancel raises inside the DELETE's
# wait, and the module's best-effort catch used to call FlushErrorState(), which
# empties the WHOLE error stack, the first cancel's error included. The re-throw
# that followed had nothing to report, and the client got "errstart was not
# called" in place of 57014.
#
# The fixture server's __delete_delay__ hook answers every DELETE after a delay,
# so the second cancel is sent while a cleanup DELETE is provably outstanding.
#
# Usage:  test/objstore_export_double_cancel.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

export PGC_EXTRA_CONF=$'pgcolumnar.objstore_allowed_endpoints=\'127.0.0.1\'\nmax_worker_processes=16'

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

S3_PORT="$(pgc_pick_free_port "$PGC_AUX_PORT_LO" "$PGC_AUX_PORT_HI")"
S3_LOG="$PGC_WORKDIR/dc.log"
SRV_PID=""
AKID="PGCTESTKEYID"
SECRET="pgctest-secret-shhh"
REGION="pgc-test-1"
BUCKET="pgc-bucket"

double_cancel_teardown() {
	[ -n "$SRV_PID" ] && kill "$SRV_PID" 2>/dev/null
	pgc_teardown
}
trap double_cancel_teardown EXIT INT TERM

pg_restart_env() {
	pgc_pg "pg_ctl -D '$PGC_PGDATA' stop -m fast -w" >/dev/null 2>&1
	pgc_pg "$* pg_ctl -D '$PGC_PGDATA' -l '$PGC_LOGFILE' start -w" >/dev/null 2>&1
	for _ in $(seq 1 30); do [ -n "$(q 'SELECT 1')" ] && return 0; sleep 0.5; done
	echo "FATAL: cluster did not come back"; exit 1
}
logn() { grep -cE "$1" "$S3_LOG" | tr -d ' '; }
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
cancel_export() {
	q "SELECT pg_cancel_backend(pid) FROM pg_stat_activity
	   WHERE query LIKE '%parallel_export_parquet%' AND backend_type = 'client backend'
	     AND pid <> pg_backend_pid()" >/dev/null
}

mkdir -p "$PGC_WORKDIR/$BUCKET"
python3 "$(dirname "${BASH_SOURCE[0]}")/objstore_http_server.py" \
	--dir "$PGC_WORKDIR" --port "$S3_PORT" --log "$S3_LOG" \
	--sigv4-key "$AKID" --sigv4-secret "$SECRET" --sigv4-region "$REGION" \
	> "$PGC_WORKDIR/dc_server.out" 2>&1 &
SRV_PID=$!
for _ in $(seq 1 50); do
	grep -q READY "$PGC_WORKDIR/dc_server.out" 2>/dev/null && break
	sleep 0.1
done
check "premise: sigv4 fixture server is up" \
	"$(grep -c READY "$PGC_WORKDIR/dc_server.out" 2>/dev/null)" "1"

pg_restart_env "AWS_ENDPOINT_URL='http://127.0.0.1:$S3_PORT'" \
	"AWS_ACCESS_KEY_ID='$AKID'" "AWS_SECRET_ACCESS_KEY='$SECRET'" \
	"AWS_REGION='$REGION'"

# Big table + small parts so the workers are mid-upload when the first cancel lands.
psql_run "CREATE TABLE dc_big (id int8, pad text) USING pgcolumnar;
          SELECT pgcolumnar.set_options('dc_big', stripe_row_limit => 4000);
          INSERT INTO dc_big SELECT g, md5(g::text)||md5((g+1)::text)
                             FROM generate_series(1,3000000) g;"

: > "$S3_LOG"
echo 2 > "$PGC_WORKDIR/__delete_delay__"
chmod 644 "$PGC_WORKDIR/__delete_delay__"
crashes0="$(logcount 'terminated by signal')"
errstart0="$(logcount 'errstart was not called')"
env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -Atq \
	-v VERBOSITY=verbose \
	-c "SET pgcolumnar.objstore_part_size = 262144" \
	-c "SELECT pgcolumnar.parallel_export_parquet('dc_big'::regclass, 's3://$BUCKET/dc', 4)" \
	>"$PGC_WORKDIR/dc_bg.out" 2>&1 &
bgpid=$!
wrote=no
for _ in $(seq 1 300); do
	[ "$(logn '^PUT /pgc-bucket/dc/part-')" -ge 1 ] && { wrote=yes; break; }
	sleep 0.1
done
check "premise: a worker was uploading before the first cancel" "$wrote" "yes"
cancel_export
# The cleanup's first DELETE is logged on arrival and answered 2 s later.
deleting=no
for _ in $(seq 1 100); do
	[ "$(logn '^DELETE /pgc-bucket/dc/')" -ge 1 ] && { deleting=yes; break; }
	sleep 0.1
done
check "premise: the first cancel's cleanup was deleting when the second cancel landed" \
	"$deleting" "yes"
cancel_export
wait "$bgpid" 2>/dev/null || true
rm -f "$PGC_WORKDIR/__delete_delay__"

check "double cancel: the export reports its own cancel (57014)" \
	"$(grep -m1 '^ERROR:' "$PGC_WORKDIR/dc_bg.out")" \
	"ERROR:  57014: canceling statement due to user request"
errstart1="$(logcount 'errstart was not called')"
check "double cancel: the server log has no 'errstart was not called'" \
	"$( [[ "$errstart0" =~ ^[0-9]+$ && "$errstart1" =~ ^[0-9]+$ ]] && echo $(( errstart1 - errstart0 )) || echo "unreadable")" "0"
check_num "double cancel: no completed object remains at the prefix" \
	"$(ls "$PGC_WORKDIR/$BUCKET/dc/" 2>/dev/null | grep -c 'part-.*\.parquet$')" "0"
check "double cancel: the server survived" "$(survived "$crashes0")" "1"

pgc_summary
