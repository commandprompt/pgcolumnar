#!/usr/bin/env bash
#
# pgColumnar #619: ListObjectsV2 discovery over remote paths. The FDW and
# read_parquet expand a remote prefix (trailing slash) or a remote glob into the
# set of objects under it, by a paged ListObjectsV2 call behind the object-store
# ABI, and Hive partition columns work over a remote prefix.
#
# The fixture (test/objstore_http_server.py) answers ListObjectsV2 over the
# objects on disk, paged by --list-page-size, so the client's continuation-token
# loop is exercised. It verifies every request's SigV4 against an independent
# implementation, so a listing that reads back also proves the C signer signs the
# ?list-type=2&prefix=... query byte-for-byte.
#
# Usage:  test/objstore_listing.sh [PG_CONFIG]

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
export PGC_EXTRA_CONF="pgcolumnar.objstore_allowed_endpoints='127.0.0.1'"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

python3 -c 'import pyarrow' 2>/dev/null || pgc_skip pyarrow "pyarrow is required to write the fixture"

S3_PORT="$(pgc_pick_free_port "$PGC_AUX_PORT_LO" "$PGC_AUX_PORT_HI")"
S3_LOG="$PGC_WORKDIR/s3.log"
SRV_PID=""
AKID="PGCTESTKEYID"
SECRET="pgctest-secret-shhh"
REGION="pgc-test-1"
BUCKET="pgc-bucket"

listing_teardown() {
	[ -n "$SRV_PID" ] && kill "$SRV_PID" 2>/dev/null
	pgc_teardown
}
trap listing_teardown EXIT INT TERM

# wait_up: wait up to 15 s for the server, so a backend that crashed in one reading
# fails that reading and survival, not every reading after it during recovery.
wait_up() { for _ in $(seq 1 30); do [ -n "$(q 'SELECT 1')" ] && return 0; sleep 0.5; done; }

# pg_restart_env ENV... -> 0 once the cluster answers again with ENV in its
# environment, 1 when it does not; it never exits the suite (#1850). It waits out
# a crash recovery first: a fast stop sent while the startup process replays WAL
# did not finish in pg_ctl's 60 s, and the start then found the old postmaster.
# The caller records the failure by name, so the arms before it still report.
pg_restart_env() {
	wait_up
	pgc_pg "pg_ctl -D '$PGC_PGDATA' stop -m fast -w" >/dev/null 2>&1
	pgc_pg "$* pg_ctl -D '$PGC_PGDATA' -l '$PGC_LOGFILE' start -w" >/dev/null 2>&1
	for _ in $(seq 1 30); do
		[ -n "$(q 'SELECT 1')" ] && return 0
		sleep 0.5
	done
	return 1
}
listn() { grep -cE 'list-type=2' "$S3_LOG" | tr -d ' '; }

# refusal_of SQL PHRASE LABEL -> "<SQLSTATE><LABEL>" when SQL raised with PHRASE in its
# message, the bare SQLSTATE when the message lacks it, or what happened instead. The
# two non-progress refusals share 08P01: with the no-keys check removed, the replayed
# page reached the token check and the bare SQLSTATE still passed.
refusal_of() {
	local out state
	wait_up
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
# alone passed with a backend aborted before it; survival also requires this count
# not to have moved.
crashes() {
	local _n
	_n="$(pgc_pg "grep -c 'terminated by signal' '$PGC_LOGFILE'" 2>/dev/null)"
	case "$_n" in '' | *[!0-9]*) ;; *) echo "$_n" ;; esac
}
survived() {	# survived BEFORE -> "1" when the server answers and no backend died since
	local _up _now
	wait_up
	_up="$(q 'SELECT 1')"; _now="$(crashes)"
	if ! [[ "$1" =~ ^[0-9]+$ && "$_now" =~ ^[0-9]+$ ]]; then echo "no (crash count unreadable)"
	elif [ "$_up" = 1 ] && [ "$_now" = "$1" ]; then echo 1
	else echo "no ([$_up], $(( _now - $1 )) backend crashes)"; fi
}

# qe SQL -> the rows as q prints them, or the first ERROR line, so a failed reading
# says why rather than comparing as empty.
qe() {
	local out
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "$1" 2>&1)"
	if grep -q '^ERROR:' <<<"$out"; then grep -m1 '^ERROR:' <<<"$out"; else echo "$out"; fi
}

# ---- fixtures on disk: a prefix of parts, a nested part, hidden + non-parquet,
#      a Hive-partitioned layout, and #1784's named hidden directories ---------
mkdir -p "$PGC_WORKDIR/$BUCKET/lst/sub" \
         "$PGC_WORKDIR/$BUCKET/hive/dt=2026-01-01" \
         "$PGC_WORKDIR/$BUCKET/hive/dt=2026-01-02" \
         "$PGC_WORKDIR/$BUCKET/_landing/_temporary" \
         "$PGC_WORKDIR/$BUCKET/.dir" \
         "$PGC_WORKDIR/$BUCKET/glb"
python3 - "$PGC_WORKDIR/$BUCKET" <<'PYEOF'
import sys, os
import pyarrow as pa, pyarrow.parquet as pq
B = sys.argv[1]
def one(path, ids):
    pq.write_table(pa.table({"id": pa.array(ids, pa.int64())}), path)
# three top-level parts (id 0..2, 10..12, 20..22) + one nested (30..32)
one(os.path.join(B, "lst/part-0000.parquet"), [0, 1, 2])
one(os.path.join(B, "lst/part-0001.parquet"), [10, 11, 12])
one(os.path.join(B, "lst/part-0002.parquet"), [20, 21, 22])
one(os.path.join(B, "lst/sub/part-0003.parquet"), [30, 31, 32])
# a hidden marker and a non-parquet object that must be excluded
open(os.path.join(B, "lst/_SUCCESS"), "w").close()
open(os.path.join(B, "lst/notes.txt"), "w").write("not parquet\n")
# Hive: two partitions, each one object
one(os.path.join(B, "hive/dt=2026-01-01/part.parquet"), [1, 2, 3])
one(os.path.join(B, "hive/dt=2026-01-02/part.parquet"), [4, 5, 6])
# #1784: a prefix the user names may itself start with '_' or '.'; the hidden
# rule applies only below it (_temporary/ under _landing/ is still skipped)
one(os.path.join(B, "_landing/a.parquet"), [1, 2, 3])
one(os.path.join(B, "_landing/_temporary/t.parquet"), [99])
one(os.path.join(B, ".dir/a.parquet"), [4, 5, 6])
# a glob matches '_' names, and a dot name only when the pattern spells the dot
one(os.path.join(B, "glb/a.parquet"), [1, 2, 3])
one(os.path.join(B, "glb/_b.parquet"), [10, 11, 12, 13, 14])
one(os.path.join(B, "glb/.h.parquet"), [100])
PYEOF

# page size 2 forces the continuation-token loop for the 4-object prefix
python3 "$(dirname "${BASH_SOURCE[0]}")/objstore_http_server.py" \
	--dir "$PGC_WORKDIR" --port "$S3_PORT" --log "$S3_LOG" \
	--sigv4-key "$AKID" --sigv4-secret "$SECRET" --sigv4-region "$REGION" \
	--list-page-size 2 > "$PGC_WORKDIR/s3_server.out" 2>&1 &
SRV_PID=$!
for _ in $(seq 1 50); do
	grep -q READY "$PGC_WORKDIR/s3_server.out" 2>/dev/null && break; sleep 0.1
done
check "premise: fixture up" "$(grep -c READY "$PGC_WORKDIR/s3_server.out" 2>/dev/null)" "1"

pg_restart_env "AWS_ENDPOINT_URL='http://127.0.0.1:$S3_PORT'" \
	"AWS_ACCESS_KEY_ID='$AKID'" "AWS_SECRET_ACCESS_KEY='$SECRET'" "AWS_REGION='$REGION'" \
	|| { echo "FATAL: cluster did not come back after env restart"; exit 1; }
CRASHES_BEFORE="$(crashes)"

psql_run "CREATE SERVER s3srv FOREIGN DATA WRAPPER pgcolumnar_parquet;"

# ---- read_parquet over a remote directory prefix (recursive) ----------------
: > "$S3_LOG"
check "read_parquet over a prefix reads every object under it (4 parts, recursive)" \
	"$(q "SELECT count(*), sum(id) FROM pgcolumnar.read_parquet('s3://$BUCKET/lst/') AS t(id int8);" | tr '|' ' ')" \
	"12 192"
check "the prefix listing paged (more than one list-type=2 request)" \
	"$([ "$(listn)" -ge 2 ] && echo paged)" "paged"

# ---- the FDW over the same prefix -------------------------------------------
psql_run "CREATE FOREIGN TABLE fprefix (id int8) SERVER s3srv OPTIONS (path 's3://$BUCKET/lst/');"
check "FDW over a prefix == read_parquet over the prefix" \
	"$(pgc_set_hash "SELECT * FROM fprefix")" \
	"$(pgc_set_hash "SELECT * FROM pgcolumnar.read_parquet('s3://$BUCKET/lst/') AS t(id int8)")"
check "hidden (_SUCCESS) and non-parquet (notes.txt) objects are excluded" \
	"$(q "SELECT count(*) FROM fprefix")" "12"

# ---- a remote glob applies segment by segment (top level only) --------------
check "glob s3://.../lst/*.parquet matches the 3 top-level parts, not sub/" \
	"$(q "SELECT count(*), sum(id) FROM pgcolumnar.read_parquet('s3://$BUCKET/lst/*.parquet') AS t(id int8);" | tr '|' ' ')" \
	"9 99"

# ---- #1784: the hidden rule applies below the named prefix only, and a glob
#      matches as a local glob() does ----------------------------------------
# A local directory skips '_' and '.' names it finds below the path you named, not
# the path itself; a local glob matches '_' names and skips a dot name unless the
# pattern spells the dot. Remote reads give the same answer.
rpq() { qe "SELECT count(*), sum(id) FROM pgcolumnar.read_parquet('s3://$BUCKET/$1') AS t(id int8);" | tr '|' ' '; }
check "a prefix whose own name starts with _ lists the objects below it, still skipping _temporary/" \
	"$(rpq _landing/)" "3 6"
check "a prefix whose own name starts with . lists the objects below it" \
	"$(rpq .dir/)" "3 15"
check "glob under a named _ directory (_landing/*.parquet) matches" \
	"$(rpq '_landing/*.parquet')" "3 6"
check "glob under a named . directory (.dir/*.parquet) matches" \
	"$(rpq '.dir/*.parquet')" "3 15"
check "glob glb/*.parquet matches _b.parquet but not .h.parquet, as a local glob does" \
	"$(rpq 'glb/*.parquet')" "8 66"
check "glob glb/_*.parquet matches the _ file it spells" \
	"$(rpq 'glb/_*.parquet')" "5 60"
check "glob glb/.*.parquet matches the dot file it spells" \
	"$(rpq 'glb/.*.parquet')" "1 100"
check "prefix glb/ still skips _b.parquet and .h.parquet below it" \
	"$(rpq glb/)" "3 6"
# The module accepts an upper-case scheme and lists keys back as s3://, so the
# scheme is compared without case, for a prefix and for a pattern alike.
rpqu() { qe "SELECT count(*), sum(id) FROM pgcolumnar.read_parquet('S3://$BUCKET/$1') AS t(id int8);" | tr '|' ' '; }
check "an upper-case scheme prefix S3://.../_landing/ reads as s3:// does" \
	"$(rpqu _landing/)" "3 6"
check "an upper-case scheme glob S3://.../_landing/*.parquet matches as s3:// does" \
	"$(rpqu '_landing/*.parquet')" "3 6"
# Only the scheme ignores case. After it, a pattern matches with case, as a
# local glob() and S3 keys do: [A_] matches _b.parquet (5 rows, sum 60) and
# not a.parquet.
check "an upper-case scheme glob S3://.../glb/[A_]*.parquet is case-sensitive after the scheme" \
	"$(rpqu 'glb/[A_]*.parquet')" "5 60"

# ---- an exact key is still one GET, no LIST ---------------------------------
: > "$S3_LOG"
psql_run "CREATE FOREIGN TABLE fexact (id int8) SERVER s3srv OPTIONS (path 's3://$BUCKET/lst/part-0000.parquet');"
check "exact-key read returns its rows" "$(q "SELECT count(*), sum(id) FROM fexact" | tr '|' ' ')" "3 3"
check "an exact-key read issues no ListObjectsV2" "$(listn)" "0"
# the 0 above reads the same log: it must hold the exact key's own request
check "and the request log recorded the exact-key read" \
	"$(grep -qE '^(GET|HEAD) /'"$BUCKET"'/lst/part-0000\.parquet ' "$S3_LOG" && echo logged || echo "not logged")" "logged"

# ---- Hive partition columns over a remote prefix ----------------------------
psql_run "CREATE FOREIGN TABLE fhive (id int8, dt date) SERVER s3srv
          OPTIONS (path 's3://$BUCKET/hive/', partition_columns 'dt');"
check "Hive over a remote prefix stamps the partition column" \
	"$(q "SELECT dt, count(*) FROM fhive GROUP BY dt ORDER BY dt;" | tr '\n|' ' ')" \
	"2026-01-01 3 2026-01-02 3 "
check "a partition predicate prunes a remote file (Files Pruned = 1)" \
	"$(q "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF)
	      SELECT count(*) FROM fhive WHERE dt = DATE '2026-01-01';" \
		| grep 'Files Pruned' | grep -oE '[0-9]+' | head -1)" "1"

# ---- a non-advancing continuation token is refused, not looped forever ------
# A broken or hostile endpoint that replays one truncated page with the same
# token would spin the paging loop toward the page cap. The client detects the
# token that does not advance and errors (08P01) instead, by that check's own
# message. Removal proof: without that check the read loops until
# statement_timeout (57014), not 08P01.
cat > "$PGC_WORKDIR/__listing_override__" <<'XML'
<?xml version="1.0"?><ListBucketResult><IsTruncated>true</IsTruncated><NextContinuationToken>STUCK</NextContinuationToken><Contents><Key>lst/part-0000.parquet</Key></Contents></ListBucketResult>
XML
check "a non-advancing continuation token is refused, not an infinite paging loop" \
	"$(refusal_of "SET statement_timeout='10s'; SELECT count(*) FROM pgcolumnar.read_parquet('s3://$BUCKET/lst/') AS t(id int8)" \
		"returned a non-advancing continuation token" " by the token check")" "08P01 by the token check"
rm -f "$PGC_WORKDIR/__listing_override__"

# A truncated page that carries NO keys is the other non-progress shape: an
# endpoint that varies its token each page (which the token check cannot catch)
# but never returns a key would otherwise page to the OS_LIST_MAX_PAGES backstop.
# ListObjectsV2 always returns a key on a truncated page, so an empty truncated
# page is refused on the first page (08P01), by the progress check's own message:
# this page replays its token, so without the progress check the token check
# refuses it on the second page with the same SQLSTATE.
cat > "$PGC_WORKDIR/__listing_override__" <<'XML'
<?xml version="1.0"?><ListBucketResult><IsTruncated>true</IsTruncated><NextContinuationToken>next-1</NextContinuationToken></ListBucketResult>
XML
check "a truncated page with no keys is refused, not looped" \
	"$(refusal_of "SET statement_timeout='10s'; SELECT count(*) FROM pgcolumnar.read_parquet('s3://$BUCKET/lst/') AS t(id int8)" \
		"returned a truncated page with no keys" " by the progress check")" "08P01 by the progress check"
rm -f "$PGC_WORKDIR/__listing_override__"

# ---- a LIST that fails is refused, never read as an empty prefix -------------
# An empty listing is a legal answer (a prefix with nothing under it), so a client
# that took a failed LIST for one would read zero rows from a prefix it never saw.
echo 500 > "$PGC_WORKDIR/__listing_status__"
check "a LIST that returns HTTP 500 is refused, not read as an empty prefix" \
	"$(refusal_of "SET statement_timeout='10s'; SELECT count(*) FROM pgcolumnar.read_parquet('s3://$BUCKET/lst/') AS t(id int8)" \
		"returned HTTP 500" " by the HTTP status check")" "08006 by the HTTP status check"
rm -f "$PGC_WORKDIR/__listing_status__"

check "backend still up after the listing refusals" "$(survived "$CRASHES_BEFORE")" "1"

# ---- optional: a real S3 implementation (Garage), as the sibling suites (#1798)
# The fixture above answers ListObjectsV2 from one Python handler, two keys a page.
# These arms put a layout on a real S3 implementation under a per-run prefix and
# read every remote prefix and glob against the same layout read from local disk,
# so the hidden-name rules, glob matching and paging meet a real ListObjectsV2.
# page/ holds 1100 one-row objects: a real server caps a page at 1000 keys, and the
# client has no max-keys setting, so 1100 rows back means it followed a real
# continuation token. The virtual-host arm needs an endpoint whose bucket
# subdomain resolves (Garage: a root_domain such as .s3.garage.localhost), named
# by PGC_S3_INTEGRATION_VHOST_ENDPOINT; without one it records nothing.
if [ -n "${PGC_S3_INTEGRATION_ENDPOINT:-}" ]; then
	IB="${PGC_S3_INTEGRATION_BUCKET:-pgcolumnar-test}"
	IRUN="lst-$$"
	INT="$PGC_WORKDIR/int"
	s3cli() {
		env AWS_ACCESS_KEY_ID="$PGC_S3_INTEGRATION_KEY" \
			AWS_SECRET_ACCESS_KEY="$PGC_S3_INTEGRATION_SECRET" \
			AWS_DEFAULT_REGION="${PGC_S3_INTEGRATION_REGION:-garage}" \
			aws --endpoint-url "$1" "${@:2}"
	}
	# s3firstpage PREFIX -> "KeyCount IsTruncated" of ListObjectsV2's first page
	# under PREFIX ("1000 True"), or why there is none
	s3firstpage() {
		local x n t
		if ! x="$(curl -sS --aws-sigv4 "aws:amz:${PGC_S3_INTEGRATION_REGION:-garage}:s3" \
			--user "$PGC_S3_INTEGRATION_KEY:$PGC_S3_INTEGRATION_SECRET" \
			"$PGC_S3_INTEGRATION_ENDPOINT/$IB?list-type=2&prefix=$1" 2>&1)"
		then
			echo "no listing (curl failed: $(printf '%s' "$x" | tr '\n' ' ' | cut -c1-200))"
			return
		fi
		n="$(printf '%s' "$x" | sed -n 's:.*<KeyCount>\([0-9]*\)</KeyCount>.*:\1:p')"
		t="$(printf '%s' "$x" | sed -n 's:.*<IsTruncated>\([a-z]*\)</IsTruncated>.*:\1:p')"
		case "$t" in true) t=True ;; false) t=False ;; esac
		if [ -n "$n" ] && [ -n "$t" ]; then
			echo "$n $t"
		else
			echo "no KeyCount and IsTruncated in the reply: $(printf '%s' "$x" | tr '\n' ' ' | cut -c1-200)"
		fi
	}
	mkdir -p "$INT"
	cp -a "$PGC_WORKDIR/$BUCKET/lst" "$PGC_WORKDIR/$BUCKET/_landing" \
		"$PGC_WORKDIR/$BUCKET/.dir" "$PGC_WORKDIR/$BUCKET/glb" "$INT/"
	python3 - "$INT" <<'PYEOF'
import sys, os
import pyarrow as pa, pyarrow.parquet as pq
B = sys.argv[1]
def one(path, ids):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pq.write_table(pa.table({"id": pa.array(ids, pa.int64())}), path)
# hidden names beside one part: a marker, a _temporary/ directory, a dot file
one(os.path.join(B, "hid/part.parquet"), [1, 2, 3])
open(os.path.join(B, "hid/_SUCCESS"), "w").close()
one(os.path.join(B, "hid/_temporary/x.parquet"), [99])
one(os.path.join(B, "hid/.hidden.parquet"), [100])
# 1100 one-row objects, more than one real 1000-key page
for i in range(1100):
    one(os.path.join(B, "page/k-%04d.parquet" % i), [i])
PYEOF
	if pg_restart_env "AWS_ENDPOINT_URL='$PGC_S3_INTEGRATION_ENDPOINT'" \
		"AWS_ACCESS_KEY_ID='$PGC_S3_INTEGRATION_KEY'" \
		"AWS_SECRET_ACCESS_KEY='$PGC_S3_INTEGRATION_SECRET'" \
		"AWS_REGION='${PGC_S3_INTEGRATION_REGION:-garage}'"
	then int_up=yes; else int_up="no (the cluster did not answer SELECT 1 within 15 s of the restart)"; fi
	check "integration: the cluster restarts with the real S3 credentials" "$int_up" "yes"
	IHOST="$(echo "${PGC_S3_INTEGRATION_ENDPOINT#*://}" | cut -d/ -f1 | cut -d: -f1)"
	pgc_pg "echo \"pgcolumnar.objstore_allowed_endpoints='127.0.0.1,$IHOST'\" >> '$PGC_PGDATA/postgresql.conf'"
	q "SELECT pg_reload_conf()" >/dev/null
	if s3cli "$PGC_S3_INTEGRATION_ENDPOINT" s3 cp --recursive --only-show-errors \
		"$INT" "s3://$IB/$IRUN" >/dev/null 2>&1
	then
		# rd URL... -> each URL's "count sum", joined by "; ": the remote and local
		# readings of one check are the same list over two roots
		rd() {
			local u out=""
			for u in "$@"; do
				out="$out${out:+; }$(qe "SELECT count(*), sum(id) FROM pgcolumnar.read_parquet('$u') AS t(id int8);" | tr '|' ' ')"
			done
			echo "$out"
		}
		R="s3://$IB/$IRUN"
		check_text "integration: real S3 prefix read == local directory" \
			"$(pgc_set_hash "SELECT * FROM pgcolumnar.read_parquet('$R/lst/') AS t(id int8)")" \
			"$(pgc_set_hash "SELECT * FROM pgcolumnar.read_parquet('$INT/lst/') AS t(id int8)")"
		check_text "integration: real S3 glob == local glob" \
			"$(rd "$R/lst/*.parquet" "$R/glb/*.parquet" "$R/glb/_*.parquet" "$R/glb/.*.parquet")" \
			"$(rd "$INT/lst/*.parquet" "$INT/glb/*.parquet" "$INT/glb/_*.parquet" "$INT/glb/.*.parquet")"
		check_text "integration: real S3 skips hidden names like a local directory" \
			"$(rd "$R/hid/" "$R/glb/")" "$(rd "$INT/hid/" "$INT/glb/")"
		check_text "integration: real S3 prefix under a _ or . directory == local" \
			"$(rd "$R/_landing/" "$R/.dir/" "$R/_landing/*.parquet" "$R/.dir/*.parquet")" \
			"$(rd "$INT/_landing/" "$INT/.dir/" "$INT/_landing/*.parquet" "$INT/.dir/*.parquet")"
		# the server's own first page, read by another client: the premise that
		# 1100 keys cannot come back in one page. curl signs the ListObjectsV2
		# request itself (--aws-sigv4), since `aws s3api` fails to start on some
		# aws-cli 2 / Python 3.14 installs ("badly formed help string")
		check "integration: the real S3 serves the 1100 keys in pages of at most 1000" \
			"$(s3firstpage "$IRUN/page/")" "1000 True"
		check "integration: real S3 listing pages past 1000 keys" \
			"$(rd "$R/page/")" "1100 604450"
		VEP="${PGC_S3_INTEGRATION_VHOST_ENDPOINT:-}"
		VHOST="$(echo "${VEP#*://}" | cut -d/ -f1 | cut -d: -f1)"
		if [ -n "$VEP" ] && getent hosts "$IB.$VHOST" >/dev/null 2>&1; then
			pgc_pg "echo \"pgcolumnar.objstore_allowed_endpoints='127.0.0.1,$IHOST,$VHOST'\" >> '$PGC_PGDATA/postgresql.conf'"
			if pg_restart_env "AWS_ENDPOINT_URL='$VEP'" \
				"AWS_ACCESS_KEY_ID='$PGC_S3_INTEGRATION_KEY'" \
				"AWS_SECRET_ACCESS_KEY='$PGC_S3_INTEGRATION_SECRET'" \
				"AWS_REGION='${PGC_S3_INTEGRATION_REGION:-garage}'"
			then v_up=yes; else v_up="no (the cluster did not answer SELECT 1 within 15 s of the restart)"; fi
			check "integration: the cluster restarts with the virtual-host endpoint" "$v_up" "yes"
			VSQL="SELECT count(*), sum(id) FROM pgcolumnar.read_parquet('$R/lst/') AS t(id int8);"
			check_text "integration: real S3 virtual-host prefix read == path-style" \
				"$(qe "SET pgcolumnar.objstore_s3_addressing = 'virtual'; $VSQL" | grep -v '^SET$' | tr '|' ' ')" \
				"$(qe "SET pgcolumnar.objstore_s3_addressing = 'path'; $VSQL" | grep -v '^SET$' | tr '|' ' ')"
		else
			echo "note: PGC_S3_INTEGRATION_VHOST_ENDPOINT unset, or $IB.<its host> does not" \
				"resolve; the virtual-host arm is not run"
		fi
		s3cli "$PGC_S3_INTEGRATION_ENDPOINT" s3 rm --recursive --only-show-errors \
			"s3://$IB/$IRUN/" >/dev/null 2>&1 || echo "note: could not remove s3://$IB/$IRUN/"
	else
		echo "note: integration endpoint set but the upload helper failed;" \
			"the fixture arms above carry the proof alone"
	fi
else
	echo "note: PGC_S3_INTEGRATION_ENDPOINT unset; the fixture arms above carry the proof alone"
fi

pgc_summary
