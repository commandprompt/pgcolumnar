#!/usr/bin/env bash
#
# pgColumnar ListObjectsV2 listing-parser fuzzer (#619).
#
# objstore_listing.sh is the differential suite over valid listings. This is the
# other half: it feeds the C listing parser (os_list_parse_page / os_xml_* in the
# object-store module) MALFORMED ListObjectsV2 XML and asserts one property,
#
#     a malformed listing makes the backend raise an ERROR, never die.
#
# That parser is the third hand-rolled parser over outside-controlled input, the
# shape that produced #210 and #228; it wears the columnar_thrift.c bounds
# discipline, and this is the machine that checks it. The fixture serves the
# mutant verbatim through its __listing_override__ hook, so the mutant reaches
# the parser exactly as a hostile endpoint's response would.
#
# Anything that is not a clean ERROR or a clean accept is a finding:
#   crash   the backend died
#   san     a sanitizer reported (only on a sanitizer build)
#   hang    the statement outlived its timeout
# Every mutant is a pure function of one integer seed (test/listing_corpus.py),
# so a finding reproduces exactly.
#
# Usage:  test/fuzz_listing.sh [PG_CONFIG]
#   PGC_SEED=<int>   base seed (default 20260814)
#   PGC_ITERS=<int>  mutants to run (default 200)

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
export PGC_EXTRA_CONF="pgcolumnar.objstore_allowed_endpoints='127.0.0.1'"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

python3 -c 'import pyarrow' 2>/dev/null || pgc_skip pyarrow "pyarrow is required to write the fixture"

SEED="${PGC_SEED:-20260814}"
ITERS="${PGC_ITERS:-200}"
CORPUS="$(dirname "${BASH_SOURCE[0]}")/listing_corpus.py"
S3_PORT="$(pgc_pick_free_port "$PGC_AUX_PORT_LO" "$PGC_AUX_PORT_HI")"
S3_LOG="$PGC_WORKDIR/s3.log"
OVERRIDE="$PGC_WORKDIR/__listing_override__"
NEWLOG="$PGC_WORKDIR/newlog.txt"
SRV_PID=""
AKID="PGCTESTKEYID"; SECRET="pgctest-secret"; REGION="pgc-test-1"; BUCKET="pgc-bucket"

fuzz_listing_teardown() {
	rm -f "$OVERRIDE"
	[ -n "$SRV_PID" ] && kill "$SRV_PID" 2>/dev/null
	pgc_teardown
}
trap fuzz_listing_teardown EXIT INT TERM

pg_restart_env() {
	pgc_pg "pg_ctl -D '$PGC_PGDATA' stop -m fast -w" >/dev/null 2>&1
	pgc_pg "$* pg_ctl -D '$PGC_PGDATA' -l '$PGC_LOGFILE' start -w" >/dev/null 2>&1
	for _ in $(seq 1 30); do [ -n "$(q 'SELECT 1')" ] && return 0; sleep 0.5; done
	echo "FATAL: cluster did not restart"; exit 1
}

# a real object the valid corpus key points at, so the control read returns rows
mkdir -p "$PGC_WORKDIR/$BUCKET/lst"
python3 - "$PGC_WORKDIR/$BUCKET/lst/real.parquet" <<'PY'
import sys, pyarrow as pa, pyarrow.parquet as pq
pq.write_table(pa.table({"id": pa.array([7, 8, 9], pa.int64())}), sys.argv[1])
PY

python3 "$(dirname "${BASH_SOURCE[0]}")/objstore_http_server.py" \
	--dir "$PGC_WORKDIR" --port "$S3_PORT" --log "$S3_LOG" \
	--sigv4-key "$AKID" --sigv4-secret "$SECRET" --sigv4-region "$REGION" \
	> "$PGC_WORKDIR/s3_server.out" 2>&1 &
SRV_PID=$!
for _ in $(seq 1 50); do grep -q READY "$PGC_WORKDIR/s3_server.out" 2>/dev/null && break; sleep 0.1; done
check "premise: fixture up" "$(grep -c READY "$PGC_WORKDIR/s3_server.out" 2>/dev/null)" "1"

pg_restart_env "AWS_ENDPOINT_URL='http://127.0.0.1:$S3_PORT'" \
	"AWS_ACCESS_KEY_ID='$AKID'" "AWS_SECRET_ACCESS_KEY='$SECRET'" "AWS_REGION='$REGION'"

READ="SELECT count(*) FROM pgcolumnar.read_parquet('s3://$BUCKET/lst/') AS t(id int8)"

# control: a VALID override lists lst/real.parquet, so the read returns its rows.
# This proves the parser is actually reached; without it a run in which nothing
# parsed could pass the three "no finding" checks vacuously.
python3 "$CORPUS" valid "$OVERRIDE"
check "control: a valid listing reaches the parser and reads the object" \
	"$(q "$READ")" "3"

LOGPOS=0
crashes=0; hangs=0; sanitizer=0; errors=0; clean=0; fresh=0
log_since() {
	local sz
	sz=$(stat -c %s "$PGC_LOGFILE" 2>/dev/null || echo 0)
	if [ "$sz" -gt "$LOGPOS" ]; then tail -c +$((LOGPOS + 1)) "$PGC_LOGFILE" > "$NEWLOG" 2>/dev/null
	else : > "$NEWLOG"; fi
	LOGPOS="$sz"
}
wait_for_cluster() {
	for _ in $(seq 1 60); do
		env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
			-d "$PGC_DB" -Atc "SELECT 1" >/dev/null 2>&1 && return 0
		sleep 0.5
	done
	return 1
}

log_since
echo "-- fuzzing $ITERS listing mutants (seed base $SEED)"
for ((i = 0; i < ITERS; i++)); do
	s=$((SEED + i))
	# The override is removed first: with no override the fixture serves the real
	# listing, so a generator that writes nothing read as a clean accept every time and
	# every check below passed with no mutant ever generated.
	rm -f "$OVERRIDE"
	python3 "$CORPUS" "$s" "$OVERRIDE" 2>/dev/null
	[ -f "$OVERRIDE" ] && fresh=$((fresh + 1))
	log_since
	out="$(timeout 30 env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" \
		-U postgres -d "$PGC_DB" -v ON_ERROR_STOP=0 \
		-c "SET statement_timeout='20s'; $READ" 2>&1)"
	rc=$?
	if grep -qE 'could not connect|Connection refused' <<<"$out" && ! grep -q '^ERROR:' <<<"$out"; then
		wait_for_cluster || { echo "FATAL: cluster gone"; break; }
	fi
	log_since
	newlog="$(cat "$NEWLOG" 2>/dev/null)"
	if grep -qE 'AddressSanitizer|runtime error:|UndefinedBehaviorSanitizer|LeakSanitizer' <<<"$newlog"; then
		sanitizer=$((sanitizer + 1)); echo "  FINDING [san] seed=$s"; echo "$newlog" | head -5 | sed 's/^/      /'; wait_for_cluster || true
	elif grep -qE 'was terminated by signal|server process .* exited with|crashed' <<<"$newlog"; then
		crashes=$((crashes + 1)); echo "  FINDING [crash] seed=$s"; echo "$newlog" | head -5 | sed 's/^/      /'; wait_for_cluster || true
	elif [ "$rc" = 124 ] || grep -q 'statement timeout' <<<"$out"; then
		hangs=$((hangs + 1)); echo "  FINDING [hang] seed=$s"; wait_for_cluster || true
	elif grep -qE 'server closed the connection|connection to server was lost' <<<"$out"; then
		crashes=$((crashes + 1)); echo "  FINDING [crash] seed=$s"; wait_for_cluster || true
	elif grep -q '^ERROR:' <<<"$out"; then
		errors=$((errors + 1))
	else
		clean=$((clean + 1))
	fi
done
rm -f "$OVERRIDE"
echo "-- done: $ITERS mutants, $errors rejected with ERROR, $clean accepted clean"

check "no mutant crashed the backend" "$crashes" "0"
check "no mutant hung the parser" "$hangs" "0"
check "no mutant tripped a sanitizer" "$sanitizer" "0"
# anti-false-green: every mutant reached psql, and the parser genuinely ran
# (the control above proved the pipeline; here the count must add up).
check "every mutant ran (errors + clean == iters)" \
	"$((errors + clean))" "$ITERS"
check "every iteration wrote a fresh mutant" "$fresh" "$ITERS"
check "the corpus reached the parser at all (some mutant errored or accepted)" \
	"$([ "$((errors + clean))" -gt 0 ] && echo yes || echo no)" "yes"

pgc_summary
