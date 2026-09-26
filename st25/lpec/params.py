"""LPEC parameter decoding: LSPs, pitch parameters, and the coefficient block.

See docs/lpec.md, "Parameter decoding" and "Coefficient block". This module
turns the raw fields of a parsed frame (st25.lpec.bitstream) into physical
quantities:

- the two parallel LSP representations of an LSP set (double, and the Q16
  integer copy that only feeds the spectral envelope);
- pitch lag and taps (double and Q15);
- the M spectral coefficients of one coefficient block: gains, the integer
  spectral envelope, per-band ranking, and filling from the VQ streams,
  sign bits and noise.

The per-band bit allocation itself is computed while parsing
(bitstream.bit_allocation), because the field widths depend on it; this
module consumes ``CoefficientBlock.alloc`` as given.

Every real is a Python float (IEEE binary64); every expression keeps the
exact operation order docs/lpec.md writes. Integer parts use the helpers
below for int16 stores, 32-bit wrap-around and C division.
"""

from __future__ import annotations

from typing import List, Sequence, Tuple

# --------------------------------------------------------------------------
# Configuration constants (docs/lpec.md, "Configuration constants")
# --------------------------------------------------------------------------

BANDS = 8

# type t -> transform length N
TRANSFORM_N = {0: 512, 1: 768, 2: 768, 3: 1024}
# type t -> band width W, first coded coefficient s, end of coded region e
BAND_WIDTH = {0: 28, 1: 42, 2: 42, 3: 56}
FIRST_CODED = {0: 2, 1: 3, 2: 3, 3: 4}
END_CODED = {0: 226, 1: 339, 2: 339, 3: 452}

Q15 = 3.0517578125e-05  # 2^-15

# --------------------------------------------------------------------------
# Integer helpers (docs/lpec.md, "Conventions")
# --------------------------------------------------------------------------


def s16(x: int) -> int:
    """The low 16 bits of x as a signed value (a store to an int16)."""
    x &= 0xFFFF
    return x - 0x10000 if x & 0x8000 else x


def i32(x: int) -> int:
    """32-bit two's-complement wrap-around."""
    x &= 0xFFFFFFFF
    return x - 0x100000000 if x & 0x80000000 else x


def u32(x: int) -> int:
    """The 32-bit pattern of x, as unsigned (for logical shifts >>>)."""
    return x & 0xFFFFFFFF


def cdiv(a: int, b: int) -> int:
    """C division: rounds toward zero."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


# --------------------------------------------------------------------------
# LSP decoding (docs/lpec.md, "LSP decoding (two parallel representations)")
# --------------------------------------------------------------------------


def _sort_pass(v: List) -> None:
    """One sorting pass, j = 1..10, one step back only (not a full sort)."""
    for j in range(1, 11):
        if v[j] < v[j - 1]:
            v[j], v[j - 1] = v[j - 1], v[j]
        if j >= 2 and v[j - 2] > v[j - 1]:
            v[j - 2], v[j - 1] = v[j - 1], v[j - 2]


def lsp_double(tables, i1: int, i2: int, i3: int) -> List[float]:
    """The double LSP vector l[0..10] of an LSP set."""
    c1, c2, c3 = tables.C1[i1], tables.C2[i2], tables.C3[i3]
    l = [0.0] * 11
    for j in range(10):
        l[j + 1] = (c3[j] + c2[j]) + c1[j]

    lim = 0.49
    for j in range(10, 5, -1):
        if l[j] >= 0.5:
            l[j] = lim
        lim = l[j] - 0.01

    lim = 0.01
    for j in range(1, 6):
        if l[j] < 0.0:
            l[j] = lim
        lim = l[j] + 0.01

    l[0] = 0.0
    _sort_pass(l)

    for j in range(1, 11):
        if l[j] - l[j - 1] < 0.01:
            if j == 1:
                l[1] = 0.01
            else:
                s = l[j - 1] + l[j]
                l[j] = (0.01 + s) * 0.5
                l[j - 1] = (s - 0.01) * 0.5

    l[0] = 0.0
    return l


def lsp_q16(tables, i1: int, i2: int, i3: int) -> List[int]:
    """The Q16 LSP vector q[0..10] of an LSP set (envelope path only)."""
    d1, d2, d3 = tables.D1[i1], tables.D2[i2], tables.D3[i3]
    q = [0] * 11
    for j in range(10):
        q[j + 1] = s16((d3[j] + 2 * d2[j] + 4 * d1[j]) >> 2)

    lim = 32112
    for j in range(10, 5, -1):
        if q[j] < 0:
            q[j] = s16(lim)
        lim = q[j] - 655

    lim = 655
    for j in range(1, 6):
        if q[j] < 0:
            q[j] = s16(lim)
        lim = q[j] + 655

    q[0] = 0
    _sort_pass(q)

    if q[1] < 655:
        q[1] = 655

    for j in range(2, 11):
        a, b = q[j], q[j - 1]
        if a - b < 655:
            q[j] = s16(cdiv(b + 656 + a, 2))
            q[j - 1] = s16(cdiv(a + b - 654, 2))

    q[0] = 0
    return q


def interpolate_lsp_double(l0: Sequence[float], l2: Sequence[float]) -> List[float]:
    """Interpolated slot 1: l1[j] = (l2[j] + l0[j])*0.5."""
    return [(l2[j] + l0[j]) * 0.5 for j in range(11)]


def interpolate_lsp_q16(q0: Sequence[int], q2: Sequence[int]) -> List[int]:
    """Interpolated slot 1: q1[j] = s16((q0[j] + q2[j]) div 2)."""
    return [s16(cdiv(q0[j] + q2[j], 2)) for j in range(11)]


# --------------------------------------------------------------------------
# Pitch parameters (docs/lpec.md, "Pitch parameters")
# --------------------------------------------------------------------------


def pitch(tables, lag: int, pgidx) -> Tuple[int, List[float], List[int]]:
    """(lag, double taps [b0, b1, b2], Q15 taps) of one pitch field."""
    if lag == 0:
        return 0, [0.0, 0.0, 0.0], [0, 0, 0]
    return lag, list(tables.PT[pgidx]), list(tables.PQ[pgidx])


# --------------------------------------------------------------------------
# Spectral envelope (docs/lpec.md, "Spectral envelope (integer)")
# --------------------------------------------------------------------------


def _mul(x: int, c: int) -> int:
    lo = x & 0xFFFF
    hi = x >> 16
    return i32(2 * (((lo * c) >> 15) + 2 * hi * c))


def _lpc_q(tables, q: Sequence[int]) -> List[int]:
    """Step 1: Q16 LSPs to the normalised int16 LPC coefficients a16[0..10]."""
    S = tables.S2048
    c = [0] * 11
    for j in range(11):
        e = i32(q[j] << 11)
        f = (e & 0xFFFF) >> 1
        i = e >> 16
        u = (i + 512) % 2048
        c[j] = s16(((32768 - f) * S[u] + S[u + 1] * f) >> 15)

    Q = [0] * 11
    P = [0] * 11
    Q[0] = -(1 << 23)
    Q[1] = 1 << 23
    P[0] = -(1 << 23)
    P[1] = -(1 << 23)
    TQ = [0] * 11
    TP = [0] * 11
    for i in range(1, 6):
        a = c[2 * i]
        b = c[2 * i - 1]
        TQ[1] = _mul(Q[0], a)
        TP[1] = _mul(P[0], b)
        for m in range(2, i + 1):
            TQ[m] = i32(_mul(Q[m - 1], a) - Q[m - 2])
            TP[m] = i32(_mul(P[m - 1], b) - P[m - 2])
        for m in range(1, i + 1):
            Q[m] = i32(Q[m] - TQ[m])
            Q[2 * i + 1 - m] = i32(-Q[m])
            P[m] = i32(P[m] - TP[m])
            P[2 * i + 1 - m] = P[m]

    R = [i32(-(P[j] + Q[j])) for j in range(11)]

    A = 0
    for r in R:
        A |= abs(r)
    d = 0
    while d < 16 and not (u32(A << d) & 0x40000000):
        d += 1

    rnd = (1 << (15 - d)) if d <= 15 else 0
    return [s16((R[j] + rnd) >> (16 - d)) for j in range(11)]


def _autocorrelation(a16: Sequence[int]) -> Tuple[int, int, List[int]]:
    """Step 2: (E0, E1, r[0..10]) with the split accumulator."""
    lo = 0
    hi = 0
    for i in range(11):
        sq = a16[i] * a16[i]
        lo += sq & 0x7FFF
        hi += sq >> 15
    l = lo & 0x7FFF
    h = hi + (u32(lo) >> 15)

    s = 0
    ah = abs(h)
    for n in range(20):
        if (ah << n) & (1 << 18):
            s = 19 - n
            break

    x = s - 10
    if x > 0:
        l = l | ((h & ((1 << x) - 1)) << 15)
        if s - 14 > 0:
            l = u32(l) >> (s - 14)
        h = h >> x
    elif x < 0:
        h = (h << -x) | (l >> (s + 5))
        l = l & ((1 << (s + 5)) - 1)

    E0 = h + 2
    E1 = l

    r = [0] * 11
    mask = (1 << s) - 1
    for m in range(1, 11):
        slo = 0
        shi = 0
        for i in range(11 - m):
            p = a16[i] * a16[i + m]
            slo += p & mask
            shi += p >> s
        r[m] = s16(shi + (u32(slo) >> s))
    return E0, E1, r


def envelope(tables, q: Sequence[int], lag: int, pg: float, N: int) -> List[int]:
    """Step 3: the integer power spectrum w[0..M-1] (small = important)."""
    M = N // 2
    a16 = _lpc_q(tables, q)
    E0, E1, r = _autocorrelation(a16)

    if N % 3 == 0:
        table, step = tables.S1536, 1536 // N
    else:
        table, step = tables.S2048, 2048 // N

    def T(p):
        return table[step * (p % N)]

    quarter = N // 4
    if lag != 0:
        gq = s16(int(32768 * pg))
    w = [0] * M
    for k in range(M):
        accH = E0
        accL = E1
        for m in range(1, 11):
            v = r[m] * T(quarter + m * k)
            accL += v & 0x7FFFF
            accH += v >> 19
        if lag == 0:
            w[k] = i32((accH << 13) + (u32(accL) >> 6))
        else:
            c = T(quarter + lag * k)
            f = (gq * ((gq - 2 * c) >> 2) + (1 << 28)) >> 15
            part1 = u32(((accL & 0x7FFFF) >> 4) * f) >> 14
            part2 = i32(i32((accH + (u32(accL) >> 19)) * f) * 2)
            w[k] = i32(part1 + part2)
    return w


# --------------------------------------------------------------------------
# Coefficient block (docs/lpec.md, "Gains", "Ranking", "Filling the
# coefficients")
# --------------------------------------------------------------------------


class NoisePosition:
    """The persistent noise position z (0..1023): carries over bands,
    blocks and frames."""

    __slots__ = ("z",)

    def __init__(self, z: int = 0):
        self.z = z


class _VQStream:
    """One VQ class's stream of vector components, running on across bands."""

    __slots__ = ("codebook", "indices", "dim", "vec", "pos")

    def __init__(self, codebook, indices, dim):
        self.codebook = codebook
        self.indices = indices
        self.dim = dim
        self.vec = 0
        self.pos = 0

    def next(self) -> float:
        if self.pos == self.dim:
            self.vec += 1
            self.pos = 0
        value = self.codebook[self.indices[self.vec]][self.pos]
        self.pos += 1
        return value


def coefficients(tables, block, q: Sequence[int], lag: int, pq: Sequence[int],
                 noise: NoisePosition) -> List[float]:
    """The M spectral coefficients X[0..M-1] of one coefficient block.

    ``q``, ``lag`` and ``pq`` are the Q16 LSPs, lag and Q15 taps of the
    block's parameter slot k. ``noise`` is advanced in place.
    """
    t = block.type
    N = TRANSFORM_N[t]
    M = N // 2
    W = BAND_WIDTH[t]
    s = FIRST_CODED[t]
    e = END_CODED[t]

    G = tables.GAIN[block.global_gain]
    bg1 = tables.BG1[block.band_gain1]
    bg2 = tables.BG2[block.band_gain2]
    bg = []
    for b in range(BANDS):
        v = bg2[b] + bg1[b]
        if v < 1e-05:
            v = 1e-05
        bg.append(v)
    pg = float(pq[1]) * Q15

    w = envelope(tables, q, lag, pg, N)

    alloc = block.alloc
    vq2 = _VQStream(tables.VQ2, block.vq2, 2)
    vq4 = _VQStream(tables.VQ4, block.vq4, 4)
    vq8 = _VQStream(tables.VQ8, block.vq8, 8)
    signs = iter(block.signs)
    NA, NB = tables.NA, tables.NB
    z = noise.z

    X = [0.0] * M
    for b in range(BANDS):
        base = s + b * W
        order = sorted(((w[base + j] & ~0x7F) | j, base + j) for j in range(W))
        positions = [x for _key, x in order]
        n4, n2, n1, ns = alloc.n4[b], alloc.n2[b], alloc.n1[b], alloc.ns[b]
        idx = 0
        for _ in range(n4):
            X[positions[idx]] = vq2.next()
            idx += 1
        for _ in range(n2):
            X[positions[idx]] = vq4.next()
            idx += 1
        for _ in range(n1):
            X[positions[idx]] = vq8.next()
            idx += 1
        for _ in range(ns):
            X[positions[idx]] = -0.6 if next(signs) else 0.6
            idx += 1
        while idx < W:
            X[positions[idx]] = ((NB[z] * 0.9) * pg) + ((NA[z] * (1.0 - pg)) * 0.9)
            z = (z + 1) % 1024
            idx += 1
    noise.z = z

    for i in range(s):
        X[i] = 0.0
    for i in range(e, M):
        X[i] = 0.0

    for b in range(BANDS):
        base = s + b * W
        g = bg[b]
        for x in range(base, base + W):
            X[x] = (X[x] * g) * G
    return X
