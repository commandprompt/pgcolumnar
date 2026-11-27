#!/usr/bin/env bash
#
# The sanitizer gate (issue #224). The ordinary matrix is five assert builds, none
# instrumented, so a memory-safety defect that does not fault -- an unaligned load,
# an out-of-bounds read that lands inside the process -- passes green on all five.
# #225 was exactly that: an unaligned varlena-header read on the plain write path,
# benign on x86_64, that every one of the 92 suites passed with and without.
#
# This runs a subset of the suites against the ASAN+UBSAN build from
# test/build_san.sh, with sanitizer violations made fatal (UBSAN_OPTIONS and
# ASAN_OPTIONS abort_on_error), so a violation kills the backend and turns its
# suite red rather than printing a line nobody reads. The subset is the write,
# read, encode, and import/decode paths, where the hand-rolled buffer walks live;
# all 92 suites on five majors are neither needed nor affordable under
# instrumentation.
#
# Prove it works the way #224 asks: undo the fix it guards and confirm this gate
# goes red while the ordinary matrix stays green. Undo it in its TRUE pre-#225
# form -- delete the PgColumnarVarSizeAnyUnaligned helper and restore VARSIZE_ANY at
# the three call sites (columnar_encoding.c, columnar_reader.c x2). That reports
# the misalignment (about 20 of the 23 suites, at columnar_encoding.c:1135).
# Do NOT instead rewrite the helper's body to the cast while keeping the inline
# wrapper: clang stops emitting the alignment check across that inline boundary at
# -O1, the gate stays green, and it looks like a proof that the gate does not work.
#
# That same boundary is the gate's one stated limit: a future regression that
# reintroduces an unaligned read from inside an inlined wrapper would not be
# reported here. "The sanitizer gate is green" means the defects in the forms it
# exercises are absent, not that no unaligned access exists anywhere.
#
# Usage:  test/run_san.sh [SAN_PREFIX]      (default /usr/local/pg18_san)
#   PGC_SAN_SUITES=<list>   override the suite subset
#   PGC_SAN_FUZZ_ITERS=<n>  fuzzer mutants under sanitizers (default 200)
#
set -uo pipefail

# The subset, and the one place it is defined.
#
# Declared before anything else runs so --list-suites can answer without building
# a sanitizer PostgreSQL first, and so a caller asking what would be sanitized
# gets the same string the loop below iterates -- not a second copy of it, and
# not a text parser's reading of this file. #473 replaced exactly that pattern in
# run_all_versions.sh, and CONTEXT.md records why: a parser over the array
# disagrees with the shell on the mistake this invites, and the disagreement is
# silent.
SUITES="${PGC_SAN_SUITES:-smoke native_writer native_roundtrip native_encoding \
	native_zonemap write_fsst_compressed write_minmax_fastpath encode_effort \
	encode_invariants \
	native_dml native_skip native_fetch_position native_fetch_cache \
	arrow_import arrow_export parquet_import parquet_export export_sink native_read_parquet \
	native_parquet_schema hardening corruption differential fuzz_arrow fuzz_parquet}"

# --list-suites: print the subset, one name per line, and exit. Answered before
# the prerequisite check below, so it works on a box with no sanitizer build at
# all -- a caller asking "what does this cover" should not need one.
if [ "${1:-}" = "--list-suites" ]; then
	printf '%s\n' $SUITES
	exit 0
fi

SAN="${1:-/usr/local/pg18_san}"
PGCONF="$SAN/bin/pg_config"
if [ ! -x "$PGCONF" ]; then
	echo "FAIL  no sanitizer build at $SAN; run test/build_san.sh first"
	exit 1
fi

# Set the sanitizer options before running anything from this build: pg_config is
# itself instrumented and leaks by design at exit, so without detect_leaks=0 even
# the prerequisite check below aborts it and reads its output as empty. A
# violation must also abort the backend, not print and continue: that is what
# turns it into a suite failure. detect_leaks / stack_use_after_return match the
# by-design tool leaks and initdb trip from build_san.sh.
export ASAN_OPTIONS="detect_leaks=0:detect_stack_use_after_return=0:abort_on_error=1:print_stacktrace=1"
export UBSAN_OPTIONS="halt_on_error=1:abort_on_error=1:print_stacktrace=1"

# A build exists is not enough: it must be the sanitizer build. Pointed at an
# ordinary or a mis-flagged one, the suites below fail for reasons unrelated to
# what this gate tests -- a build missing -fno-sanitize=function, for instance,
# aborts on dynahash's function-pointer casts and reports a timeout that reads
# like a hang. Check the flags the gate depends on are actually in this build's
# Makefile.global, the same place PGXS reads them from.
_mkglobal="$("$PGCONF" --pgxs 2>/dev/null | xargs -r dirname | xargs -r dirname)/Makefile.global"
if ! grep -q -- '-fsanitize=address,undefined' "$_mkglobal" 2>/dev/null ||
   ! grep -q -- '-fno-sanitize=function' "$_mkglobal" 2>/dev/null; then
	echo "FAIL  $SAN is not the expected ASAN+UBSAN build"
	echo "      (its Makefile.global lacks -fsanitize=address,undefined and/or -fno-sanitize=function; rebuild with test/build_san.sh)"
	exit 1
fi

cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "-- building the extension instrumented against $SAN"
make -s clean >/dev/null 2>&1 || true
if ! make -s PG_CONFIG="$PGCONF" install > /tmp/run-san-ext.log 2>&1; then
	echo "FAIL  extension build against the sanitizer PostgreSQL"
	tail -20 /tmp/run-san-ext.log
	exit 1
fi

FUZZ_ITERS="${PGC_SAN_FUZZ_ITERS:-200}"

echo "-- running the subset under ASAN+UBSAN (fatal)"
fail=0
skipped=0
ran=0
for s in $SUITES; do
	if [ ! -f "test/$s.sh" ]; then
		echo "  SKIP  $s (no such suite)"
		continue
	fi
	ran=$((ran + 1))
	out="$(PGC_SKIP_BUILD=1 PGC_ITERS="$FUZZ_ITERS" timeout 900 \
		bash "test/$s.sh" "$PGCONF" 2>&1)"
	rc=$?
	# A sanitizer report reaches here two ways: the backend aborts (SIGABRT, so the
	# suite's own crash checks or a nonzero rc), or the text appears in the output.
	san="$(printf '%s' "$out" | grep -icE 'runtime error:|AddressSanitizer|UndefinedBehaviorSanitizer|SUMMARY: .*Sanitizer|terminated by signal 6')"
	if [ "$rc" = 66 ] && [ "$san" = 0 ] && \
	   grep -q 'SKIPPED (ran no checks)' <<<"$out"; then
		# pgc_summary's skipped state: the suite ran no checks.
		#
		# This does NOT cover the pyarrow-gated suites, and an earlier version of
		# this comment claimed it did. Since a missing dependency FAILS, pgc_skip
		# exits 1, not here. What reaches this branch is a suite with nothing to
		# assert on this build, which is a different thing and a rarer one.
		skipped=$((skipped + 1))
		echo "  SKIP  $s  (ran no checks)"
	elif [ "$rc" != 0 ] || [ "$san" != 0 ]; then
		fail=$((fail + 1))
		echo "  FAIL  $s  (rc=$rc, sanitizer_lines=$san)"
		printf '%s\n' "$out" | grep -iE 'runtime error:|AddressSanitizer|SUMMARY: .*Sanitizer|FAIL' | head -4 | sed 's/^/        /'
	else
		echo "  PASS  $s"
	fi
done

echo "-- $ran suites under sanitizers, $fail failed, $skipped ran no checks"
if [ "$fail" != 0 ]; then
	echo "SANITIZER GATE FAILED"
elif [ "$skipped" != 0 ]; then
	# ANY skip, not just all of them. The previous form fired only when every
	# suite skipped, so 22 of 23 skipped still printed PASSED -- a gate reporting
	# on a surface it had almost entirely not exercised. A sanitizer run is worth
	# what it covered, so an incomplete one is not a pass.
	echo "SANITIZER GATE INCOMPLETE: $skipped of $ran suites ran no checks"
	fail=1
else
	echo "SANITIZER GATE PASSED"
fi
exit "$fail"
