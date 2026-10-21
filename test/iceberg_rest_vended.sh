#!/usr/bin/env bash
#
# pgColumnar Iceberg REST catalog VENDED credentials (#656). A REST catalog
# returns, in its loadTable reply, short-lived scoped storage credentials; the
# reader must sign its S3 data/metadata/delete reads with THOSE, not with any
# ambient environment credential. The proof: the postmaster environment holds NO
# AWS_* credentials, the warehouse is served over the SigV4-verifying S3 fixture,
# and a green read can only happen if the request was signed with the vended
# credentials the SigV4 fixture is configured to accept.
#
# Two hermetic servers: the S3 fixture (test/objstore_http_server.py, SigV4) and
# the REST catalog (test/iceberg_rest_server.py) whose loadTable points at an
# s3:// metadata-location and vends the S3 fixture's key/secret/region/endpoint.
#
# Usage:  test/iceberg_rest_vended.sh [PG_CONFIG]

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

python3 -c 'import json' 2>/dev/null || pgc_skip python "python3 is needed"
MODDIR="$("$PGC_PG_CONFIG" --pkglibdir)"
[ -f "$MODDIR/pgcolumnar_objstore.so" ] \
	|| pgc_skip objstore "the object-store module is not installed"

FX="$(dirname "${BASH_SOURCE[0]}")/fixtures/iceberg"
[ -f "$FX/warehouse_del/db/t/metadata/apply.metadata.json" ] \
	|| pgc_skip fixture "iceberg fixtures are missing"

AKID="VENDEDKEYID"; SECRET="vended-secret-shh"; REGION="pgc-test-1"; BUCKET="pgc-bucket"
RESTTOK="rest-token-vend"
# One free port is picked per server only AFTER the previous one binds:
# pgc_pick_free_port does not hold the port, so picking six up front returns the
# same number six times (nothing is bound between the picks).
S3_PORT="$(pgc_pick_free_port "$PGC_AUX_PORT_LO" "$PGC_AUX_PORT_HI")"
PIDS=""
LAST_PORT=""

vend_teardown() { for p in $PIDS; do kill "$p" 2>/dev/null; done; pgc_teardown; }
trap vend_teardown EXIT INT TERM

# refusal SQL PHRASE -> the SQLSTATE when SQL is refused with PHRASE in its message;
# "SQLSTATE (another refusal: MESSAGE)" when something else refused it; "no error"
# when it ran; nothing when the server could not be asked. The wrong-secret arm read
# the SQLSTATE alone, and 28000 is also "no credentials": with vended credentials
# never built at all, the read was refused for having none and the arm passed.
refusal() {
	local out st msg
	await_up
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -qtA 2>&1 <<SQLEOF
\\set VERBOSITY verbose
$1;
SQLEOF
)"
	st="$(sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\): .*/\1/p' <<<"$out" | head -1)"
	msg="$(sed -n 's/^ERROR:  [0-9A-Z]\{5\}: //p' <<<"$out" | head -1)"
	if [ -n "$st" ]; then
		case "$msg" in *"$2"*) echo "$st" ;; *) echo "$st (another refusal: $msg)" ;; esac
	elif grep -q 'could not connect\|server closed the connection\|terminating connection' <<<"$out"; then
		:
	else
		echo "no error"
	fi
}

# await_up -> wait, at most 60 s, until the server answers, so an arm after a crash
# reads the table rather than crash recovery.
await_up() {
	local i
	for i in $(seq 1 120); do
		[ "$(q 'SELECT 1')" = 1 ] && return 0
		sleep 0.5
	done
}

# Backends killed by a signal, from the server log: a number, or nothing when the
# log can't be read. "SELECT 1" alone answers again after crash recovery: with the
# 403 refusal replaced by abort(), the survival check passed.
crashes() {
	local _n
	_n="$(pgc_pg "grep -c 'terminated by signal' '$PGC_LOGFILE'" 2>/dev/null)"
	case "$_n" in '' | *[!0-9]*) ;; *) echo "$_n" ;; esac
}
survived() {	# survived BEFORE -> "1" when the server answers and no backend died since
	local _up _now
	await_up
	_up="$(q 'SELECT 1')"; _now="$(crashes)"
	if ! [[ "$1" =~ ^[0-9]+$ && "$_now" =~ ^[0-9]+$ ]]; then echo "no (crash count unreadable)"
	elif [ "$_up" = 1 ] && [ "$_now" = "$1" ]; then echo 1
	else echo "no ([$_up], $(( _now - $1 )) backend crashes)"; fi
}
pg_restart_env() {
	pgc_pg "pg_ctl -D '$PGC_PGDATA' stop -m fast -w" >/dev/null 2>&1
	# Every AWS_* unset first: the vended arms prove something only while the postmaster
	# holds no ambient credential, so none may arrive from the caller's environment or
	# a login profile. An arm that wants ambient credentials passes them in "$@".
	pgc_pg "unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_ENDPOINT_URL AWS_REGION AWS_DEFAULT_REGION AWS_PROFILE AWS_SHARED_CREDENTIALS_FILE AWS_CONFIG_FILE; $* pg_ctl -D '$PGC_PGDATA' -l '$PGC_LOGFILE' start -w" >/dev/null 2>&1
	for _ in $(seq 1 30); do [ -n "$(q 'SELECT 1')" ] && return 0; sleep 0.5; done
	echo "FATAL: cluster did not come back after env restart"; exit 1
}
start_rest() {  # $@=extra args; picks a fresh free port into LAST_PORT
	LAST_PORT="$(pgc_pick_free_port "$PGC_AUX_PORT_LO" "$PGC_AUX_PORT_HI")"
	python3 "$(dirname "${BASH_SOURCE[0]}")/iceberg_rest_server.py" \
		--port "$LAST_PORT" --log "$PGC_WORKDIR/rest_$LAST_PORT.log" \
		--namespace db --table t \
		--metadata-location "s3://$BUCKET/db/t/metadata/apply.metadata.json" \
		"$@" > "$PGC_WORKDIR/rest_$LAST_PORT.out" 2>&1 &
	PIDS="$PIDS $!"
	for _ in $(seq 1 50); do grep -q READY "$PGC_WORKDIR/rest_$LAST_PORT.out" 2>/dev/null && break; sleep 0.1; done
}

# ---- serve the warehouse over the SigV4 S3 fixture ---------------------------
mkdir -p "$PGC_WORKDIR/$BUCKET"
cp -r "$FX/warehouse_del/db" "$PGC_WORKDIR/$BUCKET/db"
chmod -R u+rwX "$PGC_WORKDIR/$BUCKET"
python3 "$(dirname "${BASH_SOURCE[0]}")/objstore_http_server.py" \
	--dir "$PGC_WORKDIR" --port "$S3_PORT" --log "$PGC_WORKDIR/s3.log" \
	--sigv4-key "$AKID" --sigv4-secret "$SECRET" --sigv4-region "$REGION" \
	> "$PGC_WORKDIR/s3.out" 2>&1 &
PIDS="$PIDS $!"
for _ in $(seq 1 50); do grep -q READY "$PGC_WORKDIR/s3.out" 2>/dev/null && break; sleep 0.1; done
check "premise: the SigV4 S3 fixture is up" \
	"$(grep -c READY "$PGC_WORKDIR/s3.out" 2>/dev/null)" "1"

S3EP="http://127.0.0.1:$S3_PORT"
# the catalog that vends the correct S3 creds + endpoint
start_rest --token "$RESTTOK" \
	--vend-key "$AKID" --vend-secret "$SECRET" --vend-region "$REGION" --vend-endpoint "$S3EP"
CAT="http://127.0.0.1:$LAST_PORT"

# NO AWS_* credentials in the environment: a read can only work with vended ones.
pg_restart_env "PGCOLUMNAR_ICEBERG_REST_TOKEN='$RESTTOK'"
q "ALTER SYSTEM SET pgcolumnar.objstore_allowed_endpoints = '127.0.0.1'" >/dev/null
q "SELECT pg_reload_conf()" >/dev/null
CRASHES_BEFORE="$(crashes)"

# ---- CORE: vended creds sign the S3 reads (no ambient creds exist) -----------
# apply.metadata.json drops ordinals 1,3 (id 2, id 4); survivors 1,3,5.
check "iceberg_rest_scan reads via vended credentials (no ambient creds set)" \
	"$(q "SELECT id || '|' || region || '|' || amount
	      FROM pgcolumnar.iceberg_rest_scan('$CAT','db','t') AS t(id bigint, region text, amount int)
	      ORDER BY id")" \
	"$(printf '1|eu|10\n3|us|30\n5|us|50')"

# ---- the vended secret never appears in any log --------------------------
# the secret comes from the catalog response, is used only as the SigV4 signing
# seed (never sent on the wire), and is never a SQL argument, so log_statement
# cannot capture it. Pin that: read under log_statement='all', then grep.
q "ALTER SYSTEM SET log_statement='all'" >/dev/null
q "SELECT pg_reload_conf()" >/dev/null
q "SELECT count(*) FROM pgcolumnar.iceberg_rest_scan('$CAT','db','t')
   AS t(id bigint, region text, amount int)" >/dev/null
q "ALTER SYSTEM SET log_statement='none'" >/dev/null
q "SELECT pg_reload_conf()" >/dev/null
# PREMISE FOR THE CLAIM BELOW, which can pass having read nothing (#1032).
# `grep -c` prints `0` for a file that EXISTS AND IS EMPTY. A missing or unreadable
# file prints NOTHING, so `"" != "0"` already fails the claim -- empty is the only
# hole, and it is the one a rotation, a `log_destination` change or a truncating
# reuse path would open. Measured, all four states:
#
#     real log   got=[0] passes     empty log  got=[0] PASSES having read nothing
#     missing    got=[]  fails      leaking    got=[1] fails
#
# NOT `grep -c . ... || echo 0`: `grep -c` prints `0` AND exits 1 on an empty file,
# so that form yields TWO lines and turns the comparison into a shell error. The
# assignment form below carries one value.
#
# The request logs in this suite already have positive controls; the PG log did not.
_irv_loglines="$(grep -c . "$PGC_LOGFILE" 2>/dev/null)" || _irv_loglines=0
check "premise: the PG server log holds lines to search" \
	"$([ "${_irv_loglines:-0}" -gt 0 ] && echo yes || echo no)" "yes"
check "the vended secret never appears in the PG server log" \
	"$(grep -c "$SECRET" "$PGC_LOGFILE" 2>/dev/null)" "0"
check "the vended secret never appears in the S3 request log" \
	"$(grep -c "$SECRET" "$PGC_WORKDIR/s3.log" 2>/dev/null)" "0"

# ---- a WRONG vended secret is used and refused (403 -> 28000), not bypassed --
start_rest --token "$RESTTOK" \
	--vend-key "$AKID" --vend-secret "wrong-vended-secret" --vend-region "$REGION" --vend-endpoint "$S3EP"
BAD_PORT="$LAST_PORT"
check "a wrong vended secret is refused by S3, not bypassed (28000)" \
	"$(refusal "SELECT * FROM pgcolumnar.iceberg_rest_scan('http://127.0.0.1:$BAD_PORT','db','t')
	                AS t(id bigint, region text, amount int)" "was denied (HTTP 403")" \
	"28000"

# ---- storage-credentials: longest-prefix entry (correct) beats catch-all ----
# The catalog's flat config vends WRONG credentials here too, so a reader that
# ignored the storage-credentials array and used the flat config is refused. With
# the array never read, this arm passed while the flat config was correct.
start_rest --token "$RESTTOK" \
	--vend-key "$AKID" --vend-secret "$SECRET" --vend-region "$REGION" --vend-endpoint "$S3EP" \
	--storage-cred-prefix "s3://$BUCKET/db/t"
SC_PORT="$LAST_PORT"
await_up
check "storage-credentials longest-prefix match selects the right creds" \
	"$(q "SELECT count(*) FROM pgcolumnar.iceberg_rest_scan('http://127.0.0.1:$SC_PORT','db','t')
	      AS t(id bigint, region text, amount int)")" \
	"3"

# ---- vended creds do NOT bypass the allow-list ------------------------------
# a catalog that vends an endpoint off the allow-list is refused at the read.
start_rest --token "$RESTTOK" \
	--vend-key "$AKID" --vend-secret "$SECRET" --vend-region "$REGION" \
	--vend-endpoint "http://127.0.0.2:$S3_PORT"
LL_PORT="$LAST_PORT"
check "vended creds for an off-allow-list endpoint are refused (42501)" \
	"$(refusal "SET pgcolumnar.objstore_allowed_endpoints='127.0.0.1';
	                SELECT * FROM pgcolumnar.iceberg_rest_scan('http://127.0.0.1:$LL_PORT','db','t')
	                AS t(id bigint, region text, amount int)" "is not in pgcolumnar.objstore_allowed_endpoints")" \
	"42501"

# ---- ambient still works when the catalog vends nothing ---------------------
start_rest --token "$RESTTOK"   # no --vend-key: config is empty
AMB_PORT="$LAST_PORT"
pg_restart_env "PGCOLUMNAR_ICEBERG_REST_TOKEN='$RESTTOK'" \
	"AWS_ENDPOINT_URL='$S3EP'" "AWS_ACCESS_KEY_ID='$AKID'" \
	"AWS_SECRET_ACCESS_KEY='$SECRET'" "AWS_REGION='$REGION'"
q "ALTER SYSTEM SET pgcolumnar.objstore_allowed_endpoints = '127.0.0.1'" >/dev/null
q "SELECT pg_reload_conf()" >/dev/null
await_up
check "a table vending no creds still reads with ambient (cfg NULL path)" \
	"$(q "SELECT count(*) FROM pgcolumnar.iceberg_rest_scan('http://127.0.0.1:$AMB_PORT','db','t')
	      AS t(id bigint, region text, amount int)")" \
	"3"

check "backend still up after the vended-credential arms" "$(survived "$CRASHES_BEFORE")" "1"

pgc_summary
