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
#      already arrived. The 403 and its hint now survive on an http:// endpoint,
#      which is what this fixture serves.
#   D over TLS (#1902): the same refusal from an https:// store still lost its
#      status, because a failed send leaves a TLS session unreadable, so the
#      early answer cannot be read after it. A write whose body is over 1 MiB
#      now sends its headers with Expect: 100-continue and waits up to a second
#      for the store to answer them; a refusal then arrives before the body is
#      sent. 100 Continue, or no answer in time, sends the body; 417 sends the
#      request again without Expect. A wait that runs out stops Expect for the
#      handle's later writes, so a store that ignores it costs the wait once, not
#      once per part; a 2xx before the body still gets the body (#1903 review).
#
# Facet A (s3://<bucket>/_ in prefix and glob errors) is #1869's fix and its
# arms are in test/objstore_export_open_uploads.sh.
#
# One SigV4 fixture with a refused bucket, the same fixture again over https, (--tamper-bucket: every request there
# is verified against another secret, refused 403 before any body is read), three
# REST catalogs over the committed iceberg warehouse (one vends a wrong secret in
# its flat config, one in a storage-credentials entry, one vends nothing), and a postmaster whose environment holds the fixture's
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
command -v openssl >/dev/null \
	|| pgc_skip openssl "the openssl CLI is required to make the https fixture's certificate"
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

# The same fixture over https (#1902), with a certificate for 127.0.0.1 that the
# postmaster trusts through SSL_CERT_FILE.
PKI="$PGC_WORKDIR/pki"
mkdir -p "$PKI"
openssl req -x509 -newkey rsa:2048 -nodes -keyout "$PKI/srv.key" -subj "/CN=127.0.0.1" \
	-addext "subjectAltName=IP:127.0.0.1" -days 2 -out "$PKI/srv.pem" 2>/dev/null
TLS_PORT="$(pgc_pick_free_port "$PGC_AUX_PORT_LO" "$PGC_AUX_PORT_HI")"
python3 "$(dirname "${BASH_SOURCE[0]}")/objstore_http_server.py" \
	--dir "$PGC_WORKDIR" --port "$TLS_PORT" --log "$PGC_WORKDIR/tls.log" \
	--sigv4-key "$AKID" --sigv4-secret "$SECRET" --sigv4-region "$REGION" \
	--tamper-bucket "$DENIED" --tls-cert "$PKI/srv.pem" --tls-key "$PKI/srv.key" \
	> "$PGC_WORKDIR/tls.out" 2>&1 &
PIDS="$PIDS $!"
ready "$PGC_WORKDIR/tls.out"
check "premise: the https SigV4 fixture is up" \
	"$(grep -c READY "$PGC_WORKDIR/tls.out" 2>/dev/null)" "1"

# The postmaster's environment holds the fixture's RIGHT key.
pgc_pg "pg_ctl -D '$PGC_PGDATA' stop -m fast -w" >/dev/null 2>&1
pgc_pg "unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_ENDPOINT_URL AWS_REGION AWS_DEFAULT_REGION AWS_PROFILE AWS_SHARED_CREDENTIALS_FILE AWS_CONFIG_FILE; AWS_ENDPOINT_URL='$S3EP' AWS_ACCESS_KEY_ID='$AKID' AWS_SECRET_ACCESS_KEY='$SECRET' AWS_REGION='$REGION' PGCOLUMNAR_ICEBERG_REST_TOKEN='$RESTTOK' SSL_CERT_FILE='$PKI/srv.pem' pg_ctl -D '$PGC_PGDATA' -l '$PGC_LOGFILE' start -w" >/dev/null 2>&1
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

# The same through a storage-credentials entry (#1861): the catalog vends no flat
# config, and its one entry, covering the whole bucket, holds a key the fixture
# refuses. The reader signs with the entry it chose for each file, so that
# entry's config, not a flat one, must carry the vended mark into the module.
start_rest --token "$RESTTOK" --vend-key "$AKID" --vend-secret "$SECRET" \
	--vend-region "$REGION" --vend-endpoint "$S3EP" --storage-cred "s3://$BUCKET/=bad"
CAT_ENTRY="http://127.0.0.1:$LAST_PORT"
check "vended key refused through a storage-credentials entry: the 403 hint names the catalog's vended credentials" \
	"$(refusal "SELECT count(*) FROM pgcolumnar.iceberg_rest_scan('$CAT_ENTRY','db','t') AS t(id bigint, region text, amount int)" \
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
# The size comes from the same export to a local file, so the premise does not
# depend on the remote write it sizes (a write that never sends its body, #1902).
q "SELECT pgcolumnar.export_parquet('ea_mid', '$PGC_WORKDIR/mid_local.parquet')" >/dev/null
MID_BYTES="$(stat -c %s "$PGC_WORKDIR/mid_local.parquet" 2>/dev/null || echo 0)"
check "premise: the mid-size export is one PUT of more than 4 MiB" \
	"$( [ "$MID_PUTS" = 1 ] && [ "$MID_POSTS" = 0 ] && [ "$MID_BYTES" -gt 4194304 ] && echo yes \
		|| echo "no ($MID_PUTS PUT, $MID_POSTS POST, $MID_BYTES bytes)")" "yes"
check "a small export to the refused bucket reports the 403 and its hint" \
	"$(refusal "SELECT pgcolumnar.export_parquet('ea_small', 's3://$DENIED/s.parquet')")" \
	"28000: columnar: access to \"s3://$DENIED/s.parquet\" was denied (HTTP 403: SignatureDoesNotMatch) | HINT: Check the AWS_* credential environment of the server process."
check "mid-size export refused before its body was read: the 403 and its hint survive" \
	"$(refusal "SELECT pgcolumnar.export_parquet('ea_mid', 's3://$DENIED/m.parquet')")" \
	"28000: columnar: access to \"s3://$DENIED/m.parquet\" was denied (HTTP 403: SignatureDoesNotMatch) | HINT: Check the AWS_* credential environment of the server process."

# ---- D over TLS (#1902): the store answers the headers before the body is sent --
# The writes go through a server whose endpoint is the https fixture, with the
# right key in its user mapping; the read-back goes through the http fixture,
# which serves the same directory. Each hook file plays one kind of store.
psql_run "CREATE SERVER ea_tls FOREIGN DATA WRAPPER pgcolumnar_parquet
            OPTIONS (endpoint 'https://127.0.0.1:$TLS_PORT', region '$REGION');
          CREATE USER MAPPING FOR postgres SERVER ea_tls
            OPTIONS (access_key_id '$AKID', secret_access_key '$SECRET');" >/dev/null
EXPECT_LOG="$PGC_WORKDIR/__expect_log__"
: > "$EXPECT_LOG"
# export_back REL KEY COLS [EXPORT_ARGS [SETUP]] -> "N exported, M read back", or the
# export's error. EXPORT_ARGS follow the URL in the export call; SETUP runs first.
export_back() {
	local r
	r="$(refusal "${5:-}SELECT pgcolumnar.export_parquet('$1', 's3://$BUCKET/$2'${4:-})")"
	case "$r" in "no error ("*")") ;; *) echo "$r"; return ;; esac
	r="${r#no error (}"
	echo "${r%)} exported, $(q "SELECT count(*) FROM pgcolumnar.read_parquet('s3://$BUCKET/$2') AS t($3)") read back"
}
# tls_export REL KEY COLS -> the same through the https server ea_tls
tls_export() { export_back "$1" "$2" "$3" ", 'ea_tls'"; }
MID_COLS="id int8, t text"
check "a mid-size export over https whose store answers 100 Continue writes the object" \
	"$(tls_export ea_mid tls_mid.parquet "$MID_COLS")" "300000 exported, 300000 read back"
check "the mid-size PUT over https carried Expect: 100-continue" \
	"$(grep -c "^PUT /$BUCKET/tls_mid.parquet 100-continue\$" "$EXPECT_LOG")" "1"
SMALL_TLS="$(tls_export ea_small tls_small.parquet "id int8")"
SMALL_EXPECT="$(grep -c " /$BUCKET/tls_small.parquet " "$EXPECT_LOG")"
check "a small export over https sends no Expect header" \
	"$( [ "$SMALL_TLS" = "1000 exported, 1000 read back" ] && [ "$SMALL_EXPECT" = 0 ] && echo yes \
		|| echo "no ($SMALL_TLS; $SMALL_EXPECT requests with Expect)")" "yes"
check "mid-size export over https refused before its body was sent: the 403 and its hint survive" \
	"$(refusal "SELECT pgcolumnar.export_parquet('ea_mid', 's3://$DENIED/tm.parquet', 'ea_tls')")" \
	"28000: columnar: access to \"s3://$DENIED/tm.parquet\" was denied (HTTP 403: SignatureDoesNotMatch) | HINT: Check access_key_id and secret_access_key in the user mapping the current role uses for this server."

: > "$PGC_WORKDIR/__expect_ignore__"
check "a mid-size export over https to a store that ignores Expect is sent after the wait" \
	"$(tls_export ea_mid tls_ignored.parquet "$MID_COLS")" "300000 exported, 300000 read back"
rm -f "$PGC_WORKDIR/__expect_ignore__"
echo 2 > "$PGC_WORKDIR/__expect_delay__"
check "a 100 Continue that comes after the wait is passed over for the final answer" \
	"$(tls_export ea_mid tls_late.parquet "$MID_COLS")" "300000 exported, 300000 read back"
rm -f "$PGC_WORKDIR/__expect_delay__"
: > "$PGC_WORKDIR/__expect_417__"
check "a store that answers 417 to Expect gets the export again without it" \
	"$(tls_export ea_mid tls_417.parquet "$MID_COLS")" "300000 exported, 300000 read back"
rm -f "$PGC_WORKDIR/__expect_417__"

# A store that ignores Expect costs the wait once per connection, not once per
# request (#1903 review): after a wait runs out, the handle's later writes, a
# multipart export's later parts, go without Expect. 2 MiB parts make the
# mid-size export several parts over 1 MiB, each of which would wait a second.
: > "$PGC_WORKDIR/__expect_ignore__"
: > "$S3_LOG"
MP_IGN="$(export_back ea_mid mp_ignored.parquet "$MID_COLS" "" "SET pgcolumnar.objstore_part_size = 2097152; ")"
rm -f "$PGC_WORKDIR/__expect_ignore__"
MP_PARTS="$(grep -c "^PUT /$BUCKET/mp_ignored.parquet partNumber=" "$S3_LOG")"
check "premise: the multipart export to a store that ignores Expect is 3 or more parts, all read back" \
	"$( [ "$MP_IGN" = "300000 exported, 300000 read back" ] && [ "$MP_PARTS" -ge 3 ] && echo yes \
		|| echo "no ($MP_IGN; $MP_PARTS parts)")" "yes"
check "a multipart export to a store that ignores Expect sends it on one part only" \
	"$(grep -c "^PUT /$BUCKET/mp_ignored.parquet 100-continue\$" "$EXPECT_LOG")" "1"

# A 2xx to the headers alone cannot mean the object was written, since none of
# its body was sent (#1903 review). The body is sent, and the 2xx stands as the
# request's one final answer. The fixture stores the body after the 200, so the
# read-back waits for the object to settle first.
: > "$PGC_WORKDIR/__expect_early_200__"
E200="$(refusal "SELECT pgcolumnar.export_parquet('ea_mid', 's3://$BUCKET/early200.parquet')")"
rm -f "$PGC_WORKDIR/__expect_early_200__"
E200="${E200#no error (}"; E200="${E200%)}"
E200_OBJ="$PGC_WORKDIR/$BUCKET/early200.parquet"
_prev=-1
for _ in $(seq 1 25); do
	_sz="$(stat -c %s "$E200_OBJ" 2>/dev/null || echo -1)"
	[ "$_sz" -gt 0 ] && [ "$_sz" = "$_prev" ] && break
	_prev="$_sz"; sleep 0.2
done
check "a single PUT answered 200 before its body still sends the body, and the object holds the rows" \
	"$E200 | $(q "SELECT count(*) FROM pgcolumnar.read_parquet('s3://$BUCKET/early200.parquet') AS t($MID_COLS)")" \
	"300000 | 300000"

# A store that answers neither 100 nor the refusal within the wait: the body is
# being sent when the refusal comes, which is #1895's case again, and over
# http the early answer is still read after the failed send.
echo 2 > "$PGC_WORKDIR/__deny_delay__"
check "mid-size export refused after the Expect wait ran out (http): the 403 and its hint survive" \
	"$(refusal "SELECT pgcolumnar.export_parquet('ea_mid', 's3://$DENIED/late.parquet')")" \
	"28000: columnar: access to \"s3://$DENIED/late.parquet\" was denied (HTTP 403: SignatureDoesNotMatch) | HINT: Check the AWS_* credential environment of the server process."
rm -f "$PGC_WORKDIR/__deny_delay__"

check "the server survived the refusals" "$(survived "$crashes0")" "1"

pgc_summary
