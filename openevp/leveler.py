"""The Leveler: Web Audio's DynamicsCompressorNode, as Chromium (and so WebView2)
runs it, ported to numpy so an export "as heard" matches what the player played.

This is a port of the algorithm in Chromium's
third_party/blink/renderer/platform/audio/dynamics_compressor.cc, which carries
this notice:

  Copyright (C) 2011 Google Inc. All rights reserved.

  Redistribution and use in source and binary forms, with or without
  modification, are permitted provided that the following conditions
  are met:

  1.  Redistributions of source code must retain the above copyright
      notice, this list of conditions and the following disclaimer.
  2.  Redistributions in binary form must reproduce the above copyright
      notice, this list of conditions and the following disclaimer in the
      documentation and/or other materials provided with the distribution.
  3.  Neither the name of Apple Computer, Inc. ("Apple") nor the names of
      its contributors may be used to endorse or promote products derived
      from this software without specific prior written permission.

  THIS SOFTWARE IS PROVIDED BY APPLE AND ITS CONTRIBUTORS "AS IS" AND ANY
  EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
  WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
  DISCLAIMED. IN NO EVENT SHALL APPLE OR ITS CONTRIBUTORS BE LIABLE FOR ANY
  DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
  (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
  LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
  ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
  (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF
  THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

What it does, as there: a static curve (linear to the threshold, an exponential
knee whose slope meets 1/ratio at threshold + knee, then the ratio) gives each
sample's attenuation; a fast detector follows it (down at once, back up within
about 2.5 ms); once per 32-sample division the gain heads for the detector's
value, at an attack rate from the attack time or an adaptive release rate from
the release time (the deeper the compression, the faster it releases), with a
sine warp; make-up gain is (1 / the curve's gain at 0 dBFS) ** 0.6.

Two things differ, on purpose or by necessity:
  - The browser delays the audio by 6 ms (the gain looks 6 ms ahead). Here the
    gain is applied 6 ms earlier instead, so the export keeps every position (the
    marks): the same samples and gains, without the delay.
  - The browser computes in float32, one sample after another. Here it is
    float64 and vectorized (the detector, a clipped affine recursion, by a prefix
    scan of its compositions; the gain within a division in closed form), so
    values differ in the last digits: against Chromium's own OfflineAudioContext
    the difference stayed 87 dB or more below the signal.
"""
import math

import numpy as np

DIVISION = 32
PRE_DELAY = 0.006               # seconds
SAT_RELEASE = 0.0025            # seconds
_ZONES = (0.09, 0.16, 0.42, 0.98)
_POLY = ((0.9999999999999998, 1.8432219684323923e-16, -1.9373394351676423e-16, 8.824516011816245e-18),
         (-1.5788320352845888, 2.3305837032074286, -0.9141194204840429, 0.1623677525612032),
         (0.5334142869106424, -1.272736789213631, 0.9258856042207512, -0.18656310191776226),
         (0.08783463138207234, -0.1694162967925622, 0.08588057951595272, -0.00429891410546283),
         (-0.042416883008123074, 0.1115693827987602, -0.09764676325265872, 0.028494263462021576))


def _db(x):
    return 20 * math.log10(x) if x > 0 else -math.inf


def _lin(db):
    return 10 ** (db / 20)


class Curve:
    """The static curve for a threshold (dB), knee (dB) and ratio."""

    def __init__(self, threshold, knee, ratio):
        self.lin_threshold = _lin(threshold)
        self.slope = 1 / ratio
        self.threshold, self.knee_db = threshold, knee
        self.k = self._k_at_slope(1 / ratio)
        self.db_knee_threshold = threshold + knee
        self.knee_threshold = _lin(self.db_knee_threshold)
        self.db_yknee = _db(self._knee_curve(self.knee_threshold, self.k))

    def _knee_curve(self, x, k):
        if x < self.lin_threshold:
            return x
        return self.lin_threshold + (1 - math.exp(-k * (x - self.lin_threshold))) / k

    def _k_at_slope(self, desired):
        db_x = self.threshold + self.knee_db
        x = _lin(db_x)
        x2, db_x2 = 1.0, 0.0
        if not x < self.lin_threshold:
            x2 = x * 1.001
            db_x2 = _db(x2)
        lo, hi, k, slope = 0.1, 10000.0, 5.0, 1.0
        for _ in range(15):
            if not x < self.lin_threshold:
                slope = (_db(self._knee_curve(x2, k)) - _db(self._knee_curve(x, k))) / (db_x2 - db_x)
            if slope < desired:
                hi = k
            else:
                lo = k
            k = math.sqrt(lo * hi)
        return k

    def saturate(self, x):
        """The curve applied to levels x >= 0 (an array)."""
        x = np.asarray(x, np.float64)
        lt, k = self.lin_threshold, self.k
        knee = np.where(x < lt, x, lt + (1 - np.exp(-k * (x - lt))) / k)
        with np.errstate(divide="ignore"):
            ratio = 10 ** ((self.db_yknee + self.slope * (20 * np.log10(x) - self.db_knee_threshold)) / 20)
        return np.where(x < self.knee_threshold, knee, ratio)

    def makeup(self):
        """The make-up gain: (1 / saturate(1)) ** 0.6."""
        return float((1 / self.saturate(1.0)) ** 0.6)


def _scan(a, b, c):
    """Prefix compositions of the maps v -> min(c[n], a[n] * v + b[n]): returns
    (A, B, C) with v_n = min(C[n], A[n] * v_-1 + B[n]) (Hillis-Steele scan)."""
    A, B, C = a.copy(), b.copy(), c.copy()
    s = 1
    while s < len(A):
        # map i after map i - s:  min(C_i, A_i * min(C_j, A_j v + B_j) + B_i)
        nA = A[s:] * A[:-s]
        nB = A[s:] * B[:-s] + B[s:]
        nC = np.minimum(C[s:], A[s:] * C[:-s] + B[s:])
        A[s:], B[s:], C[s:] = nA, nB, nC
        s *= 2
    return A, B, C


class Leveler:
    """A streaming compressor for `channels` channels: call it with consecutive
    blocks (frames, channels) of float64 samples, each a multiple of DIVISION
    frames but the last. The output keeps every position (see the module note), so
    it runs `delay` frames behind the input: feed `delay` frames of silence after
    the end to get the rest."""

    def __init__(self, preset, rate, channels):
        self.curve = Curve(preset["threshold"], preset["knee"], preset["ratio"])
        self.post_gain = self.curve.makeup()
        self.attack_frames = max(0.001, preset["attack"]) * rate
        release_frames = rate * preset["release"]
        self.poly = [release_frames * sum(w * z for w, z in zip(row, _ZONES)) for row in _POLY]
        self.sat_frames = SAT_RELEASE * rate
        self.delay = min(1023, int(PRE_DELAY * rate))
        self.detector = 0.0                  # as the browser starts: detector 0, gain 1
        self.gain = 1.0
        self.max_attack_db = -1.0
        self.held = np.zeros((self.delay, channels))   # audio waiting for its gain (the look-ahead)
        self.to_drop = self.delay

    def _gains(self, level):
        """The gain for each sample whose detector input level (max abs over the channels) is given."""
        n = len(level)
        att = np.where(level <= 0.0001, 1.0, self.curve.saturate(level) / np.maximum(level, 1e-30))
        with np.errstate(divide="ignore"):
            db_att = np.maximum(2.0, -20 * np.log10(att))
        r = 10 ** (db_att / 20 / self.sat_frames) - 1
        A, B, C = _scan(1 - r, r * att, att)
        det = np.minimum(np.minimum(C, A * self.detector + B), 1.0)
        divisions = -(-n // DIVISION)
        starts = np.empty(divisions)                 # the detector as each division starts
        starts[0] = self.detector
        starts[1:] = det[DIVISION - 1:(divisions - 1) * DIVISION:DIVISION]
        g0s, targets, rates = np.empty(divisions), np.empty(divisions), np.empty(divisions)
        a, b, c, d, e = self.poly
        g, max_db, attack_frames = self.gain, self.max_attack_db, self.attack_frames
        for i, desired in enumerate(starts.tolist()):
            scaled = math.asin(min(1.0, max(-1.0, desired))) / (math.pi / 2)
            releasing = scaled > g
            if scaled == 0:
                diff = -1.0 if releasing else 1.0
            else:
                diff = _db(g / scaled) if g > 0 else -math.inf
            if releasing:
                max_db = -1.0
                if not math.isfinite(diff):
                    diff = -1.0
                x = 0.25 * (min(0.0, max(-12.0, diff)) + 12)
                frames = a + b * x + c * x * x + d * x ** 3 + e * x ** 4
                rate = _lin(5 / frames)
            else:
                if not math.isfinite(diff):
                    diff = 1.0
                if max_db == -1 or max_db < diff:
                    max_db = diff
                rate = 1 - (0.25 / max(0.5, max_db)) ** (1 / attack_frames)
            g0s[i], targets[i], rates[i] = g, scaled, rate
            steps = min(DIVISION, n - i * DIVISION)
            g = scaled + (g - scaled) * (1 - rate) ** steps if rate < 1 else min(1.0, g * rate ** steps)
        self.gain, self.max_attack_db = g, max_db
        self.detector = float(det[-1])
        k = (np.arange(n) % DIVISION + 1).astype(np.float64)    # steps into its division
        div = np.arange(n) // DIVISION
        g0, tg, rt = g0s[div], targets[div], rates[div]
        with np.errstate(over="ignore"):
            attack = tg + (g0 - tg) * np.abs(1 - rt) ** k
            release = np.minimum(1.0, g0 * np.where(rt >= 1, rt, 1.0) ** k)
        gain = np.where(rt < 1, attack, release)
        return self.post_gain * np.sin(np.pi / 2 * gain)

    def __call__(self, x):
        gains = self._gains(np.abs(x).max(axis=1))
        audio = np.concatenate([self.held, x]) if self.delay else x
        out = audio[:len(x)] * gains[:, None]               # each sample with the gain from `delay` later
        self.held = audio[len(x):]
        if self.to_drop:                                    # the browser's first `delay` outputs: silence
            cut = min(self.to_drop, len(out))
            self.to_drop -= cut
            out = out[cut:]
        return out
