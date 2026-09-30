"""LPEC synthesis: inverse transform, overlap-add, long-term (pitch)
predictor, LPC synthesis and output rounding.

See docs/lpec.md, "Synthesis". Every real is a Python float (IEEE
binary64) and every expression keeps the exact operation order the doc
gives -- the FFT in particular is written out butterfly by butterfly, not
delegated to numpy, because the order of its additions decides the last
bit of the result.
"""

from __future__ import annotations

import math
from typing import List, Sequence

from . import x87

PI_C = 3.14159265359  # the DLL's per-frame truncated pi (docs/lpec.md)


# --------------------------------------------------------------------------
# FFT (docs/lpec.md, "FFT, size n = 256 or 512 (radix 2)" and "size 384";
# LPEC SP adds sizes 1024 and 768, the same two algorithms)
# --------------------------------------------------------------------------


def _fft_radix2(re: List[float], im: List[float], n: int, c: float, S: Sequence[float]) -> None:
    """In-place radix-2 FFT of size n (positive exponent), last stage
    scaled by c, followed by the bit-reversal permutation."""
    size = n
    stride = 2048 // n
    while size > 4:
        half = size // 2
        for k in range(half):
            ws = S[k * stride]
            wc = S[k * stride + 512]
            for a in range(k, n, size):
                b = a + half
                re_a, re_b, im_a, im_b = re[a], re[b], im[a], im[b]
                re[a] = re_a + re_b
                im[a] = im_b + im_a
                dr = re_a - re_b
                di = im_a - im_b
                re[b] = (dr * wc) - (di * ws)
                im[b] = (di * wc) + (dr * ws)
        stride *= 2
        size = half

    for g in range(0, n, 4):
        r0, r1, r2, r3 = re[g], re[g + 1], re[g + 2], re[g + 3]
        i0, i1, i2, i3 = im[g], im[g + 1], im[g + 2], im[g + 3]
        re[g] = ((r3 + r1) + (r2 + r0)) * c
        re[g + 1] = ((r2 + r0) - (r3 + r1)) * c
        re[g + 2] = ((r0 - r2) - (i1 - i3)) * c
        re[g + 3] = ((i1 - i3) + (r0 - r2)) * c
        im[g] = ((i1 + i3) + (i0 + i2)) * c
        im[g + 1] = ((i0 + i2) - (i1 + i3)) * c
        im[g + 2] = ((i0 - i2) + (r1 - r3)) * c
        im[g + 3] = ((i0 - i2) - (r1 - r3)) * c

    j = 0
    for i in range(n - 1):
        if i < j:
            re[i], re[j] = re[j], re[i]
            im[i], im[j] = im[j], im[i]
        k = n // 2
        while k <= j:
            j -= k
            k //= 2
        j += k


_S3 = math.sqrt(3.0) * 0.5


def _fft384(re: List[float], im: List[float], S: Sequence[float], U: Sequence[float]) -> None:
    """In-place FFT of size 384 (see _fft_radix3)."""
    _fft_radix3(re, im, 384, S, U)


def _fft_radix3(re: List[float], im: List[float], n: int, S: Sequence[float],
                U: Sequence[float]) -> None:
    """In-place FFT of size n = 3m (384 or 768): one radix-3 stage (twiddle
    step 1536/n in the period-1536 table), three radix-2 FFTs of m (scaled by
    1/n), then the interleaving reorder."""
    m = n // 3
    step = 1536 // n
    s3 = _S3
    for j in range(m):
        x0, x1, x2 = re[j], re[j + m], re[j + 2 * m]
        y0, y1, y2 = im[j], im[j + m], im[j + 2 * m]
        k = step * j
        re[j] = x0 + (x2 + x1)
        im[j] = y0 + (y2 + y1)
        alpha = x0 - (0.5 * (x2 + x1))
        beta = ((y2 + y1) * 0.5) + (-y0)
        gamma = (x1 - x2) * s3
        delta = (y1 - y2) * s3
        u1 = alpha - delta
        v1 = beta - gamma
        u2 = delta + alpha
        v2 = gamma + beta
        re[j + m] = (U[k] * v1) + (U[k + 384] * u1)
        im[j + m] = (u1 * U[k]) - (v1 * U[k + 384])
        re[j + 2 * m] = (U[2 * k] * v2) + (U[2 * k + 384] * u2)
        im[j + 2 * m] = (u2 * U[2 * k]) - (v2 * U[2 * k + 384])

    c = 1.0 / n
    thirds = []
    for off in (0, m, 2 * m):
        tr = re[off:off + m]
        ti = im[off:off + m]
        _fft_radix2(tr, ti, m, c, S)
        thirds.append((tr, ti))
    for j in range(m):
        for part in range(3):
            re[3 * j + part] = thirds[part][0][j]
            im[3 * j + part] = thirds[part][1][j]


# --------------------------------------------------------------------------
# Inverse transform (docs/lpec.md, "Inverse transform")
# --------------------------------------------------------------------------


def inverse_transform(tables, X: Sequence[float], N: int, overlap: int) -> List[float]:
    """N outputs Y[0..N-1] from the M = N/2 coefficients X, overlap Λ.
    N is one of the configuration's transform lengths (LP 512 / 768 / 1024,
    SP 1024 / 1536 / 2048)."""
    h = N // 2
    q = N // 4
    if N % 3 == 0:
        T, sigma = tables.FFT_SIN_1536, 1536 // N
    else:
        T, sigma = tables.FFT_SIN_2048, 2048 // N

    re = [0.0] * h
    im = [0.0] * h
    for i in range(q):
        x = X[2 * i]
        re[i] = x * T[(q - i) * sigma]
        im[i] = x * T[i * sigma]
    for i in range(q, h):
        x = X[N - 1 - 2 * i]
        re[i] = x * T[(i - q) * sigma]
        im[i] = x * T[(N - i) * sigma]

    if h % 3 == 0:
        _fft_radix3(re, im, h, tables.FFT_SIN_2048, tables.FFT_SIN_1536)
    else:
        _fft_radix2(re, im, h, 1.0 / h, tables.FFT_SIN_2048)

    w = tables.POST[tables.config.transform_n.index(N)]
    Y = [0.0] * N
    half_lambda = overlap // 2
    top = N - half_lambda - 1
    for i in range(h):
        Y[top - i] = (((-re[i]) * w[h - 1 - i]) + (im[i] * w[i])) * 2.0
    for j in range(half_lambda):
        Y[N - half_lambda + j] = Y[N - half_lambda - 1 - j]
    for j in range((N - overlap) // 2):
        Y[j] = -Y[N - overlap - 1 - j]
    return Y


# --------------------------------------------------------------------------
# Overlap-add (docs/lpec.md, "Overlap-add")
# --------------------------------------------------------------------------


def overlap_add_plain(O: List[float], Y: Sequence[float], length: int, carry: int,
                      v: Sequence[float]) -> List[float]:
    """out[i] = (O[i]*v[l-1-i]) + (Y[i]*v[i]), i < l; then O[0..carry-1] =
    Y[l..l+carry-1]. Updates O in place and returns out."""
    out = [(O[i] * v[length - 1 - i]) + (Y[i] * v[i]) for i in range(length)]
    O[0:carry] = Y[length:length + carry]
    return out


def overlap_add_shaped(O: List[float], Y: Sequence[float], fp: int, Ap: Sequence[float],
                       fc: int, Ac: Sequence[float], v: Sequence[float],
                       v2: Sequence[float]) -> List[float]:
    """The mode-0 half overlap-add with temporal shaping (F/2 outputs: 256
    for LP, 512 for SP; Y and the windows have F points)."""
    half = len(Y) // 2
    part = half // 4
    if fp == 0 and fc == 0:
        out = [(O[j] * v[half + j]) + (v[j] * Y[j]) for j in range(half)]
    else:
        out = [0.0] * half
        for r in range(4):
            ap = Ap[7 - r]
            ac = Ac[3 - r]
            rho1 = ap / Ap[4 + r]
            rho2 = ac / Ac[r]
            for j in range(part * r, part * r + part):
                out[j] = ((O[j] * (ap * v[half + j])) + ((ac * v[j]) * Y[j])) / (
                    (v2[half + j] * rho1) + (v2[j] * rho2))
    O[0:half] = Y[half:2 * half]
    return out


# --------------------------------------------------------------------------
# Long-term (pitch) predictor (docs/lpec.md, "Long-term (pitch) predictor")
# --------------------------------------------------------------------------


def taps_stable(lag: int, taps: Sequence[float]) -> bool:
    """The DLL's stability check of a 3-tap pitch filter at this lag."""
    b0, b1, b2 = taps
    c = [0.0] * (lag + 2)
    c[0] = 1.0
    c[lag - 1] = -b0
    c[lag] = -b1
    c[lag + 1] = -b2
    r = [math.floor((cj * 0.125) * 32768.0) * 3.0517578125e-05 for cj in c]
    for m in range(lag + 1, 0, -1):
        rm = r[m]
        if abs(rm) > 0.1225:
            return False
        kappa = rm * 8.0
        denom = 1.0 - (kappa * kappa)
        r = [
            math.floor(((r[j] - ((rm * r[m - j]) * 8.0)) / denom) * 32768.0) * 3.0517578125e-05
            for j in range(m + 1)
        ]
    return True


def pitch_section(H: List[float], x: Sequence[float], start: int, length: int,
                  lag: int, taps: Sequence[float]) -> None:
    """Run one section of the pitch predictor over H[start..start+length-1]
    (H indices; the excitation sample for H[n] is x[n - 256])."""
    if lag == 0:
        for n in range(start, start + length):
            H[n] = x[n - 256]
        return
    b0, b1, b2 = taps
    if not taps_stable(lag, taps):
        b0, b2 = 0.0, 0.0
    for n in range(start, start + length):
        H[n] = (((b2 * H[n - lag - 1]) + x[n - 256]) + (b1 * H[n - lag])) + (H[n - lag + 1] * b0)


# --------------------------------------------------------------------------
# LPC synthesis (docs/lpec.md, "LPC synthesis")
# --------------------------------------------------------------------------


def lsp_to_lpc(l: Sequence[float]) -> List[float]:
    """a[0..order] from the double LSPs l[0..order] (fcos is the x87
    emulation)."""
    n = len(l)
    order = n - 1
    C = [0.0] * (n + 1)
    for j in range(1, n + 1):
        C[j] = x87.fcos((l[j - 1] * PI_C) * 2.0)
    P = [0.0] * n
    Q = [0.0] * n
    P[0] = -1.0
    P[1] = -1.0
    Q[0] = -1.0
    Q[1] = 1.0
    TQ = [0.0] * n
    TP = [0.0] * n
    for i in range(2, order + 1, 2):
        u = C[i]
        v = C[i + 1]
        TQ[1] = (v * 2.0) * Q[0]
        TP[1] = (u * 2.0) * P[0]
        for m in range(2, i // 2 + 1):
            TQ[m] = ((Q[m - 1] * v) * 2.0) - Q[m - 2]
            TP[m] = ((P[m - 1] * u) * 2.0) - P[m - 2]
        for m in range(1, i // 2 + 1):
            Q[m] = Q[m] - TQ[m]
            Q[i + 1 - m] = -Q[m]
            P[m] = P[m] - TP[m]
            P[i + 1 - m] = P[m]
    return [(Q[m] + P[m]) * (-0.5) for m in range(n)]


def interpolate_lsp(lA: Sequence[float], lB: Sequence[float], t: float) -> List[float]:
    """l[j] = (lA[j]*(1.0 - t)) + (lB[j]*t)."""
    return [(lA[j] * (1.0 - t)) + (lB[j] * t) for j in range(len(lA))]


def lpc_filter(e: Sequence[float], start: int, length: int, a: Sequence[float],
               y: List[float]) -> None:
    """All-pole filter 1/A(z) over e[start..start+length-1], appending the
    outputs to y (whose last ``order`` entries are the filter memory)."""
    if len(a) != 11:
        order = len(a) - 1
        for n in range(start, start + length):
            k = len(y)
            v = e[n]
            for m in range(1, order + 1):
                v = v - (y[k - m] * a[m])
            y.append(v)
        return
    a1, a2, a3, a4, a5, a6, a7, a8, a9, a10 = a[1:11]
    for n in range(start, start + length):
        k = len(y)
        v = e[n]
        v = v - (y[k - 1] * a1)
        v = v - (y[k - 2] * a2)
        v = v - (y[k - 3] * a3)
        v = v - (y[k - 4] * a4)
        v = v - (y[k - 5] * a5)
        v = v - (y[k - 6] * a6)
        v = v - (y[k - 7] * a7)
        v = v - (y[k - 8] * a8)
        v = v - (y[k - 9] * a9)
        v = v - (y[k - 10] * a10)
        y.append(v)


# --------------------------------------------------------------------------
# Output (docs/lpec.md, "Output")
# --------------------------------------------------------------------------


def to_int16(y: float) -> int:
    """floor(y + 0.5), clamped to the int16 range."""
    v = math.floor(y + 0.5)
    if v > 32767:
        return 32767
    if v < -32768:
        return -32768
    return v
