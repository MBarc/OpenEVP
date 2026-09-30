"""LPEC ST synthesis: dequantisation, power compensation, IMDCT, gain
compensation, tones, noise and the 16-band synthesis filter bank.

Arithmetic follows the DLL operation by operation. Two kinds of floating
point occur and both are reproduced exactly:

- x87 code, run with precision control 53 bits: every operation is a
  Python float (IEEE double) operation, and every store to a float
  variable is ``f32()``. Values the DLL keeps on the FPU stack stay doubles.
- SSE code (packed single precision): every lane operation is rounded to
  float32 on its own, i.e. ``f32(a op b)`` with a, b already float32 (a
  double operation followed by one rounding is exact for + - * of floats).

Operand order is kept where it can matter (it cannot for a single + or *,
which are commutative in IEEE arithmetic; it does for chains).
"""

from __future__ import annotations

from array import array

from .bitstream import GainBand, ToneBand

_A = array("f", (0.0,))


def f32(x: float) -> float:
    """Round a double to float32 (overflow gives inf, like the FPU store)."""
    _A[0] = x
    return _A[0]


def f32v(values) -> list:
    return array("f", values).tolist()


_r = f32v


# ---------------------------------------------------------------------------
# State carried from frame to frame, per channel
# ---------------------------------------------------------------------------


class ChannelState:
    def __init__(self):
        self.ovl = [[0.0] * 128 for _ in range(16)]    # IMDCT overlap per subband
        self.x_last = [0.0] * 16                        # filter bank: last butterfly output
        self.b_hist = [[[0.0, 0.0] for _ in range(8)] for _ in range(11)]  # and B of the last 2 samples
        self.gain = [GainBand() for _ in range(16)]     # previous frame's gain data
        self.wnd = [0] * 16                             # previous frame's window shapes
        self.tones = [ToneBand() for _ in range(16)]    # previous frame's tone bands


class ToneInfo:
    """The unit-level tone fields a frame's synthesis reads back one frame later."""

    def __init__(self, present=0, amp_mode=0, negate=None):
        self.present = present
        self.amp_mode = amp_mode
        self.negate = negate or [0] * 16


# ---------------------------------------------------------------------------
# Dequantisation and power compensation (FUN_10007a40)
# ---------------------------------------------------------------------------


def dequantize(u, prev_gain, t):
    """The two spectra (2048 float32 each) of a parsed unit. ``prev_gain``
    gives each channel's previous-frame gain bands."""
    ch0, ch1 = u.ch
    n = u.ncoded_qu
    QS, QL = t.QU_START, t.QU_LEN
    # noise start positions per subband: sum of all scale factors
    acc = 0
    for c in u.ch:
        for i in range(n):
            acc = (acc + c.sf[i]) & 0xFFFF
    npos = []
    acc &= 0x3FC
    for sb in range(u.ncoded_sb):
        npos.append(acc)
        acc = (acc + 0x80) & 0x3FC
    # channel 1 units left empty but coded in channel 0 (and not flagged) copy channel 0
    for qu in range(n):
        if ch1.wl[qu] == 0 and ch0.wl[qu] > 0 and ch1.ct[qu] == 0:
            a = QS[qu]
            ch1.spec[a:a + QL[qu]] = ch0.spec[a:a + QL[qu]]
            ch1.wl[qu] = ch0.wl[qu]
    SF, MANT = t.F_SF, t.F_WL_MANT
    out = []
    for ci, c in enumerate(u.ch):
        spec = [0.0] * 2048
        for qu in range(n):
            wl = c.wl[qu]
            if wl > 0:
                scale = f32(SF[c.sf[qu]] * MANT[wl])
                a = QS[qu]
                q = c.spec
                spec[a:a + QL[qu]] = f32v([q[i] * scale for i in range(a, a + QL[qu])])
        for sb in range(u.ncoded_sb):
            _power_comp(u, ci, spec, npos[sb], sb, prev_gain, t)
        out.append(spec)
    s0, s1 = out
    for sb in range(u.ncoded_sb):
        if u.swap[sb] == 1:
            a = sb * 128
            s0[a:a + 128], s1[a:a + 128] = s1[a:a + 128], s0[a:a + 128]
        if u.negate[sb] == 1:
            for qu in range(t.SB_QU[sb], t.SB_QU[sb + 1]):
                for i in range(QS[qu], QS[qu + 1]):
                    s1[i] = -s1[i]
    if u.mute == 1:
        out = [[0.0] * 2048, [0.0] * 2048]
    return out


def _gain_shift(prev: GainBand, cur: GainBand, GE) -> int:
    """FUN_1000d570: headroom (in powers of two) the gain curve needs."""
    base = -GE[cur.lev[0]] if cur.npoints >= 1 else 0
    m = 0
    for i in range(prev.npoints):
        v = _s8(base - GE[prev.lev[i]])
        if v > m:
            m = v
    for i in range(cur.npoints):
        v = _s8(-GE[cur.lev[i]])
        if v > m:
            m = v
    return m


def _s8(v):
    v &= 0xFF
    return v - 256 if v & 0x80 else v


def _power_comp(u, ci, spec, pos, sb, prev_gain, t):
    """FUN_1000d5f0: add scaled noise to the coded units of a subband."""
    src = ci
    if u.swap[sb] != 0:
        src = 1 - ci
    sc = u.ch[src]
    level = sc.power[t.SB_POWGRP[sb]]
    pwr = t.F_PWR_LEVELS[level]
    if not pwr > t.C_ZERO:
        return
    NOISE, Q15 = t.NOISE, t.C_Q15
    tmp = [f32(NOISE[(pos + i) & 0x3FF] * Q15) for i in range(128)]
    m = _gain_shift(prev_gain[src][sb], sc.gain[sb], t.GAIN_EXP)
    pw = f32(pwr / (1 << m))
    c = u.ch[ci]
    QS = t.QU_START
    SF, MANT = t.F_SF, t.F_WL_MANT
    for qu in range(t.PWR_SB_QU[sb], t.SB_QU[sb + 1]):
        wl = c.wl[qu] & 0xFFFF
        if wl >= 0x8000:
            wl -= 0x10000
        if wl <= 0:
            continue
        sf = c.sf[qu] & 0xFFFF
        if sf >= 0x8000:
            sf -= 0x10000
        scale = f32(((SF[sf] * MANT[wl]) * pw) / (1 << wl))
        a, b = QS[qu], QS[qu + 1]
        for j in range(b - a):
            spec[a + j] = f32(scale * tmp[j] + spec[a + j])


# ---------------------------------------------------------------------------
# IMDCT (FUN_1000c4d0): 128 coefficients -> 256 windowed samples
# ---------------------------------------------------------------------------


def _imdct_plan(t):
    """Per FFT stage, the butterflies (a, b, cos, sin): the DLL's radix-2
    decimation-in-frequency passes over 64 complex values, twiddles taken
    from the table ends (see docs/lpec-st.md)."""
    plan = getattr(t, "_imdct_plan", None)
    if plan is None:
        C, S = t.F_IMDCT_C, t.F_IMDCT_S
        plan = []
        tw = 63
        for st in range(5, -1, -1):
            half = 1 << st
            span = 1 << (st + 1)
            stage = []
            a, b = 0, span
            for g in range(128 // (4 * half)):
                for j in range(half):
                    stage.append((a, b, C[tw - half + j], S[tw - half + j]))
                    a += 2
                    b += 2
                a += span
                b += span
            plan.append(stage)
            tw -= half
        t._imdct_plan = plan
    return plan


def imdct(X, W, rev, t):
    """128 coefficients -> 256 windowed samples. Each pass's results are
    independent of each other, so a pass is computed as one list and
    rounded to float32 at once (the DLL stores every value as a float)."""
    C, S, R = t.F_IMDCT_C, t.F_IMDCT_S, t.IMDCT_ORDER
    Z = [0.0] * 128
    for k in range(64):
        if rev:
            u, v = X[127 - 2 * k], X[2 * k]
        else:
            u, v = X[2 * k], X[127 - 2 * k]
        c, s = C[63 + k], S[63 + k]
        Z[2 * k] = u * c + v * s
        Z[2 * k + 1] = u * s - v * c
    Z = _r(Z)
    for stage in _imdct_plan(t):
        N = [0.0] * 128
        for a, b, c, s in stage:
            za, za1, zb, zb1 = Z[a], Z[a + 1], Z[b], Z[b + 1]
            t2 = za - zb
            t3 = za1 - zb1
            N[a] = zb + za
            N[a + 1] = zb1 + za1
            N[b] = t2 * c + t3 * s
            N[b + 1] = t2 * s - t3 * c
        Z = _r(N)
    Y = [0.0] * 256
    for k in range(64):
        Y[k] = Z[R[k + 64]] * W[k]
        Y[192 + k] = -(Z[R[k]] * W[192 + k])
    for j in range(128):
        Y[64 + j] = -(Z[R[127 - j]] * W[64 + j])
    return _r(Y)


# ---------------------------------------------------------------------------
# Gain compensation curve (FUN_1000c2a0)
# ---------------------------------------------------------------------------


def _pow2neg(e):
    """2**-e as the DLL builds it: (1 << -e) for e <= 0, else 1.0 / (1 << e)."""
    if -e >= 0:
        return float(1 << (-e & 31))
    return 1.0 / (1 << (e & 31))


def gain_curve(prev: GainBand, cur: GainBand, t):
    """256 gain factors and the index of the first level change (0xFF if none)."""
    GE = t.GAIN_EXP
    STEP = t.F_GAIN_STEP
    E = [0] * 64
    fill = 0
    for p in range(cur.npoints):
        end = cur.loc[p] + 0x20
        val = GE[cur.lev[p]]
        if fill <= end:
            for i in range(fill, end + 1):
                E[i] = val
            fill = end + 1
    k = 0
    for p in range(prev.npoints):
        end = prev.loc[p]
        val = GE[prev.lev[p]]
        while k <= end:
            E[k] += val
            k += 1
    gc = [0.0] * 256
    level = 0
    st = _pow2neg(E[63])
    e = 0xFF
    first = 0x100
    for idx in range(63, -1, -1):
        v = E[idx]
        if v == level:
            fs = f32(st)
            gc[e] = fs
            gc[e - 1] = fs
            gc[e - 2] = fs
            e -= 3
        else:
            if first == 0x100:
                first = e
            if v < level:
                d = (level - v - 1) * 3
                st = _pow2neg(v)
                gc[e] = f32(st * STEP[d])
                gc[e - 1] = f32(st * STEP[d + 1])
                gc[e - 2] = f32(st * STEP[d + 2])
            else:
                d = (v - level - 1) * 3
                st = _pow2neg(level)
                gc[e] = f32(st * STEP[d + 2])
                gc[e - 1] = f32(st * STEP[d + 1])
                gc[e - 2] = f32(st * STEP[d])
                st = _pow2neg(v)
            e -= 3
            level = v
        gc[e] = f32(st)
        e -= 1
    return gc, (0xFF if first == 0x100 else first)


# ---------------------------------------------------------------------------
# Tones (FUN_1000cf80 and callees)
# ---------------------------------------------------------------------------


def _tone_window_params(cur: ToneBand, prev: ToneBand):
    """FUN_10007980: where the current band's tones start and stop."""
    if cur.has_start and cur.start_pos < cur.stop_pos:
        cur.w_start_on, cur.w_start = 1, cur.start_pos * 4 + 0x80
    elif prev.has_start:
        cur.w_start_on, cur.w_start = 1, prev.start_pos * 4
    else:
        cur.w_start, cur.w_start_on = 0, 0
    if prev.has_stop and cur.w_start <= prev.stop_pos * 4:
        cur.w_stop, cur.w_stop_on = prev.stop_pos * 4, 1
    elif cur.has_stop:
        cur.w_stop_on, cur.w_stop = 1, cur.stop_pos * 4 + 0x80
    else:
        cur.w_stop, cur.w_stop_on = 0x100, 0
    cur.w_stop += 4
    if cur.w_stop > 0x100:
        cur.w_stop = 0x100


def _tone_env(band: ToneBand, t):
    """FUN_1000d3b0: the band's 256-sample on/off envelope."""
    w = [1.0] * 256
    R = t.F_TONE_RAMP
    if band.w_start_on:
        s = band.w_start
        for i in range(s):
            w[i] = 0.0
        w[s:s + 4] = R[0:4]
    if band.w_stop_on:
        e = band.w_stop
        w[e - 4] = R[3]
        w[e - 3] = R[2]
        w[e - 2] = R[1]
        w[e - 1] = R[0]
        for i in range(e, 256):
            w[i] = 0.0
    return w


def _tone_gen(band: ToneBand, start, amp_mode, negate, ci, t):
    """FUN_1000d080: the band's waves over 128 samples from ``start``."""
    buf = [0.0] * 128
    SINE = t.TONE_SINE
    for w in band.waves[:band.nwavs]:
        a = t.F_AMP_SF[w.amp_sf]
        if amp_mode == 0:
            a = f32(a * t.F_AMP_IDX[w.amp_idx])
        step = w.freq
        pos = ((start - 0x80) * step + ((w.phase & 0x1F) << 6)) & 0x7FF
        buf = _r([a * SINE[(pos + i * step) & 0x7FF] + b for i, b in enumerate(buf)])
    if ci == 1 and negate != 0:
        m1 = t.C_MINUS_ONE
        buf = _r([v * m1 for v in buf])
    env = _tone_env(band, t)
    return _r([v * e for v, e in zip(buf, env[start:start + 128])])


def tones(sbs, ci, prev_bands, cur_bands, prevT: ToneInfo, curT: ToneInfo, t):
    if not prevT.present and not curT.present:
        return
    WIN = t.TONE_WINDOW
    for b in range(16):
        pb, cb = prev_bands[b], cur_bands[b]
        _tone_window_params(cb, pb)
        if cb.nwavs == 0 and pb.nwavs == 0:
            continue
        A = _tone_gen(pb, 0x80, prevT.amp_mode, prevT.negate[b], ci, t)
        B = _tone_gen(cb, 0, curT.amp_mode, curT.negate[b], ci, t)
        done = False
        if pb.nwavs > 0:
            if cb.nwavs > 0 and pb.w_stop - 0x80 >= cb.w_start:
                A = _r([v * w for v, w in zip(A, WIN[128:256])])
                B = _r([v * w for v, w in zip(B, WIN[0:128])])
                done = True
            elif pb.w_stop_on == 0:
                A = _r([v * w for v, w in zip(A, WIN[128:256])])
        if not done and cb.nwavs > 0 and cb.w_start_on == 0:
            B = _r([v * w for v, w in zip(B, WIN[0:128])])
        AB = _r([x + y for x, y in zip(A, B)])
        sbs[b] = _r([x + y for x, y in zip(sbs[b], AB)])


# ---------------------------------------------------------------------------
# Per-channel synthesis (FUN_10007400 / FUN_100075a0)
# ---------------------------------------------------------------------------


def synthesize(state: ChannelState, c, u, spec, prevT, curT, t):
    """One channel of a frame: returns 2048 float32 samples and advances
    ``state`` (its previous-frame fields become this frame's)."""
    nsb = u.nsb
    cur_wnd = [0] * 16
    for sb in range(nsb):
        cur_wnd[sb] = c.wnd[sb]
    sbs = []
    WINS = t.F_WINDOWS
    for sb in range(nsb):
        pw, cw = state.wnd[sb], cur_wnd[sb]
        if pw == 0:
            W = WINS[0] if cw == 0 else WINS[1]
        else:
            W = WINS[2] if cw == 0 else WINS[3]
        Y = imdct(spec[sb * 128:(sb + 1) * 128], W, t.SB_REVERSE[sb], t)
        ovl = state.ovl[sb]
        pg, cg = state.gain[sb], c.gain[sb]
        if pg.npoints == 0 and cg.npoints == 0:
            out = _r([a + b for a, b in zip(ovl, Y)])
        else:
            gc, first = gain_curve(pg, cg, t)
            if first >= 128:
                Y[128:first + 1] = _r([g * y for g, y in zip(gc[128:first + 1], Y[128:first + 1])])
            p = _r([y * g for y, g in zip(Y, gc[:128])])
            out = _r([a + b for a, b in zip(ovl, p)])
        state.ovl[sb] = Y[128:256]
        sbs.append(out)
    for sb in range(nsb, 16):
        sbs.append([0.0] * 128)
        state.ovl[sb] = [0.0] * 128
    # tones
    tones(sbs, c.idx, state.tones, c.tones, prevT, curT, t)
    # time-domain noise
    if u.noise_present:
        scale = (1 << u.noise_level) * t.C_Q15
        NOISE = t.NOISE
        for sb in range(16):
            ofs = t.NOISE_OFS[u.noise_counter]
            u.noise_counter += 1
            sbs[sb] = _r([NOISE[ofs + i] * scale + v for i, v in enumerate(sbs[sb])])
    pcm = qmf(sbs, state, t)
    state.gain = c.gain
    state.wnd = cur_wnd
    state.tones = c.tones
    return pcm


# ---------------------------------------------------------------------------
# 16-band synthesis filter bank (FUN_1000c740)
# ---------------------------------------------------------------------------
#
# The DLL runs this per output sample n (16 PCM samples each) over a
# 3-phase circular buffer. Resolving its buffer addressing gives an explicit
# recurrence over per-sample vectors (m = 0..7, i = 0..9):
#
#   X_n       = butterfly(f32(sb_k[n] * PRE[k]), k = 0..15)        16 values
#   A_n[-1,m] = f32(f32(Kx[m] * X_n[8+m]) + f32(Ky[m] * X_n-1[7-m]))
#   B_n[-1,m] = f32(f32(Ky[7-m] * X_n[15-m]) - f32(Kx[7-m] * X_n-1[m]))
#   A_n[i,m]  = f32(A_n[i-1,m] + f32(Q[8i+m] * B_n-2[i-1,7-m]))
#   B_n[i,m]  = f32(f32(Q[8i+7-m] * A_n[i-1,7-m]) - B_n-2[i-1,m])
#   pcm[16n+m]   = f32(A_n[9,m] + f32(Qef[m] * B_n-2[9,7-m]))
#   pcm[16n+8+m] = f32(f32(Qef[7-m] * A_n[9,7-m]) - B_n-2[9,m])
#
# (Kx = window[0:8], Ky = window[0x60:0x68], Q = window[0x68:0xB8],
# Qef = window[0xB8:0xC0].) Nothing at sample n depends on the same stage
# of an earlier sample, so each stage is computed for all 128 samples at
# once; the state carried between frames is X of the last sample and B of
# the last two samples at every stage.


def qmf(sbs, state: ChannelState, t):
    PRE = t.F_QMF_PRE
    Wt = t.F_QMF_WIN
    Kx, Ky = Wt[0:8], Wt[0x60:0x68]
    Q = Wt[0x68:0xB8]
    Qef = Wt[0xB8:0xC0]
    x = [_r([v * PRE[k] for v in sbs[k]]) for k in range(16)]
    X = _qmf_butterfly_v(*x, _r, t.F_QMF_DCT, t.C_HALF)
    Xp = state.x_last
    Xm1 = [[Xp[k]] + X[k][:127] for k in range(16)]
    hist = state.b_hist                  # [stage][m] -> [B_-2, B_-1]
    new_hist = []
    A = []
    Bf = []
    for m in range(8):
        pa = _r([Kx[m] * v for v in X[8 + m]])
        pb = _r([Ky[m] * v for v in Xm1[7 - m]])
        A.append(_r([a + b for a, b in zip(pa, pb)]))
        pa = _r([Ky[7 - m] * v for v in X[15 - m]])
        pb = _r([Kx[7 - m] * v for v in Xm1[m]])
        Bf.append(hist[0][m] + _r([a - b for a, b in zip(pa, pb)]))
    new_hist.append([b[128:] for b in Bf])
    for i in range(10):
        q = 8 * i
        An = []
        Bn = []
        for m in range(8):
            p = _r([Q[q + m] * v for v in Bf[7 - m][:128]])
            An.append(_r([a + b for a, b in zip(A[m], p)]))
            p = _r([Q[q + 7 - m] * v for v in A[7 - m]])
            Bn.append(hist[i + 1][m] + _r([a - b for a, b in zip(p, Bf[m])]))
        A, Bf = An, Bn
        new_hist.append([b[128:] for b in Bf])
    cols = []
    for m in range(8):
        p = _r([Qef[m] * v for v in Bf[7 - m][:128]])
        cols.append(_r([a + b for a, b in zip(A[m], p)]))
    for m in range(8):
        p = _r([Qef[7 - m] * v for v in A[7 - m]])
        cols.append(_r([a - b for a, b in zip(p, Bf[m])]))
    state.x_last = [X[k][127] for k in range(16)]
    state.b_hist = new_hist
    out = []
    for row in zip(*cols):
        out.extend(row)
    return out


def _qmf_butterfly_v(x0, x1, x2, x3, x4, x5, x6, x7, x8, x9, x10, x11, x12, x13, x14, x15, R, K, H):
    K0, K1, K2, K3, K4, K5, K6, K7, K8, K9, K10, K11, K12, K13, K16, K17, K18 = K[0], K[1], K[2], K[3], K[4], K[5], K[6], K[7], K[8], K[9], K[10], K[11], K[12], K[13], K[16], K[17], K[18]
    v1 = R([p + q for p, q in zip(x7, x8)])
    v2 = R([p + q for p, q in zip(x6, x9)])
    v3 = R([p + q for p, q in zip(x5, x10)])
    v4 = R([p + q for p, q in zip(x4, x11)])
    v5 = R([p + q for p, q in zip(x0, x15)])
    v6 = R([p + q for p, q in zip(x1, x14)])
    v7 = R([p + q for p, q in zip(x2, x13)])
    v8 = R([p + q for p, q in zip(x3, x12)])
    v9 = R([p - q for p, q in zip(v5, v1)])
    v10 = R([p - q for p, q in zip(v6, v2)])
    v11 = R([p - q for p, q in zip(v7, v3)])
    v12 = R([p - q for p, q in zip(v8, v4)])
    v13 = R([p + q for p, q in zip(v5, v1)])
    v14 = R([p + q for p, q in zip(v6, v2)])
    v15 = R([p + q for p, q in zip(v7, v3)])
    v16 = R([p + q for p, q in zip(v8, v4)])
    d17 = [p + q for p, q in zip(v16, v15)]
    d18 = [p + q for p, q in zip(d17, v14)]
    v19 = R([K8 * q for q in v9])
    v20 = R([K9 * q for q in v10])
    v21 = R([K10 * q for q in v11])
    v22 = R([K11 * q for q in v12])
    v23 = R([p - q for p, q in zip(x7, x8)])
    v24 = R([p - q for p, q in zip(x6, x9)])
    v25 = R([p - q for p, q in zip(x5, x10)])
    v26 = R([p - q for p, q in zip(x4, x11)])
    d27 = [p + q for p, q in zip(d18, v13)]
    v28 = R([p - q for p, q in zip(x0, x15)])
    v29 = R([p - q for p, q in zip(x1, x14)])
    v30 = R([p - q for p, q in zip(x2, x13)])
    v31 = R([p - q for p, q in zip(x3, x12)])
    d32 = [p * H for p in d27]
    v33 = R([K7 * q for q in v23])
    v34 = R([K6 * q for q in v24])
    v35 = R([K5 * q for q in v25])
    v36 = R([K4 * q for q in v26])
    v37 = R([K0 * q for q in v28])
    v38 = R([K1 * q for q in v29])
    v39 = R([K2 * q for q in v30])
    v40 = R([K3 * q for q in v31])
    f41 = R(d32)
    d42 = [p - q for p, q in zip(v13, v14)]
    v43 = R([p - q for p, q in zip(v37, v33)])
    v44 = R([p - q for p, q in zip(v38, v34)])
    v45 = R([p - q for p, q in zip(v39, v35)])
    v46 = R([p - q for p, q in zip(v40, v36)])
    v47 = R([p + q for p, q in zip(v37, v33)])
    v48 = R([p + q for p, q in zip(v38, v34)])
    v49 = R([p + q for p, q in zip(v39, v35)])
    v50 = R([p + q for p, q in zip(v40, v36)])
    v51 = R([K8 * q for q in v43])
    v52 = R([K9 * q for q in v44])
    v53 = R([K10 * q for q in v45])
    v54 = R([K11 * q for q in v46])
    d55 = [p - q for p, q in zip(d42, v15)]
    d56 = [p + q for p, q in zip(d55, v16)]
    d57 = [p * K16 for p in d56]
    d58 = [p - q for p, q in zip(v13, v16)]
    d59 = [p * K12 for p in d58]
    d60 = [p - q for p, q in zip(v14, v15)]
    d61 = [p * K13 for p in d60]
    d62 = [K17 * q for q in d59]
    d63 = [K18 * q for q in d61]
    d64 = [p - q for p, q in zip(d62, d63)]
    f65 = R(d64)
    d66 = [p - q for p, q in zip(v19, v22)]
    d67 = [p * K12 for p in d66]
    d68 = [p - q for p, q in zip(v20, v21)]
    d69 = [p * K13 for p in d68]
    d70 = [p + q for p, q in zip(v21, v20)]
    d71 = [p + q for p, q in zip(d70, v22)]
    d72 = [p + q for p, q in zip(d71, v19)]
    d73 = [p * H for p in d72]
    f74 = R(d73)
    d75 = [p + q for p, q in zip(d69, d67)]
    d76 = [p * H for p in d75]
    d77 = [p - q for p, q in zip(d76, f74)]
    f78 = R(d77)
    d79 = [p - q for p, q in zip(v19, v20)]
    d80 = [p - q for p, q in zip(d79, v21)]
    d81 = [p + q for p, q in zip(d80, v22)]
    d82 = [p * K16 for p in d81]
    d83 = [p - q for p, q in zip(d82, f78)]
    f84 = R(d83)
    d85 = [K17 * q for q in d67]
    d86 = [p * K18 for p in d69]
    d87 = [p - q for p, q in zip(d85, d86)]
    d88 = [p - q for p, q in zip(d87, f84)]
    f89 = R(d88)
    d90 = [p + q for p, q in zip(v50, v49)]
    d91 = [p + q for p, q in zip(d90, v48)]
    d92 = [p + q for p, q in zip(d91, v47)]
    d93 = [p * H for p in d92]
    d94 = [p - q for p, q in zip(d93, f41)]
    f95 = R(d94)
    d96 = [p - q for p, q in zip(f74, f95)]
    f97 = R(d96)
    d98 = [p - q for p, q in zip(v47, v48)]
    d99 = [p - q for p, q in zip(d98, v49)]
    d100 = [p + q for p, q in zip(d99, v50)]
    d101 = [p * K16 for p in d100]
    f102 = R(d101)
    d103 = [p - q for p, q in zip(v47, v50)]
    d104 = [p * K12 for p in d103]
    d105 = [p - q for p, q in zip(v48, v49)]
    d106 = [p * K13 for p in d105]
    d107 = [p * K17 for p in d104]
    d108 = [p * K18 for p in d106]
    d109 = [p - q for p, q in zip(d107, d108)]
    f110 = R(d109)
    d111 = [p + q for p, q in zip(v54, v53)]
    d112 = [p + q for p, q in zip(d111, v52)]
    d113 = [p + q for p, q in zip(d112, v51)]
    d114 = [p * H for p in d113]
    d115 = [p - q for p, q in zip(d114, d93)]
    d116 = [p - q for p, q in zip(d115, f97)]
    f117 = R(d116)
    d118 = [p + q for p, q in zip(d106, d104)]
    d119 = [p * H for p in d118]
    d120 = [p - q for p, q in zip(d119, d115)]
    f121 = R(d120)
    d122 = [p + q for p, q in zip(d61, d59)]
    d123 = [p * H for p in d122]
    d124 = [p - q for p, q in zip(d123, f117)]
    f125 = R(d124)
    d126 = [p - q for p, q in zip(v51, v54)]
    d127 = [p * K12 for p in d126]
    d128 = [p - q for p, q in zip(f121, f125)]
    f129 = R(d128)
    d130 = [p - q for p, q in zip(v52, v53)]
    d131 = [p * K13 for p in d130]
    d132 = [p - q for p, q in zip(f78, f129)]
    f133 = R(d132)
    d134 = [p + q for p, q in zip(d131, d127)]
    d135 = [p - q for p, q in zip(d134, v51)]
    d136 = [p - q for p, q in zip(d135, v52)]
    d137 = [p - q for p, q in zip(d136, v53)]
    d138 = [p - q for p, q in zip(d137, v54)]
    d139 = [p * H for p in d138]
    f140 = R(d139)
    d141 = [p - q for p, q in zip(v51, v52)]
    d142 = [p - q for p, q in zip(d141, v53)]
    d143 = [p + q for p, q in zip(d142, v54)]
    d144 = [p * K16 for p in d143]
    d145 = [p - q for p, q in zip(d144, f140)]
    f146 = R(d145)
    d147 = [p * K17 for p in d127]
    d148 = [p * K18 for p in d131]
    d149 = [p - q for p, q in zip(d147, d148)]
    d150 = [p - q for p, q in zip(d149, f146)]
    d151 = [p - q for p, q in zip(f140, f121)]
    d152 = [p - q for p, q in zip(d151, f133)]
    f153 = R(d152)
    d154 = [p - q for p, q in zip(d57, f153)]
    f155 = R(d154)
    d156 = [p - q for p, q in zip(f102, d151)]
    d157 = [p - q for p, q in zip(d156, f155)]
    f158 = R(d157)
    d159 = [p - q for p, q in zip(f84, f158)]
    f160 = R(d159)
    d161 = [p - q for p, q in zip(f146, d156)]
    d162 = [p - q for p, q in zip(d161, f160)]
    f163 = R(d162)
    d164 = [p - q for p, q in zip(f65, f163)]
    f165 = R(d164)
    d166 = [p - q for p, q in zip(f110, d161)]
    d167 = [p - q for p, q in zip(d166, f165)]
    f168 = R(d167)
    d169 = [p - q for p, q in zip(f89, f168)]
    f170 = R(d169)
    d171 = [p - q for p, q in zip(d150, d166)]
    d172 = [p - q for p, q in zip(d171, f170)]
    f173 = R(d172)
    return (f173, f170, f168, f165, f163, f160, f158, f155, f153, f133, f129, f125, f117, f97, f95, f41)
