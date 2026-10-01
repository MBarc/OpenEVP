"""Noise reduction from a noise sample: spectral gating.

learn() takes a stretch of pure background noise and keeps its noise profile:
the magnitude of each frequency bin (RMS over the sample), per channel. reduce()
then lowers every bin of the recording that is not clearly above that profile:

  - STFT: periodic Hann windows of about 32 ms (256 samples at 8 kHz, 512 at
    16 kHz, 1024 at 44.1/48 kHz), 75% overlap, on a grid fixed to the start of
    the recording (frame k starts at (k - 3) * hop; zeros outside it).
  - the power of each bin smoothed over 5 frames (about 40 ms) and 5 bins
    first: a lone noise peak never opens a bin by itself (that is what makes the
    "musical noise", warbling watery tones, of plain spectral subtraction), while
    a tone or a voice, many dB above the noise, stays far above the gate;
  - a gate per bin from that smoothed level: 0 at or below GATE_LOW_DB above
    the profile, 1 from GATE_HIGH_DB up, a straight line (in dB) between;
  - gain = 1 - depth * (1 - gate), depth = 1 - 10 ** (-reduction / 20), the
    reduction being amount% of MAX_REDUCTION_DB. The default (DEFAULT_AMOUNT)
    is mild on purpose: a strong reduction leaves watery artefacts that can sound
    like whispers or voices.
  - weighted overlap-add with the same window (Hann squared at 75% overlap sums
    to 1.5 everywhere), so with nothing gated the output is the input.

The output has exactly the recording's length, rate, channels and sample format,
so every position (marks, selections) stays where it is. Any range can be computed
on its own (reduce(f0, f1)) and gives the very samples the whole recording would
have there: each frame, its gate and its smoothing depend only on the grid and
the samples around it, and every sum is taken in the same order. Deterministic
(numpy, no randomness, no threads).
"""
import hashlib

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from . import pcm

GATE_LOW_DB = 3.0
GATE_HIGH_DB = 10.0
TIME_SMOOTH = 2             # frames on each side
FREQ_SMOOTH = 2             # bins on each side
MAX_REDUCTION_DB = 30.0
DEFAULT_AMOUNT = 40         # percent: 12 dB
MIN_NOISE_SECONDS = 0.25
CHUNK_SAMPLES = 1 << 21     # output computed in chunks of about this many FFT input samples (hops x FFT size)


class Cancelled(Exception):
    """should_stop() said stop."""


frame_size = pcm.frame_size


class Profile:
    """A noise profile: the magnitude of each frequency bin (its RMS over the noise
    sample), per channel, for one rate and frame size."""

    def __init__(self, rate, n, mag):
        self.rate, self.n, self.mag = rate, n, np.asarray(mag, np.float64)
        h = hashlib.sha256(b"noise1:%d:%d:%d\n" % (rate, n, self.mag.shape[0]))
        h.update(self.mag.astype("<f8").tobytes())
        self.id = h.hexdigest()[:16]

    @property
    def channels(self):
        return self.mag.shape[0]


def learn(reader, start, end):
    """The noise profile of frames [start, end) of reader (pcm.Wav / pcm.WavFile).
    Raises ValueError when the stretch is too short (MIN_NOISE_SECONDS) or silent."""
    n = frame_size(reader.rate)
    hop = n // 4
    start, end = max(0, int(start)), min(reader.frames, int(end))
    if end - start < max(n, MIN_NOISE_SECONDS * reader.rate):
        raise ValueError(f"select at least {MIN_NOISE_SECONDS:g} seconds of background noise")
    x = reader.read(start, end).astype(np.float64)
    count = 1 + (x.shape[0] - n) // hop
    w = pcm.hann(n)
    total = np.zeros((reader.channels, n // 2 + 1))
    for c0 in range(0, count, 4096):
        idx = (np.arange(c0, min(count, c0 + 4096)) * hop)[:, None] + np.arange(n)
        for ch in range(reader.channels):
            spec = np.fft.rfft(x[idx, ch] * w, axis=1)
            total[ch] += (spec.real ** 2 + spec.imag ** 2).sum(axis=0)
    mag = np.sqrt(total / count)
    if not np.any(mag > 1e-9):
        raise ValueError("the selected part is silent: select background noise, not silence")
    return Profile(reader.rate, n, mag)


def depth(amount):
    """How far a gated bin is lowered (0..1) for an amount in percent."""
    amount = min(100.0, max(0.0, float(amount)))
    return 1 - 10 ** (-(amount / 100) * MAX_REDUCTION_DB / 20)


def _smooth(a, axis, half):
    """Mean over 2 * half + 1 neighbours along axis (0 or 1), the ends repeated;
    each value summed in one fixed order (independent of where a range starts)."""
    if half == 0:
        return a
    pad = [(0, 0), (0, 0)]
    pad[axis] = (half, half)
    p = np.pad(a, pad, mode="edge")
    size = a.shape[axis]

    def part(i):
        return p[i:i + size] if axis == 0 else p[:, i:i + size]
    out = part(0).copy()
    for i in range(1, 2 * half + 1):
        out += part(i)
    out /= 2 * half + 1
    return out


def reduce(reader, put, profile, amount, f0=0, f1=None, should_stop=None, progress=None):
    """Noise-reduced frames [f0, f1) of reader (default: all of it), handed to
    put(samples) a chunk at a time as float32 (frames, channels), in order.
    Raises ValueError when the profile does not fit the recording, Cancelled when
    should_stop() says so."""
    total = reader.frames
    f1 = total if f1 is None else min(int(f1), total)
    f0 = max(0, int(f0))
    if profile.rate != reader.rate or profile.channels != reader.channels:
        raise ValueError("the noise profile was learnt from other audio: learn it again")
    n = profile.n
    hop = n // 4
    w = pcm.hann(n)
    frames = -(-(total + 3 * hop) // hop)                     # frame k starts at (k - 3) * hop
    noise_db = 20 * np.log10(np.maximum(profile.mag, 1e-12))[:, None, :]   # power, dB (ch, 1, bins)
    d = depth(amount)
    t = TIME_SMOOTH
    chunk = max(64, CHUNK_SAMPLES // n) * hop                  # (the chunking never changes the result)
    for a in range(f0 - f0 % hop, f1, chunk):
        if should_stop is not None and should_stop():
            raise Cancelled()
        b = min(a + chunk, f1)
        j0, j1 = a // hop, -(-b // hop)                        # output hop segments [j0, j1)
        k0, k1 = j0, j1 + 3                                    # the frames over them
        want = np.clip(np.arange(k0 - t, k1 + t), 0, frames - 1)   # with smoothing context, ends repeated
        c0, c1 = int(want[0]), int(want[-1]) + 1
        s0 = (c0 - 3) * hop
        x = reader.read(s0, (c1 - 1 - 3) * hop + n).astype(np.float64)
        seg = np.zeros((j1 - j0, hop, reader.channels))
        for ch in range(reader.channels):
            frames_ = sliding_window_view(np.ascontiguousarray(x[:, ch]), n)[::hop][:c1 - c0]
            spec = np.fft.rfft(frames_ * w, axis=1)                            # (frames, bins)
            power = (spec.real ** 2 + spec.imag ** 2)[want - c0]               # (k1 - k0 + 2t, bins)
            power = _smooth(_smooth(power, 0, t)[t:t + (k1 - k0)], 1, FREQ_SMOOTH)
            level = 10 * np.log10(np.maximum(power, 1e-24))
            gate = np.clip((level - noise_db[ch] - GATE_LOW_DB) / (GATE_HIGH_DB - GATE_LOW_DB), 0, 1)
            y = np.fft.irfft(spec[k0 - c0:k1 - c0] * (1 - d * (1 - gate)), n, axis=1) * w
            q = y.reshape(k1 - k0, 4, hop)
            m = j1 - j0                                        # segment j: frames j+3, j+2, j+1, j (quarters 0..3)
            seg[:, :, ch] = ((q[3:3 + m, 0] + q[2:2 + m, 1]) + q[1:1 + m, 2]) + q[0:m, 3]
        out = (seg / 1.5).reshape(-1, reader.channels)
        lo, hi = max(a, f0) - j0 * hop, b - j0 * hop
        put(out[lo:hi].astype(np.float32))
        if progress is not None:
            progress(b - f0, f1 - f0)


def reduce_wav(wav_bytes, profile, amount, should_stop=None, progress=None):
    """A whole WAV (bytes) noise-reduced: a WAV of the same format and length (its
    fmt chunk as it was, no other chunks)."""
    src = pcm.Wav(wav_bytes)
    out = src.output()
    reduce(src, out.put, profile, amount, should_stop=should_stop, progress=progress)
    return out.done()


def reduce_frames(wav_bytes, profile, amount, first, last):
    """The PCM bytes of frames [first, last) of a WAV, noise-reduced: exactly those
    samples of reduce_wav() (a clip of a noise-reduced recording)."""
    src = pcm.Wav(wav_bytes)
    parts = []
    reduce(src, lambda y: parts.append(pcm.encode(y, src.kind, src.width)), profile, amount, first, last)
    return b"".join(parts)
