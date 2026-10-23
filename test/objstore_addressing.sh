#!/usr/bin/env bash
#
# pgColumnar #621: the GCS gs:// scheme and S3 virtual-host addressing.
#
# gs:// is read and written through the interoperable XML API: the module reuses
# the S3 SigV4 signing (an HMAC key maps onto access_key_id/secret_access_key)
# and defaults the endpoint to storage.googleapis.com. Virtual-host addressing
# puts the bucket in the hostname (bucket.endpoint) instead of the first path
# segment, selected by the pgcolumnar.objstore_s3_addressing GUC.
#
# The fixture verifies every request's SigV4 against an independent
# implementation, so a gs:// or virtual-host read that returns data proves the C
# signer signed the exact bytes the endpoint expects. Virtual-host is exercised
# with /etc/hosts names (s3.local and pgc-bucket.s3.local both resolve to
# 127.0.0.1), so the client's connect, Host header, and cert name are the real
# bucket.endpoint with no test-only code path.
#
# Usage:  test/objstore_addressing.sh [PG_CONFIG]

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
export PGC_EXTRA_CONF="pgcolumnar.objstore_allowed_endpoints='s3.local'"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

python3 -c 'import pyarrow' 2>/dev/null || pgc_skip pyarrow "pyarrow is required to write the fixture"
# Virtual-host needs bucket.endpoint to resolve; skip cleanly where it does not.
getent hosts pgc-bucket.s3.local >/dev/null 2>&1 || \
	pgc_skip vhost "pgc-bucket.s3.local does not resolve; add it to /etc/hosts (-> 127.0.0.1)"

S3_PORT="$(pgc_pick_free_port "$PGC_AUX_PORT_LO" "$PGC_AUX_PORT_HI")"
S3_LOG="$PGC_WORKDIR/s3.log"
SRV_PID=""
AKID="PGCTESTKEYID"; SECRET="pgctest-secret"; REGION="pgc-test-1"; BUCKET="pgc-bucket"

addr_teardown() { [ -n "$SRV_PID" ] && kill "$SRV_PID" 2>/dev/null; pgc_teardown; }
trap addr_teardown EXIT INT TERM

pg_restart_env() {
	pgc_pg "pg_ctl -D '$PGC_PGDATA' stop -m fast -w" >/dev/null 2>&1
	pgc_pg "$* pg_ctl -D '$PGC_PGDATA' -l '$PGC_LOGFILE' start -w" >/dev/null 2>&1
	for _ in $(seq 1 30); do [ -n "$(q 'SELECT 1')" ] && return 0; sleep 0.5; done
	echo "FATAL: cluster did not restart"; exit 1
}
msg_of() {  # msg_of <sql> -> the ERROR message text
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -qtA -c "$1" 2>&1 | sed -n 's/^ERROR:  //p' | head -1
}
qset() {  # qset <sql...> -- run several -c statements, return the last line
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -Atq "$@" 2>/dev/null | tail -1
}
gets() { grep -oE "GET /[^ ]*" "$S3_LOG" | awk '{print $2}'; }

mkdir -p "$PGC_WORKDIR/$BUCKET"
python3 - "$PGC_WORKDIR/$BUCKET/vh.parquet" <<'PY'
import sys, pyarrow as pa, pyarrow.parquet as pq
pq.write_table(pa.table({"id": pa.array([1, 2, 3, 4], pa.int64())}), sys.argv[1])
PY
# A key a reader must URI-encode: a space, '+' and '='. The fixture decodes the request
# path once to find the file, and verifies SigV4 over the raw path, so a key sent with
# '+' for a space, or with '+'/'=' left bare, either misses the file or shows up in the
# request log below (#621).
ENCKEY="dir sp/a+b=c.parquet"
mkdir -p "$PGC_WORKDIR/$BUCKET/dir sp"
python3 - "$PGC_WORKDIR/$BUCKET/$ENCKEY" <<'PY'
import sys, pyarrow as pa, pyarrow.parquet as pq
pq.write_table(pa.table({"id": pa.array([5, 6, 7], pa.int64())}), sys.argv[1])
PY

python3 "$(dirname "${BASH_SOURCE[0]}")/objstore_http_server.py" \
	--dir "$PGC_WORKDIR" --port "$S3_PORT" --log "$S3_LOG" \
	--sigv4-key "$AKID" --sigv4-secret "$SECRET" --sigv4-region "$REGION" \
	--virtual-host-base s3.local > "$PGC_WORKDIR/s3_server.out" 2>&1 &
SRV_PID=$!
for _ in $(seq 1 50); do grep -q READY "$PGC_WORKDIR/s3_server.out" 2>/dev/null && break; sleep 0.1; done
check "premise: fixture up" "$(grep -c READY "$PGC_WORKDIR/s3_server.out" 2>/dev/null)" "1"

# ---- phase 1: an explicit endpoint (s3.local -> 127.0.0.1) ------------------
pg_restart_env "AWS_ENDPOINT_URL='http://s3.local:$S3_PORT'" \
	"AWS_ACCESS_KEY_ID='$AKID'" "AWS_SECRET_ACCESS_KEY='$SECRET'" "AWS_REGION='$REGION'"

RD="SELECT count(*), sum(id) FROM pgcolumnar.read_parquet"

# path-style s3:// (the default addressing) reads the object, bucket in the path
: > "$S3_LOG"
check "s3:// path-style read" "$(q "$RD('s3://$BUCKET/vh.parquet') AS t(id int8)" | tr '|' ' ')" "4 10"
check "path-style put the bucket in the request path" \
	"$([ "$(gets | grep -c '^/pgc-bucket/vh.parquet$')" -ge 1 ] && echo yes)" "yes"
: > "$S3_LOG"
check "a key with a space, '+' and '=' reads back (path-style)" \
	"$(q "$RD('s3://$BUCKET/$ENCKEY') AS t(id int8)" | tr '|' ' ')" "3 18"
check "and its path-style request path is the key URI-encoded once" \
	"$([ "$(gets | grep -c '^/pgc-bucket/dir%20sp/a%2Bb%3Dc.parquet$')" -ge 1 ] && echo yes)" "yes"

# gs:// round-trips through the interop API against the same overridden endpoint
: > "$S3_LOG"
check "gs:// read round-trips (scheme handled, HMAC creds reused)" \
	"$(q "$RD('gs://$BUCKET/vh.parquet') AS t(id int8)" | tr '|' ' ')" "4 10"
psql_run "CREATE TABLE gct (id int8) USING pgcolumnar; INSERT INTO gct VALUES (5),(6),(7);"
check "gs:// write round-trips" \
	"$(q "SELECT pgcolumnar.export_parquet('gct', 'gs://$BUCKET/out.parquet') > 0")" "t"
check "gs:// read-back of the written object" \
	"$(q "$RD('gs://$BUCKET/out.parquet') AS t(id int8)" | tr '|' ' ')" "3 18"

# ---- virtual-host addressing: the bucket moves into the hostname ------------
: > "$S3_LOG"
check "virtual-host read == path-style read (same object, different addressing)" \
	"$(qset -c "SET pgcolumnar.objstore_s3_addressing='virtual'" \
	        -c "$RD('s3://$BUCKET/vh.parquet') AS t(id int8)" | tr '|' ' ')" "4 10"
check "virtual-host put the KEY ALONE in the path (no bucket segment)" \
	"$([ "$(gets | grep -c '^/vh.parquet$')" -ge 1 ] && [ "$(gets | grep -c '^/pgc-bucket/vh.parquet$')" -eq 0 ] && echo yes)" "yes"
: > "$S3_LOG"
check "a key with a space, '+' and '=' reads back (virtual-host)" \
	"$(qset -c "SET pgcolumnar.objstore_s3_addressing='virtual'" \
	        -c "$RD('s3://$BUCKET/$ENCKEY') AS t(id int8)" | tr '|' ' ')" "3 18"
check "and its virtual-host request path is the key alone, URI-encoded once" \
	"$([ "$(gets | grep -c '^/dir%20sp/a%2Bb%3Dc.parquet$')" -ge 1 ] && echo yes)" "yes"
# removal proof of the addressing switch: path-style (default) keeps the bucket
: > "$S3_LOG"
check "removal proof: with addressing=path the bucket is back in the path" \
	"$(qset -c "SET pgcolumnar.objstore_s3_addressing='path'" \
	        -c "$RD('s3://$BUCKET/vh.parquet') AS t(id int8)" | tr '|' ' ')" "4 10"
check "and the request path carried the bucket again" \
	"$([ "$(gets | grep -c '^/pgc-bucket/vh.parquet$')" -ge 1 ] && echo yes)" "yes"

# ---- phase 2: NO endpoint -> gs:// has a default, s3:// does not -------------
pg_restart_env "AWS_ACCESS_KEY_ID='$AKID'" "AWS_SECRET_ACCESS_KEY='$SECRET'" "AWS_REGION='$REGION'"
check "s3:// with no endpoint demands one (names AWS_ENDPOINT_URL)" \
	"$([ "$(msg_of "$RD('s3://$BUCKET/vh.parquet') AS t(id int8)" | grep -c 'AWS_ENDPOINT_URL')" -ge 1 ] && echo yes)" "yes"
# The default is storage.googleapis.com, and this cluster's allow-list holds only
# s3.local, so the read must be refused by the allow-list naming that host, before any
# connection. The old check only wanted the message NOT to name AWS_ENDPOINT_URL, which
# a mistyped default, any other error, or a crash (no message at all) also satisfied.
check "gs:// with no endpoint goes to storage.googleapis.com (the allow-list refuses that host by name)" \
	"$(msg_of "$RD('gs://$BUCKET/vh.parquet') AS t(id int8)" | grep -c 'endpoint "storage.googleapis.com:443" is not in pgcolumnar.objstore_allowed_endpoints')" "1"

pgc_summary
