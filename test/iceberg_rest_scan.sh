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
FX_PIDS=()
PROMO_PID=""

rest_teardown() {
	[ -n "$SRV_PID" ] && kill "$SRV_PID" 2>/dev/null
	[ -n "$PROMO_PID" ] && kill "$PROMO_PID" 2>/dev/null
	local _p
	for _p in "${FX_PIDS[@]}"; do kill "$_p" 2>/dev/null; done
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

# ---- a promoted table reads through the REST path (#1789) --------------------
# warehouse_promo's mixed snapshot: two files written before an int -> long,
# float -> double and decimal(10,2) -> decimal(20,2) promotion and one after.
# A second fixture server names it db.promo; every file reads as the current
# schema's types.
DESTP="$PGC_WORKDIR/promo"
rm -rf "$DESTP"; mkdir -p "$DESTP"
cp -r "$FX/warehouse_promo/db" "$DESTP/db"
chmod -R u+rwX "$DESTP"
PROMO_PORT="$(pgc_pick_free_port "$PGC_AUX_PORT_LO" "$PGC_AUX_PORT_HI")"
python3 "$(dirname "${BASH_SOURCE[0]}")/iceberg_rest_server.py" \
	--port "$PROMO_PORT" --log "$PGC_WORKDIR/rest_promo.log" --token "$TOKEN" \
	--namespace db --table promo \
	--metadata-location "file://$DESTP/db/t/metadata/mixed.metadata.json" \
	> "$PGC_WORKDIR/rest_promo.out" 2>&1 &
PROMO_PID=$!
for _ in $(seq 1 50); do grep -q READY "$PGC_WORKDIR/rest_promo.out" 2>/dev/null && break; sleep 0.1; done
check "premise: the promoted-table rest fixture server is up" \
	"$(grep -c READY "$PGC_WORKDIR/rest_promo.out" 2>/dev/null)" "1"
check_text "iceberg_rest_scan reads a promoted table's old and new files as the promoted types" \
	"$(q "SELECT id || '|' || k || '|' || f || '|' || d
	      FROM pgcolumnar.iceberg_rest_scan('http://127.0.0.1:$PROMO_PORT','db','promo')
	        AS t(id bigint, k bigint, f double precision, d numeric(20,2)) ORDER BY id")" \
	'1|-2147483648|1.5|1.25
2|-1|-2.25|-3.50
3|0|0.5|0.00
4|2|1024.75|99999999.99
5|2147483647|3.4028234663852886e+38|-0.01
6|7|-0.125|10.10
7|2|2|2.00
8|5000000000|3.25|123456789012345678.90
9|2|0.1|-1.00'
kill "$PROMO_PID" 2>/dev/null

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

# detail_of SQL TEXT -> "<SQLSTATE> with the catalog's message" when SQL raised with
# TEXT in its DETAIL line, else "<SQLSTATE> (detail: <the DETAIL line>)", so a
# failure shows what the detail held. Every non-200 branch adds the detail (#1820).
detail_of() {
	local out state detail
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -qtA 2>&1 <<SQLEOF
\\set VERBOSITY verbose
$1;
SQLEOF
)"
	state="$(sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\): .*/\1/p' <<<"$out" | head -1)"
	detail="$(sed -n 's/^DETAIL:  //p' <<<"$out" | head -1)"
	if [ -z "$state" ]; then echo "no error ($(head -1 <<<"$out"))"
	elif [ "$detail" = "$2" ]; then echo "$state with the catalog's message"
	else echo "$state (detail: $detail)"; fi
}
check "the catalog's namespace 404 message reaches the error detail" \
	"$(detail_of "SELECT * FROM pgcolumnar.iceberg_rest_tables('$CAT','nosuchns')" \
	   "The catalog said: no such namespace nosuchns")" \
	"3F000 with the catalog's message"
check "the catalog's 403 message reaches the error detail" \
	"$(detail_of "SELECT * FROM pgcolumnar.iceberg_rest_scan('$CAT','db','forbidden') AS t(id bigint)" \
	   "The catalog said: no read access to db.forbidden")" \
	"28000 with the catalog's message"
check "the catalog's 500 message reaches the error detail" \
	"$(detail_of "SELECT * FROM pgcolumnar.iceberg_rest_scan('$CAT','db','failing') AS t(id bigint)" \
	   "The catalog said: catalog backend unavailable")" \
	"08P01 with the catalog's message"
# An error body nested too deep to parse runs the parser out of stack. That is
# not a malformed body, and the client must not drop it as one (#1820 review).
check "a catalog error body too deep to parse raises its stack error (54001)" \
	"$(refusal_of "SELECT * FROM pgcolumnar.iceberg_rest_scan('$CAT','db','deepjson') AS t(id bigint)" \
	   "stack depth limit exceeded" " by the parser")" \
	"54001 by the parser"
# A body that starts with "{" and is cut off passes the client's pre-check, so
# only the JSON parse rejects it. That parse error (22P02) is about the body:
# the client drops it and reports the catalog's status (#1820 review).
check "a malformed catalog error body still reports the status (08P01)" \
	"$(refusal_of "SELECT * FROM pgcolumnar.iceberg_rest_scan('$CAT','db','badjson') AS t(id bigint)" \
	   "the REST catalog returned HTTP 500 for the table" " with the status")" \
	"08P01 with the status"

# ---- #1793: nested namespaces, prefixes, the warehouse, paging -------------
# Each scenario gets its own fixture, so a failure names one catalog behaviour.
# rows_of SQL -> the rows, or the first ERROR line, so a failed check shows what
# the catalog answered (q hides the error).
rows_of() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -qAt -c "$1" 2>&1 | grep -v '^$' | sed -n '/^ERROR:/{p;q};p'
}
# start_fx NAME ARGS... -> sets FX_PORT; the fixture is killed at teardown.
start_fx() {
	local name="$1"; shift
	FX_PORT="$(pgc_pick_free_port "$PGC_AUX_PORT_LO" "$PGC_AUX_PORT_HI")"
	python3 "$(dirname "${BASH_SOURCE[0]}")/iceberg_rest_server.py" \
		--port "$FX_PORT" --log "$PGC_WORKDIR/$name.log" --token "$TOKEN" \
		--metadata-location "$MDLOC" "$@" > "$PGC_WORKDIR/$name.out" 2>&1 &
	FX_PIDS+=("$!")
	for _ in $(seq 1 50); do grep -q READY "$PGC_WORKDIR/$name.out" 2>/dev/null && break; sleep 0.1; done
}
ROWS5="$(printf '1|eu|10\n2|eu|20\n3|us|30\n4|us|40\n5|us|50')"

# Nested namespaces. Like the Apache Iceberg 1.12 REST catalog, this fixture
# advertises namespace-separator %2E and answers 400 to a %1F path. It lists
# only the top level without ?parent=, and it pages every list one item at a
# time, even unasked. The table is db.x.events; db.x.y and aa have no tables.
start_fx nested --namespace db.x --table events --extra-table more \
	--extra-namespace db.x.y --extra-namespace aa \
	--namespace-separator %2E --page-size 1
NCAT="http://127.0.0.1:$FX_PORT"
check "nested namespaces: iceberg_rest_namespaces lists every level, page by page" \
	"$(rows_of "SELECT string_agg(ns, ',' ORDER BY ns) FROM pgcolumnar.iceberg_rest_namespaces('$NCAT') AS ns")" \
	"aa,db,db.x,db.x.y"
check "nested namespaces: iceberg_rest_tables of db.x lists both pages" \
	"$(rows_of "SELECT string_agg(t, ',' ORDER BY t) FROM pgcolumnar.iceberg_rest_tables('$NCAT','db.x') AS t")" \
	"events,more"
check "nested namespaces: iceberg_rest_scan of db.x.events returns the five rows" \
	"$(rows_of "SELECT id || '|' || region || '|' || amount
	            FROM pgcolumnar.iceberg_rest_scan('$NCAT','db.x','events') AS t(id bigint, region text, amount int)
	            ORDER BY id")" \
	"$ROWS5"

# A prefix with a '/' in it. The client must send it as path segments
# (catalogs/main), not as one encoded segment (catalogs%2Fmain).
start_fx slashpfx --namespace db --table events --prefix catalogs/main
SCAT="http://127.0.0.1:$FX_PORT"
check "multi-segment prefix: iceberg_rest_table_location resolves the table" \
	"$(rows_of "SELECT pgcolumnar.iceberg_rest_table_location('$SCAT','db','events')")" \
	"$MDLOC"
check "multi-segment prefix: iceberg_rest_scan returns the five rows" \
	"$(rows_of "SELECT id || '|' || region || '|' || amount
	            FROM pgcolumnar.iceberg_rest_scan('$SCAT','db','events') AS t(id bigint, region text, amount int)
	            ORDER BY id")" \
	"$ROWS5"
check "multi-segment prefix: iceberg_rest_namespaces lists the namespaces" \
	"$(rows_of "SELECT string_agg(ns, ',' ORDER BY ns) FROM pgcolumnar.iceberg_rest_namespaces('$SCAT') AS ns")" \
	"db"
check "multi-segment prefix: iceberg_rest_tables lists the tables" \
	"$(rows_of "SELECT string_agg(t, ',' ORDER BY t) FROM pgcolumnar.iceberg_rest_tables('$SCAT','db') AS t")" \
	"events"

# A prefix in the config "defaults", with no override.
start_fx defpfx --namespace db --table events --prefix wh1 --prefix-in-defaults
DCAT="http://127.0.0.1:$FX_PORT"
check "prefix in config defaults: iceberg_rest_table_location resolves the table" \
	"$(rows_of "SELECT pgcolumnar.iceberg_rest_table_location('$DCAT','db','events')")" \
	"$MDLOC"
check "prefix in config defaults: iceberg_rest_tables lists the tables" \
	"$(rows_of "SELECT string_agg(t, ',' ORDER BY t) FROM pgcolumnar.iceberg_rest_tables('$DCAT','db') AS t")" \
	"events"

# A FOREIGN SERVER with a warehouse. The catalog refuses /v1/config without
# ?warehouse=wh1 and serves its tables under the prefix whp.
start_fx whcat --namespace db --table events --prefix whp --warehouse wh1
q "CREATE SERVER irs_wh FOREIGN DATA WRAPPER pgcolumnar_iceberg_catalog
   OPTIONS (catalog_uri 'http://127.0.0.1:$FX_PORT', warehouse 'wh1')" >/dev/null
q "CREATE USER MAPPING FOR postgres SERVER irs_wh OPTIONS (token '$TOKEN')" >/dev/null
check "warehouse server: iceberg_rest_namespaces sends the warehouse and lists" \
	"$(rows_of "SELECT string_agg(ns, ',' ORDER BY ns) FROM pgcolumnar.iceberg_rest_namespaces('irs_wh') AS ns")" \
	"db"
check "warehouse server: iceberg_rest_tables sends the warehouse and lists" \
	"$(rows_of "SELECT string_agg(t, ',' ORDER BY t) FROM pgcolumnar.iceberg_rest_tables('irs_wh','db') AS t")" \
	"events"

# ---- #1859 review: catalogs that bend the spec, and the defensive refusals --
# A catalog whose top level lists every namespace, nested ones too. The walk
# into a then finds a.b again; a.b must still come out once.
start_fx flat --namespace a.b --table events --extra-namespace c --top-level-all
check "top level lists nested namespaces: iceberg_rest_namespaces lists each once" \
	"$(rows_of "SELECT string_agg(ns, ',' ORDER BY ns) FROM pgcolumnar.iceberg_rest_namespaces('http://127.0.0.1:$FX_PORT') AS ns")" \
	"a,a.b,c"
# The same catalog, and it also answers ?parent= as if it were absent.
start_fx flatig --namespace a.b --table events --extra-namespace c --top-level-all --ignore-parent
check "catalog ignores ?parent=: iceberg_rest_namespaces lists each once" \
	"$(rows_of "SELECT string_agg(ns, ',' ORDER BY ns) FROM pgcolumnar.iceberg_rest_namespaces('http://127.0.0.1:$FX_PORT') AS ns")" \
	"a,a.b,c"
# A catalog that answers 400 to ?parent=. The walk needs that answer, so the
# call fails with the catalog's status (docs/sql-reference.md says so).
start_fx parent400 --namespace a --table events --parent-status 400
check "catalog refuses ?parent=: iceberg_rest_namespaces fails with its HTTP 400" \
	"$(refusal_of "SELECT * FROM pgcolumnar.iceberg_rest_namespaces('http://127.0.0.1:$FX_PORT')" \
	   "the REST catalog returned HTTP 400 for the namespaces" " with the catalog's 400")" \
	"08P01 with the catalog's 400"
# Config precedence: overrides, then the top-level key, then defaults. Each
# fixture serves its tables only under the prefix that must win.
start_fx prec3 --namespace db --table events --prefix ovr \
	--config-json '{"defaults":{"prefix":"dft"},"prefix":"top","overrides":{"prefix":"ovr"}}'
check "config precedence: a prefix in overrides wins over the top-level key and defaults" \
	"$(rows_of "SELECT pgcolumnar.iceberg_rest_table_location('http://127.0.0.1:$FX_PORT','db','events')")" \
	"$MDLOC"
start_fx prec2 --namespace db --table events --prefix top \
	--config-json '{"defaults":{"prefix":"dft"},"prefix":"top","overrides":{}}'
check "config precedence: a top-level prefix wins over defaults" \
	"$(rows_of "SELECT pgcolumnar.iceberg_rest_table_location('http://127.0.0.1:$FX_PORT','db','events')")" \
	"$MDLOC"
start_fx badsep --namespace db --table events \
	--config-json '{"defaults":{},"overrides":{"namespace-separator":"%G1"}}'
check "an invalid namespace-separator %G1 is refused" \
	"$(refusal_of "SELECT pgcolumnar.iceberg_rest_table_location('http://127.0.0.1:$FX_PORT','db','events')" \
	   'advertised an invalid namespace-separator "%G1"' " invalid separator")" \
	"XX001 invalid separator"
start_fx badpfx --namespace db --table events \
	--config-json '{"defaults":{},"overrides":{"prefix":"a/../b"}}'
check "an invalid prefix a/../b is refused" \
	"$(refusal_of "SELECT pgcolumnar.iceberg_rest_table_location('http://127.0.0.1:$FX_PORT','db','events')" \
	   'advertised an invalid prefix "a/../b"' " invalid prefix")" \
	"XX001 invalid prefix"
# The walk bound: 64 levels are listed, a 65th is refused.
DEEP64="$(seq -s . -f 'l%g' 1 64)"
start_fx deep64 --namespace db --table events --extra-namespace "$DEEP64"
check "namespace depth: a namespace 64 levels deep is listed" \
	"$(rows_of "SELECT max(length(ns) - length(replace(ns, '.', '')) + 1)
	            FROM pgcolumnar.iceberg_rest_namespaces('http://127.0.0.1:$FX_PORT') AS ns")" \
	"64"
start_fx deep65 --namespace db --table events --extra-namespace "$DEEP64.l65"
check "namespace depth: a namespace 65 levels deep is refused" \
	"$(refusal_of "SELECT * FROM pgcolumnar.iceberg_rest_namespaces('http://127.0.0.1:$FX_PORT')" \
	   "nests namespaces deeper than 64 levels" " deeper than 64")" \
	"54000 deeper than 64"
# Page tokens that never end. The statement timeout stops a client that walks
# to the page bound, so a failure shows 57014, not a hung suite.
start_fx samepage --namespace db --table events --bad-page-token same
check "paging: a page token repeated at once is refused" \
	"$(refusal_of "SET statement_timeout = '30s'; SELECT * FROM pgcolumnar.iceberg_rest_tables('http://127.0.0.1:$FX_PORT','db')" \
	   "returned the same page token twice for the table list" " repeated token")" \
	"08P01 repeated token"
start_fx cyclepage --namespace db --table events --bad-page-token cycle
check "paging: a page token cycle A, B, A is refused" \
	"$(refusal_of "SET statement_timeout = '30s'; SELECT * FROM pgcolumnar.iceberg_rest_tables('http://127.0.0.1:$FX_PORT','db')" \
	   "returned the same page token twice for the table list" " repeated token")" \
	"08P01 repeated token"

# ---- #1865: a namespace level that holds a dot -----------------------------
# This catalog uses the default separator (%1F). Its table is in the namespace
# with the ONE level "db.x". It also has the two-level namespace db, x, which
# holds no table. The text form 'db.x' names db, x, as it always has; a text[]
# names each level as given, so ARRAY['db.x'] reaches the one level.
start_fx dotlevel --namespace-levels '["db.x"]' --table events --extra-namespace db.x
DOTCAT="http://127.0.0.1:$FX_PORT"
check "dotted level: iceberg_rest_namespace_levels lists the level db.x apart from db, x" \
	"$(rows_of "SELECT string_agg(array_to_json(ns)::text, ' ' ORDER BY array_to_json(ns)::text COLLATE \"C\")
	            FROM pgcolumnar.iceberg_rest_namespace_levels('$DOTCAT') AS ns")" \
	'["db","x"] ["db"] ["db.x"]'
check "dotted level: iceberg_rest_namespaces still dot-joins the levels" \
	"$(rows_of "SELECT string_agg(ns, ' ' ORDER BY ns COLLATE \"C\") FROM pgcolumnar.iceberg_rest_namespaces('$DOTCAT') AS ns")" \
	"db db.x db.x"
check "dotted level: iceberg_rest_tables of ARRAY['db.x'] lists its tables" \
	"$(rows_of "SELECT string_agg(t, ',' ORDER BY t) FROM pgcolumnar.iceberg_rest_tables('$DOTCAT', ARRAY['db.x']) AS t")" \
	"events"
check "dotted level: iceberg_rest_table_location of ARRAY['db.x'] resolves the table" \
	"$(rows_of "SELECT pgcolumnar.iceberg_rest_table_location('$DOTCAT', ARRAY['db.x'], 'events')")" \
	"$MDLOC"
check "dotted level: iceberg_rest_scan of ARRAY['db.x'] returns the five rows" \
	"$(rows_of "SELECT id || '|' || region || '|' || amount
	            FROM pgcolumnar.iceberg_rest_scan('$DOTCAT', ARRAY['db.x'], 'events') AS t(id bigint, region text, amount int)
	            ORDER BY id")" \
	"$ROWS5"
check "dotted level: a listed level array passes back to iceberg_rest_tables" \
	"$(rows_of "SELECT string_agg(array_to_json(ns)::text || '=' || t, ' ' ORDER BY t)
	            FROM pgcolumnar.iceberg_rest_namespace_levels('$DOTCAT') AS ns,
	                 LATERAL pgcolumnar.iceberg_rest_tables('$DOTCAT', ns) AS t
	            WHERE ns = ARRAY['db.x']")" \
	'["db.x"]=events'
check "dotted level: the text form 'db.x' still names the two levels db, x" \
	"$(refusal_of "SELECT * FROM pgcolumnar.iceberg_rest_tables('$DOTCAT','db.x')" \
	   "no such namespace" " for db, x")" \
	"3F000 for db, x"
# On the nested catalog (separator %2E), ARRAY['db','x'] is the same
# namespace as 'db.x'. A level that holds a dot cannot be sent there: the
# catalog would read the dot as a separator, so the client refuses it.
check "text[] form: ARRAY['db','x'] names the same namespace as 'db.x'" \
	"$(rows_of "SELECT string_agg(t, ',' ORDER BY t) FROM pgcolumnar.iceberg_rest_tables('$NCAT', ARRAY['db','x']) AS t")" \
	"events,more"
check "text[] form: a level that holds the catalog's separator is refused" \
	"$(refusal_of "SELECT * FROM pgcolumnar.iceberg_rest_tables('$NCAT', ARRAY['db.x'])" \
	   'the namespace level "db.x" contains the catalog'"'"'s namespace separator' " with the level named")" \
	"22023 with the level named"
# A catalog (separator %2E) that lists the one level "db.x". No request can
# name it, so the walk lists it and does not ask for its children.
start_fx dotsep --namespace-levels '["db.x"]' --table events --namespace-separator %2E
check "text[] form: a listed level that holds the separator is listed, not walked" \
	"$(rows_of "SELECT string_agg(array_to_json(ns)::text, ' ') FROM pgcolumnar.iceberg_rest_namespace_levels('http://127.0.0.1:$FX_PORT') AS ns")" \
	'["db.x"]'
# docs/sql-reference.md cautions that the plain LATERAL example fails there.
check "text[] form: the unfiltered LATERAL example fails on a dot-separator catalog" \
	"$(refusal_of "SELECT ns, t FROM pgcolumnar.iceberg_rest_namespace_levels('http://127.0.0.1:$FX_PORT') AS ns,
	                 LATERAL pgcolumnar.iceberg_rest_tables('http://127.0.0.1:$FX_PORT', ns) AS t" \
	   'the namespace level "db.x" contains the catalog'"'"'s namespace separator' " on the dotted level")" \
	"22023 on the dotted level"
# docs/sql-reference.md: on a "." catalog the LATERAL example skips rows with a
# dotted level first, so the level db.x does not abort the query.
check "text[] form: the documented filter keeps the LATERAL example running on a dot-separator catalog" \
	"$(rows_of "SELECT coalesce(string_agg(array_to_json(ns)::text || '=' || t, ' '), 'no rows')
	            FROM pgcolumnar.iceberg_rest_namespace_levels('http://127.0.0.1:$FX_PORT') AS ns,
	                 LATERAL pgcolumnar.iceberg_rest_tables('http://127.0.0.1:$FX_PORT', ns) AS t
	            WHERE NOT EXISTS (SELECT FROM unnest(ns) AS level WHERE level LIKE '%.%')")" \
	"no rows"
# docs/sql-reference.md: on a default-separator catalog the filter is
# strpos(level, chr(31)) > 0, so a listed level db<0x1F>x does not abort it.
start_fx uslevel --namespace-levels '["db\u001fx"]' --table events
check "text[] form: the documented filter keeps the LATERAL example running on a default-separator catalog" \
	"$(rows_of "SELECT coalesce(string_agg(array_to_json(ns)::text || '=' || t, ' '), 'no rows')
	            FROM pgcolumnar.iceberg_rest_namespace_levels('http://127.0.0.1:$FX_PORT') AS ns,
	                 LATERAL pgcolumnar.iceberg_rest_tables('http://127.0.0.1:$FX_PORT', ns) AS t
	            WHERE NOT EXISTS (SELECT FROM unnest(ns) AS level WHERE strpos(level, chr(31)) > 0)")" \
	"no rows"
# The refusal message names the level. A control byte in it (0x1F is the
# default separator) is shown escaped, not sent raw to the log and client.
check "separator refusal: a control byte in the refused level is escaped in the message" \
	"$(rows_of "SELECT * FROM pgcolumnar.iceberg_rest_tables('$DOTCAT', E'db\\x1fx')" | cat -v |
	   sed -e 's/.*the namespace level "\(.*\)" contains.*/\1/')" \
	'db\x1Fx'
# A catalog whose separator is the one byte 0xC3. That byte starts the UTF-8
# character "é" (C3 A9), but "é" does not hold the separator: the catalog
# reads characters, not bytes. The level must reach the catalog.
start_fx hisep --namespace-levels '["\u00e9"]' --table events --namespace-separator %C3
check "separator refusal: a separator byte above 0x7F does not match inside a UTF-8 character" \
	"$(rows_of "SELECT string_agg(t, ',' ORDER BY t) FROM pgcolumnar.iceberg_rest_tables('http://127.0.0.1:$FX_PORT', ARRAY[E'\\xc3\\xa9']) AS t")" \
	"events"
# A level is text in the database encoding, which need not be UTF-8. In a
# LATIN1 database "é" is the one byte E9, and the dot after it is a whole
# character the catalog (separator %2E) would split on, so it is refused.
# The client reads UTF-8, so the refusal message names the level as é.x.
q "CREATE DATABASE latin1db ENCODING 'LATIN1' LOCALE 'C' TEMPLATE template0" >/dev/null
PGC_DB=latin1db rows_of "CREATE EXTENSION pgcolumnar" >/dev/null
check "separator refusal: in a LATIN1 database the dot after the byte E9 is refused" \
	"$(PGC_DB=latin1db PGCLIENTENCODING=UTF8 refusal_of "SELECT * FROM pgcolumnar.iceberg_rest_tables('$NCAT', ARRAY[E'\\xe9.x'])" \
	   'contains the catalog'"'"'s namespace separator' " with the level named")" \
	"22023 with the level named"
# Every control byte in a refused level is escaped, not 0x1F alone.
check "separator refusal: a newline in the refused level is escaped in the message" \
	"$(rows_of "SELECT * FROM pgcolumnar.iceberg_rest_tables('$DOTCAT', E'db\\x1f\\nx')" | cat -v |
	   sed -e 's/.*the namespace level "\(.*\)" contains.*/\1/')" \
	'db\x1F\x0Ax'
check "text[] form: an empty namespace array is refused" \
	"$(refusal_of "SELECT * FROM pgcolumnar.iceberg_rest_tables('$DOTCAT', ARRAY[]::text[])" \
	   "a namespace array must hold at least one level" " as empty")" \
	"22023 as empty"
check "text[] form: a NULL level is refused" \
	"$(refusal_of "SELECT * FROM pgcolumnar.iceberg_rest_tables('$DOTCAT', ARRAY['db',NULL])" \
	   "a namespace level must not be NULL" " as NULL")" \
	"22004 as NULL"
check "text[] form: a two-dimensional namespace array is refused" \
	"$(refusal_of "SELECT * FROM pgcolumnar.iceberg_rest_tables('$DOTCAT', ARRAY[['db'],['x']])" \
	   "a namespace array must have one dimension" " as 2-D")" \
	"22023 as 2-D"
# ---- #1891: an empty namespace level ---------------------------------------
# The Iceberg REST catalog accepts a namespace with an empty level, so the
# client sends one as given. When the catalog has no such namespace, its 404
# carries a hint that names the empty level. A namespace whose only level is
# empty ('' or ARRAY['']) is refused before any request, with the same hint
# (#1922): its request path would hold an empty segment.
check "empty level: a missing 'db..x' gets a hint naming level 2" \
	"$(refusal_of "SELECT * FROM pgcolumnar.iceberg_rest_tables('$DOTCAT','db..x')" \
	   "Level 2 of 3 of the namespace is empty" " hint level 2")" \
	"3F000 hint level 2"
check "empty level: a missing 'db.' gets a hint naming level 2" \
	"$(refusal_of "SELECT * FROM pgcolumnar.iceberg_rest_tables('$DOTCAT','db.')" \
	   "Level 2 of 2 of the namespace is empty" " hint level 2")" \
	"3F000 hint level 2"
check "empty level: a missing '' gets a hint naming level 1" \
	"$(refusal_of "SELECT * FROM pgcolumnar.iceberg_rest_tables('$DOTCAT','')" \
	   "Level 1 of 1 of the namespace is empty" " hint level 1")" \
	"22023 hint level 1"
check "empty level: a missing ARRAY['db',''] gets a hint naming level 2" \
	"$(refusal_of "SELECT * FROM pgcolumnar.iceberg_rest_tables('$DOTCAT', ARRAY['db',''])" \
	   "Level 2 of 2 of the namespace is empty" " hint level 2")" \
	"3F000 hint level 2"
check "empty level: iceberg_rest_scan of a missing ARRAY[''] gets a hint naming level 1" \
	"$(refusal_of "SELECT * FROM pgcolumnar.iceberg_rest_scan('$DOTCAT', ARRAY[''], 'events') AS t(id bigint)" \
	   "Level 1 of 1 of the namespace is empty" " hint level 1")" \
	"22023 hint level 1"
check "empty level: iceberg_rest_table_location of a missing '.db' gets a hint naming level 1" \
	"$(refusal_of "SELECT pgcolumnar.iceberg_rest_table_location('$DOTCAT', '.db', 'events')" \
	   "Level 1 of 2 of the namespace is empty" " hint level 1")" \
	"3F000 hint level 1"
check "empty level: a missing namespace with no empty level gets no empty-level hint" \
	"$(refusal_of "SELECT * FROM pgcolumnar.iceberg_rest_tables('$DOTCAT','db.y')" \
	   "of the namespace is empty" " with the hint")" \
	"3F000"
# This catalog (separator %1F) keeps its table in the namespace db, '' (an
# empty second level), as the Apache Iceberg REST catalog allows. The client
# must reach it in every form, and a listed row must pass back unchanged.
# The catalog also serves db, '', x, a child of db, '' (#1939), and
# db, '', x, y below it, so the walk must name an empty level that sits
# between two others in ?parent=.
start_fx emptylevel --namespace-levels '["db",""]' --table events --extra-namespace 'db..x.y'
ECAT="http://127.0.0.1:$FX_PORT"
check "empty level: iceberg_rest_namespace_levels lists the namespace db, ''" \
	"$(rows_of "SELECT string_agg(array_to_json(ns)::text, ' ' ORDER BY array_to_json(ns)::text COLLATE \"C\")
	            FROM pgcolumnar.iceberg_rest_namespace_levels('$ECAT') AS ns")" \
	'["db","","x","y"] ["db","","x"] ["db",""] ["db"]'
# The walk must go on below db, '': an empty level among other levels can be
# named in ?parent=, so its children are listed (#1939).
check "empty level: iceberg_rest_namespace_levels lists the child db, '', x of db, ''" \
	"$(rows_of "SELECT count(*) FILTER (WHERE ns = ARRAY['db','','x']) || ' of ' || count(*)
	            FROM pgcolumnar.iceberg_rest_namespace_levels('$ECAT') AS ns")" \
	"1 of 4"
# The walk must go on below db, '', x too: its ?parent= holds two separators
# in a row, for the empty level in the middle.
check "empty level: iceberg_rest_namespace_levels lists the child db, '', x, y of db, '', x" \
	"$(rows_of "SELECT count(*) FILTER (WHERE ns = ARRAY['db','','x','y']) || ' of ' || count(*)
	            FROM pgcolumnar.iceberg_rest_namespace_levels('$ECAT') AS ns")" \
	"1 of 4"
# The text form shares the walk, and must list the same four namespaces.
check "empty level: iceberg_rest_namespaces lists db, db., db..x and db..x.y" \
	"$(rows_of "SELECT string_agg(ns, ',' ORDER BY ns COLLATE \"C\")
	            FROM pgcolumnar.iceberg_rest_namespaces('$ECAT') AS ns")" \
	"db,db.,db..x,db..x.y"
check "empty level: iceberg_rest_tables of ARRAY['db',''] lists its tables" \
	"$(rows_of "SELECT string_agg(t, ',' ORDER BY t) FROM pgcolumnar.iceberg_rest_tables('$ECAT', ARRAY['db','']) AS t")" \
	"events"
check "empty level: iceberg_rest_tables of the text form 'db.' lists its tables" \
	"$(rows_of "SELECT string_agg(t, ',' ORDER BY t) FROM pgcolumnar.iceberg_rest_tables('$ECAT', 'db.') AS t")" \
	"events"
check "empty level: iceberg_rest_table_location of ARRAY['db',''] resolves the table" \
	"$(rows_of "SELECT pgcolumnar.iceberg_rest_table_location('$ECAT', ARRAY['db',''], 'events')")" \
	"$MDLOC"
check "empty level: iceberg_rest_scan of ARRAY['db',''] returns the five rows" \
	"$(rows_of "SELECT id || '|' || region || '|' || amount
	            FROM pgcolumnar.iceberg_rest_scan('$ECAT', ARRAY['db',''], 'events') AS t(id bigint, region text, amount int)
	            ORDER BY id")" \
	"$ROWS5"
check "empty level: a listed level array with an empty level passes back to iceberg_rest_tables" \
	"$(rows_of "SELECT string_agg(array_to_json(ns)::text || '=' || t, ' ' ORDER BY t)
	            FROM pgcolumnar.iceberg_rest_namespace_levels('$ECAT') AS ns,
	                 LATERAL pgcolumnar.iceberg_rest_tables('$ECAT', ns) AS t
	            WHERE ns = ARRAY['db','']")" \
	'["db",""]=events'
q "CREATE SERVER irs_empty FOREIGN DATA WRAPPER pgcolumnar_iceberg_catalog
   OPTIONS (catalog_uri '$ECAT')" >/dev/null
q "CREATE USER MAPPING FOR postgres SERVER irs_empty OPTIONS (token '$TOKEN')" >/dev/null
check "empty level: the server form of iceberg_rest_tables reaches ARRAY['db','']" \
	"$(rows_of "SELECT string_agg(t, ',' ORDER BY t) FROM pgcolumnar.iceberg_rest_tables('irs_empty', ARRAY['db','']) AS t")" \
	"events"
# A missing table in a namespace with an empty level is the other 404
# (42P01, no such table). It carries the same hint.
check "empty level: iceberg_rest_table_location of a missing table in ARRAY['db',''] gets a hint naming level 2" \
	"$(refusal_of "SELECT pgcolumnar.iceberg_rest_table_location('$ECAT', ARRAY['db',''], 'nosuch')" \
	   "Level 2 of 2 of the namespace is empty" " hint level 2")" \
	"42P01 hint level 2"
# ---- #1922: a namespace whose only level is empty -------------------------
# The namespace [""] makes the request path /v1/namespaces//tables. The Apache
# Iceberg REST catalog answers that path with HTTP 400, and a proxy may
# collapse "//" to "/". The REST spec has no other way to write it. So the
# client refuses it (22023) before any request names it. This fixture serves
# [""] and answers "//" as written, so only the client's refusal stops a call.
start_fx emptyonly --namespace-levels '[""]' --table events
OCAT="http://127.0.0.1:$FX_PORT"
OLOG="$PGC_WORKDIR/emptyonly.log"
# An empty ?parent= is a query value, not a path segment, so the walk still
# asks for the children of ARRAY['']. This fixture reads an empty parent as no
# parent, so the walk finds nothing below it.
check "empty only level: iceberg_rest_namespace_levels lists ARRAY[''] and asks for its children with an empty parent" \
	"$(rows_of "SELECT string_agg(array_to_json(ns)::text, ' ') FROM pgcolumnar.iceberg_rest_namespace_levels('$OCAT') AS ns") empty parent requests=$(grep -c '?parent= ' "$OLOG")" \
	'[""] empty parent requests=1'
check "empty only level: iceberg_rest_tables of ARRAY[''] is refused naming level 1" \
	"$(refusal_of "SELECT * FROM pgcolumnar.iceberg_rest_tables('$OCAT', ARRAY[''])" \
	   "Level 1 of 1 of the namespace is empty" " hint level 1")" \
	"22023 hint level 1"
check "empty only level: iceberg_rest_table_location of '' is refused naming level 1" \
	"$(refusal_of "SELECT pgcolumnar.iceberg_rest_table_location('$OCAT', '', 'events')" \
	   "Level 1 of 1 of the namespace is empty" " hint level 1")" \
	"22023 hint level 1"
check "empty only level: iceberg_rest_scan of '' is refused naming level 1" \
	"$(refusal_of "SELECT * FROM pgcolumnar.iceberg_rest_scan('$OCAT', '', 'events') AS t(id bigint)" \
	   "Level 1 of 1 of the namespace is empty" " hint level 1")" \
	"22023 hint level 1"
# docs/sql-reference.md: adding ns <> ARRAY[''] to the documented filter keeps
# the LATERAL example running on a catalog that lists {""}.
check "empty only level: the documented filter keeps the LATERAL example running" \
	"$(rows_of "SELECT coalesce(string_agg(array_to_json(ns)::text || '=' || t, ' '), 'no rows')
	            FROM pgcolumnar.iceberg_rest_namespace_levels('$OCAT') AS ns,
	                 LATERAL pgcolumnar.iceberg_rest_tables('$OCAT', ns) AS t
	            WHERE NOT EXISTS (SELECT FROM unnest(ns) AS level WHERE strpos(level, chr(31)) > 0)
	              AND ns <> ARRAY['']")" \
	"no rows"
# The Apache Iceberg REST catalog reads an empty ?parent= as the namespace
# ARRAY[''] (it answers 200, and 404 for an unknown parent). This fixture does
# too, and serves ARRAY['','x'], so the walk must list that child.
start_fx emptykids --namespace-levels '["","x"]' --table events --honour-empty-parent
KCAT="http://127.0.0.1:$FX_PORT"
KLOG="$PGC_WORKDIR/emptykids.log"
check "empty only level: iceberg_rest_namespace_levels lists the child of ARRAY[''] when the catalog reads an empty parent as ARRAY['']" \
	"$(rows_of "SELECT string_agg(array_to_json(ns)::text, ' ' ORDER BY ns) FROM pgcolumnar.iceberg_rest_namespace_levels('$KCAT') AS ns")" \
	'[""] ["","x"]'
check "empty only level: iceberg_rest_namespaces lists '' and its child '.x'" \
	"$(rows_of "SELECT string_agg(quote_literal(ns), ' ' ORDER BY ns COLLATE \"C\") FROM pgcolumnar.iceberg_rest_namespaces('$KCAT') AS ns")" \
	"'' '.x'"
check "empty only level: the documented filter lets the LATERAL example reach the child of ARRAY['']" \
	"$(rows_of "SELECT coalesce(string_agg(array_to_json(ns)::text || '=' || t, ' '), 'no rows')
	            FROM pgcolumnar.iceberg_rest_namespace_levels('$KCAT') AS ns,
	                 LATERAL pgcolumnar.iceberg_rest_tables('$KCAT', ns) AS t
	            WHERE NOT EXISTS (SELECT FROM unnest(ns) AS level WHERE strpos(level, chr(31)) > 0)
	              AND ns <> ARRAY['']")" \
	'["","x"]=events'
# The refusal needs no catalog, so it comes before any request: a catalog that
# cannot be reached still gives 22023, not a connection error.
check "empty only level: iceberg_rest_table_location of '' is refused when the catalog cannot be reached" \
	"$(refusal_of "SELECT pgcolumnar.iceberg_rest_table_location('http://127.0.0.1:1', '', 'events')" \
	   "Level 1 of 1 of the namespace is empty" " hint level 1")" \
	"22023 hint level 1"
check "empty only level: iceberg_rest_tables of ARRAY[''] is refused when the catalog cannot be reached" \
	"$(refusal_of "SELECT * FROM pgcolumnar.iceberg_rest_tables('http://127.0.0.1:1', ARRAY[''])" \
	   "Level 1 of 1 of the namespace is empty" " hint level 1")" \
	"22023 hint level 1"
# The server form with an OAuth client credential fetches a token before it
# reads the namespace, and that POST carries the client secret. The refusal
# must come first, so a catalog that cannot be reached still gives 22023.
q "CREATE SERVER irs_down FOREIGN DATA WRAPPER pgcolumnar_iceberg_catalog
   OPTIONS (catalog_uri 'http://127.0.0.1:1')" >/dev/null
q "CREATE USER MAPPING FOR CURRENT_USER SERVER irs_down
   OPTIONS (oauth_client_id 'a', oauth_client_secret 'b')" >/dev/null
check "empty only level: the OAuth server form of iceberg_rest_table_location of '' is refused before the token request" \
	"$(refusal_of "SELECT pgcolumnar.iceberg_rest_table_location('irs_down', '', 'events')" \
	   "Level 1 of 1 of the namespace is empty" " hint level 1")" \
	"22023 hint level 1"
check "empty only level: the OAuth server form of iceberg_rest_scan of '' is refused before the token request" \
	"$(refusal_of "SELECT * FROM pgcolumnar.iceberg_rest_scan('irs_down', '', 'events') AS t(id bigint)" \
	   "Level 1 of 1 of the namespace is empty" " hint level 1")" \
	"22023 hint level 1"
check "empty only level: no request reaches the catalog with an empty path segment" \
	"$(cat "$OLOG" "$KLOG" | grep -c '^GET [^ ?]*//')" \
	"0"
# ---- #1931: an empty table name ---------------------------------------------
# An empty table name makes the loadTable path /v1/namespaces/db/tables/, which
# ends in an empty segment. A proxy that strips the trailing slash would reach
# listTables instead. So the client refuses it (22023) before any request,
# including a server's OAuth token request. This fixture would answer the path.
start_fx emptytable --table events
TCAT="http://127.0.0.1:$FX_PORT"
TLOG="$PGC_WORKDIR/emptytable.log"
check "empty table name: iceberg_rest_table_location of '' is refused" \
	"$(refusal_of "SELECT pgcolumnar.iceberg_rest_table_location('$TCAT', 'db', '')" \
	   "the table name is empty" " as empty")" \
	"22023 as empty"
check "empty table name: iceberg_rest_scan of '' is refused" \
	"$(refusal_of "SELECT * FROM pgcolumnar.iceberg_rest_scan('$TCAT', 'db', '') AS t(id bigint)" \
	   "the table name is empty" " as empty")" \
	"22023 as empty"
check "empty table name: the text[] form of iceberg_rest_table_location of '' is refused" \
	"$(refusal_of "SELECT pgcolumnar.iceberg_rest_table_location('$TCAT', ARRAY['db'], '')" \
	   "the table name is empty" " as empty")" \
	"22023 as empty"
check "empty table name: the text[] form of iceberg_rest_scan of '' is refused" \
	"$(refusal_of "SELECT * FROM pgcolumnar.iceberg_rest_scan('$TCAT', ARRAY['db'], '') AS t(id bigint)" \
	   "the table name is empty" " as empty")" \
	"22023 as empty"
check "empty table name: iceberg_rest_table_location of '' is refused when the catalog cannot be reached" \
	"$(refusal_of "SELECT pgcolumnar.iceberg_rest_table_location('http://127.0.0.1:1', 'db', '')" \
	   "the table name is empty" " as empty")" \
	"22023 as empty"
check "empty table name: the OAuth server form of iceberg_rest_table_location of '' is refused before the token request" \
	"$(refusal_of "SELECT pgcolumnar.iceberg_rest_table_location('irs_down', 'db', '')" \
	   "the table name is empty" " as empty")" \
	"22023 as empty"
check "empty table name: the OAuth server form of iceberg_rest_scan of '' is refused before the token request" \
	"$(refusal_of "SELECT * FROM pgcolumnar.iceberg_rest_scan('irs_down', 'db', '') AS t(id bigint)" \
	   "the table name is empty" " as empty")" \
	"22023 as empty"
# The control resolves db.events on the same fixture, so its log is known to
# record requests; then no GET in it may end in an empty table segment.
check "empty table name: the same catalog still resolves db.events" \
	"$(rows_of "SELECT pgcolumnar.iceberg_rest_table_location('$TCAT', 'db', 'events')")" \
	"$MDLOC"
check "empty table name: no request reaches the catalog with an empty table segment" \
	"loadTable events=$(cat "$TLOG" 2>/dev/null | grep -c '^GET [^ ?]*/tables/events ') empty table segment=$(cat "$TLOG" 2>/dev/null | grep -c '^GET [^ ?]*/tables/ ')" \
	"loadTable events=1 empty table segment=0"
# The text[] overloads are STRICT: a NULL namespace array gives no rows. It runs last,
# so a crash here reddens only it and the survival check below.
check "text[] form: iceberg_rest_tables of a NULL namespace array returns no rows" \
	"$(rows_of "SELECT count(*) FROM pgcolumnar.iceberg_rest_tables('$DOTCAT', NULL::text[])")" \
	"0"

check "backend still up after the REST read" "$(survived "$CRASHES_BEFORE")" "1"

pgc_summary
