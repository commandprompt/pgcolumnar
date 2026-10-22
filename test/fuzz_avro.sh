#!/usr/bin/env bash
#
# pgColumnar Avro manifest decoder fuzzer (#388 step 1).
#
# avro_manifest.sh is the differential suite over a valid manifest. This is the
# other half: it feeds the C decoder (columnar_avro.c) MALFORMED Avro manifests
# and asserts one property,
#
#     a malformed manifest makes the backend raise an ERROR, never die.
#
# The decoder is a hand-rolled reader over bytes an outside writer produced, the
# category that produced #210 and #228 in the Parquet decoder, so it earns the
# fuzzer and the check_stack_depth guard from day one. Every mutant is a pure
# function of one integer seed (avro_corpus.py) over the committed real
# pyiceberg manifest, so a finding reproduces exactly.
#
# Anything that is not a clean ERROR or a clean accept is a finding:
#   crash   the backend died
#   san     a sanitizer reported (only on a sanitizer build)
#   hang    the statement outlived its timeout
#
# Usage:  test/fuzz_avro.sh [PG_CONFIG]
#   PGC_SEED=<int>   base seed (default 20260814)
#   PGC_ITERS=<int>  mutants to run (default 300)

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

FX="$(dirname "${BASH_SOURCE[0]}")/fixtures/iceberg"
CORPUS="$(dirname "${BASH_SOURCE[0]}")/avro_corpus.py"
[ -f "$FX/manifest-0.avro" ] || pgc_skip fixture "iceberg manifest fixtures are missing"
python3 -c 'import hashlib' 2>/dev/null || pgc_skip python "python3 is required"

SEED="${PGC_SEED:-20260814}"
ITERS="${PGC_ITERS:-300}"
BASE="$FX/manifest-0.avro"
BASE_LIST="$FX/manifest-list.avro"
MUT="$PGC_WORKDIR/mutant.avro"
NEWLOG="$PGC_WORKDIR/newlog.txt"

# two decode targets share one Avro reader: the manifest (manifest_entry) and the
# manifest LIST (manifest_file). Alternate mutants between them by seed parity so
# the fuzzer exercises both projections, not just the manifest one.
RD_MANIFEST="SELECT count(*) FROM pgcolumnar.read_avro_manifest('$MUT')"
RD_LIST="SELECT count(*) FROM pgcolumnar.read_manifest_list('$MUT')"

# control: each unmutated file decodes to its entries, proving both decoders are
# reached (without this the "no finding" checks could pass vacuously).
python3 "$CORPUS" "$BASE" valid "$MUT"; chmod 644 "$MUT"
check "control: the valid manifest reaches the decoder (2 entries)" \
	"$(q "$RD_MANIFEST")" "2"
python3 "$CORPUS" "$BASE_LIST" valid "$MUT"; chmod 644 "$MUT"
check "control: the valid manifest-list reaches the decoder (1 entry)" \
	"$(q "$RD_LIST")" "1"

LOGPOS=0
crashes=0; hangs=0; sanitizer=0; errors=0; clean=0; fresh=0; fileerr=0
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
echo "-- fuzzing $ITERS Avro manifest mutants (seed base $SEED)"
for ((i = 0; i < ITERS; i++)); do
	s=$((SEED + i))
	if (( i % 2 == 0 )); then base="$BASE"; RD="$RD_MANIFEST"
	else base="$BASE_LIST"; RD="$RD_LIST"; fi
	# The mutant is removed first, so a generator that writes nothing leaves no file
	# rather than the previous one: a stale file read as a clean accept or a decoder
	# ERROR made every check below pass with no mutant ever generated.
	rm -f "$MUT"
	python3 "$CORPUS" "$base" "$s" "$MUT" 2>/dev/null; chmod 644 "$MUT" 2>/dev/null
	[ -f "$MUT" ] && fresh=$((fresh + 1))
	log_since
	out="$(timeout 30 env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" \
		-U postgres -d "$PGC_DB" -v ON_ERROR_STOP=0 \
		-c "SET statement_timeout='20s'; $RD" 2>&1)"
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
		# an ERROR opening the file never reached the decoder
		grep -qE 'could not open|No such file|Permission denied' <<<"$out" && fileerr=$((fileerr + 1))
	else
		clean=$((clean + 1))
	fi
done
echo "-- done: $ITERS mutants, $errors rejected with ERROR, $clean accepted clean"

check "no mutant crashed the backend" "$crashes" "0"
check "no mutant hung the decoder" "$hangs" "0"
check "no mutant tripped a sanitizer" "$sanitizer" "0"
check "every mutant ran (errors + clean == iters)" "$((errors + clean))" "$ITERS"
check "every iteration wrote a fresh mutant" "$fresh" "$ITERS"
# Reached means decoded: a file the backend could not open never reached it.
check "the corpus reached the decoder at all" \
	"$([ "$((errors - fileerr + clean))" -gt 0 ] && echo yes || echo "no ($fileerr of $errors errors were opening the file)")" "yes"

pgc_summary
