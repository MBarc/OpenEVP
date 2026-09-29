"""LPEC ST bitstream: parse one 280-byte frame into integer parameters.

The frame is a sequence of units, each introduced by a 2-bit type after a
leading 0 bit: 1 = stereo channel unit (the only kind an LPEC ST stereo
decoder accepts; 0 = mono is rejected), 2 = extension (skipped), 3 = end.
A stereo channel unit holds, in order: the number of quantisation units,
word lengths, scale factors, code-table selectors, the quantised spectrum
(plus power levels and the stereo swap/negate flags), window shapes, gain
control points, tone (sinusoid) parameters and the noise switch.

Everything here is integer work; see dsp.py for the synthesis. Structure
and field names follow what the decoder in Sony's lcstde.ax reads, in the
same order and with the same validity checks (ParseError carries the
decoder's own error code). Sony's structures are mirrored where the
decoder carries data from one channel to the other or from one frame to
the next.
"""

from __future__ import annotations

from typing import List


class ParseError(Exception):
    """The frame is invalid; ``code`` is the error code the DLL records."""

    def __init__(self, code: int):
        super().__init__(f"LPEC ST frame rejected (code {code:#x})")
        self.code = code


class BitReader:
    """MSB-first reader; a read of n bits looks at the three bytes from the
    current byte (the DLL's reader), and reads at or past bit 0x10000 give 0."""

    __slots__ = ("buf", "base", "pos")

    def __init__(self, buf: bytes, base: int):
        self.buf = buf       # must hold at least 3 bytes past any byte read
        self.base = base     # byte offset of bit 0
        self.pos = 0

    def read(self, n: int) -> int:
        p = self.pos
        self.pos = p + n
        if p >= 0x10000:
            return 0
        i = self.base + (p >> 3)
        b = self.buf
        w = (b[i] << 16) | (b[i + 1] << 8) | b[i + 2]
        return ((w << (p & 7)) & 0xFFFFFF) >> ((24 - n) & 31)

    def huff(self, h) -> int:
        """Decode one symbol with a Huffman descriptor (peek maxbits bits,
        look up the symbol, give back the bits beyond its code length)."""
        mb = h.maxbits
        sym = h.lut[self.read(mb)]
        self.pos += h.lens[sym] - mb
        return sym


def _sext(v: int, bits: int) -> int:
    """Sign-extend the low ``bits`` bits of a symbol."""
    m = 1 << bits
    v &= m - 1
    return v - m if v & (m >> 1) else v


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------


class GainBand:
    __slots__ = ("npoints", "loc", "lev")

    def __init__(self):
        self.npoints = 0
        self.loc = [0] * 7
        self.lev = [0] * 7

    def copy_from(self, o: "GainBand"):
        self.npoints = o.npoints
        self.loc = list(o.loc)
        self.lev = list(o.lev)


class Wave:
    __slots__ = ("amp_sf", "amp_idx", "phase", "freq")

    def __init__(self):
        self.amp_sf = 0
        self.amp_idx = 0
        self.phase = 0
        self.freq = 0


class ToneBand:
    """One subband's tones (the DLL's 10-dword band record)."""

    __slots__ = ("has_start", "has_stop", "start_pos", "stop_pos", "nwavs", "waves",
                 "w_start_on", "w_stop_on", "w_start", "w_stop")

    def __init__(self):
        # envelope window, filled in by the synthesis (dsp._tone_window_params)
        self.w_start_on = 0
        self.w_stop_on = 0
        self.w_start = 0
        self.w_stop = 0
        self.has_start = 0
        self.has_stop = 0
        self.start_pos = 0
        self.stop_pos = 0x20
        self.nwavs = 0
        self.waves: List[Wave] = []

    def copy_env(self, o: "ToneBand"):
        self.has_start = o.has_start
        self.has_stop = o.has_stop
        self.start_pos = o.start_pos
        self.stop_pos = o.stop_pos

    def copy_all(self, o: "ToneBand"):
        self.copy_env(o)
        self.nwavs = o.nwavs
        self.waves = o.waves
        self.w_start_on = o.w_start_on
        self.w_stop_on = o.w_stop_on
        self.w_start = o.w_start
        self.w_stop = o.w_stop


class Channel:
    """Per-channel parameters of one frame."""

    def __init__(self, idx: int):
        self.idx = idx
        self.wl = [0] * 32
        self.sf = [0] * 32
        self.ct = [0] * 32
        self.ct_flags = [0] * 32
        self.ct_set = 0                 # which code-table set (0/1)
        self.spec = [0] * 2048
        self.power = [15] * 5
        self.wnd = [0] * 16             # window shape per subband
        self.gain = [GainBand() for _ in range(16)]
        self.gain_nsb = 0               # subbands with gain data
        self.gain_coded = 0
        self.tones = [ToneBand() for _ in range(16)]
        self.tone_flags = [0] * 16      # bands whose tones this channel codes


class Unit:
    """One frame of a stereo channel unit."""

    def __init__(self):
        self.nqu = 0
        self.mute = 0
        self.ncoded_qu = 0
        self.nsb = 0
        self.ncoded_sb = 0
        self.use_full = 0
        self.swap = [0] * 16
        self.negate = [0] * 16
        self.noise_present = 0
        self.noise_level = 0
        self.noise_table = 0
        self.tones_present = 0
        self.amp_mode = 0
        self.ntone_bands = 0
        self.tone_share = [0] * 16
        self.tone_swap = [0] * 16
        self.tone_negate = [0] * 16
        self.refwave = []
        self.share_prev_env = []      # bands whose previous envelope ch1 takes from ch0
        self.noise_counter = 0        # noise offset index, advanced by the synthesis
        self.warning = 0                # DLL code 4: nqu of 29..31 (still decoded)
        self.ch = [Channel(0), Channel(1)]


# ---------------------------------------------------------------------------
# Frame
# ---------------------------------------------------------------------------


def parse_frame(buf: bytes, base: int, t, frame_bytes: int = 280) -> Unit:
    """Parse the frame whose codec bytes start at buf[base]."""
    br = BitReader(buf, base)
    if br.read(1) != 0:
        raise ParseError(0x208)
    unit = None
    utype = br.read(2)
    while utype != 3:
        if br.pos > 0x10000:
            raise ParseError(0x209)
        if utype == 2:
            br.read(5)
            n = br.read(11)
            if n > 0x7FE:
                raise ParseError(0x215)
            for _ in range(n):
                br.read(8)
        else:
            if unit is not None:
                raise ParseError(0x20C)
            if utype != 1:               # a mono unit in a stereo stream
                raise ParseError(0x20A)
            unit = Unit()
            _parse_unit(br, unit, t)
        if br.pos > frame_bytes * 8 - 2:
            raise ParseError(0x214)
        utype = br.read(2)
    if unit is None:
        raise ParseError(0x20D)
    return unit


def _parse_unit(br: BitReader, u: Unit, t) -> None:
    u.nqu = br.read(5) + 1
    if u.nqu in (29, 30, 31):
        u.warning = 4
    u.nsb = t.QU_TO_SB[u.nqu] + 1
    u.mute = br.read(1)
    _wordlens(br, u, t)
    _scalefactors(br, u, t)
    _codetables(br, u, t)
    _spectrum(br, u, t)
    for c in u.ch:                        # window shapes
        c.wnd = _flags(br, u.nsb)[2]
    _gain(br, u, t)
    _tones(br, u, t)
    u.noise_present = br.read(1)
    if u.noise_present:
        u.noise_level = br.read(4)
        u.noise_table = br.read(4)


def _flags(br: BitReader, count: int):
    """A subband flag set: present bit, all-set-or-coded bit, flags."""
    flags = [0] * 16
    present = br.read(1)
    mode = 0
    if present:
        mode = br.read(1)
        if mode == 0:
            for i in range(count):
                flags[i] = 1
        else:
            for i in range(count):
                flags[i] = br.read(1)
    return present, mode, flags


# ---------------------------------------------------------------------------
# Word lengths
# ---------------------------------------------------------------------------


def _wordlens(br, u: Unit, t):
    for c in u.ch:
        c.wl = [0] * 32
        mode = br.read(2)
        if c.idx == 0:
            (_wl_m0, _wl_ch0_m1, _wl_ch0_m2, _wl_m3)[mode](br, u, c, t)
        else:
            (_wl_m0, _wl_ch1_m1, _wl_ch1_m2, _wl_m3)[mode](br, u, c, t)
    # number of coded quantisation units: last unit with a nonzero word
    # length in either channel
    n = u.nqu
    wl0, wl1 = u.ch[0].wl, u.ch[1].wl
    while n > 0 and wl0[n - 1] == 0 and wl1[n - 1] == 0:
        n -= 1
    u.ncoded_qu = n
    u.ncoded_sb = t.QU_TO_SB[n] + 1
    for c in u.ch:
        for v in c.wl:
            if v < 0 or v > 7:
                raise ParseError(0x10B)


def _wl_ncoded(br, u, c):
    """Number of coded values and the fill mode for the rest."""
    fill = br.read(2)
    extra = 0
    if fill == 0:
        n = u.nqu
    else:
        n = br.read(5)
        if n > u.nqu:
            raise ParseError(0x10F)
        if fill == 3:
            extra = br.read(2) + (1 if c.idx == 0 else 3)
    return fill, n, extra


def _wl_fill(br, u, c, fill, n, extra):
    wl = c.wl
    nqu = u.nqu
    if fill == 1:
        for i in range(n, nqu):
            wl[i] = 0
    elif fill == 2:
        if c.idx == 0:
            for i in range(n, nqu):
                wl[i] = 1
        else:
            for i in range(n, nqu):
                wl[i] = br.read(1)
    elif fill == 3:
        if c.idx == 0:
            end = nqu - extra
            if end < n or end >= nqu:
                raise ParseError(0x10C)
        else:
            end = extra + n
            if end <= n or end > nqu:
                raise ParseError(0x10E)
        for i in range(n, end):
            wl[i] = 1
        for i in range(end, nqu):
            wl[i] = 0


def _wl_weights(u, c, weight, t):
    if weight:
        base = (c.idx * 3 + weight) * 32
        w = t.WL_WEIGHTS
        for i in range(u.nqu):
            c.wl[i] += w[base + i]


def _wl_m0(br, u, c, t):
    for i in range(u.nqu):
        c.wl[i] = br.read(3)


def _wl_ch0_m1(br, u, c, t):
    weight = br.read(2)
    fill, n, extra = _wl_ncoded(br, u, c)
    if n > 0:
        ndirect = br.read(5)
        if ndirect > n:
            raise ParseError(0x10D)
        dbits = br.read(2)
        minv = br.read(3)
        wl = c.wl
        for i in range(ndirect):
            wl[i] = br.read(3)
        if dbits < 1:
            for i in range(ndirect, n):
                wl[i] = minv
        else:
            for i in range(ndirect, n):
                wl[i] = br.read(dbits) + minv
    _wl_fill(br, u, c, fill, n, extra)
    _wl_weights(u, c, weight, t)


def _wl_shape(c, n, base, shape, t):
    """Fill wl[0..n) from a shape: group g gets base - WL_SHAPES[...][g-1]."""
    groups = t.SHAPE_GROUP[n]            # table index n means "units up to n"
    row = (base * 16 + shape) * 9
    vals = [base] + [base - t.WL_SHAPES[row + i - 1] for i in range(1, groups + 1)]
    for i in range(n):
        c.wl[i] = vals[t.SHAPE_GROUP[1 + i]]


def _wl_ch0_m2(br, u, c, t):
    fill, n, extra = _wl_ncoded(br, u, c)
    if n > 0:
        pairs = br.read(1)
        h = t.H_WL[br.read(1)]
        base = br.read(3)
        shape = br.read(4)
        _wl_shape(c, n, base, shape, t)
        wl = c.wl
        if pairs == 0:
            for i in range(n):
                wl[i] += br.huff(h)
        else:
            npairs = n >> 1
            for p in range(npairs):
                if br.read(1) == 0:
                    wl[2 * p] += br.huff(h)
                    wl[2 * p + 1] += br.huff(h)
            for i in range(npairs * 2, n):
                wl[i] += br.huff(h)
        for i in range(n):
            wl[i] &= 7
    _wl_fill(br, u, c, fill, n, extra)


def _wl_m3(br, u, c, t):
    weight = br.read(2)
    fill, n, extra = _wl_ncoded(br, u, c)
    if n > 0:
        h = t.H_WL[br.read(2)]
        wl = c.wl
        wl[0] = br.read(3)
        for i in range(1, n):
            wl[i] = (wl[i - 1] + br.huff(h)) & 7
    _wl_fill(br, u, c, fill, n, extra)
    _wl_weights(u, c, weight, t)


def _wl_ch1_m1(br, u, c, t):
    fill, n, extra = _wl_ncoded(br, u, c)
    if n > 0:
        h = t.H_WL[br.read(2)]
        ref = u.ch[0].wl
        for i in range(n):
            c.wl[i] = (ref[i] + br.huff(h)) & 7
    _wl_fill(br, u, c, fill, n, extra)


def _wl_ch1_m2(br, u, c, t):
    fill, n, extra = _wl_ncoded(br, u, c)
    if n > 0:
        h = t.H_WL[br.read(2)]
        ref = u.ch[0].wl
        wl = c.wl
        wl[0] = (ref[0] + br.huff(h)) & 7
        for i in range(1, n):
            wl[i] = ((ref[i] - ref[i - 1]) + wl[i - 1] + br.huff(h)) & 7
    _wl_fill(br, u, c, fill, n, extra)


# ---------------------------------------------------------------------------
# Scale factors
# ---------------------------------------------------------------------------


def _scalefactors(br, u: Unit, t):
    n = u.ncoded_qu
    if n <= 0:
        return                            # the DLL leaves the old values
    for c in u.ch:
        c.sf = [0] * 32
        mode = br.read(2)
        if c.idx == 0:
            (_sf_m0, _sf_ch0_m1, _sf_ch0_m2, _sf_ch0_m3)[mode](br, u, c, t)
        else:
            (_sf_m0, _sf_ch1_m1, _sf_ch1_m2, _sf_ch1_m3)[mode](br, u, c, t)
    for c in u.ch:
        for v in c.sf:
            if v < 0 or v > 63:
                raise ParseError(0x110)


def _sf_shape(c, n, base, shape, t):
    groups = t.SHAPE_GROUP[n]
    row = shape * 9
    vals = [base] + [base - t.SF_SHAPES[row + i - 1] for i in range(1, groups + 1)]
    for i in range(n):
        c.sf[i] = vals[t.SHAPE_GROUP[1 + i]]


def _sf_weights(u, c, weight, t):
    w = t.SF_WEIGHTS
    for i in range(u.ncoded_qu):
        c.sf[i] -= w[weight * 32 + i]


def _sf_m0(br, u, c, t):
    for i in range(u.ncoded_qu):
        c.sf[i] = br.read(6)


def _sf_ch0_m1(br, u, c, t):
    n = u.ncoded_qu
    sf = c.sf
    weight = br.read(2)
    if weight == 3:
        base = br.read(6)
        shape = br.read(6)
        _sf_shape(c, n, base, shape, t)
        ndirect = br.read(5)
        if ndirect < 0 or ndirect > n:
            raise ParseError(0x113)
        dbits = br.read(2)
        minv = br.read(4) - 7
        for i in range(ndirect):
            sf[i] += br.read(4) - 7
        if dbits > 0:
            for i in range(ndirect, n):
                sf[i] += br.read(dbits)
        for i in range(ndirect, n):
            sf[i] += minv
        for i in range(n):
            sf[i] &= 0x3F
    else:
        ndirect = br.read(5)
        if ndirect < 0 or ndirect > n:
            raise ParseError(0x112)
        dbits = br.read(3)
        if dbits > 6:
            raise ParseError(0x111)
        minv = br.read(6)
        for i in range(ndirect):
            sf[i] = br.read(6)
        if dbits < 1:
            for i in range(ndirect, n):
                sf[i] = minv
        else:
            for i in range(ndirect, n):
                sf[i] = br.read(dbits) + minv
        if weight != 0:
            _sf_weights(u, c, weight, t)


def _sf_ch0_m2(br, u, c, t):
    n = u.ncoded_qu
    h = t.H_SF_DELTA[br.read(2)]
    base = br.read(6)
    shape = br.read(6)
    _sf_shape(c, n, base, shape, t)
    sf = c.sf
    for i in range(n):
        sf[i] = (sf[i] + _sext(br.huff(h), 4)) & 0x3F


def _sf_ch0_m3(br, u, c, t):
    n = u.ncoded_qu
    sf = c.sf
    weight = br.read(2)
    hidx = br.read(2)
    if weight == 3:
        base = br.read(6)
        shape = br.read(6)
        _sf_shape(c, n, base, shape, t)
        h = t.H_SF_DELTA[hidx]
        d = [0] * n
        d[0] = (br.read(4) - 8) & 0x3F
        for i in range(1, n):
            d[i] = (d[i - 1] + _sext(br.huff(h), 4)) & 0x3F
        for i in range(n):
            sf[i] = (d[i] + sf[i]) & 0x3F
    else:
        h = t.H_SF[hidx]
        sf[0] = br.read(6)
        for i in range(1, n):
            sf[i] = (sf[i - 1] + br.huff(h)) & 0x3F
        if weight != 0:
            _sf_weights(u, c, weight, t)


def _sf_ch1_m1(br, u, c, t):
    h = t.H_SF[br.read(2)]
    ref = u.ch[0].sf
    for i in range(u.ncoded_qu):
        c.sf[i] = (ref[i] + br.huff(h)) & 0x3F


def _sf_ch1_m2(br, u, c, t):
    h = t.H_SF[br.read(2)]
    ref = u.ch[0].sf
    sf = c.sf
    sf[0] = (ref[0] + br.huff(h)) & 0x3F
    for i in range(1, u.ncoded_qu):
        sf[i] = ((ref[i] - ref[i - 1]) + sf[i - 1] + br.huff(h)) & 0x3F


def _sf_ch1_m3(br, u, c, t):
    ref = u.ch[0].sf
    for i in range(u.ncoded_qu):
        c.sf[i] = ref[i]


# ---------------------------------------------------------------------------
# Code-table selectors
# ---------------------------------------------------------------------------


def _codetables(br, u: Unit, t):
    n = u.ncoded_qu
    if n <= 0:
        return
    u.use_full = br.read(1)
    ch0 = u.ch[0]
    for c in u.ch:
        c.ct = [0] * 32
        c.ct_set = br.read(1)
        mode = br.read(2)
        flags = [0] * 32
        if c.idx == 0:
            for i in range(n):
                if c.wl[i] > 0:
                    flags[i] = 1
        else:
            for i in range(n):
                if c.wl[i] >= 1:
                    flags[i] = 1
                elif ch0.wl[i] > 0:
                    flags[i] = 2
        c.ct_flags = flags
        if mode == 3 and c.idx == 0:
            continue                      # all zero, nothing coded
        if br.read(1) == 0:
            cnt = n
        else:
            cnt = br.read(5)
            if cnt > n:
                raise ParseError(0x114)
        ct = c.ct
        if mode == 0:
            bits = t.CT_BITS[u.use_full]
            for i in range(cnt):
                f = flags[i]
                ct[i] = br.read(bits) if f == 1 else (br.read(1) if f == 2 else 0)
        elif mode == 1:
            h = t.H_CT[1 if u.use_full else 0]
            for i in range(cnt):
                f = flags[i]
                ct[i] = br.huff(h) if f == 1 else (br.read(1) if f == 2 else 0)
        elif mode == 2:
            if cnt > 0:
                ha = t.H_CT[1 if u.use_full else 0]
                hb = t.H_CT[2 if u.use_full else 0]
                prev = 0
                f = flags[0]
                if f == 1:
                    prev = br.huff(ha)
                    ct[0] = prev
                elif f == 2:
                    ct[0] = br.read(1)
                for i in range(1, cnt):
                    f = flags[i]
                    if f == 1:
                        prev = (br.huff(hb) + prev) & hb.mask
                        ct[i] = prev
                    elif f == 2:
                        ct[i] = br.read(1)
        else:  # mode 3, channel 1
            h = t.H_CT[3 if u.use_full else 0]
            ref = ch0.ct
            for i in range(cnt):
                f = flags[i]
                if f == 1:
                    ct[i] = (ref[i] + br.huff(h)) & h.mask
                elif f == 2:
                    ct[i] = br.read(1)
    lim = 8 if u.use_full else 4
    for c in u.ch:
        for v in c.ct:
            if v < 0 or v >= lim:
                raise ParseError(0x104)


# ---------------------------------------------------------------------------
# Spectrum
# ---------------------------------------------------------------------------


def _spectrum(br, u: Unit, t):
    n = u.ncoded_qu
    for c in u.ch:
        spec = [0] * 2048
        c.spec = spec
        c.power = [15] * 5
        for qu in range(n):
            wl = c.wl[qu]
            if wl < 1:
                continue
            ct = c.ct[qu]
            if u.use_full == 0:
                ct = t.CT_REMAP[(c.ct_set * 7 + wl) * 4 + ct]
            h = t.H_SPEC[(ct + c.ct_set * 8) * 7 + wl]
            _spec_qu(br, h, spec, t.QU_START[qu], t.QU_LEN[qu])
        if n > 2:                         # coded units, not subbands
            for i in range(t.SB_POWGRPS[u.ncoded_sb] + 1):
                c.power[i] = br.read(4)
    u.swap = _flags(br, u.ncoded_sb)[2]
    u.negate = _flags(br, u.ncoded_sb)[2]


def _spec_qu(br, h, spec, start, count):
    ncw = count >> h.shift              # codewords in this unit
    per = h.n                           # coefficients per codeword
    group = h.group
    syms = [0] * ncw
    signs = [0] * ncw
    if group == 1:
        _spec_codewords(br, h, syms, signs, 0, ncw)
    elif group > 1:
        for g in range(0, ncw, group):
            if br.read(1):
                _spec_codewords(br, h, syms, signs, g, group)
    bits = h.bits
    mask = h.mask
    pos = start
    if not h.unsigned:
        top = 1 << (bits - 1)
        for k in range(ncw):
            s = syms[k]
            for j in range(per):
                sh = (per - 1 - j) * bits
                v = (s >> sh) & mask
                if mask & top & (s >> sh):
                    v -= 1 << bits
                spec[pos] = v
                pos += 1
    else:
        for k in range(ncw):
            s = syms[k]
            if s == 0:
                pos += per
                continue
            nsign = signs[k][0]
            sbits = signs[k][1]
            bit = 1 << (nsign - 1) if nsign > 0 else 0
            for j in range(per):
                v = (s >> ((per - 1 - j) * bits)) & mask
                if v:
                    if sbits & bit:
                        v = -v
                    bit >>= 1
                spec[pos] = v
                pos += 1


def _spec_codewords(br, h, syms, signs, first, count):
    per = h.n
    bits = h.bits
    mask = h.mask
    for k in range(first, first + count):
        s = br.huff(h)
        syms[k] = s
        if h.unsigned and s != 0:
            nsign = 0
            for j in range(per):
                if s & (mask << ((per - j - 1) * bits)):
                    nsign += 1
            signs[k] = (nsign, br.read(nsign))


# ---------------------------------------------------------------------------
# Gain control
# ---------------------------------------------------------------------------


def _gain(br, u: Unit, t):
    for c in u.ch:
        c.gain = [GainBand() for _ in range(16)]
        if br.read(1) == 0:
            c.gain_nsb = 0
            continue
        coded = br.read(4) + 1
        if br.read(1) == 0:
            c.gain_nsb = coded
        else:
            c.gain_nsb = br.read(4) + 1
        c.gain_coded = coded
        _gain_npoints(br, u, c, t)
        _gain_levels(br, u, c, t)
        _gain_locations(br, u, c, t)
        for b in range(coded, c.gain_nsb):
            c.gain[b].copy_from(c.gain[b - 1])


def _gain_npoints(br, u, c, t):
    mode = br.read(2)
    g = c.gain
    n = c.gain_coded
    H = t.H_GAIN
    ref = u.ch[0].gain
    if mode == 0:
        for b in range(n):
            g[b].npoints = br.read(3)
    elif mode == 1:
        for b in range(n):
            g[b].npoints = br.huff(H[0])
    elif c.idx == 0 and mode == 2:
        g[0].npoints = br.huff(H[0])
        for b in range(1, n):
            g[b].npoints = (g[b - 1].npoints + br.huff(H[1])) & 7
    elif c.idx == 0:
        bits = br.read(2)
        minv = br.read(3)
        for b in range(n):
            g[b].npoints = minv if bits < 1 else br.read(bits) + minv
    elif mode == 2:
        for b in range(n):
            g[b].npoints = (ref[b].npoints + br.huff(H[1])) & 7
    else:
        for b in range(n):
            g[b].npoints = ref[b].npoints
    for b in range(16):
        if g[b].npoints > 7:
            raise ParseError(0x115)


def _lev_delta_band(br, band, H):
    if band.npoints > 0:
        band.lev[0] = br.huff(H[2])
        for i in range(1, band.npoints):
            band.lev[i] = (band.lev[i - 1] + br.huff(H[3])) & 0xF


def _gain_levels(br, u, c, t):
    mode = br.read(2)
    g = c.gain
    n = c.gain_coded
    H = t.H_GAIN
    ref = u.ch[0].gain
    if mode == 0:
        for b in range(n):
            for i in range(g[b].npoints):
                g[b].lev[i] = br.read(4)
    elif c.idx == 0 and mode == 1:
        for b in range(n):
            _lev_delta_band(br, g[b], H)
    elif c.idx == 0 and mode == 2:
        _lev_delta_band(br, g[0], H)
        for b in range(1, n):
            prev = g[b - 1]
            for i in range(g[b].npoints):
                d = br.huff(H[4])
                g[b].lev[i] = ((prev.lev[i] + d) if i < prev.npoints else (d + 7)) & 0xF
    elif c.idx == 0:
        bits = br.read(2)
        minv = br.read(4)
        for b in range(n):
            for i in range(g[b].npoints):
                g[b].lev[i] = minv if bits < 1 else br.read(bits) + minv
    elif mode == 1:
        for b in range(n):
            r = ref[b]
            for i in range(g[b].npoints):
                d = br.huff(H[5])
                g[b].lev[i] = ((r.lev[i] + d) if i < r.npoints else (d + 7)) & 0xF
    elif mode == 2:
        for b in range(n):
            if g[b].npoints > 0:
                if br.read(1) == 0:
                    r = ref[b]
                    for i in range(g[b].npoints):
                        g[b].lev[i] = r.lev[i] if i < r.npoints else 7
                else:
                    _lev_delta_band(br, g[b], H)
    else:
        for b in range(n):
            r = ref[b]
            for i in range(g[b].npoints):
                g[b].lev[i] = r.lev[i] if i < r.npoints else 7
    for b in range(16):
        lev = g[b].lev
        for i in range(7):
            if lev[i] < 0 or lev[i] > 15:
                raise ParseError(0x116)
        for i in range(1, g[b].npoints):
            if lev[i - 1] == lev[i]:
                raise ParseError(0x118)


def _loc_raw(br, band, i):
    """Location i coded against location i - 1 (or 5 bits for the first)."""
    loc = band.loc
    if i == 0:
        loc[0] = br.read(5)
        return
    p = loc[i - 1]
    if p < 15:
        loc[i] = br.read(5)
    elif p < 23:
        loc[i] = br.read(4) + p + 1
    elif p < 27:
        loc[i] = br.read(3) + p + 1
    elif p < 29:
        loc[i] = br.read(2) + p + 1
    elif p == 29:
        loc[i] = br.read(1) + 30
    elif p == 30:
        loc[i] = 31


def _loc_delta_band(br, band, H):
    if band.npoints > 0:
        loc, lev = band.loc, band.lev
        loc[0] = br.read(5)
        for i in range(1, band.npoints):
            h = H[7] if lev[i] > lev[i - 1] else H[6]
            loc[i] = loc[i - 1] + br.huff(h)


def _gain_locations(br, u, c, t):
    mode = br.read(2)
    g = c.gain
    n = c.gain_coded
    H = t.H_GAIN
    ref = u.ch[0].gain
    if mode == 0:
        for b in range(n):
            for i in range(g[b].npoints):
                _loc_raw(br, g[b], i)
    elif c.idx == 0 and mode == 1:
        for b in range(n):
            _loc_delta_band(br, g[b], H)
    elif c.idx == 0 and mode == 2:
        for i in range(g[0].npoints):
            _loc_raw(br, g[0], i)
        tabs = (H[8], H[6], H[9], H[7])
        for b in range(1, n):
            band, prev = g[b], g[b - 1]
            if band.npoints <= 0:
                continue
            loc, lev = band.loc, band.lev
            if prev.npoints < 1:
                loc[0] = br.huff(H[8])
            else:
                loc[0] = (prev.loc[0] + br.huff(H[8])) & 0x1F
            for i in range(1, band.npoints):
                beyond = 1 if prev.npoints <= i else 0
                h = tabs[beyond + (2 if lev[i] > lev[i - 1] else 0)]
                d = br.huff(h)
                if beyond == 0:
                    loc[i] = (prev.loc[i] + d) & 0x1F
                else:
                    loc[i] = loc[i - 1] + d
    elif c.idx == 0:
        bits = br.read(2) + 1
        minv = br.read(5)
        for b in range(n):
            for i in range(g[b].npoints):
                g[b].loc[i] = br.read(bits) + minv + i
    elif mode == 1:
        for b in range(n):
            band, r = g[b], ref[b]
            if band.npoints <= 0:
                continue
            loc, lev = band.loc, band.lev
            if r.npoints < 1:
                loc[0] = br.huff(H[10])
            else:
                loc[0] = (r.loc[0] + br.huff(H[10])) & 0x1F
            for i in range(1, band.npoints):
                beyond = r.npoints <= i
                if lev[i] <= lev[i - 1]:
                    d = br.huff(H[6] if beyond else H[10])
                    loc[i] = loc[i - 1] + d if beyond else (r.loc[i] + d) & 0x1F
                elif beyond:
                    loc[i] = loc[i - 1] + br.huff(H[7])
                elif br.read(1) == 0:
                    loc[i] = r.loc[i]
                else:
                    _loc_raw(br, band, i)
    elif mode == 2:
        for b in range(n):
            band, r = g[b], ref[b]
            if band.npoints <= 0:
                continue
            if r.npoints < band.npoints or br.read(1) != 0:
                _loc_delta_band(br, band, H)
            else:
                for i in range(band.npoints):
                    band.loc[i] = r.loc[i]
    else:
        for b in range(n):
            band, r = g[b], ref[b]
            for i in range(band.npoints):
                if i < r.npoints:
                    band.loc[i] = r.loc[i]
                else:
                    _loc_raw(br, band, i)
    for b in range(16):
        loc = g[b].loc
        for i in range(7):
            if loc[i] < 0 or loc[i] > 31:
                raise ParseError(0x117)
        for i in range(1, g[b].npoints):
            if loc[i] <= loc[i - 1]:
                raise ParseError(0x119)


# ---------------------------------------------------------------------------
# Tones
# ---------------------------------------------------------------------------


def _tones(br, u: Unit, t):
    ch0, ch1 = u.ch
    for c in u.ch:
        c.tones = [ToneBand() for _ in range(16)]
    u.tone_share = [0] * 16
    u.tone_swap = [0] * 16
    u.tone_negate = [0] * 16
    u.tones_present = br.read(1)
    if not u.tones_present:
        return
    u.amp_mode = br.read(1)
    nb = br.huff(t.H_TONE[0]) + 1
    u.ntone_bands = nb
    u.tone_share = _flags(br, nb)[2]
    u.tone_swap = _flags(br, nb)[2]
    u.tone_negate = _flags(br, nb)[2]
    u.refwave = []
    u.share_prev_env = []
    for c in u.ch:
        _tone_channel(br, u, c, t)
    for b in range(nb):
        if u.tone_share[b]:
            ch1.tones[b].copy_all(ch0.tones[b])
            u.share_prev_env.append(b)   # the decoder copies ch0's previous envelope too
        if u.tone_swap[b]:
            ch0.tones[b], ch1.tones[b] = ch1.tones[b], ch0.tones[b]


def _tone_channel(br, u, c, t):
    nb = u.ntone_bands
    H = t.H_TONE
    ch0 = u.ch[0]
    if c.idx == 0:
        c.tone_flags = [1 if b < nb else 0 for b in range(16)]
    else:
        c.tone_flags = [0] * 16
        for b in range(nb):
            c.tone_flags[b] = 1 if u.tone_share[b] == 0 else 0
    flags = c.tone_flags
    bands = c.tones
    # envelope
    env_mode = br.read(1) if c.idx == 1 else 0
    for b in range(nb):
        if not flags[b]:
            continue
        tb = bands[b]
        if env_mode == 0:
            tb.has_start = br.read(1)
            tb.start_pos = br.read(5) if tb.has_start else -1
            tb.has_stop = br.read(1)
            tb.stop_pos = br.read(5) if tb.has_stop else 0x20
        else:
            tb.copy_env(ch0.tones[b])
    # number of waves
    mode = br.read(t.TONE_MODE_BITS[c.idx])
    for b in range(nb):
        if not flags[b]:
            continue
        tb = bands[b]
        if mode == 0:
            tb.nwavs = br.read(4)
        elif mode == 1:
            tb.nwavs = br.huff(H[1])
        elif mode == 2:
            tb.nwavs = (ch0.tones[b].nwavs + _sext(br.huff(H[2]), 3)) & 0xF
        else:
            tb.nwavs = ch0.tones[b].nwavs
    total = 0
    for b in range(16):
        total += bands[b].nwavs
        if c.idx == 1:
            total += ch0.tones[b].nwavs
    if total >= 0x31:
        raise ParseError(0x11A)
    # wave records
    for b in range(nb):
        bands[b].waves = [Wave() for _ in range(bands[b].nwavs)]
    # frequencies
    fmode = br.read(1) if c.idx == 1 else 0
    for b in range(nb):
        if not flags[b]:
            continue
        tb = bands[b]
        w = tb.waves
        if fmode == 0:
            desc = 0
            if tb.nwavs >= 2:
                desc = br.read(1)
            if desc == 0:
                for k in range(tb.nwavs):
                    if k == 0:
                        w[0].freq = br.read(10)
                        continue
                    p = w[k - 1].freq
                    if p < 0x200:
                        w[k].freq = br.read(10)
                    elif p < 0x300:
                        w[k].freq = br.read(9) + 0x200
                    elif p < 0x380:
                        w[k].freq = br.read(8) + 0x300
                    elif p < 0x3C0:
                        w[k].freq = br.read(7) + 0x380
                    elif p < 0x3E0:
                        w[k].freq = br.read(6) + 0x3C0
                    elif p < 0x3F0:
                        w[k].freq = br.read(5) + 0x3E0
                    elif p < 0x3F8:
                        w[k].freq = br.read(4) + 0x3F0
                    elif p < 0x3FC:
                        w[k].freq = br.read(3) + 0x3F8
                    elif p < 0x3FE:
                        w[k].freq = br.read(2) + 0x3FC
                    else:
                        w[k].freq = br.read(1) + 0x3FE
            else:
                for k in range(tb.nwavs - 1, -1, -1):
                    if k == tb.nwavs - 1:
                        bits = 10
                    else:
                        v = w[k + 1].freq
                        bits = 10
                        for nb_, lim in ((1, 2), (2, 4), (3, 8), (4, 16), (5, 32), (6, 64),
                                         (7, 128), (8, 256), (9, 512)):
                            if v < lim:
                                bits = nb_
                                break
                    w[k].freq = br.read(bits)
        else:
            r = ch0.tones[b]
            for k in range(tb.nwavs):
                d = _sext(br.huff(H[3]), 8)
                if k < r.nwavs:
                    v = r.waves[k].freq + d
                elif r.nwavs > 0:
                    v = r.waves[r.nwavs - 1].freq + d
                else:
                    v = d
                w[k].freq = v & 0x3FF
    # reference waves (channel 1): nearest channel-0 wave in the same band
    if c.idx == 1:
        refs = []
        for b in range(nb):
            if not flags[b]:
                continue
            tb = bands[b]
            if tb.nwavs <= 0:
                continue
            r = ch0.tones[b]
            for k in range(tb.nwavs):
                best, bestd = 0, 0x400
                for j in range(r.nwavs):
                    dlt = abs(tb.waves[k].freq - r.waves[j].freq)
                    if dlt < bestd:
                        bestd, best = dlt, j
                if r.nwavs < 1 or bestd > 7:
                    refs.append(k if k < r.nwavs else -1)
                else:
                    refs.append(best)
        u.refwave = refs
    # amplitude scale factors
    mode = br.read(t.AMPSF_MODE_BITS[c.idx])
    _tone_ampsf(br, u, c, t, mode)
    if u.amp_mode == 0:
        mode = br.read(t.AMPIDX_MODE_BITS[c.idx])
        _tone_ampidx(br, u, c, t, mode)
    # phases
    for b in range(nb):
        if flags[b]:
            for w in bands[b].waves:
                w.phase = br.read(5)


def _tone_ampsf(br, u, c, t, mode):
    nb = u.ntone_bands
    H = t.H_TONE
    flags = c.tone_flags
    bands = c.tones
    ch0 = u.ch[0]
    k = 0                                  # running wave index (channel 1 refs)
    for b in range(nb):
        if not flags[b]:
            continue
        tb = bands[b]
        w = tb.waves
        if u.amp_mode == 0:
            if tb.nwavs <= 0:
                continue
            if mode == 0:
                v = br.read(6)
            elif mode == 1:
                v = br.huff(H[4]) + 0x18
            elif mode == 2:
                d = _sext(br.huff(H[6]), 5)
                r = ch0.tones[b]
                v = ((d + 0x2C) if r.nwavs < 1 else (r.waves[0].amp_sf + d)) & 0x3F
            else:
                r = ch0.tones[b]
                v = 0x31 if r.nwavs < 1 else r.waves[0].amp_sf
            for x in w:
                x.amp_sf = v
        else:
            for j, x in enumerate(w):
                if mode == 0:
                    x.amp_sf = br.read(6)
                elif mode == 1:
                    x.amp_sf = br.huff(H[5]) + 0x14
                elif mode == 2:
                    d = _sext(br.huff(H[6]), 5)
                    ref = u.refwave[k + j]
                    r = ch0.tones[b]
                    x.amp_sf = ((d + 0x22) if ref < 0 else (r.waves[ref].amp_sf + d)) & 0x3F
                else:
                    ref = u.refwave[k + j]
                    r = ch0.tones[b]
                    x.amp_sf = 0x20 if ref < 0 else r.waves[ref].amp_sf
            k += tb.nwavs


def _tone_ampidx(br, u, c, t, mode):
    nb = u.ntone_bands
    H = t.H_TONE
    flags = c.tone_flags
    bands = c.tones
    ch0 = u.ch[0]
    k = 0
    for b in range(nb):
        if not flags[b]:
            continue
        tb = bands[b]
        w = tb.waves
        if mode == 0:
            for x in w:
                x.amp_idx = br.read(4)
        elif mode == 1:
            if tb.nwavs == 1:
                w[0].amp_idx = br.huff(H[7])
            else:
                for x in w:
                    x.amp_idx = br.huff(H[8])
        elif mode == 2:
            r = ch0.tones[b]
            for j, x in enumerate(w):
                d = _sext(br.huff(H[9]), 3)
                ref = u.refwave[k + j]
                x.amp_idx = ((d - 4) if ref < 0 else (r.waves[ref].amp_idx + d)) & 0xF
            k += tb.nwavs
        else:
            r = ch0.tones[b]
            for j, x in enumerate(w):
                ref = u.refwave[k + j]
                x.amp_idx = 0xE if ref < 0 else r.waves[ref].amp_idx
            k += tb.nwavs
