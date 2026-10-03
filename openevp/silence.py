"""Splitting an analog import on silence (a recorder played into line-in).

The audio is looked at in 50 ms blocks. A block is quiet when its level is
within MARGIN_DB of the idle floor: the quietest the input has been (the low
percentile of each second, the lowest one seen), which is the hiss of the
cable and the sound card while the recorder plays nothing. So that hiss is
never taken for a recording, and a recording's own background (room tone,
the recorder's microphone hiss) counts as part of the recording, so its pauses
never split it.

A piece starts at the first loud block (with PREROLL seconds before it) and
ends once the input has been quiet for `gap` seconds; POSTROLL seconds of that
quiet are kept and the rest of the gap is dropped. Two guards keep a recording
in one piece:
- a piece only ends on quiet clearly below the piece itself: when the piece's
  own quiet level (the 20th percentile of its blocks so far, not counting the
  quiet run being judged; at least EVIDENCE seconds of them) is not at least
  MARGIN_DB above the idle floor, nothing could tell its pauses from a gap, so
  it is not split (this happens when Play was pressed before Record and the
  recording's background is the quietest thing heard; a later real gap, quieter
  than that background, lowers the floor and splitting starts working);
- a piece with less than MIN_SIGNAL seconds of loud blocks (a click as Play is
  pressed) is discarded, not kept as a file.

The splitter tells a sink what to do: start(stream_frame), write(pcm bytes),
end(), discard(). stream_frame is where the piece starts, counted in frames
since the first feed().
"""
import collections
import math

import numpy as np

BLOCK_SECONDS = 0.05
MARGIN_DB = 8.0
MIN_FLOOR_DB = -100.0        # an all-zero input is never taken as the floor itself (16-bit noise is about -101 dBFS)
PREROLL = 0.5
POSTROLL = 0.5
MIN_SIGNAL = 1.0
EVIDENCE = 3.0               # seconds of a piece (before the quiet run) needed to judge its own quiet level
FLOOR_PERCENTILE = 0.2
DEFAULT_GAP = 3.0
MAX_GAP = 60.0
SILENT_DB = -120.0


def block_levels(samples, channels, block):
    """dBFS (RMS, all channels) of each whole block of `block` frames in an int16 array."""
    n = len(samples) // (channels * block)
    if n == 0:
        return np.zeros(0)
    x = samples[:n * channels * block].astype(np.float64).reshape(n, channels * block) / 32768.0
    ms = np.mean(x * x, axis=1)
    with np.errstate(divide="ignore"):
        return np.maximum(SILENT_DB, 10.0 * np.log10(ms))


class _Levels:
    """A histogram of block levels (0.5 dB bins), for a percentile without keeping them all."""

    def __init__(self):
        self.bins = np.zeros(int(-SILENT_DB * 2) + 1, dtype=np.int64)
        self.count = 0

    def add(self, db):
        self.bins[min(len(self.bins) - 1, max(0, int((db - SILENT_DB) * 2)))] += 1
        self.count += 1

    def percentile(self, p):
        if not self.count:
            return None
        k = np.searchsorted(np.cumsum(self.bins), max(1, math.ceil(p * self.count)))
        return SILENT_DB + k / 2.0


class Splitter:
    def __init__(self, rate, channels, sink, gap=DEFAULT_GAP, margin_db=MARGIN_DB, preroll=PREROLL,
                 postroll=POSTROLL, min_signal=MIN_SIGNAL):
        self.rate, self.channels, self.sink = rate, channels, sink
        self.block = max(1, int(round(rate * BLOCK_SECONDS)))
        self.margin = margin_db
        self.gap_blocks = max(1, int(math.ceil(gap / BLOCK_SECONDS)))
        self.pre_blocks = int(round(preroll / BLOCK_SECONDS))
        self.post_blocks = int(round(postroll / BLOCK_SECONDS))
        self.min_blocks = int(math.ceil(min_signal / BLOCK_SECONDS))
        self.evidence_blocks = int(math.ceil(EVIDENCE / BLOCK_SECONDS))
        self.window = collections.deque(maxlen=int(round(1.0 / BLOCK_SECONDS)))
        self.idle = None                         # the idle floor (dBFS), once a second has been seen
        self.frames = 0                          # frames looked at so far (whole blocks)
        self._rest = np.zeros(0, dtype=np.int16)
        self._pre = collections.deque(maxlen=max(1, self.pre_blocks))   # (stream frame, bytes) while no piece
        self.in_piece = False
        self._quiet = []                         # quiet blocks of the piece not written yet
        self._quiet_db = []                      # ... and their levels
        self._quiet_run = 0                      # quiet blocks in a row (written or not)
        self._loud = 0                           # loud blocks in the piece
        self._levels = None                      # the piece's block levels (_Levels)

    # ---- the floor --------------------------------------------------------------
    def threshold(self):
        """A block louder than this is part of a recording (None: nothing learnt yet)."""
        if self.idle is None:
            return None
        return max(self.idle, MIN_FLOOR_DB) + self.margin

    def _learn(self, db):
        self.window.append(db)
        if len(self.window) == self.window.maxlen:
            low = sorted(self.window)[int(FLOOR_PERCENTILE * (len(self.window) - 1))]
            if self.idle is None or low < self.idle:
                self.idle = low

    def _splittable(self):
        lv = self._levels
        if lv is None or lv.count < self.evidence_blocks or self.idle is None:
            return False
        return lv.percentile(FLOOR_PERCENTILE) >= max(self.idle, MIN_FLOOR_DB) + self.margin

    # ---- feeding ----------------------------------------------------------------
    def feed(self, data):
        """Look at more audio (16-bit PCM bytes, whole frames)."""
        x = np.frombuffer(data, dtype="<i2")
        if len(self._rest):
            x = np.concatenate([self._rest, x])
        step = self.block * self.channels
        n = len(x) // step
        levels = block_levels(x, self.channels, self.block)
        for i in range(n):
            self._block(x[i * step:(i + 1) * step].tobytes(), float(levels[i]))
        self._rest = x[n * step:].copy()

    def _block(self, pcm, db):
        at = self.frames
        self.frames += self.block
        self._learn(db)
        t = self.threshold()
        loud = t is not None and db > t
        if not self.in_piece:
            if loud:
                self._start(at, pcm, db)
            elif self.pre_blocks:
                self._pre.append((at, pcm))
            return
        if loud:
            self._commit()
            self._levels.add(db)
            self._quiet_run = 0
            self._loud += 1
            self.sink.write(pcm)
            return
        self._quiet.append(pcm)
        self._quiet_db.append(db)
        self._quiet_run += 1
        if self._quiet_run >= self.gap_blocks:
            if self._splittable():
                self._end(keep=self.post_blocks)
            else:
                self._commit()                   # no telling its pauses from a gap: part of the piece

    def _commit(self):
        """The quiet blocks held back belong to the piece after all: written, and counted
        in its levels (the quiet run being judged is never part of them)."""
        for q in self._quiet:
            self.sink.write(q)
        for d in self._quiet_db:
            self._levels.add(d)
        self._quiet, self._quiet_db = [], []

    def _start(self, at, pcm, db):
        pre = list(self._pre)
        self._pre.clear()
        self.sink.start(pre[0][0] if pre else at)
        for _, p in pre:
            self.sink.write(p)
        self.sink.write(pcm)
        self.in_piece = True
        self._quiet, self._quiet_db, self._quiet_run, self._loud = [], [], 0, 1
        self._levels = _Levels()
        self._levels.add(db)

    def _end(self, keep):
        kept = self._quiet[:keep]
        dropped = self._quiet[keep:]
        for q in kept:
            self.sink.write(q)
        if self._loud >= self.min_blocks:
            self.sink.end()
        else:
            self.sink.discard()
        self.in_piece = False
        # The end of the gap may hold the start of the next recording's pre-roll.
        first = self.frames - len(dropped) * self.block
        self._pre.clear()
        for i, q in enumerate(dropped[-self.pre_blocks:] if self.pre_blocks else []):
            self._pre.append((first + (len(dropped) - min(len(dropped), self.pre_blocks) + i) * self.block, q))
        self._quiet, self._quiet_db, self._quiet_run, self._loud, self._levels = [], [], 0, 0, None

    def finish(self):
        """Stop: the piece being recorded ends (its quiet tail cut to POSTROLL), or is
        discarded when it is too short. The last partial block is dropped."""
        if self.in_piece:
            self._end(keep=self.post_blocks)
