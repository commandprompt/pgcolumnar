#!/usr/bin/env bash
#
# pgColumnar decode-path interrupt discipline.
#
# A column chunk is decoded one vector at a time, and a vector is not the fixed
# 1024 that pgcolumnar.storage.vector_length suggests: the writer emits a single
# vector covering the whole chunk, so the value count reaching a decoder is
# bounded only by pgcolumnar.chunk_group_row_limit, which is user-settable and
# unbounded. Verified by reading the descriptor of a 100,000-row chunk: one
# vector, 100,000 values. So every per-value loop in a decoder is a stretch of
# work whose length the user chooses, and each one needs its own interrupt check.
#
# This is a source-shape test rather than a timing one, and the reason is worth
# stating, because the obvious test does not work.
#
# Cancellation cannot distinguish these checks at any scale the matrix can carry.
# decode_dict and decode_alp both call bitunpack over the same value count before
# their own loop, and bitunpack checks, so a statement_timeout always fires there
# first: measured on 8,000,000-value single-column tables, a cancel landed at
# 63 ms against a 193 ms load with the dictionary check present and at 63 ms
# against 186 ms with it removed. decode_rle has no bitunpack in its path, but it
# decodes 8,000,000 values in about 95 ms, which is shorter than the floor a 50 ms
# statement_timeout can resolve. Making the window big enough to measure needs
# hundreds of millions of rows in one chunk group.
#
# A test that passes with the guard removed proves nothing, so this asserts the
# shape instead, the way wal_envelope.sh does for WAL. It will need updating when
# the decoders legitimately change, which is the point: adding a decoder without
# an interrupt check should fail here rather than be absorbed silently.
#
# Usage:  test/decode_interrupts.sh [PG_CONFIG]
# Written fresh for pgColumnar.

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# WHICH MAJOR THIS SUITE RAN ON (#1121, the wider half of #1109). `pgc_record`
# writes `${PGC_MAJOR:-unknown}`, and PGC_MAJOR is set inside `pgc_setup` -- which
# this suite does not call, deliberately. Without this line every record it emits
# says `unknown`, and a ledger row claiming `unknown` matches no run, so none of
# these checks could ever be seeded or matched again.
#
# The runner passes the pg_config as $1 to EVERY suite, including those that need
# no cluster, so it is available here. `pgc_major_of` returns empty on a path it
# cannot run, which degrades to exactly today's `unknown` rather than to a WRONG
# major -- a guessed major would seed a row claiming a major the check was never
# observed on, which is worse than saying nothing.
PGC_MAJOR="$(pgc_major_of "${1:-}")"

# No cluster needed; pgc_setup is skipped deliberately. Provide the counters the
# shared check() helper expects.
PGC_CHECKS=0
PGC_FAIL=0
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/src"

echo "== pgColumnar decode-path interrupt discipline =="
echo "srcdir=$SRC"

# COMMENTS ARE NOT CODE (#1341). Every check below reads the source with its
# comments blanked, multi-line ones included, each blanked comment keeping its
# newlines so line numbers still order the code. Measured on main before this:
# with decode_for's call written as /* COLUMNAR_DECODE_INTERRUPT(i); */, or the
# macro's own call as /* CHECK_FOR_INTERRUPTS(); */, all 29 checks passed.
BLANK="$(mktemp -d)"
trap 'rm -rf "$BLANK"' EXIT
blank() {	# file -> its text with comments blanked, newlines kept
	perl -0777 -pe 's{/\*.*?\*/}{ my $c = $&; $c =~ tr/\n//cd; $c }gse; s{//[^\n]*}{}g' "$1"
}
blank "$SRC/columnar_encoding.c" > "$BLANK/encoding.c"
blank "$SRC/columnar_reader.c" > "$BLANK/reader.c"
FN="$BLANK/encoding.c"

check "the decode interrupt macro exists" \
	"$(grep -c '^#define COLUMNAR_DECODE_INTERRUPT(i)' "$FN")" "1"

# The macro must call the real thing, not a placeholder.
check "the macro calls CHECK_FOR_INTERRUPTS" \
	"$(awk '/^#define COLUMNAR_DECODE_INTERRUPT\(i\)/,/^$/' "$FN" | grep -c 'CHECK_FOR_INTERRUPTS')" "1"

# Line range of a function: its definition line to its closing brace at column
# zero. It used to run to the next definition, which took in the comment block
# above that one as well.
range_of() {  # function-name [file] -> "start end"
	local f="${2:-$FN}" start end
	start="$(grep -n "^$1(" "$f" | head -1 | cut -d: -f1)"
	[ -z "$start" ] && { echo "0 0"; return; }
	end="$(awk -v s="$start" 'NR > s && /^}/ {print NR; exit}' "$f")"
	[ -z "$end" ] && end="$(wc -l < "$f")"
	echo "$start $end"
}

# INSIDE A LOOP, NOT ONLY IN THE FUNCTION (#1341). A check counts only where it
# sits inside the braces of a for, while or foreach body -- or, given a header
# pattern, inside the body of the loop whose header matches it. Measured on main
# before this: with decode_for's check hoisted in front of its loop, where it runs
# once per vector, with decode_rle's or decode_fsst_shared's moved below the loop
# it guarded, and with either reader check moved in front of its loop, all 29
# checks passed. Brace depth is tracked per character; a loop header whose
# statement ends at a ';' with no brace (a one-statement body) opens no block.
in_loop() {  # function-name file token-regex [header-regex] -> count of tokens inside such a loop
	local r; r="$(range_of "$1" "$2")"
	awk -v s="${r% *}" -v e="${r#* }" -v tok="$3" -v want="${4:-}" '
	NR < s || NR > e { next }
	{
		line = $0
		if (line ~ /(^|[^A-Za-z0-9_])(for|while|foreach)[ \t]*\(/ && line !~ /}[ \t]*while/) {
			pend = 1; ptag = (want == "" || line ~ want); pp = 0
		}
		for (k = 1; k <= length(line); k++) {
			ch = substr(line, k, 1)
			if (ch == "(") pp++
			else if (ch == ")") pp--
			else if (ch == ";" && pend && pp <= 0) pend = 0
			else if (ch == "{") { d++; lp[d] = pend && ptag; pend = 0 }
			else if (ch == "}") { if (d > 0) { lp[d] = 0; d-- } }
		}
		if (line ~ tok) for (j = 1; j <= d; j++) if (lp[j]) { n++; break }
	}
	END { print n + 0 }' "$2"
}

# Every decoder that walks values one at a time. bitunpack is included because
# the bit-packed decoders reach their value count through it.
for f in bitunpack decode_rle decode_for decode_delta decode_gorilla decode_dod \
         decode_alp decode_dict decode_fsst_shared; do
	check "$f was found" \
		"$([ "$(range_of "$f")" != "0 0" ] && echo yes || echo no)" "yes"
	check "$f checks for interrupts" \
		"$([ "$(in_loop "$f" "$FN" 'COLUMNAR_DECODE_INTERRUPT\\(')" -ge 1 ] && echo yes || echo no)" "yes"
done

# decode_rle is the one where placement matters rather than presence. Its outer
# loop walks runs and its inner loop walks values, and a single run can cover the
# whole vector, so a check in the outer loop alone runs once and then copies every
# value uninterrupted. Testing the outer counter is also unsound on its own: it
# advances by run length rather than by one, so it can step over every multiple of
# the stride and never fire again after the first run.
check "decode_rle's inner value loop was found" \
	"$(r="$(range_of decode_rle)"; awk -v s="${r% *}" -v e="${r#* }" 'NR >= s && NR <= e && /while \(run-- > 0/ {n++} END {print (n ? "yes" : "no")}' "$FN")" "yes"
check "decode_rle checks inside its value loop, not only around it" \
	"$([ "$(in_loop decode_rle "$FN" 'COLUMNAR_DECODE_INTERRUPT\\(' 'while \\(run-- > 0')" -ge 1 ] && echo yes || echo no)" "yes"

# decode_fsst_shared is the second place where placement matters rather than
# presence (#712). It has TWO loops: a symbol-table parse bounded by tableLen,
# which is a few KB, and the decode loop bounded by encLen, which is a whole
# vector's code stream. A check in the first satisfies "checks for interrupts"
# above while leaving the loop that needs it unguarded, so assert the check sits
# after the decode loop begins.
check "decode_fsst_shared's decode loop was found" \
	"$(r="$(range_of decode_fsst_shared)"; awk -v s="${r% *}" -v e="${r#* }" 'NR >= s && NR <= e && /while \(p < end\)/ {n++} END {print (n ? "yes" : "no")}' "$FN")" "yes"
check "decode_fsst_shared checks inside its decode loop, not only in the table parse" \
	"$([ "$(in_loop decode_fsst_shared "$FN" 'COLUMNAR_DECODE_INTERRUPT\\(' 'while \\(p < end\\)')" -ge 1 ] && echo yes || echo no)" "yes"

# The stride is what makes the check cheap enough to sit in a per-value loop. A
# non-power-of-two would make the test a modulo rather than a mask, and a huge
# one would put the cancellation latency back.
stride="$(grep -oE '^#define COLUMNAR_DECODE_INTERRUPT_STRIDE [0-9]+' "$FN" | awk '{print $3}')"
check "the stride is defined" "$([ -n "$stride" ] && echo yes || echo no)" "yes"
check "the stride is a power of two" \
	"$([ -n "$stride" ] && [ $(( stride & (stride - 1) )) -eq 0 ] && echo yes || echo no)" "yes"
check "the stride is not so large that a check never lands" \
	"$([ -n "$stride" ] && [ "$stride" -le 1048576 ] && echo yes || echo no)" "yes"

# The two checks in the reader are what make a scan cancellable between chunks and
# between rows. They are the coarse net under all of the above; losing them would
# leave a whole group load or a whole run of skipped rows uninterruptible.
RD="$BLANK/reader.c"
check "the group load checks for interrupts per column chunk" \
	"$(in_loop pgcolumnar_native_load_group "$RD" 'CHECK_FOR_INTERRUPTS' 'foreach\\(lc, chunks\\)')" "1"
check "the row loop checks for interrupts" \
	"$(in_loop pgcolumnar_native_next_row "$RD" 'CHECK_FOR_INTERRUPTS' 'for \\(;;\\)')" "1"

pgc_summary
