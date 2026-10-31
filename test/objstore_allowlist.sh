#!/usr/bin/env bash
#
# pgColumnar #393: the endpoint allow-list, empty by default.
#
# pgcolumnar.objstore_allowed_endpoints gates every remote scheme in the
# object-store module: empty (the default) refuses all remote access, so
# pg_read_server_files is not an SSRF primitive out of the box; and link-local
# ranges (the cloud-metadata credential-theft path, 169.254.0.0/16) are
# refused UNCONDITIONALLY, even when an operator lists them. The GUC is SUSET
# because a USERSET list would let any role widen its own allow-list, which is
# the exact privilege the list withholds. design/ISSUE_393_ENDPOINT_ALLOWLIST.md.
#
# The suite restarts its cluster between list values because the ordinary way
# to set a SUSET GUC for a workload is the configuration file; a superuser
# session SET is exercised once to prove per-session override works for
# admins.
#
# Usage:  test/objstore_allowlist.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

python3 -c 'import pyarrow' 2>/dev/null || pgc_skip pyarrow "pyarrow is required to write the fixture"

HTTP_PORT="$(pgc_pick_free_port "$PGC_AUX_PORT_LO" "$PGC_AUX_PORT_HI")"
LOG="$PGC_WORKDIR/allow.log"
SRV_PID=""

objstore_allow_teardown() {
	[ -n "$SRV_PID" ] && kill "$SRV_PID" 2>/dev/null
	pgc_teardown
}
trap objstore_allow_teardown EXIT INT TERM

sqlstate_of() {  # sqlstate_of <sql> [role]
	env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U "${2:-postgres}" \
		-d "$PGC_DB" -qtA 2>&1 <<SQLEOF | sed -n 's/^ERROR:  \([0-9A-Z]\{5\}\).*/\1/p' | head -1
\\set VERBOSITY sqlstate
$1;
SQLEOF
}

set_list() {  # set_list <value>  (ALTER SYSTEM + reload: the operator's path)
	psql_run "ALTER SYSTEM SET pgcolumnar.objstore_allowed_endpoints = '$1';"
	psql_run "SELECT pg_reload_conf();"
	sleep 0.5
}

# ---- fixture ----------------------------------------------------------------
python3 - "$PGC_WORKDIR/fixture.parquet" <<'PYEOF'
import sys
import pyarrow as pa, pyarrow.parquet as pq
pq.write_table(pa.table({"a": pa.array(range(1000), pa.int64())}), sys.argv[1])
PYEOF
python3 "$(dirname "${BASH_SOURCE[0]}")/objstore_http_server.py" \
	--dir "$PGC_WORKDIR" --port "$HTTP_PORT" --log "$LOG" \
	> "$PGC_WORKDIR/allow_server.out" 2>&1 &
SRV_PID=$!
for _ in $(seq 1 50); do
	grep -q READY "$PGC_WORKDIR/allow_server.out" 2>/dev/null && break
	sleep 0.1
done
check "premise: fixture server is up" \
	"$(grep -c READY "$PGC_WORKDIR/allow_server.out" 2>/dev/null)" "1"

URL="http://127.0.0.1:$HTTP_PORT/fixture.parquet"
Q="SELECT count(*) FROM pgcolumnar.read_parquet('$URL') AS (a int8)"

# ---- the default: deny everything remote ------------------------------------
# A check, not a premise (#1236; #1379 review): the default is the product's. And
# "empty" is produced by the query itself, so a capture that is empty because the
# query never ran cannot pass.
check "the list defaults to empty" \
	"$(q "SELECT CASE WHEN current_setting('pgcolumnar.objstore_allowed_endpoints') = '' THEN 'empty'
	          ELSE 'set: [' || current_setting('pgcolumnar.objstore_allowed_endpoints') || ']' END")" "empty"
check "default: an http read is refused 42501" "$(sqlstate_of "$Q")" "42501"
check "default: the refusal names the GUC" \
	"$([ "$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -qtA \
		-c "$Q" 2>&1 | grep -c objstore_allowed_endpoints)" -ge 1 ] && echo yes || echo no)" "yes"
check "default: a local read is untouched by the list" \
	"$(q "SELECT count(*) FROM pgcolumnar.read_parquet('$PGC_WORKDIR/fixture.parquet') AS (a int8)")" "1000"

# ---- the list admits exactly what it names ----------------------------------
set_list "127.0.0.1"
check_num "listed host: the read works" "$(q "$Q")" "1000"
set_list "127.0.0.2"
check "unlisted host: refused 42501" "$(sqlstate_of "$Q")" "42501"
set_list "127.0.0.1:$HTTP_PORT"
check_num "host:port entry, right port: works" "$(q "$Q")" "1000"
set_list "127.0.0.1:1"
check "host:port entry, wrong port: refused 42501" "$(sqlstate_of "$Q")" "42501"
set_list "other.example, 127.0.0.1"
check_num "a multi-entry list matches any entry" "$(q "$Q")" "1000"

# ---- SUSET: only a superuser may change it ----------------------------------
psql_run "DROP ROLE IF EXISTS allow_nobody; CREATE ROLE allow_nobody LOGIN;"
check "premise: the role can open a session" \
	"$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U allow_nobody -d "$PGC_DB" -qtA -c 'SELECT 1' 2>&1)" "1"
check "a non-superuser cannot widen the list (42501)" \
	"$(sqlstate_of "SET pgcolumnar.objstore_allowed_endpoints = '10.0.0.1'" allow_nobody)" "42501"
# WITH THE CONFIGURED LIST EMPTY (#1341). It was still "other.example, 127.0.0.1" here,
# so the read worked whatever the session SET did, and the arm could not fail.
set_list ""
check_num "a superuser session SET works (admin override)" \
	"$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -qtA \
		-c "SET pgcolumnar.objstore_allowed_endpoints = '127.0.0.1'" -c "$Q" 2>/dev/null | tail -1)" "1000"

# ---- link-local: refused even when listed -----------------------------------
# The IMDS credential-theft address, explicitly IN the list: still 42501, and
# the SQLSTATE proves refusal happened BEFORE any connection existed (a real
# connect attempt to 169.254/16 would surface 08006 or hang toward a timeout).
set_list "169.254.169.254, 127.0.0.1"
check "link-local: refused 42501 despite being listed" \
	"$(sqlstate_of "SELECT count(*) FROM pgcolumnar.read_parquet('http://169.254.169.254:1/x.parquet') AS (a int8)")" "42501"
check "link-local: the refusal names the range, not the list" \
	"$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -qtA \
		-c "SELECT count(*) FROM pgcolumnar.read_parquet('http://169.254.169.254:1/x.parquet') AS (a int8)" 2>&1 \
		| grep -c "link-local")" "1"
check_num "link-local: the listed ordinary host still works beside it" "$(q "$Q")" "1000"

# ---- metadata addresses beyond 169.254/16 and fe80::/10 (#1808) -------------
# AWS serves IPv6 instance metadata at fd00:ec2::254, and a NAT64 path reaches
# 169.254.169.254 as 64:ff9b::a9fe:a9fe. A URL cannot carry an IPv6 literal, so a
# host NAME must resolve to these addresses. A small getaddrinfo() shim, loaded
# into the server with LD_PRELOAD, maps names under pgc-shim.invalid to fixed
# addresses and passes every other name to the real resolver. No DNS, no
# /etc/hosts edit. Each read gets statement_timeout 3s: without the guard a
# connect() to these addresses can hang, and 57014 then shows that it was tried.
SHIM_C="$PGC_WORKDIR/gai_shim.c"
SHIM_SO="$PGC_WORKDIR/gai_shim.so"
cat > "$SHIM_C" <<'CEOF'
#define _GNU_SOURCE
#include <dlfcn.h>
#include <netdb.h>
#include <stdlib.h>
#include <string.h>
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>

static const char *const map[][2] = {
	{"ok", "127.0.0.1"},
	{"imds6", "fd00:ec2::254"},
	{"nat64", "64:ff9b::a9fe:a9fe"},
	{"nat64local", "64:ff9b:1::a9fe:a9fe"},
	{"mapped", "::ffff:169.254.169.254"},
	{"compat", "::169.254.169.254"},
};

int
getaddrinfo(const char *node, const char *service,
			const struct addrinfo *hints, struct addrinfo **res)
{
	static const char sfx[] = ".pgc-shim.invalid";
	size_t		n = node ? strlen(node) : 0;

	if (n > sizeof(sfx) - 1 && strcmp(node + n - (sizeof(sfx) - 1), sfx) == 0)
	{
		size_t		len = n - (sizeof(sfx) - 1);

		for (size_t i = 0; i < sizeof(map) / sizeof(map[0]); i++)
		{
			int			v6 = strchr(map[i][1], ':') != NULL;
			size_t		salen = v6 ? sizeof(struct sockaddr_in6) : sizeof(struct sockaddr_in);
			struct addrinfo *ai;
			int			port = service ? atoi(service) : 0;

			if (strlen(map[i][0]) != len || strncmp(node, map[i][0], len) != 0)
				continue;
			/* one block, as glibc allocates it, so its freeaddrinfo() frees it */
			ai = calloc(1, sizeof(*ai) + salen);
			if (ai == NULL)
				return EAI_MEMORY;
			ai->ai_family = v6 ? AF_INET6 : AF_INET;
			ai->ai_socktype = SOCK_STREAM;
			ai->ai_protocol = IPPROTO_TCP;
			ai->ai_addrlen = salen;
			ai->ai_addr = (struct sockaddr *) (ai + 1);
			if (v6)
			{
				struct sockaddr_in6 *s6 = (struct sockaddr_in6 *) ai->ai_addr;

				s6->sin6_family = AF_INET6;
				s6->sin6_port = htons(port);
				inet_pton(AF_INET6, map[i][1], &s6->sin6_addr);
			}
			else
			{
				struct sockaddr_in *s4 = (struct sockaddr_in *) ai->ai_addr;

				s4->sin_family = AF_INET;
				s4->sin_port = htons(port);
				inet_pton(AF_INET, map[i][1], &s4->sin_addr);
			}
			*res = ai;
			return 0;
		}
		return EAI_NONAME;
	}
	return ((int (*) (const char *, const char *, const struct addrinfo *,
					  struct addrinfo **)) dlsym(RTLD_NEXT, "getaddrinfo"))
		(node, service, hints, res);
}
CEOF
SHIM_CC_OUT="$(cc -shared -fPIC -o "$SHIM_SO" "$SHIM_C" -ldl 2>&1)"
chmod 755 "$SHIM_SO" 2>/dev/null
check "premise: the resolver shim compiles" "${SHIM_CC_OUT:-ok}" "ok"
pgc_pg "LD_PRELOAD='$SHIM_SO' pg_ctl -D '$PGC_PGDATA' -l '$PGC_LOGFILE' restart -w" >/dev/null 2>&1
check "premise: the server restarts with the resolver shim" \
	"$(q "SELECT 'up'")" "up"

shim_read() {  # shim_read <name> -> the SQLSTATE of a read from that name, port 1
	sqlstate_of "SET statement_timeout = '3s'; SELECT count(*) FROM pgcolumnar.read_parquet('http://$1.pgc-shim.invalid:1/x.parquet') AS (a int8)"
}
set_list "ok.pgc-shim.invalid, imds6.pgc-shim.invalid, nat64.pgc-shim.invalid, nat64local.pgc-shim.invalid, mapped.pgc-shim.invalid, compat.pgc-shim.invalid"
check_num "premise: a shim name that maps to 127.0.0.1 reads the fixture" \
	"$(q "SELECT count(*) FROM pgcolumnar.read_parquet('http://ok.pgc-shim.invalid:$HTTP_PORT/fixture.parquet') AS (a int8)")" "1000"
check "IPv6 IMDS fd00:ec2::254: refused 42501 despite being listed" "$(shim_read imds6)" "42501"
check "NAT64 64:ff9b::a9fe:a9fe: refused 42501 despite being listed" "$(shim_read nat64)" "42501"
check "NAT64 local-use 64:ff9b:1::a9fe:a9fe: refused 42501 despite being listed" "$(shim_read nat64local)" "42501"
check "IPv4-mapped ::ffff:169.254.169.254: refused 42501 despite being listed" "$(shim_read mapped)" "42501"
check "IPv4-compatible ::169.254.169.254: refused 42501 despite being listed" "$(shim_read compat)" "42501"
IMDS6_OUT="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres -d "$PGC_DB" -qtA \
	-c "SET statement_timeout = '3s'" \
	-c "SELECT count(*) FROM pgcolumnar.read_parquet('http://imds6.pgc-shim.invalid:1/x.parquet') AS (a int8)" 2>&1)"
# The range when the DETAIL names it, else the whole first ERROR line as the measurement.
check "IPv6 IMDS: the refusal detail names fd00:ec2::/32" \
	"$(printf '%s\n' "$IMDS6_OUT" | grep -o 'fd00:ec2::/32' | head -1 | grep . \
		|| printf '%s\n' "$IMDS6_OUT" | grep -m1 'ERROR')" "fd00:ec2::/32"

pgc_summary
