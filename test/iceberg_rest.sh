#!/usr/bin/env bash
#
# pgColumnar Iceberg REST Catalog client, 7a (#388 phase 7). iceberg_scan reads
# a table given a metadata path; the REST client resolves a table NAMED by a
# catalog (catalog URI + namespace + table) over HTTP into its current metadata
# location, which the reader (already remote-capable since phase 6) then reads.
#
# 7a lands the transport (objstore ABI v5 generic http_request) and its first
# consumer, iceberg_rest_table_location(catalog, ns, table) -> the current
# metadata-location text. The proof runs against a hermetic REST fixture
# (test/iceberg_rest_server.py) that VERIFIES the Authorization: Bearer header,
# so a green resolve proves the C client and an independent server agree on the
# request. The bearer token is read from the SERVER PROCESS ENVIRONMENT
# (PGCOLUMNAR_ICEBERG_REST_TOKEN), never a SQL argument -- so it never lands in
# a log; the suite asserts exactly that.
#
# TLS is not re-proven here: http_request rides the same os_connect/
# os_tls_handshake path objstore_tls_read.sh already gates, unchanged.
#
# Usage:  test/iceberg_rest.sh [PG_CONFIG]

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

python3 -c 'import json' 2>/dev/null || pgc_skip python "python3 is needed"
MODDIR="$("$PGC_PG_CONFIG" --pkglibdir)"
[ -f "$MODDIR/pgcolumnar_objstore.so" ] \
	|| pgc_skip objstore "the object-store module is not installed"

TOKEN="s3cr3t-rest-token-do-not-log"
REST_PORT="$(pgc_pick_free_port "$PGC_AUX_PORT_LO" "$PGC_AUX_PORT_HI")"
REST_LOG="$PGC_WORKDIR/rest.log"
# 7a only returns the location text, so any URI serves; use a realistic one.
MDLOC="file:///tmp/pgc_ice_rest/db/events/metadata/00001.metadata.json"
SRV_PID=""

rest_teardown() {
	[ -n "$SRV_PID" ] && kill "$SRV_PID" 2>/dev/null
	pgc_teardown
}
trap rest_teardown EXIT INT TERM

# refusal_of SQL PHRASE LABEL -> "<SQLSTATE><LABEL>" when SQL raised with PHRASE in its
# message, the bare SQLSTATE when the message lacks it, or what happened instead. Two
# guards here raise 42501: with the allow-list refusing everything and the link-local
# guard gone, "a link-local catalog host is refused" passed on the allow-list's 42501,
# and with every address read as link-local and the allow-list gone, "a catalog
# endpoint off the allow-list is refused" passed on the link-local guard's. Each
# refusal now wants its own guard's message.
refusal_of() {
	local out state
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

# Backends killed by a signal, from the server log: a number, or nothing when the log
# can't be read. A fresh connection answers again after crash recovery, so "SELECT 1"
# alone passed with a backend killed on the unknown-table arm; survival also requires
# this count not to have moved.
crashes() {
	local _n
	_n="$(pgc_pg "grep -c 'terminated by signal' '$PGC_LOGFILE'" 2>/dev/null)"
	case "$_n" in '' | *[!0-9]*) ;; *) echo "$_n" ;; esac
}
survived() {	# survived BEFORE -> "1" when the server answers and no backend died since
	local _up _now
	_up="$(q 'SELECT 1')"; _now="$(crashes)"
	if ! [[ "$1" =~ ^[0-9]+$ && "$_now" =~ ^[0-9]+$ ]]; then echo "no (crash count unreadable)"
	elif [ "$_up" = 1 ] && [ "$_now" = "$1" ]; then echo 1
	else echo "no ([$_up], $(( _now - $1 )) backend crashes)"; fi
}

# restart the postmaster with a given environment prefix (the REST token lives
# in the server process environment, like the objstore AWS_* credentials).
pg_restart_env() {
	pgc_pg "pg_ctl -D '$PGC_PGDATA' stop -m fast -w" >/dev/null 2>&1
	pgc_pg "$* pg_ctl -D '$PGC_PGDATA' -l '$PGC_LOGFILE' start -w" >/dev/null 2>&1
	for _ in $(seq 1 30); do [ -n "$(q 'SELECT 1')" ] && return 0; sleep 0.5; done
	echo "FATAL: cluster did not come back after env restart"; exit 1
}

# ---- start the hermetic REST catalog, token-protected -----------------------
python3 "$(dirname "${BASH_SOURCE[0]}")/iceberg_rest_server.py" \
	--port "$REST_PORT" --log "$REST_LOG" --token "$TOKEN" \
	--namespace db --table events \
	--metadata-location "$MDLOC" \
	> "$PGC_WORKDIR/rest_server.out" 2>&1 &
SRV_PID=$!
for _ in $(seq 1 50); do grep -q READY "$PGC_WORKDIR/rest_server.out" 2>/dev/null && break; sleep 0.1; done
check "premise: rest fixture server is up" \
	"$(grep -c READY "$PGC_WORKDIR/rest_server.out" 2>/dev/null)" "1"

CAT="http://127.0.0.1:$REST_PORT"
CRASHES_BEFORE="$(crashes)"

# bring the token into the postmaster environment and allow the local endpoint
pg_restart_env "PGCOLUMNAR_ICEBERG_REST_TOKEN='$TOKEN'"
q "ALTER SYSTEM SET pgcolumnar.objstore_allowed_endpoints = '127.0.0.1'" >/dev/null
q "SELECT pg_reload_conf()" >/dev/null

# ---- the core resolve -------------------------------------------------------
check "loadTable resolves the current metadata-location" \
	"$(q "SELECT pgcolumnar.iceberg_rest_table_location('$CAT','db','events')")" \
	"$MDLOC"

# the request actually carried a bearer header (else the fixture would 401)
check "the loadTable request carried an Authorization header" \
	"$(grep -c 'GET /v1/namespaces/db/tables/events AUTH=yes' "$REST_LOG" 2>/dev/null)" "1"

# ---- the token never leaks --------------------------------------------------
# it is not a SQL argument, so it is absent from a log_statement='all' PG log,
# and the fixture records only AUTH=yes/no, never the value.
q "ALTER SYSTEM SET log_statement='all'" >/dev/null
q "SELECT pg_reload_conf()" >/dev/null
q "SELECT pgcolumnar.iceberg_rest_table_location('$CAT','db','events')" >/dev/null
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
_ir_loglines="$(grep -c . "$PGC_LOGFILE" 2>/dev/null)" || _ir_loglines=0
check "premise: the PG server log holds lines to search" \
	"$([ "${_ir_loglines:-0}" -gt 0 ] && echo yes || echo no)" "yes"
check "the token never appears in the server (PG) log" \
	"$(grep -c "$TOKEN" "$PGC_LOGFILE" 2>/dev/null)" "0"
check "the token value never appears in the catalog request log" \
	"$(grep -c "$TOKEN" "$REST_LOG" 2>/dev/null)" "0"

# ---- auth failures surface cleanly (28000) ----------------------------------
pg_restart_env "PGCOLUMNAR_ICEBERG_REST_TOKEN='wrong-token'"
q "ALTER SYSTEM SET pgcolumnar.objstore_allowed_endpoints = '127.0.0.1'" >/dev/null
q "SELECT pg_reload_conf()" >/dev/null
check "a wrong bearer token is refused (28000)" \
	"$(refusal_of "SELECT pgcolumnar.iceberg_rest_table_location('$CAT','db','events')" \
	   "the REST catalog refused the request" " by the catalog")" \
	"28000 by the catalog"

pg_restart_env ""   # no token in the environment at all
q "ALTER SYSTEM SET pgcolumnar.objstore_allowed_endpoints = '127.0.0.1'" >/dev/null
q "SELECT pg_reload_conf()" >/dev/null
check "an anonymous request to a token-protected catalog is refused (28000)" \
	"$(refusal_of "SELECT pgcolumnar.iceberg_rest_table_location('$CAT','db','events')" \
	   "the REST catalog refused the request" " by the catalog")" \
	"28000 by the catalog"

# restore the good token for the remaining arms
pg_restart_env "PGCOLUMNAR_ICEBERG_REST_TOKEN='$TOKEN'"

# ---- the allow-list is the SSRF boundary (42501) ----------------------------
check "a catalog endpoint off the allow-list is refused (42501)" \
	"$(refusal_of "SET pgcolumnar.objstore_allowed_endpoints='127.0.0.1';
	                SELECT pgcolumnar.iceberg_rest_table_location('http://127.0.0.2:$REST_PORT','db','events')" \
	   "is not in pgcolumnar.objstore_allowed_endpoints" " by the allow-list")" \
	"42501 by the allow-list"

# a link-local / instance-metadata catalog is refused even when allow-listed
check "a link-local catalog host is refused even if allow-listed (42501)" \
	"$(refusal_of "SET pgcolumnar.objstore_allowed_endpoints='169.254.169.254';
	                SELECT pgcolumnar.iceberg_rest_table_location('http://169.254.169.254/','db','events')" \
	   "resolves into a link-local range" " by the link-local guard")" \
	"42501 by the link-local guard"

# ---- a response beyond the cap is refused (54000) ---------------------------
check "an over-cap catalog response is refused (54000)" \
	"$(refusal_of "SET pgcolumnar.objstore_allowed_endpoints='127.0.0.1';
	                SELECT pgcolumnar.iceberg_rest_table_location('$CAT','db','toobig')" \
	   "exceeded" " by the cap")" \
	"54000 by the cap"

# ---- an unknown table surfaces the catalog 404 cleanly (42P01) --------------
check "an unknown table surfaces a clean not-found (42P01)" \
	"$(refusal_of "SET pgcolumnar.objstore_allowed_endpoints='127.0.0.1';
	                SELECT pgcolumnar.iceberg_rest_table_location('$CAT','db','nosuch')" \
	   "the REST catalog has no such table" " by the catalog's 404")" \
	"42P01 by the catalog's 404"

# ---- a non-object /v1/config body is refused, never walked (XX001) ----------
# an untrusted catalog whose config is a JSON array/scalar must not reach the
# object-only key lookup: a debug build would Assert-crash, a release build would
# read out of bounds. A second fixture serves "[]" for /v1/config.
BAD_PORT="$(pgc_pick_free_port "$PGC_AUX_PORT_LO" "$PGC_AUX_PORT_HI")"
python3 "$(dirname "${BASH_SOURCE[0]}")/iceberg_rest_server.py" \
	--port "$BAD_PORT" --log "$PGC_WORKDIR/badcfg.log" --bad-config \
	--metadata-location "$MDLOC" \
	> "$PGC_WORKDIR/badcfg.out" 2>&1 &
BAD_PID=$!
for _ in $(seq 1 50); do grep -q READY "$PGC_WORKDIR/badcfg.out" 2>/dev/null && break; sleep 0.1; done
check "a non-object catalog config is refused, not walked (XX001)" \
	"$(refusal_of "SET pgcolumnar.objstore_allowed_endpoints='127.0.0.1';
	                SELECT pgcolumnar.iceberg_rest_table_location('http://127.0.0.1:$BAD_PORT','db','events')" \
	   "config response is not a JSON object" " by the config guard")" \
	"XX001 by the config guard"
kill "$BAD_PID" 2>/dev/null

# ---- backend still healthy after the refusals -------------------------------
check "backend still up after the REST refusals" "$(survived "$CRASHES_BEFORE")" "1"

pgc_summary
