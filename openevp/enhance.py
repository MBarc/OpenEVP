"""Listening enhancements: Boost, Leveler, Voice filter, Cut rumble, Cut hiss and
Hum remover -- the same chain the player builds live with the Web Audio API
(app/ui/app.js, enhanceGraph()), here in numpy for exports "as heard".

The chain, in order (graph()):
  filters    Cut rumble (high-pass 120 Hz), Voice filter (high-pass 300 Hz and
             low-pass 3400 Hz), Cut hiss (low-pass 5 kHz; only for recordings
             sampled at 12 kHz or more: below that there is little or nothing
             above 5 kHz), Hum remover (notches at 50 or 60 Hz and its next three
             harmonics, those below 0.95 x Nyquist). High- and low-passes are
             2nd-order Butterworth (12 dB/octave); a notch is 5 Hz wide.
  leveler    the Web Audio DynamicsCompressorNode (Light / Medium / Strong
             presets), its make-up gain included: openevp.leveler is a port of
             Chromium's own, so it matches what WebView2 plays.
  boost      0 to +24 dB of gain.
  limiter    a soft clipper, after Boost or the Leveler only: linear up to 0.8 of
             full scale, then tanh-shaped towards full scale, so a boosted
             recording never clips. (It is a Web Audio WaveShaperNode with the
             same curve; LIMIT_RANGE covers +36 dB of headroom.)

The biquads use the Audio EQ Cookbook formulas exactly as the Web Audio spec
defines BiquadFilterNode, applied as one FIR (the cascade's impulse response,
sampled from its frequency response and cut where it has decayed below 1e-7)
by FFT overlap-add, block by block, so even a 90-minute recording is never
held as floats all at once. Measured against Chromium's own
OfflineAudioContext at the recording's rate, the filters, Boost and the limiter
agree to within float32 rounding (-100 dB or better) and the Leveler to within a
small fraction of a dB (see openevp.leveler). The player differs a little more:
it runs the chain at the output device's rate (usually 48 kHz) after
resampling, so a filter close to the recording's Nyquist bends slightly
differently, and its Leveler delays the sound by 6 ms (the export does not: it
keeps every position). Output is deterministic (numpy, no randomness, no threads).
"""
import math

import numpy as np

from . import leveler, pcm

BOOST_MAX = 24
BUTTERWORTH_Q = 1 / math.sqrt(2)
RUMBLE_HZ = 120.0
VOICE_HZ = (300.0, 3400.0)
HISS_HZ = 5000.0
HISS_MIN_RATE = 12000       # Cut hiss needs this sample rate (6 kHz Nyquist) or more
HUM = ("off", "60", "50")
HUM_HARMONICS = 4           # the fundamental and three harmonics
HUM_WIDTH_HZ = 5.0          # notch bandwidth: Q = frequency / width
STRENGTHS = ("light", "medium", "strong")
LEVELER = {                 # threshold dB, knee dB, ratio, attack s, release s
    "light": {"threshold": -24.0, "knee": 12.0, "ratio": 2.0, "attack": 0.010, "release": 0.25},
    "medium": {"threshold": -32.0, "knee": 10.0, "ratio": 4.0, "attack": 0.005, "release": 0.25},
    "strong": {"threshold": -42.0, "knee": 6.0, "ratio": 8.0, "attack": 0.003, "release": 0.20},
}
LIMIT_RANGE = 64.0          # the limiter's curve covers inputs of +-64 (the page scales by 1/64 first)
LIMIT_KNEE = 0.8            # linear up to here
LIMIT_POINTS = 32769        # points on that curve (odd: 0 is a point)
SUFFIX = "_enhanced"        # exports "as heard": <stem>[_0.5x[-tape]]_enhanced
BLOCK = 1 << 15             # the smallest processing block, in frames (a multiple of the Leveler's 32-sample division)

DEFAULT = {"boost": 0, "leveler": False, "strength": "medium", "voice": False, "rumble": False,
           "hiss": False, "hum": "off"}


def spec():
    """The constants the page builds its Web Audio graph from (capabilities())."""
    return {"boost_max": BOOST_MAX, "q": BUTTERWORTH_Q, "rumble": RUMBLE_HZ, "voice": list(VOICE_HZ),
            "hiss": HISS_HZ, "hiss_min_rate": HISS_MIN_RATE, "hum_harmonics": HUM_HARMONICS,
            "hum_width": HUM_WIDTH_HZ, "leveler": LEVELER, "limit_range": LIMIT_RANGE,
            "limit_knee": LIMIT_KNEE, "limit_points": LIMIT_POINTS}


def _number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def normalize(value, strict=True):
    """Settings as a full dict of DEFAULT's keys. strict: refuse anything wrong
    (ValueError); otherwise each damaged or missing field falls back to its default
    (a stored setting). Boost is rounded to whole dB."""
    if not isinstance(value, dict):
        if strict:
            raise ValueError("unknown enhancement settings")
        value = {}
    out = dict(DEFAULT)
    checks = {"boost": lambda v: _number(v) and 0 <= v <= BOOST_MAX,
              "leveler": lambda v: isinstance(v, bool), "strength": lambda v: v in STRENGTHS,
              "voice": lambda v: isinstance(v, bool), "rumble": lambda v: isinstance(v, bool),
              "hiss": lambda v: isinstance(v, bool), "hum": lambda v: v in HUM}
    for k, ok in checks.items():
        if k not in value:
            if strict:
                raise ValueError(f"enhancement setting {k!r} is missing")
            continue
        if ok(value[k]):
            out[k] = value[k]
        elif strict:
            raise ValueError(f"enhancement setting {k!r} is not valid")
    out["boost"] = int(round(out["boost"]))
    return out


def hiss_available(rate):
    return rate >= HISS_MIN_RATE


def graph(settings, rate):
    """The stages for a recording at this sample rate, in order: ("highpass" |
    "lowpass" | "notch", frequency, Q), ("compressor", preset), ("gain", dB),
    ("limiter",). Empty when nothing is on."""
    s = normalize(settings, strict=False)
    nyq = rate / 2
    stages = []

    def filt(kind, f, q):
        if f < 0.95 * nyq:
            stages.append((kind, float(f), float(q)))
    if s["rumble"]:
        filt("highpass", RUMBLE_HZ, BUTTERWORTH_Q)
    if s["voice"]:
        filt("highpass", VOICE_HZ[0], BUTTERWORTH_Q)
        filt("lowpass", VOICE_HZ[1], BUTTERWORTH_Q)
    if s["hiss"] and hiss_available(rate):
        filt("lowpass", HISS_HZ, BUTTERWORTH_Q)
    if s["hum"] != "off":
        base = float(s["hum"])
        for k in range(1, HUM_HARMONICS + 1):
            filt("notch", base * k, base * k / HUM_WIDTH_HZ)
    if s["leveler"]:
        stages.append(("compressor", s["strength"]))
    if s["boost"] > 0:
        stages.append(("gain", float(s["boost"])))
    if s["leveler"] or s["boost"] > 0:
        stages.append(("limiter",))
    return stages


def active(settings, rate):
    """Does anything change the sound of a recording at this rate?"""
    return bool(graph(settings, rate))


# ---- biquads (Audio EQ Cookbook, as Web Audio's BiquadFilterNode) -------------------
def biquad(kind, f, q, rate):
    """(b0, b1, b2, a1, a2), normalized by a0."""
    w0 = 2 * math.pi * f / rate
    c, s = math.cos(w0), math.sin(w0)
    alpha = s / (2 * q)
    if kind == "lowpass":
        b = ((1 - c) / 2, 1 - c, (1 - c) / 2)
    elif kind == "highpass":
        b = ((1 + c) / 2, -(1 + c), (1 + c) / 2)
    elif kind == "notch":
        b = (1.0, -2 * c, 1.0)
    else:
        raise ValueError(f"unknown filter {kind!r}")
    a0 = 1 + alpha
    return b[0] / a0, b[1] / a0, b[2] / a0, -2 * c / a0, (1 - alpha) / a0


def response(coefs, n):
    """The cascade's frequency response at the rfft bins of an n-point FFT."""
    z = np.exp(-2j * np.pi * np.arange(n // 2 + 1) / n)
    h = np.ones(n // 2 + 1, np.complex128)
    for b0, b1, b2, a1, a2 in coefs:
        h *= (b0 + z * (b1 + z * b2)) / (1 + z * (a1 + z * a2))
    return h


def _decay(a1, a2, floor=1e-7):
    """Samples until a biquad's impulse response has decayed below floor."""
    r = max(abs(p) for p in np.roots([1.0, a1, a2]))
    if r >= 1:
        raise ValueError("unstable filter")
    return 64 + int(math.ceil(math.log(floor) / math.log(r))) if r > 0 else 64


def impulse(coefs):
    """The cascade's impulse response (float64), cut where it has decayed below 1e-7."""
    length = sum(_decay(c[3], c[4]) for c in coefs)
    n = 1 << max(8, (4 * length - 1).bit_length())
    return np.fft.irfft(response(coefs, n), n)[:length]


def block_for(length):
    """Frames per block for an impulse response this long: at least BLOCK, and at
    least as long as it (a power of two, so whole Leveler divisions)."""
    return max(BLOCK, 1 << (max(1, length) - 1).bit_length())


class _Fir:
    """Overlap-add FFT convolution of blocks of `block` frames with h (causal)."""

    def __init__(self, h, channels, block):
        self.n = 1 << (block + len(h) - 2).bit_length()
        self.h = np.fft.rfft(h, self.n)[:, None]
        self.carry = np.zeros((self.n - block, channels), np.float64)

    def __call__(self, x):
        y = np.fft.irfft(np.fft.rfft(x, self.n, axis=0) * self.h, self.n, axis=0)
        y[:self.carry.shape[0]] += self.carry
        out = y[:x.shape[0]]
        rest = y[x.shape[0]:]
        self.carry = np.zeros_like(self.carry)
        self.carry[:min(len(rest), len(self.carry))] = rest[:len(self.carry)]
        return out


# ---- the leveler (openevp.leveler: the browser's compressor, ported) -------------------
def makeup_db(preset):
    """The Leveler's make-up gain in dB (as Web Audio's compressor adds it)."""
    return 20 * math.log10(leveler.Curve(preset["threshold"], preset["knee"], preset["ratio"]).makeup())


# ---- the limiter ------------------------------------------------------------------------
def limit_curve():
    """The soft clipper's curve: LIMIT_POINTS values of f(v) for v from -LIMIT_RANGE
    to +LIMIT_RANGE (the WaveShaperNode's curve)."""
    v = np.linspace(-LIMIT_RANGE, LIMIT_RANGE, LIMIT_POINTS)
    a = np.abs(v)
    k = LIMIT_KNEE
    soft = k + (1 - k) * np.tanh((a - k) / (1 - k))
    return np.sign(v) * np.where(a <= k, a, soft)


def limit(x, curve=None):
    """x through the soft clipper, as WaveShaperNode applies its curve (linear
    interpolation; inputs beyond the curve take its end values)."""
    curve = limit_curve() if curve is None else curve
    pos = (len(curve) - 1) / 2 * (np.clip(x / LIMIT_RANGE, -1, 1) + 1)
    k = np.minimum(np.floor(pos).astype(np.int64), len(curve) - 2)
    f = pos - k
    return (1 - f) * curve[k] + f * curve[k + 1]


# ---- the whole chain -------------------------------------------------------------------
class Chain:
    """graph(settings, rate) as a block processor for `channels` channels: call
    it with consecutive blocks of float samples (frames, channels), `block` frames
    each (the last one may be shorter). With the Leveler the output runs `latency`
    frames behind (the first call gives that many fewer): feed that many frames of
    silence after the end."""

    def __init__(self, settings, rate, channels):
        stages = graph(settings, rate)
        coefs = [biquad(k, f, q, rate) for k, f, q in (s for s in stages if s[0] in ("highpass", "lowpass", "notch"))]
        h = impulse(coefs) if coefs else None
        self.block = block_for(len(h) if h is not None else 0)
        self.fir = _Fir(h, channels, self.block) if h is not None else None
        comp = next((s[1] for s in stages if s[0] == "compressor"), None)
        self.comp = leveler.Leveler(LEVELER[comp], rate, channels) if comp else None
        self.latency = self.comp.delay if self.comp is not None else 0   # the output runs this far behind
        gain = next((s[1] for s in stages if s[0] == "gain"), 0.0)
        self.gain = 10 ** (gain / 20)
        self.curve = limit_curve() if any(s[0] == "limiter" for s in stages) else None

    def __call__(self, x):
        y = x.astype(np.float64)
        if self.fir is not None:
            y = self.fir(y)
        if self.comp is not None:
            y = self.comp(y)
        if self.gain != 1:
            y = y * self.gain
        if self.curve is not None:
            y = limit(y, self.curve)
        return y


class Cancelled(Exception):
    """should_stop() said stop."""


def process(wav_bytes, settings, should_stop=None, progress=None):
    """wav_bytes (a WAV) through the chain for its sample rate: a WAV (bytearray)
    of the same format and length, its fmt chunk as it was, no other chunks (marks
    are written back by the caller). Nothing on: the audio unchanged. Raises
    ValueError for a WAV it cannot read, Cancelled when should_stop() says so."""
    src = pcm.Wav(wav_bytes)
    out = src.output()
    if not active(settings, src.rate):
        out.put_raw(src.raw(0, src.frames))
        return out.done()
    chain = Chain(settings, src.rate, src.channels)
    for f0 in range(0, src.frames, chain.block):
        if should_stop is not None and should_stop():
            raise Cancelled()
        f1 = min(src.frames, f0 + chain.block)
        out.put(chain(src.read(f0, f1)))
        if progress is not None:
            progress(f1, src.frames)
    if chain.latency:                                   # the Leveler's last frames: silence after the end
        out.put(chain(np.zeros((chain.latency, src.channels), np.float32)))
    return out.done()
