#!/usr/bin/env bash
#
# pgColumnar Iceberg REST catalog read + listing, 7b (#388 phase 7). 7a resolved
# a table named by a REST catalog to its metadata-location; 7b reads it. The
# proof is the SAME ORACLE: iceberg_rest_scan(catalog, ns, table) must return the
# identical rows as iceberg_scan(the metadata path the catalog reports), so the
# REST layer is proven correct by returning identical rows -- the catalog changes
# how the table is NAMED, never the rows. Plus namespace/table listing.
#
# The hermetic fixture (test/iceberg_rest_server.py) points loadTable at the
# committed warehouse, relocated into the work dir and served as a file:// URI,
# and verifies the Authorization: Bearer header. The token is read from the
# server environment (PGCOLUMNAR_ICEBERG_REST_TOKEN), never a SQL argument.
#
# Usage:  test/iceberg_rest_scan.sh [PG_CONFIG]

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

python3 -c 'import json' 2>/dev/null || pgc_skip python "python3 is needed"
MODDIR="$("$PGC_PG_CONFIG" --pkglibdir)"
[ -f "$MODDIR/pgcolumnar_objstore.so" ] \
	|| pgc_skip objstore "the object-store module is not installed"

FX="$(dirname "${BASH_SOURCE[0]}")/fixtures/iceberg"
WH="$FX/warehouse"
[ -f "$(ls "$WH"/db/events/data/*/*.parquet 2>/dev/null | head -1)" ] \
	|| pgc_skip fixture "iceberg warehouse data files are missing"

# relocate the warehouse into the work dir; that metadata.json is what the
# catalog will report as the table's current metadata-location.
DEST="$PGC_WORKDIR/wh"
rm -rf "$DEST"; mkdir -p "$DEST"
cp -r "$WH/db" "$DEST/db"
chmod -R u+rwX "$DEST"
MD="$(ls "$DEST"/db/events/metadata/*.metadata.json | sort | tail -1)"
MDLOC="file://$MD"

TOKEN="rest-scan-token-shh"
REST_PORT="$(pgc_pick_free_port "$PGC_AUX_PORT_LO" "$PGC_AUX_PORT_HI")"
REST_LOG="$PGC_WORKDIR/rest.log"
SRV_PID=""

rest_teardown() {
	[ -n "$SRV_PID" ] && kill "$SRV_PID" 2>/dev/null
	pgc_teardown
}
trap rest_teardown EXIT INT TERM

# Backends killed by a signal, from the server log: a number, or nothing when the log
# can't be read. A fresh connection answers again after crash recovery, so "SELECT 1"
# alone passed with the backend aborting on the unknown-table arm (probed 3s later);
# survival also requires this count not to have moved.
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

pg_restart_env() {
	pgc_pg "pg_ctl -D '$PGC_PGDATA' stop -m fast -w" >/dev/null 2>&1
	pgc_pg "$* pg_ctl -D '$PGC_PGDATA' -l '$PGC_LOGFILE' start -w" >/dev/null 2>&1
	for _ in $(seq 1 30); do [ -n "$(q 'SELECT 1')" ] && return 0; sleep 0.5; done
	echo "FATAL: cluster did not come back after env restart"; exit 1
}

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
pg_restart_env "PGCOLUMNAR_ICEBERG_REST_TOKEN='$TOKEN'"
q "ALTER SYSTEM SET pgcolumnar.objstore_allowed_endpoints = '127.0.0.1'" >/dev/null
q "SELECT pg_reload_conf()" >/dev/null

# ---- premise: the location the catalog reports is the relocated metadata -----
check "the catalog reports the relocated metadata location" \
	"$(q "SELECT pgcolumnar.iceberg_rest_table_location('$CAT','db','events')")" \
	"$MDLOC"

# ---- SAME ORACLE: REST read == direct iceberg_scan of that metadata ----------
DIRECT="$(q "SELECT id || '|' || region || '|' || amount
             FROM pgcolumnar.iceberg_scan('$MD') AS t(id bigint, region text, amount int)
             ORDER BY id")"
VIA_REST="$(q "SELECT id || '|' || region || '|' || amount
               FROM pgcolumnar.iceberg_rest_scan('$CAT','db','events') AS t(id bigint, region text, amount int)
               ORDER BY id")"
check "iceberg_rest_scan returns the five current-snapshot rows" \
	"$VIA_REST" "$(printf '1|eu|10\n2|eu|20\n3|us|30\n4|us|40\n5|us|50')"
check_text "iceberg_rest_scan matches iceberg_scan of the same table (same oracle)" \
	"$VIA_REST" "$DIRECT"

# ---- projection by field id flows through the REST path ----------------------
check "iceberg_rest_scan projects and reorders by field id (amount, id)" \
	"$(q "SELECT amount || '|' || id
	      FROM pgcolumnar.iceberg_rest_scan('$CAT','db','events') AS t(amount int, id bigint)
	      ORDER BY id")" \
	"$(printf '10|1\n20|2\n30|3\n40|4\n50|5')"

# ---- listing ----------------------------------------------------------------
check "iceberg_rest_namespaces lists the catalog's namespaces" \
	"$(q "SELECT string_agg(ns, ',' ORDER BY ns) FROM pgcolumnar.iceberg_rest_namespaces('$CAT') AS ns")" \
	"db"
check "iceberg_rest_tables lists a namespace's tables" \
	"$(q "SELECT string_agg(t, ',' ORDER BY t) FROM pgcolumnar.iceberg_rest_tables('$CAT','db') AS t")" \
	"events"

# ---- an unknown table surfaces the catalog 404 cleanly through the read ------
# refusal_of SQL PHRASE LABEL -> "<SQLSTATE><LABEL>" when SQL raised with PHRASE in its
# message, the bare SQLSTATE when the message lacks it, or what happened instead: the
# not-found must be the catalog's 404, not any 42P01.
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
check "iceberg_rest_scan of an unknown table is a clean not-found (42P01)" \
	"$(refusal_of "SELECT * FROM pgcolumnar.iceberg_rest_scan('$CAT','db','nosuch') AS t(id bigint)" \
	   "the REST catalog has no such table" " by the catalog's 404")" \
	"42P01 by the catalog's 404"

# ---- the namespace argument is the one asked for ---------------------------
# The catalog serves only "db". A client that ignored its namespace argument
# (sent "db" whatever it was given) passed every check above, because they all
# ask for "db". Asking for a namespace the catalog does not have must reach the
# catalog as that namespace and come back as its 404.
# The catalog's 404 says NoSuchNamespaceException here, so the error names a
# namespace (3F000), not a table (#1820).
check "iceberg_rest_tables of a namespace the catalog does not have is the catalog's 404" \
	"$(refusal_of "SELECT * FROM pgcolumnar.iceberg_rest_tables('$CAT','nosuchns')" \
	   "the REST catalog has no such namespace" " by the catalog's 404")" \
	"3F000 by the catalog's 404"
check "iceberg_rest_scan of a table in a namespace the catalog does not have is the catalog's 404" \
	"$(refusal_of "SELECT * FROM pgcolumnar.iceberg_rest_scan('$CAT','nosuchns','events') AS t(id bigint)" \
	   "the REST catalog has no such namespace" " by the catalog's 404")" \
	"3F000 by the catalog's 404"
# The catalog's own ErrorModel message reaches the error detail (#1820).
check "the catalog's 404 message reaches the error detail" \
	"$(refusal_of "SELECT * FROM pgcolumnar.iceberg_rest_scan('$CAT','db','nosuch') AS t(id bigint)" \
	   "DETAIL:  The catalog said: no such table db.nosuch" " with the catalog's message")" \
	"42P01 with the catalog's message"

check "backend still up after the REST read" "$(survived "$CRASHES_BEFORE")" "1"

pgc_summary
