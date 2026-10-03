"""Splitting an analog import on silence (a recorder played into line-in).

Nothing is ever thrown away: the pieces, put back together in order, are the
input exactly, sample for sample. Splitting only decides where one file ends
and the next begins. (The import also keeps the whole input as one file beside
the pieces: see app/live.py.)

The audio is looked at in 50 ms blocks. A block is quiet when its level is
within MARGIN_DB of the idle floor: the quietest the input has been (the low
percentile of each second, the lowest one seen), which is the hiss of the
cable and the sound card while the recorder plays nothing. A recording's own
background (room tone, the recorder's microphone hiss) is normally louder than
that, so it counts as part of the recording.

The first piece starts with the first sample. When the input has been quiet
for `gap` seconds and the piece may end there (below), a split is pending: the
quiet keeps going into the piece, except the last PREROLL seconds, which are
held back; when sound comes again the piece ends and the next one starts with
those held-back seconds. Stop with a split pending: the held-back audio goes
into the last piece.

A piece may only end when there is evidence that the quiet is a gap between
recordings and not a pause inside one:
- its own background is distinct from its content: the 95th percentile of its
  blocks (its loud parts) is at least MARGIN_DB over the 20th (its background),
  so a piece of steady sound gives no evidence of a background at all;
- that background is at least MARGIN_DB above the idle floor, so the quiet now
  is clearly quieter than the recording ever is;
- the levels are judged on at least EVIDENCE seconds of the piece, not counting
  the quiet run being judged, and the piece has at least MIN_SIGNAL seconds of
  loud blocks (a click as Play is pressed is not a recording of its own).
Without that evidence the quiet becomes part of the piece and it is not split
(this happens, for one, when Play was pressed before Record and a recording's
background is the quietest thing heard so far; a later real gap, quieter than
that background, lowers the floor and splitting starts working).

The splitter tells a sink what to do: start(stream_frame), write(pcm bytes),
end(). stream_frame is where the piece starts, counted in frames since the first
feed().
"""
import collections
import math

import numpy as np

BLOCK_SECONDS = 0.05
MARGIN_DB = 8.0
MIN_FLOOR_DB = -100.0        # an all-zero input is never taken as the floor itself (16-bit noise is about -101 dBFS)
PREROLL = 0.5
MIN_SIGNAL = 1.0
EVIDENCE = 3.0               # seconds of a piece (before the quiet run) needed to judge its levels
FLOOR_PERCENTILE = 0.2
CONTENT_PERCENTILE = 0.95
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
    """A histogram of block levels (0.5 dB bins), for percentiles without keeping them all."""

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
                 min_signal=MIN_SIGNAL):
        self.rate, self.channels, self.sink = rate, channels, sink
        self.block = max(1, int(round(rate * BLOCK_SECONDS)))
        self.margin = margin_db
        self.gap_blocks = max(1, int(math.ceil(gap / BLOCK_SECONDS)))
        self.pre_blocks = int(round(preroll / BLOCK_SECONDS))
        self.min_blocks = int(math.ceil(min_signal / BLOCK_SECONDS))
        self.evidence_blocks = int(math.ceil(EVIDENCE / BLOCK_SECONDS))
        self.window = collections.deque(maxlen=int(round(1.0 / BLOCK_SECONDS)))
        self.idle = None                         # the idle floor (dBFS), once a second has been seen
        self.frames = 0                          # frames looked at so far (whole blocks)
        self.started = False
        self.pending = False                     # a split waits for the next sound
        self._rest = np.zeros(0, dtype=np.int16)
        self._held = collections.deque()         # (stream frame, bytes): the pre-roll held back while pending
        self._new_piece()

    def _new_piece(self):
        self._levels = _Levels()                 # the piece's block levels, the quiet run excluded
        self._quiet_db = []                      # levels of the quiet run, not counted yet
        self._quiet_run = 0
        self._loud = 0

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
        if self.idle is None or self._loud < self.min_blocks or lv.count < self.evidence_blocks:
            return False
        background, content = lv.percentile(FLOOR_PERCENTILE), lv.percentile(CONTENT_PERCENTILE)
        return content - background >= self.margin and background >= max(self.idle, MIN_FLOOR_DB) + self.margin

    def _commit(self):
        """The quiet run is part of the piece after all: counted in its levels."""
        for d in self._quiet_db:
            self._levels.add(d)
        self._quiet_db = []

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
        if not self.started:
            self.sink.start(0)
            self.started = True
        self._learn(db)
        t = self.threshold()
        loud = t is not None and db > t
        if self.pending:
            if loud:                             # the next recording: the split happens here
                held = list(self._held)
                self._held.clear()
                self.sink.end()
                self.sink.start(held[0][0] if held else at)
                for _, p in held:
                    self.sink.write(p)
                self.sink.write(pcm)
                self.pending = False
                self._new_piece()
                self._levels.add(db)
                self._loud = 1
                return
            self._held.append((at, pcm))
            while len(self._held) > self.pre_blocks:
                self.sink.write(self._held.popleft()[1])
            return
        self.sink.write(pcm)
        if loud:
            self._commit()
            self._levels.add(db)
            self._loud += 1
            self._quiet_run = 0
            return
        if not self._loud:                       # quiet before the piece's first sound: the gap before it,
            return                               # not the piece's own background (and never a split)
        self._quiet_db.append(db)
        self._quiet_run += 1
        if self._quiet_run >= self.gap_blocks:
            if self._splittable():
                self.pending = True
            else:
                self._commit()                   # no telling this from a pause: part of the piece

    def finish(self):
        """Stop: everything not written yet (held-back pre-roll, the last partial
        block) goes into the piece being written, which then ends."""
        for _, p in self._held:
            self.sink.write(p)
        self._held.clear()
        if len(self._rest):
            if not self.started:
                self.sink.start(0)
                self.started = True
            self.sink.write(self._rest.tobytes())
            self._rest = np.zeros(0, dtype=np.int16)
        if self.started:
            self.sink.end()
            self.started = False
