#!/usr/bin/env bash
#
# pgColumnar Arrow IPC decode fuzzer (issue #214).
#
# The sibling of test/fuzz_parquet.sh, for the other hand-rolled parser.
# columnar_arrow.c is ~1,900 lines of hand-written FlatBuffers and Arrow IPC
# decoding with the same shape as the Parquet reader and, until this suite, none
# of its coverage. It mutates valid Arrow IPC stream files and feeds each to the
# one entry point that parses one, pgcolumnar.import_arrow, and asserts a single
# property:
#
#     a malformed file makes the backend raise an ERROR, never die.
#
# That is the property #210 broke on the Parquet side. The Arrow path has the
# analogous surface: a metadata length the reader seeks by, FlatBuffers offsets
# and vector lengths it trusts, and RecordBatch buffer offset/length pairs that
# say where to read a column's bytes from. A crafted one of those is a
# memory-safety question, not a data-quality one.
#
# Anything that is not a clean ERROR is a finding, in three kinds:
#   crash   the backend died (signal, or the connection dropped)
#   san     a sanitizer reported (only on a sanitizer build)
#   hang    the statement outlived its timeout
#
# Every mutant is a pure function of (seed file, seed integer), so a finding
# reproduces exactly and is saved with the two values that regenerate it.
#
# Usage:  test/fuzz_arrow.sh [PG_CONFIG]
#   PGC_SEED=<int>            base seed (default 20260728)
#   PGC_ITERS=<int>           mutants to run (default 100)
#   PGC_FUZZ_KEEP=1           keep every mutant, not only the findings
#   PGC_FUZZ_FINDINGS=<dir>   save findings outside the workdir, for campaigns
#
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

if ! python3 -c 'import pyarrow.ipc' 2>/dev/null; then
	pgc_skip pyarrow "pyarrow not available; the Arrow fuzzer needs it to build seeds"
fi

SEED="${PGC_SEED:-20260728}"
# 100 in the gate, a bit under a minute. The gate's job is to catch a regression
# that makes malformed input fatal again; the search for new defects is a
# campaign, run with PGC_ITERS in the thousands outside CI.
ITERS="${PGC_ITERS:-100}"
PGC_FUZZ_KEEP="${PGC_FUZZ_KEEP:-0}"
CORPUS="$PGC_WORKDIR/corpus"
FINDINGS="${PGC_FUZZ_FINDINGS:-$PGC_WORKDIR/findings}"
MUT="$PGC_WORKDIR/mutant.arrows"
NEWLOG="$PGC_WORKDIR/newlog.txt"
mkdir -p "$CORPUS" "$FINDINGS"
chmod 777 "$CORPUS" "$FINDINGS"

echo "-- seed=$SEED iters=$ITERS"
echo "-- corpus: $CORPUS"

# ---------------------------------------------------------------------------
# Seeds. Every one is valid; see test/arrow_corpus.py. Each line is
# "<name> <path> <coldef>", the column list being the schema import_arrow expects,
# since Arrow has no parquet_schema equivalent to derive it from the file.
# ---------------------------------------------------------------------------
mapfile -t SEEDLINES < <(python3 "$PGC_SRCDIR/test/arrow_corpus.py" "$CORPUS" 2>/dev/null)
check "the seed corpus generator produced files" \
	"$([ "${#SEEDLINES[@]}" -gt 0 ] && echo yes || echo no)" "yes"
echo "-- ${#SEEDLINES[@]} seed files"

# The composite type the struct seeds name in their column list. Created once here
# so a struct-target table can be built; the corpus references it by name because
# a composite column type cannot be written inline the way an array can.
psql_run "DROP TYPE IF EXISTS pgc_fuzz_xy CASCADE;
	CREATE TYPE pgc_fuzz_xy AS (x int, y text);" >/dev/null 2>&1

# Keep only seeds the reader accepts pristine: create the matching target table
# and import the untouched seed once. A seed the importer rejects would only ever
# exercise the schema check and never the decode, and would make the property
# checks below pass for the wrong reason.
declare -a SEEDPATH SEEDTAB
kept=0
for line in "${SEEDLINES[@]}"; do
	read -r name path cols <<< "$line"
	[ -z "$cols" ] && continue
	tab="arrt_$kept"
	psql_run "DROP TABLE IF EXISTS $tab; CREATE TABLE $tab ($cols) USING pgcolumnar;" >/dev/null 2>&1 || continue
	if q "SELECT pgcolumnar.import_arrow('$tab', '$path');" >/dev/null 2>&1; then
		psql_run "TRUNCATE $tab;" >/dev/null 2>&1
		SEEDPATH+=("$path")
		SEEDTAB+=("$tab")
		kept=$((kept + 1))
	fi
done
echo "-- ${#SEEDPATH[@]} seeds the importer accepts pristine"

# A named check, not an unnamed FAIL: a reader that rejects its own corpus is a
# product failure, and the fuzz loop below then runs nothing at all.
check "the importer accepts some pristine seed" \
	"$([ "${#SEEDPATH[@]}" -gt 0 ] && echo yes || echo no)" "yes"

# ---------------------------------------------------------------------------
# Crash detection. Judged from the server log rather than psql's message: a
# backend that dies takes the connection with it, so psql reports the symptom.
# The log is read incrementally from a byte offset so a finding is attributed to
# the mutant that produced it, not to every mutant after it (the mistake that
# turned one line into 257 findings in the Parquet fuzzer's first cut).
# ---------------------------------------------------------------------------
LOGPOS=0
crashes=0
gen=0
hangs=0
sanitizer=0
errors=0
clean=0

log_since() {
	local sz
	sz=$(stat -c %s "$PGC_LOGFILE" 2>/dev/null || echo 0)
	if [ "$sz" -gt "$LOGPOS" ]; then
		tail -c +$((LOGPOS + 1)) "$PGC_LOGFILE" > "$NEWLOG" 2>/dev/null
	else
		: > "$NEWLOG"
	fi
	LOGPOS="$sz"
}

wait_for_cluster() {
	local i
	for i in $(seq 1 60); do
		if env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" \
			-U postgres -d "$PGC_DB" -Atc "SELECT 1" >/dev/null 2>&1; then
			return 0
		fi
		sleep 0.5
	done
	return 1
}

save_finding() {
	local kind="$1" seedfile="$2" mutseed="$3" stmt="$4" detail="$5"
	local dir="$FINDINGS/${kind}-${mutseed}"
	mkdir -p "$dir"
	cp "$MUT" "$dir/mutant.arrows" 2>/dev/null
	{
		echo "kind:      $kind"
		echo "seed file: $(basename "$seedfile")"
		echo "seed:      $mutseed"
		echo "statement: $stmt"
		echo "reproduce:"
		echo "    python3 test/arrow_corpus.py /tmp/corpus"
		echo "    python3 test/arrow_mutate.py /tmp/corpus/$(basename "$seedfile") $mutseed /tmp/repro.arrows"
		echo "    then import /tmp/repro.arrows into a table with the seed's columns"
		echo "--- detail ---"
		echo "$detail"
	} > "$dir/README"
	echo "  FINDING [$kind] seed=$mutseed from $(basename "$seedfile")"
	echo "$detail" | head -5 | sed 's/^/      /'
}

# Runs one statement against one mutant and classifies the outcome. Returns 0
# when the outcome is acceptable (success or a clean ERROR).
run_stmt() {
	local stmt="$1" seedfile="$2" mutseed="$3"
	local out rc newlog

	log_since
	out="$(timeout 30 env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" \
		-U postgres -d "$PGC_DB" -v ON_ERROR_STOP=0 \
		-c "SET statement_timeout = '20s'; $stmt" 2>&1)"
	rc=$?

	if grep -qE 'could not connect|No such file or directory|Connection refused' <<<"$out" &&
	   ! grep -q '^ERROR:' <<<"$out"; then
		if ! wait_for_cluster; then
			echo "  FATAL: cluster unreachable and did not return"
			return 1
		fi
	fi
	log_since
	newlog="$(cat "$NEWLOG" 2>/dev/null)"

	if grep -qE 'AddressSanitizer|runtime error:|UndefinedBehaviorSanitizer|LeakSanitizer' <<<"$newlog"; then
		sanitizer=$((sanitizer + 1))
		save_finding san "$seedfile" "$mutseed" "$stmt" "$newlog"
		wait_for_cluster || return 1
		return 1
	fi

	if grep -qE 'was terminated by signal|server process .* exited with|crashed' <<<"$newlog"; then
		crashes=$((crashes + 1))
		save_finding crash "$seedfile" "$mutseed" "$stmt" "$newlog"
		wait_for_cluster || echo "  (cluster did not come back)"
		return 1
	fi

	if [ "$rc" = 124 ]; then
		hangs=$((hangs + 1))
		save_finding hang "$seedfile" "$mutseed" "$stmt" "psql wall-clock timeout (30s)"
		wait_for_cluster
		return 1
	fi

	if grep -q 'canceling statement due to statement timeout' <<<"$out"; then
		hangs=$((hangs + 1))
		save_finding hang "$seedfile" "$mutseed" "$stmt" "$out"
		return 1
	fi

	if grep -qE 'server closed the connection unexpectedly|connection to server was lost|terminating connection' <<<"$out"; then
		crashes=$((crashes + 1))
		save_finding crash "$seedfile" "$mutseed" "$stmt" "$out"
		wait_for_cluster || echo "  (cluster did not come back)"
		return 1
	fi

	if grep -q '^ERROR:' <<<"$out"; then
		errors=$((errors + 1))
	else
		clean=$((clean + 1))
	fi
	return 0
}

# Prime the log offset so seed setup above is not attributed to mutant 1.
log_since

echo "-- fuzzing"
nseeds="${#SEEDPATH[@]}"
[ "$nseeds" -gt 0 ] || ITERS_RUN=0
for ((i = 0; i < ${ITERS_RUN-$ITERS}; i++)); do
	idx=$((i % nseeds))
	sf="${SEEDPATH[$idx]}"
	tab="${SEEDTAB[$idx]}"
	ms=$((SEED + i))

	python3 "$PGC_SRCDIR/test/arrow_mutate.py" "$sf" "$ms" "$MUT" 2>/dev/null || continue
	gen=$((gen + 1))
	chmod 644 "$MUT" 2>/dev/null

	# Empty the target first so an accepted mutant does not accumulate rows across
	# the run; import_arrow appends.
	psql_run "TRUNCATE $tab;" >/dev/null 2>&1
	run_stmt "SELECT pgcolumnar.import_arrow('$tab', '$MUT');" "$sf" "$ms"

	if [ "$PGC_FUZZ_KEEP" = 1 ]; then
		cp "$MUT" "$PGC_WORKDIR/kept-$ms.arrows" 2>/dev/null
	fi

	if [ $((i % 50)) = 49 ]; then
		echo "  $((i + 1))/$ITERS  errors=$errors clean=$clean crashes=$crashes hangs=$hangs san=$sanitizer"
	fi
done

echo "-- done: $ITERS mutants, ${errors} rejected with ERROR, ${clean} accepted"

check "no mutant crashed the backend" "$crashes" "0"
check "no mutant hung the decode" "$hangs" "0"
check "no mutant tripped a sanitizer" "$sanitizer" "0"
# Every mutant generated, and every statement run with an outcome. A mutate that
# failed was skipped with `continue`, and a corpus run that generated almost nothing
# passed: the reach check below needs only one ERROR.
check "every mutant was generated and ran the import" \
	"$gen/$((errors + clean + crashes + hangs + sanitizer))" "$ITERS/$((ITERS * 1))"

# A corpus rejected outright teaches nothing and would make the three checks above
# pass for the wrong reason. Some mutants must have reached the decoder and been
# refused on their merits.
check "the corpus reached the decoder at all" \
	"$([ "$errors" -gt 0 ] && echo yes || echo "no (errors=$errors clean=$clean)")" \
	"yes"

if [ "$crashes" != 0 ] || [ "$hangs" != 0 ] || [ "$sanitizer" != 0 ]; then
	echo "-- findings kept in $FINDINGS"
	ls -1 "$FINDINGS"
fi

pgc_summary
