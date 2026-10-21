#!/usr/bin/env bash
#
# pgColumnar #710: the Parquet level-width helper, pinned as a source invariant.
#
# pq_bits_for computes the bit width the RLE/bit-pack level encoding uses for a
# maximum definition or repetition level. Two things about it cannot be reached
# from SQL, and so cannot be tested behaviourally:
#
#   1. It was DUPLICATED, once in the writer and once in the reader, byte
#      identical with nothing making them stay that way. A writer and a reader
#      that disagree on a level width produce a file that is silently wrong
#      rather than one that fails to parse, which is the same hazard
#      columnar_parquet_format.h was created to end for the format constants.
#
#   2. Its loop was `while ((1 << b) <= maxval)`, and `1 << b` is signed, so it
#      is undefined once b reaches 31 -- which the loop reaches for any
#      maxval >= 2^30. Levels accumulate one per nesting level from a recursion
#      that check_stack_depth bounds, so a real schema cannot get there; but it
#      is undefined behaviour in a decoder that reads attacker-authored files,
#      and a compiler is entitled to assume it cannot happen.
#
# The VALUES are already covered behaviourally: parquet_nested,
# parquet_nested_import and native_parquet_multifile all round-trip schemas with
# real definition and repetition levels, and an implementation that computed a
# different width would corrupt them. Verified separately that old and new agree
# on every reachable input (0 through 2^29) before this shipped, so those suites
# are the correctness gate and this suite is not duplicating them.
#
# What this pins is the part no query can reach. It is deliberately a source
# check, in the same spirit as the placement arms in decode_interrupts.sh.
#
# Usage:  test/parquet_level_width.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
pgc_setup "${1:-/usr/local/pg17/bin/pg_config}"

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/src"
HDR="$SRC/columnar_parquet_format.h"

# The body, so every assertion below reads the shared definition and not a
# comment that happens to mention it. Comments inside the body are blanked too,
# multi-line ones included: a guard commented out of the code is not a guard.
body="$(awk '/^pq_bits_for\(int maxval\)/{f=1} f{print} f&&/^}/{exit}' "$HDR" |
	perl -0777 -pe 's{/\*.*?\*/}{}gs; s{//[^\n]*}{}g')"

check "the shared level-width helper exists" \
	"$([ -n "$body" ] && echo yes || echo no)" "yes"

# SINGLE-SOURCED. A local copy reappearing in either .c file is the drift this
# is here to catch, and it would not fail any other check in the tree.
# Both files must be READ: grep -c over a missing file prints nothing for it, and the
# sum then reads 0, the passing answer -- with the writer's path wrong this passed (#1341).
check "no local copy of the helper survives in the writer or the reader" \
	"$(if [ -f "$SRC/columnar_parquet.c" ] && [ -f "$SRC/columnar_parquet_reader.c" ]; then
		grep -cE '^[a-z_]*bits_for\(int' "$SRC/columnar_parquet.c" "$SRC/columnar_parquet_reader.c" \
			| awk -F: '{n += $2} END {print n+0}'
	   else echo "no reading (a source file is missing)"; fi)" "0"
check "and both callers reach the shared one" \
	"$([ "$(grep -c 'pq_bits_for(' "$SRC/columnar_parquet.c")" -ge 1 ] &&
	   [ "$(grep -c 'pq_bits_for(' "$SRC/columnar_parquet_reader.c")" -ge 1 ] &&
	   echo yes || echo no)" "yes"

# UNSIGNED. This is the #710 defect itself.
check "the shift is unsigned" \
	"$(grep -c '1u <<' <<<"$body")" "1"
# Over the body only when there IS one: a count over an empty extraction is 0, the
# passing answer, so with the header unreadable this passed (#1341).
check "and no signed 1 << remains in it" \
	"$(if [ -n "$body" ]; then grep -cE '\(1 <<|[^u] 1 <<' <<<"$body"; else echo "no reading (no body)"; fi)" "0"

# BOUNDED, which `1u <<` alone does NOT give. Making the shift unsigned also
# makes the comparison unsigned, so a negative maxval converts to a huge value
# and the loop runs to b == 32, where the unsigned shift is undefined in its own
# right -- and under a recovering sanitizer it does not terminate at all.
# Measured: the literal 1u<<b form ran past 200 iterations on maxval = -1 while
# this form returns 0. Both ends are asserted because only fixing one moves the
# bug rather than removing it.
check "the loop is bounded below 32 shifts" \
	"$(grep -c 'b < 31' <<<"$body")" "1"
check "a non-positive maximum returns before shifting at all" \
	"$(grep -c 'maxval <= 0' <<<"$body")" "1"

pgc_summary
