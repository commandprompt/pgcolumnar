#!/usr/bin/env bash
#
# pgColumnar #1796: an object-store refusal gives advice that fits the refusal.
#
# Three facets of the issue, each a message that pointed the reader at the wrong
# thing while access was still refused correctly:
#
#   B  the 403 hint named the AWS_* environment of the server process for every
#      refused key, including a key from a user mapping or one a REST catalog
#      vended. The hint now names the source of the key that was refused.
#   C  the allow-list hint suggested the host without its port, as the whole
#      list: run as written it opened every port on the host and dropped every
#      other entry. It now suggests the host:port the message names, appended to
#      the current list.
#   D  a single-PUT export (bigger than the socket buffers, smaller than one
#      part) that the store refused before reading the body lost the HTTP
#      status: the body send failed, and the code never read the answer that had
#      already arrived. The 403 and its hint now survive.
#
# Facet A (s3://<bucket>/_ in prefix and glob errors) is #1869's fix and its
# arms are in test/objstore_export_open_uploads.sh.
#
# One SigV4 fixture with a refused bucket (--tamper-bucket: every request there
# is verified against another secret, refused 403 before any body is read), two
# REST catalogs over the committed iceberg warehouse (one vends a wrong secret,
# one vends nothing), and a postmaster whose environment holds the fixture's
# right key. So in the mapping and vended arms the environment would have read
# the object, and a premise shows it does.
#
# Usage:  test/objstore_error_advice.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# The part size is raised so that the mid-size export below is one PUT.
export PGC_EXTRA_CONF=$'pgcolumnar.objstore_allowed_endpoints=\'127.0.0.1\'\npgcolumnar.objstore_part_size=67108864'

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

MODDIR="$("$PGC_PG_CONFIG" --pkglibdir)"
[ -f "$MODDIR/pgcolumnar_objstore.so" ] \
	|| pgc_skip objstore "the object-store module is not installed"
FX="$(dirname "${BASH_SOURCE[0]}")/fixtures/iceberg"
[ -f "$FX/warehouse_del/db/t/metadata/apply.metadata.json" ] \
	|| pgc_skip fixture "iceberg fixtures are missing"

AKID="PGCTESTKEYID"; SECRET="pgctest-secret-shhh"; REGION="pgc-test-1"
BUCKET="pgc-bucket"; DENIED="pgc-denied"; RESTTOK="rest-token-advice"
PIDS=""
LAST_PORT=""

advice_teardown() { for p in $PIDS; do kill "$p" 2>/dev/null; done; pgc_teardown; }
trap advice_teardown EXIT INT TERM

# refusal SQL -> "SQLSTATE: message | HINT: hint" for the first error SQL raises
# ("| no hint" when it has none), or "no error (first line of output)".
refusal() {
	local out st msg hint
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -qtA 2>&1 <<SQLEOF
\\set VERBOSITY verbose
$1;
SQLEOF
)"
	st="$(sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\): .*/\1/p' <<<"$out" | head -1)"
	msg="$(sed -n 's/^ERROR:  [0-9A-Z]\{5\}: //p' <<<"$out" | head -1)"
	hint="$(sed -n 's/^HINT:  //p' <<<"$out" | head -1)"
	if [ -n "$st" ]; then
		echo "$st: $msg | ${hint:+HINT: }${hint:-no hint}"
	else
		echo "no error ($(head -1 <<<"$out"))"
	fi
}
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
ready() {	# ready OUTFILE -> wait until a fixture printed READY
	for _ in $(seq 1 50); do grep -q READY "$1" 2>/dev/null && return 0; sleep 0.1; done
}
start_rest() {  # $@=extra args; picks a fresh free port into LAST_PORT
	LAST_PORT="$(pgc_pick_free_port "$PGC_AUX_PORT_LO" "$PGC_AUX_PORT_HI")"
	python3 "$(dirname "${BASH_SOURCE[0]}")/iceberg_rest_server.py" \
		--port "$LAST_PORT" --log "$PGC_WORKDIR/rest_$LAST_PORT.log" \
		--namespace db --table t \
		--metadata-location "s3://$BUCKET/db/t/metadata/apply.metadata.json" \
		"$@" > "$PGC_WORKDIR/rest_$LAST_PORT.out" 2>&1 &
	PIDS="$PIDS $!"
	ready "$PGC_WORKDIR/rest_$LAST_PORT.out"
}

# ---- fixtures ----------------------------------------------------------------
mkdir -p "$PGC_WORKDIR/$BUCKET" "$PGC_WORKDIR/$DENIED"
cp -r "$FX/warehouse_del/db" "$PGC_WORKDIR/$BUCKET/db"
chmod -R u+rwX "$PGC_WORKDIR/$BUCKET"
S3_PORT="$(pgc_pick_free_port "$PGC_AUX_PORT_LO" "$PGC_AUX_PORT_HI")"
S3_LOG="$PGC_WORKDIR/s3.log"
python3 "$(dirname "${BASH_SOURCE[0]}")/objstore_http_server.py" \
	--dir "$PGC_WORKDIR" --port "$S3_PORT" --log "$S3_LOG" \
	--sigv4-key "$AKID" --sigv4-secret "$SECRET" --sigv4-region "$REGION" \
	--tamper-bucket "$DENIED" > "$PGC_WORKDIR/s3.out" 2>&1 &
PIDS="$PIDS $!"
ready "$PGC_WORKDIR/s3.out"
S3EP="http://127.0.0.1:$S3_PORT"
start_rest --token "$RESTTOK" --vend-key "$AKID" --vend-secret "wrong-vended-secret" \
	--vend-region "$REGION" --vend-endpoint "$S3EP"
CAT_BAD="http://127.0.0.1:$LAST_PORT"
start_rest --token "$RESTTOK"
CAT_NONE="http://127.0.0.1:$LAST_PORT"
check "premise: the SigV4 S3 fixture and both REST catalogs are up" \
	"$(cat "$PGC_WORKDIR/s3.out" "$PGC_WORKDIR"/rest_*.out 2>/dev/null | grep -c READY)" "3"

# The postmaster's environment holds the fixture's RIGHT key.
pgc_pg "pg_ctl -D '$PGC_PGDATA' stop -m fast -w" >/dev/null 2>&1
pgc_pg "unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_ENDPOINT_URL AWS_REGION AWS_DEFAULT_REGION AWS_PROFILE AWS_SHARED_CREDENTIALS_FILE AWS_CONFIG_FILE; AWS_ENDPOINT_URL='$S3EP' AWS_ACCESS_KEY_ID='$AKID' AWS_SECRET_ACCESS_KEY='$SECRET' AWS_REGION='$REGION' PGCOLUMNAR_ICEBERG_REST_TOKEN='$RESTTOK' pg_ctl -D '$PGC_PGDATA' -l '$PGC_LOGFILE' start -w" >/dev/null 2>&1
for _ in $(seq 1 30); do [ -n "$(q 'SELECT 1')" ] && break; sleep 0.5; done
crashes0="$(logcount 'terminated by signal')"

psql_run "CREATE TABLE ea_small USING pgcolumnar AS SELECT g::int8 AS id FROM generate_series(1,1000) g;
          CREATE TABLE ea_mid USING pgcolumnar AS
            SELECT g::int8 AS id, md5(g::text)||md5((g*7)::text) AS t FROM generate_series(1,300000) g;" >/dev/null
q "SELECT pgcolumnar.export_parquet('ea_small', 's3://$BUCKET/small.parquet')" >/dev/null
check "premise: the server environment's key writes and reads s3://pgc-bucket/small.parquet" \
	"$(q "SELECT count(*) FROM pgcolumnar.read_parquet('s3://$BUCKET/small.parquet') AS t(id int8)")" "1000"

# ---- B: the 403 hint names the source of the refused key --------------------
# A glob is resolved by listing its prefix, and #1869 names that listing by the
# prefix it listed, so the glob arm's message names s3://bucket/.
check "environment key refused: the 403 hint names the server environment" \
	"$(refusal "SELECT count(*) FROM pgcolumnar.read_parquet('s3://$DENIED/small.parquet') AS t(id int8)")" \
	"28000: columnar: access to \"s3://$DENIED/small.parquet\" was denied (HTTP 403) | HINT: Check the AWS_* credential environment of the server process."

psql_run "CREATE SERVER ea_s3 FOREIGN DATA WRAPPER pgcolumnar_parquet
            OPTIONS (endpoint '$S3EP', region '$REGION');
          CREATE USER MAPPING FOR postgres SERVER ea_s3
            OPTIONS (access_key_id '$AKID', secret_access_key '$SECRET');
          CREATE FOREIGN TABLE ea_one (id int8) SERVER ea_s3 OPTIONS (path 's3://$BUCKET/small.parquet');
          CREATE FOREIGN TABLE ea_glob (id int8) SERVER ea_s3 OPTIONS (path 's3://$BUCKET/sm*.parquet');" >/dev/null
check "premise: the foreign table reads with the right secret in the user mapping" \
	"$(q "SELECT count(*) FROM ea_one")" "1000"
psql_run "ALTER USER MAPPING FOR postgres SERVER ea_s3 OPTIONS (SET secret_access_key 'wrong')" >/dev/null
check "user mapping key refused: the 403 hint names the user mapping" \
	"$(refusal "SELECT count(*) FROM ea_one")" \
	"28000: columnar: access to \"s3://$BUCKET/small.parquet\" was denied (HTTP 403) | HINT: Check access_key_id and secret_access_key in the user mapping the current role uses for this server."
check "user mapping key refused on a glob's listing: the 403 hint names the user mapping" \
	"$(refusal "SELECT count(*) FROM ea_glob")" \
	"28000: columnar: access to \"s3://$BUCKET/\" was denied (HTTP 403: SignatureDoesNotMatch) | HINT: Check access_key_id and secret_access_key in the user mapping the current role uses for this server."

check "premise: the iceberg table reads through a catalog that vends nothing (server environment)" \
	"$(q "SELECT count(*) FROM pgcolumnar.iceberg_rest_scan('$CAT_NONE','db','t') AS t(id bigint, region text, amount int)")" "3"
check "vended key refused: the 403 hint names the catalog's vended credentials" \
	"$(refusal "SELECT count(*) FROM pgcolumnar.iceberg_rest_scan('$CAT_BAD','db','t') AS t(id bigint, region text, amount int)" \
		| sed -E 's#/metadata/[^"]*\.metadata\.json"#/metadata/M.metadata.json"#')" \
	"28000: columnar: access to \"s3://$BUCKET/db/t/metadata/M.metadata.json\" was denied (HTTP 403) | HINT: The storage credentials the catalog vended for this table were refused."

# ---- C: the allow-list hint adds host:port to the current list ---------------
READ_SMALL="SELECT count(*) FROM pgcolumnar.read_parquet('s3://$BUCKET/small.parquet') AS t(id int8)"
check "allow-list refusal, empty list: the hint suggests host:port" \
	"$(refusal "SET pgcolumnar.objstore_allowed_endpoints = ''; $READ_SMALL")" \
	"42501: columnar: endpoint \"127.0.0.1:$S3_PORT\" is not in pgcolumnar.objstore_allowed_endpoints | HINT: A superuser can add it: ALTER SYSTEM SET pgcolumnar.objstore_allowed_endpoints = '127.0.0.1:$S3_PORT'; SELECT pg_reload_conf();"
ADD_HINT="$(refusal "SET pgcolumnar.objstore_allowed_endpoints = '127.0.0.1:1,example.com'; $READ_SMALL")"
check "allow-list refusal: the hint keeps the current entries and appends host:port" \
	"$ADD_HINT" \
	"42501: columnar: endpoint \"127.0.0.1:$S3_PORT\" is not in pgcolumnar.objstore_allowed_endpoints | HINT: A superuser can add it: ALTER SYSTEM SET pgcolumnar.objstore_allowed_endpoints = '127.0.0.1:1,example.com,127.0.0.1:$S3_PORT'; SELECT pg_reload_conf();"
check "allow-list refusal: a quote in the current list is doubled in the hint" \
	"$(refusal "SET pgcolumnar.objstore_allowed_endpoints = 'o''host'; $READ_SMALL" | sed -n 's/.*HINT: //p')" \
	"A superuser can add it: ALTER SYSTEM SET pgcolumnar.objstore_allowed_endpoints = 'o''host,127.0.0.1:$S3_PORT'; SELECT pg_reload_conf();"
SUGGESTED="$(sed -n "s/.*objstore_allowed_endpoints = '\(.*\)'; SELECT pg_reload_conf();\$/\1/p" <<<"$ADD_HINT")"
check "allow-list refusal: the list the hint suggests permits the endpoint" \
	"$(q "SET pgcolumnar.objstore_allowed_endpoints = '${SUGGESTED:-unparsed}'; $READ_SMALL" | tail -n 1)" "1000"

# ---- D: a single-PUT export refused before the body was read keeps its status --
: > "$S3_LOG"
q "SELECT pgcolumnar.export_parquet('ea_mid', 's3://$BUCKET/mid.parquet')" >/dev/null
MID_PUTS="$(grep -c "^PUT /$BUCKET/mid.parquet " "$S3_LOG")"
MID_POSTS="$(grep -c "^POST /$BUCKET/mid.parquet " "$S3_LOG")"
MID_BYTES="$(stat -c %s "$PGC_WORKDIR/$BUCKET/mid.parquet" 2>/dev/null || echo 0)"
check "premise: the mid-size export is one PUT of more than 4 MiB" \
	"$( [ "$MID_PUTS" = 1 ] && [ "$MID_POSTS" = 0 ] && [ "$MID_BYTES" -gt 4194304 ] && echo yes \
		|| echo "no ($MID_PUTS PUT, $MID_POSTS POST, $MID_BYTES bytes)")" "yes"
check "a small export to the refused bucket reports the 403 and its hint" \
	"$(refusal "SELECT pgcolumnar.export_parquet('ea_small', 's3://$DENIED/s.parquet')")" \
	"28000: columnar: access to \"s3://$DENIED/s.parquet\" was denied (HTTP 403: SignatureDoesNotMatch) | HINT: Check the AWS_* credential environment of the server process."
check "mid-size export refused before its body was read: the 403 and its hint survive" \
	"$(refusal "SELECT pgcolumnar.export_parquet('ea_mid', 's3://$DENIED/m.parquet')")" \
	"28000: columnar: access to \"s3://$DENIED/m.parquet\" was denied (HTTP 403: SignatureDoesNotMatch) | HINT: Check the AWS_* credential environment of the server process."

check "the server survived the refusals" "$(survived "$crashes0")" "1"

pgc_summary
