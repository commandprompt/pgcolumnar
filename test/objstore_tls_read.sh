#!/usr/bin/env bash
#
# pgColumnar #393 M3: TLS for the object-store module, with every verification
# check proven by a certificate that must be refused.
#
# There is no byte-exact oracle for "did we verify the certificate correctly"
# (the memo's point), so this suite is a refusal matrix: one server whose
# certificate is good (CA-1, SAN IP:127.0.0.1 -- the IP-literal path that
# X509_check_host alone silently misses), and three that must each be refused
# for a different reason. The good arm doubles as the over-restriction proof: a
# configuration that refuses everything cannot pass it.
#
#   A   https:// differential oracle on the good server (set1_ip_asc path,
#       SNI suppressed for the IP literal).
#   A2  s3://bucket/key over an https endpoint with SigV4 verification on:
#       the M2 signer composed over TLS.
#   C   wronghost / expired / untrusted / self-signed each fail SQLSTATE
#       08006 with the module's own message and OpenSSL's verification
#       reason, word for word, in the DETAIL -- not any error that happens to
#       mention a certificate -- and each carries the premise
#       that the TLS handshake was genuinely attempted (the fixture logs
#       HANDSHAKE at accept time, BEFORE the handshake can fail) -- a reason
#       grep without that premise is satisfied by a server that was never
#       contacted.
#   D   statement_timeout cancels a transfer stalled mid-body over TLS: the
#       SSL_ERROR_WANT_* wait loop services interrupts like the cleartext one.
#   E   the CA in SSL_CERT_DIR alone (no SSL_CERT_FILE) carries the good read:
#       the default verify paths honour the directory as well as the file.
#   No arm may crash a backend: the server log's "terminated by signal" count
#   is read at the end, and every refusal waits for the server to answer
#   SELECT 1 again, so a crash fails its own checks and not the next arm's.
#
# The fixture CA is trusted through SSL_CERT_FILE in the postmaster
# environment, which OpenSSL's default verify paths honour; the module carries
# no test-only trust code.
#
# Usage:  test/objstore_tls_read.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# The endpoint allow-list (#393) defaults to deny-all; this suite's fixture is
# the documented configuration example for a trusted local endpoint.
export PGC_EXTRA_CONF="pgcolumnar.objstore_allowed_endpoints='127.0.0.1'"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

python3 -c 'import pyarrow' 2>/dev/null || pgc_skip pyarrow "pyarrow is required to write the fixture"
command -v openssl >/dev/null || pgc_skip openssl "openssl CLI is required to generate the fixture PKI"

PKI="$PGC_WORKDIR/pki"
LOG="$PGC_WORKDIR/tls.log"
BUCKET="pgc-bucket"
AKID="PGCTESTKEYID"
SECRET="pgctest-secret-shhh"
REGION="pgc-test-1"
SRV_PIDS=()

objstore_tls_teardown() {
	local p
	for p in "${SRV_PIDS[@]:-}"; do [ -n "$p" ] && kill "$p" 2>/dev/null; done
	pgc_teardown
}
trap objstore_tls_teardown EXIT INT TERM

# ---- PKI --------------------------------------------------------------------
mkdir -p "$PKI"
gen_ca() {  # gen_ca <name>
	openssl genrsa -out "$PKI/$1.key" 2048 2>/dev/null
	openssl req -x509 -new -key "$PKI/$1.key" -subj "/CN=pgc-test-$1" \
		-days 2 -out "$PKI/$1.pem" 2>/dev/null
}
gen_cert() {  # gen_cert <name> <ca> <san> [extra x509 args...]
	local name="$1" ca="$2" san="$3"; shift 3
	openssl genrsa -out "$PKI/$name.key" 2048 2>/dev/null
	openssl req -new -key "$PKI/$name.key" -subj "/CN=$name" \
		-out "$PKI/$name.csr" 2>/dev/null
	printf 'subjectAltName=%s\n' "$san" > "$PKI/$name.ext"
	openssl x509 -req -in "$PKI/$name.csr" -CA "$PKI/$ca.pem" \
		-CAkey "$PKI/$ca.key" -CAcreateserial -days 2 \
		-extfile "$PKI/$name.ext" "$@" -out "$PKI/$name.pem" 2>/dev/null
}
gen_ca ca1
gen_ca ca2
gen_cert good      ca1 "IP:127.0.0.1"
gen_cert goodsig   ca1 "IP:127.0.0.1"
gen_cert wronghost ca1 "DNS:other.example"
gen_cert expired   ca1 "IP:127.0.0.1" \
	-not_before 20200101000000Z -not_after 20200102000000Z
# -not_before/-not_after need OpenSSL 3.4; on an older one (Ubuntu 24.04 CI
# ships 3.0) the command fails and leaves no pem. Fall back to a zero-lifetime
# certificate: notAfter equals the signing instant, so it is expired by the
# time the server answers its first request. The premise check below still
# proves the certificate is genuinely expired, whichever path produced it.
[ -s "$PKI/expired.pem" ] || gen_cert expired ca1 "IP:127.0.0.1" -days 0
gen_cert untrusted ca2 "IP:127.0.0.1"
# A self-signed leaf: its own issuer, trusted by nobody.
openssl req -x509 -newkey rsa:2048 -nodes -keyout "$PKI/selfsigned.key" \
	-subj "/CN=selfsigned" -addext "subjectAltName=IP:127.0.0.1" \
	-days 2 -out "$PKI/selfsigned.pem" 2>/dev/null
# The SSL_CERT_DIR form of CA-1: a directory holding it under its subject hash.
mkdir -p "$PKI/cadir"
cp "$PKI/ca1.pem" "$PKI/cadir/$(openssl x509 -hash -noout -in "$PKI/ca1.pem").0"
for f in ca1 ca2 good goodsig wronghost expired untrusted selfsigned; do
	[ -s "$PKI/$f.pem" ] || { echo "FATAL: PKI generation failed for $f"; exit 1; }
done
check "premise: expired certificate is genuinely expired" \
	"$(openssl x509 -in "$PKI/expired.pem" -checkend 0 >/dev/null 2>&1 && echo valid || echo expired)" \
	"expired"

# ---- fixture + servers ------------------------------------------------------
G=3; NCOLS=8; ROWS_PER_GROUP=8000
mkdir -p "$PGC_WORKDIR/$BUCKET"
python3 - "$PGC_WORKDIR/$BUCKET/fixture.parquet" <<PYEOF
import sys
import pyarrow as pa, pyarrow.parquet as pq
G, NCOLS, RPG = $G, $NCOLS, $ROWS_PER_GROUP
n = G * RPG
cols = {("c%d" % i): pa.array([(i + 1) * v for v in range(n)], pa.int64())
        for i in range(NCOLS)}
pq.write_table(pa.table(cols), sys.argv[1], row_group_size=RPG,
               data_page_size=1024, compression="NONE", use_dictionary=False)
PYEOF

declare -A PORT
start_srv() {  # start_srv <name> <cert> [extra server args...]
	local name="$1" cert="$2"; shift 2
	PORT[$name]="$(pgc_pick_free_port "$PGC_AUX_PORT_LO" "$PGC_AUX_PORT_HI" $((RANDOM)))"
	python3 "$(dirname "${BASH_SOURCE[0]}")/objstore_http_server.py" \
		--dir "$PGC_WORKDIR" --port "${PORT[$name]}" --log "$LOG" \
		--tls-cert "$PKI/$cert.pem" --tls-key "$PKI/$cert.key" "$@" \
		> "$PGC_WORKDIR/srv_$name.out" 2>&1 &
	SRV_PIDS+=($!)
	for _ in $(seq 1 50); do
		grep -q READY "$PGC_WORKDIR/srv_$name.out" 2>/dev/null && return 0
		sleep 0.1
	done
	echo "FATAL: server $name did not start"; exit 1
}
start_srv good      good
start_srv goodsig   goodsig --sigv4-key "$AKID" --sigv4-secret "$SECRET" \
	--sigv4-region "$REGION"
start_srv wronghost wronghost
start_srv expired   expired
start_srv untrusted untrusted
start_srv selfsigned selfsigned
check_num "premise: six TLS servers are up" "${#SRV_PIDS[@]}" "6"
CRASHES_BEFORE="$(pgc_pg "grep -c 'terminated by signal' '$PGC_LOGFILE'" 2>/dev/null)"

# Restart the cluster with the trust anchor and the s3 environment.
pgc_pg "pg_ctl -D '$PGC_PGDATA' stop -m fast -w" >/dev/null 2>&1
pgc_pg "SSL_CERT_FILE='$PKI/ca1.pem' AWS_ENDPOINT_URL='https://127.0.0.1:${PORT[goodsig]}' \
AWS_ACCESS_KEY_ID='$AKID' AWS_SECRET_ACCESS_KEY='$SECRET' AWS_REGION='$REGION' \
pg_ctl -D '$PGC_PGDATA' -l '$PGC_LOGFILE' start -w" >/dev/null 2>&1
for _ in $(seq 1 30); do [ -n "$(q 'SELECT 1')" ] && break; sleep 0.5; done

COLDEFS="$(python3 -c "print(', '.join('c%d int8' % i for i in range($NCOLS)))")"
psql_run "CREATE SERVER tlssrv FOREIGN DATA WRAPPER pgcolumnar_parquet;"
psql_run "CREATE FOREIGN TABLE flocal ($COLDEFS) SERVER tlssrv OPTIONS (path '$PGC_WORKDIR/$BUCKET/fixture.parquet');"
psql_run "CREATE FOREIGN TABLE fgood ($COLDEFS) SERVER tlssrv OPTIONS (path 'https://127.0.0.1:${PORT[good]}/$BUCKET/fixture.parquet');"
psql_run "CREATE FOREIGN TABLE fs3tls ($COLDEFS) SERVER tlssrv OPTIONS (path 's3://$BUCKET/fixture.parquet');"
psql_run "CREATE FOREIGN TABLE fwrong ($COLDEFS) SERVER tlssrv OPTIONS (path 'https://127.0.0.1:${PORT[wronghost]}/$BUCKET/fixture.parquet');"
psql_run "CREATE FOREIGN TABLE fexp ($COLDEFS) SERVER tlssrv OPTIONS (path 'https://127.0.0.1:${PORT[expired]}/$BUCKET/fixture.parquet');"
psql_run "CREATE FOREIGN TABLE funtr ($COLDEFS) SERVER tlssrv OPTIONS (path 'https://127.0.0.1:${PORT[untrusted]}/$BUCKET/fixture.parquet');"
psql_run "CREATE FOREIGN TABLE fself ($COLDEFS) SERVER tlssrv OPTIONS (path 'https://127.0.0.1:${PORT[selfsigned]}/$BUCKET/fixture.parquet');"
psql_run "CREATE FOREIGN TABLE fstall ($COLDEFS) SERVER tlssrv OPTIONS (path 'https://127.0.0.1:${PORT[good]}/stall/$BUCKET/fixture.parquet');"

# SQLSTATE and full verbose error of a failing statement.
verr() {
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -qtA 2>&1 <<SQLEOF
\\set VERBOSITY verbose
$1;
SQLEOF
}
handshakes_for() {  # handshakes since last truncation
	grep -c "^HANDSHAKE" "$LOG" | tr -d ' '
}
wait_up() {  # a crashed backend fails its own check, not the next arm's
	for _ in $(seq 1 30); do [ -n "$(q 'SELECT 1')" ] && return; sleep 0.5; done
}

# ---- arm A: the good certificate carries data -------------------------------
check "A: https differential == local" \
	"$(pgc_set_hash "SELECT * FROM fgood")" "$(pgc_set_hash "SELECT * FROM flocal")"
check_num "A: count(*) over https" \
	"$(q "SELECT count(*) FROM fgood")" "$((G * ROWS_PER_GROUP))"

# ---- arm A2: SigV4 composed over TLS ---------------------------------------
check "A2: s3 over https differential == local" \
	"$(pgc_set_hash "SELECT * FROM fs3tls")" "$(pgc_set_hash "SELECT * FROM flocal")"

# ---- arm C: the refusal matrix ---------------------------------------------
# The refusal is the module's own: its message names the URL it could not
# reach, and its DETAIL is OpenSSL's verification reason for THIS certificate,
# compared whole -- a different verification failure, a cleartext reply or a
# crash each read differently.
c_arm() {  # c_arm <label> <table> <server> <verification reason>
	local label="$1" tbl="$2" srv="$3" want="$4" out state msg detail hs0 hs1
	hs0="$(handshakes_for)"
	out="$(verr "SELECT count(*) FROM $tbl")"
	hs1="$(handshakes_for)"
	wait_up
	state="$(sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\).*/\1/p' <<<"$out" | head -1)"
	msg="$(sed -n 's/^ERROR:  [0-9A-Z]\{5\}: //p' <<<"$out" | head -1)"
	detail="$(sed -n 's/^DETAIL:  //p' <<<"$out" | head -1)"
	check "premise: $label handshake was attempted" \
		"$([ "$hs1" -gt "$hs0" ] 2>/dev/null && echo yes)" "yes"
	check "C: $label refused with 08006" "$state" "08006"
	check "C: $label refusal is the TLS one" "$msg" \
		"columnar: could not establish TLS to \"https://127.0.0.1:${PORT[$srv]}/$BUCKET/fixture.parquet\""
	check "C: $label names its reason" "$detail" "Certificate verification failed: $want."
	check_num "C: $label returned no rows" \
		"$(grep -cE '^[0-9]+$' <<<"$out")" "0"
}
c_arm "wrong-host" fwrong wronghost "IP address mismatch"
c_arm "expired" fexp expired "certificate has expired"
c_arm "untrusted-ca" funtr untrusted "unable to get local issuer certificate"
c_arm "self-signed" fself selfsigned "self-signed certificate"

# ---- arm D: cancel mid-body over TLS ---------------------------------------
D_START=$(date +%s)
D_STATE="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
	-d "$PGC_DB" -qtA 2>&1 <<SQLEOF | sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\).*/\1/p' | head -1
\\set VERBOSITY sqlstate
SET statement_timeout = '2s';
SELECT count(*) FROM fstall;
SQLEOF
)"
D_ELAPSED=$(( $(date +%s) - D_START ))
check "D: stalled TLS transfer cancelled with query_canceled" "$D_STATE" "57014"
check_num "D: cancel latency bounded (<=10s)" \
	"$([ "$D_ELAPSED" -le 10 ] 2>/dev/null && echo 1 || echo 0)" "1"
wait_up

# ---- arm E: the CA from SSL_CERT_DIR alone ---------------------------------
pgc_pg "pg_ctl -D '$PGC_PGDATA' stop -m fast -w" >/dev/null 2>&1
pgc_pg "SSL_CERT_DIR='$PKI/cadir' pg_ctl -D '$PGC_PGDATA' -l '$PGC_LOGFILE' start -w" >/dev/null 2>&1
wait_up
check_num "E: SSL_CERT_DIR alone trusts the CA (count(*) over https)" \
	"$(q "SELECT count(*) FROM fgood")" "$((G * ROWS_PER_GROUP))"

check_num "survival: no backend was terminated by a signal" \
	"$(( $(pgc_pg "grep -c 'terminated by signal' '$PGC_LOGFILE'" 2>/dev/null) - CRASHES_BEFORE ))" "0"

pgc_summary
