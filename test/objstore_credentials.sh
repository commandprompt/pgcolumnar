#!/usr/bin/env bash
#
# pgColumnar #393 M4: the validator split and catalog credentials.
#
# The placement policy is enforced at DDL time: non-secret config (endpoint,
# region) lives on the SERVER, secrets (access_key_id, secret_access_key,
# session_token) live ONLY on a USER MAPPING, and every misplacement is
# HV00D at CREATE/ALTER -- so a secret can never land in a world-readable
# catalog. Ambient (postmaster environment) credentials become a privilege:
# superusers, or a mapping a superuser marked credentials_required 'false'
# (password_required's shape). See design/ISSUE_393_M4_CREDENTIALS.md.
#
# The catalog arms run with NO AWS_* in the postmaster environment at all, so
# nothing ambient can make them pass vacuously: a green read in phase 1 proves
# the catalog options reached the wire. Per-user resolution is proven by two
# roles reading the SAME table over the SAME server where the only variable is
# their mapping (alice's carries the right secret, bob's a wrong one, and the
# SigV4-verifying fixture judges).
#
# Usage:  test/objstore_credentials.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# The endpoint allow-list (#393) defaults to deny-all; this suite's fixture is
# the documented configuration example for a trusted local endpoint.
export PGC_EXTRA_CONF="pgcolumnar.objstore_allowed_endpoints='127.0.0.1'"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

python3 -c 'import pyarrow' 2>/dev/null || pgc_skip pyarrow "pyarrow is required to write the fixture"

S3_PORT="$(pgc_pick_free_port "$PGC_AUX_PORT_LO" "$PGC_AUX_PORT_HI")"
S3_LOG="$PGC_WORKDIR/cred.log"
SRV_PID=""
AKID="PGCTESTKEYID"
SECRET="pgctest-good-secret"
BADSECRET="pgctest-wrong-secret"
REGION="pgc-test-1"
BUCKET="pgc-bucket"

objstore_cred_teardown() {
	[ -n "$SRV_PID" ] && kill "$SRV_PID" 2>/dev/null
	pgc_teardown
}
trap objstore_cred_teardown EXIT INT TERM

pg_restart_env() {
	pgc_pg "pg_ctl -D '$PGC_PGDATA' stop -m fast -w" >/dev/null 2>&1
	pgc_pg "$* pg_ctl -D '$PGC_PGDATA' -l '$PGC_LOGFILE' start -w" >/dev/null 2>&1
	for _ in $(seq 1 30); do
		[ -n "$(q 'SELECT 1')" ] && return 0
		sleep 0.5
	done
	echo "FATAL: cluster did not come back after env restart"; exit 1
}

# sqlstate_of <sql> [role] -> the SQLSTATE the statement fails with; "ok" only when
# psql ran it and exited 0; "noconnect" when there was no session to run it in. Success
# is positive evidence (#1400 review): an empty capture, which a session that never
# connected also gives, is never read as success.
sqlstate_of() {
	local out rc st
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U "${2:-postgres}" \
		-d "$PGC_DB" -qtA -v ON_ERROR_STOP=1 2>&1 <<SQLEOF
\\set VERBOSITY sqlstate
$1;
SQLEOF
)"
	rc=$?
	st="$(sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\).*/\1/p' <<<"$out" | head -1)"
	if [ -n "$st" ]; then echo "$st"; elif [ "$rc" = 0 ]; then echo ok; else echo noconnect; fi
}
# outcome_as <role> <sql> -> the value, or "SQLSTATE message" of the error, so a
# check shows what the server did either way
outcome_as() {
	local out
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U "$1" -d "$PGC_DB" \
		-X -At -v ON_ERROR_STOP=1 -v VERBOSITY=verbose -c "$2" 2>&1)"
	case "$out" in
		*ERROR:*) sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\): \(.*\)$/\1 \2/p' <<<"$out" | head -1;;
		*) printf '%s\n' "$out";;
	esac
}
q_as() {  # q_as <role> <sql>
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U "$1" \
		-d "$PGC_DB" -At -c "$2" 2>/dev/null || true
}

# ---- fixture + verifying server --------------------------------------------
G=3; NCOLS=4; ROWS_PER_GROUP=6000
mkdir -p "$PGC_WORKDIR/$BUCKET"
python3 - "$PGC_WORKDIR/$BUCKET/fixture.parquet" <<PYEOF
import sys
import pyarrow as pa, pyarrow.parquet as pq
G, NCOLS, RPG = $G, $NCOLS, $ROWS_PER_GROUP
n = G * RPG
cols = {("c%d" % i): pa.array([(i + 1) * v for v in range(n)], pa.int64())
        for i in range(NCOLS)}
pq.write_table(pa.table(cols), sys.argv[1], row_group_size=RPG,
               data_page_size=4096, compression="NONE", use_dictionary=False)
PYEOF
# An Iceberg table for the pgcolumnar_iceberg arms (#1887), served at s3://BUCKET/db/t
# as test/iceberg_objstore.sh serves it. noapply.metadata.json reads 5 rows.
FX="$(dirname "${BASH_SOURCE[0]}")/fixtures/iceberg"
cp -r "$FX/warehouse_del/db" "$PGC_WORKDIR/$BUCKET/db"
chmod -R u+rwX "$PGC_WORKDIR/$BUCKET/db"
python3 "$(dirname "${BASH_SOURCE[0]}")/objstore_http_server.py" \
	--dir "$PGC_WORKDIR" --port "$S3_PORT" --log "$S3_LOG" \
	--sigv4-key "$AKID" --sigv4-secret "$SECRET" --sigv4-region "$REGION" \
	> "$PGC_WORKDIR/cred_server.out" 2>&1 &
SRV_PID=$!
for _ in $(seq 1 50); do
	grep -q READY "$PGC_WORKDIR/cred_server.out" 2>/dev/null && break
	sleep 0.1
done
check "premise: sigv4 fixture server is up" \
	"$(grep -c READY "$PGC_WORKDIR/cred_server.out" 2>/dev/null)" "1"

COLDEFS="$(python3 -c "print(', '.join('c%d int8' % i for i in range($NCOLS)))")"

# ---- roles ------------------------------------------------------------------
for r in cred_alice cred_bob cred_carol; do
	psql_run "CREATE ROLE $r LOGIN;"
	psql_run "GRANT pg_read_server_files TO $r;"
done

# ---- placement arms (the validator split) -----------------------------------
check "placement: secret on a SERVER is HV00D" \
	"$(sqlstate_of "CREATE SERVER badsrv FOREIGN DATA WRAPPER pgcolumnar_parquet OPTIONS (secret_access_key 'x')")" "HV00D"
check "placement: endpoint on a TABLE is HV00D" \
	"$(sqlstate_of "CREATE SERVER tmpsrv FOREIGN DATA WRAPPER pgcolumnar_parquet; CREATE FOREIGN TABLE ft_bad ($COLDEFS) SERVER tmpsrv OPTIONS (endpoint 'x')")" "HV00D"

psql_run "CREATE SERVER csrv FOREIGN DATA WRAPPER pgcolumnar_parquet
          OPTIONS (endpoint 'http://127.0.0.1:$S3_PORT', region '$REGION');"
check "placement: endpoint and region on the SERVER are accepted" \
	"$(q "SELECT count(*) FROM pg_foreign_server WHERE srvname = 'csrv'")" "1"
check "placement: a credential on a USER MAPPING is accepted" \
	"$(sqlstate_of "CREATE USER MAPPING FOR postgres SERVER csrv
	   OPTIONS (access_key_id '$AKID', secret_access_key '$SECRET')")" "ok"
check "placement: endpoint on a USER MAPPING is HV00D" \
	"$(sqlstate_of "CREATE USER MAPPING FOR cred_alice SERVER csrv OPTIONS (endpoint 'x')")" "HV00D"

psql_run "GRANT USAGE ON FOREIGN SERVER csrv TO cred_alice, cred_bob, cred_carol;"
psql_run "CREATE USER MAPPING FOR cred_alice SERVER csrv
          OPTIONS (access_key_id '$AKID', secret_access_key '$SECRET');"
psql_run "CREATE USER MAPPING FOR cred_bob SERVER csrv
          OPTIONS (access_key_id '$AKID', secret_access_key '$BADSECRET');"

check "placement: a non-superuser cannot set credentials_required false (42501)" \
	"$(sqlstate_of "ALTER USER MAPPING FOR cred_alice SERVER csrv OPTIONS (ADD credentials_required 'false')" cred_alice)" "42501"
check "placement: a superuser can (for carol, used below)" \
	"$(sqlstate_of "CREATE USER MAPPING FOR cred_carol SERVER csrv OPTIONS (credentials_required 'false')")" "ok"

# ---- tables -----------------------------------------------------------------
psql_run "CREATE FOREIGN TABLE flocal ($COLDEFS) SERVER csrv OPTIONS (path '$PGC_WORKDIR/$BUCKET/fixture.parquet');"
psql_run "CREATE FOREIGN TABLE fs3 ($COLDEFS) SERVER csrv OPTIONS (path 's3://$BUCKET/fixture.parquet');"
psql_run "GRANT SELECT ON flocal, fs3 TO cred_alice, cred_bob, cred_carol;"

# ---- phase 1: NO AWS environment anywhere -----------------------------------
# The premise and the point: the postmaster has no AWS_* variables, so every
# green below can only have come from the catalogs.
psql_run "DROP USER MAPPING FOR postgres SERVER csrv;"
check "premise: without mapping or environment, even a superuser gets 28000" \
	"$(sqlstate_of "SELECT count(*) FROM fs3")" "28000"
psql_run "CREATE USER MAPPING FOR postgres SERVER csrv
          OPTIONS (access_key_id '$AKID', secret_access_key '$SECRET');"

check "catalog: mapping + server options reach the wire (differential)" \
	"$(pgc_set_hash "SELECT * FROM fs3")" "$(pgc_set_hash "SELECT * FROM flocal")"
check_num "catalog: count(*) with catalog credentials only" \
	"$(q "SELECT count(*) FROM fs3")" "$((G * ROWS_PER_GROUP))"

check_num "per-user: alice (good secret in her mapping) reads" \
	"$(q_as cred_alice "SELECT count(*) FROM fs3")" "$((G * ROWS_PER_GROUP))"
check "per-user: bob (wrong secret in his mapping) is refused 28000" \
	"$(sqlstate_of "SELECT count(*) FROM fs3" cred_bob)" "28000"
check "ambient-is-privilege: carol (credentials_required=false, but no env exists) is 28000" \
	"$(sqlstate_of "SELECT count(*) FROM fs3" cred_carol)" "28000"

# ---- half a credential (#1341) ----------------------------------------------
# A mapping holding access_key_id without secret_access_key, or the reverse, is a
# misconfiguration, refused before any request rather than signed with half a key
# or sent unsigned. Bob's wrong secret also ends in 28000, from the fixture's own
# refusal, so the message is what tells this refusal apart, and the request log
# says nothing was sent.
for r in cred_dave cred_erin; do
	psql_run "CREATE ROLE $r LOGIN; GRANT pg_read_server_files TO $r;
	          GRANT USAGE ON FOREIGN SERVER csrv TO $r; GRANT SELECT ON fs3 TO $r;"
done
psql_run "CREATE USER MAPPING FOR cred_dave SERVER csrv OPTIONS (access_key_id '$AKID');
          CREATE USER MAPPING FOR cred_erin SERVER csrv OPTIONS (secret_access_key '$SECRET');"
refusal_as() {	# refusal_as ROLE SQL -> "SQLSTATE message" of its error, or "ok"
	local out
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U "$1" -d "$PGC_DB" \
		-X -At -v ON_ERROR_STOP=1 -v VERBOSITY=verbose -c "$2" 2>&1)"
	case "$out" in *ERROR:*) sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\): \(.*\)$/\1 \2/p' <<<"$out" | head -1;; *) echo "ok: $out";; esac
}
_half="28000 columnar: the user mapping for server \"csrv\" carries only half a credential"
# Each arm starts on a server that accepts connections: a backend that crashes on
# one arm must not fail the next one for it (measured: without the pairing check
# the half-credential read does crash its backend).
await_up() { local i; for i in $(seq 1 60); do [ "$(q 'SELECT 1' 2>/dev/null)" = 1 ] && return; sleep 0.5; done; }
_log_lines() { wc -l < "$S3_LOG" 2>/dev/null || echo 0; }
_reqs_before="$(_log_lines)"
q_as cred_alice "SELECT count(*) FROM fs3" >/dev/null
check "control: a full mapping's read reaches the endpoint, so the log counts requests" \
	"$([ "$(( $(_log_lines) - _reqs_before ))" -gt 0 ] && echo reached || echo "nothing logged")" "reached"
_reqs_before="$(_log_lines)"
await_up
check "a mapping with an access key and no secret is refused before any request (28000)" \
	"$(refusal_as cred_dave "SELECT count(*) FROM fs3")" "$_half"
await_up
check "and one with a secret and no access key is refused the same way" \
	"$(refusal_as cred_erin "SELECT count(*) FROM fs3")" "$_half"
await_up
check "and neither half-credential read reached the endpoint" \
	"$(( $(_log_lines) - _reqs_before ))" "0"

# ---- no-leak arm ------------------------------------------------------------
BOB_ERR="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U cred_bob \
	-d "$PGC_DB" -qtA 2>&1 <<SQLEOF
\\set VERBOSITY verbose
SELECT count(*) FROM fs3;
SQLEOF
)"
EXPL="$(q "EXPLAIN (VERBOSE, COSTS OFF) SELECT * FROM fs3")"
check_num "no-leak: bob's verbose error carries no secret" \
	"$(grep -c -e "$SECRET" -e "$BADSECRET" <<<"$BOB_ERR")" "0"
check "premise: bob's error was a real refusal, not silence" \
	"$([ -n "$BOB_ERR" ] && echo yes)" "yes"
check_num "no-leak: EXPLAIN VERBOSE carries no secret" \
	"$(grep -c -e "$SECRET" -e "$BADSECRET" <<<"$EXPL")" "0"
check_num "no-leak: srvoptions carry no secret (by construction)" \
	"$(q "SELECT count(*) FROM pg_foreign_server WHERE srvoptions::text LIKE '%$SECRET%'")" "0"

# ---- a PUBLIC mapping (#1341) ----------------------------------------------
# sql-reference.md: a read resolves the scanning user's own mapping first, then a
# PUBLIC one. Frank has no mapping of his own; bob's own carries the wrong secret, so
# a PUBLIC mapping holding the right one must not rescue him. Dropped again before
# phase 2, where a PUBLIC mapping would answer for the ambient arms.
psql_run "CREATE ROLE cred_frank LOGIN; GRANT pg_read_server_files TO cred_frank;
          GRANT USAGE ON FOREIGN SERVER csrv TO cred_frank; GRANT SELECT ON fs3 TO cred_frank;"
psql_run "CREATE USER MAPPING FOR PUBLIC SERVER csrv
          OPTIONS (access_key_id '$AKID', secret_access_key '$SECRET');"
# A refused read counts no rows, so a refusal is a measurement and not an empty capture.
check_num "a role with no mapping of its own reads through the PUBLIC mapping" \
	"$(c="$(q_as cred_frank "SELECT count(*) FROM fs3")"; echo "${c:-0}")" "$((G * ROWS_PER_GROUP))"
check "a role's own mapping is used before the PUBLIC one (bob's wrong secret is still refused 28000)" \
	"$(sqlstate_of "SELECT count(*) FROM fs3" cred_bob)" "28000"
psql_run "DROP USER MAPPING FOR PUBLIC SERVER csrv;"

# ---- phase 2: environment present; ambient is still a privilege -------------
pg_restart_env "AWS_ENDPOINT_URL='http://127.0.0.1:$S3_PORT'" \
	"AWS_ACCESS_KEY_ID='$AKID'" "AWS_SECRET_ACCESS_KEY='$SECRET'" \
	"AWS_REGION='$REGION'"

check_num "ambient: carol (credentials_required=false) now reads via the environment" \
	"$(q_as cred_carol "SELECT count(*) FROM fs3")" "$((G * ROWS_PER_GROUP))"
psql_run "DROP USER MAPPING FOR cred_carol SERVER csrv;"
check "ambient: without her mapping, carol is refused despite the environment" \
	"$(sqlstate_of "SELECT count(*) FROM fs3" cred_carol)" "28000"
# The pgcolumnar_iceberg wrapper reads with the server environment's credentials
# (#1887). docs/sql-reference.md says it takes no server or user-mapping options.
# So carol, refused above on the pgcolumnar_parquet table for want of a mapping,
# reads through it. The contrast is the point: the same role, no mapping, two
# wrappers, two answers.
check "a pgcolumnar_iceberg server refuses an endpoint option (#1887)" \
	"$(outcome_as postgres "CREATE SERVER isrv_bad FOREIGN DATA WRAPPER pgcolumnar_iceberg OPTIONS (endpoint 'http://127.0.0.1:$S3_PORT')")" \
	'HV00D invalid option "endpoint" for pgcolumnar_iceberg'
psql_run "CREATE SERVER isrv FOREIGN DATA WRAPPER pgcolumnar_iceberg;
          CREATE FOREIGN TABLE fice (id bigint, region text, amount int) SERVER isrv
            OPTIONS (metadata_path 's3://$BUCKET/db/t/metadata/noapply.metadata.json');
          GRANT USAGE ON FOREIGN SERVER isrv TO cred_carol; GRANT SELECT ON fice TO cred_carol;"
# The mapping is tried for a throwaway role, not carol: if the refusal were gone,
# the mapping would be created, and carol's no-mapping premise below must not
# depend on this check passing.
psql_run "CREATE ROLE cred_umprobe NOLOGIN;"
check "a pgcolumnar_iceberg user mapping refuses an access_key_id option (#1887)" \
	"$(outcome_as postgres "CREATE USER MAPPING FOR cred_umprobe SERVER isrv OPTIONS (access_key_id '$AKID')")" \
	'HV00D invalid option "access_key_id" for pgcolumnar_iceberg'
check "premise: carol has no user mapping on either server (#1887)" \
	"$(q "SELECT count(*) FROM pg_user_mappings WHERE usename = 'cred_carol'")" "0"
check "ambient: carol, with no mapping, is refused on the pgcolumnar_parquet table: no credentials (#1887)" \
	"$(outcome_as cred_carol "SELECT count(*) FROM fs3")" \
	"28000 columnar: no credentials for \"s3://$BUCKET/fixture.parquet\""
check "ambient: carol, with no mapping, reads 5 rows through the pgcolumnar_iceberg table (#1887)" \
	"$(outcome_as cred_carol "SELECT count(*) FROM fice")" "5"
psql_run "DROP USER MAPPING FOR postgres SERVER csrv;"
check_num "ambient: a superuser with no mapping reads via the environment" \
	"$(q "SELECT count(*) FROM fs3")" "$((G * ROWS_PER_GROUP))"

pgc_summary
