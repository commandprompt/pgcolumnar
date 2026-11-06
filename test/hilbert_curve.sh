#!/usr/bin/env bash
#
# pgColumnar Hilbert clustering curve battery (issue #889).
#
# WHAT THIS SUITE IS FOR
#
# #889 replaces the Z-order clustering key with a Hilbert one. A space-filling
# curve is exactly the kind of code that is easy to get almost right: a wrong
# curve still returns a number for every point, still sorts, still clusters
# something, and still makes a benchmark look better than no clustering at all.
# Nothing downstream of it can tell. So the curve is pinned here, directly, by
# its mathematical properties and by frozen bytes, rather than inferred from a
# query plan or a page count.
#
# THE DEFECT THIS WOULD HAVE CAUGHT
#
# An encoder that truncates, or that mixes up the bit order, is injective on the
# subset anyone happens to sample. It passes a "no collisions" test. It fails
# arm C1 here, which asserts the encoded indices are EXACTLY the contiguous
# range [0, 2^(ncols*b)) -- every value hit once, none left over.
#
# A serpentine (boustrophedon) scan is unit-step adjacent everywhere and is not
# a Hilbert curve: its dyadic sub-cubes are scattered, so a range query over a
# sub-cube reads runs from all over the file. It passes an adjacency test alone.
#
# Z-order -- what ships today -- has perfect dyadic locality and jumps a long
# way at every quadrant boundary. It passes a contiguity test alone.
#
# So neither property alone certifies a Hilbert curve, and the battery is only
# non-vacuous as C2 AND C3 together. Both wrong encoders are compiled and run in
# arm C3 as named controls, and the suite asserts their verdicts explicitly, in
# both directions. If either control ever behaves the way the real encoder
# should, the instrument is broken and this suite says so instead of passing.
#
# A third control encoder, TRUNCATE, exists for C1, which had none: the
# serpentine and Z-order are both permutations, so neither can make the
# permutation checker report a violation, and until TRUNCATE was added nothing
# in this file proved C1 could go red at all.
#
# THE CONTROL ARMS ARE CALIBRATION, NOT COVERAGE. controls.c does not link
# src/columnar_curve.c, by design, and with that file deleted the control arms
# still print PASS. Their names are prefixed INSTRUMENT and the run prints the
# instrument and battery counts separately, so nobody counts them as evidence
# about the encoder.
#
# WHY THIS IS A STANDALONE C BATTERY
#
# The curve is a pure function of ncols uint64 words. It needs no cluster, no
# catalog and no rows, and running it through SQL would put an entire storage
# engine between the assertion and the thing asserted. So the suite writes C to
# a temp directory, compiles it, runs it, and turns the program's output into
# check() arms. test/harness_selftest.sh and test/docs_style.sh are the
# precedent for a suite that skips pgc_setup; test/build_san.sh is the precedent
# for a suite that compiles C.
#
# THE INTERFACE UNDER TEST
#
#	void cluster_hilbert_transpose(uint64 *X, int ncols);
#	void cluster_pack_interleave(const uint64 *ord, int ncols,
#				     unsigned char *out);
#
# The Hilbert key is: ordinals -> transpose -> pack. The Z-order key is:
# ordinals -> pack. Output is exactly 8*ncols bytes, and arm C5 measures that
# rather than trusting it.
#
# cluster_pack_interleave is the interleave loop that lives inside
# cluster_zorder_key in src/columnar_vacuum.c today, moved out unchanged. No
# line number: the four that used to be here rot the moment anything above them
# moves, and the frozen Z-order table is what actually pins the bytes.
# src/columnar_curve.c and src/columnar_curve.h do not exist yet; until they do,
# every arm that needs them is RED, and that is the intended state.
#
# THE ARMS
#
#   C1  Permutation. For ncols 1..8 and every b with ncols*b <= 20, the b-bit
#       coordinates go in the TOP b bits of each uint64. Every one of the
#       2^(ncols*b) points is encoded and the set of indices must be exactly
#       [0, 2^(ncols*b)). 52 cases, 6,344,330 points.
#   C2  Unit-step adjacency. The packed keys are sorted with real memcmp and the
#       sorted walk is compared against the POINT COORDINATES, which the sort
#       cannot manufacture: consecutive points differ in one coordinate by one.
#   C3  Dyadic self-similarity, plus the two named negative controls above.
#   C4  Production-width bridge. (a) The top ncols*b bits of the key for a
#       full-width ordinal vector equal the exhaustively verified index of the
#       sub-cube that vector sits in. (b) An inverse transcribed from Skilling's
#       published TransposetoAxes round-trips random full-width keys, and
#       consecutive keys decode to unit-adjacent points. A round trip against an
#       inverse derived from the forward code proves nothing, so a deliberately
#       mutated inverse is compiled beside it and the arm must go red under it.
#   C5  Golden byte vectors, and the pack bit order and key length.
#   C6  ncols == 1 is the identity, pinned absolutely and relatively.
#   C7  The Z-order refactor is byte-for-byte inert.
#
# WHERE EVERY EXPECTED VALUE CAME FROM
#
# No expected value in this file was produced by the code under test. The code
# under test did not exist when they were written.
#
#  0. WHAT THE GOLDEN VECTORS DISCRIMINATE. "zero", "max" and "topbit0" are
#     structural invariants -- zero survives any permutation of the output bits,
#     max is all-ones so any permutation is identical, and topbit0's single set
#     bit lands in output bit 0 under both interleaved and column-major packing.
#     Measured against a column-major pack, 27 of the C7 arms and 13 of the C5
#     arms still passed. They are kept for the zero and all-ones edges and are
#     not counted as discrimination. "unit", "tie", "mixed", "asym" and "lowbit"
#     are what carry it; "asym" gives every column a different dense value, and
#     "lowbit" puts one set bit in the LAST column's LOWEST position, which is
#     the bit a pack that truncates or reverses column order loses first.
#
#  1. BY HAND, from the two loops:
#     - Every all-zero vector encodes to 8*ncols zero bytes. Skilling's first
#       pass leaves an all-zero X untouched (both branches XOR zero), the Gray
#       encode XORs zeros, and the accumulated mask t is zero. So the key is
#       zero.
#     - At ncols == 1 the transpose is the IDENTITY, so the key is the ordinal
#       big-endian: 0 -> 00.., 1 -> ..01, 2^63 -> 80.., UINT64_MAX -> ff..
#       Pass one XORs, for each set bit from 63 down to 1, the bits below it;
#       pass two accumulates that same mask from the result and XORs it back,
#       and the two cancel. Worked by hand at three bits: 5 -> 6 -> 7 -> 5,
#       3 -> 2 -> 3, 6 -> 5 -> 6. Arm C6 is what actually pins it.
#     - The pack bit order. The loop emits ord[0].bit63, ord[1].bit63, ...,
#       ord[n-1].bit63, ord[0].bit62, ... MSB first. So with ncols == 2,
#       pack(2^63, 0) puts a 1 in output bit 0 and pack(0, 2^63) puts it in
#       output bit 1: the keys must start 0x80 and 0x40.
#     - Every key is 8*ncols bytes, because the loop writes 64*ncols bits.
#
#  2. FROM SKILLING, NOT FROM US, for the remaining Hilbert keys. Generated on
#     2026-09-08 from an independent transcription of AxestoTranspose at b = 64
#     -- John Skilling, "Programming the Hilbert curve", AIP Conf. Proc. 707,
#     381-387 (2004) -- run once, off-tree, and pasted in as literals. That
#     transcription was checked before its output was trusted: it round-tripped
#     160,000 random full-width transposes against a separate transcription of
#     the published TransposetoAxes with 0 mismatches, and it passed C1, C2 and
#     C3 over all 52 cases with 0 violations.
#
#     This arm is the only one that pins WHICH Hilbert curve was chosen.
#     Skilling's curve differs from Butz/Hamilton for ncols >= 3 and both are
#     valid Hilbert curves, so the property arms cannot tell them apart. A
#     transcription bug here would be frozen into the goldens -- which is why C1
#     to C4 exist and do not read this table.
#
#  3. FROM THE CODE BEING REPLACED, for arm C7. The Z-order keys were produced
#     by transcribing the interleave loop out of cluster_zorder_key in
#     src/columnar_vacuum.c verbatim and running it, on 2026-09-08, at commit
#     e84a5e5, BEFORE any refactor. They are a frozen record. If both sides of C7
#     ever call the same new function the arm is a tautology; it compares new
#     code against these bytes and must keep doing so.
#
#     The control program's own ctl_pack is held to this SAME table, so "the
#     control packs the way the product packs" is an arm rather than a comment.
#     It was a comment, and nothing could falsify it: a Z-order over reversed
#     columns -- not what ships -- left all seven control arms green.
#
# WHAT IS NOT CLAIMED
#
# C1, C2 and C3 are exhaustive only up to ncols*b <= 20. Above that the evidence
# is C4's bridge to production width and C5's frozen bytes, not exhaustion.
# Nothing here measures clustering QUALITY: a correct curve that nothing calls
# would pass every arm in this file.
#
# C1 to C3 read only the TOP ncols*b bits of the key -- at most 20 of them. The
# low bits are the file's blind spot and C4b is what covers them, so C4b's
# sample size is load-bearing rather than incidental. It was not, once: the keys
# came from the low byte of a 32-bit LCG whose period there is 512, giving 344
# distinct keys where the premise arm certified 24,000, and a correct Skilling
# transpose carrying one extra bit-flip on a condition those keys never met was
# compiled against this suite and reported "138 passed + 0 failed". C4b now
# draws from splitmix64 and asserts the DISTINCT keys it round-tripped. A pack
# that ignores the low 32 bits of every ordinal is a cruder form of the same
# blind spot: it passes C1, C2, C3, C4a and both C5 extent arms, and is caught
# only by the goldens, C7, the C5 content arm, two of the C6 pins and C4b.
#
# C7 pins the PACKING of ordinals that are handed to it. cluster_zorder_key also
# derives those ordinals (cluster_type_ordinal) and maps NULL to 0, and nothing
# here calls cluster_zorder_key, so a refactor that moved the ordinal derivation
# or changed the NULL rule stays green. The arm is named for the pack, not for
# the key.
#
# THE PARAGRAPH THAT STOOD HERE WAS CARRIED OUT AND LEFT BEHIND (#1088). It made
# three claims, each true when written and none true now:
#
#     "NOT REGISTERED in test/run_all_versions.sh"   it is registered
#     "src/columnar_curve.c does not exist"          127 lines, and it links
#                                                    into pgcolumnar.so
#     "070 reports it as UNREGISTERED, which is
#      the accurate state"                           070's arm PASSES: every
#                                                    suite is registered
#
# It also said registering the suite "belongs in the commit that adds the
# encoder". That commit landed. A stale instruction is worse than a stale fact
# because it tells the next person to undo what was done, and this one names the
# undo explicitly.
#
# Usage:  test/hilbert_curve.sh [PG_CONFIG]
# The argument is accepted and ignored; this suite needs no cluster.
# Environment:
#   CC                 the compiler to use; smoke-tested before it is believed.
#   PGC_KEEP_WORKDIR   non-empty keeps the temp directory holding the generated
#                      C, the build logs and the two programs' output, for
#                      anyone reproducing an arm by hand. Local to this suite.
# Written fresh for pgColumnar.

set -uo pipefail

# Sourcing must not fail quietly. Measured: a copy run from a directory with no
# lib.sh printed this suite's two measurement lines, then "check: command not
# found" 21 times, recorded 0 checks, printed no summary and exited 127. A gate
# reading the status is safe; a log-scraper reading "violations=0" is not.
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh" || {
	echo "cannot source lib.sh beside $0" >&2
	exit 1
}

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

# No cluster, so pgc_setup is skipped deliberately -- the shape wal_envelope.sh
# uses. lib.sh already zeroes the counters; they are restated so a reader can see
# this suite keeps them itself and so pgc_summary's reconciliation is meaningful.
PGC_CHECKS=0
PGC_FAIL=0
PGC_PASSED=0
PGC_FAILED=0
PGC_UNRUN=0

SRCDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "== pgColumnar test: hilbert_curve.sh =="
echo "-- no cluster is started; this battery is pure C"

# ---- the compiler ----------------------------------------------------------
#
# Absent compiler is the one genuinely UNRUNNABLE state here: the question
# cannot be asked at all. Absent src/columnar_curve.c is NOT that. The suite can
# ask and the answer is no, so those arms FAIL.
CC="${CC:-}"
if [ -z "$CC" ]; then
	for _c in gcc cc clang; do
		if command -v "$_c" >/dev/null 2>&1; then CC="$_c"; break; fi
	done
fi

WORK="$(mktemp -d /tmp/pgcolumnar-hilbert.XXXXXX)"
cleanup() {
	if [ -n "${PGC_KEEP_WORKDIR:-}" ]; then
		echo "-- workdir kept at $WORK (PGC_KEEP_WORKDIR)"
	else
		rm -rf "$WORK"
	fi
}
trap cleanup EXIT

# The eight golden ordinal vectors, per ncols. Defined identically in the C
# below, and named here because the arm lists carry one arm per vector.
PGC_VECNAMES="zero max topbit0 unit tie mixed asym lowbit"

# Every arm this suite can report, by group, EXPANDED. The claim these lists
# exist to support is that the three states -- ran, could not compile, has no
# code under test -- report the SAME set of arms and a reader can diff two runs.
#
# That claim used to be false, and the lists were how it was false: they held
# fourteen GROUP labels against a live run's 131 battery arms, five of those
# labels never appeared verbatim in a live run at all, and four whole arm
# families ("C4a premise", both "C4b premise" arms, "C5 the pack wrote nothing
# past the key") were named in neither list, so an absent encoder left twelve
# questions not merely unanswered but unnamed -- exactly what check_unrunnable
# exists to prevent. Diffing a red run against a green one added 129 arms and
# removed 12.
#
# So the names are generated here by the same loops that assert below, and the
# diff of the two runs' arm names is empty. Keep it that way: an arm added to
# the assertions and not to this function reintroduces the defect.
CONTROL_ARMS="\
INSTRUMENT C0 premise: the control battery encoded every case and every point
INSTRUMENT C0 premise: the control encoders were called once per point
INSTRUMENT the control's own pack reproduces the frozen Z-order bytes
INSTRUMENT C1 control: the serpentine is a permutation of the index range
INSTRUMENT C2 control: the serpentine PASSES unit-step adjacency
INSTRUMENT C3 control: the serpentine FAILS dyadic contiguity in every nested case
INSTRUMENT C1 control: Z-order is a permutation of the index range
INSTRUMENT C2 control: Z-order FAILS unit-step adjacency in every multi-column case
INSTRUMENT C3 control: Z-order PASSES dyadic contiguity
INSTRUMENT C1 control: a truncating encoder FAILS the permutation test in every case"

battery_arm_names() {
	local _n _v

	echo "C0 premise: the battery encoded every case and every point"
	echo "C0 premise: the encoder was called once per point"
	echo "C1 the Hilbert index set is exactly the contiguous range"
	echo "C2 consecutive points in memcmp key order are unit-adjacent"
	echo "C3 every dyadic sub-cube occupies a contiguous run of indices"
	echo "C4a premise: the bridge covered every case and every point"
	echo "C4a the full-width key prefix does not move when the low bits change"
	echo "C4a the full-width key prefixes are exactly the index range"
	echo "C4b premise: 3,000 DISTINCT full-width keys per ncols were round-tripped"
	echo "C4b the published inverse round-trips full-width keys"
	echo "C4b control: a mutated inverse breaks every round trip the clean one made"
	echo "C4b premise: consecutive key pairs were formed"
	echo "C4b consecutive keys decode to unit-adjacent points, re-encoded by the code under test"
	for _n in 1 2 3 4 5 6 7 8; do
		for _v in $PGC_VECNAMES; do
			echo "C5 golden Hilbert keys: ncols=$_n $_v"
		done
	done
	echo "C5 the pack bit order: pack(2^63, 0) starts 0x80"
	echo "C5 the pack bit order: pack(0, 2^63) starts 0x40"
	for _n in 1 2 3 4 5 6 7 8; do
		echo "C5 the pack establishes exactly 8*ncols bytes: ncols=$_n"
		echo "C5 the pack wrote nothing past the key: ncols=$_n"
		echo "C5 the pack sets every bit of the key for an all-ones vector: ncols=$_n"
	done
	echo "C6 ncols == 1 is the big-endian ordinal: 0"
	echo "C6 ncols == 1 is the big-endian ordinal: 1"
	echo "C6 ncols == 1 is the big-endian ordinal: 2^63"
	echo "C6 ncols == 1 is the big-endian ordinal: UINT64_MAX"
	echo "C6 premise: the comparison loop ran 200,000 times"
	echo "C6 ncols == 1 is the big-endian ordinal over 200,000 random values"
	echo "C6 ncols == 1 agrees with Z-order over 200,000 random values"
	for _n in 1 2 3 4 5 6 7 8; do
		for _v in $PGC_VECNAMES; do
			echo "C7 the Z-order pack is byte-for-byte what it was: ncols=$_n $_v"
		done
	done
}
BATTERY_ARMS="$(battery_arm_names)"

# A run that could not build reports a whole group of arms in one state, so it
# still lists what it did not measure. The loops that do it are written out at each
# site rather than in a helper taking the list: a helper that reaches a recorder
# through a loop variable is a name slot compare_to_bash cannot resolve, and it
# refused this whole suite for that (#1341). At the top level the same loop is read
# as one unnamed call, which the grader drops, and the literal arms below are what
# it grades.

# A CC INHERITED FROM THE ENVIRONMENT IS NOT A COMPILER UNTIL IT COMPILES.
#
# The search above runs only when CC is empty, and PGXS and most CI images
# export CC -- so the likely value is one this suite never checked. Measured:
# `CC=/bin/false bash test/hilbert_curve.sh` reported "0 passed + 21 failed +
# 0 unrunnable", which is the toolchain being absent counted as the encoder
# being wrong, in the very state the comment above calls the one genuinely
# UNRUNNABLE one. So the resolved CC must build and run a two-line program
# before anything else is believed of it.
cc_usable=no
if [ -n "$CC" ]; then
	printf 'int main(void){return 0;}\n' > "$WORK/smoke.c"
	if "$CC" -o "$WORK/smoke" "$WORK/smoke.c" >"$WORK/smoke.log" 2>&1 &&
		"$WORK/smoke" >>"$WORK/smoke.log" 2>&1; then
		cc_usable=yes
	fi
fi
if [ "$cc_usable" != yes ]; then
	if [ -z "$CC" ]; then
		echo "-- no C compiler found (looked for gcc, cc, clang)"
		_why="no C compiler"
	else
		echo "-- CC=$CC did not build and run a two-line program:"
		sed 's/^/  /' "$WORK/smoke.log" 2>/dev/null
		_why="CC=$CC cannot build C"
	fi
	while IFS= read -r _a; do
		[ -n "$_a" ] && check_unrunnable "$_a" MISSING_DEPENDENCY "$_why"
	done <<< "$CONTROL_ARMS"$'\n'"$BATTERY_ARMS"
	pgc_summary
fi
echo "-- compiler: $CC ($("$CC" --version 2>/dev/null | head -1))"

# ---- the frozen tables -----------------------------------------------------
#
# Read by the shell and compared against what the C program printed. They are
# deliberately NOT compiled into the C program: a program that holds both the
# answer and the question can only report that it agrees with itself.

# The six ordinal vectors, per ncols. Defined identically in the C below.
#   zero     every ordinal 0
#   max      every ordinal UINT64_MAX
#   topbit0  ordinal 0 is 2^63, the rest 0
#   unit     every ordinal 1
#   tie      every ordinal 5 except the last, which is 4 (tie-heavy, small)
#   mixed    0x0123456789ABCDEF rotated left by 8*j bits for column j
#   asym     0xF0F0F0F0F0F0F0F0 >> j -- dense, and a different value per column
#   lowbit   ordinal 0 everywhere except the LAST column, which is 1
# PGC_VECNAMES is defined near the arm lists, because those name one arm per
# vector and are built before this point.

# Provenance 2 above: Skilling's AxestoTranspose at b = 64, then the interleave
# loop. Generated off-tree on 2026-09-08; not produced by the code under test.
PGC_GOLDEN_HILBERT="
n1 zero 0000000000000000
n1 max ffffffffffffffff
n1 topbit0 8000000000000000
n1 unit 0000000000000001
n1 tie 0000000000000004
n1 mixed 0123456789abcdef
n1 asym f0f0f0f0f0f0f0f0
n1 lowbit 0000000000000001
n2 zero 00000000000000000000000000000000
n2 max aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
n2 topbit0 eaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
n2 unit 00000000000000000000000000000002
n2 tie 00000000000000000000000000000023
n2 mixed 040c9c1c868ed6d684869e36ae0e54fc
n2 asym c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0
n2 lowbit 00000000000000000000000000000003
n3 zero 000000000000000000000000000000000000000000000000
n3 max b6db6db6db6db6db6db6db6db6db6db6db6db6db6db6db6d
n3 topbit0 f12492492492492492492492492492492492492492492492
n3 unit 000000000000000000000000000000000000000000000005
n3 tie 000000000000000000000000000000000000000000000146
n3 mixed 1dc46b182507638ea95e6175bececbaa0ab100a019770fc1
n3 asym fad4f6f2d840e3f8e2209562209562209562209562209562
n3 lowbit 000000000000000000000000000000000000000000000001
n4 zero 0000000000000000000000000000000000000000000000000000000000000000
n4 max aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
n4 topbit0 f844444444444444444444444444444444444444444444444444444444444444
n4 unit 000000000000000000000000000000000000000000000000000000000000000a
n4 tie 0000000000000000000000000000000000000000000000000000000000000a0d
n4 mixed 0e04480636ced888a0688e20142a7e6c2aec8ac47c481e6684c6442052ccdca6
n4 asym fcd23edc54743ed23a947c56943e9ab83a96bc16547c56943e9ab83a96bc1654
n4 lowbit 0000000000000000000000000000000000000000000000000000000000000003
n5 zero 00000000000000000000000000000000000000000000000000000000000000000000000000000000
n5 max ad6b5ad6b5ad6b5ad6b5ad6b5ad6b5ad6b5ad6b5ad6b5ad6b5ad6b5ad6b5ad6b5ad6b5ad6b5ad6b5
n5 topbit0 fc108421084210842108421084210842108421084210842108421084210842108421084210842108
n5 unit 00000000000000000000000000000000000000000000000000000000000000000000000000000015
n5 tie 0000000000000000000000000000000000000000000000000000000000000000000000000000541a
n5 mixed 0f0183c8df970eab0bf7d87586fddb2263e65e35d488645b43dd0e8c92b3c1e4652cd11ece26b677
n5 asym fe0aab4507ecab681c836dab410fdd1f07ee0f83ff07c21c871eb26c0fdd2917692f5dff07c21c87
n5 lowbit 00000000000000000000000000000000000000000000000000000000000000000000000000000007
n6 zero 000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000
n6 max aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
n6 topbit0 fe0410410410410410410410410410410410410410410410410410410410410410410410410410410410410410410410
n6 unit 00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000002a
n6 tie 00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000002a035
n6 mixed 0b8feeda23e06c90e054366c32ef4cf08448a1f8de5255fac96f7492acee7cf85a5f54fa03c558b323c4d913d4d11546
n6 asym ff02cac841fc1b6d7449c45003e0466a85ec0bec4859276ec04efa08ed7449c45003e0466a85ec0bec4859276ec04efa
n6 lowbit 00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000f
n7 zero 0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000
n7 max ab56ad5ab56ad5ab56ad5ab56ad5ab56ad5ab56ad5ab56ad5ab56ad5ab56ad5ab56ad5ab56ad5ab56ad5ab56ad5ab56ad5ab56ad5ab56ad5
n7 topbit0 ff01020408102040810204081020408102040810204081020408102040810204081020408102040810204081020408102040810204081020
n7 unit 0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000055
n7 tie 0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000154056
n7 mixed 0bc7fdcdb2d089745031e34d314d2d40d88d4132f3109e562a066d7be4dcde2ad855fd67d3350272a645a26b7cc7be84bf6eb64745a4f467
n7 asym ff80beab529757effdfd059485050fcf88edd80e1d9b6e3d83828b6937bfffc24aaa2972769ca508007fc6b55da8d8880fea14a92205027b
n7 lowbit 0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000001
n8 zero 00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000
n8 max aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
n8 topbit0 ff804040404040404040404040404040404040404040404040404040404040404040404040404040404040404040404040404040404040404040404040404040
n8 unit 000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000aa
n8 tie 00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000aa00ad
n8 mixed 0ae2feb8dae8024c920ecc1ea668dab6d2723046aeda3c1088f6b2c23894ba98ae2698b240deaa986072d688529628e60880c86ca0bc2ce422a03c40a83a5c94
n8 asym ffc02fd4ca0a040010142aaababe8038269a56b2743ad81c16f6d6d8222a0a76a85c922ec6c4644c5696dadc02069aa4586c321ad6d432240406e222ded838e4
n8 lowbit 00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000003
"

# Provenance 3 above: transcribed from the interleave loop inside
# cluster_zorder_key in src/columnar_vacuum.c and run at commit e84a5e5 on
# 2026-09-08, BEFORE the refactor. A frozen record. The control program's own
# ctl_pack is held to this table too, so the control cannot drift away from the
# packing it claims to be.
PGC_GOLDEN_ZORDER="
n1 zero 0000000000000000
n1 max ffffffffffffffff
n1 topbit0 8000000000000000
n1 unit 0000000000000001
n1 tie 0000000000000004
n1 mixed 0123456789abcdef
n1 asym f0f0f0f0f0f0f0f0
n1 lowbit 0000000000000001
n2 zero 00000000000000000000000000000000
n2 max ffffffffffffffffffffffffffffffff
n2 topbit0 80000000000000000000000000000000
n2 unit 00000000000000000000000000000003
n2 tie 00000000000000000000000000000032
n2 mixed 0407181b3437686bc4c7d8dbf4f7a8ab
n2 asym bf40bf40bf40bf40bf40bf40bf40bf40
n2 lowbit 00000000000000000000000000000001
n3 zero 000000000000000000000000000000000000000000000000
n3 max ffffffffffffffffffffffffffffffffffffffffffffffff
n3 topbit0 800000000000000000000000000000000000000000000000
n3 unit 000000000000000000000000000000000000000000000007
n3 tie 0000000000000000000000000000000000000000000001c6
n3 mixed 0500570e80ef39039772872fe50e57ee8eefd90d9792892f
n3 asym 9bf6409bf6409bf6409bf6409bf6409bf6409bf6409bf640
n3 lowbit 000000000000000000000000000000000000000000000001
n4 zero 0000000000000000000000000000000000000000000000000000000000000000
n4 max ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
n4 topbit0 8000000000000000000000000000000000000000000000000000000000000000
n4 unit 000000000000000000000000000000000000000000000000000000000000000f
n4 tie 0000000000000000000000000000000000000000000000000000000000000f0e
n4 mixed 0350035f16a016af3c503c5f79a079aff350f35fe6a0e6afcc50cc5f89a089af
n4 asym 8cef73108cef73108cef73108cef73108cef73108cef73108cef73108cef7310
n4 lowbit 0000000000000000000000000000000000000000000000000000000000000001
n5 zero 00000000000000000000000000000000000000000000000000000000000000000000000000000000
n5 max ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
n5 topbit0 80000000000000000000000000000000000000000000000000000000000000000000000000000000
n5 unit 0000000000000000000000000000000000000000000000000000000000000000000000000000001f
n5 tie 00000000000000000000000000000000000000000000000000000000000000000000000000007c1e
n5 mixed 099400995f1b2a01b2bf3e5403e55f7cea07cebff1940f195fe32a0e32bfc6540c655f84ea084ebf
n5 asym 8639e79c618639e79c618639e79c618639e79c618639e79c618639e79c618639e79c618639e79c61
n5 lowbit 00000000000000000000000000000000000000000000000000000000000000000000000000000001
n6 zero 000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000
n6 max ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
n6 topbit0 800000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000
n6 unit 00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000003f
n6 tie 00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000003f03e
n6 mixed 0cc5400cc57f1d9a801d9abf3f35403f357f7a6a807a6abff0c540f0c57fe19a80e19abfc33540c3357f866a80866abf
n6 asym 830e3c78f1c3870e3c78f1c3870e3c78f1c3870e3c78f1c3870e3c78f1c3870e3c78f1c3870e3c78f1c3870e3c78f1c3
n6 lowbit 000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000001
n7 zero 0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000
n7 max ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
n7 topbit0 8000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000
n7 unit 000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000007f
n7 tie 00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000001fc07e
n7 mixed 0e655000e6557f1ecea801eceaff3d995003d9957f7932a807932afff065500f06557fe0cea80e0ceaffc399500c39957f8732a808732aff
n7 asym 8183878787878787878787878787878787878787878787878787878787878787878787878787878787878787878787878787878787878787
n7 lowbit 0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000001
n8 zero 00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000
n8 max ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
n8 topbit0 80000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000
n8 unit 000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000ff
n8 tie 00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000ff00fe
n8 mixed 0f3355000f3355ff1e66aa001e66aaff3ccc55003ccc55ff7899aa007899aafff0335500f03355ffe166aa00e166aaffc3cc5500c3cc55ff8799aa008799aaff
n8 asym 80c0e0f0783c1e0f87c3e1f0783c1e0f87c3e1f0783c1e0f87c3e1f0783c1e0f87c3e1f0783c1e0f87c3e1f0783c1e0f87c3e1f0783c1e0f87c3e1f0783c1e0f
n8 lowbit 00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000001
"

# A value out of the frozen tables. An absent key returns the empty string, and
# every comparison below goes through check_text, which refuses an empty side --
# so a typo in a name FAILS rather than comparing "" with "" and passing (#418).
frozen() {	# frozen TABLE NCOLS VECNAME
	awk -v k1="n$2" -v k2="$3" '$1 == k1 && $2 == k2 { print $3 }' <<< "$1"
}

# A value out of a C program's name=value output. Same reasoning: missing is
# empty, and empty fails.
val() {		# val FILE NAME
	awk -F= -v k="$2" '$1 == k { print $2 }' "$1"
}

# ---- the C the suite compiles ----------------------------------------------

# Stub PostgreSQL headers, so src/columnar_curve.c compiles with no server tree.
# The .c and .h under test are COPIED into this directory, so their own
# #include "..." finds these stubs first and never reaches the real headers.
cat > "$WORK/postgres.h" <<'CEOF'
#ifndef PGC_STUB_POSTGRES_H
#define PGC_STUB_POSTGRES_H
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <stdbool.h>
typedef int8_t int8;		typedef uint8_t uint8;
typedef int16_t int16;		typedef uint16_t uint16;
typedef int32_t int32;		typedef uint32_t uint32;
typedef int64_t int64;		typedef uint64_t uint64;
#define Assert(p)		((void) 0)
#define StaticAssertDecl(c, m)	extern int pgc_stub_sa_dummy
#define palloc(sz)		malloc(sz)
#define palloc0(sz)		calloc(1, (sz))
#define pfree(p)		free(p)
#define pg_attribute_unused()
#endif
CEOF
for _h in c.h fmgr.h miscadmin.h postgres_ext.h utils/elog.h \
	  columnar.h columnar_compat.h; do
	mkdir -p "$WORK/$(dirname "$_h")"
	echo '#include "postgres.h"' > "$WORK/$_h"
done

# The property checkers. Shared by the control program and the battery, so both
# ask the SAME question of their encoders and a difference in verdict is a
# difference in the encoder.
cat > "$WORK/props.h" <<'CEOF'
#ifndef PGC_PROPS_H
#define PGC_PROPS_H
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

typedef uint64_t pgc_u64;

/*
 * Counted at the point of each enc() call.
 *
 * The C0 premise arms used to report `points += (double) (1u << (ncols * b))`,
 * which is arithmetic over the loop bounds and reports 6,344,330 whether or not
 * a single point was encoded -- the house rule "measure the work, never the
 * intent", broken in the arm whose whole job is to establish that the work
 * happened. This counter is incremented where the encoder is actually called,
 * and the premise arms assert it.
 */
static long pgc_enc_calls = 0;

/* ---- the golden ordinal vectors, shared by both programs ------------------
 *
 * Here rather than in the battery because the control program pins its own
 * pack against the same frozen Z-order table, and two copies of these
 * definitions would let that pin drift from the thing it pins.
 *
 * WHICH OF THESE DISCRIMINATE. "zero" is invariant under every permutation of
 * the output bits, "max" is all-ones so any permutation is identical, and
 * "topbit0" has one set bit that lands in output bit 0 under both interleaved
 * and column-major packing. Measured: under a column-major pack 27 of the 48
 * C7 arms and 13 of the 48 C5 arms then in the file still passed. They are kept
 * for the zero and all-ones edges, not counted as discrimination. "unit",
 * "tie", "mixed", "asym" and "lowbit" are what carry it, and "lowbit" -- one
 * set bit in the LAST column's LOWEST position -- is the one a pack that drops
 * low bits or reverses column order cannot reproduce.
 */
#define PGC_NVEC 8
static const char *const pgc_vecname[PGC_NVEC] =
{"zero", "max", "topbit0", "unit", "tie", "mixed", "asym", "lowbit"};

static void
pgc_makevec(int which, int ncols, pgc_u64 *X)
{
	int			j;

	for (j = 0; j < ncols; j++)
	{
		switch (which)
		{
			case 0:
				X[j] = 0;
				break;
			case 1:
				X[j] = UINT64_MAX;
				break;
			case 2:
				X[j] = (j == 0) ? ((pgc_u64) 1 << 63) : 0;
				break;
			case 3:
				X[j] = 1;
				break;
			case 4:
				X[j] = (j == ncols - 1) ? 4 : 5;
				break;
			case 5:
				{
					pgc_u64		s = UINT64_C(0x0123456789ABCDEF);
					int			r = (8 * j) & 63;

					X[j] = r ? ((s << r) | (s >> (64 - r))) : s;
					break;
				}
			case 6:
				X[j] = UINT64_C(0xF0F0F0F0F0F0F0F0) >> j;
				break;
			default:
				X[j] = (j == ncols - 1) ? 1 : 0;
				break;
		}
	}
}

static void
pgc_hex(const unsigned char *b, int n, char *o)
{
	int			i;

	for (i = 0; i < n; i++)
		sprintf(o + 2 * i, "%02x", b[i]);
	o[2 * n] = '\0';
}

/* The point whose index is p, as ncols b-bit coordinates, column 0 first. */
static void
pgc_digits(unsigned p, int ncols, int b, unsigned *c)
{
	int			j;

	for (j = ncols - 1; j >= 0; j--)
	{
		c[j] = p & ((1u << b) - 1);
		p >>= b;
	}
}

/*
 * The index a key carries: its top ncols*b bits, read MSB first.
 *
 * With the b-bit coordinates in the TOP b bits of each uint64, the pack loop
 * emits ord[0].bit63, ord[1].bit63, ..., so the first ncols*b bits of the key
 * are exactly the b-round curve index and everything below them is zero.
 */
static unsigned
pgc_key_index(const unsigned char *key, int ncols, int b)
{
	unsigned	idx = 0;
	int			t,
				nb = ncols * b;

	for (t = 0; t < nb; t++)
		idx = (idx << 1) | ((key[t >> 3] >> (7 - (t & 7))) & 1);
	return idx;
}

/* The transpose form whose packing is this index, for an encoder that produces
 * an index directly rather than a transpose. */
static void
pgc_index_to_transpose(unsigned idx, int ncols, int b, pgc_u64 *X)
{
	int			t,
				nb = ncols * b;

	for (t = 0; t < ncols; t++)
		X[t] = 0;
	for (t = 0; t < nb; t++)
		X[t % ncols] |= (pgc_u64) ((idx >> (nb - 1 - t)) & 1) << (63 - (t / ncols));
}

typedef void (*pgc_enc) (const unsigned *c, int ncols, int b, unsigned char *key);

static int	pgc_g_ncols;
static const unsigned char *pgc_g_keys;

static int
pgc_cmpkey(const void *a, const void *b)
{
	unsigned	pa = *(const unsigned *) a,
				pb = *(const unsigned *) b;

	return memcmp(pgc_g_keys + (size_t) pa * pgc_g_ncols * 8,
				  pgc_g_keys + (size_t) pb * pgc_g_ncols * 8,
				  (size_t) pgc_g_ncols * 8);
}

/*
 * Run C1, C2 and C3 over the whole 2^(ncols*b) point space for one encoder.
 *
 * C1 counts, together: an index outside [0, 2^(ncols*b)), an index hit twice,
 * and an index never hit. "No collisions" alone is vacuous -- a truncating
 * encoder is injective on a subset -- so the unhit sweep is the half that makes
 * this an assertion about the whole range.
 *
 * C2 sorts the PACKED KEYS with memcmp and then walks the sorted order looking
 * at the POINT COORDINATES. The sort cannot manufacture coordinates, so this is
 * not a claim the ordering makes about itself.
 *
 * C3 walks every dyadic level and requires each sub-cube's indices to be a
 * contiguous run. C1 has already established the indices are distinct, so
 * max - min + 1 == count is exactly contiguity.
 */
static void
pgc_props(pgc_enc enc, int ncols, int b, long *c1, long *c2, long *c3)
{
	unsigned	N = 1u << (ncols * b),
				p;
	unsigned   *idx = malloc(sizeof(unsigned) * (size_t) N);
	unsigned   *ord = malloc(sizeof(unsigned) * (size_t) N);
	unsigned char *seen = calloc(N, 1);
	unsigned char *keys = malloc((size_t) N * ncols * 8);
	unsigned	c[8],
				c2v[8];
	int			L;

	*c1 = *c2 = *c3 = 0;
	if (!idx || !ord || !seen || !keys)
	{
		fprintf(stderr, "out of memory at ncols=%d b=%d\n", ncols, b);
		exit(2);
	}

	for (p = 0; p < N; p++)
	{
		pgc_digits(p, ncols, b, c);
		enc(c, ncols, b, keys + (size_t) p * ncols * 8);
		pgc_enc_calls++;
		idx[p] = pgc_key_index(keys + (size_t) p * ncols * 8, ncols, b);
		if (idx[p] >= N)
			(*c1)++;
		else
		{
			if (seen[idx[p]])
				(*c1)++;
			seen[idx[p]] = 1;
		}
		ord[p] = p;
	}
	for (p = 0; p < N; p++)
		if (!seen[p])
			(*c1)++;

	pgc_g_ncols = ncols;
	pgc_g_keys = keys;
	qsort(ord, N, sizeof(unsigned), pgc_cmpkey);
	for (p = 1; p < N; p++)
	{
		int			j,
					ndiff = 0,
					ok = 1;

		pgc_digits(ord[p - 1], ncols, b, c);
		pgc_digits(ord[p], ncols, b, c2v);
		for (j = 0; j < ncols; j++)
			if (c[j] != c2v[j])
			{
				unsigned	d = c[j] > c2v[j] ? c[j] - c2v[j] : c2v[j] - c[j];

				ndiff++;
				if (d != 1)
					ok = 0;
			}
		if (ndiff != 1 || !ok)
			(*c2)++;
	}

	for (L = 1; L <= b; L++)
	{
		unsigned	ncube = 1u << (ncols * L),
					k;
		unsigned   *mn = malloc(sizeof(unsigned) * (size_t) ncube);
		unsigned   *mx = malloc(sizeof(unsigned) * (size_t) ncube);
		unsigned   *ct = calloc(ncube, sizeof(unsigned));

		for (k = 0; k < ncube; k++)
		{
			mn[k] = 0xffffffffu;
			mx[k] = 0;
		}
		for (p = 0; p < N; p++)
		{
			unsigned	id = 0;
			int			j;

			pgc_digits(p, ncols, b, c);
			for (j = 0; j < ncols; j++)
				id = (id << L) | (c[j] >> (b - L));
			if (idx[p] < mn[id])
				mn[id] = idx[p];
			if (idx[p] > mx[id])
				mx[id] = idx[p];
			ct[id]++;
		}
		for (k = 0; k < ncube; k++)
			if (ct[k] && mx[k] - mn[k] + 1 != ct[k])
				(*c3)++;
		free(mn);
		free(mx);
		free(ct);
	}

	free(idx);
	free(ord);
	free(seen);
	free(keys);
}
#endif
CEOF

# ---- the controls ----------------------------------------------------------
#
# This program needs nothing from src/. It exists so the battery's instrument is
# proved to DISCRIMINATE before the real encoder is written, and it keeps proving
# it afterwards.
cat > "$WORK/controls.c" <<'CEOF'
/*
 * Two deliberately wrong encoders, measured by the same C1/C2/C3 checkers the
 * battery uses.
 *
 * SERPENTINE must pass C2 and fail C3. Z-ORDER must fail C2 and pass C3. That
 * pair is the argument: adjacency alone certifies a serpentine, contiguity alone
 * certifies the Z-order that ships today, and only the conjunction is a Hilbert
 * curve. Each property therefore has a control on both sides -- one encoder that
 * must pass it and one that must fail it -- so a checker stuck at "pass" or
 * stuck at "fail" reddens this file instead of passing the battery.
 *
 * C1 had no such pair, though the header claimed it did: the serpentine and
 * Z-order are BOTH permutations, so nothing here could make the permutation
 * checker report a violation, and C1 is the arm the file sells hardest. TRUNCATE
 * is that missing side -- it drops the low bit of the index, so half the range
 * is hit twice and half never, and C1 must go red for every case under it.
 */
#include "props.h"

/*
 * The interleave loop out of cluster_zorder_key in src/columnar_vacuum.c. This
 * program deliberately does NOT link the code under test.
 *
 * "Transcribed from the shipped loop" was a comment, and no arm could falsify
 * it: measured, a Z-order over REVERSED columns -- which is not what ships --
 * left all seven control arms green with identical violation counts. The
 * citation also carried a hand-maintained line number that rots the moment
 * anything above it moves.
 *
 * Both are fixed by an arm rather than by prose: main() below packs the eight
 * golden vectors with THIS loop and prints them, and the shell holds the result
 * to PGC_GOLDEN_ZORDER -- the frozen record of the shipped bytes that C7 holds
 * the code under test to. A ctl_pack that is not the shipped packing now
 * reddens this file.
 */
static void
ctl_pack(const pgc_u64 *ord, int ncols, unsigned char *out)
{
	int			c,
				r,
				outbit = 0;

	memset(out, 0, (size_t) ncols * 8);
	for (r = 63; r >= 0; r--)
	{
		for (c = 0; c < ncols; c++)
		{
			if ((ord[c] >> r) & 1)
				out[outbit >> 3] |= (unsigned char) (0x80 >> (outbit & 7));
			outbit++;
		}
	}
}

/*
 * Boustrophedon scan. Column 0 is the outermost axis and the next axis reverses
 * whenever the position along this one is odd, which is what makes every step a
 * unit step: the scan turns around at the end of each row rather than jumping
 * back to its start.
 */
static unsigned
ctl_serpentine_index(const unsigned *c, int ncols, int b)
{
	unsigned	M = (1u << b) - 1,
				idx = 0,
				rev = 0,
				v;
	int			i;

	for (i = 0; i < ncols; i++)
	{
		v = rev ? (M - c[i]) : c[i];
		idx = (idx << b) | v;
		rev = v & 1;
	}
	return idx;
}

static void
enc_serpentine(const unsigned *c, int ncols, int b, unsigned char *key)
{
	pgc_u64		X[8];

	pgc_index_to_transpose(ctl_serpentine_index(c, ncols, b), ncols, b, X);
	ctl_pack(X, ncols, key);
}

/*
 * C1's negative control: the serpentine index with its low bit dropped. Every
 * even index is then hit twice and every odd one never, so the permutation
 * checker must report violations in every one of the 52 cases. Without it
 * nothing in this file ever made C1 go red, and "no collisions" is exactly the
 * property a truncating encoder satisfies.
 */
static void
enc_truncate(const unsigned *c, int ncols, int b, unsigned char *key)
{
	pgc_u64		X[8];

	pgc_index_to_transpose(ctl_serpentine_index(c, ncols, b) & ~1u, ncols, b, X);
	ctl_pack(X, ncols, key);
}

/* Z-order: ordinals straight into the pack, no transpose. The packing is pinned
 * against the frozen Z-order record below rather than asserted in a comment. */
static void
enc_zorder(const unsigned *c, int ncols, int b, unsigned char *key)
{
	pgc_u64		X[8];
	int			j;

	for (j = 0; j < ncols; j++)
		X[j] = (pgc_u64) c[j] << (64 - b);
	ctl_pack(X, ncols, key);
}

int
main(void)
{
	int			ncols,
				b;
	long		cases = 0,
				serp_c1 = 0,
				serp_c2 = 0,
				serp_c3 = 0,
				zord_c1 = 0,
				zord_c2 = 0,
				zord_c3 = 0;
	long		serp_c3_want = 0,
				serp_c3_got = 0,
				zord_c2_want = 0,
				zord_c2_got = 0,
				trunc_c1_want = 0,
				trunc_c1_got = 0;
	double		points = 0;

	for (ncols = 1; ncols <= 8; ncols++)
	{
		for (b = 1; ncols * b <= 20; b++)
		{
			long		a1,
						a2,
						a3,
						b1,
						b2,
						b3,
						t1,
						t2,
						t3;

			cases++;
			points += (double) (1u << (ncols * b));
			pgc_props(enc_serpentine, ncols, b, &a1, &a2, &a3);
			pgc_props(enc_zorder, ncols, b, &b1, &b2, &b3);
			pgc_props(enc_truncate, ncols, b, &t1, &t2, &t3);
			trunc_c1_want++;
			if (t1 > 0)
				trunc_c1_got++;
			(void) t2;
			(void) t3;
			serp_c1 += a1;
			serp_c2 += a2;
			serp_c3 += a3;
			zord_c1 += b1;
			zord_c2 += b2;
			zord_c3 += b3;

			/*
			 * A serpentine's sub-cubes can only be scattered where a proper
			 * sub-cube exists: ncols >= 2 and b >= 2. Below that the scan IS the
			 * curve and contiguity holds, so those cases are not evidence either
			 * way and are excluded from the "must fail" domain rather than
			 * absorbed into a total that would hide them.
			 */
			if (ncols >= 2 && b >= 2)
			{
				serp_c3_want++;
				if (a3 > 0)
					serp_c3_got++;
			}
			/* Z-order jumps at the first quadrant boundary, so one column is the
			 * only case where it cannot: at ncols == 1 it is the identity. */
			if (ncols >= 2)
			{
				zord_c2_want++;
				if (b2 > 0)
					zord_c2_got++;
			}
		}
	}

	/*
	 * The control's pack must be the shipped packing, and this is the arm that
	 * says so: one line holding every golden Z-order vector, compared in the
	 * shell against the frozen table.
	 */
	{
		int			n,
					k;
		pgc_u64		V[8];
		unsigned char key[64];
		char		h[200];

		printf("ctl_zorder_all=");
		for (n = 1; n <= 8; n++)
			for (k = 0; k < PGC_NVEC; k++)
			{
				pgc_makevec(k, n, V);
				ctl_pack(V, n, key);
				pgc_hex(key, n * 8, h);
				printf("%s", h);
			}
		printf("\n");
	}

	printf("ctl_cases=%ld\n", cases);
	printf("ctl_points=%.0f\n", points);
	printf("ctl_enc_calls=%ld\n", pgc_enc_calls);
	printf("trunc_c1_cases=%ld\n", trunc_c1_want);
	printf("trunc_c1_cases_that_failed=%ld\n", trunc_c1_got);
	printf("serp_c1=%ld\n", serp_c1);
	printf("serp_c2=%ld\n", serp_c2);
	printf("serp_c3_violations=%ld\n", serp_c3);
	printf("serp_c3_nested_cases=%ld\n", serp_c3_want);
	printf("serp_c3_nested_cases_that_failed=%ld\n", serp_c3_got);
	printf("zord_c1=%ld\n", zord_c1);
	printf("zord_c2_violations=%ld\n", zord_c2);
	printf("zord_c2_multicol_cases=%ld\n", zord_c2_want);
	printf("zord_c2_multicol_cases_that_failed=%ld\n", zord_c2_got);
	printf("zord_c3=%ld\n", zord_c3);
	return 0;
}
CEOF

CTLOUT="$WORK/controls.out"
CTLLOG="$WORK/controls.log"

# Where the instrument arms begin. The seven control arms link nothing from
# src/: with src/columnar_curve.{c,h} deleted they print seven PASSes, which is
# correct for calibration and misleading to anyone counting greens. They are
# prefixed INSTRUMENT and counted separately below, so "PASSED" can never be
# reached by arms that never touched the code under test.
_pre_checks=$PGC_CHECKS
_pre_passed=$PGC_PASSED
_pre_failed=$PGC_FAILED
if "$CC" -O2 -Wall -I"$WORK" -o "$WORK/controls" "$WORK/controls.c" >"$CTLLOG" 2>&1 \
	&& "$WORK/controls" > "$CTLOUT" 2>>"$CTLLOG"; then
	echo "-- controls: built and ran"
	echo "-- serpentine: C1 violations=$(val "$CTLOUT" serp_c1)" \
		"C2 violations=$(val "$CTLOUT" serp_c2)" \
		"C3 violations=$(val "$CTLOUT" serp_c3_violations)" \
		"in $(val "$CTLOUT" serp_c3_nested_cases_that_failed)" \
		"of $(val "$CTLOUT" serp_c3_nested_cases) nested cases"
	echo "-- Z-order:    C1 violations=$(val "$CTLOUT" zord_c1)" \
		"C2 violations=$(val "$CTLOUT" zord_c2_violations)" \
		"in $(val "$CTLOUT" zord_c2_multicol_cases_that_failed)" \
		"of $(val "$CTLOUT" zord_c2_multicol_cases) multi-column cases," \
		"C3 violations=$(val "$CTLOUT" zord_c3)"

	# The premise. 52 cases and 6,344,330 points is sum over ncols 1..8 and every
	# b with ncols*b <= 20 of 2^(ncols*b), computed independently of this program.
	# Pinned exactly rather than bounded: a checker that silently stopped early
	# would otherwise still satisfy "more than a few".
	check "INSTRUMENT C0 premise: the control battery encoded every case and every point" \
		"$(val "$CTLOUT" ctl_cases) cases, $(val "$CTLOUT" ctl_points) points" \
		"52 cases, 6344330 points"
	# And the same number counted where the encoders are CALLED, not derived from
	# the loop bounds: three encoders over 6,344,330 points each.
	check_num "INSTRUMENT C0 premise: the control encoders were called once per point" \
		"$(val "$CTLOUT" ctl_enc_calls)" "19032990"

	# The control's pack is the shipped packing, asserted rather than asserted in
	# a comment. Built from the same frozen table C7 holds the code under test to.
	_zall=""
	for _n in 1 2 3 4 5 6 7 8; do
		for _v in $PGC_VECNAMES; do
			_zall="$_zall$(frozen "$PGC_GOLDEN_ZORDER" "$_n" "$_v")"
		done
	done
	check_text "INSTRUMENT the control's own pack reproduces the frozen Z-order bytes" \
		"$(val "$CTLOUT" ctl_zorder_all)" "$_zall"

	check_num "INSTRUMENT C1 control: the serpentine is a permutation of the index range" \
		"$(val "$CTLOUT" serp_c1)" "0"
	check_num "INSTRUMENT C2 control: the serpentine PASSES unit-step adjacency" \
		"$(val "$CTLOUT" serp_c2)" "0"
	check "INSTRUMENT C3 control: the serpentine FAILS dyadic contiguity in every nested case" \
		"$(val "$CTLOUT" serp_c3_nested_cases_that_failed) of $(val "$CTLOUT" serp_c3_nested_cases)" \
		"25 of 25"

	check_num "INSTRUMENT C1 control: Z-order is a permutation of the index range" \
		"$(val "$CTLOUT" zord_c1)" "0"
	check "INSTRUMENT C2 control: Z-order FAILS unit-step adjacency in every multi-column case" \
		"$(val "$CTLOUT" zord_c2_multicol_cases_that_failed) of $(val "$CTLOUT" zord_c2_multicol_cases)" \
		"32 of 32"
	check_num "INSTRUMENT C3 control: Z-order PASSES dyadic contiguity" \
		"$(val "$CTLOUT" zord_c3)" "0"
	# C1's missing side. Both encoders above are permutations, so until this arm
	# existed nothing here could make the permutation checker report a violation.
	check "INSTRUMENT C1 control: a truncating encoder FAILS the permutation test in every case" \
		"$(val "$CTLOUT" trunc_c1_cases_that_failed) of $(val "$CTLOUT" trunc_c1_cases)" \
		"52 of 52"
else
	echo "---- the control program would not build or run ----"
	sed 's/^/  /' "$CTLLOG"
	while IFS= read -r _a; do
		[ -n "$_a" ] && pgc_fail "$_a" "the control program would not build or run"
	done <<< "$CONTROL_ARMS"
fi
echo "-- instrument arms: $((PGC_CHECKS - _pre_checks)) reported" \
	"($((PGC_PASSED - _pre_passed)) passed, $((PGC_FAILED - _pre_failed)) failed);" \
	"none of them links src/columnar_curve.c"
_ctl_checks=$((PGC_CHECKS - _pre_checks))
_ctl_passed=$((PGC_PASSED - _pre_passed))

# ---- the battery -----------------------------------------------------------

CURVE_C="$SRCDIR/src/columnar_curve.c"
CURVE_H="$SRCDIR/src/columnar_curve.h"
BATOUT="$WORK/battery.out"
BATLOG="$WORK/battery.log"

cat > "$WORK/battery.c" <<'CEOF'
/*
 * The battery proper. This is the only program here that links the code under
 * test, so everything it reports is about src/columnar_curve.c.
 *
 * It prints name=value lines and asserts nothing. Every expected value lives in
 * the shell, out of this program's reach, because a program holding both the
 * question and the answer can only report that it agrees with itself.
 */
#include "postgres.h"
#include "columnar_curve.h"
#include "props.h"

/*
 * A compile-time pin on the interface #889 agreed. Built with
 * -Werror=incompatible-pointer-types, so a changed signature is a build failure
 * and this suite goes red for the right reason rather than adapting silently.
 */
static void (*const pin_transpose) (uint64 *, int) = cluster_hilbert_transpose;
static void (*const pin_pack) (const uint64 *, int, unsigned char *) = cluster_pack_interleave;

/*
 * Every key buffer is filled with 0xa5 before the pack writes it. A byte the pack
 * never establishes then reads 0xa5 -- the same in every run and in the pytest
 * twin -- rather than whatever the previous key left there, or, on the first use of
 * a buffer, uninitialized memory. Measured: under a pack whose memset stops one
 * byte short, which goldens went red depended on what the previous call had left
 * in that byte. The C5 extent arms below fill their own buffers with 0x00 and 0xff
 * on purpose and call the pack directly; nothing else does.
 */
static void
pk(const uint64 *ord, int ncols, unsigned char *out)
{
	memset(out, 0xa5, (size_t) ncols * 8);
	cluster_pack_interleave(ord, ncols, out);
}

/* Deterministic, so two runs of this suite compare the same points. */
static unsigned rs = 987654321u;
static unsigned
rnd(void)
{
	rs = rs * 1103515245u + 12345u;
	return rs >> 1;
}
static uint64
rnd64(void)
{
	return ((uint64) rnd() << 40) ^ ((uint64) rnd() << 20) ^ (uint64) rnd();
}

/*
 * splitmix64, for the C4b keys.
 *
 * They were built byte by byte from `rnd() & 0xff`. rs is a 32-bit LCG and
 * rnd() returns rs >> 1, so bit j of rnd() is bit j+1 of rs and has period
 * 2^(j+2): the low byte repeats every 512 draws. A key of 8*ncols bytes cut
 * from a period-512 stream therefore takes 512/gcd(512, 8*ncols) values --
 * measured 64, 32, 64, 16, 64, 32, 64 and 8 distinct keys for ncols 1..8, 344
 * in all, while the premise arm certified 24,000. At ncols == 8 the same eight
 * keys were round-tripped 375 times each.
 *
 * That is what let a WRONG encoder pass this file. A correct Skilling transpose
 * plus "if (ncols == 8 and the low byte of the transposed X[3] is 0x5a) flip
 * bit 0 of X[0]" was compiled against the suite as it stood and reported
 * "138 passed + 0 failed". Nothing below the top ncols*b bits of the key was
 * pinned by more than a handful of inputs.
 *
 * splitmix64 has full 64-bit period, and the arm below counts and asserts the
 * DISTINCT keys it round-tripped rather than the number of iterations it ran.
 */
static uint64 sm = UINT64_C(0x243F6A8885A308D3);
static uint64
sm64(void)
{
	uint64		z = (sm += UINT64_C(0x9E3779B97F4A7C15));

	z = (z ^ (z >> 30)) * UINT64_C(0xBF58476D1CE4E5B9);
	z = (z ^ (z >> 27)) * UINT64_C(0x94D049BB133111EB);
	return z ^ (z >> 31);
}

/* memcmp over a fixed-width key, for the distinct-key count. */
static int	pgc_klen;
static int
cmp_rawkey(const void *a, const void *b)
{
	return memcmp(a, b, (size_t) pgc_klen);
}

/* The transpose a full-width key carries: the pack loop run backwards. */
static void
unpack(const unsigned char *key, int n, uint64 *X)
{
	int			t,
				nb = n * 64;

	for (t = 0; t < n; t++)
		X[t] = 0;
	for (t = 0; t < nb; t++)
		X[t % n] |= (uint64) ((key[t >> 3] >> (7 - (t & 7))) & 1) << (63 - (t / n));
}

/*
 * TransposetoAxes, transcribed from Skilling's published listing at b = 64.
 * Deliberately NOT derived from cluster_hilbert_transpose: an inverse written by
 * inverting our own forward code makes the round trip a tautology.
 *
 * N is 2 << (b-1), which is 0 at b == 64, and the loop `Q = 2; Q != N; Q <<= 1`
 * therefore stops when Q wraps past 2^63. That is the published code's own
 * arithmetic, kept rather than rewritten.
 *
 * mutate drops one line -- the Gray-decode fold back into X[0]. That is the
 * negative control for arm C4b: with it set, every round trip must break.
 */
static void
untranspose(uint64 *X, int n, int mutate)
{
	uint64		N = 0;
	uint64		P,
				Q,
				t;
	int			i;

	t = X[n - 1] >> 1;
	for (i = n - 1; i > 0; i--)
		X[i] ^= X[i - 1];
	if (!mutate)
		X[0] ^= t;			/* THE MUTATION IS DROPPING THIS LINE */
	for (Q = 2; Q != N; Q <<= 1)
	{
		P = Q - 1;
		for (i = n - 1; i >= 0; i--)
		{
			if (X[i] & Q)
				X[0] ^= P;
			else
			{
				t = (X[0] ^ X[i]) & P;
				X[0] ^= t;
				X[i] ^= t;
			}
		}
	}
}

static void
enc_hilbert(const unsigned *c, int ncols, int b, unsigned char *key)
{
	uint64		X[8];
	int			j;

	for (j = 0; j < ncols; j++)
		X[j] = (uint64) c[j] << (64 - b);
	cluster_hilbert_transpose(X, ncols);
	pk(X, ncols, key);
}

int
main(void)
{
	int			ncols,
				b,
				k,
				j;
	uint64		X[8],
				Y[8];
	unsigned char key[64],
				key2[64];
	char		h[200];

	/* ---- C1, C2, C3 ---------------------------------------------------- */
	{
		long		cases = 0,
					c1 = 0,
					c2 = 0,
					c3 = 0;
		double		points = 0;

		for (ncols = 1; ncols <= 8; ncols++)
			for (b = 1; ncols * b <= 20; b++)
			{
				long		a1,
							a2,
							a3;

				cases++;
				points += (double) (1u << (ncols * b));
				pgc_props(enc_hilbert, ncols, b, &a1, &a2, &a3);
				c1 += a1;
				c2 += a2;
				c3 += a3;
			}
		printf("cases=%ld\n", cases);
		printf("points=%.0f\n", points);
		printf("enc_calls=%ld\n", pgc_enc_calls);
		printf("c1=%ld\n", c1);
		printf("c2=%ld\n", c2);
		printf("c3=%ld\n", c3);
	}

	/* ---- C4a: the full-width key prefix is the verified sub-cube index ---
	 *
	 * The point is put at the TOP of a sub-cube's coordinate range and then the
	 * low 64-b bits are filled with noise, so the ordinals are production width
	 * and land anywhere inside the sub-cube. The top ncols*b bits of the key must
	 * still be the index C1 to C3 verified exhaustively for that sub-cube. That
	 * is the bridge from an exhaustible grid to the width the product uses.
	 *
	 * THE MISMATCH COUNT ALONE IS A SELF-COMPARISON. kb and kf are both produced
	 * by the code under test, so an encoder whose output does not depend on its
	 * input satisfies it: measured, with both product functions replaced by a
	 * constant fill, c4a_mismatch was 0 and the arm passed. The word "verified"
	 * in its name was carried entirely by other arms.
	 *
	 * So the full-width prefixes are also run through C1's own permutation test:
	 * the multiset of prefixes over p in [0, N) must be exactly [0, N). A
	 * constant or collapsing encoder now fails this arm on its own.
	 */
	{
		long		mismatch = 0,
					permbad = 0;
		double		points = 0;
		long		cases = 0;

		for (ncols = 2; ncols <= 8; ncols++)
			for (b = 1; ncols * b <= 20; b++)
			{
				unsigned	N = 1u << (ncols * b),
							p;
				unsigned char *seen = calloc(N, 1);

				if (!seen)
				{
					fprintf(stderr, "out of memory in C4a\n");
					exit(2);
				}
				cases++;
				for (p = 0; p < N; p++)
				{
					unsigned	c[8],
								ixf;
					uint64		Xb[8],
								Xf[8];
					unsigned char kb[64],
								kf[64];

					pgc_digits(p, ncols, b, c);
					for (j = 0; j < ncols; j++)
					{
						Xb[j] = (uint64) c[j] << (64 - b);
						Xf[j] = Xb[j] | (rnd64() & ((((uint64) 1) << (64 - b)) - 1));
					}
					cluster_hilbert_transpose(Xb, ncols);
					pk(Xb, ncols, kb);
					cluster_hilbert_transpose(Xf, ncols);
					pk(Xf, ncols, kf);
					points += 1;
					ixf = pgc_key_index(kf, ncols, b);
					if (pgc_key_index(kb, ncols, b) != ixf)
						mismatch++;
					if (ixf >= N)
						permbad++;
					else
					{
						if (seen[ixf])
							permbad++;
						seen[ixf] = 1;
					}
				}
				for (p = 0; p < N; p++)
					if (!seen[p])
						permbad++;
				free(seen);
			}
		printf("c4a_cases=%ld\n", cases);
		printf("c4a_points=%.0f\n", points);
		printf("c4a_mismatch=%ld\n", mismatch);
		printf("c4a_perm_bad=%ld\n", permbad);
	}

	/* ---- C4b: the published inverse, at production width ----------------- */
	{
		long		trips = 0,
					tripbad = 0,
					mutbad = 0,
					adj = 0,
					adjbad = 0,
					adjreenc = 0,
					distinct = 0;
		int			t;
		static unsigned char ks[3000][64];

		for (ncols = 1; ncols <= 8; ncols++)
		{
			for (t = 0; t < 3000; t++)
			{
				uint64		T[8],
							A0[8],
							A1[8],
							B[8];
				unsigned char kk[64];
				int			c,
							carry,
							ok0,
							ok1;

				/* splitmix64, not the LCG's low byte -- see sm64() above. */
				for (c = 0; c < ncols * 8; c++)
					key[c] = (unsigned char) (sm64() >> 56);
				memcpy(ks[t], key, (size_t) ncols * 8);

				unpack(key, ncols, T);
				memcpy(A0, T, sizeof(uint64) * ncols);
				untranspose(A0, ncols, 0);
				memcpy(B, A0, sizeof(uint64) * ncols);
				cluster_hilbert_transpose(B, ncols);
				pk(B, ncols, kk);
				trips++;
				ok0 = (memcmp(kk, key, (size_t) ncols * 8) == 0);
				if (!ok0)
					tripbad++;

				/* the same round trip through the mutated inverse */
				memcpy(A1, T, sizeof(uint64) * ncols);
				untranspose(A1, ncols, 1);
				memcpy(B, A1, sizeof(uint64) * ncols);
				cluster_hilbert_transpose(B, ncols);
				pk(B, ncols, kk);
				if (memcmp(kk, key, (size_t) ncols * 8) != 0)
					mutbad++;

				/* key + 1, as a big-endian integer, must decode to a neighbour */
				memcpy(key2, key, (size_t) ncols * 8);
				carry = 1;
				for (c = ncols * 8 - 1; c >= 0 && carry; c--)
				{
					if (key2[c] == 0xff)
						key2[c] = 0;
					else
					{
						key2[c]++;
						carry = 0;
					}
				}
				if (carry)
					continue;	/* wrapped past the last index; not a pair */
				unpack(key2, ncols, T);
				memcpy(A1, T, sizeof(uint64) * ncols);
				untranspose(A1, ncols, 0);

				/*
				 * ROUTE THE ADJACENCY THROUGH THE CODE UNDER TEST.
				 *
				 * A0 and A1 come only from unpack() and untranspose(), both
				 * defined in this file, so comparing them to each other is a
				 * self-test of the fixture: measured, this arm stayed green with
				 * BOTH product functions destroyed, while 89 other arms went red.
				 * src/columnar_curve.c could have been dropped from the link and
				 * the arm would still have passed.
				 *
				 * So both decoded points are re-encoded here with
				 * cluster_hilbert_transpose and cluster_pack_interleave, the two
				 * keys must come back byte-identical, and the adjacency is
				 * counted only over the pairs the encoder actually reproduced. A
				 * wrong encoder now either loses the re-encode count or loses the
				 * adjacency, and either way the arm goes red.
				 */
				memcpy(B, A1, sizeof(uint64) * ncols);
				cluster_hilbert_transpose(B, ncols);
				pk(B, ncols, kk);
				ok1 = (memcmp(kk, key2, (size_t) ncols * 8) == 0);

				adj++;
				if (!ok0 || !ok1)
					continue;
				adjreenc++;
				{
					int			nd = 0,
								ok = 1;

					for (c = 0; c < ncols; c++)
						if (A0[c] != A1[c])
						{
							uint64		d = A0[c] > A1[c] ? A0[c] - A1[c] : A1[c] - A0[c];

							nd++;
							if (d != 1)
								ok = 0;
						}
					if (nd != 1 || !ok)
						adjbad++;
				}
			}

			/*
			 * How many keys this ncols actually round-tripped, counted rather
			 * than assumed. The premise arm asserts THIS, not the loop count.
			 */
			pgc_klen = ncols * 8;
			qsort(ks, 3000, sizeof(ks[0]), cmp_rawkey);
			distinct++;
			for (t = 1; t < 3000; t++)
				if (memcmp(ks[t - 1], ks[t], (size_t) pgc_klen) != 0)
					distinct++;
		}
		printf("c4b_roundtrips=%ld\n", trips);
		printf("c4b_distinct_keys=%ld\n", distinct);
		printf("c4b_roundtrip_bad=%ld\n", tripbad);
		printf("c4b_mutated_bad=%ld\n", mutbad);
		printf("c4b_adjacent=%ld\n", adj);
		printf("c4b_adjacent_reencoded=%ld\n", adjreenc);
		printf("c4b_adjacent_bad=%ld\n", adjbad);
	}

	/* ---- C5: golden keys, bit order, and the byte count ------------------ */
	for (ncols = 1; ncols <= 8; ncols++)
		for (k = 0; k < PGC_NVEC; k++)
		{
			pgc_makevec(k, ncols, X);
			memcpy(Y, X, sizeof(uint64) * ncols);
			cluster_hilbert_transpose(Y, ncols);
			pk(Y, ncols, key);
			pgc_hex(key, ncols * 8, h);
			printf("golden_n%d_%s=%s\n", ncols, pgc_vecname[k], h);
		}

	{
		uint64		v[2];

		v[0] = (uint64) 1 << 63;
		v[1] = 0;
		pk(v, 2, key);
		pgc_hex(key, 16, h);
		printf("packpin_hi=%s\n", h);
		v[0] = 0;
		v[1] = (uint64) 1 << 63;
		pk(v, 2, key);
		pgc_hex(key, 16, h);
		printf("packpin_lo=%s\n", h);
	}

	/*
	 * How far the key extends, measured rather than assumed.
	 *
	 * The same vector is packed into a buffer pre-filled with 0x00 and one
	 * pre-filled with 0xff. A byte the pack established holds the same value in
	 * both; a byte it never touched still holds its fill and differs. So the
	 * count of leading agreeing bytes is the extent of the key, and the tail
	 * must still hold its fill.
	 *
	 * WHAT THIS ARM DOES NOT SEE, AND WHAT ITS NAME MAY THEREFORE NOT SAY. The
	 * pack zeroes the whole 8*ncols region before it writes, and the memset
	 * alone makes both buffers agree across the region whatever the loop then
	 * does. Measured: a pack that is NOTHING BUT the memset -- it never reads
	 * ord[] -- passes all eight of these arms and all eight of the tail arms,
	 * and a pack whose loop runs only 32 of its 64 rounds passes them too, while
	 * moving the memset one byte short reddens six of the eight. The subject is
	 * the memset's extent, so the arm is named for the extent, and it is paired
	 * with the content arm below so that "nothing past the key" cannot be
	 * satisfied by "nothing anywhere".
	 */
	for (ncols = 1; ncols <= 8; ncols++)
	{
		unsigned char a[200],
					z[200];
		int			n,
					intact = 1;

		memset(a, 0x00, sizeof(a));
		memset(z, 0xff, sizeof(z));
		pgc_makevec(5, ncols, X);
		cluster_pack_interleave(X, ncols, a);
		cluster_pack_interleave(X, ncols, z);
		for (n = 0; n < (int) sizeof(a) && a[n] == z[n]; n++)
			 /* count */ ;
		for (k = ncols * 8; k < (int) sizeof(a); k++)
			if (a[k] != 0x00 || z[k] != 0xff)
				intact = 0;
		printf("packlen_n%d=%d\n", ncols, n);
		printf("packtail_n%d=%s\n", ncols, intact ? "intact" : "clobbered");
	}

	/*
	 * The content arm the two above cannot be. An all-ones ordinal vector sets
	 * every bit the pack emits, so the key must be 8*ncols bytes of 0xff with
	 * nothing beyond. A loop that stops short leaves 0x00 INSIDE the region --
	 * verified against a pack truncated to 32 rounds, which produces 4*ncols
	 * bytes of 0xff and then zeros -- and a pack that never reads ord[] leaves
	 * the whole region zero.
	 */
	for (ncols = 1; ncols <= 8; ncols++)
	{
		unsigned char a[200];
		int			nff = 0,
					intact = 1;

		memset(a, 0x00, sizeof(a));
		pgc_makevec(1, ncols, X);	/* every ordinal UINT64_MAX */
		cluster_pack_interleave(X, ncols, a);
		while (nff < (int) sizeof(a) && a[nff] == 0xff)
			nff++;
		for (k = ncols * 8; k < (int) sizeof(a); k++)
			if (a[k] != 0x00)
				intact = 0;
		printf("packones_n%d=%d of %d ff, tail %s\n",
			   ncols, nff, ncols * 8, intact ? "intact" : "clobbered");
	}

	/* ---- C6: ncols == 1 --------------------------------------------------- */
	{
		static const char *const nm[4] = {"zero", "one", "topbit", "max"};
		uint64		vals[4];
		long		mism = 0,
					absmism = 0,
					n6 = 0;
		int			t;

		vals[0] = 0;
		vals[1] = 1;
		vals[2] = (uint64) 1 << 63;
		vals[3] = UINT64_MAX;
		for (k = 0; k < 4; k++)
		{
			X[0] = vals[k];
			cluster_hilbert_transpose(X, 1);
			pk(X, 1, key);
			pgc_hex(key, 8, h);
			printf("ident_n1_%s=%s\n", nm[k], h);
		}

		/*
		 * The relative half compares two calls into the code under test, so a
		 * packing bug that hits both identically survives it and a pack that
		 * writes nothing satisfies it with eight zero bytes on each side. It is
		 * kept -- it is the arm that says the Hilbert path and the Z-order path
		 * agree at one column -- but the same 200,000 values are ALSO compared
		 * against the big-endian bytes of x built right here, by shifting, with
		 * nothing from src/ in the path. That turns the four absolute hex pins
		 * into 200,004 of them.
		 */
		for (t = 0; t < 200000; t++)
		{
			uint64		x = rnd64();
			unsigned char kh[8],
						kz[8],
						be[8];
			int			q;

			X[0] = x;
			cluster_hilbert_transpose(X, 1);
			pk(X, 1, kh);
			Y[0] = x;
			pk(Y, 1, kz);
			for (q = 0; q < 8; q++)
				be[q] = (unsigned char) (x >> (56 - 8 * q));
			n6++;
			if (memcmp(kh, kz, 8) != 0)
				mism++;
			if (memcmp(kh, be, 8) != 0)
				absmism++;
		}
		printf("c6_random=%ld\n", n6);
		printf("c6_random_mismatch=%ld\n", mism);
		printf("c6_random_absolute_mismatch=%ld\n", absmism);
	}

	/* ---- C7: the Z-order key, which must not have moved ------------------ */
	for (ncols = 1; ncols <= 8; ncols++)
		for (k = 0; k < PGC_NVEC; k++)
		{
			pgc_makevec(k, ncols, X);
			pk(X, ncols, key);
			pgc_hex(key, ncols * 8, h);
			printf("zorder_n%d_%s=%s\n", ncols, pgc_vecname[k], h);
		}

	(void) pin_transpose;
	(void) pin_pack;
	return 0;
}
CEOF

bat_ready=no
if [ ! -f "$CURVE_C" ] || [ ! -f "$CURVE_H" ]; then
	_missing=""
	[ -f "$CURVE_C" ] || _missing="$_missing src/columnar_curve.c"
	[ -f "$CURVE_H" ] || _missing="$_missing src/columnar_curve.h"
	echo "-- the code under test is absent:$_missing"
	while IFS= read -r _a; do
		[ -n "$_a" ] && pgc_fail "$_a" "the code under test is absent:$_missing"
	done <<< "$BATTERY_ARMS"
else
	cp "$CURVE_C" "$CURVE_H" "$WORK/"
	if "$CC" -O2 -Wall -Werror=incompatible-pointer-types -I"$WORK" \
		-o "$WORK/battery" "$WORK/battery.c" "$WORK/columnar_curve.c" \
		>"$BATLOG" 2>&1 && "$WORK/battery" > "$BATOUT" 2>>"$BATLOG"; then
		bat_ready=yes
		echo "-- battery: built and ran against $CURVE_C"
	else
		echo "---- the battery would not build or run against $CURVE_C ----"
		sed 's/^/  /' "$BATLOG" | head -40
		while IFS= read -r _a; do
			[ -n "$_a" ] && pgc_fail "$_a" "the battery would not build or run; see the log above"
		done <<< "$BATTERY_ARMS"
	fi
fi

if [ "$bat_ready" = yes ]; then
	echo "-- C1/C2/C3 violations: $(val "$BATOUT" c1) / $(val "$BATOUT" c2) / $(val "$BATOUT" c3)"

	# 52 cases and 6,344,330 points is sum over ncols 1..8 and every b with
	# ncols*b <= 20 of 2^(ncols*b), computed independently of this program. Pinned
	# exactly, not bounded: a loop that stopped early would still satisfy a bound.
	check "C0 premise: the battery encoded every case and every point" \
		"$(val "$BATOUT" cases) cases, $(val "$BATOUT" points) points" \
		"52 cases, 6344330 points"
	# And the same number counted where enc() is CALLED. The arm above is
	# arithmetic over the loop bounds and reports 6,344,330 whether or not
	# anything was encoded: it passed under every mutant tried, including one
	# whose two product functions did nothing at all.
	check_num "C0 premise: the encoder was called once per point" \
		"$(val "$BATOUT" enc_calls)" "6344330"

	check_num "C1 the Hilbert index set is exactly the contiguous range" \
		"$(val "$BATOUT" c1)" "0"
	check_num "C2 consecutive points in memcmp key order are unit-adjacent" \
		"$(val "$BATOUT" c2)" "0"
	check_num "C3 every dyadic sub-cube occupies a contiguous run of indices" \
		"$(val "$BATOUT" c3)" "0"

	# 32 cases and 4,247,180 points is the same sum over ncols 2..8. ncols == 1 is
	# excluded because a one-column sub-cube is the whole coordinate, so the
	# bridge has nothing to say there; C6 pins ncols == 1 instead.
	check "C4a premise: the bridge covered every case and every point" \
		"$(val "$BATOUT" c4a_cases) cases, $(val "$BATOUT" c4a_points) points" \
		"32 cases, 4247180 points"
	# Named for what it measures. It used to say "equals the VERIFIED sub-cube
	# index", but both sides are produced by the code under test, so a constant
	# encoder satisfied it and the word "verified" was carried by other arms.
	check_num "C4a the full-width key prefix does not move when the low bits change" \
		"$(val "$BATOUT" c4a_mismatch)" "0"
	# The arm above compares the encoder's own base prefix with its own
	# noise-filled prefix, so a constant encoder satisfies it. This one holds the
	# full-width prefixes to C1's permutation test and a constant encoder cannot.
	check_num "C4a the full-width key prefixes are exactly the index range" \
		"$(val "$BATOUT" c4a_perm_bad)" "0"

	# DISTINCT keys, counted by the program, not iterations counted by the loop.
	# The old premise certified 24,000 round trips over a key stream whose real
	# period gave between 8 and 64 distinct keys per ncols -- 344 in all -- and
	# that gap is what let a wrong encoder pass this file 138/138.
	check_num "C4b premise: 3,000 DISTINCT full-width keys per ncols were round-tripped" \
		"$(val "$BATOUT" c4b_distinct_keys)" "24000"
	check_num "C4b the published inverse round-trips full-width keys" \
		"$(val "$BATOUT" c4b_roundtrip_bad)" "0"
	# The control, as a JOINT condition. A round trip against an inverse derived
	# from the forward code passes whatever either one does, so the arm above is
	# evidence only if breaking the inverse alone breaks it. But "every round trip
	# through the mutated inverse failed" is also satisfied when the FORWARD code
	# is broken and every round trip fails for that reason -- measured, the arm
	# passed with both product functions destroyed, which is precisely the state
	# it exists to rule out. So the clean count is asserted in the same arm, and
	# the control can no longer be satisfied by a run whose real round trip failed.
	check "C4b control: a mutated inverse breaks every round trip the clean one made" \
		"clean $(val "$BATOUT" c4b_roundtrip_bad) bad, mutated $(val "$BATOUT" c4b_mutated_bad) of $(val "$BATOUT" c4b_roundtrips)" \
		"clean 0 bad, mutated 24000 of 24000"
	check_num "C4b premise: consecutive key pairs were formed" \
		"$(val "$BATOUT" c4b_adjacent)" "24000"
	# Both decoded points are re-encoded by the code under test and must give the
	# two keys back; the adjacency is counted only over pairs that did. Without
	# the re-encode this arm read only the suite's own inverse and stayed green
	# with src/columnar_curve.c effectively deleted.
	check "C4b consecutive keys decode to unit-adjacent points, re-encoded by the code under test" \
		"$(val "$BATOUT" c4b_adjacent_bad) bad over $(val "$BATOUT" c4b_adjacent_reencoded) re-encoded of $(val "$BATOUT" c4b_adjacent) pairs" \
		"0 bad over 24000 re-encoded of 24000 pairs"

	for _n in 1 2 3 4 5 6 7 8; do
		for _v in $PGC_VECNAMES; do
			check_text "C5 golden Hilbert keys: ncols=$_n $_v" \
				"$(val "$BATOUT" "golden_n${_n}_${_v}")" \
				"$(frozen "$PGC_GOLDEN_HILBERT" "$_n" "$_v")"
		done
	done

	check_text "C5 the pack bit order: pack(2^63, 0) starts 0x80" \
		"$(val "$BATOUT" packpin_hi)" "80000000000000000000000000000000"
	check_text "C5 the pack bit order: pack(0, 2^63) starts 0x40" \
		"$(val "$BATOUT" packpin_lo)" "40000000000000000000000000000000"

	for _n in 1 2 3 4 5 6 7 8; do
		# Named for what it measures. A pack that never reads its input passes
		# both of these; the third is what reads the contents.
		check_num "C5 the pack establishes exactly 8*ncols bytes: ncols=$_n" \
			"$(val "$BATOUT" "packlen_n${_n}")" "$((_n * 8))"
		check_text "C5 the pack wrote nothing past the key: ncols=$_n" \
			"$(val "$BATOUT" "packtail_n${_n}")" "intact"
		check_text "C5 the pack sets every bit of the key for an all-ones vector: ncols=$_n" \
			"$(val "$BATOUT" "packones_n${_n}")" \
			"$((_n * 8)) of $((_n * 8)) ff, tail intact"
	done

	check_text "C6 ncols == 1 is the big-endian ordinal: 0" \
		"$(val "$BATOUT" ident_n1_zero)" "0000000000000000"
	check_text "C6 ncols == 1 is the big-endian ordinal: 1" \
		"$(val "$BATOUT" ident_n1_one)" "0000000000000001"
	check_text "C6 ncols == 1 is the big-endian ordinal: 2^63" \
		"$(val "$BATOUT" ident_n1_topbit)" "8000000000000000"
	check_text "C6 ncols == 1 is the big-endian ordinal: UINT64_MAX" \
		"$(val "$BATOUT" ident_n1_max)" "ffffffffffffffff"
	# Named for what it counts. Measured: wrapping the two encode calls in a
	# condition that skips half of them leaves this arm GREEN at 200,000, because
	# n6 is incremented by the loop and not by the encoder. It is a loop-ran
	# premise and says so; the arm below is what noticed the 100,000 skipped
	# encodes, at "got [100000] want [0]".
	check_num "C6 premise: the comparison loop ran 200,000 times" \
		"$(val "$BATOUT" c6_random)" "200000"
	# The absolute half: the key against the big-endian bytes of the input,
	# built in the battery by shifting, with nothing from src/ on that side.
	check_num "C6 ncols == 1 is the big-endian ordinal over 200,000 random values" \
		"$(val "$BATOUT" c6_random_absolute_mismatch)" "0"
	# The relative half alone is vacuous: a packing bug that hits both paths
	# identically survives it, and a pack that writes nothing satisfies it with
	# eight zero bytes on each side. It says the two paths agree at one column;
	# the arm above is what says WHICH value they agree on.
	check_num "C6 ncols == 1 agrees with Z-order over 200,000 random values" \
		"$(val "$BATOUT" c6_random_mismatch)" "0"

	for _n in 1 2 3 4 5 6 7 8; do
		for _v in $PGC_VECNAMES; do
			check_text "C7 the Z-order pack is byte-for-byte what it was: ncols=$_n $_v" \
				"$(val "$BATOUT" "zorder_n${_n}_${_v}")" \
				"$(frozen "$PGC_GOLDEN_ZORDER" "$_n" "$_v")"
		done
	done
fi

# The battery's own accounting, beside the instrument's. pgc_summary reports one
# total, and seven of its greens used to be arms that never link the code under
# test -- a reader counting greens counted those seven as coverage. Printed here
# so "PASSED" cannot be reached by calibration alone.
echo "-- arm split: $_ctl_checks instrument ($_ctl_passed passed)," \
	"$((PGC_CHECKS - _ctl_checks)) battery" \
	"($((PGC_PASSED - _ctl_passed)) passed)"

pgc_summary
