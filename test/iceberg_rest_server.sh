#!/usr/bin/env bash
#
# pgColumnar Iceberg REST catalog FOREIGN SERVER + USER MAPPING credentials
# (#656). The iceberg_rest_* functions accept a FOREIGN SERVER name in place of a
# catalog URI: the server holds catalog_uri, and the current role's USER MAPPING
# holds the bearer token (kept role-private in pg_user_mapping, never a SQL
# argument or a log line). The proof: with NO PGCOLUMNAR_ICEBERG_REST_TOKEN in
# the environment, a server-name call still authenticates -- so the mapping
# token, not ambient, is what signed the request (the hermetic fixture 401s a
# wrong/absent token).
#
# Usage:  test/iceberg_rest_server.sh [PG_CONFIG]

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

python3 -c 'import json' 2>/dev/null || pgc_skip python "python3 is needed"
MODDIR="$("$PGC_PG_CONFIG" --pkglibdir)"
[ -f "$MODDIR/pgcolumnar_objstore.so" ] \
	|| pgc_skip objstore "the object-store module is not installed"

TOKEN="s3cr3t-mapping-token-xyz"
REST_PORT="$(pgc_pick_free_port "$PGC_AUX_PORT_LO" "$PGC_AUX_PORT_HI")"
REST_LOG="$PGC_WORKDIR/rest.log"
MDLOC="file:///tmp/pgc_rest/db/events/metadata/00001.metadata.json"
SRV_PID=""

st_teardown() { [ -n "$SRV_PID" ] && kill "$SRV_PID" 2>/dev/null; pgc_teardown; }
trap st_teardown EXIT INT TERM

# sqlstate_of SQL -> the SQLSTATE SQL failed with; "ok" when it ran; nothing when the
# server could not be asked. "Ran" used to read as nothing too, so with the server
# stopped the two "accepted" arms passed: a dead server and a statement that ran
# looked the same.
sqlstate_of() {
	local out rc st
	await_up
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -qtA -v ON_ERROR_STOP=1 2>&1 <<SQLEOF
\\set VERBOSITY sqlstate
$1;
SQLEOF
)"
	rc=$?
	st="$(sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\).*/\1/p' <<<"$out" | head -1)"
	if [ -n "$st" ]; then echo "$st"
	elif [ "$rc" = 0 ]; then echo ok
	fi
}

# refusal SQL PHRASE -> the SQLSTATE when SQL is refused with PHRASE in its message;
# "SQLSTATE (another refusal: MESSAGE)" when something else refused it; "no error"
# when it ran; nothing when the server could not be asked. 28000 and 42501 each come
# from more than one guard here: with the endpoint allow-list removed, the link-local
# guard still refused 169.254.169.254 with 42501, and "refused by the allow-list"
# passed.
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
# reads the catalog rather than crash recovery (with the half-credential guard
# removed the backend segfaults, and the next arm went red as collateral).
await_up() {
	local i
	for i in $(seq 1 120); do
		[ "$(q 'SELECT 1')" = 1 ] && return 0
		sleep 0.5
	done
}

# Backends killed by a signal, from the server log: a number, or nothing when the
# log can't be read. "SELECT 1" alone answers again after crash recovery: with the
# half-credential guard removed (a segfault), or abort() in place of the catalog's
# 401 refusal, "backend still up" passed.
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
	pgc_pg "$* pg_ctl -D '$PGC_PGDATA' -l '$PGC_LOGFILE' start -w" >/dev/null 2>&1
	for _ in $(seq 1 30); do [ -n "$(q 'SELECT 1')" ] && return 0; sleep 0.5; done
	echo "FATAL: cluster did not come back after env restart"; exit 1
}

python3 "$(dirname "${BASH_SOURCE[0]}")/iceberg_rest_server.py" \
	--port "$REST_PORT" --log "$REST_LOG" --token "$TOKEN" \
	--namespace db --table events --metadata-location "$MDLOC" \
	> "$PGC_WORKDIR/rest.out" 2>&1 &
SRV_PID=$!
for _ in $(seq 1 50); do grep -q READY "$PGC_WORKDIR/rest.out" 2>/dev/null && break; sleep 0.1; done
check "premise: rest fixture up" "$(grep -c READY "$PGC_WORKDIR/rest.out" 2>/dev/null)" "1"

CAT="http://127.0.0.1:$REST_PORT"
# NO token in the environment: only a USER MAPPING can authenticate.
pg_restart_env ""
q "ALTER SYSTEM SET pgcolumnar.objstore_allowed_endpoints = '127.0.0.1'" >/dev/null
q "SELECT pg_reload_conf()" >/dev/null
CRASHES_BEFORE="$(crashes)"

q "CREATE SERVER cat FOREIGN DATA WRAPPER pgcolumnar_iceberg_catalog
   OPTIONS (catalog_uri '$CAT')" >/dev/null
q "CREATE USER MAPPING FOR postgres SERVER cat OPTIONS (token '$TOKEN')" >/dev/null

# ---- a server-name call authenticates with the mapping token -----------------
check "iceberg_rest_table_location('cat',...) reads via the mapping token" \
	"$(q "SELECT pgcolumnar.iceberg_rest_table_location('cat','db','events')")" \
	"$MDLOC"
check "the request carried an Authorization header" \
	"$(grep -c 'GET /v1/namespaces/db/tables/events AUTH=yes' "$REST_LOG" 2>/dev/null)" "1"

# ---- the mapping token never appears in the PG log ---------------------------
q "ALTER SYSTEM SET log_statement='all'" >/dev/null
q "SELECT pg_reload_conf()" >/dev/null
q "SELECT pgcolumnar.iceberg_rest_table_location('cat','db','events')" >/dev/null
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
_irs_loglines="$(grep -c . "$PGC_LOGFILE" 2>/dev/null)" || _irs_loglines=0
check "premise: the PG server log holds lines to search, for the mapping token" \
	"$([ "${_irs_loglines:-0}" -gt 0 ] && echo yes || echo no)" "yes"
check "the mapping token never appears in the PG server log" \
	"$(grep -c "$TOKEN" "$PGC_LOGFILE" 2>/dev/null)" "0"

# ---- a wrong mapping token is refused by the catalog (28000) -----------------
q "ALTER USER MAPPING FOR postgres SERVER cat OPTIONS (SET token 'wrong-token')" >/dev/null
check "a wrong mapping token is refused (28000)" \
	"$(refusal "SELECT pgcolumnar.iceberg_rest_table_location('cat','db','events')" "refused the request (HTTP 401")" \
	"28000"

# ---- no credential at all (no mapping, no env token) is refused --------------
q "DROP USER MAPPING FOR postgres SERVER cat" >/dev/null
check "no mapping and no env token is refused, not bypassed (28000)" \
	"$(refusal "SELECT pgcolumnar.iceberg_rest_table_location('cat','db','events')" "refused the request (HTTP 401")" \
	"28000"
q "CREATE USER MAPPING FOR postgres SERVER cat OPTIONS (token '$TOKEN')" >/dev/null

# ---- validator: secrets only on a user mapping, uri only on a server ---------
check "a token option on the SERVER is rejected (HV00D)" \
	"$(sqlstate_of "CREATE SERVER bad1 FOREIGN DATA WRAPPER pgcolumnar_iceberg_catalog OPTIONS (catalog_uri '$CAT', token 'x')")" \
	"HV00D"
check "a catalog_uri option on the USER MAPPING is rejected (HV00D)" \
	"$(sqlstate_of "ALTER USER MAPPING FOR postgres SERVER cat OPTIONS (ADD catalog_uri 'x')")" \
	"HV00D"

# ---- #656: a warehouse SERVER option is accepted and sent on GET /v1/config --
# On current main the validator rejects any server option but catalog_uri, so
# CREATE SERVER ... OPTIONS (warehouse ...) fails HV00D. It must be accepted and
# forwarded to GET /v1/config as ?warehouse=, to select a warehouse on a
# multi-warehouse catalog.
check "a warehouse option on the SERVER is accepted (not HV00D)" \
	"$(sqlstate_of "CREATE SERVER whcat FOREIGN DATA WRAPPER pgcolumnar_iceberg_catalog OPTIONS (catalog_uri '$CAT', warehouse 'wh1')")" \
	"ok"
q "CREATE USER MAPPING FOR postgres SERVER whcat OPTIONS (token '$TOKEN')" >/dev/null
await_up
check "a warehouse-configured server resolves a table location" \
	"$(q "SELECT pgcolumnar.iceberg_rest_table_location('whcat','db','events')")" \
	"$MDLOC"
check "the warehouse was sent on GET /v1/config (?warehouse=wh1)" \
	"$(grep -c 'GET /v1/config?warehouse=wh1 ' "$REST_LOG" 2>/dev/null)" "1"

# ---- OAuth2 client-credentials: mint a bearer, then use it -------------------
# A second fixture requires a client id/secret at POST /v1/oauth/tokens and, on a
# match, mints OATOKEN -- which loadTable then requires. So a green read proves
# the full mint->use flow, with NO env token and NO static mapping token. The
# secret travels only in the POST body; it must appear in neither log.
OCID="client-abc"; OCSEC="s3cr3t-oauth-secret"; OATOKEN="minted-bearer-777"
OPORT="$(pgc_pick_free_port "$PGC_AUX_PORT_LO" "$PGC_AUX_PORT_HI")"
OLOG="$PGC_WORKDIR/orest.log"
python3 "$(dirname "${BASH_SOURCE[0]}")/iceberg_rest_server.py" \
	--port "$OPORT" --log "$OLOG" --token "$OATOKEN" \
	--oauth-client-id "$OCID" --oauth-client-secret "$OCSEC" \
	--namespace db --table events --metadata-location "$MDLOC" \
	> "$PGC_WORKDIR/orest.out" 2>&1 &
OSRV_PID=$!
for _ in $(seq 1 50); do grep -q READY "$PGC_WORKDIR/orest.out" 2>/dev/null && break; sleep 0.1; done
OCAT="http://127.0.0.1:$OPORT"
q "CREATE SERVER ocat FOREIGN DATA WRAPPER pgcolumnar_iceberg_catalog
   OPTIONS (catalog_uri '$OCAT')" >/dev/null
q "CREATE USER MAPPING FOR postgres SERVER ocat
   OPTIONS (oauth_client_id '$OCID', oauth_client_secret '$OCSEC')" >/dev/null

await_up
check "a server with an OAuth2 mapping reads by a minted bearer" \
	"$(q "SELECT pgcolumnar.iceberg_rest_table_location('ocat','db','events')")" \
	"$MDLOC"
check "the OAuth2 token endpoint was called (POST /v1/oauth/tokens)" \
	"$(grep -c 'POST /v1/oauth/tokens' "$OLOG" 2>/dev/null)" "1"
check "loadTable then carried the minted bearer (Authorization present)" \
	"$(grep -c 'GET /v1/namespaces/db/tables/events AUTH=yes' "$OLOG" 2>/dev/null)" "1"
check "the client secret never appears in the fixture request log" \
	"$(grep -c "$OCSEC" "$OLOG" 2>/dev/null)" "0"
q "ALTER SYSTEM SET log_statement='all'" >/dev/null
q "SELECT pg_reload_conf()" >/dev/null
q "SELECT pgcolumnar.iceberg_rest_table_location('ocat','db','events')" >/dev/null
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
_irs_loglines2="$(grep -c . "$PGC_LOGFILE" 2>/dev/null)" || _irs_loglines2=0
check "premise: the PG server log holds lines to search, for the client secret" \
	"$([ "${_irs_loglines2:-0}" -gt 0 ] && echo yes || echo no)" "yes"
check "the client secret never appears in the PG server log" \
	"$(grep -c "$OCSEC" "$PGC_LOGFILE" 2>/dev/null)" "0"

q "ALTER USER MAPPING FOR postgres SERVER ocat OPTIONS (SET oauth_client_secret 'wrong-secret')" >/dev/null
check "a wrong OAuth2 client secret is refused (28000)" \
	"$(refusal "SELECT pgcolumnar.iceberg_rest_table_location('ocat','db','events')" "refused the OAuth2")" \
	"28000"
q "ALTER USER MAPPING FOR postgres SERVER ocat OPTIONS (DROP oauth_client_secret)" >/dev/null
# "before any request" is counted: the fixture logs every request it gets.
_irs_obefore="$(wc -l < "$OLOG")"
check "a half OAuth2 credential (id, no secret) is refused before any request (28000)" \
	"$(refusal "SELECT pgcolumnar.iceberg_rest_table_location('ocat','db','events')" "incomplete OAuth2 credential")|$(( $(wc -l < "$OLOG") - _irs_obefore ))" \
	"28000|0"
# A static token does not excuse a half pair (#1794): the docs refuse a mapping
# that sets only one of the pair, with no condition on the token. Both halves are
# checked, then the mapping returns to id-only for the steps below.
q "ALTER USER MAPPING FOR postgres SERVER ocat OPTIONS (ADD token 'static-tok')" >/dev/null
_irs_obefore="$(wc -l < "$OLOG")"
check "a half OAuth2 credential (id, no secret) is refused even with a static token (28000)" \
	"$(refusal "SELECT pgcolumnar.iceberg_rest_table_location('ocat','db','events')" "incomplete OAuth2 credential")|$(( $(wc -l < "$OLOG") - _irs_obefore ))" \
	"28000|0"
q "ALTER USER MAPPING FOR postgres SERVER ocat OPTIONS (DROP oauth_client_id, ADD oauth_client_secret '$OCSEC')" >/dev/null
_irs_obefore="$(wc -l < "$OLOG")"
check "a half OAuth2 credential (secret, no id) is refused even with a static token (28000)" \
	"$(refusal "SELECT pgcolumnar.iceberg_rest_table_location('ocat','db','events')" "incomplete OAuth2 credential")|$(( $(wc -l < "$OLOG") - _irs_obefore ))" \
	"28000|0"
q "ALTER USER MAPPING FOR postgres SERVER ocat OPTIONS (DROP token, DROP oauth_client_secret, ADD oauth_client_id '$OCID')" >/dev/null
check "oauth options are accepted on a USER MAPPING (no error)" \
	"$(sqlstate_of "ALTER USER MAPPING FOR postgres SERVER ocat OPTIONS (ADD oauth_scope 'catalog')")" \
	"ok"
# A token wins over a complete pair, and no bearer is minted. The secret here is
# wrong, so a mint would be refused by the token endpoint; the catalog takes only
# the bearer this fixture mints, given here as the static token. So "no error"
# means the static token was sent, and the count is the requests to the token
# endpoint. With the token ignored, the mint runs: 28000 and 1 request.
q "ALTER USER MAPPING FOR postgres SERVER ocat OPTIONS (ADD token '$OATOKEN', ADD oauth_client_secret 'wrong-secret')" >/dev/null
_irs_obefore="$(wc -l < "$OLOG")"
_irs_tw="$(refusal "SELECT pgcolumnar.iceberg_rest_table_location('ocat','db','events')" "@@ no refusal is expected @@")"
check "a static token and a complete OAuth2 pair read by the token, with no token request" \
	"$_irs_tw|$(tail -n +$(( _irs_obefore + 1 )) "$OLOG" | grep -c 'POST /v1/oauth/tokens')" \
	"no error|0"
q "ALTER USER MAPPING FOR postgres SERVER ocat OPTIONS (DROP token, DROP oauth_client_secret)" >/dev/null
check "an oauth option on the SERVER is rejected (HV00D)" \
	"$(sqlstate_of "CREATE SERVER bad2 FOREIGN DATA WRAPPER pgcolumnar_iceberg_catalog OPTIONS (catalog_uri '$OCAT', oauth_client_secret 'x')")" \
	"HV00D"

# ---- SSRF: a hostile oauth_token_uri is refused by the endpoint allow-list ---
# oauth_token_uri is user-controlled, and the mint POST rides the same guarded
# transport as every catalog request, so a link-local (cloud-metadata) target is
# refused BEFORE any request leaves the backend, not followed. The allow-list is
# 127.0.0.1 here, so 169.254.169.254 is off-list AND link-local; the refusal is
# 42501 (as objstore_allowlist.sh asserts for read_parquet). A complete OAuth
# credential is restored first so the resolver reaches the mint, not the earlier
# half-credential guard.
q "ALTER USER MAPPING FOR postgres SERVER ocat OPTIONS (ADD oauth_client_secret '$OCSEC')" >/dev/null 2>&1
q "ALTER USER MAPPING FOR postgres SERVER ocat OPTIONS (ADD oauth_token_uri 'http://169.254.169.254/v1/oauth/tokens')" >/dev/null
check "a link-local oauth_token_uri is refused by the allow-list (42501)" \
	"$(refusal "SELECT pgcolumnar.iceberg_rest_table_location('ocat','db','events')" "is not in pgcolumnar.objstore_allowed_endpoints")" \
	"42501"
q "ALTER USER MAPPING FOR postgres SERVER ocat OPTIONS (SET oauth_token_uri 'http://10.11.12.13/v1/oauth/tokens')" >/dev/null
check "an off-allow-list oauth_token_uri host is refused (42501)" \
	"$(refusal "SELECT pgcolumnar.iceberg_rest_table_location('ocat','db','events')" "is not in pgcolumnar.objstore_allowed_endpoints")" \
	"42501"
kill "$OSRV_PID" 2>/dev/null

# ---- the URI form still works when the env token is present ------------------
pg_restart_env "PGCOLUMNAR_ICEBERG_REST_TOKEN='$TOKEN'"
q "ALTER SYSTEM SET pgcolumnar.objstore_allowed_endpoints='127.0.0.1'" >/dev/null
q "SELECT pg_reload_conf()" >/dev/null
await_up
check "the catalog-URI form still works with the env token" \
	"$(q "SELECT pgcolumnar.iceberg_rest_table_location('$CAT','db','events')")" \
	"$MDLOC"

# ---- a non-superuser does not borrow the server's environment token (#1341 items 9, 14)
# The environment token is the postmaster's ambient identity: only a superuser, or a
# mapping a superuser marked credentials_required 'false', may use it. ice_r holds
# every other grant the call needs, so the missing token mapping is all that refuses
# it, and the refusal comes before any request reaches the catalog.
q "CREATE ROLE ice_r LOGIN NOSUPERUSER; GRANT pg_read_server_files TO ice_r;
   GRANT USAGE ON SCHEMA pgcolumnar TO ice_r; GRANT USAGE ON FOREIGN SERVER cat TO ice_r;" >/dev/null
as_ice_r() {	# as_ice_r SQL -> the single value, or the SQLSTATE it fails with, or noconnect
	local out rc st
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U ice_r -d "$PGC_DB" \
		-X -qtA -v ON_ERROR_STOP=1 -v VERBOSITY=sqlstate -c "$1" 2>&1)"
	rc=$?
	st="$(sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\).*/\1/p' <<<"$out" | head -1)"
	if [ -n "$st" ]; then echo "$st"
	elif [ "$rc" = 0 ]; then v="$(printf '%s\n' "$out" | tail -1)"; echo "${v:-ok}"
	else echo noconnect; fi
}
before="$(wc -l < "$REST_LOG")"
check "a non-superuser with no token mapping is refused (28000) despite the environment token, before any request" \
	"$(as_ice_r "SELECT pgcolumnar.iceberg_rest_table_location('cat','db','events')")|$(( $(wc -l < "$REST_LOG") - before ))" \
	"28000|0"
q "CREATE USER MAPPING FOR ice_r SERVER cat" >/dev/null
check "and it cannot mark its own mapping credentials_required false (42501)" \
	"$(as_ice_r "ALTER USER MAPPING FOR ice_r SERVER cat OPTIONS (ADD credentials_required 'false')")" "42501"
q "ALTER USER MAPPING FOR ice_r SERVER cat OPTIONS (ADD credentials_required 'false')" >/dev/null
check "control: with credentials_required false set by a superuser, the same role reads by the environment token" \
	"$(as_ice_r "SELECT pgcolumnar.iceberg_rest_table_location('cat','db','events')")" "$MDLOC"

check "backend still up" "$(survived "$CRASHES_BEFORE")" "1"
pgc_summary
