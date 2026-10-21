"""The Hilbert clustering curve, pinned by its properties and by frozen bytes (#889).
The pytest twin of `test/hilbert_curve.sh`, every check by the shell's name (#1341).

WHAT IS UNDER TEST. `cluster_hilbert_transpose` and `cluster_pack_interleave` in
`src/columnar_curve.c`. The Hilbert key is ordinals -> transpose -> pack; the Z-order key
is ordinals -> pack. Both are pure functions of ncols uint64 words, so no cluster is
started: the file is compiled here, beside a small driver, into a shared library, and
called through ctypes.

INDEPENDENT OF THE SHELL. The shell compiles a C battery that checks the properties in
C. Here the C side only ENCODES: the driver turns ordinal vectors into keys and counts
its calls, and asserts nothing. Every property is checked in Python, with its own
readers: the permutation test (C1), the memcmp-order adjacency walk (C2), the dyadic
contiguity test (C3), the production-width bridge (C4a), a Python transcription of
Skilling's TransposetoAxes for the round trips (C4b), and the three control encoders
that prove the checkers discriminate. The two harnesses share only the frozen tables,
which are records rather than code.

C3 IS ASKED THE SAME QUESTION ANOTHER WAY. The shell takes each dyadic sub-cube's
min and max index and requires max - min + 1 == count. Over a permutation that is
exactly "each sub-cube is one run in index order", and a run boundary between two
consecutive points sits at every level deeper than the levels they share, so one pass
over the consecutive pairs counts the runs at every level at once. Where the encoder is
not a permutation (C1 already red), the shell's own min/max form is computed instead,
so the verdict is the shell's in every case.

FAILS CLOSED. A compiler that cannot build a two-line program declines every arm as
MISSING_DEPENDENCY, as the shell does. An absent or unbuildable code under test is a
failed reading for every battery arm, not a skip. Every reading is a value or a
query_error(), so a broken step reddens its own arms by name.
"""
import array
import collections
import ctypes
import os
import pathlib
import random
import shutil
import subprocess

import pytest

from pgc_vacuity import query_error

ROOT = pathlib.Path(__file__).resolve().parents[2]
CURVE_C = ROOT / "src" / "columnar_curve.c"
CURVE_H = ROOT / "src" / "columnar_curve.h"
VECNAMES = ["zero", "max", "topbit0", "unit", "tie", "mixed", "asym", "lowbit"]
U64 = (1 << 64) - 1
GUARD = 64
CASES = [(n, b) for n in range(1, 9) for b in range(1, 21) if n * b <= 20]
GOLDEN = [(n, v) for n in range(1, 9) for v in VECNAMES]
NCOLS = list(range(1, 9))

# Skilling's AxestoTranspose at b = 64, then the interleave loop. Generated off-tree on
# 2026-09-08 and frozen; not produced by the code under test. The same record the shell
# holds, as a record.
GOLDEN_HILBERT = """
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
"""

# The interleave loop out of cluster_zorder_key in src/columnar_vacuum.c, run at commit
# e84a5e5 on 2026-09-08 BEFORE the refactor. A frozen record of the shipped bytes.
GOLDEN_ZORDER = """
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
"""


def _table(text):
    return {(int(n[1:]), v): h for n, v, h in (l.split() for l in text.strip().splitlines())}


HILBERT = _table(GOLDEN_HILBERT)
ZORDER = _table(GOLDEN_ZORDER)

# The C side: stub headers so src/columnar_curve.c compiles with no server tree, and a
# driver that encodes and counts. It asserts nothing; the interface pin makes a changed
# signature a build failure under -Werror=incompatible-pointer-types.
STUB = """#ifndef PGC_STUB_POSTGRES_H
#define PGC_STUB_POSTGRES_H
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <stdbool.h>
typedef int8_t int8; typedef uint8_t uint8; typedef int16_t int16; typedef uint16_t uint16;
typedef int32_t int32; typedef uint32_t uint32; typedef int64_t int64; typedef uint64_t uint64;
#define Assert(p) ((void) 0)
#define StaticAssertDecl(c, m) extern int pgc_stub_sa_dummy
#define palloc(sz) malloc(sz)
#define palloc0(sz) calloc(1, (sz))
#define pfree(p) free(p)
#define pg_attribute_unused()
#endif
"""
DRIVER = """#include "postgres.h"
#include "columnar_curve.h"
static void (*const pin_transpose) (uint64 *, int) = cluster_hilbert_transpose;
static void (*const pin_pack) (const uint64 *, int, unsigned char *) = cluster_pack_interleave;
static long calls = 0;
long hc_calls(void) { (void) pin_transpose; (void) pin_pack; return calls; }
/* npoints ordinal vectors of ncols words each -> npoints Hilbert keys of 8*ncols bytes */
void hc_hilbert(const uint64 *ord, long npoints, int ncols, unsigned char *out)
{
	long p; uint64 X[8];
	for (p = 0; p < npoints; p++)
	{
		memcpy(X, ord + p * ncols, sizeof(uint64) * ncols);
		memset(out + p * ncols * 8, 0xa5, (size_t) ncols * 8);
		cluster_hilbert_transpose(X, ncols);
		cluster_pack_interleave(X, ncols, out + p * ncols * 8);
		calls++;
	}
}
void hc_pack(const uint64 *ord, int ncols, unsigned char *out) { cluster_pack_interleave(ord, ncols, out); }
"""
# Every key region is filled with 0xa5 before the pack writes it, as the shell's pk()
# does, so a byte the pack never establishes reads 0xa5 in both harnesses. And every
# buffer has GUARD bytes of slack: a pack that writes past its key must redden the C5
# tail arms, not corrupt the heap and take the run down with no name at all.


def _vec(which, ncols):
    """The eight golden ordinal vectors, per ncols."""
    out = []
    for j in range(ncols):
        if which == "zero":
            out.append(0)
        elif which == "max":
            out.append(U64)
        elif which == "topbit0":
            out.append(1 << 63 if j == 0 else 0)
        elif which == "unit":
            out.append(1)
        elif which == "tie":
            out.append(4 if j == ncols - 1 else 5)
        elif which == "mixed":
            s, r = 0x0123456789ABCDEF, (8 * j) & 63
            out.append(((s << r) | (s >> (64 - r))) & U64 if r else s)
        elif which == "asym":
            out.append(0xF0F0F0F0F0F0F0F0 >> j)
        else:
            out.append(1 if j == ncols - 1 else 0)
    return out


def _pack_py(ords):
    """The interleave, in Python: ord[0].bit63, ord[1].bit63, ..., MSB first."""
    v = 0
    for r in range(63, -1, -1):
        for o in ords:
            v = (v << 1) | ((o >> r) & 1)
    return v.to_bytes(8 * len(ords), "big")


def _unpack_py(key, n):
    """The transpose a full-width key carries: the interleave run backwards."""
    v, X = int.from_bytes(key, "big"), [0] * n
    for t in range(64 * n):
        X[t % n] |= ((v >> (64 * n - 1 - t)) & 1) << (63 - t // n)
    return X


def _untranspose(X, n, mutate=False):
    """Skilling's TransposetoAxes at b = 64, transcribed from the published listing,
    not derived from the code under test. `mutate` drops the Gray-decode fold into
    X[0]: the negative control, under which every round trip must break."""
    X = list(X)
    t = X[n - 1] >> 1
    for i in range(n - 1, 0, -1):
        X[i] ^= X[i - 1]
    if not mutate:
        X[0] ^= t
    Q = 2
    while Q != (1 << 64):
        P = Q - 1
        for i in range(n - 1, -1, -1):
            if X[i] & Q:
                X[0] ^= P
            else:
                t = (X[0] ^ X[i]) & P
                X[0] ^= t
                X[i] ^= t
        Q <<= 1
    return X


# ---- the checkers, shared by the controls and the code under test ------------

def _props(idx, keys, ncols, b, want=(True, True, True)):
    """-> (c1, c2, c3) violation counts over the whole point space of one case.

    idx[p] is the index point p's key carries; keys[p] is the key itself, sorted with
    bytes comparison (memcmp) for C2. C1 counts out-of-range indices, repeats and
    unhit indices together. C2 walks memcmp order and looks at the POINT
    COORDINATES, which the sort cannot manufacture. C3: see the module docstring."""
    N = 1 << (ncols * b)
    c1 = c2 = c3 = None
    if want[0]:
        cnt = collections.Counter(idx)
        inrange = sum(1 for k in cnt if k < N)
        c1 = sum(c for k, c in cnt.items() if k >= N) + (sum(c for k, c in cnt.items() if k < N) - inrange) \
            + (N - inrange)
    mask = (1 << b) - 1
    if want[1]:
        order = sorted(range(N), key=keys.__getitem__)
        bad = 0
        for a, z in zip(order, order[1:]):
            d = z - a if z > a else a - z
            lo = a if a < z else z
            s = d.bit_length() - 1
            # unit-adjacent: exactly one column moved by one, with no carry out of it
            if d != (1 << s) or s % b or ((lo >> s) & mask) == mask:
                bad += 1
        c2 = bad
    if want[2]:
        perm = c1 == 0 if c1 is not None else len(set(idx)) == N and max(idx) < N
        if perm:
            # one run per sub-cube at every level, counted from the levels each
            # consecutive pair shares
            byidx = [0] * N
            for p, i in enumerate(idx):
                byidx[i] = p
            shared = collections.Counter()
            for a, z in zip(byidx, byidx[1:]):
                x, lv = a ^ z, b
                for j in range(ncols):
                    f = (x >> (j * b)) & mask
                    if f:
                        lv = min(lv, b - f.bit_length())
                shared[lv] += 1
            c3, below = 0, 0
            for L in range(1, b + 1):
                below += shared[L - 1]
                c3 += (1 + below) - (1 << (ncols * L))
        else:
            # the shell's own form: max - min + 1 == count, per sub-cube
            c3 = 0
            for L in range(1, b + 1):
                mn, mx, ct = {}, {}, collections.Counter()
                for p, i in enumerate(idx):
                    k = 0
                    for j in range(ncols):
                        k = (k << L) | (((p >> ((ncols - 1 - j) * b)) & mask) >> (b - L))
                    mn[k] = min(mn.get(k, i), i)
                    mx[k] = max(mx.get(k, i), i)
                    ct[k] += 1
                c3 += sum(1 for k in ct if mx[k] - mn[k] + 1 != ct[k])
    return c1, c2, c3


def _coords(p, ncols, b):
    mask = (1 << b) - 1
    return [(p >> ((ncols - 1 - j) * b)) & mask for j in range(ncols)]


# ---- the controls: two wrong curves and a truncating one ---------------------

def _serpentine(c, b):
    M, idx, rev = (1 << b) - 1, 0, 0
    for x in c:
        v = M - x if rev else x
        idx = (idx << b) | v
        rev = v & 1
    return idx


def _zorder(c, b):
    idx = 0
    for r in range(b - 1, -1, -1):
        for x in c:
            idx = (idx << 1) | ((x >> r) & 1)
    return idx


def _controls():
    """-> every reading the INSTRUMENT arms take, by the shell's arithmetic."""
    r = collections.Counter()
    calls = cases = points = 0
    for ncols, b in CASES:
        N, nb = 1 << (ncols * b), ncols * b
        cases += 1
        pts = [_coords(p, ncols, b) for p in range(N)]
        points += len(pts)
        serp = [_serpentine(c, b) for c in pts]
        zord = [_zorder(c, b) for c in pts]
        trunc = [i & ~1 for i in serp]
        calls += len(serp) + len(zord) + len(trunc)
        # the key an index-producing encoder packs to, MSB first, zeros below
        s1, s2, s3 = _props(serp, serp, ncols, b)
        z1, z2, z3 = _props(zord, zord, ncols, b)
        t1, _, _ = _props(trunc, trunc, ncols, b, want=(True, False, False))
        r["serp_c1"] += s1
        r["serp_c2"] += s2
        r["zord_c1"] += z1
        r["zord_c3"] += z3
        r["trunc_cases"] += 1
        r["trunc_failed"] += t1 > 0
        if ncols >= 2 and b >= 2:
            r["serp_nested"] += 1
            r["serp_nested_failed"] += s3 > 0
        if ncols >= 2:
            r["zord_multi"] += 1
            r["zord_multi_failed"] += z2 > 0
        del nb
    zall = "".join(_pack_py(_vec(v, n)).hex() for n, v in GOLDEN)
    return {
        "INSTRUMENT C0 premise: the control battery encoded every case and every point":
            f"{cases} cases, {points} points",
        "INSTRUMENT C0 premise: the control encoders were called once per point": calls,
        "INSTRUMENT the control's own pack reproduces the frozen Z-order bytes": zall,
        "INSTRUMENT C1 control: the serpentine is a permutation of the index range": r["serp_c1"],
        "INSTRUMENT C2 control: the serpentine PASSES unit-step adjacency": r["serp_c2"],
        "INSTRUMENT C3 control: the serpentine FAILS dyadic contiguity in every nested case":
            f"{r['serp_nested_failed']} of {r['serp_nested']}",
        "INSTRUMENT C1 control: Z-order is a permutation of the index range": r["zord_c1"],
        "INSTRUMENT C2 control: Z-order FAILS unit-step adjacency in every multi-column case":
            f"{r['zord_multi_failed']} of {r['zord_multi']}",
        "INSTRUMENT C3 control: Z-order PASSES dyadic contiguity": r["zord_c3"],
        "INSTRUMENT C1 control: a truncating encoder FAILS the permutation test in every case":
            f"{r['trunc_failed']} of {r['trunc_cases']}",
    }


# ---- building and calling the code under test ---------------------------------

def _compiler(work):
    """-> (cc, None) for a compiler that builds and runs a two-line program, or
    (None, why)."""
    cc = os.environ.get("CC") or next((c for c in ("gcc", "cc", "clang") if shutil.which(c)), None)
    if not cc:
        return None, "no C compiler"
    (work / "smoke.c").write_text("int main(void){return 0;}\n")
    try:
        ok = subprocess.run([cc, "-o", str(work / "smoke"), str(work / "smoke.c")],
                            capture_output=True).returncode == 0 and \
            subprocess.run([str(work / "smoke")], capture_output=True).returncode == 0
    except OSError:
        ok = False
    return (cc, None) if ok else (None, f"CC={cc} cannot build C")


def _build(cc, work):
    """-> the loaded library, or a query_error() saying why there is none."""
    missing = [p.relative_to(ROOT).as_posix() for p in (CURVE_C, CURVE_H) if not p.is_file()]
    if missing:
        return query_error(f"the code under test is absent: {' '.join(missing)}")
    (work / "postgres.h").write_text(STUB)
    for h in ("c.h", "fmgr.h", "miscadmin.h", "postgres_ext.h", "utils/elog.h", "columnar.h",
              "columnar_compat.h"):
        (work / h).parent.mkdir(parents=True, exist_ok=True)
        (work / h).write_text('#include "postgres.h"\n')
    shutil.copy(CURVE_C, work)
    shutil.copy(CURVE_H, work)
    (work / "driver.c").write_text(DRIVER)
    so = work / "libhc.so"
    r = subprocess.run([cc, "-O2", "-Wall", "-Werror=incompatible-pointer-types", "-shared", "-fPIC",
                        f"-I{work}", "-o", str(so), str(work / "driver.c"), str(work / "columnar_curve.c")],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return query_error(f"the battery would not build: {(r.stderr or r.stdout).strip()[:300]}")
    try:
        lib = ctypes.CDLL(str(so))
    except OSError as exc:
        return query_error(f"the battery would not load: {exc}")
    lib.hc_calls.restype = ctypes.c_long
    lib.hc_hilbert.argtypes = [ctypes.c_void_p, ctypes.c_long, ctypes.c_int, ctypes.c_void_p]
    lib.hc_pack.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    return lib


def _hilbert_keys(lib, ords, ncols):
    """ords: a flat list of npoints*ncols ordinals -> the keys, one bytes per point."""
    n = len(ords) // ncols
    a = array.array("Q", ords)
    out = ctypes.create_string_buffer(n * ncols * 8 + GUARD)
    lib.hc_hilbert(a.buffer_info()[0], n, ncols, out)
    w, raw = ncols * 8, out.raw
    return [raw[i:i + w] for i in range(0, n * w, w)]


def _pack(lib, ords, size=None, fill=0xa5):
    """The pack alone, into a buffer of `size` bytes (default: the key) filled with
    `fill`, plus GUARD bytes of slack. -> the `size` bytes."""
    size = size or len(ords) * 8
    a = array.array("Q", ords)
    out = ctypes.create_string_buffer(bytes([fill]) * (size + GUARD), size + GUARD)
    lib.hc_pack(a.buffer_info()[0], len(ords), out)
    return out.raw[:size]


def _battery(lib):
    """-> every reading the battery arms take, by the shell's names."""
    r = {}
    rnd = random.Random(889)

    # C1, C2, C3 over every case, every point
    cases = points = c1 = c2 = c3 = 0
    base = {}
    for ncols, b in CASES:
        N, nb = 1 << (ncols * b), ncols * b
        ords = []
        for p in range(N):
            ords.extend(c << (64 - b) for c in _coords(p, ncols, b))
        keys = _hilbert_keys(lib, ords, ncols)
        cases += 1
        points += len(keys)
        idx = [int.from_bytes(k, "big") >> (64 * ncols - nb) for k in keys]
        a1, a2, a3 = _props(idx, keys, ncols, b)
        c1, c2, c3 = c1 + a1, c2 + a2, c3 + a3
        if ncols >= 2:
            base[(ncols, b)] = (ords, idx)
    r["C0 premise: the battery encoded every case and every point"] = f"{cases} cases, {points} points"
    r["C0 premise: the encoder was called once per point"] = lib.hc_calls()
    r["C1 the Hilbert index set is exactly the contiguous range"] = c1
    r["C2 consecutive points in memcmp key order are unit-adjacent"] = c2
    r["C3 every dyadic sub-cube occupies a contiguous run of indices"] = c3

    # C4a: the same points with the low 64-b bits filled with noise
    mism = permbad = cases = points = 0
    for (ncols, b), (ords, idx) in base.items():
        N, nb, low = 1 << (ncols * b), ncols * b, (1 << (64 - b)) - 1
        noisy = [o | (rnd.getrandbits(64) & low) for o in ords]
        keys = _hilbert_keys(lib, noisy, ncols)
        cases += 1
        points += len(keys)
        ixf = [int.from_bytes(k, "big") >> (64 * ncols - nb) for k in keys]
        mism += sum(1 for x, y in zip(idx, ixf) if x != y)
        permbad += _props(ixf, None, ncols, b, want=(True, False, False))[0]
    r["C4a premise: the bridge covered every case and every point"] = f"{cases} cases, {points} points"
    r["C4a the full-width key prefix does not move when the low bits change"] = mism
    r["C4a the full-width key prefixes are exactly the index range"] = permbad

    # C4b: 3,000 random full-width keys per ncols, through the published inverse
    distinct = trips = tripbad = mutbad = adj = adjre = adjbad = 0
    for ncols in NCOLS:
        w = ncols * 8
        ks = [rnd.randbytes(w) for _ in range(3000)]
        distinct += len(set(ks))
        for key in ks:
            T = _unpack_py(key, ncols)
            A0 = _untranspose(T, ncols)
            trips += 1
            ok0 = _hilbert_keys(lib, A0, ncols)[0] == key
            tripbad += not ok0
            mutbad += _hilbert_keys(lib, _untranspose(T, ncols, mutate=True), ncols)[0] != key
            v = int.from_bytes(key, "big") + 1
            if v >> (8 * w):
                continue  # wrapped past the last index: not a pair
            key2 = v.to_bytes(w, "big")
            A1 = _untranspose(_unpack_py(key2, ncols), ncols)
            ok1 = _hilbert_keys(lib, A1, ncols)[0] == key2
            adj += 1
            if not (ok0 and ok1):
                continue
            adjre += 1
            diffs = [abs(x - y) for x, y in zip(A0, A1) if x != y]
            adjbad += not (len(diffs) == 1 and diffs[0] == 1)
    r["C4b premise: 3,000 DISTINCT full-width keys per ncols were round-tripped"] = distinct
    r["C4b the published inverse round-trips full-width keys"] = tripbad
    r["C4b control: a mutated inverse breaks every round trip the clean one made"] = \
        f"clean {tripbad} bad, mutated {mutbad} of {trips}"
    r["C4b premise: consecutive key pairs were formed"] = adj
    r["C4b consecutive keys decode to unit-adjacent points, re-encoded by the code under test"] = \
        f"{adjbad} bad over {adjre} re-encoded of {adj} pairs"

    # C5: goldens, the pack's bit order, its extent and its content
    for n, v in GOLDEN:
        r[f"golden {n} {v}"] = _hilbert_keys(lib, _vec(v, n), n)[0].hex()
    r["C5 the pack bit order: pack(2^63, 0) starts 0x80"] = _pack(lib, [1 << 63, 0]).hex()
    r["C5 the pack bit order: pack(0, 2^63) starts 0x40"] = _pack(lib, [0, 1 << 63]).hex()
    for n in NCOLS:
        a = _pack(lib, _vec("mixed", n), 200, 0x00)
        z = _pack(lib, _vec("mixed", n), 200, 0xff)
        r[f"len {n}"] = next((i for i in range(200) if a[i] != z[i]), 200)
        r[f"tail {n}"] = "intact" if a[8 * n:] == bytes(200 - 8 * n) and z[8 * n:] == b"\xff" * (200 - 8 * n) \
            else "clobbered"
        o = _pack(lib, _vec("max", n), 200, 0x00)
        nff = next((i for i in range(200) if o[i] != 0xff), 200)
        r[f"ones {n}"] = f"{nff} of {8 * n} ff, tail {'intact' if o[8 * n:] == bytes(200 - 8 * n) else 'clobbered'}"

    # C6: ncols == 1 is the identity
    for nm, x in (("0", 0), ("1", 1), ("2^63", 1 << 63), ("UINT64_MAX", U64)):
        r[f"C6 ncols == 1 is the big-endian ordinal: {nm}"] = _hilbert_keys(lib, [x], 1)[0].hex()
    xs = [rnd.getrandbits(64) for _ in range(200000)]
    kh = _hilbert_keys(lib, xs, 1)
    r["C6 premise: the comparison loop ran 200,000 times"] = len(kh)
    r["C6 ncols == 1 is the big-endian ordinal over 200,000 random values"] = \
        sum(1 for x, k in zip(xs, kh) if k != x.to_bytes(8, "big"))
    r["C6 ncols == 1 agrees with Z-order over 200,000 random values"] = \
        sum(1 for x, k in zip(xs, kh) if k != _pack(lib, [x]))

    # C7: the Z-order pack
    for n, v in GOLDEN:
        r[f"zorder {n} {v}"] = _pack(lib, _vec(v, n)).hex()
    return r


@pytest.fixture(scope="module")
def hc(tmp_path_factory):
    work = tmp_path_factory.mktemp("hilbert")
    cc, why = _compiler(work)
    if cc is None:
        return {"why": why}
    lib = _build(cc, work)
    out = {"why": None, "controls": _controls()}
    if isinstance(lib, str):
        out["failed"] = lib
    else:
        out["battery"] = _battery(lib)
    return out


def _control(hc, expect, name):
    """-> the instrument reading, or None after declining (no compiler, as the shell)."""
    if hc["why"]:
        expect.cannot_run("MISSING_DEPENDENCY", hc["why"], name=name)
        return None
    return hc["controls"][name]


def _reading(hc, expect, name, key=None):
    """-> the battery reading under `key` (default: the name), a query_error() when the
    battery could not run, or None after declining for want of a compiler."""
    if hc["why"]:
        expect.cannot_run("MISSING_DEPENDENCY", hc["why"], name=name)
        return None
    if "failed" in hc:
        return hc["failed"]
    return hc["battery"][key or name]


def _skip(v):
    return v is None


# ---- the instrument -------------------------------------------------------------

def test_hilbert_instrument_control_battery_covered_every_point(hc, expect):
    v = _control(hc, expect, "INSTRUMENT C0 premise: the control battery encoded every case and every point")
    if not _skip(v):
        expect.text(v, "52 cases, 6344330 points",
                    "INSTRUMENT C0 premise: the control battery encoded every case and every point")


def test_hilbert_instrument_control_encoders_called_once_per_point(hc, expect):
    v = _control(hc, expect, "INSTRUMENT C0 premise: the control encoders were called once per point")
    if not _skip(v):
        expect.num(v, 19032990, "INSTRUMENT C0 premise: the control encoders were called once per point")


def test_hilbert_instrument_control_pack_is_the_frozen_zorder(hc, expect):
    v = _control(hc, expect, "INSTRUMENT the control's own pack reproduces the frozen Z-order bytes")
    if not _skip(v):
        expect.text(v, "".join(ZORDER[k] for k in GOLDEN),
                    "INSTRUMENT the control's own pack reproduces the frozen Z-order bytes")


def test_hilbert_instrument_serpentine_is_a_permutation(hc, expect):
    v = _control(hc, expect, "INSTRUMENT C1 control: the serpentine is a permutation of the index range")
    if not _skip(v):
        expect.num(v, 0, "INSTRUMENT C1 control: the serpentine is a permutation of the index range")


def test_hilbert_instrument_serpentine_passes_adjacency(hc, expect):
    v = _control(hc, expect, "INSTRUMENT C2 control: the serpentine PASSES unit-step adjacency")
    if not _skip(v):
        expect.num(v, 0, "INSTRUMENT C2 control: the serpentine PASSES unit-step adjacency")


def test_hilbert_instrument_serpentine_fails_contiguity(hc, expect):
    v = _control(hc, expect, "INSTRUMENT C3 control: the serpentine FAILS dyadic contiguity in every nested case")
    if not _skip(v):
        expect.text(v, "25 of 25",
                    "INSTRUMENT C3 control: the serpentine FAILS dyadic contiguity in every nested case")


def test_hilbert_instrument_zorder_is_a_permutation(hc, expect):
    v = _control(hc, expect, "INSTRUMENT C1 control: Z-order is a permutation of the index range")
    if not _skip(v):
        expect.num(v, 0, "INSTRUMENT C1 control: Z-order is a permutation of the index range")


def test_hilbert_instrument_zorder_fails_adjacency(hc, expect):
    v = _control(hc, expect, "INSTRUMENT C2 control: Z-order FAILS unit-step adjacency in every multi-column case")
    if not _skip(v):
        expect.text(v, "32 of 32",
                    "INSTRUMENT C2 control: Z-order FAILS unit-step adjacency in every multi-column case")


def test_hilbert_instrument_zorder_passes_contiguity(hc, expect):
    v = _control(hc, expect, "INSTRUMENT C3 control: Z-order PASSES dyadic contiguity")
    if not _skip(v):
        expect.num(v, 0, "INSTRUMENT C3 control: Z-order PASSES dyadic contiguity")


def test_hilbert_instrument_truncation_fails_permutation(hc, expect):
    v = _control(hc, expect, "INSTRUMENT C1 control: a truncating encoder FAILS the permutation test in every case")
    if not _skip(v):
        expect.text(v, "52 of 52",
                    "INSTRUMENT C1 control: a truncating encoder FAILS the permutation test in every case")


# ---- C0 to C4 -------------------------------------------------------------------

def test_hilbert_battery_covered_every_point(hc, expect):
    v = _reading(hc, expect, "C0 premise: the battery encoded every case and every point")
    if not _skip(v):
        expect.text(v, "52 cases, 6344330 points", "C0 premise: the battery encoded every case and every point")


def test_hilbert_encoder_called_once_per_point(hc, expect):
    v = _reading(hc, expect, "C0 premise: the encoder was called once per point")
    if not _skip(v):
        expect.num(v, 6344330, "C0 premise: the encoder was called once per point")


def test_hilbert_index_set_is_the_contiguous_range(hc, expect):
    v = _reading(hc, expect, "C1 the Hilbert index set is exactly the contiguous range")
    if not _skip(v):
        expect.num(v, 0, "C1 the Hilbert index set is exactly the contiguous range")


def test_hilbert_memcmp_order_is_unit_adjacent(hc, expect):
    v = _reading(hc, expect, "C2 consecutive points in memcmp key order are unit-adjacent")
    if not _skip(v):
        expect.num(v, 0, "C2 consecutive points in memcmp key order are unit-adjacent")


def test_hilbert_dyadic_subcubes_are_contiguous(hc, expect):
    v = _reading(hc, expect, "C3 every dyadic sub-cube occupies a contiguous run of indices")
    if not _skip(v):
        expect.num(v, 0, "C3 every dyadic sub-cube occupies a contiguous run of indices")


def test_hilbert_bridge_covered_every_point(hc, expect):
    v = _reading(hc, expect, "C4a premise: the bridge covered every case and every point")
    if not _skip(v):
        expect.text(v, "32 cases, 4247180 points", "C4a premise: the bridge covered every case and every point")


def test_hilbert_full_width_prefix_ignores_low_bits(hc, expect):
    v = _reading(hc, expect, "C4a the full-width key prefix does not move when the low bits change")
    if not _skip(v):
        expect.num(v, 0, "C4a the full-width key prefix does not move when the low bits change")


def test_hilbert_full_width_prefixes_are_the_index_range(hc, expect):
    v = _reading(hc, expect, "C4a the full-width key prefixes are exactly the index range")
    if not _skip(v):
        expect.num(v, 0, "C4a the full-width key prefixes are exactly the index range")


def test_hilbert_distinct_full_width_keys_round_tripped(hc, expect):
    v = _reading(hc, expect, "C4b premise: 3,000 DISTINCT full-width keys per ncols were round-tripped")
    if not _skip(v):
        expect.num(v, 24000, "C4b premise: 3,000 DISTINCT full-width keys per ncols were round-tripped")


def test_hilbert_published_inverse_round_trips(hc, expect):
    v = _reading(hc, expect, "C4b the published inverse round-trips full-width keys")
    if not _skip(v):
        expect.num(v, 0, "C4b the published inverse round-trips full-width keys")


def test_hilbert_mutated_inverse_breaks_every_round_trip(hc, expect):
    v = _reading(hc, expect, "C4b control: a mutated inverse breaks every round trip the clean one made")
    if not _skip(v):
        expect.text(v, "clean 0 bad, mutated 24000 of 24000",
                    "C4b control: a mutated inverse breaks every round trip the clean one made")


def test_hilbert_consecutive_key_pairs_were_formed(hc, expect):
    v = _reading(hc, expect, "C4b premise: consecutive key pairs were formed")
    if not _skip(v):
        expect.num(v, 24000, "C4b premise: consecutive key pairs were formed")


def test_hilbert_consecutive_keys_decode_to_neighbours(hc, expect):
    v = _reading(hc, expect, "C4b consecutive keys decode to unit-adjacent points, re-encoded by the code under test")
    if not _skip(v):
        expect.text(v, "0 bad over 24000 re-encoded of 24000 pairs",
                    "C4b consecutive keys decode to unit-adjacent points, re-encoded by the code under test")


# ---- C5: goldens, bit order, extent and content ----------------------------------

@pytest.mark.parametrize("n,v", GOLDEN)
def test_hilbert_golden_key(hc, expect, n, v):
    got = _reading(hc, expect, f"C5 golden Hilbert keys: ncols={n} {v}", f"golden {n} {v}")
    if not _skip(got):
        expect.text(got, HILBERT[(n, v)], f"C5 golden Hilbert keys: ncols={n} {v}")


def test_hilbert_pack_bit_order_column_zero(hc, expect):
    v = _reading(hc, expect, "C5 the pack bit order: pack(2^63, 0) starts 0x80")
    if not _skip(v):
        expect.text(v, "80000000000000000000000000000000", "C5 the pack bit order: pack(2^63, 0) starts 0x80")


def test_hilbert_pack_bit_order_column_one(hc, expect):
    v = _reading(hc, expect, "C5 the pack bit order: pack(0, 2^63) starts 0x40")
    if not _skip(v):
        expect.text(v, "40000000000000000000000000000000", "C5 the pack bit order: pack(0, 2^63) starts 0x40")


@pytest.mark.parametrize("n", NCOLS)
def test_hilbert_pack_extent(hc, expect, n):
    v = _reading(hc, expect, f"C5 the pack establishes exactly 8*ncols bytes: ncols={n}", f"len {n}")
    if not _skip(v):
        expect.num(v, 8 * n, f"C5 the pack establishes exactly 8*ncols bytes: ncols={n}")


@pytest.mark.parametrize("n", NCOLS)
def test_hilbert_pack_tail_untouched(hc, expect, n):
    v = _reading(hc, expect, f"C5 the pack wrote nothing past the key: ncols={n}", f"tail {n}")
    if not _skip(v):
        expect.text(v, "intact", f"C5 the pack wrote nothing past the key: ncols={n}")


@pytest.mark.parametrize("n", NCOLS)
def test_hilbert_pack_sets_every_bit_of_all_ones(hc, expect, n):
    v = _reading(hc, expect, f"C5 the pack sets every bit of the key for an all-ones vector: ncols={n}",
                 f"ones {n}")
    if not _skip(v):
        expect.text(v, f"{8 * n} of {8 * n} ff, tail intact",
                    f"C5 the pack sets every bit of the key for an all-ones vector: ncols={n}")


# ---- C6: ncols == 1 ---------------------------------------------------------------

def test_hilbert_one_column_zero(hc, expect):
    v = _reading(hc, expect, "C6 ncols == 1 is the big-endian ordinal: 0")
    if not _skip(v):
        expect.text(v, "0000000000000000", "C6 ncols == 1 is the big-endian ordinal: 0")


def test_hilbert_one_column_one(hc, expect):
    v = _reading(hc, expect, "C6 ncols == 1 is the big-endian ordinal: 1")
    if not _skip(v):
        expect.text(v, "0000000000000001", "C6 ncols == 1 is the big-endian ordinal: 1")


def test_hilbert_one_column_top_bit(hc, expect):
    v = _reading(hc, expect, "C6 ncols == 1 is the big-endian ordinal: 2^63")
    if not _skip(v):
        expect.text(v, "8000000000000000", "C6 ncols == 1 is the big-endian ordinal: 2^63")


def test_hilbert_one_column_all_ones(hc, expect):
    v = _reading(hc, expect, "C6 ncols == 1 is the big-endian ordinal: UINT64_MAX")
    if not _skip(v):
        expect.text(v, "ffffffffffffffff", "C6 ncols == 1 is the big-endian ordinal: UINT64_MAX")


def test_hilbert_one_column_loop_ran(hc, expect):
    v = _reading(hc, expect, "C6 premise: the comparison loop ran 200,000 times")
    if not _skip(v):
        expect.num(v, 200000, "C6 premise: the comparison loop ran 200,000 times")


def test_hilbert_one_column_is_big_endian_over_random_values(hc, expect):
    v = _reading(hc, expect, "C6 ncols == 1 is the big-endian ordinal over 200,000 random values")
    if not _skip(v):
        expect.num(v, 0, "C6 ncols == 1 is the big-endian ordinal over 200,000 random values")


def test_hilbert_one_column_agrees_with_zorder(hc, expect):
    v = _reading(hc, expect, "C6 ncols == 1 agrees with Z-order over 200,000 random values")
    if not _skip(v):
        expect.num(v, 0, "C6 ncols == 1 agrees with Z-order over 200,000 random values")


# ---- C7: the Z-order pack ---------------------------------------------------------

@pytest.mark.parametrize("n,v", GOLDEN)
def test_hilbert_zorder_pack_unchanged(hc, expect, n, v):
    got = _reading(hc, expect, f"C7 the Z-order pack is byte-for-byte what it was: ncols={n} {v}",
                   f"zorder {n} {v}")
    if not _skip(got):
        expect.text(got, ZORDER[(n, v)], f"C7 the Z-order pack is byte-for-byte what it was: ncols={n} {v}")
