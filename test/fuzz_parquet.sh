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
# A FOURTH KIND, wrong (#1852): a mutant that reads without an error must give the
# right answer. The read statement returns the row count and an md5 of every row
# as text. The answer is right when it equals the seed's answer (the mutation did
# not reach the rows), or equals pgColumnar's answer for pyarrow's rewrite of the
# mutant (test/parquet_oracle.py: pyarrow reads the mutant and writes what it read
# to a valid file). Any other answer is a finding. That includes a different answer
# from a mutant pyarrow refuses, which is how #1855 looked: pyarrow said
# "Definition level histogram size mismatch" and pgColumnar returned a wrong sum.
#
# A random mutant rarely lands on a schema field, so after the random mutants the
# suite also sweeps the schema of the int64 seed: one copy per byte of the footer's
# schema list and per low-nibble value, the sweep that found #1855.
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
#
# parquet_schema reports a struct of scalars as its field list,
# "composite (x integer, y text)", which is not a type name (#1909). For such a
# column the suite creates the composite type from that field list and declares
# the column with it (#1920).
declare -a SEEDPATH SEEDCOLS
ctypes_made=0
ctypes_tried=0
ctypes_failed=""
for line in "${SEEDLINES[@]}"; do
	p="${line##* }"
	cols=""
	ncol=0
	while IFS=$'\t' read -r cname ctype; do
		case "$ctype" in
			"composite ("*)
				tname="fz_seed${#SEEDPATH[@]}_c$ncol"
				ctypes_tried=$((ctypes_tried + 1))
				if cterr="$(psql_run "DROP TYPE IF EXISTS $tname; CREATE TYPE $tname AS ${ctype#composite };" 2>&1)"; then
					ctypes_made=$((ctypes_made + 1))
				else
					ctypes_failed+=" ${p##*/}:$cname: $(printf '%s' "$cterr" | grep -m1 ERROR)"
				fi
				ctype="$tname" ;;
		esac
		cols+="${cols:+, }$cname $ctype"
		ncol=$((ncol + 1))
	done < <(q "SELECT format('%I', column_name) || chr(9) || coalesce(data_type, '')
		FROM pgcolumnar.parquet_schema('$p');" 2>/dev/null)
	[ -z "$cols" ] && continue
	SEEDPATH+=("$p")
	SEEDCOLS+=("$cols")
done
echo "-- ${#SEEDPATH[@]} seeds with a usable column list"

# The answer the read statement returns: the row count and an md5 of every row as
# text, in file order. answer_sql PATH COLS
answer_sql() {
	echo "SELECT count(*) || '/' || coalesce(md5(string_agg(t::text, chr(10))), '-')
		FROM pgcolumnar.read_parquet('$1') AS t($2);"
}
# answer PATH COLS -> the answer, or the ERROR line when the read fails
answer() {
	local out
	out="$(env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" -U postgres \
		-d "$PGC_DB" -At -c "SET statement_timeout = '20s'; $(answer_sql "$1" "$2")" 2>&1)"
	if grep -q '^ERROR:' <<<"$out"; then grep -m1 '^ERROR:' <<<"$out"; else tail -1 <<<"$out"; fi
}
REWRITE="$PGC_WORKDIR/rewrite.parquet"
declare -a SEEDANS
seeds_agree=0
seeds_read=0
seeds_disagree=""
for idx in "${!SEEDPATH[@]}"; do
	SEEDANS[$idx]="$(answer "${SEEDPATH[$idx]}" "${SEEDCOLS[$idx]}")"
	# a seed the reader refuses under its own column list (the nested ones) has no
	# answer to compare; its mutants still run, and one that reads is judged
	# against pyarrow's rewrite alone
	case "${SEEDANS[$idx]}" in ERROR:*) continue ;; esac
	seeds_read=$((seeds_read + 1))
	rm -f "$REWRITE"
	python3 "$PGC_SRCDIR/test/parquet_oracle.py" "${SEEDPATH[$idx]}" "$REWRITE" >/dev/null 2>&1
	rw="$(answer "$REWRITE" "${SEEDCOLS[$idx]}")"
	if [ "$rw" = "${SEEDANS[$idx]}" ]; then
		seeds_agree=$((seeds_agree + 1))
	else
		seeds_disagree+=" $(basename "${SEEDPATH[$idx]}"): [${SEEDANS[$idx]}] vs [$rw]"
	fi
done
# The oracle is only as good as its rewrite: pyarrow's rewrite of a valid seed must
# read exactly as the seed does, or every "agree" below means nothing.
check "the oracle's rewrite of every pristine seed reads the seed's answer" \
	"$seeds_agree/$seeds_read${seeds_disagree:+ (differ:$seeds_disagree)}" \
	"$([ "$seeds_read" -gt 0 ] && echo "$seeds_read/$seeds_read" || echo "some seed reads")"

# Every composite type built from a seed's field list must be created. A type
# that fails would leave its seed with a column list that cannot be read, and
# the seed would drop out of the fuzz without a word.
check "every composite type built from a seed's field list is created" \
	"$ctypes_made/$ctypes_tried${ctypes_failed:+ (failed:$ctypes_failed)}" \
	"$([ "$ctypes_tried" -gt 0 ] && echo "$ctypes_tried/$ctypes_tried" || echo "some composite type")"

# The struct seed must read under the column list built from its own schema
# (#1920). parquet_schema reports a struct of scalars as field-list text,
# "composite (x integer, y text)", which is not a type name, so the column list
# failed with a syntax error and the seed was refused: its mutants could then
# never be judged against the seed's answer. The wanted answer is computed here
# from the values parquet_corpus.py writes: row i is ("(i,si)").
struct_ans="(not in the corpus)"
for idx in "${!SEEDPATH[@]}"; do
	[ "$(basename "${SEEDPATH[$idx]}")" = struct.parquet ] && struct_ans="${SEEDANS[$idx]}"
done
check "the pristine struct seed reads under its own column list" "$struct_ans" \
	"200/$(python3 -c 'import hashlib; print(hashlib.md5("\n".join("(\"(%d,s%d)\")" % (i, i) for i in range(200)).encode()).hexdigest())')"

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
wrong=0
WRONG_FIRST=""
same=0
agree=0
unjudged=0
UNJUDGED_FIRST=""
LAST_OUT=""
LAST_KIND=""

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
	LAST_KIND=finding
	out="$(timeout 30 env PATH="$PGC_BINDIR:$PATH" psql -h 127.0.0.1 -p "$PGC_PORT" \
		-U postgres -d "$PGC_DB" -v ON_ERROR_STOP=0 -At \
		-c "SET statement_timeout = '20s'; $stmt" 2>&1)"
	rc=$?
	LAST_OUT="$out"

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
		LAST_KIND=error
	else
		clean=$((clean + 1))
		LAST_KIND=clean
	fi
	return 0
}

# mutant_diff SEED MUTANT -> where the mutant differs from its seed: the sizes, the
# number of bytes that differ, and the first difference as offset and old -> new byte
mutant_diff() {
	python3 - "$1" "$2" <<'PY'
import sys
a, b = (open(f, "rb").read() for f in sys.argv[1:3])
d = [i for i in range(min(len(a), len(b))) if a[i] != b[i]]
first = f"first at byte {d[0]}: 0x{a[d[0]]:02x} -> 0x{b[d[0]]:02x}" if d else "no byte differs in the common length"
print(f"{len(a)} -> {len(b)} bytes, {len(d)} differ, {first}")
PY
}

# judge SEEDIDX MUTSEED STMT LABEL: the result oracle, for a read that raised no
# error. The answer must equal the seed's, or pgColumnar's answer for pyarrow's
# rewrite of the mutant. LABEL says where the mutant came from, for the finding.
judge() {
	local idx="$1" mutseed="$2" stmt="$3" label="$4"
	local got want="${SEEDANS[$1]}" why rc rw
	got="$(tail -1 <<<"$LAST_OUT")"
	if [ "$got" = "$want" ]; then
		same=$((same + 1))
		return 0
	fi
	rm -f "$REWRITE"
	timeout 60 python3 "$PGC_SRCDIR/test/parquet_oracle.py" "$MUT" "$REWRITE" \
		> "$PGC_WORKDIR/oracle.out" 2>&1
	rc=$?
	why="$(tail -1 "$PGC_WORKDIR/oracle.out")"
	if [ "$rc" = 0 ]; then
		rw="$(answer "$REWRITE" "${SEEDCOLS[$idx]}")"
		if [ "$rw" = "$got" ]; then
			agree=$((agree + 1))
			return 0
		fi
		case "$rw" in
			ERROR:*)
				# pyarrow read the mutant as something the seed's column list does
				# not bind, so there is no second answer to compare with
				unjudged=$((unjudged + 1))
				[ -n "$UNJUDGED_FIRST" ] ||
					UNJUDGED_FIRST="$label: pgColumnar read [$got]; pyarrow's rewrite reads [$rw]"
				return 0 ;;
		esac
		why="pyarrow reads [$rw]"
	elif [ "$rc" != 2 ]; then
		unjudged=$((unjudged + 1))
		[ -n "$UNJUDGED_FIRST" ] ||
			UNJUDGED_FIRST="$label: pgColumnar read [$got]; the oracle exited $rc: $why"
		return 0
	fi
	wrong=$((wrong + 1))
	local detail="$label: pgColumnar read [$got] without an error; seed answer [$want]; $why"
	[ -n "$WRONG_FIRST" ] || WRONG_FIRST="$detail"
	save_finding wrong "${SEEDPATH[$idx]}" "$mutseed" "$stmt" "$detail"
	return 1
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
	stmt="$(answer_sql "$MUT" "$cols")"
	run_stmt "$stmt" "$sf" "$ms" && [ "$LAST_KIND" = clean ] &&
		judge "$idx" "$ms" "$stmt" "seed=$ms from $(basename "$sf") ($(mutant_diff "$sf" "$MUT"))"

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
# the random loop's statement count, before the sweep adds to the same counters
loop_ran=$((errors + clean + crashes + hangs + sanitizer))

# ---------------------------------------------------------------------------
# The schema sweep (#1852, #1855): every byte of the int64 seed's schema list, each
# low-nibble value. Only the read statement runs, and every clean read is judged.
# ---------------------------------------------------------------------------
sweep_idx=""
for idx in "${!SEEDPATH[@]}"; do
	[ "$(basename "${SEEDPATH[$idx]}")" = int64.parquet ] && sweep_idx="$idx"
done
sweep_gen=0
sweep_ran=0
if [ -n "$sweep_idx" ]; then
	SWEEP="$PGC_WORKDIR/sweep"
	mapfile -t SWEEPFILES < <(python3 "$PGC_SRCDIR/test/parquet_mutate.py" --schema-sweep \
		"${SEEDPATH[$sweep_idx]}" "$SWEEP" 2>/dev/null)
	sweep_gen="${#SWEEPFILES[@]}"
	echo "-- schema sweep: $sweep_gen copies of int64.parquet"
	for f in "${SWEEPFILES[@]}"; do
		cp "$f" "$MUT"
		chmod 644 "$MUT"
		tag="$(basename "$f" .parquet)"
		before=$((errors + clean + crashes + hangs + sanitizer))
		stmt="$(answer_sql "$MUT" "${SEEDCOLS[$sweep_idx]}")"
		run_stmt "$stmt" "${SEEDPATH[$sweep_idx]}" "$tag" && [ "$LAST_KIND" = clean ] &&
			judge "$sweep_idx" "$tag" "$stmt" "$tag of int64.parquet (byte offset, low nibble)"
		[ $((errors + clean + crashes + hangs + sanitizer)) -gt "$before" ] && sweep_ran=$((sweep_ran + 1))
	done
fi
echo "-- oracle: $same same as the seed, $agree agree with pyarrow, $wrong wrong, $unjudged not judged"

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
	"$gen/$loop_ran" "$ITERS/$((ITERS * 3))"
# The result oracle (#1852): a mutant that reads without an error reads the right rows.
check "no mutant that reads returned a wrong answer" \
	"$wrong${WRONG_FIRST:+ (first: $WRONG_FIRST)}" "0"
# A read the oracle could not judge proves nothing either way: pyarrow's rewrite
# did not bind the seed's column list, or the oracle timed out or crashed (#1882).
check "every read that differed from the seed was judged" \
	"$unjudged${UNJUDGED_FIRST:+ (first: $UNJUDGED_FIRST)}" "0"
check "every schema-sweep copy ran its read" \
	"$sweep_ran/$sweep_gen" "$([ "$sweep_gen" -gt 0 ] && echo "$sweep_gen/$sweep_gen" || echo "at least one copy")"
# An oracle that never compared an answer passes for the wrong reason.
check "the result oracle compared some answers" \
	"$([ $((same + agree)) -gt 0 ] && echo yes || echo "no (same=$same agree=$agree unjudged=$unjudged)")" \
	"yes"

# A corpus that is rejected outright teaches nothing, and would make the three
# checks above pass for the wrong reason. At least some mutants must have got
# far enough to be parsed and refused on their merits.
check "the corpus reached the decoder at all" \
	"$([ "$errors" -gt 0 ] && echo yes || echo "no (errors=$errors clean=$clean)")" \
	"yes"

if [ "$crashes" != 0 ] || [ "$hangs" != 0 ] || [ "$sanitizer" != 0 ] || [ "$wrong" != 0 ]; then
	echo "-- findings kept in $FINDINGS"
	ls -1 "$FINDINGS"
fi

pgc_summary
