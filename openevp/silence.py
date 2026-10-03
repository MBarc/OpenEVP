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
quiet keeps going into the piece; when sound comes again the piece ends and the
next one starts with the last PREROLL seconds of the quiet. Stop: whatever is
held back goes into the last piece.

A piece may only end when there is clear evidence that the quiet is a gap
between recordings and not a pause inside one (when in doubt, it does not end;
the whole input is kept as one file as well):
- the piece has a background of its own: a level it holds steadily for whole
  seconds (block levels within STEADY_DB), in at least two separate stretches
  (it comes back), the quietest such level above the floor by the margin. A
  quieter passage heard once, or loudness that just changes, is not one;
- its content stands out from that background (the 95th percentile of its
  blocks is at least MARGIN_DB over it), and the background is at least
  MARGIN_DB above the idle floor;
- the quiet run itself is at the idle floor seen before (within MATCH_DB): the
  input went back to where it was when the recorder played nothing;
- at least EVIDENCE seconds of the piece were judged, not counting the quiet run
  being judged, nor quiet before its first sound; and it has at least MIN_SIGNAL
  seconds of loud blocks (a click as Play is pressed joins the next piece).
Without that evidence the quiet becomes part of the piece and it is not split.
When Play was pressed before Record, the first recording's background was the
floor at first, and it stays joined to the next recording.

The last PREROLL seconds of every quiet run are held back before they are
written, so the piece that starts at the next sound always begins with them.

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
STEADY_DB = 6.0              # a whole second whose block levels (10th to 90th percentile) stay within this is steady
MATCH_DB = 4.0               # levels within this of each other are the same level
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
        self._held = collections.deque()         # (stream frame, bytes): the last quiet PREROLL, held back
        self._new_piece()

    def _new_piece(self):
        self._levels = _Levels()                 # the piece's block levels, the quiet run excluded
        self._windows = []                       # the piece's whole seconds: (median dB, steady?), in order
        self._second = []                        # block levels of the second being filled
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

    # ---- what the piece has shown of itself ----------------------------------------
    def _count(self, db):
        """One block of the piece (in order): its levels, and its whole seconds."""
        self._levels.add(db)
        self._second.append(db)
        if len(self._second) == self.window.maxlen:
            v = sorted(self._second)
            n = len(v)
            spread = v[int(0.9 * (n - 1))] - v[int(0.1 * (n - 1))]
            self._windows.append((v[n // 2], spread <= STEADY_DB))
            self._second = []

    def background(self):
        """The piece's own background: the quietest level it holds steadily (a whole second
        within STEADY_DB) in at least two separate stretches, above the floor by the margin;
        None if it shows none. A quieter passage heard once is not a background."""
        if self.idle is None:
            return None
        floor = max(self.idle, MIN_FLOOR_DB) + self.margin
        steady = [(i, m) for i, (m, ok) in enumerate(self._windows) if ok and m >= floor]
        for _, level in sorted(steady, key=lambda x: x[1]):
            hits = [i for i, m in steady if abs(m - level) <= MATCH_DB]
            stretches = sum(1 for k, i in enumerate(hits) if k == 0 or i != hits[k - 1] + 1)
            if stretches >= 2:
                return level
        return None

    def _splittable(self):
        """May the piece end in the quiet run now? Only on clear evidence (see the module's
        docstring); when in doubt, no."""
        lv = self._levels
        if self.idle is None or self._loud < self.min_blocks or lv.count < self.evidence_blocks:
            return False
        background = self.background()
        if background is None or lv.percentile(CONTENT_PERCENTILE) < background + self.margin:
            return False
        run = sorted(self._quiet_db)
        if not run or abs(run[len(run) // 2] - max(self.idle, MIN_FLOOR_DB)) > MATCH_DB:
            return False                         # the quiet is not the input's idle level seen before
        return background >= max(self.idle, MIN_FLOOR_DB) + self.margin

    def _commit(self):
        """The quiet run is part of the piece after all: counted in its levels."""
        for d in self._quiet_db:
            self._count(d)
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
        if loud:
            held = list(self._held)
            self._held.clear()
            if self.pending:                     # the next recording: the split happens here,
                self.sink.end()                  # PREROLL seconds before its first sound
                self.sink.start(held[0][0] if held else at)
                self.pending = False
                self._new_piece()
                self._quiet_run = 0
            for _, p in held:
                self.sink.write(p)
            self.sink.write(pcm)
            self._commit()
            self._count(db)
            self._loud += 1
            self._quiet_run = 0
            return
        # Quiet: the last PREROLL seconds of it are always held back (in order), so that if the
        # next sound starts a new piece, that piece begins with them.
        self._held.append((at, pcm))
        while len(self._held) > self.pre_blocks:
            self.sink.write(self._held.popleft()[1])
        if self.pending or not self._loud:       # a split waits; or quiet before the piece's first
            return                               # sound (the gap before it, never its background)
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
