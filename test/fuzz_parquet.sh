#!/usr/bin/env bash
#
# pgColumnar Parquet decode fuzzer (issue #214).
#
# test/fuzz.sh is a differential suite over data SHAPES with heap as the oracle:
# it never presents a malformed byte to a decoder. This one is the other half.
# It mutates valid Parquet files and feeds them to every entry point that parses
# one, and asserts a single property:
#
#     a malformed file makes the backend raise an ERROR, never die.
#
# That is the property #210 broke. A crafted footer reached unbounded recursion
# in PgColumnarThriftSkip, and a crafted schema chain reached a second one in
# walk_schema, either of which takes down the whole cluster rather than the
# session. Both were found by reading code. This suite is the machine that was
# missing.
#
# Anything that is not a clean ERROR is a finding, and there are three kinds:
#   crash   the backend died (signal, or the connection dropped)
#   san     a sanitizer reported (only on a sanitizer build)
#   hang    the statement outlived its timeout
# A hang is a finding in its own right and not a nuisance: an unkillable decode
# loop is #212's shape, and the only way out of one today is kill -9.
#
# Every mutant is a pure function of (seed file, seed integer), so a finding
# reproduces exactly. Findings are saved with the two values that regenerate
# them, and the file itself is kept because a minimized repro is worth more than
# a description of one.
#
# Usage:  test/fuzz_parquet.sh [PG_CONFIG]
#   PGC_SEED=<int>            base seed (default 20260728)
#   PGC_ITERS=<int>           mutants to run (default 100)
#   PGC_FUZZ_KEEP=1           keep every mutant, not only the findings
#   PGC_FUZZ_FINDINGS=<dir>   save findings outside the workdir, for campaigns
#
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

if ! python3 -c 'import pyarrow.parquet' 2>/dev/null; then
	pgc_skip pyarrow "pyarrow not available; the Parquet fuzzer needs it to build seeds"
fi

SEED="${PGC_SEED:-20260728}"
# 100 in the gate, which is roughly a minute and a half. The gate's job is to
# catch a regression that makes malformed input fatal again; the search for new
# defects is a campaign, run with PGC_ITERS in the thousands outside CI.
ITERS="${PGC_ITERS:-100}"
PGC_FUZZ_KEEP="${PGC_FUZZ_KEEP:-0}"
CORPUS="$PGC_WORKDIR/corpus"
# Findings default into the workdir, which teardown removes. That is fine for the
# gate, where the console output is the result, and useless for a campaign, where
# the saved mutant and the full server-log excerpt are the whole point. Point
# PGC_FUZZ_FINDINGS somewhere durable when running one.
FINDINGS="${PGC_FUZZ_FINDINGS:-$PGC_WORKDIR/findings}"
MUT="$PGC_WORKDIR/mutant.parquet"
NEWLOG="$PGC_WORKDIR/newlog.txt"
mkdir -p "$CORPUS" "$FINDINGS"
chmod 777 "$CORPUS" "$FINDINGS"

echo "-- seed=$SEED iters=$ITERS"
echo "-- corpus: $CORPUS"

# ---------------------------------------------------------------------------
# Seeds. Every one is valid; see test/parquet_corpus.py for why that matters.
# ---------------------------------------------------------------------------
mapfile -t SEEDLINES < <(python3 "$PGC_SRCDIR/test/parquet_corpus.py" "$CORPUS" 2>/dev/null)
check "the seed corpus generator produced files" \
	"$([ "${#SEEDLINES[@]}" -gt 0 ] && echo yes || echo no)" "yes"
echo "-- ${#SEEDLINES[@]} seed files"

# The column definition list read_parquet requires, derived from each pristine
# seed rather than hard-coded, so adding a seed needs no change here. A seed
# whose schema will not render is dropped: it would only ever exercise the
# argument check.
declare -a SEEDPATH SEEDCOLS
for line in "${SEEDLINES[@]}"; do
	p="${line##* }"
	cols="$(q "SELECT string_agg(format('%I %s', column_name, data_type), ', ')
		FROM pgcolumnar.parquet_schema('$p');" 2>/dev/null)"
	[ -z "$cols" ] && continue
	SEEDPATH+=("$p")
	SEEDCOLS+=("$cols")
done
echo "-- ${#SEEDPATH[@]} seeds with a usable column list"

# A named check, not an unnamed FAIL: a reader that rejects its own corpus is a
# product failure, and the fuzz loop below then runs nothing at all.
check "the reader renders some pristine seed's schema" \
	"$([ "${#SEEDPATH[@]}" -gt 0 ] && echo yes || echo no)" "yes"

# ---------------------------------------------------------------------------
# Crash detection.
#
# Judged from the server log rather than from psql's message, because a backend
# that dies takes the connection with it and psql then reports the symptom
# rather than the cause. The log is read incrementally from a byte offset, so a
# finding is attributed to the mutant that produced it and not to every mutant
# after it.
# ---------------------------------------------------------------------------
LOGPOS=0
crashes=0
gen=0
hangs=0
sanitizer=0
errors=0
clean=0

# Writes everything appended since the last call into $NEWLOG, and advances
# LOGPOS.
#
# It writes to a file and is called as a plain command rather than returning the
# text through $( ), because command substitution runs in a subshell and the
# LOGPOS assignment there is discarded. That version looked like it worked and
# did not: LOGPOS stayed 0, every call re-read the log from byte zero, and one
# matching line anywhere in it was then re-reported against every later mutant.
# A 500-mutant run produced 257 "findings" that were all the same five lines from
# the first second of the run, with the seed number being the only thing that
# differed. Attribution is the whole value of reading the log incrementally.
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
	cp "$MUT" "$dir/mutant.parquet" 2>/dev/null
	# The repro is written against a REGENERATED corpus, not against this run's
	# workdir. The workdir is removed by teardown, so a finding in the matrix
	# would otherwise print a path that no longer exists by the time anyone reads
	# it. The corpus generator is deterministic, so the seed's basename is a
	# stable name for the same bytes.
	{
		echo "kind:      $kind"
		echo "seed file: $(basename "$seedfile")"
		echo "seed:      $mutseed"
		echo "statement: $stmt"
		echo "reproduce:"
		echo "    python3 test/parquet_corpus.py /tmp/corpus"
		echo "    python3 test/parquet_mutate.py /tmp/corpus/$(basename "$seedfile") $mutseed /tmp/repro.parquet"
		echo "    then run the statement above against /tmp/repro.parquet"
		echo "--- detail ---"
		echo "$detail"
	} > "$dir/README"
	echo "  FINDING [$kind] seed=$mutseed from $(basename "$seedfile")"
	echo "$detail" | head -5 | sed 's/^/      /'
}

# Runs one statement against one mutant and classifies the outcome.
# Returns 0 when the outcome is acceptable (success or a clean ERROR).
run_stmt() {
	local stmt="$1" seedfile="$2" mutseed="$3"
	local out rc newlog

	# Connects exactly as lib.sh does. An earlier version used -h "$PGC_WORKDIR",
	# and the socket is not there: every statement failed to connect, the message
	# was neither an ERROR nor a known crash string, and all 180 were counted as
	# accepted. The suite reported three green checks over a run in which nothing
	# was ever parsed. That is what the corpus check below exists to catch, and it
	# is the only reason this was noticed.
	log_since
	out="$(timeout 30 env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" \
		-U postgres -d "$PGC_DB" -v ON_ERROR_STOP=0 \
		-c "SET statement_timeout = '20s'; $stmt" 2>&1)"
	rc=$?

	# Never let a connection failure be read as a clean result again.
	if grep -qE 'could not connect|No such file or directory|Connection refused' <<<"$out" &&
	   ! grep -q '^ERROR:' <<<"$out"; then
		if ! wait_for_cluster; then
			echo "  FATAL: cluster unreachable and did not return"
			return 1
		fi
	fi
	log_since
	newlog="$(cat "$NEWLOG" 2>/dev/null)"

	# A sanitizer report is a finding even when the statement then succeeds.
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

	# statement_timeout firing is a hang too: the decode did not finish in 20s
	# on a file that is at most a few hundred kilobytes.
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

psql_run "DROP TABLE IF EXISTS fz_target;" >/dev/null 2>&1

echo "-- fuzzing"
nseeds="${#SEEDPATH[@]}"
[ "$nseeds" -gt 0 ] || ITERS_RUN=0
for ((i = 0; i < ${ITERS_RUN-$ITERS}; i++)); do
	idx=$((i % nseeds))
	sf="${SEEDPATH[$idx]}"
	cols="${SEEDCOLS[$idx]}"
	ms=$((SEED + i))

	python3 "$PGC_SRCDIR/test/parquet_mutate.py" "$sf" "$ms" "$MUT" 2>/dev/null || continue
	gen=$((gen + 1))
	chmod 644 "$MUT" 2>/dev/null

	run_stmt "SELECT * FROM pgcolumnar.parquet_schema('$MUT');" "$sf" "$ms"
	run_stmt "SELECT count(*) FROM pgcolumnar.read_parquet('$MUT') AS t($cols);" "$sf" "$ms"

	# import_parquet writes, so it reaches the insert path as well as the decoder.
	psql_run "DROP TABLE IF EXISTS fz_target; CREATE TABLE fz_target ($cols) USING pgcolumnar;" \
		>/dev/null 2>&1
	run_stmt "SELECT pgcolumnar.import_parquet('fz_target', '$MUT');" "$sf" "$ms"

	if [ "$PGC_FUZZ_KEEP" = 1 ]; then
		cp "$MUT" "$PGC_WORKDIR/kept-$ms.parquet" 2>/dev/null
	fi

	if [ $((i % 50)) = 49 ]; then
		echo "  $((i + 1))/$ITERS  errors=$errors clean=$clean crashes=$crashes hangs=$hangs san=$sanitizer"
	fi
done

echo "-- done: $ITERS mutants, ${errors} rejected with ERROR, ${clean} accepted"

# The suite asserts the property, not the counts. A mutant that is accepted is
# not a failure: a bit flip inside a page payload produces a different value,
# not an invalid file.
check "no mutant crashed the backend" "$crashes" "0"
check "no mutant hung the decode" "$hangs" "0"
check "no mutant tripped a sanitizer" "$sanitizer" "0"
# Every mutant generated, and every statement run with an outcome. A mutate that
# failed was skipped with `continue`, and a corpus run that generated almost nothing
# passed: the reach check below needs only one ERROR.
check "every mutant was generated and ran its three statements" \
	"$gen/$((errors + clean + crashes + hangs + sanitizer))" "$ITERS/$((ITERS * 3))"

# A corpus that is rejected outright teaches nothing, and would make the three
# checks above pass for the wrong reason. At least some mutants must have got
# far enough to be parsed and refused on their merits.
check "the corpus reached the decoder at all" \
	"$([ "$errors" -gt 0 ] && echo yes || echo "no (errors=$errors clean=$clean)")" \
	"yes"

if [ "$crashes" != 0 ] || [ "$hangs" != 0 ] || [ "$sanitizer" != 0 ]; then
	echo "-- findings kept in $FINDINGS"
	ls -1 "$FINDINGS"
fi

pgc_summary
